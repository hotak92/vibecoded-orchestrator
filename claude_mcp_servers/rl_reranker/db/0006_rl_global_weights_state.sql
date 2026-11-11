-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (c) 2026 VibeCoded Tools
-- vct-rl-reranker module — migration 0006 (v0.2.8)
-- Per-embedding-source state of the CURRENTLY-LOADED global checkpoint.
--
-- Distinct from rl_global_weights_available (migration 0004), which is the
-- upstream-catalog cache populated by the launcher's daily rl-latest-weights
-- poll (i.e. "what's available to download"). This table tracks "what global
-- checkpoint is loaded in the container's in-memory model RIGHT NOW per
-- embedding_source" — the active source-of-truth for projects with
-- rl_use_global=1.
--
-- Without this row, the dashboard for is_using_global projects has nowhere to
-- read the active global version from (the per-project rl_weights_state row
-- shows the project's local checkpoint, which is stale once it switches to
-- global). With this row, the dashboard can render:
--   - Per-project: rl_weights_state[project, source].local_version / .last_finetuned_at
--   - Global slot: rl_global_weights_state[source].local_version / .last_rotated_at
--
-- Owner: vct-rl-reranker (namespace "rl").
--
-- Write path: container only.
--   - container startup: INSERTs a row if a global checkpoint is present on disk.
--   - /rotate_weights handler when the rotation targets a global slot (i.e.
--     the new .pt is under <state>/global/ rather than <state>/project/):
--     UPDATEs local_version, local_arch_version, local_path, last_rotated_at.
--
-- Read path: launcher dashboard widget → vct-hub
--   GET /api/v1/modules/vct-rl-reranker/db/projects/{project_id}/rows/rl_global_weights_state
--   (Note: project_id in this URL is just a routing slug; the table itself is
--   project-agnostic. The vct-hub module-DB router strips project_id from the
--   WHERE clause for tables that don't have it.)

CREATE TABLE IF NOT EXISTS rl_global_weights_state (
    embedding_source     TEXT    NOT NULL PRIMARY KEY,         -- "arctic" / "qwen3" / "openai" / etc.
    local_version        TEXT    NOT NULL DEFAULT '',          -- semver of the .pt currently active globally
    local_arch_version   TEXT,                                  -- "v2" / future arches
    local_path           TEXT,                                  -- absolute path on host
    last_loaded_at       INTEGER NOT NULL DEFAULT 0,            -- when this global checkpoint first became active
    last_rotated_at      INTEGER NOT NULL DEFAULT 0             -- last successful /rotate_weights into this slot
);
