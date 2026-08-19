"""Shock channel taxonomy and edge attribute population.

P1 (engineering audit 2026-06-20): Original vision requires explicit shock channels,
transmission lags, buffers, and replaceability for every economic edge.
This module provides the taxonomy and deterministic mapping rules.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── Taxonomies ────────────────────────────────────────────────────────────────

SHOCK_CHANNELS: dict[str, str] = {
    "demand_loss": "Loss of customer demand from a dependent counterparty",
    "supplier_disruption": "Disruption of critical input from a supplier",
    "input_cost_inflation": "Rising input costs from a commodity or supplier",
    "credit_exposure": "Counterparty credit or receivables risk",
    "refinancing_liquidity": "Funding or refinancing exposure through parent/subsidiary",
    "legal_regulatory": "Legal, regulatory, or compliance transmission",
    "operational_shutdown": "Facility or operational capacity loss",
    "logistics_disruption": "Supply chain logistics or transportation disruption",
    "commodity_price": "Commodity price or availability shock",
    "reputational": "Reputational or brand contamination",
    "ownership_control": "Parent control, capital allocation, or guarantee risk",
    "customer_concentration": "Revenue concentration in a single customer",
    "geographic_conflict": "Geographic or jurisdictional risk transmission",
    "environmental_compliance": "Environmental liability or compliance transmission",
    "unknown": "Shock channel cannot be determined from available evidence",
}

LAG_BUCKETS = ["immediate", "days", "weeks", "months", "annual_cycle", "unknown"]

BUFFER_PROXIES = [
    "inventory_buffer",
    "multi_supplier",
    "single_source",
    "contractual",
    "regulated_service",
    "financial_reserve",
    "unknown",
]

REPLACEABILITY_LEVELS = ["high", "medium", "low", "unknown"]

SWITCHING_TIME_BUCKETS = ["days", "weeks", "months", "years", "unknown"]


# ── Deterministic mapping: relationship_type → shock channels ──────────────────

RELATIONSHIP_SHOCK_MAP: dict[str, list[str]] = {
    # Legal ownership edges (GLEIF)
    "parent_subsidiary": [
        "ownership_control",
        "refinancing_liquidity",
        "credit_exposure",
    ],
    "ultimate_parent": [
        "ownership_control",
        "refinancing_liquidity",
        "reputational",
    ],
    "branch_of": ["ownership_control", "operational_shutdown"],

    # Economic dependency edges (from annual reports, TED, E-PRTR, etc.)
    "supplier_to": [
        "supplier_disruption",
        "input_cost_inflation",
        "logistics_disruption",
    ],
    "customer_of": [
        "demand_loss",
        "customer_concentration",
        "credit_exposure",
    ],
    "facility_at": [
        "operational_shutdown",
        "geographic_conflict",
        "environmental_compliance",
    ],
    "commodity_input": [
        "commodity_price",
        "input_cost_inflation",
        "supplier_disruption",
    ],
    "operational_dependency": [
        "operational_shutdown",
        "logistics_disruption",
        "legal_regulatory",
    ],
    "regulatory_dependency": [
        "legal_regulatory",
        "operational_shutdown",
    ],
    "jurisdiction_exposure": [
        "geographic_conflict",
        "legal_regulatory",
    ],
    "distribution_channel": [
        "logistics_disruption",
        "demand_loss",
    ],
}


# ── Default lag/buffer/replaceability by relationship type ─────────────────────

DEFAULT_ATTRIBUTES: dict[str, dict[str, str]] = {
    "parent_subsidiary": {
        "lag_bucket": "months",
        "buffer_proxy": "financial_reserve",
        "replaceability": "low",
        "switching_time_bucket": "years",
    },
    "ultimate_parent": {
        "lag_bucket": "months",
        "buffer_proxy": "financial_reserve",
        "replaceability": "low",
        "switching_time_bucket": "years",
    },
    "branch_of": {
        "lag_bucket": "weeks",
        "buffer_proxy": "regulated_service",
        "replaceability": "low",
        "switching_time_bucket": "months",
    },
    "supplier_to": {
        "lag_bucket": "weeks",
        "buffer_proxy": "unknown",
        "replaceability": "unknown",
        "switching_time_bucket": "unknown",
    },
    "customer_of": {
        "lag_bucket": "weeks",
        "buffer_proxy": "unknown",
        "replaceability": "unknown",
        "switching_time_bucket": "unknown",
    },
    "facility_at": {
        "lag_bucket": "days",
        "buffer_proxy": "unknown",
        "replaceability": "low",
        "switching_time_bucket": "years",
    },
    "commodity_input": {
        "lag_bucket": "weeks",
        "buffer_proxy": "inventory_buffer",
        "replaceability": "unknown",
        "switching_time_bucket": "unknown",
    },
    "operational_dependency": {
        "lag_bucket": "unknown",
        "buffer_proxy": "unknown",
        "replaceability": "unknown",
        "switching_time_bucket": "unknown",
    },
    "regulatory_dependency": {
        "lag_bucket": "months",
        "buffer_proxy": "regulated_service",
        "replaceability": "low",
        "switching_time_bucket": "years",
    },
    "jurisdiction_exposure": {
        "lag_bucket": "months",
        "buffer_proxy": "unknown",
        "replaceability": "low",
        "switching_time_bucket": "years",
    },
    "distribution_channel": {
        "lag_bucket": "days",
        "buffer_proxy": "unknown",
        "replaceability": "unknown",
        "switching_time_bucket": "unknown",
    },
}


def get_shock_channels(relationship_type: str) -> list[str]:
    """Get the shock channels for a given relationship type.

    Returns list of channel names, or ['unknown'] if unrecognised.
    """
    return RELATIONSHIP_SHOCK_MAP.get(relationship_type, ["unknown"])


def get_default_attributes(relationship_type: str) -> dict[str, str]:
    """Get default lag/buffer/replaceability for a relationship type.

    Returns dict with lag_bucket, buffer_proxy, replaceability, switching_time_bucket.
    All values default to 'unknown' if the type is unrecognised.
    """
    return DEFAULT_ATTRIBUTES.get(
        relationship_type,
        {
            "lag_bucket": "unknown",
            "buffer_proxy": "unknown",
            "replaceability": "unknown",
            "switching_time_bucket": "unknown",
        },
    )


def populate_edge_attributes(
    relationship_type: str,
    concentration: float | None = None,
    replaceability_val: float | None = None,
) -> dict[str, Any]:
    """Populate shock channel, lag, buffer, and replaceability for an edge.

    Uses deterministic mapping from relationship_type with conservative defaults.
    Existing data-derived values (concentration, replaceability) are used to
    refine the categorical proxies where available.

    Returns dict suitable for edge INSERT/UPDATE with keys:
    shock_channel, shock_channel_unknown_reason, lag_bucket,
    buffer_proxy, replaceability, replaceability_unknown_reason,
    switching_time_bucket.
    """
    channels = get_shock_channels(relationship_type)
    attrs = get_default_attributes(relationship_type)

    result: dict[str, Any] = {
        "shock_channel": channels[0] if channels else "unknown",
        "shock_channel_unknown_reason": (
            None
            if relationship_type in RELATIONSHIP_SHOCK_MAP
            else f"no shock channel mapping for relationship type '{relationship_type}'"
        ),
        "lag_bucket": attrs.get("lag_bucket", "unknown"),
        "buffer_proxy": attrs.get("buffer_proxy", "unknown"),
        "replaceability": attrs.get("replaceability", "unknown"),
        "replaceability_unknown_reason": None,
        "switching_time_bucket": attrs.get("switching_time_bucket", "unknown"),
    }

    # Refine replaceability from data-derived value if available
    if replaceability_val is not None:
        if replaceability_val >= 0.7:
            result["replaceability"] = "high"
        elif replaceability_val >= 0.3:
            result["replaceability"] = "medium"
        else:
            result["replaceability"] = "low"
        result["replaceability_unknown_reason"] = None

    # If we have concentration data, infer buffer quality
    if concentration is not None:
        if concentration >= 0.7:
            result["buffer_proxy"] = "single_source"
        elif concentration >= 0.3:
            result["buffer_proxy"] = "multi_supplier"

    return result


def format_transmission_path(edge: dict[str, Any]) -> str:
    """Format a human-readable transmission path explanation for an edge.

    P1 (engineering audit): Every edge shown to analysts must explain the transmission
    mechanism, not just the relationship type.
    """
    channel = edge.get("shock_channel") or "unknown"
    lag = edge.get("lag_bucket") or "unknown"
    buffer = edge.get("buffer_proxy") or "unknown"
    rep = edge.get("replaceability") or "unknown"

    channel_desc = SHOCK_CHANNELS.get(channel, channel)

    parts = [f"channel={channel_desc}"]

    if lag != "unknown":
        parts.append(f"lag≈{lag}")
    if buffer != "unknown":
        parts.append(f"buffer={buffer}")
    if rep != "unknown":
        parts.append(f"replaceability={rep}")

    return " | ".join(parts)
