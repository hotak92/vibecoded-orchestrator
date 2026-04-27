# VCT Launcher

Desktop control plane for `vibecoded-orchestrator`. Tauri 2 (Rust) backend, SvelteKit frontend, single native window on Windows / macOS / Linux. Manages project lifecycle, module catalog, secrets, licensing, and Claude Code surface configuration. Rust source in `launcher/src-tauri/src/`; SvelteKit UI in `launcher/src/`.

---

## Project Lifecycle

Projects are the launcher's primary unit. Each one has a stable UUID, a slug for URLs, an assigned host (`base` or `mao`), and three env files written in lockstep so VS Code, the Claude Code CLI, and the Desktop app all see the same per-project configuration. Deletion never touches user code on disk — it only removes the launcher's DB row.

### Project Creation
`create_project_v2` in `commands/projects_v2.rs` creates a folder (`mkdir -p` if absent), generates a UUID v4 id, derives a URL slug, inserts a DB row, writes per-project env files to three surfaces, and records an audit event — all in one command.

### Project Host Assignment
Each project is assigned one host at creation: `"base"` (Claude Orchestrator) or `"mao"` (MAO). Enforced at the DB level (`CHECK (host IN ('base','mao'))`); cannot be changed without `switch_project_host_v2`.

### Host Switch with Module Pruning
`switch_project_host_v2` transitions between `base` and `mao` hosts, automatically removing MAO-only modules (suffix `-mao` or known MAO-only IDs). Returns `{project, modules_removed, modules_preserved}`.

### Project Rename with Slug Regen
`rename_project_v2` regenerates the URL slug from the new name. Old `/p/<old-slug>` URLs return 404 with a friendly message — they do not silently redirect.

### Project Deletion (Zero-Destruction)
`delete_project_v2` removes the DB row (cascading to `module_installs`, `project_agents`, etc.) but never touches the user's folder on disk. The `delete_folder` parameter is accepted for API parity but is intentionally a no-op.

### Legacy Project Commands (`commands/projects.rs`)
Five commands kept alongside `projects_v2.rs` for the small set of React components not yet migrated to the SQLite-backed v2 layer: `create_project`, `get_projects`, `update_project`, `open_project`, `close_project`. Backed by a JSON file at `~/.vct/projects.json`. Slated for removal once UI migration is complete; new code should use the v2 commands.

### Auto-select Single Project
The `projects` store auto-selects a project when exactly one exists, so a freshly-onboarded user never lands on an empty state.

### Stale Selection Recovery
On `projects.load()`, if the previously selected project id no longer exists, the selection is cleared gracefully rather than leaving the UI broken.

### Per-project Env File Tri-write
On project create (and update), `write_project_env_files` writes `KG_COLLECTION`, `PROJECT_NAME`, `DEVELOPMENT_COLLECTION`, `CONVERSATION_COLLECTION` to three surfaces simultaneously.

<details>
<summary>Details</summary>

Three surfaces written:
1. `.vscode/settings.json` → `claude-code.env` block (VS Code extension)
2. `.claude/env` → POSIX `export` file sourced by `tools/claude` wrapper or shell rc (CLI)
3. `.claude/settings.json` → `env` block (canonical path read by CLI, Desktop app, and VS Code extension)

All three use **read-merge-write** semantics: existing keys in the file are preserved. Only the specific keys being written are replaced. Corrupted JSON is handled with a warning and safe overwrite. Covered by four unit tests in `commands/projects_v2.rs`.

</details>

### KG Collection Name Sanitization
`sanitize_kg_collection` converts any project display name into a Weaviate-safe collection name (TitleCase, alphanumeric only, leading-digit guard). `"my-project"` → `"MyProject"`, `"123-foo"` → `"P123Foo"`.

### Launch in Editor / Terminal
`launch_project_in_editor` opens the project folder in VS Code (`code <folder>`) or spawns a system terminal running `claude`. The `surface` parameter accepts `"vscode"`, `"cli"`, or `"auto"` (prefers `code` on PATH, falls back to `claude`). Covers gnome-terminal, konsole, xterm (Linux), Terminal.app (macOS), Windows Terminal / PowerShell (Windows).

---

## Onboarding Flow

A four-step wizard at first launch: welcome, infrastructure detection (probe ports, offer to reuse running services), install configuration (path, container sharing, volume location), and an optional GitHub PAT. Completion is gated by a regression-test invariant: the wizard must always finish with at least one project row in the DB.

### 4-Step Onboarding Wizard
`OnboardingWizard.svelte` guides new users through: welcome → infrastructure detection → install configuration (path, container sharing, volume location) → GitHub PAT (optional). Completion persists `vct.onboarding_complete` to localStorage.

### Shared Container Detection
Step 3 probes the three default service ports (Weaviate, Ollama, code-embed). If all are already running, the wizard shows "Reusing existing services." The `useSeparateContainers` checkbox maps to `VCT_FORCE_SEPARATE_CONTAINERS=1` for advanced isolation.

### Volume Location Picker
On first install with no existing named volumes, the user can choose between the runtime default (`~/.local/share/containers/storage/volumes/`) and a custom path. A `docker-compose.override.yml` with bind-mount overrides is written to `infrastructure/`. When existing volumes are detected, the picker is replaced by a read-only info panel — no override is generated to avoid masking existing data.

### Optional GitHub PAT
Step 3 offers an optional GitHub PAT input (`githubPat`). Saved to the OS keychain for future auto-update flows. Skipping does not block onboarding completion.

### Onboarding Finish Regression Guard
A unit test (`onboarding_finish_inserts_project_row`) verifies that completing the wizard always produces at least one DB project row — guards against a regression where the flow ended with zero projects.

---

## Catalog & Modules

The Catalog is where projects pick up new capabilities. Module manifests are the unit of distribution — bundled core manifests ship with the launcher; user-added or paid modules live under `~/.vct/modules/`. The installer engine refuses to run shell metacharacters inside post-install commands, so a malicious manifest cannot inject `&&` or `$(..)` even if a user trusts the wrong source.

### Module Catalog Browser (`/modules`)
`ModuleCatalog.svelte` lists modules from `list_module_catalog` (scans bundled manifests + `~/.vct/modules`). Cross-references `list_installed_modules(project_id)` for install state. Filter pills: All / Free / Pro / Installed. Full-text search on name + description.

### Module Kinds
Catalog entries carry a `kind` field controlling how the card renders: `bundled` (always installed, cannot be uninstalled), `available` (installable), `installed` (has Reconfigure / Uninstall), `subcomponent` (ships with a parent module), `coming_soon` (announced, no Install button).

### Coming Soon Tier + Target
`coming_soon_tier` and `coming_soon_target` fields display the planned tier (e.g. `"pro"`) and shipping window (e.g. `"Q3 2026"`). Reserved for roadmap-committed items only.

### Bundled Core Manifests
Seven manifest JSON files under `launcher/bundled_manifests/` ship with the launcher binary and are copied to `~/.vct/bundled_manifests/` on first launch: `vct-kg`, `vct-codegraph`, `vct-ollama`, `vct-search`, `vct-code-embedding`, `vct-hub-api`, `vct-session-state`. All free/AGPL-3.0, all auto-install on first run.

<details>
<summary>Details</summary>

Manifests contain only metadata + install instructions (git_clone / pip / npm methods). Module binaries are NOT bundled — the launcher fetches them at install time. This keeps the launcher binary small. See `launcher/bundled_manifests/README.md`.

</details>

### Module Manifest Schema (`vct-module.json`)
`manifest.rs` parses the full module manifest format: `id`, `name`, `version`, `description`, `category`, `tags`, `compatibility` (hosts), `license` (required, variant_ids, min_orchestrator_tier), `requirements` (RAM, OS), `install` (method + post_install commands), `secrets`, `settings`, `runtime`, `mcp_registration`, `setup_wizard`, `upgrade`, `uninstall`, `provides/consumes`. Unknown top-level fields are silently ignored for forward compatibility.

### Installation Engine (`installer_engine.rs`)
`run_install` walks the install steps: git clone / tarball / pypi / npm fetch, then `post_install` commands. Commands are tokenized via `shlex` and spawned directly — never through a shell — so manifests can't smuggle `&&`, `;`, or `$(..)`. Install paths are validated to stay under `~/.vct/modules/` (path traversal guard). Progress emits as `module://install-progress` Tauri events with stage + percent.

### Admin Visibility Filter
Modules with `visibility: "private"` or `"test"` are hidden from non-admin users. The `isAdminUser` derived store gates this filter on `tier === 'admin'`.

### License-Gated Install
Modules with `license_required: true` and `is_licensed: false` show "Upgrade to Pro" instead of Install. The license check reads from the `tier_cache` table (offline-safe, 3-day grace). → See [06-license-and-commercial.md](06-license-and-commercial.md#tier-cache-launcher) for the full tier cache spec.

### Module Enable/Disable Toggle
Installed modules have an `enabled` column in `module_installs`. Toggle without uninstalling; the Hub API and MCP registration respect `enabled=false` to skip starting the module.

### Module Tauri commands (`commands/modules.rs`)
Six commands cover the module catalog + lifecycle surface: `list_module_catalog()` (full catalog), `install_module_for_project(project_id, module_id, settings)` (drives the installer engine + audits), `uninstall_module_v2(project_id, module_id)` (removes the install row + the on-disk module copy), `list_installed_modules(project_id)` (per-project install state), `module_status_v2(project_id, module_id)` (single-module status: installed/enabled/version), `set_module_enabled_v2(install_id, enabled)` (enable/disable toggle without uninstall). Every mutating call audits.

---

## Settings & Preferences

Settings split cleanly between global (one row per key in `db::settings`) and per-project (one row per `(project, module, key)` in `module_settings`). Secrets never live here — see [Secrets Management](#secrets-management).

### Settings Panel (`SettingsPanel.svelte`)
Global settings: profile, downloads path, about. Backed by `db::settings` table (key/value JSON store).

### Per-project Settings
`module_settings` table stores per-(project, module) non-secret settings as JSON-encoded values. Unique constraint on `(project_id, module_id, setting_key)`.

### Per-project KG Binding
`project_kg_bindings` table maps a project to its primary Weaviate collection name, embedding model, embedding dim, `knowledge/` dir path, and optional Weaviate URL override. Role can be `primary`, `shared`, or `archive`.

### Per-project Code Graph Binding
`project_codegraph_bindings` tracks collection prefix, embedding model, last analyzed git SHA, and an `enabled` toggle. Used by the `/codegraph` UI to show analysis currency.

### Global Settings Store
`stores/settings.ts` provides a reactive Svelte store backed by `invoke('get_setting')` / `invoke('set_setting')`. Changes persist immediately to SQLite.

---

## Secrets Management

Secrets are kept out of the launcher's SQLite database entirely. Storage is the OS keychain; the DB only records *which* secrets a project needs (not their values), and even the secret-preview command returns first-4 + last-4 chars rather than the full value.

### OS Keychain Storage
`secrets.rs` stores all secrets in the OS native keychain (macOS Keychain, Windows Credential Manager, Linux libsecret/GNOME Keyring). Secrets never touch the SQLite DB or any file on disk.

### Secret Scopes
Three scopes with distinct keychain service namespaces: `PerProject { project_id }` → `vct.<project_id>.<module_id>`, `Global` → `vct.global.<module_id>`, `Shared { project_id }` → `vct.<project_id>.shared.<module_id>`.

### Secrets Panel UI
`SecretsPanel.svelte` provides scope toggle (per-project / shared / global), list of known keys with Set / Update / Clear, masked preview for sensitive entries, and an add-new form. Secret values never leave the Rust process memory.

### Secret References (No Values in DB)
`project_secret_refs` table records which secrets a project requires and how to resolve them (`keychain-per-project`, `keychain-shared`, `keychain-global`, `file`, `env`) — never the values. The `is_set` flag is updated by a presence check at runtime.

### Secrets Tab in Project State
`project-state/SecretsTab.svelte` shows the secret refs for the active project, with resolution source and `is_set` status per key. Backed by `list_project_secret_refs` command.

### Secrets / settings Tauri commands (`commands/secrets_cmd.rs`)
Seven commands cover the GUI keychain + module settings surface:
- `set_secret_v2(scope, module_id, key, value)` — write a secret to the OS keychain. The plaintext value never reaches SQLite.
- `clear_secret_v2(scope, module_id, key)` — delete a keychain entry.
- `is_secret_set(scope, module_id, key)` — boolean presence check (no read).
- `get_secret_preview(scope, module_id, key)` — first 4 + last 4 chars (never the full value); used for "currently set" display.
- `get_setting_v2(project_id, module_id, setting_key)` — read a non-secret per-(project, module) setting from `module_settings`.
- `set_setting_v2(project_id, module_id, setting_key, value_json)` — write a non-secret setting (JSON value).
- `list_module_settings_v2(project_id, module_id)` — return all settings for one module.

---

## License & Tier

The launcher's role here is narrow: cache the validated tier so the rest of the UI knows what to show, store the license key in the OS keychain, and audit activation. All actual tier classification happens server-side in the `validate-tier` Supabase edge function.

For the full tier model, see [06-license-and-commercial.md](06-license-and-commercial.md).

### Tier Cache (Offline-Safe, 3-Day Grace)
`tier_cache` table holds exactly one row: `orchestrator_tier`, `module_licenses` (JSON), `last_validated`, `last_error`. On network failure, cached tier remains authoritative for up to 72 hours. Seeded with `free` on first run.

### License Tiers
Five tiers: `free`, `pro`, `mao`, `enterprise`, `admin`. The `admin` tier was added in migration 005 via SQLite table-recreate (SQLite cannot drop CHECK constraints in place).

### license_refresh Command
POSTs `{license_key, machine_id_hash}` to `https://api.vibecodedtools.it/validate-tier` (8s timeout). On 401 drops immediately to `free`; on other errors keeps existing cached tier and records the error.

### Machine ID Binding
`machine_id_hash()` derives SHA-256 of the 6-byte MAC address, matching `VCThelpers/license/validator.py::_machine_id_hash`. Used for server-side machine binding.

### License Activate / Deactivate
`license_activate` writes the key to the OS keychain under `vct.global.licensing.VIBECODED_LICENSE_KEY`, audits the action (key prefix only), then calls `license_refresh`. `license_deactivate` deletes the keychain entry and resets tier to `free`.

### `VCT_VALIDATE_TIER_URL` Override
Operators can set `VCT_VALIDATE_TIER_URL` to point at a staging or dev validate-tier endpoint without modifying source. The hard-coded default in `commands/licensing.rs:21` is `https://api.vibecodedtools.it/validate-tier` (a public alias; the real Supabase project ref is never committed to source).

### Security Audit Tests
`licensing.rs` includes source-level audit tests: (1) default validate URL must not contain `supabase.co`, (2) production source must not contain bypass symbols (`MAINTAINER_TOKEN`, `ed25519_dalek`, etc.). Run as Rust unit tests to prevent accidental regression.

### Admin-only Routes
`/admin/diagnostic`, `/admin/feature-flags`, `/admin/license-issuance-test`. Gated by `tier === 'admin'` in `+layout.svelte`. Client-side check is UX only; each admin tab re-validates server-side.

### Dev Mode Activation Codes
Test codes (`test-transcrypt`, `test-arzillibus`, etc.) bypass the webhook and activate modules directly for development. Entered via Settings → Activation Codes (`ActivationModal.svelte`).

---

## VS Code & Claude Code Surface Integration

The launcher edits `~/.claude.json` and the project's `.vscode/settings.json` and `.claude/settings.json` to register MCP servers and per-project env. All writes are atomic (`write→rename`) and preserve unrelated keys — the launcher is one of several writers to these files, never the only one.

### MCP Registration (`mcp_registration.rs`)
`register_mcp` patches `~/.claude.json` at `mcpServers.<id>` via atomic `write→rename` with an OS pidfile lock (prevents concurrent-session corruption). All other top-level keys are preserved — verified by unit test.

### MCP Deregistration
`deregister_mcp` drops only the named `mcpServers.<id>` key; sibling servers are untouched.

### MCP Dashboard (`/mcp`)
`McpDashboard.svelte` lists all registered MCP servers (built-in + custom), shows enabled/disabled toggle. Built-in servers (`weaviate-kg`, `ollama`, `search`, `code-embed`) cannot be removed — only toggled.

### Custom MCP Server Addition
"+ Add custom MCP server" form writes to three locations: `~/.vct/orchestrator.json` (launcher config), `~/.claude.json` (Claude Code reads this), `.claude/settings.json` env block (orchestrator subprocess). Documented in `launcher/docs/MCP_INTEGRATION.md`.

### Dashboard Tauri commands (`commands/dashboard.rs`)
Nine commands back the orchestrator-config and MCP-management surfaces in the launcher dashboard:
- `get_feature_flags(user_apps)` — derives enabled feature flags from the user's purchased apps + tier.
- `get_orchestrator_config()` / `save_orchestrator_config(config)` — read/write `~/.vct/orchestrator.json` (free/pro orchestrator settings).
- `update_orchestrator_setting(key, value, user_apps)` — patch a single setting and return the updated config.
- `get_mcp_servers()` — list MCP servers known to the launcher (built-in + custom) with enabled state.
- `toggle_mcp_server(mcp_id, enabled, user_apps)` — flip the enabled flag for one server.
- `update_mcp_setting(...)` — change a per-server setting (e.g. backend, model).
- `add_custom_mcp_server(server)` — append a user-defined MCP server to the registry.
- `remove_mcp_server(mcp_id)` — remove a custom MCP server (built-ins refuse).

### `tools/claude` Wrapper
A thin shell wrapper that sources `.claude/env` before exec'ing the real `claude` binary, so CLI users without VS Code get per-project KG routing automatically.

---

## Container Management

A user with several VCT projects shouldn't end up with one Weaviate per project. The launcher probes default ports first and offers to reuse anything already running; only if reuse is declined (or impossible) does it spawn fresh containers. Volume migration goes through a single audited code path with rollback.

### Shared Services Detection
Installer checks Weaviate, Ollama, and code-embed ports before spawning new containers. If all three respond, reuse is offered. → See [05-install-and-secrets.md](05-install-and-secrets.md#shared-service-reuse) for the full detection flow.

### Volume Configuration Commands (`volumes.rs`)
Four Tauri commands govern volume layout: `get_volumes_config` (current bind-mount paths or named-volume mode), `set_volumes_config_for_install` (persist new paths and write the compose override), `set_volumes_config_dry_run` (preview the override without writing), `migrate_volumes` (the only function permitted to call `podman volume rm` / `docker volume rm`).

### Volume Migration Flow (`migrate_volumes`)
`compose down` (no `--volumes`) → `cp -a` each volume → write `docker-compose.override.yml` → `up -d` → wait for health → only then remove old volumes. Any failure triggers rollback.

<details>
<summary>Details</summary>

Migration emits phase-level `volumes://migrate-progress` Tauri events so the UI renders a real progress bar: `StoppingContainers`, `CopyingVolume {volume_role, index, total}`, `WritingOverride`, `StartingContainers`, `WaitingForHealth`, `RemovingLegacyVolumes`, `Done`, `RollingBack {reason}`. This addresses the dead-spinner UX for users with multi-GB Weaviate volumes (5+ min `cp -a`).

</details>

### Zero-Destruction Install Guard
`installer.rs` includes an audit test (`test_no_destructive_subprocess_calls_in_install_path`) that greps install-path files for banned commands (`rm -rf`, `docker volume rm`, `podman volume rm`). Prevents accidental destructive commands from slipping into the install path.

### Service Lifecycle on Launcher Start/Stop
`commands/lifecycle.rs` ties the backing services (Weaviate, Ollama, code-embed) to the launcher's own lifecycle:

- **Auto-start on boot**: `services_start_all()` runs `podman compose up -d` (or docker if no podman) at launcher startup. Failure does not block the launcher window — the Services route surfaces the error.
- **Per-service controls**: `services_start`, `services_stop`, `services_restart` Tauri commands surface to a Services route + tray submenu. Service names are validated against a hardcoded allowlist (`weaviate`, `ollama`, `code_embed`); arbitrary names refuse.
- **Runtime detection** (`services/runtime.rs`): podman-first universal preference, with macOS Podman Machine handling (`podman machine list` to detect a started VM and fall through to Docker if not).
- **Adoption modes** (`services/adoption.rs`): when a foreign service is detected on a default port, `ExternalServicesDialog.svelte` prompts the user with `unresolved | adopt | parallel | refuse` modes. The chosen mode is written to `~/.vct/services.toml` — the same lock file install.py uses, so the launcher and installer never disagree.
- **Frontend events**: `vct-services-lifecycle` (start/stop/restart progress) and `vct-external-services-detected` (adoption prompt trigger). Lets the UI render real-time state without polling.

---

## Hub API

The Hub is the launcher's local HTTP face — used by the headless `vct` CLI, by ecosystem apps (Transcrypt, Arzillibus), and by other VCT processes that need to talk to the launcher without going through Tauri IPC. It runs on `127.0.0.1` only and writes its bound port to `~/.vct/hub.port` so consumers don't have to guess.

### Local HTTP Server (port 7700)
`hub/server.rs` starts an Axum HTTP server on `127.0.0.1:7700` (or `VCT_HUB_PORT`) as a background tokio task at launcher startup. Uses WAL mode for the SQLite connection so it coexists with the Tauri-side DB handle.

### Port Discovery (`~/.vct/hub.port`)
After binding, the hub writes the actual port to `~/.vct/hub.port`. Consumers (CLI, MCP servers, other apps) read this file to discover the hub — especially useful when the default 7700 is taken.

### Port Retry
`try_bind` tries the base port and up to 5 increments before failing. Actual bound port is written to `hub.port` regardless of which offset was chosen.

### Hub API Routes
Four sub-routers nested under `/api/v1`, each in its own file under `launcher/src-tauri/src/hub/`:
- `api.rs` — core operations: health, app catalog (register/deregister/heartbeat), cross-app messaging (send/poll/ack), data catalog (register/query). 10 routes.
- `modules_api.rs` — module catalog + installed list + install + project list + project env. 8 routes.
- `project_state_api.rs` — full per-project agent/skill/hook/permission/secret-ref/KG-binding/codegraph-binding registry. ~16 method+path combos.
- `cli_api.rs` — mirror of Tauri commands for headless `vct` CLI access (project CRUD, audit list, license, hooks toggle, telemetry consent). 10 routes.

For the full route enumeration → see [Hub HTTP API — Routes](#hub-http-api--routes) below.

### CORS Wildcard
Hub server has `CorsLayer` with `allow_origin(Any)` — intentionally permissive for a localhost-only server.

### Hub Proxy Tauri Commands
`commands/hub_proxy.rs` exposes `hub_info`, `hub_list_apps`, `hub_poll_messages`, `hub_data_catalog` for the SvelteKit UI to talk to the same hub API its CLI consumers use, without re-implementing every endpoint as a Tauri command.

### Cross-app message passing (`hub_poll_messages`)
The hub maintains a per-app message queue so other VCT-ecosystem apps (Transcrypt, Arzillibus, future plugins) can drop notifications for the orchestrator. Polled via `hub_poll_messages` from the UI; consumed-or-dropped semantics, not a durable queue.

---

## Data Layer

One SQLite file (`~/.vct/launcher.db`) is the entire persistent state of the launcher. No server process, no network dependency. Schema evolution goes through numbered migrations; an in-memory variant is used for tests so no run touches the user's real database.

### SQLite Database (`~/.vct/launcher.db`)
All persistent launcher state lives in a single SQLite file. No server process, no network dependency.

### Migration System
`db/migrations.rs` runs numbered SQL migrations at startup. Five migrations: `001_initial.sql` (projects, module_installs, tier_cache, audit_log), `002_project_state.sql` (agents/skills/hooks/permissions/secrets/KG/codegraph bindings), `003_project_slug.sql` (slug column), `004_audit_actor.sql` (OS `$USER` actor column), `005_tier_cache_admin.sql` (extends tier CHECK to include `'admin'`).

### In-Memory DB for Tests
`Db::open_in_memory()` provides an in-memory SQLite instance used by unit tests without touching the user's real database.

### KG Collection Access Matrix
`kg_collection_access` table: per-(project, collection) read/write/none access level. Governs which Weaviate collections a project is allowed to query.

### Code Graph Access Matrix
`codegraph_access` table: per-(grantor_project, grantee_project) read/none access. Default is no access (missing row = no access).

---

## KG Dashboard Commands (`commands/kg.rs`)

Ten Tauri commands proxy the user's local Weaviate so the SvelteKit `/kg` view can render an Obsidian-style graph, search nodes, and manage per-project collection access without bundling a Weaviate JS client. Access policy is enforced launcher-side via the `kg_collection_access` table — Weaviate itself stays unauthenticated (localhost-only).

### `kg_list_collections(project_id)`
Lists all Weaviate collections plus this project's access level (`read` / `write` / `none`) and node count. A collection appears if Weaviate has it AND the project has an access row OR it's the declared shared collection.

### `kg_set_collection_access(project_id, collection, access)`
Sets the per-(project, collection) access level. Writes a row to `kg_collection_access` and audits the change.

### `kg_set_collection_access_mode(project_id, collection, mode)`
Switches a collection between `default-allow-with-deny-list` and `default-deny-with-allow-list` per-node access modes (v1.1 fine-grained access).

### `kg_load_graph(project_id, collection, limit)`
Returns nodes + edges for the SigmaGraph viz: title, type, tags, typed_links resolved into edge records.

### `kg_search(project_id, collection, query, limit)`
Hybrid keyword + semantic search proxied to Weaviate, scoped to one collection. Used by the `/kg` search bar.

### `kg_get_node(project_id, collection, file_path)`
Fetches a single KG node's full content + metadata for the side-panel detail view.

### `kg_promote_to_shared(project_id, file_path, target_collection)`
Copies a node from a project-local collection to the shared collection so other projects can read it. Audits the promotion.

### `kg_set_node_access(project_id, collection, file_path, access)`
Per-node access override (v1.1) — finer-grained than collection-level access.

### `kg_set_node_access_bulk(project_id, collection, entries)`
Batch version of `kg_set_node_access` for multi-select operations in the UI.

### `kg_ensure_node_access_schema(collection)`
Ensures the Weaviate collection has the `vct_access_*` properties needed for per-node access. Idempotent; safe to call on existing collections.

---

## Code Graph Commands (`commands/codegraph.rs`)

Six Tauri commands back the `/codegraph` view (graph viz + access matrix between projects).

### `codegraph_list_access(project_id)`
Lists which other projects can read this project's code graph (or none).

### `codegraph_grant_access(grantor_project, grantee_project, access)`
Sets the access level (`read` / `none`) between two projects. Audits the grant.

### `codegraph_check_access(grantor_project, grantee_project)`
Returns the current access level. Used by the search MCP to gate cross-project code-graph queries.

### `codegraph_summary(project_id)`
Returns counts per code-graph collection (modules, classes, functions, APIs, interactions) plus last-analyzed SHA — drives the `/codegraph` summary panel.

### `codegraph_load_graph(project_id, scope, limit)`
Returns nodes + edges for the SigmaGraph viz (functions and their `calls` edges, classes and their `extends` chains).

### `codegraph_set_entity_access_bulk(project_id, entries)`
Batch per-entity access overrides (v1.1) for fine-grained gating of specific functions/classes across project boundaries.

---

## Coordination Commands (`commands/coordination.rs`)

Five Tauri commands manage the Coordination tab in the launcher (Supabase-backed team coordination, Pro feature). The launcher writes config to `~/.vct/orchestrator.json` and tests connectivity; the actual coordination MCP runs separately and is not bundled in OSS.

### `coordination_get_config()`
Reads current Supabase URL + service-role key reference from launcher config.

### `coordination_set_config(config)`
Writes the config and audits the change. The service-role key reference points at a `vct-secrets` entry — the key value never enters the launcher DB.

### `coordination_test_connection(config)`
Runs a smoke probe against Supabase to confirm reachability before saving.

### `coordination_apply_schema()`
Idempotent SQL deploy of the `team_*` tables to the configured Supabase project (used by maintainers; gated to admin tier in the UI).

### `coordination_team_status(project_id)`
Returns online team members + recent activity for the Coordination tab.

---

## Telemetry Dashboard (`commands/telemetry_cmd.rs`)

Four Tauri commands back the `/telemetry` consent + dashboard view, mirroring the Python telemetry primitives in `VCThelpers/telemetry/`.

### `telemetry_status()`
Returns `{enabled, consent_categories, queue_size, pending_uploads, last_upload_at}` for the consent banner.

### `telemetry_set_consent(categories)`
Writes consent flags to `~/.vibecoded/config.json` and updates the local SQLite queue accordingly.

### `telemetry_recent_events(limit)`
Returns the most recent N events from `~/.vibecoded/telemetry.db` for the audit-style table view.

### `telemetry_clear_queue()`
Empties the local queue without uploading. Audits the action.

---

## Installer Commands (`commands/installer.rs`)

Seventeen commands wired in `lib.rs` cover the orchestrator-installer surface used by the install screen and the GitHub-PAT keychain UI.

### Detection
- `detect_system()` — OS, Python version, container runtime, GPU.
- `detect_existing_services()` — probes Weaviate / Ollama / code-embed ports for shared-service reuse.
- `get_default_install_path()` — platform-appropriate default install directory.
- `get_local_repo_source()` — returns the path to the bundled OSS repo source if running in a launcher with embedded source.

### State / version
- `check_install_status(path)` — true/false: is an orchestrator installed at that path?
- `get_installed_version(path)` — reads version from installed `.env` or repo state.
- `check_for_updates(path)` — compares installed vs latest available.
- `inspect_orchestrator_at(path)` — full state probe of an existing install at a given path.

### Install / update flow
- `preview_install(config)` — diff-style preview of what an install would change. Read-only.
- `preflight_install_safety_check(config)` — hard-path-whitelist enforcement; refuses installs that would touch user code outside whitelisted dirs.
- `install_orchestrator(config, window)` — runs the install, emits `installer://progress` events.
- `update_orchestrator(window)` — re-runs install in update mode (preserves `.env`, restarts services).
- `update_orchestrator_at(path, window)` — variant that targets a specific existing install path.

### GitHub PAT (OS keychain)
- `has_github_pat()` — boolean: is a PAT stored in the OS keychain under `vct.global.github`?
- `get_github_pat_preview()` — returns first 4 + last 4 chars (never the full token).
- `register_github_pat(token)` — writes to keychain. Audited (only the prefix).
- `clear_github_pat()` — removes the keychain entry. Audited.

---

## App Lifecycle Commands (`commands/lifecycle.rs`)

Six commands manage external app processes (Transcrypt, Arzillibus, future plugins) registered with the launcher's `AppManager`.

### `launch_app(app_id, command, args, working_dir)`
Spawn an external app as a child process, register it with `AppManager`, and stream stdout/stderr back to the UI via Tauri events.

### `kill_app(app_id)`
Terminate a registered app's process. SIGTERM with SIGKILL fallback.

### `get_app_status(app_id)`
Returns one app's `ServiceEntry` (pid, start time, last heartbeat).

### `get_all_app_statuses()`
Bulk read for the dashboard's running-services widget.

### `check_app_health(app_id, health_url)`
Probes a configured health endpoint. Returns `{ok, status_code, latency_ms}` or an error variant.

### `check_all_health()`
Iterates all registered apps' health URLs in parallel.

---

## Concurrency / Polling Commands (`commands/changes_cmd.rs`)

Two commands back the launcher's optimistic-concurrency primitive (P7 design): when two launcher windows or the CLI mutate state simultaneously, the UI polls a `change_log` table to know it needs to refresh.

### `poll_changes(since_seq)`
Returns all `change_log` rows with `seq > since_seq`. The UI polls this every few seconds; clients only re-fetch the affected entity types. See `launcher/docs/CHANGE_LOG_POLLING.md`.

### `current_change_seq()`
Returns the current high-water sequence number. The UI calls this once at load to seed its baseline.

---

## Hub HTTP API — Routes

The hub HTTP server (`hub/server.rs`, port 7700) nests four sub-routers under `/api/v1`. All routes are localhost-only with permissive CORS (intentional — see [CORS Wildcard](#cors-wildcard)). Routes by source module:

### Core operations (`hub/api.rs`)
- `GET /api/v1/health` — liveness probe; returns `{ok: true, version}`.
- `GET /api/v1/apps` — list registered VCT-ecosystem apps with last-heartbeat timestamps.
- `POST /api/v1/apps/register` — register a new app (Transcrypt, Arzillibus, custom). Body: `{app_id, name, version}`.
- `DELETE /api/v1/apps/{app_id}` — deregister an app.
- `POST /api/v1/apps/{app_id}/heartbeat` — keepalive ping; advances the app's `last_seen_at`.
- `POST /api/v1/messages` — drop a message into another app's inbox. Body: `{recipient, sender, payload}`.
- `GET /api/v1/messages/{recipient}` — poll an app's inbox; messages are consumed-or-dropped, not durable.
- `POST /api/v1/messages/{id}/ack` — acknowledge a message (clears it from the queue).
- `POST /api/v1/data/register` — register a discoverable dataset for the cross-app data catalog. Body includes path + schema hint.
- `GET /api/v1/data/catalog` — query the data catalog for datasets registered by other apps.

### Module API (`hub/modules_api.rs`)
- `GET /api/v1/modules/catalog` — full module catalog (mirror of `list_module_catalog`).
- `GET /api/v1/modules/installed` — installed modules across projects.
- `GET /api/v1/modules/{module_id}/status` — install state + version + enabled flag for one module.
- `POST /api/v1/modules/install` — install a module into a project. Body: `{module_id, project_id, settings}`.
- `GET /api/v1/projects` — list projects (mirror of `list_projects_v2`).
- `GET /api/v1/projects/{project_id}` — single project detail.
- `GET /api/v1/projects/{project_id}/env` — resolved per-project env (KG_COLLECTION, etc.) for shells / CI.
- `GET /api/v1/projects/by-slug/{slug}` — project lookup by URL slug.

### Per-project state (`hub/project_state_api.rs`)
- `GET /api/v1/projects/{project_id}/state` — full snapshot (agents/skills/hooks/permissions/secrets/bindings).
- `GET, POST /api/v1/projects/{project_id}/agents` — list / register agents.
- `PATCH, DELETE /api/v1/projects/{project_id}/agents/{agent_name}` — toggle / unregister.
- `GET, POST /api/v1/projects/{project_id}/skills` — list / register skills.
- `PATCH, DELETE /api/v1/projects/{project_id}/skills/{skill_name}` — toggle / unregister.
- `GET, POST /api/v1/projects/{project_id}/hooks` — list / register hooks.
- `PATCH, DELETE /api/v1/projects/{project_id}/hooks/{hook_id}` — toggle / unregister.
- `GET, POST /api/v1/projects/{project_id}/permissions` — list / add permission entries.
- `DELETE /api/v1/projects/{project_id}/permissions/{perm_id}` — remove a permission.
- `GET, POST /api/v1/projects/{project_id}/secrets` — list secret refs / set a secret ref.
- `DELETE /api/v1/projects/{project_id}/secrets/{secret_key}` — clear a secret ref.
- `POST /api/v1/projects/{project_id}/kg-binding` — set the project's primary KG binding.
- `POST /api/v1/projects/{project_id}/codegraph-binding` — set the code graph binding.

### CLI-facing endpoints (`hub/cli_api.rs`)
Mirror of Tauri commands so the headless `vct` CLI can drive the launcher without IPC into the Tauri app. All actions audit with `via: "cli"` tagged in the detail JSON.
- `POST /api/v1/cli/projects` — create a project.
- `PATCH, DELETE /api/v1/cli/projects/{id_or_slug}` — rename / delete.
- `GET /api/v1/cli/audit` — read audit log (mirrors `list_audit_events`).
- `GET /api/v1/cli/license` — current tier + cache status.
- `POST /api/v1/cli/license/activate` — activate a license key (writes to keychain).
- `POST /api/v1/cli/license/deactivate` — clear license.
- `GET /api/v1/cli/hooks/{project_id}` — list hooks for a project.
- `PATCH /api/v1/cli/hooks/{hook_id}/enabled` — toggle hook enabled.
- `GET /api/v1/cli/telemetry` — telemetry status.
- `POST /api/v1/cli/telemetry/consent` — set telemetry consent.

---

## Audit Log

Every mutating Tauri command audits. The log is plain SQLite — append-only, no automatic pruning, no cryptographic signing — so it's straightforward to inspect with `sqlite3` and good enough for SOC2 / NDA review, but it isn't legal-grade non-repudiation.

### Mutation Logging
Every state-changing Tauri command calls `db.audit(operation, project_id, module_id, detail_json)`. Covers: project CRUD, module install/uninstall/enable/disable, secret set/clear, license activate/deactivate, MCP register/deregister, telemetry consent, per-project agent/skill/hook mutations.

### Audit Actor Column
`actor TEXT NOT NULL` records OS `$USER` for each event. Legacy rows before migration 004 carry `"system"` as the actor.

### Audit Log UI (`/audit`)
`/audit` route renders the `audit_log` table with server-side filtering: project, actor, time range (epoch ms), free-text substring on `operation` or `detail`. Capped at 10,000 rows server-side.

<details>
<summary>Details</summary>

Secret values are never written to `detail`. Only key names are recorded (e.g. `{"key_name": "OPENAI_API_KEY"}`). The log is append-only plain SQLite with no automatic pruning and no cryptographic signing. Suitable for SOC2 / NDA use cases; not for legal-grade non-repudiation. Direct access: `sqlite3 ~/.vct/launcher.db "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 50"`.

</details>

### CLI Audit Mirror
Hub `cli_api.rs` mirrors audit operations with `via: "cli"` tagged in detail JSON, so CLI-initiated actions appear in the same audit trail as GUI-initiated ones.

### Audit Log Read API (`list_audit_events`)
`commands/audit.rs` exposes `list_audit_events(filter)` for the `/audit` route — server-side filter pushdown by project, actor, time range, and free-text substring. Cap of 10,000 rows per call.

### UI-pattern Audit Tooling
`launcher/scripts/audit-ui-patterns.sh` runs an internal lint over the SvelteKit components (DialogRoot usage, Term.svelte coverage, button accessibility patterns). Maintainer-facing only; not called from runtime.

---

## Updates

### Update Badge
`UpdateBadge.svelte` signals when a new launcher version is available. Check triggered from the system tray "Check for updates" menu item.

### Changelog Modal
`ChangelogModal.svelte` presents release notes when an update is available or on manual "What's new" trigger.

### Updater Store
`stores/updater.ts` manages update state (available version, download progress, install). Backed by Tauri's built-in updater plugin.

### Self-Update via Git Pull
`commands/self_update.rs` provides an in-app self-update path independent of the Tauri updater plugin. Tray surfaces an "Update available" state when remote `HEAD` has new commits. Manual "Update now" runs `git pull --ff-only` (conflict-aware), rebuilds (Cargo OR npm based on which directories changed), and restarts the launcher.

- **Daily check cadence**: 24-hour timer with state persisted in `~/.vct/launcher-update-state.json` (last_check, last_remote_sha).
- **User-owned-paths protection**: a hard-coded never-overwrite manifest covers `.claude/CONTEXT_STATE.md`, `.claude/context/**`, `state/**`, etc. The pull validates these paths are untouched in the incoming commit; if they would change, the update fails closed with a clear message.
- **Rebuild gating**: diff between current and target SHA is inspected. If only `launcher/src-tauri/**` changed → cargo build. If only `launcher/src/**` changed → npm build. Both → both. Saves 2-5 minutes when the change is frontend-only.
- **Restart sequence**: writes `force_quit=true` to a transient state file, exec's the new binary, parent exits. The Quit confirmation dialog reads `force_quit` and skips the prompt.

UI surface: `/preferences/updates` route with a manual check button + last-check timestamp + pending-pull preview.

### Re-Run Onboarding from Preferences
`/preferences` includes a "Re-run onboarding" button that clears the `vct.onboarding_complete` localStorage flag and reloads. The 4-step wizard fires again; existing projects, settings, and secrets are unaffected. Useful after a hardware change (GPU added, RAM upgraded) when the user wants to re-run the infrastructure-detection step.

---

## URL Routing

Each project has a stable URL slug; `/p/<slug>` is the entry point that remembers which sub-section the user last visited (KG, code graph, modules, etc.). Slug collisions get a numeric suffix; renamed projects don't redirect from old slugs (404 with a friendly message instead of silent rewriting).

### Per-project Slug URLs (`/p/<slug>`)
Every project has a stable URL slug generated at creation from its name (lowercase ASCII alphanumeric + dashes, collision-resolved with numeric suffix). `/p/<slug>` redirects to the last-visited section for that project. See `launcher/docs/MULTI_TENANT_URLS.md`.

### Last Section Memory
Per-project last-visited section tracked in `localStorage` as `vct.last_section.<id>`. Whitelisted sections: `/kg`, `/codegraph`, `/coordination`, `/audit`, `/project`, `/hub`, `/mcp`, `/telemetry`.

### Route Structure
Top-level SvelteKit routes: `/` (home), `/auth`, `/project`, `/project/[id]`, `/modules`, `/kg`, `/codegraph`, `/coordination`, `/hub`, `/mcp`, `/audit`, `/telemetry`, `/preferences`, `/glossary`, `/store`, `/admin/*`.

---

## UX Patterns

### DialogRoot — Native `<dialog>` Modal
All modals use `DialogRoot.svelte`, a wrapper around the native HTML `<dialog>` element opened via `showModal()`. Renders in the browser top layer (above Tauri's GTK title bar), with native `::backdrop`, native Escape-to-close, native focus trapping, and native accessibility tree treatment.

### Toast Notifications
`Toast.svelte` + `stores/toast.ts` — lightweight toast system (success, error, info). Used across the UI for async operation feedback.

### Glossary Tooltip (`Term.svelte`)
Wrap any jargon in `<Term key="...">` to get a hover tooltip with a short ELI5 definition from `lib/glossary.ts`. Links to `/glossary` for full detail.

### Sigma.js Knowledge Graph Visualization
`SigmaGraph.svelte` is a generic Obsidian-style force graph component used by both `/kg` and `/codegraph` views. Accepts normalized `VizNode[]` / `VizEdge[]` shapes. Supports click, context-menu, shift-click multi-select, and per-type color palettes.

### BrowserModeBanner
`BrowserModeBanner.svelte` detects non-Tauri browser context (dev mode via `npm run dev`) and renders a notice that Tauri APIs are unavailable.

---

## Project State Dashboard

### Per-project State Matrix
`/project/[id]` shows a tabbed view of the active project's Claude Code configuration, backed by the `project_*` tables from migration 002.

### Agents / Skills / Hooks / Permissions Tabs
`AgentsTab.svelte`, `SkillsTab.svelte`, `HooksTab.svelte`, `PermissionsTab.svelte` — each shows the respective project configuration with enable/disable toggles. Hooks tab includes event, matcher, command, and enabled state.

### KG / Code Graph Tab
`KgCodegraphTab.svelte` shows KG collection binding and code graph binding, including last-analyzed commit SHA and timestamp.

### Project State Snapshot
`get_project_state_snapshot` returns a complete snapshot of all project state tables in one call for the dashboard initial load.

### Per-project Registry Commands (`commands/project_state_cmd.rs`)
Twenty-one Tauri commands mutate or read the per-project Claude Code registry. Each maps to a row in one of the migration-002 tables. Every mutating command writes an audit_log row.

#### Listing commands (read-only)
- `list_project_agents(project_id)` — all agents registered for a project with `enabled` flag.
- `list_project_skills(project_id)` — all skills registered for a project.
- `list_project_hooks(project_id)` — all hooks (event, matcher, command, enabled).
- `list_project_permissions(project_id)` — entries from `permissions.allow` / `permissions.ask` / `permissions.deny`.
- `list_project_secret_refs(project_id)` — secret references with `is_set` presence flag.
- `get_project_state_snapshot(project_id)` — single-call full snapshot of all six tables for the dashboard initial load.

#### Agent / skill / hook mutations
- `register_project_agent(project_id, name, source)` / `set_project_agent_enabled(agent_id, enabled)` / `unregister_project_agent(agent_id)`.
- `register_project_skill(project_id, name, source)` / `set_project_skill_enabled(skill_id, enabled)` / `unregister_project_skill(skill_id)`.
- `register_project_hook(project_id, event, matcher, command)` / `set_project_hook_enabled(hook_id, enabled)` / `unregister_project_hook(hook_id)`.

#### Permissions / secrets / bindings
- `add_project_permission(project_id, kind, pattern)` / `delete_project_permission(perm_id)`.
- `set_project_secret_ref(project_id, key_name, resolution)` / `delete_project_secret_ref(ref_id)`.
- `set_project_kg_binding(project_id, collection, embedding_model, ...)`.
- `set_project_codegraph_binding(project_id, collection_prefix, embedding_model, last_sha, enabled)`.

### change_log Polling for Concurrency Invalidation
`commands/changes_cmd.rs` exposes `poll_changes(since_seq)` and `current_change_seq()`. The launcher writes a row to a `change_log` table whenever any project, module, or registry entry is mutated; the UI polls for changes every few seconds so two concurrent launcher windows (or the CLI mutating in parallel) stay in sync without page reload. Lightweight optimistic-concurrency primitive. See `launcher/docs/CHANGE_LOG_POLLING.md` (P7 design).

---

## System Tray

### Tray Menu
`tray.rs` builds a tray icon with: Open Launcher, Running services count (live label), Recent Projects sub-menu (top 5 by `updated_at`), Check for updates, About, Quit.

### Tray Click to Focus
Left-clicking the tray icon shows and focuses the main window (`w.show()` + `w.set_focus()`).

### Live Service Status Pill
The "Running services" line is polled every 5s and updated in-place (`MenuItem::set_text` rather than full menu rebuild — avoids flicker and OS-level menu re-grab). States: `Services: N/M running`, `No services running`, `managed externally`. The probe reads the same port set as install.py (8081 / 11435 / 11440); foreign services on the same ports are counted as "running" for the headline count, with the externally-managed case surfaced when a foreign service is detected with no `~/.vct/services.toml` lock entry.

### 3-Button Quit Confirmation
Both tray Quit and window-close (`WindowEvent::CloseRequested`) prompt with three options:

- **Quit and stop services** — full shutdown cascade
- **Reduce to tray** — minimize-to-background convenience (services keep running)
- **Cancel** — keep window open

Backed by `quit_dialog.rs` and `tauri-plugin-dialog` (native dialog on each OS). The self-update path bypasses the prompt via a `force_quit` flag set before the relaunch sequence — prevents double-prompting during update apply.

### Bundled Tauri Plugins
`tauri_plugin_opener` (open URLs and file paths in the OS default app) and `tauri_plugin_dialog` (native open/save dialogs) are bundled. Registered in `lib.rs` `Builder::default().plugin(...)`.

---

## Headless CLI (`vct`)

### Build & Install
`tools/vct-cli/install.sh` runs `cargo build --release` and copies the binary to `~/.local/bin/vct`. Built independently from the Tauri app.

### Hub Port Discovery Order
CLI resolves the hub port: `--port <N>` flag → `VCT_HUB_PORT` env → `~/.vct/hub.port` file → 7700 default.

### JSON Output
Every `vct` command outputs JSON for machine consumption. Pipe through `jq` for human-readable formatting.

### `vct project` Commands
`list`, `show <id_or_slug>`, `create --name <name> --path <dir> [--host base|mao]`, `rename <id_or_slug> <new_name>`, `delete <id_or_slug>`.

### `vct module` Commands
`list` (full catalog), `installed <project_id_or_slug>`.

### `vct audit list`
`--project <id|slug>`, `--since <epoch_ms>`, `--limit <N>`. Suitable for CI audit-pull jobs.

### `vct license` Commands
`status` (reads tier cache), `activate <key>` (persists to keychain + audits), `deactivate`.

### `vct hooks` Commands
`list <project_id_or_slug>`, `enable <hook_id> [--project]`, `disable <hook_id> [--project]`.

### `vct hub` Commands
`health` (ping hub), `url` (print hub URL).

### CLI Limitations
Module install/uninstall requires the GUI (installer engine uses Tauri app handle). KG/codegraph search not yet exposed via CLI (requires Weaviate connectivity). License activation persists the key but remote re-validation happens on next GUI refresh.

---

## Auth & Supabase

### Supabase Auth Integration
`lib/supabase.ts` initializes the Supabase client for email/password auth. Session persists across restarts. Auth store in `stores/auth.ts`.

### Auth Guard
`routes/+layout.ts` / `routes/+layout.svelte` guards the app behind authentication, redirecting unauthenticated users to `/auth`.

### Profiles Table + RLS
`20260418_profiles_schema.sql` creates the `profiles` table with RLS policies. Auto-create profile trigger fires on signup. → See [06-license-and-commercial.md](06-license-and-commercial.md#supabase-schema--rls) for RLS details.

### Store Route (`/store`)
`routes/store/+page.svelte` shows purchasable modules/apps. "Get" button opens the Lemon Squeezy checkout with the user's email pre-filled.

---

## Build & Distribution

### Cross-platform Bundles
`npm run tauri build` produces: `.msi` + `.exe` (Windows NSIS), `.deb` + `.AppImage` (Linux), `.dmg` + `.app` (macOS). Output in `src-tauri/target/release/bundle/`.

### Dev Mode (`npm run tauri dev`)
Single command starts the Vite dev server (port 1420, hot reload) + Rust/Tauri native window. First compile ~2-5 min; subsequent runs are fast.

### Frontend-only Dev (`npm run dev`)
Opens SvelteKit in the browser at `http://localhost:1420`. Tauri APIs unavailable; `BrowserModeBanner` renders a notice.
