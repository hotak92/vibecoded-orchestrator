# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Shared chain-walker for Bash command strings (v0.2.95, lane F10).

ONE home for "split a shell command into independently-walkable
segments". Extracted from ``vco_lib/diagram_delete_parser`` when a
SECOND consumer appeared (``vco_lib.bash_write_targets``, which needs
the same chain / wrapper / env-prefix / ``bash -c`` handling to find
WRITE targets rather than DELETE targets).

Per the project's A>B>C rule the two consumers now share this
implementation instead of mirroring it: a fix to the chain splitter or
the wrapper peeler lands once and both parsers get it.

What the walker does, per command string:

  1. ``shlex.split`` the command (POSIX mode).
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
"""
from __future__ import annotations

import os
import shlex
from typing import Callable, List, Optional

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

    `shlex.split` keeps `;`, `&&`, `||`, `|`, `&` as standalone tokens
    when they appear unquoted in the source. We just iterate and slice
    on those. Sub-commands inside `bash -c "..."` survive as ONE string
    token (because the quotes preserved them), so they are NOT split
    here -- they're re-parsed by `parse_dash_c` when the walker
    recognises a `bash -c` wrapper.

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


def tokenize(command: str) -> Optional[List[str]]:
    """`shlex.split` the command, returning None on malformed quoting.

    Callers treat None as "unparseable -- collect nothing", which keeps
    every consumer's soft-fail contract identical.
    """
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return None


def parse_dash_c(tokens: List[str], depth: int, collect: Collector) -> List[str]:
    """If `tokens` is shaped like `bash -c "<sub-cmd>"`, re-parse
    `<sub-cmd>` as a fresh chain with the same collector.

    Returns an empty list if the shape doesn't match, the sub-cmd is
    malformed, or the depth budget is exhausted.
    """
    sub_cmd: Optional[str] = None
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-c" and i + 1 < len(tokens):
            sub_cmd = tokens[i + 1]
            break
        # Combined flag like `-ec` / `-xc` -- last char is `c`, so the
        # next positional is the sub-cmd.
        if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
            if i + 1 < len(tokens):
                sub_cmd = tokens[i + 1]
                break
        i += 1
    if sub_cmd is None:
        return []
    # Recurse with depth-budget so attacker-crafted nested `bash -c`
    # can't trigger unbounded recursion.
    if depth + 1 > MAX_NESTING_DEPTH:
        return []
    return walk_command(sub_cmd, collect, depth=depth + 1)


def walk_command(command: str, collect: Collector, depth: int = 0) -> List[str]:
    """Top-level entry: parse `command` into chain segments, peel
    wrappers off each, recurse into `bash -c`, and hand every remaining
    peeled segment to `collect`.

    Returns the flat aggregate of everything `collect` returned, in
    source order. Bounded by `depth <= MAX_NESTING_DEPTH`.
    """
    if depth > MAX_NESTING_DEPTH:
        return []

    tokens = tokenize(command)
    if not tokens:
        return []

    out: List[str] = []
    for segment in split_chain(tokens):
        if not segment:
            continue
        stripped = strip_env_assignments(segment)
        if not stripped:
            continue
        peeled = peel_wrapper_verbs(stripped)
        if not peeled:
            continue
        verb_basename = os.path.basename(peeled[0]).lower()
        if verb_basename in SHELL_DASH_C_VERBS:
            out.extend(parse_dash_c(peeled, depth, collect))
            continue
        out.extend(collect(peeled))
    return out


__all__ = [
    "CHAIN_SEPARATORS",
    "Collector",
    "MAX_NESTING_DEPTH",
    "SHELL_DASH_C_VERBS",
    "WRAPPER_VERBS",
    "is_env_assignment",
    "parse_dash_c",
    "peel_wrapper_verbs",
    "split_chain",
    "strip_env_assignments",
    "tokenize",
    "walk_command",
]
