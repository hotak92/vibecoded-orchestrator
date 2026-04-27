-- Gap 2 (OSS launch 2026-05-12): per-project code-graph build status.
--
-- When the user creates a project via the launcher we kick off an initial
-- `code-graph-analyze` run in the background. This table tracks the
-- progress / outcome of that build so the GUI can show a status pill
-- and the user can re-trigger a rebuild from settings.
--
-- One row per project. Absence of a row = "never analyzed". Presence with
-- status='pending' or 'running' = build in flight. Terminal states are
-- 'success', 'failed', 'skipped'.

CREATE TABLE IF NOT EXISTS code_graph_builds (
    project_id     TEXT PRIMARY KEY,            -- references projects.id
    status         TEXT NOT NULL
                   CHECK (status IN ('pending','running','success','failed','skipped')),
    started_at     INTEGER,                      -- ms since epoch
    finished_at    INTEGER,
    duration_ms    INTEGER,
    files_analyzed INTEGER NOT NULL DEFAULT 0,
    languages      TEXT,                         -- JSON array, e.g. ["py","ts"]
    joern_used     INTEGER NOT NULL DEFAULT 0,   -- 0 = no, 1 = --cfg/--pdg ran
    error_message  TEXT,                         -- NULL on success/skipped
    log_tail       TEXT,                         -- last ~4KB of analyzer output
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_code_graph_builds_status
    ON code_graph_builds(status);
