"""Invariant tests for graph semantics and scoring scope.

Uses in-memory databases to avoid mutating production state.
"""

import sqlite3
import pytest


def _create_scoreable_view(db):
    """Create the scoreable_companies view in a test database."""
    db.execute("DROP VIEW IF EXISTS scoreable_companies")
    db.execute("""
        CREATE VIEW scoreable_companies AS
        SELECT * FROM registry
        WHERE domain IS NOT NULL
        AND (company_type IS NULL OR company_type != 'economic_concept')
    """)


@pytest.fixture
def graph_db():
    """In-memory DB with registry entries simulating production state."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row

    db.executescript("""
        CREATE TABLE registry (
            id INTEGER PRIMARY KEY,
            canonical_name TEXT,
            company_type TEXT,
            jurisdiction TEXT,
            domain TEXT,
            isin TEXT,
            lei TEXT,
            status TEXT DEFAULT 'active'
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY,
            source_registry_id INTEGER NOT NULL,
            target_registry_id INTEGER NOT NULL,
            relationship_type TEXT NOT NULL,
            evidence_id INTEGER,
            edge_source TEXT,
            created_at TEXT
        );
    """)

    # 50 portfolio companies (NULL company_type, have domains)
    for i in range(1, 51):
        db.execute(
            "INSERT INTO registry (id, canonical_name, company_type, domain) "
            "VALUES (?, ?, NULL, ?)",
            (i, f"Company_{i}", f"company{i}.com"),
        )

    # 10 economic concept nodes
    for i in range(101, 111):
        db.execute(
            "INSERT INTO registry (id, canonical_name, company_type) "
            "VALUES (?, ?, 'economic_concept')",
            (i, f"Concept_{i}"),
        )

    # Edges from portfolio company to economic concept
    db.execute(
        "INSERT INTO edges (source_registry_id, target_registry_id, "
        "relationship_type, evidence_id, edge_source) "
        "VALUES (1, 101, 'commodity_input', 1, 'annual_report')"
    )

    _create_scoreable_view(db)
    db.commit()
    return db


class TestScoreableView:
    def test_excludes_economic_concepts(self, graph_db):
        count = graph_db.execute(
            "SELECT COUNT(*) FROM scoreable_companies "
            "WHERE company_type = 'economic_concept'"
        ).fetchone()[0]
        assert count == 0, (
            f"INVARIANT VIOLATION: {count} economic_concept rows in scoreable_companies"
        )

    def test_includes_all_portfolio_companies(self, graph_db):
        count = graph_db.execute(
            "SELECT COUNT(*) FROM scoreable_companies"
        ).fetchone()[0]
        assert count == 50, f"Expected 50 scoreable, got {count}"

    def test_economic_concepts_not_scored(self, graph_db):
        """Economic concepts should not appear as scoring targets."""
        concepts_in_view = graph_db.execute(
            "SELECT id FROM scoreable_companies WHERE company_type = 'economic_concept'"
        ).fetchall()
        assert len(concepts_in_view) == 0

    def test_view_is_stable_subset(self, graph_db):
        """Scoreable view should be subset of registry with domains."""
        view_count = graph_db.execute(
            "SELECT COUNT(*) FROM scoreable_companies"
        ).fetchone()[0]
        domain_count = graph_db.execute(
            "SELECT COUNT(*) FROM registry WHERE domain IS NOT NULL"
        ).fetchone()[0]
        assert view_count <= domain_count
        assert view_count == 50

    def test_concept_nodes_still_traversable(self, graph_db):
        """Economic concepts can be targets of edges even if not scoreable."""
        edge_count = graph_db.execute(
            "SELECT COUNT(*) FROM edges WHERE target_registry_id = 101"
        ).fetchone()[0]
        assert edge_count > 0, "Edge to concept node should exist"
