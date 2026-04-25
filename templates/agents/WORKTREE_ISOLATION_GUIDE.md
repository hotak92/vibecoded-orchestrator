# Agent Worktree Isolation Guide

## What it does

When an agent has `isolation: worktree` in its frontmatter, Claude Code automatically:
1. Creates a temporary git worktree (a separate checkout of the repo, on a new branch)
2. Runs the agent in that isolated copy
3. If the agent makes **no changes** → worktree is auto-cleaned (no trace)
4. If the agent **makes changes** → returns the worktree path + branch name for review/merge

## Why it matters

Without isolation, a write-capable agent directly modifies your working directory. If it makes mistakes:
- Files may be overwritten with wrong content
- Partial implementations may break existing code
- Hard to undo multiple interleaved changes

With `isolation: worktree`, mistakes are contained to the worktree branch. You review, then merge or discard.

## Which agents typically have it

Enable on agents that write source code:
- `coder` — general code implementation
- `backend-specialist` — API/service/DB code
- `frontend-specialist` — React/UI components
- `code-migrator` — cross-language rewrites
- `helper-scripter` — hooks/scripts/automation

NOT enabled (read-only or advisory agents):
- `planner`, `project-architect`, `project-coordinator` — planning only
- `tester` — writes tests but low risk (tests go in tests/)
- `doc-maintainer`, `doc-organizer` — documentation
- `kg-navigator`, `knowledge-curator` — knowledge graph read/write to isolated state

## When to add isolation to new agents

Add `isolation: worktree` to any agent that:
- Writes or edits source code files
- Modifies configuration files
- Performs refactoring across multiple files
- Has a name ending in `-specialist`, `-coder`, `-migrator`, or `-writer`

Do NOT add to agents that:
- Only read files (Explore, kg-navigator)
- Only output text/analysis to the conversation
- Only write to isolated state directories (e.g., `state/`, `knowledge/`)

## Merging worktree changes

After an agent with worktree isolation completes, if it made changes:
```bash
# View what the agent changed
git diff main..<worktree-branch>

# Merge if satisfied
git merge <worktree-branch>

# Or discard
git branch -D <worktree-branch>
```

Claude Code may also prompt you to review/merge automatically.

## Frontmatter syntax

```yaml
---
model: claude-sonnet-4-6
isolation: worktree
description: "Brief description of what this agent does"
---
```

The `isolation` field is supported directly by Claude Code's agent system.
