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
    """C-5: EVERY stage1 spawn site must be guarded.

    v0.2.91 (WP-F carry-over iii): this class used to locate the call site
    with a single `str.find(...)`, which sees only the FIRST occurrence. When
    v0.2.91's WI-5 repair leg added a SECOND invocation
    (`invoke_stage1=lambda …`, handed to `vco_lib.dist_binary_repair
    .run_repair_leg`), the test kept passing on the first site and would have
    said nothing about the new one — and a launcher-driven run that spawns its
    own updater is exactly the double-updater / 30 s parent-wait timeout C-5
    exists to prevent. Now every site is enumerated and checked, and the
    delegated site's guard is followed across the module boundary into
    vco_lib rather than trusted because the string happens to be nearby.
    """

    CALL = "_try_invoke_windows_stage1_updater("

    @staticmethod
    def _enclosing_function(src: str, idx: int) -> str:
        """Source of the module-level function containing `idx`, up to `idx`.

        A fixed-size character window is NOT good enough: a 3000-char lookback
        bleeds into the PRECEDING function, so a site whose own guard was
        deleted still "passes" on a neighbour's copy of the string. Bounding
        the search at the enclosing `def` is what makes the assertion mean
        what it says. (Found by red-proofing this very test: with the guard
        removed at the delegated site, the character-window version stayed
        green.)
        """
        start = src.rfind("\ndef ", 0, idx)
        return src[start if start >= 0 else 0 : idx]

    def _call_sites(self, src: str) -> list:
        """Byte offsets of every INVOCATION (the `def` line excluded)."""
        sites, at = [], 0
        while True:
            idx = src.find(self.CALL, at)
            if idx < 0:
                return sites
            at = idx + 1
            line_start = src.rfind("\n", 0, idx) + 1
            if src[line_start:idx].lstrip().startswith("def "):
                continue  # the definition itself
            sites.append(idx)

    def test_every_stage1_call_site_is_enumerated(self):
        src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        sites = self._call_sites(src)
        self.assertGreaterEqual(
            len(sites), 2,
            "expected at least two stage1 invocation sites (the sharing-violation "
            "branch and the WI-5 repair leg); a single-site scan is what let the "
            "second one ship unchecked",
        )

    def test_auto_restart_guard_precedes_every_stage1_invocation(self):
        """Within the Windows sharing-violation branch, the
        VCT_AUTO_RESTART_LAUNCHER=1 guard must short-circuit BEFORE
        `_try_invoke_windows_stage1_updater` is reached. Pre-v0.2.54,
        a launcher-driven install.py spawned updater #1 whose 30 s
        parent-wait deterministically timed out against the launcher's
        up-to-5-min WaitForBinaryRefresh, leaving an orphaned
        update.lock.json (spurious failure toast) and a brief
        two-updaters window."""
        src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        sites = self._call_sites(src)
        self.assertTrue(sites, "stage1 call site missing")
        for idx in sites:
            # The guard must appear inside the SAME function, before the call.
            window = self._enclosing_function(src, idx)
            self.assertIn("VCT_AUTO_RESTART_LAUNCHER", window, (
                f"C-5 regression: the launcher-driven guard no longer "
                f"precedes the stage1 updater spawn at offset {idx}"
            ))

    def test_direct_invocation_marks_the_swap_logically_succeeded(self):
        """The site that spawns the updater INLINE owns the
        `swap_succeeded = True` bookkeeping (the delegated site's equivalent
        lives in vco_lib — see the next test)."""
        src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        direct = [
            i for i in self._call_sites(src)
            if src[src.rfind("\n", 0, i) + 1:i].lstrip().startswith("lock_path =")
        ]
        self.assertTrue(direct, "the inline stage1 invocation disappeared")
        for idx in direct:
            window = self._enclosing_function(src, idx)
            self.assertIn("swap_succeeded = True", window, (
                "C-5: the launcher-driven path must mark the swap as "
                "logically succeeded (staged .new; launcher handoff swaps)"
            ))

    def test_delegated_invocation_threads_the_guard_into_vco_lib(self):
        """The WI-5 repair leg passes the spawner as a CALLBACK, so its guard
        is not textually adjacent — it is `launcher_driven=`, evaluated inside
        `run_repair_leg`. Follow it: the flag must be threaded from install.py
        AND must short-circuit before `invoke_stage1` is ever called, or the
        callback fires on a launcher-driven run."""
        src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        delegated = [
            i for i in self._call_sites(src)
            if "invoke_stage1=" in src[src.rfind("\n", 0, i) + 1:i]
        ]
        self.assertTrue(
            delegated,
            "the WI-5 repair leg no longer hands a stage1 invoker to run_repair_leg",
        )
        for idx in delegated:
            window = self._enclosing_function(src, idx)
            self.assertIn(
                'launcher_driven=os.environ.get("VCT_AUTO_RESTART_LAUNCHER"',
                window,
                "the repair leg must thread the LIVE guard into run_repair_leg — "
                "a hardcoded `launcher_driven=` is the same regression with a "
                "keyword in front of it",
            )

        leg = (REPO_ROOT / "vco_lib" / "dist_binary_repair.py").read_text(encoding="utf-8")
        guard = leg.find("if launcher_driven:")
        call = leg.find("invoke_stage1(")
        self.assertGreater(guard, 0, "run_repair_leg lost its C-5 guard")
        self.assertGreater(call, 0, "run_repair_leg no longer invokes the spawner")
        self.assertLess(guard, call, (
            "C-5 regression: run_repair_leg reaches invoke_stage1 without the "
            "launcher-driven short-circuit having had its say"
        ))


if __name__ == "__main__":
    unittest.main()
