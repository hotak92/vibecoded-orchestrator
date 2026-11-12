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
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
