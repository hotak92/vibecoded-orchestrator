# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-O.11.F — Rust struct.methods correct attribution.

Pre-V52-O.11.F (audit a79152, 2026-06-09): the Rust analyzer at
``analyze_code_graph.py:3436`` did:

    methods = [m.group(1) for m in func_pattern.finditer(content_clean)]

This iterated ``func_pattern`` over the WHOLE file's ``content_clean``
inside the per-struct loop, attributing EVERY function in the file to
EVERY struct. Audit reproduced: a 50-fn file with 3 structs produced
150 incorrect method attributions, drowning real signal in noise.

V52-O.11.F introduces ``_rust_methods_for_struct(content_clean,
struct_name, source_lines)`` that scopes ``methods`` to functions
declared inside ``impl <struct_name>`` (or ``impl <Trait> for
<struct_name>``) blocks.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parent.parent


def _load_acg() -> ModuleType:
    """Isolated load (same pattern as V52-O.11.E test). See that test's
    helper for full rationale on the sys.modules / sys.path restoration."""
    sys_modules_before = set(sys.modules.keys())
    sys_path_before = list(sys.path)

    spec = importlib.util.spec_from_file_location(
        "_v52_o11_f_acg_isolated",
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


# ---------------------------------------------------------------------------
# Test 1 — inherent impl block (Shape A)
# ---------------------------------------------------------------------------


def test_inherent_impl_block() -> None:
    """impl Foo { fn a() fn b() } → methods on Foo = [a, b]."""
    content = """
struct Foo { x: u32 }

impl Foo {
    fn new() -> Self { Foo { x: 0 } }
    fn get_x(&self) -> u32 { self.x }
}

fn unrelated_function() {}
"""
    methods = acg._rust_methods_for_struct(
        content, "Foo", content.split("\n")
    )
    assert methods == ["new", "get_x"], (
        f"Expected [new, get_x] from impl Foo; got {methods}. "
        f"unrelated_function MUST NOT appear — V52-O.11.F regression."
    )


# ---------------------------------------------------------------------------
# Test 2 — trait impl block (Shape B)
# ---------------------------------------------------------------------------


def test_trait_impl_block() -> None:
    """impl Trait for Foo { fn t1() fn t2() } → methods on Foo = [t1, t2]."""
    content = """
struct Foo {}
trait MyTrait { fn t1(&self); fn t2(&self); }

impl MyTrait for Foo {
    fn t1(&self) {}
    fn t2(&self) {}
}
"""
    methods = acg._rust_methods_for_struct(
        content, "Foo", content.split("\n")
    )
    assert "t1" in methods
    assert "t2" in methods


# ---------------------------------------------------------------------------
# Test 3 — generics on impl + target
# ---------------------------------------------------------------------------


def test_generic_impl_on_generic_struct() -> None:
    """impl<T> Foo<T> { fn x() } → methods on Foo = [x]."""
    content = """
struct Foo<T> { val: T }

impl<T> Foo<T> {
    fn new(val: T) -> Self { Foo { val } }
}
"""
    methods = acg._rust_methods_for_struct(
        content, "Foo", content.split("\n")
    )
    assert methods == ["new"]


def test_generic_trait_impl_on_generic_struct() -> None:
    """impl<T: Display> Display for Foo<T> { fn fmt(...) } → methods on Foo = [fmt]."""
    content = """
struct Foo<T> { val: T }

impl<T: std::fmt::Display> std::fmt::Display for Foo<T> {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        write!(f, "{}", self.val)
    }
}
"""
    methods = acg._rust_methods_for_struct(
        content, "Foo", content.split("\n")
    )
    assert "fmt" in methods


# ---------------------------------------------------------------------------
# Test 4 — multiple impl blocks for the same struct
# ---------------------------------------------------------------------------


def test_multiple_impl_blocks_aggregate() -> None:
    """Two `impl Foo { }` blocks → methods is the union."""
    content = """
struct Foo {}

impl Foo {
    fn from_first_block() {}
}

impl Foo {
    fn from_second_block() {}
}
"""
    methods = acg._rust_methods_for_struct(
        content, "Foo", content.split("\n")
    )
    assert "from_first_block" in methods
    assert "from_second_block" in methods


# ---------------------------------------------------------------------------
# Test 5 — methods on DIFFERENT structs are NOT mixed
# ---------------------------------------------------------------------------


def test_different_structs_have_distinct_methods() -> None:
    """The audit reproduction case: file with 3 structs + many fns,
    each struct gets only its own methods."""
    content = """
struct A {}
struct B {}
struct C {}

impl A {
    fn a_method_1() {}
    fn a_method_2() {}
}

impl B {
    fn b_method_1() {}
}

impl C {
    fn c_method_1() {}
    fn c_method_2() {}
    fn c_method_3() {}
}

fn free_fn_1() {}
fn free_fn_2() {}
fn free_fn_3() {}
"""
    methods_a = acg._rust_methods_for_struct(content, "A", content.split("\n"))
    methods_b = acg._rust_methods_for_struct(content, "B", content.split("\n"))
    methods_c = acg._rust_methods_for_struct(content, "C", content.split("\n"))

    assert sorted(methods_a) == ["a_method_1", "a_method_2"]
    assert sorted(methods_b) == ["b_method_1"]
    assert sorted(methods_c) == ["c_method_1", "c_method_2", "c_method_3"]

    # No method from another struct or from free fns leaks into any struct.
    for free_fn in ["free_fn_1", "free_fn_2", "free_fn_3"]:
        assert free_fn not in methods_a, (
            f"V52-O.11.F regression: free fn {free_fn!r} leaked into "
            f"struct A's methods. Pre-fix behavior was every fn attributed "
            f"to every struct."
        )
        assert free_fn not in methods_b
        assert free_fn not in methods_c

    # Cross-struct contamination check
    for m in ["a_method_1", "a_method_2"]:
        assert m not in methods_b
        assert m not in methods_c


# ---------------------------------------------------------------------------
# Test 6 — struct with no impl blocks → empty methods
# ---------------------------------------------------------------------------


def test_data_struct_without_impl_returns_empty() -> None:
    """A struct that's pure data (no impl block) → methods = []."""
    content = """
struct DataOnly {
    field1: u32,
    field2: String,
}

fn unrelated() {}
"""
    methods = acg._rust_methods_for_struct(
        content, "DataOnly", content.split("\n")
    )
    assert methods == []


# ---------------------------------------------------------------------------
# Test 7 — pub fn / async fn / unsafe fn / const fn modifiers
# ---------------------------------------------------------------------------


def test_method_modifiers_captured() -> None:
    """V52-O.11.G-adjacent: the inner method-pattern catches `pub fn`,
    `pub(crate) fn`, `async fn`, `unsafe fn`, `const fn`."""
    content = """
struct Foo {}

impl Foo {
    pub fn pub_method() {}
    pub(crate) fn crate_method() {}
    async fn async_method() {}
    unsafe fn unsafe_method() {}
    const fn const_method() -> u32 { 0 }
    fn plain_method() {}
}
"""
    methods = acg._rust_methods_for_struct(
        content, "Foo", content.split("\n")
    )
    expected = {
        "pub_method", "crate_method", "async_method",
        "unsafe_method", "const_method", "plain_method",
    }
    assert set(methods) == expected, (
        f"Expected {expected}; got {set(methods)}. "
        f"Some modifier variant is being missed by the inner method pattern."
    )


# ---------------------------------------------------------------------------
# Test 8 — nested impl on a DIFFERENT struct doesn't leak
# ---------------------------------------------------------------------------


def test_nested_impl_on_different_struct_is_excluded() -> None:
    """``impl Foo { ... impl Bar { ... } }`` (rare but legal in nested
    mod scope) — methods of Bar must NOT appear in Foo's methods."""
    content = """
struct Outer {}
struct Inner {}

impl Outer {
    fn outer_method() {}
}

mod inner_mod {
    use super::*;
    impl Inner {
        fn inner_method() {}
    }
}
"""
    methods_outer = acg._rust_methods_for_struct(
        content, "Outer", content.split("\n")
    )
    methods_inner = acg._rust_methods_for_struct(
        content, "Inner", content.split("\n")
    )
    assert methods_outer == ["outer_method"]
    assert methods_inner == ["inner_method"]


# ---------------------------------------------------------------------------
# Test 9 — V52-O.11.F regression: no func_pattern.finditer over whole file
# ---------------------------------------------------------------------------


def test_no_unconditional_func_finditer_in_rust_struct_loop() -> None:
    """Regression test: prevents a future PR from re-introducing the
    pre-V52-O.11.F line that iterated ``func_pattern`` over the WHOLE
    file inside the Rust per-struct loop.

    Scans the Rust analyzer's struct extraction loop body for the broken
    pattern. The new code uses ``_rust_methods_for_struct(...)`` instead.

    NOTE: V52-O.11.F is SCOPED TO RUST per audit a79152. The same
    pattern exists in the Go (line ~3438), JS/TS (~2540), Java (~3675),
    and C# (~3797) struct loops — those are queued as V52-O.11.F.2
    follow-ups in the v0.2.52 backlog. This regression test deliberately
    asserts only on the Rust site to avoid blocking V52-O.11.F ship on
    those parallel fixes.
    """
    src = (
        _REPO / "templates" / "scripts" / "analyze_code_graph.py"
    ).read_text()

    # Strategy: locate the Rust-specific `signature = f"struct/enum/trait
    # {sname}"` line first (unique to Rust — Go uses
    # `f"type {sname} struct/interface"`, JS/Java/C# use other strings),
    # then scan WINDOWS of code around it. The loop body for a single
    # struct entry is small (~25 lines), so 800 chars window is plenty.
    rust_signature_anchor = 'signature = f"struct/enum/trait {sname}"'
    anchor_pos = src.find(rust_signature_anchor)
    assert anchor_pos >= 0, (
        f"Could not locate Rust signature anchor in analyze_code_graph.py — "
        f"has it changed? Looked for: {rust_signature_anchor!r}"
    )

    # Window = the struct loop body ONLY. Start 600 chars BEFORE the anchor
    # (covers the `for sname, start_line in struct_info.items():` header +
    # the body up to the signature line). End at the NEXT `for ` after the
    # anchor — that is the struct loop's real terminator (the FUNCTION
    # extraction loop `for m in func_pattern.finditer(content_clean):`, which
    # legitimately iterates all functions). A fixed char-count window used to
    # spill into that adjacent loop and false-positive on its legitimate
    # `func_pattern.finditer(content_clean)` (see
    # knowledge/concepts/test-regex-anchoring-fragility-2026-06-10.md); binding
    # to the loop boundary keeps the guard scoped to the struct loop body.
    window_start = max(0, anchor_pos - 600)
    next_for = src.find("\n        for ", anchor_pos)
    window_end = next_for if next_for != -1 else min(len(src), anchor_pos + 1500)
    window = src[window_start:window_end]

    # Sanity: window must contain the Rust struct-loop's `for sname`
    # header somewhere. Otherwise the anchor is in some other code block.
    assert "for sname, start_line in struct_info.items():" in window, (
        "Rust signature anchor isn't inside a struct-iteration loop — "
        "file shape has changed unexpectedly."
    )

    assert "func_pattern.finditer(content_clean)" not in window, (
        "V52-O.11.F regression: the Rust struct loop body contains "
        "``func_pattern.finditer(content_clean)`` which would attribute "
        "EVERY function in the file to every struct. Use "
        "``_rust_methods_for_struct(content_clean, sname, source_lines)`` "
        "instead."
    )
    assert "_rust_methods_for_struct" in window, (
        "V52-O.11.F regression: the Rust struct loop must call "
        "``_rust_methods_for_struct`` for per-struct method scoping."
    )
