# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""P2c-a + P3a riders (v0.2.75): install.py update-gate choreography.

The V52-AI initial lockfile write + A-6 per-phase deadline refreshes were
extracted from ``install.py main()`` into
``vco_lib.install_update_gate.InstallUpdateGate``; main() keeps thin
``gate.begin()`` / ``gate.refresh(phase)`` call sites. Two riders were
FIXED in the extraction:

  (a) a third refresh site now covers the venv/container/model-pull phase
      (structural guard below pins THREE refresh call sites in main());
  (b) during ``--update``, a lockfile ABSENT at refresh time is RE-CREATED
      (the phase proves mid-update; the historical no-op left the rest of
      the run unprotected after a stale-cleanup raced a slow phase). Fresh
      installs keep the no-op.

Supersedes ``TestUpdateGateDeadlineRefresh`` in
``tests/test_install_a6_a7_v0273.py`` (which pinned the pre-rider
absent→no-op behaviour of the removed ``_refresh_update_lockfile_deadline``
helper).
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import install_update_gate as iug  # noqa: E402
from vco_lib import update_gate  # noqa: E402
from vco_lib.install_update_gate import InstallUpdateGate  # noqa: E402


class _GateCase(unittest.TestCase):
    """Shared harness: temp lockfile path + recorded atexit + log lines."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock = Path(self._tmp.name) / update_gate.LOCKFILE_BASENAME
        patcher = mock.patch.object(
            update_gate, "lockfile_path", return_value=self.lock
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # Never register REAL atexit handlers from tests: at interpreter
        # exit VCT_STATE_DIR is long unpatched and delete_lockfile() would
        # target the real ~/.vct lockfile of a genuinely-running update.
        self.atexit_calls: list = []
        ax = mock.patch.object(
            iug.atexit, "register", side_effect=self.atexit_calls.append
        )
        ax.start()
        self.addCleanup(ax.stop)
        self.log_lines: list = []

    def gate(self, mode: str = "update") -> InstallUpdateGate:
        return InstallUpdateGate(mode, log=self.log_lines.append)


class BeginTests(_GateCase):
    def test_begin_writes_lockfile_and_registers_atexit_on_update(self):
        self.gate("update").begin()
        data = update_gate.read_lockfile(path=self.lock)
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["phase"], "install_py")
        self.assertEqual(self.atexit_calls, [update_gate.delete_lockfile])

    def test_begin_is_noop_on_fresh_install(self):
        self.gate("install").begin()
        self.assertFalse(self.lock.exists())
        self.assertEqual(self.atexit_calls, [])

    def test_begin_soft_fails_on_write_error(self):
        with mock.patch.object(
            update_gate, "write_lockfile", side_effect=OSError("boom")
        ):
            self.gate("update").begin()  # must not raise
        self.assertTrue(
            any("soft-fail" in ln for ln in self.log_lines), self.log_lines
        )
        self.assertEqual(self.atexit_calls, [], "no delete armed for no write")


class RefreshTests(_GateCase):
    def test_refresh_extends_deadline_and_advances_phase(self):
        g = self.gate("update")
        g.begin()
        first = update_gate.read_lockfile(path=self.lock)
        assert first is not None
        time.sleep(1.1)
        g.refresh("binary_refresh")
        second = update_gate.read_lockfile(path=self.lock)
        assert second is not None
        self.assertEqual(second["phase"], "binary_refresh")
        self.assertGreater(
            second["expected_completion_by"], first["expected_completion_by"]
        )

    def test_refresh_recreates_absent_lockfile_mid_update(self):
        """P3a rider (b): --update + absent lockfile → RE-CREATE at the
        given phase (the phase string proves mid-update), and arm the
        atexit delete for the re-created file."""
        g = self.gate("update")
        self.assertFalse(self.lock.exists())
        g.refresh("install_py")
        data = update_gate.read_lockfile(path=self.lock)
        self.assertIsNotNone(data, "absent lockfile must be re-created")
        assert data is not None
        self.assertEqual(data["phase"], "install_py")
        self.assertEqual(self.atexit_calls, [update_gate.delete_lockfile])
        self.assertTrue(
            any("re-created" in ln for ln in self.log_lines),
            "re-create must be logged for visibility",
        )

    def test_refresh_absent_lockfile_noop_on_fresh_install(self):
        """P3a rider (b), leave-alone side: fresh installs never wrote a
        lockfile — refresh must NOT create one."""
        self.gate("install").refresh("install_py")
        self.assertFalse(self.lock.exists())
        self.assertEqual(self.atexit_calls, [])

    def test_refresh_leaves_foreign_lockfile_alone_on_fresh_install(self):
        """A concurrent launcher-driven update's lockfile is not ours to
        extend from a fresh-install run."""
        update_gate.write_lockfile(phase="git_pull", path=self.lock)
        before = self.lock.read_text(encoding="utf-8")
        self.gate("install").refresh("install_py")
        self.assertEqual(self.lock.read_text(encoding="utf-8"), before)

    def test_refresh_soft_fails_on_write_error(self):
        with mock.patch.object(
            update_gate, "write_lockfile", side_effect=OSError("boom")
        ):
            self.gate("update").refresh("install_py")  # must not raise
        self.assertTrue(
            any("soft-failed" in ln for ln in self.log_lines), self.log_lines
        )

    def test_atexit_registered_exactly_once_across_recreate(self):
        """begin() + a later re-create must not stack duplicate handlers."""
        g = self.gate("update")
        g.begin()
        self.lock.unlink()  # simulate a stale-cleanup racing the run
        g.refresh("install_py")
        g.refresh("binary_refresh")
        self.assertEqual(len(self.atexit_calls), 1)


class MainCallSiteStructureTests(unittest.TestCase):
    """Structural guards on install.py (import-shape only — matches the
    deferral suites' source-scan approach)."""

    @classmethod
    def setUpClass(cls):
        cls.source = (REPO_ROOT / "install.py").read_text(encoding="utf-8")

    def _code_lines(self, needle: str) -> list:
        return [
            ln for ln in self.source.splitlines()
            if needle in ln
            and not ln.lstrip().startswith("#")
            and "``" not in ln
        ]

    def test_exactly_one_begin_call_site(self):
        self.assertEqual(
            len(self._code_lines("_update_gate_flow.begin(")), 1
        )

    def test_exactly_three_refresh_call_sites(self):
        """A-6's two original phase boundaries + the P3a rider (a) third
        site covering the venv/container/model-pull phase."""
        lines = self._code_lines("_update_gate_flow.refresh(")
        self.assertEqual(len(lines), 3, lines)

    def test_no_direct_lockfile_choreography_left_in_install(self):
        """The extraction is total: install.py must not call
        update_gate.write_lockfile / register the delete itself anymore
        (the module is the one owner)."""
        self.assertEqual(self._code_lines("write_lockfile("), [])
        self.assertEqual(
            self._code_lines("_refresh_update_lockfile_deadline("), []
        )


if __name__ == "__main__":
    unittest.main()
