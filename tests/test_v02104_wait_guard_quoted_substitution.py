"""v0.2.104 E-4: the wait guard no longer denies a one-off grep whose PATTERN
looks like a substitution.

Finding: the walker's substitution scan (``bash_command_walk.
substitution_bodies``) runs on tokens AFTER ``shlex`` has removed the quotes,
so ``grep -n '$(sleep 5)' f`` -- a literal search pattern in single quotes,
the natural thing to grep for while working on the guard or a shell script --
read exactly like ``echo $(sleep 5)`` and was denied as a sole foreground
wait. Bash runs a substitution only outside single quotes (and ``$'...'``,
and not when escaped); inside double quotes ``$(...)`` and backticks still
run, process substitution does not. The walk now keeps a substitution body
only when the raw text says bash would run it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS
from vco_lib import wait_guard as wg

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SH = REPO_ROOT / "templates" / "hooks" / "wait-guard.sh"
HOOK_PS1 = REPO_ROOT / "templates" / "hooks" / "wait-guard.ps1"

LITERAL = [
    "grep -n '$(sleep 5)' wait_guard.py",
    "grep -n '`sleep 5`' notes.md",
    "grep -rn 'diff <(sleep 5)' tests",
    'grep -n "<(sleep 5)" tests/x.py',
    'grep -n "\\$(sleep 5)" x.sh',
    "grep -n $'$(sleep 5)' x.sh",
    "rg -n 'x=$(sleep 5)' .",
]
LIVE = [
    "echo $(sleep 5)",
    'echo "$(sleep 5)"',
    "echo `sleep 5`",
    "diff <(sleep 5) f",
    "x=$(sleep 5)",
    'grep -n "$(sleep 5)" x',
    "grep -n '$(literal)' x $(sleep 5)",
]


@pytest.mark.parametrize("cmd", LITERAL)
def test_quoted_substitution_text_is_not_a_wait(cmd: str) -> None:
    """RED pre-fix: denied as the sole-wait `sleep` pattern."""
    assert wg.static_pattern(cmd) is None


@pytest.mark.parametrize("cmd", LIVE)
def test_live_substitution_still_denies(cmd: str) -> None:
    assert wg.static_pattern(cmd) == ("sleep", "sleep 5")


def test_live_keys_unreadable_quoting_keeps_everything() -> None:
    from vco_lib.bash_command_walk import live_substitution_keys

    assert live_substitution_keys("echo 'unterminated $(sleep 5)") is None


def _payload(cmd: str) -> str:
    return json.dumps({"session_id": "s1", "tool_name": "Bash",
                       "tool_use_id": "tu1", "tool_input": {"command": cmd}})


def _env(tmp_path: Path) -> dict:
    from tests.common.child_env import child_env

    env = child_env()
    env.update({"VCT_STATE_DIR": str(tmp_path / "state"), "VCT_VENV": sys.executable,
                "PYTHONPATH": str(REPO_ROOT), "HOME": str(tmp_path / "home")})
    return env


@pytest.mark.parametrize("impl", ["sh", pytest.param("ps1", marks=pytest.mark.skipif(
    shutil.which("pwsh") is None, reason="pwsh not installed"))])
def test_hook_allows_the_grep_and_denies_the_wait(impl: str, tmp_path: Path) -> None:
    argv = (["bash", str(HOOK_SH), "pretooluse"] if impl == "sh" else
            ["pwsh", *PWSH_BASE_FLAGS, "-File", str(HOOK_PS1), "pretooluse"])
    allow = subprocess.run(argv, input=_payload(LITERAL[0]), capture_output=True,
                           text=True, env=_env(tmp_path), timeout=120)
    assert allow.returncode == 0, allow.stderr
    assert allow.stdout.strip() == ""
    deny = subprocess.run(argv, input=_payload(LIVE[0]), capture_output=True,
                          text=True, env=_env(tmp_path), timeout=120)
    assert deny.returncode == 0, deny.stderr
    assert json.loads(deny.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
