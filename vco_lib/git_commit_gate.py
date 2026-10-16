# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``git commit`` detection for hook gates (v0.2.102 W6-F, one home).

The post-tool-use-async dispatcher routes ``post-git-commit-kg-sync`` (the
post-commit KG reviewer) on whether a Bash/PowerShell tool call committed.
Pre-W6-F that gate matched only a command that STARTS with ``git commit``,
so the commonest real shapes — ``cd sub && git commit``, ``git add . &&
git commit``, ``git -C repo commit`` — never fired it (the reviewer's log
showed its last run months stale).

The detection walks the command with the SHARED quote-aware chain walker
(:func:`vco_lib.bash_command_walk.walk_command`): every chain segment is
env-stripped, wrapper-peeled and ``bash -c``-recursed by that one home, and
the collector below answers "is THIS segment a ``git commit`` invocation".
PowerShell tool commands are translated first
(:func:`vco_lib.powershell_command.command_for_bash_readers`), so the same
walk serves both shell tools.

Guards that make this a DETECTOR, not a substring match:
  * quoted text is data, not a segment — ``echo "git commit"`` is False;
  * the subcommand must be exactly ``commit`` — ``git commit-tree`` and
    ``git commitlog`` are False (the retired ``if: Bash(git commit *)``
    word-boundary rule);
  * git's own GLOBAL options (``-C``, ``-c``, ``--git-dir``, …) between the
    verb and the subcommand are skipped, so ``git -C /repo commit`` is True;
  * heredoc BODIES are stripped before the walk (v0.2.103: the walker
    treats an unquoted newline as a command separator, so a body line
    would otherwise become a segment — raw text, not a command);
  * an unquoted newline separates commands (the walker's own rule), so
    ``make\\ngit commit -m x`` is True.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from vco_lib.bash_command_walk import walk_command
from vco_lib.bash_write_targets import strip_heredocs
from vco_lib.powershell_command import BASH_TOOL, POWERSHELL_TOOL, command_for_bash_readers

__all__ = ["command_has_git_commit"]

#: git GLOBAL options that take a separate value token
#: (``git -C <dir>``, ``git -c k=v``, ...).
_GIT_VALUE_FLAGS = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
})
#: the same options in ``--opt=value`` form.
_GIT_VALUE_PREFIXES = tuple(f + "=" for f in
                            ("--git-dir", "--work-tree", "--namespace",
                             "--exec-path"))


def _segment_is_git_commit(segment: List[str]) -> List[str]:
    """``walk_command`` collector: ``["commit"]`` when the peeled segment IS
    a ``git commit`` invocation, else ``[]``."""
    if len(segment) < 2:
        return []
    if os.path.basename(segment[0]).lower() != "git":
        return []
    i = 1
    while i < len(segment):
        tok = segment[i]
        if tok in _GIT_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith(_GIT_VALUE_PREFIXES):
            i += 1
            continue
        if tok.startswith("-"):
            i += 1  # boolean globals: --no-pager, -P, --no-replace-objects, …
            continue
        break
    if i < len(segment) and segment[i] == "commit":
        return ["commit"]
    return []


def command_has_git_commit(command: str, tool_name: str = BASH_TOOL) -> bool:
    """True when ``command`` — a Bash or PowerShell tool command — runs
    ``git commit`` in ANY chain segment. Quote-aware via the shared walker;
    never raises (an unparseable command is not a commit)."""
    cmd = command_for_bash_readers(tool_name, command or "")
    if not cmd.strip():
        return False
    cmd, _bodies = strip_heredocs(cmd)
    return bool(walk_command(cmd, _segment_is_git_commit))


def _cli_main(argv: Optional[List[str]] = None) -> int:
    """``python -m vco_lib.git_commit_gate [--tool PowerShell]`` — the command
    on STDIN (never argv, R31), stdout "1"/"0". Always exit 0."""
    parser = argparse.ArgumentParser(prog="vco_lib.git_commit_gate")
    parser.add_argument("--tool", default=BASH_TOOL,
                        choices=(BASH_TOOL, POWERSHELL_TOOL))
    args = parser.parse_args(argv)
    try:
        command = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        command = ""
    sys.stdout.write("1\n" if command_has_git_commit(command, args.tool) else "0\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
