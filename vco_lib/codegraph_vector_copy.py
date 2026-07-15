# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``codegraph_vector_copy`` — vector-portable row copy + project-identity
migration for the code graph (v0.2.82 G7 + G3 engine).

WHY THIS EXISTS
---------------
Two writers stamp the code-graph ``project`` property differently: the per-edit
hooks stamp the SANITIZED ``collection_prefix`` while the launcher stamps the
project's DISPLAY NAME. Because the deterministic UUID mixes ``project`` into
its seed (:func:`analyze_code_graph._deterministic_uuid`), a project whose
display name differs from its canonical prefix (any spaced name — "Old Name")
accumulates DUPLICATE rows: the SAME source entity indexed under two different
``project`` seeds mints two different UUIDs. Left alone the two writers keep
minting dupes on every edit + rebuild.

The v0.2.82 G3 decision (option b) is: pick ONE identity SSOT (the codegraph
binding ``collection_prefix``) and, when the display-name identity is detected,
run a ROW-WISE migration that re-mints each row's UUID under the canonical
identity — REUSING the stored vector VERBATIM (no re-embed). This module is that
migration engine plus its low-level primitive.

VECTOR PURITY (the load-bearing invariant)
------------------------------------------
This module NEVER computes an embedding. It is a COPY primitive: it fetches a
row WITH its vector (weaviate-client v4 ``include_vector=True`` → a named-vector
dict), and writes that exact dict back under the new UUID. A re-embed here would
(a) burn compute this release explicitly forbids and (b) risk a silently
different vector (model/tier drift) for an unchanged body. Grep proof: there is
no ``generate_embedding`` / ``embed_`` / EmbeddingService import anywhere below.

CONSERVATIVE DELETES
--------------------
A source row is deleted ONLY after its destination is positively confirmed —
either a fresh copy whose write succeeded AND read back, or a pre-existing
vector-bearing destination (a true duplicate). Any uncertainty (write failed,
read-back missing, vector absent) LEAVES the source in place and counts it as a
failure/left row. Weaviate collections are never dropped.

REUSE, NEVER MIRROR
-------------------
The destination UUID is re-derived with the analyzer's OWN
``_deterministic_uuid`` (imported, loud-fail) — no second copy of the seed
composition lives here. The chunk identity_key rule (chunk 0 = bare key, chunk
i = ``<key>::<i>``) is likewise the analyzer/guards rule, applied via the same
formula the guard's :func:`chunk_identities` uses.
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

# v0.2.82 L4: the ONE home for named-vector round-trip cleaning (dropping
# configured-but-empty ``{slot: []}`` slots weaviate rejects on re-insert).
# LOUD-FAIL import (no fallback): a broken vco_lib install must surface. MUST
# MATCH the sibling call site in vco_lib/project_init.py.
from vco_lib.weaviate_vectors import clean_named_vector

logger = logging.getLogger(__name__)


# ── The 5 code-graph collections + their per-collection identity metadata ────
#
# ``chunkable`` marks the two collections whose over-budget entities fan out into
# N chunk rows (chunk 0 = bare identity_key, chunk i = ``<key>::<i>``). Only
# those two carry ``chunk_num`` / ``total_chunks`` in their schema — asking for
# those return_properties on the other three 500s the read (mirrors the analyzer
# ``_read_props`` defense), so they are read WITHOUT the chunk props.
_CODEGRAPH_BASES: Tuple[str, ...] = (
    "CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction",
)
_CHUNKABLE_BASES: frozenset = frozenset({"CodeClass", "CodeFunction"})

# ── Cross-reference topology (MUST MATCH the analyzer's ReferenceProperty
# blocks in templates/scripts/analyze_code_graph.py) ─────────────────────────
#
# Each entry maps a base → the reference-property NAMES it carries. These edges
# are load-bearing: ``query_code_structure`` reads them (return_references +
# Filter.by_ref) and WP-2's own backfill repairs them. Because the copied row
# keeps its verbatim content_hash + embed_revision, the analyzer SKIPs it on
# every future walk — so if the migration doesn't carry the references, the
# edges are lost FOREVER (never re-created). References cross collections, so
# the UUID-remap map that B3 builds spans ALL FIVE collections.
#
# Reference target collections (for docs; the migration remaps via a GLOBAL
# old→new UUID map so it does not need to know the target base per name):
#   CodeModule       imports          → CodeModule
#   CodeClass        module           → CodeModule
#                    extends          → CodeClass
#   CodeFunction     module           → CodeModule
#                    calls            → CodeFunction
#   CodeAPI          handler          → CodeFunction
#   CodeInteraction  source_function  → CodeFunction
#                    source_module    → CodeModule
_REFERENCE_NAMES: Dict[str, Tuple[str, ...]] = {
    "CodeModule": ("imports",),
    "CodeClass": ("module", "extends"),
    "CodeFunction": ("module", "calls"),
    "CodeAPI": ("handler",),
    "CodeInteraction": ("source_function", "source_module"),
}

# The stored property whose value the deterministic UUID mixes as ``file_path``.
# CodeModule keys the SEED file slot on ``path``; the rest carry ``file_path``.
# (MUST match the analyzer storage shape — the same map resync/prune use.)
_PATH_PROP: Dict[str, str] = {
    "CodeModule": "path",
}


def _load_analyzer_uuid_builder() -> Callable[..., str]:
    """Import ``_deterministic_uuid`` from the analyzer template — LOUD-FAIL.

    The analyzer is a template script, not an importable package (its top-level
    executes heavy code), so we load it by file path via ``importlib`` and pull
    the ONE deterministic-UUID builder out. This is a REUSE (never a mirror) of
    the seed composition ``{project}::{project_source}::{file_path_rel}::
    {full_name}``. A failure here means a broken install — we raise with a clear
    message rather than silently mirroring the seed (the project's loud-fail rule
    for shipped components).
    """
    candidates = [
        Path(__file__).resolve().parent.parent
        / "templates" / "scripts" / "analyze_code_graph.py",
        Path(__file__).resolve().parent.parent
        / ".claude" / "scripts" / "analyze_code_graph.py",
    ]
    analyzer_path = next((c for c in candidates if c.is_file()), None)
    if analyzer_path is None:
        raise RuntimeError(
            "codegraph_vector_copy: cannot locate analyze_code_graph.py to import "
            "_deterministic_uuid (looked under templates/scripts and "
            ".claude/scripts). This is a BROKEN install — the identity-key builder "
            "must be REUSED, never mirrored."
        )
    spec = importlib.util.spec_from_file_location(
        "_vco_vector_copy_analyzer", str(analyzer_path)
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"codegraph_vector_copy: cannot build import spec for {analyzer_path}"
        )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    builder = getattr(mod, "_deterministic_uuid", None)
    if not callable(builder):
        raise RuntimeError(
            f"codegraph_vector_copy: {analyzer_path} exposes no callable "
            "_deterministic_uuid — the analyzer contract changed; refusing to "
            "guess a UUID seed."
        )
    # ``getattr`` types ``builder`` as ``object``; the ``callable`` guard above
    # proved it is a callable — cast to the declared return type so the
    # signature stays honest (type-only, zero behaviour change).
    return cast("Callable[..., str]", builder)


def _posix_normalize(path_value: Any) -> str:
    """``\\``→``/`` normalization for a stored path used in UUID re-derivation.

    Windows-shaped legacy rows may store a backslash ``file_path``; the POSIX
    form is what a Linux/macOS analyzer would have minted the UUID from. The
    migration tries the RAW value first and this normalized form second
    (prefer-exact), so a row minted from either shape is reconstructable.
    """
    if not path_value:
        return ""
    return str(path_value).replace("\\", "/")


# ── identity_key reconstruction, per collection shape ────────────────────────
#
# The identity_key is the 4th UUID seed slot. It is NOT itself a single stored
# property for every collection — it is derived from stored props per shape
# (mirrors CodeEntity.identity_key + the analyzer's pinned ``_identity_key``
# slots). A shape whose identity_key cannot be reconstructed from the row's
# stored props returns ``None`` → the caller LEAVES the row (counted).


def _identity_key_for_row(base: str, props: Dict[str, Any]) -> Optional[str]:
    """Reconstruct a row's dedup identity_key from its stored properties.

    Returns the BARE identity_key (chunk-0 form). For chunkable collections the
    caller derives per-chunk keys (``<key>::<i>``) itself. Returns ``None`` when
    the row's shape makes the key unreconstructable (→ left in place, counted).

    Per-shape rules (MUST MATCH ``codegraph_entities.CodeEntity.identity_key``
    and the analyzer's pinned ``_identity_key`` slots):
      * CodeModule      → ``module::<path>``  (``path`` is stored)
      * CodeClass /
        CodeFunction    → ``full_name`` if set else ``name`` (both stored)
      * CodeAPI         → ``<endpoint>:<method>`` (both stored)
      * CodeInteraction → ``ix::<source>::<endpoint>`` — the ``source`` token is
                          NOT a stored property (it is the interaction's source
                          entity name at extraction time), so this shape is
                          only reconstructable when the row carries an explicit
                          stored ``raw_target``/source hint. Absent → None.
    """
    if base == "CodeModule":
        path = props.get("path")
        if not path:
            return None
        return f"module::{path}"
    if base in ("CodeClass", "CodeFunction"):
        key = props.get("full_name") or props.get("name") or ""
        return key or None
    if base == "CodeAPI":
        endpoint = props.get("endpoint")
        method = props.get("method")
        # endpoint+method are the identity even when one is empty (matches the
        # analyzer's ``f"{endpoint}:{method}"`` which tolerates an empty half);
        # only a wholly-empty pair is unreconstructable.
        if not endpoint and not method:
            return None
        return f"{endpoint or ''}:{method or ''}"
    if base == "CodeInteraction":
        # ``ix::<source>::<endpoint>``. The source token is NEVER a stored
        # property (M1: it's the interaction's source-entity name at extraction
        # time; the schema carries no ``source``/``source_entity`` column — see
        # the analyzer's CodeInteraction Property list). So this shape is always
        # unreconstructable from stored props: we return None (LEFT + counted),
        # never guessing a wrong identity_key that would mint a wrong UUID and
        # silently orphan the row. These rows converge when their file is
        # re-walked under the new identity (D3-shaped).
        return None
    return None


@dataclass
class CopyOutcome:
    """Result of a single :func:`copy_one_row_with_vector` attempt."""

    #: ``"copied"`` — a fresh vector-bearing row was written at ``dest_uuid``.
    #: ``"exists_with_vector"`` — dest already had a vector (a true dup; no
    #:   write performed, caller may delete the source).
    #: ``"replaced_vectorless"`` — dest existed WITHOUT a vector; overwritten.
    #: ``"failed"`` — the write/read-back could not be positively confirmed;
    #:   the source MUST be left in place.
    status: str
    dest_uuid: str = ""
    message: str = ""


def _fetch_with_vector(
    coll, uuid: str, return_properties: Optional[List[str]] = None,
) -> Optional[Any]:
    """Point-read ``uuid`` WITH its vector; ``None`` on absence/error.

    Returns the weaviate ``ObjectSingleReturn`` (``.properties`` dict +
    ``.vector`` named-vector dict) or ``None`` when the object is absent OR the
    read cannot be performed. Soft-fail: any exception → ``None`` (treated as
    "unknown" by callers — never a destructive assumption).
    """
    try:
        query = getattr(coll, "query", None)
        fetch_by_id = getattr(query, "fetch_object_by_id", None) if query else None
        if not callable(fetch_by_id):
            return None
        return fetch_by_id(
            uuid, include_vector=True, return_properties=return_properties,
        )
    except Exception as exc:  # noqa: BLE001 — read failure → unknown
        logger.debug("vector-copy: fetch %s failed: %s", uuid, exc)
        return None


def _vector_is_present(vec: Any) -> bool:
    """True iff a fetched ``.vector`` carries at least one populated slot.

    Named-vector collections return a dict ``{slot: [floats]}``; a legacy single
    vector returns a list. Either non-empty shape counts as present; ``None`` /
    ``{}`` / ``[]`` / a dict of only-empty slots counts as VECTORLESS.
    """
    if not vec:
        return False
    if isinstance(vec, dict):
        return any(bool(v) for v in vec.values())
    # list / other truthy sequence → present.
    try:
        return len(vec) > 0
    except TypeError:
        return bool(vec)


def _query_references_for(base: str):
    """Build the ``return_references`` argument for ``base``'s reference names.

    Uses weaviate v4's ``QueryReference(link_on=<name>)`` so a point-read
    resolves each cross-reference to its target object(s). Returns ``None`` for
    a base with no references (never asks for an empty reference set). Imported
    lazily so the module loads without a live weaviate (mirrors the historic
    ``_iter_project_rows`` local-import contract that tests monkeypatch).
    """
    names = _REFERENCE_NAMES.get(base) or ()
    if not names:
        return None
    from weaviate.classes.query import QueryReference

    return [QueryReference(link_on=n) for n in names]


def _read_reference_targets(src_obj, base: str) -> Dict[str, List[str]]:
    """Extract ``{ref_name: [target_uuid, ...]}`` from a fetched source object.

    A fetched object's ``.references`` is ``{name: _CrossReference}`` where the
    cross-reference exposes ``.objects`` (a list of resolved target objects,
    each carrying ``.uuid``). weaviate only resolves LIVE targets, so a stored
    beacon to a since-deleted object simply does not appear (it is dropped at
    read time — the conservative outcome). Absent/None references → empty dict.
    Soft-fail: any shape surprise yields the names we could read, never raises.
    """
    out: Dict[str, List[str]] = {}
    refs = getattr(src_obj, "references", None)
    if not refs:
        return out
    for name in _REFERENCE_NAMES.get(base, ()):  # only the schema's ref names
        try:
            cross = refs.get(name) if hasattr(refs, "get") else None
            if cross is None:
                continue
            objs = getattr(cross, "objects", None) or []
            targets = [str(getattr(o, "uuid", "")) for o in objs]
            targets = [t for t in targets if t]
            if targets:
                out[name] = targets
        except Exception as exc:  # noqa: BLE001 — a bad ref name never aborts
            logger.debug("vector-copy: reading ref %s on %s failed: %s", name, base, exc)
    return out


def _remap_reference_targets(
    ref_targets: Dict[str, List[str]],
    uuid_map: Dict[str, str],
    is_live: Optional[Callable[[str], bool]] = None,
) -> Tuple[Dict[str, List[str]], int]:
    """Remap every reference target UUID through ``uuid_map``; return the write
    dict + the count of targets DROPPED (dangling/unmappable).

    Per-target rule (B3):
      * ``t in uuid_map``            → the target is being migrated → use the
                                       NEW uuid (else it dangles once the old
                                       target row is deleted). [remapped]
      * ``t not in uuid_map`` but it
        resolves to a live object    → keep ``t`` verbatim (a valid non-migrated
                                       target; its UUID is unchanged). [kept]
      * ``t`` neither in the map nor
        a live object                → DROP that single target + count it (a
                                       dangling beacon) — never guess. [dropped]

    ``is_live`` answers "does this UUID currently resolve to a live row?" (across
    every collection). When ``None`` every target is treated as live (kept) — a
    conservative default that never drops. In production, weaviate's
    ``return_references`` only surfaces LIVE targets, so ``is_live`` is
    effectively always True for what's read and the drop branch never fires; it
    is a defensive counter exercised by the unit tests.
    """
    remapped: Dict[str, List[str]] = {}
    dropped = 0
    for name, targets in ref_targets.items():
        kept: List[str] = []
        for t in targets:
            if t in uuid_map:
                kept.append(uuid_map[t])          # remapped
            elif is_live is None or is_live(t):
                kept.append(t)                     # kept (valid non-migrated)
            else:
                dropped += 1                       # dangling → drop + count
        if kept:
            remapped[name] = kept
    return remapped, dropped


def copy_one_row_with_vector(
    src_coll,
    dest_coll,
    src_uuid: str,
    dest_uuid: str,
    *,
    return_properties: Optional[List[str]] = None,
    project_override: Optional[str] = None,
    references: Optional[Dict[str, List[str]]] = None,
) -> CopyOutcome:
    """Copy ONE row (props + vector + references) from ``src_uuid`` to
    ``dest_uuid``.

    The vector is carried VERBATIM (named-vector dict written back unchanged —
    ZERO embedding computed) after :func:`clean_named_vector` drops any
    configured-but-empty ``{slot: []}`` slots weaviate would reject on write
    (B4). ``project_override`` replaces the ``project`` property on the
    destination (the whole point of an identity migration).

    ``references`` (B3) is the ALREADY-REMAPPED cross-reference write dict
    ``{ref_name: [target_uuid, ...]}`` — the caller reads the source row's
    references, remaps every target through the migration's global old→new UUID
    map, and passes the result here so it is written on insert/replace. Without
    it the copied row would carry ZERO references and — because it keeps its
    verbatim content_hash/embed_revision, so the analyzer SKIPs it forever — its
    edges (imports/extends/calls/handler/source_*) would be lost permanently.

    Collision handling (dest already present):
      * dest exists WITH a vector → it is a true duplicate of the source (same
        entity, different identity seed) → ``exists_with_vector`` (NO write; the
        caller may delete the source safely — data is preserved at dest).
      * dest exists WITHOUT a vector → overwrite it with the source's
        vector-bearing copy (``replaced_vectorless``) — a vectorless dest is
        NOT a safe stop, so we replace rather than drop the good source vector.
      * dest absent → insert a fresh copy (``copied``).

    Positive-confirmation rule: a write is confirmed by a read-back that finds
    the destination present. Any failure → ``failed`` (source LEFT in place).
    """
    # 1. Read the source row WITH its vector. Absent/unreadable → cannot copy.
    src_obj = _fetch_with_vector(src_coll, src_uuid, return_properties)
    if src_obj is None:
        return CopyOutcome("failed", dest_uuid, "source row unreadable/absent")
    src_props = dict(getattr(src_obj, "properties", None) or {})
    src_vec = getattr(src_obj, "vector", None)
    if not _vector_is_present(src_vec):
        # Copying a vectorless source would move an already-invalid row — leave
        # it (the analyzer re-embeds vectorless rows on its next walk under the
        # canonical identity anyway; nothing to preserve here).
        return CopyOutcome("failed", dest_uuid, "source row has no vector")
    if project_override is not None:
        src_props["project"] = project_override

    # B4: strip configured-but-empty named-vector slots BEFORE any write — a
    # ``{slot: []}`` round-trips on mixed-slot installs and weaviate rejects it
    # with WeaviateInvalidInputError('Invalid vectors: [].'). MUST MATCH the
    # sibling call in vco_lib/project_init.py::_copy_collection_with_vectors.
    write_vec = clean_named_vector(src_vec)

    # Only pass ``references=`` when there is at least one edge to write — the
    # weaviate insert/replace ``references`` kwarg tolerates None/absent but a
    # ``{}`` from a base with no refs is simply omitted.
    write_refs = references or None

    # 2. Collision probe on the destination.
    dest_obj = _fetch_with_vector(dest_coll, dest_uuid)
    if dest_obj is not None:
        dest_vec = getattr(dest_obj, "vector", None)
        if _vector_is_present(dest_vec):
            # A vector-bearing dest is a confirmed duplicate — the source's data
            # already lives at dest. No write; caller may delete the source.
            return CopyOutcome(
                "exists_with_vector", dest_uuid, "destination already has a vector"
            )
        # Vectorless dest → replace it with the good source vector (never drop).
        try:
            dest_coll.data.replace(
                uuid=dest_uuid, properties=src_props, vector=write_vec,
                references=write_refs,
            )
        except Exception as exc:  # noqa: BLE001 — replace failed → leave source
            return CopyOutcome("failed", dest_uuid, f"vectorless-dest replace failed: {exc}")
        if _confirm_written(dest_coll, dest_uuid):
            return CopyOutcome("replaced_vectorless", dest_uuid)
        return CopyOutcome("failed", dest_uuid, "replace not confirmed by read-back")

    # 3. Fresh insert at the destination UUID with the verbatim vector.
    try:
        dest_coll.data.insert(
            properties=src_props, uuid=dest_uuid, vector=write_vec,
            references=write_refs,
        )
    except Exception as exc:  # noqa: BLE001 — insert failed → leave source
        return CopyOutcome("failed", dest_uuid, f"insert failed: {exc}")
    if _confirm_written(dest_coll, dest_uuid):
        return CopyOutcome("copied", dest_uuid)
    return CopyOutcome("failed", dest_uuid, "insert not confirmed by read-back")


def _confirm_written(coll, uuid: str) -> bool:
    """Positive-confirmation read-back: does ``uuid`` now exist at ``coll``?

    Prefers ``data.exists`` (cheapest), falls back to a property-only point read.
    A confirmation FAILURE (read raises, method absent) returns ``False`` — the
    caller then treats the write as UNCONFIRMED and leaves the source (never
    deletes on an unproven copy).
    """
    try:
        data = getattr(coll, "data", None)
        exists = getattr(data, "exists", None) if data else None
        if callable(exists):
            return bool(exists(uuid))
    except Exception as exc:  # noqa: BLE001 — fall through to a read probe
        logger.debug("vector-copy: exists(%s) probe failed: %s", uuid, exc)
    # Fallback: a plain point-read (no vector needed) confirms presence.
    try:
        query = getattr(coll, "query", None)
        fetch_by_id = getattr(query, "fetch_object_by_id", None) if query else None
        if callable(fetch_by_id):
            return fetch_by_id(uuid, return_properties=[]) is not None
    except Exception as exc:  # noqa: BLE001
        logger.debug("vector-copy: read-back(%s) probe failed: %s", uuid, exc)
    return False


def _delete_row(coll, uuid: str) -> bool:
    """Delete ``uuid`` from ``coll``; ``True`` on success, ``False`` on failure.

    Soft-fail: a delete failure is logged and returned as ``False`` (the caller
    counts it — a source that couldn't be deleted after its dest was confirmed
    is a leftover dup, not data loss).
    """
    try:
        coll.data.delete_by_id(uuid)
        return True
    except Exception as exc:  # noqa: BLE001 — per-row soft-fail
        logger.warning("vector-copy: delete %s failed: %s", uuid, exc)
        return False


@dataclass
class MigrationSummary:
    """Aggregate outcome of :func:`migrate_project_identity`.

    ``moved``    — source rows copied to a fresh/replaced destination + deleted.
    ``deduped``  — source rows deleted because the destination already carried a
                   vector (true duplicates).
    ``left``     — source rows left in place (unreconstructable identity, or a
                   copy that could not be positively confirmed).
    ``failures`` — per-row hard errors (read/write/delete failures) — a SUBSET
                   overlap with ``left`` is possible (a failed copy is both a
                   failure and a leave); ``failures`` counts the error events.
    """

    moved: int = 0
    deduped: int = 0
    left: int = 0
    failures: int = 0
    #: B3 — cross-reference targets DROPPED because they were dangling
    #: (neither in the migration's old→new UUID map nor a currently-live UUID).
    #: A diagnostic counter only; NOT part of the machine-readable summary line
    #: (the Rust parser keys on moved/deduped/left/failures — adding a token
    #: would be tolerated by its unknown-key skip but T8 pins the exact line,
    #: so refs_dropped surfaces via the log, not the parsed line).
    refs_dropped: int = 0
    #: Per-collection breakdown (name → dict of the four counters) for logs.
    per_collection: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def summary_line(self) -> str:
        """The machine-readable one-liner WP-3's launcher parser keys on.

        Deliberately the FOUR canonical counters only — byte-identical to the
        pre-B3 contract so the Rust ``parse_identity_migration_summary`` and the
        T8 pin both keep matching. ``refs_dropped`` is logged separately.
        """
        return (
            f"IDENTITY_MIGRATION moved={self.moved} deduped={self.deduped} "
            f"left={self.left} failures={self.failures}"
        )


def _iter_project_rows(coll, base: str, old_identity: str):
    """Yield ``(uuid, props)`` for every row in ``coll`` where
    ``project == old_identity``.

    Reads only the properties the migration needs (the path + identity-bearing
    fields + chunk props for chunkable collections + ``project``).

    B2: weaviate-client v4.21's ``Collection.iterator`` has NO ``filters``
    kwarg (only ``include_vector`` / ``return_metadata`` / ``return_properties``
    / ``return_references`` / ``after`` / ``cache_size``) — an earlier draft
    passed ``filters=`` and every call raised ``TypeError`` that the caller's
    per-collection soft-fail swallowed, yielding an all-zero no-op migration.
    We iterate UNFILTERED and filter client-side on ``project == old_identity``
    (the same pattern :mod:`vco_lib.codegraph_prune` uses). On any iterator
    error the caller's per-collection try/except soft-fails the collection.
    """
    want_props = _row_return_properties(base)
    # v0.2.82 live-dry-run fix: requesting a property the LIVE schema lacks
    # 422s the whole iterate. `file_path` on CodeAPI/CodeInteraction is a NEW
    # v0.2.82 schema property — pre-.82 collections don't have it until the
    # analyzer's ensure-props runs post-update, and the pre-build migration
    # runs BEFORE the analyzer. Probe the schema (same pattern as
    # codegraph_prune) and request only present props; a row missing its path
    # prop then simply fails UUID reproduction and is LEFT + counted (the
    # self-neutralizing reconstruction check — never a wrong mint).
    try:
        schema_props = {p.name for p in coll.config.get().properties}
        want_props = [p for p in want_props if p in schema_props]
    except Exception:  # noqa: BLE001 — probe is best-effort; keep full list
        pass
    for obj in coll.iterator(return_properties=want_props):
        props = getattr(obj, "properties", None) or {}
        if props.get("project") != old_identity:
            continue
        yield str(obj.uuid), dict(props)


def _row_return_properties(base: str) -> List[str]:
    """The stored properties the migration must read for a given collection.

    Enough to (a) reconstruct the identity_key, (b) know the path for UUID
    re-derivation, and (c) know the chunk shape for chunkable collections.

    EVERY name here MUST be a real schema property of ``base`` — asking weaviate
    for a non-schema property 422s the whole read. (M1: an earlier draft
    requested ``source`` / ``source_entity`` for CodeInteraction; those are NOT
    schema properties — the interaction's source token is an extraction-time
    value that is never stored — so requesting them would 422 the whole
    collection once the iterate runs unfiltered. CodeInteraction's identity_key
    is therefore honestly unreconstructable and its rows are LEFT + counted;
    they converge when their file is re-walked under the new identity.)
    """
    props = {"project", "project_source"}
    props.add(_PATH_PROP.get(base, "file_path"))
    if base == "CodeModule":
        props.add("path")
    elif base in ("CodeClass", "CodeFunction"):
        props.update({"full_name", "name", "chunk_num", "total_chunks"})
    elif base == "CodeAPI":
        props.update({"endpoint", "method"})
    elif base == "CodeInteraction":
        # Schema-present props only (see docstring + analyzer schema block).
        # No ``source``/``source_entity`` (non-schema) → identity_key stays
        # unreconstructable → row left + counted, honestly.
        props.update({"endpoint", "raw_target"})
    return sorted(props)


def _dest_uuid_for(
    uuid_builder: Callable[..., str],
    new_identity: str,
    file_path_rel: str,
    identity_key: str,
    project_source: str,
) -> str:
    """Re-derive the destination UUID under ``new_identity`` — REUSES the
    analyzer's ``_deterministic_uuid`` (never a local mirror of the seed)."""
    return uuid_builder(
        new_identity, file_path_rel, identity_key, project_source=project_source,
    )


def _candidate_file_paths(props: Dict[str, Any], base: str) -> List[str]:
    """The file-path forms to try (RAW first, then ``\\``→``/`` normalized).

    A Windows-shaped legacy row's original UUID may have been minted from either
    the raw backslash form or the POSIX form; the migration re-derives the
    destination under BOTH and prefers the one whose SOURCE UUID matches the row
    being migrated (see :func:`_reconstruct_source_and_dest`). Order matters:
    exact (raw) first.
    """
    path_prop = _PATH_PROP.get(base, "file_path")
    raw = props.get(path_prop) or ""
    raw = str(raw)
    norm = _posix_normalize(raw)
    if norm and norm != raw:
        return [raw, norm]
    return [raw]


def migrate_project_identity(
    client,
    prefix: str,
    old_identity: str,
    new_identity: str,
    *,
    dry_run: bool = False,
    uuid_builder: Optional[Callable[..., str]] = None,
) -> MigrationSummary:
    """Migrate every ``project == old_identity`` row under ``prefix`` to be
    keyed under ``new_identity`` — copying vectors AND references VERBATIM,
    deduping, and deleting confirmed-migrated sources.

    TWO PASSES (B3 — references cross collections, so the old→new UUID map must
    be built for ALL FIVE collections before any row is copied):

    PASS 1 (plan): for each of the 5 collections ``<prefix>_<base>``, iterate
      rows where ``project == old_identity`` and, per row:
        1. reconstruct the identity_key from stored props (per shape); an
           unreconstructable row is LEFT + counted (never guessed);
        2. re-derive the destination UUID under ``new_identity`` (REUSING the
           analyzer's ``_deterministic_uuid``), trying the RAW stored path first
           and the ``\\``→``/`` normalized form second (prefer-exact); a row
           whose stored UUID no candidate reproduces is LEFT + counted.
      The reconstructable rows populate a GLOBAL ``old_uuid → new_uuid`` map
      (spanning all five collections) used to remap cross-references in pass 2.

    PASS 2 (execute): for each planned row, read it WITH its vector AND its
      cross-references, remap every reference target through the global map
      (in-map → new uuid; valid non-migrated target → keep; dangling → drop +
      count), then copy the row+vector+remapped-references to the destination
      (collision rule in :func:`copy_one_row_with_vector`), overriding
      ``project``. On a positively-confirmed destination delete the SOURCE — a
      source is NEVER deleted without that confirmation.

    ``dry_run=True`` runs pass 1 (planning + classification) but performs NO
    writes/deletes — the counters describe what WOULD happen. Per-row and
    per-collection soft-fail: a single bad row/collection never aborts the whole
    migration (there is no global timeout — per-row soft-fail is the guard, per
    project rule).

    NOTE (coordinator/user): running this on the ROOT project performs exactly
    the pending spaced-root dedup cleanup under the collision rule. Get explicit
    user OK before the first LIVE root run.
    """
    summary = MigrationSummary()
    if not prefix or not old_identity or not new_identity:
        logger.warning(
            "identity migration: missing prefix/old/new identity — no-op "
            "(prefix=%r old=%r new=%r)", prefix, old_identity, new_identity,
        )
        return summary
    if old_identity == new_identity:
        logger.info(
            "identity migration: old == new (%r) — nothing to migrate", old_identity
        )
        return summary

    builder = uuid_builder or _load_analyzer_uuid_builder()

    # ── PASS 1: plan every collection, building the GLOBAL old→new UUID map ──
    # per_collection_plans[coll_name] = (coll, base, [ _RowPlan, ... ])
    per_collection_plans: Dict[str, Tuple[Any, str, List["_RowPlan"]]] = {}
    # counters per collection accumulate `left` in pass 1, the rest in pass 2.
    per_collection_counters: Dict[str, Dict[str, int]] = {}
    uuid_map: Dict[str, str] = {}

    for base in _CODEGRAPH_BASES:
        coll_name = f"{prefix}_{base}"
        counters = {"moved": 0, "deduped": 0, "left": 0, "failures": 0}
        per_collection_counters[coll_name] = counters
        try:
            if hasattr(client.collections, "exists") and not client.collections.exists(coll_name):
                continue
            coll = client.collections.get(coll_name)
        except Exception as exc:  # noqa: BLE001 — per-collection soft-fail
            logger.warning("identity migration: cannot open %s: %s", coll_name, exc)
            continue

        try:
            rows = list(_iter_project_rows(coll, base, old_identity))
        except Exception as exc:  # noqa: BLE001 — per-collection soft-fail
            logger.warning(
                "identity migration: iterate %s failed: %s", coll_name, exc
            )
            # v0.2.82 live-dry-run fix: an unreadable collection is a FAILURE
            # of the migration's coverage, not a clean skip — count it so the
            # machine-readable summary can never report all-zero success over
            # a collection it could not even read (honest-signal rule).
            counters["failures"] += 1
            continue

        plans: List[_RowPlan] = []
        for src_uuid, props in rows:
            plan = _plan_one_row(
                base, src_uuid, props, old_identity, new_identity, builder,
            )
            if plan is None:
                counters["left"] += 1  # unreconstructable identity/seed
                continue
            plans.append(plan)
            uuid_map[plan.src_uuid] = plan.dest_uuid
        per_collection_plans[coll_name] = (coll, base, plans)

    # A liveness resolver over the whole client: "does this UUID currently
    # resolve to a live row in ANY of the five collections?" Used to decide
    # keep-vs-drop for a reference target that is NOT being migrated. Only the
    # collections we opened are probed (a missing/absent collection just never
    # matches). weaviate's read already pre-filters dangling beacons, so this is
    # defensive — it fires only for a target read back that no longer resolves.
    opened_colls = [c for (c, _b, _p) in per_collection_plans.values()]

    def _is_live(uuid: str) -> bool:
        for c in opened_colls:
            try:
                data = getattr(c, "data", None)
                exists = getattr(data, "exists", None) if data else None
                if callable(exists) and exists(uuid):
                    return True
            except Exception:  # noqa: BLE001 — a probe failure is not "live"
                continue
        return False

    # ── PASS 2: execute copies with remapped references (skipped on dry-run) ──
    for coll_name, (coll, base, plans) in per_collection_plans.items():
        counters = per_collection_counters[coll_name]
        for plan in plans:
            if dry_run:
                # Classify without writing: probe the destination to predict
                # move vs dedup (no references read; nothing is written).
                dest_obj = _fetch_with_vector(coll, plan.dest_uuid)
                if dest_obj is not None and _vector_is_present(
                    getattr(dest_obj, "vector", None)
                ):
                    counters["deduped"] += 1
                else:
                    counters["moved"] += 1
                continue
            outcome_key, dropped = _execute_one_row(
                coll, base, plan, new_identity, uuid_map, _is_live,
            )
            counters[outcome_key] += 1
            summary.refs_dropped += dropped

    # ── Aggregate the per-collection counters into the summary + log ──
    for coll_name, counters in per_collection_counters.items():
        for k in ("moved", "deduped", "left", "failures"):
            setattr(summary, k, getattr(summary, k) + counters[k])
        summary.per_collection[coll_name] = counters
        if any(counters.values()):
            logger.info(
                "identity migration: %s — moved=%d deduped=%d left=%d failures=%d%s",
                coll_name, counters["moved"], counters["deduped"],
                counters["left"], counters["failures"],
                " (dry-run)" if dry_run else "",
            )

    if summary.refs_dropped:
        logger.info(
            "identity migration: dropped %d dangling cross-reference target(s) "
            "(beacons pointing at rows neither migrated nor currently live)",
            summary.refs_dropped,
        )

    return summary


@dataclass
class _RowPlan:
    """A pass-1 plan for one reconstructable row: everything pass 2 needs to
    copy it WITHOUT re-reading the identity-bearing props."""

    src_uuid: str
    dest_uuid: str


def _plan_one_row(
    base: str,
    src_uuid: str,
    props: Dict[str, Any],
    old_identity: str,
    new_identity: str,
    builder: Callable[..., str],
) -> Optional["_RowPlan"]:
    """Pass-1 planning for ONE source row.

    Returns a :class:`_RowPlan` (src_uuid + derived dest_uuid) for a
    reconstructable row, or ``None`` when the row is unreconstructable (the
    caller counts it as ``left``). Performs NO writes — purely identity/seed
    reconstruction.

    A chunkable row carries a per-chunk identity: chunk 0 keys on the bare
    identity_key, chunk ``i`` on ``<key>::<i>`` (the analyzer/guards rule). The
    migration derives the SAME per-chunk key from the row's stored ``chunk_num``
    so each chunk row plans its own correct destination UUID (T11).
    """
    identity_key = _identity_key_for_row(base, props)
    if identity_key is None:
        logger.debug(
            "identity migration: %s row %s has unreconstructable identity_key "
            "(shape=%s) — left in place", base, src_uuid, base,
        )
        return None

    # Chunk-aware identity_key: chunk i>0 keys on ``<key>::<i>``. chunk_num is
    # stored only on chunkable collections; absent/0 → the bare (chunk-0) key.
    per_chunk_key = identity_key
    if base in _CHUNKABLE_BASES:
        try:
            chunk_num = int(props.get("chunk_num") or 0)
        except (TypeError, ValueError):
            chunk_num = 0
        if chunk_num > 0:
            per_chunk_key = f"{identity_key}::{chunk_num}"

    project_source = str(props.get("project_source") or "")

    # Try each candidate file-path form (RAW first, then POSIX-normalized) and
    # prefer the one whose re-derived SOURCE UUID matches this row's actual UUID
    # (i.e. the shape the original UUID was minted from).
    candidates = _candidate_file_paths(props, base)
    chosen_fp: Optional[str] = None
    for fp in candidates:
        try:
            src_check = _dest_uuid_for(
                builder, old_identity, fp, per_chunk_key, project_source,
            )
        except Exception as exc:  # noqa: BLE001 — bad seed inputs → try next
            logger.debug("identity migration: source re-derive failed (%s): %s", fp, exc)
            continue
        if src_check == src_uuid:
            chosen_fp = fp
            break
    if chosen_fp is None:
        # No candidate reproduced the stored UUID. This means the row's UUID was
        # minted from inputs we cannot reconstruct (an older seed shape, a
        # different identity token) — LEAVE it (counted), never mint a wrong
        # destination that would orphan the row.
        logger.debug(
            "identity migration: %s row %s — no file-path form reproduced its "
            "source UUID (candidates=%r) — left in place",
            base, src_uuid, candidates,
        )
        return None

    try:
        dest_uuid = _dest_uuid_for(
            builder, new_identity, chosen_fp, per_chunk_key, project_source,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "identity migration: %s row %s — dest UUID derivation failed: %s",
            base, src_uuid, exc,
        )
        # A dest-derivation failure with a reproduced source seed is genuinely
        # anomalous; leave the row (counted as left, not a mid-write failure).
        return None

    return _RowPlan(src_uuid=src_uuid, dest_uuid=dest_uuid)


def _execute_one_row(
    coll,
    base: str,
    plan: "_RowPlan",
    new_identity: str,
    uuid_map: Dict[str, str],
    is_live: Callable[[str], bool],
) -> Tuple[str, int]:
    """Pass-2 execution for ONE planned row. Returns ``(counter_key, refs_dropped)``
    where ``counter_key`` is ``"moved"`` | ``"deduped"`` | ``"failures"``.

    Reads the row WITH its cross-references, remaps every target through the
    global ``uuid_map`` (B3), copies the row+vector+references to the
    destination, and — on a positively-confirmed destination — deletes the
    source.
    """
    src_uuid, dest_uuid = plan.src_uuid, plan.dest_uuid

    # Read the source's cross-references and remap them through the global
    # old→new map: in-map → new uuid; live non-migrated target → keep; anything
    # else → dropped + counted. weaviate returns only live targets, so a
    # production read never yields a dangling one; the drop branch counts an
    # edge beacon that is neither migrated nor currently live.
    ref_targets = _read_source_references(coll, base, src_uuid)
    write_refs, dropped = _remap_reference_targets(ref_targets, uuid_map, is_live)

    # IMPORTANT: read the FULL source row (return_properties=None) for the copy
    # — the ``return_props`` iterator subset is ONLY the identity-reconstruction
    # fields; copying that subset would DROP every other stored property
    # (signature, body, content_hash, embed_revision, …) on the destination, a
    # silent data-loss. The copy must preserve the row verbatim.
    outcome = copy_one_row_with_vector(
        coll, coll, src_uuid, dest_uuid,
        return_properties=None,
        project_override=new_identity,
        references=write_refs,
    )
    if outcome.status == "failed":
        logger.warning(
            "identity migration: %s row %s → %s not confirmed (%s) — source left",
            base, src_uuid, dest_uuid, outcome.message,
        )
        return "failures", dropped

    # Destination positively confirmed — safe to delete the source. src == dest
    # only when old==new (guarded out) so this never deletes the row we wrote.
    if src_uuid == dest_uuid:
        # Defensive: identical UUID (shouldn't happen — project seed differs).
        # Do NOT delete the row we just wrote.
        key = "deduped" if outcome.status == "exists_with_vector" else "moved"
        return key, dropped
    if not _delete_row(coll, src_uuid):
        # Dest is confirmed (data preserved) but the source delete failed → a
        # leftover dup, counted as a failure event (NOT data loss).
        return "failures", dropped

    return ("deduped" if outcome.status == "exists_with_vector" else "moved"), dropped


def _read_source_references(coll, base: str, src_uuid: str) -> Dict[str, List[str]]:
    """Point-read ``src_uuid`` WITH its cross-references; return
    ``{ref_name: [target_uuid, ...]}``. Empty dict on absence/error/no-refs.

    Uses ``fetch_object_by_id(..., return_references=[QueryReference(...)])`` so
    each reference resolves to its target object(s) (whose ``.uuid`` we read).
    Soft-fail: any read error → empty dict (the copy proceeds with no refs
    rather than aborting — a missing ref is recoverable on a future full walk).
    """
    want_refs = _query_references_for(base)
    if want_refs is None:
        return {}
    try:
        query = getattr(coll, "query", None)
        fetch_by_id = getattr(query, "fetch_object_by_id", None) if query else None
        if not callable(fetch_by_id):
            return {}
        obj = fetch_by_id(src_uuid, return_references=want_refs)
        if obj is None:
            return {}
        return _read_reference_targets(obj, base)
    except Exception as exc:  # noqa: BLE001 — ref read failure → no refs carried
        logger.debug(
            "vector-copy: reference read for %s (%s) failed: %s", src_uuid, base, exc
        )
        return {}


# ── copy_rows_with_vectors: the batch primitive (Task 1 public entry) ────────


def copy_rows_with_vectors(
    src_coll,
    dest_coll,
    uuid_pairs: List[Tuple[str, str]],
    *,
    return_properties: Optional[List[str]] = None,
    project_override: Optional[str] = None,
    delete_source_on_confirm: bool = False,
) -> Dict[str, int]:
    """Copy many ``(src_uuid, dest_uuid)`` rows with their vectors VERBATIM.

    The batch wrapper around :func:`copy_one_row_with_vector`. Per-row soft-fail
    (one bad row never aborts the batch); a source is deleted ONLY when
    ``delete_source_on_confirm`` is set AND the destination write was positively
    confirmed. Returns ``{"copied", "deduped", "replaced", "failed", "deleted"}``
    counts. ZERO embeddings computed — vectors are carried, never recomputed.
    """
    counts = {"copied": 0, "deduped": 0, "replaced": 0, "failed": 0, "deleted": 0}
    for src_uuid, dest_uuid in uuid_pairs:
        outcome = copy_one_row_with_vector(
            src_coll, dest_coll, src_uuid, dest_uuid,
            return_properties=return_properties,
            project_override=project_override,
        )
        if outcome.status == "copied":
            counts["copied"] += 1
        elif outcome.status == "exists_with_vector":
            counts["deduped"] += 1
        elif outcome.status == "replaced_vectorless":
            counts["replaced"] += 1
        else:
            counts["failed"] += 1
            continue  # unconfirmed → never delete the source
        if delete_source_on_confirm and src_uuid != dest_uuid:
            if _delete_row(src_coll, src_uuid):
                counts["deleted"] += 1
    return counts


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_client(
    weaviate_url: Optional[str] = None, grpc_port: Optional[int] = None,
):
    """Build a Weaviate v4 client from url/env/defaults; ``None`` on failure.

    Mirrors ``codegraph_resync._build_client`` (the endorsed connection recipe)
    — kept local to avoid importing the whole resync module for the CLI path."""
    try:
        import weaviate

        url = weaviate_url or os.environ.get("WEAVIATE_URL") or "http://localhost:8081"
        m = re.match(r"^https?://([^:/]+)(?::(\d+))?", url)
        host = m.group(1) if m else "localhost"
        http_port = int(m.group(2)) if (m and m.group(2)) else 8081
        gport = int(grpc_port or os.environ.get("GRPC_PORT") or 50052)
        return weaviate.connect_to_custom(
            http_host=host, http_port=http_port, http_secure=False,
            grpc_host=host, grpc_port=gport, grpc_secure=False,
        )
    except Exception as exc:  # noqa: BLE001 — no Weaviate → CLI degrades
        logger.error("identity migration: Weaviate unavailable: %s", exc)
        return None


def _cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.codegraph_vector_copy",
        description=(
            "Migrate code-graph rows from one project identity (display name) to "
            "another (canonical prefix), reusing stored vectors verbatim."
        ),
    )
    parser.add_argument(
        "--migrate-identity", action="store_true",
        help="Run the project-identity row migration.",
    )
    parser.add_argument("--prefix", help="Weaviate class prefix (e.g. MyProj).")
    parser.add_argument(
        "--from", dest="from_identity",
        help='Source project identity (the OLD "project" value, e.g. "Old Name").',
    )
    parser.add_argument(
        "--to", dest="to_identity",
        help="Destination project identity (the canonical value, e.g. NewName).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Classify without writing/deleting (report what WOULD happen).",
    )
    parser.add_argument("--weaviate-url", default=None)
    parser.add_argument("--grpc-port", type=int, default=None)
    args = parser.parse_args(argv)

    if not args.migrate_identity:
        parser.error("nothing to do — pass --migrate-identity")
    if not (args.prefix and args.from_identity and args.to_identity):
        parser.error("--migrate-identity requires --prefix, --from and --to")

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    client = _build_client(args.weaviate_url, args.grpc_port)
    if client is None:
        # Emit a summary line so a parser sees a deterministic shape even on the
        # degraded path (all-zero counts).
        print(MigrationSummary().summary_line(), flush=True)
        return 1
    try:
        summary = migrate_project_identity(
            client, args.prefix, args.from_identity, args.to_identity,
            dry_run=args.dry_run,
        )
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    # The machine-readable summary line WP-3's launcher parser keys on. Printed
    # LAST so the parser can take the final occurrence.
    print(summary.summary_line(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
