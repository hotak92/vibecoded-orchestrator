# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.70 Stream F — deferred citation queue (F-QUEUE) + shared modules.

Covers:
  * citation_pending stage/read/list/delete roundtrip + TTL sweep.
  * answer_window extraction + position matching (shared with the MCP monitor).
  * rl_drain_citations ACCUMULATE-DON'T-DROP: a sub-gate pending file SURVIVES a
    Stop (not computed, not deleted); an at/above-gate pending file is computed
    + deleted.
  * the extracted shared modules are importable by BOTH callers (server.py
    monitor shim + the Stop-hook drain).
"""

from __future__ import annotations

import json

import pytest

from claude_mcp_servers.rl_client import citation_pending as cp
from claude_mcp_servers.rl_client import answer_window as aw
from claude_mcp_servers.scripts import rl_drain_citations as drain


# ----------------------------------------------------------------------
# 1. citation_pending — stage / read / list / delete roundtrip.
# ----------------------------------------------------------------------


class TestPendingRoundtrip:
    def test_stage_read_delete(self, tmp_path) -> None:
        ctx = {"nodes": [{"title": "A", "n_emb": [0.1, 0.2]}], "query_emb": [0.3, 0.4]}
        path = cp.stage_pending(
            session_id="sess1", task_id="pre_edit_abc", seq=2,
            query="how does X work", ctx=ctx, source="hook",
            project_root=tmp_path,
        )
        assert path is not None and path.exists()
        payload = cp.read_pending(path)
        assert payload["session_id"] == "sess1"
        assert payload["task_id"] == "pre_edit_abc"
        assert payload["seq"] == 2
        assert payload["source"] == "hook"
        assert payload["query"] == "how does X work"
        assert payload["ctx"] == ctx
        assert "ts_ms" in payload

        cp.delete_pending(path)
        assert not path.exists()

    def test_list_for_session_filters_by_prefix(self, tmp_path) -> None:
        cp.stage_pending(session_id="s1", task_id="t1", seq=1, query="q",
                         ctx={"nodes": []}, project_root=tmp_path)
        cp.stage_pending(session_id="s2", task_id="t2", seq=1, query="q",
                         ctx={"nodes": []}, project_root=tmp_path)
        s1 = cp.list_pending_for_session("s1", tmp_path)
        assert len(s1) == 1
        assert s1[0].name.startswith("s1__")
        # Empty session_id lists ALL (the drain's orphan-recovery path).
        all_files = cp.list_pending_for_session("", tmp_path)
        assert len(all_files) == 2

    def test_query_snippet_truncated(self, tmp_path) -> None:
        long_q = "x" * 500
        path = cp.stage_pending(session_id="s", task_id="t", seq=1, query=long_q,
                                ctx={"nodes": []}, project_root=tmp_path)
        payload = cp.read_pending(path)
        assert len(payload["query"]) == 120

    def test_sweep_expired(self, tmp_path) -> None:
        path = cp.stage_pending(session_id="s", task_id="old", seq=1, query="q",
                                ctx={"nodes": []}, project_root=tmp_path)
        # Force a stale ts_ms by rewriting the file.
        payload = cp.read_pending(path)
        payload["ts_ms"] = 1000  # ancient
        path.write_text(json.dumps(payload))
        # A fresh one stays.
        cp.stage_pending(session_id="s", task_id="fresh", seq=1, query="q",
                         ctx={"nodes": []}, project_root=tmp_path)
        deleted = cp.sweep_expired(tmp_path, ttl_seconds=3600)
        assert deleted == 1
        remaining = {p.name for p in cp.list_pending_for_session("", tmp_path)}
        assert any("fresh" in n for n in remaining)
        assert not any("old" in n for n in remaining)

    def test_path_separator_sanitised(self, tmp_path) -> None:
        # A session_id with a path separator must not escape the pending dir.
        path = cp.stage_pending(session_id="a/../../etc", task_id="t/x", seq=1,
                                query="q", ctx={"nodes": []}, project_root=tmp_path)
        assert path is not None
        assert path.parent == cp.pending_dir(tmp_path)


# ----------------------------------------------------------------------
# 2. answer_window — extraction + position matching (shared home).
# ----------------------------------------------------------------------


def _assistant_text(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _assistant_tool(name, inp):
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def _user_text(text):
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


class TestAnswerWindow:
    def test_excludes_tool_returns_includes_text_and_input(self) -> None:
        messages = [
            _assistant_tool("hybrid_search", {"query": "topic"}),
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "SHOULD BE EXCLUDED"}]}},
            _assistant_text("Here is the synthesized answer."),
            _assistant_tool("Edit", {"file_path": "f.py", "new": "code"}),
        ]
        window, _complete = aw.extract_answer_window(messages, 0, 0)
        assert "synthesized answer" in window
        assert "Edit:" in window  # tool name + input included
        assert "SHOULD BE EXCLUDED" not in window  # tool RETURN excluded

    def test_accumulates_across_human_turns(self) -> None:
        messages = [
            _assistant_tool("hybrid_search", {"query": "topic"}),
            _assistant_text("first part. "),
            _user_text("continue"),
            _assistant_text("second part."),
        ]
        window, _ = aw.extract_answer_window(messages, 0, 0)
        assert "first part" in window and "second part" in window

    def test_threshold_completes(self) -> None:
        big = "z" * 5000
        messages = [_assistant_tool("hybrid_search", {"query": "q"}),
                    _assistant_text(big)]
        # gate of 1000 tokens = 4000 chars; 5000 chars > that.
        window, complete = aw.extract_answer_window(messages, 0, 0, threshold_tokens=1000)
        assert complete is True
        assert len(window) <= 1000 * 4

    def test_match_position_for_query(self) -> None:
        messages = [
            _assistant_tool("hybrid_search", {"query": "alpha"}),
            _assistant_text("ans1"),
            _assistant_tool("semantic_graph_search", {"query": "beta"}),
            _assistant_text("ans2"),
        ]
        positions = aw.find_kg_positions(messages)
        assert len(positions) == 2
        matched = aw.match_position_for_query(messages, positions, "beta")
        assert matched == positions[1]


# ----------------------------------------------------------------------
# 3. rl_drain_citations — ACCUMULATE-DON'T-DROP.
# ----------------------------------------------------------------------


class TestDrainAccumulateDontDrop:
    def _make_transcript(self, query, answer):
        return [
            _assistant_tool("hybrid_search", {"query": query}),
            _assistant_text(answer),
        ]

    def test_sub_gate_pending_survives(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("RL_MIN_ANSWER_TOKENS_FOR_CITATION", "1000")  # 4000 chars
        # Stage a pending file.
        cp.stage_pending(session_id="s", task_id="t1", seq=1, query="topic",
                         ctx={"nodes": [{"title": "A", "n_emb": [0.1]}]},
                         project_root=tmp_path)
        # Transcript answer is SHORT (below 4000 chars).
        transcript = tmp_path / "t.jsonl"
        with open(transcript, "w") as fh:
            for m in self._make_transcript("topic", "short answer"):
                fh.write(json.dumps(m) + "\n")

        computed = {"called": 0}

        def _compute(task_id, answer, ctx, write=True):
            computed["called"] += 1
            return {"cited": {}}

        summary = drain.drain_session(
            "s", str(transcript), project_root=tmp_path, compute_fn=_compute,
        )
        # Below gate → NOT computed, NOT deleted.
        assert computed["called"] == 0
        assert summary["computed"] == 0
        assert summary["left"] == 1
        # The pending file SURVIVES for the next Stop.
        assert len(cp.list_pending_for_session("s", tmp_path)) == 1

    def test_above_gate_pending_computed_and_deleted(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("RL_MIN_ANSWER_TOKENS_FOR_CITATION", "10")  # 40 chars
        cp.stage_pending(session_id="s", task_id="t2", seq=1, query="topic",
                         ctx={"nodes": [{"title": "A", "n_emb": [0.1]}]},
                         project_root=tmp_path)
        big_answer = "this is a substantial synthesized answer about the topic " * 5
        transcript = tmp_path / "t.jsonl"
        with open(transcript, "w") as fh:
            for m in self._make_transcript("topic", big_answer):
                fh.write(json.dumps(m) + "\n")

        computed = {"called": 0, "task_id": None, "answer_len": 0}

        def _compute(task_id, answer, ctx, write=True):
            computed["called"] += 1
            computed["task_id"] = task_id
            computed["answer_len"] = len(answer)
            return {"cited": {"A": True}}

        summary = drain.drain_session(
            "s", str(transcript), project_root=tmp_path, compute_fn=_compute,
        )
        assert computed["called"] == 1
        assert computed["task_id"] == "t2"
        assert summary["computed"] == 1
        # The pending file is DELETED after a successful compute.
        assert len(cp.list_pending_for_session("s", tmp_path)) == 0

    def test_compute_softfail_leaves_file(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("RL_MIN_ANSWER_TOKENS_FOR_CITATION", "10")
        cp.stage_pending(session_id="s", task_id="t3", seq=1, query="topic",
                         ctx={"nodes": [{"title": "A", "n_emb": [0.1]}]},
                         project_root=tmp_path)
        big = "substantial answer about the topic here " * 5
        transcript = tmp_path / "t.jsonl"
        with open(transcript, "w") as fh:
            for m in self._make_transcript("topic", big):
                fh.write(json.dumps(m) + "\n")

        def _compute(task_id, answer, ctx, write=True):
            return None  # compute soft-failed (no embed service)

        summary = drain.drain_session(
            "s", str(transcript), project_root=tmp_path, compute_fn=_compute,
        )
        # Soft-fail → leave the file for retry, not delete.
        assert summary["left"] == 1
        assert len(cp.list_pending_for_session("s", tmp_path)) == 1

    def test_no_transcript_leaves_file(self, tmp_path) -> None:
        cp.stage_pending(session_id="s", task_id="t4", seq=1, query="q",
                         ctx={"nodes": []}, project_root=tmp_path)
        summary = drain.drain_session("s", None, project_root=tmp_path,
                                      compute_fn=lambda *a, **k: {"cited": {}})
        assert summary["left"] == 1
        assert len(cp.list_pending_for_session("s", tmp_path)) == 1


# ----------------------------------------------------------------------
# 4. Shared modules importable by BOTH callers.
# ----------------------------------------------------------------------


class TestSharedModuleImports:
    def test_answer_window_importable(self) -> None:
        from claude_mcp_servers.rl_client.answer_window import (
            extract_answer_window, load_messages, find_kg_positions,
            match_position_for_query, token_estimate,
        )
        assert callable(extract_answer_window)
        assert callable(match_position_for_query)

    def test_citation_compute_importable(self) -> None:
        from claude_mcp_servers.rl_client.citation_compute import compute_citation
        assert callable(compute_citation)

    def test_citation_pending_importable(self) -> None:
        from claude_mcp_servers.rl_client.citation_pending import (
            stage_pending, read_pending, delete_pending, sweep_expired,
            list_pending_for_session, pending_dir,
        )
        assert callable(stage_pending)

    def test_mcp_monitor_uses_shared_extract(self) -> None:
        # server.py's _rl_extract_answer_window is now a thin shim that
        # forwards to the shared answer_window module — same result.
        from claude_mcp_servers.weaviate_mcp.server import _rl_extract_answer_window
        messages = [
            _assistant_tool("hybrid_search", {"query": "q"}),
            _assistant_text("the answer"),
        ]
        window, _ = _rl_extract_answer_window(messages, 0, 0)
        assert "the answer" in window

    def test_mcp_monitor_uses_shared_compute(self) -> None:
        # server.py's _rl_compute_and_write_citations is a thin shim to the
        # shared citation_compute module. We only assert the import wiring
        # (full compute needs an embedding service).
        import inspect
        from claude_mcp_servers.weaviate_mcp.server import (
            _rl_compute_and_write_citations,
        )
        src = inspect.getsource(_rl_compute_and_write_citations)
        assert "citation_compute" in src
        assert "compute_citation" in src
