#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Trainability verdict for the rl_events corpus.

Reads ``launcher.db`` (default ``~/.vct/launcher.db``, override via
``VCT_LAUNCHER_DB_PATH``) and computes the four trainability metrics
that gate offline training of the RL retrieval reranker:

  1. citation_pair_rate  — retrievals that have a matching answer event
  2. n_emb_presence      — per-node entries with non-empty n_emb
  3. query_emb_presence  — retrieval events with non-empty query_emb
  4. cohort_uniformity   — dominance of the single largest cohort
                            (project + embedding_model + embed_dim)

Each metric is compared against the threshold in
``vco_lib.rl_trainability_thresholds``. Emits a PASS/FAIL verdict per
metric plus an overall verdict (TRAINABLE iff ALL thresholds met).

Usage::

    python scripts/trainability_check.py [--db PATH] [--json]

Exit codes:
  0  — all thresholds met (TRAINABLE)
  1  — any threshold below bar OR launcher.db unreadable
  2  — invalid arguments

History — V52-S (v0.2.52). Used by V52-K's re-collect verdict, V52-T's
post-deploy smoke test, and any operator who wants a quick corpus
health snapshot before kicking off offline training.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Make this script runnable from the repo root without installation.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.rl_trainability_thresholds import (  # noqa: E402
    TRAINABILITY_THRESHOLDS,
)


def _resolve_db_path(override: str | None) -> Path | None:
    """Resolve the launcher.db path. Priority: --db arg > env > default."""
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None

    env_path = os.environ.get("VCT_LAUNCHER_DB_PATH", "").strip()
    if env_path:
        p = Path(env_path).expanduser()
        return p if p.is_file() else None

    # v0.2.53 (test_vct_root_dir_consolidation): consolidate to the canonical
    # resolver instead of hand-rolling Path.home() / ".vct". This script runs
    # against an already-installed orchestrator so vco_lib is importable.
    try:
        from vco_lib.paths import launcher_db_path
        p = launcher_db_path()
    except ImportError:
        # Bootstrap fallback if vco_lib is somehow unavailable (e.g. partial
        # install). Matches launcher_db_reader._discover_db_path exactly.
        state_dir = os.environ.get("VCT_STATE_DIR", "").strip()
        if state_dir:
            p = Path(state_dir).expanduser() / "launcher.db"
        else:
            p = Path.home() / ".vct" / "launcher.db"
    return p if p.is_file() else None


def _open_ro(p: Path) -> sqlite3.Connection:
    """Open launcher.db read-only with row factory."""
    uri = f"file:{p}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _compute_metrics(conn: sqlite3.Connection) -> dict[str, float]:
    """Compute the four trainability metrics from rl_events.

    Each metric is in [0, 1]. Missing data yields 0.0 (i.e. fails the
    threshold) rather than raising — the script's job is to surface
    a verdict, not crash on an unexpected schema.
    """
    metrics: dict[str, float] = {
        "citation_pair_rate": 0.0,
        "n_emb_presence": 0.0,
        "query_emb_presence": 0.0,
        "cohort_uniformity": 0.0,
    }

    # 1. citation_pair_rate: retrievals with a matching answer event.
    try:
        row = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM rl_events WHERE event_type='retrieval') AS retrievals,
              (SELECT COUNT(*) FROM rl_events r
                 WHERE r.event_type='retrieval'
                   AND EXISTS (
                     SELECT 1 FROM rl_events a
                       WHERE a.event_type='answer'
                         AND json_extract(a.payload_json, '$.task_id')
                             = json_extract(r.payload_json, '$.task_id')
                   )
              ) AS paired
            """
        ).fetchone()
        retrievals = (row["retrievals"] or 0) if row else 0
        paired = (row["paired"] or 0) if row else 0
        if retrievals > 0:
            metrics["citation_pair_rate"] = paired / retrievals
    except sqlite3.Error:
        pass  # leave at 0.0

    # 2. n_emb_presence: per-node entries with non-empty n_emb.
    #    Implemented by JSON-walking nodes[] — SQLite's json_each makes
    #    this tractable without pulling every payload into Python.
    try:
        row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN json_extract(node.value, '$.n_emb') IS NOT NULL
                        AND json_array_length(json_extract(node.value, '$.n_emb')) > 0
                       THEN 1 ELSE 0 END) AS with_emb,
              COUNT(*) AS total
            FROM rl_events,
                 json_each(json_extract(payload_json, '$.nodes')) AS node
            WHERE event_type='retrieval'
              AND json_type(payload_json, '$.nodes') = 'array'
            """
        ).fetchone()
        total = (row["total"] or 0) if row else 0
        with_emb = (row["with_emb"] or 0) if row else 0
        if total > 0:
            metrics["n_emb_presence"] = with_emb / total
    except sqlite3.Error:
        pass

    # 3. query_emb_presence: retrieval events with non-empty query_emb.
    try:
        row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN json_extract(payload_json, '$.query_emb') IS NOT NULL
                        AND json_array_length(json_extract(payload_json, '$.query_emb')) > 0
                       THEN 1 ELSE 0 END) AS with_emb,
              COUNT(*) AS total
            FROM rl_events
            WHERE event_type='retrieval'
            """
        ).fetchone()
        total = (row["total"] or 0) if row else 0
        with_emb = (row["with_emb"] or 0) if row else 0
        if total > 0:
            metrics["query_emb_presence"] = with_emb / total
    except sqlite3.Error:
        pass

    # 4. cohort_uniformity: dominance of the largest cohort.
    try:
        rows = conn.execute(
            """
            SELECT
              json_extract(payload_json, '$.project_name')        AS project,
              json_extract(payload_json, '$.embedding_model')     AS model,
              json_extract(payload_json, '$.embed_dim')           AS dim,
              COUNT(*) AS n
            FROM rl_events
            WHERE event_type='retrieval'
            GROUP BY project, model, dim
            ORDER BY n DESC
            """
        ).fetchall()
        total = sum(r["n"] for r in rows) if rows else 0
        if total > 0 and rows:
            metrics["cohort_uniformity"] = (rows[0]["n"] or 0) / total
    except sqlite3.Error:
        pass

    return metrics


def _verdict(metrics: dict[str, float]) -> tuple[bool, dict[str, dict[str, Any]]]:
    """Compare metrics to thresholds; return (overall_pass, per_metric)."""
    per_metric: dict[str, dict[str, Any]] = {}
    overall = True
    for name, observed in metrics.items():
        threshold = TRAINABILITY_THRESHOLDS.get(name, 1.0)
        passed = observed >= threshold
        per_metric[name] = {
            "observed": observed,
            "threshold": threshold,
            "passed": passed,
        }
        if not passed:
            overall = False
    return overall, per_metric


def _format_human(
    db_path: Path,
    overall: bool,
    per_metric: dict[str, dict[str, Any]],
) -> str:
    """Pretty-print the verdict for terminals."""
    lines = [f"Trainability check — {db_path}", ""]
    for name, info in per_metric.items():
        marker = "PASS" if info["passed"] else "FAIL"
        lines.append(
            f"  [{marker}] {name:24s} "
            f"observed={info['observed']:.3f}  threshold={info['threshold']:.3f}"
        )
    lines.append("")
    lines.append(
        f"Overall: {'TRAINABLE' if overall else 'NOT_TRAINABLE_AS_IS'}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=str, default=None, help="Path to launcher.db (default: auto-discover)")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = ap.parse_args(argv)

    db_path = _resolve_db_path(args.db)
    if db_path is None:
        msg = "launcher.db not found (set VCT_LAUNCHER_DB_PATH or pass --db)"
        if args.json:
            print(json.dumps({"error": msg, "verdict": "ERROR"}))
        else:
            print(msg, file=sys.stderr)
        return 1

    try:
        conn = _open_ro(db_path)
    except sqlite3.Error as exc:
        msg = f"failed to open {db_path}: {exc}"
        if args.json:
            print(json.dumps({"error": msg, "verdict": "ERROR"}))
        else:
            print(msg, file=sys.stderr)
        return 1

    try:
        metrics = _compute_metrics(conn)
    finally:
        conn.close()

    overall, per_metric = _verdict(metrics)

    if args.json:
        print(json.dumps({
            "db_path": str(db_path),
            "metrics": per_metric,
            "verdict": "TRAINABLE" if overall else "NOT_TRAINABLE_AS_IS",
        }, indent=2))
    else:
        print(_format_human(db_path, overall, per_metric))

    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
