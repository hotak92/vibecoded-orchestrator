# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.89 BUG 6 — `scope: shared` frontmatter routing in kg-sync.

Pre-fix, ``sync_node`` always wrote ``COLLECTION_NAME`` — the ``scope:``
frontmatter key was silently ignored, so a node the author declared shared
landed only in the per-project collection (Windows field audit).

The fix mirrors ``store_knowledge_node``'s semantics (server.py):

* ``targets_shared = scope=="shared" AND SHARED_COLLECTION_NAME nonempty
  AND SHARED_COLLECTION_NAME != COLLECTION_NAME`` — the identity case
  (orchestrator root, shared == kg) routes to the project collection with
  no special-casing and NO migration delete.
* Write gate keyed on the REQUESTED scope (v0.2.44 fix-now-6):
  ``scope=="shared"`` + ``SHARED_KG_WRITE_DISABLED`` → the node FAILS with
  an explicit error — NO silent reroute (the load-bearing assertion).
  Legacy ``SHARED_KG_OPT_OUT`` alias honoured; canonical key wins even
  when set to a falsy spelling.
* project → shared transition: after a successful shared write, the
  same-``file_path`` rows are deleted from the PROJECT collection only.
* shared → project transition (key removed): shared rows are NOT
  auto-deleted (collision class — ``file_path`` is not project-qualified
  in the shared store); a one-line notice fires instead.
* The embed-skip fast path queries the TARGET collection (a hash match in
  the WRONG store must never cause a false skip).

Pure unit tests — in-memory fake Weaviate client (the established pattern
from test_v0270_shipped_embedding_ingest), no network.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"

PROJECT_KG = "V0289Proj_KnowledgeGraph"
SHARED_KG = "V0289Shared_KnowledgeGraph"


# ─── In-memory fake Weaviate client (per-collection stores) ──────────────


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
    """Fake server whose embed calls are COUNTED (fast-path assertions)."""

    def __init__(self, slot: str = "qwen3_embed"):
        self.client = _FakeClient()
        self.embedding_service = _FakeEmbeddingService()
        self.text_vector_slot = slot
        self.embed_calls = 0

    def _get_embedding(self, text):  # noqa: ARG002
        self.embed_calls += 1
        return [0.9, 0.9, 0.9]

    def _get_all_kg_embeddings(self, text):  # noqa: ARG002
        self.embed_calls += 1
        return {self.text_vector_slot: [0.9, 0.9, 0.9]}


_ENV_KEYS = (
    "KG_BASE_DIR", "KG_COLLECTION", "SHARED_KG_COLLECTION",
    "DEVELOPMENT_COLLECTION", "DUAL_EMBEDDING_ENABLED",
    "VCT_DISABLE_HUB_RESOLVER", "SHARED_KG_WRITE_DISABLED",
    "SHARED_KG_OPT_OUT", "VCT_PROJECT_ID", "KG_SYNC_PROJECT_ROOT",
)


def _load_sync_module(project_root: Path, *, shared: str = SHARED_KG):
    os.environ["KG_BASE_DIR"] = str(project_root)
    os.environ["KG_COLLECTION"] = PROJECT_KG
    os.environ["SHARED_KG_COLLECTION"] = shared
    os.environ["DEVELOPMENT_COLLECTION"] = ""
    os.environ["DUAL_EMBEDDING_ENABLED"] = "false"
    os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
    os.environ.pop("KG_SYNC_PROJECT_ROOT", None)
    os.environ.pop("SHARED_KG_WRITE_DISABLED", None)
    os.environ.pop("SHARED_KG_OPT_OUT", None)
    os.environ.pop("VCT_PROJECT_ID", None)

    mod_name = f"_sync_kg_scope_{uuid.uuid4().hex}"
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


def _write_node(path: Path, title: str, body: str, scope: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scope_line = f"scope: {scope}\n" if scope is not None else ""
    path.write_text(
        f"---\ntitle: {title}\ntype: concept\nstatus: active\n{scope_line}---\n{body}\n",
        encoding="utf-8",
    )


def _seed_row(server: _CountingServer, collection: str, file_path: str,
              content_hash: str = "seedhash", *, chunk_num: int = 1,
              total_chunks: int = 1) -> str:
    """Pre-seed one row into a named fake collection; returns its uuid."""
    store = server.client.collections._store_for(collection)
    uid = str(uuid.uuid4())
    store[uid] = _FakeObj(uid, {
        "file_path": file_path,
        "content_hash": content_hash,
        "chunk_num": chunk_num,
        "total_chunks": total_chunks,
    })
    return uid


class ScopeRoutingTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.knowledge = self.root / "knowledge"
        self.knowledge.mkdir(parents=True, exist_ok=True)
        self.node = self.knowledge / "concepts" / "scoped.md"
        self.fp = "knowledge/concepts/scoped.md"

    def tearDown(self):
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def _sync(self, mod, server):
        """Run sync_node with stdout captured; returns (ok, output)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = mod.sync_node(server, self.node)
        return ok, buf.getvalue()

    def _store(self, server, collection: str) -> dict:
        return server.client.collections._store_for(collection)


class RoutingMatrixTests(ScopeRoutingTestBase):
    """Plan §3.4 routing matrix: no key / project / shared / invalid."""

    def test_no_scope_key_routes_to_project(self):
        _write_node(self.node, "N", "body", scope=None)
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertEqual(len(self._store(server, PROJECT_KG)), 1)
        self.assertEqual(len(self._store(server, SHARED_KG)), 0,
                         "absent scope key must mean project — zero change "
                         "for every existing node")

    def test_scope_project_routes_to_project(self):
        _write_node(self.node, "N", "body", scope="project")
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertEqual(len(self._store(server, PROJECT_KG)), 1)
        self.assertEqual(len(self._store(server, SHARED_KG)), 0)

    def test_scope_shared_routes_to_shared(self):
        _write_node(self.node, "N", "body", scope="shared")
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertEqual(len(self._store(server, SHARED_KG)), 1,
                         "scope: shared must write the SHARED collection")
        self.assertEqual(len(self._store(server, PROJECT_KG)), 0)
        self.assertIn(SHARED_KG, out)  # routing line names the target
        self.assertEqual(mod._SHARED_ROUTED_COUNT, 1,
                         "summary counter must track shared-routed nodes")

    def test_invalid_scope_warns_and_routes_to_project(self):
        _write_node(self.node, "N", "body", scope="global")
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertIn("Invalid frontmatter scope", out)
        self.assertEqual(len(self._store(server, PROJECT_KG)), 1)
        self.assertEqual(len(self._store(server, SHARED_KG)), 0)

    def test_identity_case_routes_to_project_no_migration(self):
        """Orchestrator root: shared == kg → project routing, no migration
        delete, no special-casing (leave-alone)."""
        _write_node(self.node, "N", "body", scope="shared")
        mod = _load_sync_module(self.root, shared=PROJECT_KG)  # identity
        server = _CountingServer()
        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertEqual(len(self._store(server, PROJECT_KG)), 1)
        self.assertNotIn("moved out of project collection", out,
                         "identity case must never fire the migration delete")
        self.assertEqual(mod._SHARED_ROUTED_COUNT, 0)


class WriteGateTests(ScopeRoutingTestBase):
    """The SHARED_KG_WRITE_DISABLED gate — refuse loudly, NO silent reroute."""

    def test_gate_refuses_and_does_not_reroute(self):
        _write_node(self.node, "N", "body", scope="shared")
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        os.environ["SHARED_KG_WRITE_DISABLED"] = "true"  # call-time read
        ok, out = self._sync(mod, server)
        self.assertFalse(ok, "a gated shared-scoped node must COUNT AS FAILED")
        self.assertIn("SHARED_KG_WRITE_DISABLED", out)
        self.assertIn("scope: shared", out)  # remediation names the key
        self.assertEqual(len(self._store(server, SHARED_KG)), 0)
        self.assertEqual(
            len(self._store(server, PROJECT_KG)), 0,
            "NO silent reroute: the project collection must NOT be written "
            "when the node's declared destination was refused",
        )

    def test_gate_fires_in_identity_case_too(self):
        """v0.2.44 fix-now-6 parity: the gate keys on the REQUESTED scope,
        so it fires even when shared == kg (orchestrator root)."""
        _write_node(self.node, "N", "body", scope="shared")
        mod = _load_sync_module(self.root, shared=PROJECT_KG)
        server = _CountingServer()
        os.environ["SHARED_KG_WRITE_DISABLED"] = "true"
        ok, out = self._sync(mod, server)
        self.assertFalse(ok, out)
        self.assertEqual(len(self._store(server, PROJECT_KG)), 0)

    def test_gate_leaves_project_scoped_nodes_alone(self):
        """Leave-alone: the gate must not touch project-scoped writes."""
        _write_node(self.node, "N", "body", scope=None)
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        os.environ["SHARED_KG_WRITE_DISABLED"] = "true"
        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertEqual(len(self._store(server, PROJECT_KG)), 1)

    def test_legacy_opt_out_alias_honoured(self):
        _write_node(self.node, "N", "body", scope="shared")
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        os.environ["SHARED_KG_OPT_OUT"] = "true"  # canonical key absent
        ok, out = self._sync(mod, server)
        self.assertFalse(ok, out)
        self.assertEqual(len(self._store(server, SHARED_KG)), 0)

    def test_canonical_falsy_overrides_legacy(self):
        """server.py parity: the canonical key wins even when set to a
        falsy spelling, so users can explicitly RE-ENABLE shared writes."""
        _write_node(self.node, "N", "body", scope="shared")
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        os.environ["SHARED_KG_WRITE_DISABLED"] = "false"
        os.environ["SHARED_KG_OPT_OUT"] = "true"
        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertEqual(len(self._store(server, SHARED_KG)), 1)


class ScopeTransitionTests(ScopeRoutingTestBase):
    """Plan §3.3.6: project→shared migration delete (act + leave-alone);
    shared→project NO-delete + notice."""

    def test_project_to_shared_deletes_only_same_filepath_project_rows(self):
        _write_node(self.node, "N", "body", scope="shared")
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        # Rows that must be MIGRATED OUT (same file_path, project store):
        _seed_row(server, PROJECT_KG, self.fp)
        # Leave-alone rows: other file_path in project store; any row in an
        # unrelated third collection.
        keep_other_fp = _seed_row(server, PROJECT_KG, "knowledge/other.md")
        keep_other_coll = _seed_row(server, "Unrelated_KG", self.fp)

        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertIn("moved out of project collection", out)
        proj = self._store(server, PROJECT_KG)
        self.assertEqual(set(proj.keys()), {keep_other_fp},
                         "ONLY same-file_path project rows may be migrated out")
        self.assertIn(keep_other_coll, self._store(server, "Unrelated_KG"),
                      "rows in OTHER collections must be untouched")
        self.assertEqual(len(self._store(server, SHARED_KG)), 1)

    def test_shared_to_project_never_deletes_shared_rows(self):
        """Conservative destructive-action gate: deleting from a
        cross-project store on the evidence of a LOCAL frontmatter edit
        could destroy a colliding sibling project's rows — so the shared
        rows stay, and a one-line notice fires instead."""
        _write_node(self.node, "N", "body", scope=None)  # key removed
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        leftover = _seed_row(server, SHARED_KG, self.fp)

        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertIn(leftover, self._store(server, SHARED_KG),
                      "shared rows must NEVER be auto-deleted on scope removal")
        self.assertEqual(len(self._store(server, PROJECT_KG)), 1,
                         "the project-collection write must resume")
        self.assertIn("remain in shared collection", out)
        self.assertIn(SHARED_KG, out)


class SharedFastPathTests(ScopeRoutingTestBase):
    """Plan §3.4: the embed-skip fast path against the SHARED target."""

    def _node_hash(self, mod) -> str:
        return mod._content_signature_excluding_updated(
            self.node.read_text(encoding="utf-8")
        )

    def test_fast_path_hits_on_shared_target_and_migrates_leftovers(self):
        _write_node(self.node, "N", "body", scope="shared")
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        # Shared store already holds the up-to-date row → embed-skip.
        _seed_row(server, SHARED_KG, self.fp, content_hash=self._node_hash(mod))
        # A leftover project row from a partially-failed earlier migration
        # must STILL be cleaned on the fast-path exit.
        _seed_row(server, PROJECT_KG, self.fp)

        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertIn("Embed-skip", out)
        self.assertEqual(server.embed_calls, 0,
                         "hash match in the TARGET (shared) store must skip")
        self.assertEqual(len(self._store(server, SHARED_KG)), 1)
        self.assertEqual(len(self._store(server, PROJECT_KG)), 0,
                         "fast-path exit must still run the migration delete")

    def test_no_false_skip_when_hash_only_in_project_store(self):
        """The fast path MUST query the TARGET collection — a matching hash
        in the WRONG (project) store must not suppress the shared write."""
        _write_node(self.node, "N", "body", scope="shared")
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        _seed_row(server, PROJECT_KG, self.fp, content_hash=self._node_hash(mod))

        ok, out = self._sync(mod, server)
        self.assertTrue(ok, out)
        self.assertEqual(server.embed_calls, 1,
                         "a project-store hash match must NOT cause a false "
                         "skip for a shared-routed node")
        self.assertEqual(len(self._store(server, SHARED_KG)), 1)
        self.assertEqual(len(self._store(server, PROJECT_KG)), 0,
                         "the stale project row migrates out after the write")


if __name__ == "__main__":
    unittest.main()
