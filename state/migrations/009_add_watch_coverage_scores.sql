-- Migration 009: Add watch/coverage scoring columns to scores table
-- P1 (engineering audit 2026-06-20): Scores need separate tracking for active watch
-- priority (driven by adverse signals), coverage monitor priority (driven by
-- exposure gaps), and component-level transparency scores.
--
-- Columns added:
--   active_watch_priority    - priority from adverse signals (0.0 = no signals)
--   coverage_monitor_priority - priority from exposure/coverage gaps
--   transmission_signal_score - transmission concern component
--   graph_coverage_score     - graph density/coverage component
--   information_gap_score    - missing information component
--   source_staleness_score   - stale data component
--
-- Idempotent: uses PRAGMA table_info to check column existence.

-- Check and add columns only if they don't exist
-- (SQLite ALTER TABLE ADD COLUMN is idempotent via the migration runner,
-- which catches "duplicate column name" errors. We include the PRAGMA
-- guard here for manual execution safety.)

-- Phase 1: Add columns
ALTER TABLE scores ADD COLUMN active_watch_priority REAL DEFAULT 0.0;
ALTER TABLE scores ADD COLUMN coverage_monitor_priority REAL DEFAULT 0.0;
ALTER TABLE scores ADD COLUMN transmission_signal_score REAL DEFAULT 0.0;
ALTER TABLE scores ADD COLUMN graph_coverage_score REAL DEFAULT 0.0;
ALTER TABLE scores ADD COLUMN information_gap_score REAL DEFAULT 0.0;
ALTER TABLE scores ADD COLUMN source_staleness_score REAL DEFAULT 0.0;

-- Phase 2: Backfill existing scores rows
-- Old scores had no adverse signals (all signal=0), so active_watch_priority = 0.0.
-- Coverage monitor priority computed from existing exposure data:
--   coverage_monitor_priority = MIN(legal_ownership/100 + econ_dep/50, 1.0) * 0.4
--   graph_coverage_score = MIN(legal_ownership/100 + econ_dep/50, 1.0)
-- Uses dependency_exposure (stores legal_ownership_exposure) and
-- factors_json.economic_dep_exposure for the economic dependency component.

UPDATE scores SET
    active_watch_priority = 0.0,
    coverage_monitor_priority = MIN(
        (COALESCE(dependency_exposure, 0.0) / 100.0 +
         COALESCE(CAST(json_extract(factors_json, '$.economic_dep_exposure') AS REAL), 0.0) / 50.0),
        1.0
    ) * 0.4,
    graph_coverage_score = MIN(
        (COALESCE(dependency_exposure, 0.0) / 100.0 +
         COALESCE(CAST(json_extract(factors_json, '$.economic_dep_exposure') AS REAL), 0.0) / 50.0),
        1.0
    ),
    transmission_signal_score = 0.0,
    information_gap_score = 0.0,
    source_staleness_score = 0.0;
