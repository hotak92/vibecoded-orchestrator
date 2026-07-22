# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Stale systemd user-unit reconcile for the update flow (Linux-only).

The update flow reconciles user-level systemd units it (or a prior VCO
component) created whose entrypoint no longer resolves. A per-project service
unit written by an earlier component can outlive the code it launches: if the
module the unit runs is relocated or removed, systemd keeps the unit and its
``restart=`` policy will crashloop the missing entrypoint indefinitely, growing
its append log without bound. The update flow is the natural owner of this
reconcile step — it is the point at which install roots move and modules
relocate.

This module is that reconcile step. On ``install.py --update`` (Linux only,
graceful no-op elsewhere and when ``systemctl`` is absent) it:

1. Scans ``$HOME/.config/systemd/user/*.service`` for units whose ``ExecStart``
   references the CURRENT install root (parameterised — no hardcoded paths).
2. For each such unit, verifies the entrypoint still resolves:
   - ``<python> -m <module>`` forms → the module must be importable by the
     unit's CONFIGURED interpreter under the unit's ``Environment=PYTHONPATH=``
     (checked via ``importlib.util.find_spec`` in a SUBPROCESS of THAT
     interpreter with THAT environment — never this process, whose sys.path is
     unrelated to the unit's).
   - plain exec paths → the file must exist and be executable.
3. A provably-broken unit is auto-retired: ``systemctl --user disable --now``,
   the unit file is backup-MOVED (never deleted) to a timestamped path under
   ``<install-root>/.claude/state/retired-units/``, and the action is recorded
   via the shared deferral/run-report mechanics (the exact restore command is
   included). When retiring, an oversized ``StandardOutput=append:`` /
   ``StandardError=append:`` log (> :data:`LOG_ROTATE_THRESHOLD_BYTES`) is
   backup-moved alongside the unit.

CONSERVATIVE DEFAULTS — leave the unit alone (plus a log line) and NEVER act
when: the ``ExecStart`` is unparseable, the unit does not reference this install
root, a ``systemctl`` invocation errors, the resolution check itself errors, or
the unit file is unreadable. Files are NEVER deleted (backup-move only), and
system-level (non-user) units are NEVER touched. Every path soft-fails: the
update must never break because of this step.

SCOPE (M-8, recorded scope cut): this step runs on ORCHESTRATOR
``install.py --update`` and reconciles only units whose entrypoint references
the ORCHESTRATOR ``install_root`` passed in. It is NOT wired into the
per-project bundle-update engine, so a per-project RL/service unit whose
``ExecStart``/``PYTHONPATH`` references a PROJECT root (not the orchestrator
root) is classified ``foreign_unit`` and left alone here. Reconciling
per-project units during bundle update (passing that project's root) is a
deliberate follow-up requiring user sign-off — see wpl-DONE.md. Until then the
per-project leg is a documented gap, not a silent one.

Injection seams (all default to real behaviour; overridden only in tests so no
real ``systemctl`` / ``$HOME`` / interpreter is touched under test):
    - ``home`` — the user home whose ``.config/systemd/user`` is scanned;
      falls back to ``$VCT_USER_HOME_OVERRIDE`` then ``Path.home()`` (same
      convention install.py's boot-service writers use).
    - ``systemctl_runner`` — invoked as ``(argv) -> (rc, stdout, stderr)`` for
      the ``disable --now`` mutation; the default shells out to real
      ``systemctl``. ``systemctl_available`` gates whether any mutation runs.
    - ``resolve_module`` — invoked as
      ``(python, module, pythonpath) -> Optional[bool]`` to answer "is this
      module importable by that interpreter under that PYTHONPATH". TRI-STATE:
      ``True`` importable, ``False`` provably missing (a CLEAN interpreter run
      that positively reports module-not-found — the ONLY retire trigger),
      ``None`` the probe could not run reliably (interpreter unrunnable, timeout,
      nonzero, or find_spec raised) → leave-alone. The default runs ``find_spec``
      in a subprocess of ``python``.

``vco_lib`` is part of every healthy install, so imports here are module-top
and LOUD-FAIL (an ``ImportError`` means a broken install — it must reach the
user, never a silent inline-copy degrade).
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

from vco_lib.deferral_report import DeferralEntry

logger = logging.getLogger(__name__)

#: Append logs larger than this are backup-moved when their unit is retired
#: (10 MB). The motivating incident had a crashlooping unit grow a 101 MB log
#: because nothing rotated it — retiring the unit removes the crashloop, and
#: rotating an already-huge log reclaims the space in the same pass.
LOG_ROTATE_THRESHOLD_BYTES = 10 * 1024 * 1024

#: Backup destination for retired units, relative to the install root. Under
#: ``.claude/state`` (git-ignored / user-owned) so backups survive bundle
#: updates and never enter version control.
RETIRED_UNITS_REL = Path(".claude") / "state" / "retired-units"

#: Condition-ID prefix for the retirement run-report record. A dynamic family
#: (``stale_unit_retired_<unit-slug>``): install.py registers this prefix in
#: ``_INSTALL_OWNED_CONDITION_PREFIXES`` so a record drop-when-absent
#: self-clears on the next single-final-write once the retirement is history.
CONDITION_ID_PREFIX = "stale_unit_retired_"

# ``ExecStart=`` value on its line (the last assignment wins in systemd, but
# units VCO cares about carry exactly one). ``ExecStart=-/usr/bin/...`` and
# ``ExecStart=@/usr/bin/...`` prefix chars (ignore-failure / argv0-override)
# are stripped before parsing the command.
_EXECSTART_RE = re.compile(r"^ExecStart=(?P<val>.*)$", re.MULTILINE)

# ``Environment=PYTHONPATH=...`` (or the ``Environment="PYTHONPATH=..."``
# quoted form). systemd allows multiple Environment= lines; PYTHONPATH may also
# appear among several space-separated assignments on ONE line — both handled.
_ENVIRONMENT_RE = re.compile(r"^Environment=(?P<val>.*)$", re.MULTILINE)

# ``StandardOutput=append:<path>`` / ``StandardError=append:<path>``. Only the
# ``append:`` sink names a file we can rotate; ``journal`` / ``null`` / etc.
# carry no path and are ignored.
_STD_APPEND_RE = re.compile(
    r"^Standard(?:Output|Error)=append:(?P<path>.+)$", re.MULTILINE
)

# systemd ExecStart special prefix characters (any leading combination of
# these modifies exec semantics, not the command itself). Stripped before the
# command is tokenised.
_EXEC_PREFIX_CHARS = "-@+!:"


@dataclass
class UnitAction:
    """Outcome of reconciling ONE unit — the decision the step made.

    ``acted`` is the destructive-action gate: True only when the unit was
    actually retired (disabled + backup-moved). Every other outcome
    (leave-alone, not-ours, unparseable, resolvable) is ``acted=False`` with a
    ``reason`` naming why nothing was done — so a caller / test can assert the
    decision, not just the side effects.
    """

    unit_name: str
    acted: bool
    reason: str
    backup_path: Optional[Path] = None
    log_backup_path: Optional[Path] = None
    condition_id: Optional[str] = None


@dataclass
class ReconcileResult:
    """Aggregate outcome of a reconcile pass."""

    actions: List[UnitAction] = field(default_factory=list)

    @property
    def retired(self) -> List[UnitAction]:
        return [a for a in self.actions if a.acted]


def _log(log: Any, level: str, msg: str) -> None:
    """Emit ``msg`` at ``level`` via the caller's logger (or ours).

    ``log`` may be a stdlib logger, a plain callable, or None. Logging never
    raises into the caller — a reconcile step must not break on a log line.

    LEVEL THREADING (N-8): a callable adapter may opt in to the level by
    accepting a ``level=`` keyword (install.py's shim does, so a reconcile
    warning is logged as ``phase="warn"`` rather than being flattened to
    ``"ok"``). A legacy one-arg callable that rejects the keyword is called with
    just ``msg`` — so old adapters keep working unchanged.
    """
    if callable(log) and not isinstance(log, logging.Logger):
        try:
            try:
                log(msg, level=level)
            except TypeError:
                # Callable doesn't accept level= (legacy one-arg adapter) →
                # fall back to the message-only contract.
                log(msg)
        except Exception:  # noqa: BLE001 — logging must never raise
            try:
                logger.log(getattr(logging, level.upper(), logging.INFO), msg)
            except Exception:  # noqa: BLE001
                pass
        return
    target = log if isinstance(log, logging.Logger) else logger
    try:
        method = getattr(target, level, None)
        (method or target.info)(msg)
    except Exception:  # noqa: BLE001
        pass


def _resolve_home(home: Optional[Path]) -> Path:
    """Resolve the home directory to scan.

    Priority: explicit ``home`` arg → ``$VCT_USER_HOME_OVERRIDE`` (the same
    env install.py's boot-service writers honour, so a single monkeypatch
    sandboxes this whole surface under test) → ``Path.home()``.
    """
    if home is not None:
        return Path(home)
    override = os.environ.get("VCT_USER_HOME_OVERRIDE", "").strip()
    if override:
        return Path(override)
    return Path.home()


def _systemctl_available_default() -> bool:
    """Real ``systemctl`` presence probe (``shutil.which``)."""
    return shutil.which("systemctl") is not None


def _run_systemctl_default(argv: Sequence[str]) -> tuple[int, str, str]:
    """Run a real ``systemctl`` command, returning ``(rc, stdout, stderr)``.

    Never raises: an OSError (binary vanished mid-run, permission) is mapped to
    a non-zero rc so the caller's conservative "systemctl invocation error →
    leave alone" branch fires. A hung ``disable --now`` is bounded by a
    timeout — a stuck systemctl must not stall the whole update.
    """
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def _resolve_module_default(
    python: str, module: str, pythonpath: str
) -> Optional[bool]:
    """Answer "is ``module`` importable by ``python`` under ``pythonpath``".

    TRI-STATE contract (L-2 — a probe that COULD NOT RUN must never be read as
    proof the module is gone):

      - ``True``  → the child cleanly reported the module is importable
                    (``rc == 0`` and the ``OK`` sentinel).
      - ``False`` → the child cleanly reported the module is MISSING
                    (``rc == 0`` and the ``MISSING`` sentinel). This is the ONLY
                    outcome that classifies the unit as provably broken.
      - ``None``  → the probe could not run reliably: the interpreter could not
                    be started (``OSError`` — ``env -S`` split, PATH-relative
                    interpreter not on this process's PATH, missing binary), it
                    timed out, exited non-zero, or ``find_spec`` itself RAISED
                    (a parent package that errors on import — an error condition,
                    not a clean "module-not-found"). The caller treats ``None``
                    as a conservative leave-alone (probe_failed), never a
                    retire — see :func:`_probe_module`.

    Runs ``importlib.util.find_spec`` in a SUBPROCESS of the unit's configured
    interpreter with the unit's PYTHONPATH — the only faithful check, since
    this process's ``sys.path`` says nothing about what the unit's interpreter
    can import.
    """
    # Sentinels are emitted on rc==0. A find_spec that RAISES prints ERROR
    # (probe could not answer → None); only a clean spec-is-None prints MISSING.
    probe = (
        "import importlib.util, sys\n"
        "name = sys.argv[1]\n"
        "try:\n"
        "    spec = importlib.util.find_spec(name)\n"
        "    sys.stdout.write('OK' if spec is not None else 'MISSING')\n"
        "except Exception:\n"
        "    sys.stdout.write('ERROR')\n"
    )
    env = dict(os.environ)
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    else:
        env.pop("PYTHONPATH", None)
    try:
        proc = subprocess.run(
            [python, "-c", probe, module],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Interpreter could not be started (env -S form, relative interp not on
        # PATH, missing binary) or the run raised → probe failed → None.
        return None
    if proc.returncode != 0:
        # Non-zero for any reason (crash, syntax, refused) → cannot trust the
        # verdict → probe failed → None.
        return None
    sentinel = proc.stdout.strip()
    if sentinel == "OK":
        return True
    if sentinel == "MISSING":
        return False
    # ERROR sentinel (find_spec raised) OR no/garbled sentinel → probe failed.
    return None


def _slugify(name: str) -> str:
    """Turn a unit filename into a condition-ID-safe slug."""
    stem = name[:-len(".service")] if name.endswith(".service") else name
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "unit"


def _strip_exec_prefixes(cmd: str) -> str:
    """Drop systemd's leading ExecStart special-prefix chars (``-@+!:``)."""
    return cmd.lstrip(_EXEC_PREFIX_CHARS).strip()


def _parse_execstart(text: str) -> Optional[str]:
    """Return the LAST ``ExecStart=`` value, prefix-stripped, or None.

    None means "no ExecStart line" — an unparseable/incomplete unit the caller
    leaves alone.

    LIMITATION (N-3): "last ExecStart wins" is a deliberate simplification, not
    full systemd semantics:
      - ``Type=oneshot`` may declare MULTIPLE ``ExecStart=`` lines run in
        sequence; we only check the last one (earlier commands go unverified).
      - An empty ``ExecStart=`` RESETS the list; we do not model the reset.
      - drop-in ``*.service.d/*.conf`` overrides are NOT consulted (only the
        main unit file is read).
    For the single-ExecStart shapes VCO's own writers emit this is exact; for
    the exotic shapes above the outcome is conservative (we check the last
    command and, if it resolves, leave the unit alone — we never retire on an
    unchecked earlier command).
    """
    matches = _EXECSTART_RE.findall(text)
    if not matches:
        return None
    return _strip_exec_prefixes(matches[-1])


def _parse_pythonpath(text: str) -> str:
    """Return the effective ``PYTHONPATH`` from the unit's Environment= lines.

    systemd merges multiple ``Environment=`` lines; a later assignment of the
    same key wins. Each line may carry several space-separated ``K=V`` pairs
    (optionally quoted). We scan in order and keep the last ``PYTHONPATH=``.
    Empty string when unset.
    """
    result = ""
    for raw in _ENVIRONMENT_RE.findall(text):
        for token in _split_environment_tokens(raw):
            if token.startswith("PYTHONPATH="):
                result = token[len("PYTHONPATH="):]
    return result


def _split_environment_tokens(line: str) -> List[str]:
    """Split one ``Environment=`` value into ``K=V`` tokens.

    Handles the simple whitespace-separated form and the quoted form
    (``Environment="K=v with spaces" K2=v2``). Not a full shell parser — good
    enough for the ``K=V`` shapes systemd documents; anything it can't split
    cleanly just fails the ``PYTHONPATH=`` prefix test and is ignored (leaving
    PYTHONPATH empty, the conservative default).
    """
    try:
        import shlex

        return shlex.split(line)
    except ValueError:
        # Unbalanced quotes etc. — fall back to naive whitespace split so a
        # malformed line can't crash the scan.
        return line.split()


def _tokenize_exec(cmd: str) -> List[str]:
    """Tokenise an ExecStart command into argv, honouring shell quoting."""
    try:
        import shlex

        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def _text_references_root(text: str, install_root: Path) -> bool:
    """True when ``text`` references ``install_root`` as a PATH PREFIX.

    ANCHORED match (M-1): the root string must be followed by ``os.sep`` or sit
    at a token boundary (end-of-string / whitespace / shell separator). A bare
    substring test would let ``/home/u/VCO`` match a sibling install's unit
    (``/home/u/VCO2/...``) — claiming a FOREIGN install's unit as ours and, if
    it were broken, retiring it into the WRONG install's backup dir. Requiring a
    separator after the match confines the reference to a genuine path under our
    root (``<root>/.venv/bin/python``, ``<root>/scripts/foo.sh``,
    ``PYTHONPATH=<root>/src``) or the root itself.

    NOTE (conservative miss, by design): a symlinked-vs-resolved root spelling
    mismatch (e.g. unit written with ``/home/u/link`` while our root resolves to
    ``/home/u/real``) will NOT match — the unit is then classified FOREIGN and
    left strictly alone. That is the safe direction (we never act on something
    we cannot positively confirm is ours).
    """
    root_str = str(install_root)
    if not root_str:
        return False
    start = 0
    n = len(root_str)
    while True:
        idx = text.find(root_str, start)
        if idx < 0:
            return False
        end = idx + n
        # Boundary check: the char AFTER the match must be a path separator or
        # a token boundary (EOS / whitespace / a shell separator like : ; " ').
        if end >= len(text):
            return True
        nxt = text[end]
        if nxt == os.sep or nxt in " \t\"':;=":
            return True
        start = idx + 1  # keep scanning — this was a longer-name false hit


def _execstart_references_root(cmd: str, install_root: Path) -> bool:
    """True when the ExecStart command references ``install_root`` (anchored).

    The root path appears either as the interpreter path
    (``<root>/.venv/bin/python``), the script path (``<root>/scripts/foo.sh``),
    or a ``-m`` target's PYTHONPATH (checked separately). Conservative: a unit
    that does NOT mention this root is FOREIGN and left strictly alone. Uses the
    anchored :func:`_text_references_root` so a sibling install cannot match.
    """
    return _text_references_root(cmd, install_root)


@dataclass
class _EntryPoint:
    """The parsed, checkable entrypoint of a unit's ExecStart."""

    kind: str  # "module" | "exec"
    python: Optional[str] = None
    module: Optional[str] = None
    exec_path: Optional[str] = None
    # R2-8: True when the exec target is a SCRIPT run VIA an interpreter
    # (``python script.py``, ``bash wrapper.sh``) rather than a direct-exec head
    # (``/opt/bin/worker``). Interpreter-run scripts do NOT need the execute bit
    # — the interpreter reads them — so the exec probe requires only isfile+R_OK
    # for them, keeping X_OK for direct-exec heads. A 644 wrapper (git
    # core.fileMode=false checkouts, editor-created files) must NOT retire a
    # healthy unit.
    interpreter_run: bool = False


def _is_path_like(token: str) -> bool:
    """True when ``token`` looks like a filesystem path (checkable as a file).

    Absolute, or relative with a separator. A bare word (``bash``, ``env``)
    would be PATH-resolved by the shell/exec and is NOT something we can check
    as a file, so it is not path-like.
    """
    return token.startswith("/") or os.sep in token


# Interpreter names whose ``-m`` is the CPython/PyPy module flag. Matched on the
# argv[0] basename: ``python`` / ``python3`` / ``python3.12`` (CPython), ``pypy``
# / ``pypy3`` (PyPy), and ``py`` (the Windows Python Launcher ``py.exe`` — also a
# common POSIX alias), each with an optional ``.exe`` suffix. A NON-python head
# (``tool``, ``server``, a bare wrapper) never has a ``-m`` module flag — its
# ``-m`` is a subcommand option and must not drive a find_spec probe (L-1: a
# false module classification of a healthy unit leads to a destructive
# false-positive retirement).
_PYTHON_INTERP_RE = re.compile(
    r"^(?:python[0-9.]*|pypy[0-9.]*|py)(?:\.exe)?$", re.IGNORECASE
)

# Interpreter options that consume the FOLLOWING argv token as their value
# (``python -W ignore -m mod`` — ``ignore`` is -W's arg, NOT the first
# positional). Scanning for "the first positional before -m" must skip these
# pairs so an option value is never mistaken for a script path.
_PY_OPT_TAKES_ARG = frozenset({"-W", "-X", "--check-hash-based-pycs"})


def _looks_like_python_interpreter(head: str) -> bool:
    """True when ``head``'s basename looks like a Python interpreter."""
    return bool(_PYTHON_INTERP_RE.match(os.path.basename(head)))


# Shell interpreters whose ``-c`` argument is an inline COMMAND STRING, not a
# script path (``bash -c '<root>/scripts/foo.sh --daemon'``). The quoted command
# string is ONE shlex token that CONTAINS a ``/`` → _is_path_like would classify
# it as an exec path → isfile fails → false retire (R2-10). Mirror the python
# ``-c`` rule: a shell head running ``-c`` is unparseable → leave the unit alone.
_SHELL_INTERP_RE = re.compile(
    r"^(?:ba|z|da|a|)sh(?:\.exe)?$", re.IGNORECASE
)


def _looks_like_shell_interpreter(head: str) -> bool:
    """True when ``head``'s basename is a POSIX shell (``sh``/``bash``/``zsh``/
    ``dash``/``ash``)."""
    return bool(_SHELL_INTERP_RE.match(os.path.basename(head)))


def _find_python_module(head: str, rest: List[str]) -> Optional[str]:
    """Return the ``-m`` module name when ``head rest`` is a real ``python -m``.

    Faithful to CPython's CLI: ``-m`` is the module flag ONLY when

      1. ``head`` is a Python interpreter (basename matches ``python*``/``pypy*``),
         AND
      2. ``-m`` appears among the INTERPRETER options — i.e. before the first
         positional (non-flag) token. Once a positional (a script path, or a
         ``-c`` command) is seen, option processing has ended and any later
         ``-m`` is a script ARGUMENT, not the module flag.

    Returns the module name on a match, or None (``head rest`` is not a
    ``python -m`` invocation — the caller then treats it as an exec shape).

    Examples (the L-1 false positives this closes):
      - ``python -m rl_server --port 9000``      → ``rl_server``  (module)
      - ``python -W ignore -m rl_server``        → ``rl_server``  (module; -W arg skipped)
      - ``python server.py --mode x -m y``       → None (server.py positional precedes -m)
      - ``python -c 'code' -m y``                → None (-c ends options; -m is a cmd arg)
      - ``tool serve -m production``             → None (tool is not a python interp)
    """
    if not _looks_like_python_interpreter(head):
        return None

    i = 0
    n = len(rest)
    while i < n:
        token = rest[i]
        if token == "-m":
            module = rest[i + 1] if i + 1 < n else ""
            return module or None  # `-m` with no module name → not classifiable
        if token == "-c":
            # -c terminates option processing (the next token is the command
            # string; the remainder is sys.argv). No module flag applies.
            return None
        if token in _PY_OPT_TAKES_ARG:
            # Skip this option AND its value token so the value can't be read
            # as the first positional.
            i += 2
            continue
        if token.startswith("-"):
            # Any other flag (``-O``, ``-B``, ``-su``, combined short flags…):
            # still in interpreter-option territory, keep scanning.
            i += 1
            continue
        # First positional (script path or bare word) — interpreter options are
        # done; there is no module flag before it.
        return None
    return None


def _classify_entrypoint(argv: List[str]) -> Optional[_EntryPoint]:
    """Classify an ExecStart argv into a checkable entrypoint, or None.

    Recognised shapes:
      - ``<python> -m <module> [args...]`` → module entrypoint (find_spec), ONLY
        when ``<python>`` is a real interpreter and ``-m`` precedes the first
        positional (see :func:`_find_python_module` for the CLI semantics).
      - ``<abs-path> [args...]``           → exec entrypoint (exists+X).
      - ``/usr/bin/env <interp> ...``      → unwrap ``env`` then re-classify.
      - ``<interp> <script-path> [args]``  → the SCRIPT path is the checkable
        exec target (e.g. ``bash /opt/wrapper.sh`` — bash is the interpreter,
        the wrapper is what must exist; ``python server.py`` — server.py is the
        script even if a later arg happens to be ``-m``).

    None → unparseable/unsupported shape → caller leaves the unit alone.
    """
    if not argv:
        return None

    head = argv[0]
    rest = argv[1:]

    # Unwrap `/usr/bin/env <prog> ...` — env just resolves <prog> on PATH.
    if os.path.basename(head) == "env" and rest:
        return _classify_entrypoint(rest)

    # `<shell> -c '<command string>'` — the -c argument is an inline COMMAND, not
    # a script path (R2-10). Mirror the python -c rule: unparseable → leave alone.
    # A quoted command string that happens to contain ``/`` would otherwise be
    # _is_path_like → classified as an exec path → isfile fails → false retire of
    # a healthy unit (``ExecStart=bash -c '<root>/scripts/foo.sh --daemon'``).
    if _looks_like_shell_interpreter(head) and "-c" in rest:
        return None

    # `<python> -m <module>` form — proper CLI parse (L-1). Only a genuine
    # interpreter running `-m` before its first positional counts; anything
    # else (a `-m` after a script path, a non-python head, `-c`) falls through
    # to the exec-shape logic below and is checked as a file, never probed.
    module = _find_python_module(head, rest)
    if module is not None:
        return _EntryPoint(kind="module", python=head, module=module)

    # An INTERPRETER head (python or bash/sh) runs a SCRIPT — the first
    # path-like argument is the checkable target, not the interpreter binary
    # (``python server.py``, ``bash /opt/wrapper.sh``). Per L-1: a python head
    # with a `-m` that did NOT qualify above (e.g. ``python server.py -m y``)
    # is an exec on the SCRIPT path, never on the interpreter. Skip leading
    # option flags (``-lc``, ``-O`` …) until the first path-like token.
    head_is_interpreter = (
        _looks_like_python_interpreter(head) or _looks_like_shell_interpreter(head)
    )
    if head_is_interpreter or not _is_path_like(head):
        for token in rest:
            if _is_path_like(token):
                # R2-8: the script is READ by the interpreter, so it needs no
                # execute bit — mark interpreter_run so the exec probe requires
                # only isfile+R_OK (a 644 wrapper is healthy).
                return _EntryPoint(
                    kind="exec", exec_path=token, interpreter_run=head_is_interpreter
                )
        # No script positional under an interpreter head:
        #   - python head → the interpreter binary itself is the only
        #     checkable file (a bare ``python`` REPL unit — exists check is a
        #     safe conservative resolve).
        #   - bare-word non-python head (``bash`` with no script) → nothing
        #     path-like to check → unparseable, leave alone.
        if _is_path_like(head):
            # A direct-exec head (the interpreter binary itself is the target):
            # it DOES need the execute bit, so interpreter_run stays False.
            return _EntryPoint(kind="exec", exec_path=head)
        return None

    # Head is itself a path (a non-interpreter executable) → it is the exec
    # target (``/opt/vco/bin/worker --flag``). Direct-exec: needs +x.
    return _EntryPoint(kind="exec", exec_path=head)


def _probe_module(
    ep: _EntryPoint,
    pythonpath: str,
    resolve_module: Callable[[str, str, str], Optional[bool]],
    log: Any,
) -> Optional[bool]:
    """Return True (resolves), False (provably broken), or None (probe failed).

    TRI-STATE, propagated FAITHFULLY from the resolver (L-2). A ``None`` means
    the resolution check itself could not run reliably — the caller treats
    ``None`` as a conservative leave-alone (probe_failed), NEVER a retire. Only
    a clean ``False`` (the interpreter ran and positively reported the module
    missing) drives a retirement. The resolver's ``None`` MUST NOT be coerced to
    ``False`` here — that coercion is exactly the bug the L-2 finding named.
    """
    assert ep.python is not None and ep.module is not None
    # If the configured interpreter path is absolute and absent, we cannot run
    # the probe — that is a probe failure (None), not proof the module is gone.
    if os.path.isabs(ep.python) and not os.path.exists(ep.python):
        _log(
            log, "info",
            f"[vct] unit reconcile: interpreter {ep.python!r} not found — "
            f"leaving unit alone (cannot verify module {ep.module!r})",
        )
        return None
    try:
        verdict = resolve_module(ep.python, ep.module, pythonpath)
    except Exception as exc:  # noqa: BLE001 — probe error → leave alone
        _log(
            log, "warning",
            f"[vct] unit reconcile: module probe raised for {ep.module!r} "
            f"({exc}) — leaving unit alone",
        )
        return None
    # Preserve the tri-state exactly: True → resolves, False → provably broken,
    # None → probe could not run reliably (leave-alone). Do NOT bool()-coerce.
    if verdict is None:
        _log(
            log, "info",
            f"[vct] unit reconcile: module probe for {ep.module!r} via "
            f"{ep.python!r} could not run reliably — leaving unit alone",
        )
        return None
    # R2-13: a PATH-relative interpreter (``python3`` not ``/abs/python3``) is
    # resolved via THIS process's PATH, which can differ from systemd's own PATH
    # for the unit. So a ``False`` verdict from a non-absolute interpreter is
    # UNTRUSTWORTHY — we may have run a different ``python3`` that lacks the
    # module the unit's own interpreter has → false retire. Downgrade False→None
    # (leave-alone) for non-absolute interpreters. A ``True`` stays trustworthy
    # (a resolvable python3 that HAS the module is proof the module is
    # installed; keeping the unit is the safe direction regardless).
    if verdict is False and not os.path.isabs(ep.python):
        _log(
            log, "info",
            f"[vct] unit reconcile: module {ep.module!r} not found via "
            f"PATH-relative interpreter {ep.python!r} — this process's PATH may "
            f"differ from the unit's; treating as probe-failed (leaving alone)",
        )
        return None
    return bool(verdict)


def _probe_exec(ep: _EntryPoint) -> bool:
    """True when the exec path exists and (for direct-exec targets) is executable.

    R2-8: an INTERPRETER-run script (``python script.py``, ``bash wrapper.sh``)
    is READ by the interpreter and needs NO execute bit — requiring X_OK on it
    turned a healthy 644 wrapper (git ``core.fileMode=false`` checkouts, user-
    authored units, editor-created files) into a false-positive retirement. For
    interpreter-run targets require only ``isfile`` + ``R_OK`` (the interpreter
    must be able to READ it); keep ``X_OK`` for direct-exec heads (which the
    kernel execs directly and DO need the bit).
    """
    assert ep.exec_path is not None
    p = ep.exec_path
    if not os.path.isfile(p):
        return False
    if ep.interpreter_run:
        return os.access(p, os.R_OK)
    return os.access(p, os.X_OK)


def _utc_stamp() -> str:
    """Compact ISO-8601 UTC stamp for backup filenames (no separators)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup_move(src: Path, dst_dir: Path, stamped_name: str) -> Optional[Path]:
    """Move ``src`` into ``dst_dir`` under ``stamped_name``. Never delete.

    Returns the destination path on success, None on failure (soft-fail — the
    caller decides whether to proceed; for the UNIT backup a failed move aborts
    the retirement so we never disable a unit we couldn't preserve).
    """
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / stamped_name
        shutil.move(str(src), str(dst))
        return dst
    except OSError:
        return None


def _rotate_oversized_logs(
    unit_text: str,
    dst_dir: Path,
    stamp: str,
    unit_slug: str,
    log: Any,
) -> Optional[Path]:
    """Backup-move any ``append:`` log over the threshold. Best-effort.

    Returns the FIRST rotated log's backup path (for the record), or None when
    nothing was rotated. A log that is missing or under the threshold is left
    exactly where it is (leave-alone). Multiple append sinks (StandardOutput +
    StandardError to different files) are each considered.

    M-3(a) symlink guard: a symlinked ``append:`` log is NEVER followed. A
    cross-filesystem ``shutil.move`` on a symlink copies the TARGET's content
    and unlinks the link — we would silently rotate whatever the link points at.
    ``is_symlink()`` is checked BEFORE ``is_file()`` (which follows links).

    M-3(b) collision-safe naming: the backup name is prefixed with the retiring
    unit's slug (``<unit-slug>.<logname>.<stamp>.bak``). Two units retired in the
    SAME pass (same second-stamp) with same-named logs (``server.log``) would
    otherwise collide on ``<logname>.<stamp>.bak`` and ``shutil.move`` →
    ``os.rename`` would silently replace — destroying the first unit's backup.
    """
    first_backup: Optional[Path] = None
    seen: set[str] = set()
    for raw_path in _STD_APPEND_RE.findall(unit_text):
        log_path_str = raw_path.strip().strip('"')
        if not log_path_str or log_path_str in seen:
            continue
        seen.add(log_path_str)
        log_path = Path(os.path.expanduser(log_path_str))
        try:
            # M-3(a): never follow a symlinked log — moving it would rotate the
            # link TARGET's content (possibly a file we don't own) and break the
            # link. Leave symlinked logs strictly alone.
            if log_path.is_symlink():
                _log(
                    log, "info",
                    f"[vct] unit reconcile: append log {log_path} is a symlink "
                    f"— leaving it in place (never follow links)",
                )
                continue
            if not log_path.is_file():
                continue
            size = log_path.stat().st_size
        except OSError:
            continue
        if size <= LOG_ROTATE_THRESHOLD_BYTES:
            continue  # under threshold → leave alone
        # M-3(b): unit-slug prefix keeps same-named logs from different units
        # (retired in the same second) from clobbering each other's backup.
        backup_name = f"{unit_slug}.{log_path.name}.{stamp}.bak"
        moved = _backup_move(log_path, dst_dir, backup_name)
        if moved is not None:
            _log(
                log, "info",
                f"[vct] unit reconcile: rotated oversized append log "
                f"{log_path} ({size} bytes) → {moved}",
            )
            if first_backup is None:
                first_backup = moved
        else:
            _log(
                log, "warning",
                f"[vct] unit reconcile: could not rotate oversized log "
                f"{log_path} — leaving it in place",
            )
    return first_backup


def _restore_command(unit_name: str, backup_path: Path) -> str:
    """The exact command to restore + re-enable a retired unit.

    N-4: shell-quote the backup path and unit destination so a restore command
    is copy-paste-safe even when the install root contains spaces or shell
    metacharacters.
    """
    import shlex

    unit_dir = "~/.config/systemd/user"
    dst = shlex.quote(f"{unit_dir}/{unit_name}")
    src = shlex.quote(str(backup_path))
    unit = shlex.quote(unit_name)
    return (
        f"cp {src} {dst} && "
        f"systemctl --user daemon-reload && "
        f"systemctl --user enable --now {unit}"
    )


def _reenable_command(unit_name: str) -> str:
    """The command to re-enable a still-in-place unit (no file restore).

    Used by the M-2 disable-without-backup record: the unit file was never
    moved, only its enablement symlinks were removed by ``disable --now``.
    """
    import shlex

    unit = shlex.quote(unit_name)
    return f"systemctl --user enable --now {unit}"


def _write_restore_sidecar(
    unit_name: str, backup_path: Path, stamp: str, log: Any
) -> None:
    """M-4: write ``<backup>.RESTORE.txt`` next to the retired unit's backup.

    Best-effort, soft-fail. The deferral record self-clears after one update
    cycle, but this sidecar lives beside the ``.bak`` and survives record
    cleanup — so the restore instructions are never lost even if the user
    updates twice before reading ``UPDATE_DEFERRED.md``.
    """
    try:
        sidecar = backup_path.parent / f"{backup_path.name}.RESTORE.txt"
        sidecar.write_text(
            f"# VCO retired a stale systemd user unit: {unit_name}\n"
            f"# Retired at (UTC): {stamp}\n"
            f"# Backup file:       {backup_path}\n"
            f"#\n"
            f"# The unit was DISABLED and its file backup-moved here because its\n"
            f"# entrypoint no longer resolved. To restore + re-enable it, run:\n"
            f"\n"
            f"{_restore_command(unit_name, backup_path)}\n",
            encoding="utf-8",
        )
    except OSError as exc:
        _log(
            log, "warning",
            f"[vct] unit reconcile: could not write restore sidecar for "
            f"{unit_name} ({exc}) — the deferral record still carries the "
            f"restore command",
        )


def _make_entry(
    unit_name: str,
    condition_id: str,
    execstart: str,
    reason_detail: str,
    backup_path: Path,
    log_backup_path: Optional[Path],
) -> DeferralEntry:
    """Build the run-report record for a retired unit."""
    log_note = (
        f" Its oversized append log was rotated to `{log_backup_path}`."
        if log_backup_path is not None
        else ""
    )
    return DeferralEntry(
        condition_id=condition_id,
        title=f"Retired stale systemd user unit {unit_name}",
        detected=(
            f"`~/.config/systemd/user/{unit_name}` referenced this install root "
            f"but its entrypoint no longer resolves ({reason_detail}; "
            f"ExecStart=`{execstart}`). The unit was disabled and its file was "
            f"backup-moved to `{backup_path}`.{log_note}"
        ),
        why_deferred=(
            "The unit's entrypoint could not be resolved, so leaving it enabled "
            "would let systemd crashloop a missing target and grow its log "
            "without bound. The unit was disabled and preserved (not deleted); "
            "run the restore command below if you still need it."
        ),
        command_to_apply=_restore_command(unit_name, backup_path),
        severity="info",
    )


def _record_disable_without_backup(
    *,
    name: str,
    execstart: str,
    reason_detail: str,
    deferral_report: Any,
    log: Any,
) -> None:
    """M-2: record a `disable --now`-succeeded-but-backup-failed mutation.

    ``systemctl --user disable --now`` already removed the WantedBy symlinks, so
    the unit will NOT restart — a real state change. Because the file could not
    be backed up it was left in place (recoverable, no ``.bak``), so the record
    carries the RE-ENABLE command (not a file-restore). Warning severity: a
    mutation happened but the paperwork is degraded (no backup path).
    """
    if deferral_report is None:
        return
    condition_id = f"{CONDITION_ID_PREFIX}{_slugify(name)}_backup_failed"
    try:
        deferral_report.add_entry(
            DeferralEntry(
                condition_id=condition_id,
                title=f"Disabled stale systemd user unit {name} (backup failed)",
                detected=(
                    f"`~/.config/systemd/user/{name}` referenced this install "
                    f"root but its entrypoint no longer resolves ({reason_detail}"
                    f"; ExecStart=`{execstart}`). The unit was disabled "
                    f"(`systemctl --user disable --now {name}`) to stop the "
                    f"crashloop, but its file could NOT be backed up, so the "
                    f"file was left in place (recoverable)."
                ),
                why_deferred=(
                    "The disable succeeded (enablement symlinks removed) but the "
                    "backup-move failed, so no `.bak` exists. The unit file is "
                    "still on disk; if you fix its entrypoint you can re-enable "
                    "it with the command below, or delete the file to retire it "
                    "permanently."
                ),
                command_to_apply=_reenable_command(name),
                severity="warning",
            )
        )
    except Exception as exc:  # noqa: BLE001 — record is best-effort
        _log(
            log, "warning",
            f"[vct] unit reconcile: could not record disable-without-backup of "
            f"{name} ({exc})",
        )


def reconcile_stale_units(
    install_root: Path,
    *,
    deferral_report: Any = None,
    home: Optional[Path] = None,
    systemctl_available: Optional[Callable[[], bool]] = None,
    systemctl_runner: Optional[Callable[[Sequence[str]], tuple[int, str, str]]] = None,
    resolve_module: Optional[Callable[[str, str, str], Optional[bool]]] = None,
    log: Any = None,
) -> ReconcileResult:
    """Reconcile stale systemd user units referencing ``install_root``.

    Linux-only; a graceful no-op on other platforms and when ``systemctl`` is
    absent. See the module docstring for the full contract. Soft-fails
    throughout — this never raises into the update flow.

    Args:
        install_root: The current install root; only units whose ExecStart
            references THIS root are considered (others are FOREIGN, untouched).
        deferral_report: An in-memory ``DeferralReport`` (or None). Each
            retirement adds one :class:`DeferralEntry` so the action lands in
            the run's single final write. None → actions still happen and are
            logged; just not recorded to the report.
        home: Home dir to scan (test seam). Defaults per :func:`_resolve_home`.
        systemctl_available / systemctl_runner / resolve_module: injection
            seams (test). Default to real ``systemctl`` / find_spec-subprocess.
        log: Optional logger / callable for the soft-fail + action trail.

    Returns:
        A :class:`ReconcileResult` whose ``.actions`` records the per-unit
        decision (retired vs left-alone + reason). Tests assert on this.
    """
    result = ReconcileResult()

    # Non-Linux → graceful no-op (systemd user units are a Linux concept).
    if platform.system() != "Linux":
        return result

    avail = systemctl_available or _systemctl_available_default
    try:
        if not avail():
            _log(
                log, "info",
                "[vct] unit reconcile: systemctl not available — skipping",
            )
            return result
    except Exception as exc:  # noqa: BLE001 — probe error → conservative skip
        _log(log, "warning",
             f"[vct] unit reconcile: systemctl probe failed ({exc}) — skipping")
        return result

    runner = systemctl_runner or _run_systemctl_default
    resolver = resolve_module or _resolve_module_default

    home_dir = _resolve_home(home)
    unit_dir = home_dir / ".config" / "systemd" / "user"
    try:
        if not unit_dir.is_dir():
            return result
        unit_files = sorted(unit_dir.glob("*.service"))
    except OSError as exc:
        _log(log, "warning",
             f"[vct] unit reconcile: cannot list {unit_dir} ({exc}) — skipping")
        return result

    install_root = Path(install_root)
    backup_dir = install_root / RETIRED_UNITS_REL

    for unit_path in unit_files:
        try:
            action = _reconcile_one(
                unit_path=unit_path,
                install_root=install_root,
                backup_dir=backup_dir,
                runner=runner,
                resolver=resolver,
                deferral_report=deferral_report,
                log=log,
            )
        except Exception as exc:  # noqa: BLE001 — one unit must never break the pass
            _log(
                log, "warning",
                f"[vct] unit reconcile: unexpected error on {unit_path.name} "
                f"({exc}) — leaving it alone",
            )
            action = UnitAction(
                unit_name=unit_path.name, acted=False, reason="unexpected_error",
            )
        result.actions.append(action)

    return result


def _reconcile_one(
    *,
    unit_path: Path,
    install_root: Path,
    backup_dir: Path,
    runner: Callable[[Sequence[str]], tuple[int, str, str]],
    resolver: Callable[[str, str, str], Optional[bool]],
    deferral_report: Any,
    log: Any,
) -> UnitAction:
    """Reconcile a single unit file. Returns the decision taken.

    A symlink is a system-unit alias / user redirect we do not own — leave it
    alone. Only regular readable ``.service`` files reach the parse.
    """
    name = unit_path.name

    if unit_path.is_symlink():
        return UnitAction(unit_name=name, acted=False, reason="symlink_left_alone")

    try:
        text = unit_path.read_text(encoding="utf-8")
    except OSError:
        _log(log, "info",
             f"[vct] unit reconcile: {name} unreadable — leaving alone")
        return UnitAction(unit_name=name, acted=False, reason="unreadable")

    execstart = _parse_execstart(text)
    if execstart is None:
        return UnitAction(unit_name=name, acted=False, reason="execstart_unparseable")

    pythonpath = _parse_pythonpath(text)

    # Does this unit reference our install root at all (in the command OR its
    # PYTHONPATH)? If not, it is FOREIGN — strictly leave alone. Both checks are
    # ANCHORED (M-1): a sibling install (``<root>2/...``) must not match.
    if not (_execstart_references_root(execstart, install_root)
            or _text_references_root(pythonpath, install_root)):
        return UnitAction(unit_name=name, acted=False, reason="foreign_unit")

    argv = _tokenize_exec(execstart)
    ep = _classify_entrypoint(argv)
    if ep is None:
        return UnitAction(unit_name=name, acted=False, reason="execstart_unparseable")

    # Resolve the entrypoint.
    if ep.kind == "module":
        verdict = _probe_module(ep, pythonpath, resolver, log)
        if verdict is None:
            return UnitAction(unit_name=name, acted=False, reason="probe_failed")
        if verdict:
            return UnitAction(unit_name=name, acted=False, reason="module_resolves")
        reason_detail = f"module `{ep.module}` not importable by `{ep.python}`"
    else:  # exec
        if _probe_exec(ep):
            return UnitAction(unit_name=name, acted=False, reason="exec_resolves")
        reason_detail = f"exec path `{ep.exec_path}` missing or not executable"

    # ── Provably broken → retire. ──────────────────────────────────────────
    return _retire_unit(
        unit_path=unit_path,
        unit_text=text,
        execstart=execstart,
        reason_detail=reason_detail,
        install_root=install_root,
        backup_dir=backup_dir,
        runner=runner,
        deferral_report=deferral_report,
        log=log,
    )


def _retire_unit(
    *,
    unit_path: Path,
    unit_text: str,
    execstart: str,
    reason_detail: str,
    install_root: Path,
    backup_dir: Path,
    runner: Callable[[Sequence[str]], tuple[int, str, str]],
    deferral_report: Any,
    log: Any,
) -> UnitAction:
    """Disable + backup-move a provably-broken unit; record the action.

    Order matters:
      1. ``systemctl --user disable --now`` — stop the crashloop first. A
         non-zero rc → conservative leave-alone (do NOT move the file behind
         systemd's back; the unit stays as-is and the user can retry).
      2. Backup-MOVE the unit file. A failed move after a successful disable is
         reported but the file is left where systemd expects it (we do not want
         a disabled-but-still-referenced dangling unit — leave it recoverable).
      3. Rotate an oversized append log alongside the unit.
      4. Record the retirement in the deferral report with the restore command.
    """
    name = unit_path.name
    stamp = _utc_stamp()

    rc, _out, err = runner(["systemctl", "--user", "disable", "--now", name])
    if rc != 0:
        _log(
            log, "warning",
            f"[vct] unit reconcile: `systemctl --user disable --now {name}` "
            f"failed (rc={rc}: {err.strip()}) — leaving unit alone",
        )
        return UnitAction(unit_name=name, acted=False, reason="systemctl_error")

    backup_name = f"{name}.{stamp}.bak"
    backup_path = _backup_move(unit_path, backup_dir, backup_name)
    if backup_path is None:
        # M-2: `disable --now` already removed the WantedBy symlinks — that is a
        # real state mutation. The paperwork rule is "record every mutation", so
        # even though we could NOT back up the file (and therefore leave it in
        # place, recoverable), emit a warning-severity record naming the disable
        # and the re-enable command. Otherwise the mutation would be invisible
        # outside a warn log line.
        _log(
            log, "warning",
            f"[vct] unit reconcile: disabled {name} but could not back up its "
            f"file — leaving the file in place (recoverable)",
        )
        _record_disable_without_backup(
            name=name,
            execstart=execstart,
            reason_detail=reason_detail,
            deferral_report=deferral_report,
            log=log,
        )
        return UnitAction(unit_name=name, acted=False, reason="backup_failed")

    # Rotate an oversized append log (best-effort; never gates the retirement).
    # Pass the unit slug so same-named logs from different units retired in one
    # pass can't collide on the backup name (M-3(b)).
    unit_slug = _slugify(name)
    log_backup = _rotate_oversized_logs(
        unit_text, backup_dir, stamp, unit_slug, log
    )

    # M-4: write a durable restore sidecar next to the backup. The deferral
    # RECORD self-clears after one update cycle (owned drop-when-absent), so a
    # user who updates twice before reading UPDATE_DEFERRED.md would be left with
    # only a `.bak` and no restore instructions. The sidecar survives record
    # cleanup — it lives next to the backup, not in the report.
    _write_restore_sidecar(name, backup_path, stamp, log)

    condition_id = f"{CONDITION_ID_PREFIX}{unit_slug}"
    _log(
        log, "info",
        f"[vct] unit reconcile: retired stale unit {name} "
        f"({reason_detail}) → backup {backup_path}",
    )

    if deferral_report is not None:
        try:
            deferral_report.add_entry(
                _make_entry(
                    unit_name=name,
                    condition_id=condition_id,
                    execstart=execstart,
                    reason_detail=reason_detail,
                    backup_path=backup_path,
                    log_backup_path=log_backup,
                )
            )
        except Exception as exc:  # noqa: BLE001 — record is best-effort
            _log(
                log, "warning",
                f"[vct] unit reconcile: could not record retirement of {name} "
                f"({exc})",
            )

    return UnitAction(
        unit_name=name,
        acted=True,
        reason=reason_detail,
        backup_path=backup_path,
        log_backup_path=log_backup,
        condition_id=condition_id,
    )


__all__ = [
    "CONDITION_ID_PREFIX",
    "LOG_ROTATE_THRESHOLD_BYTES",
    "RETIRED_UNITS_REL",
    "ReconcileResult",
    "UnitAction",
    "reconcile_stale_units",
]
