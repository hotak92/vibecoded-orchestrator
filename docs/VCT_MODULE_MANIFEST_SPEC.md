# VCT Module Manifest Specification

A **VCT module** is an independently-deployable unit the launcher can install,
configure, start, stop, update, and tier-gate. Every module ships a
`vct-module.json` at its repo root (or, for container modules, baked into the
image). The launcher reads that file, enforces the license gate, runs the
install method, registers any MCP with the consuming Claude Code client, renders
the module's GUI surfaces, and supervises its runtime — with **zero
launcher-side code changes** for the common cases.

## Source of truth

The manifest contract is defined by the Rust types in
`launcher/src-tauri/vct-launcher-core/src/manifest.rs` (`ModuleManifest` and its
sub-structs), which derive `schemars::JsonSchema`. The JSON Schema at
[`docs/schemas/vct-module.schema.json`](schemas/vct-module.schema.json) is
exported from those types and is the artifact publishers validate against.

The `manifest-validate` CI workflow keeps the two in lockstep:

- **schema-drift** — `export-schema --check` fails the build unless the committed
  `docs/schemas/vct-module.schema.json` byte-matches a fresh export from
  `manifest.rs`. Touching the Rust types without regenerating the schema is a red.
- **validate-manifest** — every committed paid-module fixture round-trips through
  `ModuleManifest::from_json` in strict mode.
- **integration tests** — `cargo test -p vct-launcher-core --test manifest_ci_gate`
  pins the parser invariants (a real fixture validates, garbage rejects, the
  schema includes/excludes the right variants).

**Strict in CI, lenient in production.** CI sets `VCT_LAUNCHER_STRICT_MANIFEST=1`
so unknown enum variants (e.g. a future GUI control kind) fail at PR time. A
shipped launcher instead maps unknown control kinds to an `Unsupported` variant
and renders a placeholder — forward-compat so an older launcher tolerates a
newer paid-module manifest rather than refusing to install it.

When this document and the schema disagree, the schema (and the Rust types
behind it) win. This spec describes the same contract in prose.

---

## 1. Top-level shape

```jsonc
{
  "manifest_version": 1,
  "id": "vct-example",                 // required — globally-unique module id
  "name": "Example Module",            // required — display name
  "version": "0.1.0",                  // required — semver
  "category": "paid-independent",      // required — see §2
  "install": { … },                    // required — see §5
  "runtime": { … },                    // required — see §6

  "description": "One-line summary.",
  "publisher": "VibeCoded Tools",
  "homepage": "https://…",
  "repository": "https://github.com/…",
  "icon": "icon.png",
  "tags": ["mcp", "team"],

  "compatibility": { … },              // §3
  "license": { … },                    // §4
  "requirements": { … },               // §4.1
  "secrets": [ … ],                    // §7
  "settings": [ … ],                   // §8
  "mcp_registration": { … },           // §9
  "gui": { … },                        // §10
  "db": { … },                         // §11
  "kg_collections": ["Some_Collection"], // §12
  "setup_wizard": { … },               // §13
  "upgrade": { … },                    // §13
  "uninstall": { … },                  // §13
  "telemetry": { … },                  // §14
  "provides": [ … ],
  "consumes": [ … ]
}
```

**Required fields**: `id`, `name`, `version`, `category`, `install`, `runtime`.
Everything else has a serde default. `manifest_version` defaults to `0` when
absent.

The parser is permissive about **unknown top-level fields** (forward
compatibility) but strict about **required ones** and about **enumerated values**
(category, install method, install scope) — an unrecognized enum value is
rejected early with a clear error rather than surfacing deep in the install flow.

**Author comment convention**: top-level keys starting with `_` (e.g.
`_notes`, `_gui_doc`) are ignored by the parser — use them for manifest-author
notes instead of JSON comments (which are not valid JSON). `CommandSpec` entries
accept a `_note` field for the same purpose.

---

## 2. `category`

One of (kebab-case on the wire):

| Value | Meaning |
|---|---|
| `core` | Ships with the orchestrator clone (KG MCP, Code Graph, etc.). |
| `paid-orchestrator` | Unlocked by the user's orchestrator tier. |
| `paid-independent` | Standalone product with its own license variants. |
| `community` | User-contributed. |

---

## 3. `compatibility`

```jsonc
"compatibility": {
  "hosts": ["base", "mao"],          // host types this module attaches to
  "min_launcher_version": "1.0.0"    // optional
}
```

`hosts` gates which project types can install the module. Both fields default
(empty `hosts`, `null` min version) when the block is omitted.

---

## 4. `license`

```jsonc
"license": {
  "required": false,                 // true → license gate enforced
  "type": "AGPL-3.0-or-later",       // SPDX identifier (optional)
  "variant_ids": [],                 // LemonSqueezy variant ids that unlock it
  "min_orchestrator_tier": "free",   // unlocked if orch tier >= this
  "trial_days": 0                    // N = first N days unlocked
}
```

A `paid-orchestrator` module typically leaves `variant_ids` empty and gates on
`min_orchestrator_tier: "pro"`. A `paid-independent` product lists its
`variant_ids`. Client-side gates are advisory — real enforcement is server-side:
for `container_pull` modules the pull-token gateway re-validates the user's
validated-tier JWT on every artifact request, so an un-licensed user cannot
obtain the image at all.

### 4.1 `requirements`

```jsonc
"requirements": {
  "os": ["linux", "macos", "windows"],
  "python": ">=3.11",                // null if no python component
  "node": null,
  "memory_mb": 128,
  "disk_mb": 50,
  "network": ["https://*.supabase.co"],
  "gpu": false,                      // true → GPU strictly required
  "depends_on": []                   // other module ids this one needs
}
```

All fields default; the whole block is optional.

---

## 5. `install`

```jsonc
"install": {
  "method": "container_pull",        // required — see below
  "install_dir": "{VCT_MODULES}/{MODULE_ID}",
  "scope": "per_project",            // "per_project" (default) | "global"
  "source": "https://…",             // git url for git_clone
  "ref": "v0.1.0",                   // tag/branch/commit
  "post_install": [ { "cmd": "…", "cwd": "…", "timeout_s": 120 } ],
  "container": { … }                 // required when method = container_pull
}
```

### `method`

| Value | Behaviour |
|---|---|
| `git_clone` | Clone `source` at `ref` into `install_dir`. |
| `local` | Use an existing directory at `install_dir` (dev / orchestrator-core). |
| `container_pull` | Pull a private-registry image via a short-lived signed pull-token. Requires the `container` block. |

`tarball` / `pypi` / `npm` are **not** valid values — a manifest declaring one
fails to deserialize with a clean serde error.

### `scope`

- `per_project` (default) — one install row + one container **per project**. The
  container name follows `runtime.container_name_template` with `{project_slug}`
  substituted.
- `global` — exactly one install row per machine (`project_id IS NULL`) and one
  container named after the bare module id (no slug suffix). Per-project
  personalization happens inside the container via request headers (e.g. a
  global reranker reads `X-VCT-Project-ID`). Setting `kg_collections` (§12) on a
  global module grants every project a default KG-access row at install time.

### `container` (required for `container_pull`)

```jsonc
"container": {
  "image": "ghcr.io/publisher/vct-example",   // WITHOUT tag
  "tag_from_version": true,                    // tag = manifest.version
  "registry": "ghcr.io",                       // inferred from image if absent
  "pull_token_endpoint": "https://…/artifact-url",
  "pull_token_method": "POST",
  "rotate_weights": false,
  "rotate_weights_endpoint": "https://…/latest-weights"
}
```

The installer POSTs the user's validated-tier JWT to `pull_token_endpoint`,
receives `{ image, tag, pull_token, expires_at }` (TTL ~15 min, single-use),
then runs `podman pull` / `docker pull` with the token env-injected (never
written to disk). When `tag_from_version` is false, the tag comes from
`install.ref` (allows floating-tag pulls during early beta). `rotate_weights` +
`rotate_weights_endpoint` let the launcher's daily poller refresh model weights
independently of image-version pulls.

---

## 6. `runtime`

```jsonc
"runtime": {
  "type": "container",               // required — see below
  "command": "",                     // executable for mcp_stdio / cli; EMPTY for container/service
  "args": [],
  "platform_command": { "windows": "…" },
  "cwd": null,
  "env_from_secrets": ["SOME_TOKEN"],
  "env_from_settings": ["SOME_SETTING"],
  "env_fixed": { "MODE": "prod" },
  "health_check": { "type": "http_get", "url": "…", "timeout_s": 5, "interval_s": 30 },
  "auto_restart": true,
  "log_file": "{VCT_LOGS}/{MODULE_ID}.log"
}
```

`type` is one of `mcp_stdio` | `mcp_http` | `service` | `cli` | `container`.

**Container / service modules must leave `command` empty** — a non-empty
`command` overrides the image's baked ENTRYPOINT and has historically caused the
CMD to argparse-fail (the container CMD landing appended to a `podman run …`
line). Empty `command` = declarative form: the image ENTRYPOINT runs unmolested.

### Container runtime fields

Meaningful only when `type == "container"` — the supervisor
(`vct-hub::module_supervisor`) ignores them for other runtime types:

```jsonc
"container_name_template": "vct-example-{project_slug}",   // {project_slug} only, or {module_id} for global singletons
"image_ref": "{install.container.image}:{install.container.tag}",
"ports": [ { "host": "{RL_SERVER_PORT}", "container": 8080, "bind": "127.0.0.1" } ],
"volumes": [ { "host": "…", "container": "…", "mode": "rw" } ],
"env_derived": { "PORT": "{RL_SERVER_PORT}" },             // placeholder-substituted at start; distinct from literal env_fixed
"log_path_template": "…",
"min_gpu_vram_gb": 4.0,             // per-module VRAM threshold for GPU-mode decision
"gpu_optional": true,              // true → runs on CPU (degraded) when no qualifying GPU
"gpu_image_variants": { "cpu": "0.1.0-cpu", "cuda": "0.1.0-cuda", "rocm": "0.1.0-rocm" }
```

`ports[].bind` defaults to `127.0.0.1` so per-project containers are never
exposed to the LAN by accident. When `gpu_image_variants` is present, the
launcher reads the GPU-mode decision and picks the matching tag (Cuda → `cuda`,
Rocm → `rocm`, Cpu/Metal → `cpu`); when absent, the single tag from
`install.container` is used. A `gpu_optional: false` module refuses to install
without a qualifying GPU and surfaces a clear error.

`container_name_template` and `image_ref` are optional on the wire — the
launcher synthesizes sensible defaults (`{module_id_safe}-{project_slug}` and
the container image ref respectively) when they are absent, so a container
module is installable without declaring every structured field.

---

## 7. `secrets`

```jsonc
"secrets": [
  {
    "key": "SUPABASE_KEY",
    "prompt": "Supabase service-role key",
    "description": "…",
    "example": "eyJ…",
    "validation": "^(sb_secret_|eyJ).{20,}",
    "required": true,
    "scope": "per-project",          // "global" | "per-project" (default) | "shared"
    "sensitive": true                // redacted in logs, masked in UI
  }
]
```

The launcher's SecretsPanel collects declared secrets and stores them in the OS
keychain (namespace `vct.{scope}.{module_id}.{key}`); the hub's
`/api/v1/projects/{id}/env` resolver serves them to the module at start via
`runtime.env_from_secrets`. Modules never read secrets from user-visible config
files. `required` defaults to `true`; `scope` defaults to `per-project`.

---

## 8. `settings`

```jsonc
"settings": [
  {
    "key": "CHANNEL_WATCH",
    "prompt": "Channels to subscribe to",
    "description": "…",
    "type": "multiselect",           // "string" (default) | "integer" | "boolean" | "multiselect" | "path"
    "default": ["messages"],
    "default_by_platform": { "windows": "…" },
    "options": ["messages", "decisions", "work_items"],
    "validation": "^…$",
    "validation_cmd": "…",
    "required": false,
    "min": 0,
    "max": 100
  }
]
```

User-editable via the launcher GUI; persisted in the generic `module_settings`
KV store and injected via `runtime.env_from_settings`.

---

## 9. `mcp_registration`

```jsonc
"mcp_registration": {
  "enabled_by_default": true,        // defaults true
  "mcp_name": "vct-example",         // name under mcpServers.{…}
  "target_projects": "all",          // "all" (default) | "none" | ["path", …]
  "scope": "user",                   // "user" (default, ~/.claude.json) | "project" (.mcp.json)
  "tool_allowlist": [
    { "tool": "render", "default_enabled": true, "description": "…" }
  ]
}
```

When present, the launcher writes the MCP into the target scope(s). The optional
`tool_allowlist` seeds per-tool default-enabled state into
`module_mcp_tool_defaults`; the hub composes the resolved allowlist from these
defaults plus per-project overrides (per-project rows always win, absent rows
fall through to `default_enabled`). On update, added tools are inserted with
their declared default, removed tools are dropped, and per-project overrides are
left in place. `default_enabled` defaults to `true`.

---

## 10. `gui`

```jsonc
"gui": {
  "config_tab": {
    "title": "Example",
    "icon": "box",
    "route": "/modules/example/config",   // optional custom route
    "description": "…",
    "show_in_sidebar": true,
    "sections": [
      {
        "title": "Section",
        "collapsible": false,
        "initially_collapsed": false,
        "controls": [ { "kind": "checkbox", "id": "…", "label": "…", … } ]
      }
    ]
  }
}
```

The launcher renders the config tab from a **fixed widget palette** — module
authors describe *what* to show, the launcher decides *how*. Modules ship no
Svelte. Twelve control `kind`s are supported: `checkbox`, `multi_select`,
`button`, `select`, `info`, `text_input`, `number_input`, `status_display`,
`file_picker`, `link`, `info_dynamic`, `date_picker`. Every control accepts an
optional `tooltip`.

Interactive controls reference actions via an **ActionRef**, which is either a
legacy Tauri command name (a bare string) or a structured **ActionDescriptor**
that the generic dispatcher executes with no per-module Rust:

- `http` — an HTTP request to the module's container (127.0.0.1-only), with an
  optional `polling` block that turns it into a long-running pollable job.
- `chained_action` — a serial sequence of step descriptors, each step's response
  threaded into the next via `{{previous_step.<field>}}`; optional `polling`
  attaches to the final step.
- `tauri_command` — an explicit named Tauri command (structured form).

`info_dynamic` controls read live values from a `module_db` source (a SELECT
against the module's own namespaced tables via the DB surface) and render a
`fallback` string when the source is unavailable.

The orchestrator core's own root `vct-module.json` uses this same `gui.config_tab`
schema (its "Clone integrity" tab) — proof that the schema generalizes beyond
paid modules; both go through `ModuleManifest::from_json` and
`get_module_nav_items`.

---

## 11. `db`

```jsonc
"db": {
  "migrations_dir": "db/",           // relative to install_dir
  "namespace": "example"             // [a-z][a-z0-9_]* — every module table MUST be example_*
}
```

When present, the launcher applies SQL files matching `[0-9]+_*.sql` (zero-pad to
4 digits so string sort == numeric sort) from
`{install_dir}/{migrations_dir}/` at install + update time. Application is
idempotent, SHA256-tracked in the launcher's `module_db_migrations` table.

**Namespace enforcement**: every table the module creates or alters MUST be
prefixed `{namespace}_`. The launcher refuses SQL that touches tables outside the
declared namespace. FOREIGN KEY references *into* launcher-owned tables (e.g.
`projects(id)`) are allowed — the constraint governs DDL subjects, not FK
targets. Module state lives in these namespaced tables inside the single
`launcher.db`, so dashboard widgets can read module state without waking a
stopped container. Keep the block additive: once a paid module ships a `db`
block, breaking changes to it break that module's installed users on every
subsequent update.

---

## 12. `kg_collections`

```jsonc
"kg_collections": ["Example_Shared_KG"]
```

KG collections a module writes to. On an `install.scope: "global"` module, every
project gains a default KG-access row at install time. Ignored for per-project
modules. Absent or empty is equivalent (no rows inserted).

---

## 13. `setup_wizard` / `upgrade` / `uninstall`

```jsonc
"setup_wizard": {                    // runs once after first install
  "command": "…", "args": ["…"],
  "platform_command": { "windows": "…" },
  "env_from_secrets": ["…"], "env_from_settings": ["…"],
  "success_marker": "{install_dir}/.setup_complete"
},
"upgrade": {                         // on version change
  "strategy": "git_pull",            // "git_pull" | "reinstall" | "migrate"
  "pre_upgrade": [], "post_upgrade": [ { "cmd": "…" } ],
  "migration_script": null
},
"uninstall": {
  "remove_install_dir": true,
  "preserve_paths": ["{VCT_DATA}/{MODULE_ID}/"],   // survive uninstall
  "deregister_mcp": true,
  "clear_secrets": false             // false → secrets stay in the vault for reinstall
}
```

All three blocks are optional.

---

## 14. `telemetry`

Parsed as an opaque JSON value. The launcher does not currently merge per-module
telemetry declarations into its own collector — the block is reserved. See
[`TELEMETRY.md`](TELEMETRY.md) for what the launcher and the RL retrieval
reranker actually collect.

---

## 15. Placeholder resolution

Strings in `install`, `runtime`, `setup_wizard`, and `uninstall` may contain
placeholders the launcher expands at runtime:

| Placeholder | Resolves to |
|---|---|
| `{VCT_ROOT}` | The VCT root dir (platform-appropriate). |
| `{VCT_MODULES}` | Module install root. |
| `{VCT_DATA}` | Module data root. |
| `{VCT_LOGS}` | Log root. |
| `{install_dir}` | Resolved `install.install_dir`. |
| `{MODULE_ID}` | The module's `id`. |
| `{project_slug}` | The active project's slug (container-name / port templates). |
| `{project_id}` | The active project's UUID. |

---

## 16. Validate a manifest locally

```bash
# Node.js (ajv)
npx ajv-cli validate -s docs/schemas/vct-module.schema.json -d path/to/vct-module.json

# Python (jsonschema)
python -c "
import json, jsonschema, pathlib
schema = json.loads(pathlib.Path('docs/schemas/vct-module.schema.json').read_text())
manifest = json.loads(pathlib.Path('path/to/vct-module.json').read_text())
jsonschema.validate(manifest, schema)
print('valid')
"
```

The install-time sanitizer `vco_lib/manifest_validation.py` adds a defence layer
on top of schema validation (required keys, container-as-CMD indicators,
scope/container-name coherence) and is CLI-invocable from the launcher.

See [`PAID_MODULE_DEV_CHECKLIST.md`](PAID_MODULE_DEV_CHECKLIST.md) for the
end-to-end publisher flow and `docs/publisher-ci/` for the reusable validators.
