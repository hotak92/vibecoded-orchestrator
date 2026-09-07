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

import asyncio
import logging
from typing import Any, Callable, Optional

# W3 (v0.2.92 wiring audit): the ONE shared single-slot truncation-record
# merger — the same home the enrichment flush uses, so the two patch
# writers cannot drift on the merge rule. vco_lib is part of every healthy
# install; a failed import is a broken install, never a fallback case.
from vco_lib.kg_truncation_tags import merge_slot_truncation

logger = logging.getLogger(__name__)

__all__ = [
    "regenerate_node_vector",
    "regenerate_node_vector_tagged",
    "ensure_slot_embedding",
]

# v0.2.71 Sweep-C Piece 2: strong refs for fire-and-forget store-back tasks.
# Mirrors the ``_rl_monitor_tasks`` pattern in search_pipeline.py — without a
# strong ref the event loop's only reference to a bare ``create_task`` result
# is weak, so the GC can collect the task mid-await and the store silently
# never lands. The done-callback discards on completion so the set stays small.
_store_back_tasks: "set[asyncio.Task]" = set()

# v0.2.73 retrieval-I/O bound: dedup the fire-and-forget slot store-backs.
# ``ensure_slot_embedding`` schedules a Weaviate ``.data.update(vector=...)``
# per node whose slot is missing. With dual-write enabled a single search can
# fire up to ~limit*2 such patches, and EVERY subsequent search re-fires them
# for the same objects — an unbounded background write storm on a large
# collection (exactly the I/O class this release reduces). The store is
# IDEMPOTENT (writing the same slot vector again is a no-op-equivalent), so once
# we've scheduled a backfill for a given (collection, uuid, slot) this process
# never needs to schedule it again. This set caps each object-slot to ONE
# store-back per process. Bounded to _STORE_DEDUP_MAX entries (FIFO-ish drop) so
# a very long-lived server can't grow it without limit; a dropped key at worst
# allows one redundant (still-idempotent) re-store later.
#
# Key = (collection_name, uuid, slot). The collection name is part of the key
# because a Weaviate object UUID is unique only WITHIN a collection — the same
# UUID can legitimately exist in two collections (a single hybrid_search enriches
# project-KG + shared-KG + dev, each its own handle). Keying on (uuid, slot)
# alone would let a genuinely-needed store-back in collection B be suppressed by a
# dedup hit from collection A's identical UUID — a MISSED write. Including the
# collection makes correctness independent of the UUID-generation scheme (random
# v4 today, but deterministic UUIDs are already used elsewhere for code-graph
# rows) rather than relying on cross-collection UUID uniqueness.
_stored_slots: "set[tuple[str, str, str]]" = set()
_STORE_DEDUP_MAX = 50_000


def regenerate_node_vector(
    text: str,
    model_name: str,
    *,
    embedding_service=None,
    embed_fn: Optional[Callable[[str], "list[float] | None"]] = None,
) -> "list[float] | None":
    """Regenerate a node embedding from its chunk text. Returns None on failure.

    Args:
        text: The node's chunk text (the stored ``content`` — the full chunk,
            not a truncated snippet, so it matches what sync embedded).
        model_name: Embedding model id (for the chunk-size preset). MUST be the
            model whose SPACE the returned vector should live in — for an
            other-slot backfill (v0.2.71 dual-log) pass the OTHER model's id, not
            the active one, so the chunk preset matches that model's num_ctx
            (the chunk-asymmetry constraint: arctic ~4x more chunks than qwen3).
        embedding_service: The ``EmbeddingService`` whose ``embed_text`` will be
            used when ``embed_fn`` is None. When both are None it is resolved
            lazily via ``weaviate_mcp.server._get_embedding_service`` (the SAME
            cached instance the answer chunks use). Injectable for tests.
        embed_fn: Optional explicit embed callable ``(sized_text) -> vec|None``.
            When supplied it OVERRIDES ``embedding_service.embed_text`` — this is
            how the OTHER-slot backfill embeds in a NON-active model's space
            (e.g. a closure routing to ``svc.ollama.embed(other_model_id, ...)``)
            while still honouring this function's model-aware chunk sizing. The
            active-slot path leaves it None and uses ``embed_text`` as before.

    Returns:
        The regenerated vector (in ``model_name``'s space), or None when the
        text is empty / no embedding service / the embed call fails.
    """
    vec, _truncated = regenerate_node_vector_tagged(
        text, model_name, embedding_service=embedding_service, embed_fn=embed_fn
    )
    return vec


def regenerate_node_vector_tagged(
    text: str,
    model_name: str,
    *,
    embedding_service=None,
    embed_fn: Optional[Callable[[str], "list[float] | None"]] = None,
) -> "tuple[Optional[list[float]], bool]":
    """``regenerate_node_vector`` + the leading-window verdict (W3).

    Same computation, same arguments, same failure modes — split out so the
    dual-log backfill (``ensure_slot_embedding``) can persist the verdict
    without re-deriving the sized window. Returns ``(vec, truncated)`` where
    ``truncated`` is True exactly when the embedded text was cut to the
    model preset's leading window (``len(sized_text) < len(text)``) — a
    POSITIVE fact about what WE sent. A False means "no positive evidence
    of truncation", which the shared merge treats as no change, never as a
    provable full-fidelity claim (a runner-side refusal retry inside the
    service is not visible here; that deeper case stays UNKNOWN rather
    than being asserted either way).
    """
    if not text or not text.strip():
        return None, False

    svc = embedding_service
    if svc is None and embed_fn is None:
        try:
            from claude_mcp_servers.weaviate_mcp.server import _get_embedding_service
            svc = _get_embedding_service()
        except Exception as exc:  # noqa: BLE001
            logger.debug("embed_regen: cannot resolve EmbeddingService (%s)", exc)
            return None, False
    if svc is None and embed_fn is None:
        return None, False

    # Size to the model-aware chunk preset so we embed the same shape the sync
    # pipeline stored. A single representative chunk is enough for the
    # downstream max-over-chunks cosine.
    sized_text = text
    try:
        from claude_mcp_servers.weaviate_mcp.chunking import (
            chunking_preset_for_model,
            CHARS_PER_TOKEN_TEXT,
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
            # Truncate by chars — cheap, dependency-light. The ratio is
            # `chunking.CHARS_PER_TOKEN_TEXT` (v0.2.92 register #50), read from
            # the SAME import that already brought in the preset + counter, so
            # the budget and the count cannot disagree.
            sized_text = text[: max_tokens * CHARS_PER_TOKEN_TEXT]
    except Exception:
        pass

    try:
        vec = embed_fn(sized_text) if embed_fn is not None else svc.embed_text(sized_text)
        return (vec if vec else None), len(sized_text) < len(text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("embed_regen: embed call failed (%s)", exc)
        return None, False


def ensure_slot_embedding(
    obj_uuid: Any,
    content_text: str,
    slot: str,
    model_id: str,
    collection: Any,
    svc: Any,
    *,
    embed_fn: Optional[Callable[[str], "list[float] | None"]] = None,
    existing_props: "Optional[dict]" = None,
) -> "list[float] | None":
    """v0.2.71 Sweep-C Piece 2 — compute a missing slot vector AND store it back.

    Composes the two EXISTING primitives (does NOT reinvent either):
      * COMPUTE: ``regenerate_node_vector`` — model-aware chunk sizing keyed on
        ``model_id`` (pass the TARGET slot's model so the chunk preset matches),
        embedding via ``embed_fn`` (other-slot) or ``svc.embed_text`` (active).
      * STORE: ``collection.data.update(uuid=obj_uuid, vector={slot: vec})`` —
        the canonical single-named-vector enrichment patch (the same call shape
        ``vco_lib/embedding_enrichment.py`` already uses in production).

    W3 (v0.2.92 wiring audit): the store-back also merges the leading-window
    verdict into the row's stored truncation record via the SHARED merger
    (``vco_lib.kg_truncation_tags.merge_slot_truncation``) when the caller
    supplies ``existing_props`` (the row's current properties) — the same
    one-home merge the enrichment flush uses, so the two single-slot patch
    writers cannot drift. A row without a complete ``truncated_slots``
    record merges to NOTHING (state stays UNKNOWN, never a guessed False);
    the vector is still stored exactly as before.

    Sync use, async store: the computed vector is RETURNED immediately for this
    request's cosine / dual-log event; the Weaviate write is scheduled
    fire-and-forget via ``asyncio.create_task`` with a strong ref in
    ``_store_back_tasks`` so the retrieval hot-path latency is unchanged. The
    store soft-fails (log.debug, never raises into the caller).

    Args:
        obj_uuid: UUID of the surviving chunk object to patch.
        content_text: The chunk's stored ``content`` (full chunk, not a snippet).
        slot: The named-vector slot to fill (e.g. ``qwen3_embed``).
        model_id: The model id whose space ``slot`` lives in (drives chunk preset
            AND must match ``embed_fn``'s target model — for the other slot pass
            the OTHER model, not the active one).
        collection: A Weaviate collection handle with ``.data.update(...)``.
        svc: The ``EmbeddingService`` (used by ``regenerate_node_vector`` when
            ``embed_fn`` is None — the active-slot self-heal path).
        embed_fn: Optional explicit embed callable for a NON-active model's
            space (other-slot backfill). None → active model via ``svc``.
        existing_props: The row's CURRENT properties (for the W3
            truncation-record merge). None → no merge (vector-only store,
            the row's state stays as-is).

    Returns:
        The freshly-computed vector, or None on soft-fail (no text / embed down).
        None means "do not fill" — the caller keeps its existing skip/drop
        behaviour. The store is only scheduled when a vector was computed.
    """
    if not content_text or not content_text.strip():
        return None

    vec, truncated = regenerate_node_vector_tagged(
        content_text,
        model_id,
        embedding_service=svc,
        embed_fn=embed_fn,
    )
    if not vec:
        return None

    # W3: the shared single-slot merge. ``None`` back from it means "no
    # honest merge" (the row predates the complete record, the active slot
    # name is unavailable, or the verdict below is unprovable) and the store
    # writes the vector ONLY.
    #
    # The VERDICT is tri-state, and which branch produced the vector decides
    # whether a negative is provable:
    #   * ``truncated`` True  — WE cut the text to the model preset's leading
    #                           window before embedding. Provable either way.
    #   * ``embed_fn`` set    — the OTHER-slot backfill embeds through
    #                           ``_embed_text_in_other_model``, which does NOT
    #                           shrink: an over-window input comes back None
    #                           and nothing is stored. So a vector in hand
    #                           means the whole sized text reached the runner
    #                           → a PROVEN False.
    #   * otherwise           — the active-slot self-heal goes through
    #                           ``svc.embed_text`` → ``_embed_shrinking_on_
    #                           overflow``, which can shrink invisibly to us.
    #                           Not provable → None → slot stays UNMEASURED,
    #                           so the reader answers UNKNOWN, not False.
    active_text_slot = ""
    try:
        active_text_slot = str(getattr(svc, "text_vector_slot", "") or "")
    except Exception:  # noqa: BLE001 — never break the backfill
        active_text_slot = ""
    verdict = True if truncated else (False if embed_fn is not None else None)
    trunc_patch = merge_slot_truncation(
        existing_props, slot, verdict, active_text_slot
    )

    # Schedule the store-back fire-and-forget. If there is no running event loop
    # (CLI / sync test context) we skip the store entirely rather than block —
    # the vector is still returned for this request's immediate use.
    if collection is not None and obj_uuid is not None:
        # Per-object-slot dedup: only schedule the write ONCE per process for a
        # given (collection, uuid, slot). The store is idempotent, so re-firing
        # it on every subsequent search is pure I/O waste (Candidate-B write
        # storm). The collection identity is part of the key because a UUID is
        # unique only WITHIN a collection — see the _stored_slots definition
        # comment. The freshly-computed vector is still RETURNED above for this
        # request.
        coll_id = getattr(collection, "name", "") or ""
        dedup_key = (coll_id, str(obj_uuid), slot)
        if dedup_key in _stored_slots:
            return vec
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            if len(_stored_slots) >= _STORE_DEDUP_MAX:
                _stored_slots.clear()  # bounded; a cleared key re-stores once (still idempotent)
            _stored_slots.add(dedup_key)
            task = loop.create_task(
                _store_slot_vector(collection, obj_uuid, slot, vec, trunc_patch)
            )
            _store_back_tasks.add(task)
            task.add_done_callback(_store_back_tasks.discard)
    return vec


async def _store_slot_vector(
    collection: Any,
    obj_uuid: Any,
    slot: str,
    vec: "list[float]",
    properties: "Optional[dict]" = None,
) -> None:
    """Fire-and-forget Weaviate single-slot patch. Soft-fail, off the hot-path.

    The actual ``col.data.update`` is synchronous (Weaviate v4 client); run it in
    a thread so the store never blocks the event loop. A failed store logs at
    debug and is dropped — the in-request vector was already used, so a missed
    persist only means the next search re-backfills (idempotent).
    ``properties`` (W3) is the merged truncation-record patch, or None for a
    vector-only update; Weaviate's ``data.update`` MERGES the given properties
    into the existing object.
    """
    try:
        if properties is not None:
            await asyncio.to_thread(
                collection.data.update,
                uuid=obj_uuid,
                vector={slot: vec},
                properties=properties,
            )
        else:
            await asyncio.to_thread(
                collection.data.update, uuid=obj_uuid, vector={slot: vec}
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("ensure_slot_embedding: store-back failed for %s (%s)", slot, exc)
