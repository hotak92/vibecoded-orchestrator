# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression: stderr-cap.sh must NOT swallow non-zero exit codes.

Background (2026-05-08)
-----------------------
A PreToolUse audit subagent reported that hooks sourcing
``_lib/stderr-cap.sh`` were exiting 0 instead of the intended 2 when
they emitted a BLOCK message and called ``exit 2``. The hypothesis was
that the cap library's ``exec 2> >(...)`` process substitution either:

  1. installed a ``trap … EXIT`` handler that ran cleanup and overrode
     the script's exit code, or
  2. propagated the receiving subshell's exit (``cat >/dev/null`` →
     always 0) back to the parent script, or
  3. interacted with SIGPIPE on the writer side after ``head -c`` closed
     its end, returning a non-2 exit.

Empirical investigation (bash 5.2.21, Linux 6.17) found the bug
**not reproducible**: the cap library has no trap, ``exec 2>>(…)``
does not couple the script's ``$?`` to the procsub child's exit, and
``head -c`` closing early does not raise SIGPIPE on the writer because
the trailing ``cat >/dev/null`` drains the pipe.

This test exists to **pin the invariant structurally**: any future edit
to ``stderr-cap.sh`` (e.g. adding a trap for cleanup, switching the
drain pattern, or routing through ``tee``) that *would* swallow
non-zero exits gets caught in CI before it ships. The bug the audit
described was theoretical; this test makes sure it stays theoretical.

The test exercises the cap library directly AND the three production
PreToolUse guards (kill, vercel-token, smtp-debug) that depend on
``exit 2`` reaching Claude Code to actually block a tool.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CAP_LIB = REPO_ROOT / ".claude" / "hooks" / "_lib" / "stderr-cap.sh"
TEMPLATE_CAP_LIB = REPO_ROOT / "templates" / "hooks" / "_lib" / "stderr-cap.sh"
HOOKS_DIR = REPO_ROOT / ".claude" / "hooks"


# ---------------------------------------------------------------------------
# Library-level matrix: source the cap, then trigger each plausible
# exit-swallow scenario. Every row must report the script's intended
# exit code unchanged.
# ---------------------------------------------------------------------------


def _run_bash(snippet: str, *, env_overrides: dict[str, str] | None = None) -> int:
    """Run a bash one-liner that sources the cap lib and returns its exit."""
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    # We discard the captured stderr deliberately — the cap is supposed to
    # let the bytes through up to its limit, but the test cares only about
    # the parent script's exit code, not its stderr content.
    proc = subprocess.run(
        ["bash", "-c", snippet],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.returncode


@pytest.fixture(scope="module")
def cap_paths() -> list[Path]:
    """Both copies of the cap lib must exist before we test propagation."""
    paths = [CAP_LIB, TEMPLATE_CAP_LIB]
    for p in paths:
        assert p.is_file(), f"cap library missing: {p}"
    return paths


@pytest.mark.parametrize("cap_path", [CAP_LIB, TEMPLATE_CAP_LIB], ids=["claude", "templates"])
@pytest.mark.parametrize(
    ("scenario", "snippet_fmt", "expected"),
    [
        # 01: bare exit 2 after sourcing
        ("bare_exit2", '. "{cap}"; echo m >&2; exit 2', 2),
        # 02: set -e + exit 2
        ("setE_exit2", 'set -e; . "{cap}"; echo m >&2; exit 2', 2),
        # 03: set -euo pipefail + exit 2 (the smtp-debug-guard pattern)
        ("strict_exit2", 'set -euo pipefail; . "{cap}"; echo m >&2; exit 2', 2),
        # 04: exit 1 (not just exit 2 — any non-zero must propagate)
        ("exit1", '. "{cap}"; echo m >&2; exit 1', 1),
        # 05: exit 0 sanity (cap must not turn 0 into something else either)
        ("exit0", '. "{cap}"; echo m >&2; exit 0', 0),
        # 06: huge stderr forces head -c to close its end early. The drain
        # pattern (`cat >/dev/null`) must swallow the rest without SIGPIPE-
        # killing the parent and without changing the parent's exit code.
        (
            "overrun_exit2",
            'STDERR_CAP_BYTES=64 . "{cap}"; '
            "for i in $(seq 1 100000); do echo spam $i >&2; done; "
            "exit 2",
            2,
        ),
        # 07: exit immediately after sourcing — pipe never receives a write
        ("immediate_exit2", '. "{cap}"; exit 2', 2),
        # 08: exit code propagated up from a function via `return`
        (
            "func_return_exit",
            '. "{cap}"; f() {{ echo m >&2; return 2; }}; f || exit $?',
            2,
        ),
        # 09: double-source the lib then exit 2 (should be idempotent-ish; at
        # minimum must not break exit propagation)
        ("double_source_exit2", '. "{cap}"; . "{cap}"; echo m >&2; exit 2', 2),
        # 10: STDERR_CAP_DISABLE=1 takes the early-return path; exit must
        # still propagate
        (
            "disabled_exit2",
            'STDERR_CAP_DISABLE=1 . "{cap}"; echo m >&2; exit 2',
            2,
        ),
        # 11: explicit `trap … EXIT` set by the caller. The cap MUST NOT
        # install or override the EXIT trap (if it did, it would swallow $?).
        (
            "trap_exit_exit2",
            'trap "echo trap-ran >&2" EXIT; . "{cap}"; echo m >&2; exit 2',
            2,
        ),
        # 12: backgrounded child writing to stderr while parent exits 2
        (
            "bg_child_exit2",
            '. "{cap}"; (sleep 0.05; echo bg >&2) & echo m >&2; exit 2',
            2,
        ),
        # 13: while-read loop on a heredoc, then exit 2 (mimics input parsing)
        (
            "while_read_exit2",
            '. "{cap}"; while read line; do echo got $line >&2; done <<<"hello"; '
            "exit 2",
            2,
        ),
        # 14: heredoc emit followed by exit (the EXACT vercel-token-guard /
        # smtp-debug-guard / kill-guard pattern)
        (
            "heredoc_exit2",
            '. "{cap}"; cat >&2 <<MSG\nBLOCKED: test message\nMSG\nexit 2',
            2,
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else f"exit{v}",
)
def test_cap_preserves_exit_code(
    cap_path: Path, scenario: str, snippet_fmt: str, expected: int
) -> None:
    """Sourcing stderr-cap.sh must not alter the script's exit code.

    Each scenario is a minimal bash snippet that sources the cap lib and
    triggers a candidate failure mode (trap, errexit, overrun, disable,
    nested function, etc.). The script's exit code must reach the parent
    process unchanged.

    Why this matters: every PreToolUse guard relies on ``exit 2`` reaching
    Claude Code so the tool is blocked. If the cap library swallows the
    code into 0, Claude Code lets the dangerous tool run while the model
    sees the BLOCK message — a false sense of safety. Any change to the
    cap library that breaks this contract is a security regression.
    """
    snippet = snippet_fmt.format(cap=cap_path)
    actual = _run_bash(snippet)
    assert actual == expected, (
        f"stderr-cap.sh ({cap_path.name}) altered exit code in scenario "
        f"{scenario!r}: expected {expected}, got {actual}.\n"
        f"Snippet: {snippet!r}"
    )


# ---------------------------------------------------------------------------
# Guard-level: the three blocking PreToolUse guards must exit 2 when their
# trigger fires. This catches the bug end-to-end (cap lib + guard logic +
# heredoc emit + exit), not just the cap lib in isolation.
#
# These tests run only against the .claude/hooks/ side because the
# templates/hooks/ guards are byte-identical (enforced by the template
# drift gate in CI) — testing both would be pure redundancy.
# ---------------------------------------------------------------------------


GUARD_FIXTURES = [
    pytest.param(
        "pre-vercel-token-guard.sh",
        '{"tool_name":"Bash","tool_input":'
        '{"command":"vercel deploy --token=vcp_AAAA --prod"}}',
        2,
        id="vercel_token_blocks",
    ),
    pytest.param(
        "pre-vercel-token-guard.sh",
        '{"tool_name":"Bash","tool_input":'
        '{"command":"vercel deploy --prod"}}',
        0,
        id="vercel_no_token_passes",
    ),
    pytest.param(
        "pre-kill-guard.sh",
        '{"tool_name":"Bash","tool_input":{"command":"pkill nautilus"}}',
        2,
        id="kill_protected_blocks",
    ),
    pytest.param(
        "pre-kill-guard.sh",
        '{"tool_name":"Bash","tool_input":{"command":"pkill my-test-process"}}',
        0,
        id="kill_unprotected_passes",
    ),
    pytest.param(
        "pre-smtp-debug-guard.sh",
        # Python smtplib with set_debuglevel — the exact incident pattern.
        '{"tool_name":"Bash","tool_input":{"command":'
        '"python3 -c \\"import smtplib; '
        "s=smtplib.SMTP_SSL('smtp.x',465); "
        's.set_debuglevel(1); s.login(\\\\\\"u\\\\\\",\\\\\\"p\\\\\\")\\""}}',
        2,
        id="smtp_debuglevel_blocks",
    ),
    pytest.param(
        "pre-smtp-debug-guard.sh",
        '{"tool_name":"Bash","tool_input":{"command":"echo hello world"}}',
        0,
        id="smtp_unrelated_passes",
    ),
]


@pytest.mark.parametrize(("hook_name", "stdin_json", "expected"), GUARD_FIXTURES)
def test_guard_propagates_exit_code(
    hook_name: str, stdin_json: str, expected: int
) -> None:
    """Each blocking guard must exit with the documented code (2 or 0).

    This is the end-to-end regression: it invokes the guard exactly as
    Claude Code does per ``settings.json`` (``bash <hook>`` with the JSON
    payload on stdin), and asserts the exit code reaches the parent.

    If a future change to ``_lib/stderr-cap.sh`` breaks exit propagation,
    these tests will turn red even if the cap-library matrix above
    accidentally passes.
    """
    hook_path = HOOKS_DIR / hook_name
    if not hook_path.is_file():
        pytest.skip(f"guard {hook_name} not present in this checkout")

    proc = subprocess.run(
        ["bash", str(hook_path), "", "", ""],
        input=stdin_json,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == expected, (
        f"{hook_name} returned {proc.returncode}, expected {expected}.\n"
        f"stdin: {stdin_json}\nstderr:\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# Cap library file integrity: the .claude/ and templates/ copies must stay
# byte-identical (enforced separately by check_template_drift.py, but we
# add a lightweight guard here so a developer running pytest locally
# notices drift before CI does).
# ---------------------------------------------------------------------------


def test_cap_library_in_sync_across_canonical_and_template() -> None:
    """`.claude/hooks/_lib/stderr-cap.sh` and its templates/ twin must match.

    The drift gate in CI catches this too, but a fast unit-test failure
    is friendlier than a CI-only failure when iterating on the cap lib.
    """
    a = CAP_LIB.read_bytes()
    b = TEMPLATE_CAP_LIB.read_bytes()
    assert a == b, (
        f"stderr-cap.sh diverged: .claude/ ({len(a)} B) vs templates/ "
        f"({len(b)} B). Sync them or the template drift gate will fail."
    )


def test_cap_ps1_in_sync_across_canonical_and_template() -> None:
    """Same invariant for the PowerShell sibling."""
    ps1_a = CAP_LIB.with_suffix(".ps1")
    ps1_b = TEMPLATE_CAP_LIB.with_suffix(".ps1")
    if not (ps1_a.is_file() and ps1_b.is_file()):
        pytest.skip("no .ps1 sibling in this checkout")
    assert ps1_a.read_bytes() == ps1_b.read_bytes(), (
        "stderr-cap.ps1 diverged between .claude/ and templates/"
    )
