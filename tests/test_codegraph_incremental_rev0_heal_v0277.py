# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.77 Part 3 (5c) tasks 5+6 — incremental --incremental heals rev-0 rows.

THE GAP (task 5 / 5c-v): `_filter_changed_files` keeps only git-diff-changed
files, so a file whose content is UNCHANGED but which owns a stale/vectorless
(embed_revision NULL or 0) code-graph row never re-walks under `--incremental`
— it never reaches `_get_existing_module` (where the R-1 stale-file probe
fires). So before this fix ONLY a full walk could heal the 89 vectorless rows
the 5c incident wrote. `_union_stale_into_changed` adds those files back.

Covers:
  * act: an unchanged (not-in-git-diff) file whose rel-path is in the stale set
    IS re-queued by `_union_stale_into_changed`.
  * leave-alone: converged repo (empty stale set) → changed set unchanged;
    None stale set (probe failure / stub) → fail-open, unchanged.
  * path-form: stale-set membership uses `relative_to(source_root)` so only
    rows belonging to THIS root match.
  * task 6 (5c-iv) chain: `_get_existing_module` returns None (forces re-walk)
    for a rev-0 module row (full-walk heal path — verify it exists) AND the
    same file is reached by the incremental union (new heal path).
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_v0277_5c_analyze_code_graph", str(_ANALYZER_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer()


def _bare_analyzer(analyzer_mod: types.ModuleType):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = "TestProject"
    inst.client = None
    return inst


# ── task 5: _union_stale_into_changed ────────────────────────────────


class TestUnionStaleIntoChanged:
    def test_act_unchanged_stale_file_is_requeued(self, analyzer_mod):
        root = Path("/repo")
        changed = root / "b.py"          # git-diff changed
        stale_unchanged = root / "a.py"  # NOT changed, but owns a stale row
        clean = root / "c.py"            # neither changed nor stale
        all_files = [stale_unchanged, changed, clean]

        inst = _bare_analyzer(analyzer_mod)
        # stale set = repo-relative POSIX paths (as stored on the rows).
        inst._stale_file_set_cache = frozenset({"a.py"})

        out = inst._union_stale_into_changed(root, all_files, [changed])
        assert changed in out
        assert stale_unchanged in out, "stale unchanged file must be re-queued"
        assert clean not in out, "clean unchanged file must NOT be re-queued"
        # changed file kept first (order preserved), stale appended.
        assert out[0] == changed

    def test_leave_alone_empty_stale_set_returns_changed(self, analyzer_mod):
        root = Path("/repo")
        changed = [root / "b.py"]
        inst = _bare_analyzer(analyzer_mod)
        inst._stale_file_set_cache = frozenset()  # converged
        out = inst._union_stale_into_changed(root, [root / "a.py", root / "b.py"], changed)
        assert out == changed

    def test_fail_open_none_stale_set_returns_changed(self, analyzer_mod):
        root = Path("/repo")
        changed = [root / "b.py"]
        inst = _bare_analyzer(analyzer_mod)
        # No cache attr AND the probe raises → fail-open (unchanged).
        inst._stale_file_set_cache = None
        out = inst._union_stale_into_changed(root, [root / "a.py", root / "b.py"], changed)
        assert out == changed

    def test_no_double_add_when_stale_file_also_changed(self, analyzer_mod):
        root = Path("/repo")
        f = root / "a.py"
        inst = _bare_analyzer(analyzer_mod)
        inst._stale_file_set_cache = frozenset({"a.py"})
        # a.py is BOTH changed and stale → appears once.
        out = inst._union_stale_into_changed(root, [f], [f])
        assert out.count(f) == 1

    def test_path_form_matches_source_root_relative(self, analyzer_mod):
        # Nested file: rel path is "pkg/mod.py" relative to the source root.
        root = Path("/repo")
        nested = root / "pkg" / "mod.py"
        inst = _bare_analyzer(analyzer_mod)
        inst._stale_file_set_cache = frozenset({"pkg/mod.py"})
        out = inst._union_stale_into_changed(root, [nested], [])
        assert nested in out
        # A stale entry keyed differently (absolute) must NOT match.
        inst._stale_file_set_cache = frozenset({str(nested)})
        out2 = inst._union_stale_into_changed(root, [nested], [])
        assert nested not in out2

    def test_extra_root_only_matches_its_own_relative_rows(self, analyzer_mod):
        # A row stored under the extra root uses paths relative to the EXTRA
        # root; the union tests against source_root so only same-root files add.
        extra = Path("/extra")
        f = extra / "x.py"
        inst = _bare_analyzer(analyzer_mod)
        inst._stale_file_set_cache = frozenset({"x.py"})
        out = inst._union_stale_into_changed(extra, [f], [])
        assert f in out


# ── task 6 (5c-iv): rev-0 heal reachability, full AND incremental ─────


class _FakeObj:
    def __init__(self, uuid: str, props: Dict[str, Any]):
        self.uuid = uuid
        self.properties = props


class _FakeResult:
    def __init__(self, objs: List[_FakeObj]):
        self.objects = objs


class _FakeModulesQuery:
    def __init__(self, obj: Optional[_FakeObj]):
        self._obj = obj

    def fetch_objects(self, filters=None, limit=None):  # noqa: ARG002
        return _FakeResult([self._obj] if self._obj else [])


class _FakeModulesColl:
    def __init__(self, obj: Optional[_FakeObj]):
        self.query = _FakeModulesQuery(obj)


class TestRev0HealReachability:
    def test_full_walk_gate_reembeds_rev0_row(self, analyzer_mod):
        """Verify the EXISTING full-walk heal path: `_get_existing_module`
        returns None (→ re-walk) for a module row at embed_revision 0 even when
        path + hash match."""
        inst = _bare_analyzer(analyzer_mod)
        # Module row exists with matching hash but rev 0 (vectorless).
        obj = _FakeObj("u1", {"embed_revision": 0})
        inst.modules_collection = _FakeModulesColl(obj)
        # No stale-set path involvement here (isolate the revision conjunct):
        inst._stale_file_set_cache = frozenset()
        got = inst._get_existing_module("a.py", "hash123")
        assert got is None, "rev-0 module row must NOT skip the file (re-walk)"

    def test_full_walk_gate_skips_current_row(self, analyzer_mod):
        """Leave-alone: a CURRENT-revision matching row DOES skip (returns uuid)."""
        inst = _bare_analyzer(analyzer_mod)
        current = analyzer_mod.CODEGRAPH_EMBED_REVISION
        obj = _FakeObj("u2", {"embed_revision": current})
        inst.modules_collection = _FakeModulesColl(obj)
        inst._stale_file_set_cache = frozenset()
        got = inst._get_existing_module("a.py", "hash123")
        assert got == "u2", "current-revision matching row skips the file"

    def test_incremental_union_reaches_rev0_file(self, analyzer_mod):
        """Task 6 chain: a rev-0 file that is UNCHANGED in git is now reachable
        under incremental via the stale-set union (the new heal path)."""
        root = Path("/repo")
        rev0_file = root / "frozen.py"  # unchanged on disk, owns rev-0 rows
        inst = _bare_analyzer(analyzer_mod)
        # The stale-file probe surfaces frozen.py (its rows are rev-0).
        inst._stale_file_set_cache = frozenset({"frozen.py"})
        # git-diff found NOTHING changed → without the union, incremental skips.
        out = inst._union_stale_into_changed(root, [rev0_file], [])
        assert rev0_file in out, (
            "incremental must re-queue the rev-0 file so its rows re-embed"
        )

    def test_gate_stale_set_forces_rewalk_even_current_revision(self, analyzer_mod):
        """The per-file gate ALSO re-walks when the file is in the stale set,
        regardless of the module row's own revision (R-1 semantics)."""
        inst = _bare_analyzer(analyzer_mod)
        current = analyzer_mod.CODEGRAPH_EMBED_REVISION
        obj = _FakeObj("u3", {"embed_revision": current})
        inst.modules_collection = _FakeModulesColl(obj)
        # File "a.py" owns a stale FUNCTION/CLASS row → stale set contains it.
        inst._stale_file_set_cache = frozenset({"a.py"})
        got = inst._get_existing_module("a.py", "hash123")
        assert got is None, "a file in the stale set must re-walk (R-1)"


class TestIncrementalLoopWiring:
    """Source-level guard that the incremental branch of `analyze_repository`
    actually threads the changed set through `_union_stale_into_changed`
    (the helper is proven above; this locks the wiring so a refactor can't
    silently drop the heal path)."""

    def test_incremental_branch_calls_union_after_filter(self):
        src = _ANALYZER_PATH.read_text(encoding="utf-8")
        # The union must be called INSIDE the incremental branch, AFTER
        # _filter_changed_files, and its result fed to the not-files skip.
        assert "_union_stale_into_changed(" in src
        i_filter = src.find("files = self._filter_changed_files(")
        i_union = src.find("files = self._union_stale_into_changed(")
        assert i_filter != -1 and i_union != -1
        assert i_filter < i_union, "union must run AFTER the git-diff filter"
        # The "No changed files" early-skip must come AFTER the union so a
        # converged repo WITH stale rows still re-walks them.
        i_skip = src.find("No changed", i_union)
        assert i_skip != -1 and i_union < i_skip


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
