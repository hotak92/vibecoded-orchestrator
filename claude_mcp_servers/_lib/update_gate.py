# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Update-in-progress gate — MCP-side shim.

V52-AI (v0.2.52, 2026-06-09): mirror of :mod:`vco_lib.update_gate` for use
by MCP servers (claude_mcp_servers/*/server.py). The MCP servers are
launched by Claude Code via the venv at ``claude_mcp_servers/.venv`` —
the orchestrator's main ``vco_lib`` package is typically importable from
there, but we don't want to hard-depend on that path because MCPs ship
into release tarballs without the full repo.

This shim re-implements the bare minimum (``is_update_in_progress`` +
``exit_if_update_in_progress``) inline so it has no cross-package
imports. The canonical implementation in :mod:`vco_lib.update_gate`
remains the source of truth — both sides MUST agree on the schema and
exit code.

Usage in each MCP's ``server.py`` (very first lines of the ``__main__``
block, BEFORE any heavy imports / connections):

    from _lib.update_gate import exit_if_update_in_progress
    exit_if_update_in_progress("weaviate-kg MCP")

Why this matters: see the user-reported reproduction in the v0.2.52
backlog (V52-AI) — without this gate, the Windows update loop spawns
~100 Python processes consuming 100% CPU until killed manually. The
gate breaks the loop by exiting EACH respawn cleanly (exit code 75)
before any side effects accumulate.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Must match vco_lib.update_gate.EXIT_UPDATE_IN_PROGRESS.
EXIT_UPDATE_IN_PROGRESS = 75

# Must match vco_lib.update_gate.LOCKFILE_BASENAME.
LOCKFILE_BASENAME = ".update-in-progress.json"


def _vct_root_dir() -> Path:
    """Mirror of :func:`vco_lib.paths.vct_root_dir`.

    Resolution order:
      1. ``VCT_STATE_DIR`` env var.
      2. ``~/.vct/``.
    """
    custom = os.environ.get("VCT_STATE_DIR", "").strip()
    if custom:
        return Path(custom)
    return Path.home() / ".vct"


def _lockfile_path() -> Path:
    return _vct_root_dir() / LOCKFILE_BASENAME


def _parse_iso(s: str):
    """Parse an ISO-8601 timestamp. Returns ``None`` on any failure."""
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def is_update_in_progress() -> bool:
    """Is an orchestrator update currently in progress?

    Reads the lockfile at ``<vct_root_dir>/.update-in-progress.json``
    and returns ``True`` iff the lockfile exists AND its
    ``expected_completion_by`` is in the future.

    Soft-fail: any I/O or parse error returns ``False`` so the MCP
    proceeds normally. The fork-bomb risk only exists during the
    actual update window; a corrupt lockfile shouldn't block legitimate
    spawns.
    """
    p = _lockfile_path()
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("update_gate: failed to read %s: %s", p, e)
        return False
    deadline = _parse_iso(data.get("expected_completion_by", ""))
    if deadline is None:
        return False
    return _dt.datetime.now(_dt.timezone.utc) < deadline


def exit_if_update_in_progress(component_name: str = "MCP") -> None:
    """If an update is in progress, exit cleanly with code 75.

    Designed to be called at the very top of MCP server ``__main__``
    blocks. Logs a clear message to stderr so the user / Claude Code
    logs explain why the process declined to start.

    No-op when no lockfile is active — the typical case.
    """
    if not is_update_in_progress():
        return
    msg = (
        f"[update_gate] {component_name}: declining to start — an "
        f"orchestrator update is in progress. Exiting with code "
        f"{EXIT_UPDATE_IN_PROGRESS}; the launcher will restart MCPs "
        f"once the update finishes."
    )
    try:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 — stderr write must never crash
        pass
    sys.exit(EXIT_UPDATE_IN_PROGRESS)
