# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression: the Windows shortcut probe must prefer the canonical dist
binary over stale `target\\` build artifacts.

Background (v0.2.62 — Windows update field report):

`scripts/post-install-launcher.ps1` (the desktop-icon step install.py runs on
Windows, including on every GUI "Update orchestrator") writes the Desktop +
Start Menu `VCT Launcher.lnk` shortcuts. It picks the launcher binary via a
first-match-wins probe of a hardcoded candidate list.

Pre-fix the list probed `launcher\\src-tauri\\target\\debug\\vct-launcher-temp.exe`
*before* `launcher\\dist\\windows-x64\\vct-launcher.exe`. A months-old debug
build left in `target\\debug\\` (e.g. v0.2.48 from a prior dev session) therefore
won the probe, and BOTH shortcuts pointed at that stale binary — so the user
opened an old build (old/pixelated icon) while the launcher's own in-app
restart (`restart.rs::resolve_target_binary`) relaunched from the *fresh* dist
binary. Shortcut and app diverged.

The invariant these tests pin:

  * The canonical dist binary (`launcher\\dist\\windows-x64\\vct-launcher.exe`)
    — the exact path `restart.rs` and `_stage_built_binary_into_dist` treat as
    authoritative — must be probed before ANY `target\\` candidate in the
    post-install `.ps1` (no staleness check there; dist is freshly staged).
  * In `first-install.bat` (which keeps a contributor's `target\\release\\`
    build ahead of the bundled dist binary on purpose), the *debug* candidate
    is demoted below dist: a debug build must never outrank the dist binary.

These are static-source assertions because PowerShell / cmd can't run on the
Linux CI runner. If the candidate blocks are renamed or moved, the regex anchors
fail loudly rather than passing silently.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PS1 = REPO_ROOT / "scripts" / "post-install-launcher.ps1"
BAT = REPO_ROOT / "first-install.bat"

# Canonical / problematic path fragments (backslash form, as written in the
# Windows scripts). Matched as plain substrings on each source line.
DIST = r"launcher\dist\windows-x64\vct-launcher.exe"
DEBUG_TEMP = r"launcher\src-tauri\target\debug\vct-launcher-temp.exe"
TARGET_PREFIX = r"launcher\src-tauri\target\\"


def _first_index(lines: list[str], needle: str) -> int:
    for i, line in enumerate(lines):
        if needle in line:
            return i
    raise AssertionError(f"probe candidate not found in script: {needle!r}")


def _ps1_candidate_block(text: str) -> list[str]:
    """Return the lines of the `$candidates = @( ... )` array, in order.

    Line-based (not a single regex): each candidate is a `(Join-Path ...)`
    expression whose own `)` would terminate a non-greedy match prematurely,
    and the surrounding comments mention the same path fragments — so we scope
    strictly to the lines between the `@(` opener and its closing `)`.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.search(r"\$candidates\s*=\s*@\(", line):
            start = i + 1
            break
    assert start is not None, (
        "could not locate the $candidates = @( ... ) block in post-install-launcher.ps1"
    )
    block: list[str] = []
    for line in lines[start:]:
        if line.strip() == ")":
            return block
        block.append(line)
    raise AssertionError("unterminated $candidates = @( ... ) block")


@pytest.mark.skipif(not PS1.exists(), reason="post-install-launcher.ps1 missing")
def test_ps1_probes_dist_before_any_target() -> None:
    block = _ps1_candidate_block(PS1.read_text(encoding="utf-8"))

    dist_idx = _first_index(block, DIST)
    debug_idx = _first_index(block, DEBUG_TEMP)
    assert dist_idx < debug_idx, (
        "post-install-launcher.ps1: dist binary must be probed BEFORE "
        "target\\debug\\vct-launcher-temp.exe (a stale debug build must not "
        f"capture the shortcut). dist@{dist_idx} debug@{debug_idx}"
    )

    # Stronger: dist must precede EVERY target\ candidate on the update path,
    # so the .lnk always matches restart.rs's dist binary.
    target_indices = [
        i for i, line in enumerate(block) if TARGET_PREFIX.rstrip("\\") in line
    ]
    assert target_indices, "expected at least one target\\ fallback candidate"
    assert dist_idx < min(target_indices), (
        "post-install-launcher.ps1: dist binary must precede ALL target\\ "
        f"candidates. dist@{dist_idx} first-target@{min(target_indices)}"
    )


@pytest.mark.skipif(not BAT.exists(), reason="first-install.bat missing")
def test_bat_demotes_debug_temp_below_dist() -> None:
    lines = BAT.read_text(encoding="utf-8", errors="replace").splitlines()

    dist_idx = _first_index(lines, DIST)
    debug_idx = _first_index(lines, DEBUG_TEMP)
    assert dist_idx < debug_idx, (
        "first-install.bat: the target\\debug\\vct-launcher-temp.exe candidate "
        "must be probed AFTER launcher\\dist\\windows-x64\\vct-launcher.exe "
        "(a stale debug build must not outrank the bundled dist binary). "
        f"dist@{dist_idx} debug@{debug_idx}"
    )
