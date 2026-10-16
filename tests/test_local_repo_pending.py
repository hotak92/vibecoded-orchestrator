# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 (plan item 17, review R11 B3): a command-line bundle run reports
that a repo-less project still waits for the launcher's local git repo.

Both arms of every decision are pinned: pending -> one ledger entry; a repo
or a recorded choice -> the entry clears; a question that cannot be asked
(git cannot run, unreadable launcher.db) -> the ledger is left alone."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.common.launcher_db_fixture import make_launcher_db
from vco_lib import local_repo_pending as lrp
from vco_lib.deferral_report import DeferralReport

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _ledger_has(folder: Path) -> bool:
    md = folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
    if not md.exists():
        return False
    return DeferralReport.read(folder).has_condition(lrp.CID)


def _ledger_count(folder: Path) -> int:
    md = folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
    if not md.exists():
        return 0
    return sum(1 for e in DeferralReport.read(folder).entries if e.condition_id == lrp.CID)


def _db(tmp: Path, folder: Path, mode: object = None, *, register: bool = True) -> Path:
    """A real-schema launcher.db; the project row registers ``other`` when
    ``register`` is False, so the folder itself is unregistered."""
    target = folder if register else tmp / "other"
    settings = (
        [("p1", "orchestrator-core", "worktree_repo_mode", json.dumps(mode))]
        if mode is not None
        else []
    )
    return make_launcher_db(
        tmp,
        projects=[{"project_id": "p1", "name": "Proj", "folder_path": target}],
        module_settings=settings,
    )


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


def test_repo_less_without_launcher_is_pending_and_reported_once(tmp_path, project):
    no_db = tmp_path / "absent.db"
    assert lrp.pending_state(project, db_path=no_db) is lrp.PendingState.PENDING
    assert lrp.reconcile(project, db_path=no_db) == "pending"
    assert lrp.reconcile(project, db_path=no_db) == "pending"
    assert _ledger_count(project) == 1, "one entry, never a second copy"
    text = (project / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text(encoding="utf-8")
    assert "launcher" in text and "init --initial-branch=main" in text


def test_entry_clears_once_a_repo_exists(tmp_path, project):
    no_db = tmp_path / "absent.db"
    lrp.reconcile(project, db_path=no_db)
    assert _ledger_has(project)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    assert lrp.pending_state(project, db_path=no_db) is lrp.PendingState.NOT_PENDING
    assert lrp.reconcile(project, db_path=no_db) == "not_pending"
    assert not _ledger_has(project)


def test_folder_inside_an_enclosing_repo_is_not_pending(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert lrp.pending_state(sub, db_path=tmp_path / "absent.db") is lrp.PendingState.NOT_PENDING


@pytest.mark.parametrize("mode", ["no_repo", "use_existing_at", "local_init"])
def test_a_recorded_choice_is_respected(tmp_path, project, mode):
    db = _db(tmp_path, project, mode)
    assert lrp.pending_state(project, db_path=db) is lrp.PendingState.NOT_PENDING
    lrp.reconcile(project, db_path=db)
    assert not _ledger_has(project)


@pytest.mark.parametrize("mode", [None, "bogus", 7])
def test_no_valid_recorded_choice_is_pending(tmp_path, project, mode):
    db = _db(tmp_path, project, mode)
    assert lrp.pending_state(project, db_path=db) is lrp.PendingState.PENDING


def test_unregistered_folder_is_pending(tmp_path, project):
    db = _db(tmp_path, project, "no_repo", register=False)
    assert lrp.pending_state(project, db_path=db) is lrp.PendingState.PENDING


def test_unreadable_db_leaves_the_ledger_alone(tmp_path, project):
    bad = tmp_path / "launcher.db"
    bad.write_bytes(b"not a sqlite database at all" * 10)
    lrp.reconcile(project, db_path=tmp_path / "absent.db")
    assert _ledger_has(project)
    assert lrp.pending_state(project, db_path=bad) is lrp.PendingState.UNKNOWN
    assert lrp.reconcile(project, db_path=bad) == "unknown"
    assert _ledger_has(project), "an unanswerable question must not drop the entry"


def test_git_that_cannot_run_leaves_the_ledger_alone(tmp_path, project, monkeypatch):
    monkeypatch.setattr(lrp, "run_git", lambda *a, **k: (None, "", "git could not run"))
    assert lrp.reconcile(project, db_path=tmp_path / "absent.db") == "unknown"
    assert not (project / ".claude" / "context" / "UPDATE_DEFERRED.md").exists()


def test_mode_contract_matches_the_shared_parity_fixture():
    """The Rust writer (worktree_repo_mode.rs) pins the same fixture in
    `worktree_repo_mode_parity_fixture`, so a new Rust mode fails there until
    the fixture lists it, and fails HERE until Python knows it."""
    from vco_lib.launcher_db_reader import ORCHESTRATOR_CORE_MODULE_ID

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "worktree_repo_mode_parity.json").read_text(
            encoding="utf-8"
        )
    )
    assert fixture["_format_version"] == 1
    assert fixture["module_id"] == ORCHESTRATOR_CORE_MODULE_ID
    assert fixture["setting_key"] == lrp._SETTING_KEY
    assert set(fixture["valid_modes"]) == set(lrp._VALID_MODES)
    assert len(fixture["valid_modes"]) == len(set(fixture["valid_modes"]))


def test_registry_declares_the_condition():
    import tomllib

    reg = tomllib.loads(
        (Path(lrp.__file__).with_name("deferral_conditions.toml")).read_text(encoding="utf-8")
    )
    entry = reg["conditions"][lrp.CID]
    assert entry["class"] == "action_required"
    assert entry["clear_probe"] == "bundle-reconciled"


def test_bundle_install_reconciles_it(tmp_path, monkeypatch):
    """The ONE call site: every non-dry bundle run (launcher or CLI)."""
    from vco_lib import project_init

    calls: list[Path] = []
    monkeypatch.setattr(
        lrp, "reconcile", lambda folder, **k: calls.append(Path(folder)) or "pending"
    )
    orch = tmp_path / "orch"
    proj = tmp_path / "proj2"
    proj.mkdir()
    (orch / "templates").mkdir(parents=True)
    result = project_init.install_project_bundle(proj, orchestrator_root=orch, dry_run=True)
    assert calls == [] and "local_git_repo_pending" not in result, "dry runs never touch it"
    result = project_init.install_project_bundle(proj, orchestrator_root=orch)
    assert calls == [proj]
    assert result["local_git_repo_pending"] == "pending"
