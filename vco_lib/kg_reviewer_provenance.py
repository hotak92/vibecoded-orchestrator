# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — the provenance ledger of the KG reviewer's own sections.

The post-commit KG reviewer may FOLD its own earlier ``## Status after
<sha>`` / ``## Current state (consolidated <date>)`` sections into one
current-state section (owner request 2026-11-04), while every line the
OWNER wrote stays append-only. Which sections are "its own" must be proven,
never inferred: a commit subject (``kg-sync:``) or ``git blame`` alone says
who last TOUCHED a line, and ordinary owner actions — ``git commit --amend``
right after a reviewer commit, a fixup squashed into one, a user commit
titled ``kg-sync: ...``, a CRLF->LF rewrite of the whole file, a suffix the
reviewer appended to an owner line — all hand owner lines that attribution
(review V02105-KGREV2 H1/H2).

So the reviewer RECORDS what it wrote, at the moment its own commit is made
(:func:`vco_lib.kg_reviewer_guard.commit_reviewer_changes`): one entry per
section it originated, with the sha256 of the section's normalized bytes
and the commit sha. A section is foldable later only while its CURRENT
normalized bytes hash to an entry of the same node AND that commit is still
an ancestor of the commit being reviewed — so an amend, a rebase, a user
edit inside the section or a section the reviewer never wrote simply is
not foldable (the conservative direction). The decisions (what a section
is, which ones the reviewer originated, which ones are proven) live in
:mod:`vco_lib.kg_reviewer_guard`; this module is only the storage.

* **the ledger** — ``.claude/state/kg_reviewer_provenance.json``
  (git-excluded project state, never in version control): ``{"v": 1,
  "paths": {<node rel>: [<entry>, ...]}}``, each entry ``{"digest",
  "heading", "key", "kind", "commit", "recorded"}``.
* **bounded** — a node's entries are REPLACED on every record (entries whose
  section no longer exists in the committed node are dropped: they can
  never match again), and the whole ledger keeps at most
  :data:`MAX_ENTRIES` entries, the oldest dropped first with a log line.
* **never silent** — a ledger that cannot be read or written is LOGGED;
  the consequence is always the safe one (nothing proven -> nothing
  foldable -> plain append-only, exactly as before v0.2.105).

Line endings are normalized for HASHING only (CRLF read as LF); nothing
here ever rewrites a node.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence

__all__ = ["LEDGER_REL", "MAX_ENTRIES", "ledger_path", "section_digest",
           "load", "entries_for", "record"]

Log = Callable[[str], None]

#: The ledger, relative to the project folder that owns ``.claude/``.
LEDGER_REL = Path(".claude") / "state" / "kg_reviewer_provenance.json"
#: The lock guarding every read-modify-write of the ledger.
LOCK_NAME = "kg_reviewer_provenance.lock"
#: Ledger schema version (bump only on an incompatible shape).
LEDGER_VERSION = 1
#: Bound on the whole ledger (every node together). Far above what a
#: project accumulates (entries are per LIVE reviewer section), so a drop
#: is an exceptional, logged event.
MAX_ENTRIES = 5000


def _noop(_line: str) -> None:
    pass


def ledger_path(project_root: "str | Path") -> Path:
    return Path(project_root) / LEDGER_REL


def _lock_path(project_root: "str | Path") -> Path:
    return Path(project_root) / ".claude" / "state" / LOCK_NAME


def section_digest(lines: Iterable[str]) -> str:
    """sha256 of a section's lines, normalized for hashing ONLY: CR dropped
    from each line end (CRLF reads as LF) and trailing blank lines removed
    (the blank line before the next heading is layout, not content).
    Everything else — trailing spaces, indentation, every character — is
    hashed as written, so any edit of the section's text breaks the match."""
    out = [line[:-1] if line.endswith("\r") else line for line in lines]
    while out and not out[-1].strip():
        out.pop()
    return hashlib.sha256("\n".join(out).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty() -> Dict[str, object]:
    return {"v": LEDGER_VERSION, "paths": {}}


def load(project_root: "str | Path", log: Optional[Log] = None
         ) -> Dict[str, List[dict]]:
    """``{node rel: [entry, ...]}``. An absent ledger is empty; an
    unreadable or malformed one is empty AND logged (nothing is then
    foldable — the safe direction). Never raises."""
    log = log or _noop
    path = ledger_path(project_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log(f"provenance ledger {path} unreadable ({exc}) - no reviewer "
            f"section is foldable until it is readable again")
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        log(f"provenance ledger {path} is malformed ({exc}) - no reviewer "
            f"section is foldable until a new reviewer commit rewrites it")
        return {}
    paths = data.get("paths") if isinstance(data, dict) else None
    out: Dict[str, List[dict]] = {}
    if isinstance(paths, dict):
        for rel, entries in paths.items():
            if isinstance(rel, str) and isinstance(entries, list):
                good = [e for e in entries if isinstance(e, dict)
                        and isinstance(e.get("digest"), str)
                        and isinstance(e.get("commit"), str)]
                if good:
                    out[rel] = good
    return out


def entries_for(project_root: "str | Path", rel: str,
                log: Optional[Log] = None) -> List[dict]:
    """The ledger entries of one node (``[]`` when none)."""
    return list(load(project_root, log).get(rel, []))


def _save(project_root: "str | Path", paths: Mapping[str, List[dict]]
          ) -> Optional[str]:
    """Write the ledger atomically; the error text, or ``None`` on success."""
    from vco_lib.atomic import atomic_write_json

    path = ledger_path(project_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {"v": LEDGER_VERSION, "paths": dict(paths)},
                          indent=2, fsync=False, mode=0o600)
    except OSError as exc:
        return str(exc) or type(exc).__name__
    return None


def record(project_root: "str | Path", commit: str,
           sections: Mapping[str, Sequence[Mapping[str, str]]],
           log: Optional[Log] = None, *, now: Optional[str] = None) -> bool:
    """Record what one reviewer commit left in each node it committed.

    ``sections`` maps each committed node to EVERY section the reviewer may
    claim in the committed bytes: ``{"digest", "heading", "key", "kind",
    "keep_commit"}`` — ``keep_commit`` (optional) is the commit of an
    existing entry the section still matches (an untouched, already proven
    section keeps its ORIGINAL commit; a new or rewritten one is recorded
    under ``commit``). A node's previous entries are REPLACED (an entry
    whose section is gone can never match again). ``False`` (and a log
    line) when the ledger could not be written — the sections then stay
    append-only. Never raises."""
    log = log or _noop
    stamp = now or _now()
    try:
        from vco_lib.atomic import portable_exclusive_lock

        ledger_path(project_root).parent.mkdir(parents=True, exist_ok=True)
        with portable_exclusive_lock(_lock_path(project_root),
                                     stale_after_s=60.0):
            paths = load(project_root, log)
            for rel, secs in sections.items():
                entries = []
                for s in secs:
                    entries.append({
                        "digest": str(s["digest"]),
                        "heading": str(s.get("heading", "")),
                        "key": str(s.get("key", "")),
                        "kind": str(s.get("kind", "")),
                        "commit": str(s.get("keep_commit") or commit),
                        "recorded": str(s.get("recorded") or stamp),
                    })
                if entries:
                    paths[rel] = entries
                else:
                    paths.pop(rel, None)
            total = sum(len(v) for v in paths.values())
            if total > MAX_ENTRIES:
                flat = sorted(((e.get("recorded", ""), rel, i)
                               for rel, v in paths.items()
                               for i, e in enumerate(v)))
                drop = flat[:total - MAX_ENTRIES]
                gone: Dict[str, set] = {}
                for _stamp, rel, i in drop:
                    gone.setdefault(rel, set()).add(i)
                for rel, idxs in gone.items():
                    for i in sorted(idxs):
                        e = paths[rel][i]
                        log(f"provenance ledger over {MAX_ENTRIES} entries - "
                            f"dropping the oldest: {rel} "
                            f"{e.get('heading', '')!r} (it stays in the "
                            f"node, append-only from now on)")
                    paths[rel] = [e for i, e in enumerate(paths[rel])
                                  if i not in idxs]
                    if not paths[rel]:
                        del paths[rel]
            err = _save(project_root, paths)
    except Exception as exc:  # noqa: BLE001 — logged, the safe direction
        err = str(exc) or type(exc).__name__
    if err is not None:
        log(f"provenance NOT recorded for {commit[:12]} ({err}): the "
            f"reviewer sections it wrote stay append-only (never foldable)")
        return False
    return True
