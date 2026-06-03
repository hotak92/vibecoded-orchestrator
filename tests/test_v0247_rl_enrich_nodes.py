# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.47 RL-6b-1: tests for ``_rl_enrich_nodes_with_linked_embs``.

The helper does ONE Weaviate fetch_objects per collection (grouped by the
``collection`` field on each input node) and attaches the v3 training
fields in place: ``n_emb`` (= ``emb``), ``linked_embs`` (MAX_LINKED
packed), ``linked_type_names`` (per-slot node_type strings),
``cos_qn``/``cos_ql``/``cos_nl`` (mean cosines so offline replay matches
online byte-identically without re-fetching link embeddings).

Tests use a stub Weaviate API surface: a fake ``coll`` exposes ``.query
.fetch_objects(filters=..., include_vector=True, limit=...)`` returning a
namespace with an ``.objects`` list. Each fake object has ``.properties``
+ ``.vector`` attributes matching the real Weaviate shape (named-vector
dict keyed by slot name).
"""

from __future__ import annotations

from types import SimpleNamespace

from claude_mcp_servers.weaviate_mcp.server import (
    _rl_enrich_nodes_with_linked_embs,
)


# ----------------------------------------------------------------------
# Fake Weaviate plumbing
# ----------------------------------------------------------------------


def _fake_obj(slot: str = "qwen3_embed", vec=None, **props):
    """Match the real `obj.properties` (dict) + `obj.vector` (dict[slot, list[float]])."""
    return SimpleNamespace(properties=props, vector={slot: vec} if vec else {})


def _fake_coll(objects):
    """A ``client.collections.get(name)`` handle whose `.query.fetch_objects(...)`
    returns the given list. Ignores filters — we trust that contains_any-style
    intersections are correct at the SDK layer; here we only need to verify
    enrichment behaviour on the post-fetch shape."""

    class Q:
        def fetch_objects(self, filters=None, include_vector=False, limit=0):
            return SimpleNamespace(objects=list(objects))

    return SimpleNamespace(query=Q())


def _resolver_for(coll_by_name):
    return lambda name: coll_by_name.get(name)


# ----------------------------------------------------------------------
# 1. Empty input + no-op cases.
# ----------------------------------------------------------------------


class TestEmptyAndNoop:
    def test_empty_nodes_is_noop(self) -> None:
        nodes: list[dict] = []
        _rl_enrich_nodes_with_linked_embs(
            nodes, query_emb=[0.5], active_slot="qwen3_embed",
            coll_resolver=_resolver_for({}),
        )
        assert nodes == []

    def test_nodes_without_collection_skipped(self) -> None:
        nodes = [{"title": "A"}]  # no `collection` key
        _rl_enrich_nodes_with_linked_embs(
            nodes, query_emb=[0.5], active_slot="qwen3_embed",
            coll_resolver=_resolver_for({}),
        )
        assert "linked_embs" not in nodes[0]

    def test_nodes_with_no_matching_weaviate_rows_get_empty_linked_embs(self) -> None:
        # The helper falls back to keying by `title` when `source_node_id`
        # is absent, so the fetch still happens — but if Weaviate returns
        # nothing, the helper still attaches explicit empty lists. Empty
        # lists ARE the correct v3 signal for "we tried, there are no
        # siblings or links" — they round-trip cleanly through the
        # offline trainer (no linked-slot input == valid training case).
        nodes = [{"title": "A", "collection": "VCODev_KG"}]
        coll = _fake_coll([])
        _rl_enrich_nodes_with_linked_embs(
            nodes, query_emb=None, active_slot="qwen3_embed",
            coll_resolver=_resolver_for({"VCODev_KG": coll}),
        )
        assert nodes[0]["linked_embs"] == []
        assert nodes[0]["linked_type_names"] == []


# ----------------------------------------------------------------------
# 2. Sibling-chunk enrichment.
# ----------------------------------------------------------------------


class TestSiblingEnrichment:
    def test_sibling_chunks_become_linked_embs(self) -> None:
        # Node A is chunk 2 of a 3-chunk file (uuid-a). Siblings: chunks 1 + 3.
        nodes = [
            {
                "title": "A",
                "node_type": "concept",
                "source_node_id": "uuid-a",
                "chunk_number": 2,
                "collection": "VCODev_KG",
                "emb": [0.5, 0.5],
            }
        ]
        # Three fake objects: chunks 1, 2 (matched -- must drop), 3.
        coll = _fake_coll([
            _fake_obj(vec=[0.1, 0.1], source_node_id="uuid-a", chunk_num=1, title="A"),
            _fake_obj(vec=[0.5, 0.5], source_node_id="uuid-a", chunk_num=2, title="A"),
            _fake_obj(vec=[0.3, 0.3], source_node_id="uuid-a", chunk_num=3, title="A"),
        ])
        _rl_enrich_nodes_with_linked_embs(
            nodes, query_emb=[1.0, 0.0], active_slot="qwen3_embed",
            coll_resolver=_resolver_for({"VCODev_KG": coll}),
        )
        n = nodes[0]
        # The matched chunk (2) is dropped; siblings 1 + 3 survive.
        assert n["linked_embs"] == [[0.1, 0.1], [0.3, 0.3]]
        # Extra chunks share the parent's node_type.
        assert n["linked_type_names"] == ["concept", "concept"]
        # n_emb mirrors the existing emb (back-compat).
        assert n["n_emb"] == [0.5, 0.5]


# ----------------------------------------------------------------------
# 3. Actual-link enrichment.
# ----------------------------------------------------------------------


class TestActualLinks:
    def test_link_objects_resolved_by_title(self) -> None:
        nodes = [
            {
                "title": "A",
                "node_type": "concept",
                "collection": "VCODev_KG",
                "emb": [1.0, 0.0],
                "links": ["LinkB"],
            }
        ]
        coll = _fake_coll([
            _fake_obj(vec=[0.0, 1.0], title="LinkB", node_type="tool"),
        ])
        _rl_enrich_nodes_with_linked_embs(
            nodes, query_emb=[1.0, 0.0], active_slot="qwen3_embed",
            coll_resolver=_resolver_for({"VCODev_KG": coll}),
        )
        n = nodes[0]
        assert n["linked_embs"] == [[0.0, 1.0]]
        # Per-link node_type carries from the fetched object.
        assert n["linked_type_names"] == ["tool"]


# ----------------------------------------------------------------------
# 4. Computed scalar features: cos_qn / cos_ql / cos_nl.
# ----------------------------------------------------------------------


class TestComputedFeatures:
    def test_cos_qn_computed_when_missing(self) -> None:
        nodes = [
            {
                "title": "A",
                "collection": "VCODev_KG",
                "emb": [1.0, 0.0],
                "source_node_id": "uuid-a",
                "chunk_number": 1,
            }
        ]
        coll = _fake_coll([])
        _rl_enrich_nodes_with_linked_embs(
            nodes,
            query_emb=[1.0, 0.0],  # perfectly aligned with emb
            active_slot="qwen3_embed",
            coll_resolver=_resolver_for({"VCODev_KG": coll}),
        )
        # cos_qn(q=[1,0], emb=[1,0]) = 1.0.
        assert abs(nodes[0]["cos_qn"] - 1.0) < 1e-9

    def test_existing_cos_qn_is_preserved(self) -> None:
        nodes = [
            {
                "title": "A",
                "collection": "VCODev_KG",
                "emb": [1.0, 0.0],
                "cos_qn": 0.42,  # already computed by the search path
                "source_node_id": "uuid-a",
                "chunk_number": 1,
            }
        ]
        coll = _fake_coll([])
        _rl_enrich_nodes_with_linked_embs(
            nodes,
            query_emb=[1.0, 0.0],
            active_slot="qwen3_embed",
            coll_resolver=_resolver_for({"VCODev_KG": coll}),
        )
        # NOT overwritten — preserves the search-path-computed value.
        assert nodes[0]["cos_qn"] == 0.42

    def test_cos_ql_and_cos_nl_computed_from_packed_links(self) -> None:
        nodes = [
            {
                "title": "A",
                "node_type": "concept",
                "collection": "VCODev_KG",
                "emb": [1.0, 0.0],
                "links": ["L1", "L2"],
            }
        ]
        coll = _fake_coll([
            _fake_obj(vec=[1.0, 0.0], title="L1", node_type="concept"),
            _fake_obj(vec=[0.0, 1.0], title="L2", node_type="concept"),
        ])
        _rl_enrich_nodes_with_linked_embs(
            nodes,
            query_emb=[1.0, 0.0],
            active_slot="qwen3_embed",
            coll_resolver=_resolver_for({"VCODev_KG": coll}),
        )
        n = nodes[0]
        # cos_ql = mean(cos(q,L1), cos(q,L2)) = mean(1.0, 0.0) = 0.5
        assert abs(n["cos_ql"] - 0.5) < 1e-9
        # cos_nl = mean(cos(n,L1), cos(n,L2)) = mean(1.0, 0.0) = 0.5
        assert abs(n["cos_nl"] - 0.5) < 1e-9


# ----------------------------------------------------------------------
# 5. Soft-fail on Weaviate errors.
# ----------------------------------------------------------------------


class TestSoftFail:
    def test_fetch_objects_raising_does_not_crash(self) -> None:
        class FailingColl:
            class query:
                @staticmethod
                def fetch_objects(*args, **kwargs):
                    raise RuntimeError("simulated weaviate failure")

        nodes = [
            {
                "title": "A",
                "collection": "VCODev_KG",
                "emb": [1.0, 0.0],
                "source_node_id": "uuid-a",
                "chunk_number": 1,
                "links": ["L1"],
            }
        ]
        _rl_enrich_nodes_with_linked_embs(
            nodes,
            query_emb=[1.0, 0.0],
            active_slot="qwen3_embed",
            coll_resolver=_resolver_for({"VCODev_KG": FailingColl()}),
        )
        # No exception. linked_embs stays absent; the node keeps emb + cos_qn (computed in fallback elsewhere or just absent).
        assert "linked_embs" not in nodes[0]


# ----------------------------------------------------------------------
# 6. Multi-collection fan-out.
# ----------------------------------------------------------------------


class TestMultiCollection:
    def test_two_collections_each_get_one_fetch(self) -> None:
        # Node from each of two collections; should result in two fetches
        # (one per collection group).
        nodes = [
            {
                "title": "FromKG",
                "node_type": "concept",
                "collection": "VCODev_KG",
                "emb": [1.0, 0.0],
                "links": ["LinkKG"],
            },
            {
                "title": "FromShared",
                "node_type": "concept",
                "collection": "VibeCodedOrchestrator_KG",
                "emb": [0.0, 1.0],
                "links": ["LinkShared"],
            },
        ]
        kg_coll = _fake_coll([_fake_obj(vec=[1.0, 0.0], title="LinkKG", node_type="tool")])
        shared_coll = _fake_coll([_fake_obj(vec=[0.0, 1.0], title="LinkShared", node_type="tool")])
        _rl_enrich_nodes_with_linked_embs(
            nodes,
            query_emb=[1.0, 0.0],
            active_slot="qwen3_embed",
            coll_resolver=_resolver_for({
                "VCODev_KG": kg_coll,
                "VibeCodedOrchestrator_KG": shared_coll,
            }),
        )
        assert nodes[0]["linked_embs"] == [[1.0, 0.0]]
        assert nodes[1]["linked_embs"] == [[0.0, 1.0]]


# ----------------------------------------------------------------------
# 7. No query_emb -> cos_qn / cos_ql skipped, cos_nl still computed.
# ----------------------------------------------------------------------


class TestQueryEmbAbsent:
    def test_no_query_emb_skips_q_features(self) -> None:
        nodes = [
            {
                "title": "A",
                "node_type": "concept",
                "collection": "VCODev_KG",
                "emb": [1.0, 0.0],
                "links": ["L1"],
            }
        ]
        coll = _fake_coll([_fake_obj(vec=[0.0, 1.0], title="L1", node_type="concept")])
        _rl_enrich_nodes_with_linked_embs(
            nodes,
            query_emb=None,
            active_slot="qwen3_embed",
            coll_resolver=_resolver_for({"VCODev_KG": coll}),
        )
        n = nodes[0]
        # linked_embs still computed.
        assert n["linked_embs"] == [[0.0, 1.0]]
        # cos_qn / cos_ql not set (no query_emb).
        assert "cos_qn" not in n
        assert "cos_ql" not in n
        # cos_nl IS computed (uses n_emb + links, both available).
        assert "cos_nl" in n
        assert abs(n["cos_nl"] - 0.0) < 1e-9  # cos([1,0], [0,1]) = 0
