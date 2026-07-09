# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-O.11.F.2-GO — Go struct.methods correct attribution.

Pre-V52-O.11.F.2-GO (audit a79152, 2026-06-09): the Go analyzer at
``analyze_code_graph.py:3438`` did:

    methods = [m.group(1) for m in func_pattern.finditer(content_clean)]

This iterated ``func_pattern`` over the WHOLE file's ``content_clean``
inside the per-struct loop, attributing EVERY function in the file to
EVERY struct (the same bug as Rust's V52-O.11.F, applied to the Go
parser). Audit reproduced: a 50-fn Go file with 3 structs produced
150 incorrect method attributions, drowning the real ``methods`` signal
in noise for the ``query_code_structure(methods, StructName)`` MCP path.

V52-O.11.F.2-GO introduces ``_go_methods_for_struct(content_clean,
struct_name, source_lines)`` that scopes ``methods`` to functions whose
Go receiver matches ``struct_name``. Go uses receiver-based method
declaration (NOT ``impl`` blocks like Rust):

    func (recv *Foo) MethodName(args...) ReturnType { ... }
    func (recv  Foo) MethodName(args...) ReturnType { ... }

Both pointer and value receivers count as methods on the same type.
"""

from __future__ import annotations

from pathlib import Path

# P2f stage 2 (v0.2.76): `_go_methods_for_struct` moved verbatim to
# vco_lib/codegraph_lang/go.py, which is import-safe (no weaviate-client /
# sys.path side effects) — the old isolated-importlib loader for the full
# analyzer script is no longer needed. Alias the module as ``acg`` so every
# assertion below stays byte-identical.
from vco_lib.codegraph_lang import go as acg


# ---------------------------------------------------------------------------
# Test 1 — pointer receiver
# ---------------------------------------------------------------------------


def test_pointer_receiver() -> None:
    """func (f *Foo) Bar() → methods on Foo = [Bar]."""
    content = """
package main

type Foo struct {
    x int
}

func (f *Foo) Bar() int {
    return f.x
}

func unrelatedFreeFunction() {}
"""
    methods = acg._go_methods_for_struct(
        content, "Foo", content.split("\n")
    )
    assert methods == ["Bar"], (
        f"Expected [Bar] from pointer-receiver method on Foo; got {methods}. "
        f"unrelatedFreeFunction MUST NOT appear — V52-O.11.F.2-GO regression."
    )


# ---------------------------------------------------------------------------
# Test 2 — value receiver
# ---------------------------------------------------------------------------


def test_value_receiver() -> None:
    """func (f Foo) Baz() → methods on Foo = [Baz]."""
    content = """
package main

type Foo struct{}

func (f Foo) Baz() string {
    return "baz"
}
"""
    methods = acg._go_methods_for_struct(
        content, "Foo", content.split("\n")
    )
    assert methods == ["Baz"]


# ---------------------------------------------------------------------------
# Test 3 — both pointer and value receivers in the same file
# ---------------------------------------------------------------------------


def test_pointer_and_value_receivers_combined() -> None:
    """Mixing pointer + value receivers on the same struct → both methods
    appear in the methods list."""
    content = """
package main

type Foo struct{}

func (f *Foo) PointerMethod() {}
func (f  Foo) ValueMethod() {}
"""
    methods = acg._go_methods_for_struct(
        content, "Foo", content.split("\n")
    )
    assert "PointerMethod" in methods
    assert "ValueMethod" in methods
    assert len(methods) == 2


# ---------------------------------------------------------------------------
# Test 4 — methods on DIFFERENT structs are NOT mixed
# ---------------------------------------------------------------------------


def test_different_structs_have_distinct_methods() -> None:
    """The audit reproduction case (Go variant): file with 3 structs +
    many fns, each struct gets only its own methods."""
    content = """
package main

type A struct{}
type B struct{}
type C struct{}

func (a *A) AMethod1() {}
func (a *A) AMethod2() {}

func (b B) BMethod1() {}

func (c *C) CMethod1() {}
func (c *C) CMethod2() {}
func (c  C) CMethod3() {}

func FreeFn1() {}
func FreeFn2() {}
func FreeFn3() {}
"""
    methods_a = acg._go_methods_for_struct(content, "A", content.split("\n"))
    methods_b = acg._go_methods_for_struct(content, "B", content.split("\n"))
    methods_c = acg._go_methods_for_struct(content, "C", content.split("\n"))

    assert sorted(methods_a) == ["AMethod1", "AMethod2"]
    assert sorted(methods_b) == ["BMethod1"]
    assert sorted(methods_c) == ["CMethod1", "CMethod2", "CMethod3"]

    # No method from another struct or from free fns leaks into any struct.
    for free_fn in ["FreeFn1", "FreeFn2", "FreeFn3"]:
        assert free_fn not in methods_a, (
            f"V52-O.11.F.2-GO regression: free fn {free_fn!r} leaked into "
            f"struct A's methods. Pre-fix behavior was every fn attributed "
            f"to every struct."
        )
        assert free_fn not in methods_b
        assert free_fn not in methods_c

    # Cross-struct contamination check
    for m in ["AMethod1", "AMethod2"]:
        assert m not in methods_b
        assert m not in methods_c


# ---------------------------------------------------------------------------
# Test 5 — free (receiver-less) function does NOT appear in any struct
# ---------------------------------------------------------------------------


def test_free_function_excluded_from_struct_methods() -> None:
    """``func Free()`` (no receiver) MUST NOT appear in any struct's
    methods. Pre-V52-O.11.F.2-GO this would leak into every struct."""
    content = """
package main

type Foo struct{}

func (f *Foo) RealMethod() {}

func Free() {}
func AnotherFree() {}
func YetAnother() {}
"""
    methods = acg._go_methods_for_struct(
        content, "Foo", content.split("\n")
    )
    assert methods == ["RealMethod"]
    assert "Free" not in methods
    assert "AnotherFree" not in methods
    assert "YetAnother" not in methods


# ---------------------------------------------------------------------------
# Test 6 — struct with no methods → empty list
# ---------------------------------------------------------------------------


def test_data_struct_without_methods_returns_empty() -> None:
    """A struct that's pure data (no receiver-bound functions) →
    methods = []."""
    content = """
package main

type DataOnly struct {
    Field1 int
    Field2 string
}

func Helper() {}
"""
    methods = acg._go_methods_for_struct(
        content, "DataOnly", content.split("\n")
    )
    assert methods == []


# ---------------------------------------------------------------------------
# Test 7 — embedded-struct method promotion is NOT picked up
# ---------------------------------------------------------------------------


def test_embedded_struct_methods_not_promoted() -> None:
    """When Foo embeds Bar, Bar's methods are promoted to Foo at the Go
    type-system level — but the regex-based parser deliberately does NOT
    try to resolve this. Embedded-struct promotion requires whole-package
    analysis (queued as V52-O.11.G in backlog).

    This test pins the current behavior: methods of an embedded struct
    do NOT appear in the outer struct's methods list.
    """
    content = """
package main

type Inner struct{}

func (i *Inner) InnerMethod() {}

type Outer struct {
    Inner
}

func (o *Outer) OuterMethod() {}
"""
    methods_outer = acg._go_methods_for_struct(
        content, "Outer", content.split("\n")
    )
    methods_inner = acg._go_methods_for_struct(
        content, "Inner", content.split("\n")
    )
    # Inner's method is on Inner, NOT on Outer (no embedded-method promotion).
    assert methods_outer == ["OuterMethod"]
    assert "InnerMethod" not in methods_outer
    assert methods_inner == ["InnerMethod"]


# ---------------------------------------------------------------------------
# Test 8 — generic methods (Go 1.18+) on a generic struct
# ---------------------------------------------------------------------------


def test_generic_receiver_method() -> None:
    """func (s *Stack[T]) Push(v T) → methods on Stack = [Push].

    Go 1.18+ generics: the receiver carries a `[T]` type-parameter
    after the struct name. The regex's `[^)]*` tolerance for trailing
    receiver-paren content lets this through.
    """
    content = """
package main

type Stack[T any] struct {
    items []T
}

func (s *Stack[T]) Push(v T) {
    s.items = append(s.items, v)
}

func (s *Stack[T]) Pop() T {
    v := s.items[len(s.items)-1]
    s.items = s.items[:len(s.items)-1]
    return v
}
"""
    methods = acg._go_methods_for_struct(
        content, "Stack", content.split("\n")
    )
    assert "Push" in methods
    assert "Pop" in methods


# ---------------------------------------------------------------------------
# Test 9 — receiver-var-name doesn't matter, only the type matters
# ---------------------------------------------------------------------------


def test_receiver_variable_name_variants() -> None:
    """Receiver variable names are user-chosen; the regex must match
    any valid identifier in the receiver-var position."""
    content = """
package main

type Foo struct{}

func (f *Foo) Method1() {}
func (foo *Foo) Method2() {}
func (self *Foo) Method3() {}
func (_ *Foo) Method4() {}
"""
    methods = acg._go_methods_for_struct(
        content, "Foo", content.split("\n")
    )
    assert set(methods) == {"Method1", "Method2", "Method3", "Method4"}


# ---------------------------------------------------------------------------
# Test 10 — V52-O.11.F.2-GO regression: no func_pattern.finditer over whole file
# ---------------------------------------------------------------------------


def test_no_unconditional_func_finditer_in_go_struct_loop() -> None:
    """Regression test: prevents a future PR from re-introducing the
    pre-V52-O.11.F.2-GO line that iterated ``func_pattern`` over the
    WHOLE file inside the Go per-struct loop.

    Mirrors the equivalent test in
    ``test_v52_o11_f_rust_struct_methods.py``. Scans the Go analyzer's
    struct extraction loop body for the broken pattern. The new code
    uses ``_go_methods_for_struct(...)`` instead.

    NOTE: V52-O.11.F.2 is the parallel-language sweep tracked in the
    v0.2.52 backlog (Go this commit; JS/TS, Java, C# still queued).
    This regression test deliberately asserts only on the Go site to
    avoid blocking the GO ship on those parallel fixes.

    P2f stage 2 (v0.2.76): the Go extractor moved verbatim to
    vco_lib/codegraph_lang/go.py — the source scan follows it there.
    The tail boundary is now the NEXT loop header after the anchor
    (the rust guard's approach, see
    knowledge/concepts/test-regex-anchoring-fragility-2026-06-10.md):
    the old fixed 1500-char tail only excluded the FUNCTION loop's
    legitimate ``func_pattern.finditer(content_clean)`` by ~30 chars of
    slack, which the method -> free-function dedent (4 fewer columns per
    line) silently consumed.
    """
    src = Path(acg.__file__).read_text()

    # Strategy: locate the Go-specific `signature = f"type {sname}
    # struct/interface"` line first (unique to Go — Rust uses
    # `f"struct/enum/trait {sname}"`, JS/Java/C# use other strings),
    # then scan the struct-loop body around it.
    go_signature_anchor = 'signature = f"type {sname} struct/interface"'
    anchor_pos = src.find(go_signature_anchor)
    assert anchor_pos >= 0, (
        f"Could not locate Go signature anchor in codegraph_lang/go.py — "
        f"has it changed? Looked for: {go_signature_anchor!r}"
    )

    # Window: 600 chars BEFORE the anchor (covers the
    # `for sname, start_line in struct_info.items():` loop header) up to
    # the NEXT `for ` loop header after the anchor — the struct loop's
    # real terminator (the function-extraction loop, which legitimately
    # iterates all functions).
    window_start = max(0, anchor_pos - 600)
    next_for = src.find("\n    for ", anchor_pos)
    window_end = next_for if next_for != -1 else min(len(src), anchor_pos + 1500)
    window = src[window_start:window_end]

    # Sanity: window must contain the Go struct-loop's `for sname`
    # header somewhere. Otherwise the anchor is in some other code block.
    assert "for sname, start_line in struct_info.items():" in window, (
        "Go signature anchor isn't inside a struct-iteration loop — "
        "file shape has changed unexpectedly."
    )

    assert "func_pattern.finditer(content_clean)" not in window, (
        "V52-O.11.F.2-GO regression: the Go struct loop body contains "
        "``func_pattern.finditer(content_clean)`` which would attribute "
        "EVERY function in the file to every struct. Use "
        "``_go_methods_for_struct(content_clean, sname, source_lines)`` "
        "instead."
    )
    assert "_go_methods_for_struct" in window, (
        "V52-O.11.F.2-GO regression: the Go struct loop must call "
        "``_go_methods_for_struct`` for per-struct method scoping."
    )
