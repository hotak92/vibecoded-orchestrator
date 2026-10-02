# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The Python home of the install-root IDENTITY rule (v0.2.100 F-W1-05).

A directory is an orchestrator clone iff it carries the structural markers
(``vct-module.json``, OR ``install.py`` + ``CLAUDE.md``) AND its
``vct-module.json`` parses to an object whose ``id`` is exactly
``"orchestrator"`` (exact match — not the class-name normalisation of
``vco_lib.project_identity``).

MUST MATCH ``launcher/src-tauri/vct-launcher-core/src/services/install_root.rs``
(``is_orchestrator_clone`` / ``looks_like_orchestrator_root`` /
``manifest_id``): the launcher boot path resolves its clone in Rust because no
venv is guaranteed there (a rule-C mirror). Both sides run
``tests/fixtures/install_root_cases.json`` —
``tests/test_v02100_install_root_identity.py`` drives THIS module with it.

Related but deliberately different predicate:
:func:`vco_lib.paths.looks_like_orchestrator_root` answers "is ``.claude/``
first-party SOURCE in this tree" (``vco_lib/`` + ``.claude/``) for code-graph
indexing and the interpreter ladder; it must stay true for development
checkouts and test trees that carry no manifest, so it is a structural
heuristic, not identity. Anything that must know "this IS the orchestrator
install" (a pointer written into other projects, the install root) uses
:func:`is_orchestrator_clone`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

ORCHESTRATOR_MODULE_ID = "orchestrator"  # must match install_root.rs ORCHESTRATOR_MODULE_ID
MANIFEST_NAME = "vct-module.json"


def has_root_markers(d: Path) -> bool:
    """must match install_root.rs::looks_like_orchestrator_root"""
    d = Path(d)
    try:
        return (d / MANIFEST_NAME).is_file() or (
            (d / "install.py").is_file() and (d / "CLAUDE.md").is_file())
    except OSError:
        return False


def manifest_id(d: Path) -> Optional[str]:
    """must match install_root.rs::manifest_id"""
    try:
        data = json.loads((Path(d) / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    mid = data.get("id")
    return mid if isinstance(mid, str) else None


def is_orchestrator_clone(d: Path) -> bool:
    """must match install_root.rs::is_orchestrator_clone. Never raises."""
    return has_root_markers(d) and manifest_id(d) == ORCHESTRATOR_MODULE_ID
