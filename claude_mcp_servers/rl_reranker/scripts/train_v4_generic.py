# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Generic v0.2.0 architecture training run — parameterized over arch_version.

Mirrors train_v4_v2_no_tx.py structure but accepts the arch_version + suffix
via CLI args so all 6+ ablation runs can share one script.

Usage:
    python scripts/train_v4_generic.py --arch v2_no_tx_ln --suffix v2_no_tx_ln \\
        --epochs-max 16

Output: $RL_TRAIN_WORKDIR/data/results_v4_<suffix>.json (see _workdir.py).
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _workdir import ensure_package_importable  # noqa: E402

ensure_package_importable()

import numpy as np  # noqa: E402
import torch  # noqa: E402

from rl_reranker import offline_trainer as ot  # noqa: E402
from rl_reranker.rl_model import RLModel, WEIGHT_DECAY  # noqa: E402
from train_and_eval import (  # noqa: E402
    load_pairs, split_pairs, pairs_to_samples, evaluate, fmt_metrics,
    SEED, BATCH_SIZE, EMB_DIM, DATA_DIR, WEIGHTS_DIR,
)

LAMBDA_RANK = 0.5
EPOCHS_MIN = 3
PATIENCE = 3
LR_GRID = [5e-6, 1e-5, 2e-5]

# Device used by all _fresh_model() calls in this script. Set by main() from
# --device. Default is "auto" so direct script imports (unit tests) work too.
_DEVICE = "auto"

V3_RESULTS = DATA_DIR / "results_v3.json"
V4_NOTX_RESULTS = DATA_DIR / "results_v4_v2_no_tx.json"


def _fresh_model(tmp_ckpt: Path, arch_version: str) -> RLModel:
    if tmp_ckpt.exists():
        tmp_ckpt.unlink()
    return RLModel.load_or_create(
        tmp_ckpt, emb_dim=EMB_DIM, arch_version=arch_version,
        device=_DEVICE,
    )


def train_one(net, train_samples, val_samples, val_pairs, lr, max_epochs):
    opt = torch.optim.AdamW(net.parameters(), lr=lr, weight_decay=WEIGHT_DECAY)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        opt, T_max=max_epochs, eta_min=lr * 0.1
    )

    history = []
    best_score = -math.inf
    best_state = copy.deepcopy(net.state_dict())
    best_val_metrics = None
    best_epoch = 0
    stale = 0

    eval_net = copy.deepcopy(net)
    pre_val = evaluate(eval_net, val_pairs)
    print(f"    pre-train val: {fmt_metrics(pre_val)}", flush=True)

    for epoch in range(max_epochs):
        random.seed(SEED * 100 + epoch)
        random.shuffle(train_samples)
        train_loss, _, train_delta, train_mae = ot._epoch(
            net, train_samples, opt, BATCH_SIZE, LAMBDA_RANK,
            training=True, emb_dim=EMB_DIM,
        )
        val_loss, _, val_delta, val_mae = ot._epoch(
            net, val_samples, None, BATCH_SIZE, LAMBDA_RANK,
            training=False, emb_dim=EMB_DIM,
        ) if val_samples else (train_loss, 0.0, 0.0, train_mae)
        scheduler.step()

        val_eval_net = copy.deepcopy(net)
        v = evaluate(val_eval_net, val_pairs)
        cur_score = v["p_at_5"] + v["mrr"] + v["f1"]
        print(
            f"    epoch {epoch+1}/{max_epochs} lr={scheduler.get_last_lr()[0]:.2e} "
            f"train_loss={train_loss:.5f} val_mae={val_mae:.5f} "
            f"VAL P@5={v['p_at_5']:.4f} MRR={v['mrr']:.4f} F1={v['f1']:.4f} "
            f"sum={cur_score:.4f}",
            flush=True,
        )
        history.append({
            "epoch": epoch + 1,
            "train_loss": train_loss,
            "train_mae": train_mae,
            "train_delta": train_delta,
            "val_loss": val_loss,
            "val_mae": val_mae,
            "val_p_at_5": v["p_at_5"],
            "val_mrr": v["mrr"],
            "val_f1": v["f1"],
            "val_sum": cur_score,
            "lr": scheduler.get_last_lr()[0],
        })

        if cur_score > best_score + 1e-5:
            best_score = cur_score
            best_state = copy.deepcopy(net.state_dict())
            best_val_metrics = v
            best_epoch = epoch + 1
            stale = 0
        else:
            stale += 1

        if epoch + 1 >= EPOCHS_MIN and stale >= PATIENCE:
            print(f"    early stop at epoch {epoch+1} (stale={stale})", flush=True)
            break

    return best_state, best_val_metrics, best_epoch, history, pre_val


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--arch", required=True,
        choices=[
            "v1", "v2",
            "v2_no_tx_24ep",
            "v2_cos3heads_all_gelu",
            "v2_cos3heads_all_silu",
            "v2_cos3heads_mixed",
        ],
        help="v2 = canonical reranker; v1 = legacy snapshot in "
             "rl_model_v1.py (testing/rollback only); v2_no_tx_24ep + "
             "v2_cos3heads_* are the 2026-05-23 bake-off variants.",
    )
    parser.add_argument("--suffix", required=True,
                        help="filename suffix: data/results_v4_<suffix>.json")
    # --epochs is the canonical knob; --epochs-max is kept as a backwards-compat
    # alias for the original v0.2.0 bake-off invocations. Both accept the same
    # value and feed into the same train_one() call.
    parser.add_argument("--epochs", type=int, default=None,
                        help="Max training epochs (alias for --epochs-max).")
    parser.add_argument("--epochs-max", type=int, default=16,
                        help="Max training epochs (legacy name).")
    parser.add_argument("--device", type=str, default="auto",
                        help="auto / cpu / cuda / cuda:N")
    args = parser.parse_args()

    arch_version = args.arch
    suffix = args.suffix
    # --epochs takes precedence over --epochs-max when both are passed.
    epochs_max = args.epochs if args.epochs is not None else args.epochs_max
    global _DEVICE
    _DEVICE = args.device

    out_results = DATA_DIR / f"results_v4_{suffix}.json"
    # 2026-05-23 bake-off layout: per-arch subdir so the cos3heads runs don't
    # collide with the flat-layout v0.2.0 ablation checkpoints. The subdir is
    # created lazily — RLModel.save() does parents=True under the hood.
    ckpt_path = WEIGHTS_DIR / suffix / f"rl_model_arctic_1024_{suffix}.pt"
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"=== ARCH={arch_version} SUFFIX={suffix} EPOCHS_MAX={epochs_max} ===", flush=True)

    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)

    pairs = load_pairs()
    train, val, test, *_ = split_pairs(pairs)
    print(f"Splits: train={len(train)} val={len(val)} test={len(test)}", flush=True)
    train_samples = pairs_to_samples(train)
    val_samples = pairs_to_samples(val)

    # Load reference baselines (best-effort; missing files are fine)
    refs = {}
    try:
        v3 = json.loads(V3_RESULTS.read_text())
        refs["v1_fine_tuned_test"] = v3["new"]["test"]
        refs["v1_fine_tuned_val"] = v3["new"]["val"]
        print(f"\nRef v1 (fine-tuned)   TEST {fmt_metrics(refs['v1_fine_tuned_test'])}", flush=True)
    except Exception as exc:
        print(f"(skip v1 ref: {exc})", flush=True)
    try:
        v4 = json.loads(V4_NOTX_RESULTS.read_text())
        refs["v2_no_tx_test"] = v4.get("v2_no_tx_trained", {}).get("test", {})
        if refs["v2_no_tx_test"]:
            print(f"Ref v2_no_tx          TEST {fmt_metrics(refs['v2_no_tx_test'])}", flush=True)
    except Exception as exc:
        print(f"(skip v2_no_tx ref: {exc})", flush=True)

    # Pre-train baseline (fresh init)
    print(f"\n=== {suffix} pre-train (fresh init) ===", flush=True)
    fresh = _fresh_model(WEIGHTS_DIR / f"_{suffix}_fresh.pt", arch_version)
    pre_val = evaluate(fresh._net, val)
    pre_test = evaluate(fresh._net, test)
    print(f"  VAL  {fmt_metrics(pre_val)}", flush=True)
    print(f"  TEST {fmt_metrics(pre_test)}", flush=True)
    n_params = sum(p.numel() for p in fresh._net.parameters())
    print(f"  params: {n_params:,}", flush=True)

    sweep_results = []
    overall_best_state = None
    overall_best_lr = None
    overall_best_epoch = None
    overall_best_score = -math.inf
    overall_best_history = None

    for lr in LR_GRID:
        print(f"\n=== LR sweep: {lr} ===", flush=True)
        rl_model = _fresh_model(WEIGHTS_DIR / f"_{suffix}_tmp_lr_{lr}.pt", arch_version)
        net = rl_model._net
        best_state, best_val, best_epoch, history, _ = train_one(
            net, train_samples, val_samples, val, lr, epochs_max
        )
        if best_val is None:
            continue
        score = best_val["p_at_5"] + best_val["mrr"] + best_val["f1"]
        sweep_results.append({
            "lr": lr,
            "best_epoch": best_epoch,
            "val_score": score,
            "val_metrics": best_val,
            "history": history,
        })
        print(
            f"  Best @ lr={lr}: epoch={best_epoch} "
            f"P@5={best_val['p_at_5']:.4f} MRR={best_val['mrr']:.4f} "
            f"F1={best_val['f1']:.4f} sum={score:.4f}",
            flush=True,
        )
        if score > overall_best_score:
            overall_best_score = score
            overall_best_state = best_state
            overall_best_lr = lr
            overall_best_epoch = best_epoch
            overall_best_history = history

    if overall_best_state is None:
        results = {
            "arch_version": arch_version,
            "refs": refs,
            "pre_train": {"val": pre_val, "test": pre_test},
            "params": n_params,
            "ship": False,
            "reason": "no_lr_improved_val",
            "sweep": sweep_results,
        }
        out_results.write_text(json.dumps(results, indent=2))
        return

    print(f"\n=== Best {suffix} config: lr={overall_best_lr}, epoch={overall_best_epoch} ===", flush=True)
    rl_model = _fresh_model(WEIGHTS_DIR / f"_{suffix}_final.pt", arch_version)
    net = rl_model._net
    net.load_state_dict(overall_best_state)
    rl_model._net = net
    rl_model._state_path = ckpt_path
    rl_model._pt_path = ckpt_path
    rl_model.save()
    print(f"Saved: {ckpt_path}", flush=True)

    new_val = evaluate(net, val)
    new_test = evaluate(net, test)
    print(f"  {suffix} VAL  {fmt_metrics(new_val)}", flush=True)
    print(f"  {suffix} TEST {fmt_metrics(new_test)}", flush=True)

    # Comparison printout (vs available refs)
    print(f"\n=== TEST comparison ({suffix} vs refs) ===", flush=True)
    for k in ("p_at_5", "mrr", "f1", "precision", "recall", "mae_target"):
        my = new_test[k]
        line = f"  {k:15s}: {suffix}={my:.5f}"
        for ref_name, ref_test in refs.items():
            if not ref_test:
                continue
            rv = ref_test.get(k, 0.0)
            d = my - rv
            s = "+" if d >= 0 else ""
            line += f"  {ref_name}={rv:.5f} Δ={s}{d:.5f}"
        print(line, flush=True)

    results = {
        "arch_version": arch_version,
        "refs": refs,
        "pre_train": {"val": pre_val, "test": pre_test},
        "trained": {"val": new_val, "test": new_test},
        "params": n_params,
        "best_lr": overall_best_lr,
        "best_epoch": overall_best_epoch,
        "best_history": overall_best_history,
        "sweep_summary": [
            {"lr": s["lr"], "best_epoch": s["best_epoch"], "val_score": s["val_score"]}
            for s in sweep_results
        ],
        "sweep_full": sweep_results,
        "config": {
            "seed": SEED,
            "lr_grid": LR_GRID,
            "lambda_rank": LAMBDA_RANK,
            "weight_decay": WEIGHT_DECAY,
            "batch_size": BATCH_SIZE,
            "epochs_min": EPOCHS_MIN,
            "epochs_max": epochs_max,
            "patience": PATIENCE,
            "emb_dim": EMB_DIM,
        },
        "n_pairs": {"train": len(train), "val": len(val), "test": len(test)},
    }
    out_results.write_text(json.dumps(results, indent=2))
    print(f"\nResults: {out_results}", flush=True)


if __name__ == "__main__":
    main()
