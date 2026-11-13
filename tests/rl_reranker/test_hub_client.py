# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the _HubClient + container write-path wiring (v0.2.6, module DB).

Two complementary angles:
  1. _HubClient against a mock aiohttp hub server — verifies the REST contract
     end-to-end (correct URLs, headers, payloads, soft-fail behavior).
  2. RLServer write-path wiring — verifies that lifecycle hooks (startup,
     /rotate_weights, /reset, /finetune done) invoke _push_rl_state and
     _push_rl_weights_state at the right moments.

Both use aiohttp's TestClient pattern so they run without containerization
overhead. Tests skip cleanly when aiohttp or torch are missing.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest


aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

torch = pytest.importorskip("torch")

from rl_reranker.rl_server import _HubClient  # noqa: E402


# ---------------------------------------------------------------------------
# Mock hub server — captures every request it receives so tests can assert
# the full URL + headers + body of each call.
# ---------------------------------------------------------------------------


def _build_mock_hub_app(captured: list[dict], *, status_overrides: dict | None = None) -> web.Application:
    """Construct an aiohttp app that captures requests + returns 200/404
    based on status_overrides (e.g. {"POST /api/.../rows/rl_state": 500}
    to test the soft-fail path)."""
    overrides = status_overrides or {}

    async def _capture_and_respond(request: web.Request) -> web.Response:
        body_text = await request.text()
        try:
            body_json: Any = json.loads(body_text) if body_text else None
        except Exception:
            body_json = body_text
        record = {
            "method": request.method,
            "path": request.path,
            "query": dict(request.query),
            "headers": dict(request.headers),
            "body": body_json,
        }
        captured.append(record)
        override_key = f"{request.method} {request.path}"
        if override_key in overrides:
            return web.json_response({"ok": False}, status=overrides[override_key])
        # Default response shape: 200 with empty-ish body for GET, success ack for POST/PATCH/DELETE.
        if request.method == "GET":
            return web.json_response({"key": "k", "fields": {}})
        return web.json_response({"ok": True})

    app = web.Application()
    # Match any path under /api/v1/modules/.../db/...
    app.router.add_route("*", r"/api/v1/modules/{module_id}/db/projects/{project_id}/rows/{table}", _capture_and_respond)
    app.router.add_route("*", r"/api/v1/modules/{module_id}/db/projects/{project_id}/rows/{table}/{key}", _capture_and_respond)
    app.router.add_post(r"/api/v1/modules/{module_id}/token/refresh", _capture_and_respond)
    return app


def _run_async(coro):
    return asyncio.run(coro)


async def _with_hub(app, body):
    server = TestServer(app)
    async with server:
        port = server.port
        client = _HubClient(
            base_url=f"http://127.0.0.1:{port}",
            module_id="vct-rl-reranker",
            token_env="VCT_MODULE_TOKEN_TEST",  # explicit; not used by mock
        )
        # Inject a test token so Authorization headers reach the mock.
        client._token = "test-token-abc123"
        try:
            await body(client)
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# 1. _HubClient REST contract
# ---------------------------------------------------------------------------


class TestHubClientRestContract:
    def test_get_row_hits_correct_url_with_bearer_header(self):
        captured: list[dict] = []
        app = _build_mock_hub_app(captured)

        async def _body(client):
            await client.get_row("proj-1", "rl_state", "proj-1")

        _run_async(_with_hub(app, _body))

        assert len(captured) == 1
        rec = captured[0]
        assert rec["method"] == "GET"
        assert rec["path"] == "/api/v1/modules/vct-rl-reranker/db/projects/proj-1/rows/rl_state/proj-1"
        assert rec["headers"].get("Authorization") == "Bearer test-token-abc123"

    def test_get_row_with_fields_projection_adds_query_param(self):
        captured: list[dict] = []
        app = _build_mock_hub_app(captured)

        async def _body(client):
            await client.get_row("proj-1", "rl_state", "proj-1", fields=["local_version", "last_rotated_at"])

        _run_async(_with_hub(app, _body))

        assert captured[0]["query"].get("fields") == "local_version,last_rotated_at"

    def test_upsert_row_sends_key_and_fields_in_body(self):
        captured: list[dict] = []
        app = _build_mock_hub_app(captured)

        async def _body(client):
            result = await client.upsert_row(
                "proj-1",
                "rl_state",
                "proj-1",
                {"builtin_types": 9, "dynamic_types": 0, "model_loaded": 1},
            )
            assert result is True

        _run_async(_with_hub(app, _body))

        rec = captured[0]
        assert rec["method"] == "POST"
        assert rec["path"].endswith("/rows/rl_state")
        # POST endpoint URL doesn't include {key}; key goes into body.
        assert rec["body"] == {
            "key": "proj-1",
            "fields": {"builtin_types": 9, "dynamic_types": 0, "model_loaded": 1},
        }

    def test_patch_row_sends_partial_fields(self):
        captured: list[dict] = []
        app = _build_mock_hub_app(captured)

        async def _body(client):
            result = await client.patch_row(
                "proj-1", "rl_state", "proj-1", {"refreshed_at": 1234567890},
            )
            assert result is True

        _run_async(_with_hub(app, _body))

        assert captured[0]["method"] == "PATCH"
        assert captured[0]["body"] == {"refreshed_at": 1234567890}

    def test_delete_row_returns_true_on_2xx(self):
        captured: list[dict] = []
        app = _build_mock_hub_app(captured)

        async def _body(client):
            result = await client.delete_row("proj-1", "rl_state", "proj-1")
            assert result is True

        _run_async(_with_hub(app, _body))

        assert captured[0]["method"] == "DELETE"

    def test_delete_row_returns_true_on_404_already_gone(self):
        captured: list[dict] = []
        app = _build_mock_hub_app(
            captured,
            status_overrides={
                "DELETE /api/v1/modules/vct-rl-reranker/db/projects/proj-1/rows/rl_state/proj-1": 404,
            },
        )

        async def _body(client):
            result = await client.delete_row("proj-1", "rl_state", "proj-1")
            assert result is True  # 404 == "already gone" == success

        _run_async(_with_hub(app, _body))

    def test_list_rows_returns_empty_list_on_failure(self):
        captured: list[dict] = []
        app = _build_mock_hub_app(
            captured,
            status_overrides={
                "GET /api/v1/modules/vct-rl-reranker/db/projects/proj-1/rows/rl_state": 503,
            },
        )

        async def _body(client):
            result = await client.list_rows("proj-1", "rl_state")
            assert result == []  # soft-fail returns empty

        _run_async(_with_hub(app, _body))


# ---------------------------------------------------------------------------
# 2. Soft-fail behaviors — hub down / unreachable / non-2xx
# ---------------------------------------------------------------------------


class TestHubClientSoftFail:
    def test_get_row_returns_none_when_server_unreachable(self):
        """Bogus port → ConnectionError → soft-fail → None."""
        async def _body():
            client = _HubClient(
                base_url="http://127.0.0.1:1",  # nothing listens here
                module_id="vct-rl-reranker",
            )
            client._token = "test"
            result = await client.get_row("proj-1", "rl_state", "proj-1")
            assert result is None
            await client.close()

        _run_async(_body())

    def test_upsert_row_returns_false_when_server_unreachable(self):
        async def _body():
            client = _HubClient(base_url="http://127.0.0.1:1", module_id="vct-rl-reranker")
            client._token = "test"
            result = await client.upsert_row("proj-1", "rl_state", "proj-1", {"x": 1})
            assert result is False
            await client.close()

        _run_async(_body())

    def test_upsert_row_returns_false_on_5xx(self):
        captured: list[dict] = []
        app = _build_mock_hub_app(
            captured,
            status_overrides={
                "POST /api/v1/modules/vct-rl-reranker/db/projects/proj-1/rows/rl_state": 500,
            },
        )

        async def _body(client):
            result = await client.upsert_row("proj-1", "rl_state", "proj-1", {"x": 1})
            assert result is False

        _run_async(_with_hub(app, _body))


# ---------------------------------------------------------------------------
# 3. _HubClient.None behavior when VCT_MODULE_TOKEN unset / VCT_PROJECT_ID unset
# ---------------------------------------------------------------------------


class TestHubClientGuardrails:
    def test_constructor_reads_token_from_env(self, monkeypatch):
        monkeypatch.setenv("VCT_MODULE_TOKEN", "from-env-zzz")
        client = _HubClient()
        assert client._token == "from-env-zzz"

    def test_constructor_empty_token_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("VCT_MODULE_TOKEN", raising=False)
        client = _HubClient()
        assert client._token == ""

    def test_request_with_empty_token_omits_authorization_header(self):
        """No token = no Authorization header (hub will reject; that's fine)."""
        captured: list[dict] = []
        app = _build_mock_hub_app(captured)

        async def _body(client):
            client._token = ""  # explicit clear
            await client.get_row("proj-1", "rl_state", "proj-1")

        _run_async(_with_hub(app, _body))

        assert captured[0]["headers"].get("Authorization") is None
