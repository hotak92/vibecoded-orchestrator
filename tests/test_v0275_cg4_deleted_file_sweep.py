# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.75 P2a (CG-4): residual-orphan window — current-revision deleted files.

The v0.2.74 orphan-clear only scans collections that hold STALE-revision rows. A
file removed from disk (``git rm``, no editor hook) leaves its rows at the
CURRENT ``embed_revision`` — ``classify_row`` calls them ``not_owed`` (correctly:
they ARE embed-converged) and the stale-only scan never sees them, so they keep
surfacing in ``search_code_graph`` forever.

P2a closes the window with a reachability-only sweep in
``_build_stale_file_set`` → ``_clear_deleted_primary_rows`` that runs ONLY on a
whole-repo walk and deletes PRIMARY-source rows whose path no longer resolves on
disk — regardless of revision. These tests pin:

  * ACT: whole-repo walk + a current-revision row for a deleted file → deleted.
  * LEAVE-ALONE: an extra-path current-revision orphan → NEVER deleted (B1).
  * LEAVE-ALONE: single-file / --only-files-from walk (whole_repo flag off) →
    the sweep never runs; the current-revision orphan is preserved.
  * LEAVE-ALONE: a current-revision row whose file STILL EXISTS → kept.
  * fail-open: repo_root None → no sweep, no delete.

Pure unit — fakes record delete_by_id; no Weaviate. Reuses the shared fakes'
shape from test_v0274_orphan_clear.py.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


@pytest.fixture(scope="module")
def analyzer_mod():
    spec = importlib.util.spec_from_file_location("_acg_cg4_sweep", str(_ANALYZER_PATH))
    assert spec and spec.loader, f"analyzer missing: {_ANALYZER_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _Obj:
    def __init__(self, uuid, props):
        self.uuid = uuid
        self.properties = props


class _FakeData:
    def __init__(self):
        self.deleted = []
        self.fail_uuids = set()

    def delete_by_id(self, uuid):
        if uuid in self.fail_uuids:
            raise RuntimeError(f"simulated delete failure for {uuid}")
        self.deleted.append(uuid)


class _FakeProp:
    def __init__(self, name):
        self.name = name


class _FakeConfig:
    def __init__(self, prop_names):
        self.properties = [_FakeProp(n) for n in prop_names]


class _FakeConfigHolder:
    def __init__(self, prop_names):
        self._cfg = _FakeConfig(prop_names)

    def get(self):
        return self._cfg


class _FakeColl:
    def __init__(self, name, rows, prop_names, agg_count=None):
        self.name = name
        self._rows = rows
        self.data = _FakeData()
        self.config = _FakeConfigHolder(prop_names)
        self.iter_calls = 0
        self.aggregate = types.SimpleNamespace(
            over_all=lambda **kw: types.SimpleNamespace(total_count=agg_count)
        )

    def iterator(self, return_properties=None):
        self.iter_calls += 1
        return iter(list(self._rows))


def _bind(analyzer_mod, obj, names):
    cls = analyzer_mod.CodeGraphAnalyzer
    for name in names:
        setattr(obj, name, getattr(cls, name).__get__(obj, obj.__class__))


class _SweepStub:
    """Binds the real gate + the new P2a sweep. ``whole_repo`` toggles the flag
    the sweep gates on; ``repo_root_raw`` mirrors what analyze_repository stamps."""

    def __init__(self, analyzer_mod, modules, classes, functions, repo_root,
                 *, whole_repo=True):
        self.modules_collection = modules
        self.classes_collection = classes
        self.functions_collection = functions
        if repo_root is not None:
            self._analyze_repo_root = repo_root
            self._analyze_repo_root_raw = repo_root.as_posix()
        self._analyze_whole_repo = whole_repo
        _bind(
            analyzer_mod, self,
            (
                "_build_stale_file_set", "_get_stale_file_set",
                "_count_stale_rows_in_collection", "_clear_deleted_primary_rows",
            ),
        )


def _make_repo(tmp_path, existing_rel_paths):
    for rel in existing_rel_paths:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# real\n")
    return tmp_path


def _funcs(rows, agg_count, prop_names=("file_path", "embed_revision", "project_source")):
    return _FakeColl("P_CodeFunction", rows, prop_names, agg_count=agg_count)


def _empty_module_class():
    modules = _FakeColl("P_CodeModule", [], ("path", "embed_revision"), agg_count=0)
    classes = _FakeColl("P_CodeClass", [], ("file_path", "embed_revision"), agg_count=0)
    return modules, classes


# ─────────────────── ACT: current-revision deleted file ───────────────────


def test_whole_repo_sweep_deletes_current_revision_orphan(analyzer_mod, tmp_path):
    """The CG-4 repro: `git rm pkg/gone.py` leaves a CURRENT-revision row. On a
    whole-repo walk, the sweep deletes it even though the stale scan reports 0."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    root = _make_repo(tmp_path, ["pkg/live.py"])  # gone.py deleted
    rows = [
        _Obj("live", {"file_path": "pkg/live.py", "embed_revision": rev,
                      "project_source": root.as_posix()}),
        _Obj("orphan", {"file_path": "pkg/gone.py", "embed_revision": rev,
                        "project_source": root.as_posix()}),
    ]
    # agg_count=0 → the stale-only scan short-circuits (converged). The sweep
    # still runs on the whole-repo walk and catches the current-revision orphan.
    functions = _funcs(rows, agg_count=0)
    modules, classes = _empty_module_class()
    stub = _SweepStub(analyzer_mod, modules, classes, functions, root, whole_repo=True)

    stub._build_stale_file_set()

    assert functions.data.deleted == ["orphan"], (
        f"current-revision deleted-file row must be swept; got {functions.data.deleted}"
    )


def test_whole_repo_sweep_keeps_existing_current_revision_row(analyzer_mod, tmp_path):
    """LEAVE-ALONE: a current-revision row whose file STILL EXISTS is kept."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    root = _make_repo(tmp_path, ["pkg/live.py"])
    rows = [
        _Obj("live", {"file_path": "pkg/live.py", "embed_revision": rev,
                      "project_source": root.as_posix()}),
    ]
    functions = _funcs(rows, agg_count=0)
    modules, classes = _empty_module_class()
    stub = _SweepStub(analyzer_mod, modules, classes, functions, root, whole_repo=True)

    stub._build_stale_file_set()

    assert functions.data.deleted == [], "reachable current-revision row preserved"


def test_whole_repo_sweep_over_delete_guard(analyzer_mod, tmp_path):
    """Exact-compare: gone.py (deleted) + gone_helper.py (exists) share tokens →
    only gone.py swept."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    root = _make_repo(tmp_path, ["src/foo/gone_helper.py"])  # gone.py absent
    rows = [
        _Obj("gone", {"file_path": "src/foo/gone.py", "embed_revision": rev,
                      "project_source": root.as_posix()}),
        _Obj("helper", {"file_path": "src/foo/gone_helper.py", "embed_revision": rev,
                        "project_source": root.as_posix()}),
    ]
    functions = _funcs(rows, agg_count=0)
    modules, classes = _empty_module_class()
    stub = _SweepStub(analyzer_mod, modules, classes, functions, root, whole_repo=True)

    stub._build_stale_file_set()

    assert functions.data.deleted == ["gone"], "only the exact deleted path"


# ─────────────────── LEAVE-ALONE: extra-path rows (B1) ───────────────────


def test_whole_repo_sweep_never_deletes_extra_path_rows(analyzer_mod, tmp_path):
    """B1: an extra-path CURRENT-revision row whose file is absent under the
    PRIMARY root must NOT be swept — it converges on its own root's walk and this
    walk cannot judge its reachability."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    root = _make_repo(tmp_path, [])  # nothing under primary
    rows = [
        # primary current-revision orphan → sweep it
        _Obj("prim", {"file_path": "pkg/gone.py", "embed_revision": rev,
                      "project_source": root.as_posix()}),
        # extra-path row: absent under primary but belongs to a sibling clone → KEEP
        _Obj("extra", {"file_path": "lib/util.py", "embed_revision": rev,
                       "project_source": "/some/sibling/clone"}),
        # legacy row (no source stamp) → treated as primary → eligible
        _Obj("legacy", {"file_path": "old/legacy.py", "embed_revision": rev,
                        "project_source": ""}),
    ]
    functions = _funcs(rows, agg_count=0)
    modules, classes = _empty_module_class()
    stub = _SweepStub(analyzer_mod, modules, classes, functions, root, whole_repo=True)

    stub._build_stale_file_set()

    assert "extra" not in functions.data.deleted, (
        f"B1: extra-path row must survive the whole-repo sweep; got {functions.data.deleted}"
    )
    assert set(functions.data.deleted) == {"prim", "legacy"}


# ─────────────────── LEAVE-ALONE: narrow-scope walks ───────────────────


def test_single_file_walk_does_not_sweep(analyzer_mod, tmp_path):
    """A single-file / --only-files-from walk (whole_repo flag off) must NOT run
    the collection-wide sweep — it touches only the named file(s), so a
    current-revision orphan for a DIFFERENT (unnamed) deleted file is preserved."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    root = _make_repo(tmp_path, [])
    rows = [
        _Obj("orphan", {"file_path": "pkg/gone.py", "embed_revision": rev,
                        "project_source": root.as_posix()}),
    ]
    functions = _funcs(rows, agg_count=0)
    modules, classes = _empty_module_class()
    stub = _SweepStub(analyzer_mod, modules, classes, functions, root, whole_repo=False)

    stub._build_stale_file_set()

    assert functions.data.deleted == [], (
        "narrow-scope walk must not sweep the whole collection"
    )


def test_sweep_fail_open_when_repo_root_none(analyzer_mod, tmp_path):
    """repo_root unset → the sweep is skipped entirely (never delete on
    uncertainty)."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    rows = [
        _Obj("orphan", {"file_path": "pkg/gone.py", "embed_revision": rev,
                        "project_source": ""}),
    ]
    functions = _funcs(rows, agg_count=0)
    modules, classes = _empty_module_class()
    stub = _SweepStub(analyzer_mod, modules, classes, functions, None, whole_repo=True)

    stub._build_stale_file_set()

    assert functions.data.deleted == [], "no sweep without a repo root"


def test_sweep_pathless_rows_left_to_classifier(analyzer_mod, tmp_path):
    """A pathless CURRENT-revision row is NOT swept by P2a (the classifier-routed
    orphan-clear owns pathless purging); the P2a sweep only handles path-bearing
    deleted files."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    root = _make_repo(tmp_path, [])
    rows = [
        _Obj("pathless", {"file_path": "", "embed_revision": rev,
                          "project_source": root.as_posix()}),
    ]
    functions = _funcs(rows, agg_count=0)
    modules, classes = _empty_module_class()
    stub = _SweepStub(analyzer_mod, modules, classes, functions, root, whole_repo=True)

    stub._build_stale_file_set()

    assert functions.data.deleted == [], "P2a sweep ignores pathless rows"


def test_sweep_delete_failure_feeds_prune_failures(analyzer_mod, tmp_path):
    """A sweep delete_by_id failure feeds the prune-failure accounting (the same
    status chain the orphan-clear uses) and never raises."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    root = _make_repo(tmp_path, [])
    rows = [
        _Obj("orphan", {"file_path": "pkg/gone.py", "embed_revision": rev,
                        "project_source": root.as_posix()}),
    ]
    functions = _funcs(rows, agg_count=0)
    functions.data.fail_uuids = {"orphan"}
    modules, classes = _empty_module_class()
    stub = _SweepStub(analyzer_mod, modules, classes, functions, root, whole_repo=True)
    stub._prune_failures = 0

    result = stub._build_stale_file_set()

    assert functions.data.deleted == []  # the delete failed
    assert result is not None  # never raised
    assert int(getattr(stub, "_prune_failures", 0)) == 1, (
        "sweep failure must feed the prune-failure accounting"
    )
