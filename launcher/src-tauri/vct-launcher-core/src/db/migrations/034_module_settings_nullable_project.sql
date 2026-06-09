-- launcher.db — module_settings.project_id becomes nullable (migration 034)
--
-- v0.2.52 V52-AD: support a GLOBAL (host-wide) enable/disable toggle for
-- modules — extending the per-project toggle pattern landed in v0.2.49
-- Stream B (`MODULE_ENABLED_FOR_PROJECT_KEY`). A row with `project_id IS
-- NULL` represents the host-wide default; hub resolver fall-back order is:
--
--     effective_enabled =
--         per_project_setting (project_id = $project)
--             .unwrap_or(global_default (project_id IS NULL))
--             .unwrap_or(true)   -- fail-open default
--
-- The pattern MIRRORS migration 027 which made `module_installs.project_id`
-- nullable for global-scope installs. Same semantics: NULL == "no specific
-- project, applies to every project".
--
-- WHY (V52-AD user-stated):
--
--   "we already have a per-project opt-out in the code/GUI, can re-use
--    that code for a 'global toggle'" (2026-06-09).
--
--   The immediate use case is "disable RL reranker by default until we
--   accumulate 500+ retrieval events" — a global default that lets
--   per-project overrides take precedence once the user opts in.
--
-- HOW:
--
-- SQLite cannot ALTER a column from NOT NULL to NULL in place, so we use
-- the canonical 12-step create-copy-drop-rename pattern (same as
-- migrations 013 and 027).
--
-- New schema:
--   * `project_id TEXT NULL` (was `NOT NULL`)
--   * `UNIQUE(project_id, module_id, setting_key)` REMOVED (SQLite's
--     UNIQUE treats `NULL ≠ NULL`, so two global rows for the same
--     (module_id, setting_key) would both pass — opposite of intended).
--   * Replaced with TWO partial unique indexes:
--       - `idx_ms_unique_per_project (project_id, module_id, setting_key)
--         WHERE project_id IS NOT NULL` — same per-project uniqueness.
--       - `idx_ms_unique_global (module_id, setting_key)
--         WHERE project_id IS NULL` — at most one global row per
--         (module_id, setting_key) pair.
--
-- The FK `project_id REFERENCES projects(id) ON DELETE CASCADE` is
-- preserved. NULL `project_id` values trivially satisfy the FK (SQLite's
-- FK enforcement treats NULL as "no reference asserted"). Deleting a
-- project still cascades per-project rows of that project; global rows
-- (project_id IS NULL) are untouched, which is the correct behaviour for
-- host-wide defaults.
--
-- FK SAFETY (mirrors migration 027's discipline):
--
-- SQLite requires `PRAGMA foreign_keys = OFF` for the duration of any
-- rebuild that drops a parent table OR a table with FKs. Although THIS
-- migration rebuilds a CHILD table (module_settings), no tables today
-- reference module_settings(id). We still wrap the rebuild in
-- `foreign_keys=OFF` for defence-in-depth + consistency with
-- migration 027.
--
-- `PRAGMA foreign_keys` is a no-op inside a transaction, so the OFF/ON
-- toggles MUST be OUTSIDE the BEGIN/COMMIT block.

-- 0. Disable FK enforcement for the duration of the rebuild.
PRAGMA foreign_keys = OFF;

BEGIN;

-- 1. New table with nullable project_id. Column order + types are an
--    EXACT mirror of the pre-034 schema except for the NOT NULL drop.
CREATE TABLE module_settings_v2 (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT REFERENCES projects(id) ON DELETE CASCADE,
    module_id       TEXT NOT NULL,
    setting_key     TEXT NOT NULL,
    setting_value   TEXT NOT NULL              -- JSON
);

-- 2. Copy every existing row verbatim. Every pre-034 row has a non-NULL
--    project_id, so the column-nullability change cannot reject any row.
INSERT INTO module_settings_v2
    (id, project_id, module_id, setting_key, setting_value)
SELECT id, project_id, module_id, setting_key, setting_value
FROM module_settings;

-- 3. Drop the original.
DROP TABLE module_settings;

-- 4. Rename. Every column-by-name read against `module_settings` from
--    pre-034 code keeps working unchanged.
ALTER TABLE module_settings_v2 RENAME TO module_settings;

-- 5. Recreate the indexes that lived on the old table + the two new
--    partial-unique indexes.
CREATE INDEX IF NOT EXISTS idx_ms_project ON module_settings(project_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ms_unique_per_project
    ON module_settings(project_id, module_id, setting_key)
    WHERE project_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ms_unique_global
    ON module_settings(module_id, setting_key)
    WHERE project_id IS NULL;

COMMIT;

-- 6. Re-enable FK enforcement.
PRAGMA foreign_keys = ON;

-- 7. Integrity check — empty result == intact.
PRAGMA foreign_key_check;
