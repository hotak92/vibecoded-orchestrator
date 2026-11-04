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

Port and token resolution and the request itself are NOT re-implemented here:
they live in :mod:`vco_lib.gateway_client` (``get_json``), which reads both from
:mod:`vco_lib.vscode_settings` — the same answer the panel wiring uses.

stdout is a machine contract: exactly one JSON object —
``{"ok": true, "snapshot": {...}}`` or ``{"ok": false, "reason": <word>,
"message": <text>}``. The exit code is 0 for every answer about the GATEWAY
(not running, refused, outdated): those are states the card renders, not
crashes. It is 1 for :data:`REASON_BROKEN_INSTALL` — ``vco_lib`` or the
gateway package failed to import, which a healthy install never does — and
the message goes to stderr as well, so the breakage reaches whoever ran it
(CLAUDE.md: a failed import of a shipped dependency is a broken install,
surfaced loudly, never degraded into a quieter answer).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from vco_lib.gateway_client import (
    REASON_BAD_ANSWER,
    REASON_BROKEN_INSTALL,
    REASON_NO_TOKEN,
    REASON_OUTDATED,
    REASON_UNAUTHORISED,
    REASON_UNREACHABLE,
    get_json,
)
from vco_lib.gateway_client import failure as _failure

#: The gateway route. MUST MATCH ``create_app`` in
#: ``claude_mcp_servers/model_router/server.py``.
ROUTE = "/usage/windows"

#: Seconds. Loopback, answered from the gateway's memory — generous only so a
#: loaded machine does not read as "unreachable".
DEFAULT_TIMEOUT_S = 3.0

#: The failure vocabulary lives in :mod:`vco_lib.gateway_client` (v0.2.105:
#: the request itself moved there when the pre-flight probe became its second
#: caller); re-exported because the launcher card branches on these words.
__all__ = [
    "DEFAULT_TIMEOUT_S",
    "REASON_BAD_ANSWER",
    "REASON_BROKEN_INSTALL",
    "REASON_NO_TOKEN",
    "REASON_OUTDATED",
    "REASON_UNAUTHORISED",
    "REASON_UNREACHABLE",
    "ROUTE",
    "fetch_windows",
    "main",
]


def fetch_windows(*, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Ask the running gateway for its usage snapshot. Never raises."""
    answer = get_json(ROUTE, timeout_s=timeout_s)
    if not answer.get("ok"):
        return answer
    port = answer["port"]
    payload = answer["payload"]
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
    result = fetch_windows(timeout_s=args.timeout)
    sys.stdout.write(json.dumps(result) + "\n")
    if result.get("reason") == REASON_BROKEN_INSTALL:
        sys.stderr.write(f"vco_lib.gateway_usage: {result['message']}\n")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
