# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Never create a class while a delete of its case-variant is still replayable
(v0.2.106).

Weaviate names a class's index and data folder by the LOWER-CASED class name,
and from 1.37 a start that replays a ``DELETE_CLASS`` drops that folder unless
a later ``ADD_CLASS`` of the EXACT same spelling follows
(:mod:`vco_lib.weaviate_raft_guard`). So "delete ``ProjectX_Kg``, create
``Projectx_Kg``" is safe only once a raft snapshot holds the delete; until
then every restart wipes the new class. VCO produced exactly that history
when it re-spelled the root collections (the project name "VibeCoded
Orchestrator" and the clone folder ``vibecoded-orchestrator`` sanitize to two
spellings).

The rule, in two halves:

* every VCO path that deletes a class records the name here
  (:func:`record_deleted`, with the raft log index the delete reached when
  Weaviate reports it);
* every VCO path that creates a class asks :func:`guard_create` first. When a
  recorded delete differs from the new name only in case and Weaviate may
  replay it (a release with the orphan drop, and no snapshot covering the
  delete yet), the guard waits for the next snapshot (the shipped compose
  takes one within a minute of any schema change) and refuses —
  :class:`CaseVariantCreateRefused`, loudly — if none arrives. A covered
  delete is forgotten.

The ledger is machine state (``<vct_root_dir>/state/weaviate_deleted_classes.json``),
keyed by the Weaviate URL. A failed ledger write never fails the delete it
records (the delete already happened); it is reported on stderr.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from vco_lib import weaviate_raft_guard as _rg

__all__ = [
    "CaseVariantCreateRefused",
    "ledger_path",
    "record_deleted",
    "delete_collection",
    "deleted_entries",
    "deleted_names",
    "guard_create",
    "is_lowercased_form",
    "lowercase_residuals",
]

#: Seconds :func:`guard_create` waits for a snapshot to cover a recorded
#: delete. The shipped compose checks every 30-60 s; 150 s covers two checks.
DEFAULT_WAIT_S = 150.0


class CaseVariantCreateRefused(RuntimeError):
    """Creating *name* now would put a live class behind a replayable delete
    of *deleted* (same lower-cased name)."""

    def __init__(self, name: str, deleted: str, reason: str) -> None:
        self.name, self.deleted = name, deleted
        super().__init__(
            f"refusing to create Weaviate class {name!r}: {reason}. Weaviate stores "
            f"{name!r} and the deleted {deleted!r} in the same lower-cased index, and from "
            "1.37 a restart that replays the delete drops the new class's data. Make the "
            "running Weaviate take raft snapshots (RAFT_SNAPSHOT_THRESHOLD/"
            "RAFT_SNAPSHOT_INTERVAL — the shipped compose sets them; `python install.py "
            "--update` recreates a VCO-managed container with them), check `python -m "
            "vco_lib.weaviate_raft_guard status` prints 'covered: yes', and run this again.")


def ledger_path() -> Path:
    from vco_lib.paths import vct_root_dir  # noqa: PLC0415

    return vct_root_dir() / "state" / "weaviate_deleted_classes.json"


def _key(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def _load() -> dict:
    try:
        data = json.loads(ledger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "instances": {}}
    if not isinstance(data, dict) or not isinstance(data.get("instances"), dict):
        return {"version": 1, "instances": {}}
    return data


def _save(data: dict) -> None:
    from vco_lib.atomic import atomic_write_text  # noqa: PLC0415

    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def record_deleted(name: str, weaviate_url: str, *,
                   http: Optional[_rg.HttpFn] = None, status: Optional[int] = None) -> None:
    """Remember that VCO deleted class *name* on *weaviate_url*. Call it right
    AFTER the delete; *status* is the DELETE's HTTP status when the caller has
    it (a 404 removed nothing and is not recorded). Never raises."""
    if not name or status == 404:
        return
    try:
        stats = _rg.read_raft_stats(weaviate_url, http=http)
        data = _load()
        bucket = data["instances"].setdefault(_key(weaviate_url), {})
        bucket[name] = {
            "deleted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "log_index": stats.last_log_index if stats is not None else None,
        }
        _save(data)
    except Exception as exc:  # noqa: BLE001 — the delete already happened
        print(f"  ! could not record the delete of Weaviate class {name!r} in "
              f"{ledger_path()}: {exc}", file=sys.stderr)


def delete_collection(client: Any, name: str, weaviate_url: str) -> None:
    """``client.collections.delete(name)`` (weaviate-client v4), recorded — the
    one home for a bare-client class delete."""
    client.collections.delete(name)
    record_deleted(name, weaviate_url)


def deleted_entries(weaviate_url: str) -> dict[str, Optional[int]]:
    """Every class name recorded as deleted on *weaviate_url* → the raft log
    index its delete reached (``None`` when Weaviate did not report it)."""
    bucket = _load()["instances"].get(_key(weaviate_url)) or {}
    return {name: (rec.get("log_index") if isinstance(rec, dict)
                   and isinstance(rec.get("log_index"), int) else None)
            for name, rec in sorted(bucket.items())}


def deleted_names(weaviate_url: str) -> list[str]:
    """Every class name recorded as deleted on *weaviate_url*."""
    return sorted(deleted_entries(weaviate_url))


def _forget(weaviate_url: str, names: list[str]) -> None:
    try:
        data = _load()
        bucket = data["instances"].get(_key(weaviate_url)) or {}
        for n in names:
            bucket.pop(n, None)
        _save(data)
    except Exception:  # noqa: BLE001 — a stale entry only costs a later re-check
        pass


def guard_create(
    name: str,
    weaviate_url: str,
    *,
    http: Optional[_rg.HttpFn] = None,
    sleep: Optional[Callable[[float], None]] = None,
    monotonic: Optional[Callable[[], float]] = None,
    wait_s: Optional[float] = None,
    poll_s: float = 3.0,
) -> None:
    """Raise :class:`CaseVariantCreateRefused` when creating *name* now could
    be undone by a replay of a recorded case-variant delete (module doc).
    A no-op for every name with no such record."""
    bucket = _load()["instances"].get(_key(weaviate_url)) or {}
    prior = [n for n in bucket if n != name and n.lower() == (name or "").lower()]
    if not prior:
        return
    version = _rg.read_server_version(weaviate_url, http=http)
    if version is not None and not _rg.has_orphan_drop(version):
        # No orphan drop on this release; VCO's move to one takes a snapshot
        # first (weaviate_raft_guard.upgrade_check), which covers the delete.
        return
    indexes = [bucket[n].get("log_index") for n in prior if isinstance(bucket.get(n), dict)]
    need: Optional[int] = (max(i for i in indexes if isinstance(i, int))
                           if any(isinstance(i, int) for i in indexes)
                           and all(isinstance(i, int) for i in indexes) else None)
    sleep_fn, clock = sleep or time.sleep, monotonic or time.monotonic
    budget = DEFAULT_WAIT_S if wait_s is None else wait_s
    start = clock()
    last = None
    while True:
        stats = _rg.read_raft_stats(weaviate_url, http=http)
        if stats is not None:
            last = stats
            if need is None:
                # The delete's index was not recorded: only a snapshot taken
                # after NOW is known to hold it.
                need = stats.last_log_index
            if stats.last_snapshot_index >= need:
                _forget(weaviate_url, prior)
                return
        if clock() - start >= budget:
            break
        sleep_fn(poll_s)
    reason = ("Weaviate's raft statistics could not be read" if last is None else
              f"the delete of {prior[0]!r} is not inside a raft snapshot yet (snapshot index "
              f"{last.last_snapshot_index} < {need}) after waiting {int(budget)} s")
    raise CaseVariantCreateRefused(name, prior[0], reason)


def is_lowercased_form(name: str, minted: str) -> bool:
    """True when *name* is *minted* with one or more letters lower-cased —
    the spelling an older VCO produced for a name it now mints mixed-case.

    The first character is compared case-insensitively (Weaviate capitalises a
    class name's first letter on create, so a fully lower-cased name is stored
    with an upper-case first letter). Every other character must be the
    minted one or its lower-case form, and at least one must differ — so the
    relation is one-way: the minted spelling is never the lower-cased form of
    its own residual.
    """
    if not name or not minted or name == minted or len(name) != len(minted):
        return False
    if name[0].lower() != minted[0].lower():
        return False
    return all(c == m or c == m.lower() for c, m in zip(name[1:], minted[1:])) \
        and name[1:] != minted[1:]


def lowercase_residuals(class_names: Any, suffixes: Any) -> dict[str, list[str]]:
    """Every live class that is the lower-cased form of a VCO-minted code-graph
    class also present → the minted siblings it shadows (sorted).

    A minted name ends in one of *suffixes* EXACTLY (``CodeFunction`` …); the
    residual may carry the suffix in any case (``_codefunction``). Pure: the
    caller reads the schema.
    """
    names = sorted({str(n) for n in class_names or () if n})
    sfx = tuple(str(x) for x in suffixes or ())
    by_lower: dict[str, list[str]] = {}
    for n in names:
        by_lower.setdefault(n.lower(), []).append(n)
    out: dict[str, list[str]] = {}
    for n in names:
        if not any(n.lower().endswith(x.lower()) for x in sfx):
            continue
        minted = [m for m in by_lower.get(n.lower(), [])
                  if any(m.endswith(x) for x in sfx) and is_lowercased_form(n, m)]
        if minted:
            out[n] = sorted(minted)
    return out
