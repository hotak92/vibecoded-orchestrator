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
import os

import pytest

from tests._codegraph_truth_harness import (
    EXTRACTORS,
    THRESHOLD,
    fixture_files,
    force_regex_definitions,
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


def _require_grammar(grammar: str) -> None:
    """Skip where the extra is legitimately absent — but on CI (``CI`` set)
    a missing grammar is a broken runner (ci.yml installs ``.[codegraph-ts]``),
    and a skip there would let the gate pass vacuously: FAIL instead."""
    if codegraph_grammars.load_language(grammar) is not None:
        return
    msg = f"tree-sitter grammar for {grammar} not installed (codegraph-ts extra)"
    if os.environ.get("CI"):
        pytest.fail(msg + " — CI must install .[codegraph-ts]; the gate cannot skip there")
    pytest.skip(msg)


@pytest.mark.parametrize("lang", sorted(EXTRACTORS))
def test_definitions_meet_precision_and_recall(lang: str) -> None:
    _extractor, grammar = EXTRACTORS[lang]
    _require_grammar(grammar)
    precision, recall, tp, n_ext, n_truth = score(lang)
    assert precision >= THRESHOLD and recall >= THRESHOLD, (
        f"{lang}: precision {precision:.3f}, recall {recall:.3f} "
        f"({tp} matched of {n_ext} extracted / {n_truth} expected) — below "
        f"the {THRESHOLD:.0%} correctness bar"
    )


# The gate's DISCRIMINATING power, locked: the regex fallback (what the layer
# replaced) must stay BELOW the bar on the measured-defect languages. If a
# change ever makes these pass, either the fixtures lost their teeth or the
# truth drifted toward the regex output — both need a human look.
@pytest.mark.parametrize("lang,metric", [
    ("typescript", "recall"),    # generic functions / interfaces invisible
    ("rust", "precision"),       # string-literal phantoms + test code indexed
])
def test_regex_fallback_stays_below_the_bar(lang: str, metric: str, monkeypatch) -> None:
    force_regex_definitions(monkeypatch)
    precision, recall, *_ = score(lang)
    value = precision if metric == "precision" else recall
    assert value < THRESHOLD, (
        f"{lang} regex {metric} {value:.3f} reached the {THRESHOLD:.0%} bar — "
        "the correctness gate no longer distinguishes the two extractors"
    )


def test_missing_grammar_fails_on_ci_and_skips_elsewhere(monkeypatch) -> None:
    monkeypatch.setattr(codegraph_grammars, "load_language", lambda lang: None)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception):
        _require_grammar("rust")
    monkeypatch.delenv("CI")
    with pytest.raises(pytest.skip.Exception):
        _require_grammar("rust")
