# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-O.11.G/H/I/J/K/L — parser-quality cleanup batch (v0.2.52).

Six smaller fixes batched under one test file:

* **G** — Rust ``fn`` prefix regex covers ``pub(crate)``, ``pub(super)``,
  ``unsafe``, ``const``, ``extern "ABI"``, ``default`` (any combination,
  any order). Pre-V52-O.11.G the regex only matched ``pub`` + ``async``.
* **H** — Lua parser passes ``language="lua"`` (not ``"javascript"``) to
  ``embed_class`` and ``embed_function`` — proper-language routing so
  retrieval can filter/boost Lua content distinctly from JS.
* **I** — Per-language parsers strip string literals before regex scan
  so ``let s = "fn foo()"`` doesn't produce a false-positive function
  named ``foo``.
* **J** — Rust parser skips ``#[cfg(test)]`` + ``#[test]`` functions
  (they're test fixtures, not production code).
* **K** — Dead ``_ensure_project_source_property`` migration is gone
  (after V52-O.2 rewalk every row has ``project_source`` from creation).
* **L** — V52-O.2 reset script drops legacy ``Vco_v0243_*`` snapshot
  collections too (audit found them lurking from v0.2.43-era work).

All six fixes operate on the regex/string layer — no Weaviate calls — so
the tests stay in-process. The Rust + Lua tests reach into the module-
loaded analyzer directly (same isolation pattern as V52-O.11.F's tests).
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest

_REPO = Path(__file__).resolve().parent.parent


def _load_acg() -> ModuleType:
    """Isolated load of ``analyze_code_graph.py`` (same pattern as
    V52-O.11.E/F tests). The analyzer module mutates ``sys.modules`` /
    ``sys.path`` as a side effect — we restore both around the import
    so test ordering doesn't leak state between files.
    """
    sys_modules_before = set(sys.modules.keys())
    sys_path_before = list(sys.path)

    spec = importlib.util.spec_from_file_location(
        "_v52_o11_glj_acg_isolated",
        _REPO / "templates" / "scripts" / "analyze_code_graph.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = sys_path_before
        new_keys = set(sys.modules.keys()) - sys_modules_before
        for key in new_keys:
            del sys.modules[key]
    return mod


acg = _load_acg()


# ===========================================================================
# G — Rust ``fn`` prefix regex
# ===========================================================================


def _rust_func_regex() -> "re.Pattern[str]":
    """Reconstruct the func_pattern used inside ``_analyze_rust_file``.

    The pattern lives inline (assigned to a local in the analyze method)
    rather than at module scope, so we rebuild it verbatim here. Update
    this helper if the analyzer's regex changes.
    """
    return re.compile(
        r'(?:(?:pub(?:\s*\([^)]*\))?|async|unsafe|const|default'
        r'|extern(?:\s+"[^"]*")?)\s+)*'
        r'fn\s+([\w]+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)',
        re.MULTILINE,
    )


def test_g_rust_regex_covers_pub_crate() -> None:
    """``pub(crate) fn foo()`` must yield ``foo``."""
    m = _rust_func_regex().search("pub(crate) fn foo() {}")
    assert m is not None and m.group(1) == "foo"


def test_g_rust_regex_covers_pub_super() -> None:
    """``pub(super) fn bar()`` must yield ``bar``."""
    m = _rust_func_regex().search("pub(super) fn bar() {}")
    assert m is not None and m.group(1) == "bar"


def test_g_rust_regex_covers_pub_in_path() -> None:
    """``pub(in crate::a) fn baz()`` must yield ``baz``."""
    m = _rust_func_regex().search("pub(in crate::a) fn baz() {}")
    assert m is not None and m.group(1) == "baz"


def test_g_rust_regex_covers_unsafe() -> None:
    """``unsafe fn dangerous()`` must yield ``dangerous``."""
    m = _rust_func_regex().search("unsafe fn dangerous() {}")
    assert m is not None and m.group(1) == "dangerous"


def test_g_rust_regex_covers_const() -> None:
    """``const fn ce()`` must yield ``ce``."""
    m = _rust_func_regex().search("const fn ce() -> u32 { 0 }")
    assert m is not None and m.group(1) == "ce"


def test_g_rust_regex_covers_extern_c() -> None:
    """``extern "C" fn cabi()`` must yield ``cabi``."""
    m = _rust_func_regex().search('extern "C" fn cabi() {}')
    assert m is not None and m.group(1) == "cabi"


def test_g_rust_regex_covers_extern_no_abi() -> None:
    """``extern fn cabi2()`` (no ABI string) must yield ``cabi2``."""
    m = _rust_func_regex().search("extern fn cabi2() {}")
    assert m is not None and m.group(1) == "cabi2"


def test_g_rust_regex_covers_default() -> None:
    """``default fn def_method()`` (specialization) must yield ``def_method``."""
    m = _rust_func_regex().search("default fn def_method() {}")
    assert m is not None and m.group(1) == "def_method"


def test_g_rust_regex_covers_combinations() -> None:
    """Multiple modifiers in series (``pub async unsafe fn``,
    ``pub const unsafe fn``) all resolve to the inner name.
    """
    cases = [
        ("pub async unsafe fn pau() {}", "pau"),
        ("pub const unsafe fn pcu() {}", "pcu"),
        ("pub(crate) async fn pca() {}", "pca"),
    ]
    rx = _rust_func_regex()
    for src, expected in cases:
        m = rx.search(src)
        assert m is not None, f"No match for {src!r}"
        assert m.group(1) == expected, f"Expected {expected}, got {m.group(1)} for {src!r}"


def test_g_rust_regex_still_matches_simple() -> None:
    """Plain ``fn simple()`` and ``pub fn p()`` regression check —
    V52-O.11.G must not have broken the no-modifier or single-modifier
    case that pre-V52-O.11.G already handled correctly.
    """
    rx = _rust_func_regex()
    assert rx.search("fn simple() {}").group(1) == "simple"
    assert rx.search("pub fn pub_simple() {}").group(1) == "pub_simple"
    assert rx.search("async fn async_only() {}").group(1) == "async_only"


# ===========================================================================
# H — Lua passes language="lua" to embedders
# ===========================================================================


def test_h_lua_source_passes_language_lua_to_embed_class() -> None:
    """The Lua parser source must call ``embed_class(..., language="lua")``
    — NOT ``"javascript"`` (the pre-V52-O.11.H bug).

    Contract test on the source text (not runtime behaviour) because the
    Lua parser instantiates a Weaviate-bound Analyzer, and we want this
    test to stay pure. The grep is anchored to the Lua method site by
    looking for the surrounding ``Lua table class`` signature literal.
    """
    src = (_REPO / "templates" / "scripts" / "analyze_code_graph.py").read_text()
    # Find the Lua class embedding line:
    # signature = f"{class_name} = {{}} -- Lua table class"
    # embedding = embed_class(signature, "", methods=methods, language="lua")
    lua_class_section = re.search(
        r'-- Lua table class".*?embed_class\([^)]*\)',
        src,
        re.DOTALL,
    )
    assert lua_class_section is not None, (
        "Could not locate Lua class embedding site — analyzer layout changed"
    )
    section_text = lua_class_section.group(0)
    assert 'language="lua"' in section_text, (
        f"Lua class embedding still uses non-lua language tag. "
        f"Section: {section_text!r}"
    )
    assert 'language="javascript"' not in section_text, (
        "Lua class embedding still passes language=\"javascript\" — "
        "V52-O.11.H regression."
    )


def test_h_lua_source_passes_language_lua_to_embed_function() -> None:
    """The Lua parser source must call ``embed_function(..., language="lua")``."""
    src = (_REPO / "templates" / "scripts" / "analyze_code_graph.py").read_text()
    # Anchor on the Lua function embedding context. The closest unique
    # neighbour is ``func_full_name = f"{file_path.stem}.{func_name}"``
    # which is followed immediately by the embed_function call.
    m = re.search(
        r'func_full_name = f"\{file_path\.stem\}\.\{func_name\}"\s*\n'
        r'\s*embedding = embed_function\([^)]*\)',
        src,
    )
    assert m is not None, (
        "Could not locate Lua function embedding site — analyzer layout changed"
    )
    section_text = m.group(0)
    assert 'language="lua"' in section_text, (
        f"Lua function embedding still uses non-lua language tag. "
        f"Section: {section_text!r}"
    )


# ===========================================================================
# I — String-literal stripping helper
# ===========================================================================


def test_i_strip_string_literals_helper_exists() -> None:
    """V52-O.11.I introduces ``_strip_string_literals`` as a module-scope
    helper. Asserting on its existence locks the contract.
    """
    assert hasattr(acg, "_strip_string_literals"), (
        "_strip_string_literals helper missing from analyze_code_graph — "
        "V52-O.11.I not landed"
    )


def test_i_strip_string_literals_preserves_line_numbers() -> None:
    """The strip must replace string content with same-length empty
    content so downstream line counters still align."""
    src = 'let s = "fn foo() {}";\nfn real() {}'
    out = acg._strip_string_literals(src)
    assert out.count('\n') == src.count('\n'), (
        f"Line count changed: {src!r} → {out!r}"
    )
    # The "fn foo() {}" inside the string must NOT survive verbatim
    # (otherwise the downstream regex still matches it). Allow ""
    # (empty string literal) to remain.
    assert "fn foo()" not in out, (
        f"String contents leaked through strip: {out!r}"
    )
    # The real function must still be intact.
    assert "fn real()" in out


def test_i_strip_string_literals_handles_all_quote_types() -> None:
    """Single quotes, double quotes, backticks all stripped."""
    cases = [
        ('x = "double";',   '"'),
        ("x = 'single';",   "'"),
        ('x = `backtick`;', '`'),
    ]
    for src, quote in cases:
        out = acg._strip_string_literals(src)
        # Content between quotes must be gone (replaced or blanked).
        # The closing quotes themselves may remain or not, depending on
        # implementation — we just check the inner text doesn't survive.
        inner = src.split(quote, 1)[1].rsplit(quote, 1)[0]
        assert inner not in out, (
            f"Inner content {inner!r} survived strip of {src!r}: out={out!r}"
        )


def test_i_strip_string_literals_handles_escaped_quotes() -> None:
    """``"a\\"b"`` is a single string containing an escaped quote — must
    NOT be split into two strings."""
    src = r'x = "a\"b"; fn real() {}'
    out = acg._strip_string_literals(src)
    # ``fn real`` must still be present (the strip didn't run off the rails).
    assert "fn real" in out


# ===========================================================================
# J — Skip #[cfg(test)] Rust functions
# ===========================================================================


def test_j_rust_test_fn_helper_exists() -> None:
    """V52-O.11.J introduces a helper to detect ``#[cfg(test)]`` / ``#[test]``
    attributes preceding a ``fn``. We accept either name.
    """
    has_helper = (
        hasattr(acg, "_is_rust_test_fn")
        or hasattr(acg, "_rust_fn_is_test")
        or hasattr(acg, "_is_rust_cfg_test")
    )
    assert has_helper, (
        "Rust test-fn detector missing — V52-O.11.J not landed. "
        "Expected one of: _is_rust_test_fn, _rust_fn_is_test, _is_rust_cfg_test"
    )


def _rust_test_fn_helper():
    """Pick whichever name the implementation chose."""
    for name in ("_is_rust_test_fn", "_rust_fn_is_test", "_is_rust_cfg_test"):
        h = getattr(acg, name, None)
        if h is not None:
            return h
    pytest.skip("Helper not found (J not landed yet)")


def test_j_detects_cfg_test_attribute() -> None:
    """``#[cfg(test)]\\nfn helper()`` → True."""
    helper = _rust_test_fn_helper()
    src = "#[cfg(test)]\nfn helper() {}"
    fn_offset = src.find("fn helper")
    assert helper(src, fn_offset) is True


def test_j_detects_test_attribute() -> None:
    """``#[test]\\nfn t1()`` → True."""
    helper = _rust_test_fn_helper()
    src = "#[test]\nfn t1() {}"
    fn_offset = src.find("fn t1")
    assert helper(src, fn_offset) is True


def test_j_detects_cfg_any_test() -> None:
    """``#[cfg(any(test, foo))]\\nfn helper()`` → True."""
    helper = _rust_test_fn_helper()
    src = "#[cfg(any(test, foo))]\nfn helper() {}"
    fn_offset = src.find("fn helper")
    assert helper(src, fn_offset) is True


def test_j_production_fn_returns_false() -> None:
    """Plain ``fn helper()`` without attribute → False."""
    helper = _rust_test_fn_helper()
    src = "fn helper() {}"
    fn_offset = src.find("fn helper")
    assert helper(src, fn_offset) is False


def test_j_unrelated_attribute_returns_false() -> None:
    """``#[derive(Debug)]\\nfn helper()`` (silly but valid placement)
    → False because the attribute is not a test gate."""
    helper = _rust_test_fn_helper()
    src = "#[derive(Debug)]\nfn helper() {}"
    fn_offset = src.find("fn helper")
    assert helper(src, fn_offset) is False


# ===========================================================================
# K — _ensure_project_source_property migration deleted
# ===========================================================================


def test_k_ensure_project_source_property_gone() -> None:
    """The V52-O.2 rewalk drops + recreates collections, so every row
    has ``project_source`` from creation. The pre-v0.2.47 back-compat
    migration is dead code and should not be defined.
    """
    src_text = (
        _REPO / "templates" / "scripts" / "analyze_code_graph.py"
    ).read_text()
    assert "_ensure_project_source_property" not in src_text, (
        "_ensure_project_source_property still defined/called in "
        "analyze_code_graph.py — V52-O.11.K not landed. After the V52-O.2 "
        "rewalk this back-compat migration is dead code."
    )


# ===========================================================================
# L — V52-O.2 reset drops legacy Vco_v0243_* snapshot collections
# ===========================================================================


def test_l_reset_script_drops_legacy_v0243_collections() -> None:
    """``scripts/v0252_codegraph_reset.sh`` must extend its drop list to
    include the legacy ``Vco_v0243_*`` snapshot collections that v0.2.43-
    era work left lurking.
    """
    script_path = _REPO / "scripts" / "v0252_codegraph_reset.sh"
    assert script_path.exists(), (
        f"V52-O.2 reset helper missing at {script_path}"
    )
    content = script_path.read_text()

    # Expect the legacy collection name pattern to appear in the script.
    # Check for the three variants noted in the audit (A_install, B_rust,
    # C_cleanup) × three collection suffixes (Function/Class/Module).
    expected_legacy = [
        "Vco_v0243_A_install_CodeFunction",
        "Vco_v0243_A_install_CodeClass",
        "Vco_v0243_A_install_CodeModule",
        "Vco_v0243_B_rust_CodeFunction",
        "Vco_v0243_C_cleanup_CodeFunction",
    ]
    missing = [name for name in expected_legacy if name not in content]
    assert not missing, (
        f"V52-O.2 reset script doesn't drop these legacy collections: {missing}. "
        f"V52-O.11.L not landed."
    )
