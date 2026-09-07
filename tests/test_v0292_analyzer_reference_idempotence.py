# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — ``create_cross_references`` must not multiply stored beacons.

``data.reference_add`` is NOT idempotent: Weaviate stores every call as its own
beacon. The pre-fix pass called it once per discovered edge with no existence
check, so every re-analysis of a file re-wrote all of that file's edges. Live
data on the maintainer machine shows what that costs: 1518 ``imports`` beacons
on one module for 4 distinct targets, 506 identical ``extends`` beacons on one
class, 5 beacons for the single ``vco_lib/rl_archive.py -> vco_lib/paths.py``
edge. Storage grew on every incremental analyze, forever, and every consumer of
those references saw N copies of one edge.

Both halves of the decision are pinned here (repo rule — test the act AND the
leave-alone):

* **act** — a first walk over a fresh collection CREATES the edges;
* **leave-alone** — a second walk over the SAME unchanged content creates
  nothing and leaves the stored beacon count exactly where it was.

The fake store below is beacon-faithful: ``reference_add`` appends
unconditionally, exactly like Weaviate, and reads hand back the REAL
``_CrossReference`` wrapper. A store that deduplicated on write, or that
returned plain lists, would make this file green against the defect.
"""
from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_SRC = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"

_CrossReference = pytest.importorskip(
    "weaviate.collections.classes.internal"
)._CrossReference


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_acg_v0292", str(ANALYZER_SRC))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:  # pragma: no cover - env regression
        pytest.fail("analyzer refused to import (weaviate-client / vco_lib missing)")
    return mod


# ---------------------------------------------------------------------------
# A beacon-faithful Weaviate stand-in
# ---------------------------------------------------------------------------
class _StoredRow:
    def __init__(self, uuid: str, properties: Dict[str, Any]) -> None:
        self.uuid = uuid
        self.properties = dict(properties)
        # property name -> list of target uuids, DUPLICATES ALLOWED (Weaviate
        # stores one beacon per reference_add call).
        self.beacons: Dict[str, List[str]] = {}


class _View:
    """What a query hands back: uuid + properties + resolved references."""

    def __init__(self, row: _StoredRow, references: Any) -> None:
        self.uuid = row.uuid
        self.properties = dict(row.properties)
        self.references = references


class _FakeCollection:
    def __init__(self, name: str, rows: Dict[str, _StoredRow],
                 targets: Optional[Dict[str, _StoredRow]] = None) -> None:
        self.name = name
        self.rows = rows
        # Where beacon targets are looked up when resolving references (same
        # collection for imports/extends/calls in this fixture).
        self._targets = targets if targets is not None else rows
        self.add_calls: List[tuple] = []
        self.query = _FakeQuery(self)
        self.data = _FakeData(self)

    def _resolve(self, row: _StoredRow, link_on: Optional[str]) -> Any:
        if link_on is None:
            return None
        uuids = row.beacons.get(link_on)
        if uuids is None:
            # Weaviate returns no entry at all for a link with no beacons.
            return {}
        return {
            link_on: _CrossReference._from(
                [_View(self._targets[u], None) for u in uuids]
            )
        }

    def iterator(self, return_references=None, **_kw):
        link = getattr(return_references, "link_on", None)
        for row in self.rows.values():
            yield _View(row, self._resolve(row, link))


class _FakeQuery:
    def __init__(self, coll: _FakeCollection) -> None:
        self._coll = coll

    def fetch_object_by_id(self, uuid, return_references=None, **_kw):
        row = self._coll.rows.get(str(uuid))
        if row is None:
            return None
        link = getattr(return_references, "link_on", None)
        return _View(row, self._coll._resolve(row, link))

    def fetch_objects(self, filters=None, limit=None, return_references=None, **_kw):
        """Property-equality filtering, enough for the pass's own queries.

        Kept even though the current code point-reads by uuid: it lets this
        harness drive the PRE-fix implementation too, which is what makes the
        red-proof of these tests meaningful rather than an AttributeError.
        """
        link = getattr(return_references, "link_on", None)
        target = getattr(filters, "target", None)
        value = getattr(filters, "value", None)
        rows = [
            r for r in self._coll.rows.values()
            if target is None or r.properties.get(target) == value
        ]
        if limit is not None:
            rows = rows[:limit]
        return types.SimpleNamespace(
            objects=[_View(r, self._coll._resolve(r, link)) for r in rows]
        )


class _FakeData:
    def __init__(self, coll: _FakeCollection) -> None:
        self._coll = coll

    def update(self, uuid, properties):
        self._coll.rows[str(uuid)].properties.update(properties)

    def reference_add(self, from_uuid, from_property, to):
        # Unconditional append — the real client's behaviour, and the reason
        # the defect existed.
        row = self._coll.rows[str(from_uuid)]
        row.beacons.setdefault(from_property, []).append(str(to))
        self._coll.add_calls.append((str(from_uuid), from_property, str(to)))


# ---------------------------------------------------------------------------
# Fixture graph: pkg/a.py imports pkg/b.py (via TWO names that resolve to the
# same row), pkg.Child extends pkg.Base, mod.caller calls mod.target.
# ---------------------------------------------------------------------------
def _build_world():
    modules = {
        "m-a": _StoredRow("m-a", {
            "path": "pkg/a.py",
            "project_source": "",
            "import_names": ["b", "pkg.b"],  # both resolve to pkg/b.py
        }),
        "m-b": _StoredRow("m-b", {
            "path": "pkg/b.py",
            "project_source": "",
        }),
    }
    classes = {
        "c-child": _StoredRow("c-child", {
            "full_name": "pkg.Child",
            "signature": "class Child(Base)",
        }),
        "c-base": _StoredRow("c-base", {
            "full_name": "pkg.Base",
            "signature": "class Base",
        }),
    }
    functions = {
        "f-caller": _StoredRow("f-caller", {
            "full_name": "mod.caller",
            "function_body": "def caller():\n    target()\n",
            "language": "python",
            "file_path": "pkg/a.py",
            "total_chunks": 1,
        }),
        "f-target": _StoredRow("f-target", {
            "full_name": "mod.target",
            "function_body": "def target():\n    return 1\n",
            "language": "python",
            "file_path": "pkg/b.py",
            "total_chunks": 1,
        }),
    }
    return (
        _FakeCollection("P_CodeModule", modules),
        _FakeCollection("P_CodeClass", classes),
        _FakeCollection("P_CodeFunction", functions),
    )


def _analyzer(analyzer_mod, mods, classes, funcs):
    """A fresh analyzer bound to the SAME collections (i.e. the same stored
    state) — the whole-collection cache scan runs for real."""
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.client = object()  # truthy
    inst.module_cache = {}
    inst.class_cache = {}
    inst.function_cache = {}
    inst.module_imports = {}
    inst.modules_collection = mods
    inst.classes_collection = classes
    inst.functions_collection = funcs
    return inst


def _beacons(coll: _FakeCollection, uuid: str, prop: str) -> List[str]:
    return coll.rows[uuid].beacons.get(prop, [])


# ---------------------------------------------------------------------------
# ACT — the first walk creates the edges
# ---------------------------------------------------------------------------
def test_first_walk_creates_each_edge_exactly_once(analyzer_mod) -> None:
    mods, classes, funcs = _build_world()
    stats = _analyzer(analyzer_mod, mods, classes, funcs).create_cross_references()

    assert _beacons(mods, "m-a", "imports") == ["m-b"], (
        "two import names resolving to the same module row must yield ONE beacon"
    )
    assert _beacons(classes, "c-child", "extends") == ["c-base"]
    assert _beacons(funcs, "f-caller", "calls") == ["f-target"]
    assert (stats["imports"], stats["extends"], stats["calls"]) == (1, 1, 1)


# ---------------------------------------------------------------------------
# LEAVE-ALONE — the second walk over unchanged content writes nothing
# ---------------------------------------------------------------------------
def test_second_walk_adds_no_beacons(analyzer_mod) -> None:
    mods, classes, funcs = _build_world()
    _analyzer(analyzer_mod, mods, classes, funcs).create_cross_references()

    mods.add_calls.clear()
    classes.add_calls.clear()
    funcs.add_calls.clear()

    stats = _analyzer(analyzer_mod, mods, classes, funcs).create_cross_references()

    assert _beacons(mods, "m-a", "imports") == ["m-b"], "imports doubled"
    assert _beacons(classes, "c-child", "extends") == ["c-base"], "extends doubled"
    assert _beacons(funcs, "f-caller", "calls") == ["f-target"], "calls doubled"

    assert mods.add_calls == [], "a no-op re-analyze must not write import refs"
    assert classes.add_calls == [], "a no-op re-analyze must not write extends refs"
    assert funcs.add_calls == [], "a no-op re-analyze must not write call refs"
    assert (stats["imports"], stats["extends"], stats["calls"]) == (0, 0, 0)


def test_ten_walks_do_not_grow_the_stored_set(analyzer_mod) -> None:
    """The reported symptom was unbounded growth across many analyses — this
    is the shape that produced 66 (and 1518) beacons for one edge."""
    mods, classes, funcs = _build_world()
    for _ in range(10):
        _analyzer(analyzer_mod, mods, classes, funcs).create_cross_references()

    assert len(_beacons(mods, "m-a", "imports")) == 1
    assert len(_beacons(classes, "c-child", "extends")) == 1
    assert len(_beacons(funcs, "f-caller", "calls")) == 1


# ---------------------------------------------------------------------------
# A genuinely new edge still lands (the fix must not become "never write")
# ---------------------------------------------------------------------------
def test_a_new_edge_is_still_created_on_a_later_walk(analyzer_mod) -> None:
    mods, classes, funcs = _build_world()
    _analyzer(analyzer_mod, mods, classes, funcs).create_cross_references()

    # A new module appears and pkg/a.py grows an import of it.
    mods.rows["m-c"] = _StoredRow("m-c", {"path": "pkg/c.py", "project_source": ""})
    mods.rows["m-a"].properties["import_names"] = ["b", "c"]

    stats = _analyzer(analyzer_mod, mods, classes, funcs).create_cross_references()

    assert _beacons(mods, "m-a", "imports") == ["m-b", "m-c"]
    assert stats["imports"] == 1, "only the NEW edge is written"


def test_existing_duplicate_beacons_are_not_multiplied_further(analyzer_mod) -> None:
    """Beacons already stored are left as they are — collapsing them is a
    separate, consent-gated data operation — but the pass must not add to
    them."""
    mods, classes, funcs = _build_world()
    mods.rows["m-a"].beacons["imports"] = ["m-b"] * 66

    stats = _analyzer(analyzer_mod, mods, classes, funcs).create_cross_references()

    assert len(_beacons(mods, "m-a", "imports")) == 66
    assert stats["imports"] == 0
    assert mods.add_calls == []


# ---------------------------------------------------------------------------
# The reads that make the above possible are sourced from the ONE cache scan,
# not from per-object point reads (the v0.2.74 R4 read-amplification rule).
# ---------------------------------------------------------------------------
def test_stored_beacons_come_from_the_single_cache_scan(analyzer_mod) -> None:
    mods, classes, funcs = _build_world()
    inst = _analyzer(analyzer_mod, mods, classes, funcs)
    inst._populate_caches_from_weaviate()

    assert inst._xref_stored_import_refs == {"m-a": [], "m-b": []}
    assert inst._xref_stored_call_refs == {"f-caller": [], "f-target": []}

    mods.rows["m-a"].beacons["imports"] = ["m-b", "m-b"]
    inst2 = _analyzer(analyzer_mod, mods, classes, funcs)
    inst2._populate_caches_from_weaviate()
    assert inst2._xref_stored_import_refs["m-a"] == ["m-b", "m-b"], (
        "the scan must report the beacons AS STORED, duplicates included"
    )


def test_analyzer_uses_the_shared_reference_helpers(analyzer_mod) -> None:
    """One home: no third inline copy of the read/normalise logic."""
    from vco_lib import codegraph_references as shared

    assert analyzer_mod._reference_target_uuids is shared.reference_target_uuids
    assert analyzer_mod._add_missing_reference_edges is shared.add_missing_reference_edges


def test_no_unconditional_reference_add_remains() -> None:
    """Static guard: the three write sites must route through the shared
    add-if-absent helper, never call ``reference_add`` straight from the
    per-edge loop again."""
    src = ANALYZER_SRC.read_text(encoding="utf-8")
    code = "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "data.reference_add(" not in code, (
        "an unconditional reference_add is the pre-v0.2.92 defect — go through "
        "vco_lib.codegraph_references.add_missing_reference_edges"
    )
    assert code.count("_add_missing_reference_edges(") == 3, (
        "expected exactly three add-if-absent write sites (calls/extends/imports)"
    )
