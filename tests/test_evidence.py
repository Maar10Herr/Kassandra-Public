"""Tests for evidence storage and event creation."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from kassandra.db import migrate, _connect
from kassandra.evidence import store_evidence, store_event


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


class TestEvidence:
    def test_store_and_retrieve(self, temp_db):
        """Store evidence and retrieve it."""
        result = store_evidence(
            db=temp_db,
            content="test content",
            source_url="https://example.com/page",
            retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test_adapter",
            parser_version="1.0.0",
        )
        assert result.evidence_id is not None
        assert result.is_new is True

        row = temp_db.execute(
            "SELECT * FROM evidence WHERE id = ?", (result.evidence_id,)
        ).fetchone()
        assert row["source_url"] == "https://example.com/page"
        assert row["content_hash"] is not None
        assert len(row["content_hash"]) == 64  # SHA-256

    def test_deduplication(self, temp_db):
        """Same content yields same evidence_id (dedup)."""
        r1 = store_evidence(
            db=temp_db,
            content="identical content",
            source_url="https://a.com",
            retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test",
            parser_version="1.0",
        )
        r2 = store_evidence(
            db=temp_db,
            content="identical content",
            source_url="https://b.com",
            retrieval_time="2025-01-02T00:00:00Z",
            extraction_method="test",
            parser_version="1.0",
        )
        assert r1.evidence_id == r2.evidence_id  # Same content hash = same id
        assert r1.is_new is True
        assert r2.is_new is False

    def test_different_content_different_hash(self, temp_db):
        """Different content gets different hashes."""
        r1 = store_evidence(
            db=temp_db,
            content="content A",
            source_url="https://a.com",
            retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test",
            parser_version="1.0",
        )
        r2 = store_evidence(
            db=temp_db,
            content="content B",
            source_url="https://b.com",
            retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test",
            parser_version="1.0",
        )
        assert r1.evidence_id != r2.evidence_id


class TestEvents:
    def test_store_event(self, temp_db):
        """Store an event linked to evidence and registry."""
        ev_result = store_evidence(
            db=temp_db,
            content="event data",
            source_url="https://example.com/event",
            retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test",
            parser_version="1.0",
        )
        temp_db.execute(
            "INSERT INTO registry (canonical_name) VALUES (?)", ("TestCo",)
        )
        reg_id = temp_db.execute("SELECT id FROM registry").fetchone()["id"]

        event_result = store_event(
            db=temp_db,
            evidence_id=ev_result.evidence_id,
            registry_id=reg_id,
            event_type="insolvency",
            severity="critical",
            confidence=1.0,
            description="Company filed for insolvency",
            source_claims_directly=True,
        )
        assert event_result.event_id is not None
        assert event_result.status == "inserted"
        assert event_result.is_new is True

        row = temp_db.execute(
            "SELECT * FROM events WHERE id = ?", (event_result.event_id,)
        ).fetchone()
        assert row["event_type"] == "insolvency"
        assert row["severity"] == "critical"

    def test_event_dedup(self, temp_db):
        """Same evidence + event_type + registry returns duplicate status."""
        ev_result = store_evidence(
            db=temp_db,
            content="event data",
            source_url="https://example.com/event",
            retrieval_time="2025-01-01T00:00:00Z",
            extraction_method="test",
            parser_version="1.0",
        )
        temp_db.execute(
            "INSERT INTO registry (canonical_name) VALUES (?)", ("TestCo",)
        )
        reg_id = temp_db.execute("SELECT id FROM registry").fetchone()["id"]

        r1 = store_event(temp_db, ev_result.evidence_id, reg_id, "insolvency")
        r2 = store_event(temp_db, ev_result.evidence_id, reg_id, "insolvency")
        # First call: inserted
        assert r1.status == "inserted"
        assert r1.event_id is not None
        # Second call: duplicate
        assert r2.status == "duplicate"
        assert r2.event_id == r1.event_id
