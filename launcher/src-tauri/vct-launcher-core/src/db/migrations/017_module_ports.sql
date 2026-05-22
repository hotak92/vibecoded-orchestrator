-- launcher.db — module_ports (migration 017, v0.2.26)
--
-- Generic per-(project × module) HTTP port table. Replaces the RL-only
-- `projects.rl_port` column (migration 014) as the source of truth for
-- where a module's container is reachable.
--
-- WHY: v0.2.26 ships the declarative module-action dispatcher. The
-- dispatcher's HTTP descriptor (`{ kind: "http", method, path, ... }`)
-- needs to resolve `module_id → port` at dispatch time. The RL-only
-- column scales to one module; we're about to add `vct-coordination`
-- and `vct-transcrypt` and any further paid modules without per-module
-- launcher changes. One generic table → N modules.
--
-- KEY SHAPE: (project_id, module_id). One port per (project × module).
-- Modules that don't expose HTTP (CLI-only / future shell-action
-- modules) simply don't register a row. The dispatcher refuses to
-- dispatch when the lookup returns None and produces a clear error.
--
-- BACK-COMPAT WITH MIGRATION 014: the `projects.rl_port` column stays
-- in place. We backfill from it here so existing installs keep the
-- already-allocated port. Future writes go to `module_ports` only;
-- `get_project_rl_port` becomes a thin wrapper that reads from the
-- new table. The column will be retired in a later migration once
-- every consumer (env writer, MCP wiring) is confirmed off the old
-- column AND a release has shipped to give users time to upgrade.
--
-- SINGLE-WRITER PRINCIPLE: the supervisor in `vct-hub::module_supervisor`
-- owns the write path (`set_module_port` / `ensure_module_port`). The
-- launcher GUI does NOT write to this table directly; controls that
-- need a port read via the helpers in `db/module_ports.rs`. The GUI
-- writes module settings via `set_module_setting` (already generic on
-- `module_settings` table) — port allocation is system-observed, not
-- user-editable.
--
-- LICENSE GATE: this table tracks port allocation, NOT license state.
-- The `module_installs.status` column + the license-check pipeline are
-- the canonical license-gated install signal. A port row CAN exist for
-- a module whose license has lapsed (the container is just not started
-- by the supervisor); the dispatcher checks the install + license state
-- before dispatching, separately from port resolution.
--
-- FK semantics:
--   * project_id → projects.id ON DELETE CASCADE: row dies with project.
--
-- Forward-only, idempotent (the migrations runner gates by version in
-- `_schema_migrations`; this file is only ever executed once per DB).

CREATE TABLE IF NOT EXISTS module_ports (
    project_id   TEXT NOT NULL,
    module_id    TEXT NOT NULL,
    port         INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (project_id, module_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mp_project ON module_ports(project_id);
CREATE INDEX IF NOT EXISTS idx_mp_module ON module_ports(module_id);

-- Backfill from migration 014's `projects.rl_port` column. The
-- INSERT OR IGNORE form makes the backfill idempotent: if the
-- migration re-runs (it shouldn't, the runner gates by version, but
-- defense-in-depth) we don't clobber a hand-written row. The
-- `updated_at` value is approximate ("backfilled at migration time"
-- rather than "the original allocation time" which we no longer have).
INSERT OR IGNORE INTO module_ports (project_id, module_id, port, updated_at)
    SELECT id, 'vct-rl-reranker', rl_port, CAST(strftime('%s', 'now') AS INTEGER) * 1000
      FROM projects
     WHERE rl_port IS NOT NULL;
