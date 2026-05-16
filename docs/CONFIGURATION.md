# Configuration Philosophy

Config layout follows one rule: **minimal global, maximum per-project**.

## What this means

- **Global `~/.claude/settings.json`**: user preferences only — effort level, output tokens, universal permission denies. No project paths, no MCP server URLs, no environment variables that any specific project depends on.
- **Per-project `.claude/settings.json`**: per-project permissions and hook registrations, plus an `env` block read by Claude Code (CLI, Desktop app, AND the VS Code extension) and propagated to MCP subprocesses. This is the **canonical per-project MCP env channel** as of v0.2.12 (PR-27, 2026-05-16): empirical sentinel testing on Linux Claude Code 2.1.143 confirmed that `.vscode/settings.json` `claude-code.env` does NOT propagate to MCP subprocesses, so the launcher no longer writes that key. See PR-27 commit message + `docs/CLAUDE_CODE_COMPATIBILITY.md` → "Per-project env files" for the full empirical trace.
- **Per-project `.claude/env`**: POSIX shell-sourceable env file with the same values, for CLI users who source it from their shell rc via the `tools/claude` wrapper.
- **Per-project `.vscode/settings.json`**: VS Code editor preferences only (Pylance excludes, file-watcher excludes, formatter settings). The launcher's Python-side `_backfill_vscode_excludes_in_project` manages the Pylance/watcher exclude block; the launcher does NOT touch any `claude-code.env` block here.
- **Per-project secrets**: stored in the OS keychain via the VCT Launcher GUI — not in env files, not in JSON configs. The launcher knows about per-project scoping, so an OpenAI key for one project doesn't leak into another.

## Why

It prevents cross-contamination. Global settings apply to every project you open — set `KG_COLLECTION=MyMainProjectKG` globally and every other project will silently reuse that collection and mix knowledge graphs.

## Setup for new users

1. Copy `.vscode/settings.json.example` to `.vscode/settings.json` and adjust as needed for editor preferences (Pylance excludes, formatter settings). The example file no longer ships a `claude-code.env` block — per-project MCP env now lives in `.claude/settings.json` `env` instead (see the v0.2.12 PR-27 note in the bullet list above).
2. The VCT Launcher creates a per-project `.env` from a canonical template when you register a project (see "`.env` template management" below). For non-launcher CLI users, copy `.env.example` manually.
3. Let `install.py` wire the rest (venv, containers, KG collection creation).
4. Launch via the VCT Launcher GUI (manages secrets, tier gating, module installs).

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

# GitHub access for code-search MCP (commented)
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
| Shell/script env | `.env` | per-project | you, `.env.example` template |
| Project permissions + hooks | `.claude/settings.json` | per-project | install.py + launcher |
| Secrets (license keys, API tokens) | OS keychain | per-project | launcher GUI only |
| Hooks scripts | `.claude/hooks/` | per-project | install.py |
| Bundled agents | `.claude/agents/` | per-project | installed by default (from `templates/agents/free/`) |
| Project skills | `.claude/skills/` | per-project | install.py (from `templates/skills/`) |
| Generic agents (e.g. `code-migrator`) | `~/.claude/agents/` | global | you, optional |
| Generic skills (e.g. `debug-expert`) | `~/.claude/skills/` | global | you, optional |

## What does NOT go in global

- MCP server definitions (they point at this project's venv + source paths)
- Plugin enable flags (`enabledPlugins`) — plugins are project-specific
- Project paths (`MCP_PYTHON`, `MCP_WEAVIATE_SERVER`, etc.)
- Collection names (`KG_COLLECTION`, etc.)
- Embedding model defaults (differ per project tier)

If you see any of these in your global `~/.claude/settings.json`, move them to the per-project config. They're leaking.

## Knowledge graph env vars

The MCP server (`claude_mcp_servers/weaviate_mcp/server.py`) reads these on startup. They're written to the two per-project surfaces — `.claude/env` (POSIX shell-sourceable) and `.claude/settings.json::env` (the canonical channel that actually propagates to MCP subprocesses on Linux) — by the launcher's `write_project_env_files`. The historical third surface (`.vscode/settings.json` `claude-code.env`) was removed in v0.2.12 (PR-27, 2026-05-16); see the bullet at the top of this file for the empirical-trace KG node.

| Var | Default | What it does |
|---|---|---|
| `KG_COLLECTION` | `<ProjectName>` | Per-project Weaviate collection. Knowledge nodes from `knowledge/` land here. |
| `DEVELOPMENT_COLLECTION` | `<ProjectName>_development` | Per-project Weaviate collection for `docs/`. Auto-paired with KG by the launcher. Same chunker + named-vector slot logic as KG. |
| `SHARED_KG_COLLECTION` | `VibeCodedTools_KnowledgeGraph` | Cross-project shared KG. All projects on this machine query it alongside their own KG. Seeded by `install.py` Step 7d from `vibecoded-orchestrator/knowledge/`. |
| `SHARED_KG_WRITE_DISABLED` | `false` | Per-project WRITE gate (asymmetric model since 2026-05-01). Set to `true` (or `1`/`yes`) to refuse `store_knowledge_node(scope="shared")` calls from this project. **Reads are unconditional** — every project always queries the shared KG when configured. |
| `SHARED_KG_OPT_OUT` | `false` | Legacy alias of `SHARED_KG_WRITE_DISABLED`. Kept for back-compat ~3 releases (target removal: 2026-08). The canonical key wins when both are set. NOTE: pre-2026-05-01 this also gated reads — that behaviour is gone. |
| `SHARED_KG_NODE_FORMATS` | (unset) | Override path for the shared KG's `.node_formats.json` sidecar. Used by tests; in production the sidecar is read from `<orchestrator>/knowledge/.node_formats.json` via `_SERVER_INFERRED_BASE`. |
| `KG_TIER_MIN` / `KG_TIER_SINGLE_CHUNK` / `KG_TIER_THREE_CHUNKS` / `KG_TIER_FULL` | `0.42` / `0.55` / `0.65` / `0.75` | Score thresholds for the auto-tier retrieval system. See `knowledge/concepts/score-driven-retrieval-tiers.md`. |

**Asymmetric semantics (since 2026-05-01)**: `SHARED_KG_WRITE_DISABLED=true` refuses `store_knowledge_node(scope="shared")` calls with a clear error (`"Shared KG writes are disabled for this project. Set SHARED_KG_WRITE_DISABLED=false to enable, or use scope='project' for the per-project KG."`) — NOT a silent reroute to the project KG. `hybrid_search` / `semantic_graph_search` continue to merge the shared collection regardless of the gate; reads are never disabled per-project. Pre-2026-05-01 the same flag also zeroed reads — that behaviour is gone, by design.

**Power-user override**: point `SHARED_KG_COLLECTION` at a private team-shared collection (e.g. `AcmeTeam_SharedKG`) to share knowledge across an internal team without exposing it via the public bundled name.

## Agents and skills

See [templates/README.md](../templates/README.md) for the bundled agents and skills (29 + 28) and install-flag reference.

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
| `VCT_DISABLE_HOOKS=1` | See section below. |

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

By default, every project queries both its per-project KG and a shared cross-project collection (`VibeCodedTools_KnowledgeGraph`). Knowledge nodes captured in one project are visible to all others without re-explaining context.

Three control points:

- **`SHARED_KG_COLLECTION`** — name of the shared collection. Default `VibeCodedTools_KnowledgeGraph`. Override to point at a private team-shared collection (`AcmeTeam_SharedKG`) without exposing it via the public bundled name.
- **`SHARED_KG_WRITE_DISABLED=true`** — per-project WRITE gate. Refuses `store_knowledge_node(scope="shared")` from this project with a clear error. Reads of the shared collection remain unconditional — knowledge accumulation across projects is the headline value prop. Legacy alias: `SHARED_KG_OPT_OUT` (kept for ~3 releases, target removal 2026-08).
- **`store_knowledge_node(scope="shared")`** — explicit write to the shared collection. The default scope is `"project"` so arbitrary projects don't pollute the shared collection by accident.

Install does NOT auto-adopt a foreign shared KG it finds on the host (e.g. an existing `ClaudeKnowledgeGraph` from an earlier install). Reason: the orphan-prune pass in `sync_knowledge_graph.py` deletes entries whose `file_path` no longer exists in the active project; two installs sharing one collection would silently delete each other's nodes. VibeCoded Orchestrator (VCO) always creates `VibeCodedTools_KnowledgeGraph` fresh (or skips creation if the exact name already exists).

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

