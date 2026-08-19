"""Transparent investigation scoring with explainable components.

⚠ AUDIT FINDING (engineering audit 2026-06-19): This scoring system currently measures
legal ownership exposure only. It does NOT measure economic dependency
intelligence. All edge materiality is NULL (unknown). Renamed accordingly.

P2-5: ANALYST PRIORITY TARGET CLARIFICATION

analyst_priority is explicitly a HEURISTIC INVESTIGATION TRIAGE score.
It answers: "which companies should an analyst review today?"

Separate scores that should eventually be computed independently:
1. review_urgency — which companies need attention NOW?
2. deterioration_likelihood — probability of adverse credit event
   (requires calibrated models — NOT yet implemented)
3. exposure_severity — material impact if counterparty deteriorates
4. information_gap — companies needing enrichment/document collection

SCORING v3 (2026-06-20): Split into two independent scores:
- active_watch_priority: gated, multiplicative — ZERO without adverse signal
- coverage_monitor_priority: data quality/enrichment — NEVER credit risk

Components:
1. signal_score — adverse event evidence strength
2. recency_score — how fresh the signal is
3. credibility_score — source reliability × event confidence
4. entity_relevance_score — match quality to portfolio entity
5. legal_ownership_exposure — GLEIF legal parent/subsidiary graph exposure (NOT economic dependency)
6. economic_dep_exposure — pilot/automated customer, supplier, facility edges
7. materiality_score — economic importance (if known; currently always 0.0 — no data)
8. active_watch_priority — gated triage ranking (ZERO without adverse signal)
9. coverage_monitor_priority — data quality/enrichment score
"""

import hashlib
import json
import logging
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from kassandra.config import get_config
from kassandra.observability import journal_event

logger = logging.getLogger(__name__)

SCORER_VERSION = "3.0.0"


def _canonical_json(value: Any) -> str:
    """Serialize score inputs deterministically before fingerprinting."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _build_score_input_manifest(
    db: sqlite3.Connection, weights: dict[str, Any]
) -> dict[str, Any]:
    """Build the canonical, run-wide input record used for score provenance.

    ``global`` freshness is the latest successful completion per source adapter.
    ``entity_coverage`` is separate: the active evidence and relevant graph
    edges attached to each portfolio entity.  A source being fresh globally
    does not claim that it has covered every entity.

    Returns a dict with keys:
        manifest      — canonical provenance payload (dict)
        fingerprint   — SHA-256 of the manifest
        active_events — raw sqlite3.Row list (reused by compute_scores to
                        avoid a second active-event scan)
    """
    config = get_config()
    source_freshness = {
        row["source_name"]: row["completed_at"]
        for row in db.execute(
            """SELECT source_name, MAX(completed_at) AS completed_at
               FROM source_runs
               WHERE completed_at IS NOT NULL
               GROUP BY source_name
               ORDER BY source_name"""
        ).fetchall()
    }
    coverage: dict[str, set[str]] = {}
    for row in db.execute(
        """SELECT e.registry_id, ev.extraction_method AS source_name
           FROM events e JOIN evidence ev ON ev.id = e.evidence_id
           WHERE e.active = 1 AND e.status = 'active'
           UNION
           SELECT edge.source_registry_id, edge.edge_source FROM edges edge
           WHERE edge.edge_source IS NOT NULL
           UNION
           SELECT edge.target_registry_id, edge.edge_source FROM edges edge
           WHERE edge.edge_source IS NOT NULL"""
    ).fetchall():
        if row["registry_id"] is not None and row["source_name"]:
            coverage.setdefault(str(row["registry_id"]), set()).add(row["source_name"])

    registry_rows = db.execute(
        """SELECT r.id, r.canonical_name, r.companies_house_number, r.lei,
                  r.isin, r.jurisdiction, r.company_type, r.status,
                  r.domain, r.raw_json, r.updated_at
           FROM scoreable_companies r ORDER BY r.id"""
    ).fetchall()
    active_events = db.execute(
        """SELECT e.id, e.registry_id, e.evidence_id, e.event_type,
                  e.event_subtype, e.severity, e.confidence, e.extracted_at,
                  e.active, e.status, ev.content_hash, ev.publication_time,
                  ev.retrieval_time AS availability_time
           FROM events e JOIN evidence ev ON ev.id = e.evidence_id
           WHERE e.active = 1 AND e.status = 'active'
           ORDER BY e.id"""
    ).fetchall()
    relevant_edges = db.execute(
        """SELECT e.id, e.source_registry_id, e.target_registry_id,
                  e.relationship_type, e.evidence_id, e.edge_source,
                  e.confidence, e.economic_materiality, e.operational_criticality,
                  e.concentration, e.replaceability, e.quality_tier,
                  e.quality_score, e.valid_from, e.valid_until
           FROM edges e
           WHERE e.source_registry_id IN (SELECT id FROM scoreable_companies)
              OR e.target_registry_id IN (SELECT id FROM scoreable_companies)
           ORDER BY e.id"""
    ).fetchall()
    portfolio_membership = db.execute(
        """SELECT p.id AS portfolio_id, p.name AS portfolio_name,
                  pi.id AS item_id, pi.ticker, pi.isin, pi.name, pi.sector,
                  pi.country, pi.weight, pi.source
           FROM portfolios p JOIN portfolio_items pi ON pi.portfolio_id = p.id
           ORDER BY p.id, pi.id"""
    ).fetchall()
    manifest = {
        "schema_version": config.get("scoring", "schema_version", default=1),
        "score_schema_version": int(SCORER_VERSION.split(".")[0]),
        "scorer_version": SCORER_VERSION,
        "scoring_config": config.get("scoring", default={}),
        "weights": weights,
        "portfolio_membership": [dict(row) for row in portfolio_membership],
        "registry_identity": [dict(row) for row in registry_rows],
        "active_evidence": [
            {
                "id": row["evidence_id"], "content_hash": row["content_hash"],
                "publication_time": row["publication_time"],
                "availability_time": row["availability_time"],
            }
            for row in active_events
        ],
        "active_events": [
            {key: row[key] for key in ("id", "registry_id", "evidence_id", "event_type",
                                        "event_subtype", "severity", "confidence", "extracted_at",
                                        "active", "status")}
            for row in active_events
        ],
        "relevant_edges": [dict(row) for row in relevant_edges],
        "source_freshness": {
            "global": source_freshness,
            "entity_coverage": {key: sorted(value) for key, value in sorted(coverage.items())},
        },
    }
    return {
        "manifest": manifest,
        "fingerprint": hashlib.sha256(_canonical_json(manifest).encode()).hexdigest(),
        "active_events": active_events,
    }

# ── Transmission risk multipliers (module-level, shared) ─────────────────────

# Shock channel severity multipliers (higher = worse transmission risk)
SHOCK_CHANNEL_MULT = {
    "demand_loss": 1.5,
    "customer_concentration": 1.5,
    "supplier_disruption": 1.3,
    "operational_shutdown": 1.4,
    "input_cost_inflation": 1.1,
    "commodity_price": 1.2,
    "logistics_disruption": 1.1,
    "credit_exposure": 1.0,
    "refinancing_liquidity": 0.9,
    "legal_regulatory": 0.8,
    "geographic_conflict": 0.9,
    "environmental_compliance": 0.7,
    "ownership_control": 0.7,
    "reputational": 0.6,
    "unknown": 0.8,
}

# Lag multipliers — closer shock = higher urgency
LAG_MULT = {
    "immediate": 1.3,
    "days": 1.1,
    "weeks": 1.0,
    "months": 0.8,
    "annual_cycle": 0.6,
    "unknown": 0.9,
}

# Buffer multipliers — better buffer = lower risk
BUFFER_MULT = {
    "single_source": 1.3,
    "unknown": 1.0,
    "inventory_buffer": 0.8,
    "multi_supplier": 0.7,
    "contractual": 0.9,
    "financial_reserve": 0.8,
    "regulated_service": 0.85,
}


def compute_scores(
    db: sqlite3.Connection, *, persist: bool = True,
) -> list[dict[str, Any]]:
    """Compute explainable investigation scores for all portfolio companies.

    v3: Splits analyst_priority into two independent scores:
        - active_watch_priority: gated/multiplicative, ZERO without adverse signal
        - coverage_monitor_priority: data quality/enrichment only

    Each score is decomposed into components with human-readable explanation.
    Only stores a new snapshot when scores change (deduplication).

    Returns list of score results sorted by active_watch_priority descending,
    then coverage_monitor_priority descending.
    """
    config = get_config()
    weights = config.get("scoring", "weights", default={})
    now = datetime.now(timezone.utc).isoformat()

    input_record = _build_score_input_manifest(db, weights)
    input_fingerprint = input_record["fingerprint"]
    # A scoring run is the immutable combination of canonical inputs and scorer
    # implementation.  Use a deterministic identifier so score rows always
    # reference the manifest row even when an identical run is deduplicated.
    run_id = f"score-{SCORER_VERSION}-{input_fingerprint[:20]}"
    provenance_json = _canonical_json(input_record["manifest"])

    # Build active_events_by_registry from the already-fetched rows (no second scan)
    active_events_by_registry: dict[int, list[dict[str, Any]]] = {}
    for event in input_record["active_events"]:
        if event["event_type"] not in ('unconfirmed_review_candidate', 'unconfirmed_adverse'):
            active_events_by_registry.setdefault(event["registry_id"], []).append(dict(event))

    # Persist the run-level provenance only for analytical snapshot runs. The
    # real-time daemon passes persist=False because alerting is event-watermark
    # based and must not write this large manifest every 15 minutes.
    if persist:
        db.execute(
            """INSERT OR IGNORE INTO scoring_runs
               (run_id, input_fingerprint, scorer_version, provenance_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (run_id, input_fingerprint, SCORER_VERSION, provenance_json, now),
        )

    # Event rows are structured. Independent GROUP_CONCAT aggregates can reorder
    # or de-duplicate columns separately and therefore corrupt event tuples.
    rows = db.execute("""
        SELECT r.id as registry_id, r.canonical_name, r.companies_house_number,
               r.jurisdiction, r.status as registry_status
        FROM scoreable_companies r
        ORDER BY r.id
    """).fetchall()

    results = []
    for row in rows:
        registry_id = row["registry_id"]

        event_rows = active_events_by_registry.get(registry_id, [])
        event_list = [event["event_type"] for event in event_rows]
        event_count = len(event_rows)
        latest_event_at = max((event["extracted_at"] for event in event_rows), default=None)

        # 1. Signal score — adverse event evidence
        signal_score, signal_factors = _compute_signal_score(event_rows, weights)

        # 2. Recency score — how fresh
        recency_score = _compute_recency(latest_event_at)

        # 3. Credibility score — source reliability × event confidence
        credibility_score = _compute_credibility(db, registry_id)

        # 4. Entity relevance — match quality to portfolio
        entity_relevance = _compute_entity_relevance(row)

        # 5. Legal ownership exposure — GLEIF graph (NOT economic dependency)
        legal_ownership_exposure = _compute_legal_ownership_exposure(db, registry_id)
        exposure_normalized = min(legal_ownership_exposure / 100.0, 1.0)

        # P2-1: Separate economic dependency exposure from legal ownership
        economic_dep_exposure = _compute_economic_dependency_exposure(db, registry_id)
        econ_exposure_normalized = min(economic_dep_exposure / 50.0, 1.0)

        # P1-4: Check if graph collection was capped
        is_capped = _is_graph_capped(db, registry_id)

        # 6. Materiality — from edges (currently all NULL — no data)
        materiality_score = _compute_materiality(db, registry_id)
        materiality_known = _is_materiality_known(db, registry_id)

        # 7. Transmission signal — exposed via dependency to adverse entity
        transmission_signal_score = _compute_transmission_signal(db, registry_id)

        # 8. Source staleness — globally precomputed freshness filtered by this
        # entity's independently recorded coverage sources.
        source_staleness_score = _compute_source_staleness(
            registry_id,
            input_record["manifest"]["source_freshness"],
        )

        # ── v3: Active Watch Priority (gated, multiplicative) ─────────────────
        # Only non-zero when there's a genuine deterioration signal
        # recency_decay: 0.20 floor even for very old events, 1.0 for today
        recency_decay = 0.20 + 0.80 * recency_score
        # materiality_factor: 0.5 default when unknown (don't kill the signal)
        materiality_factor = materiality_score if materiality_score > 0 else 0.5

        if signal_score > 0:
            # Direct adverse signal — transmission is less relevant when we
            # already have a direct hit; boost with credibility and recency
            active_watch_priority = (
                signal_score
                * credibility_score
                * materiality_factor
                * recency_decay
            )
        elif transmission_signal_score > 0:
            # No direct adverse event, but exposed via transmission
            active_watch_priority = (
                transmission_signal_score
                * materiality_factor
                * recency_decay
            )
        else:
            active_watch_priority = 0.0

        # ── v3: Coverage Monitor Priority (data quality only) ─────────────────
        # Graph coverage: how much graph data exists
        graph_coverage_score = min(
            legal_ownership_exposure / 100.0 + economic_dep_exposure / 50.0,
            1.0
        )

        # Information gap: 1.0 if materiality is unknown (needs enrichment)
        information_gap_score = 1.0 if not materiality_known else 0.0

        coverage_monitor_priority = (
            0.4 * graph_coverage_score
            + 0.3 * information_gap_score
            + 0.3 * source_staleness_score
        )

        # For backward compatibility: analyst_priority = active_watch_priority
        analyst_priority = active_watch_priority

        # P0 (engineering audit): Compute priority_reason
        priority_reason = _compute_priority_reason(
            signal_score=signal_score,
            event_count=event_count,
            legal_exposure=legal_ownership_exposure,
            econ_exposure=economic_dep_exposure,
            materiality_known=materiality_known,
            is_capped=is_capped,
            db=db,
            registry_id=registry_id,
        )

        # P0 (engineering audit): Compute coverage_quality
        coverage_quality = _compute_coverage_quality(
            db=db,
            registry_id=registry_id,
            has_events=event_count > 0,
            has_legal_edges=legal_ownership_exposure > 0,
            has_econ_edges=economic_dep_exposure > 0,
            is_capped=is_capped,
        )

        # Build explanation
        explanation = _build_explanation(
            company_name=row["canonical_name"],
            event_count=event_count,
            event_types=event_list,
            signal_score=signal_score,
            recency_score=recency_score,
            credibility_score=credibility_score,
            entity_relevance=entity_relevance,
            legal_ownership_exposure=legal_ownership_exposure,
            exposure_normalized=exposure_normalized,
            economic_dep_exposure=economic_dep_exposure,
            econ_exposure_normalized=econ_exposure_normalized,
            materiality_score=materiality_score,
            analyst_priority=analyst_priority,
            active_watch_priority=active_watch_priority,
            coverage_monitor_priority=coverage_monitor_priority,
            transmission_signal_score=transmission_signal_score,
            graph_explanation=_get_graph_explanation(db, registry_id),
            graph_capped=is_capped,
            priority_reason=priority_reason,
            coverage_quality=coverage_quality,
        )

        # Check if score changed from last snapshot (dedup)
        last_score = db.execute(
            """SELECT * FROM scores WHERE registry_id = ?
               ORDER BY computed_at DESC, id DESC LIMIT 1""",
            (registry_id,),
        ).fetchone()

        score_changed = True
        if last_score:
            old_priority = last_score["analyst_priority"] or 0
            score_changed = abs(analyst_priority - old_priority) > 0.001
            # P0 (engineering audit): Force rewrite if reason code is missing from old scores
            if not score_changed and not last_score["priority_reason"]:
                score_changed = True
            # Immutable snapshots must capture changes in canonical inputs or
            # scoring implementation even when the rounded priority is stable.
            if not score_changed and last_score["input_fingerprint"] != input_fingerprint:
                score_changed = True
            if not score_changed and last_score["scorer_version"] != SCORER_VERSION:
                score_changed = True

        if score_changed and persist:
            db.execute(
                """INSERT INTO scores
                   (registry_id, score_schema_version, observation_severity,
                    deterioration_risk, dependency_exposure, analyst_priority,
                    priority_reason, coverage_quality,
                    active_watch_priority, coverage_monitor_priority,
                    transmission_signal_score, graph_coverage_score,
                    information_gap_score, source_staleness_score,
                    factors_json, explanation, computed_at, input_fingerprint,
                    provenance_json, run_id, scorer_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    registry_id,
                    int(SCORER_VERSION.split(".")[0]),
                    round(signal_score, 3),
                    round(signal_score * recency_score, 3),  # deterioration_risk
                    round(legal_ownership_exposure, 3),
                    round(analyst_priority, 4),
                    priority_reason,
                    coverage_quality,
                    round(active_watch_priority, 4),
                    round(coverage_monitor_priority, 4),
                    round(transmission_signal_score, 4),
                    round(graph_coverage_score, 3),
                    round(information_gap_score, 3),
                    round(source_staleness_score, 3),
                    json.dumps({
                        "scorer_version": SCORER_VERSION,
                        "run_id": run_id,
                        "signal_score": round(signal_score, 3),
                        "recency_score": round(recency_score, 3),
                        "credibility_score": round(credibility_score, 3),
                        "entity_relevance": round(entity_relevance, 3),
                        "legal_ownership_exposure": round(legal_ownership_exposure, 1),
                        "exposure_normalized": round(exposure_normalized, 3),
                        "economic_dep_exposure": round(economic_dep_exposure, 1),
                        "econ_exposure_normalized": round(econ_exposure_normalized, 3),
                        "materiality_score": round(materiality_score, 3),
                        "signal_factors": signal_factors,
                        "materiality_known": materiality_known,
                        "priority_reason": priority_reason,
                        "coverage_quality": coverage_quality,
                        "active_watch_priority": round(active_watch_priority, 4),
                        "coverage_monitor_priority": round(coverage_monitor_priority, 4),
                        "transmission_signal_score": round(transmission_signal_score, 4),
                        "graph_coverage_score": round(graph_coverage_score, 3),
                        "information_gap_score": round(information_gap_score, 3),
                        "source_staleness_score": round(source_staleness_score, 3),
                    }),
                    explanation,
                    now,
                    input_fingerprint,
                    None,
                    run_id,
                    SCORER_VERSION,
                ),
            )
            logger.debug(
                f"New score for {row['canonical_name']}: "
                f"watch={active_watch_priority:.4f} coverage={coverage_monitor_priority:.4f}"
            )

        results.append({
            "registry_id": registry_id,
            "canonical_name": row["canonical_name"],
            "event_count": event_count,
            "signal_score": round(signal_score, 3),
            "recency_score": round(recency_score, 3),
            "credibility_score": round(credibility_score, 3),
            "entity_relevance": round(entity_relevance, 3),
            "legal_ownership_exposure": round(legal_ownership_exposure, 1),
            "exposure_normalized": round(exposure_normalized, 3),
            "economic_dep_exposure": round(economic_dep_exposure, 1),
            "econ_exposure_normalized": round(econ_exposure_normalized, 3),
            "materiality_score": round(materiality_score, 3),
            "analyst_priority": round(analyst_priority, 4),
            "active_watch_priority": round(active_watch_priority, 4),
            "coverage_monitor_priority": round(coverage_monitor_priority, 4),
            "transmission_signal_score": round(transmission_signal_score, 4),
            "graph_coverage_score": round(graph_coverage_score, 3),
            "information_gap_score": round(information_gap_score, 3),
            "source_staleness_score": round(source_staleness_score, 3),
            "priority_reason": priority_reason,
            "coverage_quality": coverage_quality,
            "explanation": explanation,
            "materiality_known": materiality_known,
        })

    if persist:
        db.commit()
        journal_event(db, "scoring_run", {
            "run_id": run_id,
            "companies_scored": len(results),
            "new_snapshots": sum(1 for r in results if r["analyst_priority"] > 0),
            "scorer_version": SCORER_VERSION,
        })

    # Sort: active_watch_priority > 0 first (descending), then coverage_monitor_priority descending
    results.sort(
        key=lambda r: (r["active_watch_priority"], r["coverage_monitor_priority"]),
        reverse=True,
    )
    logger.info(f"Computed scores for {len(results)} companies (v{SCORER_VERSION})")
    return results


def _compute_signal_score(
    events: list[dict[str, Any]], weights: dict[str, Any]
) -> tuple[float, dict[str, float]]:
    """Compute signal score from structured active-event rows.

    Each event's type, severity, and confidence are read from the same row;
    this deliberately prevents aggregate-order tuple corruption.
    """
    if not events:
        return 0.0, {}

    severity_mult = {
        "critical": 10.0,
        "high": 6.0,
        "medium": 3.0,
        "low": 1.0,
    }

    score = 0.0
    factors = {}

    for event in events:
        event_type = event.get("event_type")
        if not event_type:
            continue
        weight = weights.get(event_type, 1.0)
        sev = event.get("severity") or "medium"
        confidence = event.get("confidence")
        conf = float(confidence) if confidence is not None else 1.0
        sev_mult = severity_mult.get(sev, 1.0)

        event_score = weight * sev_mult * conf
        score += event_score
        factors[event_type] = round(event_score, 2)

    # Normalize with divisor — higher divisor = lower signal strength.
    # v3: reduced from 30 to 10 for multiplicative scoring, where signals
    # are further attenuated by recency, materiality, and credibility.
    score = min(score / 10.0, 1.0)
    return round(score, 3), factors


def _compute_recency(latest_event_at: str | None) -> float:
    """Score how recent the latest event is. 1.0 = today, decays over 30 days."""
    if not latest_event_at:
        return 0.0

    try:
        event_date = datetime.fromisoformat(latest_event_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        days_ago = (now - event_date).total_seconds() / 86400
        return round(max(0.0, 1.0 - (days_ago / 30.0)), 3)
    except (ValueError, TypeError):
        return 0.0


def _compute_credibility(db: sqlite3.Connection, registry_id: int) -> float:
    """Compute credibility from source reliability and event confidence."""
    events = db.execute(
        """SELECT e.confidence, ev.source_reliability
           FROM events e
           JOIN evidence ev ON e.evidence_id = ev.id
           WHERE e.registry_id = ? AND e.active = 1 AND e.status = 'active'
           AND e.event_type NOT IN ('unconfirmed_review_candidate', 'unconfirmed_adverse')""",
        (registry_id,),
    ).fetchall()

    if not events:
        return 0.0

    cred_sum = 0.0
    for e in events:
        conf = e["confidence"] or 0.5
        rel = e["source_reliability"] or 0.5
        cred_sum += conf * rel

    return round(min(cred_sum / len(events), 1.0), 3)


def _compute_entity_relevance(row: Any) -> float:
    """Score entity relevance to portfolio. Portfolio companies = 1.0."""
    # All registry entries with domains ARE portfolio companies
    # In future: score how well entity resolves to portfolio identity
    return 1.0


def _compute_legal_ownership_exposure(db: sqlite3.Connection, registry_id: int) -> float:
    """Compute GLEIF legal ownership graph exposure for a registry entity.

    ⚠ NOT economic dependency intelligence — this is legal parent/subsidiary
    structure mapping only. All materiality/criticality fields are NULL.
    Hand-chosen type weights with zero empirical calibration.
    """
    edges = db.execute(
        """SELECT relationship_type, confidence, economic_materiality,
                  operational_criticality
           FROM edges
           WHERE source_registry_id = ?
           AND (edge_source = 'gleif' OR edge_source IS NULL)""",
        (registry_id,),
    ).fetchall()

    if not edges:
        return 0.0

    type_weight = {
        "parent_subsidiary": 5.0,
        "ultimate_parent": 3.0,
        "branch_of": 2.0,
    }

    exposure = 0.0
    unknown_mat_count = 0
    for edge in edges:
        tw = type_weight.get(edge["relationship_type"], 1.0)
        # P1-2: NULL materiality → 0.0, NOT 0.5. Unknown is unknown.
        mat = edge["economic_materiality"] if edge["economic_materiality"] is not None else 0.0
        if edge["economic_materiality"] is None:
            unknown_mat_count += 1
        conf = edge["confidence"] if edge["confidence"] is not None else 0.5
        # P1-2: NULL criticality → 0.0, NOT 0.3. No amplification without evidence.
        crit = edge["operational_criticality"] if edge["operational_criticality"] is not None else 0.0
        exposure += tw * mat * conf * (1.0 + crit)

    # Add incoming edges
    incoming = db.execute(
        "SELECT COUNT(*) as c FROM edges WHERE target_registry_id = ?",
        (registry_id,),
    ).fetchone()
    if incoming and incoming["c"]:
        exposure += incoming["c"] * 2.0

    return round(exposure, 1)


def _is_graph_capped(db: sqlite3.Connection, registry_id: int) -> bool:
    """Check if graph collection was truncated by max_children cap (P1-4)."""
    row = db.execute(
        """SELECT cap_hit FROM graph_collection_state
           WHERE registry_id = ? AND source = 'gleif'""",
        (registry_id,),
    ).fetchone()
    return bool(row and row["cap_hit"])


def _compute_economic_dependency_exposure(
    db: sqlite3.Connection, registry_id: int
) -> float:
    """Compute economic dependency exposure from edges with edge_source != 'gleif'.

    After the economic_entities → edges promotion bridge, all economic
    dependencies are first-class edges. Falls back to counting
    economic_entities only if no edges exist (pre-promotion state).

    P1 (engineering audit 2026-06-20): Now incorporates shock channel severity,
    transmission lag, and buffer proxies — not just connectedness.

    P1 (engineering audit 2026-06-20): Uses edge quality_score directly instead of
    hardcoded SOURCE_MULTIPLIERS. Quality tiers are maintained in the
    edges table via migration v9.
    """
    exposure = 0.0

    # 1. Count edges from non-GLEIF sources (annual_report, manual_pilot)
    edges = db.execute(
        """SELECT e.relationship_type, e.confidence, e.concentration,
                  e.replaceability, e.relationship_role, e.direction,
                  e.shock_channel, e.lag_bucket, e.buffer_proxy,
                  e.edge_source, e.quality_score
           FROM edges e
           WHERE e.source_registry_id = ?
           AND e.edge_source IS NOT NULL
           AND e.edge_source != 'gleif'""",
        (registry_id,),
    ).fetchall()

    if edges:
        # Primary path: use edges directly
        # Quality score replaces hardcoded source multipliers.
        # Falls back to 0.4 if quality_score is NULL (pre-migration edges).

        for edge in edges:
            type_score = {
                "supplier_to": 8.0,
                "customer_of": 5.0,
                "facility_at": 4.0,
                "commodity_input": 6.0,
                "operational_dependency": 3.0,
                "regulatory_dependency": 3.0,
                "jurisdiction_exposure": 3.0,
                "distribution_channel": 2.0,
            }.get(edge["relationship_type"], 2.0)

            conc = edge["concentration"] if edge["concentration"] is not None else 0.5
            rep = edge["replaceability"] if edge["replaceability"] is not None else 0.5
            criticality_factor = 1.0 + (1.0 - rep)
            dir_factor = 1.2 if edge["direction"] == "outgoing" else 1.0
            conf = edge["confidence"] if edge["confidence"] is not None else 0.7

            # Shock channel, lag, and buffer multipliers (P1 engineering audit)
            channel = edge["shock_channel"] or "unknown"
            lag = edge["lag_bucket"] or "unknown"
            buffer = edge["buffer_proxy"] or "unknown"
            shock_mult = SHOCK_CHANNEL_MULT.get(channel, 0.8)
            lag_mult = LAG_MULT.get(lag, 0.9)
            buffer_mult = BUFFER_MULT.get(buffer, 1.0)

            # Use edge quality_score directly (from migration v9 backfill)
            # Falls back to 0.4 for pre-migration NULL values
            quality_mult = edge["quality_score"] if edge["quality_score"] is not None else 0.4

            exposure += (
                type_score * conc * conf * criticality_factor
                * dir_factor * quality_mult
                * shock_mult * lag_mult * buffer_mult
            )

        return round(exposure, 1)

    # 2. Fallback: count economic_entities if no edges exist (pre-promotion)
    eco_entities = db.execute(
        """SELECT ee.entity_type, COUNT(*) as cnt
           FROM economic_entities ee
           WHERE ee.registry_id = ?
           GROUP BY ee.entity_type""",
        (registry_id,),
    ).fetchall()

    type_score_map = {
        "customer": 5.0,
        "supplier": 8.0,
        "facility": 4.0,
        "commodity": 6.0,
        "operational": 3.0,
    }

    for eco in eco_entities:
        entity_type = eco[0]
        count = eco[1]
        score = type_score_map.get(entity_type, 2.0)
        exposure += score * count * 0.4  # annual_report source multiplier

    return round(exposure, 1)


def _compute_transmission_signal(db: sqlite3.Connection, registry_id: int) -> float:
    """Compute transmission signal: risk from connected entities with adverse events.

    Quality-tier gating (engineering audit 2026-06-20):
    Only considers edges with quality_tier in T1_OFFICIAL, T2_REGISTRY,
    T3_TRUSTED_THIRD_PARTY. Skips T4_INFERRED and T5_PLACEHOLDER as noise.
    Only checks direct (1-hop) edges by default.

    For each qualifying target entity, checks if the target has adverse events
    (confidence >= 0.5). If yes, computes:
        transmission = edge_confidence * shock_channel_mult * lag_mult
                       * buffer_mult * quality_score

    Returns the max transmission across all connected entities, normalized to [0, 1].
    """
    # Get all outbound edges with quality_tier >= T3 and transmission metadata
    # Only 1-hop (direct) edges — no transitive transmission for now
    edges = db.execute(
        """SELECT e.target_registry_id, e.confidence,
                  e.shock_channel, e.lag_bucket, e.buffer_proxy,
                  e.economic_materiality, e.quality_tier, e.quality_score
           FROM edges e
           WHERE e.source_registry_id = ?
           AND e.quality_tier IN ('T1_OFFICIAL', 'T2_REGISTRY', 'T3_TRUSTED_THIRD_PARTY')""",
        (registry_id,),
    ).fetchall()

    if not edges:
        return 0.0

    max_transmission = 0.0
    suppress_reasons = []

    for edge in edges:
        target_id = edge["target_registry_id"]
        if target_id is None:
            suppress_reasons.append("null_target")
            continue

        # Check quality tier — skip T4/T5 noise
        tier = edge["quality_tier"] or "T4_INFERRED"
        if tier in ("T4_INFERRED", "T5_PLACEHOLDER"):
            suppress_reasons.append(f"tier_{tier}")
            continue

        # Check if target has adverse events (confidence >= 0.5)
        adverse_count = db.execute(
            """SELECT COUNT(*) as c FROM events
               WHERE registry_id = ? AND confidence >= 0.5
               AND active = 1 AND status = 'active'
               AND event_type NOT IN ('unconfirmed_review_candidate', 'unconfirmed_adverse')""",
            (target_id,),
        ).fetchone()

        if not adverse_count or adverse_count["c"] == 0:
            suppress_reasons.append("no_adverse_events")
            continue

        # Target has adverse events — compute transmission risk
        edge_conf = edge["confidence"] if edge["confidence"] is not None else 0.5

        channel = edge["shock_channel"] or "unknown"
        lag = edge["lag_bucket"] or "unknown"
        buffer = edge["buffer_proxy"] or "unknown"

        shock_mult = SHOCK_CHANNEL_MULT.get(channel, 0.8)
        lag_mult = LAG_MULT.get(lag, 0.9)
        buffer_mult = BUFFER_MULT.get(buffer, 1.0)

        # Materiality if known from edge, otherwise use a default
        mat = edge["economic_materiality"]
        if mat is not None and mat > 0:
            mat_factor = min(mat, 1.0)
        else:
            mat_factor = 0.3

        # Quality score from edge tier
        quality_mult = edge["quality_score"] if edge["quality_score"] is not None else 0.35

        transmission = (
            edge_conf * shock_mult * lag_mult * buffer_mult
            * mat_factor * quality_mult
        )
        max_transmission = max(max_transmission, transmission)

    # Normalize: cap at 1.0 (theoretical max is ~ 1.0 * 1.5 * 1.3 * 1.3 * 1.0 * 0.85 ≈ 2.2)
    # Apply a softer cap — divide by 2.0 as reasonable normalization
    return round(min(max_transmission / 2.0, 1.0), 4)


def _compute_materiality(db: sqlite3.Connection, registry_id: int) -> float:
    """Check if materiality is known (data-derived) or unknown (proxy/default).

    Uses materiality_unknown_reason as the authoritative signal:
    if ALL edges have an unknown_reason, return 0.0 (nothing is data-derived).
    Otherwise, compute a robust median of known materiality values.
    """
    edges = db.execute(
        """SELECT economic_materiality, materiality_unknown_reason FROM edges
           WHERE source_registry_id = ?
           AND economic_materiality IS NOT NULL""",
        (registry_id,),
    ).fetchall()

    if not edges:
        return 0.0

    # If every edge has an unknown_reason, nothing is data-derived — return 0.0
    if all(e[1] is not None for e in edges):
        return 0.0

    # Filter to edges WITHOUT an unknown_reason (truly data-derived)
    known_values = sorted(
        e[0]
        for e in edges
        if e[1] is None
        and e[0] is not None
        and e[0] > 0
    )

    if not known_values:
        return 0.0

    # Use median for robustness against outliers
    n = len(known_values)
    if n % 2 == 0:
        median = (known_values[n // 2 - 1] + known_values[n // 2]) / 2
    else:
        median = known_values[n // 2]

    # Log-normalize large raw values (employee counts, revenue)
    if median > 1.0:
        median = min(math.log10(median + 1) / 6.0, 1.0)

    return round(median, 2)


def _is_materiality_known(db: sqlite3.Connection, registry_id: int) -> bool:
    """Check if materiality is derived from data (not proxies/unknowns).

    Uses materiality_unknown_reason: if ALL edges have an unknown_reason,
    materiality is NOT known. At least one edge must have NULL unknown_reason
    and a non-null non-zero materiality value.
    """
    edges = db.execute(
        """SELECT economic_materiality, materiality_unknown_reason FROM edges
           WHERE source_registry_id = ?""",
        (registry_id,),
    ).fetchall()
    if not edges:
        return False

    for e in edges:
        mat = e[0]
        reason = e[1]
        if mat is not None and mat > 0 and reason is None:
            return True  # At least one data-derived value

    return False  # All proxy, NULL, zero, or unknown


def _get_graph_explanation(
    db: sqlite3.Connection, registry_id: int
) -> str:
    """Get a human-readable legal ownership graph explanation."""
    edges = db.execute(
        """SELECT e.relationship_type, e.confidence,
                  e.economic_materiality, e.operational_criticality,
                  tr.canonical_name as target_name
           FROM edges e
           LEFT JOIN registry tr ON e.target_registry_id = tr.id
           WHERE e.source_registry_id = ?
           ORDER BY e.economic_materiality DESC
           LIMIT 5""",
        (registry_id,),
    ).fetchall()

    if not edges:
        return "no legal ownership edges"

    parts = []
    for edge in edges:
        target = edge["target_name"] or "unknown entity"
        parts.append(
            f"{edge['relationship_type']} → {target[:40]}"
        )

    return "; ".join(parts[:3])


def _compute_priority_reason(
    signal_score: float,
    event_count: int,
    legal_exposure: float,
    econ_exposure: float,
    materiality_known: bool,
    is_capped: bool,
    db: sqlite3.Connection,
    registry_id: int,
) -> str:
    """Compute the primary reason a company is at its current priority.

    P0 (engineering audit 2026-06-20): Without this, analyst_priority is just an
    edge-count counter. This tells an analyst WHY they should (or shouldn't)
    look at this company.

    Priority reasons (in descending order of actionability):
    - adverse_signal: genuine adverse events detected
    - transmission_concern: exposed to adverse events through dependencies
    - dependency_exposure: economic dependencies exist, no adverse signals
    - graph_density: only legal ownership edges, no economic data
    - coverage_gap: incomplete data, blocked sources, cap hit
    - no_signal: nothing detected, low priority
    """
    # 1. Real adverse signal — highest priority reason
    if signal_score > 0 and event_count > 0:
        # Check if there are genuine (non-noise) events
        genuine = db.execute(
            """SELECT COUNT(*) as c FROM events e
               JOIN evidence ev ON e.evidence_id = ev.id
               WHERE e.registry_id = ? AND e.active = 1 AND e.status = 'active'
               AND e.confidence >= 0.5
               AND e.event_type NOT IN ('unconfirmed_review_candidate', 'unconfirmed_adverse')""",
            (registry_id,),
        ).fetchone()
        if genuine and genuine["c"] > 0:
            return "adverse_signal"

    # 2. Transmission concern — exposed via dependencies to an adverse entity
    # Quality-gated: only count connected entities through T1-T3 edges
    # (skips T4_INFERRED, T5_PLACEHOLDER noise from distant GLEIF subsidiaries)
    connected_events = db.execute(
        """SELECT COUNT(*) as c FROM events e
           WHERE e.registry_id IN (
               SELECT target_registry_id FROM edges
               WHERE source_registry_id = ?
               AND quality_tier IN ('T1_OFFICIAL', 'T2_REGISTRY', 'T3_TRUSTED_THIRD_PARTY')
               UNION
               SELECT source_registry_id FROM edges
               WHERE target_registry_id = ?
               AND quality_tier IN ('T1_OFFICIAL', 'T2_REGISTRY', 'T3_TRUSTED_THIRD_PARTY')
           )
           AND e.active = 1 AND e.status = 'active'
           AND e.confidence >= 0.5
           AND e.event_type NOT IN ('unconfirmed_review_candidate', 'unconfirmed_adverse')""",
        (registry_id, registry_id),
    ).fetchone()
    if connected_events and connected_events["c"] > 0:
        return "transmission_concern"

    # 3. Economic dependencies exist but no adverse signals
    if econ_exposure > 0:
        if materiality_known:
            return "dependency_exposure"
        else:
            return "dependency_exposure"

    # 4. Only legal ownership edges — graph density without economic meaning
    if legal_exposure > 0:
        if is_capped:
            return "coverage_gap"
        return "graph_density"

    # 5. Capped collection — incomplete picture
    if is_capped:
        return "coverage_gap"

    # 6. Nothing detected
    return "no_signal"


def _compute_coverage_quality(
    db: sqlite3.Connection,
    registry_id: int,
    has_events: bool,
    has_legal_edges: bool,
    has_econ_edges: bool,
    is_capped: bool,
) -> str:
    """Assess how well a company is covered by the monitoring system.

    P0 (engineering audit 2026-06-20): Coverage quality tells analysts whether a
    low priority is because the company is healthy, or because we simply
    don't have enough data to know.

    Returns: 'good', 'partial', 'poor', 'unknown'
    """
    # Count distinct source families by extraction_method + edge sources
    source_methods = set()

    # From events: count distinct extraction methods
    event_sources = db.execute(
        """SELECT DISTINCT ev.extraction_method FROM evidence ev
           JOIN events e ON e.evidence_id = ev.id
           WHERE e.registry_id = ? AND e.active = 1 AND e.status = 'active'""",
        (registry_id,),
    ).fetchall()
    for row in event_sources:
        if row[0]:
            source_methods.add(row[0])

    # From edges: count distinct edge sources
    edge_sources = db.execute(
        """SELECT DISTINCT edge_source FROM edges
           WHERE (source_registry_id = ? OR target_registry_id = ?)
           AND edge_source IS NOT NULL""",
        (registry_id, registry_id),
    ).fetchall()
    for row in edge_sources:
        if row[0]:
            source_methods.add(row[0])

    evidence_source_count = len(source_methods)

    if is_capped:
        return "poor"

    # Good: multiple evidence sources + both legal and economic edges
    if evidence_source_count >= 2 and has_legal_edges and has_econ_edges:
        return "good"

    # Good: has events from multiple sources
    if has_events and evidence_source_count >= 2:
        return "good"

    # Partial: has some data but not comprehensive
    if has_legal_edges or has_econ_edges or has_events:
        return "partial"

    # Poor: very limited data
    if evidence_source_count <= 1 and (has_legal_edges or has_events):
        return "partial"

    return "unknown"


def _compute_source_staleness(
    registry_id: int, source_freshness: dict[str, Any]
) -> float:
    """Compute entity staleness from precomputed global freshness.

    Global freshness is a once-per-run source adapter watermark. Entity
    coverage identifies which of those adapters have evidence or a relevant
    edge for this entity. An uncovered entity remains a coverage gap rather
    than being assigned a fictitious per-entity source-run timestamp.
    """
    global_freshness = source_freshness.get("global", {})
    covered_sources = source_freshness.get("entity_coverage", {}).get(str(registry_id), [])
    timestamps = [global_freshness[source] for source in covered_sources if source in global_freshness]
    if not timestamps:
        return 0.0

    now = datetime.now(timezone.utc)
    staleness_values = []
    for completed_at in timestamps:
        try:
            ts = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            staleness_values.append(min((now - ts).total_seconds() / 86400 / 30.0, 1.0))
        except (ValueError, TypeError):
            continue
    return round(sum(staleness_values) / len(staleness_values), 3) if staleness_values else 0.0


def _build_explanation(
    company_name: str,
    event_count: int,
    event_types: list[str],
    signal_score: float,
    recency_score: float,
    credibility_score: float,
    entity_relevance: float,
    legal_ownership_exposure: float,
    exposure_normalized: float,
    economic_dep_exposure: float = 0.0,
    econ_exposure_normalized: float = 0.0,
    materiality_score: float = 0.0,
    analyst_priority: float = 0.0,
    active_watch_priority: float = 0.0,
    coverage_monitor_priority: float = 0.0,
    transmission_signal_score: float = 0.0,
    graph_explanation: str = "",
    graph_capped: bool = False,
    priority_reason: str = "no_signal",
    coverage_quality: str = "unknown",
) -> str:
    """Build a human-readable explanation for the score (v3).

    Separates credit watch items from coverage/enrichment items.
    """
    # Determine the primary category
    is_watch_item = active_watch_priority > 0

    if event_count == 0 and legal_ownership_exposure == 0 and economic_dep_exposure == 0:
        base = (
            f"{company_name}: no events or graph data. "
            f"[COVERAGE] coverage_priority={coverage_monitor_priority:.3f}, "
            f"watch_priority={active_watch_priority:.3f}"
        )
        if graph_capped:
            base += " [graph incomplete — cap hit]"
        return base

    parts = []

    # Category label
    if is_watch_item:
        parts.append("[CREDIT WATCH]")
    else:
        parts.append("[COVERAGE/ENRICHMENT]")

    if event_count > 0:
        parts.append(f"detected {event_count} event(s): {', '.join(event_types[:3])}")
        parts.append(f"signal={signal_score:.2f}, recency={recency_score:.2f}")

    if legal_ownership_exposure > 0:
        mat_note = " (no materiality data)" if materiality_score == 0 else f" (mat={materiality_score:.2f})"
        parts.append(f"legal ownership edges={legal_ownership_exposure:.0f}{mat_note}: {graph_explanation}")

    if economic_dep_exposure > 0:
        parts.append(f"economic dependencies={economic_dep_exposure:.0f}")

    if transmission_signal_score > 0:
        parts.append(f"transmission_risk={transmission_signal_score:.3f}")

    # Priority level for watch items
    if is_watch_item:
        priority_label = (
            "HIGH" if active_watch_priority > 0.3
            else "MEDIUM" if active_watch_priority > 0.1
            else "LOW"
        )
    else:
        priority_label = "MONITOR"

    reason_labels = {
        "adverse_signal": "⚠ adverse event detected",
        "transmission_concern": "↗ exposed via dependency",
        "dependency_exposure": "🔗 economic dependencies (no adverse signal)",
        "graph_density": "📊 legal ownership edges only",
        "coverage_gap": "⚠ incomplete coverage",
        "no_signal": "○ no signal",
    }
    reason_text = reason_labels.get(priority_reason, priority_reason)

    base = f"{company_name}: "
    base += " | ".join(parts)
    base += (
        f" | watch={active_watch_priority:.3f} [{priority_label}]"
        f" | coverage={coverage_monitor_priority:.3f}"
        f" | reason={reason_text}"
    )

    if materiality_score == 0 and legal_ownership_exposure > 0:
        base += " [materiality unknown — no economic data]"
    if graph_capped:
        base += " [graph incomplete — cap hit]"
    if coverage_quality == "poor":
        base += " [poor coverage]"
    elif coverage_quality == "unknown":
        base += " [coverage unknown]"

    return base


# Public API for external callers
def compute_legal_ownership_exposure_for_company(
    db: sqlite3.Connection, registry_id: int
) -> dict[str, Any]:
    """Detailed GLEIF legal ownership exposure breakdown for a single company.

    ⚠ This is legal parent/subsidiary structure, NOT economic dependency intelligence.
    """
    edges = db.execute(
        """SELECT e.*, tr.canonical_name as target_name, tr.lei as target_lei
           FROM edges e
           LEFT JOIN registry tr ON e.target_registry_id = tr.id
           WHERE e.source_registry_id = ?
           ORDER BY e.economic_materiality DESC""",
        (registry_id,),
    ).fetchall()

    if not edges:
        return {"edges": [], "exposure": 0.0, "explanation": "no legal ownership edges"}

    exposure = _compute_legal_ownership_exposure(db, registry_id)
    materiality_known = _is_materiality_known(db, registry_id)

    edge_list = []
    for edge in edges:
        edge_list.append({
            "target": edge["target_name"] or f"LEI:{edge.get('target_lei', '?')}",
            "type": edge["relationship_type"],
            "confidence": edge["confidence"],
            "materiality": edge["economic_materiality"],
            "criticality": edge["operational_criticality"],
        })

    return {
        "edges": edge_list,
        "exposure": round(exposure, 2),
        "edge_count": len(edge_list),
        "materiality_known": materiality_known,
        "explanation": f"{len(edge_list)} legal ownership edges (parent/subsidiary only), exposure={exposure:.1f}"
                      + (" [materiality unknown — no economic data]" if not materiality_known else ""),
    }
