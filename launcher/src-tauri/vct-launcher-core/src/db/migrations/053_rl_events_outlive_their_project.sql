-- SPDX-License-Identifier: AGPL-3.0-or-later
-- launcher.db — RL training events outlive their project (migration 053,
-- v0.2.107 audit A-022, owner ruling: on unregister the events are KEPT, with
-- the project name in the rows, so they stay attributable).
--
-- Until now `rl_events.project_id` carried
-- `FOREIGN KEY ... REFERENCES projects(id) ON DELETE CASCADE` (migration
-- 025), so unregistering a project deleted every RL training event it had
-- collected, silently and inside one long transaction. After this migration:
--
--   * the foreign key is gone: deleting a projects row touches no rl_events
--     row. The rows keep their project_id (the id of a project that is no
--     longer registered; every registration gets a fresh UUID, so the id is
--     never reused by another project) AND their project_name. A training
--     export or a per-project read still finds them by that id.
--   * the "project_id must name a registered project" rule moves to the one
--     writer, `Db::insert_rl_event` (rl_events.rs): it refuses an unknown
--     project_id atomically in the INSERT itself, so the hub's 422
--     `unknown_project_id` answer and the writer's re-resolve are unchanged.
--     The same INSERT copies the project's name into project_name when the
--     writer sent none.
--   * project_name is back-filled once here from the projects table for the
--     existing rows that lack it.
--
-- WHY NOT a table rebuild (create-copy-drop-rename): on a real install this
-- table holds tens of GB (each retrieval row carries its payload); a rebuild
-- would need that much free disk again and would hold the launcher's boot for
-- the length of the copy. Dropping a FOREIGN KEY constraint does not change
-- how any row is stored, which is the case SQLite documents for editing the
-- stored CREATE statement directly ("Making Other Kinds Of Table Schema
-- Changes", https://www.sqlite.org/lang_altertable.html): writable_schema on,
-- one UPDATE of the table's sql text, then the schema reloaded and the schema
-- cookie bumped so every other open connection (the hub) reloads it too.
--
-- The FK clause is replaced by an always-true named CHECK, not cut out, so the
-- edit cannot leave a stray comma whatever columns later ALTERs appended
-- (039 added two after it). `replace()` matches the clause text exactly as
-- migration 025 wrote it; if it is not found the UPDATE changes nothing and
-- the table keeps its foreign key — never a broken schema. The Rust delete
-- path does not depend on this edit for the default (keep) choice to be
-- safe in that case: see `Db::delete_project`.
--
-- No index changes: every rl_events index is untouched (an index belongs to
-- its own sqlite_schema row). Not self-transactional: rides the runner's outer
-- transaction, so a failure rolls the schema edit back with everything else.
-- LAUNCHER_DB_TABLE_SET_VERSION bumps 52->53 atomically with this migration.

PRAGMA writable_schema = ON;

UPDATE sqlite_schema
   SET sql = replace(
         sql,
         'FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE',
         'CONSTRAINT rl_events_outlive_their_project CHECK (1)'
       )
 WHERE type = 'table' AND name = 'rl_events';

-- Turn writable_schema off AND reload this connection's schema from the
-- edited text.
PRAGMA writable_schema = RESET;

-- Any DDL increments the schema cookie: the other connections (the hub, a
-- Python reader) see it change and reload the schema on their next statement.
CREATE TABLE _migration_053_schema_cookie (x INTEGER);
DROP TABLE _migration_053_schema_cookie;

-- Back-fill: rows written without a project_name get their project's name
-- while the project is still registered. Rows whose writer sent a name keep
-- it; free-tier rows (project_id NULL) are untouched.
UPDATE rl_events
   SET project_name = (SELECT p.name FROM projects p WHERE p.id = rl_events.project_id)
 WHERE project_name IS NULL
   AND project_id IS NOT NULL;
