# Agents, Skills & Hooks

The Claude Code automation surface: 45 bundled agents, 53 skills, and 31 hooks (most wired in default `.claude/settings.json`; some available but not wired). Templates in `templates/agents/` and `templates/skills/`; hooks in `.claude/hooks/`, registered in `.claude/settings.json`.

For the MCP servers that agents use → see [02-mcps-and-agents.md](02-mcps-and-agents.md).

---

## Bundled Agents (`templates/agents/free/`)

Free agents install to `~/.claude/agents/` via `install.py --with-agents` (default-on). Each agent is a single `.md` file with YAML frontmatter: `name`, `description`, `model` (required), plus optional `tools`, `effort`, `isolation`, `skills`, `mcpServers`. The 45 agents below split roughly into builders (write code), researchers (read & report), and lifecycle helpers (install / migrate / bootstrap).

### `coder` (Sonnet, `isolation: worktree`)
Writes code from a spec, following patterns from the KG. Runs in git worktree isolation by default.

<details>
<summary>Details</summary>

Tools: Read, Write, Edit, Grep, Glob, Bash. MCP: `orchestrator-tools` (injected at `{{ORCHESTRATOR_ROOT}}`). Worktree isolation puts changes on a throwaway branch until reviewed — a half-finished implementation can't dirty the working directory. The `{{ORCHESTRATOR_ROOT}}` template variable is rewritten to an absolute path at install time by `install.py`.

</details>

### `planner` (Sonnet)
Requirements analysis, architectural design, and task breakdown. Injects `task-breakdown` and `architect` skills.

### `tester` (Sonnet)
Test creation, verification, and bug investigation. Injects `code-review-expert` skill. MCP: `orchestrator-tools`.

### `code-explorer` (Haiku, `effort: low`)
Read-heavy research agent that can also write findings reports.

<details>
<summary>Details</summary>

Tools: Read, Glob, Grep, Bash, Write, Edit. Unlike the built-in Explore agent (read-only), `code-explorer` can save findings to `.claude/context/`, `docs/`, or `knowledge/`. Use for audits, gap analyses, pattern inventories. Write scope is enforced by convention in the agent's system prompt rather than tool restriction. Haiku keeps cost low for scan-heavy tasks.

</details>

### `code-migrator` (Sonnet, `isolation: worktree`)
Migrate code between languages, frameworks, or versions. Injects `architecture-consultant` skill.

### `helper-scripter` (Haiku, `isolation: worktree`)
Create hooks, scripts, agents, and skills. Self-improves the automation system.

### `doc-extractor` (Sonnet)
Pulls knowledge out of scattered docs and into KG nodes. Frontmatter declares an agent-scoped `PreToolUse Write|Edit` hook pointing at `.claude/scripts/validate-readonly.sh`; the script doesn't ship in v0.1.0, so the read-only enforcement is convention-only at the moment. Tracked as a code-doc gap.

### `doc-maintainer` (Sonnet)
Keeps documentation current and prunes stale material — but always extracts to the KG before archival, so context isn't lost when files are removed.

### `doc-organizer` (Sonnet)
Detects/merges duplicates, moves loose files, archives old docs, maintains the doc tree. Does not write new documentation — only organizes what exists.

### `graph-health-checker` (Haiku, `effort: low`)
Validates KG and code-graph integrity: orphaned nodes, broken links, missing metadata. Background maintenance trigger.

### `knowledge-curator` (Haiku, `effort: low`)
Extracts WikiLink relationships from KG nodes and updates Weaviate cross-references. Background maintenance.

### `kg-navigator` (Sonnet)
Searches and explores the KG, surfaces relevant patterns before implementation, flags gaps. Read-only (Read, Grep, Bash only).

### `code-graph-updater` (Haiku, `effort: low`)
Incremental code-graph updates when files change. Background maintenance trigger.

### `gui-tester` (Sonnet, explicit model: `claude-sonnet-4-6`)
Automated GUI testing through Playwright MCP: navigate, screenshot, click, type, evaluate. Produces structured reports on layout, functionality, and regressions.

<details>
<summary>Details</summary>

Tools: restricted to Playwright MCP tools only (`mcp__playwright__browser_*`). Requires the Playwright MCP to be connected in `~/.claude.json`. Used for visual regression testing, frontend bug reproduction, and automated UI verification.

</details>

### `web-explorer` (Haiku, `effort: low`)
The web counterpart to `code-explorer`: searches, reads pages, cross-references with local files, writes a single markdown report.

### `prompt-engineer` (Sonnet, `effort: low`)
Reviews and rewrites agent prompts using current Claude 4.x patterns.

### `orchestrator-installer` (Opus)
Installs the VibeCoded Orchestrator workflow system on a new machine (Windows or Linux). Full shared-infrastructure setup.

### `project-bootstrapper` (Sonnet)
Creates a new Claude Code project from scratch with the full Orchestrator workflow: hooks, KG, scripts, settings, CLAUDE.md.

### `project-migrator` (Sonnet)
Migrates an existing Claude Code project onto the Orchestrator workflow.

---

### `expert-coder` (Opus, `isolation: worktree`)
Complex implementation work that needs cross-layer architectural reasoning, security analysis, or multi-layer debugging. Use sparingly — Sonnet handles most implementations fine and costs less.

### `project-architect` (Sonnet)
End-to-end project design: requirements, architecture, implementation plan. Injects `architect` + `architecture-consultant` skills.

### `ai-agentic-architect` (Sonnet)
Designs multi-agent systems and agentic workflows with coordination strategies. Injects `architect` + `task-breakdown` skills.

### `project-coordinator` (Sonnet, `effort: low`)
Coordinates multi-agent workflows, tracks progress, manages blackboard task assignment.

### `project-organizer` (Sonnet, `effort: low`)
Keeps the project tidy over time and captures cross-project patterns for reuse.

### `backend-specialist` (Sonnet, `isolation: worktree`, `effort: low`)
APIs, services, databases, business logic. Injects `api-designer` + `database-advisor` skills.

### `frontend-specialist` (Sonnet, `isolation: worktree`, `effort: low`)
React/Vue/Svelte components, forms, routing. Injects `react-patterns` + `accessibility-checker` skills.

### `gui-expert` (Sonnet, `effort: low`)
Designs and implements Gradio web applications with WCAG 2.1 AA compliance.

### `ai-llm-expert` (Sonnet, `effort: low`)
LLM integration work: prompt engineering, context management, multi-model routing, cost optimization. Injects `ai-prompting` + `ai-model-selector` skills.

### `deep-researcher` (Sonnet, `effort: low`)
Multi-level web research: spawns recursive sub-agents to chase down branches without losing the parent thread.

---

## Worktree Isolation

Agents with `isolation: worktree` run in a temporary git worktree (isolated branch). No changes → worktree is auto-cleaned. Changes → worktree path + branch name returned for review/merge. Prevents partial implementations from corrupting the working directory. See `templates/agents/WORKTREE_ISOLATION_GUIDE.md`.

---

## `orchestrator-tools` MCP (referenced in templates)

Agent frontmatter `mcpServers: orchestrator-tools` references `{{ORCHESTRATOR_ROOT}}/claude_mcp_servers/orchestrator_tools_mcp/server.py`. The implementation is not present in the OSS bundle (paid module). Free-tier agents use `weaviate-kg` and `search` MCPs directly. The `ollama` MCP was removed in v0.2.11.

### Graceful degradation behaviour
Seven agents reference `orchestrator-tools` in their frontmatter as of v0.1.0: `coder`, `tester`, `planner`, `expert-coder`, `project-architect`, `project-coordinator`, `ai-agentic-architect`. The OSS bundle does not ship this MCP server. Claude Code silently ignores MCP entries it cannot find on disk, so the agents install and start cleanly — calls to `orchestrator-tools` tools fail at runtime, not at install time.

---

## Bundled Skills (`templates/skills/`)

Skills are smaller and lighter than agents — they're injected into context as a single `SKILL.md` file rather than spawning a fresh process. Invoke directly via `/skill-name`, or list them in an agent's `skills:` frontmatter. Install to `~/.claude/skills/` via `install.py --with-skills` (default-on). 53 skills, organized across multiple model tiers (Opus for deep reasoning, Sonnet for implementation guidance, Haiku for quick checks).

### Opus-tier skills (deep reasoning)

**`architect`** — Design complex system architectures, evaluate tradeoffs, make critical technical decisions. (`templates/skills/architect/SKILL.md`)

**`architecture-consultant`** — Cross-domain architecture decisions: technology selection, infrastructure design, long-term tradeoff analysis. (`templates/skills/architecture-consultant/SKILL.md`)

**`code-review-expert`** — Deep code analysis: subtle bugs, security issues, performance problems, architectural concerns. (`templates/skills/code-review-expert/SKILL.md`)

**`debug-expert`** — Investigate complex bugs, intermittent failures, performance degradations across multiple components. (`templates/skills/debug-expert/SKILL.md`)

**`security-reviewer`** — Cross-layer security analysis: frontend XSS/CSRF, backend injection, AI prompt injection, infrastructure. (`templates/skills/security-reviewer/SKILL.md`)

### Sonnet-tier skills

**`ai-model-selector`** — Quick guidance on choosing AI models (LLM/VLM/Embedding) based on task, VRAM, cost, quality. (`templates/skills/ai-model-selector/SKILL.md`)

**`ai-rag-advisor`** — RAG system design: chunking strategies, embedding selection, retrieval methods, vector DB choices. (`templates/skills/ai-rag-advisor/SKILL.md`)

**`api-designer`** — API design guidance: REST vs GraphQL vs gRPC, endpoint patterns, auth strategies, versioning. (`templates/skills/api-designer/SKILL.md`)

**`database-advisor`** — Database design, schema optimization, query performance, technology selection. (`templates/skills/database-advisor/SKILL.md`)

**`deployment-advisor`** — Deployment strategy: platform selection, CI/CD pipeline design, environment config, monitoring. Includes example CI/CD workflows and platform comparison docs. (`templates/skills/deployment-advisor/SKILL.md`)

**`explore-codebase`** — Systematic codebase onboarding: structure, architecture, key data models, entry points, auth patterns. Argument hint: `[project-path-or-question]`. (`templates/skills/explore-codebase/SKILL.md`)

**`extract-docs`** — Systematically extract knowledge from scattered documentation to prevent catastrophic forgetting. Creates structured extraction reports with status tags. Argument hint: `[source-path-or-pattern]`. (`templates/skills/extract-docs/SKILL.md`)

**`fix-issue`** — Investigate and fix a GitHub issue or bug: read, reproduce, root-cause, implement fix, add regression test. Argument hint: `[issue-url-or-description]`. (`templates/skills/fix-issue/SKILL.md`)

**`gui-test`** — Automated visual testing with Playwright MCP across multiple reviewer perspectives. (`templates/skills/gui-test/SKILL.md`)

**`gui-ux-expert`** — Quick GUI/UX/UI design consultations and recommendations. (`templates/skills/gui-ux-expert/SKILL.md`)

**`interview`** — Interview the user via `AskUserQuestion` to discover requirements for a feature or task. Writes final spec to `SPEC.md`. Argument hint: `[feature-or-task-description]`. (`templates/skills/interview/SKILL.md`)

**`kg-research`** — Research using ONLY knowledge graph semantic search — no file tools, forces KG-first approach. Argument hint: `[search-query]`. (`templates/skills/kg-research/SKILL.md`)

**`performance-optimizer`** — Cross-domain performance analysis: frontend render, backend queries, AI model inference. Includes optimization checklist and pattern examples. (`templates/skills/performance-optimizer/SKILL.md`)

**`react-patterns`** — React best practices: component patterns, state management selection, performance optimization, testing strategies. Includes component pattern, performance, and state management examples. (`templates/skills/react-patterns/SKILL.md`)

**`task-breakdown`** — Break complex features into implementable tasks with estimates, dependencies, and risk matrix. Includes dependency patterns, estimation methods, and risk matrix examples. (`templates/skills/task-breakdown/SKILL.md`)

**`tdd`** — Test-Driven Development workflow: write failing test first, implement to pass. Argument hint: `[feature-or-bug-description]`. (`templates/skills/tdd/SKILL.md`)

**`workflow-maintain`** — Analyze project workflow setup and suggest/create needed automation for hooks, scripts, skills, agents. (`templates/skills/workflow-maintain/SKILL.md`)

### Haiku-tier skills (cheap/fast)

**`accessibility-checker`** — Quick A11y review: WCAG 2.1 checklist, screen reader compatibility, keyboard navigation, color contrast. Includes contrast check script and examples. (`templates/skills/accessibility-checker/SKILL.md`)

**`ai-prompting`** — Prompt engineering tips and templates: few-shot, chain-of-thought, constraint specification, output formatting. (`templates/skills/ai-prompting/SKILL.md`)

**`context`** — Efficient context state inspection, task lifecycle management, session tracking. (`templates/skills/context/SKILL.md`)

**`context-compress`** — Guide for `/compact` with the pre-compact save pipeline. Documents what gets saved and reinjected. Argument hint: `[focus-topic]`. (`templates/skills/context-compress/SKILL.md`)

**`doc-template`** — Documentation templates: README, API docs, ADRs, user guides. Includes README template. (`templates/skills/doc-template/SKILL.md`)

**`hardware-calculator`** — Quick VRAM/RAM calculations, hardware recommendations, AI model feasibility checks. (`templates/skills/hardware-calculator/SKILL.md`)

---

## Hooks (`.claude/hooks/`)

31 shell scripts (with `.ps1` Windows siblings) that fire at well-defined points in the Claude Code lifecycle (`SessionStart`, `PreToolUse`, `PostToolUse`, `Stop`, etc.). Most are wired in the default `.claude/settings.json`; `code-graph-incremental.sh` ships but is not wired. Two project-wide invariants: every hook checks `VCT_DISABLE_HOOKS=1` as its first action (so you can disable all automation in one shell), and every hook scrubs `SUPABASE_KEY`, `GITHUB_TOKEN`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, AWS credentials, and similar before spawning any subprocess.

Hook input contract (PR #176, 2026-05): hooks receive their event payload as JSON on stdin per Claude Code v2.1.x spec. `session_id`, `tool_name`, and other fields are read from stdin via `python -c 'import json,sys; d=json.loads(sys.stdin.read()); ...'`. Positional args (`$1`, `$2`) are present for backward compatibility but are empty when invoked by Claude Code v2.1.x.

### `ensure-containers.sh` — SessionStart (startup, background)
Auto-start required containers (Weaviate, Ollama, code embedding service) if stopped.

<details>
<summary>Details</summary>

Non-blocking (background). Container names configurable via `VCT_REQUIRED_CONTAINERS` (space-separated). Container runtime auto-detected (podman preferred, falls back to docker). Compose dir resolves relative to hook location; override with `VCT_COMPOSE_DIR`. Uses `flock` to prevent race conditions when multiple sessions start simultaneously.

</details>

### `ensure-code-embed-service.sh` — SessionStart (startup, background)
Auto-start the code embedding service container if it exists and is stopped. Silent no-op when the container doesn't exist (CPU-only users who haven't enabled `code_embed` in `compose.yaml`).

### `session-start-kg-loader.sh` — SessionStart (startup, blocking)
Display KG resource paths at session start. Optionally launches the RL server if installed (Pro tier; silent no-op when absent).

### `context-size-check.sh` — SessionStart (startup, blocking)
Warn if `CONTEXT_STATE.md` exceeds configured thresholds (warn: 300 lines, max: 400 lines). Prompts to run `doc-maintainer` agent to trim.

### `compact-context-reinject.sh` — SessionStart (compact)
Re-inject `CONTEXT_STATE.md`, recent git commits, active plan, and pre-compact snapshot after context compaction.

<details>
<summary>Details</summary>

Uses LITM (Lost-In-The-Middle) ordering: most critical content placed at both start and end of injected context since Claude weights context endpoints most heavily. Total budget: ~250 lines. `CONTEXT_STATE.md` gets full allocation; other sections are capped. Detects compact session via `compact` matcher in `settings.json`.

</details>

### `pre-compact-save.sh` — PreCompact (auto)
Save a snapshot of current working state before auto-compaction: git status (top 30 files), recently changed files, current open file list. Output written to `.claude/context/pre-compact-snapshot.md`.

### `post-compact.sh` — PostCompact
Log compaction event and send desktop notification after compaction completes. Appends to `~/.claude/metrics/compactions.jsonl`. Resets the `diff-context-inject.sh` baseline.

### `user-prompt-submit-reminder.sh` — UserPromptSubmit (blocking)
Inject workflow reminders (update CONTEXT_STATE.md, check KG-first policy) when session output volume suggests substantial work has occurred.

### `diff-context-inject.sh` — UserPromptSubmit (blocking)
Only inject the changed sections of `CONTEXT_STATE.md` rather than the full file on every prompt.

<details>
<summary>Details</summary>

First prompt: creates a baseline snapshot. Subsequent prompts: diffs against snapshot and outputs only changed sections (or nothing if unchanged). After compaction: resets baseline. Provides ~70-90% token savings compared to always re-injecting the full context file. Session ID is used to isolate snapshots across concurrent Claude sessions.

</details>

### `pre-tool-use.sh` — PreToolUse `*` (all tools, blocking)
Security enforcement + tool call logging + file backup.

<details>
<summary>Details</summary>

Four actions:
1. **SSRF guard**: blocks WebFetch/fetch_page requests to private IP ranges.
2. **Shell injection scan**: detects network-fetch-to-shell patterns (`curl | bash`, etc.).
3. **Tool logging**: appends tool call events to `.claude/logs/YYYY-MM-DD_tool_usage.jsonl` (TOUCAN dataset format).
4. **Build Anchor Protocol**: tracks files read this session; blocks Write/Edit on files not yet read (prevents clobber). Creates a backup of existing files before Write/Edit in `/tmp/.claude_backups/`.

Exit 2 blocks the tool call. Exit 0 allows it. Security events logged to `.claude/logs/security_events.jsonl`.

</details>

### `pre-edit-context-inject.sh` — PreToolUse Edit (blocking)
Inject KG + code graph context for the file being edited before the Edit executes.

<details>
<summary>Details</summary>

Fires only for the `Edit` tool (not `Write` — new files have less prior context value). Runs KG semantic search on the file path and injects relevant nodes as additional context. Must complete within 8 seconds. Never exits 2 — always allows the edit to proceed. Cache warms after first run; subsequent calls for the same file are ~31ms.

</details>

### `post-file-edit.sh` — PostToolUse Edit|Write (background)
Auto-sync edited files to the appropriate Weaviate collection based on path.

<details>
<summary>Details</summary>

Path-based routing:
- `knowledge/**/*.md` → KG collection sync via `sync_knowledge_graph.py`
- `docs/**/*.md` → Development collection sync
- Code files (`.py`, `.ts`, `.js`, etc.) → Code graph update queue

Also: reminds to update project expert when `CONTEXT_STATE.md` changes substantially; suggests workflow optimization when agents/skills/hooks are modified.

</details>

### `kg-summary-generator.sh` — PostToolUse Edit/Write(knowledge/**) + store_knowledge_node
Spawns a background Haiku agent to generate/update summary descriptions for KG nodes after edits. Content-hash dedup: skips regeneration if node content unchanged. Summaries written to `knowledge/.node_formats.json`.

### `post-git-commit-kg-sync.sh` — PostToolUse Bash(git commit *)
Spawn a background Haiku agent to review the commit diff and update relevant KG nodes and docs. Non-blocking. Guards with `CLAUDE_CODE_DISABLE_AUTO_MEMORY` to prevent infinite recursion inside agent subprocesses.

### `post-tool-security.sh` — PostToolUse Edit|Write (background)
Scan written files for accidentally included credentials. Non-blocking; alerts logged to `.claude/logs/credential_alerts.jsonl` with desktop notification.

### `config-change-audit.sh` — ConfigChange (background)
Log all settings.json changes to `.claude/logs/config_changes.jsonl` for audit trail.

### `cost-tracker.sh` — Stop (background)
Parse the Stop event payload and append `{timestamp, session_id, model, input_tokens, output_tokens, cache_read_tokens, auth_mode, cost_usd}` to `~/.claude/metrics/costs.jsonl`. `auth_mode` is `"subscription"` (OAuth login — `cost_usd: null`, tokens free) or `"api"` (API key — cost calculated from token counts).

### `notify-stop.sh` — Stop (background)
Send a desktop notification (`notify-send`) when Claude finishes a response. Note: Stop hooks do not fire in the VS Code extension (CLI/Desktop only).

### `stop-failure-notify.sh` — StopFailure (background)
Send an urgent desktop notification when a turn fails (rate limit, auth error, etc.) and log to `~/.claude/metrics/failures.jsonl`.

### `kg-update-nudge.sh` — UserPromptSubmit + Stop (background)
Counts substantive work tokens since the last KG node write; nudges to write a KG node when the threshold (~150k tokens) is exceeded. Bypass with `KG_NUDGE_OFF=1`.

### `verify-container-ports.sh` — SessionStart (startup, background)
Verifies that the Weaviate / Ollama / code-embed container ports are bound and reachable. Logs to `.claude/logs/container_port_check.jsonl`. Non-blocking.

### `pre-vercel-token-guard.sh` — PreToolUse Bash (blocking)
Blocks `vercel ... --token=...` invocations because the Vercel CLI echoes the token back in the `next:` block of stdout, leaking it into tool output. Forces use of `VERCEL_TOKEN` env var instead. Exit 2 on `--token=` match.

### `code-graph-incremental.sh` — (available, not wired in default settings.json)
Incremental code graph analysis on every code file edit. Auto-detects the project from the edited file path and supports Joern CFG/PDG extraction when Joern is on PATH.

### `agent-skill-keyword-suggest.sh` — UserPromptSubmit (blocking)
Scans the user prompt for keywords declared in agents'/skills' `keywords:` frontmatter and injects a short suggestion as additionalContext. Globs `.claude/agents/*.md` and `.claude/skills/*/SKILL.md` — disabled items live in sibling `.disabled/` directories and naturally fall outside the glob.

### `subagent-start-suggest.sh` — SubagentStart
Spawn-time mirror of `agent-skill-keyword-suggest.sh`: injects agent/skill suggestions into a freshly-spawned subagent's context so the subagent knows which skills/agents are relevant for its task.

### `session-start-ensure-hub.sh` — SessionStart (startup, background)
Ensure the `vct-hub` resolver service is running by invoking `vct-hub --start-if-not-running`. Idempotent. Soft-fails throughout — never blocks startup.

### `check-no-fork-bomb.sh` — defense-in-depth detector
Counts running `lean-ctx` processes and warns if the threshold is exceeded. Backstop for the historical BASH_ENV lean-ctx fork-bomb pattern (now mitigated by design in v0.2.11+).

### `lean-ctx-rewrite.sh` — PreToolUse Bash
Per-project lean-ctx PreToolUse hook for Bash tool calls. Wraps the user-issued command in `lean-ctx -c '...'` so output is compressed before it returns to Claude. Auto-detects `lean-ctx` commands and steps aside to prevent recursion.

### `embedding-failures-surface.sh` — context injection
Surfaces embedding-backend failure hints written by `vco_lib/embedding_service.py` to Claude. When no embedding backend is reachable, the service drops a hint file; this hook injects its contents so Claude can diagnose / recover.

### `pre-diagram-path-validation.sh` — PreToolUse (Write/Edit + Bash)
Defense-in-depth guard for diagrams integration. Rejects `.mmd` / `.excalidraw` writes outside `.claude/diagrams/` to keep the diagram index consistent.

### `post-file-delete.sh` — PostToolUse Bash
Detects deletes of `.mmd` / `.excalidraw` files under `.claude/diagrams/` and cascades the delete across SQLite + sidecar + Weaviate via `vco_lib.diagram_indexer drop <file>`. Matches `rm` / `unlink` / `mv` / PowerShell `Remove-Item` / `Move-Item`.

---

## Composition Patterns

### Agent → Skill injection
The `skills:` list in agent frontmatter injects skill `SKILL.md` files into the agent's context window before it runs. This provides the agent with specialist knowledge and decision frameworks without changing its tool permissions. Example: `planner` injects `task-breakdown` (Sonnet) and `architect` (Opus) — Opus-level reasoning is available as a reference even though the planner itself runs on Sonnet.

### Blackboard coordination
The `project-coordinator` agent implements a blackboard pattern: agents volunteer for tasks from a shared `CONTEXT_STATE.md` rather than receiving delegated assignments. This pattern (documented in `knowledge/concepts/blackboard-architecture-coordination.md`) reduces inter-agent communication overhead and supports parallelism without a central scheduler.

### Hook → Agent delegation
Several hooks spawn background Claude Code agents for heavyweight tasks: `kg-summary-generator.sh` → Haiku agent to update KG summaries; `post-git-commit-kg-sync.sh` → Haiku agent to sync KG after commits. All delegating hooks guard with `CLAUDE_CODE_DISABLE_AUTO_MEMORY` to prevent infinite recursion inside subprocesses.

### `VCT_DISABLE_HOOKS=1` escape hatch
Set in your shell (or in `.claude/settings.json` under `env` — the canonical per-project env channel since v0.2.12 / PR-27) to skip all hooks for that session. Every hook checks this variable as its first act.
