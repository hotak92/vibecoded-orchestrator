# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Does a reranker transfer? Leave-one-project-out training + online-adaptation simulation.

Two sub-commands, both on export shards (``rl_reranker.training.export``)::

    python -m claude_mcp_servers.rl_reranker.training.transfer lopo  --data <export> --profile qwen3 --out <dir> [training options]
    python -m claude_mcp_servers.rl_reranker.training.transfer adapt --data <export> --profile qwen3 --out <dir> [--base-report <lopo report>] [training options]

``lopo`` — one fold per project with at least ``--min-fold-queries`` queries:
that project is held out, the smaller judged projects (>= the eval harness'
``MIN_PROJECT_QUERIES``, below the fold size) are held out of EVERY fold as
extra unseen probes, and the rest trains exactly as ``shard_trainer train``
does (same options, same time hold-out). Each fold is judged by the unlock
bar; the design passes only when every fold does AND every judged unseen
project gains at least the bar on its own (a pooled gain can hide one
project's regression). One fold with one hold-out project — what ``train``
reports — cannot tell a transferable model from one that memorised which
nodes of the training projects were useful.

``adapt`` — the product question "ship a neutral base and learn per project":
for every project, its queries in time order are split into an adaptation
stream (the earlier ``1 - --test-share``) and a fixed test window (the
latest ``--test-share``). Starting from a base network — the cosine-neutral
network (ranks exactly like cosine, knows nothing of the project), and/or the
LOPO fold checkpoint that never saw the project — the network takes ONE online
pass over the stream (a step every ``--online-batch`` queries, as the serving
path would after each citation), and NDCG@10 / MRR@10 on the test window are
recorded after each ``--points`` share of the stream.

Reports: ``lopo_<profile>.json`` / ``.md`` and ``adapt_<profile>.json`` /
``.md`` in ``--out`` (hashed project ids only).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from . import eval_harness
from . import shard_trainer as st
from ..rl_model import NODE_TYPES, TYPE_CODE_OTHER

import torch

#: A project needs this many queries to be a LOPO fold of its own.
MIN_FOLD_QUERIES = 3000


# ---------------------------------------------------------------------------
# Leave-one-project-out
# ---------------------------------------------------------------------------

def plan_folds(data: st.TrainData, *, min_fold_queries: int = MIN_FOLD_QUERIES,
               min_judged: int = eval_harness.MIN_PROJECT_QUERIES) -> tuple[list[str], list[str]]:
    """(fold projects, probe projects held out of every fold), by query count."""
    counts = np.bincount(data.q_project, minlength=len(data.project_names))
    order = np.argsort(-counts, kind="stable")
    folds = [data.project_names[i] for i in order if counts[i] >= min_fold_queries]
    probes = [data.project_names[i] for i in order if min_judged <= counts[i] < min_fold_queries]
    if len(folds) < 2:
        raise ValueError(
            f"leave-one-project-out needs >= 2 projects with >= {min_fold_queries} queries; "
            f"counts: {[(data.project_names[i], int(counts[i])) for i in order]}"
        )
    return folds, probes


def unseen_project_gains(project_report: dict[str, Any]) -> dict[str, Optional[float]]:
    """Relative NDCG gain of every JUDGED project of a project hold-out report."""
    return {p: row["rel_gain"] for p, row in project_report["per_project_ndcg"].items()
            if row["judged"]}


def lopo_verdict(folds: list[dict[str, Any]], *,
                 min_rel_gain: float = eval_harness.UNLOCK_MIN_REL_GAIN) -> dict[str, Any]:
    """Pass only if every fold passes the bar and every judged unseen project gains >= the bar."""
    reasons: list[str] = []
    for f in folds:
        for r in f["decision"]["reasons"]:
            reasons.append(f"fold {f['holdout_project']}: {r}")
        for p, g in f["unseen_gains"].items():
            if g is None or not math.isfinite(g) or g < min_rel_gain:
                reasons.append(f"fold {f['holdout_project']}: unseen project {p} gain "
                               f"{'n/a' if g is None else f'{100 * g:+.2f}%'} < "
                               f"{100 * min_rel_gain:.1f}%")
    return {"passes": not reasons, "reasons": reasons, "min_rel_gain": min_rel_gain}


def run_lopo(data: st.TrainData, args: argparse.Namespace) -> dict[str, Any]:
    folds, probes = plan_folds(data, min_fold_queries=args.min_fold_queries)
    if args.folds:
        unknown = [f for f in args.folds if f not in folds]
        if unknown:
            raise ValueError(f"--fold {unknown}: not a fold project (folds: {folds})")
        folds = [f for f in folds if f in args.folds]
    emb_dim = int(data.vectors.shape[1])
    out = Path(args.out)
    fold_reports = []
    for hold in folds:
        t0 = time.time()
        splits = st.make_splits(data, hold, time_holdout=args.time_holdout, val=args.val,
                                extra_holdouts=tuple(probes), purge_hours=args.purge_hours)
        print(f"[fold {hold}] " + ", ".join(f"{k}={len(v)}" for k, v in splits.items()), flush=True)
        init_state = st.resolve_init_state(args, data, splits["train"], emb_dim)
        best, runs = st.sweep_lrs(data, splits, args, init_state)
        net = st.new_net(emb_dim, data.device, args.seed, args.arch)
        net.load_state_dict(best.state_dict)
        hold_reports = st.evaluate_holdouts(data, net, splits, hold)
        decision = eval_harness.ship_decision(hold_reports["time_holdout"],
                                              hold_reports["project_holdout"])
        # one directory per (profile, fold): save_checkpoint leaves nothing
        # else in a checkpoint's directory
        ckpt = out / "folds" / args.profile / hold / f"rl_model_{args.profile}_lopo.pt"
        sha = st.save_checkpoint(best.state_dict, ckpt, profile=args.profile,
                                 version="0.0.0-lopo", emb_dim=emb_dim, arch=args.arch)
        fold_reports.append({
            "holdout_project": hold,
            "probe_projects": probes,
            "splits": {k: int(len(v)) for k, v in splits.items()},
            "best_lr": best.lr, "best_epoch": best.best_epoch, "epochs_run": best.epochs_run,
            "runs": [{"lr": r.lr, "best_select_ndcg10": round(r.best_val_ndcg, 6),
                      "best_epoch": r.best_epoch, "history": r.history} for r in runs],
            "time_holdout": hold_reports["time_holdout"],
            "project_holdout": hold_reports["project_holdout"],
            "unseen_gains": unseen_project_gains(hold_reports["project_holdout"]),
            "decision": decision,
            "checkpoint": {"path": str(ckpt), "sha256": sha},
            "runtime_s": round(time.time() - t0, 1),
        })
        print(render_fold_line(fold_reports[-1]), flush=True)
    return {"profile": args.profile, "kind": "lopo", "folds": fold_reports,
            "training": st.train_config_record(args),
            "verdict": lopo_verdict(fold_reports)}


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None or not math.isfinite(x) else f"{100 * x:+.2f}%"


def _num(x: Optional[float]) -> str:
    return "n/a" if x is None or not math.isfinite(x) else f"{x:.3f}"


def render_fold_line(f: dict[str, Any]) -> str:
    t = f["time_holdout"]["overall"]
    p = f["project_holdout"]["overall"]
    unseen = ", ".join(f"{k} {_pct(v)}" for k, v in f["unseen_gains"].items())
    return (f"[fold {f['holdout_project']}] time {_pct(t['ndcg']['rel_gain'])} "
            f"(MRR {t['mrr']['cosine']:.3f}->{t['mrr']['model']:.3f}) | unseen pooled "
            f"{_pct(p['ndcg']['rel_gain'])} (MRR {p['mrr']['cosine']:.3f}->{p['mrr']['model']:.3f})"
            f" | per unseen project: {unseen} | {'PASS' if f['decision']['passes'] else 'fail'}")


def render_lopo_markdown(rep: dict[str, Any]) -> str:
    v = rep["verdict"]
    lines = [f"# Leave-one-project-out: {rep['profile']} "
             f"({'PASSES' if v['passes'] else 'does not pass'})", "",
             "Training: " + json.dumps(rep["training"], sort_keys=True), "",
             "| held-out project | time NDCG gain | time MRR cos/model | unseen projects (NDCG gain each) "
             "| unseen MRR cos/model | per trained project (time) |",
             "|---|---|---|---|---|---|"]
    for f in rep["folds"]:
        t = f["time_holdout"]
        p = f["project_holdout"]["overall"]
        per_time = ", ".join(f"{k} {_pct(r['rel_gain'])}" for k, r in t["per_project_ndcg"].items())
        unseen = ", ".join(f"{k} {_pct(g)}" for k, g in f["unseen_gains"].items())
        lines.append(
            f"| {f['holdout_project']} | {_pct(t['overall']['ndcg']['rel_gain'])} | "
            f"{t['overall']['mrr']['cosine']:.3f} / {t['overall']['mrr']['model']:.3f} | {unseen} | "
            f"{p['mrr']['cosine']:.3f} / {p['mrr']['model']:.3f} | {per_time} |")
    if v["reasons"]:
        lines += ["", "Why not:", ""] + [f"- {r}" for r in v["reasons"]]
    return "\n".join(lines) + "\n"


def cmd_lopo(args: argparse.Namespace) -> int:
    data = st.load_training_data([Path(d) for d in args.data], args.profile, args.device)
    rep = run_lopo(data, args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"lopo_{args.profile}.json").write_text(json.dumps(rep, indent=2) + "\n", encoding="utf-8")
    md = render_lopo_markdown(rep)
    (out / f"lopo_{args.profile}.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


# ---------------------------------------------------------------------------
# Online per-project adaptation simulation
# ---------------------------------------------------------------------------

def project_stream(data: st.TrainData, project: str, test_share: float,
                   gap_hours: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """(adaptation stream, test window) of a project's queries, by time.

    ``gap_hours``: the stream ends that many hours before the test window
    starts (the same purge as ``make_splits``), so the simulation measures
    help on later work, not on the rest of the task in progress.
    """
    pi = data.project_names.index(project)
    qs = np.flatnonzero(data.q_project == pi)
    qs = qs[np.argsort(data.q_ts[qs], kind="stable")]
    n_test = int(round(test_share * len(qs)))
    if n_test < 1 or n_test >= len(qs):
        raise ValueError(f"{project}: {len(qs)} queries cannot give a {test_share} test window")
    stream, test = qs[: len(qs) - n_test], qs[len(qs) - n_test:]
    if gap_hours > 0:
        stream = stream[data.q_ts[stream] <= data.q_ts[test].min() - int(gap_hours * 3_600_000)]
        if not len(stream):
            raise ValueError(f"{project}: nothing left before a {gap_hours} h gap")
    return stream, test


def adapt_online(
    data: st.TrainData,
    start_state: dict[str, Any],
    stream: np.ndarray,
    test_q: np.ndarray,
    *,
    points: list[float],
    lr: float,
    online_batch: int,
    loss: st.LossConfig,
    l2sp: float = 0.0,
    arch: str = "v2",
    seed: int = 1234,
) -> list[dict[str, Any]]:
    """One online pass over ``stream`` (in order); test-window metrics after each point.

    A step every ``online_batch`` queries (all their nodes in one batch), as
    the serving path would after each citation. ``points`` are shares of
    the stream (0 = the start state itself).
    """
    emb_dim = int(data.vectors.shape[1])
    net = st.new_net(emb_dim, data.device, seed, arch)
    net.load_state_dict(start_state)
    anchor = ({k: v.detach().clone() for k, v in net.named_parameters()} if l2sp > 0 else None)
    opt = torch.optim.AdamW(net.parameters(), lr=lr, weight_decay=st.WEIGHT_DECAY)
    gen = torch.Generator(device=data.device)
    gen.manual_seed(seed)
    rows = []
    done = 0

    def record(point: float) -> None:
        m = st.split_metrics(data, net, test_q)
        rows.append({"point": point, "queries_seen": done,
                     "share": round(done / max(len(stream), 1), 4),
                     "ndcg": m["ndcg"], "cos_ndcg": m["cos_ndcg"],
                     "gain": eval_harness.relative_gain(m["ndcg"], m["cos_ndcg"]),
                     "mrr": m["mrr"], "cos_mrr": m["cos_mrr"]})

    for point in sorted(set(points)):
        mark = min(len(stream), int(round(point * len(stream))))
        while done < mark:
            chunk = stream[done: min(mark, done + online_batch)]
            net.train()
            idx = torch.from_numpy(st.query_sample_ranges(data.q_first[chunk],
                                                          data.q_count[chunk])).to(data.device)
            total = st.batch_loss(net, data, idx, loss=loss, generator=gen)
            if anchor is not None:
                total = total + l2sp * st.l2sp_penalty(net, anchor)
            opt.zero_grad(set_to_none=True)
            total.backward()
            opt.step()
            done += len(chunk)
        record(point)
    return rows


def run_adapt(data: st.TrainData, args: argparse.Namespace) -> dict[str, Any]:
    emb_dim = int(data.vectors.shape[1])
    folds, probes = plan_folds(data, min_fold_queries=args.min_fold_queries)
    projects = args.projects or (folds + probes)
    lopo_ckpts: dict[str, str] = {}
    if args.base_report:
        rep = json.loads(Path(args.base_report).read_text(encoding="utf-8"))
        if rep.get("profile") != args.profile:
            raise ValueError(f"{args.base_report}: a {rep.get('profile')} report, not {args.profile}")
        for f in rep["folds"]:
            # a fold checkpoint never saw its held-out project NOR the probes
            for p in [f["holdout_project"], *f["probe_projects"]]:
                lopo_ckpts.setdefault(p, f["checkpoint"]["path"])
    loss = st.loss_from_args(args)
    results = []
    for proj in projects:
        stream, test_q = project_stream(data, proj, args.test_share, args.purge_hours)
        others = np.flatnonzero(data.q_project != data.project_names.index(proj))
        bases: dict[str, dict[str, Any]] = {
            "cosine-neutral": st.cosine_neutral_state(
                data, others, steps=args.init_steps, seed=args.seed, arch=args.arch,
                log=lambda m: None)}
        if proj in lopo_ckpts:
            bases["lopo-base"] = st.load_init_state(Path(lopo_ckpts[proj]), emb_dim, args.arch)
        for name, state in bases.items():
            for lr in args.lrs:
                rows = adapt_online(data, state, stream, test_q, points=args.points, lr=lr,
                                    online_batch=args.online_batch, loss=loss, l2sp=args.l2sp,
                                    arch=args.arch, seed=args.seed)
                results.append({"project": proj, "base": name, "lr": lr,
                                "n_stream": int(len(stream)), "n_test": int(len(test_q)),
                                "curve": rows})
                print(f"[adapt {proj}] base={name} lr={lr:g}: " + " ".join(
                    f"{r['point']:.2f}:{_pct(r['gain'])}" for r in rows), flush=True)
    return {"profile": args.profile, "kind": "adapt", "test_share": args.test_share,
            "online_batch": args.online_batch, "points": args.points,
            "training": st.train_config_record(args), "results": results}


def render_adapt_markdown(rep: dict[str, Any]) -> str:
    pts = sorted({r["point"] for res in rep["results"] for r in res["curve"]})
    lines = [f"# Online per-project adaptation: {rep['profile']}", "",
             f"Test window: latest {int(100 * rep['test_share'])}% of each project's queries; "
             f"one online pass over the earlier queries (ending "
             f"{rep.get('training', {}).get('purge_hours', 0.0):g} h before the window), a step "
             f"every {rep['online_batch']} "
             "queries. Cells: NDCG@10 gain over cosine on the test window after that share "
             "of the stream (MRR@10 of the literally cited node in brackets).", "",
             "| project | base | lr | " + " | ".join(f"{100 * p:.0f}%" for p in pts) + " |",
             "|---|---|---|" + "---|" * len(pts)]
    for res in rep["results"]:
        cells = {r["point"]: f"{_pct(r['gain'])} ({r['mrr']:.3f})" for r in res["curve"]}
        lines.append(f"| {res['project']} | {res['base']} | {res['lr']:g} | "
                     + " | ".join(cells.get(p, "") for p in pts) + " |")
    cos_mrr = {res["project"]: res["curve"][0]["cos_mrr"] for res in rep["results"]}
    lines += ["", "Cosine MRR@10 on each test window: "
              + ", ".join(f"{p} {m:.3f}" for p, m in cos_mrr.items())]
    return "\n".join(lines) + "\n"


def cmd_adapt(args: argparse.Namespace) -> int:
    data = st.load_training_data([Path(d) for d in args.data], args.profile, args.device)
    rep = run_adapt(data, args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"adapt_{args.profile}.json").write_text(json.dumps(rep, indent=2) + "\n", encoding="utf-8")
    md = render_adapt_markdown(rep)
    (out / f"adapt_{args.profile}.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


# ---------------------------------------------------------------------------
# Replay through the SERVER's per-project online learning (v0.2.106)
# ---------------------------------------------------------------------------

def _type_name(code: int) -> str:
    """An export type code back to a name ``type_code`` maps to the same code."""
    if 0 <= code < len(NODE_TYPES):
        return NODE_TYPES[code]
    if code == TYPE_CODE_OTHER:
        return "x-nonbuiltin-type"
    return ""  # unknown / null padding


def export_task(shard: Any, qi: int) -> "tuple[dict[str, Any], np.ndarray]":
    """The ``/rl_update`` task block the MCP would have posted for export query
    ``qi`` (``RLClient.rl_update_v3``: ``nodes_packed`` in SEARCH order — the
    retrieval's own score, descending — with the frozen vectors; the answer
    cosines and literal citations), and the export's own targets in that order.
    """
    q = shard.queries
    s = shard.samples
    lo = int(q["first_sample"][qi])
    idx = np.arange(lo, lo + int(q["n_samples"][qi]))
    order = idx[np.argsort(-np.nan_to_num(s["score"][idx], nan=-np.inf), kind="stable")]
    vec = shard.vectors
    nodes: list[dict[str, Any]] = []
    cos: dict[str, float] = {}
    lit: dict[str, bool] = {}
    for j in order:
        title = f"n{int(j)}"
        lv = s["linked_vec"][j]
        lt = s["linked_type"][j]
        present = [k for k in range(len(lv)) if lv[k] >= 0]
        nodes.append({
            "title": title,
            "node_type": _type_name(int(s["node_type"][j])),
            "n_emb": np.asarray(vec[int(s["node_vec"][j])], dtype=np.float32),
            "linked_embs": [np.asarray(vec[int(lv[k])], dtype=np.float32) for k in present],
            "linked_type_names": [_type_name(int(lt[k])) for k in present],
        })
        c = float(s["cos_citation"][j])
        if math.isfinite(c):
            cos[title] = c
        lit[title] = bool(s["literal_cited"][j])
    task = {
        "nodes_packed": nodes,
        "query_emb": np.asarray(vec[int(q["vec"][qi])], dtype=np.float32),
        "cosine_sims": cos,
        "literal_cited": lit,
        "cross_encoder_cited": None,
    }
    return task, s["target"][order].astype(np.float32)


def _mean(rows: "list[dict[str, Any]]", key: str) -> float:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return float(np.mean(vals)) if vals else float("nan")


def replay_rows(learner: Any) -> "list[dict[str, Any]]":
    """Per kept event, in arrival order, from the learner's own prequential
    record (``online_learning.PrequentialRecord``: every query scored by the
    model in place when it arrived, before training on it): whether the
    project was served the model, the model's / plain cosine's / the search
    order's NDCG@10 and cited-node MRR@10 (``None`` = undefined), and what
    the user got (the model's order while served, the search order
    otherwise)."""
    rec = learner.record
    cols = {c: rec.column(c) for c in ("model_ndcg", "cos_ndcg", "search_ndcg",
                                        "model_mrr", "cos_mrr", "search_mrr")}
    served = rec.served
    out = []
    for i in range(len(rec)):
        row: dict[str, Any] = {"served": bool(served[i])}
        for c, v in cols.items():
            x = float(v[i])
            row[c] = None if math.isnan(x) else x
        row["user_ndcg"] = row["model_ndcg"] if row["served"] else row["search_ndcg"]
        row["user_mrr"] = row["model_mrr"] if row["served"] else row["search_mrr"]
        out.append(row)
    return out


def replay_project(shard: Any, project: str, *, cfg: Any, device: str = "auto",
                   include_soft: bool = False, log: Any = print) -> dict[str, Any]:
    """Replay one project's citation events, in time order, through the
    server's own online learning (``online_learning.ProjectLearner`` — the
    object ``RLServer._rl_update`` / ``_finish_round`` drive: ingest, rounds
    every ``cfg.round_every`` events that score the arrivals before training
    on them, the gate on the cumulative prequential record), starting from
    the server's seeded fresh network. Whether each event was served and its
    metrics come from the learner's own record. Soft-label (short-window)
    citations are left out unless ``include_soft``: the live monitor never
    posts them."""
    from ..online_learning import ProjectLearner
    from ..rl_model import RLModel
    from ..rl_server import _seeded_init

    t0 = time.monotonic()
    pi = shard.projects.index(project)
    q = shard.queries
    qs = np.flatnonzero(q["project"] == pi)
    if not include_soft:
        qs = qs[q["soft_label"][qs] == 0]
    qs = qs[np.argsort(q["ts_hour_ms"][qs], kind="stable")]
    dim = int(shard.vectors.shape[1])
    with _seeded_init(cfg.seed, enabled=True):
        model = RLModel(emb_dim=dim, state_path=Path("/nonexistent/replay/rl_model.pt"),
                        device=device, embedding_source=shard.profile)
    learner = ProjectLearner(profile=shard.profile, emb_dim=dim, cfg=cfg)
    transitions: list[dict[str, Any]] = []
    tgt_diff = 0.0
    last_ms = 0
    for n, qi in enumerate(qs):
        task, export_target = export_task(shard, int(qi))
        last_ms = int(q["ts_hour_ms"][qi])
        if learner.ingest(task, ts_ms=last_ms):
            lq = learner.last_query
            if lq.n == len(export_target):
                tgt_diff = max(tgt_diff, float(np.max(np.abs(lq.target - export_target))))
        if learner.round_due():
            before = learner.serving
            learner.run_round_now(model, now_ms=last_ms)
            if learner.serving != before:
                transitions.append({"event": learner.counters["events_seen"],
                                    "check": learner.checks,
                                    "serving": learner.serving, "reason": learner.reason})
        if log is not None and n and n % 5000 == 0:
            log(f"  [{shard.profile} {project}] {n}/{len(qs)} events, serving={learner.serving} "
                f"({learner.reason})")
    # the events after the last round are scored like any round's arrivals
    learner.run_round_now(model, now_ms=last_ms)
    rows = replay_rows(learner)
    first = next((i for i, r in enumerate(rows) if r["served"]), None)
    after = rows[first:] if first is not None else []
    served_rows = [r for r in rows if r["served"]]
    served_cited = [r for r in served_rows if r["model_mrr"] is not None]

    def gain(a: str, b: str, rs: "list[dict[str, Any]]") -> float:
        ma, mb = _mean(rs, a), _mean(rs, b)
        return (ma - mb) / mb if mb and math.isfinite(mb) and mb > 0 else float("nan")

    st_ = learner.status()
    s_gain = gain("model_ndcg", "cos_ndcg", served_rows)
    s_mrr, s_cos = _mean(served_rows, "model_mrr"), _mean(served_rows, "cos_mrr")
    return {
        "project": project, "profile": shard.profile,
        "events": int(len(qs)), "events_kept": learner.counters["events_seen"],
        "queries_to_qualify": first,
        "served_share_after_qualify": (sum(r["served"] for r in after) / len(after)) if after else None,
        "served_queries": len(served_rows),
        "served_cited_queries": len(served_cited),
        "served_ndcg_gain_vs_cosine": s_gain,
        "served_mrr": s_mrr,
        "served_cos_mrr": s_cos,
        # The acceptance property (point estimates over the served searches):
        # NDCG gain >= the bar AND cited MRR not below cosine's. None = nothing
        # served (vacuous).
        "acceptance": (None if not served_rows else bool(
            math.isfinite(s_gain) and s_gain >= cfg.min_ndcg_gain
            and (not served_cited or s_mrr >= s_cos))),
        "served_ndcg_gain_vs_search": gain("model_ndcg", "search_ndcg", served_rows),
        "after_qualify_user_gain_vs_cosine": gain("user_ndcg", "cos_ndcg", after),
        "after_qualify_user_gain_vs_search": gain("user_ndcg", "search_ndcg", after),
        "after_qualify_user_mrr": _mean(after, "user_mrr"),
        "after_qualify_search_mrr": _mean(after, "search_mrr"),
        "after_qualify_cos_mrr": _mean(after, "cos_mrr"),
        "prequential": st_["record"],
        "all_cos_ndcg": _mean(rows, "cos_ndcg"), "all_search_ndcg": _mean(rows, "search_ndcg"),
        "checks": learner.checks,
        "serving_turned_on": learner.counters["serving_turned_on"],
        "serving_turned_off": learner.counters["serving_turned_off"],
        "transitions": transitions[:50],
        "final": {"serving": st_["serving"], "reason": st_["reason"],
                  "last_check": st_["last_check"], "counters": st_["counters"]},
        "target_max_abs_diff_vs_export": tgt_diff,
        "runtime_s": round(time.monotonic() - t0, 1),
    }


def replay_config(args: argparse.Namespace) -> Any:
    """The server's ``OnlineConfig``, with the options the command overrides."""
    from dataclasses import replace

    from ..online_learning import DEFAULT_CONFIG

    over: dict[str, Any] = {}
    for opt, field in (("lr", "lr"), ("min_queries", "min_queries"),
                       ("min_cited", "min_cited_queries"), ("round_every", "round_every"),
                       ("batch_queries", "batch_queries"), ("max_steps", "max_steps_per_round"),
                       ("error_budget", "error_budget")):
        v = getattr(args, opt, None)
        if v is not None:
            over[field] = v
    return replace(DEFAULT_CONFIG, **over)


def render_replay_markdown(rep: dict[str, Any]) -> str:
    lines = [f"# Replay through the server's online learning: {rep['profile']}", "",
             "Config: " + json.dumps(rep["config"], sort_keys=True), "",
             "| project (events) | queries to qualify | served share after | served n (cited) "
             "| served NDCG vs cos | served cited-MRR model / cos | acceptance "
             "| user NDCG vs search after | prequential NDCG gain / cited-MRR diff | checks "
             "| turned off | final |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["results"]:
        share = r["served_share_after_qualify"]
        pq = r["prequential"]
        acc = {None: "trivial", True: "pass", False: "FAIL"}[r["acceptance"]]
        pq_diff = (None if pq["mrr"] is None or pq["cos_mrr"] is None
                   else pq["mrr"] - pq["cos_mrr"])
        lines.append(
            f"| {r['project']} ({r['events_kept']}) | {r['queries_to_qualify'] if r['queries_to_qualify'] is not None else 'never'} "
            f"| {'n/a' if share is None else f'{100 * share:.0f}%'} "
            f"| {r['served_queries']} ({r['served_cited_queries']}) "
            f"| {_pct(r['served_ndcg_gain_vs_cosine'])} "
            f"| {_num(r['served_mrr'])} / {_num(r['served_cos_mrr'])} | {acc} "
            f"| {_pct(r['after_qualify_user_gain_vs_search'])} "
            f"| {_pct(pq['ndcg_gain'])} / {'n/a' if pq_diff is None else f'{pq_diff:+.3f}'} "
            f"| {r['checks']} | {r['serving_turned_off']} "
            f"| {'serving' if r['final']['serving'] else 'not serving'}: {r['final']['reason']} |")
    return "\n".join(lines) + "\n"


def cmd_replay(args: argparse.Namespace) -> int:
    from .export import load_profile_shard

    cfg = replay_config(args)
    shard = load_profile_shard(Path(args.data[0]), args.profile)
    counts = np.bincount(shard.queries["project"], minlength=len(shard.projects))
    projects = args.projects or [p for i, p in enumerate(shard.projects)
                                 if counts[i] >= eval_harness.MIN_PROJECT_QUERIES]
    results = [replay_project(shard, p, cfg=cfg, device=args.device,
                              include_soft=args.include_soft) for p in projects]
    rep = {"profile": args.profile, "kind": "replay", "config": cfg.as_dict(), "results": results}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = args.tag or args.profile
    (out / f"replay_{tag}.json").write_text(json.dumps(rep, indent=2, default=float) + "\n",
                                            encoding="utf-8")
    md = render_replay_markdown(rep)
    (out / f"replay_{tag}.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m claude_mcp_servers.rl_reranker.training.transfer")
    sub = p.add_subparsers(dest="cmd", required=True)
    lo = sub.add_parser("lopo", help="leave-one-project-out training + evaluation")
    st.add_training_options(lo)
    lo.add_argument("--min-fold-queries", type=int, default=MIN_FOLD_QUERIES)
    lo.add_argument("--fold", dest="folds", action="append", default=[],
                    help="run only this fold (hashed project id); repeatable")
    lo.set_defaults(func=cmd_lopo)
    ad = sub.add_parser("adapt", help="simulate online per-project adaptation")
    st.add_training_options(ad)
    ad.add_argument("--min-fold-queries", type=int, default=MIN_FOLD_QUERIES)
    ad.add_argument("--project", dest="projects", action="append", default=[],
                    help="project to simulate (hashed id); repeatable; default: every judged project")
    ad.add_argument("--base-report", default=None,
                    help="a lopo report: each project also starts from the fold checkpoint "
                         "that never saw it")
    ad.add_argument("--test-share", type=float, default=0.25)
    ad.add_argument("--online-batch", type=int, default=8)
    ad.add_argument("--points", type=lambda s: [float(x) for x in s.split(",")],
                    default=[0.0, 0.05, 0.1, 0.25, 0.5, 1.0])
    ad.set_defaults(func=cmd_adapt, lrs=[1e-4])
    rp = sub.add_parser("replay", help="replay each project's events through the SERVER's "
                                       "online learning + serving gate")
    rp.add_argument("--data", action="append", required=True, help="export directory")
    rp.add_argument("--profile", required=True, choices=["qwen3", "arctic"])
    rp.add_argument("--out", required=True)
    rp.add_argument("--tag", default="", help="report file suffix (default: the profile)")
    rp.add_argument("--project", dest="projects", action="append", default=[],
                    help="hashed project id; repeatable; default: every judged project")
    rp.add_argument("--device", default="auto")
    rp.add_argument("--include-soft", action="store_true",
                    help="also replay soft-label citations (the live monitor never posts them)")
    rp.add_argument("--lr", type=float, default=None)
    rp.add_argument("--min-queries", type=int, default=None,
                    help="prequential record size before any verdict")
    rp.add_argument("--min-cited", type=int, default=None,
                    help="citing queries in the record before any verdict")
    rp.add_argument("--round-every", type=int, default=None)
    rp.add_argument("--batch-queries", type=int, default=None)
    rp.add_argument("--max-steps", type=int, default=None)
    rp.add_argument("--error-budget", type=float, default=None,
                    help="total false-pass probability over every check "
                         "(the k-th check runs at 1 - budget / (k (k+1)))")
    rp.set_defaults(func=cmd_replay)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
