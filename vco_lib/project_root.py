# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Which VCO project is "here"? — the ONE walk-up from a working directory.

Why this module exists (v0.2.102 W6-W)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Several writers of ``<project>/.claude/state/...`` resolved the project as
``CLAUDE_PROJECT_DIR``, else the BARE ``Path.cwd()``. Every hook gets
``CLAUDE_PROJECT_DIR`` from the harness, but a CLI does not: ``kg-search`` run
from the Bash tool or a user shell standing in a SUBFOLDER created
``<subfolder>/.claude/state/rl_pending/nosession__kg_cli_*.json``. The pending
queue's owner sweeps only ``<project_root>/.claude/state/rl_pending``, so those
files were never drained and never TTL-swept. The fix is not "cwd, but
smarter" at each site: it is one resolver every writer shares.

The rules
~~~~~~~~~

* **Marker.** A directory is a VCO project when it has ``.claude/settings.json``
  (the per-project env channel Claude Code reads) or ``.claude/env`` (its shell
  sibling). ``install-bundle`` writes both unconditionally, safe-add included.
  A bare ``.claude/`` directory is NOT a marker: the stray
  ``<sub>/.claude/state`` this very bug created has neither file, and must
  never be mistaken for a root (the walk passes straight through it).
* **Home boundary.** The walk stops at the user's home and never accepts it:
  every Claude Code user has ``~/.claude/settings.json`` (the harness's GLOBAL
  config, not a project marker). "Home" is either :func:`vco_lib.paths.user_home`
  or the OS ``Path.home()`` — see :func:`home_boundaries`.
* **No ``.git`` boundary.** A git root is not a project boundary: a VCO
  project routinely contains nested repositories (vendored clones, submodules,
  a public-repo clone added as an extra code-graph path). Stopping at the
  first ``.git`` would leave a CLI run from inside one of them with no root —
  or worse, resolve nothing and fall back. The marker file is the authority;
  the nearest marked ancestor wins, which also keeps a project nested inside
  another project correct.
* **No root found → ``None``.** Callers that WRITE must skip the write (with a
  log line), never fall back to the cwd: a file under an unmarked directory is
  exactly the undrainable orphan this module exists to prevent.

Pure filesystem stats: no hub call, no heavy import — safe for hooks and for
the MCP's import path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

__all__ = [
    "looks_like_vco_project",
    "home_boundaries",
    "find_vco_project_root",
    "resolve_project_root",
]


def looks_like_vco_project(path: "str | Path") -> bool:
    """True when ``path`` carries the marker files a bundled VCO project has.

    ``.claude/settings.json`` or ``.claude/env``. A bare ``.claude/`` (for
    instance a stray ``.claude/state`` some writer created) does not count.
    """
    claude = Path(path) / ".claude"
    try:
        return (claude / "settings.json").is_file() or (claude / "env").is_file()
    except OSError:
        return False


def home_boundaries() -> "frozenset[Path]":
    """Every directory this process could mean by "the user's home".

    TWO members, deliberately — a SAFETY boundary, not a config read:
    :func:`vco_lib.paths.user_home` (``VCT_USER_HOME_OVERRIDE`` -> home) and
    the OS ``Path.home()``, which keeps holding the real
    ``~/.claude/settings.json`` whatever the override says. A home is out of
    bounds if it is EITHER, so a user-settable override cannot defeat it.

    Never raises: an unresolvable home contributes no boundary.
    """
    from vco_lib.paths import user_home  # noqa: PLC0415 — keep import light

    found: "set[Path]" = set()
    for probe in (user_home, Path.home):
        try:
            found.add(probe().resolve())
        except (OSError, RuntimeError, ValueError):
            continue
    return frozenset(found)


def find_vco_project_root(start: "str | Path | None" = None) -> Optional[Path]:
    """Nearest ancestor of ``start`` (default: the cwd) that is a VCO project.

    ``start`` itself counts. The walk stops at — and never accepts — the
    user's home (see :func:`home_boundaries`); a ``start`` outside home walks
    to the filesystem root. Returns the resolved path, or ``None``.
    """
    try:
        here = Path(start).resolve() if start is not None else Path.cwd().resolve()
    except (OSError, RuntimeError):
        return None
    homes = home_boundaries()
    for ancestor in (here, *here.parents):
        if ancestor in homes:
            return None  # home, and everything above it, is out of bounds
        if looks_like_vco_project(ancestor):
            return ancestor
    return None


def resolve_project_root(start: "str | Path | None" = None) -> Optional[Path]:
    """The project root a ``.claude/state`` writer should use, or ``None``.

    1. ``CLAUDE_PROJECT_DIR`` when set (non-blank) — the harness named the
       workspace; taken as given, exactly as every writer did before.
    2. :func:`find_vco_project_root` from ``start`` (default: the cwd).
    3. ``None`` — the caller skips its write and logs why.
    """
    workspace = (os.environ.get("CLAUDE_PROJECT_DIR") or "").strip()
    if workspace:
        return Path(workspace)
    return find_vco_project_root(start)
