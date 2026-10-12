# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-R item 12 — the no-commits snapshot never copies credential files.

worktree-guard snapshots a repository with no commits through a throwaway
index (``git add -A``). In a new project with no ``.gitignore`` that copied
``.env``-style files into the snapshot commit (git's object store) and into
the agent's worktree. The snapshot now excludes the credential-file globs from
the ONE list VCO keeps (``bash_security.py`` ``CREDENTIAL_FILE_EXTENSIONS``,
also read by the secrets guard's ``read_env_files`` rule), says so in the
notice, and REFUSES to snapshot when that list cannot be read (broken
install). .sh and .ps1.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_v02102_worktree_guard_no_head import (
    _LEAK_ENV_KEYS,
    _PS,
    FLAVOURS,
    HOOKS,
    _git,
    _guard,
    _notice,
    _run,
)
from tests.common.child_env import child_env
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parents[1]
SECURITY = REPO_ROOT / "templates" / "scripts" / "bash_security.py"

CRED = [".env", "config/app.env.local", "deploy/prod.credentials", ".netrc", "db/.pgpass",
        "svc/.env-prod"]
NOT_CRED = ["app.py", "environment.py", "docs/envelope.md", "config/settings.environment"]


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in _LEAK_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def _fresh_project(root: Path) -> Path:
    """A brand-new project: git init, NO .gitignore, no commits."""
    root.mkdir(parents=True)
    _git(root, "init", "-q", "--initial-branch=main")
    for rel in CRED + NOT_CRED:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{rel}\n", encoding="utf-8")
    return root


def test_globs_match_exactly_what_the_credential_rule_matches() -> None:
    proc = subprocess.run([sys.executable, str(SECURITY), "--credential-file-globs"],
                          capture_output=True, text=True, check=True, env=child_env())
    globs = proc.stdout.split()
    assert "**/*.env" in globs and "**/*.pgpass[!A-Za-z0-9_]*" in globs
    sys.path.insert(0, str(SECURITY.parent))
    try:
        import bash_security as bs
    finally:
        sys.path.remove(str(SECURITY.parent))
    rule = re.compile(r"\.(" + "|".join(bs.CREDENTIAL_FILE_EXTENSIONS) + r")\b")
    for rel in CRED:
        assert rule.search(rel), rel
    for rel in NOT_CRED:
        assert not rule.search(rel), rel
    # The secrets guard still blocks a credential-file read (one list, both readers).
    assert bs.check_command("cat .env.local")[0] is False
    assert bs.check_command("Get-Content prod.credentials", shell="powershell")[0] is False


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_snapshot_excludes_credential_files(flavour: str, tmp_path: Path) -> None:
    repo = _fresh_project(tmp_path / "proj")
    proc = _guard(flavour, "agent-c0ffee", repo)
    assert proc.returncode == 0, proc.stderr
    wt = Path(proc.stdout.strip().splitlines()[-1])
    snap = _git(wt, "rev-parse", "HEAD").stdout.strip()
    tree = set(_git(repo, "ls-tree", "-r", "--name-only", snap).stdout.split())
    for rel in CRED:
        assert rel not in tree, f"{rel} copied into the snapshot commit"
        assert not (wt / rel).exists(), f"{rel} copied into the agent's worktree"
    for rel in NOT_CRED:
        assert rel in tree and (wt / rel).is_file(), rel
    # The user's own files are untouched.
    assert all((repo / rel).is_file() for rel in CRED)
    text = _notice(repo, "agent-c0ffee").read_text(encoding="utf-8-sig")
    assert "EXCLUDED from the snapshot" in text
    for rel in CRED:
        assert rel in text, f"the notice must name {rel}"


def _broken_hooks_copy(tmp_path: Path) -> Path:
    """The hooks tree WITHOUT the sibling scripts/ dir (bash_security.py)."""
    dst = tmp_path / "broken" / "hooks"
    shutil.copytree(HOOKS, dst)
    return dst


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_unreadable_credential_list_refuses_the_snapshot(flavour: str, tmp_path: Path) -> None:
    repo = _fresh_project(tmp_path / "proj")
    hooks = _broken_hooks_copy(tmp_path)
    env = {k: v for k, v in os.environ.items() if k not in _LEAK_ENV_KEYS}
    env["CLAUDE_PROJECT_DIR"] = str(repo)
    env["HOME"] = str(tmp_path / "fakehome")
    script = hooks / f"worktree-guard.{flavour}"
    argv = ["bash", str(script)] if flavour == "sh" else [_PS, *PWSH_BASE_FLAGS, "-File", str(script)]
    proc = subprocess.run(argv, input=json.dumps({"hook_event_name": "WorktreeCreate",
                                                  "name": "agent-bad1", "cwd": str(repo)}),
                          capture_output=True, text=True, env=env, cwd=str(repo), timeout=90)
    assert proc.returncode != 0
    assert "broken install" in proc.stderr and "credential-file list" in proc.stderr
    assert _git(repo, "worktree", "list", "--porcelain").stdout.count("worktree ") == 1
    # No snapshot commit was written either.
    objs = _git(repo, "count-objects", "-v").stdout
    assert "count: 0" in objs and "in-pack: 0" in objs


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_repo_with_commits_is_unaffected(flavour: str, tmp_path: Path) -> None:
    # HEAD exists: no snapshot, no credential list needed, no exclusion text.
    repo = _fresh_project(tmp_path / "proj")
    _git(repo, "add", "app.py")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "seed")
    proc = _run("worktree-guard", flavour,
                {"hook_event_name": "WorktreeCreate", "name": "agent-d00d", "cwd": str(repo)}, repo)
    assert proc.returncode == 0, proc.stderr
    assert not _notice(repo, "agent-d00d").exists()
