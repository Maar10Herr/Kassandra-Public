"""Historical backtest framework.

Replays benchmark cases through the detection pipeline to measure:
- Detection accuracy (event type, severity)
- Lead time (first signal → deterioration date)
- False negative rate (signals missed)
- Score escalation (does priority increase with adverse signals?)

Per engineering audit P4: must prove detection works on known cases
before expanding graph or deploying.
"""

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal

from kassandra.classifier import classify_content

logger = logging.getLogger(__name__)


# The curated benchmark below remains intentionally separate from this contract.
# It has no immutable source manifest or matched controls and is therefore only
# suitable for classifier/regression exploration.
ValidationScorer = Callable[["CohortCase", list["EvaluationDocument"]], bool]


class ValidationContractError(ValueError):
    """Raised when a cohort cannot support a point-in-time product claim."""


@dataclass(frozen=True)
class EvaluationDocument:
    """Immutable source record supplied to a point-in-time evaluation."""

    document_id: str
    case_id: str
    publication_time: str
    retrieval_time: str
    text: str
    source_url: str | None = None
    evidence_hash: str | None = None


@dataclass(frozen=True)
class CohortCase:
    """One distressed case or matched non-distressed control."""

    case_id: str
    cohort: Literal["distressed", "control"]
    sector: str
    matched_group_id: str
    outcome_date: str


@dataclass(frozen=True)
class FrozenRunMetadata:
    """Identifiers that make both comparison arms reproducible."""

    direct_scorer_id: str
    graph_scorer_id: str
    classifier_id: str
    config_id: str
    corpus_hash: str


@dataclass(frozen=True)
class PointInTimeManifest:
    """Versioned input to a product-validation run; never infer missing facts."""

    manifest_id: str
    as_of: str
    cases: tuple[CohortCase, ...]
    documents: tuple[EvaluationDocument, ...]
    frozen_metadata: FrozenRunMetadata
    max_control_outcome_gap_days: int = 366


@dataclass(frozen=True)
class ComparisonMetrics:
    predictions: dict[str, bool]
    denominators: dict[str, int]
    confusion: dict[str, int]
    precision: float | None
    recall: float | None
    specificity: float | None
    lead_time_days: list[int]


@dataclass(frozen=True)
class PointInTimeEvaluation:
    status: Literal["validated_contract", "exploratory_unvalidated"]
    validation_issues: list[str]
    direct_only: ComparisonMetrics | None
    graph_enhanced: ComparisonMetrics | None


def canonical_corpus_hash(documents: tuple[EvaluationDocument, ...]) -> str:
    """Return a stable hash of every supplied document and its provenance fields."""
    canonical = [asdict(document) for document in sorted(documents, key=lambda d: d.document_id)]
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationContractError(f"{field_name} must be an ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValidationContractError(f"{field_name} must include an explicit timezone: {value!r}")
    return parsed


def _manifest_issues(manifest: PointInTimeManifest) -> list[str]:
    """Validate all prerequisites before a scorer sees any historical content."""
    issues: list[str] = []
    try:
        as_of = _parse_timestamp(manifest.as_of, "as_of")
    except ValidationContractError as exc:
        return [str(exc)]

    metadata = manifest.frozen_metadata
    for field_name in (
        "direct_scorer_id",
        "graph_scorer_id",
        "classifier_id",
        "config_id",
        "corpus_hash",
    ):
        if not getattr(metadata, field_name):
            issues.append(f"frozen_metadata.{field_name} is required")
    if metadata.corpus_hash and metadata.corpus_hash != canonical_corpus_hash(manifest.documents):
        issues.append("frozen_metadata.corpus_hash does not match immutable documents")

    cases_by_id = {case.case_id: case for case in manifest.cases}
    if len(cases_by_id) != len(manifest.cases):
        issues.append("case_id values must be unique")
    if not any(case.cohort == "distressed" for case in manifest.cases):
        issues.append("cohort requires at least one distressed case")
    if not any(case.cohort == "control" for case in manifest.cases):
        issues.append("cohort requires at least one non-distressed control")

    groups: dict[str, list[CohortCase]] = {}
    for case in manifest.cases:
        if not case.sector or not case.matched_group_id or not case.outcome_date:
            issues.append(f"case {case.case_id} requires sector, matched_group_id, and outcome_date")
            continue
        try:
            outcome = _parse_timestamp(f"{case.outcome_date}T00:00:00+00:00", "outcome_date")
            if outcome > as_of:
                issues.append(f"case {case.case_id} outcome_date is after as_of")
        except ValidationContractError as exc:
            issues.append(str(exc))
        groups.setdefault(case.matched_group_id, []).append(case)

    for group_id, cases in groups.items():
        distressed = [case for case in cases if case.cohort == "distressed"]
        controls = [case for case in cases if case.cohort == "control"]
        if not distressed or not controls:
            issues.append(f"matched group {group_id} requires distressed cases and non-distressed controls")
            continue
        sectors = {case.sector for case in cases}
        if len(sectors) != 1:
            issues.append(f"matched group {group_id} is not sector-matched")
        dates = [datetime.fromisoformat(f"{case.outcome_date}T00:00:00+00:00") for case in cases]
        if (max(dates) - min(dates)).days > manifest.max_control_outcome_gap_days:
            issues.append(f"matched group {group_id} is not time-matched")

    for document in manifest.documents:
        if document.case_id not in cases_by_id:
            issues.append(f"document {document.document_id} references an unknown case")
            continue
        try:
            publication = _parse_timestamp(document.publication_time, "publication_time")
            retrieval = _parse_timestamp(document.retrieval_time, "retrieval_time")
            if publication > as_of:
                issues.append(f"document {document.document_id} publication_time is after as_of")
            if retrieval > as_of:
                issues.append(f"document {document.document_id} retrieval_time is after as_of")
        except ValidationContractError as exc:
            issues.append(str(exc))
    return issues


def _comparison_metrics(manifest: PointInTimeManifest, scorer: ValidationScorer) -> ComparisonMetrics:
    predictions: dict[str, bool] = {}
    lead_times: list[int] = []
    tp = fp = fn = tn = 0

    # Pre-group documents by case_id and parse timestamps once (linear, not
    # cases × all_documents quadratic scan).
    docs_by_case: dict[str, list[tuple[EvaluationDocument, datetime, datetime]]] = {}
    for document in manifest.documents:
        pub = _parse_timestamp(document.publication_time, "publication_time")
        ret = _parse_timestamp(document.retrieval_time, "retrieval_time")
        docs_by_case.setdefault(document.case_id, []).append((document, pub, ret))

    for case in manifest.cases:
        outcome = datetime.fromisoformat(f"{case.outcome_date}T00:00:00+00:00")
        pre_parsed = docs_by_case.get(case.case_id, [])
        eligible = [
            (doc, pub) for doc, pub, ret in pre_parsed
            if pub <= outcome and ret <= outcome
        ]
        predicted = bool(scorer(case, [d for d, _ in eligible]))
        predictions[case.case_id] = predicted
        if case.cohort == "distressed":
            if predicted:
                tp += 1
                signal_times = [pub for _, pub in eligible]
                if signal_times:
                    lead_times.append((outcome - min(signal_times)).days)
            else:
                fn += 1
        elif predicted:
            fp += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    specificity = tn / (tn + fp) if tn + fp else None
    return ComparisonMetrics(
        predictions=predictions,
        denominators={"distressed": tp + fn, "controls": tn + fp, "total": tp + fn + tn + fp},
        confusion={"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        precision=precision,
        recall=recall,
        specificity=specificity,
        lead_time_days=lead_times,
    )


def run_point_in_time_evaluation(
    manifest: PointInTimeManifest,
    direct_scorer: ValidationScorer | None = None,
    graph_scorer: ValidationScorer | None = None,
    *,
    allow_exploratory: bool = False,
) -> PointInTimeEvaluation:
    """Compare frozen direct and graph scorers without admitting leaked evidence.

    Missing prerequisites fail closed by default.  Callers that need to inspect an
    incomplete research cohort may opt in to an explicitly non-product result.
    """
    issues = _manifest_issues(manifest)
    if issues:
        if allow_exploratory:
            return PointInTimeEvaluation("exploratory_unvalidated", issues, None, None)
        raise ValidationContractError("; ".join(issues))
    if direct_scorer is None or graph_scorer is None:
        message = "direct_scorer and graph_scorer are required for a chronological comparison"
        if allow_exploratory:
            return PointInTimeEvaluation("exploratory_unvalidated", [message], None, None)
        raise ValidationContractError(message)
    return PointInTimeEvaluation(
        "validated_contract",
        [],
        _comparison_metrics(manifest, direct_scorer),
        _comparison_metrics(manifest, graph_scorer),
    )


@dataclass
class BacktestResult:
    """Result of running a single signal through the classifier."""
    case_id: str
    signal_date: str
    expected_type: str
    expected_severity: str
    detected: bool
    detected_type: str | None = None
    detected_severity: str | None = None
    confidence: float = 0.0
    matched_pattern: str | None = None
    matched_text: str | None = None
    error: str | None = None


@dataclass
class CaseBacktestSummary:
    """Summary of backtest for one distressed case."""
    case_id: str
    company_name: str
    deterioration_date: str
    total_signals: int
    signals_detected: int
    signals_missed: int
    first_detection_date: str | None = None
    lead_time_days: int = 0
    severity_correct: int = 0
    type_correct: int = 0
    results: list[BacktestResult] = field(default_factory=list)


def run_backtest_for_case(case: Any) -> CaseBacktestSummary:
    """Run backtest for a single benchmark case.

    Feeds each signal point through the classifier and records results.
    """
    from kassandra.benchmark import SignalPoint, DistressedCase

    results: list[BacktestResult] = []

    for signal in case.signal_timeline:
        result = _classify_signal(case.case_id, signal)
        results.append(result)

    # Compute summary
    detected = [r for r in results if r.detected]
    missed = [r for r in results if not r.detected]
    type_correct = sum(
        1 for r in detected if r.detected_type == r.expected_type
    )
    severity_correct = sum(
        1 for r in detected if r.detected_severity == r.expected_severity
    )

    first = detected[0].signal_date if detected else None
    det_date = datetime.fromisoformat(case.deterioration_date)
    lead_days = 0
    if first:
        first_date = datetime.fromisoformat(first)
        lead_days = (det_date - first_date).days

    return CaseBacktestSummary(
        case_id=case.case_id,
        company_name=case.company_name,
        deterioration_date=case.deterioration_date,
        total_signals=len(case.signal_timeline),
        signals_detected=len(detected),
        signals_missed=len(missed),
        first_detection_date=first,
        lead_time_days=lead_days,
        severity_correct=severity_correct,
        type_correct=type_correct,
        results=results,
    )


def _classify_signal(case_id: str, signal: Any) -> BacktestResult:
    """Classify a single signal point."""
    text = signal.description
    if signal.excerpt:
        text = signal.excerpt  # Use the more detailed excerpt if available

    try:
        events = classify_content(text)

        # Check if expected type is detected
        matching = [e for e in events if e["event_type"] == signal.event_type]

        if matching:
            best = matching[0]
            return BacktestResult(
                case_id=case_id,
                signal_date=signal.date,
                expected_type=signal.event_type,
                expected_severity=signal.severity,
                detected=True,
                detected_type=best["event_type"],
                detected_severity=best["severity"],
                confidence=best["confidence"],
                matched_pattern=best["pattern_id"],
                matched_text=best["matched_text"][:100],
            )
        elif events:
            # Detected something else
            return BacktestResult(
                case_id=case_id,
                signal_date=signal.date,
                expected_type=signal.event_type,
                expected_severity=signal.severity,
                detected=True,
                detected_type=events[0]["event_type"],
                detected_severity=events[0]["severity"],
                confidence=events[0]["confidence"],
                matched_pattern=events[0]["pattern_id"],
                matched_text=events[0]["matched_text"][:100],
            )
        else:
            return BacktestResult(
                case_id=case_id,
                signal_date=signal.date,
                expected_type=signal.event_type,
                expected_severity=signal.severity,
                detected=False,
            )
    except Exception as e:
        return BacktestResult(
            case_id=case_id,
            signal_date=signal.date,
            expected_type=signal.event_type,
            expected_severity=signal.severity,
            detected=False,
            error=str(e),
        )


def run_full_backtest() -> list[CaseBacktestSummary]:
    """Run backtest against all benchmark cases."""
    from kassandra.benchmark import BENCHMARK_CASES

    summaries = []
    for case in BENCHMARK_CASES:
        summary = run_backtest_for_case(case)
        summaries.append(summary)
    return summaries


def print_backtest_report(summaries: list[CaseBacktestSummary]) -> str:
    """Generate a human-readable backtest report."""
    lines = []
    lines.append("=" * 70)
    lines.append("KASSANDRA CURATED CLASSIFIER/REGRESSION EXPLORATION REPORT")
    lines.append("=" * 70)
    lines.append("")

    total_signals = sum(s.total_signals for s in summaries)
    total_detected = sum(s.signals_detected for s in summaries)
    total_missed = sum(s.signals_missed for s in summaries)
    total_type_correct = sum(s.type_correct for s in summaries)
    total_severity_correct = sum(s.severity_correct for s in summaries)

    lines.append(f"Cases tested: {len(summaries)}")
    lines.append(f"Total signals: {total_signals}")
    lines.append(f"Detected: {total_detected} ({total_detected/total_signals*100:.0f}%)")
    lines.append(f"Missed: {total_missed} ({total_missed/total_signals*100:.0f}%)")
    lines.append(f"Type correct: {total_type_correct}/{total_detected}")
    lines.append(f"Severity correct: {total_severity_correct}/{total_detected}")
    lines.append("")

    lines.append("-" * 70)
    lines.append(f"{'Case':<30s} {'Signals':>8s} {'Detected':>8s} {'Lead Time':>10s} {'Type OK':>8s} {'Sev OK':>8s}")
    lines.append("-" * 70)

    for s in summaries:
        lead = f"{s.lead_time_days}d" if s.lead_time_days > 0 else "same day"
        lines.append(
            f"{s.company_name[:30]:<30s} "
            f"{s.total_signals:>8d} "
            f"{s.signals_detected:>8d} "
            f"{lead:>10s} "
            f"{s.type_correct:>8d} "
            f"{s.severity_correct:>8d}"
        )

    lines.append("-" * 70)
    lines.append("")

    # Detailed per-case breakdown
    for s in summaries:
        lines.append(f"\n## {s.company_name} ({s.case_id})")
        lines.append(f"   Deterioration: {s.deterioration_date} | "
                      f"Lead time: {s.lead_time_days} days from first signal")
        lines.append("")

        for r in s.results:
            status = "✅" if r.detected and r.detected_type == r.expected_type else (
                "⚠️" if r.detected else "❌"
            )
            type_match = r.detected_type == r.expected_type
            sev_match = r.detected_severity == r.expected_severity
            lines.append(
                f"   {status} {r.signal_date} — "
                f"Expected: {r.expected_type}/{r.expected_severity} | "
                f"Got: {r.detected_type or 'MISSED'}/{r.detected_severity or 'MISSED'}"
            )
            if r.error:
                lines.append(f"      Error: {r.error}")
            if r.matched_pattern:
                lines.append(f"      Pattern: {r.matched_pattern} "
                             f"(conf: {r.confidence:.2f}) "
                             f"— \"{r.matched_text or ''}\"")

    lines.append("")
    lines.append("=" * 70)
    lines.append("NOT A VALIDATED PRODUCT BACKTEST: this curated positive-only set has no")
    lines.append("matched controls, immutable source manifest, or frozen point-in-time corpus.")

    return "\n".join(lines)
