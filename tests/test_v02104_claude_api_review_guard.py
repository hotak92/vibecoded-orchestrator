# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 R27 (Fable FINAL2 HIGH + MEDIUM) — the ``claude-api-run`` review
guard: an ALLOWLIST PreToolUse hook (``vco_lib.claude_api_review_guard``).

Pinned:

* the ordinary review commands pass (``git diff a...b``, ``git log
  --oneline a..b``, ``git show sha:path``, ``git diff --stat``, ``git blame
  -L 10,20 f`` …);
* the review's bypass examples are refused — a global option before the
  subcommand (``git -C . commit``, ``git -c alias.x=… x``, ``git
  --git-dir=… push``), ``git diff --no-index --output=<dest>``, abbreviated
  long options (``--outp=``), shell syntax (``;``, ``&&``, ``|``, ``$( )``,
  backticks, ``>``, newline, quotes, escapes), and paths outside the tree;
* every non-allowlisted tool (``store_knowledge_node``, other MCP servers,
  ``Write``) is denied; Read/Grep/Glob and the KG search tools get no
  decision (the run's allow rules approve them);
* the hook as the CLI runs it (the exact ``--settings`` command string, via
  a shell) answers with JSON, and a guard that cannot be imported exits 2 —
  the CLI's BLOCKING code (measured live: any other non-zero exit is
  non-blocking and the CLI ran ``git status`` anyway).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import pytest

from vco_lib import claude_api_review_guard as guard
from vco_lib import claude_api_run as car

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ALLOWED = [
    "git diff 469e93ce...HEAD",
    "git log --oneline a..b",
    "git show sha:path",
    "git show 469e93ce:vco_lib/claude_api_run.py",
    "git diff --stat",
    "git blame -L 10,20 f",
    "git blame -L10,20 vco_lib/x.py",
    "git status",
    "git status --short",
    "git status --porcelain=v2 -uno",
    "git log -5",
    "git log -n 5 --format=%H",
    "git log --since=2026-10-01 --author=someone",
    "git diff --name-only HEAD~3",
    "git diff -U10 main...HEAD -- vco_lib/claude_api_run.py",
    "git diff --cached --stat",
    "git show --stat HEAD",
    "git ls-files vco_lib",
    "git ls-files -o --exclude-standard",
    "git rev-parse HEAD",
    "git rev-parse --show-toplevel",
    "git rev-parse --abbrev-ref HEAD",
    "git merge-base main HEAD",
    "git merge-base --is-ancestor a b",
    "git log -L:main:vco_lib/claude_api_run.py",
    "  git   diff\tHEAD  ",
]

DENIED = [
    # FINAL3 LOW: %G signature placeholders spawn gpg / ssh-keygen
    "git log --format=%GG",
    "git log --pretty=format:%h%GS",
    "git show --format=%GK HEAD",
    # the review's examples
    "git -C . commit",
    "git -C . status",
    "git -c alias.x='!sh -c id' x",
    "git -c alias.x=!id x",
    "git --git-dir=/x push",
    "git --git-dir=.git log",
    "git --work-tree=. status",
    "git --exec-path=/tmp log",
    "git --namespace=x log",
    "git --no-pager log",
    "git diff --no-index --output=/tmp/dest f /dev/null",
    "git diff --no-index --output=dest f nul",
    # --output and friends, in every spelling git accepts
    "git diff --output=dest",
    "git diff --output dest",
    "git log -p --output=dest",
    "git show --output=dest HEAD",
    "git diff --outp=dest",
    "git diff --out=dest",
    "git diff --output-indicator-new=x",
    "git diff --ext-diff",
    "git log --ext-diff -p",
    "git diff --textconv",
    "git show --textconv HEAD:f",
    "git show --show-signature",
    "git log --show-signature",
    "git blame --contents /etc/passwd f",
    "git blame --contents=x f",
    "git blame -S revs f",
    "git blame --ignore-revs-file=x f",
    "git ls-files --exclude-from=x",
    "git log --exec=x",
    "git log --upload-pack=x",
    # outside the working tree
    "git diff /etc/passwd /dev/null",
    "git diff ../other/file f",
    "git diff a/../../x b",
    "git diff -- ../x",
    "git log --relative=/etc",
    "git diff ~/.ssh/id_rsa f",
    "git diff C:/Windows/win.ini f",
    "git diff C:x y",  # R27 LOW: drive-relative, no slash
    "git show c:secret",
    "git diff ..",
    # not git / not read-only
    "git commit -m x",
    "git push",
    "git fetch",
    "git pull",
    "git notes add",
    "git update-ref HEAD x",
    "git branch -f main x",
    "git remote add x y",
    "git submodule update",
    "git gc",
    "git reflog expire --all",
    "git config core.pager sh",
    "git",
    "",
    "   ",
    "/usr/bin/git log",
    "env git log",
    "GIT_EXTERNAL_DIFF=sh git diff",
    "sh -c 'git log'",
    "python -c 1",
    "rm -rf x",
    # shell syntax
    "git status; rm -rf x",
    "git status && rm x",
    "git status || rm x",
    "git log | sh",
    "git log $(id)",
    "git log ${HOME}",
    "git log $HOME",
    "git log `id`",
    "git log > f",
    "git log >> f",
    "git log < f",
    "git log 2>f",
    "git log\nrm x",
    "git log\rrm x",
    "git log & rm x",
    "git log 'a b'",
    'git log "a b"',
    "git log a\\ b",
    "git log *",
    "git log ?",
    "git log [ab]",
    "git log {a,b}",
    "git log # comment",
    "git log !x",
    "git log -- -x",
    "git diff -- --output=x",
    "git diff -",
    "git log \u00e9",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_ordinary_review_commands_pass(command):
    assert guard.classify_git_command(command) is None


@pytest.mark.parametrize("command", DENIED)
def test_everything_else_is_refused_with_a_reason(command):
    why = guard.classify_git_command(command)
    assert isinstance(why, str) and why


def test_reasons_name_what_was_refused():
    assert "'-C'" in guard.classify_git_command("git -C . commit")
    assert "--outp=x" in guard.classify_git_command("git diff --outp=x")
    assert "`git push`" in guard.classify_git_command("git push")
    assert "';'" in guard.classify_git_command("git status; id")
    assert "outside the working tree" in \
        guard.classify_git_command("git diff /etc/passwd f")


def test_every_option_table_entry_is_well_formed():
    for sub, opts in guard._OPTIONS.items():
        assert sub in guard.READ_ONLY_GIT_SUBCOMMANDS
        for opt in opts:
            assert opt.startswith("-"), (sub, opt)
            if opt.endswith("*"):
                assert len(opt) == 3 and opt[1] != "-", (sub, opt)
            assert not opt.startswith("--output"), (sub, opt)
            assert opt not in ("--ext-diff", "--textconv", "--no-index",
                               "--show-signature", "--contents"), (sub, opt)
    assert set(guard._OPTIONS) == set(guard.READ_ONLY_GIT_SUBCOMMANDS)


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob",
                                  *guard.READ_ONLY_MCP_TOOLS])
def test_read_only_tools_get_no_decision(tool):
    assert guard.decide({"tool_name": tool, "tool_input": {}}) == (None, "")


@pytest.mark.parametrize("tool", [
    "mcp__weaviate-kg__store_knowledge_node", "mcp__playwright__browser_click",
    "mcp__vct-coordination__send_message", "Write", "Edit", "NotebookEdit",
    "WebFetch", "Agent", "Task", "PowerShell", "", "SomeFutureTool",
])
def test_every_other_tool_is_denied(tool):
    decision, reason = guard.decide({"tool_name": tool, "tool_input": {}})
    assert decision == "deny" and "review policy" in reason


def test_bash_is_allowed_only_through_the_classifier():
    assert guard.decide({"tool_name": "Bash",
                         "tool_input": {"command": "git status"}})[0] == "allow"
    assert guard.decide({"tool_name": "Bash",
                         "tool_input": {"command": "git -C . status"}})[0] \
        == "deny"
    assert guard.decide({"tool_name": "Bash", "tool_input": None})[0] == "deny"
    assert guard.decide({"tool_name": "Bash"})[0] == "deny"


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null"])
def test_an_unreadable_payload_is_denied(raw):
    out = io.StringIO()
    assert guard.main(io.StringIO(raw), out) == 0
    answer = json.loads(out.getvalue())["hookSpecificOutput"]
    assert answer["permissionDecision"] == "deny"


def _run_hook_command(command: str, payload: dict,
                      env: "dict | None" = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", command], input=json.dumps(payload),
        capture_output=True, text=True, timeout=60,
        env=env if env is not None else
        {**os.environ, "PYTHONPATH": REPO}, cwd=REPO)


@pytest.mark.skipif(sys.platform == "win32",
                    reason="the POSIX quoting leg; Windows quoting is pinned "
                           "by test_the_windows_hook_command_shape")
class TestTheHookAsTheCliRunsIt:

    def test_a_denied_command_answers_deny_json(self):
        cmd = car.guard_hook_command()
        proc = _run_hook_command(cmd, {"tool_name": "Bash",
                                       "tool_input": {"command": "git -C . commit"}})
        assert proc.returncode == 0, proc.stderr
        answer = json.loads(proc.stdout)["hookSpecificOutput"]
        assert answer["hookEventName"] == "PreToolUse"
        assert answer["permissionDecision"] == "deny"
        assert "'-C'" in answer["permissionDecisionReason"]

    def test_an_allowed_command_answers_allow_json(self):
        proc = _run_hook_command(car.guard_hook_command(), {
            "tool_name": "Bash", "tool_input": {"command": "git diff --stat"}})
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["hookSpecificOutput"][
            "permissionDecision"] == "allow"

    def test_a_read_gets_no_output(self):
        proc = _run_hook_command(car.guard_hook_command(), {
            "tool_name": "Read", "tool_input": {"file_path": "x"}})
        assert proc.returncode == 0 and proc.stdout == ""

    def test_a_guard_that_cannot_run_exits_2_and_blocks(self, monkeypatch):
        import types

        monkeypatch.setattr(car, "_guard", types.SimpleNamespace(
            __name__="vco_lib.no_such_guard_module_for_test"))
        proc = _run_hook_command(car.guard_hook_command(), {
            "tool_name": "Bash", "tool_input": {"command": "git status"}})
        assert proc.returncode == 2
        assert proc.stdout == ""

    def test_the_settings_json_carries_that_command(self):
        settings = json.loads(car.review_settings())
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert cmd == car.guard_hook_command()


def test_the_windows_hook_command_shape(monkeypatch):
    monkeypatch.setattr(car.os, "name", "nt")
    cmd = car.guard_hook_command(r"C:\Users\a b\.venv\Scripts\python.exe")
    assert cmd == ('"C:/Users/a b/.venv/Scripts/python.exe" -m '
                   "vco_lib.claude_api_review_guard || exit 2")
