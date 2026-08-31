import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from kassandra.db import SCHEMA_VERSION, migrate


def _private_release_exporter():
    exporter_path = Path(__file__).parents[1] / "scripts" / "public_release.py"
    if not exporter_path.exists():
        pytest.skip("private-only public release exporter")
    spec = importlib.util.spec_from_file_location("public_release", exporter_path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load release exporter: {exporter_path}")
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    return exporter


def test_release_check_ignores_root_local_venv_symlink(tmp_path):
    exporter = _private_release_exporter()
    target = tmp_path / "public"
    exporter.build(target)
    python_link = target / ".venv" / "bin" / "python3"
    python_link.parent.mkdir(parents=True)
    python_link.symlink_to(sys.executable)

    exporter.check(target)


def test_release_check_rejects_symlink_under_allowlisted_tree(tmp_path):
    exporter = _private_release_exporter()
    target = tmp_path / "public"
    exporter.build(target)
    release_link = target / "src" / ".venv" / "python3"
    release_link.parent.mkdir(parents=True)
    release_link.symlink_to(sys.executable)

    with pytest.raises(SystemExit, match="symlink"):
        exporter.check(target)


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    migrate(db)
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction, siren, spanish_tax_id, companies_house_number) VALUES ('IBERDROLA SA', 'ES', NULL, 'A15075062', NULL)"
    )
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction, companies_house_number) VALUES ('INVENSYS LIMITED', 'GB', '01234567')"
    )
    db.execute(
        "INSERT INTO registry (canonical_name, jurisdiction, siren) VALUES ('TOTALENERGIES SE', 'FR', '542019511')"
    )
    db.commit()
    return db


def test_canonical_migration_owns_explicit_official_id_columns_and_indexes():
    assert SCHEMA_VERSION >= 14
    assert "siren" in {row[1] for row in _db().execute("PRAGMA table_info(registry)")}
    assert "spanish_tax_id" in {row[1] for row in _db().execute("PRAGMA table_info(registry)")}
    indexes = {row[1] for row in _db().execute("PRAGMA index_list(registry)")}
    assert "idx_registry_siren_exact" in indexes
    assert "idx_registry_spanish_tax_id_exact" in indexes


def test_official_ids_are_exact_and_not_stored_in_lei_or_isin():
    db = _db()
    from kassandra.sources.bodacc import _match_notice_to_registry
    from kassandra.sources.borme import _match_notice_to_registry as borme_match
    from kassandra.sources.gazette import _match_notice_to_registry as gazette_match

    # BODACC: test with FR entity TOTALENERGIES SE (registry id 3, SIREN 542019511)
    assert _match_notice_to_registry(
        db, {"siren": "542019511", "commercant": "TOTALENERGIES SE"}, "TOTALENERGIES SE", 3
    ).is_confirmed
    assert (
        _match_notice_to_registry(
            db, {"siren": "987654321", "commercant": "TOTALENERGIES SE"}, "TOTALENERGIES SE", 3
        ).status
        == "rejected"
    )
    # BORME: test with ES entity IBERDROLA SA (registry id 1, spanish_tax_id A15075062)
    assert borme_match(
        db, {"title": "IBERDROLA SA (A15075062)", "description": ""}, "IBERDROLA SA", 1
    ).is_confirmed
    assert (
        borme_match(
            db, {"title": "IBERDROLA SA (B12345678)", "description": ""}, "IBERDROLA SA", 1
        ).status
        == "rejected"
    )
    # Gazette: test with GB entity INVENSYS LIMITED (registry id 2, companies_house_number 01234567)
    assert gazette_match(
        db,
        {"title": "INVENSYS LIMITED (01234567) - Winding-Up", "summary": ""},
        "INVENSYS LIMITED",
        2,
    ).is_confirmed
    unrelated = gazette_match(
        db,
        {"title": "UNRELATED TRADING LIMITED (07654321) - Winding-Up", "summary": ""},
        "INVENSYS LIMITED",
        2,
    )
    assert unrelated.status == "rejected"


@pytest.mark.parametrize(
    "source, company, notice",
    [
        ("bodacc", "HERMES INTERNATIONAL", "SOCIETE CIVILE IMMOBILIERE HERMES"),
        ("borme", "TECH SA", "SCHNEIDER ELECTRIC TECH S.L. - Fusión"),
        ("gazette", "K B TECH LIMITED", "CORENET TECH (UK) PVT LTD - Winding-Up"),
    ],
)
def test_subset_and_partial_overlap_never_confirm(source, company, notice):
    db = _db()
    if source == "bodacc":
        from kassandra.sources.bodacc import _match_notice_to_registry as match

        result = match(db, {"commercant": notice}, company, 1)
    elif source == "borme":
        from kassandra.sources.borme import _match_notice_to_registry as match

        result = match(db, {"title": notice, "description": ""}, company, 1)
    else:
        from kassandra.sources.gazette import _match_notice_to_registry as match

        result = match(db, {"title": notice, "summary": ""}, company, 1)
    assert not result.is_confirmed


def test_queue_helper_requires_migration_owned_table():
    db = sqlite3.connect(":memory:")
    from kassandra.sources.entity_resolution import queue_unconfirmed_match

    with pytest.raises(RuntimeError, match="canonical database migrations"):
        queue_unconfirmed_match(
            db,
            source_name="x",
            source_entity_name="y",
            candidate_registry_id=None,
            candidate_registry_name=None,
            match_type="partial_overlap",
            match_confidence=0.2,
            evidence_id=None,
            evidence_excerpt=None,
            reason="ambiguous",
        )


def test_source_files_do_not_own_queue_ddl():
    from pathlib import Path

    root = Path(__file__).parents[1] / "src/kassandra/sources"
    for name in ("bodacc.py", "borme.py", "gazette.py"):
        text = (root / name).read_text()
        assert "CREATE TABLE IF NOT EXISTS unconfirmed_match_queue" not in text
        assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_unconfirmed_dedup" not in text


def test_jurisdiction_normalization():
    from kassandra.sources.entity_resolution import normalize_jurisdiction

    assert normalize_jurisdiction("FR") == "FR"
    assert normalize_jurisdiction("ES") == "ES"
    assert normalize_jurisdiction("DE") == "DE"
    assert normalize_jurisdiction("GB") == "GB"
    assert normalize_jurisdiction("france") == "FR"
    assert normalize_jurisdiction("French") == "FR"
    assert normalize_jurisdiction("spain") == "ES"
    assert normalize_jurisdiction("España") == "ES"
    assert normalize_jurisdiction("Germany") == "DE"
    assert normalize_jurisdiction("de") == "DE"
    assert normalize_jurisdiction("germany") == "DE"
    assert normalize_jurisdiction("england-wales") == "GB"
    assert normalize_jurisdiction("northern-ireland") == "GB"
    # IE is sovereign, never mapped to GB
    assert normalize_jurisdiction("IE") == "IE"
    assert normalize_jurisdiction("ie") == "IE"
    assert normalize_jurisdiction("ireland") == "IRELAND"
    # Unknown jurisdictions pass through upper-cased
    assert normalize_jurisdiction("XX") == "XX"
    assert normalize_jurisdiction("unknown") == "UNKNOWN"


def test_jurisdiction_migration_normalizes_synonyms():
    """Migration 17 normalizes known jurisdiction synonyms in the registry."""
    import tempfile
    from pathlib import Path

    from kassandra.db import _connect, migrate

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    try:
        conn = _connect(path)
        # Apply migrations up to 16, then manually insert legacy jurisdictions
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        for v in range(1, 17):
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (v,))
        # Apply only migration 1 to get the registry table structure
        # Actually we need all migrations up to 16 applied. Let's just use migrate normally
        # and then insert test data before migration 17.
        conn.commit()
        conn.close()

        # Reconnect and migrate to 16
        conn = _connect(path)
        # Fake schema_version to 16
        conn.execute("DELETE FROM schema_version")
        for v in range(1, 17):
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (v,))
        conn.commit()

        # Manually apply migrations 1-16 by running them directly
        from kassandra.db import MIGRATIONS

        for v in range(1, 17):
            if v in MIGRATIONS:
                try:
                    conn.executescript(MIGRATIONS[v])
                except Exception:
                    pass
        conn.commit()

        # Insert registry rows with legacy jurisdiction values
        conn.execute(
            "INSERT INTO registry (canonical_name, jurisdiction) VALUES ('Test FR', 'france')"
        )
        conn.execute(
            "INSERT INTO registry (canonical_name, jurisdiction) VALUES ('Test ES', 'spain')"
        )
        conn.execute(
            "INSERT INTO registry (canonical_name, jurisdiction) VALUES ('Test DE', 'Germany')"
        )
        conn.execute("INSERT INTO registry (canonical_name, jurisdiction) VALUES ('Test IE', 'IE')")
        conn.execute(
            "INSERT INTO registry (canonical_name, jurisdiction) VALUES ('Test GB', 'england-wales')"
        )
        conn.execute("INSERT INTO registry (canonical_name, jurisdiction) VALUES ('Test XX', 'XX')")
        conn.commit()

        # Now run migration 17
        migrate(conn)

        rows = {
            r["canonical_name"]: r["jurisdiction"]
            for r in conn.execute("SELECT canonical_name, jurisdiction FROM registry")
        }
        assert rows.get("Test FR") == "FR", f"Expected FR, got {rows.get('Test FR')}"
        assert rows.get("Test ES") == "ES", f"Expected ES, got {rows.get('Test ES')}"
        assert rows.get("Test DE") == "DE", f"Expected DE, got {rows.get('Test DE')}"
        assert rows.get("Test IE") == "IE", f"Expected IE (unmapped), got {rows.get('Test IE')}"
        assert rows.get("Test GB") == "GB", f"Expected GB, got {rows.get('Test GB')}"
        assert rows.get("Test XX") == "XX", f"Expected XX (unchanged), got {rows.get('Test XX')}"

        conn.close()
    finally:
        path.unlink(missing_ok=True)
        path.with_suffix(".db-wal").unlink(missing_ok=True)
        path.with_suffix(".db-shm").unlink(missing_ok=True)
