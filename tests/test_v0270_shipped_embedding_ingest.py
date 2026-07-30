# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.70 Part 2 ingest guards + v0.2.89 shipped DATA.

v0.2.70 shipped the pre-shipped-embedding INGEST plumbing as a documented
NO-OP (no data file). v0.2.89 ships the DATA: per-slot sidecars for the
curated KG nodes (``templates/knowledge/.node_embeddings.qwen3_embed.json``
+ ``.node_embeddings.arctic2_embed.json``, built by
``scripts/build_shipped_kg_embeddings.py``), so a 3rd-party install INGESTS
them instead of recomputing every node's vector on first sync (the
arctic-on-CPU install-hang class).

The guard behaviours locked in v0.2.70 MUST survive the data landing —
``ShippedEmbeddingIngestTest`` below still pins them with a small in-memory
fake Weaviate client + fixture sidecars in a temp ``knowledge/``:

  (a) shipped vector present + content_hash match + slot match → INGESTED
      (no embedding computed — the embed call is asserted NOT to have run);
  (b) content_hash mismatch (node edited since the vector was computed) →
      fall back to COMPUTE (staleness guard);
  (c) slot mismatch (qwen3 vector shipped, arctic install active) → fall back
      to COMPUTE (the never-cross-model invariant);
  (d) absent sidecar → COMPUTE (guard-miss fallback, unchanged).

The v0.2.70 ``ShippedEmbeddingNoOpThisReleaseTest`` ("ships NO embedding
data") is superseded by ``ShippedEmbeddingDataShipsTest`` (the sidecars
exist + parse + declare the expected slots) and
``ShippedDataZeroEmbedSeedTest`` (the §8.4 ordering caveat: a seed of the
REAL curated nodes with the REAL shipped sidecars performs ZERO embed
calls). Schema/coverage/hash-parity invariants for the shipped data live in
``tests/test_v0289_shipped_kg_embeddings.py``.

Pure unit tests — no network / Ollama.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
TEMPLATES_KNOWLEDGE = REPO_ROOT / "templates" / "knowledge"

#: v0.2.89: the canonical shipped slots (see scripts/build_shipped_kg_embeddings.py).
SHIPPED_SLOTS = ("qwen3_embed", "arctic2_embed")

#: Env keys every test in this module may set — popped in tearDown.
_ENV_KEYS = (
    "KG_BASE_DIR", "KG_COLLECTION", "SHARED_KG_COLLECTION",
    "DEVELOPMENT_COLLECTION", "DUAL_EMBEDDING_ENABLED",
    "VCT_DISABLE_HUB_RESOLVER", "KG_SYNC_PROJECT_ROOT",
)


# ─── In-memory fake Weaviate client (mirrors the batch-collision test) ────


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

    def fetch_objects(self, filters=None, limit=100, return_properties=None, include_vector=False):
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
    """Fake server whose embed calls are COUNTED.

    ``embed_calls`` increments every time the sync path computes an embedding
    (``_get_embedding`` / ``_get_all_kg_embeddings``). A successful shipped-
    vector ingest must leave it at 0; a fall-back-to-compute must increment it.
    """

    def __init__(self, slot: str = "qwen3_embed"):
        self.client = _FakeClient()
        self.embedding_service = _FakeEmbeddingService()
        self.text_vector_slot = slot
        self.embed_calls = 0

    def _get_embedding(self, text):  # noqa: ARG002
        self.embed_calls += 1
        return [0.9, 0.9, 0.9]  # a DISTINCT vector from any shipped fixture

    def _get_all_kg_embeddings(self, text):  # noqa: ARG002
        self.embed_calls += 1
        return {self.text_vector_slot: [0.9, 0.9, 0.9]}


def _load_sync_module(project_root: Path, *, dual: bool = False):
    os.environ["KG_BASE_DIR"] = str(project_root)
    os.environ["KG_COLLECTION"] = "TestProject_KnowledgeGraph"
    os.environ["DEVELOPMENT_COLLECTION"] = "TestProject_Development"
    os.environ["SHARED_KG_COLLECTION"] = ""
    os.environ["DUAL_EMBEDDING_ENABLED"] = "true" if dual else "false"
    os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
    # Defensive: the v0.2.89 BUG-3 root channel must not leak in from the
    # ambient shell (it would outrank KG_BASE_DIR above).
    os.environ.pop("KG_SYNC_PROJECT_ROOT", None)

    mod_name = f"_sync_kg_ingest_{uuid.uuid4().hex}"
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


def _node_content_hash(mod, path: Path) -> str:
    """Compute the content signature the sync path stores for this node.

    Mirrors sync_node: ``_content_signature_excluding_updated`` over the
    file's CURRENT text (after the timestamp-update side effect — for a node
    with no ``updated:`` line the signature is stable, so reading raw is
    equivalent here).
    """
    return mod._content_signature_excluding_updated(path.read_text(encoding="utf-8"))


def _write_embeddings_sidecar(
    knowledge_root: Path,
    slot: str,
    content_hash: str,
    vector: list[float],
    *,
    total_chunks: int = 1,
) -> None:
    sidecar = {
        "schema_version": 1,
        "slot": slot,
        "model_id": "qwen3-embedding:0.6b",
        "dim": len(vector),
        "nodes": {
            content_hash: {
                "total_chunks": total_chunks,
                "chunks": [{"chunk_num": 1, "vector": vector}],
            }
        },
    }
    (knowledge_root / f".node_embeddings.{slot}.json").write_text(
        json.dumps(sidecar, indent=2), encoding="utf-8"
    )


class ShippedEmbeddingIngestTest(unittest.TestCase):
    SHIPPED_VEC = [0.11, 0.22, 0.33]  # distinct from the computed [0.9,0.9,0.9]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.knowledge = self.root / "knowledge"
        self.knowledge.mkdir(parents=True, exist_ok=True)
        self.node = self.knowledge / "concepts" / "ingest-me.md"
        _write_node(self.node, "Ingest Me", "A small single-chunk node body.")

    def tearDown(self):
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def _stored_vector(self, mod, server):
        store = server.client.collections.get(mod.COLLECTION_NAME)._store
        self.assertEqual(len(store), 1, "expected exactly one stored object")
        obj = next(iter(store.values()))
        # DUAL_EMBEDDING_ENABLED=false → vector is a flat dict {slot: vec} via
        # _build_vector_arg, OR {slot: vec} from the shipped path. Normalize.
        vec = obj.vector
        if isinstance(vec, dict):
            return vec.get(server.text_vector_slot)
        return vec

    # (a) present + hash-match + slot-match → INGESTED, no embed call.
    def test_shipped_vector_ingested_when_hash_and_slot_match(self):
        mod = _load_sync_module(self.root)
        server = _CountingServer(slot="qwen3_embed")
        c_hash = _node_content_hash(mod, self.node)
        _write_embeddings_sidecar(self.knowledge, "qwen3_embed", c_hash, self.SHIPPED_VEC)

        self.assertTrue(mod.sync_node(server, self.node))
        self.assertEqual(
            server.embed_calls, 0,
            "embedding was computed even though a valid shipped vector existed",
        )
        self.assertEqual(
            self._stored_vector(mod, server), self.SHIPPED_VEC,
            "the SHIPPED vector must be the one inserted",
        )

    # (b) hash mismatch → fall back to COMPUTE (staleness guard).
    def test_hash_mismatch_falls_back_to_compute(self):
        mod = _load_sync_module(self.root)
        server = _CountingServer(slot="qwen3_embed")
        # Sidecar keyed by a DIFFERENT (stale) hash than the node's current one.
        _write_embeddings_sidecar(
            self.knowledge, "qwen3_embed", "deadbeefdeadbeef", self.SHIPPED_VEC
        )

        self.assertTrue(mod.sync_node(server, self.node))
        self.assertEqual(
            server.embed_calls, 1,
            "stale shipped vector must NOT be ingested — embedding must compute",
        )
        self.assertNotEqual(
            self._stored_vector(mod, server), self.SHIPPED_VEC,
            "a stale shipped vector leaked into storage",
        )

    # (c) slot mismatch → fall back to COMPUTE (never cross-model).
    def test_slot_mismatch_falls_back_to_compute(self):
        mod = _load_sync_module(self.root)
        # Install is ACTIVE on arctic; sidecar holds a qwen3 vector.
        server = _CountingServer(slot="arctic2_embed")
        c_hash = _node_content_hash(mod, self.node)
        _write_embeddings_sidecar(self.knowledge, "qwen3_embed", c_hash, self.SHIPPED_VEC)

        self.assertTrue(mod.sync_node(server, self.node))
        self.assertEqual(
            server.embed_calls, 1,
            "a qwen3 vector must NEVER be ingested into an arctic install",
        )
        self.assertNotEqual(
            self._stored_vector(mod, server), self.SHIPPED_VEC,
            "cross-model vector leaked into storage",
        )

    # (d) absent sidecar → COMPUTE (v0.2.70 default, behaviour unchanged).
    def test_absent_sidecar_computes(self):
        mod = _load_sync_module(self.root)
        server = _CountingServer(slot="qwen3_embed")
        # No .node_embeddings.*.json written.

        self.assertTrue(mod.sync_node(server, self.node))
        self.assertEqual(
            server.embed_calls, 1,
            "with no shipped sidecar the embed path must compute as before",
        )

    # Defensive: a slot-named file that DECLARES a different slot is rejected.
    def test_misnamed_slot_in_file_body_rejected(self):
        mod = _load_sync_module(self.root)
        server = _CountingServer(slot="qwen3_embed")
        c_hash = _node_content_hash(mod, self.node)
        # File name says qwen3_embed but the JSON body declares arctic2_embed.
        sidecar = {
            "schema_version": 1,
            "slot": "arctic2_embed",  # mismatch with filename + active slot
            "nodes": {c_hash: {"total_chunks": 1,
                               "chunks": [{"chunk_num": 1, "vector": self.SHIPPED_VEC}]}},
        }
        (self.knowledge / ".node_embeddings.qwen3_embed.json").write_text(
            json.dumps(sidecar), encoding="utf-8"
        )

        self.assertTrue(mod.sync_node(server, self.node))
        self.assertEqual(
            server.embed_calls, 1,
            "a file whose body slot != requested slot must be treated as absent",
        )

    # Defensive: a malformed/empty vector must not be ingested.
    def test_malformed_vector_falls_back_to_compute(self):
        mod = _load_sync_module(self.root)
        server = _CountingServer(slot="qwen3_embed")
        c_hash = _node_content_hash(mod, self.node)
        _write_embeddings_sidecar(self.knowledge, "qwen3_embed", c_hash, [])  # empty

        self.assertTrue(mod.sync_node(server, self.node))
        self.assertEqual(server.embed_calls, 1)


class ShippedEmbeddingDataShipsTest(unittest.TestCase):
    """v0.2.89 evolution of the v0.2.70 no-op-this-release test.

    v0.2.70 pinned "the shipped tree contains NO .node_embeddings.*.json"
    while only the plumbing existed. The data now ships: the sidecars must
    EXIST, PARSE, and declare exactly the canonical slot set (inventory
    control — an extra slot file would be dead weight the ingest can never
    consume for a schema slot it doesn't know). Guard behaviours are pinned
    by ``ShippedEmbeddingIngestTest`` above; data schema/coverage/parity by
    ``tests/test_v0289_shipped_kg_embeddings.py``.
    """

    def test_sidecars_ship_and_parse_for_canonical_slots(self):
        for slot in SHIPPED_SLOTS:
            with self.subTest(slot=slot):
                path = TEMPLATES_KNOWLEDGE / f".node_embeddings.{slot}.json"
                self.assertTrue(
                    path.is_file(),
                    f"{path.name} missing — v0.2.89 ships the embedding data; "
                    f"regenerate with `python scripts/build_shipped_kg_embeddings.py`",
                )
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data.get("schema_version"), 1)
                self.assertEqual(data.get("slot"), slot)
                self.assertIsInstance(data.get("nodes"), dict)
                self.assertGreater(len(data["nodes"]), 0)

    def test_shipped_slot_files_are_exactly_the_canonical_set(self):
        shipped = sorted(
            p.name for p in TEMPLATES_KNOWLEDGE.glob(".node_embeddings.*.json")
        )
        expected = sorted(f".node_embeddings.{s}.json" for s in SHIPPED_SLOTS)
        self.assertEqual(
            shipped, expected,
            "the shipped sidecar inventory drifted from the canonical slot "
            "set (scripts/build_shipped_kg_embeddings.py DEFAULT_SLOT_MODELS)",
        )


class ShippedDataZeroEmbedSeedTest(unittest.TestCase):
    """§8.4 ordering caveat: seeding the REAL curated nodes with the REAL
    shipped sidecars present performs ZERO embed calls.

    Extends this module's existing fake-Weaviate + counting-server fixtures
    (per the do-not-duplicate rule): every current template node is
    materialized into a temp project root together with BOTH shipped
    sidecars, then synced through the real ``sync_node`` against a
    qwen3-active counting fake. Every node must ingest its shipped vector
    (embed_calls stays 0), and the §8.5 dual-slot merge must fire with the
    REAL arctic data for at least one node.
    """

    EXCLUDED = frozenset({"TAG_HIERARCHY.md", "VOCABULARY.md"})

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.knowledge = self.root / "knowledge"
        self.nodes: list[Path] = []
        for src in sorted(TEMPLATES_KNOWLEDGE.rglob("*.md")):
            if src.name in self.EXCLUDED:
                continue
            dst = self.knowledge / src.relative_to(TEMPLATES_KNOWLEDGE)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())  # verbatim, like Step 4d
            self.nodes.append(dst)
        for slot in SHIPPED_SLOTS:
            sidecar = TEMPLATES_KNOWLEDGE / f".node_embeddings.{slot}.json"
            if not sidecar.is_file():
                raise unittest.SkipTest(f"{sidecar.name} not shipped")
            (self.knowledge / sidecar.name).write_bytes(sidecar.read_bytes())

    def tearDown(self):
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_seed_with_shipped_sidecars_performs_zero_embed_calls(self):
        import contextlib
        import io

        mod = _load_sync_module(self.root, dual=True)
        server = _CountingServer(slot="qwen3_embed")

        # The INGESTABLE subset, per the sync script's OWN archived predicate
        # (archived/deprecated/superseded nodes are skipped by sync_node and
        # therefore ship no vectors — the generator mirrors the same rule).
        expected_paths = set()
        for node in self.nodes:
            frontmatter, _body = mod.parse_frontmatter(
                node.read_text(encoding="utf-8")
            )
            if mod._is_archived_node(node, frontmatter=frontmatter or {})[0]:
                continue
            rel = node.relative_to(self.root / "knowledge").as_posix()
            expected_paths.add(f"knowledge/{rel}")
        self.assertGreater(len(expected_paths), 0)
        self.assertLess(
            len(expected_paths), len(self.nodes),
            "fixture sanity: the curated set carries at least one "
            "archived-class node (status deprecated/superseded)",
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for node in self.nodes:
                ok = mod.sync_node(server, node)
                self.assertTrue(ok, f"sync_node failed for {node.name}:\n{buf.getvalue()[-2000:]}")

        self.assertEqual(
            server.embed_calls, 0,
            "a curated-node seed with the shipped sidecars present must "
            "perform ZERO embed calls — some node fell back to compute "
            "(stale sidecar? regenerate with "
            "`python scripts/build_shipped_kg_embeddings.py`)",
        )

        store = server.client.collections.get(mod.COLLECTION_NAME)._store
        stored_paths = {o.properties.get("file_path") for o in store.values()}
        self.assertEqual(
            stored_paths, expected_paths,
            "stored objects must be exactly the ingestable (non-archived) "
            "curated set, one single-chunk object each (qwen3-active)",
        )
        merged_secondary = 0
        for obj in store.values():
            vec = obj.vector
            self.assertIsInstance(vec, dict)
            self.assertIn(
                "qwen3_embed", vec,
                "every object must carry the ACTIVE slot's shipped vector",
            )
            if "arctic2_embed" in vec:
                merged_secondary += 1
        self.assertGreater(
            merged_secondary, 0,
            "the §8.5 dual-slot merge never fired with the REAL shipped "
            "arctic data — secondary sidecar unusable?",
        )


if __name__ == "__main__":
    unittest.main()
