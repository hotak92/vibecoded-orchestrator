# Configuration Philosophy

Config layout follows one rule: **minimal global, maximum per-project**.

## What this means

- **Global `~/.claude/settings.json`**: user preferences only — effort level, output tokens, universal permission denies. No project paths, no MCP server URLs, no environment variables that any specific project depends on.
- **Per-project `.claude/settings.json`**: per-project permissions and hook registrations, plus an `env` block read by Claude Code (CLI, Desktop app, AND the VS Code extension) and propagated to MCP subprocesses. This is the **canonical per-project MCP env channel**. Do not use `.vscode/settings.json` `claude-code.env` for these keys — empirical sentinel testing on Linux confirmed that block does NOT propagate to MCP subprocesses, and the launcher does not write it. See `docs/CLAUDE_CODE_COMPATIBILITY.md` → "Per-project env files".
- **Per-project `.claude/env`**: POSIX shell-sourceable env file with the same values, for CLI users who source it from their shell rc via the `tools/claude` wrapper.
- **Per-project `.vscode/settings.json`**: VS Code editor preferences only (Pylance excludes, file-watcher excludes, formatter settings). The launcher's Python-side `_backfill_vscode_excludes_in_project` manages the Pylance/watcher exclude block; the launcher does NOT touch any `claude-code.env` block here. The launcher also writes `.vscode/tasks.json` with a `folderOpen` task that ensures `vct-hub` is running for VS Code users (Step 8).
- **Per-project secrets**: stored in the OS keychain via the VCT Launcher GUI — not in env files, not in JSON configs. The launcher knows about per-project scoping, so an OpenAI key for one project doesn't leak into another. A small set of shared secrets (`github_pat`, `openai_api_key`) lives under SENTINEL_SHARED / `module_id=user` and is resolved by the hub for every project — see "Secrets" below.

## Why

It prevents cross-contamination. Global settings apply to every project you open — set `KG_COLLECTION=MyMainProjectKG` globally and every other project will silently reuse that collection and mix knowledge graphs.

## Setup for new users

1. Copy `.vscode/settings.json.example` to `.vscode/settings.json` and adjust as needed for editor preferences (Pylance excludes, formatter settings). The example file carries no `claude-code.env` block — per-project MCP env lives in `.claude/settings.json` `env` (see the note above).
2. The VCT Launcher creates a per-project `.env` from a canonical template when you register a project (see "`.env` template management" below). For non-launcher CLI users, copy `.env.example` manually.
3. Let `install.py` wire the rest (venv, containers, KG collection creation, `vct-hub` binary placement + boot sentinel).
4. Launch via the VCT Launcher GUI (manages secrets, tier gating, module installs). Secrets entered via the GUI (or via the OnboardingWizard at first run) are immediately resolvable by the hub for every registered project.

## Install entry-point flow

`first-install.{sh,command,bat}` (Linux / macOS / Windows) and `install.ps1` (PowerShell) are thin shims around `install.py`. The shim sequence per invocation:

1. **Python detect**: a candidate cascade (newest first: `python3.13` → `python3.12` → `python3.11` → `python3` / `python`, plus the linuxbrew prefix on Linux). The first candidate that reports `sys.version_info >= (3, 11)` wins. Missing Python fails with a distro-aware install hint.
2. **Bootstrap prepass**: `python install.py --bootstrap --json` writes a versioned, read-only system-detection envelope to `state/logs/bootstrap-prepass.json`. No install side effects; no writes outside that one file; no network; every probe has a ≤10 s timeout. Failure is soft — the full install still runs even if the prepass crashes.
3. **Full install**: `python install.py <forwarded args>` runs the canonical 10-step flow. The shim forwards user argv verbatim (with `--non-interactive` translated to `--yes` for backward compatibility).
4. **Auto-spawn launcher**: when install.py exits 0 and the user did not pass `--no-auto-launch` (or set `VCT_NO_AUTO_LAUNCH=1`), the shim runs `scripts/post-install-launcher.sh` (or the inline Windows equivalent inside `first-install.bat`). That script is best-effort: it ALWAYS exits 0 — a broken launcher spawn must not mask a successful install.
   - When no usable launcher binary is on disk, the prebuilt one for this OS, arch and VCO version is downloaded without a prompt and checked against the `.zip.sha256` sidecar the release publishes beside it (`python -m vco_lib.launcher_acquire verify-sha256`, the one check both `post-install-launcher.sh` and `first-install.bat` call). A digest mismatch, or a sidecar that is missing or unreadable, means the download is never unpacked or run: it counts as "no prebuilt obtained", so you are asked whether to build from source (`--yes` builds).

`--bootstrap` is the prepass-only mode. It is exclusive with `--update`, `--lightweight`, and `--uninstall`; combining them aborts before any work runs. `--bootstrap` is NOT the install entry point — it is exclusively a read-only probe consumed by the shim, by `vco_lib`, and by future Rust callers that need a consistent view of host capabilities before touching disk. See `install.py:618` for the policy comment and `install.py:1524` for the dispatcher.

## Bootstrap envelope (`state/logs/bootstrap-prepass.json`)

The schema is published at [`docs/schemas/install-bootstrap-envelope-v1.json`](schemas/install-bootstrap-envelope-v1.json) and pinned via the `schema_version: 1` constant — consumers MUST refuse versions they don't know how to read. Top-level keys include:

| Key | Purpose |
|---|---|
| `system` | OS, arch, RAM, CPU count, and tool probes (Python with wheel-coverage flag, Node, npm/pnpm, Podman, Docker, git, brew, lean-ctx, `claude` CLI), GPU summary (vendor / model / VRAM / driver / container-toolkit), distro-specific feature blocks (`linux_distro`, `macos_features`, `windows_features`). |
| `paths` | `install_root` + classification (`orchestrator_clone` / `completed_install` / `git_repo` / `unknown`), venv interpreter paths, launcher + hub binary paths and exists flags, state-dir locations. |
| `package_manager_advice` | Per-tool install command vectors for the host's primary package manager (apt / dnf / pacman / zypper / apk / winget / brew), plus `selinux_volume_flag_needed` (Fedora/RHEL with bind-mount layouts) and the NVIDIA Container Toolkit URL when relevant. |
| `weaviate_endpoints` | Canonical Weaviate endpoints — notably `health: /v1/.well-known/ready` (this is the SSOT; Rust + bash consumers MUST read it from the envelope rather than inventing their own probe path). |
| `ollama_endpoints` / `code_embed_endpoints` / `vct_hub_endpoints` | Same canonical-URL pattern for the other local services. |
| `missing_prereqs` | Array of `{name, human, severity, install_hint}` entries with severities `blocking` / `warning` / `optional`. |
| `ready_to_install` | True iff no `blocking` entries are present. Envelope exit-code 0 does NOT imply readiness — consumers SHOULD check this flag. |

The envelope is also useful as a diagnostic artifact: when reporting an install failure to a maintainer, attach `state/logs/bootstrap-prepass.json` so they can see the exact host shape (OS, arch, GPU, tool versions, distro package manager) the install ran on. The file is regenerated on every first-install shim invocation.

## `.env` template management

A project's `.env` has ONE writer, `vco_lib.env_template` (v0.2.97). The launcher's `create_project_v2` runs `python -m vco_lib.env_template apply` on the project root; `install.py` Step 9 (and `--update`) writes the orchestrator root's `.env` through the same function, via `vco_lib.install_env`. VCO owns only the block between the markers:

```
# >>> VCO-MANAGED ENV (do not edit between markers) >>>
# added by vco — KG_COLLECTION=Acme_KnowledgeGraph
KG_COLLECTION=Acme_KnowledgeGraph
...
# <<< VCO-MANAGED ENV <<<
```

- **`.env` missing** → a new file: commented placeholders for the optional keys (LLM API keys, `GITHUB_TOKEN`, RL module URLs, `VCT_TELEMETRY`), then the managed block. For the orchestrator root the new file starts with the install-time keys instead (`EMBEDDING_MODEL`, `CODE_EMBED_*`, `EMBEDDING_PROVIDER`, `VCT_TELEMETRY`, …).
- **`.env` exists** → the managed block is replaced in place (or appended once, when the file has none). Everything outside the markers is preserved byte-for-byte, and **a key you assign outside the block is never rendered inside it** — your line is that key's only assignment, wherever it sits. A commented `# KEY=` line sets nothing, so it does not suppress the managed value.
- **Legacy lines** written by pre-v0.2.97 VCO (`# added by vco YYYY-MM-DD: appended missing canonical keys`, the old template's `# === Service URLs …` / `# === Per-project Weaviate collections ===` sections, `# --- Added by install.py --update on … ---`) are folded into the block: their lines for keys the block now carries are removed, so each key ends up assigned once. A folded line whose value differed from VCO's is kept outside the block as `<KEY>_old=<value>` (`_old2`, … when that name is taken; never overwritten, never duplicated), with the key name recorded in `.claude/logs/auto-resolutions.jsonl` and one comment line above them (written once); secret-looking keys are never folded.
- **Unregistering the project** removes only what VCO wrote to `.env`: the block, the legacy sections above, that comment, and VCO's header at the top of a `.env` it created (only while the header is unedited). A key you assign on your own line, your `<KEY>_old` values and any `# KEY=` comment stay, and the unregister result names the keys. The same rule covers `.claude/env` (VCO's marked block goes whole) and the `env` blocks of `.claude/settings.json` / `.vscode/settings.json`: outside a marked block, a routing key goes only when it holds the value VCO writes for the project.
- **Safe add** → the live `.env` is never touched; `python -m vco_lib.env_template reference` writes what a new `.env` would hold to `.env.vco.reference` instead.
- **Idempotent** — a second run against an up-to-date file writes nothing.
- The orchestrator root's refresh (`install.py` re-install / `--update`) is fill-only: it adds keys to the block but never changes a value already there.

Keys the managed block carries (`list_canonical_env_template_keys`):

```
PROJECT_NAME, CODE_GRAPH_PROJECT
KG_COLLECTION, DEVELOPMENT_COLLECTION, SHARED_KG_COLLECTION
SHARED_KG_WRITE_DISABLED, SHARED_KG_OPT_OUT, SHARED_KG_READ_DISABLED
ACTIVE_EMBEDDING
WEAVIATE_URL, WEAVIATE_PORT, OLLAMA_URL, OLLAMA_PORT, CODE_EMBED_URL, CODE_EMBED_PORT
```

## What goes in each file

| Config | Lives in | Scope | Managed by |
|---|---|---|---|
| Effort level, max tokens, OS-level denies | `~/.claude/settings.json` | global | you, manually |
| MCP env (URLs, collection names, paths) — every Claude Code surface (CLI / Desktop / VS Code extension) AND MCP subprocesses | `.claude/settings.json` → `env` | per-project | launcher's env projection (`python -m vco_lib.config_projection apply`) |
| MCP env, POSIX shell-sourceable copy (for the `tools/claude` wrapper) | `.claude/env` | per-project | launcher's env projection (`python -m vco_lib.config_projection apply`) |
| VS Code editor preferences (Pylance excludes, formatOnSave, etc.) | `.vscode/settings.json` | per-project | launcher's Python `_backfill_vscode_excludes_in_project` + you |
| VS Code `folderOpen` task that ensures `vct-hub` is running | `.vscode/tasks.json` | per-project | install.py Step 8 / `update_project_v2` bundle update |
| Shell/script env | `.env` | per-project | you, `.env.example` template |
| Project permissions + hooks | `.claude/settings.json` | per-project | install.py + launcher |
| Secrets (license keys, API tokens) | OS keychain | per-project (with shared bucket for `github_pat` / `openai_api_key`) | launcher GUI / OnboardingWizard only |
| Hooks scripts | `.claude/hooks/` | per-project | install.py |
| Bundled agents | `.claude/agents/` | per-project | installed by default (from `templates/agents/free/`) |
| Project skills | `.claude/skills/` | per-project | install.py (from `templates/skills/`) |
| Generic agents (e.g. `code-migrator`) | `~/.claude/agents/` | global | you, optional |
| Generic skills (e.g. `debug-expert`) | `~/.claude/skills/` | global | you, optional |

## What does NOT go in global

- MCP server definitions (they point at this project's venv + source paths)
- Plugin enable flags (`enabledPlugins`) — plugins are project-specific
- Project paths (collection names, code-graph prefixes, etc.)
- Collection names (`KG_COLLECTION`, etc.)
- Embedding model defaults (differ per project tier)

If you see any of these in your global `~/.claude/settings.json`, move them to the per-project config. They're leaking.

## Env var resolution precedence

Per-project env vars (KG / codegraph / embedding selections, service URLs) flow through a fixed 5-level precedence chain. Higher levels override lower ones; consumers (MCP subprocesses, hooks, install.py, the launcher) all resolve through this chain so the active workspace's identity is consistent:

1. **vct-hub resolved values** (highest precedence). When the hub is running on `http://127.0.0.1:7700` (port configurable via `VCT_HUB_PORT`), MCP startup queries `GET /api/v1/projects/{id}/config` and uses the hub's resolved per-project record from `launcher.db`.
2. **`.claude/settings.json` `env` block**. The canonical per-project channel — written by the launcher's env projection (`python -m vco_lib.config_projection apply`), read by every Claude Code surface (CLI, Desktop app, VS Code extension) and propagated to MCP subprocesses. (`.vscode/settings.json` `claude-code.env` is NOT part of this chain — that surface does not propagate to MCP subprocesses on Linux.)
3. **`.claude/env`** (POSIX shell-sourceable). Same keys as #2; used by CLI users sourcing it from a shell rc via the `tools/claude` wrapper.
4. **`~/.claude.json` `mcpServers.<name>.env`**. The launcher intentionally restricts this surface to machine-invariant keys (e.g. `WEAVIATE_URL`); per-project keys like `KG_COLLECTION` are dropped here. See `launcher/src-tauri/src/mcp_registration.rs::ALLOWED_ENV_KEYS`.
5. **Bundled defaults** baked into `claude_mcp_servers/weaviate_mcp/server.py` (lowest precedence). Reaching this layer is logged at WARNING level. Explicit empty-string env values for `KG_COLLECTION` are coerced to the default rather than used literally.

The MCP startup log emits a `weaviate-kg: resolved collections (...)` line showing what the subprocess actually picked up plus the resolution source (env / hub / default); this is the diagnostic to grep for when a project is silently using the wrong KG.

### Which Weaviate, vs which collection — two different scopes

The chain above resolves **per-project** values. The Weaviate *instance* is not one of them, and conflating the two is the mistake this section exists to prevent:

| Concern | Scope | Resolved by |
|---|---|---|
| **Collection** — `KG_COLLECTION`, `SHARED_KG_COLLECTION`, `DEVELOPMENT_COLLECTION`, the code-graph prefix | **per-project** | the 5-level chain above (hub → `.claude/settings.json` → `.claude/env` → `~/.claude.json` → bundled default) |
| **Instance** — which Weaviate server is addressed at all | **machine-global** | the `service_endpoints` row in `launcher.db` (v0.2.97), projected into every project as `WEAVIATE_URL` + `WEAVIATE_PORT` |

There is deliberately no per-project instance override. `project_kg_bindings` carries a `weaviate_url` column, but no resolver reads it — it is preserved across writes and nothing more (stated in source at `launcher/src-tauri/src/commands/binding_reconcile.rs`). Two projects on one machine are isolated by **collection namespace**, not by separate servers.

**Precedence, and why it is not "env overriding the database".** `WEAVIATE_URL` wins over `WEAVIATE_PORT` even when their ports disagree — a full URL names scheme, host *and* port, so rewriting its port from a bare port variable would make `WEAVIATE_URL` unable to mean what it says. At either level, empty or whitespace-only is treated as **unset**, not as a literal (a `.env` written on Windows and sourced on Linux carries a trailing `\r`). A non-numeric `WEAVIATE_PORT` is interpolated anyway rather than discarded, so a typo fails loudly at connect instead of quietly resolving back to 8081 and addressing whatever else is on the canonical port. Reading these variables does not compete with the launcher's database: `vco_lib/config_projection.py` writes the row-resolved endpoint *out* as `WEAVIATE_URL` **and** `WEAVIATE_PORT` together, so they are the transport of that value, one hop later.

**Before v0.2.96 the port variable was declared but unread** by everything except `install.py`'s own hand-written copies. On a relocated Weaviate that meant the rest of the install addressed `localhost:8081` — the *other* instance — and on the retrieval path the symptom was not an error but an empty result set. Since v0.2.96 the precedence lives in one home (`vco_lib/weaviate_helpers.py::weaviate_url_default`) that every Python call-site reaches; the MCP server keeps a deliberate copy because it must boot on half-installed environments where importing `vco_lib` fails, and a parity test executes that copy against the shared helper so the two cannot drift.

**Where the written value comes from (v0.2.97).** The value the projection writes, and the ones the hub's `/config` and the MCP registration serve, come from ONE place: the `service_endpoints` table in `launcher.db` (migration 047). One row per core service (`weaviate`, `ollama`, `code_embed`) records the **mode** (`vco_managed` — VCO's compose owns the container; `adopted_container` — someone else's container, which VCO starts and stops **by name only** and never removes or recreates; `adopted_external` — a URL, native process or remote host, over which VCO has no lifecycle), the scheme/host/port (plus `grpc_port` for Weaviate), the container identity and the data-mount identity. `vco_lib/service_endpoints.py` is the only writer; the hub, the launcher, the projection and the MCP registration read the rows (Rust side: `vct-launcher-core/src/db/service_endpoints.rs` + `services/service_endpoints.rs`; the ~20-line row→URL render is the one cross-language mirror, pinned by `tests/fixtures/service_endpoint_parity.json`). When no row exists — first boot before install finishes, or a broken install — the render answers the compiled defaults (Weaviate 8081/50052, Ollama 11435, code-embed 11440) and logs a WARNING once. `WEAVIATE_URL` is deliberately not an input — it is the resolver's output, and reading it back would re-project a stale value.

The inputs this chain replaced are **retired**: `VCT_WEAVIATE_URL` / `VCT_OLLAMA_URL` / `VCT_GRPC_PORT`, the `weaviate_url` key in `vct-config.toml`, `~/.vct/services.toml`, and the `*.port_override` launcher settings were imported into the rows once on the first v0.2.97 update and are no longer read by any resolver (`services.toml` is renamed `services.toml.migrated-v0297`, never deleted). `vco doctor` warns while a retired variable is still exported. To point every surface at a different endpoint, change the row — the launcher's Services page ("Change…"), `python -m vco_lib.service_endpoints adopt|use-vco-copy|move`, or `install.py --service <svc>=adopt:container:<name>|adopt:url:<url>|vco[:<port>]` — and every registered project is re-projected and the MCP registration refreshed as part of the same change. Running two orchestrator installs with genuinely separate stacks is `VCT_FORCE_SEPARATE_CONTAINERS=1` plus per-install ports chosen through `--service <svc>=vco:<port>`; see [`features/05-install-and-secrets.md`](features/05-install-and-secrets.md).

| Var | Default | Effect |
|---|---|---|
| `WEAVIATE_URL` | unset (projected) | Complete base URL of the Weaviate server, used verbatim. Since v0.2.97 this is an **output**: the projection writes it (with `WEAVIATE_PORT`) into every registered project from the machine's `service_endpoints` row. In a project process, a value you set yourself still wins for the clients started from that shell. |
| `WEAVIATE_PORT` | `8081` | Port on `localhost`, used only when `WEAVIATE_URL` is unset — the client-side floor of the projected pair, read by every Python call-site since v0.2.96 (`vco_lib/weaviate_helpers.py::weaviate_url_default`). |
| `VCT_WEAVIATE_URL` | unset | **Retired in v0.2.97.** A legacy machine-level Weaviate statement: its value was imported into the `service_endpoints` row on the first v0.2.97 update and nothing reads it since. `vco doctor` warns while it is still exported. |
| `OLLAMA_URL` | `http://localhost:11435` | Ollama base URL (embeddings + the local KG-summary tier). Projected output of the Ollama row. |
| `OLLAMA_PORT` | `11435` | Host port for the Ollama container. Written from the row into `infrastructure/.env` by `vco_lib/compose_env.py` — not a user knob; move Ollama with `python -m vco_lib.service_endpoints move`. |
| `CODE_EMBED_SERVICE_URL` | `http://localhost:11440` | Code-embedding service base URL (see [Embedding configuration](#embedding-configuration)). Projected output of the code_embed row; `CODE_EMBED_URL` is an accepted alias. |
| `CODE_EMBED_PORT` | `11440` | Host port for the code-embed container, written from the row into `infrastructure/.env`. Same not-a-user-knob rule as `OLLAMA_PORT`. |
| `WEAVIATE_GRPC_PORT` | `50052` | Weaviate gRPC port used by the `weaviate-kg` MCP client and published by the compose file; projected from the row's `grpc_port`. `GRPC_PORT` is the legacy `.claude/settings.json` spelling and still works; the `WEAVIATE_`-prefixed name is canonical. |

### A project's own Weaviate / Ollama — infrastructure declarations (v0.2.102)

A project may run its OWN Weaviate or Ollama for its own runtime (a second Weaviate on `8082`, an Ollama on `11436`) while VCO keeps its development KG on the machine's Weaviate above — the separation is by design, since VCO has no per-project instance override. What VCO needs is to never mistake the project's pieces for its own, so the project declares them on the launcher's Identity tab (**Infrastructure this project owns**), stored in `launcher.db` `project_infra_declarations` (migration 049) and read by one module, `vco_lib/infra_declarations.py` (Rust: `vct-launcher-core/src/db/infra_declarations.rs`):

| Declaration | Projected env key | Honoured by |
|---|---|---|
| External collections | `VCT_EXTERNAL_COLLECTIONS` | the doctor's binding-evidence scan: never unclaimed, never fixture-shaped, never evidence for a binding repoint; named as skipped with its declarer |
| Unmanaged containers | `VCT_UNMANAGED_CONTAINERS` | the hub watchdog (not even inspected), compose-recovery heals (refused), service detection (never an adoption candidate, its ports never taken), the doctor (no rebuild printed for it) |
| Local ports | `VCT_SSRF_EXTRA_LOCAL_PORTS` | the WebFetch SSRF guard (allowed for this project, loopback only), service detection (never probed) |

Each key is comma-separated and emitted only when the project declares something of that kind (removing the last declaration removes the key). The keys are the projection the Python readers see (and, exported in the shell, the only channel of a launcher-less install); the hub watchdog reads `launcher.db` itself. The hub's `/config` serves the same three lists (`external_collections`, `unmanaged_containers`, `ssrf_extra_local_ports`); `vct_project_config.{sh,ps1} --field <name>` prints one value per line. Containers and collections are protected machine-wide whichever project declared them; the SSRF allowance is per-project. A launcher.db that exists but cannot be read makes every heal of a VCO container refuse.

## Knowledge graph env vars

The MCP server (`claude_mcp_servers/weaviate_mcp/server.py`) reads these on startup, resolved through the 5-level chain above. The launcher's env projection (`python -m vco_lib.config_projection apply`) writes the canonical per-project values into both `.claude/env` (POSIX shell-sourceable) and `.claude/settings.json::env` (the channel that actually propagates to MCP subprocesses on Linux).

| Var | Default | What it does |
|---|---|---|
| `KG_COLLECTION` | `<ProjectName>` | Per-project Weaviate collection. Knowledge nodes from `knowledge/` land here. |
| `DEVELOPMENT_COLLECTION` | `<ProjectName>_development` | Per-project Weaviate collection for `docs/`. Auto-paired with KG by the launcher. Same chunker + named-vector slot logic as KG. |
| `SHARED_KG_COLLECTION` | `VibeCodedOrchestrator_KnowledgeGraph` | Cross-project shared KG. All projects on this machine query it alongside their own KG. Seeded at install from the orchestrator root's `knowledge/` (the bundled curated set materializes once at the root; non-root projects read it via the shared-KG fan-out). Users migrating from an install whose shared collection has a different name can designate that collection as canonical via the launcher's Identity tab "Manage shared KG collection" picker. |
| `SHARED_KG_READ_DISABLED` | `false` | Per-project READ gate. Set to `true` (or `1`/`yes`) to exclude the shared collection from `hybrid_search` / `semantic_graph_search` results on this project — reads fall back to the per-project KG only. No legacy alias. |
| `SHARED_KG_WRITE_DISABLED` | `false` | Per-project WRITE gate. Set to `true` (or `1`/`yes`) to refuse `store_knowledge_node(scope="shared")` calls from this project. |
| `SHARED_KG_OPT_OUT` | `false` | Legacy alias of `SHARED_KG_WRITE_DISABLED` (write gate only — it never affects reads). Kept for back-compat; the canonical key wins when both are set. |
| `SHARED_KG_NODE_FORMATS` | (unset) | Override path for the shared KG's `.node_formats.json` sidecar. Used by tests; in production the sidecar is read from `<orchestrator>/knowledge/.node_formats.json` via `_SERVER_INFERRED_BASE`. |
| `KG_TIER_MIN` / `KG_TIER_SINGLE_CHUNK` / `KG_TIER_THREE_CHUNKS` / `KG_TIER_FULL` | `0.42` / `0.55` / `0.65` / `0.75` | Score thresholds for the auto-tier retrieval system. See `knowledge/concepts/score-driven-retrieval-tiers.md`. |

**Gate semantics**: the two gates are symmetric and independent. `SHARED_KG_WRITE_DISABLED=true` refuses `store_knowledge_node(scope="shared")` calls with a clear error — NOT a silent reroute to the project KG. `SHARED_KG_READ_DISABLED=true` excludes the shared collection from `hybrid_search` / `semantic_graph_search` on this project. With both unset (the default), every project reads AND can write the shared collection. To fully sever a project from the shared KG, set both to `true`.

**Power-user override**: point `SHARED_KG_COLLECTION` at a private team-shared collection (e.g. `AcmeTeam_SharedKG`) to share knowledge across an internal team without exposing it via the public bundled name.

## Embedding configuration

The `EmbeddingService` (in `vco_lib/embedding_service.py`) is the unified entry point for KG + code-graph embeddings. Configuration is purely env-driven; the launcher writes the resolved values into `.claude/settings.json::env`.

| Var | Values | What it does |
|---|---|---|
| `ACTIVE_EMBEDDING` | `qwen3` (default) | `openai` | Selects the active text-embedding slot for KG + development collections. `qwen3` → `qwen3_embed` named vector; `openai` → `openai_embed`. Projected by the launcher from the ONE global embedding profile (Preferences → Embedding profile) into every project — not chosen per project (v0.2.102). |
| `EMBEDDING_MODEL` | `qwen3-embedding:0.6b` (default) | model id | Explicit text model override. When `ACTIVE_EMBEDDING=openai` and this is unset, defaults to `text-embedding-3-small`. |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` (default) | OpenAI model id | Used only when `ACTIVE_EMBEDDING=openai` and `EMBEDDING_MODEL` is unset. |
| `OPENAI_API_KEY` | (unset) | API key | **VCO does not read this** (v0.2.98). The row stays because the name still matters elsewhere — env-scrubbing hooks strip it from spawned subprocesses, and third-party tooling uses it — but no VCO consumer takes its OpenAI key from it. The key VCO embeds with is resolved per process by `vco_lib.openai_key` from VCO's own shared slot `openai_api_key` (launcher keychain → `~/.vct-secrets/shared/`); a per-project binding under that name and the consuming project's own `.env` are not consulted either, so a project that exports its own key never has it spent on VCO. Required when `ACTIVE_EMBEDDING=openai`. `install.py --openai-key` stores it in the slot — never in `.env`. |
| `OLLAMA_URL` | `http://localhost:11435` | URL | Ollama base URL used by the qwen3 slot. |
| `CODE_EMBED_SERVICE_URL` | `http://localhost:11440` | URL | Code-embedding FastAPI service URL. |
| `CODE_EMBED_BACKEND` | `gpu` (default) | `ollama` | `gpu` → CodeSage-Large-v2 via the FastAPI service (sentence-transformers); `ollama` → routes embeds through Ollama. The Ollama-path default model is `unclemusclez/jina-embeddings-v2-base-code:latest` (768-dim); install.py overrides `CODE_EMBED_MODEL` to `qwen3-embedding:0.6b` (1024-dim) on 6-12 GB GPU hosts. |
| `CODE_EMBED_MODEL` | `codesage-large-v2` (default) | model id | The operator's explicit code-model selection — the top rung of `vco_lib/code_embed_model.py` (then the project's Codegraph-tab selection, the Preferences → Code Graph Embeddings pin, the install default `default_code_embedding`, and `CODE_EMBED_BACKEND` as a last-resort default only — the container selector never outranks a selection). Applied automatically and additively: the next code-graph run adds the new named vector and embeds only the rows without it; searches keep the RECORDED model (`project_codegraph_bindings.embedding_model`) until the new slot is complete. An OpenAI model needs the embeddings consent (Preferences → OpenAI API key). |
| `CODE_EMBED_IN_STACK` | written by install (`infrastructure/.env`) | `true` / `false` | Whether the `code_embed` container is part of this install: `true` only on a GPU host where a code model in use (the install pick, a `CODE_EMBED_MODEL` override, the Preferences pin / default, a code graph's recorded model or fill target) is CodeSage — the one model that container serves; jina / qwen3 graphs are embedded by Ollama and OpenAI ones by OpenAI. Install's compose start, the session hook / boot wrapper plan and the hub watchdog all follow it (absent = an install from before v0.2.102: the old rule). A selection change (Preferences pin, "Default code embedding", a project's Codegraph-tab target) needs no re-install: the hub watchdog notices it on its next tick, recomputes the marker through the same decision, and creates the container — or, after deselecting, stops supervising it (the container and its volumes are never removed). `CODE_EMBED_HOST_GPU` (also written by install) keeps that decision on install's GPU verdict. |
| `VCT_CODE_EMBED_BUILD_CONTEXT` | `<orchestrator>/claude_mcp_servers/code_embedding_service` | path | Build context for the `code_embed` image, which is the ONE VCO service BUILT from source rather than pulled. Set it when the compose file runs outside the orchestrator clone (a per-project layout does not bundle the service source). It is also where `vco doctor` and `python -m vco_lib.code_embed_image` look for the source they compare against the running service's `/health.source_sha`; point it at the same directory compose builds from, or the staleness check reports `unknown`. |
| `DUAL_EMBEDDING_ENABLED` | `false` (default) | `true` | When true, both `qwen3_embed` and `openai_embed` slots are populated on every write so the active slot can be switched without re-indexing. |
| `VCT_EMBED_REQUEST_TIMEOUT_SECS` | `660` (default) | seconds | Per-embed-REQUEST timeout inside every backend adapter (Ollama / CodeEmbed / OpenAI). Bounds a single chunk request — a wedged embedder (hung socket, dead container) fails at chunk granularity instead of hanging forever, while a slow-but-progressing run is never killed. Unset, empty, non-numeric or non-positive → the default (the guard is never disabled). The default MUST exceed the code-embed server's queue wait (`CODE_EMBED_QUEUE_WAIT_SECS`, 600 s): the service QUEUES over-capacity requests instead of shedding them, and a shorter client deadline would abort a request the server is still legitimately holding (one home for the floor: `vco_lib/retry_after.py::CODE_EMBED_CLIENT_TIMEOUT_SECS`). `install.py` threads an export into its KG-seed subprocess, so install-time seeding honours it too. |
| `VCO_EMBED_503_RETRY_DELAY` | (unset) | seconds | Scales every computed embed 503-retry delay — including a server-provided `Retry-After`. The value becomes the FIRST schedule delay (base 2 s / 5 s / 10 s, each +jitter) and the rest scale proportionally — `1` compresses the whole schedule. `0` selects the LEGACY bounded mode: exactly 3 retries, zero delays, `Retry-After` ignored (test harnesses). A malformed value is ignored and delays run unscaled (a knob must not be a kill switch). |
| `VCO_EMBED_503_TOLERANCE_SECS` | `420` (default) | seconds | Total wall time the client 503-retry layers (`EmbeddingService._retry_once_on_503` and the weaviate-MCP inline fallback, one shared policy in `vco_lib/retry_after.py`) keep honouring 503 + `Retry-After` before letting the failure through to the caller's fail-safe. Must be ≥ the server's CUDA-OOM load budget (`CODE_EMBED_LOAD_OOM_RETRIES` × `CODE_EMBED_LOAD_OOM_BACKOFF_SECS`, default 360 s) so a retrying caller rides out one full server-side recovery. `0` = fail-fast (no retries). |
| `VCO_EMBED_503_INTERACTIVE_TOLERANCE_SECS` | `90` (default) | seconds | The same 503-retry policy as seen from an INTERACTIVE call site — the weaviate-MCP query-embedding path a `search_code_graph` / `query_code_structure` tool call blocks on, and the `code-graph-query` CLI the context-injection hooks run. Batch surfaces (analyzer, KG sync, re-embed, backfill gather) keep the full `VCO_EMBED_503_TOLERANCE_SECS`; the interactive path gives up at `min(this knob, VCO_EMBED_503_TOLERANCE_SECS)` so a persistently-503 service fails the tool call fast (the CLI-visible "Failed to generate query embedding") instead of silently blocking the turn for minutes. Once spent, the query path does NOT run the inline CodeEmbed fallback a second time — that fallback is only for the service being unreachable/not configured (connection refused), never a second full retry after 503 tolerance exhaustion. Malformed/negative → the default; `0` = fail-fast. |
| `VCO_EMBED_INTERACTIVE_REFUSED_TOLERANCE_SECS` | `10` (default) | seconds | How long an INTERACTIVE call site (the MCP query-embedding path, the `code-graph-query` CLI) keeps retrying when the code-embed service REFUSES the connection (no listener — the service is down or restarting). A 503 means the request was queued and is waited on up to `VCO_EMBED_503_INTERACTIVE_TOLERANCE_SECS`; a refusal gets only this short bound — long enough to ride out a container restart — then fails loudly. Batch surfaces treat a refusal like a 503 and keep `VCO_EMBED_503_TOLERANCE_SECS`. Clamped to the 503 tolerance in force; malformed/negative → the default; `0` = fail-fast. One home: `vco_lib.retry_after.resolve_refused_tolerance`. |
| `CODE_EMBED_QUEUE_WAIT_SECS` | `600` (default) | seconds | Server-side: how long a request beyond `CODE_EMBED_MAX_CONCURRENT` waits in the FIFO queue for a slot before it is refused with 503 + `Retry-After`. Owner rule: concurrent requests QUEUE, none are dropped — this bound is the only refusal. `0` = do not queue (refuse over-capacity requests immediately, still with `Retry-After`). Client request timeouts must exceed it (bundled clients: 660 s). |
| `CODE_EMBED_IDLE_UNLOAD_SECS` | `300` (default) | seconds; `0` keeps resident | Server-side: idle window after which the code-embed service unloads the GPU model (~7.25 GB CodeSage VRAM), measured + proven freed (gc.collect → empty_cache → freed-bytes check); the next request lazy-reloads. If the free does not actually happen (a live reference pins the weights) the model is KEPT loaded with a WARNING — never an un-freed copy plus a second one. |
| `CODE_EMBED_LOAD_OOM_RETRIES` / `CODE_EMBED_LOAD_OOM_BACKOFF_SECS` | `12` / `30` (defaults) | count / seconds | Server-side: model-load CUDA-OOM retry budget (retries × backoff ≈ 6 min total — VRAM held by another process, e.g. Ollama's 5-min keep-alive, releases on a minutes scale). Exhausting it answers 503 + `Retry-After` and starts a short fail-fast cooldown in which queued loaders share the failed outcome instead of each re-running the budget. |
| `CODE_EMBED_UNLOAD_FREED_MIN_MB` | `64` (default) | MB | Server-side: VRAM floor below which an idle-unload counts as successful without the freed-ratio check (a cpu/mps model or a mock holds no real VRAM). Above the floor, an unload that frees less than half the held VRAM keeps the model loaded (see `CODE_EMBED_IDLE_UNLOAD_SECS`). |
| `VCO_OLLAMA_KEEP_ALIVE` | `10m` (default) | Ollama duration | `keep_alive` attached to every Ollama request VCO sends (embeds and generate calls) so consecutive calls share the resident model without paying the ~1.9 s reload per idle gap. `24h` before v0.2.102 — a live measurement showed one KG search pinning 8.3 GB of VRAM for a day (qwen3-embedding 2.4 GB + arctic 1.15 GB, both `expires_at` +24 h); `10m` releases idle VRAM while a session's back-to-back calls still hit the resident copy. Any value Ollama accepts works: `30m`, `2h`, `-1` (never evict), `0` (opt back into immediate unload). An explicitly EMPTY value sends no `keep_alive` field at all, deferring to Ollama's server-side `OLLAMA_KEEP_ALIVE`. Host-side callers read it per process. The code-embed container's body template is resolved at install/update time: a value set in the install's environment is persisted to `infrastructure/.env` as `CODE_EMBED_OLLAMA_KEEP_ALIVE`, and a later install/update run without the variable reuses that persisted value (the `10m` default applies only when neither exists). To change the container's value, set the variable again and run the update; to return to the default, set it to `10m`, or delete the `CODE_EMBED_OLLAMA_KEEP_ALIVE` line and update. |
| `VCO_OLLAMA_GENERATE_NUM_CTX` | `32768` (default) | positive integer (tokens) | The context window every VCO Ollama GENERATE request asks for (KG summaries, node formats, document summaries) — one value for every caller, set in `vco_lib/ollama_generate.py`, so a generation model is never loaded twice with different windows. A blank or invalid value is ignored with a stderr note. Supersedes a `num_ctx` inside `KG_SUMMARY_OLLAMA_OPTIONS`. |
| `OLLAMA_KEEP_ALIVE` (compose) | `10m` (shipped) | Ollama duration | Server-side backstop in the shipped compose files, for Ollama requests that carry no request-level `keep_alive`. Changing it requires recreating the `ollama` container (see the v0.2.102 CHANGELOG entry). |
| `OLLAMA_MAX_QUEUE` (compose) | `1000` (shipped) | requests | Ollama's request queue cap — beyond it, Ollama answers 503 (`server busy`). All projects on the machine share ONE copy of each model and QUEUE on it (owner rule: never drop), so the cap must stay well above the default 512 — a kg-sync on a big project batches hundreds of embeds. **Owner rule: never set below 1000.** Ollama's other concurrency defaults (`OLLAMA_NUM_PARALLEL`, `OLLAMA_MAX_LOADED_MODELS`) are left at the daemon defaults — do not raise `OLLAMA_NUM_PARALLEL` on VRAM-constrained machines: each parallel slot multiplies the KV cache. VCO clients treat a 503 as retryable with backoff (`vco_lib/retry_after.py`). Takes effect when the container is recreated. |

**Multi-slot fallback chain**: when `EmbeddingService.for_project()` resolves to a slot whose backend is unreachable (e.g. `codesage_embed` selected but the FastAPI service is down), it walks a fallback chain in order: codesage → qwen3 (via Ollama) → openai (when key set). The chain only fires for the code slot; the text slot resolution is strict. Diagnostic logging lands at `WARNING` level — check the MCP stderr if you suspect a fallback fired silently.

## Secrets

Secrets never live in env files or JSON configs. They live in the OS keychain (macOS Keychain, Linux Secret Service, Windows Credential Manager) and are written by the launcher's GUI or the OnboardingWizard.

**Shared bucket** (visible to every base-host project on this user account): declared in `vct-module.json::bundled_secrets[]`. The hub's `/api/v1/projects/{id}/env` resolver finds these via SENTINEL_SHARED + `module_id=user`.

| Slot | Written by | Consumed by |
|---|---|---|
| `github_pat` | OnboardingWizard `register_github_pat` step OR Preferences → Secrets → Shared (this user) | bundled hooks that need to talk to GitHub, and the `git-credential-vct` helper |
| `openai_api_key` | OnboardingWizard OpenAI step OR Preferences → Secrets | `vco_lib/embedding_service.py` when `ACTIVE_EMBEDDING=openai` or as multi-slot fallback. Validated via `GET /v1/models/text-embedding-3-small` — no token consumption, no billing entry. |

**Per-module / per-project secrets**: paid modules declare their own `bundled_secrets[]` in their manifest; the launcher's SecretsPanel surfaces a tab per scope. Per-project license keys, API tokens, and module-specific secrets are scoped by `project_id` and never leak across projects.

**Resolver flow** (subprocess perspective):

1. A consuming process (the `git-credential-vct` helper, or a bundled hook that talks to GitHub) runs.
2. It checks `$GITHUB_TOKEN` — if already exported in its environment (by you, or by `vct exec --secret github_pat=GITHUB_TOKEN`), use it directly. The launcher never writes secret values into project files (v0.2.73), so this is not populated for you.
3. Otherwise call `vct_secrets_resolve.sh <project_path> github_pat` → hub HTTP API at `GET /api/v1/projects/{id}/env?key=github_pat`.
4. Hub resolves via SENTINEL_SHARED + `module_id=user`, applies the cross-launcher active-flag gate, returns the secret.
5. The process uses the value and discards it.

(Until v0.2.101 the `search` MCP's `wrapper.sh` was also a consumer; it was deleted with the MCP.)

Don't put PATs or API keys in `~/.claude.json` `env:` blocks — Claude Code's env loader does not expand `${VAR}` (anthropics/claude-code#2065, #4276), so embedded secrets would land in argv and become visible to `ps`.


### Credential scan of your edits (`post-tool-security`)

After every Edit / Write, the `post-tool-security` hook scans the text that call **introduced** for credential shapes (GitHub / GitLab / Slack / AWS / Atlassian / vendor `sk-…` keys, JWTs, PEM private keys, generic secret assignments — the `content_scan` context of `vco_lib/credential_shapes.py`):

- **Generic secret assignments**: a key ending in `SECRET`, `API_KEY`, `ACCESS_TOKEN`, `PRIVATE_KEY`, `PASSWORD`, `PASSWD`, `PWD` or `PASSPHRASE`, in any letter case (`DB_PASSWORD`, `db.password`, `client_secret` and `mysql_pwd` count). It must END there, so `password_hint` does not count, and it must not itself be a `$` reference: in `"$PWD:/app"` or `PYTHONPATH=$PWD:/x`, `PWD` is a variable being expanded, not a key.
  - **Quoted literals, in every file, of any length**: a password such as `hunter2`, a port such as `5432` or an address such as `10.0.0.1` is reported, in these forms:
    - `KEY = "…"`, `KEY: "…"`, `KEY := "…"`;
    - a quoted key, as in JSON or a dict: `"KEY": "…"`;
    - a type annotation `KEY: TYPE = "…"`, where TYPE is a name or dotted / `::` path with an optional leading `&` and Rust lifetime and one `<…>` or `[…]` suffix (`KEY: str`, `KEY: Optional[str]`, `const KEY: &str`, `let KEY: &'static str`, `KEY: Option<&str>`, TypeScript `const KEY: string`), including a TypeScript optional member `KEY?: string = '…'`;
    - Go's `var KEY TYPE = "…"` (any `KEY NAME = "…"`);
    - single, double or triple quotes, optionally prefixed `r`, `b`, `u` or `f` (up to two letters);
    - a backtick literal without `${`, only in files whose language has backtick string literals: `.js`, `.jsx`, `.mjs`, `.cjs`, `.ts`, `.tsx`, `.mts`, `.cts`, `.vue`, `.svelte` and `.go` (reported as "Generic secret (backtick literal)"). Elsewhere a backtick is shell command substitution or markdown inline code, and is not reported.
  - **Unquoted values, by file kind** (the kind comes from the file name only — `vco_lib/credential_shapes.py`, `file_kind`):
    - in an env/config-style file — `.env`, `.env.*`, `*.env`, `*.env.*`, `.envrc`, `.ini`, `.cnf`, `.properties`, `.conf`, `.cfg`, `.sh`, `.bash`, `.zsh`, the shell rc files (`.bashrc`, `.bash_profile`, `.zshrc`, `.zprofile`, `.zshenv`, `.profile`), `.pypirc`, `.npmrc`, `Dockerfile`, `Dockerfile.*`, `*.dockerfile`, `Containerfile` and `Containerfile.*` (also the legacy `ENV KEY value` form) — any non-empty unquoted value is a literal and is reported, also after a quoted key (`"KEY"=…`);
    - in a YAML file (`.yaml`, `.yml`) the same ("Generic secret (unquoted, YAML)"), except YAML's own value syntax: `null` (any case), `~`, the boolean words `yes` / `no` / `true` / `false` / `on` / `off` (any case, the YAML 1.1 words real loaders resolve; the single letters `y` / `n` are not booleans in YAML 1.2 and stay reported), an `*alias`, a flow `{…}` / `[…]` and a `{{ template }}` are not reported. Such a word counts only when it is the whole value or is followed by a space (`password: no # disabled` is silent, `password: nope` and `password: offset` are reported). A block scalar (`|`, `>`, with `-` / `+`) IS reported, because its literal follows on the next line, and so is an anchored value (`&a …`). In a `.env` file `null` and `no` are just literals;
    - in every other file an unquoted value is an expression and is not matched.
  - **Not reported, because they are syntax rather than a value**: a `$` reference (`$X`, `${X}`, `$(...)`) or backtick command substitution as an unquoted value; a double-quoted value, or an `f`-prefixed single-quoted one, that starts with `$` or `{` (`"${X}"`, `f"{x}"`, `f'{x}'`); a backtick literal containing `${`; a call or expression; `==` / `!=` comparisons; a comment (`#` after whitespace); an empty value. Only the opening character of a quoted value is read, so `"pre$X"` is reported while `"${X}suffix"` is not. A plain single-quoted `'$x'` is a literal and is reported.
  - Nothing about the value itself is judged: a `<your-password>` placeholder is reported like any other literal, and the marker below is the only way to silence a line.
- Only the content scanners use the generic shape. The Bash / PowerShell command scanner (which decides whether a command's output may be compressed) and `scripts/check-no-secrets.sh` (a whole-tree pass that includes vendored text) do not: an ordinary `mysql --password=...` command must not change behaviour, and a whole-tree pass needs the precise shapes only.

- **Edit**: the full lines the new text occupies in the file afterwards (a credential already on a line you changed is reported too); **Write**: the whole content written. A credential that was already in the file, on a line you did not touch, is not reported again. Files written by Bash commands are not covered (the hook is registered for Edit/Write).
- A finding names the **project-relative path, the line number(s) and the shape** — never the value — in the desktop notification, in the `[Security alert]` note to the model, and in the ledger `.claude/logs/credential_alerts.jsonl` (one row per event: `file`, `patterns`, `findings: [{line, id, label}]`). The desktop notification is deduped for 6 h per identical finding; the ledger is not.
- **Silencing a genuine test fixture**: put the marker `vco-secret-scan: test-placeholder` on the **same line** as the value, normally in a comment:

  ```rust
  let fixture = "ghp_<36 characters>"; // vco-secret-scan: test-placeholder
  ```

  That is the only opt-out. Nothing is inferred from how a value looks (a real password can look like `123456789`) or from how short it is — every `-----BEGIN … PRIVATE KEY-----` line is reported, whatever body follows it — and the marker covers its own line only. Every VCO credential scanner honours it the same way: this hook, the SubagentStop reconciler's scan of the files a subagent changed (`_lib/credscan.sh` / `.ps1`) and the repo scanner `scripts/check-no-secrets.sh` (its credential-shape passes; its list of known-leaked token prefixes is not a shape scan and ignores the marker, because a known leak is never a placeholder). The pre-tag privacy check (private names and paths, not credentials) does not read it. Formats without comments (JSON fixtures) cannot carry it; such a line alerts only when an edit introduces or changes it.
- If the scanner cannot run (its vocabulary `_lib/credshapes.*` or its helper `.claude/scripts/credscan_introduced.py` is missing, or no Python is found by the `.ps1`), it raises a `credential scanner UNAVAILABLE` alert on every edit instead of passing silently. Re-run the bundle update to repair the install.

## Permission matrices

The launcher enforces three independent cross-project access matrices, each backed by its own table in `launcher.db`. They do NOT share a default: **KG is default-GRANT**, the other two are **default-DENY**. Inspecting `launcher.db` (or reading Rust accessors) without this table in front of you invites the wrong assumption that everything is default-deny. The defaults are set on project add in `launcher/src-tauri/src/commands/project_state_populate.rs`.

| Matrix | Table (key) | Default on project add | How to grant more |
|---|---|---|---|
| **KG access** | `kg_collection_access` (project_id, collection_name) | **GRANT** — the project's OWN KG + the machine-shared KG get read rows automatically | Cross-project KG reads are explicit-grant (launcher Identity tab → KG access matrix) |
| **Code-graph access** | `codegraph_access` (grantor_project_id, grantee_project_id) | **DENY** (empty) | Launcher Codegraph → Cross-Project Access tab |
| **Secrets** | per-`(scope, key, requester)` active flags + grants | **DENY** for cross-project | Shared-scope secrets (Preferences → Secrets) are readable by requesters unless paused per-project; per-project secrets are project-only unless explicitly shared via the SecretsPanel |

**Why KG grants by default but code-graph denies.** The KG read gate rejects any collection without an explicit row — so without the auto-grant, a fresh project's searches against its own KG would fail on day one. The default-grant seeds the project's own KG plus the machine-shared KG (knowledge is intentionally cross-project value: a pattern learned in one project is usually useful in the next). Source code is the opposite: it is proprietary and per-tenant, so code-graph stays empty until you deliberately grant one project read access to another's. Secrets follow the same default-deny posture — cross-project reads require an explicit grant, and even a shared-scope secret can be paused for a specific requester. See the KG-vs-code-graph asymmetry table in the project `CLAUDE.md` for the read-fan-out consequences of this design. (The former `diagram_access` matrix followed the same default-deny shape; its grant surface was removed with the diagrams integration in v0.2.102, and the table itself is left in place on existing installs.)

## vct-hub

A detached local HTTP server (port 7700 default) that serves as the single source of truth for project config + secrets resolution. Lives in `launcher/dist/<arch>/vct-hub`. Outlives the launcher GUI: close the GUI, the hub keeps running so hooks / MCPs / shell scripts still resolve config.

| Var / path | Default | What it does |
|---|---|---|
| `VCT_VENV` env | (unset) | Explicit venv override — tier 1 of every venv-resolution ladder (`vco_lib/python_exe.py::ladder_candidates`; shipped wrapper `templates/scripts/vct_venv_ladder.sh` / `.ps1`). Accepts a venv DIRECTORY or the interpreter binary itself; with it unset the ladder falls back `.claude/env` → the orchestrator install's `.venv` → a VCO clone, refusing loudly when no candidate works. Read by the dependency-gated shell wrappers to hand their Python backend a known-good interpreter. |
| `VCT_HUB_PORT` env | `7700` | Hub port override. Falls back to `<vct_root_dir>/hub.port` (written on startup), then `7700`. The hub ITSELF binds, in order: its own `VCT_HUB_PORT` env → the `vct-hub-api` module's global `VCT_HUB_PORT` setting (launcher.db; 1024–65535) → `7700`. |
| `VCT_HUB_TOKEN` env | (unset) | Hub auth token override (tests / dev). Production reads from `<vct_root_dir>/hub.token`. The pin wins on every FIRST attempt; since v0.2.91, a request the hub PROVABLY refuses (401/403) is retried ONCE with the on-disk token when the two differ — see the stale-token note below. |
| `VCT_HUB_TOKEN_STRICT` env | (unset) | Set to `1` to DISABLE that one-shot fallback, so a `VCT_HUB_TOKEN` pin is authoritative even when the hub refuses it. For tests / harnesses that pin a deliberately-wrong token and assert the 401 path. |
| `VCT_HUB_MODULE_HEALTH` env | (unset = on) | The hub's module health poller — it probes each active module's `runtime.health_check` (loopback only) for the status pill on the launcher's module tiles (`docs/VCT_MODULE_MANIFEST_SPEC.md` §6). `0`, `false`, `no` or `off` (any case) in the HUB's environment turns it off; every pill then reads unknown. Read once, when the hub starts. |
| `VCT_HUB_SECRET_CACHE_SECS` env | `30` | Lifetime of the hub's in-process resolved-value cache (`vct-launcher-core/src/secrets.rs`, v0.2.102 R1): a Background `/env` or `?key=` read of a key resolved successfully within the last N seconds skips the OS keychain. A successful `set`/`delete` through THIS process drops the entry immediately; misses and errors are never cached; the active/paused gate always runs first. Cross-process staleness is bounded by the TTL: a change made through the launcher GUI (a different process) reaches the hub after at most N seconds; pausing a key takes effect immediately, because the gate is re-read on every request. `0` disables the cache. Whole seconds; an unparsable value falls back to 30. |
| `VCT_KEYCHAIN_WORKER_RETIRE_AFTER_SECS` env | `600` | After a hung keychain op's worker is abandoned (`vct-launcher-core/src/secrets_worker.rs`, v0.2.102 R2), it is written off the outstanding list once its op has run longer than this — so a permanently hung Secret Service stops answering `WorkerStuck` forever and later ops get a fresh worker. Cap of 2 concurrently abandoned threads; past the cap the honest retryable 503 stays. Must exceed the inner bounds (120 s prompt, 150 s Interactive stuck threshold); default 600 s clears the worst legitimate retry chain (~482 s). Whole seconds, minimum 1; unparsable falls back to 600. |
| `VCT_STATE_DIR` env | `$HOME/.vct` | Root directory for `hub.port`, `hub.token`, `hub.pid`, `cache/`, etc. Resolution: `VCT_STATE_DIR` → `~/.vct/` → relative `./.vct/` last-resort fallback. Setting this lets dev launchers run side-by-side with production without contaminating state. |
| `<vct_root_dir>/hub.token` | — | Bearer token (32 bytes hex, OS CSPRNG). Regenerated on every hub startup, mode `0o600` on Unix. Required on every `/api/v1/*` route except `/api/v1/health` — but the two per-project `/env` + `/config` routes require a project-scoped `hub.token.<project_id>` and refuse this global token unconditionally (the `VCT_HUB_LEGACY_GLOBAL_ENV` opt-in was removed in v0.2.97). Never appears in argv — clients read the file and pass via `Authorization: Bearer ...` header. |
| `<vct_root_dir>/hub.port` | — | Plain integer, the port the hub bound to. Written before `hub.token` so a racing client either sees neither file or both. |
| `<vct_root_dir>/hub.pid` | — | Single-instance lockfile. Contains the running hub's PID. CLI checks it via OS-specific liveness probe (`kill(pid, 0)` on Unix, `OpenProcess` on Windows) + a `TcpListener::bind` probe on the hub port. |

**CLI**:

```bash
vct-hub --start-if-not-running   # idempotent boot; returns 0 even if already running
vct-hub --stop                   # graceful shutdown via lockfile PID
vct-hub --status                 # JSON status (running, port, pid, token-file mode)
vct-hub --foreground             # run in foreground (for dev / supervisor)
vct-hub --register-boot          # install boot autostart (systemd-user / launchd / Win Task)
vct-hub --unregister-boot        # remove boot autostart
vct-hub --boot-status            # check whether boot autostart is registered
```

**Boot autostart** is OS-specific and DEFAULT-OFF. Users opt in via launcher GUI Preferences. Backends:

- Linux: systemd-user unit (`~/.config/systemd/user/vct-hub.service`).
- macOS: `launchd` plist (`~/Library/LaunchAgents/com.vibecodedtools.vct-hub.plist`).
- Windows: Scheduled Task at logon (`VCTHub` task, invoked via a thin `.cmd` shim that points at the binary).

When `VCT_STATE_DIR` is non-default, boot registration prints a warning — the autostart will inherit the user's login env, where a custom `VCT_STATE_DIR` typically isn't set, so the booted hub will write to `~/.vct/` instead of the dev path. This is intentional (dev state shouldn't be auto-launched at login).

Note that this toggle governs the **hub** only, and it fires at **login**. The launcher GUI has a separate switch that fires when you open a project — see the next section.

### Starting the launcher GUI with a session (v0.2.95, default ON)

The `session-start-ensure-hub` hook — which Claude Code runs on `SessionStart` and `.vscode/tasks.json` runs on VS Code's `folderOpen` — also brings up the launcher GUI **in the tray only**, when it is not already running. `python -m vco_lib.launcher_ensure {status,ensure}` is the one home for the decision; the hook calls it and reports.

- **No window, no focus.** The launcher is started with `--start-hidden`, which it applies to its own window configuration before any window is created. On every OS this is the same mechanism — a window that is never created visible is never mapped (X11/Wayland), never `makeKeyAndOrderFront`-ed (macOS) and never given `SW_SHOW` (Windows). Left-click the tray icon to open it.
- **Never a second instance.** The leg spawns only when a process scan finds no launcher, and the single-instance plugin refuses a duplicate that races the probe — without taking focus, because it can see the flag in the duplicate's arguments.
- **Switch it off** in the launcher's **Preferences → Startup → "Start the launcher with a Claude Code session"**. The preference is `launcher.session_autostart` in `launcher.db`'s `app_state`; no row means ON, so an existing install gets the behaviour after an update without a migration.
- **Skipped automatically** when there is no desktop to start on: a Linux session with neither `$DISPLAY` nor `$WAYLAND_DISPLAY` (CLI over SSH, a container, CI), and any machine with no launcher binary at all (a fresh clone, a headless install) — both are silent, successful no-ops.
- **A launcher binary older than v0.2.95 is left alone**, loudly: it would open a window and take focus, so the leg reports `binary_too_old` and points at `python install.py --update` instead of starting it.

Environment keys for this leg:

| Key | Default | Meaning |
|---|---|---|
| `VCT_DISABLE_LAUNCHER_AUTOSTART` | unset | Set to anything non-empty to skip the leg entirely, without touching the preference. The per-machine kill switch for CI and headless hosts. |
| `VCT_LAUNCHER_BIN` | unset | Explicit path to the launcher binary, highest precedence in the discovery chain (the `VCT_HUB_BIN` of this leg). |
| `VCO_LAUNCHER_STATE` / `VCO_LAUNCHER_PID` / `VCO_LAUNCHER_REASON` | — | Not inputs: what `launcher_ensure ensure --shell` PRINTS for the bash hook to `eval` (the PowerShell sibling reads the same fields from `--json`). |

### Launcher process timing knobs (v0.2.102)

The launcher reads these from its own environment at start, so set them in the environment the launcher is started from (the session hook's, or your desktop session's). Each takes a positive integer number of seconds; an unset, empty, non-numeric, zero or negative value means the default. None of them can turn its mechanism off.

| Key | Default | Meaning |
|---|---|---|
| `VCT_LAUNCHER_STALE_IDLE_CHECK_SECS` | `600` (10 min) | How often a launcher started with `--start-hidden` checks whether it should relaunch itself onto a refreshed binary. The relaunch happens only when a `launcher_binary_stale` record exists, no window is visible, no update is running and no update-handoff modal is pending (`launcher/src-tauri/src/commands/restart.rs`). |
| `VCT_LAUNCHER_RELAUNCH_LOCK_WAIT_SECS` | `30` | How long a relaunched launcher (started with `--vct-relaunch-parent=<pid>`, by the restart modal's Continue or by the idle check above) waits for the exiting parent to release the single-instance lock. When the wait ends with the parent still alive, start-up continues with a WARNING and the single-instance hand-off decides which process stays (`restart.rs`). |
| `VCT_SETTINGS_WATCHER_REFRESH_WAIT_SECS` | `1800` (30 min) | On a boot that crosses an install boundary, the launcher re-projects every project's env before its `settings.json` watcher registers any watch, so those writes are never seen as edits and never SIGHUP the running MCPs. This is the longest the watcher waits for that refresh. The refresh is N serial projections with a 300 s per-project cap, which is why the default is long. When the wait expires, the watcher arms anyway and logs a WARNING; its seeded diff baselines then decide whether a late write reloads anything. An ordinary boot has no such refresh, and the watcher arms immediately (`launcher/src-tauri/src/services/settings_json_watcher.rs`). |

**Key endpoints**:

| Endpoint | Purpose | Notes |
|---|---|---|
| `GET /api/v1/health` | Liveness probe | No auth required. (The bare `/health` path does NOT exist — it hits the auth layer and returns 401, not a liveness answer.) |
| `GET /api/v1/projects/{id-or-slug}/config` | Resolver: KG collection, codegraph prefix, embedding selections, access-matrix lists, service URLs | Accepts UUID or slug as the `{id}` path arg (try-UUID-then-slug fallback). Replaces per-process `os.getenv("KG_COLLECTION")` etc. drift. Returns 503 when primary KG binding is missing (caller-actionable). |
| `GET /api/v1/projects/{id}/env?key=<slot>` | Secrets resolver | Resolves via per-project keychain row first; falls back to SENTINEL_SHARED + `module_id=user` for shared slots declared in `vct-module.json::bundled_secrets[]`. |
| `GET /api/v1/services/status` | Services snapshot | Returns a degraded skeleton (`degraded: true`, no per-service runtime). The `/services/{start,stop,restart}` routes return `501 not_implemented`. |
| `GET /api/v1/projects/by-path?path=<abs-path>` | Path → project UUID | Used by resolver clients before fetching `/config`. Returns 404 with `project_not_found` when the path isn't registered. |

**Resolver clients** discover the hub via the same chain:

- `templates/scripts/vct_project_config.sh` (bash, hooks + shell scripts)
- `templates/scripts/vct_project_config.ps1` (PowerShell 7+, Windows hooks)
- `vco_lib/project_config.py` (`from vco_lib.project_config import resolve, ProjectConfig` — used by `install.py`, MCPs, and any Python tooling)

Discovery: `VCT_HUB_PORT` env → `<vct_root_dir>/hub.port` → `7700` default; token: `VCT_HUB_TOKEN` env → `<vct_root_dir>/hub.token`. All clients enforce the same exit-code shape (0 success / 1 hub unreachable / 2 project not registered / 3 service misconfigured / 4 field not found / 5 forbidden — hub refused the token on `/env`|`/config`, callers MUST NOT env/file-fallback / 64 usage error). Stderr emissions are rate-limited per `(pid, error_kind)` to one line per 5 minutes — `VCO_HOOK_DEBUG=1` bypasses the limit.

**Stale `VCT_HUB_TOKEN` (v0.2.91)**: the hub regenerates `hub.token` on every start, so a shell that exported `VCT_HUB_TOKEN` before an update holds a value the hub refuses — and the env pin wins over the file, so every resolve from that shell used to fail with a misleading "hub unreachable / launcher may have restarted" diagnostic until the shell was replaced. Now, on a PROVABLE refusal (401/403) where the exported token differs from the on-disk one, every hub client — the resolver script quadruplet, the access-matrix gate trio `vct_access_check.{sh,ps1}` + `vco_lib/access_resolver.py`, `vco_lib/project_config.py`, the weaviate MCP's writable-collections probe, the codegraph-resync spawn registration, `vct-cli` (`launcher/tools/vct-cli`) and `vct` (`tools/vct-secrets`) — retries **once** with the on-disk token (scoped `hub.token.<project_id>` on the per-project routes, global otherwise) and prints one line to stderr:

```
stale VCT_HUB_TOKEN in env overridden by on-disk hub.token — run `unset VCT_HUB_TOKEN` or open a new shell
```

Bounded and non-destructive: exactly one extra request, nothing restarts or loops, and the retry's answer is **adopted only when it proves the on-disk token was accepted** — a `2xx`, or a `404` (which the hub answers only *after* its auth middleware accepted the bearer). Any other outcome — another refusal, a `5xx`, a transport failure — keeps the original error path, exit code and diagnostic unchanged, and does **not** print the line above. For the access-matrix gate trio that means the deliberate **fail-open to `write`** on a genuine auth failure is untouched — the fix only makes the fail-open reached less often, so a stale shell no longer silently degrades the access matrix to permissive on every call. Set `VCT_HUB_TOKEN_STRICT=1` to disable the fallback entirely — tests and harnesses that pin a deliberately-wrong token and assert the refusal must set it. The decision function lives in `vco_lib/project_config.py::_stale_env_token_fallback` (SSOT); the other implementations are parity-locked mirrors (`tests/test_stale_env_token_parity_v0291.py`).

**Cutover sentinel**: when `install.py` deploys `vct-hub` for the first time, it writes `<vct_root_dir>/v0.2.21-cutover.flag` (literal filename) BEFORE starting the hub. The launcher reads this flag on startup and skips its own in-process services watcher (knowing the hub will take it over). `install.py` deletes the flag after `vct-hub` responds to `/api/v1/health`. Leftover sentinels are harmless — the hub's first successful `/api/v1/health` clears the contention.

## WebFetch SSRF guard

The `pre-tool-use` hook checks every WebFetch URL against a private-address guard (`vco_lib/ssrf_url.py` is the one implementation). Alongside the local services (Weaviate, Ollama, code-embed), `http://localhost:<hub port>` — the port resolved by the same `VCT_HUB_PORT` → `hub.port` → `7700` ladder above — is **deliberately allowlisted** so WebFetch can reach the hub's `/api/v1/...` routes locally; this is safe because every `/api/v1/*` route except the `/api/v1/health` liveness probe requires the bearer token from `<vct_root_dir>/hub.token` (mode `0o600`), with the per-project `/env` and `/config` routes additionally requiring a project-scoped token, so an allowlisted fetch without the token learns nothing (v0.2.102 NB-H4 / OD-18).

A project that runs its own local service (its own Ollama on `11436`, say) can let WebFetch reach it by declaring the port on the launcher's Identity tab (**Infrastructure this project owns → Local ports**). The declaration is projected into that project's `.claude/settings.json` `env` as `VCT_SSRF_EXTRA_LOCAL_PORTS` (comma-separated); the guard allows each listed port as a loopback pair (`localhost` / `127.0.0.1` / `[::1]`) for that project only. Invalid entries are ignored, and a port nobody declared stays blocked. On a launcher-less install, export the variable in the shell that starts Claude Code instead — VCO's env projection owns the key in `.claude/settings.json` and removes a hand-added copy on the next apply.

## Paid-module license framework

This repo is fully functional standalone. Optional paid modules (RL retrieval reranking, MAO multi-agent runtime, specialist agent packs) activate only when a license key is present. Without a key, retrieval falls back to plain Weaviate cosine ordering — nothing breaks.

License resolution priority (first match wins; see `VCThelpers/license/validator.py`):

1. `VIBECODED_TIER` env var — `free` | `pro` | `mao` | `enterprise`. **Only `free` is trusted**; any other value is ignored. We never accept an env-var-claimed paid tier without a validated key.
2. `VIBECODED_LICENSE_KEY` env var — 36-char UUID. Set by the launcher after activation.
3. `~/.vct-secrets/shared/license_key` file (chmod 600, plain UUID, no trailing whitespace). Used by headless installs where the launcher hasn't run. Legacy flat layout `~/.vct-secrets/license_key` is still honored as a fallback.
4. `VIBECODED_LICENSE_URL` env var — Supabase `/validate-tier` edge function URL. Defaults to the production deployment.

**Grace period**: if the last successful remote validation was >3 days ago and the validation endpoint is unreachable, the tier degrades to `free`. A human-readable message lands in `~/.vibecoded/license_status.txt`. Nothing breaks.

**Network policy**: fail-OPEN to free tier on any transport failure. Never block startup, never raise.

**Free-tier RL gate** (in `claude_mcp_servers/weaviate_mcp/server.py`): `_rl_cache_and_rerank` skips RL reranking when `feature_enabled("rl_retrieval") == False`. Pro/MAO licenses unlock RL. Free-tier users get plain Weaviate cosine ordering.

**RL module env vars** (only meaningful with Pro+ license):

| Var | Default | What it does |
|---|---|---|
| `RL_SERVER_URL` | `http://localhost:11439` | RL retrieval service URL. Read by `RLClient` in `weaviate_mcp/server.py`. |
| `RL_SERVER_PORT` | `11439` | Back-compat port override. |
| `RL_PROJECT_ROOT` | project root | Override for the RL service's project-anchored state directory. |

### RL event retention and archives

The `rl_events` table in `launcher.db` is written on every ranked retrieval regardless of tier (only the *reranking* is Pro-gated), so its retention knobs apply to free installs too. The prune driver is `claude_mcp_servers/rl_client/rl_retention.py`; the delete and the archive are executed hub-side, by the process that owns `launcher.db`.

| Var | Default | What it does |
|---|---|---|
| `RL_EVENTS_RETENTION_MAX_AGE_DAYS` | `90` | Delete events older than N days. `0`/negative disables the age bound. Read fresh on every pass. Never closer than `RL_CITATION_ABANDONED_DAYS` + 1 day (8 days by default): a retrieval that recent may still have a pending citation (see RL citation scoring below). |
| `RL_EVENTS_RETENTION_MAX_ROWS` | `0` (disabled) | Keep at most N most-recent rows; age is the primary bound. |
| `RL_EVENTS_RETENTION_DISABLED` | unset | Truthy → never prune (offline-training operators who want the full corpus). |
| `RL_EVENTS_RETENTION_MIN_INTERVAL_S` | `3600` | Minimum seconds between prune passes **for one process**. Throttle only; a fresh process always allows the first pass. |
| `RL_EVENTS_ARCHIVE_DIR` | `<VCT_STATE_DIR or ~/.vct>/rl_archive` | Where the prune writes its archive sidecars. Resolved **hub-side**, never from a request body — a caller-supplied path would be an arbitrary-write surface on an authenticated localhost route. |
| `RL_EVENTS_PRUNE_MAX_TASKS_PER_PASS` | `500` | How many task GROUPS one pass may move, oldest-first. Bounds the archive's in-memory row set, which is what makes the prune incremental: a long-neglected corpus drains over successive passes instead of materializing gigabytes at once. Raise it to drain a backlog faster. |

Since v0.2.91 the prune is **archive-then-delete**: victim rows are written to a compressed, loader-readable sidecar and verified before any `DELETE` runs, and a failed archive aborts the prune so nothing is deleted. See [`features/04-knowledge-and-code-graph.md`](features/04-knowledge-and-code-graph.md#retention-archive-then-delete-v0291) for the archive format and how to read one back.

### RL memory bounds in the weaviate-kg MCP (v0.2.102)

The weaviate-kg MCP process lives as long as your Claude Code session, so its RL caches are bounded in bytes as well as in entries. All three knobs are read on every use, live in `claude_mcp_servers/rl_client/memory_bounds.py`, and fall back to the default on an unset, non-numeric, zero or negative value. None of them drops training data: an evicted cache entry is re-read or recomputed, and a deferred monitor's citation is computed by the Stop-hook drain from the pending file every search stages.

| Var | Default | What it does |
|---|---|---|
| `VCO_RL_TRANSCRIPT_CACHE_MAX_BYTES` | `268435456` (256 MiB) | Estimated memory the answer monitor's parsed-transcript cache may hold. Least-recently-used transcripts are evicted first. The transcript just read is always kept, because re-parsing it on every poll would cost the same memory plus the CPU. |
| `VCO_RL_NODE_CACHE_MAX_BYTES` | `67108864` (64 MiB) | Estimated memory of the per-search citation cache. Oldest entries are evicted first, and a running monitor keeps its own copy. |
| `VCO_RL_MONITOR_MAX_CONCURRENT` | `64` | How many answer monitors may run at once. When the cap is reached, no running monitor is cancelled: the new search starts no monitor, and its citation is computed from its pending file at the next Stop. |

### RL citation scoring (v0.2.102)

A retrieval's citation label is scored from the answer that followed it, by the turn-end drain (`stop-drain-citations` on Stop, SubagentStop and SessionEnd) or the MCP answer monitor. Each citation is scored against the transcript of the chat that produced it, never another chat's, and there is no scoring deadline: a subagent's citations are scored when it stops, however long it runs; the main chat's at the full token gate on any Stop, and with the lower floors below only once the chat has ended (its SessionEnd drain, or a later drain finalizing a session whose end was recorded). A citation removed without a score is recorded in `.claude/logs/rl-citations-unscored.jsonl`. All four knobs are read through `vco_lib/env_values`; an unset, blank or unparseable value keeps the default.

| Var | Default | What it does |
|---|---|---|
| `RL_MIN_ANSWER_TOKENS_FOR_CITATION` | `25000` | The full gate: an answer window at least this long is scored on any drain, also while the chat is still running. Shared with the MCP monitor. |
| `RL_TERMINAL_CITATION_MIN_TOKENS` | `2000` | Once the chat has ended, a shorter window that clears this floor is scored (`fire_reason=terminal_floor`). |
| `RL_SOFT_CITATION_MIN_TOKENS` | `200` | Once the chat has ended, a window below the terminal floor but above this one is scored as a soft label (`fire_reason=soft_terminal`, `soft_label: true`) the trainer can down-weight. |
| `RL_CITATION_ABANDONED_DAYS` | `7` | The only time bound: a pending citation whose chat's transcript has not been written for this many days, and which no drain or monitor holds, is garbage-collected — scored first when its transcript is readable, otherwise logged as unscored. Positive values only. |

`RL_TERMINAL_CITATION_AGE_SECONDS` is no longer read (v0.2.102): it applied the lower floors to a file 30 minutes old even while its chat was still running, and the chat-end events replaced it.

## Container runtime

`vco_lib/containers.py` resolves the runtime via:

1. `VCT_CONTAINER_RUNTIME` env var — explicit `podman` or `docker`. It is a **pin**, not a preference: when set, it is the *only* candidate. If the pinned runtime is unusable (not installed, client binary refuses, daemon/machine/socket down), VCO **refuses** with an actionable message naming what you pinned, why it is unusable, and whether the other runtime is usable — it does **not** fall back to the other one. See [Why a refused pin is not a fallback](#why-a-refused-pin-is-not-a-fallback) below.
2. The runtime the install recorded in `state/install/runtime.txt` — VCO's own note of where it put your data. Same pin semantics (probed alone; unusable → refusal), except that a READ-ONLY surface (a session, the boot service, the launcher, the hub) may answer the other runtime when the recorded one is no longer installed AND that one already holds VCO's containers or volumes — and never when the record is a `--container` choice of yours (`state/install/runtime.confirmed`). The next `install.py --update` re-records the runtime your data is on.
3. Caller-passed `runtime` arg.
4. `auto` (or unset, the last fallback of the one rule) → probe `podman` first, then `docker`. Podman-first is intentional: podman's rootless mode is the orchestrator's default deployment.

The chosen executable is returned as a string (`podman` or `docker`) and used uniformly through the rest of the codebase. Compose files live in `infrastructure/docker-compose.yml` — the one home every VCO path composes from (wrapper, hook, launcher, install.py), always naming the `vco_managed` services explicitly with `--no-deps`. The legacy `claude_mcp_servers/compose.yaml` home is no longer composed from; containers created there are adopted as `adopted_container` rows.

### Where VCO looks for the runtime (v0.2.97)

"Is podman (or docker) installed?" is answered on the calling process's `PATH`
**and** in the usual install locations — one list, `vco_lib/tool_search_dirs.toml`,
read by the Python side (`vco_lib.tool_search_dirs`), by the launcher and the
hub (both add the directories their `PATH` lacks at startup) and by the boot
wrapper `scripts/launch-claude-mcp-stack.{sh,ps1}` (it adds the directory of a
runtime found outside its `PATH`). Each entry has a **placement**, and every
surface applies the same order, so a name resolves to the same binary everywhere:

| Placement | Entries | Where a missing one goes |
|---|---|---|
| `prepend-when-missing` | the graphical-launch list (v0.2.53). Linux: `~/.local/bin`, `~/.cargo/bin`, Linuxbrew, `/snap/bin`, flatpak. macOS: `/opt/homebrew/bin` and `/sbin`, `~/.cargo/bin`, `~/.local/bin` | **ahead** of your `PATH` — the order your login shell builds, so a Finder launch runs Homebrew's `git`/`python3` rather than the `/usr/bin` Xcode Command Line Tools stubs |
| `append` | the container-runtime locations (v0.2.97). Linux: `~/bin` (rootless Docker), `/usr/local/bin`, `/usr/bin`. macOS: `~/bin`, `/usr/local/bin`, `/opt/podman/bin`, Docker Desktop's app bundle, `~/.docker/bin`, MacPorts, `/usr/bin`. Windows: the Docker Desktop and Podman installer directories | **after** your `PATH` — they only reach a tool nothing on it provides, and never shadow one |

A directory already on your `PATH` is never moved, whatever its placement.
A boot unit, a Finder/`.desktop`-launched launcher and the hub start with a short
`PATH`; without this list they read a runtime in `~/bin` or `/opt/homebrew/bin` as
**not installed** — and a runtime that is not installed is the one case in which
VCO's own record (`state/install/runtime.txt`) may be switched to the other
runtime. Even then, a read-only surface (a session, the boot service, the
launcher, the hub) switches only when the other runtime **holds VCO's data**, and
never away from a runtime you chose with `install.py --container` (that record is
`state/install/runtime.confirmed`; run `install.py --update --container <other>`
to change it — both files are rewritten).

| Var | Effect |
|---|---|
| `VCT_TOOL_SEARCH_DIRS` | When **set** (even to an empty string), replaces the list above for this OS: entries separated by the OS path separator (`:` / `;`), each `~/…` or `${NAME}…` expanded, every one placed `append` (after your `PATH`). Empty means "look on `PATH` only" — the test suite runs that way so it never finds the host's own runtimes. |

### Forcing Docker when both runtimes are installed

Hosts with both Podman AND Docker installed default to Podman (step 4 above; see `_detect_container_runtime` in `install.py`, a thin call into `vco_lib/containers.py::resolve`). To force Docker — for example because the Docker daemon is the one wired to team registry credentials, or because Podman's rootless mode hits a permission wall on the filesystem — export `VCT_CONTAINER_RUNTIME=docker` before running install or any container-touching hook:

```bash
export VCT_CONTAINER_RUNTIME=docker
python install.py --update          # install / update flows
.claude/hooks/ensure-containers.sh  # session-start hook
```

Persist the override by adding the export to a shell rc (`~/.bashrc` / `~/.zshrc`) or to the per-project `.claude/env` so every Claude Code session inherits it. The value wins over auto-probe and over any caller-passed `runtime` argument. Symmetric override: `VCT_CONTAINER_RUNTIME=podman` forces Podman when auto-probe would have picked Docker (unusual but possible if `podman` is installed but not first in `PATH`). Unrecognised values are logged and ignored — falling through to auto-probe (an unrecognised value is not a pin).

### Why a refused pin is not a fallback

Podman and Docker keep **separate named volumes** (`infrastructure/docker-compose.yml` maps `weaviate_data` → `vco_weaviate_data` and friends *inside whichever runtime drives compose*). So the two runtimes are two different data planes holding two different knowledge graphs.

That is why a pinned-but-unusable runtime is refused rather than swapped. Consider the common case: you pinned `podman`, rebooted, and the podman machine did not come back up while Docker Desktop did. A fallback would run `docker compose up -d` and stand up an **empty Weaviate on :8081** — which every downstream heal, sync and search then reads as *your* knowledge graph, while the launcher (strict about the pin since PR-43) reports no runtime at all. Both halves look plausible in isolation; together they are a split brain over your data.

Instead every surface says the same thing. The session-start hooks print the resolver's reason **on stdout** (so it lands in the session context — a hook's stderr is discarded when it exits 0):

```
ensure-containers: VCT_CONTAINER_RUNTIME=podman is set but `podman info` failed (daemon / machine / socket not running); docker is usable but VCO will NOT drive it for you (podman and docker have SEPARATE named volumes, so the stack would come up EMPTY on the other one) — start podman, or unset VCT_CONTAINER_RUNTIME / set it to docker; skipping
```

`install.py` writes the same text to the install log, and the launcher's install preflight returns `pinned` / `pinned_installed` / `alternative_usable` alongside `available: false` so the modal can say "podman is pinned but unusable" instead of "no container runtime is installed".

Your three ways out, in the order the message lists them: **start the pinned runtime** (usual fix — `podman machine start`, `systemctl --user start podman.socket`, launch Docker Desktop); **unset `VCT_CONTAINER_RUNTIME`** to return to auto-probe; or **repin** to the runtime the message named as usable — knowing that its volumes are a different data plane, so an existing KG on the other runtime will not be there.

### Container engine auto-start (v0.2.103)

The first of those three ways out — "start the pinned runtime" — is what VCO now does by itself. A resolver verdict of `absent` is most often "installed but not running": Docker Desktop was not launched after a reboot, the podman machine is stopped, the rootless socket is down. Since v0.2.103 the engine is started automatically at the moments it may simply be down: the SessionStart `ensure-containers` hooks (opening a VCO project in VS Code or the CLI), the hub's startup, and the launcher's startup — each of them spawns the ONE starter, `python -m vco_lib.container_engine_start ensure` (`vco_lib/container_engine_start.py`), detached, so nothing blocks on an engine boot. `install.py`'s pre-flight runs the same functions directly.

The starter's contract:

* **only a RECORDED engine is ever started** — starting requires the install to have recorded a runtime (`state/install/runtime.txt` — written by the install whenever it detected or was given a runtime, not only under `--container` — or the `VCT_CONTAINER_RUNTIME` env pin). With no recorded runtime nothing is started — never the first runtime binary on PATH, never an unattended `podman machine init`. An install that chose `--no-containers` (persisted in `state/install-manifest.json`) gets nothing started either;
* **idempotent, single-flight** — an engine that answers (`<runtime> info` exits 0) is a no-op, and the launcher + the hub + N concurrent sessions that all reach for the starter during one engine boot produce exactly ONE start: the CLI holds a per-user claim (`<vct state dir>/state/engine-start.lock`, a kernel-level lock released when the holder exits) for its whole run, and every other concurrent run reports "already in progress" and stands down;
* **the recorded-but-down runtime IS started** (the refused pin above); the other runtime never is, for the same named-volumes reason;
* **per-OS recipe** — Docker Desktop on macOS (`open -a Docker`) and Windows (shell-start of `Docker Desktop.exe`); the podman machine on macOS/Windows (`podman machine start`, init-if-absent); the rootless socket on Linux (`systemctl --user start podman.socket`). Linux Docker is NOT auto-started (`sudo systemctl start docker` needs an interactive password). On Windows every process involved runs windowless (`CREATE_NO_WINDOW`) — no console flash during a boot;
* **soft-fail, with a trace** — a start that cannot happen logs one line and gives up; nothing turns red. Because every surface spawns the starter detached, the CLI itself appends each run's outcome (started, no-op and why, stood down, crashed, plus any intermediate failure line) to `<vct state dir>/logs/engine-start.log` — the first place to look when the engine did not come up. That log is bounded (cut to its last 500 lines once past 256 KiB; the CLI is its only writer) and removed by uninstall. Each spawner also captures the child's stderr in its own per-spawn file `<vct state dir>/logs/engine-start.stderr.log`, overwritten on every spawn (so it holds the latest spawn's stderr — where a crash traceback lives; the CLI's crash outcome line points at it; when the launcher, hub and a session hook spawn at the same moment the last one wins, and on Linux a traceback may follow a run of NUL bytes from the earlier writer's offset). On Windows PowerShell 5.1 that raw file is written UTF-16LE with `NativeCommandError` records; on pwsh 7 and POSIX it is plain UTF-8;
* **update gate** — a fresh `.update-in-progress.json` (the same lockfile the session hooks honour) means the starter stands down until the update completes;
* **bounded waiting, never killing** — after a Docker Desktop launch the starter polls liveness for up to 60 s (in its own detached process); when the wait ends it reports and gives up, it never kills or cancels anything.

Two opt-outs, either of which disables the starter on EVERY surface (both are checked before any probe; either one alone is enough — the env var can only turn auto-start OFF, it cannot re-enable it over a Preferences off):

| Knob | Default | Effect |
|---|---|---|
| `VCT_CONTAINER_ENGINE_AUTOSTART` | unset (auto-start on) | Set to `0`, `false`, `no` or `off` to disable engine auto-start on every surface — the starter checks it before any probe and no-ops (the hub and the launcher also skip the spawn before it is made). The engine already running is unaffected; containers are still reconciled as usual — only the "start the engine" step is opted out. |
| Preferences → Startup → "Start the container engine automatically" | on | The GUI toggle (launcher.db `app_state` key `engine.autostart`), for the launcher-started-from-an-icon and scheduler-started-hub surfaces that never see your shell env. Turning it off takes effect from the next session / launcher / hub start. A launcher.db that cannot be read is NOT an opt-out (the shipped default applies). |

### Volume source overrides (v0.2.97)

`infrastructure/docker-compose.yml` mounts three named volumes — `weaviate_data` → `vco_weaviate_data`, `ollama_data` → `vco_ollama_data`, `code_embed_cache` → `vco_code_embed_cache`. A machine whose services were first stood up with **bind mounts** (for example an Ollama model directory at a host path shared with other containers) could not adopt that file without copying the data — and a blind `--force-recreate` from it silently re-pointed the service at an **empty** default volume, orphaning the data without deleting it. Each volume source is therefore env-overridable (the `VCT_CODE_EMBED_BUILD_CONTEXT` pattern), two knobs per service:

| Var | Default | What it does |
|---|---|---|
| `VCT_WEAVIATE_DATA_SOURCE` | `weaviate_data` | The weaviate service's mount source. Set it to a **host path** (`/`, `./` or `~` prefix) to make the mount a bind at that path. |
| `VCT_WEAVIATE_VOLUME_NAME` | `vco_weaviate_data` | The resolved name of the `weaviate_data` volume. Set it to an **existing volume name** to reuse that volume. |
| `VCT_OLLAMA_DATA_SOURCE` | `ollama_data` | Same, for the ollama service's `/root/.ollama` mount. |
| `VCT_OLLAMA_VOLUME_NAME` | `vco_ollama_data` | Same resolved-name knob for `ollama_data`. |
| `VCT_CODE_EMBED_CACHE_SOURCE` | `code_embed_cache` | Same, for the code_embed service's `/cache` mount. |
| `VCT_CODE_EMBED_VOLUME_NAME` | `vco_code_embed_cache` | Same resolved-name knob for `code_embed_cache`. |

Why two knobs and not one: compose classifies a short-syntax source as a **bind** when it starts with `/`, `./` or `~`, and as a **named volume** otherwise — and a named volume that is not declared under top-level `volumes:` is a hard error (`service refers to undefined volume`, docker compose v2; podman-compose likewise fails to parse). So "an existing volume name" can only enter through the declared volume's `name:` field. Both runtimes honour `${VAR:-default}` in both positions (verified on docker compose v2.40.3 and podman-compose 1.5.0 via side-effect-free `config` renders, 2026-09-23).

**Who writes them: VCO, not you.** Since v0.2.97 `infrastructure/.env` is written from the `service_endpoints` rows by `vco_lib/compose_env.py::write_service_keys` — the port keys (`WEAVIATE_PORT`, `WEAVIATE_GRPC_PORT`, `OLLAMA_PORT`, `CODE_EMBED_PORT`) for `vco_managed` rows, and the data-source knobs (`VCT_OLLAMA_DATA_SOURCE`, `VCT_CODE_EMBED_CACHE_SOURCE`, …) for every row that records an observed mount — adopted containers included, so a recreate outside `install.py` still binds the recorded data (v0.2.101) — inside one marker-delimited block that every row change rewrites. Never hand-edit the file: a line outside the block that assigns a key the rows state is superseded on the next write, because the row is the source of truth and compose must never see two assignments. The code_embed row stores the observed `/cache` mount (bind path or volume name) and projects it as `VCT_CODE_EMBED_CACHE_SOURCE` / `VCT_CODE_EMBED_VOLUME_NAME`; every recreate of the container verifies the mount before and after, and a mismatch refuses or rolls back — an existing model cache is never bypassed. To change a service's data source, change its row (`python -m vco_lib.service_endpoints adopt|move|hand-to-vco`, or the launcher's Services page): the mount identity travels with the row. When Ollama is not `vco_managed`, the same writer states `CODE_EMBED_OLLAMA_URL` so the code-embed container reaches it through the runtime's loopback alias (`host.containers.internal` on podman, `host.docker.internal:host-gateway` on docker) instead of the in-network name. On SELinux-enforcing hosts a swapped-in bind source needs the `:Z` flag — see [SELinux: bind-mount layouts need a `:Z` flag](TROUBLESHOOTING.md) in the troubleshooting guide.

## MCP Servers

MCP servers are registered in the user's `~/.claude.json`. Each launches via the orchestrator install's venv — canonical `<install>/.venv`, with the legacy `claude_mcp_servers/.venv` accepted as a fallback for pre-unification installs (`mcp_registration.rs::resolve_venv_python`).

**weaviate-kg** — semantic search + code graph.
- Command: `<install>/.venv/bin/python claude_mcp_servers/weaviate_mcp/server.py` (legacy installs: `claude_mcp_servers/.venv/bin/python`)
- Env: `WEAVIATE_URL`, `OLLAMA_URL`, `EMBEDDING_MODEL`, `KG_COLLECTION`, `SHARED_KG_COLLECTION`, `DEVELOPMENT_COLLECTION`, `GRPC_PORT`, `SHARED_KG_WRITE_DISABLED` (write gate; legacy alias `SHARED_KG_OPT_OUT` kept for ~3 releases), plus the EmbeddingService vars (`ACTIVE_EMBEDDING`, `CODE_EMBED_SERVICE_URL`, etc.). The OpenAI key is NOT among them — it is a secret-shaped key and never reaches this file; the embedding stack resolves it in-process from VCO's own shared slot (see the env table above).

> **v0.2.101**: the `search` MCP (paper search) was deleted outright; a still-live `search` entry is **removed automatically by the ordinary install/update**, with one notice line (no consent prompt — the module is gone). Claude's built-in WebSearch / WebFetch cover web and academic retrieval.
>
> **v0.2.102**: the `mermaid` / `excalidraw` diagram wrapper MCPs were removed entirely with the diagrams integration. A leftover wrapper entry from an older install is scrubbed automatically by the ordinary install/update (one notice line; third-party same-name entries a user installed themselves are left alone), and the `mcp__mermaid__*` / `mcp__excalidraw__*` permission-allow patterns are dropped from existing projects' `.claude/settings.json` at the next bundle update.

**playwright** — browser automation, enabled by default and invoked separately via `npx -y @playwright/mcp@<pin> --browser chromium` (pin: `vco_lib/bundled_mcp_versions.toml` `[npx.playwright_mcp]`; every install/update rewrites the entry, so older `@latest` registrations move to the pin on the next update). `install.py` pre-caches the pinned package and the Chromium revision it requests (opt out with `VCT_SKIP_PLAYWRIGHT=1`); a browser still missing afterwards is deferred as `playwright_browser_missing` with the exact install command.
- The entry stores the bare name `npx`, which Claude Code resolves from the spawn PATH at MCP-launch time. On a machine without Node.js there is nothing to resolve, so the MCP never starts — and pre-v0.2.91 nothing said so (the installer printed "the MCP will lazy-install when first invoked", which is impossible without npx). Since v0.2.91 the doctor phase probes it via `vco_lib/npx_resolver.py`, defers `npx_missing_mcp_unspawnable`, and the launcher's registration badge turns yellow with the same remediation.

**Per-project enable/disable**: a per-project MCP toggle writes `~/.claude.json` `projects[<absolute project path>].disabledMcpServers` — the per-project opt-out list Claude Code honours for user-scope servers. (The settings-file `disabledMcpjsonServers` / `enabledMcpjsonServers` keys govern only servers defined in the project's own `.mcp.json`; they are not the channel for a user-scope entry.)

**Not MCPs**: Ollama runs as infrastructure only (Weaviate text embeddings + code-embedding service CPU fallback) — there is no Ollama MCP server; Claude's native reasoning, `Read` tool, and built-in vision cover analysis, document reading, and image tasks. The code-embedding FastAPI service on port 11440 is likewise backend infrastructure. Web / academic retrieval uses Claude's built-in WebSearch / WebFetch — no local search proxy runs in the default container stack.

**Stale MCP cleanup**: `install.py --rewrite-stale-mcps` detects deprecated MCP entries left over from older versions in `~/.claude.json` and offers consent-prompted auto-rewrite. Run after upgrading from an older install.

## Agents and skills

See [templates/README.md](../templates/README.md) for the bundled agents and skills and install-flag reference.

## Parallel agents (3-5x speedup)

Claude Code can run multiple agents concurrently on independent sub-tasks. Turn it on globally:

```json
// ~/.claude/settings.json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```

With this on, asking the orchestrator to refactor 30 files or analyze 5 directories spawns up to 3 parallel agents instead of doing the work sequentially. Typical speedup on multi-file tasks: 3-5x.

This is the only global env var worth setting; everything else is per-project.

### Isolated agents in another repo — lane reservations (v0.2.102)

An `isolation: worktree` agent gets a worktree that VCO's `worktree-guard` hook creates — by default a detached checkout of the project's own repo at HEAD under `.claude/worktrees/`. A coordinator whose lane belongs in another repo, at a chosen base on a chosen branch, reserves the worktree first:

```bash
python -m vco_lib.lane_reservation reserve --project <project> --repo <repo> --base <commit> --branch <new-branch> \
    [--id <id>] [--existing-path <worktree>] [--ttl <seconds>]   # prints the reservation id
python -m vco_lib.lane_reservation list   --project <project> [--json]
python -m vco_lib.lane_reservation cancel --project <project> --id <id>
```

- **Allowed repos**: the project itself, or one of its ENABLED code folders (launcher: Identity tab → **Add code folder**; stored in `project_codegraph_extra_paths`, served by the hub's `/config` as `code_graph_extra_paths`). Normalized real paths are compared. The list comes from the hub, so with the hub down reservations are refused at reserve AND at launch time; ordinary isolation never asks the hub.
- **Validation at reserve**: the repo is a git repository; `--base` resolves to a commit; `--branch` is a valid branch name that does not exist yet and is not checked out anywhere (or, with `--existing-path`, the path is a registered, non-main worktree of the repo, on that branch, clean, and containing the base); the id is unique among pending reservations, and no other pending reservation targets the same branch.
- **Consumption**: the next isolated launch whose identifier starts with `agent-` (a subagent — `claude --worktree <name>` never consumes) takes the OLDEST pending, unexpired reservation, by an atomic rename, so two launches can never take the same one. Dispatch one isolated agent per message. The allow-list is checked again, then the worktree is created at `<parent of the repo>/<repo name>.vco-lanes/<branch-slug>-<id>` (outside the repo's working tree) with `git -C <repo> worktree add -b <branch> <path> <base>`, or the existing worktree is adopted. `git worktree add` has no VCO time limit (a very large repo can take minutes); the WorktreeCreate hook registration carries no `timeout`, so Claude Code's command-hook default (600 s) is the only bound. A reservation that fails at launch — including a filesystem error preparing the lane path — aborts the launch with the reason on stderr and is marked failed; it never falls back to a worktree of the project.
- **TTL**: 15 minutes by default (`--ttl`); an expired reservation is moved aside and never consumed.
- **State**: `<project>/.claude/state/lane-reservations/{pending,claimed,expired,cancelled}/res-<created_ns>-<id>.json` (claimed/expired/cancelled kept 30 days for audit). The agent's launch record (`.claude/state/worktree-guard/wt-agent-<id>.json`) carries the binding: mode `reservation` (or `reservation_existing`), `reservation_id`, `repo`, `branch`, `base_sha`.
- **When the agent starts**, `subagent-start-isolation-check` tells it its reservation id, worktree, repo, branch and base, and to stop if its brief names a different id. **When it stops**, `subagent-stop-reconcile` keeps a worktree with changes or new commits for review, and removes an unchanged one (in the target repo) together with its new branch — the branch only while it still points at the base — and then removes `<repo name>.vco-lanes/` if, and only if, it is now empty (never recursive). An adopted worktree is never removed.
- **Bash is not confined**: the harness holds only Edit/Write to the worktree, so a lane brief must keep its Bash commands inside the worktree path.

## Install-time env knobs

Set these before running `bash first-install.sh` (or export them for the duration of a CI run):

| Var | Effect |
|---|---|
| `VCT_NO_AUTO_LAUNCH=1` | Skip auto-spawning the launcher GUI at end of `first-install.sh` / `first-install.command`. Equivalent to passing `--no-auto-launch`. Useful for CI, agent-driven installs, or when the GUI will be controlled out-of-band (Xvfb, Playwright). |
| `VCT_NO_DESKTOP_ICON=1` | Skip creating the desktop shortcut after a successful install. Equivalent to passing `--no-desktop-icon`. Linux: `~/.local/share/applications/vct-launcher.desktop` + `~/Desktop/vct-launcher.desktop` skipped. macOS: `~/Applications/VCT Launcher` symlink skipped. Windows: `%USERPROFILE%\Desktop\VCT Launcher.lnk` + Start Menu entry skipped. Useful for CI / unattended installs, or when running multiple VCO installs on the same user account. |
| `VCT_NON_INTERACTIVE=1` | Treat the run as non-interactive. The Python auto-installer wrappers (`install.sh` / `install.ps1`) will fail loudly on missing Python rather than prompting — fix it in your CI image. Implied by `--quiet`. |
| `VCT_CONTAINER_RUNTIME=podman|docker` | Pin the container runtime instead of auto-probing. Useful in CI where both runtimes might be present but only one is configured. A **pin**: if the named runtime is unusable the install refuses with an actionable message rather than silently using the other one (they have separate volumes — see [Container runtime](#container-runtime)). |
| `VCT_STATE_DIR=/path` | Override `~/.vct/` as the launcher state-root. Lets dev launchers run alongside production without contaminating state. Hub binaries pick this up automatically; resolver clients honour it too. |
| `VCT_DISABLE_HOOKS=1` | See section below. |
| `VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY=1` | **Test-only sentinel** consumed by `hub_launcher::find_hub_dist_sibling`. When set, the launcher's hub-binary discovery skips the in-tree dist resolution (the `current_exe()` sibling + arch-less-grandparent walk) and returns `None` if no other candidate matched. **Since v0.2.92 this is a manual override, not the protection**: discovery now refuses BY DEFAULT whenever `current_exe()`'s parent directory is `deps` — i.e. under any cargo test binary — because opt-in safety failed (only this module's own tests ever set the var, so every other test in the workspace resolved `target/debug/vct-hub` and `ensure_hub_running` spawned it against the developer's real `~/.vct`). A shipped launcher never lives in a `deps` dir, and a dev `cargo run` build lives in `target/<profile>/`, not `deps/`, so both keep finding their sibling hub. Production code never sets the var. Do NOT use it as a user workaround for hub-start failures; the correct path for that is `vct-hub --start-if-not-running` (see TROUBLESHOOTING.md). |
| `VCT_RL_PULL_TOKEN_ENDPOINT=<url>` | Runtime override for the RL module's paid-module pull-token gateway URL. Short-circuits the L0 catalog / L1 manifest / hardcoded-default resolution chain inside `installer_engine::request_pull_token` and POSTs the license-key request to `<url>` verbatim. Use when the on-disk endpoint is wrong (manifest still carries a `placeholder.<tld>` URL, tenant has migrated, gateway is being staged behind a custom domain). Empty / whitespace-only values are ignored. |
| `VCT_MODULE_PULL_TIMEOUT_SECS=<n>` | Upper bound, in seconds, on a single module-image `podman`/`docker pull` during a module install or update. Default `1800` (30 min). The bound exists to catch a genuinely *stalled* registry (network black hole, half-open connection, a registry that accepts the connection but never streams layers) — without it, a stalled pull leaves the install row wedged at `status='installing'` forever (the pull future never resolves). On timeout the pull is killed and the install transitions to `status='error'` with an actionable message, then becomes retry-eligible. Raise it for unusually large GPU-variant images on a slow link. A zero, negative, or non-numeric value is **ignored** and the default is used — the bound is never disabled (a stalled pull must always be able to fail). |
| `VCT_INSTALL_DOCKER_TIMEOUT=<seconds>` | Cap on the `compose up -d` step of `install.py` / `install.py --update` (first-run image pulls included). Default `900` (15 min), clamped to a 60 s minimum; a non-numeric value falls back to the default. A hung container daemon fails the step with a message naming this variable instead of blocking forever — raise it (e.g. `1800`) on slow links with a cold image cache where healthy pulls legitimately exceed 15 min. |

The five `VCT_INSTALL_*` names below are **internal — do not set them**. `install.py` sets them itself when it
relaunches under the install's `.venv` (the launcher starts it with the system `python3`, which cannot import the
venv's packages) and reads them back in the relaunched run (`vco_lib/install_companions.py`); they are listed here so
their names are not a mystery in a process listing. They describe ONE hop, so they are never passed on: every
long-lived process `install.py` starts (hub, updater, launcher, background drivers) and every `install.py` the
launcher spawns gets an environment without them — the list both sides strip is `vco_lib/install_relaunch_env.toml`
— and a run whose argv does not carry the matching token ignores and drops any it finds (one stderr line).

| Var | Meaning |
|---|---|
| `VCT_INSTALL_RELAUNCHED` | `1` on every relaunched run — the loop guard: a relaunched run never relaunches again. |
| `VCT_INSTALL_BASE_PYTHON` | The interpreter that STARTED `install.py`, recorded on the first hop only. A venv (re)build uses it, never the venv's own python, so a rebuild follows the Python you launched with and never runs from the tree it deletes. |
| `VCT_INSTALL_BASE_PYTHON_VERSION` | That interpreter's `X.Y`. The venv-drift check compares the `.venv` against it — after the relaunch `install.py`'s own version IS the venv's, so comparing with that would never see drift. |
| `VCT_INSTALL_RELAUNCH_TOKEN` | A fresh random token per relaunch, also passed to the relaunched run as its last argument (`--vct-relaunch-token=<token>`, removed before the arguments are parsed). The environment reaches every descendant; the argument reaches only the run it was made for — so a match proves the other four were set for THIS run, not inherited from an older one through some other process. |
| `VCT_INSTALL_PARENT_WAITS` | Windows only: the **pid** of the `install.py` waiting for this run. Windows cannot replace a process (`os.exec*` starts a new one and ends the caller with exit code 0 at once), so there the relaunch runs as a child and the parent exits with the child's exit code. The child uses the pid twice: a relaunched run that must rebuild the venv it runs from hands the run back to that parent (exit code `22083`, `0x5643`), which runs outside the venv and re-runs it once; and the child watches the parent, so when the parent is killed (the launcher cancelling a run) the run stops at once with exit code `22084` (`0x5644`) and one stderr line — what killing `install.py` does on Linux/macOS. Only the run stops: services it already started (hub, model gateway, updater, analyzer) keep running there too. If the watch cannot be set up, a stderr line says so and the run continues. |
| `VCT_INSTALL_POST_VENV_HANDOFF` | Set only on the run a fresh install continues as once step 5 has filled the venv (v0.2.103): the path of an owner-only temp JSON record (mode 0600 on POSIX; on Windows it is not chmod-ed and the per-user temp directory is the access boundary — the record holds no secret) carrying what the first run already did — its buffered install-log lines, the answers it was given (adopt-project mode, storage layout, the `.env`-secret and lean-ctx offers, `--conflict-strategy`) and its deferral entries — so no prompt is asked twice. The child reads the record once and deletes it; its presence is also the loop guard (a hand-off child never hands off again). Internal — do not set. |

`VCT_SERVICE_ENDPOINT_ROWS_PENDING` is **internal — do not set it** (v0.2.104). On a machine with no
`service_endpoints` rows yet (a first install), `install.py` sets it to `1` just before the post-venv hand-off, so
the run and the children it starts until step [5b] read the compiled default endpoints without warning "no usable
row … run `python install.py --update`" — that step of the same run writes the rows. Step [5b] removes it again
(`vco_lib/service_endpoints.py`, `mark_rows_pending` / `pin_process_env`); from then on a missing row warns as before.

The three `VCT_KG_SEED_CTX_*` names below are **internal — do not set them** (v0.2.101). The install no longer waits
for the knowledge-graph seed: it hands the whole-tree seed to the detached retry driver
(`vco_lib/deferral_retry.py`) and these variables carry, in that driver's environment only, the embedding context the
install resolved, so the driver records that context once the seed has actually succeeded.

| Var | Meaning |
|---|---|
| `VCT_KG_SEED_CTX_ACTIVE_EMBEDDING` | The active embedding profile the install resolved for this seed. |
| `VCT_KG_SEED_CTX_KG_COLLECTION` | The project knowledge-graph collection the seed writes. |
| `VCT_KG_SEED_CTX_SHARED_KG_COLLECTION` | The shared knowledge-graph collection, for the shared seed. |

## Runtime env knobs

Set these in the per-project `.claude/env` (shell-sourced) or `.claude/settings.json` `env` (propagates to MCP subprocesses), or export them for one shell. Unlike the table above they are read at use-time, not at install-time.

| Var | Default | Effect |
|---|---|---|
| `VCT_RESYNC_SPAWN_DISABLED` | unset (spawn allowed) | Truthy (`true`/`1`/`yes`/`on`, case-insensitive) → `vco_lib/codegraph_resync.py` never spawns a background analyzer child. The gate is checked **before** the per-spawn log file is created and before any `Popen`, so a disabled run also stops writing `~/.vct/logs/resync-*.log` records. For CI runners, air-gapped installs, and anyone who wants code-graph walks strictly on demand. `tests/conftest.py` sets it for the whole suite (with an explicit opt-out list for the tests that assert spawn behaviour), the same convention as `RL_HUB_POST_DISABLED`. |
| `VCO_RESYNC_DRIVER_SLOTS` | `2` | Machine-wide cap on background code-graph resync drivers that RUN at once, across all projects (`vco_lib/codegraph_resync.py`, v0.2.102). Each driver walks a whole project and embeds through the one GPU / Weaviate, so an "Update all" over N projects that each owe a re-index used to run N walks at once. A driver beyond the cap QUEUES for a slot (one log line says so) and is never dropped. Slots are flock files under `<vct_root>/state/resync-driver-slots/`, released when the holder exits or crashes; on Windows or a filesystem without flock there is no cap. Values below `1` count as `1`; a fractional value is truncated; a non-numeric value means the default. Separately, at most one driver per project runs at a time (same flock caveat): a second one for the same project logs a line and exits 0. The driver is a detached child of whatever spawned it (install, bundle update, launcher), so set this in that process's environment. |
| `VCO_RESYNC_DRIVER_WAIT_S` | `21600` (6 h) | The wait period of a queued resync driver. Every slot holder records itself (`slot-<i>.holder.json`: pid + process start time). When the period expires the driver does NOT run over a live holder: it keeps waiting, logging `still queued` once per period, and takes over a busy slot only when that slot's holder is provably dead — its pid is gone, or now belongs to a different process (start time differs). Unknown liveness counts as alive. Takeover runs under a non-blocking `slot-<i>.takeover.lock`, so of many expired drivers exactly one takes a given slot. `0` means a dead holder's slot may be taken over at once; live holders are still waited for. Negative values count as `0`; a non-numeric value means the default. Known limit: a narrow overlap window remains — a crashed holder's leftover record can be read in the instant before a new holder of the freed slot rewrites it, or a dead holder's lock may survive in a still-running child; either way one extra driver runs over the cap until one finishes (see `vco_lib.atomic.machine_slot`). |
| `VCO_RL_EMIT_INLINE` | unset (detached send) | Truthy (`1`/`true`/`yes`/`on`) → a hook KG search (`rl_kg_search.py --hook-format`) sends its RL retrieval events (primary + dual-log twin) in its own process, after printing its results, instead of handing them to a detached child (`claude_mcp_servers/rl_client/deferred_emit.py`). A diagnostic switch for watching the hub write path in the foreground; the hook then waits for the POSTs again. |
| `VCO_RL_EMIT_ORPHAN_TTL_S` | `900` | Seconds after which an abandoned `vco-rl-emit-*.json` payload in the temp dir (a deferred-emit child killed by SIGKILL/OOM before it could send or clean up) is reaped by the next `hand_off`: one `deferred_unsent`/`orphaned_payload` line goes to the RL loss ledger and the file is deleted. Deliberately generous so a merely slow-to-start child is never mistaken for a dead one. |
| `VCO_RL_IDENTITY_CACHE_TTL_S` | `2592000` (30 days) | Freshness bound on the cross-process RL project-identity cache (`<vct_root>/rl_project_identity_cache.json`, keyed by project root). When the hub's project-config resolve misses, the RL telemetry writer reuses the last RESOLVED identity for the same root instead of stamping the env display name with `project_id=NULL`; an entry older than this is ignored and the env fallback (which warns once per process) applies. |
| `VCO_RETRY_ATTEMPT`, `VCO_RETRY_ATTEMPT_DRIVER_TOKEN` | (set by the retry driver) | Internal, do not set (v0.2.104). The deferral retry driver (`vco_lib/deferral_retry.py`) sets them around one dispatched retry so the handler's children inherit them: the attempt id (recorded on the attempt's `started` row in `.claude/logs/deferral-retries.jsonl`) and the driver's process start token. When a child clears the ledger entry, its `resolved` row carries the attempt id and whether that driver is still alive, so a live attempt's own clear does not reset the retry cap while a clear made after the driver died (killed, reboot) ends the episode. |
| `VCO_RL_TASK_TYPE` | unset (`cli_kg_search`) | Internal, do not set: each hook exports it so `rl_kg_search.py` tags its RL events `pre_edit_kg_search` / `pre_bash_kg_search` / `pre_tool_use_kg_search` / `subagent_kg_search`. `--task-type` wins over it; an unknown value falls back to `cli_kg_search`. |
| `VCT_EDIT_POST_CHECK_SECS` | `20` | How long, in seconds, the background poller started by `post-edit-outcome` waits for a PostToolUse checker's result before it writes the `edit_outcome` RL row (v0.2.102). The checker (today only `py-compile-check`, for a `Write` of a `.py` file inside the project) records ok/fail in `.claude/state/last_compile_status_<pathkey>_<agentkey>.json`, and the poller copies it into the row's `post_check`. Nothing arrives inside the window → `post_check` is `no_signal` (reason `timeout`). An edit no checker covers gets `no_signal` (reason `no_checker`) at once, without waiting. `0` checks once and does not wait; values above `300` are clamped to `300`; a non-integer falls back to `20`. The row is always written: if the poller cannot start or fails, `post_check` is left empty (`None`). Read by `vco_lib/edit_post_check.py`. |
| `VCT_ALLOW_FIXTURE_CLASS_WRITES` | unset (fixture-named writes refused) | Truthy (`1`/`true`/`yes`) → this process may write Weaviate classes whose project stem is one of VCO's own TEST FIXTURE names (`Alpha`, `Beta`, `Gamma`, `Foo`, `Bar`, `Baz`, `Foobar`, `Quux` — the table in `vco_lib/fixture_class_guard.py`). Unset, such a write is REFUSED with a named error naming the class and this variable. It exists because a fixture name reached a real backend: `Alpha_KnowledgeGraph` on the maintainer's live Weaviate held 70 real knowledge nodes nothing reads. `tests/conftest.py` sets it for the whole suite (same convention as `VCT_RESYNC_SPAWN_DISABLED`); an ad-hoc probe harness that genuinely owns such a class must set it deliberately — and should also point `WEAVIATE_URL` at a disposable instance or the unroutable sentinel `http://127.0.0.1:9`, which is what the suite pins by default. If a fixture name really is your project's name, set this — and consider renaming, since VCO's fixtures use it too. |
| `VCT_BOOT_SMOKE_REAL_DISPLAY` | unset (real display refused) | Maintainer/CI knob for `scripts/launcher-boot-smoke.sh` (the pre-ship gate's launcher boot smoke). The smoke runs the real launcher binary headless under `xvfb-run`; without one it exits 3 with the install hint instead of opening a window on the operator's live desktop — on GNOME/X11 that window flash crashed gnome-shell and ended the whole session twice on 2026-09-09. Set to exactly `1` to opt in to the real display anyway (platforms without Xvfb), at your own risk. Not read by any shipped runtime path. |
| `VCT_BOOT_SMOKE_XVFB_RUN` | unset (probe PATH + known install locations) | Same script: an explicit path to an `xvfb-run` outside the probed locations, used as-is; the literal `none` disables probing (the smoke's own tests use it to exercise the refusal on machines that have xvfb). Not read by any shipped runtime path. |
| `VCT_DISK_SPACE_MIN_FREE_GB` | `2` | Free-space floor, in GiB, for `vco doctor`'s disk-space probe (install root + vct state dir, deduplicated by filesystem). Strictly below the floor the finding is a warning; below 256 MiB it is critical; exactly at the floor is `ok`. Fractional values (`0.5`) are legal; a malformed or non-positive value falls back to the 2 GiB default and does **not** disable the check. Below the floor also emits the `disk_space_low` deferral condition — see [`features/05-install-and-secrets.md`](features/05-install-and-secrets.md#disk-space-probe). |
| `VCO_QUERY_ENRICH` | unset (enrichment on) | Set to exactly `off` to disable hook query enrichment — the retrieval query is then the bare trigger, as before v0.2.92. Note this knob takes the literal string `off` only; unlike the breaker's kill switch it does not also accept `0` / `false` / `no`. |
| `VCO_QUERY_ENRICH_SHORT_TOKENS` | `24` | A trigger shorter than this many tokens is considered too thin to retrieve on, and gets enriched with the previous turn's context. Raise it to enrich more aggressively, lower it to enrich almost never. Unparseable values fall back to the default. |
| `VCO_QUERY_ENRICH_SHARE` | `0.5` | Fraction of the embedding model's chunking-preset budget the added context may occupy, clamped to `[0.0, 1.0]` (the clamp applies to the resolved value, so an out-of-range kwarg is clamped too). The trigger always comes first and is never truncated to make room. Unparseable values fall back to the default. |
| `VCT_VSCODE_SETTINGS_FILES` | unset | `os.pathsep`-separated list of absolute `settings.json` paths that replaces the launcher's VS Code-variant discovery when flipping the editor panel to the model gateway (`vco_lib/vscode_settings.py`). Set it for portable installs, `--user-data-dir` setups, or any VS Code-family editor the variant table does not know by name. The launcher's Services page names this variable when discovery finds no target. |
| `VCT_CODEGRAPH_FORCE_REWALK` | unset | Env form of `analyze_code_graph.py --force-rewalk`: bypasses ONLY the per-FILE staleness gate, so the next walk re-parses every file. The per-ENTITY content-hash gate still runs — a converged project re-walks but re-embeds nothing. VCO's own background extractor-generation resync sets it (env survives the two process hops to the analyzer, where a CLI flag would not); set it by hand to force a full re-walk, e.g. after an extractor bug shipped stale rows. |
| `VCT_LAUNCHER_DB_PATH` | `<VCT_STATE_DIR or ~/.vct>/launcher.db` | Overrides the launcher-database location for every VCO-side reader (`vco_lib.paths.launcher_db_path`: install.py's config projection, `project_init`, the read-only `launcher_db_reader`). One canonical resolver since v0.2.54 — before that only the reader honoured it, so the reader and the writers could disagree about which DB they were looking at. Set it when the DB genuinely lives outside the state root; symlink `~/.vct` instead for whole-state relocation. |
| `VCT_BASH_KG_THRESHOLD_CHARS` | — | **RETIRED in v0.2.101** (injection redesign §C1): `pre-bash-context-inject` no longer reads it. The 500-character length gate was replaced by INTENT CLASSIFICATION — only READ/EDIT/SEARCH-classified commands query and inject (queries are built from the command's target paths/symbols, never its text), and MECHANICAL commands (build/test/`git status`/`ls`/…) spawn nothing at any length. To quiet the injection surfaces entirely, use `VCO_INJECT_PROFILE=off` (below) or the launcher's per-project Hooks tab. |
| `VCO_QUERY_CACHE_TTL` | `900` (15 min) | Seconds a warm query-cache entry under `.claude/state/query_cache/` is replayed instead of re-querying the KG / code graph. Two readers share the knob and the directory: the injection router's Python cache (`kgi`/`cgi` namespaces — `hook_context_router.py`) and the pre-edit wrapper's per-file replay cache. **Empty results are NEVER cached** (v0.2.101): an empty blob is indistinguishable from a leg killed by its inner timeout, and caching it used to suppress every retry of that query for the whole TTL; a leftover empty entry from an older version reads as a miss and is removed on first touch. Entries are GC'd at twice this age; any cache error falls back to a live query (best-effort, never breaks injection). (The pre-v0.2.101 shell cache library `_lib/query-cache.{sh,ps1}` was retired with its last callers.) |
| `VCO_INJECT_PROFILE` | unset (injection surfaces on) | Kill switch for the v0.2.101 injection redesign: set to `off` (case-insensitive) and the context router (`hook_context_router.py`) plus the injection wrappers exit silently — no query, no injection, no RL retrieval event. Since v0.2.102 it also silences the agent and skill **suggestions** added to a subagent's brief on the `agent` surface (they replaced the retired `subagent-start-suggest` hook and are produced by the same router run), so with it set a subagent receives neither the KG block nor the suggestion lines. The main-chat `agent-skill-keyword-suggest` UserPromptSubmit hook is a separate hook and is not affected. Checked beside `VCT_DISABLE_HOOKS` (which remains the all-hooks switch). |
| `VCO_PROMPT_INJECT` | unset (prompt surface on) | Per-surface kill switch (v0.2.102 W6-F): set to `off` (case-insensitive) to disable ONLY the UserPromptSubmit prompt-time KG injection (`prompt-context-inject.{sh,ps1}` → the router's `prompt` surface). Unlike `VCO_INJECT_PROFILE`, every other injection surface (read/edit/write/bash/grep/agent-brief) keeps working. Exists because the prompt surface spawns the router on every prompt, where its latency sits on the critical path; the wrappers check it before spawning anything and the router re-checks it (the enforcement home). |
| `VCO_PROMPT_MIN_CHARS` | `100` | v0.2.102 W6-K (owner ruling 2026-10-11): the minimum trimmed length a prompt must reach before the UserPromptSubmit KG search runs at all — shorter prompts ("ok to proceed", "start the review") spawn no search. One threshold, one home: read only by `vco_lib.inject_intent.prompt_query` (both wrappers and the router go through it). A non-positive or unparseable value falls back to 100; setting it to 1 re-enables a search on nearly every prompt (use `VCO_PROMPT_INJECT=off`, not 0, to disable the surface). |
| `VCO_KG_REVIEW_MAX_TURNS` | `30` | v0.2.102 W6-N: the turn budget (`claude -p --max-turns`) of the background post-commit KG reviewer (`post-git-commit-kg-sync` → `.claude/scripts/kg_reviewer_backend.py`), applied to every rung of its fallback chain — the chosen Claude model and each gateway model alike. The pre-W6-N 10 cut off about half the logged reviews. The value must be plain digits (no sign, blanks or `_`); a non-positive or non-numeric value falls back to 30. Each run logs the turns it used (`turns=N subtype=…` in `.claude/logs/kg-commit-review.log`), which is what to read before changing it. A run that ends on this budget (`subtype=error_max_turns`) is not counted as a backend failure by the fallback chain. Since v0.2.103 a broken install (no python, or the chain script missing) does not review the commit at all — it is reported on stderr, in that log and in `kg-commit-reviews.jsonl` — because the reviewer's safety guard is Python. |
| `VCO_KG_REVIEW_MODEL` | unset (the launcher preference, else `sonnet`) | v0.2.103 (owner ruling 2026-10-15): the Claude model of the post-commit KG reviewer — ONE machine-wide setting, choices `haiku` / `sonnet` / `opus` (case-insensitive). This env var outranks the launcher's **Preferences → KG commit reviewer** selector (`app_state` `kg_reviewer.model`); with neither set the default is `sonnet`. Any other value is ignored with a log line. The choices, the default and the key live in ONE table, `vco_lib/kg_reviewer_config.toml`, read by the resolver (`python -m vco_lib.kg_reviewer_model` prints the model and which rule chose it) and by the launcher selector. When the chosen model is unavailable (no `claude` CLI, or its tier latched on a real quota / 429 / auth signal) only a comparable gateway model may stand in — `glm-5.3`, then Kimi `k3`, each only when the local model gateway serves it — never a flash model; then OpenAI when OpenAI for summaries is enabled (review-only, writes nothing); otherwise the review is skipped with a log line. The reviewer writes only under `knowledge/`, with Write/Edit only (tool permission rules plus a post-run validator that replays the reviewer's writes from its transcript and, on bytes provably its own, reverts any edit outside `knowledge/` or through a symlink, a removed, rewritten or added frontmatter, a new node without full frontmatter or with an existing node's title, a backticked `--flag` found in nothing it read, or an edit of a locked node — the locked-node tags/types/statuses are the `[guard]` table in the same file; a file the user changed during the run is never reverted or committed, and every revert backs up the current bytes under `.claude/backups/kg-reviewer/`) and commits only its own nodes as `kg-sync: <hash> <subject>`; see the hook description in `docs/features/03-agents-skills-hooks.md`. |
| `VCO_INJECT_BUDGET_S` | `6` | Whole-run inner bound, in seconds, of the context router. Ordered under the 10 s injection-surface hook timeouts with headroom for interpreter/producer startup + emit on SLOW hardware (owner ruling 2026-10-06: this interactive class is deliberately bounded and silence-safe — a cold run that still overruns the harness timeout injects nothing and fails open; this knob is NOT a pattern for long-operation timeouts, which stay generous). The bound is counted from the moment the hook started (the router's interpreter start, or the wrapper's own start when the wrapper exports `VCO_HOOK_STARTED_AT`), so start-up and producer imports spend it too; a run that misses it ends at once, even when a producer left worker threads behind. A non-positive or unparseable value falls back to 6. |
| `VCO_INJECT_LEG_TIMEOUT_S` | `4` | Per-leg join bound, in seconds, inside the router (KG and code-graph legs run concurrently; a leg still running at the bound is abandoned, its result treated as empty, and its late stdout writes fenced off by the `_PostLegStdout` guard). Falls back to 4 on a non-positive/unparseable value. |
| `VCO_WAIT_GUARD` | unset (guard on) | Kill switch for the v0.2.102 wait-loop / polling guard (`vco_lib/wait_guard.py`, hooks `wait-guard.{sh,ps1}`): set to `off` (also `0` / `false` / `no`, case-insensitive) and every check stops — foreground `sleep` waits, `watch`/`tail -f`/`timeout … bash -c '…sleep…'` polling shapes, PowerShell `Start-Sleep` / `Get-Content -Wait` / foreground `Wait-*` and loops judged by the SAME rule as Bash (the guard runs on every PowerShell call because no `if` rule can match a loop keyword): a loop containing a wait (`Start-Sleep`, a `Wait-*` cmdlet, `[Threading.Thread]::Sleep(n)`, the Windows sleep shapes `ping -n <n>`/`timeout /t`), an unbounded loop — `while`, `do … while/until`, `for (;;)`, an unbounded `foreach` range — whose body does no real work (`> $null` and `| Out-Null` write nowhere), or a condition-refresh poll, is denied; a loop doing real work — assignments, increments and method calls ARE work (`while ($i -lt 5) { $i++ }`, `foreach ($f in Get-ChildItem) { ruff check $f }`) or bounded by an input-consuming condition (`while (($l = $r.ReadLine()) -ne $null) { $l }`) passes, the self-wake tools (`ScheduleWakeup`/`CronCreate`/`Monitor`), and the repeat detector all allow. Not every `sleep` is blocked: since v0.2.103 a `sleep` with a LITERAL numeric duration (`sleep 3`, `sleep 0.5`, `sleep 2s`; `Start-Sleep -Seconds 2`) placed BETWEEN other segments of one command (`start-x && sleep 3 && curl …`, or a `bash -c` whose sub-chain is a sole sleep while the `bash -c` itself sits between actions) is allowed as a fixed pause — a sleep inside a loop body or as the loop CONDITION (`while sleep 2; do …; done`; `bash -c` recursion included, also behind a `do bash -c …` segment or subshell parens), a non-literal duration (`sleep $N`, `sleep infinity`), a sole wait hidden in a subshell `( sleep 5 )` / command substitution `echo $(sleep 5)` (an assignment value too: `x=$(sleep 5)`) / process substitution (`diff <(sleep 5)`) / xargs per-item command (`xargs -n1 sleep`) / function-body loop / a literal `eval '<wait>'` body (re-parsed like a `bash -c` body; a non-literal `eval "$cmd"` is not), or a sleep that is the only/first/last segment of the command is still denied. Since v0.2.104 the TEXT of a wait is not a wait: when every command of the line is a program that never runs its arguments as code (`grep`/`egrep`/`fgrep`, `cat`, `head`, `wc`, `uniq`, `cut`, `tr`, `ls`, `echo`, and with per-program option checks `tail`, `find`, `git` read subcommands, `rg`, `ag`, `ack`, `sort`, `printf`) and the `$(…)`/backtick text sits only inside single quotes, `$'…'` or a backslash escape (`grep -n '$(sleep 5)' f`), the line is allowed — unless an argument or redirection target is not a plain word (an unquoted glob, `~`, `$VAR`, `$"…"`, a `$'…'` with an escape) or names a device or `/proc` file that can block (everything under `/dev/` but `/dev/null`/`/dev/stdout`/`/dev/stderr`, any letter case); the exact rules are in `vco_lib/wait_guard_literal.py`. An unquoted newline is a command separator, so a multi-line command is judged like its one-line form (heredoc bodies are stripped first — a body line is data). A function called in the same command has its body judged by the same rule within the body, brace- or subshell-bodied alike (`f() { sleep 1; }; f` and `f() ( sleep 1 ); f` denied, `f() { a; sleep 1; b; }; f` allowed, a definition with no call left alone, a call inside a loop puts the body in the loop). `cmd & wait` is allowed (it runs `cmd`; the builtin blocks once in-process — not a poll). Since v0.2.103 a `while`/`until` loop whose body does no real work is also denied as a **busy-wait loop** even with no sleep in it — every body segment empty, `:`, `true`, or a bare `echo`/`printf` with no output redirection and no pipe (fd duplications and `/dev/null` sinks write nowhere: `printf . >&2` is not work); a body with real work stays allowed (`for f in *.py; do ruff check $f; done`, `while read l; do echo "$l" >> out; done < in` — a redirected echo accumulates output), a condition that consumes input (`while read`/`mapfile`/`readarray` — never `until read`, `while ! read` (they spin at EOF), a timed `read -t <n>` (it waits by polling) or a piped producer (`while curl | read` re-runs every pass)) bounds the loop at EOF and is never a busy-wait, `for`/`select` over a word list and a bounded arithmetic `for ((i=0; i<n; i++))` are finite, and the arithmetic `for ((;;))` with an empty middle clause is `while true` in disguise and is judged like one; a loop whose ONLY work is re-fetching the value its own condition tests is a **condition-refresh poll** and is denied in both shells (`until [ "$r" = 200 ]; do r=$(curl -s u); done`, PowerShell `do { $r = Invoke-WebRequest u } until ($r.StatusCode -eq 200)`), while counted loops (arithmetic on the condition variable) and bodies with other real work stay allowed (multi-line bodies judge like single-line ones: the walker carries each statement's assignment on the segment, so a body line `r=$(curl -s u)` is a fetch wherever it sits, and a counted loop's `i=$((i+1))` line is work). **A USER decision**: the deny message names it for the user only — an agent must not set it to get past the guard. Checked beside `VCT_DISABLE_HOOKS` (all hooks). |
| `VCO_WAIT_GUARD_REPEAT_MAX` | `4` | The Nth identical read-only status check (`tail`/`head`/`cat`/`ls`/`wc`/`grep` on the same target, `Read` of the same file_path+offset+limit) within the window that the wait-loop guard DENIES; checks 1..N-1 pass. A command that WRITES never counts (since v0.2.103: a real write target — an output redirection, a heredoc-to-file — decided by the one write-target parser in `vco_lib/bash_write_targets.py`, with `/dev/null` and fd duplications counting as no write, so `cat > f <<'EOF' …` keyed on `cat` is not a poll while `tail -5 log | tee /dev/null` still keys). A non-positive or unparseable value falls back to 4. |
| `VCO_WAIT_GUARD_REPEAT_WINDOW_S` | `600` | The repeat-detector window, in seconds: identical checks older than this are pruned from the per-session ephemeral state (under `$VCT_STATE_DIR/wait_guard/`), so the count resets. A non-positive or unparseable value falls back to 600. |
| `VCO_ROUTER_KG_SCRIPT` | sibling `rl_kg_search.py` | Internal, do not set in production: explicit path to the KG producer module the router loads (a test seam for stub producers). |
| `VCO_CG_SCRIPT` | `<project>/.claude/scripts/query_code_graph.py`, else the checkout's `templates/scripts/query_code_graph.py` | Internal, do not set in production: explicit path to the code-graph producer module the router loads (a test seam for stub producers). |
| `VCT_PB_STATE_FILE`, `VCT_PB_TASK_ID`, `VCT_PB_START_TS`, `VCT_PB_SESSION`, `VCT_PB_HASH`, `VCT_PB_LEN`, `VCT_PB_INTENT`, `VCT_PB_TARGETS`, `VCT_PB_SYMBOLS`, `VCT_PB_INTENT_FILE`, `VCT_PB_TUID`, `VCT_PB_CLAIM_FILE`, `VCT_PB_LOSS_REASON`, `VCT_PREBASH_INTENT`, `VCT_PREBASH_TARGETS`, `VCT_PREBASH_SYMBOLS` | unset | Internal, do not set: per-invocation hand-off variables `pre-bash-context-inject.{sh,ps1}` passes to its OWN embedded-Python children (the `bash_task_*.json` state writer and the backgrounded `pre_bash` outcome-event emit) so command text with quotes/newlines can never break the child's source. Same class as the pre-existing `VCT_PREBASH_QUERY`/`VCT_PREBASH_TASK_ID` family; scoped to one hook run, never read by anything else. The v0.2.101 additions (`*_INTENT`/`*_TARGETS`/`*_SYMBOLS`) carry the WP-D 2 classification into both payloads. The v0.2.102 additions: `VCT_PB_TUID` carries the sanitized `tool_use_id` into the state writer (W3R-3 per-call pairing), and `VCT_PB_CLAIM_FILE` / `VCT_PB_LOSS_REASON` are the `post-bash-context-record.{sh,ps1}` equivalents — the pairing claim path handed to the `bash_outcome` emit child and the reason handed to the `outcome_unsent` loss-ledger fallback (W3R-4). |
| `VCO_KG_SYNC_DEBOUNCE_SECONDS` | `5` | Quiet window, in seconds, that the kg-sync debounce waits after a `knowledge/**` edit before syncing. `0` disables debouncing — every edit syncs immediately (the pre-2026-06-18 behaviour). A non-numeric value falls back to 5. |
| `VCO_CODEGRAPH_DRAIN_MIN_INTERVAL_SECONDS` | `120` | Per-project rate limit, in seconds, between end-of-turn code-graph drains (`stop-codegraph-drain`). A second Stop inside the window skips the drain. A non-numeric value falls back to 120. |
| `VCO_CODEGRAPH_EXTRAS_CHECK_INTERVAL_SECONDS` | `600` | How often, in seconds, the Stop hook (`stop-codegraph-drain`) checks the project's enabled extra code-graph paths for a moved repo HEAD. A check spawns `python -m vco_lib.codegraph_extras_refresh` detached; it runs whether or not the turn edited any file. A non-numeric value falls back to 600. |
| `VCO_CODEGRAPH_EXTRAS_MIN_INTERVAL_SECONDS` | `3600` | Per-path throttle, in seconds, between automatic re-index ATTEMPTS of one stale extra code-graph path (`vco_lib.codegraph_extras_refresh`). A failed attempt is also throttled, so a broken path is not retried every turn. Decisions are logged to `.claude/logs/codegraph_extras_refresh.log`. |
| `VCO_SUBAGENT_RECONCILE_INLINE` | unset (detached reconcile) | Truthy (`1`/`true`/`yes`/`on`) → `subagent-stop-reconcile` does its work (worktree reap, diff, KG/docs sync hand-off, credential scan, parent alerts) inside the SubagentStop hook process instead of handing it to one detached job. A diagnostic switch (and the test suite's pin, for deterministic assertions); the detached default exists so no harness timeout can kill a slow reconcile. |
| `VCT_SUBAGENT_MAX_DIFF` | `500` | Cap on the changed-file list `subagent-stop-reconcile` computes from a subagent's snapshot, protecting the reconcile consumers (KG-sync, code-graph drain, credential scan) from a runaway tree-wide diff (e.g. a subagent that ran a formatter pass). |
| `VCT_SNAPSHOT_DIRS` | `knowledge docs src lib launcher claude_mcp_servers .claude/scripts vco_lib templates tests` | Space-separated directories (relative to the project root) the subagent snapshot library walks. Missing directories are skipped. The same list drives the snapshot AND the diff, so override it in both places' env (any process that runs either). |
| `VCT_SNAPSHOT_CODE_EXTS` | `py\|rs\|ts\|tsx\|js\|jsx\|go\|java\|cs\|c\|cpp\|h\|hpp\|rb\|php\|swift\|kt\|scala\|sh\|ps1\|sql` | Pipe-separated code-file extensions the snapshot walk tracks (`.md` files under the snapshot dirs are always tracked). Extend it when your language is missing from the default. |
| `VCT_SNAPSHOT_PRUNE_DIRS` | `target node_modules .git .wt __pycache__ .venv dist build .next .svelte-kit .pytest_cache .mypy_cache .ruff_cache` | Space-separated directory basenames pruned from the snapshot walk (build / VCS / cache trees). Applied identically at snapshot and diff time so the two stay comparable; add a differently-named build tree here. |
| `VCT_SNAPSHOT_GC_DAYS` | `3` | Age, in days, at which orphaned subagent snapshots under `.claude/state/` are garbage-collected (a killed agent whose Stop hook never fired would otherwise leak its snapshot forever). Live agents' snapshots are always fresh enough to be untouched. |
| `VCT_WORKTREE_GUARD_ENFORCE` | unset | `=1` hard-blocks (exit non-zero) only the belt-and-suspenders branch of `worktree-guard` — a payload that supplies an explicit worktree path equal to the parent checkout. Ordinary spawns create an isolated worktree unconditionally; this flag does not gate that. |
| `VCO_STOP_FAILURE_NOTIFY` | `1` | Per-hook kill switch for the StopFailure desktop notification: `0` suppresses ONLY the notification. The metrics ledger (`failures.jsonl` in the metrics directory — `$VCT_STATE_DIR/metrics`, default `~/.vct/metrics`) still records **every** failure, suppressed ones included — that ledger is what made a 2026-09-20 field storm of 304 identical pop-ups diagnosable, so it is never gated. Since v0.2.100 that holds without Python too (the hook writes a `"ledger_writer": "shell-fallback"` line itself), and a line that cannot be written is reported on the hook's stderr. Distinct from `VCT_DISABLE_HOOKS`, which disables all hooks. Read by `stop-failure-notify.{sh,ps1}`, not by any daemon. |
| `VCO_PRE_TOOL_USE_TIMEOUT_S` | `3` | Whole seconds; MUST equal the `timeout` registered for `pre-tool-use` in `.claude/settings.json` (3 in the shipped settings). The hook hands its post-compaction transcript check only what is left of that budget (budget − elapsed − 0.7 s, at most 1 s) and blocks without starting Python when under 0.2 s is left, because a PreToolUse hook killed on its timeout does not block the tool call. The same value decides the hook's incomplete-run warning: a run that finished (allowed or blocked) at or past the budget keeps its `.claude/state/ptu_inflight_*` marker, because its answer was almost certainly discarded (within a few milliseconds of the budget the harness and the hook can disagree either way), and a same-session marker older than the budget plus 2 s counts as overdue and is reported to the model on the next call. Set it (in `.claude/settings.json` `env`) only when you change that registration. Read by `pre-tool-use.{sh,ps1}` (both uses); read as decimal (`08` is 8); anything other than a whole number from 1 to 999999 (empty, non-integer, `0`, over-range) reads as `3`, silently. |
| `VCO_HOOK_CONTEXT_CAP` | `9500` | Maximum characters a VCO hook injects into the model's context in one call (`_lib/emit-context.{sh,ps1}`: every `additionalContext` envelope, plus the plain-stdout injectors `compact-context-reinject` and `diff-context-inject`). Claude Code shows the model only a file path and a 2 000-character preview of hook output past 10 000 characters, so longer output is cut here and ends with a marker naming the file to Read for the rest. Clamped to `1000`..`10000`; a non-numeric value falls back to 9500. Since v0.2.102 the compact re-inject budgets each of its sections inside this cap (the committed table `_lib/reinject-budgets.txt`), truncating within a section with the same cut marker, so an oversized `CONTEXT_STATE.md` can no longer crowd the plan / commits / snapshot sections out of the model's context. Lowering this cap scales the table's fixed section budgets down proportionally (each floored at 300 chars), so every section stays represented at any value down to 1650; below that the whole-stream cut moves inside the later sections — five sections cannot all keep their headers in less. Table rows must be positive integers: a `0` row is rejected with one stderr line and its section runs uncapped. |
| `VCO_PTA_VCO_ROOT` | (set by the hook) | Internal, do not set. `post-tool-use-async.sh` / `.ps1` pass the resolved VCO root to their embedded Python helper through this variable (v0.2.102); it exists only inside that one child process. |
| `VCO_SNAPSHOT_MAX_AGE` | `1800` | Freshness window, in seconds, for `pre-compact-snapshot.md` at `compact-context-reinject` time (v0.2.102). `pre-compact-save` now runs before every compaction (`/compact` included), so the snapshot should be minutes old at most; when it is older than this window the re-inject does NOT replay it and prints one line naming when it was saved and where to Read it — defence in depth against a PreCompact hook that failed to run. The window is deliberately generous (compaction summary generation is one model request); `0` makes any existing snapshot read as stale; a non-numeric value falls back to 1800. |
| `VCO_SNAPSHOT_FILELIST_N` | `30` | v0.2.102 W6-K (owner ruling 2026-10-11): how many files the pre-compact snapshot's "Recently Modified Files" section lists — the N most recently modified files (`.py`/`.md`/`.json`/`.yaml`, newest first) since the LAST COMPACTION (`.claude/context/last-compact-marker`), or since the chat's start (the transcript's first-entry timestamp) when no compaction happened yet. Read by both `pre-compact-save` siblings; a non-positive or unparseable value falls back to 30. |
| `VCO_PRECOMPACT_HOOK_STDIN` | (set by the hook) | Internal, do not set. `pre-compact-save.sh` passes its already-read hook payload to the embedded Python that builds the snapshot's file list (stdin there carries the script itself, so the payload travels by env for exactly one subprocess hop). |
| `VCO_CODEGRAPH_EXTRAS_LIST_TTL_SECONDS` | `300` | How long (seconds) the edit hooks trust their cached list of a project's enabled extra code-graph paths (`.claude/state/codegraph_extra_paths_cache.txt`) before asking the launcher again; the cache is also refreshed whenever `launcher.db` is newer than it. Lower it if extra paths change often; the list only decides which refresh queue an edit goes to. |
| `VCO_FIND_PYTHON_PATH_ONLY` | unset (venv probe active) | Set to `1` to make `_lib/find-python.{sh,ps1}` (the shared helper hooks source to locate a Python interpreter) SKIP the orchestrator-venv probe and resolve from `PATH` only — the pre-v0.2.102 behaviour. It exists for hermetic test harnesses that simulate a machine with no interpreter at all: the venv ladder's clone-relative tier cannot be defeated by unsetting env vars when the checkout under test carries its own `.venv` (CI does exactly that), so the suite sets this to keep those simulations honest. A user who deliberately wants PATH resolution can set it too; everything else (including which interpreter `pre-tool-use` runs its guard with) is unchanged. |
| `VCO_UPDATE_STALL_WARN_SECS` | `600` | During a **GUI** orchestrator update, seconds of silence on `install.py`'s stdout before the launcher surfaces an "update may be stalled" notice — once per silent window, repeating only while the silence continues. The heartbeat is any stdout line, including the progress ticks install.py relays from its KG-sync child, so a slow-but-progressing re-embed never trips it. `0` disables the notice; values below 30 s clamp up; a garbage value falls back to 600. **The notice never aborts, kills or times anything out** — it tells you where to look, and a CLI `python install.py --update` prints its own output anyway. |

### Model gateway (`claude-gw`)

The local model-gateway daemon (`claude_mcp_servers/model_router/`, default port `11436`) is started, stopped and boot-registered by the launcher (Services page). Every knob below is optional and read at daemon startup by `model_router/config.py`; a healthy install needs none of them.

The daemon serves `/health` (unauthenticated liveness), `/usage`, `/usage/windows`, `/v1/models`, `/v1/messages` and
`/v1/messages/count_tokens`; all but `/health` require the host token and every route is loopback-only.
`/usage` returns the newest token-accounting row per chat (`?session=<id>` for one of them); the rows are
appended to `<vct-state-dir>/metrics/gateway-usage.jsonl`, whose path and counters also appear as
`usage_ledger` in `/health`. The ledger is for CONTEXT management, not cost — no price is recorded anywhere.

**Subscription usage — `/usage/windows`.** With the panel on the gateway, Claude Code's Account & Usage view shows a
dollar figure priced at API rates, which means nothing on a subscription. `/usage/windows` (host token, loopback)
answers the subscriptions' own windows instead: Claude's 5-hour, weekly and per-model weekly (e.g. Fable) windows
from `api.anthropic.com/api/oauth/usage` under your Claude login plus the `anthropic-ratelimit-unified-*` headers on
every relayed answer; Z.ai's 5-hour and weekly windows from its monitor endpoint (`quota_url` in
`model_router/vendors.py`). QwenCloud's Token Plan and Kimi Code publish no programmatic quota, so they contribute no row at all:
the gateway still counts the tokens it relayed for that vendor (the number rides the snapshot, and `?format=json`
carries it) but no surface renders it — it measures what this one gateway routed, not the account's plan usage, and
a figure that means something different from every other number on the card is worse than no figure. All the vendor
sources are undocumented: anything missing, unreadable, older than 30 minutes or past its own reset reads `null`
("unknown") with the reason, never 0 % or 100 %. The answer comes from the gateway's cache, which is refreshed in the
background at most once every 4–5 minutes (jittered, one refresh in flight at a time) when either a read finds it due
or chat requests are passing through the gateway — the second keeps the numbers warm, so a session started during
active use finds them fresh in its model picker. There is no timer: a gateway with no chat traffic and no readers
makes no vendor calls, and no request ever waits on a vendor. `?format=line` answers one line of text — what the status-line script prints — and
the launcher's home page shows the same data as bars with reset countdowns.

**Cache-age indicator and per-agent attribution.** Claude Code's "Prompt cache warm, about N min left" hint reads the
`cache_read_input_tokens` / `cache_creation_input_tokens` usage fields, which vendor routes state rarely and not always
under those names. Since v0.2.102 the gateway completes those two fields on every vendor answer — streamed
(`message_start`/`message_delta`) and buffered alike — from what the vendor itself returned: a stated figure is kept
verbatim, an OpenAI-style `prompt_tokens_details.cached_tokens` is mapped to the name the client reads, and anything
still absent is emitted as an explicit 0. A positive the vendor did not state is never invented, and no cache TTL is
ever claimed for a vendor (no documented figure exists), so the "N min left" half of the hint stays silent on vendor
routes rather than guessing. The same snapshot's JSON also carries `agents`: one row per (session, agent) identity
naming the model that actually ANSWERED that identity's last turn — with the fallback escape beside it when the
routing ladder served it — which the launcher's usage card renders as a "Who answered" list of SUBAGENT rows (a main-chat
row is shown only when a fallback served it: the user knows their chat's model). That list exists because
the VS Code agents panel shows the session model for every background lane (a harness bug,
anthropics/claude-code#93324): the gateway's ledger is the only place the answering model is recorded. Related: for
first-party 1M-native models (`claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`) the client sends no `[1m]` suffix and no
`context-1m` beta, so the ledger derives `window_client` for those routes from the chat-model context table instead of
defaulting every row to 200K.

**Usage in the `/model` picker.** The same data also rides in the picker itself, as text on each vendor row's
label: `glm-5.3 · Z.ai subscription · 1M ctx · 5h 10% · wk 72% used`. It always reads *used*, shortest window
first; an unknown window is left out and a vendor with nothing known gets no suffix at all — a vendor with no quota
source included, since its only number would be the gateway's own token ledger. Claude Code fetches
`/v1/models` once, when a session starts, and never refreshes it, so the label is a snapshot of that moment — when
the reading was already older than one refresh interval then, it says so as a clock time, `(as of 14:05)`. Building
the list never waits on a vendor: a cold cache answers without the text and starts the refresh, so the next session
has it. Claude's own windows cannot appear there: the client fills the picker with its built-in Claude rows and
discards the gateway's rows for the same models, text included — read them from the status line or the launcher.
Only the display text changes; model ids never do. Turn it off with `VCT_MODEL_GATEWAY_PICKER_USAGE=off`.

**Status line.** `.claude/scripts/gateway-usage-statusline.sh` (`.ps1` on Windows) prints that line for Claude Code's
`statusLine`, e.g. `Claude 5h 31% · wk 27% · Fable 12% │ GLM 5h 10% · wk 72%`, and prints
nothing at all when the gateway is not running or refuses it. VCO does not add it to your settings — a project-level
`statusLine` would override one you set in `~/.claude/settings.json` — so enable it yourself:
`"statusLine": {"type": "command", "command": "bash .claude/scripts/gateway-usage-statusline.sh"}` (Windows:
`"pwsh -NoProfile -File .claude/scripts/gateway-usage-statusline.ps1"`). The status line is drawn by the terminal
client (`claude`); the VS Code panel does not render `statusLine`.

**Panel mode — `remote-control` vs `multimodel`.** The launcher's status-bar pills (CLI: `python -m vco_lib.vscode_settings mode --set {multimodel,remote-control} --path <settings.json>`) flip the VS Code Claude Code panel between two states, one at a time — `claudeCode.environmentVariables` is VS Code machine-scope, so there is no per-workspace split. **`remote-control` is the stock client**: the four routing keys and the login-prompt key are removed, the panel talks to api.anthropic.com again, and the `/model` picker, the context-window sizing and the token accounting are all Claude Code's own. VCO writes none of `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, `CLAUDE_CODE_DISABLE_1M_CONTEXT` or `CLAUDE_CODE_AUTO_COMPACT_WINDOW` in *either* mode — that absence is what leaves the accounting native; a knob you set yourself is carried through untouched. **`multimodel` points the panel at the gateway**: the picker becomes the gateway's `/v1/models` catalog (the Z.ai subscription's GLM models, the QwenCloud Token-Plan models, Kimi Code's K3, and Claude in one list), and the client takes the context window from the model ID, so pick the "(1M context)" rows — their ids carry the `[1m]` suffix — when you want the 1M budget; ids only the gateway can resolve are stashed on the way to `remote-control` and restored on the way back. Remote Control (`/remote-control`, phone access) therefore works only in `remote-control` mode: Claude Code >= 2.1.196 refuses it whenever `ANTHROPIC_BASE_URL` is not api.anthropic.com, and a claude.ai sign-in does not bypass that. To have both at once, leave the panel on the gateway and run a detached native-auth server with the bundled `rc-native` skill (see [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md), "Remote Control").

| Var | Default | Effect |
|---|---|---|
| `VCT_MODEL_GATEWAY_PORT` | `11436` | TCP port. Falls back to the port file `<vct-state-dir>/model-gateway.port` (written by the running daemon, same convention as `hub.port`), then to the default. A non-numeric or out-of-range value falls through the same chain rather than being used literally. |
| `VCT_MODEL_GATEWAY_HOST` | `127.0.0.1` | Bind address. REFUSED at startup unless it resolves to a loopback address: the gateway proxies under your Claude login and is authorised by a local file token, so binding a routable interface is an error, not a configuration. |
| `VCT_MODEL_GATEWAY_CREDENTIALS` | `~/.claude/.credentials.json` | Path to the Claude CLI's OAuth credentials file (honours `VCT_CLAUDE_DIR`, see next section). Harness-owned: the gateway only ever reads it, never writes or copies it. |
| `VCT_MODEL_GATEWAY_CONTEXT_TABLE` | `<vct-state-dir>/model-gateway/chat_model_context.json` | Where the daemon looks for an exported chat-model context table (schema: `model_router.context_table`). Written by the launcher — on boot and on every GUI edit of the table (`launcher/src-tauri/src/commands/chat_model_context.rs`: `seed_and_export_on_boot`, `upsert_and_export`, `export_now`). Absent only before the launcher's first run; the daemon then reads the shipped seed (`claude_mcp_servers/model_router/chat_model_context.seed.json`), and the export wins per ROW over it. **On boot the launcher CONVERGES the table with that seed, row by row** (`converge_chat_model_context_seed` → `converge_rows`, the same per-row rule the reseed button applies): an absent row is inserted, a row with `user_edited = 0` whose values differ from the shipped ones is refreshed, a row that already matches is written not at all (so `updated_at` keeps meaning "when this row last changed"), a row with `user_edited = 1` — a human wrote it — is left byte-identical, and an id carrying a delete tombstone is skipped even when absent, because an automatic path must never undo a human's delete. `user_edited` is the marker that makes the first three cases safe to distinguish: it is 0 exactly when no human hand has written the row since it was seeded. So a shipped figure that changes — a model whose real window turns out to be larger than the row claimed — reaches every existing install on the next launcher boot, with no user action. The one thing boot may NOT do is clear a tombstone; that stays the model-gateway pane's **"Reseed from shipped defaults"** (`chat_model_context_reseed` → `reseed_chat_model_context`), which re-applies the shipped table, refreshes every row the user has not edited, preserves the ones they have byte-for-byte, restores a shipped row the user deleted, and leaves models they added themselves alone. |
| `VCT_MODEL_GATEWAY_SECRET_PROJECT` | unset — the gateway then uses **this install's orchestrator root**, resolved at runtime | **The per-project override for the gateway's vendor keys.** By default a gateway vendor key is a SHARED secret: put it in the launcher's Secrets panel with scope `shared` (OS keychain) or `vct set --shared --key <name>` (file store), and every install resolves it. The daemon still has to ASK as a registered project, because shared keychain secrets are reachable only through the hub's per-project `/env` route (their bucket is `_user_shared_`, owned by no project, and both stores resolve project-first-then-shared for whoever asks) — so with nothing pinned the gateway asks as the orchestrator root, which the launcher always registers. Set this variable to a project's path and THAT project's own key outranks the shared one; its `.no-shared-fallback` marker is honoured too, because there is one chain and no second mechanism. Before v0.2.95 the unpinned default was the process's **working directory**, which for a login-started daemon is the state root — not a registered project, so the hub tier was skipped entirely and every OS-keychain key was invisible while `/health` showed the vendor present with an empty key cache. The default is resolved at runtime, never baked into the boot unit, so a moved install self-heals — the systemd unit / LaunchAgent / Scheduled Task ships this variable EMPTY, and a non-empty one is always a scope you set (it is then preserved across every re-render, and a re-render of an older unit whose value merely equals this install's root drops it, so the runtime default takes over). `/health`'s `secret_scope` reports which scope is in force, where it came from (`pin` / `install_root` / `cwd`) and whether it resolves, and the daemon logs that verdict at startup. The launcher passes the variable through when it starts the daemon from the GUI. |
| `VCT_MODEL_GATEWAY_CATALOG` | `latest` | Which versions of a model family reach the `/model` picker. `latest` publishes only the newest version of each family (Fable 5.1 without Fable 5; one GLM 5 row instead of six) — variant lines like `-flash`, `-turbo` and `-air` are families of their own and each keep their newest. `all` publishes every version each upstream returns, EXCEPT ids withheld on grounds this knob does not govern: truth-withheld ids (`verified_ids` — an id that answers as a different model) and curated-hidden ids (`catalog_hide_ids`) stay hidden under `all` too, and ids matching `catalog_exclude_prefixes` are dropped from the catalog entirely and appear in neither list, by design. What this knob narrows is not lost quietly: ids it withholds come back in `_vct_catalog_hidden` on `/v1/models`, are counted as `catalog_hidden` in `/health`, and remain selectable by name — the filter narrows the picker, never the router. An unrecognised value is refused at startup rather than silently treated as the default. |
| `VCT_MODEL_GATEWAY_WINDOW_ROWS` | `one_m_only` | How many picker rows a 1M **first-party** model occupies. The gateway sizes a client's context budget from the id string — `[1m]` means 1M, any other spelling behind a custom base URL means the smaller default — so a 1M first-party model has two useful spellings and used to be published as a pair. `one_m_only` publishes just the `[1m]` one, so one model is one row. `both` also publishes the plain id, which is how you hold a 1M model to the smaller budget deliberately (the way to keep a long session under the upstream's long-context pricing tier). The withheld plain id comes back in `_vct_catalog_hidden` and stays selectable by name: this knob decides what is advertised, never what the gateway will answer to. **Scope**: only first-party rows are paired, so this knob moves nothing else — a 200K model has no second spelling, and a vendor model is always published as the single id its resolved window earns (already carrying `[1m]` when that window is 1M). An unrecognised value is refused at startup. `/health` reports the resolved mode as `window_rows`. |
| `VCT_MODEL_GATEWAY_PICKER_USAGE` | `on` | Whether each vendor row in the `/model` picker carries its subscription's usage as text (see "Usage in the `/model` picker" above). `off` gives clean labels and also stops `/v1/models` from scheduling a usage refresh; `1`/`true`/`yes` and `0`/`false`/`no` mean the same two things. Unlike the two enums above, an unrecognised value does not refuse startup — this knob changes label text only, and a gateway that will not start takes every chat routed through it down with it — it logs a warning naming the valid values and runs with `on`. `/health` reports the mode in force as `picker_usage`. |
| `VCT_MODEL_GATEWAY_CATALOG_TTL` | `21600` (6 h) | Seconds before the live model catalog is re-fetched. |
| `VCT_MODEL_GATEWAY_STATIC_RETRY_TTL` | `300` | When a live catalog fetch fails and the static fallback is serving, retry the live fetch after this many seconds instead of waiting out the full catalog TTL (a one-minute vendor outage must not cost six hours of a stale picker). |
| `VCT_MODEL_GATEWAY_KEY_TTL` | `300` | Seconds before the vendor key is re-resolved, so a rotation is picked up — and a hub that was down at boot is retried — without restarting the daemon. |
| `VCT_MODEL_GATEWAY_KEY_STALE_MAX_S` | `21600` | While key RESOLUTION fails (e.g. vct-hub stopped by an orchestrator update), keep answering with the last-known-good key for at most this long since its last successful resolution. Three consecutive KEY-level vendor rejections invalidate it immediately — serve-stale never outlives a key the vendor itself rejects. Only key-level rejections count: every 401, and a 403 whose body is not the vendor's documented model-level `access_denied` shape. A 403 `AccessDenied` means the MODEL is deprecated or gated while the key is fine, and it must not (three of them would otherwise invalidate a healthy key). Any 2xx clears the count. |
| `VCT_MODEL_GATEWAY_REWRITE_BUFFER_BYTES` | `33554432` (32 MiB) | How much of a request body the daemon HOLDS in order to rewrite ids inside it. **Not a size limit**: nothing is ever refused for being bigger — a body past this bound is streamed straight through to the upstream (with only the gateway's own `claude-gw/` namespace spliced out of the model id) and the upstream's own answer is relayed. The default sits just above Anthropic's documented 32 MB request ceiling, so every body the first-party API can accept is one the gateway still rewrites. Lower it to cap memory; raising it past the upstream's ceiling only moves a refusal from there to there. **One assumption**: routing a body past this bound reads the top-level `model` out of the first 64 KiB without parsing the rest, so `model` must be an early field. Claude Code and the JS SDK put it first; the Python SDK emits `max_tokens, messages, model`, so a >32 MiB request built with it — with `messages` ahead of `model` — cannot be routed and gets a 400 naming this (`reason=model_unreadable_in_head`). Nothing is refused for its size. |
| `VCT_GW_TMP_TOKEN` | unset | Passes the gateway's host token to `python -m vco_lib.vscode_settings` subcommands without putting it in argv (shell history, `ps` listings). Unset is the normal case: the token is then read from the gateway's own token file and never crosses a process boundary. |
| `VCT_GATEWAY_CONTEXT_GUARD_MARGIN` | `8192` | The safety margin, in tokens, of the vendor **context guard** (v0.2.102). For every non-Anthropic route whose model has a cited `context_window` in the chat-model context table, the gateway estimates a request's input tokens BEFORE calling the vendor and refuses, with Anthropic's own `prompt is too long: N tokens > M maximum` envelope (HTTP 400), a request that cannot fit. That envelope is the shape Claude Code recognises and answers with a reactive compaction; a vendor's native overflow error (QwenCloud: `Range of input length should be [1, N]`) is not, which is how a 1M-budgeted session against a 983,616-token ceiling got stuck retrying forever. The estimate always describes the CURRENT request — the chat's measured tokens-per-byte (the last accepted turn's vendor-reported input tokens over that turn's own body size, from the usage ledger, clamped to 0.15–0.5 tokens/byte) times this request's body size — so a compaction, whose request body shrinks with the conversation, passes on its own bytes and the guard is self-correcting; a calibrated estimate is refused past `context_window − margin`. A body carrying image/document (base64) blocks is NOT JUDGED at all — every estimate the guard can make for it is a text arithmetic that over-states base64 by orders of magnitude (on a 200K-window model a single screenshot would be falsely refused, and compaction cannot shrink an image), so such bodies are forwarded and the vendor-overflow translation is the net; such a turn is not used to calibrate later text turns either. And when the previous request for the same chat was refused by the guard and this body has not shrunk by at least 5% (the shape of the client's reactive-compaction request, which carries ≈ the refused history), the guard stands down once and forwards it, so it never blocks its own recovery. With no calibration yet the fallback is a bytes/4 floor over the body, which over-estimates on code, so it is refused only past the RAW `context_window` — the vendor-overflow translation (below) is the safety net for what that lets through. The same translation is applied when a vendor's own overflow refusal comes BACK despite the guard. The first-party Anthropic route is never guarded — it answers natively with this very envelope — and neither are `count_tokens` calls. The guard reads the ACTIVE table: on an install whose launcher export still carries a pre-v0.2.102 figure for a model the seed has corrected (qwen3.8-max: an export with 1,000,000 guards at 991,808 where the seed says 975,424), the export wins per row and one INFO line names both values; correct the row in Preferences → Chat model context to change it. A non-numeric or non-positive margin value falls back to the default rather than being used literally. |
| `VCT_GATEWAY_FALLBACK_RETRY_AFTER_MAX_S` | `30` | The **model-fallback ladder's** retry-after threshold, in seconds (v0.2.102, owner decision OD-9). The ladder: a Claude **haiku-slot** request whose FIRST-PARTY answer is a qualifying quota refusal escalates to `glm-5.3-flash` on the z.ai subscription; a z.ai quota verdict on that rung escalates to `qwen3.8-flash` on QwenCloud — the vendor→vendor edge is walked ONLY by an attempt that arrived at its rung BY fallback (coordinator decision 2026-10-07), so a vendor model the user picked is never rerouted: its refusal is relayed and the client's own retry owns recovery. The last rung has no entry, so its refusal is the client's answer — the ladder never fabricates a success and never reroutes a model the user picked on its own (the entry edge is the haiku slot; every route that is not the haiku slot or a rung reached by fallback keeps the shipped behaviour byte-for-byte). A refusal QUALIFIES when it is a 429 without a readable `retry-after`, or with a quota-exhausted body (the same evidence the substituted quota message reads, `model_router/quota.py`); on a 429 with `retry-after` (including `0`, "retry immediately") at or below this knob the SAME rung is retried once — after sleeping the stated delay — and falls back if it refuses again; a window LONGER than the knob falls back too (waiting it out is what the ladder exists to spare the chat). Every fallback is visible: the response's `model` field names the model that answered, the access line carries `note=model_fallback fallback_from=…` with the refused rung and the quota evidence, and the usage-ledger row carries a `fallback_from` field. A rung whose vendor key is not resolved, or whose model's context guard refuses the request, is skipped and the original refusal relayed. Only re-sendable (buffered, non-`count_tokens`) requests can walk the ladder. A non-numeric or non-positive value falls back to the default rather than being used literally. |

The three TTL knobs exist primarily so the smoke tests can drive the caches without sleeping; they are documented because a knob nobody can find is a knob that gets re-invented.

**Per-row request shaping — what the chat-model context table drives besides windows.** Two seed-row fields shape vendor requests (v0.2.102 for the second): `text_only` (a model that cannot see images — glm-5.3 — gets a short text note where each image block was, instead of the silent "I cannot see images" answer) and `clear_thinking` (A8, owner decision OD-10: a row may carry the `thinking.clear_thinking` value the vendor documents; the gateway merges it into the `thinking` param a request ALREADY carries — never adds the param — and a row without the field, the default, sends nothing extra; today only the glm-5.3-flash row carries it, `false`, per the z.ai thinking guide). Both are properties of the MODEL curated in the shipped seed with a per-row `source` citation, so neither a launcher export that predates the field nor a per-vendor window override can silently reset them. Effort translation (`output_config.effort` values a (vendor, model) pair rejects, e.g. `medium` on the QwenCloud glm-5.3 route) is a separate table — `model_router/effort.py` — with the same data-not-code discipline.

**Vendor registry — what ships, and how a vendor key is named.** The gateway's vendors are declared in one file, `claude_mcp_servers/model_router/vendors.py`; three routes ship. The Z.ai subscription (`glm_api_key` or `zai_api_key`, shared scope) publishes its models under `claude-gw/…`. The QwenCloud Token-Plan route (v0.2.96, `qwen_api_key` or `QWEN_API_KEY` — the uppercase spelling is the vendor docs' own env convention and the keychain stores the name the user typed; shared scope, same resolution path as the other vendor keys) publishes under the nested namespace `claude-gw/qwen/…`, so every panel rule that keys on the `claude-gw/` prefix keeps working, and its bare model ids (`qwen…`, `deepseek…`) route to it without the prefix too; the pay-as-you-go QwenCloud endpoint is deliberately absent until its model list is extracted and proven. The Token-Plan endpoint's anthropic app path has no model-list route, but the subscription's OpenAI-compatible base serves one (live-verified 2026-09-22), so its row points the catalog fetch there with `catalog_url` — a registry field carrying an absolute URL used INSTEAD of `upstream + catalog_path`, for a vendor whose list endpoint lives on another base — and drops the non-chat ids that list carries (voice, image, the `auto` router alias) with `catalog_exclude_prefixes`, a registry field of id prefixes applied to the live list only (the row's curated fallback needs no filter; excluded ids appear in neither the picker nor `_vct_catalog_hidden`). `/health` reports the family's catalog source as `live` when that fetch answers and `declared` when the row's eight declared ids answer instead — no key resolved yet, or the fetch failed (`declared` names the row's own list as what is being served; `static` is the shipped `static_catalog.json` snapshot; a declared fallback after a failed fetch retries on the short TTL, while a row with no list endpoint at all has nothing to retry). One catalog-truth rule applies to a vendor whose endpoint LISTS ids that reroute server-side to other models: the row's `verified_ids` names the ids verified to answer as themselves, the catalog withholds every other listed id (they appear in `_vct_catalog_hidden`, namespaced, with one INFO log line per changed withheld set), and `VCT_MODEL_GATEWAY_CATALOG=all` does not bring them back — an id that answers as a different model is not a capability to unlock. Adding a vendor stays a row in that file, never a code change; `vct-model-gateway --check` validates the registry at startup. glm-5.2 is not provided on any route (owner ruling 2026-10-01): both rows carry it in `retired_ids`, a registry field of exact ids that are not published, not declared, dropped from a live list that still carries them, and REFUSED by name like any unknown id (HTTP 400 `unknown_model`, suggesting the nearest served id — `glm-5.3`); glm-5.3 is 1M on both the z.ai and the qwen route. Three filter concepts, deliberately distinct: `verified_ids` is the TRUTH filter (ids that reroute server-side are withheld, and no knob brings them back); `catalog_exclude_prefixes` drops non-model ids (router aliases, voice/image modalities) from the catalog entirely; `catalog_hide_ids` CURATES — an id the vendor defers to a later discussion stays reported in `_vct_catalog_hidden` and routable by name but never publishes under either catalog filter, even when a live refresh lists it or it would win the latest ranking. The advertised list this produces (owner ruling 2026-09-22): the first-party Claude 5 family; z.ai's glm-5.3 and glm-5.3-flash; qwen's glm-5.3, deepseek-v4.1-flash, qwen3.8-max and qwen3.8-flash. A third route, Kimi Code (v0.2.102, owner ruling 2026-10-07: K3 only), publishes `claude-gw/kimi/k3[1m]`. Its key is `KIMI_API_KEY` or `kimi_api_key` (shared scope). The Moonshot pay-as-you-go key name is deliberately not tried: it is a different key for a different endpoint. The route is the vendor's international Anthropic-compatible base, and the key is sent as `x-api-key`. The model list is fetched live from the same base, with the shipped snapshot as its fallback. Two fields shape this row. The vendor's edge refuses library user-agents, so the gateway's model-list fetch carries the user-agent of the client whose `/v1/models` request triggered it, as `/v1/messages` always did; the gateway never makes one up. The vendor's endpoint also answers ANY model name with HTTP 200 and echoes it back, so the row sets `forward_verified_only`, a registry field. With it, the router forwards only the row's `verified_ids` and refuses every other id by name, including ids the vendor's own list carries. The model-echo counter cannot catch this vendor's substitutions, because the echo always matches. K3's 1M window is plan-gated by the vendor: Plus is capped at 256K and longer requests are refused with HTTP 401. A Plus subscriber lowers the `k3` row to 262144 in Preferences → Chat model context.

**Autostart (opt-in), and the three states it can be in.** `vct-model-gateway --register-boot` (or the launcher's gateway toggle) writes a systemd user unit / LaunchAgent / Scheduled Task; nothing an install does creates one, because a login-time daemon holding an OAuth passthrough is your decision. Two properties are worth knowing:

* **The registration is verified before it is written.** The entry point is resolved from the install root's venv — the `vct-model-gateway` console script there, else `<that venv's python> -m model_router` — and then RUN with `--version`. If nothing answers, no unit is written and the reason is printed; an existing unit is left untouched. Before v0.2.95 the interpreter that happened to run `install.py` was baked in instead, so an update run under a system python produced a unit that could never start, silently.
* **`registered but unrunnable` is its own state**, distinct from "not registered" and from "running". `python -m vco_lib.gateway_ensure status --json` reports it (`.state`), `vco doctor` reports it, and it appears in `UPDATE_DEFERRED.md` as `gateway_registered_but_unrunnable`. `python install.py --update` is the fix: it re-renders the registration, verifying it first, and the same run clears the entry.

Every Claude Code session ensures a registered gateway through the `session-start-ensure-hub` hook (one hook ensures both detached services). It never registers anything, it leaves a running daemon alone — the "only one instance" guarantee is the daemon's own pid/port guard, which the ensure reads rather than duplicating — and on Linux it issues `systemctl --user reset-failed` before `start`, because a unit parked by the unit's own `StartLimitBurst` otherwise ignores a plain `start`.

| Var | Default | Effect |
|---|---|---|
| `VCO_GATEWAY_STATE` | set by the hook, per session | **Internal, do not set.** The hook `eval`s `python -m vco_lib.gateway_ensure ensure --shell`, which prints this as the ensure's outcome word — `running`, `started`, `not_registered`, `registered_but_unrunnable`, `registered_not_running`, `start_failed` or `disabled_by_env`. Setting it yourself changes nothing: the next line of the same `eval` overwrites it. Ask for the value with `python -m vco_lib.gateway_ensure status --json` instead. |
| `VCO_GATEWAY_REASON` | set by the hook, per session | **Internal, do not set.** The one-line explanation printed beside the state above, which the hook forwards when the ensure did not succeed. It follows the same `eval` contract the hub leg of that hook uses for its own variables. |

### Metrics location and the `~/.claude` migration (v0.2.92)

VCO's JSONL telemetry streams (`failures.jsonl`, `compactions.jsonl`, `kg_update_tokens.jsonl`, `embedding_failures.jsonl`, `bundled_versions.jsonl`) live under `<`[`VCT_STATE_DIR`](#install-time-env-knobs)`>/metrics` (default `~/.vct/metrics`). Before v0.2.92 they were written to `~/.claude/metrics`; `~/.claude` is Claude Code's own directory and VCO now writes nothing under it the harness did not ask for. The old location is a frozen archive: still read, never deleted, never written by the migration.

**`VCT_CLAUDE_DIR`** — the one user-settable knob in this story. It overrides `~/.claude` as the Claude Code user directory for every VCO read of it: the MCP workflow config (`workflow/config/mcp-config.json`) and the legacy metrics archive above. All consumers resolve through a single resolver (`vco_lib/paths.py::claude_user_dir`), so one pin steers all of them; the test suite uses the same pin to stay out of real state. Not to be confused with `~/.claude.json` — that is a FILE beside this directory and follows the user-home override, not this one.

The four `VCO_METRICS_*` variables below are **internal — do not set them**. They are exported by `templates/hooks/_lib/metrics-dir.sh` (and its `.ps1` sibling) so VCO's own hooks share one answer to "where do metrics live?"; they are documented here so their names are not a mystery in a process listing:

| Var | Meaning |
|---|---|
| `VCO_METRICS_HOME` | The new home, `<VCT_STATE_DIR>/metrics` — unconditionally. |
| `VCO_LEGACY_METRICS_DIR` | The frozen archive, `$VCT_CLAUDE_DIR/metrics` (default `~/.claude/metrics`) — unconditionally. |
| `VCO_METRICS_MIGRATED` | `1` when the one-time copy is done or not needed (no archive / no `*.jsonl` in it), else `0`. |
| `VCO_METRICS_DIR` | The write target: the new home once migration is verified, the archive while a copy is still owed. |

Writers switch to the new home only after `vco_lib.metrics_migration` has copied AND verified every archived file (record: `<home>/.migrated-from-claude.json`). Until then they keep appending to the archive — nothing is stranded and nothing is double-counted; the next migration run finishes the job and the writers move on their own.

More names belong to the same family and are also **internal — do not set them**. They are shell variables shared between a hook and the `_lib` helper it sources, and they are listed here only so their names are not a mystery in a process listing or a `set` dump:

| Var | Meaning |
|---|---|
| `VCO_CODE_EXT_RE` | The one code-file extension alternation, exported by `templates/hooks/_lib/code-extensions.sh` (`.ps1`: `$script:VcoCodeExtRe`). Read by the context hooks and by `_lib/route-touched-path.sh` so "is this a code file?" has ONE answer. Setting it would change which files reach the code graph. |
| `VCO_ROUTE_NUDGE` | The LLM-visible text `_lib/route-touched-path.sh` leaves for its caller (`post-file-edit.sh` / `post-bash-file-sync.sh`) to emit as one `additionalContext` envelope — currently the pending KG duplicate-scan report. An input value is overwritten on the first routed path. |
| `VCO_MISSING_LIB_NOTICE` | The broken-install notice `vco_report_missing_hook_lib` (`templates/hooks/_lib/emit-context.sh`; `.ps1`: the return value of `Emit-VcoMissingHookLibNotice`) leaves for its caller to emit — set when a shipped `_lib/` helper the hook needs is missing, empty when the condition was already reported this session (sentinel `.claude/state/route_lib_missing_<session>_<lib>`). An input value is always overwritten. |
| `VCO_ORCHESTRATOR_SCRIPT` | The path `resolve_vco_orchestrator_script` (`templates/hooks/_lib/resolve-vco-venv.sh`; `.ps1`: the return value of `Resolve-VcoOrchestratorScript`) leaves for its caller: an orchestrator-shipped script (`rl_kg_search.py`, `rl_drain_citations.py`) located in the orchestrator install (the same roots the venv resolver uses, `$VCT_ORCHESTRATOR_ROOT` among them) — never in the project. The caller runs it with the project's own `CLAUDE_PROJECT_DIR`, so the project's KG/code-graph permissions apply. Empty when not found; an input value is always overwritten. |
| `VCO_WORKTREE_GATE_HUB_UNREACHABLE` | Set to `1` by `templates/hooks/_lib/worktree-gate.sh` (`.ps1`: `$script:WorktreeGateHubUnreachable`) when its worktree probe got "hub unreachable" from the resolver (v0.2.102). The skip/index decision does not change — the edit is still indexed on doubt — but the sourcing hook (`code-graph-incremental.sh`, `stop-codegraph-drain.sh`) reads it to tell the model the hub is down. Reset to empty whenever the lib is sourced, so an exported value never leaks in. |
| `VCO_HUB_DOWN_NOTICE` | The "[VCO hub unreachable]" text `vco_report_hub_unreachable` (`templates/hooks/_lib/emit-context.sh`; `.ps1`: the return value of `Get-VcoHubUnreachableNotice`) leaves for its caller to emit in its own `additionalContext` envelope, with the `vct-hub --start-if-not-running` restart hint (v0.2.102). Empty when the outage was already reported this session (one sentinel per agent key, `.claude/state/hub_unreachable_notice_<key>`, shared by every hub-dependent hook; hourly for a caller with no session). An input value is always overwritten. |

For the RL event-retention knobs (`RL_EVENTS_*`) see [Paid-module license framework → RL event retention and archives](#rl-event-retention-and-archives); they apply on free installs too.

## Disabling hooks for debugging or CI

Set `VCT_DISABLE_HOOKS=1` and every `.claude/hooks/*.sh` exits 0 cleanly without doing its work. Useful when:

- A hook misbehaves and you want a one-knob disable instead of editing each hook or the hook matchers.
- Running install or tests in CI where backing services (Weaviate, Ollama) are not running and hook probes would spam errors.
- You're debugging a session and need raw tool output without hook side effects.

```bash
# Ad-hoc, single session
VCT_DISABLE_HOOKS=1 claude

# Persistent, current shell only
export VCT_DISABLE_HOOKS=1

# CI runners
env VCT_DISABLE_HOOKS=1 python install.py --quiet
```

The guard sits **after** the credential-scrub block in every hook, so secrets are still stripped from the env even when the hook itself no-ops. Coverage is asserted by `tests/test_hooks_disable_guard.py` — a regression that adds a new hook without the guard fails CI.

## Vision and image analysis

For image analysis, pass the image file path to Claude using the native `Read` tool — Claude's built-in vision handles it directly. This approach is simple, requires no local GPU, and works on any hardware.

## Sharing knowledge across projects

By default, every project queries both its per-project KG and a shared cross-project collection (`VibeCodedOrchestrator_KnowledgeGraph`). Knowledge nodes captured in one project are visible to all others without re-explaining context.

Three control points:

- **`SHARED_KG_COLLECTION`** — name of the shared collection. Default `VibeCodedOrchestrator_KnowledgeGraph`. Override to point at a private team-shared collection (`AcmeTeam_SharedKG`) without exposing it via the public bundled name. The launcher's Identity tab ships a "Manage shared KG collection" picker that surfaces every orchestrator-shaped class on your Weaviate and lets you pick which one is canonical — useful when migrating from an older install whose shared collection has a different name.
- **`SHARED_KG_WRITE_DISABLED=true`** — per-project WRITE gate. Refuses `store_knowledge_node(scope="shared")` from this project with a clear error. Reads of the shared collection remain unconditional — knowledge accumulation across projects is the headline value prop. Legacy alias: `SHARED_KG_OPT_OUT` (kept for ~3 releases, target removal 2026-08).
- **`store_knowledge_node(scope="shared")`** — explicit write to the shared collection. The default scope is `"project"` so arbitrary projects don't pollute the shared collection by accident.

Install does NOT auto-adopt a foreign shared KG it finds on the host (e.g. an existing `ClaudeKnowledgeGraph` from an earlier install). Reason: the orphan-prune pass in `sync_knowledge_graph.py` deletes entries whose `file_path` no longer exists in the active project; two installs sharing one collection would silently delete each other's nodes. VibeCoded Orchestrator (VCO) always creates `VibeCodedOrchestrator_KnowledgeGraph` fresh (or skips creation if the exact name already exists).

## KG-summary backend selection

Auto-generated 2-3 sentence summaries for every KG node, written to `knowledge/.node_formats.json` and consumed by the auto-tier retrieval system. KG-node summarization is the one VCO subsystem that benefits from a standalone Claude Code CLI install, but it is not required — Ollama with a hardware-appropriate local model serves as the automatic fallback. Backends are tried in order; first one available wins:

1. `claude` CLI on PATH — optional, used only for KG-node summarization when present.
2. z.ai GLM (cloud) — the vendor's flash model (`glm-5.3-flash`), used only when the z.ai key resolves on this machine (the model gateway's own vendor registry and key resolver). No key → the tier is skipped silently; never an error.
3. QwenCloud (cloud) — the same shape on the qwen key (`qwen3.8-flash`). QwenCloud serves only this summary fallback.
4. Ollama at `http://localhost:11435` — automatic fallback; works for any VCO user since Ollama is already required for embeddings. Default model: the machine's hardware tier (`qwen3.5:9b`, `qwen3.5:4b` on smaller cards, `gemma4:e4b` on a capable CPU with no GPU — see below). Deferred while a code-graph embedding walk is active or free VRAM is below the floor — derived from the resolved model's own size (its on-disk weights plus a KV-cache margin; 8192 MB when the size cannot be read), overridden with `KG_SUMMARY_MIN_FREE_VRAM_MB`: the node stays owed for the next run rather than loading a generation model onto a GPU the re-index needs.
5. OpenAI — opt-in, and gated twice: it needs a key in VCO's own shared slot (`openai_api_key`; **not** `$OPENAI_API_KEY`, v0.2.98) **and** consent (`kg_summary_openai_consent` in launcher Preferences → KG Summaries, or the operator flag `--force-api`). Costs $$ per generation.
6. `ANTHROPIC_API_KEY` direct — **legacy** opt-in fallback (kept by design): reached only when the env key is set and the tiers above were unavailable or consent-gated. Costs $$ per generation — a cost warning is logged when this tier is selected.
7. Silent skip — friendly log line, exits 0.

Force a specific backend with `KG_SUMMARY_BACKEND=cli|zai|qwen|ollama|openai|api|skip` (`zai` = z.ai GLM, `qwen` = QwenCloud, `api` = Anthropic direct, `openai` = OpenAI); unset, the launcher's Preferences → KG Summaries backend pin applies (`app_state` `kg_summary_backend_override`, empty = auto-detect). The local Ollama model is the machine's ONE generation model, resolved by `vco_lib.summary_model` (`python -m vco_lib.summary_model` prints it and which rule chose it): `KG_SUMMARY_OLLAMA_MODEL`, else the Preferences "Ollama model" pick (`kg_summary_ollama_model`), else the model install recorded and pulled, else the hardware tier from the same table install uses (install pulls only that one model): on a GPU, the strongest of `qwen3.5:9b` (≈9.1 GB+ VRAM) and `qwen3.5:4b` (≈4.9 GB+ VRAM) whose free-VRAM floor the card can hold — the table asks the SAME floor function the runtime guard applies (`vco_lib.embedding_selection.summary_required_free_vram_mb`: on-disk size + max(1 GB, 40 %), plus ~0.5 GB an idle desktop already uses), so it never assigns a model the runtime would refuse to load; `gemma4:e4b` on a capable CPU (12 GB+ RAM, 6+ cores) with no GPU; `qwen3.5:0.8b` otherwise. A resolved model Ollama does not hold is never pulled at runtime: the tier says so and the ladder moves on. Override Ollama sampling params with `KG_SUMMARY_OLLAMA_OPTIONS='{"temperature": 0.5, "top_p": 0.9}'` (JSON object passed through to the Ollama API). The context window is not a per-caller option: every VCO generation request (KG summaries, node formats, document summaries) asks Ollama for the same `num_ctx` so each model stays ONE resident copy — 8192 by default (the smallest window that fits every generation prompt VCO sends; 32768 loaded a KV cache 4x larger than any prompt needs), changed machine-wide with `VCO_OLLAMA_GENERATE_NUM_CTX=<tokens>`. A `num_ctx` inside `KG_SUMMARY_OLLAMA_OPTIONS` is superseded by that knob and dropped with a stderr note.

### Circuit breaker (usage limits and outages)

A tier that stops serving mid-run is **demoted** rather than retried: without this, an account that hits its cap partway through a backfill keeps spawning a doomed request for every remaining node (a field report measured 426 of them in one run). The latch lives in `<vct-state-dir>/summary_backend_breaker.json` because the generator runs one process per node, and it **expires** rather than persisting — a recovered endpoint works again with no file to delete. The reason is classified, and the classes are treated differently on purpose: `rate_limit` (a transient 429) and `auth` demote on the first occurrence (retrying cannot succeed right now), `quota` (token/budget **exhaustion** — "usage limit reached", "out of credits") and `trust` (a headless `claude -p` refused because the workspace is not trusted) demote on the first occurrence for 5 hours (the account window, or a human re-accepting the trust dialog), `capacity` (529/503/timeout) needs several consecutive strikes because it recovers on its own, and `other` (unclassified) is strike-gated too since v0.2.96 — one stray unknown never demotes, but an unclassified failure *storm* does (a field night ran 304 consecutive ones with the breaker never moving). When the `cli` tier opens on a `quota` or `trust` failure, the trip seam also records a one-per-event `kg_summaries_degraded` deferral entry naming the pending rows — see `python -m vco_lib.summary_health summary-recheck` to clear the breaker and regenerate exactly those.

| Env var | Default | Effect |
|---|---|---|
| `VCO_SUMMARY_BREAKER` | enabled | Kill switch. Set to `off`, `0`, `false` or `no` to disable the breaker entirely — every tier is then tried on every node, as before v0.2.92. |
| `VCO_SUMMARY_BREAKER_COOLDOWN` | `900` | Seconds a tier stays demoted after a **rate-limit** (transient 429) failure. Long, because that clock is not yours to move. |
| `VCO_SUMMARY_BREAKER_AUTH_COOLDOWN` | `120` | Seconds a tier stays demoted after an **auth** failure. Short, because *you* are the fix: you re-authenticate and expect the next node to use the tier again. |
| `VCO_SUMMARY_BREAKER_CAPACITY_COOLDOWN` | `60` | Seconds a tier stays demoted after a sustained **capacity** failure (529 / 503 / timeout). |
| `VCO_SUMMARY_BREAKER_CAPACITY_STRIKES` | `3` | Consecutive capacity failures before that tier is demoted at all. A permanent latch on one transient 529 would be its own bug. |
| `VCO_SUMMARY_BREAKER_QUOTA_COOLDOWN` | `18000` | Seconds a tier stays demoted after a **quota** (token/budget exhaustion) or **trust** failure. 5 hours by default — the account window rolls over on a clock nobody here controls, so shorter just means futile re-probes (v0.2.92's 900 s meant ~20 per outage). |
| `VCO_SUMMARY_BREAKER_OTHER_COOLDOWN` | `18000` | Seconds a tier stays demoted after N consecutive **unclassified** failures. Same 5 h as quota: an unclassified storm is by definition something nobody has diagnosed yet. |
| `VCO_SUMMARY_BREAKER_OTHER_STRIKES` | `3` | Consecutive unclassified failures before that tier is demoted at all. One weird error still never demotes — three in a row is a storm (v0.2.96; before it, `other` never tripped). |

Values below the documented floor are ignored (a cooldown must be ≥ 0, a strike count ≥ 1), so a typo falls back to the default rather than disabling the guard. When every tier is cooling down the generator logs `no backend available — … cooling down after a usage-limit / capacity failure. Nothing to install; retry after the cooldown.` — the launcher surfaces that line verbatim instead of telling you to install a CLI you already have.

A backend named explicitly through `KG_SUMMARY_BACKEND` is **never** auto-demoted or substituted: you asked for that tier, so the failure is raised rather than quietly answered by a different model.

### Code-entity summaries (`generate-code-summary.py`)

The same backend ladder also writes one-line and short summaries for code-graph functions and classes into `.claude/.code_formats.json`. The resync driver starts the generator in the background once a code-graph walk has finished (v0.2.100 — before, it started beside the walk and summarised rows the walk was about to replace); you can also run `python .claude/scripts/generate-code-summary.py --project <name>` yourself. `CODE_SUMMARY_BACKEND` forces its backend (falling back to `KG_SUMMARY_BACKEND`).

| Env var | Default | Effect |
|---|---|---|
| `VCO_CODE_SUMMARY_MAX_PER_RUN` | `150` | Entities visited per run (the CLI flag `--max-per-run` wins). The next run continues with the next stale entities. |
| `VCO_CODE_SUMMARY_BATCH_SIZE` | `8` | Entities sent in ONE `claude -p` call on the CLI tier (a JSON reply, one entry per entity); `1` turns batching off. An entity the reply does not answer validly falls back to its own calls, so a partial reply loses nothing. Only the CLI tier batches — a local model's output budget would truncate a multi-entity reply. |

Every headless `claude -p` call made by the summary generators (KG and code) starts **without MCP servers and without hooks**: `--strict-mcp-config` with an empty `--mcp-config`, and `--settings` carrying `disableAllHooks`. Each flag is added only when the installed CLI lists it in `claude --help` (checked once per process); an older CLI that lacks one is still used, without that flag, and the log says so. The working directory stays the project folder: a headless call in a folder the CLI has never trusted fails with "this workspace has not been trusted", so moving it to a scratch directory would break the tier.

The code generator exits **1** when Weaviate is unreachable or a collection scan fails (the failing collection is named in its last log line, and that collection's existing summaries are kept, never pruned against an empty scan); otherwise **0**, including when no backend is available. Each collection is read with only the properties its own schema declares, so a property only functions carry (`n_callers`) no longer makes the class scan fail.

The summary generator depends on the required frontmatter fields (`title`, `type`, `tags`, `created`, `updated`, `status`; one list, `REQUIRED_FRONTMATTER_FIELDS` in `sync_knowledge_graph.py`). Today nothing blocks a write that lacks them: `maintain_knowledge_graph.py --check` reports such files. A hook that validates frontmatter on every write to `knowledge/**/*.md` (Write, Edit and shell writes) is scheduled for v0.2.103 by the project owner.

## Manual operator scripts

One helper script under `claude_mcp_servers/scripts/` is not wired into the
default workflow but is kept available for operators upgrading older
installations:

- **`migrate_to_new_embeddings.py`** — One-shot migration that adds new named
  vectors (qwen3_embed for KG, codesage_embed for code) to Weaviate
  collections alongside any legacy named vectors (ollama_embed,
  ollama_code_embed). Preserves all existing data; only adds the new vector
  slots and backfills them. Run manually after upgrading the embedding
  models referenced in the MCP configuration.

The manual `--all` backfill that regenerates per-node descriptions /
summaries in `knowledge/.node_formats.json` lives in the ONE sidecar writer,
`templates/scripts/generate-kg-summary.py` (also rendered into each project's
`.claude/scripts/`). It runs incrementally via the kg-summary-generator hook
on edit; run it with `--all [--knowledge-dir DIR] [--force] [--dry-run]` when
you want to rebuild the cache from scratch (for example after a bulk import
or a node-format schema change).

## `vco` CLI — verify commands

The `vco` console-script is registered on PATH automatically when
`install.py` runs (it does `pip install -e .` against the orchestrator's
`pyproject.toml` after creating `.venv/`, so `vco` lands at
`.venv/bin/vco` on Unix and `.venv\Scripts\vco.exe` on Windows). For
manual / out-of-band installs, run `pip install -e .` from the repo root
into any venv. Fall back to `python -m vco_lib.cli <subcommand>` if for any
reason the entry point isn't on PATH. Two acceptance verifiers ship:

### `vco verify-pins`

Confirms each `[npm.*]` entry in `bundled_mcp_versions.toml` is installed
at the pinned version. Compares `npm list -g <package> --json` output to
the manifest; reports either a single `OK` line or a `package | pinned |
installed | status` drift table.

| Flag    | Behaviour                                                        |
| ------- | ---------------------------------------------------------------- |
| `--json`| Emit a single JSON envelope on stdout (machine-readable).        |
| `--fix` | Re-install each drifted package via `install._install_pinned_npm`. Aborts on the first failure rather than silently skipping; re-runs the verify afterwards as an idempotency check. |

Exit codes: `0` = all OK, `1` = drift, `2` = `npm` not on PATH (sysinfo
problem, not a pinning problem), `3` = `--fix` failed to repair.

```text
$ vco verify-pins
OK — all pinned packages match manifest.
package                                       pinned  installed  status
--------------------------------------------  ------  ---------  ------
```

(v0.2.102 removed the four diagram npm pins — `mermaid_mcp`, `excalidraw_mcp`,
`mermaid_lib`, `excalidraw_lib` — with the diagrams integration; the `[npm.*]`
table is currently empty, so `verify-pins` reports a bare OK. The machinery
stays: any future `[npm.*]` pin is verified the same way.)

### `vco verify-env-projection <project_slug_or_id>`

Confirms `.claude/settings.json env`, `.claude/env`, and
`.vscode/settings.json claude-code.env` all match the canonical projection
emitted by `vco_lib.config_projection.project_env_from_db(project_id)`.
This is the source-of-truth contract: every env value in those three
surfaces is a projection of launcher DB state, never authored by hand.

| Flag    | Behaviour                                                        |
| ------- | ---------------------------------------------------------------- |
| `--json`| Emit a single JSON envelope on stdout (machine-readable).        |
| `--fix` | Call `apply_project_env(...)` to re-project from the DB onto disk. Runs a round-trip verify afterwards; exits `3` if the second check still reports drift (broken contract). |
| `--all` | Verify every registered project in the launcher DB. Worst exit code across all projects wins. |

Exit codes: `0` = all match, `1` = drift, `2` = project not found or DB
unreadable, `3` = `--fix` failed or contract idempotency broken.

