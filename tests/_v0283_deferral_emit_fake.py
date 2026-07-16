# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Test helper for the ``vco_lib.deferral_emit`` module (WP-B1, v0.2.83).

HISTORY — why this used to be a fake, and why it no longer is (N-3, v0.2.83):

WP-B2's producer code imports the REAL ``vco_lib.deferral_emit`` per the
PLAN-v0283 D7 contract. During PARALLEL development, WP-B1 authored that module
in a separate worktree, so WP-B2's tests could not assume it existed — they
injected a FAITHFUL FAKE into ``sys.modules`` before importing the producer, so
the function-level ``from vco_lib import deferral_emit`` resolved the fake.

WP-B1 has since LANDED: ``vco_lib/deferral_emit.py`` is a git-tracked, shipped
module present on every supported tree. The coordinator's import-first fix
(below) made ``install_fake_deferral_emit`` a true no-op — it returns the REAL
module whenever importable, which is always. The ~150-line faithful-fake body
(mirrors of ``emit`` / ``emit_entries`` / ``resolve_conditions`` /
``locked_report`` / ``record_auto_resolution``) was therefore DEAD CODE: no test
path could reach it on any supported tree, and no importer references those
names directly (all 8 importers pull only ``install_fake_deferral_emit`` and
``read_auto_resolutions``). N-3 trims it.

What remains:

  * ``install_fake_deferral_emit()`` — returns the real ``vco_lib.deferral_emit``
    (the only reachable path). It NO LONGER fabricates a shadow module; a
    genuinely-absent module now raises ``ImportError`` loudly (a broken install,
    not something a fake should mask — matches the module's own loud-fail
    posture). Kept as the call shape every importer uses.
  * ``read_auto_resolutions(folder)`` — parses the JSONL trail the real
    ``record_auto_resolution`` writes; used by 8 test files to assert the
    B-F9 audit rows.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

_AUTO_RESOLUTIONS_REL = Path(".claude") / "logs" / "auto-resolutions.jsonl"


def install_fake_deferral_emit() -> types.ModuleType:
    """Return the REAL ``vco_lib.deferral_emit`` module (WP-B1 shipped it).

    Idempotent and import-first:

      * If ``vco_lib.deferral_emit`` is already in ``sys.modules``, return it.
      * Otherwise import the real module and return it.

    The historical shadow-fake path is gone (N-3): the real module is git-tracked
    and ships on every supported tree, so it is ALWAYS importable. If it is
    somehow absent, that is a BROKEN install — this raises ``ImportError`` loudly
    (never silently fabricating a fake that would validate mirror behaviour
    instead of the shipped emitter, the exact bug the coordinator's import-first
    fix closed).
    """
    existing = sys.modules.get("vco_lib.deferral_emit")
    if existing is not None:
        return existing
    # Import-first: the real module always wins. No fabricated fallback.
    import vco_lib.deferral_emit as _real  # noqa: PLC0415 — lazy by design

    return _real


def read_auto_resolutions(folder: Path) -> list[dict]:
    """Test helper: parse the JSONL written by ``record_auto_resolution``."""
    target = Path(folder) / _AUTO_RESOLUTIONS_REL
    if not target.exists():
        return []
    rows: list[dict] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows
