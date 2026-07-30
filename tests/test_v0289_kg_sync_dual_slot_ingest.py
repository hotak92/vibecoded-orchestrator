# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.89 §8.5 — dual-slot shipped-vector ingest in kg-sync.

v0.2.70 shipped the ingest plumbing serving the ACTIVE slot only. With
``DUAL_EMBEDDING_ENABLED=true`` (the default) a shipped-vector hit
therefore wrote a single slot while the COMPUTE path would have fanned out
to every reachable backend. The §8.5 extension: when the active slot has a
shipped hit, ALSO merge every OTHER configured slot's sidecar hit for the
same signature/chunk into the ``{slot: vec}`` map.

Invariants pinned here:

* active hit + secondary hit → BOTH slots written, ZERO embed calls;
* active hit + secondary miss (absent / stale hash / chunk-count mismatch)
  → active slot only, ZERO embed calls — a missing secondary is NEVER
  computed for (the v0.2.70 "never synthesise" rule: secondary slots stay
  unpopulated exactly as a partial-backend compute run leaves them);
* active MISS → full fall-back to compute; the secondary's shipped data is
  NOT merged into the computed result (the merge triggers only on an
  active-slot hit);
* ``DUAL_EMBEDDING_ENABLED=false`` (legacy) → active-slot-only shape,
  exactly pre-.89 (leave-alone);
* a sidecar naming a slot OUTSIDE the canonical ``KG_NAMED_VECTORS``
  catalog is never merged (an unknown slot could fail the whole insert).

Pure unit tests — fake Weaviate client + counting embed server (same
harness family as test_v0270_shipped_embedding_ingest; kept self-contained
per that module's own convention), no network.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"

ACTIVE_SLOT = "qwen3_embed"
SECONDARY_SLOT = "arctic2_embed"
ACTIVE_VEC = [0.11, 0.22, 0.33]
SECONDARY_VEC = [0.44, 0.55, 0.66]
COMPUTED_VEC = [0.9, 0.9, 0.9]


# ─── Fake Weaviate client + counting embed server ────────────────────────


class _FakeProp:
    def __init__(self, name: str):
        self.name = name

    def equal(self, value):
        return _FakeFilter([(self.name, value)])


class _FakeFilter:
    def __init__(self, matchers=None):
        self.matchers = matchers or []

    @staticmethod
    def by_property(name: str) -> "_FakeProp":
        return _FakeProp(name)

    def __and__(self, other: "_FakeFilter") -> "_FakeFilter":
        return _FakeFilter(self.matchers + other.matchers)

    def matches(self, props: dict) -> bool:
        return all(props.get(name) == value for name, value in self.matchers)


class _FakeObj:
    def __init__(self, uid, props, vector=None):
        self.uuid = uid
        self.properties = props
        self.vector = vector or {}


class _FakeQueryResult:
    def __init__(self, objects):
        self.objects = objects


class _FakeQuery:
    def __init__(self, store: dict):
        self._store = store

    def fetch_objects(self, filters=None, limit=100, return_properties=None,
                      include_vector=False):
        objs = list(self._store.values())
        if filters is not None:
            objs = [o for o in objs if filters.matches(o.properties)]
        return _FakeQueryResult(objs[:limit])


class _FakeData:
    def __init__(self, store: dict):
        self._store = store

    def insert(self, properties=None, vector=None):
        uid = str(uuid.uuid4())
        self._store[uid] = _FakeObj(uid, dict(properties or {}), vector)
        return uid

    def delete_by_id(self, uid):
        self._store.pop(str(uid), None)

    def reference_add(self, **kwargs):  # noqa: ARG002
        pass


class _FakeCollection:
    def __init__(self, store: dict):
        self._store = store
        self.query = _FakeQuery(store)
        self.data = _FakeData(store)


class _FakeCollections:
    def __init__(self):
        self._stores: dict[str, dict] = {}

    def _store_for(self, name: str) -> dict:
        return self._stores.setdefault(name, {})

    def get(self, name: str) -> _FakeCollection:
        return _FakeCollection(self._store_for(name))

    def exists(self, name: str) -> bool:  # noqa: ARG002
        return True

    def create(self, **kwargs):  # noqa: ARG002
        pass


class _FakeClient:
    def __init__(self):
        self.collections = _FakeCollections()


class _FakeEmbeddingService:
    text_model_id = "qwen3-embedding:0.6b"


class _CountingServer:
    def __init__(self, slot: str = ACTIVE_SLOT):
        self.client = _FakeClient()
        self.embedding_service = _FakeEmbeddingService()
        self.text_vector_slot = slot
        self.embed_calls = 0

    def _get_embedding(self, text):  # noqa: ARG002
        self.embed_calls += 1
        return list(COMPUTED_VEC)

    def _get_all_kg_embeddings(self, text):  # noqa: ARG002
        self.embed_calls += 1
        return {self.text_vector_slot: list(COMPUTED_VEC)}


_ENV_KEYS = (
    "KG_BASE_DIR", "KG_COLLECTION", "SHARED_KG_COLLECTION",
    "DEVELOPMENT_COLLECTION", "DUAL_EMBEDDING_ENABLED",
    "VCT_DISABLE_HUB_RESOLVER", "KG_SYNC_PROJECT_ROOT",
)


def _load_sync_module(project_root: Path, *, dual: bool = True):
    os.environ["KG_BASE_DIR"] = str(project_root)
    os.environ["KG_COLLECTION"] = "V0289DualTest_KnowledgeGraph"
    os.environ["SHARED_KG_COLLECTION"] = ""
    os.environ["DEVELOPMENT_COLLECTION"] = ""
    os.environ["DUAL_EMBEDDING_ENABLED"] = "true" if dual else "false"
    os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
    os.environ.pop("KG_SYNC_PROJECT_ROOT", None)

    mod_name = f"_sync_kg_dual_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:
        raise unittest.SkipTest(
            f"sync_knowledge_graph.py has runtime deps not installed ({exc})"
        )
    mod.Filter = _FakeFilter
    return mod


def _write_node(path: Path, title: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\ntype: concept\nstatus: active\n---\n{body}\n",
        encoding="utf-8",
    )


def _write_sidecar(knowledge_root: Path, slot: str, content_hash: str,
                   vector: list[float], *, total_chunks: int = 1) -> None:
    sidecar = {
        "schema_version": 1,
        "slot": slot,
        "model_id": "fixture-model",
        "dim": len(vector),
        "nodes": {
            content_hash: {
                "total_chunks": total_chunks,
                "chunks": [
                    {"chunk_num": n + 1, "vector": vector}
                    for n in range(total_chunks)
                ],
            }
        },
    }
    (knowledge_root / f".node_embeddings.{slot}.json").write_text(
        json.dumps(sidecar, indent=2), encoding="utf-8"
    )


def _vco_lib_catalog_importable() -> bool:
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from vco_lib.weaviate_schema import KG_NAMED_VECTORS  # noqa: F401
        return True
    except Exception:
        return False


class DualSlotIngestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.knowledge = self.root / "knowledge"
        self.knowledge.mkdir(parents=True, exist_ok=True)
        self.node = self.knowledge / "concepts" / "dual-slot.md"
        _write_node(self.node, "Dual Slot", "A small single-chunk node body.")

    def tearDown(self):
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def _node_hash(self, mod) -> str:
        return mod._content_signature_excluding_updated(
            self.node.read_text(encoding="utf-8")
        )

    def _sync(self, mod, server):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = mod.sync_node(server, self.node)
        return ok, buf.getvalue()

    def _stored_vector(self, mod, server):
        store = server.client.collections._store_for(mod.COLLECTION_NAME)
        self.assertEqual(len(store), 1, "expected exactly one stored object")
        return next(iter(store.values())).vector

    # ── act: dual-slot merge ─────────────────────────────────────────────

    def test_active_and_secondary_hits_merge_both_slots(self):
        mod = _load_sync_module(self.root, dual=True)
        server = _CountingServer(slot=ACTIVE_SLOT)
        h = self._node_hash(mod)
        _write_sidecar(self.knowledge, ACTIVE_SLOT, h, ACTIVE_VEC)
        _write_sidecar(self.knowledge, SECONDARY_SLOT, h, SECONDARY_VEC)

        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertEqual(server.embed_calls, 0,
                         "shipped hits in both slots ⇒ ZERO embed calls")
        vec = self._stored_vector(mod, server)
        self.assertIsInstance(vec, dict)
        self.assertEqual(vec.get(ACTIVE_SLOT), ACTIVE_VEC)
        self.assertEqual(
            vec.get(SECONDARY_SLOT), SECONDARY_VEC,
            "the secondary slot's shipped vector must be MERGED on an "
            "active-slot hit (§8.5)",
        )

    # ── leave-alone: never synthesise a missing secondary ────────────────

    def test_active_hit_secondary_absent_stays_single_slot_no_compute(self):
        mod = _load_sync_module(self.root, dual=True)
        server = _CountingServer(slot=ACTIVE_SLOT)
        h = self._node_hash(mod)
        _write_sidecar(self.knowledge, ACTIVE_SLOT, h, ACTIVE_VEC)
        # No secondary sidecar at all.

        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertEqual(server.embed_calls, 0,
                         "a missing secondary must NEVER be computed for")
        vec = self._stored_vector(mod, server)
        self.assertEqual(vec.get(ACTIVE_SLOT), ACTIVE_VEC)
        self.assertNotIn(SECONDARY_SLOT, vec)

    def test_secondary_stale_hash_not_merged_not_computed(self):
        mod = _load_sync_module(self.root, dual=True)
        server = _CountingServer(slot=ACTIVE_SLOT)
        h = self._node_hash(mod)
        _write_sidecar(self.knowledge, ACTIVE_SLOT, h, ACTIVE_VEC)
        _write_sidecar(self.knowledge, SECONDARY_SLOT, "deadbeef" * 8,
                       SECONDARY_VEC)  # stale hash

        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertEqual(server.embed_calls, 0)
        vec = self._stored_vector(mod, server)
        self.assertEqual(vec.get(ACTIVE_SLOT), ACTIVE_VEC)
        self.assertNotIn(SECONDARY_SLOT, vec,
                         "a STALE secondary vector must not be merged "
                         "(staleness guard applies per slot)")

    def test_secondary_chunk_count_mismatch_not_merged(self):
        mod = _load_sync_module(self.root, dual=True)
        server = _CountingServer(slot=ACTIVE_SLOT)
        h = self._node_hash(mod)
        _write_sidecar(self.knowledge, ACTIVE_SLOT, h, ACTIVE_VEC)
        _write_sidecar(self.knowledge, SECONDARY_SLOT, h, SECONDARY_VEC,
                       total_chunks=2)  # covers 2 chunks; node embeds as 1

        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertEqual(server.embed_calls, 0)
        vec = self._stored_vector(mod, server)
        self.assertNotIn(SECONDARY_SLOT, vec,
                         "chunk-count mismatch must exclude the secondary")

    # ── leave-alone: merge only triggers on an ACTIVE hit ────────────────

    def test_active_miss_computes_and_ignores_secondary(self):
        mod = _load_sync_module(self.root, dual=True)
        server = _CountingServer(slot=ACTIVE_SLOT)
        h = self._node_hash(mod)
        # NO active-slot sidecar; secondary present + matching.
        _write_sidecar(self.knowledge, SECONDARY_SLOT, h, SECONDARY_VEC)

        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertEqual(server.embed_calls, 1,
                         "active miss ⇒ full fall-back to compute")
        vec = self._stored_vector(mod, server)
        self.assertEqual(vec.get(ACTIVE_SLOT), COMPUTED_VEC)
        self.assertNotIn(
            SECONDARY_SLOT, vec,
            "the shipped secondary must NOT be merged into a COMPUTED "
            "result — the §8.5 merge triggers only on an active-slot hit",
        )

    # ── leave-alone: legacy single-slot mode unchanged ───────────────────

    def test_dual_disabled_stays_active_slot_only(self):
        mod = _load_sync_module(self.root, dual=False)
        server = _CountingServer(slot=ACTIVE_SLOT)
        h = self._node_hash(mod)
        _write_sidecar(self.knowledge, ACTIVE_SLOT, h, ACTIVE_VEC)
        _write_sidecar(self.knowledge, SECONDARY_SLOT, h, SECONDARY_VEC)

        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertEqual(server.embed_calls, 0)
        vec = self._stored_vector(mod, server)
        # Legacy shape: flat active-slot vector (pre-.89 behaviour).
        if isinstance(vec, dict):
            self.assertEqual(vec.get(ACTIVE_SLOT), ACTIVE_VEC)
            self.assertNotIn(SECONDARY_SLOT, vec)
        else:
            self.assertEqual(vec, ACTIVE_VEC)

    # ── guard: unknown slots never merged ────────────────────────────────

    @unittest.skipUnless(_vco_lib_catalog_importable(),
                         "vco_lib KG_NAMED_VECTORS catalog not importable")
    def test_unknown_slot_sidecar_not_merged(self):
        """A sidecar naming a slot the canonical catalog doesn't know could
        fail the WHOLE insert (collection schema lacks the named vector) —
        it must be ignored, not merged."""
        mod = _load_sync_module(self.root, dual=True)
        server = _CountingServer(slot=ACTIVE_SLOT)
        h = self._node_hash(mod)
        _write_sidecar(self.knowledge, ACTIVE_SLOT, h, ACTIVE_VEC)
        _write_sidecar(self.knowledge, "bogus_embed", h, SECONDARY_VEC)

        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        vec = self._stored_vector(mod, server)
        self.assertNotIn("bogus_embed", vec)
        self.assertEqual(vec.get(ACTIVE_SLOT), ACTIVE_VEC)


if __name__ == "__main__":
    unittest.main()
