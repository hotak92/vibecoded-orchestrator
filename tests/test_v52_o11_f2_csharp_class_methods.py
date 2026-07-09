# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-O.11.F.2-CSHARP — C# class.methods correct attribution.

Pre-V52-O.11.F.2-CSHARP (audit a79152, 2026-06-09): the C# analyzer at
``analyze_code_graph.py:2540`` (pre-fix line) did:

    methods = [m.group(1) for m in method_pattern.finditer(content_clean)]

This iterated ``method_pattern`` over the WHOLE file's ``content_clean``
inside the per-class loop, attributing EVERY method in the file to EVERY
class. Audit reproduced: a 50-method file with 3 classes produced 150
incorrect method attributions, drowning real signal in noise.

V52-O.11.F.2-CSHARP introduces ``_csharp_methods_for_class(content_clean,
class_name, source_lines)`` that scopes ``methods`` to declarations
inside the lexical body of ``class``, ``struct``, ``record``, or
``interface`` named ``class_name``. C# uses lexical scoping inside braces
(like Java) — different from Rust's separate ``impl`` blocks.

Mirrors V52-O.11.F (Rust canonical, ``_rust_methods_for_struct``) and
V52-O.11.F.2-GO (``_go_methods_for_struct``).
"""

from __future__ import annotations

from pathlib import Path

# P2f stage 2 (v0.2.76): `_csharp_methods_for_class` moved verbatim to
# vco_lib/codegraph_lang/csharp.py, which is import-safe (no weaviate-client
# / sys.path side effects) — the old isolated-importlib loader for the full
# analyzer script is no longer needed. Alias the module as ``acg`` so every
# assertion below stays byte-identical.
from vco_lib.codegraph_lang import csharp as acg


# ---------------------------------------------------------------------------
# Test 1 — public/private/protected/internal access modifiers
# ---------------------------------------------------------------------------


def test_all_access_modifiers_captured() -> None:
    """The four primary C# access modifiers plus the two compound ones
    (``protected internal``, ``private protected``) all surface their
    methods in the methods list."""
    content = """
public class Modifiers {
    public void PubMethod() { }
    private void PrivMethod() { }
    protected void ProtMethod() { }
    internal void IntMethod() { }
    protected internal void ProtIntMethod() { }
    private protected void PrivProtMethod() { }
}
"""
    methods = acg._csharp_methods_for_class(
        content, "Modifiers", content.split("\n")
    )
    expected = {
        "PubMethod", "PrivMethod", "ProtMethod",
        "IntMethod", "ProtIntMethod", "PrivProtMethod",
    }
    assert set(methods) == expected, (
        f"Expected all 6 access-modifier methods; got {set(methods)}. "
        f"Some modifier variant is being missed by the inner method pattern."
    )


# ---------------------------------------------------------------------------
# Test 2 — async, virtual, override, static, abstract, sealed
# ---------------------------------------------------------------------------


def test_method_modifiers_async_virtual_override() -> None:
    """async, virtual, override, static, abstract, sealed modifiers all
    captured. These compose with access modifiers."""
    content = """
public class Mixed {
    public async Task<int> FooAsync() { return 1; }
    public virtual void VirtMethod() { }
    public override string ToString() => "foo";
    public static void StaticMethod() { }
    public abstract void AbsMethod();
    public sealed override void SealedOver() { }
}
"""
    methods = acg._csharp_methods_for_class(
        content, "Mixed", content.split("\n")
    )
    expected = {
        "FooAsync", "VirtMethod", "ToString",
        "StaticMethod", "AbsMethod", "SealedOver",
    }
    assert set(methods) == expected


# ---------------------------------------------------------------------------
# Test 3 — auto-property `{ get; set; }`
# ---------------------------------------------------------------------------


def test_auto_properties_captured_as_methods() -> None:
    """C# auto-properties (``public T Name { get; set; }``) are surfaced
    in the methods list. Task brief explicitly calls this out — mirrors
    Python's @property handling and aligns with CLR's ``get_Name``/
    ``set_Name`` synthesized methods."""
    content = """
public class WithProps {
    public int Foo { get; set; }
    public string Bar { get; }
    public double Baz { get; init; }
    private List<int> Internal { get; set; }
}
"""
    methods = acg._csharp_methods_for_class(
        content, "WithProps", content.split("\n")
    )
    expected = {"Foo", "Bar", "Baz", "Internal"}
    assert set(methods) == expected


# ---------------------------------------------------------------------------
# Test 4 — indexer (``this[...]``) normalized to ``Item``
# ---------------------------------------------------------------------------


def test_indexer_surfaces_as_Item() -> None:
    """C# indexer ``public T this[int i] { get; set; }`` is recorded as
    ``Item`` per CLR convention (the generated methods are ``get_Item``/
    ``set_Item``)."""
    content = """
public class WithIndexer {
    public int this[int i] {
        get { return 0; }
        set { }
    }
    public void RegularMethod() { }
}
"""
    methods = acg._csharp_methods_for_class(
        content, "WithIndexer", content.split("\n")
    )
    assert "Item" in methods, (
        f"Indexer should surface as 'Item' in methods; got {methods}"
    )
    assert "RegularMethod" in methods
    # The literal `this` MUST NOT appear (it's a keyword, not a method
    # name).
    assert "this" not in methods


# ---------------------------------------------------------------------------
# Test 5 — generic class with constraints + generic method
# ---------------------------------------------------------------------------


def test_generic_class_with_where_constraints() -> None:
    """Generic class with ``where T : new()`` constraints + generic method
    with its own ``where U : T, new()`` constraint. The constraint
    parentheses (``new()``) must NOT be mis-matched as a method named
    ``new`` and the method-body's ``return new U()`` must NOT register
    ``U`` as a method."""
    content = """
public class GenContainer<T> where T : new() {
    public T Make<U>() where U : T, new() {
        return new U();
    }
    public T Item { get; private set; }
}
"""
    methods = acg._csharp_methods_for_class(
        content, "GenContainer", content.split("\n")
    )
    assert "Make" in methods
    assert "Item" in methods
    # Critical regression guards
    assert "new" not in methods, (
        "V52-O.11.F.2-CSHARP regression: the `where U : T, new()` "
        "constraint must NOT register a method named 'new'."
    )
    assert "U" not in methods, (
        "V52-O.11.F.2-CSHARP regression: the body's `return new U()` "
        "statement must NOT register a method named 'U'."
    )


# ---------------------------------------------------------------------------
# Test 6 — partial classes (split declaration) aggregate methods
# ---------------------------------------------------------------------------


def test_partial_classes_aggregate_methods() -> None:
    """Two ``partial class Foo { }`` blocks → methods is the union of
    both bodies. Mirrors Rust's multi-``impl`` behavior."""
    content = """
public partial class Splitter {
    public void Part1Method() { }
    public int Part1Prop { get; set; }
}

public partial class Splitter {
    public void Part2Method() { }
}
"""
    methods = acg._csharp_methods_for_class(
        content, "Splitter", content.split("\n")
    )
    expected = {"Part1Method", "Part1Prop", "Part2Method"}
    assert set(methods) == expected


# ---------------------------------------------------------------------------
# Test 7 — nested types (inner class members appear in inner's methods)
# ---------------------------------------------------------------------------


def test_nested_class_inner_members_in_inner_methods() -> None:
    """A nested class ``Inner`` inside ``Outer`` — when we ask for
    ``Inner``'s methods, we get Inner's body members. The outer class's
    methods are documented to include the nested class's members (a
    minor over-count vs full lexical scoping; matches Java's behavior
    and is acceptable until tree-sitter rewrite)."""
    content = """
public class Outer {
    public void OuterMethod() { }

    public class Inner {
        public void InnerMethod() { }
        public int InnerProp { get; set; }
    }
}
"""
    methods_inner = acg._csharp_methods_for_class(
        content, "Inner", content.split("\n")
    )
    assert "InnerMethod" in methods_inner
    assert "InnerProp" in methods_inner


# ---------------------------------------------------------------------------
# Test 8 — methods on DIFFERENT classes are NOT mixed
# ---------------------------------------------------------------------------


def test_different_classes_have_distinct_methods() -> None:
    """The audit a79152 reproduction case: file with 3 classes + many
    methods, each class gets only its own methods."""
    content = """
public class A {
    public void a_method_1() { }
    public void a_method_2() { }
}

public class B {
    public void b_method_1() { }
}

public class C {
    public void c_method_1() { }
    public void c_method_2() { }
    public void c_method_3() { }
}
"""
    methods_a = acg._csharp_methods_for_class(content, "A", content.split("\n"))
    methods_b = acg._csharp_methods_for_class(content, "B", content.split("\n"))
    methods_c = acg._csharp_methods_for_class(content, "C", content.split("\n"))

    assert sorted(methods_a) == ["a_method_1", "a_method_2"]
    assert sorted(methods_b) == ["b_method_1"]
    assert sorted(methods_c) == ["c_method_1", "c_method_2", "c_method_3"]

    # No method from another class leaks. This is the V52-O.11.F.2-CSHARP
    # central guarantee.
    for m in ["a_method_1", "a_method_2"]:
        assert m not in methods_b, (
            f"V52-O.11.F.2-CSHARP regression: A's method {m!r} leaked "
            f"into class B's methods. Pre-fix behavior was every method "
            f"attributed to every class (50 fn × 3 cls = 150 attributions)."
        )
        assert m not in methods_c
    for m in ["b_method_1"]:
        assert m not in methods_a
        assert m not in methods_c
    for m in ["c_method_1", "c_method_2", "c_method_3"]:
        assert m not in methods_a
        assert m not in methods_b


# ---------------------------------------------------------------------------
# Test 9 — record with positional params + body members
# ---------------------------------------------------------------------------


def test_record_with_body_members() -> None:
    """``record Foo(int X, int Y)`` with an explicit body — positional
    parameters generate auto-properties at the CLR level, but we don't
    try to extract them from the primary-constructor signature. Only
    body-declared methods/properties are captured."""
    content = """
public record Point(int X, int Y) {
    public int Sum() => X + Y;
    public double Magnitude { get; init; }
}
"""
    methods = acg._csharp_methods_for_class(
        content, "Point", content.split("\n")
    )
    assert "Sum" in methods
    assert "Magnitude" in methods
    # Positional params X, Y are NOT extracted from the
    # primary-constructor signature (documented limitation).


# ---------------------------------------------------------------------------
# Test 10 — interface members (no access modifier)
# ---------------------------------------------------------------------------


def test_interface_members_captured_without_modifier() -> None:
    """C# interface members have no explicit access modifier (pre-C# 8).
    The method-pattern's modifier section is optional precisely so
    interface methods are captured."""
    content = """
public interface IFoo {
    void DoStuff();
    Task<int> GetAsync();
    int Count { get; }
}
"""
    methods = acg._csharp_methods_for_class(
        content, "IFoo", content.split("\n")
    )
    expected = {"DoStuff", "GetAsync", "Count"}
    assert set(methods) == expected


# ---------------------------------------------------------------------------
# Test 11 — struct with mixed methods and properties
# ---------------------------------------------------------------------------


def test_struct_methods_and_properties() -> None:
    """C# structs use the same lexical-scoping model as classes."""
    content = """
public struct Vector {
    public double X { get; set; }
    public double Y { get; set; }
    public double Magnitude() { return 0.0; }
    public Vector Normalize() => this;
}
"""
    methods = acg._csharp_methods_for_class(
        content, "Vector", content.split("\n")
    )
    expected = {"X", "Y", "Magnitude", "Normalize"}
    assert set(methods) == expected


# ---------------------------------------------------------------------------
# Test 12 — empty class returns []
# ---------------------------------------------------------------------------


def test_empty_class_returns_empty_list() -> None:
    """A class with no body content → methods = []."""
    content = """
public class EmptyData { }

public class HasMethod {
    public void Method() { }
}
"""
    methods = acg._csharp_methods_for_class(
        content, "EmptyData", content.split("\n")
    )
    assert methods == [], (
        f"Empty class should return []; got {methods}. "
        f"Probable cause: regex matched something from a neighbouring "
        f"class — body-extent extraction is broken."
    )


# ---------------------------------------------------------------------------
# Test 13 — nested generics (e.g. IList<KeyValuePair<string, object>>)
# ---------------------------------------------------------------------------


def test_nested_generic_return_types() -> None:
    """Methods/properties with nested generic return types (2-3 levels
    deep) are correctly captured. Common in real C# code (LINQ + EF
    Core)."""
    content = """
public class DataAccess {
    public Dictionary<string, int> Counters { get; set; }
    public IList<KeyValuePair<string, object>> Items() { return null; }
    public Task<Dictionary<int, List<string>>> FetchAsync() { return null; }
}
"""
    methods = acg._csharp_methods_for_class(
        content, "DataAccess", content.split("\n")
    )
    assert "Counters" in methods
    assert "Items" in methods
    assert "FetchAsync" in methods


# ---------------------------------------------------------------------------
# Test 14 — inheritance + interface implementation
# ---------------------------------------------------------------------------


def test_class_with_inheritance_clause() -> None:
    """A class with ``: BaseClass, IInterface<T>`` inheritance — the
    inheritance clause must not break body extraction."""
    content = """
public class Derived : BaseClass, IComparable<Derived> {
    public override int CompareTo(Derived other) { return 0; }
}
"""
    methods = acg._csharp_methods_for_class(
        content, "Derived", content.split("\n")
    )
    assert methods == ["CompareTo"]


# ---------------------------------------------------------------------------
# Test 15 — strict-prefix name disambiguation (Foo vs Foo2)
# ---------------------------------------------------------------------------


def test_strict_prefix_class_names_disambiguated() -> None:
    """Class ``Foo`` and ``Foo2`` are distinct — looking up methods for
    ``Foo`` must NOT include ``Foo2``'s methods. Word-boundary in the
    class-header regex closes this hole."""
    content = """
public class Foo {
    public void FooMethod() { }
}

public class Foo2 {
    public void Foo2Method() { }
}
"""
    methods_foo = acg._csharp_methods_for_class(
        content, "Foo", content.split("\n")
    )
    methods_foo2 = acg._csharp_methods_for_class(
        content, "Foo2", content.split("\n")
    )
    assert methods_foo == ["FooMethod"], (
        f"V52-O.11.F.2-CSHARP regression: looking up 'Foo' methods "
        f"returned {methods_foo}. The class-header regex must use "
        f"a word boundary after the escaped class name to prevent "
        f"matching prefix-supersets like 'Foo2'."
    )
    assert methods_foo2 == ["Foo2Method"]


# ---------------------------------------------------------------------------
# Test 16 — V52-O.11.F.2-CSHARP regression: no method_pattern.finditer
# over whole file in the C# class loop
# ---------------------------------------------------------------------------


def test_no_unconditional_method_finditer_in_csharp_class_loop() -> None:
    """Regression test: prevents a future PR from re-introducing the
    pre-V52-O.11.F.2-CSHARP line that iterated ``method_pattern`` over
    the WHOLE file's content_clean inside the C# per-class loop.

    Scans the C# analyzer's class extraction loop body for the broken
    pattern. The new code uses ``_csharp_methods_for_class(...)`` instead.

    P2f stage 2 (v0.2.76): the C# extractor moved verbatim to
    vco_lib/codegraph_lang/csharp.py — the source scan follows it there
    (the embed anchor gained the mechanical ``ctx.`` prefix in the move;
    the loop boundary indent went 8 -> 4 after the method ->
    free-function dedent; assertions unchanged).
    """
    src = Path(acg.__file__).read_text()

    # Locate the C#-specific class loop. The signature line
    # ``signature = f"class {cname}"`` is shared with several languages
    # (Java, JS/TS), so we anchor on a more specific marker: the
    # ``ctx.embed_class(..., language="csharp")`` call that follows the
    # methods extraction inside the C# loop body.
    csharp_anchor = 'ctx.embed_class(signature, class_body, methods=methods[:10], language="csharp")'
    anchor_pos = src.find(csharp_anchor)
    assert anchor_pos >= 0, (
        f"Could not locate C# class-loop anchor in codegraph_lang/csharp.py — "
        f"has it changed? Looked for: {csharp_anchor!r}"
    )

    # Window = the C# CLASS loop body ONLY. Start 1500 chars BEFORE the anchor
    # (covers the `for cname, start_line in class_info.items():` header + the
    # V52-O.11.F.2-CSHARP comment block + the `_csharp_methods_for_class(...)`
    # call). END at the NEXT `for ` after the anchor — the class loop's real
    # terminator is the METHOD extraction loop
    # `for m in method_pattern.finditer(content_clean):`, which legitimately
    # iterates all methods. A fixed `+200`-char window used to spill into that
    # adjacent loop (the class store block shrank under the P2f CodeEntity
    # refactor) and false-positive on its legitimate
    # `method_pattern.finditer(content_clean)` (see
    # knowledge/concepts/test-regex-anchoring-fragility-2026-06-10.md); binding
    # to the loop boundary keeps the guard scoped to the class loop body.
    window_start = max(0, anchor_pos - 1500)
    next_for = src.find("\n    for ", anchor_pos)
    window_end = next_for if next_for != -1 else min(len(src), anchor_pos + 200)
    window = src[window_start:window_end]

    # Sanity: window must contain the C# class-loop's `for cname` header.
    assert "for cname, start_line in class_info.items():" in window, (
        "C# class-loop anchor isn't inside a class-iteration loop — "
        "file shape has changed unexpectedly."
    )

    # Strip Python comment lines from the window before scanning — the
    # V52-O.11.F.2-CSHARP comment block I appended at the callsite
    # literally NAMES the pattern it replaced ("Pre-V52-O.11.F.2 this
    # line ran `method_pattern.finditer(content_clean)`...") which would
    # false-positive a naive substring scan.
    window_code_only = "\n".join(
        line for line in window.split("\n")
        if not line.lstrip().startswith("#")
    )
    assert "method_pattern.finditer(content_clean)" not in window_code_only, (
        "V52-O.11.F.2-CSHARP regression: the C# class loop body contains "
        "``method_pattern.finditer(content_clean)`` which would attribute "
        "EVERY method in the file to every class. Use "
        "``_csharp_methods_for_class(content_clean, cname, source_lines)`` "
        "instead."
    )
    assert "_csharp_methods_for_class" in window, (
        "V52-O.11.F.2-CSHARP regression: the C# class loop must call "
        "``_csharp_methods_for_class`` for per-class method scoping."
    )
