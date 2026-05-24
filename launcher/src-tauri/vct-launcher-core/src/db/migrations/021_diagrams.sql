-- launcher.db — diagrams registry + snapshots + per-tool MCP grants +
-- per-project modules (migration 021)
--
-- Backs Phase 1.1 of the Excalidraw + Mermaid diagrams integration plan
-- (.claude/context/plans/diagrams-integration-excalidraw-mermaid-2026-05-24.md).
--
-- Five new tables land here in a single migration so the schema is
-- consistent at the boundary: a downstream caller that sees migration 021
-- applied can rely on every table being present.
--
-- Design notes:
--   * `project_diagrams` is the registry. One row per diagram file in
--     `.claude/diagrams/<category>/<name>.{mmd,excalidraw}`. The extended
--     metadata columns (`category_path`, `inferred_title`, `diagram_kind`,
--     `content_text`, `node_count`, `edge_count`, `chat_id`,
--     `linked_session_summary`) come from Phase 1.5 §1.5.2 — they are
--     derived-by-construction by the (out-of-scope-here) indexer pipeline.
--     This migration just provides storage; the indexer can land later
--     without a follow-up schema change.
--   * `diagram_snapshots` stores raw bytes in a BLOB. The plan calls for
--     gzipped bytes; the column accepts opaque bytes so the wrapper can
--     start with raw content and switch to gzipped content without a
--     schema change. `content_hash` is whatever hash the writer chose
--     (typically sha256 of the pre-compression bytes) and (diagram_id,
--     content_hash) is the dedup key — identical content fed twice
--     produces a single row.
--   * `diagram_access` mirrors `codegraph_access` exactly: same column
--     names, same CHECK constraint, same composite PK. The check level
--     is binary (`read` vs `none`) because diagrams have no
--     write-from-another-project semantics — a project owns its own
--     diagram files.
--   * `project_mcp_tool_grants` is the new per-tool allowlist primitive
--     (plan §1.5 / Phase 4). An empty table for (project_id, mcp_name)
--     means "fall through to the default-allowlist baked into
--     `bundled_tool_defaults.toml`" — that fallback lives in the wrapper
--     MCP layer, NOT here. This table only stores explicit overrides.
--   * `project_modules` is the per-project module-active flag the
--     conditional-CLAUDE.md primitive (Phase 1.5.7) reads. Seeded on
--     project create with `(module_name='diagrams', enabled=1)`. The
--     toggle UI flips `enabled`; the template re-renderer reads via
--     `is_module_active`.

CREATE TABLE IF NOT EXISTS project_diagrams (
    id                      INTEGER PRIMARY KEY,
    project_id              TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    diagram_name            TEXT NOT NULL,
    diagram_type            TEXT NOT NULL
                            CHECK(diagram_type IN ('mermaid','excalidraw')),
    file_path               TEXT NOT NULL,         -- relative to project root, e.g. .claude/diagrams/gui/auth/login.mmd
    category_path           TEXT NOT NULL,         -- e.g. "gui/auth" — split into path_tags at query time
    enabled                 INTEGER NOT NULL DEFAULT 1,
    -- Derived metadata (Phase 1.5; recomputed on save by the indexer):
    inferred_title          TEXT,
    diagram_kind            TEXT,                  -- flowchart/classDiagram/sequenceDiagram/excalidraw
    content_text            TEXT,                  -- mermaid source OR concatenated excalidraw text labels
    node_count              INTEGER,
    edge_count              INTEGER,
    -- Runtime context:
    chat_id                 TEXT,                  -- nullable; populated when Claude saved
    linked_session_summary  TEXT,                  -- nullable; first 200 chars of chat summary if available
    -- Standard:
    config_json             TEXT,                  -- extensibility blob
    created_at              INTEGER NOT NULL,
    updated_at              INTEGER NOT NULL,
    UNIQUE(project_id, diagram_name)
);
CREATE INDEX IF NOT EXISTS idx_diagrams_category
    ON project_diagrams(project_id, category_path);
CREATE INDEX IF NOT EXISTS idx_diagrams_chat
    ON project_diagrams(chat_id) WHERE chat_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_diagrams_kind
    ON project_diagrams(diagram_kind);

CREATE TABLE IF NOT EXISTS diagram_snapshots (
    id              INTEGER PRIMARY KEY,
    diagram_id      INTEGER NOT NULL REFERENCES project_diagrams(id) ON DELETE CASCADE,
    content_hash    TEXT NOT NULL,                 -- sha256 of pre-compression bytes
    content         BLOB NOT NULL,                 -- opaque bytes (may be gzipped — writer's choice)
    created_at      INTEGER NOT NULL,
    trigger         TEXT NOT NULL,                 -- 'manual' | 'auto_pre_edit_save' | 'auto_interval'
    label           TEXT,                          -- optional user label
    UNIQUE(diagram_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_diagram
    ON diagram_snapshots(diagram_id, created_at DESC);

CREATE TABLE IF NOT EXISTS diagram_access (
    grantor_project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    grantee_project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    access_level        TEXT NOT NULL
                        CHECK(access_level IN ('read','none')),
    granted_at          INTEGER NOT NULL,
    PRIMARY KEY (grantor_project_id, grantee_project_id)
);

CREATE TABLE IF NOT EXISTS project_mcp_tool_grants (
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    mcp_name        TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project_id, mcp_name, tool_name)
);
CREATE INDEX IF NOT EXISTS idx_tool_grants_lookup
    ON project_mcp_tool_grants(project_id, mcp_name);

CREATE TABLE IF NOT EXISTS project_modules (
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    module_name     TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    registered_at   INTEGER NOT NULL,
    PRIMARY KEY (project_id, module_name)
);
