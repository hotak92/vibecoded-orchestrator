# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Kill guard (v0.2.105): refuse process kills that can take down the
user's session. ONE home for both layers (A>B>C: shell hooks call this
module; nothing mirrors it).

Why: on 2026-10-23 an agent ran ``kill -9 $p $(ps -o ppid= -p $p)`` on a
leaked test process. That process was an orphan, its parent was
``systemd --user``, and SIGKILL on the user manager ended the whole desktop
session. Earlier, ``pkill -f <pattern>`` had twice killed the agent's own
shell (its command line contains the pattern). The owner wanted a hook,
not a prompt rule.

Layer 1 — static deny (:func:`static_check`)
============================================
Called by the pre-tool-use security scanner (``.claude/scripts/
bash_security.py``) for every Bash / PowerShell tool call. Refuses:

* ``kill`` aimed at ``1``, ``-1``, ``0``, ``$$`` / ``-$$`` (the shell and
  its process group), ``$PPID``, or any kill in a command that looks up a
  PARENT pid (``$PPID``, ``ps -o ppid``, ``/proc/<pid>/stat[us]``,
  ``pgrep -P``, ``ParentProcessId``, ``.Parent``);
* ``pkill`` / ``killall`` with a short or broad pattern (a shell, an
  interpreter, a desktop process), ``pkill -f`` without an ANCHORED
  (``^…``), specific pattern, ``pkill -v``, selector-only kills
  (``pkill -u me``), ``killall5`` and ``skill``;
* the forms that would bypass layer 2: ``builtin kill``, ``command kill``,
  and a kill binary called by path (``/bin/kill``);
* PowerShell: ``Stop-Process`` / ``spps`` / ``kill`` on ``$PID``, pid 0/4,
  a parent lookup, a protected or wildcard name, or a bare
  ``Get-Process | Stop-Process``; ``taskkill`` with ``/FI``, pid 0/4 or a
  protected / wildcard image name.

Layer 2 — runtime guard (:func:`build_rewrite` + ``check``)
============================================================
The PreToolUse rewrite hook (``lean-ctx-rewrite.{sh,ps1}`` — the ONE hook
that emits ``updatedInput`` for shell tools; two emitters for one call
conflict) asks :func:`build_rewrite` first. For a command that runs a kill
verb it prefixes the command with guard definitions:

* Bash: a ``kill`` function (exported, so ``bash -c`` children see it) and
  a PATH directory of ``kill``/``pkill``/``killall`` shims under
  ``<project>/.claude/state/kill-guard/bin`` (so ``xargs``, ``env`` and
  ``timeout`` children go through it too);
* PowerShell: ``Stop-Process`` (which the ``kill``/``spps`` aliases resolve
  to) and ``taskkill`` functions.

Each calls ``python -m vco_lib.kill_guard check <verb> -- <args>``, which
resolves every pid the call would signal and refuses pid 1, the shell's
ancestors, its session leader and the desktop-critical names
(:mod:`vco_lib.kill_guard_runtime`). Everything else runs unchanged.

Kill switch: ``VCO_KILL_GUARD=off`` (``0``/``false``/``no``) in the
environment Claude Code runs the hooks with — for the USER only, never set
by an agent; a value set inside the command itself changes nothing.

Known limits: ``os.kill`` from Python (or any other language), kills inside
a script FILE the command runs, ``sudo`` (it resets PATH), ``sh -c`` /
``dash`` children (no exported functions; their builtin ``kill``), an
absolute-path kill inside a script, and the Bash tool on Windows (Git Bash's
``kill`` takes MSYS pids, which the Windows process table cannot map) are not
covered by layer 2.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from vco_lib import bash_command_walk as bcw
from vco_lib import kill_guard_runtime as rt
from vco_lib.atomic import atomic_write_bytes

KILL_SWITCH_ENV = "VCO_KILL_GUARD"
_OFF_VALUES = frozenset({"off", "0", "false", "no"})

#: Exit code of ``check`` when it refuses (the guard functions treat ANY
#: non-zero as a refusal — a crash refuses too).
EXIT_REFUSED = 3

#: Verbs layer 1 recognises in a Bash command.
BASH_KILL_VERBS = frozenset({"kill", "pkill", "killall", "killall5", "skill",
                             "taskkill"})
#: PowerShell spellings of Stop-Process (``kill`` is its alias).
PS_STOP_VERBS = frozenset({"stop-process", "spps", "kill",
                           "microsoft.powershell.management\\stop-process"})

#: Names whose unqualified kill hits far more than the process meant:
#: shells, interpreters, runtimes, editors and the agent itself. Together
#: with the runtime module's protected desktop names they form the
#: "broad" set a pkill/killall/Stop-Process name is judged against.
BROAD_NAMES = frozenset({
    "sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "pwsh",
    "powershell", "cmd", "conhost", "python", "python3", "python2", "py",
    "node", "deno", "bun", "java", "ruby", "perl", "php", "code", "claude",
    "electron", "tmux", "screen", "sudo", "su", "ssh", "ssh-agent", "sleep",
    "git", "cargo", "npm", "npx", "windowsterminal", "openconsole",
})

_SAFE_PATTERN = (
    " SAFE PATTERN: kill only pids you started yourself ($! right after the "
    "start, or a pid you recorded) or processes that carry a unique env "
    "token you set when starting them (scan /proc/*/environ for the token "
    "and signal only those pids — see _reap_by_env_token in VCO's "
    "tests/test_v0292_kg_sync_gate_selfcontained.py). Never compute a target "
    "from a parent lookup: the parent of an orphan is `systemd --user` or "
    "init, and killing it ends the user's desktop session."
)
_KNOB = (
    " For the USER only (your decision, never the agent's): "
    "VCO_KILL_GUARD=off disables this guard."
)


def guard_disabled(env: Optional[Dict[str, str]] = None) -> bool:
    """True when the owner switched the guard off."""
    e = os.environ if env is None else env
    return str(e.get(KILL_SWITCH_ENV, "")).strip().lower() in _OFF_VALUES


def refusal_message(reason: str) -> str:
    """The one wording both layers print."""
    return "[kill_guard] Refused: %s.%s%s" % (reason, _SAFE_PATTERN, _KNOB)


# ═══════════════════════════════════════════════════════════════════════
# Layer 1 — Bash
# ═══════════════════════════════════════════════════════════════════════

#: A parent-pid lookup anywhere in the command text.
_PARENT_LOOKUP = re.compile(
    r"\$\{?PPID\b|\bppid\b|/proc/[^\s;|&]*/stat(?:us)?\b"
    r"|\bpgrep\b[^;|&\n]*\s-[A-Za-z]*P\b|\bgetppid\b|\bParentProcessId\b"
    r"|\.Parent\b",
    re.IGNORECASE,
)

_basename = bcw.command_basename


def _peel_to_kill(tokens: Sequence[str]) -> Optional[Tuple[str, List[str], str]]:
    """``(verb, args, bypass)`` when the segment runs a kill verb, else None.
    ``bypass`` names a form that would dodge the runtime guard ("" if none).
    ``via_xargs`` is folded into bypass="" — its targets come from stdin.
    The wrapper peeling is the shared :func:`bcw.peel_to_command`."""
    peeled = bcw.peel_to_command(list(tokens))
    if peeled is None:
        return None
    toks, bypass = peeled
    verb = _basename(toks[0])
    if verb.endswith(".exe"):
        verb = verb[:-4]
    if verb not in BASH_KILL_VERBS:
        return None
    if ("/" in toks[0] or "\\" in toks[0]) and not bypass:
        bypass = "a kill binary called by path (%s)" % toks[0]
    return verb, toks[1:], bypass


def _target_problem(word: str) -> Optional[str]:
    w = word.strip()
    if w in ("1", "-1", "0", "-0", "+1"):
        return {"1": "pid 1 (init)", "+1": "pid 1 (init)",
                "-1": "-1 (every process you can signal)",
                "0": "0 (this shell's whole process group)",
                "-0": "0 (this shell's whole process group)"}[w]
    if "$$" in w or "${$}" in w:
        return "%s (this shell or its process group)" % w
    if re.search(r"\$\{?PPID\b", w):
        return "%s (the shell's parent)" % w
    return None


def _literal_core(pattern: str) -> Tuple[str, bool]:
    """``(literal text, anchored)`` of a pkill/killall regex."""
    anchored = pattern.startswith("^")
    core = pattern[1:] if anchored else pattern
    if core.endswith("$"):
        core = core[:-1]
    core = re.sub(r"\\(.)", r"\1", core)
    return core, anchored


def _is_broad_name(literal: str) -> bool:
    n = rt.normalize_name(literal)
    return n in BROAD_NAMES or rt.is_protected_name(n)


def _name_pattern_problem(pattern: str, *, exact: bool, regex: bool = True) -> Optional[str]:
    """A process-NAME pattern (pkill without -f, killall, killall -r)."""
    core, _ = _literal_core(pattern)
    meaningful = re.sub(r"[^A-Za-z0-9]", "", core)
    if "|" in core:
        return "the pattern %r is an alternation (several names at once)" % pattern
    if exact or not regex:
        if _is_broad_name(core):
            return "%r names a shell, interpreter or session process" % pattern
        if len(meaningful) < 3:
            return "the name %r is too short to be specific" % pattern
        return None
    if len(meaningful) < 4:
        return "the pattern %r is too short to be specific" % pattern
    low = core.lower()
    hits = sorted(n for n in BROAD_NAMES | rt.PROTECTED_NAMES if low in n or n == low)
    if hits or _is_broad_name(core) or low.startswith(rt.PROTECTED_PREFIXES):
        shown = ", ".join(hits[:4]) or core
        return ("the unanchored pattern %r also matches %s" % (pattern, shown))
    return None


def _full_pattern_problem(pattern: str) -> Optional[str]:
    """``pkill -f``: the pattern is searched in EVERY full command line —
    including the agent's own shell, whose command line contains it."""
    core, anchored = _literal_core(pattern)
    if not anchored:
        return ("pkill -f %r is not anchored with ^, so it also matches your "
                "own shell (its command line contains the pattern)" % pattern)
    if "|" in core:
        return "the pattern %r is an alternation (several programs at once)" % pattern
    words = core.split()
    meaningful = re.sub(r"[^A-Za-z0-9]", "", core)
    if len(meaningful) < 8:
        return "the pattern %r is too short to be specific" % pattern
    if len(words) <= 1 and words and _is_broad_name(re.sub(r"[.*+?\[\]]+$", "", words[0])):
        return ("the pattern %r matches every %s process" % (
            pattern, rt.normalize_name(words[0])))
    return None


def _pkill_problem(args: Sequence[str]) -> Optional[str]:
    a = list(args)
    i = 1 if a and rt.is_signal_spec(a[0]) else 0
    flags: set = set()
    selectors: Dict[str, List[str]] = {}
    patterns: List[str] = []
    while i < len(a):
        t = a[i]
        if t == "--":
            patterns.extend(a[i + 1:])
            break
        if t.startswith("--"):
            name, eq, val = t.partition("=")
            if name in rt._PKILL_VALUE_LONG:
                if not eq:
                    val = a[i + 1] if i + 1 < len(a) else ""
                    i += 1
                selectors.setdefault(name, []).append(val)
            else:
                flags.add({"--full": "f", "--exact": "x", "--inverse": "v",
                           "--ignore-case": "i"}.get(name, name))
            i += 1
            continue
        if t.startswith("-") and len(t) > 1:
            j = 1
            while j < len(t):
                c = t[j]
                if c in rt._PKILL_VALUE_SHORT:
                    val = t[j + 1:]
                    if not val:
                        val = a[i + 1] if i + 1 < len(a) else ""
                        i += 1
                    selectors.setdefault("-" + c, []).append(val)
                    break
                flags.add(c)
                j += 1
            i += 1
            continue
        patterns.append(t)
        i += 1
    if "v" in flags:
        return "pkill -v (inverse) signals every process that does NOT match"
    for val in selectors.get("-P", []) + selectors.get("--parent", []):
        for part in val.split(","):
            prob = _target_problem(part)
            if prob is not None and "$$" not in part:
                return "pkill -P %s signals every child of %s" % (part, prob)
    for key in ("-s", "--session", "-g", "--pgroup"):
        for val in selectors.get(key, []):
            if val.strip() in ("0", "1"):
                return "pkill %s %s targets this shell's own session/group" % (key, val)
    if not patterns:
        broad = [k for k in selectors if k not in ("-P", "--parent", "-F", "--pidfile",
                                                     "--signal", "-q", "--queue")]
        if broad:
            return ("pkill %s with no name pattern signals every process "
                    "that selector matches" % broad[0])
        return None
    for p in patterns:
        prob = (_full_pattern_problem(p) if "f" in flags
                else _name_pattern_problem(p, exact="x" in flags))
        if prob is not None:
            return prob
    return None


def _killall_problem(args: Sequence[str]) -> Optional[str]:
    a = list(args)
    i = 1 if a and rt.is_signal_spec(a[0]) else 0
    names: List[str] = []
    regex = user = False
    while i < len(a):
        t = a[i]
        if t == "--":
            names.extend(a[i + 1:])
            break
        if t in rt._KILLALL_VALUE:
            user = user or t in ("-u", "--user")
            i += 2
            continue
        if t.startswith("--"):
            regex = regex or t == "--regexp"
            user = user or t.startswith("--user=")
        elif t.startswith("-") and len(t) > 1:
            regex = regex or "r" in t
        else:
            names.append(t)
        i += 1
    if not names:
        return ("killall -u with no name signals every process of that user"
                if user else None)
    for n in names:
        prob = _name_pattern_problem(n, exact=not regex, regex=regex)
        if prob is not None:
            return prob
    return None


def _taskkill_problem(args: Sequence[str]) -> Optional[str]:
    a = list(args)
    i = 0
    while i < len(a):
        opt = "/" + a[i].lstrip("/-").lower() if a[i][:1] in ("/", "-") else ""
        val = a[i + 1] if i + 1 < len(a) else ""
        if opt == "/fi":
            return "taskkill /FI selects by filter; name the exact /PID or /IM"
        if opt == "/pid":
            if val.strip() in ("0", "4") or val.strip().lower() == "$pid":
                return "taskkill /PID %s is a system process or this shell" % val
        if opt == "/im":
            prob = _ps_name_problem(val)
            if prob is not None:
                return prob
        i += 2 if opt in ("/pid", "/im", "/s", "/u", "/p") else 1
    return None


def _bash_problem(cmd: str) -> Optional[str]:
    segments = bcw.walk_segments(cmd, _with_substitutions=True,
                                 _with_assignments=True)
    kills = [k for k in (_peel_to_kill(s.tokens) for s in segments if s.tokens)
             if k is not None]
    if not kills:
        return None
    lookup = _PARENT_LOOKUP.search(cmd)
    if lookup is not None:
        return ("this command looks up a PARENT pid (%s) and kills in the same "
                "command" % lookup.group(0))
    for verb, args, bypass in kills:
        if bypass:
            return ("%s bypasses the runtime kill guard; use plain `%s`"
                    % (bypass, verb))
        if verb == "killall5":
            return "killall5 signals every process on the machine"
        if verb == "skill":
            return "skill selects processes by user/terminal, far too broadly"
        if verb == "kill":
            if rt.is_signal_probe(args):
                continue  # a signal-0 probe delivers nothing (runtime parity)
            for t in rt.kill_targets(args) or []:
                prob = _target_problem(t)
                if prob is not None:
                    return "kill targets %s" % prob
        elif verb == "pkill":
            prob = _pkill_problem(args)
            if prob is not None:
                return prob
        elif verb == "killall":
            prob = _killall_problem(args)
            if prob is not None:
                return prob
        else:
            prob = _taskkill_problem(args)
            if prob is not None:
                return prob
    return None


#: Above this size the structural walk is not run: ``shlex`` is super-linear
#: on very long words (measured: a 200 kB word ~0.7 s, 1 MB ~18 s), and the
#: pre-tool-use scanner must answer inside its 2 s bound. A larger command
#: that names a kill verb at all is judged conservatively instead (refused by
#: layer 1, guarded by layer 2) — a kill belongs in its own small command.
SCAN_MAX_CHARS = 65536

#: Any kill verb as a word (linear; the large-command fallback).
_KILL_VERB_WORD = re.compile(
    r"(?i)\b(?:p?kill|killall5?|skill|taskkill|stop-process|spps)\b")


def bash_kill_segments(cmd: str) -> bool:
    """True when the Bash command runs a kill verb anywhere (chains,
    ``bash -c`` bodies, substitutions, wrapper verbs, ``xargs``). Unparseable
    or oversized text that mentions a kill verb counts (conservative)."""
    if len(cmd) > SCAN_MAX_CHARS or bcw.tokenize(cmd) is None:
        return _KILL_VERB_WORD.search(cmd) is not None
    return any(_peel_to_kill(s.tokens) is not None
               for s in bcw.walk_segments(cmd, _with_substitutions=True,
                                          _with_assignments=True) if s.tokens)


# ═══════════════════════════════════════════════════════════════════════
# Layer 1 — PowerShell
# ═══════════════════════════════════════════════════════════════════════


def _ps_name_problem(name: str) -> Optional[str]:
    n = rt.normalize_name(name.strip().strip("'\""))
    if not n:
        return None
    if n in ("*", "?") or re.fullmatch(r"[*?]+", n):
        return "the name %r matches every process" % name
    literal = re.sub(r"[*?]", "", n)
    if _is_broad_name(literal) and ("*" in n or "?" in n or literal == n):
        return "%r names a shell, interpreter or session process" % name
    if ("*" in n or "?" in n) and len(re.sub(r"[^a-z0-9]", "", literal)) < 4:
        return "the wildcard name %r is too broad" % name
    return None


def _ps_stmt_verb(words) -> str:
    return words[0].text.lower() if words and not words[0].quoted else ""


def _ps_stop_args(words) -> Tuple[List[str], List[str]]:
    """``(ids, names)`` bound from a Stop-Process statement's words
    (``-Id``/``-I`` prefix, ``-Name``/``-ProcessName``, positional = Id,
    comma lists, ``-Param:value``)."""
    ids: List[str] = []
    names: List[str] = []
    mode = ""
    for w in words[1:]:
        t = w.text
        if not w.quoted and t.startswith("-") and len(t) > 1 and not t[1:].isdigit():
            pname, _, val = t[1:].partition(":")
            p = pname.lower()
            if "id".startswith(p) or p == "pid":
                mode = "id"
            elif "name".startswith(p) or "processname".startswith(p):
                mode = "name"
            else:
                mode = "switch"
            if val:
                (ids if mode == "id" else names).extend(val.split(","))
                mode = ""
            continue
        target = names if mode == "name" else ids
        if mode in ("", "id", "name"):
            target.extend(x for x in t.split(",") if x)
        mode = ""
    return ids, names


def _ps_problem(cmd: str) -> Optional[str]:
    from vco_lib import powershell_command as psc

    stmts = psc._statements(cmd)
    for k, st in enumerate(stmts):
        verb = _ps_stmt_verb(st.words)
        if verb == "taskkill" or verb == "taskkill.exe":
            if _PARENT_LOOKUP.search(cmd):
                return "this command looks up a PARENT process id and kills in the same command"
            prob = _taskkill_problem([w.text for w in st.words[1:]])
            if prob is not None:
                return prob
            continue
        if verb not in PS_STOP_VERBS:
            continue
        lookup = _PARENT_LOOKUP.search(cmd)
        if lookup is not None:
            return ("this command looks up a PARENT process (%s) and stops a "
                    "process in the same command" % lookup.group(0))
        ids, names = _ps_stop_args(st.words)
        for i in ids:
            v = i.strip().lower()
            if v in ("$pid", "0", "4", "1"):
                return "Stop-Process -Id %s is this shell or a system process" % i
        for n in names:
            prob = _ps_name_problem(n)
            if prob is not None:
                return prob
        if not ids and not names and st.sep == "|" and k > 0:
            head = stmts[k - 1].words
            hverb = _ps_stmt_verb(head)
            if hverb in ("get-process", "gps", "ps"):
                hargs = [w.text for w in head[1:]]
                if not hargs or any(a.strip() == "*" for a in hargs):
                    return "Get-Process | Stop-Process stops every process"
                if any(a.strip().lower() == "$pid" for a in hargs):
                    return "the pipeline stops this shell ($PID)"
                h_ids, h_names = _ps_stop_args(head)
                for n in h_names + [x for x in h_ids if not x.lstrip("-").isdigit()]:
                    prob = _ps_name_problem(n)
                    if prob is not None:
                        return prob
    return None


def ps_kill_statements(cmd: str) -> bool:
    """True when the PowerShell command runs Stop-Process/kill/spps/taskkill."""
    from vco_lib import powershell_command as psc

    if len(cmd) > SCAN_MAX_CHARS:
        return _KILL_VERB_WORD.search(cmd) is not None
    for st in psc._statements(cmd):
        verb = _ps_stmt_verb(st.words)
        if verb in PS_STOP_VERBS or verb in ("taskkill", "taskkill.exe"):
            return True
    return False


def static_check(cmd: str, shell: str = "bash",
                 env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Layer 1. The refusal message, or None when the command may run."""
    if not isinstance(cmd, str) or not cmd.strip() or guard_disabled(env):
        return None
    if len(cmd) > SCAN_MAX_CHARS:
        if _KILL_VERB_WORD.search(cmd) is None:
            return None
        return refusal_message(
            "this command is %d characters, too large for the kill guard to "
            "check, and it names a kill verb; run the kill as its own short "
            "command" % len(cmd))
    try:
        reason = _ps_problem(cmd) if shell == "powershell" else _bash_problem(cmd)
    except Exception as exc:  # noqa: BLE001 — a parser crash must not open the gate silently
        if re.search(r"(?i)\b(?:p?kill|killall|stop-process|spps|taskkill)\b", cmd):
            reason = "the kill guard could not parse this command (%s)" % type(exc).__name__
        else:
            return None
    return None if reason is None else refusal_message(reason)


# ═══════════════════════════════════════════════════════════════════════
# Layer 2 — the rewrite (built here, emitted by lean-ctx-rewrite.{sh,ps1})
# ═══════════════════════════════════════════════════════════════════════

_SHIM_VERBS = ("kill", "pkill", "killall")


def shim_dir_for(project_root: str) -> str:
    return os.path.join(project_root, ".claude", "state", "kill-guard", "bin")


def _shim_text(python: str, verb: str) -> str:
    return ("#!/bin/sh\n# %s (generated; see vco_lib/kill_guard.py). "
            "Checks the targets, then execs the real %s.\n"
            "exec %s -m vco_lib.kill_guard run %s \"$@\"\n"
            % (rt.SHIM_MARKER, verb, shlex.quote(python), verb))


def ensure_shims(project_root: str, python: str) -> Optional[str]:
    """Write the PATH shims (only when missing or stale). The directory, or
    None when it cannot be made — the function layer still applies."""
    d = shim_dir_for(project_root)
    try:
        os.makedirs(d, 0o700, exist_ok=True)
        for verb in _SHIM_VERBS:
            path = os.path.join(d, verb)
            text = _shim_text(python, verb)
            try:
                with open(path, encoding="utf-8") as f:
                    current = f.read()
            except OSError:
                current = None
            if current != text:
                atomic_write_bytes(Path(path), text.encode("utf-8"),
                                   mode=0o700)
    except OSError:
        return None
    return d


def bash_prefix(python: str, shim_dir: Optional[str]) -> str:
    """The guard definitions prepended to a Bash command."""
    py = shlex.quote(python)
    lines = [
        "# VCO kill guard (vco_lib/kill_guard.py): kill/pkill/killall check "
        "their targets first.",
        "kill() { %s -m vco_lib.kill_guard check kill -- \"$@\" </dev/null "
        "|| return 1; builtin kill \"$@\"; }" % py,
        "[ -n \"${BASH_VERSION:-}\" ] && export -f kill 2>/dev/null",
    ]
    if shim_dir:
        lines.append("export PATH=%s:\"$PATH\"" % shlex.quote(shim_dir))
    return "\n".join(lines) + "\n"


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def ps_prefix(python: str) -> str:
    """The guard definitions prepended to a PowerShell command. A function
    named Stop-Process wins over the cmdlet, and the ``kill``/``spps``
    aliases resolve to that name, so all three spellings are covered."""
    py = _ps_quote(python)
    refused = ("Write-Error 'VCO kill guard refused this call (reason above). "
               "For the USER only: VCO_KILL_GUARD=off disables the guard.'")
    return (
        "# VCO kill guard (vco_lib/kill_guard.py): Stop-Process/kill/spps/"
        "taskkill check their targets first.\n"
        "function Stop-Process {\n"
        "  [CmdletBinding(DefaultParameterSetName='Id', SupportsShouldProcess=$true)]\n"
        "  param(\n"
        "    [Parameter(ParameterSetName='Id', Mandatory=$true, Position=0, "
        "ValueFromPipelineByPropertyName=$true)][int[]]$Id,\n"
        "    [Parameter(ParameterSetName='Name', Mandatory=$true, "
        "ValueFromPipelineByPropertyName=$true)][Alias('ProcessName')][string[]]$Name,\n"
        "    [Parameter(ParameterSetName='InputObject', Mandatory=$true, Position=0, "
        "ValueFromPipeline=$true)][System.Diagnostics.Process[]]$InputObject,\n"
        "    [switch]$PassThru, [switch]$Force)\n"
        "  process {\n"
        "    $kgArgs = @()\n"
        "    if ($PSCmdlet.ParameterSetName -eq 'InputObject') { foreach ($p in $InputObject) "
        "{ $kgArgs += '--id'; $kgArgs += [string]$p.Id } }\n"
        "    elseif ($PSCmdlet.ParameterSetName -eq 'Name') { foreach ($n in $Name) "
        "{ $kgArgs += '--name'; $kgArgs += $n } }\n"
        "    else { foreach ($i in $Id) { $kgArgs += '--id'; $kgArgs += [string]$i } }\n"
        "    & %s -m vco_lib.kill_guard check stop-process -- @kgArgs\n"
        "    if ($LASTEXITCODE -ne 0) { %s; return }\n"
        "    Microsoft.PowerShell.Management\\Stop-Process @PSBoundParameters\n"
        "  }\n"
        "}\n"
        "function taskkill {\n"
        "  & %s -m vco_lib.kill_guard check taskkill -- @args\n"
        "  if ($LASTEXITCODE -ne 0) { %s; return }\n"
        "  $kgExe = Get-Command taskkill -CommandType Application -ErrorAction SilentlyContinue "
        "| Select-Object -First 1\n"
        "  if ($kgExe) { & $kgExe.Source @args }\n"
        "}\n"
        "# Off Windows `kill` is the native binary, not the Stop-Process alias.\n"
        "if ($PSVersionTable.PSEdition -eq 'Core' -and -not $IsWindows) {\n"
        "function kill {\n"
        "  & %s -m vco_lib.kill_guard check kill -- @args\n"
        "  if ($LASTEXITCODE -ne 0) { %s; return }\n"
        "  $kgExe = Get-Command kill -CommandType Application -ErrorAction SilentlyContinue "
        "| Select-Object -First 1\n"
        "  if ($kgExe) { & $kgExe.Source @args }\n"
        "}\n"
        "}\n" % (py, refused, py, refused, py, refused)
    )


def build_rewrite(payload: dict, *, python: str,
                  project_root: Optional[str]) -> Optional[dict]:
    """The PreToolUse response that routes this call's kills through the
    runtime guard, or None (no kill verb / not a shell tool / switched
    off). Every other ``tool_input`` field is preserved."""
    if not isinstance(payload, dict) or guard_disabled():
        return None
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return None
    cmd = ti.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    tool = payload.get("tool_name", "Bash")
    if tool == "Bash":
        # Git Bash on Windows: its `kill` takes MSYS pids, while this
        # interpreter sees the Windows process table — the two pid spaces do
        # not match, so a runtime check there would judge the wrong process.
        # The static layer still applies; this is a documented limit.
        if sys.platform == "win32":
            return None
        if not bash_kill_segments(cmd):
            return None
        shims = ensure_shims(project_root, python) if project_root else None
        new_cmd = bash_prefix(python, shims) + cmd
    elif tool == "PowerShell":
        if not ps_kill_statements(cmd):
            return None
        new_cmd = ps_prefix(python) + cmd
    else:
        return None
    updated = dict(ti)
    updated["command"] = new_cmd
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "updatedInput": updated}}


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

_USAGE = (
    "usage: python -m vco_lib.kill_guard rewrite [--project-root DIR]  (stdin: hook payload)\n"
    "       python -m vco_lib.kill_guard check {kill|pkill|killall|stop-process|taskkill} -- ARGS\n"
    "       python -m vco_lib.kill_guard run {kill|pkill|killall} ARGS   (PATH shim)\n"
    "       python -m vco_lib.kill_guard static [--shell bash|powershell]  (stdin: command)\n"
)


def _cmd_check(verb: str, args: List[str]) -> int:
    try:
        reason = rt.check_call(verb, args)
    except Exception as exc:  # noqa: BLE001 — fail closed, loudly
        reason = "the kill guard failed (%s: %s)" % (type(exc).__name__, exc)
    if reason is None:
        return 0
    print(refusal_message(reason), file=sys.stderr)
    return EXIT_REFUSED


def main(argv: Optional[List[str]] = None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    if not a:
        sys.stderr.write(_USAGE)
        return 2
    mode, rest = a[0], a[1:]
    if mode == "check":
        if not rest or rest[0] not in rt.VERBS:
            sys.stderr.write(_USAGE)
            return EXIT_REFUSED
        verb, args = rest[0], rest[1:]
        if args and args[0] == "--":
            args = args[1:]
        return _cmd_check(verb, args)
    if mode == "run":
        if not rest or rest[0] not in _SHIM_VERBS:
            sys.stderr.write(_USAGE)
            return EXIT_REFUSED
        verb, args = rest[0], rest[1:]
        code = _cmd_check(verb, args)
        if code != 0:
            return code
        real = rt.real_binary(verb)
        if real is None:
            print("[kill_guard] no real %s found on PATH" % verb, file=sys.stderr)
            return 127
        os.execv(real, [verb, *args])
    if mode == "rewrite":
        root = None
        if len(rest) >= 2 and rest[0] == "--project-root":
            root = rest[1] or None
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except (ValueError, OSError):
            return 0
        out = build_rewrite(payload, python=sys.executable, project_root=root)
        if out is not None:
            sys.stdout.write(json.dumps(out))
        return 0
    if mode == "static":
        shell = rest[1] if len(rest) >= 2 and rest[0] == "--shell" else "bash"
        msg = static_check(sys.stdin.read(), shell)
        if msg is None:
            return 0
        print(msg, file=sys.stderr)
        return EXIT_REFUSED
    sys.stderr.write(_USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
