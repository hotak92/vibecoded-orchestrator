-- launcher.db — module-shipped MCP tool allowlist defaults (migration 023)
--
-- v0.2.34 Agent E (Phase 4 generalisation, 2026-05-25): per-tool allowlist
-- support extended from diagrams-only to ANY module-contributed MCP.
-- Schema migration 022 already shipped `project_mcp_tool_grants` (per-
-- project per-tool overrides). This migration adds the missing layer:
-- per-MODULE tool defaults declared in `vct-module.json::mcp_registration.
-- tool_allowlist`, persisted at install time + read by the hub's
-- /mcp-tool-grants route to compose with project overrides.
--
-- Design notes:
--   * One row per (mcp_name, tool_name). The defaults belong to the
--     wrapper MCP, NOT to any single project — so the table is scoped
--     by mcp_name, not project_id. The MODULE that ships the wrapper
--     owns the entry; we track `module_id` for traceability (so an
--     uninstall can clean up).
--   * `default_enabled` is the boolean the wrapper sees when no
--     per-project override exists. Same semantics as the hardcoded
--     `MERMAID_DEFAULT_ALLOWLIST` constants the pre-v0.2.34 hub route
--     carried.
--   * `description` is optional — best-effort sourced from upstream MCP's
--     `tools/list`. The launcher GUI's PermissionsTab renders it in the
--     per-tool checkbox tooltip; absence is non-fatal.
--   * Re-installing the SAME module replaces its rows atomically:
--     `reconcile_mcp_tool_defaults` does DELETE-then-INSERT inside a
--     transaction so a manifest update that REMOVES a tool drops the
--     stale row rather than orphaning it. Per-project overrides in
--     `project_mcp_tool_grants` survive (FK on tool_name is intentionally
--     absent so project rows can outlive a manifest re-shape).

CREATE TABLE IF NOT EXISTS module_mcp_tool_defaults (
    mcp_name        TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    default_enabled INTEGER NOT NULL DEFAULT 1,
    description     TEXT,
    module_id       TEXT NOT NULL,     -- module that shipped this default; FK omitted
                                       -- because the launcher's module-install row may
                                       -- not exist yet when defaults are seeded for
                                       -- bundled (diagrams) MCPs at startup.
    registered_at   INTEGER NOT NULL,
    PRIMARY KEY (mcp_name, tool_name)
);
CREATE INDEX IF NOT EXISTS idx_module_mcp_tool_defaults_module
    ON module_mcp_tool_defaults(module_id);
