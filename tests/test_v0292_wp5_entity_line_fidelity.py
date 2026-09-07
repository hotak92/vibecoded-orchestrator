# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-5 — one property, every producer: a row starts where it says.

WHY THIS EXISTS
---------------
The C# ``start_line`` skew turned out not to be a C# bug. It is a SHAPE that
five producers share: a class/method pattern whose leading modifier group
includes ``\\s``, so ``match.start()`` is the whitespace after the PREVIOUS
token and the derived line number is one construct too high. The golden corpus
had ratified an instance of it in every one of them:

  * ``Account.java``  — the class stored on ``package golden;``; ``deposit``
    and ``getBalance`` each stored on the PREVIOUS method's closing ``}``,
    with a ``body`` that began with that brace and (because the brace scan
    then found no matching opener) ran to end-of-file;
  * ``geometry.cpp``  — ``Circle`` stored on ``namespace shapes {``, so its
    body was the WHOLE namespace including two other types; ``Point`` stored
    on ``Circle``'s ``};``;
  * ``Inventory.cs``  — all six rows skewed;
  * ``deploy.ps1``    — both functions stored on the preceding BLANK line,
    because the anchor used ``^\\s*`` under ``re.MULTILINE`` and ``\\s``
    matches a newline;
  * ``engine.rs``     — the trait's bodiless ``fn reset(&mut self);`` given
    the ``impl``'s braces, so the two ``reset`` rows that the duplicate-
    identity fix had just separated carried nearly the same text.

Rather than five separate regression tests, this file asserts the PROPERTY —
``source_lines[start_line - 1]`` contains the entity's name, the body begins
exactly there, and ``start_line <= end_line <= EOF`` — over a fixture per
language. A future producer that regrows the shape fails here by name.

WHAT IS PINNED
--------------
* the ACT: the property above, per language;
* the LEAVE-ALONE: a declaration at column 0 with nothing above it is
  unmoved; a deeply-indented PowerShell nested function is still found (the
  v0.2.75 regression case); an entity whose start was already correct keeps
  its line.
"""
from __future__ import annotations

from typing import Any

import pytest

from vco_lib.codegraph_entities import KIND_CLASS, KIND_FUNCTION
from vco_lib.codegraph_lang.cpp import extract_cpp_file
from vco_lib.codegraph_lang.csharp import extract_csharp_file
from vco_lib.codegraph_lang.java import extract_java_file
from vco_lib.codegraph_lang.powershell import extract_powershell_file
from vco_lib.codegraph_lang.rust import extract_rust_file


class _Helpers:
    project_name = "FidelityProj"

    def embed_class(self, *_a: Any, **_k: Any) -> None:
        return None

    def embed_function(self, *_a: Any, **_k: Any) -> None:
        return None

    def generate_embedding(self, *_a: Any, **_k: Any) -> None:
        return None


_JAVA = """// header comment
package golden;

public class Account {
    private long balance;

    public Account(long opening) {
        this.balance = opening;
    }

    public void deposit(long amount) {
        this.balance += amount;
    }

    public long getBalance() {
        return this.balance;
    }
}
"""

_CPP = """// header comment

#include <vector>

namespace shapes {

class Circle {
public:
    double radius_;
};

struct Point {
    double x;
};

template <typename T>
class Box {
public:
    T value;
};

}  // namespace shapes
"""

_CSHARP = """namespace Warehouse
{
    public interface IRepository
    {
        Item Find(int id);
    }

    [Route("api/items")]
    public class InventoryController
    {
        public Item Lookup(int id)
        {
            return new Item(id, "widget");
        }
    }
}
"""

_PS1 = """<#
.SYNOPSIS
Deploy helpers.
#>

function Invoke-Deploy {
    param([string]$Target)

        function Write-Step {
            Write-Output "step"
        }

    Write-Step
}

filter Get-Even {
    if ($_ % 2 -eq 0) { $_ }
}
"""

_RUST = """//! header

use std::fmt;

pub struct Counter {
    count: u64,
}

impl Counter {
    pub fn new() -> Self {
        Counter { count: 0 }
    }
}

pub trait Resettable {
    fn reset(&mut self);
}

impl Resettable for Counter {
    fn reset(&mut self) {
        self.count = 0;
    }
}
"""

_CASES = [
    pytest.param(extract_java_file, "Account.java", _JAVA, id="java"),
    pytest.param(extract_cpp_file, "geometry.cpp", _CPP, id="cpp"),
    pytest.param(extract_csharp_file, "Inventory.cs", _CSHARP, id="csharp"),
    pytest.param(extract_powershell_file, "deploy.ps1", _PS1, id="powershell"),
    pytest.param(extract_rust_file, "engine.rs", _RUST, id="rust"),
]


@pytest.mark.parametrize("extractor,fname,source", _CASES)
def test_every_row_starts_where_it_says_it_does(tmp_path, extractor, fname, source) -> None:
    target = tmp_path / fname
    target.write_text(source, encoding="utf-8")
    fx = extractor(source, target, tmp_path, _Helpers())
    lines = source.split("\n")

    checked = 0
    for e in fx.entities:
        if e.kind not in (KIND_CLASS, KIND_FUNCTION):
            continue
        checked += 1
        assert 1 <= e.start_line <= len(lines), (
            f"{fname} {e.name}: start_line {e.start_line} outside 1..{len(lines)}"
        )
        decl = lines[e.start_line - 1]

        # (1) The start line must carry declaration text. Every pre-fix skew in
        #     this corpus violated exactly this: a BLANK line (powershell), the
        #     previous member's `}` (java), a previous type's `};` (cpp), or a
        #     bare `{` (csharp).
        assert decl.strip(), (
            f"{fname} {e.kind} {e.name}: start_line {e.start_line} is BLANK"
        )
        assert decl.strip().strip("{}();,"), (
            f"{fname} {e.kind} {e.name}: start_line {e.start_line} is {decl!r} — "
            "punctuation only, so it is a neighbouring construct's brace"
        )
        # (2) The name must be here or on the very next line. Two lines rather
        #     than one ONLY because a leading clause that is genuinely part of
        #     the declaration may precede the name (`template <typename T>`).
        #     Every pre-fix case above still fails (1) or lands the name >= 2
        #     lines away.
        window = "\n".join(lines[e.start_line - 1:e.start_line + 1])
        assert e.name in window, (
            f"{fname} {e.kind} {e.name}: start_line {e.start_line} is {decl!r}, "
            "and the name is not on it or the line after"
        )
        # (3) The body is the slice that starts there.
        assert (e.body or "").split("\n")[0] == decl, (
            f"{fname} {e.kind} {e.name}: body does not begin at start_line"
        )
        assert e.start_line <= e.end_line <= len(lines), (
            f"{fname} {e.kind} {e.name}: end_line {e.end_line} outside "
            f"{e.start_line}..{len(lines)}"
        )
    assert checked >= 2, f"{fname}: only {checked} entities — fixture stopped working"


def test_a_cpp_template_class_keeps_its_template_clause(tmp_path) -> None:
    """LEAVE-ALONE with teeth: `template <typename T>` IS part of the
    declaration, so the whitespace skip must not move `Box` onto its `class`
    line and drop the clause out of the body."""
    target = tmp_path / "geometry.cpp"
    target.write_text(_CPP, encoding="utf-8")
    fx = extract_cpp_file(_CPP, target, tmp_path, _Helpers())
    box = next(e for e in fx.entities if e.kind == KIND_CLASS and e.name == "Box")
    assert box.start_line is not None and box.body is not None
    assert _CPP.split("\n")[box.start_line - 1] == "template <typename T>"
    assert box.body.startswith("template <typename T>\nclass Box {")


def test_a_rust_trait_method_does_not_borrow_the_impls_braces(tmp_path) -> None:
    """The `engine.rs` case the golden corpus ratified."""
    target = tmp_path / "engine.rs"
    target.write_text(_RUST, encoding="utf-8")
    fx = extract_rust_file(_RUST, target, tmp_path, _Helpers())
    resets = [e for e in fx.entities if e.kind == KIND_FUNCTION and e.name == "reset"]
    assert len(resets) == 2, "both declarations must produce a row"
    bodies = {(e.body or "").strip() for e in resets}
    assert "fn reset(&mut self);" in bodies, (
        f"the trait declaration must be its own one-line body; got {bodies!r}"
    )
    assert any(b.startswith("fn reset(&mut self) {") for b in bodies)
    assert len(bodies) == 2, "the two rows must not carry the same text"


def test_a_deeply_indented_powershell_nested_function_is_still_found(tmp_path) -> None:
    """LEAVE-ALONE: the v0.2.75 deep-indent regression case. `[ \\t]*` must
    still match 8 spaces — the fix narrowed `\\s*` to exclude NEWLINES, not
    indentation."""
    target = tmp_path / "deploy.ps1"
    target.write_text(_PS1, encoding="utf-8")
    fx = extract_powershell_file(_PS1, target, tmp_path, _Helpers())
    names = {e.name for e in fx.entities if e.kind == KIND_FUNCTION}
    assert {"Invoke-Deploy", "Write-Step", "Get-Even"} <= names


def test_a_declaration_at_column_zero_with_nothing_above_is_unmoved(tmp_path) -> None:
    """LEAVE-ALONE: the skip is a no-op when the match already starts on the
    declaration."""
    source = "public class Solo {\n    public int Size() { return 1; }\n}\n"
    target = tmp_path / "Solo.java"
    target.write_text(source, encoding="utf-8")
    fx = extract_java_file(source, target, tmp_path, _Helpers())
    solo = next(e for e in fx.entities if e.kind == KIND_CLASS)
    assert solo.start_line == 1


def test_class_end_line_is_the_closing_line_in_every_brace_language(tmp_path) -> None:
    """The `end_line` convention `_extract_balanced_block` documents, which
    seven class loops contradicted by writing closing-line + 1 — visible in the
    corpus as `end_line: 45` on a 44-line file."""
    for extractor, fname, source in (
        (extract_java_file, "Account.java", _JAVA),
        (extract_cpp_file, "geometry.cpp", _CPP),
        (extract_csharp_file, "Inventory.cs", _CSHARP),
        (extract_rust_file, "engine.rs", _RUST),
    ):
        target = tmp_path / fname
        target.write_text(source, encoding="utf-8")
        fx = extractor(source, target, tmp_path, _Helpers())
        lines = source.split("\n")
        for e in fx.entities:
            if e.kind != KIND_CLASS:
                continue
            assert e.end_line is not None, f"{fname} {e.name}: null end_line"
            assert e.end_line <= len(lines), f"{fname} {e.name}: end past EOF"
            closing = lines[e.end_line - 1].strip()
            assert closing.startswith("}") or closing.endswith("}"), (
                f"{fname} {e.name}: end_line {e.end_line} is {closing!r}, "
                "not the closing line"
            )
