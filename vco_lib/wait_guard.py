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

Code a shell runs from ELSEWHERE (v0.2.105; peer field report
2026-10-21 and the R9 base-equal gaps): the SCRIPT FILE a command runs
(``bash poll.sh``, ``./poll.sh`` with a shell shebang, ``source``/``.``,
``cat poll.sh | bash``, a ``timeout``/``xargs`` wrapper in front; a
``.ps1`` on PowerShell) is read — at most :data:`SCRIPT_MAX_BYTES`, text
only, resolved against the hook input's ``cwd`` — and judged by this same
classifier in its SCRIPT READING: a loop that waits, a busy-wait or
condition-refresh loop, ``watch`` and ``tail -f`` are polls, while a single
pause is not (a script is a program) and a function body is judged where
it is defined. Code fed to a shell as TEXT — on stdin from a literal
``echo``/``printf``, a here-string, a heredoc — and code held in a
variable assigned a literal earlier in the same command (``c='sleep 5';
eval "$c"`` / ``$c`` / ``bash -c "$c"``) is judged like a ``bash -c``
body; a ``case`` arm and a ``coproc`` command are judged like any other
segment. ``run_in_background`` stays exempt from all of it — its own rule
is below.

Background wait loops (v0.2.105 fix round 2, item 7): a background command
never polls the model, so it is the sanctioned way to wait — but a WAIT
LOOP inside it must be BOUNDED, because a loop whose condition never comes
true runs until a human notices (field case 2026-10-30: an ``until`` loop
whose command was not on PATH ran 83 minutes, its ``2>/dev/null`` hiding
the error). Bounded = a literal ``timeout <N>`` around the whole command
(``timeout 600 bash -c '…'``) or a literal iteration cap; a loop whose
CONDITION throws stderr away is named in the refusal
(:func:`_background_wait_pattern`).

No-op wait loops (v0.2.105): the :data:`NOOP_REPEAT_MAX`-th IDENTICAL call
that does nothing (``true``, ``:``, ``exit 0``, a bare ``echo …`` /
``printf …`` / ``Write-Host``, ``sleep 0``) from one session/agent within
:data:`NOOP_REPEAT_WINDOW_S` is denied — "end your turn; the completion
notification wakes you".

One-shot wake-ups (owner ruling 2026-10-29, #508): a ``CronCreate`` whose
input carries the explicit ``recurring: false`` fires once and is allowed
in the main chat; recurring (or unspecified) ones keep the /loop-or-
/schedule rule, and subagents are refused either way.

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

import contextvars
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from vco_lib.bash_command_walk import (
    CONDITION_OPENERS,
    FLAG_WRAPPERS,
    LEADING_COMPOUND_KEYWORDS,
    LOOP_OPENERS,
    SHELL_DASH_C_VERBS,
    TIMEOUT_VALUE_OPTS,
    XARGS_VALUE_OPTS,
    ChainSegment,
    command_words as _command_words,
    dash_c_subcommand,
    literal_eval_body,
    peel_wrapper_verbs,
    split_chain,
    split_chain_ops,
    strip_env_assignments,
    strip_subshell_parens,
    segment_opener,
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
    has_loop as ps_has_loop,
    statements as ps_statements,
)
from vco_lib.env_values import env_int
from vco_lib.wait_guard_bounded import (
    WAIT_BUDGET_S,
    bounded_loop_sleeps,
    loop_iterations,
)
from vco_lib.wait_guard_literal import literal_text_only

__all__ = [
    "DEFAULT_REPEAT_MAX",
    "DEFAULT_REPEAT_WINDOW_S",
    "NOOP_REPEAT_MAX",
    "NOOP_REPEAT_WINDOW_S",
    "SCRIPT_MAX_BYTES",
    "SELF_WAKE_TOOLS",
    "UNLOCK_COMMANDS",
    "evaluate",
    "noop_repeat_key",
    "ps_script_pattern",
    "read_script_text",
    "record_expansion_event",
    "shell_static_pattern",
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

#: The no-op wait detector (v0.2.105, field report 2026-10-21: a subagent
#: issued an endless series of Bash ``true`` calls to "wait" for its own
#: background build). The Nth IDENTICAL no-op call (``true``, ``:``, a
#: bare ``echo …``, ``sleep 0``) from one session/agent within the window
#: is denied: calls 1..N-1 pass.
NOOP_REPEAT_MAX = 3
NOOP_REPEAT_WINDOW_S = 120

#: Verbs that do nothing a caller could be waiting on. ``echo`` /
#: ``printf`` / ``write-host`` count only with no output redirection, no
#: pipe and no command substitution; ``sleep`` only as ``sleep 0`` and
#: ``exit`` only with no argument or ``0`` (:func:`noop_repeat_key`).
_NOOP_VERBS = frozenset({"true", ":", "echo", "printf", "write-host"})

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
    ctx = _SCRIPT_CTX.get()
    if ctx is not None and ctx.in_script:
        # Inside a script FILE (v0.2.105) only the polling shapes count:
        # a script is a program, and a single pause in it is not a poll.
        return None
    args = body[1:]
    if not args or not all(_LITERAL_SLEEP_RE.match(a) for a in args):
        return ("sleep", evidence)
    if seg.position == 0 or seg.position == seg.chain_size - 1:
        return ("sleep", evidence)
    return None


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
        if tok in XARGS_VALUE_OPTS:
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
            # v0.2.105: `xargs -n1 bash poll.sh` runs the script per item.
            return _shell_input_pattern(body[i:], depth,
                                        seg._replace(in_loop=True))
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
    # Keywords, subshell/group punctuation (R6: `( sleep 5 )` — the
    # subshell's verb is what the parens wrap) and case heads/arms.
    body = _command_words(tokens)
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
        # v0.2.105: a shell with no -c runs a script file or its stdin.
        return _shell_input_pattern(body, depth, seg)
    if verb in _SOURCE_VERBS and len(body) > 1:
        # `source poll.sh` / `. poll.sh` (v0.2.105); `source /dev/stdin`
        # reads the pipe like a shell does.
        if body[1] == "/dev/stdin":
            return _stdin_pattern(body, depth, seg)
        return _script_file_pattern(body[1], depth, seg, direct=False)
    if "/" in body[0] and not body[0].startswith("$"):
        # `./poll.sh` / `scripts/poll.sh` (v0.2.105): judged only when the
        # file is a shell script (shell shebang, or none and a .sh name).
        return _script_file_pattern(body[0], depth, seg, direct=True)
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
            # v0.2.105: `timeout 600 bash poll.sh` / `nohup bash <<< …`.
            # No finding → keep scanning the tail exactly as before.
            found = _shell_input_pattern(tokens[i:], depth, seg)
            if found is not None:
                return found
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
    body = _command_words(seg.tokens)
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


# ═══════════════════════════════════════════════════════════════════════
# A BACKGROUND wait loop must be BOUNDED (v0.2.105 fix round 2, item 7)
# ═══════════════════════════════════════════════════════════════════════

#: Redirections that throw a command's errors away — the whole-token forms.
_STDERR_SILENCED_RE = re.compile(r"^(?:2|&)>>?/dev/null$")
#: …and the split form (``2> /dev/null``): the operator, then the sink.
_STDERR_SILENCED_SPLIT = frozenset({"2>", "2>>", "&>", "&>>"})
#: The PowerShell twin (``2>$null``, ``*>$null``).
_PS_STDERR_SILENCED_RE = re.compile(r"(?:\*|2)>>?\s*\$null", re.IGNORECASE)


def _silences_stderr(tokens: List[str]) -> bool:
    """True when these tokens carry a stderr-throwing redirection
    (``2>/dev/null``, ``&>/dev/null``, also split as ``2> /dev/null``)."""
    for k, tok in enumerate(tokens):
        if _STDERR_SILENCED_RE.match(tok):
            return True
        if (tok in _STDERR_SILENCED_SPLIT and k + 1 < len(tokens)
                and tokens[k + 1] == "/dev/null"):
            return True
    return False


def _peel_to_wrapper(tokens: List[str]) -> List[str]:
    """Leading wrapper verbs peeled down to the command a segment runs —
    :data:`WRAPPER_VERBS` (``sudo``/``nice``/``env``…) and the flag-only
    :data:`FLAG_WRAPPERS` (``nohup``/``setsid``/``doas``). ``timeout`` is
    NOT peeled: callers must see it to judge what it wraps."""
    words = peel_wrapper_verbs(tokens)
    while words and os.path.basename(words[0]).lower() in FLAG_WRAPPERS:
        words = peel_wrapper_verbs(words[1:])
    return words


def _timeout_options_end(words: List[str]) -> int:
    """The index of ``timeout``'s duration argument (its options, whose
    value flags take a separate token, skipped)."""
    i = 1
    while i < len(words) and words[i].startswith("-") and words[i] != "-":
        i += 2 if words[i] in TIMEOUT_VALUE_OPTS else 1
    return i


def _timeout_bounded(command: str, depth: int = 0) -> bool:
    """True when ``command`` is WRAPPED in a LITERAL ``timeout <N>`` (or
    ``gtimeout <N>``) — the timeout statement is the ONLY top-level
    statement, also through a ``bash -c`` / ``sh -c`` body that is itself
    so wrapped. One bounded segment must NOT bless an unbounded sibling
    (review E-1: ``timeout 5 ls; until …; do sleep 15; done`` used to
    pass), so a command with any other top-level statement is not
    bounded."""
    if depth > _MAX_TIMEOUT_SCAN_DEPTH:
        return False
    tokens = tokenize(command)
    if not tokens:
        return False
    segments = split_chain(tokens)
    if len(segments) != 1:
        return False
    words = _peel_to_wrapper(strip_env_assignments(segments[0]))
    if not words:
        return False
    verb = os.path.basename(words[0]).lower()
    if verb in ("timeout", "gtimeout"):
        i = _timeout_options_end(words)
        return i < len(words) and bool(_LITERAL_SLEEP_RE.match(words[i]))
    if verb in SHELL_DASH_C_VERBS:
        sub = dash_c_subcommand(words)
        if sub is not None:
            return _timeout_bounded(sub, depth + 1)
    return False


def _wrapped_shell_body(tokens: List[str]) -> Optional[str]:
    """The code a wrapper hands to a shell — ``timeout 10 bash -c '…'``,
    ``nohup sh -c '…'``. The chain walker recurses only a shell at the
    command position, so the background reading follows the wrapper
    itself."""
    words = _peel_to_wrapper(strip_env_assignments(tokens))
    if words and os.path.basename(words[0]).lower() in ("timeout", "gtimeout"):
        i = _timeout_options_end(words)
        words = _peel_to_wrapper(words[i + 1:]) if i < len(words) else []
    if words and os.path.basename(words[0]).lower() in SHELL_DASH_C_VERBS:
        return dash_c_subcommand(words)
    return None


def _background_loop_refusal(segments: List[ChainSegment],
                             rec: dict) -> Optional[tuple]:
    """``(pattern_name, evidence, stderr_silenced_in_condition)`` for a
    background command's loop that WAITS and is not bounded, else
    ``None``. Bounded = a proved literal iteration cap; a background
    ``while``/``until`` has none (its bound depends on the body's control
    flow). ``rec`` is one entry of :func:`_background_wait_pattern`'s scan."""
    if rec["iterations"] is not None:
        return None  # a literal iteration cap: bounded
    waiting = None
    for k in [rec["index"]] + rec["segments"]:
        found = _pattern_of_segment(segments[k], 0)
        if found is not None:
            waiting = found
            break
    if waiting is None:
        waiting = _busy_wait_pattern(segments, rec["index"])
    if waiting is None:
        return None  # a loop doing real work with no wait: not a wait loop
    hidden = any(_silences_stderr(strip_env_assignments(list(segments[k].tokens)))
                 for k in [rec["index"]] + rec["condition"])
    return (waiting[0], waiting[1], hidden)


def _background_wait_pattern(command: str,
                             _depth: int = 0) -> Optional[tuple]:
    """The background-loop rule (owner rulings 2026-10-29 / 2026-10-30):
    a ``run_in_background`` command is the sanctioned way to wait, and a
    background WAIT LOOP stays allowed only when it is BOUNDED — a literal
    iteration cap, or a whole-command ``timeout <N>``
    (:func:`_timeout_bounded`, checked by the caller). An unbounded wait
    loop is refused: when its condition never comes true it runs until a
    human notices (field case 2026-10-30: an ``until`` loop whose command
    was not on PATH ran 83 minutes). The returned evidence also says
    whether the loop's CONDITION throws stderr away — a command that
    cannot run then looks like "not yet" forever."""
    segments = walk_segments(command, _with_substitutions=True,
                             _with_function_bodies=False,
                             _with_assignments=True,
                             _with_shell_runs=True)
    if _depth < _MAX_TIMEOUT_SCAN_DEPTH:
        for seg in segments:
            body = _wrapped_shell_body(list(seg.tokens))
            if body is not None:
                found = _background_wait_pattern(body, _depth + 1)
                if found is not None:
                    return found
    by_depth: Dict[int, List[ChainSegment]] = {}
    for seg in segments:
        by_depth.setdefault(seg.depth, []).append(seg)
    for level in by_depth.values():  # `bash -c '…'` bodies are level 1
        stack: List[dict] = []
        for i, seg in enumerate(level):
            tokens = strip_env_assignments(list(seg.tokens))
            first = os.path.basename(tokens[0]).lower() if tokens else ""
            if first == "done":
                if stack:
                    found = _background_loop_refusal(level, stack.pop())
                    if found is not None:
                        return found
                continue
            if stack:
                if first == "do":
                    stack[-1]["in_body"] = True
                    stack[-1]["segments"].append(i)
                elif not stack[-1]["in_body"]:
                    stack[-1]["condition"].append(i)
                else:
                    stack[-1]["segments"].append(i)
            opened = segment_opener(tokens) if tokens else None
            if opened is not None:
                stack.append({"index": i, "iterations": loop_iterations(tokens),
                              "segments": [], "condition": [], "in_body": False})
    return None


def _background_refusal(tool: str, command: str,
                        readable: str) -> Optional[tuple]:
    """The one background decision for either shell: ``None`` when the
    command may run in the background, else
    ``(pattern, evidence, stderr_silenced, already_bounded)``.

    A silenced CONDITION refuses the loop even when it IS bounded (E-2:
    the promise "a loop that hides its errors is refused" is
    unconditional) — ``already_bounded`` then tells the message to drop
    the bound-it advice and name only the redirection."""
    if tool == POWERSHELL_TOOL:
        # PowerShell has no `timeout`; the bounded forms it does have (a
        # counter `for`, a `foreach` range) are what `loop_pattern`'s
        # budget reading proves. A background loop only has to TERMINATE —
        # it is not a foreground wait — so any PROVED literal bound is
        # enough and the token-saving budget does not apply.
        loop = loop_pattern(command, wait_budget_s=float("inf"))
        if loop is None:
            return None
        # E-7: only the loop's own condition (the evidence names it) can
        # decorate the refusal — a `2>$null` elsewhere in the command
        # says nothing about this loop.
        return (loop[0], loop[1],
                bool(_PS_STDERR_SILENCED_RE.search(loop[1])), False)
    found = _background_wait_pattern(readable)
    if found is None:
        return None
    bounded = _timeout_bounded(readable)
    if found[2]:
        return (found[0], found[1], True, bounded)
    if bounded:
        return None
    return (found[0], found[1], False, False)


# ═══════════════════════════════════════════════════════════════════════
# Code a shell runs from elsewhere: script files, stdin, variables
# (v0.2.105 — peer field report 2026-10-21, R9 base-equal gaps)
# ═══════════════════════════════════════════════════════════════════════

#: The largest script file the guard reads (bytes). A larger, unreadable,
#: binary or non-shell file passes through exactly as before v0.2.105.
SCRIPT_MAX_BYTES = 256 * 1024

#: How many script files deep the guard follows (a script that runs a
#: script that runs a script stops being followed at the third).
_SCRIPT_MAX_NESTING = 2

#: ``source FILE`` / ``. FILE`` run FILE in the current shell.
_SOURCE_VERBS = frozenset({"source", "."})

#: A shebang naming a POSIX-family shell (``#!/bin/bash``,
#: ``#!/usr/bin/env -S bash -e``): a directly executed file is judged only
#: then (or with no shebang and a ``.sh`` name — the kernel's fallback).
_SHELL_SHEBANG_RE = re.compile(
    r"^#!\s*\S*?/(?:env\s+(?:-\S+\s+)*)?(?:ba|da|z|k)?sh\b")

#: Shell options that take a separate VALUE token (``bash -o pipefail
#: x.sh``, ``bash --rcfile r x.sh``) — the value is not the script.
_SHELL_VALUE_OPTS = frozenset({"-o", "+o", "-O", "+O", "--rcfile",
                               "--init-file"})

#: Redirection operators whose target is the NEXT token.
_SPACED_REDIRECTS = frozenset({"<", ">", ">>", "<<<", "2>", "2>>", "&>",
                               "&>>", "1>", "<>"})


class _ScriptCtx:
    """Where script files resolve and how deep the guard already is.
    ``in_script`` switches the fixed-sleep rule to its script reading:
    only loops, ``watch`` and ``tail -f`` count, a single pause does not."""

    __slots__ = ("cwd", "nesting", "in_script")

    def __init__(self, cwd: str, nesting: int = 0, in_script: bool = False):
        self.cwd = cwd
        self.nesting = nesting
        self.in_script = in_script


#: Set by :func:`shell_static_pattern` when the caller names a working
#: directory (:func:`evaluate` always does). Unset → no file is read: the
#: classifier stays pure for every other caller.
_SCRIPT_CTX: "contextvars.ContextVar[Optional[_ScriptCtx]]" = (
    contextvars.ContextVar("vco_wait_guard_script_ctx", default=None))


def _judge_inline(text: str, depth: int, seg: ChainSegment) -> Optional[tuple]:
    """Code handed to a shell as TEXT (stdin, a here-string, a variable) is
    judged exactly like a ``bash -c`` body: same loop context, same
    position when it is the outer segment's whole content."""
    if depth >= _MAX_TIMEOUT_SCAN_DEPTH or not text.strip():
        return None
    return static_pattern(text, depth=depth + 1, _in_loop=seg.in_loop,
                          _position_override=(seg.position, seg.chain_size))


def _shell_operand(tokens: List[str]) -> tuple:
    """``(kind, value)`` for a shell invoked WITHOUT ``-c``
    (``tokens[0]`` is the shell): ``("herestring", text)``,
    ``("stdin", None)`` (``bash``, ``bash -s …``, ``bash /dev/stdin``,
    ``bash -``) or ``("file", path)``; ``(None, None)`` when the shape is
    not one of those."""
    i = 1
    n = len(tokens)
    reads_stdin_flag = False
    while i < n:
        t = tokens[i]
        if t == "<<<":
            return ("herestring", tokens[i + 1] if i + 1 < n else "")
        if t.startswith("<<<"):
            return ("herestring", t[3:])
        if t in _SPACED_REDIRECTS:
            i += 2
            continue
        if re.match(r"^\d*(>>?|<|&>>?)", t) or t.startswith(">") \
                or t.startswith("<"):
            i += 1
            continue
        if t == "--":
            i += 1
            break
        if t in _SHELL_VALUE_OPTS:
            i += 2
            continue
        if t.startswith("--"):
            i += 1
            continue
        if (t.startswith("-") or t.startswith("+")) and len(t) > 1:
            if "s" in t[1:]:
                reads_stdin_flag = True
            i += 1
            continue
        break
    # Redirections may also follow the operand; look for a here-string.
    for j in range(i, n):
        if tokens[j] == "<<<":
            return ("herestring", tokens[j + 1] if j + 1 < n else "")
        if tokens[j].startswith("<<<"):
            return ("herestring", tokens[j][3:])
    if reads_stdin_flag:
        return ("stdin", None)
    if i >= n or tokens[i] in ("-", "/dev/stdin"):
        return ("stdin", None)
    return ("file", tokens[i])


def _producer_text(producer: tuple) -> Optional[tuple]:
    """What a pipe producer feeds the shell: ``("code", text)`` for an
    ``echo``/``printf`` of literal words, ``("file", path)`` for
    ``cat FILE``; ``None`` for anything else (a non-literal word — ``$``
    or a backtick — is never guessed at)."""
    words = _command_words(peel_wrapper_verbs(strip_env_assignments(
        list(producer))))
    if not words:
        return None
    verb = os.path.basename(words[0]).lower()
    args = words[1:]
    if verb == "cat" and len(args) == 2 and args[0] == "<<<":
        return ("code", args[1])  # `cat <<EOF | bash` reaches here inlined
    if verb == "cat" and len(args) == 1 and not args[0].startswith("-"):
        return ("file", args[0])
    if any("$" in a or "`" in a for a in args):
        return None
    if verb == "echo":
        escapes = False
        while args and re.fullmatch(r"-[neE]+", args[0]):
            escapes = escapes or "e" in args[0]
            args = args[1:]
        text = " ".join(args)
        return ("code", text.replace("\\n", "\n") if escapes else text)
    if verb == "printf" and args:
        fmt, rest = args[0], args[1:]
        if not rest:
            return ("code", fmt.replace("\\n", "\n"))
        if fmt.replace("\\n", "").replace("%s", "").strip() == "":
            return ("code", "\n".join(rest))
    return None


def _stdin_pattern(tokens: List[str], depth: int,
                   seg: ChainSegment) -> Optional[tuple]:
    """A shell (or ``source /dev/stdin``) reading CODE from a pipe."""
    if not seg.piped_from:
        return None
    fed = _producer_text(seg.piped_from)
    if fed is None:
        return None
    kind, value = fed
    if kind == "code":
        return _judge_inline(value, depth, seg)
    return _script_file_pattern(value, depth, seg, direct=False)


def _shell_input_pattern(tokens: List[str], depth: int,
                         seg: ChainSegment) -> Optional[tuple]:
    """A shell invoked without ``-c``: judge the code it actually runs —
    a here-string (heredocs reach here as here-strings, see
    :func:`static_pattern`), the code piped into it, or its script file."""
    kind, value = _shell_operand(tokens)
    if kind == "herestring":
        return _judge_inline(value or "", depth, seg)
    if kind == "stdin":
        return _stdin_pattern(tokens, depth, seg)
    if kind == "file":
        return _script_file_pattern(value, depth, seg, direct=False)
    return None


def _resolve_script_path(word: str, cwd: str) -> Optional[Path]:
    if not word or any(c in word for c in "$`*?[]{}"):
        return None  # not a literal path: never guessed at
    if word.startswith("~"):
        word = os.path.expanduser(word)
    path = Path(word)
    if not path.is_absolute():
        path = Path(cwd) / path
    if os.sep != "\\" and "\\" in word and not path.exists():
        # A PowerShell-style `.\poll.ps1` read on a POSIX host.
        path = Path(cwd) / word.replace("\\", "/")
    return path


def read_script_text(word: str, cwd: str, *, direct: bool = False,
                     suffixes: tuple = (".sh",)) -> Optional[str]:
    """The text of the script file ``word`` names (resolved against
    ``cwd``), or ``None`` when it is not one the guard reads: not a
    literal path, missing, not a regular file, larger than
    :data:`SCRIPT_MAX_BYTES`, binary (a NUL byte), unreadable — or, for a
    DIRECT execution (``./poll.sh``), not a shell script (no shell
    shebang, and not a shebang-less file with one of ``suffixes``).
    Never raises."""
    path = _resolve_script_path(word, cwd)
    if path is None:
        return None
    try:
        st = path.stat()
        if not path.is_file() or st.st_size > SCRIPT_MAX_BYTES:
            return None
        data = path.read_bytes()[:SCRIPT_MAX_BYTES + 1]
    except (OSError, ValueError):
        return None
    if len(data) > SCRIPT_MAX_BYTES or b"\0" in data:
        return None
    text = data.decode("utf-8", errors="replace")
    if direct:
        first = text.split("\n", 1)[0]
        if first.startswith("#!"):
            if not _SHELL_SHEBANG_RE.match(first):
                return None
        elif not path.name.lower().endswith(suffixes):
            return None
    return text


def _script_file_pattern(word: str, depth: int, seg: ChainSegment, *,
                         direct: bool) -> Optional[tuple]:
    """Judge the shell script a command runs (``bash poll.sh``,
    ``source poll.sh``, ``./poll.sh``, ``cat poll.sh | bash``) with the
    same classifier, in its script reading (:class:`_ScriptCtx`): a loop
    that waits, a busy-wait or condition-refresh loop, ``watch`` or
    ``tail -f`` inside the file is a poll; a single fixed pause is not.
    A loop whose worst-case total sleep is proved within the budget is a
    bounded retry (:mod:`vco_lib.wait_guard_bounded`) unless the script
    itself runs in a loop. Read only when a working directory is known
    (:data:`_SCRIPT_CTX`)."""
    ctx = _SCRIPT_CTX.get()
    if ctx is None or ctx.nesting >= _SCRIPT_MAX_NESTING:
        return None
    text = read_script_text(word, ctx.cwd, direct=direct)
    if text is None:
        return None
    token = _SCRIPT_CTX.set(_ScriptCtx(ctx.cwd, ctx.nesting + 1, True))
    try:
        if literal_text_only(text):
            return None
        found = static_pattern(text, depth=depth + 1, _in_loop=seg.in_loop,
                               _bounded_loops=not seg.in_loop)
    finally:
        _SCRIPT_CTX.reset(token)
    if found is None:
        return None
    name, evidence = found
    return ("%s in script %s" % (name, word), evidence)


def ps_script_pattern(command: str, cwd: str) -> Optional[tuple]:
    """The PowerShell twin of the script-file rule (v0.2.105): a
    ``.ps1`` the command runs (``.\\poll.ps1``, ``& .\\poll.ps1``,
    ``. .\\poll.ps1``, ``pwsh -File poll.ps1``) is read and judged by the
    PowerShell rules (its Bash reading through :func:`static_pattern`,
    then :func:`~vco_lib.powershell_command.loop_pattern`), in the script
    reading. A ``bash poll.sh`` from PowerShell is judged through the
    Bash reading by :func:`shell_static_pattern`."""
    ctx = _SCRIPT_CTX.get()
    nesting = ctx.nesting if ctx is not None else 0
    if nesting >= _SCRIPT_MAX_NESTING:
        return None
    for stmt in ps_statements(command):
        if not stmt:
            continue
        word = None
        first = stmt[0]
        if first.lower().endswith(".ps1"):
            word = first
        elif os.path.basename(first).lower().removesuffix(".exe") in (
                "pwsh", "powershell"):
            for k in range(1, len(stmt) - 1):
                if stmt[k].lower() in ("-file", "-f"):
                    word = stmt[k + 1]
                    break
        if word is None:
            continue
        text = read_script_text(word, cwd, suffixes=(".ps1",))
        if text is None:
            continue
        token = _SCRIPT_CTX.set(_ScriptCtx(cwd, nesting + 1, True))
        try:
            found = static_pattern(command_for_bash_readers(
                POWERSHELL_TOOL, text))
            if found is None:
                # The bounded-wait budget (v0.2.105 fix round) only when
                # nothing loops around this script's call.
                found = loop_pattern(
                    text, wait_budget_s=(0.0 if ps_has_loop(command)
                                         else WAIT_BUDGET_S))
            if found is None:
                found = ps_script_pattern(text, cwd)
        finally:
            _SCRIPT_CTX.reset(token)
        if found is not None:
            name, evidence = found
            if " in script " not in name:
                name = "%s in script %s" % (name, word)
            return (name, evidence)
    return None


#: Statements that assign shell variables from their arguments.
_DECLARE_VERBS = frozenset({"export", "declare", "local", "readonly",
                            "typeset"})
_VAR_REF_RE = re.compile(r"^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))$")


def _note_assignments(seg: ChainSegment, values: Dict[str, str]) -> None:
    """Track ``NAME=<literal>`` from an assignment-only statement or an
    ``export``/``declare``/``local`` statement; a non-literal value
    (``$`` or a backtick) forgets the name — it is never guessed at."""
    words: List[str] = []
    if not seg.tokens:
        words = list(seg.assignments)
    else:
        body = _command_words(seg.tokens)
        if body and body[0] in _DECLARE_VERBS:
            words = [w for w in body[1:] if not w.startswith("-")]
    for w in words:
        m = _ASSIGN_RE.match(w)
        if m is None:
            continue
        value = w[m.end():]
        if "$" in value or "`" in value or "+=" in w[:m.end()]:
            values.pop(m.group(1), None)
        else:
            values[m.group(1)] = value


def _var_ref(word: str) -> Optional[str]:
    m = _VAR_REF_RE.match(word)
    return (m.group(1) or m.group(2)) if m else None


def _variable_code(seg: ChainSegment, values: Dict[str, str]) -> Optional[str]:
    """The code a segment runs out of a variable assigned a literal EARLIER
    in the same command: ``eval "$c"``, ``$c args`` as the command word,
    ``bash -c "$c"``. ``None`` otherwise."""
    if not values or not seg.tokens:
        return None
    body = _command_words(seg.tokens)
    if not body:
        return None
    verb = os.path.basename(body[0]).lower()
    name = _var_ref(body[0])
    if name is not None and name in values:
        return " ".join([values[name]] + body[1:])
    if verb == "eval" and len(body) == 2:
        ref = _var_ref(body[1])
        if ref is not None and ref in values:
            return values[ref]
    if verb in SHELL_DASH_C_VERBS:
        sub = dash_c_subcommand(body)
        ref = _var_ref(sub) if sub is not None else None
        if ref is not None and ref in values:
            return values[ref]
    return None


def _heredoc_feeds_shell(line: str) -> bool:
    """True when a heredoc opener LINE hands its body to a shell (or to
    ``source``), so the body is CODE: the line's tokens hold a shell word
    or ``source``/``.`` in command position."""
    toks = tokenize(line) or []
    for _op, segment in split_chain_ops(toks):
        words = _command_words(peel_wrapper_verbs(strip_env_assignments(
            segment)))
        if not words:
            continue
        verb = os.path.basename(words[0]).lower()
        if verb in SHELL_DASH_C_VERBS and dash_c_subcommand(words) is None:
            return True
        if verb in _SOURCE_VERBS and len(words) > 1 and words[1] == "/dev/stdin":
            return True
    return False


def static_pattern(command: str, depth: int = 0, *,
                   _in_loop: bool = False,
                   _position_override: Optional[tuple] = None,
                   _bounded_loops: bool = False) -> Optional[tuple]:
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
    pass nothing. ``_bounded_loops`` (v0.2.105 fix round) is set only by
    :func:`_script_file_pattern` for a script that does not itself run in
    a loop: a plain ``sleep`` in a counted ``for`` loop whose worst-case
    total sleep is within :data:`~vco_lib.wait_guard_bounded.WAIT_BUDGET_S`
    is a bounded retry, not a poll
    (:func:`~vco_lib.wait_guard_bounded.bounded_loop_sleeps`)."""
    if not isinstance(command, str) or not command.strip():
        return None
    # A heredoc body is data — EXCEPT when its line hands it to a shell
    # (`bash <<'EOF'`, `cat <<EOF | bash`): then it is inlined as the
    # here-string it amounts to and judged as code (v0.2.105).
    command, _heredoc_bodies = strip_heredocs(
        command, inline_when=_heredoc_feeds_shell)
    ctx = _SCRIPT_CTX.get()
    # In a script FILE a function body is judged where it is defined, not
    # re-positioned at its calls: a retry pause in a helper that a loop
    # calls once per item is not a poll (v0.2.105; a loop INSIDE the
    # function still is).
    in_script = ctx is not None and ctx.in_script
    segments = walk_segments(command, _in_loop=_in_loop,
                             _position_override=_position_override,
                             _with_substitutions=True,
                             _with_function_bodies=not in_script,
                             _with_assignments=True,
                             _with_shell_runs=True)
    bounded = (bounded_loop_sleeps(segments)
               if _bounded_loops and in_script else frozenset())
    values: Dict[str, str] = {}
    for i, seg in enumerate(segments):
        # Code held in a variable assigned a literal earlier in the same
        # command (v0.2.105, R9): `c='sleep 5'; eval "$c"` / `$c` /
        # `bash -c "$c"` (the recursed `$c` sits one level down).
        code = _variable_code(seg, values)
        if code is not None:
            found = _judge_inline(code, depth, seg)
            if found is not None:
                return found
        if seg.depth == 0:
            _note_assignments(seg, values)
        found = None if i in bounded else _pattern_of_segment(seg, depth)
        if found is not None:
            return found
        busy = _busy_wait_pattern(segments, i)
        if busy is not None:
            return busy
    return None


def shell_static_pattern(tool: str, command: str,
                         readable: Optional[str] = None, *,
                         cwd: Optional[str] = None) -> Optional[tuple]:
    """The static (wait-shape) decision for one shell command — the ONE
    place :func:`evaluate` asks it. ``readable`` is the Bash reading
    (:func:`~vco_lib.powershell_command.command_for_bash_readers`).

    The literal-text exception (v0.2.104 E-4, owner-approved allowlist
    inversion) is consulted FIRST and only for the Bash tool: when every
    simple command is an allowlisted program that never runs its arguments
    as code, and no substitution in the line would be expanded
    (:func:`vco_lib.wait_guard_literal.literal_text_only`), a ``$(sleep 5)``
    in it is quoted search text — ``grep -n '$(sleep 5)' f`` — and no wait.
    Every other command gets :func:`static_pattern`, the v0.2.103 reading.

    ``cwd`` (v0.2.105): the directory relative script paths resolve
    against. Given, the script FILES the command runs (``bash poll.sh``,
    ``source poll.sh``, ``./poll.sh``, ``cat poll.sh | bash``; on
    PowerShell ``.\\poll.ps1`` / ``pwsh -File poll.ps1``) are read — at
    most :data:`SCRIPT_MAX_BYTES` each — and judged too; omitted, no file
    is read."""
    if readable is None:
        readable = command_for_bash_readers(tool, command)
    if tool != POWERSHELL_TOOL and literal_text_only(command):
        return None
    if cwd is None:
        return static_pattern(readable)
    token = _SCRIPT_CTX.set(_ScriptCtx(cwd))
    try:
        found = static_pattern(readable)
        if found is None and tool == POWERSHELL_TOOL:
            found = ps_script_pattern(command, cwd)
        return found
    finally:
        _SCRIPT_CTX.reset(token)


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


def noop_repeat_key(command: str) -> Optional[str]:
    """The repeat key of a call that does NOTHING (v0.2.105): every
    statement is ``true``, ``:``, ``exit`` (with no argument or ``0``),
    ``sleep 0``, or a bare ``echo`` / ``printf`` / ``Write-Host`` (no
    output redirection, no pipe, no command substitution — those do or
    feed work). ``None`` for anything else, so a single real command
    anywhere in the line is never counted. The key is the token-joined
    command: a DIFFERENT no-op is a different key."""
    if "$(" in command or "`" in command:
        return None
    tokens = tokenize(command)
    if not tokens:
        return None
    for op, segment in split_chain_ops(tokens):
        if op == "|":
            return None
        words = _command_words(segment)
        if not words:
            return None
        verb = os.path.basename(words[0]).lower()
        if verb == "sleep":
            if words[1:] != ["0"]:
                return None
            continue
        if verb == "exit":
            # `exit 0` ends the shell having done nothing; `exit 1` (or any
            # other status) reports a result, so it is not a no-op.
            if words[1:] not in ([], ["0"]):
                return None
            continue
        if verb not in _NOOP_VERBS:
            return None
        if any(_is_redirect_token(t) for t in _real_output_tokens(words[1:])):
            return None
    return "noop:" + " ".join(tokens)


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


def background_wait_deny_reason(command: str, pattern: tuple,
                                tool: str = "Bash") -> str:
    """The refusal for a background wait loop that is UNBOUNDED (fix round
    2, item 7) or hides its condition's stderr (fix round 3, E-2): names
    the loop, shows the bounded form, and — when the loop's condition
    throws stderr away — says why that makes a broken command look like
    "not yet" forever."""
    name, evidence, stderr_silenced, already_bounded = pattern
    ps = tool == POWERSHELL_TOOL
    # E-3: the PowerShell branch never accepts a `timeout` wrapper on an
    # inline loop, so its advice must LEAD with the form that works there
    # (a counter `for`); a `timeout 600 pwsh -File` bounds only when the
    # loop lives in the file.
    bounded = (
        "give the loop a counter `for` with a literal bound"
        " (`for ($i = 0; $i -lt 5; $i++) { … }`), or move it into a file"
        " and wrap the whole call in a literal timeout"
        " (`timeout 600 pwsh -File check.ps1`)"
        if ps else
        "wrap the whole command in a literal timeout"
        " (e.g. `timeout 600 <your command>`), or give the loop a literal"
        " iteration cap (`for i in 1 2 3 …`)")
    if already_bounded:
        # E-2: the loop IS bounded; only its silenced condition refuses it.
        reason = (
            f"BLOCKED by the VCO wait-loop guard: this background "
            f"{_shell_label(tool)} command's wait loop (`{name}`:"
            f" {evidence}) throws its CONDITION's stderr away"
            f" (`2>/dev/null`): a command that cannot run then looks like"
            f" \"not yet\" until the timeout kills the loop, and the real"
            f" error is never shown — drop the redirection so it is"
            f" visible."
        )
    else:
        reason = (
            f"BLOCKED by the VCO wait-loop guard: this background "
            f"{_shell_label(tool)} command starts an UNBOUNDED wait loop "
            f"(`{name}`: {evidence}). A background loop must be BOUNDED — "
            f"{bounded} — so it cannot run until someone notices when its "
            f"condition never comes true."
        )
        if stderr_silenced:
            reason += (
                " Its CONDITION throws stderr away (`2>/dev/null`): a command "
                "that cannot run then looks like \"not yet\" forever — drop the "
                "redirection so the real error is visible."
            )
    return (
        reason + _WHY +
        " WHAT TO DO INSTEAD: bound the loop as above, start it ONCE with"
        " run_in_background: true, and END YOUR TURN; the completion"
        " notification wakes you." + _KNOB
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


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "%dth" % n
    return "%d%s" % (n, {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def noop_deny_reason(key: str, count: int, window_s: int,
                     tool: str = "") -> str:
    shown = key.split(":", 1)[1] if ":" in key else key
    return (
        f"BLOCKED by the VCO wait-loop guard: this is the {_ordinal(count)} "
        f"identical no-op {_shell_label(tool or 'Bash')} call (`{shown}`) "
        f"within {window_s}s — a wait loop that does nothing." + _WHY +
        " WHAT TO DO INSTEAD: end your turn; the completion notification"
        " wakes you (a run_in_background command or a subagent notifies you"
        " when it finishes)." + _KNOB
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


def _prune_window(env: Dict[str, str]) -> int:
    """The window every load prunes the state with: the widest of the
    detectors sharing the per-actor file, so loading for one detector
    never drops another's still-live stamps (each detector counts only
    the stamps inside its OWN window)."""
    return max(env_int("VCO_WAIT_GUARD_REPEAT_WINDOW_S", DEFAULT_REPEAT_WINDOW_S,
                       env=env, minimum=1), NOOP_REPEAT_WINDOW_S)


def _state_path(payload: Dict[str, Any], state_root: Optional[Path]) -> Path:
    return _state_dir(state_root) / ("wait_%s.json" % _actor_key(payload))


def _payload_cwd(payload: Dict[str, Any]) -> str:
    """The directory a command runs in: the hook input's ``cwd`` (a
    documented common input field), else this process's own."""
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    return os.getcwd()


def _stderr(line: str) -> None:
    sys.stderr.write("[wait-guard] %s\n" % line)


# ═══════════════════════════════════════════════════════════════════════
# Decision
# ═══════════════════════════════════════════════════════════════════════


def _repeat_check(
    payload: Dict[str, Any], key: str, env: Dict[str, str],
    state_root: Optional[Path], now: float, tool: str = "", *,
    window_s: Optional[int] = None, max_n: Optional[int] = None,
    reason=None,
) -> Optional[str]:
    """Count one call of ``key`` and deny the ``max_n``-th within
    ``window_s`` (defaults: the status-check knobs). ``reason`` builds the
    deny text from ``(key, count, window_s, tool)`` (default
    :func:`repeat_deny_reason`)."""
    if window_s is None:
        window_s = env_int("VCO_WAIT_GUARD_REPEAT_WINDOW_S",
                           DEFAULT_REPEAT_WINDOW_S, env=env, minimum=1)
    if max_n is None:
        max_n = env_int("VCO_WAIT_GUARD_REPEAT_MAX", DEFAULT_REPEAT_MAX,
                        env=env, minimum=1)
    reason = repeat_deny_reason if reason is None else reason
    path = _state_path(payload, state_root)
    tool_use_id = payload.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        tool_use_id = None
    from vco_lib.atomic import portable_exclusive_lock

    # The `if`-filtered registrations fire this hook CONCURRENTLY for one
    # tool call: the read-modify-write runs under the shared cross-OS lock
    # so the concurrent spawns serialize instead of racing the state file.
    with portable_exclusive_lock(path.with_name(path.name + ".lock")):
        state = _load_state(path, now, max(window_s, _prune_window(env)),
                            SLASH_TTL_S)
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
        return reason(key, count, window_s, tool)
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
        if tool == "CronCreate" and tool_input.get("recurring") is False:
            # Owner ruling 2026-10-29 (#508): a ONE-SHOT scheduled wake-up
            # fires once — it is not a poll. Only the explicit
            # `recurring: false` counts; a missing or non-boolean field is
            # the recurring default and keeps the rule below.
            return None
        path = _state_path(payload, state_root)
        state = _load_state(path, now, _prune_window(env), SLASH_TTL_S)
        if state["slash"]:
            return None
        return self_wake_main_reason(str(tool))

    if tool in SHELL_TOOLS:
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        if tool_input.get("run_in_background"):
            # The sanctioned trigger: a backgrounded command never polls —
            # but its WAIT LOOP must be bounded (fix round 2, item 7): an
            # unbounded loop whose condition never comes true runs until a
            # human notices.
            readable_bg = command_for_bash_readers(tool, command)
            found_bg = _background_refusal(str(tool), command, readable_bg)
            if found_bg is not None:
                return background_wait_deny_reason(command, found_bg, str(tool))
            return None
        # PowerShell (v0.2.102 W5-D): read through its Bash-equivalent form
        # — one detector for both shells (vco_lib.powershell_command).
        readable = command_for_bash_readers(tool, command)
        pattern = shell_static_pattern(str(tool), command, readable,
                                       cwd=_payload_cwd(payload))
        if pattern is not None:
            return static_deny_reason(command, pattern, str(tool))
        if tool == POWERSHELL_TOOL:
            # A loop that waits, with or without a Start-Sleep inside
            # (review W5D-2): judged on the PowerShell text itself.
            loop = loop_pattern(command)
            if loop is not None:
                return static_deny_reason(command, loop, str(tool), translated=False)
        noop = noop_repeat_key(readable)
        if noop is not None:
            return _repeat_check(payload, noop, env, state_root, now, str(tool),
                                 window_s=NOOP_REPEAT_WINDOW_S,
                                 max_n=NOOP_REPEAT_MAX,
                                 reason=noop_deny_reason)
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
    path = _state_path(payload, state_root)
    state = _load_state(path, now, _prune_window(env), SLASH_TTL_S)
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
