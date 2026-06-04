# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.47 RL-6a: tests for ``_rl_pack_linked_embs_for_node``.

The helper packs MAX_LINKED linked-slot vectors per node in the exact order
the paid-module's online training step builds them
(``retrieval_rl.py::_train_rl_model:914-918``):

    linked_raws = extra_chunks_of_same_node + actual_linked_nodes

Offline replay reads this exact order and feeds it into
``_rl_model.update(..., linked_raws=...)`` byte-identically. These tests
pin the packing contract so a future refactor doesn't silently flip the
order or drop the MAX_LINKED truncation.

Tests use lightweight stub Weaviate-object shapes (a class with a
``properties: dict`` attribute and a ``vector: dict`` attribute) since
``_extract_obj_vector`` only reads those two attributes — full
``weaviate.classes.Object`` instances would force a Weaviate dependency
into the test runner for no value.
"""

from __future__ import annotations

from types import SimpleNamespace

from claude_mcp_servers.weaviate_mcp.server import (
    _RL_MAX_LINKED,
    _rl_pack_linked_embs_for_node,
)


def _stub_obj(vec: list[float], slot: str = "qwen3_embed", **props) -> SimpleNamespace:
    """Build a stub object that satisfies ``_extract_obj_vector`` + property reads.

    `_extract_obj_vector(obj, slot)` reads `obj.vector[slot]` (named-vector
    schema) OR `obj.vector[default]`. We give it the named-vector shape.
    """
    return SimpleNamespace(properties=props, vector={slot: vec})


class TestEmptyInputs:
    def test_no_siblings_no_links_returns_empty(self) -> None:
        node = {"title": "A", "source_node_id": "uuid-a", "chunk_number": 1}
        embs, types = _rl_pack_linked_embs_for_node(node, {}, {}, "qwen3_embed")
        assert embs == []
        assert types == []

    def test_missing_node_metadata_does_not_crash(self) -> None:
        node = {}  # no title, no source_node_id, no chunk_number, no links
        embs, types = _rl_pack_linked_embs_for_node(node, {}, {}, "qwen3_embed")
        assert embs == []
        assert types == []


class TestExtraChunksPacking:
    def test_packs_sibling_chunks_excluding_matched(self) -> None:
        node = {
            "title": "Foo",
            "node_type": "concept",
            "source_node_id": "uuid-foo",
            "chunk_number": 2,  # this chunk is n_emb, not a sibling
        }
        siblings = [
            _stub_obj([0.1, 0.1], chunk_num=1),
            _stub_obj([0.2, 0.2], chunk_num=2),  # MUST be excluded (matched chunk)
            _stub_obj([0.3, 0.3], chunk_num=3),
        ]
        embs, types = _rl_pack_linked_embs_for_node(
            node, {"uuid-foo": siblings}, {}, "qwen3_embed"
        )
        # 2 siblings (chunks 1 + 3), matched chunk 2 dropped.
        assert embs == [[0.1, 0.1], [0.3, 0.3]]
        # All extra chunks share the parent's node_type ("concept").
        assert types == ["concept", "concept"]

    def test_sibling_with_no_vector_is_skipped(self) -> None:
        node = {"title": "Bar", "source_node_id": "uuid-bar", "chunk_number": 1}
        siblings = [
            SimpleNamespace(properties={"chunk_num": 2}, vector={}),  # no vector
            _stub_obj([0.5, 0.5], chunk_num=3),
        ]
        embs, types = _rl_pack_linked_embs_for_node(
            node, {"uuid-bar": siblings}, {}, "qwen3_embed"
        )
        assert embs == [[0.5, 0.5]]
        assert types == ["concept"]

    def test_source_id_falls_back_to_title(self) -> None:
        """When source_node_id is absent (rare; some older schema rows),
        the helper falls back to keying by title."""
        node = {"title": "TitleFallback", "chunk_number": 1}
        siblings = [_stub_obj([0.7, 0.7], chunk_num=2)]
        embs, types = _rl_pack_linked_embs_for_node(
            node, {"TitleFallback": siblings}, {}, "qwen3_embed"
        )
        assert embs == [[0.7, 0.7]]


class TestActualLinksPacking:
    def test_packs_resolved_link_objects(self) -> None:
        node = {
            "title": "Foo",
            "node_type": "concept",
            "links": ["LinkA", "LinkB"],
        }
        link_objs = {
            "LinkA": _stub_obj([0.1, 0.1], node_type="tool"),
            "LinkB": _stub_obj([0.2, 0.2], node_type="concept"),
        }
        embs, types = _rl_pack_linked_embs_for_node(node, {}, link_objs, "qwen3_embed")
        assert embs == [[0.1, 0.1], [0.2, 0.2]]
        # Each link carries its OWN node_type (not the parent's).
        assert types == ["tool", "concept"]

    def test_typed_wikilink_prefix_stripped(self) -> None:
        node = {"title": "Foo", "links": ["uses::Tool"]}
        link_objs = {"Tool": _stub_obj([0.4, 0.4], node_type="tool")}
        embs, types = _rl_pack_linked_embs_for_node(node, {}, link_objs, "qwen3_embed")
        assert embs == [[0.4, 0.4]]
        assert types == ["tool"]

    def test_wikilink_brackets_stripped(self) -> None:
        node = {"title": "Foo", "links": ["[[Tool]]"]}
        link_objs = {"Tool": _stub_obj([0.6, 0.6], node_type="concept")}
        embs, _ = _rl_pack_linked_embs_for_node(node, {}, link_objs, "qwen3_embed")
        assert embs == [[0.6, 0.6]]

    def test_unresolved_link_silently_skipped(self) -> None:
        node = {"title": "Foo", "links": ["LinkA", "MissingLink", "LinkC"]}
        link_objs = {
            "LinkA": _stub_obj([0.1, 0.1], node_type="concept"),
            "LinkC": _stub_obj([0.3, 0.3], node_type="concept"),
        }
        embs, _ = _rl_pack_linked_embs_for_node(node, {}, link_objs, "qwen3_embed")
        # MissingLink is silently dropped; ordering preserved for the rest.
        assert embs == [[0.1, 0.1], [0.3, 0.3]]

    def test_node_type_falls_back_to_concept(self) -> None:
        node = {"title": "Foo", "links": ["LinkA"]}
        link_objs = {
            "LinkA": _stub_obj([0.1, 0.1])  # no node_type prop
        }
        embs, types = _rl_pack_linked_embs_for_node(node, {}, link_objs, "qwen3_embed")
        assert embs == [[0.1, 0.1]]
        assert types == ["concept"]


class TestPackingOrder:
    """The container's online step builds linked_raws as:
        extra_chunks_of_this_node + actual_linked_nodes
    Offline MUST replay this exact order."""

    def test_extras_come_before_links(self) -> None:
        node = {
            "title": "Foo",
            "node_type": "concept",
            "source_node_id": "uuid-foo",
            "chunk_number": 1,
            "links": ["LinkA"],
        }
        siblings = [_stub_obj([0.9, 0.9], chunk_num=2)]  # extra chunk
        link_objs = {"LinkA": _stub_obj([0.1, 0.1], node_type="tool")}
        embs, types = _rl_pack_linked_embs_for_node(
            node, {"uuid-foo": siblings}, link_objs, "qwen3_embed"
        )
        # Extras (1) then links (1).
        assert embs == [[0.9, 0.9], [0.1, 0.1]]
        assert types == ["concept", "tool"]


class TestMaxLinkedTruncation:
    def test_truncates_extras_alone_at_max(self) -> None:
        node = {
            "title": "Foo",
            "source_node_id": "uuid-foo",
            "chunk_number": 1,
            "links": ["LinkA"],  # would normally append after extras
        }
        # 7 siblings, but we only ship MAX_LINKED = 5.
        siblings = [
            _stub_obj([float(i), 0.0], chunk_num=i + 2)
            for i in range(7)
        ]
        link_objs = {"LinkA": _stub_obj([99.0, 99.0], node_type="tool")}
        embs, types = _rl_pack_linked_embs_for_node(
            node, {"uuid-foo": siblings}, link_objs, "qwen3_embed"
        )
        assert len(embs) == _RL_MAX_LINKED == 5
        # Links never get appended — extras consumed the whole budget.
        assert [99.0, 99.0] not in embs

    def test_extras_partial_then_links_fill(self) -> None:
        node = {
            "title": "Foo",
            "source_node_id": "uuid-foo",
            "chunk_number": 1,
            "links": ["L1", "L2", "L3", "L4"],
        }
        # 2 extra chunks. Links fill slots 3..5 (cap MAX_LINKED=5).
        siblings = [
            _stub_obj([1.0, 1.0], chunk_num=2),
            _stub_obj([2.0, 2.0], chunk_num=3),
        ]
        link_objs = {
            f"L{i}": _stub_obj([float(i + 10), float(i + 10)], node_type="concept")
            for i in range(1, 5)
        }
        embs, types = _rl_pack_linked_embs_for_node(
            node, {"uuid-foo": siblings}, link_objs, "qwen3_embed"
        )
        assert len(embs) == 5
        # First 2 = extras; remaining 3 = first 3 links.
        assert embs[:2] == [[1.0, 1.0], [2.0, 2.0]]
        assert embs[2:] == [[11.0, 11.0], [12.0, 12.0], [13.0, 13.0]]
        # L4 dropped at the cap.
        assert [14.0, 14.0] not in embs
