"""Tests for the near-real-time alert daemon."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch


def _make_daemon_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT NOT NULL,
            status TEXT
        );
        CREATE TABLE IF NOT EXISTS journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT
        );
        CREATE TABLE IF NOT EXISTS job_state (
            job_name TEXT PRIMARY KEY,
            status TEXT,
            last_run_at TEXT,
            last_error TEXT,
            payload_json TEXT
        );
        CREATE TABLE IF NOT EXISTS sources (
            source_name TEXT PRIMARY KEY,
            source_type TEXT,
            base_url TEXT,
            status TEXT DEFAULT 'active',
            last_success_at TEXT,
            last_failure_at TEXT,
            consecutive_failures INTEGER DEFAULT 0,
            total_requests INTEGER DEFAULT 0,
            total_evidence INTEGER DEFAULT 0,
            unique_events INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS source_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            source_name TEXT NOT NULL,
            documents_discovered INTEGER DEFAULT 0,
            documents_fetched INTEGER DEFAULT 0,
            new_documents INTEGER DEFAULT 0,
            duplicate_documents INTEGER DEFAULT 0,
            candidates_generated INTEGER DEFAULT 0,
            unconfirmed_matches INTEGER DEFAULT 0,
            events_created INTEGER DEFAULT 0,
            duplicate_events INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0,
            api_errors INTEGER DEFAULT 0,
            parse_failures INTEGER DEFAULT 0,
            adverse_events_found INTEGER DEFAULT 0,
            dependency_edges_extracted INTEGER DEFAULT 0,
            latency_seconds REAL,
            started_at TEXT,
            completed_at TEXT
        );
        CREATE TABLE evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL,
            source_url TEXT NOT NULL,
            retrieval_time TEXT NOT NULL,
            publication_time TEXT,
            extraction_method TEXT NOT NULL,
            parser_version TEXT NOT NULL DEFAULT '1.0.0'
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_id INTEGER NOT NULL,
            registry_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            event_subtype TEXT,
            severity TEXT,
            confidence REAL NOT NULL DEFAULT 1.0,
            description TEXT,
            extracted_at TEXT NOT NULL DEFAULT (datetime('now')),
            source_claims_directly BOOLEAN NOT NULL DEFAULT 1,
            raw_event_json TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            tombstone_reason TEXT,
            tombstoned_at TEXT,
            UNIQUE(evidence_id, event_type, registry_id)
        );
        """
    )
    db.execute("INSERT INTO registry (id, canonical_name, status) VALUES (1, 'AlertCo', 'active')")
    db.execute(
        """INSERT INTO evidence
           (id, content_hash, source_url, retrieval_time, publication_time, extraction_method, parser_version)
           VALUES (1, 'hash1', 'https://example.test/filing', '2026-06-23T00:00:00Z',
                   '2026-06-23', 'companies_house_api', '1.0.0')"""
    )
    return db


def test_detect_new_adverse_events_filters_by_watermark_and_severity():
    from kassandra.daemon import detect_new_adverse_events

    db = _make_daemon_db()
    db.execute(
        """INSERT INTO events
           (id, evidence_id, registry_id, event_type, severity, confidence, description, raw_event_json)
           VALUES (1, 1, 1, 'profit_warning', 'medium', 0.9, 'old medium', NULL)"""
    )
    raw = json.dumps({"pattern_id": "P001", "matched_text": "filed for insolvency"})
    db.execute(
        """INSERT INTO events
           (id, evidence_id, registry_id, event_type, severity, confidence, description, raw_event_json)
           VALUES (2, 1, 1, 'insolvency', 'critical', 0.95, 'filed for insolvency', ?)""",
        (raw,),
    )
    db.commit()

    alerts = detect_new_adverse_events(db, since_event_id=1)
    assert len(alerts) == 1
    assert alerts[0]["company"] == "AlertCo"
    assert alerts[0]["event_type"] == "insolvency"
    assert alerts[0]["pattern_id"] == "P001"


def test_detect_new_adverse_events_excludes_tombstoned_events():
    from kassandra.daemon import detect_new_adverse_events

    db = _make_daemon_db()
    db.execute(
        """INSERT INTO events
           (id, evidence_id, registry_id, event_type, severity, confidence,
            description, active, status, tombstone_reason)
           VALUES (2, 1, 1, 'insolvency', 'critical', 0.99,
                   'false legal-name match', 0, 'tombstoned', 'entity mismatch')"""
    )
    db.commit()

    assert detect_new_adverse_events(db, since_event_id=1) == []


def test_format_alerts_includes_why_source_and_evidence():
    from kassandra.daemon import format_alerts

    message = format_alerts([
        {
            "event_id": 7,
            "company": "AlertCo",
            "event_type": "insolvency",
            "severity": "critical",
            "confidence": 0.95,
            "description": "filed for insolvency after lenders withdrew support",
            "pattern_id": "P001",
            "publication_time": "2026-06-23",
            "source": "companies_house_api",
            "evidence_id": 3,
            "source_url": "https://example.test/filing",
        }
    ])
    assert "Kassandra real-time adverse-event alert" in message
    assert "AlertCo" in message
    assert "Why:" in message
    assert "Evidence: `3`" in message
    assert "https://example.test/filing" in message


def test_run_alert_cycle_bootstraps_without_replaying_history(tmp_path: Path):
    from kassandra import daemon as daemon_mod

    db = _make_daemon_db()
    db.execute(
        """INSERT INTO events
           (id, evidence_id, registry_id, event_type, severity, confidence, description)
           VALUES (1, 1, 1, 'insolvency', 'critical', 0.95, 'historical insolvency')"""
    )
    db.commit()

    state_path = tmp_path / "daemon.json"
    with patch.object(daemon_mod, "get_db", return_value=db), \
         patch.object(daemon_mod, "migrate", return_value=10), \
         patch.object(daemon_mod, "run_collection", return_value={"companies_house": 0}), \
         patch.object(daemon_mod, "compute_scores", return_value=[]), \
         patch.object(daemon_mod, "_acquire_cycle_lock", return_value=True), \
         patch.object(daemon_mod, "_release_cycle_lock", return_value=None):
        result = daemon_mod.run_alert_cycle(state_path=state_path, collect=True)

    assert result["status"] == "ok"
    assert result["alerts"] == []
    state = json.loads(state_path.read_text())
    assert state["last_alerted_event_id"] == 1


def test_load_daemon_state_migrates_generated_alert_counter(tmp_path: Path):
    from kassandra.daemon import load_daemon_state

    state_path = tmp_path / "daemon.json"
    state_path.write_text(json.dumps({"alerts_sent": 7, "cycles_completed": 2}))

    state = load_daemon_state(state_path)

    assert state["alerts_generated"] == 7
    assert "alerts_sent" not in state
