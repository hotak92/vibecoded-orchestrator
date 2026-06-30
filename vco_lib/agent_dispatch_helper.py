# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Parallel-agent dispatch helper — defensive worktree-isolation boilerplate (V52-AE).

The `isolation: worktree` worktree *creation* happens inside the Claude
Code binary (closed-source). However, the orchestrator CAN intercept the
spawn pathway via two harness hook events: ``WorktreeCreate`` (can-block;
receives the intended worktree path on stdin and must echo the absolute
path on stdout — so VCO can validate or override the path before the binary
uses it) and ``SubagentStart`` (cannot block, but can inject a loud warning
into the subagent's initial context). This boilerplate is the *prompt-level*
(Layer 2) defense; the hook-level (Layer 0) defense lives in
``templates/hooks/worktree-guard.sh`` (WorktreeCreate) and the
isolation-check in the SubagentStart hooks
(``templates/hooks/subagent-start-isolation-check.sh``), with a post-hoc
Layer 3b violation-alert in ``subagent-stop-reconcile.sh``. Prompt
boilerplate alone is LLM-discretionary and can be skipped or false-pass —
see the 2026-06-30 silent-fallback incident — so it must not be the only
line of defense.

Empirical evidence on this machine during V52-J Phase 1 (2026-06-09) showed
several subagents reporting "branch switched under me mid-session" + having
to cherry-pick to recover.

Root cause (likely):
    The harness DOES create per-agent worktrees (verified: this session
    is running in such a worktree right now). But agents can `cd` away
    from their worktree mid-session — e.g., to the parent's working
    directory — and then `git commit` lands on the parent's HEAD.
    When 2+ agents do this concurrently, they clobber each other's
    branch refs.

The fix is defensive — prepend a verification block to every
parallel-agent prompt that:

1. Runs ``git worktree list`` early in the agent's lifetime.
2. Asserts the agent's CWD matches one of the listed worktree paths.
3. Records the expected worktree path + branch name in a journal file
   so any later "branch switched" event can be diagnosed quickly.
4. Aborts BEFORE any ``git commit`` if the CWD doesn't match a worktree.

This module produces the boilerplate text. Callers (this assistant, when
spawning parallel agents) prepend it to the agent prompt.

References
~~~~~~~~~~
- v0.2.52 backlog §V52-AE
- v0.2.71 Track T-WT — hook-level safeguard (worktree-guard.sh on
  WorktreeCreate + SubagentStart isolation-check + SubagentStop Layer 3b)
- knowledge/concepts/parallel-agent-worktree-isolation-footgun-2026-06-09.md
- ``.claude/context/audits/worktree-isolation-safeguard-design-2026-06-30.md``
- ``feedback_subagent_explicit_base_commit.md`` (sibling rule: verify
  base SHA before acting; THIS rule: verify worktree CWD before
  committing)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


# Version of the boilerplate format. If you change the prompt template
# in a non-trivial way, bump this so downstream agents can detect
# version drift (e.g., older fan-outs running with the new dispatcher).
BOILERPLATE_VERSION = "v52-ae-1"


@dataclass(frozen=True)
class WorktreeIsolationDirective:
    """Inputs needed to render the defensive boilerplate.

    Attributes:
        agent_id:
            Short identifier for the agent (e.g., ``a9ad887``). Used to
            distinguish journal entries across parallel siblings.
        expected_branch:
            The git branch the agent is supposed to commit to. The
            boilerplate asserts the worktree's branch matches this OR a
            transient ``worktree-agent-<id>`` branch (which the harness
            creates by default; the agent then renames/checks out the
            intended branch within the worktree).
        base_commit:
            The expected base SHA the worktree was created at. The
            boilerplate asserts ``git merge-base HEAD <base>`` equals
            <base> — i.e., the worktree IS a descendant of the base, not
            a sibling.
        parent_checkout_path:
            The absolute path of the parent's checkout (the directory
            the orchestrator's main session is running in). The agent
            asserts its CWD is NOT this path — that's the failure mode.
        require_worktree_subdir:
            If True (default), require the agent's CWD to contain the
            substring ``/worktrees/`` OR ``/tmp/vco-`` (the two
            conventions used on this machine). Set False to relax for
            non-VCO callers.
    """

    agent_id: str
    expected_branch: Optional[str] = None
    base_commit: Optional[str] = None
    parent_checkout_path: Optional[str] = None
    require_worktree_subdir: bool = True


def render_verify_boilerplate(directive: WorktreeIsolationDirective) -> str:
    """Render the worktree-verify boilerplate for one parallel-agent prompt.

    Prepend this to the agent's task prompt. The agent will execute the
    verification steps BEFORE doing any commits.

    Args:
        directive: Inputs for the agent being dispatched.

    Returns:
        Markdown-formatted boilerplate text. Self-contained — caller
        does not need to add wrapper headings.
    """
    lines: List[str] = []
    lines.append(
        "## WORKTREE ISOLATION VERIFY (V52-AE defensive boilerplate, "
        f"`{BOILERPLATE_VERSION}`)"
    )
    lines.append("")
    lines.append(
        "Before doing ANY `git commit` (or any operation that mutates "
        "branch refs), you MUST verify you are operating inside an "
        "isolated git worktree, NOT the parent's primary checkout. If "
        "the harness's `isolation: worktree` did not put you in one, "
        "this prompt has a recovery procedure below."
    )
    lines.append("")
    lines.append("### Step 1 — Probe")
    lines.append("")
    lines.append("```bash")
    lines.append("pwd                       # record your CWD")
    lines.append("git worktree list         # one line per active worktree")
    lines.append("git branch --show-current # your active branch")
    lines.append("git log --oneline -1      # your HEAD")
    lines.append("```")
    lines.append("")
    lines.append("### Step 2 — Assertions (fail the run if ANY fail)")
    lines.append("")
    if directive.parent_checkout_path:
        lines.append(
            f"- Your CWD MUST NOT equal `{directive.parent_checkout_path}` "
            "(the parent's checkout)."
        )
    if directive.require_worktree_subdir:
        lines.append(
            "- Your CWD MUST contain either the substring `/worktrees/` "
            "(harness-created, e.g. `~/.claude/worktrees/agent-<id>/` or "
            "`<repo>/.claude/worktrees/agent-<id>/`) OR `/tmp/vco-` "
            "(manually-created, e.g. `/tmp/vco-v52ae-worktree`)."
        )
    if directive.expected_branch:
        lines.append(
            f"- Your active branch MUST be `{directive.expected_branch}` "
            f"OR a transient `worktree-agent-*` branch that you then "
            f"rename / checkout TO `{directive.expected_branch}` "
            "inside the worktree."
        )
    if directive.base_commit:
        lines.append(
            f"- `git merge-base HEAD {directive.base_commit}` MUST equal "
            f"`{directive.base_commit}` (i.e., your HEAD is a descendant "
            "of the expected base)."
        )
    lines.append(
        "- The output of `git worktree list` MUST contain your CWD as "
        "one of the lines. If it doesn't, you are NOT in a worktree at "
        "all — the harness silently dropped the isolation directive."
    )
    lines.append("")
    lines.append("### Step 3 — Recovery if any assertion fails")
    lines.append("")
    lines.append(
        "Create your own worktree explicitly, switch to it, and re-run "
        "all subsequent steps from there:"
    )
    lines.append("")
    lines.append("```bash")
    lines.append(
        "# Pick a unique path (suffix with your agent ID to avoid "
        "collisions with sibling agents):"
    )
    lines.append(
        f"WT=/tmp/vco-{directive.agent_id}-worktree-$(date +%s%N | "
        "head -c12)"
    )
    if directive.base_commit:
        lines.append(
            f"git worktree add \"$WT\" -b "
            f"chore/v0252-{directive.agent_id}-fallback "
            f"{directive.base_commit}"
        )
    else:
        lines.append(
            f"git worktree add \"$WT\" -b "
            f"chore/v0252-{directive.agent_id}-fallback"
        )
    lines.append("cd \"$WT\"")
    lines.append("git worktree list  # verify the new worktree is present")
    lines.append("```")
    lines.append("")
    lines.append("### Step 4 — Journal the result")
    lines.append("")
    lines.append(
        f"Write a one-line confirmation to your progress journal: "
        f"`worktree_verified: agent_id={directive.agent_id} "
        "cwd=<your-cwd> branch=<active-branch> head=<head-sha>`. This "
        "lets the integrator diagnose any later branch-switch event in "
        "seconds."
    )
    lines.append("")
    lines.append("### Step 5 — DO NOT `cd` away from the worktree")
    lines.append("")
    lines.append(
        "Every subsequent `git` command in your run MUST execute from "
        "the worktree's CWD. If you spawn a sub-shell or run a script "
        "that does `cd /home/...orchestrator-clone`, your `git commit` "
        "WILL land on the parent's HEAD and clobber sibling agents. "
        "Prefer absolute paths to files; never use `cd` outside the "
        "worktree."
    )
    lines.append("")
    lines.append(
        "### Why this matters (one-liner): "
        "parallel agents on the same primary checkout race `HEAD` — "
        "every commit a sibling makes can swap your branch under your "
        "feet, and your `git commit` lands on the wrong branch."
    )
    return "\n".join(lines)


def render_pre_dispatch_assertion(
    directives: List[WorktreeIsolationDirective],
) -> str:
    """Render a sanity block to print BEFORE dispatching N parallel agents.

    The caller (this assistant) runs this block in its OWN session
    before spawning the fan-out. It checks the *parent's* state — e.g.,
    that the parent has a clean working directory, that no stale
    `worktrees/agent-*` directories exist from a prior crashed run, etc.

    Args:
        directives: One directive per agent to be dispatched.

    Returns:
        Bash-block-friendly text the caller can paste into a single
        Bash tool call.
    """
    n = len(directives)
    ids = ", ".join(d.agent_id for d in directives)
    lines: List[str] = []
    lines.append(
        f"# V52-AE pre-dispatch sanity ({BOILERPLATE_VERSION}): "
        f"about to spawn {n} parallel agents ({ids})."
    )
    lines.append("git status --porcelain  # parent's working tree should be clean")
    lines.append(
        "git worktree list  # baseline; after dispatch this should "
        f"grow by exactly {n} lines"
    )
    lines.append(
        "find .claude/worktrees/ -maxdepth 1 -name 'agent-*' "
        "-mtime +1 2>/dev/null  # warn on stale harness worktrees "
        "older than 1d — they might confuse the new spawn"
    )
    return "\n".join(lines)


def render_post_dispatch_audit(
    expected_count: int,
    expected_agent_ids: List[str],
) -> str:
    """Render a sanity block to run AFTER all parallel agents complete.

    Verifies that each expected agent's branch exists + diverges from
    the parent's HEAD. Catches the failure mode where two siblings
    accidentally landed on the same branch.

    Args:
        expected_count: Number of agents that were dispatched.
        expected_agent_ids: IDs in dispatch order.

    Returns:
        Bash-block text.
    """
    lines: List[str] = []
    lines.append(
        f"# V52-AE post-dispatch audit ({BOILERPLATE_VERSION}): "
        f"verify each of {expected_count} agents landed on a distinct branch."
    )
    lines.append(
        "git worktree list  # should contain "
        f"{expected_count} agent worktrees + the parent"
    )
    for agent_id in expected_agent_ids:
        lines.append(
            f"git branch --list '*{agent_id}*'  # agent {agent_id}'s "
            "branch ref must exist"
        )
    lines.append(
        "# If any branch is missing → the harness's worktree isolation "
        "failed for that agent; recover by cherry-picking the agent's "
        "commits onto a fresh branch."
    )
    return "\n".join(lines)


__all__ = [
    "BOILERPLATE_VERSION",
    "WorktreeIsolationDirective",
    "render_verify_boilerplate",
    "render_pre_dispatch_assertion",
    "render_post_dispatch_audit",
]
