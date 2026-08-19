"""Scoring correctness, provenance, and bounded-freshness regressions."""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from kassandra.db import SCHEMA_VERSION, migrate
from kassandra.scoring import (
    _build_score_input_manifest,
    _compute_signal_score,
    compute_scores,
)


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    migrate(db)
    db.execute(
        """INSERT INTO registry
           (id, canonical_name, isin, jurisdiction, company_type, status, domain)
           VALUES (1, 'Example SA', 'XS0000000001', 'FR', 'company', 'active', 'example.test')"""
    )
    db.execute("INSERT INTO portfolios (id, name) VALUES (1, 'Test portfolio')")
    db.execute(
        """INSERT INTO portfolio_items
           (portfolio_id, ticker, isin, name, sector, country, weight, source)
           VALUES (1, 'EXM', 'XS0000000001', 'Example SA', 'Test', 'FR', 1.0, 'test')"""
    )
    db.commit()
    return db


def _active_event(db: sqlite3.Connection, event_type: str, severity: str, confidence: float = 1.0) -> int:
    evidence_id = db.execute(
        """INSERT INTO evidence
           (content_hash, source_url, retrieval_time, publication_time,
            extraction_method, parser_version, source_reliability)
           VALUES (?, ?, ?, ?, 'test_source', 'test-v1', 1.0)""",
        (
            f"hash-{event_type}-{severity}-{confidence}",
            f"https://example.test/{event_type}",
            "2026-01-01T00:00:00+00:00",
            "2025-12-31T00:00:00+00:00",
        ),
    ).lastrowid
    return db.execute(
        """INSERT INTO events
           (evidence_id, registry_id, event_type, severity, confidence,
            extracted_at, active, status)
           VALUES (?, 1, ?, ?, ?, ?, 1, 'active')""",
        (evidence_id, event_type, severity, confidence, datetime.now(timezone.utc).isoformat()),
    ).lastrowid


def test_structured_event_rows_keep_type_severity_confidence_together():
    """A high insolvency remains high even when another event has lower severity."""
    score, factors = _compute_signal_score(
        [
            {"event_type": "insolvency_event", "severity": "high", "confidence": 0.9},
            {"event_type": "payment_stress", "severity": "low", "confidence": 0.2},
        ],
        {"insolvency_event": 10.0, "payment_stress": 6.0},
    )
    assert factors == {"insolvency_event": 54.0, "payment_stress": 1.2}
    assert score == 1.0


def test_manifest_is_deterministic_and_changes_for_active_input_only():
    db = _db()
    _active_event(db, "insolvency_event", "high")
    db.commit()

    first = _build_score_input_manifest(db, {"insolvency_event": 10.0})
    second = _build_score_input_manifest(db, {"insolvency_event": 10.0})
    assert first["fingerprint"] == second["fingerprint"]
    assert json.dumps(first["manifest"], sort_keys=True) == json.dumps(second["manifest"], sort_keys=True)

    db.execute("UPDATE events SET active = 0, status = 'tombstoned' WHERE id = 1")
    db.commit()
    changed = _build_score_input_manifest(db, {"insolvency_event": 10.0})
    assert changed["fingerprint"] != first["fingerprint"]
    assert changed["manifest"]["active_events"] == []
    assert compute_scores(db)[0]["event_count"] == 0
    assert compute_scores(db)[0]["active_watch_priority"] == 0.0


def test_changed_input_creates_snapshot_even_when_priority_is_unchanged():
    db = _db()
    compute_scores(db)
    first = db.execute("SELECT analyst_priority, input_fingerprint FROM scores WHERE registry_id = 1").fetchone()

    # Registry identity is a canonical scoring input; it changes provenance, not priority.
    db.execute("UPDATE registry SET raw_json = '{\"source\": \"refreshed\"}' WHERE id = 1")
    db.commit()
    compute_scores(db)
    snapshots = db.execute(
        "SELECT analyst_priority, input_fingerprint FROM scores WHERE registry_id = 1 ORDER BY id"
    ).fetchall()

    assert len(snapshots) == 2
    assert snapshots[0]["analyst_priority"] == snapshots[1]["analyst_priority"] == first["analyst_priority"]
    assert snapshots[0]["input_fingerprint"] != snapshots[1]["input_fingerprint"]


def test_source_run_freshness_is_grouped_once_and_entity_coverage_is_separate():
    db = _db()
    _active_event(db, "insolvency_event", "high")
    db.execute(
        """INSERT INTO source_runs (run_id, source_name, completed_at)
           VALUES ('run-1', 'test_source', '2026-01-02T00:00:00+00:00')"""
    )
    db.execute(
        """INSERT INTO source_runs (run_id, source_name, completed_at)
           VALUES ('run-2', 'test_source', '2026-01-03T00:00:00+00:00')"""
    )
    db.commit()

    statements: list[str] = []
    db.set_trace_callback(statements.append)
    compute_scores(db)
    db.set_trace_callback(None)
    manifest = json.loads(
        db.execute("SELECT provenance_json FROM scoring_runs").fetchone()[0]
    )

    source_run_queries = [s for s in statements if "source_runs" in s.lower()]
    assert len(source_run_queries) == 1
    assert "GROUP BY source_name" in source_run_queries[0]
    assert manifest["source_freshness"]["global"]["test_source"] == "2026-01-03T00:00:00+00:00"
    assert manifest["source_freshness"]["entity_coverage"]["1"] == ["test_source"]


def test_one_active_events_scan_per_compute_scores():
    """compute_scores must issue exactly one active-events query, not two."""
    db = _db()
    _active_event(db, "insolvency_event", "high")
    _active_event(db, "restructuring", "medium")
    db.commit()

    queries: list[str] = []
    db.set_trace_callback(lambda stmt: queries.append(stmt))
    compute_scores(db)
    db.set_trace_callback(None)

    # The old second-scan query selected specific columns from bare events table
    # with no JOIN: "SELECT id, registry_id, event_type, severity, confidence, extracted_at"
    old_second_scan_sig = "SELECT id, registry_id, event_type, severity, confidence, extracted_at"
    active_event_queries = [
        q for q in queries
        if old_second_scan_sig in q
        and "event_type NOT IN ('unconfirmed_review_candidate'" in q
    ]
    # This specific pattern was the old second scan in compute_scores.
    # Now that compute_scores reuses manifest rows, it should not appear.
    assert len(active_event_queries) == 0, (
        f"Expected 0 queries matching old second-scan pattern, "
        f"got {len(active_event_queries)}: {active_event_queries}"
    )

    # The manifest builder still has ONE active-events scan (with JOIN).
    manifest_scans = [
        q for q in queries
        if "FROM events e JOIN evidence ev" in q
        and "e.active = 1 AND e.status = 'active'" in q
        and "ORDER BY e.id" in q
    ]
    assert len(manifest_scans) == 1, (
        f"Expected exactly 1 manifest active-events scan, got {len(manifest_scans)}"
    )


def test_scoring_runs_single_manifest_per_run():
    """One scoring_runs row per compute_scores call, score rows NULL provenance."""
    db = _db()
    _active_event(db, "insolvency_event", "high")
    db.commit()

    compute_scores(db)
    runs = db.execute("SELECT * FROM scoring_runs").fetchall()
    assert len(runs) == 1, f"Expected 1 scoring_runs row, got {len(runs)}"
    assert runs[0]["provenance_json"] is not None
    assert runs[0]["scorer_version"] is not None
    assert runs[0]["input_fingerprint"] is not None

    scores = db.execute("SELECT provenance_json, run_id FROM scores").fetchall()
    for score in scores:
        assert score["provenance_json"] is None, (
            "Score row must not duplicate the full provenance manifest"
        )
        assert score["run_id"] == runs[0]["run_id"]

    # Second run with same inputs: INSERT OR IGNORE keeps one row
    compute_scores(db)
    runs2 = db.execute("SELECT * FROM scoring_runs").fetchall()
    assert len(runs2) == 1, "INSERT OR IGNORE must not create duplicate scoring_runs rows"


def test_nonpersistent_scoring_writes_no_provenance_or_scores():
    """Fast alert-cycle scoring must be read-only for analytical snapshots."""
    db = _db()
    _active_event(db, "insolvency_event", "high")
    db.commit()

    results = compute_scores(db, persist=False)

    assert results
    assert db.execute("SELECT COUNT(*) FROM scoring_runs").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0


def test_scoring_runs_idempotent_insert():
    """Multiple compute_scores calls with identical fingerprint don't duplicate."""
    db = _db()
    _active_event(db, "insolvency_event", "high")
    db.commit()

    compute_scores(db)
    compute_scores(db)
    compute_scores(db)
    runs = db.execute("SELECT * FROM scoring_runs").fetchall()
    assert len(runs) == 1, (
        f"INSERT OR IGNORE should yield 1 row, got {len(runs)}"
    )


def test_repeated_identical_runs_same_run_id():
    """Two compute_scores calls with identical inputs produce the same run_id."""
    db = _db()
    _active_event(db, "insolvency_event", "high")
    db.commit()
    compute_scores(db)
    runs1 = db.execute("SELECT run_id FROM scoring_runs").fetchone()["run_id"]

    # Second run with same inputs
    compute_scores(db)
    runs2 = db.execute("SELECT run_id FROM scoring_runs").fetchall()
    assert len(runs2) == 1, "Duplicate scoring_runs must not be created"
    assert runs2[0]["run_id"] == runs1, "Same inputs must produce same deterministic run_id"


def test_changed_input_changes_run_id():
    """Different inputs produce different deterministic run_ids."""
    db = _db()
    _active_event(db, "insolvency_event", "high")
    db.commit()
    compute_scores(db)
    run1 = db.execute("SELECT run_id FROM scoring_runs").fetchone()["run_id"]

    # Add a new event, changing the input manifest
    _active_event(db, "restructuring", "medium")
    db.commit()
    compute_scores(db)
    runs = db.execute("SELECT run_id FROM scoring_runs ORDER BY created_at").fetchall()
    assert len(runs) == 2
    assert runs[0]["run_id"] != runs[1]["run_id"], (
        "Changed inputs must produce a different deterministic run_id"
    )


def test_trigger_rejects_orphan_run_id_insert():
    """INSERT with non-null run_id not in scoring_runs must be rejected."""
    db = _db()
    # Try inserting a score referencing a non-existent scoring_runs row
    import pytest
    with pytest.raises(sqlite3.IntegrityError, match="provenance integrity"):
        db.execute("""
            INSERT INTO scores
            (registry_id, score_schema_version, analyst_priority, computed_at, run_id)
            VALUES (1, 3, 0.5, '2026-01-01T00:00:00Z', 'nonexistent-run-id')
        """)


def test_trigger_rejects_orphan_run_id_update():
    """UPDATE setting run_id to orphan must be rejected."""
    db = _db()
    _active_event(db, "insolvency_event", "high")
    db.commit()
    compute_scores(db)

    # Get an existing score row
    score = db.execute("SELECT id FROM scores WHERE registry_id = 1").fetchone()
    assert score is not None

    import pytest
    with pytest.raises(sqlite3.IntegrityError, match="provenance integrity"):
        db.execute(
            "UPDATE scores SET run_id = ? WHERE id = ?",
            ("nonexistent-run-id", score["id"]),
        )


def test_trigger_allows_null_run_id():
    """INSERT with NULL run_id is always allowed (no integrity check needed)."""
    db = _db()
    # Should not raise
    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at, run_id)
        VALUES (1, 3, 0.5, '2026-01-01T00:00:00Z', NULL)
    """)
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1


def test_compute_scores_run_id_references_existing_row():
    """compute_scores must insert a scoring_runs row before scores reference it."""
    db = _db()
    _active_event(db, "insolvency_event", "high")
    db.commit()
    compute_scores(db)

    # Verify: every score with non-null run_id references an existing scoring_runs row
    orphans = db.execute("""
        SELECT s.id, s.run_id FROM scores s
        WHERE s.run_id IS NOT NULL
        AND s.run_id NOT IN (SELECT run_id FROM scoring_runs)
    """).fetchall()
    assert len(orphans) == 0, f"Orphan run_ids found: {orphans}"


# ── Migration 18 provenance backfill tests ──────────────────────────────────


def _db_with_legacy_scores():
    """Create a DB at schema v17 with manually inserted legacy scores
    that have provenance_json, input_fingerprint, run_id, scorer_version
    but no scoring_runs rows.  Migration 18 triggers are NOT active yet.
    """
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    # Manually run migrations 1-17 without migration 18 (which creates triggers)
    from kassandra.db import MIGRATIONS, _reconcile_runtime_schema

    db.execute("""CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    for ver in sorted(MIGRATIONS.keys()):
        if ver > 17:
            break
        try:
            db.executescript(MIGRATIONS[ver])
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
        db.execute("INSERT INTO schema_version (version) VALUES (?)", (ver,))
    db.commit()

    # Add minimal registry + portfolio so scoring doesn't error
    db.execute(
        "INSERT INTO registry (id, canonical_name, isin, jurisdiction, company_type, status, domain) "
        "VALUES (1, 'TestCo', 'XX0000', 'XX', 'company', 'active', 'example.test')"
    )
    db.execute("INSERT INTO portfolios (id, name) VALUES (1, 'p')")
    db.execute(
        "INSERT INTO portfolio_items (portfolio_id, ticker, isin, name, sector, country, weight, source) "
        "VALUES (1, 'T', 'XX0000', 'TestCo', 'Tech', 'XX', 1.0, 'test')"
    )
    db.commit()
    return db


def test_migration_18_backfills_historical_provenance():
    """Scores with fingerprint + version + provenance get canonical scoring_runs rows."""
    db = _db_with_legacy_scores()

    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at,
         input_fingerprint, provenance_json, run_id, scorer_version)
        VALUES (1, 3, 0.5, '2026-01-01T00:00:00Z',
                'abc123fingerprint', '{"test": true}',
                'legacy-run-1', '3.0.0')
    """)
    db.commit()

    # Before migration 18: no scoring_runs rows
    assert db.execute("SELECT COUNT(*) FROM scoring_runs").fetchone()[0] == 0

    # Force the migration 18 backfill
    from kassandra.db import _migrate_18_scoring_provenance
    _migrate_18_scoring_provenance(db)
    db.commit()

    # After: one scoring_runs row
    runs = db.execute("SELECT * FROM scoring_runs").fetchall()
    assert len(runs) == 1
    assert runs[0]["run_id"] == "legacy-run-1"
    assert runs[0]["input_fingerprint"] == "abc123fingerprint"
    assert runs[0]["scorer_version"] == "3.0.0"
    assert runs[0]["provenance_json"] == '{"test": true}'


def test_migration_18_conflict_same_run_id_different_provenance():
    """Same run_id with incompatible fingerprint/version must fail loudly."""
    db = _db_with_legacy_scores()

    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at,
         input_fingerprint, run_id, scorer_version)
        VALUES (1, 3, 0.5, '2026-01-01T00:00:00Z',
                'fp-aaa', 'conflict-run', '3.0.0')
    """)
    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at,
         input_fingerprint, run_id, scorer_version)
        VALUES (1, 3, 0.6, '2026-01-02T00:00:00Z',
                'fp-bbb', 'conflict-run', '2.0.0')
    """)
    db.commit()

    import pytest
    from kassandra.db import _migrate_18_scoring_provenance
    with pytest.raises(sqlite3.IntegrityError, match="incompatible provenance"):
        _migrate_18_scoring_provenance(db)


def test_migration_18_conflict_same_fingerprint_different_run_ids():
    """Same fingerprint+version with different run_ids must fail."""
    db = _db_with_legacy_scores()

    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at,
         input_fingerprint, run_id, scorer_version)
        VALUES (1, 3, 0.5, '2026-01-01T00:00:00Z',
                'fp-xyz', 'run-alpha', '3.0.0')
    """)
    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at,
         input_fingerprint, run_id, scorer_version)
        VALUES (1, 3, 0.6, '2026-01-02T00:00:00Z',
                'fp-xyz', 'run-beta', '3.0.0')
    """)
    db.commit()

    import pytest
    from kassandra.db import _migrate_18_scoring_provenance
    with pytest.raises(sqlite3.IntegrityError, match="two different run_ids"):
        _migrate_18_scoring_provenance(db)


def test_migration_18_missing_fingerprint_creates_legacy_record():
    """Score with run_id but no fingerprint/version gets a legacy scoring_runs row
    and the score row is updated to reference it — no orphans."""
    db = _db_with_legacy_scores()

    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at,
         run_id)
        VALUES (1, 3, 0.5, '2026-01-01T00:00:00Z', 'orphan-run-no-fp')
    """)
    db.commit()

    from kassandra.db import _migrate_18_scoring_provenance
    _migrate_18_scoring_provenance(db)
    db.commit()

    runs = db.execute("SELECT * FROM scoring_runs").fetchall()
    assert len(runs) == 1
    assert runs[0]["run_id"].startswith("legacy-")
    assert "legacy-score" in runs[0]["input_fingerprint"]
    assert "unknown" in runs[0]["scorer_version"]

    # Score row must now reference the synthetic scoring_runs row
    score = db.execute("SELECT run_id, input_fingerprint, scorer_version FROM scores WHERE id = 1").fetchone()
    assert score["run_id"] == runs[0]["run_id"], "Score must reference synthetic run_id"
    assert score["input_fingerprint"] == runs[0]["input_fingerprint"]
    assert score["scorer_version"] == runs[0]["scorer_version"]

    # No orphan scores
    orphans = db.execute("""
        SELECT COUNT(*) FROM scores
        WHERE run_id IS NOT NULL
        AND run_id NOT IN (SELECT run_id FROM scoring_runs)
    """).fetchone()[0]
    assert orphans == 0, f"Found {orphans} orphan scores"


# ── Migration 18 atomicity, provenance backfill, and conflict tests ─────────


def _db_at_v17():
    """Create a fresh :memory: DB at schema v17 (no migration 18 yet)."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    from kassandra.db import MIGRATIONS

    db.execute("""CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    for ver in sorted(MIGRATIONS.keys()):
        if ver > 17:
            break
        try:
            db.executescript(MIGRATIONS[ver])
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
        db.execute("INSERT INTO schema_version (version) VALUES (?)", (ver,))
    db.commit()

    db.execute(
        "INSERT INTO registry (id, canonical_name, isin, jurisdiction, company_type, status, domain) "
        "VALUES (1, 'TestCo', 'XX0000', 'XX', 'company', 'active', 'example.test')"
    )
    db.execute("INSERT INTO portfolios (id, name) VALUES (1, 'p')")
    db.execute(
        "INSERT INTO portfolio_items (portfolio_id, ticker, isin, name, sector, country, weight, source) "
        "VALUES (1, 'T', 'XX0000', 'TestCo', 'Tech', 'XX', 1.0, 'test')"
    )
    db.commit()
    return db


def test_migration_18_atomic_rollback_on_conflict():
    """Conflict during backfill → full rollback; DB remains at v17, data unchanged."""
    db = _db_at_v17()

    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at,
         input_fingerprint, run_id, scorer_version)
        VALUES (1, 3, 0.5, '2026-01-01T00:00:00Z',
                'fp-aaa', 'conflict-run', '3.0.0')
    """)
    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at,
         input_fingerprint, run_id, scorer_version)
        VALUES (1, 3, 0.6, '2026-01-02T00:00:00Z',
                'fp-bbb', 'conflict-run', '2.0.0')
    """)
    db.commit()

    import pytest
    with pytest.raises(sqlite3.IntegrityError, match="incompatible provenance"):
        migrate(db)

    current = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert current == 17, f"Expected v17 after rollback, got v{current}"

    assert db.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 2

    triggers = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert "trg_scores_validate_run_id_insert" not in triggers
    assert "trg_scores_validate_run_id_update" not in triggers


def test_migration_18_rerun_after_fix_succeeds():
    """Fix the conflict, rerun migrate → succeeds at v18 with canonical row."""
    db = _db_at_v17()

    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at,
         input_fingerprint, run_id, scorer_version)
        VALUES (1, 3, 0.5, '2026-01-01T00:00:00Z',
                'fp-aaa', 'shared-run', '3.0.0')
    """)
    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at,
         input_fingerprint, run_id, scorer_version)
        VALUES (1, 3, 0.6, '2026-01-02T00:00:00Z',
                'fp-bbb', 'shared-run', '2.0.0')
    """)
    db.commit()

    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        migrate(db)
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 17

    # Fix: unify the second score's fingerprint and version
    db.execute(
        "UPDATE scores SET input_fingerprint = 'fp-aaa', scorer_version = '3.0.0' WHERE id = 2"
    )
    db.commit()

    v = migrate(db)
    assert v == SCHEMA_VERSION
    runs = db.execute("SELECT * FROM scoring_runs").fetchall()
    assert len(runs) == 1
    assert runs[0]["run_id"] == "shared-run"


def test_migration_18_clears_provenance_json():
    """Normal backfill copies provenance to scoring_runs, clears score blobs."""
    db = _db_with_legacy_scores()

    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at,
         input_fingerprint, provenance_json, run_id, scorer_version)
        VALUES (1, 3, 0.5, '2026-01-01T00:00:00Z',
                'fp-abc', '{"key": "value"}', 'run-1', 'v1')
    """)
    db.commit()

    from kassandra.db import _migrate_18_scoring_provenance
    _migrate_18_scoring_provenance(db)
    db.commit()

    runs = db.execute("SELECT * FROM scoring_runs").fetchall()
    assert len(runs) == 1
    assert runs[0]["provenance_json"] == '{"key": "value"}'

    score = db.execute("SELECT provenance_json FROM scores WHERE id = 1").fetchone()
    assert score["provenance_json"] is None, "Score provenance_json must be cleared"

    orphans = db.execute("""
        SELECT COUNT(*) FROM scores
        WHERE run_id IS NOT NULL
        AND run_id NOT IN (SELECT run_id FROM scoring_runs)
    """).fetchone()[0]
    assert orphans == 0


def test_migration_18_multiple_missing_metadata_updates_score_refs():
    """Multiple scores with missing fp/sv each get unique synthetic scoring_runs
    rows and are updated to reference them — zero orphans afterward."""
    db = _db_with_legacy_scores()

    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at, run_id)
        VALUES (1, 3, 0.5, '2026-01-01T00:00:00Z', 'old-run-1')
    """)
    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at, run_id)
        VALUES (1, 3, 0.6, '2026-01-02T00:00:00Z', 'old-run-1')
    """)
    db.commit()

    from kassandra.db import _migrate_18_scoring_provenance
    _migrate_18_scoring_provenance(db)
    db.commit()

    runs = db.execute("SELECT run_id, input_fingerprint, scorer_version FROM scoring_runs ORDER BY run_id").fetchall()
    assert len(runs) == 2
    assert runs[0]["run_id"] != runs[1]["run_id"], "Each legacy score gets its own scoring_runs row"

    scores = db.execute("SELECT id, run_id, input_fingerprint, scorer_version FROM scores ORDER BY id").fetchall()
    for score in scores:
        assert score["run_id"].startswith("legacy-")
        assert score["input_fingerprint"] is not None
        assert "unknown" in score["scorer_version"]

    orphans = db.execute("""
        SELECT COUNT(*) FROM scores
        WHERE run_id IS NOT NULL
        AND run_id NOT IN (SELECT run_id FROM scoring_runs)
    """).fetchone()[0]
    assert orphans == 0, f"Found {orphans} orphan scores"


def test_migration_18_same_keys_differing_provenance_fails():
    """Same (run_id, fingerprint, version) with different provenance_json → fail."""
    db = _db_with_legacy_scores()

    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at,
         input_fingerprint, run_id, scorer_version, provenance_json)
        VALUES (1, 3, 0.5, '2026-01-01T00:00:00Z',
                'fp-xyz', 'run-alpha', 'v1', '{"a": 1}')
    """)
    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at,
         input_fingerprint, run_id, scorer_version, provenance_json)
        VALUES (1, 3, 0.6, '2026-01-02T00:00:00Z',
                'fp-xyz', 'run-alpha', 'v1', '{"a": 2}')
    """)
    db.commit()

    import pytest
    from kassandra.db import _migrate_18_scoring_provenance
    with pytest.raises(sqlite3.IntegrityError, match="incompatible provenance_json"):
        _migrate_18_scoring_provenance(db)


def test_migration_18_preexisting_scoring_runs_different_fingerprint_fails():
    """Pre-existing scoring_runs row with different fingerprint for same run_id → fail."""
    db = _db_with_legacy_scores()

    db.execute("""
        INSERT INTO scoring_runs (run_id, input_fingerprint, scorer_version, provenance_json, created_at)
        VALUES ('run-1', 'fp-original', 'v1', '{"original": true}', '2026-01-01T00:00:00Z')
    """)
    db.commit()

    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at,
         input_fingerprint, run_id, scorer_version, provenance_json)
        VALUES (1, 3, 0.5, '2026-01-01T00:00:00Z',
                'fp-different', 'run-1', 'v2', '{"other": true}')
    """)
    db.commit()

    import pytest
    from kassandra.db import _migrate_18_scoring_provenance
    with pytest.raises(sqlite3.IntegrityError, match="different fingerprint"):
        _migrate_18_scoring_provenance(db)


def test_migration_18_preexisting_scoring_runs_different_provenance_fails():
    """Pre-existing scoring_runs with same keys but different provenance_json → fail."""
    db = _db_with_legacy_scores()

    db.execute("""
        INSERT INTO scoring_runs (run_id, input_fingerprint, scorer_version, provenance_json, created_at)
        VALUES ('run-1', 'fp-xyz', 'v1', '{"p": 1}', '2026-01-01T00:00:00Z')
    """)
    db.commit()

    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at,
         input_fingerprint, run_id, scorer_version, provenance_json)
        VALUES (1, 3, 0.5, '2026-01-01T00:00:00Z',
                'fp-xyz', 'run-1', 'v1', '{"p": 2}')
    """)
    db.commit()

    import pytest
    from kassandra.db import _migrate_18_scoring_provenance
    with pytest.raises(sqlite3.IntegrityError, match="different provenance_json"):
        _migrate_18_scoring_provenance(db)


def test_migration_18_preexisting_same_fpsv_different_rid_fails():
    """Pre-existing scoring_runs with same fp+sv but different run_id → fail."""
    db = _db_with_legacy_scores()

    db.execute("""
        INSERT INTO scoring_runs (run_id, input_fingerprint, scorer_version, provenance_json, created_at)
        VALUES ('run-existing', 'fp-xyz', 'v1', '{"p": 1}', '2026-01-01T00:00:00Z')
    """)
    db.commit()

    db.execute("""
        INSERT INTO scores
        (registry_id, score_schema_version, analyst_priority, computed_at,
         input_fingerprint, run_id, scorer_version, provenance_json)
        VALUES (1, 3, 0.5, '2026-01-01T00:00:00Z',
                'fp-xyz', 'run-different', 'v1', '{"p": 1}')
    """)
    db.commit()

    import pytest
    from kassandra.db import _migrate_18_scoring_provenance
    with pytest.raises(sqlite3.IntegrityError, match="existing run_id"):
        _migrate_18_scoring_provenance(db)


def test_repair_scoring_runs_shape_rejects_duplicate_run_ids():
    """Duplicate run_id in malformed scoring_runs → fail; don't silently drop."""
    db = _db_with_legacy_scores()

    db.execute("DROP TABLE IF EXISTS scoring_runs")
    db.execute("""
        CREATE TABLE scoring_runs (
            run_id TEXT,
            input_fingerprint TEXT NOT NULL,
            scorer_version TEXT NOT NULL,
            provenance_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    db.execute("INSERT INTO scoring_runs (run_id, input_fingerprint, scorer_version) VALUES ('dup-1', 'fp-a', 'v1')")
    db.execute("INSERT INTO scoring_runs (run_id, input_fingerprint, scorer_version) VALUES ('dup-1', 'fp-b', 'v2')")
    db.commit()

    from kassandra.db import _repair_scoring_runs_shape
    with pytest.raises(sqlite3.IntegrityError, match="duplicate run_id"):
        _repair_scoring_runs_shape(db)


def test_repair_scoring_runs_shape_rejects_duplicate_fingerprint_version():
    """Duplicate (fingerprint, version) → fail; don't silently drop."""
    db = _db_with_legacy_scores()

    db.execute("DROP TABLE IF EXISTS scoring_runs")
    db.execute("""
        CREATE TABLE scoring_runs (
            run_id TEXT,
            input_fingerprint TEXT,
            scorer_version TEXT,
            provenance_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    db.execute("INSERT INTO scoring_runs (run_id, input_fingerprint, scorer_version) VALUES ('r1', 'fp-dup', 'v1')")
    db.execute("INSERT INTO scoring_runs (run_id, input_fingerprint, scorer_version) VALUES ('r2', 'fp-dup', 'v1')")
    db.commit()

    from kassandra.db import _repair_scoring_runs_shape
    with pytest.raises(sqlite3.IntegrityError, match="duplicate.*fingerprint"):
        _repair_scoring_runs_shape(db)


def test_repair_scoring_runs_shape_rejects_null_run_id():
    """NULL run_id in scoring_runs → fail (PK cannot be NULL)."""
    db = _db_with_legacy_scores()

    db.execute("DROP TABLE IF EXISTS scoring_runs")
    db.execute("""
        CREATE TABLE scoring_runs (
            run_id TEXT,
            input_fingerprint TEXT NOT NULL,
            scorer_version TEXT NOT NULL,
            provenance_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    db.execute("INSERT INTO scoring_runs (run_id, input_fingerprint, scorer_version) VALUES (NULL, 'fp-a', 'v1')")
    db.commit()

    from kassandra.db import _repair_scoring_runs_shape
    with pytest.raises(sqlite3.IntegrityError, match="NULL run_id"):
        _repair_scoring_runs_shape(db)
