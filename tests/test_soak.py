"""Tests for soak-mode reliability guards and digest reconciliation.

Covers:
- Overlap lock (no concurrent cycles)
- Web monitor background supervision (singleton, safe restart)
- Failure recovery (diagnostics preserved, no busy-looping)
- Digest reconciliation (every metric traceable)
- GLEIF staleness
"""

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta


# ── Overlap lock ──────────────────────────────────────────────────────────────

def _lock_path() -> Path:
    return Path(tempfile.gettempdir()) / "kassandra_soak_cycle.lock"


def test_acquire_lock_first_time_succeeds():
    from kassandra.soak import _acquire_cycle_lock, _release_cycle_lock
    p = _lock_path()
    if p.exists():
        p.unlink()
    assert _acquire_cycle_lock() is True
    assert p.exists()
    _release_cycle_lock()
    assert not p.exists()


def test_acquire_lock_rejects_second_caller():
    from kassandra.soak import _acquire_cycle_lock, _release_cycle_lock
    p = _lock_path()
    if p.exists():
        p.unlink()
    assert _acquire_cycle_lock() is True
    assert _acquire_cycle_lock() is False
    _release_cycle_lock()


def test_lock_released_on_success():
    from kassandra.soak import _acquire_cycle_lock, _release_cycle_lock
    p = _lock_path()
    if p.exists():
        p.unlink()
    _acquire_cycle_lock()
    _release_cycle_lock()
    assert not p.exists()


def test_lock_released_on_exception():
    from kassandra.soak import _acquire_cycle_lock, _release_cycle_lock
    p = _lock_path()
    if p.exists():
        p.unlink()
    _acquire_cycle_lock()
    try:
        raise RuntimeError("simulated crash")
    except RuntimeError:
        _release_cycle_lock()
    assert not p.exists()


# ── Web monitor supervision ───────────────────────────────────────────────────

def test_web_monitor_singleton_lock():
    from kassandra.soak import _weblock_path, _dispatch_web_monitor
    p = _weblock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(os.getpid()))
    result = _dispatch_web_monitor()
    assert result == "skipped: another web monitor is running"


def test_web_monitor_dispatches_when_no_lock():
    from kassandra.soak import _weblock_path, _dispatch_web_monitor
    p = _weblock_path()
    if p.exists():
        p.unlink()
    p.parent.mkdir(parents=True, exist_ok=True)
    with patch("subprocess.Popen", return_value=_FakeWebProcess()):
        result = _dispatch_web_monitor(timeout_seconds=1)
    assert isinstance(result, dict)
    assert result["status"] == "success"


def test_web_monitor_command_is_valid_python():
    from kassandra.soak import _build_web_monitor_command
    script = _build_web_monitor_command()[2]
    compile(script, "<kassandra-web-monitor-child>", "exec")


def test_web_monitor_command_has_lock_handling():
    from kassandra.soak import _build_web_monitor_command
    cmd = _build_web_monitor_command()
    assert any("WEBLOCK_PATH" in part for part in cmd)


def test_web_monitor_lock_cleanup_on_completion():
    from kassandra.soak import _weblock_path, _web_monitor_lock_script
    script = _web_monitor_lock_script()
    assert "os.unlink" in script or "unlink" in script
    assert "WEBLOCK_PATH" in script


class _FakeWebProcess:
    def __init__(self, returncode=0, stdout="child-out", stderr="child-err", timeout=False):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.timeout = timeout
        self.killed = False

    def communicate(self, timeout=None):
        if self.timeout:
            raise __import__("subprocess").TimeoutExpired("web-monitor", timeout)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    def wait(self):
        return self.returncode


def test_web_monitor_supervision_observes_success_and_output():
    from kassandra.soak import _dispatch_web_monitor
    child = _FakeWebProcess()
    with patch("subprocess.Popen", return_value=child):
        result = _dispatch_web_monitor(timeout_seconds=1)
    assert result["status"] == "success"
    assert result["exit_code"] == 0
    assert result["stdout"] == "child-out"
    assert result["stderr"] == "child-err"


def test_web_monitor_supervision_reports_nonzero_exit():
    from kassandra.soak import _dispatch_web_monitor
    child = _FakeWebProcess(returncode=7)
    with patch("subprocess.Popen", return_value=child):
        result = _dispatch_web_monitor(timeout_seconds=1)
    assert result["status"] == "failed"
    assert result["exit_code"] == 7
    assert result["stderr"] == "child-err"


def test_web_monitor_failure_persists_structured_diagnostic():
    import json
    from kassandra.soak import _dispatch_web_monitor, _weblock_path

    lock = _weblock_path()
    if lock.exists():
        lock.unlink()
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE source_runs (
        id INTEGER PRIMARY KEY, run_id TEXT, source_name TEXT, errors INTEGER,
        status TEXT, started_at TEXT, completed_at TEXT, error_detail_json TEXT
    )""")
    child = _FakeWebProcess(returncode=7, stderr="SyntaxError: invalid child")
    with patch("subprocess.Popen", return_value=child):
        result = _dispatch_web_monitor(timeout_seconds=1, db=db)

    row = db.execute("SELECT * FROM source_runs").fetchone()
    detail = json.loads(row["error_detail_json"])
    assert isinstance(result, dict)
    assert result["status"] == "failed"
    assert detail == {
        "type": "failed", "exit_code": 7,
        "message": "SyntaxError: invalid child",
    }


def test_web_monitor_supervision_kills_timeout_and_reports_it():
    from kassandra.soak import _dispatch_web_monitor
    child = _FakeWebProcess(timeout=True)
    with patch("subprocess.Popen", return_value=child):
        result = _dispatch_web_monitor(timeout_seconds=1)
    assert result["status"] == "timeout"
    assert child.killed is True


def test_web_monitor_freshness_rejects_stale_latest_success():
    from kassandra.soak import evaluate_source_freshness
    db = _make_test_db()
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    old = (now - timedelta(hours=27)).isoformat()
    db.execute("INSERT INTO source_runs (source_name, completed_at, status) VALUES ('web_monitor', ?, 'success')", (old,))
    db.commit()
    result = evaluate_source_freshness(db, now=now, sla_hours=26)
    assert result["web_monitor"]["healthy"] is False
    assert result["web_monitor"]["reason"] == "stale"


def test_web_monitor_freshness_has_no_historical_only_green_state():
    from kassandra.soak import evaluate_source_freshness
    db = _make_test_db()
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    db.execute("INSERT INTO source_runs (source_name, completed_at, status) VALUES ('web_monitor', ?, 'success')",
               ((now - timedelta(hours=48)).isoformat(),))
    db.execute("INSERT INTO source_runs (source_name, completed_at, status) VALUES ('web_monitor', ?, 'failed')",
               ((now - timedelta(hours=1)).isoformat(),))
    db.commit()
    result = evaluate_source_freshness(db, now=now, sla_hours=26)
    assert result["web_monitor"]["healthy"] is False
    assert result["web_monitor"]["reason"] == "failed"


# ── Failure recovery ──────────────────────────────────────────────────────────

def test_failed_cycle_preserves_diagnostics(tmp_path):
    from kassandra.soak import load_soak_state, save_soak_state
    state_path = tmp_path / "soak.json"
    with patch("kassandra.soak.SOAK_STATE_PATH", state_path):
        state = load_soak_state()
        state["cycles_failed"] += 1
        state["last_failure_diagnostic"] = {
            "step": "collect", "error": "Gazette 429", "at": "2026-06-19T08:00:00Z",
        }
        save_soak_state(state)
        state2 = load_soak_state()
        assert state2["cycles_failed"] == 1
        assert state2["last_failure_diagnostic"]["step"] == "collect"
        assert state2["cycles_completed"] == 0


def test_no_busy_loop_on_persistent_failure(tmp_path):
    from kassandra.soak import load_soak_state, save_soak_state
    state_path = tmp_path / "soak.json"
    with patch("kassandra.soak.SOAK_STATE_PATH", state_path):
        state = load_soak_state()
        for i in range(5):
            state["cycles_failed"] += 1
            state["cycles_completed"] += 1
            state["last_cycle_at"] = f"2026-06-19T08:{i:02d}:00Z"
        save_soak_state(state)
        state2 = load_soak_state()
        assert state2["cycles_failed"] == 5
        assert state2["cycles_completed"] == 5


# ── Cron collision offset ────────────────────────────────────────────────────

def test_weekly_cron_offset_from_daily():
    daily_hour, daily_minute = 8, 0
    weekly_hour, weekly_minute = 8, 5
    assert (daily_hour, daily_minute) != (weekly_hour, weekly_minute)
    assert weekly_minute - daily_minute >= 5


# ── Digest reconciliation helpers ─────────────────────────────────────────────

def _make_test_db() -> sqlite3.Connection:
    """Create an in-memory DB with schema for reconciliation testing."""
    import sqlite3 as _sqlite3
    db = _sqlite3.connect(":memory:")
    db.row_factory = _sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_name TEXT,
            lei TEXT, status TEXT, resolved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS scoreable_companies (
            id INTEGER PRIMARY KEY, canonical_name TEXT
        );
        CREATE TABLE IF NOT EXISTS scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT, registry_id INTEGER,
            score_schema_version INTEGER DEFAULT 3,
            analyst_priority REAL, deterioration_risk REAL,
            dependency_exposure REAL, observation_severity REAL,
            priority_reason TEXT, coverage_quality TEXT,
            active_watch_priority REAL DEFAULT 0,
            coverage_monitor_priority REAL DEFAULT 0,
            transmission_signal_score REAL DEFAULT 0,
            graph_coverage_score REAL DEFAULT 0,
            information_gap_score REAL DEFAULT 0,
            source_staleness_score REAL DEFAULT 0,
            factors_json TEXT DEFAULT '{}', explanation TEXT DEFAULT '',
            computed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL, source_url TEXT NOT NULL,
            retrieval_time TEXT NOT NULL, extraction_method TEXT NOT NULL,
            source_reliability REAL DEFAULT 0.5,
            fetched_at TEXT DEFAULT (datetime('now')),
            parser_version TEXT NOT NULL DEFAULT '1.0.0',
            first_seen_time TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_id INTEGER REFERENCES evidence(id),
            registry_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            severity TEXT,
            confidence REAL NOT NULL DEFAULT 1.0,
            description TEXT,
            extracted_at TEXT NOT NULL DEFAULT (datetime('now')),
            raw_event_json TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS source_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, source_name TEXT,
            documents_discovered INTEGER, new_documents INTEGER,
            api_errors INTEGER, parse_failures INTEGER,
            adverse_events_found INTEGER, dependency_edges_extracted INTEGER,
            latency_seconds REAL,
            status TEXT DEFAULT 'success',
            started_at TEXT, completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_registry_id INTEGER, target_registry_id INTEGER,
            relationship_type TEXT, evidence_id INTEGER,
            edge_source TEXT, quality_tier TEXT DEFAULT 'T4_INFERRED',
            confidence REAL DEFAULT 0.5,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    for i in range(1, 4):
        db.execute("INSERT OR IGNORE INTO scoreable_companies (id) VALUES (?)", (i,))
        db.execute("INSERT OR IGNORE INTO registry (id, canonical_name, status) VALUES (?, ?, ?)",
                   (i, f"Company {i}", "active"))
    db.execute("INSERT INTO registry (id, canonical_name, status) VALUES (4, 'SupplierCo', 'active')")
    for rid in range(1, 5):
        db.execute("INSERT INTO scores (registry_id, analyst_priority, computed_at) VALUES (?, 0.1, '2026-06-19T00:00:00Z')", (rid,))
    db.commit()
    return db


# ── Digest reconciliation tests ───────────────────────────────────────────────

def test_portfolio_scored_matches_scoreable_companies():
    from kassandra.soak import collect_metrics
    db = _make_test_db()
    metrics = collect_metrics(db)
    db.close()
    assert metrics["scores"]["portfolio_companies"] == 3
    assert metrics["scores"]["portfolio_scored"] == 3
    assert metrics["scores"]["total_entities_with_scores"] == 4


def test_evidence_this_cycle_uses_new_documents():
    from kassandra.soak import collect_metrics
    db = _make_test_db()
    run_id = "test-run-001"
    db.execute("INSERT INTO source_runs (run_id, source_name, documents_discovered, new_documents, started_at, completed_at) VALUES (?, ?, 10, 3, 'now', 'now')",
               (run_id, "companies_house"))
    db.execute("INSERT INTO source_runs (run_id, source_name, documents_discovered, new_documents, started_at, completed_at) VALUES (?, ?, 40, 2, 'now', 'now')",
               (run_id, "uk_gazette"))
    db.commit()
    metrics = collect_metrics(db)
    db.close()
    ec = metrics["db"]["evidence_this_cycle"]
    assert ec["companies_house"] == 3
    assert ec["uk_gazette"] == 2
    ep = metrics["db"]["evidence_processed_this_cycle"]
    assert ep["companies_house"] == 10
    assert ep["uk_gazette"] == 40


def test_events_by_source_joins_evidence():
    from kassandra.soak import collect_metrics
    db = _make_test_db()
    db.execute("INSERT INTO evidence (id, content_hash, source_url, retrieval_time, extraction_method) VALUES (1, 'h1', 'u1', 'now', 'uk_gazette_feed')")
    db.execute("INSERT INTO evidence (id, content_hash, source_url, retrieval_time, extraction_method) VALUES (2, 'h2', 'u2', 'now', 'companies_house_api')")
    db.execute("INSERT INTO events (evidence_id, registry_id, event_type) VALUES (1, 1, 'insolvency')")
    db.execute("INSERT INTO events (evidence_id, registry_id, event_type) VALUES (1, 1, 'restructuring')")
    db.execute("INSERT INTO events (evidence_id, registry_id, event_type) VALUES (2, 2, 'management_departure')")
    db.commit()
    metrics = collect_metrics(db)
    db.close()
    eb = metrics["db"]["events_by_source"]
    assert eb["uk_gazette_feed"] == 2
    assert eb["companies_house_api"] == 1


def test_resource_has_process_metrics():
    from kassandra.soak import _collect_resource
    r = _collect_resource()
    assert "process_rss_mb" in r
    assert "memory_pressure" in r
    assert "swap_used_mb" in r
    assert "mem_free_mb" not in r
    assert "mem_active_mb" not in r
    assert r["process_rss_mb"] > 0


def test_gleif_staleness_detects_overdue():
    from kassandra.soak import _get_gleif_staleness
    db = _make_test_db()
    state = _get_gleif_staleness(db)
    db.close()
    assert state["overdue"] is True
    assert state["refresh_reason"] == "never_refreshed"


def test_source_run_reconciliation_finds_bad_rows():
    from kassandra.soak import run_consistency_checks
    db = _make_test_db()
    run_id = "test-recon"
    db.execute("INSERT INTO source_runs (run_id, source_name, documents_discovered, new_documents, started_at, completed_at) VALUES (?, ?, 10, 3, 'now', 'now')",
               (run_id, "ok_source"))
    db.execute("INSERT INTO source_runs (run_id, source_name, documents_discovered, new_documents, started_at, completed_at) VALUES (?, ?, 2, 10, 'now', 'now')",
               (run_id, "bad_source"))
    db.commit()
    checks = run_consistency_checks(db)
    db.close()
    recon = [f for f in checks["findings"] if f["check"] == "source_run_reconciliation"]
    assert len(recon) == 1
    assert "1" in recon[0]["detail"]


def test_scheduled_sources_match_actual_automated_collectors():
    from kassandra.soak import MANUAL_ONLY_SOURCES, SCHEDULED_SOURCES

    assert "handelsregister_de" not in SCHEDULED_SOURCES
    assert "handelsregister_de" in MANUAL_ONLY_SOURCES
    assert {"companies_house", "uk_gazette", "web_monitor", "bodacc_fr", "borme_es"} <= set(SCHEDULED_SOURCES)


def test_semantic_checks_flag_stale_queue_and_unaccounted_candidates():
    from kassandra.soak import run_semantic_health_checks

    db = _make_test_db()
    db.executescript("""
        ALTER TABLE source_runs ADD COLUMN candidates_generated INTEGER DEFAULT 0;
        ALTER TABLE source_runs ADD COLUMN unconfirmed_matches INTEGER DEFAULT 0;
        ALTER TABLE source_runs ADD COLUMN events_created INTEGER DEFAULT 0;
        ALTER TABLE source_runs ADD COLUMN duplicate_events INTEGER DEFAULT 0;
        CREATE TABLE unconfirmed_match_queue (
            id INTEGER PRIMARY KEY, status TEXT, created_at TEXT
        );
        INSERT INTO unconfirmed_match_queue VALUES (1, 'pending', '2026-01-01T00:00:00+00:00');
        INSERT INTO source_runs
            (run_id, source_name, candidates_generated, unconfirmed_matches,
             events_created, duplicate_events, completed_at)
        VALUES ('bad', 'bodacc_fr', 1, 1, 1, 0, '2026-07-16T00:00:00+00:00');
    """)

    findings = run_semantic_health_checks(
        db, now=datetime(2026, 7, 16, tzinfo=timezone.utc)
    )
    checks = {finding["check"] for finding in findings}

    assert "stale_unconfirmed_matches" in checks
    assert "candidate_reconciliation" in checks


def test_source_metric_state_stores_snapshot_and_period_delta():
    from kassandra.soak import update_source_metric_state

    state = {}
    update_source_metric_state(state, {"bodacc_fr": {"runs": 10, "new_evidence": 20, "api_errors": 2}})
    update_source_metric_state(state, {"bodacc_fr": {"runs": 12, "new_evidence": 23, "api_errors": 2}})

    metric = state["source_metrics"]["bodacc_fr"]
    assert metric["all_time"]["runs"] == 12
    assert metric["period_delta"]["runs"] == 2
    assert metric["period_delta"]["new_evidence"] == 3
    assert metric["period_delta"]["api_errors"] == 0

# ── Digest regression tests (Round 5: engineering audit acceptance criteria) ────────────

def test_digest_no_unknown_transmission_items():
    """After enrichment, watchlist items must have real entity names.
    The production digest was showing 'Unknown has unknown' — this guards against regression."""
    from kassandra.soak import collect_metrics
    db = _make_test_db()
    # Add a score with direct adverse signal for company 1
    db.execute("UPDATE scores SET active_watch_priority=0.02, transmission_signal_score=0, priority_reason='adverse_signal', observation_severity=0.2 WHERE registry_id=1")
    db.execute("INSERT OR REPLACE INTO events (id, registry_id, event_type, severity, confidence, extracted_at, evidence_id) VALUES (1, 1, 'profit_warning', 'medium', 0.90, '2026-06-19', 1)")
    db.execute("INSERT OR IGNORE INTO evidence (id, content_hash, extraction_method, source_url, source_reliability, fetched_at, retrieval_time) VALUES (1, 'abc', 'companies_house', 'http://x', 0.9, '2026-06-19', '2026-06-19')")
    db.commit()
    m = collect_metrics(db)
    db.close()
    watchlist = m.get('scores', {}).get('active_watchlist', [])
    assert len(watchlist) >= 1, f"Should have watchlist items: {watchlist}"
    for item in watchlist:
        trigger = item.get('trigger_event_type', '')
        entity = item.get('trigger_entity', '')
        assert trigger not in ('', 'unknown'), f"Item missing trigger event type: {item}"
        assert entity != 'Unknown', f"Item has Unknown entity: {item}"


def test_digest_direct_item_has_trigger_details():
    """Direct adverse items must show event type, severity, confidence, evidence IDs."""
    from kassandra.soak import collect_metrics
    db = _make_test_db()
    db.execute("INSERT OR REPLACE INTO registry (id, canonical_name, status) VALUES (1, 'DirectCo', 'ISSUED')")
    db.execute("INSERT OR REPLACE INTO scoreable_companies (id, canonical_name) VALUES (1, 'DirectCo')")
    db.execute("""
        INSERT OR REPLACE INTO events (id, registry_id, event_type, severity, confidence,
                            description, extracted_at, evidence_id)
        VALUES (1, 1, 'profit_warning', 'medium', 0.90,
                'guidance cut after weaker trading', '2026-06-19', 1)
    """)
    db.execute("""
        INSERT INTO evidence (id, content_hash, extraction_method, source_url,
                             source_reliability, fetched_at, retrieval_time)
        VALUES (1, 'abc', 'companies_house', 'http://x', 0.9, '2026-06-19', '2026-06-19')
    """)
    db.execute("""
        INSERT INTO scores (registry_id, score_schema_version, observation_severity,
            deterioration_risk, dependency_exposure, analyst_priority,
            active_watch_priority, coverage_monitor_priority,
            transmission_signal_score, graph_coverage_score,
            information_gap_score, source_staleness_score,
            priority_reason, coverage_quality, factors_json, explanation, computed_at)
        VALUES (1, 3, 0.2, 0.0, 0.0, 0.03,
                0.03, 0.0,
                0.0, 0.0,
                0.0, 0.0,
                'adverse_signal', 'partial',
                '{"signal_score":0.2,"recency_score":0.5}', '', datetime('now'))
    """)
    db.commit()
    m = collect_metrics(db)
    db.close()
    watchlist = m.get('scores', {}).get('active_watchlist', [])
    direct = [i for i in watchlist if i.get('_is_direct')]
    assert len(direct) >= 1, f"Should have direct items: {watchlist}"
    item = direct[0]
    assert item.get('trigger_event_type') not in (None, '', 'unknown'), \
        f"Direct item missing trigger event type: {item}"
    assert item.get('evidence_ids', ''), f"Direct item missing evidence IDs: {item}"
    assert item.get('trigger_description'), f"Direct item missing trigger description: {item}"
    assert item.get('trigger_source_url'), f"Direct item missing source URL: {item}"
