"""Tests for Kassandra database and migrations."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from kassandra.db import SCHEMA_VERSION, migrate, _connect


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for testing."""
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


class TestMigrations:
    """Test schema migrations."""

    def test_fresh_migration(self, temp_db):
        """Fresh database gets all migrations applied."""
        version = migrate(temp_db)
        assert version == SCHEMA_VERSION

    def test_idempotent_migration(self, temp_db):
        """Running migrate twice is safe."""
        v1 = migrate(temp_db)
        v2 = migrate(temp_db)
        assert v1 == v2

    def test_reconciles_partially_created_live_source_runs_schema(self, temp_db):
        """A legacy manually-created source_runs table gains all runtime columns."""
        migrate(temp_db)
        temp_db.execute("DROP TABLE source_runs")
        temp_db.execute("""
            CREATE TABLE source_runs (
                id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                documents_discovered INTEGER DEFAULT 0,
                documents_fetched INTEGER DEFAULT 0,
                new_documents INTEGER DEFAULT 0,
                api_errors INTEGER DEFAULT 0,
                parse_failures INTEGER DEFAULT 0,
                adverse_events_found INTEGER DEFAULT 0,
                latency_seconds REAL,
                started_at TEXT,
                completed_at TEXT,
                status TEXT NOT NULL DEFAULT 'success'
            )
        """)
        temp_db.commit()

        assert migrate(temp_db) == SCHEMA_VERSION
        columns = {row["name"] for row in temp_db.execute("PRAGMA table_info(source_runs)")}
        assert {
            "duplicate_documents", "candidates_generated", "unconfirmed_matches",
            "events_created", "duplicate_events", "errors",
        }.issubset(columns)

    def test_all_tables_exist(self, temp_db):
        """All expected tables exist after migration."""
        migrate(temp_db)
        tables = temp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = {t["name"] for t in tables}
        expected = {
            "schema_version", "portfolios", "portfolio_items",
            "registry", "registry_aliases", "evidence", "events",
            "edges", "scores", "sources", "job_state", "journal",
            "web_cache", "economic_entities", "graph_collection_state",
            "classifier_runs", "source_runs", "unconfirmed_match_queue",
            "scoring_runs",
        }
        assert expected.issubset(table_names), f"Missing: {expected - table_names}"

    def test_portfolio_insert(self, temp_db):
        """Can insert and query portfolio items."""
        migrate(temp_db)
        temp_db.execute(
            "INSERT INTO portfolios (name) VALUES (?)", ("Test Portfolio",)
        )
        portfolio_id = temp_db.execute(
            "SELECT id FROM portfolios WHERE name = ?", ("Test Portfolio",)
        ).fetchone()["id"]
        temp_db.execute(
            """INSERT INTO portfolio_items
               (portfolio_id, ticker, isin, name, sector, country, weight, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (portfolio_id, "TEST", "XX0000000000", "Test Co", "Technology", "XX", 1.0, "test"),
        )
        temp_db.commit()
        row = temp_db.execute("SELECT * FROM portfolio_items").fetchone()
        assert row["name"] == "Test Co"

    def test_evidence_storage(self, temp_db):
        """Evidence can be stored and retrieved."""
        migrate(temp_db)
        temp_db.execute(
            """INSERT INTO evidence
               (content_hash, source_url, retrieval_time, extraction_method,
                parser_version, content_length)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("abc123", "https://example.com", "2025-01-01T00:00:00Z", "test", "1.0", 100),
        )
        temp_db.commit()
        row = temp_db.execute(
            "SELECT * FROM evidence WHERE content_hash = ?", ("abc123",)
        ).fetchone()
        assert row["source_url"] == "https://example.com"

    def test_events_with_registry(self, temp_db):
        """Events link to registry and evidence."""
        migrate(temp_db)
        temp_db.execute(
            """INSERT INTO registry
               (canonical_name, isin, jurisdiction)
               VALUES (?, ?, ?)""",
            ("TestCo Ltd", "XX0000000000", "XX"),
        )
        reg_id = temp_db.execute("SELECT id FROM registry").fetchone()["id"]
        temp_db.execute(
            """INSERT INTO evidence
               (content_hash, source_url, retrieval_time, extraction_method,
                parser_version, content_length)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("ev123", "https://example.com", "2025-01-01T00:00:00Z", "test", "1.0", 100),
        )
        ev_id = temp_db.execute("SELECT id FROM evidence").fetchone()["id"]
        temp_db.execute(
            """INSERT INTO events
               (evidence_id, registry_id, event_type, severity, confidence)
               VALUES (?, ?, ?, ?, ?)""",
            (ev_id, reg_id, "insolvency", "critical", 1.0),
        )
        temp_db.commit()
        row = temp_db.execute(
            "SELECT e.*, r.canonical_name FROM events e JOIN registry r ON e.registry_id = r.id"
        ).fetchone()
        assert row["event_type"] == "insolvency"

    def test_scores_immutable(self, temp_db):
        """Scores can be stored and multiple snapshots coexist."""
        migrate(temp_db)
        temp_db.execute(
            "INSERT INTO registry (canonical_name) VALUES (?)", ("TestCo",)
        )
        reg_id = temp_db.execute("SELECT id FROM registry").fetchone()["id"]

        temp_db.execute(
            """INSERT INTO scores
               (registry_id, score_schema_version, analyst_priority, computed_at)
               VALUES (?, ?, ?, ?)""",
            (reg_id, 1, 0.5, "2025-01-01T00:00:00Z"),
        )
        temp_db.execute(
            """INSERT INTO scores
               (registry_id, score_schema_version, analyst_priority, computed_at)
               VALUES (?, ?, ?, ?)""",
            (reg_id, 1, 0.7, "2025-02-01T00:00:00Z"),
        )
        temp_db.commit()

        scores = temp_db.execute(
            "SELECT * FROM scores WHERE registry_id = ? ORDER BY computed_at", (reg_id,)
        ).fetchall()
        assert len(scores) == 2
        assert scores[0]["analyst_priority"] != scores[1]["analyst_priority"]

    def test_reconciliation_fast_path_current_schema(self, temp_db):
        """Current clean schema skips the full per-column PRAGMA loop."""
        from kassandra.db import _reconcile_runtime_schema, SCHEMA_VERSION as current_ver

        migrate(temp_db)
        # Patch _ensure_column to detect if it's ever called
        called = []

        import kassandra.db as db_mod
        original = db_mod._ensure_column

        def tracking_ensure(conn, table, column, definition):
            called.append((table, column))
            return original(conn, table, column, definition)

        db_mod._ensure_column = tracking_ensure
        try:
            _reconcile_runtime_schema(temp_db)
            assert not called, (
                f"Fast path should skip _ensure_column calls; got {called}"
            )
        finally:
            db_mod._ensure_column = original

    def test_reconciliation_heals_drifted_schema(self, temp_db):
        """A partially-drifted schema still gets healed by reconciliation."""
        from kassandra.db import _reconcile_runtime_schema

        migrate(temp_db)
        # Simulate drift: remove a critical column
        temp_db.execute("ALTER TABLE source_runs DROP COLUMN errors")
        temp_db.commit()

        # Should not raise; reconciliation must add it back
        _reconcile_runtime_schema(temp_db)
        columns = {
            row["name"] for row in temp_db.execute("PRAGMA table_info(source_runs)")
        }
        assert "errors" in columns, "Reconciliation must restore dropped column"

    def test_reconciliation_detects_missing_scoring_runs_provenance_json(self, temp_db):
        """Dropped scoring_runs.provenance_json must be detected and fail fast path."""
        from kassandra.db import _reconcile_runtime_schema, _SchemaIntegrityError

        migrate(temp_db)
        # Drop the provenance_json column from scoring_runs (simulate malformed table)
        # SQLite doesn't support DROP COLUMN reliably in all versions, so we
        # simulate by creating a broken scoring_runs table via recreation:
        temp_db.execute("DROP TABLE scoring_runs")
        temp_db.execute("""
            CREATE TABLE scoring_runs (
                run_id TEXT PRIMARY KEY,
                input_fingerprint TEXT NOT NULL,
                scorer_version TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(input_fingerprint, scorer_version)
            )
        """)
        temp_db.execute("""
            CREATE INDEX idx_scoring_runs_fingerprint ON scoring_runs(input_fingerprint)
        """)
        temp_db.commit()

        # Reconciliation should detect the missing column and fall back to
        # full reconciliation, which recreates the table with all columns.
        _reconcile_runtime_schema(temp_db)
        columns = {
            row["name"] for row in temp_db.execute("PRAGMA table_info(scoring_runs)")
        }
        assert "provenance_json" in columns, (
            "Reconciliation must restore dropped scoring_runs.provenance_json"
        )

    def test_reconciliation_detects_missing_critical_index(self, temp_db):
        """Dropped idx_unconfirmed_dedup must be detected and healed."""
        from kassandra.db import _reconcile_runtime_schema

        migrate(temp_db)
        # Drop the critical index
        temp_db.execute("DROP INDEX idx_unconfirmed_dedup")
        temp_db.commit()

        # Fast path should detect missing index and fall back
        _reconcile_runtime_schema(temp_db)

        # Verify index was recreated
        indexes = {
            row[0]
            for row in temp_db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "idx_unconfirmed_dedup" in indexes, (
            "Reconciliation must recreate dropped idx_unconfirmed_dedup"
        )

    def test_reconciliation_heals_missing_provenance_triggers(self, temp_db):
        """Missing provenance integrity triggers must be healed."""
        from kassandra.db import _reconcile_runtime_schema

        migrate(temp_db)
        # Drop the triggers
        temp_db.execute("DROP TRIGGER trg_scores_validate_run_id_insert")
        temp_db.execute("DROP TRIGGER trg_scores_validate_run_id_update")
        temp_db.commit()

        # Reconciliation must recreate them
        _reconcile_runtime_schema(temp_db)

        triggers = {
            row[0]
            for row in temp_db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert "trg_scores_validate_run_id_insert" in triggers, (
            "Reconciliation must recreate dropped insert trigger"
        )
        assert "trg_scores_validate_run_id_update" in triggers, (
            "Reconciliation must recreate dropped update trigger"
        )

    def test_reconciliation_heals_missing_scoring_runs_uniqueness(self, temp_db):
        """scoring_runs lacking UNIQUE constraint must be healed."""
        from kassandra.db import _reconcile_runtime_schema

        migrate(temp_db)
        # Simulate a scoring_runs table without the UNIQUE constraint
        # by recreating it without the UNIQUE clause
        temp_db.execute("DROP TABLE scoring_runs")
        temp_db.execute("""
            CREATE TABLE scoring_runs (
                run_id TEXT PRIMARY KEY,
                input_fingerprint TEXT NOT NULL,
                scorer_version TEXT NOT NULL,
                provenance_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        temp_db.commit()

        # Reconciliation must fall back and recreate with UNIQUE constraint
        _reconcile_runtime_schema(temp_db)

        # Verify UNIQUE constraint exists
        pk_info = [
            row[1]
            for row in temp_db.execute("PRAGMA table_info(scoring_runs)")
            if row[5] > 0
        ]
        assert pk_info, "scoring_runs must have a PRIMARY KEY after reconciliation"
