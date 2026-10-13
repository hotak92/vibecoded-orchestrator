# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-O review fix round (V02102-W6N-W6O-REVIEW.md, "W6-O findings").

* HIGH-1 — the reverse ``imports`` query capped the raw rows BEFORE dropping
  wrong-language importers (one live ``time.rs`` had 191 wrong importers, so
  the answer was empty with ``truncated: true``); the stored wrong edges
  survived every walk and every vector-copy migration. Owner decision
  2026-10-12: DELETE them on every walk (idempotent, no rung needed).
* MEDIUM-2 — three resolver paths still produced wrong edges (``@/`` alias,
  sysroot-named workspace crate, literal shell path falling through to a
  suffix match anywhere).
* MEDIUM-3 — a Rust crate was keyed by its folder, not the ``Cargo.toml``
  name (this repo's ``launcher/src-tauri`` is ``vct_launcher_temp_lib``).
* MEDIUM-4 — one cross-language fixture per read path (MCP dependencies, MCP
  reverse imports, CLI dependencies).

In-memory fakes only — no Weaviate, no network.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _extra in (REPO_ROOT / "claude_mcp_servers", REPO_ROOT):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from vco_lib import codegraph_imports as ci  # noqa: E402
from vco_lib.codegraph_imports import (  # noqa: E402
    build_import_index,
    incompatible_import_edges,
    purge_incompatible_import_edges,
    resolve_import_targets,
)


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ─────────────────────────────── MEDIUM-2 ───────────────────────────────────


def test_at_alias_resolves_from_the_nearest_src_not_the_importers_folder() -> None:
    index = build_import_index([
        "web/src/components/Card.ts",
        "web/src/components/utils.ts",   # same name beside the importer
        "web/src/utils.ts",              # what `@/utils` means
    ])
    assert resolve_import_targets("web/src/components/Card.ts", "@/utils", index) == [
        "web/src/utils.ts"]
    assert resolve_import_targets("web/src/components/Card.ts", "~/utils", index) == [
        "web/src/utils.ts"]


def test_at_alias_without_any_src_resolves_from_the_repo_root() -> None:
    index = build_import_index(["app/pages/a.ts", "app/pages/lib.ts", "lib.ts"])
    assert resolve_import_targets("app/pages/a.ts", "@/lib", index) == ["lib.ts"]


@pytest.mark.parametrize("crate", ["std", "core", "alloc", "proc_macro", "test"])
def test_sysroot_named_workspace_crate_never_captures_sysroot_imports(crate) -> None:
    index = build_import_index([f"crates/{crate}/src/lib.rs", f"crates/{crate}/src/mem.rs",
                                "crates/app/src/main.rs"])
    assert crate not in index.rust_crates
    assert resolve_import_targets("crates/app/src/main.rs", f"{crate}::mem", index) == []


def test_literal_shell_path_that_is_not_there_does_not_suffix_match() -> None:
    index = build_import_index(["a/run.sh", "b/x/common.sh"])
    assert resolve_import_targets("a/run.sh", "./common.sh", index) == []
    assert resolve_import_targets("a/run.sh", "common.sh", index) == []
    # leave-alone: a DYNAMIC prefix still resolves by its literal tail
    assert resolve_import_targets("a/run.sh", '"$DIR/x/common.sh"', index) == ["b/x/common.sh"]


# ─────────────────────────────── MEDIUM-3 ───────────────────────────────────

_TAURI = ["launcher/src-tauri/src/lib.rs", "launcher/src-tauri/src/main.rs",
          "launcher/src-tauri/src/logging.rs"]


def test_rust_crate_is_indexed_by_its_cargo_lib_name_in_this_repo() -> None:
    """The real shape: folder `src-tauri`, `[lib] name = "vct_launcher_temp_lib"`."""
    index = build_import_index(_TAURI, repo_root=str(REPO_ROOT))
    assert index.rust_crates.get("vct_launcher_temp_lib") == "launcher/src-tauri/src"
    assert "src_tauri" not in index.rust_crates
    assert resolve_import_targets("launcher/src-tauri/src/main.rs",
                                  "vct_launcher_temp_lib::logging::init_early", index) == [
        "launcher/src-tauri/src/logging.rs"]


def test_rust_crate_name_from_package_then_folder_fallback(tmp_path) -> None:
    pkg = tmp_path / "crates" / "my-dir"
    pkg.mkdir(parents=True)
    (pkg / "Cargo.toml").write_text('[package]\nname = "my-pkg"\nversion = "0.1.0"\n')
    paths = ["crates/my-dir/src/lib.rs", "crates/my-dir/src/a.rs",
             "crates/nomanifest/src/lib.rs", "crates/app/src/main.rs"]
    index = build_import_index(paths, repo_root=str(tmp_path))
    assert index.rust_crates["my_pkg"] == "crates/my-dir/src"
    assert "my_dir" not in index.rust_crates
    assert index.rust_crates["nomanifest"] == "crates/nomanifest/src"  # no Cargo.toml → folder
    assert resolve_import_targets("crates/app/src/main.rs", "my_pkg::a", index) == [
        "crates/my-dir/src/a.rs"]
    # without a root the folder name is the only evidence (pre-fix behaviour)
    assert "my_dir" in build_import_index(paths).rust_crates


def test_sysroot_name_declared_in_cargo_toml_is_still_excluded(tmp_path) -> None:
    pkg = tmp_path / "x"
    pkg.mkdir()
    (pkg / "Cargo.toml").write_text('[package]\nname = "core"\n')
    index = build_import_index(["x/src/lib.rs"], repo_root=str(tmp_path))
    assert index.rust_crates == {}


# ─────────────────────── HIGH-1: delete stored wrong edges ──────────────────


def test_incompatible_edges_need_both_languages_known() -> None:
    path_by_uuid = {"py": "pkg/usage.py", "rs": "src/logging.rs", "py2": "pkg/log.py",
                    "odd": "docs/notes.unknownext"}
    edges = incompatible_import_edges(
        {"py": ["rs", "rs", "py2", "odd", "ghost"]}, path_by_uuid)
    assert edges == [("py", "rs")]  # duplicates collapse; unknown/absent left alone


class _DeleteRecorder:
    def __init__(self, name="P_CodeModule"):
        self.name = name
        self.calls: list = []
        self.data = SimpleNamespace(reference_delete=self._delete)

    def _delete(self, from_uuid, from_property, to):
        self.calls.append((from_uuid, from_property, to))


def test_purge_deletes_one_qualified_beacon_per_wrong_edge() -> None:
    coll = _DeleteRecorder()
    n = purge_incompatible_import_edges(
        coll, {"m-py": ["m-rs", "m-rs", "m-py2"]},
        {("", "pkg/usage.py"): "m-py", ("", "src/logging.rs"): "m-rs", "pkg/log.py": "m-py2"})
    assert n == 1
    (from_uuid, prop, to), = coll.calls
    assert (from_uuid, prop) == ("m-py", "imports")
    assert getattr(to, "target_collection", None) == "P_CodeModule"
    assert str(to.uuids) == "m-rs"


def test_purge_leave_alone_and_kill_switch(monkeypatch) -> None:
    coll = _DeleteRecorder()
    cache = {"pkg/a.py": "a", "pkg/b.py": "b", "x.unknownext": "u"}
    assert purge_incompatible_import_edges(coll, {"a": ["b", "u", "ghost"]}, cache) == 0
    assert coll.calls == []
    monkeypatch.setattr(ci, "DELETE_INCOMPATIBLE_IMPORT_EDGES", False)
    assert purge_incompatible_import_edges(
        coll, {"a": ["r"]}, {"pkg/a.py": "a", "src/r.rs": "r"}) == 0
    assert coll.calls == []


def test_purge_soft_fails_per_edge() -> None:
    coll = _DeleteRecorder()

    def _boom(from_uuid, from_property, to):
        if from_uuid == "a":
            raise RuntimeError("422")
        coll.calls.append(from_uuid)

    coll.data.reference_delete = _boom
    cache = {"pkg/a.py": "a", "pkg/c.py": "c", "src/r.rs": "r"}
    assert purge_incompatible_import_edges(coll, {"a": ["r"], "c": ["r"]}, cache) == 1
    assert coll.calls == ["c"]


_IDEMPOTENCE = REPO_ROOT / "tests" / "test_v0292_analyzer_reference_idempotence.py"


@pytest.fixture(scope="module")
def harness():
    pytest.importorskip("weaviate.collections.classes.internal")
    return _load("_w6o_fix_idem_harness", _IDEMPOTENCE)


@pytest.fixture(scope="module")
def analyzer_mod():
    return _load("_w6o_fix_acg", REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py")


def _stale_world(harness):
    """An install whose pre-W6-O analyzer stored wrong-language edges."""
    rows = {
        "m-usage": harness._StoredRow("m-usage", {
            "path": "pkg/usage.py", "project_source": "", "import_names": ["logging"]}),
        "m-log": harness._StoredRow("m-log", {"path": "pkg/logging.py", "project_source": ""}),
        "m-rs": harness._StoredRow("m-rs", {"path": "src/logging.rs", "project_source": ""}),
        "m-sh": harness._StoredRow("m-sh", {"path": "scripts/logging.sh", "project_source": ""}),
        "m-svelte": harness._StoredRow("m-svelte", {
            "path": "web/src/App.svelte", "project_source": ""}),
        "m-odd": harness._StoredRow("m-odd", {"path": "docs/notes.unknownext",
                                              "project_source": ""}),
    }
    rows["m-usage"].beacons["imports"] = ["m-rs", "m-rs", "m-sh", "m-log", "m-odd", "m-ghost"]
    rows["m-svelte"].beacons["imports"] = ["m-usage"]   # Svelte → Python: wrong
    ghost = harness._StoredRow("m-ghost", {"path": "elsewhere/ghost.rs"})
    mods = harness._FakeCollection("P_CodeModule", rows, targets={**rows, "m-ghost": ghost})

    def _delete(from_uuid, from_property, to):
        # Weaviate removes every beacon equal to the one named; the analyzer's
        # beacons are class-qualified, so the request must name the class.
        assert getattr(to, "target_collection", None) == "P_CodeModule"
        target = str(to.uuids)
        beacons = mods.rows[str(from_uuid)].beacons.get(from_property, [])
        mods.rows[str(from_uuid)].beacons[from_property] = [b for b in beacons if b != target]

    mods.data.reference_delete = _delete
    return mods, harness._FakeCollection("P_CodeClass", {}), harness._FakeCollection(
        "P_CodeFunction", {})


@pytest.mark.parametrize("changed_files", [None, {"pkg/usage.py"}], ids=["forced", "incremental"])
def test_one_ordinary_walk_converges_a_stale_install(harness, analyzer_mod, changed_files) -> None:
    mods, classes, funcs = _stale_world(harness)
    stats = harness._analyzer(analyzer_mod, mods, classes, funcs).create_cross_references(
        changed_files=changed_files)
    # wrong edges gone; the right one, the undecidable ones and the unseen
    # target are untouched (and the module's ref set was never replaced)
    assert harness._beacons(mods, "m-usage", "imports") == ["m-log", "m-odd", "m-ghost"]
    assert harness._beacons(mods, "m-svelte", "imports") == []
    assert stats["imports_deleted"] == 3
    # idempotent: the next walk finds nothing to delete and makes no call
    mods.data.reference_delete = lambda **kw: pytest.fail("second walk deleted again")
    again = harness._analyzer(analyzer_mod, mods, classes, funcs).create_cross_references(
        changed_files=changed_files)
    assert again["imports_deleted"] == 0


def test_analyzer_reads_cargo_names_from_the_walked_repo(harness, analyzer_mod, tmp_path) -> None:
    """MEDIUM-3 wiring: the cross-reference pass builds its index with the
    walked repository as `repo_root`, so a crate named in `Cargo.toml` links."""
    pkg = tmp_path / "launcher" / "src-tauri"
    pkg.mkdir(parents=True)
    (pkg / "Cargo.toml").write_text('[package]\nname = "app"\n[lib]\nname = "app_lib"\n')
    rows = {
        "m-lib": harness._StoredRow("m-lib", {"path": "launcher/src-tauri/src/lib.rs",
                                              "project_source": ""}),
        "m-log": harness._StoredRow("m-log", {"path": "launcher/src-tauri/src/logging.rs",
                                              "project_source": ""}),
        "m-main": harness._StoredRow("m-main", {"path": "launcher/src-tauri/src/main.rs",
                                                "project_source": "",
                                                "import_names": ["app_lib::logging::init"]}),
    }
    mods = harness._FakeCollection("P_CodeModule", rows)
    inst = harness._analyzer(analyzer_mod, mods, harness._FakeCollection("P_CodeClass", {}),
                             harness._FakeCollection("P_CodeFunction", {}))
    inst._analyze_repo_root = tmp_path
    inst.create_cross_references()
    assert harness._beacons(mods, "m-main", "imports") == ["m-log"]


def test_vector_copy_does_not_carry_wrong_language_imports() -> None:
    from vco_lib import codegraph_vector_copy as vc

    def _t(uuid, path):
        return SimpleNamespace(uuid=uuid, properties={"path": path} if path else {})

    src = SimpleNamespace(
        properties={"path": "pkg/usage.py"},
        references={"imports": SimpleNamespace(objects=[
            _t("t-py", "pkg/log.py"), _t("t-rs", "src/logging.rs"),
            _t("t-odd", "docs/notes.unknownext"), _t("t-nopath", None)])})
    assert vc._read_reference_targets(src, "CodeModule") == {
        "imports": ["t-py", "t-odd", "t-nopath"]}
    # other references are never language-filtered
    fn = SimpleNamespace(properties={"file_path": "pkg/a.py"},
                         references={"module": SimpleNamespace(objects=[_t("m", "src/x.rs")])})
    assert vc._read_reference_targets(fn, "CodeFunction") == {"module": ["m"]}


# ─────────────────────── MEDIUM-4: the three read paths ─────────────────────

from tests.test_v02101_kg_exact_path_delete import _TokPredicate  # noqa: E402
from tests.test_v02101_read_path_exact_match import (  # noqa: E402
    PROJ,
    _obj,
    _RefColl,
    _structure,
    _TokRefFilter,
)

from weaviate_mcp import server as srv  # noqa: E402


class _ImportsRefFilter(_TokRefFilter):
    """`by_ref("imports").by_property("path").equal(v)` matches rows whose
    fake `_imports` list holds `v` (the stored beacon targets' paths)."""

    @staticmethod
    def by_ref(name):
        def _by_property(prop):
            return SimpleNamespace(equal=lambda v: _TokPredicate(
                lambda p: v in p.get(f"_{name}", ())))
        return SimpleNamespace(by_property=_by_property,
                               by_id=lambda: SimpleNamespace(
                                   equal=lambda _v: _TokPredicate(lambda _p: False)))


def _reverse(monkeypatch, rows, links, target):
    monkeypatch.setattr(srv, "Filter", _ImportsRefFilter)
    prefix = srv._code_sanitize_collection_prefix(PROJ)
    coll = _RefColl(rows, links)
    monkeypatch.setattr(srv, "get_weaviate_client", lambda *a, **k: SimpleNamespace(
        collections=SimpleNamespace(get=lambda n: coll if n == f"{prefix}_CodeModule"
                                    else (_ for _ in ()).throw(RuntimeError(n)))))
    return json.loads(srv.query_code_structure("imports", target, project=PROJ)), coll


def _importers(n_wrong, n_right, target_path="src/time.rs"):
    target = _obj({"path": target_path, "project": PROJ}, uuid="t")
    rows, links = [], {"imports": {}}
    for i in range(n_wrong):   # stale Python "importers" first, as stored
        rows.append(_obj({"path": f"py/m{i}.py", "project": PROJ, "_imports": [target_path]},
                         uuid=f"w{i}"))
    for i in range(n_right):
        rows.append(_obj({"path": f"src/r{i}.rs", "project": PROJ, "_imports": [target_path]},
                         uuid=f"r{i}"))
    for r in rows:
        links["imports"][str(r.uuid)] = [target]
    return rows + [target], links


def test_mcp_reverse_imports_filters_language_before_the_limit(monkeypatch) -> None:
    rows, links = _importers(191, 3)
    data, _ = _reverse(monkeypatch, rows, links, "src/time.rs")
    assert data["success"] is True, data
    assert sorted(r["path"] for r in data["results"]) == ["src/r0.rs", "src/r1.rs", "src/r2.rs"]
    assert data["truncated"] is False and data["limit"] == 20


@pytest.mark.parametrize("n_right,want_truncated", [(20, False), (25, True)])
def test_mcp_reverse_imports_truncation_counts_compatible_importers(
        monkeypatch, n_right, want_truncated) -> None:
    rows, links = _importers(50, n_right)
    data, _ = _reverse(monkeypatch, rows, links, "src/time.rs")
    assert len(data["results"]) == 20
    assert all(r["path"].endswith(".rs") for r in data["results"])
    assert data["truncated"] is want_truncated


def test_mcp_dependencies_hides_cross_language_targets(monkeypatch) -> None:
    a = _obj({"path": "pkg/usage.py", "project": PROJ}, uuid="m-a")
    py = _obj({"path": "pkg/log.py", "project": PROJ}, uuid="m-py")
    rs = _obj({"path": "src/logging.rs", "project": PROJ}, uuid="m-rs")
    data = _structure(monkeypatch, {"CodeModule": _RefColl([a, py, rs],
                                                           {"imports": {"m-a": [rs, py]}})},
                      "dependencies", "pkg/usage.py")
    assert [r["path"] for r in data["results"]] == ["pkg/log.py"], data


def test_cli_dependencies_hides_cross_language_targets(capsys) -> None:
    cross = pytest.importorskip("weaviate.collections.classes.internal")._CrossReference
    cli = _load("_qcg_w6o_fix", REPO_ROOT / "templates" / "scripts" / "query_code_graph.py")
    row = SimpleNamespace(uuid="m-a", properties={"path": "pkg/a.py"}, references={
        "imports": cross._from([SimpleNamespace(uuid="m-b", properties={"path": "pkg/b.py"}),
                                SimpleNamespace(uuid="m-r", properties={"path": "src/b.rs"})])})
    q = cli.CodeGraphQuery(project="Alpha")
    coll = SimpleNamespace(query=SimpleNamespace(
        fetch_objects=lambda **kw: SimpleNamespace(objects=[row])))
    q.client = SimpleNamespace(collections=SimpleNamespace(get=lambda n: coll))
    q.query_structure("dependencies", "pkg/a.py")
    out = capsys.readouterr().out
    assert "Imports 1 modules" in out and "pkg/b.py" in out
    assert "src/b.rs" not in out
