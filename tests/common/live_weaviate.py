# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The suite's opt-in LIVE Weaviate tests: the gate and the client — the ONE
home (v0.2.105).

Three files carry a live round-trip against a real Weaviate
(``test_vco_lib_migrate.LiveMigrateIntegrationTest``,
``test_weaviate_schema.LiveSchemaMigrationTest``,
``test_embedding_enrichment.LiveEnrichmentTest``). They self-skip unless
``WEAVIATE_URL`` answers, and they are meant to be pointed at a DISPOSABLE
instance. Each built its gRPC channel with ``GRPC_PORT``, defaulting to
50052 — the LIVE ``vco_weaviate``'s published gRPC port. So a run that
pointed ``WEAVIATE_URL`` at a disposable container on other ports and left
``GRPC_PORT`` unset sent its batch writes and reads over gRPC to the live
Weaviate on the same host, while its REST calls went to the container
(observed once during the v0.2.105 Weaviate-upgrade work).

The rule here: the gRPC port of a live test is never guessed. It is
``GRPC_PORT`` (else ``WEAVIATE_GRPC_PORT``, the order the shipped edges use,
``vco_lib/codegraph_edge_env.py``) when set, and otherwise the unroutable
sentinel port the suite pins every backend to — and the gate skips the test
with a reason naming the variable, before any network probe, so the sentinel
is a backstop that is never reached by a test that runs.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from vco_lib.fixture_class_guard import UNROUTABLE_SENTINEL_URL

__all__ = ["DEAD_GRPC_PORT", "explicit_grpc_port", "grpc_port", "skip_reason", "connect"]

#: The discard port of :data:`UNROUTABLE_SENTINEL_URL` (``tests/conftest.py``
#: pins ``GRPC_PORT`` / ``WEAVIATE_GRPC_PORT`` to the same value).
DEAD_GRPC_PORT = int(UNROUTABLE_SENTINEL_URL.rsplit(":", 1)[1])

_GRPC_KEYS = ("GRPC_PORT", "WEAVIATE_GRPC_PORT")


def explicit_grpc_port(environ: Optional[Mapping[str, str]] = None) -> Optional[int]:
    """The gRPC port the caller named (``GRPC_PORT``, else
    ``WEAVIATE_GRPC_PORT``), or ``None`` when neither is set."""
    env = os.environ if environ is None else environ
    for key in _GRPC_KEYS:
        raw = (env.get(key) or "").strip()
        if raw:
            return int(raw)
    return None


def grpc_port(environ: Optional[Mapping[str, str]] = None) -> int:
    """The port a live test's client uses: the named one, else the dead
    sentinel — never the live default 50052."""
    port = explicit_grpc_port(environ)
    return DEAD_GRPC_PORT if port is None else port


def skip_reason(url: str, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Why a live test must skip, or ``None`` when it may run. The gRPC check
    comes first, so an unpaired run probes nothing at all."""
    import importlib.util  # noqa: PLC0415

    if importlib.util.find_spec("weaviate") is None:
        return "the weaviate package is not installed"
    if explicit_grpc_port(environ) is None:
        return (f"no gRPC port named for the Weaviate at {url}: set GRPC_PORT to that "
                "instance's gRPC port (it is never defaulted to the live 50052)")
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/v1/.well-known/ready", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                return None
            return f"Weaviate at {url} answered HTTP {resp.status}"
    except (urllib.error.URLError, OSError, ValueError):
        return f"Weaviate not reachable at {url}"


def connect(url: str, environ: Optional[Mapping[str, str]] = None) -> Any:
    """A weaviate-client v4 connection: REST on *url*, gRPC on the same host at
    :func:`grpc_port`."""
    import weaviate  # noqa: PLC0415

    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    return weaviate.connect_to_custom(
        http_host=host, http_port=parts.port or 80, http_secure=False,
        grpc_host=host, grpc_port=grpc_port(environ), grpc_secure=False,
        skip_init_checks=True,
    )
