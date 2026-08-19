"""Focused source-accounting contracts for legal-notice adapters."""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from kassandra.contracts import CollectionMetrics, EvidenceResult, EventResult, MatchResult
from kassandra.db import _connect, migrate


@pytest.fixture
def db(tmp_path):
    conn = _connect(tmp_path / "accounting.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO registry (canonical_name, jurisdiction) VALUES ('ACME SA', 'FR')"
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.mark.parametrize(
    ("module_name", "client_name", "method_name", "notice"),
    [
        ("kassandra.sources.bodacc", "BodaccClient", "process_notices_for_company",
         {"commercant": "ACME SA", "url_complete": "https://example.test/bodacc", "familleavis_lib": "Unknown"}),
        ("kassandra.sources.borme", "BorMeClient", "process_notices_for_company",
         {"title": "ACME SA", "link": "https://example.test/borme", "description": "Unknown"}),
        ("kassandra.sources.gazette", "GazetteClient", "_process_notices_for_company",
         {"title": "ACME SA", "link": "https://example.test/gazette", "published": "2026-01-01", "summary": "Unknown"}),
    ],
)
def test_notice_adapter_duplicate_evidence_and_unclassified_notice_are_not_new_or_events(
    db, module_name, client_name, method_name, notice
):
    """Adapter outcomes classify duplicate evidence and no-event notices explicitly."""
    module = __import__(module_name, fromlist=[client_name])
    client = getattr(module, client_name)()
    method = getattr(client, method_name)

    with patch.object(module, "_match_notice_to_registry", return_value=MatchResult(
        status="confirmed", matched_registry_id=1
    )), patch.object(module, "store_evidence", return_value=EvidenceResult(
        evidence_id=10, is_new=False, content_hash="a" * 64
    )), patch.object(module, "store_event") as store_event:
        if module_name.endswith("bodacc"):
            with patch.object(module, "_classify_annonce", return_value=(None, None)):
                outcome = method(db, 1, "ACME SA", [notice])
        elif module_name.endswith("borme"):
            with patch.object(module, "_classify_borme_notice", return_value=(None, None)):
                outcome = method(db, 1, "ACME SA", [notice])
        else:
            with patch.object(client, "classify_notice", return_value=(None, None)):
                outcome = method(db, 1, "ACME SA", [notice])

    assert isinstance(outcome, CollectionMetrics)
    assert outcome.new_evidence == 0
    assert outcome.duplicates == 1
    assert outcome.candidates == 1
    assert outcome.events_created == 0
    assert outcome.duplicate_events == 0
    store_event.assert_not_called()


@pytest.mark.parametrize(
    ("module_name", "client_name", "method_name", "notice"),
    [
        ("kassandra.sources.bodacc", "BodaccClient", "process_notices_for_company",
         {"commercant": "ACME SA", "url_complete": "https://example.test/bodacc", "familleavis_lib": "Procédures collectives"}),
        ("kassandra.sources.borme", "BorMeClient", "process_notices_for_company",
         {"title": "ACME SA", "link": "https://example.test/borme", "description": "liquidación"}),
        ("kassandra.sources.gazette", "GazetteClient", "_process_notices_for_company",
         {"title": "ACME SA", "link": "https://example.test/gazette", "published": "2026-01-01", "summary": "winding-up"}),
    ],
)
def test_notice_adapter_duplicate_event_is_not_inserted_event(
    db, module_name, client_name, method_name, notice
):
    """Only EventResult.status='inserted' may increase inserted-event counters."""
    module = __import__(module_name, fromlist=[client_name])
    client = getattr(module, client_name)()
    method = getattr(client, method_name)

    with patch.object(module, "_match_notice_to_registry", return_value=MatchResult(
        status="confirmed", matched_registry_id=1
    )), patch.object(module, "store_evidence", return_value=EvidenceResult(
        evidence_id=10, is_new=True, content_hash="a" * 64
    )), patch.object(module, "store_event", return_value=EventResult(
        event_id=7, status="duplicate"
    )):
        outcome = method(db, 1, "ACME SA", [notice])

    assert isinstance(outcome, CollectionMetrics)
    assert outcome.new_evidence == 1
    assert outcome.candidates == 1
    assert outcome.events_created == 0
    assert outcome.duplicate_events == 1


@pytest.mark.parametrize(
    ("module_name", "client_name", "method_name", "notice"),
    [
        ("kassandra.sources.bodacc", "BodaccClient", "process_notices_for_company",
         {"commercant": "ACME SA", "url_complete": "https://example.test/bodacc", "familleavis_lib": "Procédures collectives"}),
        ("kassandra.sources.borme", "BorMeClient", "process_notices_for_company",
         {"title": "ACME SA", "link": "https://example.test/borme", "description": "liquidación"}),
        ("kassandra.sources.gazette", "GazetteClient", "_process_notices_for_company",
         {"title": "ACME SA", "link": "https://example.test/gazette", "published": "2026-01-01", "summary": "winding-up"}),
    ],
)
def test_unconfirmed_notice_is_queued_not_an_event(db, module_name, client_name, method_name, notice):
    """Unconfirmed candidates are a separate queue outcome, not inserted events."""
    module = __import__(module_name, fromlist=[client_name])
    client = getattr(module, client_name)()
    method = getattr(client, method_name)

    with patch.object(module, "_match_notice_to_registry", return_value=MatchResult(
        status="unconfirmed_review_candidate", match_type="partial_overlap", confidence=0.4
    )), patch.object(module, "store_evidence", return_value=EvidenceResult(
        evidence_id=10, is_new=True, content_hash="a" * 64
    )), patch.object(module, "store_event") as store_event, patch.object(module, "queue_unconfirmed_match"):
        outcome = method(db, 1, "ACME SA", [notice])

    assert isinstance(outcome, CollectionMetrics)
    assert outcome.new_evidence == 1
    assert outcome.candidates == 1
    assert outcome.unconfirmed == 1
    assert outcome.events_created == 0
    assert outcome.duplicate_events == 0
    store_event.assert_not_called()


def test_bodacc_collector_aggregates_adapter_outcome_without_raw_match_counts(db):
    """Collector must aggregate the adapter's canonical outcome, never len(matches)."""
    from kassandra.collector import _collect_bodacc

    db.execute("UPDATE registry SET jurisdiction = 'FR' WHERE id = 1")
    adapter_outcome = CollectionMetrics(
        run_id="adapter", source_name="bodacc_fr", discovered=1, fetched=1,
        new_evidence=0, duplicates=1, candidates=1, unconfirmed=1,
        events_created=0, duplicate_events=0,
    )
    client = MagicMock()
    client.fetch_notices.return_value = [{"commercant": "ACME SA"}]
    client.filter_notices_for_company.return_value = [{"commercant": "ACME SA"}]
    client.process_notices_for_company.return_value = adapter_outcome

    with patch("kassandra.collector.BodaccClient", return_value=client):
        outcome = _collect_bodacc(db, "run-1")

    assert outcome.new_evidence == 0
    assert outcome.duplicates == 1
    assert outcome.candidates == 1
    assert outcome.unconfirmed == 1
    assert outcome.events_created == 0
