# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — install.py's relaunches must not end the process a parent waits on.

install.py continues itself under another interpreter at two sites: the venv
relaunch (``install._ensure_running_under_mcp_venv``) and the venv-rebuild
handoff (``install_companions.reexec_outside_venv``). Both used ``os.execve``.
On Windows that is the C runtime's ``_wexecve``: it STARTS a new process and
ends the caller with exit code 0 at once. So the launcher's update runner and
install.ps1 (``$LASTEXITCODE``) saw every relaunched run succeed, whatever it
did — and install.ps1 went on to its post-install step while the install was
still running.

Both sites now go through ``install_companions.hand_off``: ``os.execve`` on
POSIX; on Windows a child on this process's std streams, waited for, whose exit
code this process exits with. A rebuild handoff inside a relaunched run cannot
spawn-and-wait (a Windows process cannot delete the venv it runs from), so it
hands the run BACK to the waiting parent, which runs outside the venv.

Windows is simulated (``sys.platform``) with the process-level primitives
recorded; one test runs the Windows path for real on this OS, through a pipe,
the way the launcher consumes it.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import install
from vco_lib import install_companions as ic
from tests.common.child_env import child_env
from vco_lib import manifest_paths

_KEYS = (ic.ENV_RELAUNCHED, ic.ENV_BASE_PYTHON, ic.ENV_BASE_PYTHON_VERSION, ic.ENV_PARENT_WAITS)


class _Exited(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def prims(monkeypatch):
    """Record ``os.execve``, ``os._exit`` and the child runner; never act."""
    rec: dict = {"execve": [], "runs": [], "rcs": []}

    def _execve(path, argv, env):
        rec["execve"].append((path, list(argv), dict(env)))

    def _exit(code):
        raise _Exited(code)

    def _run_child(cmd, env):
        rec["runs"].append((list(cmd), dict(env)))
        return rec["rcs"].pop(0)

    monkeypatch.setattr(ic.os, "execve", _execve)
    monkeypatch.setattr(ic.os, "_exit", _exit)
    monkeypatch.setattr(ic, "run_child", _run_child)
    return rec


def _windows(monkeypatch) -> None:
    monkeypatch.setattr(ic.sys, "platform", "win32")


# ---------------------------------------------------------------------------
# hand_off
# ---------------------------------------------------------------------------


def test_posix_still_execs(prims):
    ic.hand_off("/venv/bin/python", ["/venv/bin/python", "install.py", "--update"], {"K": "v"})
    assert prims["execve"] == [("/venv/bin/python", ["/venv/bin/python", "install.py", "--update"], {"K": "v"})]
    assert prims["runs"] == []


@pytest.mark.parametrize("rc", [0, 1, 2, 7])
def test_windows_waits_and_exits_with_the_childs_exact_code(prims, monkeypatch, rc):
    _windows(monkeypatch)
    prims["rcs"].append(rc)
    with pytest.raises(_Exited) as done:
        ic.hand_off(r"C:\o\.venv\Scripts\python.exe",
                    [r"C:\o\.venv\Scripts\python.exe", "install.py", "--update"],
                    {ic.ENV_RELAUNCHED: "1", ic.ENV_BASE_PYTHON: r"C:\Py\python.exe"})
    assert done.value.code == rc
    assert prims["execve"] == [], "no exec on Windows: it would end this process with 0"
    ((cmd, env),) = prims["runs"]
    assert cmd == [r"C:\o\.venv\Scripts\python.exe", "install.py", "--update"]
    assert env[ic.ENV_RELAUNCHED] == "1" and env[ic.ENV_BASE_PYTHON] == r"C:\Py\python.exe"
    assert env[ic.ENV_PARENT_WAITS] == str(os.getpid()), "the child is told WHO waits for it"


def test_windows_ntstatus_exit_code_survives(prims, monkeypatch):
    """0xC000013A (killed by Ctrl-C) must reach the waiter as the same 32 bits."""
    _windows(monkeypatch)
    prims["rcs"].append(0xC000013A)
    with pytest.raises(_Exited) as done:
        ic.hand_off("py", ["py", "install.py"], {})
    assert done.value.code & 0xFFFFFFFF == 0xC000013A
    assert -(2 ** 31) <= done.value.code < 2 ** 31


def test_windows_handback_reruns_once_outside_the_venv(prims, monkeypatch):
    _windows(monkeypatch)
    prims["rcs"].extend([ic.RERUN_OUTSIDE_VENV_EXIT, 3])
    with pytest.raises(_Exited) as done:
        ic.hand_off("venvpy", ["venvpy", "install.py", "--lightweight"],
                    {ic.ENV_RELAUNCHED: "1", ic.ENV_BASE_PYTHON: sys.executable})
    assert done.value.code == 3, "the rerun's code is the answer"
    (first_cmd, first_env), (rerun_cmd, rerun_env) = prims["runs"]
    assert rerun_cmd == [sys.executable, "install.py", "--lightweight"]
    assert rerun_env[ic.ENV_RELAUNCHED] == "1", "the rerun must not relaunch back into the venv"
    assert rerun_env[ic.ENV_BASE_PYTHON] == sys.executable
    assert rerun_env[ic.ENV_PARENT_WAITS] == str(os.getpid()), "the rerun is watched too"


def test_windows_handback_is_honoured_once_only(prims, monkeypatch):
    _windows(monkeypatch)
    prims["rcs"].extend([ic.RERUN_OUTSIDE_VENV_EXIT, ic.RERUN_OUTSIDE_VENV_EXIT])
    with pytest.raises(_Exited) as done:
        ic.hand_off("venvpy", ["venvpy", "install.py"], {})
    assert len(prims["runs"]) == 2 and done.value.code == ic.RERUN_OUTSIDE_VENV_EXIT


def test_exec_replaces_process_follows_the_platform(monkeypatch):
    assert ic._exec_replaces_process() is (sys.platform != "win32")
    monkeypatch.setattr(ic.sys, "platform", "win32")
    assert ic._exec_replaces_process() is False


@pytest.mark.parametrize("rc, status", [(0, 0), (1, 1), (-15, 143), (0xFFFFFFFF, -1)])
def test_exit_status(rc, status):
    assert ic.exit_status(rc) == status


# ---------------------------------------------------------------------------
# the runner
# ---------------------------------------------------------------------------


def test_run_child_returns_the_real_exit_code():
    assert ic.run_child([sys.executable, "-c", "import sys; sys.exit(3)"], child_env()) == 3


def test_run_child_keeps_waiting_through_ctrl_c(monkeypatch):
    class _Proc:
        waits = 0

        def __init__(self, *_a, **_kw):
            pass

        def wait(self):
            _Proc.waits += 1
            if _Proc.waits == 1:
                raise KeyboardInterrupt
            return 5

    monkeypatch.setattr(ic.subprocess, "Popen", _Proc)
    assert ic.run_child(["x"], {}) == 5 and _Proc.waits == 2


def test_run_child_passes_std_streams_explicitly(monkeypatch):
    """All-None stdio on Windows means no STARTF_USESTDHANDLES and no inherited
    handles — the child could not write to the launcher's pipe."""
    seen: dict = {}

    class _Proc:
        def __init__(self, cmd, **kw):
            seen.update(kw)

        def wait(self):
            return 0

    real_fstat = os.fstat

    def _fstat(fd):
        if fd == 0:
            raise OSError("closed")
        return real_fstat(fd)

    monkeypatch.setattr(ic.os, "fstat", _fstat)
    monkeypatch.setattr(ic.subprocess, "Popen", _Proc)
    ic.run_child(["x"], {"E": "1"})
    assert (seen["stdin"], seen["stdout"], seen["stderr"]) == (None, 1, 2)
    assert seen["env"] == {"E": "1"}


def test_the_windows_path_for_real_through_a_pipe(tmp_path: Path):
    """The Windows branch run on this OS, consumed the way the launcher does:
    piped stdout, exit status of the ORIGINAL process."""
    script = tmp_path / "parent.py"
    script.write_text(textwrap.dedent(f"""
        import os, sys
        from vco_lib import install_companions as ic
        ic._exec_replaces_process = lambda: False
        child = "import os, sys; print('child sees', os.environ.get({ic.ENV_PARENT_WAITS!r})); sys.exit(4)"
        print("parent", os.getpid(), flush=True)
        ic.hand_off(sys.executable, [sys.executable, "-c", child], dict(os.environ))
        print("never printed")
    """), encoding="utf-8")
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60,
                          env=child_env())
    assert done.returncode == 4, done.stderr
    parent_line, child_line = done.stdout.splitlines()
    assert child_line == "child sees " + parent_line.split()[1], "the child is told the parent's pid"


# ---------------------------------------------------------------------------
# site 1: the venv relaunch
# ---------------------------------------------------------------------------


@pytest.fixture
def weaviate_missing(monkeypatch):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda n, p=None: None if n == "weaviate" else real(n, p))


@pytest.mark.parametrize("rc", [0, 1])
def test_relaunch_on_windows_waits_and_forwards(prims, weaviate_missing, monkeypatch, tmp_path, rc):
    target = tmp_path / "o" / ".venv" / "Scripts" / "python.exe"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    monkeypatch.setattr(install, "_resolve_venv_python_for_install", lambda _r: target)
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--update"])
    _windows(monkeypatch)
    prims["rcs"].append(rc)
    with pytest.raises(_Exited) as done:
        install._ensure_running_under_mcp_venv()
    assert done.value.code == rc and prims["execve"] == []
    ((cmd, env),) = prims["runs"]
    assert cmd == [str(target), "install.py", "--update"]
    assert env[ic.ENV_RELAUNCHED] == "1"
    assert env[ic.ENV_BASE_PYTHON] == sys.executable
    assert env[ic.ENV_PARENT_WAITS] == str(os.getpid())


def test_a_relaunched_run_does_not_relaunch_again(prims, weaviate_missing, monkeypatch, tmp_path):
    monkeypatch.setenv(ic.ENV_RELAUNCHED, "1")
    _windows(monkeypatch)
    install._ensure_running_under_mcp_venv()
    assert prims["runs"] == [] and prims["execve"] == []


# ---------------------------------------------------------------------------
# site 2: the rebuild handoff
# ---------------------------------------------------------------------------


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "orchestrator"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    manifest = manifest_paths.manifest_path(root)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("{}", encoding="utf-8")
    base = tmp_path / "system-python"
    base.write_text("", encoding="utf-8")
    monkeypatch.setenv(ic.ENV_RELAUNCHED, "1")
    monkeypatch.setenv(ic.ENV_BASE_PYTHON, str(base))
    monkeypatch.setenv(ic.ENV_BASE_PYTHON_VERSION, f"{sys.version_info.major}.{sys.version_info.minor}")
    monkeypatch.setattr(install, "PROJECT_ROOT", root)
    monkeypatch.setattr(install, "_create_state_directory", lambda: None)
    monkeypatch.setattr(install, "_log_install_event", lambda *a, **k: None)
    monkeypatch.setattr(install, "_install_requirements", lambda *a, **k: None)
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--lightweight", "--rebuild-venv"])
    monkeypatch.setattr(sys, "prefix", str(root / ".venv"))  # THIS process is the venv
    return root


def _rebuild() -> argparse.Namespace:
    return argparse.Namespace(lightweight=True, lightweight_old_path=None, no_containers=True,
                              dev=False, rebuild_venv=True)


@pytest.mark.parametrize("marker", ["4242", "1"])  # a pid; "1" = the pre-pid marker, still a waiting parent
def test_windows_rebuild_hands_back_to_the_waiting_parent(root, prims, monkeypatch, capsys, marker):
    _windows(monkeypatch)
    monkeypatch.setenv(ic.ENV_PARENT_WAITS, marker)
    with pytest.raises(_Exited) as done:
        install._run_lightweight(_rebuild())
    assert done.value.code == ic.RERUN_OUTSIDE_VENV_EXIT
    assert (root / ".venv" / "bin" / "python").is_file(), "nothing deleted by the process inside it"
    assert prims["execve"] == [] and prims["runs"] == []
    assert "handing the run back" in capsys.readouterr().out


def test_windows_rebuild_with_no_waiting_parent_is_refused(root, prims, monkeypatch, capsys):
    _windows(monkeypatch)
    assert install._run_lightweight(_rebuild()) == 1
    assert (root / ".venv" / "bin" / "python").is_file()
    assert prims["execve"] == [] and prims["runs"] == []
    out = capsys.readouterr().out
    assert "Venv rebuild REFUSED" in out and "re-run it with the base interpreter" in out
    assert "--rebuild-venv" in out, "the printed command is the one to run"


def test_posix_rebuild_still_execs_the_base_interpreter(root, prims, monkeypatch):
    install._run_lightweight(_rebuild())  # the recorded execve returns, so this then refuses
    ((path, argv, env),) = prims["execve"]
    base = os.environ[ic.ENV_BASE_PYTHON]
    assert path == base and argv == [base, "install.py", "--lightweight", "--rebuild-venv"]
    assert env[ic.ENV_RELAUNCHED] == "1" and ic.ENV_PARENT_WAITS not in env


# ---------------------------------------------------------------------------
# the parent watch: a kill of the waiting parent stops the run (POSIX parity)
# ---------------------------------------------------------------------------


class _FakeWinapi:
    SYNCHRONIZE = 0x00100000
    INFINITE = 0xFFFFFFFF

    def __init__(self) -> None:
        import threading

        self.opened: list = []
        self.parent_ended = threading.Event()
        self.open_error: OSError | None = None

    def OpenProcess(self, access, inherit, pid):  # noqa: N802 — the _winapi name
        if self.open_error is not None:
            raise self.open_error
        self.opened.append((access, inherit, pid))
        return 0x1234

    def WaitForSingleObject(self, handle, timeout):  # noqa: N802
        assert (handle, timeout) == (0x1234, self.INFINITE)
        self.parent_ended.wait()
        return 0


@pytest.fixture
def watch(monkeypatch):
    """A fake ``_winapi`` and a recorded ``os._exit``. Teardown ends the fake
    parent and joins the watcher BEFORE monkeypatch restores the real
    ``os._exit`` — a watcher left running would end pytest."""
    import threading

    fake = _FakeWinapi()
    exits: list = []
    exited = threading.Event()

    def _exit(code):
        exits.append(code)
        exited.set()

    monkeypatch.setitem(sys.modules, "_winapi", fake)
    monkeypatch.setattr(ic.os, "_exit", _exit)
    yield fake, exits, exited
    fake.parent_ended.set()
    for thread in threading.enumerate():
        if thread.name == "vct-install-parent-watch":
            thread.join(timeout=10)


def _watchers() -> list:
    import threading

    return [t for t in threading.enumerate() if t.name == "vct-install-parent-watch"]


def test_parent_killed_stops_the_run_with_the_documented_code(watch, monkeypatch, capsys):
    fake, exits, exited = watch
    _windows(monkeypatch)
    monkeypatch.setenv(ic.ENV_PARENT_WAITS, "4242")
    assert ic.start_parent_watch() is True
    assert fake.opened == [(fake.SYNCHRONIZE, False, 4242)]
    fake.parent_ended.set()
    assert exited.wait(timeout=10)
    assert exits == [ic.PARENT_GONE_EXIT]
    err = [line for line in capsys.readouterr().err.splitlines() if line]
    assert len(err) == 1 and "pid 4242" in err[0] and str(ic.PARENT_GONE_EXIT) in err[0]


def test_parent_alive_has_no_effect(watch, monkeypatch, capsys):
    fake, exits, exited = watch
    _windows(monkeypatch)
    monkeypatch.setenv(ic.ENV_PARENT_WAITS, "4242")
    assert ic.start_parent_watch() is True
    assert not exited.wait(timeout=0.3)
    assert exits == [] and capsys.readouterr().err == ""
    assert len(_watchers()) == 1 and _watchers()[0].daemon, "a daemon thread never holds the run open"


def test_posix_starts_no_watcher(watch, monkeypatch):
    fake, exits, _ = watch
    monkeypatch.setattr(ic.sys, "platform", "linux")
    monkeypatch.setenv(ic.ENV_PARENT_WAITS, "4242")
    assert ic.start_parent_watch() is False
    assert fake.opened == [] and _watchers() == []


def test_no_waiting_parent_starts_no_watcher(watch, monkeypatch):
    fake, _, _ = watch
    _windows(monkeypatch)
    assert ic.start_parent_watch() is False
    assert fake.opened == [] and _watchers() == []


def test_open_failure_is_logged_and_the_run_continues(watch, monkeypatch, capsys):
    fake, exits, _ = watch
    _windows(monkeypatch)
    monkeypatch.setenv(ic.ENV_PARENT_WAITS, "4242")
    fake.open_error = PermissionError(5, "Access is denied")
    assert ic.start_parent_watch() is False
    assert exits == [] and _watchers() == []
    err = capsys.readouterr().err
    assert "cannot watch" in err and "pid 4242" in err and "continuing" in err


@pytest.mark.parametrize("raw, pid", [("4242", 4242), (" 8 ", 8), ("1", 1), ("", None),
                                      ("x", None), ("0", None), ("-5", None)])
def test_waiting_parent_pid(monkeypatch, raw, pid):
    monkeypatch.setenv(ic.ENV_PARENT_WAITS, raw)
    assert ic.waiting_parent_pid() == pid


def test_install_main_starts_the_watch_first(monkeypatch):
    """Wired at the top of ``main()``: before anything a kill should interrupt."""
    class _Started(Exception):
        pass

    def _start():
        raise _Started

    monkeypatch.setattr(ic, "start_parent_watch", _start)
    monkeypatch.setattr(install, "_ensure_running_under_mcp_venv",
                        lambda: pytest.fail("main() ran on before starting the watch"))
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--help"])
    with pytest.raises(_Started):
        install.main()
