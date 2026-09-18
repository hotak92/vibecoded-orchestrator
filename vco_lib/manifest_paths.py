# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE spelling of the bundle manifest's path (v0.2.95).

``<project>/.claude/.vco-manifest.json`` is the marker that VCO installed into
a folder: ``install-bundle`` writes it unconditionally (safe-add included), the
update engine classifies every shipped file against it, the launcher reads it
to decide whether a folder is a VCO project at all, and the weaviate-kg MCP
now uses its ABSENCE to warn that a write is landing where nothing reads it.

Until this module existed the path was spelled **twelve** times across
``vco_lib/`` and ``install.py`` — three near-identical private constants
(``deferral_dismissal.MANIFEST_REL``, ``project_move._MANIFEST_REL``,
``project_init._MANIFEST_REL``, each carrying a comment asking the reader to
keep it in step with the others) plus nine inline ``folder / ".claude" /
".vco-manifest.json"`` constructions. A rename would have had to find all
twelve; missing one would not fail to import, it would silently classify a
managed project as unmanaged.

So: one constant, imported everywhere. The three module-level names above
remain as re-exports of :data:`MANIFEST_REL` — they have external callers
(``claude_mcp_servers/weaviate_mcp/server.py`` imports the
``deferral_dismissal`` one) and an alias costs nothing, while a *second
definition* is what this module exists to end.

``tests/test_v0295_manifest_path_one_home.py`` is the ratchet: it fails on a
fresh string literal spelling this path anywhere outside this module. A
source-text gate is the right tool here precisely because the thing under test
IS the literal — there is no runtime value to assert against.

Prose is deliberately NOT in scope: a docstring or an error message that names
``.claude/.vco-manifest.json`` for a human to read is documentation, not a path
construction, and forbidding it would push authors into unreadable f-strings
for no safety gain.
"""

from __future__ import annotations

from pathlib import Path

#: The manifest's file name, on its own. The one caller that needs the
#: BASENAME rather than the project-relative path is
#: :mod:`vco_lib.git_exclude`, whose ``.git/info/exclude`` patterns are
#: root-anchored (``/.vco-manifest.json``) rather than ``.claude/``-relative.
MANIFEST_BASENAME = ".vco-manifest.json"

#: The directory the manifest lives in, relative to a project folder.
MANIFEST_DIR_NAME = ".claude"

#: Manifest path relative to a managed project folder. THE home — every other
#: module imports this rather than composing its own.
MANIFEST_REL = Path(MANIFEST_DIR_NAME) / MANIFEST_BASENAME

#: The same path in POSIX form, for the surfaces that key on a string: git
#: exclude patterns, the project-move "not copied" table, user-facing text.
#: ``Path.as_posix()`` rather than a second literal, so the two cannot drift.
MANIFEST_REL_POSIX = MANIFEST_REL.as_posix()


def manifest_path(folder: "Path | str") -> Path:
    """``<folder>/.claude/.vco-manifest.json``.

    Takes ``str`` as well as ``Path`` because several call sites hold a folder
    that arrived as a string from argv or a DB row; converting here is one
    line, and refusing would only move the ``Path(...)`` call outward.

    Pure: never touches the filesystem. Callers decide whether to ``is_file()``
    it — absence is meaningful (an unmanaged folder), not an error.
    """
    return Path(folder) / MANIFEST_REL


__all__ = [
    "MANIFEST_BASENAME",
    "MANIFEST_DIR_NAME",
    "MANIFEST_REL",
    "MANIFEST_REL_POSIX",
    "manifest_path",
]
