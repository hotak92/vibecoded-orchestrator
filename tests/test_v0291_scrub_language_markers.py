# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 (plan decision #29) — the brace-balance scrubber's language markers.

THE SHIPPED BUG
---------------
``_scrub_for_brace_balance`` stripped each line from the EARLIEST of ``#`` /
``//`` / ``--`` — every marker applied to every language — and did so BEFORE
removing string literals. Four commonplace shapes lost a real brace:

    for (int i = n; i > 0; --i) {      → truncated at the C++ pre-decrement
    if (u == "http://x") { return; }   → truncated inside the URL string
    log("#tag"); if (c) {              → truncated inside the string
    x=${VAR#pre}; if [ -n "$x" ]; then → truncated inside the shell expansion

Each dropped brace makes ``_extract_balanced_block`` return a SHORT end-line, so
the stored ``function_body`` is a truncated fragment — degraded embeddings for
every non-Python extractor. The pre-fix docstring blamed "exotic multi-line
constructs", which lose no braces at all when they contain none.

REDNESS (how each assertion below was proved to fail pre-fix)
------------------------------------------------------------
Every assertion in "Group 1 — repro shapes" and "Group 2 — per-language
markers" was executed against the pre-fix ``_shared.py`` (loaded standalone from
a copy of the file at HEAD ``de2e530a``) before the fix landed. Recorded pre-fix
results:

    scrub('for (int i = n; i > 0; --i) {')       → 'for (int i = n; i > 0; '   ✗
    scrub('if (u == "http://x") { return; }')    → 'if (u == "http:'           ✗
    scrub('log("#tag"); if (c) {')               → 'log("'                     ✗
    scrub('x=${VAR#pre}; if [ -n "$x" ]; then')  → 'x=${VAR'                   ✗
    _extract_balanced_block(<pre-decrement fn>)  → 4  (correct: 5)             ✗

The cross-line group (Group 4) is red pre-fix for the same reason the KG node
``source-text-gates-fail-toward-green-2026-08-27`` describes: the pre-fix
scrubber had no state between lines, so a multi-line string / block comment fed
its braces straight to the counter.

RESIDUAL, found by the v0.2.91 wave-5 adversarial review (MAJOR-3): ``{`` and
``}`` were still in ``_WORD_START_BEFORE``, so ``#`` directly after a brace
opened a comment and ``${#VAR}`` — parameter LENGTH expansion — was mislexed.
Recorded results against that pre-fix ``_WORD_START_BEFORE``
(``frozenset(" \\t;&|(){}`")``), 7 of this file's assertions red:

    scrub('n=${#arr[@]}; if [ "$n" -gt 0 ]; then')  → 'n=${'            ✗
    scrub('len=${#VAR}; f() {')                     → 'len=${'          ✗
    scrub('if [ ${#} -gt 0 ]; then {')              → 'if [ ${'         ✗
    scrub('$n = ${#x}; function f {', powershell)   → '$n = ${'         ✗
    scrub('f() { g; }#notacomment')                 → 'f() { g; }'      ✗
    _extract_balanced_block(<${#items[@]} fn>)      → 7  (correct: 6)   ✗
    Group 3b, on the real shipped corpus                                ✗

LABEL THE AXIS for that last row, so the count is not challenged later: the
corpus is 16 `templates/**/*.sh` lines across 8 files that use `${#`; pre-fix,
6 of them across 5 files gained a phantom brace delta of +1
(`post-tool-security.sh:160`, `pre-bash-context-inject.sh:157`,
`vct_project_config.sh:532`, `vct_retrieval_tuning_set.sh:319`,
`vct_secrets_resolve.sh:490` and `:726`). The other 10 keep the expansion
INSIDE a double-quoted string, which the scrubber removes as a balanced whole,
so their delta was unchanged even pre-fix — the corpus is asserted whole rather
than pruned to the 6, so a future hook that drops the quotes is covered
automatically. Group 3b measures this on the real corpus rather than on
synthetic lines because the defect's whole significance was that the repo's own
hooks mis-extract in every install's code graph.

The AST-oracle gate (Group 5) is red pre-fix by construction — at HEAD none of
the 23 call sites passed ``language=``.

WHAT THIS FILE POLICES
----------------------
1. the four repro shapes, end to end;
2. per-language markers (``--`` is Lua's comment and C++'s pre-decrement; ``#``
   is shell's comment and a C string character);
3. genuine comments are STILL stripped, per language (leave-alone);
4. cross-line lexer state, plus the blast-radius bound that keeps a mis-lex
   confined to one line;
5. registry↔table parity and call-site threading, using CPython's own parser as
   the oracle rather than a source-text search that a comment could satisfy;
6. the same marker rules measured on the REAL shipped shell corpus (Group 3b),
   with an applicability self-check so the corpus cannot silently empty.

There is deliberately NO skip path in this module: an unreadable extractor or an
unparseable module is a FAILURE, not a graceful degradation.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Dict, List, Optional, Set

import pytest

from vco_lib.codegraph_lang import EXTRACTORS
from vco_lib.codegraph_lang import _shared
from vco_lib.codegraph_lang._shared import (
    _extract_balanced_block,
    _scrub_for_brace_balance,
)

_REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Group 1 — the repro shapes (red pre-fix; see REDNESS above)
# ---------------------------------------------------------------------------


def test_repro_pre_decrement_keeps_its_brace() -> None:
    """``--i`` is a C++ pre-decrement, not a comment. Pre-fix: brace lost."""
    line = "for (int i = n; i > 0; --i) {"
    assert "{" in _scrub_for_brace_balance(line, "cpp")


def test_repro_url_in_string_keeps_its_braces() -> None:
    """``//`` inside a URL string is not a comment. Pre-fix: both braces lost."""
    scrubbed = _scrub_for_brace_balance('if (u == "http://x") { return; }', "cpp")
    assert "{" in scrubbed and "}" in scrubbed


def test_repro_hash_in_string_keeps_its_brace() -> None:
    """``#`` inside a C string is not a comment. Pre-fix: brace lost."""
    assert "{" in _scrub_for_brace_balance('log("#tag"); if (c) {', "cpp")


def test_repro_shell_parameter_expansion_keeps_its_brace() -> None:
    """``${VAR#pre}`` is a shell parameter expansion, not a comment.

    Found while fixing #29 — a FOURTH instance of the same class, in the one
    language where ``#`` genuinely IS the comment marker. POSIX opens a comment
    only at the start of a word, so the expansion keeps its closing brace.
    """
    assert "}" in _scrub_for_brace_balance('x=${VAR#pre}; if [ -n "$x" ]; then', "shell")


@pytest.mark.parametrize(
    ("language", "line"),
    [
        ("shell", 'n=${#arr[@]}; if [ "$n" -gt 0 ]; then'),
        ("shell", "len=${#VAR}; f() {"),
        ("shell", "if [ ${#} -gt 0 ]; then {"),
        ("powershell", "$n = ${#x}; function f {"),
        ("powershell", "if (${a#b}) {"),
    ],
)
def test_repro_parameter_length_expansion_keeps_its_braces(
    language: str, line: str
) -> None:
    """``${#VAR}`` is parameter LENGTH expansion, not a comment.

    v0.2.91 wave-5 review MAJOR-3 — the #29 residual. ``{`` was in
    ``_WORD_START_BEFORE``, so a ``#`` directly after a brace opened a
    comment: ``n=${#arr[@]}; …`` scrubbed to ``n=${``, which truncates
    the line AND hands the counter an unmatched ``{``. Braces are shell
    RESERVED WORDS, not metacharacters, so a real brace-group opener is
    always followed by whitespace and loses nothing by the change.
    """
    scrubbed = _scrub_for_brace_balance(line, language)
    assert scrubbed.count("{") == line.count("{")
    assert scrubbed.count("}") == line.count("}")


def test_repro_close_brace_does_not_start_a_comment_either() -> None:
    """The ``}`` half of the same fix: bash reads ``}#…`` as one word."""
    line = "f() { g; }#notacomment"
    assert _scrub_for_brace_balance(line, "shell") == line


def test_repro_end_to_end_body_not_truncated_at_length_expansion() -> None:
    """The overrun this causes: pre-fix the unmatched ``${`` kept the
    depth counter above zero past the real close, returning 7."""
    src = [
        "count_items() {",                   # 1
        "  local n=${#items[@]}",            # 2 — pre-fix: became `local n=${`
        '  if [ "$n" -gt 0 ]; then',         # 3
        '    echo "$n"',                     # 4
        "  fi",                              # 5
        "}",                                 # 6 — the real close
        "after=1",                           # 7 — must NOT be included
    ]
    assert _extract_balanced_block(src, start_line=1, language="shell") == 6


def test_repro_end_to_end_body_not_truncated_at_pre_decrement() -> None:
    """The end-to-end shape from the #29 report: pre-fix this returned 4."""
    src = [
        "void f() {",                        # 1
        "for (int i = n; i > 0; --i) {",     # 2 — pre-fix: this brace was lost
        "    g();",                          # 3
        "}",                                 # 4
        "}",                                 # 5 — the real close
        "int after_f = 1;",                  # 6 — must NOT be included
    ]
    assert _extract_balanced_block(src, start_line=1, language="cpp") == 5


# ---------------------------------------------------------------------------
# Group 2 — per-language markers
# ---------------------------------------------------------------------------


def test_double_dash_is_not_a_comment_in_c_family() -> None:
    assert "}" in _scrub_for_brace_balance("i--; }", "cpp")
    assert "}" in _scrub_for_brace_balance("i--; }", "csharp")
    assert "}" in _scrub_for_brace_balance("i--; }", "java")


def test_double_dash_is_a_comment_in_lua() -> None:
    """The other half of the same axis — Lua must still lose the brace."""
    assert "}" not in _scrub_for_brace_balance("body() -- comment with }", "lua")


def test_hash_is_a_comment_in_shell_at_word_start() -> None:
    assert "}" not in _scrub_for_brace_balance("run() { # trailing }", "shell")


def test_hash_is_a_comment_anywhere_in_python_and_ruby() -> None:
    """Unlike shell, Python/Ruby open a comment at ``#`` regardless of what
    precedes it — the word-start rule is shell/PowerShell-only."""
    assert "}" not in _scrub_for_brace_balance("x=1#c }", "python")
    assert "}" not in _scrub_for_brace_balance("x=1#c }", "ruby")


def test_slash_slash_is_a_comment_in_c_family_but_not_in_shell() -> None:
    assert "}" not in _scrub_for_brace_balance("do(); // trailing }", "cpp")
    # In shell, `//` is path syntax / a substitution operator, never a comment.
    assert "}" in _scrub_for_brace_balance('p=${p//x/y}', "shell")


def test_char_literal_containing_a_brace_is_ignored() -> None:
    """``if (c == '{')`` — a brace inside a char literal must not be counted."""
    scrubbed = _scrub_for_brace_balance("if (c == '{') depth++;", "cpp")
    assert "{" not in scrubbed


# ---------------------------------------------------------------------------
# Group 3 — leave-alone: genuine comments are STILL stripped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "line"),
    [
        ("cpp", "do(); // a real comment with }"),
        ("csharp", "Do(); // a real comment with }"),
        ("go", "do() // a real comment with }"),
        ("java", "do(); // a real comment with }"),
        ("javascript", "do(); // a real comment with }"),
        ("typescript", "do(); // a real comment with }"),
        ("rust", "do(); // a real comment with }"),
        ("proto", "int32 f = 1; // a real comment with }"),
        ("svelte", "do(); // a real comment with }"),
        ("python", "do()  # a real comment with }"),
        ("ruby", "do  # a real comment with }"),
        ("shell", "do # a real comment with }"),
        ("powershell", "Do # a real comment with }"),
        ("lua", "do() -- a real comment with }"),
    ],
)
def test_genuine_line_comments_are_still_stripped(language: str, line: str) -> None:
    assert "}" not in _scrub_for_brace_balance(line, language)


def test_genuine_single_line_block_comment_is_still_stripped() -> None:
    assert "}" not in _scrub_for_brace_balance("do(); /* } */ next();", "cpp")


def test_real_braces_are_preserved() -> None:
    assert "{" in _scrub_for_brace_balance("if (cond) {", "cpp")
    assert "}" in _scrub_for_brace_balance("}", "cpp")


@pytest.mark.parametrize(
    "line",
    [
        "f() { # trailing }",         # a REAL brace group + a real comment
        "( #c }",                     # subshell opener is still a word boundary
        "do; #c }",
        "a |#c }",
        "a `#c }",
        "\t#c }",
        "# whole line }",
    ],
)
def test_word_boundaries_that_do_open_a_comment_still_do(line: str) -> None:
    """The other half of MAJOR-3's axis: dropping the braces from
    ``_WORD_START_BEFORE`` must not weaken any boundary that genuinely
    starts a shell comment — including ``{`` followed by a SPACE, which
    is the only way a real brace group can precede a comment."""
    assert "}" not in _scrub_for_brace_balance(line, "shell")


@pytest.mark.parametrize(
    "line",
    ["x=foo#bar }", "x=${VAR#pre} }", "p=${p//x/y} }"],
)
def test_mid_word_hash_is_still_not_a_comment(line: str) -> None:
    assert "}" in _scrub_for_brace_balance(line, "shell")


# ---------------------------------------------------------------------------
# Group 3b — the same axis, measured on the REAL shipped hooks
#
# Provenance: these cases are not hand-written. They are every line of the
# shipped `templates/` shell corpus that actually uses `${#…}`, read from
# disk at run time — the inputs the analyzer really meets on every install.
# MAJOR-3 was found on exactly these files, and a synthetic-only red-proof
# would not have shown that the repo's own hooks mis-extract.
# ---------------------------------------------------------------------------


def _shipped_shell_length_expansion_lines() -> List[tuple]:
    """(path-relative, lineno, text) for every shipped shell line using
    ``${#``. The selector is a plain substring scan — deliberately NOT the
    lexer under test, so the corpus cannot be emptied by the very defect
    it is meant to catch."""
    found: List[tuple] = []
    for root in ("templates/hooks", "templates/scripts"):
        for path in sorted((_REPO / root).rglob("*.sh")):
            for n, text in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if "${#" in text and not text.lstrip().startswith("#"):
                    found.append((path.relative_to(_REPO).as_posix(), n, text))
    return found


def test_the_shipped_corpus_actually_contains_the_shape() -> None:
    """Applicability self-check: if the shipped hooks ever stop using
    ``${#…}``, the gate below would pass vacuously. Fail loudly instead."""
    lines = _shipped_shell_length_expansion_lines()
    files = {rel for rel, _n, _t in lines}
    assert len(lines) >= 10, f"corpus collapsed to {len(lines)} line(s)"
    assert len(files) >= 4, f"corpus collapsed to {len(files)} file(s): {sorted(files)}"


def test_shipped_hooks_keep_their_brace_balance_through_the_scrubber() -> None:
    """Brace DELTA (``{`` minus ``}``) is what ``_extract_balanced_block``
    counts, so that is the invariant asserted — not the exact text, which
    legitimately changes when the scrubber removes a quoted string whose
    braces are balanced. Pre-fix, every unquoted ``${#…}`` line gained a
    phantom ``+1``, which is precisely how a body overruns its close."""
    offenders: List[str] = []
    for rel, lineno, text in _shipped_shell_length_expansion_lines():
        scrubbed = _scrub_for_brace_balance(text, "shell")
        before = text.count("{") - text.count("}")
        after = scrubbed.count("{") - scrubbed.count("}")
        if before != after:
            offenders.append(f"{rel}:{lineno}: {before:+d} -> {after:+d}  {text.strip()}")
    assert offenders == [], "brace delta changed on shipped lines:\n" + "\n".join(
        offenders
    )


# ---------------------------------------------------------------------------
# Group 4 — cross-line lexer state, and the blast-radius bound
# ---------------------------------------------------------------------------


def test_multiline_block_comment_does_not_leak_braces() -> None:
    src = [
        "void f() {",          # 1
        "/* multi-line",       # 2
        " * with a } brace",   # 3 — pre-fix: counted, closing the block early
        " */",                 # 4
        "    g();",            # 5
        "}",                   # 6 — the real close
        "int x;",              # 7
    ]
    assert _extract_balanced_block(src, start_line=1, language="cpp") == 6


def test_rust_block_comments_nest() -> None:
    src = [
        "fn f() {",                                  # 1
        "/* outer /* inner */ still comment } */",   # 2
        "    g();",                                  # 3
        "}",                                         # 4
        "fn n() {}",                                 # 5
    ]
    assert _extract_balanced_block(src, start_line=1, language="rust") == 4


def test_js_template_literal_spans_lines() -> None:
    src = [
        "function f() {",           # 1
        "  const t = `",            # 2
        "    unbalanced } here",    # 3
        "  `;",                     # 4
        "  g();",                   # 5
        "}",                        # 6
        "function n() {}",          # 7
    ]
    assert _extract_balanced_block(src, start_line=1, language="javascript") == 6


def test_rust_raw_string_spans_lines() -> None:
    src = [
        "fn f() {",                     # 1
        '    let s = r#"',              # 2
        "        unbalanced } brace",   # 3
        '    "#;',                      # 4
        "    g();",                     # 5
        "}",                            # 6
        "fn n() {}",                    # 7
    ]
    assert _extract_balanced_block(src, start_line=1, language="rust") == 6


def test_csharp_verbatim_string_spans_lines() -> None:
    src = [
        "void F() {",                   # 1
        '    var s = @"',               # 2
        "        unbalanced } brace",   # 3
        '    ";',                       # 4
        "    G();",                     # 5
        "}",                            # 6
        "int x;",                       # 7
    ]
    assert _extract_balanced_block(src, start_line=1, language="csharp") == 6


def test_lua_long_comment_spans_lines() -> None:
    src = [
        "function f() {",       # 1 — synthetic Lua-with-braces
        "--[[",                 # 2
        "  unbalanced } brace", # 3
        "]]",                   # 4
        "  g()",                # 5
        "}",                    # 6
        "x = 1",                # 7
    ]
    assert _extract_balanced_block(src, start_line=1, language="lua") == 6


def test_unterminated_single_line_string_does_not_leak_to_the_next_line() -> None:
    """THE BLAST-RADIUS BOUND.

    Cross-line state is the fix for line-spanning constructs, but it is also the
    thing that turned a lexer desync into 19 false positives elsewhere this wave.
    So only constructs explicitly marked line-spanning carry over: an ordinary
    ``"`` string left open at end-of-line RESETS, keeping any mis-lex confined to
    the line that caused it — the same bound the pre-fix scrubber had.
    """
    src = [
        "void f() {",          # 1
        '    bad("oops;',      # 2 — an unterminated ordinary string
        "    g();",            # 3 — must still be scanned as code
        "}",                   # 4 — must still be seen
        "int x;",              # 5
    ]
    assert _extract_balanced_block(src, start_line=1, language="cpp") == 4


def test_rust_lifetime_is_not_read_as_an_unterminated_string() -> None:
    """``'a`` is a lifetime, not a quote. Reading it as a string opener would
    swallow the rest of the function — the catastrophic direction of this fix."""
    src = [
        "fn foo<'a>(x: &'a str) {",  # 1
        "    g(x);",                 # 2
        "}",                         # 3
        "fn n() {}",                 # 4
    ]
    assert _extract_balanced_block(src, start_line=1, language="rust") == 3
    assert "{" in _scrub_for_brace_balance("fn foo<'a>(x: &'a str) {", "rust")


def test_apostrophe_inside_a_comment_does_not_open_a_string() -> None:
    """Why the fix is ONE left-to-right pass and not "strings first".

    Stripping strings before comments makes the apostrophe in ``// don't`` open a
    string; stripping comments before strings is the shipped bug. A single pass
    that tracks which construct it is inside makes the ordering question moot.
    """
    src = [
        "void f() {",             # 1
        "    // don't do this",   # 2 — the apostrophe must stay inert
        "    g();",               # 3
        "}",                      # 4
        "int x;",                 # 5
    ]
    assert _extract_balanced_block(src, start_line=1, language="cpp") == 4


def test_rust_string_line_continuation_is_followed() -> None:
    """The exact shape that desynced the Rust scanner elsewhere this wave: a
    ``"…text \\`` continuation, where a naive lexer reads the NEXT line's closing
    quote as an opening one."""
    src = [
        "fn f() {",                      # 1
        '    let s = "text \\',           # 2 — continues onto line 3
        '        more } text";',         # 3
        "    g();",                      # 4
        "}",                             # 5
        "fn n() {}",                     # 6
    ]
    assert _extract_balanced_block(src, start_line=1, language="rust") == 5


def test_go_raw_string_spans_lines() -> None:
    src = [
        "func f() {",   # 1
        "  s := `",     # 2
        "   } raw",     # 3
        "  `",          # 4
        "  g()",        # 5
        "}",            # 6
        "var x = 1",    # 7
    ]
    assert _extract_balanced_block(src, start_line=1, language="go") == 6


def test_cpp_raw_string_literal_spans_lines() -> None:
    src = [
        "void f() {",         # 1
        '  auto s = R"json(', # 2
        "    } raw",          # 3
        '  )json";',          # 4
        "  g();",             # 5
        "}",                  # 6
        "int x;",             # 7
    ]
    assert _extract_balanced_block(src, start_line=1, language="cpp") == 6


def test_powershell_here_string_and_block_comment_span_lines() -> None:
    here = ["function f {", '  $s = @"', "    } unbalanced", '"@', "  g", "}", "x"]
    assert _extract_balanced_block(here, start_line=1, language="powershell") == 6
    block = ["function f {", "<#", "  } doc", "#>", "  g", "}", "x"]
    assert _extract_balanced_block(block, start_line=1, language="powershell") == 6


def test_svelte_markup_comment_is_a_block_comment() -> None:
    src = ["function f() {", "<!-- } -->", "  g();", "}", "x"]
    assert _extract_balanced_block(src, start_line=1, language="svelte") == 4


def test_ruby_block_comment_anchors_to_column_zero() -> None:
    """``=begin`` opens a comment only at column 0. Matching it mid-line would
    let ``{x=begin_val}`` swallow the rest of the file."""
    src = ["def f() {", "=begin", "  } inside", "=end", "  g", "}", "x=1"]
    assert _extract_balanced_block(src, start_line=1, language="ruby") == 6
    assert "}" in _scrub_for_brace_balance("h = {x=begin_val}", "ruby")


def test_escaped_quote_does_not_close_its_string() -> None:
    assert "{" in _scrub_for_brace_balance(r'let s = "he said \"hi\""; if x {', "rust")


def test_cpp_digit_separator_is_not_a_string_opener() -> None:
    """``1'000'000`` (C++14) must stay ordinary text, like a Rust lifetime."""
    assert "{" in _scrub_for_brace_balance("if (n == 1'000'000) {", "cpp")


def test_single_line_entry_point_never_leaks_state() -> None:
    """``_scrub_for_brace_balance`` is stateless by contract: an unterminated
    line-spanning opener yields a plain string, never a leaked lexer state."""
    assert isinstance(_scrub_for_brace_balance('let s = r#"unterminated', "rust"), str)


# ---------------------------------------------------------------------------
# Group 5 — registry parity + call-site threading, with CPython as the oracle
# ---------------------------------------------------------------------------
#
# A source-TEXT search for ``_extract_balanced_block(`` is satisfiable by a
# COMMENT or a STRING naming the call — the locator-shadowing failure the KG node
# ``source-text-gates-fail-toward-green-2026-08-27`` records twice in this wave.
# ``ast.parse`` cannot be fooled that way: a comment is discarded by the tokenizer
# and a string is a Constant, never a Call. The meta-test below proves both that
# the naive locator IS fooled and that this one is not.


def _balanced_block_calls(source: str) -> List[ast.Call]:
    """Every ``_extract_balanced_block(...)`` CALL in ``source`` (never a mention
    of one in a comment or a string)."""
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_extract_balanced_block"
    ]


def _call_language(call: ast.Call) -> Optional[str]:
    """The literal ``language=`` argument of a call, or None when absent /
    not a plain string constant."""
    for kw in call.keywords:
        if kw.arg == "language":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
            return None
    return None


def _extractor_modules() -> Dict[str, Set[str]]:
    """module name -> the dispatch keys it serves.

    The denominator is ``EXTRACTORS`` itself — the registry the analyzer really
    dispatches through — not a hand-written list that could drift from it.
    """
    modules: Dict[str, Set[str]] = {}
    for key, fn in EXTRACTORS.items():
        modules.setdefault(fn.__module__, set()).add(key)
    return modules


def _module_source(module_name: str) -> str:
    mod = importlib.import_module(module_name)
    assert mod.__file__ is not None, f"{module_name} has no __file__"
    return Path(mod.__file__).read_text(encoding="utf-8")


def test_every_extractor_key_has_an_explicit_syntax_entry() -> None:
    """Every dispatch key the analyzer can route to must declare its markers.

    Without this, a new language silently inherits the C-family fallback — the
    exact "one marker set for every language" defect #29 fixed.
    """
    missing = sorted(k for k in EXTRACTORS if k not in _shared._LANG_SYNTAX)
    assert missing == [], (
        f"{len(missing)} EXTRACTORS key(s) have no _LANG_SYNTAX row: {missing}. "
        f"Add the language's comment/string markers to the table in _shared.py."
    )


def test_only_the_python_extractor_bypasses_the_balanced_block_helper() -> None:
    """Settles the extractor count on an explicit axis.

    14 dispatch KEYS -> 13 extractor MODULES (javascript.py serves both the
    ``javascript`` and ``typescript`` keys). Python builds bodies from the AST and
    never calls the helper, so the blast radius of #29 is the other 12 MODULES
    (= 13 non-Python KEYS). Asserted positively in both directions, so a module
    that quietly stops calling the helper fails here rather than being skipped.
    """
    modules = _extractor_modules()
    assert len(EXTRACTORS) == 14, f"dispatch keys changed: {sorted(EXTRACTORS)}"
    assert len(modules) == 13, f"extractor modules changed: {sorted(modules)}"

    with_calls = {m for m in modules if _balanced_block_calls(_module_source(m))}
    without_calls = set(modules) - with_calls
    assert without_calls == {"vco_lib.codegraph_lang.python"}, (
        f"expected python.py to be the ONLY extractor not using "
        f"_extract_balanced_block; modules without a call: {sorted(without_calls)}"
    )
    assert len(with_calls) == 12


def test_every_call_site_threads_a_language_valid_for_its_module() -> None:
    """Each call must pass a ``language=`` literal that (a) exists in the marker
    table and (b) is one of the dispatch keys its own module serves."""
    problems: List[str] = []
    total = 0
    for module_name, keys in sorted(_extractor_modules().items()):
        for call in _balanced_block_calls(_module_source(module_name)):
            total += 1
            lang = _call_language(call)
            where = f"{module_name}:{call.lineno}"
            if lang is None:
                problems.append(f"{where}: no literal language= argument")
            elif lang not in _shared._LANG_SYNTAX:
                problems.append(f"{where}: language={lang!r} has no _LANG_SYNTAX row")
            elif lang not in keys:
                problems.append(
                    f"{where}: language={lang!r} is not served by this module "
                    f"(expected one of {sorted(keys)})"
                )
    assert problems == [], "\n".join(problems)
    # Self-check: the gate must still SEE the call sites it polices. A refactor
    # that renames or wraps the helper would otherwise make this pass vacuously.
    assert total >= 12, f"only {total} call sites found — the locator went blind"


def test_ast_locator_is_not_satisfiable_by_a_mention_of_the_call() -> None:
    """META-TEST: first prove the naive locator IS fooled, then that ours is not.

    Per the KG node, a red-proof that does not first exercise the hazard is not a
    red-proof. A comment and a string both name the call here; only one real call
    exists, and it has no ``language=``.
    """
    shadowed = (
        "# _extract_balanced_block(source_lines, start_line, language='rust')\n"
        "DOC = \"_extract_balanced_block(source_lines, start_line, language='rust')\"\n"
        "def f(source_lines, start_line):\n"
        "    return _extract_balanced_block(source_lines, start_line)\n"
    )
    # The naive source-text locator counts three "call sites" and would report
    # two of them as correctly threaded — the false-negative direction.
    assert shadowed.count("_extract_balanced_block(") == 3
    assert shadowed.count("_extract_balanced_block(source_lines, start_line, language=") == 2

    calls = _balanced_block_calls(shadowed)
    assert len(calls) == 1, "the AST locator matched a comment or a string"
    assert _call_language(calls[0]) is None


def test_ast_locator_sees_real_call_sites_in_the_real_tree() -> None:
    """The same self-check against the REAL tree, not a synthetic fixture — the
    failure mode the KG node records is a gate that passes on synthetic input
    while going blind on the shipped file."""
    calls = _balanced_block_calls(_module_source("vco_lib.codegraph_lang.rust"))
    assert len(calls) >= 1
    assert all(_call_language(c) == "rust" for c in calls)
