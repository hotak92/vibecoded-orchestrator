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


# ---------------------------------------------------------------------------
# F8 (v0.2.97 review): the failure entry ends on evidence, not on a dismissal
# ---------------------------------------------------------------------------


def _failing_runner(argv: Sequence[str], env: Mapping[str, str]) -> subprocess.CompletedProcess:
    """The child exited non-zero (a locked DB, a crash) — nothing was written."""
    return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr="database is locked")


def _stale_surfaces(plan: pm.MovePlan, dst: Path) -> None:
    """What the moved folder holds when the post-flip projection did not run:
    the surfaces still carry values derived from the PREVIOUS folder."""
    (dst / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {},
        "env": {"KG_BASE_DIR": plan.src, "KG_COLLECTION": "Proj_KnowledgeGraph"},
    }, indent=2), encoding="utf-8")
    (dst / ".claude" / "env").write_text(
        f'export MY_OWN="kept"\nexport KG_BASE_DIR="{plan.src}"\n', encoding="utf-8")


def _failed_move(plan: pm.MovePlan, dst: Path, db_path: Path, home: Path):
    _stale_surfaces(plan, dst)
    result = pm.execute_post_flip(
        plan, run_kg_sync=False, db_path=db_path, home=home, runner=_failing_runner)
    assert pm.CID_ENV_REPROJECTION_FAILED in result.deferrals
    return result


def _entry(dst: Path):
    from vco_lib.deferral_report import DeferralReport

    report = DeferralReport.read(dst)
    return next(
        (e for e in report.entries if e.condition_id == pm.CID_ENV_REPROJECTION_FAILED),
        None)


def _registry_verdict(dst: Path):
    """What the bundle update / install.py re-probe pass decides."""
    from vco_lib import deferral_probes

    return deferral_probes.evaluate(dst, _entry(dst))


def _run_the_printed_command(dst: Path) -> None:
    """Run the entry's command exactly as printed (interpreter aside)."""
    import os
    import shlex

    entry = _entry(dst)
    assert entry is not None
    argv = shlex.split(entry.command_to_apply.splitlines()[0], comments=True)
    assert argv[:4] == ["python", "-m", "vco_lib.config_projection", "apply"], argv
    proc = _real_runner([sys.executable, *argv[1:]], dict(os.environ))
    assert proc.returncode == 0, proc.stderr


def test_the_condition_declares_a_real_clear_probe():
    from vco_lib import deferral_probes
    from vco_lib.deferral_registry import clear_probe_for

    assert clear_probe_for(pm.CID_ENV_REPROJECTION_FAILED) == \
        "probe:py:env_reprojection_still_owed"
    assert "env_reprojection_still_owed" in deferral_probes.PROBES


def test_a_failure_records_the_project_id_the_probe_needs(moved_project, tmp_path):
    plan, dst, db_path = moved_project
    _failed_move(plan, dst, db_path, tmp_path / "home")
    entry = _entry(dst)
    assert entry is not None and entry.dismiss_fields == {"project_id": PROJECT_ID}
    assert "database is locked" in entry.detected


def test_the_entry_is_kept_while_the_surfaces_are_still_stale(moved_project, tmp_path):
    """KEEP: the projection has not run since the failure."""
    plan, dst, db_path = moved_project
    _failed_move(plan, dst, db_path, tmp_path / "home")
    assert _registry_verdict(dst) is True
    report = pm.verify_move(dst, old_path=plan.src, project_id=PROJECT_ID, db_path=db_path)
    assert pm.CID_ENV_REPROJECTION_FAILED not in report["resolved"]
    assert _entry(dst) is not None


def test_the_entry_is_kept_while_the_reprojection_still_fails(moved_project, tmp_path):
    """KEEP: a second failing re-projection neither clears nor hides it."""
    plan, dst, db_path = moved_project
    _failed_move(plan, dst, db_path, tmp_path / "home")
    with pytest.raises(pm.MoveError):
        pm.run_env_reprojection(PROJECT_ID, dst, runner=_failing_runner, db_path=db_path)
    assert _entry(dst) is not None
    assert _registry_verdict(dst) is True


def test_running_the_printed_command_lets_the_probe_clear_it(moved_project, tmp_path):
    """CLEAR (RED before the fix: `manual-dismiss`, no probe — the entry
    outlived the very command it printed)."""
    from vco_lib import deferral_probes
    from vco_lib.deferral_report import DeferralReport

    plan, dst, db_path = moved_project
    _failed_move(plan, dst, db_path, tmp_path / "home")
    _run_the_printed_command(dst)
    assert _registry_verdict(dst) is False
    assert pm.CID_ENV_REPROJECTION_FAILED in deferral_probes.resolvable_condition_ids(
        dst, DeferralReport.read(dst))
    # The user's own export outside VCO's block is never part of the question.
    assert 'export MY_OWN="kept"' in (dst / ".claude" / "env").read_text(encoding="utf-8")


def test_move_verify_clears_it_once_the_surfaces_match(moved_project, tmp_path):
    plan, dst, db_path = moved_project
    _failed_move(plan, dst, db_path, tmp_path / "home")
    _run_the_printed_command(dst)
    report = pm.verify_move(dst, old_path=plan.src, project_id=PROJECT_ID, db_path=db_path)
    assert pm.CID_ENV_REPROJECTION_FAILED in report["resolved"]
    assert _entry(dst) is None


def test_a_later_successful_reprojection_clears_it_and_leaves_a_trail(moved_project, tmp_path):
    """Paired clear: the next successful `run_env_reprojection` of the folder."""
    plan, dst, db_path = moved_project
    _failed_move(plan, dst, db_path, tmp_path / "home")
    pm.run_env_reprojection(PROJECT_ID, dst, runner=_real_runner, db_path=db_path)
    assert _entry(dst) is None
    trail = (dst / ".claude" / "logs" / "auto-resolutions.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(ln) for ln in trail.splitlines() if ln.strip()]
    assert any(r["condition_id"] == pm.CID_ENV_REPROJECTION_FAILED for r in rows)


def test_a_drifted_canonical_key_keeps_it_after_a_clean_projection(moved_project, tmp_path):
    """KEEP twin of the clear: one canonical value edited away from the
    projection is enough — in either surface."""
    plan, dst, db_path = moved_project
    _failed_move(plan, dst, db_path, tmp_path / "home")
    _run_the_printed_command(dst)
    assert _registry_verdict(dst) is False
    settings = dst / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["env"]["KG_COLLECTION"] = "Someone_Else"
    settings.write_text(json.dumps(data, indent=2), encoding="utf-8")
    assert _registry_verdict(dst) is True
    _run_the_printed_command(dst)
    env_file = dst / ".claude" / "env"
    env_file.write_text(
        env_file.read_text(encoding="utf-8").replace(str(dst), plan.src), encoding="utf-8")
    assert _registry_verdict(dst) is True


def test_the_probe_does_not_guess(moved_project, tmp_path, monkeypatch):
    """UNKNOWN: no project id, an unreadable settings.json, a row that points
    elsewhere, an unreadable database — each keeps the entry (None)."""
    from vco_lib.deferral_report import DeferralEntry

    plan, dst, db_path = moved_project
    _failed_move(plan, dst, db_path, tmp_path / "home")
    _run_the_printed_command(dst)
    assert pm.env_reprojection_still_owed(dst, _entry(dst)) is False

    bare = DeferralEntry(condition_id=pm.CID_ENV_REPROJECTION_FAILED, title="t",
                         detected="d", why_deferred="w", command_to_apply="c",
                         severity="warning")
    assert pm.env_reprojection_still_owed(dst, bare) is None

    elsewhere = tmp_path / "elsewhere"
    (elsewhere / ".claude").mkdir(parents=True)
    assert pm.env_reprojection_still_owed(elsewhere, _entry(dst)) is None

    settings = dst / ".claude" / "settings.json"
    good = settings.read_bytes()
    settings.write_bytes(b"{ not jsonc")
    assert pm.env_reprojection_still_owed(dst, _entry(dst)) is None
    settings.write_bytes(good)

    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(tmp_path / "missing" / "launcher.db"))
    assert pm.env_reprojection_still_owed(dst, _entry(dst)) is None
