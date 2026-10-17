# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Was a file re-attached to the context after the latest compaction?

The ONE home of that question (A>B>C tier A): ``pre-tool-use.{sh,ps1}`` ask
``python -m vco_lib.compact_reattach check`` on a Build Anchor miss, and
``compact-context-reinject.{sh,ps1}`` call ``... snapshot-body``; neither
shell sibling carries a copy of the rule.

What the harness does
---------------------
Right after a compaction Claude Code re-reads up to five files read or edited
in the session and puts their content back in the context (a file over 5,000
tokens comes back as a path-only ``Referenced file``) — vendor page
https://code.claude.com/docs/en/context-window, "What survives compaction".
No hook payload lists those files, and the page names no Claude Code version.

But the session transcript JSONL (``transcript_path`` in every hook's input)
records them exactly. After the ``{"type":"system","subtype":
"compact_boundary"}`` row the harness writes one row per re-attached file::

    {"type":"attachment","attachment":{"type":"file","filename":"<abs path>",
     "content":{"type":"text","file":{"filePath":…,"content":…,
     "startLine":1,"numLines":N,"totalLines":N}},"displayPath":…}}

An over-cap file is a ``compact_file_reference`` row (no content) and an
externally edited file an ``edited_text_file`` row (a snippet) — neither puts
the whole file in context, so neither counts. Those rows are flushed ~90 ms
before the SessionStart(compact) hook's success row, so a compact-time hook
may race them; this module is therefore asked LAZILY, at the first Write that
needs it.

How it is used
--------------
``post-compact`` wipes the Build Anchor reads ledger
(``.claude/state/reads_<key>.txt``) as it always has. When a later ``Write``
to an existing file misses the ledger, ``pre-tool-use`` asks
:func:`was_reattached`; "yes" allows the Write and appends the path to the
ledger (so later Writes do not rescan), anything else blocks exactly as
before. Exact and version-independent: a harness that re-attaches nothing
writes no row, so the Write is still blocked.

Fail closed, bounded
--------------------
"Yes" requires, all at once: a readable transcript; a verified
``compact_boundary`` row within the last :data:`SCAN_MAX_BYTES` of it (the
LATEST one — an earlier compaction's attachments do not count); after it, a
``file`` attachment row whose ``filename`` names the target (``realpath`` +
``normcase`` on both sides, so Windows case and separators match) and whose
content is the whole file as text. Any other outcome — no transcript, no
boundary within the window, a row that does not parse, an I/O error — is
"no". The transcript is never read whole (it can exceed 1 GB): the scan reads
:data:`CHUNK_BYTES` blocks backwards from the end, looks only for the exact
byte markers below, and parses at most one line of at most
:data:`MAX_LINE_BYTES` per marker hit, so memory stays O(chunk + line) and the
work is bounded by the window. The wall-clock time is bounded too: past
:data:`DEADLINE_SECONDS` the answer is "no" — the hook's own registration
timeout is short, and a hook killed on timeout would ALLOW the Write.

The scan is also not asked at all unless ``post-compact`` has written this
session's ``.claude/state/compacted_<key>.txt`` marker: a session that never
compacted has nothing re-attached, so ``pre-tool-use`` blocks it without
spawning Python. A subagent's tool call is always "no": the
payload's transcript is the parent's, whose re-attachments are not in the
subagent's context.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import BinaryIO

# The scan window: the latest compact_boundary must lie within this many
# bytes of the end of the transcript. Observed on real transcripts
# (2026-10-16, 64 transcripts with compactions, up to 1.6 GB each): the bytes
# between two consecutive boundaries were p50 0.9 MB, p90 6.1 MB, max 66.5 MB.
# A Write that comes after a longer post-compaction stretch is blocked (the
# model re-Reads) — the safe direction.
SCAN_MAX_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
# A boundary row is ~1.5 KB and a re-attached file row carries < 5,000 tokens
# (largest observed ~0.4 MB); a longer line is neither and is not parsed.
MAX_LINE_BYTES = 4 * 1024 * 1024
# Wall-clock budget for one answer. ``pre-tool-use`` is registered with a
# 3-second timeout, and a hook the harness kills on timeout does not block
# the tool call — so a scan that outlives the hook would turn the block into
# an allow. Past this deadline the answer is "no" (fail closed); the budget
# leaves room for interpreter start-up and the rest of the hook.
DEADLINE_SECONDS = 1.0

# Exact serialisations (compact JSON, no spaces). The same text inside a JSON
# STRING is escaped (`\"subtype\":…`), so these bytes only match real rows;
# every hit is still parsed and checked.
_BOUNDARY_MARKER = b'"subtype":"compact_boundary"'
_FILE_MARKER = b'"attachment":{"type":"file"'

ANSWER_YES = "reattached"
ANSWER_NO = "no"


class _DeadlineExceeded(Exception):
    """The scan ran past its wall-clock budget; the answer is "no"."""


def _check_deadline(deadline_at: float) -> None:
    if time.monotonic() > deadline_at:
        raise _DeadlineExceeded


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _line_around(fh: BinaryIO, pos: int, size: int) -> tuple[bytes, int] | None:
    """The whole line containing byte *pos* (newline excluded) and the offset
    just past its newline; None when the line is longer than MAX_LINE_BYTES
    or unterminated (a row still being written)."""
    lo = max(0, pos - MAX_LINE_BYTES)
    fh.seek(lo)
    nl = fh.read(pos - lo).rfind(b"\n")
    if nl < 0 and lo > 0:
        return None
    start = lo + nl + 1
    fh.seek(pos)
    end = fh.read(min(size, pos + MAX_LINE_BYTES) - pos).find(b"\n")
    if end < 0 or pos + end - start > MAX_LINE_BYTES:
        return None
    fh.seek(start)
    return fh.read(pos + end - start), pos + end + 1


def _parse(found: tuple[bytes, int] | None) -> dict | None:
    if found is None:
        return None
    try:
        row = json.loads(found[0])
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def _last_boundary_end(fh: BinaryIO, size: int, window: int,
                       deadline_at: float) -> int | None:
    """Offset just past the latest verified compact_boundary row within the
    last *window* bytes; None when there is none. Raises
    :class:`_DeadlineExceeded` past *deadline_at*."""
    floor = max(0, size - window)
    end = size
    overlap = len(_BOUNDARY_MARKER) - 1
    while end > floor:
        _check_deadline(deadline_at)
        start = max(floor, end - CHUNK_BYTES)
        fh.seek(start)
        block = fh.read(min(size, end + overlap) - start)
        idx = len(block)
        while True:
            idx = block.rfind(_BOUNDARY_MARKER, 0, idx)
            if idx < 0:
                break
            _check_deadline(deadline_at)
            found = _line_around(fh, start + idx, size)
            row = _parse(found)
            if found is not None and row is not None \
                    and row.get("type") == "system" \
                    and row.get("subtype") == "compact_boundary":
                return found[1]
        end = start
    return None


def _row_reattaches(row: dict | None, target: str) -> bool:
    """True for a `file` attachment row carrying the WHOLE text of *target*."""
    if not row or row.get("type") != "attachment":
        return False
    att = row.get("attachment")
    if not isinstance(att, dict) or att.get("type") != "file":
        return False
    filename = att.get("filename")
    if not isinstance(filename, str) or not filename:
        return False
    if not os.path.isabs(filename):
        cwd = row.get("cwd")
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            return False
        filename = os.path.join(cwd, filename)
    if _norm(filename) != target:
        return False
    content = att.get("content")
    if not isinstance(content, dict) or content.get("type") != "text":
        return False
    inner = content.get("file")
    if not isinstance(inner, dict) or not isinstance(inner.get("content"), str):
        return False
    # A line range is not the whole file.
    if inner.get("startLine", 1) not in (None, 0, 1):
        return False
    num, total = inner.get("numLines"), inner.get("totalLines")
    if isinstance(num, int) and isinstance(total, int) and num < total:
        return False
    return True


def was_reattached(transcript_path: str, target_path: str, agent_id: str = "",
                   window: int = SCAN_MAX_BYTES,
                   deadline: float = DEADLINE_SECONDS) -> bool:
    """Did the harness re-attach the whole of *target_path* after the latest
    compaction recorded in *transcript_path*? Fails closed (False) on every
    doubt, and when the scan runs past *deadline* seconds — see the module
    docstring."""
    if agent_id or not transcript_path or not target_path:
        return False
    deadline_at = time.monotonic() + deadline
    try:
        target = _norm(target_path)
        with open(transcript_path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            pos = _last_boundary_end(fh, size, window, deadline_at)
            if pos is None:
                return False
            overlap = len(_FILE_MARKER) - 1
            while pos < size:
                _check_deadline(deadline_at)
                fh.seek(pos)
                block = fh.read(min(CHUNK_BYTES + overlap, size - pos))
                idx = block.find(_FILE_MARKER)
                while 0 <= idx < CHUNK_BYTES:
                    _check_deadline(deadline_at)
                    found = _line_around(fh, pos + idx, size)
                    if _row_reattaches(_parse(found), target):
                        return True
                    idx = block.find(_FILE_MARKER, idx + 1)
                pos += CHUNK_BYTES
    except (OSError, ValueError, _DeadlineExceeded):
        return False
    return False


# The snapshot section pre-compact-save writes first; the harness re-injects
# a FRESH git status after compaction, so the stale copy is not replayed.
# Must match the header written by pre-compact-save.{sh,ps1}.
_SNAPSHOT_GIT_STATUS_HEADER = "## Modified Files (git status"
SNAPSHOT_LINE_CAP = 50


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        return []


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
    chk = sub.add_parser("check", help="print 'reattached' (exit 0) when the "
                         "path was re-attached after the latest compaction, "
                         "else 'no' (exit 1)")
    chk.add_argument("--transcript", default="")
    chk.add_argument("--path", required=True)
    chk.add_argument("--agent-id", default="")
    snap = sub.add_parser("snapshot-body")
    snap.add_argument("--path", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "snapshot-body":
        for line in snapshot_body(args.path):
            print(line)
        return 0
    if was_reattached(args.transcript, args.path, args.agent_id):
        print(ANSWER_YES)
        return 0
    print(ANSWER_NO)
    return 1


if __name__ == "__main__":
    sys.exit(_main())
