"""Tests for dependency graph builder."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from kassandra.db import migrate, _connect


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


class TestGraphInfrastructure:
    def test_edge_insert(self, temp_db):
        """Can insert and query edges."""
        # Create two registry entries
        temp_db.execute(
            "INSERT INTO registry (canonical_name, lei, jurisdiction) VALUES (?, ?, ?)",
            ("ParentCo", "LEI001", "DE"),
        )
        temp_db.execute(
            "INSERT INTO registry (canonical_name, lei, jurisdiction) VALUES (?, ?, ?)",
            ("SubCo", "LEI002", "DE"),
        )
        parent_id = temp_db.execute("SELECT id FROM registry WHERE lei = ?", ("LEI001",)).fetchone()["id"]
        sub_id = temp_db.execute("SELECT id FROM registry WHERE lei = ?", ("LEI002",)).fetchone()["id"]

        # Create evidence
        temp_db.execute(
            """INSERT INTO evidence
               (content_hash, source_url, retrieval_time, extraction_method, parser_version, content_length)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("edge_ev", "gleif", "2025-01-01T00:00:00Z", "gleif_relationship", "1.0", 100),
        )
        ev_id = temp_db.execute("SELECT id FROM evidence").fetchone()["id"]

        # Create edge
        temp_db.execute(
            """INSERT INTO edges
               (source_registry_id, target_registry_id, relationship_type,
                evidence_id, confidence, economic_materiality, operational_criticality)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (parent_id, sub_id, "parent_subsidiary", ev_id, 1.0, 0.8, 0.3),
        )
        temp_db.commit()

        edge = temp_db.execute(
            "SELECT * FROM edges WHERE source_registry_id = ?", (parent_id,)
        ).fetchone()
        assert edge["relationship_type"] == "parent_subsidiary"
        assert edge["confidence"] == 1.0

    def test_bidirectional_edges(self, temp_db):
        """Both forward and reverse edges can coexist."""
        temp_db.execute("INSERT INTO registry (canonical_name, lei) VALUES (?, ?)", ("A", "A1"))
        temp_db.execute("INSERT INTO registry (canonical_name, lei) VALUES (?, ?)", ("B", "B1"))
        a_id = temp_db.execute("SELECT id FROM registry WHERE lei = ?", ("A1",)).fetchone()["id"]
        b_id = temp_db.execute("SELECT id FROM registry WHERE lei = ?", ("B1",)).fetchone()["id"]

        temp_db.execute(
            """INSERT INTO evidence
               (content_hash, source_url, retrieval_time, extraction_method, parser_version, content_length)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("ev1", "gleif", "2025-01-01T00:00:00Z", "test", "1.0", 100),
        )
        ev_id = temp_db.execute("SELECT id FROM evidence").fetchone()["id"]

        # Forward: A → B
        temp_db.execute(
            """INSERT INTO edges (source_registry_id, target_registry_id, relationship_type,
               evidence_id, confidence, economic_materiality, operational_criticality)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (a_id, b_id, "parent_subsidiary", ev_id, 1.0, 0.8, 0.3),
        )
        # Reverse: B → A
        temp_db.execute(
            """INSERT INTO edges (source_registry_id, target_registry_id, relationship_type,
               evidence_id, confidence, economic_materiality, operational_criticality)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (b_id, a_id, "parent_subsidiary", ev_id, 1.0, 0.8, 0.3),
        )
        temp_db.commit()

        a_edges = temp_db.execute("SELECT COUNT(*) as c FROM edges WHERE source_registry_id = ?", (a_id,)).fetchone()
        b_edges = temp_db.execute("SELECT COUNT(*) as c FROM edges WHERE source_registry_id = ?", (b_id,)).fetchone()
        assert a_edges["c"] == 1
        assert b_edges["c"] == 1

    def test_lei_placeholder_creation(self, temp_db):
        """LEI placeholders can be created for unknown entities."""
        from kassandra.graph import _get_or_create_lei_entity

        id1 = _get_or_create_lei_entity(temp_db, "NEW_LEI_001")
        assert id1 is not None

        # Second call returns same id
        id2 = _get_or_create_lei_entity(temp_db, "NEW_LEI_001")
        assert id1 == id2

        row = temp_db.execute("SELECT * FROM registry WHERE lei = ?", ("NEW_LEI_001",)).fetchone()
        assert row["status"] == "gleif_placeholder"

    def test_graph_exposure_zero_no_edges(self, temp_db):
        """Graph exposure is zero when no edges exist."""
        from kassandra.scoring import _compute_legal_ownership_exposure

        temp_db.execute("INSERT INTO registry (canonical_name) VALUES (?)", ("IsolatedCo",))
        reg_id = temp_db.execute("SELECT id FROM registry").fetchone()["id"]

        exposure = _compute_legal_ownership_exposure(temp_db, reg_id)
        assert exposure == 0.0
