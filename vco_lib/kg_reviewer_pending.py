# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 L-KGREV — the files the KG reviewer left uncommitted.

The post-commit KG reviewer (``templates/scripts/kg_reviewer_backend.py``)
writes KG nodes and then commits exactly its own bytes as a ``kg-sync:``
commit. When that commit is SKIPPED (HEAD moved while the review ran, the
user already had the same file dirty, a git operation was in progress) the
reviewed nodes stay in the working tree and nothing told anybody. Field
report (2026-11-03): 539 skips against 296 commits, and a CASCADE -- once a
file was left dirty, every later review touching it skipped too, because the
guard read the reviewer's own leftovers as "the user's changes". About 30
files piled up over days.

This module is the ONE home for the state that breaks that cycle:

* **the ledger** — ``.claude/state/kg_reviewer_pending.json`` (git-excluded
  state, never in the project tree's version control): one entry per path
  with the sha256 of the bytes the reviewer PROVABLY wrote, the first skip
  and the last. A path recorded here is a candidate to fold into a later
  review's commit; :func:`own_leftovers` hands back only the entries whose
  CURRENT bytes still hash to the recorded sha, so a file the user edited
  afterwards is never taken for the reviewer's.
* **the alert** — one row filed through the shipped subagent-alert channel
  (:mod:`vco_lib.agent_alerts`, ``agent-alerts-deliver``) under the pseudo
  agent id :data:`ALERT_AGENT_ID`; the deliver hook already surfaces those
  to the main chat on the next prompt or tool call of the session. One row
  per NEW skip (:func:`surface` compares the pending set against the
  ``path:sha`` pairs already alerted, :data:`ALERT_SIG_KEY`), never one per
  prompt -- and a set that only SHRANK (a partial fold) raises nothing.
* **the line** — :func:`status_line`, what the session-start / compaction
  re-inject shows while anything is still pending.

Every entry carries the sha it was recorded with so the fold is a byte
proof, never a guess: a file whose bytes differ from the ledger is the
user's, and stays out of the commit exactly as before. An entry also carries
``foldable``: True only when the reviewer's bytes are the WHOLE change (the
file was clean when that run started, or dirty only with a byte-verified
leftover of the reviewer's own). A file skipped because the USER had
uncommitted changes on it is ledgered ``foldable: false`` -- still named to
the session, never committed by a later review (its bytes mix the user's
edit with the reviewer's).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional

#: The ledger, relative to the project folder that owns ``.claude/``.
LEDGER_REL = Path(".claude") / "state" / "kg_reviewer_pending.json"
#: The lock guarding every read-modify-write of the ledger (one per project).
LOCK_NAME = "kg_reviewer_pending.lock"

#: The pseudo agent id an alert row is filed under. It never collides with a
#: real subagent id (those are hex), and ``agent_state_key`` turns it into
#: ``<session>__kg-reviewer`` -- a file the shipped deliver hook already
#: collects for the session, since it selects rows by their FIELDS.
ALERT_AGENT_ID = "kg-reviewer"
ALERT_AGENT_TYPE = "kg-reviewer"
ALERT_KIND = "kg_reviewer_uncommitted"

#: Ledger schema version (bump only on an incompatible shape).
LEDGER_VERSION = 1
#: Bound on the paths named in one alert line / status line.
MAX_PATHS_IN_LINE = 8
#: The ``path:sha`` pairs the last alert covered (a sorted list). A new
#: alert is filed only for a pair NOT in it -- a newly skipped file or new
#: reviewer bytes -- never for a set that merely shrank.
ALERT_SIG_KEY = "alert_sig"

Log = Callable[[str], None]


# ── paths and layers ───────────────────────────────────────────────────────


def ledger_path(project_root: "str | Path") -> Path:
    return Path(project_root) / LEDGER_REL


def _lock_path(project_root: "str | Path") -> Path:
    return Path(project_root) / ".claude" / "state" / LOCK_NAME


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lf_sha(data: bytes) -> str:
    """The sha the guard's CRLF-tolerant comparison would see (``_same`` in
    :mod:`vco_lib.kg_reviewer_guard` reads CRLF as LF)."""
    return _sha(data.replace(b"\r\n", b"\n"))


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty() -> Dict[str, object]:
    return {"v": LEDGER_VERSION, "paths": {}, ALERT_SIG_KEY: []}


def load(project_root: "str | Path") -> Dict[str, object]:
    """The ledger as a dict (``{"v", "paths", "alert_sig"}``); an absent,
    unreadable or malformed ledger reads as empty. Soft-fail by design: a
    broken ledger must never stop a review or a hook."""
    try:
        data = json.loads(ledger_path(project_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    paths = data.get("paths")
    out = _empty()
    if isinstance(paths, dict):
        for rel, entry in paths.items():
            if isinstance(rel, str) and isinstance(entry, dict):
                out["paths"][rel] = entry  # type: ignore[index]
    alerted = data.get(ALERT_SIG_KEY)
    if isinstance(alerted, list):
        out[ALERT_SIG_KEY] = sorted(  # type: ignore[index]
            a for a in alerted if isinstance(a, str))
    return out


def _save(project_root: "str | Path", data: Mapping[str, object]) -> bool:
    """Write the ledger atomically, under the per-project lock. ``False`` on
    any failure (never raises)."""
    from vco_lib.atomic import atomic_write_json, portable_exclusive_lock

    path = ledger_path(project_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with portable_exclusive_lock(_lock_path(project_root)):
            atomic_write_json(path, dict(data), indent=2, fsync=False,
                              mode=0o600)
        return True
    except OSError:
        return False


def pending_paths(project_root: "str | Path") -> List[str]:
    """Every ledgered pending path, sorted (whether or not it still exists)."""
    paths = load(project_root).get("paths")
    if not isinstance(paths, dict):
        return []
    return sorted(p for p in paths if isinstance(p, str))


# ── recording a skip ───────────────────────────────────────────────────────


def record_skips(project_root: "str | Path", root: Path,
                 proofs: Mapping[str, str], *, reason: str, short: str,
                 foldable: Iterable[str] = (),
                 now: Optional[str] = None) -> List[str]:
    """Ledger every path in ``proofs`` whose CURRENT bytes still hash to the
    reviewer-proven sha.

    ``foldable`` names the paths whose bytes are the reviewer's ALONE (see
    the module docstring); every other recorded path is ``foldable: false``
    and :func:`own_leftovers` never hands it back.

    ``proofs`` is ``rel -> sha256`` of the bytes the run PROVED the reviewer
    wrote. A path whose bytes no longer hash to that value was changed by
    somebody else after the reviewer wrote it: it is NOT recorded (and an
    existing entry for it is left exactly as it was -- never re-stamped with
    the user's bytes, which would let a later run commit the user's edit).

    Returns the paths this call added or refreshed. Never raises."""
    if not proofs:
        return []
    now = now or _now_stamp()
    data = load(project_root)
    entries: Dict[str, dict] = dict(data.get("paths") or {})  # type: ignore[arg-type]
    fold_ok = set(foldable)
    touched: List[str] = []
    for rel, proven in sorted(proofs.items()):
        try:
            raw = (Path(root) / rel).read_bytes()
        except OSError:
            continue  # gone: nothing left to fold, and nothing to say
        if _sha(raw) != proven and _lf_sha(raw) != proven:
            continue
        entry = dict(entries.get(rel) or {})
        entry.update({
            "sha256": proven,
            "last_skip": now,
            "reason": reason,
            "commit": short,
            "foldable": rel in fold_ok,
        })
        entry.setdefault("first_seen", now)
        entries[rel] = entry
        touched.append(rel)
    if not touched:
        return []
    data = dict(data)
    data["paths"] = entries
    _save(project_root, data)
    return touched


# ── reading it back ────────────────────────────────────────────────────────


def _matches(entry: Mapping[str, object], raw: Optional[bytes]) -> bool:
    recorded = str(entry.get("sha256") or "")
    return bool(recorded) and raw is not None and (
        _sha(raw) == recorded or _lf_sha(raw) == recorded)


def verified_own(project_root: "str | Path",
                 pre_bytes: Callable[[str], Optional[bytes]]) -> "set[str]":
    """The FOLDABLE ledgered paths whose bytes at the START of this run (the
    caller's ``pre_bytes``) hash to the recorded sha: their dirty state at
    that start was the reviewer's own leftover and nothing else, so it is
    not "the user's uncommitted changes". A path the user edited after the
    skip, or one recorded ``foldable: false``, is NOT in the set -- the
    user-changes block applies to it exactly as before."""
    entries: Dict[str, dict] = dict(load(project_root).get("paths") or {})  # type: ignore[arg-type]
    out: "set[str]" = set()
    for rel, entry in entries.items():
        if entry.get("foldable") is not True:
            continue
        try:
            raw = pre_bytes(rel)
        except Exception:  # noqa: BLE001 -- unprovable = not ours
            continue
        if _matches(entry, raw):
            out.add(rel)
    return out


def prune_settled(project_root: "str | Path", root: Path,
                  head_bytes: Callable[[str], "tuple[bool, Optional[bytes]]"]
                  ) -> List[str]:
    """Drop every entry whose working-tree bytes equal the current commit's
    (``head_bytes(rel)`` -> ``(known, bytes)``), whatever its recorded sha:
    the file was committed (by anybody) or reverted, so nothing is pending
    on it any more. Covers the entries :func:`own_leftovers` never returns
    (user-edited, ``foldable: false``), which would otherwise be named at
    every session start forever. Returns the dropped paths; never raises."""
    entries: Dict[str, dict] = dict(load(project_root).get("paths") or {})  # type: ignore[arg-type]
    settled: List[str] = []
    for rel in sorted(entries):
        try:
            raw = (Path(root) / rel).read_bytes()
            known, head = head_bytes(rel)
        except Exception:  # noqa: BLE001 -- a vanished file is own_leftovers' job
            continue
        if known and head is not None and (
                raw.replace(b"\r\n", b"\n") == head.replace(b"\r\n", b"\n")):
            settled.append(rel)
    return clear_paths(project_root, settled) if settled else []


def own_leftovers(project_root: "str | Path",
                  root: Path) -> Dict[str, bytes]:
    """``rel -> current bytes`` for every FOLDABLE ledger entry the reviewer
    still owns (its current bytes hash to the recorded sha).

    An entry whose file vanished is dropped from the ledger here -- there is
    nothing left to fold or to warn about. An entry whose bytes changed is
    KEPT in the ledger but not returned: it is still an uncommitted
    reviewer-era file a human should look at, and its new bytes are the
    user's, so no run may ever commit them on the reviewer's behalf."""
    data = load(project_root)
    entries: Dict[str, dict] = dict(data.get("paths") or {})  # type: ignore[arg-type]
    out: Dict[str, bytes] = {}
    gone: List[str] = []
    for rel, entry in sorted(entries.items()):
        try:
            raw = (Path(root) / rel).read_bytes()
        except OSError:
            gone.append(rel)
            continue
        if entry.get("foldable") is not True:
            continue  # the user's edit is part of these bytes: never folded
        if _matches(entry, raw):
            out[rel] = raw
    if gone:
        for rel in gone:
            entries.pop(rel, None)
        data = dict(data)
        data["paths"] = entries
        _save(project_root, data)
    return out


def clear_paths(project_root: "str | Path", rels: Iterable[str]) -> List[str]:
    """Forget ``rels`` (they are committed now). Returns what was removed.

    When the last entry goes the alert signature is reset too, so a future
    skip alerts again."""
    wanted = {r for r in rels if r}
    if not wanted:
        return []
    data = load(project_root)
    entries: Dict[str, dict] = dict(data.get("paths") or {})  # type: ignore[arg-type]
    removed = [r for r in sorted(wanted) if r in entries]
    if not removed:
        return []
    for rel in removed:
        entries.pop(rel, None)
    data = dict(data)
    data["paths"] = entries
    gone = set(removed)
    data[ALERT_SIG_KEY] = [a for a in _alerted(data)
                           if a.rsplit(":", 1)[0] not in gone]
    _save(project_root, data)
    return removed


def signature(entries: Mapping[str, dict]) -> List[str]:
    """The pending set as sorted ``path:sha`` pairs. :func:`surface` alerts
    only when this holds a pair the last alert did not cover."""
    return sorted(f"{rel}:{str(e.get('sha256') or '')}"
                  for rel, e in entries.items())


def _alerted(data: Mapping[str, object]) -> List[str]:
    raw = data.get(ALERT_SIG_KEY)
    return [a for a in raw if isinstance(a, str)] if isinstance(raw, list) else []


# ── what the session is told ───────────────────────────────────────────────


def _name_paths(rels: List[str]) -> str:
    shown = rels[:MAX_PATHS_IN_LINE]
    more = len(rels) - len(shown)
    text = ", ".join(shown)
    return f"{text} (+{more} more)" if more > 0 else text


def status_line(project_root: "str | Path") -> str:
    """One line for the session-start / compaction re-inject, or ``""`` when
    nothing is pending. Never raises."""
    data = load(project_root)
    entries: Dict[str, dict] = dict(data.get("paths") or {})  # type: ignore[arg-type]
    if not entries:
        return ""
    rels = sorted(entries)
    since = min(str(e.get("first_seen") or "") for e in entries.values()) or "?"
    full = ""
    if len(rels) > MAX_PATHS_IN_LINE:
        # The line names only the first few; the command for the rest names
        # THIS interpreter (the VCO venv the hook resolved -- a bare `python`
        # in a user project rarely has vco_lib) and the absolute root.
        import sys

        full = (f" Full list: \"{sys.executable}\" -m vco_lib.kg_reviewer_pending"
                f" paths --project-root \"{Path(project_root).resolve()}\"")
    return (f"## KG reviewer: {len(rels)} file(s) left uncommitted since "
            f"{since}\n{_name_paths(rels)} - review them and commit the ones "
            f"you want (`git add` + `git commit`); a later review folds in "
            f"the unchanged ones by itself.{full}")


def alert_message(project_root: "str | Path") -> str:
    """The one-line alert text (``""`` when nothing is pending)."""
    data = load(project_root)
    entries: Dict[str, dict] = dict(data.get("paths") or {})  # type: ignore[arg-type]
    if not entries:
        return ""
    rels = sorted(entries)
    since = min(str(e.get("first_seen") or "") for e in entries.values()) or "?"
    return (f"kg-reviewer left {len(rels)} file(s) uncommitted since {since}: "
            f"{_name_paths(rels)} - review and commit them.")


def surface(project_root: "str | Path", session_id: str,
            log: Optional[Log] = None) -> bool:
    """Tell the session about the pending files: file ONE alert row when the
    pending set changed since the last alert, and write the same line to the
    reviewer log either way. Returns True when a row was filed.

    The row goes through the shipped alert channel, so the next main-chat
    prompt or tool call of ``session_id`` carries it as context -- and a row
    is consumed by its delivery, so the SAME set never alerts twice, and a
    set that only shrank (some files folded) raises nothing either
    (:data:`ALERT_SIG_KEY`). A skip with no session id cannot be delivered
    (`agent_alerts` files it under ``nosession__``, which no session sweep
    matches): the re-inject line (:func:`status_line`) is then the channel,
    and the signature is still stored so retries do not pile rows up."""
    data = load(project_root)
    entries: Dict[str, dict] = dict(data.get("paths") or {})  # type: ignore[arg-type]
    message = alert_message(project_root)
    if not message:
        return False
    sig = signature(entries)
    if not set(sig) - set(_alerted(data)):
        return False
    filed = False
    if session_id:
        try:
            from vco_lib.agent_alerts import append_alert

            filed = append_alert(str(project_root), session_id, ALERT_AGENT_ID,
                                 ALERT_AGENT_TYPE, ALERT_KIND,
                                 message) is not None
        except Exception:  # noqa: BLE001 -- an alert is never worth a crash
            filed = False
    if log is not None:
        log(message + ("" if filed else " (alert not filed for the session; "
                                       "the session-start line still shows it)"))
    data = dict(data)
    data[ALERT_SIG_KEY] = sig
    _save(project_root, data)
    return filed


# ── CLI (the hooks' access to the one implementation) ──────────────────────


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        prog="python -m vco_lib.kg_reviewer_pending",
        description="The KG reviewer's uncommitted files: the status line, "
                    "the count and the paths (one home, "
                    "vco_lib.kg_reviewer_pending)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, helptext in (
        ("status", "print the one-line pending status (nothing when empty)"),
        ("count", "print the number of pending files"),
        ("paths", "print one pending path per line"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--project-root", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "status":
        line = status_line(args.project_root)
        if line:
            sys.stdout.write(line + "\n")
        return 0
    if args.cmd == "count":
        sys.stdout.write(f"{len(pending_paths(args.project_root))}\n")
        return 0
    for rel in pending_paths(args.project_root):
        sys.stdout.write(rel + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())