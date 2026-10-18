# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The wait guard's ONE literal-text exception (v0.2.104 E-4, owner-approved
allowlist inversion, 2026-10-17).

The wait guard (:mod:`vco_lib.wait_guard`) reads command-substitution
bodies out of every token, quoted or not, so a SEARCH for the text of a
wait — ``grep -n '$(sleep 5)' f`` — was denied as if it waited. Four rounds
tried to decide substitution liveness everywhere and each let a real wait
through. This module inverts the problem: it never weakens the guard's own
reading. It answers ONE narrow question first, and only a positive answer
skips the guard's static check:

    Is every simple command of this command line a program from
    :data:`LITERAL_ALLOWLIST` (programs that never run their arguments as
    shell code), with no subshell, group, function, comment, heredoc,
    background job, assignment, or command/process substitution that bash
    would expand — so that any ``$(…)`` / backtick text in it can only sit
    inside single quotes, ``$'…'``, or a backslash escape?

"Never run their arguments as shell code" is a statement about CODE, not
about time: these programs DO wait when the file they read never ends or
never answers (``cat /dev/zero``, ``grep x /dev/stdin``, a redirection to
bash's ``/dev/tcp/<host>/<port>``). So the exception reads paths the
ALLOWLIST way: every argument and redirection target must be a PLAIN word,
whose value is exactly its text once quotes are removed — no unquoted glob
(``* ? [``), no ``~``, no ``$VAR`` / ``${…}``, no ``$"…"``, no ``$'…'``
carrying an escape (brace expansion, ``$(…)`` and backticks are refused by
the scanner itself). Anything else gets the guard's own decision; the
quoted wait text itself is plain (``'$(sleep 5)'``, ``$'$(sleep 5)'``,
a backslash-escaped ``$``). Then the plain words are checked, case-folded (a
case-insensitive volume reads ``/DEV/zero`` as ``/dev/zero``), for a device
or a ``/proc`` file — see :func:`_names_device_path`: everything under
``/dev/`` except ``/dev/null``, ``/dev/stdout`` and ``/dev/stderr``, and
everything under ``/proc/``, written absolute or reached by climbing with
``..``. What it cannot see is a symlink to a device or a named pipe (FIFO)
in the project tree, and a recursive walk from ``/`` (``grep -r x /``
walks ``/proc`` and ``/sys`` — a long walk, not a device read): the same
boundary the guard had without the quoted text (``grep -r x /`` was
always allowed).

Anything that does not match exactly answers ``False`` and the guard's
v0.2.103 decision applies unchanged. The scanner is a single linear pass
over the raw text (regex jumps over ordinary runs, ``str.find`` over quoted
bodies) and rejects at the first construct it does not fully understand.

Programs whose options CAN run a command or wait get a per-program check
(and refuse any argument bash would expand or glob, since an expansion
could become such an option): ``tail`` (follow forms), ``find``
(``-exec``/``-execdir``/``-ok``/``-okdir``/``-delete``), ``git`` (only
``log``/``show``/``diff``/``blame``/``status``/``grep``, no global option,
no ``git grep -O``/``--open-files-in-pager``), ``rg`` (``--pre``,
``--hostname-bin``), ``ag`` (pager option), ``ack`` (a short list of
plain options only — Perl, rc files, and ack 2's code-evaluating
``--output``), ``sort``
(``--compress-program``) and ``printf`` (``-v`` assigns, and an array
subscript in the name is arithmetic-evaluated — it RUNS a ``$(…)``; probed
with real bash). A ``>&`` / ``<&`` redirection must name an fd number or
``-``: bash reads ``>&word`` as ``&>word`` and expands that word a SECOND
time, so ``echo x >&'$(sleep 5)'`` runs the sleep (found by a real-bash
fuzz, bash 5.2). ``sed`` (``e`` command / ``s///e``), ``awk``
(``system()``), ``xargs``, ``env``, ``eval`` and every shell are NOT on the
list.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

__all__ = ["LITERAL_ALLOWLIST", "literal_text_only"]

#: Programs that cannot run their arguments as code through ANY option —
#: arguments may carry expansions and globs.
_INERT = frozenset({
    "grep", "egrep", "fgrep", "cat", "head", "wc", "uniq", "cut", "tr",
    "ls", "echo",
})


_GREP_FAMILY = frozenset({"grep", "egrep", "fgrep"})


def _grep_devices_ok(args: List[str]) -> bool:
    # `-D read` / `--devices=read` makes a recursive grep READ every device
    # and FIFO it meets (a FIFO blocks, `/dev/zero` never ends); getopt
    # abbreviates, so refuse every `--de…` and any short cluster with `D`.
    for a in args:
        if a == "--":
            return True
        if a.startswith("--de"):
            return False
        if a.startswith("-") and not a.startswith("--") and "D" in a:
            return False
    return True


def _tail_ok(args: List[str]) -> bool:
    # GNU getopt abbreviates long options (`--fo` == `--follow`), and the
    # obsolete `+5f` / `-5f` forms follow too: refuse every long option
    # outside a small known-safe set, and every short cluster with f/F.
    for a in args:
        if a == "--":
            return True
        if a.startswith("--"):
            if not a.startswith(("--lines", "--bytes", "--quiet",
                                 "--silent", "--verbose", "--zero")):
                return False
        elif a.startswith(("-", "+")) and ("f" in a or "F" in a):
            return False
    return True


_FIND_RUNNERS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete"})


def _find_ok(args: List[str]) -> bool:
    return not any(a in _FIND_RUNNERS for a in args)


_GIT_READ_SUBCOMMANDS = frozenset({"log", "show", "diff", "blame", "status",
                                   "grep"})


def _git_ok(args: List[str]) -> bool:
    # args[0] must BE the subcommand: any global option (`-c`, `-C`,
    # `--exec-path`, `--paginate` …) or alias falls back to the base guard.
    if not args or args[0] not in _GIT_READ_SUBCOMMANDS:
        return False
    if args[0] == "grep":
        for a in args[1:]:
            if a == "--":
                break
            # `-O<pager>` / `--open-files-in-pager` (parse-options accepts
            # unique prefixes and clustered shorts) RUN the pager command.
            if a.startswith("--o"):
                return False
            if a.startswith("-") and not a.startswith("--") and "O" in a:
                return False
    return True


def _rg_ok(args: List[str]) -> bool:
    return not any(a.startswith(("--pre", "--hostname-bin")) for a in args)


def _ag_ok(args: List[str]) -> bool:
    # ag (getopt_long) abbreviates long options: refuse every `--pa…`
    # (`--pager`) and its `--no-pa…` twin.
    return not any(a.startswith(("--pa", "--no-pa")) for a in args)


#: ack is Perl (Getopt::Long: abbreviations, rc files, and in ack 2 an
#: `--output` that evaluates Perl — backticks RUN): only these options pass.
_ACK_SAFE_OPTIONS = frozenset({
    "-i", "-w", "-n", "-l", "-L", "-c", "-h", "-H", "-Q", "-v", "-s", "-1",
    "-r", "-R", "--literal", "--nocolor", "--noheading", "--nofilter",
})


def _ack_ok(args: List[str]) -> bool:
    for a in args:
        if a == "--":
            return True
        if a.startswith("-") and a not in _ACK_SAFE_OPTIONS:
            return False
    return True


def _sort_ok(args: List[str]) -> bool:
    return not any(a.startswith("--co") for a in args)  # --compress-program


def _printf_ok(args: List[str]) -> bool:
    return not any(a.startswith("-v") for a in args)


#: Programs with an option that can run a command or wait — checked per
#: program, and only with fully literal arguments.
_CHECKED: Dict[str, Callable[[List[str]], bool]] = {
    "tail": _tail_ok,
    "find": _find_ok,
    "git": _git_ok,
    "rg": _rg_ok,
    "ag": _ag_ok,
    "ack": _ack_ok,
    "sort": _sort_ok,
    "printf": _printf_ok,
}

#: The exact allowlist (documented in the CHANGELOG entry and the tests).
LITERAL_ALLOWLIST = frozenset(_INERT | set(_CHECKED))

#: A run of characters with no meaning to the bash lexer outside quotes.
_PLAIN_RUN = re.compile(r"[^ \t\n;&|<>(){}#\\'\"$`*?\[~]+")
#: The common case in one step: a whole plain word and the blanks after it.
_PLAIN_WORD_BLANKS = re.compile(r"([^ \t\n;&|<>(){}#\\'\"$`*?\[~]+)[ \t]+")
#: The body of an ANSI-C ``$'…'`` string (escapes skipped) + closing quote.
_ANSI_BODY = re.compile(r"(?:[^'\\]|\\.)*'", re.S)
#: Characters that end an ordinary run inside double quotes.
_DQ_SPECIAL = re.compile(r'["\\$`]')
#: What may follow ``$`` to make a parameter expansion.
_PARAM_START = re.compile(r"[A-Za-z0-9_@*#?$!-]")
#: The only targets a `>&` / `<&` duplication may name here: an fd or `-`.
_FD_DUP_TARGET = re.compile(r"^(?:[0-9]+-?|-)$")
#: A bare program name — no quoting, expansion, glob or path.
_BARE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_+.-]*$")

#: The only device files the exception accepts: they never block.
_SAFE_DEVICES = frozenset({"null", "stdout", "stderr"})


def _device_candidates(text: str) -> List[str]:
    """The strings in one word that could be a path: the word, the value of
    an ``--opt=value`` / ``opt=value`` form, and every tail of a short-option
    cluster (``-f/dev/stdin`` names ``/dev/stdin``)."""
    out = [text]
    if "=" in text:
        out.append(text.split("=", 1)[1])
    if text.startswith("-") and not text.startswith("--"):
        out.extend(text[k:] for k in range(1, len(text)))
    return out


def _path_names_device(path: str) -> bool:
    absolute = path.startswith("/")
    parts: List[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == ".." and parts and parts[-1] != "..":
            parts.pop()
        elif part == ".." and absolute and not parts:
            continue                          # `/..` is `/`
        else:
            parts.append(part)
    # case-folded: `/DEV/zero` IS `/dev/zero` on a case-insensitive volume
    parts = [p.lower() for p in parts]
    # an unknown base (`~`, `~user`, `$HOME`, `${X}`) may be `/` or empty
    unknown_base = bool(parts) and (parts[0].startswith("~") or "$" in parts[0])
    k = 1 if unknown_base else 0
    climbed = False
    while k < len(parts) and parts[k] == "..":
        k += 1
        climbed = True
    if k >= len(parts) or not (absolute or climbed or unknown_base):
        return False
    if parts[k] == "proc":
        return True
    # `/dev` itself too: `grep -r -D read x /dev` reads every device in it
    return parts[k] == "dev" and (k + 1 >= len(parts) or parts[k + 1] not in _SAFE_DEVICES)


def _names_device_path(text: str) -> bool:
    """True when ``text`` (one word or redirection target, quotes removed)
    can name a file that blocks or never ends: anything under ``/dev/``
    except ``/dev/null`` / ``/dev/stdout`` / ``/dev/stderr`` (``/dev/zero``,
    ``/dev/random``, ``/dev/urandom``, ``/dev/full``, ``/dev/stdin``,
    ``/dev/tty``, ``/dev/fd/N``, bash's ``/dev/tcp`` / ``/dev/udp``, a pts
    or a FIFO under ``/dev``) and anything under ``/proc/``
    (``/proc/self/fd/0``, ``/proc/kmsg``). A glob counts as its text:
    ``/dev/z*`` is refused."""
    folded = text.lower()
    if "dev" not in folded and "proc" not in folded:
        return False                          # the common case: one scan
    return any(_path_names_device(c) for c in _device_candidates(text))


_Word = Tuple[str, bool, bool]   # (text, live, plain)


def _scan(command: str) -> Optional[List[List[_Word]]]:
    """Split ``command`` into simple commands of words, or ``None`` at the
    first construct outside the understood subset. A word is
    ``(text, live, plain)``: ``live`` = bash would expand or glob part of
    it; ``plain`` = it was written with ordinary characters only."""
    segs: List[List[_Word]] = []
    words: List[_Word] = []
    buf: List[str] = []
    in_word = False
    live = False
    plain = True
    redirect_pending = False
    dup_pending = False       # the pending target belongs to `>&` / `<&`
    broken = False
    need_cmd = False          # after `|` / `&&` / `||` a command must follow
    n = len(command)
    i = 0

    def end_word() -> None:
        nonlocal buf, in_word, live, plain, redirect_pending, dup_pending, broken
        if in_word:
            if redirect_pending:
                redirect_pending = False      # a redirection target, not an arg
                # `>&word` with a word that is not an fd number is bash's
                # `&>word`, and bash EXPANDS that word a second time —
                # `echo x >&'$(sleep 5)'` RUNS the sleep (probed, bash 5.2).
                target = "".join(buf)
                if dup_pending and not (plain and _FD_DUP_TARGET.match(target)):
                    broken = True
                # only a plain target (its value is its text) is read, and
                # a network device connects, `< /dev/zero` never ends
                if live or _names_device_path(target):
                    broken = True
                dup_pending = False
            else:
                words.append(("".join(buf), live, plain))
        buf, in_word, live, plain = [], False, False, True

    def end_seg(binds: bool = False) -> bool:
        # `binds`: the separator joins two commands (`|`, `&&`, `||`), so
        # neither side may be empty — `a |` / `a | | b` are syntax errors.
        nonlocal words, need_cmd
        end_word()
        if broken or redirect_pending or ((need_cmd or binds) and not words):
            return False
        if words:
            segs.append(words)
        words = []
        need_cmd = binds
        return True

    while i < n:
        if not in_word and not redirect_pending:
            m = _PLAIN_WORD_BLANKS.match(command, i)
            if m:                             # fast path: `word ` -> one word
                words.append((m.group(1), False, True))
                i = m.end()
                continue
        c = command[i]
        m = _PLAIN_RUN.match(command, i)
        if m:
            buf.append(m.group())
            in_word = True
            i = m.end()
            continue
        nxt = command[i + 1] if i + 1 < n else ""
        if c in " \t":
            end_word()
            i += 1
        elif c == "\n":
            if not end_seg():
                return None
            i += 1
        elif c == ";":
            if nxt in (";", "&"):
                return None                   # case terminators
            if not end_seg():
                return None
            i += 1
        elif c == "&":
            if nxt == "&":
                if not end_seg(binds=True):
                    return None
                i += 2
            elif nxt == ">":                  # &> / &>> redirection
                end_word()
                if redirect_pending:
                    return None
                redirect_pending = True
                i += 3 if command.startswith(">>", i + 1) else 2
            else:
                return None                   # background job
        elif c == "|":
            if not end_seg(binds=True):
                return None
            i += 2 if nxt in ("|", "&") else 1
        elif c in "<>":
            if in_word and buf and "".join(buf).isdigit() and plain:
                buf, in_word = [], False      # an fd number: `2>&1`
            else:
                end_word()
            if redirect_pending or command.startswith("<<", i) or nxt == "(":
                return None                   # heredoc / herestring / proc subst
            redirect_pending = True
            i += 1
            if i < n and command[i] in ">&|" and not (c == "<" and command[i] == "|"):
                dup_pending = command[i] == "&"
                i += 1
            elif c == "<" and i < n and command[i] == ">":
                i += 1
        elif c in "(){}":
            return None                       # subshell / group / function
        elif c == "#":
            if not in_word:
                return None                   # a comment
            buf.append(c)
            i += 1
        elif c == "\\":
            if nxt == "\n":
                i += 2                        # line continuation
                continue
            buf.append(nxt or "\\")
            in_word, plain = True, False
            i += 2
        elif c == "'":
            j = command.find("'", i + 1)
            if j < 0:
                return None
            buf.append(command[i + 1:j])
            in_word, plain = True, False
            i = j + 1
        elif c == "`":
            return None
        elif c == "$":
            if nxt == "'":
                m = _ANSI_BODY.match(command, i + 2)
                if not m:
                    return None
                body = m.group()[:-1]
                buf.append(body)
                # an escape can spell an option (`$'\x2df'` == `-f`)
                in_word, plain = True, False
                live = live or "\\" in body
                i = m.end()
            elif nxt == '"':
                # $"…" is a locale-TRANSLATED string: its value is not its text
                in_word, plain, live = True, False, True
                i += 1
            elif nxt in ("(", "{", "["):
                return None
            elif nxt and _PARAM_START.match(nxt):
                buf.append(c)
                in_word, plain, live = True, False, True
                i += 1
            else:
                buf.append(c)
                in_word, plain = True, False
                i += 1
        elif c == '"':
            i += 1
            in_word, plain = True, False
            while True:
                m = _DQ_SPECIAL.search(command, i)
                if not m:
                    return None               # unterminated
                buf.append(command[i:m.start()])
                i = m.start()
                d = command[i]
                if d == '"':
                    i += 1
                    break
                if d == "`":
                    return None
                dn = command[i + 1] if i + 1 < n else ""
                if d == "\\":
                    if dn in ('$', '`', '"', "\\"):
                        buf.append(dn)
                        i += 2
                    elif dn == "\n":
                        i += 2
                    else:
                        buf.append("\\")
                        i += 1
                    continue
                # d == "$"
                if dn in ("(", "{", "["):
                    return None
                if dn and _PARAM_START.match(dn):
                    live = True
                buf.append("$")
                i += 1
        elif c in "*?[~":
            buf.append(c)
            in_word, plain, live = True, False, True
            i += 1
        else:  # pragma: no cover - _PLAIN_RUN takes every other character
            return None
    if not end_seg():
        return None
    return segs


def _segment_allowed(words: List[_Word]) -> bool:
    name, _live, plain = words[0]
    if not plain or not _BARE_NAME.match(name) or name not in LITERAL_ALLOWLIST:
        return False
    # Every argument must be a PLAIN word — its value is exactly its text
    # (quotes removed): no glob, no `~`, no `$VAR`, no `$"…"`, no `$'…'` with
    # an escape. Only then can the device check below read the path it
    # names; `/d*/zero`, `$'\x2fdev/zero'` would otherwise slip past it.
    if any(live for _t, live, _p in words[1:]):
        return False
    args = [t for t, _l, _p in words[1:]]
    if any(_names_device_path(t) for t in args):
        return False                          # `cat /dev/zero` never ends
    if name in _GREP_FAMILY and not _grep_devices_ok(args):
        return False
    if name in _INERT:
        return True
    return _CHECKED[name](args)


def literal_text_only(command: str) -> bool:
    """``True`` only when every simple command of ``command`` is an
    allowlisted program and nothing in it would make bash run code —
    then a ``$(…)`` / backtick in it is quoted TEXT, not a wait. ``False``
    for everything else (the guard's own reading then applies)."""
    if not isinstance(command, str) or not any(ch in command for ch in "$`("):
        return False
    segs = _scan(command)
    if not segs:
        return False
    return all(_segment_allowed(words) for words in segs)
