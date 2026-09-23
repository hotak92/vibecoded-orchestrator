# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97: the move's (and the rename's) env re-projection runs for real.

Both movers hand-built ``python -m vco_lib.config_projection apply
--project-id <id> --folder <dst>``. ``apply`` has never had ``--folder``, so
argparse exited 2 on EVERY move and every rename; the failure landed in a
``post_flip.warnings`` list the GUI does not render. The unit tests of the
day injected a ``runner`` returning ``returncode=0`` — an argv-shape test that
cannot see a live parser rejection.

These tests therefore run the argv the mover BUILDS through a real child
process. The only thing the test runner changes is the environment (the
checkout pinned first on ``PYTHONPATH`` so the child measures this tree) and
the interpreter path (``resolve_or_current`` → ``sys.executable``); the verb
and every flag are the mover's own.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping, Sequence, cast

import pytest

from tests.common.child_env import child_env
from tests.common.launcher_db_fixture import make_launcher_db
from vco_lib import collection_rename as cr
from vco_lib import project_move as pm


PROJECT_ID = "proj-move-reproject"


def _real_runner(argv: Sequence[str], env: Mapping[str, str]) -> subprocess.CompletedProcess:
    """Execute the mover's argv verbatim as a child process."""
    pinned = child_env(dict(env))
    return subprocess.run(
        list(argv), env=pinned, capture_output=True, text=True, check=False, timeout=60,
    )


@pytest.fixture
def moved_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A launcher.db whose row ALREADY points at the destination (post-flip)."""
    src = tmp_path / "old" / "Proj"
    dst = tmp_path / "new" / "Proj"
    src.mkdir(parents=True)
    (dst / ".claude").mkdir(parents=True)
    db_path = make_launcher_db(
        tmp_path / "state" / "launcher.db",
        projects=[{
            "project_id": PROJECT_ID,
            "name": "Proj",
            "folder_path": str(dst),
            "slug": "proj",
            "kg_primary": "Proj_KnowledgeGraph",
            "kg_shared": "VibeCodedOrchestrator_KnowledgeGraph",
        }],
    )
    monkeypatch.setenv("VCT_STATE_DIR", str(db_path.parent))
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db_path))
    import vco_lib.python_exe as python_exe
    monkeypatch.setattr(python_exe, "resolve_or_current", lambda **_: sys.executable)
    plan = pm.MovePlan(
        project_id=PROJECT_ID, project_name="Proj", project_slug="proj",
        src=str(src), dst=str(dst), src_exists=True, dst_exists=True,
        dst_empty=False, dst_has_manifest=False,
    )
    return plan, dst, db_path


def _settings_env(dst: Path) -> dict:
    data = json.loads((dst / ".claude" / "settings.json").read_text(encoding="utf-8"))
    return data.get("env", {})


def test_mover_reprojection_argv_is_accepted_and_writes_the_new_root(moved_project):
    plan, dst, db_path = moved_project
    pm.run_env_reprojection(plan.project_id, dst, runner=_real_runner, db_path=db_path)
    env = _settings_env(dst)
    assert env.get("KG_COLLECTION") == "Proj_KnowledgeGraph"
    assert Path(env["KG_BASE_DIR"]) == dst


def test_post_flip_reprojects_and_emits_no_reprojection_entry(moved_project, tmp_path):
    plan, dst, db_path = moved_project
    result = pm.execute_post_flip(
        plan, run_kg_sync=False, db_path=db_path, home=tmp_path / "home",
        runner=_real_runner,
    )
    assert not [w for w in result.warnings if "re-projection" in w], result.warnings
    assert pm.CID_ENV_REPROJECTION_FAILED not in result.deferrals
    assert Path(_settings_env(dst)["KG_BASE_DIR"]) == dst


def test_post_flip_failure_is_a_visible_ledger_entry(moved_project, tmp_path):
    """Leave-alone twin: a real CLI failure is surfaced, never swallowed."""
    plan, dst, db_path = moved_project
    ghost = pm.MovePlan(
        project_id="no-such-project", project_name="Proj", project_slug="proj",
        src=plan.src, dst=plan.dst, src_exists=True, dst_exists=True,
        dst_empty=False, dst_has_manifest=False,
    )
    result = pm.execute_post_flip(
        ghost, run_kg_sync=False, db_path=db_path, home=tmp_path / "home",
        runner=_real_runner,
    )
    assert any("env re-projection failed" in w for w in result.warnings)
    assert pm.CID_ENV_REPROJECTION_FAILED in result.deferrals
    ledger = (dst / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text(encoding="utf-8")
    assert pm.CID_ENV_REPROJECTION_FAILED in ledger
    # The printed re-run command is the builder's argv, which the real parser accepts.
    assert "vco_lib.config_projection apply --project-id no-such-project" in ledger
    apply_line = next(ln for ln in ledger.splitlines() if "config_projection apply --project-id" in ln)
    assert "--folder" not in apply_line.split("# then:", 1)[0], apply_line


def test_rename_reprojection_uses_the_same_accepted_argv(moved_project):
    plan, dst, _db_path = moved_project
    # Only the two fields the re-projection reads; the rest of a RenamePlan is
    # rename bookkeeping this step never touches.
    rename_plan = cast(cr.RenamePlan, SimpleNamespace(project_id=plan.project_id, folder=str(dst)))
    cr._reproject_env(rename_plan, runner=_real_runner)  # raises RenameError on exit != 0
    assert _settings_env(dst).get("KG_COLLECTION") == "Proj_KnowledgeGraph"
