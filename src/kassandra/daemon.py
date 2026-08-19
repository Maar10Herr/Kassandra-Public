"""Near-real-time Kassandra alert daemon.

The daily scheduled soak remains the slow, analyst-style digest. This module provides a
separate fast path: poll cheap event sources, rescore, and emit compact alerts only
when newly observed adverse events cross the alert policy.

Design principles:
- no LLM calls at runtime;
- use evidence/event deduplication already in the DB;
- coordinate with the daily soak cycle through the shared cycle lock;
- stay quiet when there is nothing new, so a scheduler can suppress delivery.
"""

from __future__ import annotations

import json
import logging
import signal
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from kassandra.collector import run_collection
from kassandra.db import get_db, migrate
from kassandra.scoring import compute_scores
from kassandra.soak import _acquire_cycle_lock, _release_cycle_lock

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DAEMON_STATE_PATH = PROJECT_ROOT / "state" / "daemon.json"
DEFAULT_FAST_SOURCES = ["companies_house", "uk_gazette", "bodacc_fr", "borme_es"]
DEFAULT_ALERT_SEVERITIES = ["critical", "high"]


@dataclass(frozen=True)
class AlertPolicy:
    """Policy for real-time adverse-event alerts."""

    severities: tuple[str, ...] = ("critical", "high")
    min_confidence: float = 0.5
    include_unconfirmed: bool = False


def load_daemon_state(path: Path = DAEMON_STATE_PATH) -> dict[str, Any]:
    """Load daemon state, initialising a clean watermark if absent."""
    if path.exists():
        state = json.loads(path.read_text())
        if "alerts_generated" not in state:
            state["alerts_generated"] = int(state.pop("alerts_sent", 0))
        else:
            state.pop("alerts_sent", None)
        return state
    now = datetime.now(timezone.utc).isoformat()
    return {
        "started_at": now,
        "last_cycle_at": None,
        "cycles_completed": 0,
        "cycles_failed": 0,
        "last_alerted_event_id": 0,
        "last_alerted_at": None,
        "alerts_generated": 0,
        "last_failure": None,
    }


def save_daemon_state(state: dict[str, Any], path: Path = DAEMON_STATE_PATH) -> None:
    """Persist daemon state atomically enough for a single local writer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, default=str))


def _max_event_id(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
    return int(row[0] or 0)


def detect_new_adverse_events(
    db: sqlite3.Connection,
    since_event_id: int,
    policy: AlertPolicy | None = None,
) -> list[dict[str, Any]]:
    """Return newly inserted adverse events that should page a human.

    Uses an event-id watermark rather than timestamps because some sources have
    historical publication dates while `events.id` is a monotonic local insert
    order. This avoids replaying old adverse events after daemon restarts.
    """
    policy = policy or AlertPolicy()
    severity_placeholders = ",".join("?" for _ in policy.severities)
    params: list[Any] = [since_event_id, *policy.severities, policy.min_confidence]
    unconfirmed_clause = ""
    if not policy.include_unconfirmed:
        unconfirmed_clause = "AND e.event_type != 'unconfirmed_adverse'"

    rows = db.execute(
        f"""
        SELECT e.id AS event_id,
               e.event_type,
               e.event_subtype,
               e.severity,
               e.confidence,
               e.description,
               e.extracted_at,
               e.raw_event_json,
               r.id AS registry_id,
               r.canonical_name,
               ev.id AS evidence_id,
               ev.source_url,
               ev.retrieval_time,
               ev.publication_time,
               ev.extraction_method,
               ev.content_hash,
               ev.parser_version
        FROM events e
        JOIN registry r ON e.registry_id = r.id
        JOIN evidence ev ON e.evidence_id = ev.id
        WHERE e.id > ?
          AND COALESCE(e.active, 1) = 1
          AND COALESCE(e.status, 'active') = 'active'
          AND e.severity IN ({severity_placeholders})
          AND e.confidence >= ?
          {unconfirmed_clause}
        ORDER BY e.id ASC
        """,
        params,
    ).fetchall()
    return [_row_to_alert_dict(row) for row in rows]


def _row_to_alert_dict(row: sqlite3.Row) -> dict[str, Any]:
    raw = row["raw_event_json"]
    parsed: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}
    return {
        "event_id": row["event_id"],
        "company": row["canonical_name"],
        "registry_id": row["registry_id"],
        "event_type": row["event_type"],
        "event_subtype": row["event_subtype"],
        "severity": row["severity"],
        "confidence": row["confidence"],
        "description": row["description"] or parsed.get("matched_text", ""),
        "pattern_id": parsed.get("pattern_id"),
        "matched_pattern": parsed.get("matched_pattern"),
        "classifier_version": parsed.get("classifier_version"),
        "extracted_at": row["extracted_at"],
        "evidence_id": row["evidence_id"],
        "source_url": row["source_url"],
        "retrieval_time": row["retrieval_time"],
        "publication_time": row["publication_time"],
        "source": row["extraction_method"],
        "content_hash": row["content_hash"],
        "parser_version": row["parser_version"],
    }


def format_alerts(alerts: Iterable[dict[str, Any]], cycle_summary: dict[str, Any] | None = None) -> str:
    """Format alerts as a transport-neutral Markdown message."""
    alerts = list(alerts)
    if not alerts:
        return ""

    lines = [
        "## 🚨 Kassandra real-time adverse-event alert",
        f"*{len(alerts)} new high-severity event(s) detected*",
        "",
    ]
    for alert in alerts[:20]:
        desc = _shorten(alert.get("description"), 220)
        pattern = alert.get("pattern_id") or "n/a"
        pub_time = alert.get("publication_time") or alert.get("extracted_at") or "?"
        lines.extend([
            f"### {alert['company']} — {alert['event_type']} ({alert['severity']})",
            f"- Confidence: {float(alert.get('confidence') or 0):.2f} | Pattern: `{pattern}` | Event ID: `{alert['event_id']}`",
            f"- Date: {str(pub_time)[:10]} | Source: {alert.get('source') or 'unknown'} | Evidence: `{alert.get('evidence_id')}`",
        ])
        if desc:
            lines.append(f"- Why: “{desc}”")
        if alert.get("source_url"):
            lines.append(f"- URL: {alert['source_url']}")
        lines.append("- Action: open evidence, verify entity match/materiality, then decide whether to escalate to the daily watchlist note.")
        lines.append("")

    if len(alerts) > 20:
        lines.append(f"*{len(alerts) - 20} additional alerts suppressed from this message.*")
        lines.append("")

    if cycle_summary:
        lines.extend([
            "---",
            f"*Fast cycle: sources={cycle_summary.get('sources')} | new evidence={cycle_summary.get('new_evidence')} | scored={cycle_summary.get('scores_computed')}*",
        ])
    return "\n".join(lines).strip()


def run_alert_cycle(
    sources: list[str] | None = None,
    state_path: Path = DAEMON_STATE_PATH,
    policy: AlertPolicy | None = None,
    collect: bool = True,
) -> dict[str, Any]:
    """Run one fast polling cycle and return alert payload + operational stats."""
    sources = sources or DEFAULT_FAST_SOURCES
    state = load_daemon_state(state_path)
    cycle_started_at = datetime.now(timezone.utc).isoformat()

    if not _acquire_cycle_lock():
        return {
            "status": "skipped_locked",
            "alerts": [],
            "message": "",
            "sources": sources,
            "new_evidence": {},
        }

    db = get_db()
    try:
        migrate(db)
        # Bootstrap: never replay historical events on first run, but do alert on
        # new events inserted by this cycle.
        watermark = int(state.get("last_alerted_event_id") or 0)
        if watermark <= 0:
            watermark = _max_event_id(db)

        collection_result: dict[str, Any] = {}
        if collect:
            raw_result = run_collection(db, sources=sources)
            # Canonical collectors return CollectionMetrics; tolerate legacy
            # integer adapters in tests/older integrations during migration.
            collection_result = {}
            for key, value in raw_result.items():
                if hasattr(value, "to_dict"):
                    collection_result[key] = value.to_dict()
                else:
                    legacy_value: Any = value
                    collection_result[key] = {"new_evidence": int(legacy_value)}

        # Alerting is event-watermark based. Compute current scores for cycle
        # context without persisting a ~1 MiB provenance manifest every 15 minutes;
        # the daily soak owns immutable analytical score snapshots.
        scores = compute_scores(db, persist=False)
        alerts = detect_new_adverse_events(db, watermark, policy=policy)
        max_seen_event_id = _max_event_id(db)
        max_alerted_event_id = max([a["event_id"] for a in alerts], default=watermark)

        state["cycles_completed"] = int(state.get("cycles_completed", 0)) + 1
        state["last_cycle_at"] = cycle_started_at
        state["last_sources"] = sources
        state["last_collection_result"] = collection_result
        state["last_alerted_event_id"] = max(max_alerted_event_id, watermark)
        state["last_seen_event_id"] = max_seen_event_id
        state["last_failure"] = None
        if alerts:
            state["alerts_generated"] = int(state.get("alerts_generated", 0)) + len(alerts)
            state["last_alerted_at"] = datetime.now(timezone.utc).isoformat()
        save_daemon_state(state, state_path)

        cycle_summary = {
            "sources": ",".join(sources),
            "new_evidence": sum(v.get("new_evidence", 0) for v in collection_result.values()),
            "scores_computed": len(scores),
        }
        return {
            "status": "ok",
            "alerts": alerts,
            "message": format_alerts(alerts, cycle_summary),
            "sources": sources,
            "new_evidence": collection_result,
            "scores_computed": len(scores),
            "watermark_before": watermark,
            "watermark_after": state["last_alerted_event_id"],
        }
    except Exception as exc:
        logger.exception("Kassandra alert cycle failed")
        state["cycles_failed"] = int(state.get("cycles_failed", 0)) + 1
        state["last_failure"] = {"at": cycle_started_at, "error": str(exc)}
        save_daemon_state(state, state_path)
        raise
    finally:
        db.close()
        _release_cycle_lock()


def run_daemon(
    interval_seconds: int = 300,
    sources: list[str] | None = None,
    state_path: Path = DAEMON_STATE_PATH,
    policy: AlertPolicy | None = None,
) -> None:
    """Run continuous polling. Prints alert messages to stdout when they occur."""
    stop = False

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stop:
        result = run_alert_cycle(sources=sources, state_path=state_path, policy=policy)
        if result.get("message"):
            print(result["message"], flush=True)
        if stop:
            break
        time.sleep(interval_seconds)


def _shorten(text: str | None, limit: int) -> str:
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "…"
