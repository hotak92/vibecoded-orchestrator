-- launcher.db — per-project MCP server registry (migration 010)
--
-- Mirrors the contents of `<folder>/.claude/settings.json::mcpServers` and
-- `<folder>/.mcp.json` (Anthropic project-scoped MCP config) into a
-- queryable table so the launcher's "Custom MCP" tab can render
-- user-added entries without re-parsing those JSON files at every render.
--
-- KNOWN_ISSUES.md (v0.2.x) entry resolved by this table:
--   "Custom MCP tab is not populated by initial project registration —
--    `project_state_populate` mirrors `.claude/settings.json::mcpServers`
--    into the launcher's per-project DB on `create_project_v2`, but
--    doesn't flag user-added entries (anything beyond bundled
--    `weaviate-kg` / `ollama` / `search` / `code-embedding` / `playwright`)
--    as `is_user_added=true`."
--
-- Design notes:
--  * `is_user_added` is the discriminator the Custom MCP tab filters on.
--    Bundled = orchestrator-shipped (weaviate-kg / ollama / search /
--    code-embedding / playwright / vct-coordination). Anything else =
--    user-added.
--  * `source_file` records WHICH file the entry came from so the
--    launcher can route writes back to the correct surface (project's
--    `.claude/settings.json` vs project-scoped `.mcp.json` vs the global
--    `~/.claude.json`). Never a launcher-local path; always relative to
--    the project folder.
--  * `config_json` is the raw entry as it appeared in the source file —
--    `{ command, args, env, ... }`. The Custom MCP tab renders straight
--    from this blob; no schema lock-in for fields that vary across MCP
--    runtimes.
--  * `enabled` is independent of row existence so the GUI can toggle
--    without dropping the row's config.
--  * Idempotency: `(project_id, mcp_name)` is the natural key. Re-running
--    the populate step UPSERTs without clobbering `enabled`.

CREATE TABLE IF NOT EXISTS project_mcp_servers (
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    mcp_name        TEXT NOT NULL,                          -- e.g. 'weaviate-kg' or 'my-custom-mcp'
    is_user_added   INTEGER NOT NULL DEFAULT 0,             -- 1 = user added it; 0 = bundled
    source          TEXT NOT NULL DEFAULT 'project'         -- where the row was seeded from
                    CHECK (source IN ('bundled','user','paid-module','project')),
    source_module   TEXT,                                   -- module_id when source='paid-module'
    source_file     TEXT,                                   -- '.claude/settings.json' | '.mcp.json' | NULL
    enabled         INTEGER NOT NULL DEFAULT 1,
    command         TEXT,                                   -- top-level convenience copy of config.command
    config_json     TEXT NOT NULL DEFAULT '{}',             -- full MCP entry (command/args/env/etc.)
    installed_at    INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (project_id, mcp_name)
);
CREATE INDEX IF NOT EXISTS idx_project_mcp_servers_pid
    ON project_mcp_servers(project_id);
CREATE INDEX IF NOT EXISTS idx_project_mcp_servers_user_added
    ON project_mcp_servers(project_id, is_user_added);
