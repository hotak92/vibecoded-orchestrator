# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Machine-wide "Weaviate endpoint down" marker (v0.2.107, audit A-063).

One home for the verdict every injection hook shares: the KG producer's HTTP
client (``claude_mcp_servers/weaviate_mcp/kg_http.py``) MARKS the endpoint
when a connection is refused or unreachable and CLEARS it on a successful
readiness probe; the hook router (``hook_context_router.py``) CHECKS it before
running any leg, so with Weaviate down each hook returns at once instead of
waiting out a connect timeout per tool call. A verdict older than
:data:`ENDPOINT_DOWN_TTL_S` is ignored, so a restarted Weaviate is used again
within half a minute even if nothing clears the marker.

It lives in ``vco_lib`` because both sides import ``vco_lib`` in every
layout; the router must not depend on where ``weaviate_mcp`` resolves.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

#: How long one "endpoint down" verdict makes every hook skip its legs.
ENDPOINT_DOWN_TTL_S = 30.0


def endpoint_identity(base_url: str = "") -> str:
    """``host:port`` of the endpoint. Empty ``base_url`` resolves exactly as
    the hook's client does (``kg_query_core``: ``WEAVIATE_URL``, else
    ``localhost:$WEAVIATE_PORT``, else 8081), so the router's check and the
    client's mark name the same marker."""
    url = (base_url or (os.environ.get("WEAVIATE_URL") or "").strip()
           or f"http://localhost:{(os.environ.get('WEAVIATE_PORT') or '').strip() or 8081}")
    rest = url.replace("http://", "").replace("https://", "").rstrip("/")
    host = rest.split(":")[0].split("/")[0]
    port = rest.split(":")[-1].split("/")[0] if ":" in rest else "8081"
    return f"{host}:{port}"


def down_marker_path(base_url: str = "") -> Path:
    from vco_lib.paths import vct_root_dir  # noqa: PLC0415

    digest = hashlib.sha256(endpoint_identity(base_url).encode("utf-8")).hexdigest()[:24]
    return vct_root_dir() / "hook_endpoint_down" / digest


def endpoint_is_down(base_url: str = "") -> bool:
    """True while a fresh down verdict exists for this endpoint."""
    try:
        marker = down_marker_path(base_url)
        return time.time() - marker.stat().st_mtime < ENDPOINT_DOWN_TTL_S
    except OSError:
        return False


def mark_endpoint_down(base_url: str = "") -> None:
    """Record a refused/unreachable endpoint (best-effort, never raises)."""
    try:
        from vco_lib.atomic import atomic_write_text  # noqa: PLC0415

        atomic_write_text(down_marker_path(base_url), str(int(time.time())) + "\n",
                          fsync=False)
    except OSError:
        pass


def clear_endpoint_down(base_url: str = "") -> None:
    """Drop the verdict once the endpoint answered (best-effort)."""
    try:
        down_marker_path(base_url).unlink(missing_ok=True)
    except OSError:
        pass
