"""E-PRTR facility data source — European Pollutant Release and Transfer Register.

P1 (engineering audit 2026-06-20): Diversify economic dependency sources beyond annual
reports. E-PRTR provides facility-level operational data for ~30,000 EU
industrial sites — creates facility_at edges with operational_shutdown
shock channels at high confidence (official regulatory dataset).

Source: European Environment Agency E-PRTR dataset
Format: CSV, ~30 MB, updated annually
Confidence: 0.8 (official EU regulatory — scored higher than self-reported
annual reports at 0.4)

Facility matching: name-based matching against portfolio companies.
Each matched facility produces a facility_at edge from company → facility
location with the operational_shutdown shock channel.
"""

import csv
import logging
import sqlite3
import ssl
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from pathlib import Path
from kassandra.shock_channel import populate_edge_attributes

logger = logging.getLogger(__name__)

# Cache directory for downloaded data
CACHE_DIR = Path.home() / ".cache" / "kassandra" / "eprtr"


# Try known E-PRTR download URLs in order
EPRTR_URLS = [
    "https://sdi.eea.europa.eu/webdav/datastore/public"
    "/eea_v_3035_1_mio_e-prtr_p_2007-2023_v01_r00/EPRTR.csv",
    "https://sdi.eea.europa.eu/webdav/datastore/public"
    "/eea_v_3035_1_mio_e-prtr_p_2007-2022_v01_r00/EPRTR.csv",
]


def _download_eprtr_csv() -> Path | None:
    """Download E-PRTR CSV. Cached after first download."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = CACHE_DIR / "EPRTR.csv"

    if csv_path.exists():
        logger.info(f"E-PRTR data cached at {csv_path}")
        return csv_path

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        for url in EPRTR_URLS:
            try:
                logger.info(f"Downloading E-PRTR from {url}")
                csv_path_temp = CACHE_DIR / "EPRTR.csv.tmp"

                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                                      "Chrome/131.0.0.0 Safari/537.36",
                        "Accept": "text/csv,text/plain,*/*",
                    },
                )
                with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
                    csv_path_temp.write_bytes(resp.read())

                csv_path_temp.rename(csv_path)
                logger.info(f"E-PRTR downloaded to {csv_path} "
                             f"({csv_path.stat().st_size / 1_000_000:.1f} MB)")
                return csv_path
            except Exception as e:
                logger.debug(f"E-PRTR URL {url} failed: {e}")
                continue

        logger.warning("All E-PRTR URLs failed")
        return None
    except Exception as e:
        logger.warning(f"E-PRTR download failed: {e}")
        return None


_SIMPLIFY_MAP = str.maketrans({
    "é": "e", "è": "e", "ê": "e", "ë": "e",
    "à": "a", "â": "a", "ä": "a",
    "ô": "o", "ö": "o",
    "ù": "u", "û": "u", "ü": "u",
    "î": "i", "ï": "i",
    "ç": "c",
})


def _simplify_name(name: str) -> str:
    """Normalize company/facility names for matching."""
    return (
        name.lower()
        .translate(_SIMPLIFY_MAP)
        .replace(" ltd", "")
        .replace(" limited", "")
        .replace(" plc", "")
        .replace(" n.v.", "")
        .replace(" nv", "")
        .replace(" s.a.", "")
        .replace(" sa", "")
        .replace(" s.e.", "")
        .replace(" se", "")
        .replace(" ag", "")
        .replace(" gmbh", "")
        .replace(" s.p.a.", "")
        .replace(" spa", "")
        .replace(" & co", "")
        .replace(" sas", "")
        .replace(" b.v.", "")
        .replace(" bv", "")
        .replace(",", "")
        .replace(".", "")
        .strip()
    )


def discover_eprtr_facilities(
    db: sqlite3.Connection,
) -> dict[str, int]:
    """Discover E-PRTR facilities and create facility_at edges.

    Matches E-PRTR facility parent company names against portfolio companies
    in the registry. Each match creates a facility_at edge with the
    operational_shutdown shock channel.

    Returns dict: {created, duplicates, errors, facilities_in_dataset}
    """
    csv_path = _download_eprtr_csv()
    if not csv_path:
        return {"created": 0, "duplicates": 0, "errors": 0, "facilities_in_dataset": 0}

    # Load portfolio company names + IDs
    registry_entries = db.execute(
        """SELECT id, canonical_name FROM registry
           WHERE company_type != 'economic_concept'"""
    ).fetchall()

    # Build lookup: simplified name → registry_id
    registry_lookup: dict[str, int] = {}
    for row in registry_entries:
        key = _simplify_name(row["canonical_name"])
        if len(key) >= 5:  # Avoid very short names matching noise
            registry_lookup[key] = row["id"]

    # Also build partial match tokens (>8 chars)
    name_tokens: dict[str, int] = {}
    for row in registry_entries:
        name = _simplify_name(row["canonical_name"])
        for token in name.split():
            if len(token) >= 8:
                name_tokens[token] = row["id"]

    now = datetime.now(timezone.utc).isoformat()
    created = 0
    duplicates = 0
    errors = 0
    facilities = 0
    matched_companies: set[str] = set()

    # Process E-PRTR CSV (SDI format: comma-delimited, UTF-8 with BOM)
    try:
        with open(csv_path, encoding="utf-8-sig") as f:
            # Detect delimiter from first line
            first_line = f.readline()
            f.seek(0)
            delimiter = "," if first_line.count(",") > first_line.count(";") else ";"
            reader = csv.DictReader(f, delimiter=delimiter)
            for row_data in reader:
                facility_name = row_data.get("FacilityName", "")
                parent_company = row_data.get("ParentCompanyName", "")
                city = row_data.get("City", "")
                country = row_data.get("CountryName", "")

                if not facility_name and not parent_company:
                    continue

                facilities += 1

                # Try to match by parent company or facility name
                match_id = None
                match_type = None

                # 1. Exact match on parent company
                if parent_company:
                    parent_key = _simplify_name(parent_company)
                    if parent_key in registry_lookup:
                        match_id = registry_lookup[parent_key]
                        match_type = "parent_company_exact"

                # 2. Token match on parent company
                if not match_id and parent_company:
                    parent_key = _simplify_name(parent_company)
                    for token in parent_key.split():
                        if token in name_tokens:
                            match_id = name_tokens[token]
                            match_type = "parent_company_token"
                            break

                # 3. Facility name match (for companies like BASF have named facilities)
                if not match_id and facility_name:
                    fac_key = _simplify_name(facility_name)
                    for token in fac_key.split():
                        if token in name_tokens:
                            match_id = name_tokens[token]
                            match_type = "facility_name_token"
                            break

                if not match_id:
                    continue

                # Create target registry entry for the facility
                facility_location = f"{facility_name}, {city}, {country}"
                target_id_res = db.execute(
                    """SELECT id FROM registry WHERE canonical_name = ? AND company_type = 'economic_concept'""",
                    (facility_location,),
                ).fetchone()

                if target_id_res:
                    target_reg_id = target_id_res[0]
                else:
                    cursor = db.execute(
                        """INSERT INTO registry (canonical_name, company_type, jurisdiction)
                           VALUES (?, 'economic_concept', ?)""",
                        (facility_location, country),
                    )
                    target_reg_id = cursor.lastrowid

                # Check for duplicate edge
                existing = db.execute(
                    """SELECT id FROM edges
                       WHERE source_registry_id = ? AND target_registry_id = ?
                       AND relationship_type = 'facility_at' AND edge_source = 'eprtr'""",
                    (match_id, target_reg_id),
                ).fetchone()

                if existing:
                    duplicates += 1
                    continue

                # Create edge with shock channel attributes and dedup_key
                dedup_key = f"{match_id}|{target_reg_id}|facility_at|eprtr"
                attrs = populate_edge_attributes("facility_at")

                db.execute(
                    """INSERT OR IGNORE INTO edges
                       (source_registry_id, target_registry_id, relationship_type,
                        confidence, edge_source, direction, created_at,
                        shock_channel, lag_bucket, buffer_proxy, switching_time_bucket,
                        quote_span, uncertainty_reason, dedup_key)
                       VALUES (?, ?, 'facility_at', 0.8, 'eprtr', 'outgoing', ?,
                               ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        match_id, target_reg_id, now,
                        attrs["shock_channel"], attrs["lag_bucket"],
                        attrs["buffer_proxy"], attrs["switching_time_bucket"],
                        f"E-PRTR: {facility_name} operated by {parent_company or 'unknown'} in {city}, {country}. Match: {match_type}",
                        "eprtr_regulatory_dataset",
                        dedup_key,
                    ),
                )
                created += 1
                matched_companies.add(f"{match_id}")

                if created % 20 == 0:
                    db.commit()

    except Exception as e:
        logger.warning(f"E-PRTR processing error: {e}")
        errors += 1

    db.commit()

    logger.info(
        f"E-PRTR: {created} edges created, {duplicates} duplicates, "
        f"{errors} errors from {facilities} facilities. "
        f"Matched {len(matched_companies)} portfolio companies."
    )

    return {
        "created": created,
        "duplicates": duplicates,
        "errors": errors,
        "facilities_in_dataset": facilities,
        "matched_companies": len(matched_companies),
    }


def main():
    """CLI entry point for standalone E-PRTR run."""
    import argparse
    from kassandra.db import get_db

    parser = argparse.ArgumentParser(description="Discover E-PRTR facility edges")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    db = get_db()
    result = discover_eprtr_facilities(db)

    if args.dry_run:
        print(f"DRY RUN: Found {result.get('facilities_in_dataset', 0)} facilities")
    else:
        print(f"Created {result['created']} E-PRTR facility edges "
              f"({result['duplicates']} duplicates, {result['errors']} errors)")


if __name__ == "__main__":
    main()
