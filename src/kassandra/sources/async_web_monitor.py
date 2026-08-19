"""Async web monitor — efficient parallel monitoring of 50 companies.

Uses httpx.AsyncClient for concurrent HTTP requests.
Same discovery and conditional GET logic as synchronous version,
but runs in parallel across all portfolio companies.

Per engineering audit: must be fast enough to complete daily collection
without timing out.
"""

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import feedparser
import httpx
from bs4 import BeautifulSoup

from kassandra.config import get_config
from kassandra.evidence import store_evidence

logger = logging.getLogger(__name__)

COMMON_FEED_PATHS = [
    "/rss.xml", "/feed", "/news/rss", "/press/rss", "/en/rss",
]

SITEMAP_PATHS = [
    "/sitemap.xml", "/sitemap_index.xml",
]


class AsyncWebMonitor:
    """Discover and monitor company websites and feeds concurrently."""

    def __init__(self, user_agent: str = "Kassandra/0.2 (+https://github.com/Maar10Herr/Kassandra-Public)"):
        self._headers = {"User-Agent": user_agent}
        self._config = get_config()

    async def monitor_all_companies(
        self, db: Any, companies: list[dict]
    ) -> dict[int, int]:
        """Monitor all companies in parallel. Returns {registry_id: evidence_count}."""
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True, headers=self._headers
        ) as client:
            tasks = []
            for company in companies:
                tasks.append(self._monitor_one(client, db, company))
            results = await asyncio.gather(*tasks, return_exceptions=True)

        counts = {}
        for i, result in enumerate(results):
            rid = companies[i]["registry_id"]
            if isinstance(result, Exception):
                logger.warning(f"Monitor failed for {companies[i].get('canonical_name')}: {result}")
                counts[rid] = 0
            else:
                counts[rid] = result
        return counts

    async def _monitor_one(
        self, client: httpx.AsyncClient, db: Any, company: dict
    ) -> int:
        """Monitor a single company. Returns new evidence count."""
        collected = 0
        now = datetime.now(timezone.utc).isoformat()
        registry_id = company["registry_id"]
        domain = company.get("domain")
        feed_url = company.get("feed_url")
        ir_url = company.get("ir_url")

        base_url = f"https://{domain}" if domain else None
        if not base_url:
            return 0

        # 1. Monitor known + discovered feeds (cap at 3)
        feed_urls = []
        if feed_url:
            feed_urls.append(feed_url)

        # Quick feed discovery
        from kassandra.sources.known_feeds import get_known_feeds, has_no_feed
        isin_row = db.execute(
            "SELECT isin FROM registry WHERE id = ?", (registry_id,)
        ).fetchone()
        if isin_row and not has_no_feed(isin_row["isin"]):
            feed_urls.extend(get_known_feeds(isin_row["isin"]))

        feed_urls = list(dict.fromkeys(feed_urls))  # dedup

        for feed_url in feed_urls[:3]:
            collected += await self._monitor_feed(client, db, registry_id, feed_url, now)

        # 2. Monitor IR page
        if ir_url:
            collected += await self._monitor_page(client, db, registry_id, ir_url, now, "ir_page")

        # 3. Monitor homepage
        collected += await self._monitor_page(client, db, registry_id, base_url, now, "homepage")

        return collected

    async def _monitor_feed(
        self, client: httpx.AsyncClient, db: Any, registry_id: int,
        feed_url: str, now: str
    ) -> int:
        """Monitor an RSS/Atom feed. Returns count of new evidence items."""
        collected = 0
        try:
            content, status = await self._conditional_fetch(client, db, feed_url)
            if status != 200 or not content:
                return 0

            feed = feedparser.parse(content)
            if feed.bozo and not feed.entries:
                return 0

            ev_result = store_evidence(
                db=db, content=content, source_url=feed_url,
                retrieval_time=now, extraction_method="web_monitor_feed",
                parser_version="2.0.0", content_type="application/xml",
                excerpt=content[:2000],
            )
            evidence_id = ev_result.evidence_id
            collected += 1

            for entry in feed.entries[:5]:
                entry_text = f"Title: {entry.get('title', '')}\nSummary: {entry.get('summary', entry.get('description', ''))}"
                entry_hash = hashlib.sha256(entry_text.encode()).hexdigest()

                ev_result = store_evidence(
                    db=db, content=entry_text,
                    source_url=entry.get("link", feed_url),
                    retrieval_time=now,
                    publication_time=entry.get("published") or entry.get("updated"),
                    extraction_method="web_monitor_feed_entry",
                    parser_version="2.0.0", content_type="text/plain",
                    excerpt=entry.get("title", "")[:500],
                )
                evidence_id = ev_result.evidence_id
                collected += 1

        except Exception as e:
            logger.debug(f"Feed monitor failed for {feed_url}: {e}")

        return collected

    async def _monitor_page(
        self, client: httpx.AsyncClient, db: Any, registry_id: int,
        url: str, now: str, page_type: str
    ) -> int:
        """Monitor a single web page for changes."""
        content, status = await self._conditional_fetch(client, db, url)
        if status == 200 and content:
            ev_result = store_evidence(
                db=db, content=content, source_url=url,
                retrieval_time=now,
                extraction_method=f"web_monitor_{page_type}",
                parser_version="2.0.0", content_type="text/html",
                excerpt=content[:2000],
            )
            evidence_id = ev_result.evidence_id
            from kassandra.classifier import extract_events_from_evidence
            extract_events_from_evidence(db, evidence_id, registry_id)
            return 1
        return 0

    async def _conditional_fetch(
        self, client: httpx.AsyncClient, db: Any, url: str
    ) -> tuple[str | None, int]:
        """Fetch URL with conditional GET. Returns (content, status_code)."""
        headers = {}

        cache = db.execute(
            "SELECT last_etag, last_modified, last_content_hash FROM web_cache WHERE url = ?",
            (url,),
        ).fetchone()

        if cache:
            if cache["last_etag"]:
                headers["If-None-Match"] = cache["last_etag"]
            if cache["last_modified"]:
                headers["If-Modified-Since"] = cache["last_modified"]

        try:
            resp = await client.get(url, headers=headers)
            status = resp.status_code

            content_hash = None
            if status == 200:
                content = resp.text
                content_hash = hashlib.sha256(content.encode()).hexdigest()

                if cache and cache["last_content_hash"] == content_hash:
                    self._update_cache(db, url, dict(resp.headers), status, content_hash)
                    return None, 304

                self._update_cache(db, url, dict(resp.headers), status, content_hash)
                return content, 200

            if status == 304:
                self._update_cache(db, url, dict(resp.headers), status)
                return None, 304

            self._update_cache(db, url, dict(resp.headers), status)
            return None, status

        except Exception:
            db.execute(
                """INSERT INTO web_cache (url, last_checked_at, consecutive_failures)
                   VALUES (?, ?, 1)
                   ON CONFLICT(url) DO UPDATE SET
                   consecutive_failures = consecutive_failures + 1,
                   last_checked_at = excluded.last_checked_at""",
                (url, datetime.now(timezone.utc).isoformat()),
            )
            db.commit()
            return None, 0

    def _update_cache(
        self, db: Any, url: str, headers: dict, status: int,
        content_hash: str | None = None
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            """INSERT INTO web_cache
               (url, last_etag, last_modified, last_content_hash,
                last_status_code, last_checked_at, consecutive_failures)
               VALUES (?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(url) DO UPDATE SET
               last_etag = COALESCE(excluded.last_etag, web_cache.last_etag),
               last_modified = COALESCE(excluded.last_modified, web_cache.last_modified),
               last_content_hash = COALESCE(excluded.last_content_hash, web_cache.last_content_hash),
               last_status_code = excluded.last_status_code,
               last_checked_at = excluded.last_checked_at,
               consecutive_failures = 0""",
            (url, headers.get("etag"), headers.get("last-modified"),
             content_hash, status, now),
        )
        db.commit()
