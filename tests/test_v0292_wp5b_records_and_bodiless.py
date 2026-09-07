# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-5b — two types the graph never contained.

WHY THIS EXISTS
---------------
**C# positional records emitted no entity at all.** ``class_pattern`` captures
the type name with ``([\\w<>, ]+?)`` and then demands ``\\{``; a positional
record puts a parameter list between the two, so neither
``record Item(int Id, string Name);`` nor
``record Point(int X, int Y) { … }`` can match. Earlier in v0.2.92 the
spurious FUNCTION row that ``method_pattern`` used to mint for the first shape
was removed, which left the type with no row of any kind. A record is what C# 9+
code uses for the DTOs an API surface is made of — exactly the types a
code-graph search is asked about.

**Java never emitted an interface or abstract method.** ``method_pattern``
ended in ``\\s*\\{``, so a bodiless declaration matched nothing; and
``_java_methods_for_class``'s inner pattern ended the same way, with a comment
claiming abstract methods were "intentionally captured only when they have a
body". Every interface in every indexed project had an EMPTY methods list and
no function rows.

THE COST OF ACCEPTING ``;``, and why it needs its own assertions: the moment
``;`` is a terminator, ordinary STATEMENTS match. ``new Thread(runnable);``
parses as return-type ``new`` + name ``Thread`` + args + ``;``. The captured
name is a perfectly good identifier, so a keyword filter on the NAME cannot see
it — the tell is the modifier/return-type RUN, which is exactly the guard
v0.2.92 built for C# (``_CSHARP_NON_DECL_TOKENS``) and this change mirrors for
Java.

WHAT IS PINNED
--------------
* the ACT: a positional record (with and without a body) is a CodeClass; an
  interface method and an abstract method are CodeFunctions whose body is the
  one line they occupy;
* the LEAVE-ALONE: a statement that now matches is REJECTED, in the entity loop
  and in the methods list; a record's positional parameters are NOT members
  (the helper's docstring has always said so); a non-positional
  ``record Foo { … }`` is still matched once, by the class pattern, not twice.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from vco_lib.codegraph_entities import KIND_CLASS, KIND_FUNCTION
from vco_lib.codegraph_lang.csharp import _csharp_methods_for_class, extract_csharp_file
from vco_lib.codegraph_lang.java import (
    _java_declaration_run_is_a_member,
    _java_methods_for_class,
    extract_java_file,
)

_REPO = Path(__file__).parent / "fixtures" / "codegraph_golden" / "repo" / "src"


class _Helpers:
    project_name = "Wp5bProj"

    def embed_class(self, *_a: Any, **_k: Any) -> None:
        return None

    def embed_function(self, *_a: Any, **_k: Any) -> None:
        return None

    def generate_embedding(self, *_a: Any, **_k: Any) -> None:
        return None


def _run(extractor, name: str, source: str, tmp_path):
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    return extractor(source, target, tmp_path, _Helpers())


def _classes(fx):
    return [e for e in fx.entities if e.kind == KIND_CLASS]


def _functions(fx):
    return [e for e in fx.entities if e.kind == KIND_FUNCTION]


# ═══════════════════════════════════════════════════════════════════════════
# C# — the positional record
# ═══════════════════════════════════════════════════════════════════════════
def test_the_golden_fixtures_positional_record_now_has_a_row(tmp_path) -> None:
    src = (_REPO / "Inventory.cs").read_text(encoding="utf-8")
    fx = _run(extract_csharp_file, "Inventory.cs", src, tmp_path)
    item = next(c for c in _classes(fx) if c.name == "Item")
    assert item.full_name == "Warehouse.Item"
    # `Inventory.cs:15` is `    public record Item(int Id, string Name);`
    assert (item.start_line, item.end_line) == (15, 15)
    assert (item.body or "").strip() == "public record Item(int Id, string Name);"


def test_a_bodiless_record_ends_on_its_own_declaration(tmp_path) -> None:
    src = (
        "namespace N\n{\n"
        "    public record Item(int Id);\n\n"
        "    public class Other\n    {\n        public int F() { return 1; }\n    }\n}\n"
    )
    fx = _run(extract_csharp_file, "N.cs", src, tmp_path)
    item = next(c for c in _classes(fx) if c.name == "Item")
    assert (item.start_line, item.end_line) == (3, 3), (
        "a `;` record must not borrow the next type's braces"
    )


def test_a_positional_record_WITH_a_body_is_measured_by_its_braces(tmp_path) -> None:
    src = (
        "namespace N\n{\n"
        "    public record Point(int X, int Y)\n    {\n"
        "        public int Sum() { return X + Y; }\n    }\n}\n"
    )
    fx = _run(extract_csharp_file, "N.cs", src, tmp_path)
    point = next(c for c in _classes(fx) if c.name == "Point")
    assert (point.start_line, point.end_line) == (3, 6)
    assert point.extras.get("methods") == ["Sum"]


def test_a_record_struct_and_a_record_class_are_both_types(tmp_path) -> None:
    src = (
        "namespace N\n{\n"
        "    public record struct Vec(double X, double Y);\n"
        "    public record class Tag(string Name);\n}\n"
    )
    fx = _run(extract_csharp_file, "N.cs", src, tmp_path)
    assert {c.name for c in _classes(fx)} == {"Vec", "Tag"}


def test_a_records_positional_parameters_are_not_members(tmp_path) -> None:
    """LEAVE-ALONE: ``_csharp_methods_for_class``'s docstring has always said
    the primary-constructor parameters generate auto-properties that it does
    NOT extract. Making the type appear must not quietly change that."""
    src = (_REPO / "Inventory.cs").read_text(encoding="utf-8")
    fx = _run(extract_csharp_file, "Inventory.cs", src, tmp_path)
    item = next(c for c in _classes(fx) if c.name == "Item")
    assert item.extras.get("methods") == []
    assert "Id" not in (item.extras.get("methods") or [])


def test_the_scoped_helper_stops_at_a_declaration_terminator(tmp_path) -> None:
    """The defect adding the record UNMASKED. The class-header trailer was
    ``[^{]*``, which walks straight past a bodiless declaration's ``;`` and
    latches onto the NEXT type's brace — so asked for the members of
    ``record Item(...);`` the helper returned ``InventoryController``'s five.
    Latent until now only because nothing ever asked."""
    src = (_REPO / "Inventory.cs").read_text(encoding="utf-8")
    got = _csharp_methods_for_class(src, "Item", src.split("\n"))
    assert got == [], f"the record's members leaked from a neighbouring type: {got}"


def test_adding_the_record_does_not_move_any_other_csharp_row(tmp_path) -> None:
    """LEAVE-ALONE with teeth: a new entry in the type list shifts ``enclosing``
    attribution for every member declared BELOW it, so the fixture's other rows
    are asserted unchanged rather than assumed."""
    src = (_REPO / "Inventory.cs").read_text(encoding="utf-8")
    fx = _run(extract_csharp_file, "Inventory.cs", src, tmp_path)
    got = {f.full_name: (f.start_line, f.end_line) for f in _functions(fx)}
    assert got == {
        "Warehouse.IRepository.Find": (12, 12),
        "Warehouse.InventoryController.Lookup": (22, 25),
        "Warehouse.InventoryController.WrapAll": (27, 30),
        "Warehouse.InventoryController.GetAll": (33, 36),
        "Warehouse.InventoryController.Add": (39, 42),
    }
    controller = next(c for c in _classes(fx) if c.name == "InventoryController")
    assert (controller.start_line, controller.end_line) == (18, 43)
    assert controller.extras.get("methods") == ["Lookup", "WrapAll", "GetAll", "Add", "Count"]


def test_a_member_declared_below_a_record_belongs_to_the_record_not_the_type_above(
    tmp_path,
) -> None:
    """The attribution move the record CAN cause, made explicit rather than
    left to chance: a record with a body owns what is inside it."""
    src = (
        "namespace N\n{\n"
        "    public class Above\n    {\n        public int A() { return 1; }\n    }\n\n"
        "    public record Below(int X)\n    {\n        public int B() { return X; }\n    }\n}\n"
    )
    fx = _run(extract_csharp_file, "N.cs", src, tmp_path)
    got = {f.name: f.full_name for f in _functions(fx)}
    assert got["A"] == "N.Above.A"
    assert got["B"] == "N.Below.B"


# ═══════════════════════════════════════════════════════════════════════════
# Java — the bodiless declaration
# ═══════════════════════════════════════════════════════════════════════════
def test_the_golden_fixtures_interface_method_now_has_a_row(tmp_path) -> None:
    src = (_REPO / "Account.java").read_text(encoding="utf-8")
    fx = _run(extract_java_file, "Account.java", src, tmp_path)
    fn = next(f for f in _functions(fx) if f.name == "balanceOf")
    # `Account.java:23` is `    long balanceOf(String owner);`
    assert (fn.start_line, fn.end_line) == (23, 23)
    assert fn.full_name == "Ledger.balanceOf"
    assert (fn.body or "").strip() == "long balanceOf(String owner);"


def test_an_abstract_method_now_has_a_row(tmp_path) -> None:
    src = (_REPO / "Account.java").read_text(encoding="utf-8")
    fx = _run(extract_java_file, "Account.java", src, tmp_path)
    fn = next(f for f in _functions(fx) if f.name == "audit")
    assert (fn.start_line, fn.end_line) == (27, 27)
    assert (fn.body or "").strip() == "abstract void audit();"


def test_an_interfaces_methods_list_is_no_longer_empty(tmp_path) -> None:
    """Otherwise the graph disagrees with ITSELF: a function row exists for a
    method the class says it does not have."""
    src = (_REPO / "Account.java").read_text(encoding="utf-8")
    fx = _run(extract_java_file, "Account.java", src, tmp_path)
    ledger = next(c for c in _classes(fx) if c.name == "Ledger")
    assert ledger.extras.get("methods") == ["balanceOf"]
    base = next(c for c in _classes(fx) if c.name == "BaseAccount")
    assert base.extras.get("methods") == ["audit", "touch"]


def test_a_bodiless_declaration_does_not_borrow_the_next_members_braces(tmp_path) -> None:
    src = (
        "interface I {\n"
        "    int a();\n"
        "    int b();\n"
        "}\n"
        "class C {\n"
        "    int c() { return 1; }\n"
        "}\n"
    )
    fx = _run(extract_java_file, "I.java", src, tmp_path)
    got = {f.name: (f.start_line, f.end_line) for f in _functions(fx)}
    assert got == {"a": (2, 2), "b": (3, 3), "c": (6, 6)}


def test_a_statement_that_now_matches_is_rejected(tmp_path) -> None:
    """``Account.java:33`` is ``Object marker = new Object();``. It becomes
    matchable the moment ``;`` joins the terminator set, and only the
    modifier/return-type run tells it apart from a declaration."""
    src = (_REPO / "Account.java").read_text(encoding="utf-8")
    fx = _run(extract_java_file, "Account.java", src, tmp_path)
    names = {f.name for f in _functions(fx)}
    assert "Object" not in names, "a `new X();` statement minted a function row"
    base = next(c for c in _classes(fx) if c.name == "BaseAccount")
    assert "Object" not in (base.extras.get("methods") or [])


@pytest.mark.parametrize(
    "statement,rejected",
    [
        ("        Object m = new Object();", True),
        ("        return format(x);", True),
        ("        throw wrap(e);", True),
        ("    public int f(", False),
        ("    int f(", False),
        ("    abstract void audit(", False),
        ("    long balanceOf(", False),
    ],
)
def test_the_java_declaration_run_guard_per_shape(statement: str, rejected: bool) -> None:
    """Unit-level, so the guard is pinned independently of any one fixture.
    The run is the text from the line start to the captured NAME."""
    name_pos = statement.rindex("(")
    while name_pos > 0 and (statement[name_pos - 1].isalnum() or statement[name_pos - 1] == "_"):
        name_pos -= 1
    assert _java_declaration_run_is_a_member(statement, 0, name_pos) is not rejected


def test_the_scoped_helper_ignores_a_statement_inside_a_method_body() -> None:
    src: str = (
        "class C {\n"
        "    void go() {\n"
        "        Object m = new Object();\n"
        "        return;\n"
        "    }\n"
        "}\n"
    )
    lines: List[str] = list(src.split("\n"))
    assert _java_methods_for_class(src, "C", lines) == ["go"]


def test_an_ordinary_java_class_is_untouched(tmp_path) -> None:
    """LEAVE-ALONE: the golden fixture's original class, asserted explicitly."""
    src = (_REPO / "Account.java").read_text(encoding="utf-8")
    fx = _run(extract_java_file, "Account.java", src, tmp_path)
    account = next(c for c in _classes(fx) if c.name == "Account")
    assert (account.start_line, account.end_line) == (4, 18)
    assert account.extras.get("methods") == ["Account", "deposit", "getBalance"]
    got = {f.full_name: (f.start_line, f.end_line) for f in _functions(fx)}
    assert got["Account.Account"] == (7, 9)
    assert got["Account.deposit"] == (11, 13)
    assert got["Account.getBalance"] == (15, 17)


# ═══════════════════════════════════════════════════════════════════════════
# TRI-OS (R12 / R14)
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "terminator", [pytest.param("\n", id="linux-macos-LF"), pytest.param("\r\n", id="windows-CRLF")]
)
def test_bodiless_rows_land_on_the_same_lines_on_every_os(terminator: str, tmp_path) -> None:
    """A Windows checkout is CRLF by default and CI checks out LF, so a
    terminator-sensitive line number here would be invisible to every CI run
    while wrong in every Windows install."""
    java = (
        "interface I {\n    int a();\n}\n\nabstract class C {\n    abstract void b();\n}\n"
    ).replace("\n", terminator)
    fx = _run(extract_java_file, "I.java", java, tmp_path)
    assert {f.name: (f.start_line, f.end_line) for f in _functions(fx)} == {
        "a": (2, 2), "b": (6, 6),
    }

    cs = (
        "namespace N\n{\n    public record Item(int Id);\n\n"
        "    public class C\n    {\n        public int F() { return 1; }\n    }\n}\n"
    ).replace("\n", terminator)
    fx2 = _run(extract_csharp_file, "N.cs", cs, tmp_path)
    item = next(c for c in _classes(fx2) if c.name == "Item")
    assert (item.start_line, item.end_line) == (3, 3)
