"""Generic web monitor — sitemaps, RSS/Atom feeds, conditional GET.

Discovers and monitors company websites and feeds for changes.
Uses etag/last-modified for efficient polling.
Stores changed content as immutable evidence.

No paid APIs. No search. No authentication bypass.
"""

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

# Common feed paths to try when <link rel="alternate"> not found
COMMON_FEED_PATHS = [
    "/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml",
    "/news/feed", "/news/rss", "/en/feed", "/en/rss",
    "/investors/rss", "/investor-relations/rss", "/ir/rss",
    "/press/feed", "/press/rss", "/media/feed",
    "/feeds/press", "/rss/news", "/api/feed",
]

# Common sitemap paths
SITEMAP_PATHS = [
    "/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
    "/sitemap.php", "/sitemap/sitemap.xml",
]


class WebMonitor:
    """Discover and monitor company websites and feeds."""

    def __init__(self, user_agent: str = "Kassandra/0.1 (+https://github.com/Maar10Herr/Kassandra-Public)"):
        self._client = httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )
        self._config = get_config()

    def discover_feeds(self, base_url: str) -> list[str]:
        """Discover RSS/Atom feed URLs from a website.

        Strategy:
        1. Fetch homepage, parse for <link rel=\"alternate\"> feed links
        2. Search HTML for common RSS href patterns
        3. Try common feed paths only if no feeds found
        """
        feeds: list[str] = []

        try:
            resp = self._client.get(base_url, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            # 1. Parse <link rel="alternate"> feed links
            for link in soup.find_all("link", rel="alternate"):
                href = link.get("href", "")
                type_ = link.get("type", "")
                if any(t in type_.lower() for t in ("rss", "atom", "xml")):
                    feed_url = urljoin(base_url, href)
                    if feed_url not in feeds:
                        feeds.append(feed_url)
                        logger.info(f"Found feed via <link>: {feed_url}")

            # 2. Search for RSS href patterns in all <a> and <link> tags
            rss_patterns = ["/rss", "/feed", "press-release", "pressrelease"]
            for tag in soup.find_all(["a", "link"], href=True):
                href = tag.get("href", "")
                text = tag.get_text("", strip=True).lower()
                combined = (href + " " + text).lower()
                if any(p in combined for p in rss_patterns):
                    if any(ext in href for ext in (".xml", ".rss", "rss", "feed")):
                        feed_url = urljoin(base_url, href)
                        if feed_url not in feeds:
                            feeds.append(feed_url)

            # 3. Only try common paths if nothing found (reduced from 17 to 5)
            if not feeds:
                for path in ["/rss.xml", "/feed", "/news/rss", "/press/rss", "/en/rss"]:
                    try:
                        feed_url = urljoin(base_url, path)
                        r = self._client.get(feed_url, timeout=10)
                        if r.status_code == 200:
                            ct = r.headers.get("content-type", "")
                            if any(t in ct for t in ("xml", "rss", "atom")):
                                feeds.append(feed_url)
                    except httpx.HTTPError:
                        continue

        except httpx.HTTPError as e:
            logger.warning(f"Feed discovery failed for {base_url}: {e}")

        return feeds

    def fetch_sitemap_urls(self, base_url: str, max_urls: int = 50) -> list[str]:
        """Fetch URLs from sitemap.xml (robots-aware).

        Respects robots.txt for sitemap location hints.
        """
        urls: list[str] = []

        try:
            # Try robots.txt first for Sitemap: directive
            robots_url = urljoin(base_url, "/robots.txt")
            try:
                r = self._client.get(robots_url, timeout=10)
                if r.status_code == 200:
                    for line in r.text.splitlines():
                        if line.lower().startswith("sitemap:"):
                            sitemap_url = line.split(":", 1)[1].strip()
                            urls.extend(self._parse_sitemap(sitemap_url, max_urls))
            except httpx.HTTPError:
                pass

            # Try common sitemap paths
            for path in SITEMAP_PATHS:
                if len(urls) >= max_urls:
                    break
                try:
                    sitemap_url = urljoin(base_url, path)
                    urls.extend(self._parse_sitemap(sitemap_url, max_urls - len(urls)))
                except (httpx.HTTPError, ElementTree.ParseError):
                    continue

        except Exception as e:
            logger.warning(f"Sitemap fetch failed for {base_url}: {e}")

        return urls[:max_urls]

    def _parse_sitemap(self, sitemap_url: str, max_urls: int) -> list[str]:
        """Parse a sitemap XML, handling sitemap index files."""
        urls: list[str] = []
        resp = self._client.get(sitemap_url, timeout=15)
        resp.raise_for_status()

        # Remove namespace for easier parsing
        xml_text = re.sub(r' xmlns="[^"]*"', "", resp.text)
        root = ElementTree.fromstring(xml_text)

        # Sitemap index
        if root.tag == "sitemapindex":
            for sm in root.findall("sitemap"):
                loc = sm.findtext("loc")
                if loc and len(urls) < max_urls:
                    try:
                        urls.extend(self._parse_sitemap(loc, max_urls - len(urls)))
                    except (httpx.HTTPError, ElementTree.ParseError):
                        continue
        # Regular sitemap
        else:
            for url_elem in root.findall("url"):
                loc = url_elem.findtext("loc")
                if loc:
                    urls.append(loc)
                    if len(urls) >= max_urls:
                        break

        return urls

    def conditional_fetch(self, url: str, db: Any) -> tuple[str | None, int, str | None]:
        """Fetch URL with conditional GET (etag/last-modified).

        Returns (content, status_code, content_hash) or (None, 304, None) if unchanged.
        """
        headers = {}

        # Check cache
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
            resp = self._client.get(url, headers=headers, timeout=20)
            status = resp.status_code

            if status == 304:
                self._update_cache(db, url, resp.headers, status)
                return None, 304, None

            if status == 200:
                content = resp.text
                content_hash = hashlib.sha256(content.encode()).hexdigest()

                # Check if content actually changed (cache may have etag but content differs)
                if cache and cache["last_content_hash"] == content_hash:
                    self._update_cache(db, url, resp.headers, status, content_hash)
                    return None, 304, content_hash

                self._update_cache(db, url, resp.headers, status, content_hash)
                return content, 200, content_hash

            # Non-200/304 — log failure
            self._update_cache(db, url, resp.headers, status)
            return None, status, None

        except httpx.HTTPError as e:
            logger.warning(f"Fetch failed for {url}: {e}")
            db.execute(
                """INSERT INTO web_cache (url, last_checked_at, consecutive_failures)
                   VALUES (?, ?, 1)
                   ON CONFLICT(url) DO UPDATE SET
                   consecutive_failures = consecutive_failures + 1,
                   last_checked_at = excluded.last_checked_at""",
                (url, datetime.now(timezone.utc).isoformat()),
            )
            db.commit()
            return None, 0, None

    def _update_cache(
        self,
        db: Any,
        url: str,
        headers: httpx.Headers,
        status: int,
        content_hash: str | None = None,
    ) -> None:
        """Update the web cache with response metadata."""
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
            (
                url,
                headers.get("etag"),
                headers.get("last-modified"),
                content_hash,
                status,
                now,
            ),
        )
        db.commit()

    def monitor_company(
        self,
        db: Any,
        registry_id: int,
        domain: str | None,
        ir_url: str | None,
        feed_url: str | None,
    ) -> int:
        """Monitor a company's web presence. Returns count of new evidence items.

        Discovers feeds, fetches sitemaps, checks IR pages for changes.
        """
        collected = 0
        now = datetime.now(timezone.utc).isoformat()

        base_url = f"https://{domain}" if domain else None

        # 1. Monitor feed URLs (discovered + known)
        feed_urls: list[str] = []
        if feed_url:
            feed_urls.append(feed_url)
        if base_url:
            feed_urls.extend(self.discover_feeds(base_url))

        # Add known feeds from static data
        from kassandra.sources.known_feeds import get_known_feeds
        isin = db.execute("SELECT isin FROM registry WHERE id = ?", (registry_id,)).fetchone()
        if isin:
            feed_urls.extend(get_known_feeds(isin["isin"]))
        feed_urls = list(dict.fromkeys(feed_urls))  # deduplicate preserving order

        for feed_url in feed_urls[:5]:  # Cap at 5 feeds per company
            collected += self._monitor_feed(db, registry_id, feed_url, now)

        # 2. Monitor IR pages
        if ir_url:
            collected += self._monitor_page(db, registry_id, ir_url, now, "ir_page")
        elif base_url:
            for suffix in ["/investors", "/investor-relations", "/ir"]:
                ir_candidate = urljoin(base_url, suffix)
                content, status, ch = self.conditional_fetch(ir_candidate, db)
                if status == 200 and content:
                    ev_result = store_evidence(
                        db=db,
                        content=content,
                        source_url=ir_candidate,
                        retrieval_time=now,
                        extraction_method="web_monitor",
                        parser_version="1.0.0",
                        content_type="text/html",
                        excerpt=content[:2000],
                    )
                    evidence_id = ev_result.evidence_id
                    collected += 1
                    _classify_and_store(db, evidence_id, registry_id)
                    break

        # 3. Monitor homepage for changes
        if base_url:
            collected += self._monitor_page(db, registry_id, base_url, now, "homepage")

        return collected

    def _monitor_feed(
        self, db: Any, registry_id: int, feed_url: str, now: str
    ) -> int:
        """Monitor an RSS/Atom feed. Returns count of new evidence items."""
        collected = 0

        try:
            content, status, content_hash = self.conditional_fetch(feed_url, db)
            if status != 200 or not content:
                return 0

            # Parse feed
            feed = feedparser.parse(content)
            if feed.bozo and not feed.entries:
                logger.warning(f"Feed parse error for {feed_url}: {feed.bozo_exception}")
                return 0

            # Store feed content as evidence (for change detection)
            ev_result = store_evidence(
                db=db,
                content=content,
                source_url=feed_url,
                retrieval_time=now,
                extraction_method="web_monitor_feed",
                parser_version="1.0.0",
                content_type="application/xml",
                excerpt=content[:2000],
            )
            evidence_id = ev_result.evidence_id
            collected += 1

            # Store individual feed entries as evidence items
            for entry in feed.entries[:10]:
                entry_content = f"Title: {entry.get('title', '')}\n"
                entry_content += f"Link: {entry.get('link', '')}\n"
                entry_content += f"Published: {entry.get('published', '')}\n"
                entry_content += f"Summary: {entry.get('summary', entry.get('description', ''))}\n"

                entry_hash = hashlib.sha256(entry_content.encode()).hexdigest()

                ev_result = store_evidence(
                    db=db,
                    content=entry_content,
                    source_url=entry.get("link", feed_url),
                    retrieval_time=now,
                    publication_time=entry.get("published") or entry.get("updated"),
                    extraction_method="web_monitor_feed_entry",
                    parser_version="1.0.0",
                    content_type="text/plain",
                    excerpt=entry.get("title", "")[:500],
                )
                evidence_id = ev_result.evidence_id
                collected += 1

        except Exception as e:
            logger.warning(f"Feed monitor failed for {feed_url}: {e}")

        return collected

    def _monitor_page(
        self, db: Any, registry_id: int, url: str, now: str, page_type: str
    ) -> int:
        """Monitor a single web page for changes."""
        content, status, content_hash = self.conditional_fetch(url, db)
        if status == 200 and content:
            ev_result = store_evidence(
                db=db,
                content=content,
                source_url=url,
                retrieval_time=now,
                extraction_method=f"web_monitor_{page_type}",
                parser_version="1.0.0",
                content_type="text/html",
                excerpt=content[:2000],
            )
            evidence_id = ev_result.evidence_id
            _classify_and_store(db, evidence_id, registry_id)
            return 1
        return 0


def _classify_and_store(db: Any, evidence_id: int, registry_id: int) -> int:
    """Classify evidence content and store material events."""
    from kassandra.classifier import extract_events_from_evidence
    return extract_events_from_evidence(db, evidence_id, registry_id)
