# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-5c — C++ free functions are captured (R25).

WHY THIS EXISTS
---------------
Until this change ``cpp.py`` captured exactly ONE function shape: the
out-of-line ``Class::method(...) { … }`` definition. A free function had no
``Class::`` to anchor on and produced no row of any kind, so a C project, a
header-only utility namespace and every ``main()`` in the corpus were
invisible to the code graph. WP-5b documented that rather than closing it;
R25 refused the accepted-with-rationale — "documented does not make an
incomplete graph correct".

The companion file ``test_v0292_wp5c_cpp_negative_space.py`` is the other
half and the one that governs: the capture is only as good as the shapes it
REFUSES. Read it first.

THE CAPTURE RULE, stated once
-----------------------------
A free function is a match for::

    <line start> [template<…>] <type-run> <name> ( <args> ) [quals] [-> ret] {

with four structural guards, each of which kills a family of junk rows
rather than one instance:

  1. **The declaration must begin at the first non-whitespace character of a
     line** (``(?:^|\\n)\\s*``). Namespace-scope functions are INDENTED — the
     common case a column-0 anchor misses — but a lambda passed as an
     argument, a ``return f(x)`` call and a member-initialiser entry are all
     mid-line, and none of them can match.
  2. **A non-empty type run is required** before the name. ``if (c) {``,
     ``for (…) {``, ``switch (v) {`` and a bare macro invocation have no run
     at all, so they cannot match even before the keyword filters see them —
     and a member-initialiser list split onto its own line (``      items_(n)
     {``) begins with the member name, which is likewise a run of length
     zero.
  3. **The terminator is ``{`` and never ``;``.** This is the load-bearing
     one. C++ function-style initialization (``std::vector<int> v(10, 0);``)
     is SYNTACTICALLY a function declaration — the most vexing parse — so
     no regex can separate a prototype from a variable definition. Java
     could accept ``;`` for its interface methods because its statement
     grammar has no such form; C++ cannot.
  4. **A qualified name cannot match.** The run is a sequence of
     ``::``-joined tokens each followed by whitespace/``*``/``&``, so
     ``int Gadget::total() {`` offers no split where the name is unqualified
     — the out-of-line member stays ``method_pattern``'s exclusive property
     and is never double-counted.

WHAT IS PINNED
--------------
* the ACT: nine free-function shapes, an overload pair, and the two
  member functions of a header-only class;
* the attribution decision: a function DEFINED INSIDE a class body is a
  member — it takes the enclosing type's name and joins its ``methods``
  list, rather than being minted as a free function under the file stem;
* the LEAVE-ALONE: the pre-existing out-of-line rows and both class rows of
  the golden ``geometry.cpp`` keep their exact ranges.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from vco_lib.codegraph_entities import KIND_CLASS, KIND_FUNCTION
from vco_lib.codegraph_lang.cpp import extract_cpp_file


class _Helpers:
    project_name = "FreeFnProj"

    def embed_class(self, *_a: Any, **_k: Any) -> None:
        return None

    def embed_function(self, *_a: Any, **_k: Any) -> None:
        return None

    def generate_embedding(self, *_a: Any, **_k: Any) -> None:
        return None


_POSITIVES = r'''// Positive fixture for the C++ free-function capture.

#include <string>
#include <vector>

namespace shapes {

struct Point {
    double x;
    double y;
};

// Namespace scope, INDENTED — the common case a column-0 anchor misses.
int quadrant(const Point& p) {
    return p.x >= 0 ? 1 : 2;
}

// A `template<...>` clause on its own line is part of the declaration.
template <typename T>
T identity(T value) {
    return value;
}

}  // namespace shapes

// File scope, qualified parameter types.
double distance(const shapes::Point& a, const shapes::Point& b) {
    return a.x - b.x;
}

// static / inline / constexpr qualifier run.
static inline constexpr int qualified(int n) {
    return n * 2;
}

// Trailing return type.
auto trailing(int x) -> int {
    return x + 1;
}

// A signature split across lines; the body brace closes the third line.
std::vector<double> multi_line(
    const std::vector<double>& xs,
    double factor) {
    return xs;
}

// Allman brace — the opener is on its own line.
unsigned long long allman(int n)
{
    return n;
}

// Overloads: one full_name, two distinct rows.
double norm(double x) { return x < 0 ? -x : x; }
double norm(const shapes::Point& p) { return p.x; }

// A reference-to-template return type.
const std::vector<int>& borrow(const std::vector<int>& v) {
    return v;
}

// A definition inside `extern "C" { ... }`.
extern "C" {
int c_entry(int x) {
    return x;
}
}

// A header-only class: both members are DEFINED in the class body.
class Counter {
public:
    void bump() { ++value_; }

    int value() const {
        return value_;
    }

private:
    int value_ = 0;
};

int main(int argc, char** argv) {
    return argc > 1 ? 0 : 1;
}
'''

#: full_name → (start_line, end_line), predicted BY HAND from a numbered
#: listing of ``_POSITIVES`` before the extractor was changed, then verified
#: line by line against that listing.
_EXPECTED_FUNCTIONS: Dict[str, Tuple[int, int]] = {
    "positives.quadrant": (14, 16),
    "positives.identity": (19, 22),      # the `template` line, not `T identity`
    "positives.distance": (27, 29),
    "positives.qualified": (32, 34),
    "positives.trailing": (37, 39),
    "positives.multi_line": (42, 46),    # signature spans 42-44
    "positives.allman": (49, 52),        # opener on 50, closes on 52
    "positives.borrow": (59, 61),
    "positives.c_entry": (65, 67),
    "positives.Counter.bump": (73, 73),
    "positives.Counter.value": (75, 77),
    "positives.main": (83, 85),
}

#: The overload pair shares a full_name, so it cannot live in the dict above.
_EXPECTED_OVERLOADS = [(55, 55), (56, 56)]


def _extract(tmp_path, source: str = _POSITIVES, fname: str = "positives.cpp"):
    target = tmp_path / fname
    target.write_text(source, encoding="utf-8")
    return extract_cpp_file(source, target, tmp_path, _Helpers())


def _functions(fx) -> List[Any]:
    return [e for e in fx.entities if e.kind == KIND_FUNCTION]


def _classes(fx) -> List[Any]:
    return [e for e in fx.entities if e.kind == KIND_CLASS]


# ═══════════════════════════════════════════════════════════════════════════
# THE ACT
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("full_name,rng", sorted(_EXPECTED_FUNCTIONS.items()))
def test_a_free_function_shape_is_captured_on_its_own_lines(
    full_name: str, rng: Tuple[int, int], tmp_path
) -> None:
    fx = _extract(tmp_path)
    rows = [e for e in _functions(fx) if e.full_name == full_name]
    assert len(rows) == 1, (
        f"{full_name}: expected exactly one row, got {len(rows)}"
    )
    assert (rows[0].start_line, rows[0].end_line) == rng


def test_the_capture_produces_exactly_the_predicted_set(tmp_path) -> None:
    """The count matters as much as the membership: a pattern that captures
    everything expected PLUS three junk rows passes every per-shape test
    above and fails only here."""
    fx = _extract(tmp_path)
    got = sorted(e.full_name for e in _functions(fx))
    want = sorted(list(_EXPECTED_FUNCTIONS) + ["positives.norm"] * 2)
    assert got == want, f"unexpected extra/missing rows: {set(got) ^ set(want)}"
    assert fx.stats["functions"] == len(want)


def test_a_template_free_function_keeps_its_template_clause(tmp_path) -> None:
    """The clause IS the declaration's first line — the same property
    ``test_a_cpp_template_class_keeps_its_template_clause`` pins for types.
    A capture anchored on ``T identity(`` would drop it out of the body."""
    fx = _extract(tmp_path)
    ident = next(e for e in _functions(fx) if e.name == "identity")
    assert ident.body is not None
    assert ident.body.startswith("template <typename T>\nT identity(T value) {")


def test_an_overload_pair_yields_two_distinct_rows(tmp_path) -> None:
    """C++ overloading makes a duplicate ``full_name`` NORMAL rather than
    exceptional — the shape ``ruby.py``'s reopened class and ``engine.rs``'s
    ``fn reset`` needed the occurrence disambiguator for. Two rows carrying
    the same text would be the clobber in disguise."""
    fx = _extract(tmp_path)
    norms = [e for e in _functions(fx) if e.full_name == "positives.norm"]
    assert len(norms) == 2
    assert sorted((e.start_line, e.end_line) for e in norms) == _EXPECTED_OVERLOADS
    assert len({e.body for e in norms}) == 2
    assert len({e.signature for e in norms}) == 2, (
        "the two signatures must differ — that is what the overload IS"
    )


def test_every_captured_row_begins_on_its_own_declaration(tmp_path) -> None:
    """The WP-5 fidelity property, applied to the new producer at birth
    rather than after a corpus ratified a skew."""
    fx = _extract(tmp_path)
    lines = _POSITIVES.split("\n")
    # Not a formality: with nothing captured this loop has no iterations and
    # the test passes vacuously — the shape of a test that encodes the loss.
    assert len(_functions(fx)) == len(_EXPECTED_FUNCTIONS) + 2
    for e in _functions(fx):
        decl = lines[e.start_line - 1]
        assert decl.strip(), f"{e.full_name}: start_line {e.start_line} is blank"
        window = "\n".join(lines[e.start_line - 1:e.start_line + 1])
        assert e.name in window, (
            f"{e.full_name}: start_line {e.start_line} is {decl!r}"
        )
        assert (e.body or "").split("\n")[0] == decl
        assert lines[e.end_line - 1].rstrip().endswith(("}", "};")), (
            f"{e.full_name}: end_line {e.end_line} is "
            f"{lines[e.end_line - 1]!r}, not a closing line"
        )


def test_a_free_function_signature_carries_its_arguments(tmp_path) -> None:
    fx = _extract(tmp_path)
    by_name = {e.full_name: e for e in _functions(fx)}
    assert by_name["positives.distance"].signature == (
        "distance(const shapes::Point& a, const shapes::Point& b)"
    )
    assert by_name["positives.main"].signature == "main(int argc, char** argv)"
    # A member DEFINED in the class body takes the same `Class::method(args)`
    # signature the out-of-line form produces, so the two spellings of one
    # member are not two different-looking rows.
    assert by_name["positives.Counter.bump"].signature == "Counter::bump()"


# ═══════════════════════════════════════════════════════════════════════════
# THE ATTRIBUTION DECISION — an in-class definition is a MEMBER
# ═══════════════════════════════════════════════════════════════════════════
def test_a_method_defined_in_the_class_body_is_attributed_to_the_class(
    tmp_path,
) -> None:
    """A header-only class defines its members INSIDE the braces, so the
    free-function pattern matches them too. Minting them under the file stem
    (``positives.bump``) would be a misattribution — a junk row by the R25
    standard even though the function is real. Attribution is by CONTAINMENT
    in the type's line range, not by proximity: WP-5b had to fix exactly that
    heuristic in ``ruby.py``, where a top-level ``def`` after a class's
    ``end`` was reported as a member of it."""
    fx = _extract(tmp_path)
    names = {e.full_name for e in _functions(fx)}
    assert "positives.Counter.bump" in names and "positives.Counter.value" in names
    assert "positives.bump" not in names and "positives.value" not in names
    # ...and `main`, which follows the class, is NOT swept into it.
    assert "positives.main" in names and "positives.Counter.main" not in names


def test_the_class_methods_list_names_its_in_class_definitions(tmp_path) -> None:
    """WP-5b's lesson, applied to the shape that reintroduces it: emitting a
    function row for a member while the owning type's ``methods`` list omits
    it makes THE GRAPH DISAGREE WITH ITSELF, which is what an empty Java
    interface ``methods`` list did next to its own function rows."""
    fx = _extract(tmp_path)
    counter = next(c for c in _classes(fx) if c.name == "Counter")
    assert counter.extras is not None
    assert counter.extras["methods"] == ["bump", "value"]


def test_a_free_function_at_namespace_scope_is_not_attributed_to_a_type(
    tmp_path,
) -> None:
    """LEAVE-ALONE with teeth: ``quadrant`` is indented inside
    ``namespace shapes`` and follows ``struct Point``, so a proximity
    heuristic would call it ``Point``'s member. Containment says otherwise —
    ``Point`` closes on line 11 and ``quadrant`` opens on 14."""
    fx = _extract(tmp_path)
    names = {e.full_name for e in _functions(fx)}
    assert "positives.quadrant" in names
    assert "positives.Point.quadrant" not in names
    point = next(c for c in _classes(fx) if c.name == "Point")
    assert point.extras is not None and point.extras["methods"] == []


# ═══════════════════════════════════════════════════════════════════════════
# A PRE-EXISTING DEFECT THIS CAPTURE UNMASKED (R23)
#
# `class_pattern` could skip no token between `class` and `{` other than a
# base-clause, so two ubiquitous C++ header shapes produced NO class row:
# `class D final : public B {` and `class MYLIB_API W {`. That was a silent
# loss on its own. Combined with the free-function capture it becomes a
# MISATTRIBUTION — the members defined in such a body have no enclosing type
# for containment to find, so they are minted under the file stem — which is
# why it is fixed in the same change rather than reported onward.
# ═══════════════════════════════════════════════════════════════════════════
_TOKENS_BEFORE_THE_BRACE = r'''#define MYLIB_API __attribute__((visibility("default")))

class Base {
public:
    int seed() const { return seed_; }
protected:
    int seed_;
};

class Derived final : public Base {
public:
    int run(int x) { return x + seed_; }
};

class MYLIB_API Widget {
public:
    void draw() { }
};

struct Marker final {
    int weight() const { return 1; }
};
'''


@pytest.mark.parametrize(
    "type_name,member,rng",
    [
        pytest.param("Derived", "run", (10, 13), id="class-final-with-base"),
        pytest.param("Widget", "draw", (15, 18), id="class-with-export-macro"),
        pytest.param("Marker", "weight", (20, 22), id="struct-final"),
    ],
)
def test_a_type_with_tokens_before_its_brace_is_captured_and_owns_its_members(
    type_name: str, member: str, rng: Tuple[int, int], tmp_path
) -> None:
    fx = _extract(tmp_path, source=_TOKENS_BEFORE_THE_BRACE, fname="tokens.cpp")
    types = {c.name: (c.start_line, c.end_line) for c in _classes(fx)}
    assert type_name in types, (
        f"`{type_name}` produced no class row — its members are then minted "
        "as free functions under the file stem"
    )
    assert types[type_name] == rng
    fn_names = {e.full_name for e in _functions(fx)}
    assert f"tokens.{type_name}.{member}" in fn_names
    assert f"tokens.{member}" not in fn_names
    owner = next(c for c in _classes(fx) if c.name == type_name)
    assert owner.extras is not None and owner.extras["methods"] == [member]


def test_the_export_macro_is_not_mistaken_for_the_type_name(tmp_path) -> None:
    """Naming the type `MYLIB_API` would be worse than missing it: every
    header using the macro would collapse into one same-named type."""
    fx = _extract(tmp_path, source=_TOKENS_BEFORE_THE_BRACE, fname="tokens.cpp")
    assert "MYLIB_API" not in {c.name for c in _classes(fx)}


def test_an_ordinary_type_declaration_is_unchanged(tmp_path) -> None:
    """LEAVE-ALONE: the skip is non-greedy, so every shape the pattern
    already matched keeps its name and its range."""
    fx = _extract(tmp_path)
    assert {c.name: (c.start_line, c.end_line) for c in _classes(fx)} == {
        "Point": (8, 11), "Counter": (71, 81),
    }
    fx2 = _extract(tmp_path, source=_TOKENS_BEFORE_THE_BRACE, fname="tokens.cpp")
    assert next(c for c in _classes(fx2) if c.name == "Base").start_line == 3


# ═══════════════════════════════════════════════════════════════════════════
# THE LEAVE-ALONE — the shipped corpus fixture's pre-existing rows
# ═══════════════════════════════════════════════════════════════════════════
_GEOMETRY_HEAD = r'''// Geometry module for the golden-fixture repo (C++, regex-parsed).
// Exercises: namespace, class + struct, templates, out-of-line methods.

#include <vector>
#include <string>
#include <cmath>

namespace shapes {

class Circle {
public:
    Circle(double radius);
    double area() const;
    double circumference() const;

private:
    double radius_;
};

struct Point {
    double x;
    double y;
};

template <typename T>
class Box {
public:
    T value;
};

}  // namespace shapes

// Out-of-line method definitions: ClassName::method(...) — the only
// method shape the regex extractor captures.
shapes::Circle::Circle(double radius) : radius_(radius) {}

double shapes::Circle::area() const {
    return 3.14159 * radius_ * radius_;
}

double shapes::Circle::circumference() const {
    return 2.0 * 3.14159 * radius_;
}

double distance(const shapes::Point& a, const shapes::Point& b) {
    double dx = a.x - b.x;
    double dy = a.y - b.y;
    return std::sqrt(dx * dx + dy * dy);
}
'''


def test_the_corpus_fixtures_pre_existing_rows_do_not_move(tmp_path) -> None:
    """The shipped ``geometry.cpp`` head, verbatim. Its three class rows and
    two out-of-line method rows carry the line ranges WP-5 corrected; the
    only change this package may make to it is the ADDITION of
    ``geometry.distance``, which is the gap R25 ordered closed."""
    fx = _extract(tmp_path, source=_GEOMETRY_HEAD, fname="geometry.cpp")
    classes = {c.full_name: (c.start_line, c.end_line) for c in _classes(fx)}
    assert classes == {
        "geometry.Circle": (10, 18),
        "geometry.Point": (20, 23),
        "geometry.Box": (25, 29),
    }
    functions = {f.full_name: (f.start_line, f.end_line) for f in _functions(fx)}
    assert functions == {
        "geometry.Circle.area": (37, 39),
        "geometry.Circle.circumference": (41, 43),
        # The one addition — `geometry.cpp:45`, named by line in the corpus
        # README's "Known uncaptured shapes" section until this change.
        "geometry.distance": (45, 49),
    }
    circle = next(c for c in _classes(fx) if c.name == "Circle")
    assert circle.extras is not None
    assert circle.extras["methods"] == ["area", "circumference"], (
        "the constructor is still not captured, so it must still not appear "
        "in the methods list — the graph agrees with itself either way"
    )


# ═══════════════════════════════════════════════════════════════════════════
# TRI-OS (R12 / R14)
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "terminator",
    [pytest.param("\n", id="linux-macos-LF"), pytest.param("\r\n", id="windows-CRLF")],
)
def test_free_function_rows_land_on_the_same_lines_on_every_os(
    terminator: str, tmp_path
) -> None:
    """The line anchor is ``(?:^|\\n)\\s*`` and the type run's separator is
    ``[\\s*&]+``; both admit ``\\r``. Had either been written ``[ \\t]``, a
    Windows checkout would capture nothing at all — and CI, which checks out
    LF, would never see it."""
    source = _POSITIVES.replace("\n", terminator)
    fx = _extract(tmp_path, source=source)
    got = {
        e.full_name: (e.start_line, e.end_line)
        for e in _functions(fx)
        if e.full_name != "positives.norm"
    }
    assert got == _EXPECTED_FUNCTIONS
