# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Per-project online learning + the serving gate (v0.2.106).

Owner rulings 2026-11-12: per-project ONLINE adaptation, opt-in; a project's
model is served only while, on its own recent held-out queries, NDCG@10 is
>= +2% over plain cosine AND the cited node's MRR@10 is not below cosine's.

Two layers:

* the learner (``online_learning``): the held-out window and purge gap, the
  round's objective (exactly ``objective.batch_loss`` + AdamW), the gate,
  determinism, persistence, model swaps, dynamic-type rows;
* the real server in-process over HTTP: a synthetic project stream starts at
  the search order, qualifies, is served reranked results, and a degraded
  stream makes the gate stop serving.
"""

from __future__ import annotations

import asyncio
import copy
import math
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")
httpx = pytest.importorskip("httpx")
from aiohttp.test_utils import TestServer  # noqa: E402

from rl_client import rerank_wire as wire  # noqa: E402
from rl_reranker import objective, online_learning as ol, offline_trainer  # noqa: E402
from rl_reranker.rl_model import N_ENTITY_TYPES, NODE_TYPES, RLModel, WEIGHT_DECAY  # noqa: E402
from rl_reranker.rl_server import RLServer, _seeded_init  # noqa: E402

D = 16


def _model(tmp_path: Path, name: str = "m") -> RLModel:
    with _seeded_init(7, enabled=True):
        return RLModel(emb_dim=D, state_path=tmp_path / name / "rl_model_qwen3_local.pt",
                       device="cpu", embedding_source="qwen3")


def _unit(rng) -> np.ndarray:
    v = rng.normal(size=D).astype(np.float32)
    return v / np.linalg.norm(v)


def _event(rng, *, good: bool = True, n: int = 6) -> tuple[dict, list[dict]]:
    """A synthetic citation event + the candidates as the search returns them.

    Candidate i is ``q * s_i + noise`` with ``s`` falling, so plain cosine
    mostly ranks them by i and the answer uses them in that order too — a
    decent baseline. good: the most useful node is a ``tool`` node in the
    MIDDLE of the cosine order (its type tells the model; cosine misses it).
    Degraded (good=False): the most useful node is the one closest to the
    query (cosine is right) and the tool node is useless — a model trained
    on the good stream now ranks the wrong node first.
    """
    q = _unit(rng)
    tool_at = int(rng.integers(2, 5))
    cands = []
    for i in range(n):
        v = q * (1.6 - 0.25 * i) + 0.35 * _unit(rng)
        ntype = "tool" if i == tool_at else "concept"
        cands.append({"title": f"n{i}", "node_type": ntype, "emb": (v / np.linalg.norm(v)).tolist()})
    useful = f"n{tool_at}" if good else "n0"
    cos = {c["title"]: 0.6 - 0.06 * k for k, c in enumerate(cands)}
    cos[useful] = 0.95
    if not good:
        cos[f"n{tool_at}"] = 0.05
    # search order = cosine to the query (descending)
    cands.sort(key=lambda c: -float(np.dot(q, np.array(c["emb"]))))
    task = {
        "nodes_packed": [{"title": c["title"], "node_type": c["node_type"],
                          "n_emb": c["emb"], "linked_embs": [], "linked_type_names": []}
                         for c in cands],
        "query_emb": q.tolist(),
        "cosine_sims": cos,
        "literal_cited": {useful: True},
        "cross_encoder_cited": None,
    }
    return task, cands


# ─── the window and the purge gap ──────────────────────────────────────


def test_no_query_is_trained_on_while_in_the_window_or_within_the_gap():
    cfg = ol.OnlineConfig(holdout_queries=5, min_cited_queries=0, purge_gap_ms=10,
                          max_staged_queries=1000)
    lr = ol.ProjectLearner(profile="qwen3", emb_dim=D, cfg=cfg)
    rng = np.random.default_rng(0)
    for t in range(60):
        lr.ingest(_event(rng)[0], ts_ms=t * 3)
        window = lr._queue.window()
        assert len(window) == min(t + 1, 5)
        if lr._pending:
            newest_trainable = max(q.ts_ms for q in lr._pending)
            assert newest_trainable <= min(q.ts_ms for q in window) - cfg.purge_gap_ms
            assert not ({id(q) for q in lr._pending} & {id(q) for q in window})
    assert len(lr._pending) > 0


def test_the_window_grows_until_it_holds_enough_citing_queries():
    """On a busy project a fixed-size window can hold no citing query; the
    window extends (up to the cap) until the MRR half of the bar is judgeable."""
    cfg = ol.OnlineConfig(holdout_queries=4, min_cited_queries=3, max_holdout_queries=30,
                          purge_gap_ms=0)
    lr = ol.ProjectLearner(profile="qwen3", emb_dim=D, cfg=cfg)
    rng = np.random.default_rng(1)
    for t in range(20):
        task = _event(rng)[0]
        if t % 5:  # only every 5th query cites a node
            task["literal_cited"] = {}
        lr.ingest(task, ts_ms=t)
    w = lr._queue.window()
    assert sum(q.cites for q in w) == 3 == lr._queue.window_cited
    # citing queries at t = 0, 5, 10, 15: the shortest run with three of them
    # (and >= 4 queries) starts at t = 5 and ends at t = 19
    assert len(w) == 15 and w[0].ts_ms == 5 and w[0].cites
    # the cap wins when citations never come
    for t in range(20, 80):
        task = _event(rng)[0]
        task["literal_cited"] = {}
        lr.ingest(task, ts_ms=t)
    assert lr.holdout_size == 30


def test_max_staged_releases_before_the_gap_on_a_busy_project():
    cfg = ol.OnlineConfig(holdout_queries=3, min_cited_queries=0, purge_gap_ms=10**12,
                          max_staged_queries=4)
    lr = ol.ProjectLearner(profile="qwen3", emb_dim=D, cfg=cfg)
    rng = np.random.default_rng(2)
    for t in range(20):
        lr.ingest(_event(rng)[0], ts_ms=t)
    assert lr._queue.staged == 4 and len(lr._pending) == 20 - 3 - 4


# ─── a round is exactly the offline objective ─────────────────────────


def test_a_round_steps_on_objective_batch_loss_with_adamw(tmp_path):
    cfg = ol.OnlineConfig(holdout_queries=4, min_cited_queries=0, purge_gap_ms=0,
                          round_every=1000, batch_queries=4, max_steps_per_round=3, lr=1e-3)
    lr = ol.ProjectLearner(profile="qwen3", emb_dim=D, cfg=cfg)
    rng = np.random.default_rng(3)
    for t in range(20):
        lr.ingest(_event(rng)[0], ts_ms=t)
    m = _model(tmp_path)
    job = lr.prepare_round(m, now_ms=20)
    assert job is not None and len(job.steps) == 3

    # the reference: a fresh copy of the same net, AdamW, batch_loss, same seed
    ref = copy.deepcopy(m._net)
    opt = torch.optim.AdamW(ref.parameters(), lr=cfg.lr, weight_decay=WEIGHT_DECAY)
    gen = torch.Generator(device="cpu")
    gen.manual_seed(job.seed)
    tbl = job.train
    with torch.random.fork_rng(devices=[]):
        torch.manual_seed(job.seed)  # dropout draws from the global RNG
        for qs in job.steps:
            idx = torch.from_numpy(np.concatenate(
                [np.arange(tbl.first[i], tbl.first[i] + tbl.count[i]) for i in qs]))
            ref.train()
            loss = objective.batch_loss(ref, tbl, idx, loss=cfg.loss, generator=gen)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()

    result = ol.compute_round(job)
    assert result.steps == 3 and result.trained_queries == 12
    for k, v in ref.state_dict().items():
        assert torch.equal(v, result.state_dict[k]), k
    assert lr.apply_round(m, result) is True
    assert m.local_train_steps == 3
    for k, v in ref.state_dict().items():
        assert torch.equal(v, m._net.state_dict()[k]), k


def test_there_is_no_second_objective():
    """The per-node BCE online step and its offline replay are gone."""
    assert not hasattr(RLModel, "update")
    assert not hasattr(offline_trainer, "train_epoch")
    import inspect

    assert "link_sim" not in inspect.signature(offline_trainer.extract_samples).parameters
    assert "link_sim" not in inspect.signature(offline_trainer.resolve_features).parameters
    assert "link_sim_path" not in inspect.signature(offline_trainer.train).parameters


def test_the_linked_slot_mask_is_applied_in_training(tmp_path):
    """arctic: training inputs carry NULL linked slots, as serving does."""
    rng = np.random.default_rng(4)
    task = _event(rng)[0]
    for n in task["nodes_packed"]:
        n["linked_embs"] = [_unit(rng).tolist()]
        n["linked_type_names"] = ["tool"]
    for profile, filled in (("qwen3", True), ("arctic", False)):
        store = ol.VectorStore(D)
        q, _ = ol.labelled_query(task, store, profile=profile, ts_ms=0)
        tbl = ol.build_table([q], store, profile=profile, device=torch.device("cpu"))
        e, t, _s = objective.build_inputs(tbl, torch.arange(q.n), generator=torch.Generator())
        assert bool(e[:, 2:].abs().sum() > 0) is filled


def test_dynamic_type_rows_match_training_after_a_round(tmp_path):
    cfg = ol.OnlineConfig(holdout_queries=2, min_cited_queries=0, purge_gap_ms=0,
                          batch_queries=2, max_steps_per_round=4, lr=1e-2)
    lr = ol.ProjectLearner(profile="qwen3", emb_dim=D, cfg=cfg)
    m = _model(tmp_path)
    rows = [m.get_type_idx("pattern"), m.get_type_idx("decision")]
    assert all(r >= N_ENTITY_TYPES for r in rows)
    rng = np.random.default_rng(5)
    for t in range(12):
        lr.ingest(_event(rng)[0], ts_ms=t)
    lr.run_round_now(m, now_ms=12)
    w = m._net.type_embs.weight
    mean = w[: len(NODE_TYPES)].mean(0)
    for r in rows:
        assert torch.allclose(w[r], mean)


# ─── the gate ──────────────────────────────────────────────────────────


def _ev(**over) -> ol.Evaluation:
    base = dict(at_ms=0, n_queries=300, n_cited=25, ndcg=0.96, cos_ndcg=0.93,
                mrr=0.40, cos_mrr=0.35, search_ndcg=0.93, search_mrr=0.35)
    base.update(over)
    return ol.Evaluation(**base)


@pytest.mark.parametrize("ev, serve, needle", [
    (None, False, "no evaluation"),
    (_ev(n_queries=120), False, "collecting held-out queries (120/300)"),
    (_ev(n_cited=4), False, "only 4 held-out queries name a node"),
    (_ev(ndcg=0.94), False, "needs +2.00%"),
    (_ev(cos_ndcg=0.0, ndcg=0.0), False, "n/a"),
    (_ev(mrr=0.30), False, "below plain cosine's 0.350"),
    (_ev(), True, "over plain cosine"),
    (_ev(mrr=0.35), True, "0.350 >= 0.350"),  # equal MRR passes ("not below")
    (_ev(ndcg=0.93 * 1.02), True, "+2.00%"),   # exactly the bar passes
])
def test_gate_decision_is_the_owner_bar(ev, serve, needle):
    got, why = ol.gate_decision(ev, ol.OnlineConfig())
    assert got is serve and needle in why, why


# ─── determinism, persistence, swaps ──────────────────────────────────


def _drive(tmp_path, name, n=48):
    cfg = ol.OnlineConfig(holdout_queries=8, min_cited_queries=2, purge_gap_ms=0,
                          round_every=8, batch_queries=4, max_steps_per_round=4, lr=3e-3)
    lr = ol.ProjectLearner(profile="qwen3", emb_dim=D, cfg=cfg)
    m = _model(tmp_path, name)
    rng = np.random.default_rng(6)
    for t in range(n):
        lr.ingest(_event(rng)[0], ts_ms=t)
        if lr.round_due():
            lr.run_round_now(m, now_ms=t)
    return lr, m


def test_the_same_stream_gives_the_same_model_and_gate(tmp_path):
    a, ma = _drive(tmp_path, "a")
    b, mb = _drive(tmp_path, "b")
    assert a.last_eval == b.last_eval and a.serving == b.serving and a.counters == b.counters
    for k, v in ma._net.state_dict().items():
        assert torch.equal(v, mb._net.state_dict()[k]), k


def test_state_round_trips_and_a_foreign_checkpoint_resets_it(tmp_path):
    lr, m = _drive(tmp_path, "p")
    m.save()
    lr.model_fingerprint = ol.model_fingerprint(m)
    d = tmp_path / "state"
    lr.save(d)
    back = ol.ProjectLearner.load(d, profile="qwen3", emb_dim=D, model=m, cfg=lr.cfg)
    assert back.counters == lr.counters and back.last_eval == lr.last_eval
    assert back.serving == lr.serving
    assert [q.ts_ms for q in back._queue] == [q.ts_ms for q in lr._queue]
    assert back.holdout_size == lr.holdout_size
    w0, w1 = lr._queue.window()[0], back._queue.window()[0]
    np.testing.assert_array_equal(lr._store.take(w0.node_rows), back._store.take(w1.node_rows))
    np.testing.assert_array_equal(w0.target, w1.target)
    # the checkpoint moved on without this learner (rotated / reset elsewhere)
    m.record_local_train_steps(1)
    fresh = ol.ProjectLearner.load(d, profile="qwen3", emb_dim=D, model=m, cfg=lr.cfg)
    assert fresh.serving is False and len(fresh._queue) == 0
    assert fresh.counters["model_replaced"] == lr.counters["model_replaced"] + 1


def test_a_model_swap_clears_the_window_and_discards_a_running_round(tmp_path):
    lr, m = _drive(tmp_path, "s")
    lr.serving = True
    rng = np.random.default_rng(8)
    for t in range(8):
        lr.ingest(_event(rng)[0], ts_ms=100 + t)
    job = lr.prepare_round(m, now_ms=108)
    assert job is not None
    other = _model(tmp_path, "other")
    lr.on_model_replaced(other, "test")
    assert lr.serving is False and len(lr._queue) == 0 and "model replaced" in lr.reason
    assert lr.apply_round(m, ol.compute_round(job)) is False  # stale generation: dropped


def test_the_vector_store_stays_bounded(tmp_path):
    cfg = ol.OnlineConfig(holdout_queries=4, min_cited_queries=0, purge_gap_ms=0,
                          round_every=4, batch_queries=4, max_steps_per_round=4)
    lr = ol.ProjectLearner(profile="qwen3", emb_dim=D, cfg=cfg)
    m = _model(tmp_path)
    rng = np.random.default_rng(9)
    for t in range(1200):
        lr.ingest(_event(rng)[0], ts_ms=t)
        if lr.round_due():
            lr.run_round_now(m, now_ms=t)
    assert len(lr._store) <= 4096 + 7 * 8 * 2


# ─── the real server, over HTTP ───────────────────────────────────────


PID = "p-online"
HDR = {"X-VCT-Project-ID": PID}


def _server(tmp_path, monkeypatch) -> RLServer:
    monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
    monkeypatch.setenv("RL_MODEL_TAG", "qwen3")
    monkeypatch.setenv("RL_EMB_DIM", str(D))
    monkeypatch.setenv("RL_DEVICE", "cpu")
    monkeypatch.setenv("RL_GLOBAL_WEIGHTS_DIR", str(tmp_path / "global"))
    monkeypatch.delenv("VCT_PROJECT_ID", raising=False)
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    srv = RLServer(project_root=root, ollama_url="http://127.0.0.1:9",
                   log_path=str(tmp_path / "logs" / "rl_events.jsonl"), port=0)
    srv._online_cfg = ol.OnlineConfig(
        holdout_queries=24, min_cited_queries=8, purge_gap_ms=0, round_every=8,
        batch_queries=4, max_steps_per_round=8, lr=3e-3)
    srv._hub_client = None  # never contact a live hub from a test
    return srv


def _update_body(tasks: "list[dict]") -> dict:
    ids = [f"t{i}" for i in range(len(tasks))]
    return {"task_ids": ids, "tasks": dict(zip(ids, tasks)), "active_embedding": "qwen3"}


def _cache_body(cands: "list[dict]", q: "list[float]") -> dict:
    return {"task_id": "c", "nodes": [{"title": c["title"], "node_type": c["node_type"]}
                                      for c in cands],
            "limit": len(cands), "active_embedding": "qwen3",
            "features": wire.encode_rerank_features(q, cands, dim=D)}


def test_a_project_stream_starts_at_search_order_qualifies_and_is_turned_off(
    tmp_path, monkeypatch,
):
    srv = _server(tmp_path, monkeypatch)
    rng = np.random.default_rng(11)

    async def run():
        ts = TestServer(srv._app)
        await ts.start_server()
        base = str(ts.make_url("")).rstrip("/")
        out: dict = {"timeline": []}
        try:
            async with httpx.AsyncClient(base_url=base, timeout=30) as c:
                async def status():
                    return (await c.get("/project_status", headers=HDR)).json()

                async def cache(good=True):
                    task, cands = _event(rng, good=good)
                    r = (await c.post("/cache_nodes", json=_cache_body(cands, task["query_emb"]),
                                      headers=HDR)).json()
                    return r, cands

                # 1) before any event: the client's own order, unchanged
                r, cands = await cache()
                out["first"] = r
                # 2) the good stream until the gate opens
                for i in range(40):
                    body = _update_body([_event(rng)[0] for _ in range(8)])
                    resp = (await c.post("/rl_update", json=body, headers=HDR)).json()
                    assert resp["scheduled"] == 8
                    await srv.wait_for_rounds()
                    st = await status()
                    out["timeline"].append((st["online"]["counters"]["events_seen"], st["serving"]))
                    if st["serving"]:
                        break
                out["qualified"] = await status()
                out["served"] = [await cache() for _ in range(10)]
                # 3) the degraded stream until the gate closes
                for i in range(40):
                    body = _update_body([_event(rng, good=False)[0] for _ in range(8)])
                    await c.post("/rl_update", json=body, headers=HDR)
                    await srv.wait_for_rounds()
                    st = await status()
                    if not st["serving"]:
                        break
                out["degraded"] = await status()
                out["after"] = await cache(good=False)
                # a request without a project id: search order, nothing learned
                out["nohdr"] = (await c.post("/rl_update",
                                             json=_update_body([_event(rng)[0]]))).json()
                out["health"] = (await c.get("/health")).json()
        finally:
            await ts.close()
        return out

    out = asyncio.run(run())

    first = out["first"]
    assert first["scored"] is False and first["serving"] == "search_order"
    assert first["order"] == list(range(6))

    q = out["qualified"]
    assert q["serving"] is True, out["timeline"]
    ev = q["online"]["last_eval"]
    assert ev["ndcg_gain"] >= 0.02 and ev["mrr"] >= ev["cos_mrr"]
    assert "over plain cosine" in q["reason"]
    events_to_qualify = q["online"]["counters"]["events_seen"]
    assert 24 <= events_to_qualify <= 320

    served = out["served"]
    assert all(r["scored"] is True and r["serving"] == "model" for r, _c in served)
    # the useful (tool) node sits in the middle of the search order (rank 2-4);
    # the served model moves it up
    def tool_rank(order, cands):
        return [cands[i]["node_type"] for i in order].index("tool")

    model_rank = np.mean([tool_rank(r["order"], c) for r, c in served])
    search_rank = np.mean([tool_rank(range(len(c)), c) for _r, c in served])
    assert search_rank >= 2 and model_rank <= search_rank - 1, (model_rank, search_rank,
                                                                 out["timeline"])
    assert len(out["timeline"]) > 3  # it qualified by training, not at once

    d = out["degraded"]
    assert d["serving"] is False, d
    assert d["online"]["counters"]["serving_turned_off"] >= 1
    after, _ = out["after"]
    assert after["scored"] is False and after["order"] == list(range(6))

    assert out["nohdr"]["skipped"] == "no_project_id"
    assert out["health"]["online_learning"]["projects_loaded"] == 1

    # persistence: the project's own checkpoint + learner state; the base
    # file other projects start from was never written
    pdir = srv._state_dir / "projects" / PID
    assert (pdir / "rl_model_qwen3_local.pt").is_file()
    assert (pdir / "online_qwen3.json").is_file() and (pdir / "online_qwen3.npz").is_file()
    assert not (srv._state_dir / "rl_model_qwen3_local.pt").exists()

    # a restart picks the same project up where it was
    srv2 = _server(tmp_path, monkeypatch)
    head = asyncio.run(srv2._get_or_load_project_head(PID))
    assert head.learner is not None
    assert head.learner.counters["events_seen"] == d["online"]["counters"]["events_seen"]
    assert head.learner.serving is False
    assert head.learner.counters["model_replaced"] == 0
    assert head.rl_model.local_train_steps > 0


def test_rounds_run_off_the_event_loop_and_never_block_serving(tmp_path, monkeypatch):
    """A slow round must not delay /cache_nodes: the round trains a copy."""
    srv = _server(tmp_path, monkeypatch)
    rng = np.random.default_rng(12)
    gate = asyncio.Event()
    real = ol.compute_round

    def slow(job):
        import time
        while not gate.is_set():
            time.sleep(0.01)
        return real(job)

    monkeypatch.setattr("rl_reranker.rl_server.compute_round", slow)

    async def run():
        ts = TestServer(srv._app)
        await ts.start_server()
        base = str(ts.make_url("")).rstrip("/")
        try:
            async with httpx.AsyncClient(base_url=base, timeout=10) as c:
                r = (await c.post("/rl_update", json=_update_body(
                    [_event(rng)[0] for _ in range(8)]), headers=HDR)).json()
                assert r["round_started"] is True
                task, cands = _event(rng)
                t0 = asyncio.get_running_loop().time()
                ans = (await c.post("/cache_nodes", json=_cache_body(cands, task["query_emb"]),
                                    headers=HDR)).json()
                took = asyncio.get_running_loop().time() - t0
                gate.set()
                await srv.wait_for_rounds()
                return ans, took
        finally:
            await ts.close()

    ans, took = asyncio.run(run())
    assert ans["ok"] is True and took < 2.0
    head = srv._project_heads[(PID, "qwen3")]
    assert head.learner.counters["rounds"] == 1 and not head.learner.round_in_flight


def test_nan_safe_ndcg_gain_property():
    assert math.isnan(_ev(cos_ndcg=0.0).ndcg_gain)


# ─── the launcher's view: the rl_online_state row ─────────────────────


def test_the_pushed_row_fits_migration_0007(tmp_path, monkeypatch):
    """Every field the server pushes is a column of ``rl_online_state`` (the
    hub INSERTs the field names as columns), keyed by the project alone (the
    hub resolves ONE primary-key column)."""
    import sqlite3

    from tests.rl_reranker.test_v0_2_8_lifecycle import _apply_migrations

    db = tmp_path / "m.db"
    _apply_migrations(db, Path(objective.__file__).with_name("db"))
    conn = sqlite3.connect(str(db))
    try:
        info = list(conn.execute("PRAGMA table_info(rl_online_state)"))
    finally:
        conn.close()
    cols = {row[1] for row in info}
    pks = [row[1] for row in info if row[5] > 0]
    assert pks == ["project_id"]

    srv = _server(tmp_path, monkeypatch)
    sent: list = []

    class _Hub:
        async def upsert_row(self, pid, table, key, fields):
            sent.append((pid, table, key, fields))
            return True

    srv._hub_client = _Hub()  # type: ignore[assignment]
    head = asyncio.run(srv._get_or_load_project_head(PID))
    asyncio.run(srv._push_rl_online_state(head))
    ((pid, table, key, fields),) = sent
    assert (pid, table, key) == (PID, "rl_online_state", PID)
    assert set(fields) <= cols, set(fields) - cols


# ─── /global/retrain fits the same objective ──────────────────────────


def _db_pairs(rng, n=24, *, soft_every=0):
    events = []
    for i in range(n):
        task, _cands = _event(rng)
        tid = f"g{i}"
        events.append({"event": "retrieval", "task_id": tid, "ts_ms": i,
                       "query_emb": task["query_emb"],
                       "nodes": [{"title": nd["title"], "node_type": nd["node_type"],
                                  "emb": nd["n_emb"]} for nd in task["nodes_packed"]]})
        cit = {"event": "citation", "task_id": tid, "cosine_sims": task["cosine_sims"],
               "literal_cited": task["literal_cited"]}
        if soft_every and i % soft_every == 0:
            cit.update(soft_label=True, window_tokens=10)  # below the floor: discarded
        events.append(cit)
    return offline_trainer.pair_events(events)


def test_db_pairs_become_the_learners_samples_with_the_offline_policy():
    rng = np.random.default_rng(13)
    store = ol.VectorStore(D)
    qs = offline_trainer.queries_from_pairs(_db_pairs(rng, 12, soft_every=4), store,
                                            profile="qwen3")
    assert len(qs) == 9  # 3 soft events below the window floor are discarded
    assert all(q.n == 6 and q.weight == 1.0 for q in qs)
    assert all(q.cites for q in qs)


def test_fit_steps_on_objective_batch_loss(tmp_path):
    rng = np.random.default_rng(14)
    store = ol.VectorStore(D)
    qs = offline_trainer.queries_from_pairs(_db_pairs(rng, 8), store, profile="qwen3")
    m = _model(tmp_path)
    ref = copy.deepcopy(m._net)
    opt = torch.optim.AdamW(ref.parameters(), lr=1e-3, weight_decay=WEIGHT_DECAY)
    tbl = ol.build_table(qs, store, profile="qwen3", device=torch.device("cpu"))
    gen = torch.Generator()
    gen.manual_seed(5)
    with torch.random.fork_rng(devices=[]):
        torch.manual_seed(5)
        for start in (0, 4):
            idx = torch.from_numpy(np.concatenate(
                [np.arange(tbl.first[j], tbl.first[j] + tbl.count[j]) for j in range(start, start + 4)]))
            ref.train()
            loss = objective.batch_loss(ref, tbl, idx, loss=objective.LossConfig(), generator=gen)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
    steps = ol.fit(m, qs, store, profile="qwen3", epochs=1, batch_queries=4, lr=1e-3, seed=5,
                   shuffle=False)
    assert steps == 2 and m.local_train_steps == 2
    for k, v in ref.state_dict().items():
        if not k.endswith("type_embs.weight"):
            assert torch.equal(v, m._net.state_dict()[k]), k


@pytest.mark.parametrize("mode", ["offline", "online"])
def test_global_retrain_job_trains_with_the_one_objective(tmp_path, monkeypatch, mode):
    srv = _server(tmp_path, monkeypatch)
    rng = np.random.default_rng(15)
    pairs = _db_pairs(rng, 16)
    events = [e for pr in pairs for e in pr]
    monkeypatch.setattr(offline_trainer, "load_training_events", lambda *a, **k: list(events))
    # the job needs A hub client (its events come from the hub); a stub, never the live hub
    srv._hub_client = type("_StubHub", (), {})()  # type: ignore[assignment]
    gpath = tmp_path / "global" / "rl_model_qwen3_0.0.1.pt"
    job_id = "j1"

    async def run():
        from rl_reranker.rl_server import JobState

        srv._jobs[job_id] = JobState(job_id=job_id, kind=f"global_{mode}", state="running")
        await srv._run_global_retrain_job(job_id, mode, gpath, 30, ["pa", "pb"])
        return srv._jobs[job_id]

    job = asyncio.run(run())
    assert job.state == "done", job.message
    assert gpath.is_file()
    trained = RLModel(emb_dim=D, state_path=gpath, device="cpu", embedding_source="qwen3")
    # batch_queries 4: offline = 3 epochs x 32 queries (two projects' 16) / 4;
    # online = one pass per project, 16 / 4 each
    assert trained.local_train_steps == (24 if mode == "offline" else 8)
