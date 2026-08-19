"""Tests for CLI backfill-unconfirmed-review-candidates safety and correctness.

Uses a temporary file-backed SQLite DB so that the CLI can open and close its
own connection independently, and the test can reopen afterward to assert state.
"""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from kassandra.cli import cli as backfill_cli


def _make_backfill_db_file() -> Path:
    """Create a temp file-backed DB pre-populated with test data. Returns the path."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    import os
    os.close(fd)

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT,
            jurisdiction TEXT,
            companies_house_number TEXT,
            lei TEXT,
            isin TEXT,
            status TEXT,
            domain TEXT,
            company_type TEXT,
            siren TEXT,
            spanish_tax_id TEXT
        );
        CREATE TABLE IF NOT EXISTS evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL UNIQUE,
            source_url TEXT NOT NULL,
            retrieval_time TEXT NOT NULL,
            publication_time TEXT,
            publication_time_confidence TEXT,
            first_seen_time TEXT NOT NULL DEFAULT (datetime('now')),
            extraction_method TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            content_type TEXT,
            content_length INTEGER,
            excerpt TEXT,
            source_reliability REAL,
            corroborated_by TEXT,
            raw_headers TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_id INTEGER NOT NULL REFERENCES evidence(id),
            registry_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            event_subtype TEXT,
            severity TEXT,
            confidence REAL NOT NULL DEFAULT 1.0,
            description TEXT,
            extracted_at TEXT NOT NULL DEFAULT (datetime('now')),
            source_claims_directly BOOLEAN NOT NULL DEFAULT 1,
            raw_event_json TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            tombstone_reason TEXT,
            tombstoned_at TEXT,
            UNIQUE(evidence_id, event_type, registry_id)
        );
        CREATE TABLE IF NOT EXISTS unconfirmed_match_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            source_entity_name TEXT NOT NULL,
            candidate_registry_id INTEGER REFERENCES registry(id),
            candidate_registry_name TEXT,
            match_type TEXT NOT NULL,
            match_confidence REAL NOT NULL DEFAULT 0.5,
            evidence_id INTEGER REFERENCES evidence(id),
            evidence_excerpt TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewed_by TEXT,
            reviewed_at TEXT,
            resolution TEXT,
            resolution_registry_id INTEGER REFERENCES registry(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unconfirmed_dedup
            ON unconfirmed_match_queue(source_name, source_entity_name, candidate_registry_id)
            WHERE resolution IS NULL;
    """)
    # Insert registry rows
    for rid, name, jurisdiction in [
        (1, "LVMH MOET HENNESSY LOUIS VUITTON", "FR"),
        (2, "HERMES INTERNATIONAL", "FR"),
        (3, "TOTALENERGIES SE", "FR"),
        (4, "INDITEX SA", "ES"),
        (5, "IBERDROLA SA", "ES"),
        (6, "INVENSYS LIMITED", "GB"),
    ]:
        db.execute(
            "INSERT INTO registry (id, canonical_name, jurisdiction) VALUES (?, ?, ?)",
            (rid, name, jurisdiction),
        )
    db.commit()

    # Insert evidence and events matching TestBackfillCliSafety setup
    evidence_data = [
        ("h1", "http://b1", "bodacc_fr", "BODACC notice for LVMH"),
        ("h2", "http://b2", "bodacc_fr", "BODACC notice for HERMES"),
        ("h3", "http://b3", "borme_es", "BORME notice for INDITEX"),
        ("h4", "http://b4", "uk_gazette", "Gazette notice for INVENSYS"),
        ("h5", "http://b5", "other_source", "Other source"),
    ]
    ev_ids = []
    for ch, url, method, excerpt in evidence_data:
        db.execute(
            "INSERT INTO evidence (content_hash, source_url, retrieval_time, extraction_method, parser_version, excerpt) "
            "VALUES (?, ?, datetime('now'), ?, '1.0', ?)",
            (ch, url, method, excerpt),
        )
        ev_ids.append(db.execute("SELECT last_insert_rowid()").fetchone()[0])
    db.commit()

    ev1, ev2, ev3, ev4, ev5 = ev_ids
    events_data = [
        (100, ev1, 1, "insolvency", "high", 1, "active"),
        (101, ev2, 2, "restructuring", "medium", 1, "active"),
        (102, ev3, 4, "insolvency", "high", 1, "active"),
        (103, ev4, 6, "insolvency", "high", 1, "active"),
        (200, ev5, 1, "insolvency", "high", 1, "active"),
        (300, ev3, 2, "insolvency", "high", 0, "tombstoned"),
    ]
    for eid, ev, rid, etype, sev, active, status in events_data:
        db.execute(
            "INSERT INTO events (id, evidence_id, registry_id, event_type, severity, active, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (eid, ev, rid, etype, sev, active, status),
        )
    db.commit()
    db.close()
    return Path(db_path)


class TestBackfillCliSafety:
    """backfill_unconfirmed_review_candidates must be manifest-driven and safe."""

    @staticmethod
    def _make_backfill_db():
        """Return a file-backed DB path (not a connection)."""
        return _make_backfill_db_file()

    def _assert_events_active(self, db_path, eids, expected_active):
        """Reopen DB and assert active status for given event IDs."""
        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            for eid in eids:
                row = db.execute("SELECT active, status FROM events WHERE id = ?", (eid,)).fetchone()
                assert row is not None, f"Event {eid} not found"
                assert row["active"] == expected_active, f"Event {eid} active={row['active']}, expected {expected_active}"
        finally:
            db.close()

    def test_dry_run_makes_no_changes(self):
        """Dry-run must not mutate events or queue."""
        db_path = self._make_backfill_db()
        manifest_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        manifest_file.write("[100, 101, 102, 103]")
        manifest_file.close()

        def _get_db():
            return sqlite3.connect(str(db_path))

        # Monkeypatch get_db on the cli module so the command opens a fresh connection
        runner = CliRunner()
        with patch("kassandra.cli.get_db", side_effect=_get_db):
            result = runner.invoke(backfill_cli, [
                "backfill-unconfirmed-review-candidates",
                "--manifest", manifest_file.name,
            ])

        import os
        os.unlink(manifest_file.name)

        # Reopen to assert
        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        for eid in [100, 101, 102, 103]:
            row = db.execute("SELECT active, status FROM events WHERE id = ?", (eid,)).fetchone()
            assert row["active"] == 1, f"Event {eid} should still be active after dry-run"
            assert row["status"] == "active"

        queue_count = db.execute("SELECT COUNT(*) FROM unconfirmed_match_queue").fetchone()[0]
        assert queue_count == 0, f"Queue should be empty, got {queue_count}"
        assert "DRY RUN" in result.output
        db.close()

    def test_apply_tombstones_and_queues_valid_ids(self):
        """Apply mode tombstones valid events and inserts queue rows."""
        db_path = self._make_backfill_db()
        manifest_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        manifest_file.write("[100, 101]")
        manifest_file.close()

        def _get_db():
            db = sqlite3.connect(str(db_path))
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            return db

        runner = CliRunner()
        with patch("kassandra.cli.get_db", side_effect=_get_db):
            result = runner.invoke(backfill_cli, [
                "backfill-unconfirmed-review-candidates",
                "--manifest", manifest_file.name, "--apply",
            ])

        import os
        os.unlink(manifest_file.name)

        assert result.exit_code == 0

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        for eid in [100, 101]:
            row = db.execute("SELECT active, status, tombstone_reason FROM events WHERE id = ?", (eid,)).fetchone()
            assert row["active"] == 0
            assert row["status"] == "tombstoned"
            assert "backfill manifest" in (row["tombstone_reason"] or "")

        queue_rows = db.execute(
            "SELECT match_type FROM unconfirmed_match_queue ORDER BY id"
        ).fetchall()
        assert len(queue_rows) == 2
        assert queue_rows[0]["match_type"] == "backfill_demoted"
        assert queue_rows[1]["match_type"] == "backfill_demoted"
        db.close()

    def test_unlisted_confirmed_events_remain_active(self):
        """Events not in manifest must remain active."""
        db_path = self._make_backfill_db()
        manifest_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        manifest_file.write("[100, 102]")
        manifest_file.close()

        def _get_db():
            db = sqlite3.connect(str(db_path))
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            return db

        runner = CliRunner()
        with patch("kassandra.cli.get_db", side_effect=_get_db):
            runner.invoke(backfill_cli, [
                "backfill-unconfirmed-review-candidates",
                "--manifest", manifest_file.name, "--apply",
            ])

        import os
        os.unlink(manifest_file.name)

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        for eid in [101, 103]:
            row = db.execute("SELECT active, status FROM events WHERE id = ?", (eid,)).fetchone()
            assert row["active"] == 1
            assert row["status"] == "active"
        for eid in [100, 102]:
            row = db.execute("SELECT active FROM events WHERE id = ?", (eid,)).fetchone()
            assert row["active"] == 0
        db.close()

    def test_invalid_and_wrong_source_ids_skip(self):
        """Non-existent and wrong-source IDs are safely skipped."""
        db_path = self._make_backfill_db()
        manifest_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        manifest_file.write("[100, 200, 999]")
        manifest_file.close()

        def _get_db():
            db = sqlite3.connect(str(db_path))
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            return db

        runner = CliRunner()
        with patch("kassandra.cli.get_db", side_effect=_get_db):
            result = runner.invoke(backfill_cli, [
                "backfill-unconfirmed-review-candidates",
                "--manifest", manifest_file.name, "--apply",
            ])

        import os
        os.unlink(manifest_file.name)

        assert "Skipped (not found):" in result.output
        assert "Skipped (wrong source):" in result.output

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        row100 = db.execute("SELECT active FROM events WHERE id = 100").fetchone()
        assert row100["active"] == 0
        row200 = db.execute("SELECT active FROM events WHERE id = 200").fetchone()
        assert row200["active"] == 1
        db.close()

    def test_tombstoned_event_skipped(self):
        """Already-tombstoned events are skipped."""
        db_path = self._make_backfill_db()
        manifest_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        manifest_file.write("[100, 300]")
        manifest_file.close()

        def _get_db():
            db = sqlite3.connect(str(db_path))
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            return db

        runner = CliRunner()
        with patch("kassandra.cli.get_db", side_effect=_get_db):
            result = runner.invoke(backfill_cli, [
                "backfill-unconfirmed-review-candidates",
                "--manifest", manifest_file.name, "--apply",
            ])

        import os
        os.unlink(manifest_file.name)

        assert "Skipped (not active):" in result.output

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        row300 = db.execute("SELECT active, status FROM events WHERE id = 300").fetchone()
        assert row300["active"] == 0
        db.close()

    def test_idempotent_second_run(self):
        """Second run with same manifest is idempotent."""
        db_path = self._make_backfill_db()
        manifest_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        manifest_file.write("[100]")
        manifest_file.close()

        def _get_db():
            db = sqlite3.connect(str(db_path))
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            return db

        runner = CliRunner()
        with patch("kassandra.cli.get_db", side_effect=_get_db):
            r1 = runner.invoke(backfill_cli, [
                "backfill-unconfirmed-review-candidates",
                "--manifest", manifest_file.name, "--apply",
            ])
            assert r1.exit_code == 0
            r2 = runner.invoke(backfill_cli, [
                "backfill-unconfirmed-review-candidates",
                "--manifest", manifest_file.name, "--apply",
            ])
            assert r2.exit_code == 0
            assert "Skipped (not active):" in r2.output

        import os
        os.unlink(manifest_file.name)

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        queue_count = db.execute("SELECT COUNT(*) FROM unconfirmed_match_queue").fetchone()[0]
        assert queue_count == 1
        db.close()

    def test_line_delimited_manifest(self):
        """Line-delimited text manifest works."""
        db_path = self._make_backfill_db()
        manifest_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        manifest_file.write("100\n# comment\n101\n\n102\n")
        manifest_file.close()

        def _get_db():
            db = sqlite3.connect(str(db_path))
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            return db

        runner = CliRunner()
        with patch("kassandra.cli.get_db", side_effect=_get_db):
            result = runner.invoke(backfill_cli, [
                "backfill-unconfirmed-review-candidates",
                "--manifest", manifest_file.name, "--apply",
            ])

        import os
        os.unlink(manifest_file.name)

        assert result.exit_code == 0

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        for eid in [100, 101, 102]:
            row = db.execute("SELECT active FROM events WHERE id = ?", (eid,)).fetchone()
            assert row["active"] == 0
        db.close()

    def test_refuses_without_manifest(self):
        """Command refuses when no manifest provided."""
        runner = CliRunner()
        result = runner.invoke(backfill_cli, ["backfill-unconfirmed-review-candidates"])
        assert result.exit_code != 0
        assert "Missing option" in result.output or "manifest" in result.output.lower()
