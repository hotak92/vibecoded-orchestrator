# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Correctness gate for code-graph DEFINITIONS (v0.2.102 W6-E).

The golden suite (``tests/test_codegraph_golden.py``) pins what the analyzer
DOES; it cannot tell a right answer from a wrong one, and it ratified real
defects more than once. This gate scores each language's extractor against
COMMITTED, hand-reviewed ground truth:

* ``tests/fixtures/codegraph_truth/<lang>/<file>`` — the fixture source. The
  measured-defect files are copies of this repo's own code (the launcher's TS
  modules, two Rust command modules, a shipped hook) plus the neutral golden
  corpus files; the ``edge_cases.*`` / ``Registry.java`` / ``EdgeController.cs``
  files were written for this gate and their truth was written BY HAND before
  the extractor ran on them.
* ``<file>.truth.json`` — the expected ``(kind, name, start, end)`` symbols
  under the policies stated in :mod:`vco_lib.codegraph_defs`.

Each language must reach >= 95 % precision AND recall (multiset match on the
full tuple — a wrong line range is a miss). The extractors run their shipped
default, the tree-sitter definitions layer; on the regex fallback TypeScript,
JavaScript and Rust (among others) fall below the bar — that is the defect the
layer exists to fix (red-proof recorded in the W6-E lane report).
"""
from __future__ import annotations

import json

import pytest

from tests._codegraph_truth_harness import (
    EXTRACTORS,
    THRESHOLD,
    fixture_files,
    score,
    truth_path,
)
from vco_lib import codegraph_grammars


@pytest.mark.parametrize("lang", sorted(EXTRACTORS))
def test_every_fixture_has_reviewed_truth(lang: str) -> None:
    files = fixture_files(lang)
    assert files, f"no definitions fixtures for {lang}"
    for fixture in files:
        tp = truth_path(fixture)
        assert tp.exists(), f"{fixture.name} has no committed truth file"
        data = json.loads(tp.read_text(encoding="utf-8"))
        assert data.get("reviewed"), f"{tp.name} carries no review stamp"
        for row in data["symbols"]:
            assert row["kind"] in ("class", "function")
            assert 1 <= row["start"] <= row["end"], row


@pytest.mark.parametrize("lang", sorted(EXTRACTORS))
def test_definitions_meet_precision_and_recall(lang: str) -> None:
    _extractor, grammar = EXTRACTORS[lang]
    if codegraph_grammars.load_language(grammar) is None:
        pytest.skip(f"tree-sitter grammar for {grammar} not installed (codegraph-ts extra)")
    precision, recall, tp, n_ext, n_truth = score(lang)
    assert precision >= THRESHOLD and recall >= THRESHOLD, (
        f"{lang}: precision {precision:.3f}, recall {recall:.3f} "
        f"({tp} matched of {n_ext} extracted / {n_truth} expected) — below "
        f"the {THRESHOLD:.0%} correctness bar"
    )
