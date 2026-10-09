# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The wait-loop / polling guard (v0.2.102 lane W4-D, owner 2026-10-08).

ONE home for every decision about agents "waiting" by polling: foreground
``sleep`` / ``watch`` / ``tail -f`` / ``timeout ... bash -c '...sleep...'``
loops, self-scheduling tools (``ScheduleWakeup`` / ``CronCreate`` /
``Monitor``), and repeated identical status checks (``tail``/``head``/
``cat``/``ls``/``wc``/``grep`` on the same target, ``Read`` of the same
file_path+offset+limit). Every poll is a new request that re-sends the
whole conversation; the sanctioned way to wait is to start the work once
with ``run_in_background`` and end the turn — the completion notification
wakes the agent. The guard makes that the ONLY easy path.

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
``session_id`` + ``agent_id``-or-main). Timestamps are pruned to the
window on every touch and slash unlocks to their own longer TTL; an empty
file is deleted. This NEVER touches the durable injection seen-store.
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
    SHELL_DASH_C_VERBS,
    peel_wrapper_verbs,
    split_chain,
    strip_env_assignments,
    tokenize,
    walk_command,
)
from vco_lib.powershell_command import (
    POWERSHELL_TOOL,
    PS_WAIT_VERBS,
    SHELL_TOOLS,
    command_for_bash_readers,
)

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
SELF_WAKE_TOOLS = frozenset({"ScheduleWakeup", "CronCreate", "Monitor"})

#: First-verb table of the repeat detector: a read-only command whose
#: normalized form repeats (same verb, same tokens) N times in the window.
REPEAT_VERBS = frozenset({"tail", "head", "cat", "ls", "wc", "grep"})

#: v0.2.102 W5-D — the PowerShell twin ``if`` rules of the wait-guard
#: PreToolUse group (matcher ``Bash|PowerShell``; a single ``if`` rule
#: matches ONE tool's calls — vendor hooks doc — so the PowerShell tool gets
#: its own handlers). The cmdlets whose Bash reading is a pattern or repeat
#: verb above (derived from the ONE PowerShell table; cmdlet names only —
#: Claude Code canonicalizes aliases before matching, vendor permissions doc
#: "PowerShell"), the foreground waits, ``Receive-Job`` (its ``-Wait`` form
#: blocks) and Windows ``timeout.exe`` (a native executable, so no alias
#: canonicalization covers it). NO loop-keyword rules: ``while``/``do``/
#: ``for``/``foreach`` are language keywords, not commands, so a rule keyed
#: on one can never match — a loop body is caught by the cmdlet inside it
#: (``Start-Sleep``), each subcommand being checked on its own.
#: MUST MATCH the ``PowerShell(...)`` rules of the wait-guard group in BOTH
#: settings templates — locked by tests/test_v02102_powershell_hooks.py.
_PS_RULE_BASH_VERBS = ("sleep", "cat", "ls", "wc", "grep",
                       "wait-job", "wait-process", "wait-event")
_PS_RULE_EXTRA = ("Receive-Job", "timeout")


def powershell_rule_spellings() -> List[str]:
    """The command spellings the wait-guard's PowerShell twin rules cover."""
    from vco_lib.powershell_command import spellings_for_bash_verbs

    return spellings_for_bash_verbs(_PS_RULE_BASH_VERBS) + list(_PS_RULE_EXTRA)


DEFAULT_REPEAT_MAX = 4
DEFAULT_REPEAT_WINDOW_S = 600
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


def _env_int(env: Dict[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return value if value > 0 else default


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

#: Shell keywords that prefix the command inside a compound body — the
#: chain walker splits ``do sleep 2; done`` into a ``do sleep 2`` segment
#: and the VERB is what follows the keyword.
_LEADING_KEYWORDS = frozenset({"do", "then", "else", "{", "!", "time"})


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


def _pattern_of_segment(tokens: List[str], depth: int) -> Optional[tuple]:
    body = tokens
    while body and os.path.basename(body[0]).lower() in _LEADING_KEYWORDS:
        body = body[1:]
    if not body:
        return None
    verb = os.path.basename(body[0]).lower()
    if verb == "sleep":
        return ("sleep", " ".join(body[:2]))
    if verb == "watch":
        return ("watch", body[0])
    if verb == "tail" and _tail_follows(body[1:]):
        return ("tail --follow", " ".join(body[:3]))
    if verb in _SCAN_WRAPPER_VERBS:
        return _scan_wrapped(body, depth)
    if verb in PS_WAIT_VERBS:
        # PowerShell's foreground waits (Wait-Job / Wait-Process /
        # Wait-Event, and Receive-Job -Wait) — the translated name; no Bash
        # command starts with one, so this never fires on a Bash call.
        return (verb, " ".join(body[:3]))
    return None


def _scan_wrapped(tokens: List[str], depth: int) -> Optional[tuple]:
    """``timeout 30 bash -c 'sleep 30'`` and kin: the chain walker hands the
    whole segment to the collector with ``timeout`` as the verb, so the
    wrapped tail is scanned here — pattern verbs directly, and a nested
    ``<shell> -c '<sub>'`` recursively through :func:`static_pattern`."""
    if depth >= _MAX_TIMEOUT_SCAN_DEPTH:
        return None
    for i in range(1, len(tokens)):
        base = os.path.basename(tokens[i]).lower()
        if base in ("sleep", "watch"):
            return (base, " ".join(tokens[: i + 2])[:80])
        if base in SHELL_DASH_C_VERBS:
            rest = tokens[i + 1:]
            has_c = bool(rest) and (
                rest[0] == "-c"
                or (
                    rest[0].startswith("-")
                    and not rest[0].startswith("--")
                    and "c" in rest[0][1:]
                )
            )
            if has_c and len(rest) >= 2:
                return static_pattern(rest[1], depth=depth + 1)
    return None


def static_pattern(command: str, depth: int = 0) -> Optional[tuple]:
    """``(pattern_name, evidence)`` for the first polling shape in
    ``command``, else ``None``.

    Parsed with the ONE chain walker (:mod:`vco_lib.bash_command_walk`) —
    never a raw-string scan — so quoted text (``echo "sleep"``) cannot
    trip it and ``bash -c`` sub-commands are re-parsed. ``while``/``until``
    loops need no rule of their own: the walker splits the loop body into
    segments and the ``sleep`` segment inside it IS the detection."""
    if not isinstance(command, str) or not command.strip():
        return None

    # The walker's Collector contract is List[str]; the finding rides as
    # "name\x1fevidence" and is split back apart below.
    def collect(tokens: List[str]) -> List[str]:
        found = _pattern_of_segment(tokens, depth)
        return ["%s\x1f%s" % found] if found else []

    hits = walk_command(command, collect)
    if not hits:
        return None
    name, _, evidence = hits[0].partition("\x1f")
    return (name, evidence)


# ═══════════════════════════════════════════════════════════════════════
# Repeat-detector keys
# ═══════════════════════════════════════════════════════════════════════


def bash_repeat_key(command: str, prefix: str = "bash") -> Optional[str]:
    """The normalized repeat key for a Bash STATUS CHECK, or ``None`` when
    the command is not one (first peeled verb outside :data:`REPEAT_VERBS`).

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


def static_deny_reason(command: str, pattern: tuple, tool: str = "Bash") -> str:
    name, evidence = pattern
    ps = tool == POWERSHELL_TOOL
    read_as = f" (read as `{evidence}`)" if ps else f" ({evidence})"
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
    raw_checks = data.get("checks")
    checks = {
        k: [t for t in v if isinstance(t, (int, float)) and now - t <= window_s]
        for k, v in raw_checks.items() if isinstance(v, list)
    } if isinstance(raw_checks, dict) else {}
    checks = {k: v for k, v in checks.items() if v}
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
    window_s = _env_int(env, "VCO_WAIT_GUARD_REPEAT_WINDOW_S",
                        DEFAULT_REPEAT_WINDOW_S)
    max_n = _env_int(env, "VCO_WAIT_GUARD_REPEAT_MAX", DEFAULT_REPEAT_MAX)
    path = _state_path(payload, state_root)
    state = _load_state(path, now, window_s, SLASH_TTL_S)
    stamps = state["checks"].setdefault(key, [])
    stamps.append(now)
    state["checks"][key] = [t for t in stamps if now - t <= window_s]
    _save_state(path, state)
    if len(state["checks"][key]) >= max_n:
        return repeat_deny_reason(key, len(state["checks"][key]), window_s, tool)
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
        window_s = _env_int(env, "VCO_WAIT_GUARD_REPEAT_WINDOW_S",
                            DEFAULT_REPEAT_WINDOW_S)
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
    window_s = _env_int(env, "VCO_WAIT_GUARD_REPEAT_WINDOW_S",
                        DEFAULT_REPEAT_WINDOW_S)
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
