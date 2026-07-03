# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""R-3 (v0.2.73 / C-2): per-file failure invalidates the module row.

Walkers stamp the MODULE row (new file_hash + current embed_revision) BEFORE
the file's entities. Pre-fix, a caught per-file failure (one transient
Weaviate 500 on entity #3 of 50) left the module row claiming "done at
current revision" → the per-file gate skipped the file on every future run →
the missing entities were permanently unreachable.

Covers:
  * ACT: `_invalidate_module_row` clears file_hash + stamps embed_revision 0
    (via this-run cache UUID AND via the path-query fallback).
  * LEAVE-ALONE: no module row on record → no update issued.
  * SOFT-FAIL: an update error never raises into the caller.
  * WIRING: analyze_repository's `_DedupInsertError` handler AND the generic
    per-file handler both call the compensation (through the real dispatch
    loop, not below it).
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).parent
_ANALYZER_PATH = _THIS_DIR.parent / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_v0273_r3_analyze_code_graph", str(_ANALYZER_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer()


class _FakeModulesColl:
    def __init__(self, query_objects=None, update_raises=False):
        self.updates = []
        self._raises = update_raises
        self.query = types.SimpleNamespace(
            fetch_objects=lambda **kw: types.SimpleNamespace(
                objects=query_objects or []
            )
        )
        self.data = types.SimpleNamespace(update=self._update)

    def _update(self, uuid, properties):
        if self._raises:
            raise RuntimeError("weaviate 500")
        self.updates.append({"uuid": uuid, "properties": properties})


class _Stub:
    def __init__(self, analyzer_mod, coll, module_cache=None):
        self.modules_collection = coll
        self.module_cache = module_cache or {}
        cls = analyzer_mod.CodeGraphAnalyzer
        self._invalidate_module_row = cls._invalidate_module_row.__get__(
            self, _Stub
        )


# ─────────────────────────── direct method ───────────────────────────


def test_act_via_cache_uuid(analyzer_mod, tmp_path):
    coll = _FakeModulesColl()
    stub = _Stub(analyzer_mod, coll, module_cache={"pkg/mod.py": "uuid-M"})
    stub._invalidate_module_row(tmp_path / "pkg" / "mod.py", tmp_path)
    assert len(coll.updates) == 1
    upd = coll.updates[0]
    assert upd["uuid"] == "uuid-M"
    assert upd["properties"]["file_hash"] == ""
    assert upd["properties"]["embed_revision"] == 0


def test_act_via_path_query_fallback(analyzer_mod, tmp_path):
    """Failure on a file whose module row predates this run (not cached):
    the path-query fallback finds and invalidates it."""
    row = types.SimpleNamespace(uuid="uuid-Q", properties={})
    coll = _FakeModulesColl(query_objects=[row])
    stub = _Stub(analyzer_mod, coll)
    stub._invalidate_module_row(tmp_path / "pkg" / "mod.py", tmp_path)
    assert len(coll.updates) == 1
    assert coll.updates[0]["uuid"] == "uuid-Q"


def test_leave_alone_when_no_module_row(analyzer_mod, tmp_path):
    """Failure BEFORE the module write: nothing on record → no update
    (the gate cannot wrongly skip a file that has no module row)."""
    coll = _FakeModulesColl(query_objects=[])
    stub = _Stub(analyzer_mod, coll)
    stub._invalidate_module_row(tmp_path / "pkg" / "mod.py", tmp_path)
    assert coll.updates == []


def test_soft_fail_on_update_error(analyzer_mod, tmp_path):
    coll = _FakeModulesColl(update_raises=True)
    stub = _Stub(analyzer_mod, coll, module_cache={"pkg/mod.py": "uuid-M"})
    # Must not raise into the caller.
    stub._invalidate_module_row(tmp_path / "pkg" / "mod.py", tmp_path)


def test_no_collection_is_noop(analyzer_mod, tmp_path):
    stub = _Stub(analyzer_mod, None)
    stub._invalidate_module_row(tmp_path / "x.py", tmp_path)  # no raise


# ─────────────────── wiring: through analyze_repository ───────────────────


def _run_with_failing_walker(analyzer_mod, tmp_path, exc_factory):
    """Drive the REAL analyze_repository dispatch loop with one python file
    whose walker raises; record `_invalidate_module_row` calls."""
    (tmp_path / "boom.py").write_text("def f():\n    return 1\n")
    analyzer = analyzer_mod.CodeGraphAnalyzer("TestProj")

    calls = []
    analyzer._invalidate_module_row = (  # type: ignore[method-assign]
        lambda f, root: calls.append((f, root))
    )
    analyzer._find_python_files = lambda root: [tmp_path / "boom.py"]

    def _boom(f, root):
        raise exc_factory()

    analyzer._analyze_python_file = _boom

    stats = analyzer.analyze_repository(tmp_path, language="python")
    return stats, calls


def test_dedup_insert_error_handler_invalidates(analyzer_mod, tmp_path):
    def _mk():
        return analyzer_mod._DedupInsertError(
            RuntimeError("500"), "TestProj_CodeFunction", "u"
        )

    stats, calls = _run_with_failing_walker(analyzer_mod, tmp_path, _mk)
    assert stats["insert_errors"] == 1
    assert len(calls) == 1, "insert-error handler must invalidate the module row"


def test_generic_error_handler_invalidates(analyzer_mod, tmp_path):
    stats, calls = _run_with_failing_walker(
        analyzer_mod, tmp_path, lambda: RuntimeError("parse blew up")
    )
    assert stats["files_skipped"] == 1
    assert len(calls) == 1, "generic per-file handler must invalidate too"


def test_successful_file_does_not_invalidate(analyzer_mod, tmp_path):
    """Leave-alone: a file that analyzes cleanly triggers NO compensation."""
    (tmp_path / "ok.py").write_text("def f():\n    return 1\n")
    analyzer = analyzer_mod.CodeGraphAnalyzer("TestProj")
    calls = []
    analyzer._invalidate_module_row = (  # type: ignore[method-assign]
        lambda f, root: calls.append(f)
    )
    analyzer._find_python_files = lambda root: [tmp_path / "ok.py"]
    analyzer._analyze_python_file = lambda f, root: {"modules": 1, "functions": 1}
    stats = analyzer.analyze_repository(tmp_path, language="python")
    assert stats["files_analyzed"] == 1
    assert calls == []
