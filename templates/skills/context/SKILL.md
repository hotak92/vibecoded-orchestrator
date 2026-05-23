---
name: context
description: Efficient context state inspection, task lifecycle management, and session tracking
short_desc: "/context state inspection and session tracking"
keywords: [context state, CONTEXT_STATE, session tracking, task lifecycle, context size, /context, "what's my context", "context usage", "current task state"]
model: haiku
---

# Context Management Commands (Haiku)

**Purpose**: Efficient context state inspection, task lifecycle management, and session tracking.

**Model**: Haiku 4.5 (sub-second execution, token-efficient getter commands)

**Token Cost**: VERY LOW (getter commands, not full file reads)

---

## When to Invoke (Autonomous Suggestion)

Claude Code should proactively suggest `/context` commands when:

```
IF (user asks "what's the status?") → suggest /context status
IF (user seems lost) → suggest /context summary
IF (Claude context filled >60%) → suggest /context summary
IF (session >30 minutes) → suggest /context update
IF (switching tasks) → suggest /context pause
IF (completed milestone) → suggest /context complete
```

---

## Available Commands

### 1. Status Check (Most Used)
```bash
/context status
```
Returns single-line task status (~50 tokens).

### 2. Quick Summary
```bash
/context summary
```
Returns status + current work + blockers (~500 tokens).

### 3. Blockers Check
```bash
/context blockers
```
Returns just current blockers (~100 tokens).

### 4. Recent Activity Log
```bash
/context log [n]
```
Returns last N log entries (~150 tokens per 5 entries).

### 5. Context Size Check
```bash
/context size
```
Returns line count, token estimate, warns if >200 lines.

### 6. Pause Task
```bash
/context pause [optional-note]
```
Saves to `TEMP_MEMORY_[task].md`, resets to IDLE.

### 7. Resume Task
```bash
/context resume [taskname]
```
Restores from `TEMP_MEMORY_[task].md`.

### 8. Complete Task
```bash
/context complete [optional-note]
```
Archives to `.claude/context/archive/`, resets to IDLE.

### 9. Update Session Log
```bash
/context update "Message"
```
Adds timestamped entry to Session Log.

---

## Token Savings

**Before** (reading full CONTEXT_STATE.md):
- 3 status checks: 7,500 tokens
- 5 log reviews: 1,500 tokens
- 1 summary: 1,000 tokens
- **Total**: 10,000 tokens

**After** (using /context commands):
- 3 status checks: 150 tokens
- 5 log reviews: 500 tokens
- 1 summary: 500 tokens
- **Total**: 1,150 tokens
- **Savings**: 88%

---

## Rules for Claude Code

1. **Always use getters first**: Never read full CONTEXT_STATE.md
2. **Update frequently**: Run `/context update` after every 5-10 actions
3. **Suggest appropriately**: Offer relevant commands when user asks about status
4. **Pause before switching**: Always `/context pause` when switching tasks
5. **Keep minimal**: Target <80 lines for CONTEXT_STATE.md
6. **Internal usage**: Use `/context status` silently before expensive operations

---

## Supporting Files

- **Examples**: See [examples/usage-patterns.md](examples/usage-patterns.md) for common workflows
- **Scripts**: Backend scripts at `~/.claude/workflow/scripts/context_info.sh` and `memory_manager.py`

---

## Success Metrics

- ✅ Token usage reduced by >80% for context operations
- ✅ Commands execute in <2 seconds
- ✅ Users understand current task state instantly
