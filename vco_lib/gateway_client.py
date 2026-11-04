# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""ONE home for "GET a JSON route of the running model gateway" (v0.2.105).

Extracted from :mod:`vco_lib.gateway_usage` when a second caller appeared
(:mod:`vco_lib.gateway_preflight`, the pre-flight vendor-key probe), so the
port/token resolution, the proxy-free loopback opener and the failure
vocabulary exist once.

The host token is read from the gateway's own file (through
:func:`vco_lib.vscode_settings.resolve_host_token`), put in one request header,
and never printed. Port resolution is
:func:`vco_lib.vscode_settings.resolve_gateway_ports` — the same answer the
panel wiring uses.

:func:`get_json` never raises. Its answer is ``{"ok": True, "port": <int>,
"payload": <decoded JSON>}`` or ``{"ok": False, "reason": <word>, "message":
<text>}`` with one of the ``REASON_*`` words below.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional

REASON_NO_TOKEN = "no_token"
REASON_UNREACHABLE = "unreachable"
REASON_UNAUTHORISED = "unauthorised"
REASON_BAD_ANSWER = "bad_answer"
REASON_OUTDATED = "outdated_gateway"
#: ``vco_lib`` / ``model_router`` did not import — a broken install.
REASON_BROKEN_INSTALL = "broken_install"

REINSTALL_HINT = (
    "This is a broken install, not a gateway state — re-run "
    "`python install.py --update` from the orchestrator root."
)


def failure(reason: str, message: str) -> dict:
    return {"ok": False, "reason": reason, "message": message}


def get_json(
    route: str,
    *,
    timeout_s: float,
    query: Optional[Mapping[str, str]] = None,
) -> dict:
    """GET ``route`` from the gateway on loopback with the host token.

    A 404 is :data:`REASON_OUTDATED`: every caller asks for a route that
    exists in the release it ships with, so a 404 means a daemon started
    before the update is still serving the previous release's routes.
    """
    # Imported here so a caller's `--help` works on a broken install, and so
    # the failure is a NAMED reason instead of a traceback. `resolve_host_token`
    # imports `model_router.config` itself, so the same reason covers the
    # gateway package going missing.
    try:
        from vco_lib.vscode_settings import (
            SettingsRefused,
            resolve_gateway_ports,
            resolve_host_token,
        )
    except ImportError as exc:
        return failure(
            REASON_BROKEN_INSTALL,
            f"vco_lib.vscode_settings could not be imported ({exc}). {REINSTALL_HINT}",
        )
    try:
        token = resolve_host_token()
        port = resolve_gateway_ports()[0]
    except ImportError as exc:
        return failure(
            REASON_BROKEN_INSTALL,
            f"the model gateway package could not be imported ({exc}). {REINSTALL_HINT}",
        )
    except SettingsRefused as exc:
        return failure(REASON_NO_TOKEN, str(exc))
    if not token:
        return failure(REASON_NO_TOKEN, "the gateway's host token file is empty")
    url = f"http://127.0.0.1:{port}{route}"
    if query:
        url += "?" + urllib.parse.urlencode(dict(query))
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    # A loopback call must not be routed through a user's HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout_s) as resp:  # noqa: S310 — loopback only
            payload: Any = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return failure(
                REASON_UNAUTHORISED,
                f"the gateway on port {port} refused the host token (HTTP 401)",
            )
        if exc.code == 404:
            return failure(
                REASON_OUTDATED,
                f"the gateway on port {port} has no {route} route — it is "
                "running an older version; restart it to load the updated one",
            )
        return failure(REASON_BAD_ANSWER, f"the gateway on port {port} answered HTTP {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        return failure(REASON_UNREACHABLE, f"no gateway answered on port {port}: {exc}")
    except ValueError:
        return failure(REASON_BAD_ANSWER, f"the gateway on port {port} did not answer JSON")
    return {"ok": True, "port": port, "payload": payload}


__all__ = [
    "REASON_BAD_ANSWER",
    "REASON_BROKEN_INSTALL",
    "REASON_NO_TOKEN",
    "REASON_OUTDATED",
    "REASON_UNAUTHORISED",
    "REASON_UNREACHABLE",
    "REINSTALL_HINT",
    "failure",
    "get_json",
]
