-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (c) 2026 VibeCoded Tools
-- vct-rl-reranker module — migration 0004 (v0.2.7)
-- Per-embedding-source cache of the latest available global weights.
--
-- Replaces in-memory state of the launcher's old poller for the
-- rl-latest-weights Supabase endpoint. Powers the v0.2.7+ dashboard's
-- "Update available" badge + the new "Download default weights" button
-- (R2 in rl-v0.2.6-post-install-feedback-2026-05-24.md).
--
-- Owner: vct-rl-reranker (namespace "rl").
--
-- Write path: the LAUNCHER (not the container) polls the rl-latest-weights
-- Supabase endpoint daily with the orchestrator-tier JWT in the user's
-- keychain. UPSERTs the latest-known version per embedding_source. When
-- the user clicks "Download default weights" via the new Tauri command
-- module_download_default_weights, the launcher fetches the .pt to
-- {RL_GLOBAL_WEIGHTS_DIR}, sets local_downloaded=1 + local_path. JWT stays
-- launcher-side; container never sees it. (Per C1 decision in
-- orchestrator-response-rl-v0.2.6-feedback-2026-05-24.md.)
--
-- Read path:
-- - Dashboard renders "v0.2.6 available since 3 days ago; downloaded? yes / no"
--   from this table × rl_weights_state.weights_version.
-- - Container reads at /reset (strategy="fork") to know which global
--   checkpoint to clone-from per embedding_source.
--
-- Composite PRIMARY KEY (embedding_source, latest_version): multiple
-- shipped versions per source coexist (historical record of what was
-- available). The "current latest" is whichever row has the highest
-- polled_at for a given embedding_source.

CREATE TABLE IF NOT EXISTS rl_global_weights_available (
    embedding_source     TEXT    NOT NULL,                  -- "arctic" / "qwen3" / "openai" / etc.
    latest_version       TEXT    NOT NULL,                  -- semver, mirrors rl-latest-weights response
    latest_image_tag     TEXT,                              -- e.g. "0.2.6-cpu"
    latest_image_digest  TEXT,                              -- sha256:... if reported
    download_url         TEXT,                              -- short-lived signed URL (don't cache long)
    download_url_expires INTEGER NOT NULL DEFAULT 0,        -- unix ts; 0 = unknown/never refreshed
    local_downloaded     INTEGER NOT NULL DEFAULT 0,        -- 0/1: have we pulled this version's .pt
    local_path           TEXT,                              -- absolute path (when local_downloaded=1)
    sha256               TEXT,                              -- expected sha; for integrity check
    polled_at            INTEGER NOT NULL DEFAULT 0,        -- last hit to rl-latest-weights
    release_notes_url    TEXT,
    PRIMARY KEY (embedding_source, latest_version)
);

CREATE INDEX IF NOT EXISTS idx_rl_global_weights_available_polled
    ON rl_global_weights_available (embedding_source, polled_at DESC);
