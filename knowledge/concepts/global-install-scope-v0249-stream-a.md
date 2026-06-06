---
title: Global install scope (v0.2.49 Stream A)
type: concept
tags: [vco, launcher, paid-modules, container, supervisor, sqlite-migration, mid-level-architecture, implemented]
created: 2026-06-06T21:30:00Z
updated: 2026-06-06T21:30:00Z
valid_from: 2026-06-06T00:00:00Z
valid_until: null
status: active
---

# Global install scope (v0.2.49 Stream A)

Adds an `install.scope` discriminator to the module manifest with two values: `per_project` (default, pre-v0.2.49 behaviour) and `global`. A `global`-scope module installs ONCE per machine and runs ONE container; per-project personalization happens INSIDE the container via request headers (e.g. `X-VCT-Project-ID` for the RL Reranker).

[[implements::Manifest scope routing per project_id IS NULL]]
[[uses::SQLite partial unique indexes]]
[[relatedTo::paid-module-install-update-foundation-2026-06-02]]
[[relatedTo::supervisor-image-resolution-variant-gap-2026-06-04]]

## Motivation

For modules like RL Reranker, N per-project containers waste:
- ~1.5 GB VRAM each (4 projects on a 4080 SUPER = 6 GB just for RL)
- ~5.5 GB image disk per pull (vs once shared)
- N ports allocated per project + name-to-port map in DB

The application-layer routing approach (one container, per-project model heads via LRU cache) wins on 4/7 dimensions vs per-project containers. Fault isolation is the only loss, mitigated by `auto_restart: true` + stateless inference (weights load on container start).

## Schema (migration 027)

`module_installs.project_id` becomes nullable. Two partial unique indexes replace the table-level `UNIQUE(project_id, module_id)`:

- `idx_mi_unique_per_project (project_id, module_id) WHERE project_id IS NOT NULL` — preserves the v0.2.20–v0.2.48 per-project uniqueness.
- `idx_mi_unique_global (module_id) WHERE project_id IS NULL` — at most one global row per module.

SQLite's `NULL ≠ NULL` semantics mean a plain table-level `UNIQUE(project_id, module_id)` would let multiple global rows coexist — the partial-index split is the correct fix. The `ON CONFLICT` clause must include `WHERE project_id IS NOT NULL` to drive the partial-per-project index correctly.

Recreate-and-copy pattern mirrors migration 013 (orchestrator_root). FK landscape simpler than 013 (no inbound FKs to `module_installs.id` today); `PRAGMA foreign_keys = OFF` kept as defence-in-depth + consistency.

## Lifecycle

| Path | Per-project | Global |
|---|---|---|
| Install row | `insert_module_install(id, project_id, module_id, ...)` | `insert_global_module_install(id, module_id, ...)` |
| Container name | `{module_id}-{project_slug}` (via `resolve_container_name`) | bare `{module_id}` (via `resolve_global_container_name`, strips trailing `-{project_slug}`) |
| Container port | per-project `rl_port` allocated in `RL_PORT_RANGE_LO..=HI` | machine-wide `GLOBAL_RL_PORT = 11443` |
| Volume placeholders | `{project_slug}` ⇒ slug | `{project_slug}` ⇒ literal `"global"` |
| Resume sweep branch | walks `Some(project_id)` rows | walks `None` rows |
| Uninstall | `delete_module_install(project_id, module_id)` | `delete_global_module_install(module_id)` |

The launcher-side `start_global_container_for_module` and hub-side `start_global_container_supervisor` produce byte-identical `podman run` argv via the shared `vct_launcher_core::services::container_runtime::build_podman_run_args_global` helper (`DEDUP_SENTINEL` asserts the single-source contract).

## Auto-migration

On every launcher boot, `auto_migrate_per_project_to_global` runs BEFORE the resume sweep. For each installed module:

1. Resolve the on-disk extracted manifest. Skip if not found.
2. Skip if `install.scope != global`.
3. Skip if a global row already exists (idempotency).
4. Stop+remove every per-project container, delete every per-project row.
5. Insert one global row + audit-log `module_migrated_to_global_scope`.
6. Best-effort start the global container (failures land in `last_error`; resume sweep retries on next boot).

Per the v0.2.49 decision: no user prompt, no legacy support. The only existing users are the maintainer + Fabio.

## Backwards compat

`#[serde(default)]` on `InstallBlock::scope` deserializes every pre-v0.2.49 manifest as `per_project`. Strict-mode unchanged.

Frontend `ModuleInstallRow.project_id: string | null` is a non-breaking widening (every pre-existing usage assumes string; null narrows out at runtime). Stream D's GUI work consumes the null case.

## Files touched

- `launcher/src-tauri/vct-launcher-core/src/manifest.rs` — `InstallScope` enum + `InstallBlock.scope` field
- `launcher/src-tauri/vct-launcher-core/src/db/migrations/027_module_installs_nullable_project.sql`
- `launcher/src-tauri/vct-launcher-core/src/db/migrations.rs` — registration + tests
- `launcher/src-tauri/vct-launcher-core/src/db/models.rs` — `ModuleInstallRow.project_id: Option<String>`
- `launcher/src-tauri/vct-launcher-core/src/db/modules.rs` — `insert_global_module_install`, `get_global_module_install`, `delete_global_module_install`, `set_global_module_container_name`, `set_global_module_last_error`, `set_global_module_status`, `list_per_project_installs_for_module`, `list_global_module_installs`
- `launcher/src-tauri/vct-launcher-core/src/services/container_runtime.rs` — `resolve_global_container_name`, `rl_placeholders_global`, `build_podman_run_args_global`, `ensure_volume_host_dirs_global`
- `launcher/src-tauri/src/commands/modules.rs` — install/uninstall branch on `is_global`; new `uninstall_global_module` helper
- `launcher/src-tauri/src/commands/module_service.rs` — `start_global_container_for_module`, `start_global_container_after_install`, `auto_migrate_per_project_to_global`, `GLOBAL_RL_PORT`
- `launcher/src-tauri/src/lib.rs` — boot hook calls `auto_migrate_per_project_to_global` before `resume_containers_on_startup`
- `launcher/src-tauri/vct-hub/src/module_supervisor.rs` — resume branches on `project_id` presence; `start_global_container_supervisor`
- `launcher/src-tauri/vct-hub/src/modules_api.rs` — `InstalledRowView.project_id: Option<String>`
- `launcher/src/lib/types/launcher.ts` — `ModuleInstallRow.project_id: string | null`
- `docs/schemas/vct-module.schema.json` — `InstallScope` definition + `InstallBlock.scope` property

## Tests

Core: 502 + 9 new global-install tests + 6 new migration-027 tests + 6 new manifest-serde tests + 7 new container-runtime tests = 530 passing.
Hub: 215 + 2 new global-resume tests = 217 passing.
Launcher: 1336 + 4 new auto-migration tests = 1340 passing.

## Integration notes (for Streams B/C/D)

- Stream B (per-project enable toggle): when global-scope modules ship, `module_settings(project_id, module_id, 'enabled_for_project')` is the gate. Stream B owns this; Stream A leaves a TODO in the uninstall path.
- Stream C (container API): the manifest for `vct-rl-reranker` v0.2.10 should set `install.scope = global` and the container should accept `X-VCT-Project-ID`. Stream A's launcher path is ready; the manifest field is the activation gate.
- Stream D (GUI): `InstalledRowView.project_id: Option<String>` is the wire contract. Render the global tile once across all project tabs (when `project_id == null`); render per-project tiles per-project. `auto_migrate_per_project_to_global` may surface migrations to the GUI via the `module_migrated_to_global_scope` audit-log entry.
