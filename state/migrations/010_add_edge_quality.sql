-- Migration 010: Edge quality tiers, transmission gating, duplicate edge dedup
-- Adds quality_tier, quality_score, dedup_key, is_derived_reverse, canonical_edge_id
-- to edges table. Backfills existing edges with source-specific tier assignments.

-- Phase 1: Add columns
ALTER TABLE edges ADD COLUMN quality_tier TEXT DEFAULT 'T4_INFERRED';
ALTER TABLE edges ADD COLUMN quality_score REAL DEFAULT 0.35;
ALTER TABLE edges ADD COLUMN dedup_key TEXT;
ALTER TABLE edges ADD COLUMN is_derived_reverse INTEGER DEFAULT 0;
ALTER TABLE edges ADD COLUMN canonical_edge_id INTEGER;

-- Phase 2: Create unique index on dedup_key (partial index, nulls not indexed)
CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_dedup ON edges(dedup_key) WHERE dedup_key IS NOT NULL;

-- Phase 3: Backfill quality tier and score by source
-- gleif edges → T2_REGISTRY
UPDATE edges SET quality_tier = 'T2_REGISTRY', quality_score = 0.65 WHERE edge_source = 'gleif';

-- annual_report edges → T3_TRUSTED_THIRD_PARTY
UPDATE edges SET quality_tier = 'T3_TRUSTED_THIRD_PARTY', quality_score = 0.45 WHERE edge_source = 'annual_report';

-- eprtr edges → T1_OFFICIAL
UPDATE edges SET quality_tier = 'T1_OFFICIAL', quality_score = 0.85 WHERE edge_source = 'eprtr';

-- ted edges → T1_OFFICIAL
UPDATE edges SET quality_tier = 'T1_OFFICIAL', quality_score = 0.85 WHERE edge_source = 'ted';

-- manual_pilot edges → T3_TRUSTED_THIRD_PARTY
UPDATE edges SET quality_tier = 'T3_TRUSTED_THIRD_PARTY', quality_score = 0.55 WHERE edge_source = 'manual_pilot';

-- gazette edges → T2_REGISTRY
UPDATE edges SET quality_tier = 'T2_REGISTRY', quality_score = 0.70 WHERE edge_source = 'gazette';

-- web_monitor edges → T4_INFERRED
UPDATE edges SET quality_tier = 'T4_INFERRED', quality_score = 0.30 WHERE edge_source = 'web_monitor';

-- NULL/unknown source → T4_INFERRED
UPDATE edges SET quality_tier = 'T4_INFERRED', quality_score = 0.25 WHERE edge_source IS NULL;

-- Phase 4: Compute dedup_key for existing edges (include id to avoid collisions on pre-existing duplicates)
UPDATE edges SET dedup_key = source_registry_id || '|' || target_registry_id || '|' || relationship_type || '|' || COALESCE(edge_source, 'unknown') || '|' || id WHERE dedup_key IS NULL;
