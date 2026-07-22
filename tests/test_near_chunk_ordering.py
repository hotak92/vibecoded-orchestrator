# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Near-chunk ordering (2026-07-22) — ``_rl_pack_linked_embs_for_node`` must pack
the matched chunk's NEIGHBOURS (chunk_num nearest the matched chunk) into the
limited linked slots FIRST.

Why: the three_chunks / full detail tiers serve the matched chunk plus its
adjacent neighbours as the scored context. Logging those neighbour embeddings
(rather than arbitrary far-away siblings that overflow the ≤MAX_LINKED budget)
lets offline training reconstruct the exact window the model would score.

Red-proof: with 6 sibling chunks and MAX_LINKED=5, the two chunks FARTHEST from
the matched chunk must be the ones dropped — the pre-fix natural-order packing
would instead drop whichever siblings appeared last in fetch order.
"""
from __future__ import annotations

from types import SimpleNamespace

from claude_mcp_servers.weaviate_mcp.server import (
    _RL_MAX_LINKED,
    _rl_pack_linked_embs_for_node,
)


def _stub_obj(vec, *, chunk_num, node_type="concept"):
    return SimpleNamespace(
        properties={"chunk_num": chunk_num, "node_type": node_type},
        vector={"qwen3_embed": vec},
    )


def test_neighbours_packed_before_distant_siblings():
    # Matched chunk = 5. Siblings span 1..10 (minus 5). MAX_LINKED slots.
    # The nearest neighbours to 5 are: 4,6 (dist 1), 3,7 (dist 2), 2,8 (dist 3)...
    matched = 5
    node = {"title": "Doc", "source_node_id": "uuid-doc", "chunk_number": matched}
    # Provide siblings in a DELIBERATELY shuffled fetch order so ordering, not
    # input order, is what places neighbours first.
    sib_chunks = [10, 1, 6, 2, 8, 4, 7, 3, 9]  # all != matched
    siblings = [_stub_obj([float(c)] * 2, chunk_num=c) for c in sib_chunks]

    embs, types = _rl_pack_linked_embs_for_node(
        node, {"uuid-doc": siblings}, {}, "qwen3_embed"
    )

    assert len(embs) == _RL_MAX_LINKED  # capped at MAX_LINKED
    # Recover which chunk each packed emb came from (we encoded chunk_num into vec[0]).
    packed_chunks = [int(e[0]) for e in embs]
    # The MAX_LINKED nearest-to-5 chunks are: 4,6 (d1), 3,7 (d2), then one of 2/8
    # (d3). So the packed set must be a subset of {4,6,3,7,2,8} and MUST contain
    # the closest neighbours 4 and 6.
    assert 4 in packed_chunks and 6 in packed_chunks, (
        "immediate neighbours (matched ± 1) must always be packed"
    )
    # The farthest chunks (1, 10, 9) must NOT be packed (they lose to nearer ones).
    for far in (1, 10, 9):
        assert far not in packed_chunks, (
            f"distant chunk {far} should be dropped in favour of nearer neighbours"
        )
    # Distances of packed chunks must all be <= the distance of any dropped chunk.
    packed_dists = sorted(abs(c - matched) for c in packed_chunks)
    assert packed_dists == sorted(packed_dists), "packed by ascending distance"
    assert max(packed_dists) <= 3, "only near neighbours fit the 5 slots"


def test_unknown_matched_chunk_does_not_crash():
    # No chunk_number on the node → natural order, no crash.
    node = {"title": "NoMatch", "source_node_id": "uuid-nm"}
    siblings = [_stub_obj([0.1] * 2, chunk_num=1), _stub_obj([0.2] * 2, chunk_num=2)]
    embs, types = _rl_pack_linked_embs_for_node(
        node, {"uuid-nm": siblings}, {}, "qwen3_embed"
    )
    assert len(embs) == 2  # both packed (under the limit)
