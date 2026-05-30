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
    RESOLVER_PROTOCOL_VERSION,
    ResolverError,
    ServiceMisconfigured,
    claude_session_dir_for,
    resolve,
    resolve_field,
)


# ─── Helpers ────────────────────────────────────────────────────────────


FULL_BODY: dict[str, Any] = {
    "schema_version": 1,
    "project_id": "550e8400-e29b-41d4-a716-446655440000",
    "project_path": "/home/u/projects/myproject",
    "project_slug": "myproject",
    "project_display_name": "MyProject",
    "code_graph_project": "myproject",
    "code_graph_collection_prefix": "Myproject",
    "kg_collection": "Myproject_KnowledgeGraph",
    "shared_kg_collection": "VibeCodedOrchestrator_KnowledgeGraph",
    "development_collection": "Myproject_Development",
    "active_embedding": "qwen3",
    "embedding_models": {
        "text": "qwen3-embedding:0.6b",
        "code": "CodeSage-Large-v2",
    },
    "kg_access_list": [
        "Myproject_KnowledgeGraph",
        "VibeCodedOrchestrator_KnowledgeGraph",
    ],
    "codegraph_access_list": ["myproject"],
    "weaviate_url": "http://localhost:8081",
    "ollama_url": "http://localhost:11435",
    "grpc_port": 50052,
    "shared_kg_write_disabled": False,
    "retrieval_tuning": {
        "code_graph_score_floor": 0.35,
        "kg_tier_min": 0.42,
        "kg_tier_single_chunk": 0.55,
        "kg_tier_three_chunks": 0.65,
        "kg_tier_full": 0.75,
    },
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
            "VibeCodedOrchestrator_KnowledgeGraph",
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
        # Both attempts (initial + retry) 401 → final HubUnreachable.
        self.session.get.return_value = _make_response(
            401,
            {"error": {"code": "unauthorized", "message": "bad token"}},
        )
        with self.assertRaises(HubUnreachable):
            resolve(FULL_BODY["project_id"])
        # The retry path SHOULD have called twice (initial + retry after
        # cache invalidation). Both 401, so the user-visible error is
        # HubUnreachable. Asserts the cache-invalidation+retry path
        # actually fired (regression guard for v0.2.21 mid-session-25
        # Reviewer-A MEDIUM finding).
        self.assertGreaterEqual(self.session.get.call_count, 2)

    def test_401_then_200_recovers_via_retry(self) -> None:
        # v0.2.21 Step 25 Reviewer-A MEDIUM finding fix: when the in-
        # process discovery cache holds a stale token from a hub
        # restart, the first GET 401s. The resolver invalidates the
        # cache, re-reads hub.port + hub.token from disk, and re-issues
        # the request. On the retry it gets 200 → returns a ProjectConfig
        # rather than raising HubUnreachable.
        self.session.get.side_effect = [
            _make_response(
                401,
                {"error": {"code": "unauthorized", "message": "stale token"}},
            ),
            _make_response(200, FULL_BODY),
        ]
        cfg = resolve(FULL_BODY["project_id"])
        self.assertIsInstance(cfg, ProjectConfig)
        self.assertEqual(cfg.project_slug, "myproject")
        # Both calls should have fired (initial 401 + retry 200).
        self.assertEqual(self.session.get.call_count, 2)

    def test_401_retry_invalidates_discovery_cache(self) -> None:
        # The retry path MUST call _invalidate_discovery_cache() between
        # the first 401 and the retry attempt. Spy on the function to
        # confirm — proves the cache-invalidation step actually fires
        # rather than the retry silently re-using the stale cache (which
        # would just 401 again for the same reason).
        with mock.patch.object(
            project_config,
            "_invalidate_discovery_cache",
            wraps=project_config._invalidate_discovery_cache,
        ) as spy:
            self.session.get.return_value = _make_response(
                401,
                {"error": {"code": "unauthorized", "message": "bad"}},
            )
            with self.assertRaises(HubUnreachable):
                resolve(FULL_BODY["project_id"])
            self.assertEqual(
                spy.call_count, 1,
                "expected exactly one cache-invalidation between initial 401 + retry",
            )

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


# ─── schema_version (v0.2.22 Item #2) ───────────────────────────────────


class SchemaVersionTest(_ResolverTestBase):
    """Hub `schema_version` is parsed; higher-than-known emits one warning.

    Forward-compat hardening (v0.2.22 Item #2). The client pins its
    own knowledge at ``RESOLVER_PROTOCOL_VERSION = 1``; when the hub
    reports a higher value the client emits ONE stderr line per
    distinct hub version per process and still returns the parsed
    body. Pre-v0.2.22 hubs omit the field entirely; the client back-
    fills with its own constant so callers see a stable int.
    """

    def test_matching_version_yields_no_warning(self) -> None:
        # Body says schema_version=1 (== client's constant). No warning.
        captured = []
        self.session.get.return_value = _make_response(200, FULL_BODY)
        with mock.patch.object(
            project_config.sys.stderr, "write",
            side_effect=lambda s: captured.append(s),
        ):
            cfg = resolve(FULL_BODY["project_id"])
        self.assertEqual(cfg.schema_version, 1)
        self.assertEqual(captured, [],
            f"unexpected stderr write for matching version: {captured!r}")

    def test_higher_version_emits_warning_once_per_process(self) -> None:
        # Body says schema_version=2 (> RESOLVER_PROTOCOL_VERSION=1).
        # First resolve() must warn; second resolve() must NOT warn
        # again (dedup is per-process per-version).
        body_v2 = {**FULL_BODY, "schema_version": 2}
        self.session.get.return_value = _make_response(200, body_v2)

        captured = []
        with mock.patch.object(
            project_config.sys.stderr, "write",
            side_effect=lambda s: captured.append(s),
        ):
            cfg1 = resolve(FULL_BODY["project_id"])
            cfg2 = resolve(FULL_BODY["project_id"])

        self.assertEqual(cfg1.schema_version, 2)
        self.assertEqual(cfg2.schema_version, 2)
        # Exactly one stderr line, mentioning both versions.
        self.assertEqual(len(captured), 1,
            f"expected exactly one warning, got {len(captured)}: {captured!r}")
        self.assertIn("schema_version=2", captured[0])
        self.assertIn(f"version {RESOLVER_PROTOCOL_VERSION}", captured[0])

    def test_higher_version_warns_again_for_distinct_value(self) -> None:
        # Two different higher versions = two distinct warnings (per
        # the dedup-on-int contract; a "version 2" warning shouldn't
        # mask a later "version 3" warning).
        body_v2 = {**FULL_BODY, "schema_version": 2}
        body_v3 = {**FULL_BODY, "schema_version": 3}
        self.session.get.side_effect = [
            _make_response(200, body_v2),
            _make_response(200, body_v3),
        ]

        captured = []
        with mock.patch.object(
            project_config.sys.stderr, "write",
            side_effect=lambda s: captured.append(s),
        ):
            resolve(FULL_BODY["project_id"])
            resolve(FULL_BODY["project_id"])
        self.assertEqual(len(captured), 2)
        self.assertIn("schema_version=2", captured[0])
        self.assertIn("schema_version=3", captured[1])

    def test_missing_schema_version_back_fills_with_client_default(self) -> None:
        # Pre-v0.2.22 hub: the field is absent. Client back-fills with
        # RESOLVER_PROTOCOL_VERSION rather than raising.
        body_no_sv = {k: v for k, v in FULL_BODY.items() if k != "schema_version"}
        self.session.get.return_value = _make_response(200, body_no_sv)

        captured = []
        with mock.patch.object(
            project_config.sys.stderr, "write",
            side_effect=lambda s: captured.append(s),
        ):
            cfg = resolve(FULL_BODY["project_id"])
        self.assertEqual(cfg.schema_version, RESOLVER_PROTOCOL_VERSION)
        # Back-fill is silent — only a HIGHER hub version warns.
        self.assertEqual(captured, [])

    def test_lower_version_does_not_warn(self) -> None:
        # A hub reporting an OLDER version than the client knows about
        # is unusual but valid (client upgraded ahead of hub). No
        # warning — the client is the source of truth for "what should
        # we know about", a lower hub version just means a smaller
        # field set which we already handle via .get(...) defaults.
        body_v0 = {**FULL_BODY, "schema_version": 0}
        self.session.get.return_value = _make_response(200, body_v0)

        captured = []
        with mock.patch.object(
            project_config.sys.stderr, "write",
            side_effect=lambda s: captured.append(s),
        ):
            cfg = resolve(FULL_BODY["project_id"])
        self.assertEqual(cfg.schema_version, 0)
        self.assertEqual(captured, [])

    def test_non_integer_schema_version_maps_to_hub_unreachable(self) -> None:
        # Garbled wire body: schema_version is a string that doesn't
        # parse as int → defensive HubUnreachable rather than silent
        # back-fill (a malformed envelope hints the hub is broken).
        body_bad = {**FULL_BODY, "schema_version": "not-a-number"}
        self.session.get.return_value = _make_response(200, body_bad)
        with self.assertRaises(HubUnreachable):
            resolve(FULL_BODY["project_id"])


# ─── Auth header propagation ────────────────────────────────────────────


class AuthHeaderTest(_ResolverTestBase):
    """Every request carries Authorization: Bearer <token>."""

    def test_bearer_token_header_set_on_resolve(self) -> None:
        self.session.get.return_value = _make_response(200, FULL_BODY)
        resolve(FULL_BODY["project_id"])
        headers = self.session.get.call_args.kwargs.get("headers", {})
        self.assertEqual(headers.get("Authorization"), "Bearer test-token-abc")


# ─── v0.2.31: claude_session_dir_for slug rule + resolver propagation ───


class ClaudeSessionDirSlugTest(unittest.TestCase):
    """Pin Claude Code's session-jsonl directory slug rule.

    The citation-monitor bug at ``claude_mcp_servers/weaviate_mcp/
    server.py`` was an incomplete copy of this rule (only ``/`` → ``-``,
    missing ``_`` → ``-``). The canonical helper now lives in
    ``vco_lib.project_config``; this test pins the rule so a future
    refactor can't silently drop a substitution.
    """

    def test_handles_underscores(self) -> None:
        # Primary bug-fix regression: workspace paths with underscores
        # (VCO_dev, AI_hive) must produce slugs with `-` not `_`.
        self.assertEqual(
            claude_session_dir_for(Path("/home/user/VCO_dev")).name,
            "-home-user-VCO-dev",
        )
        self.assertEqual(
            claude_session_dir_for(Path("/home/user/AI_hive")).name,
            "-home-user-AI-hive",
        )

    def test_passthrough_without_underscores(self) -> None:
        # Non-regression: workspaces that the pre-fix code handled
        # correctly must still produce identical slugs.
        self.assertEqual(
            claude_session_dir_for(
                Path("/home/user/vibecoded-orchestrator")
            ).name,
            "-home-user-vibecoded-orchestrator",
        )

    def test_handles_dots(self) -> None:
        # Verified empirically against ~/.claude/projects/: paths under
        # `.claude/` are stored with `.` → `-` substitution (worktree
        # paths like /home/u/VCO_dev/.claude/worktrees/foo become
        # -home-u-VCO-dev--claude-worktrees-foo with a double-dash from
        # the consecutive `_` → `-` and `/.` → `--` rules combined).
        self.assertEqual(
            claude_session_dir_for(
                Path("/home/u/VCO_dev/.claude/worktrees/foo")
            ).name,
            "-home-u-VCO-dev--claude-worktrees-foo",
        )

    def test_returns_path_under_home_claude_projects(self) -> None:
        # The helper must anchor under ~/.claude/projects/<slug>/ so
        # consumers can do `(home / .claude / projects / slug).exists()`
        # without re-implementing the parent path.
        result = claude_session_dir_for(Path("/home/user/VCO_dev"))
        # Path.home() is the user's actual home; the test asserts the
        # structural anchor, not a literal HOME value.
        self.assertEqual(result.parent.name, "projects")
        self.assertEqual(result.parent.parent.name, ".claude")


# Body fixture for the claude_session_dir-aware resolver tests below.
# Mirrors FULL_BODY but adds the v0.2.31 field at a non-defaulting value
# so we can prove the resolver round-trips the hub's authoritative answer.
_HUB_CLAUDE_SESSION_DIR = "/home/user/.claude/projects/-home-user-VCO-dev"


class ResolveClaudeSessionDirTest(_ResolverTestBase):
    """v0.2.31 — verify the resolver propagates claude_session_dir."""

    def test_resolve_returns_claude_session_dir_from_hub(self) -> None:
        # Hub-primary path: the hub computed the slug, the resolver
        # propagates it verbatim. This is the canonical-source-of-truth
        # contract that the MCP citation-monitor relies on.
        body = {**FULL_BODY, "claude_session_dir": _HUB_CLAUDE_SESSION_DIR}
        self.session.get.return_value = _make_response(200, body)
        cfg = resolve(FULL_BODY["project_id"])
        self.assertEqual(cfg.claude_session_dir, _HUB_CLAUDE_SESSION_DIR)

    def test_resolve_back_fills_when_hub_omits_field(self) -> None:
        # Backward-compat: a pre-v0.2.31 hub paired with a v0.2.31+
        # client omits the field. The parser must back-fill with the
        # empty-string sentinel rather than crashing.
        body_old = {k: v for k, v in FULL_BODY.items() if k != "claude_session_dir"}
        self.session.get.return_value = _make_response(200, body_old)
        cfg = resolve(FULL_BODY["project_id"])
        self.assertEqual(cfg.claude_session_dir, "")


# ─── v0.2.40 R2: RL Reranker flag exposure ─────────────────────────────


class ResolveRlFlagsTest(_ResolverTestBase):
    """v0.2.40 R2 — RL Reranker per-project flags travel through the
    resolver end-to-end.

    Until v0.2.40 the three GUI checkboxes (``rl_use_global``,
    ``rl_online_training_disabled``, ``rl_global_training_source_flag``)
    wrote to ``module_settings`` but the RL container had no readback
    path — flipping a checkbox had zero runtime effect. v0.2.40+ hubs
    expose the values via ``GET /api/v1/projects/{id}/config`` and the
    Python ``ProjectConfig`` parser surfaces them on the dataclass.
    """

    def test_resolve_propagates_rl_flags_from_hub_body(self) -> None:
        # Canonical happy path: hub emits explicit values; parser
        # surfaces them as the dataclass booleans. Three flags with
        # distinct values (T/F/T) demonstrates each is read
        # independently rather than from a single shared key.
        body = {
            **FULL_BODY,
            "rl_use_global": True,
            "rl_online_training_disabled": False,
            "rl_global_training_source_flag": True,
        }
        self.session.get.return_value = _make_response(200, body)
        cfg = resolve(FULL_BODY["project_id"])
        self.assertIs(cfg.rl_use_global, True)
        self.assertIs(cfg.rl_online_training_disabled, False)
        self.assertIs(cfg.rl_global_training_source_flag, True)

    def test_resolve_back_fills_false_when_hub_omits_rl_flags(self) -> None:
        # Backward-compat guard: a pre-v0.2.40 hub paired with a
        # v0.2.40+ client doesn't emit the RL flag fields. The parser
        # must back-fill ``False`` for all three rather than crashing
        # (matches the Rust handler's ``unwrap_or(false)`` contract on
        # absent ``module_settings`` rows).
        body_old = {
            k: v
            for k, v in FULL_BODY.items()
            if k
            not in {
                "rl_use_global",
                "rl_online_training_disabled",
                "rl_global_training_source_flag",
            }
        }
        # FULL_BODY at the top of this file doesn't ship the RL keys
        # yet; the dict-comp above is a no-op on the missing-keys case
        # but stays defensive in case FULL_BODY grows them later.
        self.session.get.return_value = _make_response(200, body_old)
        cfg = resolve(FULL_BODY["project_id"])
        self.assertIs(cfg.rl_use_global, False)
        self.assertIs(cfg.rl_online_training_disabled, False)
        self.assertIs(cfg.rl_global_training_source_flag, False)

    def test_resolve_handles_non_bool_truthy_values_via_bool_coercion(
        self,
    ) -> None:
        # Defensive: if a non-conforming hub ever emits truthy non-bool
        # values (e.g. ``1`` instead of ``True``), the parser must
        # normalise them via ``bool(...)`` so downstream consumers can
        # rely on the dataclass type annotation. Mirrors the
        # ``bool(body["shared_kg_write_disabled"])`` pattern used for
        # the older flag at line 735 of project_config.py.
        body = {
            **FULL_BODY,
            "rl_use_global": 1,
            "rl_online_training_disabled": 0,
            "rl_global_training_source_flag": "yes",
        }
        self.session.get.return_value = _make_response(200, body)
        cfg = resolve(FULL_BODY["project_id"])
        self.assertIs(cfg.rl_use_global, True)
        self.assertIs(cfg.rl_online_training_disabled, False)
        # Non-empty string is truthy in Python — bool("yes") == True.
        self.assertIs(cfg.rl_global_training_source_flag, True)


if __name__ == "__main__":
    unittest.main()
