# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 RL data-correctness items (RL-1/3/4/6/7/9 + writer plumbing).

RL-1: retrieval events record ``rl_used`` (did the rerank RPC actually run)
plus the post-rerank SHOWN order per node (``shown_rank``) so citation labels
condition on the order the user actually saw, not the pre-rerank pool order.

RL-3: container 4xx responses are surfaced (WARNING on first occurrence +
``RLClient.last_call_ok/last_error`` state) and the pipeline counts
rerank fallbacks persistently — a paying user degraded to cosine gets a
signal instead of silence.

RL-4: the Stop-hook drain grows a terminal-session citation floor — a
pending file older than the terminal age whose accumulated window clears a
LOWER token floor is computed+written instead of being left to die at TTL
(sub-25k sessions were NEVER labeled → corpus censored toward long sessions).

RL-6/7/9: fire_reason + window_tokens (6) and session_id (9) ride the
citation event; chunks_matched (7) survives the writer boundary.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.rl_client import search_pipeline
from claude_mcp_servers.rl_client.telemetry_writer import RLTelemetryWriter


def _run(coro):
    return asyncio.run(coro)


def _make_request(**overrides):
    defaults = {
        "query": "how are batches validated?",
        "candidates": [
            {"title": "NodeA", "score": 0.91, "chunks_matched": 2, "best_chunk_number": 1},
            {"title": "NodeB", "score": 0.74},
            {"title": "NodeC", "score": 0.55},
        ],
        "limit": 2,
        "query_emb": [0.1] * 8,
        "embedding_source": "qwen3",
        "embedding_dim": 8,
        "embedding_model": "sample-embed",
        "task_id": "task-rl1",
        "task_type": "mcp_interactive",
        "session_id": "sess-rl1",
        "spawn_answer_monitor": False,
    }
    defaults.update(overrides)
    return search_pipeline.RerankRequest(**defaults)


def _writer(captured: list) -> RLTelemetryWriter:
    def _post(envelope, timeout: float = 2.0) -> bool:
        captured.append(envelope)
        return True

    return RLTelemetryWriter(
        project="sample-project",
        project_id="00000000-0000-0000-0000-000000000000",
        embedding_source="qwen3",
        embedding_dim=8,
        embedding_model="sample-embed",
        hub_post_fn=_post,
    )


def _payload(envelope: dict) -> dict:
    return json.loads(envelope["payload_json"])


# ---------------------------------------------------------------------------
# RL-1 — rl_used + shown order
# ---------------------------------------------------------------------------


def test_rl1_event_carries_rl_used_false_on_free_tier():
    async def _inner():
        req = _make_request()
        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False), \
             patch.object(search_pipeline, "emit_rl_event", return_value=True) as mock_emit:
            await search_pipeline.rerank_and_emit(req)
        ev = mock_emit.call_args.args[0]
        assert ev.rl_used is False
    _run(_inner())


def test_rl1_shown_rank_reflects_post_rerank_order():
    """The rerank REORDERS; shown_rank must follow the RETURNED order, and
    truncated-out candidates carry no shown_rank."""
    async def _inner():
        req = _make_request()

        async def fake_rerank(**_):
            # RL container promotes NodeC above NodeA; NodeB truncated out.
            return [
                {"title": "NodeC", "score": 0.55},
                {"title": "NodeA", "score": 0.91},
            ]

        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=True), \
             patch.object(search_pipeline, "_do_rerank", side_effect=fake_rerank), \
             patch.object(search_pipeline, "emit_rl_event", return_value=True) as mock_emit:
            result = await search_pipeline.rerank_and_emit(req)
        assert result.rl_used is True
        ev = mock_emit.call_args.args[0]
        assert ev.rl_used is True
        by_title = {n["title"]: n for n in ev.nodes}
        assert by_title["NodeC"]["shown_rank"] == 0
        assert by_title["NodeA"]["shown_rank"] == 1
        assert "shown_rank" not in by_title["NodeB"]
    _run(_inner())


def test_rl1_writer_persists_rl_used_and_shown_rank():
    captured: list = []
    w = _writer(captured)
    w.log_retrieval(
        task_id="t1",
        task_type="mcp_interactive",
        query="sample",
        nodes=[
            {"title": "NodeA", "score": 0.9, "tier": "top_k", "shown_rank": 1},
            {"title": "NodeB", "score": 0.8, "tier": "top_k"},
        ],
        rl_used=True,
    )
    assert captured
    event = _payload(captured[0])
    assert event["rl_used"] is True
    assert event["nodes"][0]["shown_rank"] == 1
    assert "shown_rank" not in event["nodes"][1]


def test_rl1_legacy_event_omits_rl_used():
    """None (legacy callers) ⇒ the field is absent, byte-stable old shape."""
    captured: list = []
    w = _writer(captured)
    w.log_retrieval(
        task_id="t2", task_type="mcp_interactive", query="sample",
        nodes=[{"title": "NodeA", "score": 0.9, "tier": "top_k"}],
    )
    event = _payload(captured[0])
    assert "rl_used" not in event
    assert "extras" not in event


# ---------------------------------------------------------------------------
# RL-7 — chunks_matched survives the writer boundary
# ---------------------------------------------------------------------------


def test_rl7_chunks_matched_survives_build_log_nodes():
    recs = search_pipeline._build_log_nodes(
        [{"title": "NodeA", "score": 0.9, "chunks_matched": 3, "best_chunk_number": 2}],
        limit=1,
    )
    assert recs[0]["chunks_matched"] == 3
    assert recs[0]["best_chunk_number"] == 2


def test_rl7_chunks_matched_reaches_v3_event():
    captured: list = []
    w = _writer(captured)
    w.log_retrieval(
        task_id="t3", task_type="mcp_interactive", query="sample",
        nodes=[{
            "title": "NodeA", "score": 0.9, "tier": "top_k",
            "chunks_matched": 3, "best_chunk_number": 2,
        }],
    )
    event = _payload(captured[0])
    assert event["nodes"][0]["chunks_matched"] == 3
    assert event["nodes"][0]["best_chunk_number"] == 2


# ---------------------------------------------------------------------------
# RL-6 / RL-9 — citation event riders (fire_reason, window_tokens, session_id)
# ---------------------------------------------------------------------------


def test_rl6_rl9_citation_event_carries_riders():
    captured: list = []
    w = _writer(captured)
    w.log_citations(
        task_id="t4",
        task_type="mcp_interactive",
        citations={"NodeA": True},
        cosine_sims={"NodeA": 0.81},
        literal_cited={"NodeA": True},
        session_id="sess-rl9",
        fire_reason="human_turn",
        window_tokens=31000,
    )
    event = _payload(captured[0])
    assert event["session_id"] == "sess-rl9"
    assert event["fire_reason"] == "human_turn"
    assert event["window_tokens"] == 31000


def test_rl6_rl9_citation_riders_optional():
    """Legacy citation calls (no riders) keep the old shape."""
    captured: list = []
    w = _writer(captured)
    w.log_citations(
        task_id="t5",
        task_type="mcp_interactive",
        citations={"NodeA": True},
    )
    event = _payload(captured[0])
    assert "fire_reason" not in event
    assert "window_tokens" not in event
    # session_id resolves via the env chain; absent env ⇒ empty string field
    # or omitted — either way it must not crash. (Explicit value asserted in
    # the rider test above.)

# ---------------------------------------------------------------------------
# RL-3 — container 4xx surfaced + fallback counted; rl_used accuracy
# ---------------------------------------------------------------------------


def _client_with_status(status: int, tmp_base_url="http://127.0.0.1:9"):
    """RLClient wired to a stub httpx client returning `status`."""
    from claude_mcp_servers.rl_client.client import RLClient

    class _Resp:
        status_code = status
        text = "env-pin mismatch" if status == 409 else "err"

        def json(self):
            return {"top_k": [{"title": "NodeA"}]}

    class _Stub:
        async def post(self, url, json=None, headers=None, timeout=None):
            return _Resp()

    return RLClient(base_url=tmp_base_url, client=_Stub())


def test_rl3_4xx_sets_last_error_and_falls_back(caplog):
    import logging as _logging

    client = _client_with_status(409)
    nodes = [{"title": "NodeA"}, {"title": "NodeB"}]
    with caplog.at_level(_logging.WARNING, logger="claude_mcp_servers.rl_client.client"):
        out = asyncio.run(client.cache_nodes("q", nodes, 2, task_id="t"))
    assert out == nodes[:2]
    assert client.last_call_ok is False
    assert "409" in (client.last_error or "")
    assert any("REFUSED" in r.message for r in caplog.records)
    # Second 4xx degrades to debug (no second WARNING).
    caplog.clear()
    with caplog.at_level(_logging.WARNING, logger="claude_mcp_servers.rl_client.client"):
        asyncio.run(client.cache_nodes("q", nodes, 2, task_id="t2"))
    assert not [r for r in caplog.records if "REFUSED" in r.message]


def test_rl3_success_sets_last_call_ok():
    client = _client_with_status(200)
    out = asyncio.run(client.cache_nodes("q", [{"title": "NodeA"}], 1, task_id="t"))
    assert client.last_call_ok is True
    assert client.last_error is None
    assert out == [{"title": "NodeA"}]


def test_rl3_pipeline_reports_rl_used_false_on_container_refusal(tmp_path, monkeypatch):
    """A 4xx-refusing container must yield rl_used=False (pre-RL-3 the
    input-order fallback was mislabeled as a successful rerank) and bump
    the persisted fallback counter."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(search_pipeline, "_WARNED_RL_FALLBACK", False)
    client = _client_with_status(409)

    async def _inner():
        req = _make_request()
        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=True), \
             patch("claude_mcp_servers.weaviate_mcp.server._get_rl_client",
                   return_value=client), \
             patch.object(search_pipeline, "emit_rl_event", return_value=True) as mock_emit:
            result = await search_pipeline.rerank_and_emit(req)
        assert result.rl_used is False
        ev = mock_emit.call_args.args[0]
        assert ev.rl_used is False
        # Ranked falls back to input order, trimmed.
        assert [n["title"] for n in result.ranked] == ["NodeA", "NodeB"]
    _run(_inner())

    counter = tmp_path / ".claude" / "state" / "rl_fallback_counter.json"
    assert counter.exists()
    data = json.loads(counter.read_text())
    assert data["count"] == 1
    assert "409" in data["last_reason"]


def test_rl3_fallback_counter_increments(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(search_pipeline, "_WARNED_RL_FALLBACK", True)
    search_pipeline._record_rl_fallback("reason one")
    search_pipeline._record_rl_fallback("reason two")
    data = json.loads(
        (tmp_path / ".claude" / "state" / "rl_fallback_counter.json").read_text()
    )
    assert data["count"] == 2
    assert data["last_reason"] == "reason two"


# ---------------------------------------------------------------------------
# RL-4 — terminal-session citation floor in the drain
# ---------------------------------------------------------------------------


def _stage_pending_file(tmp_path, session_id, task_id, *, age_seconds: float,
                        query="how are batches validated?"):
    from claude_mcp_servers.rl_client.citation_pending import stage_pending

    path = stage_pending(
        session_id=session_id,
        task_id=task_id,
        seq=None,
        query=query,
        ctx={"nodes": [{"title": "NodeA", "n_emb": [0.1] * 4}]},
        source="hook",
        project_root=tmp_path,
    )
    # Age the file by rewriting ts_ms (the drain reads the payload field).
    payload = json.loads(Path(path).read_text())
    payload["ts_ms"] = int((time.time() - age_seconds) * 1000)
    Path(path).write_text(json.dumps(payload))
    return path


def _transcript(tmp_path, query, answer_text):
    lines = [
        {
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "hybrid_search",
                 "input": {"query": query}},
                {"type": "text", "text": answer_text},
            ]},
        },
    ]
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines))
    return str(p)


def _drain(tmp_path, session_id, transcript, computed: list):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_rl_drain_v0273",
        PROJECT_ROOT / "claude_mcp_servers" / "scripts" / "rl_drain_citations.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _compute(task_id, answer, ctx, write=True):
        computed.append({"task_id": task_id, "ctx": dict(ctx)})
        return {"cosine_sims": {}, "literal_cited": {}, "cited": {}}

    return mod.drain_session(
        session_id, transcript,
        project_root=str(tmp_path),
        compute_fn=_compute,
        token_count_fn=lambda text: len(text) // 4,
    )


def test_rl4_young_below_gate_file_is_left(tmp_path):
    """Accumulate-don't-drop preserved: a YOUNG sub-gate file stays."""
    computed: list = []
    _stage_pending_file(tmp_path, "sess-a", "task-young", age_seconds=10)
    transcript = _transcript(tmp_path, "how are batches validated?", "short answer " * 400)
    summary = _drain(tmp_path, "sess-a", transcript, computed)
    assert summary["left"] == 1
    assert summary["computed"] == 0
    assert not computed


def test_rl4_aged_file_above_terminal_floor_is_computed(tmp_path):
    """An AGING file whose window clears the terminal floor gets labeled
    instead of dying at TTL."""
    computed: list = []
    _stage_pending_file(tmp_path, "sess-b", "task-aged", age_seconds=2400)
    # ~12k chars ≈ 3k tokens: above the 2000 terminal floor, below 25k gate.
    transcript = _transcript(tmp_path, "how are batches validated?", "answer body " * 1000)
    summary = _drain(tmp_path, "sess-b", transcript, computed)
    assert summary["computed"] == 1
    assert computed[0]["ctx"]["fire_reason"] == "terminal_floor"
    assert computed[0]["ctx"]["window_tokens"] >= 2000
    assert computed[0]["ctx"]["session_id"] == "sess-b"
    # Pending file deleted (one-shot).
    pend_dir = tmp_path / ".claude" / "state" / "rl_pending"
    assert not list(pend_dir.glob("*task-aged*"))


def test_rl4_aged_file_below_terminal_floor_is_left(tmp_path):
    """Aged but genuinely tiny window (< terminal floor) still left for TTL."""
    computed: list = []
    _stage_pending_file(tmp_path, "sess-c", "task-tiny", age_seconds=2400)
    transcript = _transcript(tmp_path, "how are batches validated?", "tiny " * 100)
    summary = _drain(tmp_path, "sess-c", transcript, computed)
    assert summary["computed"] == 0
    assert summary["left"] == 1


def test_rl4_normal_gate_still_fires_with_stop_drain_reason(tmp_path):
    """A window above the full 25k gate computes regardless of age, tagged
    stop_drain."""
    computed: list = []
    _stage_pending_file(tmp_path, "sess-d", "task-big", age_seconds=10)
    transcript = _transcript(tmp_path, "how are batches validated?", "big answer " * 12000)
    summary = _drain(tmp_path, "sess-d", transcript, computed)
    assert summary["computed"] == 1
    assert computed[0]["ctx"]["fire_reason"] == "stop_drain"
