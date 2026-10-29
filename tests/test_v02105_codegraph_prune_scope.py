# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 (prune lane) — the source-scoped `--prune-stale` and the targeted
orphan remedy.

The 2026-10-29 incident being closed: the `codegraph_orphan_interactions_
pending` entry's printed remedy re-walked the project root with
`--prune-stale`, whose stale set was judged over EVERY row of the prefix —
so it pruned all rows of every ENABLED extra code folder (33,464 rows on
VCO_dev where 727 orphans were expected), and nothing re-added them.

Under test:
* ``prune_collection(source_scope=...)`` — a primary walk judges only the
  walked root's rows; an ``--as-extra-path`` walk only the extra's; legacy
  unstamped rows are kept when the project has extra folders registered;
  the same scope gates the orphan reaping.
* ``prune_orphan_interactions`` — the TARGETED remedy: deletes only the
  provably-dead unanchored rows, never an extra folder's rows, and clears
  its own deferral on a clean pass.
* lost-extras detection/heal/purge in ``codegraph_extras_refresh`` and the
  ``codegraph_extra_rows_missing`` retry wiring.

All tests are pure-Python fakes (no Weaviate, no hub).
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

import vco_lib.codegraph_extras_refresh as xrf
import vco_lib.codegraph_resync as cr
import vco_lib.codegraph_source_scope as scope_mod
from vco_lib.codegraph_source_scope import PruneScope, prune_scope_for_walk

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_v02105_prune_scope_analyzer", str(_ANALYZER_PATH)
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
# Fakes (mirroring tests/test_codegraph_prune_preserve_skipped_v0282.py and
# tests/test_v02103_orphan_interaction_prune.py).
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
    """File-anchored collection stand-in (CodeModule / CodeFunction)."""

    def __init__(self, name: str, rows: List[Any], prop_names: List[str]) -> None:
        self.name = name
        self.deleted: List[str] = []
        self.data = _FakeData(self.deleted)
        self.config = _FakeConfig(prop_names)
        self._rows = rows

    def iterator(self, return_properties=None):  # noqa: ANN001 — mirror client
        return iter(self._rows)


def _row(uid: str, project: str, anchor_prop: str, anchor: str,
         source: Optional[str] = None) -> Any:
    props: dict = {"project": project, anchor_prop: anchor}
    if source is not None:
        props["project_source"] = source
    return SimpleNamespace(uuid=uid, properties=props)


class _RefObj:
    def __init__(self, properties):
        self.properties = dict(properties)


def _refs(*entries):
    out = {}
    for ref_prop, targets in entries:
        out[ref_prop] = types.SimpleNamespace(
            objects=[_RefObj(t) for t in targets])
    return out


def _fetch_obj(uid, refs):
    o = SimpleNamespace(uuid=uid, properties={"file_path": None})
    o.references = refs
    return o


class _InteractionsColl:
    """CodeInteraction-shaped fake with the reference fetch the ONE orphan
    predicate performs (mirrors test_v02103_orphan_interaction_prune._Coll,
    slimmed to what this file needs)."""

    def __init__(self, name="P_CodeInteraction", rows=None, fetch=None):
        self.name = name
        self._rows = rows or []
        self._fetch = dict(fetch or {})
        self.deleted: List[str] = []
        self.fetch_calls: List[str] = []
        self.config = _FakeConfig(["project", "file_path", "project_source"])
        self.data = _FakeData(self.deleted)
        self.query = SimpleNamespace(fetch_object_by_id=self._fetch_by_id)

    def _fetch_by_id(self, uuid, return_references=None):
        self.fetch_calls.append(str(uuid))
        return self._fetch.get(str(uuid))

    def iterator(self, return_properties=None):
        return iter(self._rows)


def _lang_id(v):
    return str(v or "").lower() or ""


# ---------------------------------------------------------------------------
# 1. prune_collection: the source scope
# ---------------------------------------------------------------------------

PRIMARY, EXTRA = "/repo", "/media/extra-clone"


def _scope(strict: bool = False, legacy: bool = False,
          extras: "list[str] | None" = None) -> PruneScope:
    return PruneScope(
        own_sources=(set(scope_mod.walk_own_sources(PRIMARY, PRIMARY))
                     | set(extras or [])),
        legacy_unowned=legacy, strict=strict, primary_root=PRIMARY,
        walked_extras=len(extras or []),
    )


def test_primary_prune_keeps_extra_rows_and_prunes_own_stale():
    """ACT (the incident shape): a primary-only --prune-stale walk must
    delete the primary root's stale rows and PRESERVE every extra-source
    row — pre-fix it deleted both."""
    functions = _FakeCollection(
        "P_CodeFunction",
        [_row("f-prim-stale", "P", "file_path", "gone.py", PRIMARY),
         _row("f-extra", "P", "file_path", "clone/x.py", EXTRA)],
        ["project", "file_path", "project_source"],
    )
    from vco_lib.codegraph_prune import prune_collection

    pruned, failures = prune_collection(
        functions, visited_uuids=set(), project_name="P",
        canonical_lang_id=_lang_id, source_scope=_scope(),
    )
    assert pruned == 1
    assert failures == 0
    assert functions.deleted == ["f-prim-stale"], (
        "the primary root's stale row is judged; the extra folder's row "
        "belongs to a root this walk did not cover and must survive"
    )


def test_strict_extra_walk_prunes_only_the_extras_rows():
    """ACT: an --as-extra-path walk judges ONLY the extra's rows — never the
    primary's, never legacy unstamped rows."""
    functions = _FakeCollection(
        "P_CodeFunction",
        [_row("f-prim", "P", "file_path", "a.py", PRIMARY),
         _row("f-legacy", "P", "file_path", "old.py", None),
         _row("f-extra-stale", "P", "file_path", "gone.py", EXTRA)],
        ["project", "file_path", "project_source"],
    )
    from vco_lib.codegraph_prune import prune_collection

    pruned, _ = prune_collection(
        functions, visited_uuids=set(), project_name="P",
        canonical_lang_id=_lang_id,
        source_scope=PruneScope(
            own_sources=set(scope_mod.walk_own_sources(EXTRA, EXTRA)),
            legacy_unowned=False, strict=True, primary_root=EXTRA),
    )
    assert functions.deleted == ["f-extra-stale"], (
        "an extra walk deletes only its own stale rows; the primary's row "
        "and a legacy unstamped row are never judged from it"
    )
    assert pruned == 1


def test_legacy_rows_kept_when_extras_registered_judged_when_not():
    """LEAVE-ALONE both ways: a legacy unstamped row is preserved when the
    project has extra folders registered (attribution unknowable) and judged
    as the primary root's when it has none."""
    from vco_lib.codegraph_prune import prune_collection

    modules = _FakeCollection(
        "P_CodeModule",
        [_row("m-legacy", "P", "path", "old.py", None)],
        ["project", "path", "project_source"],
    )
    prune_collection(
        modules, visited_uuids=set(), project_name="P",
        canonical_lang_id=_lang_id, source_scope=_scope(legacy=True),
    )
    assert modules.deleted == [], (
        "with extra folders registered, an unstamped row cannot be "
        "attributed — preserve it"
    )

    modules2 = _FakeCollection(
        "P_CodeModule",
        [_row("m-legacy", "P", "path", "old.py", None)],
        ["project", "path", "project_source"],
    )
    prune_collection(
        modules2, visited_uuids=set(), project_name="P",
        canonical_lang_id=_lang_id, source_scope=_scope(legacy=False),
    )
    assert modules2.deleted == ["m-legacy"], (
        "with no extra folders registered the unstamped rows are the "
        "primary root's — today's visited-only behaviour"
    )


def test_orphan_reaping_is_source_scoped():
    """ACT: the CodeInteraction orphan reaping honours the SAME scope — a
    provable orphan whose project_source is an extra root is never reaped
    from a primary walk (it converges on its own root's walk)."""
    from vco_lib.codegraph_prune import prune_collection

    rows = [
        # anchor-less, primary source, provably dead (no reference at all)
        _row("i-orph-prim", "P", "file_path", "", PRIMARY),
        # anchor-less, EXTRA source, provably dead — must SURVIVE this walk
        _row("i-orph-extra", "P", "file_path", "", EXTRA),
        # anchor-less, primary, resolvable reference — alive, kept
        _row("i-anchored", "P", "file_path", "", PRIMARY),
    ]
    inter = _InteractionsColl(rows=rows, fetch={
        "i-orph-prim": _fetch_obj("i-orph-prim", {}),
        "i-orph-extra": _fetch_obj("i-orph-extra", {}),
        "i-anchored": _fetch_obj(
            "i-anchored",
            _refs(("source_function", [{"file_path": "a.py"}]))),
    })
    pruned, failures = prune_collection(
        inter, visited_uuids=set(), project_name="P",
        canonical_lang_id=_lang_id, preserve_paths={("src/skipped.py")},
        reap_orphans=True, source_scope=_scope(),
    )
    assert failures == 0
    assert inter.deleted == ["i-orph-prim"], (
        "only the primary root's provable orphan is reaped; the extra's "
        "orphan and the anchored row survive"
    )


def test_prune_scope_for_walk_rules():
    """The ONE scope builder: raw+resolved forms of the primary, extras'
    forms joined, strict from --as-extra-path, None without a root."""
    s = prune_scope_for_walk(PRIMARY, [EXTRA])
    assert s is not None
    assert scope_mod.walk_own_sources(PRIMARY, PRIMARY) <= s.own_sources
    assert EXTRA in s.own_sources
    assert not s.strict and s.walked_extras == 1
    assert prune_scope_for_walk("") is None

    strict = prune_scope_for_walk(EXTRA, strict_source=True)
    assert strict is not None
    assert strict.strict and EXTRA in strict.own_sources
    assert PRIMARY not in strict.own_sources


def test_project_has_registered_extras_soft_fails_false(monkeypatch):
    """Hub down / unresolvable root → False (no extras known): the prune
    keeps its previous legacy-row behaviour; never raises."""
    import vco_lib.project_config as pc

    monkeypatch.setattr(
        pc, "resolve", lambda root: (_ for _ in ()).throw(
            pc.ResolverError("hub down")))
    assert scope_mod.project_has_registered_extras("/some/project") is False
    assert scope_mod.project_has_registered_extras("") is False

    class _Cfg:
        code_graph_extra_paths = (SimpleNamespace(enabled=False, path="/x"),)

    monkeypatch.setattr(pc, "resolve", lambda root: _Cfg())
    assert scope_mod.project_has_registered_extras("/some/project") is True, (
        "enabled OR disabled rows count — both mean attribution is unknowable"
    )


# ---------------------------------------------------------------------------
# 2. THE INCIDENT, END TO END: the orphan remedy AND a primary --prune-stale
#    over a project with an enabled extra folder — the extra rows survive.
# ---------------------------------------------------------------------------


def _incident_analyzer(analyzer_mod, *, hub_has_extras=True):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = "P"
    inst.visited_uuids = set()
    inst._track_visited = True
    inst._prune_discovered_paths = set()
    inst._prune_walked_paths = set()
    inst._prune_language = ""
    inst._refused_extra_sources = set()
    inst._dropped_extra_sources = set()
    inst._strict_source = False
    inst._walked_extra_roots = set()
    inst._analyze_repo_root_raw = PRIMARY
    inst._analyze_repo_root = Path(PRIMARY)
    inst.client = None
    inst.modules_collection = None
    inst.classes_collection = None
    inst.functions_collection = None
    inst.apis_collection = None
    inst.interactions_collection = None
    inst._hub_has_extras = hub_has_extras
    return inst


def test_extra_rows_survive_the_remedy_and_a_primary_prune(
        analyzer_mod, monkeypatch, capsys):
    """THE required red-proof scenario. A project with an ENABLED extra
    folder runs (a) the printed orphan remedy and (b) a primary
    ``--prune-stale`` walk. The extra folder's rows must survive BOTH; the
    provably-dead primary orphans and the primary stale rows are removed."""
    # (a) the TARGETED remedy over the same data as (b) shares.
    inter = _InteractionsColl(rows=[
        _row("i-orph-prim", "P", "file_path", "", PRIMARY),
        _row("i-orph-extra", "P", "file_path", "", EXTRA),
        _row("i-anchored", "P", "file_path", "", PRIMARY),
    ], fetch={
        "i-orph-prim": _fetch_obj("i-orph-prim", {}),
        "i-orph-extra": _fetch_obj("i-orph-extra", {}),
        "i-anchored": _fetch_obj(
            "i-anchored",
            _refs(("source_function", [{"file_path": "a.py"}]))),
    })
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "P")
    res = cr.prune_orphan_interactions(
        "P", repo_root=Path(PRIMARY), client=_client_for(inter))
    assert res["deleted"] == 1 and res["counted"] == 1, (
        "the targeted remedy deletes ONLY the provably-dead PRIMARY orphan"
    )
    assert inter.deleted == ["i-orph-prim"], (
        "the extra folder's orphan row is never judged by a project-root "
        "remedy"
    )

    # (b) the primary --prune-stale pass over the rest of the graph.
    functions = _FakeCollection(
        "P_CodeFunction",
        [_row("f-live", "P", "file_path", "a.py", PRIMARY),
         _row("f-stale", "P", "file_path", "gone.py", PRIMARY),
         _row("f-extra", "P", "file_path", "clone/x.py", EXTRA),
         _row("f-legacy", "P", "file_path", "old.py", None)],
        ["project", "file_path", "project_source"],
    )
    inter2 = _InteractionsColl(rows=[
        _row("i-orph-prim2", "P", "file_path", "", PRIMARY),
        _row("i-orph-extra2", "P", "file_path", "", EXTRA),
    ], fetch={
        "i-orph-prim2": _fetch_obj("i-orph-prim2", {}),
        "i-orph-extra2": _fetch_obj("i-orph-extra2", {}),
    })
    a = _incident_analyzer(analyzer_mod)
    a.modules_collection = None
    a.functions_collection = functions
    a.interactions_collection = inter2
    a.visited_uuids = {("P_CodeFunction", "f-live")}
    a._prune_discovered_paths = {(PRIMARY, "src/skipped.py")}
    a._prune_walked_paths = set()
    monkeypatch.setattr(
        scope_mod, "project_has_registered_extras", lambda root: True)
    monkeypatch.setattr(analyzer_mod, "_clear_after_reaping",
                        lambda analyzer: None)
    capsys.readouterr()
    a._prune_stale_objects()
    assert functions.deleted == ["f-stale"], (
        "the primary walk prunes ITS stale row; the extra row and the "
        "unattributable legacy row survive"
    )
    assert "f-extra" not in functions.deleted and "f-legacy" not in functions.deleted
    assert inter2.deleted == ["i-orph-prim2"], (
        "the reaping removes the primary orphan and keeps the extra's"
    )
    out = capsys.readouterr().out
    assert "other source roots" in out, (
        "the spared rows are reported with a count, not silent"
    )


def _client_for(*collections):
    by_name = {c.name: c for c in collections}
    return SimpleNamespace(
        collections=SimpleNamespace(
            exists=lambda n: n in by_name,
            get=lambda n: by_name[n],
        ),
        close=lambda: None,
    )


# ---------------------------------------------------------------------------
# 3. The targeted remedy itself
# ---------------------------------------------------------------------------


def test_targeted_remedy_command_targets_and_never_prune_stales():
    cmd = cr.orphan_cleanup_command("Acme Projects", Path("/repo"))
    assert "--prune-orphan-interactions" in cmd
    assert "--prune-stale" not in cmd
    assert "--project" in cmd and "Acme Projects" in cmd, (
        "the project name must survive copy-paste (shell-quoted)"
    )


def test_targeted_remedy_deletes_nothing_on_enumeration_failure(monkeypatch):
    class _Explodes(_InteractionsColl):
        def iterator(self, return_properties=None):
            raise RuntimeError("enumeration failed mid-scan")

    bad = _Explodes(name="P_CodeInteraction")
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "P")
    res = cr.prune_orphan_interactions(
        "P", repo_root=Path(PRIMARY), client=_client_for(bad))
    assert res["counted"] is None and res["deleted"] == 0, (
        "an enumeration failure authorises nothing (deferred-delete rule)"
    )


def test_targeted_remedy_cli_mode(monkeypatch, capsys):
    """The printed command is shipped code: the CLI flag exists and runs the
    targeted pass (a fake client through the module seam)."""
    inter = _InteractionsColl(rows=[
        _row("i-orph", "P", "file_path", "", PRIMARY),
    ], fetch={"i-orph": _fetch_obj("i-orph", {})})
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "P")
    monkeypatch.setattr(cr, "_build_client", lambda *a, **k: _client_for(inter))
    monkeypatch.setattr(
        cr, "clear_orphan_interactions_deferral_if_zero",
        lambda *a, **k: True)
    rc = cr._main(["--prune-orphan-interactions", "--project", "P",
                   "--repo-root", PRIMARY])
    out = capsys.readouterr().out
    assert rc == 0
    assert "orphan_interactions_deleted=1" in out


def test_clear_after_reaping_uses_the_raw_root_and_never_raises(monkeypatch):
    calls = []

    def _capture(project, *, repo_root=None, client=None):
        calls.append((project, repo_root))
        return True

    monkeypatch.setattr(
        cr, "clear_orphan_interactions_deferral_if_zero", _capture)
    link = "/repo-link"
    a = SimpleNamespace(
        project_name="P", _analyze_repo_root_raw=link,
        _analyze_repo_root=Path("/real-repo"), client=None)
    cr.clear_orphan_deferral_after_reaping(a)
    assert calls == [("P", Path(link))], (
        "R16-B-N1: the RAW root form decides — rows are stamped with it"
    )

    def _boom(*a, **k):
        raise RuntimeError("probe down")

    monkeypatch.setattr(cr, "clear_orphan_interactions_deferral_if_zero", _boom)
    cr.clear_orphan_deferral_after_reaping(a)  # must not raise


# ---------------------------------------------------------------------------
# 4. Count / purge primitives
# ---------------------------------------------------------------------------


def test_count_rows_for_project_source_exact_scan_fallback():
    """No aggregate on the fake → the exact per-row compare runs; a sibling
    path that merely shares tokens is not counted."""
    modules = _FakeCollection(
        "P_CodeModule",
        [_row("m-1", "P", "path", "a.py", EXTRA),
         _row("m-2", "P", "path", "b.py", EXTRA + "/nested"),
         _row("m-3", "P", "path", "c.py", PRIMARY)],
        ["project", "path", "project_source"],
    )
    assert cr.count_rows_for_project_source(
        "P", EXTRA, client=_client_for(modules)) == 1
    absent = _FakeCollection("P_CodeModule", [], ["project", "path"])
    assert cr.count_rows_for_project_source(
        "P", EXTRA, client=_client_for(absent)) == 0


def test_purge_rows_for_project_source_deletes_only_that_path():
    """ACT (the disabled-folder purge): rows stamped with the disabled path
    are deleted across the collections; every other row survives."""
    functions = _FakeCollection(
        "P_CodeFunction",
        [_row("f-disabled", "P", "file_path", "x.py", EXTRA),
         _row("f-primary", "P", "file_path", "x.py", PRIMARY)],
        ["project", "file_path", "project_source"],
    )
    report = cr.purge_rows_for_project_source(
        "P", EXTRA, client=_client_for(functions))
    assert functions.deleted == ["f-disabled"], (
        "the purge is scoped to the disabled path's rows only"
    )
    assert report["counts"] == {"P_CodeFunction": 1}
    assert report["failures"] == 0


def test_stored_project_sources_and_undeterminable():
    """ACT: the distinct non-empty stored sources; a legacy unstamped row is
    not reported (it is the primary root's business)."""
    modules = _FakeCollection(
        "P_CodeModule",
        [_row("m-1", "P", "path", "a.py", EXTRA),
         SimpleNamespace(uuid="m-legacy",
                         properties={"project": "P", "path": "b.py"}),
         _row("m-3", "P", "path", "c.py", PRIMARY)],
        ["project", "path", "project_source"],
    )
    got = cr.stored_project_sources("P", client=_client_for(modules))
    assert got == {EXTRA, PRIMARY}, "non-empty sources only, distinct"

    class _Explodes(_FakeCollection):
        def iterator(self, return_properties=None):
            raise RuntimeError("scan failed")

    bad = _Explodes("P_CodeModule", [], ["project"])
    assert cr.stored_project_sources("P", client=_client_for(bad)) is None, (
        "undeterminable ⇒ None — the caller must not purge on unknown"
    )


# ---------------------------------------------------------------------------
# 5. Lost-extras detection / emit / heal, disabled purge wiring
# ---------------------------------------------------------------------------


class _Extra:
    def __init__(self, path: str, enabled: bool,
                 last_indexed_commit: Optional[str] = None):
        self.path = path
        self.enabled = enabled
        self.last_indexed_commit = last_indexed_commit


class _Cfg:
    def __init__(self, extras, prefix="P", project_id="pid"):
        self.code_graph_extra_paths = tuple(extras)
        self.code_graph_collection_prefix = prefix
        self.project_id = project_id


def _extra_tree(tmp_path: Path) -> Path:
    root = tmp_path / "clone"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "m.py").write_text("def f():\n    return 1\n")
    return root


def test_missing_extra_paths_tri_state(tmp_path):
    clone = _extra_tree(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()

    def _resolve(root):
        return _Cfg([_Extra(clone.as_posix(), True),
                     _Extra(empty.as_posix(), True),
                     _Extra((tmp_path / "gone").as_posix(), True),
                     _Extra((tmp_path / "nested").as_posix(), True)])

    # nested path: refused — skip regardless of counts
    real_refusal = xrf.nested_extra_refusal
    xrf.nested_extra_refusal = (
        lambda path, root: "refused" if "nested" in path else "")
    try:
        counts = {clone.as_posix(): 0}

        def _count(prefix, path):
            return counts.get(path, 5)

        assert xrf.missing_extra_paths(
            "/proj", resolve=_resolve, count_rows=_count) == [clone.as_posix()], (
            "enabled + exists + indexable + 0 rows → missing; an empty "
            "folder and an unmounted path are not claimed")
        counts[clone.as_posix()] = 12
        assert xrf.missing_extra_paths(
            "/proj", resolve=_resolve, count_rows=_count) == [], (
            "rows present → positively not missing")

        def _unknown(prefix, path):
            return None

        assert xrf.missing_extra_paths(
            "/proj", resolve=_resolve, count_rows=_unknown) is None, (
            "undeterminable count → None, never a false claim")
    finally:
        xrf.nested_extra_refusal = real_refusal


def test_missing_extra_paths_none_on_config_failure():
    def _raise(root):
        raise RuntimeError("hub down")

    assert xrf.missing_extra_paths("/proj", resolve=_raise) is None


def test_build_entry_and_detect_emit(tmp_path, monkeypatch):
    clone = _extra_tree(tmp_path)
    emitted = []
    monkeypatch.setattr(
        "vco_lib.deferral_emit.emit",
        lambda folder, entry, log=None: emitted.append((folder, entry)))
    monkeypatch.setattr(
        "vco_lib.project_config.resolve",
        lambda root: _Cfg([_Extra(clone.as_posix(), True)]))
    monkeypatch.setattr(
        cr, "count_rows_for_project_source", lambda p, s: 0)

    missing = xrf.detect_and_emit_missing_extras("/proj")
    assert missing == [clone.as_posix()]
    assert len(emitted) == 1
    folder, entry = emitted[0]
    assert entry.condition_id == xrf.MISSING_EXTRAS_CONDITION_ID
    assert clone.as_posix() in entry.detected and "'P'" in entry.detected
    assert "deferral_retry" in entry.command_to_apply and "--folder" in (
        entry.command_to_apply), "the printed command is a real one"


def test_heal_runs_the_full_non_incremental_sync_argv(tmp_path, monkeypatch):
    clone = _extra_tree(tmp_path)
    monkeypatch.setattr(xrf, "git_head", lambda path: "abc123def456")
    runs = []

    def _run(python, analyzer, argv, timeout):
        runs.append(list(argv))
        return xrf.AnalyzerResult(True, "ok", files_analyzed=3,
                                  entities_indexed=9)

    recorded = []

    def _record(pid, path, commit, res):
        recorded.append((pid, path, commit))
        return True, "recorded"

    def _count(prefix, path):
        return 0  # missing at every detection (the heal probe below re-runs)

    outcomes = xrf.heal_missing_extra_paths(
        "/proj", "/analyzer.py", "/py", resolve=lambda r: _Cfg(
            [_Extra(clone.as_posix(), True)]),
        run=_run, count_rows=_count, record=_record)
    assert outcomes == {clone.as_posix(): "recorded"}
    assert runs and runs[0][:2] == [clone.as_posix(), "--project"], (
        "the heal walks the extra path under the project's prefix"
    )
    assert "--as-extra-path" in runs[0] and "--incremental" not in runs[0], (
        "FULL non-incremental re-sync (build_extra_sync_argv(path, prefix, "
        "False, None, root))"
    )
    assert "--primary-root" in runs[0]
    assert recorded and recorded[0][0] == "pid"
    assert recorded[0][2] == "abc123def456", (
        "the heal records the new commit like an ordinary refresh"
    )


def test_heal_performs_the_paired_clear_on_a_positive_empty(monkeypatch, tmp_path):
    """The CHILD's own narrow clear (the dispatcher never resolves): after a
    clean heal whose re-detection is a POSITIVE empty list, the ledger entry
    is resolved; an undeterminable re-detection never clears."""
    clone = _extra_tree(tmp_path)
    resolved = []
    monkeypatch.setattr(xrf, "git_head", lambda path: None)
    monkeypatch.setattr(
        "vco_lib.deferral_emit.resolve_conditions",
        lambda folder, cids, log=None: resolved.append(cids) or 1)
    counts: "list[Optional[int]]" = [0, 7]  # detect: 0; post-heal: present
    state = {"counts": counts}

    def _count(prefix, path):
        return state["counts"].pop(0) if state["counts"] else 7

    outcomes = xrf.heal_missing_extra_paths(
        "/proj", "/analyzer.py", "/py",
        resolve=lambda r: _Cfg([_Extra(clone.as_posix(), True)]),
        run=lambda *a: xrf.AnalyzerResult(True, "ok"),
        count_rows=_count, record=lambda *a: (True, "recorded"))
    assert outcomes == {clone.as_posix(): "healed"}
    assert resolved == [(xrf.MISSING_EXTRAS_CONDITION_ID,)], (
        "a POSITIVE empty re-detection resolves the entry"
    )

    # Undeterminable re-detection: no clear.
    resolved.clear()
    state["counts"] = [0, None]
    outcomes = xrf.heal_missing_extra_paths(
        "/proj", "/analyzer.py", "/py",
        resolve=lambda r: _Cfg([_Extra(clone.as_posix(), True)]),
        run=lambda *a: xrf.AnalyzerResult(True, "ok"),
        count_rows=_count, record=lambda *a: (True, "recorded"))
    assert outcomes == {clone.as_posix(): "healed"}
    assert resolved == [], "None is never a clear"


def test_heal_reports_failure_and_stays_owed(tmp_path):
    clone = _extra_tree(tmp_path)

    def _run(python, analyzer, argv, timeout):
        return xrf.AnalyzerResult(False, "exit 1")

    outcomes = xrf.heal_missing_extra_paths(
        "/proj", "/analyzer.py", "/py", resolve=lambda r: _Cfg(
            [_Extra(clone.as_posix(), True)]),
        run=_run, count_rows=lambda p, s: 0)
    assert outcomes == {clone.as_posix(): "analyzer_failed"}


def test_refresh_extras_purges_disabled_paths(tmp_path, monkeypatch):
    """ACT (item 4): the commit-stamp refresh purges the rows of an extra
    root that is no longer enabled-or-primary — a DISABLED row here, a
    REMOVED row (no config row at all) in the next test — and the enabled
    loop is untouched by the purge."""
    clone = _extra_tree(tmp_path)
    purged = []

    def _purge(prefix, path):
        purged.append((prefix, path))
        return {"counts": {"P_CodeModule": 4}, "failures": 0}

    logs = []
    monkeypatch.setattr(cr, "primary_sources_for", lambda p: {"/proj"})
    deps = xrf.Deps(
        resolve=lambda r: _Cfg(
            [_Extra(clone.as_posix(), True, last_indexed_commit=None),
             _Extra("/media/disabled-clone", False)]),
        git_head=lambda p: None,  # enabled loop: no_git_head → up-to-date
        purge=_purge,
    )
    monkeypatch.setattr(
        "vco_lib.codegraph_resync.stored_project_sources",
        lambda prefix: {clone.as_posix(), "/media/disabled-clone"})
    outcomes = xrf.refresh_extras(
        "/proj", "/analyzer.py", "/py", tmp_path / "state",
        lambda m: logs.append(m), deps=deps)
    assert purged == [("P", "/media/disabled-clone")], (
        "exactly the no-longer-walkable source is purged (the enabled clone "
        "and the primary root are in the keep set)"
    )
    assert any("purge-gone" in line and "deleted=4" in line
               for line in logs), "the purge logs its count"
    assert outcomes.get(clone.as_posix()) == "no_git_head"


def test_purge_gone_covers_a_removed_row_and_stamps(tmp_path, monkeypatch):
    """The launcher's Remove flow DELETES the config row and relied on the
    union prune to drop the rows — the source-scoped prune keeps them now,
    so the sweep must purge a stored source with NO config row at all. The
    keep-set stamp stops the sweep from re-running when nothing changed."""
    clone = _extra_tree(tmp_path)
    purged = []
    sweeps = []

    def _stored(prefix):
        sweeps.append(prefix)
        return {clone.as_posix(), "/media/removed-clone"}

    monkeypatch.setattr(cr, "primary_sources_for", lambda p: {"/proj"})
    state = tmp_path / "state"
    out = xrf.purge_gone_extra_paths(
        "/proj", state,
        resolve=lambda r: _Cfg([_Extra(clone.as_posix(), True)]),
        purge=lambda prefix, src: purged.append(src)
        or {"counts": {"P_CodeModule": 2}, "failures": 0},
        stored_sources=_stored, log=lambda m: None)
    assert purged == ["/media/removed-clone"], (
        "a source with no config row (Removed in the launcher) is purged"
    )
    assert sweeps, "the sweep enumerated the stored sources"
    # Second run with the SAME keep set: the stamp short-circuits the sweep.
    sweeps.clear()
    purged.clear()
    out2 = xrf.purge_gone_extra_paths(
        "/proj", state,
        resolve=lambda r: _Cfg([_Extra(clone.as_posix(), True)]),
        purge=lambda prefix, src: purged.append(src)
        or {"counts": {"P_CodeModule": 2}, "failures": 0},
        stored_sources=_stored, log=lambda m: None)
    assert sweeps == [] and purged == [] and out2 == {}, (
        "an unchanged keep set must not re-sweep"
    )
    assert out == {"/media/removed-clone": {
        "counts": {"P_CodeModule": 2}, "failures": 0}}


def test_purge_gone_deletes_nothing_on_undeterminable(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "primary_sources_for", lambda p: {"/proj"})
    called = []

    def _stored(prefix):
        return None

    out = xrf.purge_gone_extra_paths(
        "/proj", tmp_path / "state",
        resolve=lambda r: _Cfg([_Extra("/gone", True)]),
        purge=lambda prefix, src: called.append(src) or {},
        stored_sources=_stored, log=lambda m: None)
    assert out == {} and called == []
    # No stamp was written: the next pass retries the sweep.
    assert not (tmp_path / "state").exists() or not list(
        (tmp_path / "state").glob("codegraph_extra_keep_*.json"))


def test_purge_gone_swallows_config_failure(tmp_path):
    def _raise(root):
        raise RuntimeError("hub down")

    assert xrf.purge_gone_extra_paths(
        "/proj", tmp_path / "state", resolve=_raise,
        purge=lambda p, s: pytest.fail("never run"),
        stored_sources=lambda p: pytest.fail("never enumerated"),
        log=lambda m: None) == {}


# ---------------------------------------------------------------------------
# 6. The retry wiring (registry + handler + probe + session start)
# ---------------------------------------------------------------------------


def test_missing_extras_condition_registered_and_wired():
    from vco_lib.deferral_registry import condition
    from vco_lib import deferral_probes, deferral_retry

    spec = condition(xrf.MISSING_EXTRAS_CONDITION_ID)
    assert spec is not None
    assert spec.condition_class == "auto_retryable"
    assert spec.retry_action == "retry:py:codegraph_extras_heal"
    assert spec.clear_probe == "probe:py:codegraph_extra_rows_missing"
    assert callable(deferral_probes.PROBES["codegraph_extra_rows_missing"])
    assert "codegraph_extras_heal" in deferral_retry.HANDLERS


def _retry_ctx(folder):
    from vco_lib.deferral_retry import RetryContext

    return RetryContext(condition_id=xrf.MISSING_EXTRAS_CONDITION_ID,
                        folder=Path(folder))


def test_retry_handler_retried_on_positive_empty(monkeypatch, tmp_path):
    from vco_lib import deferral_retry as dr

    monkeypatch.setattr(xrf, "heal_missing_extra_paths",
                        lambda *a, **k: {"/clone": "recorded"})
    monkeypatch.setattr(xrf, "missing_extra_paths", lambda root: [])
    monkeypatch.setattr(cr, "_resolve_analyzer", lambda folder: Path("/a.py"))
    res = dr.retry_codegraph_extras_heal(_retry_ctx(tmp_path))
    assert res.status == dr.RETRIED, (
        "the child's own paired clear is the proof; the dispatcher's ledger "
        "re-read resolves the entry"
    )


def test_retry_handler_skips_on_undeterminable(monkeypatch, tmp_path):
    from vco_lib import deferral_retry as dr

    monkeypatch.setattr(xrf, "heal_missing_extra_paths", lambda *a, **k: {})
    monkeypatch.setattr(xrf, "missing_extra_paths", lambda root: None)
    monkeypatch.setattr("vco_lib.deferral_emit.resolve_conditions",
                        lambda *a, **k: pytest.fail("never resolved"))
    monkeypatch.setattr(cr, "_resolve_analyzer", lambda folder: Path("/a.py"))
    res = dr.retry_codegraph_extras_heal(_retry_ctx(tmp_path))
    assert res.status == dr.SKIPPED


def test_retry_handler_fails_when_rows_still_missing(monkeypatch, tmp_path):
    from vco_lib import deferral_retry as dr

    monkeypatch.setattr(xrf, "heal_missing_extra_paths",
                        lambda *a, **k: {"/clone": "analyzer_failed"})
    monkeypatch.setattr("vco_lib.deferral_emit.resolve_conditions",
                        lambda *a, **k: pytest.fail("never resolved"))
    monkeypatch.setattr(cr, "_resolve_analyzer", lambda folder: Path("/a.py"))
    res = dr.retry_codegraph_extras_heal(_retry_ctx(tmp_path))
    assert res.status == dr.FAILED


def test_missing_extras_probe_tri_state(monkeypatch, tmp_path):
    from vco_lib.deferral_probes import ProbeContext
    from vco_lib import deferral_probes as dp

    ctx = ProbeContext(
        folder=tmp_path, entry=SimpleNamespace(detected=""))
    monkeypatch.setattr(xrf, "missing_extra_paths", lambda root: ["/clone"])
    assert dp.codegraph_extra_rows_missing_probe(ctx) is True
    monkeypatch.setattr(xrf, "missing_extra_paths", lambda root: [])
    assert dp.codegraph_extra_rows_missing_probe(ctx) is False
    monkeypatch.setattr(xrf, "missing_extra_paths", lambda root: None)
    assert dp.codegraph_extra_rows_missing_probe(ctx) is None


def test_session_start_owed_check_runs_detection(monkeypatch, tmp_path):
    from vco_lib import deferral_retry as dr

    calls = []
    monkeypatch.setattr(xrf, "detect_and_emit_missing_extras",
                        lambda root: calls.append(root) or [])
    monkeypatch.setattr(dr, "owed_condition_ids", lambda folder: [])
    monkeypatch.setattr(dr, "spawn_detached", lambda *a, **k: None)
    assert dr.session_start_owed_check(tmp_path) == []
    assert calls == [str(tmp_path)], (
        "the session-start leg detects lost extras BEFORE reading the ledger"
    )
