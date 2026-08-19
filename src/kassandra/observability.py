"""Observability layer — source health, journal, run provenance.

Every collection run produces:
- A run_id (timestamp-based, unique)
- Journal entries for state changes, errors, milestones
- Source health updates (success/failure, item counts, staleness)
- Run summary with input fingerprints

Per kassandra_goals.md and engineering audit: observability is P1.
"""

import hashlib
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def start_run(db: sqlite3.Connection, run_type: str = "collection") -> str:
    """Begin an observability run. Returns run_id."""
    now = datetime.now(timezone.utc).isoformat()
    run_id = f"{run_type}-{now.replace(':', '-')}"

    db.execute(
        """INSERT INTO journal (timestamp, action, details)
           VALUES (?, ?, ?)""",
        (now, "run_start", json.dumps({"run_id": run_id, "run_type": run_type})),
    )

    # Register/update job_state
    db.execute(
        """INSERT INTO job_state (job_name, status, last_run_at, payload_json)
           VALUES (?, 'running', ?, ?)
           ON CONFLICT(job_name) DO UPDATE SET
           status = 'running', last_run_at = excluded.last_run_at,
           payload_json = excluded.payload_json""",
        (run_type, now, json.dumps({"run_id": run_id})),
    )
    db.commit()

    logger.info(f"Run started: {run_id}")
    return run_id


def finish_run(
    db: sqlite3.Connection,
    run_id: str,
    run_type: str = "collection",
    summary: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Mark a run as completed or failed."""
    now = datetime.now(timezone.utc).isoformat()
    status = "failed" if error else "completed"

    db.execute(
        """INSERT INTO journal (timestamp, action, details)
           VALUES (?, ?, ?)""",
        (now, "run_finish", json.dumps({
            "run_id": run_id, "status": status,
            "summary": summary, "error": error,
        })),
    )

    db.execute(
        """UPDATE job_state SET status = ?, last_run_at = ?, last_error = ?
           WHERE job_name = ?""",
        (status, now, error, run_type),
    )
    db.commit()

    if error:
        logger.error(f"Run {run_id} FAILED: {error}")
    else:
        logger.info(f"Run {run_id} completed")


def log_source_event(
    db: sqlite3.Connection,
    source_name: str,
    source_type: str,
    base_url: str | None,
    event: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Log a source-level event (success, failure, evidence collected, etc.)."""
    now = datetime.now(timezone.utc).isoformat()

    # Ensure source is registered
    db.execute(
        """INSERT OR IGNORE INTO sources
           (source_name, source_type, base_url, status)
           VALUES (?, ?, ?, 'active')""",
        (source_name, source_type, base_url),
    )

    # Update source health
    if event == "success":
        db.execute(
            """UPDATE sources SET
               last_success_at = ?, consecutive_failures = 0,
               total_requests = total_requests + 1,
               status = 'active'
               WHERE source_name = ?""",
            (now, source_name),
        )
    elif event == "failure":
        db.execute(
            """UPDATE sources SET
               last_failure_at = ?, consecutive_failures = consecutive_failures + 1,
               total_requests = total_requests + 1
               WHERE source_name = ?""",
            (now, source_name),
        )
        # Mark as degraded after 3 consecutive failures
        db.execute(
            """UPDATE sources SET status = 'degraded'
               WHERE source_name = ? AND consecutive_failures >= 3""",
            (source_name,),
        )
        # Mark as disabled after 10 consecutive failures
        db.execute(
            """UPDATE sources SET status = 'disabled'
               WHERE source_name = ? AND consecutive_failures >= 10""",
            (source_name,),
        )
    elif event == "evidence_collected":
        db.execute(
            """UPDATE sources SET
               total_evidence = total_evidence + ?,
               total_requests = total_requests + 1,
               last_success_at = ?,
               consecutive_failures = 0,
               status = 'active'
               WHERE source_name = ?""",
            (details.get("count", 1) if details else 1, now, source_name),
        )
    elif event == "event_detected":
        db.execute(
            """UPDATE sources SET
               unique_events = unique_events + ?,
               last_success_at = ?
               WHERE source_name = ?""",
            (details.get("count", 1) if details else 1, now, source_name),
        )

    # Journal the event
    db.execute(
        """INSERT INTO journal (timestamp, action, details)
           VALUES (?, ?, ?)""",
        (now, f"source_{event}", json.dumps({
            "source_name": source_name,
            "event": event,
            "details": details or {},
        })),
    )

    db.commit()


def journal_event(
    db: sqlite3.Connection,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Write a journal entry."""
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO journal (timestamp, action, details)
           VALUES (?, ?, ?)""",
        (now, action, json.dumps(details or {})),
    )
    db.commit()


def compute_input_fingerprint(db: sqlite3.Connection) -> str:
    """Compute a fingerprint of current input state for provenance."""
    # Hash of: registry row count + latest evidence hash + portfolio composition
    reg_count = db.execute("SELECT COUNT(*) FROM registry").fetchone()[0]
    portfolio_items = db.execute(
        "SELECT GROUP_CONCAT(isin ORDER BY isin) FROM portfolio_items"
    ).fetchone()[0] or ""
    latest_evidence = db.execute(
        "SELECT content_hash FROM evidence ORDER BY id DESC LIMIT 1"
    ).fetchone()

    fp_input = f"reg:{reg_count}|pf:{portfolio_items}|ev:{latest_evidence['content_hash'] if latest_evidence else 'none'}"
    return hashlib.sha256(fp_input.encode()).hexdigest()[:16]


def get_source_health_report(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Get a health report for all registered sources."""
    rows = db.execute("""
        SELECT source_name, source_type, status, last_success_at,
               last_failure_at, consecutive_failures, total_requests,
               total_evidence, unique_events
        FROM sources ORDER BY source_name
    """).fetchall()
    return [dict(r) for r in rows]


def get_latest_run(db: sqlite3.Connection) -> dict[str, Any] | None:
    """Get the most recent run info."""
    row = db.execute(
        "SELECT * FROM job_state WHERE last_run_at IS NOT NULL ORDER BY last_run_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def get_run_history(db: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """Get recent journal entries about runs."""
    rows = db.execute(
        """SELECT * FROM journal
           WHERE action IN ('run_start', 'run_finish', 'source_failure', 'source_success')
           ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
