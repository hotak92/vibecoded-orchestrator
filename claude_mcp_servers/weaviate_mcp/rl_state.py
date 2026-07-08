# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""RL reranking + telemetry state: the mutable caches and tuning constants.

v0.2.75 P3g (M-1 remainder): these definitions were previously module globals
on ``server.py``. The 37 RL helpers extracted to ``rl_enrichment.py`` in v0.2.73
read/write this state through a lazy ``server`` proxy, and ``rl_client.*`` +
the test-suite reach it via ``srv.<name>``. That left ``server.py`` as the
DEFINITION home for RL state even though it no longer holds the RL logic — the
"state stayed behind" remainder the v0.2.75 codegraph reconcile flagged
(SUB-OPTIMAL): every future extraction from the still-oversized ``server.py``
inherited that coupling.

This module is now the single DEFINITION home. ``server.py`` imports these
names and RE-EXPORTS them into its own namespace (``server.<name> = <name>``),
so the public contract is unchanged bit-for-bit:

  * **Mutable containers** (``_rl_client_instances``, ``_rl_telemetry_writers``,
    ``_rl_node_content_cache``, ``_rl_monitor_tasks``) are re-exported BY
    REFERENCE — ``server._rl_node_content_cache`` IS this module's dict object,
    so every in-place mutation (``srv._rl_node_content_cache[k] = v`` from
    ``rl_client.search_pipeline``, ``srv._rl_client_instances.clear()`` from the
    tests) is observed identically on both module objects.
  * **Scalar tuning constants** (the ``_RL_*`` thresholds,
    ``_CODE_STRUCTURE_TELEMETRY_MAX_NODES``, ``DUAL_RL_LOG_ENABLED_ENV``) are
    re-exported by value. ``rl_enrichment`` continues to read them via the
    ``server`` proxy (``server._RL_MONITOR_POLL_INTERVAL`` …), so a test that
    ``monkeypatch.setattr(srv, "_RL_MONITOR_POLL_INTERVAL", 0.005)`` rebinds the
    server-side copy and the moved functions — reading through the SAME
    ``server`` namespace — observe the patch exactly as before. The definition
    home moving here does NOT change the patch surface (which is, and stays,
    ``server``).
  * **Cross-module scalar counter** ``_rl_call_seq`` is incremented by
    ``rl_client.search_pipeline`` via ``srv._rl_call_seq += 1`` (a rebind on the
    server namespace). ``next_rl_call_seq()`` is provided here as the ONE-home
    incrementer; ``search_pipeline`` routes through it so the counter has a
    single authoritative storage location rather than a re-exported copy that
    would desync on rebind. The module-level ``_rl_call_seq`` below is that
    storage.

Nothing in this module imports ``server`` (no circular edge, no import-order
hazard); it is a pure leaf that ``server`` and ``rl_enrichment`` both depend on.
"""

from __future__ import annotations

import asyncio
import os

# ─── Over-fetch / packing ───────────────────────────────────────────────
# Over-fetch multiplier: fetch this many × limit from Weaviate, pass all to RL
# server for reranking.
#
# v0.2.75 P3f: promoted to the ``KG_OVERFETCH_MULTIPLIER`` env (default 2 — the
# prior hardcoded value). A malformed / <1 value falls back to the default so a
# typo can't wedge retrieval into fetching zero candidates.
def _resolve_overfetch(default: int = 2) -> int:
    raw = os.getenv("KG_OVERFETCH_MULTIPLIER", "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val >= 1 else default


_RL_OVERFETCH = _resolve_overfetch()
# v0.2.47 RL-6: maximum linked-slot vectors packed per node in the v3
# retrieval event. Must MATCH `paid-modules/vct-rl-reranker/rl_model.py::MAX_LINKED`
# (= 5). The container's `_rl_model.update(q_raw, n_raw, linked_raws, n_type_idx)`
# only consumes the first MAX_LINKED entries — over-shipping wastes bytes.
# Packing order is fixed: `extra_chunks_of_same_node + actual_linked_nodes`,
# truncated to MAX_LINKED.
_RL_MAX_LINKED: int = 5

# ─── Per-process mutable state ──────────────────────────────────────────
# Per-process call counter — used to order calls within a session (maps seq →
# transcript position). MUTATED via ``next_rl_call_seq()`` (one home); see the
# module docstring for why a re-exported copy would desync.
_rl_call_seq: int = 0
# v0.2.28: hold strong references to in-flight `_rl_answer_monitor` tasks
# so Python's GC cannot drop them mid-poll. Without this, `asyncio.create_task`
# returns a task whose only reference is the local variable in the caller —
# which goes out of scope before the task awaits. The CPython asyncio
# runtime tracks tasks in a WeakSet, so GC can (and does) collect them,
# logging "Task was destroyed but it is pending!" before silently dropping
# the citation event. The rl-logging-audit-report 2026-05-23 finding #1
# (97.7% orphan-citation rate) is the symptom. Standard mitigation:
# https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
_rl_monitor_tasks: "set[asyncio.Task]" = set()
# v0.2.47 RL-6: per-task content cache for the citation-write monitor.
# Populated by `_rl_cache_and_rerank` at retrieval time with the full per-node
# payload (best-chunk vector, MAX_LINKED packed linked_embs, node_type, links,
# cos_qn/ql/nl, etc.) PLUS the query embedding and active-embedding metadata.
# Consumed by `_rl_answer_monitor` at citation time so it can compute
# `cosine_sims` (max over answer-chunks × n_emb) + `literal_cited` and write
# a single v3 citation event without re-fetching anything from Weaviate.
#
# Eviction: LRU-ish, bounded at `_RL_NODE_CACHE_MAX`. Entries are popped by
# the monitor on success; expired-but-unwritten entries (timeout, evicted by
# new traffic) are dropped silently — the retrieval event still got written
# at cache-fill time, only the citation event is lost. This is the same
# soft-fail discipline as the hub POST: no retry, no fallback.
#
# Each entry shape (matches what `_rl_answer_monitor` reads):
#   {
#       "nodes": list[dict],          # per-node with title / node_type /
#                                     # n_emb / linked_embs / linked_type_names /
#                                     # cos_qn / cos_ql / cos_nl / file_path / links
#       "query_emb": list[float],     # 1024-dim active-slot vector
#       "active_model": str,          # for chunker.for_model + event payload
#       "embedding_source": str,
#       "embedding_dim": int,
#       "project_id": str | None,
#       "project_name": str | None,
#       "task_type": str,
#   }
_rl_node_content_cache: dict[str, dict] = {}
_RL_NODE_CACHE_MAX: int = 256

# ─── Answer-monitor tuning ──────────────────────────────────────────────
# NOTE (v0.2.73 M-1): the ``match_position_for_query as _match_position_for_query``
# import lives in ``weaviate_mcp.rl_enrichment`` alongside ``_rl_answer_monitor``
# (its sole consumer). ``rl_client.answer_window`` remains the single source of
# truth for the matcher — the monitor + Stop-hook drain still agree byte-for-byte.
# Monitor config: poll every N seconds, stop when answer window reaches this size OR a new
# human turn appears after the search.  Timeout is a hard ceiling.
_RL_MONITOR_POLL_INTERVAL: float = 2.0
# V52-N (2026-06-09): the accumulator threshold is now expressed in TOKENS
# (not chars) and aligned with the citation gate
# ``_RL_MIN_ANSWER_TOKENS_FOR_CITATION`` immediately below. Pre-V52-N the
# accumulator stopped at 64 000 chars (~16k tokens) while the gate required
# 25 000 tokens -> the monitor would fire on length, the gate would reject
# as too short, and the citation event was silently dropped. Aligning them
# to the same 25 000-token value means the monitor fires only once the
# answer has enough signal to pass the gate.
_RL_MONITOR_ANSWER_THRESHOLD_TOKENS: int = 25_000  # V52-N: align with citation gate
# Back-compat char alias for any test/external caller still importing the
# old name. 1 token ~= 4 chars (qwen3 BPE empirical average).
_RL_MONITOR_ANSWER_THRESHOLD: int = _RL_MONITOR_ANSWER_THRESHOLD_TOKENS * 4
_RL_TOOL_CONTENT_LIMIT: int = 20_000         # per tool_use input, chars
# V52-N: hard timeout raised from 10 min -> 60 min. The new accumulator
# stops either at the 25 000-token threshold or when the PreCompact hook
# writes ``.claude/state/rl_monitors_force_flush.flag``; the timeout is
# now a pure safety valve for sessions that get neither (very short
# answers, never compacted, never closed). 60 min absorbs slow agents
# without leaking monitor tasks indefinitely.
_RL_MONITOR_TIMEOUT: float = 3600.0           # 60 min hard ceiling (V52-N safety valve)
# V52-N: PreCompact hook drops this sentinel; the monitor picks it up on
# the next poll and fires with whatever's accumulated so far. After
# firing the monitor deletes the sentinel so subsequent compactions can
# re-arm it. Path is relative to ``CLAUDE_PROJECT_DIR``.
_RL_MONITOR_FORCE_FLUSH_SENTINEL: str = ".claude/state/rl_monitors_force_flush.flag"
# v0.2.47 RL-7.5: minimum answer length (TOKENS) to compute citation events.
# Below this, the monitor still POSTs to the RL container's /rl_update
# (the container may treat short answers as negative-signal training data)
# but we skip the citation-event write — too-short answers produce noisy
# cosine signals (single-chunk embedding of a few hundred tokens carries
# less signal than the noise threshold; the multi-chunk best-of-cosine
# trick only pays off when the answer spans 3+ chunks).
#
# Default 25,000 tokens = ~100KB of text = roughly enough for 2-3 chunks
# at qwen3's xlarge_context preset (target_tokens=9500) OR ~15 chunks at
# arctic2's medium preset (target=2500). Either way ample signal for the
# max-over-chunks cosine to discriminate cited-vs-not.
#
# Tunable via env so CPU / Pro users can experiment. Pre-v0.2.47.5
# this was a 200-char default which was way too low (typical Claude
# preamble like "Sure! Let me look that up..." would have passed).
_RL_MIN_ANSWER_TOKENS_FOR_CITATION: int = int(
    os.getenv("RL_MIN_ANSWER_TOKENS_FOR_CITATION", "25000")
)
# Back-compat alias for any test or downstream caller still importing
# the chars-based name. The actual gate uses the tokens version below.
_RL_MIN_ANSWER_CHARS_FOR_CITATION: int = _RL_MIN_ANSWER_TOKENS_FOR_CITATION * 4
# Minimum title length for the literal-citation regex. Below this we skip
# the per-node regex entirely — two-letter titles like "AI" / "RL" produce
# enough false-positives ("curl", "url", "fail") to swamp any signal.
# Matches the rule in paid-modules/vct-rl-reranker/retrieval_rl.py.
_RL_LITERAL_CITED_MIN_TITLE_LEN: int = 3

# ─── RLClient + telemetry-writer lazy singletons (Stream 1 / v0.2.20) ────
#
# These caches replace the inline ``aiohttp`` POSTs that used to live in
# ``_rl_cache_and_rerank`` and ``_rl_answer_monitor``.
#
# v0.2.42 RT-1: re-key RLClient singleton on active_embedding so a
# mid-session ACTIVE_EMBEDDING flip (e.g. user switches from qwen3 to
# arctic2 via the launcher's embedding dropdown) produces a client whose
# `active_embedding` attribute matches the current env value rather than
# freezing the value read at import time (which was ACTIVE_EMBEDDING, a
# module-level constant).  Pre-fix this was a bare None — replacing with
# a dict keyed by embedding name mirrors F2's telemetry-writer fix.
_rl_client_instances: dict = {}  # type: ignore[var-annotated]
# v0.2.40 F2: re-key telemetry writers on (project, embedding_source) so
# mid-session env changes (ACTIVE_EMBEDDING flip, PROJECT_NAME re-resolution
# from launcher.db adopt) produce a writer tagged with the CURRENT env
# rather than freezing first-call values for the MCP subprocess lifetime.
# Pre-fix this was a single Optional[RLTelemetryWriter] singleton.
_rl_telemetry_writers: dict = {}  # type: ignore[var-annotated]
# Kept as None-valued shim for back-compat: tests / external code that
# directly reset srv._rl_telemetry_writer_instance = None still work
# (the factory now consults the dict, so the legacy global is a tombstone
# read-only sentinel — kept to avoid AttributeError in older callers).
_rl_telemetry_writer_instance = None  # type: ignore[var-annotated]
# Legacy alias kept so external code that holds a reference to
# `srv._rl_client_instance` (e.g. older test shims) still resolves a
# value rather than raising AttributeError.  The dict is the live
# storage; this is a read-only tombstone.
_rl_client_instance = None  # type: ignore[var-annotated]

# Bound on the per-event node list for STRUCTURAL telemetry. The structural
# branches already cap their result lists (<= 64); this is a defensive second
# bound so a future uncapped branch can't balloon the event payload.
_CODE_STRUCTURE_TELEMETRY_MAX_NODES = 64

DUAL_RL_LOG_ENABLED_ENV = "DUAL_RL_LOG_ENABLED"


def next_rl_call_seq() -> int:
    """Increment and return the per-process RL call sequence.

    ONE home for the counter (v0.2.75 P3g). ``rl_client.search_pipeline`` used
    to do ``srv._rl_call_seq += 1`` — a rebind on the ``server`` namespace that
    only worked while the counter's definition ALSO lived on ``server``. With
    the definition here, a re-exported server-side copy would desync on that
    rebind, so callers route through this incrementer instead (the counter's
    single authoritative storage is this module's ``_rl_call_seq``).
    """
    global _rl_call_seq
    _rl_call_seq += 1
    return _rl_call_seq
