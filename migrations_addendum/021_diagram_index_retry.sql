-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (c) 2026 VibeCoded Tools
--
-- Phase 1.5.A addendum to migration 021 (Phase 1.1).
--
-- The main body of migration 021 is owned by Phase 1.1 (Diagrams DB) —
-- it creates the `project_diagrams` table + the `is_module_active`
-- helper. This file holds the SECOND table that Phase 1.5.A's indexer
-- writes to: a small retry queue for Weaviate upsert failures.
--
-- Integration: the Phase 1.1 integrator should fold this CREATE TABLE
-- statement into the bottom of `020_<...>.sql`'s successor (the
-- canonical 021 migration). DO NOT ship this file separately — it
-- exists only as a coordination artifact across parallel worktrees.
-- Once folded in, delete `migrations_addendum/021_diagram_index_retry.sql`
-- and the indexer's `_RETRY_SCHEMA_SQL` fallback in
-- `vco_lib/diagram_indexer.py` becomes redundant (defensive — keep
-- the fallback `CREATE TABLE IF NOT EXISTS` so the indexer remains
-- resilient if the migration didn't run yet on a given install).
--
-- Columns:
--   id                — autoincrement primary key
--   project_id        — projects(id) FK; left soft (no FK constraint
--                       so we don't cascade-delete retry rows when the
--                       project row goes through a rename-recreate cycle)
--   file_path         — absolute path on disk (the indexer always
--                       resolves before storing)
--   error             — last Weaviate error message (truncated to
--                       1KB at write time by the indexer)
--   attempt_count     — incremented by the retry sweeper before each
--                       attempt (the indexer always writes 0)
--   next_attempt_at   — unix epoch; sweeper reads rows where
--                       next_attempt_at <= NOW(). Exponential backoff
--                       computed by the sweeper, not stored separately.
--   last_error_at     — unix epoch of the most recent error log line
--
-- Index: `idx_diagram_retry_next` powers the sweeper's
-- "rows due for retry" query (`WHERE next_attempt_at <= ?`).

CREATE TABLE IF NOT EXISTS diagram_index_retry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    error TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL,
    last_error_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_diagram_retry_next
    ON diagram_index_retry(next_attempt_at);
