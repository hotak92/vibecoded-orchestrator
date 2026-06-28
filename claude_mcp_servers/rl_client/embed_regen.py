# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""On-the-fly node-embedding regeneration — shared home (F-G, v0.2.70).

ONE home for "regenerate a node's embedding from its chunk TEXT when the stored
active-slot vector is genuinely unavailable." This is the cure for the 0.08%
citation starvation: the disease was the MISSING EMBEDDING, not a threshold.
When a node's stored vector can't be pulled (object fetched without the named
vector, or the slot is empty), we regenerate it from the node's text so cosine
is ALWAYS computable — a node whose text the answer didn't actually use then
gets a legitimately LOW cosine (the TRUE signal), instead of being dropped or
fabricated-to-cited.

Reuses the EXISTING primitives — does NOT write a new embedder or chunker:
  * model-aware chunk sizing via ``weaviate_mcp.chunking.chunking_preset_for_model``
    + ``TokenCounter`` (so we embed the same shape sync_knowledge_graph stored),
  * the embed call routed through the SAME ``EmbeddingService.embed_text`` the
    answer chunks use (guaranteeing the regenerated vector lives in the active
    model's space — the F-D cross-model invariant holds by construction), which
    itself wraps ``vco_lib/embedding_providers/ollama.py``'s ``/api/embed``.

Imported by ``weaviate_mcp.server._rl_regenerate_node_vector`` (the MCP +
hook enrich path both route through it) so there is exactly one copy of this
logic. Soft-fail: returns None on genuine failure (no text / embed service
down) and the caller DROPS the node — never fabricates.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["regenerate_node_vector"]


def regenerate_node_vector(
    text: str,
    model_name: str,
    *,
    embedding_service=None,
) -> "list[float] | None":
    """Regenerate a node embedding from its chunk text. Returns None on failure.

    Args:
        text: The node's chunk text (the stored ``content`` — the full chunk,
            not a truncated snippet, so it matches what sync embedded).
        model_name: Active embedding model id (for the chunk-size preset).
        embedding_service: The ``EmbeddingService`` whose ``embed_text`` will be
            used. When None, resolved lazily via
            ``weaviate_mcp.server._get_embedding_service`` (the SAME cached
            instance the answer chunks use). Injectable for tests.

    Returns:
        The regenerated vector (active model's space), or None when the text is
        empty / no embedding service / the embed call fails.
    """
    if not text or not text.strip():
        return None

    svc = embedding_service
    if svc is None:
        try:
            from claude_mcp_servers.weaviate_mcp.server import _get_embedding_service
            svc = _get_embedding_service()
        except Exception as exc:  # noqa: BLE001
            logger.debug("embed_regen: cannot resolve EmbeddingService (%s)", exc)
            return None
    if svc is None:
        return None

    # Size to the model-aware chunk preset so we embed the same shape the sync
    # pipeline stored. A single representative chunk is enough for the
    # downstream max-over-chunks cosine.
    sized_text = text
    try:
        from claude_mcp_servers.weaviate_mcp.chunking import (
            chunking_preset_for_model,
            TokenCounter,
        )
        try:
            # chunking_preset_for_model -> (min_tokens, MAX_tokens, target_tokens)
            preset = chunking_preset_for_model(model_name)
            max_tokens = (
                preset[1]
                if isinstance(preset, (tuple, list)) and len(preset) >= 2
                else None
            )
        except Exception:
            max_tokens = None
        if max_tokens and TokenCounter.count_tokens(text) > max_tokens:
            # Truncate by chars (1 token ≈ 4 chars) — cheap, dependency-light.
            sized_text = text[: max_tokens * 4]
    except Exception:
        pass

    try:
        vec = svc.embed_text(sized_text)
        return vec if vec else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("embed_regen: embed_text failed (%s)", exc)
        return None
