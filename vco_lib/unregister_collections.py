# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Which Weaviate collections a "Remove project" may drop — the ONE home of
``python -m vco_lib.project_init drop-collections`` (moved out of
``project_init`` in v0.2.106; that module is line-ratchet-capped and keeps a
shim).

The drop is CONSENTED (the user ticked "also delete its collections") and it
is still exact: it drops THIS project's own classes and nothing else.

* **What is this project's.** The classes derived from its name (the
  ``<Name>_KnowledgeGraph`` / ``_Development`` / ``_Diagrams`` family and the
  five ``<Prefix>_Code*`` classes), and — when the launcher passes the
  project's id — the classes its launcher rows actually bind
  (:func:`vco_lib.project_identity.resolve_snapshot`): a renamed project, a
  custom KG binding or a code-graph prefix that differs from the name lived
  under names the derivation never reaches, and their vectors were left behind.
* **What is never dropped.** The shared KG — the constant and this machine's
  own shared class (:func:`vco_lib.bootstrap_case_sibling.protected_shared_names`)
  — and, by id, every class ANOTHER registered project reads or writes (its KG
  bindings, development / diagrams classes and code-graph family). Two
  projects can derive the same name; the other one's data is not this
  removal's to drop. Each such class is reported in ``kept`` with the reason.
  Class names are compared ``casefold()`` — Weaviate class names collide
  case-insensitively.

When the launcher passes the id and its registry cannot be read, nothing is
dropped: every class is reported in ``kept`` and the reply says why
(``notes``) — the other-project check cannot be skipped silently (review
A-L4). Without an id (the bare CLI) the drop covers the name-derived set.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

__all__ = ["DropPlan", "plan_drop", "drop_project_collections"]


class DropPlan:
    """``targets`` to drop (in order), ``kept`` ``[{"collection", "reason"}]``,
    ``notes`` ``[str]`` — what the drop could not check."""

    def __init__(self) -> None:
        self.targets: list[str] = []
        self.kept: list[dict] = []
        self.notes: list[str] = []


def _add(seq: list[str], name: Optional[str]) -> None:
    if name and name.casefold() not in {s.casefold() for s in seq}:
        seq.append(name)


def plan_drop(
    project_name: str,
    *,
    project_id: Optional[str] = None,
    snapshot: Any = None,
    protected_shared: frozenset = frozenset(),
) -> DropPlan:
    """The decision, pure: name + optional id + registry snapshot in, plan out.

    ``protected_shared`` — casefolded shared-class names (refused by the
    caller as errors, the pre-v0.2.106 contract; this function does not drop
    them from ``targets``)."""
    from vco_lib.project_init import derive_project_collection_names

    plan = DropPlan()
    derived = derive_project_collection_names(project_name)
    for name in (derived["kg_collection"], derived["development_collection"],
                 derived["diagrams_collection"], *(derived.get("code_collections") or [])):
        _add(plan.targets, name)
    if not project_id:
        return plan
    if snapshot is None or not getattr(snapshot, "resolvable", False):
        # Review A-L4: the Remove flow promises that other projects'
        # collections are never dropped. Unverifiable is not verified — keep
        # every class and say why; the user can run the drop again once the
        # registry reads.
        reason = ("the launcher registry could not be read, so VCO could not check that no "
                  "other project uses it; it was not dropped")
        plan.kept = [{"collection": name, "reason": reason} for name in plan.targets]
        plan.targets = []
        plan.notes.append(
            "the launcher registry could not be read, so no collection was dropped (each "
            "is listed as kept); VCO could not check whether another project uses them")
        return plan
    me = None
    others: list[Any] = []
    for ident in snapshot.projects:
        if ident.project_id == project_id:
            me = ident
        else:
            others.append(ident)
    if me is not None:
        shared = (me.kg_shared or "").casefold()
        for name in (me.kg_primary, me.development, me.diagrams, *me.code_collections()):
            if name and name.casefold() != shared:
                _add(plan.targets, name)
    else:
        plan.notes.append(
            f"project id {project_id!r} is not in the launcher registry, so only the classes "
            "derived from the project name were considered")
    used_by: dict[str, str] = {}
    for other in others:
        for name in (*other.kg_collections(), *other.code_collections()):
            if name:
                used_by.setdefault(name.casefold(), other.name)
    keep_targets: list[str] = []
    for name in plan.targets:
        owner = used_by.get(name.casefold())
        if owner is not None and name.casefold() not in protected_shared:
            plan.kept.append({"collection": name,
                              "reason": f"also used by the project {owner!r}, so it was not dropped"})
            continue
        keep_targets.append(name)
    plan.targets = keep_targets
    return plan


def drop_project_collections(
    project_name: str,
    *,
    project_id: Optional[str] = None,
    weaviate_url: Optional[str] = None,
    delete_class: Optional[Callable[..., None]] = None,
) -> dict:
    """Run the drop. Returns ``{"dropped", "skipped_shared", "kept", "notes",
    "errors": [{"collection", "error"}]}``; a 404 counts as dropped (the
    ``_delete_class`` contract). Never raises."""
    from vco_lib import bootstrap_case_sibling as _bcs
    from vco_lib import project_init as _pi

    machine_shared, protected = _bcs.protected_shared_names()
    snapshot = None
    if project_id:
        from vco_lib.project_identity import resolve_snapshot
        snapshot = resolve_snapshot()
    plan = plan_drop(project_name, project_id=project_id, snapshot=snapshot,
                     protected_shared=frozenset(p.casefold() for p in protected))
    result: dict = {"dropped": [], "skipped_shared": machine_shared,
                    "kept": plan.kept, "notes": plan.notes, "errors": []}
    deleter = delete_class if delete_class is not None else _pi._delete_class
    for name in plan.targets:
        if name.lower() in protected:
            result["errors"].append({
                "collection": name,
                "error": (f"refusing to drop shared collection {name!r} — "
                          f"shared KG is install-owned, not project-owned"),
            })
            continue
        try:
            deleter(name, weaviate_url=weaviate_url)
            result["dropped"].append(name)
        except Exception as e:  # noqa: BLE001 — per-collection error, reported
            result["errors"].append({"collection": name, "error": f"{type(e).__name__}: {e}"})
    return result
