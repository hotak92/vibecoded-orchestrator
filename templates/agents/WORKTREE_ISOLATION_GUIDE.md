# Agent Worktree Isolation Guide

## What it does

When an agent has `isolation: worktree` in its frontmatter, Claude Code and VCO's isolation hooks
(see "How VCO creates the worktree" below):
1. Create a temporary git worktree under `.claude/worktrees/` (a separate checkout of the repo, on a
   detached HEAD)
2. Run the agent in that isolated copy
3. If the agent makes **no changes**, the worktree is removed when the agent finishes. VCO's
   `subagent-stop-reconcile` hook does this, because the harness does not clean up worktrees that a
   `WorktreeCreate` hook created.
4. If the agent **makes changes**, the worktree is kept for review/merge, and its path is logged

## Why it matters

Without isolation, a write-capable agent directly modifies your working directory. If it makes mistakes:
- Files may be overwritten with wrong content
- Partial implementations may break existing code
- Hard to undo multiple interleaved changes

With `isolation: worktree`, mistakes are contained to the worktree branch. You review, then merge or discard.

## Which agents typically have it

Enable on agents that write source code:
- `expert-coder` — general code implementation (incl. backend/API work)
- `frontend-specialist` — React/UI components
- `code-migrator` (migration pack) — cross-language rewrites
- `agent-author` — hooks/scripts/automation, agent/skill definitions
- `sre-incident-responder`, `postmortem-author` (ops-sre pack) — long-form artifacts drafted from live-incident state
- `gui-expert` (design-ux pack) — designs AND implements Gradio applications
- `ai-llm-expert` (ai-engineering pack) — designs AND implements production LLM integrations
- `glm-implementer`, `glm-implementer-high`, `qwen-implementer`, `qwen-implementer-high`, `deepseek-implementer`, `kimi-implementer` (gateway module — delivered only when the model gateway is configured and that vendor's key resolves) — bounded, fully-specified code-fix lanes
- `qwen-flash-sweeper` (gateway module) — fully-specified mechanical edits across named files

NOT enabled (read-only or advisory agents). By rule: an agent whose only writes are report, spec or artifact documents — never source or config — is exempt; this list names every current exemption, and the frontmatter test in `tests/test_v02100_shipped_frontmatter_parses.py` holds the same set over write-capable agents (an agent with no Write/Edit tool would need no exemption; since v0.2.102 `code-reviewer` carries Write for its report file, so it is listed like the others):
- `planner` — planning only
- `code-reviewer`, `code-explorer` — review / research; each writes only its own report file, never source
- `tester` — writes tests but low risk (tests go in tests/)
- `doc-maintainer` — documentation
- `kg-maintainer` — knowledge graph read/write to isolated state
- `web-explorer` — research that ends in one written report
- `gui-tester` — screenshots and structured reports, no source edits
- `deepseek-researcher`, `glm-flash-researcher` (gateway module) — report-only research lanes (bounded reviews and fix-delta verifications included)
- `glm-planner` (gateway module) — planning/report-only lane
- `glm-reviewer`, `kimi-reviewer` (gateway module) — review/report-only lanes
- `ai-agentic-architect` (ai-engineering pack) — multi-agent design documents, never execution
- `automation-engineer` (ops-sre pack) — one implementation-ready spec document
- `consulting-cto-portfolio-coordinator`, `consulting-employee-impersonator`, `consulting-sow-drafter` (consulting pack) — documents and drafts
- `enterprise-ux-architect` (design-ux pack) — design deliverables
- `brand-identity-architect`, `inbox-triage-operator`, `landing-page-critic`, `launch-orchestrator`, `outbound-sequence-writer` (gtm-marketing pack) — reports and copy artifacts
- `discipline-expert`, `experiment-designer`, `paper-triage` (science pack) — consultation and report documents

## When to add isolation to new agents

Add `isolation: worktree` to any agent that:
- Writes or edits source code files
- Modifies configuration files
- Performs refactoring across multiple files
- Has a name ending in `-specialist`, `-coder`, `-migrator`, or `-writer`

Do NOT add to agents that:
- Only read files (Explore, code-explorer)
- Only output text/analysis to the conversation
- Only write to isolated state directories (e.g., `state/`, `knowledge/`)

## How VCO creates the worktree (and the two special cases)

VCO registers its own `WorktreeCreate` hook, `.claude/hooks/worktree-guard.sh` (`.ps1` on Windows). The hook
replaces Claude Code's built-in git logic for every isolated launch, so it handles three cases:

| Project folder | What the agent gets | Isolation |
|---|---|---|
| Git repository with at least one commit | a detached worktree of `HEAD` under `.claude/worktrees/` | on |
| Git repository with **no commits yet** (just `git init`-ed) | a detached worktree of a **snapshot commit** of the current working tree (built through a throwaway index; your index, refs and HEAD are not touched, `.gitignore` is honoured, the commit is on no branch) | on |
| Folder that is **not a git repository** | the folder itself; the agent edits your files directly | **off** |

In the last two cases the agent receives a notice when it starts (through the `SubagentStart` hook
`subagent-start-isolation-check`) explaining what happened, and it is asked to repeat that notice in its
report. For a snapshot worktree the notice includes the command that copies the agent's changes back into
the main checkout:

```bash
git -C <worktree> add -A && git -C <worktree> diff --binary --cached <snapshot-sha> | git -C <project> apply
```

### Cleanup when the agent finishes

For a worktree that `worktree-guard` created, Claude Code does not remove it and does not run a
`WorktreeRemove` hook when a subagent finishes. VCO's `SubagentStop` hook, `subagent-stop-reconcile`,
removes it instead (with `git worktree remove`, never `--force`), but only when **all** of these hold:

- `git status` in the worktree shows nothing: no modified files, no untracked files, and no ignored files
  either (a fresh checkout has none, so any is the agent's output, such as a log under the git-excluded
  `.claude/state/` folder);
- the worktree's HEAD is still the commit it was created from (so no commits were added);
- git itself agrees to remove it (git refuses a dirty or locked worktree).

Anything else is **kept**. The kept path and the reason (`has_changes`, `head_moved_or_commits`, …) are
written to `.claude/logs/worktree-guard.jsonl` and printed on the hook's stderr. The agent's report also
names the worktree it worked in. Remove a kept worktree yourself after merging or discarding it:
`git worktree remove <path>`.

- The worktrees are created detached, so there is no branch to delete.
- A **snapshot** worktree (repository with no commits yet) is removed the same way. Its snapshot commit is
  on no branch or tag once the worktree is gone, and git's normal garbage collection deletes it later.
- An agent that ran **in place** (folder not a git repository) is never touched: there is no worktree,
  only your folder.
- A worktree that `worktree-guard` did not create is never touched either.

The per-launch records behind this live in `.claude/state/worktree-guard/` and are pruned after 30 days.

### When isolation was skipped

If an agent's definition declares `isolation: worktree` but no worktree was created for that launch, the
agent is warned at start (`ISOLATION SKIPPED`) to stop and report rather than edit the shared checkout.
This can happen when the agent was started as an agent-teams teammate, which runs in the main session's
directory. The check reads the definition from `.claude/agents/` or `~/.claude/agents/` by the agent's
`name`. It stays silent when the definition cannot be found (plugin agents, `--agents` JSON) and when
`worktree-guard` is not registered.

Isolation switches back to the normal case automatically: once the folder is a git repository you get
snapshot worktrees, and after the first commit you get ordinary worktrees of `HEAD`. Each decision is
logged in `.claude/logs/worktree-guard.jsonl` (`in_place`, `worktree_add_snapshot_no_head`,
`worktree_add_detached_head`).

Without such a hook, Claude Code cannot launch an `isolation: worktree` agent in either special case. A
repository with no commits fails with `Failed to resolve base branch "HEAD"`, and a non-git folder fails
with `not in a git repository and no WorktreeCreate hooks are configured`. If you disable `worktree-guard`
from the launcher, those projects lose their isolated agents until the repository has a commit.

## Merging worktree changes

After an agent with worktree isolation completes, if it made changes, its worktree is kept (path in the
agent's report and in `.claude/logs/worktree-guard.jsonl`). `worktree-guard` creates it on a detached
HEAD, so give the agent's commits a branch before merging:
```bash
# View what the agent changed (committed and uncommitted)
git -C <worktree> status
git -C <worktree> diff HEAD

# Keep it: commit in the worktree if needed, name the result, merge
git -C <worktree> switch -c agent-work      # or: git -C <worktree> commit ... first
git merge agent-work

# Clean up (after merging, or to discard)
git worktree remove <worktree>              # add --force to discard uncommitted work
git branch -D agent-work                    # if you created it
```

## Frontmatter syntax

```yaml
---
model: claude-sonnet-4-6
isolation: worktree
description: "Brief description of what this agent does"
---
```

The `isolation` field is supported directly by Claude Code's agent system.
