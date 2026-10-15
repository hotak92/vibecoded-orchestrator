# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The collection bootstrap never drops a POPULATED class to repair its schema
(v0.2.103, R24 follow-up).

``project_init.bootstrap_collections`` (launcher project add / update,
``install-bundle``) used to drop and recreate every existing class whose schema
``_schema_incompatible`` flags (legacy single-vector, missing core named-vector
slots, ``indexNullState`` off), justified as "knowledge/**/*.md re-ingests".
That is false for the shared KG (nodes other projects stored with
``scope='shared'`` live in no one folder) and for any node written straight to
Weaviate, and it contradicts the standing rule: destructive schema migrations
are never auto-applied; they wait for consent via ``schema_migration_required``
and ``python -m vco_lib.project_init migrate-collections``.

The rule (:func:`defers`): a schema-incompatible class that holds objects, or
whose count cannot be read, is left untouched and deferred; an EMPTY class
keeps the drop-and-recreate (nothing to lose). Every class, the shared one
included.

:func:`settle` writes ONE ``schema_migration_required`` entry — the existing
condition (its lifecycle holds: a clean ``migrate-collections`` dry-run clears
it, and the next bootstrap re-detects it while still true). Its command block
is the emitter's own (``migrate_deferral.migrate_command``). The entry is
built here rather than by ``_emit_migrate_required_deferral`` because that
emitter's DETECTED/WHY text would be false for this source: it says a
``migrate-collections`` dry-run reported the drift, and labels every class a
"rebuild … legacy single-vector" — but the bootstrap's own probe found it, and
a class missing a named-vector slot or ``indexNullState`` is a lossless
``copy`` for ``migrate-collections`` (``_classify_action``), not a rebuild.

Which ledger and which ``--name``: a class that is the project's own KG / Dev
belongs to the bootstrapped project. Any other target is the shared KG, owned
by the orchestrator root (its KG IS the shared class) — that entry goes to the
ROOT's ledger with ``--name <root>`` (``migrate-collections --name <project>``
never rebuilds a class that is not that project's own, so pointing a
non-root project at it would be a false remedy, and the project's next clean
dry-run would clear it while the shared class is still unmigrated). With no
registered root, the entry stays in the project's ledger and names the class
stem, without ``--project-folder`` (nothing to re-ingest from).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

#: The existing condition, reused (``vco_lib/deferral_conditions.toml``).
CID = "schema_migration_required"


def _pi():
    from vco_lib import project_init

    return project_init


def defers(name: str, existing: dict, target_def_fn, reason: str, result: dict,
           log: Callable[..., None], weaviate_url: Optional[str], *, dry_run: bool) -> bool:
    """True (and the deferral recorded in ``result["schema_deferred"]``) when
    the schema-incompatible ``name`` holds objects or cannot be counted — the
    caller must leave it alone. False for an empty class (rebuild proceeds)."""
    pi = _pi()
    rows = pi._snapshot_collection_for_rebuild(name, weaviate_url=weaviate_url)["object_count"]
    if rows == 0:
        return False
    try:
        action = pi._classify_action(pi._schema_delta(existing, target_def_fn(name)))
    except Exception:  # noqa: BLE001 — the label is informative only
        action = "rebuild"
    shown = rows if rows is not None else "an unknown number of"
    log("7b.bootstrap", "warn",
        f"{name}: schema needs migration ({reason}) but it holds {shown} object(s); "
        f"not dropped — deferred to migrate-collections",
        data={"collection": name, "reason": reason, "object_count": rows,
              "migrate_action": action, "branch": "regen-deferred"})
    result.setdefault("schema_deferred", []).append(
        {"collection": name, "reason": reason, "object_count": rows, "migrate_action": action})
    result["actions"].append({"collection": name,
                              "action": "would-defer" if dry_run else "deferred", "ok": True})
    return True


def _root_project():
    try:
        from vco_lib.launcher_db_reader import list_registered_projects

        for ref in list_registered_projects() or ():
            if ref.host == "orchestrator_root":
                return ref
    except Exception:  # noqa: BLE001 — no registry = no known owner
        return None
    return None


def settle(result: dict, *, project_name: str, project_folder, derived: dict,
           weaviate_url: str, dry_run: bool, log: Callable[..., None]) -> None:
    """Write the ``schema_migration_required`` entry for every deferred class,
    to the ledger of the project that owns it. No folder or a dry run →
    nothing. Never resolves the entry (the ``migrate-collections`` dry-run
    reconcile owns clearing). Soft-fail."""
    deferred = result.get("schema_deferred") or []
    if not deferred or project_folder is None or dry_run:
        return
    own = {(derived.get(k) or "").lower() for k in ("kg_collection", "development_collection")}
    groups: dict = {}
    root = None
    for d in deferred:
        if d["collection"].lower() in own:
            key = (project_name, str(Path(project_folder)), str(Path(project_folder)))
        else:
            root = root or _root_project()
            if root is not None:
                key = (root.name, root.folder, root.folder)
            else:
                stem = d["collection"].rsplit("_KnowledgeGraph", 1)[0]
                key = (stem, None, str(Path(project_folder)))
        groups.setdefault(key, []).append(d)
    by_ledger: dict = {}
    for (name, cmd_folder, ledger), items in groups.items():
        by_ledger.setdefault(ledger, []).append((name, cmd_folder, items))
    from vco_lib import deferral_emit as _de

    for ledger, owners in by_ledger.items():
        try:
            _de.emit(Path(ledger), entry(owners, weaviate_url=weaviate_url))
        except Exception as exc:  # noqa: BLE001 — deferral I/O is best-effort
            log("7b.bootstrap", "error",
                f"schema-migration deferral write failed: {type(exc).__name__}: {exc}",
                data={"error": str(exc)[:200]})


def entry(owners: list, *, weaviate_url: str):
    """The ``schema_migration_required`` entry for ``owners`` =
    ``[(migrate --name, --project-folder or None, [deferred items])]``."""
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib.migrate_deferral import migrate_command

    lines, cmds = [], []
    for name, cmd_folder, items in owners:
        for d in sorted(items, key=lambda x: x["collection"]):
            rows = d["object_count"] if d["object_count"] is not None else "an unknown number of"
            kind = {"copy": "lossless: vectors preserved",
                    "rebuild": "drop + re-embed"}.get(d["migrate_action"], "additive")
            lines.append(f"  - `{d['collection']}` ({rows} objects): {d['reason']} → "
                         f"**{d['migrate_action']}** ({kind})")
        cmds.append(migrate_command(name, weaviate_url, cmd_folder))
    return DeferralEntry(
        condition_id=CID,
        title="Schema migration required",
        detected=("The Weaviate collection bootstrap found existing collections whose schema "
                  "is behind the current target. They hold data, so they were NOT dropped "
                  "and recreated:\n" + "\n".join(lines)),
        why_deferred=("VCO never drops a collection that holds objects to repair its schema: "
                      "its nodes may exist nowhere else (shared-KG nodes other projects wrote, "
                      "nodes written straight to Weaviate). `migrate-collections` preserves "
                      "vectors via copy where it can and rebuilds only legacy single-vector "
                      "collections — run it when you are ready. Read paths keep working on "
                      "the existing schema."),
        command_to_apply="\n\n".join(cmds),
        severity="warning",
        kg_node_refs=["knowledge/concepts/weaviate-schema-evolution.md"],
    )
