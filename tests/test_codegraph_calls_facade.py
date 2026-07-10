# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for the cross-language call-extraction facade
(``vco_lib/codegraph_calls.py``, CG-2 / v0.2.77 Part 5).

Design contract under test
--------------------------
* Python call extraction is dependency-free (``ast``) and ALWAYS available —
  its tests never skip.
* Every other language uses a tree-sitter grammar from the OPTIONAL
  ``codegraph-ts`` extra. Those tests carry a ``skipif`` on the grammar's
  importability, so this whole module is GREEN in a venv WITHOUT the extra
  (the CI baseline) and EXERCISED in a venv WITH it.
* ImportError fallback: absent grammar → ``extract_call_names`` returns
  ``None`` (act = present → names; leave-alone = absent → None) and the Python
  path is untouched either way.
"""
from __future__ import annotations

import importlib.util

import pytest

from vco_lib import codegraph_calls as cc


def _grammar_installed(module_name: str) -> bool:
    """True when the given tree-sitter grammar wheel is importable AND the
    tree-sitter core is present (both are needed to build a parser)."""
    if importlib.util.find_spec("tree_sitter") is None:
        return False
    return importlib.util.find_spec(module_name) is not None


# ---------------------------------------------------------------------------
# Python — dependency-free, never skips.
# ---------------------------------------------------------------------------


def test_python_extracts_names_ordered_and_deduped() -> None:
    body = (
        "def f():\n"
        "    foo()\n"
        "    self.bar()\n"
        "    foo()  # duplicate, dropped\n"
        "    obj.baz()\n"
    )
    assert cc.extract_call_names("python", body) == ["foo", "bar", "baz"]


def test_python_strips_builtins() -> None:
    body = "def f():\n    print(len(x))\n    real_call()\n"
    # print + len are builtins → stripped; only real_call survives.
    assert cc.extract_call_names("python", body) == ["real_call"]


def test_python_syntax_error_returns_none() -> None:
    # A non-parseable body → None (caller keeps today's skip behaviour). This is
    # the SAME signal the old analyzer's ``except SyntaxError: continue`` gave.
    assert cc.extract_call_names("python", "def f( : broken") is None


def test_python_no_calls_returns_empty_list() -> None:
    # Parsed fine, zero calls → [] (a POSITIVE result, distinct from None).
    assert cc.extract_call_names("python", "def f():\n    x = 1\n") == []


def test_python_always_supported() -> None:
    assert "python" in cc.supported_call_languages()


# ---------------------------------------------------------------------------
# Cross-language — one act-test per grammar, skipped when the extra is absent.
# ---------------------------------------------------------------------------

# (canonical language id, grammar module, source body, expected-subset names).
_GRAMMAR_CASES = [
    ("rust", "tree_sitter_rust",
     'fn m(){ foo(); a::b(); obj.meth(); println!("x"); }',
     {"foo", "b", "meth", "println"}),
    ("go", "tree_sitter_go",
     "func m(){ foo(); pkg.Bar(); obj.M(); }",
     {"foo", "Bar", "M"}),
    ("javascript", "tree_sitter_javascript",
     "function f(){ foo(); obj.bar(); a.b.c(); }",
     {"foo", "bar", "c"}),
    ("typescript", "tree_sitter_typescript",
     "function f(){ foo(); obj.bar(); }",
     {"foo", "bar"}),
    ("java", "tree_sitter_java",
     "class C{ void m(){ foo(); this.bar(); obj.method(); }}",
     {"foo", "bar", "method"}),
    ("csharp", "tree_sitter_c_sharp",
     "class C{ void M(){ Foo(); this.Bar(); obj.Method(); }}",
     {"Foo", "Bar", "Method"}),
    ("cpp", "tree_sitter_cpp",
     "void f(){ foo(); obj.bar(); ns::baz(); ptr->qux(); }",
     {"foo", "bar", "baz", "qux"}),
    ("ruby", "tree_sitter_ruby",
     "def m; bar(); obj.baz(); end",
     {"bar", "baz"}),
    ("lua", "tree_sitter_lua",
     "function f() foo(); obj.bar(); obj:baz(); end",
     {"foo", "bar", "baz"}),
    ("shell", "tree_sitter_bash",
     "f(){ foo; bar arg; baz; }",
     {"foo", "bar", "baz"}),
]


@pytest.mark.parametrize("lang, module, body, expected", _GRAMMAR_CASES)
def test_grammar_extracts_calls(lang: str, module: str, body: str, expected: set) -> None:
    if not _grammar_installed(module):
        pytest.skip(f"codegraph-ts extra not installed ({module} absent)")
    got = cc.extract_call_names(lang, body)
    assert got is not None, f"{lang}: grammar installed but returned None"
    missing = expected - set(got)
    assert not missing, f"{lang}: missing expected calls {missing} (got {got})"


@pytest.mark.parametrize("lang, module, body, expected", _GRAMMAR_CASES)
def test_grammar_language_reported_supported(
    lang: str, module: str, body: str, expected: set,
) -> None:
    if not _grammar_installed(module):
        pytest.skip(f"codegraph-ts extra not installed ({module} absent)")
    assert lang in cc.supported_call_languages()


def test_grammar_empty_body_is_positive_empty() -> None:
    # For a supported language, an empty body is a positive [] (not None).
    if not _grammar_installed("tree_sitter_rust"):
        pytest.skip("codegraph-ts extra not installed (tree_sitter_rust absent)")
    assert cc.extract_call_names("rust", "") == []


# ---------------------------------------------------------------------------
# ImportError fallback — act + leave-alone.
# ---------------------------------------------------------------------------


def test_absent_grammar_returns_none_leave_alone() -> None:
    """Leave-alone case: when the grammar is NOT installed, a non-Python
    language returns None (caller keeps today's no-edge behaviour). When it IS
    installed this is exercised by the act-tests above, so here we only assert
    the None-fallback when the extra is genuinely absent."""
    if _grammar_installed("tree_sitter_rust"):
        pytest.skip("extra installed — the act path is covered elsewhere")
    assert cc.extract_call_names("rust", "fn m(){ foo(); }") is None
    # Python must STILL work in the same process (the fallback is per-language).
    assert cc.extract_call_names("python", "def f():\n    foo()\n") == ["foo"]


def test_absent_grammar_language_not_reported_supported() -> None:
    if _grammar_installed("tree_sitter_rust"):
        pytest.skip("extra installed — rust is legitimately supported")
    assert "rust" not in cc.supported_call_languages()


# ---------------------------------------------------------------------------
# Out-of-scope / edge inputs — never depend on the extra.
# ---------------------------------------------------------------------------


def test_unknown_language_returns_none() -> None:
    assert cc.extract_call_names("brainfuck", "+++.") is None


def test_empty_language_returns_none() -> None:
    assert cc.extract_call_names("", "whatever") is None


def test_ruled_out_languages_return_none() -> None:
    # proto + powershell are excluded by ruling (no grammar wired) → None even
    # WITH the extra installed. svelte is never passed here (the caller extracts
    # the <script> block and passes it as javascript).
    assert cc.extract_call_names("proto", "message M {}") is None
    assert cc.extract_call_names("powershell", "function f { Get-Item }") is None
    assert cc.extract_call_names("svelte", "<script>foo()</script>") is None
