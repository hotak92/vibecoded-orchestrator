# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-AA (v0.2.52) — RL Reranker server port propagation chain.

Pre-V52-AA the launcher allocated a per-project RL container port and
wrote it to ``module_ports(project_id, "vct-rl-reranker", port)`` but
NEVER propagated it to the MCP subprocess. The launcher deliberately
excluded ``RL_SERVER_PORT`` from ``CANONICAL_INSTALL_ENV_KEYS`` (i.e.
``.claude/settings.json env`` + ``.claude/env``) AND from the
``~/.claude.json mcpServers.*.env`` allowlist because Claude Code's
env-precedence rules made every viable surface unsafe for per-project
values. The result: the MCP's ``_get_rl_client()`` always returned a
client in "disabled mode" because ``_resolve_base_url()`` only ever
read ``os.environ``, which was empty for the MCP subprocess.

V52-AA closes the gap by exposing the allocated port through the
hub-resolved ``ProjectConfig`` (the canonical channel for
per-project values) and having ``_get_rl_client`` read it as a
fallback when env vars are unset. Env vars still WIN when set,
preserving the existing override path for tests + dev users.

These tests pin THREE contracts:

1. **Python ``ProjectConfig`` parser** decodes ``rl_server_port`` from
   the hub JSON correctly:
   - Numeric value → coerced to int.
   - JSON null → None.
   - Missing key → None (pre-V52-AA hubs paired with V52-AA+ clients).
   - Malformed (negative / oversize / string) → None (defense-in-depth).
2. **``_get_rl_client``** uses the hub-resolved port as a fallback
   when env vars are unset; the resulting ``RLClient`` reports
   ``enabled = True``.
3. **Env precedence** — when ``RL_SERVER_URL`` or ``RL_SERVER_PORT`` is
   set, the env value WINS over the hub-resolved fallback.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.project_config import _coerce_optional_port, _from_hub_body  # noqa: E402
import claude_mcp_servers.weaviate_mcp.server as srv  # noqa: E402


def _minimal_hub_body(extra: dict | None = None) -> dict:
    """A valid ProjectConfigResponse body sufficient for _from_hub_body.

    Mirrors the existing seed_full_project test fixture in
    config_api.rs. ``extra`` overlays values onto the base envelope so
    individual tests can flip just the field under test.
    """
    body = {
        "schema_version": 1,
        "project_id": "p-rl-test",
        "project_path": "/tmp/test-rl-port",
        "project_slug": "rl-test",
        "project_display_name": "RL Test Project",
        "code_graph_project": "rl-test",
        "code_graph_collection_prefix": "RlTest",
        "kg_collection": "RlTest_KnowledgeGraph",
        "shared_kg_collection": "VibeCodedOrchestrator_KnowledgeGraph",
        "development_collection": "RlTest_Development",
        "diagrams_collection": "RlTest_Diagrams",
        "active_embedding": "qwen3",
        "embedding_models": {
            "text": "qwen3-embedding:0.6b",
            "code": "CodeSage-Large-v2",
        },
        "kg_access_list": [],
        "codegraph_access_list": [],
        "diagrams_access_list": [],
        "weaviate_url": "http://localhost:8081",
        "ollama_url": "http://localhost:11435",
        "grpc_port": 50052,
        "shared_kg_write_disabled": False,
        "shared_kg_read_disabled": False,
        "rl_use_global": False,
        "rl_online_training_disabled": False,
        "rl_global_training_source_flag": False,
        "rl_reranker_enabled_for_project": True,
        "claude_session_dir": "/home/user/.claude/projects/-tmp-test-rl-port",
        "retrieval_tuning": {
            "code_graph_score_floor": 0.45,
            "kg_tier_min": 0.42,
            "kg_tier_single_chunk": 0.55,
            "kg_tier_three_chunks": 0.65,
            "kg_tier_full": 0.75,
        },
        "code_graph_extra_paths": [],
    }
    if extra:
        body.update(extra)
    return body


class CoerceOptionalPortTest(unittest.TestCase):
    """``_coerce_optional_port`` defensively decodes the JSON value.

    The hub serialises ``Option<u16>`` as JSON null or a non-negative
    integer. Older hubs may omit the field entirely (parsed to None
    by the default in ``ProjectConfig.rl_server_port``). Defense-in-
    depth: a future schema change emitting a string or out-of-range
    value must not crash old clients.
    """

    def test_none_returns_none(self):
        self.assertIsNone(_coerce_optional_port(None))

    def test_valid_port_returns_int(self):
        self.assertEqual(_coerce_optional_port(11442), 11442)
        self.assertEqual(_coerce_optional_port(8090), 8090)

    def test_numeric_string_coerced(self):
        # Defense-in-depth: a future hub schema sending strings
        # shouldn't crash old clients.
        self.assertEqual(_coerce_optional_port("11442"), 11442)

    def test_negative_returns_none(self):
        self.assertIsNone(_coerce_optional_port(-1))
        self.assertIsNone(_coerce_optional_port(0))

    def test_oversize_returns_none(self):
        self.assertIsNone(_coerce_optional_port(65536))
        self.assertIsNone(_coerce_optional_port(1_000_000))

    def test_malformed_returns_none(self):
        self.assertIsNone(_coerce_optional_port("not-a-port"))
        self.assertIsNone(_coerce_optional_port([11442]))
        self.assertIsNone(_coerce_optional_port({"port": 11442}))


class FromHubBodyRlServerPortTest(unittest.TestCase):
    """``_from_hub_body`` populates ``ProjectConfig.rl_server_port``."""

    def test_allocated_port_round_trips(self):
        """Hub returns a numeric value → ProjectConfig.rl_server_port=int."""
        body = _minimal_hub_body({"rl_server_port": 11442})
        cfg = _from_hub_body(body)
        self.assertEqual(cfg.rl_server_port, 11442)

    def test_null_decodes_to_none(self):
        """Hub returns JSON null → ProjectConfig.rl_server_port=None."""
        body = _minimal_hub_body({"rl_server_port": None})
        cfg = _from_hub_body(body)
        self.assertIsNone(cfg.rl_server_port)

    def test_missing_key_back_fills_to_none(self):
        """Pre-V52-AA hubs omit the field; new client back-fills None."""
        body = _minimal_hub_body()  # no rl_server_port key at all
        self.assertNotIn("rl_server_port", body)
        cfg = _from_hub_body(body)
        self.assertIsNone(cfg.rl_server_port)

    def test_other_fields_unaffected(self):
        """Adding rl_server_port must not perturb the rest of the parse."""
        body = _minimal_hub_body({"rl_server_port": 8090})
        cfg = _from_hub_body(body)
        # Sanity-check a handful of unrelated fields to ensure we
        # didn't accidentally consume them.
        self.assertEqual(cfg.project_id, "p-rl-test")
        self.assertEqual(cfg.kg_collection, "RlTest_KnowledgeGraph")
        self.assertEqual(cfg.active_embedding, "qwen3")
        self.assertTrue(cfg.rl_reranker_enabled_for_project)
        self.assertEqual(cfg.rl_server_port, 8090)


class GetRlClientHubFallbackTest(unittest.TestCase):
    """``_get_rl_client`` uses hub-resolved ``rl_server_port`` as a
    fallback when env vars are unset.

    Pre-V52-AA: env unset → ``RLClient(base_url=None)`` → disabled mode.
    V52-AA: env unset AND hub returns a port → ``RLClient(base_url=
    "http://127.0.0.1:<port>")`` → enabled mode. Env override path
    remains intact for tests + dev users.
    """

    def setUp(self):
        # Drop the per-process client cache so each test gets a fresh
        # RLClient (the cache key is (embedding, project_id); tests
        # use distinct project IDs to avoid collision but explicit is
        # safer than implicit).
        srv._rl_client_instances.clear()
        # Snapshot env so we can mutate freely and restore in tearDown.
        self._env_snapshot = {
            k: __import__("os").environ.pop(k, None)
            for k in ("RL_SERVER_URL", "RL_SERVER_PORT")
        }

    def tearDown(self):
        import os
        for k, v in self._env_snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        srv._rl_client_instances.clear()

    def test_env_unset_uses_hub_port(self):
        """Env unset + hub-resolved port → client.enabled = True."""
        fake_cfg = SimpleNamespace(
            project_id="p-hub-port",
            rl_server_port=11442,
        )
        with patch.object(srv, "_try_resolve_project_config", return_value=fake_cfg):
            client = srv._get_rl_client()
        self.assertIsNotNone(client)
        self.assertTrue(
            client.enabled,
            "Hub-resolved port must enable the client when env unset",
        )
        self.assertEqual(client.base_url, "http://127.0.0.1:11442")

    def test_env_unset_no_hub_port_stays_disabled(self):
        """Env unset + hub returns None → client stays in disabled mode."""
        fake_cfg = SimpleNamespace(
            project_id="p-hub-noport",
            rl_server_port=None,
        )
        with patch.object(srv, "_try_resolve_project_config", return_value=fake_cfg):
            client = srv._get_rl_client()
        self.assertIsNotNone(client)
        self.assertFalse(
            client.enabled,
            "Disabled mode must persist when neither env nor hub provide a port",
        )

    def test_env_url_wins_over_hub_port(self):
        """RL_SERVER_URL set → hub port ignored (env override preserved)."""
        import os
        os.environ["RL_SERVER_URL"] = "http://example.com:9999"
        fake_cfg = SimpleNamespace(
            project_id="p-env-wins-url",
            rl_server_port=11442,  # hub says 11442, but env URL wins
        )
        with patch.object(srv, "_try_resolve_project_config", return_value=fake_cfg):
            client = srv._get_rl_client()
        self.assertIsNotNone(client)
        self.assertEqual(
            client.base_url,
            "http://example.com:9999",
            "RL_SERVER_URL env must override hub-resolved fallback",
        )

    def test_env_port_wins_over_hub_port(self):
        """RL_SERVER_PORT set → composed against 127.0.0.1, hub ignored."""
        import os
        os.environ["RL_SERVER_PORT"] = "8090"
        fake_cfg = SimpleNamespace(
            project_id="p-env-wins-port",
            rl_server_port=11442,  # hub says 11442, but env port wins
        )
        with patch.object(srv, "_try_resolve_project_config", return_value=fake_cfg):
            client = srv._get_rl_client()
        self.assertIsNotNone(client)
        self.assertEqual(
            client.base_url,
            "http://127.0.0.1:8090",
            "RL_SERVER_PORT env must override hub-resolved fallback",
        )

    def test_hub_resolver_exception_falls_through_to_disabled(self):
        """Hub resolver raises → client falls through to env (None) → disabled."""
        with patch.object(
            srv,
            "_try_resolve_project_config",
            side_effect=RuntimeError("hub down"),
        ):
            client = srv._get_rl_client()
        self.assertIsNotNone(client)
        self.assertFalse(
            client.enabled,
            "Hub resolver exception must not crash _get_rl_client; "
            "should fall through to env (here empty) → disabled mode",
        )


if __name__ == "__main__":
    unittest.main()
