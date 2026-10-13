# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-W: hooks find their project by the marked-root rule.

Twenty hook pairs resolved the project as ``${CLAUDE_PROJECT_DIR:-$PWD}``
(``.ps1``: ``... else (Get-Location).Path``). The harness always sets the
variable, but a hook started any other way — by hand, from a script, by a
test — wrote ``<cwd>/.claude/...`` wherever it stood. They now share
``_lib/project-root.{sh,ps1}`` (pinned to ``vco_lib/project_root.py`` by
``test_v02102_w6w_project_root_parity.py``); this file EXECUTES the hooks.

* **No project encloses the cwd** → no ``.claude/`` appears there, for every
  hook that can be run safely in a sandbox (both flavours).
* **Run from a subfolder of a marked project** → nothing lands in the
  subfolder; the writer hooks write under the project root.
* **CLAUDE_PROJECT_DIR set** (the harness case) → unchanged: the writers
  write under it, wherever the cwd is.

Four hooks are not executed here because running them starts or kills real
things on the developer's machine (the hub + gateway, cron/RL bootstrap, the
code-graph analyzer, the fork-bomb killer). Their resolution is the same
two-line call into the shared helper; ``test_unexecuted_hooks_use_the_helper``
pins that by EXECUTING each one's extracted resolution block instead.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"
PWSH = shutil.which("pwsh")
BASH = shutil.which("bash")

#: hook -> stdin payload. Hooks safe to run in a sandbox.
SAFE_HOOKS = {
    "agent-skill-keyword-suggest": '{"prompt": "review this code", "session_id": "w6w"}',
    "subagent-start-isolation-check": '{"agent_id": "a1", "agent_type": "x", "session_id": "w6w"}',
    "kg-sync-on-edit": '{"tool_input": {"file_path": "knowledge/x.md"}}',
    "compact-context-reinject": '{"session_id": "w6w"}',
    "config-change-audit": '{"source": "user_settings"}',
    "context-size-check": '{"session_id": "w6w"}',
    "user-prompt-submit-reminder": '{"prompt": "hi", "session_id": "w6w"}',
    "session-start-deferral-surface": '{"session_id": "w6w"}',
    "notify-stop": '{"session_id": "w6w"}',
    "lean-ctx-rewrite": '{"tool_name": "Bash", "tool_input": {"command": "pip install x"}}',
    "pre-compact-save": '{"trigger": "auto", "session_id": "w6w"}',
    "diff-context-inject": '{"session_id": "w6w", "prompt": "hi"}',
    "stop-failure-notify": '{"error": "rate_limit", "session_id": "w6w"}',
    "post-compact": '{"trigger": "auto", "session_id": "w6w"}',
    "worktree-guard": '{"name": "agent-w6w"}',
}

#: writer hook -> the file it writes under the project root.
WRITES = {
    "pre-compact-save": ".claude/context/pre-compact-snapshot.md",
    "config-change-audit": ".claude/logs/config_changes.jsonl",
    "worktree-guard": ".claude/logs/worktree-guard.jsonl",
}

UNEXECUTED = ("session-start-ensure-hub", "session-start-kg-loader",
              "code-graph-incremental", "check-no-fork-bomb")

FLAVOURS = ["sh"] + (["ps1"] if PWSH else [])


@pytest.fixture
def sandbox(tmp_path):
    sb = tmp_path / "sb"
    (sb / "home").mkdir(parents=True)
    (sb / "state").mkdir()
    (sb / "tmp").mkdir()
    proj = sb / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (proj / "a" / "b").mkdir(parents=True)
    (sb / "plain" / "deep").mkdir(parents=True)
    return sb


def _env(sb: Path, project_dir: "str | None") -> dict:
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("CLAUDE_PROJECT_DIR", "VCT_DISABLE_HOOKS", "VCT_INSTALL_ROOT",
                     "VCT_VENV", "VCT_USER_HOME_OVERRIDE", "KG_BASE_DIR",
                     "VCT_SESSION_ID", "CLAUDE_SESSION_ID")
    }
    env.update({
        "HOME": str(sb / "home"),
        "VCT_STATE_DIR": str(sb / "state"),
        "VCT_CLAUDE_DIR": str(sb / "home" / ".claude"),
        "TMPDIR": str(sb / "tmp"),
        "VCO_STOP_FAILURE_NOTIFY": "0",
        "WEAVIATE_URL": "http://127.0.0.1:9",
        "VCT_HUB_PORT": "9",
        "KG_NUDGE_OFF": "1",
    })
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = project_dir
    return env


def _run(hook: str, flavour: str, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    if flavour == "sh":
        argv = [BASH, str(HOOKS / f"{hook}.sh")]
    else:
        argv = [PWSH, *PWSH_BASE_FLAGS, "-NonInteractive", "-File", str(HOOKS / f"{hook}.ps1")]
    return subprocess.run(argv, cwd=cwd, env=env, input=SAFE_HOOKS[hook],
                          capture_output=True, text=True, timeout=120)


def _claude_dirs_under(root: Path) -> list:
    return sorted(str(p) for p in root.rglob(".claude"))


@pytest.mark.skipif(BASH is None, reason="no bash")
@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("hook", sorted(SAFE_HOOKS))
def test_no_project_means_no_claude_dir_in_the_cwd(hook, flavour, sandbox):
    cwd = sandbox / "plain" / "deep"
    _run(hook, flavour, cwd, _env(sandbox, None))
    assert _claude_dirs_under(sandbox / "plain") == [], f"{hook}.{flavour} planted .claude/"


@pytest.mark.skipif(BASH is None, reason="no bash")
@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("hook", sorted(SAFE_HOOKS))
def test_subfolder_run_never_plants_claude_in_the_subfolder(hook, flavour, sandbox):
    _run(hook, flavour, sandbox / "proj" / "a" / "b", _env(sandbox, None))
    assert _claude_dirs_under(sandbox / "proj" / "a") == [], f"{hook}.{flavour} planted .claude/"
    if hook in WRITES:
        assert (sandbox / "proj" / WRITES[hook]).is_file(), f"{hook}.{flavour} did not write at the root"


@pytest.mark.skipif(BASH is None, reason="no bash")
@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("hook", sorted(WRITES))
def test_claude_project_dir_still_wins(hook, flavour, sandbox):
    """The harness case is unchanged: the writer writes under the env root."""
    ws = sandbox / "ws"
    ws.mkdir()
    _run(hook, flavour, sandbox / "proj" / "a" / "b", _env(sandbox, str(ws)))
    assert (ws / WRITES[hook]).is_file(), f"{hook}.{flavour} ignored CLAUDE_PROJECT_DIR"
    assert not (sandbox / "proj" / WRITES[hook]).exists()


def _sh_resolution_block(text: str) -> str:
    """The hook's own lines that source the helper and call it."""
    keep = [ln for ln in text.splitlines()
            if "_lib/project-root.sh" in ln and ln.lstrip().startswith(".")
            or re.search(r"\$\(vco_project_root\)", ln)]
    return "\n".join(keep)


def _ps1_resolution_block(text: str) -> str:
    keep = [ln.strip() for ln in text.splitlines()
            if ("_lib/project-root.ps1" in ln and ln.lstrip().startswith("."))
            or re.search(r"=\s*Get-VcoProjectRoot\b", ln)]
    return "\n".join(keep)


@pytest.mark.skipif(BASH is None, reason="no bash")
@pytest.mark.parametrize("hook", UNEXECUTED)
def test_unexecuted_hooks_use_the_helper(hook, sandbox):
    """Run each unexecuted hook's OWN resolution lines from a subfolder.

    The lines are taken from the shipped hook (source + call), run in a shell
    whose BASH_SOURCE/PSScriptRoot points at the hooks dir, and must resolve
    the marked root — and the old ``$PWD`` fallback must be gone.
    """
    sh_text = (HOOKS / f"{hook}.sh").read_text(encoding="utf-8")
    assert not re.search(r"CLAUDE_PROJECT_DIR:-(\$PWD|\$\(pwd\))", sh_text)
    block = _sh_resolution_block(sh_text)
    assert block.count("vco_project_root") == 1, block
    var = re.search(r'(\w+)="\$\(vco_project_root\)"', block).group(1)
    script = (f'set -- ""; BASH_SOURCE_DIR="{HOOKS}"\n'
              + block.replace('$(dirname "${BASH_SOURCE[0]}")', f'"{HOOKS}"')
                     .replace("$SCRIPT_DIR", str(HOOKS))
              + f'\nprintf "%s" "${var}"\n')
    out = subprocess.run([BASH, "-c", script], cwd=sandbox / "proj" / "a" / "b",
                         env=_env(sandbox, None), capture_output=True, text=True, timeout=30)
    assert os.path.realpath(out.stdout) == os.path.realpath(sandbox / "proj"), out.stderr

    if PWSH:
        ps_text = (HOOKS / f"{hook}.ps1").read_text(encoding="utf-8-sig")
        assert "else { (Get-Location).Path }" not in ps_text
        pblock = _ps1_resolution_block(ps_text)
        pvar = re.search(r"\$(\w+)\s*=\s*Get-VcoProjectRoot", pblock).group(1)
        pscript = pblock.replace("$PSScriptRoot", f"'{HOOKS}'").replace("$ScriptDir", f"'{HOOKS}'")
        pscript += f"\n[Console]::Out.Write(${pvar})"
        pout = subprocess.run([PWSH, *PWSH_BASE_FLAGS, "-NonInteractive", "-Command", pscript],
                              cwd=sandbox / "proj" / "a" / "b", env=_env(sandbox, None),
                              capture_output=True, text=True, timeout=60)
        assert os.path.realpath(pout.stdout) == os.path.realpath(sandbox / "proj"), pout.stderr
