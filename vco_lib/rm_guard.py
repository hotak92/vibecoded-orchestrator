# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""rm guard (v0.2.105): what an agent's ``rm`` may delete. ONE home for the
decision (A>B>C): the pre-tool-use security scanner
(``.claude/scripts/bash_security.py``, run by ``pre-tool-use.{sh,ps1}``)
calls :func:`scan` for every Bash tool call and :func:`confirm_once` to
remember a confirmation; nothing mirrors it. The scanner's flat ``rm_root``
regex is a text backstop for the HARD set only (prose, process
substitution, over-deep ``bash -c`` nests) — it must match
:func:`classify_target`'s hard tier (parity test:
tests/test_v02105_rm_guard.py).

Why: the old ``rm_root`` rule refused ANY rm under the home directory —
every project lives there, so it blocked an agent deleting its own scratch
lane — and it had no word boundary (``confirm /home`` matched). Owner
ruling 2026-10-29: hard-block only the home directory itself and system
paths; a deeper path is allowed, but a recursive or glob delete is refused
ONCE with "are you sure", and the identical retry is allowed.

Tiers, per target of every ``rm`` the command runs (chains, ``bash -c``,
``eval``, substitutions, wrapper verbs, ``xargs``):

* HARD (never confirmable): ``/``, anything at or under ``/etc /usr /var
  /boot /dev /root``, ``/home`` / ``/Users``, ``/home/<user>`` (alone,
  trailing slash, ``/*``), ``~`` / ``~user`` / ``$HOME`` / ``${HOME}``
  alone or with ``/`` or ``/*``, the real home directory, and a glob in a
  root-level name (``/h*``).
* CONFIRM: a deeper path under a home directory, deleted recursively
  (``-r``/``-R``/``--recursive``, combined flags) or through a glob.
* Everything else runs: a plain file rm under home, relative paths, paths
  outside home and the system set, ``$VAR`` targets.

The confirmation store: one file per pending confirmation under
``<project>/.claude/state/rm-confirm/`` (dir 0700, file 0600), named by
the SHA-256 of session + agent + the whitespace-normalised command — the
command text itself is never stored. The second identical attempt within
:data:`CONFIRM_TTL_S` consumes it (allowed); a third needs a new confirm.
Any state error refuses (fail-closed) and says why.

Known limits: targets a program computes (``xargs rm`` fed from stdin,
``find -delete``, ``$VAR`` paths), a ``cd ~ && rm -rf *`` relative delete,
and rm inside a script FILE the command runs are not judged. On the
PowerShell tool only the flat backstop and the PowerShell rule set run.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import time
from typing import List, NamedTuple, Optional, Tuple

from vco_lib import bash_command_walk as bcw

HARD = "hard"
CONFIRM = "confirm"

#: Seconds a recorded confirmation stays usable (owner ruling: 10 min).
CONFIRM_TTL_S = 600
#: The state sub-directory (under ``<project>/.claude/state``).
STATE_SUBDIR = "rm-confirm"

#: Root-level directories protected at any depth (today's behaviour).
SYSTEM_DIRS = frozenset({"etc", "usr", "var", "boot", "dev", "root"})
#: Root-level directories holding user home directories.
HOME_PARENTS = frozenset({"home", "users"})

#: Above this size the structural walk is not run (``shlex`` is
#: super-linear on very long words; the scanner answers inside 2 s). Must
#: match kill_guard.SCAN_MAX_CHARS's reasoning.
SCAN_MAX_CHARS = 65536

_GLOB_CHARS = re.compile(r"[*?\[]")
_HOME_VAR = re.compile(r"^\$(?:HOME\b|\{HOME\})")
#: An ``rm`` command word with a home-ish path later in the same simple
#: command — the conservative reading of a command the walk cannot parse.
_RM_WORD = r"(?<![\w.-])rm\s"
_UNPARSED_HOME_RM = re.compile(
    _RM_WORD + r"(?:(?!" + _RM_WORD + r")[^;&|\n])*"
    r"(?:~|\$\{?HOME|/home/|/users/)", re.IGNORECASE)


class Finding(NamedTuple):
    tier: str      # HARD or CONFIRM
    target: str    # the target word as written


def _home_dir(home: Optional[str]) -> Optional[str]:
    h = home if home is not None else os.environ.get("HOME", "")
    h = (h or "").replace("\\", "/").rstrip("/")
    return posixpath.normpath(h) if h.startswith("/") else None


def _absolute(word: str, home: Optional[str]) -> Optional[Tuple[str, bool]]:
    """``(normalised absolute path, spelled_via_home)`` for a target word
    naming an absolute or home-relative path; None for anything else
    (relative paths, other variables)."""
    w = word.replace("\\", "/") if word.startswith(("~", "$")) else word
    if w.startswith("~"):
        head, sep, rest = w.partition("/")
        if head == "~":
            base = home or "/home/~"
        else:
            base = "/home/" + head[1:]
        return posixpath.normpath(base + "/" + rest), True
    m = _HOME_VAR.match(w)
    if m:
        rest = w[m.end():]
        if rest and not rest.startswith("/"):
            return None  # $HOMEX, ${HOME}foo — not the home dir
        return posixpath.normpath((home or "/home/~") + "/" + rest), True
    if w.startswith("/"):
        return posixpath.normpath(w), False
    return None


def _is_wildcard_name(name: str) -> bool:
    """A name that matches (nearly) every entry: ``*``, ``.*``, ``*.*``."""
    return bool(name) and _GLOB_CHARS.search(name) is not None and \
        re.fullmatch(r"[*?.\[\]]+", name) is not None


def classify_target(word: str, home: Optional[str] = None) -> Optional[str]:
    """HARD, ``"home_deep"`` (a deeper path under a home directory) or None
    (not protected). ``home`` defaults to ``$HOME``."""
    home_dir = _home_dir(home)
    resolved = _absolute(word, home_dir)
    if resolved is None:
        return None
    path, _via_home = resolved
    comps = [c for c in path.split("/") if c]
    if not comps:
        return HARD  # `/`, `/.`, `/..`
    first = comps[0].lower()
    if first in SYSTEM_DIRS:
        return HARD
    if _GLOB_CHARS.search(comps[0]):
        return HARD  # `/h*`, `/*` — may expand to anything at the root
    homes = []
    if first in HOME_PARENTS:
        if len(comps) <= 2:
            return HARD  # /home, /home/*, /home/<user>
        homes.append(comps[:2])
    if home_dir:
        hc = [c for c in home_dir.split("/") if c]
        if hc and comps[:len(hc)] == hc:
            homes.append(hc)
    for hc in homes:
        below = comps[len(hc):]
        if not below:
            return HARD
        if len(below) == 1 and _is_wildcard_name(below[0]):
            return HARD  # ~/*, ~/.*, /home/u/*
    if homes:
        return "home_deep"
    if home_dir and home_dir.startswith(path.rstrip("/") + "/"):
        return HARD  # an ancestor of the real home dir (e.g. /Users)
    return None


def _rm_operands(args: List[str]) -> Tuple[bool, List[str]]:
    """``(recursive, targets)`` of an rm argument list (GNU rm permutes:
    options may follow operands, until ``--``)."""
    recursive = False
    targets: List[str] = []
    opts_done = False
    for a in args:
        if not opts_done and a == "--":
            opts_done = True
            continue
        if not opts_done and a.startswith("--"):
            if a in ("--recursive",) or a.startswith("--recursive="):
                recursive = True
            continue
        if not opts_done and a.startswith("-") and len(a) > 1:
            if "r" in a[1:] or "R" in a[1:]:
                recursive = True
            continue
        targets.append(a)
    return recursive, targets


def _segment_findings(tokens: List[str], home: Optional[str]) -> List[Finding]:
    peeled = bcw.peel_to_command(list(tokens))
    if peeled is None:
        return []
    toks, _via = peeled
    verb = bcw.command_basename(toks[0])
    if verb.endswith(".exe"):
        verb = verb[:-4]
    if verb != "rm":
        return []
    recursive, targets = _rm_operands(toks[1:])
    out: List[Finding] = []
    for t in targets:
        tier = classify_target(t, home)
        if tier == HARD:
            out.append(Finding(HARD, t))
        elif tier == "home_deep" and (recursive or _GLOB_CHARS.search(t)):
            out.append(Finding(CONFIRM, t))
    return out


def scan(cmd: str, home: Optional[str] = None) -> List[Finding]:
    """Every protected target of every rm the Bash command runs. A command
    the walk cannot parse (or too large to) that runs ``rm`` on a home-ish
    path yields one CONFIRM finding for the whole command (conservative)."""
    if not isinstance(cmd, str) or not cmd.strip():
        return []
    if len(cmd) > SCAN_MAX_CHARS or bcw.tokenize(cmd) is None:
        if _UNPARSED_HOME_RM.search(cmd):
            return [Finding(CONFIRM, "(a command too large or malformed to "
                                     "parse, which runs rm on a home path)")]
        return []
    out: List[Finding] = []
    for seg in bcw.walk_segments(cmd, _with_substitutions=True,
                                 _with_assignments=True):
        if seg.tokens:
            out.extend(_segment_findings(seg.tokens, home))
    return out


# ═══════════════════════════════════════════════════════════════════════
# Messages
# ═══════════════════════════════════════════════════════════════════════


def hard_message(targets: List[str]) -> str:
    return ("[rm_root] Destructive rm targeting system/home directories (%s). "
            "The home directory itself, /home, / and the system directories "
            "(/etc /usr /var /boot /dev /root) are never deleted by an agent; "
            "name the deeper path you mean." % ", ".join(targets))


def confirm_message(targets: List[str], note: str = "") -> str:
    msg = ("[rm_confirm] Are you sure you want to delete all the contents of "
           "%s? This is refused once. If you are 100%% sure the target is "
           "right, retry the exact same command; the second identical attempt "
           "(same chat or subagent, within %d minutes) is allowed."
           % (", ".join(targets), CONFIRM_TTL_S // 60))
    return msg + (" " + note if note else "")


# ═══════════════════════════════════════════════════════════════════════
# The confirm-once store
# ═══════════════════════════════════════════════════════════════════════


def normalise_command(cmd: str) -> str:
    return " ".join(cmd.split())


def confirm_key(session: str, agent: str, cmd: str) -> str:
    raw = "\0".join((session, agent or "main", normalise_command(cmd)))
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


def _sweep(directory: str, now: float) -> None:
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for n in names:
        p = os.path.join(directory, n)
        try:
            if now - os.path.getmtime(p) > CONFIRM_TTL_S:
                os.unlink(p)
        except OSError:
            pass


def _read_stamp(path: str) -> Optional[float]:
    """The recorded time, None when absent. Raises OSError on other errors."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        data = os.read(fd, 64)
    finally:
        os.close(fd)
    try:
        return float(data.decode("ascii").strip())
    except ValueError:
        return 0.0  # unreadable stamp: treat as expired


def _record(path: str, now: float) -> None:
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, ("%.3f" % now).encode("ascii"))
    finally:
        os.close(fd)


def confirm_once(cmd: str, session: str, agent: str, state_dir: str,
                 now: Optional[float] = None) -> Tuple[bool, str]:
    """``(allowed, note)``. First attempt: record and refuse (note "").
    Identical attempt within the TTL: consume and allow. Any state error:
    refuse with the reason in ``note`` (fail-closed)."""
    if not session:
        return False, ("(This check has no chat session id to remember a "
                       "confirmation by, so the command stays refused; run it "
                       "yourself in a terminal if it is right.)")
    if not state_dir:
        return False, ("(This check has no state directory to remember a "
                       "confirmation in, so the command stays refused.)")
    now = time.time() if now is None else now
    directory = os.path.join(state_dir, STATE_SUBDIR)
    path = os.path.join(directory, confirm_key(session, agent, cmd))
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if os.path.islink(directory):
            raise OSError("%s is a symlink" % directory)
        if os.name == "posix":
            os.chmod(directory, 0o700)
        _sweep(directory, now)
        stamp = _read_stamp(path)
        if stamp is not None and 0 <= now - stamp <= CONFIRM_TTL_S:
            try:
                os.unlink(path)
            except FileNotFoundError:
                stamp = None  # consumed by a concurrent identical call
            else:
                return True, ""
        if stamp is not None:
            os.unlink(path)  # expired
        _record(path, now)
    except OSError as exc:
        return False, ("(The confirmation could not be recorded or read "
                       "(%s), so the command stays refused.)" % exc)
    return False, ""


# Environment the hooks hand the scanner (set by pre-tool-use.{sh,ps1}
# from the hook payload; an agent's command cannot set the hook's env).
ENV_SESSION = "VCO_GUARD_SESSION_ID"
ENV_AGENT = "VCO_GUARD_AGENT_ID"
ENV_STATE_DIR = "VCO_GUARD_STATE_DIR"


def decide(cmd: str, findings: List[Finding],
           env: Optional[dict] = None) -> Optional[str]:
    """The refusal message for ``findings`` (None = allowed). HARD findings
    refuse outright; CONFIRM findings go through :func:`confirm_once` with
    the session / agent / state dir from ``env`` (default ``os.environ``)."""
    if not findings:
        return None
    hard = [f.target for f in findings if f.tier == HARD]
    if hard:
        return hard_message(hard)
    e = os.environ if env is None else env
    allowed, note = confirm_once(cmd, e.get(ENV_SESSION, ""),
                                 e.get(ENV_AGENT, ""), e.get(ENV_STATE_DIR, ""))
    if allowed:
        return None
    seen: List[str] = []
    for f in findings:
        if f.target not in seen:
            seen.append(f.target)
    return confirm_message(seen, note)
