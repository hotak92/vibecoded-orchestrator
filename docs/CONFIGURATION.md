# Configuration Philosophy

Config layout follows one rule: **minimal global, maximum per-project**.

## What this means

- **Global `~/.claude/settings.json`**: user preferences only — effort level, output tokens, universal permission denies. No project paths, no MCP server URLs, no environment variables that any specific project depends on.
- **Per-project `.vscode/settings.json`**: where `claude-code.env` lives for the **VS Code extension**. MCP env vars (Weaviate URL, collection names, embedding backend) live here so opening this project in VS Code wires up its MCP servers correctly without affecting any other project you have open.
- **Per-project `.claude/settings.json`**: per-project permissions and hook registrations, plus an `env` block read by the **Claude Code CLI and Desktop app**. The launcher writes both files in lockstep so all three surfaces (VS Code extension / CLI / Desktop) see the same MCP environment.
- **Per-project secrets**: stored in the OS keychain via the VCT Launcher GUI — not in env files, not in JSON configs. The launcher knows about per-project scoping, so an OpenAI key for one project doesn't leak into another.

## Why

It prevents cross-contamination. Global settings apply to every project you open — set `KG_COLLECTION=MyMainProjectKG` globally and every other project will silently reuse that collection and mix knowledge graphs.

## Setup for new users

1. Copy `.vscode/settings.json.example` to `.vscode/settings.json` and adjust if needed (defaults work out of the box for a local Podman+Ollama setup).
2. The VCT Launcher creates a per-project `.env` from a canonical template when you register a project (see "`.env` template management" below). For non-launcher CLI users, copy `.env.example` manually.
3. Let `install.py` wire the rest (venv, containers, KG collection creation).
4. Launch via the VCT Launcher GUI (manages secrets, tier gating, module installs).

## `.env` template management (Deliverable 1, 2026-04-28)

Both `install.py` Step 9 and the launcher's `create_project_v2` Tauri command call `ensure_project_env_template` (Python: `_ensure_env_template`; Rust: `ensure_project_env_template`) on the project root. The behaviour is:

- **`.env` missing** → write a fresh canonical template with all known keys. Active keys (`KG_COLLECTION`, `PROJECT_NAME`, `DEVELOPMENT_COLLECTION`, `CONVERSATION_COLLECTION`, `SHARED_KG_COLLECTION`) get values substituted from the project name. Optional keys (LLM API keys, `GITHUB_TOKEN`, RL module URLs, `VCT_TELEMETRY`) stay commented out.
- **`.env` exists** → diff against the canonical key list. Any keys not yet present (commented or active) get appended in a marked block tagged `# added by vco YYYY-MM-DD`. The user's existing values are preserved verbatim — never overwritten.
- **Idempotent** — a second invocation against an up-to-date file is a no-op.

The Python and Rust canonical key lists are kept in lockstep by the cross-language test `env_template_canonical_keys_match_python` (in `commands/projects_v2.rs`). When you add a new key, update both `_env_canonical_template` (install.py) AND `env_canonical_keys` (projects_v2.rs).

Canonical keys (as of 2026-04-28):

```
# Service URLs (commented; launcher writes resolved values into .claude/settings.json)
WEAVIATE_URL, WEAVIATE_PORT, OLLAMA_URL, OLLAMA_PORT, CODE_EMBED_URL

# Per-project Weaviate collections (active; filled at create time)
KG_COLLECTION, SHARED_KG_COLLECTION, DEVELOPMENT_COLLECTION,
PROJECT_NAME, CONVERSATION_COLLECTION

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
| MCP env (URLs, collection names, paths) — VS Code extension | `.vscode/settings.json` → `claude-code.env` | per-project | VS Code + launcher |
| MCP env (URLs, collection names, paths) — CLI / Desktop app | `.claude/settings.json` → `env` | per-project | launcher (kept in sync with the VS Code copy) |
| Shell/script env | `.env` | per-project | you, `.env.example` template |
| Project permissions + hooks | `.claude/settings.json` | per-project | install.py + launcher |
| Secrets (license keys, API tokens) | OS keychain | per-project | launcher GUI only |
| Hooks scripts | `.claude/hooks/` | per-project | install.py |
| Project agents | `.claude/agents/` | per-project | install.py (from `templates/agents/free/`) |
| MAO specialist agents | `.claude/agents/` | per-project | install.py `--with-mao-agents` (MAO license) |
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

The MCP server (`claude_mcp_servers/weaviate_mcp/server.py`) reads these on startup. They're written to all three per-project surfaces (VS Code `claude-code.env`, `.claude/env`, `.claude/settings.json::env`) by the launcher's `write_project_env_files`.

| Var | Default | What it does |
|---|---|---|
| `KG_COLLECTION` | `<ProjectName>` | Per-project Weaviate collection. Knowledge nodes from `knowledge/` land here. |
| `DEVELOPMENT_COLLECTION` | `<ProjectName>_development` | Per-project Weaviate collection for `docs/`. |
| `CONVERSATION_COLLECTION` | `<ProjectName>_conversations` | Reserved for future chat history; not auto-populated in v1.0. |
| `SHARED_KG_COLLECTION` | `VibeCodedTools_KnowledgeGraph` | Cross-project shared KG. All projects on this machine query it alongside their own KG. Seeded by `install.py` Step 7d from `vibecoded-orchestrator/knowledge/`. |
| `SHARED_KG_OPT_OUT` | `false` | Per-project opt-out. Set to `true` (or `1`/`yes`) to disable shared-KG queries for this project. The shared collection itself is unaffected — other projects keep using it. |
| `SHARED_KG_NODE_FORMATS` | (unset) | Override path for the shared KG's `.node_formats.json` sidecar. Used by tests; in production the sidecar is read from `<orchestrator>/knowledge/.node_formats.json` via `_SERVER_INFERRED_BASE`. |
| `KG_TIER_MIN` / `KG_TIER_SINGLE_CHUNK` / `KG_TIER_THREE_CHUNKS` / `KG_TIER_FULL` | `0.42` / `0.55` / `0.65` / `0.75` | Score thresholds for the auto-tier retrieval system. See `knowledge/concepts/score-driven-retrieval-tiers.md`. |

**Opt-out semantics**: `SHARED_KG_OPT_OUT=true` zeros `SHARED_KG_COLLECTION` for this MCP process. `hybrid_search` / `semantic_graph_search` then skip the dual-collection merge entirely. Writes via `store_knowledge_node(scope="shared")` fall back to writing to the project KG (no silent black-hole writes).

**Power-user override**: point `SHARED_KG_COLLECTION` at a private team-shared collection (e.g. `AcmeTeam_SharedKG`) to share knowledge across an internal team without exposing it via the public bundled name.

## Agents and skills

See [templates/README.md](../templates/README.md) for the tier split (free vs MAO) and install-flag reference.

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

## Vision (read_image) memory budget

The Ollama MCP's `read_image` tool returns an image as a base64 data URL (which Claude can read directly) and optionally a local text description from a vision model. The local description tier is memory-aware:

- Probes free VRAM and system RAM at module load.
- Picks GPU if VRAM is at or above the per-model threshold; otherwise tries CPU; otherwise auto-falls-back to a smaller installed model.
- If no model fits, returns the image-as-base64 with `description_skipped_reason` set. Claude still sees the image directly.

Per-model thresholds (q4_K_M; floors include KV cache and image-feature activations):

| Model | VRAM | RAM |
|---|---|---|
| qwen3.5:9b (default) | 7.5 GB | 12 GB |
| qwen3.5:7b | 6.0 GB | 10 GB |
| qwen3.5:4b | 4.0 GB | 7 GB |
| llama3.2-vision:11b | 9.0 GB | 16 GB |
| gemma3:4b / gemma4:e4b | 5.0 GB | 8 GB |

Resize budget is also tiered: `max_total_pixels` (default 1024² = 1,048,576) is clamped DOWN to 720² / 512² / 256² when free VRAM drops below 8 / 6 / 4 GB. The actual budget used is surfaced as `image_budget_clamped_from` in the response.

Override via `OLLAMA_VISION_MODEL=<model_id>` to force a specific model regardless of memory.

## Sharing knowledge across projects

By default, every project queries both its per-project KG and a shared cross-project collection (`VibeCodedTools_KnowledgeGraph`). Knowledge nodes captured in one project are visible to all others without re-explaining context.

Three control points:

- **`SHARED_KG_COLLECTION`** — name of the shared collection. Default `VibeCodedTools_KnowledgeGraph`. Override to point at a private team-shared collection (`AcmeTeam_SharedKG`) without exposing it via the public bundled name.
- **`SHARED_KG_OPT_OUT=true`** — per-project opt-out. Zeros `SHARED_KG_COLLECTION` for this project's MCP server; `hybrid_search` and `semantic_graph_search` then query only the per-project KG. The shared collection itself is unaffected — other projects keep using it.
- **`store_knowledge_node(scope="shared")`** — explicit write to the shared collection. The default scope is `"project"` so arbitrary projects don't pollute the shared collection by accident.

Install does NOT auto-adopt a foreign shared KG it finds on the host (e.g. an existing `ClaudeKnowledgeGraph` from an earlier install). Reason: the orphan-prune pass in `sync_knowledge_graph.py` deletes entries whose `file_path` no longer exists in the active project; two installs sharing one collection would silently delete each other's nodes. vco always creates `VibeCodedTools_KnowledgeGraph` fresh (or skips creation if the exact name already exists).

## KG-summary backend selection

Auto-generated 2-3 sentence summaries for every KG node, written to `knowledge/.node_formats.json` and consumed by the auto-tier retrieval system. Backends are tried in order; first one available wins:

1. `claude` CLI on PATH — best quality, requires CLI install.
2. Ollama at `http://localhost:11435` — works for any vco user since Ollama is already required for embeddings. Default model `qwen3.5:9b` (16+ GB VRAM) or `gemma4:e4b` for low-VRAM hosts.
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

