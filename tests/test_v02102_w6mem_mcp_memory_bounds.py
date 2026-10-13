# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""W6-MEM (v0.2.102): memory bounds for the long-lived weaviate-kg MCP.

One weaviate-kg MCP process grew to 6.5 GB of anonymous heap in about two hours
of heavy use. Three causes, three fixes, locked here:

F1  ``rl_client.answer_window`` cached FULLY parsed transcripts, bounded by
    entry count only, re-parsed the whole file on every append, and the monitor
    ran that parse inside the event loop. Now: incremental append-parse (only
    new bytes), re-parse only on truncation / replacement, slim messages, a
    byte bound, and the monitor's load runs in a worker thread.
F2  ``rl_state._rl_node_content_cache`` held 256 entries of float-list vectors
    (5-9 MB each). Now: vectors packed losslessly into ``array`` buffers, plus a
    byte bound; consumers get plain lists back via ``expand_ctx``.
F3  per-search answer monitors ran with unbounded concurrency for up to an hour
    each. Now: capped; a search over the cap is DEFERRED to its pending file
    (computed by the Stop-hook drain) — never dropped, and no running monitor is
    cancelled.

Owner rule proven here: RL training logs (retrieval event + citation) are still
collected under every bound.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from array import array
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_mcp_servers.rl_client import answer_window as aw
from claude_mcp_servers.rl_client import search_pipeline

# Present only on the fixed code; the red-proof assertions below do not depend
# on them (they fall back to identity so the OLD code fails on the behaviour,
# not on an import).
_slim = getattr(aw, "slim_message", lambda m: m)


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# Realistic transcript lines (Claude Code JSONL shape)
# ----------------------------------------------------------------------

def _ts(i: int) -> str:
    return f"2026-10-13T04:{i // 60:02d}:{i % 60:02d}.000Z"


def _kg_call(i: int, query: str) -> dict:
    return {
        "type": "assistant", "timestamp": _ts(i), "uuid": f"u{i}",
        "sessionId": "s1", "isSidechain": False,
        "message": {
            "id": f"msg_{i}", "role": "assistant", "model": "m",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "content": [
                {"type": "thinking", "thinking": "plan the search ✓",
                 "signature": "S" * 400},
                {"type": "tool_use", "id": f"toolu_{i}",
                 "name": "mcp__weaviate-kg__hybrid_search",
                 "input": {"query": query, "limit": 5}},
            ],
        },
    }


def _tool_result(i: int, size: int = 4000) -> dict:
    body = "R" * size
    return {
        "type": "user", "timestamp": _ts(i), "uuid": f"u{i}",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{i - 1}",
             "content": [{"type": "text", "text": body}]},
        ]},
        "toolUseResult": {"stdout": body, "stderr": ""},
    }


def _answer(i: int, text: str) -> dict:
    return {
        "type": "assistant", "timestamp": _ts(i), "uuid": f"u{i}",
        "message": {"id": f"msg_{i}", "role": "assistant", "content": [
            {"type": "text", "text": text},
            {"type": "tool_use", "id": f"toolu_{i}", "name": "Edit",
             "input": {"file_path": "/x/y.py", "old_string": "a", "new_string": "b ünï"}},
        ]},
    }


def _human(i: int, text: str) -> dict:
    return {"type": "user", "timestamp": _ts(i),
            "message": {"role": "user", "content": text}}


def _other(i: int) -> dict:
    return {"type": "file-history-snapshot", "timestamp": _ts(i), "snapshot": {"x": 1}}


def _lines(msgs) -> bytes:
    return b"".join(json.dumps(m).encode() + b"\n" for m in msgs)


def _session(n_rounds: int, start: int = 0, query: str = "alpha") -> list[dict]:
    out: list[dict] = []
    i = start
    for r in range(n_rounds):
        out += [_kg_call(i, f"{query} {r}"), _tool_result(i + 1),
                _answer(i + 2, f"answer {r} " * 30), _other(i + 3)]
        if r % 3 == 2:
            out.append(_human(i + 4, f"next please {r}"))
        i += 5
    return out


def _expected(path: Path) -> list:
    """What the cached loader must return: the full parse, slimmed."""
    return [_slim(m) for m in aw.load_messages(path)]


@pytest.fixture(autouse=True)
def _fresh_transcript_cache(monkeypatch):
    aw._MESSAGE_CACHE.clear()
    monkeypatch.delenv("VCO_RL_TRANSCRIPT_CACHE_MAX_BYTES", raising=False)
    monkeypatch.delenv("VCO_RL_NODE_CACHE_MAX_BYTES", raising=False)
    monkeypatch.delenv("VCO_RL_MONITOR_MAX_CONCURRENT", raising=False)
    yield
    aw._MESSAGE_CACHE.clear()


class _LoadsCounter:
    """Counts json.loads calls made by answer_window (old and new code alike)."""

    def __init__(self):
        self.n = 0
        self._real = aw._json.loads

    def __call__(self, *a, **k):
        self.n += 1
        return self._real(*a, **k)


# ======================================================================
# F1 — incremental, slim, byte-bounded, off-loop transcript cache
# ======================================================================


def test_incremental_append_parse_equals_full_parse_and_parses_only_new_lines(tmp_path, monkeypatch):
    """Every append step: cached result == slimmed full parse, and only the
    APPENDED lines are parsed. RED on the pre-W6-MEM cache, which re-parsed
    the whole file on every change."""
    p = tmp_path / "t.jsonl"
    base = _session(20)
    p.write_bytes(_lines(base))
    assert aw.load_messages_cached(p) == _expected(p)

    counter = _LoadsCounter()
    monkeypatch.setattr(aw._json, "loads", counter)
    for step in range(4):
        new = _session(3, start=1000 + step * 100, query=f"beta{step}")
        with open(p, "ab") as fh:
            fh.write(_lines(new))
        before = counter.n
        got = aw.load_messages_cached(p)
        monkeypatch.setattr(aw._json, "loads", counter._real)
        assert got == _expected(p)
        monkeypatch.setattr(aw._json, "loads", counter)
        assert counter.n - before == len(new), (
            f"step {step}: parsed {counter.n - before} lines for {len(new)} appended"
        )


def test_partial_final_line_matches_full_parse_and_is_completed_later(tmp_path):
    """A writer mid-append leaves half a line: the cache must equal the full
    parse (fragment skipped), then include the line once it is completed."""
    p = tmp_path / "t.jsonl"
    p.write_bytes(_lines(_session(2)))
    aw.load_messages_cached(p)
    line = json.dumps(_answer(500, "the completed line")).encode() + b"\n"
    with open(p, "ab") as fh:
        fh.write(line[:40])
    assert aw.load_messages_cached(p) == _expected(p)
    with open(p, "ab") as fh:
        fh.write(line[40:])
    got = aw.load_messages_cached(p)
    assert got == _expected(p)
    assert got[-1]["message"]["content"][0]["text"] == "the completed line"


def test_complete_json_fragment_without_newline_is_returned_like_full_parse(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_bytes(_lines(_session(1)) + json.dumps(_human(9, "no newline yet")).encode())
    assert aw.load_messages_cached(p) == _expected(p)
    assert aw.load_messages_cached(p)[-1]["message"]["content"] == "no newline yet"


@pytest.mark.parametrize("mode", ["truncate_smaller", "rewrite_same_inode", "replace_new_inode"])
def test_truncation_or_replacement_triggers_full_reparse(tmp_path, mode):
    """Size below the consumed offset, a non-newline byte before the offset, or
    a new inode → the cache re-parses from byte 0 (never splices stale state)."""
    p = tmp_path / "t.jsonl"
    p.write_bytes(_lines(_session(6)))
    aw.load_messages_cached(p)
    old_offset = len(p.read_bytes())  # every line complete -> consumed offset

    if mode == "truncate_smaller":
        p.write_bytes(_lines(_session(1, query="gamma")))
    elif mode == "rewrite_same_inode":
        # Same inode, LONGER content, and the byte just before the old offset
        # is not a newline → must be detected as a rewrite, not an append.
        payload = json.dumps({"type": "user", "pad": "z" * old_offset}).encode() + b"\n"
        with open(p, "r+b") as fh:
            fh.seek(0)
            fh.write(payload + _lines(_session(1, query="delta")))
            fh.truncate()
    else:
        # A new file at the same path whose size >= the old offset and whose
        # byte before the old offset IS a newline ("omega" has as many
        # characters as "alpha", so every line keeps its length): only the
        # inode check sees the replacement.
        repl = tmp_path / "repl.jsonl"
        msgs = _session(6, query="omega")
        repl.write_bytes(_lines(msgs) + _lines(_session(2, start=900, query="zeta")))
        assert repl.read_bytes()[old_offset - 1:old_offset] == b"\n"
        os.replace(repl, p)

    assert aw.load_messages_cached(p) == _expected(p)


def test_slim_messages_drop_tool_results_but_keep_every_position_and_window(tmp_path):
    """The cache keeps only what the consumers read. RED on the old cache,
    which returned the full ``toolUseResult`` / tool_result bodies."""
    from claude_mcp_servers.weaviate_mcp import rl_enrichment as rlmod

    p = tmp_path / "t.jsonl"
    p.write_bytes(_lines(_session(9)))
    full = aw.load_messages(p)
    cached = aw.load_messages_cached(p)

    assert all("toolUseResult" not in m for m in cached)
    assert all("R" * 100 not in json.dumps(m) for m in cached)
    assert all("signature" not in json.dumps(m) for m in cached)
    # Same positions, same windows, same matches, same human-turn answers.
    assert aw.find_kg_positions(cached) == aw.find_kg_positions(full)
    for idx, (mi, bi) in enumerate(aw.find_kg_positions(full)):
        assert aw.extract_answer_window(cached, mi, bi) == aw.extract_answer_window(full, mi, bi)
        assert rlmod._rl_human_turn_after(cached, mi) == rlmod._rl_human_turn_after(full, mi)
        pos = aw.find_kg_positions(full)
        assert aw.match_position_for_query(cached, pos, f"alpha {idx}", idx) == \
            aw.match_position_for_query(full, pos, f"alpha {idx}", idx)
    ts = int(time.mktime(time.strptime("2026-10-13T04:00:20", "%Y-%m-%dT%H:%M:%S")) * 1000)
    assert aw.match_position_by_timestamp(cached, ts) == aw.match_position_by_timestamp(full, ts)


def test_byte_cap_evicts_lru_transcript_but_keeps_the_one_just_served(tmp_path, monkeypatch):
    """ACT: over the byte cap the least-recently-used transcript is evicted.
    RED on the old cache (count-bounded at 32, no byte bound)."""
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_bytes(_lines(_session(10)))
    b.write_bytes(_lines(_session(10, query="other")))
    monkeypatch.setenv("VCO_RL_TRANSCRIPT_CACHE_MAX_BYTES", "1000")
    aw.load_messages_cached(a)
    assert str(a) in aw._MESSAGE_CACHE  # alone and oversized: still kept
    aw.load_messages_cached(b)
    assert str(b) in aw._MESSAGE_CACHE
    assert str(a) not in aw._MESSAGE_CACHE
    # Evicted means re-parsed on the next call — still the right answer.
    assert aw.load_messages_cached(a) == _expected(a)


def test_byte_cap_leaves_transcripts_alone_when_under_the_cap(tmp_path):
    """LEAVE-ALONE: under the (default 256 MiB) cap both entries stay cached."""
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_bytes(_lines(_session(10)))
    b.write_bytes(_lines(_session(10, query="other")))
    aw.load_messages_cached(a)
    aw.load_messages_cached(b)
    assert str(a) in aw._MESSAGE_CACHE and str(b) in aw._MESSAGE_CACHE


def test_monitor_transcript_parse_runs_off_the_event_loop(tmp_path, monkeypatch):
    """The monitor's transcript load must not run on the event-loop thread, and
    the loop must keep serving other coroutines while a slow load runs. RED on
    the old monitor, which called the loader inline in the coroutine."""
    import claude_mcp_servers.weaviate_mcp.rl_enrichment as rlmod
    import claude_mcp_servers.weaviate_mcp.server as srv

    p = tmp_path / "sess.jsonl"
    p.write_bytes(_lines(_session(1)))
    load_threads: list[int] = []
    ticks_during_load: list[int] = []
    ticks = {"n": 0}

    def _slow_load(path):
        load_threads.append(threading.get_ident())
        start = ticks["n"]
        time.sleep(0.25)  # stands in for a multi-second GB-scale parse
        ticks_during_load.append(ticks["n"] - start)
        return aw.load_messages_cached(path)

    async def _fake_find_all():
        return [p]

    async def _ticker():
        while True:
            ticks["n"] += 1
            await asyncio.sleep(0.01)

    async def _main():
        loop_thread = threading.get_ident()
        t = asyncio.create_task(_ticker())
        await rlmod._rl_answer_monitor("tid-offloop", 1, "alpha 0")
        t.cancel()
        return loop_thread

    monkeypatch.setattr(srv, "_RL_MONITOR_POLL_INTERVAL", 0.005, raising=False)
    monkeypatch.setattr(srv, "_RL_MONITOR_TIMEOUT", 0.3, raising=False)
    with patch.object(srv, "_rl_find_all_transcripts", _fake_find_all), \
         patch.object(srv, "_rl_load_messages", _slow_load), \
         patch.object(srv, "_rl_check_force_flush", return_value=False), \
         patch.object(srv, "_rl_node_content_cache", {}):
        loop_thread = _run(_main())

    assert load_threads, "monitor never loaded the transcript"
    assert all(t != loop_thread for t in load_threads), "transcript load ran on the event loop"
    assert ticks_during_load[0] >= 5, (
        f"event loop starved during the load ({ticks_during_load[0]} ticks in 0.25 s)"
    )


def test_cache_is_thread_safe_under_concurrent_appends(tmp_path):
    """The loader now runs in worker threads: concurrent callers during
    appends must never corrupt the entry (final state == full parse)."""
    p = tmp_path / "t.jsonl"
    p.write_bytes(_lines(_session(5)))
    errors: list[BaseException] = []

    def _reader():
        try:
            for _ in range(30):
                aw.load_messages_cached(p)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_reader) for _ in range(4)]
    for t in threads:
        t.start()
    for k in range(10):
        with open(p, "ab") as fh:
            fh.write(_lines(_session(1, start=2000 + k * 10, query=f"c{k}")))
    for t in threads:
        t.join()
    assert not errors
    assert aw.load_messages_cached(p) == _expected(p)


# ======================================================================
# F2 — compact, byte-bounded citation ctx cache
# ======================================================================


def _f32(values):
    """Values exactly representable in float32 (what Weaviate returns)."""
    return array("f", values).tolist()


def _ctx(dim: int = 1024, n_nodes: int = 4) -> dict:
    nodes = []
    for k in range(n_nodes):
        nodes.append({
            "title": f"N{k}", "score": 0.5, "tier": "top_k", "node_type": "concept",
            "emb": _f32([0.001 * (i + k) for i in range(dim)]),
            "n_emb": [0.1 + 1e-12 * i for i in range(dim)],  # NOT float32-exact
            "linked_embs": [_f32([0.01 * i for i in range(dim)]) for _ in range(3)],
            "linked_type_names": ["tool", "concept", "concept"],
            "links": ["A", "B"], "cos_qn": 0.42, "chunks_matched": 2,
            "ints": list(range(12)),  # ints: never packed
        })
    return {
        "nodes": nodes, "query_emb": _f32([0.5] * dim), "active_model": "qwen3",
        "embedding_source": "ollama", "embedding_dim": dim, "project_id": None,
        "project_name": "P", "task_type": "mcp_interactive", "session_id": "s",
        "dual_log": True, "other_query_emb": [0.25 + 1e-13] * dim,
        "other_nodes": [{"title": "N0", "emb": _f32([0.2] * dim), "n_emb": _f32([0.2] * dim)}],
    }


def test_compact_ctx_round_trips_losslessly_and_shrinks():
    from claude_mcp_servers.rl_client.ctx_compact import compact_ctx, expand_ctx
    from claude_mcp_servers.rl_client.memory_bounds import approx_heap_bytes

    ctx = _ctx()
    snapshot = json.dumps(ctx)
    compact = compact_ctx(ctx)
    assert json.dumps(ctx) == snapshot, "compact_ctx must not mutate its input"

    n0 = compact["nodes"][0]
    assert isinstance(n0["emb"], array) and n0["emb"].typecode == "f"
    assert isinstance(n0["n_emb"], array) and n0["n_emb"].typecode == "d"  # exactness kept
    assert all(isinstance(v, array) for v in n0["linked_embs"])
    assert isinstance(n0["ints"], list)
    assert isinstance(compact["other_nodes"][0]["n_emb"], array)

    back = expand_ctx(compact)
    assert back is not None
    assert back == ctx
    assert json.dumps(back) == snapshot  # byte-identical serialisation (pending file / POST)
    assert type(back["nodes"][0]["emb"]) is list
    assert all(type(v) is float for v in back["nodes"][0]["emb"])
    assert approx_heap_bytes(compact) * 3 < approx_heap_bytes(ctx)
    assert expand_ctx(None) is None
    plain = {"nodes": [{"title": "n"}], "query_emb": [0.1]}
    assert expand_ctx(plain) == plain  # never-compacted ctx passes through


def _populate(task_id: str, candidates, query_emb, **kw):
    import claude_mcp_servers.weaviate_mcp.server as srv
    with patch.object(srv, "_try_resolve_project_config", return_value=None):
        search_pipeline._populate_citation_cache(
            task_id=task_id, candidates=candidates, limit=5, query_emb=query_emb,
            embedding_source="ollama", embedding_dim=len(query_emb or []),
            embedding_model="qwen3-embedding:0.6b", task_type="mcp_interactive",
            session_id="sess-w6mem", query="alpha 0", stage_pending_file=True, **kw,
        )


def test_citation_cache_stores_compact_entry_and_monitor_view_is_identical(tmp_path, monkeypatch):
    """The cache entry is packed (RED on the old float-list cache) and
    ``expand_ctx`` restores exactly what the old cache held."""
    import claude_mcp_servers.weaviate_mcp.server as srv
    from claude_mcp_servers.rl_client.ctx_compact import expand_ctx

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    cands = _ctx()["nodes"]
    qemb = _f32([0.3] * 1024)
    with patch.object(srv, "_rl_node_content_cache", {}):
        _populate("tid-compact", cands, qemb)
        entry = srv._rl_node_content_cache["tid-compact"]
        assert isinstance(entry["query_emb"], array)
        assert isinstance(entry["nodes"][0]["n_emb"], array)
        view = expand_ctx(entry)
    assert view is not None
    assert view["query_emb"] == qemb
    assert view["nodes"] == search_pipeline._build_log_nodes(cands, 5)

    # Log collection is not degraded: the durable pending file carries the
    # plain, full-precision vectors.
    from claude_mcp_servers.rl_client.citation_pending import list_pending_for_session, read_pending
    files = list_pending_for_session("sess-w6mem", tmp_path)
    assert len(files) == 1
    staged_payload = read_pending(files[0])
    assert staged_payload is not None
    staged = staged_payload["ctx"]
    assert staged["query_emb"] == qemb
    assert staged["nodes"] == search_pipeline._build_log_nodes(cands, 5)


def test_citation_cache_byte_cap_evicts_oldest_keeps_newest(tmp_path, monkeypatch):
    """ACT: over the byte cap the oldest entries go, the newest stays. RED on
    the old cache (256-entry count bound only)."""
    import claude_mcp_servers.weaviate_mcp.server as srv

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("VCO_RL_NODE_CACHE_MAX_BYTES", "60000")  # ≈ 1.5 entries
    cands = _ctx(dim=1024, n_nodes=2)["nodes"]
    with patch.object(srv, "_rl_node_content_cache", {}), \
         patch.object(srv, "_rl_node_content_cache_sizes", {}, create=True):
        for k in range(4):
            _populate(f"tid-{k}", cands, _f32([0.1] * 1024))
        keys = list(srv._rl_node_content_cache)
        assert keys == ["tid-3"], keys
        assert set(srv._rl_node_content_cache_sizes) == {"tid-3"}
    # Evicted entries are not lost: every search staged its pending file.
    from claude_mcp_servers.rl_client.citation_pending import list_pending_for_session
    assert len(list_pending_for_session("sess-w6mem", tmp_path)) == 4


def test_citation_cache_byte_cap_leaves_entries_alone_under_the_cap(tmp_path, monkeypatch):
    """LEAVE-ALONE: under the default cap nothing is evicted."""
    import claude_mcp_servers.weaviate_mcp.server as srv

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    cands = _ctx(dim=1024, n_nodes=2)["nodes"]
    with patch.object(srv, "_rl_node_content_cache", {}), \
         patch.object(srv, "_rl_node_content_cache_sizes", {}, create=True):
        for k in range(4):
            _populate(f"tid-{k}", cands, _f32([0.1] * 1024))
        assert list(srv._rl_node_content_cache) == [f"tid-{k}" for k in range(4)]


def test_monitor_fire_hands_consumers_plain_lists_from_a_compact_entry(monkeypatch, tmp_path):
    """The monitor expands the compact ctx before the citation compute and the
    /rl_update payload: consumers receive ``list[float]`` with the exact values."""
    import claude_mcp_servers.weaviate_mcp.rl_enrichment as rlmod
    import claude_mcp_servers.weaviate_mcp.server as srv
    from claude_mcp_servers.rl_client.ctx_compact import compact_ctx

    ctx = _ctx(dim=64, n_nodes=2)
    p = tmp_path / "sess.jsonl"
    p.write_bytes(_lines([_kg_call(0, "alpha 0"), _answer(1, "word " * 200)]))
    seen: dict = {}

    async def _compute(task_id, answer, c):
        seen["ctx"] = c
        return {"cosine_sims": {}, "literal_cited": {}}

    async def _fake_find_all():
        return [p]

    monkeypatch.setattr(srv, "_RL_MONITOR_POLL_INTERVAL", 0.005, raising=False)
    monkeypatch.setattr(srv, "_RL_MONITOR_TIMEOUT", 1.0, raising=False)
    monkeypatch.setattr(srv, "_RL_MIN_ANSWER_TOKENS_FOR_CITATION", 1, raising=False)
    with patch.object(srv, "_rl_find_all_transcripts", _fake_find_all), \
         patch.object(srv, "_rl_check_force_flush", return_value=True), \
         patch.object(srv, "_rl_clear_force_flush", return_value=None), \
         patch.object(srv, "_rl_compute_and_write_citations", _compute), \
         patch.object(srv, "_rl_delete_own_pending_file", lambda t: None), \
         patch.object(srv, "_get_rl_client", return_value=None), \
         patch.object(srv, "_rl_node_content_cache", {"tid-fire": compact_ctx(ctx)}):
        _run(rlmod._rl_answer_monitor("tid-fire", 1, "alpha 0"))

    got = seen["ctx"]
    assert type(got["nodes"][0]["n_emb"]) is list
    assert got["nodes"] == ctx["nodes"]
    assert got["query_emb"] == ctx["query_emb"]
    assert got["other_nodes"] == ctx["other_nodes"]


# ======================================================================
# F3 — concurrent-monitor cap defers to the pending file, without loss
# ======================================================================


def _req(**over):
    base = {
        "query": "alpha 0",
        "candidates": [
            {"title": "ConfigDoc", "score": 0.9, "n_emb": _f32([0.2] * 64)},
            {"title": "InstallNotes", "score": 0.7, "n_emb": _f32([0.3] * 64)},
        ],
        "limit": 5, "query_emb": _f32([0.1] * 64), "embedding_source": "ollama",
        "embedding_dim": 64, "embedding_model": "qwen3-embedding:0.6b",
        "task_id": "tid-cap", "task_type": "mcp_interactive",
        "session_id": "sess-cap", "spawn_answer_monitor": True,
    }
    base.update(over)
    return search_pipeline.RerankRequest(**base)


def _drive_search(tmp_path, monkeypatch, *, cap: int, running: int, task_id: str):
    """Run one rerank_and_emit with ``running`` live monitors already present."""
    import claude_mcp_servers.weaviate_mcp.server as srv
    from claude_mcp_servers.weaviate_mcp import rl_state

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("VCO_RL_MONITOR_MAX_CONCURRENT", str(cap))
    started: list[str] = []
    emitted: list[str] = []

    async def _fake_monitor(tid, seq, query):
        started.append(tid)
        await asyncio.sleep(10)

    async def _fake_emit(req, ranked, tid, rl_used):
        emitted.append(tid)
        return True

    async def _inner():
        tasks: set = set()
        existing = [asyncio.create_task(asyncio.sleep(10)) for _ in range(running)]
        tasks.update(existing)
        cache: dict = {}
        with patch.object(srv, "_rl_monitor_tasks", tasks), \
             patch.object(srv, "_rl_answer_monitor", _fake_monitor), \
             patch.object(srv, "_rl_node_content_cache", cache), \
             patch.object(srv, "_try_resolve_project_config", return_value=None), \
             patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False), \
             patch.object(search_pipeline, "_should_capture_citations", return_value=True), \
             patch.object(search_pipeline, "_retrieval_emit_has_consumer", return_value=True), \
             patch.object(search_pipeline, "_emit_retrieval_event", _fake_emit):
            seq_before = rl_state._rl_call_seq
            await search_pipeline.rerank_and_emit(_req(task_id=task_id))
            await asyncio.sleep(0)  # let a spawned monitor start
            seq_after = rl_state._rl_call_seq
            cancelled = sum(1 for t in existing if t.cancelled())
            live = len(tasks)
            in_cache = task_id in cache
        for t in tasks:
            t.cancel()
        return seq_before, seq_after, cancelled, live, in_cache

    seq_before, seq_after, cancelled, live, in_cache = _run(_inner())
    return {
        "started": started, "emitted": emitted, "seq_before": seq_before,
        "seq_after": seq_after, "cancelled": cancelled, "live": live,
        "in_cache": in_cache,
    }


def test_monitor_cap_defers_to_pending_file_and_drain_computes_the_citation(tmp_path, monkeypatch):
    """ACT: at the cap no new monitor starts (RED on the old unbounded spawn),
    no running monitor is cancelled, the retrieval event is still emitted, the
    pending file carries this search's seq + full ctx, and the Stop-hook drain
    computes the citation from it — no loss."""
    out = _drive_search(tmp_path, monkeypatch, cap=2, running=2, task_id="tid-deferred")
    assert out["started"] == [], "a monitor started over the cap"
    assert out["cancelled"] == 0, "a running monitor was cancelled"
    assert out["live"] == 2
    assert out["emitted"] == ["tid-deferred"], "retrieval log must still be collected"
    assert out["seq_after"] == out["seq_before"] + 1, "seq must advance for the drain's tiebreak"
    assert out["in_cache"] is False, "a deferred search needs no in-memory entry"

    from claude_mcp_servers.rl_client.citation_pending import list_pending_for_session, read_pending
    files = list_pending_for_session("sess-cap", tmp_path)
    assert len(files) == 1
    payload = read_pending(files[0])
    assert payload is not None
    assert payload["seq"] == out["seq_after"]
    assert payload["source"] == "mcp"
    assert payload["ctx"]["query_emb"] == _f32([0.1] * 64)

    # The Stop-hook drain recovers it.
    from claude_mcp_servers.scripts.rl_drain_citations import drain_session
    transcript = tmp_path / "parent.jsonl"
    transcript.write_bytes(_lines(
        [_kg_call(0, "x")] * (out["seq_after"] - 1)
        + [_kg_call(1, "alpha 0"), _answer(2, "the real answer " * 50)]
    ))
    computed: list = []

    def _compute(task_id, answer, ctx, write=True):
        computed.append((task_id, ctx))
        return {"cosine_sims": {}, "literal_cited": {}}

    summary = drain_session(
        "sess-cap", str(transcript), project_root=str(tmp_path),
        compute_fn=_compute, token_count_fn=lambda t: 10 ** 6,
    )
    assert summary["computed"] == 1
    assert computed[0][0] == "tid-deferred"
    assert computed[0][1]["nodes"][0]["n_emb"] == _f32([0.2] * 64)
    assert list_pending_for_session("sess-cap", tmp_path) == []


def test_monitor_cap_leaves_spawn_alone_below_the_cap(tmp_path, monkeypatch):
    """LEAVE-ALONE: below the cap the monitor starts and the ctx is cached."""
    out = _drive_search(tmp_path, monkeypatch, cap=3, running=2, task_id="tid-spawned")
    assert out["started"] == ["tid-spawned"]
    assert out["cancelled"] == 0
    assert out["in_cache"] is True
    assert out["emitted"] == ["tid-spawned"]


@pytest.mark.parametrize("raw,expected", [
    ("", 64), ("abc", 64), ("0", 64), ("-5", 64), ("7", 7),
])
def test_bounds_fall_back_to_defaults_on_bad_values(monkeypatch, raw, expected):
    from claude_mcp_servers.rl_client import memory_bounds as mb
    monkeypatch.setenv(mb.MONITOR_MAX_CONCURRENT_ENV, raw)
    assert mb.monitor_max_concurrent() == expected
