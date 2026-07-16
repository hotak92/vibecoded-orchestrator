# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Rate-limited fall-through warnings for the project-config resolver.

When a resolver client (``vct_project_config.sh``, ``.ps1``, or
:mod:`vco_lib.project_config`) falls through to its env-fallback path
because the hub is unreachable / the project isn't registered / a field
is missing, it emits ONE diagnostic line to stderr. Without rate-
limiting, a hook that fires hundreds of times per session (e.g.
``pre-edit-context-inject`` on a large refactor) would spam the terminal
with identical "hub unreachable" lines.

This module implements the rate-limit policy described in
``.claude/context/plans/v0.2.21-resolver-design.md`` §5 and the parent
plan ``.claude/context/plans/v0.2.21-hub-detachment-and-resolver.md``
step 17:

* Storage: ``<vct_root_dir>/cache/resolver_warn.jsonl`` (auto-created).
* Key: ``(consumer_pid, error_kind)``. Different PIDs do NOT share
  suppression (so two parallel hook invocations each emit once); a
  single PID hitting different ``error_kind`` values emits once per
  kind.
* Window: 5 minutes per key.
* Bypass: ``VCO_HOOK_DEBUG=1`` env var → emit every occurrence.
* Atomic append via ``fcntl.flock`` on a sidecar lockfile so concurrent
  writers don't interleave bytes.
* Opportunistic rotation: when the JSONL file exceeds 1 MB after an
  append, truncate it to the most-recent 100 entries.

The bash / PowerShell siblings reimplement the same protocol (same
JSONL row shape, same key derivation, same 5-min window) so the three
resolver clients share suppression: if the bash hook fires first and
writes a row, a Python hook in the same PID *cannot* (different PID) —
but a re-fire of the same bash hook in the same PID would be
suppressed by either side reading the JSONL.

Public API
~~~~~~~~~~

.. code-block:: python

    from vco_lib.resolver_warn import emit_warning_if_allowed

    try:
        cfg = resolve(Path.cwd())
    except HubUnreachable as exc:
        emit_warning_if_allowed("hub_unreachable", str(exc))
        cfg = _env_fallback()  # caller's existing fallback path
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from vco_lib.atomic import exclusive_file_lock
from vco_lib.paths import vct_root_dir


# ─── Constants ──────────────────────────────────────────────────────────

#: Suppression window. A warning with the same ``(pid, error_kind)`` key
#: emitted within this many seconds of a previous emission is dropped.
RATE_LIMIT_WINDOW_SECONDS: int = 300

#: When the JSONL exceeds this size, rotation kicks in on the next
#: ``record_emit`` call: the file is rewritten with only the most-recent
#: :data:`ROTATION_KEEP_LINES` entries.
ROTATION_THRESHOLD_BYTES: int = 1_048_576  # 1 MiB

#: Number of most-recent entries to keep after rotation. 100 entries is
#: enough to keep meaningful suppression history for active PIDs while
#: bounding disk use to ~50-100 KB per rotation.
ROTATION_KEEP_LINES: int = 100

#: Cap on the ``detail`` field bytes — keeps a single row well under the
#: 4 KB filesystem-block size and prevents pathological loggers from
#: blowing up the JSONL with multi-megabyte stack traces.
DETAIL_MAX_BYTES: int = 200

#: Where the JSONL lives (under ``cache/``, NOT in the launcher state
#: root — it's pure ephemera; deleting it only loses suppression state).
_JSONL_RELATIVE: tuple[str, str] = ("cache", "resolver_warn.jsonl")
_LOCKFILE_RELATIVE: tuple[str, str] = ("cache", "resolver_warn.jsonl.lock")


# ─── Path helpers ───────────────────────────────────────────────────────


def _jsonl_path() -> Path:
    """Return the resolver-warning JSONL path (does not create it)."""
    root = vct_root_dir()
    return root / _JSONL_RELATIVE[0] / _JSONL_RELATIVE[1]


def _lockfile_path() -> Path:
    """Return the sidecar-lockfile path (does not create it)."""
    root = vct_root_dir()
    return root / _LOCKFILE_RELATIVE[0] / _LOCKFILE_RELATIVE[1]


def _ensure_cache_dir() -> Path:
    """Create ``<vct_root_dir>/cache/`` if missing; return the JSONL path."""
    jsonl = _jsonl_path()
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    return jsonl


# ─── File locking (best-effort, cross-platform) ─────────────────────────
#
# v0.2.83 (WP-B1): the ``fcntl.flock`` idiom that used to live here as the
# ``_acquire_lock`` / ``_release_lock`` pair now lives ONCE in
# ``vco_lib.atomic.exclusive_file_lock`` (one-concern-one-home). This module
# keeps its own lockfile PATH (``resolver_warn.jsonl.lock`` under ``cache/``);
# only the acquire/release MECHANISM is delegated (imported at module top).
# Behaviour is identical: real ``LOCK_EX`` on POSIX, best-effort no-lock on
# Windows.


# ─── Process-kind derivation ────────────────────────────────────────────


def _consumer_name() -> str:
    """Derive the consumer (process-kind) name for the current process.

    Uses ``Path(sys.argv[0]).name`` so we get e.g. ``"query_code_graph.py"``
    rather than the full path. Empty argv[0] (some embedded interpreters)
    falls back to ``"python"``.
    """
    try:
        argv0 = sys.argv[0] if sys.argv else ""
    except Exception:
        argv0 = ""
    if not argv0:
        return "python"
    name = Path(argv0).name
    return name or "python"


def _make_key(error_kind: str) -> str:
    """Compose the rate-limit key for the current process."""
    return f"{os.getpid()}:{error_kind}"


# ─── JSONL read / write ─────────────────────────────────────────────────


def _last_ts_for_key(jsonl: Path, key: str) -> int | None:
    """Return the timestamp of the most-recent emission for ``key``.

    Returns ``None`` if no matching row is found or the file doesn't
    exist. Malformed JSONL lines are skipped silently — this is a
    best-effort suppression cache, not a structured log.
    """
    try:
        with open(jsonl, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return None
    except OSError:
        return None

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict) and row.get("key") == key:
            ts = row.get("ts")
            if isinstance(ts, (int, float)):
                return int(ts)
    return None


def _maybe_rotate(jsonl: Path) -> None:
    """Truncate JSONL to the most-recent :data:`ROTATION_KEEP_LINES` rows.

    Triggered when the file exceeds :data:`ROTATION_THRESHOLD_BYTES`.
    Best-effort: an error during rotation is swallowed (the JSONL keeps
    growing, but the next append still works).
    """
    try:
        size = jsonl.stat().st_size
    except OSError:
        return
    if size <= ROTATION_THRESHOLD_BYTES:
        return

    try:
        with open(jsonl, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return

    keep = lines[-ROTATION_KEEP_LINES:] if len(lines) > ROTATION_KEEP_LINES else lines
    tmp = jsonl.with_suffix(jsonl.suffix + ".rot.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, jsonl)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _append_row(jsonl: Path, row: dict) -> None:
    """Append one JSONL row to the file."""
    line = json.dumps(row, separators=(",", ":")) + "\n"
    with open(jsonl, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()


# ─── Public API ─────────────────────────────────────────────────────────


def should_emit(error_kind: str) -> bool:
    """Return ``True`` iff the current process should emit for ``error_kind``.

    Returns ``True`` (always emit) when ``VCO_HOOK_DEBUG=1`` is set. Otherwise
    reads the JSONL and returns ``False`` if a matching emission exists
    within the last :data:`RATE_LIMIT_WINDOW_SECONDS` seconds.

    This function does NOT take the lockfile — it's a fast pre-check.
    The real lock-protected check happens inside :func:`record_emit` so
    that two callers racing through ``should_emit → record_emit`` only
    one ends up writing a row.
    """
    if os.environ.get("VCO_HOOK_DEBUG") == "1":
        return True

    jsonl = _jsonl_path()
    if not jsonl.exists():
        return True

    key = _make_key(error_kind)
    last_ts = _last_ts_for_key(jsonl, key)
    if last_ts is None:
        return True

    now = int(time.time())
    return (now - last_ts) >= RATE_LIMIT_WINDOW_SECONDS


def record_emit(error_kind: str, detail: str = "") -> None:
    """Record an emission for ``error_kind`` to the JSONL cache.

    Writes a single row holding ``{ts, pid, consumer, consumer_pid, error_kind,
    key, detail, user}``. Always takes the exclusive lock to serialize
    writes from concurrent hooks. After the write, opportunistically
    rotates if the file exceeded :data:`ROTATION_THRESHOLD_BYTES`.

    On any I/O error this function returns silently — the warning has
    *already* been emitted to stderr by the caller, so failing to
    persist suppression state must not cascade into a script-level
    failure (the user would lose more than they'd gain).
    """
    try:
        jsonl = _ensure_cache_dir()
    except OSError:
        return

    pid = os.getpid()
    consumer = _consumer_name()
    key = f"{pid}:{error_kind}"
    detail_bytes = detail.encode("utf-8", errors="replace")[:DETAIL_MAX_BYTES]
    detail_clipped = detail_bytes.decode("utf-8", errors="replace")
    row = {
        "ts": int(time.time()),
        "pid": pid,
        "consumer": consumer,
        "consumer_pid": pid,
        "error_kind": error_kind,
        "key": key,
        "detail": detail_clipped,
        "user": os.environ.get("USER")
        or os.environ.get("USERNAME")
        or "unknown",
    }

    lockfile = _lockfile_path()
    try:
        with exclusive_file_lock(lockfile):
            try:
                _append_row(jsonl, row)
                _maybe_rotate(jsonl)
            except OSError:
                pass
    except OSError:
        # Opening the lockfile itself failed (unwritable cache dir) — the
        # warning is already on stderr; dropping suppression state is fine.
        pass


def emit_warning_if_allowed(error_kind: str, detail: str = "") -> bool:
    """Emit a rate-limited fall-through warning. Returns ``True`` if emitted.

    Combines :func:`should_emit`, the stderr write, and :func:`record_emit`
    into one call. This is the function consumer code should call from
    its env-fallback path::

        try:
            cfg = resolve(...)
        except HubUnreachable as exc:
            emit_warning_if_allowed("hub_unreachable", str(exc))
            cfg = _env_fallback()

    The warning line is fixed-shape (matches the bash / ps1 clients) so
    downstream log scrapers can pattern-match across all three.
    """
    if not should_emit(error_kind):
        return False

    detail_clipped = (detail or "").replace("\n", " ").strip()
    if len(detail_clipped.encode("utf-8", errors="replace")) > DETAIL_MAX_BYTES:
        # Same byte cap as the JSONL row for stderr presentation.
        encoded = detail_clipped.encode("utf-8", errors="replace")[:DETAIL_MAX_BYTES]
        detail_clipped = encoded.decode("utf-8", errors="replace")

    sys.stderr.write(
        f"[vct] project_config: {error_kind}: {detail_clipped}. "
        f"Falling back to env. "
        f"(rate-limited; set VCO_HOOK_DEBUG=1 to see every occurrence)\n"
    )
    try:
        sys.stderr.flush()
    except Exception:
        pass

    record_emit(error_kind, detail_clipped)
    return True


__all__ = [
    "DETAIL_MAX_BYTES",
    "RATE_LIMIT_WINDOW_SECONDS",
    "ROTATION_KEEP_LINES",
    "ROTATION_THRESHOLD_BYTES",
    "emit_warning_if_allowed",
    "record_emit",
    "should_emit",
]
