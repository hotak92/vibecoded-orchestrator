# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Timestamp files shared between a ``.sh`` hook and its ``.ps1`` sibling.

v0.2.92 R2, found while red-proofing the diagram-throttle branch.

Two VCO throttles persist a "unix seconds" stamp to a file that BOTH siblings
read and write:

* ``.claude/state/diagram_idx_<hash>.ts`` — ``post-file-edit.{sh,ps1}``
* ``<debounce dir>/.last_reap.ts``        — ``_lib/kg-sync-debounce.{sh,ps1}``

The ``.sh`` sides write ``date +%s``.  The ``.ps1`` sides computed the value by
subtracting DateTime instances of MIXED ``DateTimeKind`` — and .NET subtracts
raw ticks while ignoring Kind, so the expression never errors, it just returns
a number shifted by the machine's UTC offset:

    (Get-Date) - (Get-Date "1970-01-01Z")                    →  +1 offset
    (Get-Date) - (Get-Date "1970-01-01Z").ToUniversalTime()  →  +2 offsets
    $now.ToUniversalTime() - [datetime]'1970-01-01T00:00:00Z' →  -1 offset

Self-consistent within one sibling, so a Windows-only machine never noticed.
Across siblings (WSL, or a project driven from both) the comparison is off by
hours in whichever direction: east of UTC the debounce reaper read a ``.sh``
stamp as a NEGATIVE age and returned early every time — so it never ran and
orphaned lock dirs accumulated — while the diagram throttle read one as
``+offset`` and re-indexed on every single edit.

The assertion is against ``time.time()`` in this process, i.e. the same clock
``date +%s`` reads, so it is a real cross-sibling agreement check rather than
a restatement of whatever the PowerShell happens to compute.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "templates" / "hooks"

#: A wrong idiom is off by a whole UTC offset (>= 1800 s for every zone that
#: has one), so a few seconds of tolerance cannot mask one.
TOLERANCE_S = 120


def _pwsh() -> str:
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if exe is None:
        assert not os.environ.get("CI"), (
            "PowerShell is absent in CI — this parity gate cannot run and "
            "must not be reported as passing."
        )
        pytest.skip("no PowerShell interpreter on this machine")
    return exe


def _eval_ps(exe: str, expression: str, tmp_path: Path) -> int:
    script = tmp_path / "epoch.ps1"
    script.write_text(f"Write-Output ({expression})\n", encoding="utf-8")
    out = subprocess.run(
        [exe, *PWSH_BASE_FLAGS, "-File", str(script)],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return int(out.stdout.strip())


def _extract_assignment(path: Path, variable: str) -> str:
    """The right-hand side of ``$<variable> = ...`` in a hook, verbatim.

    Evaluating the SHIPPED expression (rather than a copy of it pasted into
    this file) is what makes this a test of production rather than of the
    test's own arithmetic.
    """
    body = path.read_text(encoding="utf-8-sig")
    match = re.search(rf"^\s*\${variable}\s*=\s*(.+)$", body, re.MULTILINE)
    assert match, f"no assignment to ${variable} found in {path.name}"
    return match.group(1).strip()


@pytest.mark.parametrize(
    ("hook", "variable"),
    [
        # v0.2.95 (lane F10): the diagram-throttle stamp moved with the
        # rest of the routing into the ONE home both write hooks call.
        # v0.2.102: the route-touched-path `nowTs` stamp was removed with
        # the diagrams integrations (the diagram throttle is gone); the
        # debounce's `nowEpoch` reap stamp still pins the Unix-epoch
        # discipline this file exists for.
        ("_lib/kg-sync-debounce.ps1", "nowEpoch"),
        # v0.2.92 final sweep: the three telemetry hooks write MILLISECONDS
        # ($*TsMs) into the same JSONL their .sh siblings write
        # ``time.time()*1000`` into — cross-sibling end-start was off by
        # ~7.2M ms (2x UTC offset) east of UTC. Same fix, ms scale.
        ("pre-bash-context-inject.ps1", "StartTsMs"),
        ("post-bash-context-record.ps1", "EndTsMs"),
        ("post-edit-outcome.ps1", "NowTsMs"),
    ],
)
def test_the_shipped_expression_yields_true_unix_seconds(hook, variable, tmp_path):
    """Evaluate the hook's OWN expression and compare against this clock.

    ``date +%s`` (what the .sh sibling writes into the same file) and
    ``time.time()`` read the same clock, so agreement here is agreement with
    the sibling. ``*TsMs`` variables are millisecond stamps and are compared
    at millisecond tolerance.
    """
    exe = _pwsh()
    expression = _extract_assignment(HOOKS / hook, variable)
    is_ms = variable.endswith("TsMs")
    ours = int(time.time() * (1000 if is_ms else 1))
    theirs = _eval_ps(exe, expression, tmp_path)
    drift = theirs - ours
    tolerance = TOLERANCE_S * (1000 if is_ms else 1)
    unit = "ms" if is_ms else "s"
    advice = (
        "[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()"
        if is_ms
        else "[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()"
    )
    assert abs(drift) <= tolerance, (
        f"{hook}'s ${variable} is {drift}{unit} away from unix epoch "
        f"({theirs} vs {ours}). The .sh sibling writes the same stamp into "
        "the SAME telemetry JSONL, so this shift silently corrupts "
        f"cross-sibling durations. Use {advice}."
    )


def _pwsh_version(exe: str, tmp_path: Path) -> str:
    """The interpreter's own version string (``$PSVersionTable``), or ""."""
    script = tmp_path / "version.ps1"
    script.write_text("Write-Output $PSVersionTable.PSVersion.ToString()\n",
                      encoding="utf-8")
    out = subprocess.run(
        [exe, *PWSH_BASE_FLAGS, "-File", str(script)],
        capture_output=True, text=True, timeout=60,
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def _known_to_drift(version: str) -> bool:
    """Is the mixed-Kind epoch idiom KNOWN to drift on this interpreter?

    Windows PowerShell 5.1 (major < 6) is the version family where the drift
    was observed in the field (v0.2.92 R2 — the reason this file exists):
    there ``Get-Date "1970-01-01Z"`` keeps a UTC Kind while ``Get-Date`` is
    Local, and .NET subtracts raw ticks ignoring Kind, so the stamp shifts
    by the UTC offset. pwsh 7.4.6 was MEASURED (2026-10-30, CET host) to
    parse the same literal as Local kind — the two Kinds agree and the idiom
    no longer drifts. For any other version the measurement in the test
    below decides; an unparseable version string is treated as unknown.
    """
    major = version.split(".", 1)[0]
    return major.isdigit() and int(major) < 6


def test_the_broken_idioms_really_are_broken_on_this_host(tmp_path):
    """The premise, measured — otherwise the test above proves nothing here.

    Three honest outcomes (v0.2.105 fix round 2 — pre-fix this asserted the
    drift unconditionally and failed on pwsh 7.4.6, which no longer drifts):

    * the idiom DRIFTS on this interpreter → the gate above has teeth (pass);
    * NO drift on a version known to drift (Windows PowerShell 5.1) → fail:
      either this probe broke or the known-drift table is wrong, and the
      gate above must not be trusted until that is understood;
    * NO drift on any other version → skip loudly naming the version: this
      interpreter parses ``Get-Date "1970-01-01Z"`` as Local kind, so the
      mixed-Kind idiom is simply not broken here and the gate above cannot
      distinguish it. Nothing shipped depends on the premise — the hooks
      use ``[DateTimeOffset]::UtcNow``, which is correct on every version.

    On a UTC machine every idiom agrees and nothing is observable — skip
    loudly there too rather than reporting a pass that measured nothing.
    """
    exe = _pwsh()
    if abs(time.timezone) < 1800 and abs(time.altzone) < 1800:
        pytest.skip("this host is at UTC — the offset bug is not observable")
    version = _pwsh_version(exe, tmp_path)
    named = version or "(unknown version)"
    ours = int(time.time())
    broken = _eval_ps(
        exe,
        '[int][double]::Parse(((Get-Date) - (Get-Date "1970-01-01Z")).TotalSeconds)',
        tmp_path,
    )
    if abs(broken - ours) > TOLERANCE_S:
        return  # the premise is confirmed on this interpreter — the gate has teeth
    if _known_to_drift(version):
        pytest.fail(
            f"PowerShell {named} is KNOWN to drift on the mixed-Kind epoch idiom, "
            f"but this run measured {broken - ours}s (within {TOLERANCE_S}s of unix "
            "epoch) — either this premise probe is broken or the known-drift table "
            "in _known_to_drift is wrong. The shipped-expression gate above proves "
            "nothing on this host until that is understood."
        )
    pytest.skip(
        f"PowerShell {named} parses Get-Date \"1970-01-01Z\" as Local kind — the "
        "mixed-Kind epoch idiom measures NO drift on this version (pwsh 7.4.6 "
        "confirmed 2026-10-30), so the gate above cannot distinguish it from the "
        "correct one HERE. This is a property of the interpreter, not a defect: "
        "the shipped hooks use [DateTimeOffset]::UtcNow, which is correct on "
        "every version, and this gate keeps its teeth on any host whose "
        "PowerShell does drift (e.g. Windows PowerShell 5.1)."
    )
