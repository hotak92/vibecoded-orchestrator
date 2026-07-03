-- SPDX-License-Identifier: AGPL-3.0-or-later
-- launcher.db — extend code_graph_builds.status CHECK with 'partial' (migration 038, v0.2.73)
--
-- v0.2.73 C-11 / RT-3: a code-graph build can now finish in a PARTIAL
-- terminal state. Insert work SUCCEEDED (every source file written to
-- Weaviate, files_analyzed is meaningful) but the stale-row DELETE pass
-- FAILED for one or more rows (prune_failures > 0). This is neither a
-- clean success nor a hard failure — the analyzer keeps exit code 0 and
-- emits a machine-readable `PRUNE_FAILURES=N` stdout line that the
-- launcher's stdout reader (commands/codegraph.rs) parses to flip the
-- row status success -> partial when N>0. See the contract comment in
-- templates/scripts/analyze_code_graph.py (main(), "PARTIAL (prune-
-- failure) signalling contract").
--
-- WHY a new status instead of reusing 'failed' or 'success':
--   * 'success' would hide that stale (deleted-in-source) entries remain
--     in the code graph, silently returning stale results to searches.
--   * 'failed' would (via the reader's ANY-non-zero-exit -> failed with
--     files_analyzed=0 path) WRONGLY discard the file count of a build
--     that actually inserted everything correctly.
--   'partial' captures exactly this: inserts good, prune incomplete. The
--   GUI banner renders it as a non-alert terminal warning so the user
--   can trigger a consented drop-and-rebuild if the stale rows matter.
--
-- SQLite limitation: CHECK constraints are immutable post-CREATE. The
-- only way to extend them is the table-rebuild pattern (mirror of
-- migration 021, which extended module_installs.status): create a new
-- table with the wider CHECK, copy rows, drop original, rename new.
-- Indices are recreated to preserve the original schema.
--
-- CRITICAL: the replacement table carries EVERY column the live table
-- has after migration 037 — including the `pid` column added by 037.
-- Dropping a column here would be silent data loss on any host already
-- past v37.
--
-- No FK toggles / no self-BEGIN: nothing references code_graph_builds
-- via an inbound FOREIGN KEY (only its own FK to projects and the status
-- index), so this rebuild rides the migrations runner's outer
-- transaction exactly like migration 021 (NOT in
-- SELF_TRANSACTIONAL_MIGRATIONS). foreign_keys stays ON throughout.
--
-- Forward-only, idempotent (the migrations runner gates by version in
-- `_schema_migrations`; this file is executed exactly once per DB).
--
-- ATOMIC PAIRING (B-2 lesson): vco_lib/schema_versions.py's
-- LAUNCHER_DB_TABLE_SET_VERSION bumps 37 -> 38 in the SAME merge as this
-- file — a Python-side bump landing ahead of this registration would
-- stamp a phantom schema version.

-- 1. Create the replacement table with the extended CHECK. Column set is
--    identical to code_graph_builds after migration 037 (006 base + 037 pid).
CREATE TABLE code_graph_builds_new (
    project_id     TEXT PRIMARY KEY,            -- references projects.id
    status         TEXT NOT NULL
                   CHECK (status IN ('pending','running','success','partial','failed','skipped')),
    started_at     INTEGER,                      -- ms since epoch
    finished_at    INTEGER,
    duration_ms    INTEGER,
    files_analyzed INTEGER NOT NULL DEFAULT 0,
    languages      TEXT,                         -- JSON array, e.g. ["py","ts"]
    joern_used     INTEGER NOT NULL DEFAULT 0,   -- 0 = no, 1 = --cfg/--pdg ran
    error_message  TEXT,                         -- NULL on success/skipped
    log_tail       TEXT,                         -- last ~4KB of analyzer output
    pid            INTEGER,                       -- 037: NULL = launcher-spawned, NOT NULL = detached
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- 2. Copy every row across (column order is identical).
INSERT INTO code_graph_builds_new
    (project_id, status, started_at, finished_at, duration_ms,
     files_analyzed, languages, joern_used, error_message, log_tail, pid)
SELECT
    project_id, status, started_at, finished_at, duration_ms,
    files_analyzed, languages, joern_used, error_message, log_tail, pid
FROM code_graph_builds;

-- 3. Drop the original.
DROP TABLE code_graph_builds;

-- 4. Rename replacement into place.
ALTER TABLE code_graph_builds_new RENAME TO code_graph_builds;

-- 5. Recreate the index migration 006 declared.
CREATE INDEX idx_code_graph_builds_status
    ON code_graph_builds(status);
