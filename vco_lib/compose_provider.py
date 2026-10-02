# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""vco_lib.compose_provider — WHICH compose actually runs, and the socket it needs.

v0.2.100 (AD-3). Two machine facts decide whether ``compose up`` can work and
which files it can parse, and before this module nothing asked either:

1. **The provider.** ``podman compose`` is not a compose implementation: it
   delegates to an external provider — ``docker-compose`` (v2) or
   ``podman-compose`` — chosen by ``PODMAN_COMPOSE_PROVIDER``, then
   ``containers.conf`` ``[engine] compose_providers``, then ``$PATH``
   (``docker-compose`` before ``podman-compose``). The two label their objects
   differently (``com.docker.compose.*`` vs ``io.podman.compose.*``), parse
   different GPU overlays (docker-compose cannot read the CDI ``devices:``
   spec), and the choice can change between two runs on the same machine.
   :func:`detect` names the provider from positive evidence first (the banner
   ``podman compose version`` prints) and only then from the configuration
   podman itself reads.

2. **The API socket.** ``podman info`` does not use ``podman.sock``;
   docker-compose does (``DOCKER_HOST=unix://…/podman.sock``). On 2026-09-29 the
   socket FILE vanished while the ``podman.socket`` unit stayed ``active``:
   ``podman info`` passed, ``systemctl --user start`` was a no-op, and compose
   failed with "Cannot connect to the Docker daemon". :func:`socket_status`
   tells those states apart and :func:`heal_socket` applies the one safe,
   non-destructive repair per state (containers keep running):

   ====================== =============================================
   ``ok``                  socket file present — nothing to do
   ``unit_active_file_missing``  ``systemctl --user restart podman.socket``
   ``down``                ``systemctl --user start podman.socket``
   ``machine_down``        ``podman machine`` init-if-absent + start
   ``not_applicable``      docker (its daemon is not ours to start here), a
                           compose that never dials the socket
                           (podman-compose), or a ``DOCKER_HOST`` /
                           ``CONTAINER_HOST`` that is remote or not podman's
   ``unknown``             a probe could not run — no action on uncertainty
   ====================== =============================================

   The socket examined is the one compose dials, in podman's own order:
   ``DOCKER_HOST`` (handed to the provider unchanged), then what ``podman
   info`` reports (``.Host.RemoteSocket``), then the derived default; a unit
   whose custom ``ListenStream`` is present counts as ``ok``.

Every probe is injectable (``run`` / ``which`` / ``env`` / ``system`` /
``exists``); the defaults are :mod:`vco_lib.tool_search_dirs` like the rest of
:mod:`vco_lib.containers`, the ONE home of runtime + compose-form resolution.
This module adds the provider and the socket; it does not re-derive either.
"""
from __future__ import annotations

import json
import os
import platform
import re
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from vco_lib import containers as _c
from vco_lib import tool_search_dirs as _tsd

__all__ = [
    "ComposeProvider",
    "SocketStatus",
    "HealResult",
    "ENGINE_DOCKER_COMPOSE",
    "ENGINE_PODMAN_COMPOSE",
    "ENGINE_DOCKER",
    "ENGINE_UNKNOWN",
    "SOCKET_OK",
    "SOCKET_UNIT_ACTIVE_FILE_MISSING",
    "SOCKET_DOWN",
    "SOCKET_MACHINE_DOWN",
    "SOCKET_NOT_APPLICABLE",
    "SOCKET_UNKNOWN",
    "detect",
    "provider_from_form",
    "socket_path",
    "reported_socket",
    "socket_status",
    "AUTO_PROVIDER",
    "heal_socket",
    "runtime_reachable",
    "podman_machine_init_and_start",
    "overlay_candidates",
    "overlay_for_provider",
]

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]
WhichFn = Callable[[str], Optional[str]]
ExistsFn = Callable[[str], bool]
LogFn = Callable[[str], None]

ENGINE_DOCKER_COMPOSE = "docker-compose"
ENGINE_PODMAN_COMPOSE = "podman-compose"
ENGINE_DOCKER = "docker"
ENGINE_UNKNOWN = "unknown"

#: The label family each engine stamps on containers / networks / volumes.
_FAMILY_BY_ENGINE = {
    ENGINE_DOCKER_COMPOSE: "docker",
    ENGINE_DOCKER: "docker",
    ENGINE_PODMAN_COMPOSE: "podman",
}

#: The engine a compose FORM implies when nothing more specific is known —
#: the pre-v0.2.100 rule (formerly `service_adoption.GPU_OVERLAY_BY_FORM`, constraints
#: #11), kept as the LAST resort only: a `podman compose` that delegates to
#: podman-compose breaks it, which is why :func:`detect` asks first.
_ENGINE_BY_FORM = {
    "subcommand": ENGINE_DOCKER_COMPOSE,
    "standalone": ENGINE_PODMAN_COMPOSE,
}

SOCKET_OK = "ok"
SOCKET_UNIT_ACTIVE_FILE_MISSING = "unit_active_file_missing"
SOCKET_DOWN = "down"
SOCKET_MACHINE_DOWN = "machine_down"
SOCKET_NOT_APPLICABLE = "not_applicable"
SOCKET_UNKNOWN = "unknown"

SOCKET_UNIT = "podman.socket"
_PROBE_TIMEOUT_S = 15


@dataclass(frozen=True)
class ComposeProvider:
    """The compose implementation that will actually run.

    ``form`` — ``subcommand`` (``<runtime> compose``) or ``standalone``.
    ``engine`` — the implementation behind it; ``label_family`` — the label
    namespace its objects carry (``docker`` / ``podman`` / ``unknown``).
    ``evidence`` says how the engine was established (a human line)."""

    form: str
    engine: str
    argv: tuple[str, ...]
    label_family: str
    runtime: str
    evidence: str = ""

    @property
    def needs_api_socket(self) -> bool:
        """docker-compose (and a `podman compose` delegating to it) talks to
        the podman API socket; podman-compose drives the podman CLI and never
        dials it. An engine VCO could not name counts as needing it: the socket
        heal is non-destructive, and skipping it on a guess would leave a
        docker-compose delegate unable to connect.

        Read by :func:`socket_status` / :func:`runtime_reachable` /
        :func:`heal_socket` (their ``provider`` argument): a provider that does
        not need the socket gets ``not_applicable`` — no restart, no wait, no
        ``compose_socket_heal_failed`` row for a socket it never uses
        (v0.2.100 wave-1 review W1R-07)."""
        return self.runtime == "podman" and self.label_family != "podman"


def _family(engine: str) -> str:
    return _FAMILY_BY_ENGINE.get(engine, "unknown")


def _engine_from_name(name: str) -> Optional[str]:
    base = Path(str(name).strip().strip('"').strip("'")).name.lower()
    if base.endswith(".exe"):
        base = base[:-4]
    if base == "docker-compose" or base.startswith("docker-compose-"):
        return ENGINE_DOCKER_COMPOSE
    if base == "podman-compose" or base.startswith("podman-compose-"):
        return ENGINE_PODMAN_COMPOSE
    return None


_EXTERNAL_PROVIDER_RE = re.compile(r'Executing external compose provider\s+"([^"]+)"')
_DOCKER_COMPOSE_BANNER_RE = re.compile(r"^\s*Docker Compose version\b", re.MULTILINE)
_PODMAN_COMPOSE_BANNER_RE = re.compile(r"^\s*podman-compose version\b", re.MULTILINE)


def _engine_from_version_output(text: str) -> Optional[str]:
    m = _EXTERNAL_PROVIDER_RE.search(text or "")
    if m:
        eng = _engine_from_name(m.group(1))
        if eng:
            return eng
    if _DOCKER_COMPOSE_BANNER_RE.search(text or ""):
        return ENGINE_DOCKER_COMPOSE
    if _PODMAN_COMPOSE_BANNER_RE.search(text or ""):
        return ENGINE_PODMAN_COMPOSE
    return None


def _containers_conf_paths(env: Mapping[str, str], home: Path) -> list[Path]:
    """The files podman reads for ``[engine] compose_providers`` — an explicit
    ``CONTAINERS_CONF`` replaces the chain; otherwise the user file wins over
    the system ones."""
    explicit = (env.get("CONTAINERS_CONF") or "").strip()
    if explicit:
        return [Path(explicit)]
    xdg = (env.get("XDG_CONFIG_HOME") or "").strip()
    user_dir = Path(xdg) if xdg else home / ".config"
    return [
        user_dir / "containers" / "containers.conf",
        Path("/etc/containers/containers.conf"),
        Path("/usr/share/containers/containers.conf"),
    ]


def _conf_compose_providers(path: Path) -> Optional[list[str]]:
    try:
        import tomllib  # noqa: PLC0415 — py3.11+, the project floor
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    engine = data.get("engine")
    if not isinstance(engine, dict):
        return None
    provs = engine.get("compose_providers")
    if isinstance(provs, list) and provs:
        return [str(p) for p in provs]
    return None


def _engine_from_config(env: Mapping[str, str], home: Path,
                        which: WhichFn) -> Optional[tuple[str, str]]:
    """``(engine, evidence)`` from what podman itself reads, in its order."""
    pinned = (env.get("PODMAN_COMPOSE_PROVIDER") or "").strip()
    if pinned:
        eng = _engine_from_name(pinned)
        if eng:
            return eng, f"PODMAN_COMPOSE_PROVIDER={pinned}"
    for conf in _containers_conf_paths(env, home):
        provs = _conf_compose_providers(conf)
        if not provs:
            continue
        # podman uses the first entry that exists; a name we cannot place
        # does not decide anything.
        for entry in provs:
            eng = _engine_from_name(entry)
            if eng is None:
                continue
            if os.path.isabs(entry) and not os.path.exists(entry):
                continue
            return eng, f"{conf}: compose_providers"
        break  # the first file that declares the key is the one podman uses
    for name in (ENGINE_DOCKER_COMPOSE, ENGINE_PODMAN_COMPOSE):
        if which(name):
            return name, f"$PATH search order ({name} found first)"
    return None


def provider_from_form(form: Optional[str], *, runtime: str = "podman",
                       argv: Sequence[str] = ()) -> Optional[ComposeProvider]:
    """The provider a bare compose FORM implies (last-resort rule — see
    ``_ENGINE_BY_FORM``). ``None`` for an unknown form."""
    engine = _ENGINE_BY_FORM.get(form or "")
    if engine is None:
        return None
    if runtime == "docker" and form == "subcommand":
        engine = ENGINE_DOCKER
    return ComposeProvider(form or "", engine, tuple(argv), _family(engine), runtime,
                           "inferred from the compose form only")


def detect(
    runtime: str,
    *,
    argv: Optional[Sequence[str]] = None,
    run: Optional[RunFn] = None,
    which: Optional[WhichFn] = None,
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Optional[ComposeProvider]:
    """Which compose runs for ``runtime``. ``argv`` is the compose prefix the
    caller already resolved (:func:`vco_lib.containers.compose_command`);
    without it this module resolves it the same way. ``None`` when there is no
    compose at all."""
    _run = run or _tsd.run
    _which = which or _tsd.which
    _env = os.environ if env is None else env
    _home = home or Path.home()
    if argv is None:
        found = _c.compose_command(runtime, which=_which, run=_run, home=_home)
        if found is None:
            return None
        argv, form = found
    else:
        argv = list(argv)
        form = "subcommand" if len(argv) >= 2 and argv[1] == "compose" else "standalone"
    argv_t = tuple(argv)
    if form == "standalone":
        engine = _engine_from_name(argv_t[0]) if argv_t else None
        if engine:
            return ComposeProvider(form, engine, argv_t, _family(engine), runtime,
                                   f"standalone {Path(argv_t[0]).name}")
        return provider_from_form(form, runtime=runtime, argv=argv_t)
    if runtime == "docker":
        return ComposeProvider(form, ENGINE_DOCKER, argv_t, "docker", runtime,
                               "docker compose plugin")
    # `podman compose` — ask it which provider it runs (positive evidence).
    try:
        res = _run([*argv_t, "version"], capture_output=True, text=True,
                   timeout=_PROBE_TIMEOUT_S)
        text = f"{res.stdout or ''}\n{res.stderr or ''}"
    except (subprocess.TimeoutExpired, OSError):
        text = ""
    engine = _engine_from_version_output(text)
    if engine:
        return ComposeProvider(form, engine, argv_t, _family(engine), runtime,
                               "`podman compose version` output")
    configured = _engine_from_config(_env, _home, _which)
    if configured:
        engine, why = configured
        return ComposeProvider(form, engine, argv_t, _family(engine), runtime, why)
    return ComposeProvider(form, ENGINE_UNKNOWN, argv_t, "unknown", runtime,
                           "provider could not be established")


# ---------------------------------------------------------------------------
# The API socket
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SocketStatus:
    kind: str
    path: Optional[str] = None
    unit: Optional[str] = None
    detail: str = ""


@dataclass
class HealResult:
    """What a heal did. ``healed`` — the condition is positively gone now;
    ``actions`` — the commands run (for the log / report); ``reason`` — why
    nothing was done, or why it did not help; ``deferral_cid`` — the ledger
    row the caller owes when the heal was REFUSED for safety."""

    healed: bool
    actions: list[str] = field(default_factory=list)
    reason: str = ""
    deferral_cid: Optional[str] = None
    #: facts the heal established (container id, layer, network, project …) —
    #: the ledger row renders its manual recipe from these, so the user gets
    #: the exact commands, not placeholders
    details: dict = field(default_factory=dict)


def _uid() -> Optional[int]:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid else None


def socket_path(env: Optional[Mapping[str, str]] = None, *,
                uid: Optional[int] = None) -> Optional[str]:
    """The DERIVED podman API socket path — the last-resort fallback of
    :func:`socket_status`, used only when neither the environment nor podman
    itself names one: ``DOCKER_HOST`` / ``CONTAINER_HOST`` when they name a
    ``unix://`` path, else the rootless default under ``$XDG_RUNTIME_DIR``
    (root: ``/run/podman/podman.sock``)."""
    _env = os.environ if env is None else env
    for key in ("DOCKER_HOST", "CONTAINER_HOST"):
        val = (_env.get(key) or "").strip()
        if val.startswith("unix://"):
            return val[len("unix://"):]
    _uid_v = _uid() if uid is None else uid
    if _uid_v == 0:
        return "/run/podman/podman.sock"
    runtime_dir = (_env.get("XDG_RUNTIME_DIR") or "").strip()
    if not runtime_dir and _uid_v is not None:
        runtime_dir = f"/run/user/{_uid_v}"
    if not runtime_dir:
        return None
    return str(Path(runtime_dir) / "podman" / "podman.sock")


def _explicit_host(env: Mapping[str, str]) -> Optional[tuple[str, str]]:
    """``(KEY, value)`` for the first of ``DOCKER_HOST`` / ``CONTAINER_HOST``
    that is set. ``podman compose`` hands an already-set ``DOCKER_HOST`` to its
    provider unchanged, so it names the socket compose will dial."""
    for key in ("DOCKER_HOST", "CONTAINER_HOST"):
        val = (env.get(key) or "").strip()
        if val:
            return key, val
    return None


def reported_socket(runtime: str, *, run: Optional[RunFn] = None,
                    which: Optional[WhichFn] = None) -> Optional[tuple[str, bool]]:
    """``(path, exists)`` of the API socket as PODMAN reports it
    (``podman info`` ``.Host.RemoteSocket``) — the authoritative answer, which
    already reflects podman's own configuration. ``None`` when podman does not
    answer or reports no path (the caller falls back to the derived path)."""
    if runtime != "podman":
        return None
    _run = run or _tsd.run
    _which = which or _tsd.which
    exe = _which(runtime) or runtime
    try:
        res = _run([exe, "info", "--format",
                    "{{.Host.RemoteSocket.Path}}|{{.Host.RemoteSocket.Exists}}"],
                   capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None
    line = next((ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()), "")
    path, sep, exists = line.rpartition("|")
    if not sep:
        return None
    path = path.strip()
    if path.startswith("unix://"):
        path = path[len("unix://"):]
    if not path.startswith("/"):
        return None
    return path, exists.strip().lower() == "true"


_LISTEN_PATH_RE = re.compile(r"(/\S+?)\s+\((?:Stream|SequentialPacket|Datagram)\)")


def _unit_listen_paths(run: RunFn, *, root: bool) -> list[str]:
    """The socket paths the ``podman.socket`` unit listens on
    (``systemctl show -p Listen``) — a drop-in with a custom ``ListenStream``
    shows here and nowhere else. ``[]`` when it cannot be read."""
    argv = (["systemctl", "show", SOCKET_UNIT, "--property=Listen", "--value"] if root
            else ["systemctl", "--user", "show", SOCKET_UNIT, "--property=Listen", "--value"])
    try:
        res = run(argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if res.returncode != 0:
        return []
    return _LISTEN_PATH_RE.findall(res.stdout or "")


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _systemctl_argv(verb: str, *, root: bool) -> list[str]:
    return ["systemctl", verb, SOCKET_UNIT] if root else ["systemctl", "--user", verb, SOCKET_UNIT]


def socket_status(
    runtime: str,
    *,
    run: Optional[RunFn] = None,
    which: Optional[WhichFn] = None,
    env: Optional[Mapping[str, str]] = None,
    system: Optional[str] = None,
    exists: Optional[ExistsFn] = None,
    uid: Optional[int] = None,
    provider: Optional[ComposeProvider] = None,
    path: Optional[str] = None,
) -> SocketStatus:
    """Classify the runtime's API socket (see the module table). Read-only.

    Which socket (v0.2.100 wave-1 review W1R-05) — the one compose will
    actually dial, in podman's own order: ``path`` when the caller has it
    (compose named it in its error), else ``DOCKER_HOST`` / ``CONTAINER_HOST``
    (a non-``unix://`` endpoint is ``not_applicable``: remote, nothing local
    to heal), else what ``podman info`` reports, else the derived default
    (:func:`socket_path`). A missing file under an active unit is
    ``unit_active_file_missing`` only when a restart can bring THAT file back:
    a unit listening on a different, present path (a custom ``ListenStream``)
    is ``ok``, and an explicit ``DOCKER_HOST`` naming a socket that is not
    podman's (e.g. a docker context) is ``not_applicable``.

    ``provider`` — when given and it does not use the API socket
    (:attr:`ComposeProvider.needs_api_socket`), the answer is
    ``not_applicable`` without probing anything."""
    if runtime != "podman":
        return SocketStatus(SOCKET_NOT_APPLICABLE, detail=f"{runtime}: not a podman socket")
    if provider is not None and not provider.needs_api_socket:
        return SocketStatus(SOCKET_NOT_APPLICABLE,
                            detail=f"{provider.engine} drives the podman CLI; it never dials "
                                   "the API socket")
    _run = run or _tsd.run
    _which = which or _tsd.which
    _env = os.environ if env is None else env
    _exists = exists or _is_socket
    os_name = system or platform.system()
    if os_name in ("Darwin", "Windows"):
        up = _c.daemon_responsive(runtime, which=_which, run=_run)
        if up is None:
            return SocketStatus(SOCKET_UNKNOWN, detail="`podman info` could not run")
        if up:
            return SocketStatus(SOCKET_OK, detail="podman machine answers")
        return SocketStatus(SOCKET_MACHINE_DOWN, detail="podman machine does not answer `podman info`")
    if os_name != "Linux":
        return SocketStatus(SOCKET_UNKNOWN, detail=f"unsupported OS {os_name!r}")
    _uid_v = _uid() if uid is None else uid
    root = _uid_v == 0

    explicit_path: Optional[str] = None
    if path:
        explicit_path = path[len("unix://"):] if path.startswith("unix://") else path
    else:
        host = _explicit_host(_env)
        if host is not None:
            key, val = host
            if not val.startswith("unix://"):
                return SocketStatus(SOCKET_NOT_APPLICABLE,
                                    detail=f"{key}={val} is not a local socket; VCO does not "
                                           "manage it")
            explicit_path = val[len("unix://"):]
    reported = reported_socket(runtime, run=_run, which=_which)
    derived = socket_path({k: v for k, v in _env.items()
                           if k not in ("DOCKER_HOST", "CONTAINER_HOST")}, uid=_uid_v)
    sock = explicit_path or (reported[0] if reported else None) or derived
    if sock is None:
        return SocketStatus(SOCKET_UNKNOWN, detail="no XDG_RUNTIME_DIR to locate the socket")
    if (reported is not None and reported[0] == sock and reported[1]) or _exists(sock):
        return SocketStatus(SOCKET_OK, path=sock, unit=SOCKET_UNIT)
    if not _which("systemctl"):
        return SocketStatus(SOCKET_UNKNOWN, path=sock,
                            detail="socket file missing and systemctl is not available")
    try:
        res = _run(_systemctl_argv("is-active", root=root),
                   capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return SocketStatus(SOCKET_UNKNOWN, path=sock, unit=SOCKET_UNIT,
                            detail="`systemctl is-active podman.socket` could not run")
    listen = _unit_listen_paths(_run, root=root)
    podman_paths = {p for p in (derived, reported[0] if reported else None, *listen) if p}
    if explicit_path is not None and explicit_path not in podman_paths:
        return SocketStatus(SOCKET_NOT_APPLICABLE, path=sock,
                            detail=f"{sock} is not podman's API socket (podman: "
                                   f"{', '.join(sorted(podman_paths)) or 'unknown'}); VCO does "
                                   "not manage it")
    state = (res.stdout or "").strip().splitlines()
    if state and state[0].strip() == "active":
        custom = [p for p in listen if p != sock and _exists(p)]
        if custom:
            return SocketStatus(SOCKET_OK, path=custom[0], unit=SOCKET_UNIT,
                                detail=f"{SOCKET_UNIT} listens on {custom[0]} (custom "
                                       f"ListenStream), not {sock}")
        return SocketStatus(SOCKET_UNIT_ACTIVE_FILE_MISSING, path=sock, unit=SOCKET_UNIT,
                            detail=f"{SOCKET_UNIT} is active but {sock} does not exist")
    return SocketStatus(SOCKET_DOWN, path=sock, unit=SOCKET_UNIT,
                        detail=f"{SOCKET_UNIT} is not active and {sock} does not exist")


class _AutoProvider:
    """Sentinel: :func:`runtime_reachable` detects the provider itself."""


AUTO_PROVIDER = _AutoProvider()


def runtime_reachable(runtime: str, *, run: Optional[RunFn] = None,
                      which: Optional[WhichFn] = None,
                      env: Optional[Mapping[str, str]] = None,
                      system: Optional[str] = None,
                      exists: Optional[ExistsFn] = None,
                      provider: "Optional[ComposeProvider] | _AutoProvider" = AUTO_PROVIDER,
                      ) -> bool:
    """``<runtime> info`` answers AND the podman API socket is not in a state
    that is known broken (``unit_active_file_missing`` / ``machine_down``) FOR
    A COMPOSE THAT USES IT. ``podman info`` alone does not use the socket, so
    it cannot see state 1 of the module doc. A socket that is merely not
    started (``down``) is not "unreachable": podman-compose does not need it,
    and a docker-compose provider's refusal is classified and healed at
    compose time.

    ``provider`` (W1R-07): by default the provider is detected — only when the
    socket is in a broken state, so a healthy machine pays nothing extra — and
    a provider that never dials the socket (podman-compose) keeps the runtime
    reachable. ``None`` = unknown provider (treated as needing the socket)."""
    if _c.daemon_responsive(runtime, which=which, run=run) is not True:
        return False
    prov = None if isinstance(provider, _AutoProvider) else provider
    kind = socket_status(runtime, run=run, which=which, env=env, system=system,
                         exists=exists, provider=prov).kind
    if kind not in (SOCKET_UNIT_ACTIVE_FILE_MISSING, SOCKET_MACHINE_DOWN):
        return True
    if isinstance(provider, _AutoProvider):
        detected = detect(runtime, run=run, which=which, env=env)
        if detected is not None and not detected.needs_api_socket:
            return True
    return False


def podman_machine_init_and_start(*, run: Optional[RunFn] = None,
                                  log: Optional[LogFn] = None) -> tuple[bool, str]:
    """macOS / Windows: ``podman machine init`` ONLY when no machine exists at
    all, then ``podman machine start``. The init downloads a ~500 MB VM image
    unattended (600 s cap) and says so before it starts; an existing machine
    is never re-initialised. ``(ok, detail)``; soft-fails on every path.

    (Moved from install.py's ``_podman_machine_auto_init_and_start``, v0.2.53
    M-P1-2; the default ``run`` is looked up at call time so a patched
    ``subprocess.run`` is honoured.)"""
    _run = run or (lambda argv, **kw: subprocess.run(argv, **kw))
    _log = log or (lambda msg: print(msg, flush=True))
    machine_exists = False
    try:
        listed = _run(["podman", "machine", "list", "--format", "json"],
                      capture_output=True, text=True, timeout=15)
        if listed.returncode == 0:
            try:
                machines = json.loads(listed.stdout or "[]")
                machine_exists = bool(machines and isinstance(machines, list))
            except (json.JSONDecodeError, TypeError):
                machine_exists = False
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"podman machine list failed: {exc}"
    if not machine_exists:
        _log("  Podman machine not initialized; running `podman machine init` "
             "(this downloads ~500 MB; may take 2-5 min)...")
        try:
            init = _run(["podman", "machine", "init"], capture_output=True, text=True,
                        timeout=600)
        except subprocess.TimeoutExpired:
            return False, ("podman machine init timed out after 10 min; network down or VM "
                           "image download blocked. Run `podman machine init` manually and "
                           "re-run install.py.")
        except OSError as exc:
            return False, f"podman machine init failed: {exc}"
        if init.returncode != 0:
            return False, (f"podman machine init exited {init.returncode}: "
                           f"{(init.stderr or '').strip()[:300]}")
        _log("  Podman machine initialized.")
    try:
        start = _run(["podman", "machine", "start"], capture_output=True, text=True,
                     timeout=120)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"podman machine start failed: {exc}"
    if start.returncode != 0:
        err = (start.stderr or "").strip()[:300]
        if "already running" in err.lower():
            return True, "podman machine already running"
        return False, f"podman machine start exited {start.returncode}: {err}"
    return True, "podman machine started"


def heal_socket(
    runtime: str,
    *,
    run: Optional[RunFn] = None,
    which: Optional[WhichFn] = None,
    env: Optional[Mapping[str, str]] = None,
    system: Optional[str] = None,
    exists: Optional[ExistsFn] = None,
    uid: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
    wait_s: float = 30.0,
    machine_start: Optional[Callable[[], tuple[bool, str]]] = None,
    status: Optional[SocketStatus] = None,
    provider: Optional[ComposeProvider] = None,
    path: Optional[str] = None,
) -> HealResult:
    """Apply the one non-destructive repair for the socket's state, then
    re-probe. Never touches a container, a volume or a network.

    ``healed`` means a repair was APPLIED and the condition is gone (W1R-04):
    a socket that is already ``ok`` / ``not_applicable`` returns
    ``healed=False``, no action and no ledger row, so a retry loop stops
    instead of re-running an identical ``compose up``. ``path`` is the socket
    compose actually dialled (its error names it); ``provider`` skips the heal
    for a compose that never uses the socket (W1R-07)."""
    _run = run or _tsd.run
    _which = which or _tsd.which
    def _probe() -> SocketStatus:
        return socket_status(runtime, run=_run, which=_which, env=env,
                             system=system, exists=exists, uid=uid,
                             provider=provider, path=path)

    st = status or _probe()
    if st.kind in (SOCKET_OK, SOCKET_NOT_APPLICABLE):
        why = f": {st.detail}" if st.detail else ""
        return HealResult(False, reason=f"nothing to heal (socket {st.kind}{why})")
    if st.kind == SOCKET_UNKNOWN:
        return HealResult(False, reason=f"socket state unknown: {st.detail} — no action on uncertainty")
    actions: list[str] = []
    if st.kind == SOCKET_MACHINE_DOWN:
        ok, detail = (machine_start or (lambda: podman_machine_init_and_start(run=run)))()
        actions.append("podman machine start")
        if not ok:
            return HealResult(False, actions, detail, "compose_socket_heal_failed")
    else:
        verb = "restart" if st.kind == SOCKET_UNIT_ACTIVE_FILE_MISSING else "start"
        _uid_v = _uid() if uid is None else uid
        argv = _systemctl_argv(verb, root=_uid_v == 0)
        actions.append(" ".join(argv))
        try:
            res = _run(argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return HealResult(False, actions, f"{' '.join(argv)} could not run: {exc}",
                              "compose_socket_heal_failed")
        if res.returncode != 0:
            return HealResult(False, actions,
                              f"{' '.join(argv)} exited {res.returncode}: "
                              f"{(res.stderr or '').strip()[:200]}",
                              "compose_socket_heal_failed")
    deadline = time.monotonic() + wait_s
    while True:
        now = _probe()
        if now.kind == SOCKET_OK:
            return HealResult(True, actions, "socket answers again")
        if time.monotonic() >= deadline:
            where = f" ({now.path})" if now.path else ""
            return HealResult(False, actions,
                              f"socket still {now.kind}{where} after {int(wait_s)}s: {now.detail}",
                              "compose_socket_heal_failed")
        sleep(1.0)


# ---------------------------------------------------------------------------
# GPU overlay by PROVIDER (L1-F10)
# ---------------------------------------------------------------------------


def overlay_candidates(provider: Optional[ComposeProvider],
                       gpu_vendor: Optional[str]) -> tuple[str, ...]:
    """The overlay file names this provider can parse, preferred first. The
    FILE follows the label family of the engine that parses it (docker-compose
    cannot read ``podman-compose.gpu.yml``'s CDI ``devices:`` spec), never the
    runtime's name. AMD: the canonical short name, then the legacy one."""
    family = provider.label_family if provider else "unknown"
    if family == "unknown" and provider is not None:
        implied = provider_from_form(provider.form, runtime=provider.runtime)
        family = implied.label_family if implied else "docker"
    stem = "podman-compose" if family == "podman" else "docker-compose"
    if (gpu_vendor or "").lower() == "amd":
        return (f"{stem}.rocm.yml", f"{stem}.amd-rocm.yml")
    return (f"{stem}.gpu.yml",)


def overlay_for_provider(provider: Optional[ComposeProvider], gpu_vendor: Optional[str],
                         infra_dir: Optional[Path] = None) -> Optional[str]:
    """The overlay to add for ``provider``: with ``infra_dir``, the first
    candidate present there (``None`` when none is); without, the preferred
    name. ``None`` for no provider."""
    if provider is None:
        return None
    cands = overlay_candidates(provider, gpu_vendor)
    if infra_dir is None:
        return cands[0]
    return next((c for c in cands if (Path(infra_dir) / c).is_file()), None)
