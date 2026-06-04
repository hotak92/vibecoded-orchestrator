-- SPDX-License-Identifier: AGPL-3.0-or-later
-- launcher.db — RL retrieval/citation telemetry events (migration 025, v0.2.47)
--
-- Replaces the JSONL-on-disk telemetry corpus at
-- `~/.claude/retrieval_rl_data/rl_events.jsonl` with a queryable, indexed
-- SQLite table. The MCP-side telemetry writer (`claude_mcp_servers/rl_client/`)
-- POSTs each event to the hub's `POST /api/v1/rl/events` endpoint, which
-- calls `insert_rl_event` (rl_events.rs). The hub is the single writer;
-- Python clients never touch the SQLite file directly (preserves the
-- launcher's single-writer architectural rule documented at
-- `vco_lib/config_projection.py:488-491`).
--
-- WHY DB instead of JSONL (locked decision 2026-06-04):
--   * Cross-module queryability — the dashboard widget joins rl_events
--     against `projects` for per-project event-rate displays.
--   * Schema discipline — `payload_json` carries the full v3 event JSON
--     verbatim; the indexed columns let the dashboard filter without
--     parsing payloads. The same payload shape works for free + Pro
--     (no tier-gated code paths).
--   * Single source of truth for offline training — `offline_trainer` in
--     paid-modules/vct-rl-reranker reads via the hub's
--     `GET /api/v1/rl/events?event_type=... ` route (added in a follow-up
--     commit), NOT by re-opening the JSONL.
--
-- Migration policy for the existing 700 MB JSONL corpus:
--   A one-shot migration script (`vco_lib/migrate_rl_jsonl_to_db.py`,
--   commit C9 of this work) validates each line as v2/v3, drops broken/v1
--   rows, and POSTs the valid remainder via the hub. Original JSONL is
--   renamed to `.pre-db-migration.bak` rather than deleted.
--
-- FK semantics:
--   * project_id → projects.id ON DELETE CASCADE. Rows die with project.
--   * project_id is NULLABLE for free-tier rows: free-tier installs may
--     not have an orchestrator project registered yet (rl_events still
--     accumulate; project_name carries the workspace-folder slug for
--     human reading).
--
-- Forward-only, idempotent (the migrations runner gates by version in
-- `_schema_migrations`; this file is executed exactly once per DB).

CREATE TABLE IF NOT EXISTS rl_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 'retrieval' (query + nodes presented) or 'citation' (which nodes were used).
    event_type          TEXT NOT NULL,
    -- Schema version of the payload. Currently 3 (v0.2.47); the migrator
    -- preserves 2 for back-compat rows ported from the JSONL corpus.
    schema_version      INTEGER NOT NULL DEFAULT 3,
    -- Wall-clock millis (matches the rest of launcher.db convention).
    ts                  INTEGER NOT NULL,
    -- FK to projects(id) when the row was produced for an orchestrator
    -- project. NULL on free-tier writes that occurred before the user
    -- registered the project with the launcher.
    project_id          TEXT,
    project_name        TEXT,
    task_id             TEXT NOT NULL,
    task_type           TEXT,
    embedding_source    TEXT,
    embedding_dim       INTEGER,
    embedding_model     TEXT,
    -- Full v3 event JSON, verbatim. The indexed columns above are denormalized
    -- copies of fields that also appear inside payload_json, kept SQL-queryable
    -- for the dashboard widget. offline_trainer reads payload_json + parses.
    payload_json        TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- Hot indexes for the dashboard widget's expected query shapes.
CREATE INDEX IF NOT EXISTS idx_rl_events_task_id
    ON rl_events(task_id);
CREATE INDEX IF NOT EXISTS idx_rl_events_project_ts
    ON rl_events(project_id, ts);
CREATE INDEX IF NOT EXISTS idx_rl_events_event_type_ts
    ON rl_events(event_type, ts);
CREATE INDEX IF NOT EXISTS idx_rl_events_embedding_source_ts
    ON rl_events(embedding_source, ts);
