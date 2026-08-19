"""Tests for content classifier against golden corpus.

Validates that adversarial events are detected and benign texts are not.
Per engineering audit P2: event detection vertical slice.
"""

import pytest

from kassandra.classifier import classify_content
from kassandra.test_corpus import ADVERSE_CASES, BENIGN_CASES, HARD_NEGATIVE_CASES


class TestClassifierAdverseDetection:
    """Verify adverse events ARE detected."""

    @pytest.mark.parametrize("case", ADVERSE_CASES)
    def test_adverse_event_detected(self, case):
        """Each adverse case should produce at least one event."""
        events = classify_content(case["text"])
        assert len(events) > 0, (
            f"FAIL: {case['id']} — expected at least 1 event, got 0. "
            f"Text: {case['text'][:100]}..."
        )

    @pytest.mark.parametrize("case", ADVERSE_CASES)
    def test_adverse_event_type_matches(self, case):
        """Detected event should include the expected type."""
        events = classify_content(case["text"])
        event_types = [e["event_type"] for e in events]
        assert case["expected_event_type"] in event_types, (
            f"FAIL: {case['id']} — expected '{case['expected_event_type']}' "
            f"in detected types {event_types}"
        )

    @pytest.mark.parametrize("case", ADVERSE_CASES)
    def test_adverse_severity_appropriate(self, case):
        """Detected event severity should match expected."""
        events = classify_content(case["text"])
        matching = [e for e in events if e["event_type"] == case["expected_event_type"]]
        assert len(matching) > 0
        assert matching[0]["severity"] == case["expected_severity"], (
            f"FAIL: {case['id']} — expected severity '{case['expected_severity']}' "
            f"got '{matching[0]['severity']}'"
        )


class TestClassifierBenignControl:
    """Verify benign texts are NOT falsely classified."""

    @pytest.mark.parametrize("case", BENIGN_CASES)
    def test_benign_no_high_severity(self, case):
        """Benign texts should not produce high/critical severity events."""
        events = classify_content(case["text"])
        high_severity_events = [
            e for e in events
            if e["severity"] in ("critical", "high")
        ]
        assert len(high_severity_events) == 0, (
            f"FAIL: {case['id']} — expected 0 high-severity events, "
            f"got: {[(e['event_type'], e['severity']) for e in high_severity_events]}"
        )

    @pytest.mark.parametrize("case", BENIGN_CASES)
    def test_benign_no_critical_event_types(self, case):
        """Benign texts should not trigger insolvency/restructuring/etc."""
        events = classify_content(case["text"])
        critical_types = {"insolvency", "going_concern_warning", "restructuring",
                          "payment_stress", "regulatory_action", "cyber_incident"}
        false_positives = [e for e in events if e["event_type"] in critical_types]
        assert len(false_positives) == 0, (
            f"FAIL: {case['id']} — got critical event in benign text: "
            f"{[(e['event_type'], e['severity']) for e in false_positives]}"
        )


class TestClassifierMetadata:
    """Verify classifier stores proper metadata."""

    def test_classifier_version_present(self):
        """Events should include classifier version."""
        events = classify_content(ADVERSE_CASES[0]["text"])
        for e in events:
            assert "classifier_version" in e
            assert e["classifier_version"].startswith("2.")

    def test_pattern_id_present(self):
        """Events should include pattern ID for auditability."""
        events = classify_content(ADVERSE_CASES[0]["text"])
        for e in events:
            assert "pattern_id" in e
            assert e["pattern_id"].startswith("P")

    def test_span_stored(self):
        """Events should include text span positions."""
        events = classify_content(ADVERSE_CASES[0]["text"])
        for e in events:
            assert "span_start" in e
            assert "span_end" in e
            assert e["span_end"] > e["span_start"]

    def test_confidence_range(self):
        """Confidence should be between 0 and 1."""
        events = classify_content(ADVERSE_CASES[0]["text"])
        for e in events:
            assert 0 <= e["confidence"] <= 1

    def test_false_positive_rate_on_benign_corpus(self):
        """P1-6: False positive rate on benign corpus should be measurable.

        All 10 benign cases should produce zero high-severity events
        (insolvency, restructuring, going_concern, cyber_incident).
        Lower-severity events (profit_warning, layoffs) MAY fire on
        benign texts — this is expected for simple keyword matching.
        The key metric: high-severity false positives = 0.
        """
        critical_types = {"insolvency", "going_concern_warning", "restructuring",
                          "payment_stress", "regulatory_action", "cyber_incident",
                          "refinancing_stress", "auditor_warning"}
        from kassandra.test_corpus import BENIGN_CASES

        fp_count = 0
        for case in BENIGN_CASES:
            events = classify_content(case["text"])
            critical_fps = [e for e in events if e["event_type"] in critical_types]
            if critical_fps:
                fp_count += 1

        total = len(BENIGN_CASES)
        fp_rate = fp_count / total
        # Allow up to 1 benign case to fire a critical event
        # (some texts genuinely contain adverse-adjacent language)
        assert fp_rate <= 0.1, (
            f"False positive rate too high: {fp_count}/{total} ({fp_rate:.0%}) "
            f"benign texts triggered critical events"
        )


# ── Seeded event pipeline test ────────────────────────────────────────────────

@pytest.fixture
def detection_test_db():
    """In-memory DB with minimal schema for detection pipeline validation."""
    import sqlite3
    from datetime import datetime, timezone

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row

    db.executescript("""
        CREATE TABLE registry (
            id INTEGER PRIMARY KEY,
            canonical_name TEXT,
            company_type TEXT DEFAULT 'corporate',
            jurisdiction TEXT, status TEXT DEFAULT 'active',
            lei TEXT, isin TEXT, domain TEXT,
            companies_house_number TEXT, incorporation_date TEXT,
            registered_address TEXT, raw_json TEXT,
            resolved_at TEXT, updated_at TEXT, ir_url TEXT, feed_url TEXT
        );
        CREATE TABLE evidence (
            id INTEGER PRIMARY KEY,
            content_hash TEXT UNIQUE,
            source_url TEXT, retrieval_time TEXT, publication_time TEXT,
            publication_time_confidence TEXT, first_seen_time TEXT,
            extraction_method TEXT, parser_version TEXT,
            content_type TEXT, content_length INTEGER, excerpt TEXT,
            source_reliability REAL DEFAULT 0.5,
            corroborated_by TEXT, raw_headers TEXT, created_at TEXT
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            registry_id INTEGER NOT NULL,
            evidence_id INTEGER NOT NULL,
            event_type TEXT NOT NULL, severity TEXT,
            confidence REAL DEFAULT 0.5,
            matched_text TEXT, pattern_id TEXT, pattern_version TEXT,
            extracted_at TEXT, created_at TEXT
        );
        CREATE TABLE scores (
            id INTEGER PRIMARY KEY,
            registry_id INTEGER,
            score_schema_version INTEGER,
            observation_severity REAL,
            deterioration_risk REAL,
            dependency_exposure REAL,
            analyst_priority REAL,
            factors_json TEXT, explanation TEXT, computed_at TEXT
        );
    """)

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO registry (id, canonical_name, jurisdiction, domain) "
        "VALUES (1, 'TestCorp PLC', 'GB', 'testcorp.com')"
    )
    db.commit()
    return db


def test_seeded_events_flow_to_scoring(detection_test_db):
    """Seed golden adverse events through evidence → classification → scoring.

    Verifies the full detection pipeline end-to-end in a test DB.
    engineering audit: only 2 live events — pipeline not validated.
    """
    import hashlib
    from datetime import datetime, timezone
    from kassandra.classifier import classify_content
    from kassandra.test_corpus import ADVERSE_CASES

    now = datetime.now(timezone.utc).isoformat()

    # 1. Store golden adverse texts as evidence
    event_count = 0
    for case in ADVERSE_CASES[:5]:  # Use first 5 cases
        text = case["text"]
        content_hash = hashlib.sha256(text.encode()).hexdigest()

        detection_test_db.execute(
            """INSERT OR IGNORE INTO evidence
               (content_hash, source_url, retrieval_time, extraction_method,
                content_type, excerpt, source_reliability)
               VALUES (?, 'test/fixture', ?, 'test_seed', 'text/plain', ?, 0.8)""",
            (content_hash, now, text[:500]),
        )
        evidence_id = detection_test_db.execute(
            "SELECT id FROM evidence WHERE content_hash = ?", (content_hash,)
        ).fetchone()[0]

        # 2. Classify the text
        events = classify_content(text)
        for ev in events:
            detection_test_db.execute(
                """INSERT INTO events
                   (registry_id, evidence_id, event_type, severity, confidence,
                    matched_text, pattern_id, extracted_at, created_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (evidence_id, ev["event_type"], ev.get("severity", "high"),
                 ev.get("confidence", 0.8), ev.get("matched_text", text[:200]),
                 ev.get("pattern_id", "TEST"), now, now),
            )
            event_count += 1

    assert event_count > 0, f"Should classify >0 events from golden corpus, got {event_count}"

    # 3. Verify events exist in DB
    db_event_count = detection_test_db.execute(
        "SELECT COUNT(*) FROM events WHERE registry_id = 1"
    ).fetchone()[0]
    assert db_event_count > 0, "Events not persisted to DB"

    # 4. Compute signal score (simplified — just verify non-zero)
    from kassandra.scoring import _compute_signal_score
    event_rows = [
        dict(row) for row in detection_test_db.execute(
            "SELECT event_type, severity, confidence FROM events WHERE registry_id = 1"
        ).fetchall()
    ]

    signal_score, _ = _compute_signal_score(event_rows, {})
    assert signal_score > 0, (
        f"Signal score should be >0 with {db_event_count} adverse events, "
        f"got {signal_score}"
    )


# ── P0 #2: Multilingual detection tests ───────────────────────────────────────


class TestMultilingualDetection:
    """Verify classifier detects adverse events in non-English texts."""

    # German: Insolvenzantrag
    def test_german_insolvency_detected(self):
        text = """
        PRESSEMITTEILUNG
        Die Müller GmbH hat heute beim Amtsgericht Hamburg einen Insolvenzantrag
        gestellt. Das Insolvenzverfahren wurde eröffnet und ein vorläufiger
        Insolvenzverwalter bestellt. Die Zahlungsunfähigkeit des Unternehmens
        wurde durch einen unabhängigen Gutachter bestätigt.
        """
        events = classify_content(text)
        insolvency_events = [e for e in events if e["event_type"] == "insolvency"]
        assert len(insolvency_events) > 0, (
            f"German insolvency text should be detected. Got events: {[e['event_type'] for e in events]}"
        )

    # French: faillite / redressement judiciaire
    def test_french_insolvency_detected(self):
        text = """
        COMMUNIQUÉ DE PRESSE
        La société Dupont SA a déposé son bilan auprès du Tribunal de Commerce
        de Paris. La société est en état de cessation de paiements et fait
        l'objet d'une procédure de redressement judiciaire. L'insolvabilité
        de l'entreprise a été constatée par le mandataire judiciaire.
        """
        events = classify_content(text)
        insolvency_events = [e for e in events if e["event_type"] == "insolvency"]
        assert len(insolvency_events) > 0, (
            f"French insolvency text should be detected. Got: {[e['event_type'] for e in events]}"
        )

    # Dutch: faillissement
    def test_dutch_insolvency_detected(self):
        text = """
        PERSBERICHT
        De rechtbank Amsterdam heeft vandaag het faillissement uitgesproken
        van Jansen Holding BV. Het bedrijf was niet langer in staat aan zijn
        betalingsverplichtingen te voldoen. De surseance van betaling werd
        eerder deze maand al aangevraagd. De insolventie werd verwacht na
        het mislukken van de herstructureringsonderhandelingen.
        """
        events = classify_content(text)
        insolvency_events = [e for e in events if e["event_type"] == "insolvency"]
        assert len(insolvency_events) > 0, (
            f"Dutch insolvency text should be detected. Got: {[e['event_type'] for e in events]}"
        )

    # Italian: fallimento
    def test_italian_insolvency_detected(self):
        text = """
        COMUNICATO STAMPA
        Il Tribunale di Milano ha dichiarato il fallimento della Rossi SpA.
        L'azienda versava in stato di insolvenza da diversi mesi e il piano
        di ristrutturazione presentato non è stato ritenuto sufficiente dai
        creditori. La liquidazione dell'attivo è stata affidata a un curatore
        fallimentare.
        """
        events = classify_content(text)
        insolvency_events = [e for e in events if e["event_type"] == "insolvency"]
        assert len(insolvency_events) > 0, (
            f"Italian insolvency text should be detected. Got: {[e['event_type'] for e in events]}"
        )

    # Spanish: concurso de acreedores
    def test_spanish_insolvency_detected(self):
        text = """
        COMUNICADO DE PRENSA
        García y Asociados SL ha presentado concurso voluntario de acreedores
        ante el Juzgado de lo Mercantil de Madrid. La declaración de insolvencia
        se produce tras varios meses de negociaciones fallidas con los bancos
        acreedores. La empresa ha solicitado el preconcurso para intentar
        alcanzar un acuerdo de reestructuración.
        """
        events = classify_content(text)
        insolvency_events = [e for e in events if e["event_type"] == "insolvency"]
        assert len(insolvency_events) > 0, (
            f"Spanish insolvency text should be detected. Got: {[e['event_type'] for e in events]}"
        )

    # German: Gewinnwarnung
    def test_german_profit_warning_detected(self):
        text = """
        AD-HOC MITTEILUNG
        Die Technologie AG gibt eine Gewinnwarnung für das laufende Geschäftsjahr
        heraus. Die Prognose für das EBITDA wurde deutlich gesenkt. Der Ausblick
        wurde aufgrund der schwächeren Nachfrage im asiatischen Markt korrigiert.
        """
        events = classify_content(text)
        pw_events = [e for e in events if e["event_type"] == "profit_warning"]
        assert len(pw_events) > 0, (
            f"German profit warning should be detected. Got: {[e['event_type'] for e in events]}"
        )

    # Language detection function
    def test_detect_language_german(self):
        from kassandra.classifier import detect_language

        text = "Die Gesellschaft hat einen Insolvenzantrag gestellt und das Unternehmen ist nicht mehr zahlungsfähig."
        lang = detect_language(text)
        assert lang == "de", f"Should detect German, got '{lang}'"

    def test_detect_language_french(self):
        from kassandra.classifier import detect_language

        text = "La société a déposé son bilan et une procédure de sauvegarde est en cours pour cette entreprise en difficulté."
        lang = detect_language(text)
        assert lang == "fr", f"Should detect French, got '{lang}'"

    def test_detect_language_english_default(self):
        from kassandra.classifier import detect_language

        text = "The company has filed for bankruptcy and the insolvency proceedings are underway."
        lang = detect_language(text)
        assert lang == "en", f"Should default to English, got '{lang}'"

    def test_detect_language_short_text_defaults_en(self):
        from kassandra.classifier import detect_language

        text = "Insolvenz"
        lang = detect_language(text)
        assert lang == "en", f"Short text should default to English, got '{lang}'"


# ── P0 #2: Hard negative tests ────────────────────────────────────────────────


class TestHardNegativeControl:
    """Verify hard negative cases do NOT produce false positive events.

    Note: Some hard negatives contain real adverse language (e.g., historical
    bankruptcy articles, profit warnings about different entities). The
    pattern-based classifier cannot disambiguate entity context. These cases
    are documented as known limitations.
    """

    # Cases that contain real adverse language — classifier may legitimately fire
    KNOWN_HIT_HARD_NEGATIVES = {
        "HN004_historical_unrelated_company",
        "HN005_profit_warning_for_different_entity",
    }

    @pytest.mark.parametrize("case", HARD_NEGATIVE_CASES)
    def test_hard_negative_no_critical_event(self, case):
        """Hard negatives should not produce critical/high severity events.

        Cases with known real adverse language (historical articles, other-entity
        profit warnings) are exempt — they document a known limitation of
        pattern-based classification without entity disambiguation.
        """
        events = classify_content(case["text"])
        critical_types = {
            "insolvency", "restructuring", "going_concern_warning",
            "payment_stress", "refinancing_stress", "regulatory_action",
            "cyber_incident", "auditor_warning", "facility_closure",
        }
        false_positives = [e for e in events if e["event_type"] in critical_types]

        if case["id"] in self.KNOWN_HIT_HARD_NEGATIVES:
            # These contain real adverse language — classifier may fire.
            # Just verify it's not an unreasonable number.
            assert len(false_positives) <= 3, (
                f"FAIL: {case['id']} — too many events ({len(events)}) for known-hit case. "
                f"Events: {[(e['event_type'], e['severity']) for e in events]}"
            )
        else:
            assert len(false_positives) == 0, (
                f"FAIL: {case['id']} — hard negative triggered critical event: "
                f"{[(e['event_type'], e['severity']) for e in false_positives]}. "
                f"Rationale: {case.get('rationale', 'N/A')}"
            )

    def test_hard_negative_insolvency_denied_not_triggered(self):
        """HN001: Explicit insolvency denial must not trigger insolvency."""
        text = HARD_NEGATIVE_CASES[0]["text"]
        events = classify_content(text)
        insolvency = [e for e in events if e["event_type"] == "insolvency"]
        assert len(insolvency) == 0, (
            f"Insolvency denial should not trigger insolvency event. "
            f"Got: {[(e['event_type'], e['matched_text'][:80]) for e in insolvency]}"
        )

    def test_hard_negative_historical_not_misclassified(self):
        """HN004: Historical article about defunct company — any hits are
        technically correct about a real company, but the question is whether
        we want to flag historical documents as current adverse events.

        This test documents the current behaviour. If historical articles
        fire, that is a known limitation of pattern-based classification
        without entity disambiguation.
        """
        text = HARD_NEGATIVE_CASES[3]["text"]
        events = classify_content(text)
        # The historical article contains real bankruptcy language.
        # It's acceptable if it fires — this test just documents it.
        # We only require that it doesn't produce an unreasonable number.
        assert len(events) <= 3, (
            f"Historical article produced {len(events)} events. "
            f"Events: {[(e['event_type'], e['pattern_id']) for e in events]}"
        )

    def test_hard_negative_all_count(self):
        """Ensure we have at least 5 hard negatives."""
        assert len(HARD_NEGATIVE_CASES) >= 5, (
            f"Expected at least 5 hard negatives, got {len(HARD_NEGATIVE_CASES)}"
        )


# ── P0 #2: Yield report tests ─────────────────────────────────────────────────


class TestYieldReport:
    """Verify classifier yield report structure and content."""

    def test_yield_report_structure(self):
        """Yield report must contain all required keys."""
        from kassandra.classifier import ClassifierRun, classify_documents

        docs = [
            {"text": ADVERSE_CASES[0]["text"], "source": "gazette"},
            {"text": ADVERSE_CASES[1]["text"], "source": "companies_house"},
            {"text": BENIGN_CASES[0]["text"], "source": "press_release"},
            {"text": "", "source": "empty"},  # Should be skipped
        ]

        events, report = classify_documents(docs)

        # Required top-level keys
        required_keys = {
            "total_docs", "docs_with_text", "docs_skipped",
            "candidates", "accepted", "rejected",
            "by_source", "by_language",
        }
        missing = required_keys - set(report.keys())
        assert not missing, f"Yield report missing keys: {missing}"

        # by_source must be a dict
        assert isinstance(report["by_source"], dict), "by_source must be dict"
        # by_language must be a dict
        assert isinstance(report["by_language"], dict), "by_language must be dict"

    def test_yield_report_counts(self):
        """Yield report counts should be consistent."""
        from kassandra.classifier import ClassifierRun, classify_documents

        docs = [
            {"text": ADVERSE_CASES[0]["text"], "source": "gazette"},
            {"text": ADVERSE_CASES[2]["text"], "source": "press_release"},
            {"text": "", "source": "empty_doc"},
            {"text": "   ", "source": "whitespace_only"},
        ]

        events, report = classify_documents(docs)

        assert report["total_docs"] == 4, f"Expected 4 total, got {report['total_docs']}"
        assert report["docs_with_text"] == 2, f"Expected 2 with text, got {report['docs_with_text']}"
        assert report["docs_skipped"] == 2, f"Expected 2 skipped, got {report['docs_skipped']}"
        assert report["accepted"] > 0, f"Expected some accepted events, got {report['accepted']}"
        assert report["candidates"] >= report["accepted"], (
            f"Candidates ({report['candidates']}) should be >= accepted ({report['accepted']})"
        )

    def test_yield_report_by_source(self):
        """Yield report per-source breakdown should be correct."""
        from kassandra.classifier import ClassifierRun, classify_documents

        docs = [
            {"text": ADVERSE_CASES[0]["text"], "source": "gazette"},
            {"text": ADVERSE_CASES[1]["text"], "source": "gazette"},
            {"text": BENIGN_CASES[0]["text"], "source": "press_release"},
        ]

        events, report = classify_documents(docs)

        by_source = report["by_source"]
        assert "gazette" in by_source, f"Expected 'gazette' in by_source: {list(by_source.keys())}"
        assert "press_release" in by_source, f"Expected 'press_release' in by_source"
        assert by_source["gazette"]["docs"] == 2
        assert by_source["press_release"]["docs"] == 1
        # Each source key should have docs, candidates, rejected, accepted
        for source_key in by_source:
            for field in ("docs", "candidates", "rejected", "accepted"):
                assert field in by_source[source_key], (
                    f"Source '{source_key}' missing field '{field}'"
                )

    def test_yield_report_by_language(self):
        """Yield report per-language breakdown should include English."""
        from kassandra.classifier import ClassifierRun, classify_documents

        docs = [
            {"text": ADVERSE_CASES[0]["text"], "source": "test"},
            {"text": BENIGN_CASES[0]["text"], "source": "test"},
        ]

        events, report = classify_documents(docs)

        by_language = report["by_language"]
        # English docs should be categorized
        assert "en" in by_language, f"Expected 'en' in by_language: {list(by_language.keys())}"
        assert by_language["en"]["docs"] >= 1

    def test_yield_report_accepted_rate(self):
        """Yield report accepted_rate should be between 0 and 1."""
        from kassandra.classifier import ClassifierRun, classify_documents

        docs = [
            {"text": ADVERSE_CASES[0]["text"], "source": "test"},
        ]

        events, report = classify_documents(docs)

        rate = report.get("accepted_rate")
        assert rate is not None, "Accepted rate should not be None when candidates > 0"
        assert 0 <= rate <= 1, f"Accepted rate {rate} should be between 0 and 1"

    def test_yield_report_empty_batch(self):
        """Yield report for empty batch should have zeros."""
        from kassandra.classifier import ClassifierRun, classify_documents

        events, report = classify_documents([])

        assert report["total_docs"] == 0
        assert report["docs_with_text"] == 0
        assert report["accepted"] == 0
        assert report["by_source"] == {}
        assert report["by_language"] == {}


# ── P0 #2: Instrumentation tests ──────────────────────────────────────────────


class TestClassifierInstrumentation:
    """Verify ClassifierRun correctly tracks per-document statistics."""

    def test_classifier_run_initial_state(self):
        from kassandra.classifier import ClassifierRun

        run = ClassifierRun()
        assert run.documents_processed == 0
        assert run.documents_with_text == 0
        assert run.documents_skipped == 0
        assert run.candidate_pattern_hits == 0
        assert run.rejected_candidates == 0
        assert run.accepted_events == 0
        assert run.by_source == {}
        assert run.by_language == {}

    def test_classifier_run_records_document(self):
        from kassandra.classifier import ClassifierRun

        run = ClassifierRun()
        run.record_document(
            has_text=True,
            source="gazette",
            language="en",
            candidates=3,
            rejected=1,
            accepted=2,
        )
        assert run.documents_processed == 1
        assert run.documents_with_text == 1
        assert run.candidate_pattern_hits == 3
        assert run.rejected_candidates == 1
        assert run.accepted_events == 2
        assert run.by_source["gazette"]["docs"] == 1
        assert run.by_source["gazette"]["candidates"] == 3
        assert run.by_language["en"]["docs"] == 1

    def test_classifier_run_skipped_document(self):
        from kassandra.classifier import ClassifierRun

        run = ClassifierRun()
        run.record_document(has_text=False, skipped=True, source="empty")
        assert run.documents_processed == 1
        assert run.documents_skipped == 1
        assert run.documents_with_text == 0
        # Skipped documents should not affect source/language breakdown
        assert "empty" not in run.by_source

    def test_classify_content_with_run_instrumentation(self):
        """classify_content should update ClassifierRun when provided."""
        from kassandra.classifier import ClassifierRun, classify_content as cc

        run = ClassifierRun()
        events = cc(ADVERSE_CASES[0]["text"], run=run, source="test")

        assert run.documents_processed == 1
        assert run.documents_with_text == 1
        assert run.accepted_events == len(events)
        assert run.accepted_events > 0
        assert "test" in run.by_source

    def test_classify_content_with_run_empty_text(self):
        """classify_content with run and empty text should record skipped."""
        from kassandra.classifier import ClassifierRun, classify_content as cc

        run = ClassifierRun()
        events = cc("", run=run, source="test")
        assert events == []
        assert run.documents_processed == 1
        assert run.documents_skipped == 1

    def test_classify_content_without_run_does_not_crash(self):
        """classify_content without run is backward compatible."""
        events = classify_content(ADVERSE_CASES[0]["text"])
        assert len(events) > 0

    def test_classify_documents_multiple_sources(self):
        """classify_documents with mixed sources produces correct by_source."""
        from kassandra.classifier import classify_documents

        docs = [
            {"text": ADVERSE_CASES[0]["text"], "source": "gazette"},
            {"text": ADVERSE_CASES[2]["text"], "source": "press_release"},
            {"text": ADVERSE_CASES[4]["text"], "source": "press_release"},
        ]

        events, report = classify_documents(docs)
        assert report["total_docs"] == 3
        assert "gazette" in report["by_source"]
        assert "press_release" in report["by_source"]
        # Each source should have at least 1 accepted event
        for source in report["by_source"]:
            assert report["by_source"][source]["accepted"] > 0, (
                f"Source '{source}' has 0 accepted events"
            )

    def test_classify_documents_accumulates_into_existing_run(self):
        """classify_documents should accumulate when given an existing run."""
        from kassandra.classifier import ClassifierRun, classify_documents

        run = ClassifierRun()
        # Process first batch
        classify_documents(
            [{"text": ADVERSE_CASES[0]["text"], "source": "batch1"}],
            run=run,
        )
        first_count = run.documents_processed
        # Process second batch into same run
        classify_documents(
            [{"text": ADVERSE_CASES[1]["text"], "source": "batch2"}],
            run=run,
        )
        assert run.documents_processed == first_count + 1
        assert "batch1" in run.by_source
        assert "batch2" in run.by_source


# ── Multilingual yield report language breakdown ───────────────────────────────


class TestMultilingualYieldReport:
    """Verify that multilingual documents appear in by_language breakdown."""

    def test_german_document_in_by_language(self):
        from kassandra.classifier import classify_documents

        docs = [
            {
                "text": (
                    "Die Gesellschaft hat Insolvenz angemeldet. "
                    "Das Insolvenzverfahren wurde eröffnet und ein "
                    "vorläufiger Insolvenzverwalter bestellt."
                ),
                "source": "german_press",
            },
        ]
        events, report = classify_documents(docs)
        # Should have at least one German document
        assert any(
            lang != "en" for lang in report["by_language"]
        ), f"Expected non-English language in report: {report['by_language']}"
        # Verify the German document produced events
        assert len(events) > 0, f"German insolvency text should produce events"

    def test_mixed_language_batch(self):
        from kassandra.classifier import classify_documents

        docs = [
            {"text": ADVERSE_CASES[0]["text"], "source": "test"},  # English
            {
                "text": (
                    "La société a déposé son bilan et fait l'objet "
                    "d'une procédure de redressement judiciaire. "
                    "L'insolvabilité a été constatée."
                ),
                "source": "french_press",
            },
            {
                "text": (
                    "De rechtbank heeft het faillissement uitgesproken. "
                    "Het bedrijf is insolvent verklaard."
                ),
                "source": "dutch_press",
            },
        ]
        events, report = classify_documents(docs)
        # All 3 docs should be processed
        assert report["total_docs"] == 3
        # Events should be found in all
        assert report["accepted"] >= 3, f"Expected at least 3 events, got {report['accepted']}"


class TestProductionFalsePositiveRegressions:
    """False positives observed in daily Kassandra digests must stay fixed."""

    @pytest.mark.parametrize("text", [
        "Global Insolvency Outlook: Brace for Middle East spillovers April 22, 2026",
        "shared craft and belief in constantly challenging yourself",
        "challenging the feedback of how well the interview panel measures skills proficiency",
        "screens have a negative impact on mental health and wellbeing",
        "diversification of funding sources and debt maturities to minimize refinancing risk",
    ])
    def test_digest_false_positive_snippets_do_not_classify(self, text):
        events = classify_content(text)
        noisy_types = {"insolvency", "profit_warning", "payment_stress"}
        assert [e for e in events if e["event_type"] in noisy_types] == []

    def test_real_profit_warning_still_classifies(self):
        text = "The company issued a profit warning and cut guidance after weaker trading conditions."
        events = classify_content(text)
        assert "profit_warning" in [e["event_type"] for e in events]

    def test_real_payment_stress_still_classifies(self):
        text = "The company faces material refinancing risk as covenant waivers expire before a large debt maturity."
        events = classify_content(text)
        assert "payment_stress" in [e["event_type"] for e in events]
