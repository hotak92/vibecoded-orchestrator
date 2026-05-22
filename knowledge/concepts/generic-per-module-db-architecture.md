---
title: Generic Per-Module DB Architecture
type: concept
tags: [mid-level-architecture, VCT-Launcher, database, schema, paid-modules, extensibility, single-writer, implemented]
created: 2026-05-22T18:10:00Z
updated: 2026-05-22T18:10:00Z
valid_from: 2026-05-22T00:00:00Z
valid_until: null
status: active
---

# Generic Per-Module DB Architecture

Design pattern shared by every per-`(project × module)` table in `launcher.db`. Each table is keyed `(project_id, module_id)` and applies to every paid (and free) module without per-module schema work. Replaces the older single-module pattern (one RL-specific column per module added to the `projects` table) which doesn't scale past 1-2 modules.

The v0.2.26 declarative-action dispatcher made the scale problem urgent: with [[buildsOn::Module-Contributed GUI Tabs Framework]]'s ActionDescriptor::Http now resolving `(project × module) → port` on every dispatch, **every** paid module needs the same port-resolution path. Migration 017 generalizes the last RL-specific column (`projects.rl_port`) into the generic `module_ports` table.

## Problem statement — why the old pattern doesn't scale

Pre-v0.2.26, `projects.rl_port` was the single column tracking where vct-rl-reranker's container was reachable. It was added by migration 014 specifically for RL. With the planned addition of vct-coordination (Q3) and vct-transcrypt (Q4) — plus an open-ended pipeline of future paid modules — the per-column approach would require:

1. A new SQL migration per module (`018_coordination_port.sql`, `019_transcrypt_port.sql`, …).
2. A new pair of read/write helpers per module (`get_coordination_port` / `set_coordination_port`, etc.).
3. A new launcher rebuild every time the marketplace adds a module.
4. Schema bloat on the `projects` table (column count grows monotonically).

None of those are fatal individually. Together they violate the v0.2.26 charter goal: **adding a paid module should be a pure manifest + container concern, not a launcher concern**.

## Solution — one generic table, primary key `(project_id, module_id)`

Migration 017 (v0.2.26) introduces `module_ports`:

```sql
CREATE TABLE module_ports (
    project_id   TEXT NOT NULL,
    module_id    TEXT NOT NULL,
    port         INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (project_id, module_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
```

One row per `(project × module)`. Modules that don't expose HTTP (CLI-only modules, future shell-action modules) simply don't register a row — the dispatcher refuses to dispatch when `get_module_port` returns `None` and surfaces a clear error.

Same shape applies to several already-generic tables (next section), so `module_ports` slots into an existing pattern rather than inventing a new one.

## The full generic-table family

Three other tables already followed the `(project_id, module_id, …)` pattern before v0.2.26; `module_ports` is the fourth and brings the family to parity:

| Table | Migration | Purpose | Single writer |
|---|---|---|---|
| `module_installs` | 001 | Module install state per project: status, install_dir, license_grant id, install timestamp. Consumed by license-check pipeline + the launcher's Modules tab. | Installer engine (`installer_engine::run_install`). GUI reads only. |
| `module_settings` | 001 | Per-control persisted values for module-contributed GUI tabs. Keyed `(project_id, module_id, setting_key)`, value as JSON blob. Consumed by `ModuleConfigTab.svelte` via `get_module_setting` / `set_module_setting`. | GUI renderer (user-driven; one writer per click). |
| `module_weights_state` | 016 | Per-(project × module × embedding-source) weights metadata: bundle version, downloaded_at, sha256, active flag. Consumed by the weekly-rotation poller + RL container's startup probe. | Weights-rotation poller in vct-hub. |
| `module_ports` | 017 (v0.2.26) | Per-(project × module) HTTP port allocation. Consumed by the declarative-action dispatcher + every env-injection path that needs the module's URL. | vct-hub's `module_supervisor` (allocation + persistence). |

Together they form a **complete generic per-module state surface**: install state, user-tuned settings, weight-bundle state, port allocation. Every column required for a module's lifecycle answers to a `(project_id, module_id)` key. No module-specific columns in any of these tables.

## Single-writer principle

Each table has exactly one writer in the system. The launcher GUI does NOT write directly:

- `module_installs` — written only by `installer_engine::run_install` (during install) and `installer_engine::run_uninstall`.
- `module_settings` — written via the generic `set_module_setting` Tauri command; the GUI invokes it on user input, but no other component writes there.
- `module_weights_state` — written by `vct-hub::weights_rotation`.
- `module_ports` — written by `vct-hub::module_supervisor` via `set_module_port` / `ensure_module_port`.

WHY single-writer:
- **Race-free allocation** for `module_ports`: a port picker that's also the sole writer cannot conflict with itself. Two clients allocating ports for two different modules cannot race because the supervisor serialises its own work.
- **Predictable behaviour for debugging**: when a row's value is unexpected, there's exactly one code path that could have written it.
- **License-state enforcement** for `module_installs`: only the installer can mutate install state. The launcher GUI showing "installed" without a license_grant id couldn't have written the row itself.

The pattern mirrors v0.2.25's single-writer fix for the hub-only artefact-write path (see [[relatedTo::Launcher Hub Single-Writer Principle]]).

## License-gating boundary

`module_ports` tracks **allocation**, NOT **license state**. License-active checks live in `module_installs.status` + the license-check pipeline:

- A port row CAN exist for a module whose license has lapsed. The container just isn't started by the supervisor; the port remains reserved.
- The dispatcher checks install + license state separately from port resolution: `module_installs.status = 'installed'` AND license currently valid → dispatch proceeds; either gate fails → dispatcher returns a structured error.
- This separation lets the orchestrator gracefully recover when a license re-validates: the port allocation survives, only the container start needs to re-run.

## What this enables — zero-launcher-code paid modules

The full surface for adding a new paid module after v0.2.26:

1. **Write the module's `vct-module.json` manifest** with [[buildsOn::Module-Contributed GUI Tabs Framework]]'s `gui.config_tab` using `ActionDescriptor::Http` for every action (no Tauri commands needed).
2. **Ensure the install path writes the four generic-table rows** at install time: `module_installs` (status='installed'), `module_settings` (initial defaults for each control), `module_weights_state` (if the module ships rotated weights), `module_ports` (allocated port from the supervisor's range).
3. **Ship the container** to the private registry with the manifest's `install.container.image` pointing at it.

That's the entire surface. **No launcher changes required.** The launcher reads from these four tables; the manifest declares the GUI; the dispatcher executes HTTP calls against the resolved port.

This is the v0.2.26 charter goal made concrete: a Pro-tier user installs a new paid module → the launcher sees four DB rows + one manifest + one container image → renders the tab, starts the container, dispatches actions on click. Zero code path through the launcher binary that's module-aware.

## Migration 017 specifics

The file lives at `launcher/src-tauri/vct-launcher-core/src/db/migrations/017_module_ports.sql`. Walkthrough:

```sql
-- Create the table (IF NOT EXISTS for defensive re-run safety).
CREATE TABLE IF NOT EXISTS module_ports ( ... );
CREATE INDEX IF NOT EXISTS idx_mp_project ON module_ports(project_id);
CREATE INDEX IF NOT EXISTS idx_mp_module ON module_ports(module_id);

-- Backfill from migration 014's projects.rl_port column.
INSERT OR IGNORE INTO module_ports (project_id, module_id, port, updated_at)
    SELECT id, 'vct-rl-reranker', rl_port,
           CAST(strftime('%s', 'now') AS INTEGER) * 1000
      FROM projects
     WHERE rl_port IS NOT NULL;
```

Key properties:

- **Backfill semantics** — `INSERT OR IGNORE` is idempotent. The migration runner already gates by `_schema_migrations` version (single execution per DB), but defense-in-depth: re-running can't clobber a hand-written row.
- **`updated_at` is approximate** — set to "migration time" rather than "original allocation time" (which the schema never stored). Future writes set the real timestamp.
- **Downgrade survives intact** — if a user reverts the launcher to v0.2.25, the new `module_ports` table simply isn't queried. The `projects.rl_port` column is still in place (we didn't drop it — see deprecation path below). Manual rollback is a no-op.
- **FK `ON DELETE CASCADE`** — deleting a project deletes its module_ports rows. No orphan-row cleanup logic needed.

## Future deprecation path for `projects.rl_port`

The column is kept in place for v0.2.26 — removing it would break older `vct-hub` versions that still read from it during the transition window. Plan:

1. **v0.2.26 (this release)** — both `projects.rl_port` and `module_ports` exist. `get_project_rl_port` is now a thin wrapper around `get_module_port(project_id, "vct-rl-reranker")`. Writes still go to the new table only.
2. **v0.2.27+ confirmation** — verify every consumer (env writer in `vct-hub`, MCP wiring in `claude_mcp_servers/`, any installer-internal paths) is off the column.
3. **v0.2.28+ drop** — a future migration drops the column. Users on stale `vct-hub` versions get an upgrade prompt before the drop migration runs.

The `get_project_rl_port` helper in `db/projects.rs` retains its signature throughout this transition so callers don't churn. The implementation is already module-id-generic underneath.

## Related

- [[buildsOn::Module-Contributed GUI Tabs Framework]] — the declarative-action dispatcher is the consumer that made the generic table architecture urgent. The dispatcher's `module_dispatch_action` calls `db.get_module_port` on every dispatch.
- [[relatedTo::Launcher Hub Single-Writer Principle]] — same write-discipline pattern applied to a different surface (artefact writes via the hub).
- [[relatedTo::Launcher Binary Metadata Schema]] — sibling launcher.db schema concept covering the install-state shape.
- [[implements::Single-Writer Principle]] — generic pattern that the four tables collectively instantiate.
- [[relatedTo::Paid Module License Gating]] — adjacent decision surface; `module_installs.status` + license-check pipeline gate dispatch independently of port resolution.
