"""CLI entry point for Kassandra."""

import logging
import sys
from pathlib import Path

import click

from kassandra import __version__
from kassandra.db import get_db, migrate
from kassandra.extract_edge_metrics import (
    extract_edge_metrics,
    extract_operational_criticality,
    extract_economic_materiality,
    show_edge_metrics,
    show_operational_criticality,
    show_economic_materiality,
)


@click.group()
@click.version_option(version=__version__)
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def cli(verbose: bool) -> None:
    """Kassandra — corporate early-warning system for banks and trade-credit insurers.

    Detects public signs of corporate deterioration through multi-source evidence
    collection, legal-ownership mapping, and economic dependency discovery.
    Scoring incorporates signal, recency, credibility, exposure, and materiality.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


@cli.command()
def init() -> None:
    """Initialize the Kassandra database and apply migrations."""
    db = get_db()
    version = migrate(db)
    db.close()
    click.echo(f"Kassandra initialized. Schema version: {version}")


@cli.command()
def import_portfolio() -> None:
    """Import the Euro Stoxx 50 portfolio."""
    from kassandra.portfolio import import_euro_stoxx_50

    db = get_db()
    migrate(db)
    count = import_euro_stoxx_50(db)
    db.close()
    click.echo(f"Imported {count} companies from Euro Stoxx 50")


@cli.command()
def resolve() -> None:
    """Resolve portfolio companies against Companies House registry."""
    from kassandra.registry import resolve_portfolio

    db = get_db()
    migrate(db)
    matched, lei_resolved, total = resolve_portfolio(db)
    db.close()
    click.echo(f"Resolved {matched}/{total} companies against registry ({lei_resolved} with LEI)")


@cli.command()
def collect() -> None:
    """Run evidence collection from configured sources."""
    from kassandra.collector import run_collection

    db = get_db()
    migrate(db)
    results = run_collection(db)
    db.close()
    total = sum(m.new_evidence for m in results.values())
    click.echo(f"Collected {total} new evidence items ({ {k: v.to_dict() for k, v in results.items()} })")


@cli.command()
def build_graph() -> None:
    """Build legal ownership graph edges from GLEIF (parent/subsidiary only).

    ⚠ NOT dependency intelligence — this maps legal parent/subsidiary
    relationships. No customer, supplier, facility, or commodity edges.
    All materiality/criticality is NULL (unknown).
    """
    from kassandra.graph import collect_all_edges

    db = get_db()
    migrate(db)
    results = collect_all_edges(db)
    db.close()
    click.echo(f"Edges: {results}")


@cli.command()
def enrich_graph() -> None:
    """Enrich LEI placeholders with real entity names from GLEIF."""
    from kassandra.graph_enrich import enrich_placeholders

    db = get_db()
    migrate(db)
    count = enrich_placeholders(db)
    db.close()
    click.echo(f"Enriched {count} placeholder entities")


@cli.command()
def score() -> None:
    """Compute investigation scores for all portfolio companies."""
    from kassandra.scoring import compute_scores

    db = get_db()
    migrate(db)
    results = compute_scores(db)
    db.close()
    for r in results[:20]:
        click.echo(f"  {r['canonical_name']:40s} priority={r['analyst_priority']:.2f}")


@cli.command()
@click.option("--port", "-p", default=8765, help="Port to listen on")
def dashboard(port: int) -> None:
    """Start the local investigation dashboard."""
    from kassandra.dashboard import run_dashboard

    click.echo(f"Starting dashboard on http://localhost:{port}")
    run_dashboard(port=port)


@cli.command()
@click.option("--report", is_flag=True, help="Generate validation report only (don't ingest)")
def pilot(report: bool) -> None:
    """Run the economic dependency pilot (3-5 companies, manual validation).

    Ingests 50+ manually curated economic dependency edges for Airbus,
    ASML, and LVMH with source citations, materiality proxies, and
    uncertainty quantification. Validates whether the product thesis
    (economically meaningful dependency extraction) is feasible.
    """
    from kassandra.pilot import define_pilot_edges, ingest_pilot_edges, generate_validation_report

    db = get_db()
    migrate(db)

    if report:
        click.echo(generate_validation_report(db))
    else:
        edges = define_pilot_edges()
        click.echo(f"Defined {len(edges)} pilot edges")
        created = ingest_pilot_edges(db, edges)
        click.echo(f"Ingested {created} edges\n")
        click.echo(generate_validation_report(db))

    db.close()


@cli.command()
@click.option("--isin", "-i", help="ISIN of a single company to analyze")
@click.option("--all", "all_companies", is_flag=True, help="Analyze all companies with known annual reports")
def discover_dependencies(isin: str | None, all_companies: bool) -> None:
    """Automated economic dependency discovery from annual reports.

    Downloads annual report PDFs, extracts text, and applies keyword/pattern
    matching to discover customer, supplier, facility, commodity, and
    operational dependencies. Stores results as evidence and economic_entities.

    Currently supports: ASML, LVMH, SAP, BMW (Airbus blocked by Incapsula).
    """
    from kassandra.sources.economic_dependency import discover_economic_dependencies

    db = get_db()
    migrate(db)

    data_dir = Path("data")

    if isin:
        row = db.execute(
            "SELECT id, canonical_name FROM registry WHERE isin = ?", (isin,)
        ).fetchone()
        if not row:
            click.echo(f"Company with ISIN {isin} not found in registry")
            db.close()
            return
        click.echo(f"Discovering dependencies for {row['canonical_name']}")
        counts = discover_economic_dependencies(db, row["id"], data_dir)
        click.echo(f"Results: {counts}")

    elif all_companies:
        # Discover all companies with IR pages (auto-discovery pipeline)
        rows = db.execute(
            "SELECT id, canonical_name, isin FROM registry WHERE ir_url IS NOT NULL OR domain IS NOT NULL"
        ).fetchall()
        total = len(rows)
        success = 0
        total_deps = 0
        for i, row in enumerate(rows):
            click.echo(f"\n[{i+1}/{total}] {row['canonical_name']}")
            counts = discover_economic_dependencies(db, row["id"], data_dir)
            if counts.get("stored", 0) > 0:
                success += 1
                total_deps += counts["stored"]
        click.echo(f"\nDone: {success}/{total} companies with new dependencies, {total_deps} total edges")
    else:
        click.echo("Use --isin <ISIN> for a single company or --all for batch analysis")

    db.close()


@cli.command()
def promote_dependencies() -> None:
    """Promote existing economic_entities into graph edges for scoring.

    Finds economic_entities with registry_id and evidence_id that have no
    corresponding edge, and creates edges with synthetic target registry
    entries. Idempotent — safe to run multiple times.
    """
    from kassandra.sources.economic_dependency import promote_existing_economic_entities

    db = get_db()
    migrate(db)

    result = promote_existing_economic_entities(db)
    click.echo(
        f"Promotion complete: {result['created']} created, "
        f"{result['duplicates']} duplicates, {result['errors']} errors"
    )
    db.close()


@cli.command()
def verify_state() -> None:
    """Verify state/current.json claims against the operational database.

    Reads each headline metric from state/current.json, queries the DB
    for the actual value, and reports discrepancies. Fails (exit 1) if
    any claim exceeds DB reality.

    This prevents the \"state file lies\" class of bugs.
    """
    import json
    from pathlib import Path

    db = get_db()
    migrate(db)

    state_path = Path("state/current.json")
    if not state_path.exists():
        click.echo("FAIL: state/current.json not found")
        raise SystemExit(1)

    state = json.loads(state_path.read_text())
    stats = state.get("stats", {})
    query_basis = state.get("query_basis", {})

    failures = 0
    passes = 0

    def check(metric: str, db_value, state_value, tolerance: float = 0):
        nonlocal failures, passes
        if db_value < state_value - tolerance:
            click.echo(
                f"  FAIL {metric}: state claims {state_value}, DB has {db_value} "
                f"(gap: {state_value - db_value})"
            )
            failures += 1
        else:
            click.echo(f"  OK   {metric}: DB={db_value}, state={state_value}")
            passes += 1

    click.echo("State integrity check:\n")

    # Structural checks
    if "db_path" not in state:
        click.echo("  FAIL: missing db_path in state file")
        failures += 1
    if "verified_at" not in state:
        click.echo("  FAIL: missing verified_at timestamp")
        failures += 1

    # Metric checks — each maps to a DB query
    checks = {
        "total_edges": "SELECT COUNT(*) FROM edges",
        "gleif_edges": "SELECT COUNT(*) FROM edges WHERE edge_source='gleif'",
        "manual_pilot_edges": "SELECT COUNT(*) FROM edges WHERE edge_source='manual_pilot'",
        "economic_dependency_edges": (
            "SELECT COUNT(*) FROM edges WHERE edge_source NOT IN ('gleif', 'manual_pilot')"
        ),
        "economic_entities_total": "SELECT COUNT(*) FROM economic_entities",
        "economic_entities_linked": (
            "SELECT COUNT(*) FROM economic_entities WHERE registry_id IS NOT NULL"
        ),
        "total_events": "SELECT COUNT(*) FROM events WHERE active = 1 AND status = 'active'",
        "total_scores": "SELECT COUNT(*) FROM scores",
        "evidence_entries": "SELECT COUNT(*) FROM evidence",
        "annual_report_corroborated_edges": (
            "SELECT COUNT(*) FROM edges WHERE manual_validation_status='annual_report_corroborated'"
        ),
    }

    for metric, claimed in stats.items():
        if metric in checks and isinstance(claimed, (int, float)):
            try:
                db_val = db.execute(checks[metric]).fetchone()[0]
                check(metric, db_val, claimed)
            except Exception as e:
                click.echo(f"  ERR  {metric}: query failed — {e}")
                failures += 1

    db.close()

    click.echo(f"\n{passes} passed, {failures} failed")
    if failures > 0:
        click.echo("STATE INTEGRITY CHECK FAILED — fix state/current.json to match DB")
        raise SystemExit(1)
    else:
        click.echo("State integrity verified ✓")


@cli.command(name="extract-edge-metrics")
@click.option("--dry-run", is_flag=True, help="Show what would be updated without changing data")
@click.option("--show", "show_only", is_flag=True, help="Show current metrics without extracting")
def extract_edge_metrics_cmd(dry_run: bool, show_only: bool) -> None:
    """Extract concentration and replaceability figures from annual report evidence.

    Reads annual report full-text evidence from the content-addressed disk store,
    applies regex patterns to find concentration percentages and dependency
    indicators (sole supplier, single source), and updates economic dependency
    edges with concentration and replaceability scores.

    Idempotent: only updates edges where concentration is currently NULL.
    """
    db = get_db()
    migrate(db)

    if show_only:
        results = show_edge_metrics(db)
        if not results:
            click.echo("No annual_report edges found")
        else:
            click.echo(f"{'Type':<30s} {'Total':>6s} {'HasConc':>8s} {'HasRep':>8s} {'AvgConc':>8s} {'AvgRep':>8s}")
            click.echo("-" * 70)
            for r in results:
                click.echo(
                    f"{r['relationship_type']:<30s} "
                    f"{r['total']:>6d} "
                    f"{r['has_concentration']:>8d} "
                    f"{r['has_replaceability']:>8d} "
                    f"{r['avg_concentration'] or 0.0:>8.3f} "
                    f"{r['avg_replaceability'] or 0.0:>8.3f}"
                )
    else:
        if dry_run:
            click.echo("DRY RUN — no changes will be made\n")
        stats = extract_edge_metrics(db, dry_run=dry_run)
        click.echo(
            f"Edge metrics extraction: {stats['updated']} updated, "
            f"{stats['skipped']} skipped, {stats['no_text']} no text, "
            f"{stats['total']} total"
        )

        if not dry_run and stats["updated"] > 0:
            click.echo("\nUpdated metrics by type:")
            results = show_edge_metrics(db)
            for r in results:
                click.echo(
                    f"  {r['relationship_type']:<30s} "
                    f"avg_conc={r['avg_concentration'] or 0.0:.3f} "
                    f"avg_rep={r['avg_replaceability'] or 0.0:.3f}"
                )

    db.close()


@cli.command(name="extract-criticality")
@click.option("--dry-run", is_flag=True, help="Show what would be updated without changing data")
@click.option("--show", "show_only", is_flag=True, help="Show current criticality metrics without extracting")
def extract_criticality_cmd(dry_run: bool, show_only: bool) -> None:
    """Extract operational criticality ratings from annual report evidence.

    Reads annual report full-text evidence, applies regex patterns to find
    criticality indicators (critical supplier, sole source, key dependency,
    strategic partnership, etc.), and updates economic dependency edges with
    operational_criticality scores on a 0.0-1.0 scale.

    Idempotent: only updates edges where operational_criticality is currently NULL.
    """
    db = get_db()
    migrate(db)

    if show_only:
        results = show_operational_criticality(db)
        if not results:
            click.echo("No annual_report edges found")
        else:
            click.echo(f"{'Type':<30s} {'Total':>6s} {'HasCrit':>8s} {'AvgCrit':>8s} {'MinCrit':>8s} {'MaxCrit':>8s}")
            click.echo("-" * 70)
            for r in results:
                click.echo(
                    f"{r['relationship_type']:<30s} "
                    f"{r['total']:>6d} "
                    f"{r['has_criticality']:>8d} "
                    f"{r['avg_criticality'] or 0.0:>8.3f} "
                    f"{r['min_criticality'] or 0.0:>8.3f} "
                    f"{r['max_criticality'] or 0.0:>8.3f}"
                )
    else:
        if dry_run:
            click.echo("DRY RUN — no changes will be made\n")
        stats = extract_operational_criticality(db, dry_run=dry_run)
        click.echo(
            f"Operational criticality extraction: {stats['updated']} updated, "
            f"{stats['skipped']} skipped, {stats['no_text']} no text, "
            f"{stats['total']} total"
        )

        if not dry_run and stats["updated"] > 0:
            click.echo("\nUpdated criticality by type:")
            results = show_operational_criticality(db)
            for r in results:
                click.echo(
                    f"  {r['relationship_type']:<30s} "
                    f"avg_crit={r['avg_criticality'] or 0.0:.3f} "
                    f"min_crit={r['min_criticality'] or 0.0:.3f} "
                    f"max_crit={r['max_criticality'] or 0.0:.3f}"
                )

    db.close()


@cli.command(name="extract-materiality")
@click.option("--dry-run", is_flag=True, help="Show what would be updated without changing data")
@click.option("--show", "show_only", is_flag=True, help="Show current materiality metrics without extracting")
def extract_materiality_cmd(dry_run: bool, show_only: bool) -> None:
    """Extract disclosed economic materiality figures from annual report evidence.

    Reads annual report full-text evidence, locates dependency entity
    descriptions in the text, and searches surrounding context for disclosed
    materiality indicators (percentage of revenue, qualitative statements like
    'material to our business', 'key supplier', 'not material', etc.).

    Only sets materiality when a genuine disclosed figure is found in the
    evidence. Clears materiality_unknown_reason for updated edges.
    Idempotent: only updates edges where economic_materiality is currently NULL.
    """
    db = get_db()
    migrate(db)

    if show_only:
        results = show_economic_materiality(db)
        if not results:
            click.echo("No annual_report edges found")
        else:
            click.echo(
                f"{'Type':<30s} {'Total':>6s} {'HasMat':>8s} "
                f"{'AvgMat':>8s} {'MinMat':>8s} {'MaxMat':>8s} {'Cleared':>8s}"
            )
            click.echo("-" * 78)
            for r in results:
                click.echo(
                    f"{r['relationship_type']:<30s} "
                    f"{r['total']:>6d} "
                    f"{r['has_materiality']:>8d} "
                    f"{r['avg_materiality'] or 0.0:>8.3f} "
                    f"{r['min_materiality'] or 0.0:>8.3f} "
                    f"{r['max_materiality'] or 0.0:>8.3f} "
                    f"{r['cleared_unknown_reason']:>8d}"
                )
    else:
        if dry_run:
            click.echo("DRY RUN — no changes will be made\n")
        stats = extract_economic_materiality(db, dry_run=dry_run)
        click.echo(
            f"Economic materiality extraction: {stats['updated']} updated, "
            f"{stats['skipped']} skipped, {stats['no_text']} no text, "
            f"{stats['total']} total"
        )

        if not dry_run and stats["updated"] > 0:
            click.echo("\nUpdated materiality by type:")
            results = show_economic_materiality(db)
            for r in results:
                if r["has_materiality"] > 0:
                    click.echo(
                        f"  {r['relationship_type']:<30s} "
                        f"avg_mat={r['avg_materiality'] or 0.0:.3f} "
                        f"min_mat={r['min_materiality'] or 0.0:.3f} "
                        f"max_mat={r['max_materiality'] or 0.0:.3f} "
                        f"cleared={r['cleared_unknown_reason']}"
                    )

    db.close()


@cli.command()
@click.option("--digest-only", is_flag=True, help="Generate digest from last metrics without collecting/scoring")
@click.option("--check-only", is_flag=True, help="Only run consistency checks, don't collect or score")
def soak_cycle(digest_only: bool, check_only: bool) -> None:
    """Run one complete soak cycle: collect → score → check → digest.

    Produces a Markdown digest and records all metrics.
    """
    import json
    from kassandra.soak import (
        run_soak_cycle,
        run_consistency_checks,
        collect_metrics,
        generate_digest,
        load_soak_state,
    )

    if digest_only:
        state = load_soak_state()
        db = get_db()
        migrate(db)
        metrics = collect_metrics(db)
        checks = run_consistency_checks(db)
        db.close()
        digest = generate_digest(metrics, checks, state, 0)
        click.echo(digest)
        return

    if check_only:
        db = get_db()
        migrate(db)
        checks = run_consistency_checks(db)
        db.close()
        click.echo(json.dumps(checks, indent=2, default=str))
        return

    result = run_soak_cycle()
    click.echo(json.dumps(result, indent=2, default=str))


@cli.command(name="evidence-show")
@click.argument("evidence_id", type=int)
@click.option("--content", is_flag=True, help="Include stored content excerpt from the content-addressed object")
@click.option("--include-inactive", is_flag=True, help="Explicitly show historical/tombstoned linked events (audit view)")
def evidence_show(evidence_id: int, content: bool, include_inactive: bool) -> None:
    """Show auditable provenance for an evidence item.

    Prints source URL, retrieval/publication times, content hash, parser version,
    linked events, and an excerpt so an analyst can verify what drove an alert.
    """
    import json
    from kassandra.evidence import get_evidence_path

    db = get_db()
    migrate(db)
    row = db.execute(
        """SELECT id, content_hash, source_url, retrieval_time, publication_time,
                  extraction_method, parser_version, content_type, content_length,
                  excerpt, source_reliability
           FROM evidence WHERE id = ?""",
        (evidence_id,),
    ).fetchone()
    if not row:
        db.close()
        raise click.ClickException(f"Evidence {evidence_id} not found")

    event_filter = "" if include_inactive else "AND e.active = 1 AND e.status = 'active'"
    events = [dict(r) for r in db.execute(
        f"""SELECT e.id, e.event_type, e.severity, e.confidence, e.description,
                  e.extracted_at, e.active, e.status, r.canonical_name
           FROM events e
           LEFT JOIN registry r ON e.registry_id = r.id
           WHERE e.evidence_id = ? {event_filter}
           ORDER BY e.id""",
        (evidence_id,),
    )]
    payload = dict(row)
    payload["events"] = events
    payload["view"] = "historical_audit" if include_inactive else "current"
    if content:
        path = get_evidence_path(db, evidence_id)
        if path and path.exists():
            raw = path.read_bytes()[:4000]
            payload["content_preview"] = raw.decode("utf-8", errors="replace")
            payload["object_path"] = str(path)
    db.close()
    click.echo(json.dumps(payload, indent=2, default=str, ensure_ascii=False))


@cli.command(name="alert-daemon")
@click.option("--once", is_flag=True, help="Run one fast alert cycle and exit (cron/launchd friendly)")
@click.option("--interval", default=300, show_default=True, help="Polling interval in seconds for continuous daemon mode")
@click.option("--source", "sources", multiple=True, help="Source to poll; repeatable. Defaults to Companies House + UK Gazette")
@click.option("--include-medium", is_flag=True, help="Also alert on medium-severity events (more noise)")
@click.option("--include-unconfirmed", is_flag=True, help="Include unconfirmed_adverse events (more noise)")
def alert_daemon(once: bool, interval: int, sources: tuple[str, ...], include_medium: bool, include_unconfirmed: bool) -> None:
    """Run the near-real-time adverse-event alert daemon.

    The daily soak cron remains responsible for the full daily change digest.
    This command is the fast path: poll cheap sources, rescore, and print a
    Markdown alert only when new high-severity adverse events appear.
    """
    from kassandra.daemon import AlertPolicy, run_alert_cycle, run_daemon

    severities = ["critical", "high"]
    if include_medium:
        severities.append("medium")
    policy = AlertPolicy(severities=tuple(severities), include_unconfirmed=include_unconfirmed)
    source_list = list(sources) if sources else None

    if once:
        result = run_alert_cycle(sources=source_list, policy=policy)
        message = result.get("message")
        if message:
            click.echo(message)
        else:
            click.echo("[SILENT]")
        return

    click.echo(f"Starting Kassandra alert daemon (interval={interval}s)", err=True)
    run_daemon(interval_seconds=interval, sources=source_list, policy=policy)


@cli.command()
@click.option("--dry-run", "_dry_run", is_flag=True, default=True, help="Show what would be changed without applying (default: True)")
@click.option("--apply", "do_apply", is_flag=True, help="Actually apply changes (dry-run is default)")
@click.option("--manifest", "manifest_path", type=click.Path(exists=True), required=True,
              help="Path to JSON array or line-delimited text file listing event IDs to demote")
def backfill_unconfirmed_review_candidates(_dry_run: bool, do_apply: bool, manifest_path: str) -> None:
    """Backfill: reclassify specific legal-registry events as review candidates.

    Reads a manifest file listing exact event IDs approved for demotion.
    Each ID is validated: must belong to bodacc_fr/borme_es/uk_gazette, must be
    active. Valid IDs are inserted into unconfirmed_match_queue, and only after
    queue insertion succeeds are the events tombstoned.

    Dry-run by default; use --apply to actually mutate.
    Refuses to run without --manifest.
    """
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    mp = Path(manifest_path)
    raw = mp.read_text().strip()
    if raw.startswith("["):
        event_ids = json.loads(raw)
        if not isinstance(event_ids, list) or not all(isinstance(x, int) for x in event_ids):
            click.echo("Manifest JSON must be a list of integers", err=True)
            sys.exit(1)
    else:
        event_ids = []
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    event_ids.append(int(line))
                except ValueError:
                    click.echo(f"Invalid event ID in manifest: {line!r}", err=True)
                    sys.exit(1)

    if not event_ids:
        click.echo("Manifest is empty; nothing to do.", err=True)
        return

    apply_changes = do_apply
    if not apply_changes:
        click.echo("[DRY RUN] No changes will be committed. Use --apply to mutate.")

    db = get_db()
    try:
        sources = ("bodacc_fr", "borme_es", "uk_gazette")
        now = datetime.now(timezone.utc).isoformat()

        validated: list[tuple[int, str, int, int, str, str, str]] = []
        skipped_invalid: list[tuple[int, str]] = []
        skipped_wrong_source: list[tuple[int, str, str]] = []
        skipped_not_active: list[tuple[int, str]] = []
        already_queued: list[int] = []
        inserted: list[int] = []
        tombstoned: list[int] = []

        for eid in sorted(set(event_ids)):
            row = db.execute(
                """SELECT e.id, e.evidence_id, e.registry_id, e.event_type, e.severity,
                          e.active, e.status, ev.extraction_method, ev.excerpt,
                          r.canonical_name
                   FROM events e
                   JOIN evidence ev ON e.evidence_id = ev.id
                   JOIN registry r ON e.registry_id = r.id
                   WHERE e.id = ?""",
                (eid,),
            ).fetchone()

            if not row:
                skipped_invalid.append((eid, "event not found"))
                continue

            if row["extraction_method"] not in sources:
                skipped_wrong_source.append((eid, row["extraction_method"], row["canonical_name"]))
                continue

            if row["active"] != 1 or row["status"] != "active":
                skipped_not_active.append((eid, row["status"]))
                continue

            source = row["extraction_method"]
            registry_id = row["registry_id"]
            evidence_id = row["evidence_id"]
            excerpt = (row["excerpt"] or "")[:200]
            canonical_name = row["canonical_name"]

            validated.append((eid, source, registry_id, evidence_id, excerpt, canonical_name, row["event_type"]))

        for eid, source, registry_id, evidence_id, excerpt, canonical_name, event_type in validated:
            # Insert into queue (idempotent)
            if apply_changes:
                try:
                    db.execute(
                        """INSERT OR IGNORE INTO unconfirmed_match_queue
                           (source_name, source_entity_name, candidate_registry_id,
                            candidate_registry_name, match_type, match_confidence,
                            evidence_id, evidence_excerpt, status, created_at, updated_at)
                           VALUES (?, ?, ?, ?, 'backfill_demoted', 0.15, ?, ?, 'pending', ?, ?)""",
                        (source, excerpt[:200], registry_id,
                         canonical_name, evidence_id,
                         f"Backfill manifest: demoted {event_type}. {excerpt}"[:2000],
                         now, now),
                    )
                    db.commit()
                except Exception as exc:
                    click.echo(f"Queue insert failed for event {eid}: {exc}", err=True)
                    continue

                # Only tombstone if queue insertion succeeded
                tombstone_reason = f"backfill manifest: explicit demotion of event {eid}"
                db.execute(
                    """UPDATE events
                       SET active = 0, status = 'tombstoned',
                           tombstone_reason = ?,
                           tombstoned_at = ?
                       WHERE id = ?""",
                    (tombstone_reason, now, eid),
                )
                db.commit()
                tombstoned.append(eid)
            else:
                # Dry-run: check if already queued
                existing_q = db.execute(
                    """SELECT id FROM unconfirmed_match_queue
                       WHERE source_name = ? AND source_entity_name = ?
                       AND candidate_registry_id = ? AND match_type = 'backfill_demoted'""",
                    (source, excerpt[:200], registry_id),
                ).fetchone()
                if existing_q:
                    already_queued.append(eid)
                else:
                    inserted.append(eid)

        clicked_queued = len(already_queued) + len(inserted)
        click.echo(f"Manifest loaded: {len(event_ids)} IDs")
        click.echo(f"  Skipped (not found):        {len(skipped_invalid)}")
        click.echo(f"  Skipped (wrong source):     {len(skipped_wrong_source)}")
        click.echo(f"  Skipped (not active):       {len(skipped_not_active)}")
        click.echo(f"  Validated:                  {len(validated)}")
        if apply_changes:
            click.echo(f"  Queued + tombstoned:        {len(tombstoned)}")
        else:
            click.echo(f"  Would queue (new):          {len(inserted)}")
            click.echo(f"  Already queued (idempotent): {len(already_queued)}")
        for src, reason in skipped_invalid:
            click.echo(f"    INVALID {src}: {reason}")
        for src, extraction, name in skipped_wrong_source:
            click.echo(f"    WRONG_SOURCE {src}: source={extraction} entity={name}")
        for src, status in skipped_not_active:
            click.echo(f"    NOT_ACTIVE {src}: status={status}")

    finally:
        db.close()


if __name__ == "__main__":
    cli()
