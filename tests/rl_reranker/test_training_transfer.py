# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Transfer tooling of the RL trainer: warm starts, the cosine-neutral network,
node-level dropout, L2-SP, leave-one-project-out and the online-adaptation
simulation (synthetic shard, CPU)."""
from __future__ import annotations

import json
import math

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from rl_reranker.training import eval_harness as ev  # noqa: E402
from rl_reranker.training import shard_trainer as st  # noqa: E402
from rl_reranker.training import transfer as tr  # noqa: E402

from tests.rl_reranker.test_shard_trainer_and_eval import DIM, make_synthetic_export  # noqa: E402


@pytest.fixture()
def data(tmp_path):
    root = make_synthetic_export(tmp_path / "export", projects=("pa", "pb", "pc", "pd"),
                                 n_q=50)
    return st.load_training_data([root], "qwen3", device="cpu")


def _args(tmp_path, **over):
    p = tr.build_parser()
    argv = ["lopo", "--data", "x", "--profile", "qwen3", "--out", str(tmp_path / "out"),
            "--lrs", "1e-3", "--epochs", "1", "--batch-size", "64", "--device", "cpu"]
    a = p.parse_args(argv)
    for k, v in over.items():
        setattr(a, k, v)
    return a


# ─── splits ───────────────────────────────────────────────────────────


def test_extra_holdouts_join_the_project_holdout_and_never_train(data):
    s = st.make_splits(data, "pa", extra_holdouts=("pd",))
    held = {data.project_names[i] for i in np.unique(data.q_project[s["project_holdout"]])}
    assert held == {"pa", "pd"}
    trained = {data.project_names[i] for i in np.unique(data.q_project[
        np.concatenate([s["train"], s["val"], s["time_holdout"]])])}
    assert trained == {"pb", "pc"}
    allq = np.concatenate(list(s.values()))
    assert len(allq) == len(np.unique(allq)) == len(data.q_first)
    with pytest.raises(ValueError, match="repeat"):
        st.make_splits(data, "pa", extra_holdouts=("pa",))
    with pytest.raises(ValueError, match="not in the data"):
        st.make_splits(data, "pa", extra_holdouts=("nope",))
    with pytest.raises(ValueError, match="dev project"):
        st.make_splits(data, "pa", extra_holdouts=("pd",), dev_project="pd")


def test_purge_leaves_a_gap_before_each_later_split(data):
    plain = st.make_splits(data, "pa")
    assert "purged" not in plain  # default: no purge, layout unchanged
    s = st.make_splits(data, "pa", purge_hours=5)
    ts = data.q_ts
    hour = 3_600_000
    assert ts[s["time_holdout"]].min() - ts[s["val"]].max() >= 5 * hour
    assert ts[s["val"]].min() - ts[s["train"]].max() >= 5 * hour
    assert np.array_equal(np.sort(s["time_holdout"]), np.sort(plain["time_holdout"]))
    assert len(s["purged"]) > 0
    allq = np.concatenate(list(s.values()))
    assert len(allq) == len(np.unique(allq)) == len(data.q_first)  # still disjoint + complete


# ─── starting points + regularisers ───────────────────────────────────


def _per_query_spearman(data, scores, qids):
    sidx, first, count = st._split_arrays(data, qids)
    cos = st.cosine_scores(data, sidx)
    out = []
    for f, c in zip(first, count):
        a = np.argsort(np.argsort(scores[f:f + c]))
        b = np.argsort(np.argsort(cos[f:f + c]))
        out.append(np.corrcoef(a, b)[0, 1])
    return float(np.mean(out))


def test_cosine_neutral_state_ranks_like_cosine(data):
    q = np.arange(len(data.q_first))
    state = st.cosine_neutral_state(data, q, steps=600, batch_size=128, lr=2e-3, seed=3,
                                    log=lambda m: None)
    net = st.new_net(DIM, data.device, 0)
    net.load_state_dict(state)
    sidx, _, _ = st._split_arrays(data, q)
    rho = _per_query_spearman(data, st.score_samples(data, net, sidx), q)
    fresh = st.new_net(DIM, data.device, 0)
    rho_fresh = _per_query_spearman(data, st.score_samples(data, fresh, sidx), q)
    assert rho > 0.9 > rho_fresh  # distilled: ordered like cosine; a fresh net is not


def test_load_init_state_round_trips_and_refuses_a_mismatch(data, tmp_path):
    sd = st.new_net(DIM, data.device, 5).state_dict()
    ck = tmp_path / "ck" / "rl_model_qwen3_0.1.0.pt"
    st.save_checkpoint(sd, ck, profile="qwen3", version="0.1.0", emb_dim=DIM)
    before = ck.read_bytes()
    got = st.load_init_state(ck, DIM)
    for k in sd:
        if k == "type_embs.weight":  # dynamic rows are truncated on save
            assert torch.equal(got[k][:st.N_ENTITY_TYPES], sd[k][:st.N_ENTITY_TYPES])
        else:
            assert torch.equal(got[k], sd[k]), k
    assert ck.read_bytes() == before  # the source checkpoint is never touched
    assert sorted(p.name for p in ck.parent.iterdir()) == [ck.name]
    with pytest.raises(ValueError, match="arch"):
        st.load_init_state(ck, DIM, arch="v1")
    with pytest.raises(ValueError, match="dim"):
        st.load_init_state(ck, DIM * 2)
    v1 = tmp_path / "v1" / "rl_model_qwen3_0.1.0.pt"
    st.save_checkpoint(st.new_net(DIM, data.device, 5, "v1").state_dict(), v1,
                       profile="qwen3", version="0.1.0", emb_dim=DIM, arch="v1")
    assert set(st.load_init_state(v1, DIM, arch="v1")) == set(
        st.new_net(DIM, data.device, 0, "v1").state_dict())


def test_warm_start_starts_from_the_given_state(data):
    s = st.make_splits(data, "pa")
    init = st.new_net(DIM, data.device, 99).state_dict()
    run = st.train_one(data, s, lr=1e-12, epochs=1, batch_size=64, init_state=init,
                       log=lambda m: None)
    for k in init:
        if k != "type_embs.weight":
            assert torch.allclose(run.state_dict[k], init[k], atol=1e-6), k


def test_drop_entities_zeroes_whole_samples_only():
    e = torch.ones(1000, 7, 4)
    g = torch.Generator().manual_seed(0)
    assert torch.equal(st.drop_entities(e, 0.0, generator=g), e)
    assert float(st.drop_entities(e, 1.0, generator=g).abs().sum()) == 0.0
    half = st.drop_entities(e, 0.5, generator=g)
    per_sample = half.sum(dim=(1, 2))
    assert set(per_sample.unique().tolist()) <= {0.0, 28.0}
    assert 0.4 < float((per_sample == 0).float().mean()) < 0.6


def test_l2sp_holds_the_network_at_its_start(data):
    s = st.make_splits(data, "pa")
    init = st.new_net(DIM, data.device, 4).state_dict()

    def dist(sd):
        return sum(float(((sd[k] - init[k]) ** 2).sum()) for k in init if k != "type_embs.weight")

    free = st.train_one(data, s, lr=3e-3, epochs=2, batch_size=64, init_state=init,
                        log=lambda m: None)
    held = st.train_one(data, s, lr=3e-3, epochs=2, batch_size=64, init_state=init,
                        l2sp=10.0, log=lambda m: None)
    assert dist(held.state_dict) < 0.5 * dist(free.state_dict)
    net = st.new_net(DIM, data.device, 0)
    anchor = {k: v.detach().clone() for k, v in net.named_parameters()}
    assert float(st.l2sp_penalty(net, anchor)) == 0.0


def test_select_on_dev_needs_a_dev_split(data):
    s = st.make_splits(data, "pa")
    with pytest.raises(ValueError, match="select_on"):
        st.train_one(data, s, lr=1e-3, epochs=1, select_on="dev", log=lambda m: None)
    s = st.make_splits(data, "pa", dev_project="pb")
    run = st.train_one(data, s, lr=1e-3, epochs=2, batch_size=64, select_on="dev",
                       log=lambda m: None)
    best = max(run.history, key=lambda r: r["dev_ndcg10"])
    assert run.best_epoch == best["epoch"]  # chosen by the dev project, not by val


# ─── leave-one-project-out ────────────────────────────────────────────


def test_plan_folds_splits_by_size(tmp_path):
    root = make_synthetic_export(tmp_path / "e2", projects=("pa", "pb", "pc"), n_q=40)
    d = st.load_training_data([root], "qwen3", device="cpu")
    # make "pc" small: re-label most of its queries as "pa"
    pc, pa = d.project_names.index("pc"), d.project_names.index("pa")
    pc_q = np.flatnonzero(d.q_project == pc)
    d.q_project[pc_q[10:]] = pa
    folds, probes = tr.plan_folds(d, min_fold_queries=30, min_judged=5)
    assert folds == ["pa", "pb"] and probes == ["pc"]  # largest first
    folds, probes = tr.plan_folds(d, min_fold_queries=30, min_judged=20)
    assert probes == []  # too small to judge: neither a fold nor a probe
    with pytest.raises(ValueError, match=">= 2 projects"):
        tr.plan_folds(d, min_fold_queries=10 ** 6)


def _fold(gains, passes=True):
    return {"holdout_project": "x", "unseen_gains": gains,
            "decision": {"passes": passes, "reasons": [] if passes else ["time hold-out: low"]}}


def test_lopo_verdict_judges_every_unseen_project_on_its_own():
    assert tr.lopo_verdict([_fold({"a": 0.03, "b": 0.025})])["passes"] is True
    v = tr.lopo_verdict([_fold({"a": 0.05, "b": 0.01})])  # pooled could pass; b alone does not
    assert v["passes"] is False and any("unseen project b" in r for r in v["reasons"])
    assert tr.lopo_verdict([_fold({"a": 0.05}), _fold({"a": 0.05}, passes=False)])["passes"] is False
    assert tr.lopo_verdict([_fold({"a": None})])["passes"] is False


def test_run_lopo_trains_one_fold_per_project_and_keeps_probes_out(tmp_path):
    # 110 queries per project: above the eval harness' judging floor (100)
    root = make_synthetic_export(tmp_path / "e3", projects=("pa", "pb", "pc"), n_q=110)
    data = st.load_training_data([root], "qwen3", device="cpu")
    a = _args(tmp_path, min_fold_queries=1, folds=["pa", "pb"])
    rep = tr.run_lopo(data, a)
    assert [f["holdout_project"] for f in rep["folds"]] == ["pa", "pb"]
    for f in rep["folds"]:
        assert f["probe_projects"] == []
        assert set(f["unseen_gains"]) == {f["holdout_project"]}
        assert set(f["project_holdout"]["per_project_ndcg"]) == {f["holdout_project"]}
        trained = set(f["time_holdout"]["per_project_ndcg"])
        assert f["holdout_project"] not in trained and trained
        ck = tmp_path / "out" / "folds" / "qwen3" / f["holdout_project"] / "rl_model_qwen3_lopo.pt"
        assert f["checkpoint"]["path"] == str(ck) and ck.is_file()
    # a second profile into the same --out keeps the first profile's checkpoints
    arctic = st.load_training_data([root], "arctic", device="cpu")
    a2 = _args(tmp_path, min_fold_queries=1, folds=["pa"], profile="arctic")
    tr.run_lopo(arctic, a2)
    assert (tmp_path / "out" / "folds" / "qwen3" / "pa" / "rl_model_qwen3_lopo.pt").is_file()
    assert (tmp_path / "out" / "folds" / "arctic" / "pa" / "rl_model_arctic_lopo.pt").is_file()
    assert isinstance(rep["verdict"]["passes"], bool)
    assert "Leave-one-project-out" in tr.render_lopo_markdown(rep)
    json.dumps(rep)  # the report is JSON-serialisable
    with pytest.raises(ValueError, match="not a fold project"):
        tr.run_lopo(data, _args(tmp_path, min_fold_queries=1, folds=["zz"]))


# ─── online adaptation simulation ─────────────────────────────────────


def test_project_stream_is_time_ordered_and_disjoint(data):
    stream, test = tr.project_stream(data, "pc", 0.25)
    assert data.q_ts[stream].max() <= data.q_ts[test].min()
    assert len(test) == round(0.25 * (len(stream) + len(test)))
    assert np.all(data.q_project[np.concatenate([stream, test])] == data.project_names.index("pc"))
    with pytest.raises(ValueError):
        tr.project_stream(data, "pc", 1.0)
    gapped, test2 = tr.project_stream(data, "pc", 0.25, gap_hours=20)
    assert np.array_equal(test2, test) and len(gapped) < len(stream)
    assert data.q_ts[test].min() - data.q_ts[gapped].max() >= 20 * 3_600_000
    with pytest.raises(ValueError, match="gap"):
        tr.project_stream(data, "pc", 0.25, gap_hours=10 ** 6)


def test_adapt_online_records_each_point_and_starts_at_the_start_state(data):
    stream, test = tr.project_stream(data, "pc", 0.3)
    start = st.new_net(DIM, data.device, 2).state_dict()
    rows = tr.adapt_online(data, start, stream, test, points=[0.0, 0.5, 1.0], lr=3e-3,
                           online_batch=4, loss=st.LossConfig())
    assert [r["queries_seen"] for r in rows] == [0, round(0.5 * len(stream)), len(stream)]
    assert [r["point"] for r in rows] == [0.0, 0.5, 1.0]  # the report's columns, exact
    # a second project whose stream length does not divide evenly: its 0.5 point is
    # reached after a share that is not 0.5, and must still land in the 50% column
    odd = tr.adapt_online(data, start, stream[:19], test, points=[0.0, 0.5, 1.0], lr=3e-3,
                          online_batch=4, loss=st.LossConfig())
    assert odd[1]["share"] != 0.5
    md = tr.render_adapt_markdown({"profile": "qwen3", "test_share": 0.3, "online_batch": 4,
                                   "results": [{"project": "pc", "base": "b", "lr": 3e-3,
                                                "curve": rows},
                                               {"project": "pd", "base": "b", "lr": 3e-3,
                                                "curve": odd}]})
    assert "| project | base | lr | 0% | 50% | 100% |" in md
    assert all(line.count("|") == 7 for line in md.splitlines() if line.startswith("| p"))
    net = st.new_net(DIM, data.device, 0)
    net.load_state_dict(start)
    m = st.split_metrics(data, net, test)
    assert rows[0]["ndcg"] == pytest.approx(m["ndcg"])
    assert all(math.isfinite(r["gain"]) for r in rows)
    # the synthetic signal is learnable: one pass over the stream improves on the start
    assert rows[-1]["ndcg"] > rows[0]["ndcg"]


def test_adapt_cli_defaults_to_a_small_online_lr(tmp_path):
    a = tr.build_parser().parse_args(["adapt", "--data", "x", "--profile", "arctic",
                                      "--out", str(tmp_path)])
    assert a.lrs == [1e-4] and a.test_share == 0.25 and a.points[0] == 0.0
    assert ev.UNLOCK_MIN_REL_GAIN == 0.02
