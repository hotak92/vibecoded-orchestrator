---
title: Module-Contributed GUI Tabs Framework
type: concept
tags: [mid-level-architecture, VCT-Launcher, manifest, gui, svelte, paid-modules, extensibility, schema-rendered-ui, declarative-dispatcher, implemented]
created: 2026-05-19T20:00:00Z
updated: 2026-05-22T18:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Module-Contributed GUI Tabs Framework

Landed in v0.2.20 (Stream 2). Lets installed modules (paid OR free) declare a configuration tab in their `vct-module.json` that the launcher renders into the sidebar without the module needing to bundle Svelte components or rebuild the launcher binary.

**Refresher 2026-05-22**: this node was originally written at v0.2.20 ship. v0.2.23 F2 and v0.2.25 A0bis added new wrinkles (the orchestrator-root manifest now uses the same framework via the per-project Settings embedding pattern), so the "How to wire a NEW module" section near the bottom is the load-bearing practical recipe for paid-module authors going forward.

## Why schema-rendered (not webview-bundled)

Three options were considered:

1. **Schema + launcher renders** (CHOSEN): module declares a `gui.config_tab` JSON block; launcher renders from a fixed widget palette using its own Svelte components.
2. **Tauri webview-bundle plugin**: module ships its own Svelte/HTML bundle, launcher loads it into an isolated webview frame.
3. **External executable + IPC**: module ships a separate small Tauri app; launcher embeds it via iframe.

(1) won because:
- **Zero extra build per module** — module authors just write JSON.
- **Type-safe through manifest schema** — Rust validates the structure at load time.
- **Mouseover tooltips built-in** — `tooltip: Option<String>` is part of every control variant, so authors can't forget to provide them.
- **Tauri security model fit** — no dynamic JS load, no CSP relaxation.

Cons: limited to the widget palette the launcher provides. Acceptable for v1 (5 kinds cover all RL controls; new kinds added as future modules need them).

## Schema (Rust side, `launcher/src-tauri/src/manifest.rs`)

```rust
pub struct GuiBlock {
    pub config_tab: Option<ConfigTab>,
}

pub struct ConfigTab {
    pub title: String,
    pub icon: Option<String>,        // lucide name
    pub route: Option<String>,        // default /modules/<id>/config; must start with /
    pub description: Option<String>,
    pub sections: Vec<ConfigSection>,
}

pub struct ConfigSection {
    pub title: String,
    pub description: Option<String>,
    pub collapsible: bool,
    pub initially_collapsed: bool,
    pub controls: Vec<ConfigControl>,
}

#[serde(tag = "kind")]
pub enum ConfigControl {
    Checkbox    { id, label, tooltip, default, on_change },
    MultiSelect { id, label, tooltip, options_source, on_change },
    Button      { id, label, tooltip, action, variant, confirm },
    Select      { id, label, tooltip, options, default, on_change },
    Info        { id, text, variant },
}
```

Every interactive variant carries `tooltip: Option<String>`. Hover help is the first-class affordance; module authors who provide nothing get a `?` chip with no tooltip text (visible-but-empty), which is itself a UX nudge.

## Renderer (`launcher/src/lib/components/ModuleConfigTab.svelte`)

Single Svelte 5 component, 676 LOC. Dispatches on `control.kind`:

- **checkbox**: `<input type=checkbox>` bound to `$state`. On change, calls generic `set_module_setting` Tauri command, THEN calls `control.on_change` Tauri command if set. Persist-before-side-effect ordering matters: the side-effect handler can read fresh state from the DB.
- **multi_select**: on mount, calls `control.options_source` (Tauri command returning `Vec<SelectOption>`). Renders checkboxes. Bonus convention: selected values are passed as `projectIds` arg to button actions in the same section — saves the manifest from needing explicit "bind multi_select X to button Y" annotations.
- **button**: optional confirm dialog (reuses `DialogRoot.svelte`); on confirm, invokes `control.action`. Variants: `primary` / `secondary` / `danger`.
- **select**: single-pick dropdown, static options inline.
- **info**: read-only banner. Variants: `info` / `warning`. Used for status text + tips.

State persistence is automatic via `get_module_setting` / `set_module_setting` Tauri commands (`launcher/src-tauri/src/commands/module_gui.rs`). Storage: `module_settings` table keyed on `(module_id, control_id, project_id)`, value as JSON blob. No new migration — the table is KV-shaped.

## Sidebar merge (`launcher/src/lib/components/Sidebar.svelte`)

`onMount` calls `get_module_nav_items` Tauri command. New `Module configuration` group is merged into the `$derived<NavGroup[]>` between `System` and `Admin`. Hidden when empty (zero modules expose a `config_tab`).

The Tauri command iterates installed modules' manifests, soft-fails per-module so one broken manifest doesn't break the list. Sorted by module id for stable order.

## Persistence shape

```sql
-- module_settings: (project_id, module_id, control_id) → JSON value
-- Already KV-shaped; no migration needed.
```

Per-project values are the norm (RL flags like `rl_use_global` are per-project). Module-global values would require either (a) a NULL project_id (FK forbids today) or (b) a separate `module_global_settings` table — out of scope for v0.2.20.

## Tradeoffs

- **Five control kinds is intentionally narrow**. Adding a new kind requires extending both the Rust enum AND the Svelte dispatch. This friction is wanted: each new kind is a launcher-API contract that ships forever.
- **No dynamic schema updates** — the manifest is read at launcher startup. Module authors who change `config_tab` must bump module version + reinstall.
- **No client-side validation beyond Pydantic-level types** — `on_change` Tauri commands carry the validation burden.

## First consumers (v0.2.20)

1. **vct-rl-reranker** (paid module) — 3 sections + 10 controls covering all 5 kinds. See `paid-modules/vct-rl-reranker/vct-module.json`.
2. **orchestrator-core** (always installed) — root `vct-module.json` gets `gui.config_tab` for KG + code-graph controls. Validates the schema generalizes beyond paid modules.

## Post-v0.2.20 evolution

### v0.2.23 F2 — `show_in_sidebar: false` for embedded tabs

The orchestrator-core's manifest gained `show_in_sidebar: false` in v0.2.23. Lets a module's `config_tab` be DISCOVERED by `get_module_nav_items` (so embedded surfaces like the per-project Settings page can render it) WITHOUT a duplicating sidebar nav entry.

```rust
pub struct ConfigTab {
    // ... title / icon / sections etc.

    /// When false, the Sidebar's "Module configuration" group suppresses
    /// the nav entry. Default `true` (backwards compat for paid modules
    /// whose only surface is `/modules/<id>/config`).
    #[serde(default = "default_show_in_sidebar")]
    pub show_in_sidebar: bool,
}
```

The orchestrator-core uses `show_in_sidebar: false` because its config_tab is folded into per-project Settings (`OrchestratorCoreTab.svelte` mounts `ModuleConfigTab.svelte` for the orchestrator-root project). Paid modules whose ONLY surface is the sidebar nav entry keep the default `true`.

### v0.2.25 A0bis — orchestrator-core scope refactor

The orchestrator-core was renamed "Orchestrator core" → "Clone integrity" in v0.2.25 + slimmed from 6 buttons / 3 sections to 2 buttons / 2 sections. Per-project actions (Rebuild KG / Check duplicates / Re-analyze code / Prune stale codegraph) were removed because they duplicated controls already on the KG/Codegraph tab. Diagnostics deferred to a Services-tab follow-up.

The framework itself didn't change — the orchestrator-core's manifest just shrank to the 2 genuinely root-clone-only features (Re-detect orchestrator root + Validate clone manifest). Validates that a module can ship a thin config_tab (2 buttons) just as easily as a thick one (10 controls).

### v0.2.26 evolution — declarative dispatcher + 5 new controls (2026-05-22)

v0.2.26 is the framework's largest schema bump since v0.2.20 ship. Two structural changes + five new control kinds. The driving motivation: **adding a new paid module (vct-coordination, vct-transcrypt, future) should no longer require a launcher rebuild**. Pre-v0.2.26 every `action` / `on_change` / `options_source` field was a string naming a Tauri command that had to be registered in the launcher's `invoke_handler!` — adding a module's command required a signed launcher release. After v0.2.26, modules express their actions as declarative JSON the launcher executes via one generic dispatcher.

#### Five new ControlKind variants

All five carry the standard `id` / `label` / `tooltip` fields and dispatch through `ModuleConfigTab.svelte`'s render arm. Persistence (where applicable) routes through the same `set_module_setting` Tauri command introduced in v0.2.20, so storage is unchanged.

- **`text_input`** — free-text string with an Apply button. JSON shape: `{ kind: "text_input", id, label, default?, placeholder?, apply_action? }`. On apply, `apply_action` fires; container's response shape is `{ valid: bool, message?: string }`. Persistence writes via `set_module_setting` AFTER validation succeeds (or always when `apply_action` is None). Client-side regex validation deliberately deferred — server-side only in v1.

- **`number_input`** — numeric input. JSON value type is `number`, NOT string. Shape: `{ kind: "number_input", id, label, default?, min?, max?, step?, on_change? }`. `step` controls granularity; `min`/`max` clamp the allowed range. `on_change` fires on every change (no Apply button — different UX intent than text_input, where mid-typing dispatches would be wasteful).

- **`status_display`** — polled read-only status. Shape: `{ kind: "status_display", id, label, source, render_template }`. The renderer fetches `source` on mount, then re-fetches on the interval declared in the source's `polling` block (or once if no polling). `render_template` is a free-form string with `{{field}}` placeholders substituted from the response object's top-level keys (dotted paths NOT supported in v1; nested fields require flattening server-side). Replaces the v0.2.20 `info` variant's static text with a live container-driven view.

- **`file_picker`** — native file/directory picker. Shape: `{ kind: "file_picker", id, label, extensions?, directory?, on_change? }`. On selection, the absolute path is persisted via `set_module_setting`. Tauri's `@tauri-apps/plugin-dialog` provides the cross-OS native dialog (no per-OS rendering code in the launcher). `extensions` is the allowed-extensions list without leading dots; ignored when `directory: true`.

- **`link`** — clickable link. Shape: `{ kind: "link", id, label, href, target? }`. `target: "external"` (default) opens in the system browser via `tauri_plugin_opener::open_url`; `target: "internal"` calls SvelteKit's `goto(href)` to navigate inside the launcher. No persistence, no on-click side effect beyond navigation.

#### ActionRef — untagged-enum back-compat bridge

The `action` / `on_change` / `options_source` / `apply_action` / `source` fields all changed type from `String` to `ActionRef`:

```rust
#[serde(untagged)]
pub enum ActionRef {
    Legacy(String),                   // v0.2.20-v0.2.25 manifests
    Descriptor(ActionDescriptor),     // v0.2.26 declarative path
}
```

`#[serde(untagged)]` means JSON-side a string deserializes as `Legacy("cmd_name")` and a JSON object deserializes as `Descriptor(...)`. **Every v0.2.20-v0.2.25 manifest works unchanged.** The renderer's `dispatchAction()` helper (`launcher/src/lib/module-dispatch.ts`) checks which variant it's holding and routes accordingly:
- Legacy → `invoke(action, { moduleId, projectId, value })` (same shape as v0.2.20).
- Descriptor → `invoke('module_dispatch_action', { moduleId, projectId, action, value })`.

Why this matters: **paid modules can now add new GUI controls WITHOUT shipping their own Tauri commands.** The launcher binary doesn't need a rebuild — module authors write JSON descriptors that the generic dispatcher executes against the module's containerised HTTP surface.

#### ActionDescriptor::Http + PollingSpec

The v1 descriptor ships exactly one kind — `http`. `shell` was deliberately deferred (the trust surface needs its own design pass). Schema:

```rust
#[serde(tag = "kind")]
pub enum ActionDescriptor {
    #[serde(rename = "http")]
    Http {
        method: HttpMethod,                 // GET / POST / PUT / DELETE
        path: String,                       // container-relative, e.g. "/finetune"
        body: Option<serde_json::Value>,    // template-substituted at dispatch
        polling: Option<PollingSpec>,       // kick → poll → terminal
        next_action: Option<Box<Self>>,     // chain on success
    },
}
```

The optional `polling` block converts a fire-and-forget HTTP call into a long-running pollable job. The dispatcher:
1. Issues the kick request (parent descriptor's method + body).
2. Reads the `job_id` from the kick response via `job_id_path` (default `$.job_id`).
3. Spawns a background poller that re-hits `polling.endpoint` every `interval_seconds` (default 5), passing the job id back as a query param (`job_id` by default).
4. Emits `polling.progress_event` (default `module://action-progress`) on each tick.
5. Stops when `polling.terminal_state_field` (default `$.state`) matches a value in `polling.terminal_success_values` (default `["done"]`) or `terminal_failure_values` (default `["failed", "error"]`), OR `max_attempts` (default 60) is exceeded.

Canonical example — kick + polling + chained next_action (taken from the v0.2.26 manifest fixture in `manifest.rs`'s test suite, which `paid-modules/vct-rl-reranker/vct-module.json` will mirror in Phase 2):

```json
{
  "kind": "button",
  "id": "reset",
  "label": "Reset",
  "action": {
    "kind": "http", "method": "POST", "path": "/reset",
    "body": {"strategy": "fork"},
    "next_action": {
      "kind": "http", "method": "POST", "path": "/specialize",
      "body": {"days": 30},
      "polling": {
        "endpoint": "/finetune_status",
        "interval_seconds": 5,
        "max_attempts": 60
      }
    }
  }
}
```

The chain is purely lexical (nested JSON), so cycles are structurally impossible. The dispatcher executes via an iterative loop guarded by `max_chain_steps` (default 1024) to bound runaway depth.

#### Generic dispatcher — `module_dispatch_action`

One Tauri command serves every paid module forever:

```rust
#[tauri::command]
async fn module_dispatch_action(
    module_id: String,
    project_id: String,
    action: ActionDescriptor,
    value: Option<serde_json::Value>,
) -> Result<serde_json::Value, String>;
```

Inside, it:
1. Resolves the module's HTTP port via `db.get_module_port(project_id, module_id)` (NEW generic table — see [[buildsOn::Generic Per-Module DB Architecture]]).
2. Substitutes `{{...}}` placeholders in the body (see grammar below).
3. Issues the HTTP request to `http://127.0.0.1:<port><action.path>` with the rendered body.
4. If `polling` is set, spawns a background poller and emits progress events.
5. If `next_action` is set on success, recurses with the response value piped into `{{value}}` for the next descriptor.

This is the surface for adding **any** future paid module: declare the manifest, point at the container's HTTP endpoints, ship. No launcher code.

#### Template-substitution grammar (closed-set)

The dispatcher substitutes a **closed set** of `{{...}}` placeholders into the descriptor's `body` JSON before issuing the request. The four allowed forms:

| Placeholder | Resolves to |
|---|---|
| `{{project_id}}` | The current project id (UUID string). |
| `{{module_id}}` | The module id of the module owning this descriptor. |
| `{{value}}` | The control's current value (numeric for `number_input`, string for `text_input`, etc.). Also the result of the previous `next_action` step when chained. |
| `{{control:<id>}}` | The current value of a sibling control with `id: "<id>"` in the same `config_tab`. Cross-section reads ARE allowed. |

**Why a closed set** — security boundary. Modules cannot smuggle arbitrary launcher state into request bodies. Adding new placeholders requires a launcher release (deliberate friction; each new variable is a forever-shipped contract).

How `{{control:<id>}}` resolves at dispatch time (v0.2.26 implementation): the renderer snapshots its current per-control state map (id → JSON value) and sends it as `siblingValues` alongside every descriptor dispatch. The dispatcher's resolver closure reads the snapshot first; for any id not in the snapshot (cross-tab reference, future scheduler caller with no renderer context), it falls back to a `module_settings` DB read. Whole-string `"{{control:<id>}}"` substitution preserves the value's JSON type (an array stays an array, a number stays a number); embedded form stringifies via `serde_json::to_string`.

Renderer-side, `StatusDisplayControl` uses a sibling helper `renderTemplate()` for its `render_template` field — same `{{field}}` syntax, different resolution domain (response JSON's top-level keys instead of dispatch context). **Flat keys only** — `{{response.user.name}}` is NOT supported in v1; dotted paths render as empty. This is symmetric with the dispatcher's `jsonpath_top_level` which restricts `polling.terminal_state_field` / `polling.job_id_path` to `$.<top-level-key>`. Both paths can be extended in a later release; for now, container authors flatten their response shape (e.g. emit `{"username": "alice"}` rather than `{"user": {"name": "alice"}}`).

Missing fields render as `""` (empty), never `{{undefined}}`. Templates are inserted as text nodes — no XSS via response payloads.

**Event-name discipline for polling actions.** The dispatcher emits `progress_event` / `failed_event` payloads as the raw poll-response body (no automatic `module_id` / `control_id` envelope in v0.2.26). If two `status_display` controls in the same module ever shared an event name they would both update on every tick. Recommended convention: namespace event names by `<module-id>://<control-id>-progress` (e.g. `"vct-rl-reranker://specialize-progress"`) so distinct controls have distinct streams. A future release may wrap the payload server-side with `{module_id, project_id, control_id, response}` and grandfather the flat form via a compatibility flag — until then, unique event names per control are the only collision boundary.

#### What still requires a launcher rebuild

The dispatcher's "no rebuild" property has clear boundaries. A launcher rebuild + signed release is still required for any of:

- **A new `ConfigControl` variant** (e.g. `date_range_picker`, `code_editor`). The Rust enum + Svelte dispatch arm + manifest schema all need to change in lockstep.
- **A new `ActionDescriptor` variant** (e.g. `shell` for sandboxed subprocess actions, `event` for pub-sub-only no-HTTP modules). The dispatcher's match arms need a new branch.
- **A new template-substitution variable** (e.g. `{{user_email}}`, `{{license_tier}}`). Closed-set by design; expanding the set is a contract decision.
- **A new `HttpMethod`** (PATCH, HEAD). The enum + dispatcher arm need extending.

Module authors who hit one of these submit a launcher PR proposing the extension. The friction is wanted: each addition is a forever-shipped API surface.

#### Practical-recipe update — declarative-action examples

The "How to wire a NEW paid module's config tab" section below remains the load-bearing tutorial. For v0.2.26, two patterns module authors should reach for FIRST before adding any Tauri command:

**Pattern A — Button with HTTP descriptor (no Tauri command needed):**

```json
{
  "kind": "button",
  "id": "rebuild_index",
  "label": "Rebuild search index",
  "tooltip": "Re-index every project file. Takes 1-5 min.",
  "variant": "secondary",
  "confirm": "Re-index all files? Existing search results will be invalidated.",
  "action": {
    "kind": "http", "method": "POST", "path": "/admin/reindex",
    "body": {"project": "{{project_id}}", "deep": true}
  }
}
```

The dispatcher resolves the module's port, POSTs to `/admin/reindex` with the rendered body. No launcher rebuild.

**Pattern B — StatusDisplay with polling (live dashboard tile):**

```json
{
  "kind": "status_display",
  "id": "queue_depth",
  "label": "Queue depth",
  "source": {
    "kind": "http", "method": "GET", "path": "/metrics",
    "polling": {"endpoint": "/metrics", "interval_seconds": 10}
  },
  "render_template": "{{pending}} pending — last batch took {{last_batch_ms}}ms"
}
```

Mounts → fetches `/metrics` → re-fetches every 10s → renders the template with response fields. Zero Rust code.

Module authors who genuinely need launcher-side state (e.g. a control that triggers a `cargo` invocation, or that reads a non-HTTP launcher signal) keep using `ActionRef::Legacy("my_tauri_command")` — the legacy path is preserved, not deprecated. Use it for the cases the declarative path cannot express.

### v0.2.27 evolution — `events_paths_for` token + `log_path_template` manifest field (2026-05-22)

v0.2.27 adds one new template token + one new optional manifest field, together giving paid modules a clean way to inject per-project log paths into a descriptor's request body. The RL module's two "Retrain global model" buttons were the first consumer; transcrypt and coordination will use the same pattern when they ship.

#### The problem this solves

A multi-select control persists an array of project UUIDs. A descriptor body wants to send an array of **paths** (one per selected project) to the container. The launcher knows the UUID → slug mapping (via the `projects` table); the container only knows its own log-path convention. Pre-v0.2.27 there was no template form that bridged the two — the only options were "container computes paths from UUIDs" (container doesn't have the slug map) or "duplicate the template in every action descriptor" (copy-paste hell across `online` + `offline` retrain buttons).

#### New manifest field: `runtime.log_path_template`

The module declares its host-side log-path convention in one place:

```jsonc
"runtime": {
  // ...existing fields...
  "log_path_template": "/data/logs/rl_events_{project_slug}.jsonl"
}
```

(Generic example: any per-project log-path convention works — the module declares its template, the launcher substitutes per project.)

**Closed-set tokens** inside the template:
- `{project_slug}` — the project's slug (DB column).
- `{project_id}` — the project's UUID.

**Single-brace deliberately**, to distinguish from the outer dispatcher `{{...}}` tokens. Any other `{...}` placeholder is a manifest-validation error caught by `validate_log_path_template` at parse time, so manifest typos surface at load time with a clear "unknown placeholder '{module_id}' (only {project_slug} / {project_id} allowed)" error rather than mid-dispatch with a confusing schema mismatch.

The field is **optional**. Modules that don't bind-mount per-project log paths simply omit it; if a manifest references `{{events_paths_for:<id>}}` without declaring `log_path_template`, the dispatcher refuses with a clear "module declares no log_path_template" error.

#### New dispatcher token: `{{events_paths_for:<control_id>}}`

Resolves to a `JsonValue::Array` of strings — one path per project the referenced control selected. Resolution pipeline:

1. Read the control's persisted value (renderer snapshot first, `module_settings` DB fallback). Must be a JSON array of strings (UUIDs).
2. For each UUID, look up the project via `db.get_project()` to get the slug.
3. Apply the module's `runtime.log_path_template` via `render_log_path_template` (substituting `{project_slug}` / `{project_id}`).
4. Return the resulting array.

**Whole-string-only.** The token MUST be the entire value of a body field. Embedded form (`"prefix-{{events_paths_for:x}}-suffix"`) is rejected at dispatch time because the resolution returns an array — there's no sensible way to embed an array into a longer string. The error message explicitly points module authors at the "WHOLE string value" requirement.

#### Example manifest (the RL pattern)

```jsonc
{
  // ... module metadata ...
  "runtime": {
    "type": "container",
    "log_path_template": "/data/logs/rl_events_{project_slug}.jsonl",
    // ... rest of runtime block ...
  },
  "gui": {
    "config_tab": {
      "sections": [{
        "controls": [
          {
            "kind": "multi_select",
            "id": "src_projects",
            "label": "Source projects",
            "options_source": "list_rl_global_training_source_projects"
          },
          {
            "kind": "button",
            "id": "retrain_global_offline",
            "label": "Retrain global model (offline)",
            "action": {
              "kind": "http",
              "method": "POST",
              "path": "/global/retrain",
              "body": {
                "mode": "offline",
                "event_log_paths": "{{events_paths_for:src_projects}}",
                "include_project_ids": "{{control:src_projects}}"
              },
              "polling": {
                "endpoint": "/global/retrain_status",
                "interval_seconds": 30,
                "max_attempts": 240
              }
            }
          }
        ]
      }]
    }
  }
}
```

For 3 selected projects with slugs `project-a` / `project-b` / `project-c`, the rendered request body is:

```json
{
  "mode": "offline",
  "event_log_paths": [
    "/data/logs/rl_events_project-a.jsonl",
    "/data/logs/rl_events_project-b.jsonl",
    "/data/logs/rl_events_project-c.jsonl"
  ],
  "include_project_ids": ["uuid-project-a", "uuid-project-b", "uuid-project-c"]
}
```

Notice the two tokens compose cleanly: `{{control:src_projects}}` passes the raw UUID array; `{{events_paths_for:src_projects}}` passes the per-project log-path array. The container gets both representations and decides which it wants.

#### Updated closed-set token table (v0.2.27)

| Token | Resolves to | Whole-string only? |
|---|---|---|
| `{{project_id}}` | active project's UUID | No |
| `{{module_id}}` | active module's id | No |
| `{{value}}` | the control's incoming value | No |
| `{{control:<id>}}` | another control's persisted value | No |
| `{{events_paths_for:<id>}}` (v0.2.27) | array of per-project log paths | **Yes — embedded form rejected** |

#### Error cases the dispatcher surfaces with clear messages

- Token used but module has no `runtime.log_path_template` → `"events_paths_for: ... referenced but module declares no runtime.log_path_template"`.
- Referenced control id not in the manifest or its value is missing → `"events_paths_for: control '<id>' has no value"`.
- Control's value isn't an array → `"events_paths_for: control '<id>' value must be an array, got <type>"`.
- An array element isn't a string → `"events_paths_for: control '<id>' array element <N> is not a string"`.
- A UUID doesn't resolve to a project in the DB → `"events_paths_for: project UUID '<uuid>' not found in DB"`.

All errors surface at dispatch time with the control_id / array index / UUID named — operators can copy-paste the message into a bug report and the cause is immediately legible.

## How to wire a NEW paid module's config tab (practical recipe)

**Author POV: I want the launcher to show my module's settings without rebuilding the launcher.**

### Step 1 — Declare `gui.config_tab` in `vct-module.json`

```json
{
  "id": "my-module",
  "name": "My Module",
  "version": "0.1.0",
  // ... rest of manifest ...
  "gui": {
    "config_tab": {
      "title": "My Module",
      "icon": "sliders",
      "description": "Configure my-module's behaviour.",
      "show_in_sidebar": true,
      "sections": [
        {
          "title": "Settings",
          "collapsible": false,
          "controls": [
            {
              "kind": "checkbox",
              "id": "enable_feature_x",
              "label": "Enable feature X",
              "tooltip": "Toggles the X behaviour. Off by default.",
              "default": false,
              "on_change": "my_module_toggle_feature_x"
            },
            {
              "kind": "select",
              "id": "verbosity",
              "label": "Log verbosity",
              "tooltip": "Sets the log level for my-module's container output.",
              "options": [
                {"value": "debug", "label": "Debug"},
                {"value": "info", "label": "Info"},
                {"value": "warn", "label": "Warn"}
              ],
              "default": "info",
              "on_change": "my_module_set_verbosity"
            },
            {
              "kind": "button",
              "id": "reset_state",
              "label": "Reset module state",
              "tooltip": "Clears my-module's local cache. Container restart not required.",
              "action": "my_module_reset_state",
              "variant": "danger",
              "confirm": "Reset my-module's cached state? This cannot be undone."
            }
          ]
        }
      ]
    }
  }
}
```

That's it for the manifest side. After install, the launcher shows the tab in its "Module configuration" sidebar group automatically.

### Step 2 — Implement the referenced Tauri commands in the launcher

Every `on_change` + `action` + `options_source` string is a Tauri command name the launcher must have registered. **This is the only place a launcher rebuild is required.**

Add commands in `launcher/src-tauri/src/commands/<module>_settings.rs`:

```rust
// SPDX-License-Identifier: AGPL-3.0-or-later
use serde::{Deserialize, Serialize};
use tauri::{command, State};
use crate::db::Db;

#[command]
pub async fn my_module_toggle_feature_x(
    project_id: String,
    value: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    // Persistence is automatic via the renderer's call to
    // set_module_setting; this on_change is for SIDE EFFECTS
    // (e.g., signal the container to reload config).
    // db.set_module_setting(...) is already done by the time this fires.
    println!("[my-module] feature_x toggled to {}", value);
    Ok(())
}

#[command]
pub async fn my_module_set_verbosity(
    project_id: String,
    value: String,
    db: State<'_, Db>,
) -> Result<(), String> { /* ... */ Ok(()) }

#[command]
pub async fn my_module_reset_state(
    project_id: String,
    db: State<'_, Db>,
) -> Result<(), String> { /* ... */ Ok(()) }
```

Register them in `launcher/src-tauri/src/lib.rs::invoke_handler!`:

```rust
.invoke_handler(tauri::generate_handler![
    // ... existing handlers ...
    commands::my_module_settings::my_module_toggle_feature_x,
    commands::my_module_settings::my_module_set_verbosity,
    commands::my_module_settings::my_module_reset_state,
])
```

### Step 3 — Future tab tweaks don't require a launcher rebuild

After step 2's commands ship: the module author can freely:
- Add new controls referencing the SAME existing commands → just edit `vct-module.json`, reinstall module.
- Re-order sections, rename labels, update tooltips → manifest-only.
- Remove controls → manifest-only.

A launcher rebuild is ONLY needed when:
- A NEW Tauri command is referenced via legacy `ActionRef::Legacy("cmd_name")` (pointing at a non-existent command). **v0.2.26 escape hatch:** if your action can be expressed as an HTTP call to your container, use `ActionRef::Descriptor` instead — the generic `module_dispatch_action` handles it without a rebuild.
- A NEW widget `kind` is needed beyond the current 10: `checkbox` / `multi_select` / `button` / `select` / `info` (v0.2.20) + `text_input` / `number_input` / `status_display` / `file_picker` / `link` (v0.2.26). Adding a new kind = Rust enum variant + Svelte dispatch arm + this KG node update.
- A NEW `ActionDescriptor` kind is needed beyond `http` (e.g. `shell`, `event`). Adding a new descriptor = Rust enum variant + dispatcher match arm + this KG node update.
- A NEW template-substitution variable is needed beyond `{{project_id}}` / `{{module_id}}` / `{{value}}` / `{{control:<id>}}`. Closed-set by design.

### Step 4 — Verify in the running launcher

1. Reinstall the module (`install-bundle --update` or via the launcher's Modules tab).
2. Relaunch the launcher OR fire the `reload_manifests` Tauri command (if exposed; sidebar refresh on next mount otherwise).
3. The "Module configuration" sidebar group should now show "My Module".

### What state lives where

| State | Location | Owner |
|---|---|---|
| Config tab schema | Module's `vct-module.json gui.config_tab` | Module author |
| Tauri commands (impl) | Launcher's `commands/<module>_settings.rs` + `lib.rs invoke_handler!` | Launcher author (you) |
| Per-control values | Launcher's `module_settings` DB table | Launcher (auto-persisted by renderer) |
| Side effects on change | Module's container OR Tauri commands above | Either, depending on what the side-effect needs to touch |

## Tradeoffs

- **Five control kinds is intentionally narrow**. Adding a new kind requires extending both the Rust enum AND the Svelte dispatch. This friction is wanted: each new kind is a launcher-API contract that ships forever.
- **No dynamic schema updates** — the manifest is read at launcher startup. Module authors who change `config_tab` must bump module version + reinstall.
- **No client-side validation beyond Pydantic-level types** — `on_change` Tauri commands carry the validation burden.
- **Tauri commands are launcher-side** — adding new commands DOES require a launcher build + ship. The schema-rendered approach minimizes this surface (most config_tab tweaks reuse existing commands) but doesn't eliminate it.

## First consumers (v0.2.20)

1. **vct-rl-reranker** (paid module) — 3 sections + 10 controls covering all 5 kinds. See `paid-modules/vct-rl-reranker/vct-module.json`.
2. **orchestrator-core** (always installed) — root `vct-module.json` gets `gui.config_tab` for KG + code-graph controls. Validates the schema generalizes beyond paid modules.

## Related

- [[relatedTo::Launcher Packaging & Paid-Module Distribution Design]] — parent design that motivated the framework.
- [[uses::Tauri 2]] — `invoke_handler!` for the per-control side-effect Tauri commands; `module_dispatch_action` is the v0.2.26 generic entry point.
- [[uses::Svelte 5]] — `$state` + `$props` + `$derived` for the renderer.
- [[relatedTo::GPU Mode Decision Policy]] — related dispatch concept (per-module behavior reading from manifest).
- [[buildsOn::Orchestrator-root per-path 3-way merge during update (A0, v0.2.24)]] — A0 + this framework are sibling examples of "schema/config drives UI, launcher stays generic". A0 uses `git merge-file` primitives + a hardcoded allowlist; this framework uses a JSON schema + the widget palette.
- [[buildsOn::Generic Per-Module DB Architecture]] — v0.2.26 dispatcher resolves `module_id → port` via the generic `module_ports` table; same generalization wave that produced ActionRef/ActionDescriptor.
- [[relatedTo::WebKit EGL Pre-Flight Probe]] — sibling v0.2.26 launcher hardening; unrelated subsystem but ships in the same release.
