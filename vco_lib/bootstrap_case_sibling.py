# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Collection bootstrap vs. the machine's shared KG and case-only siblings
(v0.2.103, R24 HIGH-1).

Since v0.2.103 the orchestrator root owns its FOLDER-DERIVED KG name
(:mod:`vco_lib.root_kg_identity`); on a ``vibecoded-orchestrator`` clone that is
``VibecodedOrchestrator_KnowledgeGraph``, a case-only sibling of the shipped
constant ``VibeCodedOrchestrator_KnowledgeGraph``. Weaviate refuses two classes
that differ only in case. ``project_init.bootstrap_collections`` used to target
the constant as the shared KG, meet the 422 "found similar class", and DROP the
sibling — the root's populated shared KG — to recreate it empty.

This module holds the rules ``project_init`` delegates to (extracted so the
mega-module does not grow):

* :func:`shared_targets` — the shared class a bootstrap creates is THIS
  machine's (``project_init._resolve_shared_kg_name``: the project's shared
  binding → its settings pin → the orchestrator root's binding), never the
  constant.
* :func:`blocks` — a case-only sibling that holds objects, or whose count
  cannot be read, is NEVER dropped: the target is reported (``blocked`` /
  ``errors`` in the envelope) and deferred. Only an EMPTY sibling keeps the
  pre-existing drop-and-recreate (nothing to lose).
* :func:`settle` — the ``collection_blocked_by_case_sibling`` ledger row:
  emitted while a target is blocked, resolved by the bootstrap run that meets
  none (paired resolution, ``vco_lib/deferral_conditions.toml``).
* :func:`protected_shared_names` — the consented ``drop-collections`` purge
  refuses the machine's shared class as well as the constant.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

#: Registered in ``vco_lib/deferral_conditions.toml`` (paired-resolution).
CID = "collection_blocked_by_case_sibling"


def _pi():
    from vco_lib import project_init

    return project_init


def shared_targets(project_folder, kg_collection: str, log: Callable[..., None]) -> list:
    """``[shared class]`` for the bootstrap target list, or ``[]`` when it is
    the project's own KG (the root) or pinned empty (the documented "fan-out
    disabled" choice — no class is named, nothing to create)."""
    name = _pi()._resolve_shared_kg_name(
        Path(project_folder) if project_folder is not None else None)
    if not name:
        log("7b.bootstrap", "skip",
            "shared KG not created: SHARED_KG_COLLECTION is pinned empty",
            data={"collection": None})
        return []
    return [] if name == kg_collection else [name]


def blocks(name: str, sibling: Optional[str], result: dict, log: Callable[..., None],
           weaviate_url: Optional[str]) -> bool:
    """True (and the block recorded in ``result``) when creating ``name`` met a
    case-only ``sibling`` that holds objects or cannot be counted — the caller
    must then leave both names alone. False for no sibling, or an empty one."""
    if not sibling or sibling == name:
        return False
    pi = _pi()
    rows = pi._snapshot_collection_for_rebuild(sibling, weaviate_url=weaviate_url)["object_count"]
    if rows == 0:
        return False
    shown = rows if rows is not None else "an unknown number of"
    log("7b.bootstrap", "warn",
        f"{name}: case-only sibling {sibling!r} holds {shown} object(s); "
        f"not dropped, {name} not created",
        data={"collection": name, "conflicting_name": sibling, "object_count": rows,
              "branch": "case-conflict-blocked"})
    result.setdefault("blocked", []).append(
        {"collection": name, "sibling": sibling, "object_count": rows})
    result["actions"].append({"collection": name, "action": "blocked", "ok": False})
    result["errors"].append({
        "collection": name,
        "error": (f"not created: Weaviate holds {sibling!r} "
                  f"({rows if rows is not None else 'unknown'} objects), which differs only "
                  f"in case; VCO never drops a collection that holds data — see "
                  f"UPDATE_DEFERRED.md ({CID})"),
    })
    return True


def settle(project_folder, result: dict, *, project_name: str, weaviate_url: str,
           kg_only: bool, dry_run: bool, log: Callable[..., None]) -> None:
    """Emit (a target is blocked) or resolve (none is) the ledger row in
    ``project_folder``. No folder or a dry run → nothing. Soft-fail: ledger
    I/O never breaks a bootstrap."""
    if project_folder is None or dry_run:
        return
    from vco_lib import deferral_emit as _de

    folder = Path(project_folder)
    blocked = result.get("blocked") or []
    try:
        if not blocked:
            ledger = folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
            if ledger.is_file() and CID in ledger.read_text(encoding="utf-8", errors="replace"):
                _de.resolve_conditions(folder, [CID])
            return
        _de.emit(folder, blocked_entry(folder, blocked, project_name=project_name,
                                       weaviate_url=weaviate_url, kg_only=kg_only))
    except Exception as exc:  # noqa: BLE001 — deferral I/O is best-effort
        log("7b.bootstrap", "error",
            f"case-sibling deferral write failed: {type(exc).__name__}: {exc}",
            data={"error": str(exc)[:200]})


def blocked_entry(folder: Path, blocked: list, *, project_name: str, weaviate_url: str,
                  kg_only: bool):
    """The ``collection_blocked_by_case_sibling`` entry for ``blocked``."""
    from vco_lib.deferral_report import DeferralEntry

    pairs = "; ".join(
        f"'{b['collection']}' blocked by '{b['sibling']}' "
        f"({b['object_count'] if b['object_count'] is not None else 'unknown'} objects)"
        for b in blocked)
    rerun = (f"python -m vco_lib.project_init bootstrap-collections "
             f"--name {project_name!r} --weaviate-url {weaviate_url!r} "
             f"--project-folder {str(folder)!r}" + (" --kg-only" if kg_only else ""))
    first = blocked[0]["sibling"]
    return DeferralEntry(
        condition_id=CID,
        title="Weaviate collection blocked by a populated case-only sibling",
        detected=(f"Collection bootstrap could not create: {pairs}. Weaviate refuses two "
                  f"classes that differ only in case, and the existing class holds data "
                  f"(or its count could not be read), so it was NOT dropped. Nothing was "
                  f"created, dropped or rebound."),
        why_deferred=("Whether that class is this collection's data is your decision: it may "
                      "be another install's KG, the orchestrator root's shared KG, or vectors "
                      "from another embedding model. VCO never drops a collection that holds "
                      "objects."),
        command_to_apply="\n".join([
            "# If the existing class IS the data this name should hold, bind it instead:",
            "#   the orchestrator root's KG / the shared KG:",
            f"python -m vco_lib.root_kg_identity adopt --collection {first} --yes",
            "#   a project's own KG: launcher -> the project -> Identity tab -> KG binding.",
            "# Otherwise back the class up / remove it yourself, then re-run (idempotent):",
            rerun,
        ]),
        severity="warning",
        kg_node_refs=["knowledge/concepts/weaviate-schema-evolution.md"],
    )


def protected_shared_names() -> "tuple[str, set[str]]":
    """``(this machine's shared class, the lowercased names a purge refuses)`` —
    the constant AND the machine's resolved shared class, compared
    case-insensitively (Weaviate class names collide case-insensitively), so a
    project whose derived KG name is the root's shared class can never purge it."""
    pi = _pi()
    machine = pi._resolve_shared_kg_name(None) or pi._SHARED_KG_NAME
    return machine, {pi._SHARED_KG_NAME.lower(), machine.lower()}
