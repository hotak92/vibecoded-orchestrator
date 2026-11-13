# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Step 1+2: Inventory both jsonl files, then build the arctic training corpus.

Maintainer script over the frozen pre-v0.2.92 JSONL corpus (read-only, at its
archive location under ``~/.claude/retrieval_rl_data/``; see
``rl_client.rl_logger.default_rl_data_dir``). Live training data is in
launcher.db, not here.

Streams both files (each ~700MB) without loading into memory at once.
Writes, under ``$RL_TRAIN_WORKDIR/data`` (see ``_workdir.py``):
  - inventory.json   — Step 1 counts table
  - pairs.jsonl      — Step 2 deduplicated (retrieval, citation) pairs
                            (one JSON object per line: {"retrieval": ..., "citation": ...})
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
LOG_A = HOME / ".claude" / "retrieval_rl_data" / "rl_events.jsonl"
LOG_B = HOME / ".claude" / "retrieval_rl_data" / "rl_events_qwen3.jsonl"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _workdir import work_dir  # noqa: E402

OUT_DIR = work_dir() / "data"
OUT_DIR.mkdir(exist_ok=True)


def normalize_source(src):
    if src is None or src == "":
        return "unspecified"
    return str(src)


def scan_file(path: Path):
    """Single streaming pass: return per-file stats and minimal metadata maps.

    Returns dict with:
      counts_retrieval_by_source: source -> count
      v1_count: int
      v2_retrieval_count, v2_citation_count: int
      retrieval_task_ids_by_source: source -> set of task_ids
      citation_task_ids: set of task_ids
      retrieval_offsets: dict task_id -> (file, byte_offset) for arctic retrievals only (small footprint)
      citation_offsets: dict task_id -> (file, byte_offset)
    """
    counts_ret = defaultdict(int)
    v1_count = 0
    v2_ret_count = 0
    v2_cit_count = 0
    ret_tids_by_src = defaultdict(set)
    cit_tids = set()
    arctic_ret_offsets = {}  # task_id -> byte_offset (multiple per task_id possible: keep best later)
    arctic_ret_nodecounts = {}  # task_id -> nodes count (for dedup)
    cit_offsets = {}  # task_id -> byte_offset
    task_types = defaultdict(int)

    total_bytes = path.stat().st_size
    pct_done = 0

    with path.open("rb") as f:
        while True:
            offset = f.tell()
            line = f.readline()
            if not line:
                break
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            sv = ev.get("schema_version", 0)
            event_type = ev.get("event")
            tid = ev.get("task_id", "")

            if sv < 2:
                v1_count += 1
                continue

            if event_type == "retrieval":
                v2_ret_count += 1
                src = normalize_source(ev.get("embedding_source"))
                counts_ret[src] += 1
                if tid:
                    ret_tids_by_src[src].add(tid)
                if src == "arctic" and tid:
                    n_nodes = len(ev.get("nodes") or [])
                    # Keep the entry with the most nodes per task_id (winning dedup tiebreak)
                    prev = arctic_ret_nodecounts.get(tid, -1)
                    if n_nodes > prev:
                        arctic_ret_nodecounts[tid] = n_nodes
                        arctic_ret_offsets[tid] = offset
            elif event_type == "citation":
                v2_cit_count += 1
                if tid:
                    cit_tids.add(tid)
                    cit_offsets[tid] = offset
                task_type = ev.get("task_type", "unknown")
                task_types[task_type] += 1

            # Progress reporting
            new_pct = int(100 * f.tell() / total_bytes)
            if new_pct >= pct_done + 5:
                pct_done = new_pct
                print(f"  {path.name}: {pct_done}% ({v2_ret_count} retrievals, {v2_cit_count} citations so far)", file=sys.stderr)

    return {
        "counts_retrieval_by_source": dict(counts_ret),
        "v1_count": v1_count,
        "v2_retrieval_count": v2_ret_count,
        "v2_citation_count": v2_cit_count,
        "retrieval_task_ids_by_source": {k: set(v) for k, v in ret_tids_by_src.items()},
        "citation_task_ids": cit_tids,
        "arctic_retrieval_offsets": arctic_ret_offsets,
        "arctic_retrieval_nodecounts": arctic_ret_nodecounts,
        "citation_offsets": cit_offsets,
        "task_types": dict(task_types),
    }


def main():
    print("=== Step 1: Scanning both log files ===", file=sys.stderr)
    stats_a = scan_file(LOG_A)
    stats_b = scan_file(LOG_B)

    # Build inventory
    inventory = {
        "files": {
            "rl_events.jsonl": {
                "size_bytes": LOG_A.stat().st_size,
                "v1_legacy_count": stats_a["v1_count"],
                "v2_retrieval_count": stats_a["v2_retrieval_count"],
                "v2_citation_count": stats_a["v2_citation_count"],
                "retrieval_by_source": stats_a["counts_retrieval_by_source"],
                "unique_retrieval_task_ids_by_source": {
                    k: len(v) for k, v in stats_a["retrieval_task_ids_by_source"].items()
                },
                "unique_citation_task_ids": len(stats_a["citation_task_ids"]),
                "task_types_in_citations": stats_a["task_types"],
            },
            "rl_events_qwen3.jsonl": {
                "size_bytes": LOG_B.stat().st_size,
                "v1_legacy_count": stats_b["v1_count"],
                "v2_retrieval_count": stats_b["v2_retrieval_count"],
                "v2_citation_count": stats_b["v2_citation_count"],
                "retrieval_by_source": stats_b["counts_retrieval_by_source"],
                "unique_retrieval_task_ids_by_source": {
                    k: len(v) for k, v in stats_b["retrieval_task_ids_by_source"].items()
                },
                "unique_citation_task_ids": len(stats_b["citation_task_ids"]),
                "task_types_in_citations": stats_b["task_types"],
            },
        },
    }

    # Cross-file pairing analysis for arctic
    arctic_tids_a = stats_a["retrieval_task_ids_by_source"].get("arctic", set())
    arctic_tids_b = stats_b["retrieval_task_ids_by_source"].get("arctic", set())
    all_arctic_tids = arctic_tids_a | arctic_tids_b
    citations_a = stats_a["citation_task_ids"]
    citations_b = stats_b["citation_task_ids"]
    all_citations = citations_a | citations_b

    # For arctic dedup: prefer A on tie (per spec). If same tid is arctic in both,
    # compare node counts; if tied, prefer A.
    tied_tids = arctic_tids_a & arctic_tids_b
    inventory["arctic_dedup"] = {
        "tids_only_in_A": len(arctic_tids_a - arctic_tids_b),
        "tids_only_in_B": len(arctic_tids_b - arctic_tids_a),
        "tids_in_both": len(tied_tids),
        "total_unique_arctic_tids": len(all_arctic_tids),
    }

    inventory["citation_pairing"] = {
        "arctic_retrievals_with_citation": len(all_arctic_tids & all_citations),
        "arctic_retrievals_without_citation": len(all_arctic_tids - all_citations),
        "citations_with_arctic_retrieval": len(all_citations & all_arctic_tids),
        "citations_with_arctic_retrieval_in_same_file_A": len(
            (citations_a & arctic_tids_a)
        ),
        "citations_with_arctic_retrieval_in_same_file_B": len(
            (citations_b & arctic_tids_b)
        ),
        "citations_cross_file": len(
            ((citations_a & arctic_tids_b) | (citations_b & arctic_tids_a))
        ),
    }

    (OUT_DIR / "inventory.json").write_text(json.dumps(inventory, indent=2, sort_keys=True))
    print(f"Wrote {OUT_DIR / 'inventory.json'}", file=sys.stderr)

    # =====================================================================
    # Step 2: Build arctic pair corpus
    # =====================================================================
    print("\n=== Step 2: Building arctic pair corpus ===", file=sys.stderr)

    # Decide which file holds each arctic retrieval (with dedup)
    selected = {}  # tid -> (file_path, byte_offset)
    a_offsets = stats_a["arctic_retrieval_offsets"]
    b_offsets = stats_b["arctic_retrieval_offsets"]
    a_counts = stats_a["arctic_retrieval_nodecounts"]
    b_counts = stats_b["arctic_retrieval_nodecounts"]

    for tid in all_arctic_tids:
        in_a = tid in a_offsets
        in_b = tid in b_offsets
        if in_a and in_b:
            # Pick higher node count; tie → A
            if b_counts[tid] > a_counts[tid]:
                selected[tid] = (LOG_B, b_offsets[tid])
            else:
                selected[tid] = (LOG_A, a_offsets[tid])
        elif in_a:
            selected[tid] = (LOG_A, a_offsets[tid])
        else:
            selected[tid] = (LOG_B, b_offsets[tid])

    # For citations, prefer same-file as retrieval; else cross-file
    citation_offsets_a = stats_a["citation_offsets"]
    citation_offsets_b = stats_b["citation_offsets"]

    pairs_built = 0
    pairs_skipped_no_citation = 0
    pairs_skipped_no_query_emb = 0
    pairs_skipped_empty_nodes = 0

    out_path = OUT_DIR / "pairs.jsonl"
    with out_path.open("w", encoding="utf-8") as fout:
        # Reopen log files once to read by offset
        with LOG_A.open("rb") as fa, LOG_B.open("rb") as fb:
            handles = {LOG_A: fa, LOG_B: fb}
            for tid, (path, offset) in selected.items():
                # Read retrieval event
                f = handles[path]
                f.seek(offset)
                ret_line = f.readline()
                try:
                    ret_ev = json.loads(ret_line)
                except json.JSONDecodeError:
                    continue

                if not ret_ev.get("query_emb"):
                    pairs_skipped_no_query_emb += 1
                    continue
                if not ret_ev.get("nodes"):
                    pairs_skipped_empty_nodes += 1
                    continue

                # Find citation: prefer same-file, else cross-file
                cit_path = None
                cit_off = None
                if path == LOG_A:
                    if tid in citation_offsets_a:
                        cit_path, cit_off = LOG_A, citation_offsets_a[tid]
                    elif tid in citation_offsets_b:
                        cit_path, cit_off = LOG_B, citation_offsets_b[tid]
                else:
                    if tid in citation_offsets_b:
                        cit_path, cit_off = LOG_B, citation_offsets_b[tid]
                    elif tid in citation_offsets_a:
                        cit_path, cit_off = LOG_A, citation_offsets_a[tid]

                if cit_path is None:
                    pairs_skipped_no_citation += 1
                    continue

                f2 = handles[cit_path]
                f2.seek(cit_off)
                cit_line = f2.readline()
                try:
                    cit_ev = json.loads(cit_line)
                except json.JSONDecodeError:
                    continue

                fout.write(json.dumps({"retrieval": ret_ev, "citation": cit_ev}) + "\n")
                pairs_built += 1

    summary = {
        "pairs_built": pairs_built,
        "pairs_skipped_no_citation": pairs_skipped_no_citation,
        "pairs_skipped_no_query_emb": pairs_skipped_no_query_emb,
        "pairs_skipped_empty_nodes": pairs_skipped_empty_nodes,
    }
    (OUT_DIR / "pairs_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2), file=sys.stderr)
    print(f"Wrote {out_path} ({pairs_built} pairs)", file=sys.stderr)


if __name__ == "__main__":
    main()
