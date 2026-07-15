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
entire code graphs (MeetApp: 1,876 objects → 0; Instambul1860: 436 → 0).

The fix threads a ``preserve_paths`` set (files the dispatcher DISCOVERED on
disk but did not re-walk — unchanged-skip / minified-skip / parse failure)
into the per-collection prune: rows anchored to those files are alive, not
stale, and are exempt from deletion.

Extracted from the analyzer monolith (P2f line-count ratchet:
``tests/test_analyze_code_graph_ratchet.py``) — the analyzer keeps a thin
``_prune_collection`` shim that delegates here.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Set, Tuple

logger = logging.getLogger(__name__)


def prune_collection(
    collection: Any,
    visited_uuids: Set[str],
    *,
    project_name: str,
    canonical_lang_id: Callable[[Any], str],
    language_scope: str = "",
    preserve_paths: Optional[Set[str]] = None,
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
    NOT re-walk this run (per-file unchanged-skip, minified-skip, parse
    failure). Rows anchored to those files are ALIVE — nothing visited
    them only because the walk short-circuited — so they are exempt from
    deletion. Anchor property is ``path`` for CodeModule and ``file_path``
    for the other collections; when the collection's schema lacks the
    anchor (legacy pre-migration classes) OR a row has no anchor value,
    the row is DELETED only when ``preserve_paths`` is empty (i.e. a
    genuine full re-walk where the old visited-only semantics are
    trustworthy) and PRESERVED otherwise — conservative default: without
    provenance we must not guess "stale". Deletion of true orphans (files
    gone from disk) is additionally owned by the deleted-primary sweep in
    the analyzer's ``_build_stale_file_set``.

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
    """
    pruned = 0
    failures = 0
    preserved = 0
    preserve_paths = preserve_paths or set()

    # v0.2.82 — resolve the file-anchor property for this collection:
    # CodeModule keys files on `path`; every other file-anchored
    # collection mirrors it as `file_path`. Probe the schema for the
    # anchor's presence so a legacy pre-migration collection (anchor
    # prop absent) doesn't 422 the enumerate — absent anchor degrades
    # to the conservative no-provenance branch below.
    anchor_prop: Optional[str] = (
        "path" if collection.name.endswith("_CodeModule") else "file_path"
    )
    try:
        _cfg_props = {p.name for p in collection.config.get().properties}
        if anchor_prop not in _cfg_props:
            anchor_prop = None
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

            # v0.2.82 — skipped-but-present exemption (see docstring).
            # Normalise `\` → `/` before membership: stored anchors are
            # POSIX by contract, but Windows-shaped legacy rows exist
            # (same lesson as the v0.2.81 manifest-separator incident).
            if anchor_prop is not None:
                row_anchor = (props.get(anchor_prop) or "").replace("\\", "/")
                if row_anchor:
                    if row_anchor in preserve_paths:
                        preserved += 1
                        continue
                elif preserve_paths:
                    # Anchored collection but this row has no value
                    # (legacy). With skips in play we lack provenance —
                    # preserve rather than guess.
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

    if preserved:
        # v0.2.82 — no silent caps: say what was exempted and why.
        print(
            f"🛡️  Preserved {preserved} row(s) in {collection.name} "
            f"anchored to skipped-but-present files (not stale)"
        )
    return pruned, failures
