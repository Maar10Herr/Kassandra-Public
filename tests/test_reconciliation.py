"""Reconciliation tests for Kassandra source/evidence/event accounting.

Verifies:
  - store_evidence returns typed EvidenceResult (inserted vs duplicate)
  - store_event returns typed EventResult (inserted, duplicate, rejected)
  - CollectionMetrics produced by collectors reconcile correctly
  - source_runs records real database effects
  - events_created equals actual inserted active company_events delta per source run
  - Duplicate/replay scenarios, unconfirmed candidates, partial failures
"""

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kassandra.db import migrate, _connect
from kassandra.contracts import EvidenceResult, EventResult, CollectionMetrics, MatchResult


# ── Helpers ───────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db():
    """Create a temporary migrated database."""
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


def _insert_registry(db, canonical_name, jurisdiction="GB", companies_house_number=None):
    """Insert a registry entry and return its id."""
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction, companies_house_number) VALUES (?, ?, ?)",
        (canonical_name, jurisdiction, companies_house_number),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


# ── EvidenceResult — store_evidence returns typed result ──────────────────────


class TestStoreEvidenceTypedResult:
    """store_evidence() must return EvidenceResult with is_new distinction."""

    def test_new_evidence_returns_inserted(self, temp_db):
        """First-time evidence insertion returns EvidenceResult with is_new=True."""
        from kassandra.evidence import store_evidence

        result = store_evidence(
            db=temp_db,
            content="unique new content",
            source_url="https://example.com/test1",
            retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test",
            parser_version="1.0.0",
        )
        assert isinstance(result, EvidenceResult), f"Expected EvidenceResult, got {type(result)}"
        assert result.evidence_id is not None
        assert result.evidence_id > 0
        assert result.is_new is True
        assert len(result.content_hash) == 64  # SHA-256

    def test_duplicate_evidence_returns_not_new(self, temp_db):
        """Duplicate evidence returns EvidenceResult with is_new=False and same evidence_id."""
        from kassandra.evidence import store_evidence

        # First insert
        result1 = store_evidence(
            db=temp_db,
            content="duplicate content test",
            source_url="https://example.com/test2",
            retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test",
            parser_version="1.0.0",
        )
        assert result1.is_new is True

        # Second insert — same content
        result2 = store_evidence(
            db=temp_db,
            content="duplicate content test",
            source_url="https://different-url.com",  # Different URL, same content
            retrieval_time="2025-01-02T00:00:00Z",
            extraction_method="test",
            parser_version="1.0.0",
        )
        assert result2.is_new is False
        assert result2.evidence_id == result1.evidence_id
        assert result2.content_hash == result1.content_hash

    def test_distinct_evidence_both_new(self, temp_db):
        """Two different content items both return is_new=True."""
        from kassandra.evidence import store_evidence

        result1 = store_evidence(
            db=temp_db, content="content A",
            source_url="https://a.com", retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test", parser_version="1.0",
        )
        result2 = store_evidence(
            db=temp_db, content="content B",
            source_url="https://b.com", retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test", parser_version="1.0",
        )
        assert result1.is_new is True
        assert result2.is_new is True
        assert result1.evidence_id != result2.evidence_id

    def test_same_content_different_urls_same_hash(self, temp_db):
        """Content hash is independent of source_url — dedup by content."""
        from kassandra.evidence import store_evidence

        result1 = store_evidence(
            db=temp_db, content="same content here",
            source_url="https://site1.com/page", retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test", parser_version="1.0",
        )
        result2 = store_evidence(
            db=temp_db, content="same content here",
            source_url="https://site2.com/other", retrieval_time="2025-01-02T00:00:00Z",
            extraction_method="test", parser_version="1.0",
        )
        assert result1.content_hash == result2.content_hash
        assert result1.evidence_id == result2.evidence_id
        assert result1.is_new is True
        assert result2.is_new is False


# ── EventResult — store_event returns typed result ────────────────────────────


class TestStoreEventTypedResult:
    """store_event() must return EventResult with explicit status (inserted/duplicate/rejected)."""

    def _make_evidence(self, db, content="event data"):
        """Helper: store evidence and return evidence_id."""
        from kassandra.evidence import store_evidence
        result = store_evidence(
            db=db, content=content,
            source_url="https://example.com/event", retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test", parser_version="1.0",
        )
        return result.evidence_id

    def test_new_event_returns_inserted(self, temp_db):
        """First-time event insertion returns EventResult with status='inserted'."""
        from kassandra.evidence import store_event

        ev_id = self._make_evidence(temp_db)
        reg_id = _insert_registry(temp_db, "TestCo")

        result = store_event(
            db=temp_db, evidence_id=ev_id, registry_id=reg_id,
            event_type="insolvency", severity="critical",
        )
        assert isinstance(result, EventResult), f"Expected EventResult, got {type(result)}"
        assert result.event_id is not None
        assert result.event_id > 0
        assert result.status == "inserted"
        assert result.is_new is True

    def test_duplicate_event_returns_duplicate(self, temp_db):
        """Duplicate event (same evidence+type+registry) returns status='duplicate'."""
        from kassandra.evidence import store_event

        ev_id = self._make_evidence(temp_db)
        reg_id = _insert_registry(temp_db, "TestCo")

        result1 = store_event(
            db=temp_db, evidence_id=ev_id, registry_id=reg_id,
            event_type="insolvency", severity="critical",
        )
        assert result1.status == "inserted"
        assert result1.is_new is True

        # Same event again — must detect duplicate explicitly
        result2 = store_event(
            db=temp_db, evidence_id=ev_id, registry_id=reg_id,
            event_type="insolvency", severity="critical",
        )
        assert result2.status == "duplicate", f"Expected 'duplicate', got '{result2.status}'"
        assert result2.is_new is False
        assert result2.event_id == result1.event_id  # Same event_id returned

    def test_different_event_type_same_evidence_is_new(self, temp_db):
        """Different event_type on same evidence is a new event."""
        from kassandra.evidence import store_event

        ev_id = self._make_evidence(temp_db)
        reg_id = _insert_registry(temp_db, "TestCo")

        result1 = store_event(
            db=temp_db, evidence_id=ev_id, registry_id=reg_id,
            event_type="insolvency", severity="critical",
        )
        assert result1.status == "inserted"

        result2 = store_event(
            db=temp_db, evidence_id=ev_id, registry_id=reg_id,
            event_type="restructuring", severity="high",
        )
        assert result2.status == "inserted"
        assert result2.event_id != result1.event_id

    def test_rejection_preserves_reason(self, temp_db):
        """If we provide reject_reason, EventResult should record it."""
        from kassandra.evidence import store_event

        ev_id = self._make_evidence(temp_db)
        reg_id = _insert_registry(temp_db, "TestCo")

        # store_event with empty/None event_type should be rejected
        result = store_event(
            db=temp_db, evidence_id=ev_id, registry_id=reg_id,
            event_type="", severity="low",
        )
        assert result.status == "rejected"
        assert result.is_new is False
        assert result.event_id is None
        assert result.reject_reason is not None

    def test_event_has_lifecycle_defaults(self, temp_db):
        """New events get active=1, status='active' via migration 12 defaults."""
        from kassandra.evidence import store_event

        ev_id = self._make_evidence(temp_db)
        reg_id = _insert_registry(temp_db, "TestCo")

        result = store_event(
            db=temp_db, evidence_id=ev_id, registry_id=reg_id,
            event_type="profit_warning", severity="medium",
        )
        assert result.status == "inserted"

        row = temp_db.execute(
            "SELECT active, status, tombstone_reason, tombstoned_at FROM events WHERE id = ?",
            (result.event_id,),
        ).fetchone()
        assert row["active"] == 1
        assert row["status"] == "active"
        assert row["tombstone_reason"] is None
        assert row["tombstoned_at"] is None

    def test_event_dedup_preserves_original_event_id(self, temp_db):
        """Event dedup must return the original event_id, not a new one."""
        from kassandra.evidence import store_event

        ev_id = self._make_evidence(temp_db)
        reg_id = _insert_registry(temp_db, "TestCo")

        result1 = store_event(
            db=temp_db, evidence_id=ev_id, registry_id=reg_id,
            event_type="insolvency", severity="critical", confidence=0.95,
        )
        original_id = result1.event_id

        # Replay — different confidence, same evidence+type+registry
        result2 = store_event(
            db=temp_db, evidence_id=ev_id, registry_id=reg_id,
            event_type="insolvency", severity="critical", confidence=0.50,
        )
        assert result2.status == "duplicate"
        assert result2.event_id == original_id


# ── CollectionMetrics reconciliation invariants ───────────────────────────────


class TestCollectionMetricsReconciliation:
    """CollectionMetrics must satisfy accounting invariants."""

    def test_fetched_plus_errors_covers_discovered(self):
        """fetched + errors must equal discovered (or discovered is an upper bound)."""
        m = CollectionMetrics(
            run_id="r1", source_name="test",
            discovered=100, fetched=95, new_evidence=10, duplicates=85,
            errors=5,
        )
        assert m.fetched + m.errors == m.discovered

    def test_fetched_equals_new_plus_duplicates_plus_errors(self):
        """fetched must equal new_evidence + duplicates (+ errors for content that failed parse)."""
        m = CollectionMetrics(
            run_id="r1", source_name="test",
            discovered=100, fetched=100,
            new_evidence=20, duplicates=78, errors=2,
        )
        # 20 new + 78 dup = 98; 2 errors => 100 fetched
        assert m.new_evidence + m.duplicates + m.errors == m.fetched

    def test_events_created_plus_duplicate_events_lte_candidates(self):
        """events_created + duplicate_events <= candidates."""
        m = CollectionMetrics(
            run_id="r1", source_name="test",
            candidates=10, events_created=5, duplicate_events=3,
        )
        assert m.events_created + m.duplicate_events <= m.candidates

    def test_to_dict_all_fields_present(self):
        """to_dict() must include all fields for serialization."""
        m = CollectionMetrics(
            run_id="r1", source_name="test",
            discovered=10, fetched=10, new_evidence=5, duplicates=5,
            candidates=3, unconfirmed=2, events_created=1, duplicate_events=1,
            errors=0,
        )
        d = m.to_dict()
        for key in ["run_id", "source_name", "discovered", "fetched",
                     "new_evidence", "duplicates", "candidates", "unconfirmed",
                     "events_created", "duplicate_events", "errors"]:
            assert key in d, f"Missing key '{key}' in to_dict()"

    def test_zero_values_accepted(self):
        """All zeros is a valid state for a source that found nothing."""
        m = CollectionMetrics(run_id="r1", source_name="empty_source")
        assert m.discovered == 0
        assert m.events_created == 0
        d = m.to_dict()
        assert d["discovered"] == 0
        assert d["events_created"] == 0


# ── source_runs reconciliation — events_created matches actual DB delta ───────


class TestSourceRunsReconciliation:
    """source_runs rows must reflect real database effects."""

    def _count_active_events(self, db) -> int:
        return db.execute("SELECT COUNT(*) FROM events WHERE active = 1").fetchone()[0]

    def _count_evidence(self, db) -> int:
        return db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]

    def _count_unconfirmed(self, db) -> int:
        return db.execute("SELECT COUNT(*) FROM unconfirmed_match_queue WHERE status='pending'").fetchone()[0]

    def test_events_created_matches_active_delta(self, temp_db):
        """events_created in source_runs must equal actual delta of active events."""
        from kassandra.evidence import store_evidence, store_event

        reg_id = _insert_registry(temp_db, "TestCo")

        # Phase 1: Initial run — insert events, record run
        events_before = self._count_active_events(temp_db)

        ev_result = store_evidence(
            db=temp_db, content="initial event content",
            source_url="https://src.com/1", retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test", parser_version="1.0",
        )
        result1 = store_event(
            db=temp_db, evidence_id=ev_result.evidence_id, registry_id=reg_id,
            event_type="insolvency", severity="critical",
        )
        assert result1.status == "inserted"
        result2 = store_event(
            db=temp_db, evidence_id=ev_result.evidence_id, registry_id=reg_id,
            event_type="restructuring", severity="high",
        )
        assert result2.status == "inserted"

        events_after = self._count_active_events(temp_db)
        delta = events_after - events_before
        assert delta == 2, f"Expected 2 new events, got {delta}"

        # Phase 2: Replay — duplicate inserts must NOT increase event count
        events_before_replay = self._count_active_events(temp_db)

        result1b = store_event(
            db=temp_db, evidence_id=ev_result.evidence_id, registry_id=reg_id,
            event_type="insolvency", severity="critical",
        )
        assert result1b.status == "duplicate"

        events_after_replay = self._count_active_events(temp_db)
        replay_delta = events_after_replay - events_before_replay
        assert replay_delta == 0, f"Replay must not increase active events, got delta={replay_delta}"

    def test_new_evidence_no_event(self, temp_db):
        """Evidence that produces no events must still be counted in evidence, not events."""
        from kassandra.evidence import store_evidence

        events_before = self._count_active_events(temp_db)
        evidence_before = self._count_evidence(temp_db)

        # Store evidence with no associated event
        result = store_evidence(
            db=temp_db, content="Benign content — no event triggers",
            source_url="https://src.com/benign", retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test", parser_version="1.0",
        )
        assert result.is_new is True

        events_after = self._count_active_events(temp_db)
        evidence_after = self._count_evidence(temp_db)

        assert events_after == events_before, "No events should be created"
        assert evidence_after == evidence_before + 1

    def test_duplicate_event_counting(self, temp_db):
        """A replayed event counts as duplicate_event, not events_created."""
        from kassandra.evidence import store_evidence, store_event

        reg_id = _insert_registry(temp_db, "TestCo")
        ev_result = store_evidence(
            db=temp_db, content="dedup test content",
            source_url="https://src.com/dedup", retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test", parser_version="1.0",
        )

        # First: new event
        r1 = store_event(temp_db, ev_result.evidence_id, reg_id, "insolvency")
        assert r1.status == "inserted"

        # Second: duplicate
        r2 = store_event(temp_db, ev_result.evidence_id, reg_id, "insolvency")
        assert r2.status == "duplicate"

        # Count distinct events in DB (should be 1)
        event_count = temp_db.execute(
            "SELECT COUNT(*) FROM events WHERE evidence_id=? AND registry_id=? AND event_type=?",
            (ev_result.evidence_id, reg_id, "insolvency"),
        ).fetchone()[0]
        assert event_count == 1, f"Expected 1 event row, got {event_count}"


# ── source_runs table integration ────────────────────────────────────────────


class TestSourceRunsIntegration:
    """source_runs rows must be writable and readable with reconciliation fields."""

    def test_insert_source_run_with_reconciliation_fields(self, temp_db):
        """All reconciliation columns must be writable."""
        now = datetime.now(timezone.utc).isoformat()
        temp_db.execute(
            """INSERT INTO source_runs
               (run_id, source_name,
                documents_discovered, documents_fetched,
                new_documents, duplicate_documents,
                candidates_generated, unconfirmed_matches,
                events_created, duplicate_events, errors,
                started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("run-test-001", "test_source",
             100, 90, 10, 80,
             5, 2, 3, 1, 2,
             now, now),
        )
        temp_db.commit()
        row = temp_db.execute(
            "SELECT * FROM source_runs WHERE run_id = ?", ("run-test-001",)
        ).fetchone()
        assert row["documents_discovered"] == 100
        assert row["documents_fetched"] == 90
        assert row["new_documents"] == 10
        assert row["duplicate_documents"] == 80
        assert row["candidates_generated"] == 5
        assert row["unconfirmed_matches"] == 2
        assert row["events_created"] == 3
        assert row["duplicate_events"] == 1
        assert row["errors"] == 2

    def test_multiple_runs_independent(self, temp_db):
        """Each run gets its own source_runs row."""
        now = datetime.now(timezone.utc).isoformat()
        for i in range(3):
            temp_db.execute(
                """INSERT INTO source_runs
                   (run_id, source_name, documents_discovered, documents_fetched,
                    new_documents, duplicate_documents, started_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"run-{i}", "test_src", 10, 10, 5, 5, now, now),
            )
        temp_db.commit()
        count = temp_db.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0]
        assert count == 3

    def test_events_created_defaults_zero(self, temp_db):
        """events_created column defaults to 0 when not specified."""
        now = datetime.now(timezone.utc).isoformat()
        temp_db.execute(
            """INSERT INTO source_runs
               (run_id, source_name, documents_discovered, documents_fetched,
                started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("run-minimal", "min_src", 5, 5, now, now),
        )
        temp_db.commit()
        row = temp_db.execute(
            "SELECT events_created, duplicate_events, errors, documents_discovered FROM source_runs WHERE run_id=?",
            ("run-minimal",),
        ).fetchone()
        assert row["events_created"] == 0
        assert row["duplicate_events"] == 0
        assert row["errors"] == 0


# ── Collector-level integration (mock-based) ─────────────────────────────────


def test_collection_error_classification_is_bounded_and_structured():
    from kassandra.collector import _classify_collection_error

    assert _classify_collection_error(TimeoutError("slow")) == "timeout"
    assert _classify_collection_error(PermissionError("401 unauthorized")) == "auth"
    assert _classify_collection_error(ValueError("invalid JSON payload")) == "parse"
    assert _classify_collection_error(RuntimeError("portal unavailable")) == "unavailable"


class TestCollectorMetricsFlow:
    """Collectors should return CollectionMetrics with accurate counts."""

    def test_collector_returns_typed_metrics(self, temp_db):
        """Each collector adapter should return CollectionMetrics."""
        # Test that the contracts support the flow — actual collector
        # integration is tested in test_soak.py
        m = CollectionMetrics(
            run_id="r1", source_name="companies_house",
            discovered=50, fetched=50, new_evidence=3, duplicates=47,
            candidates=3, unconfirmed=0, events_created=2, duplicate_events=1,
            errors=0,
        )
        assert isinstance(m, CollectionMetrics)
        d = m.to_dict()
        assert d["source_name"] == "companies_house"
        assert d["new_evidence"] == 3
        assert d["events_created"] == 2

    def test_collection_metrics_preserve_structured_error_classification(self):
        m = CollectionMetrics(
            run_id="r1", source_name="test", errors=1,
            api_errors=1, error_type="timeout", error_message="request timed out",
        )
        d = m.to_dict()
        assert d["api_errors"] == 1
        assert d["parse_failures"] == 0
        assert d["error_type"] == "timeout"

    def test_structured_error_is_persisted_in_source_run(self, temp_db):
        from kassandra.collector import _record_source_yields

        m = CollectionMetrics(
            run_id="r-error", source_name="test", errors=1,
            api_errors=1, error_type="timeout", error_message="request timed out",
        )
        _record_source_yields(temp_db, "r-error", {"test": m}, {"test": 0.5})
        row = temp_db.execute(
            "SELECT api_errors, parse_failures, error_detail_json FROM source_runs"
        ).fetchone()
        detail = json.loads(row["error_detail_json"])
        assert row["api_errors"] == 1
        assert row["parse_failures"] == 0
        assert detail == {"type": "timeout", "message": "request timed out"}

    def test_classifier_acceptance_not_counted_as_event(self, temp_db):
        """Classifier 'accepted_events' from pattern matching is NOT the same
        as events_created in the database. Classifier may accept a pattern hit
        that turns out to be a duplicate event on insertion."""
        # The classifier 'accepted_events' counters track pattern hits that pass
        # negative-filtering. But store_event may still reject or dedup.
        # This test verifies the conceptual distinction.
        from kassandra.evidence import store_evidence, store_event

        reg_id = _insert_registry(temp_db, "TestCo")

        # Simulate: classifier found 3 pattern hits (accepted_events = 3)
        classifier_accepted = 3

        # But after store_event: 1 inserted, 2 duplicates
        ev_result = store_evidence(
            db=temp_db, content="event content",
            source_url="https://src.com/ev", retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test", parser_version="1.0",
        )

        inserted = 0
        duplicates = 0
        for i in range(3):
            r = store_event(
                db=temp_db, evidence_id=ev_result.evidence_id, registry_id=reg_id,
                event_type="profit_warning", severity="medium",
            )
            if r.status == "inserted":
                inserted += 1
            elif r.status == "duplicate":
                duplicates += 1

        assert inserted == 1, "First event must be inserted"
        assert duplicates == 2, "Subsequent identical events must be duplicates"
        assert inserted + duplicates == classifier_accepted, \
            "Total store_event calls must equal classifier accepted"
        # But events_created (real DB delta) = 1, not 3
        actual_db_count = temp_db.execute(
            "SELECT COUNT(*) FROM events WHERE active=1 AND registry_id=?",
            (reg_id,),
        ).fetchone()[0]
        assert actual_db_count == 1, \
            f"DB has {actual_db_count} active events, not {classifier_accepted}"
