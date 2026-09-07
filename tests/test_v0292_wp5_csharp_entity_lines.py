# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-5 — the C# extractor's stored line ranges, bodies and row set.

WHY THIS EXISTS
---------------
``tests/test_v0292_csharp_route_attribution.py`` pins the ROUTE side of the C#
producer. Everything below is the ENTITY side, and it was pinned only by the
golden corpus — which had ratified the defects rather than caught them: all six
C# rows carried a skewed ``start_line``, one class stored ``end_line: 45`` for
a 44-line file, two rows were minted from a record declaration and a ``return
new`` statement, and the file's one generic method had no row at all.

THE MECHANISM (one cause, four symptoms). ``method_pattern`` opens with
``(?:public|…|\\s)+`` and continues with a return-type group
``(?:[\\w<>\\[\\]?]+\\s+)+`` that accepts ``[``, ``]`` and any word token. So
``match.start()`` is NOT the declaration: the leading group starts matching at
the whitespace after the PREVIOUS token, and a parenless attribute is swallowed
as if it were a type. ``_csharp_declaration_start`` — which existed, with a
docstring saying exactly this, and was used ONLY by the route path — is now
what the entity's own ``start_line`` comes from too. The same permissive
return-type group is why a statement and a type declaration could be read as
"modifiers + return type", and the same rigid name capture ``([\\w]+)\\s*\\(``
is why ``WrapAll<T>(`` was unreachable.

WHAT IS PINNED
--------------
* the ACT: ``start_line`` is the declaration line and ``body`` begins there,
  for a class, an attributed method, a method following another method, and a
  method below a multi-line block comment;
* ``end_line`` respects the DECLARATION TERMINATOR — a bodiless interface
  member ends on its own line rather than borrowing the next type's braces;
* generic methods produce a row; statements and type declarations do not;
* the LEAVE-ALONE: an ordinary method with a brace body is untouched; the
  keyword filter still rejects control-flow names; a class that legitimately
  starts at column 0 keeps its line.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

from vco_lib.codegraph_entities import KIND_CLASS, KIND_FUNCTION
from vco_lib.codegraph_lang.csharp import extract_csharp_file


class _Helpers:
    project_name = "CsLines"

    def embed_class(self, *_a: Any, **_k: Any) -> None:
        return None

    def embed_function(self, *_a: Any, **_k: Any) -> None:
        return None

    def generate_embedding(self, *_a: Any, **_k: Any) -> None:
        return None


def _extract(tmp_path: Path, source: str):
    target = tmp_path / "C.cs"
    target.write_text(source, encoding="utf-8")
    return extract_csharp_file(source, target, tmp_path, _Helpers())


def _ranges(fx, kind) -> Dict[str, Tuple[int, int]]:
    return {
        e.name: (e.start_line, e.end_line) for e in fx.entities if e.kind == kind
    }


def _first_body_lines(fx) -> Dict[str, str]:
    return {
        e.name: (e.body or "").split("\n")[0]
        for e in fx.entities
        if e.kind in (KIND_CLASS, KIND_FUNCTION)
    }


# The measured repro from the WP-5 brief, kept verbatim as a fixture: a
# multi-line block comment, a controller [Route], two parenless [HttpPost]
# actions, a `return new X(...)` statement and a generic method.
_REPRO = '''namespace Shop
{
    /* A multi-line
       block comment that
       spans four lines
       before the class. */
    [Route("api/items")]
    public class ItemsController
    {
        public Item Lookup(int id)
        {
            return new Item(id, "widget");
        }

        [HttpPost]
        public Item Add(Item item)
        {
            return item;
        }

        public List<T> WrapAll<T>(T single)
        {
            return new List<T> { single };
        }
    }
}
'''


def test_every_entity_starts_on_its_own_declaration(tmp_path) -> None:
    """THE act. Pre-fix: the class started on the comment's 2nd line, `Lookup`
    on the comment's last line, `Add` on `public Item Lookup(int id)`, and the
    `body` of each began with that wrong line."""
    fx = _extract(tmp_path, _REPRO)
    lines = _REPRO.split("\n")
    seen = 0
    for e in fx.entities:
        if e.kind not in (KIND_CLASS, KIND_FUNCTION):
            continue
        seen += 1
        assert e.start_line is not None and e.end_line is not None, (
            f"{e.name}: producer emitted a null line number"
        )
        assert e.name, "producer emitted an unnamed entity"
        decl = lines[e.start_line - 1]
        assert e.name in decl, (
            f"{e.name}: start_line {e.start_line} is {decl!r}"
        )
        assert (e.body or "").split("\n")[0] == decl
        assert e.end_line <= len(lines), (
            f"{e.name}: end_line {e.end_line} is past EOF ({len(lines)} lines)"
        )
        assert e.end_line >= e.start_line
    assert seen == 4, f"expected class + 3 methods, got {seen}"


def test_exact_line_ranges_of_the_repro(tmp_path) -> None:
    """The numbers themselves, so a future regression is legible."""
    fx = _extract(tmp_path, _REPRO)
    assert _ranges(fx, KIND_CLASS) == {"ItemsController": (8, 25)}
    assert _ranges(fx, KIND_FUNCTION) == {
        "Lookup": (10, 13),
        "Add": (16, 19),
        "WrapAll": (21, 24),
    }


def test_a_generic_method_produces_a_row(tmp_path) -> None:
    """`([\\w]+)\\s*\\(` could not reach the `(` of `WrapAll<T>(`, so EVERY C#
    generic method was missing from CodeFunction — while the class's `methods`
    list (a different regex) named it, so the graph disagreed with itself."""
    fx = _extract(tmp_path, _REPRO)
    fns = _ranges(fx, KIND_FUNCTION)
    assert "WrapAll" in fns
    controller = next(e for e in fx.entities if e.kind == KIND_CLASS)
    assert "WrapAll" in (controller.extras.get("methods") or []), (
        "the class methods list and the function rows must agree"
    )


def test_a_constrained_generic_method_produces_a_row(tmp_path) -> None:
    """The commonest real shape of the generic methods this release recovers:
    the constraints sit between the argument list and the body, so without a
    `where` clause in the pattern the terminator is unreachable and the row is
    still missing. Recovering only the UNCONSTRAINED form would have closed
    half the defect."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class Holder
    {
        public Task<List<T>> GetAllAsync<T>(int id) where T : class
        {
            return null;
        }

        public int Plain() { return 1; }
    }
}
''')
    fns = _ranges(fx, KIND_FUNCTION)
    assert set(fns) == {"GetAllAsync", "Plain"}
    assert fns["GetAllAsync"] == (5, 8)


def test_the_where_clause_group_cannot_eat_the_body(tmp_path) -> None:
    """LEAVE-ALONE with teeth: the constraint group is bounded by `[^{;]`, so a
    method WITHOUT constraints is unaffected and no method can swallow the one
    after it."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class Holder
    {
        public int First() { return 1; }
        public int Second() { return 2; }
    }
}
''')
    assert _ranges(fx, KIND_FUNCTION) == {"First": (5, 5), "Second": (6, 6)}


def test_a_type_declaration_is_not_a_method(tmp_path) -> None:
    """`public record Item(int Id, string Name);` minted a FUNCTION row named
    `Item` in the golden corpus."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public record Item(int Id, string Name);

    public class Holder
    {
        public int Size() { return 1; }
    }
}
''')
    assert set(_ranges(fx, KIND_FUNCTION)) == {"Size"}


def test_a_csharp12_primary_constructor_class_is_not_a_method(tmp_path) -> None:
    """`public class Foo(int x) { }` matches the method shape exactly."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class Holder(int seed)
    {
        public int Size() { return seed; }
    }
}
''')
    assert set(_ranges(fx, KIND_FUNCTION)) == {"Size"}


def test_a_statement_is_not_a_method(tmp_path) -> None:
    """`return new Item(...)` and `return Ok();` both minted rows."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class Holder
    {
        public Item Make(int id)
        {
            return new Item(id, "x");
        }

        public IActionResult Ping()
        {
            return Ok();
        }
    }
}
''')
    assert set(_ranges(fx, KIND_FUNCTION)) == {"Make", "Ping"}


def test_a_conversion_operator_is_not_a_method_named_after_its_target(tmp_path) -> None:
    """`implicit operator Money(...)` captured `Money` as a method name — the
    class docstring already claimed operator overloads are not captured."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class Money
    {
        public static implicit operator Money(decimal d) { return new Money(); }
        public int Cents() { return 0; }
    }
}
''')
    assert set(_ranges(fx, KIND_FUNCTION)) == {"Cents"}


def test_a_bodiless_interface_member_ends_on_its_own_line(tmp_path) -> None:
    """An interface member has no braces, so a brace scan from it latches onto
    the NEXT type's — the fixture's `IRepository.Find` took the record, the
    `[Route]` attribute and the controller header into its body."""
    source = '''namespace Shop
{
    public interface IRepository
    {
        Item Find(int id);
    }

    public class Holder
    {
        public int Size() { return 1; }
    }
}
'''
    fx = _extract(tmp_path, source)
    fns = _ranges(fx, KIND_FUNCTION)
    assert fns["Find"] == (5, 5)
    body = next(e for e in fx.entities if e.name == "Find").body
    assert body is not None and body.strip() == "Item Find(int id);"


def test_an_expression_bodied_member_ends_at_its_semicolon(tmp_path) -> None:
    source = '''namespace Shop
{
    public class Holder
    {
        public int Size() => 1 + 2;

        public int Other() { return 3; }
    }
}
'''
    fx = _extract(tmp_path, source)
    assert _ranges(fx, KIND_FUNCTION)["Size"] == (5, 5)


def test_an_expression_body_with_a_statement_lambda_is_not_cut_short(tmp_path) -> None:
    """The inner `;` is inside braces and must not terminate the member."""
    source = '''namespace Shop
{
    public class Holder
    {
        public int Size() => Items.Select(x => { var y = x; return y; }).Count();
    }
}
'''
    fx = _extract(tmp_path, source)
    assert _ranges(fx, KIND_FUNCTION)["Size"] == (5, 5)


def test_class_end_line_is_the_closing_brace_not_one_past_it(tmp_path) -> None:
    """The golden fixture stored `end_line: 45` for a 44-line file because the
    class loop used `start_line + len(class_lines)` while the function loop in
    the same file used `_extract_balanced_block`'s return directly."""
    source = '''namespace Shop
{
    public class Holder
    {
        public int Size() { return 1; }
    }
}
'''
    fx = _extract(tmp_path, source)
    lines = source.split("\n")
    start, end = _ranges(fx, KIND_CLASS)["Holder"]
    assert lines[end - 1].strip() == "}"
    assert end < len(lines)


# ═══════════════════════════════════════════════════════════════════════════
# LEAVE-ALONE
# ═══════════════════════════════════════════════════════════════════════════
_PLAIN = '''namespace Shop
{
    public class Holder
    {
        public int Size()
        {
            return 1;
        }
    }
}
'''


def test_a_plain_method_keeps_a_correct_range(tmp_path) -> None:
    """NOT a leave-alone, despite the section it sits in — measured against the
    pre-edit bytes this FAILS (start_line 4, the class's `{`). Even the
    simplest possible method was skewed, which is the point: the defect needed
    no attribute, no comment and no neighbour to appear. Kept here because it
    is the shape a reader will reach for when checking "is anything normal
    still normal", and its honest label matters more than its placement.
    """
    fx = _extract(tmp_path, _PLAIN)
    assert _ranges(fx, KIND_FUNCTION) == {"Size": (5, 8)}
    assert _first_body_lines(fx)["Size"] == "        public int Size()"


def test_control_flow_keywords_are_still_not_methods(tmp_path) -> None:
    fx = _extract(tmp_path, '''namespace Shop
{
    public class Holder
    {
        public int Size(int n)
        {
            if (n > 0) { return 1; }
            while (n > 0) { n--; }
            foreach (var x in All()) { }
            return 0;
        }
    }
}
''')
    assert set(_ranges(fx, KIND_FUNCTION)) == {"Size"}
