# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""G2 / G2b (2026-07-22) — offline trainer must consume the linked-type and
linked-embedding fields the LOGGER actually writes.

Root cause fixed:
  * G2  — the logger's ``serialize_node_record`` emits ``linked_type_names``;
          the trainer's ``extract_samples`` used to read ``link_types`` (a name
          the writer never emits), so offline per-link type features were always
          empty. Fix: read ``linked_type_names`` (``link_types`` kept as a
          forward-compat fallback only). The LOG field is never renamed — the
          34k+ stored rows are the source of truth.
  * G2b — the v3 event carries ``linked_embs``; the trainer's ``_epoch`` used to
          zero-pad every linked slot ("not in log", stale comment). Fix: stack
          the stored ``linked_embs`` into the entity tensor and compute the real
          per-slot cosines, so the v2 net's 5 linked entity tokens train on the
          same graph context the online path uses.

Call-site pinning (memory lesson: pin the call site, not the pure function): the
fixture node record is produced by the REAL ``serialize_node_record`` from the
free repo, so a future change to the logger's field names/order breaks this test
immediately instead of silently re-orphaning the offline features.

Red-proof: a pre-fix loader/epoch (link field read as ``link_types``, linked
slots zeroed) produces ALL-EMPTY link-type idxs and ZERO linked-slot energy;
the assertions below fail against that behaviour and pass against the fix.
"""
from __future__ import annotations


import numpy as np

# Pin the call site: use the real logger serializer (the MCP-side canonical
# ``rl_client.rl_logger``) so the fixture is byte-shaped exactly as production
# writes it. Both packages live in this repository, so the import is
# unconditional: a failure here is a broken checkout, not a skip.
from rl_client.rl_logger import serialize_node_record

from rl_reranker.offline_trainer import extract_samples
from rl_reranker.rl_model import UNKNOWN_TYPE_IDX


_EMB_DIM = 8


def _vec(seed: float) -> list[float]:
    rng = np.random.default_rng(int(seed * 1000))
    return (rng.random(_EMB_DIM).astype(np.float32)).tolist()


def _build_retrieval_event(node_rec: dict) -> dict:
    return {
        "event": "retrieval",
        "schema_version": 3,
        "task_id": "t-g2",
        "task_type": "pre_edit_kg_search",
        "embedding_source": "qwen3",
        "embedding_dim": _EMB_DIM,
        "embedding_model": "qwen3-embedding:0.6b",
        "query_emb": _vec(0.1),
        "nodes": [node_rec],
    }


def _build_citation_event(title: str, cosine: float) -> dict:
    return {
        "event": "citation",
        "schema_version": 3,
        "task_id": "t-g2",
        "task_type": "pre_edit_kg_search",
        "cosine_sims": {title: cosine},
        "literal_cited": {title: True},
        "citations": {title: True},
    }


def test_extract_samples_reads_linked_type_names_and_linked_embs() -> None:
    # Build the node record via the REAL logger serializer (call-site pin).
    node_rec = serialize_node_record(
        {
            "title": "Alpha",
            "score": 0.91,
            "tier": "top_k",
            "n_emb": _vec(0.2),
            # Two real linked-slot vectors + their type names (index-aligned).
            "linked_embs": [_vec(0.3), _vec(0.4)],
            "linked_type_names": ["tool", "concept"],
            "node_type": "concept",
        },
        include_links=True,
        include_shown_rank=True,
        include_chunks_matched=True,
        include_best_chunk_number=True,
        include_code_path_fields=True,
    )
    # Sanity: the logger writes linked_type_names (NOT link_types) + linked_embs.
    assert "linked_type_names" in node_rec
    assert "link_types" not in node_rec
    assert node_rec.get("linked_embs")

    ret = _build_retrieval_event(node_rec)
    cit = _build_citation_event("Alpha", cosine=0.8)

    samples = extract_samples([(ret, cit)], link_sim={})
    assert len(samples) == 1
    s = samples[0]

    # G2: link-type idxs come from linked_type_names -> builtin ints, NOT the
    # all-UNKNOWN/empty result a link_types read would give.
    assert s["link_type_idxs"], "link_type_idxs must be non-empty (G2 fixed)"
    assert len(s["link_type_idxs"]) == 2
    # "tool" and "concept" are builtin node types -> real (non-UNKNOWN) indices.
    assert all(idx != UNKNOWN_TYPE_IDX for idx in s["link_type_idxs"]), (
        "builtin link types must map to real indices, not UNKNOWN"
    )

    # G2b: linked_embs survive into the sample as real vectors.
    assert s["linked_embs"], "linked_embs must be non-empty (G2b fixed)"
    assert len(s["linked_embs"]) == 2
    for lv in s["linked_embs"]:
        assert getattr(lv, "shape", None) == (_EMB_DIM,)


def test_pre_fix_loader_would_fail_link_features() -> None:
    """Red-proof: a PRE-FIX reader (link_types + no linked_embs) yields empty
    link features from the exact same logged node — proving the fix is load-
    bearing, not incidental."""
    node_rec = serialize_node_record(
        {
            "title": "Beta",
            "score": 0.7,
            "tier": "top_k",
            "n_emb": _vec(0.5),
            "linked_embs": [_vec(0.6)],
            "linked_type_names": ["tool"],
            "node_type": "concept",
        },
        include_links=True,
    )

    # Simulate the PRE-FIX behaviour on this real record.
    pre_fix_link_types = node_rec.get("link_types") or []  # writer never emits it
    pre_fix_linked_embs = []  # pre-fix extract_samples never stored linked_embs

    assert pre_fix_link_types == [], (
        "pre-fix read of link_types is empty on a real logged record — the "
        "silent feature amputation this fix removes"
    )
    assert pre_fix_linked_embs == []

    # The FIXED path recovers both.
    ret = _build_retrieval_event(node_rec)
    cit = _build_citation_event("Beta", cosine=0.75)
    s = extract_samples([(ret, cit)], link_sim={})[0]
    assert s["link_type_idxs"] and s["linked_embs"], (
        "fixed path recovers link-type idxs + linked embeddings the pre-fix "
        "path dropped"
    )


def test_epoch_builds_nonzero_linked_slot_energy() -> None:
    """G2b end-to-end: run a real _epoch batch and assert the entity tensor's
    linked slots carry non-zero energy (they were all-zero pre-fix)."""
    from rl_reranker.offline_trainer import _epoch
    from rl_reranker.rl_model import _RLNet

    node_rec = serialize_node_record(
        {
            "title": "Gamma",
            "score": 0.88,
            "tier": "top_k",
            "n_emb": _vec(0.7),
            "linked_embs": [_vec(0.71), _vec(0.72), _vec(0.73)],
            "linked_type_names": ["tool", "concept", "model"],
            "node_type": "concept",
        },
        include_links=True,
    )
    ret = _build_retrieval_event(node_rec)
    cit = _build_citation_event("Gamma", cosine=0.82)
    samples = extract_samples([(ret, cit)], link_sim={})
    assert samples

    # Capture the entity tensor the net actually receives by wrapping the net's
    # forward to record its inputs, then run one eval epoch.
    net = _RLNet(_EMB_DIM)
    captured = {}
    orig_forward = net.forward

    def _spy(entities, type_idxs, slot_scalars):
        captured["entities"] = entities.detach().clone()
        captured["slot_scalars"] = slot_scalars.detach().clone()
        return orig_forward(entities, type_idxs, slot_scalars)

    net.forward = _spy  # type: ignore[method-assign]
    _epoch(net, samples, opt=None, batch_size=8, lambda_rank=0.5,
           training=False, emb_dim=_EMB_DIM)

    ent = captured["entities"]  # (B, 7, emb_dim)
    # Slots 2,3,4 (first three linked slots) must be non-zero (3 links stored).
    linked_energy = ent[:, 2:5, :].abs().sum().item()
    assert linked_energy > 0.0, (
        "linked entity slots are all zero — G2b regression (pre-fix behaviour)"
    )
    # The 5th linked slot (index 6) has no link -> must stay zero padding.
    pad_energy = ent[:, 6, :].abs().sum().item()
    assert pad_energy == 0.0, "unused linked slot must stay zero-padded"

    # Slot scalars for the linked slots must be non-(0,0) where a link exists.
    ss = captured["slot_scalars"]  # (B, 7, 2)
    assert ss[:, 2, :].abs().sum().item() > 0.0, (
        "first linked slot's cosine scalars are (0,0) — pre-fix zeroing"
    )
