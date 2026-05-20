-- launcher.db — per-project initial KG-summary backfill status
-- (KG summary auto-backfill on add-project, 2026-05-12 / v0.2.3).
--
-- v0.2.2 added auto-sync of `knowledge/**/*.md` to Weaviate on add-project
-- (migration 011 / `kg_syncs`). v0.2.3 closes a second gap: the orchestrator
-- also maintains a sidecar file `<project>/knowledge/.node_formats.json`
-- with LLM-generated summaries per KG node, used by the `hybrid_search`
-- `summary` tier (score 0.42–0.55). Those summaries are produced by
-- `.claude/scripts/generate-kg-summary.py`, invoked per-file by the
-- PostToolUse hook `kg-summary-generator.sh` — but the hook only fires on
-- Claude Code Edit/Write tool events, NOT when the launcher's kg-sync
-- subprocess writes embeddings. Result: a freshly-added project has
-- Weaviate populated but `.node_formats.json` empty. This table tracks
-- an explicit launcher-spawned backfill pass that closes that gap.
--
-- One row per project. Absence of a row = "never backfilled via the
-- launcher auto-backfill path" (the user may still have generated
-- summaries lazily via Claude sessions, in which case the launcher
-- just won't render a pill). Terminal states are 'success', 'failed',
-- 'skipped'.
--
-- Schema notes:
--  * Mirrors the shape of kg_syncs (migration 011) on purpose — same
--    lifecycle states, same {started,finished}_at, same FK cascade so a
--    project unregister leaves no dangling rows.
--  * Counters track per-node progress (total scanned vs. summarised vs.
--    skipped). `nodes_skipped` is non-zero for the common case where a
--    backend is unavailable — see `kg_summary::SubprocessOutcome` for
--    the breakdown of skipped reasons.
--  * `backend` records the backend the summariser picked on its first
--    call (cli|ollama|api|skip|mixed). The launcher GUI uses this to
--    decide whether to show a "Skipped — no backend available; install
--    Ollama or the claude CLI to backfill" banner detail.

CREATE TABLE IF NOT EXISTS kg_summaries (
    project_id      TEXT PRIMARY KEY,            -- references projects.id
    status          TEXT NOT NULL
                    CHECK (status IN ('pending','running','success','failed','skipped')),
    started_at      INTEGER,                      -- ms since epoch
    finished_at     INTEGER,
    duration_ms     INTEGER,
    nodes_total     INTEGER NOT NULL DEFAULT 0,   -- count of `knowledge/**/*.md` discovered
    nodes_succeeded INTEGER NOT NULL DEFAULT 0,   -- summariser wrote a new entry
    nodes_unchanged INTEGER NOT NULL DEFAULT 0,   -- hash match → skip (already have summary)
    nodes_failed    INTEGER NOT NULL DEFAULT 0,   -- summariser raised
    nodes_skipped   INTEGER NOT NULL DEFAULT 0,   -- summariser exited 0 with "no backend" or "no title"
    backend         TEXT,                          -- "cli" | "ollama" | "api" | "skip" | "mixed"
    error_message   TEXT,                          -- NULL on success/skipped
    log_tail        TEXT,                          -- last ~4 KiB of subprocess output (across all nodes)
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_kg_summaries_status
    ON kg_summaries(status);
