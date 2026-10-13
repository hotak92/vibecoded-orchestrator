# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE home for how a codegraph migration edge finds its scope and its
Weaviate (v0.2.102 W1-C S2).

Before this module, each ``migrations/codegraph_collection/*.py`` edge carried
its own ``_resolve_codegraph_prefix`` + ``_connect``. There were four copies,
and they had already drifted:

* ``6_to_7`` still had a silent regex fallback when ``vco_lib`` failed to
  import;
* ``4_to_5`` / ``5_to_6`` / ``7_to_8`` loud-failed instead;
* the runner had its own fifth copy of the prefix rule.

The edges and the runner now call the functions below. The edges import
``vco_lib`` from the project root, the runner's ``cwd``. A failing import is a
broken install and propagates loudly; the edges turn it into a non-zero exit,
so the runner defers and retries.
"""
from __future__ import annotations

import os
import re
from typing import Any, Mapping, Optional


def resolve_codegraph_prefix(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The project's codegraph class prefix, from ``env`` (default ``os.environ``).

    ``CODE_GRAPH_PROJECT`` wins: it is already the sanitized prefix the analyzer
    uses verbatim. Otherwise the prefix is derived from ``PROJECT_NAME`` exactly
    as the analyzer derives its class prefix: ``canonical_class_prefix``, which
    preserves underscores, with the analyzer's legacy rewrite applied only when
    that helper rejects the name (``ValueError``). Returns ``None`` when neither
    variable is set; the caller then has no scope and must not guess one.
    """
    source = os.environ if env is None else env
    cg = (source.get("CODE_GRAPH_PROJECT") or "").strip()
    if cg:
        return cg
    pn = (source.get("PROJECT_NAME") or "").strip()
    if not pn:
        return None
    from vco_lib.project_naming import canonical_class_prefix  # ImportError = broken install

    try:
        return canonical_class_prefix(pn)
    except ValueError:
        # Identical to analyze_code_graph._sanitize_collection_prefix's legacy
        # arm, so an edge probes the class name the analyzer actually wrote.
        sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", pn)
        if sanitized and sanitized[0].isalpha() and not sanitized[0].isupper():
            sanitized = sanitized[0].upper() + sanitized[1:]
        return sanitized or None


def connect_from_env() -> Any:
    """A weaviate-client v4 connection built by the ONE connect factory
    (``weaviate_helpers.connect_v4``).

    The URL comes from ``WEAVIATE_URL``, resolved by the factory. The gRPC port
    comes from ``GRPC_PORT``, else ``WEAVIATE_GRPC_PORT``, else 50052; these are
    the two variables the edges historically honoured. TLS follows the URL's
    scheme on both channels. Raises on failure, which the edge turns into a
    non-zero exit so the runner defers.
    """
    from vco_lib import weaviate_helpers as wh

    url = wh.weaviate_url_default()
    grpc_port = int(
        os.environ.get("GRPC_PORT") or os.environ.get("WEAVIATE_GRPC_PORT") or 50052
    )
    secure = url.lower().startswith("https://")
    return wh.connect_v4(url, grpc_port=grpc_port, grpc_secure=secure)
