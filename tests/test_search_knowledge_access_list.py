# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Integration tests for ``search_knowledge.py`` honoring the access
matrix (``VCT_KG_ACCESS_LIST``).

This is the CLI hook-driven path:
    .claude/scripts/kg-search → search_knowledge.py search "..."

Pre-2026-05-08 (P1-D fix) the script queried only KG_COLLECTION
[+ SHARED_KG_COLLECTION]. After the fix it fans out across self +
shared + every peer in VCT_KG_ACCESS_LIST.

Tests mock Weaviate at the ``weaviate.connect_to_custom`` boundary so
no real Weaviate is needed. The mock records which collections were
asked for; the test assertion is "the per-collection set matches what
the access matrix says it should be".
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "templates" / "scripts"
HELPER_DIR = PROJECT_ROOT / "claude_mcp_servers" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(HELPER_DIR))


def _clear_env() -> None:
    for k in (
        "VCT_KG_ACCESS_LIST",
        "VCT_CODE_GRAPH_ACCESS_LIST",
        "KG_COLLECTION",
        "SHARED_KG_COLLECTION",
        "DEVELOPMENT_COLLECTION",
    ):
        os.environ.pop(k, None)


def _fresh_search_knowledge(env_overrides: dict[str, str]):
    """Reload search_knowledge with a fresh env snapshot."""
    for mod in list(sys.modules):
        if mod in ("search_knowledge", "kg_access"):
            del sys.modules[mod]
    for k, v in env_overrides.items():
        os.environ[k] = v
    return importlib.import_module("search_knowledge")


class _FakeMetadata:
    """Mimic the slice of weaviate.metadata used by search_knowledge."""

    def __init__(self, distance: float = 0.5):
        self.distance = distance


class _FakeObj:
    def __init__(self, title: str, distance: float = 0.5):
        self.properties = {
            "title": title,
            "node_type": "concept",
            "file_path": f"knowledge/concepts/{title}.md",
            "tags": [],
            "content": f"content for {title}",
            "source_node_id": title,
            "links": [],
            "created_at": "",
            "updated_at": "",
        }
        self.metadata = _FakeMetadata(distance=distance)


class _FakeQuery:
    """Mimic ``coll.query.near_vector(...).objects``."""

    def __init__(self, objects):
        self._objects = objects

    def near_vector(self, **kwargs):
        # Capture kwargs on the parent collection so tests can inspect
        # what filter / target_vector / limit was used.
        return mock.Mock(objects=self._objects)

    def fetch_objects(self, **kwargs):
        return mock.Mock(objects=self._objects)


class _FakeCollection:
    def __init__(self, name: str, objects: list | None = None):
        self.name = name
        self.query = _FakeQuery(objects or [])


class _FakeCollections:
    """Tracks every ``client.collections.get(name)`` call."""

    def __init__(self, contents: dict[str, list]):
        self.contents = contents
        self.requested: list[str] = []

    def get(self, name: str) -> _FakeCollection:
        self.requested.append(name)
        return _FakeCollection(name, self.contents.get(name, []))


class _FakeClient:
    def __init__(self, contents: dict[str, list]):
        self.collections = _FakeCollections(contents)
        self._closed = False

    def close(self):
        self._closed = True


class SearchKnowledgeAccessListTests(unittest.TestCase):
    """``search_knowledge.search_knowledge(...)`` queries every
    collection in the access matrix."""

    def setUp(self) -> None:
        _clear_env()

    def test_no_access_list_queries_self_only(self) -> None:
        """Negative direction: with no VCT_KG_ACCESS_LIST and no shared,
        the CLI queries only the self collection — same shape as
        pre-P1-D."""
        sk = _fresh_search_knowledge({
            "KG_COLLECTION": "Alpha_KnowledgeGraph",
        })
        contents = {"Alpha_KnowledgeGraph": [_FakeObj("Foo")]}
        client = _FakeClient(contents)
        with mock.patch.object(sk, "get_weaviate_client", return_value=client), \
             mock.patch.object(sk, "get_embedding", return_value=[0.0] * 1024):
            sk.search_knowledge("foo", limit=5)
        self.assertEqual(client.collections.requested, ["Alpha_KnowledgeGraph"])

    def test_access_list_fans_out_across_peers(self) -> None:
        """Positive direction: with VCT_KG_ACCESS_LIST=Beta,Gamma the
        CLI queries Alpha + Shared + Beta + Gamma collections in
        order."""
        sk = _fresh_search_knowledge({
            "KG_COLLECTION": "Alpha_KnowledgeGraph",
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            "VCT_KG_ACCESS_LIST": "Beta,Gamma",
        })
        contents = {
            "Alpha_KnowledgeGraph": [_FakeObj("Foo", distance=0.1)],
            "VibecodedOrchestrator_KnowledgeGraph": [_FakeObj("Bar", distance=0.2)],
            "Beta_KnowledgeGraph": [_FakeObj("Baz", distance=0.3)],
            "Gamma_KnowledgeGraph": [_FakeObj("Qux", distance=0.4)],
        }
        client = _FakeClient(contents)
        with mock.patch.object(sk, "get_weaviate_client", return_value=client), \
             mock.patch.object(sk, "get_embedding", return_value=[0.0] * 1024):
            sk.search_knowledge("foo", limit=5)
        self.assertEqual(
            client.collections.requested,
            [
                "Alpha_KnowledgeGraph",
                "VibecodedOrchestrator_KnowledgeGraph",
                "Beta_KnowledgeGraph",
                "Gamma_KnowledgeGraph",
            ],
            "Expected fan-out across self → shared → peers in env-order",
        )

    def test_peer_collection_unavailable_does_not_break_self(self) -> None:
        """Peer collection that doesn't exist (peer never indexed)
        must not break the CLI — the failure should be silently
        swallowed and the self/shared results should still come back."""
        sk = _fresh_search_knowledge({
            "KG_COLLECTION": "Alpha_KnowledgeGraph",
            "VCT_KG_ACCESS_LIST": "MissingPeer",
        })

        class _PartialClient:
            """A client where Alpha works but MissingPeer raises."""

            def __init__(self):
                self.requested: list[str] = []
                self.collections = self  # self-pointing for .collections.get

            def get(self, name: str):
                self.requested.append(name)
                if name == "MissingPeer_KnowledgeGraph":
                    raise RuntimeError("collection not found")
                return _FakeCollection(name, [_FakeObj("Foo")])

            def close(self):
                pass

        client = _PartialClient()
        with mock.patch.object(sk, "get_weaviate_client", return_value=client), \
             mock.patch.object(sk, "get_embedding", return_value=[0.0] * 1024):
            # Must not raise.
            sk.search_knowledge("foo", limit=5)
        # Both collections were attempted; the failure on MissingPeer
        # was caught.
        self.assertEqual(
            client.requested,
            ["Alpha_KnowledgeGraph", "MissingPeer_KnowledgeGraph"],
        )

    def test_collections_to_query_helper_is_used(self) -> None:
        """The CLI must go through ``_kg_collections_to_search``, not
        the legacy inline ``[KG_COLLECTION, SHARED_KG_COLLECTION]``
        construction. This pins the wire-up — if a future refactor
        accidentally bypasses the helper, this test fails.
        """
        sk = _fresh_search_knowledge({
            "KG_COLLECTION": "Alpha_KnowledgeGraph",
            "VCT_KG_ACCESS_LIST": "Beta",
        })

        # Patch the helper to return a sentinel value; the CLI should
        # use exactly that list.
        sentinel = ["SENTINEL_COLLECTION_1", "SENTINEL_COLLECTION_2"]
        contents = {n: [] for n in sentinel}
        client = _FakeClient(contents)
        with mock.patch.object(sk, "_kg_collections_to_search", return_value=sentinel), \
             mock.patch.object(sk, "get_weaviate_client", return_value=client), \
             mock.patch.object(sk, "get_embedding", return_value=[0.0] * 1024):
            sk.search_knowledge("foo", limit=5)
        self.assertEqual(client.collections.requested, sentinel)


if __name__ == "__main__":
    unittest.main()
