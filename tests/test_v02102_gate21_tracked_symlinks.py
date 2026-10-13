# SPDX-License-Identifier: AGPL-3.0-or-later
# Part of VibeCoded Orchestrator.
"""v0.2.102 W6-J review NEW — Gate 21 must see tracked SYMLINK targets.

R1 shipped ``launcher/node_modules`` as a committed symlink (git mode
120000) whose entire payload was an absolute private-tree path. Every
Gate 21 check before this was a text grep over tracked blobs, and a
symlink's payload is a blob no line grep of the tree ever reads — the
gate was structurally blind to the exact leak shape it exists to stop.

THE RULE (scripts/check-pre-tag-privacy.sh, Gate 21): a tracked file may
be a symlink only when its target is a RELATIVE path that stays inside
the repository. An absolute target, or a relative target that lexically
escapes the repo root, fails the gate. In-repo relative targets
(including dangling ones) remain allowed.

HOW THIS TESTS THE REAL THING
-----------------------------
Like tests/test_pre_ship_gate22_freshness.py it does not re-implement
the gate: each case builds a scratch git repo under tmp_path, commits
the fixture shape, and runs the SHIPPED script with cwd inside it — so
the assertions run against the shipped bytes, and editing the script
edits what this test measures.

RED-PROOF: the pre-fix script (no symlink section) exits 0 on the
absolute-target repo below — verified live during the fix by running the
cp-aside pre-fix bytes against the same scratch repos.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE = REPO_ROOT / "scripts" / "check-pre-tag-privacy.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="the gate is a bash script over a git repo",
)

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "gate-test",
    "GIT_AUTHOR_EMAIL": "gate-test@example.invalid",
    "GIT_COMMITTER_NAME": "gate-test",
    "GIT_COMMITTER_EMAIL": "gate-test@example.invalid",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )


def _scratch_repo(tmp: Path, links: dict[str, str]) -> Path:
    """A committed scratch repo: one clean README plus the given symlinks.

    ``links`` maps repo-relative link path -> literal target string (the
    symlink is created with that exact target, never resolved).
    """
    repo = tmp / "scratch"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("clean scratch repo\n", encoding="utf-8")
    for name, target in links.items():
        link = repo / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "scratch")
    return repo


def _run_gate(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(GATE)],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


def test_absolute_target_symlink_fails_the_gate(tmp_path: Path) -> None:
    """The R1 shape: a committed symlink whose payload is an absolute path."""
    repo = _scratch_repo(
        tmp_path,
        {"launcher/node_modules": "/opt/other-checkout/launcher/node_modules"},
    )
    result = _run_gate(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "launcher/node_modules" in result.stdout
    assert "(absolute path)" in result.stdout


def test_escaping_relative_target_symlink_fails_the_gate(tmp_path: Path) -> None:
    """A relative target with enough ``..`` to leave the repo root."""
    repo = _scratch_repo(tmp_path, {"cache": "../../outside-repo/cache"})
    result = _run_gate(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "cache" in result.stdout
    assert "(escapes repo)" in result.stdout


def test_in_repo_relative_symlinks_pass_the_gate(tmp_path: Path) -> None:
    """Benign shapes stay allowed: same-dir, up-but-inside, and dangling."""
    repo = _scratch_repo(
        tmp_path,
        {
            "docs/readme-link.md": "../README.md",
            "docs/sibling.txt": "notes.txt",
            "dangling-link": "does/not/exist",
        },
    )
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "notes.txt").write_text("notes\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "benign links")
    result = _run_gate(repo)
    assert result.returncode == 0, result.stdout + result.stderr
