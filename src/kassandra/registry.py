"""Corporate registry — canonical identity resolution.

Maps portfolio companies to canonical entities using:
- Companies House (UK/IE)
- GLEIF (LEI records for all jurisdictions)
- Domain data from portfolio
- Placeholders with explicit unresolved markers for remaining
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone

from kassandra.config import get_config
from kassandra.portfolio import get_domain_data
from kassandra.sources.companies_house import CompaniesHouseClient
from kassandra.sources.gleif import GleifClient
from kassandra.sources.entity_resolution import normalize_jurisdiction

logger = logging.getLogger(__name__)


def resolve_portfolio(db: sqlite3.Connection) -> tuple[int, int, int]:
    """Resolve all unresolved portfolio items against the registry.

    Returns (matched, lei_resolved, total_count).
    matched: companies with non-placeholder resolution
    lei_resolved: companies with LEI found via GLEIF
    """
    items = db.execute(
        """SELECT pi.id, pi.name, pi.isin, pi.country
           FROM portfolio_items pi
           JOIN portfolios p ON pi.portfolio_id = p.id
           WHERE p.name = 'Euro Stoxx 50'
           ORDER BY pi.id"""
    ).fetchall()

    ch_client = CompaniesHouseClient()
    gleif_client = GleifClient()
    domain_data = get_domain_data()
    now = datetime.now(timezone.utc).isoformat()
    matched = 0
    lei_resolved = 0

    for item in items:
        isin = item["isin"]
        domain, ir_url = domain_data.get(isin, ("", ""))
        ch_resolved = False

        # Check if already resolved by ISIN
        existing = db.execute(
            "SELECT id, lei, domain FROM registry WHERE isin = ?", (isin,)
        ).fetchone()

        if existing and existing["lei"] and existing["domain"]:
            matched += 1
            continue

        registry_id = existing["id"] if existing else None

        # Try Companies House (UK companies only; Ireland/IE is sovereign, not UK)
        if ch_client.available and item["country"] in ("GB", "UK", "JE", "GG"):
            result = ch_client.search_company(item["name"])
            if result and result.get("company_number"):
                profile = ch_client.get_company(result["company_number"])
                if profile:
                    registry_id = _upsert_registry_entry(db, profile, item, domain, ir_url, now, registry_id)
                    ch_resolved = True

        # Try GLEIF for LEI (all jurisdictions)
        lei_info = None
        gleif_record = gleif_client.search_by_isin(isin)
        if not gleif_record:
            gleif_record = gleif_client.search_by_name(item["name"], item["country"])

        if gleif_record:
            lei_info = gleif_client.extract_entity_info(gleif_record)
            if lei_info.get("lei"):
                lei_resolved += 1

        # Create or update registry entry
        registry_id = _ensure_registry(
            db, item, domain, ir_url, lei_info, now, registry_id
        )

        # Count as resolved if we have CH data, LEI, or both
        if registry_id and (ch_resolved or lei_info):
            matched += 1

    db.commit()
    logger.info(
        f"Registry: {matched} resolved, {lei_resolved} with LEI, "
        f"of {len(items)} total"
    )
    return matched, lei_resolved, len(items)


def _upsert_registry_entry(
    db: sqlite3.Connection,
    profile: dict,
    item: sqlite3.Row,
    domain: str,
    ir_url: str,
    now: str,
    existing_id: int | None = None,
) -> int | None:
    """Insert or update a Companies House profile into the registry."""
    try:
        address = profile.get("registered_office_address", {})
        address_str = ", ".join(
            filter(None, [
                address.get("address_line_1", ""),
                address.get("address_line_2", ""),
                address.get("locality", ""),
                address.get("postal_code", ""),
                address.get("country", ""),
            ])
        )

        if existing_id:
            db.execute(
                """UPDATE registry SET
                   canonical_name = ?, companies_house_number = ?,
                   jurisdiction = ?, company_type = ?, status = ?,
                   incorporation_date = ?, registered_address = ?,
                   domain = ?, ir_url = ?, raw_json = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    profile.get("company_name", item["name"]),
                    profile.get("company_number"),
                    normalize_jurisdiction(profile.get("jurisdiction", item["country"])),
                    profile.get("type"),
                    profile.get("company_status"),
                    profile.get("date_of_creation"),
                    address_str,
                    domain or None,
                    ir_url or None,
                    json.dumps(profile),
                    now,
                    existing_id,
                ),
            )
            return existing_id
        else:
            cursor = db.execute(
                """INSERT INTO registry
                   (canonical_name, companies_house_number, isin, jurisdiction,
                    company_type, status, incorporation_date, registered_address,
                    domain, ir_url, raw_json, resolved_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    profile.get("company_name", item["name"]),
                    profile.get("company_number"),
                    item["isin"],
                    normalize_jurisdiction(profile.get("jurisdiction", item["country"])),
                    profile.get("type"),
                    profile.get("company_status"),
                    profile.get("date_of_creation"),
                    address_str,
                    domain or None,
                    ir_url or None,
                    json.dumps(profile),
                    now,
                    now,
                ),
            )
            return cursor.lastrowid
    except Exception as e:
        logger.error(f"Failed to upsert registry for {item['name']}: {e}")
        return None


def _ensure_registry(
    db: sqlite3.Connection,
    item: sqlite3.Row,
    domain: str,
    ir_url: str,
    lei_info: dict | None,
    now: str,
    existing_id: int | None = None,
) -> int | None:
    """Ensure a registry entry exists with all available identifier data.

    Marks unresolved fields explicitly with NULL, not fake values.
    """
    lei = lei_info.get("lei") if lei_info else None
    legal_name = lei_info.get("legal_name") if lei_info else None
    raw_jurisdiction = lei_info.get("jurisdiction") if lei_info else item["country"]
    jurisdiction = normalize_jurisdiction(raw_jurisdiction) if raw_jurisdiction else "XX"
    status = lei_info.get("status") if lei_info else None

    try:
        if existing_id:
            db.execute(
                """UPDATE registry SET
                   canonical_name = COALESCE(?, canonical_name),
                   lei = COALESCE(?, lei),
                   jurisdiction = COALESCE(?, jurisdiction),
                   status = COALESCE(?, status),
                   domain = COALESCE(?, domain),
                   ir_url = COALESCE(?, ir_url),
                   updated_at = ?
                   WHERE id = ?""",
                (
                    legal_name or item["name"],
                    lei,
                    jurisdiction,
                    status,
                    domain or None,
                    ir_url or None,
                    now,
                    existing_id,
                ),
            )
            return existing_id
        else:
            cursor = db.execute(
                """INSERT INTO registry
                   (canonical_name, isin, lei, jurisdiction, status,
                    domain, ir_url, resolved_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    legal_name or item["name"],
                    item["isin"],
                    lei,
                    jurisdiction,
                    status,
                    domain or None,
                    ir_url or None,
                    now,
                    now,
                ),
            )
            return cursor.lastrowid
    except Exception as e:
        logger.error(f"Failed to ensure registry for {item['name']}: {e}")
        return None
