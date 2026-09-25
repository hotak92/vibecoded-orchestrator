# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""ONE home for "is the launcher GUI running, and may I start it tray-only?".

v0.2.95, ruling **R2** ("the launcher and the hub must auto-start when VS Code
starts"). The third sibling of :mod:`vco_lib.hub_ensure` and
:mod:`vco_lib.gateway_ensure`, deliberately shaped like them — same subcommand
style, same ``--json`` / ``--shell`` output, the same "1 and 2 are not ours"
exit-code convention — so the one SessionStart hook that already ensures the
hub and the gateway ensures this too without learning a third contract.

Why a session-start leg rather than a login-time boot registration
------------------------------------------------------------------
The ruling names an EVENT: *when VS Code starts*. A boot registration fires at
LOGIN, which is a different event — it starts a GUI for a login that never
opens an editor, it cannot recover a launcher the user quit an hour ago, and
it would need a THIRD home for boot registration next to ``vct-hub``'s own
``--register-boot`` and :mod:`vco_lib.boot_service`. The hook is already
wired to the named event twice over:

* Claude Code fires it on ``SessionStart`` (CLI, Desktop and the VS Code
  panel alike);
* ``templates/.vscode/tasks.json`` runs the same script on VS Code's
  ``folderOpen`` — literally "VS Code started", with no Claude Code session
  required.

So one leg in one already-delivered file covers the ruling on all three OSes,
and it self-heals: the next session brings back a launcher that crashed or was
quit, which a login-time unit cannot.

Three constraints, and the mechanism each one gets
--------------------------------------------------
**No window steal.** The spawn passes ``--start-hidden``. The launcher reads
that flag BEFORE ``tauri::Builder``, flips ``windows[].visible`` to ``false``
in its own config, and creates no visible window at all — nothing is mapped,
nothing is activated, nothing is destroyed. That matters beyond politeness:
window create + activate + destroy as a side effect is what aborted mutter and
took down a whole GNOME session twice on 2026-09-09 (KG
``bare-tokio-spawn-in-sync-fns-kills-tauri-boot-and-tokio-test-masks-it``).
The tray icon is created as usual, so the launcher is one click away.

**No second instance.** ``tauri-plugin-single-instance`` already owns that,
and this module does not add a second guard: it READS the answer with
:func:`vco_lib.dist_binary_repair.scan_for_launcher_pid` — the same process
scan install.py uses to find a launcher holding the binary open — and only
spawns when nothing answers. If the probe still races a launcher that is
mid-boot, the plugin refuses the duplicate, and the flag it was started with
tells the FIRST instance not to take focus either.

**Cheap and idempotent.** The common case — a launcher already running from
the previous session — costs one ``pgrep``/``tasklist`` and stops. Nothing
below it runs; in particular the binary is neither resolved nor read.

Version skew is checked, not assumed
------------------------------------
A launcher binary older than v0.2.95 does not know ``--start-hidden``: it
would fall through to the GUI path and open a window with focus — the exact
outcome this exists to prevent. The state a bundle update can produce (new
hook, old binary) is therefore POSITIVELY confirmed rather than hoped for:
:func:`binary_supports_hidden_start` scans the binary for the flag's literal
bytes, which a Rust release build stores verbatim in its read-only data. A
binary that does not carry the literal is left alone with a named reason.

CLI
---
``python -m vco_lib.launcher_ensure status [--json|--shell]``
    Report the state. Never starts anything, never writes anything.

``python -m vco_lib.launcher_ensure ensure [--json|--shell]``
    Start the launcher, hidden, when every gate says yes.

Exit codes (``1`` and ``2`` are avoided for the reason
:mod:`vco_lib.hub_ensure` documents — an unhandled exception and argparse own
them):

======  ===================================================================
``0``   every leave-alone state, ``running`` and ``started`` included.
``3``   ``binary_too_old`` — the one state a user must act on.
``4``   ``spawn_failed``.
======  ===================================================================

A machine with no launcher binary at all — a fresh clone, CI, a headless
install — is ``binary_not_found``: exit 0, silent, one debug line. That is a
deliberate divergence from :mod:`vco_lib.hub_ensure`, where a missing binary
is loud (exit 3). The hub is required infrastructure for every install; a
launcher GUI on the machine is not.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from vco_lib.hub_ensure import find_dist_binary
from vco_lib.paths import vct_root_dir

__all__ = [
    "APP_STATE_SESSION_AUTOSTART",
    "DEFAULT_SESSION_AUTOSTART",
    "DISABLE_ENV",
    "ENSURE_EXIT_CODES",
    "HIDDEN_START_FLAG",
    "LAUNCHER_BIN_ENV",
    "LauncherEnsureResult",
    "LauncherState",
    "autostart_enabled",
    "binary_supports_hidden_start",
    "decide",
    "display_available",
    "ensure_running",
    "find_launcher_binary",
    "launcher_binary_stem",
    "launcher_pid",
    "launcher_status",
    "main",
]

#: Argv flag the ensure passes, and the launcher's capability marker.
#:
#: MUST MATCH ``launcher/src-tauri/src/lib.rs::HIDDEN_START_FLAG``. Pinned by
#: ``tests/test_v0295_launcher_ensure.py::test_flag_literal_matches_rust``.
#: It is a capability marker as much as a request: the binary scan below looks
#: for exactly these bytes, so renaming it on one side only makes every
#: launcher read as "too old" rather than silently stealing focus.
HIDDEN_START_FLAG = "--start-hidden"

#: Per-machine kill switch. Anything truthy skips the leg entirely — for CI,
#: for a headless server, and for a user who wants the hook to leave their
#: desktop alone without touching the launcher's preference store.
DISABLE_ENV = "VCT_DISABLE_LAUNCHER_AUTOSTART"

#: Explicit binary override, the ``VCT_HUB_BIN`` of this module.
LAUNCHER_BIN_ENV = "VCT_LAUNCHER_BIN"

#: ``app_state`` key holding the user-visible preference.
#:
#: MUST MATCH ``launcher/src-tauri/src/commands/session_autostart.rs``
#: ``::APP_STATE_SESSION_AUTOSTART``. Pinned by
#: ``tests/test_v0295_launcher_ensure.py::test_pref_key_matches_rust``.
APP_STATE_SESSION_AUTOSTART = "launcher.session_autostart"

#: Shipped default, per the 2026-09-10 ruling: ON. It lives in code on BOTH
#: sides rather than in a seeded row, which is what makes the delivery story
#: "no migration": an install that has never heard of the key reads absence as
#: yes, and only a user who turns it OFF ever gets a row.
DEFAULT_SESSION_AUTOSTART = True


class LauncherState(str, Enum):
    """What the launcher is, and what this module did about it."""

    #: A live launcher process was found — nothing was spawned.
    RUNNING = "running"
    #: A hidden start was requested.
    STARTED = "started"
    #: ``status`` only: nothing is running and nothing was attempted.
    NOT_RUNNING = "not_running"
    #: ``VCT_DISABLE_LAUNCHER_AUTOSTART`` is set.
    DISABLED_BY_ENV = "disabled_by_env"
    #: The user turned the preference off in the launcher's Preferences page.
    DISABLED_BY_PREF = "disabled_by_pref"
    #: Linux with neither ``$DISPLAY`` nor ``$WAYLAND_DISPLAY``: a GUI cannot
    #: start, so starting one is a guaranteed failure, not an attempt.
    NO_DISPLAY = "no_display"
    #: No launcher binary anywhere on the chain. Silent, successful.
    BINARY_NOT_FOUND = "binary_not_found"
    #: A binary that predates ``--start-hidden``. Starting it would open a
    #: window and take focus, so it is NOT started.
    BINARY_TOO_OLD = "binary_too_old"
    #: A binary was found and could not be executed.
    SPAWN_FAILED = "spawn_failed"


ENSURE_EXIT_CODES: dict[LauncherState, int] = {
    LauncherState.RUNNING: 0,
    LauncherState.STARTED: 0,
    LauncherState.NOT_RUNNING: 0,
    LauncherState.DISABLED_BY_ENV: 0,
    LauncherState.DISABLED_BY_PREF: 0,
    LauncherState.NO_DISPLAY: 0,
    LauncherState.BINARY_NOT_FOUND: 0,
    LauncherState.BINARY_TOO_OLD: 3,
    LauncherState.SPAWN_FAILED: 4,
}


@dataclass(frozen=True)
class LauncherEnsureResult:
    """What was found, what was done, and why."""

    state: LauncherState
    #: Always populated — every state is NAMED, the silent ones included.
    reason: str = ""
    pid: Optional[int] = None
    binary: Optional[str] = None
    #: The argv that was (or would be) spawned.
    argv: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return ENSURE_EXIT_CODES[self.state]

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "running": self.state is LauncherState.RUNNING,
            "started": self.state is LauncherState.STARTED,
            "reason": self.reason,
            "pid": self.pid,
            "binary": self.binary,
            "argv": list(self.argv),
        }


# ---------------------------------------------------------------------------
# The probes — each one small, each one injectable from the decision below
# ---------------------------------------------------------------------------


def launcher_binary_stem() -> str:
    """``"vct-launcher"`` — the stem, not the filename.

    :func:`vco_lib.hub_ensure.binary_names` adds the ``.exe`` on Windows; the
    OS → ``launcher/dist/<arch>/`` mapping is
    :func:`vco_lib.hub_ensure.dist_arch_dir`. Neither is re-encoded here: the
    dist-slot mapping already exists in four hand-kept places pinned by
    ``tests/test_launcher_dist_subdir_parity.py``, and a fifth would be the
    duplication that test exists to prevent.
    """
    return "vct-launcher"


def launcher_process_name() -> str:
    """The process-table name to look for.

    Windows' ``tasklist`` filter matches the image name including ``.exe``;
    POSIX ``pgrep -x`` matches ``comm``, which is the bare basename.
    """
    return "vct-launcher.exe" if os.name == "nt" else "vct-launcher"


def launcher_pid(scanner: Optional[Callable[..., Optional[int]]] = None) -> Optional[int]:
    """PID of a running launcher, or ``None``.

    Reads the answer from :func:`vco_lib.dist_binary_repair.scan_for_launcher_pid`
    — the ONE process scan in the tree, already cross-OS and already
    conservative about multiple matches. The launcher's single-instance
    guarantee belongs to ``tauri-plugin-single-instance``; this is a read of
    reality, not a second lock.
    """
    if scanner is None:
        from vco_lib.dist_binary_repair import scan_for_launcher_pid

        scanner = scan_for_launcher_pid
    try:
        return scanner(launcher_process_name())
    except Exception:  # noqa: BLE001 — a probe failure means "unknown", not a crash
        return None


def autostart_enabled(reader: Optional[Callable[[str], Optional[str]]] = None) -> bool:
    """The user-visible preference, defaulting to :data:`DEFAULT_SESSION_AUTOSTART`.

    An absent row, an unreadable ``launcher.db`` (fresh install, never booted)
    or any SQLite error all read as the DEFAULT rather than as "off" — the
    shipped behaviour must not depend on a database being present.

    The truthiness rule MUST MATCH the Rust reader,
    ``vct-launcher-core/src/db/app_state.rs::app_state_get_bool``
    (``matches!(v.as_str(), "true" | "1")``): the launcher WRITES this row and
    the hook READS it, so a disagreement would show the user one thing and do
    another. Pinned by ``tests/test_v0295_launcher_ensure.py``.
    """
    if reader is None:
        from vco_lib.launcher_db_reader import read_app_state_value

        reader = read_app_state_value
    try:
        raw = reader(APP_STATE_SESSION_AUTOSTART)
    except Exception:  # noqa: BLE001 — an unreadable pref is not an opt-out
        return DEFAULT_SESSION_AUTOSTART
    if raw is None:
        return DEFAULT_SESSION_AUTOSTART
    return raw.strip() in ("true", "1")


def display_available(
    env: Optional[Mapping[str, str]] = None, system: Optional[str] = None,
) -> bool:
    """Can a GUI application be started in this environment?

    Only Linux is answered NEGATIVELY, and only on positive evidence: neither
    ``$DISPLAY`` nor ``$WAYLAND_DISPLAY`` is set, which is a Claude Code CLI
    session over SSH, a container, or CI — where starting a GUI is a
    guaranteed failure rather than an attempt.

    macOS and Windows always answer yes: a GUI session is the norm there, and
    the exotic exceptions (a Windows service account, a macOS ssh login) have
    no probe that is honest across versions. Conservative in the right
    direction — the spawn is harmless and the launcher's own startup reports
    its failure.
    """
    os_name = (system or platform.system()).lower()
    if not os_name.startswith("linux"):
        return True
    e = os.environ if env is None else env
    return bool((e.get("DISPLAY") or "").strip() or (e.get("WAYLAND_DISPLAY") or "").strip())


def find_launcher_binary(
    repo_root: Optional[Path] = None,
    extra_dirs: Sequence[Path] = (),
    env: Optional[Mapping[str, str]] = None,
) -> Optional[Path]:
    """Resolve ``vct-launcher``, or ``None``. Never spawns, never raises.

    The discovery chain is :func:`vco_lib.hub_ensure.find_dist_binary` — the
    hub's own four-step walk, parameterised over the stem in v0.2.95 rather
    than copied. ``env`` is threaded so every gate in this module answers ONE
    environment.
    """
    return find_dist_binary(
        launcher_binary_stem(),
        env_override=LAUNCHER_BIN_ENV,
        repo_root=repo_root,
        extra_dirs=extra_dirs,
        env=env,
    )


def binary_supports_hidden_start(
    path: Path, *, chunk_size: int = 1 << 20
) -> bool:
    """Does this binary carry the ``--start-hidden`` literal?

    A positive capability check, by reading — never by running. Probing with
    ``<binary> --version`` would be worse than useless: the launcher's CLI
    dispatch matches two subcommands and falls through to the GUI for
    everything else, so a version probe against an OLD binary would open the
    very window this is here to avoid.

    Rust stores string literals verbatim in the read-only data of a release
    build, so the flag's bytes are present iff the code that parses it is.
    ``tests/test_v0295_launcher_ensure.py`` proves the technique against the
    REAL committed ``launcher/dist/`` binary (searching for a flag that binary
    is known to implement) rather than asserting it from first principles.

    Unreadable file → ``False``: cannot confirm, so do not start.
    """
    needle = HIDDEN_START_FLAG.encode("ascii")
    overlap = len(needle) - 1
    try:
        with open(path, "rb") as handle:
            tail = b""
            while True:
                block = handle.read(chunk_size)
                if not block:
                    return False
                if needle in tail + block:
                    return True
                tail = block[-overlap:] if overlap else b""
    except OSError:
        return False


# ---------------------------------------------------------------------------
# The decision — pure, so every gate has an act AND a leave-alone test
# ---------------------------------------------------------------------------


def decide(
    *,
    pid: Optional[int],
    disabled_by_env: bool,
    preference_on: bool,
    display_ok: bool,
    binary: Optional[Path],
    supports_flag: bool,
) -> LauncherEnsureResult:
    """Everything this module decides, with nothing it touches.

    Order is deliberate. "Already running" comes FIRST so the answer is honest
    for ``status`` even when the preference is off — the question "is the
    launcher up?" has one true answer, and a preference does not change it.
    The kill switch comes next because it must not be overridable by the
    database, and the cheap gates come before the ones that read a file.
    """
    if pid is not None:
        return LauncherEnsureResult(
            state=LauncherState.RUNNING,
            reason=f"the launcher is already running (pid {pid})",
            pid=pid,
        )
    if disabled_by_env:
        return LauncherEnsureResult(
            state=LauncherState.DISABLED_BY_ENV,
            reason=f"{DISABLE_ENV} is set — the launcher is started by hand here",
        )
    if not preference_on:
        return LauncherEnsureResult(
            state=LauncherState.DISABLED_BY_PREF,
            reason=(
                "'Start the launcher with a Claude Code session' is off in the "
                "launcher's Preferences → Startup"
            ),
        )
    if not display_ok:
        return LauncherEnsureResult(
            state=LauncherState.NO_DISPLAY,
            reason=(
                "no $DISPLAY or $WAYLAND_DISPLAY — this session has no desktop "
                "to put a tray icon on"
            ),
        )
    if binary is None:
        return LauncherEnsureResult(
            state=LauncherState.BINARY_NOT_FOUND,
            reason=(
                "no vct-launcher binary found (no $VCT_LAUNCHER_BIN, none under "
                "launcher/dist/, none on $PATH, none in ~/.vct/bin) — this "
                "machine has no launcher GUI to start"
            ),
        )
    if not supports_flag:
        return LauncherEnsureResult(
            state=LauncherState.BINARY_TOO_OLD,
            reason=(
                f"{binary} predates {HIDDEN_START_FLAG} (v0.2.95): starting it "
                "would open a window and take focus, so it was left alone. Run "
                "`python install.py --update` from the orchestrator root to "
                "refresh the launcher binary."
            ),
            binary=str(binary),
        )
    return LauncherEnsureResult(
        state=LauncherState.STARTED,
        reason=f"starting {binary} hidden (tray only)",
        binary=str(binary),
        argv=(str(binary), HIDDEN_START_FLAG),
    )


# ---------------------------------------------------------------------------
# Status and ensure — the I/O around the decision
# ---------------------------------------------------------------------------


def _gather(
    *,
    repo_root: Optional[Path],
    extra_dirs: Sequence[Path],
    scanner: Optional[Callable[..., Optional[int]]],
    pref_reader: Optional[Callable[[str], Optional[str]]],
    env: Optional[Mapping[str, str]],
    system: Optional[str],
) -> LauncherEnsureResult:
    pid = launcher_pid(scanner)
    e = os.environ if env is None else env
    if pid is not None:
        # Short-circuit: the common case reads the process table and stops.
        # Nothing below costs anything on a machine whose launcher is up.
        return decide(
            pid=pid, disabled_by_env=False, preference_on=True,
            display_ok=True, binary=None, supports_flag=True,
        )
    disabled = bool((e.get(DISABLE_ENV) or "").strip())
    # The kill switch short-circuits the SQLite read: `decide` answers
    # DISABLED_BY_ENV before it ever looks at the preference, so the value
    # passed here is never consulted.
    preference_on = True if disabled else autostart_enabled(pref_reader)
    display_ok = display_available(env=e, system=system)
    binary: Optional[Path] = None
    supports = False
    if not disabled and preference_on and display_ok:
        binary = find_launcher_binary(
            repo_root=repo_root, extra_dirs=extra_dirs, env=e,
        )
        supports = binary is not None and binary_supports_hidden_start(binary)
    return decide(
        pid=None, disabled_by_env=disabled, preference_on=preference_on,
        display_ok=display_ok, binary=binary, supports_flag=supports,
    )


def launcher_status(
    *,
    repo_root: Optional[Path] = None,
    extra_dirs: Sequence[Path] = (),
    scanner: Optional[Callable[..., Optional[int]]] = None,
    pref_reader: Optional[Callable[[str], Optional[str]]] = None,
    env: Optional[Mapping[str, str]] = None,
    system: Optional[str] = None,
) -> LauncherEnsureResult:
    """What ``ensure`` WOULD do, without doing it. Spawns nothing."""
    found = _gather(
        repo_root=repo_root, extra_dirs=extra_dirs, scanner=scanner,
        pref_reader=pref_reader, env=env, system=system,
    )
    if found.state is LauncherState.STARTED:
        return LauncherEnsureResult(
            state=LauncherState.NOT_RUNNING,
            reason="not running; every gate says a hidden start is allowed",
            binary=found.binary,
            argv=found.argv,
        )
    return found


def ensure_running(
    *,
    repo_root: Optional[Path] = None,
    extra_dirs: Sequence[Path] = (),
    scanner: Optional[Callable[..., Optional[int]]] = None,
    pref_reader: Optional[Callable[[str], Optional[str]]] = None,
    env: Optional[Mapping[str, str]] = None,
    system: Optional[str] = None,
    spawner: Optional[Callable[[Sequence[str], Optional[Path]], None]] = None,
) -> LauncherEnsureResult:
    """Start the launcher hidden when every gate says yes. Idempotent.

    Every leave-alone case is silent and successful; only a machine that has a
    launcher, wants one, can show one, and has none running is acted on.
    """
    found = _gather(
        repo_root=repo_root, extra_dirs=extra_dirs, scanner=scanner,
        pref_reader=pref_reader, env=env, system=system,
    )
    if found.state is not LauncherState.STARTED:
        return found

    cwd = _spawn_cwd()
    # `is not None`, never `or`: an injected recorder that happens to be an
    # empty container is falsy, and `or` would silently run the REAL spawn in
    # a test that believed it had replaced it.
    launch = _spawn if spawner is None else spawner
    try:
        launch(found.argv, cwd)
    except OSError as exc:
        return LauncherEnsureResult(
            state=LauncherState.SPAWN_FAILED,
            reason=f"could not execute {found.binary}: {exc}",
            binary=found.binary,
            argv=found.argv,
        )
    return found


def _spawn_cwd() -> Path:
    """Where the started launcher's working directory points.

    NEVER the caller's directory, and deliberately NOT the ``repo_root`` the
    caller passed either. A GUI outlives the session that started it, and on
    Windows a process's working directory cannot be renamed or deleted — so
    inheriting the user's project folder would quietly lock it for as long as
    the launcher runs. ``repo_root`` is not a safe substitute: the hooks are
    installed at ``<project>/.claude/hooks/`` and pass ``../..``, which IS the
    user's project.

    So: the orchestrator checkout this module was imported from — always a
    registered project, which is the same reason the model gateway's
    registration pins its secret scope there (R5b) — and the state root as the
    always-present fallback.
    """
    root = _default_repo_root()
    if root is not None and root.is_dir():
        return root
    return vct_root_dir()


def _default_repo_root() -> Optional[Path]:
    """The orchestrator checkout containing this module (``<root>/vco_lib/``)."""
    try:
        return Path(__file__).resolve().parent.parent
    except OSError:
        return None


def _spawn(argv: Sequence[str], cwd: Optional[Path]) -> None:
    """Start the launcher detached, so the hook never waits on a GUI.

    Same detach discipline as :func:`vco_lib.hub_ensure._spawn`: a new session
    on POSIX, ``DETACHED_PROCESS | CREATE_NO_WINDOW`` on Windows so no console
    flashes. stdio goes to ``DEVNULL`` — a GUI writing into the hook's pipe
    would keep the SessionStart hook's stdout open after the hook returns.
    """
    from vco_lib.install_companions import detached_child_env, detached_popen_kwargs

    subprocess.Popen(  # noqa: S603 — argv[0] is a resolved absolute path
        list(argv),
        cwd=str(cwd) if cwd else None,
        env=detached_child_env(),  # the launcher outlives us: no relaunch record
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **detached_popen_kwargs(),
    )


# ---------------------------------------------------------------------------
# CLI — the cross-language call surface (class A of the A>B>C rule)
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m vco_lib.launcher_ensure",
        description=(
            "Report, and optionally perform, a tray-only start of the launcher "
            "GUI (the ONE home; the SessionStart hook calls this instead of "
            "mirroring the logic)."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_text in (
        ("status", "Report the state. Starts nothing, writes nothing."),
        (
            "ensure",
            "Start the launcher hidden when every gate says yes. "
            "0=ok, 3=binary too old, 4=could not start.",
        ),
    ):
        s = sub.add_parser(name, help=help_text)
        s.add_argument(
            "--json", action="store_true",
            help="Print the result as JSON on stdout.",
        )
        s.add_argument(
            "--shell", action="store_true",
            help="Print `VCO_LAUNCHER_*` assignments for a bash `eval` (the .sh "
                 "hook); the .ps1 hook uses --json + ConvertFrom-Json.",
        )
        s.add_argument(
            "--repo-root", default=None, metavar="DIR",
            help="Orchestrator checkout whose launcher/dist/ holds the binary.",
        )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    import json
    import shlex

    args = _build_arg_parser().parse_args(argv)
    repo_root = Path(args.repo_root) if args.repo_root else None
    if args.cmd == "status":
        res = launcher_status(repo_root=repo_root)
    else:
        res = ensure_running(repo_root=repo_root)

    if args.json:
        print(json.dumps(res.to_dict(), sort_keys=True))
    elif args.shell:
        print(f"VCO_LAUNCHER_STATE={shlex.quote(res.state.value)}")
        print(f"VCO_LAUNCHER_PID={shlex.quote(str(res.pid) if res.pid else '')}")
        print(f"VCO_LAUNCHER_REASON={shlex.quote(res.reason)}")
    else:
        print(f"{res.state.value}: {res.reason}")

    # Every non-success path is LOUD: the reason also goes to stderr so a
    # caller that only forwards stderr still says what happened.
    if res.exit_code != 0:
        print(f"[vct] launcher_ensure: {res.reason}", file=sys.stderr)
    return res.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
