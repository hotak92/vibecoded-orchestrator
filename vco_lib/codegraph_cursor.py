# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE whole-class listing: cursor pages from the nil uuid, with a verdict.

v0.2.107 (CG-2, A-002). A listing that names no cursor returned 0 rows on a
class that held 391,000 (seen on Weaviate 1.28.4; the same read from the nil
uuid cursor returned them). Every sweep, prune, purge, copy and convergence
check read those 0 rows as "the class is empty": nothing was deleted, the run
said "converged", and the ledger entry that kept the work owed was cleared.

Two rules live here, so no caller can get either one wrong:

* **Always a cursor.** :func:`iter_class` pages with ``fetch_objects(after=...)``
  starting at :data:`NIL_UUID`; no page is ever requested without ``after``.
  :func:`rest_first_page` is the same rule for the REST sample reads.
* **Zero rows is a claim that gets checked.** A pass that yields no row asks
  :func:`vco_lib.weaviate_helpers.http_count_objects` for the class count:

  ============================  =========================================
  rows listed / count           outcome
  ============================  =========================================
  1 or more rows                the rows; the loop ends normally
  0 rows, count ``0``           truly empty: the loop ends normally
  0 rows, count positive        :class:`CursorUndeterminable`
  0 rows, count ``None``        :class:`CursorUndeterminable`
  a page request fails          :class:`CursorUndeterminable` (also mid-way)
  ============================  =========================================

THE CONTRACT FOR CALLERS. A loop over :func:`iter_class` that ends normally
has seen the whole class. :class:`CursorUndeterminable` means "this class could
not be read", never "empty" and never "converged": the caller deletes nothing
on the strength of that pass, clears no ledger entry, stamp or marker, and
leaves the work owed. It is an ``Exception``, so a caller that catches broadly
must make its ``except`` arm the conservative one - never ``pass`` followed by
a conclusion.

Every raise is logged ONCE here (class + call site + why) and remembered for
the process (:func:`undeterminable_classes`), so a long run can end "partial"
and name the classes it could not read without each call site threading a
flag through.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterator, Optional, Sequence, Union

logger = logging.getLogger("vco_lib.codegraph_cursor")

#: The cursor every listing starts from. Weaviate returns the objects whose
#: uuid sorts AFTER the cursor, so the nil uuid means "from the first object".
NIL_UUID = "00000000-0000-0000-0000-000000000000"

#: Rows per page (the client's own iterator uses the same size).
DEFAULT_PAGE_SIZE = 100


class CursorUndeterminable(RuntimeError):
    """A class listing that could not be trusted - see the module docstring.

    ``class_name`` and ``call_site`` name where; ``reason`` says why.
    """

    def __init__(self, class_name: str, call_site: str, reason: str):
        self.class_name = class_name
        self.call_site = call_site
        self.reason = reason
        super().__init__(
            f"listing of {class_name or '?'} undeterminable at {call_site}: {reason}"
        )


#: ``{class_name: call_site}`` of every undeterminable listing in this process.
_UNDETERMINABLE: "dict[str, str]" = {}


def undeterminable_classes() -> "dict[str, str]":
    """``{class_name: call_site}`` of every listing that raised
    :class:`CursorUndeterminable` since :func:`reset_undeterminable`."""
    return dict(_UNDETERMINABLE)


def reset_undeterminable() -> None:
    """Forget the recorded classes (the start of a run; a test)."""
    _UNDETERMINABLE.clear()


def _undeterminable(coll_name: str, call_site: str, reason: str) -> CursorUndeterminable:
    """Log the one line, remember the class, build the exception."""
    _UNDETERMINABLE[coll_name or "?"] = call_site
    logger.warning(
        "UNDETERMINABLE listing: class=%s call_site=%s (%s) - this read "
        "authorises no delete, no clear and no 'converged'; the work stays owed",
        coll_name or "?", call_site, reason,
    )
    return CursorUndeterminable(coll_name, call_site, reason)


def _fetch_page(coll: Any, after: Any, limit: int, kwargs: "dict[str, Any]") -> "tuple[list, Any]":
    """ONE page: ``(rows, cursor for the next page)``. The cursor is the last
    row's uuid; ``None`` when the page is empty."""
    res = coll.query.fetch_objects(limit=limit, after=after, **kwargs)
    rows = list(getattr(res, "objects", None) or [])
    return rows, (getattr(rows[-1], "uuid", None) if rows else None)


def _class_count(coll: Any, weaviate_url: Optional[str]) -> Optional[int]:
    """The class's object count through the ONE count home; ``None`` =
    unreadable (never 0)."""
    from vco_lib import weaviate_helpers as _wh

    return _wh.http_count_objects(str(getattr(coll, "name", "") or ""), weaviate_url)


def _zero_rows_verdict(name: str, call_site: str, count: Optional[int]) -> None:
    """Return when ``count`` confirms the class is empty; raise otherwise."""
    if count == 0 and not isinstance(count, bool):
        return
    if count is None:
        raise _undeterminable(
            name, call_site, "0 rows listed and the object count could not be read")
    raise _undeterminable(
        name, call_site, f"0 rows listed but the class holds {count} object(s)")


def iter_class(
    coll: Any,
    call_site: str,
    *,
    return_properties: Optional[Sequence[str]],
    include_vector: Union[bool, Sequence[str]] = False,
    return_references: Any = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    weaviate_url: Optional[str] = None,
) -> Iterator[Any]:
    """Yield every object of ``coll`` once, in cursor pages from the nil uuid.

    ``return_properties`` is required: name the properties the caller reads
    (``[]`` = none). ``None`` returns every property and is for whole-row
    copies only. ``include_vector`` takes ``[slot, ...]`` to read only those
    named vectors (``True`` = all of them). ``return_references`` takes
    ``QueryReference`` objects, each with its own property list.
    ``weaviate_url`` is where the count cross-check asks (default: the
    machine's Weaviate, the one every client here connects to).

    Raises :class:`CursorUndeterminable` as the module docstring's table says.
    A consumer that stops early (``break``) gets no verdict and needs none.
    Rows already yielded before a mid-way failure were really read; what the
    failure withdraws is the claim that the class was seen whole.
    """
    name = str(getattr(coll, "name", "") or "")
    kwargs: "dict[str, Any]" = {}
    if return_properties is not None:
        kwargs["return_properties"] = list(return_properties)
    if include_vector:
        kwargs["include_vector"] = (
            True if include_vector is True else list(include_vector))
    if return_references is not None:
        kwargs["return_references"] = return_references
    after: Any = NIL_UUID
    seen = 0
    while True:
        try:
            rows, after = _fetch_page(coll, after, max(1, int(page_size)), kwargs)
        except Exception as exc:  # noqa: BLE001 - any failed page withdraws the verdict
            raise _undeterminable(
                name, call_site,
                f"page request failed after {seen} row(s): {type(exc).__name__}: {exc}",
            ) from exc
        if not rows:
            break
        seen += len(rows)
        yield from rows
        if after is None:
            raise _undeterminable(
                name, call_site, f"a row without a uuid after {seen} row(s); cannot page on")
    if seen == 0:
        _zero_rows_verdict(name, call_site, _class_count(coll, weaviate_url))


def rest_first_page(
    class_name: str,
    call_site: str,
    *,
    limit: int,
    include_vector: bool = False,
    weaviate_url: Optional[str] = None,
    request: Any = None,
    timeout: float = 10.0,
) -> "list[dict]":
    """Up to ``limit`` objects of ``class_name`` over REST, from the nil uuid
    cursor - the sample read (first stored objects), as raw REST dicts.

    Same verdict as :func:`iter_class`: ``[]`` only when the count confirms the
    class is empty; :class:`CursorUndeterminable` for a failed request, a
    non-200, an unreadable body, or 0 objects on a class that is not (or cannot
    be shown to be) empty. ``request`` is the transport
    (:func:`vco_lib.weaviate_helpers.http_request` by default).
    """
    from vco_lib import weaviate_helpers as _wh

    base = (weaviate_url or _wh.weaviate_url_default()).rstrip("/")
    url = f"{base}/v1/objects?class={class_name}&limit={int(limit)}&after={NIL_UUID}"
    if include_vector:
        url += "&include=vector"
    try:
        status, body = (request or _wh.http_request)("GET", url, timeout=timeout)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        objs = json.loads(body.decode("utf-8")).get("objects") or []
        if not isinstance(objs, list):
            raise RuntimeError("the reply carries no object list")
    except Exception as exc:  # noqa: BLE001 - a failed sample is no verdict
        raise _undeterminable(
            class_name, call_site, f"REST page failed: {type(exc).__name__}: {exc}",
        ) from exc
    if not objs:
        _zero_rows_verdict(
            class_name, call_site,
            _wh.http_count_objects(class_name, base, request=request),
        )
    return objs
