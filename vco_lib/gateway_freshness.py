# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""ONE home for "is the running model gateway behind its checkout, and may we
restart it?".

v0.2.97. An orchestrator update rewrites the gateway's EDITABLE install
underneath a long-lived process: ``install.py --update`` re-renders the boot
registration (:mod:`vco_lib.gateway_boot_render`) and starts nothing, and
:mod:`vco_lib.gateway_ensure` starts only a gateway that is DOWN. Field proof:
a gateway up 14 h across the 0.2.96 update kept serving 0.2.95's catalog logic
until someone restarted it by hand.

Restarting it automatically is NOT the fix. The VS Code panel routes every
chat through the gateway, so a restart kills live agent sessions, and the
owner's rule is absolute: "session should not become unusable. full stop."
So this module only ever does two things:

* **check** — prove, or fail to prove, that the running gateway is behind the
  checkout. Read-only: one ``/health`` GET and, only when it is stale, one
  read of the init system's view of the unit.
* **restart** — on an explicit request (the launcher's Continue button, or a
  user typing the command ``install.py --update`` printed), restart it through
  the init system that owns it, then VERIFY that the new process serves the
  checkout's source. Refuses unless ``check`` says stale.

How staleness is PROVEN
-----------------------
The daemon reports ``/health.source_sha`` — a digest of the package files it
was started from, hashed once at import by ``model_router.source_identity``.
This module loads that same file FROM THE CHECKOUT by path and hashes the
checkout's package, so both sides run one function (A>B>C, rule A). Ordered
arms, in :func:`served_state`:

1. no ``/health`` answer → ``unknown``;
2. ``/health.service`` is not the gateway's name → ``unknown`` (not ours);
3. ``/health.version`` differs from the checkout's ``__version__`` → ``stale``
   (versions are bumped and gated every release, so this is a fact);
4. ``/health`` has no ``source_sha`` KEY → ``stale``: the checkout's server
   emits the key unconditionally, so its absence identifies older code — which
   is exactly the 0.2.96 gateway this release first meets;
5. no checkout digest, or a ``null`` served one → ``unknown``;
6. digests equal → ``current``, otherwise ``stale``.

``unknown`` never offers a restart. Process start time against update time
was considered and rejected: it needs per-OS process introspection, cannot
tell "restarted after the pull" from "restarted after the pull but before
``pip install -e`` finished", and says nothing about WHICH code the process
loaded. The digest is the fact the question is actually about.

Who may restart it
------------------
Only the init system that already owns the process. On Linux the unit's
``MainPID`` must BE the daemon's recorded pid (positive identity; a gateway
started from a terminal beside an enabled unit is not the unit's, and
``systemctl restart`` would only start a second one that exits 0 on finding
the first). macOS and Windows have no contract-stable way to read the job's
pid, so there the restart runs and the VERIFY step is what keeps the answer
honest. A gateway with no owning registration is never signalled: a pid read
from a file is not proof of which process it names (the stance
``model_gateway_stop`` in the launcher already takes). The launcher adds its
own mechanism for a child it started itself; that half lives in Rust because
only the process holding the child handle can stop it.

CLI
---
``python -m vco_lib.gateway_freshness check [--json] [--port N] [--install-root DIR]``
    Exit 0 = current / not running / unknown, 3 = stale.
``python -m vco_lib.gateway_freshness restart [--json] [--port N] [--install-root DIR]``
    Exit 0 = restarted (verified) or nothing needed, 3 = cannot restart from
    here, 4 = restart requested but not verified.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from vco_lib import boot_service, gateway_ensure

__all__ = [
    "CURRENT",
    "NOT_RUNNING",
    "STALE",
    "UNKNOWN",
    "CheckoutIdentity",
    "FreshnessReport",
    "FreshnessVerdict",
    "RestartOutcome",
    "RestartPlan",
    "check",
    "checkout_identity",
    "main",
    "report_after_update",
    "restart",
    "restart_command_line",
    "restart_plan",
    "restart_steps",
    "served_state",
    "update_notice",
]

#: Verdicts. ``CURRENT`` needs a matching digest; ``STALE`` needs positive
#: evidence of older code; everything else is ``UNKNOWN``. ``NOT_RUNNING`` is
#: its own word so "nothing to ask" is never read as "asked, and it is fine".
CURRENT = "current"
STALE = "stale"
UNKNOWN = "unknown"
NOT_RUNNING = "not_running"

#: Restart mechanisms :func:`restart_plan` can answer with. The launcher adds
#: ``launcher`` for a child it holds; that word never originates here.
MECH_BOOT_SERVICE = "boot_service"
MECH_NONE = "none"

#: Package location inside a clone. The daemon's own package directory is
#: what ``model_router.source_identity`` hashes on the served side.
GATEWAY_PACKAGE_REL = Path("claude_mcp_servers") / "model_router"
IDENTITY_MODULE = "source_identity.py"

#: The gateway binds loopback only (``model_router.config.DEFAULT_HOST``).
GATEWAY_HOST = "127.0.0.1"
HEALTH_TIMEOUT_S = 2.0

#: How long a restart waits for the old process to go (Windows' two-step
#: restart) and for the new one to serve the checkout's source.
STOP_WAIT_S = 15.0
VERIFY_WAIT_S = 30.0
POLL_INTERVAL_S = 0.5

CHECK_EXIT = {CURRENT: 0, NOT_RUNNING: 0, UNKNOWN: 0, STALE: 3}

#: Restart outcomes and their exit codes (1 and 2 are avoided for the reason
#: :mod:`vco_lib.hub_ensure` gives: an unhandled exception and argparse own them).
OUTCOME_RESTARTED = "restarted"
OUTCOME_NOT_NEEDED = "not_needed"
OUTCOME_UNSUPPORTED = "unsupported"
OUTCOME_UNVERIFIED = "unverified"
RESTART_EXIT = {
    OUTCOME_RESTARTED: 0,
    OUTCOME_NOT_NEEDED: 0,
    OUTCOME_UNSUPPORTED: 3,
    OUTCOME_UNVERIFIED: 4,
}

HealthProbe = Callable[[int], Optional[dict]]


# ---------------------------------------------------------------------------
# The checkout's identity — computed by the SAME file the daemon runs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckoutIdentity:
    """What the checkout would serve if the gateway were started now."""

    version: Optional[str] = None
    source_sha: Optional[str] = None


def gateway_package_dir(install_root) -> Path:
    return Path(install_root) / GATEWAY_PACKAGE_REL


def checkout_identity(install_root) -> CheckoutIdentity:
    """Version and source digest of ``install_root``'s gateway package.

    Loads ``model_router/source_identity.py`` from the checkout BY PATH rather
    than importing ``model_router``: the imported copy is whatever the running
    interpreter resolves, and the question is about the checkout. A checkout
    that predates the identity module (or any load failure) yields ``None``
    fields — could not look, never a fabricated digest.
    """
    if install_root is None:
        return CheckoutIdentity()
    package = gateway_package_dir(install_root)
    try:
        spec = importlib.util.spec_from_file_location(
            "_vco_gateway_source_identity_host", package / IDENTITY_MODULE,
        )
        if spec is None or spec.loader is None:
            return CheckoutIdentity()
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return CheckoutIdentity(
            version=module.package_version(package),
            source_sha=module.source_sha(package),
        )
    except Exception:  # noqa: BLE001 — could not look is not a verdict
        return CheckoutIdentity()


# ---------------------------------------------------------------------------
# The verdict — pure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FreshnessVerdict:
    """One verdict about the running gateway, with the evidence behind it."""

    verdict: str
    #: One line, safe to print verbatim.
    summary: str
    running_version: Optional[str] = None
    checkout_version: Optional[str] = None
    served_sha: Optional[str] = None
    expected_sha: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "summary": self.summary,
            "running_version": self.running_version,
            "checkout_version": self.checkout_version,
            "served_sha": self.served_sha,
            "expected_sha": self.expected_sha,
        }


def served_state(
    expected: CheckoutIdentity,
    health: Optional[dict],
    *,
    service_name: str,
) -> FreshnessVerdict:
    """Turn (checkout identity, ``/health`` payload) into one verdict.

    Pure, so every arm — including the ones that must NOT read as stale — is
    testable without a daemon. The arm order is the module docstring's list.
    """
    base = {
        "checkout_version": expected.version,
        "expected_sha": expected.source_sha,
    }
    if not health:
        return FreshnessVerdict(
            UNKNOWN,
            "model gateway: not answering /health — cannot tell which code it runs.",
            **base,
        )
    if health.get("service") != service_name:
        return FreshnessVerdict(
            UNKNOWN,
            "model gateway: the listener did not identify itself as "
            f"{service_name} — not ours to judge.",
            **base,
        )
    running_version = health.get("version")
    running_version = str(running_version) if running_version is not None else None
    base["running_version"] = running_version
    if (
        running_version is not None
        and expected.version is not None
        and running_version != expected.version
    ):
        return FreshnessVerdict(
            STALE,
            f"model gateway: running {running_version}, the checkout is "
            f"{expected.version}.",
            **base,
        )
    if "source_sha" not in health:
        return FreshnessVerdict(
            STALE,
            "model gateway: the running process predates this checkout's "
            "gateway code (its /health reports no source digest).",
            **base,
        )
    served = health.get("source_sha")
    if not expected.source_sha:
        return FreshnessVerdict(
            UNKNOWN,
            "model gateway: the checkout's gateway source could not be read — "
            "cannot compare.",
            served_sha=str(served) if served else None,
            **base,
        )
    if served is None:
        return FreshnessVerdict(
            UNKNOWN,
            "model gateway: the running process could not hash its own source "
            "— cannot compare.",
            **base,
        )
    if str(served) == expected.source_sha:
        return FreshnessVerdict(
            CURRENT,
            "model gateway: running the checkout's current source.",
            served_sha=str(served),
            **base,
        )
    return FreshnessVerdict(
        STALE,
        "model gateway: running OLDER source than the checkout (process "
        f"{str(served)[:12]}, checkout {expected.source_sha[:12]}).",
        served_sha=str(served),
        **base,
    )


# ---------------------------------------------------------------------------
# Reading the live daemon
# ---------------------------------------------------------------------------


def _gateway_config():
    """``model_router.config``, imported lazily.

    Not at module import: ``install.py`` imports this module on a path where a
    broken install must still finish. A failure here is REPORTED (the verdict
    names it), never replaced by a guessed port or service name.
    """
    from model_router import config  # noqa: PLC0415 — deliberate, see above

    return config


def probe_health(port: int, timeout: float = HEALTH_TIMEOUT_S) -> Optional[dict]:
    """``GET /health`` on the loopback port, as a dict; ``None`` if unreadable.

    Read-only and unauthenticated (``/health`` is the gateway's one open
    route). Never raises.
    """
    url = f"http://{GATEWAY_HOST}:{int(port)}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — loopback only
            if resp.status >= 400:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — every failure is "could not look"
        return None
    return payload if isinstance(payload, dict) else None


def find_gateway(
    ports: Sequence[int], service_name: str, probe: HealthProbe,
) -> tuple[Optional[int], Optional[dict]]:
    """The first candidate port whose ``/health`` identifies as the gateway.

    Returns ``(None, last payload seen)`` when none does, so a foreign
    listener on the best port still reaches :func:`served_state` as evidence
    ("not ours") rather than vanishing.
    """
    first_payload: Optional[dict] = None
    for port in ports:
        payload = probe(port)
        if payload and payload.get("service") == service_name:
            return port, payload
        if first_payload is None and payload:
            first_payload = payload
    return None, first_payload


# ---------------------------------------------------------------------------
# Who may restart it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestartPlan:
    """How the running gateway could be restarted from here, and why."""

    mechanism: str
    reason: str

    @property
    def possible(self) -> bool:
        return self.mechanism != MECH_NONE

    def to_dict(self) -> dict[str, Any]:
        return {"mechanism": self.mechanism, "possible": self.possible, "reason": self.reason}


def systemd_main_pid(unit_name: str) -> Optional[int]:
    """The pid systemd holds for ``unit_name``; ``None`` when there is none.

    ``systemctl --user show --property=MainPID --value`` is systemd's stable
    machine interface; it answers ``0`` for "no main process", which is not a
    pid. The launcher's status card reads the same property on its 5-second
    poll (``model_gateway.rs::boot_service_main_pid``) — a (C)-tier twin kept
    there because a per-poll interpreter start is the cost that rule allows.
    """
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return None
    try:
        proc = subprocess.run(
            [systemctl, "--user", "show", unit_name, "--property=MainPID", "--value"],
            check=False, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        pid = int((proc.stdout or b"").decode("utf-8", "replace").strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def restart_plan(
    pid: Optional[int],
    *,
    home: Optional[Path] = None,
    system: Optional[str] = None,
    state_dir: Optional[Path] = None,
    main_pid_reader: Callable[[str], Optional[int]] = systemd_main_pid,
) -> RestartPlan:
    """Decide whether the init system owns the running gateway.

    Every "no" names what the user can do instead, because the modal and the
    CLI line both print it verbatim.
    """
    hand = (
        "stop it where it was started (the terminal that runs it, or the "
        "launcher's gateway card if the launcher started it) and start it again"
    )
    if boot_service.boot_registration_disabled():
        return RestartPlan(
            MECH_NONE,
            f"{boot_service.DISABLE_ENV}=1 — VCO daemons are managed by hand "
            f"here; {hand}.",
        )
    os_name = system or platform.system()
    spec = boot_service.gateway_names_spec(os_name, state_dir=state_dir)
    if boot_service.status(spec, home=home, system=os_name) is (
        boot_service.BootStatus.NOT_INSTALLED
    ):
        return RestartPlan(
            MECH_NONE,
            "the gateway is not registered to start at login, so no service "
            f"manager owns it; {hand}.",
        )
    if not restart_steps(spec, system=os_name):
        return RestartPlan(
            MECH_NONE,
            f"no service-manager tool is available on {os_name}; {hand}.",
        )
    if os_name == "Linux":
        main_pid = main_pid_reader(spec.unit_name)
        if pid is None or main_pid is None or main_pid != pid:
            return RestartPlan(
                MECH_NONE,
                f"the running gateway (pid {pid}) is not the process of "
                f"{spec.unit_name} (systemd reports "
                f"{'no main process' if main_pid is None else f'pid {main_pid}'}); "
                f"{hand}.",
            )
        return RestartPlan(
            MECH_BOOT_SERVICE,
            f"restart {spec.unit_name} (systemd owns pid {pid})",
        )
    return RestartPlan(
        MECH_BOOT_SERVICE,
        "restart the login registration; whether the new process serves the "
        "updated source is checked afterwards",
    )


@dataclass(frozen=True)
class RestartSteps:
    """``stop`` runs first; when it is non-empty the old pid must exit before
    ``start`` runs (a start racing an exiting instance is ignored by a task
    that declares ``IgnoreNew``)."""

    stop: tuple[tuple[str, ...], ...] = ()
    start: tuple[tuple[str, ...], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.start)


def restart_steps(
    spec: boot_service.BootServiceSpec, *, system: Optional[str] = None,
) -> RestartSteps:
    """The init-system commands that restart ``spec``'s service.

    The sibling of :func:`vco_lib.boot_service.ensure_commands`, which STARTS
    and is written never to kill (``kickstart`` without ``-k``). This is the
    one place that deliberately does, so it lives with the gateway — the only
    daemon VCO ever restarts on a user's word — and is reached only through
    :func:`restart`, after a positive staleness verdict and an explicit request.
    """
    os_name = system or platform.system()
    if os_name == "Linux":
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return RestartSteps()
        return RestartSteps(start=(
            # A parked unit ignores `restart` exactly like it ignores `start`.
            (systemctl, "--user", "reset-failed", spec.unit_name),
            (systemctl, "--user", "restart", spec.unit_name),
        ))
    if os_name == "Darwin":
        launchctl = shutil.which("launchctl")
        if not launchctl:
            return RestartSteps()
        uid = os.getuid() if hasattr(os, "getuid") else 0
        return RestartSteps(start=(
            (launchctl, "kickstart", "-k", f"gui/{uid}/{spec.plist_label}"),
        ))
    if os_name == "Windows":
        schtasks = shutil.which("schtasks")
        if not schtasks:
            return RestartSteps()
        return RestartSteps(
            stop=((schtasks, "/End", "/TN", spec.task_name),),
            start=((schtasks, "/Run", "/TN", spec.task_name),),
        )
    return RestartSteps()


# ---------------------------------------------------------------------------
# check — the read-only question
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FreshnessReport:
    """Everything the launcher modal and the install line need to decide."""

    verdict: FreshnessVerdict
    pid: Optional[int] = None
    port: Optional[int] = None
    #: Only computed for a STALE verdict — nothing else needs it.
    plan: Optional[RestartPlan] = None
    #: Set when the check itself could not run properly (a broken install —
    #: ``model_router`` not importable). Distinct from an ``unknown`` verdict,
    #: which is a healthy check that could not decide; this one is LOUD.
    error: Optional[str] = None

    @property
    def stale(self) -> bool:
        return self.verdict.verdict == STALE

    def to_dict(self) -> dict[str, Any]:
        out = self.verdict.to_dict()
        out.update({
            "pid": self.pid,
            "port": self.port,
            # The ONE field the launcher branches on to show the modal. Stale
            # is the only verdict that prompts; unknown never does.
            "prompt": self.stale,
            "restart": self.plan.to_dict() if self.plan else None,
            "error": self.error,
        })
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FreshnessReport":
        """Rebuild a report from :meth:`to_dict` — the JSON a child printed."""
        plan_raw = payload.get("restart")
        plan = (
            RestartPlan(str(plan_raw.get("mechanism") or MECH_NONE), str(plan_raw.get("reason") or ""))
            if isinstance(plan_raw, dict) else None
        )
        return cls(
            FreshnessVerdict(
                str(payload.get("verdict") or UNKNOWN),
                str(payload.get("summary") or ""),
                running_version=payload.get("running_version"),
                checkout_version=payload.get("checkout_version"),
                served_sha=payload.get("served_sha"),
                expected_sha=payload.get("expected_sha"),
            ),
            pid=payload.get("pid"),
            port=payload.get("port"),
            plan=plan,
            error=payload.get("error"),
        )


def _resolve_install_root(install_root) -> Optional[Path]:
    from vco_lib.python_exe import resolve_install_root  # noqa: PLC0415

    return resolve_install_root(install_root)


def check(
    *,
    install_root=None,
    port: Optional[int] = None,
    state_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    system: Optional[str] = None,
    expected: Optional[CheckoutIdentity] = None,
    probe: HealthProbe = probe_health,
    main_pid_reader: Callable[[str], Optional[int]] = systemd_main_pid,
    running: Optional[bool] = None,
    pid: Optional[int] = None,
) -> FreshnessReport:
    """Is the running gateway behind the checkout? Starts nothing, writes nothing.

    ``expected`` / ``probe`` / ``main_pid_reader`` / ``running`` / ``pid`` are
    injection seams so a test describes a whole machine without a daemon.
    """
    if running is None:
        running = gateway_ensure.is_running(state_dir)
    if pid is None:
        pid = gateway_ensure.gateway_pid(state_dir)
    if not running:
        # The daemon's own pid file is the single-instance guard; no live pid
        # means nothing is serving old code, so there is nothing to offer.
        return FreshnessReport(
            FreshnessVerdict(NOT_RUNNING, "model gateway: not running."),
        )

    try:
        config = _gateway_config()
        service_name = config.SERVICE_NAME
        ports = [int(port)] if port else list(config.port_candidates())
    except Exception as exc:  # noqa: BLE001 — reported, never guessed around
        reason = (
            "model gateway: `model_router` cannot be imported by this "
            f"interpreter ({type(exc).__name__}: {exc}) — the install is "
            "broken; re-run `python install.py --update`."
        )
        return FreshnessReport(
            FreshnessVerdict(UNKNOWN, reason), pid=pid, error=reason,
        )

    if expected is None:
        expected = checkout_identity(_resolve_install_root(install_root))
    found_port, health = find_gateway(ports, service_name, probe)
    verdict = served_state(expected, health, service_name=service_name)
    plan = None
    if verdict.verdict == STALE:
        plan = restart_plan(
            pid, home=home, system=system, state_dir=state_dir,
            main_pid_reader=main_pid_reader,
        )
    return FreshnessReport(verdict, pid=pid, port=found_port, plan=plan)


# ---------------------------------------------------------------------------
# restart — only on an explicit request, only when proven stale
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestartOutcome:
    outcome: str
    message: str
    before: Optional[FreshnessReport] = None
    after: Optional[FreshnessVerdict] = None
    commands: tuple[tuple[str, ...], ...] = field(default_factory=tuple)

    @property
    def exit_code(self) -> int:
        return RESTART_EXIT[self.outcome]

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "restarted": self.outcome == OUTCOME_RESTARTED,
            "message": self.message,
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
            "commands": [list(c) for c in self.commands],
        }


def _wait_until(
    predicate: Callable[[], bool], timeout: float, *,
    sleep: Callable[[float], None], clock: Callable[[], float],
) -> bool:
    deadline = clock() + timeout
    while True:
        if predicate():
            return True
        if clock() >= deadline:
            return False
        sleep(POLL_INTERVAL_S)


def restart(
    *,
    install_root=None,
    port: Optional[int] = None,
    state_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    system: Optional[str] = None,
    expected: Optional[CheckoutIdentity] = None,
    probe: HealthProbe = probe_health,
    main_pid_reader: Callable[[str], Optional[int]] = systemd_main_pid,
    command_runner: Optional[Callable[[Sequence[str]], Optional[int]]] = None,
    pid_alive: Optional[Callable[[int], bool]] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    verify_timeout: float = VERIFY_WAIT_S,
    running: Optional[bool] = None,
    pid: Optional[int] = None,
) -> RestartOutcome:
    """Restart a gateway PROVEN stale through the init system that owns it.

    Re-checks first, so a Continue pressed on a modal whose gateway has since
    been restarted by hand (or stopped) does nothing. Every leave-alone arm
    runs no command at all.
    """
    os_name = system or platform.system()
    if expected is None:
        expected = checkout_identity(_resolve_install_root(install_root))
    before = check(
        install_root=install_root, port=port, state_dir=state_dir, home=home,
        system=os_name, expected=expected, probe=probe,
        main_pid_reader=main_pid_reader, running=running, pid=pid,
    )
    if not before.stale:
        return RestartOutcome(
            OUTCOME_NOT_NEEDED,
            f"Nothing restarted — {before.verdict.summary}",
            before=before,
        )
    plan = before.plan
    if plan is None or not plan.possible:
        return RestartOutcome(
            OUTCOME_UNSUPPORTED,
            "Not restarted from here: "
            + (plan.reason if plan else "no restart plan could be made."),
            before=before,
        )

    spec = boot_service.gateway_names_spec(os_name, state_dir=state_dir)
    steps = restart_steps(spec, system=os_name)
    run = command_runner or boot_service.run_quiet
    alive = pid_alive
    if alive is None:
        from vco_lib.deferral_probes import pid_is_alive as alive  # noqa: PLC0415
    ran: list[tuple[str, ...]] = []
    for cmd in steps.stop:
        run(cmd)
        ran.append(tuple(cmd))
    if steps.stop and before.pid is not None:
        old_pid = before.pid
        _wait_until(lambda: not alive(old_pid), STOP_WAIT_S, sleep=sleep, clock=clock)
    for cmd in steps.start:
        run(cmd)
        ran.append(tuple(cmd))

    try:
        service_name = _gateway_config().SERVICE_NAME
    except Exception:  # noqa: BLE001 — check() already proved it imports
        service_name = ""
    ports = [before.port] if before.port else [port] if port else []
    last: list[FreshnessVerdict] = []

    def _serves_checkout() -> bool:
        _, health = find_gateway(ports, service_name, probe)
        verdict = served_state(expected, health, service_name=service_name)
        last[:] = [verdict]
        return verdict.verdict == CURRENT

    if ports and _wait_until(_serves_checkout, verify_timeout, sleep=sleep, clock=clock):
        return RestartOutcome(
            OUTCOME_RESTARTED,
            "Model gateway restarted; it now serves the updated source.",
            before=before, after=last[0], commands=tuple(ran),
        )
    after = last[0] if last else None
    return RestartOutcome(
        OUTCOME_UNVERIFIED,
        "A restart was requested, but the gateway does not yet serve the "
        "updated source"
        + (f" ({after.summary})" if after else "")
        + ". If it was started outside its login registration, stop it where "
        "it was started and start it again.",
        before=before, after=after, commands=tuple(ran),
    )


# ---------------------------------------------------------------------------
# The CLI user's line — install.py --update prints this
# ---------------------------------------------------------------------------


def restart_command_line(
    install_root,
    *,
    platform_name: Optional[str] = None,
    fallback_python: Optional[str] = None,
) -> Optional[str]:
    """The exact command that restarts the gateway, for printing.

    A printed command is shipped code, so it must run as printed. It names the
    install root's own venv interpreter (the one ``pip install -e`` put
    ``vco_lib`` and ``model_router`` into) and changes into the install root
    first, so ``-m vco_lib`` resolves even for an interpreter that sees the
    checkout only through its working directory. Without a root venv it names
    the interpreter running THIS process — the one that just produced the
    stale verdict, so it demonstrably imports both packages. ``cd A && B`` is
    valid in bash, zsh, cmd.exe and PowerShell 7; paths are quoted for the
    platform's shell (``collection_rename.quote_for_shell``).
    """
    from vco_lib import install_companions  # noqa: PLC0415
    from vco_lib.collection_rename import quote_for_shell  # noqa: PLC0415

    if install_root is None:
        return None
    python = (
        install_companions.resolve_install_venv_python(install_root)
        or fallback_python
        or sys.executable
    )
    if not python:
        return None
    return (
        f"cd {quote_for_shell(install_root, platform=platform_name)} && "
        f"{quote_for_shell(python, platform=platform_name)} "
        "-m vco_lib.gateway_freshness restart"
    )


def update_notice(report: FreshnessReport, install_root) -> Optional[str]:
    """One line for ``install.py --update`` when the gateway is proven stale.

    ``None`` for every other verdict — an install log that cries wolf on an
    unknown is how the one true warning gets skimmed.
    """
    if not report.stale:
        return None
    head = (
        "[vct] The model gateway"
        + (f" (pid {report.pid})" if report.pid else "")
        + " is still running the previous release's code; it was NOT "
        "restarted, because restarting it ends any agent session routed "
        "through it."
    )
    plan = report.plan
    if plan is not None and plan.possible:
        command = restart_command_line(install_root)
        if command:
            return f"{head} When no agent is running, restart it: {command}"
    reason = plan.reason if plan else "no restart plan could be made."
    return f"{head} It cannot be restarted from here: {reason}"


#: The install-log phase the notice is recorded under — the same phase the
#: registration re-render next to it uses (``vco_lib.gateway_boot_render``).
_LOG_PHASE = "boot-service"


#: Budget for the venv child: one interpreter start, one `/health` GET and,
#: when stale, one `systemctl show`.
CHILD_TIMEOUT_S = 60

ChildRunner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]


def _run_child(cmd: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(cmd), cwd=str(cwd), stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=CHILD_TIMEOUT_S,
    )


def check_in_venv(
    install_root, *, runner: Optional[ChildRunner] = None,
) -> tuple[Optional[FreshnessReport], Optional[str]]:
    """``check`` run by the INSTALL's venv interpreter, never in-process.

    ``(report, None)`` on an answer, ``(None, why)`` when the child could not
    give one. Why a child: ``check`` imports ``model_router`` (the editable
    ``claude_mcp_servers`` package), which only the venv provides, while
    ``install.py``'s own interpreter can be whatever launched it — the shape
    that silently broke a sibling install leg for three releases
    (``machine_migrations._run_pin_child``, the pattern followed here). ``cwd``
    is the checkout so ``-m`` resolves ITS ``vco_lib``.
    """
    from vco_lib import install_companions  # noqa: PLC0415
    from vco_lib.child_process import last_json_object  # noqa: PLC0415

    root = Path(install_root)
    venv_python = install_companions.resolve_install_venv_python(root)
    if venv_python is None:
        return None, (
            f"no venv interpreter under {root} (looked for .venv and "
            "claude_mcp_servers/.venv)"
        )
    cmd = [
        str(venv_python), "-m", "vco_lib.gateway_freshness", "check", "--json",
        "--install-root", str(root),
    ]
    try:
        proc = (runner or _run_child)(cmd, root)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"`{' '.join(cmd)}` did not run: {exc}"
    payload = last_json_object(proc.stdout)
    if payload is None:
        tail = " | ".join((proc.stderr or "").strip().splitlines()[-3:])
        return None, (
            f"`{' '.join(cmd)}` exited {proc.returncode} without a result"
            + (f": {tail}" if tail else "")
        )
    return FreshnessReport.from_dict(payload), None


def report_after_update(
    *,
    update: bool,
    install_root,
    log: Optional[Callable[[str, str, str], Any]] = None,
    out: Optional[Callable[[str], Any]] = None,
    runner: Optional[ChildRunner] = None,
) -> Optional[str]:
    """``install.py --update``'s tail: print ONE line if the gateway is stale.

    Restarts nothing. Returns the line printed, or ``None``. Silent for a
    current, unknown or stopped gateway. LOUD — printed, not only logged — when
    the check itself could not run (no venv, a child that died, ``model_router``
    not importable in the venv): that is a broken install, and a warning only
    the log file carries is how the last one went unseen. Never raises: a probe
    must not fail an update that otherwise succeeded. ``update=False`` (a plain
    install) is a no-op, gated here so a second caller cannot forget it.
    """
    if not update:
        return None
    try:
        report, problem = check_in_venv(install_root, runner=runner)
        if report is not None and report.error:
            problem = report.error
        if problem:
            line: Optional[str] = (
                "[vct] Could not check whether the model gateway runs the "
                f"updated code: {problem}"
            )
        else:
            line = update_notice(report, install_root) if report else None
    except Exception as exc:  # noqa: BLE001 — soft-fail by design, but visible
        line = (
            "[vct] Could not check whether the model gateway runs the updated "
            f"code: {type(exc).__name__}: {exc}"
        )
    if line:
        (out or print)(line)
        if log is not None:
            log(_LOG_PHASE, "warn", line)
    return line


# ---------------------------------------------------------------------------
# CLI — the cross-language call surface (class A of the A>B>C rule)
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m vco_lib.gateway_freshness",
        description=(
            "Is the running model gateway behind its checkout (check), and "
            "restart it through its owning service manager (restart). Never "
            "restarts anything unless asked, and only when proven stale."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_text in (
        ("check", "Report. Starts nothing. Exit 0 current/unknown/not running, 3 stale."),
        ("restart", "Restart a PROVEN-stale gateway. Exit 0 done/not needed, "
                    "3 cannot restart from here, 4 requested but unverified."),
    ):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--json", action="store_true", help="Print one JSON object on stdout.")
        s.add_argument("--port", type=int, default=None,
                       help="Probe this port only (default: the gateway's port evidence chain).")
        s.add_argument("--install-root", default=None, metavar="DIR",
                       help="Checkout to compare against (default: this vco_lib's clone).")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.cmd == "check":
        report = check(install_root=args.install_root, port=args.port)
        if args.json:
            print(json.dumps(report.to_dict(), sort_keys=True))
        else:
            print(f"{report.verdict.verdict}: {report.verdict.summary}")
            if report.plan is not None:
                print(f"restart: {report.plan.reason}")
        return CHECK_EXIT[report.verdict.verdict]

    outcome = restart(install_root=args.install_root, port=args.port)
    if args.json:
        print(json.dumps(outcome.to_dict(), sort_keys=True))
    else:
        print(f"{outcome.outcome}: {outcome.message}")
    if outcome.exit_code != 0:
        print(f"[vct] gateway_freshness: {outcome.message}", file=sys.stderr)
    return outcome.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
