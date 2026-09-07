# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Health of a project-local copy of a VCO-shipped ``.claude/scripts`` wrapper.

WHY THIS MODULE EXISTS
----------------------
Two independent consumers ask the same question — *"is the copy of ``<bin>``
sitting in this project's ``.claude/scripts/`` a usable one, or a pre-VCO /
pre-RT-4 fossil?"*:

* ``vco_lib.project_init`` — at bundle-install time, to decide whether a
  pre-existing file is a stale VCO-shaped artifact (adopt it, with a backup)
  or genuine user work (preserve it); and again on the bundle-update
  re-probe that self-clears ``stale_codegraph_wrapper_pending``.
* the Rust launcher (``commands::codegraph::resolve_bundled_script``) — at
  script-resolution time, to decide whether to trust the project-local copy.

Before v0.2.92 each consumer carried its OWN hardcoded list of which wrappers
to check (Python: a 4-entry ``_RESILIENT_WRAPPER_BASENAMES`` tuple; Rust: a
2-stem ``matches!``). Both listed ``code-graph-analyze`` and ``kg-sync`` and
NOTHING else, so a field project carrying five pre-VCO wrappers
(``kg-sync``, ``kg-search``, ``kg-info``, ``code-graph-query``,
``code-graph-analyze``) had exactly one of them noticed.

THE ENUMERATION RULE — derived, never hand-listed
-------------------------------------------------
A wrapper is marker-bearing **iff the SHIPPED template for it contains the
marker**. The shipped file IS the rule table (option "B" of the repo's
cross-language A>B>C rule): both the Python side and the Rust side read the
same bytes and apply the same two-line test, so a wrapper added to
``templates/scripts/`` in a future release is covered the day it ships,
without either side being edited. It also derives — rather than hardcodes —
the exclusions the old lists spelled out by hand: ``kg-duplicates`` (POSIX)
and ``generate-kg-summary.py`` carry no marker in their shipped form, so they
are not marker-checked and a healthy copy is never flagged.

WHAT THE MARKER PROVES, AND WHAT IT DOES NOT
--------------------------------------------
``VCT_INSTALL_ROOT`` is the launcher-provided install root that every
resilient (RT-4, 2026-06-27 and later) wrapper consults when discovering an
interpreter. A copy WITHOUT it cannot reach this install's venv: it either
hardcodes an absolute path from another machine/checkout (exit 127,
``ModuleNotFoundError``) or, worse, defaults its collection env to a FOREIGN
collection name and writes another project's knowledge graph.

The test is a substring scan, so it proves only that the file still mentions
the ladder — a file whose ladder was replaced by a comment naming it would
pass. That is a deliberate, bounded weakness: the alternative (executing an
unknown wrapper to see what it does) is not available at classification time.
It is stated here rather than credited away, because the direction of the
error is safe: the scan can miss a broken wrapper, it cannot condemn a
healthy one.

MIRROR CONTRACT
---------------
``RESILIENT_WRAPPER_MARKER`` is mirrored in
``launcher/src-tauri/src/commands/codegraph.rs`` (``RESILIENT_WRAPPER_MARKER``)
because the launcher must answer this at resolve time with no Python
subprocess available. The literal is pinned by
``tests/test_v0292_wrapper_health.py::MarkerParityTests``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

__all__ = [
    "RESILIENT_WRAPPER_MARKER",
    "shipped_scripts_dir",
    "bytes_are_resilient",
    "path_is_resilient",
    "shipped_requires_marker",
    "marker_bearing_basenames",
    "stale_project_wrappers",
]

#: The string a healthy (RT-4+) wrapper must still contain. MUST MATCH the
#: `RESILIENT_WRAPPER_MARKER` const in
#: `launcher/src-tauri/src/commands/codegraph.rs`.
RESILIENT_WRAPPER_MARKER = "VCT_INSTALL_ROOT"


def _default_orchestrator_root() -> Path:
    """The orchestrator checkout this module was imported from.

    ``vco_lib/`` lives at the checkout root, so ``<this file>/../..`` IS the
    root — the same anchor ``vco_lib.paths`` uses. Callers that already know
    the root (the bundle installer does) should pass it explicitly; this is
    for the probe paths that do not carry one.
    """
    return Path(__file__).resolve().parent.parent


def shipped_scripts_dir(orchestrator_root: Optional[Path] = None) -> Path:
    """``<orchestrator_root>/templates/scripts`` — the shipped rule table."""
    root = Path(orchestrator_root) if orchestrator_root else _default_orchestrator_root()
    return root / "templates" / "scripts"


def bytes_are_resilient(data: bytes) -> bool:
    """Do these bytes still honour the ``$VCT_INSTALL_ROOT`` ladder?"""
    return RESILIENT_WRAPPER_MARKER.encode("utf-8") in data


def path_is_resilient(path: Path) -> bool:
    """As :func:`bytes_are_resilient`, read from disk.

    Conservative on error: an unreadable file returns ``False`` (treated as
    stale), mirroring the Rust guard's default. Being wrong in that direction
    costs a backup + a refresh; being wrong the other way costs a broken
    build or a cross-project write.
    """
    try:
        return bytes_are_resilient(Path(path).read_bytes())
    except OSError:
        return False


def shipped_requires_marker(
    basename: str, orchestrator_root: Optional[Path] = None
) -> bool:
    """Does the SHIPPED template for ``basename`` carry the marker?

    ``False`` when the template is missing or unreadable — an unknown file is
    never marker-checked, so a project-local script VCO does not ship can
    never be condemned by this rule.
    """
    shipped = shipped_scripts_dir(orchestrator_root) / basename
    try:
        return bytes_are_resilient(shipped.read_bytes())
    except OSError:
        return False


def marker_bearing_basenames(
    orchestrator_root: Optional[Path] = None,
) -> tuple[str, ...]:
    """Every shipped ``templates/scripts`` file whose bytes carry the marker.

    Sorted for determinism. This is THE enumeration — a new resilient wrapper
    joins it by being shipped, not by anyone remembering to add it here.
    """
    scripts_dir = shipped_scripts_dir(orchestrator_root)
    try:
        entries: Iterable[Path] = sorted(scripts_dir.iterdir())
    except OSError:
        return ()
    out: list[str] = []
    for entry in entries:
        if not entry.is_file():
            continue
        try:
            if bytes_are_resilient(entry.read_bytes()):
                out.append(entry.name)
        except OSError:
            continue
    return tuple(out)


def stale_project_wrappers(
    folder: Path, orchestrator_root: Optional[Path] = None
) -> list[str]:
    """Basenames of marker-bearing wrappers that this project has STALE copies of.

    A basename is returned when ``<folder>/.claude/scripts/<basename>`` exists
    and does not carry the marker (or cannot be read). Empty list ⇒ nothing
    stale, which is what the ``stale_codegraph_wrapper_pending`` self-clear
    probe reads.
    """
    scripts_dir = Path(folder) / ".claude" / "scripts"
    stale: list[str] = []
    for basename in marker_bearing_basenames(orchestrator_root):
        installed = scripts_dir / basename
        if not installed.is_file():
            continue
        if not path_is_resilient(installed):
            stale.append(basename)
    return stale
