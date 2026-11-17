"""Test doubles for ``vco_lib.codegraph_cursor`` (v0.2.107, CG-2).

Two things live here.

**1. ``CursorClass`` - a fake collection that pages like Weaviate.** It
implements ``query.fetch_objects(limit=, after=, ...)`` with real cursor
semantics (rows sorted by uuid, ``after`` exclusive) and records every page
request. Its knobs reproduce the defect and the failure modes:

* ``cursorless_first_page_empty`` - what the bug looked like from the old code
  path: a page asked WITHOUT a cursor (``after=None``) returns nothing.
* ``hide_rows`` - every page returns nothing, cursor or not (the class "lists
  empty"), while ``count`` (what the count helper is told) stays what the test
  sets: positive, ``0`` or ``None``.
* ``fail_on_page`` - the N-th page request raises.

Its count is served without HTTP: the suite-wide fixture (below) points
``codegraph_cursor._class_count`` at ``CursorClass.count``.

**2. The legacy-fake adapter** (:func:`install_legacy_adapter`, switched on
for the whole suite by ``tests/conftest.py``). Hundreds of pre-CG-2 fakes
model a collection as ``iterator(return_properties=...)`` - the method the
production code no longer calls. Rewriting each to speak ``fetch_objects(after=)``
would change nothing about what those tests check (the sweep logic given the
rows), so the adapter serves the cursor's pages from the fake's own
``iterator``: same rows, same order, and an exception raised by the fake's
iterator surfaces on the page that reaches it. For such a fake "0 rows" means
"the fake holds 0 rows", so the count cross-check answers 0. A fake that sets
``cursor_native = True`` (``CursorClass`` does) is never adapted - it is
driven through the real paging code and the real zero-row verdict.
"""
from __future__ import annotations

import inspect
import uuid as _uuid
from types import SimpleNamespace
from typing import Any, Iterable, Optional

from vco_lib import codegraph_cursor as cc

NIL = cc.NIL_UUID


def make_uuid(n: int) -> str:
    """A deterministic uuid whose sort order follows ``n`` (n >= 1)."""
    return str(_uuid.UUID(int=n))


class CursorRow(SimpleNamespace):
    """One stored object: ``uuid``, ``properties``, ``vector``, ``references``."""


def row(n: int, properties: Optional[dict] = None, vector: Any = None,
        references: Any = None) -> CursorRow:
    return CursorRow(uuid=make_uuid(n), properties=dict(properties or {}),
                     vector=vector, references=references)


class _Query:
    def __init__(self, owner: "CursorClass"):
        self._owner = owner

    def fetch_objects(self, *, limit=None, after=None, filters=None, **kwargs):
        owner = self._owner
        owner.page_requests.append(
            {"limit": limit, "after": after, "filters": filters, **kwargs})
        if filters is not None:
            return owner.filtered_reply(filters, limit, kwargs)
        if owner.fail_on_page and len(owner.listing_pages()) == owner.fail_on_page:
            raise RuntimeError("page request failed (fake)")
        if owner.hide_rows or (after is None and owner.cursorless_first_page_empty):
            return SimpleNamespace(objects=[])
        rows = sorted(owner.rows.values(), key=lambda r: str(r.uuid))
        if after is not None:
            rows = [r for r in rows if str(r.uuid) > str(after)]
        return SimpleNamespace(objects=rows[: (limit or len(rows))])


class _Data:
    def __init__(self, owner: "CursorClass"):
        self._owner = owner

    def delete_by_id(self, uuid=None, **_kw):
        self._owner.deleted.append(str(uuid))
        self._owner.rows.pop(str(uuid), None)
        return True

    def update(self, uuid=None, properties=None, **_kw):
        self._owner.updated.append((str(uuid), dict(properties or {})))
        target = self._owner.rows.get(str(uuid))
        if target is not None:
            target.properties.update(properties or {})


class CursorClass:
    """A fake collection with real cursor paging - see the module docstring."""

    cursor_native = True

    def __init__(self, name: str, rows: Iterable[CursorRow] = (), *,
                 properties: Iterable[str] = (), count: Any = "auto",
                 cursorless_first_page_empty: bool = False,
                 hide_rows: bool = False, fail_on_page: int = 0):
        self.name = name
        self.rows = {str(r.uuid): r for r in rows}
        self._props = list(properties)
        self._count = count
        self.cursorless_first_page_empty = cursorless_first_page_empty
        self.hide_rows = hide_rows
        self.fail_on_page = fail_on_page
        self.page_requests: list = []
        self.deleted: list = []
        self.updated: list = []
        self.query = _Query(self)
        self.data = _Data(self)
        self.config = SimpleNamespace(get=lambda: SimpleNamespace(
            properties=[SimpleNamespace(name=p) for p in self._all_props()]))

    def _all_props(self) -> list:
        if self._props:
            return self._props
        names: list = []
        for r in self.rows.values():
            for k in r.properties:
                if k not in names:
                    names.append(k)
        return names

    @property
    def count(self) -> Optional[int]:
        """What the count helper reports: the stored rows unless overridden."""
        return len(self.rows) if self._count == "auto" else self._count

    def listing_pages(self) -> list:
        """The unfiltered page requests (the listing), in order."""
        return [p for p in self.page_requests if p.get("filters") is None]

    def filtered_reply(self, filters, limit, kwargs):  # noqa: ARG002
        """Filtered reads are not the listing; by default they find nothing
        (a subclass or a test may replace this)."""
        return SimpleNamespace(objects=[])

    def iterator(self, *a, **k):  # pragma: no cover - the regression tripwire
        raise AssertionError(
            f"{self.name}: iterator() was called - every listing must go "
            "through vco_lib.codegraph_cursor")


class CursorClient:
    """``client.collections.get/exists`` over a dict of :class:`CursorClass`."""

    def __init__(self, classes: Iterable[CursorClass] = ()):
        self._classes = {c.name: c for c in classes}
        self.collections = SimpleNamespace(
            get=self._get, exists=lambda n: n in self._classes,
            use=self._get)
        self.closed = False

    def _get(self, name):
        if name not in self._classes:
            raise KeyError(name)
        return self._classes[name]

    def close(self):
        self.closed = True


# -- the legacy-fake adapter -------------------------------------------------

_REAL_FETCH_PAGE = cc._fetch_page
_REAL_CLASS_COUNT = cc._class_count


class _LegacyPass:
    """One listing pass over a legacy fake's ``iterator``."""

    def __init__(self, coll: Any, kwargs: dict):
        self.coll = coll  # held so id(coll) cannot be reused while we live
        self.it = iter(_call_iterator(coll, kwargs))
        self.pending: Optional[BaseException] = None
        self.served = 0
        self.done = False


def _call_iterator(coll: Any, kwargs: dict):
    """Call the fake's ``iterator`` with the keywords it accepts."""
    fn = coll.iterator
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(**kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(**kwargs)
    return fn(**{k: v for k, v in kwargs.items() if k in params})


def _is_legacy(coll: Any) -> bool:
    if getattr(coll, "cursor_native", False) is True:
        return False
    if type(coll).__module__.split(".")[0] == "weaviate":
        return False
    return callable(getattr(coll, "iterator", None))


def install_legacy_adapter():
    """Serve cursor pages for pre-CG-2 fakes from their ``iterator``.
    Returns the callable that undoes it."""
    passes: dict = {}

    def fetch_page(coll, after, limit, kwargs):
        if not _is_legacy(coll):
            return _REAL_FETCH_PAGE(coll, after, limit, kwargs)
        if after == NIL:
            passes[id(coll)] = _LegacyPass(coll, kwargs)
        state = passes[id(coll)]
        if state.pending is not None:
            exc, state.pending = state.pending, None
            raise exc
        page: list = []
        while len(page) < limit and not state.done:
            try:
                page.append(next(state.it))
            except StopIteration:
                state.done = True
            except Exception as exc:  # noqa: BLE001 - replayed on the right page
                state.done = True
                if not page:
                    raise
                state.pending = exc
        state.served += len(page)
        return page, ("legacy-cursor" if page else None)

    def class_count(coll, weaviate_url):
        if getattr(coll, "cursor_native", False) is True and hasattr(coll, "count"):
            return coll.count  # CursorClass, or a suite's own cursor-speaking fake
        if _is_legacy(coll):
            state = passes.get(id(coll))
            return state.served if state is not None else None
        return _REAL_CLASS_COUNT(coll, weaviate_url)

    saved = (cc._fetch_page, cc._class_count)
    cc._fetch_page, cc._class_count = fetch_page, class_count
    cc.reset_undeterminable()

    def restore() -> None:
        cc._fetch_page, cc._class_count = saved
        cc.reset_undeterminable()

    return restore
