# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The orchestrator root's KG identity — ONE decision (v0.2.103).

Owner ruling 2026-10-15: the orchestrator root owns the FOLDER-DERIVED KG name
its install creates (``derive_project_kg_name(<root folder>)``), and the
launcher and the hub use exactly that name. On the root one class serves both
roles — the root's own KG and the machine's shared KG — so "the root's name"
and "the shared name" are the same answer.

Before this module the answer was decided in five places with five rules
(install step 7b's case-insensitive adoption, the root seed's "SHARED wins",
the V44-G1 env-vs-DB-vs-Weaviate tiebreak, the launcher's migration-028
pointer default, and the hub's casing rebind), and they disagreed whenever the
folder-derived name and the shipped constant differed only in case
(``VibecodedOrchestrator_KnowledgeGraph`` for a ``vibecoded-orchestrator``
clone vs ``VibeCodedOrchestrator_KnowledgeGraph``). Each surface then "healed"
toward a different class.

The rule (:func:`decide`, pure):

1. ``VCT_ORCHESTRATOR_ROOT_KG_COLLECTION`` set → that name (white-label
   override, top priority).
1b. The Identity-tab pick (``app_state['shared_kg.collection_name']``,
   :data:`PICKER_KEY`) → that name. The hub and the launcher serve the pick
   FIRST as the machine's shared name, and on the root one class serves both
   roles, so the root follows it (review V02103-FINAL2 L-1). The pick is the
   user's explicit consent — but it is subject to the same embedding-profile
   rule as ``adopt``: a populated picked class written with a different model
   (and no fill toward the current one pending) is NOT followed; the decision
   reports it in ``picked_refused`` and install records the
   ``root_kg_pick_wrong_embedding`` deferral. A pick that holds no rows has
   no vectors to mismatch and is followed (blocked by a case-only sibling like
   any other name).
2. The root's existing PRIMARY binding is KEPT when that exact class holds rows
   — unless it was positively written with a different embedding model than
   the current profile (the recorded model, else one read off live objects)
   and no fill toward the current profile is pending. Weaviate state unknown
   → keep (never decide on a blind read).
3. Otherwise the folder-derived name.

Never adopted automatically: a class the root did not write. A case-only
sibling of the decided name blocks creating it (Weaviate refuses two classes
that differ only in case); that is reported as ``blocked_by`` and becomes a
consent deferral — never a drop, never a silent rebind. Adoption of an
existing class is the explicit ``adopt --yes`` CLI below, and only for a class
written with the current embedding model.

Callers: install.py step 7b (collection bootstrap), step 7c (root seed), the
launcher.db pointer persist (``app_state['orchestrator_root_kg_collection']``,
which the launcher seeds the root binding from and the hub reads), and this
module's CLI. Rust does not re-decide: the launcher seeds from the persisted
pointer and the hub serves the bound name byte-for-byte (``config_api.rs``).

CLI::

    python -m vco_lib.root_kg_identity decide [--root PATH] [--json]
    python -m vco_lib.root_kg_identity adopt --collection NAME --yes [--root PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

#: White-label / dev-clone override (highest priority).
OVERRIDE_ENV = "VCT_ORCHESTRATOR_ROOT_KG_COLLECTION"
#: launcher.db ``app_state`` key the launcher seeds the root binding from.
POINTER_KEY = "orchestrator_root_kg_collection"
#: The Identity-tab shared-KG pick (``set_shared_kg_collection_name``,
#: ``project_identity.rs``). The ONE Python home of this key: every Python
#: reader imports it from here. MUST MATCH
#: ``launcher/src-tauri/src/commands/project_env_settings.rs::
#: APP_STATE_KEY_SHARED_KG_NAME`` and the hub's read in ``config_api.rs``.
PICKER_KEY = "shared_kg.collection_name"

SOURCE_OVERRIDE = "override"
SOURCE_PICKED = "picked"
SOURCE_KEPT = "kept"
SOURCE_DERIVED = "derived"
SOURCE_UNKNOWN = "kept-unknown"


@dataclass(frozen=True)
class RootKgDecision:
    """The root's KG class name and why."""

    name: str
    source: str
    derived: str
    reason: str
    #: False when the answer rests on a blind read (Weaviate state unknown):
    #: callers then neither persist nor rebind.
    state_known: bool = True
    #: A live class that differs from ``name`` only in case — creating
    #: ``name`` is impossible until the user consents (``adopt``) or removes it.
    blocked_by: Optional[str] = None
    #: An Identity-tab pick that was NOT followed because it holds vectors
    #: from another embedding model (the profile rule); None otherwise.
    picked_refused: Optional[str] = None

    @property
    def persistable(self) -> bool:
        return bool(self.name) and self.state_known and not self.blocked_by

    def to_dict(self) -> dict:
        return {**asdict(self), "persistable": self.persistable}


def derived_name(root) -> str:
    """The folder-derived KG name of the root at ``root``."""
    from vco_lib.project_init import derive_project_kg_name

    return derive_project_kg_name(Path(root).name or "")


def case_sibling(name: str, live_names: Iterable[str]) -> Optional[str]:
    """A live class differing from ``name`` only in case, else None."""
    if not name:
        return None
    lowered = name.lower()
    for other in live_names:
        if other != name and other.lower() == lowered:
            return other
    return None


def _same_model(a: str, b: str) -> bool:
    from vco_lib.kg_embed_model import same_text_model

    return same_text_model(a, b)


def decide(root, bound: Optional[str], live_counts: Optional[Mapping[str, Optional[int]]],
           record_model: Optional[str], target_model: Optional[str],
           override: Optional[str], *, pending_fill: bool = False,
           picked: Optional[str] = None, picked_model: Optional[str] = None,
           picked_pending: bool = False) -> RootKgDecision:
    """The root's KG name (pure). ``live_counts`` maps every live class name
    to its row count (``None`` = exists, count unknown); ``live_counts=None``
    means Weaviate's state is unknown. ``record_model`` is the model the
    bound class was written with (None = not known); ``picked`` is the
    Identity-tab pick and ``picked_model`` the model it was written with."""
    derived = derived_name(root)
    ov = (override or "").strip()
    known = live_counts is not None
    names = list(live_counts or ())
    if ov:
        blocked = case_sibling(ov, names) if known and ov not in names else None
        return RootKgDecision(ov, SOURCE_OVERRIDE, derived,
                              f"{OVERRIDE_ENV} is set", True, blocked)
    pk = (picked or "").strip()
    refused: Optional[str] = None
    prefix = ""
    if pk:
        if not known:
            return RootKgDecision(pk, SOURCE_UNKNOWN, derived,
                                  f"Weaviate state unknown — keeping the Identity-tab pick '{pk}'",
                                  False)
        p_rows = live_counts.get(pk) if pk in names else 0  # type: ignore[union-attr]
        if p_rows is None:
            return RootKgDecision(pk, SOURCE_UNKNOWN, derived,
                                  f"could not count the Identity-tab pick '{pk}' — keeping it", False)
        same = bool(picked_model and target_model and _same_model(picked_model, target_model))
        if p_rows == 0 or same or picked_pending:
            blocked = case_sibling(pk, names) if pk not in names else None
            return RootKgDecision(pk, SOURCE_PICKED, derived,
                                  "the Identity-tab pick (your explicit choice)", True, blocked)
        refused = pk
        prefix = (f"the Identity-tab pick '{pk}' was written with "
                  f"{picked_model or 'an unknown model'}, the profile is "
                  f"{target_model or 'unknown'} — not followed; ")
    decision = _decide_unpicked(derived, bound, live_counts, names, known,
                                record_model, target_model, pending_fill, prefix)
    if refused:
        decision = replace(decision, picked_refused=refused)
    return decision


def _decide_unpicked(derived: str, bound: Optional[str],
                     live_counts: Optional[Mapping[str, Optional[int]]], names: list,
                     known: bool, record_model: Optional[str], target_model: Optional[str],
                     pending_fill: bool, prefix: str) -> RootKgDecision:
    """Rules 2-3 of :func:`decide` (the bound class kept, else derived)."""
    b = (bound or "").strip()
    if b:
        if not known:
            return RootKgDecision(b, SOURCE_UNKNOWN, derived,
                                  f"{prefix}Weaviate state unknown — keeping the bound '{b}'", False)
        rows = live_counts.get(b) if b in names else 0  # type: ignore[union-attr]
        if rows is None:
            return RootKgDecision(b, SOURCE_UNKNOWN, derived,
                                  f"{prefix}could not count '{b}' — keeping it", False)
        if rows > 0:
            stale = bool(record_model and target_model and not pending_fill
                         and not _same_model(record_model, target_model))
            if not stale or b == derived:
                why = f"{prefix}the bound '{b}' holds {rows} rows"
                if stale:
                    why += f" (written with {record_model}; it is also the folder-derived name)"
                return RootKgDecision(b, SOURCE_KEPT, derived, why)
            prefix += (f"the bound '{b}' was written with {record_model}, the profile is "
                       f"{target_model} and no fill toward it is pending; ")
        else:
            prefix += f"the bound '{b}' holds no rows; "
    if not known:
        return RootKgDecision(derived, SOURCE_UNKNOWN, derived,
                              f"{prefix}Weaviate state unknown — the folder-derived name", False)
    # Owner ruling 2026-10-15 (review V02103-FINAL2 I-1): a live class under
    # EXACTLY the folder-derived name is the root's own and is used as-is —
    # no consent step, even when an older uninstalled install left it (its
    # vectors from another profile stay in their own named slot and
    # `kg_embed_model`'s record/fill handles the switch, so nothing is lost).
    # Only a CASE-sibling needs consent (it may be another install's class).
    blocked = case_sibling(derived, names) if derived not in names else None
    return RootKgDecision(derived, SOURCE_DERIVED, derived,
                          f"{prefix}the folder-derived name", True, blocked)


# ── I/O edges ───────────────────────────────────────────────────────────────


def _live_class_names(weaviate_url: str) -> Optional[list]:
    from vco_lib import weaviate_helpers as wh

    try:
        status, body = wh.http_request("GET", f"{weaviate_url}/v1/schema", timeout=10)
        if status != 200:
            return None
        return sorted(wh.schema_class_names(json.loads(body)))
    except Exception:  # noqa: BLE001 — unreachable = unknown, never "empty"
        return None


def picked_shared_name() -> Optional[str]:
    """The Identity-tab pick (:data:`PICKER_KEY`), stripped; None when unset,
    empty, or launcher.db is unreadable. The ONE Python reader of the key."""
    try:
        from vco_lib.launcher_db_reader import _read_app_state_key_normalized

        return _read_app_state_key_normalized(PICKER_KEY)
    except Exception:  # noqa: BLE001 — no DB = no pick
        return None


def resolve_shared_name(folder=None, *, fallback: str) -> str:
    """THIS machine's shared-KG class name, as seen from ``folder`` — the ONE
    Python home of the order (moved from ``project_init._resolve_shared_kg_name``,
    which is now a shim over it).

    ``fallback`` (``project_init._SHARED_KG_NAME``) is VCO's LAST-RESORT
    default, not a description of the install: on an adopt-and-route install
    the root's own collection serves both roles, so the generic literal names a
    class that does not exist (field: one project's shared-KG searches returned
    nothing for three days and no component reported an error).

    Resolution order (first non-empty wins), all soft-fail:

    0. The Identity-tab pick (:func:`picked_shared_name`) — Priority 1 in the
       hub (``config_api.rs``), the launcher (``resolve_shared_kg_collection``
       / ``populate``) and ``config_projection``; review V02103-FINAL2 L-1.
    1. This project's own ``shared`` KG binding in launcher.db.
    2. The project's existing on-disk ``SHARED_KG_COLLECTION`` pin — a value
       already in ``.claude/settings.json`` is a user/launcher decision, and an
       explicit empty string is the documented "cross-project fan-out
       intentionally disabled" choice, honoured verbatim.
    3. The ORCHESTRATOR-ROOT project's ``shared`` binding, then its ``primary``.
    4. ``fallback`` — a genuinely launcher-less standalone install, where this
       name is also what bootstrap will create.

    Never raises.
    """
    # Imported OUTSIDE the swallow-all tries so a broken install is loud.
    from vco_lib.kg_binding_read import _read_kg_binding_override

    picked = picked_shared_name()
    if picked:
        return picked
    if folder is not None:
        try:
            val = (_read_kg_binding_override(Path(folder)).get("shared_kg_collection") or "").strip()
            if val:
                return val
        except Exception:  # noqa: BLE001
            pass
        try:
            from vco_lib.knowledge_residue import project_settings_env

            existing = project_settings_env(Path(folder)).get("SHARED_KG_COLLECTION")
            if isinstance(existing, str):
                return existing.strip()
        except Exception:  # noqa: BLE001
            pass
    try:
        from vco_lib.launcher_db_reader import get_orchestrator_root_bindings

        root_primary, root_shared = get_orchestrator_root_bindings()
        for cand in (root_shared, root_primary):
            if isinstance(cand, str) and cand.strip():
                return cand.strip()
    except Exception:  # noqa: BLE001
        pass
    return fallback


def _bound_primary() -> Optional[str]:
    try:
        from vco_lib.launcher_db_reader import get_orchestrator_root_bindings

        return get_orchestrator_root_bindings()[0]
    except Exception:  # noqa: BLE001 — no DB = no binding yet
        return None


def _model_facts(bound: str, target: str, weaviate_url: str) -> "tuple[Optional[str], bool]":
    """``(the model `bound` was written with, a fill toward `target` is pending)``."""
    from vco_lib import kg_embed_model as kem

    try:
        rec = kem.read_record(bound) or kem.record_from_live(bound, target,
                                                             weaviate_url=weaviate_url)
    except Exception:  # noqa: BLE001
        rec = None
    try:
        fill = kem.read_fill(bound) or {}
    except Exception:  # noqa: BLE001
        fill = {}
    pending = bool(target and fill.get("target") and _same_model(str(fill["target"]), target))
    return (rec.model if rec is not None else None), pending


def gather(root, *, weaviate_url: Optional[str] = None,
           live_names: Optional[Iterable[str]] = None,
           count: Optional[Callable[[str, str], Optional[int]]] = None,
           bound: Optional[str] = None, bound_read: Optional[Callable[[], Optional[str]]] = None,
           env: Optional[Mapping[str, str]] = None,
           picked_read: Optional[Callable[[], Optional[str]]] = None) -> RootKgDecision:
    """Read the live facts and :func:`decide`. ``live_names`` (an already
    fetched schema) and ``count`` are injectable; by default the schema is
    fetched and rows are counted with ``kg_binding_heal``'s Aggregate."""
    from vco_lib import weaviate_helpers as wh

    url = weaviate_url or wh.weaviate_url_default()
    environ = os.environ if env is None else env
    override = environ.get(OVERRIDE_ENV, "")
    b = bound if bound is not None else (bound_read or _bound_primary)()
    pk = (picked_read or picked_shared_name)() if not override.strip() else None
    names = list(live_names) if live_names is not None else _live_class_names(url)
    live: Optional[dict] = None
    if names is not None:
        if count is None:
            from vco_lib.kg_binding_heal import _count_weaviate_class_objects as count
        live = {n: None for n in names}
        for cls in {c for c in (b, pk) if c and c in live}:
            live[cls] = count(url, cls)
    target: Optional[str] = None

    def _target() -> str:
        nonlocal target
        if target is None:
            try:
                from vco_lib.kg_embed_model import target_text_model

                target = target_text_model()
            except Exception:  # noqa: BLE001 — no target = never "stale"
                target = ""
        return target

    record_model, pending = (None, False)
    if b and live is not None and (live.get(b) or 0) > 0:
        record_model, pending = _model_facts(b, _target(), url)
    picked_model, picked_pending = (None, False)
    if pk and live is not None and (live.get(pk) or 0) > 0:
        picked_model, picked_pending = _model_facts(pk, _target(), url)
    return decide(root, b, live, record_model, target or "", override, pending_fill=pending,
                  picked=pk, picked_model=picked_model, picked_pending=picked_pending)


PICK_REFUSED_CONDITION_ID = "root_kg_pick_wrong_embedding"


def pick_refused_entry(decision: RootKgDecision):
    """The deferral for an Identity-tab pick the profile rule did not follow
    (install steps 7b and 7c record it; same condition id → idempotent)."""
    from vco_lib.deferral_report import DeferralEntry

    pick = decision.picked_refused or ""
    return DeferralEntry(
        condition_id=PICK_REFUSED_CONDITION_ID,
        title="Identity-tab shared-KG pick holds another embedding model's vectors",
        detected=(f"The shared KG picked in the launcher's Identity tab is '{pick}', but it was "
                  f"written with a different embedding model than the current profile, so the "
                  f"orchestrator root keeps '{decision.name}' ({decision.reason}). The hub and "
                  f"the launcher still serve '{pick}' as every project's shared KG until you "
                  f"choose."),
        why_deferred=("Searching a class with the wrong model's vectors returns wrong results; "
                      "VCO never re-embeds, drops or rebinds it on its own."),
        command_to_apply=("In the launcher: Identity tab → Manage shared KG collection → pick a "
                          f"class written with the current embedding profile (e.g. "
                          f"'{decision.name}'), or switch Preferences → Embedding profile (global) "
                          f"to the model '{pick}' was written with; then re-run the update"),
        severity="warning",
        kg_node_refs=[],
    )


# ── the consent path ────────────────────────────────────────────────────────


def adopt(root, collection: str, *, weaviate_url: Optional[str] = None,
          out=print) -> int:
    """Bind the root to an EXISTING ``collection`` the user explicitly chose.
    Refused unless it exists under exactly that name, holds rows, and was
    written with the current embedding model. Writes the launcher.db pointer
    and the root's primary + shared binding rows; the env surfaces follow on
    the next update. Never touches the class itself."""
    from vco_lib import weaviate_helpers as wh
    from vco_lib.kg_binding_heal import _count_weaviate_class_objects

    url = weaviate_url or wh.weaviate_url_default()
    names = _live_class_names(url)
    if names is None:
        out(f"refused: Weaviate at {url} did not answer — nothing changed")
        return 1
    if collection not in names:
        out(f"refused: no class named exactly '{collection}' exists — nothing changed")
        return 1
    rows = _count_weaviate_class_objects(url, collection)
    if not rows:
        out(f"refused: '{collection}' holds no rows ({rows}) — nothing to adopt")
        return 1
    try:
        from vco_lib.kg_embed_model import target_text_model

        target = target_text_model()
    except Exception as exc:  # noqa: BLE001
        out(f"refused: the current embedding profile could not be resolved ({exc})")
        return 1
    model, _pending = _model_facts(collection, target, url)
    if not model or not _same_model(model, target):
        out(f"refused: '{collection}' was written with {model or 'an unknown model'}, the "
            f"current profile is {target} — adopting it would search the wrong vectors")
        return 1
    return _write_adoption(collection, out=out)


def _write_adoption(collection: str, *, out=print) -> int:
    import sqlite3
    import time

    from vco_lib.kg_binding_heal import rebind_orchestrator_root_bindings
    from vco_lib.launcher_db_reader import _discover_db_path, get_orchestrator_root_project_id

    db_path = _discover_db_path()
    if db_path is None:
        out("refused: launcher.db not found — start the launcher once, then re-run")
        return 1
    pid = get_orchestrator_root_project_id()
    now_ms = int(time.time() * 1000)
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            conn.execute(
                "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (POINTER_KEY, collection, now_ms))
            # A standing Identity-tab pick of ANOTHER class would out-rank this
            # adoption on the next decide; the newer explicit choice wins.
            conn.execute("UPDATE app_state SET value = ?, updated_at = ? "
                         "WHERE key = ? AND value != ?",
                         (collection, now_ms, PICKER_KEY, collection))
            if pid:
                rebind_orchestrator_root_bindings(conn.cursor(), project_id=pid,
                                                  canonical=collection, now_ms=now_ms)
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        out(f"failed: launcher.db write ({exc}) — nothing changed")
        return 1
    out(f"adopted '{collection}' as the orchestrator root's KG. Run the orchestrator update "
        "(`python install.py --update` or the launcher's Update) to project it into the env files.")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vco_lib.root_kg_identity",
                                     description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_decide = sub.add_parser("decide", help="print the root's KG name and why (read-only)")
    p_decide.add_argument("--root", default=None, help="orchestrator root (default: this checkout)")
    p_decide.add_argument("--json", action="store_true")
    p_adopt = sub.add_parser("adopt", help="bind the root to an existing class (consent)")
    p_adopt.add_argument("--root", default=None)
    p_adopt.add_argument("--collection", required=True)
    p_adopt.add_argument("--yes", action="store_true",
                         help="confirm: you chose this class and it holds the root's knowledge")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else Path(__file__).resolve().parent.parent
    if args.cmd == "decide":
        decision = gather(root)
        if args.json:
            print(json.dumps(decision.to_dict()))
        else:
            print(f"{decision.name} ({decision.source}: {decision.reason})")
            if decision.picked_refused:
                print(f"not followed: the Identity-tab pick '{decision.picked_refused}' holds "
                      "vectors from another embedding model — pick a class written with the "
                      "current profile, or switch the profile")
            if decision.blocked_by:
                print(f"blocked: '{decision.blocked_by}' differs only in case — adopt it with "
                      f"`python -m vco_lib.root_kg_identity adopt --collection "
                      f"{decision.blocked_by} --yes`, or remove it yourself")
        return 0
    if not args.yes:
        print("refused: adopting a collection needs your explicit consent — add --yes",
              file=sys.stderr)
        return 2
    return adopt(root, args.collection)


if __name__ == "__main__":
    sys.exit(main())
