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

Two more rules close the gap the v0.2.105 delta review found (W-N1): the
live classes do not only build THEIR OWN client through :func:`connect` —
they then drive PRODUCTION code whose connect homes read ``GRPC_PORT`` ONLY
(``vco_lib/weaviate_schema.py``, ``vco_lib/embedding_enrichment.py``,
``vco_lib/weaviate_helpers.py``), each defaulting to the live 50052. So a
run that named only ``WEAVIATE_GRPC_PORT`` passed the gate, talked REST+gRPC
to the disposable instance from the test's own client, and sent the
production legs' gRPC to the LIVE server. The gate therefore:

* PROMOTES a lone ``WEAVIATE_GRPC_PORT`` into ``GRPC_PORT`` (the variable
  the production legs read, and the one the gate's own message names) once
  it has decided the test may run, so every leg of the run agrees; and
* REFUSES the live server's published gRPC port (50052) unless the operator
  explicitly opts in with ``VCT_ALLOW_LIVE_GRPC_PORT=1`` — pointing a
  WRITING live test at the developer's real ``vco_weaviate`` is exactly the
  accident this module exists to prevent.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from vco_lib.fixture_class_guard import UNROUTABLE_SENTINEL_URL

__all__ = ["DEAD_GRPC_PORT", "LIVE_GRPC_PORT", "ALLOW_LIVE_GRPC_ENV",
           "explicit_grpc_port", "grpc_port", "promote_grpc_port",
           "skip_reason", "connect"]

#: The discard port of :data:`UNROUTABLE_SENTINEL_URL` (``tests/conftest.py``
#: pins ``GRPC_PORT`` / ``WEAVIATE_GRPC_PORT`` to the same value).
DEAD_GRPC_PORT = int(UNROUTABLE_SENTINEL_URL.rsplit(":", 1)[1])

#: The LIVE ``vco_weaviate``'s published gRPC port — the default every
#: production connect home falls back to, and the one a live test must
#: never dial by accident (W-N1).
LIVE_GRPC_PORT = 50052

#: Explicit opt-in for the rare run that really targets the live server's
#: gRPC port (``VCT_ALLOW_LIVE_GRPC_PORT=1``); any other value keeps the
#: refusal.
ALLOW_LIVE_GRPC_ENV = "VCT_ALLOW_LIVE_GRPC_PORT"

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


def promote_grpc_port() -> None:
    """Mirror a lone ``WEAVIATE_GRPC_PORT`` into ``GRPC_PORT`` (W-N1).

    ``GRPC_PORT`` is the variable EVERY shipped production connect home
    reads (``vco_lib/weaviate_schema.py``, ``vco_lib/embedding_enrichment.py``,
    ``vco_lib/weaviate_helpers.py`` — each defaulting to the live 50052);
    ``WEAVIATE_GRPC_PORT`` alone names the port for this gate but not for
    the production legs the ticked tests then drive. Promoting it once the
    gate opens makes every leg of the run agree. A set ``GRPC_PORT`` always
    wins and is never overwritten; a run that named nothing has nothing to
    promote. Only the real environment is mutated — callers that pass an
    explicit mapping to :func:`skip_reason` / :func:`connect` get the pure
    resolvers without side effects.
    """
    if (os.environ.get("GRPC_PORT") or "").strip():
        return
    alt = (os.environ.get("WEAVIATE_GRPC_PORT") or "").strip()
    if alt:
        os.environ["GRPC_PORT"] = alt


def _live_port_opted_in(environ: Optional[Mapping[str, str]]) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(ALLOW_LIVE_GRPC_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def skip_reason(url: str, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Why a live test must skip, or ``None`` when it may run. The gRPC check
    comes first, so an unpaired run probes nothing at all. When the gate
    OPENS on the real environment, a lone ``WEAVIATE_GRPC_PORT`` is promoted
    into ``GRPC_PORT`` so the production legs the test drives use the same
    port (:func:`promote_grpc_port`)."""
    import importlib.util  # noqa: PLC0415

    if importlib.util.find_spec("weaviate") is None:
        return "the weaviate package is not installed"
    port = explicit_grpc_port(environ)
    if port is None:
        return (f"no gRPC port named for the Weaviate at {url}: set GRPC_PORT to that "
                f"instance's gRPC port (it is never defaulted to the live {LIVE_GRPC_PORT})")
    if port == LIVE_GRPC_PORT and not _live_port_opted_in(environ):
        return (f"the named gRPC port {LIVE_GRPC_PORT} is the LIVE vco_weaviate's published "
                "port: a writing live test must target a DISPOSABLE instance's gRPC port "
                f"(set {ALLOW_LIVE_GRPC_ENV}=1 only to aim at the live server deliberately)")
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/v1/.well-known/ready", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                if environ is None:
                    promote_grpc_port()
                return None
            return f"Weaviate at {url} answered HTTP {resp.status}"
    except (urllib.error.URLError, OSError, ValueError):
        return f"Weaviate not reachable at {url}"


def connect(url: str, environ: Optional[Mapping[str, str]] = None) -> Any:
    """A weaviate-client v4 connection: REST on *url*, gRPC on the same host at
    :func:`grpc_port`. On the real environment a lone ``WEAVIATE_GRPC_PORT``
    is promoted into ``GRPC_PORT`` first, so a caller that skipped the gate
    still pairs the production legs with this client (:func:`promote_grpc_port`)."""
    import weaviate  # noqa: PLC0415

    if environ is None:
        promote_grpc_port()
    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    return weaviate.connect_to_custom(
        http_host=host, http_port=parts.port or 80, http_secure=False,
        grpc_host=host, grpc_port=grpc_port(environ), grpc_secure=False,
        skip_init_checks=True,
    )
