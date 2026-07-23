---
name: context-compress
description: Guide for using /compact (Claude Code built-in) with the pre-compact save pipeline — what gets saved before compression and reinjected after. Use when the context window is filling up, before a long implementation session, or when switching focus and you want to compact without losing task state. Not a substitute for /compact itself, which is a built-in command.
short_desc: "/compact pipeline pre-save context guidance"
keywords: ["compact context", "/compact", "context window", "/compact context", "compress context", "before compact", "context too large", "save context state"]
argument-hint: "[focus-topic]"
model: haiku
---

# /compact [focus]

Compress the current conversation context to free up space, optionally focusing on a topic.

> **Note**: `/compact` is a Claude Code built-in command — type it directly to trigger compression.
> This skill (`/context-compress`) is documentation for the pipeline around it.

## Usage

```
/compact                        # Compress with default summary
/compact auth module            # Focus summary on authentication work
/compact tests and recent fixes # Focus on testing and bug fixes
```

## What This Does

1. **Summarizes** all prior conversation into a concise state snapshot
2. **Retains** the most important context for continuing work
3. **Focus** (optional): emphasizes the specified topic in the summary

## When to Use

- Context window is filling up (> 60% used)
- Switching focus to a different part of the codebase
- After completing a major phase of work
- Before a long implementation session

## Automatic Pre-Compact Save

The `pre-compact-save.sh` hook automatically runs before compaction:
- Saves git status and recently modified files to `.claude/context/pre-compact-snapshot.md`

After compaction, `compact-context-reinject.sh` re-injects:
- `CONTEXT_STATE.md` (current task state)
- Recent git commits (last 10)
- Active plan summary (first 30 lines from `.claude/context/plans/`)
- The pre-compaction snapshot

## Tips

- Include the file or module name in the focus: `/compact auth/jwt.py`
- After compaction, CONTEXT_STATE.md is automatically re-injected
- Active plans (`.claude/context/plans/`) are re-injected from the first 30 lines
