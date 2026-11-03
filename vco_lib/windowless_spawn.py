# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Start child processes without ever giving them a visible console window.

ONE home (v0.2.105) for the Windows process-creation flags of every child a
hook can reach, from Python directly and — through the ``detach`` CLI below —
from the PowerShell hooks (``Start-VcoDetachedProcess`` in
``templates/hooks/_lib/resolve-powershell.ps1``).

Where a console window comes from (Win32 ``CreateProcess`` rules):

* A console program started by a process that HAS a console inherits that
  console — and so its window, or its absence of one. Claude Code starts every
  hook with ``windowsHide`` (libuv: ``CREATE_NO_WINDOW`` + ``SW_HIDE``), so a
  hook's interpreter owns a console with NO window, and every plain
  (``creationflags=0``) child it runs synchronously shares that windowless
  console. Those children need no flag.
* A console program started by a process with NO console gets a NEW console,
  and a new console gets a visible window. ``DETACHED_PROCESS`` produces
  exactly that parent: the child has no console at all, so every console
  program IT starts allocates a fresh, visible window. Python's own venv
  ``python.exe`` is such a program on Windows — a small launcher that starts
  the base interpreter as its child — so a ``DETACHED_PROCESS`` venv Python
  flashes a window even before it runs a line. (Win32 also IGNORES
  ``CREATE_NO_WINDOW`` when it is combined with ``DETACHED_PROCESS``, so the
  ``DETACHED_PROCESS | CREATE_NO_WINDOW`` pair VCO used to pass meant
  ``DETACHED_PROCESS`` alone.)
* ``CREATE_NO_WINDOW`` gives the child its own console with no window, which
  its descendants then share: nothing in that process tree can show a
  window. That is the flag every detached child gets here, together with
  ``CREATE_NEW_PROCESS_GROUP`` (a Ctrl+C / Ctrl+Break aimed at the caller's
  group does not reach it) — the same pair the Rust side's
  ``engine_autostart`` uses for the same reason.

A ``STARTUPINFO`` with ``SW_HIDE`` is deliberately NOT added: it only affects
GUI programs, and the one GUI program a hook starts through this module is the
VCO launcher (``vco_lib.launcher_ensure``), whose window must be able to show.

This module is STDLIB-ONLY on purpose: the PowerShell hooks run it as
``python -I <root>/vco_lib/windowless_spawn.py detach --spec-b64 <b64>``
(isolated mode — no user site, no ``PYTHON*`` env, no script directory on
``sys.path``), which works with any interpreter and cannot be shadowed by a
module in the project folder. Keep it importable that way.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

__all__ = [
    "CREATE_NEW_PROCESS_GROUP",
    "CREATE_NO_WINDOW",
    "DETACHED_PROCESS",
    "detached_popen_kwargs",
    "main",
    "no_window_popen_kwargs",
    "spawn_detached",
]

#: Win32 ``CREATE_NO_WINDOW``: a console child gets a console with no window.
CREATE_NO_WINDOW = 0x0800_0000
#: Win32 ``CREATE_NEW_PROCESS_GROUP``: the child leads its own Ctrl+C group.
CREATE_NEW_PROCESS_GROUP = 0x0000_0200
#: Win32 ``DETACHED_PROCESS`` — named only so tests can assert it is NEVER
#: passed: a console-less child makes its own console children flash.
DETACHED_PROCESS = 0x0000_0008

PopenFn = Callable[..., Any]


def _is_windows(os_name: Optional[str]) -> bool:
    return (os.name if os_name is None else os_name) == "nt"


def no_window_popen_kwargs(*, os_name: Optional[str] = None) -> Dict[str, Any]:
    """``subprocess.Popen`` keywords for a console child that must not get a
    visible window even when THIS process has no console (a launcher-hosted
    GUI, a service): ``{"creationflags": CREATE_NO_WINDOW}`` on Windows,
    ``{}`` elsewhere. *os_name* overrides ``os.name`` (tests)."""
    if _is_windows(os_name):
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def detached_popen_kwargs(*, os_name: Optional[str] = None) -> Dict[str, Any]:
    """``subprocess.Popen`` keywords that detach a child from its caller.

    POSIX: a new session (outside the caller's process group, so a hook
    runner's timeout kill of that group does not reach it). Windows:
    ``CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`` — its own windowless
    console (so neither it nor anything it starts can show a window) and its
    own Ctrl+C group. Never ``DETACHED_PROCESS`` (module docstring)."""
    if _is_windows(os_name):
        return {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def spawn_detached(
    argv: Sequence[str],
    *,
    cwd: Optional[str] = None,
    stdin_path: Optional[str] = None,
    stdout_path: Optional[str] = None,
    stderr_path: Optional[str] = None,
    stderr_append: bool = False,
    env: Optional[Mapping[str, str]] = None,
    popen: Optional[PopenFn] = None,
    os_name: Optional[str] = None,
) -> Any:
    """Start *argv* detached (:func:`detached_popen_kwargs`) and return the
    ``Popen`` object.

    Streams: *stdin_path* is read from, *stdout_path* / *stderr_path* are
    truncated (``stderr_append`` appends instead — the shared
    detached-children log); a stream with no path goes to ``DEVNULL``. The
    parent closes its copies of the file handles right after the spawn, and
    ``close_fds=True`` keeps every OTHER handle of this process — a hook's
    stdout pipe above all — out of the child, so a child that outlives the
    hook never holds the hook runner's pipes open. Raises ``OSError`` /
    ``ValueError`` when the child cannot be started (the CLI reports it)."""
    if not argv or not str(argv[0]):
        raise ValueError("no program to start")
    opened: List[Any] = []

    def _open(path: Optional[str], mode: str) -> Any:
        if not path:
            return subprocess.DEVNULL
        handle = open(path, mode)  # noqa: SIM115 — closed in the finally below
        opened.append(handle)
        return handle

    try:
        stdin = _open(stdin_path, "rb")
        stdout = _open(stdout_path, "wb")
        if stderr_path and stdout_path and stderr_path == stdout_path:
            stderr: Any = subprocess.STDOUT
        else:
            stderr = _open(stderr_path, "ab" if stderr_append else "wb")
        return (popen or subprocess.Popen)(  # noqa: S603 — argv is the caller's own
            [str(a) for a in argv],
            cwd=cwd or None,
            env=dict(env) if env is not None else None,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            **detached_popen_kwargs(os_name=os_name),
        )
    finally:
        for handle in opened:
            try:
                handle.close()
            except OSError:
                pass


def _decode_spec(spec_b64: str) -> Dict[str, Any]:
    spec = json.loads(base64.b64decode(spec_b64.encode("ascii"), validate=True).decode("utf-8"))
    if not isinstance(spec, dict):
        raise ValueError("the spawn spec is not a JSON object")
    argv = spec.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("the spawn spec carries no argv list of strings")
    return spec


def main(argv: Optional[Sequence[str]] = None, *, popen: Optional[PopenFn] = None,
         out: Any = None, err: Any = None) -> int:
    """CLI: ``detach --spec-b64 <base64 JSON>``.

    The spec (UTF-8 JSON, base64 so it survives any command line unquoted):
    ``argv`` (list, required), ``cwd``, ``stdin`` / ``stdout`` / ``stderr``
    (file paths), ``stderr_append`` (bool), ``wait`` (bool). Prints the
    child's pid on stdout as soon as it started; then exits 0 — or, with
    ``wait``, waits for the child and exits with ITS exit code, so a caller
    that holds this process holds a faithful proxy of the child (the
    PowerShell ``-PassThru`` callers wait on it and read ``ExitCode``). On a
    failed start prints ONE ``[vco] detached spawn FAILED for '<prog>':
    <reason>`` line on stderr, prints no pid, and exits 1 (the PowerShell
    caller relays the line). The child inherits this process's environment."""
    out = out or sys.stdout
    err = err or sys.stderr
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3 or args[0] != "detach" or args[1] != "--spec-b64":
        err.write("usage: windowless_spawn.py detach --spec-b64 <base64 JSON spec>\n")
        return 2
    program = "?"
    try:
        spec = _decode_spec(args[2])
        program = spec["argv"][0]
        proc = spawn_detached(
            spec["argv"],
            cwd=spec.get("cwd") or None,
            stdin_path=spec.get("stdin") or None,
            stdout_path=spec.get("stdout") or None,
            stderr_path=spec.get("stderr") or None,
            stderr_append=bool(spec.get("stderr_append")),
            popen=popen,
        )
    except (OSError, ValueError, TypeError) as exc:
        err.write(f"[vco] detached spawn FAILED for '{program}': {exc}\n")
        err.flush()
        return 1
    out.write(f"{proc.pid}\n")
    out.flush()
    if spec.get("wait"):
        return int(proc.wait())
    return 0


if __name__ == "__main__":
    sys.exit(main())
