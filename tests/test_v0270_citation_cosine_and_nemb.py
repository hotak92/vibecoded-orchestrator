# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.70 Stream F — F-D (cross-space cosine guard) + F-G (n_emb attach).

F-D: ``_cosine`` must REFUSE on a dimension mismatch (return 0.0) instead of
truncating to the shorter overlap (which returned a plausible ~0.75 for a
1024-vs-2048 cross-model comparison). ``_extract_obj_vector`` must pull ONLY the
requested active slot, never fall back to a foreign slot.

F-G: ``_rl_enrich_nodes_with_linked_embs`` must re-pull a node's own active-slot
vector when ``emb`` is absent (the ~96%-absent case that made cosine citations
structurally impossible). Single-chunk nodes (chunk_num=1, total_chunks=1) AND
legacy absent/0-chunk nodes must be non-blocking — recovered, not skipped.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from claude_mcp_servers.weaviate_mcp.server import (
    _cosine,
    _extract_obj_vector,
    _rl_enrich_nodes_with_linked_embs,
    _rl_refetch_node_vector,
)


# ----------------------------------------------------------------------
# Fake Weaviate plumbing (mirrors tests/test_v0247_rl_enrich_nodes.py).
# ----------------------------------------------------------------------


def _fake_obj(slot: str = "qwen3_embed", vec=None, **props):
    return SimpleNamespace(properties=props, vector={slot: vec} if vec else {})


def _fake_coll(objects):
    class Q:
        def fetch_objects(self, filters=None, include_vector=False, limit=0):
            return SimpleNamespace(objects=list(objects))

    return SimpleNamespace(query=Q())


def _resolver_for(coll_by_name):
    return lambda name: coll_by_name.get(name)


# ----------------------------------------------------------------------
# F-D — _cosine dimension guard.
# ----------------------------------------------------------------------


class TestFDCosineDimGuard:
    def test_same_dims_returns_real_value(self) -> None:
        # Identical unit vectors → cosine 1.0.
        assert _cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)
        # Orthogonal → 0.0.
        assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
        # A known non-trivial value.
        assert _cosine([1.0, 1.0], [1.0, 0.0]) == pytest.approx(0.7071, abs=1e-3)

    def test_mismatched_dims_returns_zero_not_truncated(self) -> None:
        # Pre-F-D this truncated to min(len) and returned a plausible value
        # (~0.75 for the classic 1024-vs-2048 case). Now it MUST refuse.
        a = [1.0] * 1024
        b = [1.0] * 2048
        assert _cosine(a, b) == 0.0
        assert _cosine(b, a) == 0.0
        # Even a 1-element difference refuses (different spaces).
        assert _cosine([1.0, 0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_empty_or_zero_norm_returns_zero(self) -> None:
        assert _cosine([], []) == 0.0
        assert _cosine([0.0, 0.0], [0.0, 0.0]) == 0.0
        assert _cosine(None, [1.0]) == 0.0


# ----------------------------------------------------------------------
# F-D — _extract_obj_vector no foreign-slot fallback.
# ----------------------------------------------------------------------


class TestFDExtractObjVector:
    def test_pulls_requested_active_slot(self) -> None:
        obj = _fake_obj(slot="qwen3_embed", vec=[0.1, 0.2])
        assert _extract_obj_vector(obj, "qwen3_embed") == [0.1, 0.2]

    def test_missing_active_slot_returns_none_not_foreign(self) -> None:
        # Object has ONLY a legacy/foreign slot; the active slot is absent.
        # Pre-F-D this fell back to "first non-empty slot" → pulled the
        # foreign (arctic 1024-but-different-space) vector. Now: None.
        obj = SimpleNamespace(
            properties={},
            vector={"ollama_embed": [0.9, 0.9, 0.9]},  # foreign slot
        )
        assert _extract_obj_vector(obj, "qwen3_embed") is None

    def test_slot_agnostic_caller_still_gets_first_slot(self) -> None:
        # Legacy single-vector mode: target_name empty → first non-empty slot.
        obj = SimpleNamespace(properties={}, vector={"default": [0.5, 0.5]})
        assert _extract_obj_vector(obj, "") == [0.5, 0.5]

    def test_unwrapped_list_vector(self) -> None:
        obj = SimpleNamespace(properties={}, vector=[0.3, 0.4])
        assert _extract_obj_vector(obj, "qwen3_embed") == [0.3, 0.4]


# ----------------------------------------------------------------------
# F-G — _rl_refetch_node_vector recovers a node's own vector.
# ----------------------------------------------------------------------


class TestFGRefetchNodeVector:
    def test_recovers_matched_chunk_vector(self) -> None:
        node = {"title": "A", "source_id": "uuid-a", "chunk_number": 2}
        siblings = {
            "uuid-a": [
                _fake_obj(vec=[0.1, 0.1], source_node_id="uuid-a", chunk_num=1),
                _fake_obj(vec=[0.2, 0.2], source_node_id="uuid-a", chunk_num=2),
            ]
        }
        got = _rl_refetch_node_vector(node, siblings, {}, "qwen3_embed")
        assert got == [0.2, 0.2]  # the matched chunk (chunk_num == 2)

    def test_single_chunk_node_recovered(self) -> None:
        # The common KG case: chunk_num=1, total_chunks=1.
        node = {"title": "Solo", "source_id": "uuid-s", "chunk_number": 1}
        siblings = {
            "uuid-s": [
                _fake_obj(vec=[0.7, 0.7], source_node_id="uuid-s", chunk_num=1),
            ]
        }
        got = _rl_refetch_node_vector(node, siblings, {}, "qwen3_embed")
        assert got == [0.7, 0.7]

    def test_legacy_absent_chunk_num_recovered(self) -> None:
        # Legacy storage where chunk_num is absent/0 — must NOT be skipped
        # (maintainer ruling: absent/0/1 is a valid single-chunk node).
        node = {"title": "Legacy", "source_id": "uuid-l", "chunk_number": None}
        siblings = {
            "uuid-l": [
                _fake_obj(vec=[0.4, 0.4], source_node_id="uuid-l"),  # no chunk_num
            ]
        }
        got = _rl_refetch_node_vector(node, siblings, {}, "qwen3_embed")
        assert got == [0.4, 0.4]

    def test_title_link_fallback(self) -> None:
        node = {"title": "Linked", "source_id": "uuid-x", "chunk_number": 5}
        link_objs = {"Linked": _fake_obj(vec=[0.9, 0.1])}
        got = _rl_refetch_node_vector(node, {}, link_objs, "qwen3_embed")
        assert got == [0.9, 0.1]

    def test_no_vector_anywhere_returns_none(self) -> None:
        node = {"title": "Empty", "source_id": "uuid-e", "chunk_number": 1}
        got = _rl_refetch_node_vector(node, {}, {}, "qwen3_embed")
        assert got is None


# ----------------------------------------------------------------------
# F-G — enrich attaches n_emb to a node that arrived WITHOUT emb.
# ----------------------------------------------------------------------


class TestFGEnrichAttachesNemb:
    def test_node_without_emb_gets_nemb_from_refetch(self) -> None:
        # The hook-path scenario: node has no `emb` (search-time enrichment
        # never ran), but its vector is in Weaviate. Enrich must recover it.
        nodes = [
            {
                "title": "Hookpath",
                "source_id": "uuid-h",
                "chunk_number": 1,
                "collection": "Proj_KnowledgeGraph",
                # NOTE: no "emb" key — the pre-F-G hook path state.
            }
        ]
        coll = _fake_coll([
            _fake_obj(vec=[0.6, 0.8], source_node_id="uuid-h", chunk_num=1,
                      title="Hookpath"),
        ])
        _rl_enrich_nodes_with_linked_embs(
            nodes, query_emb=[1.0, 0.0], active_slot="qwen3_embed",
            coll_resolver=_resolver_for({"Proj_KnowledgeGraph": coll}),
        )
        # n_emb recovered AND mirrored onto emb so _build_log_nodes carries it.
        assert nodes[0].get("n_emb") == [0.6, 0.8]
        assert nodes[0].get("emb") == [0.6, 0.8]
        # cos_qn computed against the recovered vector.
        assert "cos_qn" in nodes[0]

    def test_existing_emb_is_not_overwritten(self) -> None:
        # When emb is already present, the refetch path is skipped entirely.
        nodes = [
            {
                "title": "HasEmb",
                "source_id": "uuid-e",
                "chunk_number": 1,
                "collection": "Proj_KnowledgeGraph",
                "emb": [0.11, 0.22],
            }
        ]
        coll = _fake_coll([
            _fake_obj(vec=[0.99, 0.99], source_node_id="uuid-e", chunk_num=1,
                      title="HasEmb"),
        ])
        _rl_enrich_nodes_with_linked_embs(
            nodes, query_emb=None, active_slot="qwen3_embed",
            coll_resolver=_resolver_for({"Proj_KnowledgeGraph": coll}),
        )
        assert nodes[0]["n_emb"] == [0.11, 0.22]  # original emb preserved
        assert nodes[0]["emb"] == [0.11, 0.22]


# ----------------------------------------------------------------------
# F-G acceptance — single-chunk nodes are NON-BLOCKING:
# retrieved (formatted with valid chunk identity), ranked (no chunk gate),
# AND citation-eligible (their vector is recovered for cosine).
# ----------------------------------------------------------------------


class TestFGSingleChunkAcceptance:
    def test_format_obj_normalises_single_chunk_to_1_of_1(self) -> None:
        # A node stored with chunk_num=1, total_chunks=1 (the common KG case).
        from claude_mcp_servers.weaviate_mcp.server import _format_obj

        class _Md:
            distance = 0.1

        class _Obj:
            properties = {
                "title": "Solo", "content": "short node body",
                "chunk_num": 1, "total_chunks": 1, "node_type": "concept",
            }
            metadata = _Md()

        r = _format_obj(_Obj(), "Proj_KnowledgeGraph", 0.1)
        assert r["chunk_number"] == 1
        assert r["total_chunks"] == 1
        assert r["title"] == "Solo"

    def test_format_obj_legacy_zero_chunk_num_normalised_not_dropped(self) -> None:
        # Legacy storage: chunk_num=0 / absent. Pre-F-G the `or` chain returned
        # None for both → identity-less. Now: normalised to 1 of 1 (valid
        # single-chunk node, NEVER skipped).
        from claude_mcp_servers.weaviate_mcp.server import _format_obj

        class _Md:
            distance = 0.2

        class _Obj:
            properties = {
                "title": "Legacy", "content": "no chunk header here",
                "chunk_num": 0,  # legacy zero
                "node_type": "concept",
            }
            metadata = _Md()

        r = _format_obj(_Obj(), "Proj_KnowledgeGraph", 0.2)
        assert r["chunk_number"] == 1
        assert r["total_chunks"] == 1

    def test_single_chunk_node_is_citation_eligible(self) -> None:
        # End-to-end-ish: a single-chunk node with NO emb (hook path) gets its
        # vector recovered → it carries n_emb → it is comparable for cosine,
        # i.e. citation-eligible. Mirrors the F-G acceptance criterion.
        nodes = [
            {
                "title": "ShortKGNode",
                "source_id": "uuid-short",
                "chunk_number": 1,
                "total_chunks": 1,
                "collection": "Proj_KnowledgeGraph",
                # no emb — single-chunk node from the hook path.
            }
        ]
        coll = _fake_coll([
            _fake_obj(vec=[0.5, 0.5], source_node_id="uuid-short", chunk_num=1,
                      title="ShortKGNode"),
        ])
        _rl_enrich_nodes_with_linked_embs(
            nodes, query_emb=[1.0, 0.0], active_slot="qwen3_embed",
            coll_resolver=_resolver_for({"Proj_KnowledgeGraph": coll}),
        )
        # citation-eligible: it now has n_emb to compare the answer against.
        assert nodes[0].get("n_emb") == [0.5, 0.5]
