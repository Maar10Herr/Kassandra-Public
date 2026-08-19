"""Tests for Handelsregister (German commercial register) source adapter."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kassandra.contracts import CollectionMetrics


def _make_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT,
            jurisdiction TEXT, status TEXT, lei TEXT, isin TEXT,
            domain TEXT, companies_house_number TEXT, incorporation_date TEXT,
            registered_address TEXT, raw_json TEXT,
            resolved_at TEXT, updated_at TEXT, ir_url TEXT, feed_url TEXT
        );
        CREATE TABLE IF NOT EXISTS evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL,
            source_url TEXT NOT NULL, retrieval_time TEXT NOT NULL,
            publication_time TEXT, publication_time_confidence TEXT,
            first_seen_time TEXT NOT NULL DEFAULT (datetime('now')),
            extraction_method TEXT NOT NULL, parser_version TEXT NOT NULL,
            content_type TEXT, content_length INTEGER, excerpt TEXT,
            source_reliability REAL, corroborated_by TEXT, raw_headers TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_id INTEGER NOT NULL REFERENCES evidence(id),
            registry_id INTEGER NOT NULL,
            event_type TEXT NOT NULL, event_subtype TEXT,
            severity TEXT, confidence REAL NOT NULL DEFAULT 1.0,
            description TEXT,
            extracted_at TEXT NOT NULL DEFAULT (datetime('now')),
            source_claims_directly BOOLEAN NOT NULL DEFAULT 1,
            raw_event_json TEXT,
            UNIQUE(evidence_id, event_type, registry_id)
        );
    """)
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
        ("Siemens AG", "DE"),
    )
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
        ("Healthy GmbH", "DE"),
    )
    db.commit()
    return db


# ── Status classification ────────────────────────────────────────────────


def test_classify_insolvency_status():
    from kassandra.sources.handelsregister import HandelsregisterClient

    client = HandelsregisterClient()
    results = [{"name": "Test GmbH", "status": "Insolvenzverfahren eröffnet",
                 "register_num": "HRB 12345", "court": "Berlin HRB 12345",
                 "state": "Berlin"}]
    events = client.classify_results(results)
    assert len(events) == 1
    assert events[0]["event_type"] == "insolvency"
    assert events[0]["severity"] == "critical"
    assert events[0]["confidence"] == 0.85


def test_classify_geloscht_status():
    from kassandra.sources.handelsregister import HandelsregisterClient

    client = HandelsregisterClient()
    results = [{"name": "Dead GmbH", "status": "gelöscht",
                 "register_num": "HRB 99999", "court": "München HRB 99999",
                 "state": "Bayern"}]
    events = client.classify_results(results)
    assert len(events) == 1
    assert events[0]["event_type"] == "insolvency"
    assert events[0]["severity"] == "critical"


def test_classify_routine_status_returns_no_events():
    from kassandra.sources.handelsregister import HandelsregisterClient

    client = HandelsregisterClient()
    results = [{"name": "Alive AG", "status": "eingetragen",
                 "register_num": "HRB 55555", "court": "Hamburg HRB 55555",
                 "state": "Hamburg"}]
    events = client.classify_results(results)
    assert events == []


def test_classify_keyword_fallback():
    from kassandra.sources.handelsregister import HandelsregisterClient

    client = HandelsregisterClient()
    results = [{"name": "WeirdCo", "status": "irgendwas mit insolvenz drin",
                 "register_num": "HRA 111", "court": "Köln HRA 111",
                 "state": "NRW"}]
    events = client.classify_results(results)
    assert len(events) == 1
    assert events[0]["event_type"] == "unconfirmed_adverse"
    assert events[0]["severity"] == "low"


# ── HTML parsing (bundesAPI format) ───────────────────────────────────────


MOCK_HTML = """<html><body>
<table role="grid">
  <tr data-ri="0">
    <td>1</td>
    <td>Charlottenburg (Berlin) HRB 12345 B</td>
    <td>Siemens AG</td>
    <td>Berlin</td>
    <td>eingetragen</td>
    <td>Dokumente</td>
  </tr>
  <tr data-ri="1">
    <td>2</td>
    <td>München HRB 99999</td>
    <td>Test GmbH i.L.</td>
    <td>Bayern</td>
    <td>in liquidation</td>
    <td></td>
  </tr>
</table>
</body></html>"""


def test_parse_search_results():
    from kassandra.sources.handelsregister import _parse_search_results

    results = _parse_search_results(MOCK_HTML)
    assert len(results) == 2

    assert results[0]["name"] == "Siemens AG"
    assert results[0]["register_num"] == "HRB 12345 B"
    assert results[0]["state"] == "Berlin"
    assert results[0]["status"] == "eingetragen"

    assert results[1]["name"] == "Test GmbH i.L."
    assert results[1]["register_num"] == "HRB 99999"
    assert results[1]["status"] == "in liquidation"


def test_parse_no_results():
    from kassandra.sources.handelsregister import _parse_search_results

    results = _parse_search_results("<html><body>No results</body></html>")
    assert results == []


# ── Integration: collect for company (mocked) ────────────────────────────


def test_collect_for_company_stores_evidence_and_events():
    """collect_for_company (legacy int API) stores evidence and events correctly."""
    from kassandra.sources.handelsregister import HandelsregisterClient

    db = _make_db()
    client = HandelsregisterClient()

    with patch.object(client, "search", return_value=[
        {"name": "Siemens AG", "court": "Berlin HRB 12345",
         "register_num": "HRB 12345", "state": "Berlin",
         "status": "Insolvenzverfahren eröffnet", "documents": "5"},
    ]):
        created = client.collect_for_company(
            db=db, registry_id=1, company_name="Siemens AG",
        )

    assert created == 1
    evidence_count = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    assert evidence_count == 1
    event = db.execute(
        "SELECT event_type, severity, confidence FROM events LIMIT 1"
    ).fetchone()
    assert event["event_type"] == "insolvency"
    assert event["severity"] == "critical"
    assert event["confidence"] == 0.85


def test_collect_for_company_metrics_insolvency():
    """collect_for_company_metrics returns reconciled CollectionMetrics for one result."""
    from kassandra.sources.handelsregister import HandelsregisterClient

    db = _make_db()
    client = HandelsregisterClient()

    with patch.object(client, "search", return_value=[
        {"name": "Siemens AG", "court": "Berlin HRB 12345",
         "register_num": "HRB 12345", "state": "Berlin",
         "status": "Insolvenzverfahren eröffnet", "documents": "5"},
    ]):
        m = client.collect_for_company_metrics(
            db=db, registry_id=1, company_name="Siemens AG",
        )

    assert m.discovered == 1
    assert m.fetched == 1
    assert m.new_evidence == 1
    assert m.duplicates == 0
    assert m.candidates == 1
    assert m.events_created == 1
    assert m.duplicate_events == 0
    assert m.errors == 0
    m.assert_reconciled()


def test_collect_for_company_metrics_zero_results():
    """Zero search results yields all-zero CollectionMetrics that reconciles."""
    from kassandra.sources.handelsregister import HandelsregisterClient

    db = _make_db()
    client = HandelsregisterClient()

    with patch.object(client, "search", return_value=[]):
        m = client.collect_for_company_metrics(
            db=db, registry_id=1, company_name="Nonexistent GmbH",
        )

    assert m.discovered == 0
    assert m.fetched == 0
    assert m.new_evidence == 0
    assert m.duplicates == 0
    assert m.candidates == 0
    assert m.events_created == 0
    assert m.duplicate_events == 0
    m.assert_reconciled()


def test_collect_for_company_metrics_routine_status_no_event():
    """One result with routine status — evidence stored but zero events."""
    from kassandra.sources.handelsregister import HandelsregisterClient

    db = _make_db()
    client = HandelsregisterClient()

    with patch.object(client, "search", return_value=[
        {"name": "Healthy GmbH", "court": "München HRB 55555",
         "register_num": "HRB 55555", "state": "Bayern",
         "status": "eingetragen", "documents": "0"},
    ]):
        m = client.collect_for_company_metrics(
            db=db, registry_id=2, company_name="Healthy GmbH",
        )

    assert m.discovered == 1
    assert m.fetched == 1
    assert m.new_evidence == 1
    assert m.duplicates == 0
    assert m.candidates == 0      # routine status → no classified event
    assert m.events_created == 0
    m.assert_reconciled()


def test_collect_for_company_metrics_two_events():
    """One result classified into two events counts both as candidates."""
    from kassandra.sources.handelsregister import HandelsregisterClient

    db = _make_db()
    client = HandelsregisterClient()

    # Patch classify_results to return two events from one result
    two_events = [
        {"name": "Test GmbH", "court": "Berlin HRB 88888",
         "register_num": "HRB 88888", "state": "Berlin",
         "status": "Insolvenzverfahren eröffnet", "documents": "1"},
    ]
    with patch.object(client, "search", return_value=two_events):
        with patch.object(client, "classify_results", return_value=[
            {"event_type": "insolvency", "severity": "critical",
             "confidence": 0.85,
             "description": "Insolvency opened",
             "source_detail": {}},
            {"event_type": "restructuring", "severity": "high",
             "confidence": 0.85,
             "description": "Restructuring filed",
             "source_detail": {}},
        ]):
            m = client.collect_for_company_metrics(
                db=db, registry_id=1, company_name="Test GmbH",
            )

    assert m.discovered == 1
    assert m.fetched == 1
    assert m.new_evidence == 1
    assert m.candidates == 2
    assert m.events_created == 2
    assert m.duplicate_events == 0
    m.assert_reconciled()


def test_collect_for_company_metrics_duplicate_on_second_run():
    """Second run with same data: evidence and events are duplicates."""
    from kassandra.sources.handelsregister import HandelsregisterClient

    db = _make_db()
    client = HandelsregisterClient()

    search_results = [
        {"name": "Siemens AG", "court": "Berlin HRB 12345",
         "register_num": "HRB 12345", "state": "Berlin",
         "status": "Insolvenzverfahren eröffnet", "documents": "5"},
    ]

    # First run
    with patch.object(client, "search", return_value=search_results):
        m1 = client.collect_for_company_metrics(
            db=db, registry_id=1, company_name="Siemens AG",
        )
    assert m1.new_evidence == 1
    assert m1.duplicates == 0
    assert m1.events_created == 1
    assert m1.duplicate_events == 0
    m1.assert_reconciled()

    # Second run — same data
    with patch.object(client, "search", return_value=search_results):
        m2 = client.collect_for_company_metrics(
            db=db, registry_id=1, company_name="Siemens AG",
        )
    assert m2.new_evidence == 0
    assert m2.duplicates == 1
    assert m2.events_created == 0
    assert m2.duplicate_events == 1
    m2.assert_reconciled()

    # Legacy int API also returns correct count on second run
    with patch.object(client, "search", return_value=search_results):
        created = client.collect_for_company(
            db=db, registry_id=1, company_name="Siemens AG",
        )
    assert created == 0


def test_collect_for_company_metrics_multiple_companies_aggregation():
    """Aggregated metrics across multiple companies are truthful."""
    from kassandra.sources.handelsregister import HandelsregisterClient

    db = _make_db()
    client = HandelsregisterClient()

    # Siemens → one insolvency event
    siemens_results = [
        {"name": "Siemens AG", "court": "Berlin HRB 12345",
         "register_num": "HRB 12345", "state": "Berlin",
         "status": "Insolvenzverfahren eröffnet", "documents": "5"},
    ]
    # Healthy GmbH → routine, no event
    healthy_results = [
        {"name": "Healthy GmbH", "court": "München HRB 55555",
         "register_num": "HRB 55555", "state": "Bayern",
         "status": "eingetragen", "documents": "0"},
    ]

    with patch.object(client, "search", side_effect=[siemens_results, healthy_results]):
        m1 = client.collect_for_company_metrics(
            db=db, registry_id=1, company_name="Siemens AG",
        )
        m2 = client.collect_for_company_metrics(
            db=db, registry_id=2, company_name="Healthy GmbH",
        )

    # Manual aggregation (simulating what _collect_handelsregister does)
    from kassandra.collector import CollectionMetrics as CM
    aggregated = CM(
        run_id="test", source_name="handelsregister_de",
        discovered=m1.discovered + m2.discovered,
        fetched=m1.fetched + m2.fetched,
        new_evidence=m1.new_evidence + m2.new_evidence,
        duplicates=m1.duplicates + m2.duplicates,
        candidates=m1.candidates + m2.candidates,
        events_created=m1.events_created + m2.events_created,
        duplicate_events=m1.duplicate_events + m2.duplicate_events,
    )
    assert aggregated.discovered == 2
    assert aggregated.fetched == 2
    assert aggregated.new_evidence == 2
    assert aggregated.candidates == 1  # only Siemens classifies
    assert aggregated.events_created == 1
    aggregated.assert_reconciled()


def test_unavailable_client_returns_zero():
    from kassandra.sources.handelsregister import HandelsregisterClient

    client = HandelsregisterClient()
    # available is a @property — mock the internal _available flag directly
    client._available = False
    results = client.search("Siemens AG")
    assert results == []
