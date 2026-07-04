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

# ---------------------------------------------------------------------------
# RL-2 — code path retrieval telemetry
# ---------------------------------------------------------------------------


def _code_survivors():
    return [
        {
            "_c": "CodeFunction",
            "_s": 0.61,
            "_d": 0.39,
            "_p": {
                "full_name": "alpha.processing.process_batch",
                "file_path": "src/alpha/processing.py",
            },
            "_tier": "three_chunks",
            "_rerank": 0.66,
            "_boost": {"delta": 0.05, "signals": {"call_linked": True, "capped": False}},
            "chunks_matched": 2,
        },
        {
            "_c": "CodeModule",
            "_s": 0.40,
            "_d": 0.60,
            "_p": {"path": "src/alpha/util.py"},
            "_tier": "summary",
        },
    ]


def test_rl2_code_emit_builds_code_event(monkeypatch):
    import importlib

    srv = importlib.import_module("weaviate_mcp.server")
    captured: list = []
    w = _writer(captured)
    monkeypatch.setattr(
        srv, "_get_rl_telemetry_writer_for", lambda *a, **k: w
    )
    ok = srv._emit_code_retrieval_telemetry(
        query="where are batches validated?",
        query_emb=[0.1] * 16,
        survivors=_code_survivors(),
        limit=2,
        slot="codesage_embed",
        task_type="code_search",
        retrieval_floor=0.16,
        post_rerank_floor=0.22,
        anchor_present=False,
        scope="code",
    )
    assert ok is True
    event = _payload(captured[0])
    assert event["task_type"] == "code_search"
    assert event["rl_used"] is False
    assert event["extras"]["retrieval_kind"] == "code"
    assert event["extras"]["retrieval_floor"] == 0.16
    assert event["extras"]["post_rerank_floor"] == 0.22
    assert event["extras"]["anchor"] is False
    n0 = event["nodes"][0]
    assert n0["title"] == "alpha.processing.process_batch"
    assert n0["collection"] == "CodeFunction"
    assert n0["file_path"] == "src/alpha/processing.py"
    assert n0["shown_rank"] == 0
    assert n0["tier"] == "three_chunks"
    assert n0["rerank_score"] == pytest.approx(0.66)
    assert n0["boost_delta"] == pytest.approx(0.05)
    assert n0["boost_signals"]["call_linked"] is True
    assert n0["chunks_matched"] == 2
    # Module row keeps its identity via path fallback.
    assert event["nodes"][1]["title"] == "src/alpha/util.py"


def test_rl2_code_emit_empty_survivors_no_event(monkeypatch):
    import importlib

    srv = importlib.import_module("weaviate_mcp.server")
    captured: list = []
    monkeypatch.setattr(
        srv, "_get_rl_telemetry_writer_for", lambda *a, **k: _writer(captured)
    )
    ok = srv._emit_code_retrieval_telemetry(
        query="anything",
        query_emb=[0.1] * 16,
        survivors=[],
        limit=2,
        slot="codesage_embed",
    )
    assert ok is False
    assert not captured


def test_rl2_code_emit_soft_fails_on_writer_error(monkeypatch):
    import importlib

    srv = importlib.import_module("weaviate_mcp.server")

    def _boom(*a, **k):
        raise RuntimeError("writer construction exploded")

    monkeypatch.setattr(srv, "_get_rl_telemetry_writer_for", _boom)
    ok = srv._emit_code_retrieval_telemetry(
        query="anything",
        query_emb=[0.1] * 16,
        survivors=_code_survivors(),
        limit=2,
        slot="codesage_embed",
    )
    assert ok is False  # soft-fail, no raise


def test_rl2_mcp_and_cli_call_the_shared_emit_home():
    """Both surfaces route through _emit_code_retrieval_telemetry (one home)."""
    import inspect
    import importlib

    srv = importlib.import_module("weaviate_mcp.server")
    tool_fn = getattr(srv.search_code_graph, "fn", None) or srv.search_code_graph
    mcp_src = inspect.getsource(tool_fn)
    assert "_emit_code_retrieval_telemetry(" in mcp_src
    cli_src = (
        PROJECT_ROOT / "templates" / "scripts" / "query_code_graph.py"
    ).read_text(encoding="utf-8")
    assert "_emit_code_retrieval_telemetry(" in cli_src
    assert 'task_type="code_hook" if hook_format else "code_cli"' in cli_src


def test_rl2_code_tool_in_kg_search_tools():
    from claude_mcp_servers.rl_client.answer_window import KG_SEARCH_TOOLS

    assert "search_code_graph" in KG_SEARCH_TOOLS
    assert "mcp__weaviate-kg__search_code_graph" in KG_SEARCH_TOOLS
    # Structural lookups stay excluded (no semantic candidates to cite).
    assert "query_code_structure" not in KG_SEARCH_TOOLS


def test_rl9_drain_empty_staged_session_id_falls_back_to_payload(tmp_path):
    """A staged ctx carrying session_id='' must NOT shadow the pending
    payload's real session id (explicit falsy check, not setdefault)."""
    from claude_mcp_servers.rl_client.citation_pending import stage_pending

    stage_pending(
        session_id="sess-real",
        task_id="task-empty-sid",
        seq=None,
        query="how are batches validated?",
        ctx={"nodes": [{"title": "NodeA", "n_emb": [0.1] * 4}], "session_id": ""},
        source="hook",
        project_root=tmp_path,
    )
    computed: list = []
    transcript = _transcript(tmp_path, "how are batches validated?", "big answer " * 12000)
    summary = _drain(tmp_path, "sess-real", transcript, computed)
    assert summary["computed"] == 1
    assert computed[0]["ctx"]["session_id"] == "sess-real"


# ---------------------------------------------------------------------------
# RL-2b — code CITATION staging (vectors fetched with candidates → pending
# file → code-space compute)
# ---------------------------------------------------------------------------


def _code_survivors_with_vectors():
    rows = _code_survivors()
    rows[0]["n_emb"] = [1.0] + [0.0] * 15
    # Second row deliberately has NO vector — must not block staging of the
    # first (compute drops vectorless nodes, staging keeps them for parity
    # with the emitted event's node list).
    return rows


def test_rl2b_code_emit_stages_pending_file_with_code_ctx(monkeypatch, tmp_path):
    """The shared emit home stages a drain-owned pending file when survivors
    carry vectors: same task_id as the retrieval event, retrieval_kind=code,
    active_model = CODE model, source=hook (no in-process monitor)."""
    import importlib

    srv = importlib.import_module("weaviate_mcp.server")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("VCT_SESSION_ID", "sess-code")
    monkeypatch.setenv("CODE_EMBED_MODEL", "sample-code-model")
    captured: list = []
    monkeypatch.setattr(
        srv, "_get_rl_telemetry_writer_for", lambda *a, **k: _writer(captured)
    )
    monkeypatch.setattr(srv, "_get_embedding_service", lambda: None)
    monkeypatch.setattr(srv, "_try_resolve_project_config", lambda: None)

    ok = srv._emit_code_retrieval_telemetry(
        query="where are batches validated?",
        query_emb=[0.1] * 16,
        survivors=_code_survivors_with_vectors(),
        limit=2,
        slot="codesage_embed",
        task_type="code_search",
        task_id="task-code-pair",
    )
    assert ok is True

    # Retrieval event carries the SAME task_id + the per-node vector.
    # v0.2.73 n_emb payload-dedup: the WRITTEN event serializes the node vector
    # ONCE, under `emb` (the field the offline trainer reads) — promoted from
    # the code path's `n_emb`-only candidate shape, so these code retrievals
    # become trainable. The in-process citation-cache ctx (asserted below on the
    # pending file) still carries `n_emb` for the online /rl_update RPC + cosine.
    event = _payload(captured[0])
    assert event["task_id"] == "task-code-pair"
    assert event["nodes"][0]["emb"] == [1.0] + [0.0] * 15
    assert "n_emb" not in event["nodes"][0]
    assert "emb" not in event["nodes"][1]
    assert "n_emb" not in event["nodes"][1]

    # Pending file staged for the drain.
    pend_dir = tmp_path / ".claude" / "state" / "rl_pending"
    files = list(pend_dir.glob("sess-code__task-code-pair.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["source"] == "hook"
    assert payload["seq"] is None
    assert payload["query"] == "where are batches validated?"
    ctx = payload["ctx"]
    assert ctx["retrieval_kind"] == "code"
    assert ctx["embedding_source"] == "codesage"
    assert ctx["active_model"] == "sample-code-model"
    assert ctx["session_id"] == "sess-code"
    assert ctx["nodes"][0]["title"] == "alpha.processing.process_batch"
    assert ctx["nodes"][0]["n_emb"] == [1.0] + [0.0] * 15


def test_rl2b_code_emit_without_vectors_stages_nothing(monkeypatch, tmp_path):
    """Vectorless survivors (the CLI shape today) emit the retrieval event but
    stage NO pending file — a ctx that can never cite would only feed TTL."""
    import importlib

    srv = importlib.import_module("weaviate_mcp.server")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    captured: list = []
    monkeypatch.setattr(
        srv, "_get_rl_telemetry_writer_for", lambda *a, **k: _writer(captured)
    )
    monkeypatch.setattr(srv, "_get_embedding_service", lambda: None)

    ok = srv._emit_code_retrieval_telemetry(
        query="where are batches validated?",
        query_emb=[0.1] * 16,
        survivors=_code_survivors(),
        limit=2,
        slot="codesage_embed",
    )
    assert ok is True
    pend_dir = tmp_path / ".claude" / "state" / "rl_pending"
    assert not list(pend_dir.glob("*.json")) if pend_dir.exists() else True


def test_rl2b_mcp_fetch_requests_vectors_with_candidates():
    """search_code_graph fetches per-candidate vectors in the SAME near_vector
    query (include_vector) — no second round-trip, no CLI divergence risk."""
    import importlib
    import inspect

    srv = importlib.import_module("weaviate_mcp.server")
    tool_fn = getattr(srv.search_code_graph, "fn", None) or srv.search_code_graph
    mcp_src = inspect.getsource(tool_fn)
    assert 'kwargs["include_vector"]' in mcp_src
    assert '"n_emb"' in mcp_src


def test_rl2b_compute_citation_code_ctx_embeds_code_space(monkeypatch):
    """A retrieval_kind=code ctx must embed the answer via embed_code (NOT
    embed_text) and write via the code-slot writer (NOT the active-text
    writer)."""
    import importlib

    # citation_compute lazy-imports the CANONICAL module path — patch THAT
    # module object (the bare "weaviate_mcp.server" import is a distinct
    # module identity under the tests' dual sys.path setup).
    srv = importlib.import_module("claude_mcp_servers.weaviate_mcp.server")
    from claude_mcp_servers.rl_client.citation_compute import compute_citation

    embed_code_calls: list = []

    class _Svc:
        def embed_code(self, text):
            embed_code_calls.append(text)
            return [1.0] + [0.0] * 15

        def embed_text(self, text):
            raise AssertionError("code ctx must NOT use the text embedder")

    writer_factory_args: list = []
    citations_written: list = []

    class _W:
        def log_citations(self, **kwargs):
            citations_written.append(kwargs)

    def _factory(source, *, embedding_dim=0, embedding_model=""):
        writer_factory_args.append((source, embedding_dim, embedding_model))
        return _W()

    def _text_writer_forbidden():
        raise AssertionError("code ctx must NOT use the active-text writer")

    monkeypatch.setattr(srv, "_get_embedding_service", lambda: _Svc())
    monkeypatch.setattr(srv, "_get_rl_telemetry_writer_for", _factory)
    monkeypatch.setattr(srv, "_get_rl_telemetry_writer", _text_writer_forbidden)

    ctx = {
        "nodes": [
            {"title": "alpha.processing.process_batch", "n_emb": [1.0] + [0.0] * 15},
            {"title": "alpha.util.helper", "n_emb": [0.0, 1.0] + [0.0] * 14},
        ],
        "retrieval_kind": "code",
        "active_model": "sample-code-model",
        "embedding_source": "codesage",
        "embedding_dim": 16,
        "task_type": "code_search",
        "session_id": "sess-code",
    }
    result = compute_citation(
        "task-code-cite",
        "the answer mentions process_batch in alpha/processing explicitly",
        ctx,
        write=True,
    )
    assert result is not None
    assert embed_code_calls, "answer chunks were not embedded via embed_code"
    assert writer_factory_args == [("codesage", 16, "sample-code-model")]
    assert citations_written
    kw = citations_written[0]
    assert kw["task_id"] == "task-code-cite"
    assert kw["task_type"] == "code_search"
    assert kw["session_id"] == "sess-code"
    # Same-space cosine: node 0's vector matches the answer embedding.
    assert result["cosine_sims"]["alpha.processing.process_batch"] == pytest.approx(1.0)


def test_rl2b_compute_citation_kg_ctx_unchanged(monkeypatch):
    """A ctx WITHOUT the code marker keeps the pre-RL-2b path byte-identical:
    embed_text + the active-text writer."""
    import importlib

    srv = importlib.import_module("claude_mcp_servers.weaviate_mcp.server")
    from claude_mcp_servers.rl_client.citation_compute import compute_citation

    class _Svc:
        def embed_text(self, text):
            return [1.0] + [0.0] * 15

        def embed_code(self, text):
            raise AssertionError("KG ctx must NOT use the code embedder")

    citations_written: list = []

    class _W:
        def log_citations(self, **kwargs):
            citations_written.append(kwargs)

    def _code_writer_forbidden(*a, **k):
        raise AssertionError("KG ctx must NOT use the code-slot writer factory")

    monkeypatch.setattr(srv, "_get_embedding_service", lambda: _Svc())
    monkeypatch.setattr(srv, "_get_rl_telemetry_writer", lambda: _W())
    monkeypatch.setattr(srv, "_get_rl_telemetry_writer_for", _code_writer_forbidden)

    ctx = {
        "nodes": [{"title": "NodeA", "n_emb": [1.0] + [0.0] * 15}],
        "active_model": "sample-embed",
        "task_type": "mcp_interactive",
    }
    result = compute_citation("task-kg", "answer that cites NodeA", ctx, write=True)
    assert result is not None
    assert citations_written


# ---------------------------------------------------------------------------
# query_code_structure telemetry (uniform code-tool coverage; no rerank, no
# citation — structural edges are not ranked candidates)
# ---------------------------------------------------------------------------


def test_structure_emit_builds_structural_event(monkeypatch):
    import importlib

    srv = importlib.import_module("weaviate_mcp.server")
    captured: list = []
    monkeypatch.setattr(
        srv, "_get_rl_telemetry_writer_for", lambda *a, **k: _writer(captured)
    )
    monkeypatch.setattr(srv, "_get_embedding_service", lambda: None)
    ok = srv._emit_code_structure_telemetry(
        query_type="callers",
        target="alpha.processing.process_batch",
        results=[
            {"full_name": "alpha.api.handler", "file_path": "src/alpha/api.py"},
            {"path": "src/alpha/util.py"},
            {"composed_class": "BatchValidator"},
        ],
        truncated=False,
    )
    assert ok is True
    event = _payload(captured[0])
    assert event["task_type"] == "code_structure"
    assert event["rl_used"] is False
    assert event["query"] == "callers:alpha.processing.process_batch"
    assert event.get("query_emb") in (None, [])
    ex = event["extras"]
    assert ex["retrieval_kind"] == "code_structure"
    assert ex["query_type"] == "callers"
    assert ex["target"] == "alpha.processing.process_batch"
    assert ex["result_count"] == 3
    assert ex["truncated"] is False
    n = event["nodes"]
    # Identity fallbacks: full_name > name > path > composed_class > ...
    assert [r["title"] for r in n] == [
        "alpha.api.handler", "src/alpha/util.py", "BatchValidator",
    ]
    assert all(r["tier"] == "structural" for r in n)
    assert all(r["score"] == 0.0 for r in n)
    assert n[0]["file_path"] == "src/alpha/api.py"
    assert [r["shown_rank"] for r in n] == [0, 1, 2]


def test_structure_emit_zero_results_still_emits(monkeypatch):
    """A structural MISS (0 results) is a query-distribution signal."""
    import importlib

    srv = importlib.import_module("weaviate_mcp.server")
    captured: list = []
    monkeypatch.setattr(
        srv, "_get_rl_telemetry_writer_for", lambda *a, **k: _writer(captured)
    )
    monkeypatch.setattr(srv, "_get_embedding_service", lambda: None)
    ok = srv._emit_code_structure_telemetry(
        query_type="path",
        target="alpha.a->alpha.b",
        results=[],
    )
    assert ok is True
    event = _payload(captured[0])
    assert event["extras"]["result_count"] == 0
    assert event["nodes"] == []


def test_structure_emit_soft_fails_on_writer_error(monkeypatch):
    import importlib

    srv = importlib.import_module("weaviate_mcp.server")

    def _boom(*a, **k):
        raise RuntimeError("writer construction exploded")

    monkeypatch.setattr(srv, "_get_rl_telemetry_writer_for", _boom)
    monkeypatch.setattr(srv, "_get_embedding_service", lambda: None)
    ok = srv._emit_code_structure_telemetry(
        query_type="callers", target="alpha.fn", results=[{"full_name": "x"}],
    )
    assert ok is False  # soft-fail, no raise


def test_structure_tool_calls_the_emit_home():
    """query_code_structure routes through _emit_code_structure_telemetry at
    BOTH success exits (common chokepoint + path-not-found early return)."""
    import importlib
    import inspect

    srv = importlib.import_module("weaviate_mcp.server")
    tool_fn = getattr(srv.query_code_structure, "fn", None) or srv.query_code_structure
    src = inspect.getsource(tool_fn)
    assert src.count("_emit_code_structure_telemetry(") == 2
    # Structural lookups stay OUT of the citable-tool set (no candidates).
    from claude_mcp_servers.rl_client.answer_window import KG_SEARCH_TOOLS

    assert "query_code_structure" not in KG_SEARCH_TOOLS


# ---------------------------------------------------------------------------
# v0.2.73 telemetry-dedup — per-node serialization consolidation
# (Opus reviewer-fixer 2026-07-04). Guards the extraction of the shared
# `serialize_node_record` helper: proves the two migrated builders emit
# byte-identical per-node records and cannot silently drift from each other.
# ---------------------------------------------------------------------------

# One rich node exercising every per-node field + coercion path.
_DEDUP_RICH_NODE = {
    "title": 42,  # int -> str coercion
    "score": 0.876543210,  # full precision preserved (hub side does NOT round)
    "tier": "extra_reference",
    "emb": [0.123456, 0.987654321],
    "n_emb": [0.5555559, 0.3333333],
    "linked_embs": [[0.11119, 0.2222], [], [0.9]],  # empty inner filtered out
    "linked_type_names": ["Concept", 7],
    "node_type": "code",
    "links": [f"L{i}" for i in range(15)],  # >10 -> truncated to 10
    "cos_qn": 0.333333,
    "cos_ql": 0.0,  # falsy-but-present -> emitted (is-not-None guard)
    "cos_nl": 0.9999,
    "shown_rank": 3,
    "chunks_matched": "5",  # str -> int coercion
    "best_chunk_number": 2,
    "collection": "CodeFunction",
    "file_path": "a/b.py",
    "rerank_score": 0.77,
    "boost_delta": -0.05,
    "boost_signals": {"lex": 1, "sem": 2},
}


def test_dedup_queue_builder_per_node_record_is_leaner_surface():
    """`_build_retrieval_payload` (queue) — the LEANER surface (my lane, the
    field GATING via flags): the queue payload deliberately OMITS `links`, the
    RL-2 code-path fields, and `best_chunk_number` (which only the v3 event
    carries), while keeping full-precision scalars and the RL-1/RL-7 rank+count
    fields. This asserts the flag-driven omissions I own — NOT the exact field
    list (the presence/precision of `emb`/`n_emb` is the payload-content lane's
    contract and may change independently)."""
    w = _writer([])
    rec = w._build_retrieval_payload(
        task_id="t", task_type="x", query="q",
        nodes=[dict(_DEDUP_RICH_NODE)], session_id="s", query_emb=None,
    )["nodes"][0]
    # Coercions + full precision (hub side does NOT round scalars).
    assert rec["title"] == "42"
    assert rec["score"] == 0.876543210
    assert rec["cos_qn"] == 0.333333
    assert rec["chunks_matched"] == 5  # str -> int
    assert rec["shown_rank"] == 3
    # Flag-driven omissions owned by this lane (include_* = False here).
    assert "links" not in rec
    assert "best_chunk_number" not in rec
    assert "collection" not in rec
    assert "rerank_score" not in rec
    assert "boost_signals" not in rec


def test_dedup_v3_builder_per_node_record_is_superset_surface():
    """`_build_v3_retrieval_event` (launcher.db) — the SUPERSET surface (my
    lane): adds `links` (truncated to 10, str-coerced), `best_chunk_number`,
    and the RL-2 code-path fields the queue omits, at full precision. Asserts
    the flag-driven ADDITIONS I own, not the exact field list."""
    w = _writer([])
    rec = w._build_v3_retrieval_event(
        task_id="t", task_type="x", query="q",
        nodes=[dict(_DEDUP_RICH_NODE)], session_id="s", query_emb=None,
    )["nodes"][0]
    assert rec["links"] == [f"L{i}" for i in range(10)]  # truncated to 10
    assert rec["best_chunk_number"] == 2
    assert rec["collection"] == "CodeFunction"
    assert rec["rerank_score"] == 0.77
    assert rec["boost_delta"] == -0.05
    assert rec["boost_signals"] == {"lex": 1, "sem": 2}  # dict shape verbatim
    # Insertion order: `links` sits between `node_type` and the cos_* block
    # (the hub-side order the shared helper fixes).
    keys = list(rec.keys())
    assert keys.index("node_type") < keys.index("links") < keys.index("cos_qn")
    # best_chunk_number follows chunks_matched; code-path fields come last.
    assert keys.index("chunks_matched") < keys.index("best_chunk_number")
    assert keys.index("best_chunk_number") < keys.index("collection")


def test_dedup_v3_is_strict_superset_of_queue_shared_fields():
    """For the SAME node, every field the queue builder emits appears
    identically (key + value) in the v3 event — proving they share one
    serialization and the v3 event only ADDS fields, never diverges."""
    w = _writer([])
    node = dict(_DEDUP_RICH_NODE)
    q_rec = w._build_retrieval_payload(
        task_id="t", task_type="x", query="q", nodes=[dict(node)],
        session_id="s", query_emb=None,
    )["nodes"][0]
    v3_rec = w._build_v3_retrieval_event(
        task_id="t", task_type="x", query="q", nodes=[dict(node)],
        session_id="s", query_emb=None,
    )["nodes"][0]
    for k, v in q_rec.items():
        assert k in v3_rec, f"v3 event dropped queue field {k!r}"
        assert v3_rec[k] == v, f"field {k!r} diverged: {v!r} vs {v3_rec[k]!r}"


def test_dedup_builders_route_through_shared_helper():
    """Drift guard: both migrated builders call `serialize_node_record`, so a
    future edit to per-node shape lands in ONE place, not three copies."""
    import inspect

    from claude_mcp_servers.rl_client import telemetry_writer as tw

    for meth in (tw.RLTelemetryWriter._build_retrieval_payload,
                 tw.RLTelemetryWriter._build_v3_retrieval_event):
        src = inspect.getsource(meth)
        assert "serialize_node_record(" in src, (
            f"{meth.__name__} no longer routes through the shared helper"
        )


# ---------------------------------------------------------------------------
# n_emb payload-dedup (v0.2.73) — the WRITTEN retrieval event serializes the
# node vector ONCE, under `emb` (the field the offline trainer reads). No
# offline consumer reads a stored `n_emb`, and the KG enrichment mirrors the
# SAME vector into both keys — so pre-dedup every KG node's 1024-dim vector was
# written TWICE (~50% dead bytes). These pin the single-write contract across
# all three serialization surfaces + the offline-trainer field it targets.
# ---------------------------------------------------------------------------


def test_nemb_dedup_kg_node_writes_vector_once_under_emb():
    """A KG node carrying the SAME vector under both `emb` and `n_emb` (the
    enrichment shape) serializes ONCE under `emb`; the redundant `n_emb` copy is
    dropped from the queue payload AND the v3 event."""
    from claude_mcp_servers.rl_client.rl_logger import serialize_node_record

    kg_node = {"title": "N", "score": 0.5, "tier": "top_k",
               "emb": [0.11, 0.22], "n_emb": [0.11, 0.22]}
    rec = serialize_node_record(dict(kg_node))
    assert rec["emb"] == [0.11, 0.22]
    assert "n_emb" not in rec

    w = _writer([])
    for build in (w._build_retrieval_payload, w._build_v3_retrieval_event):
        node = build(task_id="t", task_type="x", query="q",
                     nodes=[dict(kg_node)], session_id="s", query_emb=None)["nodes"][0]
        assert node["emb"] == [0.11, 0.22]
        assert "n_emb" not in node


def test_nemb_dedup_code_node_promotes_nemb_only_vector_to_emb():
    """A code retrieval attaches the vector as `n_emb`-only. The written event
    promotes it to `emb` so the offline trainer (which reads `emb`) can consume
    it — pre-dedup these events were silently skipped by the trainer."""
    from claude_mcp_servers.rl_client.rl_logger import serialize_node_record

    code_node = {"title": "mod.fn", "score": 0.7, "tier": "top_k",
                 "n_emb": [1.0, 0.0, 0.0]}
    rec = serialize_node_record(dict(code_node))
    assert rec["emb"] == [1.0, 0.0, 0.0]
    assert "n_emb" not in rec


def test_nemb_dedup_legacy_jsonl_writer_dedups_too():
    """The legacy JSONL `RLDataLogger.log_retrieval` loop (kept separate for
    field-order reasons) applies the SAME dedup: one `emb`, no `n_emb`."""
    import tempfile

    from claude_mcp_servers.rl_client.rl_logger import RLDataLogger

    with tempfile.TemporaryDirectory() as td:
        log = RLDataLogger(log_path=Path(td) / "ev.jsonl", project="P",
                           embedding_source="qwen3", embedding_dim=2,
                           embedding_model="m")
        log.log_retrieval(
            task_id="t", task_type="x", query="q",
            nodes=[{"title": "A", "score": 0.9, "tier": "top_k",
                    "emb": [0.3, 0.4], "n_emb": [0.3, 0.4]}],
        )
        event = json.loads((Path(td) / "ev.jsonl").read_text().strip())
        node = event["nodes"][0]
        assert node["emb"] == pytest.approx([0.3, 0.4])
        assert "n_emb" not in node


def test_nemb_dedup_written_event_is_consumable_by_offline_trainer_field():
    """Contract anchor: the offline trainer reads the node vector via
    `node.get("emb")` (paid-modules/vct-rl-reranker/offline_trainer.py
    `extract_samples` + `train_epoch`). Assert the written v3 event exposes the
    vector under exactly that key for both the KG (dual-key) and code
    (n_emb-only) shapes — so nothing the trainer needs is lost by the dedup."""
    w = _writer([])
    for node_in in (
        {"title": "kg", "score": 0.5, "tier": "top_k",
         "emb": [0.1, 0.2], "n_emb": [0.1, 0.2]},
        {"title": "code", "score": 0.6, "tier": "top_k", "n_emb": [0.9, 0.8]},
    ):
        rec = w._build_v3_retrieval_event(
            task_id="t", task_type="x", query="q",
            nodes=[dict(node_in)], session_id="s", query_emb=None,
        )["nodes"][0]
        # The trainer's exact read must find the vector.
        assert rec.get("emb"), "offline trainer's node.get('emb') would be empty"


def test_dedup_helper_round_scalars_flag_matches_legacy_precision():
    """The `round_scalars` flag reproduces the legacy JSONL writer's 4-place
    rounding of `score` + `cos_*` (kept for parity even though no live hub
    caller sets it — the legacy loop stays separate for byte-order reasons)."""
    from claude_mcp_servers.rl_client.rl_logger import serialize_node_record

    unrounded = serialize_node_record(dict(_DEDUP_RICH_NODE), round_scalars=False)
    rounded = serialize_node_record(dict(_DEDUP_RICH_NODE), round_scalars=True)
    assert unrounded["score"] == 0.876543210
    assert rounded["score"] == 0.8765
    assert rounded["cos_qn"] == 0.3333
    # Embeddings are ALWAYS rounded regardless of the flag.
    assert unrounded["emb"] == rounded["emb"] == [0.1235, 0.9877]
