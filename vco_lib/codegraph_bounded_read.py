# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Bounded code-graph reads — the one home shared by the MCP and the CLI.

v0.2.107 (CG-1): a code-graph read that names no properties returns whole rows,
and a reference resolved without a property list returns whole referenced rows.
One callers page over a generated bundle (one row with ~1,000 ``calls`` links to
rows of the same size) came back as a 10–14 GB reply and Weaviate was killed.

Two rules live here so ``weaviate_mcp.server`` and
``templates/scripts/query_code_graph.py`` cannot drift:

* :func:`reference` resolves ONE link with only the fields its readers consume
  (:data:`REFERENCE_PROPERTIES`). An empty list would break the readers —
  ``path`` / ``expand_hops`` read the callee's name, signature and file;
  ``dependencies`` / ``imports`` the module's path and language; ``extends`` the
  base's name, full name and file.
* :func:`fetch_objects` / :func:`fetch_object_by_id` / :func:`rows_or_empty`
  turn a gRPC RESOURCE_EXHAUSTED ("message larger than max") into ONE log line
  naming the collection and the call site, and an empty result. The same read
  is never retried: a bigger reply limit would only move the crash into
  Weaviate.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Callable

logger = logging.getLogger("vco_lib.codegraph_bounded_read")

#: The target fields each resolved link carries — every field a reader of that
#: link consumes, nothing else (never a body).
REFERENCE_PROPERTIES: dict[str, tuple[str, ...]] = {
    "calls": ("full_name", "signature", "file_path"),
    "imports": ("path", "language"),
    "extends": ("name", "full_name", "file_path"),
}

#: Same-file sibling refs (search enrichment): the fields the sibling ref
#: renders plus the ones ``confirmed_entity_rows`` confirms on.
SIBLING_PROPERTIES: tuple[str, ...] = ("full_name", "file_path", "start_line", "project")

_CHUNK_COMMON = (
    "full_name", "file_path", "project", "chunk_num",
    "signature", "doc", "start_line", "end_line",
)
_BODY_PROPERTY = {
    "CodeFunction": "function_body",
    "CodeClass": "class_body",
    "CodeModule": "module_summary",
    "CodeAPI": "api_description",
}

#: Ranking-anchor fields (``code_ranking.rerank_score`` reads name, calls,
#: file, type uses, test flag, config identity) per chunked base.
_ANCHOR_PROPERTIES = {
    "CodeFunction": (
        "full_name", "name", "call_names", "file_path", "type_uses", "is_test",
        "chunk_num", "language", "project_source",
    ),
    "CodeClass": (
        "full_name", "name", "file_path", "is_test", "chunk_num", "language",
        "project_source",
    ),
}


def chunk_properties(base: str) -> list[str]:
    """Fields of one chunk row of a CodeFunction / CodeClass entity window."""
    extra = ("is_async", "function_body") if base == "CodeFunction" else ("methods", "class_body")
    return list(_CHUNK_COMMON + extra)


def anchor_properties(base: str) -> list[str]:
    """Fields the ranking anchor of a CodeFunction / CodeClass row needs."""
    return list(_ANCHOR_PROPERTIES[base])


def similarity_reference_properties(base: str) -> list[str]:
    """Fields ``find_similar`` reads off its reference row: identity, project
    and the body it embeds (``doc`` as the fallback on chunked bases)."""
    props = ["full_name", "project", _BODY_PROPERTY.get(base, "function_body")]
    if base in ("CodeFunction", "CodeClass"):
        props.append("doc")
    return props


_EXHAUSTED_MARKERS = ("resource_exhausted", "message larger than", "message too large")


def reference(link_on: str):
    """A ``QueryReference`` resolving ``link_on`` with its bounded field list."""
    from weaviate.classes.query import QueryReference

    return QueryReference(
        link_on=link_on, return_properties=list(REFERENCE_PROPERTIES[link_on]),
    )


def is_resource_exhausted(exc: BaseException) -> bool:
    """True when ``exc`` (or an exception it chains) is an oversized reply."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = str(cur).lower()
        if any(marker in text for marker in _EXHAUSTED_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def report_resource_exhausted(exc: BaseException, coll: Any, call_site: str) -> bool:
    """Log the one RESOURCE_EXHAUSTED line and return True, else return False."""
    if not is_resource_exhausted(exc):
        return False
    logger.error(
        "RESOURCE_EXHAUSTED code-graph read: collection=%s call_site=%s; "
        "returning no result",
        getattr(coll, "name", "?"), call_site,
    )
    return True


def fetch_objects(coll: Any, call_site: str, **kwargs: Any):
    """``coll.query.fetch_objects(**kwargs)``; an oversized reply → no rows."""
    try:
        return coll.query.fetch_objects(**kwargs)
    except Exception as exc:
        if report_resource_exhausted(exc, coll, call_site):
            return SimpleNamespace(objects=[])
        raise


def fetch_object_by_id(coll: Any, uuid: Any, call_site: str, **kwargs: Any):
    """``coll.query.fetch_object_by_id``; an oversized reply → ``None``."""
    try:
        return coll.query.fetch_object_by_id(uuid, **kwargs)
    except Exception as exc:
        if report_resource_exhausted(exc, coll, call_site):
            return None
        raise


def rows_or_empty(read: Callable[..., list], coll: Any, call_site: str,
                  *args: Any, **kwargs: Any) -> list:
    """Run a row-list reader (``code_identity_rows``, ``fetch_matching_rows``);
    an oversized reply → ``[]``."""
    try:
        return read(coll, *args, **kwargs)
    except Exception as exc:
        if report_resource_exhausted(exc, coll, call_site):
            return []
        raise
