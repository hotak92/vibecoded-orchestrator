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
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Callable, List, NamedTuple, Optional

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


def substitution_bodies(tokens: List[str]) -> List[str]:
    """The command-substitution bodies embedded in a segment's tokens
    (``echo $(sleep 5)``, backticks), best-effort; a nested substitution
    yields its INNERMOST body (where a hidden wait would live). Known
    limit: a substitution containing a top-level chain separator
    (``$(foo; sleep 1)``) is split by :func:`split_chain` BEFORE this runs
    — the inner segments then stand on their own (the sleep is judged by
    the plain position rule)."""
    text = " ".join(tokens)
    if "$(" not in text and "`" not in text:
        return []
    return (
        [m.group(1) for m in _SUBST_PAREN_RE.finditer(text)]
        + [m.group(1) for m in _SUBST_BACKTICK_RE.finditer(text)]
    )


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
    """

    tokens: List[str]
    position: int
    chain_size: int
    in_loop: bool
    depth: int

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

    Sub-commands inside `bash -c "..."` survive as ONE string token
    (because the quotes preserved them), so they are NOT split here --
    they're re-parsed by `parse_dash_c` when the walker recognises a
    `bash -c` wrapper.

    Empty segments (e.g. trailing `;`) are dropped to keep downstream
    iteration tight.
    """
    segments: List[List[str]] = []
    current: List[str] = []
    for tok in tokens:
        if tok in CHAIN_SEPARATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(tok)
    if current:
        segments.append(current)
    return segments


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


def tokenize(command: str) -> Optional[List[str]]:
    """`shlex.split` the command, returning None on malformed quoting.

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
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    raws = _quoted_token_views(command)
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


def walk_segments(command: str, depth: int = 0, *,
                  _in_loop: bool = False,
                  _position_override: Optional[tuple] = None,
                  _with_substitutions: bool = False) -> List[ChainSegment]:
    """Parse `command` like :func:`walk_command` but hand out
    :data:`ChainSegment` records — peeled tokens PLUS the structural
    context (position within the chain level, loop membership, recursion
    depth) that position/loop-aware consumers need (v0.2.103: the wait
    guard's fixed-sleep rule).

    Loop membership is tracked per chain level: a ``while``/``until``/
    ``for``/``select`` opener (including right after a ``{``/``(`` group
    token — ``f() { while …``), every segment of a ``while``/``until``
    CONDITION (up to its ``do`` — a wait there is loop machinery, owner
    ruling 2026-10-15), and everything from the ``do`` up to the matching
    ``done`` is in-loop, nested loops included; the context is INHERITED
    by a ``bash -c`` recursion (``_in_loop``), while the sub-command's
    positions restart at its own level. The recursion decision looks
    through :data:`LEADING_COMPOUND_KEYWORDS` and subshell parens
    (``do bash -c '…'``, ``( bash -c '…' )`` recurse); the EMITTED tokens
    keep the keyword/paren prefix, exactly as :func:`walk_command`
    collectors have always received them. ``depth`` and the
    ``MAX_NESTING_DEPTH`` budget behave exactly as in
    :func:`walk_command`.

    ``_position_override`` (R6, owner 2026-10-15): a recursed sub-chain
    that is the outer segment's WHOLE content inherits that segment's
    position — the recursion passes the current segment's effective
    ``(position, chain_size)``, and a chain with exactly ONE segment
    stamps it with the override. So ``x && bash -c 'sleep 3' && y`` reads
    the inner sleep as BETWEEN actions (the bash -c is segment 1 of 3),
    while a multi-segment sub-chain keeps its own positions.

    ``_with_substitutions`` (R6; opt-in — :func:`walk_command` callers are
    unchanged): each EMITTED segment's command-substitution bodies
    (:func:`substitution_bodies`) are walked as sub-chains at depth+1
    (inheriting the loop context, NOT the position — a sole wait inside
    ``$(…)`` is a sole wait).
    """
    if depth > MAX_NESTING_DEPTH:
        return []

    tokens = tokenize(command)
    if not tokens:
        return []

    segments = split_chain(tokens)
    chain_size = len(segments)
    out: List[ChainSegment] = []
    loop_depth = 0
    in_condition = False  # between a while/until opener and its `do`
    for i, segment in enumerate(segments):
        if not segment:
            continue
        stripped = strip_env_assignments(segment)
        if not stripped:
            continue
        # Loop bookkeeping reads the FIRST token before wrapper peeling:
        # a wrapper verb can never precede a shell keyword. `do` marks the
        # body (its own tail is body content); `done` ends it — the
        # decrement happens BEFORE this segment's in_loop is computed, so
        # the terminator itself is not body.
        opener = _segment_opener(stripped)
        opens = opener is not None
        is_do = os.path.basename(stripped[0]).lower() == "do"
        is_done = os.path.basename(stripped[0]).lower() == "done"
        if is_done and loop_depth > 0:
            # The terminator ends the body AT this segment: it is not
            # body itself.
            loop_depth -= 1
        in_loop = (_in_loop or loop_depth > 0 or opens or is_do
                   or in_condition)
        # R6: a recursed sub-chain that is the outer segment's whole
        # content inherits that segment's position.
        if _position_override is not None and chain_size == 1:
            pos, size = _position_override
        else:
            pos, size = i, chain_size
        peeled = peel_wrapper_verbs(stripped)
        if peeled:
            # The dash-c decision looks THROUGH leading compound keywords
            # (v0.2.103, owner ruling 2026-10-15): `do bash -c '…'`
            # recurses exactly like a bare `bash -c '…'`. The emitted
            # tokens are unchanged — collectors still receive the
            # keyword-prefixed segment for non-shell verbs.
            rec = peeled
            while rec and (os.path.basename(rec[0]).lower()
                           in LEADING_COMPOUND_KEYWORDS):
                rec = rec[1:]
            rec = strip_subshell_parens(rec)
            if rec and os.path.basename(rec[0]).lower() in SHELL_DASH_C_VERBS:
                sub_cmd = dash_c_subcommand(rec)
                if sub_cmd is not None and depth + 1 <= MAX_NESTING_DEPTH:
                    out.extend(walk_segments(
                        sub_cmd, depth + 1, _in_loop=in_loop,
                        _position_override=(pos, size),
                        _with_substitutions=_with_substitutions))
                # Consumed either way: no `-c` sub-command (e.g.
                # `bash script.sh`) or over budget → nothing emitted,
                # exactly as parse_dash_c has always done.
            else:
                out.append(ChainSegment(peeled, pos, size, in_loop, depth))
                if _with_substitutions and depth + 1 <= MAX_NESTING_DEPTH:
                    for sub_body in substitution_bodies(peeled):
                        out.extend(walk_segments(
                            sub_body, depth + 1, _in_loop=in_loop,
                            _with_substitutions=True))
        if opens and opener in CONDITION_OPENERS:
            in_condition = True
        if is_do:
            loop_depth += 1
            in_condition = False
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
    "is_env_assignment",
    "parse_dash_c",
    "peel_wrapper_verbs",
    "split_chain",
    "strip_env_assignments",
    "strip_subshell_parens",
    "substitution_bodies",
    "tokenize",
    "walk_command",
    "walk_segments",
]
