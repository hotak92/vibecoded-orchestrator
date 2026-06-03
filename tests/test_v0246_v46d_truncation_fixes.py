# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression tests for v0.2.46 V46-D — 10 silent-truncation footgun fixes.

Auditor-1 of the v0.2.46 design surfaced 10 places where
``fetch_objects(limit=N)`` silently truncates when collections grow
large. The fixes use two patterns:

**Pattern A** — cursor pagination for "fetch all" use cases
(maintain_knowledge_graph, search_knowledge, detect_duplicates,
process_documents).

**Pattern B** — emit ``truncated: true`` flag for "top-N" use cases
(get_node_info inbound scan, query_code_graph CLI, weaviate_mcp
``query_code_structure``).

This file pins both patterns + a cross-site grep guard against
re-introduction of ``limit=1000`` / ``limit=2000`` literals in any of
the touched files.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"
HELPER_DIR = REPO_ROOT / "claude_mcp_servers" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(HELPER_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Shared fakes — minimal Weaviate v4-shape mocks
# ---------------------------------------------------------------------------


class _FakeObj:
    """Stand-in for a weaviate.collections.objects element."""

    def __init__(
        self,
        title: str,
        *,
        uuid: str | None = None,
        properties: dict[str, Any] | None = None,
    ):
        self.uuid = uuid or f"uuid-{title}"
        self.properties = properties or {
            "title": title,
            "node_type": "concept",
            "file_path": f"knowledge/concepts/{title}.md",
            "tags": [],
            "links": [],
            "source_node_id": title,
        }


class _PaginatingQuery:
    """A Weaviate-v4-shape query that paginates via the ``after=uuid``
    cursor.

    Stores the FULL list of objects and emits them in pages of size
    ``page_size`` keyed on ``after``. Used to verify that callers
    iterate the full collection rather than stopping at limit=1000.
    """

    def __init__(self, all_objects: list[_FakeObj], page_size_observed: list[int] | None = None):
        self.all_objects = all_objects
        self.call_log: list[dict] = []
        # If page_size_observed is set, every fetch_objects(limit=X)
        # appends X to it — handy for asserting on the page-size used.
        self.page_size_observed = page_size_observed

    def fetch_objects(self, **kwargs):
        limit = kwargs.get("limit", len(self.all_objects))
        after = kwargs.get("after")
        filters = kwargs.get("filters")
        self.call_log.append({"limit": limit, "after": after, "filters": filters})
        if self.page_size_observed is not None:
            self.page_size_observed.append(limit)
        # Find starting index from cursor
        if after is None:
            start = 0
        else:
            # Find object with this uuid and start at the next one
            start = 0
            for i, obj in enumerate(self.all_objects):
                if obj.uuid == after:
                    start = i + 1
                    break
        page = self.all_objects[start : start + limit]
        return mock.Mock(objects=page)

    def near_object(self, **kwargs):
        return mock.Mock(objects=[])


class _FilteringQuery(_PaginatingQuery):
    """A variant that applies a simple property filter so detect_duplicates
    can paginate AFTER its ``Filter.by_property('chunk_num').equal(1)``
    pass-through. The fake doesn't actually interpret the filter — it
    just paginates the whole list (every test object IS chunk_num=1).
    """

    def near_object(self, **kwargs):
        return mock.Mock(objects=[])


class _FakeCollection:
    def __init__(self, objects: list[_FakeObj], data_recorder: list[str] | None = None):
        self.query = _PaginatingQuery(objects)
        self.data = _FakeDataAPI(data_recorder)


class _FakeDataAPI:
    def __init__(self, recorder: list[str] | None = None):
        self._deletions = recorder if recorder is not None else []

    def delete_by_id(self, uuid: str) -> None:
        self._deletions.append(uuid)

    def insert(self, **kwargs) -> None:
        pass


class _FakeCollections:
    def __init__(self, contents: dict[str, list[_FakeObj]]):
        self.contents = contents
        self.requested: list[str] = []
        self.data_recorder: dict[str, list[str]] = {n: [] for n in contents}

    def get(self, name: str) -> _FakeCollection:
        self.requested.append(name)
        return _FakeCollection(self.contents.get(name, []), self.data_recorder.setdefault(name, []))


class _FakeClient:
    def __init__(self, contents: dict[str, list[_FakeObj]]):
        self.collections = _FakeCollections(contents)
        self._closed = False

    def close(self):
        self._closed = True


# ---------------------------------------------------------------------------
# Site 1: maintain_knowledge_graph.get_all_weaviate_nodes — cursor paginates
# ---------------------------------------------------------------------------


class GetAllWeaviateNodesTests(unittest.TestCase):
    """Site 1: Pattern A — orphan-detection must see ALL nodes, not first 1000."""

    def _make_nodes(self, count: int) -> list[_FakeObj]:
        nodes = []
        for i in range(count):
            obj = _FakeObj(f"Node{i:04d}")
            obj.properties = {
                "title": f"Node{i:04d}",
                "file_path": f"knowledge/concepts/node{i:04d}.md",
            }
            nodes.append(obj)
        return nodes

    def test_returns_more_than_1000_nodes(self):
        """Reproduces the truncation bug: pre-V46-D this would return
        exactly 1000 entries. Post-fix it returns all 1500."""
        import maintain_knowledge_graph as mkg

        nodes = self._make_nodes(1500)
        fake_collection = _FakeCollection(nodes)
        fake_server = mock.Mock()
        fake_server.client = mock.Mock()
        fake_server.client.collections.get = mock.Mock(return_value=fake_collection)

        result = mkg.get_all_weaviate_nodes(fake_server)
        self.assertEqual(
            len(result),
            1500,
            "get_all_weaviate_nodes must enumerate every node via cursor "
            "pagination — pre-V46-D it silently capped at 1000.",
        )

    def test_paginates_correctly_at_exact_multiple(self):
        """Boundary: exactly 2000 objects (2 full pages of 1000)."""
        import maintain_knowledge_graph as mkg

        nodes = self._make_nodes(2000)
        fake_collection = _FakeCollection(nodes)
        fake_server = mock.Mock()
        fake_server.client.collections.get = mock.Mock(return_value=fake_collection)

        result = mkg.get_all_weaviate_nodes(fake_server)
        self.assertEqual(len(result), 2000)
        # 2 pages of 1000 → 2 fetch_objects calls (the third returns
        # empty so we don't waste a call when page is < page_size).
        # Pagination loop: first page returns 1000 (full) so we keep
        # going; second page returns 1000 (also full) so we'd loop
        # again; third page returns 0 → loop ends. So 3 calls total
        # is acceptable. We just assert >= 2 (the pagination DID
        # happen).
        self.assertGreaterEqual(len(fake_collection.query.call_log), 2)


# ---------------------------------------------------------------------------
# Site 3: search_knowledge.list_all_nodes — cursor paginates
# ---------------------------------------------------------------------------


class ListAllNodesTests(unittest.TestCase):
    """Site 3: Pattern A — kg-search list must show ALL nodes."""

    def test_lists_more_than_1000_nodes(self):
        """Pre-V46-D this displayed only the first 1000 nodes."""
        # search_knowledge.py imports heavy modules at top — patch first
        nodes = []
        for i in range(1234):
            obj = _FakeObj(f"Item{i:04d}")
            obj.properties = {
                "title": f"Item{i:04d}",
                "node_type": "concept" if i % 2 == 0 else "tool",
                "file_path": f"knowledge/concepts/item{i:04d}.md",
            }
            nodes.append(obj)

        fake_collection = _FakeCollection(nodes)
        fake_client = mock.Mock()
        fake_client.collections.get = mock.Mock(return_value=fake_collection)
        fake_client.close = mock.Mock()

        import search_knowledge as sk

        with mock.patch.object(sk, "get_weaviate_client", return_value=fake_client):
            # Capture printed output
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                sk.list_all_nodes()
            output = buf.getvalue()

        # Verify the header reports the actual full count
        self.assertIn("1234 nodes", output, "list output should report 1234 nodes, not 1000")
        # Sanity: pagination happened — at least 2 fetch_objects calls
        self.assertGreaterEqual(len(fake_collection.query.call_log), 2)


# ---------------------------------------------------------------------------
# Site 4: detect_duplicates.find_duplicates — cursor paginates
# ---------------------------------------------------------------------------


class DetectDuplicatesTests(unittest.TestCase):
    """Site 4: Pattern A — duplicate scan must inspect ALL nodes."""

    def test_scans_more_than_1000_nodes(self):
        """Pre-V46-D the scan stopped at 1000 nodes. Post-fix it iterates
        the entire collection via cursor pagination."""
        # Build 1100 fake nodes
        nodes = []
        for i in range(1100):
            obj = _FakeObj(f"Dup{i:04d}", uuid=f"u-{i}")
            obj.properties = {
                "title": f"Dup{i:04d}",
                "file_path": f"knowledge/concepts/dup{i:04d}.md",
                "node_type": "concept",
                "tags": [],
                "chunk_num": 1,
            }
            # Each obj needs a `metadata` attribute for distance scoring
            # but we never reach that path in the test because near_object
            # is stubbed to return [].
            nodes.append(obj)

        import detect_duplicates as dd

        # Patch the constructor's collection init
        # Build a real instance, then swap the collection out for our fake.
        fake_collection = _FakeCollection(nodes)

        # Skip the constructor's connect-to-Weaviate by using __new__
        finder = dd.DuplicateDetector.__new__(dd.DuplicateDetector)
        finder.client = mock.Mock()
        finder.threshold = 0.95
        finder.collection = fake_collection

        # Capture stdout
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            duplicates = finder.find_duplicates()

        output = buf.getvalue()
        # The found-count message should report the actual full count.
        self.assertIn("1100", output, f"Expected 1100 in output, got: {output[:500]}")
        # Pagination happened
        self.assertGreaterEqual(len(fake_collection.query.call_log), 2)


# ---------------------------------------------------------------------------
# Site 6: process_documents.store_document_chunks — drains ALL old chunks
# ---------------------------------------------------------------------------


class StoreDocumentChunksDeleteTests(unittest.TestCase):
    """Site 6: Pattern A — delete-then-replace must delete ALL old chunks."""

    def test_deletes_more_than_1000_old_chunks(self):
        """Pre-V46-D a doc with > 1000 chunks left chunks 1001+ stale.
        Post-fix the cursor-paginated drain removes all of them."""
        # Construct 1200 fake chunk objects all with source_id="abc"
        chunks_in_db = []
        for i in range(1200):
            obj = _FakeObj(f"chunk{i:04d}", uuid=f"chunk-uuid-{i}")
            obj.properties = {"source_id": "abc", "chunk_number": i}
            chunks_in_db.append(obj)

        # The store function deletes via collection.data.delete_by_id and
        # then iterates again — to simulate the deletion correctly we
        # use a stateful collection mock.
        class _DrainCollection:
            def __init__(self, initial: list[_FakeObj]):
                self._remaining: list[_FakeObj] = list(initial)
                self.query = self
                self.data = self
                self.delete_calls: list[str] = []

            def fetch_objects(self, **kwargs):
                limit = kwargs.get("limit", len(self._remaining))
                # No `after` cursor honored here — the fix uses cursor=None
                # after each delete-page (deleted rows naturally disappear
                # from the next fetch). Return up to `limit` from the head.
                page = self._remaining[:limit]
                return mock.Mock(objects=page)

            def delete_by_id(self, uuid: str):
                self.delete_calls.append(uuid)
                self._remaining = [o for o in self._remaining if o.uuid != uuid]

        drain_coll = _DrainCollection(chunks_in_db)

        fake_server = mock.Mock()
        fake_server.client.collections.get = mock.Mock(return_value=drain_coll)
        fake_server._get_embedding = mock.Mock(return_value=[0.0] * 1024)

        import process_documents as pd

        # Pass empty new chunks list — we only care about the deletion path
        result = pd.store_document_chunks(fake_server, [], source_id="abc")
        self.assertTrue(result, "store_document_chunks should return True on success")
        # All 1200 must have been deleted
        self.assertEqual(
            len(drain_coll.delete_calls),
            1200,
            "All 1200 old chunks must be deleted (pre-V46-D only 1000 "
            "were deleted, leaving 200 stale chunks).",
        )


# ---------------------------------------------------------------------------
# Site 8/9/10: weaviate_mcp.query_code_structure — emits truncated flag
# ---------------------------------------------------------------------------


class QueryCodeStructureTruncationTests(unittest.TestCase):
    """Sites 8, 9, 10: Pattern B — MCP tool response carries
    ``truncated: bool`` + ``limit: int`` so the LLM sees when a top-N
    result list was capped."""

    def _patched_client(self, collection_objects: dict[str, list[_FakeObj]]):
        client = _FakeClient(collection_objects)
        return client

    def _make_fn_objects(self, count: int, *, full_name_prefix: str = "caller") -> list[_FakeObj]:
        objs = []
        for i in range(count):
            obj = _FakeObj(f"{full_name_prefix}{i}", uuid=f"fn-uuid-{i}")
            obj.properties = {
                "full_name": f"{full_name_prefix}{i}",
                "signature": f"def {full_name_prefix}{i}(): ...",
                "file_path": f"src/m{i}.py",
                "call_names": ["target_func"],
                "type_uses": ["TargetType"],
            }
            objs.append(obj)
        return objs

    def test_callers_emits_truncated_flag_at_limit(self):
        """Site 9: when callers query returns exactly CALLERS_LIMIT (50)
        objects, response must include truncated=True and limit=50."""
        import claude_mcp_servers.weaviate_mcp.server as srv

        # Build 50 caller objects (== the cap → truncated must be True)
        callers = self._make_fn_objects(50)
        contents = {"CodeFunction": callers}
        client = self._patched_client(contents)

        with mock.patch.object(srv, "get_weaviate_client", return_value=client), \
             mock.patch.object(srv, "CODE_GRAPH_PROJECT", "", create=True):
            raw = srv.query_code_structure("callers", "target_func", project="")

        payload = json.loads(raw)
        self.assertTrue(payload["success"])
        self.assertIn("truncated", payload, f"callers response missing truncated key: {payload}")
        self.assertIn("limit", payload)
        self.assertEqual(payload["limit"], 50)
        self.assertTrue(payload["truncated"], "Expected truncated=True at limit; got False")

    def test_callers_omits_truncated_flag_when_below_limit(self):
        """When result count is below the limit, truncated must be False."""
        import claude_mcp_servers.weaviate_mcp.server as srv

        callers = self._make_fn_objects(3)
        contents = {"CodeFunction": callers}
        client = self._patched_client(contents)

        with mock.patch.object(srv, "get_weaviate_client", return_value=client), \
             mock.patch.object(srv, "CODE_GRAPH_PROJECT", "", create=True):
            raw = srv.query_code_structure("callers", "target_func", project="")

        payload = json.loads(raw)
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["limit"], 50)
        self.assertEqual(payload["count"], 3)

    def test_imports_emits_truncated_flag(self):
        """Site 8: imports query — verify truncated flag for reverse-imports."""
        import claude_mcp_servers.weaviate_mcp.server as srv

        modules = []
        for i in range(20):  # == IMPORTS_LIMIT
            obj = _FakeObj(f"mod{i}", uuid=f"mod-uuid-{i}")
            obj.properties = {
                "path": f"src/mod{i}.py",
                "imports": ["target_module"],
            }
            modules.append(obj)
        contents = {"CodeModule": modules}
        client = self._patched_client(contents)

        with mock.patch.object(srv, "get_weaviate_client", return_value=client), \
             mock.patch.object(srv, "CODE_GRAPH_PROJECT", "", create=True):
            raw = srv.query_code_structure("imports", "target_module", project="")

        payload = json.loads(raw)
        self.assertTrue(payload["success"])
        self.assertTrue(payload["truncated"], f"imports should be truncated at 20: {payload}")
        self.assertEqual(payload["limit"], 20)

    def test_composed_by_emits_truncated_flag(self):
        """Site 10: composed_by query — verify truncated flag."""
        import claude_mcp_servers.weaviate_mcp.server as srv

        composing_classes = []
        for i in range(50):  # == COMPOSED_BY_LIMIT
            obj = _FakeObj(f"Class{i}", uuid=f"cls-uuid-{i}")
            obj.properties = {
                "full_name": f"pkg.Class{i}",
                "file_path": f"src/cls{i}.py",
                "composes": ["TargetClass"],
            }
            composing_classes.append(obj)
        contents = {"CodeClass": composing_classes}
        client = self._patched_client(contents)

        with mock.patch.object(srv, "get_weaviate_client", return_value=client), \
             mock.patch.object(srv, "CODE_GRAPH_PROJECT", "", create=True):
            raw = srv.query_code_structure("composed_by", "TargetClass", project="")

        payload = json.loads(raw)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["limit"], 50)

    def test_type_users_emits_truncated_flag(self):
        """Site 10: type_users query — verify truncated flag."""
        import claude_mcp_servers.weaviate_mcp.server as srv

        users = self._make_fn_objects(50)  # == TYPE_USERS_LIMIT
        contents = {"CodeFunction": users}
        client = self._patched_client(contents)

        with mock.patch.object(srv, "get_weaviate_client", return_value=client), \
             mock.patch.object(srv, "CODE_GRAPH_PROJECT", "", create=True):
            raw = srv.query_code_structure("type_users", "TargetType", project="")

        payload = json.loads(raw)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["limit"], 50)

    def test_methods_query_omits_truncation_meta(self):
        """The methods query reads a single class's `methods` list field;
        it can't truncate. Verify the response omits the truncated/limit
        keys (keeps payload tight for trivial queries)."""
        import claude_mcp_servers.weaviate_mcp.server as srv

        obj = _FakeObj("MyClass", uuid="cls-1")
        obj.properties = {
            "full_name": "pkg.MyClass",
            "file_path": "src/cls.py",
            "methods": ["m1", "m2", "m3"],
        }
        contents = {"CodeClass": [obj]}
        client = self._patched_client(contents)

        with mock.patch.object(srv, "get_weaviate_client", return_value=client), \
             mock.patch.object(srv, "CODE_GRAPH_PROJECT", "", create=True):
            raw = srv.query_code_structure("methods", "pkg.MyClass", project="")

        payload = json.loads(raw)
        self.assertTrue(payload["success"])
        # methods query never truncates → no flag in response
        self.assertNotIn(
            "truncated",
            payload,
            "methods query should NOT emit truncated flag (it reads a "
            "single class's methods list, can't truncate)",
        )


# ---------------------------------------------------------------------------
# Site 7: query_code_graph.py CLI — emits truncation message
# ---------------------------------------------------------------------------


class QueryCodeGraphCliTruncationTests(unittest.TestCase):
    """Site 7: CLI emits a user-visible truncation message for the
    callers + interactions top-N queries."""

    def test_callers_cli_prints_truncation_message_at_limit(self):
        """Pre-V46-D the CLI silently capped at 50 candidate functions.
        Post-fix it prints a clear "Searched only the first 50" message
        when the limit is hit."""
        import query_code_graph as qcg

        target_func_obj = _FakeObj("target_func", uuid="target-uuid")
        target_func_obj.properties = {
            "full_name": "target_func",
            "signature": "def target_func()",
            "file_path": "src/t.py",
        }

        # 50 candidate caller objects (== CALLERS_FETCH_LIMIT)
        # The CLI then filters them by call_names containing target.
        # Make all 50 reference target_func.
        target_uuid = target_func_obj.uuid
        candidate_callers = []
        for i in range(50):
            obj = _FakeObj(f"caller{i}", uuid=f"caller-uuid-{i}")
            obj.properties = {
                "full_name": f"caller{i}",
                "signature": f"def caller{i}()",
                "file_path": f"src/c{i}.py",
            }
            # Each caller's `references.get("calls", [])` returns one ref
            # whose uuid matches target_func.
            obj.references = {"calls": [mock.Mock(uuid=target_uuid)]}
            candidate_callers.append(obj)

        # The CLI does two fetch_objects calls against CodeFunction:
        # 1. filter by full_name == "target_func" (limit=1)
        # 2. fetch caller candidates (limit=50)
        # We build a stateful fake that returns the right object per call.
        call_counter = [0]

        class _CliQuery:
            def fetch_objects(self, **kwargs):
                call_counter[0] += 1
                # First call: target lookup with filter
                if call_counter[0] == 1:
                    return mock.Mock(objects=[target_func_obj])
                # Second call: candidate caller pool
                return mock.Mock(objects=candidate_callers)

        cli_coll = mock.Mock()
        cli_coll.query = _CliQuery()

        fake_client = mock.Mock()
        fake_client.collections.get = mock.Mock(return_value=cli_coll)
        fake_client.close = mock.Mock()

        # Instantiate the CLI class via __new__ to skip its __init__
        # (which connects to Weaviate).
        cli = qcg.CodeGraphQuery.__new__(qcg.CodeGraphQuery)
        cli.client = fake_client
        cli.project = ""
        cli.collection_prefix = ""
        cli._collection_cache = {}

        # Capture printed output
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.query_structure("callers", "target_func")
        output = buf.getvalue()

        # Truncation message must be printed
        self.assertIn(
            "first 50",
            output,
            f"CLI should print truncation message at cap; output was:\n{output}",
        )


# ---------------------------------------------------------------------------
# Site 5: get_node_info — cursor paginates inbound scan
# ---------------------------------------------------------------------------


class GetNodeInfoInboundScanTests(unittest.TestCase):
    """Site 5: Pattern A-ish — inbound-link scan uses cursor pagination
    (with a defense-in-depth cap of 10000 per collection). We verify
    that nodes >200 are inspected (previously capped at limit=200)."""

    def _fresh_get_node_info(self, env_overrides: dict[str, str]):
        for mod in ("get_node_info", "kg_access"):
            sys.modules.pop(mod, None)
        for k in ("VCT_KG_ACCESS_LIST", "KG_COLLECTION", "SHARED_KG_COLLECTION"):
            os.environ.pop(k, None)
        for k, v in env_overrides.items():
            os.environ[k] = v
        return importlib.import_module("get_node_info")

    def test_inbound_scan_sees_nodes_past_200(self):
        """Build 250 nodes, with only the LAST one (index 249) linking
        to the target. Pre-V46-D this would miss it (limit=200 cap).
        Post-fix the cursor pagination reaches it."""
        gni = self._fresh_get_node_info({"KG_COLLECTION": "Alpha_KnowledgeGraph"})

        nodes = []
        for i in range(250):
            obj = _FakeObj(f"Node{i:03d}")
            obj.properties = {
                "title": f"Node{i:03d}",
                "node_type": "concept",
                "file_path": f"knowledge/concepts/n{i:03d}.md",
                "tags": [],
                "links": ["Target"] if i == 249 else [],  # only the LAST one links to Target
                "content": f"content{i}",
                "source_node_id": f"Node{i:03d}",
                "created_at": "",
                "updated_at": "",
            }
            nodes.append(obj)
        # Add the target itself at position 0
        target = _FakeObj("Target")
        target.properties = {
            "title": "Target",
            "node_type": "concept",
            "file_path": "knowledge/concepts/target.md",
            "tags": [],
            "links": [],
            "content": "target content",
            "source_node_id": "Target",
            "created_at": "",
            "updated_at": "",
        }
        nodes_with_target = [target] + nodes

        # Use the paginating fake collection
        fake_collection = _FakeCollection(nodes_with_target)
        fake_client = mock.Mock()
        fake_client.collections.get = mock.Mock(return_value=fake_collection)
        fake_client.close = mock.Mock()

        with mock.patch.object(gni, "get_weaviate_client", return_value=fake_client):
            # Capture stdout
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                gni.find_connections("Target")
            output = buf.getvalue()

        # The inbound count must include Node249 → INBOUND (1)
        # If pre-V46-D regression returns 200, this would be INBOUND (0).
        self.assertIn(
            "INBOUND (1)",
            output,
            f"Inbound scan must find Node249 (linking from beyond pos 200); output was:\n{output[-500:]}",
        )


# ---------------------------------------------------------------------------
# Cross-site grep guard: no limit=1000 / limit=2000 in patched files
# ---------------------------------------------------------------------------


class NoReintroductionOfLimitLiteralsTests(unittest.TestCase):
    """Cross-site guard — if a future refactor reintroduces a
    ``limit=1000`` / ``limit=2000`` literal in one of the V46-D files
    (outside of a comment), this test fails immediately.

    The 10 enumerated sites all replaced their bare ``limit=1000`` /
    ``limit=200`` / ``limit=50`` literals with either cursor pagination
    (Pattern A) or named constants + truncation signal (Pattern B).
    Any new bare literal is a regression."""

    PATCHED_FILES = [
        REPO_ROOT / "templates" / "scripts" / "maintain_knowledge_graph.py",
        REPO_ROOT / "templates" / "scripts" / "search_knowledge.py",
        REPO_ROOT / "templates" / "scripts" / "detect_duplicates.py",
        REPO_ROOT / "templates" / "scripts" / "process_documents.py",
        # query_code_graph.py uses sibling limit + named constants — the
        # SIBLING_FETCH_LIMIT=64 and CALLERS_FETCH_LIMIT=50 named-constant
        # form is allowed (those are intentional caps); only the BARE
        # literal forms are banned.
    ]

    def test_no_bare_limit_1000_or_2000_literals_outside_comments(self):
        """Scan via Python's ``ast`` module: walk the AST and report
        any keyword argument named ``limit`` whose value is the literal
        integer ``1000`` or ``2000``. AST-walking avoids both comment
        and docstring false-positives that a regex would hit.
        """
        import ast

        offenders: list[str] = []
        for fpath in self.PATCHED_FILES:
            self.assertTrue(fpath.exists(), f"Expected patched file to exist: {fpath}")
            try:
                tree = ast.parse(fpath.read_text(encoding="utf-8"))
            except SyntaxError as e:  # pragma: no cover
                self.fail(f"V46-D-touched file failed to parse as Python: {fpath}: {e}")
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg != "limit":
                            continue
                        if isinstance(kw.value, ast.Constant) and kw.value.value in (1000, 2000):
                            offenders.append(
                                f"{fpath.relative_to(REPO_ROOT)}:{kw.value.lineno}: "
                                f"limit={kw.value.value} in call"
                            )
        self.assertFalse(
            offenders,
            "V46-D regression: bare `limit=1000` / `limit=2000` kwarg "
            "literals reintroduced in patched files. Use cursor pagination "
            "(Pattern A) or named constant + truncation signal (Pattern B):\n"
            + "\n".join(offenders),
        )

    def test_no_bare_limit_200_in_get_node_info(self):
        """The get_node_info inbound scan replaced limit=200 with cursor
        pagination + a MAX_OBJECTS_PER_COLLECTION sentinel. A bare
        limit=200 reintroduction is a regression."""
        import ast

        fpath = REPO_ROOT / "templates" / "scripts" / "get_node_info.py"
        tree = ast.parse(fpath.read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "limit" and isinstance(kw.value, ast.Constant) and kw.value.value == 200:
                        offenders.append(f"{fpath.name}:{kw.value.lineno}")
        self.assertFalse(
            offenders,
            "V46-D regression: bare `limit=200` kwarg reintroduced in "
            "get_node_info.py — use cursor pagination:\n" + "\n".join(offenders),
        )

    def test_mcp_server_query_code_structure_uses_named_constants(self):
        """V46-D: weaviate_mcp/server.py.query_code_structure top-N caps
        must be expressed as named constants (CALLERS_LIMIT, etc.) so
        the source is grep-able and the truncated flag is computed
        against them. Verify all four named constants exist near the
        query branches."""
        srv_path = REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "server.py"
        text = srv_path.read_text(encoding="utf-8")
        # Look for the constants introduced by V46-D
        expected_constants = ("CALLERS_LIMIT", "IMPORTS_LIMIT", "INTERACTIONS_LIMIT",
                              "COMPOSED_BY_LIMIT", "TYPE_USERS_LIMIT")
        for const in expected_constants:
            self.assertIn(
                const,
                text,
                f"V46-D: named constant {const} missing from "
                "claude_mcp_servers/weaviate_mcp/server.py — top-N caps "
                "must be expressed as named constants so truncated/limit "
                "fields can be computed against them.",
            )


if __name__ == "__main__":
    unittest.main()
