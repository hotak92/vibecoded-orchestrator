# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Per-project online learning + the serving gate (v0.2.106).

Owner rulings 2026-11-12: per-project ONLINE adaptation, opt-in; a project's
model is served only while its cumulative prequential record (every query
scored, at the first round after it arrived, by the model in place at that
round, before that model trained on it) shows,
at an anytime-valid confidence, NDCG@10 >= +2% over plain cosine AND the
cited node's MRR@10 not below cosine's (test_gate_prequential.py).

Two layers:

* the learner (``online_learning``): scoring before training, the round's
  objective (exactly ``objective.batch_loss`` + AdamW), determinism,
  persistence, model swaps, dynamic-type rows;
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


# ─── prequential: scored before trained on ────────────────────────────


def test_every_query_is_scored_by_the_model_before_any_step_trains_on_it(tmp_path):
    """A round scores its arrivals with the network as it was BEFORE the
    round's steps, and a query never reaches a training batch before the
    round that scored it."""
    cfg = ol.OnlineConfig(round_every=6, batch_queries=4, max_steps_per_round=2, lr=1e-2)
    lr = ol.ProjectLearner(profile="qwen3", emb_dim=D, cfg=cfg)
    m = _model(tmp_path)
    rng = np.random.default_rng(0)
    for t in range(60):
        lr.ingest(_event(rng)[0], ts_ms=t)
        if not lr.round_due():
            continue
        job = lr.prepare_round(m, now_ms=t)
        assert job is not None and job.fresh is not None
        before = ol.score_queries(copy.deepcopy(job.net), job.fresh, cfg.k)
        res = ol.compute_round(job)
        assert res.scores is not None
        np.testing.assert_array_equal(res.scores.model.ndcg, before[0].ndcg)
        np.testing.assert_array_equal(res.scores.model.mrr, before[0].mrr)
        assert lr.apply_round(m, res) is True
        # nothing trained so far that the record does not already hold
        assert lr.counters["queries_trained"] <= len(lr.record)
    assert len(lr.record) == lr.counters["events_seen"] - len(lr._fresh)
    assert lr.counters["queries_trained"] <= len(lr.record)
    assert lr.counters["queries_trained"] > 0


def test_a_query_ingested_with_training_off_is_scored_but_never_trained(tmp_path):
    cfg = ol.OnlineConfig(round_every=8, batch_queries=4, max_steps_per_round=4)
    lr = ol.ProjectLearner(profile="qwen3", emb_dim=D, cfg=cfg)
    m = _model(tmp_path)
    rng = np.random.default_rng(1)
    for t in range(16):
        lr.ingest(_event(rng)[0], ts_ms=t, train=False)
        if lr.round_due():
            lr.run_round_now(m, now_ms=t)
    assert len(lr.record) == 16
    assert lr.counters["queries_trained"] == 0 and lr.counters["dropped_untrained"] == 16
    assert m.local_train_steps == 0


def test_the_served_flag_is_the_gate_state_when_the_query_arrived(tmp_path):
    cfg = ol.OnlineConfig(round_every=4, batch_queries=4)
    lr = ol.ProjectLearner(profile="qwen3", emb_dim=D, cfg=cfg)
    m = _model(tmp_path)
    rng = np.random.default_rng(2)
    flags = []
    for t in range(12):
        lr.serving = t >= 6         # as if a check had opened the gate at t = 6
        flags.append(lr.serving)
        lr.ingest(_event(rng)[0], ts_ms=t)
        if lr.round_due():
            lr.run_round_now(m, now_ms=t)
    np.testing.assert_array_equal(lr.record.served, flags)


# ─── a round is exactly the offline objective ─────────────────────────


def test_a_round_steps_on_objective_batch_loss_with_adamw(tmp_path):
    cfg = ol.OnlineConfig(round_every=1000, batch_queries=4, max_steps_per_round=3, lr=1e-3)
    lr = ol.ProjectLearner(profile="qwen3", emb_dim=D, cfg=cfg)
    rng = np.random.default_rng(3)
    for t in range(20):
        lr.ingest(_event(rng)[0], ts_ms=t)
    m = _model(tmp_path)
    job = lr.prepare_round(m, now_ms=20)
    assert job is not None and len(job.steps) == 3

    # the reference: a fresh copy of the same net, scored first (the round's
    # prequential scoring), then AdamW on batch_loss with the same seed
    ref = copy.deepcopy(m._net)
    with torch.random.fork_rng(devices=[]):
        torch.manual_seed(job.seed)
        ol.score_queries(ref, job.fresh, cfg.k)
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
    cfg = ol.OnlineConfig(batch_queries=2, max_steps_per_round=4, lr=1e-2)
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


# ─── the gate: tests/rl_reranker/test_gate_prequential.py ─────────────


# ─── determinism, persistence, swaps ──────────────────────────────────


def _drive(tmp_path, name, n=48):
    cfg = ol.OnlineConfig(min_queries=8, min_cited_queries=2, round_every=8,
                          batch_queries=4, max_steps_per_round=4, lr=3e-3)
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
    assert a.last_check == b.last_check and a.serving == b.serving and a.counters == b.counters
    assert a.checks == b.checks > 0
    for c in ol.RECORD_COLUMNS:
        np.testing.assert_array_equal(a.record.column(c), b.record.column(c))
    for k, v in ma._net.state_dict().items():
        assert torch.equal(v, mb._net.state_dict()[k]), k


def test_state_round_trips_and_a_foreign_checkpoint_resets_it(tmp_path):
    lr, m = _drive(tmp_path, "p")
    rng = np.random.default_rng(13)
    for t in range(5):              # arrivals not scored yet, one of them frozen
        lr.ingest(_event(rng)[0], ts_ms=1000 + t, train=t != 2)
    m.save()
    lr.model_fingerprint = ol.model_fingerprint(m)
    d = tmp_path / "state"
    lr.save(d)
    back = ol.ProjectLearner.load(d, profile="qwen3", emb_dim=D, model=m, cfg=lr.cfg)
    assert back.counters == lr.counters and back.last_check == lr.last_check
    assert back.serving == lr.serving and back.checks == lr.checks
    assert len(back.record) == len(lr.record)
    queued = list(lr._fresh) + list(lr._pending)
    back_q = list(back._fresh) + list(back._pending)
    assert queued and [q.ts_ms for q in back_q] == [q.ts_ms for q in queued]
    w0, w1 = queued[0], back_q[0]
    np.testing.assert_array_equal(lr._store.take(w0.node_rows), back._store.take(w1.node_rows))
    np.testing.assert_array_equal(w0.target, w1.target)
    assert [(q.served, q.trainable) for q in back_q] == [(q.served, q.trainable) for q in queued]
    assert [q.trainable for q in back._fresh] == [True, True, False, True, True]
    # the checkpoint moved on without this learner (rotated / reset elsewhere)
    m.record_local_train_steps(1)
    fresh = ol.ProjectLearner.load(d, profile="qwen3", emb_dim=D, model=m, cfg=lr.cfg)
    assert fresh.serving is False and len(fresh.record) == 0 and not fresh._pending
    assert fresh.counters["model_replaced"] == lr.counters["model_replaced"] + 1


def test_a_model_swap_clears_the_record_and_discards_a_running_round(tmp_path):
    lr, m = _drive(tmp_path, "s")
    lr.serving = True
    rng = np.random.default_rng(8)
    for t in range(8):
        lr.ingest(_event(rng)[0], ts_ms=100 + t)
    job = lr.prepare_round(m, now_ms=108)
    assert job is not None
    other = _model(tmp_path, "other")
    lr.on_model_replaced(other, "test")
    assert lr.serving is False and len(lr.record) == 0 and lr.checks == 0
    assert not lr._fresh and not lr._pending and "model replaced" in lr.reason
    assert lr.apply_round(m, ol.compute_round(job)) is False  # stale generation: dropped


def test_the_vector_store_stays_bounded(tmp_path):
    cfg = ol.OnlineConfig(round_every=4, batch_queries=4, max_steps_per_round=4)
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
    srv = RLServer(project_root=root,
                   log_path=str(tmp_path / "logs" / "rl_events.jsonl"), port=0)
    srv._online_cfg = ol.OnlineConfig(
        min_queries=24, min_cited_queries=8, round_every=8,
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
    ev = q["online"]["last_check"]
    assert ev["ndcg_gain"] >= 0.02 and ev["mrr"] >= ev["cos_mrr"]
    assert ev["ndcg_gain_lb"] >= 0.02 and ev["mrr_diff_lb"] >= 0.0
    assert ev["k"] == q["online"]["checks"] >= 1
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
    chk = ol.Check(k=1, at_ms=0, confidence=0.95, n_queries=0, n_cited=0, ndcg=0.0,
                   cos_ndcg=0.0, mrr=float("nan"), cos_mrr=float("nan"),
                   ndcg_gain_lb=float("-inf"), mrr_diff_lb=float("-inf"), serve=False, reason="")
    assert math.isnan(chk.ndcg_gain)
    d = chk.as_dict()
    assert d["ndcg_gain"] is None and d["ndcg_gain_lb"] is None and d["mrr"] is None


# ─── the launcher's view: the rl_online_state row ─────────────────────


def test_the_pushed_row_fits_the_migrations(tmp_path, monkeypatch):
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

    async def run():
        head = await srv._get_or_load_project_head(PID)
        await srv.wait_for_rounds()      # the push the load itself makes
        await srv._push_rl_online_state(head)

    asyncio.run(run())
    assert len(sent) == 2
    for pid, table, key, fields in sent:
        assert (pid, table, key) == (PID, "rl_online_state", PID)
        assert set(fields) <= cols, set(fields) - cols


def test_a_head_load_republishes_the_row_so_a_stale_serving_claim_is_replaced(
        tmp_path, monkeypatch):
    """The launcher's panel reads the module-DB row, not the container. A row
    written by an earlier container (``serving = 1``) must not outlive a
    container that no longer holds that model (recreated without its state,
    or its learner state discarded): the first load of the project's head
    re-publishes what this container actually has."""
    srv = _server(tmp_path, monkeypatch)
    sent: list = []

    class _Hub:
        async def upsert_row(self, pid, table, key, fields):
            sent.append((pid, table, key, dict(fields)))
            return True

    srv._hub_client = _Hub()  # type: ignore[assignment]

    async def run():
        await srv._get_or_load_project_head(PID)
        await srv._get_or_load_project_head(PID)   # a cached head: no second push
        await srv.wait_for_rounds()

    asyncio.run(run())
    assert [(p, t, k) for p, t, k, _f in sent] == [(PID, "rl_online_state", PID)]
    assert sent[0][3]["serving"] == 0 and sent[0][3]["events_seen"] == 0


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


def test_first_global_retrain_seeds_from_the_requesting_projects_newest_checkpoint(
        tmp_path, monkeypatch):
    """F1: with no global model yet, the job starts from the requesting
    project's newest real checkpoint (the writers' ``rl_model_<tag>_<ver>.pt``
    naming), never from a fresh init — and a ``_finetuned`` sibling is not a seed."""
    import os

    from rl_reranker.rl_server import JobState, _global_retrain_seed

    srv = _server(tmp_path, monkeypatch)
    rng = np.random.default_rng(16)
    events = [e for pr in _db_pairs(rng, 16) for e in pr]
    monkeypatch.setattr(offline_trainer, "load_training_events", lambda *a, **k: list(events))
    srv._hub_client = type("_StubHub", (), {})()  # type: ignore[assignment]
    pdir = srv._state_dir / "projects" / "pa"
    pdir.mkdir(parents=True)

    def ckpt(name: str, steps: int, mtime: int) -> Path:
        with _seeded_init(3, enabled=True):
            m = RLModel(emb_dim=D, state_path=pdir / name, device="cpu", embedding_source="qwen3")
        m.record_local_train_steps(steps)
        m.save()
        os.utime(pdir / name, ns=(mtime, mtime))
        return pdir / name

    ckpt("rl_model_qwen3_0.0.1.pt", 5, 1_000)
    newest = ckpt("rl_model_qwen3_local.pt", 7, 2_000)
    ckpt("rl_model_qwen3_local_finetuned.pt", 99, 3_000)
    assert _global_retrain_seed(srv._state_dir, "pa", "qwen3") == newest
    # no project checkpoint -> the base dir's; nothing at all -> None
    assert _global_retrain_seed(tmp_path / "empty", "pz", "qwen3") is None

    gpath = tmp_path / "global" / "rl_model_qwen3_local.pt"

    async def run():
        srv._jobs["j"] = JobState(job_id="j", kind="global_online", state="running")
        await srv._run_global_retrain_job("j", "online", gpath, 30, ["pa"],
                                          seed_project_id="pa")
        return srv._jobs["j"]

    job = asyncio.run(run())
    assert job.state == "done", job.message
    trained = RLModel(emb_dim=D, state_path=gpath, device="cpu", embedding_source="qwen3")
    # resumed from the seed's 7 steps, plus one online pass of 16 queries / 4
    assert trained.local_train_steps == 7 + 4
