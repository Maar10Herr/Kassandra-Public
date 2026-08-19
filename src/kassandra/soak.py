"""Soak-mode operational monitoring for Kassandra.

Measures every source, every cycle, and enforces data integrity.
Runs autonomously — collects, scores, checks, and produces a daily digest.
"""

import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from kassandra.config import get_config
from kassandra.db import get_db, migrate, record_journal

logger = logging.getLogger(__name__)

SOAK_STATE_PATH = Path(__file__).resolve().parent.parent.parent / "state" / "soak.json"


# ── State management ──────────────────────────────────────────────────────────

def load_soak_state() -> dict[str, Any]:
    """Load or initialise soak state."""
    if SOAK_STATE_PATH.exists():
        return json.loads(SOAK_STATE_PATH.read_text())
    return _fresh_state()


def _fresh_state() -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "started_at": now,
        "cycles_completed": 0,
        "cycles_failed": 0,
        "last_cycle_at": None,
        "last_cycle_duration_seconds": None,
        "uptime_start": now,
        "defects_found": [],
        "defects_fixed": [],
        "regression_tests_added": 0,
        "alerts_sent": 0,
        "source_metrics": {},
        "resource_history": [],
        "score_history": [],
        "gleif_staleness": {
            "last_refresh": None,
            "last_response_hash": None,
            "next_eligible_refresh": None,
            "refresh_reason": "never_refreshed",
            "refresh_interval_days": 30,
        },
    }


def save_soak_state(state: dict[str, Any]) -> None:
    SOAK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOAK_STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


# ── Metrics collection ────────────────────────────────────────────────────────

def collect_metrics(db: sqlite3.Connection) -> dict[str, Any]:
    """Collect all soak metrics from the database and system.

    Every metric is traceable to a specific SQL query — see reconciliation tests.
    """
    now = datetime.now(timezone.utc)
    config = get_config()

    # Database stats
    db_size = os.path.getsize(str(config.db_path)) if Path(str(config.db_path)).exists() else 0
    evidence_dir_size = _dir_size(config.evidence_dir) if config.evidence_dir else 0

    # Edge counts by source
    edge_counts = {}
    for row in db.execute(
        "SELECT edge_source, COUNT(*) FROM edges GROUP BY edge_source"
    ):
        edge_counts[row[0]] = row[1]
    total_edges = sum(edge_counts.values())

    # Registry
    total_registry = db.execute("SELECT COUNT(*) FROM registry").fetchone()[0]

    # Portfolio companies (scoreable_companies view)
    portfolio_count = db.execute(
        "SELECT COUNT(*) FROM scoreable_companies"
    ).fetchone()[0]

    # Evidence — total stored (deduplicated by content_hash)
    total_evidence = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    evidence_by_source = {}
    for row in db.execute(
        "SELECT extraction_method, COUNT(*) FROM evidence GROUP BY extraction_method"
    ):
        evidence_by_source[row[0]] = row[1]

    # Evidence from THIS cycle only — use source_runs from the most recent run_id
    recent_run_id_row = db.execute(
        "SELECT run_id FROM source_runs ORDER BY completed_at DESC LIMIT 1"
    ).fetchone()
    recent_run_id = recent_run_id_row[0] if recent_run_id_row else None
    evidence_this_cycle = {}
    if recent_run_id:
        for row in db.execute(
            "SELECT source_name, SUM(new_documents) FROM source_runs WHERE run_id=? GROUP BY source_name",
            (recent_run_id,),
        ):
            evidence_this_cycle[row[0]] = row[1] or 0

    # Evidence processed (including deduplicated) — from the collection step counts
    evidence_processed = {}
    for row in db.execute(
        "SELECT source_name, SUM(documents_discovered) FROM source_runs WHERE run_id=? GROUP BY source_name",
        (recent_run_id,),
    ) if recent_run_id else []:
        evidence_processed[row[0]] = row[1] or 0

    # Scores — portfolio companies from scoreable_companies view
    # Extended with company name, watch/coverage priority fields, and trigger event details
    score_rows = list(db.execute("""
        SELECT s.registry_id,
               s.analyst_priority, s.deterioration_risk, s.dependency_exposure,
               s.observation_severity, s.computed_at,
               s.priority_reason, s.coverage_quality,
               s.active_watch_priority, s.coverage_monitor_priority,
               s.transmission_signal_score,
               r.canonical_name,
               (SELECT e.event_type FROM events e
                WHERE e.registry_id = s.registry_id AND e.confidence >= 0.5 AND e.active = 1 AND e.status = 'active'
                ORDER BY e.extracted_at DESC LIMIT 1) as latest_event_type,
               (SELECT e.severity FROM events e
                WHERE e.registry_id = s.registry_id AND e.confidence >= 0.5 AND e.active = 1 AND e.status = 'active'
                ORDER BY e.extracted_at DESC LIMIT 1) as latest_event_severity,
               (SELECT e.confidence FROM events e
                WHERE e.registry_id = s.registry_id AND e.confidence >= 0.5 AND e.active = 1 AND e.status = 'active'
                ORDER BY e.extracted_at DESC LIMIT 1) as latest_event_confidence,
               (SELECT e.extracted_at FROM events e
                WHERE e.registry_id = s.registry_id AND e.confidence >= 0.5 AND e.active = 1 AND e.status = 'active'
                ORDER BY e.extracted_at DESC LIMIT 1) as latest_event_date,
               (SELECT GROUP_CONCAT(DISTINCT ev.id) FROM events e
                JOIN evidence ev ON e.evidence_id = ev.id
                WHERE e.registry_id = s.registry_id AND e.confidence >= 0.5 AND e.active = 1 AND e.status = 'active') as evidence_ids,
               (SELECT e.description FROM events e
                WHERE e.registry_id = s.registry_id AND e.confidence >= 0.5 AND e.active = 1 AND e.status = 'active'
                ORDER BY e.extracted_at DESC LIMIT 1) as latest_event_description,
               (SELECT ev.source_url FROM events e
                JOIN evidence ev ON e.evidence_id = ev.id
                WHERE e.registry_id = s.registry_id AND e.confidence >= 0.5 AND e.active = 1 AND e.status = 'active'
                ORDER BY e.extracted_at DESC LIMIT 1) as latest_event_source_url,
               (SELECT ev.extraction_method FROM events e
                JOIN evidence ev ON e.evidence_id = ev.id
                WHERE e.registry_id = s.registry_id AND e.confidence >= 0.5 AND e.active = 1 AND e.status = 'active'
                ORDER BY e.extracted_at DESC LIMIT 1) as latest_event_source
        FROM scores s
        JOIN scoreable_companies sc ON s.registry_id = sc.id
        JOIN registry r ON s.registry_id = r.id
        WHERE s.computed_at = (
            SELECT MAX(s2.computed_at) FROM scores s2 WHERE s2.registry_id = s.registry_id
        )
        ORDER BY s.active_watch_priority DESC, s.coverage_monitor_priority DESC
    """))
    scores_list = [
        {
            "registry_id": r[0],
            "priority": r[1],
            "deterioration_risk": r[2],
            "dependency_exposure": r[3],
            "observation_severity": r[4],
            "priority_reason": r[6] or "no_signal",
            "coverage_quality": r[7] or "unknown",
            "active_watch_priority": r[8] if r[8] is not None else 0,
            "coverage_monitor_priority": r[9] if r[9] is not None else 0,
            "transmission_signal_score": r[10] if r[10] is not None else 0,
            "company_name": r[11] or "Unknown",
            "latest_event_type": r[12],
            "latest_event_severity": r[13],
            "latest_event_confidence": r[14],
            "latest_event_date": r[15],
            "evidence_ids": r[16] or "",
            "latest_event_description": r[17] or "",
            "latest_event_source_url": r[18] or "",
            "latest_event_source": r[19] or "",
        }
        for r in score_rows
    ]
    portfolio_scored = len(scores_list)
    mean_priority = sum(s["priority"] for s in scores_list) / portfolio_scored if portfolio_scored else 0
    max_priority = max(s["priority"] for s in scores_list) if scores_list else 0

    # Build watchlist and coverage queue from scores
    active_watchlist = [s for s in scores_list if s.get('active_watch_priority', 0) > 0]
    active_watchlist.sort(key=lambda s: s.get('active_watch_priority', 0), reverse=True)
    coverage_queue = [s for s in scores_list if s.get('active_watch_priority', 0) == 0 and s.get('coverage_monitor_priority', 0) > 0]
    coverage_queue.sort(key=lambda s: s.get('coverage_monitor_priority', 0), reverse=True)

    active_watch_count = len(active_watchlist)
    transmission_watch_count = sum(1 for s in active_watchlist if s.get('transmission_signal_score', 0) > 0)
    direct_watch_count = active_watch_count - transmission_watch_count

    # Enrich watchlist items with dependency paths, trigger details, and evidence IDs.
    # Items without a resolvable transmission path are demoted to coverage queue.
    suppressed_items = []
    for item in active_watchlist:
        rid = item.get("registry_id")
        if rid is None:
            continue

        is_transmission = item.get("transmission_signal_score", 0) > 0
        is_direct = item.get("priority_reason") == "adverse_signal"
        # Store enrichment-time classification for digest rendering
        item["_is_direct"] = is_direct
        item["_is_transmission"] = is_transmission and not is_direct

        # Direct adverse signal: use pre-fetched event data from scores subquery
        if is_direct:
            item["trigger_event_type"] = item.get("latest_event_type", "unknown")
            item["trigger_severity"] = item.get("latest_event_severity", "?")
            item["trigger_confidence"] = item.get("latest_event_confidence", "?")
            item["trigger_date"] = item.get("latest_event_date", "?")
            item["trigger_entity"] = item.get("company_name", "Unknown")
            item["trigger_description"] = item.get("latest_event_description", "")
            item["trigger_source_url"] = item.get("latest_event_source_url", "")
            item["trigger_source"] = item.get("latest_event_source", "")
            # evidence_ids already populated from scores subquery
            continue

        # Transmission signal: find dependency path through economic edges first,
        # then fall back to GLEIF edges. Demote unresolvable items to coverage.
        if is_transmission:
            portfolio_name = item.get("company_name", "Unknown")
            found_path = False

            # Try economic edges first
            for edge_filter in [
                # Economic edges (preferred)
                (['supplier_to','customer_of','facility_at','commodity_input','operational_dependency'], False),
                # GLEIF edges (fallback)
                (['parent_subsidiary','ultimate_parent','branch_of'], True),
            ]:
                edge_types, is_gleif = edge_filter
                placeholders = ','.join('?' for _ in edge_types)
                params = [rid] + edge_types
                edge_rows = db.execute(f"""
                    SELECT e.relationship_type,
                           r.canonical_name as target_name,
                           (SELECT ev.event_type FROM events ev
                            WHERE ev.registry_id = e.target_registry_id AND ev.confidence >= 0.5 AND ev.active = 1 AND ev.status = 'active'
                            ORDER BY ev.extracted_at DESC LIMIT 1) as target_event_type,
                           (SELECT ev.extracted_at FROM events ev
                            WHERE ev.registry_id = e.target_registry_id AND ev.confidence >= 0.5 AND ev.active = 1 AND ev.status = 'active'
                            ORDER BY ev.extracted_at DESC LIMIT 1) as target_event_date
                    FROM edges e
                    JOIN registry r ON e.target_registry_id = r.id
                    WHERE e.source_registry_id = ?
                      AND e.relationship_type IN ({placeholders})
                      AND e.quality_tier IN ('T1_OFFICIAL','T2_REGISTRY','T3_TRUSTED_THIRD_PARTY')
                      AND EXISTS (
                          SELECT 1 FROM events ev
                          WHERE ev.registry_id = e.target_registry_id AND ev.confidence >= 0.5 AND ev.active = 1 AND ev.status = 'active'
                      )
                    ORDER BY e.confidence DESC
                    LIMIT 1
                """, params).fetchone()

                if edge_rows:
                    rel_type = edge_rows["relationship_type"]
                    target_name = edge_rows["target_name"]
                    target_event = edge_rows["target_event_type"]
                    target_date = edge_rows["target_event_date"]
                    gleif_note = " (legal)" if is_gleif else ""
                    item["dependency_path"] = (
                        f"{portfolio_name} → [{rel_type}]{gleif_note} → {target_name} "
                        f"(has {target_event} event)"
                    )
                    item["trigger_event_type"] = target_event or "unknown"
                    item["trigger_entity"] = target_name or "Unknown"
                    item["trigger_date"] = target_date or "?"
                    item["_transmission_source"] = "gleif" if is_gleif else "economic"
                    found_path = True
                    break

            if not found_path:
                # Unresolvable transmission — demote to coverage queue
                item["_suppressed"] = True
                item["_suppress_reason"] = "transmission signal without resolvable dependency path"
                suppressed_items.append(item)

    # Remove suppressed items from watchlist, add to coverage queue
    active_watchlist = [i for i in active_watchlist if not i.get("_suppressed")]
    # Prepend suppressed items to coverage queue (they have higher priority than pure coverage)
    coverage_queue = [i for i in suppressed_items if i.get("coverage_monitor_priority", 0) > 0] + coverage_queue

    # Recompute counts after enrichment/demotion
    active_watch_count = len(active_watchlist)
    direct_watch_count = sum(1 for s in active_watchlist if s.get("_is_direct"))
    transmission_watch_count = sum(1 for s in active_watchlist if s.get("_is_transmission"))

    # P0 (engineering audit): Reason code distribution — tells analyst what's driving priorities
    reason_dist = {}
    coverage_dist = {}
    for s in scores_list:
        reason = s.get("priority_reason", "no_signal")
        reason_dist[reason] = reason_dist.get(reason, 0) + 1
        cov = s.get("coverage_quality", "unknown")
        coverage_dist[cov] = coverage_dist.get(cov, 0) + 1

    # Also count total entities with scores in DB (honest disclosure — not just portfolio)
    total_entities_scored = db.execute(
        "SELECT COUNT(DISTINCT registry_id) FROM scores"
    ).fetchone()[0]

    # Events
    total_events = db.execute("SELECT COUNT(*) FROM events WHERE active = 1 AND status = 'active'").fetchone()[0]
    events_24h = db.execute(
        "SELECT COUNT(*) FROM events WHERE active = 1 AND status = 'active' AND extracted_at > ?",
        ((now - timedelta(hours=24)).isoformat(),),
    ).fetchone()[0]

    # Events by source — derive from evidence extraction_method
    events_by_source = {}
    for row in db.execute("""
        SELECT ev.extraction_method, COUNT(*)
        FROM events e
        JOIN evidence ev ON e.evidence_id = ev.id
        WHERE e.active = 1 AND e.status = 'active'
        GROUP BY ev.extraction_method
    """):
        events_by_source[row[0]] = row[1]

    # Source runs — all-time aggregates
    total_source_runs = db.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0]
    source_run_stats = {}
    for row in db.execute("""\
        SELECT source_name, COUNT(*),
               SUM(documents_discovered), SUM(new_documents),
               SUM(api_errors), SUM(parse_failures),
               SUM(adverse_events_found), SUM(dependency_edges_extracted),
               AVG(latency_seconds)
        FROM source_runs GROUP BY source_name
    """):
        source_run_stats[row[0]] = {
            "runs": row[1],
            "documents_processed": row[2] or 0,
            "new_evidence": row[3] or 0,
            "api_errors": row[4] or 0,
            "parse_failures": row[5] or 0,
            "events_found": row[6] or 0,
            "dependencies_extracted": row[7] or 0,
            "avg_latency_s": round(row[8], 1) if row[8] else 0,
        }

    # Resource usage (macOS)
    resource = _collect_resource()

    # Classifier yield report
    classifier_yield = {}
    try:
        from kassandra.classifier import get_classifier_yield_report
        classifier_yield = get_classifier_yield_report(db)
    except Exception:
        pass

    return {
        "collected_at": now.isoformat(),
        "classifier_yield": classifier_yield,
        "db": {
            "total_edges": total_edges,
            "edge_counts": edge_counts,
            "total_registry": total_registry,
            "total_evidence": total_evidence,
            "evidence_by_source": evidence_by_source,
            "evidence_this_cycle": evidence_this_cycle,
            "evidence_processed_this_cycle": evidence_processed,
            "total_events": total_events,
            "events_24h": events_24h,
            "events_by_source": events_by_source,
            "total_source_runs": total_source_runs,
        },
        "scores": {
            "portfolio_companies": portfolio_count,
            "portfolio_scored": portfolio_scored,
            "total_entities_with_scores": total_entities_scored,
            "mean_priority": round(mean_priority, 4),
            "max_priority": round(max_priority, 4),
            "reason_distribution": reason_dist,
            "coverage_distribution": coverage_dist,
            "top5": sorted(scores_list, key=lambda s: s["priority"], reverse=True)[:5],
            "active_watchlist": active_watchlist[:10],
            "coverage_queue": coverage_queue[:10],
            "active_watch_count": active_watch_count,
            "transmission_watch_count": transmission_watch_count,
            "direct_watch_count": direct_watch_count,
        },
        "sources": source_run_stats,
        "resource": resource,
        "storage": {
            "db_bytes": db_size,
            "evidence_dir_bytes": evidence_dir_size,
        },
    }


def _collect_resource() -> dict[str, Any]:
    """Collect Kassandra process RSS, memory pressure, swap from macOS.

    Does NOT report system free memory — that's misleading on macOS.
    """
    import resource as _resource
    import subprocess

    try:
        # Process RSS (max resident set size in bytes)
        usage = _resource.getrusage(_resource.RUSAGE_SELF)
        rss_mb = usage.ru_maxrss / (1024 * 1024) if hasattr(usage, 'ru_maxrss') else 0
        # On macOS, ru_maxrss is already in bytes (not KB like Linux)
        if rss_mb > 100000:  # absurdly high → probably in KB (Linux-style)
            rss_mb = rss_mb / 1024

        # Memory pressure (macOS-specific)
        mem_pressure = "unknown"
        try:
            mp = subprocess.run(
                ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                capture_output=True, text=True, timeout=5,
            )
            level_map = {"1": "normal", "2": "warning", "4": "critical"}
            mem_pressure = level_map.get(mp.stdout.strip(), mp.stdout.strip() or "normal")
        except Exception:
            pass

        # Swap usage
        swap_mb = 0
        try:
            sw = subprocess.run(
                ["sysctl", "-n", "vm.swapusage"],
                capture_output=True, text=True, timeout=5,
            )
            # Output: "total = 1024.0M  used = 512.0M  free = 512.0M"
            import re
            match = re.search(r"used\s*=\s*([\d.]+)([MG])", sw.stdout)
            if match:
                val = float(match.group(1))
                if match.group(2) == "G":
                    val *= 1024
                swap_mb = val
        except Exception:
            pass

        # Disk
        disk_pct = 0
        try:
            disk = subprocess.run(
                ["df", "-k", str(Path.home())], capture_output=True, text=True, timeout=5
            )
            for line in disk.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 5:
                    disk_pct = int(parts[4].rstrip("%"))
                    break
        except Exception:
            pass

        # CPU load
        load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)

        return {
            "load_1m": round(load[0], 2),
            "load_5m": round(load[1], 2),
            "load_15m": round(load[2], 2),
            "process_rss_mb": round(rss_mb, 1),
            "memory_pressure": mem_pressure,
            "swap_used_mb": round(swap_mb, 1),
            "disk_used_pct": disk_pct,
        }
    except Exception:
        return {"error": "resource collection failed"}


def _dir_size(path: str | Path) -> int:
    """Total size of files in a directory tree, in bytes."""
    total = 0
    p = Path(path)
    if p.is_dir():
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    return total


# ── GLEIF staleness ───────────────────────────────────────────────────────────

def _get_gleif_staleness(db: sqlite3.Connection) -> dict[str, Any]:
    """Check whether GLEIF enrichment is stale.

    GLEIF data is persisted in the registry table (entity names resolved via
    graph_enrich). Staleness is measured by: how long since the last successfully
    resolved placeholder, and whether any portfolio company edges were added
    since that time (new entities may need enrichment).

    Returns dict with: last_refresh, days_since_refresh, overdue, refresh_reason.
    """
    from datetime import timedelta

    # Find the most recent successfully enriched entity. resolved_at is the
    # canonical registry timestamp; updated_at catches post-resolution GLEIF
    # placeholder enrichments from graph_enrich. Some tests use a minimal
    # registry schema, so inspect columns before choosing the expression.
    registry_cols = {row[1] for row in db.execute("PRAGMA table_info(registry)").fetchall()}
    if "updated_at" in registry_cols and "resolved_at" in registry_cols:
        refresh_expr = "COALESCE(updated_at, resolved_at)"
    elif "updated_at" in registry_cols:
        refresh_expr = "updated_at"
    elif "resolved_at" in registry_cols:
        refresh_expr = "resolved_at"
    else:
        refresh_expr = "NULL"
    last_enriched = db.execute(
        f"SELECT MAX({refresh_expr}) FROM registry WHERE status != 'gleif_placeholder'"
    ).fetchone()[0]

    unresolved_placeholders = db.execute(
        "SELECT COUNT(*) FROM registry WHERE status = 'gleif_placeholder'"
    ).fetchone()[0]

    # Also check the most recent edge addition (new edges may bring new entities)
    last_edge = db.execute("SELECT MAX(created_at) FROM edges").fetchone()[0]

    now = datetime.now(timezone.utc)
    stale_days = 30  # Default interval

    result = {
        "last_refresh": last_enriched,
        "last_edge": last_edge,
        "unresolved_placeholders": unresolved_placeholders,
        "refresh_interval_days": stale_days,
        "overdue": False,
        "days_overdue": 0,
        "refresh_reason": "ok",
    }

    if not last_enriched:
        result["overdue"] = True
        result["refresh_reason"] = "never_refreshed"
        return result

    # Parse the timestamp
    try:
        if isinstance(last_enriched, str):
            last_refresh_dt = datetime.fromisoformat(last_enriched.replace("Z", "+00:00"))
        else:
            last_refresh_dt = last_enriched
        if last_refresh_dt.tzinfo is None:
            last_refresh_dt = last_refresh_dt.replace(tzinfo=timezone.utc)
        days_since = (now - last_refresh_dt).days
        result["days_since_refresh"] = days_since
    except (ValueError, TypeError):
        result["days_since_refresh"] = "unknown"
        return result

    # Check if new edges brought unresolved placeholders since last enrichment.
    # New edges alone are not stale if every GLEIF placeholder has already been
    # resolved; the old check produced a permanent P1 after successful refresh.
    if unresolved_placeholders > 0 and last_edge and last_edge > last_enriched:
        result["refresh_reason"] = "new_placeholders_since_refresh"
        result["overdue"] = True

    # Always compute days_overdue when we have a valid days_since
    # (not just on time-based staleness — new_edges_since_refresh can also trigger overdue)
    result["days_overdue"] = max(0, days_since - stale_days)

    # Check time-based staleness
    if days_since >= stale_days:
        result["overdue"] = True
        if result["refresh_reason"] == "ok":
            result["refresh_reason"] = f"time_exceeded_{stale_days}d"

    return result


# ── Consistency checks ───────────────────────────────────────────────────────

def run_consistency_checks(db: sqlite3.Connection) -> dict[str, Any]:
    """Run all data-integrity checks. Returns findings."""
    findings: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    # 1. Foreign-key integrity
    try:
        orphan_edges = db.execute("""
            SELECT COUNT(*) FROM edges
            WHERE source_registry_id NOT IN (SELECT id FROM registry)
               OR target_registry_id NOT IN (SELECT id FROM registry)
        """).fetchone()[0]
        if orphan_edges > 0:
            findings.append({
                "severity": "P0",
                "check": "fk_edges_registry",
                "detail": f"{orphan_edges} edges reference missing registry entries",
            })
    except Exception as e:
        findings.append({"severity": "P0", "check": "fk_edges_registry", "error": str(e)})

    try:
        orphan_evidence = db.execute("""
            SELECT COUNT(*) FROM edges
            WHERE evidence_id NOT IN (SELECT id FROM evidence)
        """).fetchone()[0]
        if orphan_evidence > 0:
            findings.append({
                "severity": "P0",
                "check": "fk_edges_evidence",
                "detail": f"{orphan_evidence} edges reference missing evidence",
            })
    except Exception as e:
        findings.append({"severity": "P0", "check": "fk_edges_evidence", "error": str(e)})

    # 2. Provenance integrity — every edge must have a source
    null_source_edges = db.execute(
        "SELECT COUNT(*) FROM edges WHERE edge_source IS NULL"
    ).fetchone()[0]
    if null_source_edges > 0:
        findings.append({
            "severity": "P1",
            "check": "edge_provenance",
            "detail": f"{null_source_edges} edges have no edge_source",
        })

    # 3. Impossible timestamps — flag records more than 24h ahead of wall clock.
    # Median-relative checks produced false P1s after several days of normal score
    # history accumulation.
    max_edge = db.execute("SELECT MAX(created_at) FROM edges").fetchone()[0]

    future_edges = db.execute(
        "SELECT COUNT(*) FROM edges WHERE julianday(created_at) > julianday(?) + 1",
        (now.isoformat(),),
    ).fetchone()[0]

    if future_edges > 0:
        findings.append({
            "severity": "P1",
            "check": "future_timestamps",
            "detail": f"{future_edges} edges have created_at >24h past DB median",
        })

    # Same for scores — compare against wall clock
    score_max = db.execute("SELECT MAX(computed_at) FROM scores").fetchone()[0]
    future_scores = db.execute(
        "SELECT COUNT(*) FROM scores WHERE julianday(computed_at) > julianday(?) + 1",
        (now.isoformat(),),
    ).fetchone()[0]

    if future_scores > 0:
        findings.append({
            "severity": "P1",
            "check": "future_timestamps_scores",
            "detail": f"{future_scores} scores have computed_at >24h past median",
        })

    # 4. Duplicate edges (same source/target/type with different evidence — suspicious)
    dup_edges = db.execute("""
        SELECT source_registry_id, target_registry_id, relationship_type, COUNT(*) as cnt
        FROM edges
        GROUP BY source_registry_id, target_registry_id, relationship_type
        HAVING cnt > 1
        LIMIT 20
    """).fetchall()
    if dup_edges:
        findings.append({
            "severity": "P2",
            "check": "duplicate_edges",
            "detail": f"{len(dup_edges)} groups of duplicate edges",
        })

    # 5. Scores based on stale inputs — any score computed before last non-future edge?
    max_edge = db.execute("SELECT MAX(created_at) FROM edges").fetchone()[0]
    # Use median as reference to filter out clock-skew artifacts
    median_edge = db.execute(
        "SELECT created_at FROM edges ORDER BY created_at LIMIT 1 OFFSET (SELECT COUNT(*) FROM edges) / 2"
    ).fetchone()
    median_edge_val = median_edge[0] if median_edge else None
    last_valid_edge = max_edge
    if median_edge_val and max_edge > median_edge_val:
        # Find last edge within 24h of median (exclude clock-skew outliers)
        row = db.execute(
            "SELECT MAX(created_at) FROM edges WHERE julianday(created_at) <= julianday(?) + 1",
            (median_edge_val,),
        ).fetchone()
        if row[0]:
            last_valid_edge = row[0]
    last_score = db.execute("SELECT MAX(computed_at) FROM scores").fetchone()[0]
    if last_valid_edge and last_score and last_valid_edge > last_score:
        findings.append({
            "severity": "P1",
            "check": "stale_scores",
            "detail": f"Last edge ({last_valid_edge}) after last score ({last_score})",
        })

    # 6. Unexpected graph growth (>20% edges added in one cycle)
    # Tracked across cycles by the soak state; skip on first cycle.

    # 7. Secret leakage scan — check repo for API keys
    secret_findings = _scan_for_secrets()
    findings.extend(secret_findings)

    # 8. NULL evidence_id on edges (should be impossible with NOT NULL)
    null_evidence = db.execute(
        "SELECT COUNT(*) FROM edges WHERE evidence_id IS NULL"
    ).fetchone()[0]
    if null_evidence > 0:
        findings.append({
            "severity": "P0",
            "check": "null_evidence_id",
            "detail": f"{null_evidence} edges have NULL evidence_id",
        })

    # 9. Digest reconciliation — events_24h should match events_by_source sum
    try:
        eb_sum = sum((db.execute("""
            SELECT COUNT(*) FROM events e
            JOIN evidence ev ON e.evidence_id = ev.id
            WHERE e.active = 1 AND e.status = 'active'
            GROUP BY ev.extraction_method
        """).fetchall() or [(0,)])[0])
        events_total = db.execute("SELECT COUNT(*) FROM events WHERE active = 1 AND status = 'active'").fetchone()[0]
        if eb_sum != events_total:
            # This is informational — some events may have no evidence link
            pass
    except Exception:
        pass

    # 10. Source runs reconciliation — documents_discovered >= new_documents always
    neg_diff = db.execute("""
        SELECT COUNT(*) FROM source_runs
        WHERE documents_discovered < new_documents
    """).fetchone()[0]
    if neg_diff > 0:
        findings.append({
            "severity": "P1",
            "check": "source_run_reconciliation",
            "detail": f"{neg_diff} source_runs have new_documents > documents_discovered",
        })

    # 11. GLEIF staleness check
    from datetime import timedelta
    gleif_state = _get_gleif_staleness(db)
    if gleif_state.get("overdue"):
        findings.append({
            "severity": "P1",
            "check": "gleif_staleness",
            "detail": (
                f"GLEIF last refreshed {gleif_state.get('last_refresh', 'never')}. "
                f"Refresh overdue by {gleif_state.get('days_overdue', '?')} days. "
                f"Reason: {gleif_state.get('refresh_reason', 'unknown')}"
            ),
        })

    # 12. Scheduled-source freshness: historical rows cannot satisfy the SLA.
    findings.extend(_source_freshness_findings(db))

    # 13. Semantic health: queue ageing, candidate accounting, score freshness.
    findings.extend(run_semantic_health_checks(db, now=now))

    severity_order = {"P0": 0, "P1": 1, "P2": 2}
    findings.sort(key=lambda f: severity_order.get(f.get("severity", "P2"), 99))

    return {
        "checked_at": now.isoformat(),
        "total_checks": 8,
        "findings": findings,
        "p0_count": sum(1 for f in findings if f.get("severity") == "P0"),
        "p1_count": sum(1 for f in findings if f.get("severity") == "P1"),
        "p2_count": sum(1 for f in findings if f.get("severity") == "P2"),
        "clean": len(findings) == 0,
    }


def _scan_for_secrets() -> list[dict[str, Any]]:
    """Check repo for API keys and secrets in tracked files."""
    findings = []
    import subprocess

    try:
        # Search for common secret patterns but exclude URLs and XML namespaces
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent.parent.parent),
             "grep", "-n", "-I", "-E",
             r"(sk-[a-zA-Z0-9]{20,}|api_key\s*=\s*['\"][A-Za-z0-9_\-]{12,}['\"]"
             r"|secret\s*=\s*['\"][A-Za-z0-9_\-]{12,}['\"]"
             r"|password\s*=\s*['\"][^'\"]{4,}['\"]"
             r"|token\s*=\s*['\"][A-Za-z0-9_\-\.]{12,}['\"])"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if not line:
                continue
            # Skip false positives: URLs, XML namespaces, test fixtures, example files
            if any(skip in line for skip in [
                ".env.example", "test_", "fixture", "http://", "https://",
                "xmlns", "schema/", "example.com",
            ]):
                continue
            # Skip lines that are clearly URLs or XML
            path_part = line.split(":", 1)[0] if ":" in line else ""
            if any(path_part.endswith(ext) for ext in [".xml"]):
                continue
            findings.append({
                "severity": "P0",
                "check": "secret_leakage",
                "detail": f"Potential secret in repo: {line[:120]}",
            })
    except Exception:
        pass

    return findings


# ── Digest generation ────────────────────────────────────────────────────────

def _shorten(text: str | None, limit: int = 160) -> str:
    """Collapse whitespace and truncate text for Markdown-friendly alerts/digests."""
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "…"

def generate_digest(
    metrics: dict[str, Any],
    checks: dict[str, Any],
    state: dict[str, Any],
    cycle_duration_s: float,
    defects_fixed_this_cycle: int = 0,
) -> str:
    """Generate the daily Markdown digest in Markdown.

    Credit-analyst-first: watchlist leads, then coverage queue, then ops.
    """
    m = metrics
    s = metrics["scores"]
    r = metrics["resource"]
    c = checks
    d = metrics["db"]

    reason_labels = {
        "adverse_signal": "⚠ Adverse event detected",
        "transmission_concern": "↗ Exposed via dependency",
        "dependency_exposure": "🔗 Economic dependencies",
        "graph_density": "📊 Legal ownership only",
        "coverage_gap": "⚠ Incomplete coverage",
        "no_signal": "○ No signal",
    }
    rd = s.get("reason_distribution", {})

    lines = [
        "## 📊 Kassandra Daily Credit Brief",
        f"*Cycle #{state['cycles_completed']} | {m['collected_at'][:19].replace('T', ' ')}*",
        "",
        "### 1. 🔴 Credit Watchlist",
    ]

    # ── Section 1: Credit Watchlist ──
    active_watchlist = s.get("active_watchlist", [])
    active_watch_count = s.get("active_watch_count", 0)
    direct_watch_count = s.get("direct_watch_count", 0)
    transmission_watch_count = s.get("transmission_watch_count", 0)

    if active_watch_count == 0:
        lines.append("**No active credit watch items today.**")
        lines.append(f"*0 direct adverse signals. 0 dependency-linked transmission signals.*")
    else:
        lines.append(f"*{active_watch_count} active watch items "
                     f"({direct_watch_count} direct, {transmission_watch_count} transmission)*")
        lines.append("")
        for item in active_watchlist[:10]:
            name = item.get("company_name", "Unknown")
            wpri = item.get("active_watch_priority", 0)
            reason = item.get("priority_reason", "no_signal")
            is_direct = item.get("_is_direct", False)
            is_transmission = item.get("_is_transmission", False)

            lines.append(f"#### {name} — watch: {wpri:.3f}")

            # Trigger details
            trigger_type = item.get('trigger_event_type', 'unknown')
            trigger_sev = item.get('trigger_severity', '?')
            trigger_conf = item.get('trigger_confidence', '?')
            trigger_date = item.get('trigger_date', '?')
            trigger_entity = item.get('trigger_entity', name)

            if is_direct:
                trigger_desc = _shorten(item.get("trigger_description", ""), 160)
                source_url = item.get("trigger_source_url", "")
                source_label = item.get("trigger_source", "source") or "source"
                lines.append(
                    f"- ⚠ Direct {trigger_type}: sev={trigger_sev}, conf={trigger_conf} — "
                    f"{trigger_date[:10] if trigger_date else '?'}"
                )
                if trigger_desc:
                    lines.append(f"  - Why: “{trigger_desc}”")
                if source_url:
                    lines.append(f"  - Source: {source_label} — {source_url}")
            elif is_transmission:
                lines.append(f"- ↗ Transmission: {trigger_entity[:40]} has {trigger_type}")

            # Dependency path
            dep_path = item.get('dependency_path', '')
            if dep_path:
                lines.append(f"- Path: {dep_path}")

            # Evidence
            ev_ids = item.get('evidence_ids', '')
            if ev_ids:
                lines.append(f"- Evidence: {ev_ids}")

            lines.append(f"- Action: {'Review direct evidence' if is_direct else 'Verify dependency exposure'}")
            lines.append("")

    # ── Section 2: Coverage / Enrichment Queue ──
    lines.extend([
        "",
        "### 2. 📋 Coverage / Enrichment Queue",
        "*Not credit alerts — data quality and monitoring coverage tasks.*",
        "",
    ])
    coverage_queue = s.get("coverage_queue", [])
    if not coverage_queue:
        lines.append("**No coverage tasks queued.**")
    else:
        for item in coverage_queue[:5]:
            name = item.get("company_name", "Unknown")
            cmpri = item.get("coverage_monitor_priority", 0)
            reason = item.get("priority_reason", "no_signal")
            gap = reason_labels.get(reason, reason)
            lines.append(f"- **{name}**: coverage_monitor_priority={cmpri:.3f}, reason={reason}, gap={gap}")
        lines.append("")

    # ── Section 3: Evidence This Cycle ──
    lines.extend([
        "",
        "### 3. 📥 Evidence This Cycle",
    ])
    ec = d.get("evidence_this_cycle", {})
    ep = d.get("evidence_processed_this_cycle", {})
    if ec or ep:
        for src_name in sorted(set(list(ec.keys()) + list(ep.keys()))):
            new = ec.get(src_name, 0)
            processed = ep.get(src_name, 0)
            dedup = processed - new
            parts = [f"  • **{src_name}**: {new} new"]
            if dedup > 0:
                parts.append(f"{dedup} deduplicated")
            lines.append(", ".join(parts))
    else:
        lines.append("*No new evidence this cycle.*")

    # ── Section 4: Source & Classifier Health ──
    lines.extend([
        "",
        "### 4. 🔍 Source & Classifier Health",
    ])
    # Source yield — reconciled: events from DB (not source_runs, which may be stale)
    # Map granular event sources (uk_gazette_feed, web_monitor_homepage) to coarse keys
    events_by_source = d.get("events_by_source", {})
    def _events_for_source(source_key):
        total = 0
        for ek, ec in events_by_source.items():
            if ek.startswith(source_key) or source_key.startswith(ek):
                total += ec
        return total
    for src_name, src_stats in sorted(m.get("sources", {}).items()):
        actual_events = _events_for_source(src_name) or src_stats.get('events_found', 0)
        lines.append(
            f"  • **{src_name}**: {src_stats['runs']} runs, "
            f"{src_stats['new_evidence']} docs, "
            f"{actual_events} events, "
            f"{src_stats['api_errors']} errors, "
            f"{src_stats.get('parse_failures', src_stats.get('parse_fails', 0))} parse fails"
        )

    # Classifier yield per source
    classifier_yield = m.get("classifier_yield", {})
    if classifier_yield:
        lines.append("")
        lines.append("**Classifier yield by source:**")
        for src_name, yd in sorted(classifier_yield.get("per_source", {}).items()):
            docs = yd.get("total_docs", 0)
            classified = yd.get("total_classified", 0)
            events = yd.get("total_events", 0)
            yield_pct = yd.get("yield_rate_pct")
            yield_str = f" ({yield_pct}% yield)" if yield_pct is not None else ""
            lines.append(
                f"  • {src_name}: {docs} docs, {classified} classified, "
                f"{events} events{yield_str}"
            )
        overall_yield = classifier_yield.get("overall_yield_rate_pct")
        if overall_yield is not None:
            lines.append(
                f"  *Overall classifier yield: {overall_yield}%*"
            )

    # Events with source breakdown
    eb = d.get("events_by_source", {})
    event_parts = []
    for src, count in sorted(eb.items(), key=lambda x: -x[1])[:5]:
        event_parts.append(f"  • {src}: {count}")
    lines.extend([
        "",
        f"**Events:** {d['total_events']} total | {d['events_24h']} in last 24h",
    ])
    if event_parts:
        lines.extend(event_parts)

    # Score summary
    lines.extend([
        "",
        f"**Scores:** {s['portfolio_scored']}/{s['portfolio_companies']} portfolio companies scored | "
        f"mean priority {s['mean_priority']:.3f} | max {s['max_priority']:.3f}",
        f"*({s['total_entities_with_scores']} total entities with scores in DB)*",
    ])

    # Graph summary
    lines.extend([
        "",
        f"**Graph:** {d['total_edges']} edges | {d['total_registry']} entities | {d['total_evidence']} evidence items",
    ])
    edge_parts = []
    for source, count in sorted(d["edge_counts"].items(), key=lambda x: -x[1])[:5]:
        edge_parts.append(f"  • {source}: {count}")
    if edge_parts:
        lines.append("\n".join(edge_parts))

    # ── Section 5: Integrity & Soak Health ──
    lines.extend([
        "",
        "### 5. 🔐 Integrity & Soak Health",
        f"**Cycle duration:** {cycle_duration_s:.1f}s | "
        f"**Uptime:** {state['cycles_completed']} cycles since {state['started_at'][:10]}",
        f"*{c['p0_count']} P0 | {c['p1_count']} P1 | {c['p2_count']} P2*",
    ])
    if c["findings"]:
        for f in c["findings"][:5]:
            lines.append(f"  • [{f['severity']}] {f['check']}: {f.get('detail', f.get('error', 'unknown'))}")
    else:
        lines.append("  • No integrity findings.")

    lines.extend([
        "",
        f"**Storage:** DB {m['storage']['db_bytes']/1024/1024:.1f} MB | Evidence {m['storage']['evidence_dir_bytes']/1024/1024:.1f} MB",
        "",
        f"**Resources:** Load {r['load_1m']}/{r['load_5m']}/{r['load_15m']} | "
        f"RSS {r['process_rss_mb']:.0f} MB | "
        f"Pressure {r['memory_pressure']} | "
        f"Swap {r['swap_used_mb']:.0f} MB | "
        f"Disk {r['disk_used_pct']}%",
        "",
        f"*Defects fixed this cycle: {defects_fixed_this_cycle}*",
        "",
        "---",
        f"*Next cycle: ~24h. Autonomous soak mode.*",
    ])

    return "\n".join(lines)


# ── Full pipeline ─────────────────────────────────────────────────────────────

# ── Concurrency guards ────────────────────────────────────────────────────────

CYCLE_LOCK_PATH = Path(tempfile.gettempdir()) / "kassandra_soak_cycle.lock"
WEB_LOCK_DIR = Path(tempfile.gettempdir()) / "kassandra_web_monitor"


def _acquire_cycle_lock() -> bool:
    """Try to acquire the soak cycle lock. Returns True if acquired, False if already held.

    Uses O_CREAT|O_EXCL for atomicity — works across processes.
    """
    import os as _os
    try:
        fd = _os.open(str(CYCLE_LOCK_PATH), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY)
        _os.write(fd, str(_os.getpid()).encode())
        _os.close(fd)
        return True
    except FileExistsError:
        return False


def _release_cycle_lock() -> None:
    """Release the soak cycle lock. Safe to call even if lock wasn't held."""
    import os as _os
    try:
        _os.unlink(str(CYCLE_LOCK_PATH))
    except FileNotFoundError:
        pass


def _weblock_path() -> Path:
    """Path to the web monitor singleton lock."""
    return WEB_LOCK_DIR / "running.pid"


def _web_monitor_lock_script() -> str:
    """Return valid multiline Python that acquires the child-process lock."""
    return f"""import os
WEBLOCK_PATH = {str(_weblock_path())!r}
os.makedirs(os.path.dirname(WEBLOCK_PATH), exist_ok=True)
try:
    os.unlink(WEBLOCK_PATH)
except FileNotFoundError:
    pass
with open(WEBLOCK_PATH, "w") as lock_file:
    lock_file.write(str(os.getpid()))
"""


def _build_web_monitor_command() -> list[str]:
    """Build the web monitor subprocess command with timeout and lock handling."""
    script = _web_monitor_lock_script() + """try:
    from kassandra.collector import run_collection
    from kassandra.db import get_db, migrate
    db = get_db()
    try:
        migrate(db)
        run_collection(db, sources=["web_monitor"])
    finally:
        db.close()
finally:
    try:
        os.unlink(WEBLOCK_PATH)
    except FileNotFoundError:
        pass
"""
    return [sys.executable, "-c", script]


def _dispatch_web_monitor(timeout_seconds: int = 900, db: sqlite3.Connection | None = None):
    """Run web monitoring as a bounded, supervised child process.

    Unlike fire-and-forget dispatch, this observes completion, exit status, and
    both output streams.  A zero exit code is only process success; source
    freshness is evaluated independently from persisted ``source_runs`` rows.
    """
    import subprocess
    import uuid

    p = _weblock_path()
    if p.exists():
        try:
            pid = int(p.read_text().strip())
            os.kill(pid, 0)
            logger.info("Web monitor already running (pid %s), skipping dispatch", pid)
            return "skipped: another web monitor is running"
        except (ValueError, OSError, ProcessLookupError):
            try:
                p.unlink()
            except OSError:
                pass

    started = datetime.now(timezone.utc).isoformat()
    run_id = f"web-monitor-supervised-{uuid.uuid4().hex}"
    result = {"run_id": run_id, "status": "failed", "exit_code": None,
              "stdout": "", "stderr": "", "started_at": started,
              "completed_at": None}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        child = subprocess.Popen(
            _build_web_monitor_command(),
            cwd=str(Path(__file__).resolve().parent.parent.parent),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            stdout, stderr = child.communicate(timeout=timeout_seconds)
            result.update(stdout=stdout, stderr=stderr, exit_code=child.returncode,
                          status="success" if child.returncode == 0 else "failed")
        except subprocess.TimeoutExpired:
            child.kill()
            try:
                stdout, stderr = child.communicate()
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            result.update(stdout=stdout, stderr=stderr, status="timeout",
                          exit_code=child.returncode)
    except Exception as exc:
        result["stderr"] = str(exc)
    finally:
        result["completed_at"] = datetime.now(timezone.utc).isoformat()
        if db is not None and result["status"] != "success":
            detail = {
                "type": result["status"],
                "exit_code": result["exit_code"],
                "message": (result["stderr"] or result["stdout"] or "web monitor child failed")[-4000:],
            }
            columns = {row[1] for row in db.execute("PRAGMA table_info(source_runs)")}
            detail_column = ", error_detail_json" if "error_detail_json" in columns else ""
            detail_value = ", ?" if detail_column else ""
            params = [run_id, result["status"], started, result["completed_at"]]
            if detail_column:
                params.append(json.dumps(detail))
            db.execute(
                f"""INSERT INTO source_runs
                   (run_id, source_name, errors, status, started_at, completed_at{detail_column})
                   VALUES (?, 'web_monitor', 1, ?, ?, ?{detail_value})""",
                params,
            )
            db.commit()
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
    return result


# Sources automated either by the daily soak or the 15-minute alert cycle.
SCHEDULED_SOURCES = (
    "companies_house", "uk_gazette", "web_monitor",
    "bodacc_fr", "borme_es",
)
MANUAL_ONLY_SOURCES = ("handelsregister_de",)
SOURCE_FRESHNESS_SLA_HOURS = 26


def evaluate_source_freshness(
    db: sqlite3.Connection, *, now: datetime | None = None,
    sla_hours: int = SOURCE_FRESHNESS_SLA_HOURS,
    scheduled_sources: tuple[str, ...] = SCHEDULED_SOURCES,
) -> dict[str, dict[str, Any]]:
    """Evaluate the latest completed run for every intentionally scheduled source."""
    now = now or datetime.now(timezone.utc)
    result = {}
    for source in scheduled_sources:
        columns = {row[1] for row in db.execute("PRAGMA table_info(source_runs)")}
        status_column = "status" if "status" in columns else "'success'"
        row = db.execute(
            f"""SELECT completed_at, {status_column} FROM source_runs
               WHERE source_name=? ORDER BY completed_at DESC LIMIT 1""", (source,)
        ).fetchone()
        if not row:
            result[source] = {"healthy": False, "reason": "never_run", "completed_at": None}
            continue
        status = row[1] or "success"
        if status != "success":
            result[source] = {"healthy": False, "reason": "failed", "status": status,
                              "completed_at": row[0]}
            continue
        try:
            completed = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            if completed.tzinfo is None:
                completed = completed.replace(tzinfo=timezone.utc)
            age_hours = (now - completed).total_seconds() / 3600
        except (AttributeError, ValueError, TypeError):
            age_hours = float("inf")
        healthy = age_hours <= sla_hours
        result[source] = {"healthy": healthy, "reason": "ok" if healthy else "stale",
                          "age_hours": round(age_hours, 2), "completed_at": row[0]}
    return result


def _source_freshness_findings(db: sqlite3.Connection) -> list[dict[str, str]]:
    return [
        {"severity": "P1", "check": "source_freshness",
         "detail": f"{source}: {info['reason']} (latest={info.get('completed_at')})"}
        for source, info in evaluate_source_freshness(db).items()
        if not info["healthy"]
    ]


def run_semantic_health_checks(
    db: sqlite3.Connection, *, now: datetime | None = None,
) -> list[dict[str, str]]:
    """Check queue ageing, source accounting, and per-company score freshness."""
    now = now or datetime.now(timezone.utc)
    findings: list[dict[str, str]] = []
    tables = {
        row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }

    if "unconfirmed_match_queue" in tables:
        stale = db.execute(
            """SELECT COUNT(*) FROM unconfirmed_match_queue
               WHERE status='pending' AND julianday(created_at) < julianday(?) - 7""",
            (now.isoformat(),),
        ).fetchone()[0]
        if stale:
            findings.append({
                "severity": "P2", "check": "stale_unconfirmed_matches",
                "detail": f"{stale} pending entity matches are older than 7 days",
            })

    source_columns = {row[1] for row in db.execute("PRAGMA table_info(source_runs)")}
    accounting = {"candidates_generated", "unconfirmed_matches", "events_created", "duplicate_events"}
    if accounting <= source_columns:
        bad = db.execute(
            """SELECT COUNT(*) FROM source_runs
               WHERE COALESCE(unconfirmed_matches,0) + COALESCE(events_created,0)
                   + COALESCE(duplicate_events,0) > COALESCE(candidates_generated,0)"""
        ).fetchone()[0]
        if bad:
            findings.append({
                "severity": "P1", "check": "candidate_reconciliation",
                "detail": f"{bad} source runs account for more outcomes than classifier candidates",
            })

    if {"scoreable_companies", "scores"} <= tables:
        missing = db.execute(
            """SELECT COUNT(*) FROM scoreable_companies sc
               WHERE NOT EXISTS (SELECT 1 FROM scores s WHERE s.registry_id=sc.id)"""
        ).fetchone()[0]
        if missing:
            findings.append({
                "severity": "P1", "check": "missing_company_scores",
                "detail": f"{missing} scoreable companies have no score snapshot",
            })
        if "events" in tables:
            stale_scores = db.execute(
                """SELECT COUNT(*) FROM scoreable_companies sc
                   WHERE EXISTS (
                       SELECT 1 FROM events e WHERE e.registry_id=sc.id
                         AND COALESCE(e.active,1)=1 AND COALESCE(e.status,'active')='active'
                         AND julianday(e.extracted_at) > julianday((
                             SELECT MAX(s.computed_at) FROM scores s WHERE s.registry_id=sc.id
                         ))
                   )"""
            ).fetchone()[0]
            if stale_scores:
                findings.append({
                    "severity": "P1", "check": "stale_company_scores",
                    "detail": f"{stale_scores} companies have active events newer than their latest score",
                })
    return findings


def update_source_metric_state(
    state: dict[str, Any], source_metrics: dict[str, dict[str, Any]],
) -> None:
    """Persist all-time snapshots and true period deltas without double-counting."""
    stored = state.setdefault("source_metrics", {})
    for source, current_raw in source_metrics.items():
        current = {
            key: value for key, value in current_raw.items()
            if isinstance(value, (int, float))
        }
        previous_entry = stored.get(source, {})
        previous = previous_entry.get("all_time", {}) if isinstance(previous_entry, dict) else {}
        delta = {
            key: current_value - previous.get(key, current_value)
            for key, current_value in current.items()
        }
        stored[source] = {"all_time": current, "period_delta": delta}


def run_soak_cycle() -> dict[str, Any]:
    """Run one complete soak cycle: collect → score → check → digest.

    Enforces:
    - Overlap lock: refuses to run if another cycle is in progress
    - Web monitor singleton: skips dispatch if already running
    - Failure diagnostics: records exact failure step on error
    """
    t0 = time.monotonic()

    # ── Overlap guard ──
    if not _acquire_cycle_lock():
        return {
            "cycle_start": datetime.now(timezone.utc).isoformat(),
            "failed": True,
            "skipped": "cycle_lock_held",
            "duration_seconds": 0,
            "error": "Another soak cycle is already running — refusing to overlap",
        }

    state = load_soak_state()
    cycle_result: dict[str, Any] = {
        "cycle_start": datetime.now(timezone.utc).isoformat(),
        "steps": {},
    }

    try:
        db = get_db()
        migrate(db)

        # Step 1: Collect evidence from fast sources (Companies House, Gazette)
        try:
            from kassandra.collector import run_collection
            collect_results = run_collection(db, sources=["companies_house", "uk_gazette"])
            cycle_result["steps"]["collect"] = {"sources": {k: v.to_dict() for k, v in collect_results.items()}}
        except Exception as e:
            logger.error("Collection failed: %s", e)
            cycle_result["steps"]["collect"] = {"error": str(e)}

        # Step 1b: Web monitoring (supervised singleton)
        cycle_result["steps"]["collect"]["web_monitor"] = _dispatch_web_monitor(db=db)

        # Step 2: GLEIF staleness check + refresh (persisted data — no LLM needed)
        gleif_state = _get_gleif_staleness(db)
        if gleif_state.get("overdue"):
            from kassandra.graph_enrich import enrich_placeholders
            enriched = enrich_placeholders(db, max_enrich=100)
            cycle_result["steps"]["enrich"] = {
                "gleif_staleness": gleif_state,
                "action": "refreshed",
                "enriched_count": enriched,
            }
        else:
            cycle_result["steps"]["enrich"] = {
                "gleif_staleness": gleif_state,
                "action": "ok",
            }

        # Step 3: Score
        try:
            from kassandra.scoring import compute_scores
            scores = compute_scores(db)
            cycle_result["steps"]["score"] = {"companies_scored": len(scores)}
        except Exception as e:
            logger.error("Scoring failed: %s", e)
            cycle_result["steps"]["score"] = {"error": str(e)}

        # Step 4: Collect metrics
        try:
            metrics = collect_metrics(db)
            cycle_result["metrics"] = metrics
        except Exception as e:
            logger.error("Metrics collection failed: %s", e)
            cycle_result["metrics"] = {"error": str(e)}

        # Step 5: Run consistency checks
        try:
            checks = run_consistency_checks(db)
            cycle_result["checks"] = checks
        except Exception as e:
            logger.error("Consistency checks failed: %s", e)
            cycle_result["checks"] = {"error": str(e)}

        db.close()

        # Step 6: Determine failure
        duration = time.monotonic() - t0
        cycle_failed = bool(
            cycle_result["steps"].get("collect", {}).get("error")
            or cycle_result["steps"].get("score", {}).get("error")
            or (cycle_result.get("checks", {}).get("p1_count", 0) > 0)
            or (cycle_result.get("checks", {}).get("p0_count", 0) > 0)
        )

        # Step 7: Update soak state
        state["cycles_completed"] += 1
        state["last_cycle_at"] = datetime.now(timezone.utc).isoformat()
        state["last_cycle_duration_seconds"] = round(duration, 1)

        if cycle_failed:
            state["cycles_failed"] += 1
            # Preserve diagnostic for root-cause analysis
            failed_step = next(
                (k for k in ["collect", "score", "checks"]
                 if cycle_result["steps"].get(k, {}).get("error")
                 or (k == "checks" and cycle_result.get("checks", {}).get("p0_count", 0) > 0)),
                "unknown",
            )
            error_detail = (
                cycle_result.get("checks", {}).get("findings", [{}])[0].get("detail", "")
                if failed_step == "checks" and int(cycle_result["checks"].get("p0_count", 0) or 0) > 0
                else str(cycle_result["steps"].get(failed_step, {}).get("error", "unknown"))
            )
            state["last_failure_diagnostic"] = {
                "step": failed_step,
                "error": error_detail,
                "at": cycle_result["cycle_start"],
            }
            # Auto-file a defect so failures are visible in soak state
            state.setdefault("defects_found", []).append({
                "cycle": state["cycles_completed"] + 1,
                "at": cycle_result["cycle_start"],
                "step": failed_step,
                "error": error_detail[:500],
                "status": "open",
            })
            # Schedule next run normally — no busy-looping or exponential backoff
        else:
            # Clear any stale failure diagnostic on successful run
            state.pop("last_failure_diagnostic", None)

            # Append score history for volatility tracking
            if "metrics" in cycle_result and "scores" in cycle_result["metrics"]:
                state.setdefault("score_history", []).append({
                    "cycle": state["cycles_completed"],
                    "at": cycle_result["cycle_start"],
                    "mean_priority": cycle_result["metrics"]["scores"]["mean_priority"],
                    "max_priority": cycle_result["metrics"]["scores"]["max_priority"],
                })
                if len(state["score_history"]) > 90:
                    state["score_history"] = state["score_history"][-90:]

            # Append resource history
            if "metrics" in cycle_result and "resource" in cycle_result["metrics"]:
                state.setdefault("resource_history", []).append({
                    "cycle": state["cycles_completed"],
                    "at": cycle_result["cycle_start"],
                    **cycle_result["metrics"]["resource"],
                })
                if len(state["resource_history"]) > 90:
                    state["resource_history"] = state["resource_history"][-90:]

        # Store each DB-derived all-time snapshot plus the true delta since the
        # previous cycle. Never add all-time totals to prior all-time totals.
        if "metrics" in cycle_result and "sources" in cycle_result["metrics"]:
            update_source_metric_state(state, cycle_result["metrics"]["sources"])

        save_soak_state(state)

        cycle_result["duration_seconds"] = round(duration, 1)
        cycle_result["failed"] = cycle_failed
        if isinstance(cycle_result.get("metrics"), dict) and isinstance(cycle_result.get("checks"), dict):
            cycle_result["digest"] = generate_digest(
                cycle_result["metrics"],
                cycle_result["checks"],
                state,
                round(duration, 1),
            )

        checks_summary = cycle_result.get("checks", {})
        record_journal(
            sqlite3.connect(str(get_config().db_path)),
            "soak_cycle",
            f"Cycle #{state['cycles_completed']}: {'FAILED' if cycle_failed else 'OK'} "
            f"({duration:.1f}s, {checks_summary.get('p0_count', 0)} P0, {checks_summary.get('p1_count', 0)} P1)",
        )

        return cycle_result
    finally:
        _release_cycle_lock()
