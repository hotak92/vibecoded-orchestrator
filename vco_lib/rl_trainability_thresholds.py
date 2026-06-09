# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Trainability thresholds for the RL retrieval-reranker training corpus.

Single source of truth for the four pre-train health checks used to
decide whether the accumulated rl_events corpus is fit for offline
training. Audit A2 (`v0252-rl-telemetry-log-quality-audit-2026-06-09.md`)
derived these from end-to-end training experiments — below any one of
them and the trained reranker produces noticeably worse rankings than
the plain Weaviate cosine baseline.

History — V52-S (v0.2.52) lifted these from prose in A2's verdict and
V52-K's re-collect plan into module-level constants so the trainability
script + the offline trainer + V52-T's post-deploy smoke test all read
from one place. Adjusting any threshold here propagates to every
consumer on the next launch.

Why a separate file from ``rl_training_targets.py``: that file is
byte-identical-vendored to ``paid-modules/vct-rl-reranker/_training_targets.py``
(the test in ``tests/test_vendored_file_sync.py`` enforces drift-free).
Adding non-formula constants would burden every future training-formula
edit with a sync chore. The thresholds are a separate concern (corpus
health gating, not the training formula itself) so they live in their
own module.

Pure constants module: no I/O, no async. Import is free.
"""

from __future__ import annotations

__all__ = [
    "TRAINABILITY_MIN_CITATION_PAIR_RATE",
    "TRAINABILITY_MIN_N_EMB_PRESENCE",
    "TRAINABILITY_MIN_QUERY_EMB_PRESENCE",
    "TRAINABILITY_MIN_COHORT_UNIFORMITY",
    "TRAINABILITY_THRESHOLDS",
]


# ─── Threshold values (see A2 verdict 2026-06-09) ────────────────────────

#: Minimum fraction of retrieval events that have a matching ``answer``
#: event pairing them. Pairs are what produce the supervised
#: cited/uncited target labels. Below 30% the corpus is dominated by
#: retrievals with no downstream supervision signal, so the trainer
#: can't compute per-candidate gradient targets. v0.2.51 measured 0.3%
#: (1/295); V52-N's pairing fix is required to get this above the bar.
TRAINABILITY_MIN_CITATION_PAIR_RATE: float = 0.30

#: Minimum fraction of per-node entries in the corpus that have a
#: non-empty ``n_emb`` field. The unified-target v3 training path
#: reconstructs per-candidate cosines against ``query_emb`` at batch
#: time; nodes missing ``n_emb`` get dropped from training. v0.2.51
#: measured 8% (the V52-R fix is required to get above the bar).
TRAINABILITY_MIN_N_EMB_PRESENCE: float = 0.95

#: Minimum fraction of retrieval events that have a non-empty
#: ``query_emb`` field. Without the query embedding the trainer can't
#: compute query-side cosines for ANY of the candidates in the event.
#: v0.2.51 measured 99.7%; threshold is set just below the observed
#: floor so a single missing embed doesn't kill an otherwise-clean
#: collection.
TRAINABILITY_MIN_QUERY_EMB_PRESENCE: float = 0.99

#: Minimum fraction of retrieval events that come from a SINGLE cohort
#: (project + embedding_model + embed_dim). Below 95% the corpus is
#: too heterogeneous — different cohorts have different embedding
#: spaces and the trainer would have to learn a per-cohort projection
#: to use them jointly, which the current architecture doesn't support.
#: v0.2.51 measured 99% orchestrator-root/qwen3/1024 dominance — comfortably
#: above the bar.
TRAINABILITY_MIN_COHORT_UNIFORMITY: float = 0.95


#: Convenience map: metric_name -> threshold. Useful for the
#: trainability-check script's compact verdict rendering.
TRAINABILITY_THRESHOLDS: dict[str, float] = {
    "citation_pair_rate": TRAINABILITY_MIN_CITATION_PAIR_RATE,
    "n_emb_presence": TRAINABILITY_MIN_N_EMB_PRESENCE,
    "query_emb_presence": TRAINABILITY_MIN_QUERY_EMB_PRESENCE,
    "cohort_uniformity": TRAINABILITY_MIN_COHORT_UNIFORMITY,
}
