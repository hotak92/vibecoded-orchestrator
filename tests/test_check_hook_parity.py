"""Tests for .github/scripts/check_hook_parity.py.

Each test sets up a self-contained git repo in a tmp dir, drops the
parity script in, and runs it as a subprocess so we exercise the CLI
exactly as CI does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_REL = ".github/scripts/check_hook_parity.py"
SCRIPT_SRC = REPO_ROOT / SCRIPT_REL


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None):
    """Run a command, raising on git failures (not on script exit codes)."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, env=full_env
    )


def git(args: list[str], cwd: Path):
    result = run(["git", *args], cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {result.stderr}"
        )
    return result.stdout


def init_repo(tmp_path: Path) -> Path:
    """Create a fresh git repo with an initial commit on `main`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(["init", "-q", "-b", "main"], cwd=repo)
    git(["config", "user.email", "test@example.com"], cwd=repo)
    git(["config", "user.name", "Test"], cwd=repo)
    # Drop the script in.
    script_dst = repo / SCRIPT_REL
    script_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT_SRC, script_dst)
    # Commit a placeholder so HEAD~1 exists later.
    (repo / "README.md").write_text("# test\n")
    git(["add", "."], cwd=repo)
    git(["commit", "-q", "-m", "init"], cwd=repo)
    return repo


def write_hook(repo: Path, rel: str, content: str = "#!/bin/bash\necho hi\n"):
    """Write a hook file to the given repo-relative path."""
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def make_branch_with_changes(repo: Path, branch: str = "feature"):
    """Create a feature branch from main and switch to it."""
    git(["checkout", "-q", "-b", branch], cwd=repo)


def commit_all(repo: Path, msg: str = "change"):
    git(["add", "-A"], cwd=repo)
    git(["commit", "-q", "-m", msg], cwd=repo)


def run_script(repo: Path, base_ref: str | None = "main"):
    """Run the parity script. Mimics CI by setting GITHUB_BASE_REF and
    aliasing 'origin/<base>' -> the local branch (since we have no remote)."""
    env: dict[str, str] = {}
    if base_ref:
        # Create a local ref `refs/remotes/origin/<base>` pointing at the
        # base branch tip, so the script's `origin/<base>` resolves.
        try:
            git(
                ["update-ref", f"refs/remotes/origin/{base_ref}", base_ref],
                cwd=repo,
            )
        except RuntimeError:
            pass
        env["GITHUB_BASE_REF"] = base_ref
    # Strip GITHUB_BASE_REF from caller env when base_ref is None so the
    # local-dev fallback (HEAD~1...HEAD) actually triggers.
    return run(
        ["python3", SCRIPT_REL],
        cwd=repo,
        env={**env, **({"GITHUB_BASE_REF": ""} if base_ref is None else {})},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_clean_pr_no_hook_changes_passes(tmp_path: Path):
    repo = init_repo(tmp_path)
    write_hook(repo, "templates/hooks/foo.sh")
    commit_all(repo, "add foo.sh on main")
    make_branch_with_changes(repo)
    # Modify a non-hook file.
    (repo / "README.md").write_text("# changed\n")
    commit_all(repo, "docs")
    result = run_script(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_sh_modified_without_ps1_in_diff_fails_when_ps1_exists(
    tmp_path: Path,
):
    repo = init_repo(tmp_path)
    # Put both .sh and .ps1 on main.
    write_hook(repo, "templates/hooks/foo.sh")
    write_hook(repo, "templates/hooks/foo.ps1", "# pwsh\nWrite-Host hi\n")
    commit_all(repo, "add foo pair on main")
    make_branch_with_changes(repo)
    # Modify only the .sh.
    (repo / "templates/hooks/foo.sh").write_text("#!/bin/bash\necho new\n")
    commit_all(repo, "modify only sh")
    result = run_script(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "modified without its sibling" in result.stdout


def test_magic_comment_exempts_missing_ps1(tmp_path: Path):
    repo = init_repo(tmp_path)
    # An exempt .sh + a real pair so we're past warn-only.
    write_hook(
        repo,
        "templates/hooks/linux-only.sh",
        "#!/bin/bash\n# OS-EXEMPT-PARITY: uses notify-send (Linux only)\necho hi\n",
    )
    write_hook(repo, "templates/hooks/cross.sh")
    write_hook(repo, "templates/hooks/cross.ps1", "# pwsh\n")
    commit_all(repo, "add hooks on main")
    make_branch_with_changes(repo)
    # Touch an unrelated file so we have a non-empty diff.
    (repo / "README.md").write_text("# x\n")
    commit_all(repo, "docs")
    result = run_script(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_sh_and_ps1_modified_together_passes(tmp_path: Path):
    repo = init_repo(tmp_path)
    write_hook(repo, "templates/hooks/foo.sh")
    write_hook(repo, "templates/hooks/foo.ps1", "# pwsh\n")
    commit_all(repo, "init pair")
    make_branch_with_changes(repo)
    (repo / "templates/hooks/foo.sh").write_text("#!/bin/bash\necho new\n")
    (repo / "templates/hooks/foo.ps1").write_text("# pwsh\nWrite-Host new\n")
    commit_all(repo, "modify both")
    result = run_script(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_warn_only_when_no_ps1_exists_yet(tmp_path: Path):
    repo = init_repo(tmp_path)
    # Repo has only .sh files. Adding a new .sh without a .ps1 should not fail.
    write_hook(repo, "templates/hooks/foo.sh")
    commit_all(repo, "add foo on main")
    make_branch_with_changes(repo)
    write_hook(repo, "templates/hooks/bar.sh")
    commit_all(repo, "add bar.sh, no ps1")
    result = run_script(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    # Should still emit a heads-up warning.
    assert "warn-only" in result.stdout.lower() or "starts blocking" in (
        result.stdout + result.stderr
    )


def test_blocking_when_ps1_exists_and_new_sh_lacks_sibling(tmp_path: Path):
    repo = init_repo(tmp_path)
    # Seed a real pair so base has a .ps1.
    write_hook(repo, "templates/hooks/foo.sh")
    write_hook(repo, "templates/hooks/foo.ps1", "# pwsh\n")
    commit_all(repo, "add pair on main")
    make_branch_with_changes(repo)
    write_hook(repo, "templates/hooks/bar.sh")  # no .ps1, no exempt
    commit_all(repo, "add bar.sh only")
    result = run_script(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "missing .ps1 sibling" in result.stdout


def test_lib_subdir_files_excluded(tmp_path: Path):
    repo = init_repo(tmp_path)
    # Sourced helper under _lib/ — should be ignored by the gate.
    write_hook(repo, "templates/hooks/_lib/helpers.sh")
    # Plus a real pair so we're past warn-only.
    write_hook(repo, "templates/hooks/foo.sh")
    write_hook(repo, "templates/hooks/foo.ps1", "# pwsh\n")
    commit_all(repo, "add lib + pair on main")
    make_branch_with_changes(repo)
    # Modify the _lib helper alone — must not trigger modification parity.
    (repo / "templates/hooks/_lib/helpers.sh").write_text(
        "#!/bin/bash\necho new\n"
    )
    commit_all(repo, "modify lib only")
    result = run_script(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_magic_comment_must_be_in_first_5_lines(tmp_path: Path):
    repo = init_repo(tmp_path)
    # Magic comment on line 6 — should NOT be honoured.
    deep_marker = (
        "#!/bin/bash\n"
        "# line 2\n"
        "# line 3\n"
        "# line 4\n"
        "# line 5\n"
        "# OS-EXEMPT-PARITY: too late\n"
        "echo hi\n"
    )
    write_hook(repo, "templates/hooks/foo.sh", deep_marker)
    # Real pair so we're past warn-only.
    write_hook(repo, "templates/hooks/baz.sh")
    write_hook(repo, "templates/hooks/baz.ps1", "# pwsh\n")
    commit_all(repo, "init")
    make_branch_with_changes(repo)
    (repo / "README.md").write_text("# x\n")
    commit_all(repo, "docs")
    result = run_script(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "foo.sh" in result.stdout


def test_magic_comment_requires_non_empty_reason(tmp_path: Path):
    repo = init_repo(tmp_path)
    # Empty reason — must still fail existence parity.
    bad_marker = "#!/bin/bash\n# OS-EXEMPT-PARITY:\necho hi\n"
    write_hook(repo, "templates/hooks/foo.sh", bad_marker)
    write_hook(repo, "templates/hooks/baz.sh")
    write_hook(repo, "templates/hooks/baz.ps1", "# pwsh\n")
    commit_all(repo, "init")
    make_branch_with_changes(repo)
    (repo / "README.md").write_text("# x\n")
    commit_all(repo, "docs")
    result = run_script(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "foo.sh" in result.stdout


def test_orphan_ps1_without_sh_fails(tmp_path: Path):
    repo = init_repo(tmp_path)
    # A .ps1 with no .sh sibling. Always blocking (we're past bootstrap
    # by definition once any .ps1 exists).
    write_hook(repo, "templates/hooks/orphan.ps1", "# pwsh\n")
    commit_all(repo, "add orphan ps1")
    make_branch_with_changes(repo)
    (repo / "README.md").write_text("# x\n")
    commit_all(repo, "docs")
    result = run_script(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "orphan.ps1" in result.stdout
    assert "missing .sh sibling" in result.stdout
