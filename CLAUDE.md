# VibeCoded Orchestrator — Project Instructions

These instructions are loaded by Claude Code whenever you open a project that has the orchestrator installed. They tell Claude how to use the Knowledge Graph, Code Graph, hooks, and MCP servers shipped with this repo.

## What This Is

An infrastructure layer on top of Claude Code that adds:
- **Knowledge Graph** — persistent semantic memory across sessions (Markdown + Weaviate + Ollama embeddings)
- **Code Graph** — AST-based code understanding with semantic search (modules, classes, functions, APIs, cross-service calls)
- **Automated Context Injection** — hooks that proactively feed relevant context to Claude
- **Security Scanning** — credential detection, shell-injection prevention
- **Workflow Automation** — CONTEXT_STATE tracking, plans, memory management, compaction-preserving replay

You use Claude Code normally. The orchestrator works in the background via hooks and MCP servers.

## Free vs Pro

The free tier in this repo is **fully functional standalone** — KG, code graph, hooks, MCP servers, and the bundled agents/skills all work without any license key.

Optional paid modules (RL-scored retrieval reranking, multi-agent maestro runtime, specialist agent packs) live in separate modules and activate when a license key is present. Without a key, retrieval gracefully falls back to cosine ordering.

---

## SESSION START (DO THIS FIRST)

**At session start and before answering any non-trivial question:**
1. Read `.claude/CONTEXT_STATE.md` — current task, recent progress, blockers
2. For architecture/system design questions → check `docs/ARCHITECTURE.md` if your project has one
3. For any other project question → search the KG/code graph **before** generating a response

**Never explain project state from memory.** If you don't know something with certainty, look it up first.

---

## Context Management

**Two-Layer Memory**:
- `MEMORY.md` (`~/.claude/projects/.../memory/`) — Claude's self-written operational notes. First 200 lines auto-loaded each session (hard cap; silent truncation after). Stable patterns, recurring bug fixes, key paths, user preferences. NOT in git. Keep it as a concise index; put detail in topic files (`debugging.md`, etc.). Edit via `/memory` or ask Claude directly ("remember that…").
- `.claude/CONTEXT_STATE.md` — Active working memory (250–350 lines, max 500). Current task, recent progress, next steps, active blockers. Update during work, not just at the end.

| MEMORY.md | CONTEXT_STATE.md |
|---|---|
| "use pnpm not npm", solved recurring bugs, stable architecture facts, service URLs | Current sprint goal, recent progress ✅, next steps, open blockers |

**Other Context**:
- `.claude/context/archive/` — Completed tasks
- `.claude/context/plans/` — Active plans (referenced, not auto-loaded)

**Token Efficiency**:
- Parallel tool calls: Read/Grep multiple files in a single message
- Read files directly when small (<150 lines)
- Cache mentally for 20–30 minutes
- Limit command output: `| head -30` or `2>&1 | tail -20`
- Skip echoing after writes
- Spawn agents for multi-file ops

**Large Documents (>20 pages)**: Skim ToC/structure first, then targeted section reads; note discoveries as you go.

---

## CRITICAL: KG-First Search Policy

**Before searching OR reasoning about project topics, choose the right tool:**
1. Conceptual question? → `hybrid_search` (Weaviate MCP) — searches KG + docs together
2. Relational / "what links to what"? → `semantic_graph_search` (Weaviate MCP)
3. Code-by-purpose? → `search_code_graph` (Weaviate MCP)
4. Quick analysis or rewrite? → `chat` (Ollama MCP, FREE local)
5. Still unsure? → `hybrid_search` (most comprehensive)

**This applies to reasoning, not just searching** — do NOT explain what the codebase does, what patterns exist, or what was previously decided from memory alone. Look it up first.

**Only use Grep/Read when**:
- The user provides an exact file path
- You're searching for a literal string (variable name, error message)
- The file is already in context
- You need line-by-line detail

**Use the code graph to explore the codebase, not file reads:**
- "How does X work?" → `search_code_graph("X")` BEFORE opening files
- "What calls function Z?" → `query_code_structure("callers", "Z")` BEFORE grep
- "What does this module import?" → `query_code_structure("dependencies", "file.py")`
- Only `Read` once you know which lines you need

**Default: check sources first, reason second.**

---

## Search Systems

### 1. kg-search / kg-info CLI (keyword/metadata, ~100 ms)
For known exact terms, tags, or node titles:
```bash
.claude/scripts/kg-search search "term" [--type TYPE] [--tags TAGS] [--limit N]
.claude/scripts/kg-search list|recent|created [--days N]
.claude/scripts/kg-info info "Node Title"
.claude/scripts/kg-info connections "Node Title"
```

### 2. Weaviate MCP (semantic / graph, ~1–2 s)
- `hybrid_search(query, limit, node_type, tags, days, detail)` — keyword + semantic across KG + project docs. **Default search tool.**
  - `detail` defaults to `"auto"` — score-driven per-result verbosity. Each hit is rendered at one of 5 tiers based on its relevance score (`discard` <0.42 / `summary` 0.42–0.55 / `single_chunk` 0.55–0.65 / `three_chunks` 0.65–0.75 / `full` ≥0.75). The top hit gets full content; marginal hits get a 6-line summary; noise is filtered. See [`knowledge/concepts/score-driven-retrieval-tiers.md`](knowledge/concepts/score-driven-retrieval-tiers.md).
  - Explicit overrides apply uniformly: `"titles"` | `"summary"` | `"single_chunk"` | `"three_chunks"` | `"full"`. Legacy alias: `"descriptions"` → `"summary"` (back-compat).
  - Tier thresholds env-tunable: `KG_TIER_MIN`, `KG_TIER_SINGLE_CHUNK`, `KG_TIER_THREE_CHUNKS`, `KG_TIER_FULL`.
- `semantic_graph_search(query, depth, detail)` — GraphRAG with WikiLink traversal. Same `detail` tiers as above; connected nodes always render at `summary` (graph topology, not score, drove their selection — re-fetching for scores would add 300–800 ms).
- `store_knowledge_node(..., scope)` — write a node; `scope="project"` (default) or `"shared"`

### 3. Code Graph (Weaviate MCP)
- `search_code_graph(query, scope, limit, expand_hops, detail)` — find code by purpose/concept (~200–500 ms)
  - `scope`: `"all"` (default) | `"code"` (functions/classes/modules) | `"interaction"` (APIs / cross-service calls)
  - `expand_hops`: 0 | 1 | 2 — follow call/interaction edges after seed retrieval
  - `detail`: `"auto"` (default — top-4 full, rest as refs) | `"titles"` (cheapest) | `"full"` (every result full). Code graph has no sidecar so tiering is position-based (rank), not score-thresholded. Score is surfaced in every result regardless.
- `query_code_structure(query_type, target, project)` — structural queries (~50–100 ms)
  - types: `dependencies` | `imports` | `callers` | `methods` | `extends` | `interactions` | `path` | `composes` | `composed_by` | `type_users`
  - `path` target format: `"source.func->dest.func"` (BFS up to depth 6)
- CLI: `.claude/scripts/code-graph-query search "auth middleware"`

### 4. Ollama MCP (local LLM — FREE)
- `chat(prompt, model, system_prompt, temperature, max_tokens)` — local inference (~1–3 s)
- `read_document(file_path, model, task, context_lines)` — summarize/extract from files; auto-chunks for large files
- `read_image(file_path)` — load image as base64 for vision + optional local description
- Models: `qwen3:0.6b` (fast inference), `qwen3:latest` (8B), `qwen3-embedding:0.6b` (text embeddings)

### Decision tree

| Need | Tool |
|---|---|
| Known exact term | `kg-search` CLI |
| Conceptual search | `hybrid_search` (default) |
| Relationships / graph | `semantic_graph_search` |
| Code by purpose | `search_code_graph` |
| Architecture queries | `query_code_structure` |
| Quick analysis (FREE) | `chat` (Ollama) |
| Summarize/extract from file (FREE) | `read_document` (Ollama) |
| Literal strings | Grep |
| File content | Read |

---

## Project Layout

**Tech Stack**: Python 3.11+, Markdown (Obsidian-style) for KG, Weaviate, Ollama, optional CodeSage-Large-v2 (GPU code embeddings), pytest. MCP venv at `claude_mcp_servers/.venv/`.

**Directories**:
- `knowledge/` — KG nodes (`projects/`, `concepts/`, `tools/`, `models/`, `research/`, …)
- `.claude/` — Workflow config (`context/`, `scripts/`, `hooks/`, `logs/`)
- `docs/` — Project documentation
- `config/` — Configuration files
- `documents/` — External content ingestion (papers, references, guides) → auto-chunked to Weaviate `DocumentChunks` + KG node created
- `claude_mcp_servers/` — MCP servers

---

## Per-Project KG Isolation

Each project gets its own Weaviate collections so KG content does not leak across projects. The active collection is controlled by environment variables, set per-project in `.vscode/settings.json` (under `claude-code.env`) or in shell rc when using the CLI:

| Variable | Purpose |
|---|---|
| `KG_COLLECTION` | Project-scoped knowledge graph collection (e.g. `MyProject_KnowledgeGraph`) |
| `SHARED_KG_COLLECTION` | Optional cross-project shared KG (e.g. `SharedKnowledgeGraph`) |
| `DEVELOPMENT_COLLECTION` | Project docs collection (e.g. `MyProject_development`) |
| `CONVERSATION_COLLECTION` | Optional chat-history collection |
| `PROJECT_NAME` | Human-readable project name used in logs |
| `KG_BASE_DIR` | Where `store_knowledge_node` writes `.md` files when given a relative path |

The active VS Code workspace determines the KG, not which project is being discussed. Working on another project's files from this chat → writes still go to *this* workspace's KG. If you want them in another project's KG, switch workspaces.

`.vscode/settings.json.example` (if present) shows the recommended layout for a new project.

---

## Knowledge Graph Format

Obsidian-style Markdown with YAML frontmatter:

```yaml
---
title: Node Title
type: concept   # project, concept, tool, research, model, hardware
tags: [tag1, tag2]
created: 2025-01-15T10:30:00Z
updated: 2025-01-28T14:22:00Z
valid_from: 2025-01-15T00:00:00Z
valid_until: null
status: active   # active, archived, deprecated, idea
---
```

**Typed WikiLinks** — `[[relationshipType::Target]]`:
- `[[uses::Tool]]` — uses tool/technology
- `[[implements::Concept]]` — implements pattern
- `[[extends::Parent]]` — extends/specializes
- `[[buildsOn::Work]]` — builds upon
- `[[relatedTo::Node]]` — general (default)

**Tags**: high/mid/low-level (`#high-level-plan`, `#mid-level-architecture`, `#low-level-implementation`), tech tags (`#python`, `#AI`, …), lifecycle (`#idea`, `#implemented`). See `knowledge/TAG_HIERARCHY.md` and `knowledge/VOCABULARY.md` if your project ships them.

**Node Guidelines**:
- High-level: broad overviews (<300 lines)
- Mid-level: specific domains (<200 lines)
- Low-level: individual tools/models (<150 lines)
- One node per tool/model/concept
- Links unidirectional (projects → concepts)

**KG Write Rule** — `store_knowledge_node` always writes the `.md` file (upsert: only on new or changed content). File-path resolution priority:
1. `file_path` is absolute → written directly
2. `file_path` is relative + `KG_BASE_DIR` set → `KG_BASE_DIR/file_path`
3. `file_path` is relative + `KG_BASE_DIR` unset → falls back to inferred project root
- Include the `knowledge/` prefix (e.g. `knowledge/concepts/foo.md`)
- Check `file_written: true` and `absolute_path` in the response to confirm where the file landed
- **Preferred workflow**: write `.md` first → hook auto-syncs to Weaviate. `store_knowledge_node` is a secondary path (used by agents that can't write files directly)
- **Warning**: subagents may inherit the global KG collection rather than the project one. Pass an absolute `file_path` if you need to be sure where the file lands.

---

## Storage Systems

**1. Knowledge Graph** (`knowledge/` → your `KG_COLLECTION`)
- Cross-project patterns, concepts, learnings
- Concise (<300 lines)
- Search: `kg-search` CLI or `hybrid_search` MCP

**1b. Shared Knowledge Graph** (`SHARED_KG_COLLECTION`, default `VibeCodedTools_KnowledgeGraph`)
- Cross-project shared collection bundled with the orchestrator install
- Seeded at install time from `vibecoded-orchestrator/knowledge/`
- Every project queries it alongside its own KG by default
- Per-project opt-out: set `SHARED_KG_OPT_OUT=true` (any of the three env surfaces)
- Writes: `store_knowledge_node(scope="shared")` — power-user only; default scope is "project"
- Sidecar: `<orchestrator>/knowledge/.node_formats.json` (separate from per-project sidecar)
- See `knowledge/concepts/shared-knowledge-graph.md` for the design.

**2. Code Graph** (Weaviate code collections)
- `CodeModule` — files with imports and metrics
- `CodeClass` — classes with inheritance, methods, composition
- `CodeFunction` — functions with call graphs and signatures
- `CodeAPI` — API endpoints with handlers
- `CodeInteraction` — cross-service calls (HTTP/gRPC/queue/etc.)
- Search: `search_code_graph`, `query_code_structure`, or the `code-graph-query` CLI
- Build/refresh: `.claude/scripts/code-graph-analyze . --project "MyProject" [--cfg] [--pdg]` (CFG/PDG flags require `joern` in PATH)

**3. Development Collection** (`docs/` → your `DEVELOPMENT_COLLECTION`)
- Project-specific docs, auto-synced via PostToolUse hooks
- Search: `hybrid_search` (auto-scoped)

**4. Conversation Collection** (optional)
- Chat history, decisions, discoveries
- Disabled by default; enable by setting `CONVERSATION_COLLECTION`

**Decision tree**:
- Reusable pattern → KG (`knowledge/`)
- Code entities → Code Graph
- Verbose project docs → Development collection (`docs/`)
- Quick local analysis → Ollama MCP (FREE)

---

## Infrastructure

### Weaviate
- Default URL: `http://localhost:8081` (HTTP), `50052` (gRPC) — configurable
- Text embeddings: `qwen3-embedding:0.6b` via Ollama (1024-dim, Apache 2.0)
- Code embeddings: CodeSage-Large-v2 via FastAPI service (2048-dim, GPU) — falls back to Ollama on CPU-only setups
- Active embedding controlled by `ACTIVE_EMBEDDING` env var
- Optional OpenAI vectors when `OPENAI_API_KEY` is set

### Ollama (FREE, local)
- Default URL: `http://localhost:11435`
- Models: `qwen3-embedding:0.6b` (text embeddings, primary), `qwen3:0.6b` (fast chat), `qwen3:latest` (8B for document processing)
- Cost: free, runs locally

### Code Embedding Service (optional, GPU)
- Default URL: `http://localhost:11440`
- Model: CodeSage-Large-v2 (1.3B params, 2048-dim, Apache 2.0)
- Env: `CODE_EMBED_BACKEND` (`gpu` | `ollama`), `CODE_EMBED_MODEL`, `CODE_EMBED_DEVICE`, `CODE_EMBED_PORT`
- Set `CODE_EMBED_BACKEND=ollama` to use Ollama embeddings instead

### MCP Servers (configured in `~/.claude.json`)
- **weaviate-kg** — semantic search + code graph
- **ollama** — local LLM inference + embeddings + image loading
- **search** — web / code / academic paper search
- **coordination** (optional) — local KG-backed coordination notes (`post_coordination_note`, `read_coordination_notes`)

### Bootstrap

If you came here through the VibeCoded Tools launcher, secrets and services are managed for you. If you cloned manually, the installer (`install.py` / `install.sh` / `install.ps1`) sets up the venv, MCP servers, hooks, and container compose files. See `docs/` for details.

---

## Hook System

Located in `.claude/hooks/`. Hooks are bash scripts on Linux/macOS; on Windows they require WSL2 to fire automatically.

**SessionStart (startup)**
- Auto-start containers (Weaviate, Ollama, …) if not running
- Display KG resource paths
- Warn if `CONTEXT_STATE.md` exceeds the size threshold

**SessionStart (compact)**
- Re-inject `CONTEXT_STATE.md` + recent commits + active plan + pre-compact snapshot

**PreCompact / PostCompact**
- Save git status + recent files to a snapshot before compaction
- Log compaction event + desktop notification

**UserPromptSubmit**
- Workflow reminders
- Diff-based context injection of `CONTEXT_STATE.md` (large token savings after first prompt)

**PreToolUse**
- Tool-usage logging
- KG + code graph search before `Edit`

**PostToolUse**
- Auto-sync on file edits: `knowledge/` → KG, `docs/` → Development collection, code files → code graph queue
- Python writes: `py_compile` syntax check, `ruff` auto-fix, `pyright` type-check (background)
- Document processing for `documents/` uploads
- Credential scan on written files

**Stop / StopFailure / SessionEnd**
- Cost tracking (logs token usage and cost to a JSONL)
- Desktop notifications, including urgent on failure
- Final cleanup and sync

**Available hook events** (Claude Code v2.1.81+):

| Event | Can Block? | Notes |
|---|---|---|
| `SessionStart` | No | Matchers: `startup`, `compact`, `resume` |
| `UserPromptSubmit` | Yes | |
| `PreToolUse` | Yes | Matcher = tool name pattern |
| `PermissionRequest` | Yes | |
| `PostToolUse` | No | |
| `PostToolUseFailure` | No | |
| `InstructionsLoaded` | No | Fires when CLAUDE.md/agents/skills loaded |
| `SubagentStart` / `SubagentStop` | Stop: Yes | |
| `Notification` | No | |
| `Stop` / `StopFailure` | Stop: Yes | |
| `PreCompact` / `PostCompact` | No | |
| `SessionEnd` | No | |
| `ConfigChange` | Yes (except policy) | |
| `WorktreeCreate` / `WorktreeRemove` | Create: Yes | Print absolute worktree path on stdout |
| `Elicitation` / `ElicitationResult` | Yes | MCP server input forms |

---

## Scripts

```bash
# Knowledge graph
.claude/scripts/kg-search search "query" [--type TYPE] [--tags TAGS] [--limit N]
.claude/scripts/kg-info info "Title"
.claude/scripts/kg-info connections "Title"
.claude/scripts/kg-sync FILE | --all
.claude/scripts/kg-duplicates [--threshold 0.95]

# Code graph
.claude/scripts/code-graph-analyze /path/to/repo [--project NAME] [--incremental] [--cfg] [--pdg]
.claude/scripts/code-graph-query search "auth middleware" [--collection TYPE] [--limit N]
.claude/scripts/code-graph-query similar "module.function" [--limit N]
.claude/scripts/code-graph-query structure dependencies|callers|methods|extends|interactions "target"
```

Backend Python scripts live next to the CLI wrappers (sync, maintain, temporal queries, vocabulary validation, code analysis, code queries).

---

## Communication

**Professional Objectivity**
- Prioritize technical accuracy over validation
- Challenge incorrect assumptions with evidence
- Pattern: Challenge → Evidence → Alternative → wait for decision

**Anti-Sycophancy Rules**
- Check actual evidence before claiming success — don't assume
- Avoid superlatives: "Great!", "Perfect!", "Beautifully!", "Amazing!"
- State facts objectively: "X launched" not "X working perfectly"
- When uncertain, say so: "We'll know when…" not "This will work"
- Don't validate feelings: "That works because…" not "Great idea!"
- Challenge politely, with evidence

**Specification Adherence**
- Follow specs exactly — don't skip steps or simplify without permission
- Never use placeholders: `... rest unchanged`, `// existing code`, `<!-- rest of HTML -->`
- Implement general solutions for ALL inputs, not just test cases
- Don't hard-code values to finish faster
- Priority: Real-world functionality per spec > tests passing > speed
- Good simplification (remove complexity, keep behavior) is encouraged
- Lazy shortcuts (skip features, drop edge cases, use workarounds) are forbidden
- If a task is unclear, ask — don't guess and implement the wrong thing

**When to Ask vs Decide**
- Ask: architecture choices, tech selection, breaking changes, multiple plausible approaches
- Decide autonomously: bug fixes, optimizations, refactors, docs
- **Ambiguous requirements**: if you find yourself reasoning through multiple interpretations in your response, STOP and ask instead of speculating out loud.

---

## Workflow

**Knowledge Management**
- Proactively capture: project details, architecture, decisions, preferences, learnings
- Create nodes in the appropriate `knowledge/` subfolder
- Use typed WikiLinks for implementation relationships
- Sync via hooks or `.claude/scripts/kg-sync`

**Update CLAUDE.md when**: new directories, tech-stack changes, new scripts/tools, new patterns, new KG conventions.

**Update CONTEXT_STATE.md**:
- During work, not just at the end
- Mark completed subtasks (✅)
- Add discoveries, new nodes, blockers

---

## Agents & Skills

The installer drops a default set of agents into `.claude/agents/` and skills into `.claude/skills/`. Customize freely — they are templates, not framework code.

**Invoke skills**: `/skill-name` (e.g. `/architect`, `/tdd`)
**Spawn agents**: `@agent-name (Model)` via the Agent tool

**Model Selection**
- **Opus**: complex tradeoffs, security review, deep debugging — use sparingly
- **Sonnet**: implementation, planning, guidance — default
- **Haiku**: simple tasks, calculations, tests — use freely

**When to Spawn**
- Task is parallel to current work
- Requires sustained focus (30+ min)
- Benefits from isolated context
- Substantial output (>200 lines)

**Don't Spawn**
- <5 min tasks
- Needs immediate back-and-forth
- Exploring/brainstorming

**Handoff Format** (300–500 tokens):
```
@agent-name (Model)
Task: One-sentence goal
Context: file paths, patterns, constraints
Success Criteria: what "done" looks like
Output: where to save
```

**Parallel Execution**
- Long task (>2 h) → break into 3–6 independent subtasks
- Spawn multiple agents in a single message with multiple Agent calls
- **Cap at 3 parallel agents** to avoid context overflow when they all return
- Use `run_in_background: true` for independent work that doesn't block your next step
- Resume stopped agents with `SendMessage({to: agentId})` (do NOT pass `resume` to the Agent tool — removed)

**Agent Frontmatter** (in `.claude/agents/NAME.md`):
- `name`, `description` (required)
- `model` — `sonnet` | `opus` | `haiku` | `inherit` (default) | full model ID
- `tools` / `disallowedTools` — allow/deny lists
- `permissionMode` — `default` | `acceptEdits` | `dontAsk` | `bypassPermissions` | `plan`
- `maxTurns`, `effort` (`low`/`medium`/`high`/`max`)
- `isolation: worktree` — runs in a temporary git worktree
- `background: true` — always run as background task
- `memory` — `user` | `project` | `local`
- `skills`, `mcpServers`, `hooks` — scoped to this subagent

**Skill Frontmatter** (in `.claude/skills/NAME/SKILL.md`):
- VS Code validates: `name`, `description`, `argument-hint`, `disable-model-invocation`, `user-invocable`, `compatibility`, `license`, `metadata`
- CLI/runtime supported (VS Code warns but works): `model`, `effort`, `allowed-tools`, `context`, `agent`, `hooks`
- `disable-model-invocation: true` — only the user can invoke via `/command`
- `user-invocable: false` — hidden from `/` menu, only Claude can auto-invoke
- `context: fork` — run in an isolated subagent context

**Coordination Patterns**
- **Blackboard**: agents volunteer for tasks listed in `CONTEXT_STATE.md` (research shows 13–57% better outcomes than hierarchical delegation, with lower token cost and better parallelism)

---

## Context Efficiency

**File operations**: check before reading (Grep, or `~/.claude/scripts/smart_file_ops.py check` if installed), use `offset`/`limit` for large files, trust writes (no re-reads), spawn agents for multi-file ops.

**Compaction**: critical context is preserved via PreCompact/PostCompact hooks. Before compacting manually, update `CONTEXT_STATE.md` with current state, modified files, open blockers. Use `/compact focus on <topic>` to guide the summary.

**Target metrics**:
- Simple task: <5K tokens
- Complex task: <20K tokens
- Session: <100K tokens

---

## Tool Usage Examples

**Implementing a new feature**
```python
# WRONG: skip knowledge search, jump to grep
Grep("def.*auth", type="py")  # only finds literal function names

# RIGHT: KG-first
hybrid_search("authentication patterns for web APIs")
search_code_graph("authentication middleware", scope="code")
query_code_structure("callers", "api.auth.validate_token")
# THEN Grep for exact strings, Read for detail
```

**Quick analysis or rewrite**
```python
# WRONG: burn Claude tokens on a 5-line rewrite
# RIGHT: free local model
chat("Rewrite this docstring to be clearer: [docstring]", model="qwen3:0.6b")
read_document("/path/to/large_file.py", task="find the authentication logic")
```

---

## GitHub Access (recommended setup)

Storing your GitHub PAT in `~/.claude.json` is risky — it ends up readable by every MCP server. The recommended pattern:

- Put the token in a single dedicated file, e.g. `~/.your-secrets/github_pat`, `chmod 600`, **never commit**
- Have the search MCP wrapper read that file and export `GITHUB_TOKEN` before exec'ing the real server
- Configure git's credential helper to read the same file (`git config --global credential.https://github.com.helper /path/to/helper`)
- For shell / `gh` CLI: `export GITHUB_TOKEN=$(cat ~/.your-secrets/github_pat) && gh <command>`

Rotating the PAT then means editing one file — no other config changes.

---

## Claude Code Auth

Claude Code can authenticate either via API key (`ANTHROPIC_API_KEY`) or via claude.ai OAuth login. Some features (`--channels`, managed agents, certain plugins) are gated on claude.ai auth — they're available only when logged in through OAuth, not via API key. If a feature seems missing, check which auth mode is active before assuming it's unavailable.

---

## Quick Reference

- Start: read `.claude/CONTEXT_STATE.md`
- Test: `pytest tests/`
- Sync KG: `.claude/scripts/kg-sync --all`
- Analyze code: `.claude/scripts/code-graph-analyze . --project "MyProject"`
- Search code: `.claude/scripts/code-graph-query search "pattern name"`
- Search knowledge: `hybrid_search("concept")` (Weaviate MCP)
- Quick analysis (FREE): `chat("prompt", model="qwen3:0.6b")` (Ollama MCP)
- MCP venv: `source claude_mcp_servers/.venv/bin/activate`
- Active plans: `.claude/context/plans/`
- Plan mode: `claude --plan` or `/plan` — read-only exploration before implementation
- Compact with focus: `/compact focus on <topic>`
- Fix from PR: `claude --from-pr <PR-URL>` — auto-loads PR diff as context

## Lean-ctx Shell Compression

`lean-ctx` (MIT, zero telemetry) compresses CLI output by 90-97% by wrapping common commands (`git`, `npm`, `pip`, `grep`, `ls`, etc.) and stripping boilerplate, progress bars, and redundant lines. In interactive shells this is handled by `~/.bashrc`. For Claude Code's non-interactive Bash subprocesses (which never source `~/.bashrc`), the shim `.claude/scripts/leanctx-bash-env.sh` is sourced automatically via the `BASH_ENV` env var set in `.claude/settings.json`. The compression is transparent — commands behave identically, output is just much shorter.

**Bypass options** (use either when you need raw output):
- `LEAN_CTX_OFF=1 some-command` — one-shot disable for that invocation
- `lean-ctx bypass "some-command"` — explicit bypass via lean-ctx itself

**Shim location**: `.claude/scripts/leanctx-bash-env.sh` (idempotent, safe no-op if lean-ctx not installed)
**install.py**: automatically wires BASH_ENV into `.claude/settings.json` when lean-ctx is detected at install time
