# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.77 9-bis — hook-cohort citation-label recovery via timestamp anchor.

The RL-collection verification found that hook-path retrievals (task_type
``pre_edit_kg_search``, source ``hook``) stage a hook-DERIVED query that never
appears verbatim as a KG ``tool_use`` in the transcript. So
``match_position_for_query`` could never locate them, the drain always left them,
and the TTL sweep deleted them unlabeled — ~87% of retrievals (the hook cohort)
lost their citation label (6 citation events vs 16,341 retrievals live).

The fix (within the existing F-QUEUE design, no schema change, no opt-out change):
when a ``source == "hook"`` payload's query-match fails, ``drain_session`` anchors
the answer window by TIMESTAMP — the first assistant message at/after the
retrieval's ``ts_ms`` — and the existing 25k gate + terminal floor apply
unchanged.

Tests aim at the REAL drain entry point (``rl_drain_citations.drain_session``)
with mocked transcript fixtures.

Coverage:
  * ACT — a hook-cohort payload whose query has NO transcript tool_use match
    still gets computed via the timestamp window.
  * LEAVE-ALONE — a transcript-matched (query) payload takes the exact current
    path; a NON-hook (mcp) payload with a failed query-match is still left;
    a hook payload with no assistant message after ts_ms is still left (discarded
    eventually by TTL, exactly as today).
"""

from __future__ import annotations

import json
import time

from claude_mcp_servers.rl_client import citation_pending as cp
from claude_mcp_servers.rl_client import answer_window as aw
from claude_mcp_servers.scripts import rl_drain_citations as drain


def _now_ms() -> int:
    """A fresh epoch-ms so the drain's TTL sweep (60 min) never reaps the fixture
    before it is processed. The unit tests of the anchor helper use synthetic
    small values (no TTL sweep there); the drain-level tests must use real time."""
    return int(time.time() * 1000)


# --- transcript fixture helpers (with top-level ISO-8601 timestamps) -------- #


def _ts_iso(ms: int) -> str:
    """Epoch-ms -> the ISO-8601 ``...Z`` shape Claude Code stamps on messages."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def _assistant_tool(name, inp, *, ts_ms=None):
    msg = {"type": "assistant",
           "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}
    if ts_ms is not None:
        msg["timestamp"] = _ts_iso(ts_ms)
    return msg


def _assistant_text(text, *, ts_ms=None):
    msg = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    if ts_ms is not None:
        msg["timestamp"] = _ts_iso(ts_ms)
    return msg


def _write_transcript(path, messages):
    with open(path, "w") as fh:
        for m in messages:
            fh.write(json.dumps(m) + "\n")


def _force_pending_ts(pending_file, ts_ms: int) -> None:
    """Rewrite a staged pending file's ``ts_ms`` to a known value so the
    timestamp anchor is deterministic in a test (one home for the 4 call-sites)."""
    payload = cp.read_pending(pending_file)
    assert payload is not None
    payload["ts_ms"] = ts_ms
    pending_file.write_text(json.dumps(payload))


def _make_compute_probe():
    seen = {"called": 0, "task_id": None, "answer_len": 0}

    def _compute(task_id, answer, ctx, write=True):
        seen["called"] += 1
        seen["task_id"] = task_id
        seen["answer_len"] = len(answer)
        return {"cited": {"A": True}}

    return seen, _compute


# --- unit: the anchor helper itself ----------------------------------------- #


class TestTimestampAnchorHelper:
    def test_returns_first_assistant_at_or_after_ts(self) -> None:
        messages = [
            _assistant_text("too early", ts_ms=1000),
            _assistant_tool("hybrid_search", {"query": "unrelated"}, ts_ms=2000),
            _assistant_text("the answer starts here", ts_ms=3000),
            _assistant_text("more answer", ts_ms=4000),
        ]
        # Retrieval staged at t=2500 → first assistant msg with ts >= 2500 is idx 2.
        matched = aw.match_position_by_timestamp(messages, 2500)
        assert matched is not None
        assert matched == (2, -1)
        # blk_idx == -1 makes extract_answer_window include block 0 onward.
        window, _ = aw.extract_answer_window(messages, matched[0], matched[1])
        assert "the answer starts here" in window and "more answer" in window
        assert "too early" not in window

    def test_none_when_no_message_after_ts(self) -> None:
        messages = [_assistant_text("early", ts_ms=1000)]
        assert aw.match_position_by_timestamp(messages, 5000) is None

    def test_none_when_ts_missing_or_invalid(self) -> None:
        messages = [_assistant_text("x", ts_ms=1000)]
        assert aw.match_position_by_timestamp(messages, None) is None
        assert aw.match_position_by_timestamp(messages, 0) is None
        assert aw.match_position_by_timestamp(messages, -3) is None

    def test_skips_unstamped_and_non_assistant(self) -> None:
        messages = [
            {"type": "user", "message": {"content": []}, "timestamp": _ts_iso(2000)},
            _assistant_text("no timestamp here"),  # unstamped → skipped
            _assistant_text("stamped answer", ts_ms=3000),
        ]
        assert aw.match_position_by_timestamp(messages, 2500) == (2, -1)


# --- ACT: hook cohort with no tool_use query match gets a label ------------- #


class TestHookCohortRecovered:
    def test_hook_payload_without_query_match_is_computed(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("RL_MIN_ANSWER_TOKENS_FOR_CITATION", "10")  # 40-char gate
        # Stage a HOOK-cohort pending file: the staged query is the hook-derived
        # concept string, which does NOT appear as a hybrid_search tool_use below.
        ts_ms = _now_ms()
        cp.stage_pending(
            session_id="s", task_id="pre_edit_abc", seq=None,
            query="database caching optimization", ctx={"nodes": [{"title": "A", "n_emb": [0.1]}]},
            source="hook", project_root=tmp_path,
        )
        # Force the payload ts_ms to a known value so the anchor is deterministic.
        _force_pending_ts(cp.list_pending_for_session("s", tmp_path)[0], ts_ms)

        # Transcript: a KG search whose query is UNRELATED to the staged query
        # (so match_position_for_query returns None), followed by the answer.
        big_answer = "this is the synthesized answer about the topic " * 5
        transcript = tmp_path / "t.jsonl"
        _write_transcript(transcript, [
            _assistant_tool("hybrid_search", {"query": "something totally different"},
                            ts_ms=ts_ms - 500),
            _assistant_text(big_answer, ts_ms=ts_ms + 500),
        ])

        seen, compute = _make_compute_probe()
        summary = drain.drain_session(
            "s", str(transcript), project_root=tmp_path, compute_fn=compute,
        )
        # The timestamp anchor recovered the label: computed + deleted.
        assert seen["called"] == 1, "hook cohort must be computed via timestamp anchor"
        assert seen["task_id"] == "pre_edit_abc"
        assert summary["computed"] == 1
        assert summary["left"] == 0
        assert len(cp.list_pending_for_session("s", tmp_path)) == 0


# --- LEAVE-ALONE: current paths unchanged ----------------------------------- #


class TestLeaveAloneCurrentPaths:
    def test_query_matched_payload_takes_current_path(self, tmp_path, monkeypatch) -> None:
        """A payload whose query DOES match a transcript tool_use uses the exact
        current query-anchor — the timestamp fallback never fires. We prove it by
        making the query-matched position and the timestamp position DIFFERENT and
        asserting the window is anchored at the query match (not the timestamp)."""
        monkeypatch.setenv("RL_MIN_ANSWER_TOKENS_FOR_CITATION", "10")
        ts_ms = _now_ms()
        cp.stage_pending(
            session_id="s", task_id="pre_edit_q", seq=None,
            query="alpha topic", ctx={"nodes": [{"title": "A", "n_emb": [0.1]}]},
            source="hook", project_root=tmp_path,
        )
        _force_pending_ts(cp.list_pending_for_session("s", tmp_path)[0], ts_ms)

        # tool_use query "alpha topic" matches the staged query. The answer right
        # after it contains a UNIQUE marker; a LATER (timestamp-only) message
        # contains a different marker. The query anchor must capture BOTH (it
        # accumulates to end-of-transcript), but critically starts at the matched
        # search — anchoring by timestamp (ts_ms + 100) would start LATER and miss
        # the first marker.
        answer_a = "QUERYANCHORED synthesized answer about alpha " * 3
        answer_b = "later block " * 3
        transcript = tmp_path / "t.jsonl"
        _write_transcript(transcript, [
            _assistant_tool("hybrid_search", {"query": "alpha topic"}, ts_ms=ts_ms - 1000),
            _assistant_text(answer_a, ts_ms=ts_ms - 900),
            _assistant_text(answer_b, ts_ms=ts_ms + 100),
        ])

        captured: dict[str, str] = {"answer": ""}

        def _compute(task_id, answer, ctx, write=True):
            captured["answer"] = answer
            return {"cited": {"A": True}}

        summary = drain.drain_session(
            "s", str(transcript), project_root=tmp_path, compute_fn=_compute,
        )
        assert summary["computed"] == 1
        # Query-anchored window starts at the matched search → includes answer_a.
        assert "QUERYANCHORED" in captured["answer"]

    def test_non_hook_source_with_failed_match_is_left(self, tmp_path, monkeypatch) -> None:
        """An mcp-source payload whose query-match fails is STILL left — the
        timestamp fallback is hook-only, so non-hook behaviour is unchanged."""
        monkeypatch.setenv("RL_MIN_ANSWER_TOKENS_FOR_CITATION", "10")
        ts_ms = _now_ms()
        cp.stage_pending(
            session_id="s", task_id="mcp_task", seq=None,
            query="query with no transcript match", ctx={"nodes": [{"title": "A", "n_emb": [0.1]}]},
            source="mcp", project_root=tmp_path,
        )
        _force_pending_ts(cp.list_pending_for_session("s", tmp_path)[0], ts_ms)

        big_answer = "synthesized answer text here " * 5
        transcript = tmp_path / "t.jsonl"
        _write_transcript(transcript, [
            _assistant_tool("hybrid_search", {"query": "unrelated"}, ts_ms=ts_ms - 500),
            _assistant_text(big_answer, ts_ms=ts_ms + 500),
        ])

        seen, compute = _make_compute_probe()
        summary = drain.drain_session(
            "s", str(transcript), project_root=tmp_path, compute_fn=compute,
        )
        assert seen["called"] == 0, "non-hook payload must NOT use the timestamp anchor"
        assert summary["computed"] == 0
        assert summary["left"] == 1
        assert len(cp.list_pending_for_session("s", tmp_path)) == 1

    def test_hook_payload_with_no_answer_after_ts_is_left(self, tmp_path, monkeypatch) -> None:
        """A hook payload whose ts_ms is AFTER every assistant message (answer not
        flushed yet) finds no anchor → left for the next Stop / TTL, exactly like
        an unmatched query today. No premature discard, no schema change."""
        monkeypatch.setenv("RL_MIN_ANSWER_TOKENS_FOR_CITATION", "10")
        ts_ms = _now_ms()
        cp.stage_pending(
            session_id="s", task_id="pre_edit_late", seq=None,
            query="hook derived query", ctx={"nodes": [{"title": "A", "n_emb": [0.1]}]},
            source="hook", project_root=tmp_path,
        )
        _force_pending_ts(cp.list_pending_for_session("s", tmp_path)[0], ts_ms)

        # Every assistant message is stamped BEFORE ts_ms → no anchor.
        transcript = tmp_path / "t.jsonl"
        _write_transcript(transcript, [
            _assistant_tool("hybrid_search", {"query": "unrelated"}, ts_ms=ts_ms - 2000),
            _assistant_text("answer written before the retrieval ts", ts_ms=ts_ms - 1000),
        ])

        seen, compute = _make_compute_probe()
        summary = drain.drain_session(
            "s", str(transcript), project_root=tmp_path, compute_fn=compute,
        )
        assert seen["called"] == 0
        assert summary["left"] == 1
        # File survives for the next Stop (accumulate-don't-drop), not discarded.
        assert len(cp.list_pending_for_session("s", tmp_path)) == 1
