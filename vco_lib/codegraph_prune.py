# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Stale-row pruning for the code-graph analyzer (v0.2.82 extraction).

Why this module exists
----------------------
``analyze_code_graph.py --prune-stale`` deletes every project row whose UUID
was not "visited" during the current run. The 2026-07-15 incident showed the
visited-only semantics are DESTRUCTIVE when combined with the per-file
unchanged gate: a fully-current project short-circuits every file BEFORE any
write, the visited set stays empty, and the prune deleted two projects'
entire code graphs (two field projects: 1,876 objects → 0 and 436 → 0).

The fix threads a ``preserve_paths`` set (files the dispatcher DISCOVERED on
disk but did not re-walk — unchanged-skip / parse failure) into the
per-collection prune: rows anchored to those files are alive, not stale, and
are exempt from deletion. (A minified-skipped file is in the set too, but since
v0.2.102 S16-3 its rows are pruned on purpose at the end of the walk — the skip
is a policy ruling that the file is out of the graph — so there is nothing left
to exempt.)

Extracted from the analyzer monolith (P2f line-count ratchet:
``tests/test_analyze_code_graph_ratchet.py``) — the analyzer keeps a thin
``_prune_collection`` shim that delegates here.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


def prune_collection(
    collection: Any,
    visited_uuids: Set[str],
    *,
    project_name: str,
    canonical_lang_id: Callable[[Any], str],
    language_scope: str = "",
    preserve_paths: Optional[Set[Any]] = None,
    preserve_sources: Optional[Set[str]] = None,
    orphan_resolver: Optional[Callable[[Any, Any], Optional[str]]] = None,
) -> Tuple[int, int]:
    """Delete every object in ``collection`` whose project matches
    ``project_name`` AND whose UUID is not in ``visited_uuids``.

    v0.2.18 (Plan C): when ``language_scope`` is a non-empty canonical
    language ID, additionally filter by ``language == language_scope``
    (case-insensitive, after ``canonical_lang_id`` normalisation so legacy
    mixed-case rows like ``"Python"`` are recognised as matching
    ``"python"``). Rows with no ``language`` property are treated as
    unknown-language and PRESERVED — they predate the v0.2.18 schema
    migration and the next full re-analyze (no ``--language``) will
    repopulate them.

    v0.2.82 (2026-07-15 wipe incident): ``preserve_paths`` is the set of
    repo-relative POSIX paths the dispatcher discovered on disk but did
    NOT re-walk this run (per-file unchanged-skip, parse failure). Rows
    anchored to those files are ALIVE — nothing visited them only because
    the walk short-circuited — so they are exempt from deletion. (Minified
    skips also land here, but their rows are pruned deliberately by the
    analyzer's end-of-walk policy prune — v0.2.102 S16-3.) Anchor property is ``path`` for CodeModule and ``file_path``
    for the other collections; when the collection's schema lacks the
    anchor (legacy pre-migration classes) OR a row has no anchor value,
    the row is DELETED only when ``preserve_paths`` is empty (i.e. a
    genuine full re-walk where the old visited-only semantics are
    trustworthy) and PRESERVED otherwise — conservative default: without
    provenance we must not guess "stale". Deletion of true orphans (files
    gone from disk) is additionally owned by the deleted-primary sweep in
    the analyzer's ``_build_stale_file_set``.

    v0.2.102 W6-CGD (D-3): a ``preserve_paths`` entry is a ``(source, path)``
    tuple — the walk records which ROOT skipped the file
    (:func:`expand_preserve_entries`), and only rows whose stored
    ``project_source`` and anchor both match are exempt. Keyed by path alone,
    a copy skipped in the project shielded the same path's rows of an extra
    folder (and the reverse), so removing or disabling the folder left its
    rows behind. A bare ``str`` entry still means "this path under any
    source" (callers that cannot name the root). When the collection's schema
    has no ``project_source`` (pre-v0.2.47) every row reads as source ``""``.
    ``preserve_sources``: rows stamped with one of these roots are never
    deleted by this run (an extra root the analyzer refused to walk because it
    is nested inside the project — not walking it must not delete its rows).

    Why filter on the ``project`` property as well as the collection name:
    a per-project collection like ``MyProject_CodeFunction`` should always
    belong to a single project, but defensive in case the schema ever
    permits cross-project sharing. The double-filter is essentially free
    (no extra query roundtrip beyond the initial enumerate).

    v0.2.73 (C-11 / RT-3): returns ``(pruned, failures)`` so callers can
    flip the build status success→partial when deletes fail. Before this
    change a per-row ``delete_by_id`` failure only ``logger.warning``'d and
    was invisible — a build with hundreds of Weaviate-500 prune failures
    (stale per-shard prop-length tracker state on a pre-chunking
    collection) reported ``success`` over silently-stale data. Now every
    failure increments a counter that propagates to the run's exit status
    and the machine-readable ``PRUNE_FAILURES=N`` summary line.

    v0.2.103 (defect 7 — orphan reaping): ``orphan_resolver`` is the
    FULL-WALK-only provability hook for anchor-less rows. A row whose anchor
    value is empty while skips are in play used to be preserved
    unconditionally — correct for skipped files' rows, but it also made
    pre-v0.2.82 orphan CodeInteraction rows (no ``file_path``, no resolvable
    source reference) immortal: every real walk has skips, so they survived
    every prune. When a resolver is supplied, an anchor-less row is judged
    by it BEFORE the conservative preserve: a resolved path → preserved
    (the anchor backfill converges it); ``""`` (positively unresolvable) →
    PROVABLY orphaned → deleted; ``None``/raise (unknown) → preserved.
    Deletion is DEFERRED until the enumeration completes — an enumeration
    failure mid-scan deletes nothing (conservative default). The caller
    supplies the resolver only for a full, unscoped ``--prune-stale`` walk
    and only for the collection that owns the orphan class.
    """
    pruned = 0
    failures = 0
    preserved = 0
    preserve_paths = preserve_paths or set()
    preserve_sources = preserve_sources or set()
    keyed = {e for e in preserve_paths if isinstance(e, tuple)}
    bare = {e for e in preserve_paths if not isinstance(e, tuple)}
    orphan_candidates: List[str] = []

    # v0.2.82 — resolve the file-anchor property for this collection:
    # CodeModule keys files on `path`; every other file-anchored
    # collection mirrors it as `file_path`. Probe the schema for the
    # anchor's presence so a legacy pre-migration collection (anchor
    # prop absent) doesn't 422 the enumerate — absent anchor degrades
    # to the conservative no-provenance branch below.
    anchor_prop: Optional[str] = (
        "path" if collection.name.endswith("_CodeModule") else "file_path"
    )
    has_source = False
    try:
        _cfg_props = {p.name for p in collection.config.get().properties}
        if anchor_prop not in _cfg_props:
            anchor_prop = None
        has_source = "project_source" in _cfg_props
    except Exception:  # noqa: BLE001 — config probe is best-effort
        anchor_prop = None

    # Read `language` only when needed so a missing-property collection
    # (pre-migration) doesn't 422 the enumerate. Weaviate returns None
    # for missing-on-row props which we treat as "unknown language".
    return_props = ["project"]
    if anchor_prop:
        return_props.append(anchor_prop)
    if language_scope:
        return_props.append("language")
    if has_source:
        return_props.append("project_source")

    try:
        for obj in collection.iterator(
            return_properties=return_props,
        ):
            props = obj.properties or {}
            obj_project = props.get("project")
            # Only consider objects belonging to this project. Foreign-
            # project rows (shouldn't exist in per-project collections,
            # but defensive) are left alone.
            if obj_project not in (None, "", project_name):
                continue

            # Plan C: language-scoped filter. Rows without a language
            # property (pre-v0.2.18 data) are PRESERVED — they need a
            # full re-analyze to repopulate the field. Rows with a
            # language other than the scope are out-of-scope this run.
            if language_scope:
                row_lang = canonical_lang_id(props.get("language"))
                if not row_lang:
                    # Unknown / pre-migration row → preserve.
                    continue
                if row_lang != language_scope:
                    # Different-language row → preserve (this is the
                    # entire point of language-scoped prune).
                    continue

            if str(obj.uuid) in visited_uuids:
                continue
            row_source = str(props.get("project_source") or "").strip()
            if row_source and row_source in preserve_sources:
                preserved += 1
                continue

            # v0.2.82 — skipped-but-present exemption (see docstring).
            # Normalise `\` → `/` before membership: stored anchors are
            # POSIX by contract, but Windows-shaped legacy rows exist
            # (same lesson as the v0.2.81 manifest-separator incident).
            if anchor_prop is not None:
                row_anchor = (props.get(anchor_prop) or "").replace("\\", "/")
                if row_anchor:
                    if row_anchor in bare or (row_source, row_anchor) in keyed:
                        preserved += 1
                        continue
                elif preserve_paths:
                    # Anchored collection but this row has no value
                    # (legacy). With skips in play we lack provenance —
                    # preserve rather than guess, UNLESS the resolver can
                    # POSITIVELY prove the row orphaned (v0.2.103 defect 7).
                    if orphan_resolver is not None:
                        try:
                            resolved = orphan_resolver(collection, obj)
                        except Exception as exc:  # noqa: BLE001 — unknown → keep
                            logger.debug(
                                "orphan resolve on %s/%s failed: %s",
                                collection.name, obj.uuid, exc,
                            )
                            resolved = None
                        if resolved is not None:
                            if resolved:
                                # Anchored by reference: the anchor backfill
                                # PATCHes file_path from it — leave it to
                                # converge, never delete on this evidence.
                                preserved += 1
                                continue
                            # "" — positively unresolvable: a provable
                            # orphan. Deferred: deleted below ONLY after the
                            # enumeration completed (a mid-scan failure must
                            # authorise nothing).
                            orphan_candidates.append(str(obj.uuid))
                            continue
                    preserved += 1
                    continue
            elif preserve_paths:
                # Anchor-less collection (legacy schema) + skips in play:
                # cannot attribute rows to skipped files → preserve all
                # unvisited rows this run; a later full re-walk (no skips)
                # restores exact visited-only semantics.
                preserved += 1
                continue

            try:
                collection.data.delete_by_id(uuid=str(obj.uuid))
                pruned += 1
            except Exception as exc:
                # v0.2.73 (C-11 / RT-3): count the failure so it can flip
                # the build status. A recurring signature here is the
                # Weaviate-500 "subtract prop lengths: property not found"
                # on delete — stale per-shard prop-length tracker state on a
                # pre-chunking collection carried across Weaviate upgrades.
                # It is upstream shard state, NOT a VCO logic bug, so we
                # never auto-drop the collection; the consented
                # drop-and-rebuild path is surfaced in main() instead.
                failures += 1
                logger.warning(
                    f"Failed to prune {obj.uuid} from {collection.name}: {exc}"
                )
    except Exception as exc:
        # Iterating a freshly-created collection can fail if it
        # has no data yet; treat as zero-prune. This is an enumeration
        # failure (not a per-row delete failure) — we cannot tell how many
        # rows were owed, so we do NOT count it as a prune failure here;
        # a walk that produced zero visited UUIDs against a populated
        # collection is caught by higher-level insert-error accounting.
        logger.debug(f"Prune enumeration on {collection.name} failed: {exc}")
        # v0.2.103 (defect 7): enumeration failure = uncertainty → the
        # deferred orphan candidates are DROPPED, never deleted.
        orphan_candidates = []

    if orphan_candidates:
        # Deferred reaping (defect 7): every candidate was POSITIVELY proven
        # orphaned (no file_path AND no resolvable source reference) and the
        # enumeration completed. Each removal is logged (id + why); a delete
        # failure feeds the same failures counter as any stale-row delete.
        reaped = 0
        for uid in orphan_candidates:
            try:
                collection.data.delete_by_id(uuid=uid)
                pruned += 1
                reaped += 1
                logger.info(
                    "Pruned orphan %s from %s: no file_path and no "
                    "resolvable source reference", uid, collection.name,
                )
            except Exception as exc:  # noqa: BLE001 — per-row soft-fail
                failures += 1
                logger.warning(
                    "Failed to prune orphan %s from %s: %s",
                    uid, collection.name, exc,
                )
        print(
            f"🧹 Pruned {reaped} unanchored orphan row(s) from "
            f"{collection.name} (no file_path, no resolvable source "
            f"reference)"
        )

    if preserved:
        # v0.2.82 — no silent caps: say what was exempted and why.
        print(
            f"🛡️  Preserved {preserved} row(s) in {collection.name} "
            f"anchored to skipped-but-present files (not stale)"
        )
    return pruned, failures


def expand_preserve_entries(
    entries: Iterable[Any], *, primary_root: str, strict: bool,
) -> Set[Any]:
    """Turn the analyzer's ``discovered − walked`` set into prune-preserve keys.

    v0.2.102 W6-CGD (D-3). Each entry is ``(walk_source, rel)`` — the walked
    root's POSIX form and the file's path relative to it. It expands to one
    ``(source, rel)`` key per source form that root owns
    (:func:`vco_lib.codegraph_source_scope.preserve_keys`), with ``("", rel)``
    added only for the project's own root (``primary_root``, and never under
    ``strict`` = ``--as-extra-path``, whose root IS an extra folder). A bare
    ``str`` entry passes through unchanged (any-source preserve).
    """
    from vco_lib import codegraph_source_scope as _scope

    out: Set[Any] = set()
    owned: Dict[str, Set[str]] = {}
    for entry in entries:
        if not isinstance(entry, tuple):
            out.add(entry)
            continue
        src, rel = entry
        if src not in owned:
            owned[src] = _scope.walk_own_sources(src, src)
        extra = strict or src != primary_root
        out |= _scope.preserve_keys(owned[src], rel, extra_root=extra)
    return out


# ── v0.2.103 (plan item 24): per-collection prune-failure accounting ────────

def record_prune_failures(holder: Any, collection_name: str, failures: int) -> None:
    """Bump BOTH prune-failure ledgers on the analyzer instance.

    ``holder`` is the ``CodeGraphAnalyzer`` (duck-typed, ``Any`` — this module
    never imports the analyzer). ``holder._prune_failures`` is the pre-existing
    TOTAL accumulator (→ stats → ``PRUNE_FAILURES=N`` → success→partial);
    ``holder._prune_failure_collections`` (new) maps the collection names that
    actually had failures so the end-of-run remedy can name the REAL class
    instead of a hard-coded ``<Prefix>_CodeFunction`` (a real run had its
    failures in ``<Prefix>_CodeInteraction`` and the printed curl named the
    wrong collection). ``getattr``-defensive like the existing bump sites; an
    empty ``collection_name`` records the total only (producers that cannot
    attribute per collection).
    """
    if failures <= 0:
        return
    holder._prune_failures = int(getattr(holder, "_prune_failures", 0)) + failures
    if not collection_name:
        return
    per_coll: Dict[str, int] = dict(getattr(holder, "_prune_failure_collections", None) or {})
    per_coll[collection_name] = per_coll.get(collection_name, 0) + failures
    holder._prune_failure_collections = per_coll


#: The default in the remedy's ``${WEAVIATE_URL:-...}`` expansion (mirrors the
#: pre-v0.2.103 hard-coded line).
PRUNE_REMEDY_URL_DEFAULT = "http://localhost:8081"


def format_prune_remedy_lines(
    failing_collections: Optional[Iterable[str]],
    project_name: str,
) -> List[str]:
    """The drop-and-rebuild remedy lines for the PRUNE_FAILURES block.

    Plan item 24 (a printed command is shipped code): ONE curl line per
    collection that actually had a prune failure, with its REAL full class
    name (``<Prefix>_<Class>`` — the collection name IS the schema path), and
    ONE re-run line with the REAL ``--project`` name. When no per-collection
    attribution exists (a producer that could not attribute), the curl line
    keeps the placeholder form rather than guessing a class. The
    never-automatic / consent-flow wording lives at the call site.
    """
    names = sorted({n for n in (failing_collections or []) if n})
    if names:
        targets = [
            f'"${{WEAVIATE_URL:-{PRUNE_REMEDY_URL_DEFAULT}}}/v1/schema/{n}"'
            for n in names
        ]
    else:
        targets = [
            f'"${{WEAVIATE_URL:-{PRUNE_REMEDY_URL_DEFAULT}}}'
            '/v1/schema/<Prefix>_<Class>"'
        ]
    lines = [f"   curl -X DELETE {t}   # then re-run analysis" for t in targets]
    project = project_name or "<name>"
    lines.append(
        f"   .claude/scripts/code-graph-analyze . --project {project} --prune-stale"
    )
    return lines
