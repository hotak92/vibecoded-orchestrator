# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Exact-match row selection over a Weaviate TOKENIZED narrowing read (v0.2.101).

Why this module exists
----------------------
The KG / Development collections declare ``file_path`` and ``title`` as
``TEXT`` with Weaviate's default ``word`` tokenization. A
``Filter.by_property("file_path").equal(p)`` therefore does NOT compare
strings: it matches every row whose token set CONTAINS the tokens of ``p``.
``knowledge/concepts/knowledge-graph.md`` tokenizes to
``{knowledge, concepts, graph, md}``, so the "exact" filter also returns the
rows of ``orchestrator-knowledge-graph.md``, ``orchestrator-code-graph.md``,
``shared-knowledge-graph.md`` … (live-confirmed on a real KG, 2026-10-06).

Every caller that DELETES the returned rows (kg-sync's upsert, its archived /
migration cleanup, the docs upsert, the MCP ``store_knowledge_node``) thus
deleted its token-subset SIBLINGS' rows too — silent KG data loss — and every
caller that JUDGES the returned rows (kg-sync's embed-skip gate) judged a set
polluted with other files' rows, so the gate never fired for the killer either.

The rule (the same one ``vco_lib/codegraph_resync.py::delete_file_rows_exact``
established for the code graph in v0.2.74): a tokenized filter may only NARROW
which rows are read back; the decision is made in PYTHON on the raw property
value read back per row. A word-tokenized ``Equal`` returns a SUPERSET of the
exact-string rows (identical token set ⇒ match), so narrowing can over-fetch —
harmless once this module's predicate rejects the extras — but never miss.

``delete_file_rows_exact`` stays the code graph's deleter (predicate + full
scan fallback + per-row failure accounting — a different shape); this module
is the KG-side home for "select the exact rows from a narrowing read", shared
by ``templates/scripts/sync_knowledge_graph.py``,
``claude_mcp_servers/weaviate_mcp/server.py`` (``store_knowledge_node``) and
``vco_lib/diagram_indexer.py`` (the ``<Project>_Diagrams`` delete/upsert).

The structural alternative — ``tokenization: field`` on ``file_path`` — is a
destructive schema migration (re-create + re-ingest) and needs the owner's
consent; this module is the non-destructive fix and stays correct after such a
migration (an exact filter returns exactly the rows this predicate accepts).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, List, Mapping, Optional, Tuple

from vco_lib.paths import to_posix_rel

logger = logging.getLogger(__name__)

__all__ = [
    "PAGE_SIZE",
    "MAX_PAGES",
    "path_spellings",
    "path_narrowing_filter",
    "is_exact_path",
    "word_tokens",
    "is_same_title",
    "fetch_matching_rows",
    "fetch_exact_path_rows",
]

#: Rows per narrowing-read page. A tokenized read over-fetches siblings, so a
#: single ``limit=100`` read could be FILLED by other files' rows and drop the
#: target's own — the reader pages until a short page instead.
PAGE_SIZE = 100
#: Page cap: 100 × 100 = 10 000 rows = Weaviate's default
#: ``QUERY_MAXIMUM_RESULTS`` (offset + limit past it is refused server-side).
MAX_PAGES = 100

_WORD_TOKEN_RE = re.compile(r"[^\W_]+")


def path_spellings(canonical: str) -> Tuple[str, ...]:
    """The canonical POSIX spelling plus its legacy backslash variant (once).

    v0.2.92 WP-B1 / C-7: rows written by a pre-canonical Windows writer carry
    ``knowledge\\concepts\\foo.md``; both spellings name the same node.
    """
    posix = to_posix_rel(canonical)
    backslash = posix.replace("/", "\\")
    return (posix,) if backslash == posix else (posix, backslash)


def path_narrowing_filter(filter_cls: Any, canonical: str, prop: str = "file_path"):
    """Build the NARROWING read filter for *canonical*: an OR of ``Equal`` on
    each spelling in :func:`path_spellings`.

    This is a candidate-selection filter ONLY — on a word-tokenized property it
    matches token-supersets (other files). Pair it with :func:`is_exact_path`
    (or :func:`fetch_exact_path_rows`) before judging or deleting anything.

    *filter_cls* is the caller's ``weaviate.classes.query.Filter`` binding, so a
    test that swaps the module-level ``Filter`` for an in-memory fake keeps
    working.
    """
    spellings = path_spellings(canonical)
    if len(spellings) == 1:
        return filter_cls.by_property(prop).equal(spellings[0])
    return filter_cls.any_of([
        filter_cls.by_property(prop).equal(s) for s in spellings
    ])


def is_exact_path(raw: Any, canonical: str) -> bool:
    """True when the RAW stored ``file_path`` names exactly *canonical*.

    Compared separator-insensitively (``to_posix_rel`` on both sides — the same
    identity ``vco_lib.kg_dedup`` groups on), so the legacy backslash spelling
    of the same node is accepted. A missing / empty / non-string value is never
    a match: a row that cannot be confirmed must never be deleted on a guess.
    """
    if not isinstance(raw, str) or not canonical:
        return False
    # Strip BEFORE comparing (re-review N3): the guard used to test
    # ``raw.strip()`` while the comparison used ``raw`` unstripped, so a
    # row with trailing whitespace passed the guard and then never matched
    # — an orphan for no reason. Stripping cannot make two different paths
    # equal, so this never over-matches.
    stripped = raw.strip()
    if not stripped:
        return False
    return to_posix_rel(stripped) == to_posix_rel(canonical)


def word_tokens(text: Any) -> List[str]:
    """Weaviate ``word`` tokenization: lowercase, split on every non-alphanumeric
    character (underscore included), keep the alphanumeric runs in order."""
    return _WORD_TOKEN_RE.findall(str(text or "").lower())


def is_same_title(raw: Any, wanted: str) -> bool:
    """True when *raw* and *wanted* tokenize to the SAME token sequence.

    Keeps exactly the case / punctuation insensitivity a tokenized ``Equal``
    gives a WikiLink (``[[uses::weaviate]]`` → ``Weaviate``,
    ``knowledge-graph`` → ``Knowledge Graph``) while rejecting what ``Equal``
    also lets through: a title that merely CONTAINS those tokens
    (``Weaviate`` → ``Weaviate Windows Ports Gotcha``).
    """
    want = word_tokens(wanted)
    return bool(want) and word_tokens(raw) == want


def fetch_matching_rows(
    coll: Any,
    narrow_filter: Any,
    accept: Callable[[Mapping[str, Any]], bool],
    *,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
    max_matches: Optional[int] = None,
    **fetch_kwargs: Any,
) -> list:
    """Page a narrowing ``fetch_objects`` read; keep only rows *accept* confirms.

    *accept* receives the row's raw ``properties`` dict; a predicate that raises
    REJECTS the row (an error must never select a row for deletion). Paging
    stops at the first short page, at *max_matches* accepted rows, or at the
    page cap (logged — the result is then a confirmed SUBSET, never a guess).

    ``offset`` is passed only from the second page on, so a first read is
    byte-identical to the pre-paging call. Exceptions from ``fetch_objects``
    PROPAGATE: callers keep their own fallback ladders.
    """
    rows: list = []
    offset = 0
    for _ in range(max_pages):
        kwargs = dict(fetch_kwargs, filters=narrow_filter, limit=page_size)
        if offset:
            kwargs["offset"] = offset
        result = coll.query.fetch_objects(**kwargs)
        objs = list(getattr(result, "objects", None) or [])
        for obj in objs:
            props = getattr(obj, "properties", None) or {}
            try:
                hit = bool(accept(props))
            except Exception:  # noqa: BLE001 — a predicate error never selects
                hit = False
            if hit:
                rows.append(obj)
                if max_matches is not None and len(rows) >= max_matches:
                    return rows
        if len(objs) < page_size:
            return rows
        offset += page_size
    logger.warning(
        "weaviate exact-match read hit the %d-page cap on %s; the confirmed "
        "rows returned are a SUBSET of the exact matches",
        max_pages, getattr(coll, "name", "?"),
    )
    return rows


def fetch_exact_path_rows(
    coll: Any,
    narrow_filter: Any,
    canonical: str,
    *,
    prop: str = "file_path",
    **fetch_kwargs: Any,
) -> list:
    """The rows whose raw *prop* is exactly *canonical* (either spelling).

    *narrow_filter* is normally :func:`path_narrowing_filter`'s result (or that
    ANDed with further narrowing). When the caller names ``return_properties``,
    *prop* is added if absent — the predicate needs the raw value back.
    """
    rp = fetch_kwargs.get("return_properties")
    if rp is not None and prop not in rp:
        fetch_kwargs["return_properties"] = list(rp) + [prop]
    return fetch_matching_rows(
        coll,
        narrow_filter,
        lambda props: is_exact_path(props.get(prop), canonical),
        **fetch_kwargs,
    )
