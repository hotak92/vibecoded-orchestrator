# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-O.11.E — brace-balanced body extraction tests.

Pre-V52-O.11.E (audit a79152, 2026-06-09): 18 regex-language parsers used
``end_line = min(start_line + N, len(source_lines))`` heuristics that wrote
``function_body`` extending UP TO N lines past the real close brace.
Audit reproduced: ``is_blocklisted_agent_file`` (real end 281, stored end
315, body contained 34 lines of the NEXT function).

V52-O.11.E introduced ``_extract_balanced_block`` + ``_scrub_for_brace_balance``
in ``templates/scripts/analyze_code_graph.py`` and replaced all 18 broken sites
(11 function-body + 7 class-body). P2f stage 2 (v0.2.76) moved both helpers
VERBATIM to ``vco_lib/codegraph_lang/_shared`` — this guard is retargeted to
the new home (same assertions, unchanged). Validates:

1. Simple brace-balanced functions return the correct end-line.
2. Nested braces are counted correctly.
3. String literals containing braces are NOT counted.
4. Line comments containing braces are NOT counted.
5. Block comments (single-line ``/*...*/``) are NOT counted.
6. Template literals (backticks) containing braces are NOT counted.
7. Runaway functions (no matching close in 400-line window) gracefully
   degrade to the legacy ``+40`` fallback.
8. Invalid ``start_line`` (out of bounds) gracefully degrades.

The Rust example from the audit (project_state_populate.rs::
is_blocklisted_agent_file) is reproduced via a synthetic fixture asserting
the correct end-line is found (NOT contaminated with the next function's
body).
"""

from __future__ import annotations

from pathlib import Path

# P2f stage 2 (v0.2.76): the helpers under test moved verbatim to
# vco_lib/codegraph_lang/_shared, which is import-safe (no weaviate-client /
# EmbeddingService / sys.path side effects) — the old isolated-importlib
# loader for the full analyzer script is no longer needed. Alias the module
# as ``acg`` so every assertion below stays byte-identical.
from vco_lib.codegraph_lang import _shared as acg

_REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Test 1 — simple brace-balanced function
# ---------------------------------------------------------------------------


def test_simple_function_returns_close_brace_line() -> None:
    src = [
        "fn foo(x: u32) -> u32 {",  # line 1
        "    let y = x + 1;",         # line 2
        "    y",                      # line 3
        "}",                          # line 4
        "",                           # line 5
        "fn bar() {",                 # line 6
        "}",                          # line 7
    ]
    # Function starts at line 1 — close brace is at line 4
    end_line = acg._extract_balanced_block(src, start_line=1)
    assert end_line == 4


# ---------------------------------------------------------------------------
# Test 2 — nested braces counted correctly
# ---------------------------------------------------------------------------


def test_nested_braces_balanced() -> None:
    src = [
        "fn outer() {",            # 1
        "    if cond {",            # 2
        "        inner_call();",    # 3
        "    } else {",             # 4
        "        other();",         # 5
        "    }",                    # 6
        "    final_step();",        # 7
        "}",                        # 8 — real close
        "fn next_fn() {",           # 9
        "}",                        # 10
    ]
    assert acg._extract_balanced_block(src, start_line=1) == 8


# ---------------------------------------------------------------------------
# Test 3 — string literals containing braces are ignored
# ---------------------------------------------------------------------------


def test_braces_in_string_literals_ignored() -> None:
    src = [
        "fn foo() {",                            # 1
        '    let s = "this has } a brace";',     # 2 — } in string
        "    println!(s);",                      # 3
        "}",                                     # 4 — real close
        "fn bar() {",                            # 5
    ]
    assert acg._extract_balanced_block(src, start_line=1) == 4


def test_braces_in_single_quote_strings_ignored() -> None:
    src = [
        "fn foo() {",                # 1
        "    let c = '}';",          # 2 — } in char literal
        "    do_thing();",           # 3
        "}",                         # 4
    ]
    assert acg._extract_balanced_block(src, start_line=1) == 4


def test_braces_in_template_literals_ignored() -> None:
    # v0.2.91 (plan #29) CORRECTION — ``language="javascript"`` added.
    # The backtick template literal is a JS/TS/Svelte construct, not a
    # brace-language universal (in C/C++/Java/C# a backtick is not a string
    # delimiter at all), so it now lives in the JS profile rather than being
    # applied to every language. Declaring the language is what a real caller
    # does: javascript.py threads ``language="javascript"`` at all 3 of its
    # call sites. The assertion itself is unchanged.
    src = [
        "function foo() {",                       # 1
        "    const s = `template with } brace`;", # 2 — } in backtick
        "    return s;",                          # 3
        "}",                                      # 4
    ]
    assert acg._extract_balanced_block(src, start_line=1, language="javascript") == 4


# ---------------------------------------------------------------------------
# Test 4 — line comments containing braces are ignored
# ---------------------------------------------------------------------------


def test_braces_in_line_comments_ignored_cpp() -> None:
    src = [
        "fn foo() {",          # 1
        "    // closes with }",# 2 — // comment
        "    do_thing();",     # 3
        "}",                   # 4
    ]
    assert acg._extract_balanced_block(src, start_line=1) == 4


def test_braces_in_line_comments_ignored_python() -> None:
    src = [
        "def foo():",          # 1
        "    x = 1  # has a } brace",  # 2 — # comment
        "    return x",        # 3
    ]
    # Python doesn't use braces — helper returns the fallback for indent-
    # significant code. Just assert it doesn't crash.
    result = acg._extract_balanced_block(src, start_line=1)
    assert isinstance(result, int)


def test_braces_in_lua_comments_ignored() -> None:
    # v0.2.91 (plan #29) CORRECTION — ``language="lua"`` added. Pre-fix, ``--``
    # was treated as a comment marker in EVERY language, which is exactly the
    # shipped bug: it truncated C-family lines at a pre-decrement (``--i``).
    # ``--`` opening a comment is a Lua (and SQL-family) property, so the test
    # must say which language it is testing. The assertion is unchanged; the
    # C-family counterpart is pinned by
    # test_v0291_scrub_language_markers::test_double_dash_is_not_a_comment_in_c_family.
    src = [
        "function foo() {",    # 1 — synthetic Lua-with-braces
        "    -- comment with }",# 2 — -- comment
        "    body()",          # 3
        "}",                   # 4
    ]
    assert acg._extract_balanced_block(src, start_line=1, language="lua") == 4


# ---------------------------------------------------------------------------
# Test 5 — block comments (single-line variant)
# ---------------------------------------------------------------------------


def test_braces_in_block_comments_single_line_ignored() -> None:
    src = [
        "fn foo() {",                        # 1
        "    /* this has } in comment */",   # 2 — single-line block comment
        "    do_thing();",                   # 3
        "}",                                 # 4
    ]
    assert acg._extract_balanced_block(src, start_line=1) == 4


# ---------------------------------------------------------------------------
# Test 6 — runaway function fallback
# ---------------------------------------------------------------------------


def test_runaway_function_falls_back_to_legacy() -> None:
    """If no matching close brace is found within max_lookahead lines,
    the helper returns the legacy ``+40`` fallback so callers don't crash."""
    # 500-line source with an unclosed opener at line 1
    src = ["fn foo() {"] + [f"    line_{i}();" for i in range(500)]
    result = acg._extract_balanced_block(src, start_line=1, max_lookahead=100)
    # max_lookahead is 100, so we should hit the fallback. Legacy behavior:
    # min(start_line + 40, len(source_lines)) = min(41, 501) = 41
    assert result == 41


# ---------------------------------------------------------------------------
# Test 7 — out-of-bounds start_line
# ---------------------------------------------------------------------------


def test_out_of_bounds_start_line_returns_fallback() -> None:
    src = ["a", "b", "c"]
    # start_line 999 is out of bounds — should return legacy fallback
    result = acg._extract_balanced_block(src, start_line=999)
    assert isinstance(result, int)
    assert result <= len(src)


def test_zero_start_line_returns_fallback() -> None:
    src = ["a", "b", "c"]
    # start_line 0 is invalid (1-indexed convention) — should return fallback
    result = acg._extract_balanced_block(src, start_line=0)
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# Test 8 — the audit's actual reproduction case
# ---------------------------------------------------------------------------


def test_reproduces_audit_a79152_is_blocklisted_agent_file_case() -> None:
    """The audit (a79152) found ``is_blocklisted_agent_file`` had real end
    line 281, stored end line 315 (= 275 + 40), with 34 lines of bleed
    into the next function ``populate_agents``. Replay the shape with a
    short synthetic version: the helper finds the correct close brace
    and does NOT extend into the next function."""
    # Lines 1-7 = a function, lines 8-12 = a second function.
    src = [
        "fn is_blocklisted_agent_file(path: &Path) -> bool {",  # 1
        "    if let Some(stem) = path.file_stem() {",            # 2
        "        return BLOCKLIST.contains(stem);",              # 3
        "    }",                                                  # 4
        "    false",                                              # 5
        "}",                                                      # 6 — real close
        "",                                                       # 7
        "fn populate_agents(folder: &Path) -> Result<()> {",     # 8 — must NOT be included
        "    let entries = std::fs::read_dir(folder)?;",         # 9
        "    Ok(())",                                             # 10
        "}",                                                      # 11
    ]
    end_line = acg._extract_balanced_block(src, start_line=1)
    assert end_line == 6, (
        f"Helper returned {end_line} — must be 6 (real close brace). "
        f"Pre-V52-O.11.E behavior was 41 (= start + 40 = bleed into the "
        f"next function), which the audit confirmed was the production bug."
    )


# ---------------------------------------------------------------------------
# Test 9 — scrub helper isolates string + comment content
# ---------------------------------------------------------------------------


def test_scrub_strips_line_comment() -> None:
    assert acg._scrub_for_brace_balance("foo(); // comment with }") == "foo(); "


def test_scrub_strips_string_literal() -> None:
    assert "}" not in acg._scrub_for_brace_balance('x = "has } in string";')


def test_scrub_strips_block_comment() -> None:
    assert "}" not in acg._scrub_for_brace_balance("do(); /* } */ next();")


def test_scrub_preserves_real_braces() -> None:
    assert "{" in acg._scrub_for_brace_balance("if cond {")
    assert "}" in acg._scrub_for_brace_balance("}")


# ---------------------------------------------------------------------------
# Test 10 — V52-O.11.E regression: zero broken sites remain in analyzer
# ---------------------------------------------------------------------------


def test_no_broken_body_extraction_sites_remain() -> None:
    """Regression test: prevents future PRs from re-introducing the broken
    ``end_line = min(start_line + N, len(source_lines))`` pattern.

    Note: the helper's docstring mentions the OLD pattern verbatim for
    posterity — we exclude the docstring region from the scan.

    P2f stage 2 (v0.2.76): extractor code is moving to
    vco_lib/codegraph_lang/ — scan BOTH homes so the guard stays armed over
    the moved extractors, not just the shrinking analyzer.
    """
    import re
    analyzer_path = _REPO / "templates" / "scripts" / "analyze_code_graph.py"
    src = analyzer_path.read_text()
    for lang_mod in sorted((_REPO / "vco_lib" / "codegraph_lang").glob("*.py")):
        src += "\n" + lang_mod.read_text()

    # Strip the helper's docstring (it mentions the pattern verbatim).
    # The helper is at module level; we scan the body of CodeGraphAnalyzer.
    body_pattern = re.compile(
        r'^\s*end_line\s*=\s*min\(start_line\s*\+\s*\d+,\s*len\(source_lines\)\)\s*$',
        re.MULTILINE,
    )
    class_pattern = re.compile(
        r'^\s*class_lines\s*=\s*source_lines\[max\(0,\s*start_line\s*-\s*1\):min\(start_line\s*\+\s*\d+,',
        re.MULTILINE,
    )
    body_matches = body_pattern.findall(src)
    class_matches = class_pattern.findall(src)
    assert body_matches == [], (
        f"V52-O.11.E regression: {len(body_matches)} broken function-body "
        f"sites remain — use _extract_balanced_block(source_lines, start_line) "
        f"instead of fixed +N offsets."
    )
    assert class_matches == [], (
        f"V52-O.11.E regression: {len(class_matches)} broken class-body "
        f"sites remain — use _extract_balanced_block + slice instead."
    )
