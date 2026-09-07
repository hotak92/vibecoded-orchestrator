# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-5b — the four Ruby defects, at the extractor.

WHY THIS EXISTS
---------------
Four independent silent data losses, all of them ratified by the shipped
golden corpus, all of them visible in ``ledger.rb``:

  1. every class and every method body ran to END-OF-FILE (the brace scanner
     on a language with no braces);
  2. the per-class ``methods`` list was built over the WHOLE FILE, so all three
     classes carried the identical six-name list — the V52-O.11.F antipattern
     that was closed for Rust, Java, C#, Go and JS in v0.2.52 and never for
     Ruby;
  3. a REOPENED class kept only its LAST definition, because ``class_info``
     was a dict keyed by name — ``ledger.rb`` declares ``class Account`` twice
     and stored ONE row, starting at the second, so the first definition's
     body and its three methods were unreachable;
  4. a class at any INDENT matched nothing, because the pattern was anchored
     at column 0 — which means ``module X`` wrapping ``class Y``, the
     commonest Ruby file shape there is, contributed no class row at all.

Defect 3's fix makes a fifth answerable: with real class ranges, a method can
finally be attributed by CONTAINMENT instead of by "nearest preceding
declaration", so a top-level ``def`` written after a class's ``end`` stops
being a member of that class.

WHAT IS PINNED
--------------
* the ACT for each of the five, asserted against the fixture SOURCE (the line
  the row claims really is the declaration) rather than against a snapshot;
* the LEAVE-ALONE: the two rows of a reopened class are DISTINCT entities with
  distinct bodies (the ``fn reset`` loss class, for a class); a method already
  correctly attributed keeps its name; a file with no reopening still yields
  exactly one row per class.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vco_lib.codegraph_entities import KIND_CLASS, KIND_FUNCTION
from vco_lib.codegraph_lang.ruby import _ruby_methods_for_class, extract_ruby_file

_FIXTURE = (
    Path(__file__).parent
    / "fixtures" / "codegraph_golden" / "repo" / "src" / "ledger.rb"
)


class _Helpers:
    project_name = "RubyProj"

    def embed_class(self, *_a: Any, **_k: Any) -> None:
        return None

    def embed_function(self, *_a: Any, **_k: Any) -> None:
        return None

    def generate_embedding(self, *_a: Any, **_k: Any) -> None:
        return None


def _extract(source: str, tmp_path, name: str = "ledger.rb"):
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    return extract_ruby_file(source, target, tmp_path, _Helpers())


@pytest.fixture(scope="module")
def ledger_source() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


@pytest.fixture()
def ledger(ledger_source, tmp_path):
    return _extract(ledger_source, tmp_path)


def _classes(fx):
    return [e for e in fx.entities if e.kind == KIND_CLASS]


def _functions(fx):
    return [e for e in fx.entities if e.kind == KIND_FUNCTION]


# ═══════════════════════════════════════════════════════════════════════════
# DEFECT 1 — bodies ran to end of file
# ═══════════════════════════════════════════════════════════════════════════
def test_no_ruby_entity_body_runs_to_the_end_of_the_file(ledger, ledger_source) -> None:
    """Before this change EVERY row in this file ended at line 40 of 40."""
    lines = ledger_source.split("\n")
    eof = len(lines)
    for e in ledger.entities:
        if e.kind not in (KIND_CLASS, KIND_FUNCTION):
            continue
        assert e.end_line is not None and e.end_line < eof, (
            f"{e.full_name} ends at {e.end_line}, the last line of the file"
        )
        assert lines[e.end_line - 1].strip().startswith("end") or e.start_line == e.end_line, (
            f"{e.full_name}: end_line {e.end_line} is "
            f"{lines[e.end_line - 1]!r}, not an `end`"
        )


def test_every_ruby_row_body_is_exactly_the_slice_it_claims(ledger, ledger_source) -> None:
    lines = ledger_source.split("\n")
    for e in ledger.entities:
        if e.kind not in (KIND_CLASS, KIND_FUNCTION):
            continue
        assert e.body == "\n".join(lines[e.start_line - 1:e.end_line])
        assert e.name in "\n".join(lines[e.start_line - 1:e.start_line + 1])


def test_a_modifier_if_does_not_extend_the_method_body(ledger) -> None:
    """``ledger.rb:47`` is ``return 0 if amount.nil?``. Counting that ``if`` as
    a block opener runs ``store`` on to the next unmatched ``end``."""
    store = next(e for e in _functions(ledger) if e.name == "store")
    assert (store.start_line, store.end_line) == (46, 49)


def test_an_endless_method_is_a_one_line_entity(ledger) -> None:
    total = next(e for e in _functions(ledger) if e.name == "total")
    assert (total.start_line, total.end_line) == (52, 52)
    assert (total.body or "").strip() == "def total = @total"


# ═══════════════════════════════════════════════════════════════════════════
# DEFECT 2 — the methods list was the whole file
# ═══════════════════════════════════════════════════════════════════════════
def test_each_class_lists_only_its_own_methods(ledger) -> None:
    by_name = {}
    for c in _classes(ledger):
        by_name.setdefault(c.name, c.extras.get("methods"))
    assert by_name["Accounting"] == ["version"]
    assert by_name["SavingsAccount"] == ["apply_interest"]
    assert by_name["Vault"] == ["store", "total"]
    # The union across BOTH declarations of a reopened class — the C# partial
    # shape this helper deliberately mirrors.
    assert by_name["Account"] == ["initialize", "deposit", "default", "withdraw?"]


def test_the_methods_list_is_no_longer_the_same_for_every_class(ledger) -> None:
    """The corpus symptom, asserted directly: three classes, one identical
    six-name list."""
    lists = [tuple(c.extras.get("methods") or []) for c in _classes(ledger)]
    assert len(set(lists)) > 1


def test_a_method_of_another_class_is_absent(ledger) -> None:
    accounting = next(c for c in _classes(ledger) if c.name == "Accounting")
    assert "apply_interest" not in (accounting.extras.get("methods") or [])
    assert "withdraw?" not in (accounting.extras.get("methods") or [])


def test_the_scoped_helper_returns_empty_for_a_name_with_no_block(ledger_source) -> None:
    """LEAVE-ALONE: same "declaration whose body is elsewhere" answer
    ``_csharp_methods_for_class`` documents."""
    assert _ruby_methods_for_class(ledger_source, "Nonexistent", ledger_source.split("\n")) == []


# ═══════════════════════════════════════════════════════════════════════════
# DEFECT 3 — a reopened class kept only its last definition
# ═══════════════════════════════════════════════════════════════════════════
def test_a_reopened_class_produces_one_row_per_declaration(ledger) -> None:
    accounts = [c for c in _classes(ledger) if c.name == "Account"]
    assert len(accounts) == 2, "both `class Account` blocks must produce a row"
    assert sorted((c.start_line, c.end_line) for c in accounts) == [(14, 26), (29, 33)]


def test_the_two_rows_of_a_reopened_class_carry_DIFFERENT_text(ledger) -> None:
    """The ``fn reset`` loss class, for a class: two rows that carry the same
    body would be the clobber wearing a disguise."""
    bodies = {(c.body or "") for c in _classes(ledger) if c.name == "Account"}
    assert len(bodies) == 2
    assert any("def initialize(balance)" in b for b in bodies)
    assert any("def withdraw?(amount)" in b for b in bodies)


def test_the_first_definitions_methods_are_reachable_again(ledger) -> None:
    """``initialize`` / ``deposit`` / ``default`` are declared in the FIRST
    ``class Account`` block, which the dict discarded."""
    fns = {f.full_name for f in _functions(ledger)}
    assert {"Account.initialize", "Account.deposit", "Account.default"} <= fns


def test_a_file_with_no_reopening_still_yields_exactly_one_row_per_class(tmp_path) -> None:
    """LEAVE-ALONE: the list shape must not duplicate anything on ordinary
    input."""
    src = "class A\n  def a\n  end\nend\n\nclass B\n  def b\n  end\nend\n"
    fx = _extract(src, tmp_path, "pair.rb")
    assert [c.full_name for c in _classes(fx)] == ["pair.A", "pair.B"]


# ═══════════════════════════════════════════════════════════════════════════
# DEFECT 4 — an indented class matched nothing
# ═══════════════════════════════════════════════════════════════════════════
def test_an_indented_class_inside_a_module_is_extracted(ledger) -> None:
    summary = next(c for c in _classes(ledger) if c.name == "Summary")
    assert (summary.start_line, summary.end_line) == (59, 63)
    assert summary.full_name == "ledger.Summary"


def test_the_commonest_ruby_file_shape_yields_both_types(tmp_path) -> None:
    src = "module Api\n  class Client\n    def get(u)\n      u\n    end\n  end\nend\n"
    fx = _extract(src, tmp_path, "api.rb")
    assert {c.full_name for c in _classes(fx)} == {"api.Api", "api.Client"}
    assert {f.full_name for f in _functions(fx)} == {"Client.get"}


def test_the_class_anchor_does_not_cross_a_newline(tmp_path) -> None:
    """The trailing anchor uses ``[ \\t]``, not ``\\s``: under MULTILINE ``\\s``
    matches a NEWLINE, which is how ``powershell.py`` came to start every
    function on the preceding blank line."""
    src = "class Only\n\n  def a\n  end\nend\n"
    fx = _extract(src, tmp_path, "only.rb")
    assert [(c.name, c.start_line) for c in _classes(fx)] == [("Only", 1)]


# ═══════════════════════════════════════════════════════════════════════════
# CONSEQUENCE — enclosing by containment, not by proximity
# ═══════════════════════════════════════════════════════════════════════════
def test_a_top_level_method_is_not_a_member_of_the_last_class(ledger) -> None:
    """``ledger.rb:68 def audit`` is written after every class has closed.
    "Nearest preceding declaration" made it ``Summary.audit``."""
    audit = next(f for f in _functions(ledger) if f.name == "audit")
    assert audit.full_name == "ledger.audit"


def test_a_method_inside_a_class_keeps_its_class(ledger) -> None:
    """LEAVE-ALONE: containment must not orphan the ordinary case."""
    fns = {f.full_name for f in _functions(ledger)}
    assert {
        "Accounting.version",
        "Account.withdraw?",
        "SavingsAccount.apply_interest",
        "Vault.store",
        "Summary.render",
    } <= fns


def test_the_reopened_blocks_methods_are_attributed_to_the_right_block(ledger) -> None:
    withdraw = next(f for f in _functions(ledger) if f.name == "withdraw?")
    assert withdraw.full_name == "Account.withdraw?"
    assert (withdraw.start_line, withdraw.end_line) == (30, 32)


# ═══════════════════════════════════════════════════════════════════════════
# The module summary stays a list of DISTINCT names
# ═══════════════════════════════════════════════════════════════════════════
def test_reopening_does_not_list_a_class_twice_in_the_module_summary(ledger) -> None:
    summary = ledger.module.module_summary
    classes_line = next(ln for ln in summary.split("\n") if ln.startswith("Classes:"))
    names = [n.strip() for n in classes_line[len("Classes:"):].split(",")]
    assert names == ["Accounting", "Account", "SavingsAccount", "Vault", "Reporting", "Summary"]
    assert len(names) == len(set(names))
