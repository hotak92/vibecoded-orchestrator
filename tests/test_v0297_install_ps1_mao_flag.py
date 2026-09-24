# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``install.ps1 -WithMaoAgents`` must never abort the install (v0.2.97).

The MAO-tier specialist agents were folded into the standard agent set
before v0.2.0 (commit 79c2635b) and ``--with-mao-agents`` was stripped
from ``install.py`` — nothing replaced it; the capability became
unconditional.  Until this fix, ``install.ps1 -WithMaoAgents`` forwarded
the dead flag verbatim and ``install.py``'s strict
``parser.parse_args()`` rejected it, aborting the whole install.

This test DRIVES the real artefacts rather than scanning them:

* the actual arg-forwarding block is extracted from ``install.ps1`` and
  executed in pwsh with the switch variables set, exactly as a real
  invocation would resolve them (the block's only inputs are the
  switch/string parameters);
* the forwarded list is then validated against the REAL ``install.py``
  parser (captured by probing ``parse_args`` inside ``main()``), so any
  flag ``install.py`` would reject fails here — not in a user's install.

Red-proofed by reintroducing the dead forwarding line
(``if ($WithMaoAgents) { $installArgs += "--with-mao-agents" }``) — the
parser rejects it and this test goes red.
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL_PS1 = REPO / "install.ps1"
INSTALL_PY = REPO / "install.py"

_ARG_BLOCK_START = "$installArgs = @()"
_ARG_BLOCK_END = 'if ($Yes -or $NonInteractive) { $installArgs += "--yes" }'

#: Every parameter the forwarding block reads.  Values mirror the
#: parameter defaults users hit in practice.
_SWITCH_VARS = {
    "NoContainers": False,
    "Gpu": False,
    "CpuOnly": False,
    "LowResource": False,
    "OpenaiKey": "",
    "Container": "",
    "Dev": False,
    "Update": False,
    "SkipModels": False,
    "Quiet": False,
    "NoAgents": False,
    "WithMaoAgents": False,
    "NoSkills": False,
    "NoCompile": False,
    "Yes": False,
    "NonInteractive": False,
}


def _extract_forwarding_block() -> str:
    """Pull the real ``# Build arguments for install.py`` block."""
    src = INSTALL_PS1.read_text(encoding="utf-8-sig")
    start = src.index(_ARG_BLOCK_START)
    end = src.index(_ARG_BLOCK_END) + len(_ARG_BLOCK_END)
    assert start < end, "forwarding block markers out of order"
    return src[start:end]


def _run_forwarding(tmp_path: Path, overrides: dict[str, object]) -> tuple[list[str], list[str]]:
    """Execute the extracted block in pwsh with the given switch values.

    Returns the forwarded ``install.py`` argument list plus any
    ``Write-Warning`` texts the block emitted (captured by shadowing the
    cmdlet, so the assertion does not depend on stream plumbing).
    """
    harness = tmp_path / "forward_args.ps1"
    lines = [
        "$script:capturedWarnings = @()",
        # Shadow the cmdlet so warnings are captured deterministically.
        "function Write-Warning { param($m) $script:capturedWarnings += $m }",
    ]
    for name, default in _SWITCH_VARS.items():
        value = overrides.get(name, default)
        if isinstance(value, bool):
            lines.append(f"${name} = ${'true' if value else 'false'}")
        else:
            lines.append(f"${name} = '{value}'")
    lines += [
        _extract_forwarding_block(),
        "Write-Output '===ARGS==='",
        "$installArgs | ForEach-Object { Write-Output $_ }",
        "Write-Output '===WARN==='",
        "$script:capturedWarnings | ForEach-Object { Write-Output $_ }",
        "Write-Output '===END==='",
    ]
    harness.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    proc = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(harness)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"pwsh harness failed:\n{proc.stderr}"
    out = proc.stdout
    args = out.split("===ARGS===", 1)[1].split("===WARN===", 1)[0]
    warns = out.split("===WARN===", 1)[1].split("===END===", 1)[0]
    return (
        [line for line in args.splitlines() if line.strip()],
        [line for line in warns.splitlines() if line.strip()],
    )


@pytest.fixture(scope="module")
def install_py_parser() -> argparse.ArgumentParser:
    """The REAL ``install.py`` parser, captured at ``main()``'s parse_args."""
    captured: dict[str, argparse.ArgumentParser] = {}

    class _Probe(Exception):
        pass

    def _capture(self, args=None, namespace=None):  # noqa: ANN001
        captured["parser"] = self
        raise _Probe

    original = argparse.ArgumentParser.parse_args
    saved_argv = sys.argv
    sys.argv = ["install.py", "--no-containers"]  # never reaches the --update lock
    argparse.ArgumentParser.parse_args = _capture
    try:
        spec = importlib.util.spec_from_file_location(
            "v0297_install_ps1_under_test", INSTALL_PY
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.main()  # aborts at parse_args via _Probe, before side effects
    except _Probe:
        pass
    finally:
        argparse.ArgumentParser.parse_args = original
        sys.argv = saved_argv
    assert "parser" in captured, "probe never reached install.py's parse_args"
    return captured["parser"]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"WithMaoAgents": True}, id="mao-only"),
        pytest.param({"WithMaoAgents": True, "Yes": True}, id="mao-plus-yes"),
        pytest.param({"Yes": True}, id="plain-yes"),
        pytest.param(
            {
                "NoContainers": True,
                "SkipModels": True,
                "Quiet": True,
                "NoSkills": True,
                "NoCompile": True,
                "Yes": True,
            },
            id="realistic-combo",
        ),
        pytest.param(
            {
                "NoContainers": True,
                "Gpu": True,
                "CpuOnly": True,
                "LowResource": True,
                "OpenaiKey": "sk-test-not-a-secret",
                "Container": "podman",
                "Dev": True,
                "Update": True,
                "SkipModels": True,
                "Quiet": True,
                "NoAgents": True,
                "NoSkills": True,
                "NoCompile": True,
                "Yes": True,
            },
            id="everything-at-once",
        ),
    ],
)
def test_forwarded_args_are_all_accepted_by_install_py(
    tmp_path: Path,
    install_py_parser: argparse.ArgumentParser,
    overrides: dict[str, object],
) -> None:
    """Whatever install.ps1 forwards, install.py's parser must accept."""
    args, _warnings = _run_forwarding(tmp_path, overrides)
    assert "--with-mao-agents" not in args, (
        "install.ps1 forwards --with-mao-agents, which install.py rejects "
        "(the flag died with the MAO tier in 79c2635b) — that aborts the "
        "whole install on Windows."
    )
    install_py_parser.parse_args(args)  # raises SystemExit on any reject


def test_mao_switch_warns_and_is_ignored(tmp_path: Path) -> None:
    """The obsolete switch must warn, not abort nor change forwarded args."""
    mao_args, mao_warns = _run_forwarding(tmp_path, {"WithMaoAgents": True})
    plain_args, _ = _run_forwarding(tmp_path, {})
    assert mao_args == plain_args, (
        "-WithMaoAgents must not change the forwarded argument list"
    )
    assert mao_warns, "-WithMaoAgents must print an explanatory warning"
    assert "obsolete" in " ".join(mao_warns).lower()


def test_mao_switch_still_parses_for_backward_compatibility() -> None:
    """Old invocations (PS-style and --with-mao-agents form) keep working."""
    src = INSTALL_PS1.read_text(encoding="utf-8-sig")
    assert "[switch]$WithMaoAgents" in src, (
        "the parameter declaration must stay so old invocations parse"
    )
    assert "'^--with-mao-agents$'" in src, (
        "the .bat-forwarded --with-mao-agents form must still map to the "
        "switch instead of falling through unmatched"
    )
