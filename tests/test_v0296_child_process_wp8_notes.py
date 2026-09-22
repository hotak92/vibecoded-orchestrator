# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.96 WP-8 — the three WP-1 review NOTEs in ``vco_lib/child_process.py``.

NOTE-1 (relay / ``VCO_PROGRESS_STREAM`` gate asymmetry) and NOTE-3 (alleged
>8 KB line splits) were adjudicated DOCUMENT-AS-ACCEPTED, with the reasoning
recorded in the module docstring — NOTE-1's asymmetry is by design (a piped
reader has no other way to see the child's progress) and NOTE-3's split
premise is not reproducible (``readline`` returns complete lines). This file
pins the two behaviours those verdicts depend on, and red-proofs the one
structural fix: NOTE-2, the non-blocking relay flush.
"""

from __future__ import annotations

import io
import os
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.child_process import run_child_logged  # noqa: E402


class _Capture(io.StringIO):
    """Non-TTY capture stand-in for ``sys.stdout`` (no real fd — also covers
    the fallback arm of the NOTE-2 flush when ``fileno`` is unavailable)."""

    def isatty(self) -> bool:
        return False


# ─── NOTE-2: a stalled reader must not wedge the drain loop ──────────────

# The child floods event lines far past the ~64 KiB OS pipe capacity while
# the "parent reader" (the read end of our pipe) deliberately never reads.
_FLOOD_CHILD = (
    "import sys\n"
    "for i in range(600):\n"
    "    sys.stdout.write('[VCO-EVENT] flood step ok %d ' % i + 'z' * 200 + '\\n')\n"
)


def test_note2_relay_flush_does_not_wedge_when_the_parent_pipe_is_full(
    tmp_path, monkeypatch
):
    """The 2026-09-20 hang, replayed one layer up: with a BLOCKING flush a
    full parent pipe parks the drain loop forever; with the NOTE-2
    non-blocking flush the drain completes and drops the undeliverable
    lines instead."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))

    r, w = os.pipe()
    wfile = os.fdopen(w, "wb", closefd=False)
    parent_stdout = io.TextIOWrapper(wfile, encoding="utf-8")

    def relay_in_thread() -> None:
        old = sys.stdout
        sys.stdout = parent_stdout
        try:
            run_child_logged(
                [sys.executable, "-c", _FLOOD_CHILD],
                log_stem="note2-flood",
                check=True,
            )
        finally:
            sys.stdout = old
            # NOT parent_stdout.close(): a TextIOWrapper close FLUSHES, and
            # flush of the leftover partial write against the never-drained
            # pipe would block — the exact hazard the fix exists to keep OUT
            # of the drain loop. Abandon the buffer and close the bare fd
            # instead: dropped bytes are the documented EAGAIN semantics.
            os.close(w)

    worker = threading.Thread(target=relay_in_thread, daemon=True)
    worker.start()
    # 600 lines x ~230 B ≈ 138 KiB against a 64 KiB pipe: a blocking flush
    # would wedge within the first hundred lines. 30 s is generous for the
    # non-blocking path (the child finishes in well under a second).
    worker.join(timeout=30)
    assert not worker.is_alive(), (
        "the drain loop wedged on a full parent pipe — the NOTE-2 "
        "non-blocking flush is not in effect"
    )

    # The read end was never drained while the child ran. Everything the
    # relay managed to push before the buffer filled is still in the pipe:
    # read it, and confirm relayed prefix is intact, ordered event lines.
    os.set_blocking(r, False)
    received = b""
    try:
        while True:
            chunk = os.read(r, 1 << 16)
            if not chunk:
                break
            received += chunk
    except BlockingIOError:
        pass
    finally:
        os.close(r)
    assert received.startswith(b"[VCO-EVENT] flood step ok 0 ")
    assert b"\n" in received  # line-oriented: no partial garbage fused

    # The LOG is the part that must be complete regardless of the parent:
    # the drain kept draining, so the child never blocked on its own output.
    logs = list((tmp_path / "logs").glob("note2-flood-*.log"))
    assert len(logs) == 1
    body = logs[0].read_bytes()
    # (startswith, not count-in-bytes: the argv header quotes the child code,
    # which itself contains the marker string.)
    logged = sum(
        1 for line in body.splitlines() if line.startswith(b"[VCO-EVENT] flood step ok ")
    )
    assert logged == 600


def test_note2_blocking_flag_is_restored_after_each_flush(tmp_path, monkeypatch):
    """The non-blocking dance must not leave the parent's stdout fd flipped:
    a fresh pipe is blocking by default, so "still blocking after the run"
    proves the finally-restore ran."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    r, w = os.pipe()
    wfile = os.fdopen(w, "wb", closefd=False)
    parent_stdout = io.TextIOWrapper(wfile, encoding="utf-8")
    flags_after: dict[str, bool] = {}

    def relay_in_thread() -> None:
        old = sys.stdout
        sys.stdout = parent_stdout
        try:
            run_child_logged(
                [sys.executable, "-c",
                 "import sys; sys.stdout.write('[VCO-EVENT] one ok\\n')"],
                log_stem="note2-flag",
                check=True,
            )
        finally:
            sys.stdout = old
            flags_after["blocking"] = os.get_blocking(w)
            parent_stdout.close()
            wfile.close()  # closefd=False: does NOT close the fd...
            os.close(w)  # ...so close it here, or the reader below never EOFs

    worker = threading.Thread(target=relay_in_thread, daemon=True)
    worker.start()
    worker.join(timeout=30)
    assert not worker.is_alive()
    assert flags_after["blocking"] is True, (
        "the relay left the parent's stdout fd non-blocking"
    )
    got = b""
    while True:
        chunk = os.read(r, 1 << 16)
        if not chunk:
            break
        got += chunk
    os.close(r)
    assert got == b"[VCO-EVENT] one ok\n"


# ─── NOTE-3 + NOTE-1 pins (documented-as-accepted) ───────────────────────


def test_note3_single_huge_event_line_relays_complete(tmp_path, monkeypatch):
    """``readline`` returns complete lines (a 100 KB line arrives as ONE
    result — the premise of the WP-1 review's NOTE-3 was that it splits).
    This pins the documented behaviour: the relayed text is the WHOLE line,
    byte-for-byte, with no synthesized boundary inside it."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    payload = "[VCO-EVENT] huge step ok " + ("A" * 100_000) + "\n"
    child = (
        "import sys\n"
        f"sys.stdout.write({payload!r})\n"
        "sys.stdout.write('[VCO-EVENT] sentinel done\\n')\n"
    )
    cap = _Capture()
    monkeypatch.setattr(sys, "stdout", cap)
    run_child_logged(
        [sys.executable, "-c", child], log_stem="note3-huge", check=True
    )
    text = cap.getvalue()
    assert payload in text, "the relayed event line must be complete"
    assert "[VCO-EVENT] sentinel done\n" in text
    assert text.count("[VCO-EVENT] huge step ok ") == 1


def test_note3_eof_tail_without_newline_is_relayed_with_synthesized_newline(
    tmp_path, monkeypatch
):
    """The one real NOTE-3 edge: a final line with no trailing newline must
    not sit in a buffer forever — the launcher reads line-oriented."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    cap = _Capture()
    monkeypatch.setattr(sys, "stdout", cap)
    run_child_logged(
        [sys.executable, "-c",
         "import sys; sys.stdout.write('[VCO-EVENT] tail ok no-newline')"],
        log_stem="note3-tail",
        check=True,
    )
    assert cap.getvalue().endswith("[VCO-EVENT] tail ok no-newline\n")


def test_note1_relay_is_not_gated_on_vco_progress_stream(tmp_path, monkeypatch):
    """NOTE-1, accepted-as-designed: the relay fires whenever the parent
    stdout is not a TTY, with NO env gate (the gate belongs to install.py's
    own emitter). If this pin ever needs to flip, re-read the module
    docstring first — the WP-1 suite pins the same behaviour."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("VCO_PROGRESS_STREAM", raising=False)
    cap = _Capture()
    monkeypatch.setattr(sys, "stdout", cap)
    run_child_logged(
        [sys.executable, "-c",
         "import sys; sys.stdout.write('[VCO-EVENT] ungated ok\\n')"],
        log_stem="note1-ungated",
        check=True,
    )
    assert "[VCO-EVENT] ungated ok\n" in cap.getvalue()
