# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The wait-loop / polling guard (v0.2.102 lane W4-D, owner 2026-10-08).

ONE home for every decision about agents "waiting" by polling: foreground
``sleep`` waits, ``watch`` / ``tail -f`` / ``timeout ... bash -c
'...sleep...'`` polling shapes, loops that wait, self-scheduling tools
(``ScheduleWakeup`` / ``CronCreate``), and repeated identical
status checks (``tail``/``head``/``cat``/``ls``/``wc``/``grep`` on the same
target, ``Read`` of the same file_path+offset+limit). Every poll is a new
request that re-sends the whole conversation; the sanctioned way to wait
is to start the work once with ``run_in_background`` and end the turn —
the completion notification wakes the agent. The guard makes that the
ONLY easy path.

The fixed-sleep allowance (owner ruling 2026-10-15): a ``sleep`` with a
LITERAL numeric duration (``sleep 3``, ``sleep 0.5``, ``sleep 2s``;
``Start-Sleep -Seconds 2`` on PowerShell) sitting BETWEEN other segments
of one command — ``start-x && sleep 3 && curl ...`` — is a fixed pause,
not a poll, and is ALLOWED. Still denied: a sleep inside a loop (body or
condition, ``bash -c`` recursion included — also behind ``do bash -c …``
or a subshell paren), a non-literal duration (``sleep $N``, ``sleep
infinity``), and a sleep that is the sole, first or last segment (outside
a loop it only waits). R6 review round (same day): the wrapper-tail scan
uses the walker's ``dash_c_subcommand`` (``timeout 30 bash -e -c '…'``
cannot slip); a sole wait inside a subshell ``( sleep 5 )``, a command
substitution ``$(sleep 5)``, an xargs per-item command
(``xargs -n1 sleep`` — xargs is a loop), or a function body's loop
(``f() { while sleep 1; … }``) denies; and a recursed ``bash -c``
sub-chain that is the outer segment's whole content inherits the outer
position, so ``x && bash -c 'sleep 3' && y`` is the between-actions pause
it reads as. A substitution in an assignment value (``x=$(sleep 5)``,
``export x=`sleep 5```) is walked like any other — a sole wait there
denies. A function body — brace- OR subshell-bodied — is judged by the
same position rule WITHIN the body when the function is called in the
same command: ``f() { sleep 1; }; f`` and ``f() ( sleep 1 ); f`` deny,
``f() { a; sleep 1; b; }; f`` is a between-actions pause, a
definition-only command runs nothing and is judged as before, and a call
inside a loop puts the whole body in-loop. ``cmd & wait`` is NOT a poll (``cmd`` runs; the builtin
blocks once, in-process) and stays allowed. An UNQUOTED NEWLINE is a
command separator (the walker rewrites it to ``;``), so a multi-line
command is judged like its one-line form; an ``eval '<literal>'`` body is
re-parsed exactly like a ``bash -c`` body; a process substitution
(``diff <(sleep 5)``) has its body scanned like a command substitution;
and heredoc BODIES are stripped before the walk (they are data — a body
line is never a command).

Busy-wait loops (owner ruling 2026-10-15): a ``while``/``until`` loop
whose BODY does no real work is denied as a ``busy-wait loop`` even when
it contains no sleep at all — it spins on its condition with no pause,
re-checking as fast as the shell allows (``until curl -s localhost:8080;
do :; done``). "No real work" is precise: every body segment is empty,
``:``, ``true``, or a bare ``echo``/``printf`` with no OUTPUT redirection
and no pipe — an ``echo "$l" >> out`` ACCUMULATES output (work), while an
fd duplication or a ``/dev/null`` sink (``printf . >&2``,
``echo x 2>/dev/null``) writes nowhere and is NOT work (R10 F2). A body
segment doing anything else — a real command, a ``read``, a redirected
``echo``, an embedded loop — is work and the loop stays allowed
(``for f in *.py; do ruff check $f; done``; ``for``/``select`` over a
word list iterate a finite list and are never busy-waits — the EXCEPTION
is the arithmetic ``for ((;;))`` with an empty middle clause, which is
``while true`` in disguise and is judged like one, R10 F3). And a loop
whose condition is a single input consumer — ``while read``,
``while IFS= read -r l``, ``while mapfile`` — is bounded by that input
(it ends at EOF) and is never a busy-wait (R10 F1, NARROWED by R14 A2:
``until read`` and ``while ! read`` spin at EOF, ``read -t <n>`` waits
by polling, and a piped producer (``while curl | read``) re-runs every
pass — none is exempt).

Condition-refresh polls (coordinator ruling 2026-10-15, R14 A3, both
shells): a loop whose ONLY work is re-fetching the value its own
condition tests is a poll and is denied as ``condition-refresh poll`` —
``until [ "$r" = 200 ]; do r=$(curl -s u); done``. Precisely: the
condition reads at least one variable, and every body segment is either
no-work or a FETCH assignment to one of those variables (a command
substitution — arithmetic ``i=$((i+1))`` and literals are NOT fetches,
so counted loops stay allowed), with at least one fetch. A body doing
anything else — real work, an assignment to a different variable — is
ordinary work and the loop passes. The walker carries each statement's
assignment prefix on the segment (re-joined to the end of its value), so
the rule reads multi-line bodies exactly like single-line ones (R16
A-N1/A-N2: a multi-line ``r=$(curl -s u)`` body line used to reach the
classifier as its torn tail, and a counted loop's ``i=$((i+1))`` line
vanished — a false busy-wait).
Position and loop membership come from the ONE chain walker
(:func:`vco_lib.bash_command_walk.walk_segments`); the PowerShell side
applies the SAME rule through
:func:`vco_lib.powershell_command.loop_pattern` (R10 parity: a loop with
a wait inside, an unbounded loop with a no-work body, or a
condition-refresh poll denies; a loop doing real work — or reading input
in its condition — passes).

Shape (A>B>C, like the v0.2.101 injection surfaces): this module is the
pure classifier + a small ephemeral-state helper; the ``.sh`` / ``.ps1``
hooks (``templates/hooks/wait-guard.{sh,ps1}``) are THIN wrappers that
pipe the hook JSON to ``python -m vco_lib.wait_guard <mode>`` and forward
its stdout verbatim. The wrappers own no decision.

Modes
=====
``pretooluse``
    Read the PreToolUse hook JSON on stdin; print the deny envelope
    (``hookSpecificOutput.permissionDecision: "deny"`` + reason) on a
    positive detection, NOTHING on allow.

``record-expansion``
    Read the UserPromptExpansion hook JSON on stdin; when the USER typed
    ``/loop`` or ``/schedule`` (input field ``command_name``, with
    ``expansion_type == "slash_command"`` — the DOCUMENTED shape, vendor
    hooks doc "UserPromptExpansion": typing a slash command directly
    bypasses PreToolUse and fires this event, matched on ``command_name``),
    record the unlock for that ``session_id``. That is the only way a
    self-wake tool becomes legal in the MAIN chat — a subagent is never
    unlocked. Registered with matcher ``loop|schedule`` so nothing else
    spawns.

Knobs (read here, documented in docs/CONFIGURATION.md)
======================================================
``VCO_WAIT_GUARD``
    Kill switch: ``off`` / ``0`` / ``false`` disables every check (beside
    ``VCT_DISABLE_HOOKS``, which the wrappers honour before spawning).
``VCO_WAIT_GUARD_REPEAT_MAX``
    The Nth identical status check within the window that is DENIED
    (default 4 — checks 1..3 pass).
``VCO_WAIT_GUARD_REPEAT_WINDOW_S``
    The repeat-detector window in seconds (default 600).

Never break a chat
==================
A positive detection always denies with the item-6 message (see the
builders below). An INTERNAL error of the guard itself (unreadable hook
input, unwritable state) ALLOWS the tool with one loud stderr line — the
guard's own bug must never block work. Every deny also writes one stderr
line so the denial is visible in the transcript/logs.

Ephemeral state
===============
Per-actor file under ``$VCT_STATE_DIR/wait_guard/`` (actor =
``session_id`` + ``agent_id``-or-main). Per repeat key the file holds the
stamps AND the ``tool_use_id`` values already counted for that key (both
pruned to the window on every touch); slash unlocks keep their own longer
TTL; an empty file is deleted. This NEVER touches the durable injection
seen-store.

One tool call counts ONCE
=========================
The settings template registers this guard once per Bash verb rule, and
the harness evaluates each ``if`` against every subcommand — so ONE
compound command fires this hook several times CONCURRENTLY with the same
payload (v0.2.103; pre-fix a single call was denied as "the 4th identical
status check" on its first run). The repeat detector therefore (a) runs
its read-modify-write under the shared cross-OS lock
(:func:`vco_lib.atomic.portable_exclusive_lock`), and (b) dedupes by the
payload's ``tool_use_id``: an id already counted for the key is one call's
fan-out, not a new check. A payload with no ``tool_use_id`` falls back to
collapsing same-key stamps closer than :data:`_BURST_COLLAPSE_S` — real
polling is never sub-second, every poll is a model round-trip. N DISTINCT
calls of the same check within the window are still denied at the Nth,
and a denied call still counts. And a command that WRITES is never a
status check at all (field case 2026-10-15: ``cat > f <<'EOF' …; sed -i
…`` keyed on ``cat``): :func:`bash_repeat_key` drops the key when the
command text shows a write, decided by the ONE write-shape predicate
(:func:`vco_lib.bash_write_targets.command_has_write_shape`) — pure reads
still accumulate.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from vco_lib.bash_command_walk import (
    CONDITION_OPENERS,
    LEADING_COMPOUND_KEYWORDS,
    LOOP_OPENERS,
    SHELL_DASH_C_VERBS,
    ChainSegment,
    dash_c_subcommand,
    literal_eval_body,
    peel_wrapper_verbs,
    split_chain,
    strip_env_assignments,
    strip_subshell_parens,
    tokenize,
    walk_segments,
)
from vco_lib.bash_write_targets import (
    REDIRECT_NOISE,
    collect_candidates,
    strip_heredocs,
)
from vco_lib.powershell_command import (
    POWERSHELL_TOOL,
    loop_pattern,
    PS_WAIT_VERBS,
    SHELL_TOOLS,
    command_for_bash_readers,
)
from vco_lib.env_values import env_int

__all__ = [
    "DEFAULT_REPEAT_MAX",
    "DEFAULT_REPEAT_WINDOW_S",
    "SELF_WAKE_TOOLS",
    "UNLOCK_COMMANDS",
    "evaluate",
    "record_expansion_event",
    "static_pattern",
    "main",
]

#: Tools whose whole purpose is to wake the agent later. Inside a subagent
#: (hook input carries ``agent_id``) they are ALWAYS denied; in the main
#: chat only after the UserPromptSubmit recorder saw the user type
#: ``/loop`` / ``/schedule`` in that session.
#: ``Monitor`` is deliberately NOT here (v0.2.104): it is Claude Code's
#: sanctioned streaming tool (``tail -f ... | grep --line-buffered``, poll
#: loops with sleep run as a background event stream, not as a foreground
#: wait), so the guard neither registers on it nor judges it.
SELF_WAKE_TOOLS = frozenset({"ScheduleWakeup", "CronCreate"})

#: First-verb table of the repeat detector: a read-only command whose
#: normalized form repeats (same verb, same tokens) N times in the window.
REPEAT_VERBS = frozenset({"tail", "head", "cat", "ls", "wc", "grep"})

#: v0.2.102 W5-D (review W5D-2): the PowerShell tool gets NO ``if``-filtered
#: twins. Its loop keywords (``while``/``do``/``for``/``foreach``) are not
#: commands, so no ``PowerShell(...)`` rule can ever match them; the guard is
#: registered for PowerShell WITHOUT an ``if`` (every call — the PowerShell
#: tool only exists where Git Bash does not, so the spawn cost is bounded)
#: and :func:`vco_lib.powershell_command.loop_pattern` judges the loop
#: shapes here.

DEFAULT_REPEAT_MAX = 4
DEFAULT_REPEAT_WINDOW_S = 600
#: No-``tool_use_id`` fallback (v0.2.103): same-key stamps closer than
#: this are ONE call's registration fan-out and collapse into one stamp —
#: real polling is never sub-second (every poll is a model round-trip).
_BURST_COLLAPSE_S = 1.0
#: Slash unlocks live far longer than one repeat window — a ``/loop``
#: session routinely runs longer than 10 minutes.
SLASH_TTL_S = 86400

_KILL_SWITCH_ENV = "VCO_WAIT_GUARD"
_OFF_VALUES = frozenset({"off", "0", "false", "no"})

#: The user-typed slash commands that unlock self-wake tools in the MAIN
#: chat, matched against the UserPromptExpansion input's ``command_name``
#: (the DOCUMENTED field for that event — never parsed out of prompt text).
UNLOCK_COMMANDS = frozenset({"loop", "schedule"})

_MAX_TIMEOUT_SCAN_DEPTH = 4


def _disabled(env: Dict[str, str]) -> bool:
    return str(env.get(_KILL_SWITCH_ENV, "")).strip().lower() in _OFF_VALUES


# ═══════════════════════════════════════════════════════════════════════
# Static polling patterns (foreground Bash, and PowerShell read as Bash)
# ═══════════════════════════════════════════════════════════════════════

_TAIL_FOLLOW_FLAGS = frozenset({"-f", "-F", "--follow"})
#: tail's own short-flag alphabet — a combined flag like ``-fn20`` follows
#: only if every character is a real tail flag and one of them is ``f``/``F``.
_TAIL_FLAG_ALPHABET = frozenset("fnqvsc0123456789")

#: Wrappers whose WRAPPED command must be scanned too (the chain walker
#: peels ``sudo``/``nice``/... but not these).
_SCAN_WRAPPER_VERBS = frozenset({"timeout", "nohup", "setsid"})

#: A literal sleep duration (owner ruling 2026-10-15): ``sleep 3``,
#: ``sleep 0.5``, ``sleep 2s`` (GNU suffixes s/m/h/d; ``ms`` covers the
#: PowerShell ``Start-Sleep -Milliseconds`` reading). ``sleep $N``,
#: ``sleep infinity``, a flag or a missing argument are NOT literal —
#: they are waits of unknown length and stay denied everywhere.
_LITERAL_SLEEP_RE = re.compile(r"^[0-9]+(\.[0-9]+)?(?:ms|[smhd])?$")

#: Shell keywords that prefix the command inside a compound body — the
#: chain walker splits ``do sleep 2; done`` into a ``do sleep 2`` segment
#: and the VERB is what follows the keyword. The set lives in the ONE
#: chain walker (:data:`vco_lib.bash_command_walk.LEADING_COMPOUND_KEYWORDS`,
#: imported above); ``time`` needs no entry — the walker's wrapper peel
#: already removed it from every emitted segment.
_LEADING_KEYWORDS = LEADING_COMPOUND_KEYWORDS


def _tail_follows(args: List[str]) -> bool:
    for arg in args:
        if arg in _TAIL_FOLLOW_FLAGS or arg.startswith("--follow"):
            return True
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            body = arg[1:]
            if "f" in body.lower() and all(
                c.lower() in _TAIL_FLAG_ALPHABET for c in body
            ):
                return True
    return False


def _sleep_pattern(body: List[str], seg: ChainSegment) -> Optional[tuple]:
    """The fixed-sleep rule (owner ruling 2026-10-15): a ``sleep`` with a
    LITERAL numeric duration BETWEEN other segments of one command
    (``start-x && sleep 3 && curl ...``) is the sanctioned pause and is
    ALLOWED. Still denied:

    (a) inside a loop (opener condition or ``do``..``done`` body —
        :data:`ChainSegment.in_loop`, inherited through ``bash -c``);
    (b) a non-literal duration (``sleep $N``, ``sleep infinity``);
    (c) the sleep as the sole, FIRST or LAST segment — outside a loop it
        only waits (the ruling's allow is "BETWEEN other actions").
    """
    evidence = " ".join(body[:2])
    if seg.in_loop:
        return ("sleep", evidence)
    args = body[1:]
    if not args or not all(_LITERAL_SLEEP_RE.match(a) for a in args):
        return ("sleep", evidence)
    if seg.position == 0 or seg.position == seg.chain_size - 1:
        return ("sleep", evidence)
    return None


#: xargs flags that take a SEPARATE value token (the combined forms
#: ``-n1`` / ``-I{}`` do not). Skipped when looking for the per-item
#: command, so ``xargs -I sleep echo`` (replace-str "sleep") is not
#: misread as a per-item wait.
_XARGS_VALUE_FLAGS = frozenset({
    "-I", "-n", "-P", "-d", "-L", "-s", "-e", "-a",
    "--replace", "--max-args", "--max-procs", "--delimiter",
    "--max-lines", "--eof", "--arg-file",
})


def _xargs_pattern(body: List[str], depth: int,
                   seg: ChainSegment) -> Optional[tuple]:
    """xargs runs its command ONCE PER INPUT ITEM — a loop (R6, owner
    ruling 2026-10-15). The per-item command is the first non-flag token
    (separate-value flags skipped): a wait verb there is a per-item wait
    (``xargs -n1 sleep``), and a ``sh -c '…'`` tail is recursed with
    loop context (``xargs -I{} sh -c 'sleep 1; …'``)."""
    i = 1
    while i < len(body):
        tok = body[i]
        if tok in _XARGS_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        verb = os.path.basename(tok).lower()
        if verb in ("sleep", "watch"):
            return ("xargs per-item wait", " ".join(body[: i + 2])[:80])
        if verb in SHELL_DASH_C_VERBS:
            sub = dash_c_subcommand(body[i:])
            if sub is not None:
                return static_pattern(sub, depth=depth + 1, _in_loop=True)
            return None
        return None  # the per-item command is something else — not a wait
    return None


def _pattern_of_tokens(tokens: List[str], depth: int,
                       seg: ChainSegment) -> Optional[tuple]:
    """The verb dispatch for one token list — used for each walked segment
    AND for the condition tail of a ``while``/``until`` opener (owner
    ruling 2026-10-15, gap 2: a wait AS the loop condition is a wait in
    the loop; the condition tail is scanned as a segment of its own, with
    the opener's in-loop context, so its sleep denies regardless of
    position)."""
    body = tokens
    while body and os.path.basename(body[0]).lower() in _LEADING_KEYWORDS:
        body = body[1:]
    # Subshell/group punctuation (R6): `( sleep 5 )` / `(sleep 5)` — the
    # subshell's verb is what the parens wrap.
    body = strip_subshell_parens(body)
    if not body:
        return None
    verb = os.path.basename(body[0]).lower()
    if verb in CONDITION_OPENERS:
        # `while <condition>` / `until <condition>`: the condition is a
        # command that runs every iteration — scan its tail. (`for` /
        # `select` take a word list, never a command: not dispatched.)
        return _pattern_of_tokens(body[1:], depth, seg)
    if verb == "for":
        # The arithmetic `for ((init; cond; repeat))` header belongs to the
        # loop (R20 M2, parity with the PowerShell `for (…;…;…)` reader in
        # vco_lib.powershell_command.loop_pattern): a wait word in ANY of
        # its clauses is a wait in the loop. A word-list `for x in …` has
        # no clauses and returns None here.
        for clause in _arith_for_clauses(body):
            found = _pattern_of_tokens(tokenize(clause) or [], depth, seg)
            if found is not None:
                return found
        return None
    if verb == "sleep":
        return _sleep_pattern(body, seg)
    if verb == "watch":
        return ("watch", body[0])
    if verb == "tail" and _tail_follows(body[1:]):
        return ("tail --follow", " ".join(body[:3]))
    if verb in _SCAN_WRAPPER_VERBS:
        return _scan_wrapped(body, depth, seg)
    if verb == "xargs":
        return _xargs_pattern(body, depth, seg)
    if verb in SHELL_DASH_C_VERBS:
        # `while bash -c 'sleep 1'; do …` — the condition's own dash-c
        # recursion (the walker only recurses a segment whose FIRST token
        # is the shell; the condition tail starts after `while`).
        sub = dash_c_subcommand(body)
        if sub is not None:
            return static_pattern(sub, depth=depth + 1, _in_loop=seg.in_loop)
        return None
    if verb == "eval":
        # `while eval 'sleep 1'; do …` — a literal eval body in a loop
        # condition is re-parsed like a dash-c body (v0.2.103).
        sub = literal_eval_body(body)
        if sub is not None:
            return static_pattern(sub, depth=depth + 1, _in_loop=seg.in_loop)
        return None
    if verb in PS_WAIT_VERBS:
        # PowerShell's foreground waits (Wait-Job / Wait-Process /
        # Wait-Event, and Receive-Job -Wait) — the translated name; no Bash
        # command starts with one, so this never fires on a Bash call.
        return (verb, " ".join(body[:3]))
    return None


def _pattern_of_segment(seg: ChainSegment, depth: int) -> Optional[tuple]:
    found = _pattern_of_tokens(seg.tokens, depth, seg)
    if found is not None:
        return found
    if seg.in_loop:
        # A loop opener can sit MID-SEGMENT behind a function/group header
        # (`f() { while sleep 1; …` — R6, owner ruling 2026-10-15: the loop
        # rule applies inside function bodies). The walker marked the
        # segment in-loop; scan the tail of a mid-segment while/until as
        # its condition.
        toks = seg.tokens
        for j in range(1, len(toks)):
            kw = os.path.basename(toks[j]).lower()
            if kw in CONDITION_OPENERS:
                return _pattern_of_tokens(toks[j + 1:], depth, seg)
            if kw in LOOP_OPENERS:
                return None  # for/select: no condition command to scan
    return None


def _scan_wrapped(tokens: List[str], depth: int,
                  seg: ChainSegment) -> Optional[tuple]:
    """``timeout 30 bash -c 'sleep 30'`` and kin: the chain walker hands the
    whole segment to the collector with ``timeout`` as the verb, so the
    wrapped tail is scanned here — pattern verbs directly, and a nested
    ``<shell> -c '<sub>'`` recursively through :func:`static_pattern`,
    where the fixed-sleep rule applies to the sub-chain as at the top
    level (loop context AND the segment's position both propagate — a
    sole sleep inside a wrapper that sits between actions is a
    between-actions pause).

    The direct tail scan judges a wrapped ``sleep`` with the OUTER
    segment's position (R6: ``timeout 5 sleep 3`` is a bounded fixed
    pause; between actions it is allowed, as the sole/last action it is a
    wait). The ``-c`` shape test is the walker's
    :func:`~vco_lib.bash_command_walk.dash_c_subcommand` (one home — R6
    MEDIUM: the retired inline rest[0] check missed ``bash -e -c``)."""
    if depth >= _MAX_TIMEOUT_SCAN_DEPTH:
        return None
    for i in range(1, len(tokens)):
        base = os.path.basename(tokens[i]).lower()
        if base == "sleep":
            return _sleep_pattern(tokens[i:], seg)
        if base == "watch":
            return ("watch", " ".join(tokens[: i + 2])[:80])
        if base in SHELL_DASH_C_VERBS:
            sub = dash_c_subcommand(tokens[i:])
            if sub is not None:
                return static_pattern(
                    sub, depth=depth + 1, _in_loop=seg.in_loop,
                    _position_override=(seg.position, seg.chain_size))
        if base == "eval":
            sub = literal_eval_body(tokens[i:])
            if sub is not None:
                return static_pattern(
                    sub, depth=depth + 1, _in_loop=seg.in_loop,
                    _position_override=(seg.position, seg.chain_size))
    return None


#: Body verbs that do NO real work (owner ruling 2026-10-15): a
#: ``while``/``until`` loop whose every body segment is one of these (or
#: empty) spins on its condition with no pause — a busy-wait. ``echo`` /
#: ``printf`` count ONLY without a redirection or pipe: an
#: ``echo "$l" >> out`` accumulates output, which is work.
_NOWORK_VERBS = frozenset({":", "true", "echo", "printf"})

#: Redirection / pipe tokens that make an ``echo``/``printf`` body real
#: work (it writes a file or feeds a downstream command). An INPUT
#: redirect is not output (R10 F7): ``echo x < f`` writes nothing.
_REDIRECT_TOKENS = frozenset({">", ">>", "|", "&>", "&>>"})

#: Condition verbs that CONSUME INPUT (R10 F1): a loop whose condition
#: reads terminates at EOF — it is bounded by its input, never a spin.
#: ``read``/``mapfile``/``readarray`` (and their ``IFS=``-prefixed forms).
_INPUT_CONDITION_VERBS = frozenset({"read", "mapfile", "readarray"})


def _is_redirect_token(tok: str) -> bool:
    return (tok in _REDIRECT_TOKENS
            or tok.startswith(">")
            or tok.startswith("&>")
            or (tok.endswith(">") and tok[:-1].isdigit()))


def _is_noise_redirect(tok: str) -> bool:
    """An fd duplication or glued ``/dev/null`` sink — output that goes
    nowhere (R10 F2): it makes no file and feeds nothing, so it is NOT
    work. The token list is the write-target module's
    :data:`REDIRECT_NOISE` (one home), extended with the ``>&N`` /
    ``N>&M`` duplication shapes. The SPACED ``> /dev/null`` pair is
    handled by :func:`_real_output_tokens`."""
    return (tok in REDIRECT_NOISE
            or re.fullmatch(r"&?>\d+", tok) is not None
            or re.fullmatch(r"\d*>&\d+", tok) is not None)


def _real_output_tokens(tokens: List[str]) -> List[str]:
    """The tokens that could be real OUTPUT: fd duplications and
    ``/dev/null`` sinks (glued or spaced operator+target pair) removed."""
    out: List[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if _is_noise_redirect(t) or t == "/dev/null":
            i += 1
            continue
        if (t in (">", ">>", "&>", "&>>") and i + 1 < n
                and tokens[i + 1] == "/dev/null"):
            i += 2
            continue
        out.append(t)
        i += 1
    return out


def _segment_does_real_work(seg: ChainSegment) -> bool:
    """The busy-wait rule's work test for ONE loop-body statement: empty,
    ``:``, ``true``, the ``done`` terminator, or a bare ``echo``/``printf``
    (no OUTPUT redirection, no pipe — fd duplications and ``/dev/null``
    sinks dropped first, R10 F2) do no work; anything else — a real
    command, a ``read``, a redirected echo, an embedded loop opener, or
    an ASSIGNMENT statement (:attr:`ChainSegment.assignments` — a counted
    loop's ``i=$((i+1))`` body line used to vanish into the env-prefix
    strip and wrongly read as a no-work body, R16 A-N2) — does."""
    if seg.assignments:
        return True  # an assignment statement is work
    body = seg.tokens
    while body and os.path.basename(body[0]).lower() in _LEADING_KEYWORDS:
        body = body[1:]
    body = strip_subshell_parens(body)
    if not body:
        return False
    first = os.path.basename(body[0]).lower()
    if first == "done":
        return False  # the terminator, not body work
    if first in _NOWORK_VERBS and first not in ("echo", "printf"):
        return False
    if first in ("echo", "printf"):
        return any(_is_redirect_token(t) for t in _real_output_tokens(body[1:]))
    return True


def _infinite_for_header(tokens: List[str], start: int) -> bool:
    """True when ``tokens[start:]`` opens an arithmetic ``for`` whose
    MIDDLE clause is empty — ``for ((;;))``, ``for ((i=0;;))``,
    ``for (( ; ; ))``, ``for (( i=0 ; ; i++ ))`` — the arithmetic
    infinite loop (R10 F3, A5; the spaced forms since R16 A-N3, where
    :func:`vco_lib.bash_command_walk.split_chain` stopped tearing the
    header at its inner ``;``). ``for ((i=0; i<n; i++))`` has a bound
    and is finite."""
    header = " ".join(tokens[start + 1:])
    s = header.strip()
    if not (s.startswith("(") and s.endswith(")")):
        return False
    inner = s[1:-1]
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    core = inner.replace("(", "").replace(")", "").strip()
    if not core:
        return True  # `(( ; ; ))`: every clause empty
    parts = core.split(";")
    return len(parts) >= 2 and parts[1].strip() == ""


def _arith_for_clauses(tokens: List[str]) -> List[str]:
    """The clause texts of an arithmetic ``for ((init; cond; repeat))``
    header in ``tokens`` (``tokens[0]`` is ``for``), else ``[]`` — a
    word-list ``for x in …`` has none. Works on both the glued
    (``((i=0;i<3;i++))``) and spaced (``(( i=0 ; i<3 ; i++ ))``) token
    shapes the chain walker emits."""
    s = " ".join(tokens[1:]).strip()
    if not (s.startswith("((") and s.endswith("))")):
        return []
    return [c.strip() for c in s[2:-2].split(";") if c.strip()]


#: A Bash loop condition's variable references (`[ "$r" = 200 ]` → ``r``).
_COND_VAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")

#: A bare assignment shape: `name=`, `name+=`, `name-=`.
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\+?=")

#: A timed-read flag (`read -t 1 x`, `read -t0`): a wait-for-input by
#: polling, never an input-consuming condition (R14 A2).
_TIMED_READ_RE = re.compile(r"^-t\d*$")


def _condition_reads_input(opener_word: str, opener_tokens: List[str]) -> bool:
    """True when the loop CONDITION is a single input consumer — the
    first verb is ``read``/``mapfile``/``readarray`` behind ``IFS=``-style
    assignments and keywords, in a ``while`` (never ``until``), not
    negated (``!``), without a ``-t`` timeout flag on ``read``, and with
    no pipe producer in front (R10 F1, narrowed by R14 A2). Such a loop
    is bounded by its input (it ends at EOF) and is never a busy-wait;
    ``until read`` and ``while ! read`` SPIN at EOF, a timed ``read -t``
    waits by polling, and ``while curl | read`` re-runs the producer
    every pass — none of them is exempt."""
    if opener_word != "while":
        return False  # `until read` loops forever once read fails at EOF
    cond = opener_tokens
    if cond and os.path.basename(cond[0]).lower() == "!":
        return False  # `while ! read` succeeds at EOF and spins
    cond = strip_env_assignments(cond)
    cond = strip_subshell_parens(cond)
    if not cond:
        return False
    verb = os.path.basename(cond[0]).lower()
    if verb not in _INPUT_CONDITION_VERBS:
        return False
    if "|" in cond:
        return False  # a pipeline producer (curl | read) re-runs per pass
    if verb == "read" and any(_TIMED_READ_RE.match(t) for t in cond):
        return False  # `read -t <n>` is a timed wait, not consumption
    return True


def _is_fetch_value(value: str) -> bool:
    """True when an assignment's VALUE comes from outside the shell: a
    command substitution (``$(curl -s u)``, ``\\`cat f\\```) — as opposed
    to arithmetic (``$((i+1))``, a counted loop's step) or a literal
    (which satisfies the condition and lets the loop exit)."""
    if "$((" in value:
        value = value.replace("$((", "")  # arithmetic is not a fetch
    return "$(" in value or "`" in value


def _condition_refresh_fetch(seg: ChainSegment, cond_vars: set) -> bool:
    """True when ONE body statement is a FETCH assignment: it assigns a
    variable the condition reads, and the value comes from OUTSIDE the
    shell. Two shapes (R16): the re-joined :attr:`ChainSegment.assignments`
    of an assignment-only statement (a multi-line ``r=$(curl -s u)``
    body line — the walker's env-prefix strip used to tear it), and the
    glued ``do r=$(curl -s u)`` form, whose assignment rides in the
    emitted tokens."""
    for assignment in seg.assignments:
        m = _ASSIGN_RE.match(assignment)
        if (m is not None and m.group(1) in cond_vars
                and _is_fetch_value(assignment[m.end():])):
            return True
    body = list(seg.tokens)
    while body and os.path.basename(body[0]).lower() in _LEADING_KEYWORDS:
        body = body[1:]
    if not body:
        return False
    m = _ASSIGN_RE.match(body[0])
    if m is None or m.group(1) not in cond_vars:
        return False
    return _is_fetch_value(" ".join(body))


def _condition_refresh_poll(cond_tokens: List[str],
                            body_segments: List[ChainSegment]) -> Optional[tuple]:
    """The condition-refresh poll rule (coordinator ruling 2026-10-15,
    both shells): a loop whose ONLY work is re-fetching the value its own
    condition tests is a poll — it re-issues the fetch as fast as the
    shell allows, exactly the shape the wait guard exists to stop.
    Precisely: the condition reads at least one variable, and EVERY body
    statement is either no-work (empty, ``:``, ``true``, a bare
    ``echo``/``printf``) or a FETCH assignment to one of those variables
    (see :func:`_condition_refresh_fetch`), with at least one fetch.
    A body statement doing anything else — real work, an assignment to a
    DIFFERENT variable, arithmetic on the condition variable (a counted
    loop) — makes the loop ordinary work and it stays allowed. Works for
    the single-line and multi-line forms alike: the walker carries the
    assignment on the segment either way (R16 A-N1)."""
    cond_vars = set()
    for tok in cond_tokens:
        cond_vars.update(_COND_VAR_RE.findall(tok))
    if not cond_vars:
        return None
    saw_fetch = False
    for seg in body_segments:
        if not _segment_does_real_work(seg):
            continue  # a no-work statement never rescues a poll
        if _condition_refresh_fetch(seg, cond_vars):
            saw_fetch = True
            continue
        return None  # real work that is not a condition refresh
    if saw_fetch:
        return ("condition-refresh poll", " ".join(cond_tokens[:6])[:80])
    return None


def _busy_wait_pattern(segments: List[ChainSegment], i: int) -> Optional[tuple]:
    """The busy-wait rule (owner ruling 2026-10-15): a ``while``/``until``
    loop whose BODY does no real work — every body segment empty, ``:``,
    ``true``, or a bare ``echo``/``printf`` — is a spin with no pause and
    is denied as ``busy-wait loop`` even with no sleep in it. A body with
    real work stays allowed — EXCEPT the condition-refresh poll
    (coordinator ruling 2026-10-15): a body whose only work is fetch
    assignments to the variables the condition reads
    (``until [ "$r" = 200 ]; do r=$(curl -s u); done``) is a poll, not
    work. A ``while`` whose single-segment condition consumes input
    (``while IFS= read -r l``) is bounded by that input and is never a
    busy-wait (R10 F1, narrowed by R14 A2: ``until``/negated/``-t``/
    piped read conditions are NOT exempt). ``for`` over a word list and
    ``select`` iterate a finite list and are never busy-waits — EXCEPT
    the arithmetic ``for ((;;))`` with an empty middle clause, which is
    ``while true`` in disguise and is judged like one (R10 F3).

    ``segments[i]`` is the opener (or a segment with a mid-segment opener
    behind a ``{``/``(`` header); the body is the following ``in_loop``
    run from its ``do`` to the matching ``done``."""
    seg = segments[i]
    toks = seg.tokens
    body0 = list(toks)
    while body0 and os.path.basename(body0[0]).lower() in _LEADING_KEYWORDS:
        body0 = body0[1:]
    body0 = strip_subshell_parens(body0)
    first0 = os.path.basename(body0[0]).lower() if body0 else ""
    opener_word = first0
    has_opener = first0 in CONDITION_OPENERS
    if not has_opener and first0 == "for" and _infinite_for_header(body0, 0):
        has_opener = True  # for ((;;)): the arithmetic infinite loop
    cond_toks = body0[1:] if has_opener and first0 in CONDITION_OPENERS else []
    if not has_opener and seg.in_loop:
        # A loop opener sitting MID-SEGMENT behind a function/group header
        # (`f() { while true; …` — same shape as _pattern_of_segment).
        for j in range(1, len(toks)):
            kw = os.path.basename(toks[j]).lower()
            if kw in CONDITION_OPENERS:
                has_opener = True
                opener_word = kw
                cond_toks = toks[j + 1:]
                break
            if kw == "for" and _infinite_for_header(toks, j):
                has_opener = True
                break
            if kw in LOOP_OPENERS:
                return None  # a finite for/select: never a busy-wait
    if not has_opener:
        return None
    if _condition_reads_input(opener_word, cond_toks):
        return None  # bounded by input: `while read -r l; …` ends at EOF
    # Multi-segment conditions (`while a && b; do …`) belong to the loop
    # but never carry the input exemption (R14 A2); skip them to the do.
    j = i + 1
    while (j < len(segments) and segments[j].in_loop
           and not (segments[j].tokens and segments[j].tokens[0] == "do")):
        cond_toks = cond_toks + segments[j].tokens
        j += 1
    if not (j < len(segments) and segments[j].in_loop
            and segments[j].tokens and segments[j].tokens[0] == "do"):
        return None  # no `do`: malformed, not our business
    body_all: List[ChainSegment] = []
    body_stmts: List[ChainSegment] = []
    while j < len(segments) and segments[j].in_loop:
        body_all.append(segments[j])
        # Substitution / bash -c bodies (depth > the opener's) are part of
        # their parent STATEMENT's value, not statements of their own —
        # the poll rule classifies statements only.
        if segments[j].depth == seg.depth:
            body_stmts.append(segments[j])
        j += 1
    if all(not _segment_does_real_work(s) for s in body_all):
        return ("busy-wait loop", " ".join(toks[:6])[:80])
    return _condition_refresh_poll(cond_toks, body_stmts)


def static_pattern(command: str, depth: int = 0, *,
                   _in_loop: bool = False,
                   _position_override: Optional[tuple] = None) -> Optional[tuple]:
    """``(pattern_name, evidence)`` for the first polling shape in
    ``command``, else ``None``.

    Parsed with the ONE chain walker (:mod:`vco_lib.bash_command_walk`) —
    never a raw-string scan — so quoted text (``echo "sleep"``) cannot
    trip it and ``bash -c`` / literal ``eval`` sub-commands are
    re-parsed. ``while``/``until`` loops need no rule of their own for
    sleeps: the walker marks loop openers and bodies
    (:data:`ChainSegment.in_loop`), and the ``sleep`` segment inside one
    IS the detection; a ``while``/``until`` whose body does no real work
    is additionally denied as a ``busy-wait loop`` (owner ruling
    2026-10-15) even with no sleep in it. Heredoc bodies are stripped
    first (they are data). Command-substitution and process-substitution
    bodies are walked too — including those in an assignment value
    (``x=$(sleep 5)``) — and the body of a function called in the same
    command is positioned WITHIN the body (both opt-in walker surfaces;
    the wait guard is the consumer). ``_in_loop`` /
    ``_position_override`` are the recursion's inherited context; callers
    pass nothing."""
    if not isinstance(command, str) or not command.strip():
        return None
    command, _heredoc_bodies = strip_heredocs(command)
    segments = walk_segments(command, _in_loop=_in_loop,
                             _position_override=_position_override,
                             _with_substitutions=True,
                             _with_function_bodies=True,
                             _with_assignments=True)
    for i, seg in enumerate(segments):
        found = _pattern_of_segment(seg, depth)
        if found is not None:
            return found
        busy = _busy_wait_pattern(segments, i)
        if busy is not None:
            return busy
    return None


# ═══════════════════════════════════════════════════════════════════════
# Repeat-detector keys
# ═══════════════════════════════════════════════════════════════════════


def bash_repeat_key(command: str, prefix: str = "bash") -> Optional[str]:
    """The normalized repeat key for a Bash STATUS CHECK, or ``None`` when
    the command is not one (first peeled verb outside :data:`REPEAT_VERBS`),
    or when the command WRITES: a command with a real write target is never
    a poll (field case 2026-10-15: ``cat > f <<'EOF' …`` keyed as a "status
    check" on ``cat``). The write test is the ONE write-target parser
    (:func:`vco_lib.bash_write_targets.collect_candidates`) with the
    non-file sinks dropped — NOT the cheap write-shape predicate, which
    over-approximates (``| tee /dev/null``, ``| python3 -c …`` and a
    ``# >`` comment would let a pure-read poll escape the detector, R10
    F4; ``/dev/null`` and fd duplications write nothing). Pure reads
    (``cat log``) still accumulate.

    The key is the full token-joined command — quoting/whitespace variants
    of the same check normalize together, while a different target or flag
    set is a DIFFERENT check and never accumulates."""
    tokens = tokenize(command)
    if not tokens:
        return None
    segments = split_chain(tokens)
    if not segments:
        return None
    first = peel_wrapper_verbs(strip_env_assignments(segments[0]))
    if not first:
        return None
    if os.path.basename(first[0]).lower() not in REPEAT_VERBS:
        return None
    if any(t not in ("/dev/null", "stdout", "stderr")
           for t in collect_candidates(command)):
        return None
    return prefix + ":" + " ".join(tokens)


def read_repeat_key(tool_input: Dict[str, Any]) -> Optional[str]:
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    return "read:%s@%s+%s" % (
        file_path, tool_input.get("offset"), tool_input.get("limit"),
    )


def generic_repeat_key(tool_name: str, tool_input: Dict[str, Any]) -> str:
    """Field-agnostic key for TaskOutput-style read tools: the whole tool
    input, canonically serialized. No field is guessed."""
    return "%s:%s" % (
        tool_name.lower(),
        json.dumps(tool_input, sort_keys=True, default=str),
    )


# ═══════════════════════════════════════════════════════════════════════
# Deny message builders — one per case, each failing LOUDLY and teaching
# (owner 2026-10-08: (a) WHAT + pattern, (b) WHY, (c) WHAT INSTEAD,
# (d) the knob named as a USER decision only).
# ═══════════════════════════════════════════════════════════════════════

_WHY = (
    " WHY: every check/poll is a new request that re-sends the whole "
    "conversation (massive token waste); waiting is done by a NOTIFICATION, "
    "not by checking."
)
_KNOB = (
    " For the USER only (your decision, never the agent's): VCO_WAIT_GUARD=off "
    "disables this guard."
)


#: What to use instead of a foreground wait, per shell tool (owner: every
#: deny explains why AND what to use instead — in the caller's own shell).
_INSTEAD_BASH = (
    " WHAT TO DO INSTEAD: run the long command ONCE with"
    " run_in_background: true (or, to wait for an existing process,"
    " `tail --pid=<PID> -f /dev/null` in the background), then END YOUR"
    " TURN; the completion notification wakes you."
)
_INSTEAD_POWERSHELL = (
    " WHAT TO DO INSTEAD: run the long command ONCE with"
    " run_in_background: true (or, to wait for an existing process or job,"
    " `Wait-Process -Id <PID>` / `Wait-Job -Id <N>` with"
    " run_in_background: true — never Start-Sleep, a while ($true) loop,"
    " Get-Content -Wait or a foreground Wait-* cmdlet), then END YOUR TURN;"
    " the completion notification wakes you."
)


def _shell_label(tool: str) -> str:
    return "PowerShell" if tool == POWERSHELL_TOOL else "Bash"


def static_deny_reason(command: str, pattern: tuple, tool: str = "Bash",
                       translated: bool = True) -> str:
    """``translated``: the evidence is the PowerShell command's Bash
    reading (said as "read as"), not a quote of the command itself."""
    name, evidence = pattern
    ps = tool == POWERSHELL_TOOL
    read_as = f" (read as `{evidence}`)" if ps and translated else f" ({evidence})"
    return (
        f"BLOCKED by the VCO wait-loop guard: the {_shell_label(tool)} command "
        f"`{command}` matches the polling pattern `{name}`{read_as}." + _WHY +
        (_INSTEAD_POWERSHELL if ps else _INSTEAD_BASH) + _KNOB
    )


def self_wake_subagent_reason(tool: str) -> str:
    return (
        f"BLOCKED by the VCO wait-loop guard: the self-wake tool `{tool}` was "
        f"called from inside a subagent (the hook input carries agent_id)."
        + _WHY +
        " WHAT TO DO INSTEAD: subagents never self-schedule — start the work"
        " with run_in_background: true and END YOUR TURN; the completion"
        " notification wakes you (or the parent) when it is done." + _KNOB
    )


def self_wake_main_reason(tool: str) -> str:
    return (
        f"BLOCKED by the VCO wait-loop guard: the self-wake tool `{tool}` is "
        f"allowed only when the USER typed /loop or /schedule in this "
        f"session, and no such command is on record for it." + _WHY +
        " WHAT TO DO INSTEAD: only the user can start a loop (/loop); use a"
        " background command (run_in_background: true) and END YOUR TURN,"
        " and let the completion notification wake you." + _KNOB
    )


def repeat_deny_reason(key: str, count: int, window_s: int,
                       tool: str = "") -> str:
    ordinal = "%dth" % count
    again = (
        " (Get-Content / Get-ChildItem / Select-String / Measure-Object"
        " included)" if tool == POWERSHELL_TOOL else ""
    )
    return (
        f"BLOCKED by the VCO wait-loop guard: this is the {ordinal} identical "
        f"status check (`{key}`) within {window_s}s — repeated polling of the "
        f"same thing." + _WHY +
        f" WHAT TO DO INSTEAD: do not re-check the file/log{again}; start the"
        " work once with run_in_background: true, END YOUR TURN, and read the"
        " file ONCE after the completion notification." + _KNOB
    )


# ═══════════════════════════════════════════════════════════════════════
# Ephemeral per-actor state
# ═══════════════════════════════════════════════════════════════════════


def _state_dir(state_root: Optional[Path]) -> Path:
    if state_root is not None:
        return Path(state_root)
    env = os.environ.get("VCT_STATE_DIR")
    if env:
        return Path(env) / "wait_guard"
    from vco_lib.paths import vct_root_dir

    return vct_root_dir() / "wait_guard"


def _actor_key(payload: Dict[str, Any]) -> str:
    session = str(payload.get("session_id") or "default")
    agent = str(payload.get("agent_id") or "main")
    raw = "%s__%s" % (session, agent)
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:128]
    return sanitized or "default"


def _load_state(path: Path, now: float, window_s: int, slash_ttl: int) -> Dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"checks": {}, "slash": {}}
    if not isinstance(data, dict):
        return {"checks": {}, "slash": {}}
    # ``checks[key]`` is ``{"stamps": [t, ...], "ids": {tool_use_id: t}}``;
    # a pre-v0.2.103 file holds a bare stamp list and is migrated on load.
    # Both stamps and counted ids prune with the window.
    checks: Dict[str, Dict[str, Any]] = {}
    raw_checks = data.get("checks")
    if isinstance(raw_checks, dict):
        for k, v in raw_checks.items():
            if isinstance(v, list):
                stamps = [t for t in v
                          if isinstance(t, (int, float)) and now - t <= window_s]
                ids: Dict[str, Any] = {}
            elif isinstance(v, dict):
                raw_stamps = v.get("stamps")
                stamps = (
                    [t for t in raw_stamps
                     if isinstance(t, (int, float)) and now - t <= window_s]
                    if isinstance(raw_stamps, list) else []
                )
                raw_ids = v.get("ids")
                ids = (
                    {str(i): t for i, t in raw_ids.items()
                     if isinstance(t, (int, float)) and now - t <= window_s}
                    if isinstance(raw_ids, dict) else {}
                )
            else:
                continue
            if stamps or ids:
                checks[k] = {"stamps": stamps, "ids": ids}
    raw_slash = data.get("slash")
    slash = {
        k: t for k, t in raw_slash.items()
        if isinstance(t, (int, float)) and now - t <= slash_ttl
    } if isinstance(raw_slash, dict) else {}
    return {"checks": checks, "slash": slash}


def _save_state(path: Path, state: Dict) -> None:
    if not state["checks"] and not state["slash"]:
        try:
            path.unlink()
        except OSError:
            pass
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        from vco_lib.atomic import atomic_write_text

        atomic_write_text(path, json.dumps(state))
    except OSError:
        # Unwritable state is an INTERNAL failure: the caller already has
        # its decision (counted in memory) and must not block the tool.
        _stderr("could not persist repeat-detector state (%s): allowing" % path)


def _state_path(payload: Dict[str, Any], state_root: Optional[Path]) -> Path:
    return _state_dir(state_root) / ("wait_%s.json" % _actor_key(payload))


def _stderr(line: str) -> None:
    sys.stderr.write("[wait-guard] %s\n" % line)


# ═══════════════════════════════════════════════════════════════════════
# Decision
# ═══════════════════════════════════════════════════════════════════════


def _repeat_check(
    payload: Dict[str, Any], key: str, env: Dict[str, str],
    state_root: Optional[Path], now: float, tool: str = "",
) -> Optional[str]:
    window_s = env_int("VCO_WAIT_GUARD_REPEAT_WINDOW_S", DEFAULT_REPEAT_WINDOW_S,
                        env=env, minimum=1)
    max_n = env_int("VCO_WAIT_GUARD_REPEAT_MAX", DEFAULT_REPEAT_MAX, env=env, minimum=1)
    path = _state_path(payload, state_root)
    tool_use_id = payload.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        tool_use_id = None
    from vco_lib.atomic import portable_exclusive_lock

    # The `if`-filtered registrations fire this hook CONCURRENTLY for one
    # tool call: the read-modify-write runs under the shared cross-OS lock
    # so the concurrent spawns serialize instead of racing the state file.
    with portable_exclusive_lock(path.with_name(path.name + ".lock")):
        state = _load_state(path, now, window_s, SLASH_TTL_S)
        entry = state["checks"].setdefault(key, {"stamps": [], "ids": {}})
        stamps = entry["stamps"]
        ids = entry["ids"]
        if tool_use_id is not None:
            # One tool call counts ONCE per key, however many
            # registrations fired for it (v0.2.103).
            if tool_use_id in ids:
                return None
            ids[tool_use_id] = now
        elif stamps and now - stamps[-1] < _BURST_COLLAPSE_S:
            # No id to dedupe on: a sub-second repeat of the same key is
            # one call's fan-out, not a new poll.
            return None
        stamps.append(now)
        entry["stamps"] = [t for t in stamps if now - t <= window_s]
        entry["ids"] = {i: t for i, t in ids.items() if now - t <= window_s}
        _save_state(path, state)
        count = len(entry["stamps"])
    if count >= max_n:
        return repeat_deny_reason(key, count, window_s, tool)
    return None


def evaluate(
    payload: Dict[str, Any],
    *,
    env: Optional[Dict[str, str]] = None,
    state_root: Optional[Path] = None,
    now: Optional[float] = None,
) -> Optional[str]:
    """The PreToolUse decision for ``payload``: a deny REASON on a positive
    detection, ``None`` to allow. Pure decision + ephemeral state; the CLI
    wraps it in the deny envelope and the loud-stderr contract."""
    env = dict(os.environ) if env is None else env
    if _disabled(env):
        return None
    if not isinstance(payload, dict):
        return None
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    now = time.time() if now is None else now

    if tool in SELF_WAKE_TOOLS:
        if payload.get("agent_id"):
            return self_wake_subagent_reason(str(tool))
        window_s = env_int("VCO_WAIT_GUARD_REPEAT_WINDOW_S", DEFAULT_REPEAT_WINDOW_S,
                            env=env, minimum=1)
        path = _state_path(payload, state_root)
        state = _load_state(path, now, window_s, SLASH_TTL_S)
        if state["slash"]:
            return None
        return self_wake_main_reason(str(tool))

    if tool in SHELL_TOOLS:
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        if tool_input.get("run_in_background"):
            # The sanctioned trigger: a backgrounded command never polls.
            return None
        # PowerShell (v0.2.102 W5-D): read through its Bash-equivalent form
        # — one detector for both shells (vco_lib.powershell_command).
        readable = command_for_bash_readers(tool, command)
        pattern = static_pattern(readable)
        if pattern is not None:
            return static_deny_reason(command, pattern, str(tool))
        if tool == POWERSHELL_TOOL:
            # A loop that waits, with or without a Start-Sleep inside
            # (review W5D-2): judged on the PowerShell text itself.
            loop = loop_pattern(command)
            if loop is not None:
                return static_deny_reason(command, loop, str(tool), translated=False)
        key = bash_repeat_key(
            readable, "powershell" if tool == POWERSHELL_TOOL else "bash")
        if key is not None:
            return _repeat_check(payload, key, env, state_root, now, str(tool))
        return None

    if tool == "Read":
        key = read_repeat_key(tool_input)
        if key is not None:
            return _repeat_check(payload, key, env, state_root, now)
        return None

    if tool == "TaskOutput":
        return _repeat_check(
            payload, generic_repeat_key(tool, tool_input), env, state_root, now
        )

    return None


def record_expansion_event(
    payload: Dict[str, Any],
    *,
    env: Optional[Dict[str, str]] = None,
    state_root: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """UserPromptExpansion recorder: note a user-typed ``/loop`` /
    ``/schedule`` for this session. Returns whether the unlock was
    recorded.

    Reads the DOCUMENTED input shape (vendor hooks doc, UserPromptExpansion):
    ``command_name`` is the typed command (``"loop"`` for ``/loop``) and
    ``expansion_type`` is ``"slash_command"`` for skill / custom commands
    (``"mcp_prompt"`` is an MCP server prompt and does NOT unlock). The
    registration's matcher (``loop|schedule``) already limits what arrives;
    the field checks here are the decision, not a second parse of prompt
    text — the prompt string's slash-command shape is not documented."""
    env = dict(os.environ) if env is None else env
    if _disabled(env) or not isinstance(payload, dict):
        return False
    if payload.get("expansion_type") != "slash_command":
        return False
    name = payload.get("command_name")
    if not isinstance(name, str) or name not in UNLOCK_COMMANDS:
        return False
    now = time.time() if now is None else now
    window_s = env_int("VCO_WAIT_GUARD_REPEAT_WINDOW_S", DEFAULT_REPEAT_WINDOW_S,
                        env=env, minimum=1)
    path = _state_path(payload, state_root)
    state = _load_state(path, now, window_s, SLASH_TTL_S)
    state["slash"][name] = now
    _save_state(path, state)
    return True


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else "pretooluse"
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError("hook input is not a JSON object")
    except ValueError as exc:
        _stderr("internal error (allowing): unreadable hook input: %s" % exc)
        return 0
    try:
        if mode == "record-expansion":
            record_expansion_event(payload)
        else:
            reason = evaluate(payload)
            if reason:
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }))
                _stderr("denied: %s" % reason.splitlines()[0][:200])
    except Exception as exc:  # noqa: BLE001 — never break the chat
        _stderr("internal error (allowing): %r" % (exc,))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
