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


def _filter_target(flt):
    """Best-effort extract the leaf property name a weaviate Filter targets.

    Handles a single _FilterValue (``.target``) and a compound _Filters
    (``.filters`` — return the FIRST leaf's target, which for our queries is
    the discriminating property). Returns "" when it can't tell."""
    if flt is None:
        return ""
    tgt = getattr(flt, "target", None)
    if isinstance(tgt, str):
        return tgt
    subs = getattr(flt, "filters", None)
    if subs:
        for s in subs:
            t = _filter_target(s)
            if t:
                return t
    return ""


class _FakeQuery:
    def __init__(self, objects, filter_aware=False):
        self._objects = objects
        self._filter_aware = filter_aware

    def fetch_objects(self, *a, **k):
        if not self._filter_aware:
            return _FakeResp(self._objects)
        # P2e: distinguish the callers/type_users query (filters on
        # call_names / type_uses) from the language probe (filters on
        # full_name / path). Return an object only when its own property
        # actually satisfies the queried predicate — so the SAME seeded row
        # can be absent as a caller yet present for the language probe.
        prop = _filter_target(k.get("filters"))
        val = getattr(k.get("filters"), "value", None)
        matched = []
        for obj in self._objects:
            props = getattr(obj, "properties", {}) or {}
            if prop in ("call_names", "type_uses"):
                have = props.get(prop) or []
                wants = val if isinstance(val, (list, tuple)) else [val]
                if any(w in have for w in wants if w is not None):
                    matched.append(obj)
            elif prop in ("full_name", "path"):
                # Language probe (or path source seed): match the identity prop.
                if props.get(prop) == val or props.get("full_name") == val:
                    matched.append(obj)
            else:
                matched.append(obj)
        return _FakeResp(matched)


class _FakeColl:
    def __init__(self, objects, filter_aware=False):
        self.query = _FakeQuery(objects, filter_aware=filter_aware)


class _FakeCollections:
    def __init__(self, by_name, filter_aware=False):
        self._by_name = by_name
        self._filter_aware = filter_aware

    def get(self, name):
        # Return empty for any collection unless seeded — the base name is
        # the suffix after the last '_'.
        for base, objs in self._by_name.items():
            if name.endswith(base):
                return _FakeColl(objs, filter_aware=self._filter_aware)
        return _FakeColl([], filter_aware=self._filter_aware)


class _FakeClient:
    def __init__(self, by_name, filter_aware=False):
        self.collections = _FakeCollections(by_name, filter_aware=filter_aware)


class QueryCodeStructureHintTests(unittest.TestCase):
    def _run(self, srv, query_type, target, by_name, filter_aware=False):
        client = _FakeClient(by_name, filter_aware=filter_aware)
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

    def test_empty_callers_non_python_marks_unsupported(self):
        """P2e: an empty callers result for a NON-Python target keeps the
        marker (the call-graph edge type is genuinely unsupported for it).
        The target-language probe finds a rust CodeFunction row for someFunc."""
        import claude_mcp_servers.weaviate_mcp.server as srv

        class _RustObj:
            uuid = "u-rust"
            properties = {"full_name": "someFunc", "language": "rust", "call_names": []}

        # callers query filters call_names.contains_any(...) → no MATCH (the row
        # doesn't call someFunc); the SAME collection then serves the language
        # probe (full_name.equal) and returns the rust row.
        payload = self._run(
            srv, "callers", "someFunc", {"CodeFunction": [_RustObj()]},
            filter_aware=True,
        )
        self.assertTrue(payload["success"])
        self.assertEqual(payload["count"], 0)
        self.assertTrue(payload.get("unsupported_for_language"))
        self.assertIn("note", payload)
        self.assertIn("call-graph", payload["note"])

    def test_empty_callers_python_no_marker_but_note(self):
        """P2e HONESTY FIX: a Python target with genuinely zero callers must NOT
        carry the over-claiming boolean (it mislabeled RL training data). The
        explanatory note still stands."""
        import claude_mcp_servers.weaviate_mcp.server as srv

        class _PyObj:
            uuid = "u-py"
            properties = {"full_name": "someFunc", "language": "python", "call_names": []}

        payload = self._run(
            srv, "callers", "someFunc", {"CodeFunction": [_PyObj()]},
            filter_aware=True,
        )
        self.assertTrue(payload["success"])
        self.assertEqual(payload["count"], 0)
        self.assertNotIn("unsupported_for_language", payload)
        self.assertIn("note", payload)

    def test_empty_callers_unknown_language_no_marker(self):
        """Ambiguous: target row absent / no language → don't over-claim the
        boolean (fail toward not-unsupported), but keep the note."""
        import claude_mcp_servers.weaviate_mcp.server as srv
        payload = self._run(srv, "callers", "someFunc", {"CodeFunction": []})
        self.assertTrue(payload["success"])
        self.assertEqual(payload["count"], 0)
        self.assertNotIn("unsupported_for_language", payload)
        self.assertIn("note", payload)

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
