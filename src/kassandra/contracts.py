"""Shared correctness contracts for Kassandra.

Typed results for evidence/event insertion, canonical CollectionMetrics,
and lifecycle/provenance structures that source adapters and scoring
must adhere to.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any, Literal


# ── Evidence insertion result ─────────────────────────────────────────────────

@dataclass
class EvidenceResult:
    """Typed result from evidence insertion.

    Attributes:
        evidence_id: The evidence row ID (always populated).
        is_new: True if this is a newly inserted row, False if deduplicated.
        content_hash: SHA-256 content hash.
    """

    evidence_id: int
    is_new: bool
    content_hash: str


# ── Event insertion result ────────────────────────────────────────────────────

@dataclass
class EventResult:
    """Typed result from event insertion.

    Attributes:
        event_id: The event row ID, or None if rejected/duplicate.
        status: One of "inserted", "duplicate", "rejected".
        is_new: True only for "inserted".
        registry_id: Optional registry ID the event targets.
        event_type: Optional event type string.
        reject_reason: Reason for rejection (only populated when status="rejected").
    """

    event_id: int | None
    status: str  # "inserted" | "duplicate" | "rejected"
    registry_id: int | None = None
    event_type: str | None = None
    reject_reason: str | None = None

    @property
    def is_new(self) -> bool:
        return self.status == "inserted"


# ── Entity match result ───────────────────────────────────────────────────

_MATCH_STATUSES = ("confirmed", "unconfirmed_review_candidate", "rejected")
MatchStatus = Literal["confirmed", "unconfirmed_review_candidate", "rejected"]


@dataclass
class MatchResult:
    """Structured result from entity-resolution matching.

    Attributes:
        status: One of "confirmed", "unconfirmed_review_candidate", "rejected".
        reason: Human-readable explanation of match decision.
        matched_registry_id: The registry ID (only set when confirmed).
        matched_official_id: Normalized official identifier (SIREN, CIF, etc.)
                             extracted from the source payload.
        method: The matching method used ("official_id", "exact_name", "subset_name").
        confidence: Match confidence [0.0, 1.0].
        match_type: Granular match sub-type for unconfirmed queue
                    ("name_only", "single_token", "partial_overlap", etc.).
    """

    status: MatchStatus
    reason: str = ""
    matched_registry_id: int | None = None
    matched_official_id: str | None = None
    method: str = ""  # "official_id", "exact_name", "subset_name"
    confidence: float = 0.0
    match_type: str = ""  # for unconfirmed: "name_only", "single_token", etc.

    def __post_init__(self) -> None:
        if self.status not in _MATCH_STATUSES:
            raise ValueError(
                f"MatchResult.status must be one of {_MATCH_STATUSES}, got {self.status!r}"
            )

    # Backward-compat booleans for existing tests/callers
    @property
    def is_confirmed(self) -> bool:
        return self.status == "confirmed"

    @property
    def is_unconfirmed(self) -> bool:
        """Deprecated alias for is_unconfirmed_review_candidate."""
        warnings.warn(
            "MatchResult.is_unconfirmed is deprecated; use is_unconfirmed_review_candidate",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.status == "unconfirmed_review_candidate"

    @property
    def is_rejected(self) -> bool:
        return self.status == "rejected"

    @property
    def is_unconfirmed_review_candidate(self) -> bool:
        return self.status == "unconfirmed_review_candidate"

    @property
    def is_match(self) -> bool:
        """Consider confirmed OR unconfirmed_review_candidate as 'match' for filtering."""
        return self.status in ("confirmed", "unconfirmed_review_candidate")


# ── Canonical collection metrics ──────────────────────────────────────────────

@dataclass
class CollectionMetrics:
    """Canonical per-source, per-run collection counters.

    All counts must reconcile: discovered >= fetched,
    fetched == new_evidence + duplicates (+ errors for fetch failures),
    events_created + duplicate_events <= candidates.
    """

    run_id: str
    source_name: str

    # Document-level
    discovered: int = 0       # total items the source reported as available
    fetched: int = 0          # successfully retrieved (may be < discovered)
    new_evidence: int = 0     # FIRST-SEEN evidence (not previously stored)
    duplicates: int = 0       # evidence rows that already existed (by hash)

    # Event-level
    candidates: int = 0       # candidate events extracted/classified
    unconfirmed: int = 0      # candidates persisted to unconfirmed_match_queue
    events_created: int = 0   # newly inserted events
    duplicate_events: int = 0 # events already present (inserted vs duplicate)
    errors: int = 0           # fetch or parse failures
    api_errors: int = 0       # transport/auth/block/unavailable failures
    parse_failures: int = 0   # response/content parsing failures
    error_type: str | None = None
    error_message: str | None = None

    def assert_reconciled(self) -> None:
        """Raise ValueError when a source outcome cannot describe real effects.

        `discovered` is a source-feed count and may be lower than `fetched` when
        one notice is evaluated against multiple registry candidates. The strict
        accounting identities are therefore evidence attempts and event results.
        """
        counters = self.to_dict()
        negative = [name for name, value in counters.items() if isinstance(value, int) and value < 0]
        if negative:
            raise ValueError(f"negative collection counters: {', '.join(negative)}")
        if self.fetched != self.new_evidence + self.duplicates:
            raise ValueError("fetched must equal new_evidence + duplicates")
        if self.unconfirmed > self.candidates:
            raise ValueError("unconfirmed must not exceed candidates")
        if self.events_created + self.duplicate_events > self.candidates:
            raise ValueError("inserted plus duplicate events must not exceed candidates")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_name": self.source_name,
            "discovered": self.discovered,
            "fetched": self.fetched,
            "new_evidence": self.new_evidence,
            "duplicates": self.duplicates,
            "candidates": self.candidates,
            "unconfirmed": self.unconfirmed,
            "events_created": self.events_created,
            "duplicate_events": self.duplicate_events,
            "errors": self.errors,
            "api_errors": self.api_errors,
            "parse_failures": self.parse_failures,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }
