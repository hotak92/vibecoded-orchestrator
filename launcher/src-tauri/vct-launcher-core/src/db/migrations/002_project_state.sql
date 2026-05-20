-- launcher.db — per-project orchestrator state (migration 002)
--
-- Tracks agents, skills, hooks, permissions, secret references, and
-- KG/codegraph bindings for each project. The launcher GUI uses these
-- rows to render the per-project state matrix; install.py calls the
-- HTTP API to register entries after copying agent/skill files into
-- a project's `.claude/` directory.
--
-- Design notes:
--  * Secret VALUES are NEVER stored here — only references (key + scope).
--    Actual values live in `~/.vct-secrets/` or the OS keychain
--    (`crate::secrets`).
--  * Every per-project table CASCADEs on `projects.id` deletion.
--  * `config_json` columns let callers add fields without a schema
--    migration (the GUI is the only consumer of those blobs).
--  * `enabled` is separate from row existence so the GUI can toggle
--    without losing config.

-- ─── Agents ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS project_agents (
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent_name      TEXT NOT NULL,
    source          TEXT NOT NULL                       -- 'bundled'|'user'|'paid-module'|'project'
                    CHECK (source IN ('bundled','user','paid-module','project')),
    source_module   TEXT,                               -- module_id if source='paid-module'
    model           TEXT,                               -- 'sonnet'|'opus'|'haiku'|'inherit'|full ID
    enabled         INTEGER NOT NULL DEFAULT 1,
    file_path       TEXT,                               -- absolute path to the .md file (for diagnostics)
    config_json     TEXT NOT NULL DEFAULT '{}',         -- frontmatter snapshot + UI-only fields
    installed_at    INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (project_id, agent_name)
);
CREATE INDEX IF NOT EXISTS idx_project_agents_pid ON project_agents(project_id);
CREATE INDEX IF NOT EXISTS idx_project_agents_source ON project_agents(source);

-- ─── Skills ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS project_skills (
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    skill_name      TEXT NOT NULL,
    source          TEXT NOT NULL
                    CHECK (source IN ('bundled','user','paid-module','project')),
    source_module   TEXT,
    model           TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    file_path       TEXT,
    config_json     TEXT NOT NULL DEFAULT '{}',
    installed_at    INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (project_id, skill_name)
);
CREATE INDEX IF NOT EXISTS idx_project_skills_pid ON project_skills(project_id);

-- ─── Hooks ─────────────────────────────────────────────────────────────
-- One row per (project, event, matcher, command). A hook is "wired" when
-- its corresponding entry exists in `.claude/settings.json`. The launcher
-- mirrors that file here so it can show the wiring without parsing JSON
-- at every render.
CREATE TABLE IF NOT EXISTS project_hooks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    event           TEXT NOT NULL,                      -- 'PreToolUse'|'PostToolUse'|'SessionStart'|...
    matcher         TEXT NOT NULL DEFAULT '',           -- e.g. 'Edit(*.py)' — empty = always
    command         TEXT NOT NULL,                      -- shell command from settings.json
    source          TEXT NOT NULL DEFAULT 'project'
                    CHECK (source IN ('bundled','user','paid-module','project')),
    source_module   TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    timeout_ms      INTEGER,                            -- NULL = default
    config_json     TEXT NOT NULL DEFAULT '{}',
    installed_at    INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    UNIQUE (project_id, event, matcher, command)
);
CREATE INDEX IF NOT EXISTS idx_project_hooks_pid ON project_hooks(project_id);
CREATE INDEX IF NOT EXISTS idx_project_hooks_event ON project_hooks(project_id, event);

-- ─── Permissions ───────────────────────────────────────────────────────
-- Per-project (and optionally per-agent) permission grant. `subject` is
-- 'project' for project-wide grants or the agent_name for an agent-scoped
-- grant. `kind` enumerates what's being granted (write_scope, allowed_tool,
-- mcp_server). Multiple rows can share the same subject.
CREATE TABLE IF NOT EXISTS project_permissions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    subject         TEXT NOT NULL,                      -- 'project' | agent_name
    kind            TEXT NOT NULL                       -- 'write_scope'|'allowed_tool'|'denied_tool'|'mcp_server'|'permission_mode'
                    CHECK (kind IN ('write_scope','allowed_tool','denied_tool','mcp_server','permission_mode')),
    value           TEXT NOT NULL,                      -- glob, tool name, MCP server name, or mode
    config_json     TEXT NOT NULL DEFAULT '{}',
    granted_at      INTEGER NOT NULL,
    UNIQUE (project_id, subject, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_project_permissions_pid ON project_permissions(project_id);
CREATE INDEX IF NOT EXISTS idx_project_permissions_subject ON project_permissions(project_id, subject);

-- ─── Secret references ─────────────────────────────────────────────────
-- Records WHICH secrets a project requires + WHERE their value is
-- resolved from. Never stores the value itself.
CREATE TABLE IF NOT EXISTS project_secret_refs (
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    secret_key      TEXT NOT NULL,                      -- e.g. 'GITHUB_TOKEN'
    resolution      TEXT NOT NULL                       -- where to fetch the value
                    CHECK (resolution IN ('keychain-per-project','keychain-shared','keychain-global','file','env')),
    file_path       TEXT,                               -- if resolution='file' (e.g. ~/.vct-secrets/github_pat)
    env_name        TEXT,                               -- if resolution='env'
    source_module   TEXT,                               -- module_id that declared the secret, NULL = project itself
    required_for    TEXT NOT NULL DEFAULT '[]',         -- JSON array of agent_names that need it
    description     TEXT NOT NULL DEFAULT '',
    is_set          INTEGER NOT NULL DEFAULT 0,         -- launcher updates this after a presence check
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (project_id, secret_key)
);
CREATE INDEX IF NOT EXISTS idx_project_secret_refs_pid ON project_secret_refs(project_id);

-- ─── Knowledge graph binding ───────────────────────────────────────────
-- Maps a project to its KG collection name + embedding model. Existing
-- `kg_collection_access` (migration 001) handles WHO can read; this
-- table is the project's OWN binding.
CREATE TABLE IF NOT EXISTS project_kg_bindings (
    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role              TEXT NOT NULL DEFAULT 'primary'   -- 'primary'|'shared'|'archive'
                      CHECK (role IN ('primary','shared','archive')),
    collection_name   TEXT NOT NULL,                    -- e.g. 'ClaudeKnowledgeGraph'
    embedding_model   TEXT,                             -- e.g. 'qwen3-embedding:0.6b'
    embedding_dim     INTEGER,
    kg_dir_path       TEXT,                             -- absolute path to knowledge/ dir
    weaviate_url      TEXT,                             -- override; NULL = use launcher default
    config_json       TEXT NOT NULL DEFAULT '{}',
    updated_at        INTEGER NOT NULL,
    PRIMARY KEY (project_id, role)
);
CREATE INDEX IF NOT EXISTS idx_project_kg_bindings_pid ON project_kg_bindings(project_id);

-- ─── Code graph binding ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS project_codegraph_bindings (
    project_id              TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    collection_prefix       TEXT NOT NULL,              -- e.g. 'ClaudeOrchestrator' (CodeFunction etc. use this prefix)
    embedding_model         TEXT,                       -- e.g. 'CodeSage-Large-v2'
    embedding_dim           INTEGER,
    last_analyzed_commit    TEXT,                       -- git SHA at last `code-graph-analyze`
    last_analyzed_at        INTEGER,
    enabled                 INTEGER NOT NULL DEFAULT 1,
    config_json             TEXT NOT NULL DEFAULT '{}',
    updated_at              INTEGER NOT NULL,
    PRIMARY KEY (project_id)
);
