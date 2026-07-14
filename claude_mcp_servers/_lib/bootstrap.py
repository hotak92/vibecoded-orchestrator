# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Bare-script import bootstrap shared by the orchestrator's MCP servers.

Every MCP server under ``claude_mcp_servers/<name>/server.py`` is launched
by Claude Code / the launcher as a BARE SCRIPT
(``<venv>/python <install>/claude_mcp_servers/<name>/server.py``), NOT as
``python -m claude_mcp_servers.<name>.server``. Run that way ``server.py``
is ``__main__`` with an empty ``__package__``, so the sibling ``_lib``
package (``claude_mcp_servers/_lib/``) is only importable once its PARENT
directory (``claude_mcp_servers/``) is on ``sys.path``.

Two shipped helpers repeated the same "try ``from _lib.X import Y``; on
``ImportError`` put the parent dir on ``sys.path`` and retry once" dance
in every MCP server, and — worse — each ended the dance with a SILENT
soft-fail stub (``register_sighup_exit_handler → False`` /
``exit_if_update_in_progress = None``). ``_lib`` is a SHIPPED component of
every healthy install (see ``claude_mcp_servers/_lib/__init__.py``); a
missing ``_lib`` module therefore means a BROKEN install, and the silent
stub MASKED that — it disabled SIGHUP env-reload and, far more dangerously,
the update-in-progress fork-bomb guard, with no signal to the user.

``import_lib_member`` centralises the dance ONCE and LOUD-FAILS when the
shipped module still can't be imported after the parent-dir retry — the
same discipline the ``10f418d7`` package-identity fix applied to
``weaviate_mcp``'s shipped submodules (``.chunking``, ``.embeddings``, …).

This module deliberately has NO cross-package imports (only stdlib) so it
is itself importable the moment ``claude_mcp_servers/`` reaches
``sys.path``. The caller does the minimal parent-dir insert (unavoidable:
``_lib.bootstrap`` itself can't be imported until ``claude_mcp_servers/``
is on the path), then imports this helper, then uses it for the real
named-submodule import + loud-fail:

    # server.py, top of file
    import sys
    from pathlib import Path
    _mcp_root = str(Path(__file__).resolve().parent.parent)  # …/claude_mcp_servers
    if _mcp_root not in sys.path:
        sys.path.insert(0, _mcp_root)
    from _lib.bootstrap import import_lib_member

    register_sighup_exit_handler = import_lib_member(
        "sighup_handler", "register_sighup_exit_handler"
    )
"""
from __future__ import annotations

import importlib
from typing import Any


def import_lib_member(module_name: str, member: str) -> Any:
    """Import ``member`` from the shipped ``_lib.<module_name>``, or raise.

    ``_lib`` is a SHIPPED part of every healthy orchestrator install, so a
    failure here is a BROKEN install — this function LOUD-FAILS with an
    actionable message rather than returning a silent no-op stub. That is
    the correct behaviour: the two callers guard the SIGHUP env-reload and
    the update-in-progress fork-bomb protection; silently disabling either
    (the pre-fix ``False`` / ``None`` stubs) hid a broken install and, in
    the update-gate case, re-armed the very fork-bomb the gate prevents.

    Args:
        module_name: A module under ``_lib`` (e.g. ``"sighup_handler"``).
        member: The attribute to pull from that module.

    Returns:
        The requested attribute.

    Raises:
        ImportError: If ``_lib.<module_name>`` cannot be imported (broken
            install) or the module lacks ``member``. The message names the
            remediation (``python install.py --update``).
    """
    try:
        mod = importlib.import_module(f"_lib.{module_name}")
    except ImportError as exc:
        raise ImportError(
            f"_lib.{module_name} is not importable — this is a BROKEN "
            f"orchestrator install (the shipped claude_mcp_servers/_lib/ "
            f"package is missing or unreadable). Run `python install.py "
            f"--update` from the orchestrator root to repair it. "
            f"Original error: {exc}"
        ) from exc
    try:
        return getattr(mod, member)
    except AttributeError as exc:
        raise ImportError(
            f"_lib.{module_name} imported but has no attribute "
            f"'{member}' — the shipped module is out of date or corrupt. "
            f"Run `python install.py --update` to repair it."
        ) from exc
