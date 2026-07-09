# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""P2d (v0.2.75): lock-step parity for the shared codegraph property home.

``vco_lib/codegraph_schema.py`` is the ONE home for the additive property
specs that three writer families must agree on:

  1. the analyzer template's ``_ensure_*_property`` helpers (inline —
     the template must run standalone at user sites, so it cannot import
     vco_lib there);
  2. migration edge ``migrations/codegraph_collection/4_to_5.py``
     (embed_revision + chunk props) — imports the shared home DIRECTLY;
  3. migration edge ``5_to_6.py`` (is_test + n_callers) — same shape.

THIS FILE IS THE LOCK. It asserts:
  * the analyzer's ensured ``(class, prop, type)`` set is EQUAL to the
    shared table (probed at runtime through the real methods, not regex);
  * X-1 / v0.2.76 (ruling #1): each edge's inline fallback (``_FALLBACK_SPECS``
    / ``_fallback_ensure``) + the legacy regex prefix-fallback are GONE — the
    edges import the shared home directly and loud-fail on a broken install;
  * the edge subsets jointly cover every prop in the shared table (a new
    table prop without an owning edge fails here — write the edge, then
    add it to ``_EDGE_SUBSETS`` below);
  * unit semantics of ``ensure_codegraph_properties`` (idempotent skip,
    absent-class skip, subset filter, skip-vectorized adds, error carries
    the failing class).

NEW PROPS: add to ``vco_lib/codegraph_schema.py`` FIRST, then mirror the
analyzer helper + the new migration edge until this file passes again.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# The analyzer template imports weaviate_mcp helpers at module level; make
# them importable standalone (matches test_codegraph_metadata_producers).
_MCP_ROOT = REPO_ROOT / "claude_mcp_servers"
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

from vco_lib.codegraph_schema import (  # noqa: E402
    CODEGRAPH_PROPERTY_SPECS,
    CodegraphPropertyEnsureError,
    ensure_codegraph_properties,
    specs_subset,
)

_ANALYZER_PATH = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
_EDGE_4_TO_5 = REPO_ROOT / "migrations" / "codegraph_collection" / "4_to_5.py"
_EDGE_5_TO_6 = REPO_ROOT / "migrations" / "codegraph_collection" / "5_to_6.py"


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ─────────────────────── analyzer ↔ shared-table parity ─────────────────────


class _RecordingColl:
    """Collection fake: empty schema, records every add_property call as
    ``(prop_name, DataType member name)``."""

    def __init__(self) -> None:
        self.added: list = []
        self.config = types.SimpleNamespace(
            get=lambda: types.SimpleNamespace(properties=[]),
            add_property=lambda prop: self.added.append(
                (prop.name, prop.dataType.name, prop.skip_vectorization)
            ),
        )


_ATTR_TO_CLASS = {
    "modules_collection": "CodeModule",
    "classes_collection": "CodeClass",
    "functions_collection": "CodeFunction",
    "apis_collection": "CodeAPI",
    "interactions_collection": "CodeInteraction",
}

# The four analyzer helpers whose scope the shared table owns (the props
# with migration edges). Older additive helpers (import_names, language,
# file_path, content_hash) predate the edge system and are out of scope.
_P2D_ENSURE_METHODS = (
    "_ensure_chunk_props_property",
    "_ensure_embed_revision_property",
    "_ensure_is_test_property",
    "_ensure_n_callers_property",
)


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_module("_p2d_parity_analyzer", _ANALYZER_PATH)


def test_analyzer_ensured_set_equals_shared_table(analyzer_mod):
    """THE LOCK: the analyzer's ensured (class, prop, type) set must be
    EQUAL to vco_lib/codegraph_schema.CODEGRAPH_PROPERTY_SPECS. Probed at
    runtime: the real `_ensure_*` methods run against recording fakes with
    empty schemas, so every spec'd add fires exactly once."""
    fakes = {attr: _RecordingColl() for attr in _ATTR_TO_CLASS}
    stub = types.SimpleNamespace(**fakes)

    for method_name in _P2D_ENSURE_METHODS:
        method = getattr(analyzer_mod.CodeGraphAnalyzer, method_name)
        method(stub)

    ensured = set()
    for attr, coll in fakes.items():
        for prop_name, dtype_name, skip_vec in coll.added:
            assert skip_vec is True, (
                f"{_ATTR_TO_CLASS[attr]}.{prop_name}: every P2d prop is "
                "metadata — skip_vectorization must be True"
            )
            ensured.add((_ATTR_TO_CLASS[attr], prop_name, dtype_name))

    expected = {
        (cls, prop, dtype)
        for cls, specs in CODEGRAPH_PROPERTY_SPECS.items()
        for (prop, dtype, _desc) in specs
    }
    assert ensured == expected, (
        "analyzer _ensure_* helpers and vco_lib/codegraph_schema drifted.\n"
        f"analyzer-only: {sorted(ensured - expected)}\n"
        f"table-only:    {sorted(expected - ensured)}\n"
        "NEW PROPS go into vco_lib/codegraph_schema.py FIRST, then mirror "
        "the analyzer helper + the migration edge."
    )


def test_analyzer_helpers_idempotent_when_props_present(analyzer_mod):
    """All four helpers must skip already-present props (belt-and-suspenders
    re-runs on every analyze must not re-add)."""
    all_props = {
        prop for specs in CODEGRAPH_PROPERTY_SPECS.values()
        for (prop, _t, _d) in specs
    }

    class _HaveAll:
        def __init__(self) -> None:
            self.added: list = []
            self.config = types.SimpleNamespace(
                get=lambda: types.SimpleNamespace(
                    properties=[
                        types.SimpleNamespace(name=n) for n in all_props
                    ]
                ),
                add_property=lambda prop: self.added.append(prop.name),
            )

    fakes = {attr: _HaveAll() for attr in _ATTR_TO_CLASS}
    stub = types.SimpleNamespace(**fakes)
    for method_name in _P2D_ENSURE_METHODS:
        getattr(analyzer_mod.CodeGraphAnalyzer, method_name)(stub)
    for attr, coll in fakes.items():
        assert coll.added == [], f"{attr}: already-present props re-added"


# ─────────────────────── edges ↔ shared-table parity ────────────────────────

# Every edge that owns table props, with its subset attribute. A NEW table
# prop must appear in exactly one NEW (or existing) edge subset — extend
# this tuple when the 7_to_8 edge lands.
_EDGE_SUBSETS = (
    ("4_to_5", _EDGE_4_TO_5, "_V5_PROPS"),
    ("5_to_6", _EDGE_5_TO_6, "_V6_PROPS"),
)


@pytest.mark.parametrize("label,path,subset_attr", _EDGE_SUBSETS)
def test_edge_has_no_inline_fallback_after_x1(label, path, subset_attr):
    """X-1 / v0.2.76 (ruling #1): the edges' inline fallback copies are GONE.
    They import ``vco_lib.codegraph_schema`` directly and fail loudly on a
    broken install rather than silently degrading to an inline copy. Grep-gate
    the removed symbol DEFINITIONS so a regression can't quietly reintroduce a
    silent fallback arm. (Prose mentions in the docstring are fine — we match
    the definition sites, not the word.)"""
    import re as _re

    text = path.read_text(encoding="utf-8")
    banned_defs = {
        "_FALLBACK_SPECS assignment": r"(?m)^_FALLBACK_SPECS\s*=",
        "_fallback_ensure def": r"(?m)^def\s+_fallback_ensure\b",
        # The legacy regex prefix-fallback (ruling #1): the resolver must use
        # canonical_class_prefix and let ImportError propagate.
        "legacy re.sub prefix fallback": r"re\.sub\(",
    }
    for what, pat in banned_defs.items():
        assert not _re.search(pat, text), (
            f"{label}: the {what} is back — the silent inline fallback was "
            "removed in X-1 (v0.2.76). The edge must import "
            "vco_lib.codegraph_schema / codegraph_naming directly and loud-fail "
            "on ImportError."
        )
    # The edge DOES import the shared home directly now.
    assert "from vco_lib.codegraph_schema import" in text
    assert "from vco_lib.codegraph_naming import canonical_class_prefix" in text


@pytest.mark.parametrize("label,path,subset_attr", _EDGE_SUBSETS)
def test_edge_live_path_routes_through_shared_home(label, path, subset_attr):
    """The edges' path (vco_lib importable — every healthy install; the runner
    executes edges with cwd=<project_root>) routes through
    ensure_codegraph_properties with the edge's subset."""
    edge = _load_module(f"_p2d_parity_live_{label}", path)
    subset = set(getattr(edge, subset_attr))
    expected_classes = {
        f"P_{cls}" for cls in specs_subset(subset)
    }
    colls = {name: _UnitColl() for name in expected_classes}
    probed: list = []
    results = edge._ensure_props(_client(colls, probed), "P")
    assert set(results) == expected_classes
    assert set(probed) == expected_classes, (
        "live path must probe exactly the subset's classes "
        "(out-of-scope classes never touched)"
    )
    added = {
        p.name for coll in colls.values() for p in coll.added
    }
    assert added == subset


@pytest.mark.parametrize("label,path,subset_attr", _EDGE_SUBSETS)
def test_edge_loud_fails_on_broken_install(label, path, subset_attr, monkeypatch):
    """X-1 / v0.2.76 (ruling #1): a broken install (vco_lib import fails during
    the property-ensure) is a LOUD non-zero exit with the broken-install
    message on stderr — NOT a silent inline-copy degrade. Simulate it by
    forcing ``_ensure_props`` to raise ImportError, with a resolvable prefix +
    a stub Weaviate client so ``main`` reaches the ensure step."""
    import io
    from contextlib import redirect_stderr

    edge = _load_module(f"_p2d_parity_loud_{label}", path)
    monkeypatch.setenv("CODE_GRAPH_PROJECT", "P")  # prefix resolvable
    monkeypatch.setattr(
        edge, "_connect", lambda: types.SimpleNamespace(close=lambda: None)
    )

    def _boom(_client, _prefix):
        raise ImportError("No module named 'vco_lib'")

    monkeypatch.setattr(edge, "_ensure_props", _boom)

    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = edge.main()
    assert rc == 1, f"{label}: broken install must exit non-zero, got {rc}"
    err = buf.getvalue()
    assert "vco_lib not importable" in err and "install.py" in err, (
        f"{label}: broken-install message missing from stderr: {err!r}"
    )


def test_edge_subsets_cover_whole_table():
    """Union of the edge subsets == every prop in the shared table. A table
    prop no edge ensures would leave existing installs permanently on the
    old shape (the analyzer only heals projects that re-analyze)."""
    covered = set()
    for label, path, subset_attr in _EDGE_SUBSETS:
        edge = _load_module(f"_p2d_parity_cover_{label}", path)
        covered.update(getattr(edge, subset_attr))
    table_props = {
        prop for specs in CODEGRAPH_PROPERTY_SPECS.values()
        for (prop, _t, _d) in specs
    }
    assert covered == table_props


# ──────────────────── ensure_codegraph_properties units ─────────────────────


class _UnitColl:
    def __init__(self, has_props=(), add_raises=False) -> None:
        self.added: list = []

        def _add(prop):
            if add_raises:
                raise RuntimeError("422 add_property refused")
            self.added.append(prop)

        self.config = types.SimpleNamespace(
            get=lambda: types.SimpleNamespace(
                properties=[types.SimpleNamespace(name=n) for n in has_props]
            ),
            add_property=_add,
        )


def _client(colls: dict, probed: list | None = None):
    def _exists(name):
        if probed is not None:
            probed.append(name)
        return name in colls

    return types.SimpleNamespace(
        collections=types.SimpleNamespace(exists=_exists, get=lambda n: colls[n])
    )


def test_ensure_adds_all_specd_props_on_empty_classes():
    from weaviate.classes.config import DataType

    colls = {
        f"P_{suffix}": _UnitColl() for suffix in CODEGRAPH_PROPERTY_SPECS
    }
    results = ensure_codegraph_properties(_client(colls), "P")
    assert all(status == "ensured" for status in results.values())
    for suffix, specs in CODEGRAPH_PROPERTY_SPECS.items():
        got = [
            (p.name, p.dataType, p.skip_vectorization)
            for p in colls[f"P_{suffix}"].added
        ]
        want = [
            (prop, getattr(DataType, dtype), True)
            for (prop, dtype, _desc) in specs
        ]
        assert got == want, f"P_{suffix}: adds must follow the spec order"


def test_ensure_skips_present_props_and_absent_classes():
    colls = {
        "P_CodeFunction": _UnitColl(
            has_props=("embed_revision", "chunk_num", "total_chunks",
                       "is_test", "n_callers")
        ),
    }
    results = ensure_codegraph_properties(_client(colls), "P")
    assert colls["P_CodeFunction"].added == [], "idempotent"
    assert results["P_CodeFunction"] == "ensured"
    for suffix in CODEGRAPH_PROPERTY_SPECS:
        if suffix != "CodeFunction":
            assert results[f"P_{suffix}"] == "absent"


def test_ensure_subset_filter_never_probes_out_of_scope_classes():
    """5_to_6 shape: with the is_test/n_callers subset, CodeAPI and
    CodeInteraction have no spec'd props → they are never even probed
    (mirrors the pre-P2d edge, which never touched them)."""
    probed: list = []
    colls = {f"P_{s}": _UnitColl() for s in CODEGRAPH_PROPERTY_SPECS}
    results = ensure_codegraph_properties(
        _client(colls, probed), "P", props_subset=("is_test", "n_callers")
    )
    assert set(results) == {"P_CodeModule", "P_CodeClass", "P_CodeFunction"}
    assert "P_CodeAPI" not in probed and "P_CodeInteraction" not in probed
    assert [p.name for p in colls["P_CodeFunction"].added] == [
        "is_test", "n_callers",
    ]
    assert [p.name for p in colls["P_CodeAPI"].added] == []


def test_ensure_error_carries_failing_class():
    colls = {"P_CodeModule": _UnitColl(add_raises=True)}
    with pytest.raises(CodegraphPropertyEnsureError) as excinfo:
        ensure_codegraph_properties(
            _client(colls), "P", props_subset=("embed_revision",)
        )
    assert excinfo.value.class_name == "P_CodeModule"
    assert "422" in str(excinfo.value)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
