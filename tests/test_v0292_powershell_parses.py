# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Every shipped `.ps1` must PARSE. There was no repo-wide gate before v0.2.92.

Two shipped hooks had never parsed, so they were dead on every Windows install,
silently:

* ``code-graph-incremental.ps1`` — ``param()`` sat below the env-scrub and two
  dot-sources. PowerShell requires a script's ``param()`` to be its FIRST
  statement; with executable code above it, ``param`` parses as a command call
  and the file fails wholesale.
* ``context-size-check.ps1`` — ``$Label:`` inside an expandable here-string.
  PowerShell reads ``$Name:`` as a SCOPE qualifier (as in ``$env:PATH``), so the
  trailing colon of an English sentence broke the parse.

Neither is caught by sibling-parity checks: parity proves a `.ps1` EXISTS and
moved when its `.sh` moved, never that it RUNS. Presence is not function — the
same lesson as the four hooks that were registered-but-never-firing for three
releases.

Scope note: ``tests/test_v52_l1_subagent_stop_reconciler.py`` parse-checks two
specific `_lib` files as part of its own subject. This gate is the repo-wide
one; that narrow check is left alone rather than folded in, because it belongs
to that module's concern and costs nothing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# v0.2.92 delivery audit m6: the gate scanned templates/** only — the
# maintainer-side entry points (scripts/*.ps1, install.ps1) were parsed by
# NO gate (only installer-smoke.yml parses install.ps1). All parse today;
# now a regression in any of them reds here instead of in the field.
SHIPPED_PS1 = sorted(
    {
        *(
            p
            for p in (REPO_ROOT / "templates").rglob("*.ps1")
            if p.is_file()
        ),
        *(p for p in (REPO_ROOT / "scripts").glob("*.ps1") if p.is_file()),
        *(
            [REPO_ROOT / "install.ps1"]
            if (REPO_ROOT / "install.ps1").is_file()
            else []
        ),
    }
)


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def test_every_shipped_ps1_parses(tmp_path: Path) -> None:
    """No shipped PowerShell file may contain a syntax error.

    The probe runs from a real script file invoked with ``-File``. Passing a
    script via ``-Command`` and hoping ``$args`` is populated does NOT work —
    that mistake makes the harness itself fail and reads exactly like a file
    that would not parse.
    """
    assert SHIPPED_PS1, "found no shipped .ps1 files — the glob is wrong"

    exe = _powershell()
    if exe is None:
        # Never let this pass silently where a parser IS available. A test that
        # is skipped in CI passes when it is switched off, which is precisely
        # how the two dead hooks survived for so long.
        assert not os.environ.get("CI"), (
            "PowerShell is absent in CI — this gate cannot run and must not be "
            "reported as passing. Install pwsh on the runner, or move this gate "
            "to a job that already has it."
        )
        pytest.skip("no PowerShell interpreter on this machine")

    probe = tmp_path / "parse_probe.ps1"
    probe.write_text(
        textwrap.dedent(
            """
            $bad = 0
            foreach ($p in $args) {
                $tokens = $null; $errors = $null
                [System.Management.Automation.Language.Parser]::ParseFile(
                    $p, [ref]$tokens, [ref]$errors) | Out-Null
                if ($errors -and $errors.Count -gt 0) {
                    $bad++
                    Write-Output "$p :: line $($errors[0].Extent.StartLineNumber) :: $($errors[0].Message)"
                }
            }
            exit $bad
            """
        ).strip(),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [exe, "-NoProfile", "-File", str(probe), *[str(p) for p in SHIPPED_PS1]],
        capture_output=True,
        text=True,
        timeout=600,
    )
    # Distinguish "the probe could not run" from "N files are broken". The
    # probe prints exactly ONE `<path> :: line <n> :: <msg>` record per broken
    # file and exits with that same count, so the two must AGREE — that
    # agreement is the check.
    #
    # The earlier guard was `returncode <= len(SHIPPED_PS1)`, which cannot
    # fail for the case it names: a crashed pwsh exits 1 (or 2, or 127) with
    # NO records on stdout, and every one of those is a legal file count. The
    # run still went red, but under the wrong headline — "1 shipped .ps1
    # file(s) do not parse" with an empty file list — which is the
    # could-not-check/absent conflation this release exists to remove, one
    # level up. A check that cannot fail is not a check.
    records = [ln for ln in proc.stdout.splitlines() if " :: line " in ln]
    assert proc.returncode == len(records), (
        "the parse probe itself failed to run — this is NOT a file-count "
        f"(exit={proc.returncode} but {len(records)} parse-error record(s) on "
        f"stdout):\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert proc.returncode == 0, (
        f"{proc.returncode} shipped .ps1 file(s) do not parse — they are DEAD on "
        f"Windows:\n{proc.stdout.strip()}\n{proc.stderr.strip()}"
    )
