"""French BODACC (Bulletin Officiel des Annonces Civiles et Commerciales) source adapter.

Monitors the official BODACC open-data API for French commercial register
announcements: collective proceedings (insolvency), radiations (strike-offs),
and other adverse corporate events.

Uses the official JSON API endpoint — no authentication required, free access.
Endpoint: https://www.bodacc.fr/api/explore/v2.1/catalog/datasets/annonces-commerciales/records

Design:
- Free public REST API — no API key needed
- Queries by date range + publication family + company name prefix
- Families monitored: "Procédures collectives" (insolvency), "Radiations" (strike-off)
- Conservative token-based matching (same spirit as Gazette _normalise_tokens)
- Stores raw JSON evidence with source_reliability=1.0
- Rate-limited (1 req/2s, same pattern as Handelsregister)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from kassandra.contracts import CollectionMetrics, MatchResult
from kassandra.evidence import store_evidence, store_event
from kassandra.sources.entity_resolution import (
    exact_legal_name_match, queue_unconfirmed_match, discovery_overlap,
)

logger = logging.getLogger(__name__)

BODACC_API_BASE = (
    "https://www.bodacc.fr/api/explore/v2.1/catalog/datasets/"
    "annonces-commerciales/records"
)

# Publication families we actively monitor
MONITORED_FAMILIES: dict[str, tuple[str, str]] = {
    "Procédures collectives": ("insolvency", "critical"),
    "Radiations": ("restructuring", "high"),
}

# Rate-limiter state (module-level)
_last_request_time: float = 0.0
_MIN_REQUEST_INTERVAL: float = 2.0  # seconds


def _respect_rate_limit() -> None:
    """Token-bucket rate limiter for BODACC API."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.monotonic()


# ── Token normalisation (same spirit as Gazette) ──────────────────────────────

# French legal-form filler tokens to strip before matching.
# NOTE: Only genuine legal-form / filler tokens.  "international", "services",
# "france", "paris", "europe" were removed (P0 fix) — stripping them collapsed
# multi-word company names to a single generic token, causing false matches.
FRENCH_LEGAL_SUFFIX_TOKENS: set[str] = {
    "sa", "sas", "sasu", "sarl", "eurl", "snc", "scs", "sca", "sci",
    "sep", "gie", "geie", "sem", "seml", "scop", "scic",
    "association", "fondation", "mutuelle", "cooperative", "coopérative",
    "societe", "société", "ets", "etablissements", "établissements",
    "compagnie", "cie", "groupe", "group", "holding", "holdings",
    "the", "and", "of", "de", "du", "des", "le", "la", "les",
    "ltd", "limited", "plc", "bv", "nv", "gmbh", "ag", "se",
}


def _normalise_tokens(text: str) -> list[str]:
    """Tokenise a company/BODACC string and strip legal-form filler words."""
    return [
        token
        for token in re.findall(r"[a-z0-9à-ÿ]+", text.lower())
        if token not in FRENCH_LEGAL_SUFFIX_TOKENS and len(token) > 1
    ]


def _notice_matches_company(company_name: str, commercant: str) -> bool:
    """Return True only for distinctive company-name matches.

    Conservative: requires all distinctive tokens from company_name
    to appear in the BODACC commercant field. Single-token companies
    need token length >= 4.
    """
    company_tokens = _normalise_tokens(company_name)
    if not company_tokens:
        return False

    commercant_tokens = set(_normalise_tokens(commercant))

    if len(company_tokens) == 1:
        token = company_tokens[0]
        # Hyphenated brands: collapse and match collapsed form
        if "-" in company_name:
            collapsed_company = "".join(re.findall(r"[a-z0-9à-ÿ]+", company_name.lower()))
            collapsed_commercant = "".join(re.findall(r"[a-z0-9à-ÿ]+", commercant.lower()))
            return len(collapsed_company) >= 4 and collapsed_company in collapsed_commercant
        # P0 fix: single-token non-hyphenated: require collapsed full-company
        # substring in collapsed commercant, not just token-in-set.
        # Prevents false matches like DELPHINE SAS (→"delphinesas") matching
        # "LIMOUSIN, Delphine" (collapsed: "limousindelphine").
        collapsed_company = "".join(re.findall(r"[a-z0-9à-ÿ]+", company_name.lower()))
        collapsed_commercant = "".join(re.findall(r"[a-z0-9à-ÿ]+", commercant.lower()))
        if len(collapsed_company) >= 4:
            return collapsed_company in collapsed_commercant
        return False

    return set(company_tokens).issubset(commercant_tokens)


def _classify_annonce(famille: str) -> tuple[str | None, str | None]:
    """Map a BODACC publication family to Kassandra event type + severity."""
    return MONITORED_FAMILIES.get(famille, (None, None))


# ── Official-ID extraction and entity-resolution matching ─────────────────────


def _extract_official_id(notice_or_text: dict | str) -> str | None:
    """Extract SIREN from a BODACC notice payload or commercant text.

    Returns normalized 9-digit SIREN or None.
    """
    # If dict, check dedicated 'siren' field first
    if isinstance(notice_or_text, dict):
        siren = (notice_or_text.get("siren") or "").strip()
        if siren and re.match(r"^\d{9}$", siren):
            return siren
        text = notice_or_text.get("commercant", "")
        # Also check 'siret' (first 9 digits)
        siret = (notice_or_text.get("siret") or "").strip()
        if siret and re.match(r"^\d{14}$", siret):
            return siret[:9]
        if not text:
            return None
    else:
        text = str(notice_or_text)

    # Pattern: "RCS PARIS 123 456 789" or "SIREN 123456789"
    m = re.search(r"(?:RCS\s+(?:\w+\s+)?|SIREN\s+)(\d[\d\s]{7,14})", text)
    if m:
        digits = re.sub(r"\s+", "", m.group(1))
        if re.match(r"^\d{9}$", digits):
            return digits
        if len(digits) == 14:
            return digits[:9]
    return None


# ── Known false-positive token patterns ──────────────────────────────────────

# Companies whose name contains only these tokens after stripping legal suffixes
# should never match BODACC notices without official-ID corroboration.
_KNOWN_AMBIGUOUS_COMPANY_TOKENS: set[str] = {
    "hermes", "delphine",
}


def _is_known_ambiguous_match(company_name: str) -> bool:
    """Return True if the company name reduces to a known ambiguous token set."""
    tokens = set(_normalise_tokens(company_name))
    if not tokens:
        return False
    return tokens.issubset(_KNOWN_AMBIGUOUS_COMPANY_TOKENS)


def _match_notice_to_registry(
    db,
    notice: dict,
    company_name: str,
    registry_id: int,
) -> MatchResult:
    """Resolve a BODACC notice against a registry entity.

    Identifier-first policy:
    1. Jurisdiction check: only FR entities (jurisdiction='FR' or LEI starts 'FR')
    2. Official ID (SIREN) required if company has one known → confirmed
       If company has SIREN but notice lacks matching SIREN → rejected
    3. Exact normalized name match → confirmed
    4. Multi-token match (2+ distinctive tokens >=4 chars) → unconfirmed_review_candidate
    5. Known-ambiguous names (hermes, delphine) without official ID → rejected
    6. No match → rejected
    """
    commercant = notice.get("commercant", "")

    # ── Jurisdiction check ──
    row = db.execute(
        "SELECT jurisdiction, siren, lei FROM registry WHERE id = ?",
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
    siren = row["siren"]  # may be None

    # Only match French entities against BODACC data
    if jurisdiction != "FR" and not lei_val.startswith("FR"):
        return MatchResult(
            status="rejected",
            reason=f"BODACC only matches French entities; registry {registry_id} has jurisdiction='{jurisdiction}', lei='{lei_val}'",
            confidence=0.0,
        )

    # ── Tier 1: Require official ID if company has known SIREN ──
    official_id = _extract_official_id(notice)

    if siren:
        # Company has a known SIREN — require it in the notice
        if official_id and official_id == siren:
            return MatchResult(
                status="confirmed",
                reason=f"SIREN {official_id} from notice matches registry",
                matched_registry_id=registry_id,
                matched_official_id=official_id,
                method="official_id",
                confidence=0.98,
            )
        # Company has SIREN but notice doesn't contain a matching one → reject
        return MatchResult(
            status="rejected",
            reason=f"Company has SIREN {siren} but notice lacks matching SIREN (found: {official_id or 'none'})",
            matched_official_id=official_id,
            confidence=0.0,
        )

    # No known SIREN — try official ID from notice
    if official_id:
        owner = db.execute(
            "SELECT id FROM registry WHERE siren = ?", (official_id,)
        ).fetchone()
        if owner and owner["id"] == registry_id:
            return MatchResult(
                status="confirmed",
                reason=f"SIREN {official_id} from notice matches registry",
                matched_registry_id=registry_id,
                matched_official_id=official_id,
                method="official_id",
                confidence=0.98,
            )
        if owner and owner["id"] != registry_id:
            return MatchResult(
                status="rejected",
                reason=f"SIREN {official_id} identifies a different legal entity",
                matched_official_id=official_id,
                confidence=0.0,
            )

    # ── Tier 2: exact normalized legal-name equality ──
    if exact_legal_name_match(company_name, commercant, FRENCH_LEGAL_SUFFIX_TOKENS):
        return MatchResult(
            status="confirmed",
            reason="Exact normalized legal name",
            matched_registry_id=registry_id,
            method="exact_name",
            confidence=0.95,
        )

    company_tokens = _normalise_tokens(company_name)
    commercant_set = set(_normalise_tokens(commercant))

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
            reason=f"Company name '{company_name}' matches known ambiguous token pattern; requires SIREN confirmation",
            match_type="known_ambiguous",
            confidence=0.0,
        )

    # ── Tier 4: Multi-token match (2+ distinctive tokens of length >= 4) ──
    distinctive_company = [t for t in company_tokens if len(t) >= 4]
    matching_distinctive = [t for t in distinctive_company if t in commercant_set]

    if len(distinctive_company) >= 2 and len(matching_distinctive) >= 2:
        # Multi-token match with 2+ distinctive tokens → unconfirmed_review_candidate
        return MatchResult(
            status="unconfirmed_review_candidate",
            reason=f"Multi-token match ({len(matching_distinctive)}/{len(distinctive_company)} distinctive tokens) but no SIREN or exact name; requires review",
            match_type="multi_token_no_id",
            confidence=0.35,
        )

    # ── Fallthrough: insufficient evidence ──
    matching = sum(1 for t in company_tokens if t in commercant_set)
    if matching == 0:
        return MatchResult(
            status="rejected",
            reason=f"No distinctive token overlap between '{company_name}' and '{commercant}'",
            confidence=0.0,
        )

    # Any other partial/weak overlap → unconfirmed_review_candidate (low confidence)
    return MatchResult(
        status="unconfirmed_review_candidate",
        reason=f"Weak token match ({matching}/{len(company_tokens)}); no SIREN, not exact name, insufficient distinctive tokens",
        match_type="weak_overlap",
        confidence=0.15,
    )


# ── Legacy matcher (preserved for backward compatibility) ──────────────────────


class BodaccClient:
    """Client for BODACC — the official French commercial announcements bulletin.

    Usage:
        client = BodaccClient()
        notices = client.fetch_notices(days_back=7, family="Procédures collectives")
        matches = client.filter_notices_for_company("LVMH", notices)
        count = client.process_notices_for_company(db, registry_id, company_name, matches)
    """

    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=10,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "Kassandra/0.1 (+https://github.com/user/kassandra)"
                ),
                "Accept": "application/json",
            },
        )

    def fetch_notices(
        self,
        days_back: int = 30,
        family: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch BODACC announcements for a publication family and date range.

        Args:
            days_back: How many days back to query (default 30).
            family: Publication family filter (e.g. "Procédures collectives").
                    If None, fetches across all monitored families.
            limit: Max records to return (API default is 10, max 100).

        Returns list of notice dicts.
        """
        since = (date.today() - timedelta(days=days_back)).isoformat()

        families: list[str] = [family] if family else list(MONITORED_FAMILIES.keys())
        all_notices: list[dict[str, Any]] = []

        for fam in families:
            params: dict[str, Any] = {
                "where": (
                    f"dateparution >= date'{since}' "
                    f"and familleavis_lib = \"{fam}\""
                ),
                "limit": limit,
                "order_by": "dateparution DESC",
            }
            try:
                _respect_rate_limit()
                resp = self._client.get(BODACC_API_BASE, params=params)
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", [])
                all_notices.extend(results)
                logger.info(
                    f"BODACC fetched {len(results)} notices for family '{fam}' "
                    f"(since {since})"
                )
            except httpx.HTTPError as e:
                logger.error(f"BODACC fetch failed for family '{fam}': {e}")
            except json.JSONDecodeError as e:
                logger.error(f"BODACC response parse failed for family '{fam}': {e}")

        return all_notices

    def filter_notices_for_company(
        self, company_name: str, notices: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Filter pre-fetched notices for a specific company (no HTTP)."""
        matches: list[dict[str, Any]] = []
        for notice in notices:
            commercant = notice.get("commercant", "")
            if (_extract_official_id(notice) or
                    discovery_overlap(company_name, commercant, FRENCH_LEGAL_SUFFIX_TOKENS)):
                matches.append(notice)
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

            source_url = notice.get("url_complete", "") or BODACC_API_BASE
            content = json.dumps(notice, ensure_ascii=False, default=str)
            excerpt_parts = [notice.get("commercant", ""), notice.get("familleavis_lib", ""), notice.get("dateparution", "")]
            excerpt = " | ".join(p for p in excerpt_parts if p)[:500]
            ev_result = store_evidence(
                db=db, content=content, source_url=source_url, retrieval_time=now,
                publication_time=notice.get("dateparution"), extraction_method="bodacc_fr_api",
                parser_version="1.0.0", content_type="application/json", excerpt=excerpt,
                source_reliability=1.0,
            )
            evidence_id = ev_result.evidence_id
            if ev_result.is_new:
                new_evidence += 1
            else:
                duplicates += 1

            if match_result.is_confirmed:
                famille = notice.get("familleavis_lib", "")
                event_type, severity = _classify_annonce(famille)
                if event_type:
                    description = " | ".join([
                        f"BODACC: {notice.get('commercant', '?')}", f"Family: {famille}",
                        f"Date: {notice.get('dateparution', '?')}",
                    ])[:500]
                    evt_result = store_event(
                        db=db, evidence_id=evidence_id, registry_id=registry_id,
                        event_type=event_type, severity=severity, confidence=0.9,
                        description=description, source_claims_directly=True, raw_event_json=content,
                    )
                    if evt_result.status == "inserted":
                        events_created += 1
                    elif evt_result.status == "duplicate":
                        duplicate_events += 1
            elif match_result.is_unconfirmed_review_candidate:
                # Count only newly queued work, not every rediscovery.
                queued = queue_unconfirmed_match(
                    db=db, source_name="bodacc_fr", source_entity_name=notice.get("commercant", ""),
                    candidate_registry_id=registry_id, candidate_registry_name=company_name,
                    match_type=match_result.match_type, match_confidence=match_result.confidence,
                    evidence_id=evidence_id, evidence_excerpt=excerpt, reason=match_result.reason,
                )
                unconfirmed += int(queued)
            else:
                raise AssertionError(
                    f"Unexpected MatchResult.status={match_result.status!r} "
                    f"for notice commerceant='{notice.get('commercant', '')}'"
                )

        return CollectionMetrics(
            run_id="", source_name="bodacc_fr", discovered=len(notices),
            fetched=new_evidence + duplicates, new_evidence=new_evidence, duplicates=duplicates,
            candidates=candidates, unconfirmed=unconfirmed, events_created=events_created,
            duplicate_events=duplicate_events,
        )

    def collect_for_company(
        self,
        db: Any,
        registry_id: int,
        company_name: str,
        days_back: int = 30,
    ) -> CollectionMetrics:
        """Fetch, process, and return reconciled source accounting."""
        notices = self.fetch_notices(days_back=days_back)
        if not notices:
            return CollectionMetrics(run_id="", source_name="bodacc_fr")

        matches = self.filter_notices_for_company(company_name, notices)
        if not matches:
            return CollectionMetrics(run_id="", source_name="bodacc_fr")

        return self.process_notices_for_company(db, registry_id, company_name, matches)
