"""Tests for collector yield tracking in source_runs.

Verifies:
  - New vs existing evidence counting
  - events_created propagated to source_runs.adverse_events_found
  - Classifier run reconciliation (accepted vs actual insertions)
  - Latency tracking populated in source_runs
"""

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kassandra.contracts import CollectionMetrics, EvidenceResult, EventResult, MatchResult
from kassandra.db import _connect, migrate
from kassandra.evidence import store_evidence, store_event


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


# ── New vs existing evidence counting ─────────────────────────────────────────


class TestNewVsExistingEvidence:
    """Collectors must distinguish new evidence from deduplicated existing evidence."""

    def test_new_evidence_increments_new_not_duplicate(self, temp_db):
        """store_evidence returns is_new=True for first-time content."""
        result = store_evidence(
            db=temp_db,
            content="brand new evidence content",
            source_url="https://example.com/new",
            retrieval_time="2025-06-01T00:00:00Z",
            extraction_method="test",
            parser_version="1.0",
        )
        assert result.is_new is True
        assert result.evidence_id > 0

        # Verify exactly 1 evidence row exists
        count = temp_db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        assert count == 1

    def test_duplicate_evidence_increments_duplicate_not_new(self, temp_db):
        """Same content hash returns is_new=False on second store."""
        content = "duplicate-bound content"

        r1 = store_evidence(
            db=temp_db, content=content,
            source_url="https://site-a.com", retrieval_time="2025-06-01T00:00:00Z",
            extraction_method="test", parser_version="1.0",
        )
        assert r1.is_new is True

        r2 = store_evidence(
            db=temp_db, content=content,
            source_url="https://site-b.com/different-url", retrieval_time="2025-06-02T00:00:00Z",
            extraction_method="test", parser_version="1.0",
        )
        assert r2.is_new is False
        assert r2.evidence_id == r1.evidence_id

        # Only one row in evidence table
        count = temp_db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        assert count == 1

    def test_collector_conflating_new_and_existing_evidence_is_avoided(self, temp_db):
        """Collectors must use is_new flag, not just evidence_id presence."""
        # Simulate what a buggy collector would do: count any ID returned
        content = "collector test content"
        r1 = store_evidence(
            db=temp_db, content=content,
            source_url="https://a.com", retrieval_time="2025-06-01T00:00:00Z",
            extraction_method="test", parser_version="1.0",
        )
        r2 = store_evidence(
            db=temp_db, content=content,
            source_url="https://b.com", retrieval_time="2025-06-01T00:00:00Z",
            extraction_method="test", parser_version="1.0",
        )

        # Buggy: increment whenever an ID is returned
        buggy_count = 2  # both calls returned an evidence_id

        # Correct: only increment when is_new is True
        correct_new = (1 if r1.is_new else 0) + (1 if r2.is_new else 0)
        correct_dup = (1 if not r1.is_new else 0) + (1 if not r2.is_new else 0)

        assert correct_new == 1, "Only one call should count as new evidence"
        assert correct_dup == 1, "One call should count as duplicate"
        assert buggy_count != correct_new, (
            "Buggy counting conflates new and existing evidence"
        )


# ── events_created propagated to source_runs ──────────────────────────────────


class TestEventsFoundPropagated:
    """adverse_events_found in source_runs must reflect actual new events."""

    def test_events_created_reflects_inserted_events(self, temp_db):
        """events_created must only count EventResult.status='inserted'."""
        reg_id = _insert_registry(temp_db, "TestCo", jurisdiction="GB")

        ev_result = store_evidence(
            db=temp_db, content="event-triggering content",
            source_url="https://src.com/event", retrieval_time="2025-06-01T00:00:00Z",
            extraction_method="test", parser_version="1.0",
        )

        # First event: inserted
        r1 = store_event(
            db=temp_db, evidence_id=ev_result.evidence_id, registry_id=reg_id,
            event_type="insolvency", severity="critical",
        )
        assert r1.status == "inserted"

        # Second event (same key): duplicate
        r2 = store_event(
            db=temp_db, evidence_id=ev_result.evidence_id, registry_id=reg_id,
            event_type="insolvency", severity="critical",
        )
        assert r2.status == "duplicate"

        # events_created should be 1, not 2
        inserted = 1 if r1.status == "inserted" else 0
        inserted += 1 if r2.status == "inserted" else 0
        assert inserted == 1

        # Verify DB has exactly 1 active event
        db_events = temp_db.execute(
            "SELECT COUNT(*) FROM events WHERE active=1 AND registry_id=?",
            (reg_id,),
        ).fetchone()[0]
        assert db_events == 1

    def test_adverse_events_found_in_source_runs(self, temp_db):
        """source_runs row must contain the correct adverse_events_found count."""
        now = datetime.now(timezone.utc).isoformat()
        actual_events = 3  # 3 new events were created

        temp_db.execute(
            """INSERT INTO source_runs
               (run_id, source_name,
                documents_discovered, documents_fetched,
                new_documents, duplicate_documents,
                candidates_generated,
                events_created, duplicate_events,
                adverse_events_found,
                latency_seconds, started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("run-adverse-001", "companies_house",
             100, 90, 10, 80,
             5,
             actual_events, 2,
             actual_events,  # adverse_events_found = events_created
             3.45, now, now),
        )
        temp_db.commit()

        row = temp_db.execute(
            "SELECT * FROM source_runs WHERE run_id = ?",
            ("run-adverse-001",),
        ).fetchone()

        assert row["events_created"] == actual_events
        assert row["adverse_events_found"] == actual_events, (
            "adverse_events_found must equal events_created"
        )
        assert row["latency_seconds"] == 3.45

    def test_zero_events_produces_zero_adverse(self, temp_db):
        """When no events are created, adverse_events_found must be 0."""
        now = datetime.now(timezone.utc).isoformat()

        temp_db.execute(
            """INSERT INTO source_runs
               (run_id, source_name,
                documents_discovered, documents_fetched,
                new_documents, duplicate_documents,
                events_created, adverse_events_found,
                latency_seconds, started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("run-empty", "web_monitor", 50, 45, 5, 40, 0, 0, 1.2, now, now),
        )
        temp_db.commit()

        row = temp_db.execute(
            "SELECT events_created, adverse_events_found FROM source_runs WHERE run_id = ?",
            ("run-empty",),
        ).fetchone()

        assert row["events_created"] == 0
        assert row["adverse_events_found"] == 0


# ── Classifier run reconciliation ─────────────────────────────────────────────


class TestClassifierRunReconciliation:
    """Classifier accepted_events must reconcile with actual DB event insertions."""

    def test_classifier_accepted_exceeds_inserted_on_duplicates(self, temp_db):
        """Classifier pattern hits (accepted) can exceed events_created due to dedup."""
        from kassandra.evidence import store_event as se

        reg_id = _insert_registry(temp_db, "TestCo")

        ev_result = store_evidence(
            db=temp_db, content="classifier dedup test",
            source_url="https://src.com/classify", retrieval_time="2025-06-01T00:00:00Z",
            extraction_method="test", parser_version="1.0",
        )

        # Simulate classifier: 5 pattern hits accepted
        classifier_accepted = 5

        # But store_event deduplicates identical events
        inserted = 0
        duplicates = 0
        for _ in range(classifier_accepted):
            r = se(
                db=temp_db, evidence_id=ev_result.evidence_id, registry_id=reg_id,
                event_type="profit_warning", severity="medium",
            )
            if r.status == "inserted":
                inserted += 1
            elif r.status == "duplicate":
                duplicates += 1

        assert inserted == 1, "Only first event should be inserted"
        assert duplicates == 4, "Remaining 4 should be duplicates"
        assert inserted + duplicates == classifier_accepted, (
            "Every classifier hit must be accounted for"
        )

        # Key assertion: classifier 'accepted' != DB 'inserted'
        assert classifier_accepted > inserted, (
            "Classifier accepted count overstates actual new events due to dedup"
        )

    def test_classifier_accepted_equals_inserted_when_all_unique(self, temp_db):
        """When all events are unique, classifier accepted = inserted."""
        from kassandra.evidence import store_event as se

        reg_id = _insert_registry(temp_db, "TestCo")

        ev_result = store_evidence(
            db=temp_db, content="unique events test",
            source_url="https://src.com/unique", retrieval_time="2025-06-01T00:00:00Z",
            extraction_method="test", parser_version="1.0",
        )

        event_types = ["insolvency", "restructuring", "profit_warning"]
        inserted = 0
        duplicates = 0

        for etype in event_types:
            r = se(
                db=temp_db, evidence_id=ev_result.evidence_id, registry_id=reg_id,
                event_type=etype, severity="medium",
            )
            if r.status == "inserted":
                inserted += 1
            else:
                duplicates += 1

        assert inserted == 3, "All unique event types should be inserted"
        assert duplicates == 0, "No duplicates expected"
        assert inserted == len(event_types)

    def test_record_classifier_run_stores_actual_new_events(self, temp_db):
        """record_classifier_run should accept actual new-event counts, not total matches."""
        from kassandra.classifier import record_classifier_run

        # Simulate: 10 pattern hits, but only 3 were actually new events after dedup
        stats = {
            "documents_discovered": 100,
            "documents_fetched": 95,
            "documents_with_text": 80,
            "documents_classified": 75,
            "candidate_pattern_hits": 10,
            "rejected_candidates": 2,
            "accepted_events": 3,  # actual new events, not total pattern hits
            "false_positive_checks": 5,
        }

        row_id = record_classifier_run(
            db=temp_db,
            run_id="run-classifier-001",
            source_name="web_monitor",
            language="en",
            stats=stats,
        )
        assert row_id > 0

        row = temp_db.execute(
            "SELECT * FROM classifier_runs WHERE id = ?", (row_id,)
        ).fetchone()

        assert row["candidate_pattern_hits"] == 10
        assert row["rejected_candidates"] == 2
        assert row["accepted_events"] == 3, (
            "accepted_events should be actual new events, not total matches"
        )
        assert row["candidate_pattern_hits"] > row["accepted_events"], (
            "Pattern hits (10) > accepted_events (3) due to dedup"
        )

    def test_yield_report_uses_latest_post_gate_run(self, temp_db):
        """Historical false positives must not contaminate current source yield."""
        from kassandra.classifier import get_classifier_yield_report, record_classifier_run

        base = {
            "documents_discovered": 100, "documents_fetched": 100,
            "documents_with_text": 100, "documents_classified": 100,
            "candidate_pattern_hits": 100, "rejected_candidates": 0,
            "false_positive_checks": 0,
        }
        record_classifier_run(
            temp_db, "legacy", "bodacc_fr", None,
            {**base, "accepted_events": 100},
        )
        record_classifier_run(
            temp_db, "current", "bodacc_fr", None,
            {**base, "rejected_candidates": 100, "accepted_events": 0},
        )

        source = get_classifier_yield_report(temp_db)["per_source"]["bodacc_fr"]
        assert source["total_events"] == 0
        assert source["total_rejected"] == 100
        assert source["runs"] == 2
        assert source["yield_rate_pct"] == 0.0


# ── Latency tracking ──────────────────────────────────────────────────────────


class TestLatencyTracking:
    """source_runs.latency_seconds must be populated by the collector."""

    def test_latency_stored_in_source_runs(self, temp_db):
        """_record_source_yields stores latency_seconds when provided."""
        from kassandra.collector import _record_source_yields

        m = CollectionMetrics(
            run_id="run-latency", source_name="test_source",
            discovered=10, fetched=10, new_evidence=3, duplicates=7,
            events_created=2, duplicate_events=1,
        )
        latencies = {"test_source": 4.567}

        _record_source_yields(temp_db, "run-latency", {"test_source": m}, latencies)

        row = temp_db.execute(
            "SELECT latency_seconds FROM source_runs WHERE run_id = ? AND source_name = ?",
            ("run-latency", "test_source"),
        ).fetchone()

        assert row is not None, "source_runs row must be inserted"
        assert row["latency_seconds"] == 4.567, (
            "latency_seconds must match the provided value"
        )

    def test_no_latency_defaults_to_null(self, temp_db):
        """When latency is not provided, latency_seconds should be NULL."""
        from kassandra.collector import _record_source_yields

        m = CollectionMetrics(
            run_id="run-no-latency", source_name="test_source",
            discovered=5, fetched=5, new_evidence=1, duplicates=4,
        )
        _record_source_yields(temp_db, "run-no-latency", {"test_source": m})

        row = temp_db.execute(
            "SELECT latency_seconds FROM source_runs WHERE run_id = ?",
            ("run-no-latency",),
        ).fetchone()

        assert row["latency_seconds"] is None, (
            "latency_seconds should be NULL when not provided"
        )

    def test_run_collection_measures_latency(self, temp_db):
        """run_collection must measure per-source latency and pass to _record_source_yields."""
        # Insert a registry entry for Companies House to find
        _insert_registry(temp_db, "TestCo", jurisdiction="GB", companies_house_number="00000000")

        with patch("kassandra.collector.CompaniesHouseClient") as mock_ch:
            mock_client = MagicMock()
            mock_client.available = True
            mock_client.get_filing_history.return_value = {
                "items": [
                    {
                        "description": "accounts",
                        "date": "2025-06-01",
                        "category": "accounts",
                    }
                ]
            }
            mock_ch.return_value = mock_client

            # Mock other sources to return empty
            with patch("kassandra.collector.GazetteClient") as mock_gaz:
                mock_gaz.return_value.fetch_insolvency_feed.return_value = []
            with patch("kassandra.collector.WebMonitor") as mock_wm:
                mock_wm.return_value.monitor_company.return_value = 0
            with patch("kassandra.collector.HandelsregisterClient") as mock_hr:
                mock_hr.side_effect = Exception("not available")
            with patch("kassandra.collector.BodaccClient") as mock_bod:
                mock_bod.return_value.fetch_notices.return_value = []
            with patch("kassandra.collector.BorMeClient") as mock_bor:
                mock_bor.return_value.fetch_feed.return_value = []

            from kassandra.collector import run_collection
            metrics = run_collection(temp_db, sources=["companies_house"])

        assert "companies_house" in metrics

        # Verify latency was recorded in source_runs
        row = temp_db.execute(
            "SELECT latency_seconds FROM source_runs WHERE run_id = ? AND source_name = ?",
            (metrics["companies_house"].run_id, "companies_house"),
        ).fetchone()

        assert row is not None, "source_runs row must exist"
        assert row["latency_seconds"] is not None, "latency must be recorded"
        assert row["latency_seconds"] >= 0, "latency must be non-negative"


# ── CollectionMetrics counting correctness ────────────────────────────────────


class TestCollectionMetricsCounting:
    """CollectionMetrics must correctly track new/existing evidence and events."""

    def test_fetched_equals_new_plus_duplicates(self, temp_db):
        """fetched = new_evidence + duplicates."""
        m = CollectionMetrics(
            run_id="r1", source_name="test",
            discovered=100, fetched=100,
            new_evidence=20, duplicates=80,
        )
        assert m.fetched == m.new_evidence + m.duplicates
        m.assert_reconciled()

    def test_assert_reconciled_rejects_mismatch(self, temp_db):
        """assert_reconciled raises when fetched != new + dup."""
        m = CollectionMetrics(
            run_id="r1", source_name="test",
            fetched=100, new_evidence=20, duplicates=70,
        )
        with pytest.raises(ValueError, match="fetched must equal"):
            m.assert_reconciled()

    def test_unconfirmed_not_exceed_candidates(self, temp_db):
        """unconfirmed must not exceed candidates."""
        m = CollectionMetrics(
            run_id="r1", source_name="test",
            candidates=5, unconfirmed=6,
        )
        with pytest.raises(ValueError, match="unconfirmed must not exceed"):
            m.assert_reconciled()

    def test_events_not_exceed_candidates(self, temp_db):
        """inserted + duplicate events must not exceed candidates."""
        m = CollectionMetrics(
            run_id="r1", source_name="test",
            candidates=3, events_created=2, duplicate_events=2,
        )
        with pytest.raises(ValueError, match="inserted plus duplicate events"):
            m.assert_reconciled()

    def test_negative_values_rejected(self, temp_db):
        """Negative counters are always invalid."""
        m = CollectionMetrics(
            run_id="r1", source_name="test",
            new_evidence=-1,
        )
        with pytest.raises(ValueError, match="negative collection counters"):
            m.assert_reconciled()


# ── Handelsregister collector integration ─────────────────────────────────────


class TestHandelsregisterCollectorIntegration:
    """Collector must call HandelsregisterClient.collect_for_company, not search_and_store."""

    def test_collect_handelsregister_calls_collect_for_company(self, temp_db):
        """_collect_handelsregister must use collect_for_company_metrics, not int API."""
        from kassandra.collector import _collect_handelsregister
        from kassandra.contracts import CollectionMetrics

        _insert_registry(temp_db, "Siemens AG", jurisdiction="DE")

        with patch("kassandra.collector.HandelsregisterClient") as mock_cls:
            mock_client = MagicMock()
            mock_client.collect_for_company_metrics.return_value = CollectionMetrics(
                run_id="run-test-de", source_name="handelsregister_de",
                discovered=1, fetched=2, new_evidence=1, duplicates=1,
                candidates=2, events_created=2, duplicate_events=0,
            )
            mock_cls.return_value = mock_client

            metrics = _collect_handelsregister(temp_db, "run-test-de")

        assert metrics.source_name == "handelsregister_de"
        assert metrics.discovered == 1
        assert metrics.fetched == 2
        assert metrics.new_evidence == 1
        assert metrics.duplicates == 1
        assert metrics.candidates == 2
        assert metrics.events_created == 2
        assert metrics.duplicate_events == 0
        assert metrics.errors == 0
        metrics.assert_reconciled()
        mock_client.collect_for_company_metrics.assert_called_once_with(
            db=temp_db, registry_id=1, company_name="Siemens AG", run_id="run-test-de",
        )
        # search_and_store must NOT exist on the client
        from kassandra.sources.handelsregister import HandelsregisterClient
        assert not hasattr(HandelsregisterClient, "search_and_store")
