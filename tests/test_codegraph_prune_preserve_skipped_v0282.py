# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for v0.2.82 — `--prune-stale` must not delete rows of
files that were SKIPPED (not re-walked) this run.

Incident (2026-07-15, maintainer machine, v0.2.78→81 update): the per-project
bundle update fired `spawn_initial_build(prune_stale=true)` for every project.
For projects whose every file was current (per-file gate `_get_existing_module`
hit on path+hash+embed_revision), the walk short-circuited before any
`_dedup_insert` / `_create_or_update_module` call, so `visited_uuids` stayed
EMPTY — and the prune pass deleted the projects' ENTIRE code graphs
(MeetApp: 399 modules + 1477 functions → 0 objects; Instambul1860: 436 → 0).

Fix under test: the dispatcher records every DISCOVERED file; the module-write
choke-point records every WALKED file; `discovered − walked` (the skipped-but-
present set) is passed to `_prune_collection`, which preserves rows anchored to
those paths. All tests are pure-Python (no Weaviate).

Fail-without/pass-with: on pre-fix analyzers `_prune_collection` has no
`preserve_paths` parameter and `_prune_stale_objects` prunes everything
unvisited — `test_all_files_skipped_wipes_nothing` reproduces the incident
shape and FAILS on the pre-fix code.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest


_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_v0282_prune_preserve_analyze_code_graph", str(_ANALYZER_PATH)
    )
    if spec is None or spec.loader is None:
        pytest.fail(f"Analyzer module missing: {_ANALYZER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.fail("weaviate-client not installed — CI env regression")
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer_module()


# ---------------------------------------------------------------------------
# Fakes (mirror test_codegraph_language_scoped_prune.py, plus a schema config
# so the anchor-prop probe finds `path` / `file_path`).
# ---------------------------------------------------------------------------


class _FakeConfig:
    def __init__(self, prop_names: List[str]) -> None:
        self._props = [SimpleNamespace(name=n) for n in prop_names]

    def get(self) -> Any:
        return SimpleNamespace(properties=self._props)


class _FakeData:
    def __init__(self, deleted: List[str]) -> None:
        self._deleted = deleted

    def delete_by_id(self, uuid: str) -> None:
        self._deleted.append(uuid)


class _FakeCollection:
    """Weaviate v4 collection stand-in with rows + a schema config."""

    def __init__(self, name: str, rows: List[Any], prop_names: List[str]) -> None:
        self.name = name
        self.deleted: List[str] = []
        self.data = _FakeData(self.deleted)
        self.config = _FakeConfig(prop_names)
        self._rows = rows

    def iterator(self, return_properties=None):  # noqa: ANN001 — mirror client
        return iter(self._rows)


class _NoConfigCollection(_FakeCollection):
    """Legacy collection whose schema probe raises (anchor unknown)."""

    def __init__(self, name: str, rows: List[Any]) -> None:
        super().__init__(name, rows, [])
        del self.config  # config.get() → AttributeError → probe soft-fails


def _row(uid: str, project: str, anchor_prop: str, anchor: str) -> Any:
    return SimpleNamespace(uuid=uid, properties={"project": project, anchor_prop: anchor})


def _make_analyzer(analyzer_mod: types.ModuleType, project: str = "TestProject"):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = project
    inst.client = None
    inst.module_cache = {}
    inst.class_cache = {}
    inst.function_cache = {}
    inst.module_imports = {}
    inst.visited_uuids = set()
    inst._track_visited = True
    inst._current_language = ""
    inst._progress_emitter = None
    inst._prune_language = ""
    inst._prune_discovered_paths = set()
    inst._prune_walked_paths = set()
    return inst


# ---------------------------------------------------------------------------
# 1. THE INCIDENT SHAPE — every file skipped, nothing visited → wipe on
#    pre-fix code; zero deletions with the fix.
# ---------------------------------------------------------------------------


def test_all_files_skipped_wipes_nothing(analyzer_mod) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    # Dispatcher discovered both files; neither was walked (per-file gate hit).
    analyzer._prune_discovered_paths = {"src/a.js", "src/b.js"}
    analyzer._prune_walked_paths = set()

    modules = _FakeCollection(
        "Test_CodeModule",
        [_row("m-a", "TestProject", "path", "src/a.js"),
         _row("m-b", "TestProject", "path", "src/b.js")],
        ["project", "path"],
    )
    functions = _FakeCollection(
        "Test_CodeFunction",
        [_row("f-a1", "TestProject", "file_path", "src/a.js"),
         _row("f-b1", "TestProject", "file_path", "src/b.js")],
        ["project", "file_path"],
    )
    analyzer.modules_collection = modules
    analyzer.classes_collection = None
    analyzer.functions_collection = functions
    analyzer.apis_collection = None
    analyzer.interactions_collection = None

    pruned = analyzer._prune_stale_objects()

    assert pruned == 0, (
        f"All-skipped run must prune NOTHING (2026-07-15 wipe incident), "
        f"pruned={pruned}, deleted={modules.deleted + functions.deleted}"
    )
    assert modules.deleted == [] and functions.deleted == []


# ---------------------------------------------------------------------------
# 2. Deleted-from-disk rows are still pruned (file never discovered).
# ---------------------------------------------------------------------------


def test_vanished_file_rows_still_pruned(analyzer_mod) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    analyzer._prune_discovered_paths = {"src/kept.py"}
    analyzer._prune_walked_paths = set()  # kept.py skipped as unchanged

    functions = _FakeCollection(
        "Test_CodeFunction",
        [_row("f-kept", "TestProject", "file_path", "src/kept.py"),
         _row("f-gone", "TestProject", "file_path", "src/deleted.py")],
        ["project", "file_path"],
    )
    pruned, failures = analyzer._prune_collection(
        functions, visited_uuids=set(),
        preserve_paths=(analyzer._prune_discovered_paths
                        - analyzer._prune_walked_paths),
    )
    assert failures == 0
    assert pruned == 1 and functions.deleted == ["f-gone"], (
        "Rows of files gone from disk must still be reaped; rows of "
        f"skipped-but-present files must survive. deleted={functions.deleted}"
    )


# ---------------------------------------------------------------------------
# 3. Re-walked file: unvisited leftover rows are still pruned (entity was
#    removed from the file) — the preserve set must NOT cover walked files.
# ---------------------------------------------------------------------------


def test_rewalked_file_leftovers_still_pruned(analyzer_mod) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    analyzer._prune_discovered_paths = {"src/mod.py"}
    analyzer._prune_walked_paths = {"src/mod.py"}  # re-walked this run

    functions = _FakeCollection(
        "Test_CodeFunction",
        [_row("f-live", "TestProject", "file_path", "src/mod.py"),
         _row("f-removed", "TestProject", "file_path", "src/mod.py")],
        ["project", "file_path"],
    )
    pruned, _failures = analyzer._prune_collection(
        functions, visited_uuids={"f-live"},
        preserve_paths=(analyzer._prune_discovered_paths
                        - analyzer._prune_walked_paths),  # = empty
    )
    assert pruned == 1 and functions.deleted == ["f-removed"]


# ---------------------------------------------------------------------------
# 4. Anchor-less collection (schema probe fails): conservative — preserved
#    while skips are in play; old visited-only semantics on a full re-walk.
# ---------------------------------------------------------------------------


def test_anchorless_collection_conservative_with_skips(analyzer_mod) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    rows = [SimpleNamespace(uuid="i-1", properties={"project": "TestProject"})]
    inter = _NoConfigCollection("Test_CodeInteraction", rows)

    pruned, _ = analyzer._prune_collection(
        inter, visited_uuids=set(), preserve_paths={"src/skipped.py"},
    )
    assert pruned == 0 and inter.deleted == [], (
        "Anchor-less rows must be preserved when any file was skipped "
        "(no provenance → never guess 'stale')"
    )


def test_anchorless_collection_old_semantics_on_full_walk(analyzer_mod) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    rows = [SimpleNamespace(uuid="i-1", properties={"project": "TestProject"})]
    inter = _NoConfigCollection("Test_CodeInteraction", rows)

    pruned, _ = analyzer._prune_collection(
        inter, visited_uuids=set(), preserve_paths=set(),
    )
    assert pruned == 1 and inter.deleted == ["i-1"], (
        "Full re-walk (nothing skipped) keeps exact pre-v0.2.82 semantics"
    )


# ---------------------------------------------------------------------------
# 5. Windows-shaped legacy anchors match after `\` → `/` normalisation
#    (same lesson as the v0.2.81 manifest-separator incident).
# ---------------------------------------------------------------------------


def test_backslash_anchor_normalised_before_preserve_match(analyzer_mod) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    functions = _FakeCollection(
        "Test_CodeFunction",
        [_row("f-win", "TestProject", "file_path", "src\\win.py")],
        ["project", "file_path"],
    )
    pruned, _ = analyzer._prune_collection(
        functions, visited_uuids=set(), preserve_paths={"src/win.py"},
    )
    assert pruned == 0 and functions.deleted == []


# ---------------------------------------------------------------------------
# 6. Anchor-prop selection: CodeModule keys on `path`, others on `file_path`
#    — a module row anchored via `path` is preserved.
# ---------------------------------------------------------------------------


def test_module_collection_uses_path_anchor(analyzer_mod) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    modules = _FakeCollection(
        "Test_CodeModule",
        [_row("m-1", "TestProject", "path", "src/a.py")],
        ["project", "path"],
    )
    pruned, _ = analyzer._prune_collection(
        modules, visited_uuids=set(), preserve_paths={"src/a.py"},
    )
    assert pruned == 0 and modules.deleted == []


# ---------------------------------------------------------------------------
# 7. Review blocker 1 — `--incremental --prune-stale` must not wipe: the
#    PRE-filter discovery is recorded via `_record_discovered_path`.
# ---------------------------------------------------------------------------


def test_record_discovered_path_gated_and_normalized(analyzer_mod) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    root = Path("/repo")

    # Gated off → no-op (non-prune runs pay nothing).
    analyzer._track_visited = False
    analyzer._record_discovered_path(root / "src" / "a.py", root)
    assert analyzer._prune_discovered_paths == set()

    # Gated on → POSIX rel recorded (the stored-anchor shape).
    analyzer._track_visited = True
    analyzer._record_discovered_path(root / "src" / "a.py", root)
    assert analyzer._prune_discovered_paths == {"src/a.py"}

    # File outside the root (relative_to raises) → normalized fallback,
    # never an exception (a bad path must not break the walk).
    analyzer._record_discovered_path(Path("/elsewhere/b.py"), root)
    assert "/elsewhere/b.py" in analyzer._prune_discovered_paths


def test_incremental_branch_records_prefilter_discovery_source_shape() -> None:
    """Structural guard (same pattern as the P9 source-shape pins): inside
    the `if incremental:` branch of `analyze_repository`, the PRE-filter
    `all_files` list is recorded via `_record_discovered_path` BEFORE
    `_filter_changed_files` runs. Without this ordering, `--incremental
    --prune-stale` (live combo: orchestrator_core.rs
    `code_graph_reanalyze_current`) deletes every unchanged file's rows —
    the 2026-07-15 wipe class again.
    """
    src = _ANALYZER_PATH.read_text(encoding="utf-8")
    inc_idx = src.find("all_files = files")
    assert inc_idx != -1, "incremental branch anchor missing"
    record_idx = src.find("self._record_discovered_path(_f_disc, source_root)", inc_idx)
    filter_idx = src.find("self._filter_changed_files(", inc_idx)
    assert record_idx != -1, (
        "incremental branch no longer records the PRE-filter discovery — "
        "`--incremental --prune-stale` would wipe unchanged files' rows"
    )
    assert filter_idx != -1 and record_idx < filter_idx, (
        "PRE-filter discovery recording must run BEFORE _filter_changed_files"
    )
