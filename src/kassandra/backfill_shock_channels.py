"""Backfill shock channel attributes for all existing edges.

P1 (engineering audit 2026-06-20): Populates shock_channel, lag_bucket, buffer_proxy,
replaceability, replaceability_unknown_reason, and switching_time_bucket
for every edge using deterministic mapping from relationship_type.

Run: python -m kassandra.backfill_shock_channels
"""

import logging
import sqlite3
from kassandra.shock_channel import populate_edge_attributes

logger = logging.getLogger(__name__)


def backfill(db: sqlite3.Connection, dry_run: bool = False) -> dict:
    """Backfill shock channel attributes for all edges where shock_channel IS NULL.

    Returns dict with stats: total, updated, already_populated, errors.
    """
    edges = db.execute(
        """SELECT id, relationship_type, concentration, replaceability
           FROM edges
           WHERE shock_channel IS NULL"""
    ).fetchall()

    if not edges:
        logger.info("No edges need backfill — all have shock_channel populated")
        return {"total": 0, "updated": 0, "already_populated": 0, "errors": 0}

    updated = 0
    errors = 0

    for edge in edges:
        try:
            attrs = populate_edge_attributes(
                relationship_type=edge["relationship_type"],
                concentration=edge["concentration"],
                replaceability_val=edge["replaceability"],
            )

            if not dry_run:
                db.execute(
                    """UPDATE edges SET
                       shock_channel = ?,
                       shock_channel_unknown_reason = ?,
                       lag_bucket = ?,
                       buffer_proxy = ?,
                       replaceability = COALESCE(replaceability,
                           CASE WHEN ? != 'unknown' AND ? IS NULL THEN
                               CASE ? WHEN 'high' THEN 0.8
                                      WHEN 'medium' THEN 0.5
                                      WHEN 'low' THEN 0.2
                                      ELSE NULL END
                           ELSE replaceability END),
                       replaceability_unknown_reason = ?,
                       switching_time_bucket = ?
                       WHERE id = ?""",
                    (
                        attrs["shock_channel"],
                        attrs["shock_channel_unknown_reason"],
                        attrs["lag_bucket"],
                        attrs["buffer_proxy"],
                        attrs["replaceability"],
                        attrs["replaceability_unknown_reason"],
                        attrs["replaceability"],
                        attrs["replaceability_unknown_reason"],
                        attrs["switching_time_bucket"],
                        edge["id"],
                    ),
                )
            updated += 1
        except Exception as e:
            logger.warning(f"Failed to backfill edge {edge['id']}: {e}")
            errors += 1

    if not dry_run:
        db.commit()

    logger.info(
        f"Shock channel backfill: {updated} updated, {errors} errors "
        f"(out of {len(edges)} edges needing backfill)"
    )

    return {
        "total": len(edges),
        "updated": updated,
        "already_populated": 0,
        "errors": errors,
    }


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Backfill shock channel attributes")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change")
    parser.add_argument("--db", default="data/state.db", help="Database path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")

    result = backfill(db, dry_run=args.dry_run)

    if args.dry_run:
        print(f"DRY RUN: Would update {result['total']} edges")
    else:
        print(f"Backfilled {result['updated']} edges ({result['errors']} errors)")

    # Verify
    total = db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    populated = db.execute(
        "SELECT COUNT(*) FROM edges WHERE shock_channel IS NOT NULL"
    ).fetchone()[0]
    print(f"Populated: {populated}/{total} edges")

    # Show distribution
    dist = db.execute(
        """SELECT shock_channel, COUNT(*) as c FROM edges
           WHERE shock_channel IS NOT NULL
           GROUP BY shock_channel
           ORDER BY c DESC"""
    ).fetchall()
    print("\nShock channel distribution:")
    for row in dist:
        print(f"  {row['shock_channel']}: {row['c']}")

    db.close()


if __name__ == "__main__":
    main()
