# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.54 Track C — self-update hardening (install.py side).

Covers:

1. C-6 — `_clear_update_resume_sentinel_after_success`: install.py's
   --update tail must clear the launcher's resume sentinel
   (`.claude/state/orchestrator-update-resume-needed.json`) after a
   successful run (the deferral's "Option B self-clears" promise was
   false pre-v0.2.54), but must REFUSE while a merge/rebase is still in
   progress.

2. Intel-Mac fix — `_launcher_binary_relative_path`,
   `_vct_hub_binary_relative_path`, `_bootstrap_launcher_dist_subdir`:
   arch-aware on Darwin (`macos-x64` for x86_64, `macos-arm64`
   otherwise).

3. C-5 ordering shape — in the Windows sharing-violation branch of
   `_refresh_dist_binary_after_rebuild`, the launcher-driven guard
   (VCT_AUTO_RESTART_LAUNCHER=1 → stage .new only, no updater spawn)
   must appear BEFORE the `_try_invoke_windows_stage1_updater` call.
   (Full behavioural coverage of that branch requires a Windows host —
   the branch is gated on ERROR_SHARING_VIOLATION; the helper itself is
   behaviourally covered by tests/test_v52_ah_windows_updater.py.)
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import install  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# 1. C-6 — resume-sentinel clear
# ---------------------------------------------------------------------------

class ClearUpdateResumeSentinel(unittest.TestCase):
    SENTINEL_REL = Path(".claude") / "state" / "orchestrator-update-resume-needed.json"

    def _mk_root(self, tmp: Path, *, sentinel: bool, merge_head: bool = False,
                 rebase_merge: bool = False) -> Path:
        root = tmp
        if sentinel:
            target = root / self.SENTINEL_REL
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{"operation": "merge", "branch": "main", '
                              '"sha_at_conflict": "abc123"}')
        git_dir = root / ".git"
        git_dir.mkdir(exist_ok=True)
        if merge_head:
            (git_dir / "MERGE_HEAD").write_text("deadbeef\n")
        if rebase_merge:
            (git_dir / "rebase-merge").mkdir()
        return root

    def test_clears_sentinel_when_no_merge_in_progress(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = self._mk_root(Path(td), sentinel=True)
            install._clear_update_resume_sentinel_after_success(root)
            self.assertFalse((root / self.SENTINEL_REL).exists(),
                             "sentinel must be cleared after a clean --update")

    def test_noop_when_sentinel_absent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = self._mk_root(Path(td), sentinel=False)
            # Must not raise.
            install._clear_update_resume_sentinel_after_success(root)
            self.assertFalse((root / self.SENTINEL_REL).exists())

    def test_refuses_while_merge_in_progress(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = self._mk_root(Path(td), sentinel=True, merge_head=True)
            install._clear_update_resume_sentinel_after_success(root)
            self.assertTrue((root / self.SENTINEL_REL).exists(),
                            "sentinel must survive while MERGE_HEAD exists")

    def test_refuses_while_rebase_in_progress(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = self._mk_root(Path(td), sentinel=True, rebase_merge=True)
            install._clear_update_resume_sentinel_after_success(root)
            self.assertTrue((root / self.SENTINEL_REL).exists(),
                            "sentinel must survive while .git/rebase-merge exists")

    def test_call_site_present_in_update_tail(self):
        """main()'s --update tail must invoke the clearer (gated on
        args.update). Source-shape check: the call exists outside the
        function's own definition."""
        src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        # Definition + at least one call site.
        self.assertGreaterEqual(
            src.count("_clear_update_resume_sentinel_after_success("), 2,
            "C-6: install.py must CALL _clear_update_resume_sentinel_"
            "after_success in the --update tail (zero-call-site dead "
            "helper is the exact defect class this fixes)",
        )


# ---------------------------------------------------------------------------
# 2. Intel-Mac arch-aware dist slots
# ---------------------------------------------------------------------------

class MacArchAwareDistSlots(unittest.TestCase):
    def test_launcher_path_is_macos_x64_on_intel(self):
        with mock.patch.object(install.platform, "system", return_value="Darwin"), \
             mock.patch.object(install.platform, "machine", return_value="x86_64"):
            subdir, fname = install._launcher_binary_relative_path()
        self.assertEqual((subdir, fname), ("macos-x64", "vct-launcher"))

    def test_launcher_path_is_macos_arm64_on_apple_silicon(self):
        with mock.patch.object(install.platform, "system", return_value="Darwin"), \
             mock.patch.object(install.platform, "machine", return_value="arm64"):
            subdir, fname = install._launcher_binary_relative_path()
        self.assertEqual((subdir, fname), ("macos-arm64", "vct-launcher"))

    def test_hub_path_mirrors_launcher_on_intel(self):
        with mock.patch.object(install.platform, "system", return_value="Darwin"), \
             mock.patch.object(install.platform, "machine", return_value="x86_64"):
            subdir, fname = install._vct_hub_binary_relative_path()
        self.assertEqual((subdir, fname), ("macos-x64", "vct-hub"))

    def test_hub_path_mirrors_launcher_on_apple_silicon(self):
        with mock.patch.object(install.platform, "system", return_value="Darwin"), \
             mock.patch.object(install.platform, "machine", return_value="arm64"):
            subdir, fname = install._vct_hub_binary_relative_path()
        self.assertEqual((subdir, fname), ("macos-arm64", "vct-hub"))

    def test_bootstrap_subdir_is_arch_aware_on_macos(self):
        with mock.patch.object(install, "_bootstrap_detect_os",
                               return_value=("macos", "arm64")), \
             mock.patch.object(install.platform, "machine", return_value="x86_64"):
            self.assertEqual(install._bootstrap_launcher_dist_subdir(), "macos-x64")
        with mock.patch.object(install, "_bootstrap_detect_os",
                               return_value=("macos", "arm64")), \
             mock.patch.object(install.platform, "machine", return_value="arm64"):
            self.assertEqual(install._bootstrap_launcher_dist_subdir(), "macos-arm64")

    def test_linux_and_windows_slots_unchanged(self):
        with mock.patch.object(install.platform, "system", return_value="Linux"):
            self.assertEqual(install._launcher_binary_relative_path(),
                             ("linux-x64", "vct-launcher"))
        with mock.patch.object(install.platform, "system", return_value="Windows"):
            self.assertEqual(install._launcher_binary_relative_path(),
                             ("windows-x64", "vct-launcher.exe"))


# ---------------------------------------------------------------------------
# 3. C-5 — launcher-driven guard precedes the stage1 updater spawn
# ---------------------------------------------------------------------------

class LauncherDrivenStage1Guard(unittest.TestCase):
    def test_auto_restart_guard_precedes_stage1_invocation(self):
        """Within the Windows sharing-violation branch, the
        VCT_AUTO_RESTART_LAUNCHER=1 guard must short-circuit BEFORE
        `_try_invoke_windows_stage1_updater` is reached. Pre-v0.2.54,
        a launcher-driven install.py spawned updater #1 whose 30 s
        parent-wait deterministically timed out against the launcher's
        up-to-5-min WaitForBinaryRefresh, leaving an orphaned
        update.lock.json (spurious failure toast) and a brief
        two-updaters window."""
        src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        # Locate the stage1 call site (skip the function's own def).
        call_idx = src.find("lock_path = _try_invoke_windows_stage1_updater(")
        self.assertGreater(call_idx, 0, "stage1 call site missing")
        # The guard must appear in the ~3000 chars immediately before the
        # call (same branch, after the .new staging).
        window = src[max(0, call_idx - 3000):call_idx]
        self.assertIn("VCT_AUTO_RESTART_LAUNCHER", window, (
            "C-5 regression: the launcher-driven guard no longer "
            "precedes the stage1 updater spawn"
        ))
        self.assertIn("swap_succeeded = True", window, (
            "C-5: the launcher-driven path must mark the swap as "
            "logically succeeded (staged .new; launcher handoff swaps)"
        ))


if __name__ == "__main__":
    unittest.main()
