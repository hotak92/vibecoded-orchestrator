# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""KG access matrix gate client (v0.2.49 Phase 8 / item #21+#22).

Python counterpart of ``templates/scripts/vct_access_check.sh``. Used
by the MCP server (`store_knowledge_node`, RL telemetry writers) and
Python-side hooks (`sync_knowledge_graph.py`, etc.) to enforce the
``kg_collection_access`` matrix at write time.

The matrix has been a read-gate only since v0.2.21 — env-routed reads
respect the access list (`VCT_KG_ACCESS_LIST` CSV in `.claude/env`) but
writes flow through `WEAVIATE_URL` blindly. This module closes the
asymmetry by consulting the hub's
``GET /api/v1/projects/{id}/access/{collection}`` endpoint at write
time.

Fail-open contract
~~~~~~~~~~~~~~~~~~

Hub unreachable, auth-failed, 404, malformed response, timeout → return
``"write"`` (the most-permissive level). This is DELIBERATE: a closed-
circuit policy would brick all KG writes during a launcher restart,
unacceptable UX. Every fail-open emission:

1. Logs a WARNING via ``logging`` (rate-limited; one per process per
   reason-key per 5 min).
2. Appends a row to ``$VCT_STATE_DIR/cache/dropped_writes.jsonl`` so
   the dropped-write metric is observable.

Public API
~~~~~~~~~~

.. code-block:: python

    from vco_lib.access_resolver import check_access_level

    level = check_access_level("p1", "MyProject_KnowledgeGraph")
    # level ∈ {"read", "write", "none"} — fail-open returns "write".

    # Caller gates the write on the literal value:
    if level != "write":
        logger.warning(...)
        return

Mirrors the bash client's contract byte-for-byte except this module:
- Uses Python's :mod:`logging` for the WARNING instead of stderr ``printf``.
- Returns the level string instead of printing it.
- Never raises (the fail-open contract).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger("vco.access_resolver")

# Resolver protocol version — must stay in lock-step with bash sibling.
RESOLVER_PROTOCOL_VERSION = 1

# In-process rate-limit state: maps reason → last-emitted-ts (float seconds).
_WARN_STATE: dict[str, float] = {}
_WARN_WINDOW_SECONDS = 300.0  # 5 min, mirrors bash sibling


def _state_dir() -> Path:
    """Resolve $VCT_STATE_DIR or default ~/.vct."""
    s = os.environ.get("VCT_STATE_DIR")
    if s:
        return Path(s)
    return Path.home() / ".vct"


def _hub_port() -> int:
    """Hub port discovery: env > state file > default."""
    p = os.environ.get("VCT_HUB_PORT")
    if p:
        try:
            return int(p)
        except ValueError:
            pass
    port_file = _state_dir() / "hub.port"
    if port_file.is_file():
        try:
            return int(port_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pass
    return 7700


def _hub_token() -> Optional[str]:
    """Hub token discovery: env > state file > None."""
    t = os.environ.get("VCT_HUB_TOKEN")
    if t:
        return t
    token_file = _state_dir() / "hub.token"
    if token_file.is_file():
        try:
            return token_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def _emit_metric(project_id: str, collection: str, reason: str) -> None:
    """Append a dropped-write row to the metric JSONL. Never raises."""
    try:
        cache_dir = _state_dir() / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        jsonl = cache_dir / "dropped_writes.jsonl"
        row = {
            "ts": int(time.time()),
            "project_id": project_id,
            "collection": collection,
            "reason": reason,
            "fail_open": True,
        }
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        # Logging failure must not break the fail-open contract.
        pass


def _emit_warning(reason: str) -> None:
    """Rate-limited WARNING — one per reason-key per 5-min window per process.

    Bypass via ``VCO_HOOK_DEBUG=1`` (emits every occurrence).
    """
    now = time.time()
    bypass = os.environ.get("VCO_HOOK_DEBUG") == "1"
    if not bypass:
        last = _WARN_STATE.get(reason)
        if last is not None and (now - last) < _WARN_WINDOW_SECONDS:
            return
    _WARN_STATE[reason] = now
    logger.warning(
        "hub unreachable (%s); failing open to write level (rate-limited)",
        reason,
    )


def _fail_open(project_id: str, collection: str, reason: str) -> str:
    """Emit metric + warning + return 'write'. The fail-open contract."""
    _emit_metric(project_id, collection, reason)
    _emit_warning(reason)
    return "write"


def check_access_level(project_id: str, collection: str) -> str:
    """Return the access level for ``(project_id, collection)``.

    Returns one of ``"read"``, ``"write"``, ``"none"``. Fail-open: any
    network / auth / parse error returns ``"write"`` (most-permissive)
    with a metric emission + rate-limited WARNING log. Never raises.

    The caller gates the write:

    .. code-block:: python

        level = check_access_level(pid, coll)
        if level != "write":
            return  # silent drop or error response per caller's contract
    """
    if not project_id or not collection:
        # No project context → can't check, fail-open without metric noise.
        return "write"

    token = _hub_token()
    if not token:
        return _fail_open(project_id, collection, "no_hub_token")

    port = _hub_port()
    url = f"http://127.0.0.1:{port}/api/v1/projects/{project_id}/access/{collection}"

    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            status = resp.status
            body_bytes = resp.read()
    except urllib.error.HTTPError as e:
        # 4xx + 5xx land here.
        if e.code == 401:
            return _fail_open(project_id, collection, "hub_auth_401")
        if e.code == 404:
            # Per the bash sibling: 404 = no row → fail-open with metric so
            # the user can investigate why the project isn't registered.
            return _fail_open(project_id, collection, "hub_404_no_row")
        if 500 <= e.code < 600:
            return _fail_open(project_id, collection, f"hub_5xx_{e.code}")
        return _fail_open(project_id, collection, f"hub_unexpected_{e.code}")
    except urllib.error.URLError as e:
        return _fail_open(project_id, collection, f"url_error_{type(e.reason).__name__ if hasattr(e, 'reason') else 'unknown'}")
    except Exception as e:
        return _fail_open(project_id, collection, f"unexpected_{type(e).__name__}")

    if status != 200:
        return _fail_open(project_id, collection, f"hub_status_{status}")

    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _fail_open(project_id, collection, "hub_malformed_json")

    level = body.get("level")
    if not isinstance(level, str) or level not in ("read", "write", "none"):
        return _fail_open(project_id, collection, "hub_malformed_level")

    return level


def is_write_allowed(project_id: str, collection: str) -> bool:
    """Convenience: returns True iff caller may write to ``collection``.

    Equivalent to ``check_access_level(project_id, collection) == "write"``,
    but more readable at call sites.
    """
    return check_access_level(project_id, collection) == "write"


__all__ = [
    "check_access_level",
    "is_write_allowed",
    "RESOLVER_PROTOCOL_VERSION",
]
