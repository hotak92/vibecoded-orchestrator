# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Step 3 (split) + Step 4 (baseline eval) + Step 5 (train) + Step 6 (post-eval).

Maintainer script over the frozen JSONL bake-off corpus built by
``inventory_and_build.py``. Imports the trainer + RL model from this package
(live, not a copy). Paths: ``_workdir.py`` (``RL_TRAIN_WORKDIR``); the baseline
checkpoint to compare against is ``RL_BASELINE_CKPT``.
"""
from __future__ import annotations

import copy
import json
import math
import os
import random
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _workdir import ensure_package_importable, work_dir  # noqa: E402

ensure_package_importable()
WORK_DIR = work_dir()

import numpy as np  # noqa: E402
import torch  # noqa: E402

from rl_reranker import offline_trainer as ot  # noqa: E402
from rl_reranker.rl_model import (  # noqa: E402
    RLModel,
    QUERY_TYPE_IDX,
    NULL_TYPE_IDX,
    MAX_LINKED,
)

DATA_DIR = WORK_DIR / "data"
WEIGHTS_DIR = WORK_DIR / "candidates"
DATA_DIR.mkdir(exist_ok=True)
WEIGHTS_DIR.mkdir(exist_ok=True)
PAIRS_PATH = DATA_DIR / "pairs.jsonl"
SPLIT_PATH = DATA_DIR / "splits.json"
BASELINE_CKPT = Path(os.environ.get("RL_BASELINE_CKPT", str(WORK_DIR / "baseline.pt")))
NEW_CKPT = WEIGHTS_DIR / "rl_model_arctic_1024.pt"

SEED = 42
BATCH_SIZE = 48
EPOCHS = 3
EPOCHS_MAX = 6
LAMBDA_RANK = 0.5
EMB_DIM = 1024


# ---------------------------------------------------------------------------
# Step 3 — Split by task_id
# ---------------------------------------------------------------------------

def load_pairs():
    pairs = []
    with PAIRS_PATH.open() as f:
        for line in f:
            d = json.loads(line)
            ret = d["retrieval"]
            cit = d["citation"]
            # Filter: must have query_emb, nodes, and at least one citation/cosine signal
            if not ret.get("query_emb") or not ret.get("nodes"):
                continue
            cs = cit.get("cosine_sims") or {}
            cs_real = any(v is not None for v in cs.values())
            cits = cit.get("citations") or {}
            cits_real = any(v is not None for v in cits.values())
            if not (cs_real or cits_real):
                continue
            pairs.append((ret, cit))
    return pairs


def split_pairs(pairs):
    """80/10/10 split, grouped by task_id (one task per pair already, but
    enforce explicitly for safety)."""
    rng = random.Random(SEED)
    by_tid = {}
    for ret, cit in pairs:
        tid = ret.get("task_id", "")
        by_tid.setdefault(tid, []).append((ret, cit))

    tids = sorted(by_tid.keys())
    rng.shuffle(tids)
    n = len(tids)
    n_train = int(n * 0.8)
    n_val = int(n * 0.1)
    train_tids = set(tids[:n_train])
    val_tids = set(tids[n_train : n_train + n_val])
    test_tids = set(tids[n_train + n_val :])

    train, val, test = [], [], []
    for tid, pl in by_tid.items():
        if tid in train_tids:
            train.extend(pl)
        elif tid in val_tids:
            val.extend(pl)
        else:
            test.extend(pl)

    return train, val, test, train_tids, val_tids, test_tids


# ---------------------------------------------------------------------------
# Step 4 — Eval metrics (P@5, MRR, F1)
# ---------------------------------------------------------------------------

def score_pair(net, ret_ev, cit_ev):
    """Score every node in the retrieval against the query, return:
      ranked: list of (title, predicted_score) sorted desc by score
      gt_cited: set of titles cited (citations[title] is True)
    """
    q_emb_raw = ret_ev.get("query_emb")
    if not q_emb_raw:
        return None, None
    q_emb = np.asarray(q_emb_raw, dtype=np.float32)

    nodes_raw = ret_ev.get("nodes") or []
    # Filter to nodes with embeddings
    valid_nodes = []
    valid_embs = []
    for n in nodes_raw:
        emb = n.get("emb")
        if not emb:
            continue
        valid_nodes.append(n)
        valid_embs.append(np.asarray(emb, dtype=np.float32))
    if not valid_nodes:
        return None, None

    B = len(valid_nodes)
    q_stack = np.tile(q_emb[None, :], (B, 1))
    n_stack = np.stack(valid_embs)

    q_t = torch.from_numpy(q_stack)
    n_t = torch.from_numpy(n_stack)
    zeros = torch.zeros(B, MAX_LINKED, q_t.shape[-1], dtype=torch.float32)
    entities = torch.cat([q_t.unsqueeze(1), n_t.unsqueeze(1), zeros], dim=1)

    q_tidx = torch.full((B, 1), QUERY_TYPE_IDX, dtype=torch.long)
    n_tidx = torch.zeros(B, 1, dtype=torch.long)
    l_tidx = torch.full((B, MAX_LINKED), NULL_TYPE_IDX, dtype=torch.long)
    type_idxs = torch.cat([q_tidx, n_tidx, l_tidx], dim=1)

    # Per-slot cosine features matching _epoch() logic
    slot_scalars_list = []
    for bi in range(B):
        q_np = q_stack[bi]
        n_np = n_stack[bi]
        # Cosine similarity (safe against zero-vectors)
        denom = float(np.linalg.norm(q_np) * np.linalg.norm(n_np))
        cos_qn = float(np.dot(q_np, n_np)) / denom if denom > 0 else 0.0
        slots = [(1.0, 1.0), (cos_qn, 1.0)]
        for _ in range(MAX_LINKED):
            slots.append((0.0, 0.0))
        slot_scalars_list.append(slots)
    slot_scalars = torch.tensor(slot_scalars_list, dtype=torch.float32)

    # Move tensors to the net's device. Inferred from the first parameter
    # so callers don't need to thread a device arg through evaluate(). When
    # the net is on CPU this is a no-op.
    try:
        _net_device = next(net.parameters()).device
    except StopIteration:
        _net_device = torch.device("cpu")
    if _net_device.type != "cpu":
        entities = entities.to(_net_device, non_blocking=True)
        type_idxs = type_idxs.to(_net_device, non_blocking=True)
        slot_scalars = slot_scalars.to(_net_device, non_blocking=True)

    net.eval()
    with torch.no_grad():
        preds = net(entities, type_idxs, slot_scalars).squeeze(-1).cpu().numpy()

    ranked = sorted(
        [(valid_nodes[i].get("title", ""), float(preds[i])) for i in range(B)],
        key=lambda x: x[1],
        reverse=True,
    )

    cits = cit_ev.get("citations") or {}
    gt_cited = {t for t, v in cits.items() if v is True}
    return ranked, gt_cited


def evaluate(net, pairs, threshold=0.5):
    """Compute P@5, MRR, F1, avg MAE on a list of (ret, cit) pairs.

    Only queries with at least one cited node contribute to P@5 and MRR;
    F1 uses all (node, cited?) pairs where citation is not None.
    """
    p5_scores = []
    rr_scores = []
    tp = 0
    fp = 0
    fn = 0
    tn = 0
    abs_errors = []  # mae proxy (vs computed target)
    n_evaluated = 0
    n_with_citations = 0

    for ret, cit in pairs:
        ranked, gt = score_pair(net, ret, cit)
        if ranked is None:
            continue
        n_evaluated += 1

        # P@5
        top5 = [t for t, _ in ranked[:5]]
        if gt:
            n_with_citations += 1
            n_relevant_in_top5 = sum(1 for t in top5 if t in gt)
            p5_scores.append(n_relevant_in_top5 / 5.0)

            # MRR
            rr = 0.0
            for rank, (t, _) in enumerate(ranked, 1):
                if t in gt:
                    rr = 1.0 / rank
                    break
            rr_scores.append(rr)

        # F1 using stored cited booleans (skip None)
        cits = cit.get("citations") or {}
        for title, score in ranked:
            v = cits.get(title)
            if v is None:
                continue
            pred_pos = score >= threshold
            true_pos = bool(v)
            if pred_pos and true_pos:
                tp += 1
            elif pred_pos and not true_pos:
                fp += 1
            elif not pred_pos and true_pos:
                fn += 1
            else:
                tn += 1

        # MAE proxy: compute target from cosine_sims (matches trainer formula)
        cs = {k: float(v) for k, v in (cit.get("cosine_sims") or {}).items() if v is not None}
        if cs:
            targets = ot.compute_targets_from_cosine_sims(cs)
        else:
            targets = {t: (1.0 if cited else 0.0) for t, cited in cits.items() if cited is not None}
        title_to_pred = dict(ranked)
        for title, tgt in targets.items():
            if title in title_to_pred:
                abs_errors.append(abs(title_to_pred[title] - tgt))

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    p5 = sum(p5_scores) / len(p5_scores) if p5_scores else 0.0
    mrr = sum(rr_scores) / len(rr_scores) if rr_scores else 0.0
    mae = sum(abs_errors) / len(abs_errors) if abs_errors else 0.0

    return {
        "p_at_5": p5,
        "mrr": mrr,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "mae_target": mae,
        "n_evaluated_queries": n_evaluated,
        "n_queries_with_citations": n_with_citations,
        "n_node_judgments": tp + fp + fn + tn,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
    }


def fmt_metrics(m):
    return (f"P@5={m['p_at_5']:.4f} MRR={m['mrr']:.4f} "
            f"F1={m['f1']:.4f} P={m['precision']:.4f} R={m['recall']:.4f} "
            f"MAE={m['mae_target']:.4f} n_q={m['n_evaluated_queries']} "
            f"n_q_cit={m['n_queries_with_citations']} "
            f"n_node={m['n_node_judgments']}")


# ---------------------------------------------------------------------------
# Step 5 — Train (re-use _epoch from offline_trainer)
# ---------------------------------------------------------------------------

def pairs_to_samples(pairs, link_sim=None):
    """Re-use extract_samples from offline_trainer."""
    if link_sim is None:
        link_sim = {}
    return ot.extract_samples(pairs, link_sim)


def main():
    # ----- Load pairs and check baseline checkpoint compatibility -----
    print("Loading pairs...")
    pairs = load_pairs()
    print(f"  {len(pairs)} usable pairs (with query_emb, nodes, and at least one citation/cosine signal)")

    # Split
    train, val, test, train_tids, val_tids, test_tids = split_pairs(pairs)
    print(f"Split sizes: train={len(train)}, val={len(val)}, test={len(test)}")
    print(f"  task_id counts: train={len(train_tids)} val={len(val_tids)} test={len(test_tids)}")

    # Sanity: no task_id leakage
    assert not (train_tids & val_tids), "leakage train-val"
    assert not (train_tids & test_tids), "leakage train-test"
    assert not (val_tids & test_tids), "leakage val-test"
    print("  no task_id leakage across splits.")

    SPLIT_PATH.write_text(json.dumps({
        "seed": SEED,
        "n_train_pairs": len(train),
        "n_val_pairs": len(val),
        "n_test_pairs": len(test),
        "train_task_ids": sorted(train_tids),
        "val_task_ids": sorted(val_tids),
        "test_task_ids": sorted(test_tids),
    }))

    # ----- Step 4: load baseline and evaluate -----
    print("\n=== Step 4: Baseline evaluation ===")
    print(f"Loading baseline: {BASELINE_CKPT}")
    if not BASELINE_CKPT.exists():
        print("FATAL: baseline checkpoint not found", file=sys.stderr)
        sys.exit(1)

    # Copy baseline to weights dir so RLModel.load_or_create reads it but
    # doesn't write back to the original location.
    baseline_local = WEIGHTS_DIR / "_baseline_copy.pt"
    shutil.copyfile(BASELINE_CKPT, baseline_local)

    try:
        baseline_model = RLModel.load_or_create(baseline_local, emb_dim=EMB_DIM)
    except Exception as exc:
        print(f"BASELINE LOAD FAILED: {exc}", file=sys.stderr)
        sys.exit(2)

    baseline_net = baseline_model._net
    print("  Baseline loaded successfully.")

    # Deep-copy the baseline net for evaluation BEFORE training begins
    eval_baseline_net = copy.deepcopy(baseline_net)

    print("  Evaluating baseline on VAL set...")
    base_val = evaluate(eval_baseline_net, val)
    print(f"    VAL  {fmt_metrics(base_val)}")

    print("  Evaluating baseline on TEST set...")
    base_test = evaluate(eval_baseline_net, test)
    print(f"    TEST {fmt_metrics(base_test)}")

    # ----- Step 5: train new model (continue from baseline) -----
    print("\n=== Step 5: Training new checkpoint ===")
    # Re-load model fresh so we don't double-trained the eval copy
    new_baseline_local = WEIGHTS_DIR / "_train_init.pt"
    shutil.copyfile(BASELINE_CKPT, new_baseline_local)
    rl_model = RLModel.load_or_create(new_baseline_local, emb_dim=EMB_DIM)
    net = rl_model._net
    opt = rl_model._opt

    train_samples = pairs_to_samples(train)
    val_samples = pairs_to_samples(val)
    print(f"  train samples: {len(train_samples)} | val samples: {len(val_samples)}")

    if not train_samples:
        print("FATAL: no training samples extractable", file=sys.stderr)
        sys.exit(3)

    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=EPOCHS_MAX, eta_min=1e-6)

    history = []
    best_val_mae = math.inf
    patience_counter = 0
    PATIENCE = 3
    best_state = copy.deepcopy(net.state_dict())

    epochs_done = 0
    last_train_loss = None

    for epoch in range(EPOCHS_MAX):
        # Stop early when EPOCHS reached AND train loss isn't dropping
        random.seed(SEED + epoch)
        random.shuffle(train_samples)
        train_loss, train_mse, train_delta, train_mae = ot._epoch(
            net, train_samples, opt, BATCH_SIZE, LAMBDA_RANK, training=True, emb_dim=EMB_DIM
        )
        val_loss, val_mse, val_delta, val_mae = ot._epoch(
            net, val_samples, None, BATCH_SIZE, LAMBDA_RANK, training=False, emb_dim=EMB_DIM
        ) if val_samples else (train_loss, train_mse, train_delta, train_mae)
        scheduler.step()
        epochs_done += 1
        lr = scheduler.get_last_lr()[0]
        print(f"  epoch {epoch+1}/{EPOCHS_MAX}: train_loss={train_loss:.5f} "
              f"train_mae={train_mae:.5f} val_loss={val_loss:.5f} val_mae={val_mae:.5f} "
              f"delta={train_delta:.5f} lr={lr:.2e}")
        history.append({
            "epoch": epoch + 1,
            "train_loss": train_loss,
            "train_mse": train_mse,
            "train_mae": train_mae,
            "train_delta": train_delta,
            "val_loss": val_loss,
            "val_mse": val_mse,
            "val_mae": val_mae,
            "val_delta": val_delta,
            "lr": lr,
        })

        if val_mae < best_val_mae - 1e-5:
            best_val_mae = val_mae
            best_state = copy.deepcopy(net.state_dict())
            patience_counter = 0
        else:
            patience_counter += 1

        # Early-stop logic: after minimum EPOCHS, if no improvement for PATIENCE
        # OR train_loss isn't dropping meaningfully, stop.
        if epoch + 1 >= EPOCHS:
            if patience_counter >= PATIENCE:
                print(f"  early stop: no val improvement for {PATIENCE} epochs")
                break
            if last_train_loss is not None and abs(last_train_loss - train_loss) < 1e-4:
                print(f"  early stop: train loss converged (Δ={abs(last_train_loss - train_loss):.5f})")
                break
        last_train_loss = train_loss

    # Restore best-val state to the model
    net.load_state_dict(best_state)
    rl_model._net = net
    rl_model._opt = opt
    rl_model._state_path = NEW_CKPT
    rl_model._pt_path = NEW_CKPT
    rl_model.save()
    print(f"  saved new checkpoint to {NEW_CKPT}")

    # ----- Step 6: post-eval -----
    print("\n=== Step 6: Post-training evaluation ===")
    print("  Evaluating new model on VAL set...")
    new_val = evaluate(net, val)
    print(f"    VAL  {fmt_metrics(new_val)}")
    print("  Evaluating new model on TEST set...")
    new_test = evaluate(net, test)
    print(f"    TEST {fmt_metrics(new_test)}")

    # ----- Persist all results -----
    results = {
        "baseline": {"val": base_val, "test": base_test},
        "new": {"val": new_val, "test": new_test},
        "history": history,
        "epochs_done": epochs_done,
        "best_val_mae": best_val_mae,
        "n_train_pairs": len(train),
        "n_val_pairs": len(val),
        "n_test_pairs": len(test),
        "n_train_samples": len(train_samples),
        "n_val_samples": len(val_samples),
        "config": {
            "seed": SEED,
            "batch_size": BATCH_SIZE,
            "epochs_min": EPOCHS,
            "epochs_max": EPOCHS_MAX,
            "lambda_rank": LAMBDA_RANK,
            "emb_dim": EMB_DIM,
            "patience": PATIENCE,
        },
    }
    (DATA_DIR / "results.json").write_text(json.dumps(results, indent=2))
    print(f"\nResults saved to {DATA_DIR / 'results.json'}")

    # Ship decision summary
    print("\n=== Ship criteria check (TEST set) ===")
    p5_d = new_test["p_at_5"] - base_test["p_at_5"]
    mrr_d = new_test["mrr"] - base_test["mrr"]
    f1_d = new_test["f1"] - base_test["f1"]
    print(f"  ΔP@5  = {p5_d:+.5f}")
    print(f"  ΔMRR  = {mrr_d:+.5f}")
    print(f"  ΔF1   = {f1_d:+.5f}")
    ship = (p5_d > 0) or (mrr_d > 0) or (f1_d > 0)
    print(f"  Decision: {'SHIP' if ship else 'NO-SHIP'}")
    results["ship"] = bool(ship)
    results["test_deltas"] = {"p_at_5": p5_d, "mrr": mrr_d, "f1": f1_d}
    (DATA_DIR / "results.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    main()
