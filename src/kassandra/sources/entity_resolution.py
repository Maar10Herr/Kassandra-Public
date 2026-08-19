"""Shared strict legal-event identity helpers.

The source adapters may discover candidates, but only this exact-name/official-ID
contract may authorize an event. Queue storage is canonical and migration-owned.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from kassandra.contracts import MatchResult


# Canonical jurisdiction to country mapping
_JURISDICTION_CANONICAL: dict[str, str] = {
    # GB/UK variants
    "england-wales": "GB",
    "england": "GB",
    "wales": "GB",
    "scotland": "GB",
    "northern-ireland": "GB",
    "ni": "GB",
    "je": "GB",
    "gg": "GB",
    # FR variants
    "france": "FR",
    "french": "FR",
    # ES variants
    "spain": "ES",
    "espana": "ES",
    "españa": "ES",
    # DE variants
    "germany": "DE",
    "de": "DE",
}


def normalize_jurisdiction(jurisdiction: str) -> str:
    """Normalize a jurisdiction string to canonical ISO-3166-1 alpha-2 upper-case.

    Returns the upper-cased canonical code. Unknown jurisdictions are returned
    upper-cased as-is so fail-closed logic still works.
    """
    return _JURISDICTION_CANONICAL.get(jurisdiction.lower().strip(), jurisdiction.upper().strip())


def normalize_legal_name(text: str, suffixes: set[str]) -> str:
    """Normalize a legal name for exact equality, never fuzzy containment."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    # Legal abbreviations such as S.A. and S.L.U. are punctuation variants,
    # not extra name tokens.
    folded = folded.replace(".", "")
    tokens = re.findall(r"[a-z0-9]+", folded)
    return " ".join(token for token in tokens if token not in suffixes)


def notice_legal_name(text: str) -> str:
    """Take the legal-name portion before a Gazette/BORME event description."""
    return re.split(r"\s+(?:[-–—:]|\|)\s+", text, maxsplit=1)[0].strip()


def exact_legal_name_match(company_name: str, notice_name: str, suffixes: set[str]) -> bool:
    left = normalize_legal_name(company_name, suffixes)
    right = normalize_legal_name(notice_legal_name(notice_name), suffixes)
    return bool(left) and left == right


def discovery_overlap(company_name: str, notice_text: str, suffixes: set[str]) -> bool:
    """Broad discovery gate; confirmation is deliberately not performed here."""
    company = set(normalize_legal_name(company_name, suffixes).split())
    notice = set(normalize_legal_name(notice_text, suffixes).split())
    return bool(company & notice)


def queue_unconfirmed_match(
    db, *, source_name: str, source_entity_name: str,
    candidate_registry_id: int | None, candidate_registry_name: str | None,
    match_type: str, match_confidence: float, evidence_id: int | None,
    evidence_excerpt: str | None, reason: str,
) -> bool:
    """Insert a pending queue item and return whether a new row was created."""
    try:
        db.execute("SELECT 1 FROM unconfirmed_match_queue LIMIT 1").fetchone()
    except Exception as exc:
        raise RuntimeError(
            "unconfirmed_match_queue is missing; apply canonical database migrations before collecting"
        ) from exc
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        """INSERT OR IGNORE INTO unconfirmed_match_queue
        (source_name, source_entity_name, candidate_registry_id, candidate_registry_name,
         match_type, match_confidence, evidence_id, evidence_excerpt, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (source_name, source_entity_name, candidate_registry_id, candidate_registry_name,
         match_type, match_confidence, evidence_id, (evidence_excerpt or reason)[:2000], now, now),
    )
    db.commit()
    return cursor.rowcount == 1
