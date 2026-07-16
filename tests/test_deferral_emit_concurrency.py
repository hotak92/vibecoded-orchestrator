# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""WP-B1 (v0.2.83) REGRESSION PIN: concurrent-writer serialization.

Two SEPARATE processes each emit N distinct deferral entries into ONE project
folder at the same time. Because ``vco_lib.deferral_emit`` performs the whole
read → mutate → write cycle under a shared exclusive file lock
(``.claude/context/.update-deferred.lock``), the final report must contain ALL
2N entries — no writer's read/write pair may interleave with the other's and
drop entries.

The pin PROVES the lock is load-bearing: the same concurrent workload run with
the lock STUBBED OUT (a no-op contextmanager) loses entries (final count < 2N),
so the "all 2N present" assertion is genuinely testing the lock, not a happy
accident of timing. We assert BOTH:

  * lock STUBBED  → entries are lost (the race is real and the workload
    actually exercises it); and
  * lock ACTIVE   → all 2N entries survive.

Real separate processes (not threads) are required: ``fcntl.flock`` is an
advisory lock keyed on the open file description; two independent processes are
the honest test of cross-process serialization, matching the suite's other
subprocess-based deferral regression tests.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.deferral_report import DeferralReport  # noqa: E402

# Each worker emits this many DISTINCT entries; two workers ⇒ 2N total.
_N = 40

# The worker script. It emits _N entries one at a time through
# ``deferral_emit.emit`` (each a full locked read-modify-write). A tiny sleep
# between the read and the write of the underlying report WIDENS the race
# window so a missing lock reliably drops entries. To exercise the
# no-lock (stubbed) case, ``--stub-lock`` replaces the shared lock with a
# no-op contextmanager BEFORE deferral_emit uses it (patched on the atomic
# module, which deferral_emit imports the symbol from — so we also patch the
# already-bound name inside deferral_emit).
_WORKER_SRC = textwrap.dedent(
    '''
    import contextlib
    import sys
    import time
    from pathlib import Path

    REPO_ROOT = Path(sys.argv[1])
    sys.path.insert(0, str(REPO_ROOT))

    folder = Path(sys.argv[2])
    prefix = sys.argv[3]          # "A" or "B" — namespaces each worker's cids
    n = int(sys.argv[4])
    stub_lock = "--stub-lock" in sys.argv

    import vco_lib.atomic as atomic
    import vco_lib.deferral_emit as de

    if stub_lock:
        @contextlib.contextmanager
        def _noop_lock(_lock_path):
            yield
        # Replace both the source and the name already bound in deferral_emit.
        atomic.exclusive_file_lock = _noop_lock
        de.exclusive_file_lock = _noop_lock

    # Slow the read→write cycle to widen the interleave window when unlocked.
    _orig_read = de.DeferralReport.read.__func__

    def _slow_read(cls, f):
        rep = _orig_read(cls, f)
        time.sleep(0.002)
        return rep

    de.DeferralReport.read = classmethod(_slow_read)

    from vco_lib.deferral_emit import DeferralEntry, emit

    for i in range(n):
        emit(
            folder,
            DeferralEntry(
                condition_id=f"{prefix}_cond_{i:03d}",
                title="T",
                detected="d",
                why_deferred="w",
                command_to_apply="c",
                severity="warning",
            ),
        )
    '''
)


class ConcurrentWriterSerializationPin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        self._worker = self.folder / "_worker.py"
        self._worker.write_text(_WORKER_SRC, encoding="utf-8")

    def _run_two_workers(self, *, stub_lock: bool) -> set:
        args = [sys.executable, str(self._worker), str(REPO_ROOT)]
        extra = ["--stub-lock"] if stub_lock else []

        def _spawn(prefix: str) -> subprocess.Popen:
            return subprocess.Popen(
                args + [str(self.folder), prefix, str(_N)] + extra,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        # Launch both at once so their read-modify-write cycles overlap.
        p_a = _spawn("A")
        p_b = _spawn("B")
        out_a = p_a.communicate(timeout=120)
        out_b = p_b.communicate(timeout=120)
        self.assertEqual(
            p_a.returncode, 0,
            f"worker A failed: {out_a[1].decode('utf-8', 'replace')}",
        )
        self.assertEqual(
            p_b.returncode, 0,
            f"worker B failed: {out_b[1].decode('utf-8', 'replace')}",
        )
        return {e.condition_id for e in DeferralReport.read(self.folder).entries}

    def test_lock_active_all_2n_survive(self):
        expected = {f"A_cond_{i:03d}" for i in range(_N)} | {
            f"B_cond_{i:03d}" for i in range(_N)
        }
        got = self._run_two_workers(stub_lock=False)
        self.assertEqual(
            got, expected,
            f"lock must serialize both writers: expected {len(expected)} "
            f"entries, got {len(got)} (missing "
            f"{sorted(expected - got)[:10]}{'…' if len(expected - got) > 10 else ''})",
        )

    def test_stubbed_lock_loses_entries_proving_the_pin(self):
        """The no-lock control: with the shared lock replaced by a no-op, the
        interleaving read-modify-write MUST drop entries (< 2N). If this ever
        stops losing entries, the concurrency workload no longer exercises the
        race and the ACTIVE-lock assertion above is no longer a real pin.

        Retried a few times to defeat scheduler luck — the race is
        probabilistic, but with N=40 and a widened window a loss is
        overwhelmingly likely on at least one attempt."""
        for _ in range(5):
            got = self._run_two_workers(stub_lock=True)
            if len(got) < 2 * _N:
                return  # entries were lost — the race is real. Pin proven.
            time.sleep(0.05)
        self.fail(
            "stubbed-lock control never lost an entry across 5 attempts — the "
            "concurrency workload is not exercising the race, so the "
            "lock-active pin is not actually testing the lock."
        )


if __name__ == "__main__":
    unittest.main()
