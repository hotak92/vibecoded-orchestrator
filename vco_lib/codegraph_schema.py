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
  3. ``migrations/codegraph_collection/7_to_8.py`` — ``project`` on
     CodeInteraction + its backfill (v0.2.102 S16-4);
     ``migrations/codegraph_collection/8_to_9.py`` — ``is_test`` on CodeAPI
     + its backfill from the stored ``file_path`` (v0.2.102 W6-O);
  4. ``templates/scripts/analyze_code_graph.py`` — the ``_ensure_*_property``
     belt-and-suspenders helpers that fire on every analyze run (covering
     Weaviate-down windows the edges can't).

This module is the ONE home for the property SPECS and the ensure loop:

  * The migration edges import :func:`ensure_codegraph_properties` DIRECTLY
    (X-1 / v0.2.76, ruling #1: the previous inline fallback copies were
    removed — a missing ``vco_lib`` is a broken install that fails loudly, not
    a silent inline-copy degrade).
  * The analyzer template's ``_ensure_*`` helpers are thin delegators to
    :func:`ensure_analyzer_properties` (v0.2.102 — the inline copies were a
    second home for the same loop; the analyzer already imports ``vco_lib``
    everywhere and a failing import is a broken install).
    ``tests/test_codegraph_schema_parity.py`` asserts the helpers jointly
    ensure EXACTLY :data:`CODEGRAPH_PROPERTY_SPECS` — the test is the lock.

NEW PROPS: add them HERE first, then mirror into the analyzer's
``_ensure_*`` helper and the new migration edge (the parity test fails
until all three agree). Adding a property is a non-destructive metadata
operation (no re-index, no data loss); every prop here is
``skip_vectorization=True`` by contract — these are metadata, never
embedding inputs.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

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
_PROJECT_DESC = "Project name"

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
        # v0.2.102 W6-O (edge 8_to_9): test-fixture routes (19 of 22 VCO rows)
        # were indistinguishable from production endpoints.
        ("is_test", "BOOL", _IS_TEST_DESC),
    ),
    "CodeInteraction": (
        ("embed_revision", "INT", _EMBED_REVISION_DESC),
        # v0.2.102 (S16-4/S16-5, edge 7_to_8): the other four classes were born
        # with `project`; CodeInteraction only ever carried `source_project`.
        # Every `project`-scoped reader (the identity sweep, the stale prune,
        # the deleted-file prune, search_code_graph's self filter) failed or
        # silently skipped this class. Same name/type/tokenization as the
        # other four classes' creation-time property.
        ("project", "TEXT", _PROJECT_DESC),
    ),
}

#: Class suffix → the analyzer attribute holding that collection handle (the
#: analyzer's belt-and-suspenders ensure path, :func:`ensure_analyzer_properties`).
ANALYZER_COLLECTION_ATTRS: Dict[str, str] = {
    "CodeModule": "modules_collection",
    "CodeClass": "classes_collection",
    "CodeFunction": "functions_collection",
    "CodeAPI": "apis_collection",
    "CodeInteraction": "interactions_collection",
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


def ensure_analyzer_properties(analyzer: Any, props: Iterable[str]) -> List[str]:
    """The analyzer's belt-and-suspenders ensure: add the spec'd ``props`` to
    whichever of its collection handles lack them. Returns the
    ``"<suffix>.<prop>"`` adds that happened this call.

    Differs from :func:`ensure_codegraph_properties` only in failure posture,
    which is the analyzer's contract since v0.2.61: SOFT-FAIL per (collection,
    property) with a debug log — an analyze run must never wedge on a 422 here
    (the readers treat an absent property conservatively), whereas a migration
    edge must fail loudly so the runner does not advance its version. A missing
    handle attribute (``None`` / absent on a stub) is skipped.
    """
    from weaviate.classes.config import DataType, Property  # local: only when running

    added: List[str] = []
    for suffix, specs in specs_subset(props).items():
        coll = getattr(analyzer, ANALYZER_COLLECTION_ATTRS[suffix], None)
        if coll is None:
            continue
        try:
            existing = {p.name for p in coll.config.get().properties}
        except Exception as exc:  # noqa: BLE001 — soft-fail (see docstring)
            logger.debug("codegraph ensure: config read on %s skipped: %s", suffix, exc)
            continue
        for prop_name, dtype_name, desc in specs:
            if prop_name in existing:
                continue
            try:
                coll.config.add_property(
                    Property(
                        name=prop_name,
                        data_type=getattr(DataType, dtype_name),
                        description=desc,
                        skip_vectorization=True,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — soft-fail (see docstring)
                logger.debug(
                    "codegraph ensure: %s on %s skipped: %s", prop_name, suffix, exc
                )
                continue
            added.append(f"{suffix}.{prop_name}")
            print(f"   Added {prop_name} property to {suffix} schema")
    return added


#: The file-anchored classes that carry ``file_path`` (born analyzer-side in
#: v0.2.52; shared here in v0.2.103 so the resync metadata backfill can ensure
#: it BEFORE its first run — see :func:`ensure_codegraph_file_path`).
CODEGRAPH_FILE_PATH_CLASSES: Tuple[str, ...] = (
    "CodeFunction", "CodeClass", "CodeAPI", "CodeInteraction",
)

_FILE_PATH_DESC = (
    "Repo-relative POSIX path of the source file (mirrors CodeModule.path)"
)


def ensure_codegraph_file_path(client: Any, prefix: str) -> Dict[str, str]:
    """Ensure the ``file_path`` property exists on the file-anchored classes.

    v0.2.103 (defect 6): the resync spawns the metadata backfill as a detached
    child that races the analyzer walk, and the walk's analyzer is what used to
    be the ONLY ensure home for ``file_path`` — so the FIRST backfill after the
    property shipped always probed/patched a class that lacked it (WARNING,
    nothing updated). This is the client+prefix twin of the analyzer's
    ``_ensure_file_path_property`` (same spec, same soft-fail posture: a config
    read or ``add_property`` failure on one class logs and continues — the
    backfill halves already soft-degrade per collection).

    ``file_path`` deliberately stays OUT of :data:`CODEGRAPH_PROPERTY_SPECS`:
    that table is scoped to properties with an OWNING migration edge (the
    parity lock asserts the analyzer's edge-scoped helpers equal it exactly),
    and ``file_path`` predates the edge system (analyzer-born, v0.2.52). The
    spec lives here so there is still ONE home for the ensure loop itself.

    Returns ``{full_class_name: "absent" | "ensured" | "error"}``.
    """
    from weaviate.classes.config import DataType, Property  # local: only when running

    results: Dict[str, str] = {}
    for suffix in CODEGRAPH_FILE_PATH_CLASSES:
        class_name = f"{prefix}_{suffix}"
        try:
            if not client.collections.exists(class_name):
                results[class_name] = "absent"
                continue
            coll = client.collections.get(class_name)
            existing = {p.name for p in coll.config.get().properties}
            if "file_path" not in existing:
                coll.config.add_property(
                    Property(
                        name="file_path",
                        data_type=DataType.TEXT,
                        description=_FILE_PATH_DESC,
                        skip_vectorization=True,
                    )
                )
                logger.info(
                    "codegraph ensure: added file_path to %s", class_name,
                )
            results[class_name] = "ensured"
        except Exception as exc:  # noqa: BLE001 — soft-fail per class
            results[class_name] = "error"
            logger.warning(
                "codegraph ensure: file_path on %s skipped: %s", class_name, exc
            )
    return results


def backfill_missing_property(coll: Any, prop: str, value: str) -> Tuple[int, int]:
    """Set ``prop = value`` on every row of ``coll`` where it is NULL / empty.
    Returns ``(updated, failures)``.

    The write side of an additive property that carries a value (v0.2.102:
    ``project`` on CodeInteraction, filled from the collection's canonical
    identity — a per-project-prefix class holds only that project's rows).

    * ONLY rows lacking the value are touched — a populated row is never
      rewritten, whatever it holds (a stale identity is the identity sweep's
      to migrate, not this function's to overwrite).
    * Resumable: each row is one independent ``data.update`` (a PATCH of that
      one property — no vector, no other property touched), so an interrupted
      run leaves a clean prefix done and the next run continues with the rest.
    * Soft-fail: a per-row update error is counted and the scan continues; an
      unreadable class (config / iterator error) is ONE failure. The caller
      decides what a failure means (an edge exits non-zero so the runner
      retries; the analyzer logs and lets the next run retry).
    * A class without the property, or an empty ``value``, backfills nothing.
    """
    if not value:
        return 0, 0
    try:
        if prop not in {p.name for p in coll.config.get().properties}:
            return 0, 0
    except Exception as exc:  # noqa: BLE001 — unreadable class → one failure
        logger.warning("backfill %s: config read on %s failed: %s",
                       prop, getattr(coll, "name", "?"), exc)
        return 0, 1
    updated = failures = 0
    try:
        for obj in coll.iterator(return_properties=[prop]):
            if (getattr(obj, "properties", None) or {}).get(prop) not in (None, ""):
                continue
            try:
                coll.data.update(uuid=obj.uuid, properties={prop: value})
                updated += 1
            except Exception as exc:  # noqa: BLE001 — per-row soft-fail
                failures += 1
                logger.warning("backfill %s: update %s in %s failed: %s",
                               prop, obj.uuid, getattr(coll, "name", "?"), exc)
    except Exception as exc:  # noqa: BLE001 — enumerate failure → one failure
        logger.warning("backfill %s: enumerate %s failed: %s",
                       prop, getattr(coll, "name", "?"), exc)
        failures += 1
    return updated, failures


def ensure_interaction_project(analyzer: Any) -> None:
    """v0.2.102 (S16-4), extracted from the analyzer in v0.2.105 (P2f
    ratchet): ensure ``project`` on CodeInteraction; when THIS call adds it,
    backfill rows lacking it (resumable, soft-fail; the 7_to_8 edge too)."""
    if "CodeInteraction.project" not in ensure_analyzer_properties(analyzer, ("project",)):
        return
    done, failed = backfill_missing_property(
        analyzer.interactions_collection, "project",
        getattr(analyzer, "project_name", "") or "")
    print(f"   Backfilled project on {done} CodeInteraction row(s)"
          + (f" ({failed} failed — the 7_to_8 edge finishes them on the next update)"
             if failed else ""))
