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
from tests._codegraph_truth_harness import TRUTH_ROOT, StubHelpers

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
    "#[test]", "#[tokio::test]", "#[async_std::test]", "#[cfg(test)]",
    "#[cfg(any(test, feature = \"x\"))]", "#[cfg(all(test, unix))]", "#![cfg(test)]",
])
def test_rust_test_attributes_are_recognised(attr: str) -> None:
    assert codegraph_defs._rust_attr_is_test(attr)


@pytest.mark.parametrize("attr", [
    "#[cfg(not(test))]", "#[cfg(all(unix, not(test)))]", "#[cfg(feature = \"test\")]",
    "#[derive(Debug)]", "#[cfg(unix)]", "#[inline]", "#[testing]",
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
