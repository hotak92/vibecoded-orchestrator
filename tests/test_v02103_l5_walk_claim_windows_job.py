# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 final review L-5 / R17-2 — on Windows the analyzer dies with the
``python -m vco_lib.codegraph_walk`` wrapper.

POSIX keeps the claim under a wrapper-only kill by letting the analyzer
inherit the flock descriptor (test_v02103_final_l5_walk_claim_wrapper_kill).
Windows cannot: the ``msvcrt.locking`` lock is per process and
``TerminateProcess`` cannot be caught. Before this fix, killing only the
wrapper freed the claim while the analyzer kept walking — a twin was then
admitted. Now the wrapper puts the analyzer in a Job Object with
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE whose only handle is the wrapper's, so the
kernel ends the analyzer when the wrapper dies.

Platform-neutral tests pin the branch selection and the kernel32 call
sequence through a fake; the Windows-only tests run real processes (a fake
analyzer, never the real one) and a real Stop-Process-equivalent kill.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import codegraph_walk  # noqa: E402


class _FakeKernel32:
    """Records every kernel32 call; ``fail`` names the one call that fails."""

    JOB = 0x1111
    PROC = 0x2222

    def __init__(self, fail: str = ""):
        self.fail = fail
        self.calls: list = []

    def _ret(self, name, ok):
        return 0 if self.fail == name else ok

    def CreateJobObjectW(self, attrs, name):
        self.calls.append(("CreateJobObjectW", attrs, name))
        return self._ret("CreateJobObjectW", self.JOB)

    def SetInformationJobObject(self, job, cls, info_ref, size):
        info = ctypes.cast(info_ref, ctypes.POINTER(
            codegraph_walk._JobExtendedLimitInformation)).contents
        self.calls.append(("SetInformationJobObject", job, cls,
                           info.BasicLimitInformation.LimitFlags, size))
        return self._ret("SetInformationJobObject", 1)

    def OpenProcess(self, access, inherit, pid):
        self.calls.append(("OpenProcess", access, inherit, pid))
        return self._ret("OpenProcess", self.PROC)

    def AssignProcessToJobObject(self, job, proc):
        self.calls.append(("AssignProcessToJobObject", job, proc))
        return self._ret("AssignProcessToJobObject", 1)

    def CloseHandle(self, handle):
        self.calls.append(("CloseHandle", handle))
        return 1


def _names(fake):
    return [c[0] for c in fake.calls]


def test_the_job_is_created_kill_on_close_and_the_child_assigned():
    fake = _FakeKernel32()
    job = codegraph_walk._bind_child_to_kill_on_close_job(4242, kernel32=fake)
    assert job == fake.JOB
    assert fake.calls == [
        ("CreateJobObjectW", None, None),  # NULL attrs: handle NOT inheritable
        ("SetInformationJobObject", fake.JOB, 9, 0x2000,
         ctypes.sizeof(codegraph_walk._JobExtendedLimitInformation)),
        ("OpenProcess", 0x0100 | 0x0001, False, 4242),
        ("AssignProcessToJobObject", fake.JOB, fake.PROC),
        ("CloseHandle", fake.PROC),  # the process handle only — the job stays open
    ]


@pytest.mark.parametrize("failing,closed", [
    ("CreateJobObjectW", []),
    ("SetInformationJobObject", [_FakeKernel32.JOB]),
    ("OpenProcess", [_FakeKernel32.JOB]),
    ("AssignProcessToJobObject", [_FakeKernel32.PROC, _FakeKernel32.JOB]),
])
def test_any_failed_step_warns_returns_none_and_leaks_no_handle(failing, closed, capsys):
    fake = _FakeKernel32(fail=failing)
    assert codegraph_walk._bind_child_to_kill_on_close_job(7, kernel32=fake) is None
    assert sorted(c[1] for c in fake.calls if c[0] == "CloseHandle") == sorted(closed)
    err = capsys.readouterr().err
    assert failing in err and "WARNING" in err


def test_the_struct_layout_matches_the_win32_abi():
    # JOBOBJECT_EXTENDED_LIMIT_INFORMATION: 144 bytes on 64-bit, 112 on 32-bit.
    expected = 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112
    assert ctypes.sizeof(codegraph_walk._JobExtendedLimitInformation) == expected


def test_posix_branch_is_a_noop(monkeypatch):
    called = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(codegraph_walk, "_bind_child_to_kill_on_close_job",
                        lambda pid, kernel32=None: called.append(pid) or 1)
    assert codegraph_walk._tie_child_to_wrapper(123) is None
    assert called == []


def test_windows_branch_binds_and_never_raises(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    seen = []
    monkeypatch.setattr(codegraph_walk, "_bind_child_to_kill_on_close_job",
                        lambda pid, kernel32=None: seen.append(pid) or 99)
    assert codegraph_walk._tie_child_to_wrapper(123) == 99
    assert seen == [123]

    def _boom(pid, kernel32=None):
        raise OSError("no kernel32")

    monkeypatch.setattr(codegraph_walk, "_bind_child_to_kill_on_close_job", _boom)
    assert codegraph_walk._tie_child_to_wrapper(123) is None
    assert "no kernel32" in capsys.readouterr().err


def test_run_child_binds_the_spawned_pid_and_releases_after_it_ends(monkeypatch):
    """The wiring: _run_child ties THE child it spawned, holds the job for
    the child's whole life, and closes it only after the child ended."""
    events = []
    monkeypatch.setattr(codegraph_walk, "_tie_child_to_wrapper",
                        lambda pid: events.append(("tie", pid)) or 77)
    monkeypatch.setattr(codegraph_walk, "_release_job",
                        lambda job, kernel32=None: events.append(("release", job)))
    real_popen = subprocess.Popen

    class _Popen(real_popen):  # type: ignore[misc, valid-type]
        def wait(self, timeout=None):
            rc = super().wait(timeout)
            events.append(("exited", self.pid))
            return rc

    monkeypatch.setattr(codegraph_walk.subprocess, "Popen", _Popen)
    rc = codegraph_walk._run_child([sys.executable, "-c", "raise SystemExit(3)"], cwd=None)
    assert rc == 3
    assert [e[0] for e in events] == ["tie", "exited", "release"]
    assert events[0][1] == events[1][1]
    assert events[2] == ("release", 77)


def test_release_job_closes_the_handle_and_ignores_none():
    fake = _FakeKernel32()
    codegraph_walk._release_job(None, kernel32=fake)
    assert fake.calls == []
    codegraph_walk._release_job(5, kernel32=fake)
    assert fake.calls == [("CloseHandle", 5)]


# --- real processes, Windows only -------------------------------------------

win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object semantics")

FAKE_ANALYZER = """import os, sys, time
from pathlib import Path
Path(os.environ["FAKE_PID_FILE"]).write_text(str(os.getpid()))
deadline = time.time() + 120
while time.time() < deadline:
    time.sleep(0.05)
sys.exit(0)
"""


def _pid_alive(pid: int) -> bool:
    """Kernel-level liveness (OpenProcess + GetExitCodeProcess): `tasklist`
    output is localized and, without a console, came back as None."""
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False  # gone (or never existed)
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _wait(pred, secs: float) -> bool:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


def _claim_held(project: str) -> bool:
    try:
        with codegraph_walk.walk_claim(project):
            return False
    except codegraph_walk.WalkAlreadyRunning:
        return True


@pytest.fixture()
def win_walk(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    script = tmp_path / "fake_analyzer.py"
    script.write_text(FAKE_ANALYZER, encoding="utf-8")
    pid_file = tmp_path / "child.pid"
    procs: list = []
    env = dict(os.environ, FAKE_PID_FILE=str(pid_file), VCT_STATE_DIR=str(state),
               PYTHONPATH=os.pathsep.join(
                   p for p in (str(REPO_ROOT), os.environ.get("PYTHONPATH", "")) if p))

    def start(project: str = "ProjWinL5"):
        proc = subprocess.Popen(
            [sys.executable, "-m", "vco_lib.codegraph_walk", str(script),
             "--project", project, str(tmp_path)],
            env=env, cwd=str(tmp_path), stderr=subprocess.PIPE, text=True)
        procs.append(proc)
        return proc

    def child_pid():
        assert _wait(lambda: pid_file.exists() and pid_file.read_text().strip(), 60), \
            "the fake analyzer never started"
        return int(pid_file.read_text())

    yield start, child_pid
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if pid_file.exists():
        subprocess.run(["taskkill", "/F", "/PID", pid_file.read_text().strip()],
                       capture_output=True, check=False)


@win_only
def test_terminating_only_the_wrapper_kills_the_analyzer_and_frees_the_claim(win_walk):
    """RED before the fix: TerminateProcess on the wrapper left the analyzer
    running and the claim free (a twin could start beside a live walk)."""
    start, child_pid = win_walk
    wrapper = start()
    child = child_pid()
    assert _claim_held("ProjWinL5")
    wrapper.kill()  # TerminateProcess — what Stop-Process / Task Manager do
    wrapper.wait(timeout=30)
    assert _wait(lambda: not _pid_alive(child), 30), \
        "the analyzer must die with the wrapper (kill-on-close job)"
    assert not _claim_held("ProjWinL5")


@win_only
def test_a_second_walk_while_the_first_runs_exits_75(win_walk):
    start, child_pid = win_walk
    first = start()
    child_pid()
    second = start()
    _out, err = second.communicate(timeout=60)
    assert second.returncode == codegraph_walk.WALK_ALREADY_RUNNING_RC
    assert "already running" in err
    assert first.poll() is None, "the refusal never touches the running walk"
