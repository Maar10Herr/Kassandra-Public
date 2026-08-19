"""Tests for Kassandra shared correctness contracts and migration integrity.

Covers:
  - source_runs table (migration 11)
  - economic_entities.registry_id column (migration 11)
  - scoreable_companies view (migration 11)
  - unconfirmed_match_queue table (migration 12)
  - event lifecycle columns (migration 12)
  - score provenance columns (migration 12)
  - Clean bootstrap vs upgrade equivalence
  - Migration idempotency
  - Typed results: EvidenceResult, EventResult
  - CollectionMetrics type
"""

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest

from kassandra.db import _connect, migrate, SCHEMA_VERSION, MIGRATIONS


# ── Helpers ───────────────────────────────────────────────────────────────────

def _temp_db():
    """Create a temporary on-disk SQLite database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    try:
        conn = _connect(path)
        yield conn
        conn.close()
    finally:
        path.unlink(missing_ok=True)
        path.with_suffix(".db-wal").unlink(missing_ok=True)
        path.with_suffix(".db-shm").unlink(missing_ok=True)


def _fresh_migrated_db():
    """Return a freshly migrated in-memory db."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    migrate(db)
    return db


def _table_columns(db, table_name):
    """Return set of column names for a table."""
    rows = db.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {r["name"] for r in rows}


def _view_exists(db, view_name):
    """Check if a view exists."""
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='view' AND name=?",
        (view_name,),
    ).fetchone()
    return row is not None


def _migrate_to_version(db, version):
    """Apply migrations up to and including `version`."""
    db.execute("""CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    db.commit()
    current = db.execute(
        "SELECT MAX(version) FROM schema_version"
    ).fetchone()[0] or 0
    for v in sorted(MIGRATIONS.keys()):
        if v > current and v <= version:
            try:
                db.executescript(MIGRATIONS[v])
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise
            db.execute("INSERT INTO schema_version (version) VALUES (?)", (v,))
            db.commit()


# ── RED tests for missing structures ──────────────────────────────────────────

class TestSourceRunsTableExists:
    """Migration 11 must create the source_runs table."""

    def test_source_runs_table_after_fresh_migration(self):
        """RED: source_runs table MUST exist after a clean bootstrap."""
        db = _fresh_migrated_db()
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t["name"] for t in tables}
        db.close()
        assert "source_runs" in table_names, (
            "source_runs table missing from clean migration"
        )

    def test_source_runs_columns(self):
        """Verify source_runs has all required columns."""
        db = _fresh_migrated_db()
        cols = _table_columns(db, "source_runs")
        db.close()
        required = {
            "id", "run_id", "source_name",
            "documents_discovered", "documents_fetched",
            "new_documents", "duplicate_documents",
            "candidates_generated", "unconfirmed_matches",
            "events_created", "duplicate_events",
            "errors", "api_errors", "parse_failures",
            "adverse_events_found", "dependency_edges_extracted",
            "latency_seconds",
            "started_at", "completed_at",
        }
        missing = required - cols
        assert not missing, f"source_runs missing columns: {missing}"


class TestEconomicEntitiesRegistryId:
    """Migration 11 must add registry_id to economic_entities."""

    def test_registry_id_column_exists(self):
        """RED: economic_entities.registry_id must exist."""
        db = _fresh_migrated_db()
        cols = _table_columns(db, "economic_entities")
        db.close()
        assert "registry_id" in cols, (
            "economic_entities.registry_id missing from clean migration"
        )


class TestScoreableCompaniesView:
    """Migration 11 must create the scoreable_companies view."""

    def test_view_exists_after_migration(self):
        """RED: scoreable_companies view must exist."""
        db = _fresh_migrated_db()
        exists = _view_exists(db, "scoreable_companies")
        db.close()
        assert exists, "scoreable_companies view missing from clean migration"

    def test_view_excludes_economic_concepts(self):
        """The view must exclude economic_concept company types."""
        db = _fresh_migrated_db()
        # Insert test data
        db.execute("INSERT INTO registry (id, canonical_name, company_type, domain) VALUES (1, 'PortfolioCo', NULL, 'pc.com')")
        db.execute("INSERT INTO registry (id, canonical_name, company_type, domain) VALUES (2, 'EconConcept', 'economic_concept', 'ec.com')")
        db.execute("INSERT INTO registry (id, canonical_name, company_type) VALUES (3, 'NoDomainCo', NULL)")
        db.commit()
        rows = db.execute("SELECT id FROM scoreable_companies").fetchall()
        ids = {r["id"] for r in rows}
        db.close()
        assert 1 in ids, "Portfolio company should be in view"
        assert 2 not in ids, "economic_concept should NOT be in view"
        assert 3 not in ids, "non-domain company should NOT be in view"


# ── RED tests for lifecycle/provenance structures (migration 12) ──────────────

class TestUnconfirmedMatchQueue:
    """Migration 12 must create the unconfirmed_match_queue table."""

    def test_table_exists(self):
        """RED: unconfirmed_match_queue table must exist."""
        db = _fresh_migrated_db()
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t["name"] for t in tables}
        db.close()
        assert "unconfirmed_match_queue" in table_names, (
            "unconfirmed_match_queue missing from clean migration"
        )

    def test_columns(self):
        """Verify unconfirmed_match_queue schema."""
        db = _fresh_migrated_db()
        cols = _table_columns(db, "unconfirmed_match_queue")
        db.close()
        required = {
            "id", "source_name", "source_entity_name",
            "candidate_registry_id", "candidate_registry_name",
            "match_type", "match_confidence",
            "evidence_id", "evidence_excerpt",
            "status", "reviewed_by", "reviewed_at",
            "resolution", "resolution_registry_id",
            "created_at", "updated_at",
        }
        missing = required - cols
        assert not missing, f"unconfirmed_match_queue missing columns: {missing}"


class TestEventLifecycleColumns:
    """Migration 12 must add active/status/tombstone columns to events."""

    def test_active_column_exists(self):
        """RED: events.active must exist."""
        db = _fresh_migrated_db()
        cols = _table_columns(db, "events")
        db.close()
        assert "active" in cols, "events.active missing"

    def test_status_column_exists(self):
        """RED: events.status must exist."""
        db = _fresh_migrated_db()
        cols = _table_columns(db, "events")
        db.close()
        assert "status" in cols, "events.status missing"

    def test_tombstone_reason_column_exists(self):
        """RED: events.tombstone_reason must exist."""
        db = _fresh_migrated_db()
        cols = _table_columns(db, "events")
        db.close()
        assert "tombstone_reason" in cols, "events.tombstone_reason missing"

    def test_tombstoned_at_column_exists(self):
        """RED: events.tombstoned_at must exist."""
        db = _fresh_migrated_db()
        cols = _table_columns(db, "events")
        db.close()
        assert "tombstoned_at" in cols, "events.tombstoned_at missing"

    def test_default_active_is_true(self):
        """New events default to active=1."""
        db = _fresh_migrated_db()
        db.execute("INSERT INTO registry (canonical_name) VALUES ('TestCo')")
        reg_id = db.execute("SELECT id FROM registry").fetchone()["id"]
        db.execute(
            "INSERT INTO evidence (content_hash, source_url, retrieval_time, extraction_method, parser_version) "
            "VALUES ('hash1', 'http://x.com', datetime('now'), 'test', '1.0')"
        )
        ev_id = db.execute("SELECT id FROM evidence").fetchone()["id"]
        db.execute(
            "INSERT INTO events (evidence_id, registry_id, event_type) VALUES (?, ?, ?)",
            (ev_id, reg_id, "insolvency"),
        )
        db.commit()
        row = db.execute("SELECT active, status, tombstone_reason, tombstoned_at FROM events WHERE id=?", (db.execute("SELECT last_insert_rowid()").fetchone()[0],)).fetchone()
        db.close()
        assert row["active"] == 1
        assert row["status"] == "active"
        assert row["tombstone_reason"] is None
        assert row["tombstoned_at"] is None


class TestScoreProvenanceColumns:
    """Migration 12 must add provenance columns to scores."""

    def test_input_fingerprint_exists(self):
        """RED: scores.input_fingerprint must exist."""
        db = _fresh_migrated_db()
        cols = _table_columns(db, "scores")
        db.close()
        assert "input_fingerprint" in cols, "scores.input_fingerprint missing"

    def test_provenance_json_exists(self):
        """RED: scores.provenance_json must exist."""
        db = _fresh_migrated_db()
        cols = _table_columns(db, "scores")
        db.close()
        assert "provenance_json" in cols, "scores.provenance_json missing"

    def test_run_id_exists(self):
        """RED: scores.run_id must exist."""
        db = _fresh_migrated_db()
        cols = _table_columns(db, "scores")
        db.close()
        assert "run_id" in cols, "scores.run_id missing"

    def test_scorer_version_exists(self):
        """RED: scores.scorer_version must exist."""
        db = _fresh_migrated_db()
        cols = _table_columns(db, "scores")
        db.close()
        assert "scorer_version" in cols, "scores.scorer_version missing"


# ── Migration idempotency tests ───────────────────────────────────────────────

class TestMigrationIdempotency:
    """Double-migration must be safe."""

    def test_fresh_migrate_twice(self):
        """Running migrate twice on a clean DB gives same version."""
        db = _fresh_migrated_db()
        v1 = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        # Migrate again
        migrate(db)
        v2 = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        db.close()
        assert v1 == v2, f"Version changed from {v1} to {v2} on re-migrate"
        assert v1 == SCHEMA_VERSION

    def test_idempotent_source_runs_create(self):
        """Creating source_runs twice must not error."""
        db = _fresh_migrated_db()
        # The migration should use CREATE TABLE IF NOT EXISTS
        try:
            migrate(db)
        except Exception as e:
            db.close()
            pytest.fail(f"Re-migration raised: {e}")
        db.close()

    def test_idempotent_registry_id_column(self):
        """Adding registry_id to economic_entities twice must not error."""
        db = _fresh_migrated_db()
        try:
            migrate(db)
        except Exception as e:
            db.close()
            pytest.fail(f"Re-migration raised: {e}")
        db.close()

    def test_idempotent_scoreable_companies_view(self):
        """Creating scoreable_companies view twice must not error."""
        db = _fresh_migrated_db()
        try:
            migrate(db)
        except Exception as e:
            db.close()
            pytest.fail(f"Re-migration raised: {e}")
        db.close()


# ── Clean bootstrap vs upgrade equivalence ─────────────────────────────────────

class TestBootstrapUpgradeEquivalence:
    """A fresh V12 migration must produce the same tables/columns/indexes/views
    as upgrading a V10-like schema."""

    def _table_and_columns(self, db):
        """Return {table_name: {column_names}} for all tables."""
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        result = {}
        for t in tables:
            tname = t["name"]
            cols = db.execute(f"PRAGMA table_info({tname})").fetchall()
            result[tname] = {c["name"] for c in cols}
        return result

    def _views(self, db):
        """Return set of view names."""
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
        ).fetchall()
        return {r["name"] for r in rows}

    def _indexes(self, db):
        """Return set of index names (excluding auto-indexes)."""
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return {r["name"] for r in rows}

    def test_fresh_equals_upgraded(self):
        """Fresh bootstrap V12 == V10 upgraded to V12 on tables/columns/views/indexes."""
        # Fresh bootstrap
        fresh_db = _fresh_migrated_db()
        fresh_tables = self._table_and_columns(fresh_db)
        fresh_views = self._views(fresh_db)
        fresh_indexes = self._indexes(fresh_db)
        fresh_version = fresh_db.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        fresh_db.close()

        # Upgrade from V10
        upgrade_db = sqlite3.connect(":memory:")
        upgrade_db.row_factory = sqlite3.Row
        upgrade_db.execute("PRAGMA foreign_keys=ON")
        _migrate_to_version(upgrade_db, 10)
        # Now upgrade all the way
        migrate(upgrade_db)
        upgrade_tables = self._table_and_columns(upgrade_db)
        upgrade_views = self._views(upgrade_db)
        upgrade_indexes = self._indexes(upgrade_db)
        upgrade_version = upgrade_db.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        upgrade_db.close()

        assert fresh_version == upgrade_version, (
            f"Version mismatch: fresh={fresh_version}, upgrade={upgrade_version}"
        )

        # Same tables
        assert fresh_tables.keys() == upgrade_tables.keys(), (
            f"Table mismatch: fresh={set(fresh_tables.keys()) - set(upgrade_tables.keys())}, "
            f"upgrade={set(upgrade_tables.keys()) - set(fresh_tables.keys())}"
        )

        # Same columns per table
        for tname in fresh_tables:
            fc = fresh_tables[tname]
            uc = upgrade_tables[tname]
            assert fc == uc, (
                f"Column mismatch for table '{tname}': "
                f"fresh_only={fc - uc}, upgrade_only={uc - fc}"
            )

        # Same views
        assert fresh_views == upgrade_views, (
            f"View mismatch: fresh={fresh_views - upgrade_views}, "
            f"upgrade={upgrade_views - fresh_views}"
        )

        # Same indexes
        assert fresh_indexes == upgrade_indexes, (
            f"Index mismatch: fresh={fresh_indexes - upgrade_indexes}, "
            f"upgrade={upgrade_indexes - fresh_indexes}"
        )


# ── Typed results tests ───────────────────────────────────────────────────────

class TestEvidenceResult:
    """Evidence insertion must return typed results (inserted vs duplicate)."""

    def test_import_exists(self):
        """EvidenceResult must be importable."""
        from kassandra.contracts import EvidenceResult
        assert EvidenceResult is not None

    def test_fields(self):
        """EvidenceResult must have correct fields."""
        from kassandra.contracts import EvidenceResult
        result = EvidenceResult(
            evidence_id=1,
            is_new=True,
            content_hash="abc123",
        )
        assert result.evidence_id == 1
        assert result.is_new is True
        assert result.content_hash == "abc123"

    def test_duplicate(self):
        """EvidenceResult for duplicate evidence."""
        from kassandra.contracts import EvidenceResult
        result = EvidenceResult(
            evidence_id=42,
            is_new=False,
            content_hash="def456",
        )
        assert result.evidence_id == 42
        assert result.is_new is False


class TestEventResult:
    """Event insertion must return typed results (inserted vs duplicate/rejected)."""

    def test_import_exists(self):
        """EventResult must be importable."""
        from kassandra.contracts import EventResult
        assert EventResult is not None

    def test_inserted(self):
        """EventResult for newly inserted event."""
        from kassandra.contracts import EventResult
        result = EventResult(event_id=5, status="inserted")
        assert result.event_id == 5
        assert result.status == "inserted"
        assert result.is_new is True

    def test_duplicate(self):
        """EventResult for duplicate event."""
        from kassandra.contracts import EventResult
        result = EventResult(event_id=5, status="duplicate")
        assert result.event_id == 5
        assert result.status == "duplicate"
        assert result.is_new is False

    def test_rejected(self):
        """EventResult for rejected event with reason."""
        from kassandra.contracts import EventResult
        result = EventResult(event_id=None, status="rejected", reject_reason="below_confidence_threshold")
        assert result.event_id is None
        assert result.status == "rejected"
        assert result.is_new is False
        assert result.reject_reason == "below_confidence_threshold"


class TestCollectionMetrics:
    """CollectionMetrics must provide canonical per-run counters."""

    def test_import_exists(self):
        """CollectionMetrics must be importable."""
        from kassandra.contracts import CollectionMetrics
        assert CollectionMetrics is not None

    def test_all_fields(self):
        """CollectionMetrics must have all required fields."""
        from kassandra.contracts import CollectionMetrics
        m = CollectionMetrics(
            run_id="r1",
            source_name="test_source",
            discovered=100,
            fetched=90,
            new_evidence=10,
            duplicates=80,
            candidates=5,
            unconfirmed=2,
            events_created=3,
            duplicate_events=1,
            errors=2,
        )
        assert m.run_id == "r1"
        assert m.source_name == "test_source"
        assert m.discovered == 100
        assert m.fetched == 90
        assert m.new_evidence == 10
        assert m.duplicates == 80
        assert m.candidates == 5
        assert m.unconfirmed == 2
        assert m.events_created == 3
        assert m.duplicate_events == 1
        assert m.errors == 2

    def test_must_reconcile_discovered(self):
        """Discovered must equal new + duplicates (+ parse_failures that couldn't be fetched)."""
        from kassandra.contracts import CollectionMetrics
        m = CollectionMetrics(
            run_id="r1",
            source_name="test",
            discovered=100,
            fetched=95,
            new_evidence=10,
            duplicates=85,
            errors=5,  # 5 fetch failures: 95 fetched, 5 errors = 100 discovered
        )
        # fetched + errors should cover discovered
        assert m.fetched + m.errors == m.discovered

    def test_to_dict(self):
        """CollectionMetrics must be serializable to dict."""
        from kassandra.contracts import CollectionMetrics
        m = CollectionMetrics(
            run_id="r1",
            source_name="test",
            discovered=10,
            fetched=10,
            new_evidence=1,
            duplicates=9,
        )
        d = m.to_dict()
        assert d["run_id"] == "r1"
        assert d["new_evidence"] == 1
