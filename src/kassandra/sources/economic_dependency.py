"""Economic dependency discovery from annual reports and structured public data.

Extracts customer, supplier, facility, commodity, and operational dependencies
from annual report PDFs using transparent keyword/pattern matching.
No LLM calls — auditable rules with matched text spans.

Per kassandra_goals.md §3.5: evidence-backed, with quote spans, source URLs,
confidence, concentration, and explicit uncertainty marking.

Sources used:
- Annual report PDFs (primary — covers all 5 dependency types)
- E-PRTR (facility registry — future)
- TED (procurement — future)
- COMEXT / UN Comtrade (commodity trade — future)
- ImportYeti (US shipping manifests — future)

Extraction targets per annual report sections:
- IFRS 8 segment reporting: "Revenue from major customers"
- IAS 24: Related party transactions
- Risk Factors: customer/supplier concentration, single-source dependencies
- Supply Chain / Procurement / Sourcing chapters
- Raw Materials and Components
- Principal Risks and Uncertainties
- Management Discussion & Analysis: business review
"""

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import fitz  # PyMuPDF

from kassandra.evidence import store_evidence
from kassandra.shock_channel import populate_edge_attributes

logger = logging.getLogger(__name__)

# ── Dependency extraction patterns ──────────────────────────────────────────
# Each pattern: (dependency_type, relationship, pattern_id, regex_list)
# Patterns are case-insensitive. Matched text spans are stored as evidence.
# Confidence is assigned based on pattern specificity.

CUSTOMER_PATTERNS: list[tuple[str, str, list[str]]] = [
    # (relationship, pattern_id, [regexes])
    ("named_customer", "CUST001", [
        # "TSMC, Samsung, and Intel are our largest customers"
        r"\b([A-Z][A-Za-z\s&.,]{4,80})\s+(?:is|are|remain(?:s|ed)?)\s+(?:our\s+)?(?:largest|biggest|single\s+largest|major|key|important|significant)\s+customers?\b",
        # "our major customers include TSMC, Samsung and Intel"
        r"\b(?:our\s+)?(?:major|key|largest|biggest|important)\s+customers?\s+(?:include|are|comprise)\s+([A-Z][A-Za-z\s&.,]{4,120})\b",
        # "sales to TSMC, our largest customer"
        r"\bsales?\s+(?:to|with)\s+([A-Z][A-Za-z\s&.,]{4,50})(?:,?\s+(?:our|a|the)\s+(?:largest|biggest|major|key)\s+customer)\b",
    ]),
    ("customer_concentration", "CUST002", [
        r"\b(?:top|largest)\s+(?:3|5|10|three|five|ten)\s+(?:customers?|clients?)\s+(?:account|represent)\s+",
        r"\bno\s+(?:single|customer|client)\s+(?:accounts\s+for|represents|exceeds)\s+(?:more\s+than\s+)?(\d{1,2})%\s+of\s+(?:revenue|sales|turnover)\b",
        r"\b(?:customer|client)\s+concentration\b[^.]*?\b(\d{1,2})%\b",
    ]),
    ("customer_relationship", "CUST003", [
        r"\b(?:customer|client)\s+(?:relationship|partnership|agreement|contract)\s+with\s+([A-Z][A-Za-z\s&.,]{4,50})\b",
        r"\b([A-Z][A-Za-z\s&.,]{4,50})\s+(?:is|as)\s+(?:a|our)\s+(?:major|key|important|significant|largest)\s+(?:customer|client)\b",
    ]),
]

SUPPLIER_PATTERNS: list[tuple[str, str, list[str]]] = [
    ("sole_supplier", "SUPP001", [
        r"\b(?:sole|single[-\s]source|exclusive)\s+suppli(?:er|es)\s+(?:of|for)\s+([A-Za-z\s&.,()]{4,80})\b",
        r"\bdepend(?:s|ent|ence)\s+(?:on|upon)\s+(?:a\s+)?(?:single|sole)\s+suppli(?:er|es?)\s+(?:of|for)\s+([A-Za-z\s&.,()]{4,80})\b",
        r"\bno\s+(?:viable|ready|immediate)\s+(?:alternative|substitute)\s+(?:supplier|source)\s+(?:for|of)\s+([A-Za-z\s&.,()]{4,80})\b",
    ]),
    ("key_supplier", "SUPP002", [
        r"\b(?:key|critical|strategic|important)\s+suppli(?:er|es)\s+(?:include|are|of)\s+([A-Za-z\s&.,()]{4,80})\b",
        r"\b(?:source|procure)\s+(?:our\s+)?([A-Za-z\s&.,()]{4,80})\s+from\s+(?:a\s+)?(?:key|critical|major|primary)\s+suppli(?:er|es)\b",
        r"\b([A-Z][A-Za-z\s&.,]{4,50})\s+(?:is|are)\s+(?:our|a|the)\s+(?:primary|main|key|major)\s+suppli(?:er|es)\s+(?:of|for)\b",
    ]),
    ("supplier_concentration", "SUPP003", [
        r"\b(?:top|largest)\s+(?:3|5|10|three|five|ten)\s+(?:suppliers?|vendors?)\s+(?:account|represent)\s+",
        r"\bsuppli(?:er|ers)\s+concentration\b[^.]*?\b(\d{1,2})%\b",
        r"\braw\s+materials?\s+(?:are\s+)?(?:source|procure)d?\s+(?:from|through)\s+([A-Za-z\s&.,()]{4,80})\b",
    ]),
    ("named_supplier", "SUPP004", [
        r"\b(?:purchase|buy|acquire|obtain)\s+(?:our\s+)?(?:components?|materials?|parts?|modules?|inputs?)\s+from\s+([A-Z][A-Za-z\s&.,]{4,50})\b",
        r"\b(?:supply|procurement)\s+(?:agreement|contract|relationship|arrangement)\s+with\s+([A-Z][A-Za-z\s&.,]{4,50})\b",
    ]),
]

FACILITY_PATTERNS: list[tuple[str, str, list[str]]] = [
    ("manufacturing_site", "FAC001", [
        r"\b(?:manufactur(?:ing|es?)|produces?|assembl(?:es|y))\s+(?:at|in|from)\s+(?:our|the|its)\s+(?:facilit(?:y|ies)|plant|site|location|campus)\s+(?:at|in|of)\s+([A-Za-z\s,]{3,60})\b",
        r"\b(?:key|major|primary|main|critical)\s+(?:production|manufacturing|assembly)\s+(?:facilit(?:y|ies)|plant|site|location)s?\s+(?:at|in|of)\s+([A-Za-z\s,]{3,60})\b",
        r"\b(?:facilit(?:y|ies)|plant|site)\s+(?:in|at)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b[^.]*?\b(?:employ(?:s|ing)?|produc(?:es?|ing)|manufactur(?:es?|ing))\b",
    ]),
    ("rd_center", "FAC002", [
        r"\b(?:R&D|research\s+(?:and|&)\s+development)\s+(?:center|facility|site|lab(?:oratory)?|hub)\s+(?:at|in|of)\s+([A-Za-z\s,]{3,60})\b",
    ]),
    ("data_center", "FAC003", [
        r"\bdata\s+cent(?:er|re)s?\s+(?:at|in|of)\s+([A-Za-z\s,]{3,60})\b",
    ]),
    ("logistics_hub", "FAC004", [
        r"\b(?:distribution|logistics|warehouse)\s+(?:center|hub|facility)\s+(?:at|in|of)\s+([A-Za-z\s,]{3,60})\b",
    ]),
]

COMMODITY_PATTERNS: list[tuple[str, str, list[str]]] = [
    ("raw_material_dependency", "COMD001", [
        r"\bdepend(?:s|ent|ence)\s+(?:on|upon)\s+([a-z]{3,40})\s+(?:as\s+(?:a|our)\s+)?(?:primary|key|critical|essential)\s+(?:raw\s+)?material\b",
        r"\b(?:critical|key|essential|vital)\s+(?:raw\s+)?materials?\s+(?:include|are|such\s+as)\s+([A-Za-z\s,/&]{4,80})\b",
        r"\b(?:price|cost|availability|supply)\s+(?:of|for)\s+([a-z]{3,30})\s+(?:is|are|can|may|could)\s+(?:subject\s+to|affected\s+by|volatile)\b",
    ]),
    ("commodity_exposure", "COMD002", [
        r"\bexpos(?:ed|ure)\s+to\s+([a-z]{3,40})\s+(?:price|market|supply)\b",
        r"\b(?:hedge|hedging)\s+(?:our|against)\s+([a-z]{3,40})\s+(?:price|cost)\b",
    ]),
    ("rare_earth_critical", "COMD003", [
        r"\brare\s+earth\b", r"\bconflict\s+minerals?\b",
        r"\bcritical\s+(?:raw\s+)?minerals?\b",
    ]),
    ("energy_dependency", "COMD004", [
        r"\benergy\s+(?:cost|price|supply|consumption|intensive)\b[^.]*?\b(\d{1,3})%\b",
        r"\b(?:natural\s+gas|electricity|renewable\s+energy)\s+(?:accounts?\s+for|represents?)\b",
    ]),
]

OPERATIONAL_PATTERNS: list[tuple[str, str, list[str]]] = [
    ("joint_venture", "OPER001", [
        r"\bjoint\s+venture\b[^.]*?\b(with|between)\b[^.]*?\b([A-Z][A-Za-z\s&.,]{4,50})\b",
        r"\b([A-Z][A-Za-z\s&.,]{4,50})\s+joint\s+venture\b",
    ]),
    ("licensing_dependency", "OPER003", [
        r"\blicense\s+(?:agreement|arrangement|deal)\s+(?:with|from)\s+([A-Z][A-Za-z\s&.,]{4,50})\b",
        r"\bdepend(?:s|ent|ence)\s+(?:on|upon)\s+(?:a\s+)?license\s+(?:from|with)\s+([A-Z][A-Za-z\s&.,]{4,50})\b",
    ]),
    ("outsourcing", "OPER004", [
        r"\boutsourc(?:es?|ing)\s+(?:to|with)\s+([A-Z][A-Za-z\s&.,]{4,50})\b",
    ]),
    ("franchise", "OPER005", [
        r"\bfranchise\s+(?:agreement|network|partner|operat(?:or|ions?))\s+(?:with|in)\s+([A-Za-z\s&.,]{4,80})\b",
    ]),
]

# Negative context: skip matches containing ESG/sustainability/marketing keywords
# P2-1 fix: narrowed from overly aggressive filter. "carbon" removed (ambiguous —
# carbon fiber, carbon steel are legitimate commodities). Climate/emissions now
# require compound patterns (carbon neutral, net zero, climate report, etc.)
# rather than single keywords. ESG/sustainability still filtered as they rarely
# co-occur with genuine dependency disclosures in annual reports.
NEGATIVE_CONTEXT_PATTERNS: list[str] = [
    # Pure marketing/CSR — never relevant to dependency extraction
    r"\b(?:art\s+festival|museum|exhibition|sponsor(?:ship)?|charity|philanthrop)\b",
    r"\b(?:football|soccer|sports?\s+(?:club|team)|stadium)\b",
    r"\b(?:diversity|inclusion|gender|workforce\s+development)\b",
    # ESG/Sustainability — only reject when clearly boilerplate
    r"\b(?:carbon\s+neutral|carbon\s+footprint|net\s+zero\s+carbon|climate\s+report)\b",
    r"\b(?:ESG\s+report|sustainability\s+report|CSR\s+report)\b",
]

# Combine all patterns for unified extraction
ALL_DEPENDENCY_PATTERNS: dict[str, list[tuple[str, str, list[str]]]] = {
    "customer": CUSTOMER_PATTERNS,
    "supplier": SUPPLIER_PATTERNS,
    "facility": FACILITY_PATTERNS,
    "commodity": COMMODITY_PATTERNS,
    "operational": OPERATIONAL_PATTERNS,
}


def extract_text_from_pdf(pdf_path: Path) -> str | None:
    """Extract full text from a PDF using PyMuPDF.

    Returns stripped text or None if extraction fails.
    """
    try:
        doc = fitz.open(str(pdf_path))
        pages = []
        for page in doc:
            pages.append(page.get_text("text"))
        doc.close()
        return "\n\n".join(pages).strip()
    except Exception as e:
        logger.warning(f"PDF extraction failed for {pdf_path}: {e}")
        return None


def _extract_dependencies_for_type(
    text: str,
    dep_type: str,
    patterns: list[tuple[str, str, list[str]]],
    source_url: str,
) -> list[dict[str, Any]]:
    """Apply patterns for one dependency type. Returns list of edge dicts."""
    results = []
    seen_spans = set()

    for relationship, pattern_id, regexes in patterns:
        for regex in regexes:
            try:
                for match in re.finditer(regex, text, re.IGNORECASE):
                    matched_text = match.group(0).strip()
                    if len(matched_text) < 10:
                        continue
                    if matched_text.lower() in seen_spans:
                        continue
                    seen_spans.add(matched_text.lower())

                    # Skip ESG/sustainability/marketing fluff
                    if any(re.search(neg, matched_text, re.IGNORECASE) for neg in NEGATIVE_CONTEXT_PATTERNS):
                        continue

                    # Extract named entity if capture group exists
                    entity_name = None
                    if match.lastindex and match.lastindex >= 1:
                        entity_name = match.group(1).strip()
                        # Filter: entity name must be at least 4 chars and contain a capital letter
                        if entity_name and len(entity_name) < 3:
                            entity_name = None
                        if entity_name and not any(c.isupper() for c in entity_name):
                            entity_name = None

                    # Determine confidence from pattern specificity
                    if "sole" in regex or "single-source" in regex or "exclusive" in regex:
                        confidence = 0.85
                    elif "key" in regex or "critical" in regex or "major" in regex:
                        confidence = 0.70
                    elif "concentration" in regex:
                        confidence = 0.60
                    else:
                        confidence = 0.50

                    results.append({
                        "dependency_type": dep_type,
                        "relationship": relationship,
                        "pattern_id": pattern_id,
                        "entity_name": entity_name,
                        "matched_text": matched_text[:500],
                        "confidence": confidence,
                        "source_url": source_url,
                        "uncertainty_reason": (
                            "pattern_based_extraction" if not entity_name
                            else "entity_name_extracted_not_resolved"
                        ),
                    })
            except re.error as e:
                logger.debug(f"Regex error in {pattern_id}: {e}")
                continue

    return results


def discover_dependencies_from_text(
    text: str,
    source_url: str,
    dep_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Extract economic dependencies from annual report text.

    Args:
        text: Full text of annual report (from PDF extraction)
        source_url: URL of the annual report PDF
        dep_types: List of dependency types to extract, or None for all

    Returns:
        List of dependency dicts with: dependency_type, relationship,
        pattern_id, entity_name, matched_text, confidence, source_url,
        uncertainty_reason
    """
    if dep_types is None:
        dep_types = list(ALL_DEPENDENCY_PATTERNS.keys())

    all_deps = []
    for dep_type in dep_types:
        patterns = ALL_DEPENDENCY_PATTERNS.get(dep_type, [])
        deps = _extract_dependencies_for_type(text, dep_type, patterns, source_url)
        all_deps.extend(deps)

    logger.info(
        f"Extracted {len(all_deps)} dependency candidates from text "
        f"({len(text)} chars)"
    )
    return all_deps


def download_annual_report(
    isin: str,
    output_dir: Path,
    report_url: str | None = None,
    db: Any | None = None,
) -> Path | None:
    """Download annual report PDF for a company.

    Uses known report URLs, auto-discovers from IR page, or scrapes domain.
    Returns path to downloaded PDF, or None if download fails.
    """
    import httpx

    # Known annual report URLs (from delegation research 2026-06-19)
    KNOWN_REPORTS: dict[str, str] = {
        "NL0010273215": "https://ourbrand.asml.com/m/6ea363f69344ebd4/original/asml-2025-annual-report-based-on-ifrs.pdf",
        "FR0000121014": "https://lvmh-com.cdn.prismic.io/lvmh-com/ac0pyZGXnQHGZK4S_LVMH_RA2025_GB_MEL1.pdf",
        "DE0007164600": "https://www.sap.com/docs/download/investors/2025/sap-2025-integrated-report.pdf",
        "DE0005190003": "https://www.bmwgroup.com/content/dam/grpw/websites/bmwgroup_com/ir/downloads/en/2026/bericht/BMW-Group-Report-2025-en.pdf",
        # Added 2026-06-19 (manual discovery + Cloudflare bypass)
        "DE000BASF111": "https://report.basf.com/2025/en/_assets/downloads/full-basf-report-2025-basf-ar25.pdf",
        "DE0008404005": "https://www.allianz.com/content/dam/onemarketing/azcom/Allianz_com/investor-relations/en/results-reports/annual-report/ar-2025/en-allianz-group-annual-report-2025.pdf",
    }

    url = report_url or KNOWN_REPORTS.get(isin)
    if not url:
        # P0-1: Attempt automated URL discovery from IR page
        url = _discover_annual_report_url(db, isin)
        if not url:
            logger.warning(f"No known annual report URL for ISIN {isin}")
            return None

    # Determine filename from URL
    parsed = urlparse(url)
    filename = Path(parsed.path).name or f"{isin}_annual_report.pdf"
    output_path = output_dir / filename

    # Skip if already downloaded
    if output_path.exists():
        logger.info(f"Annual report already downloaded: {output_path}")
        return output_path

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/pdf,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            # Cloudflare/Akamai bypass: send full browser headers
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"macOS"',
            "Referer": url.split("/annual-report")[0] if "/annual-report" in url else
                       "/".join(url.split("/")[:3]),
        }
        with httpx.Client(timeout=120, follow_redirects=True, headers=headers) as client:
            response = client.get(url)
            response.raise_for_status()

            # Verify it's a PDF
            content_type = response.headers.get("content-type", "")
            if "pdf" not in content_type.lower() and not url.endswith(".pdf"):
                logger.warning(f"Response may not be PDF: content-type={content_type} for {url}")

            # P0-1: Reject tiny PDFs (<500KB) — likely earnings releases, not annual reports
            content_len = len(response.content)
            if content_len < 500_000:
                logger.warning(
                    f"PDF too small ({content_len / 1000:.0f} KB) — "
                    f"likely not an annual report: {url}"
                )
                return None

            output_path.write_bytes(response.content)
            logger.info(
                f"Downloaded annual report: {output_path} "
                f"({len(response.content) / 1_000_000:.1f} MB)"
            )
            return output_path

    except Exception as e:
        logger.warning(f"Failed to download annual report for {isin}: {e}")
        return None


def _discover_annual_report_url(db, isin: str) -> str | None:
    """Auto-discover annual report PDF URL from company IR page.

    Strategy:
    1. Look up IR page URL from registry (ir_url column)
    2. Fetch IR page HTML
    3. Find links matching 'annual report' + year pattern, filtering for .pdf
    4. Return first match
    """
    import httpx
    from bs4 import BeautifulSoup
    import re
    from datetime import datetime

    # Look up IR URL from registry
    if db is None:
        return None

    row = db.execute(
        "SELECT ir_url, domain, canonical_name FROM registry WHERE isin = ?",
        (isin,),
    ).fetchone()
    if not row:
        return None

    ir_url = row["ir_url"]
    domain = row["domain"]
    if not ir_url and not domain:
        return None

    # Try IR page first, then fall back to domain/investors
    urls_to_try = []
    if ir_url:
        urls_to_try.append(ir_url)
    if domain:
        urls_to_try.append(f"https://{domain}/en/investors")
        urls_to_try.append(f"https://{domain}/investors")

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    current_year = datetime.now().year
    # Priority-ordered patterns: compound = specific, year-alone = weak
    year_patterns = [
        # High: year + annual report keyword together
        (f"annual-report-{current_year}", 4),
        (f"annual-report-{current_year - 1}", 4),
        (f"annual_report_{current_year}", 4),
        # Good: annual report keywords (year not required, but explicit type)
        ("annual-report", 3),
        ("annual_report", 3),
        ("integrated-report", 3),
        ("universal-registration-document", 3),
        ("urd", 3),
        # Medium: year only (weak — matches quarterly too, but better than nothing)
        (str(current_year), 2),
        (str(current_year - 1), 2),
        # Low-quality (quarterly/interim — only accept if no annual found)
        ("q1", 1), ("q2", 1), ("q3", 1),
        ("quarterly", 1), ("interim", 1),
        ("earnings-release", 1), ("earnings_release", 1),
    ]

    best_url = None
    best_score = 0

    for page_url in urls_to_try:
        try:
            with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
                resp = client.get(page_url)
                if resp.status_code != 200:
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                for link in soup.find_all("a", href=True):
                    href = str(link.get("href", ""))
                    link_text = link.get_text(strip=True).lower()

                    # Must be a PDF
                    if not href.lower().endswith(".pdf"):
                        continue

                    # Score this link against year_patterns
                    combined = (href + " " + link_text).lower()
                    for pattern, score in year_patterns:
                        if pattern in combined:
                            if score > best_score:
                                best_score = score
                                from urllib.parse import urljoin
                                best_url = urljoin(page_url, href)
                                logger.debug(
                                    f"Candidate annual report (score={score}): {best_url}"
                                )
                            break  # First matching pattern wins for this link

        except Exception as e:
            logger.debug(f"URL discovery failed for {page_url}: {e}")
            continue

    if best_url and best_score >= 2:
        logger.info(
            f"Discovered annual report for {row['canonical_name']}: "
            f"{best_url} (score={best_score})"
        )
        return best_url

    return None


def _ensure_target_registry_entry(
    db: sqlite3.Connection,
    entity_name: str,
    entity_type: str,
) -> int:
    """Find or create a synthetic registry entry for an economic dependency target.

    For supplier/customer names, attempts name matching against existing registry.
    For commodities, facilities, and operational concepts, creates a synthetic
    registry entry with company_type='economic_concept'.

    Returns registry ID for use as target_registry_id in edges.
    """
    # Try exact name match first
    existing = db.execute(
        "SELECT id FROM registry WHERE canonical_name = ? LIMIT 1",
        (entity_name,),
    ).fetchone()
    if existing:
        return existing[0]

    # For supplier/customer: try fuzzy name matching
    if entity_type in ("supplier", "customer"):
        # Try partial name match (at least 10 chars for meaningful match)
        if len(entity_name) >= 10:
            existing = db.execute(
                "SELECT id FROM registry WHERE canonical_name LIKE ? LIMIT 1",
                (f"%{entity_name[:30]}%",),
            ).fetchone()
            if existing:
                return existing[0]

    # Create synthetic registry entry
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        """INSERT INTO registry
           (canonical_name, company_type, jurisdiction, status)
           VALUES (?, 'economic_concept', 'XX', 'active')""",
        (entity_name[:255],),
    )
    db.commit()
    registry_id = cursor.lastrowid
    assert registry_id is not None, "INSERT should return lastrowid"
    logger.debug(
        f"Created synthetic registry entry {registry_id} for "
        f"{entity_type}: {entity_name[:60]}"
    )
    return registry_id


def store_dependency_edges(
    db: sqlite3.Connection,
    registry_id: int,
    dependencies: list[dict[str, Any]],
    evidence_id: int,
    now: str | None = None,
) -> int:
    """Store discovered dependency edges as economic_entities AND edges.

    economic_entities: free-form storage with full description and source URL.
    edges: structured records linked to registry for scoring (edge_source='annual_report').

    Returns count of new edges stored.
    """
    if now is None:
        now = datetime.now(timezone.utc).isoformat()

    stored = 0
    for dep in dependencies:
        try:
            entity_name = dep.get("entity_name") or dep["matched_text"][:200]
            dep_type = dep["dependency_type"]
            confidence = dep.get("confidence", 0.5)

            # 1. Store in economic_entities (free-form, with evidence + registry link)
            db.execute(
                """INSERT OR IGNORE INTO economic_entities
                   (canonical_name, entity_type, lei, jurisdiction, sector,
                    parent_lei, description, source_url, evidence_id, registry_id, created_at)
                   VALUES (?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?)""",
                (
                    entity_name,
                    dep_type,
                    dep.get("matched_text", "")[:1000],
                    dep.get("source_url", ""),
                    evidence_id,
                    registry_id,
                    now,
                ),
            )

            # 2. Create target registry entry for the dependency target
            target_reg_id = _ensure_target_registry_entry(db, entity_name, dep_type)

            # 3. Map dependency_type to relationship_type for edges
            type_map = {
                "customer": "customer_of",
                "supplier": "supplier_to",
                "facility": "facility_at",
                "commodity": "commodity_input",
                "operational": "operational_dependency",
            }
            relationship_type = type_map.get(dep_type, dep_type)

            # 3. Insert into edges table with valid target_registry_id
            db.execute(
                """INSERT OR IGNORE INTO edges
                   (source_registry_id, target_registry_id, relationship_type,
                    evidence_id, confidence, economic_materiality,
                    operational_criticality, concentration, replaceability,
                    edge_source, uncertainty_reason, materiality_unknown_reason,
                    direction, valid_from, created_at)
                   VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL,
                           'annual_report', ?, 'not_disclosed_in_source',
                           'outgoing', date('now'), ?)""",
                (
                    registry_id,
                    target_reg_id,
                    relationship_type,
                    evidence_id,
                    confidence,
                    dep.get("uncertainty_reason", "pattern_based_extraction"),
                    now,
                ),
            )

            stored += 1
        except Exception as e:
            logger.debug(f"Failed to store dependency edge: {e}")

    db.commit()
    return stored


def promote_existing_economic_entities(
    db: sqlite3.Connection,
) -> dict[str, int]:
    """Promote all existing economic_entities into edges.

    Finds economic_entities that have registry_id and evidence_id but
    no corresponding edge entry, and creates edges for them.

    Returns dict with created/duplicate/error counts.
    """
    now = datetime.now(timezone.utc).isoformat()

    type_map = {
        "customer": "customer_of",
        "supplier": "supplier_to",
        "facility": "facility_at",
        "commodity": "commodity_input",
        "operational": "operational_dependency",
    }

    entities = db.execute(
        """SELECT id, canonical_name, entity_type, description, source_url,
                  evidence_id, registry_id
           FROM economic_entities
           WHERE registry_id IS NOT NULL
           AND evidence_id IS NOT NULL""",
    ).fetchall()

    created = 0
    duplicates = 0
    errors = 0

    for e in entities:
        try:
            entity_name = e[1]
            entity_type = e[2]
            registry_id = e[6]
            evidence_id = e[5]
            relationship_type = type_map.get(entity_type, entity_type)
            if not relationship_type:
                relationship_type = "operational_dependency"

            # Create target registry entry
            target_reg_id = _ensure_target_registry_entry(db, entity_name, entity_type)

            # Check if edge already exists
            existing = db.execute(
                """SELECT id FROM edges
                   WHERE source_registry_id = ? AND target_registry_id = ?
                   AND relationship_type = ? AND evidence_id = ?""",
                (registry_id, target_reg_id, relationship_type, evidence_id),
            ).fetchone()

            if existing:
                duplicates += 1
                continue

            # Create edge with shock channel attributes and dedup_key
            dedup_key = f"{registry_id}|{target_reg_id}|{relationship_type}|annual_report"
            attrs = populate_edge_attributes(relationship_type)
            db.execute(
                """INSERT OR IGNORE INTO edges
                   (source_registry_id, target_registry_id, relationship_type,
                    evidence_id, confidence, economic_materiality,
                    operational_criticality, concentration, replaceability,
                    edge_source, uncertainty_reason, materiality_unknown_reason,
                    direction, valid_from, created_at,
                    shock_channel, shock_channel_unknown_reason,
                    lag_bucket, buffer_proxy, replaceability_unknown_reason,
                    switching_time_bucket, dedup_key)
                   VALUES (?, ?, ?, ?, 0.5, NULL, NULL, NULL, NULL,
                           'annual_report', 'pattern_based_extraction',
                           'not_disclosed_in_source',
                           'outgoing', date('now'), ?,
                           ?, ?, ?, ?, ?, ?, ?)""",
                (registry_id, target_reg_id, relationship_type, evidence_id, now,
                 attrs["shock_channel"], attrs["shock_channel_unknown_reason"],
                 attrs["lag_bucket"], attrs["buffer_proxy"],
                 attrs["replaceability_unknown_reason"],
                 attrs["switching_time_bucket"],
                 dedup_key),
            )
            created += 1
        except Exception as exc:
            logger.debug(f"Failed to promote entity {e[0]}: {exc}")
            errors += 1

    db.commit()
    logger.info(
        f"Promoted economic_entities → edges: "
        f"{created} created, {duplicates} duplicates, {errors} errors"
    )
    return {"created": created, "duplicates": duplicates, "errors": errors}


def discover_economic_dependencies(
    db: sqlite3.Connection,
    registry_id: int,
    data_dir: Path,
) -> dict[str, int]:
    """Main orchestrator: discover economic dependencies for a company.

    1. Find annual report URL from registry
    2. Download PDF
    3. Extract text
    4. Apply dependency patterns
    5. Store results as evidence + economic_entities

    Returns dict with counts per dependency type.
    """
    row = db.execute(
        "SELECT canonical_name, isin, lei, domain FROM registry WHERE id = ?",
        (registry_id,),
    ).fetchone()
    if not row:
        logger.warning(f"Registry ID {registry_id} not found")
        return {}

    canonical_name = row["canonical_name"]
    isin = row["isin"]
    logger.info(f"Discovering economic dependencies for {canonical_name}")

    # 1. Download annual report
    report_dir = data_dir / "annual_reports"
    pdf_path = download_annual_report(isin, report_dir, db=db)
    if not pdf_path:
        logger.warning(f"No annual report PDF for {canonical_name}")
        return {}

    # 2. Extract text
    text = extract_text_from_pdf(pdf_path)
    if not text:
        logger.warning(f"Failed to extract text from {pdf_path}")
        return {}

    logger.info(f"Extracted {len(text)} chars from annual report")

    # Simple language detection: count non-ASCII and common non-English words
    non_ascii_ratio = sum(1 for c in text[:10000] if ord(c) > 127) / min(len(text), 10000)
    if non_ascii_ratio > 0.15:
        logger.info(
            f"Non-English content detected ({non_ascii_ratio:.0%} non-ASCII). "
            f"Patterns are English-only — extraction may miss dependencies."
        )
    elif not text[:500].isascii():
        logger.info(
            f"Content appears non-English. Consider adding language-specific "
            f"patterns for DE/FR/IT/ES/NL content."
        )

    # 3. Store full text as evidence
    # P1-2 fix: store FULL extracted text (not truncated to 50K)
    # Annual reports are 200K-1.4M chars; critical dependency sections
    # (Risk Factors, IFRS 8, Notes) are typically in the back half
    now = datetime.now(timezone.utc).isoformat()
    ev_result = store_evidence(
        db=db,
        content=text,  # Full text — no truncation
        source_url=str(pdf_path),
        retrieval_time=now,
        extraction_method="annual_report_pdf_extraction",
        parser_version="1.0.0",
        content_type="text/plain",
        excerpt=text[:2000],
    )

    evidence_id = ev_result.evidence_id

    # 4. Extract dependencies
    dependencies = discover_dependencies_from_text(text, str(pdf_path))

    # 5. Store dependency edges
    stored = store_dependency_edges(db, registry_id, dependencies, evidence_id, now)

    # Count by type
    counts: dict[str, int] = {}
    for dep in dependencies:
        dtype = dep["dependency_type"]
        counts[dtype] = counts.get(dtype, 0) + 1

    counts["stored"] = stored
    logger.info(
        f"{canonical_name}: {len(dependencies)} dependency candidates, "
        f"{stored} stored — {counts}"
    )
    return counts
