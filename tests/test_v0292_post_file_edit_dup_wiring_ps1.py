# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""post-file-edit.ps1 → kg-duplicates.ps1 wiring (v0.2.92 MAJOR-1 / R42).

``templates/scripts/kg-duplicates.ps1`` was written and tested standalone
earlier in this cycle, but the production caller on Windows still gated the
every-10-edits duplicate scan on ``Get-Command bash`` and carried a comment
asserting the sibling "has never shipped".  A written-but-uncalled ``.ps1``
narrows the feature exactly as effectively as never writing one, which is what
R42 forbids.

These tests are DRIVEN, not scanned.  A guard that asserts the string
``kg-duplicates.ps1`` appears in the hook source is satisfiable by the very
comment that was wrong — this cycle shipped two such guards on unwired code.
So each test here runs the real hook through its production entry point (JSON
on stdin, ``CLAUDE_PROJECT_DIR`` at a staged project) and asserts the
OBSERVABLE consequence: the duplicate-scan report file appears, holding the
output of the wrapper the hook chose.

The staged wrapper deliberately sleeps before it prints.  The real scan is a
whole-collection Weaviate query that always outlives the hook, and the previous
implementation launched it with ``Start-Job`` — whose child process is torn
down when the host exits, so the report was never written even on machines
that DID have bash.  A wrapper that returns instantly would let that bug pass.

Interpreter-bound: skipped when no PowerShell is on PATH, refusing to skip
under CI (the posture ``tests/test_v0292_kg_duplicates_ps1.py`` established).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK_PS1 = REPO / "templates" / "hooks" / "post-file-edit.ps1"

#: The staged wrapper waits this long before printing, so a scan that outlives
#: the hook is the case under test (not an artificially instant one).
WRAPPER_DELAY_MS = 1500

#: Poll budget for the detached child to land its report.
REPORT_TIMEOUT_S = 45.0


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _require_pwsh() -> str:
    exe = _powershell()
    if exe is None:
        assert not os.environ.get("CI"), (
            "PowerShell is absent in CI — this wiring gate cannot run and "
            "must not be reported as passing."
        )
        pytest.skip("no PowerShell interpreter on this machine")
    return exe


def _stage_project(tmp_path: Path) -> Path:
    """A minimal project the hook will treat as a KG edit on the 10th count."""
    project = tmp_path / "proj"
    (project / ".claude" / "scripts").mkdir(parents=True)
    (project / ".claude" / "logs").mkdir(parents=True)
    (project / ".claude" / "state").mkdir(parents=True)
    (project / "knowledge").mkdir(parents=True)
    (project / "knowledge" / "node.md").write_text("# node\n", encoding="utf-8")
    # The NINTH edit has happened; this hook fire is the tenth.
    (project / ".claude" / "logs" / ".kg_edit_count").write_text("9", encoding="utf-8")
    return project


def _stage_ps1_wrapper(project: Path, marker: Path) -> None:
    """A stand-in for ``.claude/scripts/kg-duplicates.ps1``.

    It records that it ran (so a report written by anything else cannot pass
    the test) and prints one candidate line in the ✅/⚠️/📊 vocabulary the
    hook filters, plus the ❌ line detect_duplicates.py emits when the scan
    itself fails — the line the ⚠️ "See the error above." verdict points at.
    """
    body = (
        f"Start-Sleep -Milliseconds {WRAPPER_DELAY_MS}\n"
        f"Set-Content -LiteralPath '{marker}' -Value ($args -join ' ') -Encoding utf8\n"
        "Write-Output '⚠️ possible dup: PS1-WRAPPER-RAN ~ other (0.97)'\n"
        "Write-Output '❌ Error during duplicate detection: STAGED-FAILURE'\n"
    )
    (project / ".claude" / "scripts" / "kg-duplicates.ps1").write_text(
        body, encoding="utf-8"
    )


def _stage_bash_wrapper(project: Path, marker: Path) -> None:
    """A stand-in for the POSIX ``.claude/scripts/kg-duplicates`` wrapper."""
    wrapper = project / ".claude" / "scripts" / "kg-duplicates"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"sleep {WRAPPER_DELAY_MS / 1000:.1f}\n"
        f'printf "%s" "$*" > "{marker}"\n'
        "printf '\\342\\232\\240\\357\\270\\217 possible dup: "
        "BASH-WRAPPER-RAN ~ other (0.97)\\n'\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)


def _run_hook(exe: str, project: Path, edited: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("VCT_VENV", None)
    payload = {"tool_input": {"file_path": str(edited)}}
    return subprocess.run(
        [exe, "-NoProfile", "-File", str(HOOK_PS1)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _await_report(project: Path) -> str:
    report = project / ".claude" / "state" / "kg_duplicates_report.txt"
    deadline = time.monotonic() + REPORT_TIMEOUT_S
    while time.monotonic() < deadline:
        if report.is_file():
            body = report.read_text(encoding="utf-8", errors="replace")
            if body.strip():
                return body
        time.sleep(0.2)
    return ""


def test_ps1_wrapper_is_the_first_branch_and_its_scan_outlives_the_hook(tmp_path):
    """Wiring proof: with ONLY the .ps1 wrapper staged, the report appears.

    No bash ``kg-duplicates`` exists in the staged project, so the fallback
    branch has nothing to run — a report can only come from the .ps1 branch.
    The wrapper sleeps past the hook's own exit, so this also pins the
    detached spawn (Start-Job would be reaped and write nothing).
    """
    exe = _require_pwsh()
    project = _stage_project(tmp_path)
    marker = tmp_path / "ps1-ran.txt"
    _stage_ps1_wrapper(project, marker)

    result = _run_hook(exe, project, project / "knowledge" / "node.md")
    assert result.returncode == 0, result.stderr

    body = _await_report(project)
    assert "PS1-WRAPPER-RAN" in body, (
        "the every-10-edits duplicate scan never reached kg-duplicates.ps1.\n"
        f"report={body!r}\nhook stdout={result.stdout!r}\n"
        f"hook stderr={result.stderr!r}"
    )
    assert "KG duplicate scan" in body, f"report header missing: {body!r}"
    assert "❌ Error during duplicate detection" in body, (
        "the report filter dropped the scan's error line — the ⚠️ 'See the "
        "error above.' verdict would point at a line the report does not "
        f"carry. report={body!r}"
    )
    assert marker.is_file(), "the wrapper itself did not run"
    assert "--threshold" in marker.read_text(encoding="utf-8"), (
        "the wrapper was invoked without the threshold argument the bash "
        "sibling passes"
    )


def test_bash_wrapper_remains_the_fallback_when_no_ps1_is_installed(tmp_path):
    """R42 is about ADDING a path, never removing one.

    A project whose bundle predates kg-duplicates.ps1 still has the POSIX
    wrapper; on a machine with bash it must keep working exactly as before.
    """
    exe = _require_pwsh()
    if shutil.which("bash") is None:
        pytest.skip("no bash on this machine — fallback leg not exercisable")
    project = _stage_project(tmp_path)
    marker = tmp_path / "bash-ran.txt"
    _stage_bash_wrapper(project, marker)

    result = _run_hook(exe, project, project / "knowledge" / "node.md")
    assert result.returncode == 0, result.stderr

    body = _await_report(project)
    assert "BASH-WRAPPER-RAN" in body, (
        "the bash fallback stopped working when the .ps1 branch was added.\n"
        f"report={body!r}\nhook stderr={result.stderr!r}"
    )


def test_no_scan_is_launched_on_a_non_multiple_of_ten(tmp_path):
    """The decision, not just the act: the ninth edit must NOT scan.

    Without this, a mutation that drops the ``% 10`` gate and scans on every
    edit would leave the wiring tests above green while turning a periodic
    Weaviate sweep into a per-keystroke one.
    """
    exe = _require_pwsh()
    project = _stage_project(tmp_path)
    (project / ".claude" / "logs" / ".kg_edit_count").write_text("3", encoding="utf-8")
    marker = tmp_path / "ps1-ran.txt"
    _stage_ps1_wrapper(project, marker)

    result = _run_hook(exe, project, project / "knowledge" / "node.md")
    assert result.returncode == 0, result.stderr
    # Give a spawned child the same budget it would need to write.
    time.sleep(WRAPPER_DELAY_MS / 1000 + 1.5)
    assert not marker.is_file(), "the duplicate scan ran on the 4th edit"


def test_the_wrapper_the_hook_calls_is_one_the_bundle_installs():
    """The delivery layer, not just the caller.

    The hook branches on ``.claude/scripts/kg-duplicates.ps1``. If the bundle
    never copied that file into a project, the branch would be as dead as the
    bash gate it replaced — a caller wired to a file that never arrives is the
    same "credited mechanism that never fires" one layer down. Asserted
    through the shipping code's OWN enumerator, so a change to the glob set
    breaks this test rather than silently un-shipping the wrapper.
    """
    from vco_lib.bundle_globs import script_patterns

    src = REPO / "templates" / "scripts"
    shipped = {
        f.name
        for pat in script_patterns()
        for f in src.glob(pat)
        if f.is_file()
    }
    assert "kg-duplicates.ps1" in shipped, (
        "post-file-edit.ps1 calls .claude/scripts/kg-duplicates.ps1 but the "
        "bundle's script patterns do not ship it"
    )
    assert "kg-duplicates" in shipped, "the POSIX fallback wrapper is not shipped"


def test_the_stale_never_shipped_comment_is_gone(tmp_path):
    """The comment asserted a false reason for a dead branch.

    Kept deliberately narrow — this is prose hygiene, NOT the wiring guard.
    The wiring guard is the driven test above; a source scan could never tell
    the two apart, which is why this one asserts only the ABSENCE of a claim.
    """
    body = HOOK_PS1.read_text(encoding="utf-8")
    assert "has never shipped" not in body, (
        "post-file-edit.ps1 still claims kg-duplicates.ps1 has never shipped; "
        "the file exists at templates/scripts/kg-duplicates.ps1"
    )
