# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 (plan item 24): the PRUNE_FAILURES remedy names the REAL
collection(s) and the REAL --project.

Defect under test: the analyzer's end-of-run "PRUNE_FAILURES" remedy always
printed `curl -X DELETE .../v1/schema/<Prefix>_CodeFunction` — a real run had
its failures in `<Prefix>_CodeInteraction`, so the printed command named the
wrong collection. A printed command is shipped code.

Fix under test:
  * ``record_prune_failures`` (vco_lib.codegraph_prune) feeds BOTH the total
    accumulator and a per-collection ledger, at the producers that know the
    collection;
  * ``format_prune_remedy_lines`` renders ONE curl line per failing
    collection (real full class name) + ONE re-run line (real --project);
  * main()'s block routes through the formatter (source-locked, in the style
    of test_codegraph_prune_failure_status.py).
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from vco_lib.codegraph_prune import (
    PRUNE_REMEDY_URL_DEFAULT,
    format_prune_remedy_lines,
    record_prune_failures,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_v02103_prune_remedy_analyzer", str(_ANALYZER_PATH)
    )
    if spec is None or spec.loader is None:
        pytest.fail(f"Analyzer module missing: {_ANALYZER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.fail("weaviate-client not installed — CI env regression")
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer_module()


# ── the formatter (pure) ────────────────────────────────────────────────────

def test_one_failing_collection_names_it_not_codefunction():
    lines = format_prune_remedy_lines(["Acme_CodeInteraction"], "Acme")
    curl_lines = [ln for ln in lines if "curl" in ln]
    assert len(curl_lines) == 1, f"exactly one curl line; got {lines}"
    assert "/v1/schema/Acme_CodeInteraction" in curl_lines[0]
    assert "CodeFunction" not in curl_lines[0], (
        "the hard-coded CodeFunction target is the defect under test"
    )
    assert lines[-1] == (
        "   .claude/scripts/code-graph-analyze . --project Acme --prune-stale"
    )


def test_two_failing_collections_get_two_lines():
    lines = format_prune_remedy_lines(
        ["Acme_CodeInteraction", "Acme_CodeFunction"], "Acme")
    curl_lines = [ln for ln in lines if "curl" in ln]
    assert len(curl_lines) == 2
    named = {ln.split("/v1/schema/")[1].split('"')[0] for ln in curl_lines}
    assert named == {"Acme_CodeInteraction", "Acme_CodeFunction"}


def test_unattributed_failures_keep_the_placeholder_not_a_guess():
    lines = format_prune_remedy_lines([], "Acme")
    curl_lines = [ln for ln in lines if "curl" in ln]
    assert len(curl_lines) == 1 and "<Prefix>_<Class>" in curl_lines[0]
    assert "--project Acme" in lines[-1], (
        "the project name IS known in main() — never a placeholder there"
    )
    assert PRUNE_REMEDY_URL_DEFAULT in curl_lines[0]


# ── the ledger (record_prune_failures) ──────────────────────────────────────

def test_record_prune_failures_builds_total_and_per_collection():
    holder = SimpleNamespace()
    record_prune_failures(holder, "Acme_CodeInteraction", 2)
    record_prune_failures(holder, "Acme_CodeFunction", 1)
    record_prune_failures(holder, "Acme_CodeInteraction", 3)
    assert holder._prune_failures == 6, "the pre-existing total is ADD-only"
    assert holder._prune_failure_collections == {
        "Acme_CodeInteraction": 5, "Acme_CodeFunction": 1,
    }


def test_record_prune_failures_zero_is_a_noop():
    holder = SimpleNamespace(_prune_failures=4,
                             _prune_failure_collections={"A_B": 1})
    record_prune_failures(holder, "A_B", 0)
    assert holder._prune_failures == 4
    assert holder._prune_failure_collections == {"A_B": 1}


# ── the analyzer wiring (runtime, mutating the real call path) ──────────────

class _FailingData:
    def __init__(self, deleted):
        self._deleted = deleted

    def delete_by_id(self, uuid):
        self._deleted.append(uuid)
        raise RuntimeError("Weaviate-500 subtract prop lengths: "
                           "property not found")


class _FailingColl:
    def __init__(self, name, rows, props):
        self.name = name
        self.deleted: list = []
        self.data = _FailingData(self.deleted)
        self.config = types.SimpleNamespace(
            get=lambda: types.SimpleNamespace(
                properties=[SimpleNamespace(name=p) for p in props])
        )
        self._rows = rows

    def iterator(self, return_properties=None):
        return iter(self._rows)


def _make_analyzer(analyzer_mod):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(
        analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = "Acme"
    inst.visited_uuids = set()
    inst._track_visited = True
    inst._prune_discovered_paths = set()
    inst._prune_walked_paths = set()
    inst._prune_language = ""
    inst._refused_extra_sources = None
    inst._strict_source = False
    inst._analyze_repo_root_raw = ""
    return inst


def test_stale_prune_records_failures_per_collection(analyzer_mod):
    """Failures in CodeInteraction ONLY → the ledger names CodeInteraction,
    not CodeFunction (the real incident's shape)."""
    analyzer = _make_analyzer(analyzer_mod)
    inter = _FailingColl(
        "Acme_CodeInteraction",
        [SimpleNamespace(uuid="i-0",
                         properties={"project": "Acme",
                                     "file_path": "src/gone.py"})],
        ["project", "file_path"],
    )
    analyzer.modules_collection = None
    analyzer.classes_collection = None
    analyzer.functions_collection = None
    analyzer.apis_collection = None
    analyzer.interactions_collection = inter

    analyzer._prune_stale_objects()

    assert analyzer._prune_failures == 1
    assert analyzer._prune_failure_collections == {"Acme_CodeInteraction": 1}


def test_orphan_clear_records_failures_per_collection(analyzer_mod):
    """The CG-4 sweep's delete failures land in BOTH ledgers (the second
    producer that knows its collection)."""
    analyzer = _make_analyzer(analyzer_mod)
    coll = _FailingColl(
        "Acme_CodeFunction",
        [SimpleNamespace(uuid="f-0",
                         properties={"project": "Acme",
                                     "file_path": "src/gone.py",
                                     "embed_revision": 7})],
        ["project", "file_path", "embed_revision"],
    )
    # The CG-4 sweep path: _clear_deleted_primary_rows deletes rows whose
    # path is gone from disk, regardless of revision.
    pruned, failures = analyzer._clear_deleted_primary_rows(
        coll, "file_path", _REPO_ROOT, set())
    assert failures == 1 and pruned == 0
    record_prune_failures(analyzer, coll.name, failures)
    assert analyzer._prune_failures == 1
    assert analyzer._prune_failure_collections == {"Acme_CodeFunction": 1}


# ── main()'s block routes through the formatter (source lock) ──────────────

def test_source_remedy_block_uses_the_formatter_and_real_names():
    """Companion to the runtime tests above (same style as
    test_codegraph_prune_failure_status.py): main() must route the curl lines
    through _format_prune_remedy_lines with stats['prune_failure_collections']
    — the hard-coded <Prefix>_CodeFunction curl is the defect."""
    src = _ANALYZER_PATH.read_text(encoding="utf-8")
    assert "_format_prune_remedy_lines(" in src, (
        "main() must render the remedy through the shared formatter"
    )
    assert "_record_prune_failures(self, coll.name, failures)" in src, (
        "the stale prune must attribute failures per collection"
    )
    assert '"/v1/schema/<Prefix>_CodeFunction"' not in src, (
        "the hard-coded CodeFunction curl line must be gone"
    )
    assert "stats.get(\"prune_failure_collections\")" in src


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
