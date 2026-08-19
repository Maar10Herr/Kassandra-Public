"""Tests for Spain BORME (Boletín Oficial del Registro Mercantil) source adapter."""

import sqlite3
from unittest.mock import MagicMock, patch

import httpx
import pytest


def _make_db(extra_registry_rows: list[tuple[str, str]] | None = None) -> sqlite3.Connection:
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
        ("INDITEX SA", "ES"),
    )
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
        ("LABORATORIOS INGENIERIA Y CONSTRUCCION S.A.", "ES"),
    )
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
        ("TELEFONICA SA", "ES"),
    )
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
        ("DEUTSCHE BANK AG", "DE"),
    )
    if extra_registry_rows:
        for name, jurisdiction in extra_registry_rows:
            db.execute(
                "INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
                (name, jurisdiction),
            )
    db.commit()
    return db


def _make_entry(
    title: str = "",
    description: str = "",
    link: str = "",
    published: str = "Mon, 15 Jun 2026 00:00:00 +0200",
    entry_id: str = "",
) -> dict:
    return {
        "title": title,
        "link": link or f"https://www.boe.es/diario_borme/txt.php?id=TEST-{hash(title)}",
        "description": description,
        "published": published,
        "id": entry_id or f"id-{hash(title)}",
    }


# ── Sample BORME RSS entries ──────────────────────────────────────────────────

MOCK_BORME_ENTRIES = [
    _make_entry(
        title="INDITEX S.A. — Reducción de capital",
        description="SECCIÓN SEGUNDA. Anuncios y avisos legales. Reducción de capital social.",
        link="https://www.boe.es/diario_borme/txt.php?id=BORME-A-2026-115-28",
    ),
    _make_entry(
        title="LABORATORIOS INGENIERIA Y CONSTRUCCION S.A. (EN LIQUIDACIÓN)",
        description="SECCIÓN SEGUNDA. Anuncios y avisos legales. Disolución y liquidación.",
        link="https://www.boe.es/diario_borme/txt.php?id=BORME-A-2026-115-29",
    ),
    _make_entry(
        title="TELEFONICA S.A. — Fusión por absorción",
        description="SECCIÓN SEGUNDA. Anuncios y avisos legales. Fusión por absorción.",
        link="https://www.boe.es/diario_borme/txt.php?id=BORME-A-2026-115-30",
    ),
    _make_entry(
        title="EMPRESA DESCONOCIDA S.L. — Convocatoria de junta general",
        description="SECCIÓN SEGUNDA. Convocatoria de junta general ordinaria.",
        link="https://www.boe.es/diario_borme/txt.php?id=BORME-A-2026-115-31",
    ),
    _make_entry(
        title="CONSTRUCCIONES MARTINEZ S.L. — Concurso de acreedores",
        description="SECCIÓN SEGUNDA. Anuncios y avisos legales. Declaración de concurso voluntario.",
        link="https://www.boe.es/diario_borme/txt.php?id=BORME-A-2026-115-32",
    ),
]


# ── Matching tests ───────────────────────────────────────────────────────────

def test_notice_matches_company_basic():
    from kassandra.sources.borme import _notice_matches_company

    assert _notice_matches_company("INDITEX SA", "INDITEX S.A. — Reducción de capital", "")
    assert _notice_matches_company(
        "LABORATORIOS INGENIERIA Y CONSTRUCCION S.A.",
        "LABORATORIOS INGENIERIA Y CONSTRUCCION S.A. (EN LIQUIDACIÓN)",
        "",
    )
    assert _notice_matches_company("TELEFONICA SA", "TELEFONICA S.A. — Fusión por absorción", "")


def test_notice_matches_company_conservative():
    """Generic legal-suffix-only overlap must not match."""
    from kassandra.sources.borme import _notice_matches_company

    # "SA" is a stripped token, so a company named just "SA" won't match on that alone
    assert not _notice_matches_company("SA", "SA DISTRIBUCION S.L.", "")
    # "Sociedad" is stripped
    assert not _notice_matches_company("Sociedad", "SOCIEDAD ANONIMA DE GESTION", "")


def test_notice_matches_hyphenated():
    from kassandra.sources.borme import _notice_matches_company

    # Hyphenated brand like "ECO-ENERGIA" should match collapsed form
    assert _notice_matches_company("ECO-ENERGIA", "ECO ENERGIA S.L.", "")


def test_notice_no_match_different_company():
    from kassandra.sources.borme import _notice_matches_company

    assert not _notice_matches_company("INDITEX SA", "TELEFONICA S.A. — Fusión", "")
    assert not _notice_matches_company("TELEFONICA", "INDITEX S.A. — Reducción", "")


def test_notice_matches_subset_tokens():
    """All distinctive tokens from company_name must appear in the notice."""
    from kassandra.sources.borme import _notice_matches_company

    assert _notice_matches_company(
        "Ingenieria Construccion",
        "LABORATORIOS INGENIERIA Y CONSTRUCCION S.A. (EN LIQUIDACIÓN)",
        "",
    )
    # "Ingenieria" + "Telecom" requires BOTH
    assert not _notice_matches_company(
        "Ingenieria Telecom",
        "LABORATORIOS INGENIERIA Y CONSTRUCCION S.A. (EN LIQUIDACIÓN)",
        "",
    )


def test_notice_matches_single_distinctive_token():
    from kassandra.sources.borme import _notice_matches_company

    assert _notice_matches_company("INDITEX", "INDITEX S.A. — Reducción de capital", "")
    # Single token too short (< 4)
    assert not _notice_matches_company("SA", "INDITEX S.A. — Reducción de capital", "")
    assert not _notice_matches_company("AI", "AIE CONSTRUCCIONES S.L.", "")


# ── Classification tests ──────────────────────────────────────────────────────

def test_classify_liquidacion():
    from kassandra.sources.borme import _classify_borme_notice

    event_type, severity = _classify_borme_notice(
        "LABORATORIOS INGENIERIA S.A. (EN LIQUIDACIÓN)",
        "SECCIÓN SEGUNDA. Anuncios de liquidación.",
    )
    assert event_type == "insolvency"
    assert severity == "critical"


def test_classify_concurso():
    from kassandra.sources.borme import _classify_borme_notice

    event_type, severity = _classify_borme_notice(
        "CONSTRUCCIONES MARTINEZ S.L. — Concurso de acreedores",
        "Declaración de concurso voluntario.",
    )
    assert event_type == "insolvency"
    assert severity == "critical"


def test_classify_disolución():
    from kassandra.sources.borme import _classify_borme_notice

    event_type, severity = _classify_borme_notice(
        "EMPRESA X S.L. — Disolución",
        "Disolución de la sociedad.",
    )
    assert event_type == "insolvency"
    assert severity == "critical"


def test_classify_reduccion_capital():
    from kassandra.sources.borme import _classify_borme_notice

    event_type, severity = _classify_borme_notice(
        "INDITEX S.A. — Reducción de capital",
        "Reducción de capital social.",
    )
    assert event_type == "restructuring"
    assert severity == "medium"


def test_classify_fusion():
    from kassandra.sources.borme import _classify_borme_notice

    event_type, severity = _classify_borme_notice(
        "TELEFONICA S.A. — Fusión por absorción",
        "Proyecto de fusión por absorción.",
    )
    assert event_type == "restructuring"
    assert severity == "medium"


def test_classify_convocatoria_with_disolución():
    from kassandra.sources.borme import _classify_borme_notice

    event_type, severity = _classify_borme_notice(
        "EMPRESA Y S.L.",
        "Convocatoria de junta general para disolución de la sociedad.",
    )
    assert event_type == "restructuring"
    assert severity == "high"


def test_classify_unknown():
    from kassandra.sources.borme import _classify_borme_notice

    event_type, severity = _classify_borme_notice(
        "EMPRESA X S.L. — Nombramiento de administrador",
        "Nombramiento de administrador único.",
    )
    assert event_type is None
    assert severity is None


# ── Client integration tests ──────────────────────────────────────────────────

def test_filter_notices_for_company():
    from kassandra.sources.borme import BorMeClient

    client = BorMeClient()
    entries = MOCK_BORME_ENTRIES

    matches = client.filter_notices_for_company("INDITEX SA", entries)
    assert len(matches) == 1
    assert "INDITEX" in matches[0]["title"]

    matches = client.filter_notices_for_company("LABORATORIOS INGENIERIA Y CONSTRUCCION S.A.", entries)
    assert len(matches) == 1
    assert "INGENIERIA" in matches[0]["title"]

    matches = client.filter_notices_for_company("TELEFONICA", entries)
    assert len(matches) == 1
    assert "TELEFONICA" in matches[0]["title"]

    matches = client.filter_notices_for_company("BANCO SANTANDER SA", entries)
    assert matches == []


def test_process_notices_stores_evidence_and_events():
    from kassandra.sources.borme import BorMeClient

    db = _make_db()
    client = BorMeClient()

    notices = [
        _make_entry(
            title="LABORATORIOS INGENIERIA Y CONSTRUCCION S.A. (EN LIQUIDACIÓN)",
            description="SECCIÓN SEGUNDA. Disolución y liquidación.",
            link="https://www.boe.es/diario_borme/txt.php?id=TEST-1",
        ),
    ]

    collected = client.process_notices_for_company(
        db=db, registry_id=2, company_name="LABORATORIOS INGENIERIA Y CONSTRUCCION S.A.",
        notices=notices,
    )

    assert collected.new_evidence == 1
    assert collected.events_created == 0  # ambiguous → queued, not an event
    assert collected.unconfirmed >= 1     # routed to unconfirmed_match_queue

    # Verify evidence stored
    evidence_count = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    assert evidence_count == 1

    evidence = db.execute("SELECT * FROM evidence LIMIT 1").fetchone()
    assert evidence["extraction_method"] == "borme_es_feed"
    assert evidence["source_reliability"] == 1.0
    assert evidence["content_type"] == "application/rss+xml"

    # Legal name plus lifecycle extension is not exact → queued, NOT an event
    event_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert event_count == 0, "ambiguous match must not create an event"

    # Verify queue entry
    queue_row = db.execute(
        "SELECT * FROM unconfirmed_match_queue LIMIT 1"
    ).fetchone()
    assert queue_row is not None
    assert queue_row["source_name"] == "borme_es"
    assert queue_row["status"] == "pending"


def test_process_notices_restructuring_event():
    from kassandra.sources.borme import BorMeClient

    db = _make_db()
    client = BorMeClient()

    notices = [
        _make_entry(
            title="INDITEX S.A. — Reducción de capital",
            description="Reducción de capital social.",
            link="https://www.boe.es/diario_borme/txt.php?id=TEST-2",
        ),
    ]

    collected = client.process_notices_for_company(
        db=db, registry_id=1, company_name="INDITEX SA", notices=notices,
    )

    assert collected.new_evidence == 1
    assert collected.events_created == 1

    event = db.execute(
        "SELECT event_type, severity FROM events LIMIT 1"
    ).fetchone()
    assert event["event_type"] == "restructuring"
    assert event["severity"] == "medium"


def test_fetch_feed_mocked():
    """fetch_feed makes HTTP call and parses RSS response."""
    from kassandra.sources.borme import BorMeClient, BORME_RSS_FEED

    client = BorMeClient()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.raise_for_status.return_value = None
    mock_response.text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>BORME</title>
<item><title>INDITEX S.A. - Reducción de capital</title>
<link>https://www.boe.es/diario_borme/txt.php?id=TEST-1</link>
<pubDate>Mon, 15 Jun 2026</pubDate>
<description>SECCIÓN SEGUNDA. Reducción de capital.</description>
<guid>test-1</guid></item>
<item><title>TELEFONICA S.A. - Fusión</title>
<link>https://www.boe.es/diario_borme/txt.php?id=TEST-2</link>
<pubDate>Mon, 15 Jun 2026</pubDate>
<description>SECCIÓN SEGUNDA. Fusión por absorción.</description>
<guid>test-2</guid></item>
</channel></rss>"""

    with patch.object(client._client, "get", return_value=mock_response) as mock_get:
        entries = client.fetch_feed()

    mock_get.assert_called_once_with(BORME_RSS_FEED)
    assert len(entries) == 2
    assert entries[0]["title"].startswith("INDITEX")
    assert entries[1]["title"].startswith("TELEFONICA")


def test_fetch_feed_http_error_returns_empty():
    from kassandra.sources.borme import BorMeClient

    client = BorMeClient()

    with patch.object(client._client, "get", side_effect=httpx.HTTPError("timeout")):
        entries = client.fetch_feed()

    assert entries == []


def test_collect_for_company_integration():
    from kassandra.sources.borme import BorMeClient

    db = _make_db()
    client = BorMeClient()

    with patch.object(client, "fetch_feed", return_value=MOCK_BORME_ENTRIES):
        collected = client.collect_for_company(
            db=db, registry_id=1, company_name="INDITEX SA",
        )

    assert collected.new_evidence >= 1
    assert collected.events_created == 1

    # Verify evidence stored
    evidence_count = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    assert evidence_count >= 1

    # Verify event stored
    event = db.execute(
        "SELECT event_type, severity FROM events LIMIT 1"
    ).fetchone()
    assert event["event_type"] == "restructuring"
    assert event["severity"] == "medium"


def test_convocatoria_juntas_with_disolución_classification():
    from kassandra.sources.borme import _classify_borme_notice

    event_type, severity = _classify_borme_notice(
        "EMPRESA TEST S.L.",
        "Convocatoria de Junta General para disolución de la sociedad.",
    )
    assert event_type == "restructuring"
    assert severity == "high"


def test_transformacion_classification():
    from kassandra.sources.borme import _classify_borme_notice

    event_type, severity = _classify_borme_notice(
        "TRANSFORMACIONES S.A. — Transformación de sociedad",
        "Transformación de sociedad anónima a sociedad limitada.",
    )
    assert event_type == "restructuring"
    assert severity == "medium"


# ── Identifier-first policy regression tests ───────────────────────────

def _make_db_borme_regression() -> sqlite3.Connection:
    """DB with ES companies, some with CIF/NIF."""
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
    # INDITEX SA (ES, with CIF)
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction, spanish_tax_id) VALUES (?, ?, ?)",
        ("INDITEX SA", "ES", "A15075062"),
    )
    # An ES company without CIF
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
        ("LABORATORIOS INGENIERIA Y CONSTRUCCION S.A.", "ES"),
    )
    # A DE company (should be rejected by jurisdiction)
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction) VALUES (?, ?)",
        ("DEUTSCHE BANK AG", "DE"),
    )
    # Another ES company with known CIF (for multi-entity dedup test)
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction, spanish_tax_id) VALUES (?, ?, ?)",
        ("ZARA ESPANA S.A.", "ES", "B15000001"),
    )
    db.commit()
    return db


def test_borme_non_spanish_jurisdiction_rejected():
    """A German company must not match BORME notices."""
    from kassandra.sources.borme import _match_notice_to_registry

    db = _make_db_borme_regression()
    entry = _make_entry(
        title="DEUTSCHE BANK AG — Restructuring",
        description="Restructuring announcement.",
    )
    result = _match_notice_to_registry(
        db, entry, "DEUTSCHE BANK AG", registry_id=3,
    )
    assert result.is_rejected, f"Expected rejected for DE jurisdiction, got {result.status}"


def test_borme_with_cif_requires_cif_in_notice():
    """Company with known CIF must have it in notice, or be rejected."""
    from kassandra.sources.borme import _match_notice_to_registry

    db = _make_db_borme_regression()
    entry = _make_entry(
        title="INDITEX S.A. — Reducción de capital",
        description="Reducción de capital social.",
    )
    result = _match_notice_to_registry(
        db, entry, "INDITEX SA", registry_id=1,
    )
    # INDITEX SA has known CIF A15075062, notice has no CIF → rejected
    assert result.is_rejected, f"Expected rejected (no CIF in notice), got {result.status}: {result.reason}"


def test_borme_genuine_cif_match_still_works():
    """A BORME notice containing the company's CIF must confirm."""
    from kassandra.sources.borme import _match_notice_to_registry

    db = _make_db_borme_regression()
    entry = _make_entry(
        title="INDITEX S.A. (A15075062) — Reducción de capital",
        description="Reducción de capital social.",
    )
    result = _match_notice_to_registry(
        db, entry, "INDITEX SA", registry_id=1,
    )
    assert result.is_confirmed, f"Expected confirmed CIF match, got {result.status}: {result.reason}"


def test_borme_exact_name_still_confirms_for_no_cif_company():
    """Exact name match must confirm for companies without known CIF."""
    from kassandra.sources.borme import _match_notice_to_registry

    db = _make_db_borme_regression()
    entry = _make_entry(
        title="LABORATORIOS INGENIERIA Y CONSTRUCCION S.A. — Disolución",
        description="Disolución de la sociedad.",
    )
    result = _match_notice_to_registry(
        db, entry, "LABORATORIOS INGENIERIA Y CONSTRUCCION S.A.", registry_id=2,
    )
    # No CIF known → exact name should confirm
    assert result.is_confirmed, f"Expected confirmed exact name, got {result.status}: {result.reason}"


def test_borme_multi_entity_notice_dedup():
    """One notice matching 2 entities should create review candidates, not 2 duplicate direct events."""
    from kassandra.sources.borme import _match_notice_to_registry

    db = _make_db_borme_regression()
    entry = _make_entry(
        title="INDITEX ZARA — Fusión por absorción",
        description="Fusión de sociedades del grupo INDITEX.",
    )
    # Match against INDITEX SA (has CIF, notice lacks CIF → rejected)
    result1 = _match_notice_to_registry(db, entry, "INDITEX SA", registry_id=1)
    assert result1.is_rejected, "INDITEX SA has CIF, notice lacks CIF, must reject"

    # ZARA ESPANA has CIF too, notice lacks CIF → rejected
    result2 = _match_notice_to_registry(db, entry, "ZARA ESPANA S.A.", registry_id=4)
    assert result2.is_rejected, "ZARA ESPANA has CIF, notice lacks CIF, must reject"
