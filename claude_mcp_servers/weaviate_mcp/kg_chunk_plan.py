# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The KG chunk plan — ONE computation shared by every KG writer (W8).

WHY THIS MODULE (v0.2.92 wiring audit, minor-W8): two writers produced
DIFFERENT chunk plans for the SAME node. The MCP ``store_knowledge_node``
gated on a hardcoded ``_MAX_SINGLE_CHUNK_TOKENS = 2000`` ("legacy arctic
limit"), measured the gate with the Ollama tokenizer
(``count_tokens_async``), prefixed ``[chunk N/M]`` into the stored
``content``, wrote ``source_node_id = title`` and no ``content_hash``;
kg-sync gated on the ACTIVE model's preset max (8 192 for qwen3), measured
with the chunker's deterministic ``TokenCounter``, stored RAW chunk
content, ``source_node_id = uuid4`` and a ``content_hash``. A node written
by the MCP tool was therefore re-planned and re-embedded by the next
``kg-sync --all``, and ``_stored_plan_matches_current`` (the ``--rechunk``
comparison that decides what to re-embed) could never judge it current.

This module is the one plan both writers (and the plan COMPARISON) call:

* the single-vs-multi gate uses the ACTIVE model's preset max, measured in
  the chunker's ONE budget unit (``TokenCounter`` — deterministic character
  arithmetic, D16), never a hardcoded token constant and never a tokenizer
  whose unit drifts from the budget's;
* the boundaries come from ``Chunker.for_model(active_model_id)`` — the
  ACTIVE model's own preset, unclamped (the no-functionality-loss rule);
* the stored form is RAW chunk content (``chunk.content`` — no ordering
  prefix: readers prefer the ``chunk_num`` / ``total_chunks`` properties,
  which every writer stores, and fall back to parsing a prefix only on
  legacy rows that carry one).

Both writers additionally stamp ``content_hash`` (the shared
``vco_lib.knowledge_residue.content_signature_excluding_updated``) and a
per-write ``source_node_id = uuid4()``, so an MCP-written row is
skip-eligible and plan-comparable on the next kg-sync. The parity is pinned
by ``tests/test_wiring_w8_shared_chunk_plan.py`` (same node through BOTH
production entry points → identical stored plans).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# SIBLING-FIRST import. This package is imported both as ``weaviate_mcp``
# (pip-installed editable) and as ``claude_mcp_servers.weaviate_mcp``
# (repo-root path), so a fallback is needed — but the RELATIVE form must be
# tried FIRST, not second.
#
# Measured 2026-09-05: with a second orchestrator checkout on ``sys.path``
# (the dogfood tree, which every maintainer and every in-place updater has),
# an absolute ``claude_mcp_servers.weaviate_mcp.chunking`` resolved to the
# OTHER checkout's chunking module — pre-v0.2.92 presets, qwen3 max 13 500
# instead of 8 192 — while ``kg_chunk_plan`` itself was this tree's file.
# The plan then used a FOREIGN preset while the writers around it counted
# with their own sibling ``TokenCounter``: a 9 354-unit node planned single
# here and multi there. That is the exact two-writers-disagree defect W8
# exists to remove, reintroduced one import line lower.
#
# ``.chunking`` can only ever bind THIS package's module, and every other
# file in the package (``server.py``, ``embeddings.py``, ``rl_state.py``)
# already imports it that way. The absolute form stays only for the case
# the relative one genuinely cannot serve: this file loaded as a top-level
# module with no package context.
try:
    from .chunking import (  # noqa: E402
        Chunker,
        TokenCounter,
        chunking_preset_for_model,
    )
except ImportError:  # pragma: no cover — loaded without package context
    from claude_mcp_servers.weaviate_mcp.chunking import (  # type: ignore[no-redef]  # noqa: E402
        Chunker,
        TokenCounter,
        chunking_preset_for_model,
    )

__all__ = [
    "KGChunkPlan",
    "LEGACY_FALLBACK_MAX_TOKENS",
    "single_chunk_threshold",
    "chunker_for_model",
    "plan_node_chunks",
]

#: Fallback single-chunk gate when the active model id is empty/unregistered.
#: THE one number: before this module the two writers' no-model fallbacks
#: disagreed — kg-sync's module-level ``MAX_EMBEDDING_TOKENS = 2500`` (now
#: deleted from that script) vs the MCP's ``_MAX_SINGLE_CHUNK_TOKENS = 2000``
#: (now deleted from server.py). Both are gone; this is where the value lives.
LEGACY_FALLBACK_MAX_TOKENS = 2500

#: Fallback chunker for an empty/unregistered model id — the pre-v0.2.28
#: hardcoded preset kg-sync used (mirrored so a model-less service sees the
#: same plan from both writers).
_LEGACY_FALLBACK_CHUNKER = Chunker(min_tokens=1500,
                                   max_tokens=LEGACY_FALLBACK_MAX_TOKENS,
                                   target_tokens=2500)


def single_chunk_threshold(model_id: str) -> int:
    """Token threshold (chunker budget units) below which a node stays WHOLE.

    The ACTIVE model's own preset max — the same number that sizes the
    chunks must decide WHETHER to chunk (kg-sync's invariant: "the branch
    decision and the actual chunk size come from the SAME preset").
    """
    try:
        if model_id:
            _min_t, max_t, _tgt = chunking_preset_for_model(model_id)
            return max_t
    except Exception:  # noqa: BLE001 — unregistered model → legacy fallback
        pass
    return LEGACY_FALLBACK_MAX_TOKENS


def chunker_for_model(model_id: str) -> "Chunker":
    """The chunker for ``model_id`` — active-model preset, unclamped.

    Empty/unregistered model id → the legacy fallback preset (see
    :data:`LEGACY_FALLBACK_MAX_TOKENS`), matching kg-sync's pre-shared
    behaviour for test harnesses that construct services without a model.
    """
    if not model_id:
        return _LEGACY_FALLBACK_CHUNKER
    return Chunker.for_model(model_id)


@dataclass
class KGChunkPlan:
    """The chunk plan for ONE node's content under ONE active model.

    ``is_single`` nodes are stored as one row with the content VERBATIM
    (NOT ``chunker.chunk_text``-stripped — stripping single-chunk content
    would churn re-embeds); ``chunks`` is empty and ``total == 1``.
    Multi-chunk nodes store one row per :class:`Chunk`, content RAW
    (no ordering prefix), ``chunk_num`` 1-indexed, ``total_chunks`` shared.
    """

    is_single: bool
    total: int
    chunks: "List[Any]" = field(default_factory=list)
    threshold: int = 0

    @property
    def chunk_count(self) -> int:
        return 1 if self.is_single else len(self.chunks)


def plan_node_chunks(
    content: str,
    model_id: str,
    source_id: str = "",
    metadata: "Optional[Dict[str, Any]]" = None,
) -> "KGChunkPlan":
    """Plan how ``content`` is chunked for storage under ``model_id``.

    ONE computation, deterministic in ``(content, model_id)`` — the SAME
    node given to the MCP store and to kg-sync yields the SAME plan (the
    W8 parity pin). ``source_id`` / ``metadata`` attach to the produced
    chunks and do not affect boundaries.
    """
    threshold = single_chunk_threshold(model_id)
    if TokenCounter.count_tokens(content) <= threshold:
        return KGChunkPlan(is_single=True, total=1, chunks=[], threshold=threshold)
    chunker = chunker_for_model(model_id)
    chunks = chunker.chunk_text(
        text=content, source_id=source_id, metadata=metadata or {}
    )
    return KGChunkPlan(
        is_single=False, total=len(chunks), chunks=chunks, threshold=threshold
    )
