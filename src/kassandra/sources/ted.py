"""TED (Tenders Electronic Daily) — EU public procurement data source.

P1 (engineering audit 2026-06-20): Diversify economic dependency sources beyond annual
reports. TED provides EU public procurement notices — when Company A wins a
contract from Company B, it creates a supplier_to edge (A supplies to B via
a public contract), or customer_of (B is a customer of A).

Source: TED REST API (https://ted.europa.eu/api/v2.0/)
Format: JSON, free, updated daily
Confidence: 0.7 (official EU procurement notices)

Each notice creates edges where:
- The awarding authority = customer
- The winning contractor = supplier
- Relationship: supplier_to (contractor → authority) or customer_of

The 13 existing TED edges in the DB were created during pilot enrichment
and serve as the baseline.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from kassandra.shock_channel import populate_edge_attributes

logger = logging.getLogger(__name__)

# TED REST API endpoint (v2)
TED_API_BASE = "https://ted.europa.eu/api/v2.0"
TED_SEARCH_URL = f"{TED_API_BASE}/notices/search"

# Cache for last run timestamp
CACHE_DIR = Path.home() / ".cache" / "kassandra" / "ted"


def _ted_api_request(endpoint: str, params: dict | None = None) -> dict | None:
    """Make a request to the TED REST API.

    Returns parsed JSON or None on failure.
    """
    import urllib.parse

    url = endpoint
    if params:
        # Filter out None values
        clean_params = {k: v for k, v in params.items() if v is not None}
        url = f"{endpoint}?{urllib.parse.urlencode(clean_params)}"

    try:
        req = Request(
            url,
            headers={
                "User-Agent": "Kassandra/1.0 (economic-dependency-monitor; "
                              "https://github.com/Maar10Herr/Kassandra-Public)",
                "Accept": "application/json",
            },
        )
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.warning(f"TED API request failed: {e}")
        return None


def search_ted_notices(
    query: str,
    max_results: int = 100,
    notice_type: str | None = None,
) -> list[dict[str, Any]]:
    """Search TED for procurement notices matching a company name.

    Args:
        query: Company name to search for
        max_results: Max notices to return
        notice_type: Filter by notice type (e.g., 'CN' = contract notice,
                     'CA' = contract award)

    Returns:
        List of notice dicts with: id, title, buyer_name, winner_name,
        contract_value, publication_date, country, etc.
    """
    params = {
        "q": query,
        "pageSize": min(max_results, 100),
        "pageNum": 1,
        "scope": "ACTIVE",
        "reverseOrder": "false",
    }
    if notice_type:
        params["noticeType"] = notice_type

    result = _ted_api_request(TED_SEARCH_URL, params)
    if not result:
        return []

    notices = result.get("notices", []) or result.get("results", [])
    if not notices and isinstance(result, list):
        notices = result

    return notices[:max_results]


def _extract_contract_parties(
    notice: dict,
) -> list[dict[str, str]]:
    """Extract buyer → winner relationships from a TED notice.

    Returns list of {buyer_name, winner_name, contract_title, value_eur}.
    """
    results = []

    # Try the structured notice format
    title = notice.get("title", "") or notice.get("contractTitle", "")

    # Extract buyers (awarding authorities)
    buyers = []
    buyer_section = notice.get("buyer", {}) or notice.get("contractingAuthority", {})
    if isinstance(buyer_section, dict):
        buyer_name = buyer_section.get("officialName", "") or buyer_section.get("name", "")
        if buyer_name:
            buyers.append(buyer_name)
    elif isinstance(buyer_section, list):
        for b in buyer_section:
            name = b.get("officialName", "") or b.get("name", "")
            if name:
                buyers.append(name)

    # Extract winners
    winners = []
    award_section = (
        notice.get("award", {})
        or notice.get("lot", {})
        or notice.get("awardedContract", {})
    )
    if isinstance(award_section, dict):
        winner = award_section.get("winner", {}) or award_section.get("contractor", {})
        if isinstance(winner, dict):
            winner_name = winner.get("officialName", "") or winner.get("name", "")
            if winner_name:
                winners.append(winner_name)
        elif isinstance(winner, str):
            winners.append(winner)
    elif isinstance(award_section, list):
        for aw in award_section:
            winner = aw.get("winner", {}) or aw.get("contractor", {})
            if isinstance(winner, dict):
                name = winner.get("officialName", "") or winner.get("name", "")
                if name:
                    winners.append(name)

    # Extract value
    value_eur = None
    value_section = (
        award_section.get("value", {})
        if isinstance(award_section, dict)
        else None
    )
    if value_section:
        value_eur = value_section.get("valueEur", None) or value_section.get("total", None)

    # Try flat fields
    if not buyers:
        buyer = notice.get("buyerName", "") or notice.get("contractingAuthorityName", "")
        if buyer:
            buyers.append(buyer)
    if not winners:
        winner = notice.get("winnerName", "") or notice.get("contractorName", "")
        if winner:
            winners.append(winner)
    if not value_eur:
        value_eur = notice.get("contractValueEur") or notice.get("estimatedValueEur")

    for buyer in buyers:
        for winner in winners:
            results.append({
                "buyer_name": buyer,
                "winner_name": winner,
                "contract_title": title[:200] if title else "Unknown",
                "value_eur": value_eur,
            })

    return results


def discover_ted_edges(
    db: sqlite3.Connection,
    company_names: list[str] | None = None,
) -> dict[str, int]:
    """Discover TED procurement edges for portfolio companies.

    For each company, searches TED for procurement notices where the company
    appears as buyer or winner. Creates supplier_to edges.

    Returns dict: {created, duplicates, errors, companies_checked, notices_found}
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Load portfolio companies
    if company_names is None:
        rows = db.execute(
            """SELECT id, canonical_name FROM registry
               WHERE company_type != 'economic_concept'
               AND status = 'active'"""
        ).fetchall()
        company_names = [(r["id"], r["canonical_name"]) for r in rows]
    elif isinstance(company_names, list) and company_names and isinstance(company_names[0], str):
        # Convert to (id, name) tuples
        lookup = {}
        rows = db.execute(
            """SELECT id, canonical_name FROM registry
               WHERE company_type != 'economic_concept'"""
        ).fetchall()
        name_to_id = {r["canonical_name"]: r["id"] for r in rows}
        company_names = [
            (name_to_id.get(n), n)
            for n in company_names
            if n in name_to_id
        ]

    now = datetime.now(timezone.utc).isoformat()
    created = 0
    duplicates = 0
    errors = 0
    notices_found = 0
    companies_checked = 0

    for registry_id, name in company_names:
        companies_checked += 1
        try:
            notices = search_ted_notices(name, max_results=20)
            notices_found += len(notices)

            for notice in notices:
                parties = _extract_contract_parties(notice)
                for party in parties:
                    # Create a supplier_to edge: winner → buyer
                    # The winner (contractor) supplies to the buyer (authority)

                    # Find or create buyer registry entry
                    buyer_res = db.execute(
                        """SELECT id FROM registry
                           WHERE canonical_name = ? AND company_type = 'economic_concept'""",
                        (party["buyer_name"],),
                    ).fetchone()
                    if buyer_res:
                        buyer_reg_id = buyer_res[0]
                    else:
                        c = db.execute(
                            """INSERT INTO registry (canonical_name, company_type)
                               VALUES (?, 'economic_concept')""",
                            (party["buyer_name"],),
                        )
                        buyer_reg_id = c.lastrowid

                    # Check duplicate
                    existing = db.execute(
                        """SELECT id FROM edges
                           WHERE source_registry_id = ? AND target_registry_id = ?
                           AND relationship_type = 'supplier_to'
                           AND edge_source = 'ted'""",
                        (registry_id, buyer_reg_id),
                    ).fetchone()
                    if existing:
                        duplicates += 1
                        continue

                    # Value-based materiality
                    materiality = None
                    if party["value_eur"]:
                        try:
                            materiality = float(party["value_eur"]) / 1_000_000
                        except (ValueError, TypeError):
                            pass

                    # Create edge with dedup_key for duplicate prevention
                    dedup_key = f"{registry_id}|{buyer_reg_id}|supplier_to|ted"
                    attrs = populate_edge_attributes("supplier_to")
                    db.execute(
                        """INSERT OR IGNORE INTO edges
                           (source_registry_id, target_registry_id, relationship_type,
                            confidence, economic_materiality, edge_source,
                            direction, created_at,
                            shock_channel, lag_bucket, buffer_proxy,
                            switching_time_bucket,
                            quote_span, uncertainty_reason, dedup_key)
                           VALUES (?, ?, 'supplier_to', 0.7, ?, 'ted',
                                   'outgoing', ?,
                                   ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            registry_id, buyer_reg_id,
                            materiality, now,
                            attrs["shock_channel"], attrs["lag_bucket"],
                            attrs["buffer_proxy"],
                            attrs["switching_time_bucket"],
                            f"TED: {party['contract_title']} — "
                            f"value={party['value_eur']} EUR",
                            "ted_official_procurement_notice",
                            dedup_key,
                        ),
                    )
                    created += 1

            if created % 10 == 0:
                db.commit()

        except Exception as e:
            logger.debug(f"TED search failed for {name}: {e}")
            errors += 1

    db.commit()

    logger.info(
        f"TED: {created} edges, {duplicates} duplicates, {errors} errors "
        f"from {notices_found} notices for {companies_checked} companies"
    )

    return {
        "created": created,
        "duplicates": duplicates,
        "errors": errors,
        "companies_checked": companies_checked,
        "notices_found": notices_found,
    }


def main():
    """CLI entry point."""
    import argparse
    from kassandra.db import get_db

    parser = argparse.ArgumentParser(description="Discover TED procurement edges")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--company", help="Search specific company name")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    db = get_db()

    companies = [args.company] if args.company else None
    result = discover_ted_edges(db, company_names=companies)

    if args.dry_run:
        print("DRY RUN")
    else:
        print(f"Created {result['created']} TED edges "
              f"({result['duplicates']} dupes, {result['errors']} errors)")
        print(f"Checked {result['companies_checked']} companies, "
              f"found {result['notices_found']} notices")


if __name__ == "__main__":
    main()
