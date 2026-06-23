# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for Track C (v0.2.65): per-session CONTEXT_STATE files.

Concurrent long-lived chats against the same project (e.g. a main chat + an
RL chat) each keep their own session_id and would otherwise clobber one
shared .claude/CONTEXT_STATE.md (last-writer-wins). Track C lets each session
keep its OWN .claude/context/CONTEXT_STATE_<session_id>.md, picked up by the
four context hooks automatically once it exists:

  - diff-context-inject.{sh,ps1}        (UserPromptSubmit)
  - compact-context-reinject.{sh,ps1}   (SessionStart matcher=compact)
  - context-size-check.{sh,ps1}         (SessionStart)
  - post-compact.{sh,ps1}               (PostCompact)

The shared CONTEXT_STATE.md rollup behaviour is UNCHANGED. Every per-session
addition is gated on the per-session file existing, so single-session projects
behave byte-identically to before this change (zero-impact).

These tests cover:
  - bash -n syntax on every touched .sh
  - .sh/.ps1 sibling parity (the touched hooks all keep their PS1 sibling)
  - Zero-impact: NO per-session file => behaviour identical to today
  - Per-session file present => its own diff / reinject / size-check on the
    `_session_` key
  - Two different session_ids do NOT cross-contaminate baselines
  - post-compact resets the per-session diff baseline but NEVER the content file
  - GC sweeps the per-session snapshot baseline but NEVER the content file
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = REPO_ROOT / "templates" / "hooks"

# The four hooks Track C touches (each has a .sh + .ps1 sibling).
TOUCHED_HOOKS = [
    "diff-context-inject",
    "compact-context-reinject",
    "context-size-check",
    "post-compact",
]


def _bash() -> str | None:
    return shutil.which("bash")


class BashSyntaxCheck(unittest.TestCase):
    """bash -n parses each touched .sh hook without errors."""

    def test_all_sh_pass_bash_n(self) -> None:
        bash = _bash()
        if not bash:
            self.skipTest("bash not on PATH")
        for name in TOUCHED_HOOKS:
            p = HOOK_DIR / f"{name}.sh"
            with self.subTest(hook=name):
                self.assertTrue(p.is_file(), f"missing .sh hook: {p}")
                result = subprocess.run(
                    [bash, "-n", str(p)], capture_output=True, text=True
                )
                self.assertEqual(
                    result.returncode, 0,
                    f"{p}: bash -n failed:\nstdout: {result.stdout}\n"
                    f"stderr: {result.stderr}",
                )


class SiblingParity(unittest.TestCase):
    """Every touched .sh keeps its .ps1 sibling (hook-os-parity discipline)."""

    def test_ps1_siblings_exist(self) -> None:
        for name in TOUCHED_HOOKS:
            sh = HOOK_DIR / f"{name}.sh"
            ps1 = HOOK_DIR / f"{name}.ps1"
            with self.subTest(hook=name):
                self.assertTrue(sh.is_file(), f"missing .sh: {sh}")
                self.assertTrue(
                    ps1.is_file(),
                    f"missing .ps1 sibling for {name}: {ps1} "
                    "(multi-OS sibling discipline)",
                )

    def test_ps1_siblings_reference_session_key(self) -> None:
        """Each .ps1 sibling must reference the `ctx_snapshot_session_` key
        OR the per-session CONTEXT_STATE path, mirroring its .sh sibling.
        """
        expected = {
            "diff-context-inject": "ctx_snapshot_session_",
            "compact-context-reinject": "CONTEXT_STATE_",
            "context-size-check": "CONTEXT_STATE_",
            "post-compact": "ctx_snapshot_session_",
        }
        for name, token in expected.items():
            ps1 = HOOK_DIR / f"{name}.ps1"
            text = ps1.read_text(encoding="utf-8")
            with self.subTest(hook=name):
                self.assertIn(
                    token, text,
                    f"{ps1}: PS1 sibling must reference '{token}' "
                    "to stay in parity with its .sh sibling",
                )


class _HookHarness(unittest.TestCase):
    """Shared scaffolding: a throwaway project dir + a hook runner."""

    def setUp(self) -> None:
        self.bash = _bash()
        if not self.bash:
            self.skipTest("bash not on PATH")
        self.tmp = tempfile.mkdtemp(prefix="v0265_persession_")
        self.proj = Path(self.tmp)
        self.claude = self.proj / ".claude"
        self.context_dir = self.claude / "context"
        self.state_dir = self.claude / "state"
        self.context_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.rollup = self.claude / "CONTEXT_STATE.md"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_hook(self, hook: str, session_id: str | None,
                 extra_payload: dict | None = None) -> tuple[int, str, str]:
        """Run a hook with a synthesized stdin payload, CWD = project dir."""
        payload: dict = {}
        if session_id is not None:
            payload["session_id"] = session_id
        if extra_payload:
            payload.update(extra_payload)
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = str(self.proj)
        env.pop("VCT_DISABLE_HOOKS", None)
        result = subprocess.run(
            [self.bash, str(HOOK_DIR / f"{hook}.sh")],
            input=json.dumps(payload),
            capture_output=True, text=True,
            cwd=str(self.proj), env=env, timeout=20,
        )
        return result.returncode, result.stdout, result.stderr

    def session_file(self, session_id: str) -> Path:
        return self.context_dir / f"CONTEXT_STATE_{session_id}.md"

    def session_snapshot(self, session_id: str) -> Path:
        return self.state_dir / f"ctx_snapshot_session_{session_id}"

    def rollup_snapshot(self, session_id: str) -> Path:
        return self.state_dir / f"ctx_snapshot_{session_id}"


class ZeroImpactWhenNoPerSessionFile(_HookHarness):
    """No per-session file present => behaviour identical to pre-Track-C.

    The shared rollup diff still works; no `_session_` artifacts are created.
    """

    def test_diff_inject_no_session_file_creates_only_rollup_baseline(self) -> None:
        self.rollup.write_text("## Goal\nbuild thing\n## Status\nWIP\n")
        rc, out, err = self.run_hook("diff-context-inject", "sessZERO")
        self.assertEqual(rc, 0, err)
        # Rollup baseline created (first prompt) — original behaviour.
        self.assertTrue(self.rollup_snapshot("sessZERO").is_file())
        # NO per-session snapshot baseline (the file never existed).
        self.assertFalse(
            self.session_snapshot("sessZERO").exists(),
            "no per-session file => no ctx_snapshot_session_ baseline",
        )
        # First prompt is silent (baseline only).
        self.assertEqual(out.strip(), "")

    def test_diff_inject_no_session_file_emits_only_context_label(self) -> None:
        """After a rollup edit, only the shared 'Context' diff is emitted —
        never a 'Session context' block (there is no per-session file).
        """
        self.rollup.write_text("## Goal\nbuild thing\n## Status\nWIP\n")
        self.run_hook("diff-context-inject", "sessZERO")  # baseline
        self.rollup.write_text("## Goal\nbuild thing\n## Status\nDONE\n")
        rc, out, err = self.run_hook("diff-context-inject", "sessZERO")
        self.assertEqual(rc, 0, err)
        self.assertIn("Context update", out)
        self.assertNotIn("Session context", out)

    def test_size_check_no_session_file_only_checks_rollup(self) -> None:
        big = "## Section\n" + ("filler line\n" * 500)
        self.rollup.write_text(big)
        rc, out, err = self.run_hook("context-size-check", "sessZERO")
        self.assertEqual(rc, 0, err)
        self.assertIn("CONTEXT_STATE.md Size Alert", out)
        # No per-session-keyed alert (no per-session file).
        self.assertNotIn("CONTEXT_STATE_sessZERO.md", out)

    def test_compact_reinject_no_session_file_only_rollup(self) -> None:
        self.rollup.write_text("## Goal\nship it\n")
        rc, out, err = self.run_hook("compact-context-reinject", "sessZERO")
        self.assertEqual(rc, 0, err)
        self.assertIn("Current Task State (re-injected after compaction)", out)
        self.assertIn("ship it", out)
        self.assertNotIn("This Session's Task State", out)


class PerSessionFilePickedUp(_HookHarness):
    """Per-session file present => it gets its own diff / reinject / size-check."""

    def test_diff_inject_creates_session_baseline_on_first_prompt(self) -> None:
        self.rollup.write_text("## Goal\nrollup\n")
        self.session_file("sessA").write_text("## RL Task\ntune reranker\n")
        rc, out, err = self.run_hook("diff-context-inject", "sessA")
        self.assertEqual(rc, 0, err)
        # Both baselines created on first prompt; both silent.
        self.assertTrue(self.rollup_snapshot("sessA").is_file())
        self.assertTrue(
            self.session_snapshot("sessA").is_file(),
            "per-session file present => ctx_snapshot_session_ baseline created",
        )
        self.assertEqual(out.strip(), "")

    def test_diff_inject_emits_session_context_block_on_change(self) -> None:
        self.rollup.write_text("## Goal\nrollup\n")
        self.session_file("sessA").write_text("## RL Task\ntune reranker\n")
        self.run_hook("diff-context-inject", "sessA")  # baselines
        # Change ONLY the per-session file.
        self.session_file("sessA").write_text("## RL Task\ntune reranker HARDER\n")
        rc, out, err = self.run_hook("diff-context-inject", "sessA")
        self.assertEqual(rc, 0, err)
        self.assertIn("Session context update", out)
        self.assertIn("HARDER", out)
        # The shared rollup didn't change => no 'Context update' header.
        self.assertNotIn("Context update — changed", out)

    def test_size_check_flags_oversized_session_file(self) -> None:
        self.rollup.write_text("## small\nok\n")
        big = "## Section\n" + ("filler line\n" * 500)
        self.session_file("sessA").write_text(big)
        rc, out, err = self.run_hook("context-size-check", "sessA")
        self.assertEqual(rc, 0, err)
        self.assertIn("CONTEXT_STATE_sessA.md Size Alert", out)

    def test_compact_reinject_includes_session_file(self) -> None:
        self.rollup.write_text("## Goal\nrollup state\n")
        self.session_file("sessA").write_text("## RL Task\nthis session only\n")
        rc, out, err = self.run_hook("compact-context-reinject", "sessA")
        self.assertEqual(rc, 0, err)
        self.assertIn("Current Task State (re-injected after compaction)", out)
        self.assertIn("rollup state", out)
        self.assertIn("This Session's Task State", out)
        self.assertIn("this session only", out)

    def test_compact_reinject_session_file_capped_at_120_lines(self) -> None:
        """The per-session reinject sub-cap (120 lines) is respected."""
        self.rollup.write_text("## Goal\nshort\n")
        # 300 distinct numbered lines; only the first 120 should appear.
        lines = "".join(f"line-{i}\n" for i in range(300))
        self.session_file("sessA").write_text(lines)
        rc, out, err = self.run_hook("compact-context-reinject", "sessA")
        self.assertEqual(rc, 0, err)
        self.assertIn("line-0\n", out)
        self.assertIn("line-119\n", out)
        self.assertNotIn("line-120\n", out)
        self.assertNotIn("line-200\n", out)


class NoCrossContamination(_HookHarness):
    """Two different session_ids keep independent baselines."""

    def test_two_sessions_independent_baselines(self) -> None:
        self.rollup.write_text("## Goal\nshared rollup\n")
        self.session_file("sessA").write_text("## A\nalpha\n")
        self.session_file("sessB").write_text("## B\nbeta\n")

        # Prime both sessions' baselines.
        self.run_hook("diff-context-inject", "sessA")
        self.run_hook("diff-context-inject", "sessB")
        self.assertTrue(self.session_snapshot("sessA").is_file())
        self.assertTrue(self.session_snapshot("sessB").is_file())

        # Change ONLY session A's file. Session B's hook run must NOT see it.
        self.session_file("sessA").write_text("## A\nalpha CHANGED\n")

        # Running B's hook: B's file unchanged => no 'Session context' block,
        # and B must not surface A's content.
        rc_b, out_b, err_b = self.run_hook("diff-context-inject", "sessB")
        self.assertEqual(rc_b, 0, err_b)
        self.assertNotIn("CHANGED", out_b)
        self.assertNotIn("Session context update", out_b)

        # Running A's hook: A's file changed => its diff is emitted.
        rc_a, out_a, err_a = self.run_hook("diff-context-inject", "sessA")
        self.assertEqual(rc_a, 0, err_a)
        self.assertIn("Session context update", out_a)
        self.assertIn("CHANGED", out_a)
        self.assertNotIn("beta", out_a)


class PostCompactResetsBaselineNotContent(_HookHarness):
    """post-compact wipes the per-session diff baseline, never the content."""

    def test_post_compact_resets_session_snapshot_keeps_content(self) -> None:
        self.rollup.write_text("## Goal\nrollup\n")
        self.session_file("sessA").write_text("## RL Task\ncurated by user\n")
        # Prime baselines.
        self.run_hook("diff-context-inject", "sessA")
        self.assertTrue(self.session_snapshot("sessA").is_file())

        rc, out, err = self.run_hook("post-compact", "sessA",
                                     extra_payload={"trigger": "manual"})
        self.assertEqual(rc, 0, err)
        # Throwaway baseline gone.
        self.assertFalse(
            self.session_snapshot("sessA").exists(),
            "post-compact must reset the per-session diff baseline",
        )
        # Content file PRESERVED (may be user-curated).
        self.assertTrue(
            self.session_file("sessA").is_file(),
            "post-compact must NEVER delete the per-session CONTEXT_STATE file",
        )
        self.assertIn("curated by user",
                      self.session_file("sessA").read_text())


class GcSweepsBaselineNotContent(_HookHarness):
    """The 14-day GC sweeps stale per-session baselines, never content files."""

    def test_gc_removes_stale_session_baseline_keeps_content(self) -> None:
        self.rollup.write_text("## Goal\nrollup\n")
        self.session_file("sessOLD").write_text("## old\ncontent\n")

        # Fabricate a stale per-session baseline (mtime 20 days ago).
        stale = self.session_snapshot("sessOLD")
        stale.write_text("old baseline\n")
        old_epoch = time.time() - (20 * 86400)
        os.utime(stale, (old_epoch, old_epoch))

        # A fresh hook run for a DIFFERENT session triggers the GC sweep.
        rc, out, err = self.run_hook("diff-context-inject", "sessFRESH")
        self.assertEqual(rc, 0, err)

        self.assertFalse(
            stale.exists(),
            "14-day GC must sweep stale ctx_snapshot_session_ baselines",
        )
        # Content file untouched by GC.
        self.assertTrue(
            self.session_file("sessOLD").is_file(),
            "GC must NEVER delete per-session CONTEXT_STATE content files",
        )


class TemplateDocumentsConvention(unittest.TestCase):
    """The ORCHESTRATOR-CLAUDE.md template documents the per-session convention."""

    def test_template_mentions_per_session_context_state(self) -> None:
        p = REPO_ROOT / "templates" / "ORCHESTRATOR-CLAUDE.md.template"
        self.assertTrue(p.is_file(), f"missing: {p}")
        text = p.read_text(encoding="utf-8")
        self.assertIn("CONTEXT_STATE_<session_id>.md", text)
        # And it frames the rollup vs per-session distinction.
        self.assertIn("shared project rollup", text)


if __name__ == "__main__":
    unittest.main()
