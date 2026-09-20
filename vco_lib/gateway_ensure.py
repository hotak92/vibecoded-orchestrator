# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""ONE home for "is the model gateway registered, runnable, and running?".

v0.2.95, R5c. The sibling of :mod:`vco_lib.hub_ensure`, deliberately shaped
like it (same subcommand style, same ``--json`` / ``--shell`` output, the same
"1 and 2 are not ours" exit-code convention) so the SessionStart hook that
already ensures the hub can ensure this too without learning a second contract.
That hook is the ONE caller a session goes through — ruling R20 — and the
gateway rides it rather than getting a second ensure of its own.

What makes this different from the hub's ensure, and why each difference is
deliberate:

* **The gateway is OPT-IN.** The hub is ensured on every machine; the gateway
  is a login-time daemon holding an OAuth passthrough, so nothing here may
  create a registration. "Not registered" is a silent, successful no-op — the
  user's decision, already made.
* **A parked unit must be un-parked first.** The shipped systemd unit bounds a
  crash loop (``StartLimitIntervalSec=600`` / ``StartLimitBurst=5``). That is
  what stops a 1442-restart night, and it is also why a plain ``start`` on a
  unit in ``failed`` does nothing at all: ``reset-failed`` comes first, or the
  ensure is a no-op exactly when it is needed. macOS and Windows have no
  equivalent park — see :func:`vco_lib.boot_service.ensure_commands`.
* **There is no second single-instance guard here.** The daemon owns that (an
  ``O_CREAT|O_EXCL`` pid file plus a ``/health`` identity check), and a start
  that finds a live gateway exits 0. This module READS that pid file, the same
  way ``hub_ensure.is_running`` reads ``hub.pid``, so "already running" is an
  honest answer rather than an opaque "started".
* **"Registered but unrunnable" is its own state.** The eight hours of
  2026-09-10 were exactly that: a unit whose ``ExecStart`` named an interpreter
  which cannot import ``model_router``, enabled, failing, with the launcher
  toggle reporting "registered". Starting it again cannot help, so this module
  does not — it reports, and records a deferral row, and stops.

CLI
---
``python -m vco_lib.gateway_ensure status [--json|--shell]``
    Report the state. Never starts anything, never writes anything.

``python -m vco_lib.gateway_ensure ensure [--json|--shell] [--folder DIR]``
    Start a REGISTERED gateway that is not running. ``--folder`` is the
    managed project whose ledger gets the ``gateway_registered_but_unrunnable``
    row when the registration cannot run; without it nothing is written.

Exit codes (``1`` and ``2`` are avoided for the reasons
:mod:`vco_lib.hub_ensure` documents — an unhandled exception and argparse own
them):

======  ===================================================================
``0``   ``running`` / ``started`` / ``not_registered`` / ``disabled_by_env``
``3``   ``registered_but_unrunnable`` — loud, named, action required.
``4``   ``start_failed`` — registered and runnable, but no way to start it.
======  ===================================================================
"""

from __future__ import annotations

import argparse
import platform
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Sequence

from vco_lib import boot_service
from vco_lib.paths import vct_root_dir

__all__ = [
    "ENSURE_EXIT_CODES",
    "GatewayState",
    "GatewayEnsureResult",
    "ensure_running",
    "gateway_pid",
    "gateway_status",
    "is_running",
    "main",
]


class GatewayState(str, Enum):
    """What the gateway's boot registration is, right now."""

    #: The pid file names a live process — the daemon's own guard says yes.
    RUNNING = "running"
    #: A start was requested through the init system.
    STARTED = "started"
    #: No unit / plist / task. The opt-in was never taken: nothing to do.
    NOT_REGISTERED = "not_registered"
    #: Registered and runnable, but nothing is serving. What ``status``
    #: reports and what ``ensure`` acts on — the two must not share a word,
    #: or "I looked" and "I started it" become indistinguishable in a log.
    REGISTERED_NOT_RUNNING = "registered_not_running"
    #: Registered, but its baked entry point does not answer ``--version``.
    REGISTERED_BUT_UNRUNNABLE = "registered_but_unrunnable"
    #: Registered and runnable, but no init tool could be invoked.
    START_FAILED = "start_failed"
    #: ``VCT_DISABLE_BOOT_SERVICE=1`` — the user starts VCO daemons by hand.
    DISABLED_BY_ENV = "disabled_by_env"


ENSURE_EXIT_CODES: dict[GatewayState, int] = {
    GatewayState.RUNNING: 0,
    GatewayState.STARTED: 0,
    GatewayState.NOT_REGISTERED: 0,
    GatewayState.REGISTERED_NOT_RUNNING: 0,
    GatewayState.DISABLED_BY_ENV: 0,
    GatewayState.REGISTERED_BUT_UNRUNNABLE: 3,
    GatewayState.START_FAILED: 4,
}

#: The deferral condition this module and the doctor both emit. Declared in
#: ``vco_lib/deferral_conditions.toml``; cleared by
#: ``deferral_probes.gateway_exec_still_unrunnable``.
CID_GATEWAY_UNRUNNABLE = "gateway_registered_but_unrunnable"


@dataclass(frozen=True)
class GatewayEnsureResult:
    """What was found, what was done, and why."""

    state: GatewayState
    #: Always populated — every state is NAMED, including the silent ones.
    reason: str = ""
    pid: Optional[int] = None
    #: The argv read back from the INSTALLED artefact (``()`` when absent).
    argv: tuple[str, ...] = ()
    #: The artefact's path, for a report that has to tell the user where.
    unit_path: Optional[str] = None
    #: The secret scope the installed artefact pins (R5b), or ``None``.
    secret_project: Optional[str] = None
    #: Commands actually invoked, for the report and the tests.
    commands: tuple[tuple[str, ...], ...] = ()

    @property
    def exit_code(self) -> int:
        return ENSURE_EXIT_CODES[self.state]

    @property
    def registered(self) -> bool:
        return self.state is not GatewayState.NOT_REGISTERED

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "registered": self.registered,
            "running": self.state is GatewayState.RUNNING,
            "runnable": (
                False if self.state is GatewayState.REGISTERED_BUT_UNRUNNABLE
                else (True if self.registered and self.argv else None)
            ),
            "reason": self.reason,
            "pid": self.pid,
            "argv": list(self.argv),
            "unit_path": self.unit_path,
            "secret_project": self.secret_project,
            "commands": [list(c) for c in self.commands],
        }


# ---------------------------------------------------------------------------
# Reading the daemon's OWN single-instance guard (never a second one)
# ---------------------------------------------------------------------------


def gateway_pid(state_dir: Optional[Path] = None) -> Optional[int]:
    """The pid recorded by the running daemon, or ``None``.

    Same reading as ``hub_ensure.hub_pid``: first line, trimmed, positive int.
    The FILE is the daemon's, written under ``O_CREAT|O_EXCL`` by
    ``model_router.__main__._acquire_single_instance``; this is a read of that
    guard, not a second one.
    """
    from vco_lib.intfile import read_int_line

    root = state_dir if state_dir is not None else vct_root_dir()
    return read_int_line(root / boot_service.GATEWAY_PID_BASENAME, minimum=1)


def is_running(state_dir: Optional[Path] = None) -> bool:
    """True when the recorded pid is still alive.

    A stale pid file (the crash case) reads as NOT running, so the ensure
    starts a fresh daemon — and the daemon's own ``/health`` identity check is
    what settles a REUSED pid, because only it can ask the process what it is.
    """
    pid = gateway_pid(state_dir)
    if pid is None:
        return False
    from vco_lib.deferral_probes import pid_is_alive

    return pid_is_alive(pid)


# ---------------------------------------------------------------------------
# Status — read-only, starts nothing
# ---------------------------------------------------------------------------


def gateway_status(
    *,
    home: Optional[Path] = None,
    system: Optional[str] = None,
    state_dir: Optional[Path] = None,
    verify: bool = True,
    runner=None,
) -> GatewayEnsureResult:
    """The three states the launcher toggle and the doctor both need.

    ``verify=False`` skips the ``--version`` run (the artefact is still read
    back), for a caller that must not spawn — nothing on the request path of
    anything does, but a boot-scope probe might.
    """
    os_name = system or platform.system()
    spec = boot_service.gateway_names_spec(os_name, state_dir=state_dir)
    facts = boot_service.installed_gateway_facts(
        home=home, system=os_name, state_dir=state_dir,
    )
    if boot_service.status(spec, home=home, system=os_name) is (
        boot_service.BootStatus.NOT_INSTALLED
    ):
        return GatewayEnsureResult(
            state=GatewayState.NOT_REGISTERED,
            reason=(
                "the model gateway is not registered to start at login "
                "(opt-in: `vct-model-gateway --register-boot`, or the "
                "launcher's gateway toggle)"
            ),
            unit_path=str(facts.path) if facts.path else None,
        )

    pid = gateway_pid(state_dir)
    running = is_running(state_dir)
    if running:
        return GatewayEnsureResult(
            state=GatewayState.RUNNING,
            reason=f"the model gateway is already running (pid {pid})",
            pid=pid,
            argv=facts.argv,
            unit_path=str(facts.path) if facts.path else None,
            secret_project=facts.secret_project,
        )

    if not facts.argv:
        return GatewayEnsureResult(
            state=GatewayState.REGISTERED_BUT_UNRUNNABLE,
            reason=(
                f"the registration at {facts.path} names no entry point "
                f"({facts.parse_error or 'it could not be read'})"
            ),
            unit_path=str(facts.path) if facts.path else None,
            secret_project=facts.secret_project,
        )

    if verify:
        ok, detail = boot_service.verify_gateway_exec(facts.argv, runner=runner)
        if not ok:
            return GatewayEnsureResult(
                state=GatewayState.REGISTERED_BUT_UNRUNNABLE,
                reason=(
                    "the registered entry point cannot run: "
                    f"`{' '.join(facts.argv)}` → {detail}. Re-run "
                    "`python install.py --update` from the orchestrator root; "
                    "it re-renders this registration from the install venv and "
                    "verifies it before writing."
                ),
                argv=facts.argv,
                unit_path=str(facts.path) if facts.path else None,
                secret_project=facts.secret_project,
            )

    return GatewayEnsureResult(
        state=GatewayState.REGISTERED_NOT_RUNNING,
        reason="registered and runnable, but nothing is serving",
        argv=facts.argv,
        unit_path=str(facts.path) if facts.path else None,
        secret_project=facts.secret_project,
    )


# ---------------------------------------------------------------------------
# Ensure — the SessionStart action
# ---------------------------------------------------------------------------


def ensure_running(
    *,
    home: Optional[Path] = None,
    system: Optional[str] = None,
    state_dir: Optional[Path] = None,
    folder: Optional[Path] = None,
    verify: bool = True,
    runner=None,
    command_runner=None,
) -> GatewayEnsureResult:
    """Start a REGISTERED gateway that is not running. Idempotent.

    The leave-alone cases come first and each is silent-and-successful: the
    kill switch, an unregistered gateway, and one that is already serving.
    Only a registration that is present, runnable and idle is started.
    """
    if boot_service.boot_registration_disabled():
        return GatewayEnsureResult(
            state=GatewayState.DISABLED_BY_ENV,
            reason=(
                f"{boot_service.DISABLE_ENV}=1 — VCO daemons are started by "
                "hand in this environment"
            ),
        )

    found = gateway_status(
        home=home, system=system, state_dir=state_dir, verify=verify,
        runner=runner,
    )
    if found.state in (
        GatewayState.NOT_REGISTERED,
        GatewayState.RUNNING,
    ):
        return found
    if found.state is GatewayState.REGISTERED_BUT_UNRUNNABLE:
        # No start, and no retry: the init system is already retrying this on
        # its own schedule and every attempt fails the same way. What is owed
        # is a report the user will actually see.
        _record_unrunnable(folder, found)
        return found
    if found.state is not GatewayState.REGISTERED_NOT_RUNNING:  # pragma: no cover
        # Defensive: a state added to the enum without a decision here must
        # leave the machine alone rather than fall through into a start.
        return found

    os_name = system or platform.system()
    spec = boot_service.gateway_names_spec(os_name, state_dir=state_dir)
    started, commands, why = boot_service.start_if_registered(
        spec, home=home, system=os_name, runner=command_runner,
    )
    if not started:
        return GatewayEnsureResult(
            state=GatewayState.START_FAILED,
            reason=why,
            argv=found.argv,
            unit_path=found.unit_path,
            secret_project=found.secret_project,
        )
    return GatewayEnsureResult(
        state=GatewayState.STARTED,
        reason=(
            "start requested for the registered model gateway "
            "(a start that finds one already serving exits 0 — the daemon's "
            "own pid/port guard, not a second one here)"
        ),
        argv=found.argv,
        unit_path=found.unit_path,
        secret_project=found.secret_project,
        commands=tuple(tuple(c) for c in commands),
    )


def _record_unrunnable(folder: Optional[Path], result: GatewayEnsureResult) -> None:
    """Write the ledger row for an unrunnable registration. Best-effort.

    Only into a folder that is already a managed project — a session-start
    hook must never create ``.claude/`` somewhere the user did not ask for.
    """
    if folder is None:
        return
    root = Path(folder)
    if not (root / ".claude").is_dir():
        return
    try:
        from vco_lib.deferral_emit import emit
        from vco_lib.deferral_report import DeferralEntry

        emit(root, DeferralEntry(
            condition_id=CID_GATEWAY_UNRUNNABLE,
            title="The model gateway is registered to start at login, but cannot run",
            detected=result.reason,
            why_deferred=(
                "Starting it again cannot help: the entry point baked into the "
                "registration is the thing that fails, and the init system is "
                "already retrying it. Re-rendering the registration is what "
                "fixes it, and that happens in an install/update run."
            ),
            command_to_apply=(
                "python install.py --update   # from the orchestrator root"
            ),
            severity="warning",
            disposition="action_required",
        ))
    except Exception:  # noqa: BLE001 — a report must never break a session start
        return


# ---------------------------------------------------------------------------
# CLI — the cross-language call surface (class A of the A>B>C rule)
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m vco_lib.gateway_ensure",
        description=(
            "Report, and optionally start, the model gateway's login-time "
            "registration (the ONE home; the SessionStart hook and the "
            "launcher call this instead of mirroring the logic)."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_text in (
        ("status", "Report the state. Starts nothing, writes nothing."),
        (
            "ensure",
            "Start a registered gateway that is not running. "
            "0=ok, 3=registered but unrunnable, 4=could not start.",
        ),
    ):
        s = sub.add_parser(name, help=help_text)
        s.add_argument(
            "--json", action="store_true",
            help="Print the result as JSON on stdout.",
        )
        s.add_argument(
            "--shell", action="store_true",
            help="Print `VCO_GATEWAY_*` assignments for a bash `eval` (the .sh "
                 "hook); the .ps1 hook uses --json + ConvertFrom-Json.",
        )
        s.add_argument(
            "--no-verify", action="store_true",
            help="Do not run the registered entry point with --version. "
                 "Faster, and blind to the one failure this exists to catch.",
        )
        if name == "ensure":
            s.add_argument(
                "--folder", default=None, metavar="DIR",
                help="Managed project whose deferral ledger records an "
                     "unrunnable registration. Omitted: nothing is written.",
            )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    import json
    import shlex

    args = _build_arg_parser().parse_args(argv)
    verify = not args.no_verify
    if args.cmd == "status":
        res = gateway_status(verify=verify)
    else:
        res = ensure_running(
            folder=Path(args.folder) if args.folder else None, verify=verify,
        )

    if args.json:
        print(json.dumps(res.to_dict(), sort_keys=True))
    elif args.shell:
        print(f"VCO_GATEWAY_STATE={shlex.quote(res.state.value)}")
        print(f"VCO_GATEWAY_PID={shlex.quote(str(res.pid) if res.pid else '')}")
        print(f"VCO_GATEWAY_REASON={shlex.quote(res.reason)}")
    else:
        print(f"{res.state.value}: {res.reason}")

    # Every non-success path is LOUD: the reason also goes to stderr so a
    # caller that only forwards stderr still says what happened.
    if res.exit_code != 0:
        print(f"[vct] gateway_ensure: {res.reason}", file=sys.stderr)
    return res.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
