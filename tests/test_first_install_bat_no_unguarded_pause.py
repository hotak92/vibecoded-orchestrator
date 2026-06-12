# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Static lint: every `pause` in first-install.bat must be interactivity-gated
(v0.2.54 Track W).

Why this exists: `pause` blocks forever waiting for a keypress. That is the
desired UX for an Explorer double-click (keep the cmd window open so the user
can read the output), but on unattended runners — CI's Install Smoke (tri-OS)
Windows leg invokes `first-install.bat --yes ...` — an unconditional `pause`
hangs the job until the runner-level timeout kills it. first-install.bat
sniffs `--yes` / `--non-interactive` / `--quiet` into `YES_FLAG` BEFORE any
pause-able path, so every `pause` must be written as:

    if "%YES_FLAG%"=="0" pause

This lint statically rejects any `pause` not gated that way on the SAME line.
cmd.exe cannot run on the Linux/macOS CI runners that execute pytest, so a
static source scan is the local gate; the live counterpart is the tri-OS
install smoke itself on windows-latest.

Exception conditions (when a bare `pause` would be acceptable and this test
should be amended rather than the bat):
  - a `pause` on a code path that provably runs before argument sniffing AND
    is reachable only from an interactive context (none exist today — the
    sniff loop was deliberately moved above the first pause-able path, the
    install.ps1 sanity check, in v0.2.54 Track W);
  - a `pause` gated by a DIFFERENT interactivity variable — add that variable
    to GATE_RE below with a comment explaining the new gate.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BAT = REPO_ROOT / "first-install.bat"
# v0.2.54 Track G (G-2): uninstall.bat ships the same YES_FLAG pause
# discipline — scan it with the same lint. Add future .bat entry points here.
ALL_BATS = (BAT, REPO_ROOT / "uninstall.bat")

# `pause` as a standalone word, case-insensitive (cmd.exe keywords are
# case-insensitive). Negative lookarounds keep tokens like "paused" or
# paths containing "pause" out of scope.
PAUSE_TOKEN_RE = re.compile(r"(?i)(?<![\w./\\-])pause(?![\w./\\-])")

# The approved same-line interactivity gate. Whitespace-tolerant; the
# comparison string must be exactly "0" (YES_FLAG defaults to 0 and is
# flipped to 1 by --yes / --non-interactive / --quiet).
GATE_RE = re.compile(r"(?i)if\s+\"%YES_FLAG%\"\s*==\s*\"0\"\s+(?:@\s*)?pause\b")


def _iter_pause_lines(bat: Path = BAT) -> list[tuple[int, str]]:
    """Yield (lineno, stripped_line) for every executable line containing a
    `pause` token. REM / `::` comments and echo text are not executable
    pause sites and are skipped."""
    out: list[tuple[int, str]] = []
    for lineno, raw in enumerate(
        bat.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = raw.strip()
        low = stripped.lower()
        if low.startswith(("rem", "::", "@rem")):
            continue  # comment — `pause` here is documentation, not a command
        if low.startswith(("echo", "@echo")):
            continue  # echoed text mentioning pause is not a command
        if PAUSE_TOKEN_RE.search(stripped):
            out.append((lineno, stripped))
    return out


def test_bats_exist() -> None:
    for bat in ALL_BATS:
        assert bat.is_file(), f"missing {bat}"


def test_every_pause_is_gated_on_yes_flag() -> None:
    for bat in ALL_BATS:
        offenders = [
            (lineno, line)
            for lineno, line in _iter_pause_lines(bat)
            if not GATE_RE.search(line)
        ]
        assert not offenders, (
            f"{bat.name} contains unguarded `pause` statement(s) — these "
            "hang unattended runs (CI's --yes invocation) forever. Gate each "
            'one as `if "%YES_FLAG%"=="0" pause`:\n'
            + "\n".join(f"  line {n}: {line}" for n, line in offenders)
        )


def test_scanner_finds_the_gated_pauses() -> None:
    """Self-check: if the scanner ever stops seeing the known gated pauses
    (e.g. a refactor renames the file or rewrites the pause sites), the
    unguarded-pause test above would pass vacuously. Pin the expectation
    that at least the known pause sites per .bat are still found and gated:
    first-install.bat has three (broken-clone sanity check, install-failed
    path, end-of-script keep-window-open); uninstall.bat has three (sanity
    check, python-missing, end-of-script keep-window-open)."""
    expected_min = {"first-install.bat": 3, "uninstall.bat": 3}
    for bat in ALL_BATS:
        gated = [
            (lineno, line)
            for lineno, line in _iter_pause_lines(bat)
            if GATE_RE.search(line)
        ]
        floor = expected_min[bat.name]
        assert len(gated) >= floor, (
            f"expected >= {floor} gated pause sites in {bat.name}, found "
            f"{len(gated)}: {gated!r} — if pause sites were legitimately "
            "removed, update this expectation."
        )
