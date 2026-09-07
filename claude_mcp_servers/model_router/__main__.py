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
``os.getuid``. Single-instance is a pid file plus the shared cross-OS liveness
probe (``os.kill(pid, 0)`` on POSIX, ``OpenProcess`` on Windows — the probe
knows that ``os.kill`` on Windows TERMINATES rather than probes). Paths are
built with ``pathlib``, never string-joined.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger("model_router")

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _configure_logging(log_file: Optional[Path]) -> Optional[str]:
    """stderr always; a file when one can be opened. Returns a warning or None.

    The file handler is best-effort by design: a gateway that refuses to start
    because it could not open a log file would be worse than one that logs to
    stderr only. Failing to open it is reported once, on stderr, naming the
    path — not swallowed.
    """
    from vco_lib.log_setup import configure_logging

    configure_logging(logging.INFO, format=_LOG_FORMAT, stream=sys.stderr)
    if log_file is None:
        return None
    from .fileperms import PermissionHardeningError, restrict_to_owner

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file, encoding="utf-8")
    except OSError as exc:
        return f"could not open log file {log_file} ({exc}); logging to stderr only"
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logging.getLogger().addHandler(handler)
    try:
        restrict_to_owner(log_file)
    except PermissionHardeningError as exc:
        return (
            f"log file {log_file} could not be made owner-only ({exc}); it may "
            "be readable by other local users"
        )
    return None


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
    from .fileperms import PermissionHardeningError, restrict_to_owner

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
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


def _acquire_single_instance(pid_path: Path) -> Optional[str]:
    """Claim the pid file, or return a message explaining who holds it.

    The liveness probe is conservative: an indeterminate answer counts as
    ALIVE, so the gateway declines to start rather than race a process it
    cannot see. The message names the file, because a stale pid file whose
    number has since been reused by an unrelated process is the one case a
    user must resolve by hand.
    """
    from vco_lib.deferral_probes import pid_is_alive

    try:
        existing = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        try:
            other = int(existing)
        except ValueError:
            other = -1
        if other > 0 and other != os.getpid() and pid_is_alive(other):
            return (
                f"another model gateway appears to be running (pid {other}, "
                f"recorded in {pid_path}). Stop it first, or delete that file "
                "if you are certain the process is gone."
            )
    _write_owner_only(pid_path, f"{os.getpid()}\n", strict=False)
    return None


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
        GatewayConfig,
        HostNotLoopbackError,
        log_path,
        pid_path,
        port_path,
        token_path,
    )
    from .fileperms import PermissionHardeningError, owner_only_state
    from .server import create_app

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

    held = _acquire_single_instance(pid_path())
    if held:
        print(f"vct-model-gateway: {held}", file=sys.stderr)
        return 1

    app = create_app(config, token_permissions=owner_only_state(token_path()))
    _write_owner_only(port_path(), f"{config.port}\n", strict=False)
    try:
        logger.info(
            "model-gateway: listening on http://%s:%d (token: %s)",
            config.host, config.port, token_path(),
        )
        web.run_app(app, host=config.host, port=config.port, print=None)
        return 0
    except OSError as exc:
        logger.error(
            "model-gateway: cannot bind %s:%d (%s)", config.host, config.port, exc,
        )
        print(
            f"vct-model-gateway: cannot bind {config.host}:{config.port} ({exc}). "
            "Another process may hold the port; set VCT_MODEL_GATEWAY_PORT to "
            "choose a different one.",
            file=sys.stderr,
        )
        return 1
    finally:
        _release_single_instance(pid_path())
        try:
            if port_path().read_text(encoding="utf-8").strip() == str(config.port):
                port_path().unlink()
        except OSError:
            pass


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
        help="TCP port (default: VCT_MODEL_GATEWAY_PORT, then the port file, then 11436)",
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
