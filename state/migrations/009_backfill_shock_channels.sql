-- Migration 009: Backfill shock channel attributes for all existing edges
-- P1 (engineering audit 2026-06-20): All edges need shock_channel, lag_bucket, buffer_proxy,
-- replaceability, and switching_time_bucket populated from deterministic mapping.
-- Run via Python: kassandra migrate 009  (uses shock_channel.py populate_edge_attributes)

-- SQL part: ensure replaceability_unknown_reason column exists (added in v6 migration)
-- The Python backfill handles the actual population via populate_edge_attributes()
