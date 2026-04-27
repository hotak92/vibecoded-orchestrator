---
title: Orchestrator Hook System
type: concept
tags: [mid-level-architecture, vibecoded-orchestrator, hooks, automation, workflow]
created: 2026-04-27T18:30:00Z
updated: 2026-04-27T18:30:00Z
status: active
---

# Orchestrator Hook System

The hook system is the orchestrator's nervous system. By intercepting Claude Code lifecycle events — session start, tool use, file edits, compaction, stop — hooks turn Claude Code into an automated workflow engine with security enforcement, knowledge-graph sync, cost tracking, and context preservation built in.

[[implements::Agentic Workflow Patterns]] [[uses::Claude Code]] [[relatedTo::Orchestrator Context Management]] [[relatedTo::Orchestrator Security]]

## Overview

Hooks are shell scripts (with a few Python helpers) registered in `.claude/settings.json`. Each hook binds to a lifecycle event and optionally a tool-name matcher. Blocking hooks (exit 0/1) can abort an operation; non-blocking hooks run in the background or fire-and-forget. Claude Code v2.1.81 exposes 17 distinct hook events; the orchestrator wires the most useful ones for autonomous workflow:

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

**ensure-containers.sh** (background)
- Checks Podman/Docker container health for Weaviate, Ollama, code-embedding service.
- Starts any stopped containers via `podman-compose up -d` or `docker compose up -d`.
- Non-blocking — runs in background so it does not delay the first prompt.
- Logs to `.claude/logs/container-health.log`.

**ensure-code-embed-service.sh** (background)
- Checks if the code-embedding FastAPI server (port 11438) is up; starts it if not.

**session-start-kg-loader.sh**
- Prints key KG resource paths (CONTEXT_STATE.md, active plan file, knowledge/ root).

**context-size-check.sh**
- Counts lines in `.claude/CONTEXT_STATE.md`; warns to stdout if line count exceeds a threshold.

### SessionStart — matcher: compact

**compact-context-reinject.sh**
- Lost-In-The-Middle (LITM) ordering: places critical info at start and end of reinject payload.
- START: `.claude/CONTEXT_STATE.md` (current task state, uncapped — most critical).
- MIDDLE: active plan file (~30 lines max), recent 8 git commits.
- END: `.claude/context/pre-compact-snapshot.md` (pre-compaction state, 50 lines max).

### PreCompact

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

### PreToolUse (blocking)

**pre-tool-use.sh** (matcher: `*`)
- Appends tool name, arguments hash, and timestamp to `.claude/logs/YYYY-MM-DD_tool_usage.jsonl`.

**SSRF guard** (matcher: `Bash(*)`)
- Scans Bash commands for HTTP requests to private IP ranges; blocks unless target is a whitelisted localhost service (Weaviate 8081, Ollama 11435, code-embed 11438, SearXNG 8888, etc.).

**Shell injection scan** (matcher: `Bash(*)`)
- Scans for dangerous patterns: `curl | sh`, `eval $(curl ...)`, `base64 | sh`.
- Delegates to `bash_security.py` which applies 9 rule categories and 20+ rules.

**pre-edit-context-inject.sh** (matcher: `Edit(*)`)
- Before editing a file, runs KG search for the filename/concept and code-graph search for related functions.
- Injects search results as context (live ~2.7s, cached ~31ms via 10-min TTL file cache).
- Session-level dedup via `SEEN_NODES_FILE` prevents repeating the same nodes; resets on compaction.

### PostToolUse (non-blocking)

**post-file-edit.sh** (matcher: `Edit(*)|Write(*)`)
- Routes edited files to the appropriate sync pipeline:
  - `knowledge/**/*.md` → `sync_knowledge_graph.py` → Weaviate KG collection
  - `docs/**/*.md` → development docs collection
  - Python/JS/TS code files → code-graph analysis queue
- Runs duplicate detection every 10 edits.

**ruff** (matcher: `Edit(*.py)|Write(*.py)`)
- Runs `ruff check --fix --quiet <file>` in background — auto-fixes formatting and imports.

**pyright** (matcher: `Edit(*.py)|Write(*.py)`)
- Runs pyright type-checking in background — non-blocking.

**py_compile** (matcher: `Write(*.py)`)
- Runs `python -m py_compile <file>` immediately after a Python file is written.
- Catches syntax errors before ruff/pyright run.

**post-tool-security.sh** (matcher: `Edit(*)|Write(*)`)
- Scans written file content for credential patterns (API keys, tokens, AWS keys).
- Logs findings to `.claude/logs/security-scan.jsonl`. Non-blocking — informational only.

**kg-summary-generator.sh** (matcher: `Edit(knowledge/**/*.md)|Write(knowledge/**/*.md)`)
- Spawns a background Haiku/Ollama summary job to refresh `knowledge/.node_formats.json` for the edited file.
- See [[KG-Summary Three-Tier Generation Pipeline]] for backend selection (claude CLI → Ollama → API → skip).

**code-graph-incremental.sh** (matcher: `Edit(*.py)|Write(*.py)|Edit(*.ts)|Edit(*.js)|...`)
- Queues edited code files for incremental code-graph re-analysis.

### ConfigChange

**config-change-audit.sh**
- Logs `.claude/settings.json` modifications to `.claude/logs/config-changes.jsonl`.

### Stop

**cost-tracker.sh**
- Appends `{timestamp, session_id, model, input_tokens, output_tokens, cache_read_tokens, cost_usd}` to `~/.claude/metrics/costs.jsonl`.
- Summary CLI: `.claude/scripts/cost-summary`.

**notify-stop.sh**
- Desktop notification via `notify-send`: "Claude session ended".

### StopFailure

**stop-failure-notify.sh**
- Sends urgent desktop notification (different urgency level than normal stop).
- Appends failure record to `~/.claude/metrics/failures.jsonl`.

## Hook Discipline

Every shell hook in `.claude/hooks/*.sh` carries:

1. **Env-scrub block** at the top — strips `SUPABASE_KEY`, `GITHUB_TOKEN`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AWS_*`, `TELEGRAM_BOT_TOKEN`, `DATABASE_URL`, etc. before any subprocess inherits the environment.
2. **VCT_DISABLE_HOOKS escape hatch** immediately after the env-scrub — `VCT_DISABLE_HOOKS=1 claude` disables every hook for debugging or CI. See [[Hook Discipline — VCT_DISABLE_HOOKS Escape Hatch]].
3. **Cross-OS portability** — `${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}` instead of hardcoded `/tmp`. See [[Cross-OS Hook Portability]].
4. **`bash -n` syntax check** runs in CI on every commit touching `.claude/hooks/`.

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
