# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The SSE split loop has ONE home, and both callers really share it.

v0.2.95. ``model_router.usage.split_sse_events`` was written as the extraction
TARGET for the loop duplicated in ``tool_ids.SseIdRewriter._drain``, and said
so in its own docstring: one framing rule, one boundary pattern, two loops. The
rewriter needs each event's SEPARATOR (it re-emits the original bytes, so a
CRLF stream stays CRLF) and the reader does not, which is why the split now
returns frames and the reader projects the separator away.

The test that matters is the last one: it MUTATES the shared function and
asserts BOTH callers change. A test that merely called each of them would pass
just as happily against two copies — which is the state this change ended.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))

from model_router import tool_ids, usage  # noqa: E402

LF = b"event: a\ndata: {\"type\":\"ping\"}\n\n"
CRLF = b"event: a\r\ndata: {\"type\":\"ping\"}\r\n\r\n"


def test_frames_carry_their_own_separator():
    frames, rest = tool_ids.split_sse_frames(LF + CRLF, final=False)
    assert [sep for _event, sep in frames] == [b"\n\n", b"\r\n\r\n"]
    assert rest == b""


def test_the_reader_projects_the_separator_away():
    events, rest = usage.split_sse_events(LF + CRLF, final=False)
    frames, frame_rest = tool_ids.split_sse_frames(LF + CRLF, final=False)
    assert events == [event for event, _sep in frames]
    assert rest == frame_rest


def test_a_trailing_cr_waits_mid_stream_and_terminates_at_eof():
    """The one thing the boundary pattern cannot settle, and the reason both
    callers must pass `final` to the SAME implementation."""
    buffer = b"data: 1\r\n\r"
    open_frames, open_rest = tool_ids.split_sse_frames(buffer, final=False)
    assert open_frames == [] and open_rest == buffer

    closed_frames, closed_rest = tool_ids.split_sse_frames(buffer, final=True)
    assert [event for event, _sep in closed_frames] == [b"data: 1"]
    assert closed_rest == b""

    assert usage.split_sse_events(buffer, final=False) == ([], buffer)
    assert usage.split_sse_events(buffer, final=True) == ([b"data: 1"], b"")


def test_the_rewriter_re_emits_the_original_separator():
    """What the projection drops, the rewriter keeps: matching only "\\n\\n"
    would buffer a CRLF stream to the 32 MB limit and relay it unrepaired."""
    out = tool_ids.SseIdRewriter().feed(CRLF)
    assert out == CRLF


def test_both_callers_break_when_the_shared_function_is_mutated(monkeypatch):
    """The sharing PROOF. Mutate the one home; neither caller may escape it.

    A shared helper that one caller happens not to use is exactly the
    duplication this change removed, and only a mutation can tell the two
    apart.
    """
    def swallow_everything(buffer, *, final):
        return [], b""

    monkeypatch.setattr(tool_ids, "split_sse_frames", swallow_everything)
    monkeypatch.setattr(usage, "split_sse_frames", swallow_everything)

    assert usage.split_sse_events(LF, final=True) == ([], b"")
    assert tool_ids.SseIdRewriter().feed(LF) == b""
