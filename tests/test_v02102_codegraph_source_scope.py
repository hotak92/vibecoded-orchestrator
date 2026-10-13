# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-CGD — the code graph's per-root identity rules (D-1..D-4).

A code-graph row carries ``project_source`` (the root it was indexed from) and
its UUID mixes that root in, so a project's copy of a file and an extra code
folder's copy of the same file are two row sets. These tests pin the four
places that must agree with that keying:

* D-1 — the unchanged-file gate skips a file only when the WALKING root's own
  row is current. Act: a project file identical to an extra folder's
  already-indexed copy gets project rows, and removing the folder (a
  ``--prune-stale`` reindex without it) keeps them. Leave-alone: an unchanged
  file in the same root is still skipped with zero writes.
* D-2 — an extra root always stamps itself (``--canonical-source`` is for the
  project's own scoped edits); the drain's vanished-file prune uses the same
  stamp, so an extra's drain never deletes the project's rows of that path.
* D-3 — the ``--prune-stale`` preserve set is keyed ``(source, path)``.
* D-4 — the ONE overlap rule (``vco_lib.codegraph_extra_overlap``) and the
  analyzer's refusals.

No Weaviate: an in-memory collection evaluates the analyzer's REAL
``weaviate`` filter objects, so the walk runs its production code paths.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from vco_lib import codegraph_extra_overlap as ov
from vco_lib import codegraph_source_scope as scope
from tests.common.child_env import child_env
from vco_lib.codegraph_prune import expand_preserve_entries, prune_collection

_REPO = Path(__file__).resolve().parent.parent
_ANALYZER = _REPO / "templates" / "scripts" / "analyze_code_graph.py"


@pytest.fixture(scope="module")
def amod() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_w6cgd_analyzer", str(_ANALYZER))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── in-memory Weaviate collection (evaluates real filter objects) ──────────


def _match(flt: Any, props: Dict[str, Any]) -> bool:
    if flt is None:
        return True
    name = type(flt).__name__
    if name == "_FilterAnd":
        return all(_match(f, props) for f in flt.filters)
    if name == "_FilterOr":
        return any(_match(f, props) for f in flt.filters)
    op = getattr(getattr(flt, "operator", None), "name", "")
    target = flt.target if isinstance(flt.target, str) else getattr(flt.target, "target", flt.target)
    value = props.get(target)
    if op == "EQUAL":
        return value == flt.value
    if op == "NOT_EQUAL":
        return value != flt.value
    if op == "CONTAINS_ANY":
        vals = value if isinstance(value, list) else [value]
        return any(v in vals for v in flt.value)
    return False  # an operator this fake does not model never matches


class _Data:
    def __init__(self, coll: "_Coll") -> None:
        self.c = coll

    def _put(self, uuid, properties=None, **_kw):
        self.c.rows[str(uuid)] = dict(properties or {})
        self.c.writes.append(str(uuid))
        return str(uuid)

    insert = replace = _put

    def update(self, uuid, properties=None, **_kw):
        self.c.rows.setdefault(str(uuid), {}).update(properties or {})
        self.c.writes.append(str(uuid))

    def delete_by_id(self, uuid):
        self.c.rows.pop(str(uuid), None)
        self.c.deleted.append(str(uuid))


class _Query:
    def __init__(self, coll: "_Coll") -> None:
        self.c = coll

    def fetch_objects(self, filters=None, limit=None, return_properties=None, **_kw):
        objs = [SimpleNamespace(uuid=u, properties=dict(p))
                for u, p in self.c.rows.items() if _match(filters, p)]
        return SimpleNamespace(objects=objs[: limit or len(objs)])

    def fetch_object_by_id(self, uuid, **_kw):
        p = self.c.rows.get(str(uuid))
        return None if p is None else SimpleNamespace(uuid=str(uuid), properties=dict(p))


class _Coll:
    PROPS = ("project", "project_source", "path", "file_path", "language", "embed_revision")

    def __init__(self, name: str) -> None:
        self.name = name
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.writes: List[str] = []
        self.deleted: List[str] = []
        self.data = _Data(self)
        self.query = _Query(self)
        self.config = SimpleNamespace(get=lambda: SimpleNamespace(
            properties=[SimpleNamespace(name=n) for n in self.PROPS]))

    def iterator(self, return_properties=None, **_kw):
        return iter([SimpleNamespace(uuid=u, properties=dict(p)) for u, p in list(self.rows.items())])


def _analyzer(amod, project: str = "W6Proj"):
    a = amod.CodeGraphAnalyzer.__new__(amod.CodeGraphAnalyzer)
    a.project_name, a.client = project, object()
    a.module_cache, a.class_cache, a.function_cache, a.module_imports = {}, {}, {}, {}
    a.visited_uuids, a._track_visited = set(), False
    a._current_language = a._current_source = ""
    a._progress_emitter, a._cfg_pdg_data = None, {}
    a.modules_collection = _Coll(f"{project}_CodeModule")
    a.classes_collection = _Coll(f"{project}_CodeClass")
    a.functions_collection = _Coll(f"{project}_CodeFunction")
    a.apis_collection = _Coll(f"{project}_CodeAPI")
    a.interactions_collection = _Coll(f"{project}_CodeInteraction")
    return a


def _rewire(fresh, old) -> None:
    """A new analyzer process over the SAME stored rows (one CLI run each)."""
    for attr in ("modules_collection", "classes_collection", "functions_collection",
                 "apis_collection", "interactions_collection"):
        setattr(fresh, attr, getattr(old, attr))


#: Truthy on purpose: a vectorless write is stamped embed_revision 0 (R-2),
#: which the gate treats as stale — every re-walk would then re-write.
_VEC = [0.1] * 8


@pytest.fixture()
def no_embed(amod, monkeypatch):
    monkeypatch.setattr(amod, "generate_embedding", lambda text: list(_VEC))
    monkeypatch.setattr(amod, "embed_module", lambda summary: list(_VEC))
    monkeypatch.setattr(amod, "embed_function", lambda sig, body, language="python": list(_VEC))
    monkeypatch.setattr(amod, "embed_class", lambda sig, body, methods=None, language="python": list(_VEC))


def _module_sources(a, rel: str) -> List[str]:
    return sorted(p.get("project_source", "") for p in a.modules_collection.rows.values()
                  if p.get("path") == rel)


def _fn_sources(a, rel: str) -> List[str]:
    return sorted(p.get("project_source", "") for p in a.functions_collection.rows.values()
                  if p.get("file_path") == rel)


def _trees(tmp_path: Path):
    proj, extra = tmp_path / "proj", tmp_path / "clone"
    for root in (proj, extra):
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / "same.py").write_text("def shared_fn():\n    return 41\n")
    return proj.resolve(), extra.resolve()


# ── D-1: the unchanged-file gate is source-aware ──────────────────────────


def test_d1_identical_file_gets_rows_in_both_roots_and_survives_folder_removal(
    amod, no_embed, tmp_path
):
    """ACT. The extra folder indexes ``pkg/same.py`` first (the dogfood order);
    the project's identical copy must still get its OWN rows, and a
    ``--prune-stale`` reindex without the folder (what Remove runs) deletes the
    folder's rows and keeps the project's. Pre-fix the project walk skipped the
    file as "unchanged" (it matched the folder's row), so the removal left the
    file with no rows at all."""
    proj, extra = _trees(tmp_path)
    a = _analyzer(amod)
    a.analyze_repository(extra, as_extra_path=True)  # Sync button / auto-refresh
    assert _module_sources(a, "pkg/same.py") == [extra.as_posix()]

    b = _analyzer(amod)
    _rewire(b, a)
    b.analyze_repository(proj)  # the project's own walk
    assert _module_sources(b, "pkg/same.py") == sorted([proj.as_posix(), extra.as_posix()]), (
        "the project's identical copy must get its own module row (D-1)")
    assert proj.as_posix() in _fn_sources(b, "pkg/same.py")

    c = _analyzer(amod)
    _rewire(c, b)
    c.analyze_repository(proj, prune_stale=True)  # Remove → reindex without the folder
    assert _module_sources(c, "pkg/same.py") == [proj.as_posix()], (
        "removing the folder deletes its rows and keeps the project's")
    assert _fn_sources(c, "pkg/same.py") == [proj.as_posix()]


def test_d1_unchanged_file_same_root_still_skipped_zero_writes(amod, no_embed, tmp_path):
    """LEAVE-ALONE. A second walk of the same root over an unchanged file is
    still a no-op: zero writes to any collection."""
    proj, _extra = _trees(tmp_path)
    a = _analyzer(amod)
    a.analyze_repository(proj)
    b = _analyzer(amod)
    _rewire(b, a)
    for coll in (b.modules_collection, b.functions_collection, b.classes_collection):
        coll.writes.clear()
    stats = b.analyze_repository(proj)
    assert stats["modules"] == 0 and stats["functions"] == 0
    assert b.modules_collection.writes == [] and b.functions_collection.writes == [], (
        "an unchanged file of the walking root must be skipped (D-1 leave-alone)")


def test_d1_extra_walk_does_not_skip_on_the_projects_row(amod, no_embed, tmp_path):
    """ACT, the other direction: the project indexed first; the extra folder's
    walk still writes the folder's own rows (today's expected duplicate until
    the v0.2.103 de-duplication records it on purpose)."""
    proj, extra = _trees(tmp_path)
    a = _analyzer(amod)
    a.analyze_repository(proj)
    b = _analyzer(amod)
    _rewire(b, a)
    b.analyze_repository(extra, as_extra_path=True)
    assert _module_sources(b, "pkg/same.py") == sorted([proj.as_posix(), extra.as_posix()])


def test_d1_legacy_unstamped_row_counts_for_the_project_not_an_extra(amod):
    """A pre-v0.2.47 row (no project_source) belongs to the project's own root:
    the project walk may skip on it; an extra walk never does."""
    rev = amod.CODEGRAPH_EMBED_REVISION
    a = _analyzer(amod)
    a.modules_collection.rows["legacy"] = {"path": "x.py", "file_hash": "h", "embed_revision": rev}
    a._get_stale_file_set = lambda: frozenset()
    a._walk_own, a._strict_source = ({"/p"}, False), False
    assert a._get_existing_module("x.py", "h") == "legacy"
    a._walk_own = ({"/x"}, True)
    assert a._get_existing_module("x.py", "h") is None


def test_d1_invalidation_targets_this_roots_row_not_the_twin(amod, tmp_path):
    """R-3 compensation reads the same row the gate does: after a per-file
    failure it must un-stamp THIS root's module row, never the other root's
    identical copy (which would leave this root's row claiming "done")."""
    a = _analyzer(amod)
    a.modules_collection.rows["twin"] = {"path": "pkg/same.py", "project_source": "/clone",
                                         "file_hash": "h"}
    a.modules_collection.rows["mine"] = {"path": "pkg/same.py", "project_source": "/proj",
                                         "file_hash": "h"}
    a._current_source, a._walk_own = "/proj", ({"/proj"}, False)
    a._invalidate_module_row(Path("/proj/pkg/same.py"), Path("/proj"))
    assert a.modules_collection.rows["mine"]["file_hash"] == ""
    assert a.modules_collection.rows["twin"]["file_hash"] == "h"


# ── D-2: one stamp rule ────────────────────────────────────────────────────


@pytest.mark.parametrize("kw,want", [
    (dict(canonical_source="/main", scoped=True, extra_root=False), "/main"),
    (dict(canonical_source="/main", scoped=True, extra_root=True), "/wt"),
    (dict(canonical_source="/main", scoped=False, extra_root=False), "/wt"),
    (dict(canonical_source=None, scoped=True, extra_root=False), "/wt"),
])
def test_d2_walk_source_stamp(kw, want):
    assert scope.walk_source_stamp("/wt", **kw) == want


def test_d2_extra_scoped_edit_ignores_canonical_source(amod, no_embed, tmp_path):
    """An extra-path edit (``--as-extra-path``) stamps the extra root even when
    a caller still passes ``--canonical-source`` (e.g. the git main root of a
    worktree extra) — the value every other extra producer stamps."""
    _proj, extra = _trees(tmp_path)
    a = _analyzer(amod)
    a.analyze_repository(extra, only_file=extra / "pkg" / "same.py",
                         canonical_source="/somewhere/main", as_extra_path=True)
    assert _module_sources(a, "pkg/same.py") == [extra.as_posix()]


def test_d2_project_scoped_edit_keeps_canonical_source(amod, no_embed, tmp_path):
    """LEAVE-ALONE: the project's own worktree edit still converges on the
    canonical main root."""
    proj, _extra = _trees(tmp_path)
    a = _analyzer(amod)
    a.analyze_repository(proj, only_file=proj / "pkg" / "same.py", canonical_source="/main/root")
    assert _module_sources(a, "pkg/same.py") == ["/main/root"]


def test_d2_extra_drain_vanished_file_prunes_only_the_extras_rows(amod, tmp_path):
    """The queued Stop drain of an extra (``--only-files-from`` +
    ``--as-extra-path``, no canonical source) prunes a vanished file's rows
    under the extra's own source. It used to pass an EMPTY source (=
    unscoped) and deleted the project's rows of the same path too."""
    _proj, extra = _trees(tmp_path)
    a = _analyzer(amod)
    for coll, prop in ((a.modules_collection, "path"), (a.functions_collection, "file_path")):
        coll.rows["p-" + prop] = {prop: "pkg/gone.py", "project": a.project_name,
                                  "project_source": "/the/project"}
        coll.rows["x-" + prop] = {prop: "pkg/gone.py", "project": a.project_name,
                                  "project_source": extra.as_posix()}
    lst = tmp_path / "list.txt"
    lst.write_text(f"{extra / 'pkg' / 'gone.py'}\n")
    a.analyze_repository(extra, only_files_from=lst, as_extra_path=True)
    for coll, prop in ((a.modules_collection, "path"), (a.functions_collection, "file_path")):
        assert "x-" + prop in coll.deleted, "the extra's own row of the vanished file goes"
        assert "p-" + prop in coll.rows, "the project's row of the same path stays"


# ── D-3: the prune-preserve set is keyed (source, path) ───────────────────


def _prow(uid, src, rel, prop="file_path"):
    return SimpleNamespace(uuid=uid, properties={"project": "P", "project_source": src, prop: rel})


def _pcoll(rows, name="P_CodeFunction"):
    c = _Coll(name)
    c.iterator = lambda return_properties=None, **_kw: iter(rows)
    return c


def test_d3_skip_in_one_root_no_longer_shields_the_twin():
    """ACT: ``pkg/a.py`` skipped in the project (unchanged) preserves the
    project's rows only; the extra folder's ``pkg/a.py`` rows, unvisited by a
    reindex without the folder, are pruned."""
    keys = expand_preserve_entries({("/proj", "pkg/a.py")}, primary_root="/proj", strict=False)
    coll = _pcoll([_prow("p", "/proj", "pkg/a.py"), _prow("x", "/clone", "pkg/a.py"),
                   _prow("legacy", "", "pkg/a.py")])
    pruned, failures = prune_collection(coll, set(), project_name="P",
                                        canonical_lang_id=lambda v: v, preserve_paths=keys)
    assert (pruned, failures) == (1, 0)
    assert coll.deleted == ["x"], "only the other tree's copy is judged stale"


def test_d3_skip_still_preserves_its_own_root_and_bare_entries():
    """LEAVE-ALONE: the skipping root's rows (raw, resolved and legacy forms)
    survive; a bare-path entry (a caller that cannot name the root) still
    preserves the path under any source."""
    keys = expand_preserve_entries({("/proj", "pkg/a.py"), "pkg/b.py"}, primary_root="/proj",
                                   strict=False)
    coll = _pcoll([_prow("p", "/proj", "pkg/a.py"), _prow("legacy", "", "pkg/a.py"),
                   _prow("b1", "/clone", "pkg/b.py"), _prow("b2", "/proj", "pkg/b.py")])
    pruned, _ = prune_collection(coll, set(), project_name="P",
                                 canonical_lang_id=lambda v: v, preserve_paths=keys)
    assert pruned == 0 and coll.deleted == []


def test_d3_extra_root_skip_never_preserves_legacy_rows():
    """Under ``--as-extra-path`` the walked root IS an extra: its skip keys
    never include the unstamped (project-owned) form."""
    keys = expand_preserve_entries({("/clone", "a.py")}, primary_root="/clone", strict=True)
    assert ("", "a.py") not in keys and ("/clone", "a.py") in keys


def test_d3_refused_nested_source_rows_are_kept_by_the_prune():
    coll = _pcoll([_prow("n", "/proj/sub", "x.py"), _prow("gone", "/old", "x.py")])
    pruned, _ = prune_collection(coll, set(), project_name="P", canonical_lang_id=lambda v: v,
                                 preserve_sources={"/proj/sub"})
    assert coll.deleted == ["gone"] and pruned == 1


def test_d3_walk_records_source_keyed_entries(amod, no_embed, tmp_path):
    """The walk records ``(root, rel)`` for discovered AND walked files, so
    ``discovered − walked`` is per root."""
    proj, extra = _trees(tmp_path)
    a = _analyzer(amod)
    a.analyze_repository(proj, extra_paths=[extra], prune_stale=True)
    assert (proj.as_posix(), "pkg/same.py") in a._prune_discovered_paths
    assert (extra.as_posix(), "pkg/same.py") in a._prune_discovered_paths
    assert (proj.as_posix(), "pkg/same.py") in a._prune_walked_paths
    assert (extra.as_posix(), "pkg/same.py") in a._prune_walked_paths


# ── D-4: the ONE overlap rule + the analyzer's refusals ───────────────────


@pytest.mark.parametrize("extra,project,verdict", [
    ("/a/proj", "/a/proj", ov.SAME_AS_PROJECT),
    ("/a/proj/", "/a/proj", ov.SAME_AS_PROJECT),
    ("/a/proj/sub/dir", "/a/proj", ov.INSIDE_PROJECT),
    ("/a/proj/vendor/lib", "/a/proj", ov.INSIDE_PROJECT_IGNORED),
    ("/a/proj/.wt/feature", "/a/proj", ov.INSIDE_PROJECT_IGNORED),
    ("/a/proj/.claude/scripts", "/a/proj", ov.INSIDE_PROJECT_IGNORED),
    ("/a", "/a/proj", ov.CONTAINS_PROJECT),
    ("/A/PROJ/sub", "/a/proj", ov.INSIDE_PROJECT),
    ("/a/projz", "/a/proj", ov.OK),
    ("/a/foobar", "/a/foo", ov.OK),
    ("/b/clone", "/a/proj", ov.OK),
    ("c:/opt/myproj/sub", "c:\\opt\\myproj", ov.INSIDE_PROJECT),
])
def test_d4_overlap_rule(extra, project, verdict):
    v = ov.classify(extra, project)
    assert v.verdict == verdict, v
    assert v.refused is (verdict != ov.OK)
    assert bool(v.message) is v.refused


def test_d4_ignored_subfolder_message_does_not_claim_already_indexed():
    v = ov.classify("/a/proj/vendor/x", "/a/proj")
    assert "never indexes" in v.message and "already indexed" not in v.message
    assert v.ignored_dir == "vendor"


def test_d4_symlinked_project_folder_compares_by_realpath(tmp_path):
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert ov.classify(str(real / "sub"), str(link)).verdict == ov.INSIDE_PROJECT
    assert ov.classify(str(link / "sub"), str(real)).verdict == ov.INSIDE_PROJECT


def test_d4_cli_json_and_exit_codes(tmp_path):
    run = lambda *a: subprocess.run(  # noqa: E731
        [sys.executable, "-m", "vco_lib.codegraph_extra_overlap", *a],
        capture_output=True, text=True, env=child_env(), cwd=str(tmp_path))
    r = run("--extra", "/a/proj/sub", "--project-folder", "/a/proj", "--json")
    assert r.returncode == 0
    body = json.loads(r.stdout)
    assert body["ok"] is True and body["refused"] is True and body["verdict"] == ov.INSIDE_PROJECT
    r = run("--extra", "/a/proj/sub", "--project-folder", "/a/proj")
    assert r.returncode == 3 and "inside this project" in r.stdout
    r = run("--extra", "/b/clone", "--project-folder", "/a/proj")
    assert r.returncode == 0 and r.stdout == ""


def test_d4_combined_walk_refuses_a_nested_extra_and_keeps_its_rows(amod, no_embed, tmp_path, capsys):
    """ACT: ``--extra-path`` inside the project is not walked (the project
    already walks it) and its earlier rows survive a ``--prune-stale``
    reindex. LEAVE-ALONE: an external extra is walked."""
    proj, extra = _trees(tmp_path)
    nested = proj / "pkg"
    a = _analyzer(amod)
    a.functions_collection.rows["nested-row"] = {
        "file_path": "same.py", "project": a.project_name, "project_source": nested.as_posix()}
    a.analyze_repository(proj, extra_paths=[nested, extra], prune_stale=True)
    err = capsys.readouterr().err
    assert "Extra path refused, not walked" in err and "already indexed" in err
    assert "nested-row" in a.functions_collection.rows, "a refused nested extra's rows are kept"
    assert extra.as_posix() in _module_sources(a, "pkg/same.py"), "the external extra is walked"
    assert nested.as_posix() not in _module_sources(a, "same.py")


def _run_analyzer_cli(args, tmp_path):
    return subprocess.run([sys.executable, str(_ANALYZER), *args], capture_output=True, text=True,
                          env=child_env(WEAVIATE_URL="http://127.0.0.1:9"), cwd=str(tmp_path),
                          timeout=120)


def test_d4_cli_as_extra_path_nested_in_primary_root_exits_2(tmp_path):
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    r = _run_analyzer_cli([str(proj / "sub"), "--project", "W6", "--as-extra-path",
                           "--primary-root", str(proj)], tmp_path)
    assert r.returncode == 2, (r.returncode, r.stderr[-800:])
    assert "refusing --as-extra-path" in r.stderr


def test_d4_cli_as_extra_path_external_is_not_refused(tmp_path):
    """LEAVE-ALONE: a folder beside the project passes the overlap check. The
    lone ``--canonical-source`` makes the run stop at the NEXT argument check
    (exit 1, before any walk or ledger write), proving the overlap check let
    it through."""
    proj, clone = tmp_path / "proj", tmp_path / "clone"
    proj.mkdir()
    clone.mkdir()
    r = _run_analyzer_cli([str(clone), "--project", "W6", "--as-extra-path",
                           "--primary-root", str(proj), "--canonical-source", "/x"], tmp_path)
    assert "refusing --as-extra-path" not in r.stderr
    assert r.returncode == 1 and "--canonical-source requires" in r.stderr, r.stderr[-800:]
    assert "--canonical-source ignored with --as-extra-path" in r.stderr


def test_d4_refresh_skips_a_nested_row_with_a_log_line(tmp_path):
    from vco_lib import codegraph_extras_refresh as cer

    nested = str(tmp_path / "sub")
    cfg = SimpleNamespace(project_id="pid", code_graph_collection_prefix="Acme",
                          code_graph_extra_paths=[SimpleNamespace(
                              path=nested, enabled=True, last_indexed_commit=None)])
    ran: list = []
    deps = cer.Deps(resolve=lambda _r: cfg, git_head=lambda _p: "b" * 40,
                    run_analyzer=lambda *a, **k: ran.append(a))
    lines: list = []
    out = cer.refresh_extras(str(tmp_path), "/x/a.py", "/x/py", tmp_path / "st", lines.append, deps=deps)
    assert out == {nested: "overlaps_project"} and ran == []
    assert any("already indexed" in ln for ln in lines), lines
    out = cer.refresh_queued(str(tmp_path), "/x/a.py", "/x/py", tmp_path / "st", lines.append, deps=deps)
    assert out == {nested: "overlaps_project"} and ran == []
