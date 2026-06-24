---
title: Orchestrator Context Management
type: concept
tags: [mid-level-architecture, vibecoded-orchestrator, context-management, memory, hook-system]
created: 2026-04-27T18:30:00Z
updated: 2026-05-16T20:30:00Z
status: active
---

# Orchestrator Context Management

Context management is the discipline of keeping Claude's working state coherent across compaction events, session restarts, and long multi-phase tasks. The orchestrator implements a two-layer memory architecture and a save/restore pipeline that preserves working context through Claude Code's automatic context compaction, with RL-scored tiered injection and session-level deduplication.

[[implements::Two Layer Memory Pattern]] [[relatedTo::Orchestrator Hook System]] [[relatedTo::Orchestrator RL Retrieval]]

## Why It Matters

Claude Code has a fixed context window. When conversations grow long, Claude Code auto-compacts: it summarizes the conversation history into a shorter representation and discards the raw turns. Without intervention this means losing the active task state, recently modified files, git status, and the active plan.

The orchestrator solves this with a pipeline that saves state before compaction and rehydrates it after, using Lost-In-The-Middle (LITM) ordering to place critical information at the start and end of the reinject payload.

## Two-Layer Memory

### Layer 1: MEMORY.md (Stable, Auto-Loaded)

Location: `~/.claude/projects/<project-hash>/memory/MEMORY.md`

**Purpose**: long-term stable facts that don't change session-to-session.

- Project root paths
- Recurring bug fixes and their solutions
- Workflow rules
- Tool/service URLs
- User preferences

**Characteristics**:

- First 200 lines auto-loaded at session start (hard cap; silent truncation after).
- Not in git — machine-local.
- Written by Claude via `/memory` command or "remember that..." prompts.
- Indexed by topic files (e.g. `memory/debugging.md`).
- Edit concisely — the 200-line cap means every line counts.

### Layer 2: CONTEXT_STATE.md (Active, Task-Scoped)

Location: `.claude/CONTEXT_STATE.md` (project root).

**Purpose**: active working memory for the current task or sprint.

- Current task goal and phase
- Recently completed steps (with checkmarks)
- Open blockers
- Files modified in this session
- Key decisions made
- Next steps

**Characteristics**:

- Target 250-350 lines, soft max 500.
- In git — shared across machines and visible to all team members.
- Updated **during** work, not just at the end of session.
- Read at session start via `compact-context-reinject.sh`.
- A SessionStart hook (`context-size-check.sh`) emits a soft *warning* at 500 lines (it never truncates — unlike MEMORY.md, which the Claude Code memory feature hard-caps at the first 200 lines / ~25 KB).

**Example structure**:

```markdown
# Current Task: Implement JWT Authentication

## Current Phase
Implementing token validation middleware

## Completed
- User model with password hashing
- Login endpoint (src/api/auth.py lines 15-45)

## Open Blockers
- Need refresh-token endpoint spec

## Modified Files
- src/api/auth.py
- src/middleware/auth.py
- tests/test_auth.py

## Next Steps
1. Implement refresh token endpoint
2. Add integration tests
```

## Pre-Compact Save Cycle

When Claude Code detects the context window filling, it fires `PreCompact`:

```
Context nearing limit
        |
[PreCompact hook — matcher: auto]
        |
pre-compact-save.sh:
  - git status
  - git diff --stat
  - find . -newer .last_run (recently modified files)
  - Write all to: .claude/context/pre-compact-snapshot.md
        |
precompact_prune.py (optional):
  - Remove completed tasks older than current session from CONTEXT_STATE.md
        |
Claude Code runs compaction
        |
[PostCompact hook]
  - Log to ~/.claude/metrics/compactions.jsonl
  - Desktop notification: "Context compacted"
```

## Post-Compact Reinject (LITM Ordering)

When the session resumes after compaction, `SessionStart compact` fires:

```
[SessionStart — matcher: compact]
        |
compact-context-reinject.sh (tiered LITM ordering):

  START position (most critical, uncapped):
    1. CONTEXT_STATE.md — current task state, blockers, next steps

  MIDDLE position (less critical, capped):
    2. Active plan file (cap: 30 lines)
    3. Recent git log (cap: 8 commits)

  END position (second most critical, capped):
    4. Pre-compact snapshot (cap: 50 lines) — git status, recent files
```

LITM principle: Claude weights the start and end of context most heavily. The reinject strategy places most critical info at START and END, less critical in MIDDLE. Total reinject payload ~250 lines max. Claude wakes up with full awareness of what was being worked on, with the most critical information at the boundaries where it is weighted highest.

## Session-Level Deduplication

The pre-edit-context-inject hook tracks which KG and code-graph nodes have already been injected into context during the current session:

1. Tracks injected nodes in `${TMPDIR:-/tmp}/claude_seen_nodes_${SESSION_ID}`.
2. Skips re-injection if a node was recently injected (same session).
3. Resets dedup tracking on compaction (fresh context state).

Prevents repetitive context bloat when the same pattern nodes match multiple similar queries across a long session.

## Node Summaries

KG nodes have structured metadata stored in `knowledge/.node_formats.json`:

```json
{
  "knowledge/concepts/foo.md": {
    "title": "Foo Pattern",
    "description": "3-4 sentence description of what/how/why",
    "summary": "1-2 sentence whole-node summary",
    "chunk_summaries": {"1": "...", "2": "..."},
    "total_chunks": 2,
    "generated_at": "2026-04-11T12:00:00Z",
    "content_hash": "abc123def456"
  }
}
```

Generation: the `kg-summary-generator.sh` PostToolUse hook spawns a background job that generates description + summary on each KG file edit (debounced 60s; skips regeneration if content hash unchanged). See [[KG-Summary Three-Tier Generation Pipeline]] for backend selection.

Consumption:

- RL scoring uses summaries as ranking features.
- `hybrid_search(detail="descriptions")` returns summaries instead of full content (~50% token savings vs. full).
- `hybrid_search(detail="full")` returns the complete markdown body for deep work.

## Diff-Context Injection

For long sessions, reading the full CONTEXT_STATE.md on every prompt is wasteful. The `diff-context-inject.sh` hook optimizes this:

- **First prompt of session**: inject full CONTEXT_STATE.md.
- **Subsequent prompts**: compute section-level diff vs. previous version; inject only changed sections.
- **Token savings**: 70-90% reduction in context overhead after the first prompt of a session.

Implementation:

1. Hook saves a hash of CONTEXT_STATE.md after each injection.
2. On next UserPromptSubmit, compares current file vs. saved hash.
3. If unchanged: inject nothing (no overhead).
4. If changed: compute diff, inject only changed sections.

## Context Size Check

`context-size-check.sh` (SessionStart hook):

1. Counts lines in `.claude/CONTEXT_STATE.md`.
2. Warns (never truncates) if >500 lines (needs pruning toward the 250-350 target).
3. Output is visible in the session preamble.

Prevents gradual context bloat — CONTEXT_STATE.md should be a focused snapshot, not a running log.

## Token Efficiency Patterns

### Parallel Tool Calls

```
# WRONG (sequential, slow):
Read file1.py
Wait...
Read file2.py
Wait...

# RIGHT (parallel, 50-70% faster):
Read file1.py | Read file2.py | Grep "pattern" dir/
(all in single message, all execute simultaneously)
```

### Targeted File Reads

- Use `offset` + `limit` for large files (read only the relevant section).
- Use Grep to locate the relevant lines first, then Read with offset.
- Trust writes — don't re-read files after writing them.

### Agent Delegation

For multi-file operations (>10 files), spawn a sub-agent rather than reading files sequentially in the main context. The agent's context is separate and doesn't consume the main session's token budget.

### Ollama for Analysis

The Ollama MCP (`chat`, `read_document`) is no longer installed by default as of v0.2.11. If you've opted into the Ollama MCP (Launcher → Modules → "Ollama Local LLM"), you can use `chat` for quick local analysis at zero API cost. Without the opt-in, use Claude's native reasoning and the `Read` tool with `offset`/`limit` for large files instead.

## Active Plan Files

Plans for current work live in `.claude/context/plans/`. They are NOT auto-loaded (too large) but are:

- Referenced by name in CONTEXT_STATE.md.
- Loaded on-demand when relevant.
- Injected by `compact-context-reinject.sh` when referenced in the active CONTEXT_STATE.

Completed plans are archived to `.claude/context/archive/`.

## Integration Points

- `compact-context-reinject.sh` and `pre-compact-save.sh` are the core hooks.
- MEMORY.md auto-loading is a Claude Code built-in.
- `diff-context-inject` depends on UserPromptSubmit being a blocking hook.
- CONTEXT_STATE.md is the shared state that team agents read and write during blackboard coordination.
- Session dedup tracking is cleared on compaction to ensure fresh state.
- Node summaries from `knowledge/.node_formats.json` improve RL retrieval and reduce token consumption.
