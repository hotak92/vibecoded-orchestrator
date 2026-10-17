"""v0.2.104 E-1: the end-of-turn "[Code edit reminder]" lists only the MAIN
agent's edits inside the project.

The reminder accumulator (``.claude/state/edit_reminder_<sid>.txt``) is
appended by the routing home (``_lib/route-touched-path.{sh,ps1}``) that
``post-file-edit`` and ``post-bash-file-sync`` call, and drained by the Stop
hook ``stop-codegraph-reminder``. A subagent's PostToolUse payload carries the
PARENT's ``session_id`` plus a non-empty ``agent_id``, so before the fix a
lane's edits (in its own worktree, in a temp dir) landed in the parent's
accumulator and the parent's Stop reminder listed files it never touched.

These tests run the REAL hooks end-to-end (stdin payload in, state files out)
so a caller that stops passing the agent id goes red, not just the library.
The code-graph drain queue is asserted UNCHANGED for every arm: a lane's edit
must still reach the code graph.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"
POST_EDIT_SH = HOOKS / "post-file-edit.sh"
POST_EDIT_PS1 = HOOKS / "post-file-edit.ps1"
POST_BASH_SH = HOOKS / "post-bash-file-sync.sh"
POST_BASH_PS1 = HOOKS / "post-bash-file-sync.ps1"

_BASH = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("python3")), reason="bash required"
)
_PWSH = pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh required")

SID = "sessMain1"


def _project(tmp_path: Path) -> tuple[Path, Path]:
    proj = Path(os.path.realpath(tmp_path)) / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    (proj / "src").mkdir()
    (proj / "src" / "a.py").write_text("x = 1\n")
    scratch = Path(os.path.realpath(tmp_path)) / "scratch"
    scratch.mkdir()
    (scratch / "b.py").write_text("y = 2\n")
    return proj, scratch


def _env(tmp_path: Path, proj: Path) -> dict:
    env = {
        **child_env(),
        "CLAUDE_PROJECT_DIR": str(proj),
        "VCT_STATE_DIR": str(tmp_path / "vct-state"),
        "VCT_VENV": "",
        "VCT_INSTALL_ROOT": "",
        "VCO_VENV_PYTHON": "",
        "WEAVIATE_URL": "http://127.0.0.1:9",
    }
    env.pop("VCT_DISABLE_HOOKS", None)
    return env


def _edit_payload(path: Path, agent_id: str = "") -> str:
    d = {"session_id": SID, "tool_name": "Edit", "tool_input": {"file_path": str(path)}}
    if agent_id:
        d["agent_id"] = agent_id
        d["agent_type"] = "general-purpose"
    return json.dumps(d)


def _run(argv: list[str], payload: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=payload, capture_output=True, text=True,
                          timeout=120, env=env)


def _sh(hook: Path) -> list[str]:
    return ["bash", str(hook)]


def _ps1(hook: Path) -> list[str]:
    return ["pwsh", *PWSH_BASE_FLAGS, "-File", str(hook)]


def _reminder(proj: Path) -> str:
    f = proj / ".claude" / "state" / f"edit_reminder_{SID}.txt"
    return f.read_text() if f.exists() else ""


def _drain(proj: Path) -> str:
    f = proj / ".claude" / "state" / f"codegraph_drain_{SID}.txt"
    return f.read_text() if f.exists() else ""


def _check_three_arms(tmp_path: Path, argv: list[str]) -> None:
    proj, scratch = _project(tmp_path)
    env = _env(tmp_path, proj)
    in_proj = proj / "src" / "a.py"
    outside = scratch / "b.py"

    # Subagent edit inside the project tree: NOT listed, still drained.
    r = _run(argv, _edit_payload(in_proj, agent_id="a1b2c3"), env)
    assert r.returncode == 0, r.stderr
    assert str(in_proj) not in _reminder(proj), (
        "a subagent's edit must never land in the main session's reminder"
    )
    assert str(in_proj) in _drain(proj), "a subagent's edit must still reach the code-graph queue"

    # Main-agent edit outside the project (scratch dir): NOT listed.
    r = _run(argv, _edit_payload(outside), env)
    assert r.returncode == 0, r.stderr
    assert str(outside) not in _reminder(proj), (
        "a scratch file outside the project is not this project's work item"
    )

    # Main-agent edit inside the project: listed.
    r = _run(argv, _edit_payload(in_proj), env)
    assert r.returncode == 0, r.stderr
    assert _reminder(proj).splitlines() == [str(in_proj)]


@_BASH
def test_sh_post_file_edit_lists_only_main_agent_in_project_edits(tmp_path: Path) -> None:
    _check_three_arms(tmp_path, _sh(POST_EDIT_SH))


@_PWSH
def test_ps1_post_file_edit_lists_only_main_agent_in_project_edits(tmp_path: Path) -> None:
    _check_three_arms(tmp_path, _ps1(POST_EDIT_PS1))


def _bash_payload(cmd: str, agent_id: str = "") -> str:
    d = {"session_id": SID, "tool_name": "Bash", "tool_input": {"command": cmd},
         "tool_response": {"stdout": "", "stderr": "", "interrupted": False}}
    if agent_id:
        d["agent_id"] = agent_id
    return json.dumps(d)


def _check_bash_write(tmp_path: Path, argv: list[str]) -> None:
    proj, _ = _project(tmp_path)
    env = _env(tmp_path, proj)
    target = proj / "src" / "a.py"
    cmd = f"printf 'z = 3\\n' > '{target}'"
    r = _run(argv, _bash_payload(cmd, agent_id="lane9"), env)
    assert r.returncode == 0, r.stderr
    assert str(target) in _drain(proj), "precondition: the CLI write was routed"
    assert str(target) not in _reminder(proj)
    r = _run(argv, _bash_payload(cmd), env)
    assert r.returncode == 0, r.stderr
    assert str(target) in _reminder(proj)


@_BASH
def test_sh_post_bash_file_sync_skips_subagent_writes_in_reminder(tmp_path: Path) -> None:
    _check_bash_write(tmp_path, _sh(POST_BASH_SH))


@_PWSH
def test_ps1_post_bash_file_sync_skips_subagent_writes_in_reminder(tmp_path: Path) -> None:
    _check_bash_write(tmp_path, _ps1(POST_BASH_PS1))
