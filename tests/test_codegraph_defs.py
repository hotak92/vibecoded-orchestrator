# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for the tree-sitter definitions layer and the shared grammar
home (``vco_lib/codegraph_defs.py``, ``vco_lib/codegraph_grammars.py``,
v0.2.102 W6-E).

The per-language correctness bar lives in
``tests/test_codegraph_definitions_truth.py``; this module pins the plumbing:
the grammar table stays in step with the pinned extra, a missing grammar falls
back to the regex extractor (act + leave-alone), each fallback/path is logged
once, and the Rust test-gating predicate never mistakes production code for
test code.
"""
from __future__ import annotations

import importlib.util
import logging
import re
from pathlib import Path

import pytest

from vco_lib import codegraph_defs, codegraph_grammars
from vco_lib.codegraph_lang.csharp import extract_csharp_file
from vco_lib.codegraph_lang.rust import extract_rust_file
from tests._codegraph_truth_harness import TRUTH_ROOT, StubHelpers, force_regex_definitions

_REPO = Path(__file__).resolve().parent.parent


def _ts_available(grammar: str) -> bool:
    return codegraph_grammars.load_language(grammar) is not None


# ---------------------------------------------------------------------------
# Grammar table <-> pinned extra
# ---------------------------------------------------------------------------


def test_every_grammar_module_is_pinned_in_the_codegraph_ts_extra() -> None:
    pyproject = (_REPO / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^codegraph-ts = \[(.*?)^\]", pyproject, re.MULTILINE | re.DOTALL)
    assert block, "pyproject.toml lost its [codegraph-ts] extra"
    pinned = set(re.findall(r'"(tree-sitter-[\w-]+) ==', block.group(1)))
    for lang, (module, _factory) in codegraph_grammars.GRAMMARS.items():
        package = module.replace("_", "-")
        assert package in pinned, f"{lang}: grammar module {module} is not pinned in codegraph-ts"


def test_every_definitions_walker_has_a_grammar() -> None:
    for lang, (grammar, _walker) in codegraph_defs._WALKERS.items():
        assert grammar in codegraph_grammars.GRAMMARS, lang


# ---------------------------------------------------------------------------
# Fallback: grammar unavailable -> None -> regex extractor (act + leave-alone)
# ---------------------------------------------------------------------------


def test_missing_grammar_returns_none_and_logs_once(monkeypatch, caplog) -> None:
    monkeypatch.setitem(codegraph_grammars.GRAMMARS, "nolang", ("tree_sitter_does_not_exist", "language"))
    monkeypatch.setattr(codegraph_grammars, "_LANGUAGES", {})
    with caplog.at_level(logging.INFO, logger="vco_lib.codegraph_grammars"):
        assert codegraph_grammars.load_language("nolang") is None
        assert codegraph_grammars.load_language("nolang") is None
    hits = [r for r in caplog.records if "unavailable for 'nolang'" in r.getMessage()]
    assert len(hits) == 1, "the unavailable-grammar line must be logged exactly once"


def test_unknown_language_has_no_definitions() -> None:
    assert codegraph_defs.extract_definitions("powershell", "function F { }") is None
    assert codegraph_defs.extract_definitions("proto", "message M {}") is None


def test_regex_fallback_runs_when_the_grammar_is_absent(monkeypatch) -> None:
    """Leave-alone: no grammar -> the extractor still produces rows (regex)."""
    fixture = TRUTH_ROOT / "rust" / "engine.rs"
    monkeypatch.setattr(codegraph_defs, "parse", lambda lang, data: None)
    assert codegraph_defs.extract_definitions("rust", fixture.read_text()) is None
    fx = extract_rust_file(fixture.read_text(), fixture, TRUTH_ROOT, StubHelpers())
    names = {e.name for e in fx.entities}
    assert {"Counter", "build_registry", "fetch_remote"} <= names


def test_treesitter_path_is_taken_when_the_grammar_loads() -> None:
    """Act: with the grammar, a string-literal `fn` mints no row (the regex
    fallback's measured phantom) and `#[tokio::test]` code is skipped."""
    if not _ts_available("rust"):
        pytest.skip("codegraph-ts extra not installed")
    fixture = TRUTH_ROOT / "rust" / "edge_cases.rs"
    fx = extract_rust_file(fixture.read_text(), fixture, TRUTH_ROOT, StubHelpers())
    names = {e.name for e in fx.entities}
    assert "not_real" not in names and "main" not in names
    assert not names & {"setup", "runs_async", "runs_sync", "top_level_async_test", "Fixture"}
    assert {"production_only", "helper", "first"} <= names


def test_walker_failure_falls_back_for_that_file(monkeypatch, caplog) -> None:
    if not _ts_available("rust"):
        pytest.skip("codegraph-ts extra not installed")

    def boom(src, root):  # noqa: ANN001,ANN202
        raise RuntimeError("walker bug")

    monkeypatch.setitem(codegraph_defs._WALKERS, "rust", ("rust", boom))
    with caplog.at_level(logging.WARNING, logger="vco_lib.codegraph_defs"):
        assert codegraph_defs.extract_definitions("rust", "fn f() {}") is None
    assert any("walk failed" in r.getMessage() for r in caplog.records)


def test_note_path_logs_once_per_language_and_path(monkeypatch, caplog) -> None:
    monkeypatch.setattr(codegraph_defs, "_PATH_LOGGED", set())
    with caplog.at_level(logging.INFO, logger="vco_lib.codegraph_defs"):
        for _ in range(3):
            codegraph_defs.note_path("go", "tree-sitter")
        codegraph_defs.note_path("go", "regex")
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs.count("codegraph_defs: go definitions via tree-sitter") == 1
    assert msgs.count("codegraph_defs: go definitions via regex") == 1


# ---------------------------------------------------------------------------
# Rust test-gating predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attr", [
    "#[test]", "#[tokio::test]", "#[async_std::test]", "#[rstest]", "#[cfg(test)]",
    "#[cfg(any(test, debug_assertions))]", "#[cfg(all(test, unix))]",
    "#[cfg(all(unix, test))]", "#![cfg(test)]",
])
def test_rust_test_attributes_are_recognised(attr: str) -> None:
    assert codegraph_defs._rust_attr_is_test(attr)


@pytest.mark.parametrize("attr", [
    "#[cfg(not(test))]", "#[cfg(all(unix, not(test)))]", "#[cfg(feature = \"test\")]",
    "#[derive(Debug)]", "#[cfg(unix)]", "#[inline]", "#[testing]",
    # `any(...)` is test-only only when EVERY argument is test/debug_assertions
    # (the repo's own rule, launcher/src-tauri/tests/command_silent_gate.rs
    # `is_test_cfg_attr`); the real case is embed_admission.rs's
    # `parse_mem_available_mib`.
    "#[cfg(any(target_os = \"linux\", test))]", "#[cfg(any(test, feature = \"x\"))]",
    "#[cfg(debug_assertions)]", "#[cfg(any(debug_assertions))]",
])
def test_rust_production_attributes_are_not_test(attr: str) -> None:
    assert not codegraph_defs._rust_attr_is_test(attr)


# ---------------------------------------------------------------------------
# C# routes through the tree-sitter attribute path
# ---------------------------------------------------------------------------


def test_csharp_routes_from_treesitter_attributes() -> None:
    if not _ts_available("csharp"):
        pytest.skip("codegraph-ts extra not installed")
    fixture = TRUTH_ROOT / "csharp" / "EdgeController.cs"
    fx = extract_csharp_file(fixture.read_text(), fixture, TRUTH_ROOT, StubHelpers())
    apis = {
        (e.extras["method"], e.extras["endpoint"], e.extras.get("_handler_full_name"))
        for e in fx.entities if e.kind == "api"
    }
    # controller [Route("api/edge")] + action [HttpGet("one")], handler bound
    # to the action's own function row.
    assert apis == {("GET", "/api/edge/one", "Edge.EdgeController.GetOne")}, apis


def test_grammar_modules_resolvable_when_extra_installed() -> None:
    """With the extra installed every table entry loads (ABI-compatible)."""
    if importlib.util.find_spec("tree_sitter") is None:
        pytest.skip("codegraph-ts extra not installed")
    for lang in codegraph_grammars.GRAMMARS:
        assert codegraph_grammars.load_language(lang) is not None, lang


# ---------------------------------------------------------------------------
# Parse errors: a file tree-sitter cannot parse cleanly takes the regex path
# ---------------------------------------------------------------------------

_BROKEN_TS = (
    "export function a(): number { return 1; }\n"
    "\n"
    "export function b( { broken\n"
    "\n"
    "export function c(): number { return 3; }\n"
    "export class D {}\n"
    "export function e(): number { return 5; }\n"
)


def test_a_parse_error_routes_the_file_to_the_regex_path(monkeypatch, caplog) -> None:
    """HIGH-1 (review 2026-10-10): tree-sitter error recovery swallowed every
    symbol after a half-edited signature (only `a` survived). A tree with
    ``has_error`` now returns None, so the extractor uses its regex discovery
    for that file — logged once per file."""
    if not _ts_available("typescript"):
        pytest.skip("codegraph-ts extra not installed")
    monkeypatch.setattr(codegraph_defs, "_PARSE_ERROR_LOGGED", set())
    with caplog.at_level(logging.INFO, logger="vco_lib.codegraph_defs"):
        assert codegraph_defs.extract_definitions("typescript", _BROKEN_TS, origin="x.ts") is None
        assert codegraph_defs.extract_definitions("typescript", _BROKEN_TS, origin="x.ts") is None
    hits = [r for r in caplog.records if "parse error" in r.getMessage() and "x.ts" in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in caplog.records]


def test_a_clean_file_still_takes_the_treesitter_path() -> None:
    if not _ts_available("typescript"):
        pytest.skip("codegraph-ts extra not installed")
    clean = "export function a(): number { return 1; }\n"
    defs = codegraph_defs.extract_definitions("typescript", clean, origin="y.ts")
    assert defs is not None and [f.name for f in defs.functions] == ["a"]


def test_broken_file_keeps_every_intact_symbol_through_the_extractor(tmp_path) -> None:
    from vco_lib.codegraph_lang.javascript import extract_js_file

    p = tmp_path / "broken.ts"
    p.write_text(_BROKEN_TS)
    fx = extract_js_file(_BROKEN_TS, p, tmp_path, StubHelpers())
    names = {e.name for e in fx.entities}
    assert {"a", "c", "D", "e"} <= names


# ---------------------------------------------------------------------------
# Regex fallback — behavioural coverage (LOW-2): with no grammar, every
# extractor still finds the plain definitions of its golden-corpus file.
# ---------------------------------------------------------------------------

_GOLDEN_SRC = _REPO / "tests" / "fixtures" / "codegraph_golden" / "repo" / "src"


@pytest.mark.parametrize("module,func,fname,expected", [
    ("javascript", "extract_js_file", "models.ts", {"makeUser"}),
    ("javascript", "extract_js_file", "client.js", {"ApiClient", "buildClient", "doubler"}),
    ("rust", "extract_rust_file", "engine.rs", {"Counter", "Resettable", "build_registry", "fetch_remote"}),
    ("go", "extract_go_file", "service.go", {"Greeter", "Greet", "NewGreeter", "addNumbers"}),
    ("java", "extract_java_file", "Account.java", {"Account", "deposit", "Ledger", "balanceOf"}),
    ("csharp", "extract_csharp_file", "Inventory.cs", {"InventoryController", "Lookup", "Item"}),
    ("cpp", "extract_cpp_file", "geometry.cpp", {"Circle", "area", "distance", "Tally"}),
    ("ruby", "extract_ruby_file", "ledger.rb", {"Account", "deposit", "audit"}),
    ("lua", "extract_lua_file", "vector.lua", {"Vector", "clamp"}),
    ("shell", "extract_shell_file", "backup.sh", {"prepare_dir", "upload_archive", "run_backup"}),
    ("svelte", "extract_svelte_file", "Counter.svelte", {"createStore", "increment", "doubled"}),
])
def test_regex_fallback_extracts_the_plain_definitions(module, func, fname, expected, monkeypatch) -> None:
    import importlib

    force_regex_definitions(monkeypatch)
    extractor = getattr(importlib.import_module(f"vco_lib.codegraph_lang.{module}"), func)
    path = _GOLDEN_SRC / fname
    fx = extractor(path.read_text(), path, _GOLDEN_SRC, StubHelpers())
    names = {e.name for e in fx.entities}
    assert expected <= names, expected - names


def test_the_analyzer_docstring_states_the_shipped_parsers() -> None:
    """HIGH-2 (review 2026-10-10): the shipped analyzer's docstring listed every
    language as "(regex)" — a false promise once tree-sitter became the default."""
    text = (_REPO / "templates" / "scripts" / "analyze_code_graph.py").read_text(encoding="utf-8")
    doc = text.split('"""')[1]
    assert "tree-sitter" in doc
    for lang in ("Rust", "Go", "Java", "Ruby", "Lua", "Shell", "C++/C", "JavaScript/TypeScript/JSX"):
        assert f"{lang} (regex)" not in doc, lang
    assert "PowerShell, Proto (regex)" in doc
