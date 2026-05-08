# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Integration tests for ``query_code_graph.py`` honoring the access
matrix (``VCT_CODE_GRAPH_ACCESS_LIST``).

This is the CLI hook-driven path:
    .claude/scripts/code-graph-query → query_code_graph.py search "..."

Pre-2026-05-08 (P1-D fix) the script queried only
``<self_prefix>_<base>``. After the fix it fans out across self +
every peer in VCT_CODE_GRAPH_ACCESS_LIST.

Tests mock Weaviate at the ``client.collections.get`` boundary so no
real Weaviate is needed. The mock records which collections were
requested; the test asserts the fan-out matches the access matrix.
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
        "PROJECT_NAME",
        "CODE_GRAPH_PROJECT",
    ):
        os.environ.pop(k, None)


def _fresh_query_code_graph(env_overrides: dict[str, str]):
    """Reload query_code_graph with a fresh env snapshot."""
    for mod in list(sys.modules):
        if mod in ("query_code_graph", "kg_access"):
            del sys.modules[mod]
    for k, v in env_overrides.items():
        os.environ[k] = v
    return importlib.import_module("query_code_graph")


class _FakeMetadata:
    def __init__(self, distance: float = 0.5):
        self.distance = distance


class _FakeObj:
    def __init__(self, full_name: str, distance: float = 0.5):
        self.uuid = f"uuid-{full_name}"
        self.properties = {
            "full_name": full_name,
            "name": full_name.split(".")[-1],
            "signature": f"def {full_name}(...)",
            "doc": f"Doc for {full_name}",
            "start_line": 1,
            "end_line": 10,
        }
        self.metadata = _FakeMetadata(distance=distance)


class _FakeQuery:
    def __init__(self, objects):
        self._objects = objects
        self.last_kwargs: dict | None = None

    def near_vector(self, **kwargs):
        self.last_kwargs = kwargs
        return mock.Mock(objects=self._objects)


class _FakeCollection:
    def __init__(self, name: str, objects: list | None = None):
        self.name = name
        self.query = _FakeQuery(objects or [])


class _FakeCollections:
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


class QueryCodeGraphAccessListTests(unittest.TestCase):
    """``CodeGraphQuery.search_by_concept`` fans out across the code
    graph access matrix."""

    def setUp(self) -> None:
        _clear_env()

    def test_no_access_list_queries_self_only(self) -> None:
        """Negative direction: with no VCT_CODE_GRAPH_ACCESS_LIST and a
        single project, the CLI queries only the self collection."""
        qcg = _fresh_query_code_graph({})
        contents = {"Alpha_CodeFunction": [_FakeObj("alpha.foo")]}
        client = _FakeClient(contents)
        querier = qcg.CodeGraphQuery(project="Alpha")
        querier.client = client
        with mock.patch.object(qcg, "generate_code_embedding", return_value=[0.0] * 2048):
            querier.search_by_concept("foo", collection="CodeFunction", limit=5)
        self.assertEqual(client.collections.requested, ["Alpha_CodeFunction"])

    def test_access_list_fans_out_across_peers(self) -> None:
        """Positive direction: with VCT_CODE_GRAPH_ACCESS_LIST=Beta,Gamma
        the CLI queries Alpha + Beta + Gamma collections in env-order
        for the requested base."""
        qcg = _fresh_query_code_graph({
            "VCT_CODE_GRAPH_ACCESS_LIST": "Beta,Gamma",
        })
        contents = {
            "Alpha_CodeFunction": [_FakeObj("alpha.foo", distance=0.1)],
            "Beta_CodeFunction": [_FakeObj("beta.foo", distance=0.2)],
            "Gamma_CodeFunction": [_FakeObj("gamma.foo", distance=0.3)],
        }
        client = _FakeClient(contents)
        querier = qcg.CodeGraphQuery(project="Alpha")
        querier.client = client
        with mock.patch.object(qcg, "generate_code_embedding", return_value=[0.0] * 2048):
            querier.search_by_concept("foo", collection="CodeFunction", limit=5)
        self.assertEqual(
            client.collections.requested,
            [
                "Alpha_CodeFunction",
                "Beta_CodeFunction",
                "Gamma_CodeFunction",
            ],
            "Expected fan-out across self → peers in env-order",
        )

    def test_no_project_uses_bare_collection(self) -> None:
        """When ``self.project`` is None (cross-tenant search), the CLI
        uses the bare collection name and does NOT consult the access
        list. This mirrors the MCP server's behaviour: empty project
        means "search everything", not "search self + peers"."""
        qcg = _fresh_query_code_graph({
            "VCT_CODE_GRAPH_ACCESS_LIST": "Beta,Gamma",
        })
        contents = {"CodeFunction": [_FakeObj("foo")]}
        client = _FakeClient(contents)
        querier = qcg.CodeGraphQuery(project=None)
        querier.client = client
        with mock.patch.object(qcg, "generate_code_embedding", return_value=[0.0] * 2048):
            querier.search_by_concept("foo", collection="CodeFunction", limit=5)
        self.assertEqual(client.collections.requested, ["CodeFunction"])

    def test_results_merged_and_sorted_by_distance(self) -> None:
        """Multi-collection fan-out merges results across collections
        and sorts by distance ascending. With Beta returning a closer
        hit than Alpha, the Beta result must appear first."""
        qcg = _fresh_query_code_graph({
            "VCT_CODE_GRAPH_ACCESS_LIST": "Beta",
        })
        contents = {
            "Alpha_CodeFunction": [_FakeObj("alpha.foo", distance=0.5)],
            "Beta_CodeFunction": [_FakeObj("beta.foo", distance=0.1)],
        }
        client = _FakeClient(contents)
        querier = qcg.CodeGraphQuery(project="Alpha")
        querier.client = client

        captured: list[str] = []

        def _spy_print(line: str = "", *args, **kwargs):
            captured.append(line)

        with mock.patch.object(qcg, "generate_code_embedding", return_value=[0.0] * 2048), \
             mock.patch("builtins.print", side_effect=_spy_print):
            querier.search_by_concept("foo", collection="CodeFunction", limit=5)

        # Beta result (lower distance) must come before Alpha in the
        # output. We grep for the result lines (start with "1." / "2.").
        result_lines = [ln for ln in captured if ln.startswith(("1. ", "2. "))]
        self.assertGreaterEqual(len(result_lines), 2, f"too few results: {result_lines}")
        self.assertIn("beta.foo", result_lines[0])
        self.assertIn("alpha.foo", result_lines[1])

    def test_peer_collection_unavailable_does_not_break_self(self) -> None:
        """Peer code-graph collection that doesn't exist must not break
        the CLI — the failure should be silently swallowed and self
        results should still come back."""
        qcg = _fresh_query_code_graph({
            "VCT_CODE_GRAPH_ACCESS_LIST": "MissingPeer",
        })

        class _PartialClient:
            def __init__(self):
                self.requested: list[str] = []
                self.collections = self

            def get(self, name: str):
                self.requested.append(name)
                if name.startswith("MissingPeer_"):
                    raise RuntimeError("collection not found")
                return _FakeCollection(name, [_FakeObj("alpha.foo")])

            def close(self):
                pass

        client = _PartialClient()
        querier = qcg.CodeGraphQuery(project="Alpha")
        querier.client = client
        with mock.patch.object(qcg, "generate_code_embedding", return_value=[0.0] * 2048):
            # Must not raise
            querier.search_by_concept("foo", collection="CodeFunction", limit=5)
        self.assertEqual(
            client.requested,
            ["Alpha_CodeFunction", "MissingPeer_CodeFunction"],
        )


if __name__ == "__main__":
    unittest.main()
