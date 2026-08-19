"""Regression tests for current-signal event lifecycle filtering."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from kassandra.db import _connect, migrate
from kassandra.evidence import store_evidence, store_event


def _db(tmp_path: Path) -> sqlite3.Connection:
    db = _connect(tmp_path / "lifecycle.db")
    migrate(db)
    db.execute(
        "INSERT INTO registry (id, canonical_name, domain, status) VALUES (1, 'LifecycleCo', 'lifecycle.test', 'active')"
    )
    db.execute(
        "INSERT INTO scores (registry_id, score_schema_version, analyst_priority, active_watch_priority, coverage_monitor_priority, computed_at) "
        "VALUES (1, 3, 0, 0, 0, '2026-07-20T00:00:00Z')"
    )
    db.commit()
    return db


def _event(db, *, active: int, status: str, suffix: str):
    evidence = store_evidence(
        db, f"event-{suffix}", f"https://example.test/{suffix}",
        "2026-07-20T00:00:00Z", extraction_method="test", parser_version="1",
    )
    result = store_event(
        db, evidence.evidence_id, 1, "insolvency", severity="critical", confidence=0.99,
        description=f"description-{suffix}", source_claims_directly=True,
    )
    db.execute(
        "UPDATE events SET active = ?, status = ?, extracted_at = ? WHERE id = ?",
        (active, status, f"2026-07-20T00:00:0{1 if active else 9}Z", result.event_id),
    )
    db.commit()
    return result.event_id, evidence.evidence_id


def test_tombstoned_event_is_absent_from_soak_current_signals_and_totals(tmp_path):
    from kassandra.soak import collect_metrics

    db = _db(tmp_path)
    _event(db, active=0, status="tombstoned", suffix="dead")
    metrics = collect_metrics(db)

    score = metrics["scores"]["top5"][0]
    assert score["latest_event_type"] is None
    assert score["evidence_ids"] == ""
    assert metrics["db"]["total_events"] == 0
    assert metrics["db"]["events_24h"] == 0
    assert metrics["db"]["events_by_source"] == {}


def test_tombstoned_event_is_absent_from_transmission_lookup(tmp_path):
    from kassandra.scoring import _compute_transmission_signal

    db = _db(tmp_path)
    db.execute("INSERT INTO registry (id, canonical_name, status) VALUES (2, 'SupplierCo', 'active')")
    db.execute(
        "INSERT INTO evidence (id, content_hash, source_url, retrieval_time, extraction_method, parser_version) "
        "VALUES (1, 'edge-hash', 'https://example.test/edge', '2026-07-20T00:00:00Z', 'test', '1')"
    )
    db.execute(
        """INSERT INTO edges
           (source_registry_id, target_registry_id, relationship_type, edge_source,
            evidence_id, quality_tier, quality_score, confidence)
           VALUES (1, 2, 'supplier_to', 'annual_report', 1, 'T3_TRUSTED_THIRD_PARTY', 0.55, 0.9)"""
    )
    db.commit()
    _event_for_registry = store_evidence(
        db, "supplier-event", "https://example.test/supplier", "2026-07-20T00:00:00Z",
        extraction_method="test", parser_version="1",
    )
    result = store_event(
        db, _event_for_registry.evidence_id, 2, "insolvency",
        severity="critical", confidence=0.99, description="dead",
    )
    db.execute("UPDATE events SET active=0, status='tombstoned' WHERE id=?", (result.event_id,))
    db.commit()

    assert _compute_transmission_signal(db, 1) == 0


def test_dashboard_current_event_views_exclude_tombstones(tmp_path):
    from kassandra.dashboard import DashboardHandler

    db = _db(tmp_path)
    _event(db, active=0, status="tombstoned", suffix="dead")
    handler = object.__new__(DashboardHandler)
    handler.db = db

    assert handler._get_companies()[0]["event_count"] == 0
    assert handler._get_company_detail(1)["events"] == []
    assert handler._get_events("1") == []
    assert handler._get_events() == []


def test_cli_evidence_show_excludes_tombstoned_linked_events(tmp_path):
    from kassandra.cli import cli

    db = _db(tmp_path)
    _, evidence_id = _event(db, active=0, status="tombstoned", suffix="dead")
    runner = CliRunner()
    with patch("kassandra.cli.get_db", return_value=db), patch("kassandra.cli.migrate"):
        result = runner.invoke(cli, ["evidence-show", str(evidence_id)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["events"] == []


def test_cli_evidence_show_labels_explicit_historical_audit_and_exposes_status(tmp_path):
    from kassandra.cli import cli

    db = _db(tmp_path)
    _, evidence_id = _event(db, active=0, status="tombstoned", suffix="dead")
    runner = CliRunner()
    with patch("kassandra.cli.get_db", return_value=db), patch("kassandra.cli.migrate"):
        result = runner.invoke(cli, ["evidence-show", "--include-inactive", str(evidence_id)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["view"] == "historical_audit"
    assert payload["events"][0]["status"] == "tombstoned"


def test_daemon_does_not_alert_on_tombstoned_event(tmp_path):
    from kassandra.daemon import detect_new_adverse_events

    db = _db(tmp_path)
    event_id, _ = _event(db, active=0, status="tombstoned", suffix="dead")
    assert detect_new_adverse_events(db, event_id - 1) == []
