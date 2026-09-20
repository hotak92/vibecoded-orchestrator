# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 — repair a KG row's stale METADATA without re-embedding it.

The gap this closes
-------------------
``sync_knowledge_graph.py``'s embed-skip gate compares the file's TEXT
signature (``_content_signature_excluding_updated``) against the stored
``content_hash``. It never compares the PARSED properties. So a node stored
before v0.2.95 — when the nested ``metadata:`` dialect was not promoted, a
string ``tags:`` was not split, and ``name:`` was not read as ``title:`` —
keeps its wrong ``tags`` / ``node_type`` / ``title`` in Weaviate for as long
as its text is untouched. ``kg-sync --all`` did not repair it either: the
skip path returned having written nothing, so a bulk run printed its
promotion line and left the stored row exactly as wrong as it found it.

Why a metadata PATCH and not a hash change
------------------------------------------
Salting the content signature for promoted files would make every one of
them re-embed, and the signature has readers that are not this gate:
``install.py``'s CI-10 seed-diff gate and ``vco_lib.kg_sync_drift`` both
RECOMPUTE it from the file with no knowledge of frontmatter dialects, so a
salted stored value would read as permanent drift to both — an update loop
that re-syncs the same nodes forever and a deferral that can never clear.
The shipped-vector sidecar (``knowledge/.node_embeddings.<slot>.json``) and
the curated provenance registry (``templates/knowledge/.curated_hashes.json``,
which gates a DELETION) are keyed on it as well. A hash whose writer and
reader disagree is precisely the 2026-07-20 sidecar incident; this module
exists so that failure class is not re-entered.

When the text is unchanged the stored vector is still valid, so the repair
is a property-only ``data.update`` — no re-chunk, no re-embed, no hash
change, and therefore nothing any of those readers can observe.

Shape
-----
Pure decision here, I/O in the caller — the same split
:mod:`vco_lib.codegraph_guards` uses, and its ``RowAction`` vocabulary is
reused rather than forked: ``SKIP`` = write nothing, ``STAMP`` = the
metadata patch. ``EMBED`` is NEVER returned. The code-graph's own
``classify_row`` / ``skip_or_stamp_all_chunks`` could not be reused
directly: they decide on an ``embed_revision`` integer against a
compatibility floor (the KG has no such property), their patch payload is
that one integer, and a declining STAMP there falls through to re-embedding
every chunk — the one outcome forbidden on this path.

Conservative by construction
----------------------------
Every uncertainty resolves to "write nothing", never to "they differ":
unreadable properties, a property the fetch did not return, a stored value
of an unexpected type, a row without an id, or a desired value that is not
usable. A wrong "they differ" verdict would rewrite every row of a
knowledge graph.

All-or-nothing across a node's chunks: a node's properties live on EVERY
chunk row, so judgeability is established for all rows BEFORE any row is
written (the never-half-stamp discipline
:func:`vco_lib.codegraph_guards.stamp_all_chunks` owns for the code graph).
A patch that fails mid-sequence aborts the remaining patches and is
REPORTED — never swallowed. Rows already patched are left patched: they now
hold the CORRECT value, and the next run retries the rest (the content hash
still matches and the remaining rows still differ), so the repair converges
instead of oscillating.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from vco_lib.codegraph_guards import RowAction

#: Explicit re-export set. ``RowAction`` is listed deliberately: callers of
#: this module decide on ITS vocabulary, and re-exporting it here is what
#: keeps them from importing a code-graph module to read a KG verdict.
__all__ = [
    "REPAIRABLE_PROPERTIES",
    "MetadataRepairPlan",
    "RowAction",
    "apply_metadata_repair",
    "classify_metadata_row",
    "comparable_properties",
    "desired_is_usable",
    "plan_metadata_repair",
    "property_differs",
    "row_unjudgeable_reason",
]

#: The properties this repair owns. The membership rule, so a later editor
#: can extend it correctly: a property belongs here only when it is (a)
#: alterable by ``sync_knowledge_graph.py::_normalise_frontmatter`` — the
#: defect being repaired — and (b) written UNCONDITIONALLY by every KG write
#: path, so "what a full re-sync would store" is always defined, and (c) a
#: pure function of the file's text.
#:
#: Deliberately EXCLUDED, with the reason:
#:   * ``created_at`` / ``updated_at`` — filesystem stat, not text; mtime
#:     churns, so comparing them would write on every run.
#:   * ``created`` / ``updated`` / ``valid_from`` / ``valid_until`` /
#:     ``status`` — written only when the key is present, so "absent" has no
#:     comparable desired value, and the date normalisation on both sides
#:     would make a false difference easy to manufacture.
#:   * ``links`` / ``typed_links`` — pure functions of the BODY, which the
#:     matching content hash already proves unchanged, so they cannot carry
#:     this defect; ``typed_links`` is a nested OBJECT_ARRAY whose read-back
#:     shape can differ from the written shape, which is exactly how a false
#:     "differs" verdict would be manufactured.
#:   * ``content`` / ``content_hash`` / ``chunk_num`` / ``total_chunks`` /
#:     ``source_node_id`` — identity and embedding inputs; patching them is
#:     what a re-embed is for.
REPAIRABLE_PROPERTIES: Tuple[str, ...] = (
    "title",
    "node_type",
    "tags",
    "external_links",
)

#: Properties compared as SETS, not sequences. ``tags`` is assembled with
#: ``list(set(...))`` when link inference contributes (see ``sync_node``), so
#: its ORDER is not stable across runs; an order-sensitive comparison would
#: rewrite those rows on every single sync forever. Order carries no meaning
#: here, so set equality is both cheaper and correct.
_SET_COMPARED: "frozenset[str]" = frozenset({"tags"})

#: Properties that must be a NON-EMPTY string to be usable as a desired
#: value (a node always has a title and a type — the parser falls back to the
#: filename stem / folder name — so an empty one means the parse failed and
#: must never be written over stored data).
_NON_EMPTY_TEXT: "frozenset[str]" = frozenset({"title", "node_type"})


class MetadataRepairPlan:
    """What (if anything) to write, decided without touching the backend.

    ``verdict`` is :attr:`RowAction.STAMP` when at least one row needs a
    property patch, else :attr:`RowAction.SKIP`. ``patches`` is the
    per-row ``(uuid, properties)`` payload list — only the properties that
    actually differ, only on the rows that differ. ``fields`` names every
    property any patch touches (for the run's log line). ``reason``
    explains a SKIP, so a "why did nothing happen" question is answerable
    from the code path alone.
    """

    __slots__ = ("verdict", "patches", "fields", "reason")

    def __init__(
        self,
        verdict: RowAction,
        patches: "Optional[List[Tuple[str, Dict[str, Any]]]]" = None,
        fields: "Sequence[str]" = (),
        reason: str = "",
    ) -> None:
        self.verdict = verdict
        self.patches: "List[Tuple[str, Dict[str, Any]]]" = list(patches or [])
        self.fields: Tuple[str, ...] = tuple(fields)
        self.reason = reason

    @property
    def row_count(self) -> int:
        """How many ROWS the plan would write (0 for a SKIP)."""
        return len(self.patches)

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return (
            f"MetadataRepairPlan({self.verdict!r}, rows={self.row_count}, "
            f"fields={self.fields!r}, reason={self.reason!r})"
        )


def desired_is_usable(name: str, value: Any) -> bool:
    """May *value* be written over stored data for property *name*?

    ``tags`` must be a list of strings (an empty list IS usable — it is what
    v0.2.95 stores for a node that declares frontmatter without tags, and
    writing it is how a prose-scraped ``['4', '12']`` gets repaired).
    ``external_links`` must be a string (``""`` is its canonical "none").
    ``title`` / ``node_type`` must be NON-EMPTY strings.
    """
    if name in _SET_COMPARED:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    if not isinstance(value, str):
        return False
    if name in _NON_EMPTY_TEXT:
        return bool(value.strip())
    return True


def comparable_properties(desired: "Mapping[str, Any]") -> Tuple[str, ...]:
    """The repairable properties whose desired value is usable this run.

    A property whose freshly-parsed value is unusable is dropped from the
    comparison entirely — never compared, never written.
    """
    return tuple(
        name
        for name in REPAIRABLE_PROPERTIES
        if name in desired and desired_is_usable(name, desired[name])
    )


def _normalised_stored(name: str, value: Any) -> "Tuple[bool, Any]":
    """``(judgeable, normalised_value)`` for one stored property value.

    Weaviate returns ``None`` for an unset property; that is a POSITIVE
    fact ("this row carries no value"), so it normalises to the empty value
    of the property's type and stays judgeable. A value of an unexpected
    TYPE is NOT judgeable — a str where a list belongs means the row was
    written by something this comparison does not model.
    """
    if name in _SET_COMPARED:
        if value is None:
            return True, []
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return True, value
        return False, None
    if value is None:
        return True, ""
    if isinstance(value, str):
        return True, value
    return False, None


def row_unjudgeable_reason(props: Any, names: "Sequence[str]") -> Optional[str]:
    """Why this row cannot be judged, or ``None`` when it can.

    "Cannot be judged" is never "differs": the caller turns any reason here
    into a whole-node SKIP.
    """
    if not isinstance(props, Mapping):
        return "row properties unreadable"
    for name in names:
        if name not in props:
            # The fetch did not return it (older client, narrowed
            # return_properties, a double that ignores the argument). Absent
            # is NOT empty.
            return f"stored {name!r} was not returned by the fetch"
        judgeable, _ = _normalised_stored(name, props[name])
        if not judgeable:
            return f"stored {name!r} has an unexpected type"
    return None


def property_differs(name: str, stored: Any, desired: Any) -> bool:
    """Does the stored value differ from the desired one, meaningfully?

    Callers must have established judgeability first
    (:func:`row_unjudgeable_reason`); this function is total on judgeable
    input and never raises on it.
    """
    judgeable, normalised = _normalised_stored(name, stored)
    if not judgeable:
        return False  # unjudgeable never means "differs"
    if name in _SET_COMPARED:
        return set(normalised) != set(desired)
    return normalised != desired


def classify_metadata_row(
    props: "Mapping[str, Any]",
    desired: "Mapping[str, Any]",
    names: "Sequence[str]",
) -> "Tuple[RowAction, Dict[str, Any]]":
    """``(SKIP | STAMP, payload)`` for ONE row. PURE.

    :attr:`RowAction.EMBED` is never returned, by construction: this
    classifier runs only where the content hash already matched, so the
    text is unchanged and the stored vector is valid. Forcing an embed here
    would burn a model call to rewrite bytes that did not change — the
    standing rule this whole path exists to honour.
    """
    payload = {
        name: desired[name]
        for name in names
        if property_differs(name, props.get(name), desired[name])
    }
    return (RowAction.STAMP if payload else RowAction.SKIP), payload


def plan_metadata_repair(
    rows: "Iterable[Tuple[Any, Any]]",
    desired: "Mapping[str, Any]",
) -> MetadataRepairPlan:
    """Decide the whole node's repair from its ``(uuid, properties)`` rows.

    Two passes, and the order is the safety property: judgeability is
    established for EVERY row before ANY row contributes a patch, so a node
    whose chunks are not uniformly readable is left entirely alone rather
    than half-repaired.
    """
    try:
        row_list = list(rows)
    except Exception:  # noqa: BLE001 — unreadable result set → do nothing
        return MetadataRepairPlan(RowAction.SKIP, reason="rows unreadable")

    if not row_list:
        return MetadataRepairPlan(RowAction.SKIP, reason="no stored rows")

    names = comparable_properties(desired)
    if not names:
        return MetadataRepairPlan(
            RowAction.SKIP, reason="no comparable properties this run"
        )

    # Pass 1 — judgeability for every row.
    for row_uuid, props in row_list:
        if not row_uuid:
            return MetadataRepairPlan(RowAction.SKIP, reason="a row has no id")
        reason = row_unjudgeable_reason(props, names)
        if reason:
            return MetadataRepairPlan(RowAction.SKIP, reason=reason)

    # Pass 2 — the diffs.
    patches: "List[Tuple[str, Dict[str, Any]]]" = []
    fields: "set[str]" = set()
    for row_uuid, props in row_list:
        action, payload = classify_metadata_row(props, desired, names)
        if action is RowAction.STAMP:
            patches.append((str(row_uuid), payload))
            fields.update(payload)

    if not patches:
        return MetadataRepairPlan(
            RowAction.SKIP, reason="stored metadata already matches"
        )
    return MetadataRepairPlan(RowAction.STAMP, patches, sorted(fields))


def apply_metadata_repair(
    plan: MetadataRepairPlan,
    patch_row: "Callable[[str, Dict[str, Any]], None]",
) -> "Tuple[int, Optional[str]]":
    """Execute *plan* through the caller's one I/O callable.

    Returns ``(rows_patched, error)``. ``error`` is ``None`` on a complete
    run; otherwise it is the first failure's text and the remaining patches
    are ABANDONED — the caller must report it (a repair that could not
    complete is not a silent one) and the next run retries what is left.
    """
    if plan.verdict is not RowAction.STAMP:
        return 0, None
    done = 0
    for row_uuid, payload in plan.patches:
        try:
            patch_row(row_uuid, payload)
        except Exception as exc:  # noqa: BLE001 — surfaced, never swallowed
            return done, f"{type(exc).__name__}: {exc}"
        done += 1
    return done, None
