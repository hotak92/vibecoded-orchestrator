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
import shutil
import subprocess
import sys
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
) -> int:
    """``--register-boot``: 0 on success, 1 on failure. Enables AND starts.

    Mirrors ``boot.rs::run_register_boot`` including the enable-and-start
    decision: a user who asks for autostart expects the service running on
    the same invocation, not after a reboot.
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

    ok = register(spec, templates_root=root, on_event=sink)
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


def resolve_gateway_exec() -> list[str]:
    """The argv a boot unit runs to start the model gateway.

    Preferred: the ``vct-model-gateway`` console script beside the running
    interpreter, because that is the shipped entry point and it carries the
    right interpreter with it. Fallback: ``<this python> -m model_router``,
    which resolves from any working directory (``claude_mcp_servers/`` has
    no ``__init__.py``, so the dotted ``claude_mcp_servers.model_router``
    form only ever worked from the repo root and is never used here).

    Resolved at REGISTRATION time and baked into the unit — the same choice
    ``boot.rs`` makes with ``current_exe()``. A clone that later moves needs
    a re-register, which is what ``install.py --update``'s re-render does.
    """
    bindir = Path(sys.executable).parent
    for name in ("vct-model-gateway", "vct-model-gateway.exe"):
        candidate = bindir / name
        if candidate.exists():
            return [str(candidate)]
    return [sys.executable, "-m", "model_router"]


def model_gateway_spec(
    *,
    os_key: str,
    exec_argv: Optional[Sequence[str]] = None,
    working_dir: Optional[Path] = None,
    log_file: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    task_xml_path: Optional[Path] = None,
) -> BootServiceSpec:
    """The model gateway's spec.

    Every argument defaults to something resolved at call time rather than
    at import time, so a redirected ``VCT_STATE_DIR`` (dev launchers) is
    honoured and no build-host path can be baked in.

    ``enable_now=True``: unlike the container stack this is only ever
    reached because a user asked for it, and they expect the gateway
    running now — the same decision ``vct-hub --register-boot`` makes.
    """
    argv = [*(list(exec_argv) if exec_argv else resolve_gateway_exec()), "serve"]
    root = state_dir if state_dir is not None else vct_root_dir()
    wd = working_dir if working_dir is not None else root
    if os_key == "Windows":
        substitutions = {
            # `cmd.exe /c "set VAR=…&& <command> <args> >> log 2>&1"` is the
            # only way a Scheduled Task can both set an env var and capture
            # output; the container-stack task established the shape.
            "EXEC_COMMAND": _windows_forward(Path(argv[0])),
            "EXEC_ARGUMENTS": " ".join(argv[1:]),
            "WORKING_DIR": _windows_forward(wd),
            "STATE_DIR": _windows_forward(root),
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
            "WORKING_DIR": str(wd),
            "STATE_DIR": str(root),
        }
    else:
        substitutions = {
            "EXEC_START": " ".join(_posix_quote(part) for part in argv),
            "WORKING_DIR": str(wd),
            "STATE_DIR": str(root),
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
# Gateway runtime state — the "path off the machine" half of delivery
# ---------------------------------------------------------------------------

#: Files the model gateway (and the launcher's export of its context table)
#: leave under the state root. Basenames only — the root is resolved by the
#: caller so ``VCT_STATE_DIR`` is honoured.
GATEWAY_STATE_FILES = (
    "model-gateway.token",
    "model-gateway.pid",
    "model-gateway.port",
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
    "GATEWAY_STATE_DIRS",
    "GATEWAY_STATE_FILES",
    "MODEL_GATEWAY_PLIST_LABEL",
    "MODEL_GATEWAY_TASK_NAME",
    "MODEL_GATEWAY_UNIT_NAME",
    "BootServiceSpec",
    "BootStatus",
    "backup_and_write_idempotent",
    "boot_log_file",
    "boot_registration_disabled",
    "container_stack_spec",
    "default_templates_root",
    "gateway_state_paths",
    "launchd_plist_path",
    "linux_log_file",
    "macos_log_file",
    "model_gateway_spec",
    "parse_windows_status_output",
    "read_template",
    "register",
    "register_linux",
    "register_macos",
    "register_windows",
    "remove_gateway_state",
    "render_template",
    "rerender_if_registered",
    "resolve_gateway_exec",
    "run_boot_status",
    "run_quiet",
    "run_register_boot",
    "run_unregister_boot",
    "status",
    "systemd_unit_path",
    "unregister",
    "unregister_hub_boot_service",
    "windows_log_file",
    "windows_user_id",
    "xml_escape_content",
]
