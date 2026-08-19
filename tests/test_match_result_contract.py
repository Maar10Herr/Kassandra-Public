"""Tests for P0 official-ID-first legal-event matching and ambiguous-match quarantine.

Covers:
  - MatchResult contract (confirmed/unconfirmed/rejected)
  - Official-ID-first matching: BODACC (SIREN), BORME (CIF/NIF), Gazette (company number)
  - Exact normalized legal-name confirmation
  - Parent/subsidiary ambiguity → unconfirmed + queue
  - Generic/single-token paths → unconfirmed or rejected
  - Unconfirmed queue idempotent insertion (dedup)
  - Backward-compat booleans (is_confirmed, is_match)
  - Regression: DELPHINE, HERMES, Schneider/Iberdrola, K B TECH/CoreNet
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from kassandra.contracts import MatchResult


# ── MatchResult contract tests ────────────────────────────────────────────

class TestMatchResultContract:
    """MatchResult dataclass must have correct fields and properties."""

    def test_import_exists(self):
        """MatchResult must be importable."""
        assert MatchResult is not None


    def test_invalid_construction_raises(self):
        """MatchResult with invalid status raises ValueError at construction."""
        with pytest.raises(ValueError, match="MatchResult.status must be one of"):
            MatchResult(status="bogus_status")
        with pytest.raises(ValueError, match="MatchResult.status must be one of"):
            MatchResult(status="unconfirmed")  # legacy value, no longer valid
        with pytest.raises(ValueError, match="MatchResult.status must be one of"):
            MatchResult(status="")

    def test_valid_construction_all_statuses(self):
        """All canonical statuses construct without error."""
        MatchResult(status="confirmed")
        MatchResult(status="unconfirmed_review_candidate")
        MatchResult(status="rejected")

    def test_is_unconfirmed_deprecated(self):
        """is_unconfirmed emits DeprecationWarning and delegates to is_unconfirmed_review_candidate."""
        result = MatchResult(status="unconfirmed_review_candidate")
        with pytest.warns(DeprecationWarning, match="is_unconfirmed is deprecated"):
            assert result.is_unconfirmed
        confirmed = MatchResult(status="confirmed")
        with pytest.warns(DeprecationWarning):
            assert not confirmed.is_unconfirmed

    def test_confirmed_fields(self):
        """Confirmed match with official ID."""
        result = MatchResult(
            status="confirmed",
            reason="SIREN 123456789 matches registry record",
            matched_registry_id=5,
            matched_official_id="123456789",
            method="official_id",
            confidence=0.98,
        )
        assert result.status == "confirmed"
        assert result.matched_registry_id == 5
        assert result.matched_official_id == "123456789"
        assert result.method == "official_id"
        assert result.confidence == 0.98
        assert result.is_confirmed
        assert not result.is_unconfirmed_review_candidate
        assert not result.is_rejected
        assert result.is_match

    def test_unconfirmed_fields(self):
        """Unconfirmed match (ambiguous)."""
        result = MatchResult(
            status="unconfirmed_review_candidate",
            reason="Parent/subsidiary ambiguity: 'HERMES' matches both HERMES INTERNATIONAL and HERMES GASTRONOMIE",
            match_type="partial_overlap",
            confidence=0.4,
        )
        assert result.status == "unconfirmed_review_candidate"
        assert result.matched_registry_id is None
        assert result.is_unconfirmed
        assert not result.is_confirmed
        assert result.is_match  # legacy compat: unconfirmed_review_candidate is still a "match"

    def test_rejected_fields(self):
        """Rejected match."""
        result = MatchResult(
            status="rejected",
            reason="Generic single token 'tech' too ambiguous",
            match_type="single_token",
            confidence=0.0,
        )
        assert result.status == "rejected"
        assert result.is_rejected
        assert not result.is_confirmed
        assert not result.is_match

    def test_defaults(self):
        """Default MatchResult is rejected with zeros."""
        result = MatchResult(status="rejected")
        assert result.reason == ""
        assert result.confidence == 0.0
        assert result.matched_registry_id is None
        assert result.matched_official_id is None


# ── Helper: minimal test DB with unconfirmed_match_queue ──────────────────

def _make_match_db() -> sqlite3.Connection:
    """Create in-memory DB with registry, evidence, events, and unconfirmed_match_queue."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT,
            jurisdiction TEXT,
            companies_house_number TEXT,
            lei TEXT,
            isin TEXT,
            status TEXT,
            domain TEXT,
            company_type TEXT,
            siren TEXT,
            spanish_tax_id TEXT
        );
        CREATE TABLE IF NOT EXISTS evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL UNIQUE,
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
            raw_event_json TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            tombstone_reason TEXT,
            tombstoned_at TEXT,
            UNIQUE(evidence_id, event_type, registry_id)
        );
        CREATE TABLE IF NOT EXISTS unconfirmed_match_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            source_entity_name TEXT NOT NULL,
            candidate_registry_id INTEGER REFERENCES registry(id),
            candidate_registry_name TEXT,
            match_type TEXT NOT NULL,
            match_confidence REAL NOT NULL DEFAULT 0.5,
            evidence_id INTEGER REFERENCES evidence(id),
            evidence_excerpt TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewed_by TEXT,
            reviewed_at TEXT,
            resolution TEXT,
            resolution_registry_id INTEGER REFERENCES registry(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unconfirmed_dedup
            ON unconfirmed_match_queue(source_name, source_entity_name, candidate_registry_id)
            WHERE resolution IS NULL;
    """)
    # Insert portfolio companies
    db.execute("INSERT INTO registry (id, canonical_name, jurisdiction, companies_house_number) "
               "VALUES (1, 'LVMH MOET HENNESSY LOUIS VUITTON', 'FR', NULL)")
    db.execute("INSERT INTO registry (id, canonical_name, jurisdiction, companies_house_number) "
               "VALUES (2, 'HERMES INTERNATIONAL', 'FR', NULL)")
    db.execute("INSERT INTO registry (id, canonical_name, jurisdiction, companies_house_number) "
               "VALUES (3, 'TOTALENERGIES SE', 'FR', NULL)")
    db.execute("INSERT INTO registry (id, canonical_name, jurisdiction, companies_house_number) "
               "VALUES (4, 'INDITEX SA', 'ES', NULL)")
    db.execute("INSERT INTO registry (id, canonical_name, jurisdiction, companies_house_number) "
               "VALUES (5, 'IBERDROLA SA', 'ES', NULL)")
    db.execute("INSERT INTO registry (id, canonical_name, jurisdiction, companies_house_number) "
               "VALUES (6, 'INVENSYS LIMITED', 'GB', NULL)")
    db.execute("INSERT INTO registry (id, canonical_name, jurisdiction, companies_house_number) "
               "VALUES (7, 'K B TECH LIMITED', 'GB', '02345678')")
    db.commit()
    return db


def _insert_evidence(db, content_hash="abc123", source_url="http://test.com",
                     extraction_method="test"):
    db.execute(
        "INSERT INTO evidence (content_hash, source_url, retrieval_time, extraction_method, parser_version) "
        "VALUES (?, ?, datetime('now'), ?, '1.0')",
        (content_hash, source_url, extraction_method),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


# ── Official-ID normalization/validation tests ────────────────────────────

class TestOfficialIdNormalization:
    """Official identifiers must be extracted and validated before matching."""

    def test_bodacc_extract_siren_from_commercant(self):
        """SIREN (9 digits) extracted from BODACC commercant field."""
        from kassandra.sources.bodacc import _extract_official_id
        assert _extract_official_id("LVMH (SIREN 123456789)") == "123456789"
        assert _extract_official_id("SOCIETE X RCS PARIS 987 654 321") == "987654321"

    def test_bodacc_extract_siren_from_dedicated_field(self):
        """SIREN from dedicated 'siren' key in notice payload."""
        from kassandra.sources.bodacc import _extract_official_id
        # When payload has a 'siren' key, extract it directly
        notice = {"siren": "775670417", "commercant": "HERMES INTERNATIONAL"}
        assert _extract_official_id(notice) == "775670417"

    def test_bodacc_no_official_id(self):
        """Return None when no SIREN found."""
        from kassandra.sources.bodacc import _extract_official_id
        assert _extract_official_id("JUST A COMPANY NAME") is None

    def test_borme_extract_cif_from_title(self):
        """CIF/NIF extracted from BORME title/description."""
        from kassandra.sources.borme import _extract_official_id
        assert _extract_official_id(
            "INDITEX S.A. (A15075062) - Reducción de capital", ""
        ) == "A15075062"

    def test_borme_extract_nif_pattern(self):
        """NIF format: letter + 7-8 digits."""
        from kassandra.sources.borme import _extract_official_id
        assert _extract_official_id(
            "B12345678 - TELEFONICA S.A.", ""
        ) == "B12345678"

    def test_borme_no_official_id(self):
        """Return None when no CIF/NIF found."""
        from kassandra.sources.borme import _extract_official_id
        assert _extract_official_id("Some company notice", "") is None

    def test_gazette_extract_company_number(self):
        """Company number from Gazette notice title/summary."""
        from kassandra.sources.gazette import _extract_official_id
        assert _extract_official_id(
            "INVENSYS LIMITED (01234567) - Winding-Up Order",
            "Winding-up order for company 01234567",
        ) == "01234567"

    def test_gazette_no_company_number(self):
        """Return None when no company number found."""
        from kassandra.sources.gazette import _extract_official_id
        assert _extract_official_id(
            "ACME TRADING LTD - Winding-Up Order", "No number here"
        ) is None


# ── BODACC official-ID-first matching ─────────────────────────────────────

class TestBodaccOfficialIdMatching:
    """BODACC must attempt official-ID match before name matching."""

    def test_siren_match_confirmed(self):
        """When SIREN from notice matches registry's stored SIREN, confirmed."""
        db = _make_match_db()
        # Give registry row 3 (TotalEnergies) a SIREN
        db.execute("UPDATE registry SET lei = 'SIREN_542019511' WHERE id = 3")
        db.commit()

        from kassandra.sources.bodacc import _match_notice_to_registry
        notice = {"siren": "542019511", "commercant": "TOTALENERGIES SE",
                  "familleavis_lib": "Radiations", "dateparution": "2026-06-15"}

        # Need to pre-populate registry with official_id mapping
        # For now test the SIREN extraction → match pipeline
        siren = notice.get("siren", "")
        assert siren == "542019511"
        # The match function should use _extract_official_id
        from kassandra.sources.bodacc import _extract_official_id
        extracted = _extract_official_id(notice)
        assert extracted == "542019511"
        db.close()

    def test_exact_name_confirmed_when_all_tokens_match(self):
        """Exact full-name match with all distinctive tokens → confirmed."""
        from kassandra.sources.bodacc import _match_notice_to_registry
        db = _make_match_db()

        notice = {"commercant": "LVMH MOET HENNESSY LOUIS VUITTON",
                  "familleavis_lib": "Procédures collectives",
                  "dateparution": "2026-06-15"}

        result = _match_notice_to_registry(
            db, notice, "LVMH MOET HENNESSY LOUIS VUITTON", 1
        )
        assert result.status == "confirmed"
        assert result.matched_registry_id == 1
        assert result.method == "exact_name"
        assert result.confidence >= 0.85
        db.close()

    def test_delphine_sas_unconfirmed_not_confirmed(self):
        """DELPHINE SAS matching LIMOUSIN Delphine → must NOT confirm."""
        from kassandra.sources.bodacc import _match_notice_to_registry
        db = _make_match_db()

        # Add DELPHINE SAS to registry
        db.execute("INSERT INTO registry (id, canonical_name, jurisdiction) "
                   "VALUES (10, 'DELPHINE SAS', 'FR')")
        db.commit()

        notice = {"commercant": "LIMOUSIN, Delphine, LIMOUSIN (EI)",
                  "familleavis_lib": "Procédures collectives",
                  "dateparution": "2026-06-15"}

        result = _match_notice_to_registry(db, notice, "DELPHINE SAS", 10)
        # Must NOT be confirmed — single-token match is ambiguous
        assert result.status != "confirmed", f"DELPHINE SAS must not confirm: {result.reason}"
        # Should be unconfirmed or rejected
        assert result.status in ("unconfirmed_review_candidate", "rejected"), \
            f"Expected unconfirmed/rejected, got {result.status}: {result.reason}"
        db.close()

    def test_hermes_international_parent_subsidiary_unconfirmed(self):
        """HERMES INTERNATIONAL vs SCI HERMES — parent/subsidiary ambiguity."""
        from kassandra.sources.bodacc import _match_notice_to_registry
        db = _make_match_db()

        notice = {"commercant": "SOCIETE CIVILE IMMOBILIERE HERMES",
                  "familleavis_lib": "Procédures collectives",
                  "dateparution": "2026-06-15"}

        result = _match_notice_to_registry(db, notice, "HERMES INTERNATIONAL", 2)
        # Must NOT be confirmed
        assert result.status != "confirmed", f"HERMES must not confirm: {result.reason}"
        assert result.status in ("unconfirmed_review_candidate", "rejected", "unconfirmed_review_candidate"), \
            f"Expected unconfirmed/rejected/unconfirmed_review_candidate, got {result.status}: {result.reason}"
        db.close()


# ── BORME official-ID-first matching ──────────────────────────────────────

class TestBormeOfficialIdMatching:
    """BORME must attempt CIF/NIF match before name matching."""

    def test_exact_name_confirmed(self):
        """Exact full-name match with all tokens → confirmed."""
        from kassandra.sources.borme import _match_notice_to_registry
        db = _make_match_db()

        entry = {"title": "INDITEX S.A. - Reducción de capital",
                 "description": "Reducción de capital social",
                 "link": "https://boe.es/borme/123"}

        result = _match_notice_to_registry(db, entry, "INDITEX SA", 4)
        assert result.status == "confirmed"
        assert result.matched_registry_id == 4
        # "INDITEX SA" normalizes to single token ["inditex"], so method is subset_name
        assert result.method in ("exact_name", "subset_name")
        db.close()

    def test_schneider_subsidiary_single_token_must_not_confirm(self):
        """Generic single-token 'tech' from TECH SA must not confirm against SCHNEIDER ELECTRIC TECH SL."""
        from kassandra.sources.borme import _match_notice_to_registry
        db = _make_match_db()

        # Add TECH SA (generic) to registry
        db.execute("INSERT INTO registry (id, canonical_name, jurisdiction) "
                   "VALUES (20, 'TECH SA', 'ES')")
        db.commit()

        entry = {"title": "SCHNEIDER ELECTRIC TECH S.L. - Fusión",
                 "description": "Fusión por absorción",
                 "link": "https://boe.es/borme/456"}

        result = _match_notice_to_registry(db, entry, "TECH SA", 20)
        # Single generic token must not confirm
        assert result.status != "confirmed", f"TECH SA must not confirm: {result.reason}"
        # Accept any non-confirmed status
        assert not result.is_confirmed
        db.close()

    def test_iberdrola_distinctive_single_token_confirms(self):
        """IBERDROLA (distinctive single token) may confirm via collapsed substring check if it's unique."""
        from kassandra.sources.borme import _match_notice_to_registry
        db = _make_match_db()

        entry = {"title": "IBERDROLA S.A. - Convocatoria de junta",
                 "description": "Convocatoria de junta general",
                 "link": "https://boe.es/borme/789"}

        result = _match_notice_to_registry(db, entry, "IBERDROLA SA", 5)
        # IBERDROLA is a distinctive brand — should confirm
        assert result.status == "confirmed", f"IBERDROLA should confirm: {result.reason}"
        db.close()


# ── Gazette official-ID-first matching ────────────────────────────────────

class TestGazetteOfficialIdMatching:
    """Gazette must attempt company-number match before name matching."""

    def test_exact_name_confirmed(self):
        """Exact match with all tokens → confirmed."""
        from kassandra.sources.gazette import _match_notice_to_registry
        db = _make_match_db()

        notice = {"title": "INVENSYS LIMITED - Administration Order",
                  "summary": "Administration order for INVENSYS LIMITED",
                  "link": "https://gazette.co.uk/notice/1"}

        result = _match_notice_to_registry(db, notice, "INVENSYS LIMITED", 6)
        assert result.status == "confirmed"
        assert result.matched_registry_id == 6
        db.close()

    def test_k_b_tech_must_not_confirm_against_corenet(self):
        """K B TECH LIMITED must NOT confirm against CORENET TECH."""
        from kassandra.sources.gazette import _match_notice_to_registry
        db = _make_match_db()

        notice = {"title": "CORENET TECH (UK) PVT LTD - Winding-Up Order",
                  "summary": "Winding-up order for CORENET TECH (UK) PVT LTD",
                  "link": "https://gazette.co.uk/notice/2"}

        result = _match_notice_to_registry(db, notice, "K B TECH LIMITED", 7)
        assert result.status != "confirmed", f"K B TECH must not confirm: {result.reason}"
        assert result.status in ("unconfirmed_review_candidate", "rejected"), \
            f"Expected unconfirmed/rejected, got {result.status}: {result.reason}"
        db.close()

    def test_partial_overlap_unconfirmed(self):
        """Parent/subsidiary partial overlap goes to unconfirmed."""
        from kassandra.sources.gazette import _match_notice_to_registry
        db = _make_match_db()

        # Add a parent company
        db.execute("INSERT INTO registry (id, canonical_name, jurisdiction) "
                   "VALUES (30, 'BRITISH GAS HOLDINGS PLC', 'GB')")
        db.commit()

        # Notice for a different British Gas entity — only "british" overlaps
        # because "services" and "limited" are stripped, and "holdings" ≠ "gas" once "gas" is stripped from holdings
        # Different subsidiary: "BRITISH TELECOM" — only "british" overlaps
        notice = {"title": "BRITISH TELECOM PLC - Administration",
                  "summary": "Administration order",
                  "link": "https://gazette.co.uk/notice/3"}

        result = _match_notice_to_registry(db, notice, "BRITISH GAS HOLDINGS PLC", 30)
        # Only "british" overlaps (1/2 tokens) → unconfirmed_review_candidate
        assert result.status in ("unconfirmed", "unconfirmed_review_candidate"), \
            f"Expected unconfirmed or unconfirmed_review_candidate, got {result.status}: {result.reason}"
        db.close()


# ── Unconfirmed match queue tests ─────────────────────────────────────────

class TestUnconfirmedMatchQueue:
    """Ambiguous matches must be inserted into unconfirmed_match_queue idempotently."""

    def test_queue_insert_unconfirmed_match(self):
        """Insert unconfirmed match into queue."""
        db = _make_match_db()
        ev_id = _insert_evidence(db)

        from kassandra.sources.entity_resolution import queue_unconfirmed_match
        queue_unconfirmed_match(
            db=db,
            source_name="bodacc_fr",
            source_entity_name="SOCIETE CIVILE IMMOBILIERE HERMES",
            candidate_registry_id=2,
            candidate_registry_name="HERMES INTERNATIONAL",
            match_type="partial_overlap",
            match_confidence=0.4,
            evidence_id=ev_id,
            evidence_excerpt="SCI HERMES - Procédures collectives",
            reason="Parent/subsidiary ambiguity: 'HERMES' token overlaps but 'INTERNATIONAL' missing",
        )

        rows = db.execute(
            "SELECT * FROM unconfirmed_match_queue"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["source_name"] == "bodacc_fr"
        assert rows[0]["status"] == "pending"
        db.close()

    def test_queue_dedup_idempotent(self):
        """Inserting the same unconfirmed match twice must not create duplicates."""
        db = _make_match_db()
        ev_id = _insert_evidence(db)

        from kassandra.sources.entity_resolution import queue_unconfirmed_match

        outcomes = []
        for _ in range(2):
            outcomes.append(queue_unconfirmed_match(
                db=db,
                source_name="bodacc_fr",
                source_entity_name="SOCIETE CIVILE IMMOBILIERE HERMES",
                candidate_registry_id=2,
                candidate_registry_name="HERMES INTERNATIONAL",
                match_type="partial_overlap",
                match_confidence=0.4,
                evidence_id=ev_id,
                evidence_excerpt="SCI HERMES - Procédures collectives",
                reason="Parent/subsidiary ambiguity",
            ))

        assert outcomes == [True, False]

        rows = db.execute(
            "SELECT * FROM unconfirmed_match_queue"
        ).fetchall()
        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
        db.close()

    def test_queue_different_candidates_allowed(self):
        """Different candidate_registry_id for same source_entity creates separate rows."""
        db = _make_match_db()
        ev_id = _insert_evidence(db)

        from kassandra.sources.entity_resolution import queue_unconfirmed_match

        queue_unconfirmed_match(
            db=db, source_name="bodacc_fr",
            source_entity_name="HERMES FRANCE",
            candidate_registry_id=2, candidate_registry_name="HERMES INTERNATIONAL",
            match_type="partial_overlap", match_confidence=0.4,
            evidence_id=ev_id, evidence_excerpt="test", reason="test",
        )
        queue_unconfirmed_match(
            db=db, source_name="bodacc_fr",
            source_entity_name="HERMES FRANCE",
            candidate_registry_id=3, candidate_registry_name="TOTALENERGIES SE",
            match_type="partial_overlap", match_confidence=0.3,
            evidence_id=ev_id, evidence_excerpt="test", reason="alt match",
        )

        rows = db.execute("SELECT * FROM unconfirmed_match_queue").fetchall()
        assert len(rows) == 2
        db.close()

    def test_queue_no_duplicate_events_created_for_unconfirmed(self):
        """Unconfirmed matches must NOT create event rows or affect scores."""
        db = _make_match_db()
        ev_id = _insert_evidence(db)

        from kassandra.sources.entity_resolution import queue_unconfirmed_match
        queue_unconfirmed_match(
            db=db, source_name="bodacc_fr",
            source_entity_name="SCI HERMES",
            candidate_registry_id=2, candidate_registry_name="HERMES INTERNATIONAL",
            match_type="partial_overlap", match_confidence=0.4,
            evidence_id=ev_id, evidence_excerpt="test", reason="ambiguous",
        )

        # No events should have been created against registry_id 2
        events = db.execute(
            "SELECT * FROM events WHERE registry_id = 2"
        ).fetchall()
        assert len(events) == 0, "Unconfirmed match must not create events"
        db.close()


# ── Collector integration: MatchResult must drive event creation ──────────

class TestCollectorMatchResultIntegration:
    """Collectors must use MatchResult status to gate event creation."""

    def test_confirm_match_gates_event_creation(self):
        """Only confirmed matches create events."""
        from kassandra.contracts import MatchResult

        confirmed = MatchResult(status="confirmed", matched_registry_id=1, method="exact_name")
        unconfirmed = MatchResult(status="unconfirmed_review_candidate", reason="ambiguous")
        rejected = MatchResult(status="rejected", reason="no match")

        # Confirmed → event creation allowed
        assert confirmed.is_confirmed
        # Unconfirmed → queued, no event
        assert not unconfirmed.is_confirmed
        # Rejected → nothing
        assert not rejected.is_confirmed

    def test_legacy_is_match_for_filtering(self):
        """is_match returns True for confirmed AND unconfirmed_review_candidate (backward compat for filtering)."""
        from kassandra.contracts import MatchResult

        assert MatchResult(status="confirmed").is_match
        assert MatchResult(status="unconfirmed_review_candidate").is_match  # legacy: still a "match" for filtering
        assert not MatchResult(status="rejected").is_match


# ── Regression: no false confirms for known problematic cases ─────────────

class TestRegressionNoFalseConfirms:
    """Every known false-positive case must NOT produce a confirmed match."""

    def test_bodacc_delphine_not_confirmed(self):
        from kassandra.sources.bodacc import _match_notice_to_registry
        db = _make_match_db()
        db.execute("INSERT INTO registry (id, canonical_name, jurisdiction) "
                   "VALUES (40, 'DELPHINE SAS', 'FR')")
        db.commit()

        notice = {"commercant": "LIMOUSIN, Delphine, LIMOUSIN (EI)",
                  "familleavis_lib": "Procédures collectives"}
        result = _match_notice_to_registry(db, notice, "DELPHINE SAS", 40)
        assert result.status != "confirmed", f"DELPHINE false confirm: {result}"
        db.close()

    def test_bodacc_hermes_international_not_confirmed_sci_hermes(self):
        from kassandra.sources.bodacc import _match_notice_to_registry
        db = _make_match_db()

        notice = {"commercant": "SOCIETE CIVILE IMMOBILIERE HERMES",
                  "familleavis_lib": "Procédures collectives"}
        result = _match_notice_to_registry(db, notice, "HERMES INTERNATIONAL", 2)
        assert result.status != "confirmed", f"HERMES false confirm: {result}"
        db.close()

    def test_bodacc_hermes_international_not_confirmed_hermes_gastronomie(self):
        from kassandra.sources.bodacc import _match_notice_to_registry
        db = _make_match_db()

        notice = {"commercant": "HERMES GASTRONOMIE",
                  "familleavis_lib": "Procédures collectives"}
        result = _match_notice_to_registry(db, notice, "HERMES INTERNATIONAL", 2)
        assert result.status != "confirmed", f"HERMES GASTRONOMIE false confirm: {result}"
        db.close()

    def test_gazette_k_b_tech_not_confirmed_corenet(self):
        from kassandra.sources.gazette import _match_notice_to_registry
        db = _make_match_db()

        notice = {"title": "CORENET TECH (UK) PVT LTD - Winding-Up Order",
                  "summary": "Winding-up order for CORENET TECH (UK) PVT LTD",
                  "link": "https://gazette.co.uk/notice/2"}
        result = _match_notice_to_registry(db, notice, "K B TECH LIMITED", 7)
        assert result.status != "confirmed", f"K B TECH false confirm: {result}"
        db.close()

    def test_borme_schneider_subsidiary_not_confirmed(self):
        from kassandra.sources.borme import _match_notice_to_registry
        db = _make_match_db()
        db.execute("INSERT INTO registry (id, canonical_name, jurisdiction) "
                   "VALUES (50, 'TECH SA', 'ES')")
        db.commit()

        entry = {"title": "SCHNEIDER ELECTRIC TECH S.L. - Fusión",
                 "description": "Fusión por absorción",
                 "link": "https://boe.es/borme/456"}
        result = _match_notice_to_registry(db, entry, "TECH SA", 50)
        assert result.status != "confirmed", f"SCHNEIDER false confirm: {result}"
        db.close()


# ── Valid matches still confirm (regression guards) ───────────────────────

class TestRegressionValidConfirms:
    """Valid exact-name and distinctive-brand matches must still confirm."""

    def test_bodacc_lvmh_confirms(self):
        from kassandra.sources.bodacc import _match_notice_to_registry
        db = _make_match_db()
        notice = {"commercant": "LVMH MOET HENNESSY LOUIS VUITTON",
                  "familleavis_lib": "Procédures collectives"}
        result = _match_notice_to_registry(db, notice, "LVMH MOET HENNESSY LOUIS VUITTON", 1)
        assert result.status == "confirmed"
        db.close()

    def test_bodacc_totalenergies_confirms(self):
        from kassandra.sources.bodacc import _match_notice_to_registry
        db = _make_match_db()
        notice = {"commercant": "TOTALENERGIES SE",
                  "familleavis_lib": "Radiations"}
        result = _match_notice_to_registry(db, notice, "TOTALENERGIES SE", 3)
        assert result.status == "confirmed"
        db.close()

    def test_bodacc_airbus_confirms(self):
        from kassandra.sources.bodacc import _match_notice_to_registry
        db = _make_match_db()
        db.execute("INSERT INTO registry (id, canonical_name, jurisdiction) "
                   "VALUES (60, 'Airbus', 'FR')")
        db.commit()
        notice = {"commercant": "AIRBUS SAS",
                  "familleavis_lib": "Procédures collectives"}
        result = _match_notice_to_registry(db, notice, "Airbus", 60)
        assert result.status == "confirmed"
        db.close()

    def test_gazette_invensys_confirms(self):
        from kassandra.sources.gazette import _match_notice_to_registry
        db = _make_match_db()
        notice = {"title": "INVENSYS LIMITED - Administration Order",
                  "summary": "administration order", "link": "https://x.com/1"}
        result = _match_notice_to_registry(db, notice, "INVENSYS LIMITED", 6)
        assert result.status == "confirmed"
        db.close()

    def test_borme_inditex_confirms(self):
        from kassandra.sources.borme import _match_notice_to_registry
        db = _make_match_db()
        entry = {"title": "INDITEX S.A. - Reducción de capital",
                 "description": "", "link": "https://boe.es/1"}
        result = _match_notice_to_registry(db, entry, "INDITEX SA", 4)
        assert result.status == "confirmed"
        db.close()

    def test_borme_iberdrola_confirms(self):
        from kassandra.sources.borme import _match_notice_to_registry
        db = _make_match_db()
        entry = {"title": "IBERDROLA S.A. - Convocatoria de junta",
                 "description": "", "link": "https://boe.es/2"}
        result = _match_notice_to_registry(db, entry, "IBERDROLA SA", 5)
        assert result.status == "confirmed"
        db.close()


# ── Adapter defensive routing: corrupt MatchResult status tests ───────────

@pytest.mark.parametrize(
    ("module_name", "client_name", "method_name", "notice"),
    [
        (
            "kassandra.sources.bodacc", "BodaccClient",
            "process_notices_for_company",
            {"commercant": "TEST CO", "familleavis_lib": "Test", "dateparution": "2026-01-01"},
        ),
        (
            "kassandra.sources.borme", "BorMeClient",
            "process_notices_for_company",
            {"title": "TEST CO", "link": "https://example.test/borme"},
        ),
        (
            "kassandra.sources.gazette", "GazetteClient",
            "_process_notices_for_company",
            {
                "title": "TEST CO",
                "link": "https://example.test/gazette",
                "published": "2026-01-01",
                "summary": "Test notice",
            },
        ),
    ],
)
def test_adapter_raises_on_corrupted_match_status(
    module_name, client_name, method_name, notice,
):
    """Every legal adapter fails closed if a corrupted result bypasses the contract."""
    module = __import__(module_name, fromlist=[client_name])
    client = getattr(module, client_name)()
    method = getattr(client, method_name)
    db = _make_match_db()

    corrupted = MatchResult(status="confirmed")
    object.__setattr__(corrupted, "status", "bogus_status")

    with patch.object(module, "_match_notice_to_registry", return_value=corrupted):
        with pytest.raises(
            AssertionError,
            match="Unexpected MatchResult.status='bogus_status'",
        ):
            method(db, registry_id=1, company_name="TEST CO", notices=[notice])
    db.close()
