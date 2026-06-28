# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.70 FIX #1: ``kg-sync --all`` must not silently drop an active node.

Reported symptom (3rd-party adoption): ``kg-sync --all`` printed
"147 succeeded, 0 failed" yet the collection count stayed below the file
count — the SAME handful of active nodes were dropped on every ``--all``
run, while syncing each file INDIVIDUALLY stored it fine.

Verified root cause (this module's reproduction):
``sync_node``'s archived-node cleanup deleted prior Weaviate rows by
``title``. Title is NOT unique across nodes. During a ``--all`` run, when an
archived node shares its ``title`` with a DIFFERENT active node, processing
the archived node ran a title-scoped delete that also removed the active
node's rows. ``sync_node`` still returned ``True`` for both (the archive skip
is an intentional success), so the success tally was inflated while real data
was lost — and a per-file sync of the active node alone never triggered the
archived sibling's delete, so it always worked. Re-running ``--all`` re-
inserted the active node then deleted it again, matching "re-running doesn't
land them".

The fix scopes the archived-node cleanup to ``file_path`` (unique per node)
instead of ``title``. These tests lock in:

  * the batch DROP regression (active node survives the same-title archived
    sibling),
  * that the archived node itself is still excluded from the index,
  * that the clean, no-collision case is unaffected,
  * and the docs/ (development collection) mirror of the same fix.

Pure unit tests with an in-memory fake Weaviate client — no network / Ollama.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"


# ─── In-memory fake Weaviate client ──────────────────────────────────────


class _FakeProp:
    def __init__(self, name: str):
        self.name = name

    def equal(self, value):
        return _FakeFilter([(self.name, value)])


class _FakeFilter:
    """Minimal stand-in for ``weaviate.classes.query.Filter``.

    Records (prop_name, value) match pairs and AND-combines them so the
    real ``sync_node`` filter expressions resolve against a dict store.
    """

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

    def fetch_objects(
        self,
        filters=None,
        limit=100,
        return_properties=None,
        include_vector=False,
    ):
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


class _FakeServer:
    def __init__(self):
        self.client = _FakeClient()
        self.embedding_service = _FakeEmbeddingService()
        self.text_vector_slot = "qwen3_embed"

    def _get_embedding(self, text):  # noqa: ARG002
        return [0.1, 0.2, 0.3]

    def _get_all_kg_embeddings(self, text):  # noqa: ARG002
        return {"qwen3_embed": [0.1, 0.2, 0.3]}


# ─── Module loader (fresh import per project root) ────────────────────────


def _load_sync_module(project_root: Path):
    """Import sync_knowledge_graph.py with module-level globals bound to
    ``project_root`` (PROJECT_ROOT / KNOWLEDGE_ROOT are computed from
    ``KG_BASE_DIR`` at import time, so set the env BEFORE importing).

    A unique module name per call avoids cross-test global bleed.
    """
    os.environ["KG_BASE_DIR"] = str(project_root)
    os.environ["KG_COLLECTION"] = "TestProject_KnowledgeGraph"
    os.environ["DEVELOPMENT_COLLECTION"] = "TestProject_Development"
    os.environ["DUAL_EMBEDDING_ENABLED"] = "false"
    # Keep the hub out of the loop so collection names resolve from env.
    os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"

    mod_name = f"_sync_kg_under_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:  # deps not installed in this env
        raise unittest.SkipTest(
            f"sync_knowledge_graph.py has runtime deps not installed "
            f"({exc}); skipping."
        )
    # Swap the real Weaviate Filter for the in-memory fake.
    mod.Filter = _FakeFilter
    return mod


def _write(path: Path, title: str, status: str, body: str = "Body."):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\ntype: concept\nstatus: {status}\n---\n{body}\n",
        encoding="utf-8",
    )


class BatchTitleCollisionTest(unittest.TestCase):
    """The core regression: --all must not drop an active node that shares a
    title with an archived sibling."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        for k in (
            "KG_BASE_DIR", "KG_COLLECTION", "DEVELOPMENT_COLLECTION",
            "DUAL_EMBEDDING_ENABLED", "VCT_DISABLE_HUB_RESOLVER",
        ):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def _kg_file_paths(self, mod, server) -> list[str]:
        store = server.client.collections.get(mod.COLLECTION_NAME)._store
        return [o.properties.get("file_path") for o in store.values()]

    def test_active_node_survives_same_title_archived_sibling(self):
        """Active node (sorts BEFORE archived sibling) must remain indexed.

        Pre-fix: the archived node's title-scoped delete removed the active
        node's row → objects=0, active gone, success inflated to 2.
        Post-fix: file_path-scoped delete touches only the archived file →
        the active node persists.
        """
        # aaa-active.md sorts before zzz-archived.md, so the archived node is
        # processed AFTER the active one is inserted — the dangerous order.
        _write(self.root / "knowledge" / "concepts" / "aaa-active.md",
               "Shared Title", "active", "Active body.")
        _write(self.root / "knowledge" / "concepts" / "zzz-archived.md",
               "Shared Title", "archived", "Archived body.")

        mod = _load_sync_module(self.root)
        server = _FakeServer()
        success, fail = mod.sync_all_nodes(server)

        fps = self._kg_file_paths(mod, server)
        self.assertIn(
            "knowledge/concepts/aaa-active.md", fps,
            "active node was dropped by the archived sibling's cleanup "
            f"(file_paths={fps!r})",
        )
        self.assertNotIn(
            "knowledge/concepts/zzz-archived.md", fps,
            "archived node must NOT be indexed",
        )
        # Exactly one object: the active node.
        store = server.client.collections.get(mod.COLLECTION_NAME)._store
        self.assertEqual(len(store), 1, f"expected 1 object, got {len(store)}")
        self.assertEqual(fail, 0)

    def test_path_archived_sibling_does_not_drop_active(self):
        """Same fix via the path-based (`archive/`) archive branch.

        Here the active node sorts AFTER the archived one (concepts/ > archive/
        alphabetically), so the archived delete runs first (harmless), then the
        active node is inserted. Either ordering must keep the active node.
        """
        _write(self.root / "knowledge" / "archive" / "old.md",
               "Reused Title", "active", "Archived-by-path body.")
        _write(self.root / "knowledge" / "concepts" / "current.md",
               "Reused Title", "active", "Active body.")

        mod = _load_sync_module(self.root)
        server = _FakeServer()
        mod.sync_all_nodes(server)

        fps = self._kg_file_paths(mod, server)
        self.assertIn("knowledge/concepts/current.md", fps)
        self.assertNotIn("knowledge/archive/old.md", fps)

    def test_clean_no_collision_idempotent(self):
        """No title collisions: every distinct node persists across two --all
        runs (embed-skip preserves them; nothing is dropped)."""
        for i in range(5):
            _write(self.root / "knowledge" / "concepts" / f"n{i}.md",
                   f"Node {i}", "active", f"Body {i}.")

        mod = _load_sync_module(self.root)
        server = _FakeServer()
        mod.sync_all_nodes(server)
        store = server.client.collections.get(mod.COLLECTION_NAME)._store
        self.assertEqual(len(store), 5, "all 5 nodes must persist after run 1")

        mod.sync_all_nodes(server)  # second pass → embed-skip
        store = server.client.collections.get(mod.COLLECTION_NAME)._store
        self.assertEqual(len(store), 5, "all 5 nodes must persist after run 2")

    def test_resync_changed_file_does_not_stack_duplicate_rows(self):
        """CONCERN 1 (v0.2.70 dedup follow-up): re-syncing a file whose CONTENT
        changed must DELETE its prior file_path rows BEFORE inserting the new
        ones — never stack. A changed body misses the embed-skip fast-path, so
        this exercises the delete-then-insert leg directly.
        """
        path = self.root / "knowledge" / "concepts" / "evolving.md"
        _write(path, "Evolving", "active", "First version of the body.")

        mod = _load_sync_module(self.root)
        server = _FakeServer()
        self.assertTrue(mod.sync_node(server, path))
        store = server.client.collections.get(mod.COLLECTION_NAME)._store
        self.assertEqual(len(store), 1, "first sync → exactly 1 row")

        # Change the body so the content_hash differs (fast-path will NOT fire).
        _write(path, "Evolving", "active", "Second, substantially different body.")
        self.assertTrue(mod.sync_node(server, path))
        store = server.client.collections.get(mod.COLLECTION_NAME)._store
        self.assertEqual(
            len(store), 1,
            f"re-sync of a changed file stacked a duplicate (rows={len(store)})",
        )
        # The surviving row carries the NEW body, proving delete-then-insert.
        surviving = next(iter(store.values()))
        self.assertIn("Second", surviving.properties.get("content", ""))

    def test_two_distinct_files_identical_content_are_two_nodes(self):
        """CONCERN 1 over-collapse guard: two DIFFERENT files with byte-identical
        bodies are two legitimately-distinct nodes (different file_path) and must
        BOTH be indexed — the sync layer must NOT collapse them into one.
        """
        body = "Exactly the same body text in two files."
        _write(self.root / "knowledge" / "concepts" / "one.md", "One", "active", body)
        _write(self.root / "knowledge" / "concepts" / "two.md", "Two", "active", body)

        mod = _load_sync_module(self.root)
        server = _FakeServer()
        mod.sync_all_nodes(server)

        fps = sorted(self._kg_file_paths(mod, server))
        self.assertEqual(
            fps,
            ["knowledge/concepts/one.md", "knowledge/concepts/two.md"],
            "two distinct files with identical content must stay two nodes",
        )

    def test_delete_node_by_file_path_is_scoped(self):
        """Unit-level: _delete_node_by_file_path removes only the matching
        file_path's rows, never a same-title sibling at a different path."""
        mod = _load_sync_module(self.root)
        server = _FakeServer()
        coll = server.client.collections.get(mod.COLLECTION_NAME)
        coll.data.insert(properties={
            "title": "Dup", "file_path": "knowledge/concepts/a.md",
        })
        coll.data.insert(properties={
            "title": "Dup", "file_path": "knowledge/concepts/b.md",
        })

        removed = mod._delete_node_by_file_path(
            server, "knowledge/concepts/a.md"
        )
        self.assertEqual(removed, 1)
        remaining = [
            o.properties["file_path"]
            for o in server.client.collections.get(mod.COLLECTION_NAME)._store.values()
        ]
        self.assertEqual(remaining, ["knowledge/concepts/b.md"])


if __name__ == "__main__":
    unittest.main()
