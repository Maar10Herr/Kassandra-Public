-- Migration 011: Add classifier_runs table for per-source yield tracking
-- Answers: '584 documents, 0 events' → meaningful per-source denominator reporting
--
-- Tracks per-source: documents discovered, fetched, with-text, classified,
-- candidate pattern hits, rejections, accepted events, false positive checks.
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
