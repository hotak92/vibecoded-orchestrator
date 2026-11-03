# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Where the Claude Code CLI (``claude``) is, for a process that may not have
the user's shell PATH (v0.2.105).

A process the launcher starts detached — the model gateway — inherits the
launcher's environment, which on Linux/macOS often lacks ``~/.local/bin``
(where Claude Code's native installer puts ``claude``). So after the PATH
lookup this resolver also tries the two install locations Claude Code itself
uses: ``<home>/.local/bin/claude`` (native installer) and
``<claude dir>/local/claude`` (the older ``claude migrate-installer`` local
install). Both are derived through :mod:`vco_lib.paths` — the one home of
the user's home and of ``~/.claude`` — never rebuilt inline.

Interactive-session callers that must find the CLI the way the user invokes
it (``vco_lib.vscode_settings``'s dogfood leg, the KG reviewer hook) keep
their PATH-only ``shutil.which("claude")`` on purpose: a stale copy in an
install-relative location must not stand in for the user's own ``claude``.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional


def install_candidates() -> tuple[Path, ...]:
    """The install locations Claude Code uses, in the order tried."""
    from vco_lib.paths import claude_user_dir, user_home

    exe = "claude.exe" if os.name == "nt" else "claude"
    return (user_home() / ".local" / "bin" / exe,
            claude_user_dir() / "local" / exe)


def resolve_claude_cli(*, override: Optional[str] = None) -> Optional[str]:
    """The ``claude`` CLI's path, or ``None``.

    ``override`` (an explicit path a caller's own setting names) wins and is
    used only when it is a file — a wrong override answers ``None`` rather
    than silently running some other ``claude``. Otherwise PATH (PATHEXT
    honoured on Windows), then :func:`install_candidates`.
    """
    if override is not None and override.strip():
        return override if os.path.isfile(override) else None
    found = shutil.which("claude")
    if found:
        return found
    for candidate in install_candidates():
        if candidate.is_file():
            return str(candidate)
    return None
