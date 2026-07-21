# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Optional-companion install primitives for install.py (v0.2.75).

D-11 extraction: the lean-ctx discovery-copy helper that install.py used to
host inline. Kept out of the install.py monolith (soft line-ratchet, CLAUDE.md
"extract before you add") — the logic is a pure function of its inputs plus
the OS, with all filesystem effects at the edges.

This module does NOT import install.py — install.py imports FROM it, keeping
the dependency edge one-directional (install.py -> vco_lib.install_companions).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import List, Optional, Tuple


#: Reason codes returned by :func:`codegraph_ts_install_plan` when it decides
#: NOT to install (so install.py can log/print a matching notice).
CODEGRAPH_TS_SKIP_ENV = "skip_env"        # VCT_SKIP_CODEGRAPH_TS=1
CODEGRAPH_TS_SKIP_NO_PYPROJECT = "skip_no_pyproject"  # extra pins live there


def codegraph_ts_install_plan(
    *,
    pyproject_exists: bool,
    skip_env: bool,
    project_root: str,
) -> Tuple[bool, Optional[str], Optional[List[str]]]:
    """Decide whether install.py should install the optional ``codegraph-ts``
    extra (tree-sitter call-extraction grammars), and with what pip argv.

    Pure decision function (no I/O, no subprocess) — the testable core of
    install.py's ``_install_codegraph_treesitter`` soft-fail step. install.py
    owns the actual subprocess run + its logging/animation helpers (irreducible
    glue coupled to ``_run_logged_subprocess`` / ``_log_install_event``), so
    only the DECISION lives here.

    Args:
        pyproject_exists: whether ``<project_root>/pyproject.toml`` is present
            (the extra's exact pins live there; nothing to install without it).
        skip_env: whether ``VCT_SKIP_CODEGRAPH_TS=1`` is set (explicit opt-out).
        project_root: the orchestrator root; the pip target is
            ``<project_root>[codegraph-ts]`` so pyproject's pins are the single
            source of truth (no duplicated version list).

    Returns ``(should_install, skip_reason, pip_target_argv)``:
        * ``(False, <reason>, None)`` when skipping — ``skip_reason`` is one of
          the ``CODEGRAPH_TS_SKIP_*`` codes.
        * ``(True, None, ["<project_root>[codegraph-ts]"])`` when installing —
          the tail argv element install.py appends to its pip invocation.
    """
    if skip_env:
        return (False, CODEGRAPH_TS_SKIP_ENV, None)
    if not pyproject_exists:
        return (False, CODEGRAPH_TS_SKIP_NO_PYPROJECT, None)
    # ``<root>[codegraph-ts]`` — pip resolves the extra from pyproject's pins.
    return (True, None, [f"{project_root}[codegraph-ts]"])


def ensure_discovered_lean_ctx_on_path(
    found_path: str,
    *,
    home: Path | None = None,
    os_name: str | None = None,
) -> str | None:
    """D-11 (v0.2.75): copy a DISCOVERED lean-ctx binary into ~/.local/bin
    (Windows: %USERPROFILE%\\.cargo\\bin) so a hook shell with a minimal PATH
    resolves it — extending the vendored-copy path to any found binary.

    ``home`` / ``os_name`` are injectable for tests; they default to
    ``Path.home()`` / ``platform.system()`` at call time.

    Returns the destination path when a copy actually happened, None when:
      * the binary is ALREADY on PATH (``shutil.which`` found it) — nothing to do;
      * the binary is already AT the canonical dest — idempotent no-op;
      * the source doesn't exist, or the copy failed (soft-fail — never blocks
        install).

    We do NOT vendor new platform prebuilts here (binary provenance is a
    maintainer decision); we only relocate a binary the user already has.
    """
    import platform

    if home is None:
        home = Path.home()
    if os_name is None:
        os_name = platform.system()
    try:
        src = Path(found_path)
        if not src.is_file():
            return None
        if os_name == "Windows":
            dest_dir = home / ".cargo" / "bin"
            dest = dest_dir / "lean-ctx.exe"
        else:
            dest_dir = home / ".local" / "bin"
            dest = dest_dir / "lean-ctx"
        # Already resolvable on PATH -> the hook's `command -v` finds it; skip.
        if shutil.which("lean-ctx"):
            return None
        # Already at the destination -> idempotent no-op.
        try:
            if dest.exists() and dest.resolve() == src.resolve():
                return None
        except OSError:
            pass
        dest_dir.mkdir(parents=True, exist_ok=True)
        # F-11: overwrite the same ~/.local/bin/lean-ctx dest that
        # install.py's migrated site writes — route through the shared
        # atomic primitive (one-concern-one-home) so a mid-copy crash
        # can't leave a truncated binary on PATH. Reads the (small) binary
        # fully into memory, which atomic_copy_file explicitly supports.
        from vco_lib.atomic import atomic_copy_file  # noqa: PLC0415

        atomic_copy_file(src, dest)
        if os_name != "Windows":
            os.chmod(dest, 0o755)
        return str(dest)
    except (OSError, shutil.Error) as e:
        print(f"  lean-ctx: failed to copy discovered binary to PATH dir: {e}")
        return None
