# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""C# extractor for the code-graph analyzer (P2f stage 2, v0.2.76).

Moved VERBATIM from ``templates/scripts/analyze_code_graph.py``:
``_csharp_methods_for_class`` (V52-O.11.F.2-CSHARP per-class method
attribution) and ``CodeGraphAnalyzer._analyze_csharp_file`` — the move itself was verbatim apart from
the mechanical ``self.`` -> ``ctx.`` rename (``ctx`` IS the analyzer
instance) and the analyzer-resident embedding seams reached via ``ctx.``.
Behaviour has since been CORRECTED here (v0.2.92 and WP-5b — see the notes
below), so it is no longer byte-identical to the analyzer's original;
``tests/test_codegraph_golden.py`` pins what it does TODAY, and the
corpus README explains why a snapshot is evidence of behaviour rather
than of correctness.

v0.2.92 — two route-extraction defects fixed here (see
``tests/test_v0292_csharp_route_attribution.py``); the golden fixture's output
is unchanged by both, which is precisely why neither was caught:

  * the ``[Http*]`` attribute was located with a 5-line LOOKBACK WINDOW that
    could see a neighbouring method's attribute and miss the method's own, so
    two adjacent actions collapsed onto one ``endpoint:method`` identity and
    one endpoint was LOST. Replaced by structural attribution — see
    :func:`_csharp_attribute_block`.
  * an endpoint with no controller-level ``[Route]`` was stored WITHOUT its
    leading slash (``"all"``), unlike every other producer. Normalised by
    :func:`vco_lib.codegraph_lang._shared.join_route` — the ONE join, shared
    with the python producer since v0.2.92.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from vco_lib.codegraph_entities import (
    CodeEntity,
    FileExtraction,
    InteractionGroup,
    KIND_CLASS,
    KIND_FUNCTION,
    ModuleDescriptor,
)
from vco_lib.codegraph_lang._shared import (
    _extract_balanced_block,
    _extract_external_calls,
    blank_block_comments_preserving_lines,
    build_api_entity,
    join_route,
    run_pure_extractor,
    scan_to_declaration_terminator,
    skip_leading_whitespace,
)

# ── ASP.NET attribute binding (v0.2.92) ────────────────────────────────────
#
# An HTTP verb attribute decorates the declaration that FOLLOWS it, with only
# whitespace (and comments, already stripped from ``content_clean``) and other
# attributes in between. That is a structural relationship, and it is the one
# the extractor now walks. The previous proximity heuristic — "search the 5
# source lines ending at the method's ``start_line`` for `[Http`" — depended on
# ``start_line`` landing on the attribute, which it does only for SOME
# attribute shapes:
#
#   * ``[HttpGet("all")]`` — the ``(`` stops ``method_pattern``'s return-type
#     group, so the match begins at the newline ENDING the attribute line,
#     ``start_line`` IS that line, and the window's last line happens to be the
#     right attribute. This is the golden fixture's shape, and the reason both
#     defects here shipped green;
#   * ``[HttpPost]`` — the return-type group ``(?:[\w<>\[\]?]+\s+)+`` accepts
#     ``[``, ``]`` and word characters, so a PARENLESS attribute is swallowed
#     into the match as if it were a type token. The match then begins at the
#     whitespace after the PREVIOUS member (its ``}``/``;``), ``start_line`` is
#     one member too high, and the window both MISSES the method's own
#     attribute and REACHES the previous method's — which ``re.search`` (first
#     match wins) returns.
#
# Live consequence, reproduced in the test suite: ``[HttpGet("all")]`` on one
# action and ``[HttpPost]`` on the next produced two rows both claiming
# ``GET all`` — identical ``"<endpoint>:<method>"`` dedup identity, so they
# collapsed to ONE stored row and the POST endpoint vanished.
#
# Shapes handled: ``[HttpGet]``, ``[HttpGet("path")]``, ``[HttpGetAttribute]``
# (the CLR name), several attributes stacked on their own lines, several
# sharing one bracket (``[HttpGet, Route("x")]``), an attribute on the same
# line as the declaration, and more than one verb attribute on one action
# (each yields its own row — one row per declared endpoint is the whole point).
# Shapes deliberately NOT guessed at: a route template that is not a string
# literal, and an attribute block whose brackets do not balance. Those emit
# nothing, per the rule the python producer already follows — a fabricated
# endpoint is worse than a missing one.
_CSHARP_HTTP_ATTR_RE = re.compile(
    # Attribute-position anchor: an attribute starts either at the opening
    # bracket or after a comma inside a shared bracket. Both are required to
    # be in attribute position so an `Http…` substring inside a route template
    # string can never be read as a verb.
    r'[\[,]\s*Http(Get|Post|Put|Delete|Patch|Options|Head)'
    # `Attribute` suffix (and any other trailing word chars) — `[HttpGet]` and
    # `[HttpGetAttribute]` are the same attribute. Matches the pre-v0.2.92
    # prefix-match behaviour.
    r'\w*'
    # Optional route template. A non-literal template (`nameof(x)`, a const)
    # yields no capture and the method falls back to the same default it
    # always had.
    r'(?:\s*\(\s*["\']([^"\']+)["\'])?',
    re.IGNORECASE,
)

# A METHOD-level `[Route("…")]`, read only from inside a method's own attribute
# block. Same `[`-or-`,` attribute-position anchor as the verb pattern above, so
# `[HttpGet, Route("all")]` resolves. Deliberately NOT reused for the
# CONTROLLER-level lookup, which scans the whole file prefix: widening that one
# would let an earlier method's shared-bracket `, Route("x")` become every later
# method's prefix — a new mis-attribution in place of the one being fixed.
_CSHARP_METHOD_ROUTE_RE = re.compile(r'[\[,]\s*Route\s*\(\s*["\']([^"\']+)["\']')

# An attribute argument list can legitimately contain balanced brackets inside
# a string (`[Route("api/[controller]")]` — ASP.NET token replacement), so the
# closing bracket is matched by depth rather than by the first `[` to the left.
# Bounded: an "attribute" longer than this is not one, and an unbalanced `]`
# must not turn a whole file into a backward scan.
_CSHARP_ATTR_SCAN_LIMIT = 4000

# Tokens that look like an identifier in capture position but are actually C#
# keywords. v0.2.92: hoisted to module level from inside
# ``_csharp_methods_for_class``, where it was the SECOND of two divergent
# keyword filters in this file — the entity-emitting loop carried its own
# 10-word inline tuple (``if while for foreach switch catch try return new
# throw``) and therefore did NOT filter ``await``, ``default``, ``finally``,
# ``base``, ``nameof`` and the rest. One set, both call sites.
_CSHARP_KW_FILTER = frozenset({
    "if", "else", "while", "for", "foreach", "switch", "try", "catch",
    "finally", "return", "new", "throw", "using", "lock", "yield",
    "do", "break", "continue", "goto", "case", "default", "checked",
    "unchecked", "fixed", "stackalloc", "await", "is", "as", "in",
    "out", "ref", "params", "where", "when", "var", "true", "false",
    "null", "this", "base", "typeof", "sizeof", "nameof",
})

# Tokens that may not appear in a member declaration's MODIFIER / RETURN-TYPE
# run. ``method_pattern``'s return-type group ``(?:[\w<>\[\]?]+\s+)+`` accepts
# any word-shaped token, so it happily reads a STATEMENT or a TYPE DECLARATION
# as "modifiers + return type" and mints a function row for it. Measured on the
# golden fixture, which pinned two such rows:
#
#   ``public record Item(int Id, string Name);``  → run ``public record``
#   ``return new Item(id, "widget");``            → run ``return new``
#   ``return Ok();``                              → run ``return``
#   ``public class Foo(int x) { }`` (C# 12)       → run ``public class``
#
# Note the fix is on the RUN, not on the captured name: in every case above the
# captured name (``Item``, ``Ok``, ``Foo``) is a perfectly valid identifier, so
# ``_CSHARP_KW_FILTER`` cannot see the problem. ``new`` and ``ref`` are
# deliberately ABSENT here — both are legal member modifiers (``public new int
# F()``, ``public ref int F()``) and the statement forms that contain them are
# already rejected by the ``return`` / ``throw`` / ``yield`` in the same run.
_CSHARP_NON_DECL_TOKENS = frozenset({
    # statement keywords
    "return", "throw", "yield", "await", "case", "goto", "stackalloc",
    # type-declaration keywords (a type is not a method)
    "class", "struct", "interface", "record", "enum", "delegate",
    "namespace", "event",
    # `implicit operator Foo(...)` / `explicit operator Foo(...)` capture the
    # TARGET TYPE as the method name. The class docstring already claims
    # operator overloads are not captured; rejecting the run makes that true
    # for the conversion forms too (`operator+` was never capturable, since
    # `+` is not `[\w]+`).
    "operator",
})


# v0.2.92 WP-5b — the POSITIONAL RECORD, which emitted no entity at all.
#
# ``class_pattern`` below captures the type name with ``([\w<>, ]+?)`` and then
# demands ``\{``. A positional record puts a parameter list between the two:
#
#     public record Item(int Id, string Name);        ← no body at all
#     public record Point(int X, int Y) { … }         ← body, but after `(…)`
#
# ``(`` is not in the name character class, so NEITHER form can match and the
# type was simply absent from the graph — while `method_pattern` used to mint a
# spurious FUNCTION row for the first shape (removed earlier in v0.2.92, which
# left the type with no row of any kind). A record IS a type: it is what C# 9+
# code uses for the DTOs an API surface is made of, so losing it loses exactly
# the types a code-graph search is most often asked about.
#
# This pattern is disjoint from ``class_pattern`` by construction — it REQUIRES
# the ``(`` that ``class_pattern`` cannot match — so the two never double-count
# the same declaration and no dedup is needed. ``record class`` / ``record
# struct`` (C# 10) are accepted.
_CSHARP_POSITIONAL_RECORD_RE = re.compile(
    r'(?:public|private|protected|internal|abstract|sealed|partial|\s)+'
    r'record(?:\s+(?:class|struct))?\s+([\w]+)(?:\s*<[^>]*>)?\s*\(',
    re.MULTILINE,
)


def _csharp_declaration_run_is_a_member(content_clean: str, decl_pos: int, name_pos: int) -> bool:
    """True when ``content_clean[decl_pos:name_pos]`` is a member's modifier /
    return-type run rather than a statement or a type declaration.

    ``decl_pos`` is :func:`_csharp_declaration_start`'s output (attributes
    already skipped) and ``name_pos`` is the offset of the captured name.
    """
    return not (_CSHARP_NON_DECL_TOKENS & set(
        re.findall(r"[A-Za-z_]\w*", content_clean[decl_pos:name_pos])
    ))


def _csharp_expression_body_end(content_clean: str, from_pos: int) -> int:
    """Offset just past the ``;`` terminating an expression-bodied member.

    An expression body (``public int F() => x + 1;``) ends at the first ``;``
    that is not nested inside brackets — the nesting check (owned by the
    shared scanner) is what keeps a STATEMENT-lambda argument
    (``=> Items.Select(x => { var y = x; return y; }).Count();``) from ending
    the member at its inner ``;``. Returns ``from_pos`` when no terminator is
    found inside the scan bound.
    """
    ch, pos = scan_to_declaration_terminator(
        content_clean, from_pos, stops=";", limit=_CSHARP_ATTR_SCAN_LIMIT
    )
    return pos + 1 if ch is not None else from_pos


def _csharp_match_open_bracket(text: str, close_idx: int) -> Optional[int]:
    """Index of the ``[`` matching the ``]`` at ``close_idx``, or ``None``.

    ``None`` means "cannot attribute this" (unbalanced, or beyond the scan
    bound) and callers must then record NO route rather than guess one.
    """
    depth = 0
    floor = max(0, close_idx - _CSHARP_ATTR_SCAN_LIMIT)
    for k in range(close_idx, floor - 1, -1):
        ch = text[k]
        if ch == ']':
            depth += 1
        elif ch == '[':
            depth -= 1
            if depth == 0:
                return k
    return None


def _csharp_declaration_start(content_clean: str, match_start: int, match_end: int) -> int:
    """First character of the DECLARATION inside a ``method_pattern`` match.

    ``match.start()`` is not it. Two independent reasons, and a method hits one
    or the other depending on what precedes it:

      * the pattern's leading ``(?:public|…|\\s)+`` group starts matching at the
        whitespace that follows the previous token, so the match routinely
        begins on the PREVIOUS line;
      * the pattern's return-type group ``(?:[\\w<>\\[\\]?]+\\s+)+`` accepts
        ``[`` and ``]``, so an attribute sitting between the previous token and
        the declaration is swallowed INTO the match as if it were a type.

    Skip forward over whitespace and over whole balanced ``[...]`` groups; what
    remains is the modifier/return-type/name run. Never walks past
    ``match_end``, and an unbalanced ``[`` stops the walk (the caller then finds
    no attribute block and records no route — the conservative outcome).
    """
    pos = match_start
    while pos < match_end:
        # The whitespace half is the SHARED walk (v0.2.92) — java and cpp need
        # exactly it; only the attribute skip below is C#-specific.
        pos = skip_leading_whitespace(content_clean, pos, match_end)
        if pos >= match_end or content_clean[pos] != '[':
            break
        depth = 0
        close = None
        for k in range(pos, min(match_end, pos + _CSHARP_ATTR_SCAN_LIMIT)):
            ch = content_clean[k]
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    close = k
                    break
        if close is None:
            break
        pos = close + 1
    return pos


def _csharp_attribute_block(content_clean: str, decl_pos: int) -> Tuple[str, int]:
    """The attribute block that DECORATES the declaration starting at ``decl_pos``.

    Walks left from the declaration over any run of ``[...]`` attributes
    separated by whitespace, and stops at the first token that is not one —
    ``{`` (the enclosing type's body opener), ``;`` or ``}`` (the previous
    member), or the start of the file. That stop condition is what keeps a
    sibling's attribute, a field's attribute and the class-level ``[Route]``
    out of this method's block.

    Returns ``(block_text, block_start)``; ``("", decl_pos)`` when the
    declaration carries no attributes at all.
    """
    block_start = decl_pos
    cursor = decl_pos
    while True:
        probe = cursor - 1
        while probe >= 0 and content_clean[probe].isspace():
            probe -= 1
        if probe < 0 or content_clean[probe] != ']':
            break
        open_idx = _csharp_match_open_bracket(content_clean, probe)
        if open_idx is None:
            break
        block_start = open_idx
        cursor = open_idx
    return content_clean[block_start:decl_pos], block_start


def _csharp_methods_for_class(
    content_clean: str,
    class_name: str,
    source_lines: List[str],
) -> List[str]:
    """V52-O.11.F.2-CSHARP (v0.2.52, 2026-06-09): extract methods declared
    inside the lexical body of a C# ``class``, ``struct``, ``record``, or
    ``interface`` named ``class_name``.

    Replaces the pre-V52-O.11.F.2-CSHARP line at ``analyze_code_graph.py:2540``
    that ran ``method_pattern.finditer(content_clean)`` unconditionally —
    the same bug Rust V52-O.11.F closed, applied to the C# parser. Audit
    a79152 confirmed: a 50-method file with 3 classes produced 150
    incorrect method attributions per the
    ``query_code_structure(methods, ClassName)`` MCP path.

    C# method-scoping model (lexical, like Java):

      Methods live between a type's opening ``{`` and its matching close.
      Unlike Rust there is no separate ``impl`` block — declarations are
      lexically inside the class body. Same shape as Java, but with C#
      specifics:
        * Modifiers: ``public``, ``private``, ``protected``, ``internal``,
          ``protected internal``, ``private protected``, ``static``,
          ``virtual``, ``override``, ``sealed``, ``abstract``, ``async``,
          ``extern``, ``partial``, ``unsafe``, ``readonly`` (on structs).
        * Properties (``public int Foo { get; set; }``) — we INCLUDE the
          property name in the methods list. Rationale: mirrors how the
          Python parser counts ``@property`` decorated members, and the
          underlying ``get_Foo`` / ``set_Foo`` are real methods at the
          CLR level. The task brief calls this out explicitly.
        * Indexers (``public T this[int i] { get; set; }``) — surfaced
          as ``Item`` per the CLR property convention (``get_Item`` /
          ``set_Item``).
        * Records: ``record Foo(int X, int Y)`` — the positional
          parameters generate auto-property accessors. We do NOT try to
          extract those from the primary-constructor signature; only
          explicit declarations inside the record body land in methods.
        * Async methods (``async Task<T> Foo()``) — picked up.
        * Generic methods (``T Method<U>(U arg)``) — picked up.
        * Partial classes (``partial class Foo { ... } partial class Foo
          { ... }``) — every matching declaration's body contributes; the
          union is returned (mirrors Rust's multi-``impl`` behavior).

    Algorithm:

      1. Locate every class/struct/record/interface declaration whose
         name matches ``class_name``. Multiple declarations are allowed
         (partial classes). The class header regex tolerates the same
         modifier mix as the parser's outer ``class_pattern`` plus an
         optional inheritance clause (``: BaseClass, IInterface``).
      2. For each match, use ``_extract_balanced_block`` to find the
         body's closing brace (already brace-balanced via V52-O.11.E).
      3. Scan the body text (sliced from ``content_clean``, NOT
         ``source_lines`` — keeps the same comment-stripped surface the
         old per-file regex used) for:
            a. Method declarations: ``[modifiers] [returntype] Name(args) {`` or
               ``[modifiers] [returntype] Name(args) =>`` (expression-bodied)
               or ``[modifiers] [returntype] Name(args);`` (abstract /
               interface).
            b. Property declarations: ``[modifiers] [type] Name { get; set; }``
               (no parens after the name) — the body starts directly with
               ``{`` containing ``get``/``set``.
            c. Indexer declarations: ``[modifiers] [type] this[...] {`` —
               normalized to the literal ``Item``.
      4. Filter out C# keywords that the method-regex could otherwise
         hit (``if``, ``while``, ``for``, ``foreach``, ``switch``, ``try``,
         ``catch``, ``return``, ``new``, ``throw``, ``using``, ``lock``,
         ``yield``).
      5. Return deduplicated list, preserving first-seen order.

    Returns empty list if no class/struct/record/interface body matches
    ``class_name`` — common when ``class_name`` was extracted from a
    forward-declaration or a partial class whose other halves live in
    different files.

    Limitations:
      * Constructors are extracted as methods (their "name" matches the
        class name). The existing Java/JS parsers behave the same way;
        the embedding pipeline doesn't distinguish.
      * Static constructors (``static Foo() { ... }``) are also captured.
      * Operator overloads (``public static Foo operator+(Foo a, Foo b)``)
        are NOT captured — the name after ``operator`` isn't a valid
        identifier. This matches the upstream parser's behavior.
      * Nested types inside the class body are correctly scoped via
        brace-balance: members of nested types fall inside the outer
        class's braces but ALSO inside the nested type's own braces, so
        we'd double-count them if not careful. We handle this by
        recognizing that the nested type's header itself doesn't match
        the method pattern, but its INNER members do — so a nested class
        ``Inner`` inside ``Outer`` would leak ``Inner``'s members into
        ``Outer``'s methods. We accept this minor over-count for now
        (matches Java's behavior); a tree-sitter rewrite is the right
        fix (queued as V52-O.11.G).
      * Doesn't follow ``partial`` declarations across files (each file
        is parsed independently — same as Java's package-private split).
    """
    escaped = re.escape(class_name)
    # Class/struct/record/interface header. Modifiers + keyword + name +
    # optional generics + optional inheritance + opening brace. The name
    # capture is anchored by the `escaped` literal so we only match THE
    # target class (not other classes that happen to start with the same
    # prefix).
    #
    # Why not reuse the parser's outer `class_pattern`? That one captures
    # the name into group(1) for class-info population. Here we need to
    # gate on a SPECIFIC name and find the body opener — a different shape.
    class_header_pattern = re.compile(
        # Optional modifiers (zero or more, repeated). C# allows any order.
        r"(?:(?:public|private|protected|internal|abstract|sealed|partial|"
        r"static|unsafe|readonly|ref)\s+)*"
        # Keyword introducing the type
        r"(?:class|struct|record|interface)\s+"
        # The target name (exact match — escaped). Trailing word boundary
        # prevents `Stress` from matching `Stress2` (a strict-prefix
        # superset name). Critical: without `\b` the regex matches BOTH
        # the literal name AND any longer name starting with it, causing
        # body extraction to fall through to the next class's brace.
        rf"{escaped}\b"
        # Optional generic parameters: <T>, <T, U>, <T : IFoo>
        r"(?:\s*<[^>]*>)?"
        # Optional primary-constructor parameter list (record Foo(int X, int Y))
        r"(?:\s*\([^)]*\))?"
        # Optional pre-body trailer: covers BOTH the inheritance clause
        # (`: Base, IFoo<T>`) AND the generic constraints (`where T : new()`).
        # We accept any sequence of non-`{`, non-`;` chars; the `where`
        # clause's own `()` and `<>` are safely consumed because the only
        # stopping conditions are the opening brace and the declaration
        # terminator. This single permissive trailer handles all of:
        #     class Foo : Base { ... }
        #     class Foo<T> where T : new() { ... }
        #     class Foo<T> : Base where T : new() { ... }
        #     class Foo<T, U> where T : class where U : struct, new() { ... }
        #
        # v0.2.92 WP-5b: `;` added to the stop set. It used to be `[^{]*`,
        # which walks straight PAST a bodiless declaration's terminator and
        # latches onto the NEXT type's brace: asked for the members of
        # `record Item(int Id, string Name);` the helper returned
        # `InventoryController`'s five members, because the trailer ate
        # `;`, the blank line, the `[Route]` attribute and the class header.
        # Latent until this release — the positional record was absent from
        # `class_info`, so nothing ever asked. No legal C# type header
        # contains a `;` before its body opener.
        r"[^{;]*"
        # Opening brace
        r"\{",
        re.MULTILINE,
    )

    # Inner method/property/indexer pattern. Three shapes packed into one
    # alternation so a single pass over the class body collects all three.
    #
    # Strategy:
    #   * Anchor on start-of-line (re.MULTILINE + `^[ \t]*`) — declarations
    #     in C# are always on their own line, statements inside methods
    #     are indented further or follow other statements.
    #   * Modifiers are OPTIONAL because (a) interface members have no
    #     explicit modifier prior to C# 8 default-interface-methods, and
    #     (b) struct members can also be implicit-private.
    #   * To compensate for optional modifiers, we filter out matches whose
    #     captured name is a C# control-flow keyword (`if`, `return`,
    #     `new`, etc.) — that catches the common false-positive shapes
    #     like `return new U();` and `where U : T, new()`.
    #   * We also filter out matches where the supposed return type IS
    #     itself a control-flow keyword (`return`, `throw`, etc.) — that
    #     filters `return new U();` more aggressively.
    #
    # Shape M (method):
    #   ^[ws] [modifier]* [returntype] Name(args) ( { | => | ; )
    # Shape P (property):
    #   ^[ws] [modifier]* [type] Name { get ...|set ...|init ... }
    # Shape I (indexer):
    #   ^[ws] [modifier]* [type] this[args] {
    method_modifier_alt = (
        r"(?:public|private|protected|internal|static|virtual|override|"
        r"async|abstract|sealed|extern|partial|new|unsafe|readonly)"
    )
    # Zero or more modifier tokens (with whitespace between). Optional
    # so interface methods (no modifier) and implicit-private members
    # still match. We compensate via the keyword filter below.
    method_modifiers_opt = rf"(?:{method_modifier_alt}\s+)*"
    # Compact type pattern — matches type-shaped tokens like:
    #   T, int, string, Task, Task<int>, IList<T>, T[], int?, Dictionary<K,V>,
    #   IList<KeyValuePair<string, object>>  (nested generics)
    # The `\w` alternative handles single-letter generic params; the
    # longer alternative handles compound generics.
    #
    # CRITICAL: type_shape MUST NOT allow whitespace at the top level,
    # only inside angle brackets. Otherwise `return new U(` matches with
    # type_shape spanning `return new` (taking the whitespace) and name
    # being `U` — false positive. We allow whitespace only via nested
    # generic groups that swallow arbitrary text including commas/spaces.
    #
    # Nested generics: Python regex doesn't support true recursion, but we
    # can hand-roll a 3-level nested-generic pattern that covers all
    # practical cases. Format builds bottom-up:
    #   level0: <...> with no nested angles
    #   level1: <... level0 ...> — one level of nesting
    #   level2: <... level1 ...> — two levels of nesting (e.g.
    #     IList<KeyValuePair<string, object>>)
    #   level3: <... level2 ...> — three levels (e.g.
    #     Task<Dictionary<int, List<string>>>)
    # A 4th level (Task<Dictionary<int, List<Dictionary<...>>>>) is
    # exceedingly rare and falls back to graceful failure (method not
    # captured; doesn't break the parser).
    _ang_lvl0 = r"<[^<>]*>"
    _ang_lvl1 = rf"<(?:[^<>]|{_ang_lvl0})*>"
    _ang_lvl2 = rf"<(?:[^<>]|{_ang_lvl1})*>"
    _generic_block = _ang_lvl2
    type_shape = (
        # First char: word or dot
        r"[\w.]"
        # Then any mix of:
        #   * word/dot/?/[]
        #   * nested generic block (covers up to 2 levels of nesting)
        rf"(?:[\w.\[\]\?]|{_generic_block})*"
    )
    method_decl = re.compile(
        r"^[ \t]*"
        + method_modifiers_opt
        # Return type
        + type_shape + r"\s+"
        # Method name (captured)
        + r"([\w]+)"
        # Optional generic type parameters on the method
        + r"(?:\s*<[^>]*>)?"
        # Argument paren (anchor)
        + r"\s*\(",
        re.MULTILINE,
    )
    property_decl = re.compile(
        r"^[ \t]*"
        + method_modifiers_opt
        # Return type
        + type_shape + r"\s+"
        # Property name (captured) followed by `{` (NOT `(`)
        + r"([\w]+)\s*\{"
        # Lookahead for accessor keyword to confirm this is a property
        + r"(?=\s*(?:[\[\w]|//|/\*)*\s*(?:get|set|init)\b)",
        re.MULTILINE,
    )
    indexer_decl = re.compile(
        r"^[ \t]*"
        + method_modifiers_opt
        + type_shape + r"\s+"
        + r"(this)\s*\[[^\]]*\]\s*\{",
        re.MULTILINE,
    )

    # Tokens that look like an identifier in capture position but are actually
    # C# control-flow keywords are filtered via the MODULE-level
    # ``_CSHARP_KW_FILTER`` (v0.2.92 — it used to be redeclared here, which is
    # how the entity-emitting loop came to use a smaller, divergent set).

    methods: List[str] = []
    seen: set = set()
    for hdr in class_header_pattern.finditer(content_clean):
        # Locate the opening brace position (regex anchors on it).
        # hdr.end() is one past `{`; the brace itself is at end()-1.
        body_open_pos = hdr.end() - 1
        body_open_line = content_clean[:body_open_pos].count("\n") + 1
        body_close_line = _extract_balanced_block(
            source_lines, body_open_line, max_lookahead=800, language="csharp"
        )
        # Convert close-line back to a char offset in content_clean by
        # counting newlines from body_open_pos onward.
        target_newlines = body_close_line - body_open_line
        if target_newlines <= 0:
            continue
        block_start_pos = body_open_pos + 1  # skip the `{`
        seen_newlines = 0
        block_end_pos = block_start_pos
        while seen_newlines < target_newlines and block_end_pos < len(content_clean):
            if content_clean[block_end_pos] == "\n":
                seen_newlines += 1
            block_end_pos += 1
        body = content_clean[block_start_pos:block_end_pos]

        # Indexers FIRST — the indexer regex captures the literal `this`
        # which we map to `Item`. Doing this before the method regex
        # avoids the method regex over-matching on `this(` constructor
        # chains (rare, but defensive).
        for im in indexer_decl.finditer(body):
            if "Item" in seen:
                continue
            seen.add("Item")
            methods.append("Item")

        # Methods second.
        for mm in method_decl.finditer(body):
            name = mm.group(1)
            if name in _CSHARP_KW_FILTER:
                continue
            if name in seen:
                continue
            seen.add(name)
            methods.append(name)

        # Properties third — the property regex is anchored on `{` with
        # no preceding `(`, so it doesn't double-match methods.
        for pm in property_decl.finditer(body):
            name = pm.group(1)
            if name in _CSHARP_KW_FILTER:
                continue
            if name in seen:
                continue
            seen.add(name)
            methods.append(name)

    return methods


def extract_csharp_file(
    source_text: str, file_path: Path, repo_root: Path, helpers: Any,
) -> FileExtraction:
    """Pure producer: parse a C# file, RETURN a :class:`FileExtraction`.

    Extracts using directives, classes, interfaces, methods, and ASP.NET route attributes.
    Also populates CodeAPI for [HttpGet/Post/Put/Delete/Patch] annotated methods.
    Each ASP.NET CodeAPI entity references its handler function via
    ``extras['_handler_full_name']`` — the writer resolves it to the function's
    freshly-minted UUID (the imperative extractor captured ``func_uuid`` from
    its own ``store_entity`` return; a pure producer cannot).
    """
    content = source_text
    source_lines = content.split('\n')
    loc = len([line for line in source_lines
               if line.strip() and not line.strip().startswith('//')
               and not line.strip().startswith('*')])
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()

    content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
    content_clean = blank_block_comments_preserving_lines(content_clean)

    # using directives
    imports = re.findall(r'^\s*using\s+([\w.]+)\s*;', content, re.MULTILINE)

    # namespace
    ns_match = re.search(r'namespace\s+([\w.]+)', content)
    ns = ns_match.group(1) if ns_match else file_path.stem

    # Classes / interfaces / records
    class_pattern = re.compile(
        r'(?:public|private|protected|internal|abstract|sealed|partial|\s)+'
        r'(?:class|interface|record|struct)\s+([\w<>, ]+?)(?:\s*:\s*[\w<>, ]+?)?\s*\{',
        re.MULTILINE
    )
    # v0.2.92 WP-5b: a LIST of (name, start_line, end_line), not a dict keyed
    # by name. Two reasons, both of which the dict made unrepresentable:
    #   * two same-named types in one file (two namespaces, or a `partial
    #     class` split within the file) collapsed onto one row — the same loss
    #     class this release fixed for FUNCTIONS via the occurrence
    #     disambiguator, which keys on `(kind, identity_key)` and has always
    #     covered KIND_CLASS;
    #   * a positional record's `end_line` cannot come from a brace scan, so
    #     each declaration now carries the end its OWN shape implies.
    class_decls: List[Tuple[str, int, int]] = []
    for m in class_pattern.finditer(content_clean):
        raw = m.group(1).strip().split('<')[0].strip()  # strip generics
        if not raw or raw[0].islower():
            continue
        # v0.2.92: `m.start()`, NOT the declaration. `class_pattern`'s leading
        # group `(?:public|…|\s)+` starts matching at the whitespace after the
        # PREVIOUS token, so the match routinely begins on the previous line —
        # the golden fixture's `interface IRepository` was stored starting on
        # the enclosing namespace's `{`, which then made
        # `_extract_balanced_block` return the NAMESPACE's closing brace and
        # gave the interface a `body` containing the whole file.
        start_line = content_clean[
            :_csharp_declaration_start(content_clean, m.start(), m.end())
        ].count('\n') + 1
        class_decls.append((
            raw, start_line,
            _extract_balanced_block(source_lines, start_line, language="csharp"),  # V52-O.11.E (was: start_line + 60)
        ))

    # v0.2.92 WP-5b: positional records (`record Item(int Id, string Name);`).
    # See `_CSHARP_POSITIONAL_RECORD_RE` — disjoint from `class_pattern`, so a
    # declaration is never counted twice. The terminator decides the extent:
    # `;` is a bodiless record that ends on its own declaration, `{` opens a
    # body the brace scanner can measure. Same three-way branch the METHOD loop
    # below already uses, for the same reason — running a brace scan on a
    # declaration that opens no brace latches onto the next construct's.
    for m in _CSHARP_POSITIONAL_RECORD_RE.finditer(content_clean):
        rname = m.group(1)
        decl_pos = _csharp_declaration_start(content_clean, m.start(), m.end())
        start_line = content_clean[:decl_pos].count('\n') + 1
        # Scanning from the `(` itself: the shared scanner counts it as depth,
        # so the parameter list's own `;`/`{` can never be read as the
        # terminator (`record Pair(Func<int> f = () => { });`).
        term, term_pos = scan_to_declaration_terminator(
            content_clean, m.end() - 1, stops=";{"
        )
        if term == '{':
            end_line = _extract_balanced_block(source_lines, start_line, language="csharp")
        elif term == ';':
            end_line = content_clean[:term_pos].count('\n') + 1
        else:
            end_line = start_line
        class_decls.append((rname, start_line, end_line))

    class_decls.sort(key=lambda d: d[1])  # source order, records interleaved
    #: Unique type names in source order — what the module summary lists.
    class_names: List[str] = list(dict.fromkeys(n for n, _, _ in class_decls))

    # Methods: access modifier + return type + name(...)
    method_pattern = re.compile(
        r'(?:public|private|protected|internal|static|virtual|override|async|abstract|\s)+'
        r'(?:[\w<>\[\]?]+\s+)+([\w]+)'
        # v0.2.92: optional generic type parameters on the METHOD. Without this
        # group the name capture `([\w]+)\s*\(` cannot reach the `(` of
        # `WrapAll<T>(T single)`, so every generic method in every C# file was
        # missing from CodeFunction entirely — including the golden fixture's,
        # which `_csharp_methods_for_class` listed under the class while no
        # function row existed. The parameter list is deliberately NOT allowed
        # to contain `<`, `>` or a paren, and must START with an identifier
        # character, so a relational expression (`a < b && c > (d)`) cannot be
        # read as a generic argument list. Nested generics (`Foo<List<int>>(`)
        # therefore still miss — strictly better than today's zero, and the
        # conservative direction. Mirrors the `(?:\s*<[^>]*>)?` group
        # `_csharp_methods_for_class`'s own `method_decl` has always carried.
        r'(?:\s*<[A-Za-z_][\w\s,\.\[\]?]*>)?'
        r'\s*\([^)]*\)\s*'
        # v0.2.92: a generic method's CONSTRAINTS sit between the argument list
        # and the body (`GetAll<T>(int id) where T : class {`). Without this the
        # terminator is unreachable and the method has no row — the constrained
        # form is the commonest real shape of the generic methods this release
        # set out to recover, so recovering only the unconstrained ones would
        # have closed half the defect. Bounded by `[^{;]` so it can never eat
        # the body it precedes. The class header pattern has accepted a `where`
        # clause since v0.2.52; the method pattern now agrees with it.
        r'(?:where\s+[^{;]*)?'
        # The terminator is CAPTURED (group 2). The producer always knew whether
        # the declaration opens a brace block, an expression body or nothing at
        # all, and threw that away — then ran a brace scan on all three. See the
        # `end_line` branch below.
        r'(\{|=>|;)',
        re.MULTILINE
    )

    # Route attribute (base route on controller or per-method)
    route_attr_pattern = re.compile(r'\[Route\s*\(\s*["\']([^"\']+)["\']')

    # Module summary
    file_comment = ''
    for line in source_lines[:20]:
        s = line.strip()
        if s.startswith('///') or s.startswith('//'):
            file_comment = s.lstrip('/').strip()
            break
    summary_parts = [f"C# module: {relative_path} (namespace {ns})"]
    if file_comment:
        summary_parts.append(file_comment)
    if class_names:
        summary_parts.append(f"Classes: {', '.join(class_names[:8])}")
    module_summary = '\n'.join(summary_parts)

    complexity = float(1 + sum(content_clean.count(kw)
                               for kw in ['if (', 'while (', 'for (', 'foreach (', 'switch (', 'catch (']))

    module = ModuleDescriptor(
        path=relative_path, language="C#", loc=loc, complexity=complexity,
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash, imports=imports, module_summary=module_summary,
    )
    entities: List[CodeEntity] = []
    stats: Dict[str, int] = {'modules': 1, 'classes': 0, 'functions': 0}

    # Classes
    #
    # v0.2.92 — the `end_line` convention. `_extract_balanced_block` returns
    # the 1-indexed CLOSING line, and its docstring states that IS the
    # `end_line` at every caller site. This loop used to store
    # `start_line + len(class_lines)`, which is that line PLUS ONE — the golden
    # fixture recorded `end_line: 45` for a 44-line file — while the METHOD
    # loop below already used the returned value directly. One convention now.
    _methods_by_name: Dict[str, List[str]] = {}
    for cname, start_line, _class_end_line in class_decls:
        class_lines = source_lines[max(0, start_line - 1):_class_end_line]
        class_body = '\n'.join(class_lines)
        # V52-O.11.F.2-CSHARP (v0.2.52, 2026-06-09): scope `methods` to
        # method declarations INSIDE `class <cname> { ... }` (also struct,
        # record, interface). Pre-V52-O.11.F.2 this line ran
        # `method_pattern.finditer(content_clean)` over the WHOLE file —
        # attributing EVERY method to EVERY class. Same antipattern as
        # V52-O.11.F (Rust). Audit a79152.
        #
        # Computed once per NAME: the helper already unions every declaration
        # of that name (the partial-class case), so two rows for one name would
        # otherwise repeat identical work. A POSITIONAL record gets `[]` — its
        # header has no `{`, and the helper's docstring has always said the
        # primary-constructor parameters are not extracted as members.
        if cname not in _methods_by_name:
            _methods_by_name[cname] = _csharp_methods_for_class(
                content_clean, cname, source_lines
            )
        methods = _methods_by_name[cname]
        signature = f"class {cname}"
        entities.append(CodeEntity(
            kind=KIND_CLASS, file_path_rel=relative_path,
            name=cname, full_name=f"{ns}.{cname}",
            body=class_body, signature=signature, doc="",
            start_line=start_line, end_line=_class_end_line,
            project=helpers.project_name,
            extras={"methods": methods[:20]},
            deferred_embed=(
                lambda sig=signature, cb=class_body, mth=methods:
                helpers.embed_class(sig, cb, methods=mth[:10], language="csharp")
            ),
        ))
        stats['classes'] += 1

    # Methods
    for m in method_pattern.finditer(content_clean):
        mname = m.group(1)
        terminator = m.group(2)
        # v0.2.92: the MODULE-level filter, which is the same set
        # `_csharp_methods_for_class` uses. The inline 10-word tuple that stood
        # here let `await`, `default`, `finally`, `base` and `nameof` through.
        if mname in _CSHARP_KW_FILTER:
            continue
        # v0.2.92: the declaration start, NOT `m.start()`. See
        # `_csharp_declaration_start` — the leading modifier group begins
        # matching at the whitespace after the previous token, and the
        # return-type group swallows a parenless `[HttpPost]` attribute as if
        # it were a type token, so `m.start()` lands one member too high. It is
        # already what the route path below uses; the entity's own
        # `start_line`/`end_line`/`body` were still derived from the raw match.
        decl_pos = _csharp_declaration_start(content_clean, m.start(), m.end())
        # v0.2.92: reject a match whose modifier/return-type run is actually a
        # statement (`return Ok();`) or a type declaration
        # (`record Item(int Id, string Name);`). Both minted function rows in
        # the golden fixture. See `_CSHARP_NON_DECL_TOKENS`.
        if not _csharp_declaration_run_is_a_member(content_clean, decl_pos, m.start(1)):
            continue
        start_line = content_clean[:decl_pos].count('\n') + 1
        # v0.2.92: branch on the DECLARATION TERMINATOR. Only a `{` opens a
        # brace block, and only for that shape is a brace scan meaningful:
        #   * `;`  — an interface / abstract / extern declaration has NO body
        #     and ends on its own line. Running `_extract_balanced_block` from
        #     it finds no opener on the declaration line, keeps scanning, and
        #     latches onto the NEXT type's braces: on the golden fixture,
        #     `IRepository.Find` (a one-line interface method) would take a
        #     `body` spanning the record, the `[Route]` attribute and the
        #     controller's class header. Masked before this release only
        #     because the skewed `start_line` happened to point at the
        #     interface's own `{`.
        #   * `=>` — an expression-bodied member ends at its `;`.
        if terminator == '{':
            end_line = _extract_balanced_block(source_lines, start_line, language="csharp")  # V52-O.11.E (was: start_line + 50)
        elif terminator == '=>':
            end_line = content_clean[
                :_csharp_expression_body_end(content_clean, m.end())
            ].count('\n') + 1
        else:  # ';' — a declaration with no body
            end_line = content_clean[:m.end()].count('\n') + 1
        body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
        enclosing = next(
            (c for c, cl, _ in sorted(class_decls, key=lambda d: d[1], reverse=True)
             if cl <= start_line), file_path.stem
        )
        is_async = bool(re.search(r'\basync\b', body[:200]))
        full_name = f"{ns}.{enclosing}.{mname}"
        signature = f"{mname}(...)"
        entities.append(CodeEntity(
            kind=KIND_FUNCTION, file_path_rel=relative_path,
            name=mname, full_name=full_name,
            body=body, signature=signature, doc="",
            start_line=start_line, end_line=end_line,
            is_async=is_async, project=helpers.project_name,
            deferred_embed=(
                lambda sig=signature, fb=body:
                helpers.embed_function(sig, fb, language="csharp")
            ),
        ))
        stats['functions'] += 1

        # ASP.NET route entries for HTTP-attributed methods.
        # v0.2.92: the attribute block is the one that STRUCTURALLY decorates
        # this declaration (see _csharp_attribute_block) — not whatever `[Http`
        # happened to fall inside a 5-line window. ``decl_pos`` is computed once
        # above, where the entity's own line numbers now come from it too.
        attr_text, attr_start = _csharp_attribute_block(content_clean, decl_pos)

        # Controller-level [Route("…")]: the first one declared BEFORE this
        # method's own attribute block. Slicing at `attr_start` (rather than at
        # `m.start()`, which sits AFTER the attributes) keeps a method's own
        # `[Route]` from being read as its controller's prefix and then joined
        # to itself.
        ctrl_route = ''
        base_route_m = route_attr_pattern.search(content_clean[:attr_start])
        if base_route_m:
            ctrl_route = '/' + base_route_m.group(1).strip('/')
        # A method-level [Route("…")] supplies the template when the verb
        # attribute carries none (`[HttpGet]` + `[Route("all")]`, and the
        # shared-bracket `[HttpGet, Route("all")]`), which is how ASP.NET
        # resolves it too.
        method_route_m = _CSHARP_METHOD_ROUTE_RE.search(attr_text)

        # One row per verb attribute: an action may legitimately declare more
        # than one (`[HttpGet]` + `[HttpPost]`), and emitting a single row for
        # it loses an endpoint the same way the lookback bug did.
        for verb_m in _CSHARP_HTTP_ATTR_RE.finditer(attr_text):
            http_method = verb_m.group(1).upper()
            template = verb_m.group(2)
            if template is None and method_route_m:
                template = method_route_m.group(1)
            # v0.2.92: NO template means NO template. The previous default
            # fabricated a path segment from the method name, so
            # ``[HttpPost]`` under ``[Route("api/items")]`` — which ASP.NET
            # serves at ``POST /api/items`` — was stored as
            # ``/api/items/add``: a route that does not exist, and the reason a
            # user searching the graph for the real endpoint found nothing.
            # Falling through to the shared join reproduces ASP.NET's own
            # template combination (controller template + empty action template
            # = the controller template; both empty = the application root),
            # and is the same rule ``join_route`` already documents for
            # ``APIRouter(prefix="/v1")`` + ``@router.get("")``.
            #
            # The fabrication was load-bearing until v0.2.92: two no-template
            # actions sharing a verb both resolve to ONE ``endpoint:method``
            # dedup identity, and pre-v0.2.92 the second silently overwrote the
            # first. The occurrence disambiguation that landed this cycle
            # covers it — VERIFIED, not assumed:
            # ``assign_duplicate_identity_suffixes`` groups on
            # ``(kind, identity_key)`` for EVERY entity kind including
            # ``KIND_API``, so the second row is keyed ``/api/items:POST#2``
            # and both are stored. Pinned by
            # ``tests/test_v0292_wp5_csharp_route_default.py``.
            #
            # ``route`` is a verbatim regex capture from between the quotes of
            # ``[HttpGet(" all ")]``, so it is normalised HERE (a C#-input
            # concern) rather than inside the shared join. The name is kept
            # (rather than folding ``template`` straight into the call) because
            # ``tests/test_v0292_shared_join_route.py`` pins this exact
            # expression as the thing a future editor must not drop; the alias
            # is what the pin reads.
            route = template
            full_route = join_route(ctrl_route, (route or "").strip())
            api_desc = f"C# ASP.NET {http_method} {full_route} → {ns}.{enclosing}.{mname}"
            # The handler edge points at the FUNCTION emitted just above; the
            # writer resolves ``_handler_full_name`` -> that function's UUID
            # (references={"handler": func_uuid} in the imperative extractor).
            # v0.2.82 (G1 task 2): the API embed is DEFERRED (SKIP/STAMP on a
            # metadata-only revision bump); the shared builder owns the
            # default-arg capture that pins api_desc.
            # v0.2.92: constructed by the ONE shared builder (see _shared).
            entities.append(build_api_entity(
                file_path_rel=relative_path,
                endpoint=full_route,
                method=http_method,
                description=api_desc,
                project=helpers.project_name,
                handler_full_name=full_name,
                embed=helpers.generate_embedding,
            ))
            stats.setdefault('apis', 0)
            stats['apis'] += 1

    # Cross-language interactions (writer replays with the module UUID).
    interactions: List[InteractionGroup] = []
    ix = _extract_external_calls(content_clean, imports, "csharp", relative_path)
    if ix:
        interactions.append(InteractionGroup(interactions=ix, language="C#"))

    return FileExtraction(
        module=module, entities=entities, interactions=interactions,
        imports=[], stats=stats,
    )


def analyze_csharp_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Thin shim: skip gates analyzer-side, then extract -> write."""
    return run_pure_extractor(
        ctx, file_path, repo_root, extract_csharp_file,
        {'modules': 0, 'classes': 0, 'functions': 0},
    )
