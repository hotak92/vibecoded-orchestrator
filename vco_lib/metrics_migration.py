# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""COPY the legacy ``~/.claude/metrics`` history into ``~/.vct/metrics``.

v0.2.92 W7. The metrics home moved (see :func:`vco_lib.paths.vct_metrics_dir`);
this module carries the existing history across.

**COPY, NEVER MOVE — the binding constraint.** The originals are left
byte-identical as a frozen archive:

* nothing here opens a source file for writing, renames it, truncates it or
  unlinks it — the only source-side syscalls are ``stat`` and a read;
* every run re-hashes each source file AFTER the merge and refuses to report
  success if the digest, size or line count moved (:attr:`FileCopyRecord.
  source_unchanged`), so "we did not touch the originals" is a *measured*
  property of each run, not a claim about the code;
* **nobody deletes the archive.** Not this module, not the installer, not the
  uninstaller. There is deliberately no ``--cleanup`` flag and no "you can now
  delete these" prompt: the user decides, on their timetable, by hand.

**Append-merge, deduplicated by line identity, so a re-run changes nothing.**
When the destination already exists, a source line is appended only to the
extent the destination does not already hold it. The dedup is a MULTISET
difference, not a set difference: a source file holding the same row twice and
a destination holding it once contributes one more copy, not zero. A set
difference would have silently dropped genuine duplicate rows (JSONL telemetry
can legitimately repeat a row — two identical failures in the same second), and
"idempotent" must not be bought with data loss.

**Writers switch only after a verified copy.** The sentinel this module writes
(``<dest>/.migrated-from-claude.json``) is the record of that verification, and
it is written LAST — after every file has copied and verified. The shell helper
``templates/hooks/_lib/metrics-dir.{sh,ps1}`` gates its write-target on that
sentinel, so a machine whose copy failed keeps writing to the archive, where its
history already is. Nothing is stranded and nothing is double-counted; the next
run finishes the job.

Three axes:

* **Fresh install** — no archive directory at all: a clean no-op. Nothing is
  created (not even the destination), nothing is an error, and the shell helper
  sends writers straight to the new home because there is nothing to wait for.
* **Update** — archive present: copy, verify, write the sentinel, writers move.
* **Already-damaged / interrupted** — a previous run copied some files and
  stopped (crash, killed session, full disk). The sentinel is absent, so the
  next run re-merges every file; the multiset dedup makes the already-copied
  ones no-ops and finishes the rest. Running the migration twice in a row is
  the test for this and must leave the second run reporting zero appended
  lines and every destination byte unchanged.

Entry points::

    from vco_lib.metrics_migration import ensure_metrics_migrated
    ensure_metrics_migrated()            # cheap; safe to call on every session

    python -m vco_lib.metrics_migration --json
    python -m vco_lib.metrics_migration --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from vco_lib.paths import legacy_claude_metrics_dir, vct_metrics_dir

#: Written into the destination directory once — and only once — every source
#: file has been copied AND verified. Its presence is what lets a writer move
#: to the new home; its absence keeps writers on the archive. Named with a
#: leading dot so a ``*.jsonl`` glob never picks it up as a metrics stream.
SENTINEL_NAME = ".migrated-from-claude.json"

#: Schema version of the sentinel payload. Bump when the recorded fields change
#: in a way a reader must notice; :func:`_load_sentinel` treats an unrecognised
#: version as "no usable record" and re-merges (conservative: re-merging is a
#: verified no-op, trusting a record we cannot parse is not).
SENTINEL_VERSION = 1

#: Only these files are carried across. The archive can also hold a user's own
#: notes or an editor's backup; copying an unknown file into VCO's state root
#: is not this migration's business.
SOURCE_GLOB = "*.jsonl"


@dataclass
class FileCopyRecord:
    """Per-file outcome. Every count here is measured, never assumed."""

    name: str
    source_lines: int = 0
    source_bytes: int = 0
    source_sha256: str = ""
    #: Digest/size/line-count re-measured AFTER the merge. The LEAVE-ALONE
    #: evidence: False means we modified an original and the run is a failure.
    source_unchanged: bool = True
    dest_lines_before: int = 0
    dest_lines_after: int = 0
    dest_bytes_before: int = 0
    dest_bytes_after: int = 0
    appended_lines: int = 0
    #: Every source line is present in the destination at least as many times
    #: as it appears in the source, and the byte/line arithmetic adds up.
    verified: bool = False
    #: Set when the fast path skipped the file: the sentinel already records
    #: this exact source digest and the destination still satisfies it.
    skipped_unchanged: bool = False
    error: str = ""


@dataclass
class MigrationResult:
    """Outcome of one migration run.

    ``status`` is a four-state, deliberately distinguishing "there was nothing
    to do" from "it worked" from "it could not be determined/completed" —
    a check that cannot tell those apart is not a check.
    """

    #: ``"no_source"`` (archive absent — clean no-op) | ``"migrated"`` (copy
    #: completed and verified this run) | ``"already_current"`` (sentinel
    #: present and every source digest already recorded) | ``"failed"``.
    status: str = "no_source"
    source: str = ""
    dest: str = ""
    files: list[FileCopyRecord] = field(default_factory=list)
    sentinel_written: bool = False
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the run left the destination trustworthy.

        ``no_source`` and ``already_current`` are OK: nothing was owed.
        """
        return self.status != "failed"

    @property
    def appended_lines(self) -> int:
        return sum(f.appended_lines for f in self.files)

    @property
    def originals_untouched(self) -> bool:
        """The leave-alone half, as a single measured boolean."""
        return all(f.source_unchanged for f in self.files)

    def to_dict(self) -> dict:
        out = asdict(self)
        out["ok"] = self.ok
        out["appended_lines"] = self.appended_lines
        out["originals_untouched"] = self.originals_untouched
        return out


def _read_lines(path: Path) -> list[str]:
    """Lines of a JSONL file WITHOUT their terminators; [] when absent.

    ``errors="replace"`` rather than strict: a torn or mis-encoded byte in a
    telemetry stream must not abort a migration whose job is to preserve it.
    The line still round-trips as the same string on both sides of the
    comparison, which is all the dedup needs.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except OSError:
        raise
    if not text:
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _measure(path: Path) -> tuple[int, int, str]:
    """``(line_count, byte_size, sha256)`` for a file; zeros when absent."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return (0, 0, "")
    digest = hashlib.sha256(data).hexdigest()
    if not data:
        return (0, 0, digest)
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return (len(lines), len(data), digest)


def _lines_to_append(source_lines: list[str], dest_lines: list[str]) -> list[str]:
    """Multiset difference ``source - dest``, in source order.

    A source line is skipped only while the destination still has an unclaimed
    copy of it. So:

    * dest already holds the whole source  -> [] (the idempotent re-run);
    * dest holds one of two identical rows -> the second row is appended;
    * dest holds unrelated rows           -> the whole source is appended.
    """
    remaining = Counter(dest_lines)
    out: list[str] = []
    for line in source_lines:
        if remaining.get(line, 0) > 0:
            remaining[line] -= 1
            continue
        out.append(line)
    return out


def _append_lines(path: Path, lines: list[str]) -> int:
    """Append ``lines`` to ``path``; return the bytes written.

    Append mode, not read-modify-rewrite: a metrics file can be receiving
    concurrent appends from a hook, and ``os.replace`` of a whole rewritten
    file would silently drop rows another process added between our read and
    our write. Appending can only ever race at the END of the file, which is
    the race the JSONL readers already tolerate.

    If the destination exists and does not end in a newline (a torn previous
    append), the terminator is added first so our first row is not glued onto
    a partial one. Those repair bytes are included in the return value, so the
    caller's byte arithmetic still balances.
    """
    if not lines:
        return 0
    written = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_terminator = False
    try:
        if path.is_file() and path.stat().st_size > 0:
            with open(path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                needs_terminator = fh.read(1) != b"\n"
    except OSError:
        needs_terminator = False
    with open(path, "a", encoding="utf-8") as fh:
        if needs_terminator:
            fh.write("\n")
            written += 1
        for line in lines:
            fh.write(line + "\n")
            written += len(line.encode("utf-8")) + 1
    return written


def _load_sentinel(dest: Path) -> Optional[dict]:
    """Parse the destination sentinel, or None when there is no usable one.

    Unreadable / unparseable / wrong-version all collapse to None on purpose:
    the consequence of None is a re-merge, which is a verified no-op. Trusting
    a record we could not parse would be the expensive direction.
    """
    path = dest / SENTINEL_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != SENTINEL_VERSION:
        return None
    return data


def _write_sentinel(dest: Path, result: "MigrationResult") -> bool:
    """Record the verified copy. Written LAST, and only on full success."""
    from vco_lib.atomic import atomic_write_json

    payload = {
        "version": SENTINEL_VERSION,
        "migrated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(result.source),
        "dest": str(result.dest),
        "note": (
            "COPY, not move: the source files above are a frozen archive. VCO "
            "never writes to them and never deletes them; removing them is a "
            "user decision."
        ),
        "files": {
            f.name: {
                "source_sha256": f.source_sha256,
                "source_lines": f.source_lines,
                "source_bytes": f.source_bytes,
                "appended_lines": f.appended_lines,
                "dest_lines_after": f.dest_lines_after,
            }
            for f in result.files
            if not f.error
        },
    }
    try:
        dest.mkdir(parents=True, exist_ok=True)
        atomic_write_json(dest / SENTINEL_NAME, payload)
        return True
    except OSError:
        return False


def _copy_one(
    src_file: Path,
    dest_file: Path,
    recorded: Optional[dict],
    dry_run: bool,
) -> FileCopyRecord:
    """Merge ONE file and verify both halves (the act and the leave-alone)."""
    rec = FileCopyRecord(name=src_file.name)
    try:
        rec.source_lines, rec.source_bytes, rec.source_sha256 = _measure(src_file)
        source_lines = _read_lines(src_file)
        dest_lines = _read_lines(dest_file)
        rec.dest_lines_before, rec.dest_bytes_before, _ = _measure(dest_file)
    except OSError as exc:
        rec.error = f"read failed: {exc}"
        return rec

    # Fast path: the sentinel already records this exact source, and the
    # destination still satisfies it. Skipping is what keeps a per-session
    # call cheap on a machine that migrated months ago.
    if (
        recorded
        and recorded.get("source_sha256") == rec.source_sha256
        and rec.dest_lines_before >= recorded.get("dest_lines_after", 0)
        and not _lines_to_append(source_lines, dest_lines)
    ):
        rec.skipped_unchanged = True
        rec.verified = True
        rec.dest_lines_after = rec.dest_lines_before
        rec.dest_bytes_after = rec.dest_bytes_before
        return rec

    pending = _lines_to_append(source_lines, dest_lines)

    if dry_run:
        rec.appended_lines = len(pending)
        rec.dest_lines_after = rec.dest_lines_before + len(pending)
        rec.dest_bytes_after = rec.dest_bytes_before
        rec.verified = True
        return rec

    try:
        bytes_written = _append_lines(dest_file, pending)
    except OSError as exc:
        rec.error = f"append failed: {exc}"
        # Re-measure the source anyway: a failed run still owes the caller an
        # honest answer about whether the ORIGINAL survived.
        rec.source_unchanged = _measure(src_file) == (
            rec.source_lines, rec.source_bytes, rec.source_sha256
        )
        return rec

    rec.appended_lines = len(pending)
    rec.dest_lines_after, rec.dest_bytes_after, _ = _measure(dest_file)

    # ── VERIFY (the act) ────────────────────────────────────────────────────
    # Line count, byte count, and containment, all three. Byte arithmetic
    # alone would miss a wrong-content append; containment alone would miss a
    # truncation somewhere else in the file.
    expected_lines = rec.dest_lines_before + len(pending)
    expected_bytes = rec.dest_bytes_before + bytes_written
    final_counts = Counter(_read_lines(dest_file))
    contains_all = all(
        final_counts.get(line, 0) >= count
        for line, count in Counter(source_lines).items()
    )
    rec.verified = (
        rec.dest_lines_after == expected_lines
        and rec.dest_bytes_after == expected_bytes
        and contains_all
    )
    if not rec.verified:
        rec.error = (
            f"verification failed: lines {rec.dest_lines_after} != "
            f"{expected_lines} or bytes {rec.dest_bytes_after} != "
            f"{expected_bytes} or missing rows (contains_all={contains_all})"
        )

    # ── VERIFY (the leave-alone) ────────────────────────────────────────────
    # The original must be byte-identical to what we measured before touching
    # anything. Proved with a digest, not by inspection of the code above.
    rec.source_unchanged = _measure(src_file) == (
        rec.source_lines, rec.source_bytes, rec.source_sha256
    )
    if not rec.source_unchanged:
        rec.verified = False
        rec.error = (
            "the archived original changed during the copy — it must be left "
            "byte-identical"
        )
    return rec


def _same_directory(a: Path, b: Path) -> bool:
    """True when two paths name the same directory (symlinks resolved).

    Conservative on error: a path we cannot resolve is treated as DIFFERENT,
    because the alternative — refusing a legitimate migration because a stat
    failed — strands the user's history for a filesystem hiccup.
    """
    try:
        if not (a.is_dir() and b.is_dir()):
            return False
        return a.resolve() == b.resolve()
    except OSError:
        return False


def migrate_metrics(
    source: Optional[Path] = None,
    dest: Optional[Path] = None,
    *,
    dry_run: bool = False,
) -> MigrationResult:
    """Copy the legacy metrics archive into the new home. Never moves.

    Args:
        source: archive dir. Defaults to :func:`vco_lib.paths.legacy_claude_metrics_dir`.
        dest: new metrics home. Defaults to :func:`vco_lib.paths.vct_metrics_dir`.
        dry_run: measure and report; write nothing (not even the sentinel).

    Returns:
        A :class:`MigrationResult`. Never raises for an expected condition —
        an unreadable file becomes an ``error`` on that file's record and a
        ``failed`` status, because a migration that aborts halfway with a
        traceback is exactly the interruption this design exists to survive.
    """
    src = Path(source) if source is not None else legacy_claude_metrics_dir()
    dst = Path(dest) if dest is not None else vct_metrics_dir()
    result = MigrationResult(source=str(src), dest=str(dst), dry_run=dry_run)

    if _same_directory(src, dst):
        # Degenerate but reachable: $VCT_CLAUDE_DIR and $VCT_STATE_DIR pointed
        # at the same tree. Copying a directory onto itself would double every
        # row on the first run. Refuse rather than "helpfully" proceed.
        result.status = "failed"
        result.errors.append("source and destination are the same directory")
        return result

    if not src.is_dir():
        # Fresh install: a clean no-op. Deliberately creates nothing — not the
        # destination, not a sentinel — so an install that never had metrics
        # leaves no trace of a migration that had nothing to do.
        result.status = "no_source"
        return result

    try:
        sources = sorted(p for p in src.glob(SOURCE_GLOB) if p.is_file())
    except OSError as exc:
        result.status = "failed"
        result.errors.append(f"cannot list {src}: {exc}")
        return result

    sentinel = _load_sentinel(dst)
    recorded_files = (sentinel or {}).get("files") or {}

    for src_file in sources:
        rec = _copy_one(
            src_file,
            dst / src_file.name,
            recorded_files.get(src_file.name) if isinstance(recorded_files, dict) else None,
            dry_run,
        )
        result.files.append(rec)
        if rec.error:
            result.errors.append(f"{rec.name}: {rec.error}")

    if result.errors:
        result.status = "failed"
        return result

    every_file_skipped = bool(result.files) and all(
        f.skipped_unchanged for f in result.files
    )
    if sentinel is not None and (every_file_skipped or not result.files):
        result.status = "already_current"
    else:
        result.status = "migrated"

    if not dry_run:
        # Written LAST: the sentinel is the record that the copy VERIFIED, and
        # the shell helper reads it as permission for writers to move. Writing
        # it before the copy completed would move writers off a history that
        # had not arrived yet.
        result.sentinel_written = _write_sentinel(dst, result)
        if not result.sentinel_written:
            result.status = "failed"
            result.errors.append(
                f"could not write {dst / SENTINEL_NAME}; writers stay on the "
                f"archive until they can"
            )
    return result


def ensure_metrics_migrated(
    source: Optional[Path] = None,
    dest: Optional[Path] = None,
) -> MigrationResult:
    """Idempotent, cheap-after-the-first-time entry point.

    Safe to call on every session start / every CLI invocation: when the
    archive does not exist it is a single ``is_dir()``; when the sentinel is
    current it is one small JSON read plus one digest per archived file.

    Concurrency: the whole run is taken under an exclusive lock in the
    destination directory, so two Claude sessions starting at the same moment
    cannot both append the same rows. If the lock cannot be taken (Windows
    without ``fcntl`` falls back to a no-op lock in
    :func:`vco_lib.atomic.exclusive_file_lock`), the multiset dedup is the
    second line of defence — a doubled run appends nothing the destination
    already holds.
    """
    src = Path(source) if source is not None else legacy_claude_metrics_dir()
    dst = Path(dest) if dest is not None else vct_metrics_dir()
    if not src.is_dir():
        return MigrationResult(status="no_source", source=str(src), dest=str(dst))

    from vco_lib.atomic import exclusive_file_lock

    try:
        dst.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(dst / (SENTINEL_NAME + ".lock")):
            return migrate_metrics(src, dst)
    except OSError as exc:
        return MigrationResult(
            status="failed",
            source=str(src),
            dest=str(dst),
            errors=[f"could not prepare {dst}: {exc}"],
        )


def _format_human(result: MigrationResult) -> str:
    lines: list[str] = []
    if result.status == "no_source":
        return (
            f"metrics-migration: nothing to do — no legacy archive at "
            f"{result.source}"
        )
    verb = "would copy" if result.dry_run else "copied"
    lines.append(f"metrics-migration: {result.source} -> {result.dest}")
    for f in result.files:
        if f.error:
            lines.append(f"  ! {f.name}: {f.error}")
        elif f.skipped_unchanged:
            lines.append(f"  = {f.name}: already current ({f.source_lines} rows)")
        else:
            lines.append(
                f"  + {f.name}: {verb} {f.appended_lines} of {f.source_lines} rows "
                f"(dest {f.dest_lines_before} -> {f.dest_lines_after})"
            )
    lines.append(f"  status: {result.status}")
    lines.append(
        "  originals: "
        + (
            "unchanged (frozen archive, deleted by nobody)"
            if result.originals_untouched
            else "CHANGED — this is a failure, report it"
        )
    )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.metrics_migration",
        description=(
            "COPY the legacy ~/.claude/metrics history into ~/.vct/metrics. "
            "The originals are left byte-identical and are never deleted."
        ),
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be copied; write nothing",
    )
    parser.add_argument("--source", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--dest", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--quiet", action="store_true",
        help="print nothing on success (for hook use)",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        result = migrate_metrics(args.source, args.dest, dry_run=True)
    else:
        result = ensure_metrics_migrated(args.source, args.dest)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    elif not args.quiet or not result.ok:
        stream = sys.stdout if result.ok else sys.stderr
        print(_format_human(result), file=stream)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
