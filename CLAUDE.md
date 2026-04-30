# VibeCoded Orchestrator — Project Instructions

These instructions are loaded by Claude Code whenever you open a project that has the orchestrator installed. They tell Claude how to use the Knowledge Graph, Code Graph, hooks, and MCP servers shipped with this repo.

This repo is licensed **AGPL-3.0**. Any source file you create or modify under `claude_mcp_servers/`, `.claude/scripts/`, `.claude/hooks/`, or top-level packages must keep / inherit the AGPL header. Do not strip license headers when refactoring.

---

## SESSION START (always)

**Before answering any non-trivial question:**
1. Read `.claude/CONTEXT_STATE.md` — current task, recent progress, blockers.
2. For architecture/system questions → check `docs/ARCHITECTURE.md` if the project ships one.
3. For any other project question → search the KG / code graph **before** generating a response.

**Never explain project state from memory.** If you don't know something with certainty, look it up first.

---

## CRITICAL: KG/Context/Memory/Plans are LOAD-BEARING

These four persistence layers exist for a reason — future-you, future-agents, and the user rely on them. Treat them like commits to a codebase, not optional notes:

- **Knowledge Graph** (`knowledge/**/*.md`): cross-project patterns, concepts, decisions. Write a node BEFORE moving on after learning a non-obvious fact (architecture, gotcha, decision rationale, post-incident insight). The `kg-update-nudge` hook fires every 150k tokens of work without a KG write — treat that nudge as a hard prompt, not optional. Bypass with `KG_NUDGE_OFF=1` only when in a release operation or other inappropriate context.
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
6. Quick analysis or rewrite → `chat` (Ollama MCP, FREE)
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
    Explicit overrides (`titles`, `summary`, `single_chunk`, `three_chunks`, `full`) apply uniformly to every result. Legacy alias `descriptions` → `summary`. Tier thresholds are tunable via `KG_TIER_MIN` / `KG_TIER_SINGLE_CHUNK` / `KG_TIER_THREE_CHUNKS` / `KG_TIER_FULL` env vars.
  - Each result carries `score` (0..1) and `tier` (the verbosity actually applied).
- `semantic_graph_search(query, depth, detail)` — GraphRAG with WikiLink traversal (~1–2 s). Same `detail="auto"` tiering on primary results; connected nodes always render at `summary` tier (graph topology, not score, drove their selection).
- `store_knowledge_node(..., scope)` — Write node; `scope="project"` (default) or `scope="shared"` (writes into the shared `VibeCodedTools_KnowledgeGraph` collection, visible to every other project).
- Use when: conceptual queries, discovering patterns, comprehensive research.

**3. Code Graph (Semantic Code Search)** — Weaviate MCP:
- `search_code_graph(query, scope, limit, expand_hops, detail)` — Find code by purpose/concept (~200–500 ms).
  - `scope`: `"all"` (default), `"code"` (functions/classes/modules), `"interaction"` (APIs/cross-service calls).
  - `expand_hops`: 0 (default) | 1 | 2 — follow call/interaction edges after seed retrieval.
  - `detail` (default `"auto"`) — code graph has no sidecar so tiering is position-based: `auto` gives top-4 full details + rest as metadata refs (legacy behaviour); `titles` returns metadata refs for all; `full` returns full details for all.
- `query_code_structure(query_type, target, project)` — Structural queries (~50–100 ms).
  - Types: `dependencies` | `imports` | `callers` | `methods` | `extends` | `interactions` | `path` | `composes` | `composed_by` | `type_users`.
  - `path`: target format `"source.func->dest.func"` (BFS up to depth 6).
  - `composes` / `composed_by`: composition relationships between classes.
  - `type_users`: functions using a given type in annotations.
- CLI: `.claude/scripts/code-graph-query search "auth middleware"`.
- Use when: finding code entities, understanding architecture, cross-service call mapping.

**4. Ollama MCP (Local LLM + Vision)** — FREE:
- `chat(prompt, model, system_prompt, temperature, max_tokens)` — Local inference (~1–3 s).
- `read_document(file_path, model, task, context_lines)` — Summarize or extract info from files; auto-switches to chunked scan for large files.
- `read_image(file_path, max_total_pixels, describe, vision_model, description_prompt)` — Read an image as a base64 data URL Claude can see directly. Optionally get a local text description from a vision model (default `qwen3.5:9b`, unified text+vision; no separate `-vl` tag needed). Auto-resizes images to fit within `max_total_pixels` (default 1,048,576 ≈ 1024×1024) to bound VRAM during local inference. Supports PNG, JPEG, GIF, WebP, BMP, TIFF, SVG.
  - **Memory-aware gating**: the description tier auto-skips if neither GPU VRAM nor system RAM is sufficient (image base64 is still returned for Claude's own vision). Auto-swaps to a smaller installed VLM when the requested model doesn't fit but a smaller one does. Per-model thresholds: `qwen3.5:9b` ≥7.5 GB VRAM / 12 GB RAM; `qwen3.5:7b` ≥6 / 10; `qwen3.5:4b` ≥4 / 7; `llama3.2-vision:11b` ≥9 / 16; `gemma3:4b` ≥5 / 8. Override default with `OLLAMA_VISION_MODEL` env var.
  - **Tiered resize budget**: `max_total_pixels` is an UPPER bound and is auto-clamped on tight hardware (1024² → 720² → 512² → 256²) based on free VRAM (or RAM in CPU mode). The clamp is reported as `image_budget_clamped_from`.
- Models: `gemma4:e4b` (fast inference + summarization for low-power machines), `qwen3-embedding:0.6b` (text embeddings, 1024-dim), `qwen3.5:9b` (text + vision; default for inference, summarization, and `read_image` on machines with ≥24 GB RAM or ≥7.5 GB VRAM).
- Use when: simple analysis, rewrites, summarizing files, reading images (all FREE).
- Roadmap: a heavier `image_interpretation_mcp` (object detection, OCR, table extraction via YOLO / Donut / GOT-OCR2) is in design; ships post-1.0 if user demand materializes. Today's `read_image` covers the common case.

**Decision Tree**:
- Known exact terms → `kg-search`
- Conceptual search → `hybrid_search` (default — searches KG + docs automatically)
- Relationships/graph → `semantic_graph_search`
- Code by purpose → `search_code_graph`
- Architecture queries → `query_code_structure`
- Quick analysis → `chat` (Ollama, FREE)
- Summarize/extract from file → `read_document` (Ollama, FREE)
- Read an image / vision task → `read_image` (Ollama, FREE)
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

**Per-project isolation**: `KG_COLLECTION` (per-project KG) and `SHARED_KG_COLLECTION` (cross-project shared, default `VibeCodedTools_KnowledgeGraph`) are set per-project via `.vscode/settings.json` `claude-code.env` and `.claude/settings.json` `env`. Per-project opt-out: `SHARED_KG_OPT_OUT=true` skips the shared collection on read. The active workspace determines the active KG, not which project is being discussed.

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

**Backend Python helpers** (next to the wrappers):
- `search_knowledge.py` — keyword search backend
- `sync_knowledge_graph.py` — parse/chunk/sync to Weaviate
- `maintain_knowledge_graph.py` — integrity checks
- `add_temporal_metadata.py` — add temporal fields from git
- `query_temporal.py` — point-in-time queries
- `migrate_to_vocabulary.py` — validate tags/vocabulary
- `analyze_code_graph.py` — AST-based code entity extraction
- `query_code_graph.py` — semantic/structural code queries

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
  - `<SHARED_KG_COLLECTION>` — cross-project shared KG (`VibeCodedTools_KnowledgeGraph` by default)
  - `<DEVELOPMENT_COLLECTION>` — verbose project docs (`docs/`)
  - `<CONVERSATION_COLLECTION>` — chat history (optional, often disabled)
  - `CodeModule`, `CodeClass`, `CodeFunction`, `CodeAPI`, `CodeInteraction` — code entities
- **Access**: Weaviate MCP tools (`hybrid_search`, `semantic_graph_search`, `store_knowledge_node`, `search_code_graph`, `query_code_structure`).

### Ollama Local LLM
- **URL**: `http://localhost:11435`
- **Purpose**: FREE local inference and embeddings (internal use).
- **Models**:
  - `qwen3-embedding:0.6b` — text embeddings (1024 dim, primary; needs `num_ctx=8192`)
  - `gemma4:e4b` — fast inference + summarization (low-power default, ~50-100 tok/s on CPU)
  - `qwen3.5:9b` — default inference + vision + summarization (auto-selected by `_select_text_model` when host has ≥24 GB RAM or ≥7.5 GB VRAM; falls back to `gemma4:e4b` on tighter hardware)
- **Access**: Ollama MCP tools (`chat`, `read_document`).
- **Cost**: FREE (runs locally).

### Code Embedding Service
- **URL**: `http://localhost:11440` (default; configurable via `CODE_EMBED_PORT`)
- **Purpose**: GPU-accelerated code embeddings via sentence-transformers.
- **Model**: CodeSage-Large-v2 (1.3B params, 2048-dim, Apache 2.0).
- **Start**: `python -m claude_mcp_servers.code_embedding_service.server`
- **Env**: `CODE_EMBED_BACKEND` (`gpu`/`ollama`), `CODE_EMBED_MODEL`, `CODE_EMBED_DEVICE`, `CODE_EMBED_PORT`.
- **Fallback**: set `CODE_EMBED_BACKEND=ollama` to skip the GPU path entirely (useful on CPU-only machines).

### MCP Servers
Located in the user's `~/.claude.json`. Each launches via the project venv (`claude_mcp_servers/.venv`):
- **weaviate-kg** — semantic search and code graph.
  - Command: `claude_mcp_servers/.venv/bin/python claude_mcp_servers/weaviate_mcp/server.py`
  - Env: `WEAVIATE_URL`, `OLLAMA_URL`, `EMBEDDING_MODEL`, `KG_COLLECTION`, `SHARED_KG_COLLECTION`, `DEVELOPMENT_COLLECTION`, `GRPC_PORT`, `SHARED_KG_OPT_OUT`.
- **ollama** — local LLM inference.
  - Command: `claude_mcp_servers/.venv/bin/python claude_mcp_servers/ollama_mcp/server.py`
  - Env: `OLLAMA_URL`.
- **coordination** — local KG-backed coordination notes (decisions, tasks, patterns).
  - Command: `claude_mcp_servers/.venv/bin/python claude_mcp_servers/coordination_mcp/server.py`
  - Env: `KG_BASE_DIR` (optional, defaults to project root).
  - Tools: `post_coordination_note`, `read_coordination_notes`.

**Claude SKU pinning** — this fork pins `claude/opus` → `claude-opus-4-6` (not 4-7) inside the orchestrator's model resolver. Don't rewrite that mapping unless you're consciously upgrading; downstream agents and skills assume 4-6's behaviour.

**Free-tier RL gate** — `_rl_cache_and_rerank` skips RL reranking when `feature_enabled("rl_retrieval") == False`. Pro/MAO licenses unlock RL; free-tier users get plain Weaviate cosine ordering. Nothing breaks when the gate is closed — retrieval just falls back to base scores.

### Hook System
Located in `.claude/hooks/` — automated workflow actions. On Windows the bash hooks need WSL2 to fire automatically.

**SessionStart (startup)**:
- `ensure-containers.sh` — auto-start Podman containers (Weaviate, Ollama, code-embed) if not running (background).
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

**TaskCompleted** (Agent Teams only):
- `task-completed-validate.sh` — quality gates for agent output.

**TeammateIdle** (Agent Teams only):
- `teammate-idle-redirect.sh` — redirect idle agents to pending plan items.

**SessionEnd**:
- `session-end.sh` — cleanup, final sync.

**All available hook events** (as of v2.1.81):

| Event | Can Block? | Notes |
|---|---|---|
| `SessionStart` | No | Matchers: `startup`, `compact`, `resume` |
| `UserPromptSubmit` | Yes | |
| `PreToolUse` | Yes | Matcher = tool name pattern |
| `PermissionRequest` | Yes | |
| `PostToolUse` | No | Matcher = tool name pattern |
| `PostToolUseFailure` | No | |
| `InstructionsLoaded` | No | Fires when CLAUDE.md/agents/skills loaded (v2.1.69) |
| `SubagentStart` | No | |
| `SubagentStop` | Yes | Exit 2 prevents stop |
| `Notification` | No | |
| `Stop` | Yes | |
| `StopFailure` | No | |
| `PreCompact` | No | |
| `PostCompact` | No | |
| `SessionEnd` | No | |
| `ConfigChange` | Yes | Except policy_settings |
| `TeammateIdle` | Yes | Exit 2 → feedback to teammate, keeps working. Requires Agent Teams. |
| `TaskCompleted` | Yes | Exit 2 → task not marked done, stderr fed to model. Requires Agent Teams. |
| `WorktreeCreate` | Yes | Must print absolute worktree path on stdout |
| `WorktreeRemove` | No | Cleanup only |
| `Elicitation` | Yes | MCP server input forms (v2.1.76) |
| `ElicitationResult` | Yes | Auto-respond to elicitation (v2.1.76) |

---

## Storage Systems

**1. Knowledge Graph** (`knowledge/` → `KG_COLLECTION`):
- Per-project patterns, concepts, learnings.
- Concise (<300 lines per node).
- Search: `kg-search` CLI or `hybrid_search` MCP (auto-merges with shared KG + docs).
- Format: Markdown with YAML frontmatter, typed WikiLinks.
- Embedding: `qwen3-embedding:0.6b` (1024-dim) via Ollama.

**2. Shared KG** (`VibeCodedTools_KnowledgeGraph`):
- Cross-project patterns visible to every workspace by default.
- Auto-merged into `hybrid_search` results unless `SHARED_KG_OPT_OUT=true`.
- Write to it explicitly with `store_knowledge_node(scope="shared", ...)`.

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

**5. Conversation Collection** (`CONVERSATION_COLLECTION`):
- Chat history, decisions, discoveries.
- Auto-capture is opt-in (often disabled).
- Search: `hybrid_search` (auto-scoped when present).

**Decision Tree**:
- Reusable cross-project pattern → shared KG (`scope="shared"`).
- Project-specific pattern → per-project KG.
- Code entities → code graph.
- Verbose docs → development collection.
- Quick local analysis → Ollama MCP (FREE).

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
**Spawn agents**: `@agent-name (Model)` via the Agent tool. (`Task` is a legacy alias for `Agent`, renamed in v2.1.63.)

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

**Parallel Execution**:
- Lengthy task (>2 hours) → break into 3–6 independent subtasks.
- Spawn multiple agents in a single message via multiple Agent calls.
- **Cap at 3 parallel agents** to avoid context overflow when they all return.
- `run_in_background: true` runs the agent in background; you get notified on completion. Use for independent work that doesn't block your next step.
- Resume stopped agents with `SendMessage({to: agentId})` (the `resume` param on the Agent tool was removed in v2.1.74+).

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
- `effort` — `low` | `medium` | `high` | `max` — caps per-agent reasoning cost.
- `isolation: worktree` — runs in a temporary git worktree (auto-cleaned if no changes).
- `background: true` — always run as background task.
- `memory` — `user` | `project` | `local` — enables persistent agent memory directory.
- `skills` — skills injected into subagent context (not inherited from parent).
- `mcpServers` — MCP servers scoped to this subagent only.
- `hooks` — lifecycle hooks scoped to this subagent.

---

## Context Efficiency

**File Operations**: check before reading (Grep first), use `offset`/`limit` for large files, trust writes (no re-reads), spawn agents for multi-file ops (cap at 3 parallel).

**Lean-ctx shim**: `.claude/scripts/leanctx-bash-env.sh` is sourced automatically via the `BASH_ENV` env var set in `.claude/settings.json`. When `lean-ctx` is on PATH, it compresses CLI output ~90–97% by wrapping common commands (`git`, `npm`, `pip`, `grep`, `ls`, `find`, etc.) and stripping boilerplate, progress bars, and redundant lines. Behaviour is identical, output is just much shorter. Bypass:
- `LEAN_CTX_OFF=1 some-command` — one-shot disable.
- `lean-ctx bypass "some-command"` — explicit bypass.

The shim is a no-op when `lean-ctx` isn't installed.

**⚠️ Known footgun — silent stderr swallowing on `git commit`**: `lean-ctx`'s default mode can swallow stderr from `git commit` to the point where a hook-failed commit returns exit code 1 with **zero output**, making the failure invisible. Symptom: `git commit` returns exit 1 but no error message; subsequent `git status` shows the file is staged but uncommitted. **Workaround**: prefix any `git commit` (and any command where you suspect lean-ctx is hiding errors) with `LEAN_CTX_OFF=1`:

```bash
# If `git -c user.name=... commit -m "..."` exits 1 silently:
LEAN_CTX_OFF=1 git -c user.name=... commit -m "..."
```

This affects automated agents and Claude Code sessions on this machine. Apply the same workaround for `git push` if it returns silent non-zero (rare, but possible with pre-push hooks). When in doubt, prefix `LEAN_CTX_OFF=1` for any git command that exits non-zero with no output.

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
# WRONG: use Claude API for simple task (wastes tokens)
# CORRECT: use Ollama MCP (FREE)
chat("Rewrite this docstring to be clearer: [docstring]", model="gemma4:e4b")
read_document("/path/to/large_file.py", task="find the authentication logic")  # Extract from file, FREE
```

---

## Quick Reference

- Start: read `.claude/CONTEXT_STATE.md`.
- Test: `pytest tests/`.
- Sync KG: `.claude/scripts/kg-sync --all`.
- Analyze code: `.claude/scripts/code-graph-analyze . --project "MyProject"`.
- Search code: `.claude/scripts/code-graph-query search "pattern"`.
- Search knowledge: `hybrid_search("concept")` (Weaviate MCP).
- Quick analysis (FREE): `chat("prompt", model="gemma4:e4b")` (Ollama MCP).
- MCP venv: `source claude_mcp_servers/.venv/bin/activate`.
- Active plans: `.claude/context/plans/`.
- Tag hierarchy: `knowledge/TAG_HIERARCHY.md`.
- Score-tier reference: `knowledge/concepts/score-driven-retrieval-tiers.md`.
- Plan mode: `claude --plan` or `/plan` (read-only exploration).
- Compact with focus: `/compact focus on <topic>`.
- Fix from PR: `claude --from-pr <PR-URL>`.

**Default ports**: Weaviate `8081` (HTTP) / `50052` (gRPC), Ollama `11435`, code-embed service `11440` (optional GPU).
**Default models**: text embeddings `qwen3-embedding:0.6b` (1024-dim), code embeddings CodeSage-Large-v2 (2048-dim, GPU) / `qwen3-embedding:0.6b` (CPU fallback), inference `gemma4:e4b` (low-power) / `qwen3.5:9b` (default + vision).

---

<!-- BEGIN: SETUP-ONLY (remove after first successful session) -->
## First-Run Setup

If you're seeing this orchestrator for the first time, here's the 60-second mental model:

- **What it is**: an infrastructure layer for Claude Code that adds persistent memory (KG), semantic code search (code graph), and ~20 hooks that run automatically. You keep using Claude Code the way you already do; the orchestrator works in the background via hooks and MCP servers.
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
# Expected: weaviate-kg ✓ Connected, ollama ✓ Connected (and search if configured)

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
