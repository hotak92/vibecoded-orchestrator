# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for ``vco_lib.project_config`` — Step 16 of v0.2.21.

Covers the resolver-client contract documented in
``.claude/context/plans/v0.2.21-resolver-design.md`` §2:

  * Hub-discovery chain (env > file > default) with 5-second TTL cache.
  * Happy-path ``resolve()`` decodes the full envelope into a frozen
    ProjectConfig dataclass.
  * Error mapping: 401/404/503/500 → the correct exception subclass.
  * ``resolve_field()`` round-trips through the hub's ``?key=`` filter.
  * Singleton-per-process ``requests.Session``.

The tests stub :class:`requests.Session.get` rather than spinning up a
real HTTP server — this keeps them fast (sub-second), deterministic,
and able to run without the launcher installed.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from vco_lib import project_config
from vco_lib.project_config import (
    EmbeddingModels,
    FieldNotFound,
    HubUnreachable,
    ProjectConfig,
    ProjectNotFound,
    ResolverError,
    ServiceMisconfigured,
    resolve,
    resolve_field,
)


# ─── Helpers ────────────────────────────────────────────────────────────


FULL_BODY: dict[str, Any] = {
    "project_id": "550e8400-e29b-41d4-a716-446655440000",
    "project_path": "/home/u/projects/myproject",
    "project_slug": "myproject",
    "project_display_name": "MyProject",
    "code_graph_project": "myproject",
    "code_graph_collection_prefix": "Myproject",
    "kg_collection": "Myproject_KnowledgeGraph",
    "shared_kg_collection": "VibecodedOrchestrator_KnowledgeGraph",
    "development_collection": "Myproject_Development",
    "active_embedding": "qwen3",
    "embedding_models": {
        "text": "qwen3-embedding:0.6b",
        "code": "CodeSage-Large-v2",
    },
    "kg_access_list": [
        "Myproject_KnowledgeGraph",
        "VibecodedOrchestrator_KnowledgeGraph",
    ],
    "codegraph_access_list": ["myproject"],
    "weaviate_url": "http://localhost:8081",
    "ollama_url": "http://localhost:11435",
    "grpc_port": 50052,
    "shared_kg_write_disabled": False,
}


def _make_response(
    status_code: int,
    body: Any = None,
    *,
    text: str | None = None,
) -> mock.Mock:
    """Build a fake ``requests.Response`` mock with the given status/body."""
    resp = mock.Mock()
    resp.status_code = status_code
    if text is not None:
        resp.text = text
        resp.json = mock.Mock(side_effect=ValueError("not json"))
    else:
        as_text = json.dumps(body) if body is not None else ""
        resp.text = as_text
        resp.json = mock.Mock(return_value=body) if body is not None else mock.Mock(
            side_effect=ValueError("empty")
        )
    return resp


class _ResolverTestBase(unittest.TestCase):
    """Shared setup: stub hub discovery, stub the requests session.

    Every test starts with a known port/token (no disk reads), a fresh
    session (so calls don't bleed across tests), and a fresh discovery
    cache (so env overrides are observed immediately).
    """

    def setUp(self) -> None:
        # Wipe any inherited caches so test order doesn't matter.
        project_config._test_clear_cache()
        project_config._test_reset_session()
        # Force env-only discovery so the test never touches the disk.
        self._env_patch = mock.patch.dict(
            os.environ,
            {"VCT_HUB_PORT": "9999", "VCT_HUB_TOKEN": "test-token-abc"},
        )
        self._env_patch.start()
        # Replace the singleton session with a Mock so we can assert calls.
        self.session = mock.Mock(spec=["get", "close", "mount"])
        self._session_patch = mock.patch.object(
            project_config, "_http_session", return_value=self.session
        )
        self._session_patch.start()

    def tearDown(self) -> None:
        self._session_patch.stop()
        self._env_patch.stop()
        project_config._test_clear_cache()
        project_config._test_reset_session()


# ─── Discovery chain ────────────────────────────────────────────────────


class DiscoverHubTest(unittest.TestCase):
    """The hub-discovery chain: env > file > default."""

    def setUp(self) -> None:
        project_config._test_clear_cache()

    def tearDown(self) -> None:
        project_config._test_clear_cache()

    def test_env_var_overrides_file(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"VCT_HUB_PORT": "8888", "VCT_HUB_TOKEN": "from-env"},
        ):
            port, token = project_config._discover_hub()
        self.assertEqual(port, 8888)
        self.assertEqual(token, "from-env")

    def test_file_fallback_when_env_absent(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "hub.port").write_text("7755\n", encoding="utf-8")
            (Path(td) / "hub.token").write_text("from-file\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("VCT_HUB_PORT", "VCT_HUB_TOKEN")}
            env["VCT_STATE_DIR"] = td
            with mock.patch.dict(os.environ, env, clear=True):
                port, token = project_config._discover_hub()
        self.assertEqual(port, 7755)
        self.assertEqual(token, "from-file")

    def test_port_defaults_when_no_port_env_and_no_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            # token file present (required) but no port file.
            (Path(td) / "hub.token").write_text("tok", encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("VCT_HUB_PORT", "VCT_HUB_TOKEN")}
            env["VCT_STATE_DIR"] = td
            with mock.patch.dict(os.environ, env, clear=True):
                port, token = project_config._discover_hub()
        self.assertEqual(port, project_config.DEFAULT_HUB_PORT)
        self.assertEqual(token, "tok")

    def test_missing_token_raises_hub_unreachable(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            env = {k: v for k, v in os.environ.items()
                   if k not in ("VCT_HUB_PORT", "VCT_HUB_TOKEN")}
            env["VCT_STATE_DIR"] = td
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(HubUnreachable):
                    project_config._discover_hub()

    def test_ttl_cache_repeated_calls_hit_disk_once(self) -> None:
        """Second call within TTL returns cached values without re-reading."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "hub.port").write_text("7700", encoding="utf-8")
            (Path(td) / "hub.token").write_text("t1", encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("VCT_HUB_PORT", "VCT_HUB_TOKEN")}
            env["VCT_STATE_DIR"] = td
            with mock.patch.dict(os.environ, env, clear=True):
                port1, token1 = project_config._discover_hub()
                # Mutate disk; cached call should NOT pick up the new token.
                (Path(td) / "hub.token").write_text("t2", encoding="utf-8")
                port2, token2 = project_config._discover_hub()
        self.assertEqual(token1, "t1")
        self.assertEqual(token2, "t1")  # cache hit; t2 ignored

    def test_clear_cache_invalidates_ttl(self) -> None:
        """Explicit cache clear lets the next call re-read the disk."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "hub.port").write_text("7700", encoding="utf-8")
            (Path(td) / "hub.token").write_text("t1", encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("VCT_HUB_PORT", "VCT_HUB_TOKEN")}
            env["VCT_STATE_DIR"] = td
            with mock.patch.dict(os.environ, env, clear=True):
                project_config._discover_hub()
                (Path(td) / "hub.token").write_text("t2", encoding="utf-8")
                project_config._test_clear_cache()
                _, token2 = project_config._discover_hub()
        self.assertEqual(token2, "t2")


# ─── Singleton session ──────────────────────────────────────────────────


class HttpSessionSingletonTest(unittest.TestCase):
    """`_http_session()` returns the same instance across calls."""

    def setUp(self) -> None:
        project_config._test_reset_session()

    def tearDown(self) -> None:
        project_config._test_reset_session()

    def test_singleton_returns_same_instance(self) -> None:
        s1 = project_config._http_session()
        s2 = project_config._http_session()
        self.assertIs(s1, s2)


# ─── Happy path ─────────────────────────────────────────────────────────


class ResolveHappyPathTest(_ResolverTestBase):
    """Hub returns the full envelope; resolver yields a ProjectConfig."""

    def test_resolve_with_uuid_skips_by_path_lookup(self) -> None:
        self.session.get.return_value = _make_response(200, FULL_BODY)
        cfg = resolve(FULL_BODY["project_id"])
        self.assertIsInstance(cfg, ProjectConfig)
        self.assertEqual(cfg.kg_collection, "Myproject_KnowledgeGraph")
        self.assertEqual(cfg.project_slug, "myproject")
        self.assertEqual(cfg.code_graph_project, "myproject")
        self.assertEqual(cfg.kg_access_list, (
            "Myproject_KnowledgeGraph",
            "VibecodedOrchestrator_KnowledgeGraph",
        ))
        self.assertEqual(cfg.codegraph_access_list, ("myproject",))
        self.assertIsInstance(cfg.embedding_models, EmbeddingModels)
        self.assertEqual(cfg.embedding_models.text, "qwen3-embedding:0.6b")
        self.assertEqual(cfg.grpc_port, 50052)
        self.assertIs(cfg.shared_kg_write_disabled, False)
        # UUID input means we only made the /config GET, not by-path.
        self.assertEqual(self.session.get.call_count, 1)
        url = self.session.get.call_args.args[0]
        self.assertIn(f"/projects/{FULL_BODY['project_id']}/config", url)

    def test_resolve_with_path_calls_by_path_first(self) -> None:
        # First GET returns {"id": ...}, second GET returns full body.
        self.session.get.side_effect = [
            _make_response(200, {"id": FULL_BODY["project_id"]}),
            _make_response(200, FULL_BODY),
        ]
        cfg = resolve("/some/path/myproject")
        self.assertEqual(cfg.project_id, FULL_BODY["project_id"])
        self.assertEqual(self.session.get.call_count, 2)
        first_url = self.session.get.call_args_list[0].args[0]
        self.assertIn("/projects/by-path", first_url)

    def test_frozen_dataclass_immutable(self) -> None:
        self.session.get.return_value = _make_response(200, FULL_BODY)
        cfg = resolve(FULL_BODY["project_id"])
        with self.assertRaises(Exception):  # FrozenInstanceError
            cfg.kg_collection = "mutated"  # type: ignore[misc]

    def test_shared_kg_collection_empty_string_passthrough(self) -> None:
        body = {**FULL_BODY, "shared_kg_collection": "", "development_collection": ""}
        self.session.get.return_value = _make_response(200, body)
        cfg = resolve(FULL_BODY["project_id"])
        self.assertEqual(cfg.shared_kg_collection, "")
        self.assertEqual(cfg.development_collection, "")


# ─── Error mapping ──────────────────────────────────────────────────────


class ResolveErrorMappingTest(_ResolverTestBase):
    """Each HTTP status maps to the right exception subclass."""

    def test_404_project_not_found(self) -> None:
        self.session.get.return_value = _make_response(
            404,
            {"error": {"code": "project_not_found", "message": "missing"}},
        )
        with self.assertRaises(ProjectNotFound):
            resolve(FULL_BODY["project_id"])

    def test_503_service_misconfigured(self) -> None:
        self.session.get.return_value = _make_response(
            503,
            {"error": {"code": "service_misconfigured", "message": "backfill"}},
        )
        with self.assertRaises(ServiceMisconfigured):
            resolve(FULL_BODY["project_id"])

    def test_401_maps_to_hub_unreachable(self) -> None:
        self.session.get.return_value = _make_response(
            401,
            {"error": {"code": "unauthorized", "message": "bad token"}},
        )
        with self.assertRaises(HubUnreachable):
            resolve(FULL_BODY["project_id"])

    def test_500_maps_to_hub_unreachable(self) -> None:
        self.session.get.return_value = _make_response(
            500,
            {"error": {"code": "internal_error", "message": "db boom"}},
        )
        with self.assertRaises(HubUnreachable):
            resolve(FULL_BODY["project_id"])

    def test_400_maps_to_resolver_error(self) -> None:
        self.session.get.return_value = _make_response(
            400,
            {"error": {"code": "invalid_request", "message": "bad shape"}},
        )
        with self.assertRaises(ResolverError):
            resolve(FULL_BODY["project_id"])

    def test_connection_refused_raises_hub_unreachable(self) -> None:
        import requests as _r

        self.session.get.side_effect = _r.ConnectionError("refused")
        with self.assertRaises(HubUnreachable):
            resolve(FULL_BODY["project_id"])

    def test_timeout_raises_hub_unreachable(self) -> None:
        import requests as _r

        self.session.get.side_effect = _r.Timeout("slow")
        with self.assertRaises(HubUnreachable):
            resolve(FULL_BODY["project_id"])

    def test_malformed_200_body_raises_hub_unreachable(self) -> None:
        # 200 OK but missing a required field — defensive guard.
        broken = {k: v for k, v in FULL_BODY.items() if k != "kg_collection"}
        self.session.get.return_value = _make_response(200, broken)
        with self.assertRaises(HubUnreachable):
            resolve(FULL_BODY["project_id"])


# ─── resolve_field() ────────────────────────────────────────────────────


class ResolveFieldTest(_ResolverTestBase):
    """The single-field fast path uses the hub's ?key= filter."""

    def test_scalar_field_returned_unwrapped(self) -> None:
        self.session.get.return_value = _make_response(
            200, {"kg_collection": "Myproject_KnowledgeGraph"}
        )
        val = resolve_field(FULL_BODY["project_id"], "kg_collection")
        self.assertEqual(val, "Myproject_KnowledgeGraph")
        # Confirm the ?key= filter was actually applied.
        kwargs = self.session.get.call_args.kwargs
        self.assertEqual(kwargs.get("params"), {"key": "kg_collection"})

    def test_nested_object_returned_intact(self) -> None:
        nested = {"text": "qwen3-embedding:0.6b", "code": "CodeSage-Large-v2"}
        self.session.get.return_value = _make_response(
            200, {"embedding_models": nested}
        )
        val = resolve_field(FULL_BODY["project_id"], "embedding_models")
        self.assertEqual(val, nested)

    def test_list_field_returned_as_list(self) -> None:
        items = ["A_KG", "B_KG"]
        self.session.get.return_value = _make_response(
            200, {"kg_access_list": items}
        )
        val = resolve_field(FULL_BODY["project_id"], "kg_access_list")
        self.assertEqual(val, items)

    def test_field_not_found_maps_to_FieldNotFound(self) -> None:
        self.session.get.return_value = _make_response(
            404,
            {"error": {"code": "field_not_found", "message": "no such field"}},
        )
        with self.assertRaises(FieldNotFound):
            resolve_field(FULL_BODY["project_id"], "no_such_field")

    def test_project_not_found_routes_to_ProjectNotFound(self) -> None:
        self.session.get.return_value = _make_response(
            404,
            {"error": {"code": "project_not_found", "message": "missing"}},
        )
        with self.assertRaises(ProjectNotFound):
            resolve_field(FULL_BODY["project_id"], "kg_collection")

    def test_empty_key_raises_resolver_error(self) -> None:
        with self.assertRaises(ResolverError):
            resolve_field(FULL_BODY["project_id"], "")
        # No HTTP call should have been made.
        self.session.get.assert_not_called()


# ─── Auth header propagation ────────────────────────────────────────────


class AuthHeaderTest(_ResolverTestBase):
    """Every request carries Authorization: Bearer <token>."""

    def test_bearer_token_header_set_on_resolve(self) -> None:
        self.session.get.return_value = _make_response(200, FULL_BODY)
        resolve(FULL_BODY["project_id"])
        headers = self.session.get.call_args.kwargs.get("headers", {})
        self.assertEqual(headers.get("Authorization"), "Bearer test-token-abc")


if __name__ == "__main__":
    unittest.main()
