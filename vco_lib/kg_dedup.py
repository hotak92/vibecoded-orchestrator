# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Reconcile pre-existing DUPLICATE OBJECTS in a project's KG collection
(v0.2.92 WP-B2, field defect D13).

WP-B1 fixed the CAUSE: every write now stores ONE canonical POSIX-relative
``file_path`` and the delete leg of the upsert matches both spellings
(``vco_lib.paths.to_posix_rel`` + the dual-shape ``Filter.any_of`` idiom in
``templates/scripts/sync_knowledge_graph.py``). So a node that is written
again heals itself.

That is exactly the limit of WP-B1: it heals a path only when that path is
NEXT WRITTEN. A node nobody edits again stays duplicated forever — the field
tester's collection held 115 objects over 111 distinct paths, with 4x and 2x
duplication on individual nodes. This module is the maintenance pass that
reconciles those already-written rows, shipped as the ``kg-dedup`` CLI.

Design notes:

* **Grouping applies WP-B1's canonicalisation, at READ time.** Two rows
  spelled ``knowledge\\concepts\\foo.md`` and ``knowledge/concepts/foo.md``
  are the same source file — that divergence IS the D13 defect — so both
  land in one group via :func:`vco_lib.paths.to_posix_rel`. This is the same
  one-home normalizer ``sync_knowledge_graph.py`` imports; grouping by the
  canonical form subsumes the dual-shape filter rather than re-deriving it.
* **The group key includes ``chunk_num``.** A chunked node legitimately owns
  N objects for one ``file_path`` (chunk 1..N). Keying on ``file_path``
  alone would delete real chunks — data loss dressed as a fix.
* **Deletes are by UUID**, taken from rows we just read, so a delete can
  never widen to rows we did not inspect.
* **Dry run is the default.** This is destructive on user data, and the repo
  rule is that destructive operations are never auto-applied.
* **Every unreadable state REFUSES** with a named reason and a non-zero
  exit — unreachable Weaviate, absent collection, empty collection, failed
  read. A check that cannot distinguish "I could not determine this" from
  "this is fine" is not a check, and reporting "0 duplicates" for a
  collection it never read is precisely that failure.

Exit-code convention matches ``templates/scripts/kg-sync`` (whose vocabulary
is documented in ``sync_knowledge_graph.py``'s module docstring):
0 = clean · 1 = operational failure / refusal · 2 = usage error.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from vco_lib.paths import to_posix_rel

__all__ = [
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_USAGE",
    "REASON_UNREACHABLE",
    "REASON_COLLECTION_MISSING",
    "REASON_COLLECTION_EMPTY",
    "REASON_READ_FAILED",
    "REASON_DELETE_FAILED",
    "DuplicateGroup",
    "DedupReport",
    "WeaviateBackend",
    "group_rows",
    "reconcile",
    "format_report",
    "main",
]

# Exit codes — match `templates/scripts/kg-sync`'s convention exactly so a
# caller that already knows kg-sync's codes is not surprised here.
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2

# Named refusal reasons. These are printed verbatim and asserted on by
# tests — a refusal must always say WHICH state it could not read.
REASON_UNREACHABLE = "weaviate_unreachable"
REASON_COLLECTION_MISSING = "collection_missing"
REASON_COLLECTION_EMPTY = "collection_empty"
REASON_READ_FAILED = "read_failed"
REASON_DELETE_FAILED = "delete_failed"

#: Sentinel chunk key for rows whose ``chunk_num`` is absent or unparseable
#: (legacy unchunked rows). Kept an int so group keys stay sortable.
_NO_CHUNK = -1


@dataclass(frozen=True)
class DuplicateGroup:
    """One ``(canonical file_path, chunk_num)`` key that owns >1 object."""

    file_path: str
    chunk_num: int
    keep_uuid: str
    keep_updated_at: str
    delete_uuids: tuple[str, ...]
    #: Every distinct RAW ``file_path`` spelling seen in this group. More
    #: than one means the group was united by canonicalisation — i.e. this
    #: is a genuine D13 mixed-separator duplicate.
    spellings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DedupReport:
    """Outcome of one reconcile pass.

    ``status`` is one of ``"ok"`` (read succeeded, nothing to do),
    ``"duplicates"`` (read succeeded, duplicates found) or ``"refused"``
    (the target state could not be read / the delete failed).
    """

    status: str
    collection: str
    reason: str = ""
    detail: str = ""
    scanned: int = 0
    distinct_keys: int = 0
    groups: tuple[DuplicateGroup, ...] = ()
    deleted: int = 0
    applied: bool = False

    @property
    def duplicate_objects(self) -> int:
        """Objects that are extras (i.e. would be / were deleted)."""
        return sum(len(g.delete_uuids) for g in self.groups)

    @property
    def exit_code(self) -> int:
        return EXIT_REFUSED if self.status == "refused" else EXIT_OK


# ──────────────────────────────────────────────────────────────────────
# Pure grouping / keeper selection
# ──────────────────────────────────────────────────────────────────────

def _chunk_key(raw: Any) -> int:
    """Normalize a row's ``chunk_num`` into a sortable int.

    Absent / None / unparseable → :data:`_NO_CHUNK`. Never raises: a row
    with a malformed chunk number must still be grouped (conservatively,
    with its own kind) rather than crash the whole pass.
    """
    if raw is None:
        return _NO_CHUNK
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _NO_CHUNK


def _ts_key(raw: Any) -> str:
    """Normalize ``updated_at`` into a lexicographically comparable string.

    Weaviate returns RFC3339 strings (or ``datetime`` via the v4 client),
    both of which sort correctly as ISO text. Missing → ``""``, which sorts
    below every real timestamp, so a row that never recorded an update can
    only be the keeper when EVERY member of its group is equally undated.
    """
    if raw is None:
        return ""
    if isinstance(raw, datetime):
        return raw.isoformat()
    return str(raw)


def _pick_keeper(members: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Newest by ``updated_at``; ties broken by the smallest UUID.

    The tiebreak is deterministic on purpose: two runs over the same
    collection must choose the same survivor, or a dry run would not
    predict what ``--apply`` does.
    """
    best_ts = max(_ts_key(m.get("updated_at")) for m in members)
    tied = [m for m in members if _ts_key(m.get("updated_at")) == best_ts]
    return min(tied, key=lambda m: str(m.get("uuid", "")))


def group_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[
    tuple[DuplicateGroup, ...], int
]:
    """Group *rows* by ``(canonical file_path, chunk_num)``.

    Returns ``(duplicate_groups, distinct_key_count)``. Rows with an empty
    or missing ``file_path`` are SKIPPED, not guessed at — there is no
    canonical key to reconcile them under, and deleting on a guess is the
    failure mode this tool exists to avoid.
    """
    buckets: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for row in rows:
        raw_path = row.get("file_path")
        if raw_path is None or not str(raw_path).strip():
            continue
        canonical = to_posix_rel(str(raw_path))
        buckets.setdefault((canonical, _chunk_key(row.get("chunk_num"))), []).append(row)

    groups: list[DuplicateGroup] = []
    for (canonical, chunk), members in sorted(buckets.items()):
        if len(members) < 2:
            continue
        keeper = _pick_keeper(members)
        keep_uuid = str(keeper.get("uuid", ""))
        delete_uuids = tuple(sorted(
            str(m.get("uuid", "")) for m in members
            if str(m.get("uuid", "")) != keep_uuid
        ))
        spellings = tuple(sorted({str(m.get("file_path")) for m in members}))
        groups.append(DuplicateGroup(
            file_path=canonical,
            chunk_num=chunk,
            keep_uuid=keep_uuid,
            keep_updated_at=_ts_key(keeper.get("updated_at")),
            delete_uuids=delete_uuids,
            spellings=spellings,
        ))
    return tuple(groups), len(buckets)


# ──────────────────────────────────────────────────────────────────────
# Backend (injectable — the tests supply a fake, never a live Weaviate)
# ──────────────────────────────────────────────────────────────────────

class WeaviateBackend:
    """Live Weaviate access for :func:`reconcile`.

    Opens ONE connection (via the shared ``weaviate_helpers.connect_v4``
    factory — the same one ``sync_knowledge_graph.py`` uses) and serves
    every probe from it. Tests inject a fake object exposing the same four
    methods instead of standing up a real instance.
    """

    def __init__(self, weaviate_url: str, grpc_port: int | None = None) -> None:
        self.weaviate_url = weaviate_url
        self._grpc_port = grpc_port
        self._client: Any = None

    # -- lifecycle ----------------------------------------------------
    def reachable(self) -> bool:
        try:
            from vco_lib import weaviate_helpers as _wh
            self._client = _wh.connect_v4(
                self.weaviate_url,
                grpc_port=self._grpc_port,
                http_secure=False,
                skip_init_checks=False,
            )
            return bool(self._client.is_ready())
        except Exception:
            self._client = None
            return False

    def close(self) -> None:
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass
        self._client = None

    def __enter__(self) -> "WeaviateBackend":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- probes -------------------------------------------------------
    def collection_exists(self, collection: str) -> bool:
        return bool(self._client.collections.exists(collection))

    def fetch_rows(self, collection: str) -> list[dict[str, Any]]:
        coll = self._client.collections.get(collection)
        rows: list[dict[str, Any]] = []
        for obj in coll.iterator(
            return_properties=["file_path", "updated_at", "chunk_num"]
        ):
            props = obj.properties or {}
            rows.append({
                "uuid": str(obj.uuid),
                "file_path": props.get("file_path"),
                "updated_at": props.get("updated_at"),
                "chunk_num": props.get("chunk_num"),
            })
        return rows

    def delete(self, collection: str, uuids: Sequence[str]) -> int:
        """Delete by UUID, one at a time — the same idiom as
        ``sync_knowledge_graph.py::_delete_node_by_file_path``. Returns the
        count actually removed; a mid-way failure propagates so the caller
        can report a PARTIAL delete rather than claim a clean run."""
        coll = self._client.collections.get(collection)
        n = 0
        for u in uuids:
            coll.data.delete_by_id(u)
            n += 1
        return n


# ──────────────────────────────────────────────────────────────────────
# Reconcile
# ──────────────────────────────────────────────────────────────────────

def reconcile(backend: Any, collection: str, *, apply: bool = False) -> DedupReport:
    """Read *collection* through *backend*, group, and (optionally) delete.

    Never deletes unless ``apply=True``. Never reports a clean result for a
    state it could not read — every such case returns ``status="refused"``
    with a named ``reason``.
    """
    def refuse(reason: str, detail: str = "", **kw: Any) -> DedupReport:
        return DedupReport(
            status="refused", collection=collection, reason=reason,
            detail=detail, **kw,
        )

    try:
        if not backend.reachable():
            return refuse(
                REASON_UNREACHABLE,
                f"cannot reach Weaviate at {getattr(backend, 'weaviate_url', '?')}",
            )
    except Exception as exc:  # a probe that CRASHES is also "unreachable"
        return refuse(REASON_UNREACHABLE, f"{type(exc).__name__}: {exc}")

    try:
        exists = backend.collection_exists(collection)
    except Exception as exc:
        return refuse(REASON_READ_FAILED, f"{type(exc).__name__}: {exc}")
    if not exists:
        return refuse(
            REASON_COLLECTION_MISSING,
            f"collection {collection!r} does not exist on this Weaviate",
        )

    try:
        rows = list(backend.fetch_rows(collection))
    except Exception as exc:
        return refuse(REASON_READ_FAILED, f"{type(exc).__name__}: {exc}")

    if not rows:
        return refuse(
            REASON_COLLECTION_EMPTY,
            f"collection {collection!r} returned 0 objects — refusing to "
            f"report 'no duplicates' for a collection with nothing in it",
        )

    groups, distinct = group_rows(rows)
    # The shared fields are spelled out at each call rather than splatted from
    # a dict: a ``dict(...)`` of mixed value types widens to a union that
    # cannot be checked against the dataclass's per-field types, so ``**base``
    # silently gave up type-checking on every DedupReport construction here.
    scanned = len(rows)
    if not groups:
        return DedupReport(
            status="ok",
            collection=collection, scanned=scanned,
            distinct_keys=distinct, groups=groups,
        )
    if not apply:
        return DedupReport(
            status="duplicates", applied=False, deleted=0,
            collection=collection, scanned=scanned,
            distinct_keys=distinct, groups=groups,
        )

    victims = [u for g in groups for u in g.delete_uuids]
    try:
        deleted = int(backend.delete(collection, victims))
    except Exception as exc:
        return DedupReport(
            status="refused", reason=REASON_DELETE_FAILED,
            detail=f"{type(exc).__name__}: {exc}", applied=True, deleted=0,
            collection=collection, scanned=scanned,
            distinct_keys=distinct, groups=groups,
        )
    return DedupReport(
        status="duplicates", applied=True, deleted=deleted,
        collection=collection, scanned=scanned,
        distinct_keys=distinct, groups=groups,
    )


# ──────────────────────────────────────────────────────────────────────
# Presentation + CLI
# ──────────────────────────────────────────────────────────────────────

_TAG = "kg-dedup"


def format_report(report: DedupReport) -> list[str]:
    """Human-readable lines for *report* (stdout lines; refusals go to
    stderr — see :func:`main`)."""
    if report.status == "refused":
        return [
            f"{_TAG}: ERROR - {report.reason}: {report.detail}",
            f"{_TAG}: Refusing to report a duplicate count for a state it "
            f"could not read.",
        ]

    lines = [
        f"{_TAG}: collection '{report.collection}' - {report.scanned} object(s), "
        f"{report.distinct_keys} distinct (file_path, chunk_num) key(s)",
    ]
    if report.status == "ok":
        lines.append(f"{_TAG}: no duplicates. Nothing to do.")
        return lines

    verb = "deleted" if report.applied else "would delete"
    lines.append(
        f"{_TAG}: {len(report.groups)} key(s) hold duplicates - "
        f"{verb} {report.duplicate_objects} extra object(s):"
    )
    for g in report.groups:
        chunk = "-" if g.chunk_num == _NO_CHUNK else str(g.chunk_num)
        lines.append(
            f"{_TAG}:   {g.file_path} [chunk {chunk}] - keep {g.keep_uuid} "
            f"(updated_at={g.keep_updated_at or 'n/a'}), {verb} "
            f"{len(g.delete_uuids)}"
        )
        for u in g.delete_uuids:
            lines.append(f"{_TAG}:     - {u}")
        if len(g.spellings) > 1:
            lines.append(
                f"{_TAG}:     (mixed file_path spellings: "
                f"{', '.join(repr(s) for s in g.spellings)})"
            )
    if report.applied:
        lines.append(f"{_TAG}: deleted {report.deleted} object(s).")
    else:
        lines.append(
            f"{_TAG}: DRY RUN - nothing was changed. Re-run with --apply to "
            f"delete."
        )
    return lines


def _resolve_project_root(cli_root: str | None) -> Path:
    """``--project-root`` > ``$KG_SYNC_PROJECT_ROOT`` (set by the wrappers,
    non-leaking) > legacy ``$KG_BASE_DIR`` > cwd."""
    for candidate in (
        cli_root,
        os.environ.get("KG_SYNC_PROJECT_ROOT", "").strip() or None,
        os.environ.get("KG_BASE_DIR", "").strip() or None,
    ):
        if candidate:
            return Path(candidate)
    return Path.cwd()


def _resolve_collection(cli_collection: str | None, project_root: Path) -> str:
    """``--collection`` > hub-resolved per-project KG collection > env.

    Deliberately routes through the SHARED resolver
    (``vco_lib.project_config.resolve``) rather than re-deriving the
    precedence — same channel ``sync_knowledge_graph.py`` uses.
    """
    if cli_collection:
        return cli_collection
    if not os.environ.get("VCT_DISABLE_HUB_RESOLVER"):
        try:
            from vco_lib.project_config import resolve as _resolve
            name = _resolve(project_root).kg_collection
            if name:
                return str(name)
        except Exception:
            pass
    return os.environ.get("KG_COLLECTION", "").strip()


def main(argv: Sequence[str] | None = None, backend: Any = None) -> int:
    parser = argparse.ArgumentParser(
        prog=_TAG,
        description=(
            "Reconcile duplicate objects in a project's KG collection "
            "(one object per file_path + chunk_num). Dry run by default."
        ),
    )
    parser.add_argument("--collection", default=None,
                        help="KG collection name (default: hub-resolved, "
                             "else $KG_COLLECTION)")
    parser.add_argument("--project-root", default=None,
                        help="project root used to resolve the collection "
                             "(default: $KG_SYNC_PROJECT_ROOT, else cwd)")
    parser.add_argument("--weaviate-url", default=None,
                        help="Weaviate URL (default: $WEAVIATE_URL)")
    parser.add_argument("--apply", action="store_true",
                        help="actually delete the duplicates (destructive; "
                             "omit for a dry run)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    project_root = _resolve_project_root(args.project_root)
    collection = _resolve_collection(args.collection, project_root)
    if not collection:
        print(
            f"{_TAG}: ERROR - collection_unresolved: no KG collection could "
            f"be resolved for {project_root}. Pass --collection NAME or set "
            f"$KG_COLLECTION.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    owns_backend = backend is None
    if owns_backend:
        from vco_lib.weaviate_helpers import weaviate_url_default
        backend = WeaviateBackend(args.weaviate_url or weaviate_url_default())
    try:
        report = reconcile(backend, collection, apply=args.apply)
    finally:
        if owns_backend:
            try:
                backend.close()
            except Exception:
                pass

    stream = sys.stderr if report.status == "refused" else sys.stdout
    for line in format_report(report):
        print(line, file=stream)
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover - exercised via the wrappers
    sys.exit(main())
