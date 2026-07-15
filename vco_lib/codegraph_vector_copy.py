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
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    return builder


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
        # ``ix::<source>::<endpoint>``. The source token is not stored; only
        # reconstructable when the row carries an explicit source hint. We do
        # NOT guess (a wrong identity_key mints a wrong UUID → a silent orphan),
        # so absent-source interaction rows are LEFT + counted (D3-shaped:
        # they converge when their file is re-walked under the new identity).
        source = props.get("source") or props.get("source_entity")
        endpoint = props.get("endpoint")
        if not source or not endpoint:
            return None
        return f"ix::{source}::{endpoint}"
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


def copy_one_row_with_vector(
    src_coll,
    dest_coll,
    src_uuid: str,
    dest_uuid: str,
    *,
    return_properties: Optional[List[str]] = None,
    project_override: Optional[str] = None,
) -> CopyOutcome:
    """Copy ONE row (props + vector) from ``src_uuid`` to ``dest_uuid``.

    The vector is carried VERBATIM (named-vector dict written back unchanged —
    ZERO embedding computed). ``project_override`` replaces the ``project``
    property on the destination (the whole point of an identity migration).

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
                uuid=dest_uuid, properties=src_props, vector=src_vec,
            )
        except Exception as exc:  # noqa: BLE001 — replace failed → leave source
            return CopyOutcome("failed", dest_uuid, f"vectorless-dest replace failed: {exc}")
        if _confirm_written(dest_coll, dest_uuid):
            return CopyOutcome("replaced_vectorless", dest_uuid)
        return CopyOutcome("failed", dest_uuid, "replace not confirmed by read-back")

    # 3. Fresh insert at the destination UUID with the verbatim vector.
    try:
        dest_coll.data.insert(
            properties=src_props, uuid=dest_uuid, vector=src_vec,
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
    #: Per-collection breakdown (name → dict of the four counters) for logs.
    per_collection: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def summary_line(self) -> str:
        """The machine-readable one-liner WP-3's launcher parser keys on."""
        return (
            f"IDENTITY_MIGRATION moved={self.moved} deduped={self.deduped} "
            f"left={self.left} failures={self.failures}"
        )


def _iter_project_rows(coll, base: str, old_identity: str):
    """Yield ``(uuid, props)`` for every row in ``coll`` where
    ``project == old_identity``.

    Reads only the properties the migration needs (the path + identity-bearing
    fields + chunk props for chunkable collections + ``project``). A filtered
    server-side query is preferred; on any filter/iterator error the caller's
    per-collection try/except soft-fails the whole collection.
    """
    from weaviate.classes.query import Filter

    want_props = _row_return_properties(base)
    flt = Filter.by_property("project").equal(old_identity)
    for obj in coll.iterator(return_properties=want_props, filters=flt):
        props = getattr(obj, "properties", None) or {}
        yield str(obj.uuid), dict(props)


def _row_return_properties(base: str) -> List[str]:
    """The stored properties the migration must read for a given collection.

    Enough to (a) reconstruct the identity_key, (b) know the path for UUID
    re-derivation, and (c) know the chunk shape for chunkable collections.
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
        props.update({"endpoint", "source", "source_entity", "raw_target"})
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
    keyed under ``new_identity`` — copying vectors VERBATIM, deduping, and
    deleting confirmed-migrated sources.

    For each of the 5 collections ``<prefix>_<base>``:
      1. iterate rows where ``project == old_identity``;
      2. reconstruct the identity_key from stored props (per shape); an
         unreconstructable row is LEFT + counted (never guessed);
      3. re-derive the destination UUID under ``new_identity`` (REUSING the
         analyzer's ``_deterministic_uuid``), trying the RAW stored path first
         and the ``\\``→``/`` normalized form second (prefer-exact);
      4. copy the row+vector to the destination (collision rule in
         :func:`copy_one_row_with_vector`), overriding the ``project`` property;
      5. on a positively-confirmed destination (fresh copy, replaced vectorless,
         or a pre-existing vector-bearing dest) delete the SOURCE. A source is
         NEVER deleted without that confirmation.

    ``dry_run=True`` reads + classifies but performs NO writes/deletes — the
    counters describe what WOULD happen. Per-row and per-collection soft-fail:
    a single bad row/collection never aborts the whole migration (there is no
    global timeout — per-row soft-fail is the guard, per project rule).

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

    for base in _CODEGRAPH_BASES:
        coll_name = f"{prefix}_{base}"
        counters = {"moved": 0, "deduped": 0, "left": 0, "failures": 0}
        try:
            if hasattr(client.collections, "exists") and not client.collections.exists(coll_name):
                summary.per_collection[coll_name] = counters
                continue
            coll = client.collections.get(coll_name)
        except Exception as exc:  # noqa: BLE001 — per-collection soft-fail
            logger.warning("identity migration: cannot open %s: %s", coll_name, exc)
            summary.per_collection[coll_name] = counters
            continue

        try:
            rows = list(_iter_project_rows(coll, base, old_identity))
        except Exception as exc:  # noqa: BLE001 — per-collection soft-fail
            logger.warning(
                "identity migration: iterate %s failed: %s", coll_name, exc
            )
            summary.per_collection[coll_name] = counters
            continue

        for src_uuid, props in rows:
            outcome = _migrate_one_row(
                coll, base, src_uuid, props, old_identity, new_identity,
                builder, dry_run=dry_run,
            )
            counters[outcome] += 1

        for k in counters:
            setattr(summary, k, getattr(summary, k) + counters[k])
        summary.per_collection[coll_name] = counters
        if any(counters.values()):
            logger.info(
                "identity migration: %s — moved=%d deduped=%d left=%d failures=%d%s",
                coll_name, counters["moved"], counters["deduped"],
                counters["left"], counters["failures"],
                " (dry-run)" if dry_run else "",
            )

    return summary


def _migrate_one_row(
    coll,
    base: str,
    src_uuid: str,
    props: Dict[str, Any],
    old_identity: str,
    new_identity: str,
    builder: Callable[..., str],
    *,
    dry_run: bool,
) -> str:
    """Migrate ONE source row; return the counter key it lands in
    (``"moved"`` | ``"deduped"`` | ``"left"`` | ``"failures"``).

    A chunkable row carries a per-chunk identity: chunk 0 keys on the bare
    identity_key, chunk ``i`` on ``<key>::<i>`` (the analyzer/guards rule). The
    migration derives the SAME per-chunk key from the row's stored ``chunk_num``
    so each chunk row moves to its own correct destination UUID (T11).
    """
    identity_key = _identity_key_for_row(base, props)
    if identity_key is None:
        logger.debug(
            "identity migration: %s row %s has unreconstructable identity_key "
            "(shape=%s) — left in place", base, src_uuid, base,
        )
        return "left"

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
    # (i.e. the shape the original UUID was minted from). If NEITHER matches the
    # source, we still migrate under the RAW form's destination (best-effort:
    # the row exists, its identity is what it is — but we log the mismatch so a
    # genuinely-unreconstructable seed is visible rather than silently wrong).
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
        return "left"

    try:
        dest_uuid = _dest_uuid_for(
            builder, new_identity, chosen_fp, per_chunk_key, project_source,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "identity migration: %s row %s — dest UUID derivation failed: %s",
            base, src_uuid, exc,
        )
        return "failures"

    if dry_run:
        # Classify without writing: probe the destination to predict move vs dedup.
        dest_obj = _fetch_with_vector(coll, dest_uuid)
        if dest_obj is not None and _vector_is_present(getattr(dest_obj, "vector", None)):
            return "deduped"
        return "moved"

    # IMPORTANT: read the FULL source row (return_properties=None) for the copy
    # — the ``return_props`` iterator subset is ONLY the identity-reconstruction
    # fields; copying that subset would DROP every other stored property
    # (signature, body, content_hash, embed_revision, …) on the destination, a
    # silent data-loss. The copy must preserve the row verbatim.
    outcome = copy_one_row_with_vector(
        coll, coll, src_uuid, dest_uuid,
        return_properties=None,
        project_override=new_identity,
    )
    if outcome.status == "failed":
        logger.warning(
            "identity migration: %s row %s → %s not confirmed (%s) — source left",
            base, src_uuid, dest_uuid, outcome.message,
        )
        return "failures"

    # Destination positively confirmed — safe to delete the source. src == dest
    # only when old==new (guarded out) so this never deletes the row we wrote.
    if src_uuid == dest_uuid:
        # Defensive: identical UUID (shouldn't happen — project seed differs).
        # Do NOT delete the row we just wrote.
        return "moved" if outcome.status != "exists_with_vector" else "deduped"
    if not _delete_row(coll, src_uuid):
        # Dest is confirmed (data preserved) but the source delete failed → a
        # leftover dup, counted as a failure event (NOT data loss).
        return "failures"

    return "deduped" if outcome.status == "exists_with_vector" else "moved"


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
