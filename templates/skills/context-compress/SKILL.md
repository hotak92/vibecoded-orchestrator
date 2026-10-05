---
name: context-compress
description: Guides the /compact pipeline (what it saves and reinjects) and maintains CONTEXT_STATE.md — status, summary, blockers, size, update, complete. Use when the context window fills up, focus switches, or the user asks about session state.
short_desc: "/compact pipeline guidance + CONTEXT_STATE inspection"
keywords: ["compact context", "/compact", "context window", "/compact context", "compress context", "before compact", "context too large", "save context state", "context state", CONTEXT_STATE, "session tracking", "task lifecycle", "context size", "what's my context", "context usage", "current task state"]
argument-hint: "[focus-topic]"
model: haiku
---

# Context compression and state inspection

Two halves of one concern: keeping the conversation context small (the
`/compact` pipeline) and keeping the project's working-memory file
(`.claude/CONTEXT_STATE.md`) small and current. This skill absorbs the
former `context` skill.

> **Note**: `/compact` itself is a Claude Code built-in command — type it
> directly to trigger compression. This skill documents the pipeline around
> it and the CONTEXT_STATE discipline.

## Part 1 — /compact [focus]

```
/compact                        # Compress with default summary
/compact auth module            # Focus summary on authentication work
/compact tests and recent fixes # Focus on testing and bug fixes
```

What it does: summarizes prior conversation into a concise state snapshot,
retains the most important context, and (with a focus argument) emphasizes
the specified topic.

When to use:
- Context window is filling up (> 60% used)
- Switching focus to a different part of the codebase
- After completing a major phase of work
- Before a long implementation session

### Automatic pre-compact save

The `pre-compact-save.sh` hook runs before compaction and saves git status +
recently modified files to `.claude/context/pre-compact-snapshot.md`.

After compaction, `compact-context-reinject.sh` re-injects:
- `CONTEXT_STATE.md` (current task state)
- Recent git commits (last 10)
- Active plan summary (first 30 lines from `.claude/context/plans/`)
- The pre-compaction snapshot

Tips: include the file or module name in the focus (`/compact auth/jwt.py`);
CONTEXT_STATE.md and active plans come back automatically.

## Part 2 — CONTEXT_STATE.md inspection and maintenance

Every subcommand below is carried out with ordinary tools (`Read` with
`offset`/`limit`, `Grep` for section headers, `Edit` for appends, `Bash` for
`wc -l`) against `.claude/CONTEXT_STATE.md` and `.claude/context/`. There is
no separate binary — the value is the discipline: read only the section you
need, report tersely, keep the state file current.

Suggest a subcommand when: the user asks "what's the status?" (→ status),
seems lost (→ summary), the session passed ~30 minutes of work (→ update),
or a milestone completed (→ complete).

1. **Status** — grep `CONTEXT_STATE.md` for the current-task heading; report
   a single line. Do not read the whole file.
2. **Summary** — read current-task, recent-progress, and blockers sections;
   report a few lines each.
3. **Blockers** — report only the blockers section (or "no blockers
   recorded").
4. **Log [n]** — report the last N entries (default 5) of the session-log /
   recent-progress section.
5. **Size** — `wc -l .claude/CONTEXT_STATE.md`; target 250–350 lines (the
   `context-size-check` hook warns, without truncating, at 500). If over
   target, offer to trim by archiving completed items.
6. **Update "message"** — append a timestamped entry to the session-log /
   recent-progress section via `Edit`.
7. **Complete [note]** — move the current task's block into
   `.claude/context/archive/` (one dated file per completed task), then reset
   the current-task section. Never delete content — archive it.

**Task switching**: no separate pause/resume mechanism exists. Record where
the current task stands (files touched, next step, open questions) via
update, then rewrite the current-task section for the new task. The recorded
state is how future sessions resume the old one.

### Rules

1. Targeted reads first — `Grep`/`offset`/`limit` instead of full-file reads
   for status questions.
2. Update DURING work, not just at session end.
3. Record state before switching tasks.
4. Keep it lean: 250–350 lines target; archive completed work to
   `.claude/context/archive/`.

### Done means

- Status questions answered from a section read, not a full-file read.
- `CONTEXT_STATE.md` stays within its target size.
- Completed tasks end up archived, never deleted.
- A fresh session can resume any recorded task from the state file alone.
