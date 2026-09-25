# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.96 WP-1: blocking child spawns whose output never lands on inherited
pipes.

Register issue 1 (2026-09-20 update-resume incident class): install.py's
KG-seed children were spawned with inherited stdio while install.py's own
stdout/stderr were launcher-owned pipes. The old launcher's reader does not
necessarily drain both pipes concurrently, the child's ~64 KB pipe buffer
filled, the child blocked mid-write, and install.py blocked in
``subprocess.run`` — the user sits at "Seeding" forever
(LANE-PLAN-REVIEW-V0296 m-1/M-3).

``run_child_logged`` is the ONE home for that spawn shape (review n-1: no
helper owned it). Design rules, modelled on the detached-spawn precedent in
``vco_lib/codegraph_resync.py`` (per-spawn log + argv header + handle
hygiene — that file keeps its own local variant and is untouched):

* child stdout+stderr merge into ONE pipe drained line-by-line by the
  parent, appended to ``<vct_root_dir>/logs/<log_stem>-<UTC>.log`` (one
  log per run, header line records the argv — the 2026-09-09 incident was
  only solvable by re-deriving the spawn chain because no log named it).
  A single merged stream is what makes the relay starvation-proof: with
  two pipes, a flood on one can starve reads on the other.
* TTY parents (a human at a terminal) still see everything — child lines
  are tee'd to the terminal.
* NON-TTY parents (the launcher) get a bounded, line-oriented RELAY of
  child lines matching ``^\\[VCO-EVENT\\]`` to the parent's own stdout.
  That relay is install.py's only progress source while it blocks on the
  child (WP-8's heartbeat source, review M-3), so it must keep flowing
  while the child floods stderr. Every relayed write is flush-and-drop:
  a slow or closed parent stdout must never wedge the drain loop whose
  entire purpose is to prevent that class of deadlock.
* ``subprocess.run`` semantics preserved: returns a ``CompletedProcess``,
  ``check=True`` + non-zero exit raises ``CalledProcessError`` exactly as
  the previous inline spawns did, a missing executable still raises
  ``FileNotFoundError``, and an interrupted parent still kills the child.
  There is NO timeout parameter on purpose (v0.2.69 FIX 3 — the seed path
  is un-timed; the guard lives per-embed-request in EmbeddingService).

WP-1 review NOTEs — dispositions (v0.2.96 WP-8; do not silently re-litigate):

* NOTE-1 (relay is not gated on ``VCO_PROGRESS_STREAM``) — ACCEPTED
  ASYMMETRY, by design. install.py's own event mirroring is env-gated
  (the launcher sets ``VCO_PROGRESS_STREAM=1`` only when it actually has a
  progress modal to feed); this helper's relay is gated on "parent stdout
  is not a TTY" instead, because a PIPED reader has no other way to see
  the child's progress lines. The observable difference — a piped
  terminal capture of a CLI run sees child event lines but not
  install.py's own — is diagnostic output, not a machine contract, and
  the ungated relay behaviour is pinned by the WP-1 test suite
  (relay tests set no env). When the launcher drives an update both
  gates agree (env set + non-TTY).
* NOTE-2 (a healthy-but-stalled parent reader can wedge the drain via a
  blocking flush) — FIXED: ``_safe_parent_write`` flushes with the fd
  temporarily non-blocking on POSIX and drops the line on EAGAIN. The
  pre-fix equivalence with inherited stdio is thereby removed on POSIX;
  on Windows (where ``os.set_blocking`` does not apply to pipes) the
  flush stays blocking — bounded there by the launcher's own concurrent
  drain (v0.2.95+), which never leaves the pipe unread for long.
* NOTE-3 (>8 KB lines split by ``readline``; UTF-8 multibyte split across
  chunks) — NOT REPRODUCIBLE, documented: ``BufferedReader.readline`` on
  a pipe returns COMPLETE lines (verified empirically: a single 100 KB
  line arrives as one ``readline`` result, CPython/Linux), so the relay
  never sees a truncated event and a multibyte char is never decoded
  across a chunk boundary mid-line. The one real edge — a final line
  with no trailing newline (EOF tail) — is already handled: it is
  relayed with a synthesized newline by ``_safe_parent_write``. The
  raw-bytes log is unaffected either way.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Optional

from vco_lib import progress_event
from vco_lib.paths import vct_root_dir

__all__ = ["last_json_object", "run_child_logged"]

logger = logging.getLogger(__name__)

#: The only child lines a non-TTY parent sees on its own stdout. Verbatim
#: forwarding — the GUI-side filter contract (phase ``start``/``ok``, label
#: mapping, detail length) is the CHILD's and WP-8's concern, not a reason
#: for this transport to edit payloads.
#:
#: v0.2.96 (D-1): built from ``progress_event.EVENT_PREFIX``, the ONE home
#: for the grammar, rather than re-typing the literal. A relay that filters
#: on a hand-copied prefix is a silent drop the moment the prefix moves —
#: and the drop looks exactly like "the child produced no progress", which
#: is the state the launcher's stall watchdog warns about.
_EVENT_LINE_RE = re.compile("^" + re.escape(progress_event.EVENT_PREFIX.rstrip()))


def last_json_object(stdout: Optional[str]) -> Optional[dict]:
    """The JSON object a ``--json`` child prints as its LAST ``{`` line, or ``None``.

    The ONE parser for "a vco_lib CLI's final report on stdout" (v0.2.97; it
    replaced ``machine_migrations._last_json_object``, which a second module
    was importing privately, and ``embedding_enrichment._last_json_line``).
    Stray non-JSON lines above the report — warnings, a venv banner — are
    tolerated.

    STRICT on the last candidate: the last line that starts with ``{`` IS the
    report. If it does not parse (a child killed mid-write) or is not an
    object, the answer is ``None`` — never an EARLIER ``{`` line. A child that
    streams JSON progress lines before its report (``embedding_enrichment
    --stream-progress``) would otherwise have a progress line read back as the
    report, and a caller checking ``report["failed"]`` would see success where
    there was a truncated failure.
    """
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _open_log(
    log_stem: str,
    cmd: Sequence[str],
    cwd: str | Path | None,
) -> Optional[tuple[IO[bytes], Path]]:
    """Open the per-run log file and write the argv header.

    Returns ``(handle, path)`` — the path is what failure paths name to the
    user (WP-1 review M-1: a child failure whose full log nobody can find
    is half a message). Returns ``None`` on ANY preparation failure —
    mirroring the resync precedent (R-5), a logging problem must never
    block or fail the spawn: the child then runs with its output discarded
    (the deadlock fix does not depend on the log existing, only on the
    child not inheriting the parent's pipes).
    """
    try:
        logs_dir = vct_root_dir() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", log_stem or "child")
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
        log_path = logs_dir / f"{safe}-{ts}.log"
        handle: IO[bytes] = open(log_path, "ab")
    except Exception:  # noqa: BLE001 — logging must not break the spawn
        logger.warning(
            "run_child_logged: log file unavailable; child output of %r "
            "will not be recorded", list(cmd), exc_info=True,
        )
        return None
    try:
        parts = [
            "# run_child_logged — spawned "
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\n",
            "# argv: " + " ".join(shlex.quote(str(a)) for a in cmd) + "\n",
        ]
        if cwd is not None:
            parts.append(f"# cwd: {cwd}\n")
        handle.write("".join(parts).encode("utf-8"))
        handle.flush()
    except OSError:
        logger.warning(
            "run_child_logged: log header write failed; child output of %r "
            "will not be recorded", list(cmd), exc_info=True,
        )
        try:
            handle.close()
        except OSError:
            pass
        return None
    return handle, log_path


def _note_failure_log(
    cmd: Sequence[str],
    returncode: Optional[int],
    log_path: Optional[Path],
) -> None:
    """Name the child's log on a failure path — stderr, never stdout.

    A library function cannot know its caller's stdout contract (the
    v0.2.84 JSON-contract rule: ``file=sys.stderr`` or a logger). One
    best-effort pair of lines; never raises.
    """
    try:
        shown = " ".join(str(a) for a in cmd)
        if len(shown) > 120:
            shown = shown[:117] + "..."
        rc = f"exit {returncode}" if returncode is not None else "spawn failed"
        where = (
            str(log_path) if log_path is not None
            else "not recorded (log unavailable)"
        )
        print(f"[child] {rc}: {shown}", file=sys.stderr)
        print(f"[child] full output log: {where}", file=sys.stderr)
    except Exception:  # noqa: BLE001 — a failure note must not raise
        pass


def _safe_parent_write(stream: IO[str], text: str) -> None:
    """Write + flush one line to the parent's stdout, dropping on error.

    The trailing-newline guarantee matters more than it looks: the
    launcher reads install.py's stdout line-oriented, and a relayed final
    line without ``\\n`` would sit in the pipe's buffer forever.
    """
    if not text.endswith("\n"):
        text += "\n"
    try:
        stream.write(text)
    except Exception:  # noqa: BLE001 — a slow/broken parent must not wedge us
        return
    # WP-1 review NOTE-2 (fixed v0.2.96 WP-8): flush with the fd
    # temporarily NON-BLOCKING on POSIX. A healthy-but-stalled parent
    # reader (the pre-0.2.95 launcher reading stdout to EOF before
    # draining stderr) fills the 64 KB pipe buffer; a blocking flush()
    # would then park THIS drain loop — the exact wedge this module
    # exists to prevent. Non-blocking, a full buffer raises
    # BlockingIOError and the line is DROPPED: one lost progress line
    # beats a dead update. The write above only buffers, so only the
    # flush needs the treatment. Streams without a real Python fd (test
    # fakes with no fileno) and Windows (os.set_blocking does not apply
    # to pipes) fall back to the plain blocking flush — bounded there by
    # the launcher's own concurrent drain (v0.2.95+).
    fd: int | None = None
    was_blocking: bool | None = None
    try:
        fd = stream.fileno()
        was_blocking = os.get_blocking(fd)
    except (AttributeError, OSError, ValueError):
        fd = None
    if fd is None:
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        os.set_blocking(fd, False)
        try:
            stream.flush()
        except BlockingIOError:
            pass  # pipe full: drop this line rather than park the drain loop
        except Exception:  # noqa: BLE001
            pass
    finally:
        if was_blocking is not None:
            try:
                os.set_blocking(fd, was_blocking)
            except OSError:
                pass


def _drain_child(
    proc: subprocess.Popen[bytes],
    *,
    log_handle: Optional[IO[bytes]],
    tee: bool,
    relay: bool,
    parent_stdout: IO[str],
) -> None:
    """Read the child's merged output to EOF: log it, route it, never stall.

    The log write is the one place a mid-run failure (disk full) is allowed
    to disable itself: the pipe MUST keep being drained or the child blocks
    on its own output — the exact defect this module exists to prevent.

    WP-1 review NOTE-3, disposition (v0.2.96 WP-8): ``readline`` on the
    buffered pipe returns COMPLETE lines — a single 100 KB line arrives as
    one result (verified empirically, CPython/Linux), so the relay never
    forwards a truncated event and a UTF-8 multibyte char is never decoded
    across a chunk boundary mid-line. The one real edge — a final line
    with no trailing newline at EOF — is relayed with a synthesized
    newline by ``_safe_parent_write``. No accumulation buffer is added on
    purpose: it would guard a split that does not occur, at the cost of a
    second buffer to bound.
    """
    child_out = proc.stdout
    assert child_out is not None, "stdout=PIPE guarantees a readable stream"
    log_ok = log_handle is not None
    for raw in iter(child_out.readline, b""):
        if log_ok and log_handle is not None:
            try:
                log_handle.write(raw)
            except OSError:
                log_ok = False
        if tee:
            _safe_parent_write(parent_stdout, raw.decode("utf-8", "replace"))
        elif relay:
            text = raw.decode("utf-8", "replace")
            if _EVENT_LINE_RE.match(text):
                _safe_parent_write(parent_stdout, text)


def run_child_logged(
    cmd: Sequence[str],
    *,
    log_stem: str,
    tee_when_tty: bool = True,
    relay_events: bool = True,
    check: bool = True,
    cwd: str | Path | None = None,
    env: Optional[Mapping[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Run ``cmd`` to completion with its output routed to a per-run log.

    See the module docstring for the full contract. Behaviour summary:

    * stdout+stderr → ``<vct_root_dir>/logs/<log_stem>-<UTC>.log``;
    * parent stdout is a TTY → every child line is tee'd to the terminal;
    * parent stdout is NOT a TTY and ``relay_events`` → child lines
      matching ``^\\[VCO-EVENT\\]`` are forwarded to the parent's stdout
      (line-oriented, flushed, dropped on write error);
    * ``check=True`` (default) raises ``subprocess.CalledProcessError`` on
      a non-zero exit, like ``subprocess.run(..., check=True)``.
    """
    parent_stdout = sys.stdout
    try:
        is_tty = bool(parent_stdout.isatty())
    except Exception:  # noqa: BLE001 — a broken isatty means "not a terminal"
        is_tty = False
    tee = tee_when_tty and is_tty
    relay = relay_events and not is_tty

    opened = _open_log(log_stem, cmd, cwd)
    log_handle: Optional[IO[bytes]] = opened[0] if opened is not None else None
    log_path: Optional[Path] = opened[1] if opened is not None else None
    proc: Optional[subprocess.Popen[bytes]] = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        _drain_child(
            proc,
            log_handle=log_handle,
            tee=tee,
            relay=relay,
            parent_stdout=parent_stdout,
        )
        returncode = proc.wait()
    except FileNotFoundError:
        _note_failure_log(cmd, None, log_path)
        raise
    except BaseException:
        # subprocess.run's own interruption semantics: kill the child so a
        # Ctrl-C never leaves it running with an unreachable parent.
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait()
        raise
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except OSError:
                pass
    if check and returncode != 0:
        _note_failure_log(cmd, returncode, log_path)
        raise subprocess.CalledProcessError(returncode, cmd)
    return subprocess.CompletedProcess(cmd, returncode)
