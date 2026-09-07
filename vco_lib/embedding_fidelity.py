# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Embedding-fidelity surfacing — the shrink/refusal leg of the failure
surface (v0.2.92 W4/W5).

The problem this module closes
------------------------------
``~/.vct/metrics/embedding_failures.jsonl`` — the file the
``embedding-failures-surface`` SessionStart hook points Claude at — was
written ONLY when ``EmbeddingService.for_project()`` raised
``NoEmbeddingBackendError`` (no backend reachable at construction). Two
v0.2.92 behaviours never reached it:

* **shrink-on-refusal** — ``truncate: false`` makes an over-window input a
  hard refusal, and ``_embed_shrinking_on_overflow`` retries under a tighter
  sub-window. On the ACTIVE slot that is a real fidelity loss on the vector
  retrieval reads, and its only disclosure was a WARNING to MCP stderr or a
  detached kg-sync's stdout — i.e. to nobody.
* **floor refusal** — the shrink ladder exhausted (input still refused at
  the floor), so that slot got NO vector at all.

kg-sync's "See ~/.claude/metrics/embedding_failures.jsonl for details."
therefore pointed at a file those refusals never landed in.

Granularity — PER-RUN SUMMARY, deliberately
-------------------------------------------
A shrink is not the same severity as a hard backend failure, and it is not
an event a user acts on individually: on dense corpora (markdown tables,
box-drawing, CJK) a large fraction of chunks legitimately shrink. Per-node
rows would spam the file and the surface. So:

* ``note_shrink`` / ``note_floor_refusal`` are IN-MEMORY counters — zero IO
  per chunk, no per-node rows, ever.
* ``flush_run()`` appends exactly ONE ``"kind": "shrink_summary"`` row per
  embedding run (a process) to the SAME jsonl, carrying per-model counts,
  character totals and the last floor-refusal message.
* The first note registers an ``atexit`` flush, so a run that never calls
  ``flush_run()`` explicitly (a detached kg-sync, an MCP subprocess) still
  lands its summary when the process exits. Explicit flush is also fine and
  resets the counters.
* Legacy outage rows have no ``kind`` field; the reader below treats a
  missing ``kind`` as an outage and never renders it here. Severity
  separation lives in the DATA, not in the renderer's guesswork.

The reader side (``--notice``)
------------------------------
``python -m vco_lib.embedding_fidelity notice --project-root <root>`` is
what the SessionStart hook spawns (both the ``.sh`` and ``.ps1`` siblings —
the A-leg of the cross-language rule: ONE implementation, two thin shell
wrappers). It:

* resolves the jsonl through :func:`vco_lib.paths.metrics_read_dirs` (new
  home before the frozen archive — the same read discipline every metrics
  reader uses),
* consumes only COMPLETE new lines past a per-project byte-offset marker at
  ``<project-root>/.claude/state/embedding-fidelity.seen``,
* prints ONE combined notice for the new summary rows — worded as a
  fidelity note, explicitly "NOT an outage" — or nothing at all, and

* always exits 0 (soft-fail: a broken notice must never block
  SessionStart; errors go to stderr).

The marker makes the notice once-per-new-rows per project session start,
so a machine whose syncs routinely shrink does not get nagged on every
session — only when a NEW run summary has landed since this project last
looked.

Wiring status — read this before crediting the module
----------------------------------------------------
* **Reader: LIVE.** ``templates/hooks/embedding-failures-surface.{sh,ps1}``
  spawn ``python -m vco_lib.embedding_fidelity notice`` on SessionStart.
* **Outage writer: LIVE for kg-sync.**
  ``templates/scripts/sync_knowledge_graph.py`` calls
  :func:`append_outage_row` before its "No embedding backend produced a
  vector" raise, so that message's jsonl pointer is true for the
  call-time-failure population the construction-time capture never covers.
* **Shrink / floor-refusal writers: LIVE.**
  ``vco_lib/embedding_service.py`` reaches :func:`note_shrink` and
  :func:`note_floor_refusal` through its guarded ``_note_fidelity`` helper,
  called from inside ``_embed_shrinking_on_overflow`` — the ONE home all four
  embed paths route through, so those two call sites cover primary text,
  secondary text, active code and the CodeEmbed service leg at once.
  ``note_shrink`` is in the LOOP, not after it, so a multi-step ladder is
  recorded once per accepted embed with the length actually sent; both calls
  are two dict updates and no IO, so they are safe on a per-chunk path and
  the single summary row lands at process exit.

Re-run the check before crediting any of the above in prose — this list has
been wrong in BOTH directions inside one release cycle (writers credited
while unwired, then described as unwired the hour after they landed)::

    grep -rnE "note_shrink|note_floor_refusal|append_outage_row" \
        --include=*.py . | grep -v tests/

Every writer named above must show a caller OUTSIDE this file. A writer with
no caller leaves the SessionStart notice silent while the module reads as
covered, which is the exact defect shape this cycle kept reproducing — and a
grep is only evidence of the call SITE; the behavioural proof (a real embed
against a refusing backend, then assert a ``shrink_summary`` row appears) is
in ``tests/test_embedding_fidelity_surface.py``.
"""

from __future__ import annotations

import atexit
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

#: Row kind for a per-run shrink/floor-refusal summary. Legacy outage rows
#: (written by ``NoEmbeddingBackendError`` capture) carry no ``kind`` — the
#: reader treats missing as outage and never renders it here.
KIND_SHRINK_SUMMARY = "shrink_summary"

#: Marker file (relative to a project root) recording how much of the jsonl
#: this project's sessions have already surfaced.
MARKER_REL = Path(".claude") / "state" / "embedding-fidelity.seen"


class _RunState:
    """In-memory per-process counters. One row per run, not per chunk."""

    __slots__ = ("started", "shrinks", "floor_refusals")

    def __init__(self) -> None:
        self.started = datetime.now(timezone.utc)
        # {model_id: {"count": int, "orig_chars": int, "sent_chars": int}}
        self.shrinks: dict[str, dict[str, int]] = {}
        # {model_id: {"count": int, "last_chars": int, "last_message": str}}
        self.floor_refusals: dict[str, dict[str, object]] = {}


_STATE = _RunState()
_ATEXIT_REGISTERED = False


def reset_run_state() -> None:
    """Zero the in-memory counters (test seam; also the post-flush reset)."""
    global _STATE
    _STATE = _RunState()


def note_shrink(model_id: str, orig_chars: int, sent_chars: int) -> None:
    """Record that ``model_id`` accepted only ``sent_chars`` of an
    ``orig_chars``-long input after a refusal-driven shrink.

    In-memory only — the row lands at :func:`flush_run` (or the auto-registered
    atexit flush). Cheap enough to call per chunk: two dict updates.
    """
    _register_atexit()
    entry = _STATE.shrinks.setdefault(
        model_id, {"count": 0, "orig_chars": 0, "sent_chars": 0},
    )
    entry["count"] += 1
    entry["orig_chars"] += max(0, int(orig_chars))
    entry["sent_chars"] += max(0, int(sent_chars))


def note_floor_refusal(model_id: str, chars: int, message: str) -> None:
    """Record that ``model_id`` still refused an input at the shrink floor —
    that slot got NO vector. Rare by construction and actionable, so the last
    message is kept as an exemplar inside the summary row."""
    _register_atexit()
    entry = _STATE.floor_refusals.setdefault(
        model_id, {"count": 0, "last_chars": 0, "last_message": ""},
    )
    # The row is heterogeneous (two ints + a string) so the value type is
    # ``object``; narrow before arithmetic rather than ``int(...)``-ing an
    # ``object``, which pyright rejects (reportArgumentType) and which would
    # raise at runtime on a malformed entry instead of counting.
    prior = entry.get("count", 0)
    entry["count"] = (prior if isinstance(prior, int) else 0) + 1
    entry["last_chars"] = max(0, int(chars))
    entry["last_message"] = str(message)[:400]


def _register_atexit() -> None:
    """First note wins: flush whatever accumulated when the process exits.

    This is what makes the writer COMPLETE without end-of-run wiring in the
    callers — a detached kg-sync that never calls ``flush_run`` still lands
    its summary. Idempotent; errors in the atexit flush are swallowed (an
    embedding run must never fail because its telemetry could not be
    written).
    """
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return
    _ATEXIT_REGISTERED = True
    atexit.register(_auto_flush)


def _auto_flush() -> None:
    try:
        flush_run()
    except Exception:  # noqa: BLE001 — telemetry must never break the run
        pass


def _jsonl_path() -> Path:
    """The shared ``embedding_failures.jsonl`` (same file the outage rows
    and the hook's pointer use). Local import so a partial-install state
    cannot break import of this module's note-side functions."""
    from vco_lib.paths import vct_metrics_dir

    return vct_metrics_dir() / "embedding_failures.jsonl"


def failures_jsonl_display_path() -> str:
    """The path a FAILURE MESSAGE should name, as text.

    Exists because four shipped scripts printed the literal
    ``~/.claude/metrics/embedding_failures.jsonl`` — which v0.2.92 W7 turned
    into a read-only ARCHIVE. Every row now lands under ``vct_metrics_dir()``
    (``~/.vct/metrics``), including the ``NoEmbeddingBackendError`` capture:
    ``embedding_service._failure_jsonl_path`` resolves through
    ``paths.claude_metrics_dir``, which since W7 is a DEPRECATED ALIAS for
    ``vct_metrics_dir`` and no longer returns anything under ``~/.claude``.
    So the messages named a file the rows were not in — the same
    pointer-is-false defect this module was written to close, one layer out.

    Soft-fails to a descriptive placeholder rather than raising: this is
    called from inside error paths, and a broken hint must never replace the
    failure the user actually needs to read.
    """
    try:
        return str(_jsonl_path())
    except Exception:  # noqa: BLE001 — never mask the caller's real error
        return "<vct-state-dir>/metrics/embedding_failures.jsonl"


def flush_run(*, reset: bool = True) -> bool:
    """Append ONE summary row for the accumulated notes. No-op (returns
    False) when nothing was noted — a clean run writes nothing, keeping the
    file signal-dense. Soft-fails on IO errors (logs to stderr, returns
    False) — same discipline as the outage writer.

    ``reset`` (default True) zeroes the in-memory state so a subsequent
    explicit flush does not double-count; the atexit flush after an explicit
    one is therefore a no-op.
    """
    # (No `global _STATE` here: this function only READS the module state;
    # the rebind happens inside `reset_run_state`, which declares its own.)
    if not _STATE.shrinks and not _STATE.floor_refusals:
        return False
    row = {
        "kind": KIND_SHRINK_SUMMARY,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_started": _STATE.started.isoformat(),
        "pid": _get_pid(),
        "shrinks": _STATE.shrinks,
        "floor_refusals": _STATE.floor_refusals,
    }
    path = _jsonl_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        print(
            f"[embedding-fidelity] failed to append run summary: {exc}",
            file=sys.stderr,
        )
        return False
    if reset:
        reset_run_state()
    return True


def _get_pid() -> int:
    import os

    return os.getpid()


def append_outage_row(message: str) -> bool:
    """Append an outage-kind row on behalf of a caller that is about to
    raise/point at the jsonl without having gone through
    ``NoEmbeddingBackendError`` (whose capture writes these rows).

    The concrete caller (owned by another lane): kg-sync's
    ``_build_vector_arg`` raises ``RuntimeError("No embedding backend
    produced a vector. See ~/.claude/metrics/embedding_failures.jsonl …")``
    when construction SUCCEEDED but every per-slot embed failed at call
    time — a population the construction-time capture never covers, so the
    message's pointer was false for exactly them. One call before the raise
    makes it true. Soft-fails like everything here.
    """
    row = {
        "kind": "outage",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pid": _get_pid(),
        "message": str(message)[:800],
    }
    path = _jsonl_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        print(
            f"[embedding-fidelity] failed to append outage row: {exc}",
            file=sys.stderr,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Reader side — the notice the SessionStart hook surfaces
# ---------------------------------------------------------------------------


def _read_marker(marker_path: Path) -> int:
    """The byte offset this project already surfaced. 0 when absent/invalid
    (a missing marker means "surface everything readable" — correct for a
    first run on a machine with history)."""
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
        offset = int(data.get("offset", 0))
        return max(0, offset)
    except (OSError, ValueError):
        return 0


def _write_marker(marker_path: Path, offset: int) -> None:
    """Persist the consumed offset. Soft-fail: a marker we cannot write only
    means the next session surfaces the same rows again (a repeat notice,
    not a lost one)."""
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps({"offset": int(offset)}), encoding="utf-8",
        )
    except OSError:
        pass


def _new_summary_rows(
    jsonl: Path, marker_path: Path,
) -> "tuple[list[dict], int]":
    """Complete ``kind=shrink_summary`` rows beyond the marker offset.

    Returns ``(rows, new_offset)`` — ``new_offset`` is the end of the last
    COMPLETE line consumed (a concurrently-written partial final line is
    left for the next read; its bytes are not marked consumed). Outage rows
    past the marker advance the offset but are not returned: they belong to
    the EMBEDDING_FAILURES.md banner mechanism, not this one.
    """
    offset = _read_marker(marker_path)
    rows: list[dict] = []
    try:
        size = jsonl.stat().st_size
    except OSError:
        return [], offset
    if size <= offset:
        return [], offset
    try:
        with jsonl.open("rb") as fh:
            fh.seek(offset)
            raw = fh.read()
    except OSError:
        return [], offset
    consumed = 0
    for line in raw.split(b"\n")[:-1]:  # drop the partial tail
        consumed += len(line) + 1
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except ValueError:
            continue  # foreign/partial row — advance past, never crash
        if isinstance(record, dict) and record.get("kind") == KIND_SHRINK_SUMMARY:
            rows.append(record)
    return rows, offset + consumed


def _fmt_row(record: dict) -> str:
    """One summary row as notice lines — counts, not per-chunk detail."""
    lines = []
    shrinks = record.get("shrinks") or {}
    for model in sorted(shrinks):
        s = shrinks[model]
        count = s.get("count", 0)
        orig = s.get("orig_chars", 0)
        sent = s.get("sent_chars", 0)
        pct = f" ({100 * sent // orig}% of the text)" if orig else ""
        lines.append(
            f"  - {model}: {count} chunk(s) embedded from a leading "
            f"sub-window{pct}"
        )
    refusals = record.get("floor_refusals") or {}
    for model in sorted(refusals):
        r = refusals[model]
        lines.append(
            f"  - {model}: {r.get('count', 0)} chunk(s) got NO vector "
            "(shrink ladder exhausted — last error: "
            f"{str(r.get('last_message', ''))[:120]})"
        )
    ts = str(record.get("timestamp", "?"))
    head = f"run at {ts}:"
    return head + "\n" + "\n".join(lines)


def render_notice(rows: list[dict], jsonl_path: Path) -> str:
    """The Claude-facing notice for the given summary rows ("" when none).

    Wording is the load-bearing constraint: a shrink is NOT an outage. The
    first line says so explicitly, and the notice never claims backends were
    down — they were reachable; the vectors are just leading-window ones.
    """
    if not rows:
        return ""
    body = "\n".join(_fmt_row(r) for r in rows)
    return (
        "===================================================================\n"
        "Embedding fidelity note (NOT an outage): recent embedding run(s)\n"
        "stored some vectors from leading sub-windows after the runner\n"
        "refused the full input. Backends were reachable; retrieval on the\n"
        "affected slots sees less of those chunks than usual.\n"
        "Claude: no action needed unless retrieval quality matters for\n"
        "these slots; per-run details (counts per model) below and in:\n"
        f"  {jsonl_path}\n"
        f"{body}\n"
        "==================================================================="
    )


def emit_notice(project_root: Path) -> int:
    """CLI entry: print the notice for new summary rows, advance the marker.

    Always returns 0 — a broken notice must not block SessionStart. Errors
    go to stderr; stdout carries ONLY the notice (or nothing).
    """
    from vco_lib.paths import metrics_read_dirs

    marker_path = project_root / MARKER_REL
    jsonl: Optional[Path] = None
    for base in metrics_read_dirs():
        candidate = base / "embedding_failures.jsonl"
        if candidate.is_file():
            jsonl = candidate
            break
    if jsonl is None:
        return 0
    rows, new_offset = _new_summary_rows(jsonl, marker_path)
    notice = render_notice(rows, jsonl)
    if notice:
        print(notice)
    _write_marker(marker_path, new_offset)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.embedding_fidelity",
        description=(
            "Surface per-run embedding-fidelity summaries (shrinks after "
            "refusals, floor refusals) recorded in embedding_failures.jsonl."
        ),
    )
    sub = parser.add_subparsers(dest="cmd")
    notice = sub.add_parser(
        "notice",
        help="print one combined notice for rows not yet surfaced",
    )
    notice.add_argument(
        "--project-root", required=True,
        help="the project whose .claude/state marker deduplicates runs",
    )
    args = parser.parse_args(argv)
    if args.cmd == "notice":
        return emit_notice(Path(args.project_root).resolve())
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    raise SystemExit(main())
