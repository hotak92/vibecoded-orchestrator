# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``vct-model-gateway`` — the daemon's entry point.

Usage::

    vct-model-gateway                    # serve (default)
    vct-model-gateway serve --port 11500
    vct-model-gateway --check            # configuration self-test, exit 0/1
    vct-model-gateway --print-token-path
    vct-model-gateway --print-export-path
    vct-model-gateway --register-boot    # start at login (opt-in), exit 0/1
    vct-model-gateway --unregister-boot  # idempotent inverse, exit 0/1
    vct-model-gateway --boot-status      # enabled|disabled|not-installed

Also reachable as ``python -m model_router`` in any environment where the
distribution is installed. Note the module is ``model_router``, NOT
``claude_mcp_servers.model_router``: ``claude_mcp_servers/`` has no
``__init__.py`` and is not itself a package, so the dotted form only resolves
from the repository root, which a daemon must never assume it is running in.

Boot registration (systemd user unit, launchd agent, Windows Scheduled Task)
is performed by the shared boot-service home that already registers the
container stack — :mod:`vco_lib.boot_service`. The three flags below are
argument parsing and nothing else: no per-OS branch, no template, no
``systemctl`` call lives in this file, because a second implementation is
exactly what the tri-OS rule exists to prevent. Their verb names, stdout
words and exit codes mirror ``vct-hub --register-boot`` /
``--unregister-boot`` / ``--boot-status`` so one launcher code path can
drive either daemon.

Registration is OPT-IN and never happens at install time: a login-time
daemon holding an OAuth passthrough is a security-surface change the user
makes deliberately. ``install.py --update`` re-renders an EXISTING
registration so a moved clone keeps a working unit, and creates none.

Cross-OS notes: no ``flock``, no ``fork``, no signal games, no ``/proc``, no
``os.getuid``. Single-instance is a pid file, the shared cross-OS liveness
probe (``os.kill(pid, 0)`` on POSIX, ``OpenProcess`` on Windows — the probe
knows that ``os.kill`` on Windows TERMINATES rather than probes) AND a
``/health`` identity check: a live pid is not proof that a gateway is
running, and the difference decides between exiting 0 ("already serving,
nothing to do") and taking the pid file over. Paths are built with
``pathlib``, never string-joined.

**A start that finds a running gateway SUCCEEDS.** ``Restart=on-failure`` in
the shipped unit means a non-zero exit is respun forever at ``RestartSec``,
and on 2026-09-08 that turned "you already started one by hand" into 1442
restarts over four hours. The unit's start limit now ends a real crash loop;
this file makes sure the ordinary case is not one.
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import socket
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger("model_router")

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _stderr_handlers() -> "list[logging.Handler]":
    """Root handlers writing to THIS process's stderr (never the file one)."""
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        and getattr(handler, "stream", None) is sys.stderr
    ]


def _configure_logging(log_file: Optional[Path]) -> Optional[str]:
    """A file when one can be opened; stderr for what the file cannot hold.

    The file handler is best-effort by design: a gateway that refuses to start
    because it could not open a log file would be worse than one that logs to
    stderr only. Failing to open it is reported once, on stderr, naming the
    path — not swallowed.

    **When the file handler opens, stderr is raised to WARNING.** An init
    system captures this process's stderr into ``<name>.boot.log``, so an
    INFO-level stderr wrote every access line into BOTH files: the daemon's
    own log and a boot log nothing rotates, which then grew for the life of
    the daemon rather than only across failed starts. Raising the level keeps
    the boot log to what it is FOR — a refusal, a traceback, a warning — and
    loses nothing, because the same records are in the daemon's own log at
    full detail. If the file handler could NOT open, stderr stays at INFO:
    then it is the only place the records exist.
    """
    from vco_lib.log_setup import configure_logging

    configure_logging(logging.INFO, format=_LOG_FORMAT, stream=sys.stderr)
    if log_file is None:
        return None
    from .fileperms import PermissionHardeningError, restrict_to_owner

    # Captured BEFORE the file handler is added, so the loop below can never
    # reach the handler it is meant to leave at INFO.
    on_stderr = _stderr_handlers()
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file, encoding="utf-8")
    except OSError as exc:
        return f"could not open log file {log_file} ({exc}); logging to stderr only"
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logging.getLogger().addHandler(handler)
    for existing in on_stderr:
        existing.setLevel(logging.WARNING)
    try:
        restrict_to_owner(log_file)
    except PermissionHardeningError as exc:
        return (
            f"log file {log_file} could not be made owner-only ({exc}); it may "
            "be readable by other local users"
        )
    return None


#: How long the liveness half of "is this port free?" waits for a connect to
#: be accepted or refused. Loopback answers in microseconds; the timeout is
#: only there so a firewall that DROPS rather than refuses cannot stall a
#: start. A timeout reads as "not a live listener", which is the conservative
#: direction here: the bind that follows is the authority.
_CONNECT_PROBE_TIMEOUT_S = 0.2


def _apply_reuse_flags(sock: "socket.socket") -> None:
    """The one place the per-platform socket-option decision is made.

    MUST MATCH ``port_is_free`` in
    ``launcher/src-tauri/src/commands/model_gateway.rs``: the launcher decides
    whether to pass ``--port`` using its own probe, so a rule that differs
    between the two produces a pin the daemon then refuses.

    * **POSIX (Linux AND macOS/BSD)** — ``SO_REUSEADDR``. It is what lets a
      restart reclaim a port whose previous CONNECTIONS are still in
      ``TIME_WAIT`` (~30 s after a clean stop). Setting it only on Linux made
      every macOS restart inside that window fail: a resolved port drifted
      into the fallback range, and a port the launcher had PINNED with
      ``--port`` made ``serve`` exit 1 for half a minute after every stop.
      What the flag does NOT do on either platform is defeat the liveness
      check, because that check is a ``connect()`` — see
      :func:`_listener_accepts` — not a bind.
    * **Windows** — NOTHING. Not ``SO_REUSEADDR``, which there means "steal
      a port another process is listening on"; and not
      ``SO_EXCLUSIVEADDRUSE``, which was the obvious guard against that
      theft and is the wrong trade here. Microsoft documents that a socket
      bound with it "cannot necessarily be reused immediately after socket
      closure … until the original connection becomes inactive" — i.e. it
      re-introduces on Windows exactly the TIME_WAIT refusal the POSIX flag
      exists to remove, and asymmetrically: the launcher's own probe (Rust
      ``TcpListener::bind``, which sets no option on Windows) would report
      the port FREE, spawn ``serve --port P``, and the exclusive bind would
      answer ``WSAEADDRINUSE`` — an explicit pin, so exit 1, for the whole
      wait window. A default Windows bind already succeeds over TIME_WAIT,
      which is what a restart needs.

      What is given up is small and was never a boundary: the hijack
      ``SO_EXCLUSIVEADDRUSE`` prevents is another process on the SAME user
      account binding this loopback port with ``SO_REUSEADDR``. A process
      running as this user can read the host-token file anyway, so there is
      no privilege line here to defend — and the liveness half of "is this
      port free?" is the ``connect()`` probe on every OS, not the flag.
    """
    if os.name == "nt":
        return
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def _listener_accepts(family: int, sockaddr: object) -> bool:
    """True when something ACCEPTS a connection at ``sockaddr``.

    The liveness half of "is this port free?", and the half a bind cannot
    answer once ``SO_REUSEADDR`` is in play: on macOS/BSD that flag lets a
    bind to ``127.0.0.1:P`` succeed while another process listens on
    ``0.0.0.0:P``, so a successful bind is not proof the port is ours alone.
    A refused connect is: nothing is listening at that address, by any
    binding, in any process.
    """
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.settimeout(_CONNECT_PROBE_TIMEOUT_S)
        probe.connect(sockaddr)  # type: ignore[arg-type]
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _bind_targets(host: str, port: int) -> "list[tuple[int, int, int, object]]":
    """Every distinct address ``host`` names for ``port``, deduplicated."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    targets: list[tuple[int, int, int, object]] = []
    seen: set[tuple[int, object]] = set()
    for family, socktype, proto, _canonname, sockaddr in infos:
        key = (family, tuple(sockaddr[:2]))
        if key in seen:
            continue
        seen.add(key)
        targets.append((family, socktype, proto, sockaddr))
    return targets


def _bind_socket(host: str, port: int) -> "list[socket.socket]":
    """Bind EVERY address ``host`` names on ``port``, or raise. All-or-nothing.

    Probing with a throwaway bind and then binding for real is a race with a
    window wide enough to lose on a busy machine; these are the very sockets
    handed to ``run_app``, so what was tested is what serves.

    **All the addresses, not the first one.** ``AF_INET`` used to be
    hardcoded, which un-supported ``VCT_MODEL_GATEWAY_HOST=::1``; taking
    ``getaddrinfo``'s first result instead fixed that and introduced a
    subtler one, because ``localhost`` is a NAME: its first result may be
    ``::1`` while every consumer of the port file connects to
    ``127.0.0.1``, so the daemon would serve an address nobody dials.
    Binding all of them removes the guess. Any one of them being taken makes
    the PORT taken — the sockets already bound are closed and the caller
    moves to the next port — because a gateway reachable on half its
    addresses is worse than one that moved.

    Freedom is decided in two steps, in this order, and the shared design is
    documented in :func:`_apply_reuse_flags`:

    1. :func:`_listener_accepts` — a connect that is ACCEPTED means a live
       listener owns the address (specific or wildcard). Reported as
       ``EADDRINUSE`` so the existing fallback and explicit-pin logic apply
       unchanged.
    2. the bind itself, with the platform's reuse flags.
    """
    targets = _bind_targets(host, port)
    if not targets:
        raise OSError(
            errno.EADDRNOTAVAIL, f"{host!r} resolves to no bindable address",
        )
    for family, _socktype, _proto, sockaddr in targets:
        if _listener_accepts(family, sockaddr):
            raise OSError(
                errno.EADDRINUSE,
                f"a listener already accepts connections on {sockaddr!r}",
            )
    bound: list[socket.socket] = []
    try:
        for family, socktype, proto, sockaddr in targets:
            sock = socket.socket(family, socktype, proto)
            bound.append(sock)
            _apply_reuse_flags(sock)
            sock.bind(sockaddr)  # type: ignore[arg-type]
            sock.listen(128)
    except OSError:
        for sock in bound:
            sock.close()
        raise
    return bound


def _bind_with_fallback(
    host: str, port: int, explicit: bool,
) -> "tuple[list[socket.socket], int]":
    """The requested port, else the first free one in the fallback range.

    Both halves of the decision matter and both are tested. An EXPLICIT port
    is a pin: it fails with the address-in-use error naming that port, because
    a daemon that quietly moved off a port the user pinned is a daemon whose
    clients now talk to nothing. A RESOLVED port is a guess, and moving off a
    taken one is the difference between a gateway that starts at login and one
    that has never started on this machine.

    Only "address in use" triggers the fallback. A permission error or an
    unroutable host is not a busy port and must surface as itself.
    """
    from .config import FALLBACK_PORT_RANGE

    try:
        return _bind_socket(host, port), port
    except OSError as first:
        if explicit or first.errno != errno.EADDRINUSE:
            raise
    for candidate in FALLBACK_PORT_RANGE:
        if candidate == port:
            continue
        try:
            return _bind_socket(host, candidate), candidate
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
    raise OSError(
        errno.EADDRINUSE,
        f"port {port} and every fallback in "
        f"{FALLBACK_PORT_RANGE.start}-{FALLBACK_PORT_RANGE.stop - 1} are in use",
    )


def _write_owner_only(path: Path, text: str, *, strict: bool) -> None:
    """Write ``text`` and restrict the file to its owner.

    ``strict=True`` re-raises when the restriction cannot be applied; that is
    reserved for the host token, which IS a credential — serving with a
    world-readable token file would hand any other local user the ability to
    proxy under this user's Claude login and paid vendor subscription. The pid
    and port files carry a process id and a port number, so a failure there is
    reported loudly and the daemon continues: refusing to run over an
    unrestricted port file would trade a real capability for no secrecy gain.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _harden_owner_only(path, strict=strict)


def _harden_owner_only(path: Path, *, strict: bool) -> None:
    """The restriction half of :func:`_write_owner_only`, on its own.

    Split out because the pid file is no longer WRITTEN by that function —
    it is created through an exclusive ``os.open`` (see
    :func:`_acquire_single_instance`) and only needs the hardening. One home
    for the hardening decision, two ways of producing the bytes.
    """
    from .fileperms import PermissionHardeningError, restrict_to_owner

    try:
        restrict_to_owner(path)
    except PermissionHardeningError as exc:
        if strict:
            raise
        logger.warning(
            "model-gateway: %s could not be made owner-only (%s); it may be "
            "readable by other local users",
            path, exc,
        )


#: Returned in place of a pid when the existing pid file cannot be read as
#: one. Not ``None``: "the file exists and says nothing usable" and "the file
#: is ours now" are different answers and the caller acts differently on each.
UNREADABLE_PID = -1


def _recorded_pid(pid_path: Path) -> int:
    """The pid in ``pid_path``, or :data:`UNREADABLE_PID`. Never raises.

    The sentinel is a VALUE here, not ``None``, which is why the shared
    reader (:func:`vco_lib.intfile.read_int_line`) takes one: this caller
    must tell "the file exists and says nothing usable" from "the file is
    mine now", and both are answers ``_acquire_single_instance`` returns.

    Imported INSIDE the function, unlike ``config._read_port_file`` which
    imports the same helper at module level. Not a cycle — this file has no
    ``vco_lib`` import at module scope at all, deliberately: ``--version``,
    ``--print-token-path`` and ``--print-export-path`` answer without
    importing anything heavier than the stdlib, so they still work on an
    install whose ``vco_lib`` is mid-update. ``config.py`` has no such
    constraint (it already imports ``vco_lib.paths`` to exist at all), so
    each file follows its own rule rather than one file breaking its own.
    """
    from vco_lib.intfile import read_int_line

    return read_int_line(pid_path, sentinel=UNREADABLE_PID, minimum=1)


def _acquire_single_instance(pid_path: Path) -> Optional[int]:
    """Claim the pid file ATOMICALLY, or return the pid recorded in it.

    ``O_CREAT | O_EXCL`` — the CREATE is the lock. Read-then-write left a
    window in which two starts could both find no live holder, both write
    their own pid, and both bind: the pid file then named one instance while
    the port file named the other, and every reader that trusts the pair was
    reading two different daemons. Exclusive creation closes it at the only
    layer that can — the filesystem — without importing a cross-OS locking
    primitive this file deliberately avoids (no ``flock``, no ``fork``).

    It decides NOTHING beyond who got there first. Whether the recorded pid
    means "already serving" (exit 0), "a number reused after a crash" or
    "garbage" is :func:`_serve`'s call, because only it can ask ``/health``.

    Returns ``None`` when the file is now ours; otherwise the recorded pid,
    or :data:`UNREADABLE_PID`.
    """
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(
            str(pid_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600,
        )
    except FileExistsError:
        return _recorded_pid(pid_path)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n")
    # The 0o600 above is already the POSIX answer (modulo umask, which can
    # only REMOVE bits); this is what makes Windows agree, and it warns
    # rather than refusing — a pid file is a process id, not a credential.
    _harden_owner_only(pid_path, strict=False)
    return None


#: How long a start keeps asking an already-recorded gateway to identify
#: itself before concluding there is none. Three seconds because the racing
#: case is a SIBLING that has claimed the pid file and not yet finished
#: importing aiohttp and binding — a cold interpreter on a loaded machine at
#: login is the slow case — and because the whole cost is paid only on the
#: rare path where a live pid answers nothing.
_HEALTH_PROBE_DEADLINE_S = 3.0

#: Per-attempt timeout. Short: a loopback port either answers at once or is
#: not ours.
_HEALTH_PROBE_TIMEOUT_S = 1.0

_HEALTH_PROBE_INTERVAL_S = 0.25

#: Bound on the ``/health`` body read during the probe. It is a small JSON
#: document; anything larger is not this daemon answering.
_HEALTH_BODY_LIMIT_BYTES = 64 * 1024


def _health_url(host: str, port: int) -> str:
    """``http://host:port/health``, with an IPv6 literal bracketed."""
    return f"http://{f'[{host}]' if ':' in host else host}:{port}/health"


def _gateway_answers(host: str, port: int, timeout_s: float) -> bool:
    """True when THIS daemon — not merely something — answers on ``host:port``.

    Identity, not liveness. A socket that accepts is no evidence at all here:
    "something else owns the port we would use" is the exact condition the
    whole port chain exists to survive, and on the machine this was written
    for it is a legacy container. So the answer must carry the service name
    :data:`model_router.config.SERVICE_NAME`, which is the same constant
    ``/health`` writes.

    Proxy handlers are stripped from the opener deliberately: with
    ``http_proxy`` set in the environment, ``urlopen`` would send a probe for
    a loopback address to a proxy and get somebody else's answer.
    """
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(_health_url(host, port), timeout=timeout_s) as response:
            if getattr(response, "status", None) != 200:
                return False
            payload = json.loads(response.read(_HEALTH_BODY_LIMIT_BYTES))
    except (OSError, ValueError):
        return False
    from .config import SERVICE_NAME

    return isinstance(payload, dict) and payload.get("service") == SERVICE_NAME


def _probe_running_gateway(
    host: str,
    ports: "Sequence[int]",
    *,
    deadline_s: Optional[float] = None,
) -> Optional[int]:
    """The port one of OUR gateways answers on, or ``None``.

    Every candidate is tried on every round rather than one port per round:
    the port file and the last-port record can name different ports (a start
    that fell back rewrites both, a start that crashed leaves the older
    record behind), and the running daemon is on exactly one of them.

    **Residual race, and its bound.** Two starts inside the same window can
    both conclude "nobody is serving": A claims the pid file and is still
    importing when B's deadline expires, so B takes the file over and binds
    too. The outcome is ONE extra instance, never a corrupted state — each
    binds its own port through :func:`_bind_with_fallback`, which cannot
    steal a port another process holds on any platform (see
    :func:`_bind_socket`); the port files are last-writer-wins DATA, and
    every reader of them health-probes before trusting them, so the loser
    degrades to a stale record that fails a probe. Closing the race properly
    needs an OS lock this file deliberately does not take (no ``flock``, no
    ``fork`` — see the module docstring), and paying a cross-OS locking
    primitive to save one duplicate process in a three-second window nothing
    else opens would be the worse trade.
    """
    budget = _HEALTH_PROBE_DEADLINE_S if deadline_s is None else deadline_s
    deadline = time.monotonic() + budget
    while True:
        for port in ports:
            if _gateway_answers(host, port, _HEALTH_PROBE_TIMEOUT_S):
                return port
        if time.monotonic() >= deadline:
            return None
        time.sleep(_HEALTH_PROBE_INTERVAL_S)


#: Boot log ceiling and how much of the tail survives a rotation. The file is
#: append-only and nothing else rotates it: an init system opens it once and
#: keeps writing, so a daemon that refuses to start in a restart loop grows it
#: without bound (2026-09-08: 294 KB of one repeated refusal in four hours,
#: and only because the loop was noticed).
#:
#: Rotation happens at START, which bounds the file ACROSS starts. Within one
#: long-lived start it is bounded instead by what reaches stderr at all —
#: warnings and errors only, see :func:`_configure_logging`. Both are needed:
#: rotating alone would leave a healthy daemon appending an access line per
#: request until its next restart, and raising the level alone would leave a
#: restart loop appending its refusal for as long as the loop runs.
_BOOT_LOG_MAX_BYTES = 512 * 1024
_BOOT_LOG_KEEP_LINES = 500


def _rotate_boot_log() -> None:
    """Trim the init system's capture file. Best-effort, never fatal.

    ``in_place=True`` is required, not a preference: the init system holds
    this very file open in append mode and passed us the descriptor, so a
    rotation that replaced the path would leave every later line — including
    the ones explaining why THIS start failed — in an unlinked inode.
    """
    from vco_lib.atomic import rotate_tail_lines
    from vco_lib.boot_service import boot_log_file

    from .config import log_path

    rotate_tail_lines(
        boot_log_file(log_path()),
        max_bytes=_BOOT_LOG_MAX_BYTES,
        keep_lines=_BOOT_LOG_KEEP_LINES,
        in_place=True,
    )


def _release_single_instance(pid_path: Path) -> None:
    """Remove the pid file only if it still names THIS process."""
    try:
        recorded = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if recorded == str(os.getpid()):
        try:
            pid_path.unlink()
        except OSError:
            pass


def _serve(port_override: Optional[int]) -> int:
    from aiohttp import web

    from .auth import ensure_host_token
    from .config import (
        FALLBACK_PORT_RANGE,
        GatewayConfig,
        HostNotLoopbackError,
        explicit_port,
        last_port_path,
        log_path,
        pid_path,
        port_candidates,
        port_path,
        token_path,
    )
    from .fileperms import PermissionHardeningError, owner_only_state
    from .server import create_app

    # Before the first line is written, not after: this start may be one of
    # many in a restart loop, and the point is that the file the loop writes
    # to stays bounded.
    _rotate_boot_log()
    log_warning = _configure_logging(log_path())
    if log_warning:
        print(f"vct-model-gateway: {log_warning}", file=sys.stderr)

    if port_override is not None:
        os.environ["VCT_MODEL_GATEWAY_PORT"] = str(port_override)

    try:
        token = ensure_host_token(token_path())
    except PermissionHardeningError as exc:
        print(f"vct-model-gateway: refusing to start — {exc}", file=sys.stderr)
        return 1

    try:
        config = GatewayConfig.from_env(token=token)
    except HostNotLoopbackError as exc:
        print(f"vct-model-gateway: refusing to start — {exc}", file=sys.stderr)
        return 1

    holder = _acquire_single_instance(pid_path())
    if holder is not None:
        # "Someone else holds the pid file" is a QUESTION, not an answer, and
        # answering it "failure" cost four hours on 2026-09-08: a hand-started
        # gateway was already serving, the login unit exited 1, and
        # `Restart=on-failure` with `RestartSec=10s` respun it 1442 times —
        # never tripping systemd's default start limit, which needs 5 starts
        # inside 10 seconds. An already-running service is SUCCESS.
        from vco_lib.deferral_probes import pid_is_alive

        candidates = port_candidates()
        # Conservative: an indeterminate liveness answer counts as ALIVE, so
        # the probe below runs rather than a live daemon being evicted.
        if holder != UNREADABLE_PID and pid_is_alive(holder):
            running = _probe_running_gateway(config.host, candidates)
            if running is not None:
                logger.info(
                    "model-gateway: already running on %s:%d (pid %d); "
                    "nothing to do", config.host, running, holder,
                )
                print(
                    f"vct-model-gateway: already running on {config.host}:"
                    f"{running} (pid {holder}); nothing to do",
                )
                return 0
            reason = (
                f"names live process {holder}, but no gateway answered on "
                f"{config.host} ports "
                + ", ".join(str(port) for port in candidates)
            )
        else:
            reason = (
                f"names {holder}, which is not a running process"
                if holder != UNREADABLE_PID
                else "holds no readable process id"
            )
        # Not a gateway: a reused number, a start that died, or a damaged
        # file. Take it over rather than telling the user to delete it — a
        # printed command must never act on a condition we have not
        # confirmed, and "delete this file" was being printed for a healthy
        # daemon too.
        logger.warning(
            "model-gateway: %s %s; taking the pid file over",
            pid_path(), reason,
        )
        print(
            f"vct-model-gateway: {pid_path()} {reason}. Continuing and "
            "claiming the file.",
            file=sys.stderr,
        )
        try:
            pid_path().unlink()
        except OSError:
            pass
        # ONE retry, never a loop: losing this one means another start
        # created the file in the instant between the unlink and the create,
        # so that start is coming up right now and this one has nothing left
        # to do. Exiting 0 is what keeps `Restart=on-failure` from turning a
        # lost race into the spin this whole path exists to end.
        contender = _acquire_single_instance(pid_path())
        if contender is not None:
            logger.info(
                "model-gateway: another start claimed %s (pid %s) while this "
                "one was reclaiming it; leaving it to that instance",
                pid_path(), contender,
            )
            print(
                f"vct-model-gateway: another start claimed {pid_path()} "
                f"(pid {contender}) first; nothing to do",
            )
            return 0

    pinned = explicit_port()
    try:
        socks, port = _bind_with_fallback(config.host, config.port, pinned is not None)
    except OSError as exc:
        logger.error(
            "model-gateway: cannot bind %s:%d (%s)", config.host, config.port, exc,
        )
        print(
            f"vct-model-gateway: cannot bind {config.host}:{config.port} ({exc}). "
            + (
                "That port was requested explicitly, so no other was tried; "
                "unset VCT_MODEL_GATEWAY_PORT (or drop --port) to let the "
                f"daemon fall back to {FALLBACK_PORT_RANGE.start}-"
                f"{FALLBACK_PORT_RANGE.stop - 1}."
                if pinned is not None
                else "Every fallback port is taken too; free one, or set "
                "VCT_MODEL_GATEWAY_PORT to choose explicitly."
            ),
            file=sys.stderr,
        )
        _release_single_instance(pid_path())
        return 1

    if port != config.port:
        logger.info(
            "model-gateway: %d is taken; bound %d from the fallback range",
            config.port, port,
        )
        config = replace(config, port=port)

    # Everything past the bind runs INSIDE the try, so the cleanup below is
    # reached however it ends. It used to start after the port-file writes
    # and the app build: a failure in either — a read-only state dir, a
    # broken vendor registry — left this function by an exception with the
    # sockets still listening and the pid file still naming a process that
    # was on its way out, which is precisely the stale claim that blocks the
    # next start.
    try:
        app = create_app(config, token_permissions=owner_only_state(token_path()))
        # AFTER the bind, never before: a port file naming a port we did not
        # get is what sends every reader at somebody else's service.
        _write_owner_only(port_path(), f"{port}\n", strict=False)
        _write_owner_only(last_port_path(), f"{port}\n", strict=False)
        logger.info(
            "model-gateway: listening on %s (token: %s)",
            ", ".join(
                _health_url(str(s.getsockname()[0]), port).removesuffix("/health")
                for s in socks
            ),
            token_path(),
        )
        # ``access_log=None``: aiohttp's own access logger would write a
        # SECOND line per request, at INFO, into the same file handler — and
        # the gateway's access line is deliberately ONE line in ONE shape
        # (see the logging policy in ``model_router.server``). Two lines per
        # request is not extra detail, it is a log nobody can grep.
        web.run_app(app, sock=socks, print=None, access_log=None)
        return 0
    finally:
        # ``run_app`` takes ownership of the sockets and closes them on
        # shutdown; closing an already-closed socket is a no-op. Doing it
        # here as well means no listener can outlive this function on ANY
        # path — including one where run_app raised before adopting them.
        for sock in socks:
            sock.close()
        # The PID file is liveness and must go: a stale one names a process
        # that is not us and blocks the next start.
        _release_single_instance(pid_path())
        # The PORT file STAYS. It answers "where did this gateway last run?",
        # not "is it running?" — and readers already resolve liveness by
        # probing /health. Deleting it on exit made a clean stop
        # indistinguishable from never having run: after a SIGTERM (boot
        # service stop, reboot) a gateway that had fallen back to a non-default
        # port was forgotten, every resolver dropped to the compiled-in
        # default, and on a machine where something ELSE listens there —
        # a legacy container on the historical port is the case that bit —
        # the client silently talks to the wrong service. A stale port file
        # that fails a health probe is a recoverable miss; a missing one is a
        # confident wrong answer.


def run_check(stream=sys.stdout) -> int:
    """Configuration self-test. Exit 0 when the daemon could serve.

    Deliberately does NOT resolve a vendor key or contact the hub: resolution
    is lazy at request time so the daemon starts on a machine whose hub is
    down, and a self-test that broke that property would be testing something
    the daemon does not do.
    """
    from .catalog import STATIC_CATALOG_PATH
    from .config import (
        HostNotLoopbackError,
        credentials_path,
        export_path,
        log_path,
        pid_path,
        port_path,
        resolve_host,
        resolve_port,
        token_path,
    )
    from .context_table import ContextTableLoader, load_seed
    from .vendors import VENDORS, RegistryError, validate_registry

    problems: list[str] = []

    try:
        validate_registry()
        vendor_line = ", ".join(sorted(VENDORS)) or "(none)"
    except RegistryError as exc:
        problems.append(f"vendor registry: {exc}")
        vendor_line = "INVALID"

    try:
        host = resolve_host()
    except HostNotLoopbackError as exc:
        problems.append(str(exc))
        host = "INVALID"
    port = resolve_port()

    seed = load_seed()
    if not seed.rows:
        problems.append(
            "the shipped chat-model context seed has no usable rows "
            "(expected it beside the package)",
        )
    table = ContextTableLoader(export_path()).current()

    try:
        static_families = json.loads(
            STATIC_CATALOG_PATH.read_text(encoding="utf-8"),
        )["families"]
        static_line = ", ".join(
            f"{k}={len(v.get('models') or [])}" for k, v in sorted(static_families.items())
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        problems.append(f"static catalog {STATIC_CATALOG_PATH} unreadable: {exc}")
        static_line = "INVALID"

    creds = credentials_path()
    creds_line = "present" if creds.exists() else "absent (a Claude login will be needed)"

    report = {
        "host": host,
        "port": port,
        "vendors": vendor_line,
        "token_file": str(token_path()),
        "pid_file": str(pid_path()),
        "port_file": str(port_path()),
        "log_file": str(log_path()),
        "credentials_file": f"{creds} ({creds_line})",
        "context_table": f"{table.source} ({len(table.rows)} rows)",
        "context_export_path": str(export_path()),
        "static_catalog": static_line,
        "vendor_keys": "not resolved (lazy at request time, by design)",
    }
    width = max(len(k) for k in report)
    for key, value in report.items():
        print(f"{key.ljust(width)}  {value}", file=stream)
    if problems:
        print("", file=stream)
        for problem in problems:
            print(f"PROBLEM: {problem}", file=stream)
        return 1
    print("\nOK — the gateway can serve with this configuration.", file=stream)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vct-model-gateway",
        description=(
            "Local Anthropic-shaped gateway serving the user's Claude login "
            "and prefixed vendor namespaces in one model picker."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=("serve",),
        help="what to do (default: serve)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "TCP port. Default: VCT_MODEL_GATEWAY_PORT, then the running "
            "daemon's port file, then the last-port record (where a gateway "
            "was last STARTED, which outlives the daemon), then 11436. A "
            "port given here is a PIN and is never moved to a fallback."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the configuration self-test and exit 0/1 without serving",
    )
    parser.add_argument(
        "--print-token-path",
        action="store_true",
        help="print the host-token file path and exit (the path, never the token)",
    )
    parser.add_argument(
        "--print-export-path",
        action="store_true",
        help="print the chat-model context export path and exit",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the gateway version and exit",
    )
    boot = parser.add_argument_group(
        "boot autostart",
        "Opt-in login-time start. Never registered by an install: the "
        "gateway proxies under your Claude login, so making it a daemon is "
        "your decision. Mirrors `vct-hub`'s flags of the same names.",
    )
    boot.add_argument(
        "--register-boot",
        action="store_true",
        help=(
            "register the gateway to start at login and start it now "
            "(exit 0 registered, 1 failed)"
        ),
    )
    boot.add_argument(
        "--unregister-boot",
        action="store_true",
        help="remove the login-time registration; idempotent (exit 0/1)",
    )
    boot.add_argument(
        "--boot-status",
        action="store_true",
        help=(
            "print enabled / disabled / not-installed and exit 0 / 1 / 2 "
            "(3 on an inspection error)"
        ),
    )
    return parser


def _boot_spec():
    """Build the gateway's boot spec from THIS process's own resolution.

    The paths are read from :mod:`model_router.config` rather than
    recomputed, so the unit points at the same state root, log file and
    port file the daemon itself will use — including a redirected
    ``VCT_STATE_DIR``. Everything is resolved now and baked into the unit,
    which is why a moved clone needs the re-render ``install.py --update``
    performs.
    """
    import platform

    from vco_lib import boot_service
    from vco_lib.paths import vct_root_dir

    from .config import log_path

    return boot_service.model_gateway_spec(
        os_key=platform.system(),
        log_file=log_path(),
        state_dir=vct_root_dir(),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.version:
        from . import __version__

        print(__version__)
        return 0
    if args.print_token_path:
        from .config import token_path

        print(token_path())
        return 0
    if args.print_export_path:
        from .config import export_path

        print(export_path())
        return 0
    if args.register_boot:
        from vco_lib import boot_service

        return boot_service.run_register_boot(_boot_spec())
    if args.unregister_boot:
        from vco_lib import boot_service

        return boot_service.run_unregister_boot(_boot_spec())
    if args.boot_status:
        from vco_lib import boot_service

        return boot_service.run_boot_status(_boot_spec())
    if args.check:
        return run_check()
    if args.port is not None and not (1 <= args.port <= 65535):
        print(
            f"vct-model-gateway: --port {args.port} is out of range (1-65535)",
            file=sys.stderr,
        )
        return 2
    return _serve(args.port)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
