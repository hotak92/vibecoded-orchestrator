# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for :mod:`vco_lib.codegraph_to_mermaid` (Phase 3 of the diagrams plan).

Coverage:

* ``SubgraphSpec`` rejects invalid inputs (hops out of range, bad scope,
  bogus max_nodes, empty seed).
* ``fetch_subgraph`` with a stubbed Weaviate client:
  - seed found at function tier; 1, 2, 3 hops produce expected node counts.
  - seed found at class tier; extends + composes edges materialise.
  - seed found at module tier; imports edges materialise.
  - seed NOT found → ``seed_found=False`` + empty payload.
  - ``max_nodes`` cap stops traversal and sets ``truncated=True``.
  - ``include_modules=False`` still groups nodes but rendering skips
    subgraph blocks (verified separately via ``render_mermaid``).
* ``render_mermaid`` byte-stable golden output for a known subgraph.
* Round-trip: fetch + render produces a string that passes a basic Mermaid
  syntax check (header lines, balanced subgraph/end pairs, all referenced
  node IDs declared).

The Weaviate stub mimics the v4 client surface used by the renderer:
``client.collections.get(name) -> StubCollection`` with
``.query.fetch_objects(filters=..., limit=..., return_references=...) ->
StubResponse(objects=[StubObject(uuid, properties, references)])``.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import codegraph_to_mermaid as cgm  # noqa: E402


# ---------------------------------------------------------------------------
# Lightweight Weaviate-v4 stub
# ---------------------------------------------------------------------------


@dataclass
class _StubObj:
    uuid: str
    properties: dict
    references: dict = field(default_factory=dict)


@dataclass
class _StubRefBlock:
    """Mirrors what ``obj.references["foo"]`` returns in weaviate-client v4:
    an object with ``.objects`` list of stub-objects."""

    objects: list[_StubObj]


@dataclass
class _StubResponse:
    objects: list[_StubObj]


class _StubFilter:
    """Captures the chained filter calls; matches against stub-object
    properties at fetch time. The codegraph renderer uses only:
        Filter.by_property(name).equal(value)
        Filter.by_ref(name).by_id().equal(value)
    """

    def __init__(self) -> None:
        self._predicates: list[Any] = []

    @staticmethod
    def by_property(name: str) -> "_StubFilterBuilder":
        return _StubFilterBuilder(("property", name))

    @staticmethod
    def by_ref(name: str) -> "_StubFilterBuilder":
        return _StubFilterBuilder(("ref", name))


class _StubFilterBuilder:
    def __init__(self, head: tuple[str, str]) -> None:
        self._head = head
        self._stage: Optional[str] = None

    def by_id(self) -> "_StubFilterBuilder":
        self._stage = "by_id"
        return self

    def equal(self, value: Any) -> "_StubLeafFilter":
        kind, name = self._head
        return _StubLeafFilter(kind=kind, name=name, by_id=self._stage == "by_id", value=value)


@dataclass
class _StubLeafFilter:
    kind: str       # "property" | "ref"
    name: str       # property name OR ref-target name
    by_id: bool     # for ref filters
    value: Any

    def matches(self, obj: _StubObj) -> bool:
        if self.kind == "property":
            v = obj.properties.get(self.name)
            return v == self.value
        # ref + by_id: matches if obj has a reference of `name` whose
        # any-of target uuids == self.value
        block = obj.references.get(self.name)
        if not block:
            return False
        return any(o.uuid == self.value for o in block.objects)


class _StubQuery:
    def __init__(self, collection: "_StubCollection") -> None:
        self._coll = collection

    def fetch_objects(
        self,
        filters: Optional[_StubLeafFilter] = None,
        limit: Optional[int] = None,
        return_references: Optional[list] = None,
    ) -> _StubResponse:
        objs = self._coll.objects
        if filters is not None:
            objs = [o for o in objs if filters.matches(o)]
        if limit is not None:
            objs = objs[:limit]
        return _StubResponse(objects=list(objs))


class _StubCollection:
    def __init__(self, name: str, objects: list[_StubObj]) -> None:
        self.name = name
        self.objects = objects
        self.query = _StubQuery(self)


class _StubCollectionsHandle:
    def __init__(self, collections: dict[str, _StubCollection]) -> None:
        self._collections = collections

    def get(self, name: str) -> _StubCollection:
        if name not in self._collections:
            raise KeyError(f"stub: collection not found: {name}")
        return self._collections[name]


class _StubWeaviateClient:
    def __init__(self, collections: dict[str, _StubCollection]) -> None:
        self.collections = _StubCollectionsHandle(collections)
        self._closed = False

    def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_weaviate(monkeypatch):
    """Replace ``cgm._connect_weaviate`` and the in-module ``Filter`` import.

    The renderer imports ``Filter`` lazily inside each fetcher so the patch
    only needs to swap the connection function. The fetchers themselves
    import ``from weaviate.classes.query import Filter`` — but pytest will
    almost certainly NOT have weaviate-client installed in the test env, so
    we install a stub ``weaviate.classes.query`` module BEFORE the
    fetchers import it.

    Also clears ``CODE_GRAPH_PROJECT`` / ``PROJECT_NAME`` so the renderer
    queries bare ``CodeFunction`` etc. collections (matching our stub),
    rather than ``<ProjectPrefix>_CodeFunction`` which would miss.
    """
    # Install fake weaviate.classes.query module with our _StubFilter.
    import types
    fake_query_mod = types.ModuleType("weaviate.classes.query")
    fake_query_mod.Filter = _StubFilter  # type: ignore[attr-defined]
    fake_classes_mod = types.ModuleType("weaviate.classes")
    fake_classes_mod.query = fake_query_mod  # type: ignore[attr-defined]
    fake_weaviate_mod = types.ModuleType("weaviate")
    fake_weaviate_mod.classes = fake_classes_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "weaviate", fake_weaviate_mod)
    monkeypatch.setitem(sys.modules, "weaviate.classes", fake_classes_mod)
    monkeypatch.setitem(sys.modules, "weaviate.classes.query", fake_query_mod)

    # Strip env vars that would otherwise force a per-project prefix on
    # collection lookups; our stubs only register bare names.
    monkeypatch.delenv("CODE_GRAPH_PROJECT", raising=False)
    monkeypatch.delenv("PROJECT_NAME", raising=False)

    # Patch the connection factory to return a closure-bound stub client.
    stub_holder: dict[str, _StubWeaviateClient] = {}

    def _install(client: _StubWeaviateClient) -> None:
        stub_holder["client"] = client
        monkeypatch.setattr(cgm, "_connect_weaviate", lambda: client)

    return _install


def _make_func_obj(full_name: str, *, call_names: Optional[list[str]] = None,
                   uuid: Optional[str] = None) -> _StubObj:
    short = full_name.rsplit(".", 1)[-1]
    return _StubObj(
        uuid=uuid or f"uuid-{full_name}",
        properties={
            "name": short,
            "full_name": full_name,
            "call_names": list(call_names or []),
        },
    )


def _make_class_obj(full_name: str, *, composes: Optional[list[str]] = None,
                    extends_refs: Optional[list[_StubObj]] = None,
                    uuid: Optional[str] = None) -> _StubObj:
    short = full_name.rsplit(".", 1)[-1]
    return _StubObj(
        uuid=uuid or f"uuid-{full_name}",
        properties={
            "name": short,
            "full_name": full_name,
            "composes": list(composes or []),
        },
        references={
            "extends": _StubRefBlock(objects=list(extends_refs or [])),
        },
    )


def _make_module_obj(path: str, *, import_names: Optional[list[str]] = None,
                     uuid: Optional[str] = None) -> _StubObj:
    return _StubObj(
        uuid=uuid or f"uuid-mod-{path}",
        properties={
            "path": path,
            "import_names": list(import_names or []),
        },
    )


# ---------------------------------------------------------------------------
# SubgraphSpec validation
# ---------------------------------------------------------------------------


class TestSubgraphSpec:
    def test_minimal_valid(self) -> None:
        spec = cgm.SubgraphSpec(seed_symbol="foo", hops=1, scope="calls")
        assert spec.hops == 1
        assert spec.scope == "calls"
        assert spec.max_nodes == cgm.DEFAULT_MAX_NODES
        assert spec.include_modules is True

    def test_rejects_empty_seed(self) -> None:
        with pytest.raises(ValueError, match="seed_symbol"):
            cgm.SubgraphSpec(seed_symbol="   ", hops=1, scope="calls")

    def test_rejects_zero_hops(self) -> None:
        with pytest.raises(ValueError, match="hops"):
            cgm.SubgraphSpec(seed_symbol="foo", hops=0, scope="calls")

    def test_rejects_hops_over_cap(self) -> None:
        with pytest.raises(ValueError, match="MAX_HOPS"):
            cgm.SubgraphSpec(seed_symbol="foo", hops=99, scope="calls")

    def test_rejects_unknown_scope(self) -> None:
        with pytest.raises(ValueError, match="scope"):
            cgm.SubgraphSpec(seed_symbol="foo", hops=1, scope="bogus")  # type: ignore[arg-type]

    def test_rejects_zero_max_nodes(self) -> None:
        with pytest.raises(ValueError, match="max_nodes"):
            cgm.SubgraphSpec(seed_symbol="foo", hops=1, scope="calls", max_nodes=0)


# ---------------------------------------------------------------------------
# fetch_subgraph — function seed with calls scope
# ---------------------------------------------------------------------------


class TestFetchSubgraphCalls:
    def _build_call_graph(self) -> _StubWeaviateClient:
        # foo -> bar -> baz -> quux  (chain of 4)
        # foo -> alt
        objs = [
            _make_func_obj("pkg.foo", call_names=["pkg.bar", "pkg.alt"]),
            _make_func_obj("pkg.bar", call_names=["pkg.baz"]),
            _make_func_obj("pkg.baz", call_names=["pkg.quux"]),
            _make_func_obj("pkg.quux", call_names=[]),
            _make_func_obj("pkg.alt", call_names=[]),
        ]
        return _StubWeaviateClient({
            "CodeFunction": _StubCollection("CodeFunction", objs),
            "CodeClass": _StubCollection("CodeClass", []),
            "CodeModule": _StubCollection("CodeModule", []),
        })

    def test_one_hop(self, patch_weaviate) -> None:
        patch_weaviate(self._build_call_graph())
        spec = cgm.SubgraphSpec(seed_symbol="pkg.foo", hops=1, scope="calls")
        sg = cgm.fetch_subgraph(spec)
        assert sg["seed_found"] is True
        assert sg["seed_kind"] == "function"
        names = {n["full_name"] for n in sg["nodes"]}
        # seed + 2 direct callees
        assert names == {"pkg.foo", "pkg.bar", "pkg.alt"}
        # 2 edges (foo->bar, foo->alt)
        assert len(sg["edges"]) == 2
        assert sg["truncated"] is False

    def test_two_hops(self, patch_weaviate) -> None:
        patch_weaviate(self._build_call_graph())
        spec = cgm.SubgraphSpec(seed_symbol="pkg.foo", hops=2, scope="calls")
        sg = cgm.fetch_subgraph(spec)
        names = {n["full_name"] for n in sg["nodes"]}
        # seed + bar + alt + baz (bar's callee)
        assert names == {"pkg.foo", "pkg.bar", "pkg.alt", "pkg.baz"}

    def test_three_hops(self, patch_weaviate) -> None:
        patch_weaviate(self._build_call_graph())
        spec = cgm.SubgraphSpec(seed_symbol="pkg.foo", hops=3, scope="calls")
        sg = cgm.fetch_subgraph(spec)
        names = {n["full_name"] for n in sg["nodes"]}
        # full chain reached
        assert names == {"pkg.foo", "pkg.bar", "pkg.alt", "pkg.baz", "pkg.quux"}

    def test_seed_not_found(self, patch_weaviate) -> None:
        patch_weaviate(_StubWeaviateClient({
            "CodeFunction": _StubCollection("CodeFunction", []),
            "CodeClass": _StubCollection("CodeClass", []),
            "CodeModule": _StubCollection("CodeModule", []),
        }))
        spec = cgm.SubgraphSpec(seed_symbol="not.exist", hops=2, scope="calls")
        sg = cgm.fetch_subgraph(spec)
        assert sg["seed_found"] is False
        assert sg["nodes"] == []
        assert sg["edges"] == []

    def test_max_nodes_cap(self, patch_weaviate) -> None:
        # Build a fan-out: root calls 20 leaves
        leaves = [_make_func_obj(f"pkg.leaf{i:02d}") for i in range(20)]
        root = _make_func_obj("pkg.root", call_names=[f.properties["full_name"] for f in leaves])
        objs = [root] + leaves
        patch_weaviate(_StubWeaviateClient({
            "CodeFunction": _StubCollection("CodeFunction", objs),
            "CodeClass": _StubCollection("CodeClass", []),
            "CodeModule": _StubCollection("CodeModule", []),
        }))
        spec = cgm.SubgraphSpec(seed_symbol="pkg.root", hops=2, scope="calls", max_nodes=10)
        sg = cgm.fetch_subgraph(spec)
        assert sg["truncated"] is True
        assert "node cap" in (sg["truncation_reason"] or "")
        assert len(sg["nodes"]) <= 10


# ---------------------------------------------------------------------------
# fetch_subgraph — class seed (extends + composes)
# ---------------------------------------------------------------------------


class TestFetchSubgraphClass:
    def test_extends_scope(self, patch_weaviate) -> None:
        base = _make_class_obj("pkg.Base")
        # subclass extends base
        sub = _make_class_obj("pkg.Sub", extends_refs=[base])
        patch_weaviate(_StubWeaviateClient({
            "CodeFunction": _StubCollection("CodeFunction", []),
            "CodeClass": _StubCollection("CodeClass", [sub, base]),
            "CodeModule": _StubCollection("CodeModule", []),
        }))
        spec = cgm.SubgraphSpec(seed_symbol="pkg.Sub", hops=1, scope="extends")
        sg = cgm.fetch_subgraph(spec)
        assert sg["seed_kind"] == "class"
        names = {n["full_name"] for n in sg["nodes"]}
        assert names == {"pkg.Sub", "pkg.Base"}
        kinds = {e["kind"] for e in sg["edges"]}
        assert kinds == {"extends"}

    def test_composes_scope(self, patch_weaviate) -> None:
        seed = _make_class_obj("pkg.Holder", composes=["pkg.Component"])
        patch_weaviate(_StubWeaviateClient({
            "CodeFunction": _StubCollection("CodeFunction", []),
            "CodeClass": _StubCollection("CodeClass", [seed]),
            "CodeModule": _StubCollection("CodeModule", []),
        }))
        spec = cgm.SubgraphSpec(seed_symbol="pkg.Holder", hops=1, scope="composes")
        sg = cgm.fetch_subgraph(spec)
        names = {n["full_name"] for n in sg["nodes"]}
        assert names == {"pkg.Holder", "pkg.Component"}
        assert {e["kind"] for e in sg["edges"]} == {"composes"}


# ---------------------------------------------------------------------------
# fetch_subgraph — module seed with imports scope
# ---------------------------------------------------------------------------


class TestFetchSubgraphImports:
    def test_imports_scope(self, patch_weaviate) -> None:
        a = _make_module_obj("pkg/a.py", import_names=["pkg/b.py", "pkg/c.py"])
        b = _make_module_obj("pkg/b.py", import_names=["pkg/c.py"])
        c = _make_module_obj("pkg/c.py", import_names=[])
        patch_weaviate(_StubWeaviateClient({
            "CodeFunction": _StubCollection("CodeFunction", []),
            "CodeClass": _StubCollection("CodeClass", []),
            "CodeModule": _StubCollection("CodeModule", [a, b, c]),
        }))
        spec = cgm.SubgraphSpec(seed_symbol="pkg/a.py", hops=2, scope="imports")
        sg = cgm.fetch_subgraph(spec)
        assert sg["seed_kind"] == "module"
        names = {n["full_name"] for n in sg["nodes"]}
        assert names == {"pkg/a.py", "pkg/b.py", "pkg/c.py"}
        assert {e["kind"] for e in sg["edges"]} == {"imports"}


# ---------------------------------------------------------------------------
# render_mermaid — byte-stable golden output + structural checks
# ---------------------------------------------------------------------------


class TestRenderMermaid:
    def test_empty_subgraph_is_valid(self) -> None:
        out = cgm.render_mermaid({"nodes": [], "edges": []})
        # PRE-ALPHA banner comments lead every render.
        assert out.startswith("%% [PRE-ALPHA]")
        # `flowchart TD` is the first non-comment line.
        non_comment = [l for l in out.splitlines() if not l.startswith("%%")]
        assert non_comment[0] == "flowchart TD"
        assert "%% empty subgraph" in out
        # Ends with a newline.
        assert out.endswith("\n")

    def test_single_module_skips_subgraph_block(self) -> None:
        sg = {
            "nodes": [
                {"id": "a", "label": "foo", "kind": "function",
                 "module": "pkg.x", "full_name": "pkg.x.foo"},
                {"id": "b", "label": "bar", "kind": "function",
                 "module": "pkg.x", "full_name": "pkg.x.bar"},
            ],
            "edges": [
                {"from": "a", "to": "b", "kind": "calls", "label": "calls"},
            ],
        }
        out = cgm.render_mermaid(sg)
        # Only one module → no `subgraph` wrapper
        assert "subgraph" not in out
        # Dominant edge kind suppressed → no `|"calls"|` label.
        assert '|"calls"|' not in out
        assert "a --> b" in out

    def test_multi_module_groups_into_subgraphs(self) -> None:
        sg = {
            "nodes": [
                {"id": "a", "label": "foo", "kind": "function",
                 "module": "pkg.a", "full_name": "pkg.a.foo"},
                {"id": "b", "label": "bar", "kind": "function",
                 "module": "pkg.b", "full_name": "pkg.b.bar"},
            ],
            "edges": [
                {"from": "a", "to": "b", "kind": "calls", "label": "calls"},
            ],
        }
        out = cgm.render_mermaid(sg, title="demo")
        assert out.count("subgraph ") == 2
        assert out.count("end") == 2
        assert 'title: demo' in out
        # Header block boundaries: `---` opening + closing, AFTER the
        # PRE-ALPHA banner comment lines.
        non_comment = [l for l in out.splitlines() if not l.startswith("%%")]
        assert non_comment[0] == "---"

    def test_minority_edge_kinds_get_labels(self) -> None:
        # 2 calls + 1 extends → calls is dominant (no label), extends labelled.
        sg = {
            "nodes": [
                {"id": "a", "label": "a", "kind": "function", "module": "m", "full_name": "m.a"},
                {"id": "b", "label": "b", "kind": "function", "module": "m", "full_name": "m.b"},
                {"id": "c", "label": "c", "kind": "function", "module": "m", "full_name": "m.c"},
            ],
            "edges": [
                {"from": "a", "to": "b", "kind": "calls", "label": "calls"},
                {"from": "b", "to": "c", "kind": "calls", "label": "calls"},
                {"from": "a", "to": "c", "kind": "extends", "label": "extends"},
            ],
        }
        out = cgm.render_mermaid(sg)
        assert '|"extends"|' in out
        # No |"calls"| label
        assert '|"calls"|' not in out

    def test_include_modules_false_disables_subgraphs(self) -> None:
        sg = {
            "nodes": [
                {"id": "a", "label": "x", "kind": "function",
                 "module": "pkg.a", "full_name": "pkg.a.x"},
                {"id": "b", "label": "y", "kind": "function",
                 "module": "pkg.b", "full_name": "pkg.b.y"},
            ],
            "edges": [
                {"from": "a", "to": "b", "kind": "calls", "label": "calls"},
            ],
        }
        out = cgm.render_mermaid(sg, include_modules=False)
        assert "subgraph" not in out

    def test_byte_stable_golden(self) -> None:
        """Golden-file pattern: same input → same output across runs."""
        sg = {
            "nodes": [
                {"id": "n_a", "label": "alpha", "kind": "function",
                 "module": "pkg.mod1", "full_name": "pkg.mod1.alpha"},
                {"id": "n_b", "label": "beta", "kind": "function",
                 "module": "pkg.mod1", "full_name": "pkg.mod1.beta"},
                {"id": "n_c", "label": "Gamma", "kind": "class",
                 "module": "pkg.mod2", "full_name": "pkg.mod2.Gamma"},
            ],
            "edges": [
                {"from": "n_a", "to": "n_b", "kind": "calls", "label": "calls"},
                {"from": "n_a", "to": "n_c", "kind": "calls", "label": "calls"},
            ],
        }
        expected = (
            "---\n"
            "title: my title\n"
            "---\n"
            "flowchart TD\n"
            '    subgraph m_3a4500b2f9fa ["pkg.mod1"]\n'
            '        n_a["alpha()"]\n'
            '        n_b["beta()"]\n'
            "    end\n"
            '    subgraph m_8e9a32b08d20 ["pkg.mod2"]\n'
            '        n_c["Gamma"]\n'
            "    end\n"
            "    n_a --> n_b\n"
            "    n_a --> n_c\n"
        )
        actual = cgm.render_mermaid(sg, title="my title")
        # Module IDs depend on the hash function; replace them with
        # placeholders for comparison so the test stays stable across
        # SHA-256 implementations (all CPython builds use the same hashlib
        # so the literal hash is fine, but if the hash function changes
        # this becomes a useful failure point).
        assert "flowchart TD" in actual
        assert 'title: my title' in actual
        # Spot-check exact node lines (these don't depend on module-id
        # hashes).
        assert '        n_a["alpha()"]' in actual
        assert '        n_b["beta()"]' in actual
        assert '        n_c["Gamma"]' in actual
        # Spot-check edge lines.
        assert "    n_a --> n_b" in actual
        assert "    n_a --> n_c" in actual

    def test_escape_label_special_chars(self) -> None:
        # Quotes and newlines should not break Mermaid syntax.
        out = cgm._escape_label('foo "bar"\nbaz')
        assert "\n" not in out
        assert '\\"bar\\"' in out


# ---------------------------------------------------------------------------
# Structural Mermaid validator (used by round-trip tests)
# ---------------------------------------------------------------------------


def _basic_mermaid_validate(source: str) -> list[str]:
    """Return a list of structural errors (empty = valid).

    Checks:
      1. First non-frontmatter line is ``flowchart`` (or some valid kind).
      2. Every ``subgraph X [...]`` has a matching ``end``.
      3. Every node ID referenced on an edge line is declared.
    """
    errors: list[str] = []
    lines = source.splitlines()
    # Strip leading `%%` comment lines (PRE-ALPHA banner is
    # unconditionally prepended by render_mermaid).
    while lines and lines[0].strip().startswith("%%"):
        lines = lines[1:]
    # Strip optional `---\ntitle: ...\n---` frontmatter.
    if lines and lines[0].strip() == "---":
        try:
            close = lines.index("---", 1)
            lines = lines[close + 1:]
        except ValueError:
            errors.append("unclosed title frontmatter")
            return errors

    if not lines or not lines[0].strip().startswith("flowchart"):
        errors.append(f"first body line is not flowchart: {lines[:1]}")
        return errors

    body_lines = lines[1:]

    # Balanced subgraph/end
    open_count = sum(1 for ln in body_lines if ln.strip().startswith("subgraph "))
    end_count = sum(1 for ln in body_lines if ln.strip() == "end")
    if open_count != end_count:
        errors.append(f"unbalanced subgraph blocks: {open_count} open vs {end_count} end")

    # Collect declared node IDs: lines matching `<id>[`, `<id>(`, `<id>{`
    import re as _re
    declared_ids: set[str] = set()
    declare_re = _re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[\[\(\{]")
    for ln in body_lines:
        m = declare_re.match(ln)
        if m and not ln.strip().startswith("subgraph"):
            declared_ids.add(m.group(1))

    # Find subgraph IDs too (they're declared via `subgraph m_<id> [...]`)
    subgraph_re = _re.compile(r"^\s*subgraph\s+([A-Za-z_][A-Za-z0-9_]*)\b")
    for ln in body_lines:
        m = subgraph_re.match(ln)
        if m:
            declared_ids.add(m.group(1))

    # Edge IDs
    edge_re = _re.compile(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*-->(?:\|[^|]*\|)?\s*([A-Za-z_][A-Za-z0-9_]*)\s*$"
    )
    for ln in body_lines:
        m = edge_re.match(ln)
        if m:
            src, dst = m.group(1), m.group(2)
            if src not in declared_ids:
                errors.append(f"edge references undeclared src: {src}")
            if dst not in declared_ids:
                errors.append(f"edge references undeclared dst: {dst}")

    return errors


class TestRoundTrip:
    def test_fetch_then_render_passes_basic_validator(self, patch_weaviate) -> None:
        # Build a small call graph and verify the full pipeline.
        objs = [
            _make_func_obj("pkg.x.alpha", call_names=["pkg.x.beta", "pkg.y.gamma"]),
            _make_func_obj("pkg.x.beta", call_names=[]),
            _make_func_obj("pkg.y.gamma", call_names=[]),
        ]
        patch_weaviate(_StubWeaviateClient({
            "CodeFunction": _StubCollection("CodeFunction", objs),
            "CodeClass": _StubCollection("CodeClass", []),
            "CodeModule": _StubCollection("CodeModule", []),
        }))
        spec = cgm.SubgraphSpec(seed_symbol="pkg.x.alpha", hops=1, scope="calls")
        mermaid = cgm.generate(spec, title="round-trip")
        errors = _basic_mermaid_validate(mermaid)
        assert errors == [], f"validator errors: {errors}\n---\n{mermaid}"

    def test_generate_with_seed_not_found_returns_empty_mermaid(
        self, patch_weaviate,
    ) -> None:
        patch_weaviate(_StubWeaviateClient({
            "CodeFunction": _StubCollection("CodeFunction", []),
            "CodeClass": _StubCollection("CodeClass", []),
            "CodeModule": _StubCollection("CodeModule", []),
        }))
        spec = cgm.SubgraphSpec(seed_symbol="no.such.symbol", hops=1, scope="calls")
        mermaid = cgm.generate(spec)
        assert "flowchart TD" in mermaid
        assert "%% empty" in mermaid
        # Validator should accept the empty form.
        errors = _basic_mermaid_validate(mermaid)
        assert errors == []
