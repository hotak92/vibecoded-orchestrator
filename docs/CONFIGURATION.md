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
| **Instance** — which Weaviate server is addressed at all | **machine-global** | `WEAVIATE_URL` → `WEAVIATE_PORT` → `http://localhost:8081` |

There is deliberately no per-project instance override. `project_kg_bindings` carries a `weaviate_url` column, but no resolver reads it — it is preserved across writes and nothing more (stated in source at `launcher/src-tauri/src/commands/binding_reconcile.rs`). Two projects on one machine are isolated by **collection namespace**, not by separate servers.

**Precedence, and why it is not "env overriding the database".** `WEAVIATE_URL` wins over `WEAVIATE_PORT` even when their ports disagree — a full URL names scheme, host *and* port, so rewriting its port from a bare port variable would make `WEAVIATE_URL` unable to mean what it says. At either level, empty or whitespace-only is treated as **unset**, not as a literal (a `.env` written on Windows and sourced on Linux carries a trailing `\r`). A non-numeric `WEAVIATE_PORT` is interpolated anyway rather than discarded, so a typo fails loudly at connect instead of quietly resolving back to 8081 and addressing whatever else is on the canonical port. Reading these variables does not compete with the launcher's database: `vco_lib/config_projection.py` writes the DB-resolved port *out* as `WEAVIATE_URL` **and** `WEAVIATE_PORT` together, so they are the transport of that value, one hop later.

**Before v0.2.96 the port variable was declared but unread** by everything except `install.py`'s own hand-written copies. On a relocated Weaviate that meant the rest of the install addressed `localhost:8081` — the *other* instance — and on the retrieval path the symptom was not an error but an empty result set. Since v0.2.96 the precedence lives in one home (`vco_lib/weaviate_helpers.py::weaviate_url_default`) that every Python call-site reaches; the MCP server keeps a deliberate copy because it must boot on half-installed environments where importing `vco_lib` fails, and a parity test executes that copy against the shared helper so the two cannot drift.

**Where the written value comes from (v0.2.97).** The value the projection writes, and the one the hub's `/config` serves as `weaviate_url`, is ONE machine resolution: `VCT_WEAVIATE_URL`, else `weaviate_url` in `vct-config.toml` (next to the launcher binary) → the `weaviate.port_override` launcher setting → the Weaviate recorded in `~/.vct/services.toml` (an adopted instance keeps its host; a parallel copy is `localhost:<its port>`) → `http://localhost:8081`. It lives in `vct-launcher-core/src/services/service_endpoints.rs` and its Python mirror `vco_lib/service_endpoints.py`, which both run `tests/fixtures/service_endpoint_parity.json`. `WEAVIATE_URL` is deliberately not one of its inputs — it is the resolver's output, and reading it back would re-project a stale value. Ollama and code-embed ports follow the same chain (override → `services.toml` → 11435 / 11440). So to point every surface at a different Weaviate, set `vct-config.toml` (or `VCT_WEAVIATE_URL` for the launcher and hub processes), restart the hub, and re-project every registered project with `python -m vco_lib.config_projection reproject-all`; a bare `WEAVIATE_URL` / `WEAVIATE_PORT` in one shell reaches only the clients started from it. The launcher's Services-card liveness probe still uses the compiled canonical port. Running two orchestrator installs with genuinely separate stacks is `VCT_FORCE_SEPARATE_CONTAINERS=1` plus the port overrides; see [`features/05-install-and-secrets.md`](features/05-install-and-secrets.md).

| Var | Default | Effect |
|---|---|---|
| `WEAVIATE_URL` | unset | Complete base URL of the Weaviate server, used verbatim. Highest precedence; also the variable the launcher's own resolver honours. |
| `WEAVIATE_PORT` | `8081` | Port on `localhost`, used only when `WEAVIATE_URL` is unset. Read by every Python call-site since v0.2.96. |
| `VCT_WEAVIATE_URL` | unset | The machine's Weaviate statement for the process that reads it (launcher, hub, or a `vco_lib` projection): the top leg of the resolution that the hub's `/config` serves and the projection writes out as `WEAVIATE_URL`. Outranks `vct-config.toml`'s `weaviate_url`. The launcher hands its own value to every `vco_lib` child it spawns. |
| `OLLAMA_URL` | `http://localhost:11435` | Ollama base URL (embeddings + the local KG-summary tier). |
| `OLLAMA_PORT` | `11435` | Host port for the Ollama container, consumed by `infrastructure/docker-compose.yml` and the install-time port probes. |
| `CODE_EMBED_SERVICE_URL` | `http://localhost:11440` | Code-embedding service base URL (see [Embedding configuration](#embedding-configuration)). |
| `CODE_EMBED_PORT` | `11440` | Host port for the code-embed container, read directly by `infrastructure/docker-compose.yml`. |
| `WEAVIATE_GRPC_PORT` | `50052` | Weaviate gRPC port used by the `weaviate-kg` MCP client and published by the compose file. `GRPC_PORT` is the legacy `.claude/settings.json` spelling and still works; the `WEAVIATE_`-prefixed name is canonical. |

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
| `ACTIVE_EMBEDDING` | `qwen3` (default) | `openai` | Selects the active text-embedding slot for KG + development collections. `qwen3` → `qwen3_embed` named vector; `openai` → `openai_embed`. |
| `EMBEDDING_MODEL` | `qwen3-embedding:0.6b` (default) | model id | Explicit text model override. When `ACTIVE_EMBEDDING=openai` and this is unset, defaults to `text-embedding-3-small`. |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` (default) | OpenAI model id | Used only when `ACTIVE_EMBEDDING=openai` and `EMBEDDING_MODEL` is unset. |
| `OPENAI_API_KEY` | (unset) | API key | Required when `ACTIVE_EMBEDDING=openai`. Resolved per process by `vco_lib.openai_key`: `$OPENAI_API_KEY` when set, else the `openai_api_key` secret through the canonical chain (launcher keychain → `~/.vct-secrets/shared/` → the project's own `.env`). `install.py --openai-key` stores it there — never in `.env`. |
| `OLLAMA_URL` | `http://localhost:11435` | URL | Ollama base URL used by the qwen3 slot. |
| `CODE_EMBED_SERVICE_URL` | `http://localhost:11440` | URL | Code-embedding FastAPI service URL. |
| `CODE_EMBED_BACKEND` | `gpu` (default) | `ollama` | `gpu` → CodeSage-Large-v2 via the FastAPI service (sentence-transformers); `ollama` → routes embeds through Ollama. The Ollama-path default model is `unclemusclez/jina-embeddings-v2-base-code:latest` (768-dim); install.py overrides `CODE_EMBED_MODEL` to `qwen3-embedding:0.6b` (1024-dim) on 6-12 GB GPU hosts. |
| `CODE_EMBED_MODEL` | `codesage-large-v2` (default) | model id | Explicit code model override. |
| `VCT_CODE_EMBED_BUILD_CONTEXT` | `<orchestrator>/claude_mcp_servers/code_embedding_service` | path | Build context for the `code_embed` image, which is the ONE VCO service BUILT from source rather than pulled. Set it when the compose file runs outside the orchestrator clone (a per-project layout does not bundle the service source). It is also where `vco doctor` and `python -m vco_lib.code_embed_image` look for the source they compare against the running service's `/health.source_sha`; point it at the same directory compose builds from, or the staleness check reports `unknown`. |
| `DUAL_EMBEDDING_ENABLED` | `false` (default) | `true` | When true, both `qwen3_embed` and `openai_embed` slots are populated on every write so the active slot can be switched without re-indexing. |
| `VCT_EMBED_REQUEST_TIMEOUT_SECS` | `180` (default) | seconds | Per-embed-REQUEST timeout inside every backend adapter (Ollama / CodeEmbed / OpenAI). Bounds a single chunk request — a wedged embedder (hung socket, dead container) fails at chunk granularity instead of hanging forever, while a slow-but-progressing run is never killed. Unset, empty, non-numeric or non-positive → the 180 s default (the guard is never disabled). Raise it on hardware where chunks legitimately take minutes (e.g. arctic on CPU); `install.py` threads an export into its KG-seed subprocess, so install-time seeding honours it too. |
| `VCO_EMBED_503_RETRY_DELAY` | (unset) | seconds | Scales the embed 503-retry backoff schedule (base 2 s / 5 s / 10 s, each +jitter). The value becomes the FIRST delay and the rest scale proportionally — `0` makes every retry sleep 0 s, `1` compresses the whole schedule. A malformed value is ignored and the base schedule runs unscaled (a knob must not be a kill switch). |
| `VCO_OLLAMA_KEEP_ALIVE` | `24h` (default) | Ollama duration | `keep_alive` pinned on every Ollama embed request so the model stays resident (Ollama's own ~5 min idle default otherwise costs a ~1.9 s model reload on the next embed). Any value Ollama accepts works: `30m`, `2h`, `-1` (never evict), `0` (opt back into immediate unload). An explicitly EMPTY value sends no `keep_alive` field at all, deferring to Ollama's server-side `OLLAMA_KEEP_ALIVE`. |

**Multi-slot fallback chain**: when `EmbeddingService.for_project()` resolves to a slot whose backend is unreachable (e.g. `codesage_embed` selected but the FastAPI service is down), it walks a fallback chain in order: codesage → qwen3 (via Ollama) → openai (when key set). The chain only fires for the code slot; the text slot resolution is strict. Diagnostic logging lands at `WARNING` level — check the MCP stderr if you suspect a fallback fired silently.

## Secrets

Secrets never live in env files or JSON configs. They live in the OS keychain (macOS Keychain, Linux Secret Service, Windows Credential Manager) and are written by the launcher's GUI or the OnboardingWizard.

**Shared bucket** (visible to every base-host project on this user account): declared in `vct-module.json::bundled_secrets[]`. The hub's `/api/v1/projects/{id}/env` resolver finds these via SENTINEL_SHARED + `module_id=user`.

| Slot | Written by | Consumed by |
|---|---|---|
| `github_pat` | OnboardingWizard `register_github_pat` step OR Preferences → Special Secrets → SecretsPanel "Shared (this user)" tab | `claude_mcp_servers/search_mcp/wrapper.sh` (exported as `GITHUB_TOKEN`), bundled hooks that need to talk to GitHub |
| `openai_api_key` | OnboardingWizard OpenAI step OR Preferences → Special Secrets | `vco_lib/embedding_service.py` when `ACTIVE_EMBEDDING=openai` or as multi-slot fallback. Validated via `GET /v1/models/text-embedding-3-small` — no token consumption, no billing entry. |

**Per-module / per-project secrets**: paid modules declare their own `bundled_secrets[]` in their manifest; the launcher's SecretsPanel surfaces a tab per scope. Per-project license keys, API tokens, and module-specific secrets are scoped by `project_id` and never leak across projects.

**Resolver flow** (subprocess perspective):

1. Wrapper script (`search_mcp/wrapper.sh` or equivalent) runs.
2. Wrapper checks `$GITHUB_TOKEN` — if already exported in its environment (by you, or by `vct exec --secret github_pat=GITHUB_TOKEN`), use it directly. The launcher never writes secret values into project files (v0.2.73), so this is not populated for you.
3. Otherwise call `vct_secrets_resolve.sh <project_path> github_pat` → hub HTTP API at `GET /api/v1/projects/{id}/env?key=github_pat`.
4. Hub resolves via SENTINEL_SHARED + `module_id=user`, applies the cross-launcher active-flag gate, returns the secret.
5. Wrapper exports the value and `exec`s the real MCP server binary.

Don't put PATs or API keys in `~/.claude.json` `env:` blocks — Claude Code's env loader does not expand `${VAR}` (anthropics/claude-code#2065, #4276), so embedded secrets would land in argv and become visible to `ps`.

## Permission matrices

The launcher enforces four independent cross-project access matrices, each backed by its own table in `launcher.db`. They do NOT share a default: **KG is default-GRANT**, the other three are **default-DENY**. Inspecting `launcher.db` (or reading Rust accessors) without this table in front of you invites the wrong assumption that everything is default-deny. The defaults are set on project add in `launcher/src-tauri/src/commands/project_state_populate.rs`.

| Matrix | Table (key) | Default on project add | How to grant more |
|---|---|---|---|
| **KG access** | `kg_collection_access` (project_id, collection_name) | **GRANT** — the project's OWN KG + the machine-shared KG get read rows automatically | Cross-project KG reads are explicit-grant (launcher Identity tab → KG access matrix) |
| **Code-graph access** | `codegraph_access` (grantor_project_id, grantee_project_id) | **DENY** (empty) | Launcher Codegraph → Cross-Project Access tab |
| **Diagrams access** | `diagram_access` (same shape as code-graph) | **DENY** (empty) | Launcher Diagrams cross-project surface |
| **Secrets** | per-`(scope, key, requester)` active flags + grants | **DENY** for cross-project | Shared-scope secrets (Preferences → Special Secrets) are readable by requesters unless paused per-project; per-project secrets are project-only unless explicitly shared via the SecretsPanel |

**Why KG grants by default but code-graph denies.** The KG read gate rejects any collection without an explicit row — so without the auto-grant, a fresh project's searches against its own KG would fail on day one. The default-grant seeds the project's own KG plus the machine-shared KG (knowledge is intentionally cross-project value: a pattern learned in one project is usually useful in the next). Source code is the opposite: it is proprietary and per-tenant, so code-graph (and diagrams, which follow the same shape) stay empty until you deliberately grant one project read access to another's. Secrets follow the same default-deny posture — cross-project reads require an explicit grant, and even a shared-scope secret can be paused for a specific requester. See the KG-vs-code-graph asymmetry table in the project `CLAUDE.md` for the read-fan-out consequences of this design.

## vct-hub

A detached local HTTP server (port 7700 default) that serves as the single source of truth for project config + secrets resolution. Lives in `launcher/dist/<arch>/vct-hub`. Outlives the launcher GUI: close the GUI, the hub keeps running so hooks / MCPs / shell scripts still resolve config.

| Var / path | Default | What it does |
|---|---|---|
| `VCT_VENV` env | (unset) | Explicit venv override — tier 1 of every venv-resolution ladder (`vco_lib/python_exe.py::ladder_candidates`; shipped wrapper `templates/scripts/vct_venv_ladder.sh` / `.ps1`). Accepts a venv DIRECTORY or the interpreter binary itself; with it unset the ladder falls back `.claude/env` → the orchestrator install's `.venv` → a VCO clone, refusing loudly when no candidate works. Read by the dependency-gated shell wrappers to hand their Python backend a known-good interpreter. |
| `VCT_HUB_PORT` env | `7700` | Hub port override. Falls back to `<vct_root_dir>/hub.port` (written on startup), then `7700`. The hub ITSELF binds, in order: its own `VCT_HUB_PORT` env → the `vct-hub-api` module's global `VCT_HUB_PORT` setting (launcher.db; 1024–65535) → `7700`. |
| `VCT_HUB_TOKEN` env | (unset) | Hub auth token override (tests / dev). Production reads from `<vct_root_dir>/hub.token`. The pin wins on every FIRST attempt; since v0.2.91, a request the hub PROVABLY refuses (401/403) is retried ONCE with the on-disk token when the two differ — see the stale-token note below. |
| `VCT_HUB_TOKEN_STRICT` env | (unset) | Set to `1` to DISABLE that one-shot fallback, so a `VCT_HUB_TOKEN` pin is authoritative even when the hub refuses it. For tests / harnesses that pin a deliberately-wrong token and assert the 401 path. |
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

**Stale `VCT_HUB_TOKEN` (v0.2.91)**: the hub regenerates `hub.token` on every start, so a shell that exported `VCT_HUB_TOKEN` before an update holds a value the hub refuses — and the env pin wins over the file, so every resolve from that shell used to fail with a misleading "hub unreachable / launcher may have restarted" diagnostic until the shell was replaced. Now, on a PROVABLE refusal (401/403) where the exported token differs from the on-disk one, every hub client — the resolver script quadruplet, the access-matrix gate trio `vct_access_check.{sh,ps1}` + `vco_lib/access_resolver.py`, `vco_lib/project_config.py`, the wrapper MCPs, the weaviate MCP's writable-collections probe, `vco verify-diagrams`, the codegraph-resync spawn registration, `vct-cli` (`launcher/tools/vct-cli`) and `vct` (`tools/vct-secrets`) — retries **once** with the on-disk token (scoped `hub.token.<project_id>` on the per-project routes, global otherwise) and prints one line to stderr:

```
stale VCT_HUB_TOKEN in env overridden by on-disk hub.token — run `unset VCT_HUB_TOKEN` or open a new shell
```

Bounded and non-destructive: exactly one extra request, nothing restarts or loops, and the retry's answer is **adopted only when it proves the on-disk token was accepted** — a `2xx`, or a `404` (which the hub answers only *after* its auth middleware accepted the bearer). Any other outcome — another refusal, a `5xx`, a transport failure — keeps the original error path, exit code and diagnostic unchanged, and does **not** print the line above. For the access-matrix gate trio that means the deliberate **fail-open to `write`** on a genuine auth failure is untouched — the fix only makes the fail-open reached less often, so a stale shell no longer silently degrades the access matrix to permissive on every call. Set `VCT_HUB_TOKEN_STRICT=1` to disable the fallback entirely — tests and harnesses that pin a deliberately-wrong token and assert the refusal must set it. The decision function lives in `vco_lib/project_config.py::_stale_env_token_fallback` (SSOT); the other implementations are parity-locked mirrors (`tests/test_stale_env_token_parity_v0291.py`).

**Cutover sentinel**: when `install.py` deploys `vct-hub` for the first time, it writes `<vct_root_dir>/v0.2.21-cutover.flag` (literal filename) BEFORE starting the hub. The launcher reads this flag on startup and skips its own in-process services watcher (knowing the hub will take it over). `install.py` deletes the flag after `vct-hub` responds to `/api/v1/health`. Leftover sentinels are harmless — the hub's first successful `/api/v1/health` clears the contention.

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
| `RL_EVENTS_RETENTION_MAX_AGE_DAYS` | `90` | Delete events older than N days. `0`/negative disables the age bound. Read fresh on every pass. |
| `RL_EVENTS_RETENTION_MAX_ROWS` | `0` (disabled) | Keep at most N most-recent rows; age is the primary bound. |
| `RL_EVENTS_RETENTION_DISABLED` | unset | Truthy → never prune (offline-training operators who want the full corpus). |
| `RL_EVENTS_RETENTION_MIN_INTERVAL_S` | `3600` | Minimum seconds between prune passes **for one process**. Throttle only; a fresh process always allows the first pass. |
| `RL_EVENTS_ARCHIVE_DIR` | `<VCT_STATE_DIR or ~/.vct>/rl_archive` | Where the prune writes its archive sidecars. Resolved **hub-side**, never from a request body — a caller-supplied path would be an arbitrary-write surface on an authenticated localhost route. |
| `RL_EVENTS_PRUNE_MAX_TASKS_PER_PASS` | `500` | How many task GROUPS one pass may move, oldest-first. Bounds the archive's in-memory row set, which is what makes the prune incremental: a long-neglected corpus drains over successive passes instead of materializing gigabytes at once. Raise it to drain a backlog faster. |

Since v0.2.91 the prune is **archive-then-delete**: victim rows are written to a compressed, loader-readable sidecar and verified before any `DELETE` runs, and a failed archive aborts the prune so nothing is deleted. See [`features/04-knowledge-and-code-graph.md`](features/04-knowledge-and-code-graph.md#retention-archive-then-delete-v0291) for the archive format and how to read one back.

## Container runtime

`vco_lib/containers.py` resolves the runtime via:

1. `VCT_CONTAINER_RUNTIME` env var — explicit `podman` or `docker`. It is a **pin**, not a preference: when set, it is the *only* candidate. If the pinned runtime is unusable (not installed, client binary refuses, daemon/machine/socket down), VCO **refuses** with an actionable message naming what you pinned, why it is unusable, and whether the other runtime is usable — it does **not** fall back to the other one. See [Why a refused pin is not a fallback](#why-a-refused-pin-is-not-a-fallback) below.
2. Caller-passed `runtime` arg.
3. `auto` (or unset) → probe `podman` first, then `docker`. Podman-first is intentional: podman's rootless mode is the orchestrator's default deployment.

The chosen executable is returned as a string (`podman` or `docker`) and used uniformly through the rest of the codebase. Compose files live in `infrastructure/docker-compose.yml` (canonical) and `claude_mcp_servers/compose.yaml` (legacy path, same shared volumes).

### Forcing Docker when both runtimes are installed

Hosts with both Podman AND Docker installed default to Podman (step 3 above; see `_detect_container_runtime` at `install.py:8920`, a thin call into `vco_lib/containers.py::resolve`). To force Docker — for example because the Docker daemon is the one wired to team registry credentials, or because Podman's rootless mode hits a permission wall on the filesystem — export `VCT_CONTAINER_RUNTIME=docker` before running install or any container-touching hook:

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

Set them in `infrastructure/.env` — compose auto-reads it from the project directory, VCO's own compose reader (`vco_lib/service_adoption.py`) merges it too, and the installer preserves unmanaged lines there on re-runs — or export them in the shell that drives compose. Nothing set → the exact pre-v0.2.97 behaviour. On SELinux-enforcing hosts, remember a swapped-in bind source needs the `:Z` flag — see [SELinux: bind-mount layouts need a `:Z` flag](TROUBLESHOOTING.md) in the troubleshooting guide.

## MCP Servers

MCP servers are registered in the user's `~/.claude.json`. Each launches via the orchestrator install's venv — canonical `<install>/.venv`, with the legacy `claude_mcp_servers/.venv` accepted as a fallback for pre-unification installs (`mcp_registration.rs::resolve_venv_python`).

**weaviate-kg** — semantic search + code graph.
- Command: `<install>/.venv/bin/python claude_mcp_servers/weaviate_mcp/server.py` (legacy installs: `claude_mcp_servers/.venv/bin/python`)
- Env: `WEAVIATE_URL`, `OLLAMA_URL`, `EMBEDDING_MODEL`, `KG_COLLECTION`, `SHARED_KG_COLLECTION`, `DEVELOPMENT_COLLECTION`, `GRPC_PORT`, `SHARED_KG_WRITE_DISABLED` (write gate; legacy alias `SHARED_KG_OPT_OUT` kept for ~3 releases), plus the EmbeddingService vars (`ACTIVE_EMBEDDING`, `OPENAI_API_KEY`, `CODE_EMBED_SERVICE_URL`, etc.).

**search** — academic paper search via OpenAlex and arXiv.
- Command (Unix): `claude_mcp_servers/search_mcp/wrapper.sh` — exports `GITHUB_TOKEN` from the keychain (env-first then resolver), then `exec`s the real server.
- Command (Windows): `<install>/.venv/Scripts/python.exe claude_mcp_servers/search_mcp/server.py` (no wrapper; PowerShell resolver client handles the secret; legacy installs use `claude_mcp_servers/.venv/Scripts/python.exe`).
- Env: `OPENALEX_EMAIL` (optional, gives polite-pool priority on OpenAlex API); `GITHUB_TOKEN` (resolved at wrapper startup from the `github_pat` shared keychain slot).
- Tools: `search_papers` only. (Claude's built-in WebFetch covers ad-hoc web retrieval, so no general web-search tool is exposed.)

**mermaid** and **excalidraw** — diagram describe/extract servers. Registered in `~/.claude.json` at install but **per-project default-disabled**: `claude mcp list` shows them Connected, yet their tools aren't callable until you opt in via the launcher's Diagrams tab.

**playwright** — browser automation, enabled by default and invoked separately via `npx -y @playwright/mcp@latest`. `install.py` pre-caches it (opt out with `VCT_SKIP_PLAYWRIGHT=1`).
- The entry stores the bare name `npx`, which Claude Code resolves from the spawn PATH at MCP-launch time. On a machine without Node.js there is nothing to resolve, so the MCP never starts — and pre-v0.2.91 nothing said so (the installer printed "the MCP will lazy-install when first invoked", which is impossible without npx). Since v0.2.91 the doctor phase probes it via `vco_lib/npx_resolver.py`, defers `npx_missing_mcp_unspawnable`, and the launcher's registration badge turns yellow with the same remediation.

**Not MCPs**: Ollama runs as infrastructure only (Weaviate text embeddings + code-embedding service CPU fallback) — there is no Ollama MCP server; Claude's native reasoning, `Read` tool, and built-in vision cover analysis, document reading, and image tasks. The code-embedding FastAPI service on port 11440 is likewise backend infrastructure. `search_papers` calls OpenAlex and arXiv directly — no local search proxy runs in the default container stack.

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

## Runtime env knobs

Set these in the per-project `.claude/env` (shell-sourced) or `.claude/settings.json` `env` (propagates to MCP subprocesses), or export them for one shell. Unlike the table above they are read at use-time, not at install-time.

| Var | Default | Effect |
|---|---|---|
| `VCT_RESYNC_SPAWN_DISABLED` | unset (spawn allowed) | Truthy (`true`/`1`/`yes`/`on`, case-insensitive) → `vco_lib/codegraph_resync.py` never spawns a background analyzer child. The gate is checked **before** the per-spawn log file is created and before any `Popen`, so a disabled run also stops writing `~/.vct/logs/resync-*.log` records. For CI runners, air-gapped installs, and anyone who wants code-graph walks strictly on demand. `tests/conftest.py` sets it for the whole suite (with an explicit opt-out list for the tests that assert spawn behaviour), the same convention as `RL_HUB_POST_DISABLED`. |
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
| `VCT_BASH_KG_THRESHOLD_CHARS` | `500` | Minimum length, in characters, of a proposed Bash command before `pre-bash-context-inject` runs a KG search on it and injects matches as additional context. Raise it to quiet the hook on medium-sized routine commands; lower it to enrich more often. |
| `VCO_QUERY_CACHE_TTL` | `900` (15 min) | Seconds a warm query-cache entry under `.claude/state/` is replayed by `pre-edit-context-inject` instead of re-querying the KG / code graph. Empty results are cached too (sentinel file), so a symbol that returns nothing isn't re-queried within the TTL. Entries are GC'd at twice this age; any cache error falls back to a live query (best-effort, never breaks injection). |
| `VCO_KG_SYNC_DEBOUNCE_SECONDS` | `5` | Quiet window, in seconds, that the kg-sync debounce waits after a `knowledge/**` edit before syncing. `0` disables debouncing — every edit syncs immediately (the pre-2026-06-18 behaviour). A non-numeric value falls back to 5. |
| `VCO_CODEGRAPH_DRAIN_MIN_INTERVAL_SECONDS` | `120` | Per-project rate limit, in seconds, between end-of-turn code-graph drains (`stop-codegraph-drain`). A second Stop inside the window skips the drain. A non-numeric value falls back to 120. |
| `VCT_SUBAGENT_MAX_DIFF` | `500` | Cap on the changed-file list `subagent-stop-reconcile` computes from a subagent's snapshot, protecting the reconcile consumers (KG-sync, code-graph drain, credential scan) from a runaway tree-wide diff (e.g. a subagent that ran a formatter pass). |
| `VCT_SNAPSHOT_DIRS` | `knowledge docs src lib launcher claude_mcp_servers .claude/scripts vco_lib templates tests` | Space-separated directories (relative to the project root) the subagent snapshot library walks. Missing directories are skipped. The same list drives the snapshot AND the diff, so override it in both places' env (any process that runs either). |
| `VCT_SNAPSHOT_CODE_EXTS` | `py\|rs\|ts\|tsx\|js\|jsx\|go\|java\|cs\|c\|cpp\|h\|hpp\|rb\|php\|swift\|kt\|scala\|sh\|ps1\|sql` | Pipe-separated code-file extensions the snapshot walk tracks (`.md` files under the snapshot dirs are always tracked). Extend it when your language is missing from the default. |
| `VCT_SNAPSHOT_PRUNE_DIRS` | `target node_modules .git .wt __pycache__ .venv dist build .next .svelte-kit .pytest_cache .mypy_cache .ruff_cache` | Space-separated directory basenames pruned from the snapshot walk (build / VCS / cache trees). Applied identically at snapshot and diff time so the two stay comparable; add a differently-named build tree here. |
| `VCT_SNAPSHOT_GC_DAYS` | `3` | Age, in days, at which orphaned subagent snapshots under `.claude/state/` are garbage-collected (a killed agent whose Stop hook never fired would otherwise leak its snapshot forever). Live agents' snapshots are always fresh enough to be untouched. |
| `VCT_WORKTREE_GUARD_ENFORCE` | unset | `=1` hard-blocks (exit non-zero) only the belt-and-suspenders branch of `worktree-guard` — a payload that supplies an explicit worktree path equal to the parent checkout. Ordinary spawns create an isolated worktree unconditionally; this flag does not gate that. |
| `VCO_STOP_FAILURE_NOTIFY` | `1` | Per-hook kill switch for the StopFailure desktop notification: `0` suppresses ONLY the notification. The metrics ledger (`~/.claude/metrics/failures.jsonl`) still records **every** failure, suppressed ones included — that ledger is what made a 2026-09-20 field storm of 304 identical pop-ups diagnosable, so it is never gated. Distinct from `VCT_DISABLE_HOOKS`, which disables all hooks. Read by `stop-failure-notify.{sh,ps1}`, not by any daemon. |
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
`model_router/vendors.py`); and, for QwenCloud — whose Token Plan publishes no quota anywhere — the tokens this
gateway relayed for it since the start of the month, labelled as tokens, never as a percentage. All the vendor
sources are undocumented: anything missing, unreadable, older than 30 minutes or past its own reset reads `null`
("unknown") with the reason, never 0 % or 100 %. The answer comes from the gateway's cache, which is refreshed in the
background at most once every 4–5 minutes (jittered, one refresh in flight at a time) when either a read finds it due
or chat requests are passing through the gateway — the second keeps the numbers warm, so a session started during
active use finds them fresh in its model picker. There is no timer: a gateway with no chat traffic and no readers
makes no vendor calls, and no request ever waits on a vendor. `?format=line` answers one line of text — what the status-line script prints — and
the launcher's home page shows the same data as bars with reset countdowns.

**Usage in the `/model` picker.** The same data also rides in the picker itself, as text on each vendor row's
label: `glm-5.3 · Z.ai subscription · 1M ctx · 5h 10% · wk 72% used`, and for QwenCloud `… · 1.2M tokens used this
month` (or `since Sep 2` when the ledger covers only part of the month). It always reads *used*, shortest window
first; an unknown window is left out and a vendor with nothing known gets no suffix at all. Claude Code fetches
`/v1/models` once, when a session starts, and never refreshes it, so the label is a snapshot of that moment — when
the reading was already older than one refresh interval then, it says so as a clock time, `(as of 14:05)`. Building
the list never waits on a vendor: a cold cache answers without the text and starts the refresh, so the next session
has it. Claude's own windows cannot appear there: the client fills the picker with its built-in Claude rows and
discards the gateway's rows for the same models, text included — read them from the status line or the launcher.
Only the display text changes; model ids never do. Turn it off with `VCT_MODEL_GATEWAY_PICKER_USAGE=off`.

**Status line.** `.claude/scripts/gateway-usage-statusline.sh` (`.ps1` on Windows) prints that line for Claude Code's
`statusLine`, e.g. `Claude 5h 31% · wk 27% · Fable 12% │ GLM 5h 10% · wk 72% │ Qwen 1.2M tok/mo`, and prints
nothing at all when the gateway is not running or refuses it. VCO does not add it to your settings — a project-level
`statusLine` would override one you set in `~/.claude/settings.json` — so enable it yourself:
`"statusLine": {"type": "command", "command": "bash .claude/scripts/gateway-usage-statusline.sh"}` (Windows:
`"pwsh -NoProfile -File .claude/scripts/gateway-usage-statusline.ps1"`). The status line is drawn by the terminal
client (`claude`); the VS Code panel does not render `statusLine`.

**Panel mode — `remote-control` vs `multimodel`.** The launcher's status-bar pills (CLI: `python -m vco_lib.vscode_settings mode --set {multimodel,remote-control} --path <settings.json>`) flip the VS Code Claude Code panel between two states, one at a time — `claudeCode.environmentVariables` is VS Code machine-scope, so there is no per-workspace split. **`remote-control` is the stock client**: the four routing keys and the login-prompt key are removed, the panel talks to api.anthropic.com again, and the `/model` picker, the context-window sizing and the token accounting are all Claude Code's own. VCO writes none of `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, `CLAUDE_CODE_DISABLE_1M_CONTEXT` or `CLAUDE_CODE_AUTO_COMPACT_WINDOW` in *either* mode — that absence is what leaves the accounting native; a knob you set yourself is carried through untouched. **`multimodel` points the panel at the gateway**: the picker becomes the gateway's `/v1/models` catalog (the Z.ai subscription's GLM models, the QwenCloud Token-Plan models, and Claude in one list), and the client takes the context window from the model ID, so pick the "(1M context)" rows — their ids carry the `[1m]` suffix — when you want the 1M budget; ids only the gateway can resolve are stashed on the way to `remote-control` and restored on the way back. Remote Control (`/remote-control`, phone access) therefore works only in `remote-control` mode: Claude Code >= 2.1.196 refuses it whenever `ANTHROPIC_BASE_URL` is not api.anthropic.com, and a claude.ai sign-in does not bypass that. To have both at once, leave the panel on the gateway and run a detached native-auth server with the bundled `rc-native` skill (see [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md), "Remote Control").

| Var | Default | Effect |
|---|---|---|
| `VCT_MODEL_GATEWAY_PORT` | `11436` | TCP port. Falls back to the port file `<vct-state-dir>/model-gateway.port` (written by the running daemon, same convention as `hub.port`), then to the default. A non-numeric or out-of-range value falls through the same chain rather than being used literally. |
| `VCT_MODEL_GATEWAY_HOST` | `127.0.0.1` | Bind address. REFUSED at startup unless it resolves to a loopback address: the gateway proxies under your Claude login and is authorised by a local file token, so binding a routable interface is an error, not a configuration. |
| `VCT_MODEL_GATEWAY_CREDENTIALS` | `~/.claude/.credentials.json` | Path to the Claude CLI's OAuth credentials file (honours `VCT_CLAUDE_DIR`, see next section). Harness-owned: the gateway only ever reads it, never writes or copies it. |
| `VCT_MODEL_GATEWAY_CONTEXT_TABLE` | `<vct-state-dir>/model-gateway/chat_model_context.json` | Where the daemon looks for an exported chat-model context table (schema: `model_router.context_table`). Written by the launcher — on boot and on every GUI edit of the table (`launcher/src-tauri/src/commands/chat_model_context.rs`: `seed_and_export_on_boot`, `upsert_and_export`, `export_now`). Absent only before the launcher's first run; the daemon then reads the shipped seed (`claude_mcp_servers/model_router/chat_model_context.seed.json`), and the export wins per ROW over it. |
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

The three TTL knobs exist primarily so the smoke tests can drive the caches without sleeping; they are documented because a knob nobody can find is a knob that gets re-invented.

**Vendor registry — what ships, and how a vendor key is named.** The gateway's vendors are declared in one file, `claude_mcp_servers/model_router/vendors.py`; two routes ship. The Z.ai subscription (`glm_api_key` or `zai_api_key`, shared scope) publishes its models under `claude-gw/…`. The QwenCloud Token-Plan route (v0.2.96, `qwen_api_key` or `QWEN_API_KEY` — the uppercase spelling is the vendor docs' own env convention and the keychain stores the name the user typed; shared scope, same resolution path as the other vendor keys) publishes under the nested namespace `claude-gw/qwen/…`, so every panel rule that keys on the `claude-gw/` prefix keeps working, and its bare model ids (`qwen…`, `deepseek…`) route to it without the prefix too; the pay-as-you-go QwenCloud endpoint is deliberately absent until its model list is extracted and proven. The Token-Plan endpoint's anthropic app path has no model-list route, but the subscription's OpenAI-compatible base serves one (live-verified 2026-09-22), so its row points the catalog fetch there with `catalog_url` — a registry field carrying an absolute URL used INSTEAD of `upstream + catalog_path`, for a vendor whose list endpoint lives on another base — and drops the non-chat ids that list carries (voice, image, the `auto` router alias) with `catalog_exclude_prefixes`, a registry field of id prefixes applied to the live list only (the row's curated fallback needs no filter; excluded ids appear in neither the picker nor `_vct_catalog_hidden`). `/health` reports the family's catalog source as `live` when that fetch answers and `declared` when the row's nine declared ids answer instead — no key resolved yet, or the fetch failed (`declared` names the row's own list as what is being served; `static` is the shipped `static_catalog.json` snapshot; a declared fallback after a failed fetch retries on the short TTL, while a row with no list endpoint at all has nothing to retry). One catalog-truth rule applies to a vendor whose endpoint LISTS ids that reroute server-side to other models: the row's `verified_ids` names the ids verified to answer as themselves, the catalog withholds every other listed id (they appear in `_vct_catalog_hidden`, namespaced, with one INFO log line per changed withheld set), and `VCT_MODEL_GATEWAY_CATALOG=all` does not bring them back — an id that answers as a different model is not a capability to unlock. Adding a vendor stays a row in that file, never a code change; `vct-model-gateway --check` validates the registry at startup. Three filter concepts, deliberately distinct: `verified_ids` is the TRUTH filter (ids that reroute server-side are withheld, and no knob brings them back); `catalog_exclude_prefixes` drops non-model ids (router aliases, voice/image modalities) from the catalog entirely; `catalog_hide_ids` CURATES — an id the vendor defers to a later discussion stays reported in `_vct_catalog_hidden` and routable by name but never publishes under either catalog filter, even when a live refresh lists it or it would win the latest ranking. The advertised list this produces (owner ruling 2026-09-22): the first-party Claude 5 family; z.ai's glm-5.3 and glm-5.3-flash; qwen's glm-5.3, deepseek-v4.1-flash, qwen3.8-max and qwen3.8-flash.

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

Three more names belong to the same family and are also **internal — do not set them**. They are shell variables shared between a hook and the `_lib` helper it sources, and they are listed here only so their names are not a mystery in a process listing or a `set` dump:

| Var | Meaning |
|---|---|
| `VCO_CODE_EXT_RE` | The one code-file extension alternation, exported by `templates/hooks/_lib/code-extensions.sh` (`.ps1`: `$script:VcoCodeExtRe`). Read by the context hooks and by `_lib/route-touched-path.sh` so "is this a code file?" has ONE answer. Setting it would change which files reach the code graph. |
| `VCO_ROUTE_NUDGE` | The LLM-visible text `_lib/route-touched-path.sh` leaves for its caller (`post-file-edit.sh` / `post-bash-file-sync.sh`) to emit as one `additionalContext` envelope — currently the pending KG duplicate-scan report. An input value is overwritten on the first routed path. |
| `VCO_MISSING_LIB_NOTICE` | The broken-install notice `vco_report_missing_hook_lib` (`templates/hooks/_lib/emit-context.sh`; `.ps1`: the return value of `Emit-VcoMissingHookLibNotice`) leaves for its caller to emit — set when a shipped `_lib/` helper the hook needs is missing, empty when the condition was already reported this session (sentinel `.claude/state/route_lib_missing_<session>_<lib>`). An input value is always overwritten. |

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
2. Ollama at `http://localhost:11435` — automatic fallback; works for any VCO user since Ollama is already required for embeddings. Default model `qwen3.5:9b` (16+ GB VRAM) or `gemma4:e4b` for low-VRAM hosts.
3. OpenAI — opt-in, and gated twice: it needs `OPENAI_API_KEY` **and** consent (`kg_summary_openai_consent` in launcher Preferences → KG Summaries, or the operator flag `--force-api`). Costs $$ per generation.
4. `ANTHROPIC_API_KEY` direct — opt-in fallback; costs $$ per generation.
5. Silent skip — friendly log line, exits 0.

Force a specific backend with `KG_SUMMARY_BACKEND=cli|ollama|openai|api|skip` (`api` = Anthropic direct, `openai` = OpenAI). Override Ollama generation params with `KG_SUMMARY_OLLAMA_OPTIONS='{"temperature": 0.5, "num_ctx": 32768}'` (JSON object passed through to the Ollama API).

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

A separate `PreToolUse` hook validates frontmatter on every write to `knowledge/**/*.md` and blocks writes missing required fields (`title`, `type`, `tags`, `created`, `updated`, `status`). The summary generator depends on these.

## Manual operator scripts

Two helper scripts under `claude_mcp_servers/scripts/` are not wired into the
default workflow but are kept available for operators upgrading older
installations:

- **`migrate_to_new_embeddings.py`** — One-shot migration that adds new named
  vectors (qwen3_embed for KG, codesage_embed for code) to Weaviate
  collections alongside any legacy named vectors (ollama_embed,
  ollama_code_embed). Preserves all existing data; only adds the new vector
  slots and backfills them. Run manually after upgrading the embedding
  models referenced in the MCP configuration.

- **`generate_node_formats.py`** — Manual `--all` backfill that regenerates
  per-node descriptions / summaries in `knowledge/.node_formats.json`.
  Normally produced incrementally by the kg-summary-generator hook on edit;
  use this script when you want to rebuild the cache from scratch (for
  example after a bulk import or a node-format schema change).

## `vco` CLI — verify commands (Phase 0 of the diagrams integration)

The `vco` console-script is registered on PATH automatically when
`install.py` runs (it does `pip install -e .` against the orchestrator's
`pyproject.toml` after creating `.venv/`, so `vco` lands at
`.venv/bin/vco` on Unix and `.venv\Scripts\vco.exe` on Windows). For
manual / out-of-band installs, run `pip install -e .` from the repo root
into any venv. Fall back to `python -m vco_lib.cli <subcommand>` if for any
reason the entry point isn't on PATH. Phase 0 ships two acceptance
verifiers:

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
claude-mermaid                                1.4.2   1.4.2      match
@sanjibdevnathlabs/mcp-excalidraw-local       0.3.1   0.3.1      match
```

### `vco verify-env-projection <project_slug_or_id>`

Confirms `.claude/settings.json env`, `.claude/env`, and
`.vscode/settings.json claude-code.env` all match the canonical projection
emitted by `vco_lib.config_projection.project_env_from_db(project_id)`.
This is the source-of-truth contract codified by the diagrams plan: every
env value in those three surfaces is a projection of launcher DB state,
never authored by hand.

| Flag    | Behaviour                                                        |
| ------- | ---------------------------------------------------------------- |
| `--json`| Emit a single JSON envelope on stdout (machine-readable).        |
| `--fix` | Call `apply_project_env(...)` to re-project from the DB onto disk. Runs a round-trip verify afterwards; exits `3` if the second check still reports drift (broken contract). |
| `--all` | Verify every registered project in the launcher DB. Worst exit code across all projects wins. |

Exit codes: `0` = all match, `1` = drift, `2` = project not found or DB
unreadable, `3` = `--fix` failed or contract idempotency broken.

### `vco verify-diagrams <project_slug_or_id>`

End-to-end verifier for the Diagrams Integration feature. Runs 13
focused checks covering: project row in launcher DB, `project_modules`
seed row, migration 022 applied, MCP wrappers registered in
`~/.claude.json`, hub allowlist HTTP route alive, env projection
across the three surfaces, per-project Weaviate `<Project>_Diagrams`
class present, `PreToolUse` + `PostToolUse` hooks registered, hook
scripts on disk + executable, `vco_lib.diagram_indexer` /
`vco_lib.diagram_paths` importable, CLAUDE.md diagrams section
rendered.

| Flag      | Behaviour                                                       |
| --------- | --------------------------------------------------------------- |
| `--json`  | Emit a single JSON envelope on stdout (machine-readable).        |
| `--fix`   | Best-effort repair where possible (re-seed `project_modules` row, re-project env, create minimal Weaviate class). Logs and continues per-check on failure — unlike `verify-pins` which aborts on the first failure. |
| `--all`   | Iterate every project registered in the launcher DB. Worst exit code across all projects wins. |
| `--quick` | Skip the slow checks (Weaviate connectivity, hub HTTP probe). Useful in CI / pre-commit hooks. |

Example output:

```text
$ vco verify-diagrams demo --quick
verify-diagrams: demo (project_id=p-1)
  folder: /home/me/projects/demo

  [OK]   project_row — project 'demo' (id=p-1)
  [OK]   project_modules_row — project_modules('diagrams', enabled=1) row present
  [OK]   migration_022 — migration 22 applied + all 6 tables present
  [OK]   mcp_wrappers — mermaid + excalidraw wrappers registered with correct module path
  [SKIP] hub_allowlist — --quick: hub HTTP probe skipped
  [FAIL] env_projection — 1 drift entries: DIAGRAMS_COLLECTION on .vscode/settings.json: expected 'Demo_Diagrams', got '<missing>'
         > fix: vco verify-env-projection p-1 --fix
  [SKIP] weaviate_diagrams_class — --quick: Weaviate connectivity check skipped
  [OK]   pretooluse_hooks — both PreToolUse entries (Write|Edit + MCP matchers) present
  [OK]   post_delete_hook — PostToolUse Bash entry → post-file-delete registered
  [OK]   hook_scripts_on_disk — all 2 hook scripts present + executable
  [OK]   indexer_importable — key functions resolvable
  [OK]   path_validator — round-trip OK (good→None, bad→string)
  [OK]   claude_md_section — diagrams section present

Summary: 10 OK, 1 FAIL, 2 SKIP
```

Exit codes: `0` = all OK (or only SKIP/FIXED), `1` = at least one
FAIL, `2` = environment problem (project not in launcher DB, DB
unreadable), `3` = `--fix` ran but failed to repair at least one
check.
