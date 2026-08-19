"""Point-in-time validation contract tests.

All data is synthetic and exists only to exercise contract enforcement; it is not
historical validation evidence or a Kassandra benchmark result.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kassandra.backtest import (
    CohortCase,
    EvaluationDocument,
    FrozenRunMetadata,
    PointInTimeManifest,
    ValidationContractError,
    _comparison_metrics,
    canonical_corpus_hash,
    run_point_in_time_evaluation,
)


def _document(case_id: str, date: str, text: str = "adverse signal") -> EvaluationDocument:
    timestamp = f"{date}T10:00:00+00:00"
    return EvaluationDocument(
        document_id=f"{case_id}-doc",
        case_id=case_id,
        publication_time=timestamp,
        retrieval_time=timestamp,
        text=text,
    )


def _manifest() -> PointInTimeManifest:
    cases = (
        CohortCase(
            case_id="distressed-1",
            cohort="distressed",
            sector="Industrials",
            matched_group_id="industrials-2020",
            outcome_date="2020-12-31",
        ),
        CohortCase(
            case_id="control-1",
            cohort="control",
            sector="Industrials",
            matched_group_id="industrials-2020",
            outcome_date="2020-12-30",
        ),
    )
    documents = (
        _document("distressed-1", "2020-06-01"),
        _document("control-1", "2020-06-01", "ordinary update"),
    )
    return PointInTimeManifest(
        manifest_id="synthetic-contract-fixture",
        as_of="2021-07-01T00:00:00+00:00",
        cases=cases,
        documents=documents,
        frozen_metadata=FrozenRunMetadata(
            direct_scorer_id="direct-scorer@fixture",
            graph_scorer_id="graph-scorer@fixture",
            classifier_id="classifier@fixture",
            config_id="config@fixture",
            corpus_hash=canonical_corpus_hash(documents),
        ),
    )


def test_rejects_evidence_retrieved_after_as_of() -> None:
    manifest = _manifest()
    leaked = replace(
        manifest.documents[0], retrieval_time="2021-07-02T00:00:00+00:00"
    )
    manifest = replace(
        manifest, documents=(leaked, manifest.documents[1]))

    with pytest.raises(ValidationContractError, match="retrieval_time"):
        run_point_in_time_evaluation(manifest)


def test_missing_time_and_sector_matched_control_fails_closed() -> None:
    manifest = _manifest()
    manifest = replace(manifest, cases=(manifest.cases[0],))

    with pytest.raises(ValidationContractError, match="control"):
        run_point_in_time_evaluation(manifest)


def test_missing_frozen_metadata_is_explicitly_exploratory_when_requested() -> None:
    manifest = _manifest()
    metadata = replace(manifest.frozen_metadata, config_id="")
    manifest = replace(manifest, frozen_metadata=metadata)

    result = run_point_in_time_evaluation(manifest, allow_exploratory=True)

    assert result.status == "exploratory_unvalidated"
    assert "config_id" in result.validation_issues[0]


def test_reports_case_denominators_and_confusion_metrics() -> None:
    manifest = _manifest()

    result = run_point_in_time_evaluation(
        manifest,
        direct_scorer=lambda case, documents: case.case_id == "distressed-1",
        graph_scorer=lambda case, documents: case.case_id == "distressed-1",
    )

    assert result.status == "validated_contract"
    assert result.direct_only.denominators == {"distressed": 1, "controls": 1, "total": 2}
    assert result.direct_only.confusion == {"tp": 1, "fp": 0, "fn": 0, "tn": 1}
    assert result.direct_only.precision == 1.0
    assert result.direct_only.recall == 1.0
    assert result.direct_only.specificity == 1.0
    assert result.direct_only.lead_time_days == [212]


def test_reports_direct_only_and_graph_enhanced_chronological_outputs() -> None:
    manifest = _manifest()

    result = run_point_in_time_evaluation(
        manifest,
        direct_scorer=lambda case, documents: False,
        graph_scorer=lambda case, documents: case.case_id == "distressed-1",
    )

    assert result.direct_only.confusion == {"tp": 0, "fp": 0, "fn": 1, "tn": 1}
    assert result.graph_enhanced.confusion == {"tp": 1, "fp": 0, "fn": 0, "tn": 1}
    assert result.direct_only.predictions["distressed-1"] is False
    assert result.graph_enhanced.predictions["distressed-1"] is True


def test_comparison_metrics_pre_parses_timestamps_linearly():
    """_comparison_metrics must pre-group docs and parse timestamps once, not
    re-parse for every case × document pair (quadratic).

    The test verifies linear behaviour: with N documents spread across M cases,
    _parse_timestamp should be called exactly 2N times (publication + retrieval
    per document), not M × N × 2.
    """
    from unittest.mock import patch
    from kassandra import backtest as bt_module

    manifest = _manifest()
    # Add a few extra cases and documents to make the quadratic blow-up
    # measurable if pre-parsing is missing.
    extra_cases = []
    extra_docs = []
    for i in range(5):
        cid = f"distressed-{i + 2}"
        extra_cases.append(
            CohortCase(cid, "distressed", "Industrials", "industrials-2020", "2020-12-31")
        )
        extra_docs.append(_document(cid, "2020-06-01"))
        extra_cases.append(
            CohortCase(f"control-{i + 2}", "control", "Industrials", "industrials-2020", "2020-12-30")
        )
        extra_docs.append(_document(f"control-{i + 2}", "2020-06-01", "ordinary update"))
    manifest = replace(
        manifest,
        cases=manifest.cases + tuple(extra_cases),
        documents=manifest.documents + tuple(extra_docs),
    )

    call_count = 0

    def counting_parse(value, field_name):
        nonlocal call_count
        call_count += 1
        from datetime import datetime
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValidationContractError(f"{field_name}: {value!r}") from exc
        if parsed.tzinfo is None:
            raise ValidationContractError(f"{field_name} must include timezone: {value!r}")
        return parsed

    with patch.object(bt_module, "_parse_timestamp", side_effect=counting_parse):
        metrics = _comparison_metrics(
            manifest, lambda case, documents: case.case_id.startswith("distressed")
        )

    total_docs = len(manifest.documents)
    total_cases = len(manifest.cases)
    # Linear expectation: 2 calls per document (publication + retrieval time)
    expected_max = 2 * total_docs

    assert call_count == expected_max, (
        f"Expected {expected_max} _parse_timestamp calls (2 × {total_docs} docs), "
        f"got {call_count}. If this were quadratic it would be "
        f"{total_cases} × {total_docs} × 2 = {total_cases * total_docs * 2}."
    )
