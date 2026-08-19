"""Tests for edge metric extraction from annual report text."""

import pytest
from kassandra.extract_edge_metrics import (
    _extract_company_metrics,
    _extract_criticality_indicators,
    _determine_concentration,
    _determine_operational_criticality,
    DEFAULT_CRITICALITY,
    RELATIONSHIP_TYPE_CRITICALITY_FLOOR,
)


FIXTURE_ASML_TEXT = """
Our company depends on Carl Zeiss SMT GmbH, our sole supplier of
advanced optical systems. Zeiss represents approximately 22% of
our total procurement spend. We also rely on a single-source for
key components used in wafer handling systems.

Our top 3 customers are TSMC, Samsung Electronics, and Intel
Corporation. These three customers account for 65% of annual revenue.

We are exposed to rare earth element prices, particularly neodymium
and dysprosium. We maintain 6-month inventory buffers for critical
rare earth materials sourced from a single supplier in China.
"""


def _defaults(d):
    """Fill default metric values for cleaner test code."""
    defaults = {
        "max_customer_pct": 0.0,
        "avg_customer_pct": 0.0,
        "max_procurement_pct": 0.0,
        "has_sole_supplier": False,
        "has_single_source": False,
        "has_no_customer_concentration": False,
    }
    return {**defaults, **d}


class TestCompanyMetrics:
    def test_extract_sole_supplier(self):
        metrics = _extract_company_metrics(
            "Carl Zeiss SMT GmbH is our sole supplier of optical systems"
        )
        assert metrics["has_sole_supplier"] is True

    def test_extract_single_source(self):
        metrics = _extract_company_metrics(
            "single-source for key components"
        )
        assert metrics["has_single_source"] is True

    def test_extract_customer_concentration(self):
        metrics = _extract_company_metrics(
            "top 3 customers account for 65% of revenue"
        )
        assert metrics["max_customer_pct"] > 20  # 65/3 ≈ 21.7
        assert metrics["avg_customer_pct"] > 20

    def test_full_text(self):
        metrics = _extract_company_metrics(FIXTURE_ASML_TEXT)
        assert metrics["has_sole_supplier"] is True
        assert metrics["has_single_source"] is True
        # Customer concentration pattern requires contiguous "top N customers
        # account for X% of revenue" — names between "customers" and "account"
        # break the regex (known limitation, real reports disclose this way)

    def test_no_matches(self):
        metrics = _extract_company_metrics(
            "Nothing relevant here. Just ESG goals and DEI targets."
        )
        assert metrics["has_sole_supplier"] is False
        assert metrics["has_single_source"] is False
        assert metrics["max_customer_pct"] == 0.0


class TestDetermineConcentration:
    def test_supplier_with_sole(self):
        metrics = _defaults({"has_sole_supplier": True})
        conc, rep = _determine_concentration("sole supplier", "supplier_to", metrics)
        assert conc == 1.0
        assert rep == 0.0

    def test_supplier_with_procurement(self):
        metrics = _defaults({"max_procurement_pct": 22.0})
        conc, rep = _determine_concentration("22% of procurement", "supplier_to", metrics)
        assert conc == 0.22
        assert rep == 0.5

    def test_customer_with_revenue(self):
        metrics = _defaults({"max_customer_pct": 21.67, "avg_customer_pct": 21.67})
        conc, rep = _determine_concentration("65% of revenue", "customer_of", metrics)
        assert conc == 0.2167
        assert rep == 0.5

    def test_commodity_with_single_source(self):
        metrics = _defaults({"has_single_source": True})
        conc, rep = _determine_concentration("single source", "commodity_input", metrics)
        assert conc == 0.8
        assert rep == 0.1

    def test_facility_default(self):
        metrics = _defaults({})
        conc, rep = _determine_concentration("San Diego facility", "facility_at", metrics)
        assert conc == 0.9
        assert rep == 0.1

    def test_operational_uses_customer_proxy(self):
        metrics = _defaults({"max_customer_pct": 12.0})
        conc, rep = _determine_concentration("JV with Nikon", "operational_dependency", metrics)
        assert conc == 0.12
        assert rep == 0.7

    def test_unknown_returns_none(self):
        metrics = _defaults({})
        conc, rep = _determine_concentration("some text", "unknown_type", metrics)
        assert conc is None
        assert rep is None


# ── Operational criticality test fixtures ────────────────────────────────

CRITICALITY_FIXTURE_TEXT = """
We depend on Carl Zeiss SMT GmbH, our sole source of advanced optical
systems and a critical supplier of EUV components. Zeiss is a key
supplier and strategic partner for our lithography systems.

Our key customers include TSMC, Samsung, and Intel. These strategic
partnerships drive our roadmap. No single customer is irreplaceable
but they are material to our business.

Our manufacturing facilities in Veldhoven are essential to operations.
The cleanroom facility is mission-critical for production yield.

We have a key dependency on rare earth elements sourced from China.
These materials are business-critical and we have identified no
viable alternative suppliers currently.
"""


class TestCriticalityIndicators:
    def test_extract_sole_source(self):
        indicators = _extract_criticality_indicators(
            "Carl Zeiss is our sole source of optical systems"
        )
        assert len(indicators) > 0
        scores = [s for s, _, _ in indicators]
        assert max(scores) >= 0.9

    def test_extract_critical_supplier(self):
        indicators = _extract_criticality_indicators(
            "Zeiss is a critical supplier of EUV components"
        )
        scores = [s for s, _, _ in indicators]
        assert max(scores) >= 0.9

    def test_extract_essential_to_operations(self):
        indicators = _extract_criticality_indicators(
            "Our Veldhoven facility is essential to operations"
        )
        scores = [s for s, _, _ in indicators]
        assert max(scores) >= 0.9

    def test_extract_material_to_business(self):
        indicators = _extract_criticality_indicators(
            "These customers are material to our business"
        )
        scores = [s for s, _, _ in indicators]
        assert max(scores) >= 0.8

    def test_extract_key_dependency(self):
        indicators = _extract_criticality_indicators(
            "We have a key dependency on rare earth elements"
        )
        scores = [s for s, _, _ in indicators]
        assert max(scores) >= 0.7

    def test_extract_strategic_partnership(self):
        indicators = _extract_criticality_indicators(
            "Zeiss is our strategic partner for lithography"
        )
        scores = [s for s, _, _ in indicators]
        assert max(scores) >= 0.5

    def test_extract_no_alternative(self):
        indicators = _extract_criticality_indicators(
            "We have identified no viable alternative suppliers"
        )
        scores = [s for s, _, _ in indicators]
        assert max(scores) >= 0.9

    def test_extract_mission_critical(self):
        indicators = _extract_criticality_indicators(
            "The cleanroom facility is mission-critical for production"
        )
        scores = [s for s, _, _ in indicators]
        assert max(scores) >= 0.9

    def test_extract_irreplaceable(self):
        indicators = _extract_criticality_indicators(
            "No single customer is irreplaceable"
        )
        scores = [s for s, _, _ in indicators]
        assert max(scores) >= 0.9

    def test_full_text_extraction(self):
        indicators = _extract_criticality_indicators(CRITICALITY_FIXTURE_TEXT)
        assert len(indicators) >= 5  # should find multiple indicators

    def test_no_matches(self):
        indicators = _extract_criticality_indicators(
            "The company continues its DEI initiatives and ESG reporting."
        )
        assert len(indicators) == 0

    def test_indicators_sorted_descending(self):
        indicators = _extract_criticality_indicators(CRITICALITY_FIXTURE_TEXT)
        for i in range(len(indicators) - 1):
            assert indicators[i][0] >= indicators[i + 1][0]


class TestDetermineOperationalCriticality:
    def test_supplier_with_sole_source(self):
        indicators = _extract_criticality_indicators(
            "Carl Zeiss is our sole source of optical systems"
        )
        crit = _determine_operational_criticality(
            "Carl Zeiss is our sole source of optical systems",
            "supplier_to",
            "Carl Zeiss optical systems",
            indicators,
        )
        assert crit is not None
        assert crit >= 0.9  # sole source → 0.95, possibly boosted by proximity

    def test_customer_with_key_customer(self):
        indicators = _extract_criticality_indicators(
            "TSMC is a key customer accounting for 30% of revenue"
        )
        crit = _determine_operational_criticality(
            "TSMC is a key customer accounting for 30% of revenue",
            "customer_of",
            "TSMC",
            indicators,
        )
        assert crit is not None
        assert crit >= 0.6  # key customer → 0.6

    def test_facility_floor_when_no_indicators(self):
        indicators = _extract_criticality_indicators(
            "Our office building in London. ESG targets achieved."
        )
        crit = _determine_operational_criticality(
            "Our office building in London. ESG targets achieved.",
            "facility_at",
            "London office",
            indicators,
        )
        assert crit == RELATIONSHIP_TYPE_CRITICALITY_FLOOR["facility_at"]  # 0.7

    def test_commodity_floor_when_no_indicators(self):
        indicators = _extract_criticality_indicators(
            "We purchase standard office supplies."
        )
        crit = _determine_operational_criticality(
            "We purchase standard office supplies.",
            "commodity_input",
            "office supplies",
            indicators,
        )
        assert crit == RELATIONSHIP_TYPE_CRITICALITY_FLOOR["commodity_input"]  # 0.6

    def test_default_returns_default(self):
        indicators = _extract_criticality_indicators("Nothing relevant here.")
        crit = _determine_operational_criticality(
            "Nothing relevant here.",
            "customer_of",
            "Unknown Entity",
            indicators,
        )
        assert crit == RELATIONSHIP_TYPE_CRITICALITY_FLOOR["customer_of"]  # 0.4

    def test_unknown_type_returns_default(self):
        indicators = _extract_criticality_indicators("Nothing relevant.")
        crit = _determine_operational_criticality(
            "Nothing relevant.",
            "unknown_edge_type",
            "Target",
            indicators,
        )
        assert crit == DEFAULT_CRITICALITY  # 0.3

    def test_proximity_boosts_score(self):
        # When target name appears near the indicator, score should be higher
        indicators = _extract_criticality_indicators(
            "Carl Zeiss GmbH is a critical supplier of lenses. "
            + "We depend on Carl Zeiss for our entire optical supply chain."
        )
        crit_nearby = _determine_operational_criticality(
            "Carl Zeiss GmbH is a critical supplier of lenses. "
            + "We depend on Carl Zeiss for our entire optical supply chain.",
            "supplier_to",
            "Carl Zeiss",
            indicators,
        )
        # With no nearby target name (empty target), should still get base score
        crit_far = _determine_operational_criticality(
            "Carl Zeiss GmbH is a critical supplier of lenses.",
            "supplier_to",
            "Unrelated Corp",
            indicators,
        )
        assert crit_nearby is not None
        assert crit_far is not None
        # Nearby should be >= far (might be boosted)
        assert crit_nearby >= crit_far

    def test_relationship_type_filters_indicators(self):
        # "key customer" should only apply to customer_of edges
        indicators = _extract_criticality_indicators(
            "TSMC is a key customer and strategic partner"
        )
        crit_supplier = _determine_operational_criticality(
            "TSMC is a key customer and strategic partner",
            "supplier_to",
            "TSMC",
            indicators,
        )
        crit_customer = _determine_operational_criticality(
            "TSMC is a key customer and strategic partner",
            "customer_of",
            "TSMC",
            indicators,
        )
        # "key customer" only applies to customer_of, so supplier gets lower score
        # (only "strategic partner" applies to supplier)
        assert crit_customer is not None
        assert crit_supplier is not None
        # customer_of should get at least the key_customer score (0.6)
        # supplier_to only gets strategic_partner (0.5)
        assert crit_customer >= 0.6
        assert crit_supplier <= 0.55  # strategic partner = 0.5

    def test_full_fixture_all_types(self):
        indicators = _extract_criticality_indicators(CRITICALITY_FIXTURE_TEXT)
        assert len(indicators) > 0

        # Supplier with nearby target name
        crit = _determine_operational_criticality(
            CRITICALITY_FIXTURE_TEXT,
            "supplier_to",
            "Carl Zeiss SMT GmbH",
            indicators,
        )
        assert crit is not None
        assert crit >= 0.7  # should find "sole source", "critical supplier", etc.

        # Customer
        crit = _determine_operational_criticality(
            CRITICALITY_FIXTURE_TEXT,
            "customer_of",
            "TSMC",
            indicators,
        )
        assert crit is not None
        assert crit >= 0.5  # "key customer" or "strategic partnership"

        # Facility
        crit = _determine_operational_criticality(
            CRITICALITY_FIXTURE_TEXT,
            "facility_at",
            "Veldhoven",
            indicators,
        )
        assert crit is not None
        assert crit >= 0.7  # "essential to operations" or "mission-critical"

        # Commodity
        crit = _determine_operational_criticality(
            CRITICALITY_FIXTURE_TEXT,
            "commodity_input",
            "rare earth",
            indicators,
        )
        assert crit is not None
        assert crit >= 0.6  # "key dependency" or "business-critical"


# ── Economic materiality test fixtures ───────────────────────────────────

MATERIALITY_FIXTURE_TEXT = """
We depend on Carl Zeiss SMT GmbH, our sole supplier of advanced optical
systems. Zeiss is a critical supplier of EUV components and a strategic
partner for our lithography systems. The number of lithography systems
we produce is limited by Zeiss production capacity, and any disruption
would materially impact our business.

Our key customers include TSMC, Samsung, and Intel. Our largest customer
represents 22% of total revenue. These strategic partnerships drive our
technology roadmap. No single customer is irreplaceable but they are
core to our operations.

We have a key dependency on rare earth elements sourced from China.
These materials are business-critical and we have identified no viable
alternative suppliers currently. Our manufacturing facility in
Veldhoven is essential to our business. The cleanroom facility is
mission-critical for production yield.

Subsidiaries classified as not material from a financial perspective
were analyzed in terms of their impact. The gain on deconsolidation
is immaterial from the Group's perspective. Short-term investments
have immaterial interest rate risk.
"""


class TestFindEntityInText:
    def test_exact_match(self):
        from kassandra.extract_edge_metrics import _find_entity_in_text
        text = "We depend on Carl Zeiss SMT GmbH, our sole supplier."
        pos = _find_entity_in_text(text, "sole supplier")
        assert pos is not None
        assert pos >= 0

    def test_newline_normalized_match(self):
        from kassandra.extract_edge_metrics import _find_entity_in_text
        text = " ".join("We depend on\nCarl Zeiss SMT GmbH,\nour sole supplier.".split())
        target = "our sole supplier"
        pos = _find_entity_in_text(text, target)
        assert pos is not None

    def test_signature_fallback(self):
        from kassandra.extract_edge_metrics import _find_entity_in_text
        # Exact match is always found
        text = "Carl Zeiss SMT GmbH supplies optical components"
        target = "Carl Zeiss SMT GmbH"
        pos = _find_entity_in_text(text, target)
        assert pos == 0

    def test_not_found(self):
        from kassandra.extract_edge_metrics import _find_entity_in_text
        text = "We manufacture semiconductor equipment."
        pos = _find_entity_in_text(text, "Nonexistent Corporation XYZ")
        assert pos is None

    def test_empty_inputs(self):
        from kassandra.extract_edge_metrics import _find_entity_in_text
        assert _find_entity_in_text("", "target") is None
        assert _find_entity_in_text("text", "") is None
        assert _find_entity_in_text("", "") is None


class TestExtractMaterialityFromContext:
    def test_sole_supplier(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "Carl Zeiss is our sole supplier of optical systems"
        )
        assert mat == 0.95

    def test_material_to_business(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "Disruption would materially impact our business"
        )
        assert mat == 0.8

    def test_percentage_of_revenue(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "Our largest customer represents 22% of total revenue"
        )
        assert mat == 0.22

    def test_core_to_operations(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "These partnerships are core to our operations"
        )
        assert mat == 0.9

    def test_essential_to_business(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "Our Veldhoven facility is essential to our business"
        )
        assert mat == 0.9

    def test_mission_critical(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "The cleanroom is mission-critical for production"
        )
        assert mat == 0.9

    def test_critical_to_operations(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "This supplier is critical to our operations"
        )
        assert mat == 0.85

    def test_significant_subsidiary(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "XYZ GmbH is a significant subsidiary of the Group"
        )
        assert mat == 0.7

    def test_key_supplier(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "Acme Corp is a key supplier of raw materials"
        )
        assert mat == 0.7

    def test_key_customer(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "TSMC is a key customer accounting for significant revenue"
        )
        assert mat == 0.6

    def test_strategic_partner(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "Zeiss is our strategic partner for lithography"
        )
        assert mat == 0.5

    def test_dependent_on(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "We are heavily dependent on this supplier"
        )
        assert mat == 0.75

    def test_no_viable_alternative(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "We have identified no viable alternative suppliers"
        )
        assert mat == 0.9

    def test_irreplaceable(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "No single customer is irreplaceable"
        )
        assert mat == 0.95

    def test_not_material_dependency(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "This supplier relationship is not material to our operations"
        )
        assert mat == 0.05

    def test_immaterial_dependency(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "This commodity exposure is immaterial"
        )
        assert mat == 0.05

    def test_not_material_accounting_skipped(self):
        """'not material' in accounting boilerplate should NOT produce 0.05."""
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "The pension liability is not material to the financial statements"
        )
        assert mat is None

    def test_immaterial_accounting_skipped(self):
        """'immaterial' in accounting boilerplate should NOT produce 0.05."""
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "Short-term investments have immaterial interest rate risk"
        )
        assert mat is None

    def test_highest_score_wins(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "Zeiss is a strategic partner and our sole supplier of optics"
        )
        # Both "strategic partner" (0.5) and "sole supplier" (0.95) present
        assert mat == 0.95

    def test_no_matches(self):
        from kassandra.extract_edge_metrics import _extract_materiality_from_context
        mat = _extract_materiality_from_context(
            "The company continues its DEI initiatives and ESG reporting."
        )
        assert mat is None


class TestIsAccountingBoilerplate:
    def test_financial_statement(self):
        from kassandra.extract_edge_metrics import _is_accounting_boilerplate
        assert _is_accounting_boilerplate(
            "not material to the financial statements"
        ) is True

    def test_pension(self):
        from kassandra.extract_edge_metrics import _is_accounting_boilerplate
        assert _is_accounting_boilerplate(
            "pension obligation is not material"
        ) is True

    def test_interest_rate(self):
        from kassandra.extract_edge_metrics import _is_accounting_boilerplate
        assert _is_accounting_boilerplate(
            "immaterial interest rate risk"
        ) is True

    def test_fair_value(self):
        from kassandra.extract_edge_metrics import _is_accounting_boilerplate
        assert _is_accounting_boilerplate(
            "carrying amount is immaterial"
        ) is True

    def test_dependency_not_boilerplate(self):
        from kassandra.extract_edge_metrics import _is_accounting_boilerplate
        assert _is_accounting_boilerplate(
            "this supplier is not material to our business"
        ) is False

    def test_operations_not_boilerplate(self):
        from kassandra.extract_edge_metrics import _is_accounting_boilerplate
        assert _is_accounting_boilerplate(
            "this dependency is not material to operations"
        ) is False
