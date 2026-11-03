# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105: no hook-reached child may get a visible console window on Windows.

Field report (Windows 11, VS Code extension): a console window flashed for a
fraction of a second on every hook fire — dozens per minute. The two ways a
VCO hook could create a NEW (visible) console are pinned here:

* Python children started ``DETACHED_PROCESS``: such a child has no console,
  so every console program it starts — first of all the base interpreter a
  venv ``python.exe`` launcher starts — gets a fresh, visible one (and Win32
  ignores ``CREATE_NO_WINDOW`` next to ``DETACHED_PROCESS``). The ONE home,
  ``vco_lib.windowless_spawn``, now detaches with
  ``CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP``; every hook-reached
  detached spawn goes through it.
* PowerShell ``Start-Process`` children: a new console that ``-WindowStyle
  Hidden`` only asks to hide. ``Start-VcoDetachedProcess`` no longer uses
  Start-Process on Windows; it hands the spawn to the Python home's
  ``detach`` CLI (driven here on a POSIX host through the test-only
  ``$script:VcoSpawnViaWindowlessHelper`` switch, so the whole Windows wiring
  runs end to end).

And the hook REGISTRATION shape is pinned: Claude Code starts every hook with
``windowsHide`` (so the registered interpreter gets a windowless console);
``-WindowStyle Hidden`` in a registration would only hide a console AFTER
PowerShell started — the very flash — so no registered command may carry it.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS
from vco_lib import windowless_spawn as ws

REPO = Path(__file__).resolve().parents[1]
PS_LIB = REPO / "templates" / "hooks" / "_lib" / "resolve-powershell.ps1"
HELPER = REPO / "vco_lib" / "windowless_spawn.py"
WIN_TEMPLATE = REPO / "templates" / "settings.json.windows.template"

NO_WINDOW_DETACHED = ws.CREATE_NO_WINDOW | ws.CREATE_NEW_PROCESS_GROUP


# ---------------------------------------------------------------------------
# The Python home: flags
# ---------------------------------------------------------------------------

def test_no_window_kwargs_windows_and_posix():
    assert ws.no_window_popen_kwargs(os_name="nt") == {"creationflags": 0x0800_0000}
    assert ws.no_window_popen_kwargs(os_name="posix") == {}


def test_detached_kwargs_windows_never_detached_process():
    kw = ws.detached_popen_kwargs(os_name="nt")
    assert kw == {"creationflags": NO_WINDOW_DETACHED}
    assert kw["creationflags"] & ws.DETACHED_PROCESS == 0, (
        "DETACHED_PROCESS leaves the child console-less: every console program "
        "it starts (the venv python.exe's base interpreter first) flashes a window"
    )
    assert kw["creationflags"] & ws.CREATE_NO_WINDOW


def test_detached_kwargs_posix_is_a_new_session():
    assert ws.detached_popen_kwargs(os_name="posix") == {"start_new_session": True}


# ---------------------------------------------------------------------------
# The Python home: spawn_detached
# ---------------------------------------------------------------------------

class _Rec:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, argv, **kw):
        self.calls.append({"argv": argv, **kw})
        return mock.Mock(pid=4242)


def test_spawn_detached_windows_flags_and_handle_hygiene(tmp_path):
    rec = _Rec()
    inp = tmp_path / "in.txt"
    inp.write_text("payload", encoding="utf-8")
    ws.spawn_detached(["prog", "a b", ""], cwd=str(tmp_path), stdin_path=str(inp),
                      stdout_path=str(tmp_path / "o.log"), stderr_path=str(tmp_path / "e.log"),
                      popen=rec, os_name="nt")
    (call,) = rec.calls
    assert call["argv"] == ["prog", "a b", ""]
    assert call["creationflags"] == NO_WINDOW_DETACHED
    assert call["close_fds"] is True, "a detached child must not inherit the hook's pipes"
    assert call["cwd"] == str(tmp_path)
    assert call["stdin"].name == str(inp)
    assert call["stdout"].name.endswith("o.log")
    assert call["stderr"].name.endswith("e.log")
    # the parent closed its copies right after the spawn
    assert call["stdin"].closed and call["stdout"].closed and call["stderr"].closed


def test_spawn_detached_defaults_to_devnull_and_appends_when_asked(tmp_path):
    rec = _Rec()
    ws.spawn_detached(["prog"], popen=rec, os_name="posix")
    call = rec.calls[0]
    assert call["stdin"] is subprocess.DEVNULL
    assert call["stdout"] is subprocess.DEVNULL
    assert call["stderr"] is subprocess.DEVNULL
    assert call["start_new_session"] is True
    log = tmp_path / "err.log"
    log.write_text("old\n", encoding="utf-8")
    ws.spawn_detached(["prog"], stderr_path=str(log), stderr_append=True, popen=rec, os_name="posix")
    assert rec.calls[1]["stderr"].mode == "ab"
    ws.spawn_detached(["prog"], stderr_path=str(log), popen=rec, os_name="posix")
    assert rec.calls[2]["stderr"].mode == "wb"


def test_spawn_detached_same_out_and_err_path_is_one_log(tmp_path):
    rec = _Rec()
    log = str(tmp_path / "one.log")
    ws.spawn_detached(["prog"], stdout_path=log, stderr_path=log, popen=rec, os_name="posix")
    assert rec.calls[0]["stderr"] is subprocess.STDOUT


def test_spawn_detached_rejects_an_empty_argv():
    with pytest.raises(ValueError):
        ws.spawn_detached([], popen=_Rec())


def _b64(spec: Any) -> str:
    return base64.b64encode(json.dumps(spec).encode("utf-8")).decode("ascii")


def test_cli_prints_the_pid_and_exits_zero():
    rec = _Rec()
    out, err = io.StringIO(), io.StringIO()
    rc = ws.main(["detach", "--spec-b64", _b64({"argv": ["prog", "x"]})], popen=rec, out=out, err=err)
    assert rc == 0
    assert out.getvalue() == "4242\n"
    assert err.getvalue() == ""
    assert rec.calls[0]["argv"] == ["prog", "x"]


def test_cli_reports_a_failed_start_on_stderr_and_prints_no_pid():
    def boom(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    out, err = io.StringIO(), io.StringIO()
    rc = ws.main(["detach", "--spec-b64", _b64({"argv": ["/no/such/prog"]})], popen=boom, out=out, err=err)
    assert rc == 1
    assert out.getvalue() == ""
    assert err.getvalue().startswith("[vco] detached spawn FAILED for '/no/such/prog':")


@pytest.mark.parametrize("bad", ["not-base64!", _b64({"argv": []}), _b64(["argv"]), _b64({"argv": [1]})])
def test_cli_rejects_a_malformed_spec(bad):
    out, err = io.StringIO(), io.StringIO()
    rc = ws.main(["detach", "--spec-b64", bad], popen=_Rec(), out=out, err=err)
    assert rc == 1 and out.getvalue() == ""
    assert "detached spawn FAILED" in err.getvalue()


def test_cli_wait_relays_the_childs_exit_code():
    proc = mock.Mock(pid=7)
    proc.wait.return_value = 5
    out = io.StringIO()
    rc = ws.main(["detach", "--spec-b64", _b64({"argv": ["prog"], "wait": True})],
                 popen=lambda argv, **kw: proc, out=out, err=io.StringIO())
    assert rc == 5 and out.getvalue() == "7\n"
    proc.wait.assert_called_once()


def test_cli_without_wait_never_waits():
    proc = mock.Mock(pid=7)
    rc = ws.main(["detach", "--spec-b64", _b64({"argv": ["prog"]})],
                 popen=lambda argv, **kw: proc, out=io.StringIO(), err=io.StringIO())
    assert rc == 0
    proc.wait.assert_not_called()


def test_the_helper_runs_isolated_and_stdlib_only(tmp_path):
    """The PowerShell hooks run the helper BY PATH with ``-I`` (no vco_lib
    import, no PYTHONPATH): it must work that way, end to end."""
    marker = tmp_path / "marker.txt"
    child = [sys.executable, "-c",
             "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text('|'.join(sys.argv[2:]))",
             str(marker), "a b", "", 'q"uote']
    spec = {"argv": child, "cwd": str(tmp_path)}
    res = subprocess.run([sys.executable, "-I", str(HELPER), "detach", "--spec-b64", _b64(spec)],
                         capture_output=True, text=True, timeout=60, cwd=str(tmp_path),
                         env={"PATH": os.environ.get("PATH", "")})
    assert res.returncode == 0, res.stderr
    assert re.fullmatch(r"\d+\n", res.stdout)
    deadline = time.monotonic() + 30
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.read_text() == 'a b||q"uote'


# ---------------------------------------------------------------------------
# Every hook-reached detached spawn goes through the Python home
# (drive the caller with the home answering "Windows"; mutate the caller to
# its pre-v0.2.105 inline form and these go red)
# ---------------------------------------------------------------------------

@pytest.fixture
def as_windows():
    with mock.patch.object(ws, "_is_windows", return_value=True):
        yield


def test_hook_supervisor_relay_uses_the_windowless_detach(as_windows, tmp_path, monkeypatch):
    from vco_lib import hook_supervisor

    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    rec = _Rec()
    rec_proc = mock.Mock(pid=1)
    rec_proc.poll.return_value = 0
    rec_proc.returncode = 0

    def popen(argv, **kw):
        rec.calls.append(kw)
        return rec_proc

    hook_supervisor.relay_detached(["true"], name="t", budget_s=0.0, popen=popen,
                                   out=lambda s: None, sleep=lambda s: None)
    assert rec.calls and rec.calls[0]["creationflags"] == NO_WINDOW_DETACHED


def test_deferral_retry_driver_uses_the_windowless_detach(as_windows, tmp_path):
    from vco_lib import deferral_retry

    rec = _Rec()
    with mock.patch.object(deferral_retry.subprocess, "Popen", rec):
        deferral_retry.spawn_detached(tmp_path, python=sys.executable)
    assert rec.calls, "the driver was not spawned"
    assert rec.calls[0]["creationflags"] == NO_WINDOW_DETACHED
    assert "start_new_session" not in rec.calls[0]


def test_edit_post_check_poller_uses_the_windowless_detach(as_windows, tmp_path):
    from vco_lib import edit_post_check

    rec = _Rec()
    with mock.patch.object(edit_post_check.subprocess, "Popen", rec):
        edit_post_check._spawn_poller(tmp_path / "pending.json", str(tmp_path))
    assert rec.calls[0]["creationflags"] == NO_WINDOW_DETACHED


def test_rl_deferred_emit_sender_uses_the_windowless_detach(as_windows):
    from claude_mcp_servers.rl_client import deferred_emit

    rec = _Rec()
    with mock.patch.object(deferred_emit.subprocess, "Popen", rec):
        deferred_emit._spawn_child("{}")
    try:
        assert rec.calls[0]["creationflags"] == NO_WINDOW_DETACHED
    finally:
        Path(rec.calls[0]["argv"][-1]).unlink(missing_ok=True)


def test_launcher_ensure_spawn_uses_the_windowless_detach(as_windows, tmp_path):
    from vco_lib import launcher_ensure

    rec = _Rec()
    with mock.patch.object(launcher_ensure.subprocess, "Popen", rec):
        launcher_ensure._spawn(["launcher"], tmp_path)
    assert rec.calls[0]["creationflags"] == NO_WINDOW_DETACHED


# ---------------------------------------------------------------------------
# The PowerShell home: Start-VcoDetachedProcess's Windows branch
# ---------------------------------------------------------------------------

def _require_pwsh() -> str:
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if exe is None:
        assert not os.environ.get("CI"), "PowerShell is absent in CI; these gates must run"
        pytest.skip("no PowerShell interpreter on this machine")
    return exe


def _run_ps(script: str, tmp_path: Path, env_extra: Dict[str, str]) -> subprocess.CompletedProcess:
    exe = _require_pwsh()
    path = tmp_path / "drive.ps1"
    path.write_text(script, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("VCT_")}
    env.update(env_extra)
    return subprocess.run([exe, *PWSH_BASE_FLAGS, "-NonInteractive", "-File", str(path)],
                          capture_output=True, text=True, timeout=120, env=env, cwd=str(tmp_path))


def _helper_env(tmp_path: Path) -> Dict[str, str]:
    return {"VCT_VENV": sys.executable, "VCT_INSTALL_ROOT": str(REPO),
            "VCT_STATE_DIR": str(tmp_path / "state")}


def _wait_for(path: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def test_ps_spec_carries_every_element_intact(tmp_path):
    script = f"""
. '{PS_LIB}'
$b = New-VcoWindowlessSpawnSpec -FilePath 'C:\\Program Files\\x.exe' -ArgumentList @('a b', '', 'q"uote', 'trail\\', 'caf\u00e9') -WorkingDirectory 'C:\\w' -StdIn 'C:\\i' -StdOut 'C:\\o' -StdErr 'C:\\e' -StdErrAppend $true -Wait $true
Write-Output $b
Write-Output (Get-VcoWindowlessTrampolineArguments -Script 'C:\\VCO dir\\vco_lib\\windowless_spawn.py' -SpecB64 $b)
"""
    res = _run_ps(script, tmp_path, {})
    assert res.returncode == 0, res.stderr
    b64, args = res.stdout.strip().splitlines()
    spec = json.loads(base64.b64decode(b64).decode("utf-8"))
    assert spec == {
        "argv": ["C:\\Program Files\\x.exe", "a b", "", 'q"uote', "trail\\", "caf\u00e9"],
        "cwd": "C:\\w", "stdin": "C:\\i", "stdout": "C:\\o", "stderr": "C:\\e",
        "stderr_append": True, "wait": True,
    }
    assert re.fullmatch(r"[A-Za-z0-9+/=]+", b64), "the command line must carry only base64"
    assert args == f'-I "C:\\VCO dir\\vco_lib\\windowless_spawn.py" detach --spec-b64 {b64}'


def test_ps_windows_branch_spawns_through_the_python_home(tmp_path):
    """The spawn outlives the driver (child sleeps, then writes), argv
    elements survive, stdin/stdout redirects are honoured byte-exact, and the
    call itself returns nothing and prints nothing."""
    marker = tmp_path / "marker.txt"
    inp = tmp_path / "in.json"
    inp.write_bytes(b'{"k": 1}')
    out = tmp_path / "out.txt"
    code = ("import sys, time, pathlib; time.sleep(1.5); "
            "data = sys.stdin.buffer.read(); sys.stdout.buffer.write(data); "
            "pathlib.Path(sys.argv[1]).write_text('|'.join(sys.argv[2:]))")
    script = f"""
. '{PS_LIB}'
$script:VcoSpawnViaWindowlessHelper = $true
$r = @(Start-VcoDetachedProcess -FilePath '{sys.executable}' -ArgumentList @('-c', "{code}", '{marker}', 'a b', '') -RedirectStandardInput '{inp}' -RedirectStandardOutput '{out}')
Write-Output ("returned=" + $r.Count)
"""
    # The venv Python is a recording shim, so the test proves the spawn went
    # THROUGH the Python home (a POSIX-branch spawn would also land the marker).
    calls = tmp_path / "helper-calls.log"
    shim = tmp_path / "venv-python"
    shim.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\nexec "{sys.executable}" "$@"\n',
                    encoding="utf-8")
    shim.chmod(0o755)
    env = _helper_env(tmp_path)
    env["VCT_VENV"] = str(shim)
    res = _run_ps(script, tmp_path, env)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "returned=0", res.stdout
    helper_call = calls.read_text(encoding="utf-8")
    assert f"-I {HELPER} detach --spec-b64 " in helper_call, helper_call
    assert "FAILED" not in res.stderr, res.stderr
    assert not marker.exists(), "the driver waited for the child: it was not detached"
    _wait_for(marker)
    assert marker.read_text() == "a b|"
    assert out.read_bytes() == b'{"k": 1}'


def test_ps_windows_branch_passthru_relays_the_childs_exit_code(tmp_path):
    script = f"""
. '{PS_LIB}'
$script:VcoSpawnViaWindowlessHelper = $true
$p = Start-VcoDetachedProcess -FilePath '{sys.executable}' -ArgumentList @('-c', 'import sys; sys.exit(5)') -PassThru
$p.WaitForExit()
Write-Output ("rc=" + $p.ExitCode)
"""
    res = _run_ps(script, tmp_path, _helper_env(tmp_path))
    assert res.returncode == 0, res.stderr
    assert "rc=5" in res.stdout, (res.stdout, res.stderr)


def test_ps_windows_branch_reports_a_failed_start_once(tmp_path):
    script = f"""
. '{PS_LIB}'
$script:VcoSpawnViaWindowlessHelper = $true
$r = Start-VcoDetachedProcess -FilePath '{tmp_path}/no-such-program' -PassThru
Write-Output ("null=" + ($null -eq $r))
"""
    res = _run_ps(script, tmp_path, _helper_env(tmp_path))
    assert res.returncode == 0, res.stderr
    assert "null=True" in res.stdout, "a failed -PassThru spawn must hand back no process"
    assert res.stderr.count("[vco] detached spawn FAILED for") == 1, res.stderr
    assert "no-such-program" in res.stderr


def test_ps_windows_branch_unresolvable_helper_is_a_loud_broken_install(tmp_path):
    """No venv Python / no orchestrator root: nothing is started, and the one
    stderr line says it is a broken install (never a silent Start-Process)."""
    lib_copy = tmp_path / "proj" / ".claude" / "hooks" / "_lib"
    lib_copy.mkdir(parents=True)
    for name in ("resolve-powershell.ps1", "resolve-vco-venv.ps1"):
        shutil.copy(REPO / "templates" / "hooks" / "_lib" / name, lib_copy / name)
    marker = tmp_path / "never.txt"
    script = f"""
. '{lib_copy / "resolve-powershell.ps1"}'
$script:VcoSpawnViaWindowlessHelper = $true
Start-VcoDetachedProcess -FilePath '{sys.executable}' -ArgumentList @('-c', "open(r'{marker}', 'w')")
Write-Output "after"
"""
    res = _run_ps(script, tmp_path, {"VCT_VENV": str(tmp_path / "no-venv"),
                                      "VCT_STATE_DIR": str(tmp_path / "state")})
    assert res.returncode == 0, res.stderr
    assert "after" in res.stdout
    assert "detached spawn FAILED" in res.stderr and "broken install" in res.stderr, res.stderr
    time.sleep(1.0)
    assert not marker.exists()


# ---------------------------------------------------------------------------
# The hook registration shape (registry test over the shipped template)
# ---------------------------------------------------------------------------

_WIN_SHAPE = re.compile(
    r'^powershell -NoProfile -ExecutionPolicy Bypass -File '
    r'"\$\{CLAUDE_PROJECT_DIR\}/\.claude/hooks/[A-Za-z0-9][A-Za-z0-9._-]*\.ps1"'
    r'(?: (?:[A-Za-z0-9_-]+|"\$[A-Z_]+"))*$')


def _windows_commands() -> List[str]:
    data = json.loads(WIN_TEMPLATE.read_text(encoding="utf-8"))
    out: List[str] = []
    for groups in data["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                if hook.get("type") == "command":
                    out.append(hook["command"])
    return out


def test_every_windows_hook_command_has_the_windowless_shape():
    commands = _windows_commands()
    assert len(commands) > 100
    bad = [c for c in commands if not _WIN_SHAPE.match(c)]
    assert not bad, f"hook commands off the shipped shape: {bad[:5]}"
    hidden = [c for c in commands if "windowstyle" in c.lower()]
    assert not hidden, (
        "-WindowStyle Hidden hides a console only after PowerShell started in it "
        f"- the flash itself: {hidden[:3]}")


def test_the_shape_check_rejects_the_flashing_spelling():
    ok = 'powershell -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PROJECT_DIR}/.claude/hooks/x.ps1"'
    assert _WIN_SHAPE.match(ok)
    assert _WIN_SHAPE.match(ok + " pretooluse")
    assert not _WIN_SHAPE.match(ok.replace("-NoProfile", "-NoProfile -WindowStyle Hidden"))
    assert not _WIN_SHAPE.match('cmd /c start "" ' + ok)
