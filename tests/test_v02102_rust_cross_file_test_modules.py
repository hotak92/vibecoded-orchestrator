# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-G — Rust test modules declared in one file, defined in another.

``#[cfg(test)] mod tests;`` in ``lib.rs`` makes ``tests.rs`` test code, but the
analyzer extracts one file at a time, so ``tests.rs`` used to be indexed as
production. ``vco_lib.codegraph_rust_modules`` is the per-crate pre-pass that
resolves gated declarations to their files; these tests pin:

* the resolution (separate file, ``#[path]``, ``x/mod.rs``, transitive
  descendants, non-mod-rs directories, inline-module nesting) and the
  test-only rule (``any(test, debug_assertions)`` gates, ``any(test,
  feature = "x")`` and an ungated ``mod tests;`` do not);
* the extractor drops a test-only file's classes/functions and salts its
  module hash, and leaves production files byte-identical;
* an incremental edit that ADDS or REMOVES the gate in ``lib.rs`` re-walks
  ``tests.rs`` (the changed-files filter re-queues it, the unchanged-file gate
  lets it through) and flips its rows; a dependent whose status did not flip
  is still skipped as unchanged;
* the per-edit single-file scope (``--only-file`` / drain) carries the same
  dependents through the analyzer's ``_single_file_dispatch``;
* without the Rust grammar nothing is gated (conservative).

Everything runs on a crate built under ``tmp_path``; no Weaviate.
"""
from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import types
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from vco_lib import codegraph_defs, codegraph_grammars, codegraph_rust_modules as crm
from vco_lib.codegraph_deleted_files import filter_changed_files
from vco_lib.codegraph_entities import KIND_CLASS, KIND_FUNCTION
from vco_lib.codegraph_lang.rust import analyze_rust_file, extract_rust_file

pytestmark = pytest.mark.skipif(
    codegraph_grammars.load_language("rust") is None,
    reason="tree-sitter Rust grammar not installed (codegraph-ts extra)",
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"

_LIB_GATED = """\
//! fixture crate
pub mod prod;
mod other;

#[cfg(test)]
mod tests;

#[cfg(test)]
#[path = "testing/helpers.rs"]
mod helpers;

#[cfg(any(test, debug_assertions))]
mod debug_only;

#[cfg(any(test, feature = "extra"))]
mod featured;

#[cfg(test)]
mod inline_tests {
    fn inline_case() {}
}

pub fn lib_fn() {}
"""

_FILES = {
    "Cargo.toml": "[package]\nname = \"fixture\"\nversion = \"0.1.0\"\n",
    "src/prod.rs": "pub struct Prod;\nimpl Prod { pub fn run(&self) {} }\nmod sub;\n",
    "src/prod/sub.rs": "pub fn sub_fn() {}\n",
    "src/other.rs": "pub fn other_fn() {}\nmod tests;\n",          # UNGATED mod tests;
    "src/other/tests.rs": "pub fn kept_case() {}\n",
    "src/tests.rs": "fn case_a() {}\nstruct Fixture;\nmod deep;\n",  # gated in lib.rs
    "src/tests/deep.rs": "fn deep_case() {}\n",                      # reached via tests.rs
    "src/testing/helpers.rs": "fn helper() {}\n",                    # #[path]
    "src/debug_only/mod.rs": "fn dbg_helper() {}\n",                 # x/mod.rs form
    "src/featured.rs": "pub fn featured_fn() {}\n",
}


class _Helpers:
    project_name = "W6G"

    def embed_class(self, *a: Any, **k: Any) -> None:
        return None

    def embed_function(self, *a: Any, **k: Any) -> None:
        return None

    def generate_embedding(self, *a: Any, **k: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def _fresh_cache():
    crm.clear_cache()
    yield
    crm.clear_cache()


def _crate(root: Path, lib: str = _LIB_GATED) -> Path:
    for rel, text in _FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (root / "src" / "lib.rs").write_text(lib)
    return root


def _names(root: Path, rel: str) -> List[str]:
    f = root / rel
    fx = extract_rust_file(f.read_text(), f, root, _Helpers())
    return [e.name for e in fx.entities if e.kind in (KIND_CLASS, KIND_FUNCTION)]


def _test_only(root: Path) -> Dict[str, bool]:
    return {p.relative_to(root).as_posix(): crm.rust_file_is_test_only(p, root)
            for p in sorted(root.rglob("*.rs"))}


# ---------------------------------------------------------------------------
# Resolution + rule
# ---------------------------------------------------------------------------


def test_declarations_carry_the_shared_test_only_rule() -> None:
    decls = codegraph_defs.extract_rust_module_declarations(_LIB_GATED)
    assert decls is not None
    by_name = {d.name: d for d in decls}
    assert set(by_name) == {"prod", "other", "tests", "helpers", "debug_only", "featured"}
    assert by_name["tests"].test_gated and by_name["helpers"].test_gated
    assert by_name["debug_only"].test_gated          # any(test, debug_assertions)
    assert not by_name["featured"].test_gated        # any(test, feature = "x")
    assert not by_name["prod"].test_gated and not by_name["other"].test_gated
    assert by_name["helpers"].path_attr == "testing/helpers.rs"


def test_file_level_inner_cfg_test_gates_every_declaration() -> None:
    decls = codegraph_defs.extract_rust_module_declarations("#![cfg(test)]\nmod a;\nmod b { mod c; }\n")
    assert decls is not None
    assert [(d.name, d.inline, d.test_gated) for d in decls] == [
        ("a", (), True), ("c", (("b", None),), True)]


def test_gated_files_resolve_and_everything_else_stays_production(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    assert _test_only(root) == {
        "src/debug_only/mod.rs": True,
        "src/featured.rs": False,
        "src/lib.rs": False,
        "src/other.rs": False,
        "src/other/tests.rs": False,     # ungated `mod tests;` is kept
        "src/prod.rs": False,
        "src/prod/sub.rs": False,
        "src/testing/helpers.rs": True,  # #[path]
        "src/tests.rs": True,            # separate-file `#[cfg(test)] mod tests;`
        "src/tests/deep.rs": True,       # transitively, through a test-only file
    }


def test_resolution_follows_the_module_path_rules(tmp_path: Path) -> None:
    pkg = tmp_path
    present = {
        pkg / "src" / "a" / "b.rs", pkg / "src" / "c" / "mod.rs",
        pkg / "src" / "a" / "inner" / "d.rs", pkg / "src" / "a" / "custom" / "e.rs",
        pkg / "src" / "x.rs", pkg / "tests" / "common.rs",
    }
    exists = present.__contains__
    D = codegraph_defs.RustModDecl

    def res(declaring: str, decl: Any) -> Any:
        return crm.resolve_declaration(pkg / declaring, decl, pkg, exists)

    # non-mod-rs file: children live in a directory named after it
    assert res("src/a.rs", D("b", None, (), False)) == pkg / "src/a/b.rs"
    # mod-rs (lib.rs): x/mod.rs form
    assert res("src/lib.rs", D("c", None, (), False)) == pkg / "src/c/mod.rs"
    # inline module nesting adds directories
    assert res("src/a.rs", D("d", None, (("inner", None),), False)) == pkg / "src/a/inner/d.rs"
    assert res("src/a.rs", D("e", None, (("m", "custom"),), False)) == pkg / "src/a/custom/e.rs"
    # top-level #[path] is relative to the declaring file's directory
    assert res("src/a/b.rs", D("y", "../x.rs", (), False)) == pkg / "src/x.rs"
    # Cargo target roots are mod-rs
    assert res("tests/it.rs", D("common", None, (), False)) == pkg / "tests/common.rs"
    # raw identifiers
    assert res("src/lib.rs", D("r#x", None, (), False)) == pkg / "src/x.rs"
    assert res("src/lib.rs", D("missing", None, (), False)) is None


def test_scan_is_bounded_by_the_nearest_package(tmp_path: Path) -> None:
    """A nested package is its own scope: its files are not module targets of
    the outer crate, and the outer crate's gate cannot reach them."""
    root = _crate(tmp_path)
    nested = root / "nested"
    (nested / "src").mkdir(parents=True)
    (nested / "Cargo.toml").write_text("[package]\nname = \"n\"\n")
    (nested / "src" / "lib.rs").write_text("mod tests;\n")
    (nested / "src" / "tests.rs").write_text("fn nested_kept() {}\n")
    assert crm.rust_file_is_test_only(nested / "src" / "tests.rs", root) is False
    assert crm.rust_file_is_test_only(root / "src" / "tests.rs", root) is True


@pytest.mark.parametrize("gated_first", [True, False])
def test_a_file_reached_through_both_a_gated_and_an_ungated_declaration_stays_production(
    tmp_path: Path, gated_first: bool,
) -> None:
    """Production wins: one ungated route is enough, whichever declaration
    comes first and whichever file declares it."""
    root = tmp_path
    (root / "Cargo.toml").write_text("[package]\nname = \"dual\"\n")
    (root / "src").mkdir()
    gated = "#[cfg(test)]\n#[path = \"shared.rs\"]\nmod shared_for_tests;\n"
    ungated = "#[path = \"shared.rs\"]\nmod shared;\n"
    (root / "src" / "lib.rs").write_text(
        "mod other;\n" + (gated + ungated if gated_first else ungated + gated))
    # A second declaring file adds a gated route from elsewhere too.
    (root / "src" / "other.rs").write_text("#[cfg(test)]\n#[path = \"shared.rs\"]\nmod again;\n")
    (root / "src" / "shared.rs").write_text("pub fn shared_fn() {}\n")
    (root / "src" / "only_gated.rs").write_text("fn g() {}\n")
    (root / "src" / "lib.rs").write_text(
        (root / "src" / "lib.rs").read_text() + "#[cfg(test)]\nmod only_gated;\n")
    assert crm.rust_file_is_test_only(root / "src" / "shared.rs", root) is False
    assert _names(root, "src/shared.rs") == ["shared_fn"]
    # control: the same crate's gated-only file IS test-only
    assert crm.rust_file_is_test_only(root / "src" / "only_gated.rs", root) is True


def test_a_project_without_rust_never_scans(tmp_path: Path, monkeypatch) -> None:
    """Zero cost for non-Rust projects: no entry point lists or parses
    anything when no `.rs` file is involved."""
    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("the Rust pre-pass scanned a non-Rust project")

    monkeypatch.setattr(crm, "_list_rust_files", boom)
    monkeypatch.setattr(crm, "_graph", boom)
    py = tmp_path / "app.py"
    py.write_text("def f():\n    return 1\n")
    (tmp_path / ".git").mkdir()
    assert crm.rust_file_is_test_only(py, tmp_path) is False
    assert crm.rust_module_dependents([py], tmp_path) == []
    assert crm.add_rust_module_dependents([py], [py], tmp_path) == [py]
    assert crm.single_file_scope(py, tmp_path, lambda p: False) == [py]
    assert filter_changed_files(tmp_path, [py], run=_git_diff_run(["app.py"])) == [py]


def test_without_the_grammar_nothing_is_gated(tmp_path: Path, monkeypatch) -> None:
    root = _crate(tmp_path)
    monkeypatch.setattr(codegraph_defs, "parse", lambda lang, data: None)
    assert not any(_test_only(root).values())


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


def test_extractor_drops_test_only_files_and_keeps_production(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    assert _names(root, "src/tests.rs") == []
    assert _names(root, "src/tests/deep.rs") == []
    assert _names(root, "src/testing/helpers.rs") == []
    assert _names(root, "src/debug_only/mod.rs") == []
    assert _names(root, "src/other/tests.rs") == ["kept_case"]
    assert _names(root, "src/featured.rs") == ["featured_fn"]
    assert _names(root, "src/prod.rs") == ["Prod", "run"]
    # the inline gated module was already dropped in-file; lib_fn stays
    assert _names(root, "src/lib.rs") == ["lib_fn"]


def test_module_hash_is_salted_only_for_test_only_files(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    for rel, salted in (("src/tests.rs", True), ("src/prod.rs", False), ("src/lib.rs", False)):
        f = root / rel
        text = f.read_text()
        fx = extract_rust_file(text, f, root, _Helpers())
        plain = hashlib.sha256(text.encode()).hexdigest()
        assert fx.module is not None
        assert (fx.module.file_hash != plain) is salted, rel
        assert fx.module.file_hash == crm.rust_effective_file_hash(text, f, root)


# ---------------------------------------------------------------------------
# Incremental: adding / removing the gate in lib.rs flips tests.rs
# ---------------------------------------------------------------------------


class _Ctx:
    """The two analyzer seams ``run_pure_extractor`` uses: the unchanged-file
    gate and the writer. Stored state = {path: (module hash, entity names)}."""

    def __init__(self) -> None:
        self.store: Dict[str, Tuple[str, List[str]]] = {}
        self.walked: List[str] = []
        self.project_name = "W6G"

    def _get_existing_module(self, path: str, file_hash: str):  # noqa: ANN202
        row = self.store.get(path)
        return "uuid" if row is not None and row[0] == file_hash else None

    def write_file_extraction(self, fx) -> Dict[str, int]:  # noqa: ANN001
        self.walked.append(fx.module.path)
        self.store[fx.module.path] = (
            fx.module.file_hash,
            [e.name for e in fx.entities if e.kind in (KIND_CLASS, KIND_FUNCTION)],
        )
        return {"modules": 1, "classes": 0, "functions": 0}

    def _embed_class(self, *a: Any, **k: Any) -> None:  # ExtractorHelpers seams
        return None

    _embed_function = _embed_class
    _generate_embedding = _embed_class


def _git_diff_run(changed: List[str]):  # noqa: ANN202
    def run(argv, **kw):  # noqa: ANN001,ANN003,ANN202
        assert argv[:3] == ["git", "diff", "--name-only"], argv
        return subprocess.CompletedProcess(argv, 0, stdout="\n".join(changed) + "\n", stderr="")
    return run


def _incremental(ctx: _Ctx, root: Path, changed: List[str]) -> List[str]:
    files = sorted(root.rglob("*.rs"))
    queued = filter_changed_files(root, files, run=_git_diff_run(changed))
    ctx.walked.clear()
    for f in queued:
        analyze_rust_file(ctx, f, root)
    return [p.relative_to(root).as_posix() for p in queued]


def test_incremental_edit_adding_and_removing_the_gate_rewalks_the_test_file(tmp_path: Path) -> None:
    ungated = _LIB_GATED.replace("#[cfg(test)]\nmod tests;", "mod tests;")
    root = _crate(tmp_path, lib=ungated)
    (root / ".git").mkdir()
    ctx = _Ctx()
    for f in sorted(root.rglob("*.rs")):  # the initial full walk
        analyze_rust_file(ctx, f, root)
    assert ctx.store["src/tests.rs"][1] == ["Fixture", "case_a"]
    assert ctx.store["src/tests/deep.rs"][1] == ["deep_case"]

    # Edit 1: ADD the gate. Only lib.rs is in the diff.
    (root / "src" / "lib.rs").write_text(_LIB_GATED)
    queued = _incremental(ctx, root, ["src/lib.rs"])
    assert "src/tests.rs" in queued and "src/tests/deep.rs" in queued
    assert "src/tests.rs" in ctx.walked and "src/tests/deep.rs" in ctx.walked
    assert ctx.store["src/tests.rs"][1] == []
    assert ctx.store["src/tests/deep.rs"][1] == []
    # a dependent whose status did not flip is queued but skipped as unchanged
    assert "src/prod.rs" in queued and "src/prod.rs" not in ctx.walked

    # Edit 2: REMOVE the gate again — the rows come back.
    (root / "src" / "lib.rs").write_text(ungated)
    _incremental(ctx, root, ["src/lib.rs"])
    assert ctx.store["src/tests.rs"][1] == ["Fixture", "case_a"]
    assert ctx.store["src/tests/deep.rs"][1] == ["deep_case"]

    # No Rust change → no dependents re-queued.
    assert _incremental(ctx, root, []) == []


def test_non_rust_changes_are_left_alone(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    (root / ".git").mkdir()
    readme = root / "README.md"
    readme.write_text("x")
    assert filter_changed_files(root, [readme], run=_git_diff_run(["README.md"])) == [readme]


# ---------------------------------------------------------------------------
# Per-edit single-file scope (the analyzer's --only-file / drain path)
# ---------------------------------------------------------------------------


def test_single_file_scope_carries_dependents_through_the_walk_exclusions(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    lib = root / "src" / "lib.rs"
    scope = crm.single_file_scope(lib, root, lambda p: False)
    assert scope[0] == lib
    assert root / "src" / "tests.rs" in scope and root / "src" / "prod" / "sub.rs" in scope
    narrowed = crm.single_file_scope(lib, root, lambda p: "testing" in p.parts)
    assert root / "src" / "testing" / "helpers.rs" not in narrowed
    leaf = root / "src" / "prod" / "sub.rs"
    assert crm.single_file_scope(leaf, root, lambda p: False) == [leaf]


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_w6g_analyze_code_graph", str(_ANALYZER_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_analyzer_single_file_dispatch_includes_rust_module_dependents(
    analyzer_mod: types.ModuleType, tmp_path: Path,
) -> None:
    root = _crate(tmp_path).resolve()
    (root / "target").mkdir()
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    dispatch = [("rust", inst._find_rust_files, inst._analyze_rust_file)]
    out = inst._single_file_dispatch(root / "src" / "lib.rs", root, dispatch, None)
    assert len(out) == 1
    _lang, _fn, files, _root = out[0]
    rel = {f.relative_to(root).as_posix() for f in files}
    assert {"src/lib.rs", "src/tests.rs", "src/tests/deep.rs", "src/testing/helpers.rs"} <= rel
    # a leaf edit stays exactly the one file
    out = inst._single_file_dispatch(root / "src" / "prod" / "sub.rs", root, dispatch, None)
    assert out[0][2] == [root / "src" / "prod" / "sub.rs"]
