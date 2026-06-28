# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Oversized-query handling for the HOOK retrieval path — shared home (v0.2.70).

ONE home for "the QUERY itself exceeds the embedding model's max chunk size".
HOOK retrieval only (rl_kg_search.py / search_pipeline) — MCP calls do NOT use
this; they let Weaviate handle oversize as today.

Flow (only when the query is oversize — the common small-query case is left to
the caller's single-retrieval path, unchanged):
  1. Detect: ``is_oversized(query, model)`` — TokenCounter vs the model's max
     from ``chunking_preset_for_model``.
  2. Chunk the query via the SHARED ``Chunker.for_model`` (same model-aware
     primitive sync uses — so chunk size matches the active embedding model).
     Gives Q query chunks.
  3. Retrieve per chunk (via an injected ``retrieve_fn`` so the caller owns the
     Weaviate fan-out):
       - KG: N+1 results per chunk.
       - CodeGraph: ceil(N / Q) results per chunk.
  4. Combine:
       - KG: pool all nodes (dedup by node identity). Rerank EACH pooled node
         against EACH query chunk; take the MAX over (node_chunk × query_chunk)
         pairs (the multi-query-chunk generalization of the single-query rerank
         — reuses the SAME ``_cosine`` scoring primitive, not a second copy).
         Return top-N by that max score.
       - CodeGraph: the deduplicated UNION of per-chunk results (no rerank).

The rerank reuses ``weaviate_mcp.server._cosine`` (the existing scorer) and the
node-dedup / top-N selection mirrors ``_collapse_to_one_per_node`` + score-sort
that the single-query path uses — no parallel reranker.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "is_oversized",
    "chunk_query",
    "kg_results_per_chunk",
    "codegraph_results_per_chunk",
    "combine_kg_results",
    "combine_codegraph_results",
]


def _model_max_tokens(model_name: str) -> Optional[int]:
    try:
        from claude_mcp_servers.weaviate_mcp.chunking import chunking_preset_for_model
        preset = chunking_preset_for_model(model_name)
        # preset = (min_tokens, MAX_tokens, target_tokens)
        if isinstance(preset, (tuple, list)) and len(preset) >= 2:
            return int(preset[1])
    except Exception as exc:  # noqa: BLE001
        logger.debug("query_chunking: preset lookup failed (%s)", exc)
    return None


def is_oversized(query: str, model_name: str) -> bool:
    """True iff the query token count exceeds the model's max chunk size."""
    if not query:
        return False
    max_tokens = _model_max_tokens(model_name)
    if not max_tokens:
        return False
    try:
        from claude_mcp_servers.weaviate_mcp.chunking import TokenCounter
        return TokenCounter.count_tokens(query) > max_tokens
    except Exception as exc:  # noqa: BLE001
        logger.debug("query_chunking: token count failed (%s)", exc)
        return False


def chunk_query(query: str, model_name: str) -> list[str]:
    """Chunk an oversized query via the shared model-aware Chunker.

    Returns the list of query-chunk texts (Q items). On any failure returns the
    whole query as a single chunk (degrades to the single-retrieval path).
    """
    try:
        from claude_mcp_servers.weaviate_mcp.chunking import Chunker
        chunker = Chunker.for_model(model_name)
        chunks = chunker.chunk_text(query, source_id="oversized-query")
        texts = [
            (getattr(c, "content", None) or (c if isinstance(c, str) else "")).strip()
            for c in chunks
        ]
        texts = [t for t in texts if t]
        return texts or [query]
    except Exception as exc:  # noqa: BLE001
        logger.debug("query_chunking: chunk_query failed (%s); using whole query", exc)
        return [query]


def kg_results_per_chunk(n: int) -> int:
    """KG: N+1 results per query chunk."""
    return max(1, n) + 1


def codegraph_results_per_chunk(n: int, q: int) -> int:
    """CodeGraph: ceil(N / Q) results per query chunk (>= 1)."""
    q = max(1, q)
    return max(1, math.ceil(max(1, n) / q))


def _node_key(node: dict) -> tuple:
    """Node identity for dedup — mirrors _collapse_to_one_per_node's key.

    Delegates to the shared ``content_dedup.node_identity_key`` (one home) so
    the KG identity definition is identical across this module,
    ``server._collapse_to_one_per_node``, and the seen-store layer.
    """
    from claude_mcp_servers.rl_client.content_dedup import node_identity_key
    return node_identity_key(node)


def combine_kg_results(
    pooled_per_chunk: list[list[dict]],
    query_chunk_embs: list[list[float]],
    n: int,
    *,
    cosine_fn: Callable[[Any, Any], float] | None = None,
) -> list[dict]:
    """Pool + dedup KG nodes, rerank each by MAX over (node × query-chunk), top-N.

    Args:
        pooled_per_chunk: per-query-chunk lists of node dicts (each carrying
            ``emb`` / ``n_emb``).
        query_chunk_embs: the embedding of each query chunk (parallel concept —
            used as the rerank's query side). May be shorter/empty; nodes then
            fall back to their existing ``score``.
        n: the post-rerank count to return (top-N).
        cosine_fn: the scoring primitive; defaults to ``server._cosine`` (the
            existing scorer — NOT a second copy).

    Returns the top-N deduplicated nodes by max-over-pairs score, each annotated
    with ``oversized_query_score``.
    """
    if cosine_fn is None:
        from claude_mcp_servers.weaviate_mcp.server import _cosine as cosine_fn

    from claude_mcp_servers.rl_client.content_dedup import dedup_by_content_identity

    # Pool + dedup by node identity, keeping the first-seen dict (and its emb).
    by_key: dict[tuple, dict] = {}
    for chunk_nodes in pooled_per_chunk:
        for node in chunk_nodes:
            if not isinstance(node, dict):
                continue
            key = _node_key(node)
            if key not in by_key:
                by_key[key] = node

    # Content-identity dedup, PRE-rerank (coordinator refinement #1): collapse
    # any (name, content_hash)-identical survivors BEFORE we spend cosine compute
    # on them. Catches the cross-collection duplicate (same node title + body
    # under project + shared KG) that identity dedup above misses because their
    # file_path differs. Over-collapse guard lives in the shared helper: two
    # DISTINCT-title nodes with coincidentally-identical bodies are NOT merged.
    pooled_nodes = dedup_by_content_identity(by_key.values(), kind="kg")

    scored: list[tuple[float, dict]] = []
    for node in pooled_nodes:
        node_emb = node.get("n_emb") or node.get("emb")
        if node_emb and query_chunk_embs:
            # MAX over (node_chunk × query_chunk) pairs — here one node vector
            # vs each query chunk vector (the single-query rerank generalized to
            # multiple query chunks). Reuses the existing cosine scorer.
            best = max(
                (cosine_fn(qe, node_emb) for qe in query_chunk_embs),
                default=0.0,
            )
        else:
            # No comparable vector → fall back to the node's existing score so
            # it is still rankable (never silently dropped).
            best = float(node.get("score") or 0.0)
        node["oversized_query_score"] = float(best)
        scored.append((float(best), node))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [node for _score, node in scored[: max(1, n)]]


def combine_codegraph_results(pooled_per_chunk: list[list[dict]]) -> list[dict]:
    """CodeGraph: deduplicated UNION of per-chunk results (no rerank).

    RESERVED scaffolding — intentionally unwired this release (exported + tested
    but never invoked by a live path). The KG counterparts above
    (``combine_kg_results`` / ``kg_results_per_chunk``) ARE called from the hook
    retrieval path; the codegraph hook surface uses the shell CLI, which has no
    oversized-query branch (codegraph queries are always short symbol/path
    tokens), so there is no caller today. This (and ``codegraph_results_per_chunk``)
    is kept ready for a future codegraph-oversized-query surface — it is NOT a
    dead/forgotten path, so do not delete it and do not wire it without a real
    oversized-codegraph-query need.

    Two-axis dedup via the shared ``content_dedup`` helper (one home):
      1. IDENTITY union — collapse the same entity (full_name/endpoint/path/
         title) surfaced by multiple query chunks.
      2. CONTENT-IDENTITY — then collapse any survivors that share BOTH a name
         AND a body fingerprint (the maintainer's name+hash bar). The
         over-collapse guard in the helper keeps two distinct entities with
         coincidentally-identical bodies separate (different full_name).
    """
    from claude_mcp_servers.rl_client.content_dedup import (
        code_identity_key,
        dedup_by_content_identity,
    )

    by_key: dict[Any, dict] = {}
    for chunk_nodes in pooled_per_chunk:
        for node in chunk_nodes:
            if not isinstance(node, dict):
                continue
            key = code_identity_key(node)
            if key not in by_key:
                by_key[key] = node
    return dedup_by_content_identity(by_key.values(), kind="code")
