# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 RL-STORE (audit A-014, HOOKS-04, MCPREAD-05): what an rl_events
row costs, and what reads it back.

* A stored vector is a compact half-float string (``vco_lib.rl_vector_codec``),
  not decimal JSON text: a retrieval row is a third of its old size, and the
  trainer's loader (and the training export, and the trainability check)
  read both forms.
* A cache-warmer run posts no retrieval event; the events are held beside the
  cache entry and sent, under the viewing run's session, when the entry is
  shown.
* The trainer's hub client pages newest-first with a keyset cursor and keeps
  the newest ``max_events`` rows.

The retention pass (archive verified by row count AND checksum before its
delete, batches that keep up, incremental vacuum) is Rust and is pinned in
``vct-launcher-core/src/db/rl_events.rs``'s tests.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vco_lib.rl_vector_codec import (
    PREFIX,
    decode_event_vectors,
    decode_vector,
    encode_event_vectors,
    encode_vector,
    sql_vector_present,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _vec(dim: int, seed: int) -> list:
    rnd = random.Random(seed)
    return [rnd.gauss(0.0, 0.03) for _ in range(dim)]


# --- 1. the codec ------------------------------------------------------------


def test_a_vector_round_trips_within_half_float_precision() -> None:
    v = _vec(1024, 1)
    enc = encode_vector(v)
    assert isinstance(enc, str) and enc.startswith(PREFIX)
    back = decode_vector(enc)
    assert len(back) == 1024
    assert max(abs(a - b) for a, b in zip(v, back)) < 1e-4


def test_a_list_passes_through_and_a_non_vector_is_left_alone() -> None:
    assert decode_vector([0.5, 0.25]) == [0.5, 0.25]
    assert decode_vector(None) is None
    assert decode_vector("plain text") == "plain text"


def test_a_damaged_vector_raises_instead_of_decoding_wrong() -> None:
    with pytest.raises(ValueError):
        decode_vector(PREFIX + "not*base64")
    with pytest.raises(ValueError):
        decode_vector(PREFIX + "AAAA")  # 3 bytes: not whole half floats


def test_a_value_half_floats_cannot_hold_stays_a_list() -> None:
    assert encode_vector([1e6, 0.1]) == [1e6, 0.1]
    assert encode_vector([math.inf]) == [math.inf]


def test_event_codec_covers_every_vector_field() -> None:
    ev = {
        "query_emb": [0.5, 0.25],
        "answer_chunk_embs": [[0.5], [0.25]],
        "nodes": [{"title": "A", "emb": [0.5], "linked_embs": [[0.25], [0.5]],
                   "score": 0.9, "cos_qn": 0.1}],
        "cosine_sims": {"A": 0.5},
    }
    enc = encode_event_vectors(json.loads(json.dumps(ev)))
    assert enc["query_emb"].startswith(PREFIX)
    assert all(v.startswith(PREFIX) for v in enc["answer_chunk_embs"])
    assert enc["nodes"][0]["emb"].startswith(PREFIX)
    assert all(v.startswith(PREFIX) for v in enc["nodes"][0]["linked_embs"])
    assert enc["nodes"][0]["score"] == 0.9 and enc["cosine_sims"] == {"A": 0.5}
    assert decode_event_vectors(json.loads(json.dumps(enc))) == ev


# --- 2. the writer stores no decimal vectors ---------------------------------


def _retrieval_nodes(n_nodes: int, dim: int) -> list:
    return [
        {"title": f"N{i}", "score": 0.8, "tier": "top_k", "emb": _vec(dim, 10 + i),
         "linked_embs": [_vec(dim, 100 + i * 10 + k) for k in range(5)],
         "linked_type_names": ["concept"] * 5, "node_type": "concept"}
        for i in range(n_nodes)
    ]


def _envelope(monkeypatch, nodes, query_emb):
    from claude_mcp_servers.rl_client import telemetry_writer as tw

    monkeypatch.delenv("RL_LOCAL_LOGGING_DISABLED", raising=False)
    monkeypatch.delenv("RL_LOCAL_LOGGING_DISABLED_GLOBAL", raising=False)
    monkeypatch.setattr(tw, "_upload_consent_granted", lambda: False)
    posted: list = []
    w = tw.RLTelemetryWriter(project="p", project_id="pid", embedding_source="qwen3",
                             embedding_dim=len(query_emb), embedding_model="m",
                             hub_post_fn=posted.append)
    monkeypatch.setattr(w, "_maybe_prune_rl_events", lambda: None)
    w.log_retrieval(task_id="t1", task_type="search", query="q", nodes=nodes,
                    session_id="s", query_emb=query_emb)
    assert len(posted) == 1
    return posted[0]


def test_a_retrieval_row_carries_no_decimal_vector_and_is_a_third_of_the_size(
        monkeypatch) -> None:
    from claude_mcp_servers.rl_client.rl_logger import _round_emb, serialize_node_record

    dim = 1024
    nodes = _retrieval_nodes(4, dim)
    q = _vec(dim, 7)
    env = _envelope(monkeypatch, [dict(n) for n in nodes], q)
    payload = json.loads(env["payload_json"])
    assert isinstance(payload["query_emb"], str) and payload["query_emb"].startswith(PREFIX)
    for n in payload["nodes"]:
        assert isinstance(n["emb"], str) and n["emb"].startswith(PREFIX)
        assert all(isinstance(v, str) for v in n["linked_embs"])
    assert env["embedding_dim"] == dim, "the measured dim survives the encoding"

    # The bytes the row used to cost: the same event with decimal vectors.
    old = dict(payload)
    old["query_emb"] = _round_emb(q)
    old["nodes"] = [serialize_node_record(n, include_links=True, include_shown_rank=True,
                                          include_chunks_matched=True,
                                          include_best_chunk_number=True,
                                          include_code_path_fields=True) for n in nodes]
    new_bytes, old_bytes = len(env["payload_json"]), len(json.dumps(old))
    assert new_bytes < 0.4 * old_bytes, (new_bytes, old_bytes)

    # And it decodes back to what was logged.
    back = decode_event_vectors(payload)
    assert max(abs(a - b) for a, b in zip(back["query_emb"], q)) < 2e-4
    assert len(back["nodes"][0]["linked_embs"]) == 5


def test_a_citation_row_stores_its_answer_vectors_compact(monkeypatch) -> None:
    from claude_mcp_servers.rl_client import telemetry_writer as tw

    monkeypatch.delenv("RL_LOCAL_LOGGING_DISABLED", raising=False)
    monkeypatch.delenv("RL_LOCAL_LOGGING_DISABLED_GLOBAL", raising=False)
    monkeypatch.setattr(tw, "_upload_consent_granted", lambda: False)
    posted: list = []
    w = tw.RLTelemetryWriter(project="p", project_id="pid", embedding_source="qwen3",
                             embedding_dim=8, embedding_model="m", hub_post_fn=posted.append)
    w.log_citations(task_id="t1", task_type="search", citations={"A": True},
                    cosine_sims={"A": 0.9}, session_id="s",
                    answer_chunk_embs=[_vec(8, 1), _vec(8, 2)], answer_chunk_hashes=["h1", "h2"])
    payload = json.loads(posted[0]["payload_json"])
    assert all(v.startswith(PREFIX) for v in payload["answer_chunk_embs"])
    assert payload["cosine_sims"] == {"A": 0.9}


# --- 3. the readers decode both forms ----------------------------------------


def _row(payload: dict, rid: int = 1) -> dict:
    return {"id": rid, "event_type": payload.get("event", "retrieval"), "schema_version": 3,
            "ts_ms": 1_700_000_000_000 + rid, "project_id": "pid", "embedding_source": "qwen3",
            "payload_json": json.dumps(payload)}


def test_the_trainer_loader_decodes_compact_rows_and_keeps_list_rows() -> None:
    sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))
    from rl_reranker.hub_event_loader import _reshape_hub_row

    ev = {"event": "retrieval", "schema_version": 3, "query_emb": [0.5, 0.25],
          "nodes": [{"title": "A", "emb": [0.25, 0.5], "linked_embs": [[0.5, 0.5]]}]}
    compact = encode_event_vectors(json.loads(json.dumps(ev)))
    assert _reshape_hub_row(_row(compact))["nodes"][0]["emb"] == [0.25, 0.5]
    assert _reshape_hub_row(_row(compact))["query_emb"] == [0.5, 0.25]
    assert _reshape_hub_row(_row(ev))["nodes"][0]["linked_embs"] == [[0.5, 0.5]]
    damaged = dict(compact, query_emb=PREFIX + "%%%")
    assert _reshape_hub_row(_row(damaged)) is None, "a damaged vector skips the row"


def test_the_trainability_check_counts_compact_vectors_as_present() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (p TEXT)")
    for doc in ({"q": encode_vector([0.5])}, {"q": [0.5]}, {"q": []}, {"q": "x"}, {}):
        conn.execute("INSERT INTO t VALUES (?)", (json.dumps(doc),))
    got = [r[0] for r in conn.execute(
        f"SELECT CASE WHEN {sql_vector_present('p', '$.q')} THEN 1 ELSE 0 END FROM t")]
    assert got == [1, 1, 0, 0, 0]


# --- 4. a warmer run posts no retrieval event --------------------------------


EMITTING_STUB = """\
import os, sys
def main():
    sys.path.insert(0, os.environ["VCO_TEST_REPO"])
    from claude_mcp_servers.rl_client.citation_pending import stage_pending
    from claude_mcp_servers.rl_client import search_pipeline
    from claude_mcp_servers.rl_client.deferred_emit import DeferredEmit
    from claude_mcp_servers.rl_client.telemetry_emit import RetrievalEvent
    stage_pending(session_id="warm-sess", task_id="prompt_t", seq=None, query="q",
                  ctx={"nodes": [{"title": "Stub Node", "n_emb": [0.1]}],
                       "active_model": "m", "task_type": "prompt_kg_search"},
                  source="hook")
    ev = RetrievalEvent(query="q", query_emb=[0.5, 0.25], embedding_source="qwen3",
                        embedding_dim=2, embedding_model="m",
                        nodes=[{"title": "Stub Node", "score": 0.9}],
                        task_id="prompt_t", task_type="prompt_kg_search",
                        session_id="warm-sess")
    params = {"project": "p", "project_id": None, "embedding_source": "qwen3",
              "embedding_dim": 2, "embedding_model": "m"}
    print("KG: Stub Node | concept | score=0.90 | TITLES | src=knowledge/stub.md")
    sys.stdout.flush()
    result = search_pipeline.hand_off_deferred((DeferredEmit(ev, None, params),))
    with open(os.environ["VCO_TEST_HANDOFF"], "w") as fh:
        fh.write(str(result))
"""


def _warm_emitting(tmp_path: Path, proj: Path, key: str):
    from tests.common.child_env import child_env
    from tests.test_v02105_router_hard_deadline import ROUTER

    stub = tmp_path / "kg_emit_stub.py"
    stub.write_text(EMITTING_STUB)
    spec = {"project_root": str(proj), "legs": {
        "kg": {"script": str(stub), "argv": ["q", "--limit", "3"], "cache_key": key}}}
    env = child_env(CLAUDE_PROJECT_DIR=str(proj), PYTHONPATH=str(REPO_ROOT),
                    VCO_TEST_REPO=str(REPO_ROOT), WEAVIATE_URL="http://127.0.0.1:9",
                    VCT_STATE_DIR=str(tmp_path / "vct-state"), RL_HUB_POST_DISABLED="1",
                    VCO_TEST_HANDOFF=str(tmp_path / "handoff.txt"))
    for k in ("VCT_SESSION_ID", "VCO_RL_NO_TRANSCRIPT"):
        env.pop(k, None)
    return subprocess.run([sys.executable, str(ROUTER), "--warm-legs"],
                          input=json.dumps(spec), capture_output=True, text=True,
                          timeout=120, cwd=str(proj), env=env)


def _held(proj: Path, key: str) -> Path:
    return proj / ".claude" / "state" / "rl_warm_held" / f"{key}.json"


def test_a_warmer_run_holds_its_retrieval_events_instead_of_posting(tmp_path: Path) -> None:
    from tests.test_v02105_router_hard_deadline import ROUTER, _project

    sys.path.insert(0, str(ROUTER.parent))
    import hook_context_router as hcr  # pyright: ignore[reportMissingImports]

    proj = _project(tmp_path)
    key = hcr.cache_key("kgi", "prompt", "q", "3")
    r = _warm_emitting(tmp_path, proj, key)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "handoff.txt").read_text() == "held", "the warmer posted its events"
    doc = json.loads(_held(proj, key).read_text())
    assert [e["event"]["task_id"] for e in doc["events"]] == ["prompt_t"]
    assert doc["events"][0]["writer"]["project"] == "p"


def test_an_interactive_run_still_hands_its_events_off(monkeypatch) -> None:
    """Leave-alone: outside the warmer the hand-off is the real one."""
    from claude_mcp_servers.rl_client import deferred_emit, search_pipeline

    seen: list = []
    monkeypatch.setattr(deferred_emit, "hand_off", lambda items, emit=None: seen.append(items) or "inline")
    assert search_pipeline.hand_off_deferred(("x",)) == "inline"
    assert seen == [("x",)]


def _hit(hcr, proj: Path, monkeypatch, run_id: str, session: str):
    plan = hcr._Plan()
    plan.kg_query = "q"
    plan.kg_profile = "prompt"
    plan.kg_limit = 3
    hcr._install_staged_ctx_run_id_wrapper()
    monkeypatch.setattr(hcr, "_CURRENT_RUN_ID", run_id)
    for var in ("VCT_SESSION_ID", "CLAUDE_SESSION_ID", "VCT_AGENT_ID"):
        monkeypatch.delenv(var, raising=False)
    if session:
        monkeypatch.setenv("VCT_SESSION_ID", session)
    return hcr._run_legs(plan, session, "p1", str(proj), "", time.monotonic() + 6.0)


def test_the_run_that_shows_the_entry_sends_the_held_events_under_its_session(
        tmp_path: Path, monkeypatch) -> None:
    from tests.test_v02105_router_hard_deadline import ROUTER, _project

    sys.path.insert(0, str(ROUTER.parent))
    import hook_context_router as hcr  # pyright: ignore[reportMissingImports]
    from claude_mcp_servers.rl_client import deferred_emit

    proj = _project(tmp_path)
    key = hcr.cache_key("kgi", "prompt", "q", "3")
    assert _warm_emitting(tmp_path, proj, key).returncode == 0
    released: list = []
    monkeypatch.setattr(deferred_emit, "release_held",
                        lambda batch, session_id="", emit=None: released.append((batch, session_id)) or "inline")
    try:
        # No session of its own: nothing is sent, the held file waits.
        _hit(hcr, proj, monkeypatch, "router_nosess", "")
        assert released == [] and _held(proj, key).is_file()
        kg_text, _cg = _hit(hcr, proj, monkeypatch, "router_show", "viewer-sess")
    finally:
        hcr._STAGED_BY_RUN.pop("router_nosess", None)
        hcr._STAGED_BY_RUN.pop("router_show", None)
    assert "KG: Stub Node" in kg_text
    assert len(released) == 1
    batch, session = released[0]
    assert session == "viewer-sess"
    assert [e["event"]["task_id"] for e in batch] == ["prompt_t"]
    assert not _held(proj, key).exists(), "the held events are sent once"


def test_release_held_sends_each_event_under_the_new_session(monkeypatch) -> None:
    from claude_mcp_servers.rl_client import deferred_emit

    monkeypatch.setenv(deferred_emit.INLINE_ENV, "1")
    sent: list = []

    def _emit(ev, writer_factory=None):
        sent.append(ev)
        return True

    batch = [{"event": {"query": "q", "query_emb": [0.5], "embedding_source": "qwen3",
                        "embedding_dim": 1, "embedding_model": "m", "nodes": [],
                        "task_id": "t1", "task_type": "prompt_kg_search",
                        "session_id": "warm-sess"},
              "writer": {"project": "p"}}]
    assert deferred_emit.release_held(batch, session_id="viewer", emit=_emit) == "inline"
    assert [e.session_id for e in sent] == ["viewer"]
    assert batch[0]["event"]["session_id"] == "warm-sess", "the held batch is not mutated"
    assert deferred_emit.release_held([], session_id="viewer", emit=_emit) == "none"


# --- 5. the trainer's hub client pages newest-first --------------------------


class _FakeResp:
    def __init__(self, rows):
        self.status = 200
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._rows


class _FakeSession:
    """Serves ``rows`` the way ``Db::list_rl_events_page`` does: newest first
    by (ts, id), the before_ts/before_id keyset, the limit."""

    def __init__(self, rows, *, ignore_cursor: bool = False):
        self.rows = rows
        self.calls: list = []
        self.closed = False
        self.ignore_cursor = ignore_cursor

    def get(self, url, headers=None, params=None):
        self.calls.append(dict(params or {}))
        rows = sorted(self.rows, key=lambda r: (r["ts_ms"], r["id"]), reverse=True)
        if not self.ignore_cursor and "before_ts" in params:
            cur = (int(params["before_ts"]), int(params["before_id"]))
            rows = [r for r in rows if (r["ts_ms"], r["id"]) < cur]
        return _FakeResp(rows[: int(params["limit"])])


def _client(session):
    sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))
    from rl_reranker.rl_server import _HubClient

    c = _HubClient(base_url="http://127.0.0.1:9", module_id="m", token_env="VCO_TEST_NO_TOKEN")
    c._token = "t"

    async def _no_refresh():
        return None

    async def _session():
        return session

    c._maybe_refresh_token = _no_refresh  # type: ignore[method-assign]
    c._ensure_session = _session  # type: ignore[method-assign]
    return c


def _rows(n: int) -> list:
    # Pairs of rows share a timestamp, so page boundaries fall inside a ts.
    return [{"id": i + 1, "ts_ms": 1000 + i // 2, "payload_json": "{}"} for i in range(n)]


def test_paging_reads_the_whole_window_once_oldest_first() -> None:
    import asyncio

    sess = _FakeSession(_rows(25))
    got = asyncio.run(_client(sess).list_rl_events("pid", page_limit=4))
    assert [r["id"] for r in got] == list(range(1, 26))
    assert len(sess.calls) == 8, "25 rows in pages of 4, plus the empty last page"
    assert all(c["limit"] == 4 for c in sess.calls)


def test_max_events_keeps_the_newest_rows() -> None:
    import asyncio

    got = asyncio.run(_client(_FakeSession(_rows(25))).list_rl_events(
        "pid", page_limit=4, max_events=6))
    assert [r["id"] for r in got] == [20, 21, 22, 23, 24, 25]


def test_a_hub_that_ignores_the_cursor_does_not_loop_or_duplicate() -> None:
    import asyncio

    sess = _FakeSession(_rows(10), ignore_cursor=True)
    got = asyncio.run(_client(sess).list_rl_events("pid", page_limit=4))
    assert [r["id"] for r in got] == [7, 8, 9, 10]
    assert len(sess.calls) == 2


def test_the_training_load_keeps_a_bounded_newest_window() -> None:
    sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))
    from rl_reranker import hub_event_loader as hel

    seen: dict = {}

    class _Hub:
        async def list_rl_events(self, project_id, **kw):
            seen.update(kw)
            return []

    hel.load_events_from_hub(_Hub(), "pid")
    assert seen["max_events"] == hel.DEFAULT_MAX_EVENTS == 10_000
