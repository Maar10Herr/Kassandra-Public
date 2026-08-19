"""Spain BORME (Boletín Oficial del Registro Mercantil) RSS feed source adapter.

Monitors the official BOE BORME RSS feed for Spanish commercial register
announcements: liquidations, insolvencies (concurso), dissolutions, capital
reductions, transformations, mergers, and other adverse corporate events.

Uses the official RSS feed — no authentication required, free access.
Feed URL: https://www.boe.es/rss/borme.php

Design:
- Free public RSS feed — no API key needed
- Fetches feed ONCE, filters locally for each company (fetch-once-filter-locally)
- Conservative token-based matching with Spanish legal suffix stripping
- Stores text content evidence with source_reliability=1.0
- Classifies notices based on Spanish legal keywords
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

from kassandra.contracts import CollectionMetrics, MatchResult
from kassandra.evidence import store_evidence, store_event
from kassandra.sources.entity_resolution import exact_legal_name_match, queue_unconfirmed_match, discovery_overlap

logger = logging.getLogger(__name__)

BORME_RSS_FEED = "https://www.boe.es/rss/borme.php"

# Spanish legal-form suffix tokens to strip before matching
SPANISH_LEGAL_SUFFIX_TOKENS: set[str] = {
    "sa", "sl", "sll", "slu", "sc", "sco", "sca", "aie", "uta", "sal",
    "sdad", "sociedad", "limitada", "anónima", "anonima", "slne", "ai", "cb",
    "de", "del", "la", "las", "los", "el", "en", "y", "e", "con",
    "the", "and", "of", "ltd", "limited", "plc", "bv", "nv", "gmbh", "ag", "se",
}


def _normalise_tokens(text: str) -> list[str]:
    """Tokenise a company/BORME string and strip legal-form filler words."""
    return [
        token
        for token in re.findall(r"[a-z0-9áéíóúüñ]+", text.lower())
        if token not in SPANISH_LEGAL_SUFFIX_TOKENS and len(token) > 1
    ]


def _notice_matches_company(company_name: str, title: str, description: str = "") -> bool:
    """Return True only for distinctive company-name matches.

    Conservative: strips Spanish legal-form tokens and requires either:
    - all distinctive company tokens to be present when there are 2+ tokens; or
    - one distinctive single token (length >= 4) to appear as a whole word.
    """
    company_tokens = _normalise_tokens(company_name)
    if not company_tokens:
        return False

    notice_tokens = set(_normalise_tokens(f"{title} {description}"))
    if len(company_tokens) == 1:
        token = company_tokens[0]
        # Hyphenated brands: collapse and match collapsed form
        if "-" in company_name:
            collapsed_company = "".join(
                re.findall(r"[a-z0-9áéíóúüñ]+", company_name.lower())
            )
            collapsed_notice = "".join(
                re.findall(r"[a-z0-9áéíóúüñ]+", f"{title} {description}".lower())
            )
            return len(collapsed_company) >= 4 and collapsed_company in collapsed_notice
        # P0 fix: single-token non-hyphenated: require collapsed full-company
        # substring in collapsed notice, not just token-in-set.
        # Prevents false matches like TECH SA (->"techsa") matching
        # "SCHNEIDER ELECTRIC TECH S.L." (collapsed: "schneiderelectrictechsl").
        collapsed_company = "".join(
            re.findall(r"[a-z0-9áéíóúüñ]+", company_name.lower())
        )
        collapsed_notice = "".join(
            re.findall(r"[a-z0-9áéíóúüñ]+", f"{title} {description}".lower())
        )
        if len(collapsed_company) >= 4:
            return collapsed_company in collapsed_notice
        return False

    # Require all distinctive words
    return set(company_tokens).issubset(notice_tokens)


def _classify_borme_notice(title: str, description: str) -> tuple[str | None, str | None]:
    """Classify a BORME notice into event type and severity based on Spanish keywords.

    Mapping:
    - liquidación, concurso, disolución, quiebra → insolvency / critical
    - reducción de capital, transformación, fusión, escisión → restructuring / medium
    - convocatoria de juntas with disolución → restructuring / high (special case)
    - None otherwise
    """
    text = f"{title} {description}".lower()

    # Check for "convocatoria de juntas" with disolución FIRST
    # (before general disolución match, because convocatoria alone is not insolvency)
    if ("convocatoria" in text and "junta" in text and
            any(kw in text for kw in ["disolución", "disolucion"])):
        return "restructuring", "high"

    # Critical: insolvency-related keywords
    if any(kw in text for kw in ["liquidación", "liquidacion",
                                   "concurso",
                                   "disolución", "disolucion",
                                   "quiebra"]):
        return "insolvency", "critical"

    # Other restructuring-related keywords
    if any(kw in text for kw in ["reducción de capital", "reduccion de capital",
                                   "transformación", "transformacion",
                                   "fusión", "fusion",
                                   "escisión", "escision"]):
        return "restructuring", "medium"

    return None, None


# ── Official-ID extraction and entity-resolution matching ─────────────────────


def _extract_official_id(title: str, description: str = "") -> str | None:
    """Extract Spanish CIF/NIF from BORME title/description.

    CIF format: letter + 7-8 digits (e.g., A15075062, B12345678).
    Returns normalized CIF/NIF or None.
    """
    text = f"{title} {description}"
    # Pattern: letter followed by 7-8 digits, often in parentheses
    m = re.search(r"(?:^|[\(\s,])([ABCDEFGHJKLMNPQRSUVW]\d{7,8})(?:[\)\s,]|$)", text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


# Generic tokens that alone are never distinctive
_GENERIC_TOKENS: set[str] = {
    "tech", "services", "group", "holding", "holdings", "international",
    "spain", "espana", "españa", "madrid", "barcelona", "europe",
    "solutions", "systems", "global", "trading", "capital", "management",
    "company", "corporation", "corp", "inc", "llc",
    "electric", "finance", "industrial", "industries", "energy",
}


# ── Known false-positive token patterns ──────────────────────────────────────

# Companies whose name contains only these tokens after stripping legal suffixes
# should never match BORME notices without official-ID corroboration.
_BORME_KNOWN_AMBIGUOUS_TOKENS: set[str] = {
    "hermes", "delphine",
}


def _is_known_ambiguous_match(company_name: str) -> bool:
    """Return True if the company name reduces to a known ambiguous token set."""
    tokens = set(_normalise_tokens(company_name))
    if not tokens:
        return False
    return tokens.issubset(_BORME_KNOWN_AMBIGUOUS_TOKENS)


def _match_notice_to_registry(
    db,
    entry: dict,
    company_name: str,
    registry_id: int,
) -> MatchResult:
    """Resolve a BORME notice against a registry entity.

    Identifier-first policy:
    1. Jurisdiction check: only ES entities
    2. Official ID (CIF/NIF) required if company has one known → confirmed
       If company has CIF/NIF but notice lacks matching one → rejected
    3. Exact normalized name match → confirmed
    4. Multi-token match (2+ distinctive tokens >=4 chars) → unconfirmed_review_candidate
    5. Known-ambiguous names without official ID → rejected
    6. No match → rejected
    """
    title = entry.get("title", "")
    description = entry.get("description", "")

    # ── Jurisdiction check ──
    row = db.execute(
        "SELECT jurisdiction, spanish_tax_id, lei FROM registry WHERE id = ?",
        (registry_id,),
    ).fetchone()
    if not row:
        return MatchResult(
            status="rejected",
            reason="Registry entity not found",
            confidence=0.0,
        )

    jurisdiction = (row["jurisdiction"] or "").upper()
    lei_val = (row["lei"] or "").upper()
    spanish_tax_id = row["spanish_tax_id"]  # may be None

    # Only match Spanish entities against BORME data
    if jurisdiction != "ES":
        return MatchResult(
            status="rejected",
            reason=f"BORME only matches Spanish entities; registry {registry_id} has jurisdiction='{jurisdiction}'",
            confidence=0.0,
        )

    # ── Tier 1: Require official ID if company has known CIF/NIF ──
    official_id = _extract_official_id(title, description)

    if spanish_tax_id:
        # Company has a known CIF/NIF — require it in the notice
        if official_id and official_id.upper() == spanish_tax_id.upper():
            return MatchResult(
                status="confirmed",
                reason=f"CIF/NIF {official_id} from notice matches registry",
                matched_registry_id=registry_id,
                matched_official_id=official_id,
                method="official_id",
                confidence=0.98,
            )
        # Company has CIF/NIF but notice doesn't contain a matching one → reject
        return MatchResult(
            status="rejected",
            reason=f"Company has CIF/NIF {spanish_tax_id} but notice lacks matching CIF/NIF (found: {official_id or 'none'})",
            matched_official_id=official_id,
            confidence=0.0,
        )

    # No known CIF/NIF — try official ID from notice
    if official_id:
        owner = db.execute(
            "SELECT id FROM registry WHERE spanish_tax_id = ?", (official_id,)
        ).fetchone()
        if owner and owner["id"] == registry_id:
            return MatchResult(
                status="confirmed",
                reason=f"CIF/NIF {official_id} from notice matches registry",
                matched_registry_id=registry_id,
                matched_official_id=official_id,
                method="official_id",
                confidence=0.98,
            )
        if owner and owner["id"] != registry_id:
            return MatchResult(
                status="rejected",
                reason=f"CIF/NIF {official_id} identifies a different legal entity",
                matched_official_id=official_id,
                confidence=0.0,
            )

    # ── Tier 2: exact normalized legal-name equality ──
    if exact_legal_name_match(company_name, title, SPANISH_LEGAL_SUFFIX_TOKENS):
        return MatchResult(
            status="confirmed",
            reason="Exact normalized legal name",
            matched_registry_id=registry_id,
            method="exact_name",
            confidence=0.95,
        )

    company_tokens = _normalise_tokens(company_name)
    notice_tokens = _normalise_tokens(f"{title} {description}")
    notice_set = set(notice_tokens)

    if not company_tokens:
        return MatchResult(
            status="rejected",
            reason=f"Company name '{company_name}' has no distinctive tokens after normalization",
            confidence=0.0,
        )

    # ── Tier 3: Known ambiguous name check ──
    if _is_known_ambiguous_match(company_name):
        return MatchResult(
            status="rejected",
            reason=f"Company name '{company_name}' matches known ambiguous token pattern; requires CIF/NIF confirmation",
            match_type="known_ambiguous",
            confidence=0.0,
        )

    # ── Tier 4: Multi-token match (2+ distinctive tokens of length >= 4) ──
    distinctive_company = [t for t in company_tokens if len(t) >= 4]
    matching_distinctive = [t for t in distinctive_company if t in notice_set]

    if len(distinctive_company) >= 2 and len(matching_distinctive) >= 2:
        return MatchResult(
            status="unconfirmed_review_candidate",
            reason=f"Multi-token match ({len(matching_distinctive)}/{len(distinctive_company)} distinctive tokens) but no CIF/NIF or exact name; requires review",
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
        reason=f"Weak token match ({matching}/{len(company_tokens)}); no CIF/NIF, not exact name, insufficient distinctive tokens",
        match_type="weak_overlap",
        confidence=0.15,
    )


# ── Client ────────────────────────────────────────────────────────────────────


class BorMeClient:
    """Client for BORME — the official Spanish commercial announcements bulletin.

    Usage:
        client = BorMeClient()
        entries = client.fetch_feed()
        matches = client.filter_notices_for_company("INDITEX SA", entries)
        count = client.process_notices_for_company(db, registry_id, "INDITEX SA", matches)
    """

    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=30,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "Kassandra/0.1 (+https://github.com/user/kassandra)"
                ),
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            },
        )

    def fetch_feed(self, max_entries: int = 100) -> list[dict[str, Any]]:
        """Fetch recent BORME announcements from the RSS feed.

        Returns list of entry dicts with title, link, description, published.
        """
        entries: list[dict[str, Any]] = []

        try:
            resp = self._client.get(BORME_RSS_FEED)
            resp.raise_for_status()

            feed = feedparser.parse(resp.text)
            if feed.bozo and not feed.entries:
                logger.warning(f"BORME feed parse error: {feed.bozo_exception}")
                return entries

            for entry in feed.entries[:max_entries]:
                entries.append({
                    "title": entry.get("title", ""),
                    "link": entry.get("link", ""),
                    "description": entry.get("description", ""),
                    "published": entry.get("published", ""),
                    "id": entry.get("id", ""),
                })

            logger.info(f"Fetched {len(entries)} BORME notices from feed")
        except httpx.HTTPError as e:
            logger.error(f"BORME feed fetch failed: {e}")

        return entries

    def filter_notices_for_company(
        self, company_name: str, entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Filter pre-fetched feed entries for a specific company (no HTTP)."""
        matches: list[dict[str, Any]] = []
        for entry in entries:
            title = entry.get("title", "")
            description = entry.get("description", "")
            if (_extract_official_id(title, description) or
                    discovery_overlap(company_name, f"{title} {description}", SPANISH_LEGAL_SUFFIX_TOKENS)):
                matches.append(entry)
        return matches

    def process_notices_for_company(
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
            content = (
                f"Title: {notice['title']}\nLink: {notice['link']}\n"
                f"Published: {notice.get('published', '')}\nDescription: {notice.get('description', '')}"
            )
            ev_result = store_evidence(
                db=db, content=content, source_url=notice["link"], retrieval_time=now,
                publication_time=notice.get("published"), extraction_method="borme_es_feed",
                parser_version="1.0.0", content_type="application/rss+xml",
                excerpt=notice["title"][:500], source_reliability=1.0,
            )
            evidence_id = ev_result.evidence_id
            if ev_result.is_new:
                new_evidence += 1
            else:
                duplicates += 1

            if match_result.is_confirmed:
                event_type, severity = _classify_borme_notice(notice["title"], notice.get("description", ""))
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
                    db=db, source_name="borme_es", source_entity_name=notice.get("title", ""),
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
            run_id="", source_name="borme_es", discovered=len(notices),
            fetched=new_evidence + duplicates, new_evidence=new_evidence, duplicates=duplicates,
            candidates=candidates, unconfirmed=unconfirmed, events_created=events_created,
            duplicate_events=duplicate_events,
        )

    def collect_for_company(
        self,
        db: Any,
        registry_id: int,
        company_name: str,
    ) -> CollectionMetrics:
        """Fetch, process, and return reconciled source accounting."""
        entries = self.fetch_feed()
        if not entries:
            return CollectionMetrics(run_id="", source_name="borme_es")

        matches = self.filter_notices_for_company(company_name, entries)
        if not matches:
            return CollectionMetrics(run_id="", source_name="borme_es")

        return self.process_notices_for_company(db, registry_id, company_name, matches)
