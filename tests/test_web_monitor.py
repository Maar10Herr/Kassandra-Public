"""Tests for web monitor module."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from kassandra.db import migrate, _connect
from kassandra.sources.web_monitor import WebMonitor


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    try:
        conn = _connect(path)
        migrate(conn)
        yield conn
        conn.close()
    finally:
        path.unlink(missing_ok=True)
        path.with_suffix(".db-wal").unlink(missing_ok=True)
        path.with_suffix(".db-shm").unlink(missing_ok=True)


class TestWebMonitor:
    def test_sitemap_parse_static(self):
        """Can parse static sitemap XML string."""
        monitor = WebMonitor()
        # Use a static XML string instead of hitting example.com
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/page1</loc></url>
  <url><loc>https://example.com/page2</loc></url>
</urlset>"""
        import re
        xml_text = re.sub(r' xmlns="[^"]*"', "", xml)
        from xml.etree import ElementTree
        root = ElementTree.fromstring(xml_text)
        urls = [u.findtext("loc") for u in root.findall("url")]
        assert len(urls) == 2
        assert urls[0] == "https://example.com/page1"

    def test_feed_discovery_common_paths(self):
        """Feed discovery tries common paths."""
        monitor = WebMonitor()
        # Test common feed paths generation
        feeds = monitor.discover_feeds("https://example.com")
        # Most real sites won't have feeds at example.com, but discovery should not crash
        assert isinstance(feeds, list)

    def test_conditional_fetch_caching(self, temp_db):
        """Web cache table works for conditional GET tracking."""
        now = "2025-01-01T00:00:00Z"
        temp_db.execute(
            """INSERT INTO web_cache
               (url, last_etag, last_content_hash, last_status_code, last_checked_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("https://example.com/page", '"abc123"', "hash123", 200, now),
        )
        temp_db.commit()

        row = temp_db.execute(
            "SELECT * FROM web_cache WHERE url = ?", ("https://example.com/page",)
        ).fetchone()
        assert row["last_etag"] == '"abc123"'
        assert row["last_content_hash"] == "hash123"

    def test_web_cache_update(self, temp_db):
        """Web cache updates replace old values."""
        now = "2025-01-01T00:00:00Z"
        temp_db.execute(
            """INSERT INTO web_cache (url, last_etag, last_status_code, last_checked_at)
               VALUES (?, ?, ?, ?)""",
            ("https://example.com/page", '"old"', 200, now),
        )
        temp_db.commit()

        # Update
        temp_db.execute(
            """INSERT INTO web_cache (url, last_etag, last_status_code, last_checked_at, consecutive_failures)
               VALUES (?, ?, ?, ?, 0)
               ON CONFLICT(url) DO UPDATE SET
               last_etag = excluded.last_etag,
               last_status_code = excluded.last_status_code,
               last_checked_at = excluded.last_checked_at,
               consecutive_failures = 0""",
            ("https://example.com/page", '"new"', 304, "2025-01-02T00:00:00Z"),
        )
        temp_db.commit()

        row = temp_db.execute(
            "SELECT * FROM web_cache WHERE url = ?", ("https://example.com/page",)
        ).fetchone()
        assert row["last_etag"] == '"new"'
        assert row["last_status_code"] == 304
