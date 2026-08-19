"""UK Gazette insolvency notice source adapter.

Monitors the official UK Gazette for insolvency, winding-up,
administration, and restructuring notices.
Free public RSS feeds — no authentication required.

Sources:
- https://www.thegazette.co.uk/insolvency/feed
- Company-specific notices via search
"""

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

from kassandra.contracts import CollectionMetrics, MatchResult
from kassandra.config import get_config
from kassandra.evidence import store_evidence, store_event
from kassandra.sources.entity_resolution import exact_legal_name_match, queue_unconfirmed_match, discovery_overlap

logger = logging.getLogger(__name__)

GAZETTE_INSOLVENCY_FEED = "https://www.thegazette.co.uk/insolvency/notice/data.xml"
GAZETTE_SEARCH_URL = "https://www.thegazette.co.uk/notice/"


LEGAL_SUFFIX_TOKENS = {
    "ltd", "limited", "plc", "public", "company", "co", "ag", "se", "sa",
    "nv", "bv", "gmbh", "sarl", "spa", "sas", "llc", "inc", "holdings",
    "holding", "group", "the", "and", "of", "uk", "gb", "ireland",
}


def _normalise_tokens(text: str) -> list[str]:
    """Tokenise a Gazette/company string and remove legal-form filler words."""
    return [
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in LEGAL_SUFFIX_TOKENS and len(token) > 1
    ]


def _notice_matches_company(company_name: str, title: str, summary: str = "") -> bool:
    """Return True only for distinctive company-name matches.

    The Gazette insolvency feed contains many companies with generic words like
    "LIMITED". Matching on legal suffix overlap caused false alerts for every
    UK subsidiary. We therefore strip legal-form tokens and require either:
    - all distinctive company tokens to be present when there are 2+ tokens; or
    - one distinctive single token (length >= 4) to appear as a whole word.
    """
    company_tokens = _normalise_tokens(company_name)
    if not company_tokens:
        return False

    notice_tokens = set(_normalise_tokens(f"{title} {summary}"))
    if len(company_tokens) == 1:
        token = company_tokens[0]
        # Hyphenated brands like T-SYSTEMS should match the full collapsed brand
        # (tsystems), not any notice containing the generic word "systems".
        if "-" in company_name:
            collapsed_company = "".join(re.findall(r"[a-z0-9]+", company_name.lower()))
            collapsed_notice = "".join(re.findall(r"[a-z0-9]+", f"{title} {summary}".lower()))
            return len(collapsed_company) >= 4 and collapsed_company in collapsed_notice
        # P0 fix: single-token non-hyphenated: require collapsed full-company
        # substring in collapsed notice, not just token-in-set.
        # Prevents false matches like K B TECH LIMITED (->"kbtechlimited")
        # matching CORENET TECH (UK) PVT LTD (collapsed: "corenettechukpvtltd").
        collapsed_company = "".join(re.findall(r"[a-z0-9]+", company_name.lower()))
        collapsed_notice = "".join(re.findall(r"[a-z0-9]+", f"{title} {summary}".lower()))
        if len(collapsed_company) >= 4:
            return collapsed_company in collapsed_notice
        return False

    # Require all distinctive words. This is deliberately conservative because
    # false positives are more damaging than missed Gazette feed matches; daily
    # broader web monitoring still provides a second chance.
    return set(company_tokens).issubset(notice_tokens)

# ── Official-ID extraction and entity-resolution matching ─────────────────────


def _extract_official_id(title: str, summary: str = "") -> str | None:
    """Extract UK Company Number from Gazette notice.

    Company numbers are 6-8 digit strings, often in parenthesized form.
    Returns normalized number string or None.
    """
    text = f"{title} {summary}"
    # Pattern: "(01234567)" or "company number 01234567" or "company 01234567"
    m = re.search(r"(?:^|[\(\s,])(\d{6,8})(?:[\)\s,]|$)", text)
    if m:
        digits = m.group(1)
        # Validate typical UK company number range (not random 6-8 digit numbers)
        if len(digits) >= 6:
            return digits
    # Alternative: "company number: 01234567"
    m = re.search(r"company\s+(?:number|no)[:\s#]*(\d{6,8})", text, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


# Generic tokens that alone are never distinctive for Gazette context
_GAZETTE_GENERIC_TOKENS: set[str] = {
    "tech", "services", "group", "holding", "holdings", "international",
    "uk", "gb", "ireland", "scotland", "england", "wales",
    "solutions", "systems", "global", "trading", "capital", "management",
    "company", "corp", "inc", "llc", "limited", "ltd", "plc",
    "services", "property", "properties", "investments", "enterprises",
    "associates", "partners", "consulting", "development",
}


def _match_notice_to_registry(
    db,
    notice: dict,
    company_name: str,
    registry_id: int,
) -> MatchResult:
    """Resolve a Gazette notice against a registry entity.

    Identifier-first policy:
    1. Jurisdiction check: only GB/UK entities
    2. Official ID (Companies House number) required if company has one known → confirmed
       If company has CH number but notice lacks matching one → rejected
    3. Exact normalized name match → confirmed
    4. Multi-token match (2+ distinctive tokens >=4 chars) → unconfirmed_review_candidate
    5. No match → rejected
    """
    title = notice.get("title", "")
    summary = notice.get("summary", "")

    # ── Jurisdiction check ──
    row = db.execute(
        "SELECT jurisdiction, companies_house_number, lei FROM registry WHERE id = ?",
        (registry_id,),
    ).fetchone()
    if not row:
        return MatchResult(
            status="rejected",
            reason="Registry entity not found",
            confidence=0.0,
        )

    jurisdiction = (row["jurisdiction"] or "").upper()
    ch_number = row["companies_house_number"]  # may be None

    # Only match UK entities against Gazette data
    uk_jurisdictions = {"GB", "UK", "ENGLAND-WALES", "ENGLAND", "WALES", "SCOTLAND", "NORTHERN-IRELAND", "NI"}
    if jurisdiction not in uk_jurisdictions:
        return MatchResult(
            status="rejected",
            reason=f"Gazette only matches UK entities; registry {registry_id} has jurisdiction='{jurisdiction}'",
            confidence=0.0,
        )

    # ── Tier 1: Require official ID if company has known CH number ──
    official_id = _extract_official_id(title, summary)

    if ch_number:
        # Company has a known Companies House number — require it in the notice
        if official_id and str(official_id) == str(ch_number):
            return MatchResult(
                status="confirmed",
                reason=f"Company number {official_id} from Gazette matches registry",
                matched_registry_id=registry_id,
                matched_official_id=str(official_id),
                method="official_id",
                confidence=0.98,
            )
        # Company has CH number but notice doesn't contain a matching one → reject
        return MatchResult(
            status="rejected",
            reason=f"Company has CH number {ch_number} but notice lacks matching CH number (found: {official_id or 'none'})",
            matched_official_id=str(official_id) if official_id else None,
            confidence=0.0,
        )

    # No known CH number — try official ID from notice
    if official_id:
        owner = db.execute(
            "SELECT id FROM registry WHERE companies_house_number = ?", (official_id,)
        ).fetchone()
        if owner and owner["id"] == registry_id:
            return MatchResult(
                status="confirmed",
                reason=f"Company number {official_id} from Gazette matches registry",
                matched_registry_id=registry_id,
                matched_official_id=str(official_id),
                method="official_id",
                confidence=0.98,
            )
        if owner and owner["id"] != registry_id:
            return MatchResult(
                status="rejected",
                reason=f"Company number {official_id} identifies a different legal entity",
                matched_official_id=str(official_id),
                confidence=0.0,
            )

    # ── Tier 2: exact normalized legal-name equality ──
    if exact_legal_name_match(company_name, title, LEGAL_SUFFIX_TOKENS):
        return MatchResult(
            status="confirmed",
            reason="Exact normalized legal name",
            matched_registry_id=registry_id,
            method="exact_name",
            confidence=0.95,
        )

    company_tokens = _normalise_tokens(company_name)
    notice_tokens = _normalise_tokens(f"{title} {summary}")
    notice_set = set(notice_tokens)

    if not company_tokens:
        return MatchResult(
            status="rejected",
            reason=f"Company name '{company_name}' has no distinctive tokens after normalization",
            confidence=0.0,
        )

    # ── Tier 3: Multi-token match (2+ distinctive tokens of length >= 4) ──
    distinctive_company = [t for t in company_tokens if len(t) >= 4]
    matching_distinctive = [t for t in distinctive_company if t in notice_set]

    if len(distinctive_company) >= 2 and len(matching_distinctive) >= 2:
        return MatchResult(
            status="unconfirmed_review_candidate",
            reason=f"Multi-token match ({len(matching_distinctive)}/{len(distinctive_company)} distinctive tokens) but no CH number or exact name; requires review",
            match_type="multi_token_no_id",
            confidence=0.35,
        )

    # ── Fallthrough: insufficient evidence ──
    matching = sum(1 for t in company_tokens if t in notice_set)
    if matching == 0:
        return MatchResult(
            status="rejected",
            reason=f"No distinctive token overlap",
            confidence=0.0,
        )

    return MatchResult(
        status="unconfirmed_review_candidate",
        reason=f"Weak token match ({matching}/{len(company_tokens)}); no CH number, not exact name, insufficient distinctive tokens",
        match_type="weak_overlap",
        confidence=0.15,
    )



# Patterns to classify Gazette notices
INSOLVENCY_PATTERNS = {
    "insolvency": [
        r"winding.?up", r"liquidation", r"insolven",
        r"administration\s+order", r"creditors.*meeting",
        r"petition.*wind", r"compulsory.*liquidat",
    ],
    "restructuring": [
        r"administration(?!\s+order)", r"voluntary\s+arrangement",
        r"cva\b", r"moratorium", r"restructuring",
    ],
    "management_departure": [
        r"resignation.*director", r"removal.*director",
        r"appointment.*liquidator", r"appointment.*administrator",
    ],
}


class GazetteClient:
    """UK Gazette insolvency notice monitor."""

    def __init__(self) -> None:
        self._client = httpx.Client(timeout=30)
        self.last_error: Exception | None = None

    def fetch_insolvency_feed(self, max_entries: int = 50) -> list[dict[str, Any]]:
        """Fetch recent insolvency notices from the Gazette feed."""
        notices: list[dict] = []
        self.last_error = None

        try:
            resp = self._client.get(GAZETTE_INSOLVENCY_FEED)
            resp.raise_for_status()

            feed = feedparser.parse(resp.text)
            if feed.bozo and not feed.entries:
                self.last_error = ValueError(f"Gazette feed parse error: {feed.bozo_exception}")
                logger.warning(str(self.last_error))
                return notices

            for entry in feed.entries[:max_entries]:
                notices.append({
                    "title": entry.get("title", ""),
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                    "summary": entry.get("summary", ""),
                    "id": entry.get("id", ""),
                })

            logger.info(f"Fetched {len(notices)} Gazette insolvency notices")
        except httpx.HTTPError as e:
            self.last_error = e
            logger.error(f"Gazette feed fetch failed: {e}")

        return notices

    def classify_notice(self, title: str, summary: str) -> tuple[str | None, str | None]:
        """Classify a Gazette notice into event type and severity."""
        text = f"{title} {summary}".lower()

        for event_type, patterns in INSOLVENCY_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, text):
                    severity_map = {
                        "insolvency": "critical",
                        "restructuring": "high",
                        "management_departure": "low",
                    }
                    return event_type, severity_map.get(event_type, "medium")

        # Default: if it's in the insolvency feed, it's relevant
        if any(w in text for w in ["notice", "gazette", "company"]):
            return "unconfirmed_adverse", "low"

        return None, None

    def search_company(self, company_name: str) -> list[dict[str, Any]]:
        """Search Gazette notices for a specific company name.

        Uses the Gazette's search URL. Limited to basic name matching.
        """
        # Gazette doesn't have a simple API search; we filter feed entries
        notices = self.fetch_insolvency_feed(max_entries=50)
        matches = []
        for notice in notices:
            title = notice.get("title", "")
            summary = notice.get("summary", "")
            if (_extract_official_id(title, summary) or
                    discovery_overlap(company_name, f"{title} {summary}", LEGAL_SUFFIX_TOKENS)):
                matches.append(notice)

        return matches

    def collect_for_company(
        self,
        db: Any,
        registry_id: int,
        company_name: str,
    ) -> CollectionMetrics:
        """Collect Gazette notices and return reconciled source accounting."""
        notices = self.search_company(company_name)
        return self._process_notices_for_company(db, registry_id, company_name, notices)

    def collect_for_company_from_notices(
        self,
        db: Any,
        registry_id: int,
        company_name: str,
        notices: list[dict[str, Any]],
    ) -> CollectionMetrics:
        """Collect from pre-fetched notices — no additional HTTP calls."""
        filtered = self._filter_notices_for_company(company_name, notices)
        return self._process_notices_for_company(db, registry_id, company_name, filtered)

    def _filter_notices_for_company(
        self, company_name: str, notices: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Filter pre-fetched notices for a specific company (no HTTP)."""
        matches = []
        for notice in notices:
            title = notice.get("title", "")
            summary = notice.get("summary", "")
            if (_extract_official_id(title, summary) or
                    discovery_overlap(company_name, f"{title} {summary}", LEGAL_SUFFIX_TOKENS)):
                matches.append(notice)
        return matches

    def _process_notices_for_company(
        self,
        db: Any,
        registry_id: int,
        company_name: str,
        notices: list[dict[str, Any]],
    ) -> CollectionMetrics:
        """Store notices and return reconciled evidence/event outcomes."""
        new_evidence = duplicates = candidates = unconfirmed = 0
        events_created = duplicate_events = 0
        now = datetime.now(timezone.utc).isoformat()

        for notice in notices:
            match_result = _match_notice_to_registry(db, notice, company_name, registry_id)
            if match_result.is_rejected:
                continue
            candidates += 1
            content = f"Title: {notice['title']}\nLink: {notice['link']}\nPublished: {notice['published']}\nSummary: {notice['summary']}"
            ev_result = store_evidence(
                db=db, content=content, source_url=notice["link"], retrieval_time=now,
                publication_time=notice.get("published"), extraction_method="uk_gazette_feed",
                parser_version="1.0.0", content_type="application/rss+xml",
                excerpt=notice["title"][:500], source_reliability=1.0,
            )
            evidence_id = ev_result.evidence_id
            if ev_result.is_new:
                new_evidence += 1
            else:
                duplicates += 1

            if match_result.is_confirmed:
                event_type, severity = self.classify_notice(notice["title"], notice.get("summary", ""))
                if event_type:
                    evt_result = store_event(
                        db=db, evidence_id=evidence_id, registry_id=registry_id,
                        event_type=event_type, severity=severity, confidence=0.9,
                        description=notice["title"][:500], source_claims_directly=True,
                    )
                    if evt_result.status == "inserted":
                        events_created += 1
                    elif evt_result.status == "duplicate":
                        duplicate_events += 1
            elif match_result.is_unconfirmed_review_candidate:
                # Count only newly queued work, not every rediscovery.
                queued = queue_unconfirmed_match(
                    db=db, source_name="uk_gazette", source_entity_name=notice.get("title", ""),
                    candidate_registry_id=registry_id, candidate_registry_name=company_name,
                    match_type=match_result.match_type, match_confidence=match_result.confidence,
                    evidence_id=evidence_id, evidence_excerpt=notice.get("title", ""),
                    reason=match_result.reason,
                )
                unconfirmed += int(queued)
            else:
                raise AssertionError(
                    f"Unexpected MatchResult.status={match_result.status!r} "
                    f"for notice '{notice.get('title', '')}'"
                )

        return CollectionMetrics(
            run_id="", source_name="uk_gazette", discovered=len(notices),
            fetched=new_evidence + duplicates, new_evidence=new_evidence, duplicates=duplicates,
            candidates=candidates, unconfirmed=unconfirmed, events_created=events_created,
            duplicate_events=duplicate_events,
        )
