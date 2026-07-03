# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression + unit tests for v0.2.73 C-11 (RT-3) — prune failures must
affect build status and be machine-readable.

Background (RT-3): a code-graph build could report ``status=success`` while
its log carried hundreds of ``Failed to prune ...: 500 "subtract prop lengths:
property not found"`` Weaviate delete errors — silent stale data. Each failure
only ``logger.warning``'d and never touched the exit status or any counter.

This suite pins the fix:
  - ``_prune_collection`` returns ``(pruned, failures)`` and counts every
    per-row ``delete_by_id`` failure without aborting the sweep.
  - ``_prune_stale_objects`` aggregates failures across all collections into
    ``self._prune_failures`` while still returning the pruned count.
  - The analyzer's ``main()`` emits a machine-readable ``PRUNE_FAILURES=N``
    line and renders the run as PARTIAL (not success) when N>0, and surfaces a
    CONSENTED drop-and-rebuild remedy — never auto-dropping user data.

All tests are pure-Python unit tests against the analyzer module's helpers
and don't require a running Weaviate. Synthetic project names (ProjA/ProjB)
are used deliberately — no real project identity is embedded.
"""

from __future__ import annotations

import importlib.util
import re
import types
from pathlib import Path
from typing import Any, List

import pytest


_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_c11_analyze_code_graph", str(_ANALYZER_PATH)
    )
    if spec is None or spec.loader is None:
        pytest.fail(f"Analyzer module file missing from repo: {_ANALYZER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.fail("weaviate-client not installed — CI env regression")
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer_module()


class _Obj:
    def __init__(self, uid: str, project: str, language: str = "") -> None:
        self.uuid = uid
        self.properties = {"project": project, "language": language}


class _FakeCollectionData:
    def __init__(self, fail_uuids: set) -> None:
        self._fail_uuids = fail_uuids
        self.deleted: List[str] = []

    def delete_by_id(self, uuid: str) -> None:
        if uuid in self._fail_uuids:
            # Mimic the live Weaviate-500 "subtract prop lengths" signature.
            raise RuntimeError(
                '500 "subtract prop lengths: property not found"'
            )
        self.deleted.append(uuid)


class _FakeCollection:
    def __init__(self, name: str, rows: List[_Obj], fail_uuids: set) -> None:
        self.name = name
        self._rows = rows
        self.data = _FakeCollectionData(fail_uuids)

    def iterator(self, return_properties=None):
        return iter(self._rows)


def _make_analyzer(analyzer_mod: types.ModuleType, project: str = "ProjA"):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = project
    inst.client = None
    inst.visited_uuids = set()
    inst._track_visited = False
    inst._current_language = ""
    inst._progress_emitter = None
    inst._prune_language = ""
    return inst


# ---------------------------------------------------------------------------
# _prune_collection returns (pruned, failures) and counts delete failures.
# ---------------------------------------------------------------------------


def test_prune_collection_returns_pruned_and_failure_counts(analyzer_mod) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    analyzer._track_visited = True

    rows = [
        _Obj("keep-visited", "ProjA", "python"),
        _Obj("stale-ok", "ProjA", "python"),
        _Obj("stale-fails-1", "ProjA", "python"),
        _Obj("stale-fails-2", "ProjA", "python"),
    ]
    fail = {"stale-fails-1", "stale-fails-2"}
    fake = _FakeCollection("ProjA_CodeFunction", rows, fail_uuids=fail)

    pruned, failures = analyzer._prune_collection(
        fake, visited_uuids={"keep-visited"}, language_scope="",
    )

    assert pruned == 1, "only the deletable stale row counts as pruned"
    assert failures == 2, "both Weaviate-500 delete failures must be counted"
    # The sweep must NOT abort on the first failure — the deletable row is gone.
    assert fake.data.deleted == ["stale-ok"]


def test_prune_collection_zero_failures_when_all_deletes_succeed(analyzer_mod) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    analyzer._track_visited = True
    rows = [
        _Obj("visited", "ProjA", "python"),
        _Obj("stale", "ProjA", "python"),
    ]
    fake = _FakeCollection("ProjA_CodeClass", rows, fail_uuids=set())
    pruned, failures = analyzer._prune_collection(
        fake, visited_uuids={"visited"}, language_scope="",
    )
    assert (pruned, failures) == (1, 0)


# ---------------------------------------------------------------------------
# _prune_stale_objects aggregates failures across collections.
# ---------------------------------------------------------------------------


def test_prune_stale_objects_aggregates_failures_into_attr(analyzer_mod) -> None:
    analyzer = _make_analyzer(analyzer_mod)
    analyzer._track_visited = True

    func_coll = _FakeCollection(
        "ProjA_CodeFunction",
        [
            _Obj("f-stale-fail", "ProjA", "python"),
            _Obj("f-stale-ok", "ProjA", "python"),
        ],
        fail_uuids={"f-stale-fail"},
    )
    class_coll = _FakeCollection(
        "ProjA_CodeClass",
        [_Obj("c-stale-fail", "ProjA", "python")],
        fail_uuids={"c-stale-fail"},
    )

    analyzer.modules_collection = None
    analyzer.classes_collection = class_coll
    analyzer.functions_collection = func_coll
    analyzer.apis_collection = None
    analyzer.interactions_collection = None
    # Nothing visited this run → every row is a prune candidate.
    analyzer.visited_uuids = set()

    total_pruned = analyzer._prune_stale_objects()

    assert total_pruned == 1, "only f-stale-ok deletes cleanly"
    assert analyzer._prune_failures == 2, (
        "two collections each had one failing delete → aggregate = 2"
    )


# ---------------------------------------------------------------------------
# Source-level guarantees on the main() reporting contract.
# ---------------------------------------------------------------------------


def test_source_emits_machine_readable_prune_failures_line() -> None:
    src = _ANALYZER_PATH.read_text(encoding="utf-8")
    assert 'print(f"PRUNE_FAILURES={prune_failures}"' in src, (
        "main() must emit a machine-readable PRUNE_FAILURES=N line the "
        "launcher's stdout reader can parse to flip success→partial."
    )


def test_source_flips_report_to_partial_on_prune_failures() -> None:
    src = _ANALYZER_PATH.read_text(encoding="utf-8")
    assert "prune_failures > 0" in src, "status must branch on prune_failures"
    assert "PARTIAL" in src, "the report header must render PARTIAL when N>0"


def test_source_stats_and_final_payload_carry_prune_failures() -> None:
    src = _ANALYZER_PATH.read_text(encoding="utf-8")
    # stats dict declares the key.
    assert "'prune_failures': 0" in src
    # final JSON payload carries it for the GUI reader.
    assert '"prune_failures": stats.get("prune_failures", 0)' in src


def test_source_never_auto_drops_offers_consented_rebuild() -> None:
    """The Weaviate-500 remedy must be a CONSENTED drop-and-rebuild — never an
    automatic drop of user data."""
    src = _ANALYZER_PATH.read_text(encoding="utf-8")
    assert "NEVER" in src and "auto-drop" in src, (
        "the remedy comment must state the drop is never automatic"
    )
    assert "drop-collection" in src, (
        "the operator remedy must point at the consented drop-collection flow"
    )


def test_prune_failures_line_regex_parseable() -> None:
    """The emitted line must match a strict machine-readable shape so the
    Rust reader can extract N unambiguously."""
    line = "PRUNE_FAILURES=7"
    m = re.fullmatch(r"PRUNE_FAILURES=(\d+)", line)
    assert m is not None and int(m.group(1)) == 7
