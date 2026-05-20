-- launcher.db — module_weights_state (migration 016, Phase 3C)
--
-- Adds a per-(project × module × embedding_source) state row tracking:
--   * the local weights version currently active for that module
--   * the last time we polled /rl-latest-version (for rate-limit display)
--   * the last time we successfully fine-tuned on local data
--
-- WHY: phase 3C of the Pro-tier RL Reranker release. The launcher polls
-- /rl-latest-version daily; on a new version detected, downloads the
-- .pt + prompts the user (Phase 4A) → fine-tune now / use unmodified /
-- skip. We need to know what version is locally active to compare
-- against the server's `latest_version` and to drive the dashboard's
-- "current version / last checked / last fine-tuned" widget (Phase 4B).
--
-- KEY SHAPE: (project_id, module_id, embedding_source). Per-embedding-
-- source NN weights → one row per source per (project × module). The
-- manifest's ACTIVE_EMBEDDING setting picks WHICH source's weights the
-- container loads; other sources sit dormant until the user switches.
--
-- EMBEDDING-SOURCE FLEXIBILITY: stored as `TEXT` (not a CHECK-enum). We
-- accept any string today ("qwen3", "arctic", "openai") and into the
-- future (jina, custom). The container picks the right .pt file based
-- on the ACTIVE_EMBEDDING env var; the launcher just persists state.
--
-- FK semantics:
--   * project_id → projects.id ON DELETE CASCADE: the row dies with its
--     project (no orphans).
--
-- (We don't add a second FK to module_installs because that table
-- already cascades from projects.id via its own FK — when a project
-- goes away, all its module_installs go too, and so do the weights
-- state rows by the project_id FK above. A composite FK to
-- module_installs(project_id, module_id) would also require a UNIQUE
-- constraint on that pair which migration 001 already has via the
-- primary-key shape, but the simpler single-FK is enough for the
-- "no orphans when a project is deleted" invariant we need.)
--
-- NULL handling: `version` and the two timestamps default to '' / 0 so
-- callers don't have to coalesce. `last_checked_at = 0` ⇒ "never
-- polled"; `last_finetuned_at = 0` ⇒ "never fine-tuned".
--
-- Forward-only, idempotent.

CREATE TABLE IF NOT EXISTS module_weights_state (
    project_id        TEXT NOT NULL,
    module_id         TEXT NOT NULL,
    embedding_source  TEXT NOT NULL,
    version           TEXT NOT NULL DEFAULT '',
    last_checked_at   INTEGER NOT NULL DEFAULT 0,
    last_finetuned_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, module_id, embedding_source),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mws_project ON module_weights_state(project_id);
CREATE INDEX IF NOT EXISTS idx_mws_module ON module_weights_state(module_id);
