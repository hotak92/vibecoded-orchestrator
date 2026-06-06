-- launcher.db — module_installs.project_id becomes nullable (migration 027)
--
-- v0.2.49 Stream A: support `install.scope = "global"` in module manifests.
-- A global-scope install row carries `project_id IS NULL` to mean "this
-- module is installed once per machine, not once per project". Per-project
-- routing happens INSIDE the container at the application layer (e.g. the
-- v0.2.10 RL Reranker reads `X-VCT-Project-ID` from request headers).
--
-- WHY:
--
--   * Per-project containers waste VRAM (N × 1.5 GB resident on a 4080 SUPER)
--     and disk (N × 5.5 GB image) for the RL Reranker, where a single
--     in-process router can serve every project from one model load.
--   * Port-allocation complexity disappears: one container, one port.
--   * Lifecycle is simpler: one container restarts on image redeploy,
--     not N.
--
-- HOW:
--
-- SQLite cannot ALTER a column from NOT NULL to NULL in place, so we use
-- the canonical 12-step create-copy-drop-rename pattern documented at
-- https://www.sqlite.org/lang_altertable.html#otheralter — same pattern
-- migration 013 uses for the projects table.
--
-- New schema:
--   * `project_id TEXT NULL` (was `NOT NULL`)
--   * `UNIQUE(project_id, module_id)` REMOVED (SQLite's UNIQUE treats
--     `NULL ≠ NULL`, so two global rows for the same module would BOTH
--     pass the constraint — which is the opposite of what we want).
--   * Replaced with TWO partial unique indexes:
--       - `idx_mi_unique_per_project (project_id, module_id) WHERE project_id IS NOT NULL`
--         — same uniqueness guarantee per-project as before.
--       - `idx_mi_unique_global (module_id) WHERE project_id IS NULL`
--         — at most one global install per module.
--
-- The FK `project_id REFERENCES projects(id) ON DELETE CASCADE` is
-- preserved. NULL `project_id` values trivially satisfy the FK (SQLite's
-- FK enforcement treats NULL as "no reference asserted"). Deleting a
-- project still cascades per-project rows of that project; global rows
-- (project_id IS NULL) are untouched, which is the correct behaviour for
-- machine-wide installs.
--
-- FK SAFETY (mirrors migration 013's discipline):
--
-- SQLite requires `PRAGMA foreign_keys = OFF` for the duration of any
-- rebuild that drops a parent table. Although THIS migration rebuilds a
-- CHILD table (module_installs), there are no tables that reference
-- module_installs(id) today — so the FK landscape is simpler than 013's.
-- We still wrap the rebuild in `foreign_keys=OFF` for two reasons:
--
--   1. Defence-in-depth — if a future migration adds an FK pointing AT
--      module_installs.id, this script's pattern keeps working without
--      modification.
--   2. Consistency with migration 013 — the same OFF/COMMIT/ON/check
--      sequence is the canonical SQLite recreate-and-copy idiom.
--
-- `PRAGMA foreign_keys` is a no-op inside a transaction, so the OFF/ON
-- toggles MUST be OUTSIDE the BEGIN/COMMIT block (same as 013).

-- 0. Disable FK enforcement for the duration of the rebuild.
PRAGMA foreign_keys = OFF;

BEGIN;

-- 1. New table with nullable project_id.
--
--    Column order + types are an EXACT mirror of the post-021 + post-015
--    `module_installs` schema (id, project_id, module_id, module_version,
--    install_path, status, enabled, installed_at, last_started_at,
--    last_error, container_name). Keep aligned with the explicit-column
--    INSERT below.
--
--    Status CHECK now mirrors migration 021's extended set
--    (installing|installed|running|stopped|error|broken).
--
--    UNIQUE constraint is dropped from the table body; replaced with two
--    partial indexes below (NULL semantics force this).
CREATE TABLE module_installs_v2 (
    id              TEXT PRIMARY KEY,
    project_id      TEXT REFERENCES projects(id) ON DELETE CASCADE,
    module_id       TEXT NOT NULL,
    module_version  TEXT NOT NULL,
    install_path    TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN ('installing','installed','running','stopped','error','broken')),
    enabled         INTEGER NOT NULL DEFAULT 1,
    installed_at    INTEGER NOT NULL,
    last_started_at INTEGER,
    last_error      TEXT,
    container_name  TEXT
);

-- 2. Copy every existing row verbatim. Every pre-027 row has a non-NULL
--    project_id, so the column-nullability change cannot reject any row.
INSERT INTO module_installs_v2
    (id, project_id, module_id, module_version, install_path,
     status, enabled, installed_at, last_started_at, last_error,
     container_name)
SELECT id, project_id, module_id, module_version, install_path,
       status, enabled, installed_at, last_started_at, last_error,
       container_name
FROM module_installs;

-- 3. Drop the original. With foreign_keys=OFF (set above the BEGIN),
--    this DROP does not cascade-delete any child rows — there are none
--    today, but the discipline matches 013's.
DROP TABLE module_installs;

-- 4. Rename. Every column-by-name read against `module_installs` from
--    pre-027 code keeps working unchanged.
ALTER TABLE module_installs_v2 RENAME TO module_installs;

-- 5. Recreate the indexes that lived on the old table:
--      * idx_mi_project (migration 001) — used by per-project SELECTs.
--      * idx_mi_status (migration 001) — used by status-filter SELECTs.
--    Plus the NEW partial unique indexes that replace the dropped
--    table-level UNIQUE(project_id, module_id):
--      * idx_mi_unique_per_project — at most one row per (project,
--        module) pair when project_id IS NOT NULL.
--      * idx_mi_unique_global — at most one GLOBAL row per module
--        (project_id IS NULL).
CREATE INDEX IF NOT EXISTS idx_mi_project ON module_installs(project_id);
CREATE INDEX IF NOT EXISTS idx_mi_status  ON module_installs(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mi_unique_per_project
    ON module_installs(project_id, module_id)
    WHERE project_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_mi_unique_global
    ON module_installs(module_id)
    WHERE project_id IS NULL;

COMMIT;

-- 6. Re-enable FK enforcement.
PRAGMA foreign_keys = ON;

-- 7. Integrity check — empty result == intact.
PRAGMA foreign_key_check;
