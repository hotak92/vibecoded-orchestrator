# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Deferred citation pending-queue — stage / read / accumulate / delete + TTL.

F-QUEUE (v0.2.70, maintainer ruled NO DEFER): hook-path citations are recovered
by persisting the staged retrieval ctx to disk at retrieval time, then a Stop
hook drains it at turn-end. This module owns the pending-file lifecycle — ONE
home, imported by both the staging side (``rl_kg_search.py`` + the MCP
``_populate_citation_cache``) and the drain side (``rl_drain_citations.py``).

Reuses the proven pre-bash/post-bash pairing-file pattern (a JSON file under
``.claude/state/``), so it is launcher.db-free, hub-free, and survives process
death. NO new hub endpoint, NO new launcher.db table (per the queue design).

Pending file path::

    <project_root>/.claude/state/rl_pending/<session>__<task_id>.json

⚠️ ACCUMULATE-DON'T-DROP (maintainer ruling): the pending file is a true
ACCUMULATOR. At turn-end, if the cumulative answer window is still < the token
threshold the drain LEAVES the file for the next Stop (``delete_pending`` is NOT
called) so it keeps accumulating into subsequent turns. Compute+write+delete
happen only at a genuine threshold or a true terminal (session-end / compaction
force-flush) or TTL expiry.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "pending_dir",
    "stage_pending",
    "read_pending",
    "list_pending_for_session",
    "delete_pending",
    "sweep_expired",
    "PENDING_TTL_SECONDS",
]

# Abandoned-session TTL: a pending file with no turn-end (session killed) is
# garbage after this. Mirrors _RL_MONITOR_TIMEOUT (60 min).
PENDING_TTL_SECONDS: float = 3600.0

# Filesystem-safe key sanitiser. session_id / task_id come from Claude Code +
# our own uuid hex, but a defensive scrub keeps a stray path separator from
# escaping the pending dir.
_SAFE_KEY = re.compile(r"[^A-Za-z0-9._-]")


def _resolve_project_root() -> Path:
    """Resolve the project root the same way the force-flush sentinel does:
    ``CLAUDE_PROJECT_DIR`` env first, then a best-effort cwd fallback."""
    base = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if base:
        return Path(base)
    return Path.cwd()


def pending_dir(project_root: "str | Path | None" = None) -> Path:
    """Return ``<root>/.claude/state/rl_pending`` (created on demand)."""
    root = Path(project_root) if project_root else _resolve_project_root()
    d = root / ".claude" / "state" / "rl_pending"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _safe(key: str) -> str:
    return _SAFE_KEY.sub("-", key or "")


def _pending_path(session_id: str, task_id: str, root: "str | Path | None") -> Path:
    fname = f"{_safe(session_id) or 'nosession'}__{_safe(task_id)}.json"
    return pending_dir(root) / fname


def stage_pending(
    *,
    session_id: str,
    task_id: str,
    seq: Optional[int],
    query: str,
    ctx: dict,
    source: str = "hook",
    project_root: "str | Path | None" = None,
) -> Optional[Path]:
    """Persist the staged citation ctx as a pending file. Returns the path
    (or None on soft-fail). Idempotent per (session_id, task_id): a re-stage
    overwrites. The ctx is the same dict shape as ``_rl_node_content_cache``.

    Adds the queue-routing fields the drain needs: ``session_id``, ``task_id``,
    ``seq`` (1-based KG-call counter, or None for the hook path which has no
    process-global seq), ``query`` snippet (for transcript matching),
    ``ts_ms`` (TTL), and ``source`` ("hook" | "mcp").
    """
    try:
        payload = {
            "session_id": session_id or "",
            "task_id": task_id,
            "seq": seq,
            "query": (query or "")[:120],
            "ts_ms": int(time.time() * 1000),
            "source": source,
            "ctx": ctx,
        }
        path = _pending_path(session_id, task_id, project_root)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)  # atomic
        return path
    except Exception as exc:  # noqa: BLE001
        logger.debug("stage_pending: write failed for %s (%s)", task_id[:8], exc)
        return None


def read_pending(path: "str | Path") -> Optional[dict]:
    """Read one pending file. Returns the payload dict or None on soft-fail."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def list_pending_for_session(
    session_id: str, project_root: "str | Path | None" = None
) -> list[Path]:
    """All pending files for a session, oldest-first (by mtime).

    Matches the ``<session>__*`` prefix. When ``session_id`` is empty (the hook
    path could not resolve one) the drain still needs to see every orphan, so an
    empty session_id lists ALL pending files (the drain then matches each by its
    own ``session_id`` field + the transcript)."""
    d = pending_dir(project_root)
    if not d.exists():
        return []
    safe = _safe(session_id)
    pattern = f"{safe}__*.json" if safe else "*.json"
    try:
        files = [p for p in d.glob(pattern) if p.suffix == ".json"]
    except OSError:
        return []
    return sorted(files, key=lambda p: _safe_mtime(p))


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def delete_pending(path: "str | Path") -> None:
    """One-shot delete after a successful (or definitively-terminal) compute.
    Soft-fail (already-gone / permission). Do NOT call this for a sub-threshold
    accumulation — the accumulate-don't-drop ruling keeps the file for the next
    Stop."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("delete_pending: unlink failed (%s)", exc)


def sweep_expired(
    project_root: "str | Path | None" = None,
    ttl_seconds: float = PENDING_TTL_SECONDS,
    *,
    now_ms: Optional[int] = None,
) -> int:
    """Delete pending files whose ``ts_ms`` is older than ``ttl_seconds``.
    Returns the count deleted. Abandoned-session GC — cheap mtime+payload
    sweep. ``now_ms`` is injectable for tests."""
    d = pending_dir(project_root)
    if not d.exists():
        return 0
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    cutoff = now - int(ttl_seconds * 1000)
    deleted = 0
    try:
        candidates = list(d.glob("*.json"))
    except OSError:
        return 0
    for p in candidates:
        payload = read_pending(p)
        ts = None
        if isinstance(payload, dict):
            ts = payload.get("ts_ms")
        # Fall back to mtime when the payload is unreadable / ts missing.
        if not isinstance(ts, (int, float)):
            ts = int(_safe_mtime(p) * 1000)
        if ts < cutoff:
            delete_pending(p)
            deleted += 1
    return deleted
