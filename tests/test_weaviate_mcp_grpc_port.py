# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for B8: WEAVIATE_GRPC_PORT / GRPC_PORT dual-key read in weaviate_mcp.

weaviate_mcp/server.py must:
  - Prefer WEAVIATE_GRPC_PORT when set.
  - Fall back to GRPC_PORT when only that is set.
  - Use default 50052 when neither is set.
  - When both are set, WEAVIATE_GRPC_PORT wins.
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _reload_server_grpc_port(env: dict) -> int:
    """Reload weaviate_mcp.server with the given env and return GRPC_PORT."""
    with mock.patch.dict(os.environ, env, clear=False):
        # We need to re-evaluate the module-level constant. Importlib reload
        # re-executes the module body, so GRPC_PORT is recomputed.
        import claude_mcp_servers.weaviate_mcp.server as server_mod  # type: ignore
        importlib.reload(server_mod)
        return server_mod.GRPC_PORT


class WeaviateMcpGrpcPortTests(unittest.TestCase):
    """B8: weaviate_mcp reads both GRPC_PORT keys; WEAVIATE_GRPC_PORT wins."""

    def _with_only_weaviate_grpc_port(self):
        env = {k: "" for k in ("GRPC_PORT", "WEAVIATE_GRPC_PORT")}
        env["WEAVIATE_GRPC_PORT"] = "51000"
        return env

    def _with_only_grpc_port(self):
        env = {k: "" for k in ("GRPC_PORT", "WEAVIATE_GRPC_PORT")}
        env["GRPC_PORT"] = "52000"
        return env

    def _with_both(self):
        return {"WEAVIATE_GRPC_PORT": "51000", "GRPC_PORT": "52000"}

    def _with_neither(self):
        return {"GRPC_PORT": "", "WEAVIATE_GRPC_PORT": ""}

    def test_weaviate_grpc_port_wins_when_set(self):
        port = _reload_server_grpc_port(self._with_only_weaviate_grpc_port())
        self.assertEqual(port, 51000)

    def test_grpc_port_used_as_fallback(self):
        port = _reload_server_grpc_port(self._with_only_grpc_port())
        self.assertEqual(port, 52000)

    def test_weaviate_grpc_port_wins_when_both_set(self):
        port = _reload_server_grpc_port(self._with_both())
        self.assertEqual(port, 51000)

    def test_default_when_neither_set(self):
        port = _reload_server_grpc_port(self._with_neither())
        self.assertEqual(port, 50052)


if __name__ == "__main__":
    unittest.main()
