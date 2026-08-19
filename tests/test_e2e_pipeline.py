"""End-to-end pipeline tests for Kassandra early-warning system.

Exercises the full pipeline against an in-memory/temp-file SQLite database:
- Migration bootstrap
- Evidence dedup
- Event storage and query
- Scoring smoke test
- Source yield recording
- Unconfirmed match queue
"""

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kassandra.db import SCHEMA_VERSION, _connect, migrate
from kassandra.evidence import store_evidence, store_event
from kassandra.contracts import CollectionMetrics


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db():
    """Create a temporary migrated SQLite database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    try:
        conn = _connect(path)
        migrate(conn)
        yield conn
        conn.close()
    finally:
        path.unlink(missing_ok=True)
        path.with_suffix(".db-wal").unlink(missing_ok=True)
        path.with_suffix(".db-shm").unlink(missing_ok=True)


@pytest.fixture
def in_memory_db():
    """Create an in-memory SQLite database with migrations applied.

    Uses a temp-file based connection (via _connect) because WAL mode
    and pragma settings are required for full migration compatibility.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    try:
        conn = _connect(path)
        migrate(conn)
        yield conn
        conn.close()
    finally:
        path.unlink(missing_ok=True)
        path.with_suffix(".db-wal").unlink(missing_ok=True)
        path.with_suffix(".db-shm").unlink(missing_ok=True)


def _insert_registry(db, canonical_name, jurisdiction="GB", domain=None, isin=None):
    """Insert a registry entry and return its id."""
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction, domain, isin) VALUES (?, ?, ?, ?)",
        (canonical_name, jurisdiction, domain, isin),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


# ── 1. Migration bootstrap test ───────────────────────────────────────────────


class TestMigrationBootstrap:
    """Verify that migrate() creates all required tables and structures."""

    REQUIRED_TABLES = {
        "registry",
        "evidence",
        "events",
        "edges",
        "scores",
        "source_runs",
        "economic_entities",
        "unconfirmed_match_queue",
        "classifier_runs",
    }

    def test_all_required_tables_exist(self, temp_db):
        """All required tables exist after migration."""
        tables = temp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = {t["name"] for t in tables}
        missing = self.REQUIRED_TABLES - table_names
        assert not missing, f"Missing tables: {missing}"

    def test_scoreable_companies_view_exists(self, temp_db):
        """scoreable_companies view exists after migration."""
        views = temp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='view' AND name='scoreable_companies'"
        ).fetchall()
        assert len(views) == 1, "scoreable_companies view must exist"

    def test_economic_entities_has_registry_id(self, temp_db):
        """economic_entities has registry_id column after migration."""
        columns = {
            row["name"]
            for row in temp_db.execute("PRAGMA table_info(economic_entities)")
        }
        assert "registry_id" in columns, (
            "economic_entities must have registry_id column"
        )

    def test_schema_version_matches(self, temp_db):
        """migrate() returns SCHEMA_VERSION."""
        version = migrate(temp_db)
        assert version == SCHEMA_VERSION, (
            f"migrate() returned {version}, expected {SCHEMA_VERSION}"
        )

    def test_migration_is_idempotent(self, temp_db):
        """Running migrate twice returns same version."""
        v1 = migrate(temp_db)
        v2 = migrate(temp_db)
        assert v1 == v2 == SCHEMA_VERSION


# ── 2. Evidence dedup test ────────────────────────────────────────────────────


class TestEvidenceDedup:
    """Verify evidence content-addressed deduplication."""

    def test_same_content_dedup_single_row(self, temp_db):
        """Same content inserted twice results in only one evidence row."""
        r1 = store_evidence(
            db=temp_db,
            content="dedup test content",
            source_url="https://example.com/a",
            retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test",
            parser_version="1.0",
        )
        r2 = store_evidence(
            db=temp_db,
            content="dedup test content",
            source_url="https://example.com/b",
            retrieval_time="2025-01-02T00:00:00Z",
            extraction_method="test",
            parser_version="1.0",
        )

        assert r1.is_new is True
        assert r2.is_new is False
        assert r1.evidence_id == r2.evidence_id, (
            "Dedup must return the same evidence_id"
        )

        count = temp_db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        assert count == 1, f"Expected 1 evidence row, got {count}"

    def test_different_content_produces_two_rows(self, temp_db):
        """Different content produces distinct evidence rows."""
        r1 = store_evidence(
            db=temp_db,
            content="content A",
            source_url="https://a.com",
            retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test",
            parser_version="1.0",
        )
        r2 = store_evidence(
            db=temp_db,
            content="content B",
            source_url="https://b.com",
            retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test",
            parser_version="1.0",
        )
        assert r1.evidence_id != r2.evidence_id
        assert r1.is_new is True
        assert r2.is_new is True
        count = temp_db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        assert count == 2


# ── 3. Event storage and query test ───────────────────────────────────────────


class TestEventStorageAndQuery:
    """Verify event insertion, query by registry_id, and dedup."""

    def test_events_query_by_registry_id(self, temp_db):
        """Insert events for multiple registry entries and query counts."""
        reg_a = _insert_registry(temp_db, "Company A")
        reg_b = _insert_registry(temp_db, "Company B")

        ev = store_evidence(
            db=temp_db,
            content="event evidence",
            source_url="https://src.com/events",
            retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test",
            parser_version="1.0",
        )

        # Two events for Company A, one for Company B
        store_event(temp_db, ev.evidence_id, reg_a, "insolvency", severity="critical")
        store_event(temp_db, ev.evidence_id, reg_a, "restructuring", severity="high")
        store_event(temp_db, ev.evidence_id, reg_b, "profit_warning", severity="medium")

        count_a = temp_db.execute(
            "SELECT COUNT(*) FROM events WHERE registry_id = ? AND active = 1",
            (reg_a,),
        ).fetchone()[0]
        count_b = temp_db.execute(
            "SELECT COUNT(*) FROM events WHERE registry_id = ? AND active = 1",
            (reg_b,),
        ).fetchone()[0]

        assert count_a == 2, f"Company A should have 2 events, got {count_a}"
        assert count_b == 1, f"Company B should have 1 event, got {count_b}"

    def test_event_dedup_same_key(self, temp_db):
        """Same evidence_id + event_type + registry_id is deduplicated."""
        reg_id = _insert_registry(temp_db, "TestCo")
        ev = store_evidence(
            db=temp_db,
            content="dedup event test",
            source_url="https://src.com/dedup",
            retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test",
            parser_version="1.0",
        )

        r1 = store_event(temp_db, ev.evidence_id, reg_id, "insolvency")
        r2 = store_event(temp_db, ev.evidence_id, reg_id, "insolvency")

        assert r1.status == "inserted"
        assert r2.status == "duplicate"
        assert r1.event_id == r2.event_id

        count = temp_db.execute("SELECT COUNT(*) FROM events WHERE active = 1").fetchone()[0]
        assert count == 1

    def test_events_link_to_registry_and_evidence(self, temp_db):
        """Events correctly join to registry and evidence tables."""
        reg_id = _insert_registry(temp_db, "LinkedCo")
        ev = store_evidence(
            db=temp_db,
            content="linked test",
            source_url="https://src.com/linked",
            retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test",
            parser_version="1.0",
        )
        event_result = store_event(
            temp_db, ev.evidence_id, reg_id, "insolvency", severity="critical"
        )

        row = temp_db.execute(
            """SELECT e.id, e.event_type, e.severity, r.canonical_name,
                      ev.content_hash, ev.source_url
               FROM events e
               JOIN registry r ON e.registry_id = r.id
               JOIN evidence ev ON e.evidence_id = ev.id
               WHERE e.id = ?""",
            (event_result.event_id,),
        ).fetchone()

        assert row["event_type"] == "insolvency"
        assert row["severity"] == "critical"
        assert row["canonical_name"] == "LinkedCo"
        assert row["content_hash"] is not None
        assert row["source_url"] == "https://src.com/linked"


# ── 4. Scoring smoke test ─────────────────────────────────────────────────────


class TestScoringSmoke:
    """Verify compute_scores() produces results for scoreable companies."""

    @pytest.fixture(autouse=True)
    def _mock_config(self):
        """Mock get_config so scoring doesn't need a real config file."""
        mock_cfg = MagicMock()
        mock_cfg.get.return_value = {}  # scoring weights default to empty dict
        with patch("kassandra.scoring.get_config", return_value=mock_cfg):
            yield

    def test_compute_scores_returns_results(self, temp_db):
        """compute_scores returns a result for each scoreable company."""
        # Insert a scoreable company (must have domain set)
        _insert_registry(temp_db, "ScoreCo", jurisdiction="GB", domain="scoreco.com", isin="XX0000000001")

        # Create a portfolio with this company
        temp_db.execute("INSERT INTO portfolios (name) VALUES (?)", ("Test Portfolio",))
        pf_id = temp_db.execute("SELECT last_insert_rowid()").fetchone()[0]
        temp_db.execute(
            """INSERT INTO portfolio_items
               (portfolio_id, ticker, isin, name, sector, country, weight, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (pf_id, "SCO", "XX0000000001", "ScoreCo", "Technology", "GB", 1.0, "test"),
        )
        temp_db.commit()

        from kassandra.scoring import compute_scores
        results = compute_scores(temp_db)

        assert len(results) >= 1, "Should have at least one result"
        # Find our company
        our_result = [r for r in results if r["canonical_name"] == "ScoreCo"]
        assert len(our_result) == 1, "ScoreCo must be in results"

        score = our_result[0]
        # Verify expected fields exist
        assert "registry_id" in score
        assert "canonical_name" in score
        assert "analyst_priority" in score
        assert "active_watch_priority" in score
        assert "coverage_monitor_priority" in score
        assert "event_count" in score
        assert "signal_score" in score
        assert "recency_score" in score
        assert "credibility_score" in score
        assert "legal_ownership_exposure" in score
        assert "economic_dep_exposure" in score
        assert "priority_reason" in score
        assert "coverage_quality" in score
        assert "explanation" in score

    def test_compute_scores_with_adverse_events(self, temp_db):
        """A company with adverse events gets a non-zero active_watch_priority."""
        reg_id = _insert_registry(
            temp_db, "TroubledCo", jurisdiction="GB", domain="troubledco.com", isin="XX0000000002"
        )
        temp_db.execute("INSERT INTO portfolios (name) VALUES (?)", ("Test PF",))
        pf_id = temp_db.execute("SELECT last_insert_rowid()").fetchone()[0]
        temp_db.execute(
            """INSERT INTO portfolio_items
               (portfolio_id, ticker, isin, name, sector, country, weight, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (pf_id, "TRB", "XX0000000002", "TroubledCo", "Industrial", "GB", 1.0, "test"),
        )
        temp_db.commit()

        # Add evidence and an adverse event
        ev = store_evidence(
            db=temp_db,
            content="adverse signal evidence",
            source_url="https://src.com/adverse",
            retrieval_time=datetime.now(timezone.utc).isoformat(),
            extraction_method="test",
            parser_version="1.0",
        )
        store_event(temp_db, ev.evidence_id, reg_id, "insolvency", severity="critical")

        from kassandra.scoring import compute_scores
        results = compute_scores(temp_db)

        our_result = [r for r in results if r["canonical_name"] == "TroubledCo"]
        assert len(our_result) == 1
        score = our_result[0]

        assert score["event_count"] >= 1, "Should have at least 1 event"
        assert score["signal_score"] > 0, "Adverse event should produce non-zero signal"
        assert score["active_watch_priority"] > 0, "Insolvency event must produce non-zero active_watch_priority"

    def test_scores_are_stored_in_db(self, temp_db):
        """compute_scores writes rows to the scores table."""
        _insert_registry(temp_db, "StoreCo", jurisdiction="GB", domain="storeco.com", isin="XX0000000003")
        temp_db.execute("INSERT INTO portfolios (name) VALUES (?)", ("Test PF",))
        pf_id = temp_db.execute("SELECT last_insert_rowid()").fetchone()[0]
        temp_db.execute(
            """INSERT INTO portfolio_items
               (portfolio_id, ticker, isin, name, sector, country, weight, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (pf_id, "STR", "XX0000000003", "StoreCo", "Tech", "GB", 1.0, "test"),
        )
        temp_db.commit()

        from kassandra.scoring import compute_scores
        compute_scores(temp_db)

        score_count = temp_db.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
        assert score_count >= 1, "Scores must be written to the scores table"


# ── 5. Source yield recording test ────────────────────────────────────────────


class TestSourceYieldRecording:
    """Verify _record_source_yields writes correct data to source_runs."""

    def test_record_source_yields_writes_row(self, temp_db):
        """_record_source_yields creates a source_runs row with correct values."""
        from kassandra.collector import _record_source_yields

        m = CollectionMetrics(
            run_id="run-yield-001",
            source_name="test_source",
            discovered=100,
            fetched=90,
            new_evidence=25,
            duplicates=65,
            candidates=10,
            unconfirmed=3,
            events_created=7,
            duplicate_events=3,
            errors=2,
        )
        latencies = {"test_source": 5.123}

        _record_source_yields(temp_db, "run-yield-001", {"test_source": m}, latencies)

        row = temp_db.execute(
            "SELECT * FROM source_runs WHERE run_id = ? AND source_name = ?",
            ("run-yield-001", "test_source"),
        ).fetchone()

        assert row is not None, "source_runs row must exist"
        assert row["documents_discovered"] == 100
        assert row["documents_fetched"] == 90
        assert row["new_documents"] == 25
        assert row["duplicate_documents"] == 65
        assert row["candidates_generated"] == 10
        assert row["unconfirmed_matches"] == 3
        assert row["events_created"] == 7
        assert row["duplicate_events"] == 3
        assert row["errors"] == 2
        assert row["adverse_events_found"] == 7  # equals events_created
        assert row["latency_seconds"] == 5.123

    def test_record_source_yields_multiple_sources(self, temp_db):
        """_record_source_yields handles multiple sources in one call."""
        from kassandra.collector import _record_source_yields

        m1 = CollectionMetrics(
            run_id="run-multi",
            source_name="source_a",
            discovered=50, fetched=50, new_evidence=10, duplicates=40,
            events_created=3, duplicate_events=1,
        )
        m2 = CollectionMetrics(
            run_id="run-multi",
            source_name="source_b",
            discovered=30, fetched=30, new_evidence=5, duplicates=25,
            events_created=0, duplicate_events=0,
        )

        _record_source_yields(temp_db, "run-multi", {"source_a": m1, "source_b": m2})

        rows = temp_db.execute(
            "SELECT source_name, new_documents, events_created FROM source_runs WHERE run_id = ? ORDER BY source_name",
            ("run-multi",),
        ).fetchall()

        assert len(rows) == 2
        assert rows[0]["source_name"] == "source_a"
        assert rows[0]["new_documents"] == 10
        assert rows[0]["events_created"] == 3
        assert rows[1]["source_name"] == "source_b"
        assert rows[1]["new_documents"] == 5
        assert rows[1]["events_created"] == 0

    def test_record_source_yields_without_latency(self, temp_db):
        """_record_source_yields handles omitted latencies (NULL)."""
        from kassandra.collector import _record_source_yields

        m = CollectionMetrics(
            run_id="run-no-lat", source_name="test_src",
            discovered=10, fetched=10, new_evidence=2, duplicates=8,
        )
        _record_source_yields(temp_db, "run-no-lat", {"test_src": m})

        row = temp_db.execute(
            "SELECT latency_seconds FROM source_runs WHERE run_id = ?",
            ("run-no-lat",),
        ).fetchone()
        assert row["latency_seconds"] is None


# ── 6. Unconfirmed match queue test ───────────────────────────────────────────


class TestUnconfirmedMatchQueue:
    """Verify unconfirmed_match_queue table exists and can store/retrieve rows."""

    def test_table_exists_after_migration(self, temp_db):
        """unconfirmed_match_queue table exists after migration."""
        tables = temp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='unconfirmed_match_queue'"
        ).fetchall()
        assert len(tables) == 1, "unconfirmed_match_queue table must exist"

    def test_insert_and_query(self, temp_db):
        """Can insert a row into unconfirmed_match_queue and query it back."""
        # Need a registry entry for FK reference
        reg_id = _insert_registry(temp_db, "Test Registry Entity")

        now = datetime.now(timezone.utc).isoformat()
        temp_db.execute(
            """INSERT INTO unconfirmed_match_queue
               (source_name, source_entity_name, candidate_registry_id,
                candidate_registry_name, match_type, match_confidence,
                evidence_excerpt, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "borme_es",
                "ENTIDAD EJEMPLO SL",
                reg_id,
                "Test Registry Entity",
                "name_only",
                0.65,
                "Partial name match from BORME notice",
                "pending",
                now,
                now,
            ),
        )
        temp_db.commit()

        row = temp_db.execute(
            "SELECT * FROM unconfirmed_match_queue WHERE source_name = ?",
            ("borme_es",),
        ).fetchone()

        assert row is not None
        assert row["source_entity_name"] == "ENTIDAD EJEMPLO SL"
        assert row["match_type"] == "name_only"
        assert row["match_confidence"] == 0.65
        assert row["status"] == "pending"
        assert row["evidence_excerpt"] == "Partial name match from BORME notice"

    def test_default_status_is_pending(self, temp_db):
        """Default status for new rows is 'pending'."""
        now = datetime.now(timezone.utc).isoformat()
        temp_db.execute(
            """INSERT INTO unconfirmed_match_queue
               (source_name, source_entity_name, match_type, match_confidence,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("uk_gazette", "Some Entity Ltd", "partial_overlap", 0.4, now, now),
        )
        temp_db.commit()

        row = temp_db.execute(
            "SELECT status FROM unconfirmed_match_queue WHERE source_entity_name = ?",
            ("Some Entity Ltd",),
        ).fetchone()
        assert row["status"] == "pending"

    def test_unique_index_prevents_duplicate_unresolved(self, temp_db):
        """The unique partial index prevents duplicate unresolved entries."""
        reg_id = _insert_registry(temp_db, "Unique Registry Co")

        now = datetime.now(timezone.utc).isoformat()
        temp_db.execute(
            """INSERT INTO unconfirmed_match_queue
               (source_name, source_entity_name, candidate_registry_id,
                match_type, match_confidence, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("test_source", "Unique Entity", reg_id, "name_only", 0.5, now, now),
        )
        temp_db.commit()

        # Second insert with same (source_name, source_entity_name, candidate_registry_id)
        # and resolution IS NULL should fail due to unique partial index
        with pytest.raises(sqlite3.IntegrityError):
            temp_db.execute(
                """INSERT INTO unconfirmed_match_queue
                   (source_name, source_entity_name, candidate_registry_id,
                    match_type, match_confidence, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("test_source", "Unique Entity", reg_id, "name_only", 0.5, now, now),
            )
            temp_db.commit()

    def test_adapter_queues_ambiguous_match_not_event(self, temp_db):
        """Ambiguous match routed through adapter → queue table, NOT events table."""
        from kassandra.sources.entity_resolution import queue_unconfirmed_match

        reg_id = _insert_registry(temp_db, "AmbiguousCo")

        # Simulate an adapter routing an ambiguous match
        queue_unconfirmed_match(
            db=temp_db,
            source_name="bodacc_fr",
            source_entity_name="AMBIGUOUS ENTITY SAS",
            candidate_registry_id=reg_id,
            candidate_registry_name="AmbiguousCo",
            match_type="single_token",
            match_confidence=0.3,
            evidence_id=None,
            evidence_excerpt="BODACC notice mentioning Ambiguous",
            reason="Single-token match is too weak to confirm",
        )

        # Must appear in queue
        queue_rows = temp_db.execute(
            "SELECT * FROM unconfirmed_match_queue WHERE source_name = 'bodacc_fr'"
        ).fetchall()
        assert len(queue_rows) == 1
        assert queue_rows[0]["source_entity_name"] == "AMBIGUOUS ENTITY SAS"
        assert queue_rows[0]["status"] == "pending"

        # Must NOT appear in events
        event_count = temp_db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert event_count == 0, "ambiguous match must not create an event"
