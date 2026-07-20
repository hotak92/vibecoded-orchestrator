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

Both `install.py` Step 9 and the launcher's `create_project_v2` Tauri command call `ensure_project_env_template` (Python: `_ensure_env_template`; Rust: `ensure_project_env_template`) on the project root. The behaviour is:

- **`.env` missing** → write a fresh canonical template with all known keys. Active keys (`KG_COLLECTION`, `PROJECT_NAME`, `DEVELOPMENT_COLLECTION`, `SHARED_KG_COLLECTION`) get values substituted from the project name. Optional keys (LLM API keys, `GITHUB_TOKEN`, RL module URLs, `VCT_TELEMETRY`) stay commented out.
- **`.env` exists** → diff against the canonical key list. Any keys not yet present (commented or active) get appended in a marked block tagged `# added by vco YYYY-MM-DD`. The user's existing values are preserved verbatim — never overwritten.
- **Idempotent** — a second invocation against an up-to-date file is a no-op.

The Python and Rust canonical key lists are kept in lockstep by the cross-language test `env_template_canonical_keys_match_python` (in `commands/projects_v2.rs`). When you add a new key, update both `list_canonical_env_template_keys` (`vco_lib/env_template.py`, the Python authority) AND `env_canonical_keys` (projects_v2.rs).

Canonical keys:

```
# Service URLs (commented; launcher writes resolved values into .claude/settings.json)
WEAVIATE_URL, WEAVIATE_PORT, OLLAMA_URL, OLLAMA_PORT, CODE_EMBED_URL

# Per-project Weaviate collections (active; filled at create time)
KG_COLLECTION, SHARED_KG_COLLECTION, DEVELOPMENT_COLLECTION, PROJECT_NAME

# LLM API keys (commented)
ANTHROPIC_API_KEY, OPENAI_API_KEY

# GitHub access for search-mcp wrapper (commented)
GITHUB_TOKEN

# RL retrieval module — Pro tier (commented)
RL_SERVER_URL, RL_SERVER_PORT, RL_PROJECT_ROOT

# Telemetry (commented; opt-in only)
VCT_TELEMETRY
```

## What goes in each file

| Config | Lives in | Scope | Managed by |
|---|---|---|---|
| Effort level, max tokens, OS-level denies | `~/.claude/settings.json` | global | you, manually |
| MCP env (URLs, collection names, paths) — every Claude Code surface (CLI / Desktop / VS Code extension) AND MCP subprocesses | `.claude/settings.json` → `env` | per-project | launcher's `write_project_env_files` |
| MCP env, POSIX shell-sourceable copy (for the `tools/claude` wrapper) | `.claude/env` | per-project | launcher's `write_project_env_files` |
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
2. **`.claude/settings.json` `env` block**. The canonical per-project channel — written by the launcher's `write_project_env_files`, read by every Claude Code surface (CLI, Desktop app, VS Code extension) and propagated to MCP subprocesses. (`.vscode/settings.json` `claude-code.env` is NOT part of this chain — that surface does not propagate to MCP subprocesses on Linux.)
3. **`.claude/env`** (POSIX shell-sourceable). Same keys as #2; used by CLI users sourcing it from a shell rc via the `tools/claude` wrapper.
4. **`~/.claude.json` `mcpServers.<name>.env`**. The launcher intentionally restricts this surface to machine-invariant keys (e.g. `WEAVIATE_URL`); per-project keys like `KG_COLLECTION` are dropped here. See `launcher/src-tauri/src/mcp_registration.rs::ALLOWED_ENV_KEYS`.
5. **Bundled defaults** baked into `claude_mcp_servers/weaviate_mcp/server.py` (lowest precedence). Reaching this layer is logged at WARNING level. Explicit empty-string env values for `KG_COLLECTION` are coerced to the default rather than used literally.

The MCP startup log emits a `weaviate-kg: resolved collections (...)` line showing what the subprocess actually picked up plus the resolution source (env / hub / default); this is the diagnostic to grep for when a project is silently using the wrong KG.

## Knowledge graph env vars

The MCP server (`claude_mcp_servers/weaviate_mcp/server.py`) reads these on startup, resolved through the 5-level chain above. The launcher's `write_project_env_files` writes the canonical per-project values into both `.claude/env` (POSIX shell-sourceable) and `.claude/settings.json::env` (the channel that actually propagates to MCP subprocesses on Linux).

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
| `OPENAI_API_KEY` | (unset) | API key | Required when `ACTIVE_EMBEDDING=openai`. Resolved per-process from env; the launcher injects it from the shared keychain slot (see Secrets below). |
| `OLLAMA_URL` | `http://localhost:11435` | URL | Ollama base URL used by the qwen3 slot. |
| `CODE_EMBED_SERVICE_URL` | `http://localhost:11440` | URL | Code-embedding FastAPI service URL. |
| `CODE_EMBED_BACKEND` | `gpu` (default) | `ollama` | `gpu` → CodeSage-Large-v2 via the FastAPI service (sentence-transformers); `ollama` → routes embeds through Ollama. The Ollama-path default model is `unclemusclez/jina-embeddings-v2-base-code:latest` (768-dim); install.py overrides `CODE_EMBED_MODEL` to `qwen3-embedding:0.6b` (1024-dim) on 6-12 GB GPU hosts. |
| `CODE_EMBED_MODEL` | `codesage-large-v2` (default) | model id | Explicit code model override. |
| `DUAL_EMBEDDING_ENABLED` | `false` (default) | `true` | When true, both `qwen3_embed` and `openai_embed` slots are populated on every write so the active slot can be switched without re-indexing. |

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
2. Wrapper checks `$GITHUB_TOKEN` — if already exported (launcher's `write_project_env_files` populates it from the keychain on project registration), use it directly.
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
| `VCT_HUB_PORT` env | `7700` | Hub port override. Falls back to `<vct_root_dir>/hub.port` (written on startup), then `7700`. |
| `VCT_HUB_TOKEN` env | (unset) | Hub auth token override (tests / dev). Production reads from `<vct_root_dir>/hub.token`. |
| `VCT_STATE_DIR` env | `$HOME/.vct` | Root directory for `hub.port`, `hub.token`, `hub.pid`, `cache/`, etc. Resolution: `VCT_STATE_DIR` → `~/.vct/` → relative `./.vct/` last-resort fallback. Setting this lets dev launchers run side-by-side with production without contaminating state. |
| `<vct_root_dir>/hub.token` | — | Bearer token (32 bytes hex, OS CSPRNG). Regenerated on every hub startup, mode `0o600` on Unix. Required on every `/api/v1/*` route except `/api/v1/health` — but the two per-project `/env` + `/config` routes require a project-scoped `hub.token.<project_id>` and refuse this global token by default (`VCT_HUB_LEGACY_GLOBAL_ENV=1` on the hub reopens a compat window). Never appears in argv — clients read the file and pass via `Authorization: Bearer ...` header. |
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

## Container runtime

`vco_lib/containers.py` resolves the runtime via:

1. `VCT_CONTAINER_RUNTIME` env var — explicit `podman` or `docker`. Wins over everything when set.
2. Caller-passed `runtime` arg.
3. `auto` (or unset) → probe `podman` first, then `docker`. Podman-first is intentional: podman's rootless mode is the orchestrator's default deployment.

The chosen executable is returned as a string (`podman` or `docker`) and used uniformly through the rest of the codebase. Compose files live in `infrastructure/docker-compose.yml` (canonical) and `claude_mcp_servers/compose.yaml` (legacy path, same shared volumes).

### Forcing Docker when both runtimes are installed

Hosts with both Podman AND Docker installed default to Podman (step 3 above; see `_detect_container_runtime` at `install.py:6799`). To force Docker — for example because the Docker daemon is the one wired to team registry credentials, or because Podman's rootless mode hits a permission wall on the filesystem — export `VCT_CONTAINER_RUNTIME=docker` before running install or any container-touching hook:

```bash
export VCT_CONTAINER_RUNTIME=docker
python install.py --update          # install / update flows
.claude/hooks/ensure-containers.sh  # session-start hook
```

Persist the override by adding the export to a shell rc (`~/.bashrc` / `~/.zshrc`) or to the per-project `.claude/env` so every Claude Code session inherits it. The value wins over auto-probe and over any caller-passed `runtime` argument. Symmetric override: `VCT_CONTAINER_RUNTIME=podman` forces Podman when auto-probe would have picked Docker (unusual but possible if `podman` is installed but not first in `PATH`). Unrecognised values are logged and ignored — falling through to auto-probe.

## MCP Servers

MCP servers are registered in the user's `~/.claude.json`. Each launches via the project venv (`claude_mcp_servers/.venv`).

**weaviate-kg** — semantic search + code graph.
- Command: `claude_mcp_servers/.venv/bin/python claude_mcp_servers/weaviate_mcp/server.py`
- Env: `WEAVIATE_URL`, `OLLAMA_URL`, `EMBEDDING_MODEL`, `KG_COLLECTION`, `SHARED_KG_COLLECTION`, `DEVELOPMENT_COLLECTION`, `GRPC_PORT`, `SHARED_KG_WRITE_DISABLED` (write gate; legacy alias `SHARED_KG_OPT_OUT` kept for ~3 releases), plus the EmbeddingService vars (`ACTIVE_EMBEDDING`, `OPENAI_API_KEY`, `CODE_EMBED_SERVICE_URL`, etc.).

**search** — academic paper search via OpenAlex and arXiv.
- Command (Unix): `claude_mcp_servers/search_mcp/wrapper.sh` — exports `GITHUB_TOKEN` from the keychain (env-first then resolver), then `exec`s the real server.
- Command (Windows): `claude_mcp_servers/.venv/Scripts/python.exe claude_mcp_servers/search_mcp/server.py` (no wrapper; PowerShell resolver client handles the secret).
- Env: `OPENALEX_EMAIL` (optional, gives polite-pool priority on OpenAlex API); `GITHUB_TOKEN` (resolved at wrapper startup from the `github_pat` shared keychain slot).
- Tools: `search_papers` only. (Claude's built-in WebFetch covers ad-hoc web retrieval, so no general web-search tool is exposed.)

**mermaid** and **excalidraw** — diagram describe/extract servers. Registered in `~/.claude.json` at install but **per-project default-disabled**: `claude mcp list` shows them Connected, yet their tools aren't callable until you opt in via the launcher's Diagrams tab.

**playwright** — browser automation, enabled by default and invoked separately via `npx -y @playwright/mcp@latest`. `install.py` pre-caches it (opt out with `VCT_SKIP_PLAYWRIGHT=1`).

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
| `VCT_CONTAINER_RUNTIME=podman|docker` | Pin the container runtime instead of auto-probing. Useful in CI where both runtimes might be present but only one is configured. |
| `VCT_STATE_DIR=/path` | Override `~/.vct/` as the launcher state-root. Lets dev launchers run alongside production without contaminating state. Hub binaries pick this up automatically; resolver clients honour it too. |
| `VCT_DISABLE_HOOKS=1` | See section below. |
| `VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY=1` | **Test-only sentinel** consumed by `launcher/src-tauri/src/hub_launcher.rs:84`. When set, the launcher's hub-binary discovery skips steps 4 and 5 (in-tree dist resolution via `current_exe()` walking) and returns `None` if no other candidate matched. Production code never sets this — it exists so `cargo test` runs against `target/debug/` (where sibling cargo invocations may leave a real `vct-hub` binary) can deterministically assert "no hub anywhere". Do NOT use this as a user workaround for hub-start failures; the correct path for that is `vct-hub --start-if-not-running` (see TROUBLESHOOTING.md). |
| `VCT_RL_PULL_TOKEN_ENDPOINT=<url>` | Runtime override for the RL module's paid-module pull-token gateway URL. Short-circuits the L0 catalog / L1 manifest / hardcoded-default resolution chain inside `installer_engine::request_pull_token` and POSTs the license-key request to `<url>` verbatim. Use when the on-disk endpoint is wrong (manifest still carries a `placeholder.<tld>` URL, tenant has migrated, gateway is being staged behind a custom domain). Empty / whitespace-only values are ignored. |
| `VCT_MODULE_PULL_TIMEOUT_SECS=<n>` | Upper bound, in seconds, on a single module-image `podman`/`docker pull` during a module install or update. Default `1800` (30 min). The bound exists to catch a genuinely *stalled* registry (network black hole, half-open connection, a registry that accepts the connection but never streams layers) — without it, a stalled pull leaves the install row wedged at `status='installing'` forever (the pull future never resolves). On timeout the pull is killed and the install transitions to `status='error'` with an actionable message, then becomes retry-eligible. Raise it for unusually large GPU-variant images on a slow link. A zero, negative, or non-numeric value is **ignored** and the default is used — the bound is never disabled (a stalled pull must always be able to fail). |

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
3. `ANTHROPIC_API_KEY` direct — opt-in fallback; costs $$ per generation.
4. Silent skip — friendly log line, exits 0.

Force a specific backend with `KG_SUMMARY_BACKEND=cli|ollama|api|skip`. Override Ollama generation params with `KG_SUMMARY_OLLAMA_OPTIONS='{"temperature": 0.5, "num_ctx": 32768}'` (JSON object passed through to the Ollama API).

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
