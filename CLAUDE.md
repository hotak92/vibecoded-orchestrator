<!-- vco-deferral-reminder-begin -->
**Pending VCO action**: `.claude/context/UPDATE_DEFERRED.md` exists.
Read it at session start — it contains commands to resolve
unresolved VCO install actions.

To remove THIS reminder block: once the deferral is resolved (e.g.
via `--update --force`), VCO's next install run will delete
UPDATE_DEFERRED.md AND strip this block. Manual cleanup if needed:
delete everything between the HTML-comment markers wrapping this
block.
<!-- vco-deferral-reminder-end -->

<!-- BEGIN: SETUP-ONLY (project-scoping nudge — remove after the user has either declined or completed the scoping pass) -->
## FIRST SESSION — wait for the user to define the project, then offer to scope agents/skills

This block appears on every new project until you remove it. It's NOT for you to act on alone — it's a reminder to do ONE thing the first time the user starts a substantive conversation:

1. **Wait until the user has explained what this project is about** (one or two paragraphs of intent, stack, goals). Do NOT ask before they've spoken — they may open Claude for an unrelated reason on this session.

2. **Once you understand the project**, offer the scoping choice. Use AskUserQuestion (or plain prose if AskUserQuestion isn't available). Suggested phrasing:

   > VCO ships with 45 agents, 52 skills, and 27 hooks pre-registered for this project. Most of them are off-topic for what you've described. Want me to scope this project — disable the agents/skills that don't apply to your stack, leaving just the relevant ones? Pros: cleaner Agents/Skills tabs, less noise in `@agent-*` autocomplete, faster mental model of what's available. Cons: if the project's scope expands later, you'll have to re-enable items manually (one click each from the launcher's per-project Agents/Skills tabs). If you'd rather keep everything visible and triage on demand, that's also a valid choice — just say "keep all" and I'll skip this.

3. **If the user says yes**, do it via the launcher's enabled-toggle APIs (NOT by deleting files):
   - Tauri commands `set_project_agent_enabled` / `set_project_skill_enabled` / `set_project_hook_enabled` (callable from the launcher GUI's per-project tabs).
   - SQL fallback if you have direct DB access: `UPDATE project_agents SET enabled = 0 WHERE project_id = ? AND agent_name IN (...)` (same for `project_skills` / `project_hooks`).
   - WHY not delete the `.md` files: `install-bundle --update` is idempotent UPSERT, so disabled rows survive bundle updates. Deleting the files would cause the next bundle update to re-add them (and the user has to re-disable). The enabled-flag pattern is the supported one.

4. **Decide which to disable** by reading: the user's project description, `.claude/CONTEXT_STATE.md` (if present), the agent/skill `description:` frontmatter (one-line summary of what each does), and your judgement. Bias toward keeping a small core (planner, coder, tester, doc-extractor, kg-navigator) + everything that maps to the user's stack/goal. When unsure about a borderline item, default to ENABLED — the user can always disable later from the launcher.

5. **After the scoping pass (or if the user declines), remove this block** so it doesn't nag every future session:
   ```bash
   python .claude/scripts/cleanup-setup-sections.py
   ```
   The script strips ALL `BEGIN: SETUP-ONLY` blocks at once — fine in practice because by the time the user has done the scoping pass, the other two SETUP-ONLY blocks ("First-Run Setup" and "Verifying Installation") have also served their purpose.

**Why this block exists**: every project that gets VCO installed inherits the full agent/skill catalog, but a project's actual domain rarely needs all 45+52 of them. Disabling the off-topic ones at project-start time is a 30-second improvement to every future session's mental model — and the user almost always forgets to do it unless prompted explicitly. The nudge here makes it a deliberate first-session choice rather than a deferred housekeeping task that never happens.
<!-- END: SETUP-ONLY -->

# VibeCoded Orchestrator — Project Instructions

These instructions are loaded by Claude Code whenever you open a project that has the orchestrator installed. They tell Claude how to use the Knowledge Graph, Code Graph, hooks, and MCP servers shipped with this repo.

The orchestrator is licensed **AGPL-3.0**. If you modify any orchestrator-shipped source file (under `claude_mcp_servers/`, `.claude/scripts/`, `.claude/hooks/`, or the top-level packages of the orchestrator clone itself), keep/inherit the AGPL header — don't strip it when refactoring.

---

## Good behaviour — rules for working in a VCO-installed project

These rules apply to YOU (Claude) working inside any project that has VCO installed. They're independent of the project's own domain — keep them in mind regardless of whether you're coding a Rust web app, training an ML model, or writing documentation.

1. **Don't auto-destroy user data.** Weaviate collections, KG embeddings, code-graph embeddings, the launcher SQLite DB, and `.claude/state/` content represent hours of work and on-disk vectors that are expensive to regenerate. If your task seems to require dropping a collection, deleting `.claude/state/`, or wiping the launcher DB — STOP and ask the user explicitly. The orchestrator's own update flow uses a deferral pattern (`UPDATE_DEFERRED.md`) precisely so destructive operations are never auto-applied; follow the same discipline in any new code.

2. **Don't delete `.claude/{agents,skills,hooks}/*.md` to "uninstall" them.** Those are bundled by the orchestrator and `install-bundle --update` will re-add them. Instead, disable via the launcher GUI (per-project Agents/Skills/Hooks tabs → toggle off) or via the `set_project_{agent,skill,hook}_enabled` Tauri commands. Disabled rows survive bundle updates.

3. **Don't bypass hooks or MCPs without telling the user.** If a hook fails or an MCP times out, surface the issue and let the user decide. Don't silently `export VCT_DISABLE_HOOKS=1` or strip a misbehaving server from `~/.claude.json` — those defaults were chosen for a reason, and silent overrides leak into other sessions.

4. **Don't write to `~/.claude.json` directly.** That file is the user's global Claude Code config. The orchestrator's install + update flows manage MCP registrations there; manual edits create drift. If you need to add an MCP, do it via `install.py` (orchestrator-root) or `install-bundle --update` (per-project), not by hand-editing the JSON.

5. **Respect the orchestrator vs project boundary.** `<orchestrator-root>/.claude/` is the orchestrator clone's own state (bundled agents, hooks, KG). `<your-project>/.claude/` is YOUR project's state. Don't write project-specific knowledge into the orchestrator clone, and don't write orchestrator-shipped content into the project (it'll get overwritten on the next bundle update unless the file's hash matches the user-edited registration in `.vco-manifest.json`).

6. **Knowledge Graph writes should match scope.** Project-specific patterns → per-project KG (default `scope="project"`). Cross-project patterns that other projects on the same machine would benefit from → shared KG (`scope="shared"`). Don't pollute the shared KG with project-internal trivia; don't isolate genuinely reusable knowledge to a single project.

7. **When in doubt about an update flow, ask.** "Should I run `install.py --update`?" or "Should I drop the Weaviate collection to re-embed with the new model?" are reasonable questions to ask the user before acting — those operations take time and a wrong call costs the user more than the question.

---

## SESSION START (always)

**Before answering any non-trivial question:**
1. Read `.claude/CONTEXT_STATE.md` — current task, recent progress, blockers.
2. For architecture/system questions → check `docs/features/` (per-area design notes: MCPs + agents, code graph, KG, RL retrieval, install flow, etc.) if the project has one.
3. For any other project question → search the KG / code graph **before** generating a response.

**Never explain project state from memory.** If you don't know something with certainty, look it up first.

---

## CRITICAL: KG/Context/Memory/Plans are LOAD-BEARING

These four persistence layers exist for a reason — future-you, future-agents, and the user rely on them. Treat them like commits to a codebase, not optional notes:

- **Knowledge Graph** (`knowledge/**/*.md`): cross-project patterns, concepts, decisions. Write a node BEFORE moving on after learning a non-obvious fact (architecture, gotcha, decision rationale, post-incident insight). The `kg-update-nudge` hook fires after ~175k work units accumulated without a KG write (then every 50k work units after, escalating) — treat that nudge as a hard prompt, not optional. *Work units = output tokens + intake from Read/Web/Agent/Bash + file edits authored.* **Three legitimate ways to silence it**: (a) **write a real KG node** — `Write`/`Edit` to `knowledge/**/*.md` or call `store_knowledge_node` (the default and what the nudge wants); (b) **transcript escape marker** `[No KG update needed: <one-line reason naming what you searched for>]` as top-level text in your reply (NOT inside a tool call) — only valid after running `hybrid_search` for 2-5 candidate lessons and finding nothing; the reason must name the topic(s) you searched (bare reasons like "nothing new" are rejected); (c) **env var `KG_NUDGE_OFF=1`** — disables the hook for that shell. Use (a) by default, (b) when work was genuinely orthogonal (deploys, status reports, scrub-only), (c) only during release operations where injection would be noise.
- **CONTEXT_STATE.md**: current task, recent progress, blockers. Update DURING work, not at end. Stale state = future-you starts confused.
- **MEMORY.md** (`~/.claude/projects/.../memory/`): user preferences, recurring fixes, stable facts. Save corrections AND validations (don't just record what you got wrong).
- **Plans** (`.claude/context/plans/`): non-trivial work breakdowns with rationale. Active plans live here; archived plans in `archive/`. Update when scope changes.

**Concrete rule**: if you've done >2 hours of substantive work or learned a non-obvious thing, write the KG node BEFORE the user has to ask. KG updates need the same discipline as context and plan updates — context/plans tend to get auto-updated, KG often does not. The hook backstops you, but the goal is to never need its nudge.

**Per-chat counter**: the hook keys state by `session_id` in `~/.claude/metrics/kg_update_tokens.jsonl`. Tokens from one Claude Code session do NOT leak into another — each conversation has its own counter.

---

## Context Management

**Two-Layer Memory**:
- `MEMORY.md` (`~/.claude/projects/.../memory/`) — Claude's self-written operational notes. First 200 lines auto-loaded each session (hard cap; silent truncation after). Stable patterns, recurring bug fixes, key paths, user preferences. NOT in git. Keep as concise index; put details in topic files (`debugging.md`, etc.). Edit via `/memory` or ask Claude directly ("remember that...").
- `.claude/CONTEXT_STATE.md` — Active working memory (250–350 lines, max 500). Current task, recent progress, next steps, active blockers. Update during work, not just at end.

| MEMORY.md | CONTEXT_STATE.md |
|---|---|
| "use pnpm not npm", solved recurring bugs, stable architecture facts, service URLs | Current sprint goal, recent progress ✅, next steps, open blockers |

**Other Context**:
- `.claude/context/archive/` — Completed tasks
- `.claude/context/plans/` — Active plans (referenced, not auto-loaded)

**Token Efficiency**:
- Parallel tool calls: Read/Grep multiple files in a single message.
- Read files directly for files <150 lines; use `offset`/`limit` for larger ones.
- Cache mentally for 20–30 minutes — don't re-read the same file repeatedly within a session.
- Limit shell output: `| head -30` or `2>&1 | tail -20`.
- Skip echoing after writes; trust the tool result.
- Spawn agents for multi-file ops (cap at 3 parallel).

**Large Documents (>20 pages)**: Skim ToC/structure first, then targeted section reads; note discoveries as you go.

---

## CRITICAL: KG-First Search Policy

**Before searching OR reasoning about project topics, choose the right tool:**

1. Conceptual question → `hybrid_search` (Weaviate MCP) — searches KG + docs together
2. Relational ("what links to what") → `semantic_graph_search` (Weaviate MCP)
3. Code by purpose → `search_code_graph` (Weaviate MCP)
4. Architecture / callers / deps → `query_code_structure` (Weaviate MCP)
5. Known exact term/tag/title → `kg-search` CLI (~100 ms)
6. Quick analysis or rewrite → use Claude directly
7. Still unsure → `hybrid_search` (most comprehensive)

**This applies to reasoning too** — do NOT explain what the codebase does, what patterns exist, or what was previously decided from memory alone. Look it up first.

**Only use Grep/Read when**:
- User provides exact file path
- Searching for literal strings (variable names, error messages)
- File already in context
- Need line-by-line detail

**Use the code graph to explore the codebase, not file reads:**
- "How does X work?" → `search_code_graph("X")` BEFORE opening files.
- "What calls function Z?" → `query_code_structure("callers", "Z")` BEFORE grep.
- "What does this module import?" → `query_code_structure("dependencies", "file.py")`.
- Only `Read` once you know which lines you need.

**Default: check sources first, reason second.**

**If you retrieve knowledge that's clearly outdated, bring it up to date immediately** — don't act on stale information and don't defer the fix. Memory entries, KG nodes, reference docs, and instruction files (CLAUDE.md, MEMORY.md, etc.) are point-in-time snapshots; they drift as the codebase evolves. When you spot drift (a memory cites a guard hook that doesn't exist, a KG node names a project that's been renamed, a CLAUDE.md instruction references a subsystem that's been replaced), fix the source-of-truth document in the same turn you discovered the drift, before continuing the original task. The cost of a 60-second fix is far less than the cost of every future agent re-deriving the same outdated conclusion.

---

## Search Systems

**1. kg-search / kg-info (Keyword/Metadata)** — Fast (~100 ms):
- Known exact terms, tags, node titles
- `.claude/scripts/kg-search search "term" [--type TYPE] [--tags TAGS]`
- `.claude/scripts/kg-info info "Node Title"`
- `.claude/scripts/kg-info connections "Node Title"`
- Use when: you know the exact term to search for.

**2. Weaviate MCP Tools (Semantic/Graph)**:
- `hybrid_search(query, limit, node_type, tags, days, detail)` — Keyword+semantic across per-project KG + shared KG + project docs, auto-scoped (~1–2 s). **Default search tool.**
  - `detail` (default `"auto"`) — score-driven verbosity. Five tiers (calibrated against a canonical eval set; see `knowledge/concepts/score-driven-retrieval-tiers.md`):
    - `score < 0.42` → discarded (noise)
    - `0.42..0.55` → `summary` (LLM description from sidecar, ~6 lines)
    - `0.55..0.65` → `single_chunk` (matched chunk, ~2000 chars)
    - `0.65..0.75` → `three_chunks` (matched + neighbours, 3 chunks)
    - `>= 0.75` → `full` (whole node, up to 7 nearest chunks)
    Auto-mode varies tier per result based on score → most relevant nodes get richer detail, marginal nodes only get a summary. Token savings ~50% vs uniform `full`.
    Explicit overrides (`titles`, `summary`, `single_chunk`, `three_chunks`, `full`) apply uniformly to every result. Tier thresholds are tunable via `KG_TIER_MIN` / `KG_TIER_SINGLE_CHUNK` / `KG_TIER_THREE_CHUNKS` / `KG_TIER_FULL` env vars.
  - Each result carries `score` (0..1) and `tier` (the verbosity actually applied).
- `semantic_graph_search(query, depth, detail)` — GraphRAG with WikiLink traversal (~1–2 s). Same `detail="auto"` tiering on primary results; connected nodes always render at `summary` tier (graph topology, not score, drove their selection).
- `store_knowledge_node(..., scope)` — Write node; `scope="project"` (default) or `scope="shared"` (writes into the shared `VibeCodedOrchestrator_KnowledgeGraph` collection — visible to every other project).
- Use when: conceptual queries, discovering patterns, comprehensive research.

**3. Code Graph (Semantic Code Search)** — Weaviate MCP:
- `search_code_graph(query, scope, limit, expand_hops, detail)` — Find code by purpose/concept (~200–500 ms).
  - `scope`: `"all"` (default), `"code"` (functions/classes/modules), `"interaction"` (APIs/cross-service calls).
  - `expand_hops`: 0 (default) | 1 | 2 — follow call/interaction edges after seed retrieval.
  - `detail` (default `"auto"`): `auto` gives top-4 full details + rest as metadata refs; `titles` returns metadata refs for all; `full` returns full details for all.
- `query_code_structure(query_type, target, project)` — Structural queries (~50–100 ms).
  - Types: `dependencies` | `imports` | `callers` | `methods` | `extends` | `interactions` | `path` | `composes` | `composed_by` | `type_users`.
  - `path`: target format `"source.func->dest.func"` (BFS up to depth 6).
  - `composes` / `composed_by`: composition relationships between classes.
  - `type_users`: functions using a given type in annotations.
- CLI: `.claude/scripts/code-graph-query search "auth middleware"`.
- Use when: finding code entities, understanding architecture, cross-service call mapping.

**Decision Tree**:
- Known exact terms → `kg-search`
- Conceptual search → `hybrid_search` (default — searches KG + docs automatically)
- Relationships/graph → `semantic_graph_search`
- Code by purpose → `search_code_graph`
- Architecture queries → `query_code_structure`
- Quick analysis → use Claude directly
- Summarize/extract from file → `Read` tool with `offset`/`limit`, or native reasoning
- Read an image / vision task → `Read` tool on image path (Claude's built-in vision)
- Academic research → `search_papers` (Search MCP)
- Literal strings → Grep
- File content → Read

---

## Knowledge Graph

**Format** (Obsidian-style `.md` with YAML frontmatter):

```yaml
---
title: Node Title
type: concept  # project, concept, tool, research, model, hardware
tags: [tag1, tag2]
created: 2026-01-15T10:30:00Z
updated: 2026-01-28T14:22:00Z
valid_from: 2026-01-15T00:00:00Z
valid_until: null
status: active  # active, archived, deprecated, idea
---
```

**Typed WikiLinks** — `[[relationshipType::Target]]`:
- `[[uses::Tool]]` — uses a tool/technology
- `[[implements::Concept]]` — implements a pattern
- `[[extends::Parent]]` — extends/specializes
- `[[buildsOn::Work]]` — builds upon
- `[[relatedTo::Node]]` — general (default)

**Tags**: `#high-level-plan`, `#mid-level-architecture`, `#low-level-implementation`, `#AI`, `#python`, `#idea`, `#implemented`, etc. See `knowledge/TAG_HIERARCHY.md`.

**Node Guidelines**:
- High-level: broad overviews (<300 lines)
- Mid-level: specific domains (<200 lines)
- Low-level: individual tools/models (<150 lines)
- One node per tool/model/concept
- Links unidirectional (projects → concepts)

**Per-project isolation**: `KG_COLLECTION` (per-project KG) and `SHARED_KG_COLLECTION` (cross-project shared, default `VibeCodedOrchestrator_KnowledgeGraph`) are set per-project via `.claude/settings.json` `env` — **the canonical channel** that propagates to MCP subprocesses on every Claude Code surface (CLI, Desktop app, VS Code extension). The active workspace determines the active KG, not which project is being discussed. The launcher's Identity tab "Manage shared KG collection" picker lets you designate an existing orchestrator-shaped class as canonical (useful when migrating from an older install whose shared collection had a different name).

> ⚠️ **`.vscode/settings.json claude-code.env` does NOT propagate to MCP subprocesses on Linux** (sentinel testing 2026-05-16 against Claude Code 2.1.143; see PR-27 / v0.2.12 commit). Editing the VS Code key for KG / code-graph routing is a common footgun — values look correct in the workspace settings but the MCP subprocess sees nothing and falls back to bundled defaults. Always use `.claude/settings.json env` for KG / code-graph / embedding env vars; the launcher's per-project Identity tab writes this file. Verify with the MCP startup log line `weaviate-kg: resolved collections (...)` which shows what the subprocess actually picked up + the resolution source (env / hub / default). When a fallback to defaults happens, the MCP also emits a WARNING pointing at the same canonical channel.

**Precedence order** (highest to lowest, where the resolved value reaches MCP subprocesses):
1. vct-hub-resolved values (when the launcher is running; the MCP queries `vct-hub` on import and the hub returns the per-project ProjectConfig from `launcher.db`).
2. Env vars from `.claude/settings.json env` (cross-editor canonical).
3. Env vars from `.claude/env` (shell-sourced — CLI users who source it from their rc).
4. Env vars from `~/.claude.json mcpServers.weaviate-kg.env` (the launcher intentionally restricts this to truly machine-invariant keys like `WEAVIATE_URL`; `KG_COLLECTION`-shape per-project keys are dropped from this surface, see `launcher/src-tauri/src/mcp_registration.rs::ALLOWED_ENV_KEYS`).
5. Bundled defaults baked into `claude_mcp_servers/weaviate_mcp/server.py` (last-resort fallback; logged at WARNING when reached, and explicit empty-string env values for `KG_COLLECTION` are coerced to this default rather than used literally — see v0.2.27 fix).

**Asymmetric shared-KG access**: every project ALWAYS reads the shared KG when `SHARED_KG_COLLECTION` is set — there is no per-project read opt-out (knowledge accumulates across all projects, by design). The per-project gate `SHARED_KG_WRITE_DISABLED=true` restricts only WRITES from this project to the shared collection (`store_knowledge_node(scope='shared')` returns an error rather than silently rerouting).

**Weaviate Collection** (per-project KG, name from `KG_COLLECTION`):
- Properties: `title`, `content`, `file_path`, `node_type`, `tags`, `links`, `typed_links`, `created_at`, `updated_at`, `valid_from`, `valid_until`, `status`.
- Search via embeddings, filter by type/tags.
- Graph queries for reverse links.

**KG Write Rule** — `store_knowledge_node` always writes the `.md` file (upsert: new or changed content). File path resolution priority:
1. `file_path` is absolute → written directly.
2. `file_path` is relative + `KG_BASE_DIR` set → `KG_BASE_DIR/file_path` (the VS Code extension sets this to the workspace root).
3. `file_path` is relative + `KG_BASE_DIR` unset → falls back to the inferred project root.

- `file_path` should include the `knowledge/` prefix (e.g. `knowledge/concepts/foo.md`).
- Check `file_written: true` and `absolute_path` in the response to confirm where the file landed.
- Upsert: file is skipped only if content is identical (avoids unnecessary writes).
- **Preferred workflow**: write the `.md` file directly → `PostToolUse` hook auto-syncs to Weaviate. `store_knowledge_node` is the secondary path (used by agents that can't write files directly).
- **Warning**: subagents spawned via the Agent tool may inherit a different KG collection from `~/.claude.json` than the one set for the workspace. Pass an absolute `file_path` (or set the agent's `mcpServers` env explicitly) to ensure the file lands in the correct knowledge folder.

---

## Scripts

**Knowledge Graph** (auto venv):

```bash
.claude/scripts/kg-search   search "query" [--type TYPE] [--tags TAGS] [--limit N]
.claude/scripts/kg-search   list | recent | created [--days N]
.claude/scripts/kg-info     info "Title"
.claude/scripts/kg-info     connections "Title"
.claude/scripts/kg-sync     FILE | --all
.claude/scripts/kg-duplicates [--threshold 0.95]
```

**Code Graph** (auto venv):

```bash
.claude/scripts/code-graph-analyze  /path/to/repo [--project NAME] [--incremental] [--cfg] [--pdg]
# --cfg/--pdg require joern in PATH
.claude/scripts/code-graph-query    search "auth middleware" [--collection TYPE] [--limit N]
.claude/scripts/code-graph-query    similar "module.function" [--limit N]
.claude/scripts/code-graph-query    structure dependencies|callers|methods|extends|interactions "target"
```

PowerShell variants (`*.ps1`) ship for Windows users.

**Setup utility**:
- `.claude/scripts/cleanup-setup-sections.py` — strip the SETUP-ONLY blocks below once first-run is done. Idempotent.

---

## Infrastructure Overview

### Weaviate Vector Database
- **URL**: `http://localhost:8081` (HTTP), port 50052 (gRPC)
- **Purpose**: Semantic search across knowledge, code, docs, conversations.
- **Text embeddings**: `qwen3-embedding:0.6b` via Ollama (1024-dim, Apache 2.0). Requires `num_ctx=8192`.
- **Code embeddings**: CodeSage-Large-v2 via FastAPI service at port 11440 (2048-dim, Apache 2.0). CPU fallback uses `qwen3-embedding:0.6b` via Ollama.
- **Named vectors per collection**: KG: `qwen3_embed` (+ `openai_embed` if configured); Code: `codesage_embed` (+ `openai_embed`).
- **Active search vector**: controlled by `ACTIVE_EMBEDDING` env (`qwen3` default).
- **Collections** (names depend on `PROJECT_NAME` / `KG_COLLECTION`):
  - `<KG_COLLECTION>` — per-project KG (`knowledge/`)
  - `<SHARED_KG_COLLECTION>` — cross-project shared KG (default `VibeCodedOrchestrator_KnowledgeGraph`)
  - `<DEVELOPMENT_COLLECTION>` — verbose project docs (`docs/`); auto-paired with KG by the launcher
  - `CodeModule`, `CodeClass`, `CodeFunction`, `CodeAPI`, `CodeInteraction` — code entities
- **Access**: Weaviate MCP tools (`hybrid_search`, `semantic_graph_search`, `store_knowledge_node`, `search_code_graph`, `query_code_structure`).

### Ollama Local LLM
- **URL**: `http://localhost:11435`
- **Purpose**: Local embeddings for Weaviate; code-embedding service CPU fallback. Not exposed as an MCP tool to Claude (the Ollama MCP was removed because Claude's native reasoning, `Read` tool, and built-in vision handle the same use cases at higher quality).
- **Models**:
  - `qwen3-embedding:0.6b` — text embeddings (1024 dim, primary; needs `num_ctx=8192`)
  - KG-summary generation models (fallback only — see below): `gemma4:e4b` (low-VRAM/CPU), `qwen3.5:9b` (16 GB+ VRAM). Used by `generate-kg-summary.py` only when the `claude` CLI is not on PATH.

### Code Embedding Service
- **URL**: `http://localhost:11440` (default; configurable via `CODE_EMBED_PORT`)
- **Purpose**: GPU-accelerated code embeddings via sentence-transformers.
- **Model**: CodeSage-Large-v2 (1.3B params, 2048-dim, Apache 2.0).
- **Env**: `CODE_EMBED_BACKEND` (`gpu`/`ollama`), `CODE_EMBED_MODEL`, `CODE_EMBED_DEVICE`, `CODE_EMBED_PORT`.
- **Fallback**: set `CODE_EMBED_BACKEND=ollama` to skip the GPU path entirely (useful on CPU-only machines).
- Started automatically by the orchestrator's container ensure-hook on session start.

### vct-hub
- **URL**: `http://127.0.0.1:7700` (default; configurable via `VCT_HUB_PORT`)
- **Purpose**: Detached background service that resolves per-project configuration (KG collection, codegraph prefix, embedding model, secrets) for hooks, MCPs, and scripts. Started by `install.py`'s post-install step, by the `session-start-ensure-hub.sh` Claude Code hook on every session, and by the launcher GUI. Outlives the launcher GUI (close the GUI; hooks/MCPs/scripts still reach the hub).
- **Lockfile**: `<vct_root_dir>/hub.pid` — single-instance per user. CLI: `vct-hub --start-if-not-running` / `--stop` / `--status` / `--register-boot` / `--unregister-boot` / `--boot-status` / `--foreground`.
- **Auth**: bearer token at `<vct_root_dir>/hub.token` (regenerated every startup, mode 0o600). Required on every `/api/v1/*` route except `/api/v1/health` (liveness probe — no auth).
- **Key endpoints**:
  - `GET /api/v1/projects/{id-or-slug}/config` — resolver for KG collection / codegraph prefix / embedding model / access-matrix lists.
  - `GET /api/v1/projects/{id}/env` — secrets resolver.
  - `GET /api/v1/services/status` — services snapshot.
- **Resolver clients**: `templates/scripts/vct_project_config.sh` (bash), `templates/scripts/vct_project_config.ps1` (PowerShell 7+), `vco_lib/project_config.py` (Python). All three discover hub via `$VCT_HUB_PORT` → `<vct_root_dir>/hub.port` → `7700` default; token via `$VCT_HUB_TOKEN` → `<vct_root_dir>/hub.token`.
- **Boot auto-start**: cross-OS (systemd-user / launchd / Windows Scheduled Task). Default OFF; user opts in via launcher GUI Preferences.

### MCP Servers
Located in the user's `~/.claude.json`. Each launches via the project venv (`claude_mcp_servers/.venv`):
- **weaviate-kg** — semantic search and code graph.
  - Env: `WEAVIATE_URL`, `OLLAMA_URL`, `EMBEDDING_MODEL`, `KG_COLLECTION`, `SHARED_KG_COLLECTION`, `DEVELOPMENT_COLLECTION`, `GRPC_PORT`, `SHARED_KG_WRITE_DISABLED` (write gate).
- **search** — academic paper search via OpenAlex and arXiv.
  - Env: `OPENALEX_EMAIL` (optional, gives polite-pool priority on OpenAlex API).
  - Tools: `search_papers` only.
- **coordination** — local KG-backed coordination notes (decisions, tasks, patterns).
  - Env: `KG_BASE_DIR` (optional, defaults to project root).
  - Tools: `post_coordination_note`, `read_coordination_notes`.

**Free-tier RL gate** — retrieval reranking skips RL when `feature_enabled("rl_retrieval") == False`. Pro/MAO licenses unlock RL; free-tier users get plain Weaviate cosine ordering. Nothing breaks when the gate is closed — retrieval just falls back to base scores.

### Hook System
Located in `.claude/hooks/` — automated workflow actions. On Windows the bash hooks need WSL2 to fire automatically (the `.ps1` siblings are the native-Windows code path).

**SessionStart (startup)**:
- `ensure-containers.sh` — auto-start Podman/Docker containers (Weaviate, Ollama, code-embed) if not running (background).
- `session-start-ensure-hub.sh` — ensure `vct-hub` is running.
- `session-start-kg-loader.sh` — display KG resource paths.
- `context-size-check.sh` — warn if `CONTEXT_STATE.md` exceeds 200 lines.

**SessionStart (compact/resume)**:
- `compact-context-reinject.sh` — re-inject `CONTEXT_STATE.md` + recent commits + active plan + pre-compact snapshot.

**PreCompact (auto)**:
- `pre-compact-save.sh` — save git status + recent files to `.claude/context/pre-compact-snapshot.md`.

**PostCompact**:
- `post-compact.sh` — log compaction event + desktop notification (logs to `~/.claude/metrics/compactions.jsonl`).

**UserPromptSubmit**:
- `user-prompt-submit-reminder.sh` — workflow reminders.
- `diff-context-inject.sh` — section-level diffs of `CONTEXT_STATE.md` (70–90% token savings after the first prompt).

**PreToolUse**:
- `pre-tool-use.sh` — log tool usage to `.claude/logs/YYYY-MM-DD_tool_usage.jsonl` (matcher: `*`).
- `pre-edit-context-inject.sh` — KG + code-graph search before `Edit` (~2.7 s live, ~31 ms cached) (matcher: `Edit(*)`).

**PostToolUse**:
- `post-file-edit.sh` — auto-sync on file edits: `knowledge/` → KG, `docs/` → development collection, code files → code-graph queue; duplicate detection every 10 edits (matcher: `Edit(*)|Write(*)`).
- `py_compile` — syntax check on Python writes (matcher: `Write(*.py)`).
- `ruff` — `ruff check --fix --quiet` auto-fix (background) (matcher: `Edit(*.py)|Write(*.py)`).
- `pyright` — type checking (background, non-blocking) (matcher: `Edit(*.py)|Write(*.py)`).
- KG auto-sync — sync knowledge nodes to Weaviate on edit (matcher: `Edit(knowledge/**/*.md)|Write(knowledge/**/*.md)`).
- document processing — process uploaded documents (matcher: `Write(documents/**/*.md)|Write(documents/**/*.pdf)`).
- `post-tool-security.sh` — credential scan on written files (matcher: `Edit(*)|Write(*)`).

**ConfigChange**:
- `config-change-audit.sh` — log `settings.json` changes to JSONL (background).

**Stop**:
- `cost-tracker.sh` — append `{timestamp, session_id, model, input_tokens, output_tokens, cache_read_tokens, cost_usd}` to `~/.claude/metrics/costs.jsonl`. Summary: `.claude/scripts/cost-summary`.
- `notify-stop.sh` — desktop notification (`notify-send`).

**StopFailure**:
- `stop-failure-notify.sh` — urgent notification + failure logging to `~/.claude/metrics/failures.jsonl`.

**SessionEnd**:
- `session-end.sh` — cleanup, final sync.

**All available hook events** (as of Claude Code v2.1.81):

| Event | Can Block? | Notes |
|---|---|---|
| `SessionStart` | No | Matchers: `startup`, `compact`, `resume` |
| `UserPromptSubmit` | Yes | |
| `PreToolUse` | Yes | Matcher = tool name pattern |
| `PermissionRequest` | Yes | |
| `PostToolUse` | No | Matcher = tool name pattern |
| `PostToolUseFailure` | No | |
| `InstructionsLoaded` | No | Fires when CLAUDE.md/agents/skills loaded |
| `SubagentStart` | No | |
| `SubagentStop` | Yes | Exit 2 prevents stop |
| `Notification` | No | |
| `Stop` | Yes | |
| `StopFailure` | No | |
| `PreCompact` | No | |
| `PostCompact` | No | |
| `SessionEnd` | No | |
| `ConfigChange` | Yes | Except policy_settings |
| `WorktreeCreate` | Yes | Must print absolute worktree path on stdout |
| `WorktreeRemove` | No | Cleanup only |
| `Elicitation` | Yes | MCP server input forms |
| `ElicitationResult` | Yes | Auto-respond to elicitation |

---

## Storage Systems

**1. Knowledge Graph** (`knowledge/` → `KG_COLLECTION`):
- Per-project patterns, concepts, learnings.
- Concise (<300 lines per node).
- Search: `kg-search` CLI or `hybrid_search` MCP (auto-merges with shared KG + docs).
- Format: Markdown with YAML frontmatter, typed WikiLinks.
- Embedding: `qwen3-embedding:0.6b` (1024-dim) via Ollama.

**2. Shared KG** (`VibeCodedOrchestrator_KnowledgeGraph` by default):
- Cross-project patterns visible to every workspace.
- Auto-merged into `hybrid_search` results — read access is unconditional.
- Write to it explicitly with `store_knowledge_node(scope="shared", ...)`. Per-project gate `SHARED_KG_WRITE_DISABLED=true` refuses such writes with a clear error (no silent reroute to project KG).

**3. Code Graph** (Weaviate collections):
- **CodeModule** — files with imports and metrics (`path`, `language`, `module_summary`, `loc`, `complexity`).
- **CodeClass** — classes with inheritance (`name`, `full_name`, `class_body`, `methods`, `extends`, `field_types`, `composes`).
- **CodeFunction** — functions with call graphs (`name`, `full_name`, `function_body`, `signature`, `calls`, `type_uses`, `cfg_summary`*, `data_flow_vars`*).
  - *`cfg_summary` / `data_flow_vars` populated only when `--cfg`/`--pdg` flags are used with Joern.*
- **CodeAPI** — API endpoints with handlers (`endpoint`, `method`, `api_description`, `handler`).
- **CodeInteraction** — cross-service calls (`interaction_type`, `protocol`, `endpoint`, `confidence`, `direction`). Query: `query_code_structure("interactions", "module.py")`.
- Search: `search_code_graph`, `query_code_structure` MCPs or `code-graph-query` CLI.
- Embedding: CodeSage-Large-v2 (2048-dim) via code embedding service (CPU fallback: `qwen3-embedding:0.6b`).
- Analysis: `.claude/scripts/code-graph-analyze . --project "ProjectName" [--cfg] [--pdg]`.

**4. Development Collection** (`docs/` → `DEVELOPMENT_COLLECTION`):
- Verbose project-specific docs.
- Auto-syncs via `post-file-edit` hook.
- Search: `hybrid_search` (auto-scoped).
- Embedding: `qwen3-embedding:0.6b` (1024-dim) via Ollama.

**Decision Tree**:
- Reusable cross-project pattern → shared KG (`scope="shared"`).
- Project-specific pattern → per-project KG.
- Code entities → code graph.
- Verbose docs → development collection.
- Quick local analysis → use Claude directly.

---

## Voice + Communication

**Professional Objectivity**:
- Prioritize technical accuracy over validation.
- Challenge incorrect assumptions with evidence.
- Pattern: Challenge → Evidence → Alternative → wait for decision.

**Anti-Sycophancy Rules**:
- Check actual evidence before claiming success (don't assume).
- Avoid superlatives: "Great!", "Perfect!", "Beautifully!", "Amazing!"
- State facts objectively: "X launched", not "X working perfectly".
- When uncertain, say so: "We'll know when..." not "This will work".
- Don't validate feelings: "That works because..." not "Great idea!"
- Challenge politely: "That approach has issues because..." with evidence.

**Specification Adherence**:
- Follow specs exactly — don't skip steps or simplify without permission.
- Never use placeholders: `... rest unchanged`, `// existing code`, `<!-- rest of HTML -->`.
- Implement general solutions for ALL inputs, not just test cases.
- Don't hard-code values or make assumptions to finish faster.
- Priority hierarchy: real-world functionality per spec > tests passing > speed.
- Good simplification (remove complexity, keep behavior) — encouraged.
- Lazy shortcuts (skip features, drop edge cases, use workarounds) — forbidden.
- If task unclear, ask questions — don't guess and implement the wrong thing.

**When to Ask vs Decide**:
- Ask: architecture choices, tech selection, breaking changes, multiple plausible approaches.
- Decide autonomously: bug fixes, optimizations, refactors, docs.
- **Ambiguous requirements**: if you find yourself reasoning through multiple interpretations in your reply, STOP and ask instead. Do not speculate out loud.

---

## Workflow

**Knowledge Management**:
- Proactively capture: project details, architecture, decisions, preferences, learnings.
- Create nodes in the appropriate `knowledge/` subfolder; use typed WikiLinks for implementation relationships.
- Sync via the `PostToolUse` hook or `.claude/scripts/kg-sync`.

**Update `CLAUDE.md` when**:
- New directories, tech-stack changes, new scripts/tools, new patterns, new KG conventions.

**Update `CONTEXT_STATE.md`**:
- During work (not just at end).
- Mark completed subtasks (`✅`).
- Add discoveries, new nodes, blockers.

**Compaction**: critical context is preserved by `PreCompact` / `PostCompact` hooks. Before compacting manually, update `CONTEXT_STATE.md` with current state, modified files, open blockers. Use `/compact focus on <topic>` to guide the summary.

---

## Agents & Skills

The installer drops a default set of agents into `.claude/agents/` and skills into `.claude/skills/`. Customize freely — they are templates, not framework code.

**Invoke skills**: `/skill-name` (e.g. `/architect`, `/tdd`).
**Spawn agents**: `@agent-name (Model)` via the Agent tool. (`Task` is a legacy alias for `Agent`.)

**Model Selection**:
- **Opus**: complex tradeoffs, security review, deep debugging — sparingly.
- **Sonnet**: implementation, planning, guidance — default.
- **Haiku**: simple tasks, calculations, tests — freely.

**When to Spawn**:
- Task parallel to current work.
- Requires sustained focus (30+ min).
- Isolated context.
- Substantial output (>200 lines).

**Don't Spawn**:
- <5 min tasks.
- Needs immediate back-and-forth.
- Exploring/brainstorming.

**Handoff Format** (300–500 tokens):

```
@agent-name (Model)
Task: One sentence goal
Context: File paths, patterns, constraints
Success Criteria: What "done" looks like
Output: Where to save
```

**Parallel agents on the same git repo**: when spawning 2+ agents that will edit the same working tree, give each one an isolated git worktree (Agent-tool `isolation: "worktree"`, or instruct the agent in its prompt to `git worktree add` and work there). Otherwise concurrent `git checkout` / staging operations clobber each other and you can lose work. Single-agent work doesn't need this. Cap at 3 parallel agents to avoid context overflow on return.

**Skill Frontmatter** (in `.claude/skills/NAME/SKILL.md`):
- VS Code validates: `name`, `description`, `argument-hint`, `disable-model-invocation`, `user-invocable`, `compatibility`, `license`, `metadata`.
- CLI/runtime also accepts (VS Code warns but they work): `model`, `effort`, `allowed-tools`, `context: fork`, `agent`, `hooks`.
- `model` — `sonnet` | `opus` | `haiku` | full model ID | `inherit`.
- `disable-model-invocation: true` — only the user can invoke via `/command`.
- `user-invocable: false` — hide from the `/` menu, only Claude auto-invokes.
- `context: fork` — run in an isolated subagent context (CLI only).

**Agent Frontmatter** (in `.claude/agents/NAME.md`):
- `name`, `description` — required.
- `model` — `sonnet` | `opus` | `haiku` | `inherit` (default) | full model ID.
- `tools` / `disallowedTools` — allow/deny tool lists.
- `permissionMode` — `default` | `acceptEdits` | `dontAsk` | `bypassPermissions` | `plan`.
- `maxTurns` — max agentic turns before stopping.
- `effort` — `low` | `medium` | `high` | `xhigh` | `max` — caps per-agent reasoning cost. Frontmatter wins over session level. Cannot be set as Agent-tool parameter — must be in `.md` frontmatter. The env var `CLAUDE_CODE_EFFORT_LEVEL` overrides frontmatter globally. When spawning ad-hoc agents (Agent tool with a description but no pre-existing definition file), tell them in the prompt to operate at `high` effort by default for substantive work — implementation, refactors, multi-file changes, design + code. Reserve `xhigh` / `max` for genuinely deep-reasoning tasks where `high` has empirically fallen short: gnarly architecture spec across many subsystems, security review of unfamiliar code, hard debugging that already proved resistant to standard effort. Trivial sweeps stay at default.
- `isolation: worktree` — runs in a temporary git worktree (auto-cleaned if no changes).
- `background: true` — always run as background task.
- `memory` — `user` | `project` | `local` — enables persistent agent memory directory.
- `skills` — skills injected into subagent context (not inherited from parent).
- `mcpServers` — MCP servers scoped to this subagent only.
- `hooks` — lifecycle hooks scoped to this subagent.

---

## Context Efficiency

**File Operations**: check before reading (Grep first), use `offset`/`limit` for large files, trust writes (no re-reads), spawn agents for multi-file ops (cap at 3 parallel).

**Lean-ctx Bash compression**: when `lean-ctx` is on PATH, every `Bash(<cmd>)` tool call goes through the per-project PreToolUse hook `.claude/hooks/lean-ctx-rewrite.sh` (Windows: `lean-ctx-rewrite.ps1`), which rewrites the command to `lean-ctx -c '<cmd>'`. Result: Claude sees output compressed ~90–97% (boilerplate, progress bars, redundant lines stripped) without losing behaviour. The hook is a graceful no-op when `lean-ctx` isn't installed.

**Three-tier bypass hierarchy** — pick the right one for what you want:

| Scope | Mechanism | When to use |
|---|---|---|
| Per-call (granular) | `lean-ctx bypass "<cmd>"` or `lean-ctx -c --raw "<cmd>"` | See raw output for ONE command without disabling other VCO hooks. The hook auto-detects commands starting with `lean-ctx` and steps aside — no double-wrap, no recursion. |
| Per-call inverse | `lean-ctx -c "<cmd>"` | Force-compress a single command when the per-project default is `off`. |
| Per-project (default) | Add `VCO_LEAN_CTX_DEFAULT=off` to `.claude/env` | Switch the project's default to raw output. Default `on` (compression active) when the line is missing or `.claude/env` doesn't exist. |
| Global (sledgehammer) | `export VCT_DISABLE_HOOKS=1` | Disables ALL `.claude/hooks/*.sh` for the current shell, not just lean-ctx. Use only when debugging hook interactions. |

Override symmetry is intentional: with project default `on`, `lean-ctx bypass "<cmd>"` still produces raw output. With default `off`, prefixing `lean-ctx -c "<cmd>"` still produces compressed output.

**⚠️ Known footgun — silent stderr swallowing on `git commit`**: `lean-ctx`'s default mode can swallow stderr from `git commit` to the point where a hook-failed commit returns exit code 1 with **zero output**, making the failure invisible. Symptom: `git commit` returns exit 1 but no error message; subsequent `git status` shows the file is staged but uncommitted. **Workaround**: bypass compression for that command:

```bash
# If `git -c user.name=... commit -m "..."` exits 1 silently:
lean-ctx bypass "git -c user.name=... commit -m \"...\""
```

Apply the same workaround for `git push` if it returns silent non-zero (rare, but possible with pre-push hooks). When in doubt, run via `lean-ctx bypass "..."` for any git command that exits non-zero with no output.

**Target Metrics**:
- Simple: <5K tokens
- Complex: <20K tokens
- Session: <100K tokens

---

## Tool Usage Examples (READ THIS!)

**Example 1: Implementing an authentication feature**

```python
# WRONG: skip knowledge search, use Grep immediately
Grep "def.*auth" --type py  # Only finds literal function names

# CORRECT: KG-first search policy
hybrid_search("authentication patterns for web APIs")          # Find proven patterns
search_code_graph("authentication middleware", scope="code")   # Find similar code
query_code_structure("callers", "api.auth.validate_token")     # Understand usage
search_code_graph("HTTP calls to external API", scope="interaction")  # Cross-service calls
query_code_structure("interactions", "api/routes.py")          # All outbound calls from a module
# THEN use Grep for exact strings, Read for detail
```

**Example 2: User asks about project state or architecture**

```python
# WRONG: start reasoning immediately from what you remember
# "Based on what I know, the code graph system uses 5 collections..."

# CORRECT: check CONTEXT_STATE.md + KG first
Read(".claude/CONTEXT_STATE.md")                   # What's current task / recent work?
hybrid_search("code graph collections schema")      # What does KG say about this?
# THEN answer based on what you found, not what you assumed
```

**Example 3: Quick analysis task**

```python
# CORRECT: use Claude's native capabilities
# Read a file section: Read(file_path, offset=N, limit=50)
# Analyze an image: Read(image_path)  — Claude's built-in vision
# Summarize a file: Read the relevant section, then reason over it
# No separate MCP tool needed.
```

---

## Update flow

Two distinct "update" actions live in the launcher:

- **Update orchestrator** (Settings → Updates) — refreshes the orchestrator clone itself (the global venv, bundled hooks/MCP servers, the templates folder, the launcher and hub binaries). Stops the detached `vct-hub` before the update so the binary swap isn't blocked on Windows, then restarts it and the launcher.
- **Update bundle** (per-project Settings page → "Update bundle" button) — propagates newly-shipped orchestrator files to ONE existing user project without overwriting user customizations.

The "Update bundle" path is manifest-driven via `<project>/.claude/.vco-manifest.json`:
- New shipped file → created in the project.
- Installed file matches the manifest's prior-shipped hash (= user untouched) → overwritten with the new shipped version.
- Installed file differs from the manifest's prior-shipped hash (= user-modified) → preserved on disk; a `bundle_user_modified_preserved` deferral entry is written to `<project>/.claude/context/UPDATE_DEFERRED.md` listing each preserved file + the explicit `--force` command to accept the orchestrator's defaults.
- Schema drift detected (Weaviate target schema differs from on-disk) → `schema_migration_required` deferral entry; the destructive migration is NOT auto-applied. Requires explicit consent via `python -m vco_lib.project_init migrate-collections --name <project>`.

The toast summarises the result ("5 files updated, 2 user-modifications preserved"). Soft-fail throughout: subprocess errors flow through warnings, never block.

---

## Quick Reference

- Start: read `.claude/CONTEXT_STATE.md`.
- Sync KG: `.claude/scripts/kg-sync --all`.
- Analyze code: `.claude/scripts/code-graph-analyze . --project "MyProject"`.
- Search code: `.claude/scripts/code-graph-query search "pattern"`.
- Search knowledge: `hybrid_search("concept")` (Weaviate MCP).
- Quick analysis: use Claude directly.
- Academic research: `search_papers("topic")` (Search MCP).
- Active plans: `.claude/context/plans/`.
- Tag hierarchy: `knowledge/TAG_HIERARCHY.md`.
- Score-tier reference: `knowledge/concepts/score-driven-retrieval-tiers.md`.
- Plan mode: `claude --plan` or `/plan` (read-only exploration).
- Compact with focus: `/compact focus on <topic>`.
- Fix from PR: `claude --from-pr <PR-URL>`.

**Default ports**: Weaviate `8081` (HTTP) / `50052` (gRPC), Ollama `11435` (embeddings + KG-summary generation), code-embed service `11440` (optional GPU), vct-hub `7700`.
**Default models**: text embeddings `qwen3-embedding:0.6b` (1024-dim), code embeddings CodeSage-Large-v2 (2048-dim, GPU) / `qwen3-embedding:0.6b` (CPU fallback). KG-summary generation uses a three-tier fallback in `generate-kg-summary.py`: (1) `claude` CLI on PATH (best quality, uses your Claude subscription / API key), (2) local Ollama models (`qwen3.5:9b` for 16 GB+ VRAM / `gemma4:e4b` for low-VRAM/CPU), (3) Anthropic API direct (if `ANTHROPIC_API_KEY` is set). Force a tier via `KG_SUMMARY_BACKEND=cli|ollama|api|skip`.

---

<!-- BEGIN: SETUP-ONLY (remove after first successful session) -->
## First-Run Setup

If you're seeing this orchestrator for the first time, here's the 60-second mental model:

- **What it is**: an infrastructure layer for Claude Code that adds persistent memory (KG), semantic code search (code graph), and ~27 hooks that run automatically. You keep using Claude Code the way you already do; the orchestrator works in the background via hooks and MCP servers.
- **What runs locally**: Weaviate (vector DB) on `:8081`, Ollama (local LLM + embeddings) on `:11435`. Both started automatically by the `SessionStart` hook if not already running.
- **Free vs Pro**: this repo is fully functional standalone. Optional paid modules (RL retrieval reranking, MAO multi-agent runtime, specialist agent packs) activate only when a license key is present. Without a key, retrieval falls back to cosine ordering — nothing breaks.

If install hasn't run yet (or you're not sure):

```bash
bash first-install.sh       # Linux / macOS — or double-click first-install.command (macOS) / first-install.desktop (Linux)
# Windows: double-click first-install.bat
```

Full install walk-through and options: [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md).
First-run problems (Weaviate won't start, MCP not connected, hooks not firing on Windows): [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).
Where each config file lives and why: [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).

*Once you've completed first-run setup, remove this section: `python .claude/scripts/cleanup-setup-sections.py`*
<!-- END: SETUP-ONLY -->

<!-- BEGIN: SETUP-ONLY (remove after first successful session) -->
## Verifying Installation

Run these once to confirm the install landed correctly:

```bash
claude mcp list
# Expected: weaviate-kg ✓ Connected, search ✓ Connected (Ollama runs as infrastructure, not as an MCP)

curl -s http://localhost:8081/v1/.well-known/ready    # Weaviate ready
curl -s http://localhost:11435/api/tags               # Ollama models
```

Inside a Claude Code session, run `/context` to print the active workspace path and KG collection name. If the collection is wrong (e.g. it's reusing another project's KG), you opened Claude in the wrong directory — see the per-project isolation note above and [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).

**GitHub access** (only if you push from this project): the recommended pattern is a single chmod-600 file (e.g. `~/.your-secrets/github_pat`) plus a wrapper that exports `GITHUB_TOKEN` for the search MCP and a git credential helper that reads the same file. Don't store the PAT in `~/.claude.json`. Details: [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).

*Once you've completed first-run setup, remove this section: `python .claude/scripts/cleanup-setup-sections.py`*
<!-- END: SETUP-ONLY -->

---

## Claude Code Auth

Claude Code authenticates either via `ANTHROPIC_API_KEY` or claude.ai OAuth login. Some features (`--channels`, managed agents, certain plugins) are gated on claude.ai auth and unavailable when running with an API key only. If a feature seems missing, check the active auth mode before assuming it's not implemented.
