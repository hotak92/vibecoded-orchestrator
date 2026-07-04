# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 READ-amp: migrations/codegraph_collection/6_to_7.py — the one-time
purge of ``.claude/state/`` transient-scratch rows.

SAFETY is the whole point of these tests. The edge deletes production rows, so
the invariant under test is: it removes EVERY row whose raw ``file_path`` (or
``path``) literally contains ``.claude/state/`` and NOT ONE row that doesn't —
including rows a naive Weaviate ``Like`` filter would wrongly match.

The data-loss trap this guards against (live-diagnosed 2026-07-04): ``file_path``
is ``tokenization=word``, so a ``delete_many(file_path Like "*.claude/state/*")``
matches on the TOKENS ``claude``/``state`` and would have deleted ~5.5k REAL
``analyze_code_graph.py`` functions (whose flattened backup COPIES share those
tokens). The edge therefore matches an EXACT PYTHON SUBSTRING on the value read
back, never a ``Like``. These tests pin that.

Pure unit — a fake collection records delete_by_id calls; no Weaviate.
"""
from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest


_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_EDGE_PATH = _REPO_ROOT / "migrations" / "codegraph_collection" / "6_to_7.py"


def _load_edge() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_edge_6_to_7", str(_EDGE_PATH))
    assert spec and spec.loader, f"edge module missing: {_EDGE_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def edge() -> types.ModuleType:
    return _load_edge()


class _Obj:
    def __init__(self, uuid: str, file_path: str) -> None:
        self.uuid = uuid
        self.properties = {"file_path": file_path}


class _FakeData:
    def __init__(self) -> None:
        self.deleted: list = []
        self.fail_uuids: set = set()

    def delete_by_id(self, uuid: str) -> None:
        if uuid in self.fail_uuids:
            raise RuntimeError(f"simulated delete failure for {uuid}")
        self.deleted.append(uuid)


class _FakeProp:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeConfig:
    def __init__(self, prop_names) -> None:
        self.properties = [_FakeProp(n) for n in prop_names]


class _FakeConfigHolder:
    def __init__(self, prop_names) -> None:
        self._cfg = _FakeConfig(prop_names)

    def get(self):
        return self._cfg


class _FakeCollection:
    def __init__(self, name: str, rows: list, prop_names=("file_path",)) -> None:
        self.name = name
        self._rows = rows
        self.data = _FakeData()
        self.config = _FakeConfigHolder(prop_names)

    def iterator(self, return_properties=None):
        # `return_properties` is honoured by the real client; the fake just
        # yields the rows it was seeded with (each already carries file_path).
        return iter(self._rows)


# The rows that MUST be deleted (raw file_path literally under .claude/state/).
_GARBAGE = [
    _Obj("g1", ".claude/state/tool_backups/20260701_200951___x_.wt_v0272_templates_scripts_analyze_code_graph.py"),
    _Obj("g2", ".claude/state/tool_backups/20260702_044842___y_launcher_src_project_state.py"),
    _Obj("g3", ".claude/state/scratch.py"),
    _Obj("g4", "some/nested/.claude/state/tool_backups/z.py"),  # nested extra-path root
]
# The rows that MUST be preserved (no .claude/state/ in the raw path) — note
# several share word-tokens with garbage (analyze/code/graph, launcher, state)
# to prove tokenization can't cause a false delete.
_REAL = [
    _Obj("r1", "templates/scripts/analyze_code_graph.py"),      # the exact real file
    _Obj("r2", ".claude/hooks/_lib/kg-sync-debounce.sh"),        # real .claude source, NOT state
    _Obj("r3", ".claude/scripts/kg-search.py"),                  # real .claude source, NOT state
    _Obj("r4", "launcher/src-tauri/src/commands/project_state_cmd.rs"),  # 'state' token, real
    _Obj("r5", "src/state_machine/store.py"),                    # 'state' in a dir name, real
]


def test_purge_deletes_only_exact_state_substring(edge):
    coll = _FakeCollection("P_CodeFunction", _GARBAGE + _REAL)
    deleted, failures = edge._purge_transient_rows(coll, "file_path")

    assert failures == 0
    assert deleted == len(_GARBAGE)
    assert set(coll.data.deleted) == {"g1", "g2", "g3", "g4"}
    # No real row was ever deleted.
    assert not (set(coll.data.deleted) & {"r1", "r2", "r3", "r4", "r5"})


def test_real_dot_claude_source_is_preserved(edge):
    """A real `.claude/hooks/` or `.claude/scripts/` file is NOT under
    `.claude/state/` and must survive — the marker is state-specific."""
    rows = [
        _Obj("keep1", ".claude/hooks/pre-tool-use.sh"),
        _Obj("keep2", ".claude/scripts/code-graph-query.py"),
        _Obj("kill1", ".claude/state/tool_backups/foo.py"),
    ]
    coll = _FakeCollection("P_CodeModule", rows)
    deleted, failures = edge._purge_transient_rows(coll, "file_path")
    assert failures == 0
    assert deleted == 1
    assert coll.data.deleted == ["kill1"]


def test_empty_and_missing_file_path_are_safe(edge):
    rows = [
        _Obj("n1", ""),        # empty string
        _Obj("n2", "real/file.py"),
    ]
    rows.append(type("O", (), {"uuid": "n3", "properties": None})())  # None props
    coll = _FakeCollection("P_CodeClass", rows)
    deleted, failures = edge._purge_transient_rows(coll, "file_path")
    assert (deleted, failures) == (0, 0)
    assert coll.data.deleted == []


def test_delete_failure_is_counted_not_swallowed(edge):
    coll = _FakeCollection("P_CodeFunction", list(_GARBAGE))
    coll.data.fail_uuids = {"g2"}  # one delete fails
    deleted, failures = edge._purge_transient_rows(coll, "file_path")
    assert failures == 1
    assert deleted == len(_GARBAGE) - 1
    assert "g2" not in coll.data.deleted


def test_marker_is_exact_substring_not_like(edge):
    """Guard the safety-critical constant + that the module never hands the
    marker to a Weaviate Like filter."""
    assert edge._TRANSIENT_MARKER == ".claude/state/"
    src = _EDGE_PATH.read_text(encoding="utf-8")
    # The edge must NOT use a Like filter or delete_many for the purge (those are
    # the tokenization-unsafe primitives this design deliberately rejects).
    assert ".like(" not in src, "6_to_7 must not use a Weaviate Like filter (word-tokenized -> unsafe)"
    assert "delete_many(" not in src, "6_to_7 must delete_by_id confirmed UUIDs, not delete_many(filter)"
    assert "delete_by_id" in src


def test_module_class_uses_path_prop(edge):
    """CodeModule keys the file on `path` (not `file_path`); the purge must work
    on whichever prop the class carries."""
    rows = [
        _Obj("m1", ".claude/state/tool_backups/mod.py"),
        _Obj("m2", "vco_lib/project_init.py"),
    ]
    # _Obj stores under 'file_path'; re-key to 'path' for this class.
    for o in rows:
        o.properties = {"path": o.properties["file_path"]}
    coll = _FakeCollection("P_CodeModule", rows, prop_names=("path",))
    deleted, failures = edge._purge_transient_rows(coll, "path")
    assert (deleted, failures) == (1, 0)
    assert coll.data.deleted == ["m1"]


def test_missing_path_prop_is_a_clean_skip(edge):
    """A class WITHOUT the expected path prop (e.g. CodeAPI has endpoint/handler,
    NOT file_path) must be a no-op, never a 500/crash. Regression guard for the
    live bug where CodeAPI/CodeInteraction were wrongly mapped to file_path."""
    rows = [_Obj("x", ".claude/state/tool_backups/foo.py")]
    coll = _FakeCollection("P_CodeAPI", rows, prop_names=("endpoint", "handler"))
    deleted, failures = edge._purge_transient_rows(coll, "file_path")
    assert (deleted, failures) == (0, 0)
    assert coll.data.deleted == []


def test_api_interaction_excluded_from_class_map(edge):
    """CodeAPI / CodeInteraction must NOT be in the purge map — they are not
    file-anchored on a path property (the live crash this fixed)."""
    assert "CodeAPI" not in edge._CLASS_PATH_PROP
    assert "CodeInteraction" not in edge._CLASS_PATH_PROP
    assert set(edge._CLASS_PATH_PROP) == {"CodeModule", "CodeFunction", "CodeClass"}


def test_edge_header_annotations(edge):
    """Runner cross-check contract: derived + non-destructive (no drop)."""
    src = _EDGE_PATH.read_text(encoding="utf-8")
    assert "# @classification: derived" in src
    assert "# @destructive: no" in src
    assert "# @idempotent: yes" in src
