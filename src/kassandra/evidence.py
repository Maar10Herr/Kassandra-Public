"""Immutable content-addressed evidence storage.

Every piece of evidence is stored on disk with a content-hash filename,
and metadata is recorded in SQLite. Content is never modified after storage.
"""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kassandra.config import get_config
from kassandra.contracts import EvidenceResult, EventResult

logger = logging.getLogger(__name__)


def store_evidence(
    db: sqlite3.Connection,
    content: str | bytes,
    source_url: str,
    retrieval_time: str,
    publication_time: str | None = None,
    publication_time_confidence: str | None = None,
    extraction_method: str = "direct",
    parser_version: str = "1.0.0",
    content_type: str | None = None,
    excerpt: str | None = None,
    source_reliability: float | None = None,
    raw_headers: str | None = None,
) -> EvidenceResult:
    """Store evidence immutably. Returns EvidenceResult with is_new distinction.

    Returns:
        EvidenceResult(evidence_id, is_new, content_hash)
        - is_new=True: first-time insertion
        - is_new=False: content hash already existed (dedup)
    """
    config = get_config()

    # Compute content hash
    if isinstance(content, str):
        content_bytes = content.encode("utf-8")
    else:
        content_bytes = content

    algo = config.content_hash_algorithm
    content_hash = hashlib.new(algo, content_bytes).hexdigest()

    # Check if already stored
    existing = db.execute(
        "SELECT id FROM evidence WHERE content_hash = ?",
        (content_hash,),
    ).fetchone()
    if existing:
        logger.debug(f"Evidence already stored: {content_hash[:12]}")
        return EvidenceResult(
            evidence_id=existing["id"],
            is_new=False,
            content_hash=content_hash,
        )

    # Store content on disk
    evidence_dir = config.evidence_dir / "objects"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    file_path = evidence_dir / content_hash

    # Don't overwrite existing files
    if not file_path.exists():
        file_path.write_bytes(content_bytes)

    # Record metadata
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        """INSERT INTO evidence
           (content_hash, source_url, retrieval_time, publication_time,
            publication_time_confidence, first_seen_time, extraction_method,
            parser_version, content_type, content_length, excerpt,
            source_reliability, raw_headers, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            content_hash,
            source_url,
            retrieval_time,
            publication_time,
            publication_time_confidence,
            now,
            extraction_method,
            parser_version,
            content_type,
            len(content_bytes),
            excerpt[:2000] if excerpt else None,
            source_reliability,
            raw_headers[:4000] if raw_headers else None,
            now,
        ),
    )

    db.commit()
    evidence_id = cursor.lastrowid
    logger.info(f"Stored evidence {content_hash[:12]} from {source_url[:80]}")
    return EvidenceResult(
        evidence_id=evidence_id,
        is_new=True,
        content_hash=content_hash,
    )


def get_evidence_path(db: sqlite3.Connection, evidence_id: int) -> Path | None:
    """Get the on-disk path for a stored evidence item."""
    row = db.execute(
        "SELECT content_hash FROM evidence WHERE id = ?",
        (evidence_id,),
    ).fetchone()
    if not row:
        return None
    return get_config().evidence_dir / "objects" / row["content_hash"]


def store_event(
    db: sqlite3.Connection,
    evidence_id: int,
    registry_id: int,
    event_type: str,
    event_subtype: str | None = None,
    severity: str | None = None,
    confidence: float = 1.0,
    description: str | None = None,
    source_claims_directly: bool = True,
    raw_event_json: str | None = None,
) -> EventResult:
    """Store a material event linked to evidence and a registry entity.

    Returns EventResult with explicit status:
    - "inserted": new event row created
    - "duplicate": event already exists (same evidence_id + event_type + registry_id)
    - "rejected": event was rejected (empty event_type, etc.) — no row written

    Dedup detection is explicit (SELECT-then-INSERT) rather than relying
    on INSERT OR IGNORE lastrowid behavior.
    """
    # Reject empty event_type
    if not event_type:
        return EventResult(
            event_id=None,
            status="rejected",
            registry_id=registry_id,
            event_type=event_type,
            reject_reason="empty_event_type",
        )

    try:
        # Check for existing duplicate
        existing = db.execute(
            """SELECT id FROM events
               WHERE evidence_id = ? AND event_type = ? AND registry_id = ?""",
            (evidence_id, event_type, registry_id),
        ).fetchone()

        if existing:
            logger.debug(
                f"Duplicate event: evidence={evidence_id} type={event_type} "
                f"registry={registry_id} -> existing id={existing['id']}"
            )
            return EventResult(
                event_id=existing["id"],
                status="duplicate",
                registry_id=registry_id,
                event_type=event_type,
            )

        # Insert new event
        cursor = db.execute(
            """INSERT INTO events
               (evidence_id, registry_id, event_type, event_subtype, severity,
                confidence, description, source_claims_directly, raw_event_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                evidence_id,
                registry_id,
                event_type,
                event_subtype,
                severity,
                confidence,
                description,
                int(source_claims_directly),
                raw_event_json,
            ),
        )
        db.commit()
        event_id = cursor.lastrowid
        if event_id is None:
            raise RuntimeError("INSERT succeeded but lastrowid is None")
        logger.debug(
            f"Stored event id={event_id}: type={event_type} "
            f"registry={registry_id} evidence={evidence_id}"
        )
        return EventResult(
            event_id=event_id,
            status="inserted",
            registry_id=registry_id,
            event_type=event_type,
        )
    except Exception as e:
        logger.error(f"Failed to store event: {e}")
        return EventResult(
            event_id=None,
            status="rejected",
            registry_id=registry_id,
            event_type=event_type,
            reject_reason=str(e),
        )
