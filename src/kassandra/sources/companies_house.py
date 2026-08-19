"""Companies House REST API source adapter.

Uses the Companies House REST API for lawful corporate registry lookups.
Authentication via COMPANIES_HOUSE_API_KEY environment variable (Basic auth).
"""

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from kassandra.config import get_config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.companieshouse.gov.uk"


class CompaniesHouseClient:
    """Rate-limited Companies House API client."""

    def __init__(self) -> None:
        config = get_config()
        self._api_key = config.companies_house_api_key
        self._rps = config.api_requests_per_second
        self._last_request: float = 0
        self._daily_count = 0
        self._max_daily = config.api_max_per_day
        self.last_error: Exception | None = None

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _rate_limit(self) -> None:
        """Enforce rate limiting."""
        if self._last_request:
            elapsed = time.time() - self._last_request
            if elapsed < 1.0 / self._rps:
                time.sleep(1.0 / self._rps - elapsed)
        self._last_request = time.time()
        self._daily_count += 1

    def _auth_headers(self) -> dict[str, str]:
        """Basic auth with API key as username, empty password."""
        import base64
        credentials = base64.b64encode(f"{self._api_key}:".encode()).decode()
        return {"Authorization": f"Basic {credentials}"}

    def search_company(self, name: str) -> dict[str, Any] | None:
        """Search for a company by name. Returns first match or None."""
        if not self.available:
            logger.warning("Companies House API key not available")
            return None
        if self._daily_count >= self._max_daily:
            logger.warning("Daily request limit reached")
            return None

        self._rate_limit()
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.get(
                    f"{BASE_URL}/search/companies",
                    params={"q": name, "items_per_page": 3},
                    headers=self._auth_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                items = data.get("items", [])
                return items[0] if items else None
        except httpx.HTTPError as e:
            logger.error(f"Companies House search failed for '{name}': {e}")
            return None

    def get_company(self, company_number: str) -> dict[str, Any] | None:
        """Get full company profile by Companies House number."""
        if not self.available:
            return None

        self._rate_limit()
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.get(
                    f"{BASE_URL}/company/{company_number}",
                    headers=self._auth_headers(),
                )
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as e:
            logger.error(f"Companies House get company '{company_number}' failed: {e}")
            return None

    def get_filing_history(self, company_number: str, items_per_page: int = 10) -> dict | None:
        """Get filing history for a company."""
        self.last_error = None
        if not self.available:
            self.last_error = PermissionError("Companies House API key unavailable")
            return None

        self._rate_limit()
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.get(
                    f"{BASE_URL}/company/{company_number}/filing-history",
                    params={"items_per_page": items_per_page},
                    headers=self._auth_headers(),
                )
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as e:
            self.last_error = e
            logger.error(f"Filing history for '{company_number}' failed: {e}")
            return None
