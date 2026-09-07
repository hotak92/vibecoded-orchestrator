# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Per-slot truncation tags for KG chunk rows — the ONE writer-side home (W3).

WHY THIS MODULE (v0.2.92 wiring audit, MAJOR-W3): the per-call truncation
record captured atomically by
``vco_lib.embedding_service.EmbeddingService.embed_text_all_configured_tagged``
reached Weaviate from ONE of the four writers that store KG slot vectors —
the MCP ``store_knowledge_node`` MULTI-chunk branch — so the partition
capability the release credited ("which stored vectors are leading
windows?") did not exist for kg-sync-written corpora, which is nearly all of
them. The READER already has one home
(``weaviate_mcp.rl_enrichment._stored_slot_truncation_state``, tri-state);
the WRITER now has one too, here. It lives in ``vco_lib`` — not beside the
reader — because ``vco_lib.embedding_enrichment`` (the launcher's enrich-slot
writer) cannot import ``weaviate_mcp``, while every other writer can import
``vco_lib``. ``rl_enrichment`` imports the property-name constants from
here so there is exactly one definition of each name.

Two operations, one semantics each:

* :func:`truncation_tag_properties` — the FULL-WRITE stamper. For every
  writer that stores a complete set of slot vectors from ONE atomic capture
  (kg-sync ``_build_vector_arg``, MCP ``store_knowledge_node`` single- AND
  multi-chunk). Derives BOTH stored properties from the one truncated-slot
  list:

  - ``truncated_slots`` — the COMPLETE record (every configured slot, the
    ACTIVE one included, whose stored vector was embedded from a bounded
    leading sub-window). Its PRESENCE is the era marker: a row that carries
    it can answer for every slot; a row that predates it resolves UNKNOWN
    for the active slot (never a False coerced out of the legacy property).
  - ``secondary_truncated_slots`` — the SAME list with the active slot
    DROPPED, byte-identical to the property's frozen pre-v0.2.92 meaning
    (it never records active-slot state).
  - ``truncation_measured_slots`` — the slots the write actually MEASURED,
    i.e. the ones this row can answer for. See "Absence is not False".

  A ``None`` capture (the writer's atomic gather did not produce the
  vector it is storing — an untagged fallback embed did) derives NOTHING:
  the stamper returns ``{}`` so the row carries no record and resolves
  UNKNOWN, rather than an empty record that would claim full fidelity
  for a vector nobody measured.

* :func:`merge_slot_truncation` — the SINGLE-SLOT PATCH merger, for the
  writers that fill ONE slot on an EXISTING row (``vco_lib.embedding_
  enrichment``'s batch flush; the dual-log backfill store-back in
  ``rl_client.embed_regen``). It records a verdict ONLY when the caller can
  PROVE one; an unprovable verdict (``truncated=None``) patches nothing, so
  the slot stays out of ``truncation_measured_slots`` and reads as UNKNOWN.

Absence is not False — why ``truncation_measured_slots`` exists
---------------------------------------------------------------
A full write measures every slot it stores, so for THOSE slots "not in
``truncated_slots``" is a real ``False``. A single-slot patch then adds a
vector for a slot that write never looked at. Without a record of WHICH
slots were measured, the reader's ``slot in truncated_slots`` would answer
``False`` for that freshly-patched slot — a confident wrong answer fed
straight into a training pipeline, which is the precise failure the
tri-state was built to avoid.

So the answerable set is stored explicitly:

* a FULL write stamps ``truncation_measured_slots`` = the slots it stored;
* a PATCH adds its slot to that list ONLY together with a proven verdict;
* the reader treats a slot absent from the list as UNKNOWN.

The residual honest gap this leaves is UNDER-reporting, never
over-claiming: e.g. the enrichment batch flush can prove a leading window
from the shared ``_bounded_for_model`` pre-bound, but cannot see the
per-item shrink that happens after a whole-batch refusal — so it passes
``None`` there and the slot reads UNKNOWN instead of a wrong ``False``.

Hard constraints this module enforces (W3 brief):

* **Additive only** — no writer here re-embeds, migrates, or deletes.
* **Old rows resolve UNKNOWN, never False** — a row without a complete
  ``truncated_slots`` record is never given one by a single-slot patch:
  recording one slot's verdict would assert that the rest are known, and
  they are not. Such rows keep NO record → the reader resolves UNKNOWN.
* **The legacy property's meaning is frozen** — always derived as the
  complete list minus the active slot, never widened.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "TRUNCATED_SLOTS_PROP",
    "SECONDARY_TRUNCATED_SLOTS_PROP",
    "MEASURED_SLOTS_PROP",
    "truncation_tag_properties",
    "merge_slot_truncation",
]

#: COMPLETE per-slot truncation record; presence marks a row that can answer
#: for EVERY slot (the ACTIVE slot included). Canonical definition —
#: ``weaviate_mcp.rl_enrichment`` re-exports this name; do not fork it.
TRUNCATED_SLOTS_PROP = "truncated_slots"

#: R3-2 property: SECONDARY slots only. Never answers for the ACTIVE slot.
#: Canonical definition (same re-export rule as above).
SECONDARY_TRUNCATED_SLOTS_PROP = "secondary_truncated_slots"

#: The slots this row's stored truncation record can actually ANSWER for —
#: the ones a writer MEASURED. Membership, not mere presence of the record,
#: is what makes ``slot in truncated_slots`` a real answer: a single-slot
#: patch adds a vector the original full write never measured, so without
#: this list "absent from truncated_slots" would read as a confident False
#: for a slot nobody ever looked at. See the module docstring.
MEASURED_SLOTS_PROP = "truncation_measured_slots"


def _coerce_slot_list(raw: Any) -> "Optional[list[str]]":
    """Normalize a stored truncated-slot list; ``None`` when not one.

    Soft by design: odd-shaped stored values (a string, a number) read as
    "no record" so a malformed row degrades to UNKNOWN rather than raising
    into a write path.
    """
    if isinstance(raw, (list, tuple)):
        return [str(s) for s in raw if s]
    return None


def truncation_tag_properties(
    truncated_slots: "Optional[Iterable[str]]",
    active_text_slot: str,
    measured_slots: "Optional[Iterable[str]]" = None,
) -> "dict[str, list[str]]":
    """Derive BOTH truncation properties from ONE complete capture.

    Args:
        truncated_slots: The COMPLETE per-call record — every configured
            slot (active included) whose vector was embedded from a bounded
            leading sub-window. This is exactly what
            ``embed_text_all_configured_tagged`` returns as its second
            element; callers MUST pass that capture, not the derived
            ``last_secondary_truncated`` / ``last_active_truncated`` views
            (reading those as separate property calls is race-prone).

            ``None`` means the writer has NO honest capture for the vector
            it is about to store (the atomic gather produced no vector for
            the slot and a separate, untagged embed supplied it). The
            stamper then returns ``{}`` — the row is written with NO
            record, so the tri-state reader resolves UNKNOWN. Writing an
            EMPTY record instead would assert full fidelity for a vector
            nobody measured, which is exactly the confident-wrong-answer
            this module exists to prevent.
        active_text_slot: The ACTIVE text slot NAME (e.g. ``"qwen3_embed"``),
            dropped from the legacy secondary-only view. An empty name
            derives a legacy view equal to the complete record — acceptable
            only when the capture's record is empty (a cold service cache
            means the inline fallback ran, whose record IS empty); callers
            with a live capture always have the name.
        measured_slots: The slots this write MEASURED — normally the keys of
            the ``{slot: vector}`` map it is about to store, since the
            capture covers exactly those. Every truncated slot is measured
            by definition, so the stored list is the UNION; a caller that
            genuinely cannot enumerate them passes ``None`` and the row then
            answers only for the slots it names as truncated (the
            pre-measured-set behaviour, kept for that one case).

    Returns:
        A props fragment to ``update()`` into the row's properties dict:
        ``{truncated_slots: […], secondary_truncated_slots: […],
        truncation_measured_slots: […]}``. The lists are freshly-built and
        sorted, so the stored shape is stable regardless of the capture's
        iteration order. An EMPTY dict when ``truncated_slots is None`` —
        ``data_obj.update({})`` is a no-op, so every caller stays a single
        unconditional ``update`` call and the "no honest record" outcome
        cannot be forgotten at one site.
    """
    if truncated_slots is None:
        return {}
    complete = sorted({str(s) for s in truncated_slots if s})
    measured = sorted(
        set(complete) | {str(s) for s in (measured_slots or ()) if s}
    )
    return {
        TRUNCATED_SLOTS_PROP: complete,
        SECONDARY_TRUNCATED_SLOTS_PROP: [
            slot for slot in complete if slot != active_text_slot
        ],
        MEASURED_SLOTS_PROP: measured,
    }


def merge_slot_truncation(
    existing_props: "Optional[Mapping[str, Any]]",
    slot: str,
    truncated: "Optional[bool]",
    active_text_slot: str,
) -> "Optional[dict[str, list[str]]]":
    """Merge ONE freshly-written slot's PROVEN verdict into a stored row.

    For the single-slot patch writers (enrichment flush, dual-log backfill):
    they fill ``slot``'s vector on a row that may already carry a complete
    record from an earlier full write. Recording the verdict is what stops
    the reader answering ``False`` for a slot that write never measured:

    ``truncated = True``  → ``slot`` joins BOTH the truncated record and the
                            measured set.
    ``truncated = False`` → ``slot`` joins ONLY the measured set (a PROVEN
                            full-fidelity answer — pass this only when the
                            caller knows the whole text reached the runner).
    ``truncated = None``  → nothing is provable → returns ``None`` (write the
                            vector alone). ``slot`` stays out of the measured
                            set, so the reader resolves UNKNOWN for it rather
                            than reading its absence from the truncated list
                            as a confident ``False``.

    Because these writers only ever target slots whose vector was EMPTY,
    ``slot`` cannot already be in the stored truncated list — so nothing
    here can ever REMOVE a recorded truncation.

    Args:
        existing_props: The row's CURRENT properties (the caller holds the
            iterated/fetched object). ``None`` or a mapping without a
            complete ``truncated_slots`` list → no honest merge exists
            (see the module docstring's old-rows rule) → returns ``None``.
        slot: The slot whose vector this patch writes.
        truncated: The patch's OWN verdict — see the table above. ``True``
            only when the caller positively knows the embedded text was a
            leading sub-window (its own char cap, or the shared
            ``_bounded_for_model`` bound); ``False`` only when it positively
            knows the FULL text was embedded; ``None`` otherwise.
        active_text_slot: The ACTIVE text slot NAME, used to derive the
            frozen legacy view. Empty → ``None`` (deriving the view without
            knowing the active slot could place the active slot into the
            secondary-only property, silently widening its meaning).

    Returns:
        The props fragment to pass to Weaviate ``data.update(properties=…)``
        alongside the vector, or ``None`` when nothing should be patched
        (row predates the complete record, the active slot is unknown, or
        the verdict is unprovable). ``None`` is the caller's signal to write
        the vector ONLY.
    """
    if not slot or truncated is None:
        return None
    existing = _coerce_slot_list(
        (existing_props or {}).get(TRUNCATED_SLOTS_PROP)
    )
    if existing is None:
        # Pre-record row: a single-slot verdict is un-recordable without
        # asserting unknowns as full-fidelity. The row stays UNKNOWN for
        # every slot (absence), which is exactly what the tri-state reader
        # resolves — never a guessed False.
        return None
    if not active_text_slot:
        # Cannot derive the frozen legacy view without the active slot name.
        return None
    measured = _coerce_slot_list(
        (existing_props or {}).get(MEASURED_SLOTS_PROP)
    )
    if measured is None:
        # Row written before the measured set existed: everything it named
        # as truncated it measured, and (this same release, no shipped rows)
        # so did every slot it stored — but only the truncated ones are
        # provable from the row itself, so seed from those.
        measured = list(existing)
    merged = set(existing) | ({slot} if truncated else set())
    props = truncation_tag_properties(
        merged, active_text_slot, measured_slots=set(measured) | {slot},
    )
    return props
