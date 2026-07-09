# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-O.11.F.2-JS — JS/TS class.methods correct attribution.

Pre-V52-O.11.F.2-JS (audit a79152, 2026-06-09): the JS/TS analyzer at
``analyze_code_graph.py:~2942`` inlined a single regex
``method_inside.finditer(class_body)`` with three correctness defects:

  1. Missed method shapes: ``static name()``, ``get/set``, ``*name()``
     (generators), ``#name()`` (private fields), ``static async``,
     ``static *``, ``async *``.
  2. Matched ANY ``name(args) {`` pattern over the whole class body
     including nested function-call statements inside method bodies
     (``cb(x) { ... }`` invoked inside a method body would be
     miscounted as a method of the outer class).
  3. Erroneously filtered ``constructor`` out (constructor IS a method
     that should be tracked).

V52-O.11.F.2-JS introduces ``_js_methods_for_class(content_clean,
class_name, source_lines)`` that scopes ``methods`` to declarations
inside the brace-balanced body of ``class <class_name> {...}``, walking
the body with depth tracking so only top-level method shapes are
captured.

Parallels V52-O.11.F (Rust) — same shape of bug, same shape of fix.
"""

from __future__ import annotations

from pathlib import Path

# P2f stage 2 (v0.2.76): `_js_methods_for_class` moved verbatim to
# vco_lib/codegraph_lang/javascript.py, which is import-safe (no
# weaviate-client / sys.path side effects) — the old isolated-importlib
# loader for the full analyzer script is no longer needed. Alias the module
# as ``acg`` so every assertion below stays byte-identical.
from vco_lib.codegraph_lang import javascript as acg


# ---------------------------------------------------------------------------
# Test 1 — shorthand methods
# ---------------------------------------------------------------------------


def test_shorthand_methods() -> None:
    """class Foo { a() {} b() {} } → methods on Foo = [a, b]."""
    content = """
class Foo {
    a() { return 1; }
    b() { return 2; }
}

function unrelated() {}
"""
    methods = acg._js_methods_for_class(content, "Foo", content.split("\n"))
    assert methods == ["a", "b"], (
        f"Expected [a, b] from class Foo; got {methods}. "
        f"unrelated() MUST NOT appear — V52-O.11.F.2-JS regression."
    )


# ---------------------------------------------------------------------------
# Test 2 — async methods
# ---------------------------------------------------------------------------


def test_async_methods() -> None:
    """class Foo { async fetchData() {} } picks up fetchData."""
    content = """
class Foo {
    async fetchData() { return await api(); }
    sync_method() {}
}
"""
    methods = acg._js_methods_for_class(content, "Foo", content.split("\n"))
    assert "fetchData" in methods
    assert "sync_method" in methods


# ---------------------------------------------------------------------------
# Test 3 — static methods (including static async)
# ---------------------------------------------------------------------------


def test_static_methods() -> None:
    """static and static async are captured by their method name (not 'static')."""
    content = """
class Foo {
    static create() {}
    static async loadFromDisk() {}
    instance_method() {}
}
"""
    methods = acg._js_methods_for_class(content, "Foo", content.split("\n"))
    assert "create" in methods, (
        f"Expected `create` from `static create()`; got {methods}. "
        f"The pattern is probably eating `static` as the method name."
    )
    assert "loadFromDisk" in methods, (
        f"Expected `loadFromDisk` from `static async loadFromDisk()`; "
        f"got {methods}."
    )
    assert "instance_method" in methods
    assert "static" not in methods  # the keyword itself must not leak as a method
    assert "async" not in methods


# ---------------------------------------------------------------------------
# Test 4 — getters and setters
# ---------------------------------------------------------------------------


def test_get_set_methods() -> None:
    """get/set property accessors are methods."""
    content = """
class Foo {
    get value() { return this._v; }
    set value(v) { this._v = v; }
    other_method() {}
}
"""
    methods = acg._js_methods_for_class(content, "Foo", content.split("\n"))
    assert "value" in methods, (
        f"Expected `value` from get/set; got {methods}. The pattern "
        f"likely captured `get`/`set` as the method name."
    )
    assert "other_method" in methods
    assert "get" not in methods
    assert "set" not in methods


# ---------------------------------------------------------------------------
# Test 5 — generators (sync + async)
# ---------------------------------------------------------------------------


def test_generator_methods() -> None:
    """*name() and async *name() are methods."""
    content = """
class Foo {
    *iter() { yield 1; }
    async *asyncIter() { yield await 1; }
    plain() {}
}
"""
    methods = acg._js_methods_for_class(content, "Foo", content.split("\n"))
    assert "iter" in methods, (
        f"Expected `iter` from `*iter()`; got {methods}."
    )
    assert "asyncIter" in methods, (
        f"Expected `asyncIter` from `async *asyncIter()`; got {methods}."
    )
    assert "plain" in methods


# ---------------------------------------------------------------------------
# Test 6 — private fields (#name)
# ---------------------------------------------------------------------------


def test_private_field_methods() -> None:
    """#privateMethod() is a method — leading # is preserved in name."""
    content = """
class Foo {
    #secret() { return 42; }
    async #fetchPrivate() {}
    publicMethod() {}
}
"""
    methods = acg._js_methods_for_class(content, "Foo", content.split("\n"))
    assert "#secret" in methods, (
        f"Expected `#secret` from private field; got {methods}."
    )
    assert "#fetchPrivate" in methods
    assert "publicMethod" in methods


# ---------------------------------------------------------------------------
# Test 7 — extends does NOT bring in base-class methods
# ---------------------------------------------------------------------------


def test_extends_does_not_inherit_base_methods() -> None:
    """class Foo extends Bar — only Foo's OWN methods are listed, not Bar's."""
    content = """
class Bar {
    fromBar() {}
    baseUtil() {}
}

class Foo extends Bar {
    fromFoo() {}
    override() {}
}
"""
    methods_foo = acg._js_methods_for_class(content, "Foo", content.split("\n"))
    methods_bar = acg._js_methods_for_class(content, "Bar", content.split("\n"))

    assert sorted(methods_foo) == ["fromFoo", "override"], (
        f"Foo should only carry its own methods, not Bar's. "
        f"Got {methods_foo}. Pre-V52-O.11.F.2-JS the file-wide regex "
        f"would have leaked Bar's methods into Foo."
    )
    assert sorted(methods_bar) == ["baseUtil", "fromBar"]


# ---------------------------------------------------------------------------
# Test 8 — multiple classes in same file — no cross-contamination
# ---------------------------------------------------------------------------


def test_multiple_classes_no_cross_contamination() -> None:
    """The audit reproduction case: file with many classes and free
    functions — each class gets only its own methods, no leakage."""
    content = """
class A {
    a_method_1() {}
    a_method_2() {}
}

class B {
    b_method_1() {}
}

class C {
    c_method_1() {}
    c_method_2() {}
    c_method_3() {}
}

function free_fn_1() {}
function free_fn_2() {}
async function free_fn_3() {}
"""
    methods_a = acg._js_methods_for_class(content, "A", content.split("\n"))
    methods_b = acg._js_methods_for_class(content, "B", content.split("\n"))
    methods_c = acg._js_methods_for_class(content, "C", content.split("\n"))

    assert sorted(methods_a) == ["a_method_1", "a_method_2"]
    assert sorted(methods_b) == ["b_method_1"]
    assert sorted(methods_c) == ["c_method_1", "c_method_2", "c_method_3"]

    # Free fns must not leak.
    for free_fn in ["free_fn_1", "free_fn_2", "free_fn_3"]:
        assert free_fn not in methods_a, (
            f"V52-O.11.F.2-JS regression: free fn {free_fn!r} leaked "
            f"into class A's methods."
        )
        assert free_fn not in methods_b
        assert free_fn not in methods_c

    # Cross-class contamination check.
    for m in ["a_method_1", "a_method_2"]:
        assert m not in methods_b
        assert m not in methods_c


# ---------------------------------------------------------------------------
# Test 9 — class with no methods (data-only) returns []
# ---------------------------------------------------------------------------


def test_data_only_class_returns_empty() -> None:
    """class with only field initializers / no method shorthand → []."""
    content = """
class DataOnly {
    field1 = 1;
    field2 = "hello";
}

function unrelated() {}
"""
    methods = acg._js_methods_for_class(content, "DataOnly", content.split("\n"))
    # Note: `field1 = 1;` is a class-field declaration, not a method.
    # The pattern requires `name(...)`, so fields don't match.
    assert methods == [], (
        f"Class with only fields should yield []; got {methods}."
    )


# ---------------------------------------------------------------------------
# Test 10 — constructor IS a method
# ---------------------------------------------------------------------------


def test_constructor_is_a_method() -> None:
    """V52-O.11.F.2-JS deliberately keeps `constructor` (the previous
    inline regex erroneously filtered it out via the keyword skip list).
    """
    content = """
class Foo {
    constructor(x) { this.x = x; }
    bar() {}
}
"""
    methods = acg._js_methods_for_class(content, "Foo", content.split("\n"))
    assert "constructor" in methods, (
        "V52-O.11.F.2-JS regression: `constructor` should be tracked as "
        "a method (the previous inline regex skipped it via keyword_skip; "
        "the new helper deliberately keeps it)."
    )
    assert "bar" in methods


# ---------------------------------------------------------------------------
# Test 11 — nested call statements inside method bodies don't leak
# ---------------------------------------------------------------------------


def test_nested_function_calls_in_method_bodies_excluded() -> None:
    """Pre-V52-O.11.F.2-JS the inline `name(args) {` regex would match
    nested call statements like `cb(x) { ... }` inside method bodies.
    The new helper's depth tracking ensures only top-level (depth=0 of
    class body) method shapes are captured.
    """
    content = """
class Foo {
    realMethod() {
        helper(arg) { return arg + 1; }
        otherFn(x) { return x * 2; }
        return 42;
    }
    anotherReal() {}
}
"""
    methods = acg._js_methods_for_class(content, "Foo", content.split("\n"))
    assert "realMethod" in methods
    assert "anotherReal" in methods
    # `helper` and `otherFn` are NESTED inside realMethod's body — must
    # not be counted as class methods.
    assert "helper" not in methods, (
        f"V52-O.11.F.2-JS regression: nested function-like statement "
        f"`helper(arg) {{}}` inside a method body should NOT be counted "
        f"as a class method. Got {methods}."
    )
    assert "otherFn" not in methods


# ---------------------------------------------------------------------------
# Test 12 — exact name match (Foo doesn't match FooBar)
# ---------------------------------------------------------------------------


def test_exact_class_name_match() -> None:
    """class FooBar should NOT match when searching for class_name='Foo'."""
    content = """
class Foo {
    foo_method() {}
}

class FooBar {
    foobar_method() {}
}
"""
    methods_foo = acg._js_methods_for_class(content, "Foo", content.split("\n"))
    methods_foobar = acg._js_methods_for_class(content, "FooBar", content.split("\n"))

    assert methods_foo == ["foo_method"], (
        f"Foo should only match `class Foo` exactly, not `class FooBar`. "
        f"Got {methods_foo}."
    )
    assert methods_foobar == ["foobar_method"]
    assert "foobar_method" not in methods_foo


# ---------------------------------------------------------------------------
# Test 13 — export / export default class
# ---------------------------------------------------------------------------


def test_export_default_class() -> None:
    """`export class Foo` and `export default class Foo` are recognized."""
    content = """
export class A {
    a_method() {}
}

export default class B {
    b_method() {}
}
"""
    methods_a = acg._js_methods_for_class(content, "A", content.split("\n"))
    methods_b = acg._js_methods_for_class(content, "B", content.split("\n"))

    assert methods_a == ["a_method"]
    assert methods_b == ["b_method"]


# ---------------------------------------------------------------------------
# Test 14 — V52-O.11.F.2-JS regression: no inline `method_inside.finditer`
# ---------------------------------------------------------------------------


def test_no_inline_method_finditer_in_js_class_loop() -> None:
    """Regression test: prevents a future PR from re-introducing the
    pre-V52-O.11.F.2-JS inline regex that under-covered method shapes
    and matched nested call statements.

    The new code uses ``_js_methods_for_class(...)`` instead.

    P2f stage 2 (v0.2.76): the JS extractor moved verbatim to
    vco_lib/codegraph_lang/javascript.py — the source scan follows it there
    (assertions unchanged).
    """
    src = Path(acg.__file__).read_text()

    # Anchor on the STABLE per-class scoping call itself — this is the exact
    # thing V52-O.11.F.2-JS introduced and this test guards. (v0.2.73: the
    # earlier anchor was the JS `embed_class(...)` literal + a 1400-char lookback
    # window to the loop header; FIX-B2's embed-hoist wrapped that embed in a
    # `_deferred_embed` lambda and inserted scaffolding, pushing the loop header
    # outside the fixed window. Anchoring on `_js_methods_for_class(...)` is
    # refactor-robust — it's the call that must exist and the regex-ban must hold
    # around it, regardless of how the downstream embed is dispatched.)
    js_call_anchor = '_js_methods_for_class(content_clean, cname, source_lines)'
    anchor_pos = src.find(js_call_anchor)
    assert anchor_pos >= 0, (
        "V52-O.11.F.2-JS regression: the JS class loop must call "
        f"``_js_methods_for_class`` for per-class method scoping. Looked for: "
        f"{js_call_anchor!r} — has it changed?"
    )

    # The JS class-iteration loop header must precede the scoping call (confirms
    # the anchor is inside the JS class loop, not some other reference). Search
    # the whole prefix up to the anchor — refactor-size-independent.
    prefix = src[:anchor_pos]
    assert "for cname, (start_line, base_class) in class_info.items():" in prefix, (
        "JS `_js_methods_for_class` call isn't preceded by the JS "
        "class-iteration loop header — file shape has changed unexpectedly."
    )

    # The banned inline regex that V52-O.11.F.2-JS supersedes must appear
    # NOWHERE in the file (it under-covered method shapes + matched nested
    # call statements). Checking the whole file is strictly stronger than the
    # old windowed check and immune to embed-dispatch refactors.
    assert "method_inside.finditer(class_body)" not in src, (
        "V52-O.11.F.2-JS regression: analyze_code_graph.py reintroduced "
        "``method_inside.finditer(class_body)`` which under-covered method "
        "shapes (no static / get/set / generators / private / constructor) "
        "and matched nested function-call statements. Use "
        "``_js_methods_for_class(content_clean, cname, source_lines)`` instead."
    )
