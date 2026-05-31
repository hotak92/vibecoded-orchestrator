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

## v0.2.31 — module-shipped SQL migrations (the substrate evolves)

The pattern this node originally documented (launcher-owned generic tables consumed by modules) extends in v0.2.31 to: **modules ship their own SQL** alongside their manifest; the launcher applies the migrations idempotently at install/update time and exposes hub endpoints for module-side reads/writes.

### Manifest declaration

`vct-module.json::db` block (optional; absent = no module-owned tables):
```jsonc
"db": {
  "migrations_dir": "db/",   // relative to module repo root
  "namespace": "rl"           // every table created must start with "rl_"
}
```

### Apply pipeline (launcher-side, v0.2.31)

1. At install/update, launcher reads `manifest.db`, scans `db/[0-9]+_*.sql` files sorted by name.
2. For each: compute SHA256, check against `module_db_migrations` table (launcher schema, NEW in v0.2.31).
3. Matching SHA → skip (idempotent).
4. Different SHA on same `(module_id, filename)` → REFUSE (mutation forbidden; ship a new `00N+1` file instead).
5. Unseen → execute SQL inside a transaction; on success, INSERT row.

Namespace enforcement: launcher parses each `CREATE TABLE` statement, asserts the table name starts with `{namespace}_`. ALTER TABLE on non-namespaced tables is refused. FK clauses referencing launcher-owned tables (e.g. `projects.id`) are allowed.

### Hub endpoints for module-side reads/writes

REST under `/api/v1/modules/{module_id}/db/projects/{project_id}/rows/{table}` — 5 verbs (GET row, GET list, POST upsert, PATCH partial, DELETE). Bearer-token auth via `VCT_MODULE_TOKEN` env (per-install shared secret in v0.2.31; JWT in v0.2.32+). Token refresh via `POST /api/v1/modules/{module_id}/token/refresh`. `?fields=col1,col2` projection on GET for cheap dashboard reads.

### vct-rl-reranker as first consumer (paid-module v0.2.6 → v0.2.7)

| SQL file | Table | What |
|---|---|---|
| `db/0001_rl_state.sql` | `rl_state` | Live state cache mirroring `/state_summary` endpoint (dynamic_types, marker_present, sidecar_dir_present, model_loaded, arch_version, emb_dim, active_embedding) |
| `db/0002_rl_weights_state.sql` | `rl_weights_state` | Per-(project, embedding_source) active checkpoint metadata. Replaces the launcher's legacy `module_weights_state` (which was dropped in v0.2.31 migration 020 — zero production users existed; no backfill needed). |
| `db/0003_rl_weights_state_add_weights_version.sql` (v0.2.7) | ALTER `rl_weights_state` | Adds `weights_version` (semver) + `embedding_source` columns. |
| `db/0004_rl_global_weights_available.sql` (v0.2.7) | `rl_global_weights_available` | Per-embedding-source cache of latest shipped weights (download_url + sha256 + local_downloaded + polled_at). Launcher-owned writes (JWT stays out of container). |

**Container is sole writer** for the runtime state tables (`rl_state`, `rl_weights_state`). Launcher is sole writer for the discovery table (`rl_global_weights_available`) — keeps the license JWT out of the container's trust boundary.

**Decision artefacts**:
- Spec: `.claude/context/plans/rl-module-launcher-db-tables-spec-2026-05-23.md`
- Shared plan: `.claude/context/plans/FINAL-v0.2.31-shared-plan-2026-05-23.md`
- v0.2.31 ship details: `.claude/context/plans/v0.2.31-plan-2026-05-23.md`
- v0.2.7 manifest redesign: `paid-modules/vct-rl-reranker/vct-module.json` (worktree, uncommitted as of 2026-05-24)

### Operational lessons from v0.2.31's first consumer

1. **Migration numbering is per-module-namespace, not global.** RL ships `0001`, `0002`, `0003`, `0004`. If MAO later ships its own `db/0001_*.sql`, it lives in MAO's namespace; the `module_db_migrations` table key is `(module_id, filename)` so no collision.
2. **Cleanup of the OLD pattern's RL-specific column (`projects.rl_port`)** is deferred to whenever any production install has a populated launcher.db column to drop safely. Pre-v0.2.31 the table was a launcher-side artifact; post-v0.2.31 the RL module's `rl_state` covers the same data + more.
3. **No backward-compat ever needed for paid-module v0.2.5 → v0.2.6 transition** because zero production users existed on v0.2.5. This shaped the v0.2.31 ship sequence (migration 020 just DROPs old `module_weights_state`; no backfill helper, no double-write feature flag).
4. **Composite-key UPSERTs in the hub endpoints** use `f"{project_id}:{embedding_source}"` as the URL-path key. Hub parses back into two-column PK on write. Pattern generalizes to any composite-key table; documented in the hub endpoints spec.

## Related

- [[buildsOn::Module-Contributed GUI Tabs Framework]] — the declarative-action dispatcher is the consumer that made the generic table architecture urgent. The dispatcher's `module_dispatch_action` calls `db.get_module_port` on every dispatch.
- [[relatedTo::Launcher Hub Single-Writer Principle]] — same write-discipline pattern applied to a different surface (artefact writes via the hub).
- [[relatedTo::Launcher Binary Metadata Schema]] — sibling launcher.db schema concept covering the install-state shape.
- [[implements::Single-Writer Principle]] — generic pattern that the four tables collectively instantiate.
- [[relatedTo::Paid Module License Gating]] — adjacent decision surface; `module_installs.status` + license-check pipeline gate dispatch independently of port resolution.
