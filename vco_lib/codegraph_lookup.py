# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE home for code-graph IDENTITY row lookups (v0.2.102 W4-C L5).

``full_name`` / ``path`` / ``file_path`` / ``project`` are TEXT with
Weaviate's default ``word`` tokenization, so ``Filter.by_property(...).
equal(x)`` matches every row whose token set CONTAINS ``x``'s tokens —
``a.run`` → ``a.run_all`` (live-confirmed v0.2.101). The MCP
(``weaviate_mcp/server.py``) moved to exact-then-tolerant reads in v0.2.101;
the CLI (``templates/scripts/query_code_graph.py``) kept the tokenized
``Equal`` + ``limit`` shape at its chunk fetcher, sibling fetch, anchor
resolution and structure lookups, so the two surfaces could answer the SAME
question with DIFFERENT rows (P1 report :157-163). This module is the shared
helper both call — cross-language rule A (shared code, never a mirror):

* :func:`code_identity_rows` — EXACT-then-tolerant resolution of a
  user-typed target (the v0.2.101 owner-ruling shape; exactness wins when a
  row confirms, tolerance keeps ``a::run`` / bare-leaf lookups resolving).
* :func:`confirmed_entity_rows` — CONFIRMED-only rows for one entity's
  fetchers (chunk windows, same-file siblings): no tolerant fallback, a
  sibling's rows can never interleave.
* :func:`pick_canonical_chunk` — the canonical chunk-0 selection both
  surfaces need for chunked code entities.

Pure decision logic lives in :mod:`vco_lib.weaviate_exact_match`; this
module adds the code-graph clause assembly (project clause, path vs value
identity) on top. ``filter_cls`` is passed in so tests (and the CLI) can
swap the real ``weaviate.classes.query.Filter`` for an in-memory fake.
"""
from __future__ import annotations

from typing import Any, Optional

import vco_lib.weaviate_exact_match as _wem

__all__ = [
    "code_identity_rows",
    "confirmed_entity_rows",
    "pick_canonical_chunk",
]


def pick_canonical_chunk(objects: list) -> Any:
    """From a list of Weaviate code objects sharing a full_name, return the
    CANONICAL (chunk_num == 0) one.

    v0.2.72 (P3): a chunked function/class is stored as N objects that all
    carry the same ``full_name`` but different ``chunk_num`` (0-indexed). The
    structure lookups (methods / extends / interactions) want THE object for
    that name — which must be the canonical chunk 0 (it holds the
    signature-leading body, the methods list, and is the reference target
    cross-references resolve to).

    Selection: prefer chunk_num == 0; then a legacy row with chunk_num
    None/absent (single-chunk pre-migration entity); else the lowest
    chunk_num present; else the first object. Never raises on a missing
    property. Returns ``None`` for an empty list.
    """
    if not objects:
        return None

    def _cn(o) -> Any:
        try:
            v = o.properties.get("chunk_num")
        except Exception:  # noqa: BLE001
            return None
        return v

    # chunk_num == 0 → canonical.
    for o in objects:
        if _cn(o) == 0:
            return o
    # legacy single-chunk (property absent/None).
    for o in objects:
        if _cn(o) is None:
            return o
    # else: lowest chunk_num present.
    try:
        return min(objects, key=lambda o: (_cn(o) if _cn(o) is not None else 1 << 30))
    except Exception:  # noqa: BLE001
        return objects[0]


def _and_project(filter_cls: Any, flt: Any, project: str):
    """AND the project clause when the caller scoped to a project."""
    if project:
        return flt & filter_cls.by_property("project").equal(project)
    return flt


def code_identity_rows(
    coll: Any,
    prop: str,
    target: str,
    project: str,
    *,
    filter_cls: Any,
    tolerant_limit: int,
    **fetch_kwargs: Any,
) -> list:
    """The rows of the code entity named ``target`` on ``prop`` (``full_name``
    for CodeFunction / CodeClass, ``path`` for CodeModule) — EXACT-THEN-FALLBACK.

    v0.2.101 pull-in (owner ruling), shared by the MCP and the CLI since
    v0.2.102: ``full_name`` / ``path`` / ``project`` are word-TOKENIZED, so
    the ``Equal`` these lookups used matched every token-superset entity as
    well (``a.run`` → ``a.run_all``), and a ``limit=1`` read could answer
    with the sibling. That same tolerance is also what resolves a user-typed
    ``a::run`` or a bare ``paths.py``, so it is kept as the FALLBACK, never
    dropped: exact rows when any exist (``full_name`` by ``==``, ``path`` by
    the separator-insensitive exact path, ``project`` by token sequence),
    otherwise the very tokenized read the site made before.
    ``project`` empty → no project clause, as before.
    """
    flt = filter_cls.by_property(prop).equal(target)
    flt = _and_project(filter_cls, flt, project)
    if prop in ("path", "file_path"):
        accept = _wem.exact_row_predicate(
            paths={prop: target}, same_tokens={"project": project},
        )
    else:
        accept = _wem.exact_row_predicate(
            values={prop: target}, same_tokens={"project": project},
        )
    # Called through the MODULE (never a from-import binding): the suite's
    # red-proof seam patches ``weaviate_exact_match.fetch_matching_rows`` at
    # the ONE shared home and every read site must see it.
    return _wem.fetch_rows_exact_then_tolerant(
        coll, flt, accept, tolerant_limit=tolerant_limit, **fetch_kwargs,
    )


def confirmed_entity_rows(
    coll: Any,
    *,
    filter_cls: Any,
    project: str = "",
    full_name: str = "",
    file_path: str = "",
    max_matches: Optional[int] = None,
    **fetch_kwargs: Any,
) -> list:
    """Rows CONFIRMED in Python to be this entity's — no tolerant fallback.

    For the fetchers that assemble ONE entity's context (its chunk window,
    its same-file siblings): a token-superset sibling's rows interleaving
    the window is exactly the v0.2.101 defect class, and tolerance has no
    upside here — the caller already holds the winning row's own
    ``full_name`` / ``file_path``. Clauses are ANDed only when a value is
    given (``file_path=""`` → no clause, the pre-v0.2.75 C-8 behaviour);
    ``project`` narrows AND confirms by token sequence.
    """
    narrowing = []
    if full_name:
        narrowing.append(filter_cls.by_property("full_name").equal(full_name))
    if file_path:
        narrowing.append(filter_cls.by_property("file_path").equal(file_path))
    if not narrowing:
        return []
    flt = narrowing[0]
    for extra in narrowing[1:]:
        flt = flt & extra
    flt = _and_project(filter_cls, flt, project)
    accept = _wem.exact_row_predicate(
        values={"full_name": full_name},
        paths={"file_path": file_path},
        same_tokens={"project": project},
    )
    # Module-attribute call — see the note in `code_identity_rows`.
    return _wem.fetch_matching_rows(
        coll, flt, accept, max_matches=max_matches, **fetch_kwargs,
    )




