# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Reusable helpers for safe Weaviate GraphQL interaction.

Centralizes the "inspect errors[] array BEFORE consuming data" pattern
that v0.2.42-v0.2.45's recurring re-embed bug was rooted in. See
knowledge/concepts/silent-zero-fallback-antipattern.md (instance #3) +
knowledge/concepts/mcp-loud-fail-error-pattern.md (GraphQL errors[]
sub-pattern).

By GraphQL convention, errors can be present even on HTTP 200 OK
responses. The bug pattern is:

    body = json.loads(resp.read())
    objs = body.get("data", {}).get("Get", {}).get(coll, []) or []
    # ^ silently coalesces (200 OK + errors[]) -> [] -> "0 objects found"

Callers should instead use ``post_graphql_safe`` (one-call wrapper) or
``check_graphql_errors`` (just the errors gate) so they inherit the
protection by default.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable, Optional


class WeaviateGraphQLError(Exception):
    """Raised when a Weaviate GraphQL response carries a non-empty errors array.

    Carries the full errors list for caller inspection. HTTP status is
    irrelevant — by GraphQL convention errors can be present even on 200 OK.
    """

    def __init__(self, errors: list[dict[str, Any]], ctx: str = ""):
        self.errors = errors
        self.ctx = ctx
        first = errors[0].get("message", "unknown") if errors else "unknown"
        super().__init__(f"{ctx}: {first[:200]}")


def check_graphql_errors(
    body: dict[str, Any],
    ctx: str = "graphql",
    on_error: Optional[Callable[[list[dict[str, Any]]], None]] = None,
) -> bool:
    """Returns True if errors array present (and calls on_error if given).
    Returns False if errors absent (caller should consume data normally).

    Pattern:
        body = json.loads(resp.read())
        if check_graphql_errors(body, ctx="ci10_diff_gate"):
            return {}  # or whatever the failure-path return is
        data = body["data"]  # safe to consume now

    on_error callback receives the errors list; useful for logging via
    the caller's _log_install_event or similar. Exceptions raised by the
    callback are swallowed so logging failure can never block the caller.
    """
    errors = body.get("errors")
    if not errors:
        return False
    if on_error:
        try:
            on_error(errors)
        except Exception:
            pass  # logging failure should never block the caller
    return True


def post_graphql_safe(
    weaviate_url: str,
    gql: dict[str, Any],
    ctx: str = "graphql",
    timeout: float = 30.0,
    on_error: Optional[Callable[[list[dict[str, Any]]], None]] = None,
) -> Optional[dict[str, Any]]:
    """POST a GraphQL query to Weaviate; return the ``data`` field on success,
    None on errors-array OR HTTP failure (caller decides recovery).

    ``on_error`` is called with the errors list when present. ``on_error`` is
    also called with a synthetic error-list when the HTTP transport fails
    (e.g., ``[{"message": "transport: <details>"}]``) so the caller has a
    single observability path.

    Example caller:
        def _batch_query_weaviate_content_hashes(coll, url):
            gql = {"query": f"{{ Get {{ {coll}(limit: 10000) "
                            f"{{ file_path content_hash }} }} }}"}
            data = post_graphql_safe(url, gql, ctx=f"hash-query-{coll}",
                                     on_error=lambda errs: log_warn(coll, errs))
            if data is None:
                return {}
            objs = data.get("Get", {}).get(coll) or []
            return {o["file_path"]: o.get("content_hash", "") for o in objs if o.get("file_path")}
    """
    try:
        req = urllib.request.Request(
            f"{weaviate_url}/v1/graphql",
            data=json.dumps(gql).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except Exception as exc:
        if on_error:
            try:
                on_error([{"message": f"transport: {exc}"}])
            except Exception:
                pass
        return None

    if check_graphql_errors(body, ctx=ctx, on_error=on_error):
        return None

    return body.get("data")
