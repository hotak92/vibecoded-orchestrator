# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``weaviate_vectors`` — the ONE home for named-vector round-trip cleaning.

WHY THIS EXISTS (v0.2.82 L4 consolidation)
------------------------------------------
Two independent copiers fetch a Weaviate object WITH its vector and write that
vector back under a new UUID/collection:

  * :mod:`vco_lib.project_init` (``_copy_collection_with_vectors``) — the KG /
    Development collection schema-migration copier.
  * :mod:`vco_lib.codegraph_vector_copy` — the code-graph project-identity
    row-copy migration.

Both hit the SAME weaviate footgun: a named-vector collection that has a slot
CONFIGURED but never POPULATED for a given object round-trips that slot as
``{slot: []}`` on ``include_vector=True``. Passing ``[]`` straight back to
``insert`` / ``replace`` / ``batch.add_object`` raises
``WeaviateInvalidInputError('Invalid vectors: [].')`` and fails the whole copy
on any mixed-slot install (e.g. a collection with both ``codesage_embed`` and
``openai_embed`` where only the first was ever written).

``project_init`` documented + inline-fixed this exact rule with a dict-comp
(``{k: v for k, v in vec.items() if v}``). ``codegraph_vector_copy`` needed the
identical fix. Per the project's "extract before you duplicate" rule, the rule
lives HERE once and BOTH call sites route through it — no second inline copy.

LOUD-FAIL IMPORT
----------------
This module is pure-stdlib (no weaviate import needed — it only shapes a value
weaviate handed us), so it always imports cleanly on a healthy install. Its
CONSUMERS are the ones that must loud-fail if THIS import breaks; they import it
at module top with no fallback (a failing ``from vco_lib.weaviate_vectors import
clean_named_vector`` means a broken install — surface it, never inline-degrade).
"""
from __future__ import annotations

from typing import Any


def clean_named_vector(vec: Any) -> Any:
    """Drop empty/falsy named-vector slots so a round-tripped vector is writable.

    A named-vector collection returns ``obj.vector`` as a dict
    ``{slot: [floats]}``. When a slot is CONFIGURED on the schema but was never
    POPULATED for this object, weaviate's ``include_vector=True`` returns it as
    ``{slot: []}``. Passing that ``[]`` back to ``insert`` / ``replace`` /
    ``batch.add_object`` raises ``WeaviateInvalidInputError('Invalid vectors:
    [].')`` — so we strip empty slots before the write. The destination's
    missing slots simply stay empty (same observable state as the source).

    Shape handling (MUST MATCH the rule documented at
    ``vco_lib/project_init.py`` ``_copy_collection_with_vectors``):
      * ``dict`` (named-vector) → a NEW dict with only the truthy-valued slots.
        An all-empty dict becomes ``{}`` (weaviate accepts an empty dict as
        "no vector supplied"; it rejects only an empty LIST value inside a slot).
      * ``list`` (legacy single-vector) → returned unchanged. A single-vector
        collection has no per-slot ``[]`` footgun; the caller decides whether a
        legacy single vector is even copyable (project_init routes those to a
        rebuild, codegraph_vector_copy carries them verbatim).
      * ``None`` / anything else → returned unchanged (the caller's own
        vector-presence gate decides what an absent vector means).
    """
    if isinstance(vec, dict):
        return {k: v for k, v in vec.items() if v}
    return vec
