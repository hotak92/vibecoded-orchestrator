# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Serve-time scoring of the RL reranker, end to end (v0.2.106).

Before v0.2.106 the reranker never scored: ``rerank_query_aware`` embedded the
query through an injected ``embedding_fn`` no caller wired, so ``/cache_nodes``
always answered in search order, and the client did not send the vectors it
already had. Now the client sends the query vector + per-candidate vectors
(``rl_client.rerank_wire``, float16) and the server scores with the loaded
model.

Covered here:

* the wire format round-trips (float16 values, de-duplication, refusals);
* train/serve parity: the same logged event gives the same model input
  tensors through the training export + ``shard_trainer.build_inputs`` and
  through the client encode + server decode + ``RLModel._build_inputs``;
* arctic linked-slot masking at serve time (and in that parity);
* a tiny model trained on synthetic data provably swaps a known pair, over
  real HTTP, through ``RLClient`` and through ``search_pipeline._do_rerank``;
* no HTTP call when the scoring lock is set, when the container has no trained
  weights, or when no container is wired — and no fallback counted for those;
* a timeout falls back to search order, counts the fallback, and warns once
  per session.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

aiohttp = pytest.importorskip("aiohttp")
torch = pytest.importorskip("torch")
httpx = pytest.importorskip("httpx")
from aiohttp.test_utils import TestServer  # noqa: E402

from rl_client import rerank_wire as wire  # noqa: E402
from rl_reranker import rl_model as rlm  # noqa: E402
from rl_reranker.retrieval_rl import (  # noqa: E402
    RetrievalRL,
    order_by_scores,
    serving_candidates,
)
from rl_reranker.rl_model import RLModel  # noqa: E402
from rl_reranker.rl_server import RLServer  # noqa: E402
from rl_reranker.training import export as ex  # noqa: E402
from rl_reranker.training import shard_trainer as st  # noqa: E402

from claude_mcp_servers.rl_client import search_pipeline  # noqa: E402
from claude_mcp_servers.rl_client.client import RLClient  # noqa: E402

D = 16


def _run(coro):
    return asyncio.run(coro)


def _unit(rng: np.random.Generator) -> list[float]:
    v = rng.normal(size=D).astype(np.float32)
    return (v / np.linalg.norm(v)).tolist()


def _f16(v) -> np.ndarray:
    return np.asarray(v, dtype=np.float16).astype(np.float32)


# ─── wire format ───────────────────────────────────────────────────────


def test_max_linked_matches_the_model():
    assert wire.MAX_LINKED == rlm.MAX_LINKED


def test_features_round_trip_as_float16_and_dedupe():
    rng = np.random.default_rng(1)
    q, a, b, shared = (_unit(rng) for _ in range(4))
    nodes = [
        {"title": "A", "emb": a, "node_type": "tool",
         "linked_embs": [shared, [], b], "linked_type_names": ["concept", "x", ""]},
        {"title": "B", "emb": b, "linked_embs": [shared], "link_types": ["model"]},
        {"title": "C"},  # no vector: not scored
    ]
    feats = wire.encode_rerank_features(q, nodes, dim=D)
    assert feats is not None and feats["format"] == wire.FEATURE_FORMAT
    # q, a, shared, b — `b` and `shared` are stored once each
    assert len(base64.b64decode(feats["vectors"])) == 4 * D * 2
    dec = wire.decode_rerank_features(json.loads(json.dumps(feats)))
    assert np.array_equal(dec.query, _f16(q))
    n0, n1, n2 = dec.nodes
    assert np.array_equal(n0.vec, _f16(a)) and n0.node_type == "tool"
    # the empty linked entry is skipped together with its type name
    assert [list(x) for x in n0.linked] == [list(_f16(shared)), list(_f16(b))]
    assert n0.linked_types == ["concept", ""]
    assert n1.linked_types == ["model"] and n1.node_type == ""
    assert n2.vec is None and n2.linked == []


def test_nothing_scorable_encodes_to_none():
    rng = np.random.default_rng(2)
    q = _unit(rng)
    assert wire.encode_rerank_features(None, [{"emb": q}], dim=D) is None
    assert wire.encode_rerank_features(q[:-1], [{"emb": q}], dim=D) is None  # wrong dim
    assert wire.encode_rerank_features(q, [{"title": "no vec"}], dim=D) is None
    assert wire.encode_rerank_features(q, [{"emb": q[:3]}], dim=D) is None


def test_include_linked_false_sends_no_linked_vectors():
    rng = np.random.default_rng(3)
    q, a, b = (_unit(rng) for _ in range(3))
    feats = wire.encode_rerank_features(
        q, [{"emb": a, "linked_embs": [b]}], dim=D, include_linked=False,
    )
    assert feats is not None
    assert feats["nodes"][0]["l"] == [] and feats["nodes"][0]["lt"] == []


@pytest.mark.parametrize("mutate", [
    lambda f: f.update(format="f32"),
    lambda f: f.update(dim=0),
    lambda f: f.update(vectors="!!not base64!!"),
    lambda f: f.update(vectors=base64.b64encode(b"\0" * 3).decode()),
    lambda f: f.update(query=99),
    lambda f: f["nodes"][0].update(n=99),
    lambda f: f["nodes"][0].update(l=[0, 0], lt=["x"]),
    lambda f: f["nodes"][0].update(l=[0] * 6, lt=[""] * 6),
    lambda f: f.update(nodes="x"),
])
def test_malformed_features_raise_value_error(mutate):
    rng = np.random.default_rng(4)
    feats = wire.encode_rerank_features(_unit(rng), [{"emb": _unit(rng)}], dim=D)
    mutate(feats)
    with pytest.raises(ValueError):
        wire.decode_rerank_features(feats)


def test_order_by_scores_keeps_unscored_positions():
    # positions 0, 2, 3 are scored; 1 has no vector and stays put
    assert order_by_scores(4, [0, 2, 3], [0.1, 0.9, 0.5]) == [2, 1, 3, 0]
    # ties keep search order
    assert order_by_scores(3, [0, 1, 2], [0.5, 0.5, 0.5]) == [0, 1, 2]


def test_payload_for_a_realistic_search_stays_bounded():
    """30 candidates x (node + 5 linked) at 1024 dims: about 0.5 MB, an
    eighth of the same vectors as JSON numbers (measured 0.499 MB vs 3.81 MB)."""
    rng = np.random.default_rng(5)
    dim = 1024

    def vec():
        return rng.normal(size=dim).astype(np.float32).tolist()

    nodes = [{"title": f"n{i}", "emb": vec(), "linked_embs": [vec() for _ in range(5)],
              "linked_type_names": ["concept"] * 5} for i in range(30)]
    feats = wire.encode_rerank_features(vec(), nodes, dim=dim)
    body = json.dumps({"nodes": [{"title": n["title"]} for n in nodes], "features": feats})
    assert len(body) < 550_000
    # the old wire carried every vector as JSON float text
    assert len(body) < len(json.dumps(nodes)) / 6


# ─── train/serve parity ────────────────────────────────────────────────


def _parity_event() -> dict:
    rng = np.random.default_rng(7)
    nodes = [
        {"title": "N0", "score": 0.9, "emb": _unit(rng), "node_type": "concept",
         "linked_embs": [_unit(rng), _unit(rng)], "linked_type_names": ["tool", "pattern"]},
        {"title": "N1", "score": 0.8, "emb": _unit(rng), "node_type": "pattern",
         "linked_embs": [_unit(rng)], "linked_type_names": [""]},
        {"title": "N2", "score": 0.7, "emb": _unit(rng), "node_type": "",
         "linked_embs": [[], _unit(rng)], "link_types": ["model", "research"]},
        {"title": "N3", "score": 0.6, "emb": _unit(rng), "node_type": "tool"},
    ]
    return {"event": "retrieval", "schema_version": 4, "task_id": "t-parity",
            "query": "q", "embedding_source": "qwen3", "embedding_dim": D,
            "nodes": nodes, "query_emb": _unit(rng)}


def _export_event(tmp_path: Path, event: dict) -> Path:
    db = tmp_path / "launcher.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL,
            folder_path TEXT NOT NULL UNIQUE, host TEXT NOT NULL,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, slug TEXT);
        CREATE TABLE rl_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 3,
            ts INTEGER NOT NULL, project_id TEXT, project_name TEXT,
            task_id TEXT NOT NULL, task_type TEXT, embedding_source TEXT,
            embedding_dim INTEGER, embedding_model TEXT, payload_json TEXT NOT NULL,
            quarantined_at INTEGER, quarantine_reason TEXT);
    """)
    conn.execute("INSERT INTO projects VALUES ('p1','ParityProject','/x/parity','base',0,0,NULL)")
    titles = [n["title"] for n in event["nodes"]]
    citation = {"event": "citation", "schema_version": 4, "task_id": event["task_id"],
                "citations": {t: True for t in titles},
                "cosine_sims": {t: 0.5 for t in titles},
                "literal_cited": {t: False for t in titles}, "window_tokens": 3000}
    ts = ex.V0_2_101_RELEASE_MS + 86_400_000
    for etype, payload, t in (("retrieval", event, ts), ("citation", citation, ts + 1)):
        conn.execute(
            "INSERT INTO rl_events (event_type, ts, project_id, task_id, task_type,"
            " embedding_source, embedding_dim, payload_json) VALUES (?,?,?,?,?,?,?,?)",
            (etype, t, "p1", event["task_id"], "pre_bash_kg_search", "qwen3", D,
             json.dumps(payload)))
    conn.commit()
    conn.close()
    out = tmp_path / "export"
    ex.run_export(db, out, salt=b"s" * 64, include=[], exclude=[], profiles=("qwen3",), dim=D)
    return out


def _slot_rows(ent, types, scal) -> list[tuple]:
    """Linked slots 2..6 as a sorted multiset (both sides shuffle them)."""
    rows = [
        (int(types[j]), tuple(np.round(ent[j], 5)), tuple(np.round(scal[j], 5)))
        for j in range(2, ent.shape[0])
    ]
    return sorted(rows)


@pytest.mark.parametrize("profile", ["qwen3", "arctic"])
def test_train_and_serve_build_the_same_model_inputs(tmp_path, profile):
    event = _parity_event()
    data = st.load_training_data([_export_event(tmp_path, event)], "qwen3", device="cpu")
    data.profile = profile  # the export of the same event under the masked profile
    idx = torch.arange(len(data.np_target))
    t_ent, t_types, t_scal = (
        x.numpy() for x in st.build_inputs(data, idx, generator=torch.Generator().manual_seed(0))
    )

    model = RLModel(emb_dim=D, state_path=tmp_path / "m" / "rl.pt", device="cpu",
                    embedding_source="qwen3")
    feats = wire.encode_rerank_features(event["query_emb"], event["nodes"], dim=D)
    dec = wire.decode_rerank_features(json.loads(json.dumps(feats)))
    cands, scored = serving_candidates(
        model, dec.nodes, masked=rlm.linked_slots_masked(profile),
    )
    assert scored == [0, 1, 2, 3]
    served = []
    for n_raw, linked, n_t, l_t in cands:
        e, t, s = model._build_inputs(dec.query, n_raw, linked, n_t, linked_type_idxs=l_t)
        served.append((e[0].numpy(), t[0].numpy(), s[0].numpy()))

    assert t_ent.shape[0] == len(served) == 4
    # a non-builtin type (here "pattern") lands on the trainer's OTHER row
    assert model.get_type_idx("pattern") == st.OTHER_TYPE_ROW
    for i in range(t_ent.shape[0]):
        # pair training sample i with the served candidate of the same node vector
        j = next(k for k, (e, _t, _s) in enumerate(served)
                 if np.allclose(e[1], t_ent[i][1], atol=0))
        e, t, s = served[j]
        assert np.array_equal(e[0], t_ent[i][0])                # query slot
        assert np.array_equal(e[1], t_ent[i][1])                # node slot
        assert list(t[:2]) == list(t_types[i][:2])              # query + node types
        assert np.allclose(s[:2], t_scal[i][:2], atol=1e-5)     # (1,1), (cos_qn, 1)
        assert _slot_rows(e, t, s) == _slot_rows(t_ent[i], t_types[i], t_scal[i])
        if rlm.linked_slots_masked(profile):
            assert (t[2:] == rlm.NULL_TYPE_IDX).all() and not e[2:].any()
    # the qwen3 case does carry linked slots (N0 has two, N2 one after the skip)
    if profile == "qwen3":
        assert sum(int((t[2:] != rlm.NULL_TYPE_IDX).sum()) for _e, t, _s in served) == 4


# ─── a tiny trained model, served over HTTP ────────────────────────────


@pytest.fixture(scope="module")
def trained_model(tmp_path_factory) -> RLModel:
    """A D=16 network trained on synthetic data: high score when the node is
    close to the query, low for an unrelated node."""
    torch.manual_seed(0)
    rng = np.random.default_rng(0)
    m = RLModel(emb_dim=D, state_path=tmp_path_factory.mktemp("trained") / "rl.pt",
                device="cpu", embedding_source="qwen3")
    for _ in range(300):
        q = rng.normal(size=D).astype(np.float32)
        m.update(q, (q + 0.3 * rng.normal(size=D)).astype(np.float32), [], 0, target=0.95)
        m.update(q, rng.normal(size=D).astype(np.float32), [], 0, target=0.05)
    m._weights_version = "9.9.9"  # a released, trained checkpoint
    return m


def _known_pair() -> tuple[list[float], list[dict]]:
    """Search order puts the unrelated node FIRST; the model must swap them."""
    rng = np.random.default_rng(42)
    q = rng.normal(size=D).astype(np.float32)
    related = (q + 0.2 * rng.normal(size=D)).astype(np.float32)
    unrelated = rng.normal(size=D).astype(np.float32)
    return q.tolist(), [
        {"title": "unrelated", "score": 0.9, "node_type": "concept", "emb": unrelated.tolist()},
        {"title": "related", "score": 0.8, "node_type": "concept", "emb": related.tolist()},
    ]


def _make_server(tmp_path, monkeypatch, model: "RLModel | None", source: str = "qwen3"):
    monkeypatch.setenv("ACTIVE_EMBEDDING", source)
    monkeypatch.setenv("RL_MODEL_TAG", source)
    monkeypatch.setenv("RL_EMB_DIM", str(D))
    monkeypatch.setenv("RL_GLOBAL_WEIGHTS_DIR", str(tmp_path / "global"))
    monkeypatch.delenv("VCT_PROJECT_ID", raising=False)
    root = tmp_path / "project"
    root.mkdir()
    srv = RLServer(project_root=root,
                   log_path=str(tmp_path / "logs" / "rl_events.jsonl"), port=0)
    if model is not None:
        srv._rl._rl_model = model
        for key, head in srv._project_heads.items():
            head.rl_model = model
    return srv


async def _serve(srv, body):
    ts = TestServer(srv._app)
    await ts.start_server()
    try:
        return await body(str(ts.make_url("")).rstrip("/"))
    finally:
        await ts.close()


async def _post(base: str, payload: dict) -> tuple[int, dict]:
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{base}/cache_nodes", json=payload)
        return r.status_code, r.json()


def test_server_swaps_the_known_pair(tmp_path, monkeypatch, trained_model):
    """RED before v0.2.106: the server answered in search order (no `order`)."""
    q, nodes = _known_pair()
    srv = _make_server(tmp_path, monkeypatch, trained_model)
    payload = {
        "task_id": "t1", "query": "q", "limit": 2, "active_embedding": "qwen3",
        # the pre-v0.2.106 body carried the vectors on the nodes; keep them so
        # the red proof runs the old server on the same input
        "nodes": nodes,
        "features": wire.encode_rerank_features(q, nodes, dim=D),
    }
    status, data = _run(_serve(srv, lambda base: _post(base, payload)))
    assert status == 200, data
    assert data.get("order") == [1, 0], data
    assert [n["title"] for n in data["top_k"]] == ["related", "unrelated"]
    s_unrel, s_rel = data["scores"]
    assert s_rel > s_unrel


def test_server_health_advertises_the_scoring_contract(tmp_path, monkeypatch, trained_model):
    srv = _make_server(tmp_path, monkeypatch, trained_model)

    async def body(base):
        async with httpx.AsyncClient() as c:
            return (await c.get(f"{base}/health")).json()

    h = _run(_serve(srv, body))
    assert h["protocol_version"] == wire.RERANK_PROTOCOL_VERSION
    assert h["embedding_dim"] == D and h["embedding_space"] == "qwen3"
    assert h["trained"] is True and h["linked_slots_masked"] is False


@pytest.mark.parametrize("payload_patch, code", [
    ({"features": None}, 422),
    ({"features": {"format": "nope"}}, 422),
    ({"nodes": "x"}, 400),
    ({"task_id": 5}, 400),
    ({"active_embedding": "arctic"}, 409),
])
def test_server_refuses_bad_bodies(tmp_path, monkeypatch, trained_model, payload_patch, code):
    q, nodes = _known_pair()
    srv = _make_server(tmp_path, monkeypatch, trained_model)
    payload = {"task_id": "t", "nodes": [{"title": n["title"]} for n in nodes], "limit": 2,
               "features": wire.encode_rerank_features(q, nodes, dim=D)}
    payload.update(payload_patch)
    if payload.get("features") is None:
        payload.pop("features")
    status, data = _run(_serve(srv, lambda base: _post(base, payload)))
    assert status == code, data
    assert data["ok"] is False


def test_arctic_server_masks_linked_slots_it_is_sent(tmp_path, monkeypatch, trained_model):
    """Linked vectors sent to an arctic server never reach the model."""
    srv = _make_server(tmp_path, monkeypatch, None, source="arctic")
    seen: list = []

    def spy(q_raw, candidates):
        seen.extend(candidates)
        return [0.5] * len(candidates)

    srv._rl._rl_model.score_batch = spy  # type: ignore[method-assign]
    rng = np.random.default_rng(9)
    nodes = [{"title": "a", "emb": _unit(rng), "linked_embs": [_unit(rng)],
              "linked_type_names": ["tool"]}]
    payload = {"task_id": "t", "nodes": [{"title": "a"}], "limit": 1,
               "features": wire.encode_rerank_features(_unit(rng), nodes, dim=D)}
    assert payload["features"]["nodes"][0]["l"]  # the client did send one
    status, data = _run(_serve(srv, lambda base: _post(base, payload)))
    assert status == 200 and data["linked_slots_masked"] is True
    (_n, linked, _t, linked_types), = seen
    assert linked == [] and linked_types is None


def test_rerank_requires_a_model(tmp_path):
    rl = RetrievalRL(project_root=tmp_path, rl_model=None)
    with pytest.raises(RuntimeError):
        rl.rerank({}, 0, profile="qwen3")


# ─── the client and the search pipeline ────────────────────────────────


def _http_client(base: str) -> RLClient:
    return RLClient(base_url=base, text_dim=D, active_embedding="qwen3")


def test_client_reranks_through_the_real_server(tmp_path, monkeypatch, trained_model):
    q, nodes = _known_pair()
    srv = _make_server(tmp_path, monkeypatch, trained_model)

    async def body(base):
        client = _http_client(base)
        try:
            neg = await client.negotiate()
            ranked = await client.cache_nodes("q", nodes, 2, task_id="t", query_emb=q)
            return neg, ranked, client.last_call_ok, client.last_scores
        finally:
            await client.aclose()

    neg, ranked, ok, scores = _run(_serve(srv, body))
    assert neg.status == "compatible" and neg.trained is True and neg.weights_version == "9.9.9"
    assert ok is True
    assert ranked[0] is nodes[1] and ranked[1] is nodes[0]  # the caller's own dicts
    assert scores[0] > scores[1]


@pytest.fixture()
def counter_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(search_pipeline, "_WARNED_RL_FALLBACK", False)
    return tmp_path


def _counter(root: Path) -> dict:
    p = root / ".claude" / "state" / "rl_fallback_counter.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


async def _pipeline_rerank(client, q, nodes, session="sess-1"):
    with patch("claude_mcp_servers.weaviate_mcp.server._get_rl_client", return_value=client):
        return await search_pipeline._do_rerank(
            query="q", candidates=nodes, limit=2, task_id="t", session_id=session,
            query_emb=q,
        )


def test_pipeline_reranks_end_to_end(tmp_path, monkeypatch, trained_model, counter_root):
    q, nodes = _known_pair()
    srv = _make_server(tmp_path, monkeypatch, trained_model)

    async def body(base):
        client = _http_client(base)
        try:
            return await _pipeline_rerank(client, q, nodes)
        finally:
            await client.aclose()

    ranked = _run(_serve(srv, body))
    assert [n["title"] for n in ranked] == ["related", "unrelated"]
    assert _counter(counter_root).get("success_count") == 1


class _CountingTransport:
    """httpx stand-in: answers /health with ``health``; counts POSTs."""

    def __init__(self, health: dict, post_exc: "Exception | None" = None):
        self.health = health
        self.post_exc = post_exc
        self.gets = 0
        self.posts = 0

    async def get(self, url, timeout=None):
        self.gets += 1
        return httpx.Response(200, json=self.health)

    async def post(self, url, json=None, headers=None, timeout=None):
        self.posts += 1
        if self.post_exc is not None:
            raise self.post_exc
        return httpx.Response(500)

    async def aclose(self):
        pass


_TRAINED_HEALTH = {"ok": True, "model": "RLModel loaded", "trained": True,
                   "protocol_version": 2, "embedding_dim": D, "embedding_space": "qwen3",
                   "linked_slots_masked": False, "weights": {"loaded_version": "1.0.0"}}


def test_not_trained_container_is_never_posted_to(counter_root):
    q, nodes = _known_pair()
    t = _CountingTransport({**_TRAINED_HEALTH, "trained": False, "model": "not trained"})
    client = RLClient(base_url="http://127.0.0.1:9", text_dim=D, active_embedding="qwen3",
                      client=t)
    for _ in range(3):
        assert _run(_pipeline_rerank(client, q, nodes)) is None
    assert t.posts == 0 and t.gets == 1  # one probe, memoised
    assert _counter(counter_root) == {}  # an off state, not a fallback


def test_old_container_is_never_posted_to(counter_root):
    q, nodes = _known_pair()
    t = _CountingTransport({"ok": True, "model": "RLModel loaded"})  # pre-v0.2.106 /health
    client = RLClient(base_url="http://127.0.0.1:9", text_dim=D, active_embedding="qwen3",
                      client=t)
    assert _run(_pipeline_rerank(client, q, nodes)) is None
    assert t.posts == 0
    assert "degraded_old_server" in _counter(counter_root)["last_reason"]


def test_unwired_client_counts_nothing(counter_root, monkeypatch):
    monkeypatch.delenv("RL_SERVER_URL", raising=False)
    monkeypatch.delenv("RL_SERVER_PORT", raising=False)
    q, nodes = _known_pair()
    client = RLClient(text_dim=D, active_embedding="qwen3")
    assert client.enabled is False
    assert _run(_pipeline_rerank(client, q, nodes)) is None
    assert _counter(counter_root) == {}


def test_scoring_lock_means_no_container_contact(counter_root, monkeypatch):
    """While the shipped lock is set, rerank_and_emit never builds a client."""
    q, nodes = _known_pair()
    monkeypatch.setattr(search_pipeline, "_rl_scoring_lock_reason", lambda: "locked")
    monkeypatch.setattr(search_pipeline, "_retrieval_emit_has_consumer", lambda: False)
    monkeypatch.setattr(search_pipeline, "_should_capture_citations", lambda _e: False)
    monkeypatch.setattr(search_pipeline, "_drive_retention_housekeeping", lambda: None)
    with patch("claude_mcp_servers.weaviate_mcp.server._get_rl_client") as getter:
        res = _run(search_pipeline.rerank_and_emit(search_pipeline.RerankRequest(
            query="q", candidates=nodes, limit=2, query_emb=q, task_id="t",
        )))
    getter.assert_not_called()
    assert res.rl_used is False and [n["title"] for n in res.ranked] == ["unrelated", "related"]
    assert _counter(counter_root) == {}


def test_timeout_falls_back_and_warns_once_per_session(counter_root, caplog):
    q, nodes = _known_pair()
    t = _CountingTransport(_TRAINED_HEALTH, post_exc=httpx.ReadTimeout("timed out"))
    client = RLClient(base_url="http://127.0.0.1:9", text_dim=D, active_embedding="qwen3",
                      client=t, timeout=0.05)
    with caplog.at_level(logging.WARNING, logger=search_pipeline.logger.name):
        assert _run(_pipeline_rerank(client, q, nodes, session="s-A")) is None
        # a new hook process of the same session: the in-process flag is fresh
        search_pipeline._WARNED_RL_FALLBACK = False
        assert _run(_pipeline_rerank(client, q, nodes, session="s-A")) is None
        search_pipeline._WARNED_RL_FALLBACK = False
        assert _run(_pipeline_rerank(client, q, nodes, session="s-B")) is None
    warnings = [r for r in caplog.records if "fell back to search order" in r.getMessage()]
    assert len(warnings) == 2  # once for s-A, once for s-B
    assert t.posts == 3
    c = _counter(counter_root)
    assert c["count"] == 3 and c["warned_session"] == "s-B"
    assert "timed out" in c["last_reason"]


def test_masked_profile_client_sends_no_linked_vectors(counter_root):
    q, nodes = _known_pair()
    nodes = [dict(n, linked_embs=[n["emb"]], linked_type_names=["tool"]) for n in nodes]
    sent: list = []

    class _T(_CountingTransport):
        async def post(self, url, json=None, headers=None, timeout=None):
            sent.append(json)
            return httpx.Response(200, json={"scored": True, "order": [1, 0],
                                             "scores": [0.1, 0.9]})

    t = _T({**_TRAINED_HEALTH, "embedding_space": "arctic", "linked_slots_masked": True})
    client = RLClient(base_url="http://127.0.0.1:9", text_dim=D, active_embedding="arctic",
                      client=t)
    ranked = _run(_pipeline_rerank(client, q, nodes))
    assert ranked is not None
    assert [n["title"] for n in ranked] == ["related", "unrelated"]
    (body,) = sent
    assert all(e["l"] == [] for e in body["features"]["nodes"])
    assert all(set(n) == {"title", "node_type"} for n in body["nodes"])  # slim nodes
