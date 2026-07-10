# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Integration test: the analyzer's ``create_cross_references`` post-pass wires
a non-Python (rust) function's ``call_names`` through the tree-sitter facade
(CG-2 / v0.2.77 Part 5).

Runs only when the rust grammar (part of the optional ``codegraph-ts`` extra)
is importable — carries a ``skipif`` so the suite stays GREEN in a venv without
the extra. When the extra is absent, the SEPARATE leave-alone assertion below
confirms the same pass writes NO call_names for the rust row (facade → None →
row skipped, exactly as pre-Part-5).
"""
from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _rust_grammar_installed() -> bool:
    if importlib.util.find_spec("tree_sitter") is None:
        return False
    return importlib.util.find_spec("tree_sitter_rust") is not None


def _load_analyzer_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_xref_analyze_code_graph", str(_ANALYZER_PATH)
    )
    if spec is None or spec.loader is None:
        pytest.fail(f"analyzer module missing: {_ANALYZER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.fail("weaviate-client not installed — CI env regression")
    return mod


# --- minimal Weaviate stand-ins ------------------------------------------


class _FakeObj:
    def __init__(self, props: Dict[str, Any]) -> None:
        self.properties = props


class _FakeQuery:
    def __init__(self, rows: Dict[str, Dict[str, Any]]) -> None:
        self._rows = rows

    def fetch_object_by_id(self, uuid: str) -> Optional[_FakeObj]:
        props = self._rows.get(str(uuid))
        return _FakeObj(props) if props is not None else None


class _FakeData:
    """Records call_names updates + reference_add calls."""

    def __init__(self) -> None:
        self.updates: Dict[str, Dict[str, Any]] = {}
        self.refs: List[tuple] = []

    def update(self, uuid: str, properties: Dict[str, Any]) -> None:
        self.updates.setdefault(str(uuid), {}).update(properties)

    def reference_add(self, from_uuid: str, from_property: str, to: str) -> None:
        self.refs.append((str(from_uuid), from_property, str(to)))


class _FakeFunctions:
    def __init__(self, rows: Dict[str, Dict[str, Any]]) -> None:
        self.name = "P_CodeFunction"
        self.query = _FakeQuery(rows)
        self.data = _FakeData()


def _wire(analyzer_mod: types.ModuleType, rows: Dict[str, Dict[str, Any]],
          full_to_uuid: Dict[str, str]) -> Any:
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.client = object()
    inst.function_cache = dict(full_to_uuid)
    inst.class_cache = {}
    inst.module_cache = {}
    inst.module_imports = {}
    inst.functions_collection = _FakeFunctions(rows)
    # Side-dicts the R4 read-amp path reads; empty = "nothing stored yet".
    inst._xref_file_path = {}
    inst._xref_stored_calls = {}
    inst._xref_stored_ncallers = {}
    # Stub the whole-collection scan — we seeded the caches directly.
    inst._populate_caches_from_weaviate = lambda: None  # type: ignore[assignment]
    return inst


# A rust caller ``mymod.caller`` whose body calls ``helper`` (also a known
# function) + a stdlib-ish ``println`` (unresolved → no edge).
_RUST_BODY = "fn caller() { helper(); other::thing(); println!(\"x\"); }"
_ROWS = {
    "u-caller": {
        "full_name": "mymod.caller",
        "function_body": _RUST_BODY,
        "language": "rust",
        "total_chunks": 1,
    },
    "u-helper": {
        "full_name": "mymod.helper",
        "function_body": "fn helper() {}",
        "language": "rust",
        "total_chunks": 1,
    },
}
_FULL_TO_UUID = {"mymod.caller": "u-caller", "mymod.helper": "u-helper"}


@pytest.mark.skipif(
    not _rust_grammar_installed(),
    reason="codegraph-ts extra not installed (tree_sitter_rust absent)",
)
def test_rust_call_names_written_through_facade() -> None:
    """ACT: with the rust grammar installed, the cross-ref pass extracts the
    rust body's calls into call_names AND resolves ``helper`` to a calls ref."""
    mod = _load_analyzer_module()
    inst = _wire(mod, _ROWS, _FULL_TO_UUID)

    stats = inst.create_cross_references(changed_files=None)  # whole-repo scope

    written = inst.functions_collection.data.updates.get("u-caller", {})
    call_names = written.get("call_names")
    assert call_names is not None, "rust caller got no call_names write"
    # helper + thing + println are the extracted names (order-preserving).
    assert "helper" in call_names
    assert "thing" in call_names
    # The resolvable call (helper → u-helper) became a calls reference edge.
    assert ("u-caller", "calls", "u-helper") in inst.functions_collection.data.refs
    assert stats["calls"] >= 1


def test_rust_leave_alone_when_extra_absent() -> None:
    """LEAVE-ALONE: without the rust grammar, the SAME pass writes NO call_names
    for the rust row (facade → None → row skipped), exactly as pre-Part-5.

    Runs only when the extra is genuinely absent; when present the act-test
    above covers the positive path."""
    if _rust_grammar_installed():
        pytest.skip("extra installed — act path covered by the sibling test")
    mod = _load_analyzer_module()
    inst = _wire(mod, _ROWS, _FULL_TO_UUID)

    stats = inst.create_cross_references(changed_files=None)

    # No call_names write for the rust caller, and no calls edge created.
    assert "u-caller" not in inst.functions_collection.data.updates or (
        "call_names" not in inst.functions_collection.data.updates["u-caller"]
    )
    assert stats["calls"] == 0


def test_python_row_unaffected_by_wiring() -> None:
    """A Python row still extracts calls via the ast path regardless of the
    extra (dependency-free) — the wiring never regressed Python."""
    mod = _load_analyzer_module()
    rows = {
        "u-pyc": {
            "full_name": "pymod.caller",
            "function_body": "def caller():\n    helper()\n    print('x')\n",
            "language": "python",
            "total_chunks": 1,
        },
        "u-pyh": {
            "full_name": "pymod.helper",
            "function_body": "def helper():\n    pass\n",
            "language": "python",
            "total_chunks": 1,
        },
    }
    full_to_uuid = {"pymod.caller": "u-pyc", "pymod.helper": "u-pyh"}
    inst = _wire(mod, rows, full_to_uuid)

    inst.create_cross_references(changed_files=None)

    written = inst.functions_collection.data.updates.get("u-pyc", {})
    call_names = written.get("call_names")
    assert call_names == ["helper"], f"python ast path changed: {call_names}"
    assert ("u-pyc", "calls", "u-pyh") in inst.functions_collection.data.refs
