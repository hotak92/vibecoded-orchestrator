# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.77 Part 9 task 8 — I/O reduction sweep (verification / regression guard).

The audit + writes-verification found the per-tool-call write surface is already
bounded: the ONLY hook writing on EVERY tool call is the TOUCAN dataset log,
which is size-capped + rotated (D-14, v0.2.75), and CONTEXT_STATE injection is
DIFF-based (diff-context-inject) rather than a per-prompt full re-read.

Per the v0.2.77 USER ruling (no telemetry/log/sync stream may be dropped to
reduce I/O; only batching / caching / rotation-with-data-preserved / refresh-
timing qualify), task 8 makes NO behavioural change — it pins these bounds so a
future edit can't silently reintroduce an unbounded per-call writer or turn the
diff-based injector back into a full re-read.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"


def test_toucan_log_is_size_capped_and_rotated() -> None:
    """The every-tool-call TOUCAN log must stay bounded (D-14): a byte cap +
    rotation that keeps the newest rows. Both .sh and .ps1."""
    sh = (HOOKS / "pre-tool-use.sh").read_text(encoding="utf-8")
    assert "_TOUCAN_MAX_BYTES" in sh, "TOUCAN byte cap missing from pre-tool-use.sh"
    assert "_TOUCAN_KEEP_LINES" in sh, "TOUCAN rotation keep-lines missing"
    # Rotation must actually re-write the file (tail of newest rows), not just
    # detect the overflow.
    assert 'tail -n "$_TOUCAN_KEEP_LINES"' in sh, (
        "TOUCAN rotation must keep the newest rows on overflow"
    )
    ps1 = (HOOKS / "pre-tool-use.ps1").read_text(encoding="utf-8")
    assert "TOUCAN" in ps1 or "toucan" in ps1, "ps1 sibling must handle the TOUCAN log"


def test_toucan_field_values_are_truncated() -> None:
    """Large fields (content/new_string/old_string/command) are truncated before
    serialize so a single row can't balloon the every-call log."""
    sh = (HOOKS / "pre-tool-use.sh").read_text(encoding="utf-8")
    assert "_TOUCAN_FIELD_CAP" in sh, "TOUCAN per-field truncation cap missing"


def test_context_state_injection_is_diff_based() -> None:
    """diff-context-inject must diff a baseline snapshot (section-level), not
    re-read + re-emit the whole CONTEXT_STATE.md every prompt."""
    sh = (HOOKS / "diff-context-inject.sh").read_text(encoding="utf-8")
    # Baseline snapshot + diff are the load-bearing mechanism.
    assert "ctx_snapshot_" in sh, "diff-context-inject must key a per-session baseline snapshot"
    assert "diff" in sh.lower(), "diff-context-inject must diff against the baseline"


def test_no_unbounded_every_tool_call_writer_introduced() -> None:
    """Guard: the every-tool-call PreToolUse hook (pre-tool-use.sh) must not add
    a NEW append to an uncapped log. We assert the known append targets are the
    bounded/rare ones (TOUCAN — capped; SECURITY_LOG — security-block only;
    SESSION_READS_FILE / seen-reads — session-scoped, compact-wiped). This is a
    canary: a new `>> "$SOME_LOG"` on the hot path should make a reviewer either
    bound it or update this allow-list.
    """
    sh = (HOOKS / "pre-tool-use.sh").read_text(encoding="utf-8")
    allowed_targets = {
        '>> "$TOUCAN_LOG"',
        '>> "$SECURITY_LOG"',
        '>> "$SESSION_READS_FILE"',
        '>> "$_UNIFIED_READS"',
    }
    # Collect every `>> "$..."` append target in the hook.
    import re

    appends = set(re.findall(r'>>\s*"\$[A-Za-z_][A-Za-z0-9_]*"', sh))
    unexpected = {a for a in appends if a not in allowed_targets}
    assert not unexpected, (
        f"new append target(s) on the every-tool-call hook: {unexpected}. "
        "Bound them (size cap / rotation) or add to the allow-list here."
    )
