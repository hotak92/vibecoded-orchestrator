# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Foreign-row pruning for per-project KG/Development collections
(v0.2.89 §7.3 — BUG-3 damage repair, Fabio wave-2).

Why this module exists
----------------------
BUG 3 (kg-sync resolving the project root from a leaked ``KG_BASE_DIR``)
could embed ANOTHER project's files into this project's collections — e.g.
orchestrator ``docs/**`` rows inside ``Arzillibus_Development``. The BUG-3
fix (wave-2 P1) removes every path to the class; this module heals the
EXISTING damage: rows whose ``file_path`` does not exist on disk under the
project are stale/foreign and are deleted.

Guards (binding, from PLAN-fabio-wave2-2026-07-30 §7.3):

* Rows whose file EXISTS on disk are NEVER touched (even if content
  drifted — normal sync owns that).
* **Shared-identity skip** (the load-bearing guard): when the project's KG
  collection IS the shared collection (``kg_collection ==
  shared_kg_collection`` — the orchestrator root), the KG leg is skipped
  entirely. BUG-6 shared-scoped nodes from OTHER projects make
  "absent locally" NORMAL in the shared store — pruning there would
  destroy other projects' legitimate rows.
* The Development leg has no shared variant, but keeps the root skip for
  symmetry.
* ``file_path`` values that are absolute or contain ``..`` (after ``\\`` →
  ``/`` normalization) are skipped defensively — never deleted.
* Dry-run reports the would-be deletions without touching anything.
* One ``foreign_rows_pruned`` info deferral records the per-collection
  counts.

Enumeration follows the repo's established GraphQL convention
(``limit: 10000`` — Weaviate's QUERY_MAXIMUM_RESULTS default — with the
errors[]-before-data gate; see ``vco_lib.kg_sync.batch_query_content_hashes``).
Saturation is SAFE here (unlike visited-set pruning): the delete decision is
per-row, so a truncated view only means fewer rows examined this run — the
remainder is caught on a later run. The result marks ``saturated`` honestly.

CLI::

    python -m vco_lib.collection_repair --project <folder> [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from vco_lib import weaviate_helpers as _wh
from vco_lib.install_weaviate import _path_resolves_on_disk
from vco_lib.knowledge_residue import (
    project_settings_env,
    weaviate_reachable,
)

#: Deferral condition ID owned by this module.
CID_PRUNED = "foreign_rows_pruned"

#: Weaviate's QUERY_MAXIMUM_RESULTS default — the repo-wide enumeration cap.
QUERY_MAX_LIMIT = 10000

#: Batch-delete chunk size for the ContainsAny uuid filter.
_DELETE_CHUNK = 500

#: How many pruned paths the info deferral lists before truncating.
_DEFERRAL_PATHS_SHOWN = 10


def _http_request(
    method: str,
    url: str,
    *,
    body: Optional[dict] = None,
    timeout: float = 30.0,
) -> tuple[int, bytes]:
    """Module-level delegator to :func:`vco_lib.weaviate_helpers.http_request`
    (mock seam — same convention as ``project_init._http_request``)."""
    return _wh.http_request(method, url, body=body, timeout=timeout)


_ABS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _classify_file_path(fp: str) -> str:
    """Classify a stored ``file_path`` value for prune eligibility.

    Returns one of:
      * ``"defensive"`` — empty, absolute, UNC, or ``..``-bearing after
        normalization: NEVER deleted.
      * ``"relative"`` — a plain project-relative path, eligible for the
        on-disk existence check.
    """
    if not fp or not fp.strip():
        return "defensive"
    raw = fp.strip()
    norm = raw.replace("\\", "/")
    if norm.startswith("/") or norm.startswith("//"):
        return "defensive"
    if _ABS_DRIVE_RE.match(norm):
        return "defensive"
    if ".." in [p for p in norm.split("/") if p]:
        return "defensive"
    return "relative"


def _enumerate_rows(
    weaviate_url: str,
    collection: str,
) -> Optional[tuple[list[tuple[str, str]], bool]]:
    """Fetch ``(uuid, file_path)`` pairs for every row in *collection*.

    Returns ``(rows, saturated)`` or ``None`` on any transport / GraphQL
    failure (the caller skips the leg — no destructive action without an
    authoritative row list). The errors[] array is inspected BEFORE data is
    consumed (silent-zero-fallback lesson).
    """
    base = weaviate_url.rstrip("/")
    gql = {
        "query": (
            f"{{ Get {{ {collection}(limit: {QUERY_MAX_LIMIT}) "
            f"{{ _additional {{ id }} file_path }} }} }}"
        ),
    }
    try:
        status, body = _http_request(
            "POST", f"{base}/v1/graphql", body=gql, timeout=30.0,
        )
    except Exception:  # noqa: BLE001 — transport failure ⇒ skip leg
        return None
    if status != 200:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if payload.get("errors"):
        # A missing class surfaces here — treat as "nothing to prune"
        # ONLY when the error names an unresolved type; anything else is
        # an enumeration failure. Conservative: skip the leg either way.
        return None
    objects = (
        payload.get("data", {}).get("Get", {}).get(collection, [])
    ) or []
    rows: list[tuple[str, str]] = []
    for obj in objects:
        uid = ((obj or {}).get("_additional") or {}).get("id") or ""
        fp = (obj or {}).get("file_path")
        if uid:
            rows.append((str(uid), fp if isinstance(fp, str) else ""))
    return rows, len(objects) >= QUERY_MAX_LIMIT


def _batch_delete_uuids(
    weaviate_url: str, collection: str, uuids: list,
) -> tuple[int, list]:
    """Delete rows by uuid via ``DELETE /v1/batch/objects`` (ContainsAny +
    ``valueTextArray`` — the V46-A-verified shape). Chunked. Returns
    ``(deleted, errors)``; stops on transport loss."""
    base = weaviate_url.rstrip("/")
    deleted = 0
    errors: list[str] = []
    for i in range(0, len(uuids), _DELETE_CHUNK):
        chunk = uuids[i:i + _DELETE_CHUNK]
        try:
            status, body = _http_request(
                "DELETE",
                f"{base}/v1/batch/objects",
                body={
                    "match": {
                        "class": collection,
                        "where": {
                            "path": ["id"],
                            "operator": "ContainsAny",
                            "valueTextArray": chunk,
                        },
                    },
                    "output": "minimal",
                    "dryRun": False,
                },
                timeout=60.0,
            )
        except Exception as exc:  # noqa: BLE001 — transport gone mid-run
            errors.append(f"transport lost during batch delete: {exc}")
            break
        if status != 200:
            errors.append(
                f"batch delete → HTTP {status}: {body[:200]!r}"
            )
            continue
        try:
            payload = json.loads(body.decode("utf-8"))
            n = (payload.get("results") or {}).get("successful", 0)
            deleted += int(n) if isinstance(n, (int, float)) else 0
        except Exception:  # noqa: BLE001 — 200 with odd body still deleted
            pass
    return deleted, errors


def prune_foreign_rows(
    project_root: Path,
    collection: str,
    *,
    weaviate_url: Optional[str] = None,
    is_shared_identity: bool = False,
    dry_run: bool = False,
    log_event: Optional[Callable[..., None]] = None,
) -> dict:
    """Delete rows in *collection* whose ``file_path`` does not exist on disk
    under *project_root*.

    See the module docstring for the guard contract. Returns a
    JSON-serialisable dict::

        {
          "collection": str,
          "skipped": None | "<reason>",
          "rows": int,                 # rows enumerated
          "defensive_skipped": int,    # absolute / ``..`` / empty paths
          "stale_paths": [<fp>...],    # distinct foreign paths found
          "deleted": int,              # rows actually deleted (0 in dry-run)
          "saturated": bool,
          "dry_run": bool,
          "errors": [str...],
        }
    """
    project_root = Path(project_root).resolve()
    result: dict = {
        "collection": collection,
        "skipped": None,
        "rows": 0,
        "defensive_skipped": 0,
        "stale_paths": [],
        "deleted": 0,
        "saturated": False,
        "dry_run": bool(dry_run),
        "errors": [],
    }

    def _log(phase: str, detail: str, *, data: Any = None) -> None:
        if log_event is None:
            return
        try:
            log_event("4.bundle.foreign_rows", phase, detail, data=data)
        except TypeError:
            try:
                log_event("4.bundle.foreign_rows", phase, detail)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 — logging never breaks the prune
            pass

    if not collection or not str(collection).strip():
        result["skipped"] = "no collection name"
        return result
    collection = str(collection).strip()
    result["collection"] = collection

    # THE load-bearing guard (§7.3): in a shared-identity collection,
    # "file absent locally" is NORMAL — BUG-6 shared-scoped nodes from
    # other projects legitimately have no local file. Never prune there.
    if is_shared_identity:
        result["skipped"] = "shared-identity collection"
        _log("ok", f"prune skipped for {collection!r}: shared-identity "
                   "collection (other projects' shared-scoped rows live here)")
        return result

    url = (weaviate_url or _wh.weaviate_url_default()).rstrip("/")
    enumerated = _enumerate_rows(url, collection)
    if enumerated is None:
        result["skipped"] = "enumeration failed"
        _log("warn", f"prune skipped for {collection!r}: could not "
                     "enumerate rows (no destructive action without an "
                     "authoritative row list)")
        return result
    rows, saturated = enumerated
    result["rows"] = len(rows)
    result["saturated"] = saturated
    if saturated:
        _log("warn",
             f"{collection!r} hit the QUERY_MAXIMUM_RESULTS cap "
             f"({QUERY_MAX_LIMIT}); this run examines a truncated view — "
             "remaining rows are caught on a later run")

    stale_uuids: list[str] = []
    stale_paths: set = set()
    defensive = 0
    for uid, fp in rows:
        kind = _classify_file_path(fp)
        if kind == "defensive":
            defensive += 1
            continue
        norm = fp.strip().replace("\\", "/")
        if _path_resolves_on_disk(norm, project_root):
            continue  # file exists ⇒ NEVER touched
        stale_uuids.append(uid)
        stale_paths.add(norm)
    result["defensive_skipped"] = defensive
    result["stale_paths"] = sorted(stale_paths)

    if not stale_uuids:
        _log("ok", f"{collection!r}: no foreign rows "
                   f"({len(rows)} row(s) examined)")
        return result

    if dry_run:
        _log("ok", f"{collection!r} dry-run: {len(stale_uuids)} foreign "
                   f"row(s) across {len(stale_paths)} path(s) would be deleted")
        return result

    deleted, errors = _batch_delete_uuids(url, collection, stale_uuids)
    result["deleted"] = deleted
    result["errors"].extend(errors)
    _log("ok", f"{collection!r}: deleted {deleted} foreign row(s) "
               f"across {len(stale_paths)} path(s)",
         data={"deleted": deleted, "paths": len(stale_paths)})
    return result


def prune_foreign_rows_for_project(
    project_root: Path,
    *,
    weaviate_url: str,
    kg_collection: str,
    development_collection: str,
    shared_kg_collection: str,
    is_root_target: bool = False,
    dry_run: bool = False,
    emit_deferral: bool = True,
    log_event: Optional[Callable[..., None]] = None,
) -> dict:
    """Run both prune legs (KG + Development) for one project.

    Gate composition (§7.3):
      * KG leg skipped when ``kg_collection == shared_kg_collection``
        (case-insensitive — Weaviate class names cannot differ by case
        only, and case-insensitive equality is the more conservative
        reading).
      * Development leg skipped on a root target (symmetry — the root's
        docs are the orchestrator's own).
      * Both legs skipped when Weaviate is unreachable (bounded probe).

    Emits ONE ``foreign_rows_pruned`` info deferral when anything was
    deleted (per-collection counts in the entry body).
    """
    project_root = Path(project_root).resolve()
    kg = (kg_collection or "").strip()
    dev = (development_collection or "").strip()
    shared = (shared_kg_collection or "").strip()
    kg_shared_identity = bool(kg) and kg.lower() == shared.lower()

    result: dict = {
        "skipped": None,
        "dry_run": bool(dry_run),
        "legs": [],
        "total_deleted": 0,
    }

    if not weaviate_reachable(weaviate_url):
        result["skipped"] = "weaviate unreachable"
        return result

    legs: list[tuple[str, bool, str]] = []
    if kg:
        legs.append((
            kg,
            kg_shared_identity,
            "shared-identity collection" if kg_shared_identity else "",
        ))
    if dev:
        legs.append((
            dev,
            is_root_target,
            "root target" if is_root_target else "",
        ))

    for coll, skip, reason in legs:
        if skip:
            leg = {
                "collection": coll, "skipped": reason, "rows": 0,
                "defensive_skipped": 0, "stale_paths": [], "deleted": 0,
                "saturated": False, "dry_run": bool(dry_run), "errors": [],
            }
            result["legs"].append(leg)
            continue
        leg = prune_foreign_rows(
            project_root,
            coll,
            weaviate_url=weaviate_url,
            is_shared_identity=False,
            dry_run=dry_run,
            log_event=log_event,
        )
        result["legs"].append(leg)
        result["total_deleted"] += leg.get("deleted", 0)

    if result["total_deleted"] and not dry_run and emit_deferral:
        _emit_pruned_deferral(project_root, result["legs"])
    return result


def _emit_pruned_deferral(folder: Path, legs: list) -> None:
    """ONE ``foreign_rows_pruned`` info entry with per-collection counts."""
    from vco_lib import deferral_emit as _de
    from vco_lib.deferral_report import DeferralEntry

    lines: list[str] = []
    for leg in legs:
        if leg.get("skipped") or not leg.get("deleted"):
            continue
        shown = leg.get("stale_paths", [])[:_DEFERRAL_PATHS_SHOWN]
        paths = "\n".join(f"  - `{p}`" for p in shown)
        more = len(leg.get("stale_paths", [])) - len(shown)
        if more > 0:
            paths += f"\n  - … and {more} more path(s)"
        lines.append(
            f"- `{leg['collection']}`: {leg['deleted']} row(s) removed\n{paths}"
        )
    entry = DeferralEntry(
        condition_id=CID_PRUNED,
        title="Foreign rows pruned from project collections",
        detected=(
            "Rows whose `file_path` does not exist on disk under this "
            "project were removed (repair for the v0.2.88 kg-sync "
            "wrong-project bug):\n" + "\n".join(lines)
        ),
        why_deferred=(
            "Informational — no action required. Rows whose file exists on "
            "disk were never touched; shared-identity collections were "
            "skipped entirely (shared-scoped nodes from other projects "
            "legitimately have no local file)."
        ),
        command_to_apply=(
            "# Informational. Dismiss when read:\n"
            f"python -m vco_lib.project_init dismiss-deferral "
            f"--folder {str(folder)!r} --condition-id {CID_PRUNED}"
        ),
        severity="info",
    )
    _de.emit(folder, entry, log=None)


# ---------------------------------------------------------------------------
# CLI — python -m vco_lib.collection_repair --project <folder> [--dry-run]
# ---------------------------------------------------------------------------

def _resolve_project_context(folder: Path) -> dict:
    """Resolve weaviate_url + collection names for *folder*.

    Hub-first (:func:`vco_lib.project_config.resolve`); falls back to the
    project's ``.claude/settings.json`` env + the binding-first name
    resolver when the hub is unreachable / the project unregistered.
    """
    try:
        from vco_lib.project_config import resolve as _hub_resolve
        cfg = _hub_resolve(folder)
        return {
            "weaviate_url": cfg.weaviate_url,
            "kg_collection": cfg.kg_collection,
            "development_collection": cfg.development_collection,
            "shared_kg_collection": cfg.shared_kg_collection,
            "source": "hub",
        }
    except Exception:  # noqa: BLE001 — hub-down / unregistered ⇒ fallback
        pass

    from vco_lib.project_init import (
        _SHARED_KG_NAME,
        _resolve_bundle_collection_names_binding_first,
        _weaviate_url_default,
    )
    env = project_settings_env(folder)
    names = _resolve_bundle_collection_names_binding_first(
        folder.name or "Project", folder,
    )
    shared = env.get("SHARED_KG_COLLECTION")
    if not isinstance(shared, str):
        shared = _SHARED_KG_NAME
    weaviate_url = (env.get("WEAVIATE_URL") or "").strip() or _weaviate_url_default()
    return {
        "weaviate_url": weaviate_url,
        "kg_collection": names.get("kg_collection", ""),
        "development_collection": names.get("development_collection", ""),
        "shared_kg_collection": shared,
        "source": "settings/binding-first",
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.collection_repair",
        description=(
            "Prune foreign rows (file_path absent on disk) from a project's "
            "KG + Development collections. Repair tool for the v0.2.88 "
            "kg-sync wrong-project bug."
        ),
    )
    parser.add_argument("--project", required=True,
                        help="Project folder (the tree the collections mirror)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report would-be deletions; touch nothing")
    parser.add_argument("--weaviate-url", default=None,
                        help="Override the resolved Weaviate URL")
    parser.add_argument("--json", action="store_true",
                        help="Emit the result dict as JSON on stdout")
    args = parser.parse_args(argv)

    folder = Path(args.project).expanduser()
    if not folder.is_dir():
        print(f"error: --project {folder} is not a directory", file=sys.stderr)
        return 2
    folder = folder.resolve()

    ctx = _resolve_project_context(folder)
    if args.weaviate_url:
        ctx["weaviate_url"] = args.weaviate_url

    from vco_lib.project_init import (
        _find_orchestrator_root_from_module,
        _is_root_bundle_target,
    )
    try:
        is_root = _is_root_bundle_target(
            _find_orchestrator_root_from_module(), folder,
        )
    except Exception:  # noqa: BLE001 — cannot determine ⇒ conservative True
        is_root = True

    result = prune_foreign_rows_for_project(
        folder,
        weaviate_url=ctx["weaviate_url"],
        kg_collection=ctx["kg_collection"],
        development_collection=ctx["development_collection"],
        shared_kg_collection=ctx["shared_kg_collection"],
        is_root_target=is_root,
        dry_run=args.dry_run,
    )
    result["resolution_source"] = ctx["source"]

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result.get("skipped"):
            print(f"skipped: {result['skipped']}")
        for leg in result.get("legs", []):
            tag = "(dry-run) " if leg.get("dry_run") else ""
            if leg.get("skipped"):
                print(f"  {leg['collection']}: skipped — {leg['skipped']}")
            else:
                n = len(leg.get("stale_paths", []))
                print(
                    f"  {tag}{leg['collection']}: {leg.get('rows', 0)} row(s) "
                    f"examined, {n} foreign path(s), "
                    f"{leg.get('deleted', 0)} row(s) deleted"
                )
        print(f"total deleted: {result.get('total_deleted', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
