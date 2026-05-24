-- SPDX-License-Identifier: AGPL-3.0-or-later
-- launcher.db — extend module_installs.status CHECK with 'broken' (migration 021, v0.2.33)
--
-- v0.2.33 Agent C: introduces a startup reconciler that walks every
-- `module_installs` row where status='installed' and verifies that the
-- on-disk extracted manifest at `~/.vct/modules/<id>/vct-module.json`
-- exists. When the manifest is missing (user manually deleted the dir,
-- a previous post-install extract crashed before atomic-rename, etc.)
-- the row is flipped to status='broken' so the GUI tile can render a
-- "Reinstall needed" CTA instead of a misleading "Open dashboard"
-- button.
--
-- WHY a new status instead of reusing 'error':
--   * 'error' is the generic "something went wrong while transitioning
--     state" bucket (runtime container crash, post_install command
--     non-zero exit, etc.) and is recoverable via Restart.
--   * 'broken' is the specific "the on-disk artifact for this install
--     no longer exists" state and is only recoverable via Reinstall
--     (Restart can't bring back a deleted manifest).
--   The GUI button-state matrix needs to distinguish these two so the
--   user is funnelled to the right repair path.
--
-- SQLite limitation: CHECK constraints are immutable post-CREATE. The
-- only way to extend them is the table-rebuild pattern: create a new
-- table with the wider CHECK, copy rows, drop original, rename new.
-- Indices are recreated to preserve the original schema.
--
-- Forward-only, idempotent (the migrations runner gates by version in
-- `_schema_migrations`; this file is executed exactly once per DB).

-- 1. Create the replacement table with the extended CHECK.
CREATE TABLE module_installs_new (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    module_id       TEXT NOT NULL,
    module_version  TEXT NOT NULL,
    install_path    TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN ('installing','installed','running','stopped','error','broken')),
    enabled         INTEGER NOT NULL DEFAULT 1,
    installed_at    INTEGER NOT NULL,
    last_started_at INTEGER,
    last_error      TEXT,
    container_name  TEXT,
    UNIQUE(project_id, module_id)
);

-- 2. Copy every row across (column order is identical).
INSERT INTO module_installs_new
    (id, project_id, module_id, module_version, install_path,
     status, enabled, installed_at, last_started_at, last_error, container_name)
SELECT
    id, project_id, module_id, module_version, install_path,
    status, enabled, installed_at, last_started_at, last_error, container_name
FROM module_installs;

-- 3. Drop the original.
DROP TABLE module_installs;

-- 4. Rename replacement into place.
ALTER TABLE module_installs_new RENAME TO module_installs;

-- 5. Recreate the indices the original migration declared.
CREATE INDEX idx_mi_project ON module_installs(project_id);
CREATE INDEX idx_mi_status ON module_installs(status);
