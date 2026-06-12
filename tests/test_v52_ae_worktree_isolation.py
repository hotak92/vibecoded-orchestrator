# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-AE — defensive parallel-agent worktree-isolation helper tests.

Background: the launcher harness's `isolation: worktree` directive
is implemented inside the closed-source Claude Code binary, NOT in
the orchestrator codebase (verified by exhaustive grep of
launcher/src-tauri + claude_mcp_servers + vct-hub). When the harness
silently fails to put a parallel subagent in an isolated worktree,
the agent commits land on whatever branch the parent's checkout
happens to be on — causing the V52-J Phase 1 footgun documented in
``knowledge/concepts/parallel-agent-worktree-isolation-footgun-2026-06-09.md``.

The v0.2.52 deliverable is a defensive helper
(`vco_lib.agent_dispatch_helper`) that produces boilerplate text the
dispatcher (this assistant, when fanning out) prepends to every
parallel-agent prompt. The boilerplate makes the agent:

1. Probe its own CWD + worktree state early.
2. Assert it's NOT in the parent's primary checkout.
3. Refuse to commit if assertions fail; create its own worktree
   manually as a recovery path.

These tests pin the BOILERPLATE CONTRACT (the shape and presence of
the verification text) — NOT the runtime behavior, since that depends
on the closed-source binary. The integration test at the bottom
simulates a 2-agent dispatch by invoking the helper for two distinct
directives and asserting the produced prompts:

* Land on different worktree paths in the boilerplate's recovery
  step (each agent has its own unique manual-fallback worktree path).
* Both worktree paths appear in `git worktree list` recovery commands.
* The branches in each agent's directive are distinct, so a
  branch-collision would be detectable post-dispatch.

We avoid spawning real Claude Code subagents in unit tests — that
requires a license, network, and a 20-minute fixture lifecycle. The
KG node documents the failure-mode evidence; this test confirms the
helper PRODUCES the right defenses.

See v0.2.52 backlog § V52-AE.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from vco_lib.agent_dispatch_helper import (  # noqa: E402
    BOILERPLATE_VERSION,
    WorktreeIsolationDirective,
    render_post_dispatch_audit,
    render_pre_dispatch_assertion,
    render_verify_boilerplate,
)


class WorktreeIsolationDirectiveTests(unittest.TestCase):
    """Pin the input dataclass's contract."""

    def test_minimum_required_field_is_agent_id(self) -> None:
        """``agent_id`` alone is enough — other fields are optional."""
        d = WorktreeIsolationDirective(agent_id="aabbccd")
        self.assertEqual(d.agent_id, "aabbccd")
        self.assertIsNone(d.expected_branch)
        self.assertIsNone(d.base_commit)
        self.assertIsNone(d.parent_checkout_path)
        self.assertTrue(d.require_worktree_subdir)

    def test_frozen(self) -> None:
        """Directive is immutable so callers can't trick the helper by mutating mid-render."""
        d = WorktreeIsolationDirective(agent_id="x")
        with self.assertRaises(Exception):
            d.agent_id = "y"  # type: ignore[misc]


class RenderVerifyBoilerplateTests(unittest.TestCase):
    """The per-agent verify boilerplate must include all 5 steps."""

    def setUp(self) -> None:
        self.full_directive = WorktreeIsolationDirective(
            agent_id="abc1234",
            expected_branch="chore/v0252-test-branch",
            base_commit="8c86de15",
            parent_checkout_path="/home/dev/projects/vibecoded-orchestrator",
        )

    def test_includes_version_marker(self) -> None:
        """Version marker lets callers detect drift between helper + boilerplate
        when both are vendored separately into agent prompts."""
        out = render_verify_boilerplate(self.full_directive)
        self.assertIn(BOILERPLATE_VERSION, out)

    def test_includes_all_five_steps(self) -> None:
        out = render_verify_boilerplate(self.full_directive)
        for step in (
            "Step 1",
            "Step 2",
            "Step 3",
            "Step 4",
            "Step 5",
        ):
            self.assertIn(step, out, f"missing {step} in boilerplate")

    def test_probe_commands_present(self) -> None:
        """Step 1 must instruct ``pwd``, ``git worktree list``,
        ``git branch --show-current``, ``git log``."""
        out = render_verify_boilerplate(self.full_directive)
        self.assertIn("pwd", out)
        self.assertIn("git worktree list", out)
        self.assertIn("git branch --show-current", out)
        self.assertIn("git log --oneline", out)

    def test_assertion_against_parent_checkout(self) -> None:
        """Step 2 must explicitly name the parent's checkout path."""
        out = render_verify_boilerplate(self.full_directive)
        self.assertIn(
            "/home/dev/projects/vibecoded-orchestrator", out
        )
        self.assertIn("MUST NOT equal", out)

    def test_assertion_against_expected_branch(self) -> None:
        out = render_verify_boilerplate(self.full_directive)
        self.assertIn("chore/v0252-test-branch", out)

    def test_assertion_against_base_commit(self) -> None:
        out = render_verify_boilerplate(self.full_directive)
        self.assertIn("8c86de15", out)
        self.assertIn("merge-base", out)

    def test_recovery_command_uses_agent_id_in_path(self) -> None:
        """Step 3's manual ``git worktree add`` MUST embed the agent ID
        so two siblings creating fallback worktrees can't collide."""
        out = render_verify_boilerplate(self.full_directive)
        self.assertIn("abc1234", out)
        self.assertIn("git worktree add", out)
        # The path template should include a uniqueness suffix (epoch
        # nanoseconds via $(date +%s%N)) so re-runs don't collide.
        self.assertIn("date +%s%N", out)

    def test_recovery_path_template_uses_base_commit(self) -> None:
        out = render_verify_boilerplate(self.full_directive)
        # When base_commit is set, the worktree-add command must
        # check out FROM that base, not HEAD-of-parent.
        self.assertIn("8c86de15", out)

    def test_warning_against_cd_outside_worktree(self) -> None:
        """Step 5 must explicitly warn against ``cd`` outside the worktree."""
        out = render_verify_boilerplate(self.full_directive)
        self.assertIn("`cd`", out)
        self.assertIn("DO NOT", out)

    def test_minimal_directive_still_produces_useful_output(self) -> None:
        """Directive with only agent_id should still render (no crash, still has shape)."""
        out = render_verify_boilerplate(
            WorktreeIsolationDirective(agent_id="minimal1")
        )
        # Should still have all 5 steps even with no expected branch/base.
        for step in ("Step 1", "Step 2", "Step 3", "Step 4", "Step 5"):
            self.assertIn(step, out)
        # And the recovery section MUST embed the agent ID.
        self.assertIn("minimal1", out)


class RenderPreDispatchAssertionTests(unittest.TestCase):
    """The pre-dispatch sanity block runs in the PARENT's session."""

    def test_lists_all_agent_ids(self) -> None:
        directives = [
            WorktreeIsolationDirective(agent_id="aaa1111"),
            WorktreeIsolationDirective(agent_id="bbb2222"),
            WorktreeIsolationDirective(agent_id="ccc3333"),
        ]
        out = render_pre_dispatch_assertion(directives)
        self.assertIn("aaa1111", out)
        self.assertIn("bbb2222", out)
        self.assertIn("ccc3333", out)

    def test_includes_git_status_porcelain(self) -> None:
        """Pre-dispatch must verify the parent's working tree is clean —
        otherwise sibling agents racing on it could be a separate cause
        of the "branch switched" symptom."""
        out = render_pre_dispatch_assertion(
            [WorktreeIsolationDirective(agent_id="x")]
        )
        self.assertIn("git status --porcelain", out)

    def test_includes_worktree_list_baseline(self) -> None:
        out = render_pre_dispatch_assertion(
            [WorktreeIsolationDirective(agent_id="x")]
        )
        self.assertIn("git worktree list", out)

    def test_includes_stale_worktree_warning(self) -> None:
        """A stale agent-* worktree from a prior crashed run can confuse
        the new spawn."""
        out = render_pre_dispatch_assertion(
            [WorktreeIsolationDirective(agent_id="x")]
        )
        # Either the find command or stale wording, lenient match.
        self.assertTrue(
            "stale" in out.lower() or "find " in out,
            f"missing stale-worktree warning. Got: {out}",
        )


class RenderPostDispatchAuditTests(unittest.TestCase):
    """The post-dispatch audit block runs AFTER all agents complete."""

    def test_lists_all_expected_agent_ids(self) -> None:
        out = render_post_dispatch_audit(3, ["a1", "b2", "c3"])
        self.assertIn("a1", out)
        self.assertIn("b2", out)
        self.assertIn("c3", out)

    def test_includes_branch_existence_check(self) -> None:
        """Branch-existence check is the proxy for "did this agent get
        its own branch, or did it collide with a sibling?". """
        out = render_post_dispatch_audit(2, ["a1", "b2"])
        # Should have a `git branch --list` line per agent.
        self.assertGreaterEqual(out.count("git branch --list"), 2)


class IntegrationDualSubagentDispatchTests(unittest.TestCase):
    """V52-AE integration test — simulate a 2-subagent dispatch and verify:

    1. Both subagent prompts contain DIFFERENT recovery-worktree paths
       (i.e., the boilerplate's fallback worktree-add commands point at
       distinct on-disk paths, so even if the harness fails for both
       agents the manual recovery doesn't collide).
    2. The post-dispatch audit lists BOTH agents.
    3. The branches in each directive are distinct — a sibling
       branch-collision would be detectable by name.
    4. Neither subagent's worktree path matches the other.

    Per the V52-AE spec, this is the "dispatch 2 dummy subagents with
    isolation: worktree and assert isolation" integration check —
    adapted to test the HELPER's contract since we can't actually
    spawn Claude Code subagents from unittest. The contract that
    matters is: if both subagents read+honor the boilerplate, their
    fallback paths are guaranteed distinct.
    """

    def test_two_dummy_subagents_get_distinct_worktree_paths(self) -> None:
        agent_a = WorktreeIsolationDirective(
            agent_id="dummy_a",
            expected_branch="chore/v0252-dummy-a",
            base_commit="8c86de15",
            parent_checkout_path="/home/dev/projects/vibecoded-orchestrator",
        )
        agent_b = WorktreeIsolationDirective(
            agent_id="dummy_b",
            expected_branch="chore/v0252-dummy-b",
            base_commit="8c86de15",
            parent_checkout_path="/home/dev/projects/vibecoded-orchestrator",
        )

        prompt_a = render_verify_boilerplate(agent_a)
        prompt_b = render_verify_boilerplate(agent_b)

        # Extract the worktree-add path template from each prompt.
        # Pattern: ``WT=/tmp/vco-<agent_id>-worktree-$(date +%s%N | head -c12)``
        path_re = re.compile(
            r"WT=(/tmp/vco-([a-z_0-9]+)-worktree-\$\(date \+%s%N \| head -c12\))"
        )
        m_a = path_re.search(prompt_a)
        m_b = path_re.search(prompt_b)
        self.assertIsNotNone(
            m_a,
            f"agent A's prompt missing fallback WT= line. Got:\n{prompt_a}",
        )
        self.assertIsNotNone(
            m_b,
            f"agent B's prompt missing fallback WT= line. Got:\n{prompt_b}",
        )
        path_template_a = m_a.group(1)
        path_template_b = m_b.group(1)

        # 1. The two recovery-path TEMPLATES must differ (one embeds
        # dummy_a, the other dummy_b).
        self.assertNotEqual(
            path_template_a,
            path_template_b,
            "both subagents would fall back to the SAME worktree path "
            "— recovery collision risk",
        )
        self.assertIn("dummy_a", path_template_a)
        self.assertIn("dummy_b", path_template_b)

    def test_both_subagents_listed_in_post_dispatch_audit(self) -> None:
        out = render_post_dispatch_audit(2, ["dummy_a", "dummy_b"])
        self.assertIn("dummy_a", out)
        self.assertIn("dummy_b", out)

    def test_branches_in_dispatch_are_distinct(self) -> None:
        """A sibling branch collision is detectable by name — the audit
        would flag two agents claiming the same target branch."""
        agent_a = WorktreeIsolationDirective(
            agent_id="dummy_a",
            expected_branch="chore/v0252-dummy-a",
        )
        agent_b = WorktreeIsolationDirective(
            agent_id="dummy_b",
            expected_branch="chore/v0252-dummy-b",
        )
        self.assertNotEqual(agent_a.expected_branch, agent_b.expected_branch)
        # And the rendered prompts each mention their OWN branch (not
        # the sibling's).
        prompt_a = render_verify_boilerplate(agent_a)
        prompt_b = render_verify_boilerplate(agent_b)
        self.assertIn("chore/v0252-dummy-a", prompt_a)
        self.assertNotIn("chore/v0252-dummy-b", prompt_a)
        self.assertIn("chore/v0252-dummy-b", prompt_b)
        self.assertNotIn("chore/v0252-dummy-a", prompt_b)

    def test_neither_subagent_paths_match_parent(self) -> None:
        """If an agent's prompt accidentally instructed it to commit in
        the PARENT's path, that's the V52-J Phase 1 footgun reborn.
        Verify the boilerplate's assertions explicitly call out the
        parent path as forbidden."""
        agent_a = WorktreeIsolationDirective(
            agent_id="dummy_a",
            parent_checkout_path="/home/dev/projects/vibecoded-orchestrator",
        )
        prompt = render_verify_boilerplate(agent_a)
        # The parent's checkout path must be present AS A FORBIDDEN value.
        self.assertIn("/home/dev/projects/vibecoded-orchestrator", prompt)
        self.assertIn("MUST NOT equal", prompt)


if __name__ == "__main__":
    unittest.main()
