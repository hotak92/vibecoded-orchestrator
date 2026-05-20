-- launcher.db — per-project initial KG / docs sync status (KG auto-sync, 2026-05-12).
--
-- When the user adds a project via the launcher, the bundle install drops
-- `.claude/scripts/kg-sync` into the project. If the project arrived with
-- pre-existing `knowledge/**/*.md` and/or `docs/**/*.md` files (a very
-- common case for projects ported from a private Claude orchestrator
-- install), those files used to remain unindexed in Weaviate until the
-- user opened a Claude session and the post-file-edit hook fired on a
-- subsequent edit. This table tracks an explicit launcher-spawned
-- `kg-sync --all` run that closes that gap.
--
-- One row per project. Absence of a row = "never synced via the launcher
-- auto-sync path" (the user may still have synced manually via the CLI,
-- in which case the launcher just won't render a pill). Terminal states
-- are 'success', 'failed', 'skipped'.
--
-- Schema notes:
--  * Mirrors the shape of code_graph_builds (migration 006) on purpose —
--    same lifecycle states, same {started,finished}_at, same FK cascade
--    so a project unregister leaves no dangling rows.
--  * `kg_*` and `docs_*` counters are tracked separately because the
--    underlying `sync_knowledge_graph.py --all` emits independent
--    "📊 KG:   X succeeded, Y failed" + "📊 Docs: X succeeded, Y failed"
--    summary lines, and the UI may want to show partial-success cases
--    (e.g. KG synced but Weaviate's `Development` collection write failed).

CREATE TABLE IF NOT EXISTS kg_syncs (
    project_id      TEXT PRIMARY KEY,            -- references projects.id
    status          TEXT NOT NULL
                    CHECK (status IN ('pending','running','success','failed','skipped')),
    started_at      INTEGER,                      -- ms since epoch
    finished_at     INTEGER,
    duration_ms     INTEGER,
    kg_total        INTEGER NOT NULL DEFAULT 0,   -- "📚 Found N markdown files in knowledge/"
    kg_succeeded    INTEGER NOT NULL DEFAULT 0,
    kg_failed       INTEGER NOT NULL DEFAULT 0,
    docs_total      INTEGER NOT NULL DEFAULT 0,   -- "📚 Found N markdown files in docs/"
    docs_succeeded  INTEGER NOT NULL DEFAULT 0,
    docs_failed     INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT,                          -- NULL on success/skipped
    log_tail        TEXT,                          -- last ~4 KiB of subprocess output
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_kg_syncs_status
    ON kg_syncs(status);
