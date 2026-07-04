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

    def test_no_vector_no_text_no_model_returns_none(self) -> None:
        # No stored vector, no text, no model → genuinely unrecoverable → None
        # (the caller then DROPS the node — never fabricates).
        node = {"title": "Empty", "source_id": "uuid-e", "chunk_number": 1}
        got = _rl_refetch_node_vector(node, {}, {}, "qwen3_embed")
        assert got is None

    def test_regenerates_from_chunk_text_when_no_stored_vector(self, monkeypatch) -> None:
        # F-C CORRECTED: a node whose stored vector is missing but whose chunk
        # TEXT we have gets its embedding REGENERATED on the fly (so cosine is
        # always computable), instead of being dropped/fabricated.
        import claude_mcp_servers.rl_client.embed_regen as er

        class _FakeSvc:
            def embed_text(self, text):
                return [0.123, 0.456]  # deterministic regenerated vector

        monkeypatch.setattr(er, "regenerate_node_vector",
                            lambda text, model, embedding_service=None: [0.123, 0.456]
                            if text else None)
        # Sibling object carries content but NO vector (slot absent).
        sib = SimpleNamespace(
            properties={"source_node_id": "uuid-r", "chunk_num": 1,
                        "content": "the node body text", "title": "Regen"},
            vector={},  # no stored vector
        )
        node = {"title": "Regen", "source_id": "uuid-r", "chunk_number": 1}
        got = _rl_refetch_node_vector(
            node, {"uuid-r": [sib]}, {}, "qwen3_embed",
            model_name="qwen3-embedding:0.6b",
        )
        assert got == [0.123, 0.456]

    def test_no_model_skips_regeneration(self) -> None:
        # Without a model_name the regeneration step is skipped (can't pick a
        # chunk preset / embedder) → returns None even with text present.
        sib = SimpleNamespace(
            properties={"source_node_id": "uuid-r", "chunk_num": 1,
                        "content": "body", "title": "Regen"},
            vector={},
        )
        node = {"title": "Regen", "source_id": "uuid-r", "chunk_number": 1}
        got = _rl_refetch_node_vector(node, {"uuid-r": [sib]}, {}, "qwen3_embed",
                                      model_name="")
        assert got is None

    def test_allow_regen_false_skips_synchronous_embed(self, monkeypatch) -> None:
        # v0.2.73 retrieval-lock guard: with allow_regen=False the step-4
        # synchronous Ollama embed is NOT invoked even when text + model are
        # present — the whole point is to avoid N blocking embeds when the
        # active slot is absent group-wide. Assert the regen helper is never
        # called AND the result is None (node carries no comparable vector).
        import claude_mcp_servers.weaviate_mcp.server as srv

        called = {"n": 0}

        def _spy_regen(text, model):
            called["n"] += 1
            return [0.123, 0.456]

        monkeypatch.setattr(srv, "_rl_regenerate_node_vector", _spy_regen)
        sib = SimpleNamespace(
            properties={"source_node_id": "uuid-r", "chunk_num": 1,
                        "content": "the node body text", "title": "Regen"},
            vector={},  # active slot absent
        )
        node = {"title": "Regen", "source_id": "uuid-r", "chunk_number": 1}
        got = _rl_refetch_node_vector(
            node, {"uuid-r": [sib]}, {}, "qwen3_embed",
            model_name="qwen3-embedding:0.6b", allow_regen=False,
        )
        assert got is None
        assert called["n"] == 0  # the blocking embed was skipped

    def test_allow_regen_true_still_regenerates(self, monkeypatch) -> None:
        # The leave-alone case's counterpart: with the slot present group-wide
        # (allow_regen=True, the default), the regen path still fires so the
        # single-stray-miss recovery is preserved.
        import claude_mcp_servers.weaviate_mcp.server as srv

        called = {"n": 0}

        def _spy_regen(text, model):
            called["n"] += 1
            return [0.123, 0.456]

        monkeypatch.setattr(srv, "_rl_regenerate_node_vector", _spy_regen)
        sib = SimpleNamespace(
            properties={"source_node_id": "uuid-r", "chunk_num": 1,
                        "content": "the node body text", "title": "Regen"},
            vector={},
        )
        node = {"title": "Regen", "source_id": "uuid-r", "chunk_number": 1}
        got = _rl_refetch_node_vector(
            node, {"uuid-r": [sib]}, {}, "qwen3_embed",
            model_name="qwen3-embedding:0.6b", allow_regen=True,
        )
        assert got == [0.123, 0.456]
        assert called["n"] == 1


# ----------------------------------------------------------------------
# v0.2.73 retrieval-lock guard at the ENRICH level: a group whose fetched
# objects ALL lack the active slot must not fire a synchronous per-node embed.
# ----------------------------------------------------------------------


class TestRetrievalLockSlotAbsentGuard:
    def _run(self, monkeypatch, fetched_objs, active_slot):
        import claude_mcp_servers.weaviate_mcp.server as srv

        called = {"n": 0}

        def _spy_regen(text, model):
            called["n"] += 1
            return [0.9, 0.9]

        monkeypatch.setattr(srv, "_rl_regenerate_node_vector", _spy_regen)
        coll = _fake_coll(fetched_objs)
        # Three nodes, none carrying a pre-existing emb → each would hit the
        # refetch path; without the guard each fires a blocking embed.
        nodes = [
            {"title": f"N{i}", "source_id": f"uuid-{i}", "chunk_number": 1,
             "collection": "Proj_KnowledgeGraph"}
            for i in range(3)
        ]
        _rl_enrich_nodes_with_linked_embs(
            nodes, query_emb=None, active_slot=active_slot,
            coll_resolver=_resolver_for({"Proj_KnowledgeGraph": coll}),
            model_name="qwen3-embedding:0.6b",
        )
        return called["n"]

    def test_slot_absent_group_skips_all_regens(self, monkeypatch) -> None:
        # Every fetched object carries ONLY a foreign slot → active slot absent
        # group-wide → ZERO synchronous embeds (the lock is avoided) even though
        # all 3 nodes lack emb and have recoverable text.
        foreign = [
            SimpleNamespace(
                properties={"source_node_id": f"uuid-{i}", "chunk_num": 1,
                            "content": "body", "title": f"N{i}"},
                vector={"ollama_embed": [0.1, 0.2]},  # foreign slot only
            )
            for i in range(3)
        ]
        n_regens = self._run(monkeypatch, foreign, "qwen3_embed")
        assert n_regens == 0

    def test_slot_present_group_allows_regen(self, monkeypatch) -> None:
        # At least one object carries the active slot → the group is healthy →
        # a node that still misses (no matching sibling) may regen. Here one
        # object HAS the slot (so the probe passes) but a different node's
        # source_id has no vector-bearing sibling → its regen is allowed.
        mixed = [
            SimpleNamespace(
                properties={"source_node_id": "uuid-0", "chunk_num": 1,
                            "content": "body0", "title": "N0"},
                vector={"qwen3_embed": [0.5, 0.5]},  # active slot present
            ),
            SimpleNamespace(
                properties={"source_node_id": "uuid-1", "chunk_num": 1,
                            "content": "body1", "title": "N1"},
                vector={},  # this node's sibling has no vector → regen path
            ),
        ]
        n_regens = self._run(monkeypatch, mixed, "qwen3_embed")
        # uuid-0 recovered from its own slot (no regen); uuid-1 + uuid-2 miss
        # but the slot IS present group-wide → regen allowed to fire.
        assert n_regens >= 1


# ----------------------------------------------------------------------
# F-C CORRECTED — embed_regen.regenerate_node_vector shared helper.
# ----------------------------------------------------------------------


class TestEmbedRegenHelper:
    def test_regenerates_via_injected_service(self) -> None:
        from claude_mcp_servers.rl_client.embed_regen import regenerate_node_vector

        class _Svc:
            def embed_text(self, text):
                return [0.9, 0.8, 0.7]

        got = regenerate_node_vector("some node text", "qwen3-embedding:0.6b",
                                     embedding_service=_Svc())
        assert got == [0.9, 0.8, 0.7]

    def test_empty_text_returns_none(self) -> None:
        from claude_mcp_servers.rl_client.embed_regen import regenerate_node_vector

        class _Svc:
            def embed_text(self, text):
                return [1.0]

        assert regenerate_node_vector("", "m", embedding_service=_Svc()) is None
        assert regenerate_node_vector("   ", "m", embedding_service=_Svc()) is None

    def test_no_service_returns_none(self) -> None:
        from claude_mcp_servers.rl_client.embed_regen import regenerate_node_vector
        # No service injected and the lazy server resolver returns None in this
        # test context → None (genuine failure → caller drops).
        import claude_mcp_servers.weaviate_mcp.server as srv
        import unittest.mock as mock
        with mock.patch.object(srv, "_get_embedding_service", return_value=None):
            assert regenerate_node_vector("text", "m") is None

    def test_embed_failure_returns_none(self) -> None:
        from claude_mcp_servers.rl_client.embed_regen import regenerate_node_vector

        class _Svc:
            def embed_text(self, text):
                raise RuntimeError("embed service down")

        assert regenerate_node_vector("text", "m", embedding_service=_Svc()) is None

    def test_model_aware_chunk_sizing_truncates_to_model_preset(self) -> None:
        """The OTHER-slot backfill must size text to the OTHER model's preset.

        A model with a SMALL context (arctic/medium) truncates more than one
        with a LARGE context (qwen3/xlarge) for the same long body — the chunk
        asymmetry constraint. We assert the embed callable received a SHORTER
        sized text for the small-context model than for the large-context one.
        """
        from claude_mcp_servers.rl_client.embed_regen import regenerate_node_vector

        seen: dict[str, int] = {}

        def _make_fn(tag):
            def _fn(sized_text):
                seen[tag] = len(sized_text)
                return [0.1, 0.2, 0.3]
            return _fn

        long_text = "word " * 40000  # well past either preset's max-token cap
        regenerate_node_vector(long_text, "snowflake-arctic-embed2",
                               embed_fn=_make_fn("arctic"))
        regenerate_node_vector(long_text, "qwen3-embedding:0.6b",
                               embed_fn=_make_fn("qwen3"))
        # arctic (medium_context max ~3200 tok) truncates harder than qwen3
        # (xlarge_context max ~13500 tok), so the sized arctic text is shorter.
        assert seen["arctic"] < seen["qwen3"]


# ----------------------------------------------------------------------
# v0.2.71 Sweep-C — ensure_slot_embedding (compute + async store-back).
# ----------------------------------------------------------------------


class TestEnsureSlotEmbedding:
    def test_missing_slot_computed_returned_and_store_scheduled(self) -> None:
        """Active-slot self-heal: a node missing its slot gets the vector
        computed + returned, and the store-back is scheduled (in an event loop)."""
        import asyncio

        from claude_mcp_servers.rl_client.embed_regen import (
            ensure_slot_embedding,
            _store_back_tasks,
        )

        class _Svc:
            def embed_text(self, text):
                return [0.5, 0.6, 0.7]

        class _Coll:
            def __init__(self):
                self.updated = []

            class _Data:
                def __init__(self, outer):
                    self._outer = outer

                def update(self, *, uuid, vector):
                    self._outer.updated.append((uuid, vector))

            @property
            def data(self):
                return _Coll._Data(self)

        async def _inner():
            coll = _Coll()
            vec = ensure_slot_embedding(
                "uuid-1", "node content text", "qwen3_embed",
                "qwen3-embedding:0.6b", coll, _Svc(),
            )
            # Vector returned immediately for THIS request.
            assert vec == [0.5, 0.6, 0.7]
            # A store-back task was scheduled (strong-ref'd so GC can't drop it).
            assert len(_store_back_tasks) >= 1
            # Let the fire-and-forget store run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert coll.updated == [("uuid-1", {"qwen3_embed": [0.5, 0.6, 0.7]})]

        asyncio.run(_inner())

    def test_no_text_returns_none_no_store(self) -> None:
        """Empty/whitespace content → None, no compute, no store."""
        from claude_mcp_servers.rl_client.embed_regen import ensure_slot_embedding

        class _Svc:
            def embed_text(self, text):
                return [1.0]

        assert ensure_slot_embedding(
            "u", "", "qwen3_embed", "qwen3-embedding:0.6b", object(), _Svc()
        ) is None
        assert ensure_slot_embedding(
            "u", "   ", "qwen3_embed", "qwen3-embedding:0.6b", object(), _Svc()
        ) is None

    def test_embed_down_returns_none_so_caller_keeps_skip(self) -> None:
        """When the embed call fails (single-slot install / service down) the
        OTHER slot is NOT generated → None, caller keeps its skip/drop path."""
        from claude_mcp_servers.rl_client.embed_regen import ensure_slot_embedding

        class _Svc:
            def embed_text(self, text):
                raise RuntimeError("ollama down")

        # embed_fn (other-slot path) also failing → None.
        def _embed_fn(_text):
            raise RuntimeError("other model down")

        assert ensure_slot_embedding(
            "u", "some text", "qwen3_embed", "qwen3-embedding:0.6b",
            object(), _Svc(), embed_fn=_embed_fn,
        ) is None

    def test_sync_context_returns_vector_skips_store(self) -> None:
        """With no running event loop (CLI/sync test) the vector is still
        returned for immediate use; the store is skipped rather than blocking."""
        from claude_mcp_servers.rl_client.embed_regen import ensure_slot_embedding

        class _Svc:
            def embed_text(self, text):
                return [0.9, 0.8]

        class _Coll:
            def __init__(self):
                self.updated = []

            class _Data:
                def __init__(self, outer):
                    self._outer = outer

                def update(self, *, uuid, vector):  # pragma: no cover
                    self._outer.updated.append((uuid, vector))

            @property
            def data(self):
                return _Coll._Data(self)

        coll = _Coll()
        # Called directly (no asyncio.run) → no running loop.
        vec = ensure_slot_embedding(
            "u", "text", "qwen3_embed", "qwen3-embedding:0.6b", coll, _Svc()
        )
        assert vec == [0.9, 0.8]
        assert coll.updated == []  # store skipped (no loop), vector still returned


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
