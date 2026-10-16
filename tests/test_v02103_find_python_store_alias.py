# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 (Windows field): ``templates/hooks/_lib/find-python.{sh,ps1}``
must not take the Microsoft Store app-execution-alias stub
(``%LOCALAPPDATA%\\Microsoft\\WindowsApps\\python.exe``) for an interpreter.

With no Python installed the stub is first on PATH; it prints "Python was
not found" (localized) and exits 9009, so every hook that used it failed.
The stub is simulated here by an executable at a ``.../Microsoft/WindowsApps/``
path that behaves exactly like that; the real interpreter is a symlink to the
test's own Python. The venv probe is switched off
(``VCO_FIND_PYTHON_PATH_ONLY=1``) so PATH alone decides.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "templates" / "hooks" / "_lib"
PWSH = shutil.which("pwsh")
BASH = shutil.which("bash") or "bash"
_SHELLS = ["sh", pytest.param("ps1", marks=pytest.mark.skipif(PWSH is None, reason="pwsh not installed"))]

STUB = "#!/bin/sh\necho 'Python was not found; run without arguments to install from the Microsoft Store.'\nexit 49\n"


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _store_dir(tmp_path: Path) -> Path:
    return tmp_path / "AppData" / "Local" / "Microsoft" / "WindowsApps"


def _stubs(tmp_path: Path) -> Path:
    d = _store_dir(tmp_path)
    for name in ("python3", "python", "py"):
        _exe(d / name, STUB)
    return d


def _real(tmp_path: Path, name: str = "python3") -> Path:
    d = tmp_path / "realbin"
    d.mkdir(parents=True, exist_ok=True)
    link = d / name
    link.symlink_to(sys.executable)
    return link


def _base_path(tmp_path: Path) -> str:
    """A PATH with the shell tools the libs need but NO python of its own."""
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    for tool in ("uname", "tr", "cat", "dirname"):
        found = shutil.which(tool)
        if found:
            (tools / tool).symlink_to(found)
    return str(tools)


def _resolve(impl: str, path: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env = dict(child_env())
    env["PATH"] = path
    env["VCO_FIND_PYTHON_PATH_ONLY"] = "1"
    if impl == "sh":
        script = f'. "{LIB / "find-python.sh"}"; printf "PY=%s\\n" "$PY"'
        argv = [BASH, "-c", script]
    else:
        script = f'. "{LIB / "find-python.ps1"}"; Write-Output "PY=$PY"'
        argv = [str(PWSH), *PWSH_BASE_FLAGS, "-NonInteractive", "-Command", script]
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, env=env,
                          cwd=str(tmp_path))


def _py(res: subprocess.CompletedProcess) -> str:
    for line in res.stdout.splitlines():
        if line.startswith("PY="):
            return line[3:].strip()
    raise AssertionError((res.stdout, res.stderr))


@pytest.mark.parametrize("impl", _SHELLS)
def test_store_alias_first_on_path_is_skipped_for_the_real_interpreter(impl, tmp_path):
    stubs = _stubs(tmp_path)
    real = _real(tmp_path, "python3" if impl == "sh" else "python")
    res = _resolve(impl, os.pathsep.join([str(stubs), str(real.parent), _base_path(tmp_path)]),
                   tmp_path)
    assert _py(res) == str(real), (res.stdout, res.stderr)
    assert "Microsoft Store" not in res.stderr


@pytest.mark.parametrize("impl", _SHELLS)
def test_only_store_aliases_leaves_py_empty_and_says_why(impl, tmp_path):
    stubs = _stubs(tmp_path)
    res = _resolve(impl, os.pathsep.join([str(stubs), _base_path(tmp_path)]), tmp_path)
    assert _py(res) == "", (res.stdout, res.stderr)
    assert "Microsoft Store 'python' alias" in res.stderr


@pytest.mark.parametrize("impl", _SHELLS)
def test_a_real_store_python_under_windowsapps_is_kept(impl, tmp_path):
    """A genuine Store install also lives in WindowsApps — it runs Python,
    so it is not skipped."""
    d = _store_dir(tmp_path)
    d.mkdir(parents=True)
    name = "python3" if impl == "sh" else "python"
    (d / name).symlink_to(sys.executable)
    res = _resolve(impl, os.pathsep.join([str(d), _base_path(tmp_path)]), tmp_path)
    assert _py(res) == str(d / name), (res.stdout, res.stderr)


@pytest.mark.parametrize("impl", _SHELLS)
def test_posix_resolution_is_unchanged(impl, tmp_path):
    """No WindowsApps on PATH: the first PATH match wins, as before."""
    real = _real(tmp_path, "python3" if impl == "sh" else "python")
    res = _resolve(impl, os.pathsep.join([str(real.parent), _base_path(tmp_path)]), tmp_path)
    assert _py(res) == str(real)
    assert res.stderr.strip() == "" or "Microsoft Store" not in res.stderr


def test_sh_prefers_the_py_launcher_on_windows(tmp_path):
    """Git Bash / MSYS: `py -3` is asked for the interpreter it runs, before
    any PATH name. Simulated by a `uname` reporting MINGW64 and a `py` that
    answers `-3 -c ...` with a real interpreter path."""
    real = _real(tmp_path, "python3")
    other = tmp_path / "otherbin"
    _exe(other / "python3", STUB.replace("exit 49", "exit 0"))  # must NOT be chosen
    fake = tmp_path / "fakebin"
    _exe(fake / "uname", "#!/bin/sh\necho MINGW64_NT-10.0\n")
    _exe(fake / "py", f'#!/bin/sh\n[ "$1" = "-3" ] && echo "{real}" && exit 0\nexit 1\n')
    res = _resolve("sh", os.pathsep.join([str(fake), str(other), _base_path(tmp_path)]), tmp_path)
    assert _py(res) == str(real), (res.stdout, res.stderr)
