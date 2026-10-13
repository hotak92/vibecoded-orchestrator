# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 NB-H1 + NB-H2 — pre-tool-use SSRF fail-closed + find-python venv probe.

NB-H1 (FRR:64) — a WebFetch payload whose ``url`` field cannot be extracted
(malformed ``tool_input``, parser error) used to skip the SSRF guard
entirely: ``URL`` came back empty and the ``if [[ -n "$URL" ]]`` arm simply
did not run, so the fetch went out UNGUARDED. The no-Python arm already
fails CLOSED (exit 2); the extraction-failure arm now does too. Non-WebFetch
tools are unaffected, and a well-formed WebFetch payload never sees the new
arm.

NB-H2 (FRR:65) — ``_lib/find-python.sh`` decided "no Python on this
machine" from PATH alone, so a machine whose ONLY interpreter is the
orchestrator venv's (a healthy install!) reported a broken install. The
orchestrator venv is now probed FIRST (the ``_lib/resolve-vco-venv.{sh,ps1}``
ladder every other VCO-venv consumer uses), PATH second.

Every hook / lib runs for real against a project under ``tmp_path``; the
find-python legs stage COPIES of the two libs so the clone-relative tier of
the venv ladder can never leak the checkout's own state into the verdict.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "templates" / "hooks"
LIB = HOOKS / "_lib"
PWSH = shutil.which("pwsh")
BASH = shutil.which("bash") or "bash"
_SH_TOOLS = ("cat", "head", "grep", "dirname", "tr", "date", "mktemp", "tail",
             "rm", "sed", "mkdir")
_SHELLS = ["sh", pytest.param("ps1", marks=pytest.mark.skipif(PWSH is None, reason="pwsh not installed"))]

EXTRACT_MSG = "could not be extracted"


# --- NB-H1: WebFetch with an unreadable url fails CLOSED -------------------------


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    return proj


def _env(tmp_path: Path) -> dict:
    env = dict(child_env())
    for key in ("VCT_DISABLE_HOOKS", "VCT_VENV", "VCT_INSTALL_ROOT",
                "VCT_ORCHESTRATOR_ROOT"):
        env.pop(key, None)
    env.update(HOME=str(tmp_path / "home"), VCT_STATE_DIR=str(tmp_path / "state"))
    return env


def _run(impl: str, proj: Path, payload: dict, env: dict) -> subprocess.CompletedProcess:
    argv = ([BASH, str(HOOKS / "pre-tool-use.sh")] if impl == "sh" else
            [str(PWSH), *PWSH_BASE_FLAGS, "-NonInteractive", "-File", str(HOOKS / "pre-tool-use.ps1")])
    return subprocess.run(argv, input=json.dumps(payload), capture_output=True,
                          text=True, timeout=180,
                          env=dict(env, CLAUDE_PROJECT_DIR=str(proj)), cwd=str(proj))


@pytest.mark.parametrize("impl", _SHELLS)
@pytest.mark.parametrize(
    "tool_input",
    [
        "oops-not-an-object",          # tool_input does not decode to an object
        {"prompt": "x"},               # an object with NO url field
    ],
)
def test_webfetch_unreadable_url_fails_closed(impl: str, tool_input, tmp_path: Path) -> None:
    """The red case: today the empty URL makes the guard arm not run and the
    hook exits 0 — the fetch goes out UNGUARDED. A security guard that
    cannot read its input fails CLOSED (the no-Python arm's contract)."""
    payload = {"tool_name": "WebFetch", "tool_input": tool_input, "session_id": "s1"}
    res = _run(impl, _project(tmp_path), payload, _env(tmp_path))
    assert res.returncode == 2, (res.returncode, res.stderr[-500:])
    assert EXTRACT_MSG in res.stderr, res.stderr[-500:]


@pytest.mark.parametrize("impl", _SHELLS)
def test_bash_payload_without_url_is_unaffected(impl: str, tmp_path: Path) -> None:
    """Leave-alone: a Bash payload carries no url — the new arm is for
    WebFetch ONLY."""
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}, "session_id": "s1"}
    res = _run(impl, _project(tmp_path), payload, _env(tmp_path))
    assert res.returncode == 0, res.stderr[-500:]
    assert EXTRACT_MSG not in res.stderr


@pytest.mark.parametrize("impl", _SHELLS)
def test_wellformed_webfetch_never_sees_the_extraction_arm(impl: str, tmp_path: Path) -> None:
    """Leave-alone: a well-formed WebFetch payload goes to the REAL guard —
    whatever its verdict, the extraction-failure message must not appear."""
    payload = {"tool_name": "WebFetch",
               "tool_input": {"url": "https://example.com/"}, "session_id": "s1"}
    res = _run(impl, _project(tmp_path), payload, _env(tmp_path))
    assert EXTRACT_MSG not in res.stderr, res.stderr[-500:]


# --- NB-H2: find-python probes the orchestrator venv BEFORE PATH -----------------


def _stage_libs(tmp_path: Path) -> Path:
    """Copies of the two libs under a throwaway hooks/ dir, so the venv
    ladder's clone-relative tier (script_dir/../..) lands on a NON-clone and
    only the env tiers can resolve."""
    lib = tmp_path / "hooks" / "_lib"
    lib.mkdir(parents=True, exist_ok=True)
    for name in ("find-python.sh", "resolve-vco-venv.sh",
                 "find-python.ps1", "resolve-vco-venv.ps1"):
        shutil.copy2(LIB / name, lib / name)
    return lib


def _nopy_path(tmp_path: Path, with_python: bool = False) -> str:
    bindir = tmp_path / ("bin-py" if with_python else "bin-nopy")
    bindir.mkdir(exist_ok=True)
    for tool in _SH_TOOLS:
        src = shutil.which(tool)
        assert src, tool
        link = bindir / tool
        if not link.exists():
            link.symlink_to(src)
    if with_python:
        real = shutil.which("python3")
        assert real
        (bindir / "python3").symlink_to(real)
    return str(bindir)


def _fake_venv(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "orch"
    venv_py = root / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("# fake interpreter — resolution only, never executed\n",
                       encoding="utf-8")
    venv_py.chmod(0o755)
    scripts_py = root / ".venv" / "Scripts" / "python.exe"
    scripts_py.parent.mkdir(parents=True, exist_ok=True)
    scripts_py.write_text("# fake\n", encoding="utf-8")
    return root, venv_py


def _lib_env(tmp_path: Path, path: str, install_root: Path | None) -> dict:
    env = dict(child_env())
    for key in ("VCT_VENV", "VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT"):
        env.pop(key, None)
    env["PATH"] = path
    env["HOME"] = str(tmp_path / "home")
    if install_root is not None:
        env["VCT_INSTALL_ROOT"] = str(install_root)
    return env


def _sh_py(lib: Path, env: dict) -> str:
    script = (
        f'. "{lib}/find-python.sh"\n'
        'if [ -n "$PY" ]; then printf "%s" "$PY"; else printf "EMPTY"; fi\n'
    )
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                       timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_find_python_probes_venv_when_path_has_no_python(tmp_path: Path) -> None:
    """The red case: empty PATH + a fake orchestrator venv → $PY must be the
    VENV interpreter. Today find-python only runs `command -v` and reports
    EMPTY — the broken-install misdiagnosis of NB-H2."""
    lib = _stage_libs(tmp_path)
    root, venv_py = _fake_venv(tmp_path)
    env = _lib_env(tmp_path, _nopy_path(tmp_path), root)
    assert _sh_py(lib, env) == str(venv_py)


def test_find_python_venv_wins_over_path(tmp_path: Path) -> None:
    """The venv is probed FIRST: even with a python3 on PATH, $PY is the
    venv interpreter (the resolve-vco-venv discipline — hooks must use VCO's
    own interpreter, never whatever PATH happens to carry)."""
    lib = _stage_libs(tmp_path)
    root, venv_py = _fake_venv(tmp_path)
    env = _lib_env(tmp_path, _nopy_path(tmp_path, with_python=True), root)
    assert _sh_py(lib, env) == str(venv_py)


def test_find_python_path_fallback_preserved(tmp_path: Path) -> None:
    """Leave-alone: no venv anywhere (the staged libs' clone tier lands on a
    non-clone) → the PATH probe decides, exactly as before NB-H2."""
    lib = _stage_libs(tmp_path)
    env = _lib_env(tmp_path, _nopy_path(tmp_path, with_python=True), None)
    assert _sh_py(lib, env).endswith("python3")


def test_find_python_still_empty_when_nothing_exists(tmp_path: Path) -> None:
    """Leave-alone: no venv AND no PATH python → $PY stays EMPTY — the
    broken-install contract pre-tool-use's no-Python arm depends on."""
    lib = _stage_libs(tmp_path)
    env = _lib_env(tmp_path, _nopy_path(tmp_path), None)
    assert _sh_py(lib, env) == "EMPTY"


def test_find_python_path_only_opt_out(tmp_path: Path) -> None:
    """The documented VCO_FIND_PYTHON_PATH_ONLY=1 opt-out: even with a
    resolvable venv, PATH decides (the pre-NB-H2 behaviour) — the seam the
    interpreter-less-machine simulations in the v0.2.100 suites pin."""
    lib = _stage_libs(tmp_path)
    root, _venv_py = _fake_venv(tmp_path)
    env = _lib_env(tmp_path, _nopy_path(tmp_path, with_python=True), root)
    env["VCO_FIND_PYTHON_PATH_ONLY"] = "1"
    assert _sh_py(lib, env).endswith("python3")


def test_find_python_path_only_opt_out_empty(tmp_path: Path) -> None:
    lib = _stage_libs(tmp_path)
    root, _venv_py = _fake_venv(tmp_path)
    env = _lib_env(tmp_path, _nopy_path(tmp_path), root)
    env["VCO_FIND_PYTHON_PATH_ONLY"] = "1"
    assert _sh_py(lib, env) == "EMPTY"


# --- NB-H2 ps1 leg ----------------------------------------------------------------


def _ps1_py(lib: Path, env_path: str, install_root: Path | None) -> str:
    root_line = f'$env:VCT_INSTALL_ROOT = "{install_root}"' if install_root else \
        'Remove-Item Env:VCT_INSTALL_ROOT -ErrorAction SilentlyContinue'
    script = (
        f'$env:PATH = "{env_path}"\n'
        'Remove-Item Env:VCT_VENV -ErrorAction SilentlyContinue\n'
        'Remove-Item Env:VCT_ORCHESTRATOR_ROOT -ErrorAction SilentlyContinue\n'
        f'{root_line}\n'
        f'. "{lib}/find-python.ps1"\n'
        'if ($PY) { Write-Output $PY } else { Write-Output "EMPTY" }\n'
    )
    r = subprocess.run([str(PWSH), *PWSH_BASE_FLAGS, "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=120,
                       env=dict(child_env()))
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.skipif(PWSH is None, reason="pwsh not installed")
def test_find_python_ps1_probes_venv_first(tmp_path: Path) -> None:
    lib = _stage_libs(tmp_path)
    root, _venv_py = _fake_venv(tmp_path)
    want = str(root / ".venv" / "Scripts" / "python.exe")
    got = _ps1_py(lib, _nopy_path(tmp_path), root)
    assert os.path.normpath(got) == os.path.normpath(want), got


@pytest.mark.skipif(PWSH is None, reason="pwsh not installed")
def test_find_python_ps1_still_empty_when_nothing_exists(tmp_path: Path) -> None:
    lib = _stage_libs(tmp_path)
    assert _ps1_py(lib, _nopy_path(tmp_path), None) == "EMPTY"


@pytest.mark.skipif(PWSH is None, reason="pwsh not installed")
def test_find_python_ps1_path_only_opt_out(tmp_path: Path) -> None:
    """The ps1 half of the opt-out pin (MUST MATCH the .sh sibling): with
    the knob set the venv is NOT probed, PATH decides."""
    lib = _stage_libs(tmp_path)
    root, _venv_py = _fake_venv(tmp_path)
    script_knob = 'Set-Item Env:VCO_FIND_PYTHON_PATH_ONLY "1"\n'
    root_line = f'$env:VCT_INSTALL_ROOT = "{root}"'
    script = (
        f'{script_knob}'
        f'$env:PATH = "{_nopy_path(tmp_path)}"\n'
        'Remove-Item Env:VCT_VENV -ErrorAction SilentlyContinue\n'
        'Remove-Item Env:VCT_ORCHESTRATOR_ROOT -ErrorAction SilentlyContinue\n'
        f'{root_line}\n'
        f'. "{lib}/find-python.ps1"\n'
        'if ($PY) { Write-Output $PY } else { Write-Output "EMPTY" }\n'
    )
    r = subprocess.run([str(PWSH), *PWSH_BASE_FLAGS, "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=120,
                       env=dict(child_env()))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "EMPTY"
