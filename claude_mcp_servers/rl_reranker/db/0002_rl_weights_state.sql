-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (c) 2026 VibeCoded Tools
-- vct-rl-reranker module — migration 0002
-- Per-(project, embedding_source) active checkpoint state.
--
-- Replaces launcher's legacy `module_weights_state` table (launcher migration
-- 016 — dropped by launcher migration 020 in v0.2.31 since no production users
-- exist on the prior shape; no backfill needed). Same logical purpose, RL-owned.
--
-- Owner: vct-rl-reranker (namespace "rl").
--
-- Read path: launcher dashboard widget renders `weights_version` from
-- local_version + `last_training` from last_finetuned_at. Via vct-hub
--   GET /api/v1/modules/vct-rl-reranker/db/projects/{project_id}/rows/rl_weights_state
-- Write path: container only.
--   - /rotate_weights handler: UPDATEs local_version, local_path,
--     local_arch_version, last_rotated_at.
--   - /finetune handler on completion: UPDATEs last_finetuned_at.
--   - container startup: INSERTs initial row with the shipped reference
--     checkpoint's version + path (treats startup as first rotation).
-- Launcher's signal_finetune / apply_weights_update Tauri commands are pure
-- orchestration (download .pt + call container); they do NOT write this table.
-- Container is sole writer — no race window.
--
-- Composite key (project_id, embedding_source) supports future multi-embedder
-- installs where one project's reranker has separate networks per source.

CREATE TABLE IF NOT EXISTS rl_weights_state (
    project_id           TEXT    NOT NULL,
    embedding_source     TEXT    NOT NULL,                  -- "arctic" / "qwen3" / "openai" / etc.
    local_version        TEXT    NOT NULL DEFAULT '',       -- semver of the .pt currently loaded
    local_arch_version   TEXT,                              -- "v2" / future arches
    local_path           TEXT,                              -- absolute path on host
    last_checked_at      INTEGER NOT NULL DEFAULT 0,        -- last poll of rl-latest-weights
    last_finetuned_at    INTEGER NOT NULL DEFAULT 0,        -- last successful local finetune
    last_rotated_at      INTEGER NOT NULL DEFAULT 0,        -- last successful /rotate_weights
    PRIMARY KEY (project_id, embedding_source),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
