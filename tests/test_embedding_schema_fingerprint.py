"""v0.2.60 Piece 3 — embedding-schema fingerprint + the re-embed gate.

The fingerprint (`embedding_schema_fingerprint`) and the live probe
(`live_fingerprint_stale`) gate re-embedding on an ACTUAL
embedding-invalidating change (slot removed / dim changed), NOT on a
version bump and NOT on a purely-additive optional-slot gap.
"""
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import weaviate_schema as ws
from vco_lib import project_init as pi


# ── Fingerprint ─────────────────────────────────────────────────────────

def test_fingerprint_is_deterministic_and_stable():
    a = ws.embedding_schema_fingerprint("VCODev_KnowledgeGraph")
    b = ws.embedding_schema_fingerprint("VCODev_KnowledgeGraph")
    assert a == b and len(a) == 24


def test_fingerprint_same_catalog_across_collection_names():
    # All KG-shaped collections share the KG catalog → same fingerprint.
    kg = ws.embedding_schema_fingerprint("Foo_KnowledgeGraph")
    shared = ws.embedding_schema_fingerprint("Bar_KnowledgeGraph")
    dev = ws.embedding_schema_fingerprint("Baz_Development")
    assert kg == shared == dev


def test_fingerprint_kg_differs_from_code():
    kg = ws.embedding_schema_fingerprint("X_KnowledgeGraph")
    code = ws.embedding_schema_fingerprint("X_CodeFunction")
    assert kg != code


def test_fingerprint_changes_when_a_slot_dim_changes():
    base = ws.embedding_schema_fingerprint("X_KnowledgeGraph")
    # Swap the catalog for one with a changed dim → fingerprint must change.
    bumped = [
        ws.NamedVectorSlot(s.name, s.dim + (1 if s.name == "qwen3_embed" else 0))
        for s in ws.KG_NAMED_VECTORS
    ]
    with mock.patch.object(ws, "KG_NAMED_VECTORS", bumped):
        changed = ws.embedding_schema_fingerprint("X_KnowledgeGraph")
    assert changed != base


def test_fingerprint_changes_when_a_slot_is_removed():
    base = ws.embedding_schema_fingerprint("X_KnowledgeGraph")
    fewer = [s for s in ws.KG_NAMED_VECTORS if s.name != "openai_embed"]
    with mock.patch.object(ws, "KG_NAMED_VECTORS", fewer):
        changed = ws.embedding_schema_fingerprint("X_KnowledgeGraph")
    assert changed != base


# ── live_fingerprint_stale ──────────────────────────────────────────────

def test_stale_probe_failure_soft_when_weaviate_down():
    # detect_kg_schema_drift returns (False, []) on transport failure; the
    # dim probe also soft-fails → overall not stale (no spurious migration).
    with mock.patch.object(pi, "detect_kg_schema_drift", return_value=(False, [])), \
         mock.patch.object(pi, "_fetch_schema", side_effect=Exception("down")):
        stale, fields = pi.live_fingerprint_stale("http://localhost:8081", "X_KnowledgeGraph")
    assert stale is False and fields == []


def test_stale_probe_reports_invariant_drift():
    # A missing core slot from the invariant detector → stale.
    with mock.patch.object(
        pi, "detect_kg_schema_drift",
        return_value=(True, ["named-vector slots (missing: qwen3_embed)"]),
    ), mock.patch.object(pi, "_fetch_schema", return_value=None):
        stale, fields = pi.live_fingerprint_stale("http://localhost:8081", "X_KnowledgeGraph")
    assert stale is True
    assert any("qwen3_embed" in f for f in fields)


def test_stale_probe_flags_dim_mismatch_the_invariant_detector_misses():
    # Invariant detector says clean (all core slots present, indexNullState ok),
    # but a slot's LIVE stored dim differs from the catalog → must be stale.
    live_schema = {"vectorConfig": {"qwen3_embed": {}}, "invertedIndexConfig": {"indexNullState": True}}
    with mock.patch.object(pi, "detect_kg_schema_drift", return_value=(False, [])), \
         mock.patch.object(pi, "_fetch_schema", return_value=live_schema), \
         mock.patch.object(pi, "_existing_vector_dim_for_slot", return_value=512):  # catalog qwen3=1024
        stale, fields = pi.live_fingerprint_stale("http://localhost:8081", "X_KnowledgeGraph")
    assert stale is True
    assert any("dim 512" in f and "1024" in f for f in fields)


def test_stale_probe_not_stale_when_dims_match():
    live_schema = {"vectorConfig": {"qwen3_embed": {}}, "invertedIndexConfig": {"indexNullState": True}}
    with mock.patch.object(pi, "detect_kg_schema_drift", return_value=(False, [])), \
         mock.patch.object(pi, "_fetch_schema", return_value=live_schema), \
         mock.patch.object(pi, "_existing_vector_dim_for_slot", return_value=1024):  # matches catalog
        stale, fields = pi.live_fingerprint_stale("http://localhost:8081", "X_KnowledgeGraph")
    assert stale is False and fields == []


def test_stale_probe_inconclusive_dim_probe_is_not_stale():
    # _existing_vector_dim_for_slot returns None (empty slot / unprobeable) →
    # never re-embed on an inconclusive probe.
    live_schema = {"vectorConfig": {"qwen3_embed": {}}, "invertedIndexConfig": {"indexNullState": True}}
    with mock.patch.object(pi, "detect_kg_schema_drift", return_value=(False, [])), \
         mock.patch.object(pi, "_fetch_schema", return_value=live_schema), \
         mock.patch.object(pi, "_existing_vector_dim_for_slot", return_value=None):
        stale, fields = pi.live_fingerprint_stale("http://localhost:8081", "X_KnowledgeGraph")
    assert stale is False and fields == []
