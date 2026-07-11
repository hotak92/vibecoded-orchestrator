---
title: Orchestrator Hook System
type: concept
tags: [mid-level-architecture, vibecoded-orchestrator, hooks, automation, workflow]
created: 2026-04-27T18:30:00Z
updated: 2026-06-25T00:00:00Z
status: active
---

# Orchestrator Hook System

The hook system is the orchestrator's nervous system. By intercepting Claude Code lifecycle events — session start, tool use, file edits, compaction, stop — hooks turn Claude Code into an automated workflow engine with security enforcement, knowledge-graph sync, cost tracking, and context preservation built in.

[[implements::Agentic Workflow Patterns]] [[uses::Claude Code]] [[relatedTo::Orchestrator Context Management]] [[relatedTo::Orchestrator Security]]

## Overview

Hooks are shell scripts (with a few Python helpers) registered in `.claude/settings.json`. Each hook binds to a lifecycle event and optionally a tool-name matcher. Blocking hooks (exit non-zero) can abort an operation; non-blocking hooks run in the background or fire-and-forget. Claude Code exposes ~20 distinct hook events; the orchestrator wires the most useful ones for autonomous workflow:

- **Security enforcement** — scan commands before execution, scrub credentials
- **Knowledge sync** — push edited KG/doc/code files to Weaviate automatically
- **Context preservation** — save state before compaction, reinject after
- **Cost tracking** — log token usage and USD cost every session
- **Quality gates** — validate agent output before marking tasks done
- **Developer experience** — auto-format Python, type-check, compile-check

## Architecture

```
User action / Claude turn
        |
        v
[UserPromptSubmit]      — may inject diff context, show reminders
        |
        v
[PreToolUse]            — security scan, logging, KG search suggestion
        |
   Tool executes
        |
        v
[PostToolUse]           — sync KG, lint, type-check, credential scan
        |
        v
[Stop / StopFailure]    — cost tracking, notification
```

Compaction path:
```
Context nearing limit
        |
[PreCompact]            — save git status + recent files, prune stale context
        |
   Compaction runs
        |
[PostCompact]           — log event, desktop notify
        |
[SessionStart compact]  — reinject CONTEXT_STATE + commits + plan + snapshot
```

## Hook Inventory

### SessionStart — matcher: startup

**check-no-fork-bomb.sh**
- Guards against a recursive hook-spawn pattern before any container/service work runs.

**ensure-containers.sh** (background)
- Checks Podman/Docker container health for Weaviate, Ollama, code-embedding service.
- Starts any stopped containers via `podman-compose up -d` or `docker compose up -d`.
- Non-blocking — runs in background so it does not delay the first prompt.

**verify-container-ports.sh**
- Confirms the expected service ports (Weaviate, Ollama, code-embed) are reachable.

**ensure-code-embed-service.sh** (background)
- Checks if the code-embedding FastAPI server (port 11440) is up; starts it if not.

**session-start-ensure-hub.sh**
- Ensures the detached `vct-hub` config-resolver service is running.

**session-start-kg-loader.sh**
- Prints key KG resource paths (CONTEXT_STATE.md, active plan file, knowledge/ root).

**embedding-failures-surface.sh**
- Surfaces any queued embedding/sync failures from the last session so they are visible at startup.

**context-size-check.sh**
- Counts lines in `.claude/CONTEXT_STATE.md`; warns to stdout if line count exceeds a threshold.

### SessionStart — matcher: compact

**compact-context-reinject.sh**
- Lost-In-The-Middle (LITM) ordering: places critical info at start and end of reinject payload.
- START: `.claude/CONTEXT_STATE.md` (current task state, uncapped — most critical).
- MIDDLE: active plan file (~30 lines max), recent 8 git commits.
- END: `.claude/context/pre-compact-snapshot.md` (pre-compaction state, 50 lines max).

**kg-update-nudge.sh**
- Re-evaluates the accumulated-work-unit counter so a deferred KG-write nudge survives compaction.

### PreCompact — matcher: auto

**pre-compact-save.sh**
- Captures `git status`, `git diff --stat`, and recently modified files.
- Writes snapshot to `.claude/context/pre-compact-snapshot.md`.

### PostCompact

**post-compact.sh**
- Appends compaction event to `~/.claude/metrics/compactions.jsonl`.
- Sends desktop notification via `notify-send`.

### UserPromptSubmit (blocking)

**user-prompt-submit-reminder.sh**
- Injects workflow reminders into context (e.g., "check CONTEXT_STATE first").

**diff-context-inject.sh**
- First prompt of session reads CONTEXT_STATE.md in full.
- Subsequent prompts compute a section-level diff and inject only changed sections.
- Achieves 70-90% token savings on repeat prompts in a long session.

**kg-update-nudge.sh**
- Accumulates work units (output tokens + intake + edits) and nudges toward a KG write once the threshold is crossed.

**agent-skill-keyword-suggest.sh**
- Surfaces relevant agents/skills when the prompt's keywords match a bundled capability.

### SubagentStart

**subagent-start-suggest.sh** / **subagent-start-kg-inject.sh**
- Suggest relevant capabilities and inject KG context into a freshly-spawned subagent.

### SubagentStop (blocking)

**subagent-stop-reconcile.sh**
- Reconciles subagent output (worktree diffs, KG-write discipline) before the subagent is allowed to stop.

### PreToolUse

**pre-tool-use.sh** (matcher: `*`)
- Hosts the security layers (SSRF guard, shell-injection scan, Build Anchor + file backup) plus the pre-Edit/Write KG suggestion. See [[Orchestrator Security]].
- Historical note (v0.2.77 9-bis): this hook previously wrote every tool call to a `.claude/logs/toucan_dataset.jsonl` "TOUCAN dataset" log. That collector had zero consumers (never wired into RL training — RL training data lives in `launcher.db rl_events` + the citation drain), so it was retired to drop the per-tool-call I/O.

**SSRF guard** (inside `pre-tool-use.sh`, matcher: `*`, acts on `WebFetch`)
- Inspects `WebFetch` target URLs and blocks private/internal addresses unless whitelisted (Weaviate 8081, Ollama 11435, code-embed 11440, Gradio 7860). `search_papers` reaches its APIs directly and is not routed through this guard.

**Shell injection scan** (inside `pre-tool-use.sh`, acts on `Bash`)
- Blocks fetch-piped-to-shell patterns (`curl|sh`, `eval $(curl …)`, `base64 -d | sh`), then delegates to `bash_security.py` (a flat list of ~24 regex rules covering disk-destroy, credential exfil, secret-file reads, world-writable chmod, remote installs, reverse shells, etc.).

**pre-vercel-token-guard.sh** (matcher: `Bash`)
- Blocks Bash commands that would echo or leak a Vercel deploy token.

**lean-ctx-rewrite.sh / lean-ctx-rewrite.ps1** (matcher: `Bash`)
- Rewrites Bash commands for token compression via `lean-ctx`. Scrubs sensitive environment variables before delegating to the lean-ctx subprocess; graceful no-op when lean-ctx is not installed; symmetric `bypass` support for raw output per call.

**pre-bash-context-inject.sh** (matcher: `Bash`)
- Injects relevant KG context ahead of a Bash command when the command's intent maps to known patterns.

**pre-edit-context-inject.sh** (matcher: `Edit`)
- Before editing a file, runs KG search for the filename/concept and code-graph search for related functions.
- Injects search results as context. Cold (cache-miss) ~1.3s; warm (cache-hit) ~0.1s via the 10-min TTL per-file cache. v0.2.77 Part 9 moved the cache-replay branch ahead of the search launch so a warm hit is served from cache without re-paying the ~1.3s Weaviate+embed round-trip (the pre-fix cache was dead — it launched+awaited the searches before the replay branch, so warm ≈ cold). A cross-surface shared TTL result-cache (query-cache.sh) additionally serves repeat queries across pre-edit/pre-bash/pre-tool-use.
- Session-level dedup via a seen-nodes file prevents repeating the same nodes; resets on compaction.

**pre-diagram-path-validation.sh** (matcher: `Write|Edit` and `mcp__mermaid__.*|mcp__excalidraw__.*`)
- Validates that diagram outputs land in an indexed path before the write proceeds.

### PostToolUse (non-blocking)

**post-file-edit.sh** (matcher: `Edit|Write`)
- Routes edited files to the appropriate sync pipeline:
  - `knowledge/**/*.md` → `sync_knowledge_graph.py` → Weaviate KG collection
  - `docs/**/*.md` → development docs collection
  - code files → `code-graph-incremental.sh` (incremental code-graph re-analysis)
- Runs duplicate detection periodically.

**post-edit-outcome.sh** (matcher: `Edit|Write`)
- Records the edit outcome for retrieval-quality telemetry.

**py_compile** (inline `python3 -c`, matcher: `Write`)
- Compile-checks a Python file immediately after it is written, surfacing syntax errors.

**post-tool-security.sh** (matcher: `Edit|Write`)
- Scans written file content for credential patterns. Logs findings to `.claude/logs/security-scan.jsonl`. Non-blocking — informational only.

**sync_knowledge_graph.py + kg-summary-generator.sh** (matcher: `Edit|Write`)
- Syncs an edited knowledge node to Weaviate and spawns a background summary job to refresh `knowledge/.node_formats.json`.
- See [[KG-Summary Three-Tier Generation Pipeline]] for backend selection (claude CLI → Ollama → API → skip).

**kg-summary-generator.sh** (matcher: `mcp__weaviate-kg__store_knowledge_node`)
- Refreshes the sidecar summary when a node is written through the MCP tool rather than a file edit.

**post-bash-context-record.sh** (matcher: `Bash`)
- Records Bash context for later retrieval; **post-git-commit-kg-sync.sh** + **post-file-delete.sh** also fire on `Bash` to sync KG on commit and prune deleted-file entries.

**kg-update-nudge.sh** (matcher: `*`)
- Tracks accumulated work units across all tool use so the next-prompt nudge fires at the right threshold.

### ConfigChange

**config-change-audit.sh**
- Logs `.claude/settings.json` modifications to a JSONL audit trail.

### Stop

**cost-tracker.sh**
- Appends `{timestamp, session_id, model, input_tokens, output_tokens, cache_read_tokens, cost_usd}` to `~/.claude/metrics/costs.jsonl`.
- Summary CLI: `python .claude/scripts/cost-summary.py [--days N]` (portable, all OSes; bash `cost-summary` shim delegates to it on POSIX).

**notify-stop.sh**
- Desktop notification via `notify-send`.

### StopFailure

**stop-failure-notify.sh**
- Sends urgent desktop notification (different urgency level than normal stop).
- Appends failure record to `~/.claude/metrics/failures.jsonl`.

## Hook Source Layout

The source-of-truth for all hook scripts is `templates/hooks/*.sh` (and their `.ps1` Windows siblings). The `.claude/hooks/` directory in any installed project is rendered from these templates at install time — it is not an authoritative source. When reading or referencing hook code, use `templates/hooks/*.sh`.

## Python Environment (venv) Resolution

The hooks that call Python helpers (`code-graph-incremental.sh`, `kg-summary-generator.sh`, `pre-edit-context-inject.sh`) need a Python interpreter from the orchestrator venv. The resolution order is:

1. `$VCT_PYTHON` / `$VCT_VENV` — explicit override (highest priority).
2. `$REPO_ROOT/.venv/bin/python` — top-level venv layout (installs where `install.py` creates `.venv` at the install root).
3. `$REPO_ROOT/claude_mcp_servers/.venv/bin/python` — fallback layout (installs that placed the venv inside `claude_mcp_servers/`).
4. Windows path variants of the above (`.venv/Scripts/python.exe`).

Checking the top-level `.venv` first keeps code-graph and KG-summary updates working on installs that no longer keep the venv under `claude_mcp_servers/`.

## MCP Lifecycle — SIGHUP Env Reload

MCP server processes register a `SIGHUP` handler that calls `sys.exit(0)` (clean exit). Claude Code's MCP supervisor respawns any process that exits cleanly, picking up the latest env from `~/.claude.json` on restart. The launcher's file watcher on `.claude/settings.json` (debounced) dispatches the SIGHUP automatically whenever the file changes, and a "Reload MCP env" action in the launcher dispatches it on demand. The handler degrades to a no-op (the MCP keeps working, just without auto-reload) when the helper module is unavailable.

## Hook Discipline

Every shell hook in `templates/hooks/*.sh` carries:

1. **Env-scrub block** at the top — strips `SUPABASE_KEY`, `GITHUB_TOKEN`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AWS_*`, `TELEGRAM_BOT_TOKEN`, `DATABASE_URL`, etc. before any subprocess inherits the environment.
2. **VCT_DISABLE_HOOKS escape hatch** immediately after the env-scrub — `VCT_DISABLE_HOOKS=1 claude` disables every hook for debugging or CI. See [[Hook Discipline — VCT_DISABLE_HOOKS Escape Hatch]].
3. **Cross-OS portability** — `${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}` instead of hardcoded `/tmp`. See [[Cross-OS Hook Portability]].
4. **`bash -n` syntax check** runs in CI on every commit touching `templates/hooks/`.

## Integration Points

- All hooks share `.claude/logs/` for output.
- Security hooks share credential patterns with `bash_security.py`.
- KG sync hooks all route through `sync_knowledge_graph.py`.
- Cost tracking integrates with the metrics dashboard at `~/.claude/metrics/`.

## Technical Details

- Hook scripts are bash by default; Python helpers run via the MCP venv.
- Hooks inherit the Claude Code process environment, with sensitive vars scrubbed by the env-scrub header.
- Blocking hooks should complete in <2s to avoid delaying Claude responses.
- Matchers use glob patterns; `*` matches all tools; `Edit(*)|Write(*)` matches file ops.
