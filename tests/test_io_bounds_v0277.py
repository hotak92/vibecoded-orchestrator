# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.77 Part 9 task 8 — I/O reduction sweep (verification / regression guard).

The audit + writes-verification found the per-tool-call write surface is
bounded. v0.2.77 9-bis went further and RETIRED the TOUCAN dataset writer
(the former every-tool-call log) after the RL-collection verification confirmed
it had zero consumers — so no per-tool-call log is written at all now. What
remains guarded here: CONTEXT_STATE injection is DIFF-based (diff-context-inject)
rather than a per-prompt full re-read, and the every-tool-call hook must not
reintroduce an unbounded per-call append.

Per the v0.2.77 USER ruling (no telemetry/log/sync stream carrying training data
may be dropped to reduce I/O; only batching / caching / rotation-with-data-
preserved / refresh-timing / retiring-a-zero-consumer-collector qualify), task 8
makes NO behavioural change to the retained streams — it pins the bounds so a
future edit can't silently reintroduce an unbounded per-call writer or turn the
diff-based injector back into a full re-read.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"


def test_toucan_writer_is_retired() -> None:
    """v0.2.77 9-bis retired the every-tool-call TOUCAN dataset writer (zero
    consumers). Guard that the WRITE MACHINERY stays gone on BOTH .sh and .ps1 so
    a future edit can't silently reintroduce the unbounded-per-call-write class it
    caused. (A historical-note comment may still mention the filename by name;
    what must not return is the log-path variable + rotate/truncate constants that
    actually did the writing.)"""
    for name in ("pre-tool-use.sh", "pre-tool-use.ps1"):
        text = (HOOKS / name).read_text(encoding="utf-8")
        # No live append to the retired log path.
        assert ">> \"$TOUCAN_LOG\"" not in text, f"{name} still appends to $TOUCAN_LOG"
        assert "Add-Content -Path $ToucanLog" not in text, (
            f"{name} still appends to $ToucanLog"
        )
        # The write machinery (log-path var + rotate/truncate constants) must
        # also be gone.
        for token in ("TOUCAN_LOG", "TOUCAN_MAX_BYTES", "TOUCAN_FIELD_CAP",
                      "TOUCAN_KEEP_LINES", "TOUCAN_JSONL",
                      "ToucanLog", "ToucanMaxBytes", "ToucanFieldCap",
                      "ToucanKeepLines", "ToucanTruncFields"):
            assert token not in text, f"{name} still carries retired TOUCAN token {token!r}"


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
    bounded/rare ones (SECURITY_LOG — security-block only; SESSION_READS_FILE /
    seen-reads — session-scoped, compact-wiped). The TOUCAN log — the former
    every-tool-call writer — was RETIRED in v0.2.77 9-bis, so it is no longer on
    this list. This is a canary: a new `>> "$SOME_LOG"` on the hot path should
    make a reviewer either bound it or update this allow-list.
    """
    sh = (HOOKS / "pre-tool-use.sh").read_text(encoding="utf-8")
    allowed_targets = {
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
