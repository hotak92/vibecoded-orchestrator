# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 final review L-5 — the code-graph walk claim follows the
ANALYZER, not just the ``python -m vco_lib.codegraph_walk`` wrapper.

Before the fix the wrapper held the flock on its own descriptor (not
inherited by the child) and ran the analyzer with ``subprocess.run``: a
SIGTERM to the wrapper alone ended the wrapper, freed the claim, and left the
analyzer running without it — a second walk was then admitted (the twin the
claim exists to stop). Now the wrapper forwards SIGTERM/SIGHUP to the child
and waits for it, and on POSIX the child inherits the claim's descriptor, so
even a SIGKILL of the wrapper cannot free the claim under a running walk.

Real processes, a fake analyzer (never the real one), a temp VCT state dir.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import codegraph_walk  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="POSIX signal + flock semantics")

FAKE_ANALYZER = """import os, sys, time
from pathlib import Path
Path(os.environ["FAKE_PID_FILE"]).write_text(str(os.getpid()))
deadline = time.time() + 60
while time.time() < deadline:
    time.sleep(0.05)
sys.exit(0)
"""

QUICK_ANALYZER = "import sys; sys.exit(int(sys.argv[-1]))\n"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie still answers kill(0); read its state where /proc exists
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            return stat.read_text().split()[2] != "Z"
        except (OSError, IndexError):
            return True
    return True


def _wait(pred, secs: float) -> bool:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _claim_held(project: str) -> bool:
    try:
        with codegraph_walk.walk_claim(project):
            return False
    except codegraph_walk.WalkAlreadyRunning:
        return True


@pytest.fixture()
def walk(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    script = tmp_path / "fake_analyzer.py"
    script.write_text(FAKE_ANALYZER, encoding="utf-8")
    pid_file = tmp_path / "child.pid"
    procs: list = []

    def start(project: str = "ProjL5"):
        from tests.common.child_env import child_env

        env = child_env()
        env.update(FAKE_PID_FILE=str(pid_file), VCT_STATE_DIR=str(state))
        proc = subprocess.Popen(
            [sys.executable, "-m", "vco_lib.codegraph_walk", str(script),
             "--project", project, str(tmp_path)],
            env=env, cwd=str(tmp_path))
        procs.append(proc)
        assert _wait(lambda: pid_file.exists() and pid_file.read_text().strip(), 30), \
            "the fake analyzer never started"
        return proc, int(pid_file.read_text())

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


def test_sigterm_to_the_wrapper_stops_the_analyzer_and_only_then_frees_the_claim(walk):
    """RED before the fix: the wrapper died alone, the analyzer kept running
    and the claim was free (a twin could start)."""
    wrapper, child = walk()
    assert _claim_held("ProjL5")
    os.kill(wrapper.pid, signal.SIGTERM)
    assert _wait(lambda: not _alive(child), 20), \
        "SIGTERM to the wrapper must reach the analyzer"
    wrapper.wait(timeout=20)
    assert not _claim_held("ProjL5"), "the claim is released once the walk ended"


def test_sigkill_of_the_wrapper_keeps_the_claim_while_the_analyzer_runs(walk):
    """RED before the fix: SIGKILL of the wrapper freed the claim at once."""
    wrapper, child = walk()
    os.kill(wrapper.pid, signal.SIGKILL)
    wrapper.wait(timeout=20)
    assert _alive(child)
    assert _claim_held("ProjL5"), \
        "a wrapper-only kill must not free the claim under a running walk"
    os.kill(child, signal.SIGKILL)
    assert _wait(lambda: not _claim_held("ProjL5"), 20), \
        "the claim dies with the last holder (the analyzer)"


def test_leave_alone_exit_code_passes_through_and_a_held_claim_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    script = tmp_path / "quick.py"
    script.write_text(QUICK_ANALYZER, encoding="utf-8")
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    assert codegraph_walk.run_claimed(script, ["--project", "ProjQ", "4"]) == 4
    assert not _claim_held("ProjQ")
    with codegraph_walk.walk_claim("ProjQ"):
        assert codegraph_walk.run_claimed(script, ["--project", "ProjQ", "0"]) \
            == codegraph_walk.WALK_ALREADY_RUNNING_RC
    # the forwarding handlers are this run's only: the caller's are restored
    assert {s: signal.getsignal(s) for s in before} == before
