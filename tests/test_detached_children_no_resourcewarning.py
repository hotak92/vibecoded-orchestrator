# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Hygiene (v0.2.75): detached children must not print ResourceWarning.

Live-observed on the 2026-07-07 dogfood update: install.py's flow spawns
deliberately-detached children (P7 resync driver + its prune / metadata-
backfill / code-summary riders in ``vco_lib.codegraph_resync``; Docker
Desktop launch and the stage-1 updater handoff in ``install.py``). Nobody
waits on them — they are meant to outlive the parent — but dropping the
``subprocess.Popen`` handle lets CPython's ``Popen.__del__`` fire on GC and
print ``ResourceWarning: subprocess NNN is still running`` (+ tracemalloc
advice) into every user's update output.

Fix under test: each detached spawn site appends its handle to a
module-level ``_DETACHED_CHILDREN`` list, keeping the object alive for the
parent's lifetime so the destructor never runs mid-process. No global
warning suppression — the warning stays correct for genuinely-forgotten
children.

Covers:
  * behavioural: a Popen appended to ``_DETACHED_CHILDREN`` emits NO
    ResourceWarning on forced GC; the inverse (unreferenced handle) DOES
    emit one — proving the assertion isn't vacuous (CPython-only).
  * structural: every detached spawn site in codegraph_resync.py and
    install.py routes through the keep-alive list.
"""

from __future__ import annotations

import gc
import subprocess
import sys
import unittest
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import codegraph_resync  # noqa: E402


def _spawn_short_lived() -> subprocess.Popen:
    """Spawn a child with the same detached shape the production sites use
    (DEVNULL stdio, no wait) that exits immediately on its own."""
    return subprocess.Popen(  # noqa: S603 — argv is ours
        [sys.executable, "-c", "pass"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_exit_without_reaping(proc: subprocess.Popen) -> None:
    """Let the child EXIT before we force GC — deterministic, per the
    hygiene spec — without calling wait()/poll() (reaping sets returncode,
    which would suppress the warning for the wrong reason)."""
    import time

    deadline = time.monotonic() + 30.0
    pid_dir = Path("/proc") / str(proc.pid)
    while time.monotonic() < deadline:
        if not pid_dir.exists():
            # Non-/proc platform (or already reaped): can't observe zombie
            # state — return; the warning semantics don't depend on it
            # (returncode stays None either way until someone reaps).
            return
        try:
            state_line = (pid_dir / "stat").read_text()
            # Field 3 of /proc/<pid>/stat is the state; Z = zombie (exited,
            # unreaped — exactly the state a detached-and-dropped handle
            # leaves behind).
            if state_line.rsplit(")", 1)[-1].split()[0] == "Z":
                return
        except OSError:
            return  # raced the reap — child is gone
        time.sleep(0.02)


class TestDetachedChildrenKeepAlive(unittest.TestCase):
    def test_handle_in_detached_children_no_resourcewarning(self):
        """The fix: a handle parked in _DETACHED_CHILDREN survives GC, so
        no ResourceWarning reaches the user."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            proc = _spawn_short_lived()
            pid = proc.pid
            codegraph_resync._DETACHED_CHILDREN.append(proc)
            try:
                _wait_for_exit_without_reaping(proc)
                del proc  # drop the local; the module list keeps it alive
                gc.collect()
                # Match on OUR pid so handles leaked by unrelated tests and
                # collected by this gc.collect() can't flake this assertion.
                resource_warnings = [
                    w for w in caught
                    if issubclass(w.category, ResourceWarning)
                    and f"subprocess {pid} " in str(w.message)
                ]
                self.assertEqual(
                    resource_warnings, [],
                    "a kept-alive detached handle must not warn on GC",
                )
            finally:
                # Cleanup: reap the child so the test leaves no zombie.
                kept = codegraph_resync._DETACHED_CHILDREN.pop()
                kept.wait(timeout=30)

    @unittest.skipUnless(
        sys.implementation.name == "cpython",
        "Popen.__del__ warning timing is only deterministic on CPython",
    )
    def test_dropped_handle_does_warn(self):
        """Inverse (the pre-fix bug shape): an UNREFERENCED handle warns on
        GC — proves the assertion above isn't vacuously green."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            proc = _spawn_short_lived()
            pid = proc.pid
            _wait_for_exit_without_reaping(proc)
            del proc  # no keep-alive reference anywhere
            gc.collect()
            resource_warnings = [
                w for w in caught
                if issubclass(w.category, ResourceWarning)
                and f"subprocess {pid} " in str(w.message)
            ]
            self.assertTrue(
                resource_warnings,
                "an unreferenced detached handle should warn — if this "
                "stops firing, the keep-alive test above proves nothing",
            )
        # __del__ already dead-state-polled the child; nothing to reap.


class TestSpawnSitesRouteThroughKeepAlive(unittest.TestCase):
    """Structural ratchet: every detached Popen in the two known files is
    parked in _DETACHED_CHILDREN. (install.py's one WAITED Popen — the
    dot-cycle progress runner, which calls communicate() — is exempt.)"""

    def test_codegraph_resync_spawn_sites(self):
        src = (REPO_ROOT / "vco_lib" / "codegraph_resync.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("_DETACHED_CHILDREN: list = []", src)
        # 4 detached children: resync driver + prune + backfill + summary.
        self.assertGreaterEqual(src.count("_DETACHED_CHILDREN.append("), 4)

    def test_install_py_spawn_sites(self):
        src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        self.assertIn("_DETACHED_CHILDREN: list = []", src)
        # 2 detached children: Docker Desktop launch + stage-1 updater.
        self.assertGreaterEqual(src.count("_DETACHED_CHILDREN.append("), 2)
        # Ratchet: any NEW bare `subprocess.Popen(` line in install.py must
        # either be waited on or parked. Today exactly one Popen call is
        # NOT wrapped by an append (the waited dot-cycle runner:
        # `proc = subprocess.Popen(`). If this count grows, route the new
        # spawn through _DETACHED_CHILDREN (detached) or wait on it.
        popen_lines = [
            ln for ln in src.splitlines()
            if "subprocess.Popen(" in ln and not ln.lstrip().startswith("#")
        ]
        unparked = [
            ln for ln in popen_lines if "proc = subprocess.Popen(" in ln
        ]
        self.assertEqual(
            len(popen_lines) - len(unparked), 2,
            f"unexpected install.py Popen call sites: {popen_lines}",
        )


if __name__ == "__main__":
    unittest.main()
