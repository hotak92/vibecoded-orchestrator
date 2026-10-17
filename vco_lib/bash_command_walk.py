# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Shared chain-walker for Bash command strings (v0.2.95, lane F10).

ONE home for "split a shell command into independently-walkable
segments". Extracted from the diagram-delete parser (a module v0.2.102
removed with the diagrams runtime) when a SECOND consumer appeared
(``vco_lib.bash_write_targets``, which needs the same chain / wrapper /
env-prefix / ``bash -c`` handling to find WRITE targets rather than
DELETE targets).

Per the project's A>B>C rule the two consumers now share this
implementation instead of mirroring it: a fix to the chain splitter or
the wrapper peeler lands once and both parsers get it.

What the walker does, per command string:

  1. ``shlex.split`` the command (POSIX mode) and peel glued UNQUOTED
     separator tails into standalone tokens (``log;`` → ``log``, ``;``)
     — quote-aware, via a posix=False shadow pass (see :func:`tokenize`).
     An UNQUOTED newline is a command separator exactly like ``;``
     (v0.2.103): ``make\\nsleep 5`` is judged like ``make; sleep 5``.
     A newline inside quotes, or after a backslash (line continuation),
     is data and stays.
  2. Split the token list on top-level ``;`` ``&&`` ``||`` ``|`` ``&``.
  3. Per segment: drop leading ``KEY=val`` env assignments, then peel
     wrapper verbs (``sudo``, ``nice -n 10``, ``env``, ``time``, ...).
  4. If the peeled verb is a shell (``bash``/``sh``/``zsh``/...) invoked
     with ``-c "<sub-command>"``, RECURSE into the sub-command with the
     same collector (depth-bounded at ``MAX_NESTING_DEPTH``).
  5. Otherwise hand the peeled segment to the caller's ``collect``
     callback and aggregate whatever it returned.

The walker is side-effect free: no filesystem reads, no subprocess
spawns. Collectors may be stateful (``bash_write_targets`` tracks a
running ``cd`` so a relative redirect after ``cd /tmp`` is not
mis-attributed to the project root) -- segments are handed over in
source order, left to right, which is what makes that legal.

Structural context (v0.2.103)
=============================
:func:`walk_segments` is the context-exposing twin of
:func:`walk_command` (which is now a thin adapter over it, so the two can
never disagree): every peeled segment is handed out as a
:data:`ChainSegment` carrying its ``position``/``chain_size`` within its OWN chain
level (a ``bash -c`` sub-command is its own chain with its own positions)
and ``in_loop`` — True when the segment opens a ``while``/``until``/
``for``/``select`` loop, sits in a ``while``/``until`` CONDITION (every
segment from the opener up to its ``do``), or sits in its ``do``..``done``
body, propagated through ``bash -c`` recursion so a
``while c; do echo x; bash -c '…; sleep 2; …'; done`` marks the INNER
sleep as in-loop. The recursion decision looks through leading compound
keywords (:data:`LEADING_COMPOUND_KEYWORDS`), so ``do bash -c '…'``
recurses exactly like a bare ``bash -c '…'`` (owner ruling 2026-10-15;
before, the keyword hid the sub-command from every consumer). Added for
the wait guard's fixed-sleep rule (a literal ``sleep`` between other
actions is allowed; a sleep in a loop — body or condition — or as the
sole/last/first segment, is not) — position and loop membership are shell
structure, so they live here, not in a consumer's raw-string scan.
Two further opt-in surfaces serve the same consumer: command-substitution
bodies (assignment values included — ``x=$(sleep 5)``) and the
re-positioning of a CALLED function's body within that body
(:func:`function_header`); :func:`walk_command` opts into neither.
Process substitutions (``<(sleep 5)`` / ``>(cat f)``) count as
substitution bodies; an ``eval '<literal>'`` body is re-parsed exactly
like a ``bash -c '<sub>'`` body (:func:`literal_eval_body`), and a
subshell-bodied function (``f() ( sleep 1 )``) is recognised by
:func:`function_header` like a brace-bodied one.
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Callable, List, NamedTuple, Optional, Tuple

# --- Verb / separator vocabulary (shared by every consumer) --------------

# Verbs whose target is the *next* verb (they wrap an underlying command).
# Sudo/nice/taskset/time pass their tail args to the wrapped command;
# `env` does the same but with env-var resetting in between (we treat it
# identically -- the env-var prefix logic also handles the bare `KEY=val
# command` shape, so `env KEY=val command` is just `env` peeling plus
# the env-prefix walker on the remainder).
WRAPPER_VERBS: frozenset[str] = frozenset({
    "sudo",
    "nice",
    "taskset",
    "time",
    "env",
    "ionice",
    "chronic",
    "stdbuf",
    "exec",  # `exec rm foo` re-execs as rm; same target set.
    # `command [-pvV] <cmd>` / `builtin <cmd>` run the next word as the
    # command (v0.2.104 R7 N1: `command eval '…'` hid the eval from the
    # substitution-liveness decision). Their only flags are value-less.
    "command",
    "builtin",
})

# `bash -c "<sub-command>"` / `sh -c "<sub-command>"` -- recognised by
# name, then we peel the optional `-c` and re-parse the next positional
# as a fresh chain.
SHELL_DASH_C_VERBS: frozenset[str] = frozenset({
    "bash",
    "sh",
    "zsh",
    "dash",
    "ksh",
})

# Chain separators. Top-level operators that break command sequences
# into independent segments -- each segment gets its own verb walk.
# Token-equal match (after shlex split with posix=True, these survive
# as standalone tokens because shlex doesn't fold them).
CHAIN_SEPARATORS: frozenset[str] = frozenset({
    ";",
    "&&",
    "||",
    "|",
    "&",  # background -- same semantics for our purposes; the foreground
          # part of `cmd1 & cmd2` is a target-bearing segment.
})

# Max recursion depth for `bash -c "..."` parsing. 4 is generous --
# real Claude-generated commands rarely nest beyond 2, and a deeply-
# nested chain is more likely an attacker testing the parser than a
# legitimate user command.
MAX_NESTING_DEPTH: int = 4

#: Keywords that OPEN a loop whose body runs between ``do`` and ``done``
#: (v0.2.103). Recognised on a segment's FIRST token (before wrapper
#: peeling — a wrapper verb can never precede a shell keyword).
LOOP_OPENERS: frozenset[str] = frozenset({"while", "until", "for", "select"})

#: Compound/loop keywords that may LEAD a segment before its command
#: (``do sleep 2``, ``{ bash -c '…'; }``, ``! cmd``). The ``bash -c``
#: recursion decision looks THROUGH them (v0.2.103, owner ruling
#: 2026-10-15: ``do bash -c '…'`` recurses exactly like a bare
#: ``bash -c '…'`` — previously the keyword hid the sub-command from every
#: consumer). Consumers that classify a segment's verb (the wait guard)
#: strip this same set — import it from here, one home.
LEADING_COMPOUND_KEYWORDS: frozenset[str] = frozenset(
    {"do", "then", "else", "{", "!"})

#: ``while``/``until`` are the loop openers whose CONDITION is a command
#: (``for``/``select`` take a word list, never a command). Every segment
#: from one of these openers up to its ``do`` is marked in-loop (owner
#: ruling 2026-10-15: a wait as the loop condition is a wait in the loop).
CONDITION_OPENERS: frozenset[str] = frozenset({"while", "until"})


def _segment_opener(stripped: List[str]) -> Optional[str]:
    """The loop opener governing a segment: its FIRST token, or an opener
    immediately after a ``{`` / ``(`` group token — the function/group
    header shape ``f() { while …`` (R6, owner ruling 2026-10-15: the loop
    rule applies inside function bodies). Else ``None``."""
    if not stripped:
        return None
    first = os.path.basename(stripped[0]).lower()
    if first in LOOP_OPENERS:
        return first
    for j in range(0, len(stripped) - 1):
        if stripped[j] in ("{", "("):
            cand = os.path.basename(stripped[j + 1]).lower()
            if cand in LOOP_OPENERS:
                return cand
    return None


def strip_subshell_parens(tokens: List[str]) -> List[str]:
    """Peel the grouping punctuation of a subshell segment: leading ``(``
    tokens or a glued leading ``(`` on the first token (``(sleep 5)`` —
    never ``$(``, that is a command substitution), and the matching
    trailing ``)`` tokens / glued suffix. No-op for anything else. The
    walker never rewrites EMITTED tokens; consumers that classify a
    segment's verb (the wait guard) call this — one home for the peel."""
    out = list(tokens)
    opened = False
    while out and out[0] == "(":
        out = out[1:]
        opened = True
    if out and out[0].startswith("(") and not out[0].startswith("$("):
        out[0] = out[0].lstrip("(")
        opened = True
    if opened:
        while out and out[-1] == ")":
            out = out[:-1]
        if out and out[-1].endswith(")"):
            out[-1] = out[-1].rstrip(")")
    return [t for t in out if t]


#: Non-nested command-substitution bodies: ``$(...)`` and backticks.
#: Nesting extracts the INNERMOST body (``$(echo $(sleep 5))`` →
#: ``sleep 5``) — which is where a hidden wait would live. Applied to the
#: whitespace-joined tokens, so a body split by :func:`tokenize`
#: (``$(sleep`` + ``5)``) reassembles.
_SUBST_PAREN_RE = re.compile(r"\$\(([^()]*)\)")
_SUBST_BACKTICK_RE = re.compile(r"`([^`]+)`")
#: Process substitutions (v0.2.103): ``<(sleep 5)`` / ``>(cat f)`` — the
#: body runs as its own command, so it is scanned like a substitution.
_PROC_SUBST_RE = re.compile(r"[<>]\(([^()]*)\)")


def substitution_bodies(tokens: List[str]) -> List[str]:
    """The command-substitution bodies embedded in a segment's tokens
    (``echo $(sleep 5)``, backticks), best-effort; a nested substitution
    yields its INNERMOST body (where a hidden wait would live). Process
    substitutions (``diff <(sleep 5) <(cat f)``) count too (v0.2.103) —
    the body runs as its own command. Known limit: a substitution
    containing a top-level chain separator (``$(foo; sleep 1)``) is split
    by :func:`split_chain` BEFORE this runs — the inner segments then
    stand on their own (the sleep is judged by the plain position rule)."""
    text = " ".join(tokens)
    if "$(" not in text and "`" not in text and "<(" not in text and ">(" not in text:
        return []
    return (
        [m.group(1) for m in _SUBST_PAREN_RE.finditer(text)]
        + [m.group(1) for m in _SUBST_BACKTICK_RE.finditer(text)]
        + [m.group(1) for m in _PROC_SUBST_RE.finditer(text)]
    )


#: Literal-opener neutralisation (v0.2.104 E-4 / R7 N1). The substitution
#: scan (:func:`substitution_bodies`) reads tokens AFTER ``shlex`` removed
#: the quotes, so ``grep -n '$(sleep 5)' f`` — a LITERAL pattern — read
#: exactly like ``echo $(sleep 5)``. Bash runs a substitution only outside
#: single quotes / ``$'…'`` and when its opener is not escaped; inside
#: double quotes ``$(…)`` and backticks run but ``<(`` / ``>(`` do not. The
#: ONE quote-aware scanner (:func:`_newlines_as_separators`) therefore
#: swaps, in those literal regions only, each character that could open or
#: close a substitution for a private-use placeholder — so the regexes
#: cannot match there — and :func:`walk_segments` swaps them back on every
#: token it emits or re-parses. The liveness decision is made AT THE
#: CHARACTER, never by comparing normalised text: the previous design
#: (a set of live body texts) dropped every live body whose text differed
#: from its token-side shape (``$(sleep '5')``, ``$(sleep \\5)``, an
#: ``eval`` argument behind a wrapper — R7 N1).
_LITERAL_OPENER_PLACEHOLDERS: dict = {
    "$": "\ue000",
    "`": "\ue001",
    "(": "\ue002",
    ")": "\ue003",
    "<": "\ue004",
    ">": "\ue005",
}
_RESTORE_TABLE = str.maketrans(
    {v: k for k, v in _LITERAL_OPENER_PLACEHOLDERS.items()})
_PLACEHOLDER_RE = re.compile("[" + "".join(_LITERAL_OPENER_PLACEHOLDERS.values()) + "]")
#: What is neutralised where. Single-quoted / ANSI-C text: everything that
#: forms ``$(``, a backtick pair or ``<(``/``>(`` is literal there — the
#: parens and backticks suffice (``$`` and ``<``/``>`` alone open nothing).
#: Double-quoted text: ``$(`` and backticks RUN, so the parens of a live
#: ``$(…)`` (nesting tracked) stay; every other paren, an escaped ``\$`` /
#: ``\``` and a process-substitution ``<``/``>`` before ``(`` are literal.
#: An escaped character outside quotes is literal.
_LITERAL_IN_SINGLE = "`()"
_LITERAL_IN_DOUBLE_ESCAPED = "$`"
_LITERAL_IN_DOUBLE_PARENS = "()"
_LITERAL_ESCAPED = "$`()<>"


def _neutralise(text: str, chars: str) -> str:
    """``text`` with every character in ``chars`` swapped for its
    placeholder (characters outside ``chars`` untouched)."""
    return "".join(_LITERAL_OPENER_PLACEHOLDERS[c] if c in chars else c
                   for c in text)


def restore_literal_openers(text: str) -> str:
    """``text`` with every literal-opener placeholder swapped back to the
    character it stood for — the exact bytes :func:`tokenize` would have
    produced without the neutralisation (a no-op on text without
    placeholders)."""
    return text.translate(_RESTORE_TABLE)


def _restore_tokens(tokens: List[str]) -> List[str]:
    return [restore_literal_openers(t) for t in tokens]


def _without_token(tokens: List[str], value: Optional[str],
                   restore: Callable[[str], str]) -> List[str]:
    """``tokens`` minus the FIRST token whose restored text is ``value``
    (``None`` → unchanged)."""
    if value is None:
        return tokens
    for i, t in enumerate(tokens):
        if restore(t) == value:
            return tokens[:i] + tokens[i + 1:]
    return tokens


class ChainSegment(NamedTuple):
    """One peeled chain segment plus its structural context (v0.2.103).

    ``tokens``   — the peeled segment (what :func:`walk_command`'s
                   collectors have always received).
    ``position`` / ``chain_size`` — the segment's raw position within its OWN
                   chain level (a ``bash -c`` sub-command is a fresh chain
                   with its own positions; the ``bash -c`` segment itself
                   counts at the outer level even though it is recursed
                   rather than emitted).
    ``in_loop``  — True when the segment OPENS a
                   ``while``/``until``/``for``/``select`` loop, sits in
                   the CONDITION of a ``while``/``until`` (every segment
                   from the opener up to its ``do``), or sits between
                   ``do`` and ``done``; inherited through
                   ``bash -c`` recursion, so a sleep inside
                   ``while c; do echo x; bash -c '…; sleep 1; …'; done``
                   is in-loop.
    ``depth``    — the ``bash -c`` recursion depth this chain level sits
                   at (0 = the top-level command).
    ``assignments`` — the statement's leading assignment prefix, each
                   assignment RE-JOINED to the end of its ``$(…)`` /
                   backtick value (``r=$(curl -s u)`` stays ONE string,
                   v0.2.103 R16) — including an assignment-ONLY statement,
                   whose emitted ``tokens`` are then empty. The field is
                   populated for every caller; the assignment-only
                   EMISSION is opt-in (:func:`walk_segments`'
                   ``_with_assignments``) so :func:`walk_command`
                   collectors keep their byte-identical contract.
    """

    tokens: List[str]
    position: int
    chain_size: int
    in_loop: bool
    depth: int
    assignments: Tuple[str, ...] = ()

# A collector receives ONE peeled segment (tokens) and returns whatever
# path-ish strings it found in it.
Collector = Callable[[List[str]], List[str]]


# --- Helpers -------------------------------------------------------------


def is_env_assignment(tok: str) -> bool:
    """True for `KEY=value` shapes that Bash treats as transient env
    assignments BEFORE a command: contains `=`, no leading `-`, LHS is
    alnum_underscore, LHS starts with a letter or underscore (POSIX env
    name rule)."""
    if "=" not in tok or tok.startswith("-"):
        return False
    eq = tok.index("=")
    head = tok[:eq]
    if not head:
        return False
    if not (head[0].isalpha() or head[0] == "_"):
        return False
    return all(c.isalnum() or c == "_" for c in head)


def split_chain(tokens: List[str]) -> List[List[str]]:
    """Split a token list into segments at top-level chain operators.

    Matching is token-equal on :data:`CHAIN_SEPARATORS` — ``shlex.split``
    keeps ``;``, ``&&``, ``||``, ``|``, ``&`` as standalone tokens when
    they appear SURROUNDED BY SPACES in the source. Glued UNQUOTED tails
    (``do tail log; sleep 5; done`` keeps ``log;`` as one shlex token)
    are peeled at :func:`tokenize` time, NOT here: quote information is
    gone by the time tokens reach this function, so a QUOTED tail
    (``echo "done;"`` — the ``;`` is data, and the real shell would not
    split there) is indistinguishable from a glued separator at this
    level (v0.2.102 W4-D LOW 2). ``|`` / ``||`` glued to a word end are
    left alone everywhere: the piped shapes real commands use put spaces
    around them.

    A ``((``-opened arithmetic header is NOT split at its inner ``;``:
    ``for (( i=0 ; i<3 ; i++ ))`` is ONE segment, exactly as the glued
    ``for ((i=0; i<3; i++))`` always was (v0.2.103 R16 A-N3 — before, the
    header's clauses were torn apart and the empty middle of
    ``for (( ; ; ))`` became invisible). The span runs until the paren
    depth opened by ``((`` closes.

    Sub-commands inside `bash -c "..."` survive as ONE string token
    (because the quotes preserved them), so they are NOT split here --
    they're re-parsed by `parse_dash_c` when the walker recognises a
    `bash -c` wrapper.

    Empty segments (e.g. trailing `;`) are dropped to keep downstream
    iteration tight.
    """
    segments: List[List[str]] = []
    current: List[str] = []
    arith_depth = 0
    for tok in tokens:
        if arith_depth > 0:
            current.append(tok)
            arith_depth += tok.count("(") - tok.count(")")
            continue
        if tok in CHAIN_SEPARATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(tok)
        if tok.startswith("((") and tok.count("(") > tok.count(")"):
            arith_depth = tok.count("(") - tok.count(")")
    if current:
        segments.append(current)
    return segments


def _substitution_depth(text: str) -> int:
    """The open ``$(``/``(``/backtick depth of ``text`` — how much more
    closing is needed before the value is complete. Backticks count in
    pairs (``\\`cat`` opens, ``f\\``` closes)."""
    depth = text.count("(") - text.count(")")
    if text.count("`") % 2 == 1:
        depth += 1
    return depth


def split_assignment_prefix(tokens: List[str]) -> Tuple[List[str], List[str]]:
    """``(assignments, rest)``: the statement's leading assignment prefix,
    each assignment RE-JOINED to the end of its own value. shlex splits
    ``r=$(curl -s u)`` into ``r=$(curl`` + ``-s`` + ``u)``; the prefix
    token alone is not the assignment, so tokens keep being consumed
    while the value's ``$(``/backtick depth is open. An unterminated
    value swallows the rest of the statement (it is still one
    assignment). The ONE home for this shape (v0.2.103 R16): the wait
    guard's poll/work classifiers read the re-joined assignment
    directly instead of guessing from a torn token."""
    assignments: List[str] = []
    rest = list(tokens)
    while rest and is_env_assignment(rest[0]):
        text = rest[0]
        i = 1
        while _substitution_depth(text) > 0 and i < len(rest):
            text += " " + rest[i]
            i += 1
        assignments.append(text)
        rest = rest[i:]
    return assignments, rest


#: A token with a glued trailing ``;`` / ``&`` run (``log;``, ``foo&&``).
#: The word may be empty (a leading separator); the separator run is
#: re-emitted as the standalone token(s) :data:`CHAIN_SEPARATORS` knows.
#: Applied by :func:`tokenize` only to tokens whose tail was NOT quoted
#: (posix=False shadow pass) — a quoted tail is data, not a separator.
_GLUED_RE = re.compile(r"^(?P<word>.*?)(?P<sep>[;&][;&]*)$")


def _split_glued(token: str) -> List[str]:
    """``['log;', 'sleep']`` → ``['log', ';', 'sleep']`` (separator runs of
    two identical characters stay one token — ``foo&&`` → ``foo``, ``&&``).
    Tokens without a glued trailing ``;`` / ``&`` pass through untouched."""
    if len(token) <= 1 or token in CHAIN_SEPARATORS:
        return [token]
    m = _GLUED_RE.match(token)
    if m is None:
        return [token]
    out: List[str] = []
    word = m.group("word")
    if word:
        out.append(word)
    seps = m.group("sep")
    i = 0
    while i < len(seps):
        if seps[i : i + 2] == "&&":
            out.append("&&")
            i += 2
            continue
        out.append(seps[i])
        i += 1
    return out


def strip_env_assignments(tokens: List[str]) -> List[str]:
    """Drop leading `KEY=VAL` env assignments and return the remainder.
    Returns an empty list if every token is an env assignment (malformed,
    but graceful)."""
    i = 0
    while i < len(tokens) and is_env_assignment(tokens[i]):
        i += 1
    return tokens[i:]


def peel_wrapper_verbs(tokens: List[str]) -> List[str]:
    """Strip leading wrapper verbs (``sudo``, ``nice -n 10``, ``time``,
    ``env``, etc.) until we land on a non-wrapper verb.

    Wrapper verbs may carry their own flags + numeric args (``nice -n
    10``, ``taskset 1``, ``ionice -c 2``). We skip leading flags
    (``-...``) AND a single numeric positional argument that some
    wrappers take. Stops as soon as the next non-flag, non-numeric
    token is a known non-wrapper.

    Returns the tokens FROM the first non-wrapper verb onward. Returns
    an empty list if every token is a wrapper or flag (malformed input).
    """
    out = list(tokens)
    safety = 0  # avoid infinite loop on pathological input
    while out and safety < 16:
        safety += 1
        verb_basename = os.path.basename(out[0]).lower()
        if verb_basename not in WRAPPER_VERBS:
            return out
        # Peel the wrapper. Walk forward, skipping flags + a single
        # numeric arg (for `nice -n 10`, `taskset 1`, etc.).
        i = 1
        while i < len(out):
            t = out[i]
            if t.startswith("-"):
                # Flag -- skip. Some flags take values (e.g. `-n 10` for
                # nice), but consuming flag-and-its-value would require
                # per-wrapper knowledge we don't have. The next token
                # (the value) is numeric -> also skipped by the
                # `isdigit()` branch below; if it's not numeric, we
                # stop here and let the next loop iteration re-examine
                # it as a candidate verb.
                i += 1
                continue
            # Numeric positional (e.g. taskset's CPU mask, nice's
            # priority value). Skip ONE numeric token, then break.
            if t.lstrip("-").isdigit():
                i += 1
                continue
            # Env-style assignment between wrapper-flags and the verb
            # (e.g. `sudo -E ENV=val command`). Skip it too.
            if is_env_assignment(t):
                i += 1
                continue
            break
        out = out[i:]
    return out


def _quoted_token_views(command: str) -> Optional[List[str]]:
    """Posix=False shadow tokenization of `command`: same whitespace
    boundaries :func:`tokenize` produces, but with quote characters
    RETAINED — so a token whose tail was quoted still ENDS in ``'`` or
    ``"`` here, which is exactly the information the posix=True split
    destroys.

    Returns None when the shadow pass fails; callers then fall back to
    the peel-everything semantics rather than guess.
    """
    try:
        lex = shlex.shlex(command, posix=False)
        lex.whitespace_split = True
        lex.commenters = ""  # mirror shlex.split(comments=False)
        return list(lex)
    except ValueError:
        return None


def _tail_quoted(raw_token: str) -> bool:
    """True when a posix=False raw token ends in a closing quote — i.e.
    the corresponding posix=True token's tail was QUOTED, so a trailing
    ``;`` / ``&`` in it is data, not a chain separator."""
    return raw_token.endswith(("'", '"'))


def _backslash_run_before(command: str, i: int) -> int:
    """The number of consecutive backslashes immediately before position
    ``i`` — an ODD run means the character at ``i`` is escaped."""
    j = i
    while j > 0 and command[j - 1] == "\\":
        j -= 1
    return i - j


def _ansi_c_body(command: str, start: int) -> Tuple[str, int]:
    """``(decoded_body, end_index)`` of the ``$'…'`` ANSI-C string whose
    opening quote is at ``start``. Escapes are decoded exactly as far as
    tokenisation needs (``\\'`` and ``\\\\``; other ``\\X`` stay two
    characters — the walk never inspects ANSI-C escapes beyond quotes);
    an unterminated string consumes the rest."""
    out: List[str] = []
    i = start
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and i + 1 < n and command[i + 1] in ("'", "\\"):
            out.append(command[i + 1])
            i += 2
            continue
        if ch == "'" and _backslash_run_before(command, i) % 2 == 0:
            return "".join(out), i
        out.append(ch)
        i += 1
    return "".join(out), n


def _double_quoted_literal(body: str) -> str:
    """``body`` as one shlex-parseable double-quoted token."""
    escaped = (body.replace("\\", "\\\\").replace('"', '\\"')
               .replace("$", "\\$").replace("`", "\\`"))
    return '"' + escaped + '"'


def _newlines_as_separators(command: str, *,
                            literal_openers: bool = False) -> str:
    """An UNQUOTED newline is a command separator, exactly like ``;``
    (v0.2.103): ``make\\nsleep 5`` is judged like ``make; sleep 5``, and a
    multi-line loop/condition like its one-line form. A newline inside a
    single/double-quoted string is data; an unquoted ``\\<newline>`` is a
    line continuation and is DELETED, both characters (bash removes it;
    keeping it made shlex fold the newline into the next token). An
    unquoted ``#`` at word start starts a comment that runs to the end of
    the line (shlex is called with comments disabled, so this is the one
    place comments are stripped — a ``# it's fine`` line must not open a
    quote). ``$'…'`` (ANSI-C quoting, escape-aware) is re-emitted as a
    plain quoted token — shlex does not know the form, so an apostrophe
    inside one would look like an unbalanced quote. A closing quote is
    real only when the backslash run before it is EVEN (``\\\\"`` is a
    backslash then a CLOSE, ``\\\\"`` … an odd run escapes it). Quote-aware
    character scan — the one home for this rule (every :func:`tokenize`
    consumer, including the wait guard, gets it).

    ``literal_openers`` (v0.2.104 E-4): swap the substitution-opener
    characters of every LITERAL region (single quotes, ``$'…'``, escaped
    characters, the escaped ``\\$`` / ``\\``` and ``<(``/``>(`` inside
    double quotes) for their placeholders
    (:data:`_LITERAL_OPENER_PLACEHOLDERS`) — the output tokenises exactly
    as before, but a ``$(…)`` bash would not run can no longer be read as
    a substitution. Off by default: every other consumer sees the same
    bytes it always has."""
    out: List[str] = []
    quote: Optional[str] = None
    dq_subst = 0  # open live `$(` depth inside the current double quotes
    i = 0
    n = len(command)
    word_start = True
    while i < n:
        ch = command[i]
        if quote == "'":
            out.append(_LITERAL_OPENER_PLACEHOLDERS[ch]
                       if literal_openers and ch in _LITERAL_IN_SINGLE else ch)
            if ch == "'":
                quote = None
        elif quote == '"':
            if ch == "\\" and i + 1 < n and command[i + 1] in '\\$`"':
                # An escape inside double quotes: the pair is one unit
                # (an escaped backslash before `$(` leaves that `$(` live),
                # and an escaped `$` or backtick is literal.
                nxt = command[i + 1]
                out.append(ch)
                out.append(_LITERAL_OPENER_PLACEHOLDERS[nxt]
                           if literal_openers and nxt in _LITERAL_IN_DOUBLE_ESCAPED
                           else nxt)
                i += 1
            elif (literal_openers and ch in "<>" and i + 1 < n
                  and command[i + 1] == "("):
                # No process substitution inside double quotes.
                out.append(_LITERAL_OPENER_PLACEHOLDERS[ch])
            elif literal_openers and ch == "$" and i + 1 < n and command[i + 1] == "(":
                out.append("$(")  # a live `$(…)` (also `$((…))`): its parens stay
                dq_subst += 1
                i += 1
            elif literal_openers and ch in _LITERAL_IN_DOUBLE_PARENS:
                if dq_subst > 0:
                    out.append(ch)
                    dq_subst += 1 if ch == "(" else -1
                else:
                    out.append(_LITERAL_OPENER_PLACEHOLDERS[ch])
            else:
                out.append(ch)
                if ch == '"' and _backslash_run_before(command, i) % 2 == 0:
                    quote = None
                    dq_subst = 0
        elif ch == "#" and word_start:
            while i < n and command[i] != "\n":
                i += 1
            continue  # the newline (if any) is handled on the next pass
        elif ch == "$" and i + 1 < n and command[i + 1] == "'":
            body, end = _ansi_c_body(command, i + 2)
            lit = _double_quoted_literal(body)
            if literal_openers:
                # After the re-emission, so the backslash it puts before a
                # backtick survives the round trip byte for byte.
                lit = _neutralise(lit, _LITERAL_IN_SINGLE)
            out.append(lit)
            i = end
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "\\" and i + 1 < n and command[i + 1] == "\n":
            i += 1  # line continuation: consume the newline too (both vanish)
        elif ch == "\\" and i + 1 < n:
            out.append(ch)
            i += 1
            nxt = command[i]
            out.append(_LITERAL_OPENER_PLACEHOLDERS[nxt]
                       if literal_openers and nxt in _LITERAL_ESCAPED else nxt)
        elif ch == "\n":
            out.append(" ; ")
        else:
            out.append(ch)
        word_start = ch.isspace() or ch in ";|&()<>{"
        i += 1
    return "".join(out)


def tokenize(command: str, *, literal_openers: bool = False) -> Optional[List[str]]:
    """`shlex.split` the command, returning None on malformed quoting.

    An unquoted newline is rewritten to ``;`` FIRST (one home — see
    :func:`_newlines_as_separators`), so a multi-line command splits into
    the same segments its ``;``-joined form produces.

    Glued UNQUOTED separator tails (``log;``) are peeled HERE, not in
    :func:`split_chain` (v0.2.102 W4-D LOW 2): quote information is
    already gone once ``shlex.split`` returns, so the peel is driven by
    a :func:`_quoted_token_views` shadow pass that keeps the quotes —
    ``log;`` → ``log``, ``;`` while ``echo "done;"`` keeps ``done;`` as
    ONE token. When the two tokenizations do not align token-for-token
    (pathological shapes such as mid-word quotes or ``$(...)``), the
    pre-v0.2.102 peel-everything semantics apply as a conservative
    fallback.

    Callers treat None as "unparseable -- collect nothing", which keeps
    every consumer's soft-fail contract identical.

    ``literal_openers`` (v0.2.104 E-4) is the opt-in described at
    :func:`_newlines_as_separators`: the tokens carry placeholders where a
    quoted or escaped substitution opener stood, and the caller swaps them
    back with :func:`restore_literal_openers` before handing a token on.
    """
    prepared = _newlines_as_separators(command, literal_openers=literal_openers)
    try:
        tokens = shlex.split(prepared, posix=True)
    except ValueError:
        return None
    raws = _quoted_token_views(prepared)
    if raws is None or len(raws) != len(tokens):
        return [piece for tok in tokens for piece in _split_glued(tok)]
    return [
        piece
        for tok, raw in zip(tokens, raws)
        for piece in ((tok,) if _tail_quoted(raw) else _split_glued(tok))
    ]


def dash_c_subcommand(tokens: List[str]) -> Optional[str]:
    """The ``<sub-command>`` of a ``<shell> -c <sub>`` shape, else None.

    Peels the optional ``-c`` (or a combined flag whose last letter is
    ``c``, like ``-ec``) and returns the next positional. The ONE home for
    this shape — :func:`walk_segments`, :func:`parse_dash_c` and the wait
    guard's loop-condition scan all use it."""
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-c" and i + 1 < len(tokens):
            return tokens[i + 1]
        # Combined flag like `-ec` / `-xc` -- last char is `c`, so the
        # next positional is the sub-cmd.
        if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
            if i + 1 < len(tokens):
                return tokens[i + 1]
        i += 1
    return None


#: Characters that make an ``eval`` body NON-literal — the string is not a
#: fixed command, so re-parsing it would judge something that never runs
#: as written (``eval "$cmd"``, ``eval 'sleep $N'``).
_EVAL_NONLITERAL_CHARS = ("$", "`")


def literal_eval_body(tokens: List[str]) -> Optional[str]:
    """The literal string body of ``eval '<cmd>'`` / ``eval "<cmd>"``
    (v0.2.103), else ``None``. Only the single-argument form whose body
    contains no expansions (``$`` / backtick) counts: ``eval 'sleep 5'``
    → ``sleep 5``, while ``eval "$cmd"`` / ``eval 'sleep $N'`` / the
    multi-token ``eval sleep 5`` stay as they are (never re-parsed). The
    ONE home for this shape — :func:`walk_segments`' recursion decision
    and the wait guard's condition/wrapper scans use it."""
    if len(tokens) == 2 and not any(c in tokens[1] for c in _EVAL_NONLITERAL_CHARS):
        return tokens[1]
    return None


def parse_dash_c(tokens: List[str], depth: int, collect: Collector) -> List[str]:
    """If `tokens` is shaped like `bash -c "<sub-cmd>"`, re-parse
    `<sub-cmd>` as a fresh chain with the same collector.

    Returns an empty list if the shape doesn't match, the sub-cmd is
    malformed, or the depth budget is exhausted.
    """
    sub_cmd = dash_c_subcommand(tokens)
    if sub_cmd is None:
        return []
    # Recurse with depth-budget so attacker-crafted nested `bash -c`
    # can't trigger unbounded recursion.
    if depth + 1 > MAX_NESTING_DEPTH:
        return []
    return walk_command(sub_cmd, collect, depth=depth + 1)


#: A shell function NAME (bash accepts more than POSIX; this is the
#: practical subset) — with an optional glued ``()`` and/or ``{`` / ``(``.
_FN_NAME = r"[A-Za-z_][A-Za-z0-9_.:-]*"
_FN_PAREN_RE = re.compile(r"^(" + _FN_NAME + r")\(\)(\{)?$")
_FN_KEYWORD_NAME_RE = re.compile(r"^(" + _FN_NAME + r")(\(\))?(\{)?$")
#: ``f()(`` — name, parameter parens and the subshell-body paren glued
#: into one token.
_FN_PAREN_OPEN_GLUED_RE = re.compile(r"^(" + _FN_NAME + r")\(\)\($")


def function_header(tokens: List[str]) -> Optional[tuple]:
    """``(name, header_len)`` when ``tokens`` OPENS a function definition
    — brace-bodied (``f() {``, ``f(){``, ``f () {``, ``function f {``,
    ``function f() {``) or subshell-bodied (``f() ( sleep 1 )``,
    ``f()(``, ``function f (`` — v0.2.103) — where ``header_len`` counts
    the header tokens up to and including the ``{`` / ``(`` body opener;
    else ``None`` (v0.2.103, owner ruling 2026-10-15: a function body's
    waits are judged by the position rule WITHIN the body). The body KIND
    is read by the caller from ``tokens[header_len - 1]`` (``{`` brace,
    ``(`` subshell) — one home for both."""
    if not tokens:
        return None
    if tokens[0] == "function" and len(tokens) >= 2:
        m = _FN_KEYWORD_NAME_RE.match(tokens[1])
        if m is None:
            return None
        if m.group(3):
            return m.group(1), 2
        j = 2
        if not m.group(2) and j < len(tokens) and tokens[j] == "()":
            j += 1
        if j < len(tokens) and tokens[j] in ("{", "("):
            return m.group(1), j + 1
        return None
    m = _FN_PAREN_RE.match(tokens[0])
    if m is not None:
        if m.group(2):
            return m.group(1), 1
        if len(tokens) > 1 and tokens[1] in ("{", "("):
            return m.group(1), 2
        return None
    g = _FN_PAREN_OPEN_GLUED_RE.match(tokens[0])
    if g is not None:
        return g.group(1), 1
    if (len(tokens) >= 3 and re.fullmatch(_FN_NAME, tokens[0])
            and tokens[1] == "()" and tokens[2] in ("{", "(")):
        return tokens[0], 3
    return None


def _loop_membership(segments: List[List[str]], inherited: bool) -> List[bool]:
    """Per-segment loop membership for ONE chain level (pure bookkeeping).

    A ``while``/``until``/``for``/``select`` opener (also right after a
    ``{``/``(`` group token — ``f() { while …``), every segment of a
    ``while``/``until`` CONDITION up to its ``do``, and everything from the
    ``do`` up to the matching ``done`` is in-loop, nested loops included;
    ``inherited`` is the context of an enclosing recursion. Reads the FIRST
    token before wrapper peeling: a wrapper verb never precedes a shell
    keyword. The ``done`` decrement happens BEFORE that segment's flag is
    computed, so the terminator itself is not body."""
    flags: List[bool] = []
    loop_depth = 0
    in_condition = False  # between a while/until opener and its `do`
    for segment in segments:
        stripped = strip_env_assignments(segment)
        if not stripped:
            flags.append(inherited or loop_depth > 0 or in_condition)
            continue
        opener = _segment_opener(stripped)
        first = os.path.basename(stripped[0]).lower()
        is_do = first == "do"
        if first == "done" and loop_depth > 0:
            loop_depth -= 1
        flags.append(inherited or loop_depth > 0 or opener is not None
                     or is_do or in_condition)
        if opener in CONDITION_OPENERS:
            in_condition = True
        if is_do:
            loop_depth += 1
            in_condition = False
    return flags


def _strip_command_prefix(tokens: List[str]) -> List[str]:
    """Env assignments, wrapper verbs, leading compound keywords and
    subshell parens peeled — what is left starts with the command word."""
    rec = peel_wrapper_verbs(strip_env_assignments(tokens))
    while rec and os.path.basename(rec[0]).lower() in LEADING_COMPOUND_KEYWORDS:
        rec = rec[1:]
    return strip_subshell_parens(rec)


def _row_invoked_names(tokens: List[str]) -> List[str]:
    """The function NAMES this row invokes as its command word — directly
    or as the command word of one of its substitution bodies (``x=$(f)``).
    Only name-shaped words are reported (a header's ``f()`` verb is not a
    name), so the layout's invocation index stays small."""
    names: List[str] = []
    cmd = _strip_command_prefix(tokens)
    if cmd and re.fullmatch(_FN_NAME, cmd[0]):
        names.append(cmd[0])
    for body in substitution_bodies(tokens):
        sub = tokenize(body)
        if sub:
            for s in split_chain(sub):
                c = _strip_command_prefix(s)
                if c and re.fullmatch(_FN_NAME, c[0]):
                    names.append(c[0])
    return names


#: One layout row: (tokens, position, chain_size, in_loop).
_Row = tuple


def _function_body_layout(rows: List[_Row]) -> List[_Row]:
    """Re-position the body of every function (brace- OR subshell-bodied)
    that is CALLED later in the same chain level (v0.2.103, owner ruling
    2026-10-15).

    The body's segments — the header segment's tail after ``{`` / ``(``
    plus every segment up to the matching ``}`` / ``)`` — get positions
    WITHIN the body, so the position rule judges a wait by where it sits
    in the function: sole, first or last inside the body is a plain wait
    once the function runs; between actions inside the body is the
    sanctioned pause. A call site in a loop marks the whole body in-loop
    (the body runs per iteration).

    A definition that is never called runs nothing: its rows stay exactly
    as the plain chain walk laid them out (positions at the outer level,
    header token prefix kept), so a definition-only command is judged as
    before. No matching closer (an unterminated body) also leaves the
    rows untouched.

    Performance (v0.2.103): call sites are looked up through an invocation
    index built in ONE pass up front (name → the entry indices of the rows
    that invoke it, with their loop flags) — the per-definition scan of the
    whole tail was quadratic (1500 function definitions walked in ~2 s;
    now linear-ish). Indices are ENTRY indices: every splice replaces its
    range with the same rows minus the header prefix and deletes exactly
    the closer row before everything examined later, so
    ``entry_index = current_index + deleted`` stays true left to right,
    and a spliced header row's new (tail) command word is re-indexed
    under its original index."""
    out = list(rows)
    invocations: dict = {}
    for idx, r in enumerate(out):
        for name in _row_invoked_names(r[0]):
            invocations.setdefault(name, []).append((idx, r[3]))
    deleted = 0
    h = 0
    while h < len(out):
        header = function_header(out[h][0])
        if header is None:
            h += 1
            continue
        name, hlen = header
        header_toks = out[h][0]
        # Body kind from the header's LAST token: every recognised opener
        # ends in `{` (brace) or `(` (subshell, also glued as `f()(`).
        subshell_body = header_toks[hlen - 1].endswith("(")
        tail = header_toks[hlen:]
        body: List[tuple] = []
        closer: Optional[int] = None
        if not subshell_body:
            depth = 1 + tail.count("{")
            for k in range(h + 1, len(out)):
                toks = out[k][0]
                if toks and toks[0] == "}":
                    depth -= 1
                    if depth == 0:
                        closer = k
                        break
                depth += toks.count("{")
            if closer is None:
                h += 1
                continue
            if tail:
                body.append((tail, out[h][3]))
            body.extend((r[0], r[3]) for r in out[h + 1:closer])
        else:
            # Subshell body: scan pieces until the paren depth closes; the
            # piece that closes is trimmed at the closing ``)`` and the
            # closer row is consumed entirely (unlike a brace closer, the
            # ``)`` never stands as its own segment).
            depth = 1
            k = h
            piece = tail
            while k < len(out):
                trimmed: List[str] = []
                for tok in piece:
                    if depth == 0:
                        break
                    opens = tok.count("(")
                    closes = tok.count(")")
                    if closes == 0 or depth + opens - closes > 0:
                        trimmed.append(tok)
                        depth += opens - closes
                    else:
                        keep: List[str] = []
                        d = depth
                        for ch in tok:
                            if ch == "(":
                                d += 1
                            elif ch == ")":
                                d -= 1
                                if d == 0:
                                    break
                            keep.append(ch)
                        if keep:
                            trimmed.append("".join(keep))
                        depth = 0
                body.append((trimmed, out[k][3]))
                if depth == 0:
                    closer = k
                    break
                k += 1
                piece = out[k][0] if k < len(out) else []
            if closer is None:
                h += 1
                continue
        closer_entry = closer + deleted
        calls = [e for e in invocations.get(name, ()) if e[0] > closer_entry]
        if not calls:
            h = closer + 1  # never runs: leave the definition untouched
            continue
        called_in_loop = any(flag for _i, flag in calls)
        kept_body = [(toks, fl) for toks, fl in body if toks]
        size = len(kept_body)
        new_rows = [(toks, j, size, fl or called_in_loop)
                    for j, (toks, fl) in enumerate(kept_body)]
        end = closer if not subshell_body else closer + 1
        out[h:end] = new_rows
        deleted += (end - h) - len(new_rows)
        if new_rows and new_rows[0][0] != header_toks:
            # The repositioned header row lost its prefix, so its command
            # word may have become a CALL of another function — re-index.
            for n2 in _row_invoked_names(new_rows[0][0]):
                invocations.setdefault(n2, []).append(
                    (h + deleted, new_rows[0][3]))
        # Re-scan from h: the tail may itself open a nested definition.
    return out


def walk_segments(command: str, depth: int = 0, *,
                  _in_loop: bool = False,
                  _position_override: Optional[tuple] = None,
                  _with_substitutions: bool = False,
                  _with_function_bodies: bool = False,
                  _with_assignments: bool = False) -> List[ChainSegment]:
    """Parse `command` like :func:`walk_command` but hand out
    :data:`ChainSegment` records — peeled tokens PLUS the structural
    context (position within the chain level, loop membership, recursion
    depth) that position/loop-aware consumers need (v0.2.103: the wait
    guard's fixed-sleep rule).

    Loop membership is tracked per chain level by :func:`_loop_membership`;
    the context is INHERITED by a ``bash -c`` recursion (``_in_loop``),
    while the sub-command's positions restart at its own level. The
    recursion decision looks through :data:`LEADING_COMPOUND_KEYWORDS` and
    subshell parens (``do bash -c '…'``, ``( bash -c '…' )`` recurse); the
    EMITTED tokens keep the keyword/paren prefix, exactly as
    :func:`walk_command` collectors have always received them. ``depth``
    and the ``MAX_NESTING_DEPTH`` budget behave exactly as in
    :func:`walk_command`.

    ``_position_override`` (R6, owner 2026-10-15): a recursed sub-chain
    that is the outer segment's WHOLE content inherits that segment's
    position — the recursion passes the current segment's effective
    ``(position, chain_size)``, and a chain with exactly ONE segment
    stamps it with the override. So ``x && bash -c 'sleep 3' && y`` reads
    the inner sleep as BETWEEN actions (the bash -c is segment 1 of 3),
    while a multi-segment sub-chain keeps its own positions.

    ``_with_substitutions`` (R6; opt-in — :func:`walk_command` callers are
    unchanged): the command-substitution bodies (:func:`substitution_bodies`)
    of each segment are walked as sub-chains at depth+1 (inheriting the loop
    context, NOT the position — a sole wait inside ``$(…)`` is a sole
    wait). The scan covers the segment's env-assignment and wrapper prefix
    too, so ``x=$(sleep 5)`` / ``export x=$(sleep 5)`` / ``sudo -E
    X=$(sleep 1) cmd`` are walked even though the assignment itself is
    never emitted as a command.

    ``_with_function_bodies`` (opt-in; :func:`_function_body_layout`): the
    body of a brace-bodied function that is called later in the same chain
    level is re-positioned WITHIN the body, and its header segment is
    emitted as the tail after ``{`` (``f() { sleep 1; }; f`` emits
    ``sleep 1`` as the body's sole segment).

    ``_with_assignments`` (opt-in, v0.2.103 R16; :func:`walk_command`
    callers are unchanged): an assignment-ONLY statement — whose tokens
    the env-prefix strip used to swallow whole, so a multi-line
    ``r=$(curl -s u)`` body statement reached classifiers as its torn
    tail and ``i=$((i+1))`` vanished — is EMITTED with empty ``tokens``
    and its re-joined :data:`ChainSegment.assignments`, so classifiers
    read it directly. Collectors skip empty token lists, and without the
    flag such statements are not emitted at all — the pre-R16 contract.
    """
    if depth > MAX_NESTING_DEPTH:
        return []

    # v0.2.104 E-4: only substitutions bash would RUN are walked — a
    # `'$(sleep 5)'` grep pattern is literal text. The quote-aware scanner
    # swaps every quoted / escaped opener for a placeholder
    # (:data:`_LITERAL_OPENER_PLACEHOLDERS`) so :func:`substitution_bodies`
    # cannot see it, and every token / body that leaves this function is
    # restored first — emitted tokens are byte-identical to the plain
    # walk. The quote reading is trusted ONLY at depth 0, on the command
    # exactly as the outer shell receives it: a `bash -c` body reaches a
    # deeper level as a shlex token whose outer quoting was already removed
    # (imperfectly: a `\$` survives), and a nested shell re-expands it —
    # so every deeper level keeps all its substitutions (the conservative
    # pre-E-4 walk), and so does an `eval` argument at depth 0 (eval
    # re-parses its words). A command that already carries a placeholder
    # character is walked plainly (keep all) — it could not be restored.
    literal = (_with_substitutions and depth == 0
               and _PLACEHOLDER_RE.search(command) is None)
    tokens = tokenize(command, literal_openers=literal)
    if not tokens:
        return []
    # Restore only what THIS walk neutralised: a placeholder character the
    # command itself carried (walked plainly, above) must come back as is.
    _restore: Callable[[List[str]], List[str]] = _restore_tokens if literal else list
    _restore_text: Callable[[str], str] = restore_literal_openers if literal else str

    segments = split_chain(tokens)
    chain_size = len(segments)
    flags = _loop_membership(segments, _in_loop)
    rows: List[_Row] = []
    for i, segment in enumerate(segments):
        # R6: a recursed sub-chain that is the outer segment's whole
        # content inherits that segment's position.
        if _position_override is not None and chain_size == 1:
            pos, size = _position_override
        else:
            pos, size = i, chain_size
        rows.append((segment, pos, size, flags[i]))
    if _with_function_bodies:
        rows = _function_body_layout(rows)

    sub_budget = depth + 1 <= MAX_NESTING_DEPTH
    out: List[ChainSegment] = []
    for segment, pos, size, in_loop in rows:
        if not segment:
            continue
        seg_is_eval = False
        seg_assignments, rest = split_assignment_prefix(segment)
        # Without the opt-in the peeled tokens come from the ORIGINAL
        # env-prefix strip, byte-identical to the pre-R16 walk; with it,
        # a value-spanning assignment consumes its whole tail.
        stripped = rest if _with_assignments else strip_env_assignments(segment)
        peeled = peel_wrapper_verbs(stripped) if stripped else []
        # The env-assignment / wrapper prefix the command word sits behind:
        # never emitted, but its substitutions still run (`x=$(sleep 5)`).
        subst_src = segment[:len(segment) - len(peeled)]
        if peeled:
            # The dash-c decision looks THROUGH leading compound keywords
            # (v0.2.103, owner ruling 2026-10-15): `do bash -c '…'`
            # recurses exactly like a bare `bash -c '…'`. The emitted
            # tokens are unchanged — collectors still receive the
            # keyword-prefixed segment for non-shell verbs. The decision
            # reads the RESTORED tokens: a `-c` / eval body is re-parsed by
            # another shell, so its quoting is not this level's.
            rec = _restore(peeled)
            while rec and (os.path.basename(rec[0]).lower()
                           in LEADING_COMPOUND_KEYWORDS):
                rec = rec[1:]
            rec = strip_subshell_parens(rec)
            rec_verb = os.path.basename(rec[0]).lower() if rec else ""
            seg_is_eval = rec_verb == "eval"
            sub_cmd: Optional[str] = None
            if rec_verb in SHELL_DASH_C_VERBS:
                sub_cmd = dash_c_subcommand(rec)
            elif rec_verb == "eval":
                # `eval '<literal>'` (v0.2.103): the body is re-parsed
                # exactly like a `bash -c` body — same loop-context and
                # position inheritance. A NON-literal body (`eval "$cmd"`)
                # is not re-parsed: the segment is emitted as usual.
                sub_cmd = literal_eval_body(rec)
            if rec_verb in SHELL_DASH_C_VERBS or sub_cmd is not None:
                if sub_cmd is not None and sub_budget:
                    out.extend(walk_segments(
                        sub_cmd, depth + 1, _in_loop=in_loop,
                        _position_override=(pos, size),
                        _with_substitutions=_with_substitutions,
                        _with_function_bodies=_with_function_bodies,
                        _with_assignments=_with_assignments))
                # Consumed either way: no sub-command (`bash script.sh`)
                # or over budget → nothing emitted, exactly as parse_dash_c
                # has always done. Everything else in the segment — the
                # prefix AND the arguments around the sub-command
                # (`bash -c 'x' $(sleep 5)`) — runs in THIS shell, so its
                # substitutions are scanned here (R7 N1: a quote-stripped
                # `-c` body re-parsed at depth 1 carries its inner
                # substitution as a trailing argument).
                subst_src = _without_token(segment, sub_cmd, _restore_text)
            else:
                out.append(ChainSegment(_restore(peeled), pos, size, in_loop,
                                        depth, tuple(_restore(seg_assignments))))
                subst_src = segment
        elif _with_assignments and seg_assignments:
            # An assignment-only statement: emitted with EMPTY tokens
            # (collectors skip those) and its re-joined assignments.
            out.append(ChainSegment([], pos, size, in_loop, depth,
                                    tuple(_restore(seg_assignments))))
        if _with_substitutions and sub_budget:
            # `eval` re-parses its words, so a quoted `$(…)` among them is
            # live after all: its bodies are read from the restored tokens.
            if seg_is_eval:
                subst_src = _restore(subst_src)
            for sub_body in substitution_bodies(subst_src):
                out.extend(walk_segments(
                    _restore_text(sub_body), depth + 1, _in_loop=in_loop,
                    _with_substitutions=True,
                    _with_function_bodies=_with_function_bodies,
                    _with_assignments=_with_assignments))
    return out


def walk_command(command: str, collect: Collector, depth: int = 0) -> List[str]:
    """Top-level entry: parse `command` into chain segments, peel
    wrappers off each, recurse into `bash -c`, and hand every remaining
    peeled segment to `collect`.

    Returns the flat aggregate of everything `collect` returned, in
    source order. Bounded by `depth <= MAX_NESTING_DEPTH`.

    A thin adapter over :func:`walk_segments` (v0.2.103) — the collector
    contract is byte-identical to the pre-ChainSegment walker: same
    peeled segments, same source order, `bash -c` recursion inline.
    """
    out: List[str] = []
    for seg in walk_segments(command, depth=depth):
        out.extend(collect(seg.tokens))
    return out


__all__ = [
    "CHAIN_SEPARATORS",
    "ChainSegment",
    "Collector",
    "CONDITION_OPENERS",
    "LEADING_COMPOUND_KEYWORDS",
    "LOOP_OPENERS",
    "MAX_NESTING_DEPTH",
    "SHELL_DASH_C_VERBS",
    "WRAPPER_VERBS",
    "dash_c_subcommand",
    "function_header",
    "is_env_assignment",
    "literal_eval_body",
    "parse_dash_c",
    "peel_wrapper_verbs",
    "restore_literal_openers",
    "split_chain",
    "strip_env_assignments",
    "strip_subshell_parens",
    "substitution_bodies",
    "tokenize",
    "walk_command",
    "walk_segments",
]
