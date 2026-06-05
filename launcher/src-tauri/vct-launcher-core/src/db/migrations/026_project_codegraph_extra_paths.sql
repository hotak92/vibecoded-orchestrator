-- SPDX-License-Identifier: AGPL-3.0-or-later
-- launcher.db — v0.2.47 project-extra codegraph paths (migration 026)
--
-- Read-only filesystem paths contributing entities to a project's codegraph
-- collection. Use case: index a sibling clone (e.g. `vibecoded-orchestrator/`)
-- into the active project's codegraph without making it a launcher project.
-- See `knowledge/concepts/project-extra-codegraph-paths-2026-06-05.md` and
-- `.claude/context/plans/v0.2.47-project-extra-codegraph-paths-2026-06-05.md`
-- for the full design rationale.
--
-- Why a separate table (not JSON in project_codegraph_bindings.config_json):
--   * Hooks query by path-prefix to decide "is this edited file under any
--     project's extra path?". SQL `WHERE ? LIKE path || '%'` needs a
--     real column.
--   * ON DELETE CASCADE on project_id is free.
--   * Each path has its own `last_indexed_at` for incremental analyze.
--   * Future per-path glob filters slot in as a column without a schema
--     rewrite of an opaque JSON blob.
--
-- Why last_indexed_commit per path (parallel to
-- `project_codegraph_bindings.last_analyzed_commit`):
--   Extra paths are typically git repos themselves (e.g.
--   `vibecoded-orchestrator/`). The binding's last_analyzed_commit tracks
--   the project's OWN repo; per-path tracking is required so incremental
--   analyzes can pass `--since-commit <sha>` to the analyzer per source
--   root.
--
-- enabled column semantics:
--   * 1 = active: included in resolver responses, hook detection,
--     re-analyze fan-outs, and the "Reindex codegraph" sweep.
--   * 0 = soft-disabled: row kept (preserves user-set label,
--     last_indexed timestamps for audit), but treated as if absent by
--     every consumer. Flipping enabled=false MUST be followed by a
--     re-analyze with --prune-stale to drop entries the disabled path
--     contributed (the Tauri layer wires this — see §14.2 of the plan).
--
-- Canonicalisation rule (enforced at Tauri-command boundary BEFORE insert,
-- not by SQL — SQLite has no path primitives):
--   * Path MUST be absolute (Path::is_absolute).
--   * Path MUST exist + be a directory.
--   * Path::canonicalize() resolves symlinks, normalises segments,
--     lowercases drive letter on Windows.
--   * Trailing /  (or \) stripped so prefix-match queries are unambiguous.
--   * Windows forward-slash storage form (replace('\\', '/')) for
--     cross-platform prefix-match SQL.
--
-- Forward-only, idempotent (the migrations runner gates by version in
-- _schema_migrations; this file is executed exactly once per DB).

CREATE TABLE IF NOT EXISTS project_codegraph_extra_paths (
    project_id           TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- Absolute, canonicalised at add time. Cross-platform storage form:
    -- forward slashes throughout (Windows backslashes converted before
    -- INSERT). No trailing separator.
    path                 TEXT NOT NULL,
    -- Optional UI label; falls back to file basename if NULL.
    label                TEXT,
    -- Unix epoch millis matching the rest of launcher.db convention.
    added_at             INTEGER NOT NULL,
    -- Unix epoch millis. NULL until first successful analyze pass.
    last_indexed_at      INTEGER,
    -- Git SHA at last analyze. NULL when the extra path is not a git
    -- repo OR has not been analyzed yet. Used by --since-commit for
    -- incremental analyze invocations against this single path.
    last_indexed_commit  TEXT,
    -- 0 = soft-disabled (won't re-index, kept for history + audit).
    enabled              INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project_id, path)
);

-- Hot index for the per-project list query (the dominant access pattern:
-- "give me all extras for project P"). The PRIMARY KEY composite already
-- covers (project_id, path); this redundant index on project_id alone
-- helps when the second column isn't bound. SQLite's planner will pick
-- the PK index when both columns are present; this one only matters for
-- "list by project" scans.
CREATE INDEX IF NOT EXISTS idx_pcep_pid
    ON project_codegraph_extra_paths(project_id);

-- Hot index for hook prefix-match: hooks ask "is <edited file> under
-- any enabled extra path of any project?". The query shape is roughly
-- `WHERE ? LIKE path || '%' AND enabled = 1`. Without an index, the
-- table scans linearly. Index on enabled puts the predicate-prune
-- ahead of the substring match.
CREATE INDEX IF NOT EXISTS idx_pcep_enabled
    ON project_codegraph_extra_paths(enabled);
