"""Tests for BODACC (French commercial announcements) source adapter."""

import json
import sqlite3
from unittest.mock import MagicMock, patch

import httpx
import pytest


def _make_db() -> sqlite3.Connection:
    """Create an in-memory database with minimum schema for testing."""
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
            resolved_at TEXT, updated_at TEXT, ir_url TEXT, feed_url TEXT,
            siren TEXT, spanish_tax_id TEXT
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
        CREATE TABLE IF NOT EXISTS unconfirmed_match_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL, source_entity_name TEXT NOT NULL,
            candidate_registry_id INTEGER, candidate_registry_name TEXT,
            match_type TEXT NOT NULL, match_confidence REAL NOT NULL,
            evidence_id INTEGER, evidence_excerpt TEXT, status TEXT NOT NULL DEFAULT 'pending',
            resolution TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unconfirmed_dedup
            ON unconfirmed_match_queue(source_name, source_entity_name, candidate_registry_id)
            WHERE resolution IS NULL;
    """)
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
        ("LVMH", "FR"),
    )
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
        ("TotalEnergies SE", "FR"),
    )
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
        ("Airbus SE", "NL"),  # Dutch but might have French registrations
    )
    db.commit()
    return db


# ── Sample BODACC API responses ──────────────────────────────────────────

def _make_bodacc_notice(
    commercant: str = "LVMH MOET HENNESSY LOUIS VUITTON",
    famille: str = "Procédures collectives",
    dateparution: str = "2026-06-15",
    notice_id: str = "A2026006001234",
) -> dict:
    return {
        "id": notice_id,
        "parution": "20260060",
        "dateparution": dateparution,
        "commercant": commercant,
        "familleavis_lib": famille,
        "categorieavis_lib": "Ouverture de procédure",
        "tribunal_lib": "Tribunal de Commerce de Paris",
        "departement_lib": "Paris",
        "region_lib": "Île-de-France",
        "texteavis": f"Avis concernant {commercant}...",
        "url_complete": f"https://www.bodacc.fr/pages/annonces-commerciales-detail/?q.id=id:{notice_id}",
    }


MOCK_BODACC_RESPONSE = {
    "total_count": 2,
    "results": [
        _make_bodacc_notice(
            commercant="LVMH MOET HENNESSY LOUIS VUITTON",
            famille="Procédures collectives",
            dateparution="2026-06-15",
            notice_id="A2026006001234",
        ),
        _make_bodacc_notice(
            commercant="TOTALENERGIES SE",
            famille="Radiations",
            dateparution="2026-06-10",
            notice_id="A2026006001235",
        ),
    ],
}


# ── Matching tests ───────────────────────────────────────────────────────

def test_notice_matches_company_basic():
    from kassandra.sources.bodacc import _notice_matches_company

    assert _notice_matches_company("LVMH", "LVMH MOET HENNESSY LOUIS VUITTON")
    assert _notice_matches_company("TotalEnergies", "TOTALENERGIES SE")
    assert _notice_matches_company("Airbus", "AIRBUS SAS")


def test_notice_matches_company_conservative():
    """Generic legal-suffix-only overlap must not match."""
    from kassandra.sources.bodacc import _notice_matches_company

    # "SAS" is a stripped token, so a company named "SAS" won't match on that alone
    assert not _notice_matches_company("SAS", "SAS DISTRIBUTION")
    # A company composed entirely of stripped tokens must not match
    assert not _notice_matches_company("THE LIMITED", "THE LIMITED COMPANY LTD")


def test_notice_matches_hyphenated():
    from kassandra.sources.bodacc import _notice_matches_company

    assert _notice_matches_company(
        "SAINT-GOBAIN", "SAINT GOBAIN DISTRIBUTION BÂTIMENT"
    )


def test_notice_no_match_different_company():
    from kassandra.sources.bodacc import _notice_matches_company

    assert not _notice_matches_company(
        "LVMH", "SOCIETE GENERALE SA"
    )
    assert not _notice_matches_company(
        "TotalEnergies", "RENAULT SAS"
    )


def test_notice_matches_subset_tokens():
    """All distinctive tokens from company_name must appear in commercant."""
    from kassandra.sources.bodacc import _notice_matches_company

    # "Moet" is distinctive and in the notice
    assert _notice_matches_company("Moet", "LVMH MOET HENNESSY LOUIS VUITTON")
    # "Moet Chandon" requires BOTH tokens
    assert _notice_matches_company(
        "Moet Chandon", "LVMH MOET HENNESSY CHANDON"
    )


# ── Classification tests ─────────────────────────────────────────────────

def test_classify_procedures_collectives():
    from kassandra.sources.bodacc import _classify_annonce

    event_type, severity = _classify_annonce("Procédures collectives")
    assert event_type == "insolvency"
    assert severity == "critical"


def test_classify_radiations():
    from kassandra.sources.bodacc import _classify_annonce

    event_type, severity = _classify_annonce("Radiations")
    assert event_type == "restructuring"
    assert severity == "high"


def test_classify_unknown_family():
    from kassandra.sources.bodacc import _classify_annonce

    event_type, severity = _classify_annonce("Avis de dépôt des comptes")
    assert event_type is None
    assert severity is None


# ── Client integration tests ─────────────────────────────────────────────

def test_filter_notices_for_company():
    from kassandra.sources.bodacc import BodaccClient

    client = BodaccClient()
    notices = MOCK_BODACC_RESPONSE["results"]

    matches = client.filter_notices_for_company("LVMH", notices)
    assert len(matches) == 1
    assert matches[0]["commercant"].startswith("LVMH")

    matches = client.filter_notices_for_company("TotalEnergies", notices)
    assert len(matches) == 1
    assert matches[0]["commercant"].startswith("TOTALENERGIES")

    matches = client.filter_notices_for_company("Renault", notices)
    assert matches == []


def test_process_notices_stores_evidence_and_events():
    from kassandra.sources.bodacc import BodaccClient

    db = _make_db()
    client = BodaccClient()

    notices = [
        _make_bodacc_notice(
            commercant="LVMH MOET HENNESSY LOUIS VUITTON",
            famille="Procédures collectives",
        ),
    ]

    collected = client.process_notices_for_company(
        db=db, registry_id=1, company_name="LVMH", notices=notices,
    )

    assert collected.new_evidence == 1
    assert collected.events_created == 0  # ambiguous → queued, not an event
    assert collected.unconfirmed >= 1     # routed to unconfirmed_match_queue

    # Verify evidence stored
    evidence_count = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    assert evidence_count == 1

    # Verify evidence content type and method
    evidence = db.execute("SELECT * FROM evidence LIMIT 1").fetchone()
    assert evidence["extraction_method"] == "bodacc_fr_api"
    assert evidence["source_reliability"] == 1.0
    assert evidence["content_type"] == "application/json"

    # LVMH has no SIREN, single-token company → queued, NOT in events table
    event_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert event_count == 0, "ambiguous match must not create an event"

    # Verify queue entry
    queue_row = db.execute(
        "SELECT * FROM unconfirmed_match_queue LIMIT 1"
    ).fetchone()
    assert queue_row is not None
    assert queue_row["source_name"] == "bodacc_fr"
    assert queue_row["status"] == "pending"


def test_process_notices_radiations_event():
    from kassandra.sources.bodacc import BodaccClient

    db = _make_db()
    client = BodaccClient()

    notices = [
        _make_bodacc_notice(
            commercant="TOTALENERGIES SE",
            famille="Radiations",
        ),
    ]

    collected = client.process_notices_for_company(
        db=db, registry_id=2, company_name="TotalEnergies", notices=notices,
    )

    assert collected.new_evidence == 1
    assert collected.events_created == 1

    event = db.execute(
        "SELECT event_type, severity FROM events LIMIT 1"
    ).fetchone()
    assert event["event_type"] == "restructuring"
    assert event["severity"] == "high"


def test_fetch_notices_mocked():
    """fetch_notices makes HTTP calls and parses JSON response."""
    from kassandra.sources.bodacc import BodaccClient, BODACC_API_BASE

    client = BodaccClient()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = MOCK_BODACC_RESPONSE

    with patch.object(client._client, "get", return_value=mock_response) as mock_get:
        notices = client.fetch_notices(days_back=30, family="Procédures collectives")

    # Verify HTTP call made
    mock_get.assert_called_once()
    call_args = mock_get.call_args
    assert call_args[0][0] == BODACC_API_BASE
    assert "Procédures collectives" in call_args[1]["params"]["where"]

    # Verify notices parsed
    assert len(notices) == 2
    assert notices[0]["commercant"].startswith("LVMH")


def test_fetch_notices_no_family_fetches_all_monitored():
    """When family=None, fetch across all MONITORED_FAMILIES."""
    from kassandra.sources.bodacc import BodaccClient

    client = BodaccClient()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"results": []}

    with patch.object(client._client, "get", return_value=mock_response) as mock_get:
        notices = client.fetch_notices(days_back=7, family=None)

    # Two families × one call each
    assert mock_get.call_count == 2


def test_fetch_notices_http_error_returns_empty():
    from kassandra.sources.bodacc import BodaccClient

    client = BodaccClient()

    with patch.object(client._client, "get", side_effect=httpx.HTTPError("timeout")):
        notices = client.fetch_notices(days_back=7, family="Procédures collectives")

    assert notices == []


def test_collect_for_company_integration():
    from kassandra.sources.bodacc import BodaccClient

    db = _make_db()
    client = BodaccClient()

    with patch.object(client, "fetch_notices", return_value=MOCK_BODACC_RESPONSE["results"]):
        collected = client.collect_for_company(
            db=db, registry_id=1, company_name="LVMH", days_back=30,
        )

    assert collected.new_evidence >= 1
    assert collected.events_created == 0  # ambiguous → queued, not an event
    assert collected.unconfirmed >= 1     # routed to unconfirmed_match_queue

    # Verify evidence stored
    evidence_count = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    assert evidence_count >= 1

    # LVMH single-token → queued, NOT in events table
    event_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert event_count == 0, "ambiguous match must not create an event"

    # Verify queue entry
    queue_row = db.execute(
        "SELECT * FROM unconfirmed_match_queue LIMIT 1"
    ).fetchone()
    assert queue_row is not None
    assert queue_row["source_name"] == "bodacc_fr"
    assert queue_row["status"] == "pending"


# ── Identifier-first policy regression tests ───────────────────────────

def _make_db_with_hermes() -> sqlite3.Connection:
    """DB with HERMES INTERNATIONAL (FR, with SIREN) and a DELPHINE company."""
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
            resolved_at TEXT, updated_at TEXT, ir_url TEXT, feed_url TEXT,
            siren TEXT, spanish_tax_id TEXT
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
        CREATE TABLE IF NOT EXISTS unconfirmed_match_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL, source_entity_name TEXT NOT NULL,
            candidate_registry_id INTEGER, candidate_registry_name TEXT,
            match_type TEXT NOT NULL, match_confidence REAL NOT NULL,
            evidence_id INTEGER, evidence_excerpt TEXT, status TEXT NOT NULL DEFAULT 'pending',
            resolution TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unconfirmed_dedup
            ON unconfirmed_match_queue(source_name, source_entity_name, candidate_registry_id)
            WHERE resolution IS NULL;
    """)
    # HERMES INTERNATIONAL (FR, with SIREN)
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction, siren) VALUES (?, ?, ?)",
        ("HERMES INTERNATIONAL", "FR", "572061123"),
    )
    # DELPHINE SAS (FR, no SIREN known)
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
        ("DELPHINE SAS", "FR"),
    )
    # A company without SIREN for exact-name testing
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
        ("TotalEnergies SE", "FR"),
    )
    # A company with known SIREN
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction, siren) VALUES (?, ?, ?)",
        ("RENAULT SAS", "FR", "351826335"),
    )
    # A Dutch company (should be rejected by jurisdiction)
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
        ("Airbus SE", "NL"),
    )
    db.commit()
    return db


def test_bodacc_hermes_gastronomie_must_not_match_hermes_international():
    """HERMES INTERNATIONAL must NOT match HERMES GASTRONOMIE via token overlap."""
    from kassandra.sources.bodacc import _match_notice_to_registry

    db = _make_db_with_hermes()
    notice = _make_bodacc_notice(
        commercant="HERMES GASTRONOMIE",
        famille="Procédures collectives",
    )
    result = _match_notice_to_registry(
        db, notice, "HERMES INTERNATIONAL", registry_id=1,
    )
    # HERMES INTERNATIONAL has known SIREN 572061123.
    # Notice lacks SIREN → must be rejected.
    assert result.is_rejected, f"Expected rejected, got {result.status}: {result.reason}"


def test_bodacc_sci_hermes_must_not_match_hermes_international():
    """HERMES INTERNATIONAL must NOT match SCI HERMES."""
    from kassandra.sources.bodacc import _match_notice_to_registry

    db = _make_db_with_hermes()
    notice = _make_bodacc_notice(
        commercant="SOCIETE CIVILE IMMOBILIERE HERMES",
        famille="Procédures collectives",
    )
    result = _match_notice_to_registry(
        db, notice, "HERMES INTERNATIONAL", registry_id=1,
    )
    assert result.is_rejected, f"Expected rejected, got {result.status}: {result.reason}"


def test_bodacc_delphine_limousin_must_not_match_delphine_sas():
    """DELPHINE SAS must NOT match LIMOUSIN, Delphine, LIMOUSIN (EI)."""
    from kassandra.sources.bodacc import _match_notice_to_registry

    db = _make_db_with_hermes()
    notice = _make_bodacc_notice(
        commercant="LIMOUSIN, Delphine, LIMOUSIN (EI)",
        famille="Procédures collectives",
    )
    result = _match_notice_to_registry(
        db, notice, "DELPHINE SAS", registry_id=2,
    )
    # DELPHINE SAS has no SIREN, and "delphine" is a known ambiguous token
    assert result.is_rejected, f"Expected rejected, got {result.status}: {result.reason}"


def test_bodacc_genuine_siren_match_still_works():
    """A BODACC notice containing the company's SIREN must confirm."""
    from kassandra.sources.bodacc import _match_notice_to_registry

    db = _make_db_with_hermes()
    notice = _make_bodacc_notice(
        commercant="RENAULT SAS",
        famille="Procédures collectives",
    )
    notice["siren"] = "351826335"
    result = _match_notice_to_registry(
        db, notice, "RENAULT SAS", registry_id=4,
    )
    assert result.is_confirmed, f"Expected confirmed, got {result.status}: {result.reason}"


def test_bodacc_non_french_jurisdiction_rejected():
    """A Dutch company must not match BODACC notices."""
    from kassandra.sources.bodacc import _match_notice_to_registry

    db = _make_db_with_hermes()
    notice = _make_bodacc_notice(
        commercant="AIRBUS SAS",
        famille="Procédures collectives",
    )
    result = _match_notice_to_registry(
        db, notice, "Airbus SE", registry_id=5,
    )
    assert result.is_rejected, f"Expected rejected for NL jurisdiction, got {result.status}"


def test_bodacc_exact_name_still_confirms():
    """Exact normalized name match must still confirm (TotalEnergies)."""
    from kassandra.sources.bodacc import _match_notice_to_registry

    db = _make_db_with_hermes()
    notice = _make_bodacc_notice(
        commercant="TOTALENERGIES SE",
        famille="Radiations",
    )
    # Use RENAULT SAS entity but with TotalEnergies name — this tests exact match
    # (TotalEnergies doesn't have SIREN → won't be rejected by require_official_id)
    result = _match_notice_to_registry(
        db, notice, "TotalEnergies SE", registry_id=3,
    )
    # TotalEnergies tokens: ['totalenergies']; notice tokens: {'totalenergies'}
    # exact_legal_name_match: normalize('TotalEnergies')='totalenergies' vs normalize('TOTALENERGIES SE')='totalenergies' → match
    assert result.is_confirmed, f"Expected confirmed exact name, got {result.status}: {result.reason}"
