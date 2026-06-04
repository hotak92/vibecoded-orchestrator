# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Reusable hash-diff sync helpers for KG / Development Weaviate collections.

This module centralises the hash-diff infrastructure used by:

- ``install.py`` ``_batch_query_weaviate_content_hashes`` (per-file content_hash
  fetch for the CI-10 "pay once, never again" diff gate added in v0.2.42).
- The v0.2.46 KG-rebind re-sync path (forthcoming): when the user changes a
  per-project KG binding via the launcher GUI, the launcher needs the same
  hash-diff logic to decide which on-disk files actually need re-embedding
  vs. which are already aligned with the new target collection's content.

By extracting the helper here, both call paths share:

1. **The V46-A safety triad** (load-bearing — was the root cause of the
   v0.2.42–v0.2.45 silent-zero-fallback bug):
   - No ``where: {Like, "%"}`` clause (SQL-wildcard convention; Weaviate's
     BM25 tokenizer rejects ``%`` as "only stopwords provided" and the
     HTTP 200 + errors-array response was silently coalesced to ``[]``).
   - ``limit: 10000`` matches Weaviate's ``QUERY_MAXIMUM_RESULTS`` default.
     Anything smaller silently truncates collections >1k objects.
   - **Inspect ``body["errors"]`` BEFORE consuming ``data``** — the
     canonical loud-fail-on-GraphQL-errors discipline.
   - **Saturation warning** when result count hits the 10000 cap, signalling
     the caller that cursor pagination may be needed.

2. **V46-F's ``post_graphql_safe`` routing** (not raw ``urllib.request``).
   Inherits the errors-array gate via callback; centralises the GraphQL
   transport in one place.

3. **V46-B's source-inspection regression guard** (in
   ``tests/test_v0246_v46b_live_ci10_diff_gate.py``) is extended to cover
   THIS module so a future refactor that drops the safety triad fails CI
   loudly instead of silently re-introducing the v0.2.42 bug.

Cross-OS / cross-runtime / cross-GPU notes:
- Pure stdlib + ``vco_lib.weaviate_helpers``. No ``platform`` / ``os.name``
  branches; no container-CLI invocation; no GPU dependencies.
- Tested on Linux / macOS / Windows via the same test fixtures.

References:
- ``knowledge/concepts/silent-zero-fallback-antipattern.md`` instance #3
- ``knowledge/concepts/mcp-loud-fail-error-pattern.md`` § GraphQL errors[]
- ``knowledge/concepts/install-py-collection-bootstrap-bugs.md`` v0.2.46
- ``vco_lib/weaviate_helpers.py`` (V46-F: ``post_graphql_safe``)
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from vco_lib.weaviate_helpers import post_graphql_safe


# The Weaviate-side cap matching ``QUERY_MAXIMUM_RESULTS`` (default 10000).
# Saturation at this value triggers a warning callback; future cursor-based
# pagination would land here.
QUERY_MAX_LIMIT: int = 10000


def batch_query_content_hashes(
    weaviate_url: str,
    collection_name: str,
    on_warn: Optional[Callable[[str, dict[str, Any]], None]] = None,
) -> "dict[str, str]":
    """Fetch stored ``content_hash`` values from a Weaviate collection.

    Returns a dict mapping ``file_path`` → ``content_hash`` for every
    object in the collection that has both properties populated. Objects
    missing ``content_hash`` (e.g. legacy rows from before v0.2.17) return
    an empty string for that file_path; the diff caller treats this as
    "always stale" → triggers a single-file re-sync (correct behaviour:
    we want to populate the missing hash).

    Returns an empty dict on any error so the caller defaults to "full
    sync required". Soft-fail throughout; never raises.

    Args:
        weaviate_url: Base URL (e.g. ``http://localhost:8081``).
        collection_name: Weaviate class name (e.g.
            ``VCODev_KnowledgeGraph`` or ``VCODev_Development``).
        on_warn: Optional callback called for non-fatal anomalies:
            - ``on_warn("graphql_errors", {"collection": name, "errors": [...]})``
            - ``on_warn("saturation", {"collection": name, "rows": int})``
            - ``on_warn("transport_failure", {"collection": name, "error": str})``
            Callbacks are wrapped in try/except so observability failures
            never break the caller.

    The V46-A safety triad is preserved:
    1. No ``where`` filter — pre-v0.2.46 used ``Like "%"`` which Weaviate's
       BM25 tokenizer rejects.
    2. ``limit: 10000`` = Weaviate's ``QUERY_MAXIMUM_RESULTS`` default.
    3. Routes through ``post_graphql_safe`` (V46-F) which inspects
       ``body["errors"]`` BEFORE consuming ``data``.
    4. Emits ``on_warn("saturation", ...)`` when result count hits the cap.

    The function is **pure** w.r.t. its inputs — same (url, class) returns
    the same result modulo Weaviate state.
    """
    gql = {
        "query": (
            f"{{ Get {{ {collection_name}(limit: {QUERY_MAX_LIMIT}) "
            f"{{ file_path content_hash }} }} }}"
        ),
    }

    # Route via V46-F's safe POST — inherits errors-array gate.
    def _on_error_relay(errors: list[dict[str, Any]]) -> None:
        if on_warn is None:
            return
        # First error wins for the "channel" decision: transport vs
        # graphql_errors. ``post_graphql_safe`` synthesises a single
        # ``[{"message": "transport: ..."}]`` entry on HTTP failures.
        first_msg = (errors[0] or {}).get("message", "") if errors else ""
        channel = "transport_failure" if first_msg.startswith("transport:") else "graphql_errors"
        try:
            on_warn(
                channel,
                {
                    "collection": collection_name,
                    "errors": [
                        (e or {}).get("message", "")[:200]
                        for e in errors[:3]
                    ],
                },
            )
        except Exception:
            pass  # observability failure must never break the caller

    data = post_graphql_safe(
        weaviate_url,
        gql,
        ctx=f"kg_sync.batch_query_content_hashes[{collection_name}]",
        on_error=_on_error_relay,
    )
    if data is None:
        return {}

    objects = data.get("Get", {}).get(collection_name) or []

    # V46-A saturation warning: signals approaching the QUERY_MAXIMUM_RESULTS
    # cap. Future enhancement = cursor pagination via ``after:`` parameter.
    if on_warn is not None and len(objects) >= QUERY_MAX_LIMIT:
        try:
            on_warn(
                "saturation",
                {"collection": collection_name, "rows": len(objects)},
            )
        except Exception:
            pass

    result: dict[str, str] = {}
    for obj in objects:
        fp = (obj.get("file_path") or "").strip()
        ch = (obj.get("content_hash") or "").strip()
        if fp:
            result[fp] = ch
    return result
