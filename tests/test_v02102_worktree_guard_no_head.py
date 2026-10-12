# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-Q — isolated agents must launch in a repo with NO commits and
in a folder that is NOT a git repo.

VCO registers ``worktree-guard`` as the project's WorktreeCreate hook, and a
WorktreeCreate hook "replaces default git behavior" for every isolated launch
(https://code.claude.com/docs/en/hooks#worktreecreate). Before this fix:

  * a ``git init``-ed repo with no commits (every brand-new project) made the
    hook run ``git worktree add --detach <path> HEAD`` on an unborn HEAD →
    ``fatal: invalid reference: HEAD`` → exit 1 → the ``isolation: worktree``
    agent could not launch at all;
  * a non-git folder made the hook print nothing, and "If the hook fails or
    produces no path, worktree creation fails with an error" (hooks reference,
    WorktreeCreate output) → same.

The fix, pinned here for BOTH the .sh and the .ps1 hook:

  * no commits → the worktree detaches at a SNAPSHOT commit of the working
    tree, built through a throwaway index (user index / refs / HEAD untouched,
    .gitignore honoured), and a one-shot notice carries the snapshot sha plus
    the command that brings the agent's changes back — that command is run
    here end to end;
  * not a git repo → the hook returns the folder itself (isolation OFF) and the
    notice says so;
  * an ordinary repo → unchanged (``HEAD``), and NO notice;
  * subagent-start-isolation-check delivers the notice as SubagentStart
    additionalContext, once, keyed by the payload's ``agent_id``;
  * an upgraded install's 3 s WorktreeCreate timeout is reshaped to the
    template's, a user-tuned one is left alone.

The live-harness evidence (Claude Code 2.1.285, scratch repos) is in the
W6-Q lane report; these tests pin the hook-level behaviour it rests on.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS
from vco_lib import settings_merge

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS = REPO_ROOT / "templates" / "hooks"
_PS = shutil.which("pwsh") or shutil.which("powershell")
_LEAK_ENV_KEYS = ("CLAUDE_PROJECT_DIR", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="bash flavour is POSIX-only; the pwsh arm runs where pwsh exists")

FLAVOURS = [
    pytest.param("sh", id="sh"),
    pytest.param("ps1", id="ps1", marks=pytest.mark.skipif(_PS is None, reason="no pwsh on PATH")),
]


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in _LEAK_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _LEAK_ENV_KEYS}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return subprocess.run(["git", *args], cwd=str(cwd), check=check,
                          capture_output=True, text=True, env=env)


def _run(script: str, flavour: str, payload: dict | str, project: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _LEAK_ENV_KEYS}
    env["CLAUDE_PROJECT_DIR"] = str(project)
    path = HOOKS / f"{script}.{flavour}"
    argv = ["bash", str(path)] if flavour == "sh" else [_PS, *PWSH_BASE_FLAGS, "-File", str(path)]
    return subprocess.run(argv, input=payload if isinstance(payload, str) else json.dumps(payload),
                          capture_output=True, text=True, env=env, cwd=str(project), timeout=90)


def _guard(flavour: str, name: str, project: Path) -> subprocess.CompletedProcess:
    return _run("worktree-guard", flavour,
                {"hook_event_name": "WorktreeCreate", "name": name, "cwd": str(project)}, project)


def _notice(project: Path, name: str) -> Path:
    return project / ".claude" / "state" / "worktree-guard" / f"notice-{name}.txt"


def _log_rows(project: Path) -> list[dict]:
    log = project / ".claude" / "logs" / "worktree-guard.jsonl"
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]


def _no_commit_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q", "--initial-branch=main")
    (root / "app.py").write_text("print('v1')\n", encoding="utf-8")
    (root / "staged.txt").write_text("staged\n", encoding="utf-8")
    (root / ".gitignore").write_text("secret.env\n", encoding="utf-8")
    (root / "secret.env").write_text("TOKEN=x\n", encoding="utf-8")
    _git(root, "add", "staged.txt")
    return root


# ── no commits: snapshot base, isolation kept ───────────────────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_no_commit_repo_gets_a_snapshot_worktree(flavour: str, tmp_path: Path) -> None:
    repo = _no_commit_repo(tmp_path / "proj")
    index_before = _git(repo, "ls-files", "--stage").stdout

    proc = _guard(flavour, "agent-a1b2c3", repo)

    assert proc.returncode == 0, proc.stderr
    wt = Path(proc.stdout.strip().splitlines()[-1])
    assert wt.parent == (repo / ".claude" / "worktrees").resolve()
    # A registered, separate worktree whose HEAD is the snapshot of the tree.
    listed = _git(repo, "worktree", "list", "--porcelain").stdout
    assert f"worktree {wt}" in listed
    assert (wt / "app.py").read_text(encoding="utf-8") == "print('v1')\n"
    assert (wt / "staged.txt").is_file()
    assert not (wt / "secret.env").exists(), ".gitignore must be honoured"
    snap = _git(wt, "rev-parse", "HEAD").stdout.strip()
    assert len(snap) == 40
    # The user's repository is untouched: HEAD still unborn, no refs, same index.
    assert _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0
    assert _git(repo, "for-each-ref").stdout.strip() == ""
    assert _git(repo, "ls-files", "--stage").stdout == index_before
    # Logged, and a notice names the snapshot and the way back.
    assert _log_rows(repo)[-1]["reason"] == "worktree_add_snapshot_no_head"
    text = _notice(repo, "agent-a1b2c3").read_text(encoding="utf-8")
    assert "no commits yet" in text and snap in text and "apply" in text


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_snapshot_notice_command_brings_changes_back(flavour: str, tmp_path: Path) -> None:
    """The printed command is shipped code: run it and check it works."""
    repo = _no_commit_repo(tmp_path / "proj")
    proc = _guard(flavour, "agent-back", repo)
    assert proc.returncode == 0, proc.stderr
    wt = Path(proc.stdout.strip().splitlines()[-1])
    (wt / "app.py").write_text("print('v2')\n", encoding="utf-8")
    (wt / "new_module.py").write_text("X = 1\n", encoding="utf-8")
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('v1')\n", "main checkout isolated"

    text = _notice(repo, "agent-back").read_text(encoding="utf-8")
    cmd = text.split("brings your changes back: ", 1)[1].strip()
    run = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, cwd=str(tmp_path),
                         env={k: v for k, v in os.environ.items() if k not in _LEAK_ENV_KEYS})
    assert run.returncode == 0, run.stderr
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('v2')\n"
    assert (repo / "new_module.py").read_text(encoding="utf-8") == "X = 1\n"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_empty_no_commit_repo_still_launches(flavour: str, tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q")
    proc = _guard(flavour, "agent-empty", repo)
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip().splitlines()[-1]).is_dir()


# ── not a git repo: run in place, isolation OFF, said out loud ──────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_non_git_folder_runs_in_place_with_notice(flavour: str, tmp_path: Path) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    proc = _guard(flavour, "agent-plain", folder)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == str(folder.resolve())
    row = _log_rows(folder)[-1]
    assert (row["decision"], row["reason"]) == ("in_place", "not_a_repo")
    text = _notice(folder, "agent-plain").read_text(encoding="utf-8")
    assert "isolation is OFF" in text and "not inside a git repository" in text


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_non_git_without_identifier_stays_a_noop(flavour: str, tmp_path: Path) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    proc = _run("worktree-guard", flavour, {"hook_event_name": "WorktreeCreate", "cwd": str(folder)}, folder)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert not (folder / ".claude" / "state" / "worktree-guard").exists()


# ── ordinary repo: unchanged, no notice (leave-alone arm) ───────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_repo_with_commits_uses_head_and_writes_no_notice(flavour: str, tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "commit", "--allow-empty", "-q", "-m", "seed")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    proc = _guard(flavour, "agent-normal", repo)
    assert proc.returncode == 0, proc.stderr
    wt = Path(proc.stdout.strip().splitlines()[-1])
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == head
    assert _log_rows(repo)[-1]["reason"] == "worktree_add_detached_head"
    assert not _notice(repo, "agent-normal").exists()


# ── the notice reaches the subagent (SubagentStart) ─────────────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_isolation_check_delivers_the_notice_once(flavour: str, tmp_path: Path) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    assert _guard(flavour, "agent-a2e83ad5", folder).returncode == 0
    # Live SubagentStart payload shape (2.1.285): bare agent_id, no isolation flag.
    payload = {"hook_event_name": "SubagentStart", "cwd": str(folder),
               "agent_id": "a2e83ad5", "agent_type": "expert-coder"}
    first = _run("subagent-start-isolation-check", flavour, payload, folder)
    assert first.returncode == 0, first.stderr
    env = json.loads(first.stdout.strip().splitlines()[-1])
    assert env["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
    assert "isolation is OFF" in env["hookSpecificOutput"]["additionalContext"]
    assert not _notice(folder, "agent-a2e83ad5").exists(), "one-shot: removed after delivery"
    second = _run("subagent-start-isolation-check", flavour, payload, folder)
    assert second.returncode == 0 and second.stdout.strip() == ""


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_isolation_check_silent_without_a_notice(flavour: str, tmp_path: Path) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    payload = {"hook_event_name": "SubagentStart", "cwd": str(folder),
               "agent_id": "deadbeef", "agent_type": "expert-coder"}
    proc = _run("subagent-start-isolation-check", flavour, payload, folder)
    assert proc.returncode == 0 and proc.stdout.strip() == ""


# ── settings: the raised WorktreeCreate timeout reaches upgraded installs ───

_TEMPLATES = {
    "linux": REPO_ROOT / "templates" / "settings.json.linux.template",
    "windows": REPO_ROOT / "templates" / "settings.json.windows.template",
}
_V02100 = REPO_ROOT / "tests" / "fixtures" / "settings_templates_v0.2.100"


def _guard_hooks(doc: dict) -> list[dict]:
    return [h for g in doc["hooks"].get("WorktreeCreate", []) for h in g.get("hooks", [])
            if "worktree-guard" in h.get("command", "")]


@pytest.mark.parametrize("os_name", sorted(_TEMPLATES))
def test_upgraded_install_receives_the_raised_timeout(os_name: str) -> None:
    template = json.loads(_TEMPLATES[os_name].read_text(encoding="utf-8"))
    old = json.loads((_V02100 / f"{os_name}.json").read_text(encoding="utf-8"))
    assert [h["timeout"] for h in _guard_hooks(old)] == [3]
    merged = settings_merge.smart_merge_settings(old, template)
    assert _guard_hooks(merged) == _guard_hooks(template)
    assert [h["timeout"] for h in _guard_hooks(merged)] == [60]


@pytest.mark.parametrize("os_name", sorted(_TEMPLATES))
def test_user_tuned_timeout_is_left_alone(os_name: str) -> None:
    template = json.loads(_TEMPLATES[os_name].read_text(encoding="utf-8"))
    old = copy.deepcopy(json.loads((_V02100 / f"{os_name}.json").read_text(encoding="utf-8")))
    for h in _guard_hooks(old):
        h["timeout"] = 15
    merged = settings_merge.smart_merge_settings(old, template)
    assert [h["timeout"] for h in _guard_hooks(merged)] == [15]
