# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A-12 + G1 (v0.2.73): safe_add+update_mode warn + unconditional git-exclude.

A-12: ``install_project_bundle(safe_add=True, update_mode=True)`` previously
silently no-op'd safe-add (the gate is add-time-only). Now it logs a warn so
the caller isn't misled.

G1 (secrets spec item #9): ``.git/info/exclude`` coverage runs on EVERY bundle
update (``update_mode=True``), regardless of safe_add — keeping VCO-created
paths out of the user's commits for projects added before safe-add existed.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402
from tests.test_install_bundle import _make_fake_orchestrator  # noqa: E402


class A12G1UpdateModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-a12g1-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)
        # Make the project a git repo so .git/info/exclude is reachable.
        git_dir = self.proj / ".git"
        (git_dir / "info").mkdir(parents=True)
        self.exclude_path = git_dir / "info" / "exclude"
        self.logs: list = []

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _run(self, *, safe_add: bool, update_mode: bool):
        return project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=update_mode,
            safe_add=safe_add,
            log_event=lambda step, phase, detail="", *, data=None:
                self.logs.append((step, phase, detail)),
        )

    # --- A-12 -----------------------------------------------------------

    def test_safe_add_with_update_mode_logs_warn(self):
        self._run(safe_add=True, update_mode=True)
        warns = [
            (s, p, d) for (s, p, d) in self.logs
            if s == "4.bundle.safe_add" and p == "warn"
            and "ignored in update_mode" in d
        ]
        self.assertTrue(
            warns, f"expected safe_add-ignored warn; logs={self.logs[-8:]}"
        )

    def test_safe_add_add_mode_does_not_log_the_warn(self):
        """On a fresh ADD (update_mode=False), safe_add runs normally — no
        'ignored in update_mode' warn."""
        self._run(safe_add=True, update_mode=False)
        warns = [
            d for (s, p, d) in self.logs
            if s == "4.bundle.safe_add" and "ignored in update_mode" in d
        ]
        self.assertEqual(warns, [])

    # --- G1 -------------------------------------------------------------

    def test_g1_exclude_runs_on_update_regardless_of_safe_add(self):
        """A plain bundle update (safe_add=False) still appends VCO paths to
        .git/info/exclude (G1 unconditional coverage)."""
        result = self._run(safe_add=False, update_mode=True)
        self.assertIn("g1_git_exclude", result)
        # The .claude/ glob is the canonical VCO-created namespace.
        if self.exclude_path.exists():
            body = self.exclude_path.read_text(encoding="utf-8")
            self.assertIn("/.claude/", body)

    def test_g1_exclude_idempotent_on_second_update(self):
        self._run(safe_add=False, update_mode=True)
        first = (
            self.exclude_path.read_text(encoding="utf-8")
            if self.exclude_path.exists() else ""
        )
        self.logs.clear()
        result = self._run(safe_add=False, update_mode=True)
        second = (
            self.exclude_path.read_text(encoding="utf-8")
            if self.exclude_path.exists() else ""
        )
        # No duplicate lines appended.
        self.assertEqual(first, second)
        self.assertEqual(result["g1_git_exclude"]["action"], "noop")

    def test_g1_exclude_not_run_on_fresh_add(self):
        """G1's update-only block must NOT fire on a fresh non-safe add
        (that path is governed by the safe-add branch)."""
        self._run(safe_add=False, update_mode=False)
        result_keys = [s for (s, p, d) in self.logs if s == "4.bundle.g1_git_exclude"]
        self.assertEqual(result_keys, [])


if __name__ == "__main__":
    unittest.main()
