# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Shared codegraph property-ensure home (P2d, v0.2.75).

THREE writer families keep the live ``<prefix>_Code*`` Weaviate classes at
the current schema shape, and before P2d each carried its own copy of the
same "add this skip-vectorized property if missing" loop:

  1. ``migrations/codegraph_collection/4_to_5.py`` — embed_revision +
     chunk_num + total_chunks (v0.2.72 P3/P7);
  2. ``migrations/codegraph_collection/5_to_6.py`` — is_test + n_callers
     (v0.2.73 M1/M4);
  3. ``templates/scripts/analyze_code_graph.py`` — the ``_ensure_*_property``
     belt-and-suspenders helpers that fire on every analyze run (covering
     Weaviate-down / standalone-analyzer windows the edges can't).

This module is the ONE home for the property SPECS and the ensure loop:

  * The migration edges import :func:`ensure_codegraph_properties` (with a
    MINIMAL inline fallback for torn checkouts — see each edge's
    ``_FALLBACK_SPECS``, marked MUST-MATCH).
  * The analyzer template keeps its inline ``_ensure_*`` helpers (it must
    run standalone at user sites), but
    ``tests/test_codegraph_schema_parity.py`` asserts its ensured
    ``(class, prop, type)`` set is EQUAL to :data:`CODEGRAPH_PROPERTY_SPECS`
    — the test is the lock.

NEW PROPS: add them HERE first, then mirror into the analyzer's
``_ensure_*`` helper and the new migration edge (the parity test fails
until all three agree). Adding a property is a non-destructive metadata
operation (no re-index, no data loss); every prop here is
``skip_vectorization=True`` by contract — these are metadata, never
embedding inputs.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Tuple

# ---------------------------------------------------------------------------
# Canonical property descriptions (shared by the migration edges; the
# analyzer's inline copies may phrase slightly differently — parity is
# locked on (class, prop, type), not on description text).
# ---------------------------------------------------------------------------

_EMBED_REVISION_DESC = (
    "Embedding-generation revision this row's vector(s) were produced under "
    "(P7 revision-gated forced resync; see CODEGRAPH_EMBED_REVISION)"
)
_CHUNK_NUM_DESC = "0-indexed chunk number within this entity (0 for single-chunk)"
_TOTAL_CHUNKS_DESC = "Total chunk count for this entity (1 for single-chunk)"
_IS_TEST_DESC = (
    "True when the source file is a test/spec/fixture "
    "(path heuristic; retrieval downweight)"
)
_N_CALLERS_DESC = (
    "Inbound call count (Python-resolved, project-internal; "
    "render-time context)"
)

#: One spec row: (property name, Weaviate DataType member name, description).
PropSpec = Tuple[str, str, str]

#: Class-name suffix → the ensured property specs for that class.
#:
#: Scope: EXACTLY the properties the migration edges ensure (4_to_5 +
#: 5_to_6). Older additive props (import_names, language, file_path,
#: content_hash) predate the migration-edge system and remain
#: analyzer-only — do NOT fold them in here without also writing an edge.
#:
#: Ordering is meaningful only for log-line stability (Module → Class →
#: Function → API → Interaction, matching the pre-P2d edge loops).
CODEGRAPH_PROPERTY_SPECS: Dict[str, Tuple[PropSpec, ...]] = {
    "CodeModule": (
        ("embed_revision", "INT", _EMBED_REVISION_DESC),
        ("is_test", "BOOL", _IS_TEST_DESC),
    ),
    "CodeClass": (
        ("embed_revision", "INT", _EMBED_REVISION_DESC),
        ("chunk_num", "INT", _CHUNK_NUM_DESC),
        ("total_chunks", "INT", _TOTAL_CHUNKS_DESC),
        ("is_test", "BOOL", _IS_TEST_DESC),
    ),
    "CodeFunction": (
        ("embed_revision", "INT", _EMBED_REVISION_DESC),
        ("chunk_num", "INT", _CHUNK_NUM_DESC),
        ("total_chunks", "INT", _TOTAL_CHUNKS_DESC),
        ("is_test", "BOOL", _IS_TEST_DESC),
        ("n_callers", "INT", _N_CALLERS_DESC),
    ),
    "CodeAPI": (
        ("embed_revision", "INT", _EMBED_REVISION_DESC),
    ),
    "CodeInteraction": (
        ("embed_revision", "INT", _EMBED_REVISION_DESC),
    ),
}


def specs_subset(props: Iterable[str]) -> Dict[str, Tuple[PropSpec, ...]]:
    """Return the class→specs table filtered to the given property names,
    omitting classes whose filtered spec list is empty.

    The migration edges each own a props subset (4_to_5: embed_revision +
    chunk props; 5_to_6: is_test + n_callers); their inline fallbacks are
    parity-tested against this projection.
    """
    wanted = set(props)
    out: Dict[str, Tuple[PropSpec, ...]] = {}
    for suffix, specs in CODEGRAPH_PROPERTY_SPECS.items():
        filtered = tuple(s for s in specs if s[0] in wanted)
        if filtered:
            out[suffix] = filtered
    return out


class CodegraphPropertyEnsureError(RuntimeError):
    """A genuine ``add_property`` (or config read) failure on one class.

    Carries ``class_name`` so callers (the migration edges) can keep their
    "add_property failed on <class>" log-line shape; ``__cause__`` is the
    underlying exception.
    """

    def __init__(self, class_name: str, message: str) -> None:
        super().__init__(message)
        self.class_name = class_name


def ensure_codegraph_properties(
    client,
    prefix: str,
    props_subset: Optional[Iterable[str]] = None,
) -> Dict[str, str]:
    """Idempotent ensure-if-missing loop over the spec'd codegraph classes.

    For every class suffix in :data:`CODEGRAPH_PROPERTY_SPECS` (restricted
    to ``props_subset`` property names when given):

      * class absent in Weaviate → skipped (it will be born current-shaped
        by ``analyze_code_graph.create_collections``); result ``"absent"``.
      * class present → each missing property is added as a
        ``skip_vectorization`` prop of the spec'd type (an already-present
        property is skipped silently — trivially safe to repeat); result
        ``"ensured"``.

    Returns ``{full_class_name: "absent" | "ensured"}`` in spec order.
    Classes with no spec'd props after the subset filter are omitted
    entirely (never probed — mirrors the pre-P2d 5_to_6 edge, which never
    touched CodeAPI/CodeInteraction).

    Raises :class:`CodegraphPropertyEnsureError` on the FIRST genuine
    failure (config read or ``add_property``) — the caller decides exit
    semantics (the migration runner keys the version advance on exit 0, so
    the edges convert this into a non-zero exit → defer + retry).
    """
    from weaviate.classes.config import DataType, Property  # local: only when running

    wanted = set(props_subset) if props_subset is not None else None
    results: Dict[str, str] = {}
    for suffix, specs in CODEGRAPH_PROPERTY_SPECS.items():
        selected = (
            specs if wanted is None else tuple(s for s in specs if s[0] in wanted)
        )
        if not selected:
            continue
        class_name = f"{prefix}_{suffix}"
        try:
            if not client.collections.exists(class_name):
                results[class_name] = "absent"
                continue
            coll = client.collections.get(class_name)
            existing = {p.name for p in coll.config.get().properties}
            for prop_name, dtype_name, desc in selected:
                if prop_name in existing:
                    continue
                coll.config.add_property(
                    Property(
                        name=prop_name,
                        data_type=getattr(DataType, dtype_name),
                        description=desc,
                        skip_vectorization=True,
                    )
                )
            results[class_name] = "ensured"
        except Exception as exc:
            # A half-applied ensure is a real failure — surface the class so
            # the edge's log line names it and the runner does NOT advance.
            raise CodegraphPropertyEnsureError(class_name, str(exc)) from exc
    return results
