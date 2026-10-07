# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 S16-6 + M2 — launcher-binary selection rule, both siblings.

The ONE rule (stated identically in the headers of
``scripts/post-install-launcher.sh`` Step 2 and
``scripts/post-install-launcher.ps1``):

1. the bundled dist binary wins when it exists and passes the freshness
   check (the .ps1 relies on its callers having run that check);
2. a locally built ``launcher/src-tauri/target/...`` binary wins ONLY when
   it is newer than the dist binary (mtime), or when no fresh dist binary
   exists;
3. the chosen candidate and WHY are logged on one line.

This module covers the surfaces the standalone bash test cannot:

* it runs ``tests/test_v02102_shortcut_discovery.sh`` so the rule's bash
  arms are part of the pytest suite, and
* it exercises the .ps1 ``Select-LauncherBinary`` directly under pwsh
  (skipped when pwsh is absent), including the arm that is RED against
  the pre-fix first-match-wins probe: a genuinely NEWER local build must
  beat an existing dist binary.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SH_TEST = REPO_ROOT / "tests" / "test_v02102_shortcut_discovery.sh"
PS1 = REPO_ROOT / "scripts" / "post-install-launcher.ps1"


def test_bash_selection_rule_suite_passes():
    """Run the standalone bash arms (dist-first, mtime, macOS, stale-dist)."""
    res = subprocess.run(
        ["bash", str(SH_TEST)], capture_output=True, text=True, timeout=120
    )
    assert res.returncode == 0, (
        f"bash selection-rule suite failed:\n{res.stdout}\n{res.stderr}"
    )
    assert "0 failed" in res.stdout, res.stdout


def _extract_ps1_function(name: str) -> str:
    """Extract `function <name> { ... }` (closing brace in column 0)."""
    lines = PS1.read_text(encoding="utf-8").splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"function {name} {{"):
            start = i
            break
    assert start is not None, f"function {name} not found in {PS1}"
    end = None
    for j in range(start + 1, len(lines)):
        if lines[j] == "}":
            end = j
            break
    assert end is not None, f"closing brace for {name} not found"
    return "\n".join(lines[start : end + 1])


# Arms: (arm-id, want_reason). mtimes are set relative to "now" inside the
# test so the comparison never depends on the wall clock; the local binary
# must be strictly newer to win.
PWSH_ARMS = [
    # dist newer than local -> dist wins (pre-fix behaviour, must stay).
    ("dist", "bundled dist binary"),
    # local strictly newer than dist -> local wins. RED against the
    # pre-fix .ps1 (first-match-wins always preferred dist).
    ("local", "local build is newer than the dist binary"),
    # no dist at all -> local is the repo's only artifact.
    ("local-only", "no fresh bundled dist binary available"),
]


@pytest.mark.parametrize(
    "arm,want_reason",
    PWSH_ARMS,
    ids=[a[0] for a in PWSH_ARMS],
)
def test_ps1_selection_rule(arm: str, want_reason: str, tmp_path: Path):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh not available — .ps1 selection rule untested here")

    repo = tmp_path / "repo"
    dist = repo / "launcher" / "dist" / "windows-x64" / "vct-launcher.exe"
    local = repo / "launcher" / "src-tauri" / "target" / "release" / "vct-launcher.exe"

    def plant(path: Path, offset: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"MZ")
        t = path.stat().st_mtime + offset
        os.utime(path, (t, t))

    if arm == "dist":
        plant(dist, 0)          # dist: now
        plant(local, -3600)     # local: an hour older -> dist must win
    elif arm == "local":
        plant(dist, -3600)      # dist: an hour old
        plant(local, 0)         # local: just built -> local must win
    else:  # local-only
        plant(local, 0)

    fn = _extract_ps1_function("Select-LauncherBinary")
    script = textwrap.dedent(
        f"""\
        {fn}
        $chosen = Select-LauncherBinary -DistBin '{dist}' `
            -SystemBins @() `
            -LocalBins @('{local}')
        Write-Output "CHOSEN=$chosen"
        """
    )
    res = subprocess.run(
        [pwsh, "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, f"pwsh failed: {res.stderr}"
    out = res.stdout
    chosen_line = next(ln for ln in out.splitlines() if ln.startswith("CHOSEN="))
    reason_line = next(ln for ln in out.splitlines() if "Selected" in ln)

    want_path = dist if arm == "dist" else local
    assert chosen_line == f"CHOSEN={want_path}", out
    assert want_reason in reason_line, out


def test_ps1_still_parses():
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh not available")
    res = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-Command",
            "$t=$null;$e=$null;"
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{PS1}',"
            " [ref]$t, [ref]$e); if ($e.Count) { $e | ForEach-Object { $_.Message }; exit 1 }",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, f"ps1 parse errors: {res.stdout}{res.stderr}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
