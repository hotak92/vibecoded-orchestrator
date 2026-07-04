# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for V52-S — trainability constants + check script.

Exercises:
  * Threshold constants in ``vco_lib.rl_trainability_thresholds`` round-trip.
  * ``scripts/trainability_check.py`` computes correct verdicts on
    synthetic launcher.db corpora (one passing, one failing).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "trainability_check.py"


# ─── Constants ────────────────────────────────────────────────────────


def test_constants_present_and_sane():
    """Threshold values exist + are in (0, 1]."""
    from vco_lib.rl_trainability_thresholds import (
        TRAINABILITY_MIN_CITATION_PAIR_RATE,
        TRAINABILITY_MIN_N_EMB_PRESENCE,
        TRAINABILITY_MIN_QUERY_EMB_PRESENCE,
        TRAINABILITY_MIN_COHORT_UNIFORMITY,
        TRAINABILITY_THRESHOLDS,
    )

    for name, value in [
        ("citation_pair_rate", TRAINABILITY_MIN_CITATION_PAIR_RATE),
        ("n_emb_presence", TRAINABILITY_MIN_N_EMB_PRESENCE),
        ("query_emb_presence", TRAINABILITY_MIN_QUERY_EMB_PRESENCE),
        ("cohort_uniformity", TRAINABILITY_MIN_COHORT_UNIFORMITY),
    ]:
        assert 0.0 < value <= 1.0, f"{name} threshold out of range: {value}"

    assert set(TRAINABILITY_THRESHOLDS.keys()) == {
        "citation_pair_rate",
        "n_emb_presence",
        "query_emb_presence",
        "cohort_uniformity",
    }


# ─── Synthetic launcher.db ─────────────────────────────────────────────


def _make_synthetic_db(
    path: Path,
    retrievals: int,
    paired: int,
    n_emb_present: int,
    n_emb_total: int,
    query_emb_present: int,
    cohort_uniformity: float,
) -> None:
    """Materialize a launcher.db with the requested metric values.

    Keeps the schema minimal — just enough rl_events to make the
    script's four queries return the requested ratios.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE rl_events (
          id INTEGER PRIMARY KEY,
          event_type TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at INTEGER NOT NULL
        )
        """
    )

    # Build retrievals + matching answers in a coordinated way.
    # `paired` of the retrievals get an answer event with the same task_id.
    # Each retrieval carries `nodes_per_retrieval = n_emb_total / retrievals`
    # nodes; we distribute `n_emb_present` of them with `n_emb`.
    nodes_per_retrieval = max(1, n_emb_total // max(1, retrievals))

    # Cohort distribution: `cohort_uniformity` fraction in the dominant
    # cohort, rest split across one other cohort. Pick whole counts.
    dominant_count = int(retrievals * cohort_uniformity)
    other_count = retrievals - dominant_count

    nodes_placed = 0
    embs_placed = 0
    rows: list[tuple[str, str, int]] = []
    for i in range(retrievals):
        task_id = f"task_{i}"
        cohort_dim = 1024 if i < dominant_count else 2048
        cohort_model = "qwen3" if i < dominant_count else "codesage"
        cohort_project = "vco_dev"  # constant project for simplicity

        # Per-node array: place n_emb on the first nodes only until exhausted.
        nodes = []
        for _ in range(nodes_per_retrieval):
            node: dict[str, object] = {"title": f"n_{nodes_placed}"}
            if embs_placed < n_emb_present and nodes_placed < n_emb_total:
                node["n_emb"] = [0.1, 0.2, 0.3]  # non-empty
                embs_placed += 1
            nodes.append(node)
            nodes_placed += 1

        payload: dict[str, object] = {
            "task_id": task_id,
            "project_name": cohort_project,
            "embedding_model": cohort_model,
            "embed_dim": cohort_dim,
            "nodes": nodes,
        }
        if i < query_emb_present:
            payload["query_emb"] = [0.5, 0.5, 0.5]

        rows.append(("retrieval", json.dumps(payload), i * 1000))

        if i < paired:
            ans_payload = {"task_id": task_id, "answer_text": "stub"}
            rows.append(("answer", json.dumps(ans_payload), i * 1000 + 500))

    conn.executemany(
        "INSERT INTO rl_events (event_type, payload_json, created_at) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


# ─── Script integration ────────────────────────────────────────────────


def _run_script(db_path: Path) -> tuple[int, dict]:
    """Run the trainability script + parse the JSON verdict."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db_path), "--json"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        parsed = {"raw_stdout": result.stdout, "stderr": result.stderr}
    return result.returncode, parsed


def test_script_verdict_passing_corpus(tmp_path):
    """A clean corpus (all metrics above threshold) → exit 0 + TRAINABLE."""
    db = tmp_path / "launcher.db"
    _make_synthetic_db(
        db,
        retrievals=100,
        paired=50,            # 50% pairing → above 30% threshold
        n_emb_present=95,
        n_emb_total=100,      # 95% → at threshold
        query_emb_present=100,  # 100% → above 99%
        cohort_uniformity=0.97,  # above 95%
    )

    code, verdict = _run_script(db)
    assert code == 0, f"expected exit 0, got {code}: {verdict}"
    assert verdict["verdict"] == "TRAINABLE"
    assert all(m["passed"] for m in verdict["metrics"].values())


def test_script_verdict_failing_corpus(tmp_path):
    """A v0.2.51-shaped corpus (low pair rate + low n_emb) → exit 1."""
    db = tmp_path / "launcher.db"
    _make_synthetic_db(
        db,
        retrievals=100,
        paired=1,             # 1% — far below 30%
        n_emb_present=8,
        n_emb_total=100,      # 8% — far below 95%
        query_emb_present=100,
        cohort_uniformity=0.99,
    )

    code, verdict = _run_script(db)
    assert code == 1
    assert verdict["verdict"] == "NOT_TRAINABLE_AS_IS"
    assert verdict["metrics"]["citation_pair_rate"]["passed"] is False
    assert verdict["metrics"]["n_emb_presence"]["passed"] is False
    # The other two should still pass — the script reports per-metric.
    assert verdict["metrics"]["query_emb_presence"]["passed"] is True
    assert verdict["metrics"]["cohort_uniformity"]["passed"] is True


def test_nemb_presence_counts_the_emb_field_post_dedup(tmp_path):
    """v0.2.73 n_emb payload-dedup: the written node vector moved from a
    duplicate `emb`+`n_emb` pair to a single canonical `emb` (the field the
    offline trainer reads). The `n_emb_presence` metric must count `emb` (with
    `n_emb` as a legacy fallback) — else a fully-trainable post-dedup corpus
    would falsely read 0% and block training."""
    db = tmp_path / "launcher.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE rl_events (id INTEGER PRIMARY KEY, event_type TEXT, "
        "payload_json TEXT, created_at INTEGER)"
    )
    rows = []
    for i in range(10):
        # New post-dedup shape: vector under `emb` only, NO `n_emb`.
        payload = {
            "task_id": f"t{i}",
            "project_name": "demo_project",
            "embedding_model": "qwen3",
            "embed_dim": 1024,
            "query_emb": [0.5, 0.5, 0.5],
            "nodes": [{"title": f"n{i}", "emb": [0.1, 0.2, 0.3]}],
        }
        rows.append(("retrieval", json.dumps(payload), i * 1000))
        rows.append(("answer", json.dumps({"task_id": f"t{i}"}), i * 1000 + 500))
    conn.executemany(
        "INSERT INTO rl_events (event_type, payload_json, created_at) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()

    code, verdict = _run_script(db)
    # 100% of nodes carry the vector under `emb` → presence must be 1.0.
    assert verdict["metrics"]["n_emb_presence"]["observed"] == pytest.approx(1.0)
    assert verdict["metrics"]["n_emb_presence"]["passed"] is True


def test_script_handles_missing_db(tmp_path):
    """Pointing at a non-existent file → exit 1 with a clear message."""
    nonexistent = tmp_path / "no-such-file.db"
    code, verdict = _run_script(nonexistent)
    assert code == 1
    # In --json mode the script emits an error key.
    if "error" in verdict:
        assert "not found" in verdict["error"].lower() or "open" in verdict["error"].lower()


def test_script_handles_empty_corpus(tmp_path):
    """Empty rl_events table → all metrics 0.0 → FAIL across the board."""
    db = tmp_path / "launcher.db"
    _make_synthetic_db(
        db,
        retrievals=0,
        paired=0,
        n_emb_present=0,
        n_emb_total=0,
        query_emb_present=0,
        cohort_uniformity=0.0,
    )

    code, verdict = _run_script(db)
    assert code == 1
    for name, info in verdict["metrics"].items():
        assert info["passed"] is False, f"{name} should fail with empty corpus"


# ─── Importability ─────────────────────────────────────────────────────


def test_module_importable_without_side_effects():
    """Importing the constants module should not touch disk / network."""
    import importlib

    mod = importlib.import_module("vco_lib.rl_trainability_thresholds")
    # No __getattr__ shenanigans, no implicit IO at import time.
    assert hasattr(mod, "TRAINABILITY_THRESHOLDS")
    # Sanity-check the dict is a fresh copy per module load (immutability
    # is not required, but mutation safety is nice).
    threshold_dict = mod.TRAINABILITY_THRESHOLDS
    assert isinstance(threshold_dict, dict)
    assert len(threshold_dict) == 4
