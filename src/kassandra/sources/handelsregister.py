"""German Handelsregister (Commercial Register) source adapter.

Queries the official handelsregister.de portal for company information,
status changes, and filing history. Uses mechanize to navigate the JSF
form-based portal — the same approach proven by the bundesAPI/handelsregister
open-source project (419 ★, maintained by LilithWittmann).

Design:
- No API key needed — free public portal
- Name-based search with exact/min/all keyword matching
- Extracts: company name, register court, register number (HRB/HRA/VR/GnR),
  Bundesland, current status, document references
- Caches search results on disk (configurable TTL)
- Maps status strings to Kassandra event taxonomy
- Respects rate limits (token-bucket, 1 req/2s)

Known limitations:
- handelsregister.de blocks non-EU IPs. Must be deployed on an EU-hosted
  machine or routed through an EU proxy.
- The portal is JSF/Java-based — forms require ViewState tracking. mechanize
  handles this automatically; plain httpx would need manual state management.
- Filing history is available but requires navigating to per-company detail
  pages (not yet implemented — current implementation is search-only).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kassandra.config import get_config
from kassandra.contracts import CollectionMetrics
from kassandra.evidence import store_evidence, store_event

logger = logging.getLogger(__name__)

# Status string → Kassandra event taxonomy mapping
STATUS_EVENT_MAP: dict[str, tuple[str | None, str | None]] = {
    # Critical — company dissolution/deletion
    "gelöscht": ("insolvency", "critical"),
    "geloscht": ("insolvency", "critical"),
    "von amts wegen gelöscht": ("insolvency", "critical"),
    "von amts wegen geloscht": ("insolvency", "critical"),
    "durch rechtskraft des ablehnenden beschlusses beendet": ("insolvency", "critical"),
    # High — restructuring or legal changes
    "insolvenzverfahren eröffnet": ("insolvency", "critical"),
    "insolvenzverfahren eroffnet": ("insolvency", "critical"),
    "in insolvenz": ("insolvency", "critical"),
    "in liquidation": ("restructuring", "high"),
    "aufgelöst": ("restructuring", "high"),
    "aufgelost": ("restructuring", "high"),
    # Medium — administrative changes that may signal distress
    "sitzverlegung": ("restructuring", "medium"),
    "umwandlung": ("restructuring", "medium"),
    "verschmelzung": ("restructuring", "medium"),
    # Low — routine statuses (not stored as events)
    "eingetragen": (None, None),
    "currently registered": (None, None),
    "newly registered": (None, None),
}

# Keywords that indicate adverse events in the status/document text
ADVERSE_KEYWORDS = [
    "insolvenz", "löschung", "loschung", "auflösung", "auflosung",
    "liquidation", "abwicklung", "beendet",
]

# Rate limiter state (module-level to persist across calls)
_last_request_time: float = 0.0
_MIN_REQUEST_INTERVAL: float = 2.0  # seconds


def _respect_rate_limit() -> None:
    """Token-bucket rate limiter for handelsregister.de."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.monotonic()


class HandelsregisterClient:
    """Client for handelsregister.de — the official German commercial register.

    Uses mechanize to navigate the JSF form-based portal. Call .available
    to check whether mechanize + network access are working before use.

    Usage:
        client = HandelsregisterClient()
        if not client.available:
            return  # blocked or mechanize not installed
        results = client.search("Siemens AG")
        events = client.classify_results(results)
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._browser = None
        self._available: bool | None = None
        self._cache_dir = cache_dir or (
            Path(tempfile.gettempdir()) / "handelsregister_cache"
        )
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def available(self) -> bool:
        """Check whether the handelsregister.de portal is reachable."""
        if self._available is not None:
            return self._available
        try:
            import mechanize  # noqa: F811

            br = mechanize.Browser()
            br.set_handle_robots(False)
            br.set_handle_equiv(True)
            br.set_handle_gzip(True)
            br.set_handle_refresh(False)
            br.set_handle_redirect(True)
            br.set_handle_referer(True)
            br.addheaders = [
                (
                    "User-Agent",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/15.5 Safari/605.1.15",
                ),
                ("Accept-Language", "en-GB,en;q=0.9"),
                ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            ]
            _respect_rate_limit()
            br.open("https://www.handelsregister.de", timeout=30)
            self._browser = br
            self._available = True
            return True
        except ImportError:
            logger.info("mechanize not installed — HandelsregisterClient unavailable")
            self._available = False
            return False
        except Exception as e:
            logger.warning(f"handelsregister.de unreachable: {e}")
            self._available = False
            return False

    def search(
        self,
        company_name: str,
        match_mode: str = "exact",
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Search handelsregister.de for a company.

        Args:
            company_name: Name to search for (e.g. "Siemens AG")
            match_mode: "exact", "all", or "min" (keyword matching)
            force_refresh: Skip cache and re-fetch

        Returns list of dicts with: name, court, register_num, state,
        status, statusCurrent, documents, history.
        """
        if not self.available:
            return []

        mode_map = {"all": "1", "min": "2", "exact": "3"}
        mode_value = mode_map.get(match_mode, "3")

        # Cache check
        cache_key = hashlib.sha256(
            f"{company_name}|{match_mode}".encode()
        ).hexdigest()[:16]
        cache_path = self._cache_dir / f"search_{cache_key}.json"

        if not force_refresh and cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < 86400:  # 24h cache
                logger.debug(f"Handelsregister cache hit for {company_name}")
                return json.loads(cache_path.read_text())

        assert self._browser is not None
        br = self._browser

        try:
            # Navigate to advanced search
            _respect_rate_limit()
            br.select_form(name="naviForm")
            br.form.new_control(
                "hidden",
                "naviForm:erweiterteSucheLink",
                {"value": "naviForm:erweiterteSucheLink"},
            )
            br.form.new_control("hidden", "target", {"value": "erweiterteSucheLink"})
            br.submit()

            # Fill search form
            _respect_rate_limit()
            br.select_form(name="form")
            br["form:schlagwoerter"] = company_name
            br["form:schlagwortOptionen"] = [str(mode_value)]
            response = br.submit()
            html = response.read().decode("utf-8")

            results = _parse_search_results(html)
            cache_path.write_text(json.dumps(results, ensure_ascii=False, default=str))
            return results
        except Exception as e:
            logger.warning(f"Handelsregister search failed for '{company_name}': {e}")
            return []

    def classify_results(
        self, results: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Classify Handelsregister search results into Kassandra events.

        Returns list of event dicts with: event_type, severity, confidence,
        description, source_detail.
        """
        events: list[dict[str, Any]] = []
        for result in results:
            status = result.get("status", "").strip().lower()
            status_current = result.get("statusCurrent", "").strip().lower()

            # Check explicit status mapping
            event_type, severity = STATUS_EVENT_MAP.get(
                status, STATUS_EVENT_MAP.get(status_current, (None, None))
            )

            # Fallback: keyword scan for adverse signals
            if event_type is None:
                for kw in ADVERSE_KEYWORDS:
                    if kw in status or kw in status_current:
                        event_type = "unconfirmed_adverse"
                        severity = "low"
                        break

            if event_type is None:
                continue

            events.append({
                "event_type": event_type,
                "severity": severity,
                "confidence": 0.85,  # Official register, high authority
                "description": (
                    f"Handelsregister status for {result.get('name', '?')}: "
                    f"{result.get('status', '?')} "
                    f"(Register: {result.get('register_num', '?')}, "
                    f"Court: {result.get('court', '?')})"
                ),
                "source_detail": {
                    "register_num": result.get("register_num"),
                    "court": result.get("court"),
                    "state": result.get("state"),
                    "status": result.get("status"),
                    "name": result.get("name"),
                },
            })

        return events

    def collect_for_company(
        self,
        db: Any,
        registry_id: int,
        company_name: str,
    ) -> int:
        """Search, store evidence, classify events. Returns count of new events.

        Idempotent — evidence is content-addressed, events are deduplicated.

        Backward-compatible wrapper: delegates to collect_for_company_metrics
        and returns only events_created. Prefer collect_for_company_metrics
        for full reconciliation-safe CollectionMetrics.
        """
        metrics = self.collect_for_company_metrics(
            db=db, registry_id=registry_id,
            company_name=company_name,
        )
        return metrics.events_created

    def collect_for_company_metrics(
        self,
        db: Any,
        registry_id: int,
        company_name: str,
        run_id: str = "",
    ) -> CollectionMetrics:
        """Search, store evidence, classify events — return reconciled metrics.

        Uses actual EvidenceResult.is_new and EventResult.status to count
        discovered/fetched/new_evidence/duplicates/candidates/events_created/
        duplicate_events. No fabricated counters.
        """
        results = self.search(company_name, match_mode="exact")
        discovered = len(results)

        new_evidence = 0
        duplicates = 0
        candidates = 0
        events_created = 0
        duplicate_events = 0

        if not results:
            return CollectionMetrics(
                run_id=run_id, source_name="handelsregister_de",
                discovered=0, fetched=0,
                new_evidence=0, duplicates=0,
                candidates=0, events_created=0, duplicate_events=0,
                errors=0,
            )

        now = datetime.now(timezone.utc).isoformat()

        for result in results:
            content = json.dumps(result, ensure_ascii=False, default=str)
            ev_result = store_evidence(
                db=db,
                content=content,
                source_url=(
                    f"https://www.handelsregister.de/erweiterteSuche?"
                    f"schlagwoerter={company_name}"
                ),
                retrieval_time=now,
                extraction_method="handelsregister_de",
                parser_version="1.0.0",
                content_type="application/json",
                excerpt=(
                    f"{result.get('name', '?')} | {result.get('court', '?')} "
                    f"| {result.get('status', '?')}"
                )[:500],
                source_reliability=1.0,
            )

            evidence_id = ev_result.evidence_id
            if ev_result.is_new:
                new_evidence += 1
            else:
                duplicates += 1

            events = self.classify_results([result])
            for event in events:
                candidates += 1
                evt_result = store_event(
                    db=db,
                    evidence_id=evidence_id,
                    registry_id=registry_id,
                    event_type=event["event_type"],
                    severity=event["severity"],
                    confidence=event["confidence"],
                    description=event["description"],
                    source_claims_directly=True,
                    raw_event_json=json.dumps(event, ensure_ascii=False, default=str),
                )
                if evt_result.status == "inserted":
                    events_created += 1
                elif evt_result.status == "duplicate":
                    duplicate_events += 1

        return CollectionMetrics(
            run_id=run_id, source_name="handelsregister_de",
            discovered=discovered,
            fetched=new_evidence + duplicates,
            new_evidence=new_evidence, duplicates=duplicates,
            candidates=candidates,
            events_created=events_created, duplicate_events=duplicate_events,
            errors=0,
        )


# ── HTML parsing ────────────────────────────────────────────────────────────


def _parse_search_results(html: str) -> list[dict[str, Any]]:
    """Parse handelsregister.de search results HTML into structured dicts.

    The results table is a JSF PrimeFaces dataTable with role='grid'.
    Each row has a data-ri attribute for the row index.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    grid = soup.find("table", role="grid")
    if grid is None:
        logger.warning("No results grid found in handelsregister.de response")
        return []

    results: list[dict[str, Any]] = []
    for row in grid.find_all("tr"):
        data_ri = row.get("data-ri")
        if data_ri is None:
            continue

        cells = [c.text.strip() for c in row.find_all("td")]
        if len(cells) < 5:
            continue

        # Column layout:
        # 0: row number, 1: court/register, 2: name, 3: state, 4: status, 5+: documents
        court_raw = cells[1] if len(cells) > 1 else ""
        result: dict[str, Any] = {
            "name": cells[2] if len(cells) > 2 else "",
            "court": court_raw,
            "register_num": _extract_register_number(court_raw),
            "state": cells[3] if len(cells) > 3 else "",
            "status": cells[4].strip() if len(cells) > 4 else "",
            "documents": cells[5] if len(cells) > 5 else "",
            "history": _extract_history(cells),
        }
        # Canonicalized status for matching
        result["statusCurrent"] = (
            result["status"].upper().replace(" ", "_")
        )
        results.append(result)

    return results


def _extract_register_number(court_text: str) -> str | None:
    """Extract register number from court column (e.g. 'HRB 12345 B')."""
    match = re.search(
        r"(HRA|HRB|GnR|VR|PR)\s*\d+(\s+[A-Z])?(?!\w)",
        court_text,
    )
    if not match:
        return None
    reg = match.group(0)
    return reg


def _extract_history(cells: list[str]) -> list[tuple[str, str]]:
    """Extract name/location history from result cells.

    History starts at cell index 8, with pairs of (name, location).
    Stops when encountering 'Niederlassungen' (branches).
    """
    history: list[tuple[str, str]] = []
    if len(cells) < 10:
        return history

    hist_start = 8
    for i in range(hist_start, len(cells), 3):
        if i + 1 >= len(cells):
            break
        if "Branches" in cells[i] or "Niederlassungen" in cells[i]:
            break
        history.append((cells[i], cells[i + 1]))

    return history
