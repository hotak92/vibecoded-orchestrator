# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-5c — the NEGATIVE SPACE of the C++ free-function capture.

WHY THIS EXISTS
---------------
WP-5b DECLINED to capture C++ free functions, and its reasoning was right
about the risk even though the user (R25) overruled the disposition: the
failure mode of a return-type-shaped pattern is SPURIOUS ROWS, which is the
exact defect class v0.2.92 spent itself REMOVING from the C# extractor —
rows minted from ``record Item(...)`` and ``return new Item(...)``, a
conversion operator stored under the target type's name, a primary
constructor minting a method. C++ has strictly more shapes that look like a
definition than C# does, and the pattern runs over every ``.cpp``/``.h`` in
every indexed project: the largest blast radius in the codebase.

So the deliverable is this file, and the capture is what it permits. Every
construct below is a shape a naive pattern mistakes for a function
definition; each one names the identifier it would have minted. A capture
that produces junk rows is worse than the honest gap it replaces.

WRITTEN BEFORE THE EXTRACTOR CHANGE, DELIBERATELY. Before the change these
assertions pass TRIVIALLY (``cpp.py`` captured only out-of-line
``Class::method`` definitions, so nothing here could mint anything). That is
why :func:`test_the_negative_fixture_is_actually_parsed` exists and is
strict — an absence-assertion over a fixture that silently stopped being
parsed is the "test encoded the loss" antipattern, one layer down. The
positive control pins rows that are TRUE ON BOTH SIDES of the change, so
this whole file is a LEAVE-ALONE suite: identical result before and after.

WHAT IS PINNED
--------------
* the LEAVE-ALONE: eleven shapes that must never mint a free-function row,
  each asserted by the identifier it would carry;
* two generic junk detectors that catch a spurious row nobody enumerated —
  no row may begin on a control-flow header, and every row must begin on a
  line that carries its own name;
* the out-of-line ``Class::method`` definition is captured EXACTLY ONCE, by
  ``method_pattern``, and never re-captured as a free function;
* the ONE documented gap this package deliberately leaves open (an
  out-of-line constructor with a member-initialiser list), labelled so that
  reversing the decision changes exactly one assertion.
"""
from __future__ import annotations

from typing import Any, List, Set

import pytest

from vco_lib.codegraph_entities import KIND_CLASS, KIND_FUNCTION
from vco_lib.codegraph_lang.cpp import extract_cpp_file


class _Helpers:
    project_name = "NegativeSpaceProj"

    def embed_class(self, *_a: Any, **_k: Any) -> None:
        return None

    def embed_function(self, *_a: Any, **_k: Any) -> None:
        return None

    def generate_embedding(self, *_a: Any, **_k: Any) -> None:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# THE ANTI-FIXTURE
#
# Every shape here is legal C++ that a return-type-shaped pattern can read as
# `<type> <name>(<args>) {`. The inline comments name the identifier each one
# would mint; the parametrised test below asserts each of those names is
# absent from the extraction.
# ═══════════════════════════════════════════════════════════════════════════
_NEGATIVES = r'''// Negative-space fixture for the C++ free-function capture.

#include <algorithm>
#include <stdexcept>
#include <vector>

// (9) A function-like macro DEFINITION, and one whose body is a definition
//     living on a backslash-continuation line.
#define DECLARE_MODULE(name) static const char* kModule_##name = #name
#define REQUIRE_POSITIVE(x) ((void)(x))
#define DEFINE_TRIVIAL_FN            \
    int macro_bodied_fn() {          \
        return 1;                    \
    }

// (9) ...and a macro INVOCATION that looks exactly like a call.
DECLARE_MODULE(negatives);

// (4) `extern "C" {` opens a block with no parentheses at all.
extern "C" {
// (8) A prototype inside it.
int c_linkage_declared(int x);
}

// (8) Function DECLARATIONS (prototypes). Their definitions live in another
//     translation unit; capturing them would double-count every function in
//     any project whose headers the analyzer also walks.
int declared_only(int x);
static bool also_declared(const char* s);

struct Gadget {
    // (8) In-class prototypes, and a pure-virtual.
    Gadget(int n);
    int total() const;
    virtual void pure_virtual() = 0;

    int count_;
    std::vector<int> items_;
};

// (6) An out-of-line constructor with a member-initialiser list, split
//     across lines. `count_(n)` and `items_(n, 0)` are the classic bait:
//     each is `<name>(<args>)` and the second is followed by ` {}`.
Gadget::Gadget(int n)
    : count_(n),
      items_(n, 0) {}

// (7) An out-of-line member definition. Captured — but by `method_pattern`,
//     exactly once, and never as a free function named `total`.
int Gadget::total() const {
    return count_;
}

// (10) A `template<...>` clause on its own line, over a TYPE. It must fold
//      into the type's declaration and never stand as a row of its own.
template <typename T>
struct Holder {
    T value;
};

int driver(const std::vector<int>& values, Gadget& gadget) {
    // (5) Brace-initializer lists, and function-style initialization —
    //     `sized(4, 0)` is syntactically a function declaration (C++'s most
    //     vexing parse) and is why `;` may never be an accepted terminator.
    std::vector<int> primes = {2, 3, 5};
    std::vector<int> sized(4, 0);
    Gadget* held{nullptr};

    // (3) A lambda assigned to a variable, and one passed inline.
    auto add = [](int a, int b) { return a + b; };
    std::sort(primes.begin(), primes.end(), [](int a, int b) { return a > b; });

    int total = 0;
    // (2) A `return f(x);` call.
    if (values.empty()) {
        return add(0, 0);
    } else if (values.size() == 1) {
        total = values[0];
    }
    // (1) Control-flow headers: for / while / do / switch / try / catch.
    for (std::size_t i = 0; i < values.size(); ++i) {
        total += values[i];
    }
    while (total > 100) {
        total -= 1;
    }
    do {
        total += 1;
    } while (total < 0);
    switch (total) {
        case 0: {
            total = 1;
            break;
        }
        default:
            break;
    }
    try {
        REQUIRE_POSITIVE(total);
    } catch (const std::exception& e) {
        total = -1;
    }
    // (1) A range-based `for` whose range is a qualified call, and an `if`
    //     whose condition is one — both end in `) {` like a definition.
    for (const auto& v : std::vector<int>(sized)) {
        total += v;
    }
    if (static_cast<int>(primes.size()) > 0) {
        total += 1;
    }
    return add(total, gadget.total());
}
'''

#: Identifier a naive pattern mints, keyed by the shape that produces it.
#: Each entry is one line of the anti-fixture above.
_FORBIDDEN = [
    pytest.param("if-header", "if", id="if-header"),
    pytest.param("else-if-header", "else", id="else-if-header"),
    pytest.param("for-header", "for", id="for-header"),
    pytest.param("while-header", "while", id="while-header"),
    pytest.param("switch-header", "switch", id="switch-header"),
    pytest.param("catch-header", "catch", id="catch-header"),
    pytest.param("return-call", "add", id="return-f-x-call"),
    pytest.param("inline-lambda-arg", "sort", id="inline-lambda-in-std-sort"),
    pytest.param("inline-lambda-arg", "begin", id="inline-lambda-begin"),
    pytest.param("inline-lambda-arg", "end", id="inline-lambda-end"),
    pytest.param("extern-C-block", "c_linkage_declared", id="extern-C-prototype"),
    pytest.param("prototype", "declared_only", id="prototype-plain"),
    pytest.param("prototype", "also_declared", id="prototype-static"),
    pytest.param("prototype", "pure_virtual", id="prototype-pure-virtual"),
    pytest.param("member-init-list", "count_", id="member-init-list-first"),
    pytest.param("member-init-list", "items_", id="member-init-list-braced"),
    pytest.param("brace-init", "primes", id="brace-initializer-list"),
    pytest.param("function-style-init", "sized", id="most-vexing-parse"),
    pytest.param("brace-init", "held", id="braced-pointer-init"),
    pytest.param("macro-invocation", "DECLARE_MODULE", id="macro-invocation"),
    pytest.param("macro-invocation", "REQUIRE_POSITIVE", id="macro-call-statement"),
    pytest.param("macro-definition", "macro_bodied_fn", id="macro-continuation-body"),
    pytest.param("template-clause", "Holder", id="template-clause-over-a-type"),
    pytest.param("template-clause", "T", id="template-parameter"),
    pytest.param("functional-cast", "static_cast", id="functional-cast"),
]

#: Words that may never OPEN a captured row's declaration line. A row whose
#: body begins with one of these is a control-flow header wearing a function's
#: clothes — the generic detector for a shape nobody enumerated above.
_CONTROL_OPENERS = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "default", "try",
    "catch", "return", "throw", "goto", "break", "continue", "namespace",
    "class", "struct", "union", "enum", "using", "typedef", "template",
    "public", "private", "protected", "extern",
})


def _extract(tmp_path, source: str = _NEGATIVES, fname: str = "negatives.cpp"):
    target = tmp_path / fname
    target.write_text(source, encoding="utf-8")
    return extract_cpp_file(source, target, tmp_path, _Helpers())


def _functions(fx) -> List[Any]:
    return [e for e in fx.entities if e.kind == KIND_FUNCTION]


def _classes(fx) -> List[Any]:
    return [e for e in fx.entities if e.kind == KIND_CLASS]


def _function_names(fx) -> Set[str]:
    return {e.name for e in _functions(fx)}


# ═══════════════════════════════════════════════════════════════════════════
# POSITIVE CONTROL — the reason the absences below mean anything
# ═══════════════════════════════════════════════════════════════════════════
def test_the_negative_fixture_is_actually_parsed(tmp_path) -> None:
    """An absence-assertion over a fixture nothing parses is vacuous.

    Every row asserted here is true BOTH BEFORE AND AFTER the free-function
    capture landed, so this control does not move with the feature it
    guards: the module descriptor, the two type rows the class pattern has
    always produced, and the one out-of-line member row ``method_pattern``
    has always produced.
    """
    fx = _extract(tmp_path)
    assert fx.module is not None, "no module descriptor — the file was skipped"
    assert fx.module.language == "C++"
    assert fx.module.path == "negatives.cpp"
    assert "algorithm" in fx.module.imports and "vector" in fx.module.imports

    class_names = {c.name for c in _classes(fx)}
    assert {"Gadget", "Holder"} <= class_names, (
        "the class pattern stopped seeing this fixture — every absence "
        "asserted in this file would then be vacuously true"
    )
    assert "total" in _function_names(fx), (
        "the out-of-line `Gadget::total` row is the proof that the FUNCTION "
        "half of the extractor ran over this source at all"
    )


# ═══════════════════════════════════════════════════════════════════════════
# THE NEGATIVE SPACE, shape by shape
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("shape,identifier", _FORBIDDEN)
def test_a_non_definition_shape_mints_no_function_row(
    shape: str, identifier: str, tmp_path
) -> None:
    """Each C++ shape that reads like ``<type> <name>(<args>) {`` to a naive
    pattern, asserted by the identifier it would have carried."""
    fx = _extract(tmp_path)
    names = _function_names(fx)
    assert identifier not in names, (
        f"{shape}: a `{identifier}` function row was minted. This is the "
        "spurious-row failure mode WP-5b declined the capture over and R25 "
        "made the acceptance bar — see the shape marked in _NEGATIVES."
    )


def test_no_captured_row_opens_on_a_control_flow_keyword(tmp_path) -> None:
    """Generic detector for a spurious shape nobody enumerated.

    A real definition's declaration line begins with a return type or a
    modifier. Every junk row this package can mint begins with a statement
    or block keyword instead, so the keyword set catches the unenumerated
    case without anyone having to predict it.
    """
    fx = _extract(tmp_path)
    lines = _NEGATIVES.split("\n")
    for e in _functions(fx) + _classes(fx):
        assert e.start_line is not None
        first_word = lines[e.start_line - 1].strip().split("(")[0].strip()
        opener = first_word.split()[0] if first_word.split() else ""
        assert opener.rstrip(":") not in _CONTROL_OPENERS or opener in (
            "struct", "class", "template",
        ), (
            f"{e.kind} {e.name}: declaration line {e.start_line} opens on "
            f"{opener!r} — a control-flow header captured as an entity"
        )


def test_every_captured_row_begins_on_a_line_that_carries_its_name(tmp_path) -> None:
    """The fidelity property, restated over the anti-fixture.

    ``tests/test_v0292_wp5_entity_line_fidelity.py`` asserts this per
    language over clean sources. Repeating it HERE is what makes it bite:
    a spurious row's ``start_line`` lands on whatever line the mis-match
    began on, which is exactly where the name is not.
    """
    fx = _extract(tmp_path)
    lines = _NEGATIVES.split("\n")
    for e in _functions(fx) + _classes(fx):
        assert e.start_line is not None and e.end_line is not None
        assert 1 <= e.start_line <= e.end_line <= len(lines), (
            f"{e.name}: range {e.start_line}..{e.end_line} outside 1..{len(lines)}"
        )
        window = "\n".join(lines[e.start_line - 1:e.start_line + 1])
        assert e.name in window, (
            f"{e.kind} {e.name}: start_line {e.start_line} is "
            f"{lines[e.start_line - 1]!r} and does not carry the name"
        )
        assert (e.body or "").split("\n")[0] == lines[e.start_line - 1], (
            f"{e.kind} {e.name}: body does not begin at start_line"
        )


def test_the_out_of_line_member_is_captured_exactly_once(tmp_path) -> None:
    """``Class::method`` is ``method_pattern``'s property and no other's.

    Double-counting it — one row from the member pattern and one from the
    free-function pattern — would be a DATA defect rather than a visible
    junk row, so it is asserted by count and by ``full_name`` rather than by
    absence.
    """
    fx = _extract(tmp_path)
    totals = [e for e in _functions(fx) if e.name == "total"]
    assert len(totals) == 1, (
        f"`Gadget::total` produced {len(totals)} rows — the free-function "
        "pattern re-captured a qualified definition"
    )
    assert totals[0].full_name == "negatives.Gadget.total", (
        "the out-of-line member must stay attributed to its class"
    )
    assert "negatives.total" not in {e.full_name for e in _functions(fx)}


def test_an_out_of_line_constructor_with_a_member_init_list_has_no_row(
    tmp_path,
) -> None:
    """THE ONE DOCUMENTED GAP THIS PACKAGE LEAVES OPEN — labelled.

    ``Gadget::Gadget(int n) : count_(n), items_(n, 0) {}`` produces no row.
    It is an out-of-line MEMBER, not a free function, so it belongs to
    ``method_pattern``, whose ``\\)\\s*(?:const)?…\\{`` tail the
    member-initialiser list breaks. R25 ruled on free functions; this half
    of the pre-existing README gap is reported with its recipe rather than
    fixed here, because the same fixture line is also the bait for
    ``count_`` / ``items_`` and the two decisions must not be entangled in
    one change.

    This is the assertion to INVERT if that call is reversed — it is the
    only one in this file that encodes the gap rather than the guard, and
    the ``count_`` / ``items_`` absences above stay correct either way.
    """
    fx = _extract(tmp_path)
    ctors = [e for e in _functions(fx) if e.name == "Gadget"]
    assert ctors == [], (
        "an out-of-line constructor now produces a row — intended? then "
        "update the corpus README's 'Known uncaptured shapes' section in "
        "the same change (R16), and invert this test"
    )


# ═══════════════════════════════════════════════════════════════════════════
# TRI-OS (R12 / R14)
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "terminator",
    [pytest.param("\n", id="linux-macos-LF"), pytest.param("\r\n", id="windows-CRLF")],
)
def test_the_negative_space_holds_under_both_line_terminators(
    terminator: str, tmp_path
) -> None:
    """The only OS-dependent input to a text extractor is the line
    terminator: a Windows checkout is CRLF, CI checks out LF. A guard that
    keyed on ``[ \\t]`` rather than ``\\s`` would admit every one of these
    shapes on Windows only, where no CI run would ever see it."""
    source = _NEGATIVES.replace("\n", terminator)
    fx = _extract(tmp_path, source=source)
    names = _function_names(fx)
    for _shape, identifier in ((p.values[0], p.values[1]) for p in _FORBIDDEN):
        assert identifier not in names, (
            f"{identifier!r} is captured under {terminator!r} only — a "
            "terminator-sensitive guard is invisible to CI and wrong on "
            "every Windows install"
        )
    assert "total" in names, "the CRLF fixture stopped parsing entirely"
