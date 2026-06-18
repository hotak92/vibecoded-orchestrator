# SPDX-License-Identifier: AGPL-3.0-or-later
"""Track E (v0.2.61): per-object content-hash tombstone-skip in _dedup_insert.

Background — the disk-write leak this fix targets:
  The code-graph collections accumulated thousands of HNSW tombstones, driving
  a Weaviate cleanup-spin loop that wrote GB/hour to disk. Root cause: when a
  source file changed by even one line, the per-FILE skip
  (`_get_existing_module`, keyed on path + file_hash) correctly fired only for
  byte-identical FILES — so a 1-function edit in a 50-function file re-`replace()`'d
  ALL 50 functions, and each `replace()` of a vector-bearing object TOMBSTONES
  the old HNSW vector node. 49 of those 50 replaces were needless.

Fix under test:
  `_dedup_insert` now stamps a stable `content_hash` on each object and, before
  replacing, point-reads the existing object's stored hash by its deterministic
  UUID. When the hash matches (object unchanged), it SKIPS the replace() — no
  tombstone, no write. Layered UNDER the per-file fast path, so it only ever
  runs for objects in files that genuinely changed.

Correctness contract (the four cases this file pins):
  (a) unchanged object (stored hash == computed hash) → replace() NOT called.
  (b) changed object   (stored hash != computed hash) → replace() called.
  (c) absent object / missing-or-empty stored hash      → falls through to write.
  (d) read error on the hash fetch                       → falls through to write
                                                            (NEVER a silent skip).

A skipped-but-changed object would mean a stale code graph, so every uncertain
branch MUST write. These tests assert that bias explicitly.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest


# ---------------------------------------------------------------------------
# Load the analyzer module (same pattern as test_analyze_code_graph_v0_2_16)
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_tracke_analyze_code_graph", str(_ANALYZER_PATH)
    )
    if spec is None or spec.loader is None:
        pytest.fail(
            f"Analyzer module file missing from repo — CI env regression: {_ANALYZER_PATH}"
        )
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.fail(
            "weaviate-client package not installed — CI env regression (required dependency missing)"
        )
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer_module()


# ---------------------------------------------------------------------------
# Fake Weaviate collection that supports the per-object skip's read path.
# ---------------------------------------------------------------------------


class _FakeObject:
    """Stand-in for weaviate ObjectSingleReturn — only `.properties` matters."""

    def __init__(self, properties: Dict[str, Any]) -> None:
        self.properties = properties


class _FakeQuery:
    """Stand-in for collection.query.

    `fetch_object_by_id` returns the pre-seeded object for the given UUID, or
    None when absent (mirrors the weaviate v4 contract). When `raise_on_fetch`
    is set, it raises — exercising the read-error fall-through (case d).
    """

    def __init__(self, store: Dict[str, Dict[str, Any]], raise_on_fetch: bool = False) -> None:
        self._store = store
        self._raise_on_fetch = raise_on_fetch
        self.fetch_calls: List[str] = []

    def fetch_object_by_id(self, uuid: str, return_properties=None) -> Optional[_FakeObject]:  # noqa: ARG002
        self.fetch_calls.append(str(uuid))
        if self._raise_on_fetch:
            raise RuntimeError("simulated transient read error")
        props = self._store.get(str(uuid))
        if props is None:
            return None
        return _FakeObject(props)


class _FakeCollectionData:
    """Records replace()/insert() calls so a test can assert (non-)invocation."""

    def __init__(self) -> None:
        self.replace_calls: List[Dict[str, Any]] = []
        self.insert_calls: List[Dict[str, Any]] = []

    def replace(self, uuid: str, **kwargs: Any) -> None:
        self.replace_calls.append({"uuid": str(uuid), **kwargs})
        return None

    def insert(self, uuid: str, **kwargs: Any) -> str:
        self.insert_calls.append({"uuid": str(uuid), **kwargs})
        return str(uuid)

    def delete_by_id(self, uuid: str) -> None:  # pragma: no cover - prune path
        pass


class _FakeCollection:
    """Weaviate v4 collection stand-in WITH a query namespace."""

    def __init__(
        self,
        name: str,
        store: Optional[Dict[str, Dict[str, Any]]] = None,
        raise_on_fetch: bool = False,
    ) -> None:
        self.name = name
        self.data = _FakeCollectionData()
        self.query = _FakeQuery(store if store is not None else {}, raise_on_fetch)


def _make_analyzer(analyzer_mod: types.ModuleType, project: str = "TestProject"):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = project
    inst.client = None
    inst.module_cache = {}
    inst.class_cache = {}
    inst.function_cache = {}
    inst.module_imports = {}
    inst.visited_uuids = set()
    inst._track_visited = False
    return inst


def _func_params() -> Dict[str, Any]:
    """A representative CodeFunction insert_params (vector-bearing)."""
    return {
        "properties": {
            "name": "foo",
            "full_name": "mod.foo",
            "function_body": "def foo():\n    return 1\n",
            "signature": "foo()",
            "type_uses": [],
            "cfg_summary": "",
            "data_flow_vars": [],
        },
        "vector": [0.1, 0.2, 0.3],
    }


# ---------------------------------------------------------------------------
# _content_hash_for_object — stability + sensitivity
# ---------------------------------------------------------------------------


def test_content_hash_stable_for_identical_content(analyzer_mod: types.ModuleType) -> None:
    """Same content (and irrelevant volatile fields varying) → same hash."""
    a = {
        "full_name": "mod.foo", "signature": "foo()",
        "function_body": "return 1", "type_uses": ["int"],
        "cfg_summary": "", "data_flow_vars": [],
        # Volatile / derived fields MUST NOT affect the hash:
        "last_modified": "2026-01-01T00:00:00Z",
        "project_source": "/some/root", "language": "python",
        "file_path": "src/mod.py", "start_line": 1, "end_line": 2,
    }
    b = dict(a)
    b["last_modified"] = "2026-12-31T23:59:59Z"
    b["project_source"] = "/a/totally/different/root"
    b["language"] = "PYTHON"
    b["file_path"] = "renamed/mod.py"
    b["start_line"] = 99
    b["end_line"] = 200
    h1 = analyzer_mod._content_hash_for_object("X_CodeFunction", a)
    h2 = analyzer_mod._content_hash_for_object("X_CodeFunction", b)
    assert h1 == h2, "Volatile/derived fields must be excluded from the hash"


def test_content_hash_changes_with_body(analyzer_mod: types.ModuleType) -> None:
    """A genuine body change must change the hash (else we'd skip a real edit)."""
    a = {"full_name": "mod.foo", "signature": "foo()", "function_body": "return 1"}
    b = {"full_name": "mod.foo", "signature": "foo()", "function_body": "return 2"}
    assert (
        analyzer_mod._content_hash_for_object("X_CodeFunction", a)
        != analyzer_mod._content_hash_for_object("X_CodeFunction", b)
    )


def test_content_hash_collection_scoped(analyzer_mod: types.ModuleType) -> None:
    """The collection base-name is mixed in so two collections never collide
    on the same digest for coincidentally-overlapping field values."""
    props = {"endpoint": "/x", "method": "GET", "path": "/x"}
    h_api = analyzer_mod._content_hash_for_object("P_CodeAPI", props)
    h_mod = analyzer_mod._content_hash_for_object("P_CodeModule", props)
    assert h_api != h_mod


# ---------------------------------------------------------------------------
# (a) unchanged object → replace() NOT called
# ---------------------------------------------------------------------------


def test_unchanged_object_is_skipped(analyzer_mod: types.ModuleType) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    params = _func_params()

    det_uuid = analyzer_mod._deterministic_uuid(
        analyzer.project_name, "src/mod.py", "mod.foo"
    )
    stored_hash = analyzer_mod._content_hash_for_object("T_CodeFunction", params["properties"])
    # Seed the store as if a prior run already indexed this exact object.
    coll = _FakeCollection(
        "T_CodeFunction", store={det_uuid: {"content_hash": stored_hash}}
    )

    out = analyzer._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")

    assert out == det_uuid, "Skip path must still return the deterministic UUID"
    assert coll.data.replace_calls == [], (
        "Unchanged object must NOT be replaced (no tombstone)"
    )
    assert coll.data.insert_calls == [], "Unchanged object must NOT be inserted"
    assert coll.query.fetch_calls == [det_uuid], (
        "Exactly one point-read by deterministic UUID was expected"
    )


# ---------------------------------------------------------------------------
# (b) changed object → replace() called
# ---------------------------------------------------------------------------


def test_changed_object_is_replaced(analyzer_mod: types.ModuleType) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    params = _func_params()

    det_uuid = analyzer_mod._deterministic_uuid(
        analyzer.project_name, "src/mod.py", "mod.foo"
    )
    # Stored hash is for an OLDER body → mismatch → must write.
    coll = _FakeCollection(
        "T_CodeFunction", store={det_uuid: {"content_hash": "stale-hash-of-old-body"}}
    )

    analyzer._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")

    assert len(coll.data.replace_calls) == 1, "Changed object must be replaced"
    # The written object must carry the freshly-computed hash for next run.
    written = coll.data.replace_calls[0]
    new_hash = analyzer_mod._content_hash_for_object("T_CodeFunction", params["properties"])
    assert written["properties"]["content_hash"] == new_hash


# ---------------------------------------------------------------------------
# (c) absent object / missing-or-empty stored hash → write (fall through)
# ---------------------------------------------------------------------------


def test_absent_object_falls_through_to_write(analyzer_mod: types.ModuleType) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    params = _func_params()
    # Empty store → fetch_object_by_id returns None → write.
    coll = _FakeCollection("T_CodeFunction", store={})

    analyzer._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")

    assert len(coll.data.replace_calls) == 1, "Absent object must be written"
    written = coll.data.replace_calls[0]
    assert written["properties"]["content_hash"], (
        "content_hash must be stamped on the written object"
    )


def test_pre_migration_row_missing_hash_falls_through(analyzer_mod: types.ModuleType) -> None:
    """A pre-v0.2.61 row exists but has NO/empty content_hash → unknown →
    fall through to write (one-time re-stamp), never skip."""
    analyzer = _make_analyzer(analyzer_mod)
    params = _func_params()
    det_uuid = analyzer_mod._deterministic_uuid(
        analyzer.project_name, "src/mod.py", "mod.foo"
    )
    # Object present but content_hash absent (None) — the pre-migration shape.
    coll = _FakeCollection("T_CodeFunction", store={det_uuid: {"content_hash": None}})

    analyzer._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")

    assert len(coll.data.replace_calls) == 1, (
        "Pre-migration row (no stored hash) must be re-written, not skipped"
    )


# ---------------------------------------------------------------------------
# (d) read error on hash fetch → write (NEVER a silent skip)
# ---------------------------------------------------------------------------


def test_read_error_falls_through_to_write(analyzer_mod: types.ModuleType) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    params = _func_params()
    # fetch_object_by_id raises → must fall through to write, never skip.
    coll = _FakeCollection("T_CodeFunction", store={}, raise_on_fetch=True)

    analyzer._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")

    assert len(coll.data.replace_calls) == 1, (
        "A read error must NEVER cause a skip — fall through to write (fail-safe)"
    )


def test_no_query_namespace_falls_through_to_write(analyzer_mod: types.ModuleType) -> None:
    """A collection object WITHOUT a `.query` attribute (older/mocked client)
    must not crash and must fall through to write."""
    analyzer = _make_analyzer(analyzer_mod)
    params = _func_params()

    class _NoQueryCollection:
        def __init__(self) -> None:
            self.name = "T_CodeFunction"
            self.data = _FakeCollectionData()
            # deliberately no `.query`

    coll = _NoQueryCollection()
    analyzer._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")
    assert len(coll.data.replace_calls) == 1


# ---------------------------------------------------------------------------
# Prune-safety: a skipped (unchanged) object is still recorded as visited.
# ---------------------------------------------------------------------------


def test_skipped_object_still_marked_visited(analyzer_mod: types.ModuleType) -> None:
    """When --prune-stale tracking is on, a SKIPPED unchanged object must still
    be added to visited_uuids — otherwise a concurrent prune would delete the
    live row we deliberately didn't re-write."""
    analyzer = _make_analyzer(analyzer_mod)
    analyzer._track_visited = True
    params = _func_params()
    det_uuid = analyzer_mod._deterministic_uuid(
        analyzer.project_name, "src/mod.py", "mod.foo"
    )
    stored_hash = analyzer_mod._content_hash_for_object("T_CodeFunction", params["properties"])
    coll = _FakeCollection(
        "T_CodeFunction", store={det_uuid: {"content_hash": stored_hash}}
    )

    analyzer._dedup_insert(coll, params, "mod.foo", file_path_rel="src/mod.py")

    assert coll.data.replace_calls == [], "must be skipped"
    assert ("T_CodeFunction", det_uuid) in analyzer.visited_uuids, (
        "A skipped live object must still count as visited for prune-safety"
    )
