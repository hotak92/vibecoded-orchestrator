# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Every hub-port reader runs ONE case table (v0.2.97).

Owner ruling 2026-09-24: a set-but-INVALID ``VCT_HUB_PORT`` falls through to
``<state>/hub.port`` — the file names the running hub — and only then to 7700;
a valid port is an integer in 1..65535, for env and file alike. R7b F9 made the
VALUE rule exact and shared: after trimming the ends, ASCII ``[0-9]{1,5}`` in
range — a sign, ``_``, a non-ASCII numeral or internal whitespace is invalid.

The table (``tests/fixtures/hub_port_cases.json``) carries three columns, one
per ladder:

* ``expect`` — a CLIENT's ladder (env pin → file → 7700): the Python reader
  ``vco_lib.hub_ensure.resolve_hub_port`` and its callers
  (``vco verify-diagrams`` since R7b F3), every sh / ps1 client below, and
  ``vct-cli`` (its own Rust test runs the same file).
* ``expect_running`` — DESCRIBING the running hub (file → env → 7700):
  ``hub_ensure.running_hub_port`` (``install.py --bootstrap --json``, R7b F7)
  and Rust ``services::hub_port::resolve_hub_port``.
* ``expect_file`` — the STRICT file-only read: ``hub_ensure.read_hub_port_file``
  (install.py's hub health probe, R7b F3) and Rust ``read_hub_port_file_in``.

Each shell client's port function (and its value helper) is extracted from the
script and run alone, so no hub, token or CLI entry point is involved. No
reader here opens a socket: install.py's probe has ``open_probe`` replaced.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vco_lib import hub_ensure

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_PWSH = shutil.which("pwsh") or shutil.which("powershell")

# (script, port function, extra function names the port function calls,
# stub body defining anything else it calls).
BASH_CLIENTS = [
    ("templates/scripts/vct_secrets_resolve.sh", "hub_port", (), ""),
    ("templates/scripts/vct_access_check.sh", "_access_hub_port", (), ""),
    ("tools/vct-secrets/vct", "_hub_port", (), ""),
    # The warning-emitting client (F-8); its warnings are asserted by
    # tests/test_resolver_corrupt_discovery_inputs.py — here only the port.
    ("templates/scripts/vct_project_config.sh", "hub_port", ("_emit_port_warning",),
     "_emit_warning() { :; }\n"),
]
PS1_CLIENTS = [
    ("templates/scripts/vct_secrets_resolve.ps1", "Get-HubPort", (), ""),
    ("templates/scripts/vct_access_check.ps1", "Get-AccessHubPort", (),
     "function Get-AccessStateDir { return $Env:VCT_STATE_DIR }\n"),
    ("templates/scripts/vct_project_config.ps1", "Get-HubPort", ("Emit-PortWarning",),
     "function Emit-Warning { param($ErrorKind, $Detail, $StderrLine) }\n"),
]

TABLE = json.loads(
    (REPO_ROOT / "tests" / "fixtures" / "hub_port_cases.json").read_text(encoding="utf-8")
)["cases"]
_IDS = [c["name"] for c in TABLE]

# (env VCT_HUB_PORT or None, hub.port content or None, expected client port).
CASES = [(c["env_port"], c["file_port"], c["expect"]) for c in TABLE]


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
    env = {"PATH": "/usr/bin:/bin", "HOME": str(state.parent), "VCT_STATE_DIR": str(state),
           "LANG": "C.UTF-8"}
    if env_port is not None:
        env["VCT_HUB_PORT"] = env_port
    return env


def _set_env_port(monkeypatch: pytest.MonkeyPatch, env_port: str | None) -> None:
    if env_port is None:
        monkeypatch.delenv("VCT_HUB_PORT", raising=False)
    else:
        monkeypatch.setenv("VCT_HUB_PORT", env_port)


@pytest.mark.parametrize("case", TABLE, ids=_IDS)
def test_python_client_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case) -> None:
    state = _state(tmp_path, case["file_port"])
    _set_env_port(monkeypatch, case["env_port"])
    assert hub_ensure.resolve_hub_port(state) == case["expect"]


@pytest.mark.parametrize("case", TABLE, ids=_IDS)
def test_python_running_hub_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case) -> None:
    """R7b F7: the DESCRIBE ladder is file-first."""
    state = _state(tmp_path, case["file_port"])
    _set_env_port(monkeypatch, case["env_port"])
    assert hub_ensure.running_hub_port(state) == case["expect_running"]


@pytest.mark.parametrize("case", TABLE, ids=_IDS)
def test_python_strict_file_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case) -> None:
    state = _state(tmp_path, case["file_port"])
    _set_env_port(monkeypatch, case["env_port"])
    assert hub_ensure.read_hub_port_file(state) == case["expect_file"]


@pytest.mark.parametrize("case", TABLE, ids=_IDS)
def test_verify_diagrams_hub_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case) -> None:
    """R7b F3(a): ``vco verify-diagrams`` check 5 used ``$VCT_HUB_PORT`` else
    7700 and never read ``hub.port``."""
    from vco_lib.cli import verify_diagrams

    state = _state(tmp_path, case["file_port"])
    _set_env_port(monkeypatch, case["env_port"])
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    assert verify_diagrams._vct_hub_base_url() == f"http://127.0.0.1:{case['expect']}"


@pytest.mark.parametrize("case", TABLE, ids=_IDS)
def test_install_hub_health_probe_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case) -> None:
    """R7b F3(b): install.py's health probe reads ``hub.port`` STRICTLY (no
    file / not a port → no probe at all). It gated on ``str.isdigit()``, which
    accepts non-ASCII numerals, and accepted port 0."""
    import install

    state = _state(tmp_path, case["file_port"])
    _set_env_port(monkeypatch, case["env_port"])
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    probed: list[str] = []

    class _Resp:
        status = 200

    def fake_open_probe(url, timeout=None):  # never a socket
        probed.append(url)
        return _Resp()

    # The probe opens through the one probe opener (R8 G10).
    from vco_lib import service_probe_http

    monkeypatch.setattr(service_probe_http, "open_probe", fake_open_probe)
    healthy = install._probe_vct_hub_health()
    if case["expect_file"] is None:
        assert (healthy, probed) == (False, [])
    else:
        assert healthy is True
        assert probed == [f"http://127.0.0.1:{case['expect_file']}/api/v1/health"]


@pytest.mark.parametrize("script,function,extra,stub", BASH_CLIENTS)
@pytest.mark.parametrize("env_port,file_port,expected", CASES, ids=_IDS)
def test_bash_clients(tmp_path: Path, script, function, extra, stub, env_port, file_port, expected) -> None:
    source = (REPO_ROOT / script).read_text(encoding="utf-8")
    state = _state(tmp_path, file_port)
    snippet = (
        stub
        + _bash_function(source, "_hub_port_value")
        + "".join(_bash_function(source, name) for name in extra)
        + _bash_function(source, function)
        + f'state_dir="{state}"\n{function}\n'
    )
    r = subprocess.run(["bash", "-c", snippet], env=_env(state, env_port),
                       capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(expected), (script, r.stdout, r.stderr)


@pytest.mark.skipif(_PWSH is None, reason="no PowerShell runtime on PATH")
@pytest.mark.parametrize("script,function,extra,stub", PS1_CLIENTS)
@pytest.mark.parametrize("env_port,file_port,expected", CASES, ids=_IDS)
def test_ps1_clients(tmp_path: Path, script, function, extra, stub, env_port, file_port, expected) -> None:
    """Under ``$ErrorActionPreference = 'Stop'`` — the clients' own setting —
    so a value that makes the helper THROW fails here (R7b F9: a non-ASCII
    numeral did, through ``\\d`` + ``[long]``)."""
    source = (REPO_ROOT / script).read_text(encoding="utf-8-sig")
    state = _state(tmp_path, file_port)
    body = (
        "$ErrorActionPreference = 'Stop'\n"
        + stub
        + _ps1_function(source, "ConvertTo-HubPort")
        + "".join(_ps1_function(source, name) for name in extra)
        + _ps1_function(source, function)
    )
    lib = tmp_path / "lib.ps1"
    lib.write_text(body, encoding="utf-8")
    assert _PWSH is not None
    r = subprocess.run([_PWSH, "-NoProfile", "-NonInteractive", "-Command", f'. "{lib}"; {function}'],
                       env=_env(state, env_port), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(expected), (script, r.stdout, r.stderr)
