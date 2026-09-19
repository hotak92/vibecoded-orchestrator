# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The one home for VCO's boot-time (login-time) service registration.

Before v0.2.92 this concern had TWO Python homes: the three renderers
``_materialize_boot_service_{linux,macos,windows}`` inside ``install.py`` did
the REGISTER half for the container stack, and a since-deleted
``vco_lib/boot_service_cleanup.py`` did the UNREGISTER half. A register that
knows where a unit goes and an unregister that has to know the same thing, in
two files, is one drift away from an uninstall that misses what an install
wrote. This module holds both halves plus ``status``; ``install.py``'s three
renderer names remain as thin calls into :func:`register_linux` /
:func:`register_macos` / :func:`register_windows` (two test files monkeypatch
them there), and ``install.py --uninstall`` calls :func:`unregister` with
:func:`container_stack_unregister_spec` directly. The cleanup shim was
deleted by the v0.2.92 duplication-merge lane once its only two importers
(the uninstaller and ``tests/test_uninstall_boot_service.py``) were repointed.

Scope: the artefact the host init system reads (systemd user unit / launchd
LaunchAgent / Windows Scheduled Task), its enable/disable/start calls, and —
for a daemon that keeps runtime state files — removal of what it leaves
behind (:func:`remove_gateway_state`). It does NOT own the SERVICES; each
service supplies a :class:`BootServiceSpec` describing its own identity.

Two services use it today:

* **container stack** — registered on every default install
  (``install.py``'s Step 7b), unit ``claude-mcp-containers.service``.
* **model gateway** — DEFAULT OFF, registered only when the user asks
  (``vct-model-gateway --register-boot`` or the launcher toggle). A daemon
  holding an OAuth passthrough is a security-surface change, so it is a
  conscious opt-in rather than something an install decides for the user.

RELATIONSHIP TO ``vct-hub``'s ``boot.rs`` — a DECLARED CLASS-C MIRROR.
``launcher/src-tauri/vct-hub/src/boot.rs`` implements the same three-OS
pattern in Rust for the hub binary. It is NOT ported here and this is not
ported there: the hub registers a compiled binary from inside its own
process (``current_exe()``), while this module registers services described
by an installer/CLI that is not the service. What IS pinned in lockstep is
the CLI CONTRACT and the ARTIFACT SHAPES, because a user and the launcher
GUI read both: the same ``--register-boot`` / ``--unregister-boot`` /
``--boot-status`` verb set, the same ``enabled`` / ``disabled`` /
``not-installed`` / ``error: …`` stdout words, the same exit codes
(0/1/2/3), and the same per-OS mechanism table. MUST MATCH ``boot.rs``;
``tests/test_boot_service_shared.py`` reads ``boot.rs`` and asserts the
contract has not drifted on either side.

Everything is SOFT-FAIL by design and by inheritance from ``install.py``'s
Bug L2 contract: a missing ``systemctl`` / ``launchctl`` / ``schtasks``, an
unwritable unit directory, an absent template — each is logged and returns,
never raises to the caller and never blocks an install.
"""

from __future__ import annotations

import html
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from vco_lib.paths import user_home, vct_root_dir
from vco_lib.timeutil import utc_iso_now

# ---------------------------------------------------------------------------
# Service identities
# ---------------------------------------------------------------------------

#: Container stack (weaviate / ollama / code-embed). Registered by install.py.
CONTAINER_STACK_UNIT_NAME = "claude-mcp-containers.service"
CONTAINER_STACK_PLIST_LABEL = "com.vibecodedtools.claude-mcp-containers"
CONTAINER_STACK_TASK_NAME = "ClaudeMcpContainers"

#: Model gateway (``vct-model-gateway``). Registered only on explicit request.
MODEL_GATEWAY_UNIT_NAME = "vct-model-gateway.service"
MODEL_GATEWAY_PLIST_LABEL = "com.vibecodedtools.vct-model-gateway"
MODEL_GATEWAY_TASK_NAME = "VCT-ModelGateway"

#: Env kill-switch. Honoured by BOTH services — a flag that gated only the
#: container stack would be a promise the gateway did not keep.
DISABLE_ENV = "VCT_DISABLE_BOOT_SERVICE"


class BootStatus(Enum):
    """Tri-state report. Mirrors ``boot.rs::BootStatus`` value-for-value."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    NOT_INSTALLED = "not-installed"


#: ``--boot-status`` exit codes. MUST MATCH ``boot.rs::run_boot_status``.
BOOT_STATUS_EXIT_CODES = {
    BootStatus.ENABLED: 0,
    BootStatus.DISABLED: 1,
    BootStatus.NOT_INSTALLED: 2,
}
BOOT_STATUS_ERROR_EXIT = 3


@dataclass(frozen=True)
class BootServiceSpec:
    """Everything the renderers need to register ONE service on any OS.

    ``substitutions`` carries the service-specific ``{{KEY}}`` values; the
    renderers add the ones only they can know (``INSTALLED_AT_PATH``,
    ``LOG_FILE``, ``LABEL``, ``CREATED_AT``, ``USER_ID``). A service value
    wins over a derived one only if the service sets that exact key, which
    no shipped spec does — the split is by ownership, not precedence.
    """

    service_id: str
    unit_name: str
    plist_label: str
    task_name: str
    template_linux: str
    template_macos: str
    template_windows: str
    log_basename: str
    #: Where the rendered Task Scheduler XML is materialised. Required to
    #: REGISTER on Windows. ``unregister`` deletes it only when it is set:
    #: the container stack's copy lives inside the clone the user is
    #: deleting anyway, while the gateway's lives under the state root and
    #: would otherwise be orphaned.
    windows_task_xml_path: Optional[Path] = None
    substitutions: Mapping[str, str] = field(default_factory=dict)
    #: Explicit log path. When None the per-OS default under the user's home
    #: is used (the container stack's historical behaviour).
    log_file: Optional[Path] = None
    #: systemd ``enable --now`` / launchctl ``kickstart`` / ``schtasks /Run``
    #: — i.e. "the user sees it running on the same invocation". True for
    #: user-invoked registration (mirrors ``vct-hub --register-boot``), False
    #: for install.py's container stack, which the install itself has already
    #: brought up.
    enable_now: bool = False
    #: ``loginctl enable-linger`` so a user unit fires without a login
    #: session. Linux only; ignored elsewhere.
    linger: bool = True


#: Called as ``on_event(phase, detail, data)``. ``install.py`` binds it to
#: ``_log_install_event("boot-service", …)``; the gateway CLI binds it to a
#: stderr printer. None means "log nothing" (used by tests).
EventSink = Callable[[str, str, Optional[dict]], None]


def _noop_event(phase: str, detail: str, data: Optional[dict] = None) -> None:
    return None


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------


def boot_registration_disabled() -> bool:
    """True when ``VCT_DISABLE_BOOT_SERVICE=1`` forbids registering anything.

    One reader for both services. CI and minimal installs set it; so does a
    user who wants VCO's daemons started only by hand.
    """
    return os.environ.get(DISABLE_ENV, "").strip() == "1"


def read_template(templates_root: Path, relpath: str) -> Optional[str]:
    """Read a unit template. None when absent (minimal install, no clone).

    ``relpath`` is relative to the ORCHESTRATOR ROOT, not to
    ``templates_root``'s parent, because that is the shape install.py's
    call sites have always passed (``"templates/systemd/…"``) and a test
    monkeypatches that reader by relpath.
    """
    try:
        return (Path(templates_root) / relpath).read_text(encoding="utf-8")
    except OSError:
        return None


def render_template(template_text: str, substitutions: Mapping[str, object]) -> str:
    """Naive ``{{KEY}}`` substitution.

    Deliberately not jinja2 / ``string.Template``: the substitution set is
    closed, and install.py's early steps stay stdlib-only.
    """
    rendered = template_text
    for key, value in substitutions.items():
        rendered = rendered.replace("{{" + key + "}}", str(value))
    return rendered


def backup_and_write_idempotent(
    target: Path, rendered: str,
) -> tuple[bool, Optional[Path]]:
    """Write ``rendered`` to ``target`` only when the content differs.

    Backs the prior file up to ``<target>.bak-<compact ISO8601>`` first.
    Re-running an install/update with unchanged template + substitutions is
    a no-op: zero writes, zero backups, unchanged mtime.

    Returns ``(changed, backup_path_or_None)``.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError:
            existing = None
        if existing == rendered:
            return False, None
        stamp = utc_iso_now().replace(":", "").replace("-", "")
        backup = target.with_name(target.name + f".bak-{stamp}")
        try:
            backup.write_text(existing or "", encoding="utf-8")
        except OSError:
            backup = None
    else:
        backup = None
    target.write_text(rendered, encoding="utf-8")
    return True, backup


def run_quiet(cmd: Sequence[str], timeout: int = 15) -> Optional[int]:
    """Run a command, swallowing output. Exit code, or None if unspawnable."""
    try:
        proc = subprocess.run(
            list(cmd), check=False, capture_output=True, timeout=timeout,
        )
        return proc.returncode
    except (OSError, subprocess.TimeoutExpired):
        return None


def windows_user_id() -> str:
    """``DOMAIN\\user`` for the Task Scheduler logon trigger, UNESCAPED.

    Escaping happens once, centrally, in :func:`register_windows` — every
    value substituted into the Task XML goes through the same escape, so a
    caller cannot forget one and no value can be escaped twice. Before
    v0.2.92 this function escaped its own result and was the ONLY escaped
    value, which is how ``WORKING_DIR`` / ``WRAPPER_SCRIPT`` stayed raw: an
    install path containing ``&`` produced XML that ``schtasks /Create /XML``
    rejects outright, the same class of defect v0.2.53 W-P1-5 fixed for
    ``USERDOMAIN`` alone.
    """
    raw_domain = os.environ.get("USERDOMAIN", "")
    raw_username = os.environ.get("USERNAME", "")
    user_id = (raw_domain + ("\\" if raw_domain else "") + raw_username).strip("\\")
    if user_id:
        return user_id
    # POSIX-style fallback (CI / WSL / Git Bash).
    return os.environ.get("USER", "user")


def xml_escape_content(value: object) -> str:
    """Escape ``&``, ``<`` and ``>`` for XML ELEMENT CONTENT.

    Quotes are deliberately left alone: every substituted value in the
    shipped Task XML templates lands in element content, never in an
    attribute, and escaping ``"`` there would corrupt the ``&quot;``-quoted
    inner command line the templates already build by hand.

    A DELIBERATE DIVERGENCE from ``boot.rs::xml_escape``, which escapes all
    five characters — its values reach a different template with different
    quoting. Named here so nobody "fixes" the asymmetry by copying the Rust
    version across; it is not a MUST-MATCH pair and no parity test claims
    otherwise.
    """
    return html.escape(str(value), quote=False)


def default_templates_root() -> Path:
    """The orchestrator clone whose ``templates/`` this install should read.

    Ladder (delegated to :func:`vco_lib.python_exe.resolve_install_root`):
    ``$VCT_INSTALL_ROOT``, then ``$VCT_ORCHESTRATOR_ROOT``, each only when it
    really names an orchestrator clone (a stale exported value from a moved
    clone must not win), then ``vco_lib/..``. The second rung is exact
    rather than best-effort: ``install.py`` installs the root distribution
    with ``pip install -e .``, so an importable ``vco_lib`` lives INSIDE the
    clone. If it does not, the install is broken in the way
    ``probe_vco_lib_editable`` reports, and no path this function could
    invent would fix it.

    Used only by callers that are not themselves inside the clone — the
    gateway daemon. ``install.py`` passes its own ``PROJECT_ROOT``, which is
    more specific and stays authoritative there.
    """
    # v0.2.94: ONE home for "which clone am I in" — `vco_lib.python_exe.
    # resolve_install_root` (explicit → $VCT_INSTALL_ROOT → $VCT_ORCHESTRATOR_ROOT,
    # each only when it really names a clone → vco_lib/..). Precedence is a
    # deliberate choice: VCT_INSTALL_ROOT is the variable every VCO-written
    # environment carries; VCT_ORCHESTRATOR_ROOT is the older spelling, and
    # both name the same clone on every install VCO produced. The final rung
    # keeps the exactness argued above: vco_lib/.. IS the clone or the
    # install is broken.
    from vco_lib.python_exe import resolve_install_root

    return resolve_install_root() or Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Per-OS artefact locations
# ---------------------------------------------------------------------------


def systemd_unit_path(spec: BootServiceSpec, home: Optional[Path] = None) -> Path:
    return (home or user_home()) / ".config" / "systemd" / "user" / spec.unit_name


def launchd_plist_path(spec: BootServiceSpec, home: Optional[Path] = None) -> Path:
    return (
        (home or user_home()) / "Library" / "LaunchAgents"
        / f"{spec.plist_label}.plist"
    )


def linux_log_file(spec: BootServiceSpec, home: Optional[Path] = None) -> Path:
    if spec.log_file is not None:
        return spec.log_file
    return (home or user_home()) / ".local" / "state" / "vct" / spec.log_basename


def macos_log_file(spec: BootServiceSpec, home: Optional[Path] = None) -> Path:
    if spec.log_file is not None:
        return spec.log_file
    return (home or user_home()) / "Library" / "Logs" / spec.log_basename


def windows_log_file(spec: BootServiceSpec) -> Path:
    """Windows log target.

    v0.2.92 (R23, pre-existing defect): the Windows renderer substituted
    ``LABEL`` / ``WORKING_DIR`` / ``WRAPPER_SCRIPT`` / ``CREATED_AT`` /
    ``USER_ID`` but NOT ``LOG_FILE``, while the shipped Task XML template
    sets ``VCT_STACK_LOG_FILE={{LOG_FILE}}``. Every Windows machine
    registered since v0.2.14 therefore ran its logon task with the literal
    two-brace string as a log path, and the PS1 wrapper — which documents
    that variable as its log target — wrote to a file named after the
    placeholder. Substituting it is the fix; there is no Windows equivalent
    of ``~/.local/state`` or ``~/Library/Logs``, so the log lands under the
    documented state root, which is where the launcher already writes its
    own logs.
    """
    if spec.log_file is not None:
        return spec.log_file
    return vct_root_dir() / "logs" / spec.log_basename


def boot_log_file(log_file: Path) -> Path:
    """Where the INIT SYSTEM captures the process's own stdout/stderr.

    Deliberately a sibling of the service's own log rather than the same
    file. A daemon that configures a file handler on ``log_file`` and also
    logs to stderr would have every record written twice if the unit
    redirected stderr into the same path; that duplication reads as a bug in
    the daemon and is unfixable from the unit side.

    Two paths must BOTH hold for this file to stay small, and only one of
    them lives here. Separate paths (this function) stop the exact-duplicate
    case; what they do not stop is the daemon writing INFO to stderr, which
    the init system appends HERE — so an access line landed in both files
    and this one grew for the life of a healthy daemon. The other half of
    the rule therefore lives in the daemon:
    ``model_router.__main__._configure_logging`` raises stderr to WARNING
    once its file handler opens.

    With both in force the split is: ``<name>.log`` holds the daemon's
    records in full, and ``<name>.boot.log`` holds what the daemon could not
    record (a refusal to start, an import error, a crash) PLUS warnings and
    errors, which appear in both by design — a warning is exactly what
    someone reading a boot log is looking for.
    """
    return log_file.with_name(f"{log_file.stem}.boot{log_file.suffix}")


# ---------------------------------------------------------------------------
# REGISTER
# ---------------------------------------------------------------------------


def register_linux(
    spec: BootServiceSpec,
    template_text: Optional[str],
    *,
    on_event: EventSink = _noop_event,
    home: Optional[Path] = None,
) -> bool:
    """Render the systemd user unit and enable it. Returns True when enabled.

    Soft-fail: a missing ``systemctl`` still leaves a correct unit file on
    disk (the user can enable it later); a missing ``loginctl`` means the
    unit fires at login rather than at boot, which is stated rather than
    silently accepted.
    """
    if template_text is None:
        on_event("skip", f"{spec.template_linux} missing", None)
        return False

    unit_path = systemd_unit_path(spec, home)
    log_file = linux_log_file(spec, home)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    rendered = render_template(template_text, {
        "INSTALLED_AT_PATH": str(unit_path),
        "LOG_FILE": str(log_file),
        "BOOT_LOG_FILE": str(boot_log_file(log_file)),
        **spec.substitutions,
    })
    try:
        changed, backup = backup_and_write_idempotent(unit_path, rendered)
    except OSError as exc:
        on_event("warn", f"could not write systemd unit: {exc}", None)
        return False

    on_event(
        "ok" if changed else "skip",
        "systemd unit written" if changed else "systemd unit unchanged",
        {"unit_path": str(unit_path), "backup": str(backup) if backup else None},
    )

    systemctl = shutil.which("systemctl")
    if not systemctl:
        on_event(
            "skip", "systemctl not on PATH — skipping daemon-reload / enable", None,
        )
        return False
    enable_argv = [systemctl, "--user", "enable"]
    if spec.enable_now:
        enable_argv.append("--now")
    enable_argv.append(spec.unit_name)
    for cmd in ([systemctl, "--user", "daemon-reload"], enable_argv):
        try:
            subprocess.run(cmd, check=False, capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            on_event("warn", f"systemctl invocation failed: {' '.join(cmd)} → {exc}", None)

    if spec.linger:
        _enable_linger(on_event)
    return True


def _enable_linger(on_event: EventSink) -> None:
    """``loginctl enable-linger`` so the user unit fires at boot, not only at
    login. Idempotent: the current state is probed first."""
    loginctl = shutil.which("loginctl")
    if not loginctl:
        on_event(
            "skip", "loginctl not on PATH — user-unit will only fire at login", None,
        )
        return
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not user:
        return
    try:
        probe = subprocess.run(
            [loginctl, "show-user", user, "--property=Linger"],
            check=False, capture_output=True, text=True, timeout=5,
        )
        already_lingering = "Linger=yes" in probe.stdout
    except (OSError, subprocess.TimeoutExpired):
        already_lingering = False
    if already_lingering:
        return
    try:
        subprocess.run(
            [loginctl, "enable-linger", user],
            check=False, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        on_event("warn", f"loginctl enable-linger failed: {exc}", None)


def register_macos(
    spec: BootServiceSpec,
    template_text: Optional[str],
    *,
    on_event: EventSink = _noop_event,
    home: Optional[Path] = None,
) -> bool:
    """Render the LaunchAgent plist and bootstrap it.

    ``launchctl bootstrap gui/<uid>`` is the modern syntax; ``load -w`` is
    the legacy fallback, and "already bootstrapped" (``Bootstrap failed: 17:
    File exists``) is tolerated so re-registration is idempotent. The log
    directory is created BEFORE bootstrap because launchd refuses to start a
    job whose ``StandardOutPath`` directory does not exist.
    """
    if template_text is None:
        on_event("skip", "launchd template missing", None)
        return False

    plist_path = launchd_plist_path(spec, home)
    log_file = macos_log_file(spec, home)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    rendered = render_template(template_text, {
        "INSTALLED_AT_PATH": str(plist_path),
        "LABEL": spec.plist_label,
        "LOG_FILE": str(log_file),
        "BOOT_LOG_FILE": str(boot_log_file(log_file)),
        **spec.substitutions,
    })
    try:
        changed, backup = backup_and_write_idempotent(plist_path, rendered)
    except OSError as exc:
        on_event("warn", f"could not write launchd plist: {exc}", None)
        return False

    on_event(
        "ok" if changed else "skip",
        "launchd plist written" if changed else "launchd plist unchanged",
        {"plist_path": str(plist_path), "backup": str(backup) if backup else None},
    )

    launchctl = shutil.which("launchctl")
    if not launchctl:
        on_event("skip", "launchctl not on PATH — skipping load", None)
        return False

    uid = os.getuid() if hasattr(os, "getuid") else 0
    target = f"gui/{uid}"
    bootstrap_rc = -1
    try:
        proc = subprocess.run(
            [launchctl, "bootstrap", target, str(plist_path)],
            check=False, capture_output=True, text=True, timeout=10,
        )
        bootstrap_rc = proc.returncode
    except (OSError, subprocess.TimeoutExpired):
        bootstrap_rc = -1
    if bootstrap_rc != 0:
        try:
            subprocess.run(
                [launchctl, "load", "-w", str(plist_path)],
                check=False, capture_output=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            on_event("warn", f"launchctl load fallback failed: {exc}", None)
    if spec.enable_now:
        try:
            subprocess.run(
                [launchctl, "kickstart", "-k", f"{target}/{spec.plist_label}"],
                check=False, capture_output=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            on_event("warn", f"launchctl kickstart failed: {exc}", None)
    return True


def register_windows(
    spec: BootServiceSpec,
    template_text: Optional[str],
    *,
    on_event: EventSink = _noop_event,
) -> bool:
    """Render the Task Scheduler XML and import it via ``schtasks /Create``.

    The rendered XML is materialised at a stable path first so re-runs are a
    file compare and an operator can inspect exactly what was registered.
    """
    if template_text is None:
        on_event("skip", "Windows Task Scheduler XML template missing", None)
        return False

    task_xml_path = spec.windows_task_xml_path
    if task_xml_path is None:
        on_event(
            "warn",
            f"{spec.service_id}: no Task Scheduler XML path in the spec — "
            "nothing to import",
            None,
        )
        return False
    log_file = windows_log_file(spec)
    # cmd.exe's `>>` creates the FILE but not its directory, so a task
    # pointed at a missing logs/ dir would fail with the reason it could
    # not report. Create it here, soft-fail like every other write.
    try:
        boot_log_file(log_file).parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    # ONE escape point: every value that reaches the Task XML is escaped
    # here, exactly once, so no spec builder can forget one and none can
    # double-escape.
    rendered = render_template(template_text, {
        key: xml_escape_content(value)
        for key, value in {
            "LABEL": spec.task_name,
            "CREATED_AT": utc_iso_now(),
            "USER_ID": windows_user_id(),
            "LOG_FILE": _windows_forward(log_file),
            "BOOT_LOG_FILE": _windows_forward(boot_log_file(log_file)),
            **spec.substitutions,
        }.items()
    })
    try:
        changed, backup = backup_and_write_idempotent(task_xml_path, rendered)
    except OSError as exc:
        on_event("warn", f"could not write Task Scheduler XML: {exc}", None)
        return False

    on_event(
        "ok" if changed else "skip",
        "Task XML written" if changed else "Task XML unchanged",
        {"task_xml_path": str(task_xml_path),
         "backup": str(backup) if backup else None},
    )

    schtasks = shutil.which("schtasks")
    if not schtasks:
        on_event("skip", "schtasks not on PATH — Task Scheduler import skipped", None)
        return False
    try:
        subprocess.run(
            [schtasks, "/Create", "/TN", spec.task_name,
             "/XML", str(task_xml_path), "/F"],
            check=False, capture_output=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        on_event("warn", f"schtasks /Create failed: {exc}", None)
        return False
    if spec.enable_now:
        try:
            subprocess.run(
                [schtasks, "/Run", "/TN", spec.task_name, "/I"],
                check=False, capture_output=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            on_event("warn", f"schtasks /Run failed: {exc}", None)
    return True


def _windows_forward(path: Path) -> str:
    """Forward-slash form. PowerShell accepts both separators; forward
    slashes avoid XML/`cmd.exe` quoting surprises inside the task XML."""
    return str(path).replace("\\", "/")


def register(
    spec: BootServiceSpec,
    *,
    templates_root: Path,
    on_event: EventSink = _noop_event,
    system: Optional[str] = None,
    home: Optional[Path] = None,
) -> bool:
    """Register ``spec`` for the host OS. Returns True when a unit was written.

    Honours :func:`boot_registration_disabled`. Callers that need a
    different skip message (install.py) may check it themselves first; the
    gate is repeated here so no caller can forget it.
    """
    if boot_registration_disabled():
        on_event("skip", f"{DISABLE_ENV}=1 — skipping", None)
        return False
    os_name = system or platform.system()
    if os_name == "Linux":
        return register_linux(
            spec, read_template(templates_root, spec.template_linux),
            on_event=on_event, home=home,
        )
    if os_name == "Darwin":
        return register_macos(
            spec, read_template(templates_root, spec.template_macos),
            on_event=on_event, home=home,
        )
    if os_name == "Windows":
        return register_windows(
            spec, read_template(templates_root, spec.template_windows),
            on_event=on_event,
        )
    on_event("skip", f"unsupported OS for boot-service registration: {os_name}", None)
    return False


# ---------------------------------------------------------------------------
# UNREGISTER
# ---------------------------------------------------------------------------


def rerender_if_registered(
    spec: BootServiceSpec,
    *,
    templates_root: Path,
    on_event: EventSink = _noop_event,
    system: Optional[str] = None,
    home: Optional[Path] = None,
) -> bool:
    """Re-render an EXISTING registration; create nothing. Returns True when
    a re-render ran.

    This is how an opt-in service survives a clone that moved. Its unit
    embeds absolute paths resolved at registration time, so after the clone
    moves the unit points at an ``ExecStart`` that no longer exists and the
    service silently stops coming up at login — the same failure mode
    ``_repair_systemd_unit_working_dir`` was written for on the container
    side. ``install.py --update`` calls this so the paths are refreshed.

    It must NOT create a first registration: the gateway is opt-in, and an
    update that quietly turned a login-time OAuth-bearing daemon on would be
    the security decision this package deliberately left to the user. The
    ARTEFACT's presence is the consent record, which is why the gate is
    "not :attr:`BootStatus.NOT_INSTALLED`" rather than a config flag.
    """
    if boot_registration_disabled():
        return False
    current = status(spec, home=home, system=system)
    if current is BootStatus.NOT_INSTALLED:
        return False
    register(
        spec, templates_root=templates_root, on_event=on_event,
        system=system, home=home,
    )
    return True


def unregister(
    spec: BootServiceSpec,
    *,
    home: Optional[Path] = None,
    system: Optional[str] = None,
    runner: Optional[Callable[..., Optional[int]]] = None,
) -> list[str]:
    """Remove the boot autostart entry for the host OS. Returns audit lines.

    Idempotent and never raises: unregistering something that was never
    registered says so and removes nothing. The unit/plist DELETE is the
    part that matters — an entry the user's home keeps pointing into a
    deleted clone retries at every logon, forever, which is the gap this
    module's cleanup half was written for.

    ``runner`` exists so ``install.py --uninstall``'s tests can record the
    tool invocations without spawning ``systemctl`` / ``launchctl`` /
    ``schtasks`` on the developer's machine.

    v0.2.92 (R23, pre-existing asymmetry): ``home`` defaults to
    :func:`vco_lib.paths.user_home` — the resolver the REGISTER half has
    used since PR-16 — rather than ``Path.home()``. PR-16 introduced
    ``VCT_USER_HOME_OVERRIDE`` after a test rewrote the developer's real
    systemd unit, but it hardened the WRITE path only; the DELETE path kept
    resolving the real home, so a test that forgot an explicit ``home``
    would have removed that unit outright — strictly worse than the incident
    the override exists for. In production the override is unset and the two
    resolve identically.
    """
    audit: list[str] = []
    home = home or user_home()
    os_name = system or platform.system()
    run = runner or run_quiet

    try:
        if os_name == "Linux":
            systemctl = shutil.which("systemctl")
            if systemctl:
                run([systemctl, "--user", "disable", "--now", spec.unit_name])
            unit_path = home / ".config" / "systemd" / "user" / spec.unit_name
            if unit_path.exists():
                try:
                    unit_path.unlink()
                    audit.append(f"removed systemd user unit {unit_path}")
                except OSError as e:
                    audit.append(f"WARN: could not remove systemd unit {unit_path}: {e}")
            else:
                audit.append(f"no systemd user unit at {unit_path} (nothing to remove)")
            if systemctl:
                run([systemctl, "--user", "daemon-reload"])
            else:
                audit.append(
                    "WARN: systemctl not on PATH — unit file removed (if present) "
                    "but disable/daemon-reload skipped"
                )

        elif os_name == "Darwin":
            plist_path = home / "Library" / "LaunchAgents" / f"{spec.plist_label}.plist"
            launchctl = shutil.which("launchctl")
            if launchctl and plist_path.exists():
                uid = os.getuid() if hasattr(os, "getuid") else 0
                rc = run([launchctl, "bootout", f"gui/{uid}", str(plist_path)])
                if rc != 0:
                    run([launchctl, "unload", "-w", str(plist_path)])
            if plist_path.exists():
                try:
                    plist_path.unlink()
                    audit.append(f"removed LaunchAgent plist {plist_path}")
                except OSError as e:
                    audit.append(
                        f"WARN: could not remove LaunchAgent plist {plist_path}: {e}"
                    )
            else:
                audit.append(f"no LaunchAgent plist at {plist_path} (nothing to remove)")

        elif os_name == "Windows":
            schtasks = shutil.which("schtasks")
            if schtasks:
                rc = run([schtasks, "/Delete", "/TN", spec.task_name, "/F"])
                if rc == 0:
                    audit.append(f"deleted Scheduled Task {spec.task_name}")
                else:
                    audit.append(
                        f"Scheduled Task {spec.task_name} not removed "
                        f"(schtasks rc={rc}; task may not exist)"
                    )
            else:
                audit.append(
                    f"WARN: schtasks not on PATH — Scheduled Task {spec.task_name} "
                    f"not removed; run `schtasks /Delete /TN {spec.task_name} /F` manually"
                )
            xml_path = spec.windows_task_xml_path
            if xml_path is not None and xml_path.exists():
                try:
                    xml_path.unlink()
                    audit.append(f"removed task XML {xml_path}")
                except OSError as e:
                    audit.append(f"WARN: could not remove task XML {xml_path}: {e}")
        else:
            audit.append(f"boot-service removal skipped: unsupported OS {os_name}")
    except Exception as e:  # noqa: BLE001 — uninstall must never crash here
        audit.append(
            f"WARN: {spec.service_id} boot-service removal raised {type(e).__name__}: {e}"
        )
    return audit


def unregister_hub_boot_service(
    hub_binary: Optional[Path] = None,
    *,
    runner: Optional[Callable[..., Optional[int]]] = None,
) -> list[str]:
    """Best-effort ``vct-hub --unregister-boot``.

    The hub owns its own registration in Rust (``boot.rs``), so the inverse
    is its own CLI rather than this module's :func:`unregister` — calling
    ``schtasks``/``systemctl`` behind its back would be a second mechanism
    for one concern. Frequently a no-op: hub autostart is opt-in and
    ``--unregister-boot`` succeeds when nothing was registered.
    """
    audit: list[str] = []
    run = runner or run_quiet
    if hub_binary is not None and Path(hub_binary).exists():
        candidate: Optional[str] = str(hub_binary)
    else:
        candidate = shutil.which("vct-hub")

    if not candidate:
        audit.append(
            "vct-hub binary not found (bundled or on PATH) — hub boot "
            "autostart not unregistered; if you enabled it in the launcher "
            "Preferences, run `vct-hub --unregister-boot` manually"
        )
        return audit

    rc = run([candidate, "--unregister-boot"], timeout=30)
    if rc == 0:
        audit.append("vct-hub --unregister-boot completed (idempotent)")
    else:
        audit.append(
            f"WARN: `{candidate} --unregister-boot` "
            + ("could not be spawned / timed out" if rc is None else f"exited {rc}")
        )
    return audit


# ---------------------------------------------------------------------------
# STATUS
# ---------------------------------------------------------------------------


def status(
    spec: BootServiceSpec,
    *,
    home: Optional[Path] = None,
    system: Optional[str] = None,
) -> BootStatus:
    """Report the current registration state. Mirrors ``boot.rs::status``.

    The artefact's ABSENCE is the only ``NOT_INSTALLED`` answer; a present
    artefact that the init system will not report as enabled reads
    ``DISABLED`` rather than being guessed upward. On Windows a task that
    exists but whose state cannot be parsed reads ``ENABLED``, because this
    module never writes a disabled task — the same last-resort reasoning
    ``boot.rs::windows::status`` documents.
    """
    home = home or user_home()
    os_name = system or platform.system()

    if os_name == "Linux":
        if not systemd_unit_path(spec, home).exists():
            return BootStatus.NOT_INSTALLED
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return BootStatus.DISABLED
        try:
            out = subprocess.run(
                [systemctl, "--user", "is-enabled", spec.unit_name],
                check=False, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return BootStatus.DISABLED
        if out.returncode == 0 and out.stdout.strip() == "enabled":
            return BootStatus.ENABLED
        return BootStatus.DISABLED

    if os_name == "Darwin":
        plist_path = launchd_plist_path(spec, home)
        if not plist_path.exists():
            return BootStatus.NOT_INSTALLED
        launchctl = shutil.which("launchctl")
        if not launchctl:
            return BootStatus.DISABLED
        uid = os.getuid() if hasattr(os, "getuid") else 0
        try:
            out = subprocess.run(
                [launchctl, "print", f"gui/{uid}/{spec.plist_label}"],
                check=False, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return BootStatus.DISABLED
        return BootStatus.ENABLED if out.returncode == 0 else BootStatus.DISABLED

    if os_name == "Windows":
        schtasks = shutil.which("schtasks")
        if not schtasks:
            return BootStatus.NOT_INSTALLED
        try:
            probe = subprocess.run(
                [schtasks, "/Query", "/TN", spec.task_name],
                check=False, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return BootStatus.NOT_INSTALLED
        if probe.returncode != 0:
            return BootStatus.NOT_INSTALLED
        try:
            detail = subprocess.run(
                [schtasks, "/Query", "/TN", spec.task_name, "/V", "/FO", "LIST"],
                check=False, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return BootStatus.ENABLED
        parsed = parse_windows_status_output(detail.stdout)
        return parsed if parsed is not None else BootStatus.ENABLED

    return BootStatus.NOT_INSTALLED


def parse_windows_status_output(text: str) -> Optional[BootStatus]:
    """Parse ``schtasks /Query /V /FO LIST`` output. None when unparseable.

    Locale-fragile in the wild: ``schtasks`` localises both keys AND values,
    so an English-marker miss is the EXPECTED case on non-English Windows,
    not a corner case. Returning None (rather than guessing ``ENABLED``) is
    what lets the caller reach its fallback — the same defect ``boot.rs``
    fixed in v0.2.54 G-8, and the reason this parser is a mirror of
    ``boot.rs::parse_win_status_output`` rather than an independent one.
    """
    for line in text.splitlines():
        trimmed = line.strip()
        if trimmed.endswith(": Enabled") or trimmed.endswith(":Enabled"):
            return BootStatus.ENABLED
        if trimmed.endswith(": Disabled") or trimmed.endswith(":Disabled"):
            return BootStatus.DISABLED
    return None


# ---------------------------------------------------------------------------
# CLI runners — the contract mirrored from `vct-hub`'s boot.rs
# ---------------------------------------------------------------------------


def run_register_boot(
    spec: BootServiceSpec,
    *,
    templates_root: Optional[Path] = None,
    stream=None,
    registrar: Optional[Callable[..., bool]] = None,
) -> int:
    """``--register-boot``: 0 on success, 1 on failure. Enables AND starts.

    Mirrors ``boot.rs::run_register_boot`` including the enable-and-start
    decision: a user who asks for autostart expects the service running on
    the same invocation, not after a reboot.

    ``registrar`` replaces the plain :func:`register` for a service that must
    PROVE something before it writes — today only the model gateway, whose
    entry point is run with ``--version`` first (see
    :func:`gateway_registrar`). It is called as
    ``registrar(spec, templates_root=…, on_event=…)`` and returns the same
    truthy "a unit was written" value; anything it refuses to do it explains
    through ``on_event``, so the printed lines below are the whole story
    either way. One runner, one printed contract, no second copy of the
    kill-switch gate or the exit-code mapping.
    """
    out = stream if stream is not None else sys.stderr
    if boot_registration_disabled():
        print(
            f"boot registration refused: {DISABLE_ENV}=1 is set in this "
            "environment. Unset it and re-run.",
            file=out,
        )
        return 1
    root = templates_root if templates_root is not None else default_templates_root()
    messages: list[str] = []

    def sink(phase: str, detail: str, data: Optional[dict] = None) -> None:
        messages.append(f"{phase}: {detail}")

    ok = (registrar or register)(spec, templates_root=root, on_event=sink)
    for message in messages:
        print(f"{spec.service_id}: {message}", file=out)
    if not ok:
        print(
            f"{spec.service_id}: boot registration did not complete. The lines "
            "above name what was missing.",
            file=out,
        )
        return 1
    return 0


#: Audit fragments that mean the unregister genuinely FAILED, as opposed to
#: warnings that describe a machine where there was nothing to do (no
#: ``systemctl``, no ``schtasks``). Unregistering a service that was never
#: registered, on a host that lacks the tool that would have registered it,
#: is a success — treating it as failure would make an uninstall report an
#: error for doing exactly the right thing.
_UNREGISTER_FAILURE_MARKERS = ("could not remove", "raised")


def run_unregister_boot(spec: BootServiceSpec, *, stream=None) -> int:
    """``--unregister-boot``: 0 on success, 1 on failure. Idempotent."""
    out = stream if stream is not None else sys.stderr
    audit = unregister(spec)
    for line in audit:
        print(f"{spec.service_id}: {line}", file=out)
    failed = any(
        marker in line
        for line in audit
        for marker in _UNREGISTER_FAILURE_MARKERS
    )
    return 1 if failed else 0


def run_boot_status(spec: BootServiceSpec, *, stream=None) -> int:
    """``--boot-status``: prints ONE contract word, exits 0/1/2/3.

    The word and the exit code are a MACHINE CONTRACT shared with
    ``vct-hub --boot-status`` (``enabled`` 0, ``disabled`` 1,
    ``not-installed`` 2, ``error: …`` 3) so one launcher code path can read
    either daemon. Printed with a bare ``print`` to stdout so no log level
    can suppress or reformat it.
    """
    out = stream if stream is not None else sys.stdout
    try:
        result = status(spec)
    except Exception as exc:  # noqa: BLE001 — inspection error is its own state
        print(f"error: {exc}", file=out)
        return BOOT_STATUS_ERROR_EXIT
    print(result.value, file=out)
    return BOOT_STATUS_EXIT_CODES[result]


# ---------------------------------------------------------------------------
# Spec builders
# ---------------------------------------------------------------------------


def container_stack_spec(
    install_path: Path, working_dir: Path, *, os_key: str,
) -> BootServiceSpec:
    """The container stack's spec, as install.py's Step 7b builds it.

    ``os_key`` is ``"Linux"`` / ``"Darwin"`` / ``"Windows"`` and selects the
    wrapper script and the path formatting: Windows prefers the PowerShell
    sibling (v0.2.14 Bug #2 — the bash wrapper made the Scheduled Task
    depend on Git Bash / WSL being on PATH, and it failed silently at every
    logon when they were not) and uses forward slashes.
    """
    if os_key == "Windows":
        wrapper_ps1 = install_path / "scripts" / "launch-claude-mcp-stack.ps1"
        wrapper_sh = install_path / "scripts" / "launch-claude-mcp-stack.sh"
        wrapper = wrapper_ps1 if wrapper_ps1.exists() else wrapper_sh
        substitutions = {
            "WORKING_DIR": _windows_forward(working_dir),
            "WRAPPER_SCRIPT": _windows_forward(wrapper),
        }
    else:
        substitutions = {
            "WORKING_DIR": str(working_dir),
            "WRAPPER_SCRIPT": str(
                install_path / "scripts" / "launch-claude-mcp-stack.sh"
            ),
        }
    return BootServiceSpec(
        service_id="container-stack",
        unit_name=CONTAINER_STACK_UNIT_NAME,
        plist_label=CONTAINER_STACK_PLIST_LABEL,
        task_name=CONTAINER_STACK_TASK_NAME,
        template_linux="templates/systemd/claude-mcp-containers.service.template",
        template_macos=(
            "templates/launchd/com.vibecodedtools.claude-mcp-containers.plist.template"
        ),
        template_windows="templates/windows/claude-mcp-containers.task.xml.template",
        log_basename="claude-mcp-containers.log",
        windows_task_xml_path=install_path / "state" / "installed_boot_task.xml",
        substitutions=substitutions,
        # The install has already brought the stack up; the unit exists for
        # the NEXT boot. Starting it again here would race compose.
        enable_now=False,
        linger=True,
    )


def container_stack_unregister_spec() -> BootServiceSpec:
    """The container stack's spec for UNREGISTER — names only.

    :func:`unregister` needs the three service names and, on Windows, the
    rendered Task XML path; it never renders, so the install-path
    substitutions :func:`container_stack_spec` computes are noise here and
    the uninstaller (``install.py --uninstall``) has no install path to hand
    over that is guaranteed to still exist.

    ``windows_task_xml_path`` is ``None`` ON PURPOSE (leave-alone with a
    reason, pinned by ``test_windows_unregister_does_not_remove_a_task_xml_the_spec_omits``):
    the container stack's rendered Task XML lives at
    ``<install>/state/installed_boot_task.xml``, INSIDE the clone the user is
    deleting. Nothing outside the clone is orphaned, so nothing outside the
    clone is touched.
    """
    return BootServiceSpec(
        service_id="container-stack",
        unit_name=CONTAINER_STACK_UNIT_NAME,
        plist_label=CONTAINER_STACK_PLIST_LABEL,
        task_name=CONTAINER_STACK_TASK_NAME,
        template_linux="templates/systemd/claude-mcp-containers.service.template",
        template_macos=(
            "templates/launchd/com.vibecodedtools.claude-mcp-containers.plist.template"
        ),
        template_windows="templates/windows/claude-mcp-containers.task.xml.template",
        log_basename="claude-mcp-containers.log",
        windows_task_xml_path=None,
    )


#: Console-script names probed beside a candidate interpreter, in order. The
#: ``.exe`` sibling is probed on EVERY OS for the same reason
#: :data:`vco_lib.python_exe.VENV_INTERPRETER_NAMES` is not per-OS: probing a
#: name that cannot exist costs one ``is_file()``, and a per-OS branch is one
#: more place for two resolvers to disagree about the same machine.
GATEWAY_SCRIPT_NAMES = ("vct-model-gateway", "vct-model-gateway.exe")

#: The module form. NOT ``claude_mcp_servers.model_router``:
#: ``claude_mcp_servers/`` has no ``__init__.py``, so the dotted form only ever
#: resolved from the repository root, which a daemon must never assume.
GATEWAY_MODULE = "model_router"

#: The flag that asks a candidate argv "can you run at all?".
#: ``model_router.__main__.main`` answers ``--version`` from the stdlib alone,
#: before it imports ``vco_lib`` or touches the network, a file or a port — so
#: the probe measures exactly what it claims to (this interpreter can import
#: this package) and nothing else. It is appended to the FULL baked argv,
#: ``serve`` included, because ``--version`` is checked ahead of the ``command``
#: positional: what is verified is then byte-for-byte what the unit runs.
GATEWAY_VERIFY_ARG = "--version"

#: Wall-clock cap on ONE verification run. Generous — a cold page cache can
#: make a first interpreter start take seconds — and bounded, because this runs
#: inside an install/update and inside a user's ``--register-boot``.
GATEWAY_VERIFY_TIMEOUT_S = 20


@dataclass(frozen=True)
class GatewayExec:
    """A resolved gateway entry point, and whether it was PROVEN to run."""

    argv: tuple[str, ...]
    #: True only when :func:`verify_gateway_exec` ran ``argv --version`` and
    #: it exited 0. ``False`` with a populated :attr:`argv` means "resolved
    #: but not proven" (verification was not asked for).
    verified: bool
    #: Always populated — every outcome, success or refusal, is NAMED.
    reason: str = ""
    #: ``(argv-as-text, why it failed)`` for every candidate that did not
    #: verify, in probe order, so a refusal can say what was tried.
    tried: tuple[tuple[str, str], ...] = ()

    @property
    def argv_list(self) -> list[str]:
        return list(self.argv)


def _run_capture(cmd: Sequence[str], timeout: float) -> tuple[Optional[int], str]:
    """Run ``cmd``, returning ``(returncode, last stderr/stdout line)``.

    ``None`` as the return code means the process could not be spawned or
    timed out — a state the caller must tell from "ran and failed", because
    only the first one can be a missing file rather than a broken import.
    """
    try:
        proc = subprocess.run(
            list(cmd), check=False, capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"no answer within {timeout:g}s"
    except OSError as exc:
        return None, str(exc)
    text = (proc.stderr or b"").decode("utf-8", "replace").strip()
    if not text:
        text = (proc.stdout or b"").decode("utf-8", "replace").strip()
    tail = text.splitlines()[-1] if text else ""
    return proc.returncode, tail


def verify_gateway_exec(
    argv: Sequence[str],
    *,
    runner: Optional[Callable[[Sequence[str], float], tuple[Optional[int], str]]] = None,
    timeout: float = GATEWAY_VERIFY_TIMEOUT_S,
) -> tuple[bool, str]:
    """Can ``argv`` actually start the gateway? ``(ok, why not)``.

    The 2026-09-10 field defect in one sentence: a unit was written whose
    ``ExecStart`` named an interpreter that cannot import ``model_router``, so
    it was unrunnable from the moment it was written — and nothing noticed for
    eight hours, because the PREVIOUS process was still serving. A resolver
    that only inspects paths cannot see that; running the thing can.
    """
    probe = [*[str(part) for part in argv], GATEWAY_VERIFY_ARG]
    run = runner or _run_capture
    try:
        code, detail = run(probe, timeout)
    except Exception as exc:  # noqa: BLE001 — a probe never raises to a caller
        return False, f"{type(exc).__name__}: {exc}"
    if code == 0:
        return True, ""
    if code is None:
        return False, detail or "could not be spawned"
    return False, f"exited {code}" + (f": {detail}" if detail else "")


def gateway_exec_candidates(
    *,
    install_root: "str | Path | None" = None,
    env: Optional[Mapping[str, str]] = None,
) -> list[list[str]]:
    """Every argv that could start the gateway on this machine, best first.

    Resolved from the INSTALL ROOT's venv through the one interpreter ladder
    (:func:`vco_lib.python_exe.ladder_candidates`: ``$VCT_VENV``, then
    ``<install_root>/.venv``, then the legacy
    ``<install_root>/claude_mcp_servers/.venv``), NOT from ``sys.executable``.
    That inversion is the R5a fix: ``install.py --update`` re-renders the unit
    for every user who registered it, so whichever interpreter happened to run
    the installer used to be baked in — on 2026-09-10 that was a system python
    which cannot import ``model_router`` at all.

    Per rung the console script wins over the module form: it is the shipped
    entry point and it carries its own interpreter, so it keeps working if the
    caller's environment does not. ``sys.executable`` is kept as the LAST rung
    (deduplicated — it is usually the first one already): when no venv resolves
    the install is broken, and this is the only remaining chance of a working
    unit. Which of them is actually used is decided by
    :func:`verify_gateway_exec`, not by this ordering.
    """
    from vco_lib.python_exe import ladder_candidates  # noqa: PLC0415 — see below

    out: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def _add(argv: list[str]) -> None:
        key = tuple(argv)
        if key not in seen:
            seen.add(key)
            out.append(argv)

    def _from_interpreter(interpreter: Path) -> None:
        bindir = interpreter.parent
        for name in GATEWAY_SCRIPT_NAMES:
            script = bindir / name
            try:
                present = script.is_file()
            except OSError:  # pragma: no cover — defensive
                present = False
            if present:
                _add([str(script)])
        _add([str(interpreter), "-m", GATEWAY_MODULE])

    for cand in ladder_candidates(
        install_root=install_root, env=dict(env) if env is not None else None,
    ):
        if cand.ok and cand.path:
            _from_interpreter(Path(cand.path))
    if sys.executable:
        _from_interpreter(Path(sys.executable))
    return out


def resolve_gateway_exec(
    *,
    install_root: "str | Path | None" = None,
    env: Optional[Mapping[str, str]] = None,
) -> list[str]:
    """The best-guess argv a boot unit runs to start the model gateway.

    PURE: paths only, no subprocess — every caller that merely needs the spec's
    NAMES (status, unregister, the uninstaller) pays nothing. The callers that
    are about to WRITE a unit use :func:`resolve_gateway_exec_verified`
    instead, because a resolved path is not evidence that it runs.

    Resolved at registration time and baked into the unit — the same choice
    ``boot.rs`` makes with ``current_exe()``, from the install root's venv
    rather than from this process's interpreter. A clone that later moves needs
    a re-register, which is what ``install.py --update``'s re-render does.
    """
    candidates = gateway_exec_candidates(install_root=install_root, env=env)
    if candidates:
        return list(candidates[0])
    # Only reachable with an empty `sys.executable` (an embedded interpreter).
    return [sys.executable or "python3", "-m", GATEWAY_MODULE]


def resolve_gateway_exec_verified(
    *,
    install_root: "str | Path | None" = None,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[Callable[[Sequence[str], float], tuple[Optional[int], str]]] = None,
    timeout: float = GATEWAY_VERIFY_TIMEOUT_S,
) -> GatewayExec:
    """The first candidate argv that ANSWERS ``--version``, or a refusal.

    ``verified=False`` carries an empty :attr:`GatewayExec.argv`: there is no
    "best effort" answer here on purpose. Writing a unit that cannot start is
    strictly worse than writing none — the unit is enabled, so the init system
    keeps trying it, and the failure lands in a boot log nobody reads while the
    launcher toggle says "registered".
    """
    tried: list[tuple[str, str]] = []
    for argv in gateway_exec_candidates(install_root=install_root, env=env):
        ok, detail = verify_gateway_exec(argv, runner=runner, timeout=timeout)
        if ok:
            return GatewayExec(
                argv=tuple(argv),
                verified=True,
                reason=f"{' '.join(argv)} answered {GATEWAY_VERIFY_ARG}",
                tried=tuple(tried),
            )
        tried.append((" ".join(argv), detail))
    if tried:
        detail = "; ".join(f"{argv} → {why}" for argv, why in tried)
    else:
        detail = "no candidate interpreter resolved at all"
    return GatewayExec(
        argv=(),
        verified=False,
        reason=(
            "no model-gateway entry point on this machine could answer "
            f"`{GATEWAY_VERIFY_ARG}`: {detail}. The install's venv is the one "
            "that must be able to import `model_router` — re-run "
            "`python install.py --update` from the orchestrator root."
        ),
        tried=tuple(tried),
    )


#: Env var pinning the SECRET SCOPE a gateway daemon resolves vendor keys in.
#: Declared by ``model_router.config`` and documented in ``docs/CONFIGURATION.md``.
GATEWAY_SECRET_PROJECT_ENV = "VCT_MODEL_GATEWAY_SECRET_PROJECT"


def _same_secret_scope(a: str, b: str) -> bool:
    """Do two secret-scope strings name the same place?

    ``Path`` equality rather than string equality: it normalises a trailing
    separator and doubled separators, and on Windows it compares
    case-insensitively, which a string compare does not. Deliberately NO
    ``resolve()`` — that stats the filesystem and follows symlinks, and this
    question has to be answerable about an install root that has since MOVED,
    whose old path may no longer exist.
    """
    if a == b:
        return True
    try:
        return Path(a) == Path(b)
    except (TypeError, ValueError):  # pragma: no cover — defensive
        return False


def resolve_gateway_secret_project(
    *,
    env: Optional[Mapping[str, str]] = None,
    installed: Optional[str] = None,
    install_root: "str | Path | None" = None,
) -> str:
    """Which project scope the unit PINS for vendor-key resolution. ``""`` = none.

    The answer on a healthy install is ``""`` — **the rendered unit carries no
    derived value at all**, and the daemon resolves its own scope at runtime.
    A non-empty answer means somebody CHOSE a scope; it is never something
    this function worked out.

    Why, since v0.2.95 did it the other way round first. R5b saw that a boot
    unit runs with ``WorkingDirectory=<VCT_STATE_DIR>``, which is not a
    registered project — and ``agent_secrets`` resolves tier 1 (the hub's
    ``/env`` route, the ONLY route to an OS-keychain key) against
    ``project or Path.cwd()`` — so a daemon started at login answered every
    vendor request "no key found" while the identical key resolved in
    milliseconds from any project's cwd. R5b's fix was to BAKE the install
    root here, at registration time. Q3 then fixed the same defect one layer
    down, in the daemon: with nothing pinned it no longer falls back to its
    cwd but resolves this install's orchestrator root at RUNTIME
    (``model_router.secrets._default_install_root``), which reaches every
    start path — the launcher's, a bare ``vct-model-gateway serve``, an OS
    with no boot registration — and not merely a rendered unit.

    Keeping the bake after that would have DEFEATED the runtime default
    everywhere the gateway is boot-started: the daemon would answer
    ``scope_origin() == "pin"``, ``/health`` would report a pin the user never
    set, and rung 2 below would carry that frozen path forward through every
    later re-render — an install root that MOVED would keep naming the old
    one, which is precisely the defect the hand-written systemd drop-in was.
    A path frozen at render time cannot self-heal; a value resolved by the
    running process can. So rung 3 renders EMPTY and the daemon decides.

    The hub-route alternative (serving shared-scope secrets with no project
    id) was put to the owner and is SETTLED as unnecessary — 2026-09-17,
    *"gateway secrets should be shared in the VCO system, but user can
    override per-project"*. It would not have delivered that: the per-project
    override, the launcher's per-requester pause and the
    ``.no-shared-fallback`` marker are all gated on WHO is asking, and a route
    with no project id has no requester to gate on. The hub's auth surface is
    therefore untouched.

    Precedence, and why:

    1. ``$VCT_MODEL_GATEWAY_SECRET_PROJECT`` in the registering process — an
       explicit pin by the user or the launcher outranks everything. This is
       the per-project OVERRIDE, and it stays available on purpose.
    2. The value already in the INSTALLED artefact — a re-render must not
       silently drop a scope somebody chose (by registering with the variable
       set, by hand-editing the unit, or through a systemd drop-in, which
       :func:`installed_gateway_facts` folds in). ONE carve-out: when that
       value names exactly what this install's root resolves to NOW, it was
       derived rather than chosen — by a pre-fix render or by the drop-in this
       release replaces — and dropping it changes nothing, because the runtime
       default answers with the same path and keeps answering after a move.
       That carve-out is what stops an updating machine from inheriting the
       bake for life.
    3. ``""``: pin nothing. It renders as an empty assignment, which
       ``GatewayConfig.from_env`` reads back as ``None``, which is what makes
       the daemon resolve its own root. Guessing a project here would be worse
       than the gap it fills.
    """
    environ = os.environ if env is None else env
    pinned = (environ.get(GATEWAY_SECRET_PROJECT_ENV) or "").strip()
    if pinned:
        return pinned
    installed_scope = (installed or "").strip()
    if not installed_scope:
        return ""
    try:
        from vco_lib.python_exe import resolve_install_root  # noqa: PLC0415

        root = resolve_install_root(install_root)
    except Exception:  # noqa: BLE001 — cannot compare ⇒ keep what is installed
        return installed_scope
    if root is not None and _same_secret_scope(installed_scope, str(root)):
        return ""
    return installed_scope


def model_gateway_spec(
    *,
    os_key: str,
    exec_argv: Optional[Sequence[str]] = None,
    working_dir: Optional[Path] = None,
    log_file: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    task_xml_path: Optional[Path] = None,
    secret_project: Optional[str] = None,
) -> BootServiceSpec:
    """The model gateway's spec.

    Every argument defaults to something resolved at call time rather than
    at import time, so a redirected ``VCT_STATE_DIR`` (dev launchers) is
    honoured and no build-host path can be baked in.

    ``secret_project`` pins the scope the daemon resolves vendor keys in
    (R5b — see :func:`resolve_gateway_secret_project`). ``None`` means "work
    it out now"; ``""`` means "pin nothing", which renders as an empty
    assignment and reads back as unset.

    ``enable_now=True``: unlike the container stack this is only ever
    reached because a user asked for it, and they expect the gateway
    running now — the same decision ``vct-hub --register-boot`` makes.
    """
    argv = [*(list(exec_argv) if exec_argv else resolve_gateway_exec()), "serve"]
    root = state_dir if state_dir is not None else vct_root_dir()
    wd = working_dir if working_dir is not None else root
    scope = (
        secret_project if secret_project is not None
        else resolve_gateway_secret_project()
    )
    if os_key == "Windows":
        substitutions = {
            # `cmd.exe /c "set VAR=…&& <command> <args> >> log 2>&1"` is the
            # only way a Scheduled Task can both set an env var and capture
            # output; the container-stack task established the shape.
            "EXEC_COMMAND": _windows_forward(Path(argv[0])),
            "EXEC_ARGUMENTS": " ".join(argv[1:]),
            "WORKING_DIR": _windows_forward(wd),
            "STATE_DIR": _windows_forward(root),
            # Forward-slash form like every other path in this task, and
            # EMPTY when nothing is pinned: `set VAR=` clears the variable
            # rather than setting a literal.
            "SECRET_PROJECT": _windows_forward(Path(scope)) if scope else "",
        }
    elif os_key == "Darwin":
        substitutions = {
            # Pre-rendered array body: launchd's ProgramArguments is a plist
            # array, and building it here keeps the template free of any
            # loop construct the naive renderer does not have.
            "EXEC_ARGV_PLIST": "\n".join(
                f"        <string>{xml_escape_content(part)}</string>"
                for part in argv
            ),
            # Every value here lands in plist ELEMENT CONTENT, so every value
            # is escaped — the argv array was, and these were not, which made
            # a home directory containing `&` render an unparseable plist that
            # launchd rejects outright (the same class v0.2.92 fixed centrally
            # for the Windows Task XML).
            "WORKING_DIR": xml_escape_content(wd),
            "STATE_DIR": xml_escape_content(root),
            "SECRET_PROJECT": xml_escape_content(scope),
        }
    else:
        substitutions = {
            "EXEC_START": " ".join(_posix_quote(part) for part in argv),
            "WORKING_DIR": str(wd),
            "STATE_DIR": str(root),
            "SECRET_PROJECT": scope,
        }
    return BootServiceSpec(
        service_id="model-gateway",
        unit_name=MODEL_GATEWAY_UNIT_NAME,
        plist_label=MODEL_GATEWAY_PLIST_LABEL,
        task_name=MODEL_GATEWAY_TASK_NAME,
        template_linux="templates/systemd/vct-model-gateway.service.template",
        template_macos=(
            "templates/launchd/com.vibecodedtools.vct-model-gateway.plist.template"
        ),
        template_windows="templates/windows/vct-model-gateway.task.xml.template",
        log_basename="model-gateway.log",
        # Under `model-gateway/` rather than loose in the state root: the
        # uninstaller's `remove_gateway_state` removes that directory only
        # when it is empty and NAMES whatever is left, so a task XML that
        # somehow outlived its unregister is reported to the user instead of
        # sitting there unmentioned.
        windows_task_xml_path=(
            task_xml_path if task_xml_path is not None
            else root / "model-gateway" / "vct-model-gateway-task.xml"
        ),
        substitutions=substitutions,
        log_file=log_file if log_file is not None else root / "logs" / "model-gateway.log",
        enable_now=True,
        linger=True,
    )


def _posix_quote(token: str) -> str:
    """Single-quote a token for a systemd ``ExecStart=``.

    systemd accepts single-quoted tokens with embedded quotes escaped as
    ``'\\''``, which is what guards an install path containing a space or a
    shell metacharacter. MUST MATCH ``boot.rs::shell_single_quote``.
    """
    return "'" + token.replace("'", r"'\''") + "'"


# ---------------------------------------------------------------------------
# What is ACTUALLY installed — read back from the artefact, never re-derived
# ---------------------------------------------------------------------------

#: Placeholder argv for a spec built to carry NAMES ONLY (unit name, plist
#: label, task name, task-XML path). Never rendered: the callers that pass it
#: read an artefact or ask for a status, they never write one.
_NAMES_ONLY_ARGV = ("-",)

#: Pulls the command and its arguments out of the Windows task's single
#: ``cmd.exe /c "…"`` argument string. The shape is fixed by the shipped
#: template: ``set VAR=…&& "<command>" <args> >> "<log>" 2>&1``.
_WIN_EXEC_RE = re.compile(r'&&\s*"([^"]+)"\s*(.*?)\s*>>')


@dataclass(frozen=True)
class GatewayUnitFacts:
    """What the artefact on THIS machine actually says.

    Read back rather than re-derived, because the two answer different
    questions. "What would we resolve now?" is what a re-render writes;
    "what does the installed unit run?" is the only thing that can tell a user
    their registration is broken — which for eight hours on 2026-09-10 it was.
    """

    #: Where the artefact lives (``None`` on an OS with no known location).
    path: Optional[Path]
    exists: bool
    #: The argv the init system runs, ``serve`` verb included. Empty when the
    #: artefact is absent or could not be parsed.
    argv: tuple[str, ...] = ()
    #: The pinned secret scope, or ``None`` when the unit pins none.
    secret_project: Optional[str] = None
    #: Why a PRESENT artefact yielded no argv. ``None`` when there was nothing
    #: to parse or the parse succeeded — absence is not a parse failure.
    parse_error: Optional[str] = None


def gateway_names_spec(
    os_key: str,
    *,
    state_dir: Optional[Path] = None,
    task_xml_path: Optional[Path] = None,
) -> BootServiceSpec:
    """The gateway spec with NAMES ONLY — no resolution, no subprocess.

    The :func:`container_stack_unregister_spec` precedent: a caller that only
    inspects (status, read-back, ensure) must not pay for, or depend on, the
    entry-point resolution a REGISTRATION needs.
    """
    return model_gateway_spec(
        os_key=os_key,
        exec_argv=_NAMES_ONLY_ARGV,
        state_dir=state_dir,
        task_xml_path=task_xml_path,
        secret_project="",
    )


def _facts_from_systemd(text: str) -> tuple[tuple[str, ...], Optional[str], Optional[str]]:
    """Parse ONE systemd unit body. Last assignment wins, empty one resets.

    systemd's own rule, and the reason it is applied here rather than
    "first ExecStart wins": a DROP-IN (``<unit>.service.d/*.conf``) is
    concatenated after the unit, and the idiom for replacing a command there is
    an empty ``ExecStart=`` followed by the new one. A parser that stopped at
    the first line would report the command the machine does NOT run.
    """
    argv: tuple[str, ...] = ()
    scope: Optional[str] = None
    error: Optional[str] = None
    seen_exec = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("ExecStart="):
            seen_exec = True
            raw = stripped[len("ExecStart="):].strip()
            if not raw:
                argv = ()  # the reset form; a later line supplies the new one
                continue
            try:
                argv = tuple(shlex.split(raw))
                error = None
            except ValueError as exc:
                argv = ()
                error = f"ExecStart= is not parseable: {exc}"
        elif stripped.startswith(f"Environment={GATEWAY_SECRET_PROJECT_ENV}="):
            scope = stripped.split("=", 2)[2].strip().strip("'\"")
    if not argv and error is None:
        error = "no ExecStart= line" if not seen_exec else "ExecStart= is empty"
    return argv, scope, error


def systemd_dropin_paths(unit_path: Path) -> list[Path]:
    """``<unit>.service.d/*.conf``, in the order systemd applies them.

    Lexical order by filename, which is systemd's own ordering rule. VCO writes
    none of these; a user (or a distribution) may, and reading the unit without
    them would report a command this machine does not run — the maintainer's
    own machine carried exactly such a drop-in while this was being written.
    """
    directory = unit_path.with_name(unit_path.name + ".d")
    try:
        return sorted(p for p in directory.glob("*.conf") if p.is_file())
    except OSError:  # pragma: no cover — defensive
        return []


def _facts_from_plist(data: bytes) -> tuple[tuple[str, ...], Optional[str], Optional[str]]:
    try:
        parsed = plistlib.loads(data)
    except Exception as exc:  # noqa: BLE001 — any malformed plist is one state
        return (), None, f"plist is not parseable: {type(exc).__name__}: {exc}"
    argv = parsed.get("ProgramArguments") if isinstance(parsed, dict) else None
    env = parsed.get("EnvironmentVariables") if isinstance(parsed, dict) else None
    scope = None
    if isinstance(env, dict):
        value = env.get(GATEWAY_SECRET_PROJECT_ENV)
        if isinstance(value, str):
            scope = value
    if not isinstance(argv, list) or not argv:
        return (), scope, "no ProgramArguments array"
    return tuple(str(part) for part in argv), scope, None


def _facts_from_task_xml(text: str) -> tuple[tuple[str, ...], Optional[str], Optional[str]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return (), None, f"task XML is not parseable: {exc}"
    arguments = None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "Arguments":
            arguments = element.text or ""
            break
    if arguments is None:
        return (), None, "no <Arguments> element"
    scope_match = re.search(
        rf"set {re.escape(GATEWAY_SECRET_PROJECT_ENV)}=(.*?)&&", arguments,
    )
    scope = scope_match.group(1).strip() if scope_match else None
    exec_match = _WIN_EXEC_RE.search(arguments)
    if exec_match is None:
        return (), scope, "the cmd.exe argument string names no quoted command"
    command, tail = exec_match.group(1), exec_match.group(2).strip()
    try:
        args = shlex.split(tail) if tail else []
    except ValueError as exc:
        return (), scope, f"task arguments are not parseable: {exc}"
    return (command, *args), scope, None


def installed_gateway_facts(
    *,
    home: Optional[Path] = None,
    system: Optional[str] = None,
    state_dir: Optional[Path] = None,
    task_xml_path: Optional[Path] = None,
) -> GatewayUnitFacts:
    """Read the installed gateway artefact. Never raises.

    On Windows the artefact read is the rendered Task XML this module wrote,
    not ``schtasks /Query``: the XML is the same bytes that were imported, it
    is readable without spawning anything, and ``schtasks`` output is
    localised (the lesson :func:`parse_windows_status_output` carries).
    Whether the TASK still exists is :func:`status`'s question; this one is
    "what does the registration run?".
    """
    os_name = system or platform.system()
    spec = gateway_names_spec(
        os_name, state_dir=state_dir, task_xml_path=task_xml_path,
    )
    if os_name == "Linux":
        path = systemd_unit_path(spec, home)
        reader = _facts_from_systemd
        binary = False
    elif os_name == "Darwin":
        path = launchd_plist_path(spec, home)
        reader = _facts_from_plist  # type: ignore[assignment]
        binary = True
    elif os_name == "Windows":
        path = spec.windows_task_xml_path
        reader = _facts_from_task_xml
        binary = False
    else:
        return GatewayUnitFacts(path=None, exists=False)

    if path is None or not path.is_file():
        return GatewayUnitFacts(path=path, exists=False)
    try:
        payload = path.read_bytes() if binary else path.read_text(encoding="utf-8")
        if os_name == "Linux":
            # Drop-ins are part of the unit systemd runs, so they are part of
            # the unit this function reports. Appended in systemd's own order;
            # the parser's last-wins rule then yields the effective command.
            for extra in systemd_dropin_paths(path):
                payload = f"{payload}\n{extra.read_text(encoding='utf-8')}"
    except OSError as exc:
        return GatewayUnitFacts(
            path=path, exists=True, parse_error=f"could not be read: {exc}",
        )
    argv, scope, error = reader(payload)  # type: ignore[arg-type]
    return GatewayUnitFacts(
        path=path,
        exists=True,
        argv=argv,
        secret_project=scope or None,
        parse_error=error,
    )


# ---------------------------------------------------------------------------
# REGISTER the gateway — verified before anything is written
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GatewayRegistration:
    """Outcome of :func:`register_model_gateway`."""

    #: What :func:`register` reported: the artefact is on disk AND the init
    #: system accepted it. It is deliberately NOT "a file was written" —
    #: ``register_linux`` returns False on a machine with no ``systemctl``,
    #: having correctly left a unit behind for the user to enable later, and a
    #: field that called that a write would make the CLI exit 0 on a
    #: registration nothing will start. What happened either way is on
    #: ``on_event``.
    registered: bool
    #: Nothing was written BECAUSE no entry point could be proven to run.
    refused: bool
    exec_result: GatewayExec
    #: The spec that was rendered, or ``None`` when nothing was.
    spec: Optional[BootServiceSpec]
    reason: str


def register_model_gateway(
    *,
    templates_root: Path,
    on_event: EventSink = _noop_event,
    system: Optional[str] = None,
    home: Optional[Path] = None,
    update_only: bool = False,
    install_root: "str | Path | None" = None,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[Callable[[Sequence[str], float], tuple[Optional[int], str]]] = None,
    state_dir: Optional[Path] = None,
    log_file: Optional[Path] = None,
    verify: bool = True,
) -> GatewayRegistration:
    """The ONE home for writing a model-gateway boot registration.

    Both writers go through here — ``vct-model-gateway --register-boot``
    (fresh) and ``install.py --update`` (``update_only=True``, which creates
    nothing) — so the verify-before-write rule cannot hold on one path and not
    the other.

    Order is load-bearing:

    1. ``update_only`` asks :func:`status` FIRST, so a machine that never
       opted in pays no subprocess and keeps its "no registration" state.
    2. The entry point is resolved AND run (:func:`resolve_gateway_exec_verified`).
    3. Only then is anything written. A refusal leaves an existing artefact
       BYTE-IDENTICAL: the unit that is there may be broken, but replacing it
       with a second broken one helps nobody, and the caller is told.
    """
    os_name = system or platform.system()
    if boot_registration_disabled():
        on_event("skip", f"{DISABLE_ENV}=1 — skipping", None)
        return GatewayRegistration(
            registered=False, refused=False,
            exec_result=GatewayExec(argv=(), verified=False, reason="kill switch"),
            spec=None, reason=f"{DISABLE_ENV}=1",
        )

    facts = installed_gateway_facts(
        home=home, system=os_name, state_dir=state_dir,
    )
    names = gateway_names_spec(os_name, state_dir=state_dir)
    if update_only and status(names, home=home, system=os_name) is BootStatus.NOT_INSTALLED:
        return GatewayRegistration(
            registered=False, refused=False,
            exec_result=GatewayExec(
                argv=(), verified=False, reason="not registered",
            ),
            spec=None,
            reason="no existing model-gateway registration — nothing to re-render",
        )

    if verify:
        resolution = resolve_gateway_exec_verified(
            install_root=install_root, env=env, runner=runner,
        )
    else:
        argv = resolve_gateway_exec(install_root=install_root, env=env)
        resolution = GatewayExec(
            argv=tuple(argv), verified=False,
            reason="verification skipped by the caller",
        )
    if verify and not resolution.verified:
        on_event("warn", f"model-gateway: {resolution.reason}", {
            "tried": [argv for argv, _ in resolution.tried],
        })
        return GatewayRegistration(
            registered=False, refused=True, exec_result=resolution, spec=None,
            reason=resolution.reason,
        )

    spec = model_gateway_spec(
        os_key=os_name,
        exec_argv=resolution.argv_list,
        state_dir=state_dir,
        log_file=log_file,
        secret_project=resolve_gateway_secret_project(
            env=env, installed=facts.secret_project, install_root=install_root,
        ),
    )
    if update_only:
        accepted = rerender_if_registered(
            spec, templates_root=templates_root, on_event=on_event,
            system=os_name, home=home,
        )
    else:
        accepted = register(
            spec, templates_root=templates_root, on_event=on_event,
            system=os_name, home=home,
        )
    return GatewayRegistration(
        registered=bool(accepted), refused=False, exec_result=resolution, spec=spec,
        reason=resolution.reason,
    )


def gateway_registrar(
    *,
    install_root: "str | Path | None" = None,
    state_dir: Optional[Path] = None,
    log_file: Optional[Path] = None,
    runner: Optional[Callable[[Sequence[str], float], tuple[Optional[int], str]]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Callable[..., bool]:
    """A :func:`run_register_boot` registrar that VERIFIES before it writes.

    The ``spec`` handed to it supplies the service IDENTITY the runner prints
    with; the spec actually rendered is rebuilt by
    :func:`register_model_gateway` around the entry point that answered
    ``--version``, which is the whole point — a registration must never bake
    an argv nobody ran.
    """

    def _registrar(
        spec: BootServiceSpec,
        *,
        templates_root: Path,
        on_event: EventSink = _noop_event,
    ) -> bool:
        result = register_model_gateway(
            templates_root=templates_root,
            on_event=on_event,
            install_root=install_root,
            state_dir=state_dir,
            log_file=log_file,
            runner=runner,
            env=env,
        )
        if result.refused:
            on_event(
                "warn",
                "nothing was written: an unrunnable unit is worse than none, "
                "because the init system keeps retrying it while the launcher "
                "toggle reads `registered`",
                None,
            )
        return result.registered

    return _registrar


# ---------------------------------------------------------------------------
# ENSURE — start a registration that already exists
# ---------------------------------------------------------------------------

#: Per-OS "start it if it is not already running", as command TEMPLATES the
#: caller fills with the spec's names. Documented together because the three
#: differ in one way that matters and in one that does not:
#:
#: * Linux NEEDS a ``reset-failed`` first. A unit parked by ``StartLimitBurst``
#:   answers ``start`` with "start request repeated too quickly" and does
#:   nothing — a no-op exactly when the ensure is needed. ``reset-failed`` on a
#:   healthy unit is itself a no-op, so it is issued unconditionally rather
#:   than after a second probe.
#: * macOS and Windows have no equivalent park: launchd throttles (10 s,
#:   ``ThrottleInterval``) and Task Scheduler retries a bounded number of times
#:   (``RestartOnFailure``); neither latches a unit out of startability.
#: * ``launchctl kickstart`` WITHOUT ``-k`` starts a job only if it is not
#:   running; ``-k`` would KILL a healthy gateway mid-stream, which is the
#:   opposite of an ensure. ``schtasks /Run`` is safe for the same reason from
#:   the other direction: the task declares ``IgnoreNew``.
_ENSURE_SUPPORTED_OS = ("Linux", "Darwin", "Windows")


def ensure_commands(spec: BootServiceSpec, *, system: Optional[str] = None) -> list[list[str]]:
    """The commands that start ``spec``'s registered service, in order.

    Returns ``[]`` when the OS is unsupported or the init tool is absent —
    "nothing to run" rather than a guess. The tool paths come from
    :func:`shutil.which` so a test can make the whole set absent or present.
    """
    os_name = system or platform.system()
    if os_name == "Linux":
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return []
        return [
            [systemctl, "--user", "reset-failed", spec.unit_name],
            [systemctl, "--user", "start", spec.unit_name],
        ]
    if os_name == "Darwin":
        launchctl = shutil.which("launchctl")
        if not launchctl:
            return []
        uid = os.getuid() if hasattr(os, "getuid") else 0
        return [[launchctl, "kickstart", f"gui/{uid}/{spec.plist_label}"]]
    if os_name == "Windows":
        schtasks = shutil.which("schtasks")
        if not schtasks:
            return []
        return [[schtasks, "/Run", "/TN", spec.task_name]]
    return []


def start_if_registered(
    spec: BootServiceSpec,
    *,
    home: Optional[Path] = None,
    system: Optional[str] = None,
    runner: Optional[Callable[..., Optional[int]]] = None,
) -> tuple[bool, list[list[str]], str]:
    """Start ``spec``'s service IF it is registered. ``(started, cmds, why)``.

    The leave-alone case is first and it is silent: an unregistered service is
    an OPT-IN the user has not taken, and a session-start ensure that
    registered one would be making that decision for them.
    """
    os_name = system or platform.system()
    if status(spec, home=home, system=os_name) is BootStatus.NOT_INSTALLED:
        return False, [], "not registered"
    commands = ensure_commands(spec, system=os_name)
    if not commands:
        return False, [], (
            f"no init-system tool available to start {spec.service_id} on {os_name}"
        )
    run = runner or run_quiet
    for cmd in commands:
        run(cmd)
    return True, commands, f"{spec.service_id} start requested"


# ---------------------------------------------------------------------------
# Gateway runtime state — the "path off the machine" half of delivery
# ---------------------------------------------------------------------------

#: Files the model gateway (and the launcher's export of its context table)
#: leave under the state root. Basenames only — the root is resolved by the
#: caller so ``VCT_STATE_DIR`` is honoured.
#: The daemon's single-instance lockfile and port file, under the state root.
#: Named constants because a READER exists (``vco_lib.gateway_ensure``, which
#: reports "already running" from the daemon's own guard rather than adding a
#: second one) and a second literal is how the scrub list and the reader drift
#: apart. ``model_router.config`` builds the same paths for the daemon itself;
#: ``test_the_scrub_list_matches_the_paths_the_gateway_actually_uses`` is what
#: keeps the two sides honest.
GATEWAY_PID_BASENAME = "model-gateway.pid"
GATEWAY_PORT_BASENAME = "model-gateway.port"

GATEWAY_STATE_FILES = (
    "model-gateway.token",
    GATEWAY_PID_BASENAME,
    GATEWAY_PORT_BASENAME,
    "logs/model-gateway.log",
    "model-gateway/chat_model_context.json",
    # Written ONLY when something already occupied the export path before
    # VCO first wrote it (`BackupPolicy::Once`), i.e. the already-damaged
    # case. Absent on every healthy machine; removed when present because
    # it is a copy of a file we are removing.
    "model-gateway/chat_model_context.json.pre-vco",
)

#: Emptied-then-removed if and only if nothing else is left inside it.
GATEWAY_STATE_DIRS = ("model-gateway",)


def gateway_state_paths(state_dir: Optional[Path] = None) -> list[Path]:
    """Absolute paths of the gateway's state files, in removal order."""
    root = state_dir if state_dir is not None else vct_root_dir()
    return [root / rel for rel in GATEWAY_STATE_FILES]


def remove_gateway_state(
    state_dir: Optional[Path] = None, *, dry_run: bool = False,
) -> list[str]:
    """Remove the gateway's runtime state files. Returns audit lines.

    NAMED FILES ONLY. The state root itself is never removed and is never
    walked: it also holds ``hub.token``, ``hub.port``, ``services.toml`` and
    the launcher database, which are other components' state or the user's
    own data. ``logs/`` is likewise left in place — the hub and the launcher
    write there too — and ``model-gateway/`` is removed only when it is
    empty after the named files are gone, so a file someone else put there
    survives.

    Never raises: a file that cannot be removed is reported, and the
    uninstall continues.
    """
    root = state_dir if state_dir is not None else vct_root_dir()
    audit: list[str] = []
    for rel in GATEWAY_STATE_FILES:
        path = root / rel
        if not path.exists():
            audit.append(f"no {path} (nothing to remove)")
            continue
        if dry_run:
            audit.append(f"would remove {path}")
            continue
        try:
            path.unlink()
            audit.append(f"removed {path}")
        except OSError as e:
            audit.append(f"WARN: could not remove {path}: {e}")
    for rel in GATEWAY_STATE_DIRS:
        directory = root / rel
        if not directory.is_dir():
            continue
        try:
            remaining = sorted(p.name for p in directory.iterdir())
        except OSError as e:
            audit.append(f"WARN: could not inspect {directory}: {e}")
            continue
        if remaining:
            audit.append(
                f"kept {directory} — still holds {', '.join(remaining)}"
            )
            continue
        if dry_run:
            audit.append(f"would remove empty {directory}")
            continue
        try:
            directory.rmdir()
            audit.append(f"removed empty {directory}")
        except OSError as e:
            audit.append(f"WARN: could not remove {directory}: {e}")
    return audit


__all__ = [
    "BOOT_STATUS_ERROR_EXIT",
    "BOOT_STATUS_EXIT_CODES",
    "CONTAINER_STACK_PLIST_LABEL",
    "CONTAINER_STACK_TASK_NAME",
    "CONTAINER_STACK_UNIT_NAME",
    "DISABLE_ENV",
    "GATEWAY_MODULE",
    "GATEWAY_PID_BASENAME",
    "GATEWAY_PORT_BASENAME",
    "GATEWAY_SCRIPT_NAMES",
    "GATEWAY_SECRET_PROJECT_ENV",
    "GATEWAY_STATE_DIRS",
    "GATEWAY_STATE_FILES",
    "GATEWAY_VERIFY_ARG",
    "GATEWAY_VERIFY_TIMEOUT_S",
    "MODEL_GATEWAY_PLIST_LABEL",
    "MODEL_GATEWAY_TASK_NAME",
    "MODEL_GATEWAY_UNIT_NAME",
    "BootServiceSpec",
    "BootStatus",
    "GatewayExec",
    "GatewayRegistration",
    "GatewayUnitFacts",
    "backup_and_write_idempotent",
    "boot_log_file",
    "boot_registration_disabled",
    "container_stack_spec",
    "default_templates_root",
    "ensure_commands",
    "gateway_exec_candidates",
    "gateway_names_spec",
    "gateway_registrar",
    "gateway_state_paths",
    "installed_gateway_facts",
    "launchd_plist_path",
    "linux_log_file",
    "macos_log_file",
    "model_gateway_spec",
    "parse_windows_status_output",
    "read_template",
    "register",
    "register_linux",
    "register_macos",
    "register_model_gateway",
    "register_windows",
    "remove_gateway_state",
    "render_template",
    "rerender_if_registered",
    "resolve_gateway_exec",
    "resolve_gateway_exec_verified",
    "resolve_gateway_secret_project",
    "run_boot_status",
    "run_quiet",
    "run_register_boot",
    "run_unregister_boot",
    "start_if_registered",
    "status",
    "verify_gateway_exec",
    "systemd_dropin_paths",
    "systemd_unit_path",
    "unregister",
    "unregister_hub_boot_service",
    "windows_log_file",
    "windows_user_id",
    "xml_escape_content",
]
