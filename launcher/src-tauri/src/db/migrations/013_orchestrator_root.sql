-- launcher.db — orchestrator-root as a first-class project (migration 013)
--
-- Extends the projects.host CHECK constraint from IN ('base','mao') to
-- IN ('base','mao','orchestrator_root'), enabling the orchestrator clone
-- itself to participate as a real `projects` row.
--
-- WHY: today the orchestrator clone (the directory containing
-- `vct-module.json`) is modelled OUTSIDE this table — via
-- `find_orchestrator_manifest()` walks + a path stored in launcher.toml.
-- That means the clone cannot participate in any FK-strict subsystem:
-- `codegraph_access`, `kg_collection_access`, `project_permissions`,
-- `project_kg_bindings`, `project_codegraph_bindings`,
-- `code_graph_builds`, `project_mcp_servers`, `kg_syncs`, `kg_summaries`
-- all reference `projects(id)` with `ON DELETE CASCADE`. After this
-- migration the launcher auto-registers a row with host='orchestrator_root'
-- at startup (one fixed slug 'orchestrator-root', UNIQUE-guarded), and
-- every FK-bearing subsystem can attach to it natively.
--
-- HOW: SQLite does NOT support ALTER TABLE DROP/ADD CHECK, so we use
-- the canonical 12-step create-copy-drop-rename pattern documented at
-- https://www.sqlite.org/lang_altertable.html#otheralter. The new CHECK
-- is a SUPERSET of the old one, so the copy step cannot reject any
-- existing row (rows with host='base' and host='mao' satisfy both
-- constraints).
--
-- Forward-only, idempotent (the migrations runner gates by version in
-- `_schema_migrations`; this file is only ever executed once per DB).
--
-- FK safety note (CRITICAL — this is what 0.2.10→0.2.11 actually
-- needs to get right):
--
-- The SQLite 12-step plan REQUIRES `PRAGMA foreign_keys = OFF` for the
-- duration of the rebuild. `PRAGMA defer_foreign_keys = ON` is NOT a
-- substitute — it only defers the integrity check until COMMIT but
-- does NOT change how `DROP TABLE projects` interacts with FK-bearing
-- child rows: with FK enforcement enabled, dropping the parent leaves
-- child FKs dangling (or, on some SQLite builds, fires the ON DELETE
-- CASCADE on every child row before the DROP commits — either way the
-- data in codegraph_access, kg_collection_access, project_permissions,
-- project_kg_bindings, project_codegraph_bindings, code_graph_builds,
-- project_mcp_servers, kg_syncs, kg_summaries is at risk).
--
-- Crucially, `PRAGMA foreign_keys` is a NO-OP inside a transaction
-- (SQLite ignores the change until the transaction completes). So the
-- pragma MUST be issued OUTSIDE the BEGIN/COMMIT block — first
-- statement of the migration. After the rebuild we re-enable
-- enforcement OUTSIDE the transaction, then run
-- `PRAGMA foreign_key_check` to verify every FK row still resolves
-- (returns empty when integrity is intact; rows otherwise).
--
-- rusqlite's `Connection::execute_batch` processes statements
-- sequentially split on `;`, so the OFF → BEGIN → ... → COMMIT → ON →
-- foreign_key_check sequence below executes in order on a single
-- connection. That connection is the same one held by `Db::open()`
-- and the same one all migrations run on, so the pragma flip is
-- guaranteed to bracket only the rebuild.
--
-- After this migration commits and foreign_keys returns to ON, the
-- migrations runner's next statement (the INSERT into
-- _schema_migrations) and every subsequent launcher operation see FKs
-- as enabled — identical to the pre-migration state.
--
-- Note: SQLite's `PRAGMA foreign_keys` is connection-scoped, not DB-
-- scoped. Once this migration commits, the next `Db::open` call (or
-- any other launcher process attaching to launcher.db) sets
-- `foreign_keys=ON` in its own connection. The off→on toggle here is
-- invisible to any other connection that might be open at the same
-- moment, but since migrations only run during `Db::open` (before any
-- other code can attach), no concurrent connection can see the off
-- state.
--
-- Indexes:
--   - `idx_projects_host` (from migration 001) and `idx_projects_slug`
--     (from migration 003) are dropped with the old table. We recreate
--     them on the renamed table at the end of the migration so query
--     plans remain identical.

-- 0. Disable FK enforcement for the duration of the rebuild. MUST be
--    outside any transaction — SQLite ignores `foreign_keys` changes
--    issued inside BEGIN/COMMIT. Restored at the end of the migration.
PRAGMA foreign_keys = OFF;

BEGIN;

-- 1. New table with the extended CHECK. Column order + types are an
--    EXACT mirror of the post-003 `projects` schema (id, name,
--    folder_path, host, created_at, updated_at, slug). Keep them
--    aligned with the explicit-column INSERT below — if a future
--    migration adds a column it MUST land BEFORE this migration
--    (renumbered) or its own create-copy-drop-rename must replicate
--    the projects schema accurately.
CREATE TABLE projects_v2 (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    folder_path     TEXT NOT NULL UNIQUE,
    host            TEXT NOT NULL
                    CHECK (host IN ('base','mao','orchestrator_root')),
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    slug            TEXT
);

-- 2. Copy every existing row verbatim. New CHECK is a superset so no
--    row can be rejected here. Explicit column list (NOT `SELECT *`)
--    so reordering or adding columns to the source `projects` in a
--    future migration produces a compile-time-style failure (column
--    count mismatch) rather than silently copying into the wrong
--    columns. Both source and destination have the same 7 columns in
--    the same order after migration 003 (slug appended via ALTER
--    TABLE), so the list below matches the source layout.
INSERT INTO projects_v2 (id, name, folder_path, host, created_at, updated_at, slug)
SELECT id, name, folder_path, host, created_at, updated_at, slug
FROM projects;

-- 3. Drop the original. With `foreign_keys=OFF` (set above the
--    BEGIN), this DROP does not cascade-delete child rows in
--    codegraph_access / kg_collection_access / project_permissions /
--    project_kg_bindings / project_codegraph_bindings /
--    code_graph_builds / project_mcp_servers / kg_syncs /
--    kg_summaries; the child rows are preserved verbatim and their
--    FK pointers (foreign_key by NAME, not by ROWID in SQLite) are
--    rebound to the renamed table in step 4.
DROP TABLE projects;

-- 4. Rename the new table to claim the canonical name. After this,
--    every FK in the child tables resolves to the new `projects`
--    table; only the CHECK constraint differs.
ALTER TABLE projects_v2 RENAME TO projects;

-- 5. Recreate the indexes that lived on the old `projects` table:
--      * idx_projects_host (migration 001) — used by host-filter queries.
--      * idx_projects_slug (migration 003) — UNIQUE; backs slug lookups
--        and the "max one orchestrator_root row" invariant (the auto-
--        register code uses the fixed slug 'orchestrator-root').
CREATE INDEX IF NOT EXISTS idx_projects_host ON projects(host);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_slug ON projects(slug);

COMMIT;

-- 6. Re-enable FK enforcement. Connection-scoped, takes effect for
--    every subsequent statement on this connection (including the
--    migrations runner's INSERT into _schema_migrations).
PRAGMA foreign_keys = ON;

-- 7. Integrity check. Returns one row per dangling FK; empty result
--    means every FK pointer still resolves. We don't have a way to
--    abort the migration from PRAGMA output alone (SQL statements
--    can't conditionally raise on rowset), but rusqlite's
--    `execute_batch` surfaces the PRAGMA's output via the connection
--    and any orphaned-FK error caused by a subsequent INSERT into a
--    dependent table would be loud at runtime. We keep this PRAGMA
--    here as a deliberate signal: any tooling that runs migrations
--    interactively can inspect the result, and any future regression
--    in this migration would be caught by a fresh
--    `migration_013_preserves_fk_resolution_on_upgrade` test run.
PRAGMA foreign_key_check;
