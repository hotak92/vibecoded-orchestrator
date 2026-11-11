# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The shard trainer (synthetic shard, CPU) and the evaluation harness math."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from rl_reranker import bundled_weights as bw  # noqa: E402
from rl_reranker.rl_model import NULL_TYPE_IDX, RLModel  # noqa: E402
from rl_reranker.training import eval_harness as ev  # noqa: E402
from rl_reranker.training import export as ex  # noqa: E402
from rl_reranker.training import shard_trainer as st  # noqa: E402

DIM = 32


def make_synthetic_export(root: Path, *, projects=("pa", "pb", "pc"), n_q=60, n_nodes=6,
                          seed=0, profiles=("qwen3", "arctic")) -> Path:
    """An export directory in the documented format, with a learnable signal."""
    rng = np.random.default_rng(seed)
    root.mkdir(parents=True)
    index = {"format_version": ex.EXPORT_FORMAT_VERSION, "projects": list(projects),
             "task_types": ["pre_bash_kg_search"], "label_regimes": ["answer-window-legacy"],
             "profiles": {}}
    for prof in profiles:
        d = root / prof
        d.mkdir()
        vecs, q_vec, q_proj, q_ts, q_first, q_n = [], [], [], [], [], []
        s = {k: [] for k in ("query", "node_vec", "linked_vec", "linked_type", "node_type",
                             "target", "literal_cited", "cos_qn", "score", "shown_rank")}
        direction = rng.normal(size=DIM)
        for qi in range(n_q * len(projects)):
            q = rng.normal(size=DIM)
            vecs.append(q)
            q_vec.append(len(vecs) - 1)
            q_proj.append(qi % len(projects))
            q_ts.append(1_790_000_000_000 + qi * 3_600_000)
            q_first.append(len(s["query"]))
            q_n.append(n_nodes)
            for j in range(n_nodes):
                n = rng.normal(size=DIM)
                vecs.append(n)
                cos = float(q @ n / np.linalg.norm(q) / np.linalg.norm(n))
                bonus = float(n @ direction > 0)  # learnable beyond cosine
                s["query"].append(qi)
                s["node_vec"].append(len(vecs) - 1)
                s["linked_vec"].append([len(vecs) - 1 if j == 0 else -1] + [-1] * 4)
                s["linked_type"].append([2 if j == 0 else NULL_TYPE_IDX] + [NULL_TYPE_IDX] * 4)
                s["node_type"].append(0 if j % 2 else ex.TYPE_CODE_OTHER)
                s["target"].append(min(1.0, max(0.0, 0.4 + 0.2 * cos + 0.35 * bonus)))
                s["literal_cited"].append(int(bonus and j == 0))
                s["cos_qn"].append(cos)
                s["score"].append(cos)
                s["shown_rank"].append(j)
        np.save(d / "vectors.npy", np.asarray(vecs, dtype=np.float16))
        nq = len(q_vec)
        np.savez(d / "queries.npz", vec=np.array(q_vec, np.int32),
                 project=np.array(q_proj, np.int16), ts_hour_ms=np.array(q_ts, np.int64),
                 task_type=np.zeros(nq, np.int16), label_regime=np.zeros(nq, np.int16),
                 soft_label=np.zeros(nq, np.int8), weight=np.ones(nq, np.float32),
                 first_sample=np.array(q_first, np.int64), n_samples=np.array(q_n, np.int32))
        np.savez(d / "samples.npz",
                 query=np.array(s["query"], np.int32), node_vec=np.array(s["node_vec"], np.int32),
                 linked_vec=np.array(s["linked_vec"], np.int32),
                 linked_type=np.array(s["linked_type"], np.int8),
                 node_type=np.array(s["node_type"], np.int8),
                 target=np.array(s["target"], np.float32),
                 cos_citation=np.array(s["target"], np.float32),
                 literal_cited=np.array(s["literal_cited"], np.int8),
                 cited=np.ones(len(s["query"]), np.int8),
                 cos_qn=np.array(s["cos_qn"], np.float32), score=np.array(s["score"], np.float32),
                 shown_rank=np.array(s["shown_rank"], np.int16),
                 emb_truncated=np.full(len(s["query"]), -1, np.int8))
        index["profiles"][prof] = {"queries": nq}
    (root / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return root


@pytest.fixture()
def shard(tmp_path):
    return make_synthetic_export(tmp_path / "export")


# ─── trainer ───────────────────────────────────────────────────────────


def test_trainer_smoke_learns_and_saves_a_serving_checkpoint(shard, tmp_path):
    data = st.load_training_data([shard], "qwen3", device="cpu")
    splits = st.make_splits(data, "pc", dev_project="pb")
    assert set(splits) == {"train", "val", "time_holdout", "project_holdout", "dev"}
    allq = np.concatenate(list(splits.values()))
    assert len(allq) == len(np.unique(allq)) == len(data.q_first)  # disjoint, complete
    assert np.all(data.q_project[splits["project_holdout"]] == data.project_names.index("pc"))
    assert data.q_ts[splits["time_holdout"]].min() >= data.q_ts[splits["val"]].max()
    run = st.train_one(data, splits, lr=1e-3, epochs=3, patience=3, batch_size=64,
                       loss=st.LossConfig(pointwise=1.0, pair=0.5, listwise=0.5),
                       log=lambda m: None)
    assert run.best_epoch >= 1 and math.isfinite(run.best_val_ndcg)
    assert {"dev_ndcg10", "dev_mrr10"} <= set(run.history[-1])
    net = st.new_net(DIM, data.device, 0)
    net.load_state_dict(run.state_dict)
    rep = st.evaluate_holdouts(data, net, splits, "pc")
    assert rep["project_holdout"]["n_queries"] == len(splits["project_holdout"])
    ckpt = tmp_path / "out" / "qwen3" / "0.9.0" / "rl_model_qwen3_0.9.0.pt"
    sha = st.save_checkpoint(run.state_dict, ckpt, profile="qwen3", version="0.9.0", emb_dim=DIM)
    assert sorted(p.name for p in ckpt.parent.iterdir()) == [ckpt.name]  # nothing beside it
    raw = torch.load(ckpt, map_location="cpu", weights_only=True)
    assert raw["opt_state"]["state"] == {}  # no optimizer moments shipped
    m = RLModel(emb_dim=DIM, state_path=ckpt, device="cpu", embedding_source="qwen3")
    assert (m.weights_version, m.base_weights_version, m.local_train_steps) == ("0.9.0", "0.9.0", 0)
    assert len(sha) == 64


def test_training_is_deterministic_for_a_seed(shard):
    data = st.load_training_data([shard], "qwen3", device="cpu")
    splits = st.make_splits(data, "pc")
    a = st.train_one(data, splits, lr=1e-3, epochs=1, batch_size=64, seed=7, log=lambda m: None)
    b = st.train_one(data, splits, lr=1e-3, epochs=1, batch_size=64, seed=7, log=lambda m: None)
    for k in a.state_dict:
        assert torch.equal(a.state_dict[k], b.state_dict[k]), k


def test_linked_slots_are_masked_for_arctic_only(shard):
    for prof, expect_linked in (("qwen3", True), ("arctic", False)):
        data = st.load_training_data([shard], prof, device="cpu")
        idx = torch.arange(len(data.np_target))
        g = torch.Generator().manual_seed(0)
        ent, types, scal = st.build_inputs(data, idx, generator=g)
        has = data.linked_vec[:, 0] >= 0
        filled = ent[:, 2:, :].abs().sum(-1).sum(-1) > 0
        assert bool(filled[has].all()) is expect_linked
        assert not bool(filled[~has].any())
        if not expect_linked:
            assert bool((types[:, 2:] == NULL_TYPE_IDX).all())
            assert bool((scal[:, 2:, :] == 0).all())


def test_other_type_row_is_the_mean_of_the_builtins():
    net = st.new_net(DIM, torch.device("cpu"), 0)
    st.sync_other_type_row(net)
    w = net.type_embs.weight
    assert torch.allclose(w[st.OTHER_TYPE_ROW], w[:6].mean(0))


def test_project_cap_subsamples_the_dominant_project():
    rng = np.random.default_rng(0)
    q = np.arange(1000)
    proj = np.array([0] * 900 + [1] * 100)
    out = st.epoch_queries(q, proj, 0.5, rng)
    share = (proj[out] == 0).mean()
    assert 0.4 < share < 0.6
    assert len(set(out.tolist())) == len(out)


def test_listwise_loss_is_zero_for_a_perfect_order_and_ignores_node_priors():
    tgt = torch.tensor([[0.9], [0.5], [0.1], [0.7], [0.2]])
    qid = torch.tensor([0, 0, 0, 1, 1])
    w = torch.ones_like(tgt)
    assert float(st.within_query_listwise_loss(tgt.clone(), tgt, w, qid)) == pytest.approx(0, abs=1e-6)
    shifted = tgt + torch.tensor([[0.0], [0.0], [0.0], [0.05], [0.05]])  # per-query offset
    assert float(st.within_query_listwise_loss(shifted, tgt, w, qid)) == pytest.approx(0, abs=1e-6)
    wrong = torch.tensor([[0.1], [0.5], [0.9], [0.2], [0.7]])
    assert float(st.within_query_listwise_loss(wrong, tgt, w, qid)) > 0.1


def test_pair_loss_only_counts_pairs_of_the_same_query():
    pred = torch.tensor([[0.5], [0.5], [0.0]])
    tgt = torch.tensor([[0.5], [0.5], [1.0]])  # query 1 is a single node -> no pairs
    w = torch.ones_like(tgt)
    assert float(st.within_query_pair_loss(pred, tgt, w, torch.tensor([0, 0, 1]))) == 0.0


def test_ship_requires_both_profiles_passing(shard, tmp_path):
    weights = tmp_path / "weights"
    weights.mkdir()
    (weights / bw.MANIFEST_NAME).write_text(json.dumps({"schema_version": 1, "profiles": {
        "qwen3": {"current": None, "versions": {}},
        "arctic": {"current": None, "versions": {}}}}), encoding="utf-8")
    reports = {}
    for prof in ("qwen3", "arctic"):
        sd = st.new_net(DIM, torch.device("cpu"), 0).state_dict()
        ck = tmp_path / "out" / prof / "0.9.0" / f"rl_model_{prof}_0.9.0.pt"
        sha = st.save_checkpoint(sd, ck, profile=prof, version="0.9.0", emb_dim=DIM)
        rp = tmp_path / f"report_{prof}.json"
        rp.write_text(json.dumps({"profile": prof, "decision": {"passes": True},
                                  "checkpoint": {"path": str(ck), "version": "0.9.0",
                                                 "sha256": sha}}), encoding="utf-8")
        reports[prof] = rp
    with pytest.raises(ValueError, match="ship together"):
        st.ship([reports["qwen3"]], weights)
    failing = json.loads(reports["arctic"].read_text())
    failing["decision"]["passes"] = False
    bad = tmp_path / "bad_arctic.json"
    bad.write_text(json.dumps(failing))
    with pytest.raises(ValueError, match="nothing ships"):
        st.ship([reports["qwen3"], bad], weights)
    assert bw.verify(weights) == {"qwen3": None, "arctic": None}
    st.ship([reports["qwen3"], reports["arctic"]], weights)
    assert bw.verify(weights) == {"qwen3": "0.9.0", "arctic": "0.9.0"}


# ─── eval harness math ────────────────────────────────────────────────


def test_ndcg_on_a_known_ranking():
    scores = np.array([3.0, 2.0, 1.0])
    gains = np.array([0.0, 0.5, 1.0])
    dcg = 0.5 / math.log2(3) + 1.0 / math.log2(4)
    idcg = 1.0 + 0.5 / math.log2(3)
    assert ev.ndcg_at_k(scores, gains, 10) == pytest.approx(dcg / idcg)
    assert ev.ndcg_at_k(-scores, gains, 10) == pytest.approx(1.0)
    assert ev.ndcg_at_k(scores, gains, 1) == pytest.approx(0.0)
    assert ev.ndcg_at_k(scores, np.zeros(3), 10) is None


def test_ties_keep_logged_order():
    assert ev.ndcg_at_k(np.zeros(3), np.array([1.0, 0.0, 0.0]), 1) == 1.0
    assert ev.ndcg_at_k(np.zeros(3), np.array([0.0, 1.0, 0.0]), 1) == 0.0


def test_mrr_and_recall():
    s = np.array([3.0, 2.0, 1.0, 0.0])
    rel = np.array([False, True, False, True])
    assert ev.mrr_at_k(s, rel, 10) == pytest.approx(0.5)
    assert ev.mrr_at_k(s, rel, 1) == 0.0
    assert ev.recall_at_k(s, rel, 2) == pytest.approx(0.5)
    assert ev.recall_at_k(s, rel, 10) == pytest.approx(1.0)
    assert ev.mrr_at_k(s, np.zeros(4, bool), 10) is None


def _split_report(model_better: bool, n=200, proj_names=("p0", "p1")):
    rng = np.random.default_rng(1)
    first = np.arange(n) * 4
    count = np.full(n, 4)
    gains = rng.random(n * 4)
    good = gains + rng.normal(0, 0.05, n * 4)
    bad = rng.random(n * 4)
    return ev.evaluate_split(
        model_scores=good if model_better else bad, cosine_scores=bad if model_better else good,
        gains=gains, relevant=gains > 0.8, first=first, count=count,
        project=np.arange(n) % 2, task_type=np.zeros(n, int),
        project_names=list(proj_names), task_type_names=["t"], n_boot=200,
    )


def test_ship_decision_applies_the_bar():
    good = _split_report(True)
    bad = _split_report(False)
    assert good["overall"]["ndcg"]["rel_gain"] > 0.02
    lo, hi = good["overall"]["ndcg"]["rel_gain_ci95"]
    assert lo <= good["overall"]["ndcg"]["rel_gain"] <= hi
    assert ev.ship_decision(good, good)["passes"] is True
    d = ev.ship_decision(good, bad)
    assert d["passes"] is False and any("project hold-out" in r for r in d["reasons"])
    # a single judged project worse than cosine blocks, even with a good overall
    mixed = json.loads(json.dumps(good))
    mixed["per_project_ndcg"]["p1"].update(model=0.1, cosine=0.2, worse_than_cosine=True)
    assert ev.ship_decision(mixed, good)["passes"] is False
    md = ev.render_markdown([{
        "profile": "qwen3", "holdout_project": "p1", "decision": d,
        "training": {"n_train_queries": 1, "best_lr": 1e-4, "epochs_run": 1, "runtime_s": 1.0},
        "time_holdout": good, "project_holdout": bad}])
    assert "does not pass" in md and "p1" in md
