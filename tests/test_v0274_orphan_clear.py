# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.74 R3 + D1 orphan-clear: the deleted-file convergence fix.

SAFETY is the whole point of these tests. Two DELETE paths on a word-tokenized
``file_path`` / ``path`` field are exercised, and the invariant is the same as
the 6_to_7 migration edge: delete EXACTLY the rows whose RAW stored path is
confirmed (in Python) to be an orphan, and NOT ONE sibling row a naive Weaviate
``Like`` / ``Equal`` filter would over-match on shared word tokens.

Covered:
  * D1 orphan-clear in ``analyze_code_graph._build_stale_file_set``: a stale row
    whose file is absent-on-disk is DELETED (delete_by_id); a stale row whose
    file exists is KEPT in the stale set, NOT deleted.
  * over-delete guard: ``src/foo/bar.py`` (deleted) + ``src/foo/baz.py`` (exists)
    with overlapping word tokens → ONLY bar.py deleted (proves exact-compare).
  * ``_prune_deleted_file_objects``: same over-delete guard + project /
    project_source scoping compared in Python.
  * fail-safe: repo_root None, or an existence check that raises → NO deletes.
  * R3 ``count_stale_rows``: aggregate stale==0 → returns 0 with NO per-row scan
    (iterator not called); aggregate stale>0 → per-row scan counts only
    REACHABLE stale rows (orphans excluded).

Pure unit — fakes record delete_by_id calls; no Weaviate.
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


# ─────────────────────── analyzer module loader ───────────────────────


@pytest.fixture(scope="module")
def analyzer_mod():
    """Load analyze_code_graph.py as a module (it is a template script, not an
    importable package member)."""
    spec = importlib.util.spec_from_file_location(
        "_acg_orphan_clear", str(_ANALYZER_PATH)
    )
    assert spec and spec.loader, f"analyzer missing: {_ANALYZER_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ─────────────────────── shared Weaviate fakes ───────────────────────


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
    """Collection fake: records delete_by_id + counts iterator scans."""

    def __init__(self, name, rows, prop_names, agg_count=None):
        self.name = name
        self._rows = rows
        self.data = _FakeData()
        self.config = _FakeConfigHolder(prop_names)
        self.iter_calls = 0
        self._agg_count = agg_count
        self.aggregate = types.SimpleNamespace(
            over_all=lambda **kw: types.SimpleNamespace(total_count=agg_count)
        )

    def iterator(self, return_properties=None):
        self.iter_calls += 1
        return iter(self._rows)


def _bind(analyzer_mod, obj, names):
    cls = analyzer_mod.CodeGraphAnalyzer
    for name in names:
        setattr(obj, name, getattr(cls, name).__get__(obj, obj.__class__))


class _StaleStub:
    """Binds the REAL gate + probe methods (incl. the D1 orphan-clear)."""

    def __init__(self, analyzer_mod, modules, classes, functions, repo_root):
        self.modules_collection = modules
        self.classes_collection = classes
        self.functions_collection = functions
        if repo_root is not None:
            self._analyze_repo_root = repo_root
        _bind(
            analyzer_mod, self,
            (
                "_build_stale_file_set", "_get_stale_file_set",
                "_count_stale_rows_in_collection", "_get_existing_module",
            ),
        )


def _make_repo(tmp_path, existing_rel_paths):
    """Create ``existing_rel_paths`` as real files under tmp_path; return root."""
    for rel in existing_rel_paths:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# real\n")
    return tmp_path


# ─────────────────── _path_reachable_on_disk (analyzer) ───────────────────


def test_reachable_true_for_existing_inside_root(analyzer_mod, tmp_path):
    root = _make_repo(tmp_path, ["src/foo/bar.py"])
    assert analyzer_mod._path_reachable_on_disk("src/foo/bar.py", root) is True


def test_reachable_false_for_absent_inside_root(analyzer_mod, tmp_path):
    root = _make_repo(tmp_path, ["src/foo/bar.py"])
    assert analyzer_mod._path_reachable_on_disk("src/foo/gone.py", root) is False


def test_reachable_false_for_escape_outside_root(analyzer_mod, tmp_path):
    """A ``../../etc/passwd`` escape resolves outside the root → NOT reachable
    (an orphan), even though the target may exist on disk."""
    root = _make_repo(tmp_path, ["src/foo/bar.py"])
    assert (
        analyzer_mod._path_reachable_on_disk("../../../../etc/passwd", root)
        is False
    )


def test_reachable_true_on_empty_path(analyzer_mod, tmp_path):
    """Empty path → cannot prove absence → keep (fail-safe)."""
    assert analyzer_mod._path_reachable_on_disk("", tmp_path) is True


def _patch_stat_for_target(monkeypatch, target_name, exc):
    """Monkeypatch os.stat so that any candidate path whose final component is
    ``target_name`` raises ``exc``; all other paths delegate to the real
    os.stat (so pytest's own internals keep working). ``cr.os`` is the shared
    stdlib ``os`` module, so the wrapper must be transparent for everything else
    AND accept the real signature (incl. ``follow_symlinks``)."""
    from vco_lib import codegraph_row_classify as cr

    real_stat = cr.os.stat

    def _wrapped(path, *args, **kwargs):
        if str(path).replace("\\", "/").rstrip("/").endswith(target_name):
            raise exc
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(cr.os, "stat", _wrapped)


def test_reachable_true_when_stat_raises_generic_oserror(
    analyzer_mod, tmp_path, monkeypatch
):
    """os.stat raising a generic OSError → indeterminate → reachable (no delete)."""
    root = _make_repo(tmp_path, ["src/foo/bar.py"])
    _patch_stat_for_target(
        monkeypatch, "bar.py", OSError("simulated stat failure")
    )
    assert (
        analyzer_mod._path_reachable_on_disk("src/foo/bar.py", root) is True
    )


def test_reachable_true_when_stat_raises_eacces(
    analyzer_mod, tmp_path, monkeypatch
):
    """Py>=3.13 fail-safe hole regression: a permission-denied file (EACCES) is
    INDETERMINATE, not absent — must classify as reachable so its rows are KEPT,
    never purged. (Path.exists() returns False on EACCES on 3.13+, which is the
    bug this test guards against.)"""
    import errno as _errno

    root = _make_repo(tmp_path, ["src/foo/bar.py"])
    _patch_stat_for_target(
        monkeypatch,
        "bar.py",
        PermissionError(_errno.EACCES, "Permission denied"),
    )
    assert analyzer_mod._path_reachable_on_disk("src/foo/bar.py", root) is True


def test_reachable_false_when_stat_raises_enoent(
    analyzer_mod, tmp_path, monkeypatch
):
    """A determinate ENOENT (no such file) → genuinely absent → NOT reachable."""
    import errno as _errno

    root = _make_repo(tmp_path, ["src/foo/bar.py"])
    _patch_stat_for_target(
        monkeypatch,
        "bar.py",
        FileNotFoundError(_errno.ENOENT, "No such file or directory"),
    )
    assert analyzer_mod._path_reachable_on_disk("src/foo/bar.py", root) is False


# ─────────────────── D1 orphan-clear in _build_stale_file_set ───────────────────


def test_orphan_row_deleted_reachable_kept(analyzer_mod, tmp_path):
    """A stale row whose file is ABSENT → deleted via delete_by_id.
    A stale row whose file EXISTS → kept in the stale set, NOT deleted."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    root = _make_repo(tmp_path, ["pkg/live.py"])
    func_rows = [
        _Obj("live", {"file_path": "pkg/live.py", "embed_revision": rev - 1}),
        _Obj("orphan", {"file_path": "pkg/deleted.py", "embed_revision": rev - 1}),
    ]
    functions = _FakeColl(
        "P_CodeFunction", func_rows, ("file_path", "embed_revision"), agg_count=2,
    )
    modules = _FakeColl("P_CodeModule", [], ("path", "embed_revision"), agg_count=0)
    classes = _FakeColl("P_CodeClass", [], ("file_path", "embed_revision"), agg_count=0)
    stub = _StaleStub(analyzer_mod, modules, classes, functions, root)

    result = stub._build_stale_file_set()

    # The orphan row was deleted by-id; the live row was not.
    assert functions.data.deleted == ["orphan"]
    # The reachable stale file stays in the set; the orphan does not.
    assert result == frozenset({"pkg/live.py"})


def test_orphan_clear_over_delete_guard(analyzer_mod, tmp_path):
    """OVER-DELETE GUARD: bar.py (deleted) + baz.py (exists) share word tokens
    (src/foo/…). Exact-compare must delete ONLY bar.py."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    root = _make_repo(tmp_path, ["src/foo/baz.py"])  # baz exists, bar does NOT
    rows = [
        _Obj("bar", {"file_path": "src/foo/bar.py", "embed_revision": rev - 1}),
        _Obj("baz", {"file_path": "src/foo/baz.py", "embed_revision": rev - 1}),
    ]
    functions = _FakeColl(
        "P_CodeFunction", rows, ("file_path", "embed_revision"), agg_count=2,
    )
    modules = _FakeColl("P_CodeModule", [], ("path", "embed_revision"), agg_count=0)
    classes = _FakeColl("P_CodeClass", [], ("file_path", "embed_revision"), agg_count=0)
    stub = _StaleStub(analyzer_mod, modules, classes, functions, root)

    result = stub._build_stale_file_set()

    assert functions.data.deleted == ["bar"], "only the deleted-file row"
    assert result == frozenset({"src/foo/baz.py"})


def test_orphan_clear_never_deletes_extra_path_rows(analyzer_mod, tmp_path):
    """B1 REGRESSION (silent data loss): a stale row from an `--extra-path`
    source root (DIFFERENT non-empty `project_source`) must NOT be deleted by the
    orphan-clear, even though its `file_path` is absent under the PRIMARY root we
    tested reachability against. The primary-only reachability check has no basis
    to call an extra-path row an orphan; deleting it destroyed legitimate data on
    every primary-only resync walk. Only PRIMARY-source rows (empty or
    primary-matching project_source) are eligible."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    # Primary root has NOTHING on disk → both rows are absent-under-primary.
    root = _make_repo(tmp_path, [])
    rows = [
        # Primary-source stale+absent row → legitimately an orphan → delete.
        _Obj("prim", {
            "file_path": "pkg/gone.py",
            "embed_revision": rev - 1,
            "project_source": root.as_posix(),
        }),
        # Extra-path stale row: file lives under /some/sibling, absent under the
        # primary root — but project_source marks it as an extra-path row → KEEP.
        _Obj("extra", {
            "file_path": "lib/util.py",
            "embed_revision": rev - 1,
            "project_source": "/some/sibling/clone",
        }),
        # Legacy row with NO project_source stamp → treated as primary → eligible.
        _Obj("legacy", {
            "file_path": "old/legacy.py",
            "embed_revision": rev - 1,
            "project_source": "",
        }),
    ]
    functions = _FakeColl(
        "P_CodeFunction", rows,
        ("file_path", "embed_revision", "project_source"), agg_count=3,
    )
    modules = _FakeColl("P_CodeModule", [], ("path", "embed_revision"), agg_count=0)
    classes = _FakeColl("P_CodeClass", [], ("file_path", "embed_revision"), agg_count=0)
    stub = _StaleStub(analyzer_mod, modules, classes, functions, root)

    stub._build_stale_file_set()

    # ONLY the primary + legacy rows deleted; the extra-path row SURVIVES.
    assert "extra" not in functions.data.deleted, (
        "B1: an --extra-path row must NEVER be orphan-cleared on a primary walk "
        f"(deleted={functions.data.deleted})"
    )
    assert set(functions.data.deleted) == {"prim", "legacy"}, (
        f"primary + legacy orphans deleted; got {functions.data.deleted}"
    )


def test_orphan_clear_purges_claude_state_rows(analyzer_mod, tmp_path):
    """F2/F4 (Fable update-process review): `.claude/state/` transient-scratch
    rows are ALWAYS purged by the orphan-clear — regardless of revision (even
    CURRENT-revision) and regardless of project_source — the marker itself is
    the proof of garbage (6_to_7 parity; exact substring, never tokenized).
    This delivers the purge to CLI-only / false-v7 users the migration can't
    reach, via ANY analyze run. Real code rows keep the normal rules."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    # The scratch files EXIST on disk (reachable) — the marker alone decides.
    root = _make_repo(tmp_path, [".claude/state/tool_backups/junk.py", "pkg/live.py"])
    rows = [
        # Scratch, STALE → purge.
        _Obj("scr-stale", {
            "file_path": ".claude/state/tool_backups/junk.py",
            "embed_revision": rev - 1,
        }),
        # Scratch, CURRENT revision → STILL purged (6_to_7 deletes regardless).
        _Obj("scr-current", {
            "file_path": ".claude/state/tool_backups/junk.py",
            "embed_revision": rev,
        }),
        # Scratch under an EXTRA source → STILL purged (garbage is garbage).
        _Obj("scr-extra", {
            "file_path": ".claude/state/tool_backups/junk.py",
            "embed_revision": rev - 1,
            "project_source": "/some/sibling/clone",
        }),
        # Real code row, stale + reachable → kept for the re-walk (NOT deleted).
        _Obj("live", {"file_path": "pkg/live.py", "embed_revision": rev - 1}),
    ]
    functions = _FakeColl(
        "P_CodeFunction", rows,
        ("file_path", "embed_revision", "project_source"), agg_count=4,
    )
    modules = _FakeColl("P_CodeModule", [], ("path", "embed_revision"), agg_count=0)
    classes = _FakeColl("P_CodeClass", [], ("file_path", "embed_revision"), agg_count=0)
    stub = _StaleStub(analyzer_mod, modules, classes, functions, root)

    stub._build_stale_file_set()

    assert set(functions.data.deleted) == {"scr-stale", "scr-current", "scr-extra"}, (
        f"all .claude/state rows purged, real code kept; got {functions.data.deleted}"
    )
    assert "live" not in functions.data.deleted


def test_orphan_clear_never_touches_current_revision_row(analyzer_mod, tmp_path):
    """A CURRENT-revision row for a deleted file must NOT be deleted by the
    orphan-clear (scoped to stale rows only)."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    root = tmp_path  # nothing on disk
    rows = [
        # current-revision row whose file is gone — out of orphan-clear scope
        _Obj("cur", {"file_path": "pkg/gone.py", "embed_revision": rev}),
        # stale + gone → orphan → delete
        _Obj("stale", {"file_path": "pkg/also_gone.py", "embed_revision": rev - 1}),
    ]
    # agg_count reports the stale row so the scan runs; the current row is not
    # counted stale by the aggregate but IS present in the iterator.
    functions = _FakeColl(
        "P_CodeFunction", rows, ("file_path", "embed_revision"), agg_count=1,
    )
    modules = _FakeColl("P_CodeModule", [], ("path", "embed_revision"), agg_count=0)
    classes = _FakeColl("P_CodeClass", [], ("file_path", "embed_revision"), agg_count=0)
    stub = _StaleStub(analyzer_mod, modules, classes, functions, root)

    stub._build_stale_file_set()

    assert functions.data.deleted == ["stale"], "current-revision row preserved"


def test_orphan_clear_fail_safe_when_repo_root_none(analyzer_mod, tmp_path):
    """repo_root unset (stub/older caller) → orphan-clear SKIPPED → NO deletes;
    orphan path stays in the stale set (pre-fix behaviour preserved)."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    rows = [
        _Obj("orphan", {"file_path": "pkg/deleted.py", "embed_revision": rev - 1}),
    ]
    functions = _FakeColl(
        "P_CodeFunction", rows, ("file_path", "embed_revision"), agg_count=1,
    )
    modules = _FakeColl("P_CodeModule", [], ("path", "embed_revision"), agg_count=0)
    classes = _FakeColl("P_CodeClass", [], ("file_path", "embed_revision"), agg_count=0)
    # repo_root=None → fail open
    stub = _StaleStub(analyzer_mod, modules, classes, functions, None)

    result = stub._build_stale_file_set()

    assert functions.data.deleted == [], "no deletes without a repo root"
    assert result == frozenset({"pkg/deleted.py"}), "orphan path retained"


def test_orphan_clear_fail_safe_when_exists_raises(analyzer_mod, tmp_path, monkeypatch):
    """An existence probe that raises a generic OSError → treat as reachable
    → NO delete. (Probe is os.stat now, not Path.exists — a generic OSError is
    indeterminate, so the row is KEPT.)"""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    rows = [
        _Obj("orphan", {"file_path": "pkg/deleted.py", "embed_revision": rev - 1}),
    ]
    functions = _FakeColl(
        "P_CodeFunction", rows, ("file_path", "embed_revision"), agg_count=1,
    )
    modules = _FakeColl("P_CodeModule", [], ("path", "embed_revision"), agg_count=0)
    classes = _FakeColl("P_CodeClass", [], ("file_path", "embed_revision"), agg_count=0)
    stub = _StaleStub(analyzer_mod, modules, classes, functions, tmp_path)

    _patch_stat_for_target(monkeypatch, "deleted.py", OSError("boom"))
    stub._build_stale_file_set()

    assert functions.data.deleted == [], "indeterminate existence → no delete"


def test_orphan_clear_converged_no_scan_no_delete(analyzer_mod, tmp_path):
    """Steady state: all aggregates report 0 → no iteration, no delete."""
    modules = _FakeColl("P_CodeModule", [], ("path", "embed_revision"), agg_count=0)
    classes = _FakeColl("P_CodeClass", [], ("file_path", "embed_revision"), agg_count=0)
    functions = _FakeColl("P_CodeFunction", [], ("file_path", "embed_revision"), agg_count=0)
    stub = _StaleStub(analyzer_mod, modules, classes, functions, tmp_path)

    result = stub._build_stale_file_set()

    assert result == frozenset()
    for coll in (modules, classes, functions):
        assert coll.iter_calls == 0, "converged collections never scan"
        assert coll.data.deleted == []


def test_orphan_clear_delete_failure_soft_fails(analyzer_mod, tmp_path):
    """A delete_by_id failure logs + continues; the row stays in the set is
    acceptable (pre-fix status quo) — the build must NOT raise."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    root = tmp_path  # orphan file absent
    rows = [
        _Obj("orphan", {"file_path": "pkg/deleted.py", "embed_revision": rev - 1}),
    ]
    functions = _FakeColl(
        "P_CodeFunction", rows, ("file_path", "embed_revision"), agg_count=1,
    )
    functions.data.fail_uuids = {"orphan"}
    modules = _FakeColl("P_CodeModule", [], ("path", "embed_revision"), agg_count=0)
    classes = _FakeColl("P_CodeClass", [], ("file_path", "embed_revision"), agg_count=0)
    stub = _StaleStub(analyzer_mod, modules, classes, functions, root)

    # Must not raise; deleted list stays empty (the delete failed).
    result = stub._build_stale_file_set()
    assert functions.data.deleted == []
    assert result is not None


# ─────────── CG-4 sweep-guarantee: whole-repo walk sweeps even at zero dispatch ───────────


class _WholeRepoStub:
    """Binds the whole-repo sweep-guarantee plumbing: the lazily-cached stale
    set + its builder (which runs the P2a sweep when `_analyze_whole_repo` is
    True) + the sweep method itself. Sets `_analyze_whole_repo=True` so the
    residual-orphan (current-revision, deleted-on-disk) sweep runs."""

    def __init__(self, analyzer_mod, modules, classes, functions, repo_root):
        self.modules_collection = modules
        self.classes_collection = classes
        self.functions_collection = functions
        self._analyze_repo_root = repo_root
        self._analyze_repo_root_raw = repo_root.as_posix()
        self._analyze_whole_repo = True
        _bind(
            analyzer_mod, self,
            (
                "_build_stale_file_set", "_get_stale_file_set",
                "_count_stale_rows_in_collection", "_get_existing_module",
                "_clear_deleted_primary_rows",
            ),
        )


def test_cg4_zero_dispatch_whole_repo_walk_still_sweeps(analyzer_mod, tmp_path):
    """CG-4 respawn-loop edge (ACT): a whole-repo walk that dispatches ZERO
    analyzable files (every analyzable file deleted from disk) must STILL sweep
    the residual current-revision orphans — otherwise the resync owed-gate keeps
    counting cleanup-owed>0 and re-spawns forever.

    The rows here are CURRENT-revision (not stale) primary rows whose files are
    gone from disk — exactly the class the stale-only D1 scan misses and the P2a
    whole-repo sweep targets. `agg_count=0` proves the stale-scan sees nothing
    (zero-dispatch analogue); the end-of-walk force-trigger `_get_stale_file_set`
    still runs `_build_stale_file_set`, whose whole-repo sweep purges them."""
    rev = analyzer_mod.CODEGRAPH_EMBED_REVISION
    root = _make_repo(tmp_path, [])  # nothing on disk → every stored row orphaned
    rows = [
        _Obj("gone1", {
            "file_path": "pkg/a.py", "embed_revision": rev, "project_source": "",
        }),
        _Obj("gone2", {
            "file_path": "pkg/b.py", "embed_revision": rev,
            "project_source": root.as_posix(),
        }),
    ]
    functions = _FakeColl(
        "P_CodeFunction", rows,
        ("file_path", "embed_revision", "project_source"), agg_count=0,
    )
    modules = _FakeColl(
        "P_CodeModule", [], ("path", "embed_revision", "project_source"), agg_count=0
    )
    classes = _FakeColl(
        "P_CodeClass", [], ("file_path", "embed_revision", "project_source"), agg_count=0
    )
    stub = _WholeRepoStub(analyzer_mod, modules, classes, functions, root)

    # The end-of-walk force-trigger (what analyze_repository now calls even when
    # zero files dispatched). Idempotent via the `_stale_file_set_cache` guard.
    stub._get_stale_file_set()

    assert set(functions.data.deleted) == {"gone1", "gone2"}, (
        "whole-repo sweep must purge current-revision orphans even at zero "
        f"dispatch (deleted={functions.data.deleted})"
    )


def test_cg4_force_trigger_is_idempotent_single_sweep(analyzer_mod, tmp_path):
    """CG-4 LEAVE-ALONE / single-invocation: the end-of-walk force-trigger must
    run the sweep EXACTLY ONCE per whole-repo walk — the lazy per-file path and
    the end-of-walk force-trigger share the `_stale_file_set_cache` guard, so a
    second call is a no-op. Assert via a per-collection sweep counter."""
    root = _make_repo(tmp_path, ["pkg/live.py"])
    live_row = _Obj(
        "live",
        {"file_path": "pkg/live.py", "embed_revision":
            analyzer_mod.CODEGRAPH_EMBED_REVISION, "project_source": ""},
    )
    functions = _FakeColl(
        "P_CodeFunction", [live_row],
        ("file_path", "embed_revision", "project_source"), agg_count=0,
    )
    modules = _FakeColl(
        "P_CodeModule", [], ("path", "embed_revision", "project_source"), agg_count=0
    )
    classes = _FakeColl(
        "P_CodeClass", [], ("file_path", "embed_revision", "project_source"), agg_count=0
    )
    stub = _WholeRepoStub(analyzer_mod, modules, classes, functions, root)

    # Count how many times the real sweep method is entered.
    real_sweep = stub._clear_deleted_primary_rows
    sweep_calls = {"n": 0}

    def _counting_sweep(*a, **k):
        sweep_calls["n"] += 1
        return real_sweep(*a, **k)

    stub._clear_deleted_primary_rows = _counting_sweep

    # Simulate the lazy per-file path having already built the set, THEN the
    # end-of-walk force-trigger firing: the second call must NOT re-sweep.
    stub._get_stale_file_set()  # builds once (lazy analogue)
    first = sweep_calls["n"]
    stub._get_stale_file_set()  # end-of-walk force-trigger — cached, no re-sweep
    second = sweep_calls["n"]

    # Exactly one sweep per collection on the first build; zero on the second.
    assert first == 3, f"one sweep per file-anchored collection, got {first}"
    assert second == first, "force-trigger must be a no-op (cached), not re-sweep"
    # A live, on-disk, current-revision row is NEVER purged.
    assert functions.data.deleted == [], "live file must survive the sweep"


# ─────────────────── _delete_file_rows_exact (shared helper) ───────────────────


def test_helper_skips_class_missing_path_prop(analyzer_mod):
    """A class missing the path property → (0, 0), no iteration attempted."""
    coll = _FakeColl("P_CodeFunction", [], ("something_else",))
    called = {"iter": False}
    orig = coll.iterator

    def _spy(return_properties=None):
        called["iter"] = True
        return orig(return_properties=return_properties)

    coll.iterator = _spy
    deleted, failures = analyzer_mod._delete_file_rows_exact(
        coll, "file_path", lambda raw, props: True,
    )
    assert (deleted, failures) == (0, 0)
    assert called["iter"] is False


def test_helper_exact_match_and_failure_count(analyzer_mod):
    rows = [
        _Obj("a", {"file_path": "x/a.py"}),
        _Obj("b", {"file_path": "x/b.py"}),
        _Obj("c", {"file_path": "x/a.py"}),  # same path as a
    ]
    coll = _FakeColl("P_CodeFunction", rows, ("file_path",))
    coll.data.fail_uuids = {"c"}
    deleted, failures = analyzer_mod._delete_file_rows_exact(
        coll, "file_path", lambda raw, props: raw == "x/a.py",
    )
    # a deleted, c failed, b not matched.
    assert coll.data.deleted == ["a"]
    assert (deleted, failures) == (1, 1)


# ─────────────────── _prune_deleted_file_objects (over-delete + scoping) ───────────────────


class _PruneStub:
    def __init__(self, analyzer_mod, modules, functions, classes,
                 project_name="P", ):
        self.modules_collection = modules
        self.functions_collection = functions
        self.classes_collection = classes
        self.project_name = project_name
        _bind(analyzer_mod, self, ("_prune_deleted_file_objects",))


def test_prune_over_delete_guard(analyzer_mod):
    """bar.py + baz.py share word tokens → exact-compare deletes ONLY bar.py."""
    rows = [
        _Obj("bar", {"file_path": "src/foo/bar.py", "project": "P", "project_source": ""}),
        _Obj("baz", {"file_path": "src/foo/baz.py", "project": "P", "project_source": ""}),
    ]
    functions = _FakeColl(
        "P_CodeFunction", rows, ("file_path", "project", "project_source"),
    )
    modules = _FakeColl("P_CodeModule", [], ("path", "project", "project_source"))
    classes = _FakeColl("P_CodeClass", [], ("file_path", "project", "project_source"))
    stub = _PruneStub(analyzer_mod, modules, functions, classes)

    n = stub._prune_deleted_file_objects("src/foo/bar.py")

    assert functions.data.deleted == ["bar"], "only the exact path"
    assert n == 1


def test_prune_project_scoping_in_python(analyzer_mod):
    """Same rel_path in a DIFFERENT project must not be swept."""
    rows = [
        _Obj("mine", {"file_path": "shared/x.py", "project": "P", "project_source": ""}),
        _Obj("theirs", {"file_path": "shared/x.py", "project": "OTHER", "project_source": ""}),
    ]
    functions = _FakeColl(
        "P_CodeFunction", rows, ("file_path", "project", "project_source"),
    )
    modules = _FakeColl("P_CodeModule", [], ("path", "project", "project_source"))
    classes = _FakeColl("P_CodeClass", [], ("file_path", "project", "project_source"))
    stub = _PruneStub(analyzer_mod, modules, functions, classes, project_name="P")

    stub._prune_deleted_file_objects("shared/x.py")

    assert functions.data.deleted == ["mine"], "other project's row preserved"


def test_prune_source_scoping_in_python(analyzer_mod):
    """Same rel_path + same project but a DIFFERENT project_source must not be
    swept when canonical_source is provided."""
    rows = [
        _Obj("root_a", {"file_path": "x.py", "project": "P", "project_source": "/rootA"}),
        _Obj("root_b", {"file_path": "x.py", "project": "P", "project_source": "/rootB"}),
    ]
    functions = _FakeColl(
        "P_CodeFunction", rows, ("file_path", "project", "project_source"),
    )
    modules = _FakeColl("P_CodeModule", [], ("path", "project", "project_source"))
    classes = _FakeColl("P_CodeClass", [], ("file_path", "project", "project_source"))
    stub = _PruneStub(analyzer_mod, modules, functions, classes, project_name="P")

    stub._prune_deleted_file_objects("x.py", "/rootA")

    assert functions.data.deleted == ["root_a"], "other source root's row preserved"


def test_prune_failure_increments_prune_failures(analyzer_mod):
    rows = [
        _Obj("bar", {"file_path": "a.py", "project": "P", "project_source": ""}),
    ]
    functions = _FakeColl("P_CodeFunction", rows, ("file_path", "project", "project_source"))
    functions.data.fail_uuids = {"bar"}
    modules = _FakeColl("P_CodeModule", [], ("path", "project", "project_source"))
    classes = _FakeColl("P_CodeClass", [], ("file_path", "project", "project_source"))
    stub = _PruneStub(analyzer_mod, modules, functions, classes)

    stub._prune_deleted_file_objects("a.py")

    assert getattr(stub, "_prune_failures", 0) == 1


# ─────────────────── R3: count_stale_rows reachability + aggregate bound ───────────────────

from vco_lib import codegraph_resync as cr  # noqa: E402


class _R3Coll:
    """Resync-side collection fake: aggregate + iterator (records scans).

    v0.2.74 (F2): also exposes ``config.get()`` so the count-side
    project_source scoping probe sees which properties the class "has"
    (default includes project_source, mirroring post-v0.2.47 schemas).
    """

    def __init__(self, agg_count, rows=None, prop_names=None):
        self.name = "X"
        self._agg = agg_count
        self._rows = rows or []
        self.iter_calls = 0
        self.aggregate = types.SimpleNamespace(
            over_all=lambda **kw: types.SimpleNamespace(total_count=agg_count)
        )
        _names = prop_names if prop_names is not None else (
            "embed_revision", "file_path", "path", "project_source",
        )
        _props = [types.SimpleNamespace(name=n) for n in _names]
        self.config = types.SimpleNamespace(
            get=lambda: types.SimpleNamespace(properties=_props)
        )

    def iterator(self, return_properties=None):
        self.iter_calls += 1
        for props in self._rows:
            yield types.SimpleNamespace(properties=props)


class _R3Client:
    def __init__(self, colls):
        self._colls = colls
        self.collections = types.SimpleNamespace(
            exists=lambda name: name in colls,
            get=lambda name: colls[name],
        )

    def close(self):
        pass


def _r3_client(prefix, module, klass, func):
    return _R3Client({
        f"{prefix}_CodeModule": module,
        f"{prefix}_CodeClass": klass,
        f"{prefix}_CodeFunction": func,
    })


def test_r3_aggregate_zero_returns_zero_no_scan(monkeypatch, tmp_path):
    """Aggregate stale==0 → returns 0 with NO per-row scan (iterator not
    called), even with a repo_root supplied."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")
    module = _R3Coll(agg_count=0)
    klass = _R3Coll(agg_count=0)
    func = _R3Coll(agg_count=0)
    client = _r3_client("Proj", module, klass, func)

    counts = cr.count_stale_rows(
        "Proj", current_revision=1, client=client, repo_root=tmp_path,
    )
    assert counts == {
        "Proj_CodeModule": 0, "Proj_CodeClass": 0, "Proj_CodeFunction": 0,
    }
    for c in (module, klass, func):
        assert c.iter_calls == 0, "aggregate-0 must not trigger a per-row scan"


def test_r3_aggregate_positive_counts_only_reachable(monkeypatch, tmp_path):
    """Aggregate stale>0 → per-row scan runs and counts only REACHABLE stale
    rows (orphan of a deleted file excluded)."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")
    # one on-disk file, one deleted
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "live.py").write_text("# real\n")

    func_rows = [
        {"embed_revision": 0, "file_path": "pkg/live.py"},     # stale + reachable
        {"embed_revision": None, "file_path": "pkg/gone.py"},  # stale + orphan
        {"embed_revision": 1, "file_path": "pkg/live.py"},     # current — not stale
    ]
    module = _R3Coll(agg_count=0)
    klass = _R3Coll(agg_count=0)
    func = _R3Coll(agg_count=2, rows=func_rows)
    client = _r3_client("Proj", module, klass, func)

    counts = cr.count_stale_rows(
        "Proj", current_revision=1, client=client, repo_root=tmp_path,
    )
    # Only the reachable stale row counts; the orphan is excluded.
    assert counts["Proj_CodeFunction"] == 1
    assert func.iter_calls == 1, "aggregate>0 pays exactly one scan"


def test_f4_claude_state_rows_not_counted_as_owed(monkeypatch, tmp_path):
    """F4 REGRESSION (Fable update-process review): `.claude/state/` transient-
    scratch rows are walk-EXCLUDED since v0.2.73, so a resync can never re-stamp
    them — counting them as owed made the probe permanently non-zero for users
    the 6_to_7 purge can't reach (CLI-only, false-v7). They must be excluded
    from the owed count even though their files EXIST on disk."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")
    # The scratch file genuinely exists on disk (reachable!) — the marker alone
    # must exclude it.
    scratch = tmp_path / ".claude" / "state" / "tool_backups"
    scratch.mkdir(parents=True)
    (scratch / "junk.py").write_text("# scratch\n")
    (tmp_path / "real.py").write_text("# real\n")

    func_rows = [
        {"embed_revision": 0, "file_path": ".claude/state/tool_backups/junk.py"},
        {"embed_revision": 0, "file_path": "real.py"},  # genuinely owed
    ]
    module = _R3Coll(agg_count=0)
    klass = _R3Coll(agg_count=0)
    func = _R3Coll(agg_count=2, rows=func_rows)
    client = _r3_client("Proj", module, klass, func)

    counts = cr.count_stale_rows(
        "Proj", current_revision=1, client=client, repo_root=tmp_path,
    )
    assert counts["Proj_CodeFunction"] == 1, (
        ".claude/state row must NOT be owed (never re-walkable); only real.py"
    )


def test_f2_extra_path_rows_not_counted_as_owed(monkeypatch, tmp_path):
    """F2 REGRESSION (Fable patch review): a stale extra-path row whose repo-
    relative path ALSO exists under the primary root (fork + upstream-clone —
    e.g. install.py exists in both) was counted owed forever: reachable-under-
    primary, never deleted (B1, correctly), never re-walked (primary resync
    doesn't pass --extra-path). The count must skip rows whose project_source
    is a DIFFERENT non-empty root — they converge on their own extra-path walk."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")
    (tmp_path / "install.py").write_text("# primary copy\n")
    primary_src = tmp_path.as_posix()

    func_rows = [
        # Extra-path row: same rel path exists under primary, but the row
        # belongs to the sibling clone → NOT owed on a primary walk.
        {"embed_revision": 0, "file_path": "install.py",
         "project_source": "/some/sibling/clone"},
        # Primary row, stale + reachable → genuinely owed.
        {"embed_revision": 0, "file_path": "install.py",
         "project_source": primary_src},
        # Legacy row (no source stamp) → treated as primary → owed.
        {"embed_revision": None, "file_path": "install.py",
         "project_source": ""},
    ]
    module = _R3Coll(agg_count=0)
    klass = _R3Coll(agg_count=0)
    func = _R3Coll(agg_count=3, rows=func_rows)
    client = _r3_client("Proj", module, klass, func)

    counts = cr.count_stale_rows(
        "Proj", current_revision=1, client=client, repo_root=tmp_path,
    )
    assert counts["Proj_CodeFunction"] == 2, (
        f"extra-path row must NOT be owed on a primary walk; got "
        f"{counts['Proj_CodeFunction']}"
    )


def test_f2_source_scoping_off_when_class_lacks_property(monkeypatch, tmp_path):
    """A class that PREDATES project_source (property absent) must not 500 the
    scan — the probe disables source-scoping and the count still works."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")
    (tmp_path / "real.py").write_text("# real\n")
    func_rows = [{"embed_revision": 0, "file_path": "real.py"}]
    module = _R3Coll(agg_count=0)
    klass = _R3Coll(agg_count=0)
    func = _R3Coll(
        agg_count=1, rows=func_rows,
        prop_names=("embed_revision", "file_path"),  # no project_source
    )
    client = _r3_client("Proj", module, klass, func)

    counts = cr.count_stale_rows(
        "Proj", current_revision=1, client=client, repo_root=tmp_path,
    )
    assert counts["Proj_CodeFunction"] == 1


def test_r3_module_uses_path_property(monkeypatch, tmp_path):
    """CodeModule keys the file on ``path`` (not ``file_path``); the reachability
    filter must read the right property per base."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")
    (tmp_path / "m_live.py").write_text("# real\n")
    module_rows = [
        {"embed_revision": 0, "path": "m_live.py"},   # reachable stale
        {"embed_revision": 0, "path": "m_gone.py"},   # orphan stale
    ]
    module = _R3Coll(agg_count=2, rows=module_rows)
    klass = _R3Coll(agg_count=0)
    func = _R3Coll(agg_count=0)
    client = _r3_client("Proj", module, klass, func)

    counts = cr.count_stale_rows(
        "Proj", current_revision=1, client=client, repo_root=tmp_path,
    )
    assert counts["Proj_CodeModule"] == 1  # only m_live.py


def test_r3_no_repo_root_counts_all_stale(monkeypatch, tmp_path):
    """Without repo_root (R3 disabled) the aggregate count is returned as-is —
    orphans included (pre-R3 behaviour, conservative)."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")
    module = _R3Coll(agg_count=0)
    klass = _R3Coll(agg_count=0)
    func = _R3Coll(agg_count=5)  # aggregate says 5 stale
    client = _r3_client("Proj", module, klass, func)

    counts = cr.count_stale_rows("Proj", current_revision=1, client=client)
    assert counts["Proj_CodeFunction"] == 5
    assert func.iter_calls == 0, "no repo_root → trust the aggregate, no scan"


def test_r3_escape_path_excluded_from_reachable(monkeypatch, tmp_path):
    """A stale row whose stored path escapes the repo root (``../../etc``) is an
    orphan → excluded from the reachable count."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")
    func_rows = [
        {"embed_revision": 0, "file_path": "../../../../etc/passwd"},
    ]
    module = _R3Coll(agg_count=0)
    klass = _R3Coll(agg_count=0)
    func = _R3Coll(agg_count=1, rows=func_rows)
    client = _r3_client("Proj", module, klass, func)

    counts = cr.count_stale_rows(
        "Proj", current_revision=1, client=client, repo_root=tmp_path,
    )
    assert counts["Proj_CodeFunction"] == 0, "escape path is not reachable"


def test_r3_reachability_filter_memoizes(monkeypatch, tmp_path):
    """The reachability predicate caches per-path so repeated file_paths across
    rows resolve with one existence check per UNIQUE path."""
    calls = {"n": 0}
    real = cr._path_reachable_on_disk

    def _counting(path, root):
        calls["n"] += 1
        return real(path, root)

    monkeypatch.setattr(cr, "_path_reachable_on_disk", _counting)
    filt = cr._make_reachability_filter(tmp_path)
    assert filt is not None
    filt("a.py")
    filt("a.py")
    filt("a.py")
    filt("b.py")
    assert calls["n"] == 2, "one probe per unique path"


def test_r3_reachability_filter_none_without_root():
    assert cr._make_reachability_filter(None) is None


def test_r3_aggregate_unavailable_scans_with_reachability(monkeypatch, tmp_path):
    """Aggregate raises (old collection w/o null index) → the NULL-safe scan
    still applies the R3 reachability filter (counts only reachable stale)."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")
    (tmp_path / "live.py").write_text("# real\n")

    class _AggBoom(_R3Coll):
        def __init__(self, rows):
            super().__init__(agg_count=None, rows=rows)
            self.aggregate = types.SimpleNamespace(
                over_all=lambda **kw: (_ for _ in ()).throw(RuntimeError("no null idx"))
            )

    func_rows = [
        {"embed_revision": None, "file_path": "live.py"},   # stale + reachable
        {"embed_revision": None, "file_path": "gone.py"},   # stale + orphan
        {"embed_revision": 1, "file_path": "live.py"},      # current
    ]
    module = _R3Coll(agg_count=0)
    klass = _R3Coll(agg_count=0)
    func = _AggBoom(func_rows)
    client = _r3_client("Proj", module, klass, func)

    counts = cr.count_stale_rows(
        "Proj", current_revision=1, client=client, repo_root=tmp_path,
    )
    assert counts["Proj_CodeFunction"] == 1, "only reachable stale via scan"


def test_r3_scan_failure_returns_none(monkeypatch, tmp_path):
    """Aggregate positive but the R3 scan raises → None (undeterminable), never
    a possibly-wrong number."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")

    class _BoomColl(_R3Coll):
        def iterator(self, return_properties=None):
            raise RuntimeError("scan failed")

    module = _R3Coll(agg_count=0)
    klass = _R3Coll(agg_count=0)
    func = _BoomColl(agg_count=3)
    client = _r3_client("Proj", module, klass, func)

    counts = cr.count_stale_rows(
        "Proj", current_revision=1, client=client, repo_root=tmp_path,
    )
    assert counts is None
