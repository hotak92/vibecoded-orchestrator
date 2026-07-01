# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.71 Track T-WT — subagent-start-isolation-check.sh tests (Layer 0b).

This SubagentStart hook injects a LOUD additionalContext warning when a
subagent that requested `isolation: worktree` reports a cwd that IS the
parent checkout toplevel (the suspected silent-fallback). It NO-OPs when:
  * isolation was not requested,
  * the payload exposes no cwd,
  * the cwd is a genuinely separate worktree,
  * the project is not inside a git repo.

It can never block (SubagentStart is non-blocking) — always exits 0.

POSIX-only; the .ps1 sibling is the Windows path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / "templates" / "hooks" / "subagent-start-isolation-check.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook is POSIX-only; .ps1 sibling covers Windows.",
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "--initial-branch=main")
    _git(root, "commit", "--allow-empty", "-q", "-m", "seed")


def _run(payload: dict, project_dir: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    return subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_hook_exists() -> None:
    assert HOOK_PATH.is_file()


def test_always_exits_zero_on_violation(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run(
        {"isolation": "worktree", "cwd": str(repo), "agent_id": "a1"},
        repo,
    )
    # Non-blocking event: must exit 0 even when it injects a warning.
    assert proc.returncode == 0


def test_injects_warning_when_cwd_is_parent(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run(
        {"isolation": "worktree", "cwd": str(repo), "agent_id": "a1"},
        repo,
    )
    assert proc.stdout.strip(), "expected an additionalContext envelope"
    envelope = json.loads(proc.stdout)
    ctx = envelope["hookSpecificOutput"]["additionalContext"]
    assert "ISOLATION VIOLATION SUSPECTED" in ctx
    assert envelope["hookSpecificOutput"]["hookEventName"] == "SubagentStart"


def test_noop_when_isolation_not_requested(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run({"cwd": str(repo), "agent_id": "a1"}, repo)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_noop_when_no_cwd(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run({"isolation": "worktree", "agent_id": "a1"}, repo)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_noop_when_cwd_is_separate_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    separate = str(tmp_path / "worktrees" / "agent-a1")
    proc = _run(
        {"isolation": "worktree", "cwd": separate, "agent_id": "a1"},
        repo,
    )
    # cwd != parent toplevel → no violation → silent.
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_noop_when_not_a_repo(tmp_path: Path) -> None:
    proc = _run(
        {"isolation": "worktree", "cwd": str(tmp_path), "agent_id": "a1"},
        tmp_path,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_isolation_flag_synonyms(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    # `worktree: true` synonym should also trigger detection.
    proc = _run(
        {"worktree": True, "cwd": str(repo), "agent_id": "a1"},
        repo,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip(), "worktree:true should be treated as isolation"


def test_vct_disable_hooks_noop(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(repo)
    env["VCT_DISABLE_HOOKS"] = "1"
    proc = subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=json.dumps({"isolation": "worktree", "cwd": str(repo)}),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
