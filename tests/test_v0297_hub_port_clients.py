# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Every shell / PowerShell hub-port client agrees with the Python one (v0.2.97).

Owner ruling 2026-09-24: a set-but-INVALID ``VCT_HUB_PORT`` falls through to
``<state>/hub.port`` — the file names the running hub — and only then to 7700;
a valid port is an integer in 1..65535, for env and file alike. The F-8
clients (``vct_project_config.{sh,ps1}``) are driven with their warnings by
``tests/test_resolver_corrupt_discovery_inputs.py``; this file drives the
OTHER clients, which are silent, through the same cases, next to the Python
reader ``vco_lib.hub_ensure.resolve_hub_port``. Before v0.2.97 they printed an
invalid env value (or file content) verbatim — a malformed hub URL.

Each client's port function (and its value helper) is extracted from the
script and run alone, so no hub, token or CLI entry point is involved.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from vco_lib import hub_ensure

REPO_ROOT = Path(__file__).resolve().parent.parent
_PWSH = shutil.which("pwsh") or shutil.which("powershell")

BASH_CLIENTS = [
    ("templates/scripts/vct_secrets_resolve.sh", "hub_port"),
    ("templates/scripts/vct_access_check.sh", "_access_hub_port"),
    ("tools/vct-secrets/vct", "_hub_port"),
]
PS1_CLIENTS = [
    ("templates/scripts/vct_secrets_resolve.ps1", "Get-HubPort"),
    ("templates/scripts/vct_access_check.ps1", "Get-AccessHubPort"),
]

# (env VCT_HUB_PORT or None, hub.port content or None, expected port)
CASES = [
    ("not-a-port", "7811", 7811),   # invalid env falls through to the file
    ("0", "7812", 7812),            # out of range is invalid too
    ("7813", "7899", 7813),         # a valid env pin wins
    (None, "7814\n", 7814),         # the file
    (None, "70000", 7700),          # an out-of-range file → default
    (None, "garbage", 7700),
    ("junk", None, 7700),           # nothing usable → default
    (None, None, 7700),
]


def _bash_function(source: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", source, re.S | re.M)
    assert match, f"{name}() not found"
    return match.group(0)


def _ps1_function(source: str, name: str) -> str:
    match = re.search(rf"^function {re.escape(name)} \{{\n.*?^\}}\n", source, re.S | re.M)
    assert match, f"function {name} not found"
    return match.group(0)


def _state(tmp_path: Path, file_port: str | None) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    if file_port is not None:
        (state / "hub.port").write_text(file_port, encoding="utf-8")
    return state


def _env(state: Path, env_port: str | None) -> dict[str, str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(state.parent), "VCT_STATE_DIR": str(state)}
    if env_port is not None:
        env["VCT_HUB_PORT"] = env_port
    return env


@pytest.mark.parametrize("env_port,file_port,expected", CASES)
def test_python_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_port, file_port, expected) -> None:
    state = _state(tmp_path, file_port)
    if env_port is None:
        monkeypatch.delenv("VCT_HUB_PORT", raising=False)
    else:
        monkeypatch.setenv("VCT_HUB_PORT", env_port)
    assert hub_ensure.resolve_hub_port(state) == expected


@pytest.mark.parametrize("script,function", BASH_CLIENTS)
@pytest.mark.parametrize("env_port,file_port,expected", CASES)
def test_bash_clients(tmp_path: Path, script, function, env_port, file_port, expected) -> None:
    source = (REPO_ROOT / script).read_text(encoding="utf-8")
    state = _state(tmp_path, file_port)
    snippet = (
        _bash_function(source, "_hub_port_value")
        + _bash_function(source, function)
        + f'state_dir="{state}"\n{function}\n'
    )
    r = subprocess.run(["bash", "-c", snippet], env=_env(state, env_port),
                       capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(expected), (script, r.stdout, r.stderr)


@pytest.mark.skipif(_PWSH is None, reason="no PowerShell runtime on PATH")
@pytest.mark.parametrize("script,function", PS1_CLIENTS)
@pytest.mark.parametrize("env_port,file_port,expected", CASES)
def test_ps1_clients(tmp_path: Path, script, function, env_port, file_port, expected) -> None:
    source = (REPO_ROOT / script).read_text(encoding="utf-8-sig")
    state = _state(tmp_path, file_port)
    body = _ps1_function(source, "ConvertTo-HubPort") + _ps1_function(source, function)
    if function == "Get-AccessHubPort":
        body += "function Get-AccessStateDir { return $Env:VCT_STATE_DIR }\n"
    lib = tmp_path / "lib.ps1"
    lib.write_text(body, encoding="utf-8")
    assert _PWSH is not None
    r = subprocess.run([_PWSH, "-NoProfile", "-NonInteractive", "-Command", f'. "{lib}"; {function}'],
                       env=_env(state, env_port), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(expected), (script, r.stdout, r.stderr)
