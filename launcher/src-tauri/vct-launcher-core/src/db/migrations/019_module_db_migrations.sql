-- SPDX-License-Identifier: AGPL-3.0-or-later
-- launcher.db — module-shipped DB migrations tracking (migration 019, v0.2.31)
--
-- Records every SQL migration file applied on behalf of a paid / community
-- module via the generic mechanism in
-- `vct_launcher_core::db::module_db_migrations`. The launcher applies a
-- module's `db/[0-9]+_*.sql` files at install + update + manual repair time,
-- and tracks them here keyed by (module_id, filename) with the SHA256 of
-- the file content captured at apply time.
--
-- WHY a single launcher-owned tracking table (vs one per module):
--   * The launcher is the actor running the migrations; it needs cross-
--     module visibility ("what migrations from any module have I applied?")
--     for repair / audit surfaces.
--   * One file = ONE row at a single primary-key slot. Easier to reason
--     about than per-module sidecar tables that the launcher would also
--     have to manage.
--   * The namespace-collision soft check (v0.2.31 supplemental) needs to
--     query across modules: "does any OTHER module own this namespace?"
--     A per-module table would force us to scan every module's table,
--     which doesn't compose.
--
-- SHA256 captures the file content at the moment the launcher ran it.
-- Idempotent re-runs (same file, same sha) skip silently. Sha mismatch
-- on a previously-applied filename means the module mutated a shipped
-- migration — we REFUSE with a structured error pointing at the offender.
-- The fix-pattern is "ship a NEW migration file, don't mutate an old one"
-- (standard forward-only-migrations discipline).
--
-- `namespace` is denormalised from the manifest at apply time so the
-- collision check (`apply_module_db_migrations` pre-check) is a single
-- indexed lookup. Multiple files from the same module share the same
-- namespace — that's fine; the query for collisions is DISTINCT-on
-- (namespace, module_id).
--
-- No FK to `module_installs`: deliberately. Migrations survive uninstall
-- so the user can re-install a module and skip already-applied schema
-- work. The cleanup path (full module purge + DB reset) is a future
-- v0.2.32+ surface and will DELETE these rows explicitly.
--
-- Forward-only, idempotent (gated by `_schema_migrations` version).

CREATE TABLE IF NOT EXISTS module_db_migrations (
    module_id     TEXT NOT NULL,
    filename      TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    namespace     TEXT NOT NULL,                 -- denormalised from manifest.db.namespace
    applied_at    INTEGER NOT NULL,              -- unix epoch millis
    PRIMARY KEY (module_id, filename)
);

CREATE INDEX IF NOT EXISTS idx_module_db_migrations_module
    ON module_db_migrations (module_id);

-- Used by the namespace-collision soft check at apply time:
-- `SELECT DISTINCT module_id FROM module_db_migrations
--    WHERE namespace = ? AND module_id != ?`
CREATE INDEX IF NOT EXISTS idx_module_db_migrations_namespace
    ON module_db_migrations (namespace);

-- v0.2.31 token registry — per-(module_id, project_id) shared secret used
-- by the hub's `/api/v1/modules/{module_id}/db/projects/{project_id}/rows/...`
-- routes for bearer-token auth. The launcher generates a random 32-byte
-- secret at install / first-use time, stores the hex-encoded form here,
-- and threads it to the container via `VCT_MODULE_TOKEN` env var.
--
-- Why per-(module, project) and not per-module: a single module can be
-- installed in multiple projects, each with its own container instance.
-- Scoping the token to a specific (module, project) pair lets the hub
-- enforce "this token can only touch project X" without consulting the
-- module_installs catalog on every request.
--
-- expires_at: unix-millis. Default TTL on issue is 1h (3_600_000 ms).
-- The container can refresh via `POST /modules/{id}/token/refresh` (auth
-- with the current near-expiry token) — that endpoint generates a fresh
-- secret + replaces the row + returns the new value. v0.2.32 will swap
-- this to JWT-signed claims; the row-replace mechanism stays the same.
--
-- No FK to projects(id): tokens may briefly outlive a project deletion
-- if cleanup races (hub still has the token cached in memory). On the
-- next request the hub looks up (module_id, project_id) and finds no
-- row → 401. Self-healing.
CREATE TABLE IF NOT EXISTS module_access_tokens (
    module_id      TEXT NOT NULL,
    project_id     TEXT NOT NULL,
    token_secret   TEXT NOT NULL,                -- hex-encoded random 32 bytes
    issued_at      INTEGER NOT NULL,             -- unix epoch millis
    expires_at     INTEGER NOT NULL,             -- unix epoch millis
    PRIMARY KEY (module_id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_module_access_tokens_module
    ON module_access_tokens (module_id);
