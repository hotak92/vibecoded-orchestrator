# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The PreToolUse guard of ``claude-api-run --policy review`` (v0.2.104).

:mod:`vco_lib.claude_api_run` runs the review in the CLI's ``--restricted``
mode (user / project / local settings files are ignored) and delivers ONE
hook through ``--settings``::

    <the same interpreter> -m vco_lib.claude_api_review_guard

The hook reads the PreToolUse payload on stdin and answers with an
ALLOWLIST — anything it does not positively recognise is denied:

* ``Read`` / ``Grep`` / ``Glob`` and :data:`READ_ONLY_MCP_TOOLS` (VCO's
  KG / code-graph SEARCH tools) — no decision: the run's own allow rules
  approve them, and ``--restricted`` confines the file tools to the working
  directories.
* ``Bash`` — approved (``permissionDecision: allow``) only when
  :func:`classify_git_command` accepts the command; denied otherwise.
* every other tool (``store_knowledge_node``, any other MCP server's tools,
  anything a newer CLI adds) — denied.

:func:`classify_git_command` approves a command only when ALL hold:

1. every character is in :data:`_SAFE_CHARS` (ASCII letters, digits, space,
   tab and ``_ - . / : @ ^ ~ = , + %``) — so there is no quoting, escaping,
   globbing, substitution, redirection, separator, pipe, comment or newline
   for the shell to interpret, and the words the shell will pass are exactly
   the whitespace-split words checked here;
2. the first word is exactly ``git`` (no path, no ``env``, no ``VAR=``);
3. the second word is one of :data:`READ_ONLY_GIT_SUBCOMMANDS` — so no global
   option (``-c``, ``-C``, ``--git-dir``, ``--work-tree``, ``--exec-path``,
   ``--namespace``, ``--no-pager`` …) can come before it;
4. every word that starts with ``-`` is an EXACT entry of that
   subcommand's option allowlist (:data:`_OPTIONS`), or a ``--name=value`` /
   ``-Xvalue`` form of an entry listed as taking a value. Git accepts
   abbreviated long options (``--outp=x`` is ``--output=x``), so the rule is
   "exact names from a list of read-only options", never "not one of these
   prefixes". Left out on purpose: everything that writes a file
   (``--output``), runs a program (``--ext-diff``, ``--textconv``,
   ``--show-signature`` → gpg, ``--exec*``, ``--upload-pack``) or reads a
   file outside the repository (``diff --no-index``, ``blame --contents`` /
   ``-S``, ``--ignore-revs-file``, ``ls-files --exclude-from``). After ``--``
   the words are pathspecs, and none of them may start with ``-`` either.
   ``git log`` / ``git show`` also take ``-<number>`` (``-5``);
5. no word names a path outside the working tree: none starts with ``/``,
   ``~``, ``../`` or a drive letter (``C:/``), and none contains ``/../`` or
   is ``..`` (``git diff`` silently becomes ``--no-index`` when a path lies
   outside the repository). Ranges such as ``a..b`` and ``a...b`` pass.

What is NOT covered (and is the same in any git use): drivers the
repository's OWN git config / attributes define (a ``diff.<x>.textconv``,
``core.fsmonitor``) still run when git consults them — the model cannot
change that config, because ``-c``, ``git config`` and every write tool are
denied. ``git status`` may refresh the index file, as it always does.

The hook fails CLOSED: if it cannot run (a broken interpreter), the CLI
treats the hook error as non-blocking and falls back to the permission
rules, and the review policy pre-approves NO Bash rule — under
``--permission-mode dontAsk`` the command is then refused.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Mapping, Optional

#: The only subcommands a review run may execute.
READ_ONLY_GIT_SUBCOMMANDS = (
    "diff", "log", "show", "status", "blame", "ls-files", "rev-parse",
    "merge-base",
)

#: VCO's bundled read-only KG / code-graph search tools (the weaviate-kg
#: MCP server). ``store_knowledge_node`` is deliberately absent: it writes a
#: markdown file and upserts it into Weaviate.
READ_ONLY_MCP_TOOLS = (
    "mcp__weaviate-kg__hybrid_search",
    "mcp__weaviate-kg__semantic_graph_search",
    "mcp__weaviate-kg__search_code_graph",
    "mcp__weaviate-kg__query_code_structure",
)

#: Built-in tools passed through to the run's own permission rules.
READ_ONLY_BUILTIN_TOOLS = ("Read", "Grep", "Glob")

_SAFE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    " \t_-./:@^~=,+%"
)

# ── option allowlists ────────────────────────────────────────────────────
# A plain entry (``"--stat"``) is accepted exactly. An entry ending in ``=``
# (``"--stat="``) also accepts ``--stat=<value>``. A short entry ending in
# ``*`` (``"-n*"``) accepts ``-n`` followed by an attached value (``-n5``).
# Values are already limited to _SAFE_CHARS by rule 1.

_DIFF_OUTPUT = (
    "-p", "-u", "-s", "--patch", "--no-patch", "-U*", "--unified=",
    "--raw", "--patch-with-raw", "--patch-with-stat", "--stat", "--stat=",
    "--numstat", "--shortstat", "--dirstat", "--dirstat=", "--summary",
    "--compact-summary", "--name-only", "--name-status", "-z",
    "--color", "--color=", "--no-color", "--word-diff", "--word-diff=",
    "--color-words", "--minimal", "--patience", "--histogram",
    "--diff-algorithm=", "--full-index", "--binary", "--abbrev", "--abbrev=",
    "--no-abbrev", "-M", "-M*", "--find-renames", "--find-renames=",
    "-C", "-C*", "--find-copies", "--find-copies=", "-B", "-B*",
    "--break-rewrites", "--break-rewrites=", "-D", "--irreversible-delete",
    "--no-renames", "--diff-filter=", "-R", "--relative", "--relative=",
    "--no-relative", "-a", "--text", "-b", "-w", "--ignore-space-change",
    "--ignore-all-space", "--ignore-space-at-eol", "--ignore-blank-lines",
    "--ignore-cr-at-eol", "--inter-hunk-context=", "-W", "--function-context",
    "--check", "--exit-code", "--quiet", "--no-ext-diff", "--no-textconv",
    "--ignore-submodules", "--ignore-submodules=", "--src-prefix=",
    "--dst-prefix=", "--no-prefix", "-S*", "-G*", "--pickaxe-all",
    "--pickaxe-regex", "-l*",
)

_DIFF_ONLY = ("--cached", "--staged", "--merge-base", "-1", "-2", "-3")

_LOG_ONLY = (
    "--oneline", "--graph", "--decorate", "--decorate=", "--no-decorate",
    "--all", "--branches", "--tags", "--remotes", "--reverse", "--follow",
    "--source", "--topo-order", "--date-order", "--author-date-order",
    "--left-right", "--cherry-pick", "--cherry-mark", "--cherry",
    "--boundary", "--full-history", "--simplify-merges", "--ancestry-path",
    "--first-parent", "--no-merges", "--merges", "--no-walk", "--no-walk=",
    "--walk-reflogs", "-g", "-m", "-c", "--cc", "--diff-merges=",
    "--no-diff-merges", "--format=", "--pretty", "--pretty=", "--date=",
    "--relative-date", "--since=", "--until=", "--after=", "--before=",
    "--author=", "--committer=", "--grep=", "-i", "--regexp-ignore-case",
    "--invert-grep", "--all-match", "-E", "--extended-regexp", "-F",
    "--fixed-strings", "-n*", "--max-count=", "--skip=", "--max-parents=",
    "--min-parents=", "-L*", "-L", "--abbrev-commit", "--no-abbrev-commit",
    "--parents", "--children", "--count", "--log-size", "--shortstat",
    "--expand-tabs", "--expand-tabs=", "--no-expand-tabs", "--mailmap",
    "--no-mailmap", "--use-mailmap", "-n",
)

_BLAME = (
    "-L*", "-L", "-l", "-t", "-s", "-e", "--show-email", "-w", "-M", "-M*",
    "-C", "-C*", "-p", "--porcelain", "--line-porcelain", "--incremental",
    "--root", "--show-stats", "-f", "--show-name", "-n", "--show-number",
    "--date=", "--abbrev=", "-c", "--reverse", "--first-parent",
    "--minimal", "-b", "--color-lines", "--color-by-age", "--progress",
    "--no-progress",
)

_STATUS = (
    "-s", "--short", "-b", "--branch", "--porcelain", "--porcelain=",
    "--long", "-v", "--verbose", "-u", "-u*", "--untracked-files",
    "--untracked-files=", "--ignored", "--ignored=", "--ignore-submodules",
    "--ignore-submodules=", "-z", "--no-column", "--ahead-behind",
    "--no-ahead-behind", "--renames", "--no-renames", "--find-renames",
    "--find-renames=", "--show-stash",
)

_LS_FILES = (
    "-c", "--cached", "-d", "--deleted", "-m", "--modified", "-o",
    "--others", "-i", "--ignored", "-s", "--stage", "-u", "--unmerged",
    "-k", "--killed", "-z", "-t", "-v", "-f", "--full-name",
    "--recurse-submodules", "--abbrev", "--abbrev=", "--directory",
    "--no-empty-directory", "--eol", "--error-unmatch", "--exclude-standard",
    "-x", "--exclude=", "--format=", "--deduplicate", "--sparse",
)

_REV_PARSE = (
    "--verify", "-q", "--quiet", "--short", "--short=", "--abbrev-ref",
    "--abbrev-ref=", "--symbolic", "--symbolic-full-name", "--all",
    "--branches", "--branches=", "--tags", "--tags=", "--remotes",
    "--remotes=", "--show-toplevel", "--show-prefix", "--show-cdup",
    "--git-dir", "--git-common-dir", "--absolute-git-dir",
    "--is-inside-work-tree", "--is-inside-git-dir", "--is-bare-repository",
    "--is-shallow-repository", "--show-superproject-working-tree",
    "--show-object-format", "--show-object-format=", "--revs-only",
    "--no-revs", "--flags", "--no-flags", "--default", "--since=",
    "--until=", "--after=", "--before=", "--git-path", "--local-env-vars",
    "--end-of-options",
)

_MERGE_BASE = ("--all", "--octopus", "--is-ancestor", "--independent",
               "--fork-point")

_OPTIONS: "dict[str, frozenset[str]]" = {
    "diff": frozenset(_DIFF_OUTPUT + _DIFF_ONLY),
    "log": frozenset(_DIFF_OUTPUT + _LOG_ONLY),
    "show": frozenset(_DIFF_OUTPUT + _LOG_ONLY),
    "status": frozenset(_STATUS),
    "blame": frozenset(_BLAME),
    "ls-files": frozenset(_LS_FILES),
    "rev-parse": frozenset(_REV_PARSE),
    "merge-base": frozenset(_MERGE_BASE),
}


def _option_allowed(word: str, allowed: "frozenset[str]") -> bool:
    if word in allowed:
        return True
    if word.startswith("--") and "=" in word:
        name = word.split("=", 1)[0]
        return f"{name}=" in allowed
    if len(word) > 2 and word[0] == "-" and word[1] != "-":
        return f"{word[:2]}*" in allowed
    return False


_NUMBER_OPTION = re.compile(r"-[0-9]+")
#: Any Windows drive prefix, the drive-RELATIVE ``C:x`` included (R27 LOW:
#: ``C:x`` resolves against that drive's current directory, outside the repo).
#: Conservative cost: a one-letter ref in ``<ref>:<path>`` is refused too.
_DRIVE = re.compile(r"[A-Za-z]:")


def _outside_path(word: str) -> bool:
    """Whether *word* (an option value included) names a path outside the
    working tree (rule 5 of the module docstring)."""
    value = word.split("=", 1)[1] if word.startswith("-") and "=" in word \
        else word
    return (value.startswith(("/", "~", "../")) or value == ".."
            or "/../" in value or value.endswith("/..")
            or bool(_DRIVE.match(value)))


def classify_git_command(command: str) -> Optional[str]:
    """``None`` when *command* is an approved read-only git call, else the
    reason it is denied (one sentence, shown to the model)."""
    text = (command or "").strip()
    if not text:
        return "an empty command"
    bad = sorted({c for c in text if c not in _SAFE_CHARS})
    if bad:
        shown = " ".join(repr(c) for c in bad[:5])
        return (f"the command contains {shown} — only a single plain git "
                "command is allowed: no quotes, escapes, globs, pipes, "
                "redirection, substitution, separators or newlines")
    words = text.split()
    if words[0] != "git":
        return (f"{words[0]!r} is not allowed — only read-only git "
                f"({', '.join(READ_ONLY_GIT_SUBCOMMANDS)})")
    if len(words) < 2:
        return "a bare `git` with no subcommand"
    sub = words[1]
    if sub.startswith("-"):
        return (f"global git option {sub!r} before the subcommand is not "
                "allowed — write `git <subcommand> …` with options after it")
    if sub not in READ_ONLY_GIT_SUBCOMMANDS:
        return (f"`git {sub}` is not allowed — only "
                f"{', '.join(READ_ONLY_GIT_SUBCOMMANDS)}")
    allowed = _OPTIONS[sub]
    after_separator = False
    for word in words[2:]:
        if word == "--" and not after_separator:
            after_separator = True
            continue
        if _outside_path(word):
            return (f"{word!r} points outside the working tree — a review "
                    "reads only the repository it runs in")
        if not word.startswith("-"):
            continue
        if after_separator:
            return f"a pathspec that starts with '-' ({word!r}) is not allowed"
        if sub in ("log", "show") and _NUMBER_OPTION.fullmatch(word):
            continue
        if not _option_allowed(word, allowed):
            return (f"option {word!r} is not on the read-only allowlist for "
                    f"`git {sub}` (options that write files, run programs or "
                    "read files outside the repository are refused; "
                    "abbreviated long options are refused too)")
    return None


def decide(payload: Mapping[str, Any]) -> "tuple[Optional[str], str]":
    """``(decision, reason)`` for one PreToolUse payload.

    *decision* is ``"allow"``, ``"deny"`` or ``None`` (no decision: the
    run's permission rules apply).
    """
    tool = str(payload.get("tool_name") or "")
    if tool in READ_ONLY_BUILTIN_TOOLS or tool in READ_ONLY_MCP_TOOLS:
        return None, ""
    if tool == "Bash":
        tool_input = payload.get("tool_input")
        command = ""
        if isinstance(tool_input, Mapping):
            command = str(tool_input.get("command") or "")
        why = classify_git_command(command)
        if why is None:
            return "allow", "read-only git (claude-api-run review policy)"
        return "deny", f"claude-api-run review policy: {why}"
    return "deny", (f"claude-api-run review policy: the tool {tool!r} is not "
                    "available in a read-only review (allowed: Read, Grep, "
                    "Glob, read-only git through Bash, and VCO's KG / "
                    "code-graph search tools)")


def _answer(decision: str, reason: str) -> str:
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }})


def main(stdin: Any = None, stdout: Any = None) -> int:
    src = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    try:
        payload = json.loads(src.read() or "{}")
    except (ValueError, OSError):
        payload = None
    if not isinstance(payload, dict):
        out.write(_answer("deny", "claude-api-run review policy: the hook "
                          "could not read the tool call"))
        return 0
    decision, reason = decide(payload)
    if decision is not None:
        out.write(_answer(decision, reason))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
