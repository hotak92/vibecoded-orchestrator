# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Citation compute core — shared home (v0.2.70).

ONE home for "given a complete answer + the staged retrieval ctx, compute the
citation event and write it." Called by BOTH:

  * the in-process MCP monitor (``weaviate_mcp.server._rl_answer_monitor``),
    which becomes a thin caller, and
  * the turn-end Stop-hook drain (``scripts/rl_drain_citations.py``), which
    recovers hook-path citations.

Honours the modularity ruling (one concern, one home) + the
>50-lines-to-a->5k-line-file extraction rule (server.py is ~8k lines). The
server.py-local primitives this depends on (``Chunker``, the embedding service,
``_cosine``, ``_rl_is_literal_cited``, the telemetry writer) are reached via a
lazy import of ``weaviate_mcp.server`` at call time — the same defensible
dependency direction ``search_pipeline.py`` uses (this module lives in
``rl_client`` alongside the writer it orchestrates; server.py owns the
MCP-tool-shape concerns + module globals).

Signal contract (do NOT change shape): per node,
``cosine_sims[title] = max over answer-chunks of _cosine(answer_chunk, n_emb)``;
``literal_cited[title]`` via the node-identity word-boundary check;
``compute_unified_targets`` (vendored formula) derives the binary ``cited`` map.
Tool RETURNS are already excluded upstream by the answer-window extractor.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = ["compute_citation", "CitationResult"]


class CitationResult(dict):
    """Result of one citation compute. A plain dict subclass for backwards
    compatibility with the MCP monitor's existing ``citation_result["..."]``
    reads (cosine_sims / literal_cited / cited)."""


def compute_citation(
    task_id: str,
    answer: str,
    ctx: dict,
    *,
    write: bool = True,
) -> Optional[CitationResult]:
    """Compute (and optionally write) the citation event from a complete answer.

    Args:
        task_id: The retrieval task id (pairing key for the citation event).
        answer: The accumulated answer window (assistant text+thinking+
            tool_use-input; tool returns already excluded).
        ctx: The staged retrieval ctx — same shape as
            ``_rl_node_content_cache[task_id]``:
            ``{nodes: [...], query_emb, active_model, embedding_source,
            embedding_dim, project_id, project_name, task_type}``.
            Each node carries ``n_emb`` (or ``emb``) for the cosine side.
        write: When True (default) write the citation event via the telemetry
            writer. The drain sets True; a dry-run caller can set False to get
            the computed maps without persisting.

    Returns:
        ``CitationResult`` with keys ``cosine_sims`` / ``literal_cited`` /
        ``cited`` on success, or None on soft-fail (no nodes / no embedding
        service / chunker error / no trainable signal). Also mutates ``ctx`` in
        place with ``cosine_sims_computed`` / ``literal_cited_computed`` so a
        downstream /rl_update POST can reuse them without re-embedding (matches
        the pre-extraction in-MCP behaviour).

    Soft-fail throughout — never raises into the monitor / drain.
    """
    nodes = ctx.get("nodes") or []
    if not nodes:
        return None

    # Lazy import the server-local primitives (defensible direction; see
    # module docstring). A failed import → soft-fail None.
    try:
        from claude_mcp_servers.weaviate_mcp.server import (
            Chunker,
            _cosine,
            _get_embedding_service,
            _get_rl_telemetry_writer,
            _rl_is_literal_cited,
            EMBEDDING_MODEL,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("citation_compute: server import failed (%s)", exc)
        return None

    active_model = ctx.get("active_model") or EMBEDDING_MODEL

    # --- Step 1: chunk + embed the answer ---
    try:
        chunker = Chunker.for_model(active_model)
        chunks = chunker.chunk_text(answer, source_id=task_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("citation_compute: chunker failed (%s)", exc)
        return None
    if not chunks:
        return None

    svc = _get_embedding_service()
    if svc is None:
        logger.debug("citation_compute: no EmbeddingService available; skip")
        return None

    answer_chunk_embs: list[list[float]] = []
    for chunk in chunks:
        text = getattr(chunk, "content", None) or (
            chunk if isinstance(chunk, str) else None
        )
        if not text:
            continue
        try:
            vec = svc.embed_text(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("citation_compute: chunk embed failed (%s); continuing", exc)
            continue
        if vec:
            answer_chunk_embs.append(vec)

    if not answer_chunk_embs:
        return None

    # --- Step 2: per-node cosine_sims (max over answer chunks vs node.n_emb) ---
    # F-C (CORRECTED v0.2.70): the cure for the 0.08% starvation is ALWAYS
    # HAVING THE EMBEDDING (F-G attaches/regenerates the node vector upstream so
    # cosine is computable for every node whose text we have), NOT lowering a
    # bar. The formula here drops a no-cosine node (it iterates cosine keys
    # only), so we must NOT fabricate a target for it. A node that STILL has no
    # vector at this point means F-G's attach+regenerate genuinely failed (no
    # text / embed service down): DROP it (pre-F-C behaviour) and log at INFO so
    # the drop is auditable (F-LOG). Never record it into literal_cited (that
    # was the withdrawn poisoning path).
    answer_lower = answer.lower()
    cosine_sims: dict[str, float] = {}
    literal_cited: dict[str, bool] = {}
    dropped_no_vector = 0
    for n in nodes:
        if not isinstance(n, dict):
            continue
        title = n.get("title", "")
        if not title:
            continue
        n_emb = n.get("n_emb") or n.get("emb")
        if not n_emb:
            dropped_no_vector += 1
            continue
        try:
            best = max(_cosine(ac, n_emb) for ac in answer_chunk_embs)
        except Exception:  # noqa: BLE001
            continue
        cosine_sims[title] = float(best)
        literal_cited[title] = _rl_is_literal_cited(n, answer_lower)

    if dropped_no_vector:
        logger.info(
            "citation_compute %s: dropped %d node(s) with no embedding "
            "(F-G attach+regenerate failed: no text / embed service down)",
            task_id[:8], dropped_no_vector,
        )

    if not cosine_sims:
        return None

    # --- Step 3: unified-target formula → binary cited dict ---
    try:
        from vco_lib.rl_training_targets import compute_unified_targets

        _targets, cited = compute_unified_targets(
            cosine_sims,
            literal_cited=literal_cited,
            cross_encoder_cited=None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("citation_compute: compute_unified_targets failed (%s)", exc)
        return None

    # --- Step 4: write the citation event via the centralized writer ---
    if write:
        try:
            writer = _get_rl_telemetry_writer()
            if writer is None:
                return None
            writer.log_citations(
                task_id=task_id,
                task_type=ctx.get("task_type") or "mcp_interactive",
                citations={t: bool(v) for t, v in cited.items()},
                cosine_sims=cosine_sims,
                literal_cited=literal_cited,
                cross_encoder_cited=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("citation_compute: writer.log_citations failed (%s)", exc)
            return None

    ctx["cosine_sims_computed"] = cosine_sims
    ctx["literal_cited_computed"] = literal_cited

    logger.debug(
        "citation_compute %s: %d cosine, %d literal-cited, %d cited",
        task_id[:8],
        len(cosine_sims),
        sum(1 for v in literal_cited.values() if v),
        sum(1 for v in cited.values() if v),
    )
    return CitationResult({
        "cosine_sims": cosine_sims,
        "literal_cited": literal_cited,
        "cited": cited,
    })
