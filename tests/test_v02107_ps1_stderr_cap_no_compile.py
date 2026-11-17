# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 -- no PowerShell hook compiles C# when it fires.

``_lib/stderr-cap.ps1`` (dot-sourced by most ``.ps1`` hooks, ``pre-tool-use``
among them -- every tool call) used to build its capped stderr writer with
``Add-Type -TypeDefinition``. Windows PowerShell 5.1 has no cross-process
cache for that: each fire started ``csc.exe`` + ``cvtres.exe`` (measured
~150 ms per fire, ~310 ms under PowerShell 7, which compiles in-process with
Roslyn). The writer is now a PowerShell class, emitted in-process.

Static leg: no shipped hook or hook ``_lib`` file compiles source with
``Add-Type`` (``-AssemblyName`` only LOADS a framework assembly and stays
allowed). Behaviour leg (pwsh): the cap still caps, the exit code is kept,
``STDERR_CAP_DISABLE=1`` still bypasses, and the writer type sits in a
dynamic (Reflection.Emit) assembly -- a compiled one would not be.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"
CAP = HOOKS / "_lib" / "stderr-cap.ps1"
_PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(_PWSH is None, reason="pwsh not installed")

# Add-Type with a source to compile: -TypeDefinition / -MemberDefinition, a
# positional source string, or -Path/-LiteralPath to a .cs/.vb file. Only
# `Add-Type -AssemblyName X` (load, no compile) passes.
_ADD_TYPE = re.compile(r"\bAdd-Type\b(?![^\n#]*-AssemblyName)", re.IGNORECASE)
_CODE_PROVIDER = re.compile(r"CSharpCodeProvider|CodeDomProvider", re.IGNORECASE)


def _code_lines(text: str):
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        yield n, line


def test_no_shipped_hook_compiles_csharp():
    offenders = []
    for p in sorted(HOOKS.rglob("*.ps1")):
        for n, line in _code_lines(p.read_text(encoding="utf-8-sig")):
            if _ADD_TYPE.search(line) or _CODE_PROVIDER.search(line):
                offenders.append(f"{p.relative_to(REPO_ROOT)}:{n}: {line.strip()}")
    assert offenders == [], (
        "a hook fire must not compile C# (csc.exe per fire on Windows "
        f"PowerShell 5.1): {offenders}")


def _run(tmp_path: Path, body: str, **env_extra) -> subprocess.CompletedProcess:
    lib = tmp_path / "_lib"
    lib.mkdir(exist_ok=True)
    shutil.copy2(CAP, lib / "stderr-cap.ps1")
    script = tmp_path / "probe.ps1"
    script.write_text('. "$PSScriptRoot/_lib/stderr-cap.ps1"\n' + body,
                      encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("STDERR_CAP_DISABLE", "STDERR_CAP_BYTES")}
    env.update(env_extra)
    return subprocess.run(
        [str(_PWSH), *PWSH_BASE_FLAGS, "-NonInteractive", "-File", str(script)],
        stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=120)


@needs_pwsh
def test_cap_truncates_and_keeps_the_exit_code(tmp_path):
    res = _run(tmp_path,
               '[Console]::Error.WriteLine("0123456789")\n'
               '[Console]::Error.Write("abc")\n'
               "[Console]::Error.Write([char]'z')\n"
               '[Console]::Error.WriteLine(("x" * 500))\n'
               "exit 3\n",
               STDERR_CAP_BYTES="40")
    assert res.returncode == 3, res
    err = res.stderr.decode("utf-8", "replace").replace("\r\n", "\n")
    assert err.startswith("0123456789\nabcz"), err
    assert len(err) == 40, (len(err), err)


@needs_pwsh
def test_under_the_cap_everything_passes_and_write_error_is_routed(tmp_path):
    res = _run(tmp_path, 'Write-Error "an error record"\n'
                         '[Console]::Error.WriteLine("tail")\nexit 0\n')
    assert res.returncode == 0
    err = res.stderr.decode("utf-8", "replace")
    assert "an error record" in err and "tail" in err


@needs_pwsh
def test_disable_switch_leaves_stderr_uncapped(tmp_path):
    res = _run(tmp_path, '[Console]::Error.WriteLine(("y" * 300))\n',
               STDERR_CAP_BYTES="10", STDERR_CAP_DISABLE="1")
    assert res.stderr.decode().count("y") == 300


@needs_pwsh
def test_the_writer_type_is_emitted_in_process_not_compiled(tmp_path):
    """Where the CappedTextWriter type comes from decides whether a compiler
    ran: a PowerShell class lives in a DYNAMIC (Reflection.Emit) assembly;
    ``Add-Type -TypeDefinition`` produces a compiled PE image (csc.exe's
    temporary DLL under 5.1, Roslyn's in-memory image under 7), which is
    never dynamic."""
    res = _run(tmp_path,
               '$t = @([AppDomain]::CurrentDomain.GetAssemblies() | '
               'ForEach-Object { try { $_.GetTypes() } catch { @() } } | '
               "Where-Object { $_.Name -eq 'CappedTextWriter' })\n"
               '[Console]::Out.WriteLine("types=" + $t.Count)\n'
               '[Console]::Out.WriteLine("dynamic=" + ($t | ForEach-Object { $_.Assembly.IsDynamic }))\n')
    out = res.stdout.decode()
    assert "types=1" in out, (out, res.stderr)
    assert "dynamic=True" in out, (out, res.stderr)
