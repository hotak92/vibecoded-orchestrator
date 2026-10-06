# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 S2 — the detached retry driver's pidfile is a HEARTBEAT, not a start stamp.

Before: ``_lock_is_held`` aged the pidfile from the mtime ``_acquire_lock``
wrote and nothing ever refreshed it, so the 6 h bound covered the WHOLE run.
v0.2.101 item 4 moved the full install-time seeds into this driver: one pass of
``kg_seed`` + ``kg_seed_shared`` + ``code_graph_walk`` + ``codegraph_resync``
(up to ``MAX_SETTLE_PASSES``) legitimately outlives that on a slow machine, and
the next session-start then started a SECOND driver against the same
collections.

Now: the driver touches the pidfile before every handler and between settle
passes (staleness = "no progress for N hours"), and the pidfile records the
holder's process start token so pid REUSE is detected by identity rather than
by age.

The "second-start gate" is asserted from a REAL second process
(``_lock_is_held`` short-circuits for its own pid), so liveness and identity
are the real probes, not mocks. Time is simulated by back-dating the pidfile's
mtime — exactly the quantity the gate reads.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import deferral_probes, deferral_retry  # noqa: E402

KG_CID = "kg_sync_no_embedding_backend"
CG_CID = "code_graph_no_embedding_backend"
CG_CODE_CID = "code_graph_code_backend_unreachable"

#: How long each fake handler "runs" (seconds of mtime back-dating). Three of
#: them add up past PIDFILE_STALE_SECONDS; any ONE stays well under it.
HANDLER_SECONDS = deferral_retry.PIDFILE_STALE_SECONDS * 0.55

_GATE_PROBE = (
    "import sys\n"
    "from pathlib import Path\n"
    "from vco_lib import deferral_retry as d\n"
    "print('HELD' if d._lock_is_held(Path(sys.argv[1])) else 'OPEN')\n"
)


def _second_driver_sees(path: Path) -> str:
    """Ask a SEPARATE process whether the lock is held (``HELD``/``OPEN``)."""
    # child_env(): the probe imports vco_lib.deferral_retry — the child must
    # resolve it from THIS checkout, never a stale site-packages copy (the
    # ratchet in test_v0292_fixround_child_env_lint exists for exactly this).
    from tests.common.child_env import child_env

    env = dict(child_env())
    proc = subprocess.run(
        [sys.executable, "-c", _GATE_PROBE, str(path)],
        capture_output=True, text=True, timeout=60, env=env, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _age(path: Path, seconds: float) -> None:
    """Pretend *seconds* passed since the pidfile was last touched."""
    st = path.stat()
    os.utime(path, (st.st_atime - seconds, st.st_mtime - seconds))


def _project(tmp_path: Path) -> Path:
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("sync_knowledge_graph.py", "analyze_code_graph.py"):
        (scripts / name).write_text("# stub\n", encoding="utf-8")
    return tmp_path


class _LongPassRunner:
    """Each handler child: first look at the gate from a second driver, then
    'run' for HANDLER_SECONDS (back-date the pidfile mtime by that much)."""

    def __init__(self, folder: Path) -> None:
        self.path = deferral_retry.pidfile_path(folder)
        self.gate: list[str] = []

    def __call__(self, argv, cwd) -> int:
        self.gate.append(_second_driver_sees(self.path))
        _age(self.path, HANDLER_SECONDS)
        return 0


def _run_long_pass(folder: Path) -> _LongPassRunner:
    runner = _LongPassRunner(folder)
    with mock.patch.object(deferral_retry, "_record_resolution"), \
         mock.patch.object(deferral_retry, "_code_embed_image_verdict",
                           return_value="current"):
        deferral_retry.dispatch(
            folder, condition_ids=[KG_CID, CG_CID, CG_CODE_CID],
            backend_probe=lambda f, k: True, runner=runner,
        )
    return runner


needs_proc_identity = pytest.mark.skipif(
    deferral_probes.process_start_token(os.getpid()) is None,
    reason="this platform cannot read a process start token",
)


class TestHeartbeat:
    def test_a_long_multi_handler_pass_keeps_the_second_start_gate_closed(
        self, tmp_path: Path,
    ) -> None:
        """ACT: three handlers whose durations SUM past the bound — the gate
        stays closed for the whole pass because each handler starts with a
        heartbeat."""
        folder = _project(tmp_path)
        runner = _run_long_pass(folder)
        assert len(runner.gate) == 3, "every handler must have run"
        assert runner.gate == ["HELD", "HELD", "HELD"], (
            "a driver making progress was declared stale mid-pass — a second "
            f"driver would start against the same collections: {runner.gate}"
        )

    def test_without_the_heartbeat_the_same_pass_ages_out(self, tmp_path: Path) -> None:
        """RED-PROOF of the test above: the identical pass with the heartbeat
        disabled (the pre-S2 shape) opens the gate on the third handler."""
        folder = _project(tmp_path)
        with mock.patch.object(deferral_retry, "_heartbeat_lock"):
            runner = _run_long_pass(folder)
        assert runner.gate[-1] == "OPEN", runner.gate

    def test_the_heartbeat_also_fires_between_settle_passes(self, tmp_path: Path) -> None:
        """A row enqueued WHILE pass 1 ran (SF-1) starts pass 2; a pass 1 that
        ran past the bound must not let a second driver in before it."""
        folder = _project(tmp_path)
        path = deferral_retry.pidfile_path(folder)
        seen: list[str] = []
        owed_calls = {"n": 0}

        def _owed(_folder):
            owed_calls["n"] += 1
            return [KG_CID] if owed_calls["n"] == 1 else [KG_CID, CG_CID]

        def _fake_pass(_folder, *, cids, **_kw):
            if not seen:
                _age(path, deferral_retry.PIDFILE_STALE_SECONDS + 3600)
                seen.append("pass1")
            else:
                seen.append(_second_driver_sees(path))
            return [deferral_retry.RetryResult(c, deferral_retry.SKIPPED, "fake")
                    for c in cids]

        def _drive() -> list[str]:
            seen.clear()
            owed_calls["n"] = 0
            with mock.patch.object(deferral_retry, "owed_condition_ids", _owed), \
                 mock.patch.object(deferral_retry, "_dispatch_pass", _fake_pass):
                deferral_retry.dispatch(folder)
            return list(seen)

        assert _drive() == ["pass1", "HELD"]
        with mock.patch.object(deferral_retry, "_heartbeat_lock"):
            assert _drive() == ["pass1", "OPEN"], "red-proof: no pass heartbeat"

    def test_true_silence_still_ages_out(self, tmp_path: Path) -> None:
        """LEAVE-ALONE leg: a live holder (our parent, with its REAL identity
        token) that has not heartbeated for longer than the bound is taken
        over — the wedge guard survives the heartbeat."""
        folder = _project(tmp_path)
        path = deferral_retry.pidfile_path(folder)
        path.parent.mkdir(parents=True, exist_ok=True)
        ppid = os.getppid()
        token = deferral_probes.process_start_token(ppid) or ""
        path.write_text(f"{ppid}\n{token}\n", encoding="utf-8")
        assert deferral_retry._lock_is_held(path), "fresh + alive + same identity ⇒ held"
        _age(path, deferral_retry.PIDFILE_STALE_SECONDS + 60)
        assert not deferral_retry._lock_is_held(path)
        runner = mock.Mock(return_value=0)
        with mock.patch.object(deferral_retry, "_record_resolution"):
            results = deferral_retry.dispatch(
                folder, condition_ids=[KG_CID],
                backend_probe=lambda f, k: True, runner=runner,
            )
        assert runner.call_count == 1, [r.detail for r in results]

    def test_heartbeat_never_refreshes_another_drivers_claim(self, tmp_path: Path) -> None:
        folder = _project(tmp_path)
        path = deferral_retry.pidfile_path(folder)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{os.getppid()}\n", encoding="utf-8")
        _age(path, 1000)
        before = path.stat().st_mtime
        deferral_retry._heartbeat_lock(folder)
        assert path.stat().st_mtime == before

    def test_heartbeat_without_a_pidfile_is_a_silent_noop(self, tmp_path: Path) -> None:
        deferral_retry._heartbeat_lock(tmp_path)  # no .claude/state at all
        assert not deferral_retry.pidfile_path(tmp_path).exists()


@needs_proc_identity
class TestIdentity:
    def test_acquire_records_the_holders_start_token(self, tmp_path: Path) -> None:
        path = deferral_retry._acquire_lock(tmp_path)
        assert path is not None
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == str(os.getpid())
        assert lines[1] == deferral_probes.process_start_token(os.getpid())
        assert deferral_retry._read_pidfile(path) == os.getpid(), (
            "the first line stays the plain pid every older reader parses")

    def test_a_reused_pid_is_taken_over_at_once(self, tmp_path: Path) -> None:
        """ACT: alive pid, FRESH file, but the recorded identity is another
        process's — the crashed driver's pid was handed on. No 24 h wait."""
        path = deferral_retry.pidfile_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        kind = deferral_probes.process_start_token(os.getppid()).split(":", 1)[0]
        wrong = "proc:1" if kind == "proc" else "wall:1.000"
        path.write_text(f"{os.getppid()}\n{wrong}\n", encoding="utf-8")
        assert not deferral_retry._lock_is_held(path)

    def test_an_unreadable_identity_stays_held(self, tmp_path: Path) -> None:
        """LEAVE-ALONE: no token (an older driver's one-line file) ⇒ the
        conservative branch — held while fresh."""
        path = deferral_retry.pidfile_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{os.getppid()}\n", encoding="utf-8")
        assert deferral_retry._lock_is_held(path)

    def test_identity_helper_is_tri_state(self) -> None:
        me = deferral_probes.process_start_token(os.getpid())
        assert deferral_probes.process_identity_matches(os.getpid(), me) is True
        assert deferral_probes.process_identity_matches(os.getpid(), None) is None
        assert deferral_probes.process_identity_matches(os.getpid(), "other:1") is None
        assert deferral_probes.process_start_token(0) is None
