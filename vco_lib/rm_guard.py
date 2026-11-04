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
  alone or with ``/`` or ``/*``, the real home directory, a glob in a
  root-level name (``/h*``), and a dot-matching wildcard component
  (``.*``) in ANY position below a home directory (``~/.*/tmp``) — such a
  glob expands to ``..`` and can climb out of the user's home.
* CONFIRM: a deeper path under a home directory, deleted recursively
  (``-r``/``-R``/``--recursive``, combined flags) or through a glob — and
  a recursive or glob delete of a target the command leaves
  indeterminable (``rm -rf build`` with no ``cd`` to place it, ``rm -rf
  $D/x`` with an unassigned ``$D``): never allowed silently, never
  hard-blocked (owner ruling, review R-2). ``$VAR`` targets the command
  itself assigns (``D=~/proj; rm -rf $D/build``) and relative targets a
  ``cd`` in the same command places (``cd ~/proj && rm -rf build``) are
  judged at their real, resolved place.
* Everything else runs: a plain file rm under home, relative paths and
  paths outside home and the system set.

The confirmation store: one file per pending confirmation under
``<project>/.claude/state/rm-confirm/`` (dir 0700, file 0600), named by
the SHA-256 of session + agent + the whitespace-normalised command — the
command text itself is never stored. The second identical attempt within
:data:`CONFIRM_TTL_S` consumes it (allowed); a third needs a new confirm.
The stamp is written losslessly and a stamp up to
:data:`CONFIRM_FUTURE_SKEW_S` in the future still counts as "now", so the
identical retry issued moments later is always the one that is allowed —
the decision depends on session, agent, command and state dir, never on how
the clock rounded in between. Any state error refuses (fail-closed) and
says why.

Known limits: targets a program computes (``xargs rm`` fed from stdin,
``find -delete``) and rm inside a script FILE the command runs are not
judged; a ``$VAR`` or relative target is judged as far as the command
itself makes it determinable and is confirm-once otherwise (see the CONFIRM
tier). On the PowerShell tool only the flat backstop and the PowerShell
rule set run.
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
#: How far in the FUTURE a recorded stamp may sit and still count as "now".
#: The stamp is a number read back from a text file, so it is never a guess
#: about the size of the negative difference a rounding or a small clock step
#: can produce — it only has to cover that, never to extend the TTL (the TTL
#: bound below is untouched). Review R2 item 8: a millisecond-rounded stamp
#: made a retry issued immediately afterwards read ``now - stamp < 0``, be
#: treated as EXPIRED, and record a fresh confirmation — so the identical
#: retry was refused instead of allowed, at random, whichever test ran first.
CONFIRM_FUTURE_SKEW_S = 1.0
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
        for c in below:
            # A dot-matching wildcard in ANY position (``~/.*/tmp``,
            # ``~/.*/*``) expands to ``..`` too, so the glob can climb out
            # of the user's home (``/home/u/../tmp`` = ``/home/tmp``, and
            # ``~/.*/*`` reaches every user's home) — never confirmable
            # (review R2 rmguard R-1; a plain ``*`` cannot match ``..``).
            if _is_wildcard_name(c) and "." in c:
                return HARD
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


def _segment_findings(tokens: List[str], home: Optional[str],
                      cwd: Optional[str] = None,
                      varmap: Optional[dict] = None,
                      cwd_proven: bool = True) -> List[Finding]:
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
        word, shown, placed, from_cwd = _resolve_word(t, home, cwd, varmap)
        tier = classify_target(word, home)
        if tier == HARD and from_cwd and not cwd_proven:
            # The cwd this relative target was resolved against sits at a
            # walk depth where the walker may have flattened two sibling
            # ``bash -c`` bodies into one chain — the resolution is not
            # proven beyond doubt, so it is confirm-once, never a hard
            # block on an ordinary relative delete (owner ruling, R-2).
            out.append(Finding(CONFIRM, shown))
        elif tier == HARD:
            out.append(Finding(HARD, shown))
        elif tier == "home_deep" and (recursive
                                      or _GLOB_CHARS.search(word)
                                      or _GLOB_CHARS.search(t)):
            out.append(Finding(CONFIRM, shown))
        elif tier is None and not placed and (recursive
                                              or _GLOB_CHARS.search(word)):
            # A delete the guard cannot place (relative with no known cwd,
            # or an unresolvable ``$VAR``) never slips through silently:
            # it is confirm-once (owner ruling, review R-2). A word the
            # guard DID place outside every protected root runs, as before.
            out.append(Finding(CONFIRM, shown))
    return out


#: A ``$NAME`` / ``${NAME}`` reference in a target word.
_VAR_REF = re.compile(r"\$(\{)?([A-Za-z_]\w*)(?(1)\})")


def _expand_vars(word: str, varmap: dict) -> Optional[str]:
    """*word* with every ``$VAR`` the command itself assigned expanded.
    ``None`` when a reference stays unresolvable (never guess a value)."""
    def sub(m: "re.Match[str]") -> str:
        return varmap.get(m.group(2), "\0")
    expanded = _VAR_REF.sub(sub, word)
    return None if "\0" in expanded or "$" in expanded else expanded


def _resolve_word(t: str, home: Optional[str], cwd: Optional[str],
                  varmap: Optional[dict]) -> Tuple[str, str, bool, bool]:
    """``(effective word, target as shown, placed, from_cwd)`` for one rm
    operand.

    ``placed`` is True when the word is determinable: ``$VAR`` references
    assigned earlier in the SAME command expanded, and a relative word
    resolved against the cwd a ``cd`` in the same command established.
    ``from_cwd`` is True only for that last case — the one resolution the
    caller may need to distrust (see :func:`_segment_findings`). An
    indeterminable word (unresolved ``$VAR``, or relative with no known
    cwd) is returned AS WRITTEN with ``placed=False``, so the caller can
    confirm-once instead of allowing silently; it is never resolved to a
    guess and never hard-blocked.
    """
    word = _expand_vars(t, varmap) if (varmap and "$" in t) else t
    if word is None:
        return t, t, False, False
    abs_word = _absolute(word, home)
    if abs_word is not None:
        return abs_word[0], abs_word[0], True, False
    if not word.startswith("/") and word not in (".", ".."):
        if cwd:
            resolved = posixpath.normpath(cwd + "/" + word)
            return resolved, resolved, True, True
        return word, t, False, False
    return word, t, word.startswith("/"), False


#: ``cd``-like builtins whose argument can move the shell's cwd. A cd the
#: command cannot resolve (``cd $D`` with unknown ``$D``, ``cd -``) marks
#: the cwd UNKNOWN, so later relative deletes confirm instead of resolving
#: against a directory that may no longer be right.
_CD_UNKNOWN_ARGS = frozenset({"-", "~-", "+", "^"})


def _cd_target(toks: List[str], home: Optional[str],
               varmap: Optional[dict]) -> Optional[Optional[str]]:
    """The cwd after a ``cd`` segment: an absolute path, ``None``-meaning-
    home (``cd`` with no argument), or ``None``-meaning-unknown (the
    sentinel :data:`_UNKNOWN` for a target that cannot be resolved)."""
    args = [a for a in toks[1:] if not a.startswith("-")]
    if not args:
        return home_dir if (home_dir := _home_dir(home)) else _UNKNOWN
    word = _expand_vars(args[0], varmap) if (varmap and "$" in args[0]) \
        else args[0]
    if word is None or word in _CD_UNKNOWN_ARGS:
        return _UNKNOWN
    resolved = _absolute(word, home)
    if resolved is not None:
        return resolved[0]
    return _UNKNOWN  # a relative cd target: the cwd is real but unknowable


_UNKNOWN = "\0unknown-cwd\0"


def scan(cmd: str, home: Optional[str] = None) -> List[Finding]:
    """Every protected target of every rm the Bash command runs. A command
    the walk cannot parse (or too large to) that runs ``rm`` on a home-ish
    path yields one CONFIRM finding for the whole command (conservative).

    A target the command makes determinable is judged at its real place:
    ``$VAR`` references assigned earlier in the same command are expanded,
    and a relative target is resolved against the cwd a ``cd`` in the same
    command (or an enclosing ``bash -c`` of it) established. A target that
    stays indeterminable never slips past silently: a recursive or glob
    delete of it is confirm-once (owner ruling, review R-2)."""
    if not isinstance(cmd, str) or not cmd.strip():
        return []
    if len(cmd) > SCAN_MAX_CHARS or bcw.tokenize(cmd) is None:
        if _UNPARSED_HOME_RM.search(cmd):
            return [Finding(CONFIRM, "(a command too large or malformed to "
                                     "parse, which runs rm on a home path)")]
        return []
    out: List[Finding] = []
    varmap: dict = {}
    cwd: Optional[str] = None          # the established cwd, or _UNKNOWN
    cwd_depth: Optional[int] = None    # the walk depth it was set at
    cwd_pos: int = -1                  # last position seen at that depth
    for seg in bcw.walk_segments(cmd, _with_substitutions=True,
                                 _with_assignments=True):
        if cwd_depth is not None:
            if seg.depth < cwd_depth:
                # Left the sub-chain that cd'd: its cwd does not reach the
                # outer level (`bash -c 'cd /etc'; rm -rf build`).
                cwd, cwd_depth, cwd_pos = None, None, -1
            elif seg.depth == cwd_depth and seg.position == 0 and cwd_pos > 0:
                # A SIBLING sub-chain at the same depth restarts at
                # position 0: it inherits the OUTER cwd, not the previous
                # sibling's (`bash -c 'cd /etc'; bash -c 'rm -rf build'`).
                cwd, cwd_depth, cwd_pos = None, None, -1
            elif seg.depth == cwd_depth:
                cwd_pos = seg.position
        if not seg.tokens:
            # An assignment-ONLY statement persists for the rest of the
            # command (an env-prefix ``D=x rm …`` does not — the shell
            # expands ``$D`` before that assignment takes effect).
            for a in seg.assignments:
                name, eq, value = a.partition("=")
                if eq and name.isidentifier() and "$" not in value:
                    varmap[name] = value
            continue
        peeled = bcw.peel_to_command(list(seg.tokens))
        if peeled is not None:
            toks, _via = peeled
            verb = bcw.command_basename(toks[0])
            if verb.endswith(".exe"):
                verb = verb[:-4]
            if verb == "cd":
                cwd = _cd_target(toks, home, varmap)
                if cwd == _UNKNOWN:
                    cwd = None  # a cd the command cannot resolve: unknowable
                cwd_depth = seg.depth
                cwd_pos = seg.position
                continue
        out.extend(_segment_findings(
            seg.tokens, home, cwd, varmap,
            cwd_proven=(cwd_depth == 0 or seg.depth > (cwd_depth or 0))))
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
        # ``repr`` round-trips the float exactly. ``"%.3f"`` rounded the
        # stamp UP by up to 0.5 ms, so a retry issued within that window read
        # a stamp in the future and was refused as expired (review R2
        # item 8). ``CONFIRM_FUTURE_SKEW_S`` covers what a clock step still
        # can do.
        os.write(fd, repr(float(now)).encode("ascii"))
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
        if (stamp is not None
                and -CONFIRM_FUTURE_SKEW_S <= now - stamp <= CONFIRM_TTL_S):
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
