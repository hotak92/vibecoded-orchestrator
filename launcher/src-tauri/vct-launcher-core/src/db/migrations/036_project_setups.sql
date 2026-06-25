-- launcher.db — per-project async setup status (Defect B, v0.2.68).
--
-- `create_project_v2` used to `.await` the two slow Python subprocesses
-- (bootstrap-collections + install-bundle) plus the post-bundle phase
-- inline, before returning. On a COLD Weaviate/Ollama backend that was
-- ~51s of silent modal blur — the add read as "frozen". The heavy phase
-- now runs in a detached `tokio::spawn` task that drives THIS row's
-- lifecycle and emits `project://setup-progress` events to a global
-- top banner; `create_project_v2` returns FAST once the synchronous
-- phase (DB row + .claude/env) is committed.
--
-- One row per project. Absence of a row = "no launcher-driven async
-- setup ran" (older projects pre-Defect-B, or the all-synchronous path).
-- A row in 'pending' / 'running' is the re-entrancy LOCK: a second setup
-- for the same project is refused while the row is live (mirrors the
-- v0.2.67 `install_in_flight` backstop). Terminal states are 'done'
-- (clean), 'deferred' (a phase deferred cleanly, e.g. Weaviate bootstrap
-- on a cold backend — informational, NOT a failure), and 'failed' (a
-- genuine subprocess failure).
--
-- Schema notes:
--  * Mirrors the shape of code_graph_builds (migration 006) + kg_syncs
--    (migration 011) on purpose — same {started,finished}_at, same FK
--    cascade so a project unregister leaves no dangling rows.
--  * `phase` carries the last-known coarse phase label ('bootstrap',
--    'bundle', 'post_bundle') so the boot-resume sweep + GUI can show
--    where an interrupted setup got to.
--  * `warnings` is a JSON array of the human-readable warning strings
--    collected across all phases. The terminal `project://setup-progress`
--    event carries the same list so the frontend can re-toast them
--    (preserving the pre-Defect-B inline-toast behaviour) — see F5.

CREATE TABLE IF NOT EXISTS project_setups (
    project_id     TEXT PRIMARY KEY,            -- references projects.id
    status         TEXT NOT NULL
                   CHECK (status IN ('pending','running','done','deferred','failed')),
    phase          TEXT,                         -- 'bootstrap' | 'bundle' | 'post_bundle' | NULL
    started_at     INTEGER,                      -- ms since epoch
    finished_at    INTEGER,
    duration_ms    INTEGER,
    warnings       TEXT,                         -- JSON array of warning strings
    error_message  TEXT,                         -- NULL unless status='failed'
    log_tail       TEXT,                         -- reserved; last ~4 KiB of output
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_project_setups_status
    ON project_setups(status);
