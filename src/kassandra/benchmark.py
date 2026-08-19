"""Curated positive-only classifier regression fixtures.

⚠ P1-1 AUDIT QUALIFICATION (engineering audit 2026-06-19):

The 100% detection rate and 329-day average lead time are PROVISIONAL
and likely inflated. These claims are based on:
- 5 SELECTED distressed companies (Carillion, Thomas Cook, Wirecard,
  Patisserie Valerie, Interserve) — not a representative sample
- 20 hand-curated signal texts — NOT a random draw from real public sources
- Possible hindsight leakage: signal texts may contain post-collapse language
- Pattern tuning: classifier patterns were iteratively refined against these
  exact examples (NOT a frozen, independent test set)
- No false-positive denominator: no benign texts from the same time period
  were tested alongside these signals

The honest claim is:
"On a small curated positive set of 20 historical signal examples from
5 known distressed companies, the classifier detected all included examples
after iterative pattern refinement."

This does NOT validate real-world detection, precision, or early-warning
capability. A proper evaluation requires:
- Publication-date enforcement on all evidence
- Frozen classifier version evaluated on unseen examples
- Mixed corpus with contemporaneous benign documents
- False-positive rate measurement on a real portfolio

Curated cases with public deterioration signals and known timelines.
Used only for classifier/regression exploration. It must not emit validated-product claims or be used as a point-in-time benchmark.
"""

from dataclasses import dataclass, field


@dataclass
class SignalPoint:
    """A point-in-time adverse signal."""
    date: str  # ISO date (YYYY-MM-DD)
    description: str
    event_type: str
    severity: str
    source_type: str  # companies_house, gazette, press_release, regulatory_filing, etc.
    source_url: str | None = None
    excerpt: str | None = None  # Representative text snippet


@dataclass
class DistressedCase:
    """A known case of corporate deterioration."""
    case_id: str
    company_name: str
    jurisdiction: str  # ISO 3166-1 alpha-2
    isin: str | None  # If publicly listed
    company_number: str | None  # Companies House number if UK

    # Key dates
    signal_timeline: list[SignalPoint] = field(default_factory=list)
    deterioration_date: str = ""  # When deterioration crystallized (insolvency/admin)
    market_impact_date: str = ""  # When market priced it in (share suspension, delisting)

    # Metadata
    sector: str = ""
    outcome: str = ""  # liquidation, administration, acquisition, restructuring, etc.
    peak_market_cap_eur: float = 0.0
    notes: str = ""


# =============================================================================
# Benchmark Cases
# =============================================================================

BENCHMARK_CASES: list[DistressedCase] = [
    DistressedCase(
        case_id="C001_carillion",
        company_name="Carillion Plc",
        jurisdiction="GB",
        isin="GB0007365546",
        company_number="03782379",
        sector="Construction & Services",
        outcome="Compulsory liquidation",
        deterioration_date="2018-01-15",  # Compulsory liquidation
        market_impact_date="2018-01-15",  # Share suspension same day
        peak_market_cap_eur=2_200_000_000,  # ~£2bn at peak
        signal_timeline=[
            SignalPoint(
                date="2017-07-10",
                description="Profit warning: £845m write-down on construction contracts, CEO resigns",
                event_type="profit_warning",
                severity="medium",
                source_type="press_release",
                excerpt="Carillion announces a provision of £845 million against its support services and construction contracts. CEO Richard Howson steps down with immediate effect."
            ),
            SignalPoint(
                date="2017-09-29",
                description="Second profit warning, dividend suspended, net debt higher than expected",
                event_type="profit_warning",
                severity="medium",
                source_type="press_release",
                excerpt="Carillion announces further contract provisions and suspends the 2017 dividend. Net debt at half-year higher than market expectations."
            ),
            SignalPoint(
                date="2017-11-17",
                description="Third profit warning: further £200m provision, covenant breach likely",
                event_type="profit_warning",
                severity="medium",
                source_type="press_release",
                excerpt="Carillion warns on further covenant breaches and announces additional provisions, bringing total write-downs to over £1 billion."
            ),
            SignalPoint(
                date="2018-01-14",
                description="Banks refuse further funding, directors file for compulsory liquidation",
                event_type="insolvency",
                severity="critical",
                source_type="press_release",
                excerpt="The Board of Carillion announced it had no choice but to take steps to enter into compulsory liquidation with immediate effect."
            ),
        ],
        notes="Three profit warnings between July-Nov 2017 before liquidation Jan 2018. Filed extensive accounts at Companies House. Gazette notices published post-liquidation."
    ),

    DistressedCase(
        case_id="C002_thomas_cook",
        company_name="Thomas Cook Group Plc",
        jurisdiction="GB",
        isin="GB00B1VYCH82",
        company_number="06091951",
        sector="Travel & Leisure",
        outcome="Compulsory liquidation",
        deterioration_date="2019-09-23",  # Entered administration
        market_impact_date="2019-09-23",  # Share suspension
        peak_market_cap_eur=2_500_000_000,
        signal_timeline=[
            SignalPoint(
                date="2018-11-27",
                description="Profit warning: weaker summer trading, cautious outlook",
                event_type="profit_warning",
                severity="medium",
                source_type="press_release",
                excerpt="Thomas Cook warns that full year underlying EBIT will be lower than market expectations due to weaker summer trading."
            ),
            SignalPoint(
                date="2019-02-07",
                description="Second profit warning, strategic review announced",
                event_type="profit_warning",
                severity="medium",
                source_type="press_release",
                excerpt="Thomas Cook issues another profit warning and announces a strategic review of its airline business."
            ),
            SignalPoint(
                date="2019-05-16",
                description="Half-year loss of £1.5bn, going concern warning from auditors",
                event_type="going_concern_warning",
                severity="high",
                source_type="regulatory_filing",
                excerpt="E&Y flags material uncertainty regarding Thomas Cook's ability to continue as a going concern after £1.5bn half-year loss."
            ),
            SignalPoint(
                date="2019-07-12",
                description="Bailout talks with Fosun, banks demand £200m contingency facility",
                event_type="refinancing_stress",
                severity="high",
                source_type="press_release",
                excerpt="Thomas Cook confirms it is in advanced discussions with Fosun and its banks for a £750m recapitalisation."
            ),
            SignalPoint(
                date="2019-09-20",
                description="Banks demand additional £200m, rescue talks collapse",
                event_type="refinancing_stress",
                severity="high",
                source_type="press_release",
                excerpt="Thomas Cook confirms that discussions with stakeholders have not resulted in agreement, and the Board concludes there is no choice but to enter compulsory liquidation."
            ),
        ],
        notes="Multiple profit warnings in 2018-2019 before Sep 2019 collapse. Had going concern warning from auditors in May 2019. Gazette notices post-liquidation."
    ),

    DistressedCase(
        case_id="C003_wirecard",
        company_name="Wirecard AG",
        jurisdiction="DE",
        isin="DE0007472060",
        company_number=None,
        sector="Financial Technology",
        outcome="Insolvency",
        deterioration_date="2020-06-25",  # Filed for insolvency
        market_impact_date="2020-06-18",  # Share price collapsed after auditor refusal
        peak_market_cap_eur=24_000_000_000,
        signal_timeline=[
            SignalPoint(
                date="2019-01-30",
                description="FT investigation alleges accounting fraud in Singapore operations",
                event_type="litigation_material",
                severity="medium",
                source_type="press_release",
                excerpt="Financial Times investigation alleges fraud and forgery at Wirecard's Singapore office."
            ),
            SignalPoint(
                date="2019-10-22",
                description="KPMG special audit finds inability to verify large parts of business",
                event_type="auditor_warning",
                severity="high",
                source_type="regulatory_filing",
                excerpt="KPMG special audit report states it was unable to verify the existence of large parts of Wirecard's reported business."
            ),
            SignalPoint(
                date="2020-06-18",
                description="Auditor E&Y refuses to sign off accounts — €1.9bn in cash cannot be verified",
                event_type="auditor_departure",
                severity="medium",
                source_type="regulatory_filing",
                excerpt="EY refuses to sign off Wirecard's 2019 accounts, stating €1.9 billion in cash balances likely do not exist."
            ),
            SignalPoint(
                date="2020-06-22",
                description="CEO Markus Braun arrested, Wirecard admits €1.9bn probably does not exist",
                event_type="litigation_material",
                severity="medium",
                source_type="press_release",
                excerpt="Wirecard CEO Markus Braun arrested by Munich prosecutors. Company admits €1.9 billion in cash balances probably do not exist."
            ),
        ],
        notes="German DAX company. Auditor refusal to sign accounts was the crystallising event. Limited UK Companies House data since German entity."
    ),

    DistressedCase(
        case_id="C004_patisserie_holdings",
        company_name="Patisserie Holdings Plc",
        jurisdiction="GB",
        isin="GB00BM4NV504",
        company_number="05105874",
        sector="Food & Beverage",
        outcome="Administration (wound up)",
        deterioration_date="2019-01-22",  # Entered administration
        market_impact_date="2018-10-10",  # Share suspension after fraud discovery
        peak_market_cap_eur=500_000_000,
        signal_timeline=[
            SignalPoint(
                date="2018-10-10",
                description="Shares suspended — discovery of 'significant and potentially fraudulent accounting irregularities'",
                event_type="litigation_material",
                severity="medium",
                source_type="press_release",
                excerpt="Patisserie Holdings announces discovery of significant and potentially fraudulent accounting irregularities, shares suspended."
            ),
            SignalPoint(
                date="2018-10-11",
                description="Finance director arrested, company has £1.14m cash vs £9.8m claimed",
                event_type="payment_stress",
                severity="high",
                source_type="press_release",
                excerpt="Patisserie Holdings finance director Chris Marsh arrested. Company's actual cash position is £1.14m, not the £9.8m previously reported."
            ),
            SignalPoint(
                date="2018-10-12",
                description="Winding-up petition filed by HMRC for unpaid tax",
                event_type="insolvency",
                severity="critical",
                source_type="gazette",
                excerpt="HM Revenue & Customs files winding-up petition against Patisserie Holdings for £1.14m unpaid tax."
            ),
        ],
        notes="Very sudden: fraud discovered Oct 10, in administration by Jan 22. Gazette winding-up petition filed by HMRC was an early public signal. UK registered company."
    ),

    DistressedCase(
        case_id="C005_intsr_petrofac",
        company_name="Interserve Plc",
        jurisdiction="GB",
        isin="GB0001528156",
        company_number="02653227",
        sector="Construction & Support Services",
        outcome="Administration (pre-pack sale)",
        deterioration_date="2019-03-15",  # Entered administration
        market_impact_date="2019-03-15",
        peak_market_cap_eur=600_000_000,
        signal_timeline=[
            SignalPoint(
                date="2017-09-21",
                description="Profit warning: energy-from-waste contract losses",
                event_type="profit_warning",
                severity="medium",
                source_type="press_release",
                excerpt="Interserve issues profit warning due to significant cost overruns on energy-from-waste contracts."
            ),
            SignalPoint(
                date="2018-03-27",
                description="Debt restructuring plan announced, lenders take control",
                event_type="restructuring",
                severity="high",
                source_type="press_release",
                excerpt="Interserve announces debt restructuring plan involving deleveraging and conversion of debt to equity."
            ),
            SignalPoint(
                date="2018-12-07",
                description="Second restructuring plan rejected by shareholders",
                event_type="restructuring",
                severity="high",
                source_type="press_release",
                excerpt="Interserve shareholders reject deleveraging plan, company warns it may need to enter administration."
            ),
            SignalPoint(
                date="2019-03-15",
                description="Entered administration, pre-pack sale to lenders",
                event_type="insolvency",
                severity="critical",
                source_type="press_release",
                excerpt="Interserve Plc enters administration. Business and assets sold to newly incorporated company controlled by lenders."
            ),
        ],
        notes="Gradual deterioration: profit warning Sep 2017 → restructuring Mar 2018 → administration Mar 2019. 18-month lead time from first warning."
    ),
]
