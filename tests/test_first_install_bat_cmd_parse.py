# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Live cmd.exe / PowerShell help-contract tests for the Windows entry points
(v0.2.54 G-1 — W-P1-3 regression).

History: CI's "Windows entry-point parser smoke" ran `first-install.bat /help`
expecting usage + a benign exit. The .bat had NO help handler, so `/help`
fell through `%*` into install.ps1 (which had no help handling either) and
started a REAL install on the runner — ~3 minutes of side effects, then a
non-zero exit (CI run 27316431468, red from the day the job landed).

These are LIVE-binary tests per the project's testing discipline (see the
`feedback_argv_shape_tests_miss_live_cli_parser_rejections` lesson): we run
the actual cmd.exe / powershell.exe parser, not string-shape assertions
against the script source. They execute on Windows only; on Linux/macOS CI
the same contract is enforced live by installer-smoke.yml's
`windows-entry-point-smoke` job, and the cmd-step parse hazards are gated
cross-platform by tests/test_workflow_cmd_paren_safety.py.

Speed guard: every invocation here must return in seconds. A timeout means
the help handler regressed into the real install path again — fail loudly.
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BAT = REPO_ROOT / "first-install.bat"
PS1 = REPO_ROOT / "install.ps1"

WINDOWS_ONLY = pytest.mark.skipif(
    platform.system() != "Windows",
    reason="needs the real cmd.exe / powershell.exe parser; covered in CI by "
    "installer-smoke.yml::windows-entry-point-smoke on windows-latest",
)

CMD_PARSER_ERRORS = (
    "is not recognized",
    "system cannot find",
    "syntax of the command",
    "was unexpected at this time",
)


def _run_bat_help(flag: str) -> subprocess.CompletedProcess:
    # /d: skip AutoRun registry commands (deterministic parser environment).
    # `call` mirrors how the CI step and other batch scripts invoke it.
    return subprocess.run(
        ["cmd.exe", "/d", "/c", "call", str(BAT), flag],
        capture_output=True,
        text=True,
        timeout=60,  # help must be instant; a timeout = install path regression
        cwd=REPO_ROOT,
    )


@WINDOWS_ONLY
@pytest.mark.parametrize("flag", ["/help", "--help", "-h", "/?"])
def test_first_install_bat_help_flag(flag):
    """`first-install.bat <help-flag>` must print usage and exit 0 with no
    side effects and no cmd.exe parser errors."""
    proc = _run_bat_help(flag)
    combined = (proc.stdout or "") + (proc.stderr or "")
    for err in CMD_PARSER_ERRORS:
        assert err.lower() not in combined.lower(), (
            f"cmd.exe parser error for {flag!r}: {combined[:2000]}"
        )
    assert proc.returncode == 0, (
        f"{flag!r} exited {proc.returncode}; output: {combined[:2000]}"
    )
    assert "Usage: first-install.bat" in combined, (
        f"{flag!r} printed no usage text; output: {combined[:2000]}"
    )


@WINDOWS_ONLY
def test_install_ps1_help_flag():
    """`install.ps1 --help` (the .bat-forwarded form) must print usage and
    exit 0 before any side effect — previously it started a full install."""
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PS1),
            "--help",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, (
        f"install.ps1 --help exited {proc.returncode}; output: {combined[:2000]}"
    )
    assert "Usage:" in combined, f"no usage text; output: {combined[:2000]}"
    # The real installer banner must NOT appear — its presence means the
    # help gate sits after side-effecting code again.
    assert "Orchestrator Installer ===" not in combined, (
        "install.ps1 --help fell through to the real install path"
    )


# ---------------------------------------------------------------------------
# Cross-platform structural guards (cheap, run everywhere). These do NOT
# replace the live tests above — they catch the two specific ordering
# regressions that would silently defeat the help contract while still
# letting the live smoke pass on a fast machine.
# ---------------------------------------------------------------------------


def test_bat_help_gate_precedes_side_effects():
    """The /help dispatch must appear BEFORE the first side-effecting line
    (the install.ps1 invocation) so help can never trigger an install."""
    text = BAT.read_text(encoding="utf-8", errors="replace")
    help_pos = text.find(':show_help')
    invoke_pos = text.find('-File "%~dp0install.ps1"')
    assert help_pos != -1, "first-install.bat lost its :show_help handler"
    assert invoke_pos != -1, "first-install.bat no longer invokes install.ps1?"
    assert help_pos < invoke_pos, (
        ":show_help must be dispatched before install.ps1 runs"
    )


def test_ps1_help_gate_precedes_side_effects():
    text = PS1.read_text(encoding="utf-8", errors="replace")
    gate_pos = text.find("if ($Help)")
    wsl_pos = text.find("if ($env:WSL_DISTRO_NAME)")
    assert gate_pos != -1, "install.ps1 lost its $Help gate"
    assert wsl_pos != -1
    assert gate_pos < wsl_pos, (
        "$Help gate must run before the WSL guard / any side effect"
    )
