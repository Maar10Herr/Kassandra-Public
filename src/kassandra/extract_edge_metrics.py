"""Extract concentration and replaceability figures from annual report evidence.

Reads annual report full-text evidence from the content-addressed disk store,
applies regex patterns to find concentration percentages and dependency
indicators, and updates economic dependency edges with concentration and
replaceability scores on a 0.0-1.0 scale.
"""

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from kassandra.config import get_config
from kassandra.evidence import get_evidence_path

logger = logging.getLogger(__name__)

# ── Concentration extraction patterns ──────────────────────────────────────
# Patterns match common annual report disclosure language about
# customer/supplier concentration and dependency criticality.

REVENUE_CONCENTRATION_PATTERNS: list[tuple[str, float]] = [
    # "top N customers account for X% of revenue"
    (
        r"\b(?:our\s+)?(?:top|largest)\s+(\d{1,2})\s+(?:customers?|clients?)\s+"
        r"(?:account|represent)(?:s|ed)?\s+(?:for\s+)?(?:approximately\s+)?"
        r"(\d{1,3}(?:\.\d)?)\s*%\s*(?:of\s+)?(?:our\s+)?(?:revenue|sales|turnover)",
        1.0,
    ),
    # "N main customers represented respectively X%, Y% and Z%"
    (
        r"\b(?:our\s+)?(\d{1,2})\s+(?:main|largest|biggest|major|key)\s+"
        r"(?:customers?|clients?)\s+(?:represent|account)(?:ed|s)?\s+"
        r"(?:for\s+)?(?:respectively\s+)?(?:approximately\s+)?"
        r"(\d{1,3}(?:\.\d)?)\s*%",
        1.0,
    ),
    # "largest customer represents X% of revenue"
    (
        r"\b(?:our\s+)?(?:largest|biggest|single\s+largest|biggest)\s+"
        r"(?:customer|client)\s+(?:account|represent)(?:s|ed)?\s+"
        r"(?:for\s+)?(?:approximately\s+)?(\d{1,3}(?:\.\d)?)\s*%\s*(?:of\s+)?"
        r"(?:our\s+)?(?:revenue|sales|turnover)",
        1.0,
    ),
    # Generic: "one customer accounts for X% of revenue"
    (
        r"\b(?:a|one|single)\s+(?:single\s+)?(?:customer|client)\s+"
        r"(?:account|represent)(?:s|ed)?\s+(?:for\s+)?"
        r"(?:approximately\s+)?(?:more\s+than\s+)?(\d{1,3}(?:\.\d)?)\s*%\s*"
        r"(?:of\s+)?(?:our\s+)?(?:revenue|sales|turnover)",
        0.9,
    ),
    # "no single customer accounts for more than X% of revenue"
    (
        r"\bno\s+(?:single\s+)?(?:customer|client)\s+(?:accounts?|represents?)\s+"
        r"(?:for\s+)?(?:more\s+than\s+)?(\d{1,3}(?:\.\d)?)\s*%\s*"
        r"(?:of\s+)?(?:our\s+)?(?:revenue|sales|turnover)",
        0.8,
    ),
    # "customer concentration: no customer exceeds X%"
    (
        r"\b(?:customer|client)\s+concentration\b[^.]*?"
        r"\b(?:no\s+(?:single\s+)?(?:customer|client)\s+(?:exceeds?|accounts?\s+for\s+more\s+than))?"
        r"(\d{1,3}(?:\.\d)?)\s*%",
        0.7,
    ),
]

PROCUREMENT_CONCENTRATION_PATTERNS: list[tuple[str, float]] = [
    # "top N suppliers account for X% of procurement"
    (
        r"\b(?:our\s+)?(?:top|largest)\s+(\d{1,2})\s+(?:suppliers?|vendors?)\s+"
        r"(?:account|represent)(?:s|ed)?\s+(?:for\s+)?(?:approximately\s+)?"
        r"(\d{1,3}(?:\.\d)?)\s*%\s*(?:of\s+)?(?:our\s+)?(?:procurement|purchases|spend|supply)",
        1.0,
    ),
    # Generic: "X% of procurement" (but NOT "procurement associates/team/staff")
    (
        r"(\d{1,3}(?:\.\d)?)\s*%\s*(?:of\s+)?(?:our\s+)?(?:total\s+)?"
        r"(?:procurement|purchases|spend)(?:\s+(?:costs?|budget|volume|expenditure))"
        r"(?!\s+(?:associate|team|staff|professional|department|function|organization))",
        0.7,
    ),
    # "supplier concentration: X%"
    (
        r"\bsuppli(?:er|ers)\s+concentration\b[^.]*?(\d{1,3}(?:\.\d)?)\s*%",
        0.8,
    ),
]

SOLE_SUPPLIER_PATTERNS: list[str] = [
    r"\bsole\s+(?:supplier|source|provider)\b",
    r"\b(?:our|the|a)\s+(?:sole|exclusive|only)\s+(?:supplier|source|provider)\s+(?:of|for)\b",
    r"\bdepend(?:s|ent|ence)\s+(?:on|upon)\s+(?:a\s+)?(?:single|sole)\s+suppli",
    r"\bno\s+(?:viable|ready|immediate|practical)\s+(?:alternative|substitute|replacement)\s+(?:supplier|source|provider)\b",
]

SINGLE_SOURCE_PATTERNS: list[str] = [
    r"\bsingle[- ]sourc(?:e|ed|ing)\b",
    r"\bsingle[- ]suppli(?:er|ed)\b",
    r"\blimited\s+number\s+of\s+(?:critical\s+)?suppliers?\s+of\s+single",
]

# Generic percentage mentions that indicate concentration
# ── Operational criticality extraction patterns ────────────────────────────
# Patterns match common annual report disclosure language about
# operational dependency criticality. Each pattern maps to a criticality score
# on 0.0–1.0 scale and optionally a set of relationship types it applies to.

CRITICALITY_PATTERNS: list[tuple[str, float, set[str] | None]] = [
    # "sole source" → 0.95 (extremely critical — no alternatives)
    (
        r"\bsole\s+(?:source|provider|supplier)\b",
        0.95,
        {"supplier_to", "commodity_input"},
    ),
    # "critical supplier" → 0.9
    (
        r"\bcritical\s+(?:supplier|vendor|provider)\b",
        0.9,
        {"supplier_to", "commodity_input"},
    ),
    # "essential to (our/the) operations" → 0.9
    (
        r"\bessential\s+(?:to|for)\s+(?:(?:our|the)\s+)?operations?\b",
        0.9,
        None,  # applies to all types
    ),
    # "critical to (our/the) operations"
    (
        r"\bcritical\s+(?:to|for)\s+(?:(?:our|the)\s+)?operations?\b",
        0.85,
        None,
    ),
    # "material(ly) to/for/affect (our/the) business" → 0.8
    (
        r"\bmaterial(?:ly)?\s+(?:to|for|affect)\s+(?:(?:our|the)\s+)?business\b",
        0.8,
        None,
    ),
    # "key supplier" or "key dependency" → 0.7
    (
        r"\bkey\s+(?:supplier|vendor|dependency|dependencies)\b",
        0.7,
        {"supplier_to", "commodity_input", "operational_dependency"},
    ),
    # "key customer" → 0.6
    (
        r"\bkey\s+(?:customer|client)\b",
        0.6,
        {"customer_of"},
    ),
    # "strategic partnership" → 0.5
    (
        r"\bstrategic\s+(?:partnership|alliance|relationship|partner)\b",
        0.5,
        None,
    ),
    # "important supplier" → 0.5
    (
        r"\bimportant\s+(?:supplier|vendor)\b",
        0.5,
        {"supplier_to", "commodity_input"},
    ),
    # "major supplier" → 0.45
    (
        r"\bmajor\s+(?:supplier|vendor)\b",
        0.45,
        {"supplier_to", "commodity_input"},
    ),
    # "significant supplier" → 0.4
    (
        r"\bsignificant\s+(?:supplier|vendor)\b",
        0.4,
        {"supplier_to", "commodity_input"},
    ),
    # "dependent on" or "dependence on" → 0.75
    (
        r"\b(?:heavily\s+)?depend(?:ent|ence)\s+(?:on|upon)\b",
        0.75,
        None,
    ),
    # "no alternative" → 0.9
    (
        r"\bno\s+(?:viable|ready|immediate|practical)\s+alternative\b",
        0.9,
        None,
    ),
    # "irreplaceable" → 0.95
    (
        r"\birreplaceable\b",
        0.95,
        None,
    ),
    # "mission-critical" → 0.9
    (
        r"\bmission[- ]?critical\b",
        0.9,
        None,
    ),
    # "business-critical" → 0.85
    (
        r"\bbusiness[- ]?critical\b",
        0.85,
        None,
    ),
]

# Default criticality when no indicators are found
DEFAULT_CRITICALITY = 0.3

# ── Economic materiality extraction patterns ──────────────────────────────
# Patterns match disclosed materiality language in annual reports, ranked by
# confidence. Each tuple is (regex, materiality_score_0_1, exclusion_pattern).
# The exclusion pattern (if set) filters out false positives like
# accounting boilerplate ("not material to financial statements").

MATERIALITY_PATTERNS: list[tuple[str, float, str | None]] = [
    # Explicit percentage disclosures: "represents X% of revenue"
    (
        r"(?:represent|account)(?:s|ed|ing)?\s+(?:for\s+)?(?:approximately\s+)?"
        r"(\d{1,3}(?:\.\d)?)\s*%\s*(?:of\s+)?(?:our\s+)?"
        r"(?:total\s+)?(?:revenue|sales|turnover|procurement|costs?|spend)",
        0.0,  # 0.0 signals "use captured percentage / 100"
        None,
    ),
    # "approximately X% of revenue" near entity
    (
        r"(?:approximately|about|roughly)\s+(\d{1,3}(?:\.\d)?)\s*%\s*"
        r"(?:of\s+)?(?:our\s+)?(?:total\s+)?(?:revenue|sales|turnover)",
        0.0,
        None,
    ),
    # Sole supplier / sole source → 0.95
    (
        r"\bsole\s+(?:supplier|source|provider)\b",
        0.95,
        None,
    ),
    # "core to our operations" → 0.9
    (
        r"\bcore\s+to\s+(?:our|the)\s+operations?\b",
        0.9,
        None,
    ),
    # "essential to (our/the) business" → 0.9
    (
        r"\bessential\s+(?:to|for)\s+(?:our|the)\s+(?:business|operations)\b",
        0.9,
        None,
    ),
    # "mission-critical" → 0.9
    (
        r"\bmission[- ]?critical\b",
        0.9,
        None,
    ),
    # "critical to (our/the) (business|operations|supply chain)" → 0.85
    (
        r"\bcritical\s+(?:to|for)\s+(?:our|the)\s+"
        r"(?:business|operations|supply\s+chain)\b",
        0.85,
        None,
    ),
    # "material(ly) (to|for|affect|impact) (our/the) business" → 0.8
    (
        r"\bmaterial(?:ly)?\s+(?:to|for|affect|impact(?:ing|s)?)\s+"
        r"(?:our|the)\s+business\b",
        0.8,
        None,
    ),
    # "significant subsidiary" → 0.7
    (
        r"\bsignificant\s+subsidiary\b",
        0.7,
        None,
    ),
    # "key supplier" / "key dependency" → 0.7
    (
        r"\bkey\s+(?:supplier|vendor|dependency|dependencies)\b",
        0.7,
        None,
    ),
    # "key customer" → 0.6
    (
        r"\bkey\s+(?:customer|client)\b",
        0.6,
        None,
    ),
    # "strategic (partner|supplier|relationship)" → 0.5
    (
        r"\bstrategic\s+(?:partner|supplier|relationship|alliance)\b",
        0.5,
        None,
    ),
    # "dependent on" / "dependence on" → 0.75
    (
        r"\b(?:heavily\s+)?depend(?:ent|ence|s)\s+(?:on|upon)\b",
        0.75,
        None,
    ),
    # "no viable alternative" → 0.9
    (
        r"\bno\s+(?:viable|ready|immediate|practical)\s+alternative\b",
        0.9,
        None,
    ),
    # "irreplaceable" → 0.95
    (
        r"\birreplaceable\b",
        0.95,
        None,
    ),
    # "not material" / "immaterial" → 0.05 (with accounting boilerplate filter)
    (
        r"\bnot\s+material\b",
        0.05,
        r"\b(?:financial\s+statements?|pension|interest\s+rate|fair\s+value|"
        r"carrying\s+(?:amount|value)|amorti[sz]|depreciation|goodwill|"
        r"deferred\s+tax|balance\s+sheet|statement\s+of|credit\s+rating)",
    ),
    (
        r"\bimmaterial\b",
        0.05,
        r"\b(?:financial\s+statements?|pension|interest\s+rate|fair\s+value|"
        r"carrying\s+(?:amount|value)|amorti[sz]|depreciation|goodwill|"
        r"deferred\s+tax|balance\s+sheet|statement\s+of|credit\s+rating|"
        r"subsidiary|associate)",
    ),
]

# Words that indicate accounting boilerplate context (suppress "not material")
ACCOUNTING_BOILERPLATE_PATTERN = re.compile(
    r"\b(?:financial\s+statements?|pension|interest\s+rate|fair\s+value|"
    r"carrying\s+(?:amount|value)|amorti[sz]|depreciation|goodwill|"
    r"deferred\s+tax|balance\s+sheet|statement\s+of|credit\s+rating|"
    r"defined\s+benefit|consolidated\s+(?:financial|income)|"
    r"short-term\s+investments?|subsidiary|associate)",
    re.IGNORECASE,
)


def _is_accounting_boilerplate(context: str) -> bool:
    """Check if a matched text is accounting boilerplate (not dependency-related)."""
    return bool(ACCOUNTING_BOILERPLATE_PATTERN.search(context))

# Criticality floor for different relationship types (fallback when no text indicators)
RELATIONSHIP_TYPE_CRITICALITY_FLOOR: dict[str, float] = {
    "facility_at": 0.7,         # physical facilities are inherently critical
    "commodity_input": 0.6,     # raw materials tend to be operationally important
    "supplier_to": 0.5,         # suppliers are generally important
    "customer_of": 0.4,         # customers are important but presumes some diversification
    "operational_dependency": 0.6,  # explicit operational dependencies are important
}


GENERIC_PCT_PATTERNS: list[tuple[str, float]] = [
    # "accounts for X% of" (general)
    (
        r"(?:account|represent)(?:s|ing|ed)?\s+(?:for\s+)?(?:approximately\s+)?"
        r"(\d{1,3}(?:\.\d)?)\s*%\s*(?:of\s+)?",
        0.5,
    ),
]


def _read_evidence_text(db: sqlite3.Connection, evidence_id: int) -> str | None:
    """Read evidence full text from content-addressed disk store."""
    path = get_evidence_path(db, evidence_id)
    if not path or not path.exists():
        logger.warning(f"Evidence path not found for id={evidence_id}")
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"Failed to read evidence {evidence_id}: {e}")
        return None


def _extract_concentration_pct(
    text: str,
    patterns: list[tuple[str, float]],
) -> list[tuple[float, float, str]]:
    """Extract concentration percentages with confidence weights.

    Returns list of (percentage_value_0_100, confidence, matched_pattern) tuples.

    For patterns capturing "top N customers account for X%", the X is divided by N
    to get a per-customer average. For "respectively" patterns (where individual
    percentages are listed), the captured percentage is treated as the max.
    """
    results = []
    for pattern, confidence in patterns:
        try:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                groups = match.groups()
                if not groups:
                    continue

                matched_text = match.group(0)

                # If pattern has two capture groups and first is a number (N)
                if len(groups) >= 2 and groups[0] is not None and groups[1] is not None:
                    try:
                        n_val = int(groups[0])
                        pct = float(groups[1])
                        if n_val > 0 and 0 < pct <= 100:
                            if "respectively" in matched_text.lower():
                                # "N customers represented respectively X%, Y%, Z%"
                                # The captured X is the max individual concentration
                                results.append(
                                    (pct, confidence, matched_text[:200])
                                )
                            else:
                                # "top N customers account for X%" — X is total
                                avg_pct = pct / n_val
                                results.append(
                                    (avg_pct, confidence, matched_text[:200])
                                )
                            continue
                    except (ValueError, TypeError):
                        pass

                # Single percentage group
                for g in groups:
                    if g is None:
                        continue
                    try:
                        pct = float(g)
                        if 0 < pct <= 100:
                            results.append((pct, confidence, matched_text[:200]))
                            break
                    except (ValueError, TypeError):
                        continue
        except re.error as e:
            logger.debug(f"Regex error in pattern: {e}")
            continue

    return results


def _has_pattern(text: str, patterns: list[str]) -> bool:
    """Check if any pattern matches in text."""
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def _extract_criticality_indicators(text: str) -> list[tuple[float, str, set[str] | None]]:
    """Extract operational criticality indicators from annual report text.

    Returns list of (criticality_score, matched_text, applicable_relationship_types)
    tuples sorted by criticality score descending.
    """
    results = []
    for pattern, score, rel_types in CRITICALITY_PATTERNS:
        try:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                matched_text = match.group(0)[:200]
                results.append((score, matched_text, rel_types))
        except re.error as e:
            logger.debug(f"Regex error in criticality pattern: {e}")
            continue

    # Sort by score descending (highest criticality first)
    results.sort(key=lambda x: x[0], reverse=True)
    return results


def _determine_operational_criticality(
    text: str,
    edge_type: str,
    target_name: str,
    criticality_indicators: list[tuple[float, str, set[str] | None]],
) -> float | None:
    """Determine operational criticality for a specific edge.

    Uses evidence-level criticality indicators, edge relationship type,
    and target entity name proximity matching.

    Args:
        text: Full evidence text.
        edge_type: Relationship type (supplier_to, customer_of, etc.)
        target_name: Canonical name of the target entity.
        criticality_indicators: Pre-extracted criticality indicators from text.

    Returns:
        Operational criticality score 0.0-1.0, or None if unknown.
    """
    # Normalize target name for proximity matching
    target_words: set[str] = set()
    if target_name:
        # Extract significant words (3+ chars, not common words)
        common = {"the", "our", "and", "for", "are", "not", "its", "has", "that",
                  "this", "with", "from", "have", "been", "were", "they", "will",
                  "also", "which", "their", "other", "such", "these", "those"}
        for word in re.findall(r"\b\w{3,}\b", target_name.lower()):
            if word not in common:
                target_words.add(word)

    best_score = 0.0
    best_is_nearby = False

    for score, matched_text, rel_types in criticality_indicators:
        # Check if this indicator applies to the edge's relationship type
        if rel_types is not None and edge_type not in rel_types:
            continue

        # Check proximity: is the matched text near target entity name words?
        nearby = False
        if target_words:
            # Find the match position
            for match in re.finditer(re.escape(matched_text[:60]), text, re.IGNORECASE):
                match_start = max(0, match.start() - 300)
                match_end = min(len(text), match.end() + 300)
                context = text[match_start:match_end].lower()
                for word in target_words:
                    if word in context:
                        nearby = True
                        break
                if nearby:
                    break

        # Score is boosted when indicator text is near the target entity name
        effective_score = min(score * 1.1, 1.0) if nearby else score

        if effective_score > best_score:
            best_score = effective_score
            best_is_nearby = nearby

    # If no text indicators matched, use relationship type floor
    if best_score == 0.0:
        floor = RELATIONSHIP_TYPE_CRITICALITY_FLOOR.get(edge_type, DEFAULT_CRITICALITY)
        return floor

    return best_score


def _compute_replaceability(
    concentration: float,
    has_sole_supplier: bool,
    has_single_source: bool,
    edge_type: str,
) -> float:
    """Compute replaceability score (0.0 = impossible to replace, 1.0 = easy).

    Rules:
    - Sole supplier → 0.0 (cannot replace)
    - Single source → 0.1 (very hard to replace)
    - High concentration (>50%) → 0.2
    - Medium concentration (20-50%) → 0.5
    - Low concentration (<20%) → 0.7
    - facility → 0.1 (physical facilities hard to replace quickly)
    - Default → 0.5 (unknown)
    """
    if edge_type in ("facility_at",):
        return 0.1

    if has_sole_supplier:
        return 0.0
    if has_single_source:
        return 0.1

    if concentration >= 0.8:
        return 0.1
    elif concentration >= 0.5:
        return 0.25
    elif concentration >= 0.2:
        return 0.5
    elif concentration > 0:
        return 0.7
    else:
        return 0.5  # unknown default


def _determine_concentration(
    text: str,
    edge_type: str,
    company_metrics: dict[str, Any],
) -> tuple[float | None, float | None]:
    """Determine concentration and replaceability for a specific edge.

    Uses company-level metrics extracted from the full annual report text
    plus edge-type-specific heuristics.
    """
    has_sole = company_metrics.get("has_sole_supplier", False)
    has_single = company_metrics.get("has_single_source", False)

    if edge_type == "customer_of":
        # Use max customer concentration found
        max_cust = company_metrics.get("max_customer_pct", 0)
        avg_cust = company_metrics.get("avg_customer_pct", 0)
        conc = max(max_cust, avg_cust) / 100.0 if max(max_cust, avg_cust) > 0 else None
        if conc is not None:
            conc = min(conc, 1.0)
            rep = _compute_replaceability(conc, False, False, edge_type)
            return conc, rep

    elif edge_type == "supplier_to":
        if has_sole:
            return 1.0, 0.0
        if has_single:
            return 0.8, 0.1

        # Try procurement concentration
        max_proc = company_metrics.get("max_procurement_pct", 0)
        if max_proc > 0:
            conc = min(max_proc / 100.0, 1.0)
            rep = _compute_replaceability(conc, False, has_single, edge_type)
            return conc, rep

    elif edge_type == "commodity_input":
        if has_sole:
            return 1.0, 0.0
        if has_single:
            return 0.8, 0.1

        max_proc = company_metrics.get("max_procurement_pct", 0)
        if max_proc > 0:
            conc = min(max_proc / 100.0, 1.0)
            rep = _compute_replaceability(conc, False, False, edge_type)
            return conc, rep

    elif edge_type == "facility_at":
        # Facilities are highly concentrated by nature
        return 0.9, 0.1

    elif edge_type == "operational_dependency":
        # Generic dependency - use company-level revenue concentration as proxy
        max_cust = company_metrics.get("max_customer_pct", 10)
        conc = min(max_cust / 100.0, 0.5)  # cap at 0.5 for operational
        if conc > 0:
            rep = _compute_replaceability(conc, has_sole, has_single, edge_type)
            return conc, rep

    return None, None


def _extract_company_metrics(text: str) -> dict[str, Any]:
    """Extract company-level concentration metrics from annual report text.

    Returns dict with keys like:
    - max_customer_pct: largest single customer concentration (%)
    - avg_customer_pct: average per-customer concentration when "top N" mentioned
    - max_procurement_pct: largest procurement concentration (%)
    - has_sole_supplier: bool
    - has_single_source: bool
    - has_no_customer_concentration: bool (explicit statement of low concentration)
    """
    metrics: dict[str, Any] = {
        "max_customer_pct": 0.0,
        "avg_customer_pct": 0.0,
        "max_procurement_pct": 0.0,
        "has_sole_supplier": False,
        "has_single_source": False,
        "has_no_customer_concentration": False,
    }

    # Extract revenue concentration
    revenue_matches = _extract_concentration_pct(text, REVENUE_CONCENTRATION_PATTERNS)
    if revenue_matches:
        # max_customer_pct = highest single-customer concentration
        # avg_customer_pct = per-customer average from "top N" patterns
        max_pct = 0.0
        avg_sum = 0.0
        avg_count = 0
        for pct, conf, matched in revenue_matches:
            max_pct = max(max_pct, pct)
            if "top" in matched.lower() or "largest" in matched.lower():
                # These are already per-customer averages from top-N patterns
                pass
            avg_sum += pct
            avg_count += 1
        metrics["max_customer_pct"] = max_pct
        metrics["avg_customer_pct"] = avg_sum / avg_count if avg_count > 0 else 0.0

    # Extract procurement concentration
    proc_matches = _extract_concentration_pct(text, PROCUREMENT_CONCENTRATION_PATTERNS)
    if proc_matches:
        metrics["max_procurement_pct"] = max(m[0] for m in proc_matches)

    # Check for sole supplier / single source
    metrics["has_sole_supplier"] = _has_pattern(text, SOLE_SUPPLIER_PATTERNS)
    metrics["has_single_source"] = _has_pattern(text, SINGLE_SOURCE_PATTERNS)

    # Check for explicit "no customer concentration" statements
    metrics["has_no_customer_concentration"] = bool(
        re.search(
            r"\bno\s+(?:single\s+)?(?:customer|client)\s+(?:exceeds?|accounts?\s+for\s+more\s+than)\s+\d{1,2}\s*%",
            text,
            re.IGNORECASE,
        )
    )

    logger.debug(
        f"Company metrics: max_cust={metrics['max_customer_pct']:.1f}% "
        f"max_proc={metrics['max_procurement_pct']:.1f}% "
        f"sole={metrics['has_sole_supplier']} single={metrics['has_single_source']}"
    )
    return metrics


def extract_edge_metrics(db: sqlite3.Connection, dry_run: bool = False) -> dict[str, int]:
    """Extract concentration and replaceability from annual report evidence
    and update economic dependency edges.

    Only updates edges where concentration is currently NULL (idempotent).

    Args:
        db: SQLite database connection.
        dry_run: If True, report what would be updated without changing data.

    Returns:
        Dict with counts: {'updated': N, 'skipped': N, 'no_text': N, 'total': N}
    """
    config = get_config()

    # Get all annual_report edges with NULL concentration
    edges = db.execute(
        """SELECT e.id, e.source_registry_id, e.target_registry_id,
                  e.relationship_type, e.evidence_id, e.concentration,
                  e.replaceability
           FROM edges e
           WHERE e.edge_source = 'annual_report'
           AND e.concentration IS NULL
           ORDER BY e.evidence_id, e.id"""
    ).fetchall()

    if not edges:
        logger.info("No annual_report edges with NULL concentration found")
        return {"updated": 0, "skipped": 0, "no_text": 0, "total": 0}

    # Group edges by evidence_id for efficient text reading
    ev_to_edges: dict[int, list[sqlite3.Row]] = {}
    for edge in edges:
        ev_id = edge["evidence_id"]
        if ev_id not in ev_to_edges:
            ev_to_edges[ev_id] = []
        ev_to_edges[ev_id].append(edge)

    # Cache for evidence text and company metrics
    text_cache: dict[int, str | None] = {}
    metrics_cache: dict[int, dict[str, Any]] = {}

    stats = {"updated": 0, "skipped": 0, "no_text": 0, "total": len(edges)}

    for ev_id, ev_edges in ev_to_edges.items():
        # Read evidence text (cached)
        if ev_id not in text_cache:
            text = _read_evidence_text(db, ev_id)
            text_cache[ev_id] = text
            if text:
                metrics_cache[ev_id] = _extract_company_metrics(text)
            else:
                metrics_cache[ev_id] = {}
                stats["no_text"] += len(ev_edges)
                continue

        text = text_cache[ev_id]
        if not text:
            stats["no_text"] += len(ev_edges)
            continue

        company_metrics = metrics_cache.get(ev_id, {})

        for edge in ev_edges:
            edge_type = edge["relationship_type"]

            # Check idempotency: skip if already has non-NULL concentration
            if edge["concentration"] is not None:
                stats["skipped"] += 1
                continue

            concentration, replaceability = _determine_concentration(
                text, edge_type, company_metrics
            )

            if concentration is None and replaceability is None:
                stats["skipped"] += 1
                continue

            if dry_run:
                logger.info(
                    f"[DRY RUN] Edge {edge['id']}: "
                    f"type={edge_type} conc={concentration} rep={replaceability}"
                )
                stats["updated"] += 1
                continue

            # Update the edge
            db.execute(
                """UPDATE edges
                   SET concentration = ?, replaceability = ?
                   WHERE id = ?""",
                (concentration, replaceability, edge["id"]),
            )
            stats["updated"] += 1
            logger.debug(
                f"Updated edge {edge['id']}: "
                f"type={edge_type} conc={concentration} rep={replaceability}"
            )

    if not dry_run and stats["updated"] > 0:
        db.commit()

    logger.info(
        f"Edge metrics extraction: {stats['updated']} updated, "
        f"{stats['skipped']} skipped, {stats['no_text']} no text, "
        f"{stats['total']} total"
    )
    return stats


def show_edge_metrics(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Show current concentration/replaceability stats for annual_report edges."""
    rows = db.execute(
        """SELECT relationship_type,
                  COUNT(*) as total,
                  SUM(CASE WHEN concentration IS NOT NULL THEN 1 ELSE 0 END) as has_conc,
                  SUM(CASE WHEN replaceability IS NOT NULL THEN 1 ELSE 0 END) as has_rep,
                  ROUND(AVG(concentration), 3) as avg_conc,
                  ROUND(AVG(replaceability), 3) as avg_rep
           FROM edges
           WHERE edge_source = 'annual_report'
           GROUP BY relationship_type
           ORDER BY total DESC"""
    ).fetchall()

    results = []
    for row in rows:
        results.append({
            "relationship_type": row["relationship_type"],
            "total": row["total"],
            "has_concentration": row["has_conc"],
            "has_replaceability": row["has_rep"],
            "avg_concentration": row["avg_conc"],
            "avg_replaceability": row["avg_rep"],
        })
    return results


def extract_operational_criticality(
    db: sqlite3.Connection, dry_run: bool = False
) -> dict[str, int]:
    """Extract operational criticality from annual report evidence
    and update economic dependency edges.

    Reads annual report full-text evidence, applies regex patterns to find
    criticality indicators (critical supplier, sole source, key dependency,
    etc.), and updates edges with operational_criticality scores on a 0.0-1.0
    scale.

    Only updates edges where operational_criticality is currently NULL
    (idempotent).

    Args:
        db: SQLite database connection.
        dry_run: If True, report what would be updated without changing data.

    Returns:
        Dict with counts: {'updated': N, 'skipped': N, 'no_text': N, 'total': N}
    """
    # Get all annual_report edges with NULL operational_criticality
    # Join with registry to get target entity names for proximity matching
    edges = db.execute(
        """SELECT e.id, e.source_registry_id, e.target_registry_id,
                  e.relationship_type, e.evidence_id, e.operational_criticality,
                  r.canonical_name as target_name
           FROM edges e
           JOIN registry r ON e.target_registry_id = r.id
           WHERE e.edge_source = 'annual_report'
           AND e.operational_criticality IS NULL
           ORDER BY e.evidence_id, e.id"""
    ).fetchall()

    if not edges:
        logger.info(
            "No annual_report edges with NULL operational_criticality found"
        )
        return {"updated": 0, "skipped": 0, "no_text": 0, "total": 0}

    # Group edges by evidence_id for efficient text reading
    ev_to_edges: dict[int, list[sqlite3.Row]] = {}
    for edge in edges:
        ev_id = edge["evidence_id"]
        if ev_id not in ev_to_edges:
            ev_to_edges[ev_id] = []
        ev_to_edges[ev_id].append(edge)

    # Cache for evidence text and criticality indicators
    text_cache: dict[int, str | None] = {}
    indicators_cache: dict[int, list[tuple[float, str, set[str] | None]]] = {}

    stats = {"updated": 0, "skipped": 0, "no_text": 0, "total": len(edges)}

    for ev_id, ev_edges in ev_to_edges.items():
        # Read evidence text (cached)
        if ev_id not in text_cache:
            text = _read_evidence_text(db, ev_id)
            text_cache[ev_id] = text
            if text:
                indicators_cache[ev_id] = _extract_criticality_indicators(text)
            else:
                indicators_cache[ev_id] = []
                stats["no_text"] += len(ev_edges)
                continue

        text = text_cache[ev_id]
        if not text:
            stats["no_text"] += len(ev_edges)
            continue

        criticality_indicators = indicators_cache.get(ev_id, [])

        for edge in ev_edges:
            edge_type = edge["relationship_type"]
            target_name = edge["target_name"] or ""

            # Check idempotency: skip if already has non-NULL operational_criticality
            if edge["operational_criticality"] is not None:
                stats["skipped"] += 1
                continue

            criticality = _determine_operational_criticality(
                text, edge_type, target_name, criticality_indicators
            )

            if criticality is None:
                stats["skipped"] += 1
                continue

            if dry_run:
                logger.info(
                    f"[DRY RUN] Edge {edge['id']}: "
                    f"type={edge_type} criticality={criticality:.2f}"
                )
                stats["updated"] += 1
                continue

            # Update the edge
            db.execute(
                """UPDATE edges
                   SET operational_criticality = ?
                   WHERE id = ?""",
                (criticality, edge["id"]),
            )
            stats["updated"] += 1
            logger.debug(
                f"Updated edge {edge['id']}: "
                f"type={edge_type} criticality={criticality:.2f}"
            )

    if not dry_run and stats["updated"] > 0:
        db.commit()

    logger.info(
        f"Operational criticality extraction: {stats['updated']} updated, "
        f"{stats['skipped']} skipped, {stats['no_text']} no text, "
        f"{stats['total']} total"
    )
    return stats


def show_operational_criticality(
    db: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Show current operational criticality stats for annual_report edges."""
    rows = db.execute(
        """SELECT relationship_type,
                  COUNT(*) as total,
                  SUM(CASE WHEN operational_criticality IS NOT NULL THEN 1 ELSE 0 END) as has_crit,
                  ROUND(AVG(operational_criticality), 3) as avg_crit,
                  ROUND(MIN(operational_criticality), 3) as min_crit,
                  ROUND(MAX(operational_criticality), 3) as max_crit
           FROM edges
           WHERE edge_source = 'annual_report'
           GROUP BY relationship_type
           ORDER BY total DESC"""
    ).fetchall()

    results = []
    for row in rows:
        results.append({
            "relationship_type": row["relationship_type"],
            "total": row["total"],
            "has_criticality": row["has_crit"],
            "avg_criticality": row["avg_crit"],
            "min_criticality": row["min_crit"],
            "max_criticality": row["max_crit"],
        })
    return results


# ── Economic materiality extraction ────────────────────────────────────────


def _find_entity_in_text(text: str, target_name: str) -> int | None:
    """Find the position of a target entity name in the evidence text.

    The target name (from registry.canonical_name) is a text snippet
    extracted from the annual report. We normalize newlines and try
    flexible matching to locate it.

    Returns the character position in text, or None if not found.
    """
    if not target_name or not text:
        return None

    # Normalize: collapse whitespace and newlines
    target_norm = " ".join(target_name.split())

    # Try exact match first
    pos = text.find(target_norm)
    if pos >= 0:
        return pos

    # Try with newlines collapsed in text too
    text_norm = " ".join(text.split())
    pos = text_norm.find(target_norm)
    if pos >= 0:
        return pos

    # Try matching the first ~8 significant words
    sig_words = [w for w in target_norm.split() if len(w) > 3]
    if len(sig_words) >= 4:
        for n_words in range(min(8, len(sig_words)), 3, -1):
            sig = " ".join(sig_words[:n_words])
            if len(sig) > 20:
                pos = text_norm.find(sig)
                if pos >= 0:
                    return pos

    return None


def _extract_materiality_from_context(
    context: str,
) -> float | None:
    """Extract the best materiality score from a context window around an entity.

    Applies MATERIALITY_PATTERNS in priority order (highest confidence first)
    and returns the best matching materiality score. Patterns with score=0.0
    indicate a captured percentage should be used directly.

    Returns None if no disclosed materiality indicator is found.
    """
    best_score: float | None = None
    best_priority = -1  # Lower = better (pattern order)

    for idx, (pattern, base_score, exclusion) in enumerate(MATERIALITY_PATTERNS):
        for match in re.finditer(pattern, context, re.IGNORECASE):
            match_text = match.group(0)

            # Check exclusion filter (for accounting boilerplate)
            if exclusion is not None:
                excl_match = re.search(exclusion, context, re.IGNORECASE)
                if excl_match:
                    continue

            if base_score == 0.0:
                # Percentage-based: use captured percentage / 100
                groups = match.groups()
                if groups and groups[0] is not None:
                    try:
                        pct = float(groups[0])
                        if 0 < pct <= 100:
                            score = pct / 100.0
                            if best_score is None or score > best_score:
                                best_score = score
                                best_priority = idx
                    except (ValueError, TypeError):
                        continue
            else:
                # Fixed score pattern
                if best_score is None or (
                    base_score > best_score and idx <= best_priority
                ):
                    # For "not material"/"immaterial" patterns, extra filtering
                    if base_score == 0.05:
                        if _is_accounting_boilerplate(context):
                            continue
                    best_score = base_score
                    best_priority = idx

    return best_score


def _should_skip_accounting_not_material(context: str) -> bool:
    """Heuristic: skip 'not material' in accounting boilerplate contexts.

    Annual reports routinely say 'X is not material to financial statements'
    for pensions, financial instruments, etc. These are NOT dependency
    materiality signals.
    """
    return _is_accounting_boilerplate(context)


def extract_economic_materiality(
    db: sqlite3.Connection, dry_run: bool = False
) -> dict[str, int]:
    """Extract economic materiality from annual report evidence and update edges.

    For each annual_report edge with NULL economic_materiality, reads the
    associated evidence text, locates the target entity description in the
    text, and searches the surrounding context for disclosed materiality
    indicators (percentages, qualitative statements, etc.).

    Only sets materiality when a genuine disclosed figure or statement is
    found in the evidence. Clears materiality_unknown_reason when updated.

    Args:
        db: SQLite database connection.
        dry_run: If True, report what would be updated without changing data.

    Returns:
        Dict with counts: {'updated': N, 'skipped': N, 'no_text': N, 'total': N}
    """
    # Get all annual_report edges with NULL economic_materiality
    # Join with registry to get target entity names for text location
    edges = db.execute(
        """SELECT e.id, e.source_registry_id, e.target_registry_id,
                  e.relationship_type, e.evidence_id, e.economic_materiality,
                  e.materiality_unknown_reason,
                  r.canonical_name as target_name
           FROM edges e
           JOIN registry r ON e.target_registry_id = r.id
           WHERE e.evidence_id IN (
               SELECT evidence_id FROM economic_entities
               WHERE evidence_id IS NOT NULL AND registry_id IS NOT NULL
           )
           AND e.economic_materiality IS NULL
           ORDER BY e.evidence_id, e.id"""
    ).fetchall()

    if not edges:
        logger.info(
            "No annual_report edges with NULL economic_materiality found"
        )
        return {"updated": 0, "skipped": 0, "no_text": 0, "total": 0}

    # Group edges by evidence_id for efficient text reading
    ev_to_edges: dict[int, list[sqlite3.Row]] = {}
    for edge in edges:
        ev_id = edge["evidence_id"]
        if ev_id not in ev_to_edges:
            ev_to_edges[ev_id] = []
        ev_to_edges[ev_id].append(edge)

    # Cache for evidence text
    text_cache: dict[int, str | None] = {}

    stats = {"updated": 0, "skipped": 0, "no_text": 0, "total": len(edges)}

    for ev_id, ev_edges in ev_to_edges.items():
        # Read evidence text (cached)
        if ev_id not in text_cache:
            text = _read_evidence_text(db, ev_id)
            text_cache[ev_id] = text

        text = text_cache[ev_id]
        if not text:
            stats["no_text"] += len(ev_edges)
            continue

        # Normalize text once for this evidence
        text_norm = " ".join(text.split())

        for edge in ev_edges:
            target_name = edge["target_name"] or ""

            # Find the entity in the evidence text (normalized)
            pos = _find_entity_in_text(text_norm, target_name)

            if pos is None:
                stats["skipped"] += 1
                continue

            # Extract context window around entity (~400 chars total)
            target_norm = " ".join(target_name.split())
            ctx_start = max(0, pos - 200)
            ctx_end = min(len(text_norm), pos + len(target_norm) + 200)
            context = text_norm[ctx_start:ctx_end]

            # Extract materiality from context
            materiality = _extract_materiality_from_context(context)

            if materiality is None:
                stats["skipped"] += 1
                continue

            if dry_run:
                logger.info(
                    f"[DRY RUN] Edge {edge['id']}: "
                    f"type={edge['relationship_type']} "
                    f"materiality={materiality:.2f}"
                )
                stats["updated"] += 1
                continue

            # Update the edge
            db.execute(
                """UPDATE edges
                   SET economic_materiality = ?,
                       materiality_unknown_reason = NULL
                   WHERE id = ?""",
                (materiality, edge["id"]),
            )
            stats["updated"] += 1
            logger.debug(
                f"Updated edge {edge['id']}: "
                f"type={edge['relationship_type']} "
                f"materiality={materiality:.2f}"
            )

    if not dry_run and stats["updated"] > 0:
        db.commit()

    logger.info(
        f"Economic materiality extraction: {stats['updated']} updated, "
        f"{stats['skipped']} skipped, {stats['no_text']} no text, "
        f"{stats['total']} total"
    )
    return stats


def show_economic_materiality(
    db: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Show current economic materiality stats for annual_report edges."""
    rows = db.execute(
        """SELECT relationship_type,
                  COUNT(*) as total,
                  SUM(CASE WHEN economic_materiality IS NOT NULL THEN 1 ELSE 0 END) as has_mat,
                  ROUND(AVG(economic_materiality), 3) as avg_mat,
                  ROUND(MIN(economic_materiality), 3) as min_mat,
                  ROUND(MAX(economic_materiality), 3) as max_mat,
                  SUM(CASE WHEN materiality_unknown_reason IS NULL
                      AND economic_materiality IS NOT NULL THEN 1 ELSE 0 END) as cleared
           FROM edges
           WHERE edge_source = 'annual_report'
           GROUP BY relationship_type
           ORDER BY total DESC"""
    ).fetchall()

    results = []
    for row in rows:
        results.append({
            "relationship_type": row["relationship_type"],
            "total": row["total"],
            "has_materiality": row["has_mat"],
            "avg_materiality": row["avg_mat"],
            "min_materiality": row["min_mat"],
            "max_materiality": row["max_mat"],
            "cleared_unknown_reason": row["cleared"],
        })
    return results
