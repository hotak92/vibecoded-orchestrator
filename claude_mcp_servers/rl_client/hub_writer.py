# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""HTTP client for posting RL events to vct-hub (v0.2.47 RL-5).

Replaces the JSONL append path. Free-tier and Pro use the SAME client;
the hub binary is part of the orchestrator install and runs alongside
the launcher GUI (and survives the GUI being closed).

Soft-fail discipline (locked decision 2026-06-04):
    - Connect-refused, timeout, missing token, non-2xx all return False.
    - Caller does NOT raise; treats False as "event lost" and continues.
    - No retry queue. No JSONL fallback (the JSONL path is dead going
      forward; historical events come over via the one-shot migration
      script in vco_lib/migrate_rl_jsonl_to_db.py).

Discovery:
    - Hub port: ``$VCT_HUB_PORT`` -> ``<vct_root>/hub.port`` -> 7700.
    - Hub token: ``<vct_root>/hub.token`` (regenerated at every hub startup,
      mode 0o600). Read at every call — no in-process cache (the token
      rotates and we don't want stale-token surprises in a long-lived
      MCP subprocess that survives a launcher restart).
    - vct_root: ``$VCT_STATE_DIR`` -> ``~/.vct``.

Endpoint:
    POST http://127.0.0.1:<port>/api/v1/rl/events
        Authorization: Bearer <token>
        Content-Type: application/json
        Body: see ``RlEventBody`` (matches the hub-side handler).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 7700
_DEFAULT_TIMEOUT_S = 2.0


def _vct_root_dir() -> Path:
    """Resolve <vct_root>: $VCT_STATE_DIR -> ~/.vct."""
    env = os.environ.get("VCT_STATE_DIR")
    return Path(env) if env else Path.home() / ".vct"


def _read_hub_port() -> int:
    """Resolve hub port: $VCT_HUB_PORT -> <vct_root>/hub.port -> default 7700.

    Any read error falls through to the default. The hub-startup code
    writes the file on every successful bind so the file presence and
    its content reflect the actual listening port.
    """
    env = os.environ.get("VCT_HUB_PORT")
    if env:
        try:
            return int(env.strip())
        except (ValueError, AttributeError):
            pass
    port_file = _vct_root_dir() / "hub.port"
    try:
        return int(port_file.read_text().strip())
    except (OSError, ValueError):
        return _DEFAULT_PORT


def _read_hub_token() -> str | None:
    """Read the hub bearer token from <vct_root>/hub.token.

    Returns None when the file is missing or unreadable. Caller treats
    None as "hub not running" and skips the POST entirely (soft-fail).

    The token rotates on every hub startup, so callers MUST NOT cache
    the result across requests — a single MCP subprocess can outlive
    multiple hub restarts.
    """
    p = _vct_root_dir() / "hub.token"
    try:
        return p.read_text().strip()
    except OSError:
        return None


def post_rl_event(event: dict[str, Any], timeout: float = _DEFAULT_TIMEOUT_S) -> bool:
    """POST one v3 RL event to the hub's ``/api/v1/rl/events`` route.

    Returns True on HTTP 2xx, False on any error (missing token, connect
    refused, timeout, non-2xx response). Never raises.

    Body schema (matches the hub's ``rl_events_api::PostEventBody``):

        {
            "event_type":       "retrieval" | "citation",
            "schema_version":   int,
            "ts_ms":            int (unix epoch ms; writer-side time),
            "project_id":       str | null,
            "project_name":     str | null,
            "task_id":          str (required),
            "task_type":        str | null,
            "embedding_source": str | null,
            "embedding_dim":    int | null,
            "embedding_model":  str | null,
            "payload_json":     str (the full v3 event JSON, verbatim)
        }

    The hub validates ``event_type`` is one of {retrieval, citation} and
    requires non-empty ``task_id`` + ``payload_json``. Caller is responsible
    for providing those — a 400 from the hub is a writer bug, not a
    soft-fail-recoverable condition.

    Args:
        event: A dict matching the body schema above.
        timeout: Per-request timeout in seconds. Default 2.0. The hub
            INSERT is microsecond-fast; long timeouts here would only
            mask a wedged hub.

    Returns:
        True if the hub returned 2xx; False otherwise (including the
        "hub not running" case where no token file exists).
    """
    token = _read_hub_token()
    if token is None:
        logger.debug("rl_events POST skipped: no hub.token (hub not running?)")
        return False

    port = _read_hub_port()
    url = f"http://127.0.0.1:{port}/api/v1/rl/events"

    try:
        body = json.dumps(event).encode("utf-8")
    except (TypeError, ValueError) as exc:
        # Caller bug: event isn't JSON-serializable. Surface in debug
        # but DON'T raise — we still return False so the caller's flow
        # is uniform with the network-failure case.
        logger.debug("rl_events POST skipped: not JSON-serializable (%s)", exc)
        return False

    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        # The hub returned a structured 4xx/5xx. Log the status code so a
        # writer bug (e.g. unknown event_type) is debuggable; the body
        # has the error envelope but reading it here defeats the no-raise
        # contract on closed connections, and the soft-fail caller doesn't
        # need it.
        logger.debug("rl_events POST returned HTTP %s", e.code)
        return False
    except urllib.error.URLError as e:
        # Connect-refused / DNS / TLS / etc. Most common: hub not running.
        logger.debug("rl_events POST URL error: %s", e.reason)
        return False
    except (OSError, TimeoutError) as exc:
        logger.debug("rl_events POST failed: %s", exc)
        return False


__all__ = ["post_rl_event"]
