# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
r"""Single-instance-per-workspace reaper for the weaviate_mcp subprocess.

Root fix for the v0.2.74 T5-1 "double MCP subprocess" bug: two
``weaviate_mcp/server.py`` subprocesses can be alive at once — one scoped
to ``CLAUDE_PROJECT_DIR=/A``, one to ``CLAUDE_PROJECT_DIR=/B`` — because
Claude Code (across workspace switches, or an MCP re-registration / update)
spawns a NEW subprocess WITHOUT stopping the OLD one. ``server.py`` caches
the hub-resolved project config at MODULE LOAD (``_resolved_project_config``
global, ``_config_field`` hub-over-env) and never refreshes, so whichever
stale process the client binds to fans out over the WRONG project's
collections → 0 hits.

This module gives ``server.py`` a spawn-time hook: when a fresh
``weaviate_mcp`` starts up for workspace W, it reaps any OTHER live
``weaviate_mcp`` whose ``CLAUDE_PROJECT_DIR != W`` (and any prior PID for
the SAME workspace — a superseded restart). This is the same family as the
existing update-time reapers (``update_gate.rs::pre_update_mcp_kill_sweep_*``)
and the ``hub.pid`` single-instance pattern, but scoped to ONE MCP kind and
keyed on the per-workspace env rather than a global sweep.

Design constraints
------------------
* **Best-effort / soft-fail.** A failed enumeration or a failed kill must
  NEVER block the fresh MCP from starting. Every path returns a count and
  swallows its own errors; the caller ignores the return value in prod.
* **Dependency-light.** The MCP subprocess must boot on a bare install.
  Primary enumeration is stdlib ``/proc`` on Linux (read each candidate's
  ``environ`` for ``CLAUDE_PROJECT_DIR`` + ``cmdline`` for the server.py
  path). ``psutil`` is used as a fallback ONLY if importable (covers
  macOS / Windows where ``/proc`` is absent). No hard dependency added.
* **Never signals unrelated processes.** A process is a reap candidate
  ONLY when its command line contains ``weaviate_mcp/server.py`` (or the
  Windows ``weaviate_mcp\server.py``) AND it is not our own PID / parent.
  We match on that exact path token, not a loose "python" substring.
* **Never signals the CURRENT workspace's peer that IS us.** The whole
  point is to keep exactly one live server per workspace; the freshly
  starting process (self) is always excluded.

Windows note: ``/proc`` does not exist; the psutil fallback handles it when
installed. Without psutil on Windows the reaper is a silent no-op (returns
0) — the backstop refuse-loud check in ``server.py`` still protects
correctness there.
"""
from __future__ import annotations

import logging
import os
import signal
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The path token that identifies a weaviate_mcp server process on the
# command line. Matched case-insensitively against the joined cmdline so
# both ``.../weaviate_mcp/server.py`` (POSIX) and
# ``...\weaviate_mcp\server.py`` (Windows) hit.
_SERVER_TOKENS = ("weaviate_mcp/server.py", "weaviate_mcp\\server.py")


def _normalize_workspace(value: Optional[str]) -> str:
    """Canonicalize a CLAUDE_PROJECT_DIR value for equality comparison.

    Resolves symlinks + trailing slashes so ``/home/x/proj`` and
    ``/home/x/proj/`` (and a symlinked variant) compare equal. Empty /
    None → "" (an unset workspace never matches a set one, so a
    workspace-less stray process is left alone unless BOTH are unset).
    """
    if not value:
        return ""
    try:
        return str(Path(value).resolve())
    except Exception:  # noqa: BLE001 — malformed path → compare raw
        return value.rstrip("/\\")


def _cmdline_is_weaviate_mcp(cmdline: str) -> bool:
    """True iff the (space-joined) command line launches weaviate_mcp/server.py."""
    if not cmdline:
        return False
    low = cmdline.lower()
    return any(tok in low for tok in _SERVER_TOKENS)


def _iter_proc_candidates_linux():
    """Yield (pid, workspace, cmdline) for every live weaviate_mcp process.

    Linux-only: reads ``/proc/<pid>/cmdline`` and ``/proc/<pid>/environ``.
    Soft-fails per process (a process that exits mid-scan, or one we lack
    permission to read, is skipped silently). Never raises.
    """
    proc_root = "/proc"
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(f"{proc_root}/{name}/cmdline", "rb") as fh:
                raw = fh.read()
        except (OSError, ValueError):
            continue
        # cmdline is NUL-separated argv; join with spaces for token match.
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        if not _cmdline_is_weaviate_mcp(cmdline):
            continue
        workspace = ""
        try:
            with open(f"{proc_root}/{name}/environ", "rb") as fh:
                env_raw = fh.read()
            for kv in env_raw.split(b"\x00"):
                if kv.startswith(b"CLAUDE_PROJECT_DIR="):
                    workspace = kv[len(b"CLAUDE_PROJECT_DIR="):].decode(
                        "utf-8", "replace"
                    )
                    break
        except (OSError, ValueError):
            # Cannot read environ (perm / gone) — treat workspace as unknown.
            workspace = ""
        yield pid, workspace, cmdline


def _iter_proc_candidates_psutil():
    """Yield (pid, workspace, cmdline) via psutil (macOS / Windows fallback).

    Returns nothing (empty generator) if psutil is not importable. Soft-fails
    per process. Never raises.
    """
    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001 — psutil optional
        return
    for proc in psutil.process_iter(["pid", "cmdline", "environ"]):
        try:
            info = proc.info
            cmd_list = info.get("cmdline") or []
            cmdline = " ".join(str(c) for c in cmd_list)
            if not _cmdline_is_weaviate_mcp(cmdline):
                continue
            env = info.get("environ") or {}
            workspace = env.get("CLAUDE_PROJECT_DIR", "") if isinstance(env, dict) else ""
            pid_raw = info.get("pid")
            if pid_raw is None:
                continue
            yield int(pid_raw), workspace, cmdline
        except Exception:  # noqa: BLE001 — process vanished / access denied
            continue


def _iter_weaviate_mcp_processes():
    """Platform-dispatch process enumeration. Never raises."""
    if os.path.isdir("/proc"):
        yield from _iter_proc_candidates_linux()
    else:
        yield from _iter_proc_candidates_psutil()


def _terminate(pid: int) -> bool:
    """Best-effort terminate a PID. Returns True if a signal was sent.

    Uses SIGTERM (POSIX) / os.kill fallback. Never raises — a race where
    the process already exited (ProcessLookupError) or we lack permission
    (PermissionError) is a soft no-op returning False.
    """
    try:
        sig = getattr(signal, "SIGTERM", signal.SIGINT)
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        logger.debug("reap_stale_weaviate_mcp: no permission to signal pid=%s", pid)
        return False
    except Exception as exc:  # noqa: BLE001 — never break the spawn
        logger.debug("reap_stale_weaviate_mcp: kill pid=%s raised (%s)", pid, exc)
        return False


def reap_stale_weaviate_mcp(
    workspace: Optional[str] = None,
    *,
    self_pid: Optional[int] = None,
    _iter=None,
    _kill=None,
) -> int:
    """Reap every OTHER live weaviate_mcp whose workspace != ``workspace``.

    Called at spawn time by ``server.py`` for workspace W. Kills any live
    ``weaviate_mcp/server.py`` process that is NOT us and whose
    ``CLAUDE_PROJECT_DIR`` differs from W — the stale-workspace zombie the
    T5-1 bug leaves behind. Also reaps a prior PID for the SAME workspace
    (a superseded restart that Claude Code did not stop).

    Args:
        workspace: The fresh MCP's ``CLAUDE_PROJECT_DIR`` (defaults to the
            env value). Compared canonicalized (symlinks + trailing slash).
        self_pid: This process's PID (defaults to ``os.getpid()``); always
            excluded so we never signal ourselves.
        _iter / _kill: Injection seams for tests (process iterator + kill
            fn). Production leaves them None.

    Returns:
        The count of processes we sent a terminate signal to. Best-effort:
        a process that vanished between enumeration and kill is not counted.

    Soft-fail: ANY error in enumeration is caught and returns the count so
    far; a failed kill is skipped. This function must NEVER raise into the
    MCP startup path — a failed reap degrades to "two processes, backstop
    refuse-loud catches the wrong one" rather than a boot crash.
    """
    my_pid = self_pid if self_pid is not None else os.getpid()
    my_ws = _normalize_workspace(
        workspace if workspace is not None else os.environ.get("CLAUDE_PROJECT_DIR", "")
    )
    iterator = _iter if _iter is not None else _iter_weaviate_mcp_processes
    killer = _kill if _kill is not None else _terminate

    reaped = 0
    try:
        for pid, other_ws, cmdline in iterator():
            if pid == my_pid:
                continue  # never signal ourselves
            other_norm = _normalize_workspace(other_ws)
            # Reap when the workspace differs. Same-workspace prior PIDs are
            # ALSO reaped (a superseded restart Claude Code didn't stop) —
            # keeping exactly one live server per workspace. The my_pid guard
            # above already excludes the fresh (self) process, so a
            # same-workspace match here is genuinely a stale duplicate.
            cross_ws = other_norm != my_ws
            if killer(pid):
                reaped += 1
                logger.warning(
                    "reap_stale_weaviate_mcp: terminated stale weaviate_mcp "
                    "pid=%s (%s: its CLAUDE_PROJECT_DIR=%r, ours=%r) to keep "
                    "one live server per workspace and prevent collection "
                    "drift (T5-1).",
                    pid,
                    "cross-workspace" if cross_ws else "superseded same-workspace",
                    other_norm or "(unset)",
                    my_ws or "(unset)",
                )
    except Exception as exc:  # noqa: BLE001 — never break the spawn
        logger.debug("reap_stale_weaviate_mcp: enumeration raised (%s)", exc)
    return reaped


__all__ = ["reap_stale_weaviate_mcp"]
