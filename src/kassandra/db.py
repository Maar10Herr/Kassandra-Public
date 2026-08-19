"""SQLite state database with schema migrations.

All operational state, portfolios, registry, evidence metadata, events,
and job state stored in a single SQLite WAL-mode database.
"""

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from kassandra.config import get_config

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 19

MIGRATIONS: dict[int, str] = {
    1: """
    -- Portfolio root companies
    CREATE TABLE IF NOT EXISTS portfolios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS portfolio_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
        ticker TEXT,
        isin TEXT,
        name TEXT NOT NULL,
        sector TEXT,
        country TEXT,
        weight REAL,
        source TEXT NOT NULL,
        imported_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(portfolio_id, isin)
    );

    -- Canonical company registry (resolved identities)
    CREATE TABLE IF NOT EXISTS registry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_name TEXT NOT NULL,
        companies_house_number TEXT UNIQUE,
        lei TEXT UNIQUE,
        isin TEXT,
        jurisdiction TEXT,
        company_type TEXT,
        status TEXT,
        incorporation_date TEXT,
        registered_address TEXT,
        raw_json TEXT,
        resolved_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_registry_ch_number ON registry(companies_house_number);
    CREATE INDEX IF NOT EXISTS idx_registry_name ON registry(canonical_name);

    -- Aliases / alternate names for registry entities
    CREATE TABLE IF NOT EXISTS registry_aliases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        registry_id INTEGER NOT NULL REFERENCES registry(id),
        alias TEXT NOT NULL,
        alias_type TEXT NOT NULL,  -- previous_name, trading_name, ticker, etc.
        confidence REAL NOT NULL DEFAULT 1.0,
        source TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(registry_id, alias, alias_type)
    );

    -- Immutable evidence storage (metadata; content is content-addressed on disk)
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
    CREATE INDEX IF NOT EXISTS idx_evidence_hash ON evidence(content_hash);
    CREATE INDEX IF NOT EXISTS idx_evidence_url ON evidence(source_url);
    CREATE INDEX IF NOT EXISTS idx_evidence_retrieval ON evidence(retrieval_time);

    -- Material events (point-in-time observations)
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        evidence_id INTEGER NOT NULL REFERENCES evidence(id),
        registry_id INTEGER REFERENCES registry(id),
        event_type TEXT NOT NULL,
        event_subtype TEXT,
        severity TEXT,
        confidence REAL NOT NULL DEFAULT 1.0,
        description TEXT,
        extracted_at TEXT NOT NULL DEFAULT (datetime('now')),
        source_claims_directly BOOLEAN NOT NULL DEFAULT 1,
        raw_event_json TEXT,
        UNIQUE(evidence_id, event_type, registry_id)
    );
    CREATE INDEX IF NOT EXISTS idx_events_registry ON events(registry_id);
    CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

    -- Legal ownership + economic dependency edges (source distinguishes)
    CREATE TABLE IF NOT EXISTS edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_registry_id INTEGER NOT NULL REFERENCES registry(id),
        target_registry_id INTEGER NOT NULL REFERENCES registry(id),
        relationship_type TEXT NOT NULL,
        evidence_id INTEGER NOT NULL REFERENCES evidence(id),
        confidence REAL NOT NULL DEFAULT 1.0,
        economic_materiality REAL,
        operational_criticality REAL,
        concentration REAL,
        replaceability REAL,
        switching_time_days INTEGER,
        inventory_buffer_days INTEGER,
        payment_lag_days INTEGER,
        shock_channels TEXT,
        is_reversible BOOLEAN NOT NULL DEFAULT 1,
        valid_from TEXT,
        valid_until TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_registry_id);
    CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_registry_id);
    CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(relationship_type);

    -- Scoring / ranking snapshots (immutable)
    CREATE TABLE IF NOT EXISTS scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        registry_id INTEGER NOT NULL REFERENCES registry(id),
        score_schema_version INTEGER NOT NULL,
        observation_severity REAL,
        deterioration_risk REAL,
        dependency_exposure REAL,
        analyst_priority REAL,
        factors_json TEXT,
        explanation TEXT,
        computed_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_scores_registry ON scores(registry_id);
    CREATE INDEX IF NOT EXISTS idx_scores_computed ON scores(computed_at);

    -- Source health
    CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_name TEXT NOT NULL UNIQUE,
        source_type TEXT NOT NULL,
        base_url TEXT,
        last_success_at TEXT,
        last_failure_at TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        total_requests INTEGER NOT NULL DEFAULT 0,
        total_evidence INTEGER NOT NULL DEFAULT 0,
        unique_events INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- Job state (resumable)
    CREATE TABLE IF NOT EXISTS job_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_name TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'idle',
        last_run_at TEXT,
        last_error TEXT,
        next_run_at TEXT,
        payload_json TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    2: """
    -- Durable append-only journal for audit trail
    CREATE TABLE IF NOT EXISTS journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL DEFAULT (datetime('now')),
        action TEXT NOT NULL,
        details TEXT
    );
    """,
    3: """
    -- Domain and web monitoring fields for registry
    ALTER TABLE registry ADD COLUMN domain TEXT;
    ALTER TABLE registry ADD COLUMN ir_url TEXT;
    ALTER TABLE registry ADD COLUMN feed_url TEXT;

    -- Web cache for conditional GET tracking
    CREATE TABLE IF NOT EXISTS web_cache (
        url TEXT PRIMARY KEY,
        last_etag TEXT,
        last_modified TEXT,
        last_content_hash TEXT,
        last_status_code INTEGER,
        last_checked_at TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_web_cache_checked ON web_cache(last_checked_at);
    """,
    4: """
    -- Economic dependency edge metadata (engineering audit P0: separate legal ownership from economic dependency)
    ALTER TABLE edges ADD COLUMN edge_source TEXT;          -- 'gleif', 'annual_report', 'procurement', 'manual', etc.
    ALTER TABLE edges ADD COLUMN quote_span TEXT;           -- exact source text establishing the edge
    ALTER TABLE edges ADD COLUMN direction TEXT;            -- 'outgoing' (portfolio→target), 'incoming' (target→portfolio)
    ALTER TABLE edges ADD COLUMN relationship_role TEXT;    -- e.g. 'sole supplier', 'top-3 customer'
    ALTER TABLE edges ADD COLUMN materiality_unknown_reason TEXT;   -- why materiality isn't known
    ALTER TABLE edges ADD COLUMN criticality_unknown_reason TEXT;   -- why criticality isn't known
    ALTER TABLE edges ADD COLUMN uncertainty_reason TEXT;   -- free-text uncertainty explanation
    ALTER TABLE edges ADD COLUMN manual_validation_status TEXT;     -- 'unvalidated', 'confirmed', 'disputed', 'needs_review'

    -- Backfill: mark all existing edges as GLEIF source
    UPDATE edges SET edge_source = 'gleif' WHERE edge_source IS NULL;

    -- Non-LEI economic dependency targets (facilities, commodity suppliers, products, etc.)
    CREATE TABLE IF NOT EXISTS economic_entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_name TEXT NOT NULL,
        entity_type TEXT NOT NULL,        -- 'facility', 'supplier', 'customer', 'commodity', 'product', 'service', 'jurisdiction'
        lei TEXT,                          -- nullable (these entities often lack LEIs)
        jurisdiction TEXT,
        sector TEXT,
        parent_lei TEXT,                   -- LEI of parent legal entity if known
        description TEXT,
        source_url TEXT,
        evidence_id INTEGER REFERENCES evidence(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_econ_entities_type ON economic_entities(entity_type);
    CREATE INDEX IF NOT EXISTS idx_econ_entities_parent ON economic_entities(parent_lei);

    -- Graph collection completeness (P1-4: track cap hits)
    CREATE TABLE IF NOT EXISTS graph_collection_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        registry_id INTEGER NOT NULL REFERENCES registry(id),
        source TEXT NOT NULL,              -- 'gleif', 'companies_house', 'annual_report', etc.
        total_available INTEGER,           -- total edges available from source (if API reports it)
        retrieved_count INTEGER NOT NULL,  -- edges actually collected
        cap_applied INTEGER,               -- the cap value applied (e.g. 30)
        cap_hit BOOLEAN NOT NULL DEFAULT 0,-- whether the cap truncated results
        next_page_token TEXT,              -- pagination token if available
        collection_stopped_reason TEXT,    -- 'cap', 'end_of_data', 'error', 'rate_limit'
        collected_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(registry_id, source)
    );
    CREATE INDEX IF NOT EXISTS idx_graph_state_registry ON graph_collection_state(registry_id);
    """,
    5: """
    -- P0 scoring reframe: priority_reason codes + coverage_quality (engineering audit 2026-06-20)
    ALTER TABLE scores ADD COLUMN priority_reason TEXT;       -- 'adverse_signal', 'transmission_concern', 'dependency_exposure', 'coverage_gap', 'graph_density', 'no_signal'
    ALTER TABLE scores ADD COLUMN coverage_quality TEXT;      -- 'good', 'partial', 'poor', 'unknown'
    """,
    6: """\n    -- P1 unknown-as-product: add unknown_reason for replaceability + switching_time_bucket\n    ALTER TABLE edges ADD COLUMN replaceability_unknown_reason TEXT;\n    ALTER TABLE edges ADD COLUMN switching_time_bucket TEXT;\n    """,
    7: """\n    -- P1 shock channel taxonomy + transmission attributes (engineering audit 2026-06-20)\n    ALTER TABLE edges ADD COLUMN shock_channel TEXT;              -- primary shock channel (e.g. 'supplier_disruption')\n    ALTER TABLE edges ADD COLUMN shock_channel_unknown_reason TEXT;  -- why channel couldn't be determined\n    ALTER TABLE edges ADD COLUMN lag_bucket TEXT;                 -- transmission lag: immediate/days/weeks/months/annual_cycle/unknown\n    ALTER TABLE edges ADD COLUMN buffer_proxy TEXT;               -- buffer against shock: inventory_buffer/multi_supplier/contractual/etc.\n    """,
    8: """\n    -- P1 watch/coverage scoring columns (engineering audit 2026-06-20)\n    -- Separate active watch priority (adverse signals) from coverage monitor\n    -- priority (exposure gaps), plus component-level transparency scores.\n    ALTER TABLE scores ADD COLUMN active_watch_priority REAL DEFAULT 0.0;\n    ALTER TABLE scores ADD COLUMN coverage_monitor_priority REAL DEFAULT 0.0;\n    ALTER TABLE scores ADD COLUMN transmission_signal_score REAL DEFAULT 0.0;\n    ALTER TABLE scores ADD COLUMN graph_coverage_score REAL DEFAULT 0.0;\n    ALTER TABLE scores ADD COLUMN information_gap_score REAL DEFAULT 0.0;\n    ALTER TABLE scores ADD COLUMN source_staleness_score REAL DEFAULT 0.0;\n\n    -- Backfill existing scores: no adverse signals → active_watch=0.0\n    -- Coverage priority computed from existing exposure data:\n    --   coverage_monitor_priority = MIN(legal_ownership/100 + econ_dep/50, 1.0) * 0.4\n    --   graph_coverage_score = MIN(legal_ownership/100 + econ_dep/50, 1.0)\n    UPDATE scores SET\n        active_watch_priority = 0.0,\n        coverage_monitor_priority = MIN(\n            (COALESCE(dependency_exposure, 0.0) / 100.0 +\n             COALESCE(CAST(json_extract(factors_json, '$.economic_dep_exposure') AS REAL), 0.0) / 50.0),\n            1.0\n        ) * 0.4,\n        graph_coverage_score = MIN(\n            (COALESCE(dependency_exposure, 0.0) / 100.0 +\n             COALESCE(CAST(json_extract(factors_json, '$.economic_dep_exposure') AS REAL), 0.0) / 50.0),\n            1.0\n        ),\n        transmission_signal_score = 0.0,\n        information_gap_score = 0.0,\n        source_staleness_score = 0.0;\n    """,
    9: """
    -- Edge quality tiers, transmission gating, and duplicate edge dedup (engineering audit 2026-06-20)
    -- Tiers: T1_OFFICIAL (0.85) > T2_REGISTRY (0.65-0.70) > T3_TRUSTED_THIRD_PARTY (0.45-0.55) > T4_INFERRED (0.25-0.35) > T5_PLACEHOLDER (0.0)
    ALTER TABLE edges ADD COLUMN quality_tier TEXT DEFAULT 'T4_INFERRED';
    ALTER TABLE edges ADD COLUMN quality_score REAL DEFAULT 0.35;
    ALTER TABLE edges ADD COLUMN dedup_key TEXT;
    ALTER TABLE edges ADD COLUMN is_derived_reverse INTEGER DEFAULT 0;
    ALTER TABLE edges ADD COLUMN canonical_edge_id INTEGER;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_dedup ON edges(dedup_key) WHERE dedup_key IS NOT NULL;

    -- Backfill quality tier and score by source
    UPDATE edges SET quality_tier = 'T2_REGISTRY', quality_score = 0.65 WHERE edge_source = 'gleif';
    UPDATE edges SET quality_tier = 'T3_TRUSTED_THIRD_PARTY', quality_score = 0.45 WHERE edge_source = 'annual_report';
    UPDATE edges SET quality_tier = 'T1_OFFICIAL', quality_score = 0.85 WHERE edge_source = 'eprtr';
    UPDATE edges SET quality_tier = 'T1_OFFICIAL', quality_score = 0.85 WHERE edge_source = 'ted';
    UPDATE edges SET quality_tier = 'T3_TRUSTED_THIRD_PARTY', quality_score = 0.55 WHERE edge_source = 'manual_pilot';
    UPDATE edges SET quality_tier = 'T2_REGISTRY', quality_score = 0.70 WHERE edge_source = 'gazette';
    UPDATE edges SET quality_tier = 'T4_INFERRED', quality_score = 0.30 WHERE edge_source = 'web_monitor';
    UPDATE edges SET quality_tier = 'T4_INFERRED', quality_score = 0.25 WHERE edge_source IS NULL;

    -- Compute dedup_key for existing edges (include id to avoid collisions on pre-existing duplicates)
    UPDATE edges SET dedup_key = source_registry_id || '|' || target_registry_id || '|' || relationship_type || '|' || COALESCE(edge_source, 'unknown') || '|' || id WHERE dedup_key IS NULL;
    """,
    10: """
    -- P1 classifier denominator reporting (engineering audit 2026-06-20)
    -- Tracks per-source documents discovered/fetched/classified/events
    -- so '584 documents, 0 events' becomes interpretable.
    CREATE TABLE IF NOT EXISTS classifier_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        source_name TEXT NOT NULL,
        language TEXT,
        documents_discovered INTEGER DEFAULT 0,
        documents_fetched INTEGER DEFAULT 0,
        documents_with_text INTEGER DEFAULT 0,
        documents_classified INTEGER DEFAULT 0,
        candidate_pattern_hits INTEGER DEFAULT 0,
        rejected_candidates INTEGER DEFAULT 0,
        accepted_events INTEGER DEFAULT 0,
        false_positive_checks INTEGER DEFAULT 0,
        classifier_version TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_classifier_runs_run ON classifier_runs(run_id);
    """,
    11: """
    -- P1 canonical migrations: source_runs, scoreable_companies, economic_entities.registry_id
    -- These structures were manually evolved in the operational DB but absent
    -- from committed migrations. This migration makes clean bootstraps reproducible.

    -- source_runs: per-source yield tracking for each collection run
    CREATE TABLE IF NOT EXISTS source_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        source_name TEXT NOT NULL,
        documents_discovered INTEGER DEFAULT 0,
        documents_fetched INTEGER DEFAULT 0,
        new_documents INTEGER DEFAULT 0,
        duplicate_documents INTEGER DEFAULT 0,
        candidates_generated INTEGER DEFAULT 0,
        unconfirmed_matches INTEGER DEFAULT 0,
        events_created INTEGER DEFAULT 0,
        duplicate_events INTEGER DEFAULT 0,
        errors INTEGER DEFAULT 0,
        api_errors INTEGER DEFAULT 0,
        parse_failures INTEGER DEFAULT 0,
        adverse_events_found INTEGER DEFAULT 0,
        dependency_edges_extracted INTEGER DEFAULT 0,
        latency_seconds REAL,
        started_at TEXT,
        completed_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_source_runs_run ON source_runs(run_id);
    CREATE INDEX IF NOT EXISTS idx_source_runs_source ON source_runs(source_name);
    CREATE INDEX IF NOT EXISTS idx_source_runs_completed ON source_runs(completed_at);

    -- scoreable_companies: view excluding economic_concept registry entries
    -- Used by scoring and soak to identify portfolio companies.
    DROP VIEW IF EXISTS scoreable_companies;
    CREATE VIEW scoreable_companies AS
        SELECT * FROM registry
        WHERE domain IS NOT NULL
        AND (company_type IS NULL OR company_type != 'economic_concept');

    -- economic_entities.registry_id: back-link to registry for cross-source corroboration
    -- Used by graph.py, economic_dependency.py, scoring.py, dashboard.py.
    ALTER TABLE economic_entities ADD COLUMN registry_id INTEGER REFERENCES registry(id);
    """,
    12: """
    -- Lifecycle, provenance, and unconfirmed-match structures (engineering audit 2026-07-15)
    -- Lifecycle, provenance, and unconfirmed-match structures (engineering audit 2026-07-15)

    -- unconfirmed_match_queue: ambiguous identity candidates needing analyst review
    CREATE TABLE IF NOT EXISTS unconfirmed_match_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_name TEXT NOT NULL,
        source_entity_name TEXT NOT NULL,
        candidate_registry_id INTEGER REFERENCES registry(id),
        candidate_registry_name TEXT,
        match_type TEXT NOT NULL,          -- 'name_only', 'single_token', 'partial_overlap', etc.
        match_confidence REAL NOT NULL DEFAULT 0.5,
        evidence_id INTEGER REFERENCES evidence(id),
        evidence_excerpt TEXT,
        status TEXT NOT NULL DEFAULT 'pending',  -- pending, accepted, rejected, ignored
        reviewed_by TEXT,
        reviewed_at TEXT,
        resolution TEXT,                    -- 'matched', 'rejected', 'new_entity_required'
        resolution_registry_id INTEGER REFERENCES registry(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_unconfirmed_queue_status ON unconfirmed_match_queue(status);
    CREATE INDEX IF NOT EXISTS idx_unconfirmed_queue_source ON unconfirmed_match_queue(source_name);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_unconfirmed_dedup
        ON unconfirmed_match_queue(source_name, source_entity_name, candidate_registry_id)
        WHERE resolution IS NULL;

    -- Event lifecycle columns: allow invalidation without destroying evidence
    ALTER TABLE events ADD COLUMN active INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE events ADD COLUMN status TEXT NOT NULL DEFAULT 'active';    -- active, tombstoned
    ALTER TABLE events ADD COLUMN tombstone_reason TEXT;                    -- 'false_match', 'retracted', etc.
    ALTER TABLE events ADD COLUMN tombstoned_at TEXT;

    -- Backfill existing events as active
    UPDATE events SET active = 1, status = 'active' WHERE active IS NULL OR status IS NULL;

    -- Score provenance columns: fingerprint inputs for reproducibility
    ALTER TABLE scores ADD COLUMN input_fingerprint TEXT;      -- hash of event/edge/registry state at scoring time
    ALTER TABLE scores ADD COLUMN provenance_json TEXT;         -- structured provenance (run params, config hash, etc.)
    ALTER TABLE scores ADD COLUMN run_id TEXT;                  -- collection run that produced this score
    ALTER TABLE scores ADD COLUMN scorer_version TEXT;          -- SCORER_VERSION at time of scoring
    """,
    13: """
    ALTER TABLE source_runs ADD COLUMN status TEXT NOT NULL DEFAULT 'success';
    """,
    14: """
    -- Canonical official identifiers. Never overload LEI or ISIN with local IDs.
    ALTER TABLE registry ADD COLUMN siren TEXT;
    ALTER TABLE registry ADD COLUMN spanish_tax_id TEXT;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_registry_siren_exact
        ON registry(siren) WHERE siren IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_registry_spanish_tax_id_exact
        ON registry(spanish_tax_id) WHERE spanish_tax_id IS NOT NULL;
    """,
    15: """
    -- Python-side reconciliation below repairs partially-created legacy schemas.
    SELECT 1;
    """,
    16: """
    -- Canonical scoring_runs: persist provenance manifest once per input
    -- fingerprint+version instead of duplicating the full global payload in
    -- every score row.  Deduplicated on (input_fingerprint, scorer_version).
    CREATE TABLE IF NOT EXISTS scoring_runs (
        run_id TEXT PRIMARY KEY,
        input_fingerprint TEXT NOT NULL,
        scorer_version TEXT NOT NULL,
        provenance_json TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(input_fingerprint, scorer_version)
    );
    CREATE INDEX IF NOT EXISTS idx_scoring_runs_fingerprint
        ON scoring_runs(input_fingerprint);
    """,
    17: """
    -- Normalize jurisdiction synonyms in the registry to ISO-3166-1 alpha-2.
    -- Unknown values are left unchanged; Ireland (IE) is never mapped to GB.
    UPDATE registry SET jurisdiction = 'FR' WHERE LOWER(jurisdiction) IN ('france', 'french');
    UPDATE registry SET jurisdiction = 'ES' WHERE LOWER(jurisdiction) IN ('spain', 'espana', 'españa');
    UPDATE registry SET jurisdiction = 'DE' WHERE LOWER(jurisdiction) IN ('germany');
    -- UK constituent country mappings (preserve GB as canonical):
    UPDATE registry SET jurisdiction = 'GB' WHERE LOWER(jurisdiction) IN ('england-wales', 'england', 'wales', 'scotland', 'northern-ireland', 'ni', 'je', 'gg', 'uk');
    -- Null/empty jurisdictions left as-is; uppercase normalization is applied
    -- at write boundaries via normalize_jurisdiction() going forward.
    """,
    18: """
    -- Provenance integrity: triggers enforce scores.run_id -> scoring_runs
    -- referential integrity.  SQLite cannot ALTER ADD CONSTRAINT, so we use
    -- BEFORE INSERT/UPDATE triggers.
    --
    -- Python-side _migrate_18_scoring_provenance() handles:
    --   1. Repairing scoring_runs table shape (v16/v17 partial deployments)
    --   2. Backfilling historical score provenance into scoring_runs
    --   3. Conflict detection (incompatible run_ids / provenance)
    --   4. Marking orphaned legacy references
    --
    -- These triggers are the permanent enforcement layer.
    CREATE TRIGGER IF NOT EXISTS trg_scores_validate_run_id_insert
    BEFORE INSERT ON scores
    WHEN NEW.run_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT,
            'Scoring provenance integrity violation: run_id has no matching scoring_runs row')
        WHERE (SELECT COUNT(*) FROM scoring_runs WHERE run_id = NEW.run_id) = 0;
    END;

    CREATE TRIGGER IF NOT EXISTS trg_scores_validate_run_id_update
    BEFORE UPDATE ON scores
    WHEN NEW.run_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT,
            'Scoring provenance integrity violation: run_id has no matching scoring_runs row')
        WHERE (SELECT COUNT(*) FROM scoring_runs WHERE run_id = NEW.run_id) = 0;
    END;
    """,
    19: """
    -- Structured per-run source failure details for operational diagnosis.
    ALTER TABLE source_runs ADD COLUMN error_detail_json TEXT;
    """,
}


def _migrate_18_scoring_provenance(conn: sqlite3.Connection) -> None:
    """Backfill historical score provenance and repair scoring_runs table shape.

    Migration 16 created scoring_runs but with run_id PRIMARY KEY in the
    canonical definition.  Already-deployed v16/v17 DBs may have only
    UNIQUE(input_fingerprint, scorer_version) without a PRIMARY KEY on
    run_id, or the PK may be on an implicit rowid.  This repairs the
    canonical shape, backfills provenance from historical scores, detects
    conflicts, and marks unresolvable legacy rows.

    Must run inside a migration transaction.
    """
    # ── 1. Repair scoring_runs table shape ─────────────────────────────────
    _repair_scoring_runs_shape(conn)

    # ── 2. Gather historical scores with provenance data ───────────────────
    rows = conn.execute("""
        SELECT id, run_id, input_fingerprint, provenance_json, scorer_version
        FROM scores
        WHERE run_id IS NOT NULL
        ORDER BY id
    """).fetchall()

    if not rows:
        return

    # Index for dedup: (input_fingerprint, scorer_version) -> canonical row
    canonical: dict[tuple[str, str], tuple[str, str, str | None]] = {}
    # Track run_id -> (fingerprint, version) for conflict detection
    run_id_map: dict[str, tuple[str, str]] = {}
    # Track provenance per run_id inserted during this backfill
    provenance_map: dict[str, str | None] = {}

    # ── 2a. Load pre-existing scoring_runs rows for conflict detection ────
    pre_by_rid: dict[str, tuple[str, str, str | None]] = {}
    pre_by_fpsv: dict[tuple[str, str], str] = {}
    for erow in conn.execute(
        "SELECT run_id, input_fingerprint, scorer_version, provenance_json "
        "FROM scoring_runs"
    ).fetchall():
        erid = erow["run_id"]
        efp = erow["input_fingerprint"]
        esv = erow["scorer_version"]
        eprov = erow["provenance_json"]
        pre_by_rid[erid] = (efp, esv, eprov)
        if efp is not None and esv is not None:
            pre_by_fpsv[(efp, esv)] = erid

    for row in rows:
        rid = row["run_id"]
        fp = row["input_fingerprint"]
        sv = row["scorer_version"]
        prov = row["provenance_json"]

        # ── 3a. Missing fingerprint/version: create synthetic legacy record ─
        if not fp or not sv:
            logger.info(
                "Score id=%s has run_id=%r but missing fingerprint/version; "
                "creating legacy scoring_runs record",
                row["id"], rid,
            )
            # Use score id to guarantee uniqueness across legacy rows
            synth_rid = f"legacy-{row['id']}"
            synth_fp = f"legacy-score-{row['id']}"
            synth_sv = f"unknown-{row['id']}"

            # Detect collision with pre-existing scoring_runs
            if synth_rid in pre_by_rid:
                _, _, eprov = pre_by_rid[synth_rid]
                if not _provenance_equal(prov, eprov):
                    raise sqlite3.IntegrityError(
                        f"Scoring provenance conflict: synthetic run_id "
                        f"'{synth_rid}' already exists with different "
                        f"provenance_json. Score id={row['id']}. "
                        f"Aborting migration 18."
                    )
            if synth_rid in provenance_map:
                if not _provenance_equal(prov, provenance_map[synth_rid]):
                    raise sqlite3.IntegrityError(
                        f"Scoring provenance conflict: synthetic run_id "
                        f"'{synth_rid}' already inserted with different "
                        f"provenance_json. Score id={row['id']}. "
                        f"Aborting migration 18."
                    )

            conn.execute(
                """INSERT INTO scoring_runs
                   (run_id, input_fingerprint, scorer_version, provenance_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    synth_rid, synth_fp, synth_sv, prov,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

            # Update the score to reference the synthetic legacy row
            conn.execute(
                "UPDATE scores SET run_id = ?, input_fingerprint = ?, "
                "scorer_version = ? WHERE id = ?",
                (synth_rid, synth_fp, synth_sv, row["id"]),
            )

            provenance_map[synth_rid] = prov
            continue

        key = (fp, sv)

        # ── 3b. Conflict with pre-existing scoring_runs row ────────────────
        if rid in pre_by_rid:
            pe_fp, pe_sv, pe_prov = pre_by_rid[rid]
            if pe_fp != fp or pe_sv != sv:
                raise sqlite3.IntegrityError(
                    f"Scoring provenance conflict: run_id '{rid}' exists in "
                    f"scoring_runs with different fingerprint/version "
                    f"({pe_fp[:16]}.../{pe_sv} vs {fp[:16]}.../{sv}). "
                    f"Aborting migration 18."
                )
            if not _provenance_equal(prov, pe_prov):
                raise sqlite3.IntegrityError(
                    f"Scoring provenance conflict: run_id '{rid}' exists in "
                    f"scoring_runs with different provenance_json. "
                    f"Aborting migration 18."
                )
            run_id_map[rid] = key
            canonical[key] = (rid, fp, prov)
            provenance_map[rid] = prov
            continue

        if key in pre_by_fpsv:
            pe_rid = pre_by_fpsv[key]
            if pe_rid != rid:
                raise sqlite3.IntegrityError(
                    f"Scoring provenance conflict: fingerprint {fp[:16]}... "
                    f"version '{sv}' has existing run_id '{pe_rid}' "
                    f"but score references run_id '{rid}'. "
                    f"Aborting migration 18."
                )

        # ── 3c. Conflict: same run_id but different fingerprint/version ───
        if rid in run_id_map:
            existing_fp, existing_sv = run_id_map[rid]
            if (existing_fp, existing_sv) != (fp, sv):
                raise sqlite3.IntegrityError(
                    f"Scoring provenance conflict: run_id '{rid}' maps to "
                    f"incompatible provenance "
                    f"({existing_fp[:16]}.../{existing_sv} vs "
                    f"{fp[:16]}.../{sv}). "
                    f"Score ids involved: see scores with run_id='{rid}'. "
                    f"Aborting migration 18."
                )
            # Same fingerprint+version+run_id → check provenance
            if not _provenance_equal(prov, provenance_map.get(rid)):
                raise sqlite3.IntegrityError(
                    f"Scoring provenance conflict: run_id '{rid}' has "
                    f"incompatible provenance_json between score rows. "
                    f"Aborting migration 18."
                )
            continue

        # ── 3d. Conflict: same fingerprint+version → different run_id ─────
        if key in canonical:
            existing_rid, existing_fp, existing_prov = canonical[key]
            if existing_rid != rid:
                raise sqlite3.IntegrityError(
                    f"Scoring provenance conflict: fingerprint {fp[:16]}... "
                    f"with version '{sv}' maps to two different run_ids: "
                    f"'{existing_rid}' and '{rid}'. "
                    f"Cannot determine which is canonical. "
                    f"Aborting migration 18."
                )
            if not _provenance_equal(prov, existing_prov):
                raise sqlite3.IntegrityError(
                    f"Scoring provenance conflict: run_id '{rid}' has "
                    f"incompatible provenance_json between score rows. "
                    f"Aborting migration 18."
                )
            continue

        # ── 4. Insert canonical scoring_runs row ──────────────────────────
        try:
            conn.execute(
                """INSERT INTO scoring_runs
                   (run_id, input_fingerprint, scorer_version, provenance_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    rid, fp, sv, prov,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        except sqlite3.IntegrityError as e:
            # Unique constraint violation: a row with different run_id for
            # this fingerprint+version already exists.
            existing = conn.execute(
                """SELECT run_id, provenance_json
                   FROM scoring_runs
                   WHERE input_fingerprint = ? AND scorer_version = ?""",
                (fp, sv),
            ).fetchone()
            if existing and existing["run_id"] != rid:
                raise sqlite3.IntegrityError(
                    f"Scoring provenance conflict: fingerprint {fp[:16]}... "
                    f"version '{sv}' has existing run_id '{existing['run_id']}' "
                    f"but score references run_id '{rid}'. "
                    f"Aborting migration 18."
                ) from e
            if existing and not _provenance_equal(prov, existing["provenance_json"]):
                raise sqlite3.IntegrityError(
                    f"Scoring provenance conflict: run_id '{rid}' has "
                    f"different provenance_json in existing scoring_runs row. "
                    f"Aborting migration 18."
                ) from e
            raise

        canonical[key] = (rid, fp, prov)
        run_id_map[rid] = key
        provenance_map[rid] = prov

    # ── 5. Clear scores.provenance_json for verified rows ─────────────────
    # Only clear for scores whose run_id has been validated in scoring_runs.
    cleared = conn.execute("""
        UPDATE scores
        SET provenance_json = NULL
        WHERE provenance_json IS NOT NULL
          AND run_id IS NOT NULL
          AND run_id IN (SELECT run_id FROM scoring_runs)
    """).rowcount
    if cleared > 0:
        logger.info(
            "Migration 18: cleared provenance_json from %d score rows "
            "(provenance now lives in scoring_runs)",
            cleared,
        )

    # ── 6. Verify no orphan scores exist ──────────────────────────────────
    orphans = conn.execute("""
        SELECT s.id, s.run_id FROM scores s
        WHERE s.run_id IS NOT NULL
          AND s.run_id NOT IN (SELECT run_id FROM scoring_runs)
    """).fetchall()
    if orphans:
        raise sqlite3.IntegrityError(
            f"Migration 18 orphan detection: {len(orphans)} score row(s) "
            f"with run_id not found in scoring_runs. "
            f"First orphan: id={orphans[0]['id']} run_id={orphans[0]['run_id']!r}. "
            f"Aborting migration 18."
        )

    logger.info(
        "Migration 18 provenance backfill: %d canonical scoring_runs rows, "
        "%d scores processed",
        len(canonical), len(rows),
    )


def _repair_scoring_runs_shape(conn: sqlite3.Connection) -> None:
    """Repair scoring_runs table to match migration 16 canonical definition.

    Some already-deployed v16/v17 DBs may have scoring_runs without the
    run_id TEXT PRIMARY KEY definition (e.g. only UNIQUE on fingerprint+version).
    This reconstructs the table when it diverges from the canonical shape.

    Validates all existing rows before DROP; fails on duplicates, conflicting
    mappings, or null required fields instead of silently discarding data.
    """
    table_info = {
        (row[1], row[2]) for row in conn.execute("PRAGMA table_info(scoring_runs)")
    }
    # Expected columns with types (name, type-normalized)
    expected_cols = {
        ("run_id", "TEXT"),
        ("input_fingerprint", "TEXT"),
        ("scorer_version", "TEXT"),
        ("provenance_json", "TEXT"),
        ("created_at", "TEXT"),
    }
    if expected_cols.issubset(table_info):
        # Check PRIMARY KEY
        pk_info = {
            row[1]: row[5]
            for row in conn.execute("PRAGMA table_info(scoring_runs)")
            if row[5] > 0  # pk column position > 0
        }
        if pk_info and list(pk_info.values())[0] == 1:
            return  # Already canonical: run_id is PK column 1

    # Rebuild: save existing data, validate, drop, recreate, restore
    logger.info("Repairing scoring_runs table shape to canonical definition")

    # Determine which columns actually exist
    existing_col_names = {row[1] for row in conn.execute("PRAGMA table_info(scoring_runs)")}

    # Build a safe SELECT that only picks columns that exist
    select_cols = []
    for col in ["run_id", "input_fingerprint", "scorer_version", "provenance_json", "created_at"]:
        if col in existing_col_names:
            select_cols.append(col)
        else:
            select_cols.append("NULL")

    existing = conn.execute(
        f"SELECT {', '.join(select_cols)} FROM scoring_runs"
    ).fetchall()

    # ── Validate all existing rows before DROP ──────────────────────────
    seen_run_ids: set[str] = set()
    seen_fpsv: set[tuple[str, str]] = set()
    null_run_ids: list[int] = []

    for i, row in enumerate(existing):
        rid = row[0]
        fp = row[1]
        sv = row[2]

        if rid is None:
            null_run_ids.append(i)

        # Check duplicate run_id
        if rid is not None:
            if rid in seen_run_ids:
                raise sqlite3.IntegrityError(
                    f"Cannot repair scoring_runs: duplicate run_id '{rid}' "
                    f"would violate PRIMARY KEY constraint. "
                    f"Manual resolution required."
                )
            seen_run_ids.add(rid)

        # Check duplicate (fingerprint, version) — only non-null pairs
        if fp is not None and sv is not None:
            fpsv = (fp, sv)
            if fpsv in seen_fpsv:
                raise sqlite3.IntegrityError(
                    f"Cannot repair scoring_runs: duplicate "
                    f"(fingerprint='{fp[:40]}...', version='{sv}') "
                    f"would violate UNIQUE constraint. "
                    f"Manual resolution required."
                )
            seen_fpsv.add(fpsv)

    if null_run_ids:
        raise sqlite3.IntegrityError(
            f"Cannot repair scoring_runs: {len(null_run_ids)} row(s) have "
            f"NULL run_id. run_id is the PRIMARY KEY and cannot be NULL. "
            f"Row indices (0-based): {null_run_ids}"
        )

    # Synthesize missing fingerprints/versions where unambiguous
    for i, row in enumerate(existing):
        rid = row[0]
        fp = row[1]
        sv = row[2]
        prov = row[3]
        created = row[4]

        need_synth = fp is None or sv is None
        if need_synth:
            synth_fp = fp if fp is not None else f"legacy-fp-{rid}"
            synth_sv = sv if sv is not None else "unknown"

            if fp is None and (synth_fp, synth_sv) in seen_fpsv:
                raise sqlite3.IntegrityError(
                    f"Cannot repair scoring_runs: synthesized fingerprint "
                    f"'{synth_fp}' for run_id '{rid}' collides with existing "
                    f"(fingerprint, version) pair. "
                    f"Manual resolution required."
                )

            existing[i] = (rid, synth_fp, synth_sv, prov, created)
            if fp is None:
                seen_fpsv.add((synth_fp, synth_sv))

    # Save indexes to recreate
    indexes = [
        row[1]
        for row in conn.execute("PRAGMA index_list(scoring_runs)")
        if not row[1].startswith("sqlite_autoindex")
    ]
    conn.execute("DROP TABLE IF EXISTS scoring_runs")
    conn.execute("""
        CREATE TABLE scoring_runs (
            run_id TEXT PRIMARY KEY,
            input_fingerprint TEXT NOT NULL,
            scorer_version TEXT NOT NULL,
            provenance_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(input_fingerprint, scorer_version)
        )
    """)
    for idx_name in indexes:
        if idx_name == "idx_scoring_runs_fingerprint":
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scoring_runs_fingerprint "
                "ON scoring_runs(input_fingerprint)"
            )

    # Re-insert with INSERT (not INSERT OR IGNORE) — constraints enforce
    # integrity; duplicates and conflicts are already ruled out above.
    for row in existing:
        conn.execute(
            """INSERT INTO scoring_runs
               (run_id, input_fingerprint, scorer_version, provenance_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (row[0], row[1], row[2], row[3], row[4]),
        )


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with WAL mode and sensible pragmas."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def get_db() -> sqlite3.Connection:
    """Get the singleton database connection."""
    config = get_config()
    return _connect(config.db_path)


def get_db_path() -> Path:
    return get_config().db_path


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str,
) -> None:
    """Add one missing column without masking partially-applied migrations."""
    if column not in _column_names(conn, table):
        logger.info("Reconciling missing column %s.%s", table, column)
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


class _SchemaIntegrityError(Exception):
    """Raised when critical schema structures are missing or malformed."""


def _validate_critical_indexes(conn: sqlite3.Connection) -> None:
    """Verify every runtime-required index exists and is functional.

    Raises _SchemaIntegrityError if any critical index is missing.
    """
    all_indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    required = {
        "idx_unconfirmed_dedup",
        "idx_registry_siren_exact",
        "idx_registry_spanish_tax_id_exact",
        "idx_scoring_runs_fingerprint",
    }
    missing = required - all_indexes
    if missing:
        raise _SchemaIntegrityError(
            f"Critical indexes missing: {', '.join(sorted(missing))}"
        )
    # Verify scoring_runs unique constraint exists
    scoring_indexes = {
        row[1]: row[2]
        for row in conn.execute("PRAGMA index_list(scoring_runs)")
    }
    unique_indexes = {
        name for name, origin in scoring_indexes.items()
        if origin == "u" or name.startswith("sqlite_autoindex")
    }
    if not unique_indexes:
        # Check via table_info for implicit PK
        pk_columns = [
            row[1]
            for row in conn.execute("PRAGMA table_info(scoring_runs)")
            if row[5] > 0
        ]
        if not pk_columns:
            raise _SchemaIntegrityError(
                "scoring_runs has no UNIQUE constraint or PRIMARY KEY"
            )


def _validate_provenance_triggers(conn: sqlite3.Connection) -> None:
    """Verify provenance integrity triggers exist.

    Raises _SchemaIntegrityError if either trigger is missing.
    """
    triggers = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }
    required_triggers = {
        "trg_scores_validate_run_id_insert",
        "trg_scores_validate_run_id_update",
    }
    missing = required_triggers - triggers
    if missing:
        raise _SchemaIntegrityError(
            f"Provenance integrity triggers missing: {', '.join(sorted(missing))}"
        )


def _reconcile_runtime_schema(conn: sqlite3.Connection) -> None:
    """Repair legacy/manual schemas that were marked migrated only partially.

    Older migrations used a broad duplicate-column exception. If the first
    ``ALTER TABLE`` in a migration had already been applied manually, SQLite
    skipped the remaining statements while the migration was still marked
    complete. This explicit reconciliation owns every runtime-required column.

    Fast path: when the DB is at the current schema version and a compact
    sentinel query confirms all critical columns, indexes, and tables exist,
    skip the full per-column PRAGMA loop.
    """
    # ── Fast path: current schema + sentinel columns present ────────────────
    current_version = conn.execute(
        "SELECT MAX(version) FROM schema_version"
    ).fetchone()[0] or 0
    if current_version == SCHEMA_VERSION:
        try:
            conn.execute("""
                SELECT
                    sr.duplicate_documents, sr.candidates_generated,
                    sr.unconfirmed_matches, sr.events_created, sr.duplicate_events,
                    sr.errors, sr.status, sr.error_detail_json,
                    e.active, e.status, e.tombstone_reason, e.tombstoned_at,
                    s.input_fingerprint, s.provenance_json, s.run_id, s.scorer_version,
                    sruns.run_id, sruns.input_fingerprint, sruns.scorer_version,
                    sruns.provenance_json, sruns.created_at,
                    ee.registry_id,
                    r.siren, r.spanish_tax_id
                FROM source_runs sr
                CROSS JOIN (SELECT 1 AS _) x
                LEFT JOIN events e ON 0
                LEFT JOIN scores s ON 0
                LEFT JOIN scoring_runs sruns ON 0
                LEFT JOIN economic_entities ee ON 0
                LEFT JOIN registry r ON 0
                WHERE 0
            """)

            # Validate critical runtime indexes
            _validate_critical_indexes(conn)

            # Validate provenance integrity triggers (migration 18)
            _validate_provenance_triggers(conn)

            return  # All critical columns + indexes + triggers present
        except (sqlite3.OperationalError, _SchemaIntegrityError):
            logger.info("Sentinel check failed; running full schema reconciliation")

    source_run_columns = {
        "duplicate_documents": "INTEGER DEFAULT 0",
        "candidates_generated": "INTEGER DEFAULT 0",
        "unconfirmed_matches": "INTEGER DEFAULT 0",
        "events_created": "INTEGER DEFAULT 0",
        "duplicate_events": "INTEGER DEFAULT 0",
        "errors": "INTEGER DEFAULT 0",
        "status": "TEXT NOT NULL DEFAULT 'success'",
        "error_detail_json": "TEXT",
    }
    for column, definition in source_run_columns.items():
        _ensure_column(conn, "source_runs", column, definition)

    for column, definition in {
        "active": "INTEGER NOT NULL DEFAULT 1",
        "status": "TEXT NOT NULL DEFAULT 'active'",
        "tombstone_reason": "TEXT",
        "tombstoned_at": "TEXT",
    }.items():
        _ensure_column(conn, "events", column, definition)

    for column, definition in {
        "input_fingerprint": "TEXT",
        "provenance_json": "TEXT",
        "run_id": "TEXT",
        "scorer_version": "TEXT",
    }.items():
        _ensure_column(conn, "scores", column, definition)

    _ensure_column(conn, "economic_entities", "registry_id", "INTEGER REFERENCES registry(id)")
    _ensure_column(conn, "registry", "siren", "TEXT")
    _ensure_column(conn, "registry", "spanish_tax_id", "TEXT")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scoring_runs (
            run_id TEXT PRIMARY KEY,
            input_fingerprint TEXT NOT NULL,
            scorer_version TEXT NOT NULL,
            provenance_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(input_fingerprint, scorer_version)
        );
        CREATE INDEX IF NOT EXISTS idx_scoring_runs_fingerprint
            ON scoring_runs(input_fingerprint);
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
        -- Legacy source adapters could enqueue the same unresolved candidate
        -- repeatedly before the canonical partial unique index existed. Keep
        -- the oldest row auditable and resolve later copies as duplicates.
        UPDATE unconfirmed_match_queue
           SET status = 'ignored',
               resolution = 'duplicate',
               reviewed_by = 'migration-15',
               reviewed_at = COALESCE(reviewed_at, datetime('now')),
               updated_at = datetime('now')
         WHERE resolution IS NULL
           AND id NOT IN (
               SELECT MIN(id)
                 FROM unconfirmed_match_queue
                WHERE resolution IS NULL
                GROUP BY source_name, source_entity_name, candidate_registry_id
           );
        CREATE INDEX IF NOT EXISTS idx_source_runs_run ON source_runs(run_id);
        CREATE INDEX IF NOT EXISTS idx_source_runs_source ON source_runs(source_name);
        CREATE INDEX IF NOT EXISTS idx_source_runs_completed ON source_runs(completed_at);
        CREATE INDEX IF NOT EXISTS idx_unconfirmed_queue_status ON unconfirmed_match_queue(status);
        CREATE INDEX IF NOT EXISTS idx_unconfirmed_queue_source ON unconfirmed_match_queue(source_name);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unconfirmed_dedup
            ON unconfirmed_match_queue(source_name, source_entity_name, candidate_registry_id)
            WHERE resolution IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_registry_siren_exact
            ON registry(siren) WHERE siren IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_registry_spanish_tax_id_exact
            ON registry(spanish_tax_id) WHERE spanish_tax_id IS NOT NULL;

        -- Provenance integrity triggers (migration 18)
        DROP TRIGGER IF EXISTS trg_scores_validate_run_id_insert;
        CREATE TRIGGER trg_scores_validate_run_id_insert
        BEFORE INSERT ON scores
        WHEN NEW.run_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT,
                'Scoring provenance integrity violation: run_id has no matching scoring_runs row')
            WHERE (SELECT COUNT(*) FROM scoring_runs WHERE run_id = NEW.run_id) = 0;
        END;

        DROP TRIGGER IF EXISTS trg_scores_validate_run_id_update;
        CREATE TRIGGER trg_scores_validate_run_id_update
        BEFORE UPDATE ON scores
        WHEN NEW.run_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT,
                'Scoring provenance integrity violation: run_id has no matching scoring_runs row')
            WHERE (SELECT COUNT(*) FROM scoring_runs WHERE run_id = NEW.run_id) = 0;
        END;
    """)
    # Repair scoring_runs table shape in case it exists but with wrong columns
    # (e.g. missing provenance_json on a partially-created table)
    _repair_scoring_runs_shape(conn)
    conn.execute("UPDATE events SET active = 1 WHERE active IS NULL")
    conn.execute("UPDATE events SET status = 'active' WHERE status IS NULL")
    conn.commit()


def _provenance_equal(a: str | None, b: str | None) -> bool:
    """Compare provenance payloads. NULL and non-NULL are distinct."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return a == b


def _split_sql(script: str) -> list[str]:
    """Split a SQL script into individual statements on semicolons.

    Handles CREATE TRIGGER ... BEGIN ... END; blocks that contain
    semicolons inside RAISE(ABORT, ...) and other trigger-body statements.
    """
    statements: list[str] = []
    current: list[str] = []
    in_trigger = False

    for line in script.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            current.append(line)
            continue

        # Track CREATE TRIGGER ... BEGIN blocks
        if not in_trigger and re.match(
            r"CREATE\s+TRIGGER\s+", stripped, re.IGNORECASE
        ):
            in_trigger = True
        current.append(line)

        # A semicolon at end of line closes the current statement
        # (or the trigger block, if we're inside one)
        if stripped.rstrip().endswith(";"):
            if in_trigger:
                # Inside a trigger: only END; closes the trigger
                if re.match(r"END\s*;\s*$", stripped, re.IGNORECASE):
                    in_trigger = False
                    stmt = "\n".join(current).strip()
                    if stmt:
                        statements.append(stmt)
                    current = []
                # else: line ends with ; but we're still inside the trigger
                # (e.g., RAISE(ABORT, ...); inside the body)
            else:
                stmt = "\n".join(current).strip()
                if stmt:
                    statements.append(stmt)
                current = []

    # Catch any trailing non-closed statement
    remaining = "\n".join(current).strip()
    if remaining:
        statements.append(remaining)

    return statements


def migrate(conn: sqlite3.Connection | None = None) -> int:
    """Apply pending migrations. Returns the current schema version."""
    close_after = conn is None
    if conn is None:
        conn = get_db()

    try:
        # Create schema_version table if it doesn't exist (first run)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()

        current = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0] or 0

        for version in sorted(MIGRATIONS.keys()):
            if version > current:
                logger.info(f"Applying migration v{version}")

                if version == 18:
                    # Migration 18 must be fully atomic: SQL triggers +
                    # Python provenance backfill + schema_version row all
                    # commit together, or roll back entirely leaving the DB
                    # at version < 18.  executescript cannot be used because
                    # it issues implicit COMMITs that break the transaction.
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        for stmt in _split_sql(MIGRATIONS[18]):
                            conn.execute(stmt)
                        _migrate_18_scoring_provenance(conn)
                        conn.execute(
                            "INSERT INTO schema_version (version) VALUES (?)",
                            (version,),
                        )
                        conn.commit()
                        current = version
                    except Exception:
                        conn.rollback()
                        raise
                else:
                    try:
                        conn.executescript(MIGRATIONS[version])
                    except sqlite3.OperationalError as e:
                        if "duplicate column name" in str(e):
                            logger.info(
                                f"Migration v{version} already applied (column exists), "
                                f"marking as done"
                            )
                        else:
                            raise
                    conn.execute(
                        "INSERT INTO schema_version (version) VALUES (?)",
                        (version,),
                    )
                    conn.commit()
                    current = version

        _reconcile_runtime_schema(conn)
        return current
    finally:
        if close_after:
            conn.close()


def record_journal(conn: sqlite3.Connection, action: str, details: str) -> None:
    """Append a journal entry to the database for durable audit trail."""
    conn.execute(
        "INSERT INTO journal (timestamp, action, details) VALUES (?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), action, details),
    )
    conn.commit()
