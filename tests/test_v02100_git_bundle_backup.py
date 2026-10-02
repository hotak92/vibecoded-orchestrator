# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 F-W3-13: ONE "save history to a verified bundle" home.

``vco_lib.git_bundle_backup.create_verified_bundle`` is called by
``vco_lib.hard_cut`` (step 1) and — through ``python -m
vco_lib.git_bundle_backup create --json`` — by the launcher's ResetHard backup
(``update_run.rs``; its Rust tests drive the real CLI against real git repos).
These tests run real ``git`` on throwaway local repositories only.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from vco_lib import git_bundle_backup as gbb
from vco_lib import hard_cut as hc

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "one")
    return r


def test_a_verified_bundle_is_written(repo: Path, tmp_path: Path) -> None:
    res = gbb.create_verified_bundle(repo, tmp_path / "backups", "b.bundle", ["--all"])
    assert res.ok, res.error
    assert res.path == tmp_path / "backups" / "b.bundle" and res.path.is_file()
    _git(repo, "bundle", "verify", str(res.path))


def test_a_failed_create_leaves_nothing_behind(repo: Path, tmp_path: Path) -> None:
    res = gbb.create_verified_bundle(repo, tmp_path / "backups", "b.bundle",
                                     ["refs/heads/does-not-exist"])
    assert not res.ok and "git bundle create" in res.error
    assert not (tmp_path / "backups" / "b.bundle").exists()


def test_a_bundle_that_does_not_verify_is_removed(repo: Path, tmp_path: Path) -> None:
    real = subprocess.run

    def run(argv, **kw):
        if argv[:3] == ["git", "bundle", "verify"]:
            return subprocess.CompletedProcess(argv, 1, "", "error: not a bundle")
        return real(argv, **kw)

    res = gbb.create_verified_bundle(repo, tmp_path / "backups", "b.bundle", ["--all"], run=run)
    assert not res.ok and "not trustworthy" in res.error and "not a bundle" in res.error
    assert not (tmp_path / "backups" / "b.bundle").exists(), "partial bundle must be removed"


def test_an_uncreatable_backup_dir_refuses(repo: Path, tmp_path: Path) -> None:
    blocker = tmp_path / "backups"
    blocker.write_text("a file, not a directory")
    res = gbb.create_verified_bundle(repo, blocker, "b.bundle", ["--all"])
    assert not res.ok and "could not create the backup directory" in res.error


def test_a_name_with_a_separator_is_refused(repo: Path, tmp_path: Path) -> None:
    assert not gbb.create_verified_bundle(repo, tmp_path, "../x.bundle", ["--all"]).ok


def test_the_cli_takes_option_shaped_refs_and_prints_one_json_object(
        repo: Path, tmp_path: Path, capsys) -> None:
    rc = gbb.main(["create", "--repo", str(repo), "--dir", str(tmp_path / "b"),
                   "--name", "x.bundle", "--ref=--all", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is True and Path(out["path"]).is_file()
    rc = gbb.main(["create", "--repo", str(repo), "--dir", str(tmp_path / "b"),
                   "--name", "y.bundle", "--ref=refs/heads/nope", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False and out["message"] == out["error"]


def test_hard_cut_step_1_goes_through_the_shared_helper(tmp_path: Path, monkeypatch) -> None:
    """The second caller: a refusal from the ONE helper aborts the hard cut
    before anything destructive, carrying the helper's reason."""
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    calls: list = []

    def refuse(repo, backups_dir, name, refs, **_kw):
        calls.append((Path(repo), Path(backups_dir), name, list(refs)))
        return gbb.BundleResult(ok=False, error="`git bundle verify` exited 1: boom.")

    monkeypatch.setattr(gbb, "create_verified_bundle", refuse)
    ran: list = []
    res = hc.hard_cut("0.2.99", "0.3.0", clone_root=clone, vct_root=tmp_path / ".vct",
                      project_id=None, stamp="S",
                      runner=lambda argv, **_k: ran.append(argv))  # type: ignore[arg-type]
    assert calls == [(clone, tmp_path / ".vct" / "backups", "pre-hardcut-S.bundle", ["--all"])]
    assert res.aborted_before_reset and not res.bundle_verified and "boom" in res.error
    assert ran == [], "nothing after the backup may run"
