# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit pins for the P2f stage-3 (v0.2.77 Part 6) pure-producer writer.

``CodeGraphAnalyzer.write_file_extraction(fx)`` is the ONE side-effect owner the
pure ``extract_<lang>_file`` producers feed. Before any language migrates to the
pure contract, these tests pin the writer's lifecycle against fake collections
wired exactly like the golden harness (UUID-keyed recording store, no Weaviate,
stubbed embeds):

  * the module row is written FIRST (via ``_create_or_update_module``) and its
    UUID is stamped onto every entity's ``module`` reference;
  * class / function UUIDs land in ``class_cache`` / ``function_cache`` keyed by
    ``full_name`` — the same cache writes ``_extract_class`` / ``_extract_function``
    performed imperatively;
  * ``module_imports`` is populated from ``fx.imports`` (python cross-ref input);
  * interactions are written LAST, with the fresh module UUID, and their count
    is folded into ``stats['interactions']``;
  * a module-less ``FileExtraction`` (the walk-time skip shape) is a total
    no-op with stats returned verbatim.
"""
from __future__ import annotations

import importlib.util
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

from vco_lib.codegraph_entities import (
    CodeEntity,
    FileExtraction,
    InteractionGroup,
    KIND_CLASS,
    KIND_FUNCTION,
    ModuleDescriptor,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_codegraph_writer_acg", str(_ANALYZER_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── Fake collections mirroring the golden harness (UUID-keyed, upsert) ──────
class _FakeCollectionData:
    def __init__(self, store: Dict[str, Dict[str, Any]]) -> None:
        self._store = store

    def replace(self, uuid: str, **kwargs: Any) -> None:
        self._store[str(uuid)] = kwargs
        return None

    def insert(self, uuid: str, **kwargs: Any) -> str:
        self._store[str(uuid)] = kwargs
        return str(uuid)

    def update(self, uuid: str, **kwargs: Any) -> None:
        existing = self._store.setdefault(str(uuid), {})
        props = existing.setdefault("properties", {})
        props.update(kwargs.get("properties", {}))
        return None


class _FakeCollection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.store: Dict[str, Dict[str, Any]] = {}
        self.data = _FakeCollectionData(self.store)


_PROJECT = "WriterProj"


def _wire(analyzer_mod: types.ModuleType) -> Any:
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = _PROJECT
    inst.client = object()
    inst.module_cache = {}
    inst.class_cache = {}
    inst.function_cache = {}
    inst.module_imports = {}
    inst.visited_uuids = set()
    inst._track_visited = False
    inst._current_language = ""
    inst._current_source = ""
    inst.modules_collection = _FakeCollection(f"{_PROJECT}_CodeModule")
    inst.classes_collection = _FakeCollection(f"{_PROJECT}_CodeClass")
    inst.functions_collection = _FakeCollection(f"{_PROJECT}_CodeFunction")
    inst.apis_collection = _FakeCollection(f"{_PROJECT}_CodeAPI")
    inst.interactions_collection = _FakeCollection(f"{_PROJECT}_CodeInteraction")
    return inst


def _stub_embeddings(analyzer_mod: types.ModuleType) -> None:
    analyzer_mod.generate_embedding = lambda text: None
    analyzer_mod.embed_module = lambda summary: None
    analyzer_mod.embed_function = lambda sig, body, language="python": None
    analyzer_mod.embed_class = lambda sig, body, methods=None, language="python": None


def _mtime() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _sample_extraction() -> FileExtraction:
    rel = "pkg/mod.py"
    cls = CodeEntity(
        kind=KIND_CLASS, file_path_rel=rel,
        name="Foo", full_name="mod.Foo",
        body="class Foo:\n    pass", signature="class Foo", doc="",
        start_line=1, end_line=2, project=_PROJECT,
        extras={"methods": ["bar"], "field_types": [], "composes": []},
    )
    fn = CodeEntity(
        kind=KIND_FUNCTION, file_path_rel=rel,
        name="bar", full_name="mod.Foo.bar",
        body="def bar(self):\n    return 1", signature="bar(self)", doc="",
        start_line=2, end_line=3, is_async=False, project=_PROJECT,
        extras={"type_uses": []},
    )
    return FileExtraction(
        module=ModuleDescriptor(
            path=rel, language="Python", loc=3, complexity=1.0,
            last_modified=_mtime(), file_hash="deadbeef",
            imports=["os", "sys"], module_summary="Module: pkg/mod.py",
        ),
        entities=[cls, fn],
        interactions=[],
        imports=["os", "sys"],
        stats={"modules": 1, "classes": 1, "functions": 1},
    )


def test_writer_writes_module_first_and_stamps_references(analyzer_mod, monkeypatch):
    _stub_embeddings(analyzer_mod)
    analyzer = _wire(analyzer_mod)

    fx = _sample_extraction()
    stats = analyzer.write_file_extraction(fx)

    # Module row landed.
    assert len(analyzer.modules_collection.store) == 1
    module_uuid = next(iter(analyzer.modules_collection.store))

    # Both entities landed and reference the freshly-written module UUID.
    class_rows = list(analyzer.classes_collection.store.values())
    func_rows = list(analyzer.functions_collection.store.values())
    assert len(class_rows) == 1 and len(func_rows) == 1
    assert class_rows[0]["references"]["module"] == module_uuid
    assert func_rows[0]["references"]["module"] == module_uuid

    # stats passed through verbatim.
    assert stats == {"modules": 1, "classes": 1, "functions": 1}


def test_writer_populates_caches_by_full_name(analyzer_mod, monkeypatch):
    _stub_embeddings(analyzer_mod)
    analyzer = _wire(analyzer_mod)

    analyzer.write_file_extraction(_sample_extraction())

    assert "mod.Foo" in analyzer.class_cache
    assert analyzer.class_cache["mod.Foo"], "class UUID must be captured"
    assert "mod.Foo.bar" in analyzer.function_cache
    assert analyzer.function_cache["mod.Foo.bar"], "function UUID must be captured"
    # module_imports populated from fx.imports keyed on the module path.
    assert analyzer.module_imports["pkg/mod.py"] == ["os", "sys"]


def test_writer_writes_interactions_last_with_module_uuid(analyzer_mod, monkeypatch):
    _stub_embeddings(analyzer_mod)
    analyzer = _wire(analyzer_mod)

    fx = _sample_extraction()
    fx.interactions = [
        InteractionGroup(
            interactions=[{
                "source": "mod.Foo.bar", "interaction_type": "http",
                "protocol": "requests", "endpoint": "example.com",
                "confidence": "high",
            }],
            language="Python",
        )
    ]
    stats = analyzer.write_file_extraction(fx)

    ix_rows = list(analyzer.interactions_collection.store.values())
    assert len(ix_rows) == 1
    module_uuid = next(iter(analyzer.modules_collection.store))
    assert ix_rows[0]["references"]["source_module"] == module_uuid
    # count folded into stats.
    assert stats["interactions"] == 1


def test_writer_moduleless_extraction_is_a_noop(analyzer_mod, monkeypatch):
    _stub_embeddings(analyzer_mod)
    analyzer = _wire(analyzer_mod)

    fx = FileExtraction(module=None, stats={"modules": 0, "classes": 0, "functions": 0})
    stats = analyzer.write_file_extraction(fx)

    assert stats == {"modules": 0, "classes": 0, "functions": 0}
    assert not analyzer.modules_collection.store
    assert not analyzer.classes_collection.store
    assert not analyzer.functions_collection.store
    assert not analyzer.class_cache
    assert not analyzer.function_cache
    assert not analyzer.module_imports
