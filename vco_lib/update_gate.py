# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Update-in-progress lockfile helpers — Python side.

V52-AI (v0.2.52, 2026-06-09): mitigate the MCP fork-bomb that hit users
running `update orchestrator` on Windows. During the update window, the
launcher restart + MCP supervisor restart + Claude Code's reconnection
attempts overlap. On Windows mandatory locks, every MCP-spawn-against-an-
updating-binary fails → Claude Code retries → respawn loop. The user
ended up with ~97 python processes and ~77 node processes consuming 100%
CPU for hours, requiring manual taskkill.

The fix is a lockfile-based gate. A file at
``<vct_root>/.update-in-progress.json`` is written by the launcher's
update flow BEFORE issuing the git pull, and deleted AFTER the install.py
+ binary refresh completes. Any MCP server (or the ensure-containers
hook) that starts up while this lockfile is fresh exits cleanly with
EXIT_UPDATE_IN_PROGRESS. Claude Code may respawn the process, but each
respawn exits immediately — no fork-bomb loop.

This module is consumed by:
  * install.py (writes the lockfile in --update mode)
  * MCP server scripts (claude_mcp_servers/**/server.py — checks at startup)
  * templates/hooks/ensure-containers.{sh,ps1} (skips startup if active)

Mirror in Rust: ``launcher/src-tauri/src/commands/update_gate.rs`` (used
by the launcher's update_orchestrator + MCP supervisor).

Schema (canonical, kept in sync with Rust)::

    {
      "started_at": "2026-06-09T17:30:00Z",
      "started_by_pid": 12345,
      "phase": "git_pull" | "install_py" | "binary_refresh" | "complete",
      "expected_completion_by": "2026-06-09T17:45:00Z"
    }

Soft-fail throughout: any error reading/parsing the lockfile is treated
as "no lockfile" (caller proceeds normally). The lockfile is advisory
guidance, not a security boundary.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Literal, Optional

from vco_lib.paths import vct_root_dir

logger = logging.getLogger(__name__)

# Exit code for MCP servers / hooks that decline to start because an
# orchestrator update is in progress. Distinct from 0 (normal exit) and
# 1 (generic error) so Claude Code's logs distinguish the two states.
EXIT_UPDATE_IN_PROGRESS = 75

# Lockfile basename (under ``<vct_root_dir>/``). Hidden by leading dot so
# it doesn't clutter `ls ~/.vct/`.
LOCKFILE_BASENAME = ".update-in-progress.json"

# Default expected duration of a full update (git pull + install.py +
# binary refresh). The actual update should complete well within this
# window; if it takes longer, the lockfile is considered stale and gets
# self-healed by the boot-time cleanup.
DEFAULT_UPDATE_DURATION_MIN = 15

Phase = Literal["git_pull", "install_py", "binary_refresh", "complete"]


def lockfile_path() -> Path:
    """Return the absolute path to the update-in-progress lockfile.

    Honours ``$VCT_STATE_DIR`` via :func:`vco_lib.paths.vct_root_dir`.
    The parent directory is NOT created here — callers that write the
    lockfile (the launcher's update flow + install.py) ensure the
    directory exists; callers that read it (MCP servers + hooks) treat
    a missing directory as "no lockfile".
    """
    return vct_root_dir() / LOCKFILE_BASENAME


def _iso_now() -> str:
    """Return the current UTC time in ISO-8601 with trailing Z."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> Optional[_dt.datetime]:
    """Parse an ISO-8601 timestamp, returning ``None`` on any failure.

    Accepts both ``...Z`` (legacy form we write) and ``...+00:00`` (what
    ``datetime.isoformat()`` produces on some platforms) for resilience.
    """
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def write_lockfile(
    phase: Phase = "git_pull",
    expected_duration_min: int = DEFAULT_UPDATE_DURATION_MIN,
    *,
    path: Optional[Path] = None,
) -> Path:
    """Create or update the update-in-progress lockfile.

    Args:
        phase: Current phase of the update. Callers should advance this
            as the update progresses (``git_pull`` → ``install_py`` →
            ``binary_refresh`` → ``complete``). The launcher's Rust
            side does this in :file:`update_gate.rs::advance_phase`.
        expected_duration_min: Minutes until the lockfile is considered
            stale if not deleted first. Defaults to 15 (covers a typical
            git pull + venv rebuild + binary refresh on cold cache).
        path: Override the lockfile path (used by tests). Production
            callers should leave this as ``None``.

    Returns:
        The absolute path to the written lockfile.

    Soft-fail: any I/O error is logged and re-raised — the launcher
    treats lockfile write failure as a hard error (continuing without
    the gate would re-expose the fork-bomb).
    """
    p = path or lockfile_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    expected_completion = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(
        minutes=expected_duration_min
    )
    payload = {
        "started_at": _iso_now(),
        "started_by_pid": os.getpid(),
        "phase": phase,
        "expected_completion_by": expected_completion.strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }

    # Atomic write: temp file + rename. Avoids the race where another
    # process reads a half-written JSON file mid-write.
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    logger.info(
        "update_gate: wrote %s (phase=%s, expected_completion=%s)",
        p,
        phase,
        payload["expected_completion_by"],
    )
    return p


def read_lockfile(path: Optional[Path] = None) -> Optional[dict]:
    """Read and parse the lockfile.

    Returns ``None`` if the file does not exist, is unreadable, or
    contains invalid JSON. Callers should treat ``None`` as "no
    lockfile" and proceed with normal behaviour.
    """
    p = path or lockfile_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("update_gate: failed to read %s: %s", p, e)
        return None


def delete_lockfile(path: Optional[Path] = None) -> bool:
    """Delete the lockfile if present.

    Returns:
        ``True`` if a file existed and was removed (or no longer exists
        after the call). ``False`` only if removal failed (e.g.
        permission denied).
    """
    p = path or lockfile_path()
    try:
        if p.exists():
            p.unlink()
            logger.info("update_gate: removed %s", p)
        return True
    except OSError as e:
        logger.warning("update_gate: failed to remove %s: %s", p, e)
        return False


def is_update_in_progress(*, path: Optional[Path] = None) -> bool:
    """Fast-path check: is an orchestrator update currently in progress?

    Returns ``True`` if the lockfile exists AND its
    ``expected_completion_by`` is in the future. A lockfile whose
    deadline has passed is treated as stale (returns ``False``) — the
    caller can choose to self-heal by calling :func:`cleanup_if_stale`.

    Soft-fail: any error parsing the lockfile returns ``False`` so the
    MCP / hook proceeds with its normal behaviour. The fork-bomb risk
    only exists during the active update window; if the lockfile is
    corrupt, we err on the side of letting the user's work continue.
    """
    data = read_lockfile(path=path)
    if not data:
        return False
    deadline = _parse_iso(data.get("expected_completion_by", ""))
    if deadline is None:
        return False
    return _dt.datetime.now(_dt.timezone.utc) < deadline


def cleanup_if_stale(*, path: Optional[Path] = None) -> bool:
    """Boot-time self-healing: delete a stale lockfile.

    A stale lockfile is one whose ``expected_completion_by`` is in the
    past — i.e. the update either crashed mid-way or took longer than
    expected. Either way, the lockfile no longer represents a real
    in-progress update; leaving it would block legitimate MCP spawns
    indefinitely.

    Returns:
        ``True`` if a stale lockfile was found and removed.
        ``False`` if no lockfile exists OR the lockfile is still fresh.
    """
    data = read_lockfile(path=path)
    if not data:
        return False
    deadline = _parse_iso(data.get("expected_completion_by", ""))
    if deadline is None or _dt.datetime.now(_dt.timezone.utc) >= deadline:
        logger.warning(
            "update_gate: detected stale lockfile (deadline=%s) — cleaning up",
            data.get("expected_completion_by"),
        )
        delete_lockfile(path=path)
        return True
    return False


def exit_if_update_in_progress(
    component_name: str = "MCP",
    *,
    path: Optional[Path] = None,
    stream=None,
) -> None:
    """Convenience: exit the process with EXIT_UPDATE_IN_PROGRESS if locked.

    Designed to be called at the top of MCP server ``__main__`` blocks
    (and the ensure-containers hook's Python equivalents). Logs a clear
    message to stderr so the user / Claude Code logs explain why the
    process declined to start.

    No-op when the lockfile is absent or stale — the typical case.

    Args:
        component_name: Human-readable name for the log message
            (``"weaviate-kg MCP"``, ``"search MCP"``, etc.).
        path: Override lockfile path (test hook).
        stream: Override the write stream (test hook). Defaults to stderr.
    """
    if not is_update_in_progress(path=path):
        return
    out = stream if stream is not None else sys.stderr
    msg = (
        f"[update_gate] {component_name}: declining to start — an "
        f"orchestrator update is in progress. This process will exit "
        f"cleanly with code {EXIT_UPDATE_IN_PROGRESS}; the launcher will "
        f"restart MCPs once the update finishes."
    )
    try:
        out.write(msg + "\n")
        out.flush()
    except Exception:  # noqa: BLE001 — stderr write must never crash
        pass
    sys.exit(EXIT_UPDATE_IN_PROGRESS)
