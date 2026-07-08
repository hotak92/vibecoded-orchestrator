# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""SIGHUP-driven clean-exit handler shared by every orchestrator MCP.

Per v0.2.12 PR-42 (Issue B from the mcp-instability audit at
``.claude/context/mcp-instability-vs-public-repo-2026-05-16.md``):
editing ``.claude/settings.json env`` mid-chat needs the running MCP
subprocess to pick up the new env. Claude Code itself never sends any
reload signal — it reads env at MCP-spawn time and the spawned process
keeps the snapshot for its whole lifetime.

We chose the **safe way**: SIGHUP triggers a CLEAN ``sys.exit(0)``, not
an in-process reconnect. Claude Code respawns the MCP on the next
request and the fresh subprocess reads the new env at module-import
time. No half-reloaded state, no per-var allowlist, no need to track
which env reads ran early vs late in the server lifecycle.

**Accepted in-flight trade (F-12).** The clean exit is not free — it is
deliberately preferred over the more complex alternatives:
  * An in-flight tool call (a ``hybrid_search`` mid-fan-out, a
    ``store_knowledge_node`` mid-write) that is executing when SIGHUP
    arrives dies with the process — the client sees a transport error and
    must retry. This is rare (reloads fire only on a settings.json env
    change, not per request) and a retry is cheap, so we accept it rather
    than build a request-quiescence barrier.
  * A ``store_knowledge_node`` interrupted BETWEEN its .md file write and
    its Weaviate upsert leaves the node on disk but not yet in the index;
    it self-heals at the next ``kg-sync`` (or the next successful upsert of
    that node) — no data loss, just a temporary retrieval gap.
If telemetry ever correlates reload audits with a spike in tool errors,
the escape hatch is a *drain flag*: set a "reloading" sentinel, let
in-flight calls finish (bounded wait), reject new ones with a retriable
error, then exit. Not built today because the observed error rate does
not justify the added state.

Usage in each MCP's ``server.py`` (early, just after the logger is
ready and before ``mcp.run_*()``):

    from _lib.sighup_handler import register_sighup_exit_handler
    register_sighup_exit_handler(logger)

Windows note: ``signal.SIGHUP`` is POSIX-only. The helper is a no-op on
platforms without it, so the import never crashes a Windows MCP. The
launcher-side reload path on Windows can fall back to "restart your
Claude Code chat session"; the auto-reload UX described in PR-42 is
Linux/macOS-only.
"""
from __future__ import annotations

import signal
import sys
from logging import Logger


def register_sighup_exit_handler(logger: Logger) -> bool:
    """Install a SIGHUP handler that exits the process cleanly with code 0.

    Args:
        logger: The MCP's logger — used to record that the signal arrived
            so post-incident forensics can tell "Claude Code closed the
            stdio pipe" apart from "launcher asked us to reload env".

    Returns:
        ``True`` when the handler was installed, ``False`` on platforms
        where ``signal.SIGHUP`` is unavailable (Windows native).

    Exit code 0 is deliberate: this is an EXPECTED clean exit, not an
    error. Claude Code's spawn-on-demand path treats exit 0 the same as
    never-spawned and re-launches the MCP on the next tool call.
    """
    if not hasattr(signal, "SIGHUP"):
        # Windows native — no SIGHUP. Caller should fall back to a
        # different reload mechanism (kill+respawn or session restart).
        return False

    def _exit_for_env_reload(_signum: int, _frame: object) -> None:
        logger.info(
            "Received SIGHUP — exiting cleanly for env reload (PR-42). "
            "Claude Code will respawn this MCP with fresh env on the next request."
        )
        # sys.exit raises SystemExit; FastMCP's stdio loop unwinds and
        # the process terminates with exit code 0.
        sys.exit(0)

    signal.signal(signal.SIGHUP, _exit_for_env_reload)
    return True
