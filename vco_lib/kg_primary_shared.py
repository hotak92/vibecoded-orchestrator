# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A registered project whose PRIMARY KG collection is the SHARED one —
detection and the doctor finding (v0.2.105, read-only).

THE FIELD DEFECT (Windows install): 19 of 20 registered projects had the
shared KG class (``VibeCodedOrchestrator_KnowledgeGraph`` or the configured
shared name) bound as their PRIMARY KG collection. Every such project then
writes its own notes into the shared corpus every other project reads, and
reads the shared corpus as if it were its own.

THE CAUSE (fixed at its source): the update-time cross-prefix adoption pass
(``vco_lib.kg_binding_heal._prefix_adopt_kg_bindings_pass``) re-points a
binding whose class does not exist in Weaviate to the ONE populated class
with the same suffix. A project that never synced its knowledge folder has
no class yet; on a machine where the only populated ``*_KnowledgeGraph``
class is the shared one, that ONE candidate was the shared class — so every
unsynced project adopted it on the next update. The pass now never adopts,
as a PRIMARY, a class that is the shared collection or that another project
binds. The launcher's Identity-tab and binding writers refuse the same thing
up front.

THIS MODULE finds the rows the defect already wrote, so the user can repair
them. The orchestrator ROOT legitimately uses one class for both roles and is
never flagged. Nothing here writes: the repair is the launcher's Identity-tab
"Repair primary KG binding" action (``update_project_identity`` re-points the
primary binding to the name-derived class and re-projects the project's env
from it). No object is moved or deleted by either side — the shared class
keeps everything, including notes the project wrote into it while bound.

Readers:
* the doctor probe ``kg_primary_shared`` (emits/clears
  ``kg_primary_bound_to_shared``; :func:`probe`, :func:`doctor_entry`);
* the registry clear probe ``kg_primary_still_bound_to_shared``
  (:func:`still_bound`).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

#: The condition id (registered in ``vco_lib/deferral_conditions.toml``).
CID_PRIMARY_BOUND_TO_SHARED = "kg_primary_bound_to_shared"

#: The shared class when ``app_state`` names none and no root row exists.
#: MUST MATCH ``vco_lib.kg_binding_doctor.DEFAULT_SHARED_KG_COLLECTION``.
DEFAULT_SHARED_KG_COLLECTION = "VibeCodedOrchestrator_KnowledgeGraph"

_APP_STATE_SHARED_POINTER = "orchestrator_root_kg_collection"
_ROOT_HOST = "orchestrator_root"
_ROOT_SLUG = "orchestrator-root"

#: Where the user repairs it — the GUI action the entry names.
GUI_ACTION = "Identity tab → \"Repair primary KG binding\""


@dataclass(frozen=True)
class PrimaryBoundToShared:
    """One non-root project whose primary binding names the shared class."""

    project_id: str
    project_name: str
    folder: str
    bound: str
    expected: str

    @property
    def expected_is_shared(self) -> bool:
        """The name-derived class would itself be the shared one (a project
        named like the orchestrator) — the repair then needs a hand-picked
        name on the Identity tab."""
        return self.expected.lower() == self.bound.lower()


def _is_root(host: str, slug: str) -> bool:
    return host == _ROOT_HOST or slug == _ROOT_SLUG


def shared_collection_names(conn: sqlite3.Connection) -> set:
    """Lower-cased names that ARE the shared collection on this machine: the
    ``app_state`` pointer, and every class the orchestrator ROOT binds (its
    primary and shared rows — one class for both roles). The default name
    when the machine records neither."""
    names: set = set()
    try:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?",
                           (_APP_STATE_SHARED_POINTER,)).fetchone()
        if row and str(row[0] or "").strip().strip('"'):
            names.add(str(row[0]).strip().strip('"').lower())
    except sqlite3.Error:
        pass
    rows = conn.execute(
        "SELECT b.collection_name FROM project_kg_bindings b JOIN projects p "
        "ON p.id = b.project_id WHERE (p.host = ? OR p.slug = ?) "
        "AND b.role IN ('primary', 'shared')", (_ROOT_HOST, _ROOT_SLUG)).fetchall()
    names |= {str(r[0]).strip().lower() for r in rows if r and str(r[0] or "").strip()}
    return names or {DEFAULT_SHARED_KG_COLLECTION.lower()}


def _roles_of(conn: sqlite3.Connection, project_id: str) -> dict:
    return {str(r[0]): str(r[1] or "").strip() for r in conn.execute(
        "SELECT role, collection_name FROM project_kg_bindings WHERE project_id = ?",
        (project_id,)).fetchall()}


def names_shared_for(name: str, shared: set, roles: dict) -> bool:
    """THE rule: ``name`` is the shared collection for a project whose
    bindings are ``roles`` — one of the machine's shared names, or the
    project's OWN shared row. (Callers exclude the root first.)"""
    low = (name or "").strip().lower()
    return bool(low) and (low in shared or low == roles.get("shared", "").lower())


def find_in_conn(conn: sqlite3.Connection) -> list:
    """Every non-root project whose primary binding names the shared class
    (:func:`names_shared_for`). Raises ``sqlite3.Error`` when the tables
    cannot be read."""
    from vco_lib.project_identity import expected_kg_primary_class

    shared = shared_collection_names(conn)
    projects = conn.execute("SELECT id, name, slug, host, folder_path FROM projects").fetchall()
    out: list = []
    for pid, name, slug, host, folder in projects:
        if _is_root(str(host or ""), str(slug or "")):
            continue
        roles = _roles_of(conn, pid)
        primary = roles.get("primary", "")
        if names_shared_for(primary, shared, roles):
            out.append(PrimaryBoundToShared(
                project_id=str(pid), project_name=str(name or ""), folder=str(folder or ""),
                bound=primary, expected=expected_kg_primary_class(str(name or "")),
            ))
    return sorted(out, key=lambda f: f.project_name.lower())


def find(db_path: Optional[Path] = None) -> Optional[list]:
    """:func:`find_in_conn` on a read-only ``launcher.db`` open; ``None`` when
    the database cannot be opened or read (unknown is never "none found")."""
    try:
        from vco_lib.launcher_db_reader import _open_db_readonly

        conn = _open_db_readonly(db_path)
    except Exception:  # noqa: BLE001 — broken/partial install: cannot look
        return None
    if conn is None:
        return None
    try:
        conn.row_factory = None
        return find_in_conn(conn)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def remedy(findings: list) -> str:
    """The repair, per project: the GUI action and the class it re-points to."""
    lines = []
    for f in findings:
        if f.expected_is_shared:
            lines.append(
                f"{f.project_name}: open the project in the launcher → Identity tab and "
                "type a KG collection name of its own (its name-derived class IS the "
                "shared one)")
        else:
            lines.append(
                f"{f.project_name}: open the project in the launcher → {GUI_ACTION} "
                f"(re-points its primary KG binding to {f.expected})")
    return "\n".join(lines)


def probe(folder: Path, res: Any, ctx: dict) -> list:
    """Doctor probe ``kg_primary_shared``. Install roots read the machine's
    ``launcher.db`` (through ``res.resolve_kg_primary_shared``); ``None``
    (could not look) emits nothing. A clean reading is an OK finding CARRYING
    the cid, so the reading that emitted an entry is the one that clears it."""
    from vco_lib import doctor as d

    findings = res.resolve_kg_primary_shared()
    if findings is None:
        return []
    if not findings:
        return [d.Finding(
            probe="kg_primary_shared", status=d.STATUS_OK,
            summary="no registered project uses the shared KG collection as its primary",
            condition_id=CID_PRIMARY_BOUND_TO_SHARED,
        )]
    names = ", ".join(f"{f.project_name} ({f.bound})" for f in findings)
    return [d.Finding(
        probe="kg_primary_shared", status=d.STATUS_PROBLEM,
        summary=(f"{len(findings)} project(s) use the SHARED KG collection as their "
                 f"primary KG: {names}"),
        fix=d.FIX_DEFER, condition_id=CID_PRIMARY_BOUND_TO_SHARED,
        command=remedy(findings),
        detail={
            "projects": [f.project_name for f in findings],
            "bindings": [{"project_id": f.project_id, "project": f.project_name,
                          "bound": f.bound, "expected": f.expected} for f in findings],
        },
    )]


def doctor_entry(finding: Any):
    """The ``kg_primary_bound_to_shared`` ledger entry (one builder)."""
    from vco_lib.deferral_report import DeferralEntry

    return DeferralEntry(
        condition_id=CID_PRIMARY_BOUND_TO_SHARED,
        title="Project(s) use the shared KG collection as their own primary KG",
        detected=finding.summary,
        why_deferred=(
            "Each listed project's PRIMARY knowledge-graph binding names the "
            "collection every project reads as the SHARED KG. Its own notes then "
            "land in the shared corpus, and its searches treat the shared corpus as "
            "its own. Only the orchestrator root may use one collection for both "
            "roles. Older updates caused this: a project that had not synced its "
            "knowledge yet was re-pointed to the only populated KG collection on the "
            "machine, which was the shared one (fixed in v0.2.105). The repair "
            "re-points the binding only; it moves and deletes nothing, and notes the "
            "project already wrote stay in the shared collection. Re-sync the "
            "project's knowledge folder afterwards to fill its own collection. The "
            "entry clears itself once no project is bound this way."
        ),
        command_to_apply=finding.command,
        severity="warning",
        disposition="action_required",
        # A dismissal holds for THIS set of projects; a newly affected project
        # re-fires the entry (registry dismiss_key = ["projects"]).
        dismiss_fields={"projects": sorted(
            str(p) for p in ((getattr(finding, "detail", None) or {}).get("projects") or []))},
        kg_node_refs=["docs/features/04-knowledge-and-code-graph.md"],
    )


def still_bound(db_path: Optional[Path] = None) -> Optional[bool]:
    """Registry clear probe: True while any project is still bound this way,
    False once none is, None when ``launcher.db`` cannot be read."""
    findings = find(db_path)
    if findings is None:
        return None
    return bool(findings)


def candidate_is_shared(project_id: str, candidate: str,
                        db_path: Optional[Path] = None) -> Optional[bool]:
    """Would binding ``candidate`` as ``project_id``'s PRIMARY make it use the
    shared collection? The launcher's binding writers ask this before they
    write (the Identity tab's KG field, the KG tab's picker, re-detect from
    disk). Always False for the orchestrator root; None when launcher.db
    cannot be read."""
    try:
        from vco_lib.launcher_db_reader import _open_db_readonly

        conn = _open_db_readonly(db_path)
    except Exception:  # noqa: BLE001 — cannot look
        return None
    if conn is None:
        return None
    try:
        conn.row_factory = None
        row = conn.execute("SELECT slug, host FROM projects WHERE id = ?",
                           (project_id,)).fetchone()
        if row is not None and _is_root(str(row[1] or ""), str(row[0] or "")):
            return False
        return names_shared_for(candidate, shared_collection_names(conn),
                                _roles_of(conn, project_id))
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def project_status(project_id: str, db_path: Optional[Path] = None,
                   candidate: Optional[str] = None) -> dict:
    """The launcher's reading for ONE project (the reply of
    ``python -m vco_lib.kg_primary_shared status``): ``bound_to_shared`` plus
    the bound and name-derived classes when it is. With ``candidate`` the
    reply also carries ``candidate_is_shared`` (the writers' pre-write
    check). ``ok: false`` when launcher.db cannot be read — the caller then
    shows / decides nothing rather than guessing."""
    if candidate is not None:
        verdict = candidate_is_shared(project_id, candidate, db_path)
        if verdict is None:
            return {"ok": False, "error": "launcher_db_unreadable",
                    "message": "launcher.db could not be read"}
        return {"ok": True, "candidate": candidate, "candidate_is_shared": verdict}
    findings = find(db_path)
    if findings is None:
        return {"ok": False, "error": "launcher_db_unreadable",
                "message": "launcher.db could not be read"}
    hit = next((f for f in findings if f.project_id == project_id), None)
    if hit is None:
        return {"ok": True, "bound_to_shared": False, "bound": "", "expected": "",
                "expected_is_shared": False}
    return {"ok": True, "bound_to_shared": True, "bound": hit.bound,
            "expected": hit.expected, "expected_is_shared": hit.expected_is_shared}


def main(argv: Optional[list] = None) -> int:
    """``python -m vco_lib.kg_primary_shared status --project-id <id> --json``
    (the launcher's Identity tab) — one JSON object on stdout, exit 0; an
    unreadable launcher.db is ``ok: false`` with exit 1."""
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="python -m vco_lib.kg_primary_shared")
    sub = parser.add_subparsers(dest="verb", required=True)
    st = sub.add_parser("status", help="is this project's primary KG the shared one?")
    st.add_argument("--project-id", required=True)
    st.add_argument("--db", type=Path, default=None, help="launcher.db (default: discovered)")
    st.add_argument("--candidate", default=None,
                    help="judge this class name as the project's primary instead")
    st.add_argument("--json", action="store_true", help="JSON reply (the only output form)")
    args = parser.parse_args(argv)
    reply = project_status(args.project_id, args.db, args.candidate)
    print(json.dumps(reply))
    return 0 if reply.get("ok") else 1


__all__ = [
    "CID_PRIMARY_BOUND_TO_SHARED", "DEFAULT_SHARED_KG_COLLECTION", "GUI_ACTION",
    "PrimaryBoundToShared", "candidate_is_shared", "doctor_entry", "find", "find_in_conn",
    "main", "names_shared_for", "probe", "project_status", "remedy",
    "shared_collection_names", "still_bound",
]


if __name__ == "__main__":  # pragma: no cover - exercised through the launcher bridge
    import sys

    sys.exit(main())
