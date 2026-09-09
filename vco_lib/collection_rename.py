# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``vco project rename-collections`` — carry a project's populated Weaviate
collections to the class names its NEW name derives (v0.2.92 WP-18 / W14).

WHY
---
Since v0.2.89 a rename is identity-PRESERVING: it changes ``projects.name`` +
``slug`` and nothing else, because the pre-.89 propagation moved the code-graph
prefix while ``project_kg_bindings`` stayed put — a half-update by construction
that pointed reads at an empty class set. That made rename SAFE and left a
capability missing: renaming "Old Name" keeps ``OldName_KnowledgeGraph``
forever. This module is that capability, as ONE consented operation that
carries the DATA rather than just re-pointing a label.

THE FAILURE MODE THIS IS DESIGNED AGAINST
-----------------------------------------
The dangerous outcome is not a crash. It is a project that keeps its data and
loses the ability to find it — a binding naming a class that does not exist.
Measured on this machine: 3 of 5 ``codegraph-prefix-generation.json`` records
name a prefix matching ZERO live classes. So:

1. **Nothing is ever dropped** — not on cleanup, not on rollback, not when a
   class looks empty. The old family is RETAINED; :func:`drop_retired` is the
   only deleting path and it re-validates at run time.
2. **Nothing moves without confirming BOTH endpoints** — source present with
   the expected count, destination absent (or created by THIS rename, per the
   sentinel). Every probe is tri-state; "could not check" REFUSES.
3. **Bindings and collections move together** as far as is achievable, which is
   stated honestly below rather than claimed.

FAILURE SEMANTICS — resumable, ONE durable commit point, NOT atomic
-------------------------------------------------------------------
No transaction spans Weaviate and SQLite; claiming atomicity would be the
promise this cycle keeps finding false. What is provided instead:

* **One durable commit point** — the flip (name/slug + KG binding rows + code
  prefix) is ONE SQLite transaction in the sanctioned writer
  ``db/bindings_writer.rs::commit_collection_rename``.
* **A copy idempotent by construction** — ``_copy_collection_with_vectors``
  passes ``uuid=obj.uuid``, so repeating it re-writes the SAME objects instead
  of duplicating. An interrupted copy is resumed, not repaired.
* **An interrupted state that is VISIBLE** — the sentinel records the phase
  (``copying`` / ``verified`` / ``flipped``) and every destination class this
  rename CREATED. No phase's crash reads as "done": success DELETES the
  sentinel, so its presence always means work is owed.

    plan -> claim -> copy -> verify -> **FLIP** -> reconcile -> release
    |______ nothing committed; any failure is a clean refusal ______|

THE CODE-GRAPH IDENTITY RE-MINT, AND WHY IT IS POST-FLIP
--------------------------------------------------------
Code rows carry a ``project`` property mixed into the analyzer's deterministic
UUID seed. After the copy those rows still name the OLD prefix, so the next
analyze would mint a SECOND generation for the same entities (the v0.2.82 G3
duplicate shape). The fix is the existing identity migration in
:mod:`vco_lib.codegraph_vector_copy` — already "the ONE identity-copy home" —
which re-mints UUIDs REUSING the stored vector verbatim. It runs POST-flip
because it may legitimately change the object count (true duplicates dedupe),
and a count change before the verify gate would make the gate unable to tell a
dedup from a lost row. Re-analysis is not the alternative: it re-embeds, which
this operation exists to avoid.

TWO FAMILIES, TWO SANITIZERS, ZERO LOCAL REGEXES
------------------------------------------------
The KG family uses the underscore-DROPPING ``sanitize_for_weaviate_class``; the
code family the underscore-PRESERVING ``canonical_class_prefix``. Both are
reached through their existing homes (``derive_project_collection_names`` /
``derive_project_code_prefix``). No name-derivation rule lives here — that
divergence is the R2 bug and re-deriving locally is the easiest way back to it.
"""
from __future__ import annotations

import errno
import json
import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from vco_lib.atomic import atomic_write_json
from typing import Any, Callable, Mapping, Optional, Sequence

__all__ = [
    "SENTINEL_REL", "COMPLETED_REL", "STALE_AFTER_SECONDS",
    "CID_OLD_RETAINED", "CID_RECONCILE_PENDING", "CID_IDENTITY_REMINT_INCOMPLETE",
    "REFUSAL_REASONS", "RenameError", "RenameRefused", "ClassMove", "RenamePlan",
    "WeaviateOps", "default_ops", "plan_rename", "current_family",
    "claim_sentinel", "read_sentinel", "write_sentinel", "clear_sentinel",
    "rename_status", "execute_copy", "verify_copy", "execute_reconcile",
    "write_completed_record", "drop_retired", "drop_retired_command",
    "quote_for_shell", "emit_rename_deferrals",
]

#: The in-flight claim, relative to the PROJECT folder.
SENTINEL_REL = Path(".claude") / "context" / ".vco-collection-rename.json"
#: Where a COMPLETED rename records what it retired, so the drop command has
#: something to re-validate. Distinct from the sentinel, which success removes.
COMPLETED_REL = Path(".claude") / "state" / "collection-rename-last.json"

#: How long a ``copying`` claim may sit before another run may take it over.
#: Same six hours as the move's claim: the copy is minutes-long on a large code
#: family and a tighter bound would let a second run start mid-write.
STALE_AFTER_SECONDS = 6 * 60 * 60

PHASE_COPYING = "copying"
PHASE_VERIFIED = "verified"
PHASE_FLIPPED = "flipped"
PHASES = (PHASE_COPYING, PHASE_VERIFIED, PHASE_FLIPPED)

CID_OLD_RETAINED = "collections_renamed_old_retained"
CID_RECONCILE_PENDING = "collection_rename_reconcile_pending"
CID_IDENTITY_REMINT_INCOMPLETE = "collection_rename_identity_remint_incomplete"

ROLE_KG_PRIMARY = "kg_primary"
ROLE_DEVELOPMENT = "development"
ROLE_DIAGRAMS = "diagrams"
FAMILY_KG = "kg"
FAMILY_CODE = "code"

ACTION_COPY = "copy"
ACTION_CREATE_EMPTY = "create-empty"
ACTION_SOURCE_ABSENT = "source-absent"
ACTION_RESUME = "resume"
ACTION_NOOP = "noop"

REFUSAL_REASONS: dict[str, str] = {
    "project_not_found":
        "no registered project matches that id or slug (exact match only — a "
        "fuzzy match for an operation that moves data is not a convenience).",
    "orchestrator_root":
        "the orchestrator-root project's slug is canonical and several "
        "auto-heal paths match on it; moving its collection family would "
        "silently disable them.",
    "no_change":
        "the new name derives the family the project already uses — there is "
        "nothing to carry.",
    "degenerate_name":
        "the new name does not sanitize to a usable Weaviate class name.",
    "weaviate_unreachable":
        "the live Weaviate schema could not be read, so neither endpoint can "
        "be confirmed. 'I could not check' must refuse.",
    "count_unreadable":
        "an object count could not be read, so the copy could not be verified "
        "against anything.",
    "destination_exists":
        "a destination class already exists and this rename did not create "
        "it. Writing into a class VCO did not make is how two projects end up "
        "sharing one collection.",
    "prefix_collision":
        "the new name derives a family another registered project is bound to.",
    "rename_in_flight":
        "a rename of this project is already claimed, or one flipped and has "
        "not been reconciled. Run --status, then --resume.",
    "move_in_flight":
        "a folder move of this project is in progress; a move and a rename "
        "re-point the same rows and must not interleave.",
    "build_in_flight":
        "a code-graph build is pending or running; it would write into the "
        "family this operation is carrying.",
    "writer_unavailable":
        "the launcher database writer (vct-hub) is not reachable, so the flip "
        "could not be committed. Refusing before anything is copied.",
    "shared_kg_designation":
        "this project designates the collection being renamed as the SHARED "
        "KG that every other project reads. Re-designate it first.",
    "sentinel_unwritable":
        "the in-flight claim could not be written to the project folder, so a "
        "second run could not be prevented.",
}


class RenameError(RuntimeError):
    """A step failed after it had begun."""


class RenameRefused(RenameError):
    """A precondition was not met. NOTHING was done."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{REFUSAL_REASONS.get(reason, reason)} {detail}".strip())


def quote_for_shell(value: "str | Path", *, platform: Optional[str] = None) -> str:
    """Quote ``value`` for the shell the user is most likely holding.

    ``platform`` is ``os.name``; it is a parameter so the WINDOWS shape is
    unit-testable from Linux (R14: pin the shape of every OS-dependent
    decision; this repo cannot run the other two OSes in CI).

    POSIX gets :func:`shlex.quote`. Windows gets double quotes with embedded
    ``"`` doubled, because ``cmd.exe`` does not understand single quotes at
    all — a POSIX-quoted path pasted at a Windows prompt is a command that
    cannot run, and a printed command that cannot run is worse than none.
    """
    text = str(value)
    if (platform or os.name) == "nt":
        return '"' + text.replace('"', '""') + '"'
    return shlex.quote(text)


# ───────────────────────────────────────────────────────────────────────────
# Plan
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class ClassMove:
    """One source class and the class this rename would carry it to."""

    role: str
    family: str
    src: str
    dst: str
    action: str
    src_count: Optional[int] = None
    dst_count: Optional[int] = None
    note: str = ""

    def to_json(self) -> dict:
        return {
            "role": self.role, "family": self.family, "src": self.src,
            "dst": self.dst, "action": self.action, "src_count": self.src_count,
            "dst_count": self.dst_count, "note": self.note,
        }


@dataclass
class RenamePlan:
    """Everything the operation would do, computed read-only."""

    project_id: str
    project_name: str
    new_name: str
    folder: str
    old_code_prefix: str
    new_code_prefix: str
    moves: list[ClassMove] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    resuming_from: Optional[dict] = None

    @property
    def carried_objects(self) -> int:
        return sum(m.src_count or 0 for m in self.moves)

    @property
    def retired_classes(self) -> list[str]:
        """The OLD class names. This operation never drops them."""
        return [m.src for m in self.moves if m.action != ACTION_SOURCE_ABSENT]

    @property
    def replacements(self) -> dict[str, str]:
        return {m.src: m.dst for m in self.moves
                if m.action != ACTION_SOURCE_ABSENT}

    def kg_flip_payload(self) -> list[dict]:
        """``[{"role": "primary", "collection": ...}]``.

        Only the PRIMARY KG role exists as a row; ``_Development`` /
        ``_Diagrams`` are derived from it by suffix swap (the v0.2.84 D1
        one-rule derivation), so flipping primary carries all three. Emitting
        a row per sibling would invent binding roles the schema does not have.
        """
        return [{"role": "primary", "collection": m.dst}
                for m in self.moves if m.role == ROLE_KG_PRIMARY]

    def to_json(self) -> dict:
        return {
            "project_id": self.project_id, "project_name": self.project_name,
            "new_name": self.new_name, "folder": self.folder,
            "old_code_prefix": self.old_code_prefix,
            "new_code_prefix": self.new_code_prefix,
            "moves": [m.to_json() for m in self.moves],
            "carried_objects": self.carried_objects,
            "retired_classes": self.retired_classes,
            "warnings": list(self.warnings), "resuming_from": self.resuming_from,
        }


# ───────────────────────────────────────────────────────────────────────────
# Weaviate I/O behind ONE injectable seam
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class WeaviateOps:
    """Every Weaviate interaction, as injectable callables.

    A seam rather than direct calls because the engine's tests must exercise
    the whole flow — including the branch that refuses a populated destination
    and the one that aborts a verify — and none of that may touch a real
    server. :func:`default_ops` wires production implementations; each is an
    EXISTING home, never a reimplementation.
    """

    probe: Callable[[Sequence[str]], Mapping[str, Optional[bool]]]
    count: Callable[[str], Optional[int]]
    create_class: Callable[[Mapping[str, Any]], None]
    copy_class: Callable[[str, str], int]
    sample_vectors: Callable[[str, int], Mapping[str, Any]]
    fetch_vectors: Callable[[str, Sequence[str]], Mapping[str, Any]]
    delete_class: Callable[[str], None]
    remint_identity: Callable[..., Mapping[str, Any]]


def default_ops(weaviate_url: Optional[str] = None) -> WeaviateOps:
    """Production wiring. Every callable delegates to an existing home."""
    from vco_lib import project_init as _pi
    from vco_lib import weaviate_helpers as _wh

    url = weaviate_url or _wh.weaviate_url_default()

    def _sample(name: str, limit: int) -> Mapping[str, Any]:
        client = _pi._connect_v4_client(weaviate_url=url)
        try:
            out: dict[str, Any] = {}
            for obj in client.collections.get(name).iterator(include_vector=True):
                out[str(obj.uuid)] = obj.vector
                if len(out) >= limit:
                    break
            return out
        finally:
            client.close()

    def _fetch(name: str, uuids: Sequence[str]) -> Mapping[str, Any]:
        client = _pi._connect_v4_client(weaviate_url=url)
        try:
            col = client.collections.get(name)
            out: dict[str, Any] = {}
            for uid in uuids:
                try:
                    obj = col.query.fetch_object_by_id(uid, include_vector=True)
                except Exception:  # noqa: BLE001 — a miss is a verify failure
                    obj = None
                out[str(uid)] = obj.vector if obj is not None else None
            return out
        finally:
            client.close()

    def _remint(prefix: str, old_identity: str, new_identity: str,
                *, dry_run: bool = False) -> Mapping[str, Any]:
        from vco_lib import codegraph_vector_copy as _cvc

        client = _pi._connect_v4_client(weaviate_url=url)
        try:
            s = _cvc.migrate_project_identity(
                client, prefix, old_identity, new_identity, dry_run=dry_run)
            return {"moved": s.moved, "deduped": s.deduped, "left": s.left,
                    "failures": s.failures, "line": s.summary_line()}
        finally:
            client.close()

    return WeaviateOps(
        probe=lambda names: _pi.probe_classes_exist(list(names), url),
        count=lambda name: _wh.http_count_objects(name, url),
        create_class=lambda payload: _pi._create_class(dict(payload),
                                                       weaviate_url=url),
        copy_class=lambda s, d: _pi._copy_collection_with_vectors(
            s, d, weaviate_url=url),
        sample_vectors=_sample,
        fetch_vectors=_fetch,
        delete_class=lambda name: _pi._delete_class(name, weaviate_url=url),
        remint_identity=_remint,
    )


def _probe_or_refuse(ops: WeaviateOps, names: Sequence[str]) -> dict[str, bool]:
    """Probe ``names``; REFUSE on any ``None``.

    ``probe_classes_exist`` is tri-state and ``None`` means the schema could
    not be read. Reading it as ``False`` here would let a rename create a
    destination that already exists — the exact conflation
    :class:`vco_lib.weaviate_helpers.ProbeResult` exists to make impossible.
    """
    wanted = [n for n in names if n]
    if not wanted:
        return {}
    try:
        raw = ops.probe(wanted)
    except Exception as exc:  # noqa: BLE001 — could not check != absent
        raise RenameRefused("weaviate_unreachable",
                            f"({type(exc).__name__}: {exc})") from exc
    unknown = sorted(n for n in wanted if raw.get(n) is None)
    if unknown:
        raise RenameRefused(
            "weaviate_unreachable",
            f"could not determine whether these classes exist: "
            f"{', '.join(unknown)}")
    return {n: bool(raw.get(n)) for n in wanted}


# ───────────────────────────────────────────────────────────────────────────
# Reading the CURRENT family — binding-first, never name-derived
# ───────────────────────────────────────────────────────────────────────────


def current_family(project_id: str, project_name: str, *,
                   db_path: Optional[Path] = None) -> dict:
    """The collection family this project uses TODAY.

    BINDING-FIRST (the v0.2.82/84 rule): the KG collection is the ``primary``
    binding row and the code prefix the code-graph binding row. Only when a
    binding is genuinely ABSENT does the name-derived value stand in — and the
    result records WHICH, because a derived source is a guess and the caller
    must be able to say so.

    A database that cannot be READ is not "no bindings": it raises, so the
    caller refuses rather than renaming a family it inferred.
    """
    from vco_lib import project_init as _pi
    from vco_lib.config_projection import probe_codegraph_binding_prefix
    from vco_lib.launcher_db_reader import _open_db_readonly

    derived = _pi.derive_project_collection_names(project_name)
    out = {
        "kg": derived["kg_collection"],
        "development": derived["development_collection"],
        "diagrams": derived["diagrams_collection"],
        "code_prefix": _pi.derive_project_code_prefix(project_name),
        "kg_source": "derived", "code_source": "derived", "shared": None,
    }
    conn = _open_db_readonly(db_path)
    if conn is None:
        raise RenameRefused(
            "writer_unavailable",
            "the launcher database could not be opened read-only, so the "
            "project's CURRENT collection bindings are unknown.")
    try:
        for r in conn.execute(
            "SELECT role, collection_name FROM project_kg_bindings "
            "WHERE project_id = ?", (project_id,)
        ):
            name = (r["collection_name"] or "").strip()
            if not name:
                continue
            if r["role"] == "primary":
                out["kg"] = name
                out["kg_source"] = "binding"
                dev, diag = _pi._dev_diagrams_from_primary(
                    name, derived["kg_basename"])
                out["development"], out["diagrams"] = dev, diag
            elif r["role"] == "shared":
                out["shared"] = name
        probe = probe_codegraph_binding_prefix(conn, project_id)
        if probe.is_unknown():
            raise RenameRefused(
                "writer_unavailable",
                f"the code-graph binding could not be read ({probe.reason}); "
                f"the current code prefix is therefore unknown.")
        bound = probe.or_none()
        if bound:
            out["code_prefix"] = bound
            out["code_source"] = "binding"
    except RenameRefused:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RenameRefused(
            "writer_unavailable",
            f"reading the project's bindings failed "
            f"({type(exc).__name__}: {exc}).") from exc
    finally:
        _close(conn)
    return out


def _close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _bound_names(*, db_path: Optional[Path] = None,
                 exclude_project: Optional[str] = None) -> tuple[set, set]:
    """``(kg_collection_names, code_prefixes)`` bound in launcher.db.

    ``exclude_project`` omits one project's own rows (the collision check);
    omitting it returns EVERY binding (the drop guard, where "bound by anyone"
    is the property that makes a drop unsafe).

    An unreadable database RAISES: "no bindings found" and "I could not look"
    must not be the same answer to a question guarding a delete.
    """
    from vco_lib.launcher_db_reader import _open_db_readonly

    conn = _open_db_readonly(db_path)
    if conn is None:
        raise RenameRefused(
            "writer_unavailable",
            "the launcher database could not be read, so no class can be "
            "proven bound or unbound.")
    kg: set = set()
    code: set = set()
    try:
        for r in conn.execute(
            "SELECT project_id, collection_name FROM project_kg_bindings"
        ):
            if r["project_id"] == exclude_project:
                continue
            if (r["collection_name"] or "").strip():
                kg.add(r["collection_name"].strip())
        try:
            for r in conn.execute(
                "SELECT project_id, collection_prefix "
                "FROM project_codegraph_bindings"
            ):
                if r["project_id"] == exclude_project:
                    continue
                if (r["collection_prefix"] or "").strip():
                    code.add(r["collection_prefix"].strip())
        except Exception:  # noqa: BLE001 — table absent on a pre-migration DB
            pass
    finally:
        _close(conn)
    return kg, code


def _refuse_if_in_flight(project_id: str, *,
                         db_path: Optional[Path] = None) -> None:
    """Refuse while a move or a code-graph build is live for this project.

    Both write rows this operation flips. A MISSING table is tolerated (an
    older launcher.db has no ``project_moves``) — an unreadable database
    already refused in :func:`current_family`.
    """
    from vco_lib.launcher_db_reader import _open_db_readonly

    conn = _open_db_readonly(db_path)
    if conn is None:
        return
    try:
        for sql, reason, fmt in (
            ("SELECT id, status FROM project_moves WHERE project_id = ? "
             "AND status IN ('running','flipped') LIMIT 1",
             "move_in_flight",
             lambda r: f"(move {r['id']}, status {r['status']})"),
            ("SELECT status FROM code_graph_builds WHERE project_id = ? "
             "AND status IN ('pending','running') LIMIT 1",
             "build_in_flight",
             lambda r: f"(status {r['status']})"),
        ):
            try:
                row = conn.execute(sql, (project_id,)).fetchone()
            except Exception:  # noqa: BLE001 — table absent: nothing in flight
                continue
            if row is not None:
                raise RenameRefused(reason, fmt(row))
    finally:
        _close(conn)


def _project_host(project_id: str, db_path: Optional[Path]) -> Optional[str]:
    from vco_lib.launcher_db_reader import _open_db_readonly

    conn = _open_db_readonly(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT host FROM projects WHERE id = ?",
                           (project_id,)).fetchone()
        return str(row["host"]) if row else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        _close(conn)


# ───────────────────────────────────────────────────────────────────────────
# Planning — the ONE planner (dry-run, live run and GUI preview all call it)
# ───────────────────────────────────────────────────────────────────────────


def plan_rename(selector: str, new_name: str, *,
                ops: Optional[WeaviateOps] = None,
                db_path: Optional[Path] = None,
                weaviate_url: Optional[str] = None,
                host_of: Optional[Callable[[str], Optional[str]]] = None,
                ) -> RenamePlan:
    """Read-only. Everything the rename would do, or a :class:`RenameRefused`."""
    from vco_lib import project_init as _pi
    from vco_lib import project_move as _pm

    ops = ops or default_ops(weaviate_url)
    rows = _pm.registered_projects(db_path)
    project = _pm.resolve_project_row(selector, rows)
    if project is None:
        raise RenameRefused("project_not_found", f"selector {selector!r}")
    project_id = str(project["id"])
    folder = Path(str(project["folder_path"]))

    host = host_of(project_id) if host_of else _project_host(project_id, db_path)
    if host == "orchestrator_root":
        raise RenameRefused("orchestrator_root")

    new_name = (new_name or "").strip()
    target = _pi.derive_project_collection_names(new_name)
    new_prefix = _pi.derive_project_code_prefix(new_name)
    if not target["kg_basename"] or not new_prefix:
        raise RenameRefused(
            "degenerate_name",
            f"{new_name!r} sanitizes to KG basename "
            f"{target['kg_basename']!r} / code prefix {new_prefix!r}")

    current = current_family(project_id, str(project["name"]), db_path=db_path)
    old_prefix = current["code_prefix"]
    _refuse_if_in_flight(project_id, db_path=db_path)

    sentinel = read_sentinel(folder)
    resuming: Optional[dict] = None
    if sentinel is not None:
        if sentinel.get("phase") == PHASE_FLIPPED:
            raise RenameRefused(
                "rename_in_flight",
                "a previous rename FLIPPED and its reconciliation is still "
                "owed. The collections and bindings already moved.")
        same = (str(sentinel.get("new_name") or "").strip() == new_name
                and str(sentinel.get("project_id") or "") == project_id)
        age = time.time() - float(sentinel.get("started_at") or 0)
        if not same and age < STALE_AFTER_SECONDS:
            raise RenameRefused(
                "rename_in_flight",
                f"a rename to {sentinel.get('new_name')!r} started "
                f"{int(age)}s ago is still claimed.")
        resuming = dict(sentinel)

    if current.get("shared") and current["shared"] == current["kg"]:
        raise RenameRefused(
            "shared_kg_designation",
            f"this project designates {current['kg']!r} as the SHARED KG.")

    old_names = {ROLE_KG_PRIMARY: current["kg"],
                 ROLE_DEVELOPMENT: current["development"],
                 ROLE_DIAGRAMS: current["diagrams"]}
    new_names = {ROLE_KG_PRIMARY: target["kg_collection"],
                 ROLE_DEVELOPMENT: target["development_collection"],
                 ROLE_DIAGRAMS: target["diagrams_collection"]}
    code_pairs = [(f"{old_prefix}{s}", f"{new_prefix}{s}")
                  for s in _pi._CODEGRAPH_SUFFIXES]

    if (all(old_names[r].lower() == new_names[r].lower() for r in old_names)
            and old_prefix.lower() == new_prefix.lower()):
        raise RenameRefused(
            "no_change", f"the family is already {current['kg']!r} / "
                         f"{old_prefix!r}.")

    # Collision with a PEER's bindings, CASE-INSENSITIVELY: Weaviate class
    # names are case-sensitive but every VCO adoption path treats a case
    # variant as the same logical class (BUG-1).
    peer_kg, peer_code = _bound_names(db_path=db_path, exclude_project=project_id)
    if new_names[ROLE_KG_PRIMARY].lower() in {n.lower() for n in peer_kg}:
        raise RenameRefused(
            "prefix_collision",
            f"{new_names[ROLE_KG_PRIMARY]!r} is another project's bound KG "
            f"collection.")
    if new_prefix.lower() in {n.lower() for n in peer_code}:
        raise RenameRefused(
            "prefix_collision",
            f"code prefix {new_prefix!r} is another project's bound prefix.")

    live = _probe_or_refuse(
        ops,
        list(old_names.values()) + list(new_names.values())
        + [s for s, _ in code_pairs] + [d for _, d in code_pairs])
    created_before = set((resuming or {}).get("created_classes") or [])
    moves: list[ClassMove] = []

    def _add(role: str, family: str, src: str, dst: str) -> None:
        if src.lower() == dst.lower():
            moves.append(ClassMove(role, family, src, dst, ACTION_NOOP,
                                   note="this member's name does not change"))
            return
        if not live.get(src, False):
            # ALREADY-DAMAGED: the binding names a class that does not exist.
            moves.append(ClassMove(
                role, family, src, dst, ACTION_SOURCE_ABSENT,
                note="the source class does not exist in Weaviate — no data "
                     "is carried; the destination is created empty so the new "
                     "binding names a real class"))
            return
        n = ops.count(src)
        if n is None:
            raise RenameRefused("count_unreadable",
                                f"the object count of {src!r} could not be read.")
        if live.get(dst, False):
            if dst not in created_before:
                raise RenameRefused(
                    "destination_exists",
                    f"{dst!r} already exists and no in-flight rename of this "
                    f"project created it.")
            moves.append(ClassMove(
                role, family, src, dst, ACTION_RESUME,
                src_count=n, dst_count=ops.count(dst),
                note="created by an interrupted run of this same rename; the "
                     "copy is UUID-preserving, so repeating it re-writes the "
                     "same objects"))
            return
        moves.append(ClassMove(role, family, src, dst,
                               ACTION_COPY if n else ACTION_CREATE_EMPTY,
                               src_count=n))

    for role in (ROLE_KG_PRIMARY, ROLE_DEVELOPMENT, ROLE_DIAGRAMS):
        _add(role, FAMILY_KG, old_names[role], new_names[role])
    for src, dst in code_pairs:
        _add(src.rsplit("_", 1)[-1], FAMILY_CODE, src, dst)

    warnings: list[str] = []
    if current["kg_source"] == "derived":
        warnings.append(
            "this project has no primary KG binding row, so its CURRENT KG "
            "collection was derived from its name. If the real collection is "
            "called something else, register the binding first.")
    if current["code_source"] == "derived":
        warnings.append(
            "this project has no code-graph binding row, so its CURRENT code "
            "prefix was derived from its name.")
    absent = [m.src for m in moves if m.action == ACTION_SOURCE_ABSENT]
    if absent:
        warnings.append(
            "these bound classes do not exist in Weaviate and carry no data: "
            + ", ".join(absent) + ". They are created empty at the new names "
            "so nothing ends up bound to a class that is missing.")

    return RenamePlan(
        project_id=project_id, project_name=str(project["name"]),
        new_name=new_name, folder=str(folder), old_code_prefix=old_prefix,
        new_code_prefix=new_prefix, moves=moves, warnings=warnings,
        resuming_from=resuming)


# ───────────────────────────────────────────────────────────────────────────
# The sentinel — the in-flight claim AND the interrupted-state record
# ───────────────────────────────────────────────────────────────────────────


def sentinel_path(folder: "str | Path") -> Path:
    return Path(folder) / SENTINEL_REL


def claim_sentinel(folder: "str | Path", payload: Mapping[str, Any]) -> Path:
    """Create the sentinel with ``O_EXCL``: an ATOMIC claim, or a refusal.

    ``O_CREAT|O_EXCL`` is atomic on POSIX and on Windows, so two concurrent
    runs cannot both believe they hold the claim. What it does NOT survive is
    a killed process — that is what :data:`STALE_AFTER_SECONDS` and the
    planner's takeover branch are for. Saying so is cheaper than a future
    reader assuming the lock is stronger than it is.
    """
    path = sentinel_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise RenameRefused("rename_in_flight",
                                f"claim already held at {path}") from exc
        raise RenameRefused("sentinel_unwritable", f"{path}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps(dict(payload), indent=2).encode("utf-8"))
    except OSError as exc:
        raise RenameRefused("sentinel_unwritable", f"{path}: {exc}") from exc
    return path


def write_sentinel(folder: "str | Path", payload: Mapping[str, Any]) -> None:
    """Overwrite an EXISTING claim (phase transitions). Never creates one."""
    # v0.2.92: through the ONE atomic writer; `mode=0o600` keeps the claim
    # owner-only, matching `claim_sentinel`'s O_CREAT|O_EXCL 0o600 create.
    atomic_write_json(sentinel_path(folder), dict(payload), mode=0o600)


def read_sentinel(folder: "str | Path") -> Optional[dict]:
    try:
        data = json.loads(sentinel_path(folder).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — absent or unreadable -> no claim
        return None
    return data if isinstance(data, dict) else None


def clear_sentinel(folder: "str | Path") -> bool:
    try:
        sentinel_path(folder).unlink()
        return True
    except Exception:  # noqa: BLE001
        return False


def rename_status(folder: "str | Path") -> dict:
    """What an interrupted rename left behind, in the words the user needs.

    The ``flipped`` wording is deliberately unambiguous: it must never read as
    "something failed, your data may be gone". The data moved; the follow-up
    did not finish.
    """
    s = read_sentinel(folder)
    if s is None:
        return {"in_flight": False, "phase": None,
                "summary": "No rename is in flight at this folder."}
    phase = str(s.get("phase") or "")
    created = list(s.get("created_classes") or [])
    if phase == PHASE_FLIPPED:
        summary = (
            f"A rename to {s.get('new_name')!r} COMMITTED: the collections "
            f"were copied and the project's bindings now name them. The "
            f"post-flip reconciliation did not finish. Nothing is lost, the "
            f"old classes are still there, and re-running with --resume "
            f"finishes it.")
    else:
        summary = (
            f"A rename to {s.get('new_name')!r} stopped during '{phase}'. "
            f"NOTHING was committed: the project still uses its previous "
            f"collections and still works. {len(created)} destination "
            f"class(es) were created and hold a partial copy; re-running the "
            f"same rename resumes it (the copy preserves UUIDs, so it "
            f"re-writes rather than duplicates).")
    return {"in_flight": True, "phase": phase, "new_name": s.get("new_name"),
            "created_classes": created, "started_at": s.get("started_at"),
            "summary": summary}


def _bump_sentinel(plan: RenamePlan, phase: str, created: Sequence[str]) -> None:
    s = read_sentinel(plan.folder) or {}
    s.update({
        "schema": "vco.collection_rename.v1", "project_id": plan.project_id,
        "project_name": plan.project_name, "new_name": plan.new_name,
        "phase": phase, "created_classes": list(created),
        "old_code_prefix": plan.old_code_prefix,
        "new_code_prefix": plan.new_code_prefix,
        "retired_classes": plan.retired_classes,
        "replacements": plan.replacements,
        "started_at": s.get("started_at") or time.time(),
        "updated_at": time.time(),
    })
    write_sentinel(plan.folder, s)


# ───────────────────────────────────────────────────────────────────────────
# Copy + verify — PRE-FLIP: nothing is committed, every failure is a refusal
# ───────────────────────────────────────────────────────────────────────────


def _target_definition(move: ClassMove, plan: RenamePlan) -> dict:
    """The TARGET schema for one destination class, from the schema homes."""
    from vco_lib import project_init as _pi

    if move.family == FAMILY_KG:
        if move.role == ROLE_KG_PRIMARY:
            return _pi.kg_class_definition(move.dst)
        if move.role == ROLE_DEVELOPMENT:
            return _pi.development_class_definition(move.dst)
        return _pi.diagrams_class_definition(move.dst)
    defs = _pi.code_class_definitions(f"{plan.new_code_prefix}_")
    return defs[move.dst[len(plan.new_code_prefix) + 1:]]


def execute_copy(plan: RenamePlan, *, ops: Optional[WeaviateOps] = None,
                 weaviate_url: Optional[str] = None,
                 progress: Optional[Callable[[str], None]] = None) -> dict:
    """Create every destination class and copy its source into it.

    Idempotent: the copy preserves UUIDs, so a destination left half-populated
    by an interrupted run converges on a repeat instead of duplicating.

    The sentinel is updated with each class as it is CREATED, BEFORE its copy
    starts — so a crash mid-copy still records that VCO owns that destination,
    which is exactly what lets the next run resume instead of refusing it as
    somebody else's data.
    """
    ops = ops or default_ops(weaviate_url)
    say = progress or (lambda _m: None)
    created: list[str] = list((plan.resuming_from or {}).get("created_classes") or [])
    copied: dict[str, int] = {}

    for move in plan.moves:
        if move.action == ACTION_NOOP:
            continue
        if move.dst not in created:
            say(f"creating {move.dst}")
            ops.create_class(_target_definition(move, plan))
            created.append(move.dst)
            _bump_sentinel(plan, PHASE_COPYING, created)
        if move.action == ACTION_SOURCE_ABSENT or not move.src_count:
            copied[move.dst] = 0
            continue
        say(f"copying {move.src} -> {move.dst} ({move.src_count} objects)")
        copied[move.dst] = ops.copy_class(move.src, move.dst)
    _bump_sentinel(plan, PHASE_COPYING, created)
    return {"created": created, "copied": copied}


def _vectors_equal(a: Any, b: Any) -> bool:
    """Slot-wise EXACT equality of two named-vector payloads.

    Exact, not approximate: the copy re-imports the stored floats verbatim, so
    anything but equality means the round-trip changed the data and the flip
    must not happen. A tolerance here would hide precisely that.
    """
    if not isinstance(a, dict) or not isinstance(b, dict) or set(a) != set(b):
        return False
    for slot, va in a.items():
        vb = b[slot]
        if not isinstance(va, (list, tuple)) or not isinstance(vb, (list, tuple)):
            return False
        if len(va) != len(vb) or any(x != y for x, y in zip(va, vb)):
            return False
    return True


def verify_copy(plan: RenamePlan, *, ops: Optional[WeaviateOps] = None,
                weaviate_url: Optional[str] = None, sample: int = 8) -> dict:
    """THE GATE. Returns a report, or raises :class:`RenameRefused` to abort.

    Three independent checks, all of which must pass before ONE binding moves:

    1. every destination class is PRESENT (tri-state probe; ``unknown`` aborts);
    2. object counts are EQUAL per class (``None`` — could not read — aborts);
    3. a sample of UUIDs read back from the destination carries a
       byte-identical named-vector payload.

    On abort NOTHING is flipped and the destination classes are LEFT IN PLACE.
    Deleting them would be a drop, and this operation does not drop; the
    sentinel already records that VCO created them, so the next run resumes
    into the same classes rather than colliding with them.
    """
    ops = ops or default_ops(weaviate_url)
    dsts = [m.dst for m in plan.moves if m.action != ACTION_NOOP]
    live = _probe_or_refuse(ops, dsts)
    missing = sorted(d for d in dsts if not live.get(d))
    if missing:
        raise RenameRefused(
            "destination_exists",
            f"verification failed: destination class(es) absent after the "
            f"copy: {', '.join(missing)}. Nothing was flipped.")

    counts: dict[str, dict] = {}
    for move in plan.moves:
        if move.action in (ACTION_NOOP, ACTION_SOURCE_ABSENT):
            continue
        src_n, dst_n = ops.count(move.src), ops.count(move.dst)
        if src_n is None or dst_n is None:
            unreadable = move.src if src_n is None else move.dst
            raise RenameRefused(
                "count_unreadable",
                f"could not read the object count of {unreadable!r} during "
                f"verification. Nothing was flipped.")
        counts[move.dst] = {"src": src_n, "dst": dst_n}
        if src_n != dst_n:
            raise RenameRefused(
                "count_unreadable",
                f"verification failed: {move.src} holds {src_n} object(s) but "
                f"{move.dst} holds {dst_n}. Nothing was flipped and nothing "
                f"was deleted.")

    sampled = 0
    for move in plan.moves:
        if move.action in (ACTION_NOOP, ACTION_SOURCE_ABSENT) or not move.src_count:
            continue
        src_vecs = ops.sample_vectors(move.src, sample)
        if not src_vecs:
            continue
        dst_vecs = ops.fetch_vectors(move.dst, list(src_vecs))
        for uid, vec in src_vecs.items():
            got = dst_vecs.get(uid)
            if got is None:
                raise RenameRefused(
                    "count_unreadable",
                    f"verification failed: object {uid} from {move.src} is not "
                    f"readable in {move.dst}. Nothing was flipped.")
            if not _vectors_equal(vec, got):
                raise RenameRefused(
                    "count_unreadable",
                    f"verification failed: the vector of object {uid} differs "
                    f"between {move.src} and {move.dst}. Nothing was flipped "
                    f"and nothing was deleted.")
            sampled += 1

    created = list((read_sentinel(plan.folder) or {}).get("created_classes") or [])
    _bump_sentinel(plan, PHASE_VERIFIED, created)
    return {"classes": counts, "vectors_sampled": sampled, "ok": True}


# ───────────────────────────────────────────────────────────────────────────
# Post-flip reconciliation — idempotent, re-runnable, never undoes the flip
# ───────────────────────────────────────────────────────────────────────────


def execute_reconcile(plan: RenamePlan, *, ops: Optional[WeaviateOps] = None,
                      weaviate_url: Optional[str] = None,
                      env_runner: Optional[Callable[..., Any]] = None,
                      remint: bool = True) -> dict:
    """Everything owed AFTER the durable commit. Each part soft-fails alone.

    A failure here never un-does the flip: the project HAS moved to the new
    family and works. What can still be owed is the env projection, the prefix
    generation record and the identity re-mint — all idempotent, all
    re-runnable, each reported so the ledger can name it.
    """
    ops = ops or default_ops(weaviate_url)
    folder = Path(plan.folder)
    out: dict[str, Any] = {"warnings": [], "env": None, "prefix_record": None,
                           "remint": None, "deferrals": []}

    # 1. Env surfaces, RE-DERIVED from the flipped rows (never a sed list).
    try:
        _reproject_env(plan, runner=env_runner)
        out["env"] = "reprojected"
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(
            f"the env surfaces (.claude/settings.json, .claude/env) could not "
            f"be re-projected: {exc}. KG routing may still name the previous "
            f"collection until this is re-run.")

    # 2. The code-graph prefix generation record, with its provenance.
    try:
        from vco_lib import codegraph_prefix_record as _cpr

        _cpr.write(folder, plan.new_code_prefix, source=_cpr.SOURCE_BINDING)
        out["prefix_record"] = plan.new_code_prefix
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"prefix generation record not updated: {exc}")

    # 3. Code-graph identity re-mint INSIDE the new classes (never a re-embed).
    if remint and plan.old_code_prefix.lower() != plan.new_code_prefix.lower():
        try:
            out["remint"] = dict(ops.remint_identity(
                plan.new_code_prefix, plan.old_code_prefix,
                plan.new_code_prefix, dry_run=False))
        except Exception as exc:  # noqa: BLE001
            out["warnings"].append(
                f"the code-graph identity re-mint did not run ({exc}). Rows in "
                f"the new classes still carry the previous identity.")

    out["deferrals"] = emit_rename_deferrals(plan, out)
    clear_sentinel(folder)
    return out


def _reproject_env(plan: RenamePlan, *,
                   runner: Optional[Callable[..., Any]]) -> None:
    """``python -m vco_lib.config_projection apply`` against the FLIPPED row.

    The projection derives every value from the database, so re-running it
    after the flip rewrites ``KG_COLLECTION`` / ``CODE_GRAPH_PROJECT`` without
    this module owning a list of keys to sed. Reuses W3's child-env builder
    rather than composing a second one.
    """
    import subprocess

    from vco_lib.project_move import build_child_env
    from vco_lib.python_exe import resolve_or_current

    # v0.2.94: the ONE resolver, not `sys.executable` — a rename can be driven
    # from the launcher, whose bundle path spawns Python via a bare PATH probe.
    argv = [resolve_or_current(), "-m", "vco_lib.config_projection", "apply",
            "--project-id", plan.project_id, "--folder", plan.folder]
    env = build_child_env({"CLAUDE_PROJECT_DIR": plan.folder,
                           "KG_BASE_DIR": plan.folder})
    proc = (runner(argv, env) if runner is not None
            else subprocess.run(argv, env=env, capture_output=True, text=True,
                                check=False))
    if getattr(proc, "returncode", 1) != 0:
        raise RenameError((getattr(proc, "stderr", "") or "").strip()[-400:]
                          or f"exit {getattr(proc, 'returncode', '?')}")


def write_completed_record(plan: RenamePlan) -> Path:
    """Record what this rename retired, so the drop command can re-validate it.

    The record is INPUT to the guard, never its authority: :func:`drop_retired`
    re-derives every fact (live schema, current bindings, current counts) at
    run time and the record only supplies the NAMES to check.
    """
    path = Path(plan.folder) / COMPLETED_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "vco.collection_rename_completed.v1",
        "project_id": plan.project_id, "previous_name": plan.project_name,
        "new_name": plan.new_name, "retired_classes": plan.retired_classes,
        "replacements": plan.replacements, "completed_at": time.time(),
    }
    atomic_write_json(path, payload)  # one home for tmp+os.replace (v0.2.92)
    return path


def _completed_record(folder: Path) -> Optional[dict]:
    try:
        data = json.loads((folder / COMPLETED_REL).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


# ───────────────────────────────────────────────────────────────────────────
# Deferrals
# ───────────────────────────────────────────────────────────────────────────


def drop_retired_command(folder: "str | Path", *,
                         platform: Optional[str] = None) -> str:
    """The ONE command the record hands the user.

    It names a real verb — ``vco project rename-collections --drop-retired``,
    which is :func:`drop_retired` behind ``--confirm``. Not a raw
    ``DELETE /v1/schema``, and not the unguarded codegraph drop that BUG-M2
    was: it re-derives the retired set at run time and refuses unless the old
    class is bound NOWHERE and its replacement still holds the data.
    """
    return ("vco project rename-collections --drop-retired --confirm --folder "
            + quote_for_shell(folder, platform=platform))


def _analyze_wrapper(folder: Path, *, platform: Optional[str] = None) -> str:
    """The bundled analyzer wrapper for ``folder``, as a real path.

    There is no ``vco codegraph`` verb. The analyzer ships as the BUNDLED
    wrapper in the project's own ``.claude/scripts/``, so that is what any
    remediation names — a printed command is shipped code.
    """
    nt = (platform or os.name) == "nt"
    return str(folder / ".claude" / "scripts"
               / ("code-graph-analyze.ps1" if nt else "code-graph-analyze"))


def emit_rename_deferrals(plan: RenamePlan,
                          reconcile: Mapping[str, Any]) -> list[str]:
    """Write the ledger entries this operation owes. Returns condition ids."""
    from vco_lib.deferral_emit import emit_entries
    from vco_lib.deferral_report import DeferralEntry

    folder = Path(plan.folder)
    entries: list[DeferralEntry] = []

    if plan.retired_classes:
        entries.append(DeferralEntry(
            condition_id=CID_OLD_RETAINED,
            title="Renamed collections: the previous classes were kept",
            detected=(
                f"The project's collections were carried to the names derived "
                f"from {plan.new_name!r}. The previous classes were NOT "
                f"dropped and still hold every object: "
                + ", ".join(plan.retired_classes) + "."),
            why_deferred=(
                "Weaviate collections are hours of embedding work. VCO never "
                "drops one as a cleanup step, so the retired family outlives "
                "the rename until you decide otherwise."),
            command_to_apply=drop_retired_command(folder),
            severity="info"))

    warnings = list(reconcile.get("warnings") or [])
    if warnings:
        entries.append(DeferralEntry(
            condition_id=CID_RECONCILE_PENDING,
            title="Collection rename: follow-up work did not finish",
            detected=(
                "The collections and the database bindings moved together and "
                "the project works. These follow-up steps did not complete: "
                + " | ".join(warnings)),
            why_deferred=(
                "The flip is durable and re-running the follow-up is safe; VCO "
                "does not undo a committed rename to retry a reconciliation "
                "step."),
            command_to_apply=("vco project rename-collections --resume "
                              "--folder " + quote_for_shell(folder)),
            severity="warning"))

    remint = reconcile.get("remint") or {}
    if remint and int(remint.get("left") or 0):
        entries.append(DeferralEntry(
            condition_id=CID_IDENTITY_REMINT_INCOMPLETE,
            title="Code-graph rows still carry the previous project identity",
            detected=(
                f"{remint.get('left')} row(s) under {plan.new_code_prefix!r} "
                f"could not be re-keyed to the new identity "
                f"({remint.get('line')}). They were LEFT in place — never "
                f"guessed at and never deleted."),
            why_deferred=(
                "A row whose identity key cannot be reconstructed from its "
                "stored properties cannot be re-minted without inventing one. "
                "The next full code-graph analysis rebuilds those rows under "
                "the new identity."),
            command_to_apply=(_analyze_wrapper(folder) + " . --project "
                              + quote_for_shell(plan.new_name)),
            severity="warning"))

    if entries:
        emit_entries(folder, entries)

    # PAIRED RESOLUTION, the other half of what the registry promises. An
    # emitter that only ever ADDS entries leaves a resumed rename carrying the
    # reminder it just satisfied — which is how a ledger silts up and stops
    # being read. Resolving is keyed on the condition ACTUALLY being gone this
    # run, not on the run having happened.
    emitted = {e.condition_id for e in entries}
    stale = [cid for cid in (CID_RECONCILE_PENDING,
                             CID_IDENTITY_REMINT_INCOMPLETE)
             if cid not in emitted]
    if stale:
        from vco_lib.deferral_emit import resolve_conditions

        resolve_conditions(folder, stale)
    return [e.condition_id for e in entries]


# ───────────────────────────────────────────────────────────────────────────
# The guarded drop — the ONLY place this feature deletes anything
# ───────────────────────────────────────────────────────────────────────────


def drop_retired(folder: "str | Path", *, confirm: bool = False,
                 ops: Optional[WeaviateOps] = None,
                 weaviate_url: Optional[str] = None,
                 db_path: Optional[Path] = None,
                 record: Optional[Mapping[str, Any]] = None) -> dict:
    """Drop the RETIRED classes of a completed rename. Re-validates first.

    Everything is re-derived AT RUN TIME; the record supplies only names. Five
    independent refusals, each leaving every class untouched:

    * no ``--confirm``;
    * no completed-rename record at this folder;
    * the live schema could not be read (tri-state — "could not check" refuses);
    * the retired class is STILL BOUND by any project's KG or code-graph
      binding, including this one (which is what a reverted or half-finished
      rename looks like);
    * its replacement is missing, or no longer holds at least as many objects.
      A drop is only safe while the replacement demonstrably still has the data.

    Returns a report; a refusal is data, not an exception.
    """
    folder = Path(folder)
    ops = ops or default_ops(weaviate_url)
    out: dict[str, Any] = {"ok": False, "dropped": [], "refused": [],
                           "checked": [], "errors": []}

    rec = dict(record) if record is not None else (_completed_record(folder) or {})
    retired = [str(x) for x in (rec.get("retired_classes") or []) if str(x).strip()]
    replacements = {str(k): str(v)
                    for k, v in (rec.get("replacements") or {}).items()}
    if not retired:
        out["refused"].append(
            "no completed rename record was found at this folder, so there is "
            "no retired class set to drop. Nothing was touched.")
        return out
    if not confirm:
        out["refused"].append(
            "refusing to drop without --confirm (this deletes Weaviate "
            "collections, which is hours of embedding work).")
        return out

    try:
        live = _probe_or_refuse(ops, retired + list(replacements.values()))
        bound_kg, bound_code = _bound_names(db_path=db_path)
    except RenameRefused as exc:
        out["refused"].append(f"{exc} Nothing was dropped.")
        return out

    from vco_lib.project_init import _CODEGRAPH_SUFFIXES

    bound = {n.lower() for n in bound_kg} | {
        f"{p}{s}".lower() for p in bound_code for s in _CODEGRAPH_SUFFIXES}

    for name in retired:
        out["checked"].append(name)
        replacement = replacements.get(name, "")
        if not live.get(name, False):
            out["refused"].append(
                f"{name}: already absent — nothing to drop (not an error).")
            continue
        if name.lower() in bound:
            out["refused"].append(
                f"{name}: STILL BOUND by a registered project. Dropping a "
                f"bound class is the failure this command exists to prevent.")
            continue
        if not replacement or not live.get(replacement, False):
            out["refused"].append(
                f"{name}: its replacement {replacement or '(unrecorded)'} does "
                f"not exist, so the data would not survive the drop.")
            continue
        old_n, new_n = ops.count(name), ops.count(replacement)
        if old_n is None or new_n is None:
            out["refused"].append(
                f"{name}: object counts could not be read, so the replacement "
                f"could not be proven to still hold the data.")
            continue
        if new_n < old_n:
            out["refused"].append(
                f"{name}: holds {old_n} object(s) but its replacement "
                f"{replacement} holds only {new_n}. Refusing.")
            continue
        try:
            ops.delete_class(name)
            out["dropped"].append(name)
        except Exception as exc:  # noqa: BLE001
            out["errors"].append({"collection": name,
                                  "error": f"{type(exc).__name__}: {exc}"})

    # PAIRED RESOLUTION: the record's job is to be the pointer to classes that
    # still exist. Once none of them does, it is describing nothing and must
    # stop being listed. Re-probed rather than inferred from `dropped` — a
    # class removed by hand between runs also ends the record.
    try:
        still_live = _probe_or_refuse(ops, retired)
        if not any(still_live.values()):
            from vco_lib.deferral_emit import resolve_conditions

            resolve_conditions(folder, [CID_OLD_RETAINED])
            out["record_resolved"] = True
    except RenameRefused:
        # Could not re-probe. The record stays — never resolved on a guess.
        pass

    out["ok"] = not out["errors"]
    return out
