---
name: context
description: Efficient context state inspection, task lifecycle management, and session tracking
short_desc: "/context state inspection and session tracking"
keywords: [context state, CONTEXT_STATE, session tracking, task lifecycle, context size, /context, "what's my context", "context usage", "current task state"]
model: haiku
---

# Context Management Commands

**Purpose**: Efficient inspection and maintenance of `.claude/CONTEXT_STATE.md` — the project's active working memory (current task, recent progress, next steps, blockers).

**Model**: Haiku (fast execution, token-efficient targeted reads)

**Token Cost**: LOW (targeted section reads, not full-file dumps)

---

## How it works

Every subcommand below is carried out by Claude with ordinary tools (`Read` with `offset`/`limit`, `Grep` for section headers, `Edit` for appends, `Bash` for `wc -l`) against `.claude/CONTEXT_STATE.md` and `.claude/context/`. There is no separate binary — the value of this skill is the discipline: read only the section you need, report tersely, and keep the state file current.

---

## When to Invoke (Autonomous Suggestion)

Suggest `/context` subcommands when:

```
IF (user asks "what's the status?") → /context status
IF (user seems lost) → /context summary
IF (session >30 minutes of work) → /context update
IF (completed a milestone) → /context complete
```

---

## Subcommands

### 1. Status Check (Most Used)
```
/context status
```
Grep `CONTEXT_STATE.md` for the current-task heading and report a single-line status. Do not read the whole file.

### 2. Quick Summary
```
/context summary
```
Read the current-task, recent-progress, and blockers sections and report a short summary (a few lines each).

### 3. Blockers Check
```
/context blockers
```
Report only the blockers section (or "no blockers recorded").

### 4. Recent Activity Log
```
/context log [n]
```
Report the last N entries of the session-log / recent-progress section (default 5).

### 5. Context Size Check
```
/context size
```
Run `wc -l .claude/CONTEXT_STATE.md` and report the line count. Target is 250-350 lines; the `context-size-check` hook warns (without truncating) at 500 lines. If over target, offer to trim by archiving completed items.

### 6. Update Session Log
```
/context update "Message"
```
Append a timestamped entry to the session-log / recent-progress section via `Edit`.

### 7. Complete Task
```
/context complete [optional-note]
```
Move the current task's block into `.claude/context/archive/` (one file per completed task, dated), then reset the current-task section in `CONTEXT_STATE.md`. Never delete content — archive it.

**Task switching**: there is no separate pause/resume mechanism. To switch tasks, first run `/context update` recording exactly where the current task stands (files touched, next step, open questions), then update the current-task section for the new task. The recorded state is how future sessions resume the old task.

---

## Rules for Claude Code

1. **Targeted reads first**: use `Grep`/`offset`/`limit` on `CONTEXT_STATE.md` instead of reading the full file for a status question.
2. **Update during work**: add a `/context update` entry after significant progress, not just at session end.
3. **Suggest appropriately**: offer the matching subcommand when the user asks about status.
4. **Record before switching**: always write the current state down before changing tasks.
5. **Keep it lean**: target 250-350 lines for `CONTEXT_STATE.md` (hard warning at 500 via the `context-size-check` hook); archive completed work to `.claude/context/archive/`.

---

## Success Metrics

- Status questions answered from a section read, not a full-file read
- `CONTEXT_STATE.md` stays within its target size
- Completed tasks end up in `.claude/context/archive/`, never deleted
- A fresh session can resume any recorded task from the state file alone
