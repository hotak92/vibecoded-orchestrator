# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Foreign-row pruning for per-project KG/Development collections
(v0.2.89 §7.3 — BUG-3 damage repair, wave-2).

Why this module exists
----------------------
BUG 3 (kg-sync resolving the project root from a leaked ``KG_BASE_DIR``)
could embed ANOTHER project's files into this project's collections — e.g.
orchestrator ``docs/**`` rows inside ``Arzillibus_Development``. The BUG-3
fix (wave-2 P1) removes every path to the class; this module heals the
EXISTING damage: rows whose ``file_path`` does not exist on disk under the
project are stale/foreign and are deleted.

Guards (binding, from the 2026-07-30 wave-2 plan §7.3):

* Rows whose file EXISTS on disk are NEVER touched (even if content
  drifted — normal sync owns that). Existence is judged on the path AS
  THE SYNC STORES IT — lexically, ``(project_root / path).exists()``,
  which FOLLOWS symlinks (v0.2.103 R2, review H-1): the sync stores
  ``file_path`` lexically (``knowledge/x.md`` even when ``knowledge/``
  is a symlink to an outside vault), so a resolve-and-contain check
  read every such row as "file gone" and a full sync deleted the rows
  it had just written. A stored path whose resolution ESCAPES the root
  is unjudgeable by this tree's walk and is KEPT, never read as
  missing.
* **Shared-identity skip** (the load-bearing guard): when the project's KG
  collection IS the shared collection, the KG leg is skipped
  entirely. BUG-6 shared-scoped nodes from OTHER projects make
  "absent locally" NORMAL in the shared store — pruning there would
  destroy other projects' legitimate rows. v0.2.103 R2 (review H-2):
  the identity decision is ONE predicate,
  :func:`is_shared_identity_collection` — name match
  (case-insensitive) OR the walked root being the orchestrator clone
  (root ≡ shared by construction, v0.2.81 adopt-and-route) — computed
  INSIDE :func:`prune_foreign_rows`, so no caller can pass
  ``is_shared_identity=False`` past a root clone or an
  empty/unresolved shared name.
* **Registered-owner gate** (v0.2.103; in this home since the final
  review M-2): the pruned tree must BE the registered launcher.db owner of
  the collection (:func:`collection_owner_refusal`) — a lane worktree or a
  second clone resolves the same names but must never prune against its
  own, different tree. Applies to both legs and every caller.
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
from vco_lib.knowledge_residue import (
    project_settings_env,
    weaviate_reachable,
)
from vco_lib.orchestrator_identity import is_orchestrator_clone

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


def is_shared_identity_collection(
    kg_collection: "str | None",
    shared_kg_collection: "str | None",
    project_root: "Path | str | None",
) -> bool:
    """THE shared-identity predicate for every KG prune caller (v0.2.103
    R2, review H-2 — previously three divergent copies: kg-sync matched
    by name only, the install adapter matched name-or-root with its own
    root rule, the bundle leg matched name only, and an EMPTY unresolved
    shared name disarmed all three on the orchestrator root).

    True when the collection IS the shared store, by either evidence:

    * NAME — ``kg_collection`` equals ``shared_kg_collection``
      case-insensitively (Weaviate class names cannot differ by case
      only, and case-insensitive equality is the more conservative
      reading); or
    * ROOT — ``project_root`` is the orchestrator clone
      (:func:`vco_lib.orchestrator_identity.is_orchestrator_clone`):
      root ≡ shared by construction (v0.2.81 adopt-and-route), so the
      skip must hold even when the shared NAME is empty or unresolved
      (kg-sync resolves it hub → launcher.db → env, and ``""`` is a
      legitimate answer with the hub down or a free-tier install).

    An empty ``kg_collection`` is never shared-identity here — the
    callers' "no collection name" skip owns that case.
    ``is_orchestrator_clone`` never raises, and a ``None`` root simply
    disarms the ROOT leg.
    """
    kg = (kg_collection or "").strip()
    if not kg:
        return False
    shared = (shared_kg_collection or "").strip()
    if shared and kg.lower() == shared.lower():
        return True
    if project_root is None:
        return False
    return is_orchestrator_clone(Path(project_root))


def collection_owner_refusal(
    project_root: "Path | str", collection: "str | None",
) -> Optional[str]:
    """``None`` when *project_root* may prune *collection*; a skip REASON
    when it must not — the REGISTERED-OWNER gate (v0.2.103 R2 review M-3;
    moved here, the prune home, by the final review M-2 so EVERY caller is
    covered: the full kg-sync walk, a bundle update run on any folder, and
    this module's own CLI).

    A lane worktree or a second clone of a project carries the same
    ``.claude/settings.json`` collection names, so a prune run there
    resolves the SAME collection for a tree that is not its registered
    owner — and pruning it against that tree deletes rows whose files exist
    only in the real checkout (uncommitted work, gitignored ``knowledge/``,
    nodes added on main after the branch point). The rule: *project_root*
    must BE the registered folder of every registration that owns
    *collection* — its KG ``primary`` or ``shared`` binding, or the
    development / diagrams collection derived from its primary (the ONE
    derivation, :func:`vco_lib.config_projection.resolve_project_collection_names`).

    * no launcher.db at all (free tier, no registry) ⇒ proceed — no second
      tree is registered to claim the collection;
    * launcher.db present but unreadable ⇒ REFUSE — ownership is
      unprovable this run, and a destructive path waits for proof;
    * a registered project owns the name (case-insensitive): its folder IS
      this root ⇒ proceed; a DIFFERENT folder ⇒ REFUSE, naming the owner;
    * no registration owns the name ⇒ proceed (unregistered project).

    Read-only. A failed ``vco_lib`` import propagates (a broken install is
    loud, and the caller's prune does not run).
    """
    from vco_lib.config_projection import resolve_project_collection_names
    from vco_lib.launcher_db_reader import (
        _discover_db_path,
        get_kg_binding,
        list_registered_projects,
    )
    from vco_lib.module_gated_delivery import _canonical_path_eq

    wanted = (collection or "").strip().lower()
    if not wanted:
        return None
    db_path = _discover_db_path()
    if db_path is None:
        return None  # no registry exists ⇒ no second tree can claim it
    unreadable = ("the launcher registry exists but could not be read — "
                  "collection ownership is unprovable this run, so the "
                  "prune is refused")
    projects = list_registered_projects()
    if projects is None:
        return unreadable
    for proj in projects:
        owned = []
        for role in ("primary", "shared"):
            owned.append(get_kg_binding(proj.id, role) or "")
        try:
            names = resolve_project_collection_names(proj.id, db_path=db_path)
        except Exception:  # noqa: BLE001 — unprovable ⇒ refuse, never guess
            return unreadable
        owned.extend([names.get("kg_collection", ""),
                      names.get("development_collection", ""),
                      names.get("diagrams_collection", "")])
        if wanted not in {o.strip().lower() for o in owned if o}:
            continue
        if _canonical_path_eq(proj.folder, project_root):
            continue  # this root IS the registered owner
        return (f"'{collection}' is registered to {proj.folder!r} (project "
                f"{proj.name!r}), not to the walked root {project_root} — a "
                "worktree or second clone must not prune another tree's "
                "collection")
    return None


#: Rows written from an isolated lane worktree carry the worktree prefix;
#: the same file under the project root is what the prune must judge.
_WORKTREE_PREFIX_RE = re.compile(r"\.claude/worktrees/agent-[^/]+/(.*)$")


def _resolves_outside_root(candidate: Path, project_root: Path) -> bool:
    """True when *candidate*'s RESOLUTION escapes *project_root* — or when
    the question cannot be answered (OSError/ValueError ⇒ True, the keep
    side).

    The prune walks THIS tree; a stored path whose symlink chain lands
    outside it (a vault-linked ``knowledge/``, a linked node) is
    unjudgeable by that walk — the file's absence from the walk proves
    nothing. "Outside" is therefore a KEEP verdict, never evidence of a
    missing file (v0.2.103 R2, review H-1).
    """
    try:
        return not candidate.resolve().is_relative_to(
            project_root.resolve()
        )
    except (OSError, ValueError):
        return True


def _stored_file_exists_for_prune(file_path_str: str, project_root: Path) -> bool:
    """Existence judged on the path AS THE SYNC STORES IT (v0.2.103 R2,
    review H-1).

    The sync stores ``file_path`` LEXICALLY (``knowledge/x.md`` even when
    ``knowledge/`` is a symlink to an outside vault — the walk reports
    the link's location, not its target). The pre-fix check re-resolved
    the candidate and required the result INSIDE the root, so every row
    of a symlinked ``knowledge/`` read as "file gone" and a full
    ``kg-sync --all`` deleted the rows it had just written — on every
    run.

    Rule (any leg True ⇒ KEEP the row):

      1. ``(project_root / path).exists()`` — lexical, FOLLOWS symlinks.
      2. The candidate RESOLVES outside the root ⇒ keep (unjudgeable by
         this tree's walk — :func:`_resolves_outside_root`).
      3. Worktree-prefixed rows (``.claude/worktrees/agent-*/…``): the
         stripped tail is judged by legs 1–2.
      4. Any OS/value error while judging ⇒ keep (a destructive action
         needs a positive "gone", never a guess).

    The empty / absolute / UNC / ``..`` shapes never reach here —
    :func:`_classify_file_path` already classifies them defensive.
    """
    try:
        candidate = project_root / file_path_str
        if candidate.exists():
            return True
        if _resolves_outside_root(candidate, project_root):
            return True
    except (OSError, ValueError):
        # NUL bytes / invalid chars in a stored file_path — unjudgeable ⇒
        # keep.
        return True
    m = _WORKTREE_PREFIX_RE.search(file_path_str)
    if m:
        try:
            stripped = project_root / m.group(1)
            if stripped.exists():
                return True
            if _resolves_outside_root(stripped, project_root):
                return True
        except (OSError, ValueError):
            return True
    return False


def _get_rows_payload(
    base: str, collection: str, *, with_title: bool,
) -> Optional[dict]:
    """The parsed GraphQL answer listing *collection*'s rows, or ``None`` on a
    transport failure, a non-200 status or an unparsable body."""
    fields = "_additional { id } file_path title" if with_title \
        else "_additional { id } file_path"
    gql = {
        "query": (
            f"{{ Get {{ {collection}(limit: {QUERY_MAX_LIMIT}) "
            f"{{ {fields} }} }} }}"
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
    return payload if isinstance(payload, dict) else None


def _errors_name_missing_title(payload: dict) -> bool:
    """Whether every GraphQL error is Weaviate's "Cannot query field \"title\""
    for a class without that property (and there is at least one)."""
    errors = payload.get("errors") or []
    if not errors:
        return False
    return all(
        isinstance(e, dict) and 'field "title"' in str(e.get("message", ""))
        for e in errors
    )


def _enumerate_rows(
    weaviate_url: str,
    collection: str,
) -> Optional[tuple[list[tuple[str, str, "str | None"]], bool]]:
    """Fetch ``(uuid, file_path, title)`` triples for every row in
    *collection* (v0.2.103: ``title`` joined so prune callers can log every
    removed row by path AND title; rows whose stored title is absent report
    ``None``).

    Returns ``(rows, saturated)`` or ``None`` on any transport / GraphQL
    failure (the caller skips the leg — no destructive action without an
    authoritative row list). The errors[] array is inspected BEFORE data is
    consumed (silent-zero-fallback lesson).
    """
    base = weaviate_url.rstrip("/")
    payload = _get_rows_payload(base, collection, with_title=True)
    if payload is not None and _errors_name_missing_title(payload):
        # A collection whose schema has no ``title`` property (the title join
        # is a v0.2.103 logging aid, not part of the prune decision): Weaviate
        # rejects the whole query, which used to turn every prune of such a
        # collection into "enumeration failed". Ask again without it; titles
        # then report ``None`` exactly like an absent stored title.
        payload = _get_rows_payload(base, collection, with_title=False)
    if payload is None:
        return None
    if payload.get("errors"):
        # A missing class surfaces here — treat as "nothing to prune"
        # ONLY when the error names an unresolved type; anything else is
        # an enumeration failure. Conservative: skip the leg either way.
        return None
    objects = (
        payload.get("data", {}).get("Get", {}).get(collection, [])
    ) or []
    rows: list[tuple[str, str, "str | None"]] = []
    for obj in objects:
        uid = ((obj or {}).get("_additional") or {}).get("id") or ""
        fp = (obj or {}).get("file_path")
        title = (obj or {}).get("title")
        if uid:
            rows.append((
                str(uid),
                fp if isinstance(fp, str) else "",
                title if isinstance(title, str) else None,
            ))
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
    shared_kg_collection: Optional[str] = None,
    dry_run: bool = False,
    log_event: Optional[Callable[..., None]] = None,
    log_step: str = "4.bundle.foreign_rows",
) -> dict:
    """Delete rows in *collection* whose ``file_path`` does not exist on disk
    under *project_root*.

    See the module docstring for the guard contract. ``log_step`` is the
    step id forwarded to ``log_event`` (the bundle-update default keeps its
    historical value; the install-time adapter passes its own step id so the
    forensic log names the phase that actually ran).

    ``is_shared_identity`` remains accepted for callers that already proved
    identity themselves, but the decision no longer depends on the caller:
    v0.2.103 R2 (review H-2) computes
    :func:`is_shared_identity_collection` HERE from *collection*,
    *shared_kg_collection* and *project_root*, so a root clone or a name
    match can never be pruned because a caller passed ``False``.

    Returns a JSON-serialisable dict::

        {
          "collection": str,
          "skipped": None | "<reason>",
          "rows": int,                 # rows enumerated
          "defensive_skipped": int,    # absolute / ``..`` / empty paths
          "stale_paths": [<fp>...],    # distinct foreign paths found
          "stale_rows": [              # v0.2.103: per-ROW detail for logging
            {"path": <fp>, "title": <title | None>}, ...
          ],
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
        "stale_rows": [],
        "deleted": 0,
        "saturated": False,
        "dry_run": bool(dry_run),
        "errors": [],
    }

    def _log(phase: str, detail: str, *, data: Any = None) -> None:
        if log_event is None:
            return
        try:
            log_event(log_step, phase, detail, data=data)
        except TypeError:
            try:
                log_event(log_step, phase, detail)
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
    # v0.2.103 R2 (review H-2): the decision is COMPUTED here through the
    # one predicate (name match OR orchestrator-clone root), BEFORE any
    # enumeration — a caller passing is_shared_identity=False cannot
    # disarm it.
    if is_shared_identity or is_shared_identity_collection(
        collection, shared_kg_collection, project_root
    ):
        result["skipped"] = "shared-identity collection"
        _log("ok", f"prune skipped for {collection!r}: shared-identity "
                   "collection (other projects' shared-scoped rows live here)")
        return result

    # REGISTERED-OWNER gate (v0.2.103 final review M-2): computed HERE, in
    # the prune home, so a worktree / second clone is refused on every
    # caller path, BEFORE any enumeration.
    not_owner = collection_owner_refusal(project_root, collection)
    if not_owner is not None:
        result["skipped"] = not_owner
        _log("warn", f"prune skipped for {collection!r}: {not_owner}")
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
    stale_rows: list[dict] = []
    stale_paths: set = set()
    defensive = 0
    for uid, fp, title in rows:
        kind = _classify_file_path(fp)
        if kind == "defensive":
            defensive += 1
            continue
        norm = fp.strip().replace("\\", "/")
        if _stored_file_exists_for_prune(norm, project_root):
            continue  # file exists (or is unjudgeable) ⇒ NEVER touched
        stale_uuids.append(uid)
        stale_paths.add(norm)
        stale_rows.append({"path": norm, "title": title})
    result["defensive_skipped"] = defensive
    result["stale_paths"] = sorted(stale_paths)
    result["stale_rows"] = sorted(
        stale_rows, key=lambda r: (r["path"], r["title"] or "")
    )

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
      * KG leg skipped on shared identity per the ONE predicate
        (:func:`is_shared_identity_collection`): ``kg_collection ==
        shared_kg_collection`` (case-insensitive — Weaviate class names
        cannot differ by case only) OR *project_root* is the orchestrator
        clone (root ≡ shared by construction; holds even with an empty or
        unresolved shared name).
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
    kg_shared_identity = is_shared_identity_collection(kg, shared, project_root)

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
                "defensive_skipped": 0, "stale_paths": [], "stale_rows": [],
                "deleted": 0,
                "saturated": False, "dry_run": bool(dry_run), "errors": [],
            }
            result["legs"].append(leg)
            continue
        leg = prune_foreign_rows(
            project_root,
            coll,
            weaviate_url=weaviate_url,
            shared_kg_collection=shared,
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

    Returns ``source`` ("hub" | "settings/binding-first") AND
    ``fallback_reason`` — ``None`` on the hub path, otherwise the exception
    that pushed us off it.

    v0.2.96: the ``except`` arm was ``pass``, so this function could say WHICH
    resolver answered but never WHY the preferred one did not. A prune that
    addresses the wrong collection because the hub was down is then
    indistinguishable, in its own report, from one that addresses the wrong
    collection because the hub answered wrongly — and the operator is left
    re-deriving it. The reason travels IN THE RESULT rather than only through
    a log line: ``main --json`` is a machine surface whose consumer never
    reads stderr, and `resolution_source` is already in that document, so the
    "why" belongs beside the "which". `main` also prints it for humans.
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
            "fallback_reason": None,
        }
    except Exception as exc:  # noqa: BLE001 — hub-down / unregistered ⇒ fallback
        fallback_reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__

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
        "fallback_reason": fallback_reason,
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
    # v0.2.96: a soft-fail that discards its own diagnosis makes a resolution
    # bug unreportable. `None` when the hub answered.
    result["resolution_fallback_reason"] = ctx.get("fallback_reason")

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result.get("resolution_fallback_reason"):
            # stderr: this is a caveat on the numbers below, not one of them.
            print(
                "note: the hub did not resolve this project "
                f"({result['resolution_fallback_reason']}); collections were "
                f"resolved from {result['resolution_source']}. If the counts "
                "below name a collection you did not expect, that is where "
                "the name came from.",
                file=sys.stderr,
            )
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
