# Agents, Skills & Hooks

The Claude Code automation surface: 44 bundled agents, 54 skills, and 46 hooks (44 event-registered in the default `.claude/settings.json`; 2 ship **unregistered and uninvoked**, kept for users who want to wire them themselves — `kg-sync-on-edit.sh`, superseded by `post-file-edit.sh`'s auto-sync, and `code-graph-incremental.sh`, whose scheduling moved to `stop-codegraph-drain.sh`. Neither is dead code; both run standalone). Templates in `templates/agents/` and `templates/skills/`; hooks in `.claude/hooks/`, registered in `.claude/settings.json`.

For the MCP servers that agents use → see [02-mcps-and-agents.md](02-mcps-and-agents.md).

---

## Bundled Agents (`templates/agents/free/`)

Free agents install to `~/.claude/agents/` via `install.py --with-agents` (default-on). Each agent is a single `.md` file with YAML frontmatter: `name`, `description`, `model` (required), plus optional `tools`, `effort`, `isolation`, `skills`, `mcpServers`. The 44 agents split roughly into builders (write code), researchers (read & report), and lifecycle helpers (install / bootstrap-refinement); the list below describes the most-used of them, not all 44 — `ls templates/agents/free/` is the complete set. The `project-migrator` agent was archived in v0.2.54 to `templates/agents/_archive/` — `install.py --add-project` and the launcher GUI's "+ Existing Project" tab now handle that flow automatically.

**Every bundled agent declares `effort: medium`** (v0.2.97 — they all declared `high` in v0.2.96, and ten of them `xhigh` before that). Frontmatter effort *overrides* the session's level and cannot be overridden from the Agent tool, so the value a shipped agent pins is the value you get. `medium` is the shipped default; the few agents in genuinely hard-reasoning roles (deep research, incident response, adversarial review) are kept at `high`, which is the ceiling — `xhigh` and `max` are not used for subagents at all, both for the cost and because `xhigh` is rejected outright by models that do not expose extended thinking, making the agent fail to start rather than run more carefully. Raise it per-agent if you want it; the frontmatter still accepts `xhigh` and `max`, on a model you know supports them.

**Module-scoped agents.** Bundled agents used to ship to every project unconditionally. Since v0.2.96 a project with the **model gateway** module active additionally receives `templates/agents/module-gateway/` — `glm-implementer`, `glm-reviewer`, `glm-planner` and `glm-flash-researcher` — on its next bundle update. These are the first shipped artefacts delivered on a per-module condition: a project without the module active receives none of them, and neither does a launcher-less CLI install, because the module state lives in `launcher.db`. They are deliberately outside the 44 free-agent count, which measures `templates/agents/free/` alone. Spawn them by definition **name** with no model override — the Agent tool's model list cannot carry `claude-gw/*` ids, while an agent's own frontmatter can. (`glm-flash-reviewer` was retired in the same release: flash is kept for research and investigation, review moved to `glm-5.3`. An already-delivered copy is orphan-cleaned on the next bundle update if unmodified, and preserved with a deferral entry if you edited it.)

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

### `code-explorer` (Haiku)
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
Pulls knowledge out of scattered docs and into KG nodes. Read-only on its sources by instruction, not enforcement: the agent's frontmatter declares no hook, and its tool list includes `Write`/`Edit` for the extraction reports it produces.

### `doc-maintainer` (Sonnet)
Keeps documentation current and prunes stale material — but always extracts to the KG before archival, so context isn't lost when files are removed.

### `doc-organizer` (Sonnet)
Detects/merges duplicates, moves loose files, archives old docs, maintains the doc tree. Does not write new documentation — only organizes what exists.

### `graph-health-checker` (Haiku)
Validates KG and code-graph integrity: orphaned nodes, broken links, missing metadata. Background maintenance trigger.

### `knowledge-curator` (Haiku)
Extracts WikiLink relationships from KG nodes and updates Weaviate cross-references. Background maintenance.

### `kg-navigator` (Sonnet)
Searches and explores the KG, surfaces relevant patterns before implementation, flags gaps. Read-only (Read, Grep, Bash only).

### `code-graph-updater` (Haiku)
Incremental code-graph updates when files change. Background maintenance trigger.

### `gui-tester` (Sonnet, explicit model: `claude-sonnet-4-6`)
Automated GUI testing through Playwright MCP: navigate, screenshot, click, type, evaluate. Produces structured reports on layout, functionality, and regressions.

<details>
<summary>Details</summary>

Tools: restricted to Playwright MCP tools only (`mcp__playwright__browser_*`). Requires the Playwright MCP to be connected in `~/.claude.json`. Used for visual regression testing, frontend bug reproduction, and automated UI verification.

</details>

### `web-explorer` (Haiku)
The web counterpart to `code-explorer`: searches, reads pages, cross-references with local files, writes a single markdown report.

### `prompt-engineer` (Sonnet)
Reviews and rewrites agent prompts using current Claude 4.x patterns.

### `orchestrator-installer` (Opus)
Diagnose and recover from a partially-failed install. Canonical install path: `bash first-install.sh` → `install.py`.

### `project-bootstrapper` (Sonnet)
Refine bootstrap docs (CLAUDE.md, ARCHITECTURE.md) after the launcher's "+ New/Existing Project" flow generates them.

---

### `expert-coder` (Opus, `isolation: worktree`)
Complex implementation work that needs cross-layer architectural reasoning, security analysis, or multi-layer debugging. Use sparingly — Sonnet handles most implementations fine and costs less.

### `project-architect` (Sonnet)
End-to-end project design: requirements, architecture, implementation plan. Injects `architect` + `architecture-consultant` skills.

### `ai-agentic-architect` (Sonnet)
Designs multi-agent systems and agentic workflows with coordination strategies. Injects `architect` + `task-breakdown` skills.

### `project-coordinator` (Sonnet)
Coordinates multi-agent workflows, tracks progress, manages blackboard task assignment.

### `project-organizer` (Sonnet)
Keeps the project tidy over time and captures cross-project patterns for reuse.

### `backend-specialist` (Sonnet, `isolation: worktree`)
APIs, services, databases, business logic. Injects `api-designer` + `database-advisor` skills.

### `frontend-specialist` (Sonnet, `isolation: worktree`)
React/Vue/Svelte components, forms, routing. Injects `react-patterns` + `accessibility-checker` skills.

### `gui-expert` (Sonnet)
Designs and implements Gradio web applications with WCAG 2.1 AA compliance.

### `ai-llm-expert` (Sonnet)
LLM integration work: prompt engineering, context management, multi-model routing, cost optimization. Injects `ai-prompting` + `ai-model-selector` skills.

### `deep-researcher` (Sonnet)
Multi-level web research: spawns recursive sub-agents to chase down branches without losing the parent thread.

---

## Worktree Isolation

Agents with `isolation: worktree` run in a temporary git worktree (isolated branch). No changes → worktree is auto-cleaned. Changes → worktree path + branch name returned for review/merge. Prevents partial implementations from corrupting the working directory. See `templates/agents/WORKTREE_ISOLATION_GUIDE.md`.

---

## `orchestrator-tools` MCP (referenced in templates)

Agent frontmatter `mcpServers: orchestrator-tools` references `{{ORCHESTRATOR_ROOT}}/claude_mcp_servers/orchestrator_tools_mcp/server.py`. The implementation is not present in the OSS bundle (paid module). Free-tier agents use `weaviate-kg` and `search` MCPs directly.

### Graceful degradation behaviour
Seven free agents reference `orchestrator-tools` in their frontmatter: `coder`, `tester`, `planner`, `expert-coder`, `project-architect`, `consulting-cto-portfolio-coordinator`, `ai-agentic-architect`. The OSS bundle does not ship this MCP server. Claude Code silently ignores MCP entries it cannot find on disk, so the agents install and start cleanly — calls to `orchestrator-tools` tools fail at runtime, not at install time.

---

## Bundled Skills (`templates/skills/`)

Skills are smaller and lighter than agents — they're injected into context as a single `SKILL.md` file rather than spawning a fresh process. Invoke directly via `/skill-name`, or list them in an agent's `skills:` frontmatter. Install to `~/.claude/skills/` via `install.py --with-skills` (default-on). 54 skills, organized across multiple model tiers (Opus for deep reasoning, Sonnet for implementation guidance, Haiku for quick checks).

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

46 shell scripts (with `.ps1` Windows siblings) that fire at well-defined points in the Claude Code lifecycle (`SessionStart`, `PreToolUse`, `PostToolUse`, `Stop`, etc.). 44 are wired in the default `.claude/settings.json`; two ship unwired and are invoked by nothing — `code-graph-incremental.sh`, whose scheduling moved to `stop-codegraph-drain.sh` (that hook mirrors its analyzer/venv resolution and calls the analyzer itself rather than calling the hook), and `kg-sync-on-edit.sh`, an opt-in single-purpose hook superseded by `post-file-edit.sh`'s auto-sync (see the note at the end of this section). Both run standalone, for users who want to wire them in their own settings. Two project-wide invariants: every hook checks `VCT_DISABLE_HOOKS=1` as its first action (so you can disable all automation in one shell), and every hook scrubs `SUPABASE_KEY`, `GITHUB_TOKEN`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, AWS credentials, and similar before spawning any subprocess.

Hook input contract (PR #176, 2026-05): hooks receive their event payload as JSON on stdin per Claude Code v2.1.x spec. `session_id`, `tool_name`, and other fields are read from stdin via `python -c 'import json,sys; d=json.loads(sys.stdin.read()); ...'`. Positional args (`$1`, `$2`) are present for backward compatibility but are empty when invoked by Claude Code v2.1.x.

### `ensure-containers.sh` — SessionStart (startup, background)
Auto-start required containers (Weaviate, Ollama, code embedding service) if stopped.

<details>
<summary>Details</summary>

Non-blocking (background). Container names configurable via `VCT_REQUIRED_CONTAINERS` (space-separated). Container runtime chosen by the one rule (`python -m vco_lib.containers resolve`): `VCT_CONTAINER_RUNTIME` if set, else the runtime the install recorded in `state/install/runtime.txt`, else auto-detection (podman first); a pinned runtime that is down is refused, never swapped. When the record names a runtime that is no longer installed and the other one holds VCO's data, the hook uses that one and says so in one line (the resolver's internal `VCO_RUNTIME_RECONCILED` flag — do not set it); the next update re-records it. Compose dir resolves relative to hook location; override with `VCT_COMPOSE_DIR` (a directory already containing the compose file) or `VCT_INFRASTRUCTURE_DIR` (an `infrastructure/` directory used as the compose dir). Its reconcile → plan → act runs under a per-user session lock (`flock`, taken by `python -m vco_lib.service_lifecycle with-session-lock`; the `.ps1` sibling holds the same lock file), shared with `verify-container-ports` and with sessions that start at the same moment, and the session reconcile runs at most once a minute across them (v0.2.97). That locked part runs DETACHED (`service_lifecycle run-detached`, which sets the internal `VCO_SESSION_DETACHED` — do not set it): the hook relays its output for a budget inside its registered timeout and then returns, while the detached run keeps the lock until its `compose up` has really ended. A hook timeout therefore never releases the lock under running work, and the lock holder forwards SIGTERM/SIGINT/SIGHUP to the work's process group and waits for it before releasing the lock. When the work outlives the budget, the hook says so and names its log under `<VCT state dir>/logs/session-hooks/`.

</details>

### `ensure-code-embed-service.sh` — SessionStart (startup, background)
Auto-start the code embedding service container if it exists and is stopped. Silent no-op when the container doesn't exist (CPU-only users who haven't enabled `code_embed` in `compose.yaml`). The container it inspects/starts is named `code_embed` by default — override with `VCT_CODE_EMBED_CONTAINER` if yours is named differently; the port probe follows `CODE_EMBED_PORT` (default `11440`).

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
Security enforcement + file backup + KG suggestion.

<details>
<summary>Details</summary>

Three actions:
1. **SSRF guard**: blocks WebFetch/fetch_page requests to private IP ranges.
2. **Shell injection scan**: detects network-fetch-to-shell patterns (`curl | bash`, etc.).
3. **Build Anchor Protocol**: tracks files read this session; blocks Write/Edit on files not yet read (prevents clobber). Creates a backup of existing files before Write/Edit in `.claude/state/tool_backups/`.

Exit 2 blocks the tool call. Exit 0 allows it. Security events logged to `.claude/logs/security_events.jsonl`.

> **Retired (v0.2.77):** an earlier version of this hook also wrote every tool call to a `toucan_dataset.jsonl` "TOUCAN dataset" log. That collector had zero consumers (it was never wired into any RL training path — RL training telemetry lives in `launcher.db rl_events` plus the citation drain, both unaffected), so it was removed to save the per-tool-call I/O. No user action is needed; any existing `.claude/logs/toucan_dataset.jsonl` file is gitignored and inert, and can be deleted at leisure.

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

### `post-mcp-retrieval-record.sh` — PostToolUse `mcp__weaviate-kg__hybrid_search|mcp__weaviate-kg__semantic_graph_search|mcp__weaviate-kg__search_code_graph` (v0.2.91)
Records what an **explicit** retrieval already put in the context window, into the same per-session stores the injecting hooks consult (the inject-dedup store and the explicit-reads ledger), so the pre-edit / pre-bash injectors stop re-showing it.

<details>
<summary>Details</summary>

Before v0.2.91 the session had two suppression channels — things the *hooks* injected, and files the model *Read* — and results the model deliberately fetched with an MCP retrieval call were recorded in neither. A node an agent had just pulled on purpose could be re-injected minutes later by `pre-edit-context-inject`.

The safety rule is "suppress only what is provably in context, byte-for-byte", never "the model saw this node":

- **KG results** record the injector's own per-chunk key `<title>#<sha1(body)[:12]>`, computed from the same body text `rl_kg_search.py --hook-format` would have printed for that entry *at that tier*. Retrieving the same node later at a different tier yields a different body, a different hash, and the block still injects — correct, because that is new content.
- The hashed body is **trailing-newline-normalized on both sides** (`vco_seen_normalize_body` / `Get-VcoSeenNormalizedBody` in the seen-store, `normalize_block_body` in the recorder): trailing newlines collapse to exactly one, and an empty body stays empty. How many a rendered block actually carries is a function of *where in the blob it sits*, not of its content — the producer's `print(body)` emits an extra empty line for a body that already ends in `\n`, and the injector's `KG_RESULT="$(…)"` capture strips it back off for the LAST block only. Content `"x\n"` therefore reassembles as `"x\n\n"` in a non-final block and `"x\n"` in a final one, and the recorder cannot know a result's eventual position. Normalizing on both sides makes the key a function of the content alone; verified byte-identical three ways (bash filter, PowerShell filter, Python recorder) across every block position.
- **KG results carrying `coverage: "complete"`** (the formatter's explicit all-chunks-returned marker) additionally write the node's source path into the reads-ledger, so any chunk of that node is suppressed. Sound only because the whole node is demonstrably in context; a partial view never does this.
- **Code results** record the entity's `full_name`, and only when the result carried `function_body` / `class_body` (the untruncated top tier). A metadata-only "ref" entry records nothing — the model saw a name, not the code.

Everything else records nothing: a `titles`-detail search, a truncated middle tier, a connected-node stub. Over-suppression silently costs the model context, which is strictly worse than a duplicate injection.

Both OS flavours shell out to one shared implementation, `templates/scripts/mcp_retrieval_record.py` — all parsing and key derivation live there rather than being mirrored into PowerShell (CLAUDE.md "share, don't mirror, cross-language logic", option A). The hook never blocks, never writes to stdout (PostToolUse output would land in the transcript), and soft-fails everywhere: a parse failure records nothing.

</details>

### `config-change-audit.sh` — ConfigChange (background)
Log all settings.json changes to `.claude/logs/config_changes.jsonl` for audit trail.

### `notify-stop.sh` — Stop (background)
Send a desktop notification (`notify-send`) when Claude finishes a response. Note: Stop hooks do not fire in the VS Code extension (CLI/Desktop only).

### `stop-failure-notify.sh` — StopFailure (background)
Send an urgent desktop notification when a turn fails (rate limit, auth error, etc.) and log to `~/.claude/metrics/failures.jsonl`.

Notifications are **coalesced** (v0.2.96): at most one per 5 minutes per `(project, error class)`, and the next one through carries the count it suppressed. Before that the hook notified on every event and parsed the payload with the wrong shape, so one repeating background failure produced 304 identical "unknown: No details" pop-ups in a single night. A payload whose shape it does not recognise is now reported as a truncated copy of the payload itself rather than as "no details". **Every** event — suppressed or not — is still written to the ledger; that ledger is what made the storm diagnosable, so `VCO_STOP_FAILURE_NOTIFY=0` silences only the pop-up and never the record. The `.ps1` sibling behaves identically.

If you are seeing that storm, `vco doctor` now reads Claude Code's `hasTrustDialogAccepted` flag for the folder: when it is false every headless `claude -p` fails "this workspace has not been trusted", including the ones VCO's own summary generators make. The probe names the state and the one-step recovery (run `claude` in the folder and accept the dialog); it never writes the flag, because that is the CLI's own decision to record.

### `kg-update-nudge.sh` — UserPromptSubmit + Stop (background)
Counts substantive work tokens since the last KG node write; nudges to write a KG node when the threshold (~150k tokens) is exceeded. Bypass with `KG_NUDGE_OFF=1`.

### `verify-container-ports.sh` — SessionStart (startup, background)
Verifies that the Weaviate / Ollama / code-embed container ports are bound and reachable. Non-blocking.

Every run appends one JSON line to `<project>/.claude/logs/container_port_check.jsonl` (`<project>` = `CLAUDE_PROJECT_DIR`, else the project the hook is installed in): `timestamp` (UTC), `runtime`, `services` — for each of `weaviate` / `ollama` / `code_embed` its `result` (`healthy`, `slow` = PID alive but the port not answering yet, `zombie`, or `absent`) with the `container` and `port` — and `action`: `none`, `skipped` (with a `reason`), `lock_busy` (`.ps1`: `ensure-containers` held the session lock), `waiting_for_session_lock` (`.sh`: the run found a zombie and re-runs itself under the lock, which appends its own line) or `recovered`, with a `recovery` list saying what was done to each zombie (`recreated`, `restarted`, `left_as_is` or `failed`, and why). A log that cannot be written never changes what the hook does.

It probes each service on the port its `service_endpoints` row records (v0.2.97), and treats a container that `ps` calls running but whose main PID is dead as a zombie without probing it. It recovers a zombie only under the per-user session lock it shares with `ensure-containers` (`python -m vco_lib.service_lifecycle with-session-lock`; the `.ps1` siblings hold the same lock file), in a detached re-run like `ensure-containers`' (so its 30 s timeout never releases the lock mid-recovery), and only after the session reconcile has re-checked the rows: the two hooks never act on the same container at once, and a container is never removed on a row that could not be re-checked. Only a VCO-managed row's zombie is removed and re-created. An adopted container, or one whose service has no row yet, is only ever started by name.

### `pre-vercel-token-guard.sh` — PreToolUse Bash (blocking)
Blocks `vercel ... --token=...` invocations because the Vercel CLI echoes the token back in the `next:` block of stdout, leaking it into tool output. Forces use of `VERCEL_TOKEN` env var instead. Exit 2 on `--token=` match.

### `code-graph-incremental.sh` — (available, not wired in default settings.json)
Incremental code graph analysis on every code file edit. Auto-detects the project from the edited file path.

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

### `pre-bash-context-inject.sh` — PreToolUse Bash (V52-M)
KG context injection before `Bash` tool calls. Reads the proposed command, runs a `hybrid_search` for related concepts, and injects matches as `additionalContext`. PowerShell sibling at `templates/hooks/pre-bash-context-inject.ps1`. Propagates `session_id` to child processes so downstream invocations of `rl_kg_search.py` are attributable to the same session.

**v0.2.95 — query shape.** When the same write-target parser used by `post-bash-file-sync` recovers a target from the command, the query is built the way `pre-edit-context-inject.sh` builds it: module name from the basename plus a content snippet (the heredoc body, when the target is under `knowledge/`/`docs/` and carries no credential shape), and the written file is passed as `--anchor` / `--exclude-file` on the code-graph leg. With no recoverable target — the overwhelmingly common case — the query is byte-for-byte the pre-v0.2.95 noise-stripped command. The 500-char KG threshold and its `VCT_BASH_KG_THRESHOLD_CHARS` override are unchanged, and the code-graph branch still runs *before* that threshold.

### `post-bash-file-sync.sh` — PostToolUse Bash (v0.2.95)
Gives a **CLI write** the same treatment an `Edit`/`Write` gets. `post-file-edit.sh` is registered on matcher `Edit|Write` only, so before v0.2.95 a `cat > knowledge/foo.md <<EOF`, a `sed -i` on a `docs/` page or a `cp` into a source tree reached Weaviate *never*. This hook parses the executed command for write targets (`vco_lib/bash_write_targets.py` — redirections, heredocs, `tee`, `sed -i`, `cp`/`mv`/`install` destinations, `touch`, `dd of=`, long `--output` flags; chains / wrapper verbs / `bash -c` come from the shared `vco_lib/bash_command_walk`) and feeds each one to the SAME routing home `post-file-edit.sh` uses, `_lib/route-touched-path.sh`. PowerShell sibling ships alongside.

Two costs are deliberately bounded:

- A **pure-shell prefilter** (`_lib/bash-write-targets.sh`) rejects routine commands — `ls`, `git status`, `pytest`, and any command whose only redirections are `2>&1` / `>/dev/null` — with no subprocess at all. Python is spawned only for a command that plausibly wrote something.
- A write performed by an interpreter from its own source (`python - <<EOF … open(p,'w') … EOF`) **cannot** be read out of the command text. For `knowledge/` and `docs/` it is recovered by a watermarked mtime scan of those two directories only (lookback capped at 300 s, ~3 ms for a 1 134-file tree, at most 32 results), triggered only when the parse found nothing and the command has an opaque-write shape. **Code files written that way are a known miss** — a whole-repo walk per Bash call is unbounded, and the next `Edit` re-queues the file for the code-graph drain. Both misses are recorded in the hook's own header.

### `_lib/route-touched-path.{sh,ps1}` — the routing home (v0.2.95)
`knowledge/**` → `kg-sync` (KG collection), `docs/**.md` → `kg-sync` (development collection), `.claude/diagrams/*.{mmd,excalidraw}` → `vco_lib.diagram_indexer` (60 s throttle), code extensions → the per-turn code-graph drain queue — each gated by the Phase-8 access matrix and coalesced by the per-file debounce. Extracted from `post-file-edit.{sh,ps1}` when `post-bash-file-sync` became a second consumer; both hooks call it, neither re-implements it.

### `_lib/code-extensions.{sh,ps1}` — "is this a code file?" (v0.2.95)
One home for the extension alternation the code graph acts on. `pre-edit-context-inject`, `pre-bash-context-inject` and the routing home read it; four remaining pairs (`pre-tool-use`, `code-graph-incremental`, `stop-codegraph-drain`, `_lib/command-noise-strip`) still spell it out for reasons recorded in `tests/test_v0295_code_extension_one_home.py`, which fails if any of them drifts from the home.

### `post-bash-context-record.sh` — PostToolUse Bash (V52-M)
Outcome recorder paired with `pre-bash-context-inject.sh`. Writes a `bash` event into the per-session learning log (exit code, elapsed time, stderr-tail). Used by the RL retrieval reranker training pipeline. PowerShell sibling ships alongside.

### `post-edit-outcome.sh` — PostToolUse Edit|Write (V52-M)
Outcome event recorder for file edits. Companion to the V52-M bash pair; mirrors the contract for edit-shaped tools. PowerShell sibling at `templates/hooks/post-edit-outcome.ps1`.

### V52-M cross-OS bug fixes (v0.2.53)
The pre-existing test investigator caught two P1 production bugs in the V52-M hooks shipped at v0.2.52:

1. **POSIX exec bit missing** — three `.sh` hooks shipped with mode `0o664` (no exec bit). Without exec bit, Claude Code refuses to fire the hook on POSIX. install.py:11299–11305 now force-sets `0o755` on every `.sh` hook target after `shutil.copy2` (`copy2` preserves source mode, so any future contributor who commits a 664-mode hook silently disables it on user machines without this defensive `os.chmod`).
2. **UTF-8 BOM missing on Windows PS 5.1** — the `.ps1` siblings need a UTF-8 BOM to parse correctly under stock Windows PowerShell 5.1 (pwsh 7 tolerates BOM-less UTF-8; PS 5.1 mis-decodes as Windows-1252). Hooks now ship with BOMs encoded in the template files.

Both bugs were silently dead-on-arrival prior to v0.2.53; users with V52-M-aware retrieval reranker setups silently lost RL training signal. Tracked via the pre-existing-failure investigation in `.claude/context/audits/pre-existing-failure-investigation-2026-06-10.md`.

### Deliberate hook/script asymmetries (not orphans or gaps)

Two bundled items look like coverage gaps at a glance but are intentional — a future cleanup pass should leave them alone:

- **`kg-sync-on-edit.{sh,ps1}` ships but is registered NOWHERE by default.** It is superseded by the `post-file-edit.sh` PostToolUse auto-sync (which routes `knowledge/**/*.md` to the KG collection). The dedicated hook is kept for users who want a single-purpose KG-sync hook they can wire in their own `.claude/settings.json`; it runs standalone and no-ops under `VCT_DISABLE_HOOKS=1` (verified by `tests/test_w2d_session_start_hooks_v0273.py`). Not a dead file — an opt-in one.
- **`detect-workflow-needs` is a thin wrapper pair over a canonical `.py`.** `templates/scripts/detect_workflow_needs.py` is the pure-stdlib implementation; `detect-workflow-needs` (bash) and `detect-workflow-needs.ps1` are both thin launchers that shell out to it. The `.ps1` is not a Windows-only orphan — it has a matching bash sibling, and the OS-parity gate is satisfied because both wrappers exist. The `.py` carries the logic; the two wrappers only resolve a Python interpreter and forward argv.

---

## Per-project Agent / Skill Enable/Disable Contract (B2)

When the user toggles a per-project agent or skill off via the launcher GUI, the underlying `.md` file is **moved** from `.claude/{agents,skills}/<name>` to `.claude/{agents,skills}.disabled/<name>` (sibling directory, not deleted). The Tauri command path is `set_project_agent_enabled` / `set_project_skill_enabled` in `launcher/src-tauri/src/commands/project_state_cmd.rs` (lines 10–103).

**Why move, not delete**: bundled hooks like `agent-skill-keyword-suggest.sh` glob `.claude/agents/*.md` and `.claude/skills/*/SKILL.md`. Disabled items live in sibling `.disabled/` directories and naturally fall outside the glob; the agent/skill is invisible to keyword-suggest but the file is preserved so re-enabling is a simple toggle.

**Why move, not flag**: the FS layout doubles as the source-of-truth. `install-bundle --update` is idempotent UPSERT against the same directories; the `.disabled/` sibling location means a disabled row survives bundle updates without being silently re-enabled. The user instruction in `CLAUDE.md` ("don't delete `.claude/{agents,skills,hooks}/*.md` to uninstall — disable via the launcher") relies on this contract.

v0.2.53 Track F (B2) verified the end-to-end contract: GUI toggle off → `mv` to `.disabled/` → `install-bundle --update` respects it → toggle on → `mv` back. Test at `tests/test_fs_disable_contract_end_to_end.py`. Per-project bundle code at `vco_lib/project_init.py:3063–3245` honours the `.disabled/` companion location at update time so preservation entries are NOT written for items the user has deliberately disabled.

### Retired hook REGISTRATIONS (v0.2.95)

A hook's `.claude/settings.json` entry is merged by `_merge_hooks_for_bundle`, which recognises a VCO hook by the presence of its script identity in the CURRENT template: a shipped command whose form changed SUPERSEDES the stale one, while anything it does not recognise is the user's own hook and is preserved byte-for-byte. That rule has one blind spot — when a hook stops being shipped at all, its registration stops being recognised as VCO's and survives every future update, invoking a script the same update deleted.

`vco_lib/hook_retirements.py` closes it by declaring retired registrations as data (event, matcher, reason, retiring release, replacement). The bundle engine consults that table on every run, removes a matching inner hook, prunes an emptied group and writes one `record_auto_resolution` row per removal into `.claude/logs/auto-resolutions.jsonl`. Matching is deliberately narrow — a `.claude/hooks/` retiree by invoked-script identity, an inline one by whole-command equality — so a user command that merely *mentions* a retired path keeps running untouched.

---

## Composition Patterns

### Agent → Skill injection
The `skills:` list in agent frontmatter injects skill `SKILL.md` files into the agent's context window before it runs. This provides the agent with specialist knowledge and decision frameworks without changing its tool permissions. Example: `planner` injects `task-breakdown` (Sonnet) and `architect` (Opus) — Opus-level reasoning is available as a reference even though the planner itself runs on Sonnet.

### Blackboard coordination
The `project-coordinator` agent implements a blackboard pattern: agents volunteer for tasks from a shared `CONTEXT_STATE.md` rather than receiving delegated assignments. This pattern reduces inter-agent communication overhead and supports parallelism without a central scheduler: the shared file is the only coordination point, so no agent needs to know which others exist or wait on them.

### Hook → Agent delegation
Several hooks spawn background Claude Code agents for heavyweight tasks: `kg-summary-generator.sh` → Haiku agent to update KG summaries; `post-git-commit-kg-sync.sh` → Haiku agent to sync KG after commits. All delegating hooks guard with `CLAUDE_CODE_DISABLE_AUTO_MEMORY` to prevent infinite recursion inside subprocesses.

### `VCT_DISABLE_HOOKS=1` escape hatch
Set in your shell (or in `.claude/settings.json` under `env` — the canonical per-project env channel) to skip all hooks for that session. Every hook checks this variable as its first act.
