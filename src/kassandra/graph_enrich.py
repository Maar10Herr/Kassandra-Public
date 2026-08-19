"""Graph enrichment — resolve LEI placeholders to named entities.

When the graph builder discovers new entities via GLEIF relationships,
they get placeholder names (LEI:XXXXX). This module resolves those
to real legal entity names from GLEIF for analyst inspection.
"""

import logging
import time
from typing import Any

from kassandra.sources.gleif import GleifClient

logger = logging.getLogger(__name__)


def enrich_placeholders(
    db: Any, max_enrich: int = 200, batch_delay: float = 0.5
) -> int:
    """Resolve LEI placeholders to named entities from GLEIF.

    Prioritizes placeholders referenced by portfolio company edges.

    Returns count of enriched entities.
    """
    gleif = GleifClient()
    enriched = 0

    # Find placeholders that are targets of portfolio company edges
    rows = db.execute("""
        SELECT DISTINCT r.id, r.lei
        FROM registry r
        JOIN edges e ON r.id = e.target_registry_id
        JOIN registry src ON e.source_registry_id = src.id
        WHERE r.status = 'gleif_placeholder'
          AND src.domain IS NOT NULL  -- portfolio company
        ORDER BY r.id
        LIMIT ?
    """, (max_enrich,)).fetchall()

    logger.info(f"Enriching {len(rows)} LEI placeholders with names")

    for row in rows:
        lei = row["lei"]
        if not lei:
            continue

        try:
            record = gleif.get_by_lei(lei)
            if record:
                info = gleif.extract_entity_info(record)
                name = info.get("legal_name", "")
                jurisdiction = info.get("jurisdiction", "")
                status = info.get("status", "")

                if name:
                    db.execute(
                        """UPDATE registry SET
                           canonical_name = ?, jurisdiction = ?,
                           status = ?, resolved_at = datetime('now'), updated_at = datetime('now')
                           WHERE id = ?""",
                        (name, jurisdiction or None, status or None, row["id"]),
                    )
                    enriched += 1

                    if enriched % 20 == 0:
                        db.commit()
                        logger.info(f"Enriched {enriched} placeholders")
        except Exception as e:
            logger.warning(f"Enrich failed for LEI {lei}: {e}")

        # Rate limit
        if enriched > 0 and enriched % 10 == 0:
            time.sleep(batch_delay)

    db.commit()
    logger.info(f"Enriched {enriched}/{len(rows)} placeholders")
    return enriched
