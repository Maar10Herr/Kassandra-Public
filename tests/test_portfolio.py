"""Tests for portfolio import."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from kassandra.db import migrate, _connect
from kassandra.portfolio import import_euro_stoxx_50


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


class TestPortfolio:
    def test_import_count(self, temp_db):
        """Import returns exactly 50 companies."""
        count = import_euro_stoxx_50(temp_db)
        assert count == 50

    def test_portfolio_created(self, temp_db):
        """Portfolio named 'Euro Stoxx 50' is created."""
        import_euro_stoxx_50(temp_db)
        row = temp_db.execute(
            "SELECT * FROM portfolios WHERE name = ?", ("Euro Stoxx 50",)
        ).fetchone()
        assert row is not None

    def test_all_have_isin(self, temp_db):
        """All portfolio items have a non-empty ISIN."""
        import_euro_stoxx_50(temp_db)
        rows = temp_db.execute("SELECT * FROM portfolio_items").fetchall()
        for row in rows:
            assert row["isin"], f"Empty ISIN for {row['name']}"
            assert len(row["isin"]) == 12, f"ISIN should be 12 chars: {row['isin']}"

    def test_idempotent_import(self, temp_db):
        """Importing twice replaces, doesn't duplicate."""
        import_euro_stoxx_50(temp_db)
        import_euro_stoxx_50(temp_db)
        count = temp_db.execute("SELECT COUNT(*) as c FROM portfolio_items").fetchone()["c"]
        assert count == 50

    def test_known_companies(self, temp_db):
        """Known key companies are present."""
        import_euro_stoxx_50(temp_db)
        names = {
            row["name"]
            for row in temp_db.execute("SELECT name FROM portfolio_items").fetchall()
        }
        assert "LVMH" in names
        assert "SAP" in names
        assert "Airbus" in names
