# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 R11 — the RL loss ledger's spill/drain is crash-safe and exactly-once.

A ``record_loss`` writer that finds the ledger lock busy spills its line to
``<ledger>.pending.d/``; the next lock holder drains it. These tests pin:

* F1 — a drainer killed at any step (before its append, mid-append, after the
  append and before the unlink) loses nothing and duplicates nothing: each
  spilled record carries a ``spill_id`` and the drain appends only ids the
  ledger does not hold, then unlinks.
* N1 — a pending line that is not a record is dropped with a WARNING, never
  copied into the ledger, and its file is unlinked.
* F2 — an append that raises after writing some or all of its bytes is
  retried without a duplicate.
* F3 — a spill that fails falls back to a longer locked wait, then to an
  unlocked append, and says so at WARNING.
* F4 — pending files drain in spill-time order, not pid order.
* F5 — ``vco doctor`` reports the pending backlog.

The kill tests crash a real child process (``os._exit``) holding the lock at
the named step, through a wrapper around the ledger's append handle, so they
exercise whatever drain implementation is installed.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from tests.common.child_env import child_env

# A wrapper around ``open(<ledger>, "a…")`` that crashes the process at STEP.
# Shared by the child-process kill tests (exec'd in the child) and the
# in-process F2 tests.
_CRASH_OPEN = r'''
import builtins, errno, os
_real_open = builtins.open

class _Crashing:
    def __init__(self, fh, step):
        self._fh, self._step = fh, step
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        self._fh.close()
        if self._step == "after_append":
            os._exit(9)
        return False
    def write(self, data):
        step = self._step
        if step == "before_append":
            os._exit(9)
        if step == "torn":
            self._fh.write(data[: max(1, len(data) // 2)]); self._fh.flush(); os._exit(9)
        if step == "full_then_oserror":
            self._fh.write(data); self._fh.flush()
            raise OSError(errno.ENOSPC, "No space left on device")
        if step == "half_then_oserror":
            self._fh.write(data[: max(1, len(data) // 2)]); self._fh.flush()
            raise OSError(errno.ENOSPC, "No space left on device")
        return self._fh.write(data)
    def __getattr__(self, name):
        return getattr(self._fh, name)

def install_crashing_open(ledger, step, once=False):
    state = {"armed": True}
    def patched(file, mode="r", *a, **k):
        fh = _real_open(file, mode, *a, **k)
        if state["armed"] and str(file) == str(ledger) and "a" in mode:
            if once:
                state["armed"] = False
            return _Crashing(fh, step)
        return fh
    builtins.open = patched
    return lambda: setattr(builtins, "open", _real_open)
'''

_KILLED_DRAINER = _CRASH_OPEN + r'''
import sys
import vco_lib.rl_telemetry_loss as m
from vco_lib.atomic import exclusive_file_lock
install_crashing_open(m.loss_log_path(), sys.argv[1])
with exclusive_file_lock(m.lock_path()):
    m.drain_pending_locked()
'''

_RECORD_FINAL = (
    "import vco_lib.rl_telemetry_loss as m\n"
    "m.record_loss('dual_skip', 'race', seq='final')\n"
)


def _rec(seq: str, ts: int = 1) -> str:
    return json.dumps({"ts_ms": ts, "kind": "dual_skip", "reason": "r", "seq": seq},
                      separators=(",", ":")) + "\n"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from vco_lib import rl_telemetry_loss as m

    state = tmp_path / "vct"
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    m._reset_warned_for_test()
    p = m.loss_log_path()
    p.parent.mkdir(parents=True)
    p.write_text(_rec("old"))
    yield p
    m._reset_warned_for_test()


def _run(code: str, ledger: Path, *args: str) -> subprocess.CompletedProcess:
    env = child_env(VCT_STATE_DIR=str(ledger.parent.parent))
    return subprocess.run([sys.executable, "-c", code, *args], env=env,
                          capture_output=True, text=True, timeout=120)


def _seqs_read(ledger: Path) -> list:
    from vco_lib import rl_telemetry_loss as m

    return [r["seq"] for r in m.iter_records(ledger)]


def _seqs_raw(ledger: Path) -> list:
    out = []
    for raw in ledger.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(raw)["seq"])
        except ValueError:
            continue  # a torn fragment is not a record
    return out


def _pending(ledger: Path) -> list:
    from vco_lib import rl_telemetry_loss as m

    d = m.pending_dir(ledger)
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


# ---------------------------------------------------------------------------
# F1 — a drainer killed at each step
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step", ["before_append", "torn", "after_append"])
def test_a_drainer_killed_at_any_step_loses_and_duplicates_nothing(ledger, step):
    from vco_lib import rl_telemetry_loss as m

    m._spill(ledger, _rec("p1"))
    proc = _run(_KILLED_DRAINER, ledger, step)
    assert proc.returncode == 9, (proc.returncode, proc.stderr[-2000:])

    # Between the crash and the next drain, a reader sees the line exactly once.
    assert _seqs_read(ledger) == ["old", "p1"], step

    # The next locked writer finishes the drain: the line lands in the
    # ledger once, and the pending dir is empty.
    proc = _run(_RECORD_FINAL, ledger)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _seqs_raw(ledger) == ["old", "p1", "final"], step
    assert _pending(ledger) == []
    assert _seqs_read(ledger) == ["old", "p1", "final"]


def test_a_leftover_whose_id_is_in_the_ledger_is_not_appended_again(ledger):
    from vco_lib import rl_telemetry_loss as m
    from vco_lib.atomic import exclusive_file_lock

    m._spill(ledger, _rec("p1"))
    (name,) = _pending(ledger)
    body = (m.pending_dir(ledger) / name).read_text()
    assert json.loads(body)[m.SPILL_ID_KEY] == name[: -len(".jsonl")]
    with open(ledger, "a") as fh:  # = a drain that appended, then died before the unlink
        fh.write(body)
    assert _seqs_read(ledger) == ["old", "p1"]
    with exclusive_file_lock(m.lock_path(ledger)):
        m.drain_pending_locked(ledger)
    assert _seqs_raw(ledger) == ["old", "p1"]
    assert _pending(ledger) == []


def test_a_leftover_not_in_the_ledger_is_appended_once(ledger):
    from vco_lib import rl_telemetry_loss as m
    from vco_lib.atomic import exclusive_file_lock

    m._spill(ledger, _rec("p1"))
    for _ in range(2):
        with exclusive_file_lock(m.lock_path(ledger)):
            m.drain_pending_locked(ledger)
    assert _seqs_raw(ledger) == ["old", "p1"]
    assert _pending(ledger) == []


# ---------------------------------------------------------------------------
# N1 — an unparseable pending line
# ---------------------------------------------------------------------------


def test_an_unparseable_pending_line_is_dropped_with_a_warning(ledger, caplog):
    """Drain and reader agree on what a record is: a pending line that is not
    a JSON object is skipped by ``iter_records`` and dropped by the drain
    (WARNING once), never appended verbatim; its file is unlinked."""
    from vco_lib import rl_telemetry_loss as m

    d = m.pending_dir(ledger)
    d.mkdir(parents=True)
    (d / f"{1_700_000_000_000_000_000:020d}-1-aa.jsonl").write_text("this is not json\n")
    (d / f"{1_700_000_000_000_000_001:020d}-1-bb.jsonl").write_text("[1, 2]\n")
    assert _seqs_read(ledger) == ["old"]
    with caplog.at_level(logging.WARNING, logger=m.__name__):
        m.record_loss("dual_skip", "race", seq="final")
        m.record_loss("dual_skip", "race", seq="final2")
    assert _seqs_raw(ledger) == ["old", "final", "final2"]
    assert "not json" not in ledger.read_text(encoding="utf-8")
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 3
    assert _pending(ledger) == []
    drops = [r for r in caplog.records
             if r.levelno == logging.WARNING and "not a record" in r.getMessage()]
    assert len(drops) == 1


# ---------------------------------------------------------------------------
# F2 — an append that raises after writing bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step", ["full_then_oserror", "half_then_oserror"])
def test_an_append_that_fails_after_writing_is_retried_without_a_duplicate(ledger, step):
    from vco_lib import rl_telemetry_loss as m
    from vco_lib.atomic import exclusive_file_lock

    m._spill(ledger, _rec("p1"))
    ns: dict = {}
    exec(_CRASH_OPEN, ns)  # noqa: S102 — test-local helper source
    restore = ns["install_crashing_open"](ledger, step, once=True)
    try:
        with exclusive_file_lock(m.lock_path(ledger)):
            m.drain_pending_locked(ledger)
    finally:
        restore()
    assert _seqs_read(ledger) == ["old", "p1"]
    m.record_loss("dual_skip", "race", seq="final")
    assert _seqs_raw(ledger) == ["old", "p1", "final"], step
    assert _pending(ledger) == []


# ---------------------------------------------------------------------------
# F4 — time order, not pid order
# ---------------------------------------------------------------------------


def test_pending_files_drain_in_spill_time_order(ledger):
    """pid 9 spills first, pid 10 second: the name leads with the spill
    time, so the earlier line drains first whatever the pids sort as."""
    from vco_lib import rl_telemetry_loss as m

    with mock.patch.object(os, "getpid", lambda: 9):
        m._spill(ledger, _rec("first"))
    time.sleep(0.002)
    with mock.patch.object(os, "getpid", lambda: 10):
        m._spill(ledger, _rec("second"))
    assert _seqs_read(ledger) == ["old", "first", "second"]
    m.record_loss("dual_skip", "race", seq="final")
    assert _seqs_raw(ledger) == ["old", "first", "second", "final"]


# ---------------------------------------------------------------------------
# F3 — a spill that fails
# ---------------------------------------------------------------------------


def _hold_lock(ledger: Path, release_after_s: float):
    from vco_lib import rl_telemetry_loss as m
    from vco_lib.atomic import exclusive_file_lock

    taken = threading.Event()

    def run():
        with exclusive_file_lock(m.lock_path(ledger)):
            taken.set()
            time.sleep(release_after_s)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert taken.wait(10)
    return t


def _block_pending_dir(ledger: Path) -> None:
    from vco_lib import rl_telemetry_loss as m

    m.pending_dir(ledger).write_text("not a directory")


def test_a_failed_spill_waits_longer_for_the_lock_and_warns(ledger, monkeypatch, caplog):
    from vco_lib import rl_telemetry_loss as m

    _block_pending_dir(ledger)
    monkeypatch.setattr(m, "_LOCK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(m, "_SPILL_FAILED_LOCK_TIMEOUT_S", 10.0)
    holder = _hold_lock(ledger, 0.6)
    with caplog.at_level(logging.WARNING, logger=m.__name__):
        m.record_loss("dual_skip", "race", seq="late")
    holder.join(10)
    assert _seqs_raw(ledger) == ["old", "late"]
    assert any("could not spill" in r.getMessage() for r in caplog.records)
    assert not any("without the lock" in r.getMessage() for r in caplog.records)


def test_a_failed_spill_with_the_lock_still_busy_appends_unlocked_and_warns(
        ledger, monkeypatch, caplog):
    from vco_lib import rl_telemetry_loss as m

    _block_pending_dir(ledger)
    monkeypatch.setattr(m, "_LOCK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(m, "_SPILL_FAILED_LOCK_TIMEOUT_S", 0.05)
    holder = _hold_lock(ledger, 1.5)
    with caplog.at_level(logging.WARNING, logger=m.__name__):
        m.record_loss("dual_skip", "race", seq="last-resort")
    holder.join(10)
    assert _seqs_raw(ledger) == ["old", "last-resort"]
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("could not spill" in x for x in msgs)
    assert any("without the lock" in x for x in msgs)


def test_a_record_that_cannot_be_written_anywhere_is_a_warning(tmp_path, monkeypatch, caplog):
    from vco_lib import rl_telemetry_loss as m

    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct"))
    m._reset_warned_for_test()
    p = m.loss_log_path()
    p.mkdir(parents=True)  # the ledger path is a directory: nothing can append
    with caplog.at_level(logging.WARNING, logger=m.__name__):
        m.record_loss("dual_skip", "race", seq="x")  # never raises
    assert any("could not record a dual_skip (race) line" in r.getMessage()
               and r.levelno == logging.WARNING for r in caplog.records)
    m._reset_warned_for_test()


# ---------------------------------------------------------------------------
# F5 — vco doctor reports the backlog
# ---------------------------------------------------------------------------


def test_doctor_reports_pending_spills_and_their_age(ledger):
    from vco_lib import doctor
    from vco_lib import rl_telemetry_loss as m

    m._spill(ledger, _rec("p1", ts=int(time.time() * 1000)))
    assert m.pending_stats(ledger)["files"] == 1
    (f,) = doctor.probe_rl_telemetry_loss(Path("/tmp/x"), doctor.DoctorResolvers(), {})
    assert "1 spilled line file(s) waiting to be drained" in f.summary, f.summary
    assert "s old" in f.summary
    assert f.detail["pending_files"] == 1
    assert isinstance(f.detail["pending_oldest_ms"], int)
    assert f.detail["total"] == 1  # the spilled line is counted once


def test_doctor_is_silent_about_an_empty_pending_dir(ledger):
    from vco_lib import doctor

    (f,) = doctor.probe_rl_telemetry_loss(Path("/tmp/x"), doctor.DoctorResolvers(), {})
    assert "spilled" not in f.summary
    assert f.detail["pending_files"] == 0


# ---------------------------------------------------------------------------
# metrics_migration ignores the pending dir
# ---------------------------------------------------------------------------


def test_metrics_migration_leaves_a_pending_dir_alone(tmp_path):
    """The archive-to-``~/.vct/metrics`` copy globs top-level ``*.jsonl``
    files only; a ``rl_telemetry_loss.jsonl.pending.d/`` on either side is
    neither copied nor touched, and does not fail the run."""
    from vco_lib.metrics_migration import migrate_metrics

    src, dst = tmp_path / "claude-metrics", tmp_path / "vct-metrics"
    for root in (src, dst):
        (root / "rl_telemetry_loss.jsonl.pending.d").mkdir(parents=True)
        (root / "rl_telemetry_loss.jsonl.pending.d" / "1-2-aa.jsonl").write_text(_rec(root.name))
    (src / "failures.jsonl").write_text('{"a":1}\n')
    res = migrate_metrics(src, dst)
    assert res.status == "migrated", res.errors
    assert (dst / "failures.jsonl").read_text() == '{"a":1}\n'
    assert sorted(p.name for p in (dst / "rl_telemetry_loss.jsonl.pending.d").iterdir()) == [
        "1-2-aa.jsonl"]
    assert (dst / "rl_telemetry_loss.jsonl.pending.d" / "1-2-aa.jsonl").read_text() == _rec(dst.name)
    assert not (dst / "rl_telemetry_loss.jsonl").exists()
