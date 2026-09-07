# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-5b — the shared ``end``-keyword block scanner.

WHY THIS EXISTS
---------------
``_extract_balanced_block`` counts BRACES. Ruby and Lua close their blocks with
the WORD ``end``, and idiomatic Ruby puts no braces around a class or a method
at all — so every Ruby class, every Ruby method and every Lua ``function … end``
took the helper's no-opener runaway branch and stored a body running from its
declaration to the end of the file. Measured on the shipped golden corpus
before this change: all three ``ledger.rb`` classes AND all six of its methods
ended at line 40 of a 40-line file, and ``vector.lua``'s ``clamp`` ended at 39
for a function that closes on 38.

THE PART THAT IS NOT AN OFF-BY-ONE, and the reason WP-5 declined this as a
CAPABILITY rather than half-building it: Ruby spells its statement modifier
with the same keyword as its block form. ``value = 1 if flag`` takes no ``end``.
A scanner that counts that ``if`` as an opener does not merely mis-measure — it
runs the body on to the next unmatched ``end``, which is strictly worse than
the bug being fixed. So the scanner decides, per occurrence, whether the
keyword is in expression-START position, and this file is mostly about that
decision.

WHAT IS PINNED
--------------
* the ACT: a matching ``end`` is found for every block form of both languages,
  including the ones nested inside it;
* the MODIFIER discrimination, in both directions — ``if`` after an expression
  opens nothing, ``if`` after ``=`` opens a block;
* the two forms that open NOTHING and must yield a one-line body rather than
  the rest of the file: Ruby 3.0's endless method, and a construct already
  closed on its own line;
* the LEAVE-ALONE: ``def value=(v)`` and ``def ==(other)`` are ORDINARY
  methods whose ``=`` belongs to the name, not endless methods; a ``do`` that
  terminates a ``while``/``for`` header is not a second opener; ``repeat …
  until`` is not counted at all; ``range.end`` / ``:end`` / ``end:`` are not
  closers;
* tri-OS (R12/R14): the only OS-dependent input to a pure text helper is the
  line terminator, so LF and CRLF are asserted to give identical answers;
* the registry parity that keeps a future language from silently inheriting
  "this declaration opens nothing".
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
    _END_BLOCK_PROFILES,
    _at_expression_start,
    _is_endless_def,
    extract_end_keyword_block,
)


def _end(source: str, start_line: int = 1, **kw) -> int:
    return extract_end_keyword_block(source.split("\n"), start_line, **kw)


# ═══════════════════════════════════════════════════════════════════════════
# THE ACT — a block ends where its `end` is
# ═══════════════════════════════════════════════════════════════════════════
_RUBY_BLOCKS = [
    pytest.param("def a(x)\n  x\nend\n", 3, id="def"),
    pytest.param("class Foo\n  def a\n  end\nend\n", 4, id="class-with-nested-def"),
    pytest.param("module M\n  X = 1\nend\n", 3, id="module"),
    pytest.param("def a(x)\n  if x\n    1\n  end\nend\n", 5, id="block-if"),
    pytest.param("def a(x)\n  case x\n  when 1 then 2\n  else 3\n  end\nend\n", 6, id="case"),
    pytest.param("def a\n  begin\n    y\n  rescue\n  end\nend\n", 6, id="begin-rescue"),
    pytest.param("def a\n  [1].each do |x|\n    x\n  end\nend\n", 5, id="do-block"),
    pytest.param("def a(x)\n  for i in x do\n    i\n  end\nend\n", 5, id="for-in-do"),
    pytest.param("def a(x)\n  while x do\n    x -= 1\n  end\nend\n", 5, id="while-do"),
    pytest.param("def a(x)\n  until x\n    x += 1\n  end\nend\n", 5, id="until"),
]


@pytest.mark.parametrize("source,expected", _RUBY_BLOCKS)
def test_a_ruby_block_ends_at_its_matching_end(source: str, expected: int) -> None:
    assert _end(source, language="ruby") == expected


_LUA_BLOCKS = [
    pytest.param("function f()\n  return 1\nend\n", 3, id="function"),
    pytest.param("local function f()\n  return 1\nend\n", 3, id="local-function"),
    pytest.param("function f()\n  if a then\n    for i=1,2 do\n    end\n  end\nend\n", 6, id="nested"),
    pytest.param("function f()\n  while x do\n  end\nend\n", 4, id="while-do"),
    pytest.param("function f()\n  local g = function() return 1 end\n  return g\nend\n", 4, id="anonymous-fn"),
    pytest.param("function f()\n  repeat\n    x = 1\n  until x\nend\n", 5, id="repeat-until-is-not-counted"),
]


@pytest.mark.parametrize("source,expected", _LUA_BLOCKS)
def test_a_lua_block_ends_at_its_matching_end(source: str, expected: int) -> None:
    assert _end(source, language="lua") == expected


def test_the_golden_ruby_fixture_lines_are_what_the_source_says() -> None:
    """End to end on the shipped corpus, checked against the file rather than
    against a snapshot — the discriminator that found the defect."""
    src = (
        Path(__file__).parent
        / "fixtures" / "codegraph_golden" / "repo" / "src" / "ledger.rb"
    ).read_text(encoding="utf-8")
    lines = src.split("\n")
    for start, expected in ((8, 12), (14, 26), (29, 33), (35, 39), (43, 53), (55, 64), (59, 63)):
        got = extract_end_keyword_block(lines, start, language="ruby", max_lookahead=2000)
        assert got == expected, (
            f"ledger.rb:{start} ({lines[start - 1]!r}) ends at {got}, "
            f"expected {expected} ({lines[expected - 1]!r})"
        )
        assert lines[got - 1].strip().startswith("end")


def test_the_golden_lua_fixture_function_does_not_run_to_eof() -> None:
    src = (
        Path(__file__).parent
        / "fixtures" / "codegraph_golden" / "repo" / "src" / "vector.lua"
    ).read_text(encoding="utf-8")
    lines = src.split("\n")
    assert extract_end_keyword_block(lines, 29, language="lua") == 38
    assert len(lines) == 39, "the fixture's trailing line is what the old scan ran to"


# ═══════════════════════════════════════════════════════════════════════════
# THE MODIFIER DISCRIMINATION — both directions
# ═══════════════════════════════════════════════════════════════════════════
_MODIFIERS = [
    pytest.param("def a(x)\n  return 0 if x.nil?\n  x\nend\n", id="return-if"),
    pytest.param("def a(x)\n  raise unless x\n  x\nend\n", id="raise-unless"),
    pytest.param("def a(x)\n  x += 1 while x < 3\n  x\nend\n", id="while-modifier"),
    pytest.param("def a(x)\n  x -= 1 until x.zero?\n  x\nend\n", id="until-modifier"),
    pytest.param("def a(x)\n  puts x if x\n  x\nend\n", id="puts-if"),
    pytest.param("def a(x)\n  x.save! if x\n  x\nend\n", id="bang-method-then-if"),
    pytest.param("def a(x)\n  x.valid? if x\n  x\nend\n", id="predicate-method-then-if"),
]


@pytest.mark.parametrize("source", _MODIFIERS)
def test_a_ruby_statement_modifier_opens_no_block(source: str) -> None:
    """THE hazard. Counting one of these as an opener does not mis-measure by a
    line — it runs the body to the next unmatched ``end`` in the file."""
    assert _end(source, language="ruby") == 4


def test_a_string_before_the_modifier_does_not_make_it_an_opener() -> None:
    """The reason ``_scrub_line_stateful`` grew a ``placeholder``.

    Dropping the string leaves ``x = "hi" if flag`` as ``x =  if flag`` — a
    prefix ending in ``=``, which reads as expression-START and would classify
    this modifier as a block opener.
    """
    assert _end('def a\n  x = "hi" if flag\n  x\nend\n', language="ruby") == 4
    assert _end("def a\n  x = 'hi' unless flag\n  x\nend\n", language="ruby") == 4


def test_an_if_in_expression_position_DOES_open_a_block() -> None:
    """The other direction: the discrimination must not simply refuse every
    ``if``, or a real conditional expression's ``end`` is consumed as the
    method's."""
    src = "def a(x)\n  y = if x\n    1\n  else\n    2\n  end\n  y\nend\n"
    assert _end(src, language="ruby") == 8


@pytest.mark.parametrize(
    "prefix,opens",
    [
        ("", True),
        ("y = ", True),
        ("y ||= ", True),
        ("(", True),
        ("[", True),
        ("foo(a, ", True),
        ("a and ", True),
        ("a or ", True),
        ("z = 1; ", True),
        ("value ", False),
        ("value? ", False),
        ("save! ", False),
        ("return ", False),
        ("next ", False),
        ("break ", False),
        ("end ", False),
    ],
)
def test_expression_start_position_per_prefix(prefix: str, opens: bool) -> None:
    text = prefix + "if flag"
    assert _at_expression_start(text, text.index("if")) is opens


# ═══════════════════════════════════════════════════════════════════════════
# OPENS NOTHING — a one-line body, never the rest of the file
# ═══════════════════════════════════════════════════════════════════════════
def test_an_endless_method_is_one_line() -> None:
    """Ruby 3.0 ``def size = @n``. A scanner that waits for an ``end`` swallows
    every following method until it finds one."""
    src = "def size = @n\ndef other\n  1\nend\n"
    assert _end(src, language="ruby") == 1


def test_a_one_line_construct_closed_on_its_own_line_is_one_line() -> None:
    assert _end("class Foo; end\nx = 1\n", language="ruby") == 1


def test_an_unknown_language_answers_opens_nothing_rather_than_raising() -> None:
    """A heuristic must never crash the walk. Conservative: one line, not the
    rest of the file."""
    assert _end("func f() {\n  return 1\n}\n", language="go") == 1
    assert _end("x\ny\n", language="") == 1


def test_a_start_line_outside_the_file_degrades_like_the_brace_scanner() -> None:
    lines = "a\nb\nc\n".split("\n")
    assert extract_end_keyword_block(lines, 0, language="ruby") == min(40, len(lines))
    assert extract_end_keyword_block(lines, 99, language="ruby") == len(lines)


def test_an_unterminated_block_degrades_to_the_same_bounded_window() -> None:
    """Byte-for-byte the graceful-degradation branch ``_extract_balanced_block``
    already uses, so the two scanners tell one story about runaway input."""
    src = "def a\n" + "  x\n" * 200
    lines = src.split("\n")
    assert extract_end_keyword_block(lines, 1, language="ruby") == min(41, len(lines))


# ═══════════════════════════════════════════════════════════════════════════
# LEAVE-ALONE — shapes that must NOT be reinterpreted
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "decl,endless",
    [
        ("def size = @n", True),
        ("def self.size = @n", True),
        ("def scaled(f) = @n * f", True),
        ("def ==(other) = @n == other.n", True),
        ("def value=(v)", False),
        ("def ==(other)", False),
        ("def []=(k, v)", False),
        ("def deposit(amount)", False),
        ("def initialize(balance)", False),
        ("def total", False),
    ],
)
def test_endless_def_detection_reads_the_name_before_the_equals(decl: str, endless: bool) -> None:
    """``def value=(v)`` is a SETTER and ``def ==(other)`` an operator: the
    ``=`` is part of the NAME and is consumed before the endless test."""
    assert _is_endless_def(decl, decl.index("def") + 3) is endless


@pytest.mark.parametrize(
    "source,expected,why",
    [
        ("def a\n  begin\n    y\n  end while x\n  1\nend\n", 6,
         "`end while cond` — the while follows an `end`, so it is a modifier"),
        ("def a(r)\n  r.end if r\n  1\nend\n", 4, "`range.end` is a method, not a closer"),
        ("def a\n  h = {end: 1}\n  h\nend\n", 4, "`end:` is a hash key"),
        ("def a\n  s = :end\n  s\nend\n", 4, "`:end` is a symbol"),
        ("def a\n  # end\n  1\nend\n", 4, "an `end` in a comment is not a closer"),
        ("def a\n  s = 'end'\n  1\nend\n", 4, "an `end` in a string is not a closer"),
        ("def a\n=begin\nend\n=end\n  1\nend\n", 6,
         "an `end` inside a =begin block is not a closer"),
        ("def define_method_ish\n  1\nend\n", 3,
         "`def` inside a longer identifier is not a keyword"),
    ],
)
def test_ruby_tokens_that_look_like_keywords_are_not(source, expected, why) -> None:
    assert _end(source, language="ruby") == expected, why


def test_lua_long_comment_does_not_close_the_function() -> None:
    assert _end("function f()\n--[[\nend\n]]\n  return 1\nend\n", language="lua") == 6


# ═══════════════════════════════════════════════════════════════════════════
# TRI-OS (R12 / R14) — the only OS-dependent input is the line terminator
# ═══════════════════════════════════════════════════════════════════════════
_OS_CASES = [
    pytest.param("\n", id="linux-macos-LF"),
    pytest.param("\r\n", id="windows-CRLF"),
]

_TRI_OS_SOURCES = [
    pytest.param("ruby", "class Vault\n  def store(a)\n    return 0 if a.nil?\n    a\n  end\nend\n", 6, id="ruby-modifier"),
    pytest.param("ruby", "def size = @n\ndef other\nend\n", 1, id="ruby-endless"),
    pytest.param("ruby", "def a\n  [1].each do |x|\n    x\n  end\nend\n", 5, id="ruby-do-block"),
    pytest.param("lua", "function f()\n  if a then\n  end\nend\n", 4, id="lua-nested"),
]


@pytest.mark.parametrize("language,source,expected", _TRI_OS_SOURCES)
@pytest.mark.parametrize("terminator", _OS_CASES)
def test_the_answer_is_identical_on_every_os_line_terminator(
    language: str, source: str, expected: int, terminator: str
) -> None:
    """CI checks out LF, so a CRLF-only defect here would be invisible on every
    Linux and macOS run while breaking every Windows checkout — R14's "unit-test
    the decision against all three shapes" applied to this helper."""
    text = source.replace("\n", terminator)
    lines = text.split("\n")
    assert extract_end_keyword_block(lines, 1, language=language) == expected


def test_a_cr_only_file_is_a_single_line_for_this_scanner_too() -> None:
    """LEAVE-ALONE: classic pre-OS-X Mac line endings were already one line to
    every producer in this package, and this scanner does not change that."""
    text = "def a\r  1\rend\r"
    assert extract_end_keyword_block(text.split("\n"), 1, language="ruby") == 1


def test_no_wp5b_helper_branches_on_the_platform() -> None:
    """The scanner is pure text processing; an OS conditional in it would be
    the wrong fix, not a portability measure."""
    src = Path(_shared.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "extract_end_keyword_block"
    )
    seg = ast.get_source_segment(src, fn) or ""
    # Dotted attribute paths only. A bare `"nt"` / `"posix"` would be a
    # SUBSTRING check that any identifier containing those letters satisfies
    # (`int`, `position`) — a gate that fails on correct code.
    for token in ("sys.platform", "os.name", "platform.system", "os.sep"):
        assert token not in seg, f"{token!r} in extract_end_keyword_block"


# ═══════════════════════════════════════════════════════════════════════════
# REGISTRY PARITY — a call site must name a language the table declares
# ═══════════════════════════════════════════════════════════════════════════
def _end_block_calls(source: str) -> List[ast.Call]:
    """Every ``extract_end_keyword_block(...)`` CALL — never a mention of one in
    a comment or a string. Same AST locator idiom as the v0.2.91 marker-parity
    gate, for the same reason: a source-text search is satisfied by a docstring.
    """
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "extract_end_keyword_block"
    ]


def _call_language(call: ast.Call) -> Optional[str]:
    for kw in call.keywords:
        if kw.arg == "language":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
            return None
    return None


def _extractor_modules() -> Dict[str, Set[str]]:
    modules: Dict[str, Set[str]] = {}
    for key, fn in EXTRACTORS.items():
        modules.setdefault(fn.__module__, set()).add(key)
    return modules


def _module_source(module_name: str) -> str:
    mod = importlib.import_module(module_name)
    assert mod.__file__ is not None
    return Path(mod.__file__).read_text(encoding="utf-8")


def test_every_call_site_names_a_language_this_scanner_knows() -> None:
    """An unknown key answers "opens nothing" — correct as a fail-safe, silent
    as a typo. This is the gate that makes the typo loud."""
    problems: List[str] = []
    total = 0
    for module_name, keys in sorted(_extractor_modules().items()):
        for call in _end_block_calls(_module_source(module_name)):
            total += 1
            lang = _call_language(call)
            where = f"{module_name}:{call.lineno}"
            if lang is None:
                problems.append(f"{where}: no literal language= argument")
            elif lang not in _END_BLOCK_PROFILES:
                problems.append(f"{where}: language={lang!r} has no _END_BLOCK_PROFILES row")
            elif lang not in keys:
                problems.append(
                    f"{where}: language={lang!r} is not served by this module "
                    f"(expected one of {sorted(keys)})"
                )
    assert problems == [], "\n".join(problems)
    # Self-check: the gate must still SEE the sites it polices.
    assert total >= 3, f"only {total} call sites found — the locator went blind"


def test_every_profiled_language_also_has_its_comment_string_markers() -> None:
    """The scanner reads keywords out of ``_scrub_line_stateful``'s output, so a
    profile without a marker row would lex its own language as C-family and
    count an ``end`` inside a ``#`` comment."""
    missing = sorted(k for k in _END_BLOCK_PROFILES if k not in _shared._LANG_SYNTAX)
    assert missing == [], missing


def test_every_extractor_module_uses_exactly_one_block_scanner() -> None:
    """The invariant that replaces "only python bypasses the brace helper":
    ruby and lua now use the ``end``-keyword scanner INSTEAD of the brace one,
    so the honest property is that every non-python extractor uses one of the
    two, and python uses neither (it builds bodies from the AST).
    """
    braceless: Set[str] = set()
    for module_name in _extractor_modules():
        src = _module_source(module_name)
        tree = ast.parse(src)
        names = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        if not ({"_extract_balanced_block", "extract_end_keyword_block"} & names):
            braceless.add(module_name)
    assert braceless == {"vco_lib.codegraph_lang.python"}, sorted(braceless)
