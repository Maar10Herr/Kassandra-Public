"""GLEIF (Global LEI Foundation) source adapter.

Resolves Legal Entity Identifiers from ISIN or company name.
Free public API, no authentication required.
Provides authoritative entity identifiers across jurisdictions.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GLEIF_API = "https://api.gleif.org/api/v1"


class GleifClient:
    """GLEIF API client for LEI resolution."""

    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=30,
            headers={"Accept": "application/vnd.api+json"},
        )
        self._last_request: float = 0

    def _rate_limit(self) -> None:
        """GLEIF asks for reasonable rate limiting; 2 req/s is safe."""
        if self._last_request:
            elapsed = time.time() - self._last_request
            if elapsed < 0.5:
                time.sleep(0.5 - elapsed)
        self._last_request = time.time()

    def search_by_isin(self, isin: str) -> dict[str, Any] | None:
        """Search for an LEI record by ISIN.

        Returns the first matching LEI record or None.
        """
        self._rate_limit()
        try:
            resp = self._client.get(
                f"{GLEIF_API}/lei-records",
                params={"filter[isin]": isin, "page[size]": 3},
            )
            resp.raise_for_status()
            data = resp.json()
            records = data.get("data", [])
            return records[0] if records else None
        except httpx.HTTPError as e:
            logger.warning(f"GLEIF ISIN lookup failed for {isin}: {e}")
            return None

    def search_by_name(self, name: str, country: str | None = None) -> dict[str, Any] | None:
        """Search for LEI records by legal name, optionally filtered by country."""
        self._rate_limit()
        try:
            params: dict[str, Any] = {
                "filter[entity.legalName]": name,
                "page[size]": 3,
            }
            if country:
                params["filter[entity.jurisdiction]"] = country

            resp = self._client.get(
                f"{GLEIF_API}/lei-records",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            records = data.get("data", [])
            return records[0] if records else None
        except httpx.HTTPError as e:
            logger.warning(f"GLEIF name lookup failed for '{name}': {e}")
            return None

    def get_by_lei(self, lei: str) -> dict[str, Any] | None:
        """Get full LEI record by LEI code. Returns inner data record."""
        self._rate_limit()
        try:
            resp = self._client.get(f"{GLEIF_API}/lei-records/{lei}")
            resp.raise_for_status()
            data = resp.json()
            return data.get("data")  # Return inner record, not wrapper
        except httpx.HTTPError as e:
            logger.warning(f"GLEIF LEI lookup failed for {lei}: {e}")
            return None

    def extract_entity_info(self, record: dict[str, Any]) -> dict[str, str]:
        """Extract key entity information from a GLEIF record."""
        attrs = record.get("attributes", {})
        entity = attrs.get("entity", {})
        return {
            "lei": attrs.get("lei", ""),
            "legal_name": entity.get("legalName", {}).get("name", ""),
            "jurisdiction": entity.get("jurisdiction", ""),
            "legal_form": entity.get("legalForm", {}).get("entityLegalFormCode", ""),
            "status": attrs.get("registration", {}).get("status", ""),
            "registered_address": self._format_address(entity.get("legalAddress", {})),
        }

    def get_relationships(
        self, lei: str, relationship_type: str = "direct-child", page_size: int = 50
    ) -> list[dict[str, Any]]:
        """Fetch relationship records for an LEI.

        relationship_type: 'direct-child' or 'ultimate-child'
        Returns list of relationship record objects.
        """
        self._rate_limit()
        try:
            resp = self._client.get(
                f"{GLEIF_API}/lei-records/{lei}/{relationship_type}-relationships",
                params={"page[size]": page_size},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", [])
        except httpx.HTTPError as e:
            logger.warning(f"GLEIF relationships failed for {lei}: {e}")
            return []

    def extract_relationship(self, rel_record: dict[str, Any]) -> dict[str, Any] | None:
        """Extract key relationship info from a GLEIF relationship record."""
        attrs = rel_record.get("attributes", {})
        rel = attrs.get("relationship", {})
        reg = attrs.get("registration", {})

        start_node = rel.get("startNode", {})
        end_node = rel.get("endNode", {})

        return {
            "source_lei": start_node.get("id", ""),
            "target_lei": end_node.get("id", ""),
            "relationship_type": rel.get("type", ""),
            "relationship_status": rel.get("status", ""),
            "corroboration_level": reg.get("corroborationLevel", ""),
            "corroboration_documents": reg.get("corroborationDocuments", ""),
            "initial_registration": reg.get("initialRegistrationDate"),
            "last_update": reg.get("lastUpdateDate"),
            "periods": json.dumps(rel.get("periods", [])),
        }

    @staticmethod
    def _format_address(address: dict) -> str:
        parts = []
        for key in ("addressLines", "line1", "line2", "line3", "line4"):
            val = address.get(key)
            if val:
                if isinstance(val, list):
                    parts.extend(val)
                else:
                    parts.append(str(val))
        for key in ("city", "region", "country", "postalCode"):
            val = address.get(key)
            if val:
                parts.append(str(val))
        return ", ".join(parts)
