# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Read the model gateway's subscription usage windows — the launcher's bridge.

The gateway computes and caches the windows (``model_router.usage_windows``,
served on ``GET /usage/windows``). This module is the one client the launcher
spawns for them (``python -m vco_lib.gateway_usage``), for the reason every
gateway call from the launcher goes through Python: the host token must never
cross the Rust process (``launcher/src-tauri/src/commands/model_gateway.rs``,
"The host token never crosses this process"). The token is read here from the
gateway's own file, put in one request header, and never printed.

Port and token resolution are NOT re-implemented: both come from
:mod:`vco_lib.vscode_settings` (``resolve_gateway_ports`` /
``resolve_host_token``), the same answer the panel wiring uses.

stdout is a machine contract: exactly one JSON object —
``{"ok": true, "snapshot": {...}}`` or ``{"ok": false, "reason": <word>,
"message": <text>}`` — and the exit code is 0 either way, because "the
gateway is not running" is an answer the card renders, not a crash.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any, Optional, Sequence

#: The gateway route. MUST MATCH ``create_app`` in
#: ``claude_mcp_servers/model_router/server.py``.
ROUTE = "/usage/windows"

#: Seconds. Loopback, answered from the gateway's memory — generous only so a
#: loaded machine does not read as "unreachable".
DEFAULT_TIMEOUT_S = 3.0

REASON_NO_TOKEN = "no_token"
REASON_UNREACHABLE = "unreachable"
REASON_UNAUTHORISED = "unauthorised"
REASON_BAD_ANSWER = "bad_answer"
REASON_OUTDATED = "outdated_gateway"


def _failure(reason: str, message: str) -> dict:
    return {"ok": False, "reason": reason, "message": message}


def fetch_windows(*, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Ask the running gateway for its usage snapshot. Never raises."""
    # Imported here so `--help` works on a machine whose gateway package is
    # not importable, and so the failure is reported as data.
    from vco_lib.vscode_settings import (
        SettingsRefused,
        resolve_gateway_ports,
        resolve_host_token,
    )

    try:
        token = resolve_host_token()
    except (SettingsRefused, ImportError) as exc:
        return _failure(REASON_NO_TOKEN, str(exc))
    if not token:
        return _failure(REASON_NO_TOKEN, "the gateway's host token file is empty")
    port = resolve_gateway_ports()[0]
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{ROUTE}",
        headers={"Authorization": f"Bearer {token}"},
    )
    # A loopback call must not be routed through a user's HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout_s) as resp:  # noqa: S310 — loopback only
            payload: Any = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return _failure(
                REASON_UNAUTHORISED,
                f"the gateway on port {port} refused the host token (HTTP 401)",
            )
        if exc.code == 404:
            # The route arrived in 0.2.97; a gateway started before the
            # update is still serving the previous release's routes.
            return _failure(
                REASON_OUTDATED,
                f"the gateway on port {port} has no {ROUTE} route — it is "
                "running an older version; restart it to load the updated one",
            )
        return _failure(REASON_BAD_ANSWER, f"the gateway on port {port} answered HTTP {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        return _failure(REASON_UNREACHABLE, f"no gateway answered on port {port}: {exc}")
    except ValueError:
        return _failure(REASON_BAD_ANSWER, f"the gateway on port {port} did not answer JSON")
    if not isinstance(payload, dict) or not isinstance(payload.get("vendors"), list):
        return _failure(
            REASON_BAD_ANSWER,
            f"the service on port {port} answered without a usage snapshot "
            "(an older gateway, or not a gateway)",
        )
    return {"ok": True, "port": port, "snapshot": payload}


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.gateway_usage",
        description="Print the model gateway's subscription usage windows as JSON.",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S,
        help=f"seconds to wait for the gateway (default {DEFAULT_TIMEOUT_S})",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    sys.stdout.write(json.dumps(fetch_windows(timeout_s=args.timeout)) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
