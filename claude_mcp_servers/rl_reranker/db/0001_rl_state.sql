-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (c) 2026 VibeCoded Tools
-- vct-rl-reranker module — migration 0001
-- Per-(project, module) live state cache.
--
-- Caches the data the container exposes via GET /state_summary (vct-rl-reranker
-- v0.2.3+) so the launcher's dashboard widget can render without a per-render
-- HTTP round-trip to a possibly-stopped container.
--
-- Owner: vct-rl-reranker (namespace "rl").
-- Schema applied by launcher's module-shipped DB migration pipeline (launcher
-- v0.2.31+). Launcher refuses tables outside this module's namespace.
--
-- Read path: launcher dashboard widget, via vct-hub
--   GET /api/v1/modules/vct-rl-reranker/db/projects/{project_id}/rows/rl_state/{project_id}
-- Write path: container's RLServer writes a row on:
--   - container start (after RLModel.load_or_create returns)
--   - every RLModel.save() (catches dynamic-type registrations + arch changes)
-- Soft-fails when hub unreachable; container in-memory state is the truth and
-- the launcher widget is allowed to show stale-with-timestamp.
--
-- Forward-only. SHA256 of this file pinned in module_db_migrations on apply;
-- mutation is forbidden — ship a new 0002+ file to change anything.

CREATE TABLE IF NOT EXISTS rl_state (
    project_id          TEXT    NOT NULL,
    refreshed_at        INTEGER NOT NULL DEFAULT 0,         -- unix ts; 0 = never refreshed
    builtin_types       INTEGER NOT NULL DEFAULT 9,         -- N_ENTITY_TYPES = 6 builtins + 3 sentinels
    dynamic_types       INTEGER NOT NULL DEFAULT 0,         -- count of registry entries with idx >= 9
    marker_present      INTEGER NOT NULL DEFAULT 0,         -- 0/1: state/.types_layout_v2 exists
    sidecar_dir_present INTEGER NOT NULL DEFAULT 0,         -- 0/1: state/type_embs/ is a directory
    model_loaded        INTEGER NOT NULL DEFAULT 0,         -- 0/1: RLModel.__init__ completed
    arch_version        TEXT,                                -- "v2" (D1' sidecar + cos3heads_all_silu) / etc.
    emb_dim             INTEGER,                             -- 1024 today
    active_embedding    TEXT,                                -- "arctic" / "qwen3" / "openai"
    PRIMARY KEY (project_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
