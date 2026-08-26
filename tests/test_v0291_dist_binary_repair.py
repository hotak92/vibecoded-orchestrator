# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-A / WI-5 — the terminal-path dist-binary repair leg.

``vco_lib.dist_binary_repair`` is the permanent escape hatch for the bootstrap
paradox: every fix to the launcher's own delivery chain ships INSIDE the
launcher binary, so when that binary is the broken component nothing in the GUI
can deliver the fix. This module needs only git + python, so it repairs an
install whose launcher is frozen, mis-copied, or missing.

RED-PROOF: on the pre-fix tree (``bd8f6836``) this module does not exist, and
``install.py`` contains no ``git checkout`` / restore of ``launcher/dist/**``
anywhere — verified by grep at investigation time. Every test here therefore
fails against the unfixed tree (import error for the unit tests, missing
call-site for the structural ones).

DESTRUCTIVE-GATE DISCIPLINE: ``restore_paths_from_head`` runs
``git checkout -- <path>``, which DISCARDS working-tree content. Both legs of
every branch that gates it are tested: the act (a dirty dist binary IS
restored) and the leave-alone (a dirty file outside the dist dir, and an
untracked file inside it, are NOT touched).
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.dist_binary_repair import (  # noqa: E402
    RepairOutcome,
    dist_dirty_paths,
    repair_dist_binaries,
    restore_paths_from_head,
    scan_for_launcher_pid,
    staged_sibling,
    stage_paths_from_head,
)

DIST_REL = "launcher/dist/linux-x64"
BIN_REL = f"{DIST_REL}/vct-launcher"
HUB_REL = f"{DIST_REL}/vct-hub"


def _have_git() -> bool:
    try:
        return subprocess.run(
            ["git", "--version"], capture_output=True, check=False
        ).returncode == 0
    except OSError:
        return False


def _git(repo: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, check=False
    )
    assert proc.returncode == 0, (
        f"git {args} failed: {proc.stderr.decode('utf-8', 'replace')}"
    )


class DistBinaryRepairTests(unittest.TestCase):
    """Every test builds a throwaway git repo shaped like an install root."""

    def setUp(self) -> None:
        if not _have_git():
            self.skipTest("git not on PATH")
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / DIST_REL).mkdir(parents=True)
        (self.repo / BIN_REL).write_bytes(b"HEAD-LAUNCHER-BYTES")
        (self.repo / HUB_REL).write_bytes(b"HEAD-HUB-BYTES")
        (self.repo / "install.py").write_text("# source\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "seed")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- dirty detection ------------------------------------------------

    def test_clean_tree_reports_nothing_dirty(self) -> None:
        self.assertEqual(dist_dirty_paths(self.repo, DIST_REL), [])

    def test_hand_copied_stale_binary_is_reported_dirty(self) -> None:
        """The field shape: a working binary hand-copied over the canonical path."""
        (self.repo / BIN_REL).write_bytes(b"STALE-0.2.88-HAND-COPIED")
        self.assertEqual(dist_dirty_paths(self.repo, DIST_REL), [BIN_REL])

    def test_untracked_files_in_dist_are_never_reported(self) -> None:
        """LEAVE-ALONE leg of the destructive gate.

        ``.new`` staging siblings and ``.old-<pid>`` backups live in the dist
        directory and are untracked. Reporting them would let a careless caller
        widen the restore into deleting the user's own recovery copies.
        """
        (self.repo / f"{BIN_REL}.old-4242").write_bytes(b"user backup")
        (self.repo / f"{BIN_REL}.new").write_bytes(b"staged")
        self.assertEqual(dist_dirty_paths(self.repo, DIST_REL), [])

    def test_dirty_files_outside_the_dist_dir_are_never_reported(self) -> None:
        """LEAVE-ALONE leg: the probe is scoped, so a WIP source edit is safe."""
        (self.repo / "install.py").write_text("# EDITED BY USER\n", encoding="utf-8")
        self.assertEqual(dist_dirty_paths(self.repo, DIST_REL), [])

    def test_trailing_slash_is_optional_in_the_pathspec(self) -> None:
        (self.repo / BIN_REL).write_bytes(b"x")
        self.assertEqual(dist_dirty_paths(self.repo, DIST_REL + "/"), [BIN_REL])

    # -- restore leg (ACT) ----------------------------------------------

    def test_restore_puts_head_bytes_back(self) -> None:
        (self.repo / BIN_REL).write_bytes(b"STALE-0.2.88-HAND-COPIED")
        restored, failed = restore_paths_from_head(self.repo, [BIN_REL])
        self.assertEqual(restored, [BIN_REL])
        self.assertEqual(failed, [])
        self.assertEqual((self.repo / BIN_REL).read_bytes(), b"HEAD-LAUNCHER-BYTES")

    def test_restore_reports_failure_for_an_unknown_path(self) -> None:
        restored, failed = restore_paths_from_head(self.repo, ["not/a/tracked/path"])
        self.assertEqual(restored, [])
        self.assertEqual(failed, ["not/a/tracked/path"])

    def test_restore_of_a_staged_modified_binary_comes_from_head_not_the_index(
        self,
    ) -> None:
        """v0.2.91 fix-round MINOR-2 — RED-PROOF.

        ``git checkout -- <path>`` restores from the INDEX. A dist binary in the
        ``M `` (staged-modified) state — one ``git add`` away, and the exact
        shape a half-finished conflict resolution leaves behind — is then
        "restored" to the STAGED bytes, i.e. to the diverged bytes. git exits 0,
        this module reports a successful repair, ``git status`` still shows the
        file diverged from HEAD, and the install stays frozen forever with a
        green log line over it.

        Against the pre-fix ``["checkout", "--", rel]`` this test fails on the
        content assertion (the file still holds ``STAGED-DIVERGED-BYTES``).
        """
        (self.repo / BIN_REL).write_bytes(b"STAGED-DIVERGED-BYTES")
        _git(self.repo, "add", BIN_REL)
        # Precondition: git sees it as STAGED-modified, and the index no longer
        # agrees with HEAD.
        self.assertEqual(dist_dirty_paths(self.repo, DIST_REL), [BIN_REL])

        restored, failed = restore_paths_from_head(self.repo, [BIN_REL])

        self.assertEqual(restored, [BIN_REL])
        self.assertEqual(failed, [])
        self.assertEqual(
            (self.repo / BIN_REL).read_bytes(),
            b"HEAD-LAUNCHER-BYTES",
            "a repair may only ever put HEAD's bytes on disk — restoring the "
            "INDEX copy re-creates the divergence it claims to have fixed",
        )

    def test_repair_converges_a_staged_modified_binary(self) -> None:
        """The same MINOR-2 hazard through the orchestration entry point: the
        repair must leave the WORKING TREE matching HEAD.

        (The index still carries the staged blob afterwards — ``git checkout
        HEAD -- <path>`` rewrites both the index and the working tree for that
        path, so ``dist_dirty_paths`` reads clean; this asserts the convergence
        the whole leg exists to produce.)
        """
        (self.repo / BIN_REL).write_bytes(b"STAGED-DIVERGED-BYTES")
        _git(self.repo, "add", BIN_REL)

        outcome = repair_dist_binaries(self.repo, DIST_REL)

        self.assertEqual(outcome.restored, (BIN_REL,))
        self.assertEqual((self.repo / BIN_REL).read_bytes(), b"HEAD-LAUNCHER-BYTES")
        self.assertEqual(
            dist_dirty_paths(self.repo, DIST_REL),
            [],
            "after the repair the dist tree must actually agree with HEAD — "
            "otherwise the next run repairs it again, forever",
        )

    def test_restore_names_head_explicitly(self) -> None:
        """Structural pin for MINOR-2: the tree is named in the argv, so a
        future edit cannot silently drop back to the index-restoring form."""
        import inspect

        import vco_lib.dist_binary_repair as dbr

        src = inspect.getsource(dbr.restore_paths_from_head)
        self.assertIn('["checkout", "HEAD", "--", rel]', src)
        self.assertNotIn('["checkout", "--", rel]', src)

    # -- stage leg -------------------------------------------------------

    def test_stage_writes_head_bytes_to_the_new_sibling(self) -> None:
        (self.repo / BIN_REL).write_bytes(b"STALE")
        staged, failed = stage_paths_from_head(self.repo, [BIN_REL])
        self.assertEqual(staged, [BIN_REL])
        self.assertEqual(failed, [])
        sibling = staged_sibling(self.repo / BIN_REL)
        self.assertEqual(sibling.read_bytes(), b"HEAD-LAUNCHER-BYTES")
        # The canonical path is untouched until the updater renames.
        self.assertEqual((self.repo / BIN_REL).read_bytes(), b"STALE")
        # No `.tmp` residue.
        self.assertFalse(sibling.with_name(sibling.name + ".tmp").exists())

    def test_staged_sibling_matches_the_updater_reader_convention(self) -> None:
        self.assertEqual(
            staged_sibling(Path("/d/vct-launcher.exe")).name, "vct-launcher.exe.new"
        )
        self.assertEqual(staged_sibling(Path("/d/vct-launcher")).name, "vct-launcher.new")

    # -- orchestration ---------------------------------------------------

    def test_repair_restores_every_dirty_dist_binary(self) -> None:
        (self.repo / BIN_REL).write_bytes(b"STALE-LAUNCHER")
        (self.repo / HUB_REL).write_bytes(b"STALE-HUB")
        outcome = repair_dist_binaries(self.repo, DIST_REL)
        self.assertEqual(sorted(outcome.dirty), [HUB_REL, BIN_REL])
        self.assertEqual(sorted(outcome.restored), [HUB_REL, BIN_REL])
        self.assertEqual(outcome.staged, ())
        self.assertFalse(outcome.handoff_needed)
        self.assertTrue(outcome.changed_anything)
        self.assertEqual((self.repo / BIN_REL).read_bytes(), b"HEAD-LAUNCHER-BYTES")
        self.assertEqual((self.repo / HUB_REL).read_bytes(), b"HEAD-HUB-BYTES")

    def test_repair_is_a_no_op_on_a_clean_tree(self) -> None:
        """LEAVE-ALONE leg for the orchestration entry point."""
        outcome = repair_dist_binaries(self.repo, DIST_REL)
        self.assertEqual(outcome, RepairOutcome())
        self.assertFalse(outcome.changed_anything)
        self.assertFalse(outcome.handoff_needed)

    def test_repair_falls_back_to_staging_when_the_restore_cannot_write(self) -> None:
        """The Windows locked-.exe path, simulated by making the file read-only
        inside a read-only directory so ``git checkout`` cannot rewrite it.

        Skipped when running as root (root ignores the permission bits, so the
        checkout succeeds and there is nothing to assert).
        """
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("running as root: permission bits do not block writes")
        (self.repo / BIN_REL).write_bytes(b"STALE-LAUNCHER")
        dist_dir = self.repo / DIST_REL
        mode = dist_dir.stat().st_mode
        os.chmod(dist_dir, 0o500)  # r-x: no create/replace inside
        try:
            outcome = repair_dist_binaries(self.repo, DIST_REL)
        finally:
            os.chmod(dist_dir, mode)
        # Neither leg can write in a read-only dir; the module must report the
        # failure rather than claim success.
        self.assertEqual(outcome.dirty, (BIN_REL,))
        self.assertEqual(outcome.restored, ())
        self.assertEqual(outcome.failed, (BIN_REL,))
        self.assertFalse(outcome.handoff_needed)
        # And it must not have corrupted the on-disk file.
        self.assertEqual((self.repo / BIN_REL).read_bytes(), b"STALE-LAUNCHER")

    def test_repair_never_touches_a_dirty_source_file(self) -> None:
        """LEAVE-ALONE leg: the whole point of the dist-scoped pathspec."""
        (self.repo / "install.py").write_text("# USER WIP\n", encoding="utf-8")
        (self.repo / BIN_REL).write_bytes(b"STALE-LAUNCHER")
        repair_dist_binaries(self.repo, DIST_REL)
        self.assertEqual(
            (self.repo / "install.py").read_text(encoding="utf-8"), "# USER WIP\n"
        )

    def test_repair_never_deletes_untracked_recovery_copies(self) -> None:
        """LEAVE-ALONE leg: a user's hand-made backup must survive the repair."""
        backup = self.repo / f"{BIN_REL}.old-4242"
        backup.write_bytes(b"user backup")
        (self.repo / BIN_REL).write_bytes(b"STALE-LAUNCHER")
        repair_dist_binaries(self.repo, DIST_REL)
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), b"user backup")


class RunRepairLegTests(unittest.TestCase):
    """Behavioural tests for the orchestration entry point install.py calls.

    The v0.2.54 C-5 guard gates a SPAWN (a detached ``vct-updater`` that will
    rename binaries), so both legs are tested: it acts when install.py owns the
    handoff, and it stands down when the launcher does.
    """

    def setUp(self) -> None:
        if not _have_git():
            self.skipTest("git not on PATH")
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / DIST_REL).mkdir(parents=True)
        (self.repo / BIN_REL).write_bytes(b"HEAD-LAUNCHER-BYTES")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "seed")
        self.calls: list[tuple] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _leg(self, **kw):
        from vco_lib import dist_binary_repair as dbr

        return dbr.run_repair_leg(
            self.repo,
            dist_rel_dir=DIST_REL,
            binary_name="vct-launcher",
            on_restart_required=lambda pid: self.calls.append(("restart", pid)),
            on_swap_locked=lambda detail: self.calls.append(("locked", detail)),
            invoke_stage1=lambda pid: self.calls.append(("stage1", pid)) or None,
            **kw,
        )

    def test_clean_tree_fires_no_callbacks(self) -> None:
        """LEAVE-ALONE leg: nothing dirty ⇒ no side effects at all."""
        outcome = self._leg(launcher_driven=False)
        self.assertFalse(outcome.dirty)
        self.assertEqual(self.calls, [])

    def test_restore_path_asks_for_a_restart_when_a_launcher_is_running(self) -> None:
        (self.repo / BIN_REL).write_bytes(b"STALE")
        self._leg(launcher_driven=False, launcher_pid_env="4242")
        self.assertIn(("restart", 4242), self.calls)
        # A successful restore needs no stage1 handoff.
        self.assertNotIn("stage1", [c[0] for c in self.calls])

    def test_launcher_driven_run_emits_no_restart_deferral(self) -> None:
        """C-5 ACT leg (deferral half): the launcher auto-restarts, so the
        'please restart' entry would be a redundant nag."""
        (self.repo / BIN_REL).write_bytes(b"STALE")
        self._leg(launcher_driven=True, launcher_pid_env="4242")
        self.assertEqual([c[0] for c in self.calls], [])

    def test_launcher_driven_run_never_spawns_the_stage1_updater(self) -> None:
        """C-5 ACT leg (spawn half). Simulated by making staging the only
        possible outcome: the restore cannot write into a read-only dir, so the
        leg would reach the handoff decision — and must not spawn."""
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("running as root: permission bits do not block writes")
        (self.repo / BIN_REL).write_bytes(b"STALE")
        d = self.repo / DIST_REL
        mode = d.stat().st_mode
        os.chmod(d, 0o500)
        try:
            self._leg(launcher_driven=True, launcher_pid_env="4242")
        finally:
            os.chmod(d, mode)
        self.assertNotIn("stage1", [c[0] for c in self.calls])

    def test_pid_env_is_parsed_and_a_garbage_value_falls_back_to_the_scan(self) -> None:
        (self.repo / BIN_REL).write_bytes(b"STALE")
        # Garbage env ⇒ parse fails ⇒ scan runs ⇒ no launcher named
        # "vct-launcher" is running under the test host, so no restart entry.
        self._leg(launcher_driven=False, launcher_pid_env="not-a-pid")
        self.assertEqual([c[0] for c in self.calls], [])


class LauncherPidScanTests(unittest.TestCase):
    """WI-5 leg 1 — the process scan the docstring promised since v0.2.52.

    RED-PROOF: on ``bd8f6836`` ``_try_invoke_windows_stage1_updater`` skipped
    outright when ``launcher_pid is None`` (install.py:19354), so a terminal
    ``install.py --update`` could never hand off to the stage1 updater. There
    was no scan function to test at all.
    """

    def test_scan_never_returns_our_own_pid(self) -> None:
        """The scanner must never nominate the install.py process itself —
        handing our own PID to the updater would make it wait for a process
        that only exits after the handoff it is waiting on."""
        # Scan for THIS interpreter's own program name: the only guaranteed
        # running process with a predictable name on the test host.
        own_name = Path(sys.executable).name
        found = scan_for_launcher_pid(own_name)
        self.assertNotEqual(found, os.getpid())

    def test_scan_returns_none_for_a_name_that_cannot_be_running(self) -> None:
        self.assertIsNone(scan_for_launcher_pid("vct-launcher-definitely-not-running"))

    def test_scan_honours_exclude_pid(self) -> None:
        own_name = Path(sys.executable).name
        found = scan_for_launcher_pid(own_name)
        if found is None:
            self.skipTest("no sibling interpreter process to exclude")
        self.assertNotEqual(scan_for_launcher_pid(own_name, exclude_pid=found), found)


class InstallPyWiringTests(unittest.TestCase):
    """Structural pins on the install.py side of WI-5.

    Unit tests cannot reach `_refresh_dist_binary_after_rebuild`'s no-cargo
    branch without a full install fixture, so the CALL-SITE is pinned by
    source scan. Both assertions fail on ``bd8f6836``.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")

    def test_no_cargo_artifact_branch_invokes_the_repair_leg(self) -> None:
        self.assertIn(
            "_repair_dist_from_head_leg(",
            self.src,
            "the no-cargo-artifact path must run the git-restore leg; on bd8f6836 "
            "it returned None and install.py could not repair dist at all",
        )
        # The call must sit on the `not src.is_file()` branch, before its return.
        idx_branch = self.src.index("if not src.is_file():")
        idx_call = self.src.index("_repair_dist_from_head_leg(", idx_branch)
        idx_return = self.src.index("return None", idx_branch)
        self.assertLess(
            idx_call,
            idx_return,
            "the repair leg must run BEFORE the no-cargo early return",
        )

    def test_stage1_updater_scans_for_a_launcher_pid(self) -> None:
        self.assertIn(
            "scan_for_launcher_pid",
            self.src,
            "the documented process-scan fallback must actually be implemented",
        )

    def _repair_leg_body(self) -> str:
        start = self.src.index("def _repair_dist_from_head_leg(")
        end = self.src.index("\ndef ", start + 1)
        return self.src[start:end]

    def test_repair_leg_imports_vco_lib_loudly(self) -> None:
        """The vco_lib import must NOT sit inside a try/except that degrades to
        an inline copy — a missing shipped module is a BROKEN install."""
        body = self._repair_leg_body()
        import_line = "from vco_lib.dist_binary_repair import"
        self.assertIn(import_line, body)
        before_import = body[: body.index(import_line)]
        self.assertNotIn(
            "try:",
            before_import,
            "the shipped-module import must be OUTSIDE any try/except "
            "(loud-fail, never silent-fallback)",
        )

    def test_shim_threads_the_c5_launcher_driven_flag_through(self) -> None:
        """The C-5 guard's DECISION lives in vco_lib (behaviourally tested in
        ``RunRepairLegTests``); install.py's only duty is to read the env var
        and pass it. Pin that it does — dropping the kwarg would silently
        default the guard off."""
        body = self._repair_leg_body()
        self.assertIn(
            'launcher_driven=os.environ.get("VCT_AUTO_RESTART_LAUNCHER", "").strip() == "1"',
            body,
        )
        self.assertIn("invoke_stage1=lambda pid: _try_invoke_windows_stage1_updater(", body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
