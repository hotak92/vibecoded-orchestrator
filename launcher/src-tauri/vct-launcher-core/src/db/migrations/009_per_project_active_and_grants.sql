-- 0.2.1: per-project active gate + per-project secret grants.
--
-- Two related schema changes packaged together because they share a
-- migration window for the secrets backend rework. The hub resolver
-- and the secrets_cmd readers are updated in a follow-up commit; this
-- migration is purely additive — applying it without the resolver
-- changes leaves behaviour identical to 0.2.0.
--
-- ─── Change 1: per-(secret × requester_project) active flag ─────────
--
-- The existing secret_active_state table keys on (scope, project_id,
-- module_id, key). `project_id` there names the OWNING scope —
-- `_user_shared_` for shared, `_global_` for global, or the literal
-- project id for per-project secrets. The boolean `active` was global:
-- pausing a shared secret turned it off for every project that could
-- see it.
--
-- New shape adds `requester_project_id` to the key, so a single shared
-- secret can be active for project A and paused for project B. The
-- column carries one of:
--   * literal project id of the requester
--   * `*` sentinel meaning "applies to every requester unless a more
--     specific row overrides it"
--
-- Resolver semantics: when answering "is this secret active for
-- project P?", look up
--    (scope, project_id, module_id, key, requester_project_id=P)
-- first; if absent, fall back to
--    (scope, project_id, module_id, key, requester_project_id='*').
-- If neither row exists, treat as inactive (default-deny on first
-- registration of a secret without an explicit row).
--
-- Default-on-new-project (per design decision 2026-05-10):
--   * Global / Shared: active for newly added projects → existing
--     rows get a `*` sentinel row, no per-project rows needed.
--   * Per-project (own scope): active only for the owning project →
--     literal owner row, no `*` sentinel.
--   * Per-project (granted to other projects): see secret_grants
--     table below; the grant grid carries its own active flag, again
--     defaulting to true and toggle-able by the grantee project.
--
-- ─── Change 2: secret_grants — per-project secret sharing ───────────
--
-- A new table that maps (owner-project secret) → (grantee-project
-- read access). The hub resolver's "can project P read this secret?"
-- check expands from "P == owner_project_id" to "P == owner OR
-- exists row in secret_grants(owner, P)". The grantee can opt itself
-- out via the per-(secret × requester_project) active flag in
-- secret_active_state above; the grant entry stays so the launcher
-- GUI can show "B has paused this grant" without losing the
-- relationship. Only the owner can revoke the grant outright.

PRAGMA foreign_keys = OFF;

-- ── Step 1: rebuild secret_active_state with requester_project_id ──

CREATE TABLE secret_active_state_v2 (
    scope                 TEXT NOT NULL                  -- 'per_project'|'shared'|'global'
                          CHECK (scope IN ('per_project','shared','global')),
    project_id            TEXT NOT NULL,                  -- owning scope: real id OR '_user_shared_'/'_global_'
    module_id             TEXT NOT NULL,
    key                   TEXT NOT NULL,
    requester_project_id  TEXT NOT NULL,                  -- project asking, OR '*' sentinel for "any"
    active                INTEGER NOT NULL DEFAULT 1,     -- 1=visible; 0=paused
    updated_at            INTEGER NOT NULL,
    PRIMARY KEY (scope, project_id, module_id, key, requester_project_id)
);

-- Backfill from the v1 table. Decision rules for the requester column:
--   * shared/global rows → '*' sentinel (active for every requester)
--   * per_project rows   → owner project_id literally (active only for owner)
INSERT INTO secret_active_state_v2
    (scope, project_id, module_id, key, requester_project_id, active, updated_at)
SELECT
    scope,
    project_id,
    module_id,
    key,
    CASE
        WHEN scope IN ('shared', 'global') THEN '*'
        ELSE project_id
    END AS requester_project_id,
    active,
    updated_at
FROM secret_active_state;

DROP TABLE secret_active_state;

ALTER TABLE secret_active_state_v2 RENAME TO secret_active_state;

CREATE INDEX IF NOT EXISTS idx_secret_active_state_active
    ON secret_active_state(active);

-- Helper index for the resolver's two-step lookup pattern (specific
-- requester first, '*' fallback second). Both rows live under the same
-- (scope, project_id, module_id, key) prefix so the index covers both
-- queries from one B-tree walk.
CREATE INDEX IF NOT EXISTS idx_secret_active_state_lookup
    ON secret_active_state(scope, project_id, module_id, key, requester_project_id);

-- ── Step 2: secret_grants table ───────────────────────────────────────
--
-- Owner-side: only owner_project_id can INSERT or DELETE rows here
-- (enforced in the Tauri command layer, not the DB).
-- Grantee-side: the grantee can opt itself out via secret_active_state
-- but cannot DELETE the grant row itself.

CREATE TABLE IF NOT EXISTS secret_grants (
    -- Pointer to the owning secret. (scope, project_id, module_id, key)
    -- mirrors the natural key shape used everywhere else for secrets.
    -- `scope` is always 'per_project' here in 0.2.1 — granting global
    -- or shared secrets is meaningless (they're already cross-project).
    -- The CHECK leaves room for a future extension (e.g. granting a
    -- shared secret with extra restrictions) without a schema migration.
    scope                TEXT NOT NULL CHECK (scope IN ('per_project')),
    owner_project_id     TEXT NOT NULL,
    module_id            TEXT NOT NULL,
    key                  TEXT NOT NULL,

    -- Grantee project — must be a real project id (no sentinels here).
    -- Application enforces the distinct-from-owner check.
    grantee_project_id   TEXT NOT NULL,

    -- Bookkeeping for GUI display + audit log lookups.
    granted_at           INTEGER NOT NULL,
    granted_by_actor     TEXT,                              -- nullable; actor schema lives in 004
    note                 TEXT,                              -- optional human-readable label

    PRIMARY KEY (scope, owner_project_id, module_id, key, grantee_project_id),

    CHECK (owner_project_id <> grantee_project_id)
);

CREATE INDEX IF NOT EXISTS idx_secret_grants_grantee
    ON secret_grants(grantee_project_id);

CREATE INDEX IF NOT EXISTS idx_secret_grants_owner
    ON secret_grants(owner_project_id);

PRAGMA foreign_keys = ON;
