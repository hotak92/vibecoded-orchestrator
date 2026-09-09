# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The KG named-vector slot — ONE home for "which vector do I read/write?".

WHY THIS MODULE (v0.2.94, field defect): every VCO knowledge collection is
created with MULTIPLE named vectors (``qwen3_embed`` + ``arctic2_embed`` +
``openai_text_embed`` + the legacy ``ollama_embed`` / ``openai_embed``; see
``vco_lib.project_init.named_vector_config``). Weaviate refuses ANY vector
query against such a collection unless the caller names the slot::

    extract target vectors: class VCODev_KnowledgeGraph has multiple
    vectors, but no target vectors were provided

The shipped duplicate scanner (``templates/scripts/detect_duplicates.py``)
issued ``near_object`` WITHOUT that kwarg, so it had produced no verdict on
every install since named vectors shipped — its own honest "Scan did NOT
complete" line was the only trace. The MCP (``server.py``) and the KG search
CLI (``search_knowledge.py``) each already answered the same question with
their own inline copy; that divergence is what let one consumer miss it.

So the question has ONE home now, in three layers:

* :func:`slot_for_service` — PURE read of an already-constructed
  ``EmbeddingService``'s active text slot. No construction, no probe, no
  env access: safe on hot write paths (``weaviate_mcp.embeddings``) and for
  callers holding the authoritative object (``kg-sync``).
* :func:`active_text_vector_slot` — the slot for the ACTIVE text model,
  derived from env / launcher.db WITHOUT constructing a service (so a
  read-only tool never has to probe Ollama just to name a slot). Delegates
  to ``embedding_service.resolve_active_text_model_id`` + the ``TEXT_SLOT_MAP``
  so it cannot disagree with what ``kg-sync`` actually WROTE.
* :func:`kg_query_target_vector` — the query-side decision for a specific
  collection: the ``target_vector`` value to pass, or ``""`` meaning OMIT the
  kwarg (a pre-named-vector collection has ONE unnamed vector and rejects
  the kwarg). :func:`collection_vector_slots` is the schema probe behind it.

Read/write symmetry is the point: ``kg-sync`` writes
``active_text_vector_slot(service)`` and every reader targets the same value,
so a scan can never query a slot the sync never populated.
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "slot_for_service",
    "active_text_vector_slot",
    "collection_vector_slots",
    "kg_query_target_vector",
]


def slot_for_service(service: Any, default: str = "") -> str:
    """Return *service*'s active text slot, or *default*.

    PURE: never constructs an ``EmbeddingService``, never touches the
    environment, never raises. That is deliberate — the KG write path calls
    this per chunk and a probe/retry side effect there would change state
    mid-write (see ``weaviate_mcp.embeddings._active_text_slot_name``).

    Args:
        service: An ``EmbeddingService`` (or any object exposing
            ``text_vector_slot``). ``None`` is allowed and yields *default*.
        default: Returned when *service* is ``None``, lacks the attribute, or
            carries an empty slot.
    """
    if service is None:
        return default
    try:
        return str(getattr(service, "text_vector_slot", "") or "") or default
    except Exception:  # noqa: BLE001 — defensive: never break a write path
        return default


def active_text_vector_slot(service: Any = None, *, default: str = "") -> str:
    """The named-vector slot the ACTIVE text embedding model reads/writes.

    Resolution order:

      1. *service* — when a live ``EmbeddingService`` is in hand it is
         authoritative: it is the object that will produce the vectors.
      2. Env / launcher.db, via ``embedding_service.resolve_active_text_model_id``
         (``EMBEDDING_MODEL`` → ``ACTIVE_EMBEDDING`` → launcher.db
         ``app_state[embedding.active_profile]`` → ``qwen3``) mapped through
         ``TEXT_SLOT_MAP``. Network-free: no backend is probed, so a read-only
         tool can name the slot with Ollama down.
      3. *default* — only when step 2 resolves to an empty slot name.

    ``ImportError`` from ``vco_lib.embedding_service`` is NOT swallowed: a
    vco_lib that cannot import its own sibling is a broken install, and the
    standing rule is to surface that, never to guess a slot name.
    """
    slot = slot_for_service(service)
    if slot:
        return slot

    # Imported lazily: `slot_for_service` callers (the hot write path) must
    # not pay for the embedding-service import, and this branch is the only
    # one that needs the model→slot table.
    from vco_lib.embedding_service import (  # noqa: PLC0415 - lazy: see the comment above
        _resolve_text_slot,
        resolve_active_text_model_id,
    )

    resolved, _dim = _resolve_text_slot(resolve_active_text_model_id())
    return resolved or default


def collection_vector_slots(collection: Any) -> "tuple[str, ...] | None":
    """Named-vector slot names configured on a Weaviate collection.

    Returns:
        * ``()`` — the collection reports NO named vectors. In weaviate-client
          4.x that is what a pre-named-vector class looks like:
          ``CollectionConfig.vector_config`` is ``None`` (verified live against
          a legacy ``*_KnowledgeGraph`` still on this machine). Such a class has
          ONE UNNAMED vector, and ``target_vector`` must be OMITTED for it —
          passing any name is an error.
        * a non-empty tuple — the configured named slots, in the order the
          client reports them.
        * ``None`` — **undeterminable**: the schema call raised, or the client
          exposed ``vector_config`` in a shape this function cannot read.
          NOT "assume named vectors" — see :func:`kg_query_target_vector`,
          which turns it into a loud failure rather than a guess.
    """
    try:
        config = collection.config.get()
    except Exception:  # noqa: BLE001 — old client / double / transport error
        return None

    if not hasattr(config, "vector_config"):
        # The attribute the whole decision rests on is absent: we cannot tell a
        # legacy single-vector class from a multi-vector one, and the two need
        # OPPOSITE kwargs. Undeterminable, not empty.
        return None
    vector_config = config.vector_config
    if not vector_config:
        return ()

    try:
        names = (
            list(vector_config.keys())
            if hasattr(vector_config, "keys")
            else list(vector_config)
        )
    except Exception:  # noqa: BLE001 - any client/schema shape error means "unreadable"; the caller FAILS the scan on None
        return None
    return tuple(str(name) for name in names)


def kg_query_target_vector(
    collection: Any,
    *,
    service: Any = None,
    default_slot: str = "",
) -> "str | None":
    """The ``target_vector`` value for a vector query on *collection*.

    Returns:
        * a slot name — pass it as ``target_vector``;
        * ``""`` — **omit the kwarg** (never pass ``target_vector=""``);
        * ``None`` — **the schema could not be read, so there is no answer.**
          The caller must FAIL the operation, loudly. Both other results are
          decisions; this one is the absence of one.

    Decision table:

    ==========================  ============================================
    collection schema           result
    ==========================  ============================================
    one UNNAMED vector          ``""`` (omit — legacy collections reject it)
    exactly one NAMED vector    that name (unambiguous, and naming it keeps
                                the choice visible to the operator)
    several NAMED vectors       the ACTIVE slot
    undeterminable              ``None`` — refuse
    ==========================  ============================================

    v0.2.94 review item 4: the undeterminable row used to return the ACTIVE
    slot, on the reasoning that every VCO-created collection since v0.2.18 has
    named vectors. That is a GUESS about the very thing the probe failed to
    establish, and it guesses in the dangerous direction: on a legacy
    single-unnamed-vector class the named kwarg is an ERROR, so the scan dies
    with a message about a slot instead of about the schema read that failed.
    Cannot confirm → do nothing (the standing conservative-default rule).

    When the ACTIVE slot IS confirmed absent from a multi-vector collection it
    is still returned: Weaviate then fails loudly naming that slot, which is
    the honest report ("this collection was never embedded with your active
    model") — that is a read that SUCCEEDED and disagreed, not one that failed.
    """
    slots = collection_vector_slots(collection)
    if slots is None:
        return None

    active = active_text_vector_slot(service, default=default_slot)
    if not slots:
        return ""
    if len(slots) == 1:
        return slots[0]
    return active or slots[0]
