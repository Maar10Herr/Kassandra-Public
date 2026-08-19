"""Tests for UK Gazette source adapter — single-fetch path."""

import sqlite3
from unittest.mock import MagicMock, patch

import httpx
import pytest

from kassandra.contracts import CollectionMetrics


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_db() -> sqlite3.Connection:
    """Create an in-memory database with the minimum schema for testing."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT,
            jurisdiction TEXT,
            companies_house_number TEXT,
            lei TEXT
        );
        CREATE TABLE IF NOT EXISTS evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL,
            source_url TEXT NOT NULL,
            retrieval_time TEXT NOT NULL,
            publication_time TEXT,
            publication_time_confidence TEXT,
            first_seen_time TEXT NOT NULL DEFAULT (datetime('now')),
            extraction_method TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            content_type TEXT,
            content_length INTEGER,
            excerpt TEXT,
            source_reliability REAL,
            corroborated_by TEXT,
            raw_headers TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_id INTEGER NOT NULL REFERENCES evidence(id),
            registry_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            event_subtype TEXT,
            severity TEXT,
            confidence REAL NOT NULL DEFAULT 1.0,
            description TEXT,
            extracted_at TEXT NOT NULL DEFAULT (datetime('now')),
            source_claims_directly BOOLEAN NOT NULL DEFAULT 1,
            raw_event_json TEXT
        );
    """)
    db.execute("INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
               ("ACME UK LTD", "GB"))
    db.execute("INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
               ("BRITISH GAS PLC", "england-wales"))
    db.execute("INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
               ("DEUTSCHE BANK AG", "DE"))
    db.commit()
    return db


def _make_notice(title: str, summary: str = "", link: str = "") -> dict:
    return {
        "title": title,
        "link": link or f"https://www.thegazette.co.uk/notice/{hash(title)}",
        "published": "2026-06-15T00:00:00Z",
        "summary": summary,
        "id": f"notice-{hash(title)}",
    }


# ── Single-fetch verification ─────────────────────────────────────────────────

_RSS_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Insolvency Notices</title>
<item><title>ACME UK LTD — Winding-Up Order</title>
<link>https://www.thegazette.co.uk/notice/1</link>
<pubDate>Mon, 15 Jun 2026</pubDate>
<description>Winding-up order for ACME UK LTD</description>
<guid>notice-1</guid></item>
<item><title>British Gas PLC — Administration Application</title>
<link>https://www.thegazette.co.uk/notice/2</link>
<pubDate>Mon, 15 Jun 2026</pubDate>
<description>Administration application for British Gas</description>
<guid>notice-2</guid></item>
</channel></rss>"""


def test_single_fetch_per_collection_cycle():
    """One _collect_gazette call = exactly one Gazette HTTP GET."""
    from kassandra.sources.gazette import GAZETTE_INSOLVENCY_FEED
    from kassandra.collector import _collect_gazette

    db = _make_db()

    fetch_count = 0

    def counting_get(url, **kwargs):
        nonlocal fetch_count
        fetch_count += 1
        if GAZETTE_INSOLVENCY_FEED in url:
            resp = MagicMock(spec=httpx.Response)
            resp.raise_for_status.return_value = None
            resp.text = _RSS_BODY
            return resp
        raise ValueError(f"Unexpected URL: {url}")

    # Mock httpx.Client so GazetteClient.__init__ gets a controlled client
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.side_effect = counting_get
        mock_client_cls.return_value = mock_client

        metrics = _collect_gazette(db, run_id="test-single-fetch")

    # The critical assertion: ONE HTTP request, not one per company
    assert fetch_count == 1, (
        f"Expected 1 Gazette HTTP GET, got {fetch_count}. "
        "Per-company feed fetching causes 429 rate-limiting."
    )
    # Should process notices for matching companies
    assert metrics.new_evidence >= 0


def test_collect_for_company_from_notices_no_http():
    """collect_for_company_from_notices must make zero HTTP calls."""
    from kassandra.sources.gazette import GazetteClient
    db = _make_db()

    with patch("httpx.Client"):
        client = GazetteClient()

    notices = [
        _make_notice("ACME UK LTD — Winding-Up Order", "winding up order"),
        _make_notice("British Gas PLC — Administration", "administration"),
        _make_notice("SOMECO — Something Else", "something"),
    ]

    # Monkey-patch the client's _client so we catch any accidental requests
    client._client = MagicMock()
    collected = client.collect_for_company_from_notices(
        db=db, registry_id=1, company_name="ACME UK LTD", notices=notices,
    )

    # No HTTP calls
    client._client.get.assert_not_called()

    # Should match at least the ACME notice and return typed accounting.
    assert isinstance(collected, CollectionMetrics)
    assert collected.new_evidence > 0


def test_deprecated_path_still_works():
    """collect_for_company still works for callers that haven't migrated."""
    from kassandra.sources.gazette import GazetteClient
    db = _make_db()

    with patch("httpx.Client"):
        client = GazetteClient()

    notices = [
        _make_notice("ACME UK LTD — Winding-Up Order", "winding up order"),
    ]

    with patch.object(client, "search_company", return_value=notices):
        collected = client.collect_for_company(
            db=db, registry_id=1, company_name="ACME UK LTD",
        )

    assert isinstance(collected, CollectionMetrics)
    assert collected.new_evidence > 0


def test_filter_notices_token_overlap():
    """_filter_notices_for_company uses token overlap, not substring matching."""
    from kassandra.sources.gazette import GazetteClient

    with patch("httpx.Client"):
        client = GazetteClient()

    notices = [
        _make_notice("British Gas PLC — Administration Order", "administration order for British Gas"),
        _make_notice("ACME UK LTD — Winding-Up", "winding-up"),
        _make_notice("GASTECH HOLDINGS — Liquidation", "liquidation"),
    ]

    result = client._filter_notices_for_company("British Gas PLC", notices)
    assert len(result) == 1
    assert "British Gas" in result[0]["title"]

    result = client._filter_notices_for_company("ACME UK LTD", notices)
    assert len(result) == 1
    assert "ACME" in result[0]["title"]


def test_classify_insolvency():
    from kassandra.sources.gazette import GazetteClient

    with patch("httpx.Client"):
        client = GazetteClient()

    event_type, severity = client.classify_notice(
        "ACME UK LTD — Winding-Up Order", "A winding-up order was made"
    )
    assert event_type == "insolvency"
    assert severity == "critical"


def test_classify_restructuring():
    from kassandra.sources.gazette import GazetteClient

    with patch("httpx.Client"):
        client = GazetteClient()

    event_type, severity = client.classify_notice(
        "British Gas PLC — Administration Application",
        "Notice of administrator appointment",
    )
    assert event_type == "restructuring"
    assert severity == "high"


def test_filter_notices_ignores_legal_suffix_only_overlap():
    """A UK subsidiary named '* LIMITED' must not match every Gazette LIMITED notice."""
    from kassandra.sources.gazette import GazetteClient

    with patch("httpx.Client"):
        client = GazetteClient()

    notices = [
        _make_notice("WELCOME HOMES (SCOTLAND) LIMITED — Winding-Up", "winding-up"),
        _make_notice("GENESIS INVESTMENTS (ABERDEEN) LIMITED — Liquidation", "liquidation"),
    ]

    assert client._filter_notices_for_company("INVENSYS LIMITED", notices) == []
    assert client._filter_notices_for_company("AIRBUS DEFENCE AND SPACE LIMITED", notices) == []


def test_filter_notices_matches_single_distinctive_token_company():
    from kassandra.sources.gazette import GazetteClient

    with patch("httpx.Client"):
        client = GazetteClient()

    notices = [
        _make_notice("INVENSYS LIMITED — Administration Order", "administration order"),
        _make_notice("OTHER LIMITED — Liquidation", "liquidation"),
    ]
    result = client._filter_notices_for_company("INVENSYS LIMITED", notices)
    assert len(result) == 1
    assert "INVENSYS" in result[0]["title"]
