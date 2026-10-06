# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""RL telemetry LOSS ledger — the one home for "a training event was not written".

Why this exists (v0.2.100)
--------------------------
Standing owner rule: RL is optional, but its training logs are ALWAYS
collected. Two ways a log can go missing were invisible until now:

* ``dual_skip`` — the dual-RL-log fan-out (the ``<task_id>:<slot>`` twin event
  for the OTHER embedding slot) was wanted (dual-log on, a distinct secondary
  slot configured) but could not be produced on this call: the secondary query
  embed overran the hook-path budget, the embed failed, or the query was
  oversized. Without a record, "fewer twins" is indistinguishable from "twins
  lost".
* ``dual_partial`` — the twin was written, but some nodes of the primary
  event had no vector for the other slot (never dual-written, and the hook path
  does not backfill), so the twin carries fewer nodes. Counted apart from the
  event losses.
* ``hub_post_failed`` — the vct-hub POST of an RL event failed (hub not
  running, connection refused, timeout, a 4xx/5xx). The writer is soft-fail by
  design, and the connection-level cases used to be logged at DEBUG only, so
  the loss could not be measured.
* ``deferred_unsent`` — a deferred-emit child loaded events but never sent
  them (v0.2.101 NB-04: a loader/emit crash, a writer that raised, a validation
  failure). Recorded apart from ``hub_post_failed`` because no POST was ever
  attempted for these events, so nothing else names them.

Every such loss appends ONE JSON line to
``<vct_root>/metrics/rl_telemetry_loss.jsonl`` (the shared metrics home, beside
``embedding_failures.jsonl``) and logs one WARNING per (kind, reason) per
process — loud enough to be seen, rate-limited so a downed hub does not flood
an MCP's stderr. The ledger's reader is :func:`summarize`, which ``vco doctor``
renders as the ``rl_telemetry_loss`` probe.

Never raises: recording a loss must never become a second failure on the path
that is already losing an event.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

LOSS_FILE_NAME = "rl_telemetry_loss.jsonl"

#: The dual-log twin event was wanted but not produced on this call.
KIND_DUAL_SKIP = "dual_skip"
#: An RL event POST to vct-hub did not land.
KIND_HUB_POST_FAILED = "hub_post_failed"
#: A dual-log twin WAS written but some of the primary's nodes had no vector
#: for the other slot (``missing`` / ``nodes`` in the record). Not an event
#: loss — a partial one — so :func:`summarize` counts it apart (W5R-08).
KIND_DUAL_PARTIAL = "dual_partial"
#: A deferred-emit child (``claude_mcp_servers/rl_client/deferred_emit``)
#: loaded events but never sent them — a loader/emit crash, a writer that
#: raised, a validation failure (v0.2.101 NB-04). This is the ONLY deferred
#: case with no per-event line of its own: an event whose POST was ATTEMPTED
#: and failed is recorded per event under :data:`KIND_HUB_POST_FAILED`
#: instead, so the two never count the same event twice.
KIND_DEFERRED_UNSENT = "deferred_unsent"

#: Once the ledger passes ``_MAX_BYTES`` it is cut to its newest
#: ``_KEEP_LINES`` lines (through the repo's one rotation home,
#: ``vco_lib.atomic.rotate_tail_lines``). A downed hub produces a line per
#: event; the counts that matter are recent ones.
_MAX_BYTES = 2 * 1024 * 1024
_KEEP_LINES = 10_000

_warned: set = set()
_lock = threading.Lock()


def loss_log_path() -> Path:
    """``<vct_root>/metrics/rl_telemetry_loss.jsonl`` (via the one metrics home)."""
    from vco_lib.paths import vct_metrics_dir

    return vct_metrics_dir() / LOSS_FILE_NAME


def record_loss(kind: str, reason: str, **detail: Any) -> None:
    """Append one loss line and warn once per (kind, reason) per process.

    ``detail`` carries small, non-sensitive context (task_id, task_type,
    embedding_source, http status). Never pass query text or vectors.
    """
    key = (kind, reason)
    if key not in _warned:
        _warned.add(key)
        logger.warning(
            "RL telemetry loss: %s (%s) — recorded in %s; further occurrences "
            "in this process are counted there, not logged",
            kind, reason, LOSS_FILE_NAME,
        )
    try:
        path = loss_log_path()
        rec: dict[str, Any] = {
            "ts_ms": int(time.time() * 1000),
            "kind": kind,
            "reason": reason,
            "pid": os.getpid(),
        }
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
        if project_dir:
            rec["project_dir"] = project_dir
        for k, v in detail.items():
            if v is not None and v != "":
                rec[k] = v
        line = json.dumps(rec, separators=(",", ":"), default=str) + "\n"
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_and_append(path, line)
    except Exception as exc:  # noqa: BLE001 — a loss record never raises
        logger.debug("record_loss: could not append (%s)", exc)


#: Bound on waiting for another PROCESS's rotate+append. Appends take
#: microseconds; past this a writer appends WITHOUT rotating (an O_APPEND write
#: of one short line is atomic on its own) rather than stall a hook.
_LOCK_TIMEOUT_S = 1.0


def _rotate_and_append(path: Path, line: str) -> None:
    """Rotate (if due) and append under ONE cross-process lock (W5R-15).

    Hooks are separate processes. A per-process ``threading.Lock`` let one
    process rotate (read the tail, ``os.replace``) while another appended to
    the file being replaced, dropping that line at the 2 MiB boundary. The
    sidecar lock is the repo's one file-lock home,
    ``vco_lib.atomic.exclusive_file_lock`` (POSIX flock; best-effort on
    Windows, as everywhere else that uses it).
    """
    from vco_lib.atomic import LockTimeout, exclusive_file_lock, rotate_tail_lines

    try:
        with exclusive_file_lock(
            path.with_name(path.name + ".lock"), timeout_s=_LOCK_TIMEOUT_S
        ):
            rotate_tail_lines(path, max_bytes=_MAX_BYTES, keep_lines=_KEEP_LINES)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return
    except LockTimeout:
        pass
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)


def summarize(since_ms: Optional[int] = None, path: Optional[Path] = None) -> dict:
    """Aggregate the ledger: ``{"total", "by_kind": {kind: {reason: n}}, "last_ts_ms",
    "partial_twins", "partial_missing_nodes"}`` (``total`` / ``by_kind`` count
    lost EVENTS; partial twins are reported apart).

    ``since_ms`` limits the count to lines at or after that epoch-ms. Reads the
    ledger as it stands (older lines beyond the rotation tail are gone). A missing file is an
    empty summary; an unreadable one raises ``OSError`` so a caller can report
    UNKNOWN rather than a false "no losses".
    """
    p = path or loss_log_path()
    out: dict[str, Any] = {
        "total": 0,
        "by_kind": {},
        "last_ts_ms": None,
        # W5R-08: partial twins are not lost EVENTS; counted on their own.
        "partial_twins": 0,
        "partial_missing_nodes": 0,
    }
    if not p.exists():
        return out
    with open(p, encoding="utf-8") as fh:
        for raw in fh:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            ts = rec.get("ts_ms")
            if since_ms is not None and (not isinstance(ts, int) or ts < since_ms):
                continue
            kind = str(rec.get("kind", "unknown"))
            reason = str(rec.get("reason", "unknown"))
            if isinstance(ts, int) and (out["last_ts_ms"] is None or ts > out["last_ts_ms"]):
                out["last_ts_ms"] = ts
            if kind == KIND_DUAL_PARTIAL:
                out["partial_twins"] += 1
                missing = rec.get("missing")
                if isinstance(missing, int):
                    out["partial_missing_nodes"] += missing
                continue
            bucket = out["by_kind"].setdefault(kind, {})
            bucket[reason] = bucket.get(reason, 0) + 1
            out["total"] += 1
    return out


def _reset_warned_for_test() -> None:
    _warned.clear()


__all__ = [
    "KIND_DEFERRED_UNSENT",
    "KIND_DUAL_PARTIAL",
    "KIND_DUAL_SKIP",
    "KIND_HUB_POST_FAILED",
    "LOSS_FILE_NAME",
    "loss_log_path",
    "record_loss",
    "summarize",
]
