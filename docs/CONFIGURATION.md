# Configuration Philosophy

Config layout follows one rule: **minimal global, maximum per-project**.

## What this means

- **Global `~/.claude/settings.json`**: user preferences only — effort level, output tokens, universal permission denies. No project paths, no MCP server URLs, no environment variables that any specific project depends on.
- **Per-project `.claude/settings.json`**: per-project permissions and hook registrations, plus an `env` block read by Claude Code (CLI, Desktop app, AND the VS Code extension) and propagated to MCP subprocesses. This is the **canonical per-project MCP env channel** as of v0.2.12 (PR-27, 2026-05-16): empirical sentinel testing on Linux Claude Code 2.1.143 confirmed that `.vscode/settings.json` `claude-code.env` does NOT propagate to MCP subprocesses, so the launcher no longer writes that key. See PR-27 commit message + `docs/CLAUDE_CODE_COMPATIBILITY.md` → "Per-project env files" for the full empirical trace.
- **Per-project `.claude/env`**: POSIX shell-sourceable env file with the same values, for CLI users who source it from their shell rc via the `tools/claude` wrapper.
- **Per-project `.vscode/settings.json`**: VS Code editor preferences only (Pylance excludes, file-watcher excludes, formatter settings). The launcher's Python-side `_backfill_vscode_excludes_in_project` manages the Pylance/watcher exclude block; the launcher does NOT touch any `claude-code.env` block here. Since v0.2.21 the launcher also writes `.vscode/tasks.json` with a `folderOpen` task that ensures `vct-hub` is running for VS Code users (Step 8).
- **Per-project secrets**: stored in the OS keychain via the VCT Launcher GUI — not in env files, not in JSON configs. The launcher knows about per-project scoping, so an OpenAI key for one project doesn't leak into another. A small set of shared secrets (`github_pat`, `openai_api_key`) lives under SENTINEL_SHARED / `module_id=user` and is resolved by the hub for every project — see "Secrets" below.

## Why

It prevents cross-contamination. Global settings apply to every project you open — set `KG_COLLECTION=MyMainProjectKG` globally and every other project will silently reuse that collection and mix knowledge graphs.

## Setup for new users

1. Copy `.vscode/settings.json.example` to `.vscode/settings.json` and adjust as needed for editor preferences (Pylance excludes, formatter settings). The example file no longer ships a `claude-code.env` block — per-project MCP env now lives in `.claude/settings.json` `env` instead (see the v0.2.12 PR-27 note above).
2. The VCT Launcher creates a per-project `.env` from a canonical template when you register a project (see "`.env` template management" below). For non-launcher CLI users, copy `.env.example` manually.
3. Let `install.py` wire the rest (venv, containers, KG collection creation, `vct-hub` binary placement + boot sentinel).
4. Launch via the VCT Launcher GUI (manages secrets, tier gating, module installs). Secrets entered via the GUI (or via the OnboardingWizard at first run) are immediately resolvable by the hub for every registered project.

## `.env` template management

Both `install.py` Step 9 and the launcher's `create_project_v2` Tauri command call `ensure_project_env_template` (Python: `_ensure_env_template`; Rust: `ensure_project_env_template`) on the project root. The behaviour is:

- **`.env` missing** → write a fresh canonical template with all known keys. Active keys (`KG_COLLECTION`, `PROJECT_NAME`, `DEVELOPMENT_COLLECTION`, `SHARED_KG_COLLECTION`) get values substituted from the project name. Optional keys (LLM API keys, `GITHUB_TOKEN`, RL module URLs, `VCT_TELEMETRY`) stay commented out. (`CONVERSATION_COLLECTION` removed 2026-04-30 — capture flow deprecated.)
- **`.env` exists** → diff against the canonical key list. Any keys not yet present (commented or active) get appended in a marked block tagged `# added by vco YYYY-MM-DD`. The user's existing values are preserved verbatim — never overwritten.
- **Idempotent** — a second invocation against an up-to-date file is a no-op.

The Python and Rust canonical key lists are kept in lockstep by the cross-language test `env_template_canonical_keys_match_python` (in `commands/projects_v2.rs`). When you add a new key, update both `_env_canonical_template` (install.py) AND `env_canonical_keys` (projects_v2.rs).

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
| VS Code `folderOpen` task that ensures `vct-hub` is running (v0.2.21+) | `.vscode/tasks.json` | per-project | install.py Step 8 / `update_project_v2` bundle update |
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

## Knowledge graph env vars

The MCP server (`claude_mcp_servers/weaviate_mcp/server.py`) reads these on startup. They're written to the two per-project surfaces — `.claude/env` (POSIX shell-sourceable) and `.claude/settings.json::env` (the canonical channel that actually propagates to MCP subprocesses on Linux) — by the launcher's `write_project_env_files`. The historical third surface (`.vscode/settings.json` `claude-code.env`) was removed in v0.2.12 (PR-27, 2026-05-16); see the bullet at the top of this file for the empirical-trace KG node.

| Var | Default | What it does |
|---|---|---|
| `KG_COLLECTION` | `<ProjectName>` | Per-project Weaviate collection. Knowledge nodes from `knowledge/` land here. |
| `DEVELOPMENT_COLLECTION` | `<ProjectName>_development` | Per-project Weaviate collection for `docs/`. Auto-paired with KG by the launcher. Same chunker + named-vector slot logic as KG. |
| `SHARED_KG_COLLECTION` | `VibeCodedOrchestrator_KnowledgeGraph` (renamed from `VibeCodedTools_KnowledgeGraph` in v0.2.12) | Cross-project shared KG. All projects on this machine query it alongside their own KG. Seeded by `install.py` Step 7d from `vibecoded-orchestrator/knowledge/`. Users with data under the old name can designate it as canonical via the launcher's Identity tab "Manage shared KG collection" picker — see PR-26 Group E. |
| `SHARED_KG_WRITE_DISABLED` | `false` | Per-project WRITE gate (asymmetric model since 2026-05-01). Set to `true` (or `1`/`yes`) to refuse `store_knowledge_node(scope="shared")` calls from this project. **Reads are unconditional** — every project always queries the shared KG when configured. |
| `SHARED_KG_OPT_OUT` | `false` | Legacy alias of `SHARED_KG_WRITE_DISABLED`. Kept for back-compat ~3 releases (target removal: 2026-08). The canonical key wins when both are set. NOTE: pre-2026-05-01 this also gated reads — that behaviour is gone. |
| `SHARED_KG_NODE_FORMATS` | (unset) | Override path for the shared KG's `.node_formats.json` sidecar. Used by tests; in production the sidecar is read from `<orchestrator>/knowledge/.node_formats.json` via `_SERVER_INFERRED_BASE`. |
| `KG_TIER_MIN` / `KG_TIER_SINGLE_CHUNK` / `KG_TIER_THREE_CHUNKS` / `KG_TIER_FULL` | `0.42` / `0.55` / `0.65` / `0.75` | Score thresholds for the auto-tier retrieval system. See `knowledge/concepts/score-driven-retrieval-tiers.md`. |

**Asymmetric semantics (since 2026-05-01)**: `SHARED_KG_WRITE_DISABLED=true` refuses `store_knowledge_node(scope="shared")` calls with a clear error (`"Shared KG writes are disabled for this project. Set SHARED_KG_WRITE_DISABLED=false to enable, or use scope='project' for the per-project KG."`) — NOT a silent reroute to the project KG. `hybrid_search` / `semantic_graph_search` continue to merge the shared collection regardless of the gate; reads are never disabled per-project. Pre-2026-05-01 the same flag also zeroed reads — that behaviour is gone, by design.

**Power-user override**: point `SHARED_KG_COLLECTION` at a private team-shared collection (e.g. `AcmeTeam_SharedKG`) to share knowledge across an internal team without exposing it via the public bundled name.

## Embedding configuration (v0.2.18+)

The `EmbeddingService` (in `vco_lib/embedding_service.py`) is the unified entry point for KG + code-graph embeddings. It replaces the older code paths that read `ACTIVE_EMBEDDING` directly and hardcoded slot selection (KG-W1 audit). Configuration is purely env-driven; the launcher writes the resolved values into `.claude/settings.json::env`.

| Var | Values | What it does |
|---|---|---|
| `ACTIVE_EMBEDDING` | `qwen3` (default) | `openai` | Selects the active text-embedding slot for KG + development collections. `qwen3` → `qwen3_embed` named vector; `openai` → `openai_embed`. |
| `EMBEDDING_MODEL` | `qwen3-embedding:0.6b` (default) | model id | Explicit text model override. When `ACTIVE_EMBEDDING=openai` and this is unset, defaults to `text-embedding-3-small`. |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` (default) | OpenAI model id | Used only when `ACTIVE_EMBEDDING=openai` and `EMBEDDING_MODEL` is unset. |
| `OPENAI_API_KEY` | (unset) | API key | Required when `ACTIVE_EMBEDDING=openai`. Resolved per-process from env; the launcher injects it from the shared keychain slot (see Secrets below). |
| `OLLAMA_URL` | `http://localhost:11435` | URL | Ollama base URL used by the qwen3 slot. |
| `CODE_EMBED_SERVICE_URL` | `http://localhost:11440` | URL | Code-embedding FastAPI service URL. |
| `CODE_EMBED_BACKEND` | `service` (default) | `ollama` | `service` → CodeSage-Large-v2 via the FastAPI service; `ollama` → CPU fallback via `qwen3-embedding:0.6b`. |
| `CODE_EMBED_MODEL` | `codesage-large-v2` (default) | model id | Explicit code model override. |
| `DUAL_EMBEDDING_ENABLED` | `false` (default) | `true` | When true, both `qwen3_embed` and `openai_embed` slots are populated on every write so the active slot can be switched without re-indexing. |

**Multi-slot fallback chain**: when `EmbeddingService.for_project()` resolves to a slot whose backend is unreachable (e.g. `codesage_embed` selected but the FastAPI service is down), it walks a fallback chain in order: codesage → qwen3 (via Ollama) → openai (when key set). The chain only fires for the code slot; the text slot resolution is strict. Diagnostic logging lands at `WARNING` level — check the MCP stderr if you suspect a fallback fired silently.

## Secrets

Secrets never live in env files or JSON configs. They live in the OS keychain (macOS Keychain, Linux Secret Service, Windows Credential Manager) and are written by the launcher's GUI or the OnboardingWizard.

**Shared bucket** (visible to every base-host project on this user account): declared in `vct-module.json::bundled_secrets[]`. The hub's `/api/v1/projects/{id}/env` resolver finds these via SENTINEL_SHARED + `module_id=user` (formerly `installer` pre-2026-05-10; both writer paths now land at the same row).

| Slot | Written by | Consumed by |
|---|---|---|
| `github_pat` | OnboardingWizard `register_github_pat` step OR Preferences → Special Secrets → SecretsPanel "Shared (this user)" tab | `claude_mcp_servers/search_mcp/wrapper.sh` (exported as `GITHUB_TOKEN`), bundled hooks that need to talk to GitHub |
| `openai_api_key` (v0.2.18+) | OnboardingWizard OpenAI step OR Preferences → Special Secrets | `vco_lib/embedding_service.py` when `ACTIVE_EMBEDDING=openai` or as multi-slot fallback. Validated via `GET /v1/models/text-embedding-3-small` — no token consumption, no billing entry. |

**Per-module / per-project secrets**: paid modules declare their own `bundled_secrets[]` in their manifest; the launcher's SecretsPanel surfaces a tab per scope. Per-project license keys, API tokens, and module-specific secrets are scoped by `project_id` and never leak across projects.

**Resolver flow** (subprocess perspective):

1. Wrapper script (`search_mcp/wrapper.sh` or equivalent) runs.
2. Wrapper checks `$GITHUB_TOKEN` — if already exported (launcher's `write_project_env_files` populates it from the keychain on project registration), use it directly.
3. Otherwise call `vct_secrets_resolve.sh <project_path> github_pat` → hub HTTP API at `GET /api/v1/projects/{id}/env?key=github_pat`.
4. Hub resolves via SENTINEL_SHARED + `module_id=user`, applies the cross-launcher active-flag gate, returns the secret.
5. Wrapper exports the value and `exec`s the real MCP server binary.

Don't put PATs or API keys in `~/.claude.json` `env:` blocks — Claude Code's env loader does not expand `${VAR}` (anthropics/claude-code#2065, #4276), so embedded secrets would land in argv and become visible to `ps`.

## vct-hub (since v0.2.21)

A detached local HTTP server (port 7700 default) that serves as the single source of truth for project config + secrets resolution. Lives in `launcher/dist/<arch>/vct-hub`. Outlives the launcher GUI: close the GUI, the hub keeps running so hooks / MCPs / shell scripts still resolve config.

| Var / path | Default | What it does |
|---|---|---|
| `VCT_HUB_PORT` env | `7700` | Hub port override. Falls back to `<vct_root_dir>/hub.port` (written on startup), then `7700`. |
| `VCT_HUB_TOKEN` env | (unset) | Hub auth token override (tests / dev). Production reads from `<vct_root_dir>/hub.token`. |
| `VCT_STATE_DIR` env | `$HOME/.vct` | Root directory for `hub.port`, `hub.token`, `hub.pid`, `cache/`, etc. Resolution: `VCT_STATE_DIR` → `~/.vct/` → relative `./.vct/` last-resort fallback. Setting this lets dev launchers run side-by-side with production without contaminating state. |
| `<vct_root_dir>/hub.token` | — | Bearer token (32 bytes hex, OS CSPRNG). Regenerated on every hub startup, mode `0o600` on Unix. Required on every `/api/v1/*` route except `/health`. Never appears in argv — clients read the file and pass via `Authorization: Bearer ...` header. |
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

**Boot autostart** is OS-specific and DEFAULT-OFF in v0.2.21. Users opt in via launcher GUI Preferences (Step 13 follow-up). Backends:

- Linux: systemd-user unit (`~/.config/systemd/user/vct-hub.service`).
- macOS: `launchd` plist (`~/Library/LaunchAgents/com.vibecodedtools.vct-hub.plist`).
- Windows: Scheduled Task at logon (`VCTHub` task, invoked via a thin `.cmd` shim that points at the binary).

When `VCT_STATE_DIR` is non-default, boot registration prints a warning — the autostart will inherit the user's login env, where a custom `VCT_STATE_DIR` typically isn't set, so the booted hub will write to `~/.vct/` instead of the dev path. This is intentional (dev state shouldn't be auto-launched at login).

**Key endpoints**:

| Endpoint | Purpose | Notes |
|---|---|---|
| `GET /health` | Liveness probe | No auth required. |
| `GET /api/v1/projects/{id-or-slug}/config` | Resolver: KG collection, codegraph prefix, embedding selections, access-matrix lists, service URLs | Accepts UUID or slug as the `{id}` path arg (try-UUID-then-slug fallback). Replaces per-process `os.getenv("KG_COLLECTION")` etc. drift. Returns 503 when primary KG binding is missing (caller-actionable). |
| `GET /api/v1/projects/{id}/env?key=<slot>` | Secrets resolver | Resolves via per-project keychain row first; falls back to SENTINEL_SHARED + `module_id=user` for shared slots declared in `vct-module.json::bundled_secrets[]`. |
| `GET /api/v1/services/status` | Services snapshot | v0.2.21: returns a degraded skeleton. Supervisor relocation to hub (Step 24 Stream B) brought the full snapshot back. |
| `GET /api/v1/projects/by-path?path=<abs-path>` | Path → project UUID | Used by resolver clients before fetching `/config`. Returns 404 with `project_not_found` when the path isn't registered. |

**Resolver clients** discover the hub via the same chain:

- `templates/scripts/vct_project_config.sh` (bash, hooks + shell scripts)
- `templates/scripts/vct_project_config.ps1` (PowerShell 7+, Windows hooks)
- `vco_lib/project_config.py` (`from vco_lib.project_config import resolve, ProjectConfig` — used by `install.py`, MCPs, and any Python tooling)

Discovery: `VCT_HUB_PORT` env → `<vct_root_dir>/hub.port` → `7700` default; token: `VCT_HUB_TOKEN` env → `<vct_root_dir>/hub.token`. All clients enforce the same exit-code shape (0 success / 1 hub unreachable / 2 project not registered / 3 service misconfigured / 4 field not found / 64 usage error). Stderr emissions are rate-limited per `(pid, error_kind)` to one line per 5 minutes — `VCO_HOOK_DEBUG=1` bypasses the limit.

**v0.2.20 → v0.2.21 cutover sentinel**: when `install.py` deploys `vct-hub` for the first time, it writes `<vct_root_dir>/v0.2.21-cutover.flag` BEFORE starting the hub. The v0.2.21 launcher reads this flag on startup and skips its own in-process services watcher (knowing the hub will take it over). `install.py` deletes the flag after `vct-hub` responds to `/health`. Leftover sentinels are harmless — the hub's first successful `/health` clears the contention.

## Paid-module license framework (v0.2.14+)

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

Hosts with both Podman AND Docker installed default to Podman (step 3 above). To force Docker — for example because your Docker daemon is the one wired to your team's registry credentials, or because Podman's rootless mode hits a permission wall on your filesystem — export `VCT_CONTAINER_RUNTIME=docker` before running install or any container-touching hook:

```bash
export VCT_CONTAINER_RUNTIME=docker
python install.py --update          # install / update flows
.claude/hooks/ensure-containers.sh  # session-start hook
```

Persist the override by adding the export to your shell rc (`~/.bashrc` / `~/.zshrc`) or to the per-project `.claude/env` so every Claude Code session inherits it. The value wins over auto-probe and over any caller-passed `runtime` argument. Symmetric override: `VCT_CONTAINER_RUNTIME=podman` forces Podman when auto-probe would have picked Docker (unusual but possible if `podman` is installed but not first in `PATH`).

## MCP Servers

MCP servers are registered in the user's `~/.claude.json`. Each launches via the project venv (`claude_mcp_servers/.venv`).

**weaviate-kg** — semantic search + code graph.
- Command: `claude_mcp_servers/.venv/bin/python claude_mcp_servers/weaviate_mcp/server.py`
- Env: `WEAVIATE_URL`, `OLLAMA_URL`, `EMBEDDING_MODEL`, `KG_COLLECTION`, `SHARED_KG_COLLECTION`, `DEVELOPMENT_COLLECTION`, `GRPC_PORT`, `SHARED_KG_WRITE_DISABLED` (write gate; legacy alias `SHARED_KG_OPT_OUT` kept for ~3 releases), plus the EmbeddingService vars (`ACTIVE_EMBEDDING`, `OPENAI_API_KEY`, `CODE_EMBED_SERVICE_URL`, etc.).

**search** — academic paper search via OpenAlex and arXiv (narrowed to `search_papers` only in v0.2.11; `web_search`, `search_code`, and `fetch_page` removed as redundant with Claude's built-in WebFetch and web capabilities).
- Command (Unix): `claude_mcp_servers/search_mcp/wrapper.sh` — exports `GITHUB_TOKEN` from the keychain (env-first then resolver), then `exec`s the real server.
- Command (Windows): `claude_mcp_servers/.venv/Scripts/python.exe claude_mcp_servers/search_mcp/server.py` (no wrapper; PowerShell resolver client handles the secret).
- Env: `OPENALEX_EMAIL` (optional, gives polite-pool priority on OpenAlex API); `GITHUB_TOKEN` (resolved at wrapper startup from the `github_pat` shared keychain slot).
- Tools: `search_papers` only.

**coordination** — local KG-backed coordination notes (decisions, tasks, patterns).
- Command: `claude_mcp_servers/.venv/bin/python claude_mcp_servers/coordination_mcp/server.py`
- Env: `KG_BASE_DIR` (optional, defaults to project root).
- Tools: `post_coordination_note`, `read_coordination_notes`.

**Removed in v0.2.11**: the **ollama** MCP (`chat`, `read_document`, `read_image`) was removed as redundant. Claude's native reasoning, `Read` tool, and built-in vision handle the same use cases at higher quality. Ollama continues running as infrastructure for Weaviate text embeddings and the code-embedding service CPU fallback. The **SearXNG** container was also removed from the default stack — `search_papers` calls OpenAlex and arXiv directly without a local search proxy. See `knowledge/concepts/mcp-simplification-v0211.md` for the full rationale.

**Stale MCP cleanup**: `install.py --rewrite-stale-mcps` (added in v0.2.12 / PR-33) detects deprecated MCP entries left over from older versions in `~/.claude.json` and offers consent-prompted auto-rewrite. Run after upgrading from pre-v0.2.11 installs.

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
| `VCT_RL_PULL_TOKEN_ENDPOINT=<url>` | (v0.2.45 V45-D) Runtime override for the RL module's paid-module pull-token gateway URL. Short-circuits the L0 catalog / L1 manifest / hardcoded-default resolution chain inside `installer_engine::request_pull_token` and POSTs the license-key request to `<url>` verbatim. Use when the on-disk endpoint is wrong (manifest still carries a `placeholder.<tld>` URL, tenant has migrated, gateway is being staged behind a custom domain). Empty / whitespace-only values are ignored. Per-module-id generalization (`VCT_<MODULE_ID>_PULL_TOKEN_ENDPOINT`) is on the v0.2.46-46-2 backlog; the V45-D shape is intentionally module-id-flavoured so that the upgrade is backwards-compatible. |

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

## Vision and image analysis (v0.2.11+)

The `read_image` Ollama MCP tool was removed in v0.2.11. For image analysis, pass the image file path to Claude using the native `Read` tool — Claude's built-in vision handles it directly. This approach is simpler, requires no local GPU, and works on any hardware.

For historical reference on the memory-aware gating that `read_image` implemented, see `knowledge/concepts/read-image-memory-aware-gating.md` (marked deprecated).

## Sharing knowledge across projects

By default, every project queries both its per-project KG and a shared cross-project collection (`VibeCodedOrchestrator_KnowledgeGraph` since v0.2.12 — renamed from `VibeCodedTools_KnowledgeGraph`). Knowledge nodes captured in one project are visible to all others without re-explaining context.

Three control points:

- **`SHARED_KG_COLLECTION`** — name of the shared collection. Default `VibeCodedOrchestrator_KnowledgeGraph`. Override to point at a private team-shared collection (`AcmeTeam_SharedKG`) without exposing it via the public bundled name. The launcher's Identity tab ships a "Manage shared KG collection" picker (PR-26 Group E, v0.2.12) that surfaces every orchestrator-shaped class on your Weaviate and lets you pick which one is canonical — useful when migrating from a pre-v0.2.12 install with data still under `VibeCodedTools_KnowledgeGraph`.
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
into any venv. Until v0.2.34 the same surface shipped as
`scripts/vco{,.ps1}` shim wrappers; those were removed once packaging
landed. Fall back to `python -m vco_lib.cli <subcommand>` if for any
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
