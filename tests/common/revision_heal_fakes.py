# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Test seam for the resync driver's revision heal (v0.2.107).

Driver tests that fake ``subprocess.run`` and ``count_stale_rows`` (with no
owed-path listing) are about what happens AROUND the analyzer run — terminal
reports, the walk claim, convergence, the exposure heal. :func:`heal_always_runs`
gives them a heal plan that runs the scoped analyzer over one file, with the
pre-walk counts taken from the module's (faked) ``count_stale_rows`` exactly as
before the heal existed. The heal's own decisions are tested in
``tests/test_v02107_revision_heal.py`` without this seam.
"""
from __future__ import annotations

from pathlib import Path


def heal_always_runs(monkeypatch, cr) -> None:
    """Patch ``cr._plan_revision_heal`` to a plan that runs the analyzer."""
    from vco_lib import codegraph_revision_heal as rh

    def _plan(project_name, repo_root, analyzer_path, index_dot_claude):
        try:
            counts = cr.count_stale_rows(
                project_name, analyzer_path=analyzer_path, repo_root=repo_root,
                index_dot_claude=index_dot_claude,
            )
        except Exception:  # noqa: BLE001 — the old pre-walk probe's soft-fail
            counts = None
        return rh.HealPlan(rh.HEAL, counts=counts, files=[str(Path(repo_root) / "owed.py")])

    monkeypatch.setattr(cr, "_plan_revision_heal", _plan)
