# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Which files Claude Code re-attached to the context after a compaction.

The ONE home for that estimate (A>B>C tier A): ``post-compact.{sh,ps1}`` call
``python -m vco_lib.compact_reattach reseed`` and
``compact-context-reinject.{sh,ps1}`` call ``... reattached`` and
``... snapshot-body``; neither shell
sibling carries a copy of the rule.

Harness rule this module models
-------------------------------
Source: https://code.claude.com/docs/en/context-window, section "What
survives compaction" (verified 2026-10-16; the page names no Claude Code
version for it):

* "Right after compaction, Claude Code re-reads up to five of the files
  Claude has read or edited in the session, choosing the ones modified most
  recently. A file over 5,000 tokens comes back as a path reference without
  its content, shown as ``Referenced file`` instead of ``Read``."  No total
  cap is documented.
* Also re-injected from disk by the harness: the project-root CLAUDE.md and
  unscoped rules, the plan-mode plan, auto memory, invoked skills (5k tokens
  each, 25k total) and a fresh git status (which carries the recent
  commits). SessionStart hooks matching ``compact`` run and their output is
  added.
* No hook payload lists the re-attached files (PreCompact: trigger,
  custom_instructions; PostCompact: trigger, compact_summary; SessionStart:
  source="compact") and no setting controls the re-attach — so VCO has to
  ESTIMATE the set. If a future Claude Code changes the count, the cap or the
  ordering, ``MAX_REATTACHED`` / ``TOKEN_CAP`` / ``select_reattached`` here
  are the only things to update.

Why it matters
--------------
``pre-tool-use``'s Build Anchor guard blocks a ``Write`` to an existing file
that is not in the session's reads ledger (``.claude/state/reads_<key>.txt``,
which records every Read AND every Write/Edit path, in call order). Before
v0.2.104 ``post-compact`` wiped that ledger outright, so a Write to a file the
harness had just re-attached was blocked and forced a redundant re-Read. Now
``post-compact`` re-seeds the ledger with exactly the estimated set.

Conservative by construction: a file wrongly LEFT OUT only costs one extra
Read (Build Anchor asks for it); a file wrongly LET IN would allow a blind
overwrite of a file whose content is not in context. So every uncertainty
resolves towards leaving a file out:

* "modified most recently" is ambiguous — file mtime, or the time Claude last
  touched it in the session. A file is kept only when it is among the five
  newest by mtime AND among the five most recently touched ledger entries
  (deleted files included in the latter, so they still take a slot).
* the token estimate over-counts (``TOKEN_CAP`` below); binary or
  non-UTF-8 content counts as over the cap.
* any error while computing the set leaves the ledger EMPTY (the pre-v0.2.104
  behaviour), never the old full list.

A re-seeded entry is an ordinary ledger line: Build Anchor treats it exactly
like a file Read this session (the ledger is a plain path list with no
staleness check, so a later on-disk change by something else is handled the
same way it is for any read file).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Iterable

from vco_lib.atomic import atomic_write_text

# Documented harness values (see the module docstring for the source).
MAX_REATTACHED = 5
TOKEN_CAP = 5000

# Freshness window for the persisted set, read by compact-context-reinject at
# SessionStart(compact). PostCompact fires just before it; an older file
# belongs to an earlier compaction and is ignored. Same knob and default as
# the reinject hook's snapshot staleness guard (VCO_SNAPSHOT_MAX_AGE).
_DEFAULT_MAX_AGE = 1800
# Persisted-set files untouched this long are garbage-collected on reseed
# (same 14-day horizon as pre-tool-use's reads_*.txt sweep).
_GC_SECONDS = 14 * 24 * 3600
_SNIFF_BYTES = 8192


def estimated_tokens(data: bytes) -> int:
    """Over-estimate of the token count of *data* as the harness re-reads it.

    ~3 bytes per token (real code and prose average nearer 4) plus one token
    per line for the line-number prefix a re-read carries. Deliberately high:
    an over-estimate only costs one extra Read.
    """
    return len(data) // 3 + 1 + data.count(b"\n")


def within_token_cap(path: str, cap: int = TOKEN_CAP) -> bool:
    """True only when *path* is positively known to fit under *cap*."""
    try:
        size = os.path.getsize(path)
        # Fast reject: even at the estimate's floor it cannot fit.
        if size // 3 > cap:
            return False
        with open(path, "rb") as fh:
            data = fh.read(size + 1)
    except OSError:
        return False
    if b"\x00" in data[:_SNIFF_BYTES]:
        return False  # binary: not re-read as text, never let in
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return estimated_tokens(data) <= cap


def _resolve(entry: str, project_dir: str) -> str:
    return os.path.normpath(os.path.join(project_dir, entry))


def select_reattached(entries: Iterable[str], project_dir: str,
                      limit: int = MAX_REATTACHED,
                      cap: int = TOKEN_CAP) -> list[str]:
    """The ledger entries (original spelling, newest mtime first) that the
    harness re-attached WITH content after compaction.

    *entries* are the reads-ledger lines in call order; relative entries
    resolve against *project_dir*.
    """
    # Keyed by the case-normalised path (one file, one slot, on Windows too).
    last_touch: dict[str, tuple[int, str]] = {}
    for idx, raw in enumerate(entries):
        entry = raw.strip()
        if not entry:
            continue
        last_touch[os.path.normcase(_resolve(entry, project_dir))] = (idx, entry)

    recent_touch = {
        p for p, _ in sorted(last_touch.items(), key=lambda kv: kv[1][0],
                             reverse=True)[:limit]
    }
    dated: list[tuple[float, int, str, str]] = []
    for path, (idx, entry) in last_touch.items():
        if not os.path.isfile(path):
            continue  # deleted (or never a regular file): nothing re-read
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        dated.append((mtime, idx, path, entry))
    dated.sort(reverse=True)
    picked = dated[:limit]
    return [entry for _, _, path, entry in picked
            if path in recent_touch and within_token_cap(path, cap)]


def state_dir(project_dir: str) -> str:
    return os.path.join(project_dir, ".claude", "state")


def reads_ledger_path(project_dir: str, key: str) -> str:
    """Must match SESSION_READS_FILE in pre-tool-use.{sh,ps1}."""
    return os.path.join(state_dir(project_dir), f"reads_{key}.txt")


def reattached_path(project_dir: str, key: str) -> str:
    return os.path.join(state_dir(project_dir), f"compact_reattached_{key}.txt")


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        return []


def _write_atomic(path: str, lines: list[str]) -> None:
    atomic_write_text(Path(path), "".join(f"{line}\n" for line in lines),
                      fsync=False)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _gc_reattached(project_dir: str, now: float) -> None:
    sdir = state_dir(project_dir)
    try:
        names = os.listdir(sdir)
    except OSError:
        return
    for name in names:
        if name.startswith("compact_reattached_") and name.endswith(".txt"):
            path = os.path.join(sdir, name)
            try:
                if now - os.path.getmtime(path) > _GC_SECONDS:
                    os.remove(path)
            except OSError:
                pass


def _project_relative(entry: str, project_dir: str) -> str:
    """POSIX path of *entry* relative to the project; "" when it lies on
    another drive (Windows) — the re-inject compares only project files."""
    try:
        return os.path.relpath(_resolve(entry, project_dir),
                               project_dir).replace(os.sep, "/")
    except ValueError:
        return ""


def reseed(project_dir: str, key: str) -> list[str]:
    """Replace the reads ledger with the re-attached set; persist the set for
    the SessionStart(compact) re-inject. Returns the set.

    On any failure the ledger is removed (the safe, pre-v0.2.104 state) and
    the error propagates to the caller.
    """
    ledger = reads_ledger_path(project_dir, key)
    marker = reattached_path(project_dir, key)
    try:
        chosen = select_reattached(_read_lines(ledger), project_dir)
        os.makedirs(state_dir(project_dir), exist_ok=True)
        if chosen:
            _write_atomic(ledger, chosen)
        else:
            _remove(ledger)
        _write_atomic(marker, [r for r in (_project_relative(e, project_dir)
                                           for e in chosen) if r])
        _gc_reattached(project_dir, time.time())
        return chosen
    except Exception:
        _remove(ledger)
        _remove(marker)
        raise


def _max_age() -> int:
    raw = (os.environ.get("VCO_SNAPSHOT_MAX_AGE") or "").strip()
    return int(raw) if raw.isdigit() else _DEFAULT_MAX_AGE


def reattached(project_dir: str, key: str) -> list[str]:
    """The persisted set (project-relative POSIX paths) when it belongs to
    the compaction that just happened; empty otherwise (nothing is then
    skipped by the re-inject — the safe direction)."""
    marker = reattached_path(project_dir, key)
    try:
        mtime = os.path.getmtime(marker)
    except OSError:
        return []
    if mtime < time.time() - _max_age():
        return []
    return [line for line in _read_lines(marker) if line.strip()]


# The snapshot section pre-compact-save writes first; the harness re-injects
# a FRESH git status after compaction, so the stale copy is not replayed.
# Must match the header written by pre-compact-save.{sh,ps1}.
_SNAPSHOT_GIT_STATUS_HEADER = "## Modified Files (git status"
SNAPSHOT_LINE_CAP = 50


def snapshot_body(path: str, line_cap: int = SNAPSHOT_LINE_CAP) -> list[str]:
    """The pre-compact snapshot as re-injected after a compaction: its first
    *line_cap* lines minus the git-status section the harness already
    restores fresh."""
    out: list[str] = []
    skipping = False
    for line in _read_lines(path):
        if line.startswith("## "):
            skipping = line.startswith(_SNAPSHOT_GIT_STATUS_HEADER)
        if not skipping:
            out.append(line)
    return out[:line_cap]


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vco_lib.compact_reattach")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("reseed", "reattached"):
        p = sub.add_parser(name)
        p.add_argument("--project", required=True)
        p.add_argument("--key", required=True,
                       help="the session (or session__agent) ledger key")
    snap = sub.add_parser("snapshot-body")
    snap.add_argument("--path", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "snapshot-body":
        for line in snapshot_body(args.path):
            print(line)
        return 0
    key = "".join(ch for ch in args.key if ch.isalnum() or ch in "_-")
    if not key:
        # No usable ledger key: report failure so the caller falls back to
        # its conservative path (post-compact clears the ledger).
        return 1 if args.cmd == "reseed" else 0
    project = os.path.abspath(args.project)
    if args.cmd == "reseed":
        try:
            reseed(project, key)
        except Exception as exc:  # noqa: BLE001 - reported, ledger already cleared
            print(f"[VCO] compact_reattach reseed failed ({exc}); the Build "
                  "Anchor reads list was cleared instead", file=sys.stderr)
            return 1
        return 0
    for line in reattached(project, key):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
