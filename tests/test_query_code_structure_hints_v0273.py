# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 M-2 / CG-2 — query_code_structure error hints + language marker.

- M-2: 'not found' errors carry an actionable hint (search_code_graph to
  confirm the identifier, slug-vs-prefix trap, stale-graph re-analyze).
- CG-2: a call-graph query (callers/path/type_users) that returns EMPTY
  carries `unsupported_for_language: True` + a note so the caller doesn't
  read "0 results" as "definitely none".
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP = REPO_ROOT / "claude_mcp_servers"
for _p in (str(REPO_ROOT), str(MCP)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class _FakeResp:
    def __init__(self, objects):
        self.objects = objects


class _FakeQuery:
    def __init__(self, objects):
        self._objects = objects

    def fetch_objects(self, *a, **k):
        return _FakeResp(self._objects)


class _FakeColl:
    def __init__(self, objects):
        self.query = _FakeQuery(objects)


class _FakeCollections:
    def __init__(self, by_name):
        self._by_name = by_name

    def get(self, name):
        # Return empty for any collection unless seeded — the base name is
        # the suffix after the last '_'.
        for base, objs in self._by_name.items():
            if name.endswith(base):
                return _FakeColl(objs)
        return _FakeColl([])


class _FakeClient:
    def __init__(self, by_name):
        self.collections = _FakeCollections(by_name)


class QueryCodeStructureHintTests(unittest.TestCase):
    def _run(self, srv, query_type, target, by_name):
        client = _FakeClient(by_name)
        with mock.patch.object(srv, "get_weaviate_client", return_value=client), \
             mock.patch.object(srv, "CODE_GRAPH_PROJECT", "", create=True):
            return json.loads(srv.query_code_structure(query_type, target, project=""))

    def test_class_not_found_has_actionable_hint(self):
        import claude_mcp_servers.weaviate_mcp.server as srv
        payload = self._run(srv, "methods", "NoSuchClass", {"CodeClass": []})
        self.assertFalse(payload["success"])
        err = payload["error"]
        self.assertIn("NoSuchClass", err)
        self.assertIn("search_code_graph", err)
        self.assertIn("slug-vs-prefix", err)
        self.assertIn("code-graph-analyze", err)

    def test_module_not_found_has_hint(self):
        import claude_mcp_servers.weaviate_mcp.server as srv
        payload = self._run(srv, "dependencies", "no/such/mod.py", {"CodeModule": []})
        self.assertFalse(payload["success"])
        self.assertIn("search_code_graph", payload["error"])

    def test_empty_callers_marks_unsupported_for_language(self):
        import claude_mcp_servers.weaviate_mcp.server as srv
        payload = self._run(srv, "callers", "someFunc", {"CodeFunction": []})
        self.assertTrue(payload["success"])
        self.assertEqual(payload["count"], 0)
        self.assertTrue(payload.get("unsupported_for_language"))
        self.assertIn("note", payload)
        self.assertIn("call-graph", payload["note"])

    def test_nonempty_callers_has_no_language_marker(self):
        import claude_mcp_servers.weaviate_mcp.server as srv

        class _Obj:
            def __init__(self):
                self.uuid = "u1"
                self.properties = {
                    "full_name": "caller.fn",
                    "signature": "def fn(): ...",
                    "file_path": "src/a.py",
                    "call_names": ["someFunc"],
                }
        payload = self._run(srv, "callers", "someFunc", {"CodeFunction": [_Obj()]})
        self.assertTrue(payload["success"])
        self.assertGreaterEqual(payload["count"], 1)
        self.assertNotIn("unsupported_for_language", payload)

    def test_non_callgraph_empty_has_no_marker(self):
        """An empty 'methods'/'dependencies' result is a plain not-found
        error, not a language-support marker."""
        import claude_mcp_servers.weaviate_mcp.server as srv
        payload = self._run(srv, "dependencies", "x.py", {"CodeModule": []})
        # dependencies returns an error (not the success+marker path).
        self.assertFalse(payload["success"])
        self.assertNotIn("unsupported_for_language", payload)


if __name__ == "__main__":
    unittest.main()
