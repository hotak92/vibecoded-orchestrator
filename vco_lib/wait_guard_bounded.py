# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The wait guard's BOUNDED-TOTAL-WAIT reading of loops inside script
FILES (v0.2.105 fix round, owner ruling 2026-10-29).

Inside a script the guard read every ``sleep`` in a loop as a poll — the
25 s readiness loop of ``rc-native.sh`` and the 20 × 50 ms lock retry of
``vct_project_config.ps1`` alike. The ruling: judge a script loop by its
WORST-CASE TOTAL WAIT. A loop whose iteration count is proved by a LITERAL
bound, whose every wait is a literal duration, and whose iterations ×
waits-per-iteration is at most :data:`WAIT_BUDGET_S` seconds is a bounded
retry, not a poll. Everything else keeps the old reading and is refused:
sampling/polling loops stay banned.

The proof is CONSERVATIVE — whatever this module cannot read exactly is
unbounded:

* Bash: only a COUNTED ``for`` loop is bounded — a literal word list
  (``for x in a b c``), ``$(seq [A [S]] B)``, a brace range ``{A..B[..S]}``
  or an arithmetic ``for ((i=A; i<B; i++))`` whose body never names ``i``
  bare. A ``while``/``until``/``select`` loop is never bounded here: its
  bound depends on the body's control flow, which the flat segment stream
  cannot prove (an ``&&``-guarded or backgrounded increment looks the same
  as an unconditional one).
* A wait inside two nested loops, a non-literal duration (``sleep $n``,
  ``sleep infinity``), a wrapped sleep (``timeout 3 sleep 1``) or a wait in
  a loop condition is never bounded.
* The PowerShell twin (:func:`vco_lib.powershell_command.loop_pattern`'s
  ``wait_budget_s``) also proves a ``while`` COUNTER loop — the lock-retry
  shape — because its token tree carries the statement and block structure
  the Bash stream lacks.

A loop is judged where it is DEFINED: a loop in a function body counts
once, however often the function is called — the same reading the script
rule already gives a function's single pause. The budget never applies
when the script itself runs inside a loop of the calling command (the
caller then multiplies it by an unknown count).
"""
from __future__ import annotations

import os
import re
from typing import FrozenSet, List, Optional, Sequence

from vco_lib.bash_command_walk import (
    ChainSegment,
    command_words,
    segment_opener,
    strip_env_assignments,
)

#: The bounded-total-wait budget (owner ruling 2026-10-29): a loop inside
#: a script whose worst-case total wait is at most this many seconds is a
#: bounded retry. The ONE home of the number — the PowerShell reading gets
#: it passed in.
WAIT_BUDGET_S = 5.0

#: One literal ``sleep`` operand: GNU suffixes s/m/h/d, plus ``ms`` (the
#: PowerShell ``Start-Sleep -Milliseconds`` reading). Same alphabet as the
#: guard's fixed-sleep rule.
_SLEEP_ARG_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(ms|[smhd])?$")
_UNIT_S = {None: 1.0, "s": 1.0, "ms": 0.001, "m": 60.0, "h": 3600.0,
           "d": 86400.0}

_SEQ_RE = re.compile(r"^\$\(\s*seq((?:\s+-?[0-9]+){1,3})\s*\)$")
_BRACE_RANGE_RE = re.compile(r"^\{(-?[0-9]+)\.\.(-?[0-9]+)(?:\.\.(-?[0-9]+))?\}$")
#: A for-list word that expands to exactly ONE word: no ``$``, glob,
#: brace, quote residue or backtick.
_PLAIN_WORD_RE = re.compile(r"^[A-Za-z0-9_./:@%+=,-]+$")
_ARITH_FOR_RE = re.compile(
    r"^\(\(\s*([A-Za-z_]\w*)\s*=\s*(-?[0-9]+)\s*;"
    r"\s*([A-Za-z_]\w*)\s*(<=|<)\s*(-?[0-9]+)\s*;"
    r"\s*(?:([A-Za-z_]\w*)\s*\+\+|\+\+\s*([A-Za-z_]\w*)"
    r"|([A-Za-z_]\w*)\s*\+=\s*1)\s*\)\)$")


def sleep_seconds(args: Sequence[str]) -> Optional[float]:
    """Total seconds of ``sleep ARGS`` (GNU sleep sums its operands), or
    ``None`` when any operand is not a literal duration."""
    if not args:
        return None
    total = 0.0
    for arg in args:
        m = _SLEEP_ARG_RE.match(arg)
        if m is None:
            return None
        total += float(m.group(1)) * _UNIT_S[m.group(2)]
    return total


def _seq_count(nums: List[int]) -> Optional[int]:
    if len(nums) == 1:
        first, step, last = 1, 1, nums[0]
    elif len(nums) == 2:
        first, step, last = nums[0], 1, nums[1]
    else:
        first, step, last = nums
    if step <= 0:
        return None  # a descending or zero step: not read
    return max(0, (last - first) // step + 1)


def for_list_count(words: Sequence[str]) -> Optional[int]:
    """Iterations of ``for x in WORDS`` when every word is literal, else
    ``None``."""
    text = " ".join(words)
    m = _SEQ_RE.match(text)
    if m is not None:
        return _seq_count([int(x) for x in m.group(1).split()])
    count = 0
    for word in words:
        r = _BRACE_RANGE_RE.match(word)
        if r is not None:
            a, b = int(r.group(1)), int(r.group(2))
            step = abs(int(r.group(3))) if r.group(3) else 1
            if step == 0:
                return None
            count += abs(b - a) // step + 1
            continue
        if not _PLAIN_WORD_RE.match(word):
            return None
        count += 1
    return count


def _arith_for(header: str) -> Optional[tuple]:
    """``(var, iterations)`` for ``((i=A; i<B; i++))``, else ``None``."""
    m = _ARITH_FOR_RE.match(header)
    if m is None:
        return None
    var, start, cvar, op, end = (m.group(1), int(m.group(2)), m.group(3),
                                 m.group(4), int(m.group(5)))
    step_var = m.group(6) or m.group(7) or m.group(8)
    if not (var == cvar == step_var):
        return None
    return var, max(0, end - start + (1 if op == "<=" else 0))


class _Loop:
    __slots__ = ("iterations", "var", "in_body", "valid", "sleeps",
                 "per_iteration")

    def __init__(self, iterations: Optional[int], var: Optional[str]):
        self.iterations = iterations
        self.var = var
        self.in_body = False
        self.valid = iterations is not None
        self.sleeps: List[int] = []
        self.per_iteration = 0.0


def _opener_loop(tokens: List[str]) -> Optional[_Loop]:
    """The loop a segment opens, with its proved iteration count (``None``
    count = unbounded), or ``None`` when the segment opens no loop."""
    opener = segment_opener(tokens)
    if opener is None:
        return None
    j = next(k for k, t in enumerate(tokens)
             if os.path.basename(t).lower() == opener)
    rest = tokens[j + 1:]
    if opener != "for" or not rest:
        return _Loop(None, None)
    if rest[0].startswith("(("):
        found = _arith_for(" ".join(rest))
        if found is None:
            return _Loop(None, None)
        var, n = found
        return _Loop(n, var)
    if len(rest) >= 2 and rest[1] == "in":
        return _Loop(for_list_count(rest[2:]), None)
    return _Loop(None, None)  # `for x; do` iterates "$@"


def _names_var_bare(seg: ChainSegment, var: str) -> bool:
    """The segment names ``var`` other than as a ``$var`` / ``${var}``
    expansion — any write (``i=…``, ``((i++))``, ``read i``) included."""
    rx = re.compile(r"(?<![\w${])" + re.escape(var) + r"(?!\w)")
    return any(rx.search(t) for t in list(seg.tokens) + list(seg.assignments))


def bounded_loop_sleeps(segments: Sequence[ChainSegment],
                        budget_s: float = WAIT_BUDGET_S) -> FrozenSet[int]:
    """Indexes (into ``segments``) of the plain ``sleep`` segments that sit
    in a counted ``for`` loop whose worst-case total sleep is at most
    ``budget_s``. ``segments`` is the wait guard's own walk of ONE script
    (:func:`vco_lib.wait_guard.static_pattern`), so an index names the very
    segment the classifier would judge. Fail-closed: any loop structure
    this reading does not track exactly yields the empty set."""
    stack: List[_Loop] = []
    approved: set = set()
    for i, seg in enumerate(segments):
        if seg.depth != 0:
            continue  # substitution / shell-run bodies: judged as before
        tokens = strip_env_assignments(list(seg.tokens))
        first = os.path.basename(tokens[0]).lower() if tokens else ""
        if first == "done":
            if not stack:
                return frozenset()
            loop = stack.pop()
            if (loop.valid and loop.iterations is not None
                    and loop.iterations * loop.per_iteration <= budget_s):
                approved.update(loop.sleeps)
            continue
        opened = _opener_loop(tokens) if tokens else None
        if opened is not None:
            if stack and not stack[-1].in_body:
                stack[-1].valid = False  # a loop in a loop header
            stack.append(opened)
            continue
        if first == "do":
            if not stack or stack[-1].in_body:
                return frozenset()  # a `do` this reading cannot place
            stack[-1].in_body = True
        if not stack:
            continue
        if not seg.in_loop:
            return frozenset()  # the walker disagrees: trust neither
        for outer in stack:
            if outer.var is not None and _names_var_bare(seg, outer.var):
                outer.valid = False
        words = command_words(tokens)
        if not words or os.path.basename(words[0]).lower() != "sleep":
            continue
        inner = stack[-1]
        secs = sleep_seconds(words[1:])
        if len(stack) > 1 or not inner.in_body or secs is None:
            for loop in stack:
                loop.valid = False
            continue
        inner.sleeps.append(i)
        inner.per_iteration += secs
    if stack:
        return frozenset()  # an unclosed loop
    return frozenset(approved)


__all__ = [
    "WAIT_BUDGET_S",
    "bounded_loop_sleeps",
    "for_list_count",
    "sleep_seconds",
]
