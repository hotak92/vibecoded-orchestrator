# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Source-scoped code-graph row removal (v0.2.105).

Everything here DELETES or counts rows by ``project_source`` — which root
(the project folder, or one extra code folder) a row was walked from:

* :func:`prune_orphan_interactions` — the targeted remedy behind the
  ``codegraph_orphan_interactions_pending`` entry (never a whole-walk
  stale prune; the 2026-10-29 incident deleted 33,464 extra-folder rows
  that way).
* :func:`count_rows_for_project_source` — the lost-extras detection's count.
* :func:`purge_rows_for_project_source` / :func:`purge_gone_extra_paths` —
  remove the rows of an extra code folder the owner removed or disabled in
  the launcher, and the rows of a folder the project itself was MOVED away
  from, and ONLY of such a folder: the hub must POSITIVELY report it gone
  (``code_graph_extra_paths_gone`` — a disabled row, or a removed row with
  its ``codegraph_extra_path_removed`` audit entry — or
  ``code_graph_moved_from_sources``, the ``src`` of a completed move in
  ``project_moves``). An enabled list the hub emptied on a DB error, an old
  hub, or a hub outage purges nothing; nor do a moved-from root's rows go
  before the current folder holds rows of its own.

Split out of ``vco_lib/codegraph_resync.py`` (the file-size rule); the
shared Weaviate plumbing (client, collection open, the tokenization-safe
delete primitive) stays there and is reached through the module object, so
one patch point serves both modules.

CLI (the launcher's Remove/Disable flow calls it — rule A, one home)::

    python -m vco_lib.codegraph_source_purge --project-root DIR [--force] --json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

from vco_lib import codegraph_resync as _cr

logger = logging.getLogger(__name__)

__all__ = [
    "SKIP_PREFIX",
    "count_rows_for_project_source",
    "main",
    "prune_orphan_interactions",
    "purge_gone_extra_paths",
    "purge_rows_for_project_source",
]


def _source_filter(src: str):
    """Candidate-selection filter for one ``project_source`` (narrowing only:
    ``delete_file_rows_exact`` re-checks every row in Python). ``None`` when
    the client classes are unavailable — the caller then full-scans."""
    try:
        from weaviate.classes.query import Filter

        return Filter.by_property("project_source").equal(src)
    except Exception:  # noqa: BLE001 — no narrowing, still exact
        return None


def _posix_form(src: str) -> str:
    """The path form code-graph rows are stamped with
    (``source_root.as_posix()`` — ``vco_lib/codegraph_resync.py``). The
    launcher's move ledger holds the RAW ``folder_path`` string, which on
    Windows is ``C:\\x\\proj`` while the rows say ``C:/x/proj``: an exact
    string match between the two silently deletes nothing there (review
    A-LOW-1). Every source spelling in this module is compared in this one
    form."""
    return str(src).replace("\\", "/")


def prune_orphan_interactions(
    project_name: str,
    *,
    repo_root: Optional[Path] = None,
    client=None,
    weaviate_url: Optional[str] = None,
    grpc_port: Optional[int] = None,
) -> dict:
    """v0.2.105 (prune lane): delete ONLY the provably-dead unanchored
    CodeInteraction rows — the targeted remedy behind the
    ``codegraph_orphan_interactions_pending`` entry's printed command.

    NOT a whole-collection stale prune. The pre-v0.2.105 remedy was
    ``--run-resync --prune-stale``, whose walk covered the repo root only
    and therefore deleted every ENABLED extra code folder's rows with it
    (the 2026-10-29 incident: 33,464 rows pruned, 727 orphans expected).
    This pass judges each row through the ONE predicate
    (:func:`~vco_lib.codegraph_resync.is_provable_codegraph_orphan`, ``repo_root`` given — a row whose
    non-empty ``project_source`` is not a primary source is never touched)
    and deletes NOTHING else: no visited-set semantics, no file-anchored
    rows, no extras. Deletion is DEFERRED until the enumeration completes
    (a mid-scan failure authorises nothing — the same rule as
    ``codegraph_prune.prune_collection``).

    Returns ``{"counted": int|None, "deleted": int, "failures": int}``;
    ``counted`` is None when the scan was undeterminable (in which case
    nothing was deleted). On a complete scan with zero failures the paired
    clear runs
    (:func:`~vco_lib.codegraph_resync.clear_orphan_interactions_deferral_if_zero`) so
    running the printed command also clears its own ledger entry.
    Never raises.
    """
    out = {"counted": None, "deleted": 0, "failures": 0}
    if not project_name:
        return out
    prefix = _cr._collection_prefix(project_name)
    if prefix is None:
        return out
    with _cr._probe_client(client, weaviate_url, grpc_port) as client:
        if client is None:
            return out
        coll, status = _cr._open_collection(
            client, f"{prefix}_CodeInteraction", "orphan-interaction prune",
        )
        if status == _cr._COLL_ABSENT:
            out["counted"] = 0
            return out
        if status == _cr._COLL_ERROR:
            return out
        read_props = ["file_path", "project"]
        try:
            if "project_source" in {
                p.name for p in coll.config.get().properties
            }:
                read_props.append("project_source")
        except Exception:  # noqa: BLE001 — probe best-effort; skip the prop
            pass
        candidates: list = []
        try:
            for obj in coll.iterator(return_properties=read_props):
                props = getattr(obj, "properties", None) or {}
                if _cr.is_provable_codegraph_orphan(
                    coll, str(obj.uuid), props,
                    project_name=project_name, repo_root=repo_root,
                ):
                    candidates.append(str(obj.uuid))
        except Exception as exc:  # noqa: BLE001 — undeterminable ⇒ delete nothing
            logger.warning(
                "codegraph orphan-interaction prune: scan failed: %s", exc
            )
            return out
        out["counted"] = len(candidates)
        for uid in candidates:
            try:
                coll.data.delete_by_id(uuid=uid)
                out["deleted"] += 1
                logger.info(
                    "Pruned orphan %s from %s: no file_path and no "
                    "resolvable source reference", uid, coll.name,
                )
            except Exception as exc:  # noqa: BLE001 — per-row soft-fail
                out["failures"] += 1
                logger.warning(
                    "Failed to prune orphan %s from %s: %s", uid, coll.name, exc,
                )
        if not out["failures"]:
            # Paired clear INSIDE the probe-client scope (a built client is
            # closed by it): a positive zero resolves the ledger entry, so
            # the printed command clears its own condition.
            try:
                _cr.clear_orphan_interactions_deferral_if_zero(
                    project_name, repo_root=repo_root, client=client,
                )
            except Exception as exc:  # noqa: BLE001 — clear never gates the prune
                logger.warning(
                    "codegraph orphan-interaction prune: clear check failed: %s",
                    exc,
                )
    return out


def count_rows_for_project_source(
    project_name: str,
    source: str,
    *,
    bases: "tuple" = ("CodeModule",),
    client=None,
    weaviate_url: Optional[str] = None,
    grpc_port: Optional[int] = None,
) -> Optional[int]:
    """v0.2.105 (prune lane): how many rows of ``project_name``'s collections
    carry ``project_source == source`` (EXACT string, compared in Python).

    The missing-extra-folder detection asks this ("an enabled extra has 0
    rows of its own in the prefix"). Cheap filtered aggregate first
    (``project_source`` is TEXT — an ``equal`` filter matches on the token
    set, so a pathological sibling path could over-count, never under-count;
    the exact per-row compare below is the fallback when the filter cannot
    run). Returns ``None`` when undeterminable — the caller must NOT read
    that as zero (a false "0 rows" would trigger an unnecessary re-sync; a
    false positive merely skips one).
    """
    src = str(source or "").strip()
    if not project_name or not src:
        return 0
    prefix = _cr._collection_prefix(project_name)
    if prefix is None:
        return None
    total = 0
    with _cr._probe_client(client, weaviate_url, grpc_port) as client:
        if client is None:
            return None
        for base in bases:
            coll_name = f"{prefix}_{base}"
            coll, status = _cr._open_collection(client, coll_name, "source count")
            if status == _cr._COLL_ABSENT:
                continue  # a class that does not exist holds no rows
            if status == _cr._COLL_ERROR:
                return None
            n: Optional[int] = None
            try:
                from weaviate.classes.query import Filter

                agg = coll.aggregate.over_all(
                    filters=Filter.by_property("project_source").equal(src),
                    total_count=True,
                )
                _tc = getattr(agg, "total_count", None)
                if _tc is not None:
                    n = int(_tc)
            except Exception:  # noqa: BLE001 — fall to the exact scan
                n = None
            if n is None:
                # Exact per-row compare (the tokenization-safe rule — the
                # same one every delete here follows).
                try:
                    n = sum(
                        1 for obj in coll.iterator(
                            return_properties=["project_source"])
                        if str((getattr(obj, "properties", None) or {})
                               .get("project_source") or "").strip() == src
                    )
                except Exception as exc:  # noqa: BLE001 — undeterminable
                    logger.warning(
                        "source count on %s failed: %s", coll_name, exc)
                    return None
            total += n
        return total


def purge_rows_for_project_source(
    project_name: str,
    source: str,
    *,
    client=None,
    weaviate_url: Optional[str] = None,
    grpc_port: Optional[int] = None,
) -> dict:
    """v0.2.105 (prune lane): delete every row with ``project_source ==
    source`` from ``project_name``'s five code-graph collections.

    The removed/disabled-extra-folder purge, called ONLY by
    :func:`purge_gone_extra_paths` for a source the hub POSITIVELY reports
    as gone. Scoped by the one tokenization-safe delete primitive
    (:func:`~vco_lib.codegraph_resync.delete_file_rows_exact` — the raw
    ``project_source`` is read back per row and compared in Python; the
    ``project_source`` filter below only NARROWS which rows are read back,
    it never decides a delete). Returns ``{"counts": {coll: deleted},
    "failures": N}``; per-collection failures are logged and counted
    (soft-fail — a purge failure must never fail the refresh; the caller
    does not record the sweep as done while ``failures > 0``, so the next
    refresh re-purges).
    """
    report: dict = {"counts": {}, "failures": 0}
    src = str(source or "").strip()
    if not project_name or not src:
        return report
    prefix = _cr._collection_prefix(project_name)
    if prefix is None:
        return report
    # Both spellings, because the rows are POSIX-stamped while the caller's
    # ledger string may be the raw Windows form (``_posix_form``): one of the
    # two matches, the other deletes nothing (review A-LOW-1).
    spellings = [src]
    posix = _posix_form(src)
    if posix != src:
        spellings.append(posix)
    with _cr._probe_client(client, weaviate_url, grpc_port) as client:
        if client is None:
            return report
        for base in _cr._CODEGRAPH_BASES:
            coll_name = f"{prefix}_{base}"
            coll, status = _cr._open_collection(client, coll_name, "source purge")
            if status == _cr._COLL_ABSENT:
                continue
            if status == _cr._COLL_ERROR:
                report["failures"] += 1
                continue
            for spelling in spellings:
                try:
                    deleted, failures = _cr.delete_file_rows_exact(
                        coll, _cr._PRUNE_PATH_PROP.get(base, "file_path"),
                        lambda _raw_path, _props: True,
                        project_source=spelling,
                        log_prefix="gone-extra purge",
                        narrow_filter=_source_filter(spelling),
                    )
                except Exception as exc:  # noqa: BLE001 — per-collection soft-fail
                    logger.warning("source purge: %s failed: %s", coll_name, exc)
                    report["failures"] += 1
                    continue
                if deleted:
                    report["counts"][coll_name] = (
                        report["counts"].get(coll_name, 0) + deleted)
                report["failures"] += failures
    return report


#: Every "nothing was purged because nothing is positively known" log line
#: starts with this; the CLI reports the first one as ``refused`` (the
#: launcher's toast then says the rows were KEPT, not that they were pruned).
SKIP_PREFIX = "purge-gone: skip"


def _stamp_path(state_dir: Path, prefix: str) -> Path:
    from vco_lib.codegraph_extras_refresh import path_key

    return state_dir / f"codegraph_extra_keep_{path_key(prefix)}.json"


def _destination_has_rows(
    prefix: str,
    primary: Iterable[str],
    count_rows: "Callable[[str, str], Optional[int]]",
) -> bool:
    """Does the project's CURRENT folder hold code-graph rows yet?

    The gate in front of the moved-from purge (v0.2.105 fix round): after a
    move the old root's rows may be the only copy until the rebuild finishes
    (a row's UUID mixes ``project_source``, so a walk of the new root writes
    NEW rows rather than rewriting the old ones). Deleting the old rows
    before that would leave the project with an empty graph.

    TRUE only on a POSITIVE count > 0 for one of the root's own source forms.
    An undeterminable count (``None`` — Weaviate unreachable) is NOT "empty"
    and NOT "has rows": the callers' rule is "when unsure, keep the rows", so
    it counts as False here and the move's purge waits for a later sweep.
    """
    for src in sorted(s for s in primary if s):
        try:
            n = count_rows(prefix, src)
        except Exception:  # noqa: BLE001 — a failed count proves nothing
            continue
        if n is not None and int(n) > 0:
            return True
    return False


def purge_gone_extra_paths(
    project_root: str,
    state_dir: Path,
    *,
    resolve: Optional[Callable[[str], object]] = None,
    purge: "Optional[Callable[[str, str], dict]]" = None,
    log: Optional[Callable[[str], None]] = None,
    force: bool = False,
    count_rows: "Optional[Callable[[str, str], Optional[int]]]" = None,
) -> dict[str, dict]:
    """Purge the rows of extra code folders the owner REMOVED or DISABLED,
    and of the folders this project was POSITIVELY moved away from.

    Positive knowledge only. A folder is purged when the hub lists it in
    ``code_graph_extra_paths_gone`` (a disabled launcher.db row, or a row
    that is gone and has a ``codegraph_extra_path_removed`` audit entry) and
    it is neither the primary root nor a currently-enabled extra. Everything
    else is kept — in particular when the hub could not read its DB (it then
    empties the enabled list AND reports ``gone`` as unknown), when the hub
    is down or too old to send the field, and for any stored source nobody
    registered as removed. When unsure, the rows stay.

    v0.2.105 fix round (review NIT): the folders the project MOVED away from
    join the same sweep. A move is recorded in launcher.db's ``project_moves``
    ledger — the durable record that the project used to live elsewhere — and
    the hub serves those ``src`` paths as ``code_graph_moved_from_sources``.
    Every code-graph row is stamped with the absolute root it was walked from
    (mixed into its UUID), so after a move the old root's rows are a second,
    stale copy of the same files that nothing rewrites and ``--prune-stale``
    may not judge. They are removed only once the project's CURRENT root
    really holds rows (``count_rows`` > 0): a move must never empty a graph
    that has not been rebuilt yet. ``None`` from the hub (unknown) purges
    nothing, exactly like ``gone``.

    Cost gate: a stamp under ``state_dir`` records the (keep, gone, moved)
    sets of the last COMPLETE sweep; an unchanged triple is one file read. The
    stamp is written only when every purge reported ``failures == 0`` — a
    partial purge leaves it untouched, so the next refresh retries. ``force``
    ignores the stamp (the launcher's Remove/Disable flow: the user just
    acted). Returns ``{source: report}``; never raises.
    """
    if resolve is None:
        from vco_lib import project_config

        resolve = project_config.resolve
    if purge is None:
        purge = purge_rows_for_project_source
    if count_rows is None:
        count_rows = count_rows_for_project_source
    if log is None:
        def _nolog(_msg: str) -> None:  # callers pass the RunLog
            return None

        log = _nolog
    outcomes: dict[str, dict] = {}
    try:
        cfg = resolve(project_root)
    except Exception as exc:  # noqa: BLE001 — hub down: nothing is known
        log(f"{SKIP_PREFIX}: the project's configuration is unavailable "
            f"(hub down?) — nothing purged ({exc})")
        return outcomes
    prefix = getattr(cfg, "code_graph_collection_prefix", "") or ""
    if not prefix:
        return outcomes
    gone_raw = getattr(cfg, "code_graph_extra_paths_gone", None)
    if gone_raw is None:
        log(
            f"{SKIP_PREFIX}: the hub could not say which code folders were "
            "removed (its database read failed, or it is older than this "
            "VCO) — nothing purged"
        )
        return outcomes
    enabled = {
        _posix_form((getattr(e, "path", "") or "").strip())
        for e in getattr(cfg, "code_graph_extra_paths", ())
        if getattr(e, "enabled", False)
    } - {""}
    try:
        primary = set(_cr.primary_sources_for(Path(project_root)) or set())
    except Exception:  # noqa: BLE001 — unknown primary forms: keep everything
        log(f"{SKIP_PREFIX}: the project folder's own row forms are "
            "unknown — nothing purged")
        return outcomes
    keep = sorted(primary | enabled)
    targets = sorted({_posix_form(str(g).strip()) for g in gone_raw}
                     - {""} - set(keep))
    # v0.2.105 fix round: the folders this project POSITIVELY moved away
    # from. Same "positive knowledge only" rule: an unknown list (a hub that
    # cannot read its DB, or an older hub) purges nothing. The current root is
    # in `keep`, so a move BACK to an earlier folder keeps that folder's rows.
    moved: set[str] = set()
    moved_raw = getattr(cfg, "code_graph_moved_from_sources", None)
    if moved_raw is None:
        # Plain line, NOT `SKIP_PREFIX`: the gone/extra part of the sweep is
        # still authorized above, so this is a partial omission, not the
        # "nothing was purged because nothing is known" refusal the CLI
        # reports as `refused`.
        log(
            "purge-gone: moved-from unknown — the hub could not say which "
            "code folders this project was moved away from (its database read "
            "failed, or it is older than this VCO); their rows are kept"
        )
    else:
        moved = {_posix_form(str(m).strip()) for m in moved_raw} - {""} - set(keep)
        if moved and not _destination_has_rows(prefix, primary, count_rows):
            log(
                "purge-gone: moved-from deferred — the current project folder "
                f"holds no code-graph rows yet, so the rows stamped with "
                f"{len(moved)} old folder(s) are kept until the rebuild lands "
                "(a move must never empty a graph that has not been rebuilt)"
            )
            moved = set()
    reason_for = {
        src: ("extra code folder removed or disabled by the owner")
        for src in targets
    }
    for src in moved:
        reason_for[src] = "the project was moved away from this folder"
    sweep = sorted(set(targets) | moved)
    stamp = _stamp_path(state_dir, prefix)
    key = {"keep": keep, "gone": targets, "moved": sorted(moved)}
    if not force:
        try:
            if stamp.exists():
                prev = json.loads(stamp.read_text(encoding="utf-8"))
                if prev == key:
                    return outcomes  # nothing changed since the last sweep
        except Exception:  # noqa: BLE001 — an unreadable stamp sweeps again
            pass
    complete = True
    for src in sweep:
        try:
            report = purge(prefix, src)
        except Exception as exc:  # noqa: BLE001 — one source never stops the rest
            report = {"counts": {}, "failures": 1, "error": repr(exc)}
        outcomes[src] = report
        failures = int(report.get("failures", 0) or 0)
        if failures:
            complete = False
        total = sum((report.get("counts") or {}).values())
        log(
            f"purge-gone: source={src} prefix={prefix} "
            f"deleted={total} failures={failures} "
            f"({reason_for.get(src, 'positive knowledge from the hub')})"
        )
    if not complete:
        log("purge-gone: partial purge — the sweep stays owed and re-runs "
            "on the next refresh")
        return outcomes
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        stamp.write_text(json.dumps(key), encoding="utf-8")
    except Exception:  # noqa: BLE001 — an unwritable stamp only re-sweeps
        pass
    return outcomes


def main(argv: "Optional[list[str]]" = None) -> int:
    """``python -m vco_lib.codegraph_source_purge --project-root DIR``.

    Prints ONE JSON object (``ok``, ``purged`` = ``{source: deleted}``,
    ``failures``, ``refused`` = why nothing was purged or ``null``, ``log``). Exit 0 whenever the sweep ran — a refusal
    (nothing positively removed) is a normal outcome, not an error.
    """
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.codegraph_source_purge",
        description=(
            "Remove the code-graph rows of extra code folders the owner "
            "removed or disabled (positive knowledge only)."
        ),
    )
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--force", action="store_true",
                        help="ignore the last-sweep stamp")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.project_root)
    state_dir = Path(args.state_dir) if args.state_dir else root / ".claude" / "state"
    lines: list[str] = []
    outcomes = purge_gone_extra_paths(
        str(root), state_dir, log=lines.append, force=args.force)
    payload = {
        "ok": True,
        "purged": {
            src: sum((rep.get("counts") or {}).values())
            for src, rep in outcomes.items()
        },
        "failures": sum(int(rep.get("failures", 0) or 0)
                        for rep in outcomes.values()),
        "refused": next(
            (line[len(SKIP_PREFIX) + 2:] for line in lines
             if line.startswith(SKIP_PREFIX)), None),
        "log": lines,
    }
    if args.json:
        print(json.dumps(payload))
    else:
        for line in lines:
            print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entry
    sys.exit(main())
