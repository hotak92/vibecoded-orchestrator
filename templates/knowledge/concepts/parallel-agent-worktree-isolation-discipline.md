---
title: Parallel-agent worktree isolation discipline
type: concept
tags: [mid-level-architecture, orchestration, agents, gotcha, claude-code, harness, worktree, lessons-learned]
created: 2026-06-09T00:00:00Z
updated: 2026-07-20T00:00:00Z
valid_from: 2026-06-09T00:00:00Z
valid_until: null
status: active
---

# Parallel-agent worktree isolation discipline

When dispatching multiple Claude Code subagents in parallel via the Agent
tool with `isolation: worktree`, every agent MUST operate in a physically
separate git checkout directory for the full lifetime of its run.
Otherwise concurrent `git commit` / `git checkout` calls clobber each
other and the integrator pays cherry-pick + force-reset costs.

The harness *should* enforce this — `isolation: worktree` in agent
frontmatter is meant to make Claude Code call `git worktree add` per
subagent. Empirically the harness honors this on this machine (each
subagent gets its own `worktree-agent-<id>` branch + checkout
directory). But the *agent itself* can defeat the isolation by `cd`-ing
to the parent's checkout mid-session and committing there.

## Failure mode (observed 2026-06-09)

Five Opus subagents dispatched in parallel for the v0.2.52 work. Several
reported variants of:

- "twice the working-copy branch switched under me mid-session"
- "had to cherry-pick + force-reset to land 5 commits on a clean branch"
- Commits landing on a branch the agent's prompt did NOT name
- `git stash list` showing entries the agent never created

Root cause: agents `cd`-ed away from their isolated worktrees (e.g., to
the parent's working dir) and then `git commit`'d there. With ≥2 agents
doing this concurrently, `HEAD` swaps under each other.

## Three layers of defense (use ALL of them)

### Layer 1 — Trust `isolation: worktree`

When the harness honors it (default on Claude Code v2.1.x), each agent
gets its own `worktree-agent-<id>` branch and CWD. Verify by checking
the agent's `git worktree list` and `pwd` early in its session.

### Layer 2 — Defensive prompt boilerplate

Every parallel-dispatch prompt MUST include a verify-or-abort block:

```text
## WORKTREE ISOLATION VERIFY

Before any `git commit`:

1. Run `pwd`, `git worktree list`, `git branch --show-current`.
2. Assert your CWD is NOT the parent's primary checkout path.
3. Assert your CWD contains `/worktrees/` or `/tmp/vco-` (per the
   conventions on this machine).
4. Assert the branch is your expected target (or a transient
   `worktree-agent-*` ref).
5. If any assertion fails: create your own worktree via
   `git worktree add /tmp/vco-<agent_id>-worktree-$(date +%s%N)` and
   work there.
6. DO NOT `cd` outside the worktree. Use absolute paths to files.
```

The helper `vco_lib/agent_dispatch_helper.py` renders this boilerplate
from a `WorktreeIsolationDirective`. Callers pass the agent ID,
expected branch, base commit, parent's checkout path.

### Layer 3 — Pre/post-dispatch audits in the parent

Before the fan-out, the parent (this assistant) prints:

```bash
git status --porcelain       # parent's working tree clean
git worktree list            # baseline count
find .claude/worktrees/ -maxdepth 1 -name 'agent-*' -mtime +1
```

After fan-out completes, audit:

```bash
git worktree list             # baseline + N agents
git branch --list '*<agent_id>*'   # one per dispatched agent
```

If branches are missing or count is wrong → that agent's isolation
failed; cherry-pick its commits manually.

## When NOT to worry

- Single-agent dispatches — no contention.
- Multi-agent READ-ONLY dispatches (audits, surveys) — no commits.
- Time-separated dispatches (dispatch, wait, dispatch) — but loses
  parallelism's benefit.

## Why this isn't the same as base-SHA discipline

A sibling rule documents the "stale-base-SHA" failure (agents pick a
v0.2.21-era SHA as their base when the cluster is on v0.2.38). That's
a CONFUSION-about-starting-point bug — agents read from stale workspace
state. THIS rule is different: agents are dispatched with correct base
SHAs but the harness or the agent itself puts them all in the same
checkout, so their work clobbers each other AFTER they've correctly
identified the start point.

Both rules apply simultaneously to every parallel-dispatch prompt:
- (old rule) verify-or-abort the base SHA
- (new rule) verify-or-abort the worktree CWD

## Symptoms that diagnose the bug

Suspect this footgun if any of these appear in a subagent's reply:
- "branch switched under me"
- "had to cherry-pick to recover"
- "unstaged changes from another agent"
- Commits landing on a branch the agent's prompt did NOT name
- `git stash list` showing entries the agent never created

## Implementation reference

- Helper: `vco_lib/agent_dispatch_helper.py`
- Tests: `tests/test_v52_ae_worktree_isolation.py`
- Documentation: `templates/agents/WORKTREE_ISOLATION_GUIDE.md`
- Frontmatter: `templates/agents/free/*.md` (12 agents declare
  `isolation: worktree`)

## Related

- [[relatedTo::Agent Orchestration]] — parent doc for multi-agent dispatch patterns
- [[relatedTo::Agentic LLM Workflows]] — coordination patterns across agents
- v0.2.52 backlog § V52-AE (in-repo only; private operational plan)
