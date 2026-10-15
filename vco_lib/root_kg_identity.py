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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

#: White-label / dev-clone override (highest priority).
OVERRIDE_ENV = "VCT_ORCHESTRATOR_ROOT_KG_COLLECTION"
#: launcher.db ``app_state`` key the launcher seeds the root binding from.
POINTER_KEY = "orchestrator_root_kg_collection"

SOURCE_OVERRIDE = "override"
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
           override: Optional[str], *, pending_fill: bool = False) -> RootKgDecision:
    """The root's KG name (pure). ``live_counts`` maps every live class name
    to its row count (``None`` = exists, count unknown); ``live_counts=None``
    means Weaviate's state is unknown. ``record_model`` is the model the
    bound class was written with (None = not known)."""
    derived = derived_name(root)
    ov = (override or "").strip()
    known = live_counts is not None
    names = list(live_counts or ())
    if ov:
        blocked = case_sibling(ov, names) if known and ov not in names else None
        return RootKgDecision(ov, SOURCE_OVERRIDE, derived,
                              f"{OVERRIDE_ENV} is set", True, blocked)
    b = (bound or "").strip()
    prefix = ""
    if b:
        if not known:
            return RootKgDecision(b, SOURCE_UNKNOWN, derived,
                                  f"Weaviate state unknown — keeping the bound '{b}'", False)
        rows = live_counts.get(b) if b in names else 0  # type: ignore[union-attr]
        if rows is None:
            return RootKgDecision(b, SOURCE_UNKNOWN, derived,
                                  f"could not count '{b}' — keeping it", False)
        if rows > 0:
            stale = bool(record_model and target_model and not pending_fill
                         and not _same_model(record_model, target_model))
            if not stale or b == derived:
                why = f"the bound '{b}' holds {rows} rows"
                if stale:
                    why += f" (written with {record_model}; it is also the folder-derived name)"
                return RootKgDecision(b, SOURCE_KEPT, derived, why)
            prefix = (f"the bound '{b}' was written with {record_model}, the profile is "
                      f"{target_model} and no fill toward it is pending; ")
        else:
            prefix = f"the bound '{b}' holds no rows; "
    if not known:
        return RootKgDecision(derived, SOURCE_UNKNOWN, derived,
                              f"{prefix}Weaviate state unknown — the folder-derived name", False)
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
           env: Optional[Mapping[str, str]] = None) -> RootKgDecision:
    """Read the live facts and :func:`decide`. ``live_names`` (an already
    fetched schema) and ``count`` are injectable; by default the schema is
    fetched and rows are counted with ``kg_binding_heal``'s Aggregate."""
    from vco_lib import weaviate_helpers as wh

    url = weaviate_url or wh.weaviate_url_default()
    environ = os.environ if env is None else env
    override = environ.get(OVERRIDE_ENV, "")
    b = bound if bound is not None else (bound_read or _bound_primary)()
    names = list(live_names) if live_names is not None else _live_class_names(url)
    live: Optional[dict] = None
    if names is not None:
        if count is None:
            from vco_lib.kg_binding_heal import _count_weaviate_class_objects as count
        live = {n: None for n in names}
        if b and b in live:
            live[b] = count(url, b)
    record_model, pending = (None, False)
    target = ""
    if b and live is not None and (live.get(b) or 0) > 0:
        try:
            from vco_lib.kg_embed_model import target_text_model

            target = target_text_model()
        except Exception:  # noqa: BLE001 — no target = never "stale"
            target = ""
        record_model, pending = _model_facts(b, target, url)
    return decide(root, b, live, record_model, target, override, pending_fill=pending)


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
