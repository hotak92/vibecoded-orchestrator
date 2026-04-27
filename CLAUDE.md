# VibeCoded Orchestrator — Project Instructions

These instructions are loaded by Claude Code whenever you open a project that has the orchestrator installed. They tell Claude how to use the Knowledge Graph, Code Graph, hooks, and MCP servers shipped with this repo.

---

## SESSION START (always)

**Before answering any non-trivial question:**
1. Read `.claude/CONTEXT_STATE.md` — current task, recent progress, blockers.
2. For architecture/system questions → check `docs/ARCHITECTURE.md` if the project ships one.
3. For any other project question → search the KG / code graph **before** generating a response.

**Never explain project state from memory.** If you don't know something with certainty, look it up first.

---

## CRITICAL: KG-First Search Policy (always)

**Before searching OR reasoning about project topics, choose the right tool:**

| Need | Tool | Notes |
|---|---|---|
| Conceptual question | `hybrid_search` (Weaviate MCP) | Default — searches KG + docs together |
| Relationships / "what links to what" | `semantic_graph_search` | GraphRAG with WikiLink traversal |
| Code by purpose / concept | `search_code_graph` | Functions/classes/modules/APIs |
| Architecture / callers / deps | `query_code_structure` | Structural queries (~50–100 ms) |
| Known exact term, tag, or title | `kg-search` CLI | ~100 ms keyword/metadata |
| Quick analysis or rewrite | `chat` (Ollama MCP) | FREE local LLM |
| Summarize/extract from a file | `read_document` (Ollama MCP) | FREE, auto-chunks large files |
| Literal string (variable, error) | Grep | Last resort |
| Specific file you already know | Read | Use `offset`/`limit` for large files |

**This applies to reasoning, not just searching** — do NOT explain what the codebase does, what patterns exist, or what was previously decided from memory alone. Look it up first.

**Use the code graph to explore code, not file reads:**
- "How does X work?" → `search_code_graph("X")` BEFORE opening files.
- "What calls function Z?" → `query_code_structure("callers", "Z")` BEFORE grep.
- Only `Read` once you know which lines you need.

**`detail` parameter** (Weaviate MCP): defaults to `"auto"` — score-driven tiering renders top hits full and marginal hits as a 6-line summary. Override with `"titles"` | `"summary"` | `"single_chunk"` | `"three_chunks"` | `"full"`. Tier thresholds: `KG_TIER_MIN`/`SINGLE_CHUNK`/`THREE_CHUNKS`/`FULL`.

---

## Hooks (always)

Hooks are bash scripts in `.claude/hooks/`; on Windows they need WSL2 to fire automatically. One-line summary by event:

| Event | What it does |
|---|---|
| `SessionStart` (startup) | Auto-start containers, display KG paths, warn on oversized `CONTEXT_STATE.md` |
| `SessionStart` (compact/resume) | Re-inject `CONTEXT_STATE.md` + recent commits + active plan + pre-compact snapshot |
| `UserPromptSubmit` | Workflow reminders + diff-based `CONTEXT_STATE.md` injection (large token savings after first prompt) |
| `PreToolUse` | Tool-usage logging; KG + code-graph search before `Edit` |
| `PostToolUse` | Auto-sync `knowledge/` → KG, `docs/` → development collection, code → code-graph queue; `py_compile` / `ruff` / `pyright` on Python; credential scan |
| `PreCompact` / `PostCompact` | Save git+files snapshot before compaction; log + notify after |
| `Stop` / `StopFailure` / `SessionEnd` | Cost tracking (`~/.claude/metrics/costs.jsonl`), desktop notifications, final cleanup |

Full event list and blocking semantics: `docs/features/03-hooks.md` (or whichever hooks doc your install ships).

---

## Scripts (always)

```bash
# Knowledge graph
.claude/scripts/kg-search   search "query" [--type TYPE] [--tags TAGS] [--limit N]
.claude/scripts/kg-search   list | recent | created [--days N]
.claude/scripts/kg-info     info "Title"
.claude/scripts/kg-info     connections "Title"
.claude/scripts/kg-sync     FILE | --all
.claude/scripts/kg-duplicates [--threshold 0.95]

# Code graph
.claude/scripts/code-graph-analyze  /path/to/repo [--project NAME] [--incremental] [--cfg] [--pdg]
.claude/scripts/code-graph-query    search "auth middleware" [--collection TYPE] [--limit N]
.claude/scripts/code-graph-query    similar "module.function" [--limit N]
.claude/scripts/code-graph-query    structure dependencies|callers|methods|extends|interactions "target"
```

Backend Python helpers (sync, maintain, temporal, vocabulary, code analysis) live next to the wrappers. PowerShell variants (`*.ps1`) ship for Windows users.

---

## Knowledge Graph (always)

**Format**: Obsidian-style Markdown with YAML frontmatter under `knowledge/`. Required keys: `title`, `type` (`concept`/`project`/`tool`/`research`/`model`/`hardware`), `tags`, `status`. Typed WikiLinks: `[[uses::Tool]]`, `[[implements::Concept]]`, `[[extends::Parent]]`, `[[buildsOn::Work]]`, `[[relatedTo::Node]]`.

**Size guidelines**: high-level <300 lines, mid-level <200, low-level <150. One node per tool/model/concept. Links unidirectional (projects → concepts).

**Write rule**: write the `.md` file directly → the `PostToolUse` hook auto-syncs to Weaviate. `store_knowledge_node` is the secondary path (used by agents that can't write files); always check `file_written: true` and `absolute_path` in the response. Pass an absolute `file_path` if a subagent might inherit a different KG collection.

**Per-project isolation**: `KG_COLLECTION` (per-project KG) and `SHARED_KG_COLLECTION` (cross-project shared, default `VibeCodedTools_KnowledgeGraph`) are set per-project via `.vscode/settings.json` `claude-code.env` and `.claude/settings.json` `env`. Per-project opt-out: `SHARED_KG_OPT_OUT=true`. The active workspace determines the active KG, not which project is being discussed.

Tag hierarchy + vocabulary: `knowledge/TAG_HIERARCHY.md`, `knowledge/VOCABULARY.md` (if shipped).

---

## Storage Systems (always)

| Store | Source | Collection | Search via |
|---|---|---|---|
| Knowledge Graph | `knowledge/*.md` | `KG_COLLECTION` | `hybrid_search`, `kg-search` |
| Shared KG | `<orchestrator>/knowledge/` | `SHARED_KG_COLLECTION` | `hybrid_search` (auto-merged) |
| Code Graph | source files | `CodeModule`/`Class`/`Function`/`API`/`Interaction` | `search_code_graph`, `query_code_structure` |
| Development docs | `docs/*.md` | `DEVELOPMENT_COLLECTION` | `hybrid_search` (auto-scoped) |
| Conversation log (optional) | chat history | `CONVERSATION_COLLECTION` | `hybrid_search` (when enabled) |

**Decision tree**: reusable pattern → KG. Code entities → code graph. Verbose project docs → development collection. Quick local analysis → Ollama MCP (FREE).

---

## Voice + Communication (always)

**Professional objectivity**
- Prioritize technical accuracy over validation.
- Challenge incorrect assumptions with evidence.
- Pattern: Challenge → Evidence → Alternative → wait for decision.

**Anti-sycophancy**
- Check actual evidence before claiming success.
- Avoid superlatives ("Great!", "Perfect!", "Beautifully!"). State facts: "X launched", not "X working perfectly".
- When uncertain, say so. Don't validate feelings — explain why.

**Specification adherence**
- Follow specs exactly. No placeholders (`... rest unchanged`, `// existing code`).
- Implement general solutions, not test-case shortcuts.
- Real-world functionality per spec > tests passing > speed.
- Good simplification (remove complexity, keep behavior) — encouraged. Lazy shortcuts (skip features, drop edge cases) — forbidden.

**When to ask vs. decide**
- Ask: architecture choices, tech selection, breaking changes, multiple plausible approaches.
- Decide autonomously: bug fixes, optimizations, refactors, docs.
- If you find yourself reasoning through multiple interpretations in your reply, STOP and ask instead.

---

## Workflow (always)

**Knowledge management**
- Proactively capture: project details, architecture, decisions, preferences, learnings.
- Create nodes in the appropriate `knowledge/` subfolder; use typed WikiLinks for implementation relationships.
- Sync via the `PostToolUse` hook or `.claude/scripts/kg-sync`.

**Update `CLAUDE.md` when**: new directories, tech-stack changes, new scripts/tools, new patterns, new KG conventions.

**Update `CONTEXT_STATE.md`**: during work, not just at the end. Mark completed subtasks (`✅`). Add discoveries, new nodes, blockers.

**Compaction**: critical context is preserved by `PreCompact`/`PostCompact` hooks. Before compacting manually, update `CONTEXT_STATE.md`. Use `/compact focus on <topic>` to guide the summary.

---

## Agents & Skills (always)

The installer drops a default set of agents into `.claude/agents/` and skills into `.claude/skills/`. Customize freely — they are templates, not framework code.

**Invoke skills**: `/skill-name` (e.g. `/architect`, `/tdd`).
**Spawn agents**: `@agent-name (Model)` via the Agent tool.

**Model selection**
- **Opus**: complex tradeoffs, security review, deep debugging — sparingly.
- **Sonnet**: implementation, planning, guidance — default.
- **Haiku**: simple tasks, calculations, tests — freely.

**When to spawn**: parallel to current work, sustained focus (30+ min), isolated context, substantial output (>200 lines). **Don't spawn**: <5 min tasks, needs immediate back-and-forth, exploring/brainstorming.

**Parallel execution**: cap at 3 parallel agents (avoids context overflow when they all return). Use `run_in_background: true` for independent work. Resume stopped agents with `SendMessage({to: agentId})` (the `resume` param on the Agent tool was removed).

**Frontmatter quick reference**
- Agents: `model`, `tools`/`disallowedTools`, `permissionMode`, `maxTurns`, `effort`, `isolation: worktree`, `background`, `memory`, `skills`, `mcpServers`, `hooks`.
- Skills: VS Code validates `name`, `description`, `argument-hint`, `disable-model-invocation`, `user-invocable`, `compatibility`, `license`, `metadata`. CLI/runtime also accepts `model`, `effort`, `allowed-tools`, `context: fork`, `agent`, `hooks` (VS Code warns but they work).

---

## Context Efficiency (always)

- Parallel tool calls: Read/Grep multiple files in a single message.
- File operations: check before reading (Grep first), use `offset`/`limit` for large files, trust writes (no re-reads).
- Spawn agents for multi-file ops; cap at 3 parallel.
- Limit shell output: `| head -30` or `2>&1 | tail -20`.
- `lean-ctx` (if installed) compresses CLI output ~90–97% transparently via `BASH_ENV`. Bypass: `LEAN_CTX_OFF=1 some-command`.

**Target metrics**: simple <5K tokens, complex <20K, full session <100K.

---

## Quick Reference (always)

- Start: read `.claude/CONTEXT_STATE.md`.
- Test: `pytest tests/`.
- Sync KG: `.claude/scripts/kg-sync --all`.
- Analyze code: `.claude/scripts/code-graph-analyze . --project "MyProject"`.
- Search code: `.claude/scripts/code-graph-query search "pattern"`.
- Search knowledge: `hybrid_search("concept")` (Weaviate MCP).
- Quick analysis (FREE): `chat("prompt", model="qwen3:0.6b")` (Ollama MCP).
- MCP venv: `source claude_mcp_servers/.venv/bin/activate`.
- Active plans: `.claude/context/plans/`.
- Plan mode: `claude --plan` or `/plan` (read-only exploration).
- Compact with focus: `/compact focus on <topic>`.
- Fix from PR: `claude --from-pr <PR-URL>`.

**Default ports**: Weaviate `8081` (HTTP) / `50052` (gRPC), Ollama `11435`, code-embed service `11440` (optional GPU).
**Default models**: text embeddings `qwen3-embedding:0.6b` (1024-dim), code embeddings CodeSage-Large-v2 (2048-dim, GPU) / qwen3-embedding (CPU fallback), inference `qwen3:0.6b` / `qwen3:latest`.

---

<!-- BEGIN: SETUP-ONLY (remove after first successful session) -->
## First-Run Setup

If you're seeing this orchestrator for the first time, here's the 60-second mental model:

- **What it is**: an infrastructure layer for Claude Code that adds persistent memory (KG), semantic code search (code graph), and ~20 hooks that run automatically. You keep using Claude Code the way you already do; the orchestrator works in the background via hooks and MCP servers.
- **What runs locally**: Weaviate (vector DB) on `:8081`, Ollama (local LLM + embeddings) on `:11435`. Both started automatically by the `SessionStart` hook if not already running.
- **Free vs Pro**: this repo is fully functional standalone. Optional paid modules (RL retrieval reranking, MAO multi-agent runtime, specialist agent packs) activate only when a license key is present. Without a key, retrieval falls back to cosine ordering — nothing breaks.

If install hasn't run yet (or you're not sure):

```bash
./install.sh           # Linux / macOS
.\install.ps1          # Windows
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
