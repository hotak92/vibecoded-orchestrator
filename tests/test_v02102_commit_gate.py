# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-F item 4 — the post-commit KG reviewer's gate sees CHAINS.

Pre-fix defect (research report 2026-10-10, item 11 — verified live: the
reviewer last ran 2026-06-13): the post-tool-use-async dispatcher's
``git-commit`` gate matched only a command that STARTS with ``git commit``,
so ``cd sub && git commit -m x``, ``git add . && git commit`` and
``git -C repo commit`` never triggered the post-commit review.

Post-fix contract: ``vco_lib.git_commit_gate.command_has_git_commit`` walks
the command with the SHARED quote-aware walker
(``vco_lib.bash_command_walk.walk_command``; PowerShell commands go through
``vco_lib.powershell_command.command_for_bash_readers`` first) and fires on
a ``git commit`` SEGMENT anywhere in the chain — while quoted text
(``echo "git commit"``) and other porcelain (``git commit-tree``) do NOT
fire it. The dispatcher's gate (both siblings) calls this one home.

RED on the pre-fix tree: the module does not exist, and the dispatcher's
gate is prefix-only (the driven chain cases fail).
"""
from __future__ import annotations

import pytest

from vco_lib.git_commit_gate import command_has_git_commit

#: (command, expected) — Bash tool.
BASH_CASES = [
    # the pre-fix shape still fires
    ("git commit -m x", True),
    ("git commit", True),
    # chains — the defect's misses
    ("cd sub && git commit -m x", True),
    ("git add . && git commit -m 'msg'", True),
    ("git add .; git commit -m x; git push", True),
    ("echo start | tee /tmp/log && git commit -m x", True),
    # git global options between the verb and the subcommand
    ("git -C /repo commit -m x", True),
    ("git -c user.name=t commit -m x", True),
    ("git --git-dir=/repo/.git commit -m x", True),
    ("git --no-pager commit -m x", True),
    # wrappers and nesting
    ("sudo git commit -m x", True),
    ("bash -c 'git commit -m x'", True),
    ("FOO=1 git commit -m x", True),
    # NOT commits — the quoted-text and porcelain guards
    ("git commit-tree abc123", False),
    ("git commitlog", False),
    ("echo 'git commit'", False),
    ('echo "git commit -m x"', False),
    ("printf '%s' 'cd x && git commit'", False),
    ("git status && echo git commit", False),  # quoted? no — two segments:
    # `git status` and `echo git commit`; the echo segment's verb is echo
    ("git log --oneline -5", False),
    ("ls -la", False),
    ("git checkout -b commit-fix", False),
    ("", False),
]


@pytest.mark.parametrize("command,expected", BASH_CASES)
def test_bash_chains(command: str, expected: bool) -> None:
    assert command_has_git_commit(command) is expected, command


#: PowerShell tool commands (translated to their Bash reading first).
PS_CASES = [
    ("git commit -m x", True),
    ("Set-Location sub; git commit -m x", True),
    ("git add .; git commit -m x", True),
    ("Write-Output 'git commit'", False),
    ("git commit-tree abc123", False),
]


@pytest.mark.parametrize("command,expected", PS_CASES)
def test_powershell_tool_commands(command: str, expected: bool) -> None:
    assert command_has_git_commit(command, tool_name="PowerShell") is expected, command
