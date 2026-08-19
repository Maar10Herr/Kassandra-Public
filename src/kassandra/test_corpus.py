"""Golden test corpus for event classification.

Known adverse and benign texts used to validate the content classifier.
Each entry has: id, text, expected_event_type, expected_severity, is_adverse
"""

# Known adverse events — texts that SHOULD be classified
ADVERSE_CASES = [
    {
        "id": "A001_insolvency_petition",
        "text": """
IN THE HIGH COURT OF JUSTICE BUSINESS AND PROPERTY COURTS OF ENGLAND AND WALES
INSOLVENCY AND COMPANIES LIST (ChD)

No CR-2024-001234 of 2024

In the Matter of ACME TRADING LIMITED
and in the Matter of the INSOLVENCY ACT 1986

A petition to wind up the above-named company of 123 Commercial Road, London EC1A 1AA
presented on 15 March 2024 by HMRC is scheduled to be heard on 10 May 2024.

Any person intending to appear at the hearing must give notice in accordance with
Rule 7.14 of the Insolvency (England and Wales) Rules 2016 by 16:00 on 9 May 2024.

The petitioner's solicitor is HMRC Legal Services, 100 Parliament Street, London SW1A 2BQ.
        """.strip(),
        "expected_event_type": "insolvency",
        "expected_severity": "critical",
        "is_adverse": True,
    },
    {
        "id": "A002_administration_order",
        "text": """
COMPANY NUMBER: 04567890

NOTICE OF ADMINISTRATION ORDER

In the High Court of Justice, Business and Property Courts of England and Wales

Date: 20 January 2024

Nature of Business: Construction and Civil Engineering

An administration order was made on 15 January 2024 in respect of BUILDWELL CONSTRUCTION LTD
(Company Number 04567890) of Unit 5, Olympic Park, Stratford, London E20 1AA.

Joint Administrators: James Smith and Sarah Jones of KPMG LLP, 15 Canada Square, London E14 5GL.

The administrators may be contacted at buildwell@example.com or by telephone at 020 0000 0000.
        """.strip(),
        "expected_event_type": "insolvency",
        "expected_severity": "critical",
        "is_adverse": True,
    },
    {
        "id": "A003_profit_warning_press",
        "text": """
PRESS RELEASE — FOR IMMEDIATE RELEASE

EUROTECH INDUSTRIES PLC ISSUES PROFIT WARNING

LONDON, 25 February 2024 — EuroTech Industries Plc (LSE: ETI) today announces a profit warning
for the financial year ending 31 December 2023.

Following a preliminary review of unaudited results, the Board expects adjusted EBITDA to be
approximately 40% lower than current market consensus. The shortfall is primarily attributable
to delayed customer orders in the Semiconductor division and higher than anticipated raw
material costs in the Automotive division.

The Company now expects full-year revenue of approximately EUR 450 million, compared to
prior guidance of EUR 520-540 million issued on 15 November 2023.

CEO Marcus Weber commented: "We are clearly disappointed with these preliminary results.
We have initiated a comprehensive cost reduction program and are reviewing all non-core assets."

The Company will announce its full-year results on 28 March 2024.
        """.strip(),
        "expected_event_type": "profit_warning",
        "expected_severity": "medium",
        "is_adverse": True,
    },
    {
        "id": "A004_going_concern_auditor",
        "text": """
INDEPENDENT AUDITOR'S REPORT TO THE MEMBERS OF REGIONAL RETAIL GROUP PLC

Opinion
We have audited the financial statements of Regional Retail Group Plc for the year ended
31 December 2023.

Material uncertainty related to going concern
We draw attention to Note 2 in the financial statements, which indicates that the Group
incurred a net loss of GBP 45 million during the year ended 31 December 2023 and, as of that
date, the Group's current liabilities exceeded its current assets by GBP 28 million.

As stated in Note 2, these events or conditions, along with the matters set forth in Note 2,
indicate that a material uncertainty exists that may cast significant doubt on the Group's
ability to continue as a going concern. Our opinion is not modified in respect of this matter.
        """.strip(),
        "expected_event_type": "going_concern_warning",
        "expected_severity": "high",
        "is_adverse": True,
    },
    {
        "id": "A005_restructuring_program",
        "text": """
ANNOUNCEMENT

NORTHERN MANUFACTURING GROUP ANNOUNCES MAJOR RESTRUCTURING PROGRAM

STOCKHOLM, 10 April 2024 — Northern Manufacturing Group AB (STO: NMG) today announced a
comprehensive restructuring program designed to improve operational efficiency and reduce
annual costs by approximately SEK 800 million.

The restructuring program includes:
- A workforce reduction of approximately 1,200 positions across Europe
- Consolidation of three manufacturing facilities into two
- A SEK 500 million capital reduction to strengthen the balance sheet
- A debt restructuring agreement with principal lenders

CEO Anna Lindström stated: "These are difficult but necessary decisions to secure the
long-term competitiveness of Northern Manufacturing Group. We expect annualized savings
of SEK 800 million by the end of 2025."

The Company expects to record restructuring charges of approximately SEK 600 million in Q2 2024.
        """.strip(),
        "expected_event_type": "restructuring",
        "expected_severity": "high",
        "is_adverse": True,
    },
    {
        "id": "A006_auditor_resignation",
        "text": """
COMPANIES HOUSE FILING

Company Number: 07123456

NOTIFICATION OF RESIGNATION OF AUDITOR

Pursuant to Section 519 of the Companies Act 2006

Company Name: BRIGHTON PROPERTY DEVELOPMENTS LIMITED

We hereby give notice that PricewaterhouseCoopers LLP resigned as auditor of the company on
5 March 2024.

A statement of circumstances connected with the resignation has been deposited at the
company's registered office.

Signed: PwC LLP, 1 Embankment Place, London WC2N 6RH
        """.strip(),
        "expected_event_type": "auditor_departure",
        "expected_severity": "medium",
        "is_adverse": True,
    },
    {
        "id": "A007_layoffs_announcement",
        "text": """
INTERNAL COMMUNICATION — FOR DISTRIBUTION TO ALL STAFF

Subject: Organisational Changes

Dear Colleagues,

Following the Board's review of our operational structure, I regret to inform you that we will
be implementing a workforce reduction affecting approximately 800 positions across our European
operations. This represents roughly 15% of our total headcount.

The redundancies will be primarily in our manufacturing and administrative functions.
Affected employees will be notified by their line managers within the next two weeks and
will receive enhanced severance packages and outplacement support.

These job cuts are part of a broader downsizing initiative aimed at reducing our cost base
by EUR 50 million annually. A short-time work scheme has also been proposed for our
German operations.

I understand this is difficult news, and we are committed to treating all affected
colleagues with dignity and respect.
        """.strip(),
        "expected_event_type": "layoffs",
        "expected_severity": "medium",
        "is_adverse": True,
    },
]

# Known benign cases — texts that should NOT be classified
BENIGN_CASES = [
    {
        "id": "B001_annual_results_positive",
        "text": """
PRESS RELEASE

GLOBAL TECH SOLUTIONS PLC ANNOUNCES STRONG FULL-YEAR RESULTS

LONDON, 28 February 2024 — Global Tech Solutions Plc (LSE: GTS) today announces its
audited results for the year ended 31 December 2023.

Financial Highlights:
- Revenue increased 18% to GBP 2.4 billion
- Operating profit increased 22% to GBP 480 million
- Basic EPS increased 20% to 245 pence
- Full-year dividend increased 15% to 92 pence per share
- Strong balance sheet with net cash position of GBP 350 million

CEO David Chen commented: "2023 was an outstanding year for Global Tech Solutions.
We delivered record revenue and profit, driven by strong demand across all our business
segments. We continued to invest in our technology platform while maintaining cost discipline."

The Board is confident in the outlook for 2024, with a strong order book and positive
momentum across all markets.
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
    },
    {
        "id": "B002_board_appointment",
        "text": """
REGULATORY ANNOUNCEMENT

APPOINTMENT OF NEW NON-EXECUTIVE DIRECTOR

EUROPEAN BANKING GROUP PLC

The Board of European Banking Group Plc is pleased to announce the appointment of
Dr. Maria Fernandez as an independent Non-Executive Director with effect from
1 February 2024.

Dr. Fernandez brings extensive experience in financial services, having previously served
as Chief Risk Officer at Banco Santander and as a member of the Prudential Regulation
Authority's advisory panel.

She will join the Audit Committee and the Risk Committee.

There are no further disclosures required under Listing Rule 9.6.13 in respect of
this appointment.
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
    },
    {
        "id": "B003_standard_risk_disclosure",
        "text": """
RISK FACTORS

The following risk factors may materially affect the Company's business, financial
condition, and results of operations. The risks described below are not the only
risks the Company faces.

- The Company may be unable to sustain its historical growth rates.
- The Company operates in highly competitive markets.
- The Company's results could be adversely affected by currency exchange rate fluctuations.
- The Company is subject to extensive government regulation.
- The Company may experience cybersecurity incidents or data breaches.
- The Company depends on key personnel and may not be able to attract or retain talent.
- The Company may face litigation and regulatory proceedings in the ordinary course of business.
- The Company's intellectual property rights may not be adequately protected.
- The Company may be subject to product liability or warranty claims.

These forward-looking statements involve risks and uncertainties that could cause actual
results to differ materially from those projected.
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
    },
    {
        "id": "B004_quarterly_update_neutral",
        "text": """
TRADING UPDATE

QUARTERLY TRADING UPDATE FOR Q1 2024

RENEWABLE ENERGY GROUP SA

Renewable Energy Group SA (EPA: REG) today publishes its trading update for the
first quarter ended 31 March 2024.

Total installed capacity: 12.4 GW (+3% vs Q1 2023)
Power generation: 18.7 TWh (+5% vs Q1 2023)
Average realised price: EUR 82/MWh (-8% vs Q1 2023)

The Group's operations continued to perform in line with expectations. Construction
of the North Sea Wind cluster remains on schedule with first power expected in H2 2025.

Full-year 2024 guidance unchanged: EBITDA EUR 4.8 - 5.2 billion.
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
    },
    {
        "id": "B005_dividend_announcement_neutral",
        "text": """
DIVIDEND ANNOUNCEMENT

CONTINENTAL CONSUMER GOODS NV

The Board of Continental Consumer Goods NV has declared an interim dividend of EUR 0.85
per ordinary share for the financial year 2024.

Key dates:
- Ex-dividend date: 15 May 2024
- Record date: 16 May 2024
- Payment date: 5 June 2024

This represents a 6% increase compared to the prior year interim dividend of EUR 0.80
per share, reflecting the Board's confidence in the Group's cash generation and outlook.

The dividend will be paid in cash. Shareholders may elect to receive the dividend in
shares through the optional scrip dividend program.
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
    },
    # P1-6: Additional negative examples — known false positive patterns
    {
        "id": "B006_sap_corporate_blog_layoffs",
        "text": """
SAP Announces Workforce Transformation Program

WALLDORF, Germany — SAP SE today announced a company-wide restructuring program
designed to reposition the company for future growth in AI and cloud technologies.

The program includes a voluntary leave program and targeted job reductions affecting
approximately 8,000 positions globally. However, SAP expects its overall headcount
to remain stable as it invests in strategic growth areas, with total headcount expected
to exceed current levels by end of 2025.

CEO Christian Klein said: "This is not a cost-cutting exercise. We are reshaping our
workforce to capture the significant opportunity in Business AI. We will continue to
hire in strategic areas while offering voluntary programs in others."
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
    },
    {
        "id": "B007_sports_layoff_context",
        "text": """
Los Angeles Lakers star LeBron James is expected to miss 2-3 weeks due to a groin
injury suffered during Tuesday night's game against the Denver Nuggets. The injury
layoff comes at a critical point in the season with the Lakers fighting for playoff
positioning in the Western Conference.

Team doctors confirmed the injury is a grade 1 groin strain and will not require
surgery. James is expected to begin rehabilitation immediately.
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
    },
    {
        "id": "B008_cybersecurity_risk_disclosure",
        "text": """
RISK FACTORS (continued)

The Group faces cybersecurity risk from a variety of sources. According to a recent
CESIN survey, one in two French companies reported experiencing at least one cyber
incident in the past 12 months. The Group maintains comprehensive cybersecurity
programs and insurance coverage to mitigate these risks.

While the Group has not experienced a material cybersecurity breach to date, there
can be no assurance that its security measures will be sufficient to prevent future
incidents, which could result in reputational damage, regulatory penalties, and
financial losses.
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
    },
    {
        "id": "B009_restructuring_digital_transformation",
        "text": """
PRESS RELEASE

BNP Paribas Announces Digital Transformation Program

PARIS — BNP Paribas today announced a three-year digital transformation program
designed to modernize its technology infrastructure and improve customer experience.
The program will involve restructuring of certain IT operations and the consolidation
of legacy systems, with expected investments of EUR 3 billion over the period.

The transformation is expected to generate annual cost savings of EUR 400 million
from 2027 onwards, primarily through automation and process optimisation. The bank
emphasised that this is a technology-driven initiative and does not involve significant
workforce reductions.
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
    },
    {
        "id": "B010_product_liquidation_sale",
        "text": """
SPECIAL OFFER — STOCK LIQUIDATION SALE

Due to a warehouse relocation, we are offering significant discounts on all remaining
inventory. All items must go — liquidation prices on furniture, electronics, and
home goods. Up to 70% off retail prices. Sale ends 30 June 2024.

Visit our showroom at 123 High Street or shop online at www.example.com/liquidation.
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
    },
]

# Hard negative cases — texts that look like adverse events but should NOT trigger
# P0 #2: At least 5 hard negatives to validate classifier precision
HARD_NEGATIVE_CASES = [
    {
        "id": "HN001_insolvency_explicitly_denied",
        "text": """
PRESS RELEASE — CLARIFICATION

GLOBAL MARITIME HOLDINGS PLC

Following recent market speculation, Global Maritime Holdings Plc wishes to clarify
that it is not entering insolvency and has not filed for bankruptcy or administration.
The Company confirms that it is not in administration and has no intention of entering
into any insolvency proceedings.

The Company continues to trade normally and is in full compliance with all its
financial covenants. The recent speculation is entirely unfounded and the Company
is taking legal advice regarding the source of these rumours.

For further information, please contact: investor.relations@example.com
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
        "rationale": "Explicit denial of insolvency — negative language should not trigger",
    },
    {
        "id": "HN002_restructuring_rumour_denied",
        "text": """
STATEMENT REGARDING MARKET RUMOURS

ATLANTIC ENERGY GROUP

Atlantic Energy Group notes recent press articles speculating about a potential
restructuring program and cost-cutting exercise at the Company.

The Board confirms that no restructuring program is being planned or implemented.
While the Company continuously reviews its cost base as part of normal business
operations, there are no plans for significant workforce reductions or facility
closures.

The rumour of a restructuring is categorically denied by management.
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
        "rationale": "Rumour of restructuring explicitly denied — should not trigger",
    },
    {
        "id": "HN003_not_material_litigation",
        "text": """
QUARTERLY REPORT — LEGAL UPDATE

MERIDIAN BANKING CORPORATION

During the quarter, the Company was named as a defendant in a routine commercial
dispute relating to a vendor contract. The amount in dispute is approximately
EUR 50,000.

Management has assessed the matter and determined that it is not material to the
Company's financial position or results of operations. The Company intends to
defend the claim vigorously and does not expect any material adverse impact.

This disclosure is made for completeness and does not represent a material event.
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
        "rationale": "Litigation explicitly stated as not material — should not trigger",
    },
    {
        "id": "HN004_historical_unrelated_company",
        "text": """
ARCHIVE — FINANCIAL TIMES, 15 MARCH 2003

The collapse of European Telecom NV in 2002 remains one of the largest corporate
bankruptcies in Dutch history. The company filed for insolvency protection under
Dutch law after failing to restructure EUR 12 billion in debt.

European Telecom NV, once the largest telecommunications provider in the Netherlands,
entered administration in December 2002 following a failed debt restructuring
negotiation with its principal lenders. The bankruptcy resulted in approximately
5,000 layoffs across its European operations.

The case is not related to any currently trading company and is presented here
for historical reference only. No current entity shares this company's name or
registration details.
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
        "rationale": "Historical article about unrelated long-defunct company — should not trigger adverse for any current entity",
    },
    {
        "id": "HN005_profit_warning_for_different_entity",
        "text": """
MARKET REPORT — SECTOR UPDATE

EQUITY RESEARCH — EUROPEAN RETAIL SECTOR

Our analysis indicates that MegaRetail GmbH, a privately-held German discount
retailer, issued a profit warning to its private shareholders last week. MegaRetail
is not a listed company and is not part of any major index.

This profit warning has no implications for the listed companies in our coverage
universe, including the entities we monitor. The warning relates specifically to
MegaRetail's German discount operations which do not compete directly with our
covered names.

For questions about this report, contact: research@example.com
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
        "rationale": "Profit warning about an unrelated unlisted entity — should not trigger for monitored companies",
    },
    {
        "id": "HN006_severance_program_denied_as_cost_cutting",
        "text": """
INTERNAL MEMO — EMPLOYEE COMMUNICATION

Subject: Voluntary Severance Program

Dear Colleagues,

We are pleased to announce a new voluntary early retirement program for employees
aged 55 and above with at least 10 years of service. This program is designed to
provide flexible retirement options and is not a cost-cutting exercise.

This is a voluntary leave program — no redundancies are planned, and participation
is entirely at the employee's discretion. The program is part of our ongoing
commitment to supporting our workforce through all career stages.

We expect this program to have no material impact on our headcount or operating costs.
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
        "rationale": "Voluntary program explicitly not cost-cutting — should not trigger restructuring/layoffs",
    },
    {
        "id": "HN007_cyber_incident_risk_assessment_only",
        "text": """
RISK COMMITTEE REPORT — Q2 2024

CYBERSECURITY POSTURE ASSESSMENT

The Risk Committee has reviewed the Company's cybersecurity posture. While the
risk of a cyber attack or data breach cannot be eliminated entirely, the Committee
notes that the Company has not experienced any material cybersecurity incident
in the reporting period.

The Committee reviewed hypothetical scenarios including what the impact of a
ransomware incident could be on operations. These scenarios are purely analytical
and do not reflect any actual or suspected breach.

The Company maintains comprehensive cybersecurity insurance and incident response
capabilities.
        """.strip(),
        "expected_no_events": True,
        "is_adverse": False,
        "rationale": "Risk assessment only, no actual incident — should not trigger cyber_incident",
    },
]
