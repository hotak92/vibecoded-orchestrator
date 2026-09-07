# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Defect 2 (v0.2.92) — a truncated secondary vector is identifiable from the
RL event ALONE, via the per-node ``emb_truncated`` field (schema v4).

Pre-fix, the truncation fact existed only as the ``secondary_truncated_slots``
Weaviate chunk property; dataset assembly reads ``launcher.db.rl_events`` and
the RL event carried ``emb_other`` with NO truncation indicator — so the
stated purpose ("partition truncated arctic vectors from stored events")
was impossible without a Weaviate join.

Three-state contract (user-ruled): months of existing telemetry must stay
trainable. ``emb_truncated`` is ``true`` / ``false`` only where the event
KNOWS the state; ABSENT means ``unknown`` — never "not truncated":

  * pre-v4 events predate the field (the schema_version itself is evidence);
  * v4 events whose other-slot vector was backfilled on the fly;
  * active-slot events, which carry no persisted truncation state.

``resolve_emb_truncation_state`` is the ONE reader; the naive
``node.get("emb_truncated", False)`` is precisely the defect — it coerces
the entire pre-v4 corpus to "not truncated", and the actually-truncated rows
are exactly the ones that would poison secondary-slot training. The consumer
POLICY for unknown rows is deferred to the paid-module trainer (user ruling:
"the training algorithm will be adapted to use it when present").
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.rl_client.rl_logger import (  # noqa: E402
    RLDataLogger,
    TRUNCATION_FALSE,
    TRUNCATION_TRUE,
    TRUNCATION_UNKNOWN,
    resolve_emb_truncation_state,
    serialize_node_record,
)

SCHEMA_VERSION = RLDataLogger.SCHEMA_VERSION  # 4 as of this writing


# ---------------------------------------------------------------------------
# The resolver: tri-state, version-gated, absence = unknown (never False)
# ---------------------------------------------------------------------------


def test_schema_version_is_4():
    """The field exists from v4 on — the version a historical-case reader
    gates on."""
    assert SCHEMA_VERSION == 4


def test_resolver_v4_true_false_and_absent():
    node_true = {"title": "a", "emb": [0.1], "emb_truncated": True}
    node_false = {"title": "b", "emb": [0.1], "emb_truncated": False}
    node_absent = {"title": "c", "emb": [0.1]}
    assert resolve_emb_truncation_state(node_true, 4) == TRUNCATION_TRUE
    assert resolve_emb_truncation_state(node_false, 4) == TRUNCATION_FALSE
    assert resolve_emb_truncation_state(node_absent, 4) == TRUNCATION_UNKNOWN


def test_resolver_version_gate_pre_v4_stray_field_is_unknown():
    """A pre-v4 event CANNOT know the state — even a stray field copy must
    not be trusted (the schema_version is the evidence)."""
    stray = {"title": "a", "emb": [0.1], "emb_truncated": True}
    assert resolve_emb_truncation_state(stray, 3) == TRUNCATION_UNKNOWN


def test_resolver_trusts_field_when_version_not_supplied():
    """``schema_version=None`` skips the gate: a field that IS present on the
    record is trusted as-is (the caller-without-envelope case)."""
    assert resolve_emb_truncation_state({"emb_truncated": True}, None) == TRUNCATION_TRUE
    assert resolve_emb_truncation_state({"emb_truncated": False}, None) == TRUNCATION_FALSE
    assert resolve_emb_truncation_state({}, None) == TRUNCATION_UNKNOWN


def test_resolver_tolerates_garbage():
    assert resolve_emb_truncation_state(None, 4) == TRUNCATION_UNKNOWN
    assert resolve_emb_truncation_state("not-a-dict", 4) == TRUNCATION_UNKNOWN
    # Non-bool truthy/falsy values are NOT real answers → unknown.
    assert (
        resolve_emb_truncation_state({"emb_truncated": "yes"}, 4)
        == TRUNCATION_UNKNOWN
    )


def test_historical_v3_event_resolves_unknown_never_false():
    """THE historical case (red-proofed against the naive implementation):
    a genuine pre-v4 serialized node — no field, because none could exist —
    must resolve UNKNOWN. The naive ``node.get("emb_truncated", False)``
    returns False for exactly this record, silently mislabelling every
    truncated row in the pre-v4 corpus as full-fidelity; that coercion is
    the defect this tri-state exists to prevent."""
    historical_node = serialize_node_record({"title": "old", "score": 0.5, "emb": [0.1]})
    assert "emb_truncated" not in historical_node, (
        "a node that never knew a truncation state must serialize WITHOUT "
        "the field (absence is the unknown marker)"
    )
    state = resolve_emb_truncation_state(historical_node, 3)
    assert state == TRUNCATION_UNKNOWN
    assert state != TRUNCATION_FALSE, (
        "unknown is NOT false — historical truncated rows must never be "
        "read as full-fidelity"
    )
    # Document the hazard the naive read would create on THIS record:
    assert historical_node.get("emb_truncated", False) is False, (
        "the naive .get(..., False) default WOULD return False here — the "
        "resolver must (and does) disagree with it"
    )


# ---------------------------------------------------------------------------
# serialize_node_record: emission + byte-order contract
# ---------------------------------------------------------------------------


def test_serialize_emits_true_false_and_absent():
    rec_true = serialize_node_record({"title": "a", "emb": [0.1], "emb_truncated": True})
    rec_false = serialize_node_record({"title": "b", "emb": [0.1], "emb_truncated": False})
    rec_absent = serialize_node_record({"title": "c", "emb": [0.1]})
    assert rec_true["emb_truncated"] is True
    assert rec_false["emb_truncated"] is False, (
        "an explicit False is a real full-fidelity answer — it must survive "
        "serialization, not be dropped for falsiness"
    )
    assert "emb_truncated" not in rec_absent


def test_serialize_byte_order_emb_truncated_directly_after_emb():
    """The record's insertion order is a fixed contract (JSON preserves it;
    the byte-order test pins it). ``emb_truncated`` sits between ``emb`` and
    ``linked_embs``."""
    rec = serialize_node_record(
        {
            "title": "a",
            "emb": [0.1],
            "emb_truncated": True,
            "linked_embs": [[0.2]],
            "linked_type_names": ["concept"],
            "node_type": "concept",
        }
    )
    keys = list(rec.keys())
    assert keys.index("emb_truncated") == keys.index("emb") + 1
    assert keys.index("emb_truncated") < keys.index("linked_embs")


# ---------------------------------------------------------------------------
# The carry chain: enrichment → other-slot log-nodes → serialized record
# ---------------------------------------------------------------------------


def _rle():
    import importlib

    return importlib.import_module("weaviate_mcp.rl_enrichment")


def _srv():
    import importlib

    return importlib.import_module("weaviate_mcp.server")


class _FakeRepObj:
    def __init__(self, vector, properties):
        self.uuid = "00000000-0000-0000-0000-000000000001"
        self.vector = vector
        self.properties = properties


def _attach(node, rep_obj, **kwargs):
    rle = _rle()
    rle._rl_attach_other_slot_for_node(
        node,
        rep_obj,
        other_slot=kwargs.get("other_slot", "arctic2_embed"),
        other_query_emb=kwargs.get("other_query_emb"),
        other_model_name=kwargs.get("other_model_name", "snowflake-arctic-embed2:latest"),
        backfill_other=kwargs.get("backfill_other", False),
        coll_for_backfill=kwargs.get("coll_for_backfill"),
    )
    return node


def test_enrichment_attaches_true_from_persisted_property():
    """A STORAGE-read vector: the state comes from the chunk's persisted
    ``secondary_truncated_slots`` — slot listed → True."""
    node = {"title": "n"}
    rep = _FakeRepObj(
        {"arctic2_embed": [0.1, 0.2]},
        {"secondary_truncated_slots": ["arctic2_embed"]},
    )
    _attach(node, rep)
    assert node["emb_other"] == [0.1, 0.2]
    assert node["emb_other_truncated"] is True


def test_enrichment_attaches_false_when_slot_not_listed():
    """Explicit False: a storage vector whose slot is NOT in the truncated
    list is full-fidelity — a real answer, carried as False."""
    node = {"title": "n"}
    rep = _FakeRepObj(
        {"arctic2_embed": [0.1, 0.2]},
        {"secondary_truncated_slots": ["qwen3_embed"]},
    )
    _attach(node, rep)
    assert node["emb_other_truncated"] is False


def test_enrichment_leaves_state_unset_when_property_missing():
    """A pre-R3-2 row (no property at all) leaves the state UNSET → unknown
    downstream, never a guessed False."""
    node = {"title": "n"}
    rep = _FakeRepObj({"arctic2_embed": [0.1, 0.2]}, {})
    _attach(node, rep)
    assert node["emb_other"] == [0.1, 0.2]
    assert "emb_other_truncated" not in node


def test_enrichment_backfilled_vector_stays_unknown(monkeypatch):
    """A vector BACKFILLED on the fly has no knowable state at the attach
    site — it must stay unset (unknown), never a guessed False."""
    monkeypatch.setattr(_srv(), "_get_embedding_service", lambda: object())
    monkeypatch.setattr(_srv(), "_slot_short_source", lambda slot: "arctic")
    seen: dict = {}

    def _fake_ensure(uid, content, slot, model, coll, svc, *, embed_fn,
                     existing_props=None):
        """Mirrors the REAL `ensure_slot_embedding` signature.

        The previous fake was a positional lambda without `existing_props`.
        When the production call started passing that kwarg (W3: the store-back
        merges the leading-window verdict into the row's truncation record),
        the fake raised TypeError — which the attach site's broad
        `except Exception` swallowed into a debug line, so the vector silently
        stopped being attached and the failure surfaced three asserts later as
        a bare KeyError.

        Keyword-only `embed_fn` and the recorded kwargs are deliberate: a fake
        that accepts MORE than the real function hides the next signature
        change just as effectively as one that accepts less.
        """
        seen["existing_props"] = existing_props
        return [0.3, 0.4]

    monkeypatch.setattr(
        "claude_mcp_servers.rl_client.embed_regen.ensure_slot_embedding",
        _fake_ensure,
    )
    node = {"title": "n", "content": "the chunk body"}
    rep = _FakeRepObj({}, {"secondary_truncated_slots": []})
    _attach(node, rep, backfill_other=True, coll_for_backfill=object())
    assert node["emb_other"] == [0.3, 0.4], "the backfilled vector is attached"
    assert seen["existing_props"] == {"secondary_truncated_slots": []}, (
        "the store-back must receive the row's CURRENT properties, or the "
        "merge silently drops every other slot's recorded verdict"
    )
    assert "emb_other_truncated" not in node, (
        "a backfilled vector's state is NOT determinable here — absent means "
        "unknown downstream"
    )


def test_pipeline_carries_state_into_other_slot_log_nodes():
    """``_build_other_slot_log_nodes`` maps ``emb_other_truncated`` → the
    per-node ``emb_truncated`` the event carries — True, explicit False, and
    absent (skip-less) all preserved."""
    from claude_mcp_servers.rl_client.search_pipeline import _build_other_slot_log_nodes

    candidates = [
        {
            "title": "trunc",
            "score": 0.9,
            "emb_other": [0.1],
            "emb_other_truncated": True,
        },
        {
            "title": "full",
            "score": 0.8,
            "emb_other": [0.2],
            "emb_other_truncated": False,
        },
        {"title": "unknown", "score": 0.7, "emb_other": [0.3]},
        {"title": "skipped", "score": 0.6},  # no emb_other → skipped
    ]
    recs = _build_other_slot_log_nodes(candidates, limit=10)
    by_title = {r["title"]: r for r in recs}
    assert set(by_title) == {"trunc", "full", "unknown"}, (
        "candidates without an other-slot vector are skipped, not fabricated"
    )
    assert by_title["trunc"]["emb_truncated"] is True
    assert by_title["full"]["emb_truncated"] is False, (
        "an explicit False must survive the reduction"
    )
    assert "emb_truncated" not in by_title["unknown"]


def test_event_alone_identifies_truncated_secondary_end_to_end():
    """Acceptance 2: a truncated secondary vector is identifiable from the RL
    event ALONE (no Weaviate join): enrichment flag → pipeline log-node →
    serialized record → resolver."""
    from claude_mcp_servers.rl_client.search_pipeline import _build_other_slot_log_nodes

    node = {"title": "n"}
    rep = _FakeRepObj(
        {"arctic2_embed": [0.1, 0.2]},
        {"secondary_truncated_slots": ["arctic2_embed"]},
    )
    _attach(node, rep)
    recs = _build_other_slot_log_nodes([node], limit=10)
    serialized = [serialize_node_record(r) for r in recs]
    assert resolve_emb_truncation_state(serialized[0], SCHEMA_VERSION) == TRUNCATION_TRUE
    # And the same chain over a historical (state-less) node resolves unknown.
    hist = {"title": "old", "score": 0.5, "emb_other": [0.3]}
    hist_recs = _build_other_slot_log_nodes([hist], limit=10)
    hist_ser = [serialize_node_record(r) for r in hist_recs]
    assert resolve_emb_truncation_state(hist_ser[0], 3) == TRUNCATION_UNKNOWN


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
