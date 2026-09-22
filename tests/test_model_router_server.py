# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The HTTP surface, driven against STUB upstreams — no external network.

The stub upstreams are real aiohttp servers on loopback, not mocked client
objects, so the streaming path, the header filtering and the body bytes are
exercised as they will run in production. Every vendor row is rewritten to
point at the stub, which is only possible because vendors are DATA.

Covers the failure modes that were each paid for once during field proving:

* ``/v1/models?limit=20`` and ``/v1/models/`` both served — an exact-path
  match closes the connection with no response, which the client reports as
  "issue with the selected model" with zero upstream requests logged;
* the forwarded BODY carries the routed model name, checked against the bytes
  the stub actually received;
* SSE relayed unbuffered, and a client disconnect mid-stream does not take the
  server down;
* 401 for a wrong/missing host token, 400 for routing refusals, 403 for a
  non-loopback peer, 502 for an unreachable upstream, and vendor 4xx relayed
  verbatim;
* ``anthropic-version`` defaulted for a bare client;
* ``/health`` answers while a secret resolver is wedged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from aiohttp import ClientTimeout, web
from aiohttp.test_utils import TestClient, TestServer

from tests.common.qwen_catalog import (  # noqa: E402
    QWEN_EXCLUDED_SIX,
    QWEN_LIVE_MODELS,
)
from model_router.auth import OAuthReader, host_token_stamp
from model_router.catalog import (
    SOURCE_DECLARED,
    SOURCE_LIVE,
    SOURCE_STATIC,
    SOURCE_UNFETCHED,
)
from model_router.config import GatewayConfig
from model_router.secrets import VendorKeyResolver
from model_router.server import APP_KEY, create_app
from model_router.vendors import ANTHROPIC_FAMILY, VENDORS, AnthropicFamily, Vendor

HOST_TOKEN = "wp9-host-token-not-a-real-secret"
FAKE_VENDOR_KEY = "wp9-vendor-key-synthetic"
FAKE_OAUTH = "wp9-oauth-access-token-synthetic"


class _Upstream:
    """A recording stub for one upstream."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.models_payload: dict | None = None
        self.messages_status = 200
        self.messages_body = {"id": "msg_1", "model": "stub", "content": []}
        self.messages_raw: bytes | None = None
        self.content_type = "application/json"
        self.stream_chunks: list[bytes] | None = None
        #: A NON-stream body delivered in several writes, so the client's
        #: reader sees several chunks. The gap between them is what makes the
        #: difference between "one buffered chunk" and "the whole body"
        #: observable rather than a race — see
        #: ``tests/test_v0294_gateway_relay_buffer.py``.
        self.json_chunks: list[bytes] | None = None
        self.stream_gap_s = 0.0
        self.server: TestServer | None = None

    @property
    def message_requests(self) -> list[dict[str, Any]]:
        """Only the proxied Messages calls — the catalog GET is also recorded."""
        return [
            r for r in self.requests if r["path"].startswith("/v1/messages")
        ]

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/v1/models", self._models)
        app.router.add_post("/v1/messages", self._messages)
        app.router.add_post("/v1/messages/count_tokens", self._messages)
        return app

    async def start(self) -> str:
        self.server = TestServer(self.app())
        await self.server.start_server()
        return str(self.server.make_url("")).rstrip("/")

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.close()

    async def _record(self, request: web.Request) -> None:
        body = await request.read()
        self.requests.append(
            {
                "path": request.path,
                "query": request.query_string,
                "headers": dict(request.headers),
                "body": body,
            }
        )

    async def _models(self, request: web.Request) -> web.Response:
        await self._record(request)
        if self.models_payload is None:
            return web.json_response({"error": "no catalog"}, status=500)
        return web.json_response(self.models_payload)

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        await self._record(request)
        if self.json_chunks is not None:
            response = web.StreamResponse(
                status=self.messages_status,
                headers={"Content-Type": self.content_type},
            )
            response.enable_chunked_encoding()
            await response.prepare(request)
            for chunk in self.json_chunks:
                await response.write(chunk)
                if self.stream_gap_s:
                    await asyncio.sleep(self.stream_gap_s)
            await response.write_eof()
            return response
        if self.stream_chunks is not None:
            response = web.StreamResponse(
                status=self.messages_status,
                headers={"Content-Type": "text/event-stream"},
            )
            response.enable_chunked_encoding()
            await response.prepare(request)
            for chunk in self.stream_chunks:
                await response.write(chunk)
                if self.stream_gap_s:
                    await asyncio.sleep(self.stream_gap_s)
            await response.write_eof()
            return response
        if self.messages_raw is not None:
            return web.Response(
                body=self.messages_raw,
                status=self.messages_status,
                content_type=self.content_type,
            )
        return web.json_response(self.messages_body, status=self.messages_status)


class GatewayTestBase(unittest.IsolatedAsyncioTestCase):
    """Builds a gateway wired to two stub upstreams."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wp9-srv-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        # Every gateway built here owns a usage ledger, and a ledger writes a
        # JSONL file under ``vco_lib.paths.vct_metrics_dir()``. The suite-wide
        # conftest redirect already keeps that out of the user's real
        # ``~/.vct``; this narrows it further to THIS test, so one test's rows
        # can never be counted by the next one's assertion. Every subclass —
        # including the files that import this base and are owned elsewhere —
        # inherits the isolation without doing anything.
        state = mock.patch.dict(
            os.environ, {"VCT_STATE_DIR": str(self.root / "vct")},
        )
        state.start()
        self.addCleanup(state.stop)
        self.usage_ledger_path = (
            self.root / "vct" / "metrics" / "gateway-usage.jsonl"
        )

        self.vendor_up = _Upstream()
        self.anthropic_up = _Upstream()
        vendor_url = await self.vendor_up.start()
        anthropic_url = await self.anthropic_up.start()
        self.addAsyncCleanup(self.vendor_up.stop)
        self.addAsyncCleanup(self.anthropic_up.stop)

        base = VENDORS["zai"]
        self.vendor = Vendor(
            vendor_id=base.vendor_id,
            display_suffix=base.display_suffix,
            display_name=base.display_name,
            namespace=base.namespace,
            upstream=vendor_url,
            secret_keys=base.secret_keys,
            bare_id_prefixes=base.bare_id_prefixes,
            catalog_path=base.catalog_path,
            docs_url=base.docs_url,
            alias_trap=base.alias_trap,
        )
        self.anthropic = AnthropicFamily(
            family_id=ANTHROPIC_FAMILY.family_id,
            upstream=anthropic_url,
            catalog_path=ANTHROPIC_FAMILY.catalog_path,
            catalog_query=ANTHROPIC_FAMILY.catalog_query,
            docs_url=ANTHROPIC_FAMILY.docs_url,
        )

        self.credentials = self.root / ".credentials.json"
        self.write_credentials(FAKE_OAUTH, expires_in_ms=3_600_000)

        self.key_getter = lambda key, project=None: FAKE_VENDOR_KEY
        self.config = GatewayConfig(
            host="127.0.0.1",
            port=0,
            token=HOST_TOKEN,
            credentials_file=self.credentials,
            context_table_file=self.root / "chat_model_context.json",
            catalog_ttl_s=3600,
            static_retry_ttl_s=1,
            upstream_idle_timeout_s=10,
            catalog_timeout_s=5,
        )
        self.client = await self.make_client()

    async def make_client(self, **overrides) -> TestClient:
        app = create_app(
            self.config,
            vendors=overrides.pop("vendors", None)
            or {self.vendor.vendor_id: self.vendor},
            anthropic=self.anthropic,
            oauth_reader=OAuthReader(self.credentials),
            key_resolver=VendorKeyResolver(
                getter=overrides.pop("key_getter", self.key_getter),
            ),
            token_permissions=overrides.pop("token_permissions", "owner_only"),
            token_file_stamp=overrides.pop("token_file_stamp", None),
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    def write_credentials(self, token: str | None, *, expires_in_ms: int) -> None:
        section: dict[str, Any] = {}
        if token is not None:
            section["accessToken"] = token
        section["expiresAt"] = int(time.time() * 1000) + expires_in_ms
        self.credentials.write_text(
            json.dumps({"claudeAiOauth": section}), encoding="utf-8",
        )

    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {HOST_TOKEN}"}


class QueryStrippedPathTests(GatewayTestBase):
    """Gotcha 1 — the CLI calls ``/v1/models?limit=…``."""

    async def test_models_served_with_query_string(self) -> None:
        self.vendor_up.models_payload = {"data": [{"id": "glm-5.3"}]}
        self.anthropic_up.models_payload = {"data": [{"id": "claude-opus-5"}]}
        resp = await self.client.get("/v1/models?limit=20", headers=self.auth())
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body["data"])

    async def test_models_served_with_trailing_slash(self) -> None:
        self.vendor_up.models_payload = {"data": [{"id": "glm-5.3"}]}
        self.anthropic_up.models_payload = {"data": []}
        resp = await self.client.get("/v1/models/", headers=self.auth())
        self.assertEqual(resp.status, 200)

    async def test_models_served_with_both(self) -> None:
        self.vendor_up.models_payload = {"data": []}
        self.anthropic_up.models_payload = {"data": []}
        resp = await self.client.get("/v1/models/?limit=5", headers=self.auth())
        self.assertEqual(resp.status, 200)

    async def test_messages_served_with_query_string(self) -> None:
        resp = await self.client.post(
            "/v1/messages?beta=true",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.vendor_up.requests[0]["query"], "beta=true")

    async def test_messages_served_with_trailing_slash(self) -> None:
        resp = await self.client.post(
            "/v1/messages/",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 200)
        # The trailing slash is NOT passed upstream.
        self.assertEqual(self.vendor_up.requests[0]["path"], "/v1/messages")

    async def test_an_unknown_path_gets_a_named_404_not_a_dropped_connection(self) -> None:
        resp = await self.client.get("/v1/nonsense", headers=self.auth())
        self.assertEqual(resp.status, 404)
        body = await resp.json()
        self.assertIn("/v1/models", body["error"]["message"])


class BodyRewriteTests(GatewayTestBase):
    """Gotcha 2 — the routed name must be in the BYTES."""

    async def _forwarded(self) -> dict:
        return json.loads(self.vendor_up.requests[0]["body"])

    async def test_prefix_is_rewritten_in_forwarded_body(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "max_tokens": 8},
        )
        forwarded = await self._forwarded()
        self.assertEqual(forwarded["model"], "glm-5.3")

    async def test_one_m_suffix_is_stripped_in_the_forwarded_body(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3[1m]", "messages": []},
        )
        forwarded = await self._forwarded()
        self.assertEqual(forwarded["model"], "glm-5.3")

    async def test_the_rest_of_the_body_is_preserved(self) -> None:
        payload = {
            "model": "claude-gw/glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 42,
            "stream": False,
            "system": "be brief",
        }
        await self.client.post("/v1/messages", headers=self.auth(), json=payload)
        forwarded = await self._forwarded()
        for key, value in payload.items():
            if key == "model":
                continue
            self.assertEqual(forwarded[key], value)

    async def test_first_party_body_is_forwarded_byte_identical(self) -> None:
        """No rewrite means no re-encode: the exact bytes go upstream."""
        raw = b'{"model":"claude-opus-5","messages":[],"max_tokens":1}'
        await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "Content-Type": "application/json"},
            data=raw,
        )
        self.assertEqual(self.anthropic_up.requests[0]["body"], raw)

    async def test_count_tokens_rewrites_too(self) -> None:
        await self.client.post(
            "/v1/messages/count_tokens",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(self.vendor_up.requests[0]["path"], "/v1/messages/count_tokens")
        self.assertEqual((await self._forwarded())["model"], "glm-5.3")


class HeaderTests(GatewayTestBase):
    """Gotcha 3 and the credential-forwarding rules."""

    async def test_anthropic_version_is_defaulted_for_a_bare_client(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        headers = self.vendor_up.requests[0]["headers"]
        self.assertEqual(headers.get("anthropic-version"), "2023-06-01")

    async def test_a_client_supplied_version_is_not_overwritten(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "anthropic-version": "2099-01-01"},
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        headers = self.vendor_up.requests[0]["headers"]
        self.assertEqual(headers.get("anthropic-version"), "2099-01-01")

    async def test_the_host_token_is_never_forwarded_upstream(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "x-api-key": HOST_TOKEN},
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        headers = self.vendor_up.requests[0]["headers"]
        serialised = json.dumps(dict(headers))
        self.assertNotIn(HOST_TOKEN, serialised)
        self.assertEqual(headers.get("Authorization"), f"Bearer {FAKE_VENDOR_KEY}")

    async def test_the_vendor_key_never_reaches_the_first_party_upstream(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-opus-5", "messages": []},
        )
        serialised = json.dumps(dict(self.anthropic_up.requests[0]["headers"]))
        self.assertNotIn(FAKE_VENDOR_KEY, serialised)
        self.assertIn(FAKE_OAUTH, serialised)

    async def test_the_oauth_beta_header_is_added_for_the_first_party_route(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-opus-5", "messages": []},
        )
        headers = self.anthropic_up.requests[0]["headers"]
        self.assertIn("oauth-2025-04-20", headers.get("anthropic-beta", ""))

    async def test_an_existing_beta_header_is_extended_not_replaced(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "anthropic-beta": "context-1m-2025-08-07"},
            json={"model": "claude-opus-5", "messages": []},
        )
        beta = self.anthropic_up.requests[0]["headers"].get("anthropic-beta", "")
        self.assertIn("context-1m-2025-08-07", beta)
        self.assertIn("oauth-2025-04-20", beta)

    async def test_hop_by_hop_headers_are_not_forwarded(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "Connection": "keep-alive"},
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        headers = {
            k.lower() for k in self.vendor_up.requests[0]["headers"]
        }
        self.assertNotIn("x-api-key", headers)


class StreamingTests(GatewayTestBase):
    async def test_sse_chunks_are_relayed_with_the_content_type(self) -> None:
        self.vendor_up.stream_chunks = [
            b"event: message_start\ndata: {}\n\n",
            b"event: content_block_delta\ndata: {}\n\n",
            b"event: message_stop\ndata: {}\n\n",
        ]
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "stream": True},
        )
        self.assertEqual(resp.status, 200)
        self.assertIn("event-stream", resp.headers["Content-Type"])
        body = await resp.read()
        self.assertEqual(body, b"".join(self.vendor_up.stream_chunks))

    async def test_chunks_arrive_progressively_not_buffered_to_the_end(self) -> None:
        self.vendor_up.stream_chunks = [b"data: a\n\n", b"data: b\n\n"]
        self.vendor_up.stream_gap_s = 0.25
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "stream": True},
        )
        started = time.monotonic()
        first = await resp.content.read(9)
        first_at = time.monotonic() - started
        await resp.read()
        total = time.monotonic() - started
        self.assertEqual(first, b"data: a\n\n")
        self.assertLess(
            first_at, total - 0.1,
            "the first chunk arrived only once the stream had finished — the "
            "relay is buffering instead of passing through",
        )

    async def test_client_disconnect_mid_stream_leaves_the_server_alive(self) -> None:
        self.vendor_up.stream_chunks = [b"data: %d\n\n" % i for i in range(40)]
        self.vendor_up.stream_gap_s = 0.02
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "stream": True},
        )
        await resp.content.read(9)
        resp.close()
        await asyncio.sleep(0.2)
        health = await self.client.get("/health")
        self.assertEqual(health.status, 200)
        # ...and the gateway still serves a fresh request afterwards.
        again = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(again.status, 200)


class AuthAndRoutingTests(GatewayTestBase):
    async def test_missing_token_is_401(self) -> None:
        resp = await self.client.get("/v1/models")
        self.assertEqual(resp.status, 401)
        body = await resp.json()
        self.assertEqual(body["error"]["type"], "authentication_error")

    async def test_wrong_token_is_401(self) -> None:
        resp = await self.client.post(
            "/v1/messages",
            headers={"Authorization": "Bearer wrong"},
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 401)

    async def test_x_api_key_is_accepted_as_the_host_token(self) -> None:
        resp = await self.client.post(
            "/v1/messages",
            headers={"x-api-key": HOST_TOKEN},
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 200)

    async def test_unknown_model_is_a_local_400_and_reaches_no_upstream(self) -> None:
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "mistral-large", "messages": []},
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.vendor_up.requests, [])
        self.assertEqual(self.anthropic_up.requests, [])

    async def test_a_claude_id_under_the_vendor_namespace_never_reaches_the_vendor(self) -> None:
        """The silent-substitution guard, end to end."""
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/claude-sonnet-5", "messages": []},
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.vendor_up.requests, [])
        body = await resp.json()
        self.assertIn("OWN model", body["error"]["message"])

    async def test_an_unreadable_body_gets_the_upstreams_verdict(self) -> None:
        """It used to be a gateway 400 — which native cannot produce.

        Native sends whatever the client wrote and the API judges it, so the
        gateway forwards the bytes unread to the first-party upstream and
        relays what comes back. A refusal invented here is a refusal the user
        cannot appeal to anyone.
        """
        self.anthropic_up.messages_status = 400
        self.anthropic_up.messages_body = {
            "type": "error", "error": {"type": "invalid_request_error"},
        }
        resp = await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "Content-Type": "application/json"},
            data=b"{not json",
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(await resp.json(), self.anthropic_up.messages_body)
        # Forwarded byte for byte, and to the FIRST-PARTY upstream — no
        # vendor key is spent on bytes nobody could read.
        self.assertEqual(
            self.anthropic_up.message_requests[0]["body"], b"{not json",
        )
        self.assertEqual(self.vendor_up.requests, [])

    async def test_upstream_error_is_relayed_verbatim(self) -> None:
        """Verbatim relay, on a status that is NOT quota exhaustion.

        This used to assert it for 429, which is now the ONE substituted
        status (``tests/test_v0294_gateway_quota.py``). 400 keeps the general
        policy pinned: the vendor's own words still reach the user.
        """
        self.vendor_up.messages_status = 400
        self.vendor_up.messages_raw = b'{"error":{"code":"1302","message":"bad"}}'
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(await resp.read(), self.vendor_up.messages_raw)

    async def test_unreachable_upstream_is_502(self) -> None:
        await self.vendor_up.stop()
        self.vendor_up.server = None
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 502)

    async def test_no_vendor_key_is_503_with_an_actionable_message(self) -> None:
        client = await self.make_client(
            key_getter=lambda key, project=None: (_ for _ in ()).throw(
                LookupError("absent"),
            ),
        )
        resp = await client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 503)
        body = await resp.json()
        self.assertIn("Secrets panel", body["error"]["message"])


class OAuthExpiryTests(GatewayTestBase):
    async def test_expired_token_is_401_naming_the_fix(self) -> None:
        self.write_credentials(FAKE_OAUTH, expires_in_ms=-1000)
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(resp.status, 401)
        message = (await resp.json())["error"]["message"]
        self.assertIn("expired", message)
        self.assertIn("claude", message)
        self.assertEqual(self.anthropic_up.requests, [])

    async def test_empty_token_is_401_naming_the_fix(self) -> None:
        self.write_credentials(None, expires_in_ms=3_600_000)
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(resp.status, 401)
        self.assertIn("no access token", (await resp.json())["error"]["message"])

    async def test_missing_file_is_401_not_a_crash(self) -> None:
        self.credentials.unlink()
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(resp.status, 401)
        message = (await resp.json())["error"]["message"]
        self.assertIn("does not exist", message)

    async def test_a_refreshed_file_is_picked_up_without_a_restart(self) -> None:
        """The CLI rewrites the file whenever it refreshes; a gateway holding
        a stale copy would 401 a user who had just logged in."""
        self.write_credentials(None, expires_in_ms=3_600_000)
        first = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(first.status, 401)
        self.write_credentials("refreshed-synthetic", expires_in_ms=3_600_000)
        import os

        os.utime(self.credentials, (2_000_000, 2_000_000))
        second = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(second.status, 200)

    async def test_no_token_material_appears_in_the_401_body(self) -> None:
        self.write_credentials(FAKE_OAUTH, expires_in_ms=-1000)
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertNotIn(FAKE_OAUTH, await resp.text())


class HealthTests(GatewayTestBase):
    #: Kept in step with ``health_handler``'s docstring by the test below.
    DOCUMENTED_FIELDS = {
        "ok", "service", "version", "port", "host", "catalog_source",
        "catalog_filter", "window_rows", "catalog_hidden",
        "context_table_source", "context_table_path", "oauth_present",
        "oauth_state", "oauth_expires_in_s", "vendors", "vendor_keys_cached",
        "vendor_keys_stale",
        "secret_scope",
        "model_echo_mismatches",
        "token_file_permissions", "host_token_file", "usage_ledger",
    }

    async def test_health_needs_no_token(self) -> None:
        resp = await self.client.get("/health")
        self.assertEqual(resp.status, 200)

    async def test_health_reports_exactly_the_documented_fields(self) -> None:
        body = await (await self.client.get("/health")).json()
        self.assertEqual(set(body), self.DOCUMENTED_FIELDS)

    async def test_every_documented_field_appears_in_the_handler_docstring(self) -> None:
        """A field documented but not emitted, or emitted but undocumented,
        is exactly the promise this rule exists to catch."""
        from model_router.server import health_handler

        doc = health_handler.__doc__ or ""
        for field in self.DOCUMENTED_FIELDS:
            self.assertIn(f"``{field}``", doc, f"/health field {field} undocumented")

    async def test_health_reports_the_secret_scope_it_resolves_keys_in(self) -> None:
        """R5b: `vendors` beside an empty `vendor_keys_cached` reads like "no
        key configured yet". This field is what tells those two apart from
        "this daemon's scope cannot see any key you configure"."""
        body = await (await self.client.get("/health")).json()
        scope = body["secret_scope"]
        self.assertEqual(set(scope), {"project", "resolvable", "reason"})
        # Unprobed is `null`, NOT `false`: a probe that has not run is not
        # evidence of absence.
        self.assertIsNone(scope["resolvable"])

    async def test_health_never_probes_the_scope_itself(self) -> None:
        """The verdict is produced at startup and refreshed on a key miss. A
        liveness route that could reach the hub can hang when the hub is
        down — the thing /health exists not to do."""
        gateway = self.client.app[APP_KEY]
        probed: list[str] = []
        gateway.keys._scope_prober = lambda arg: probed.append(arg) or "id"

        await self.client.get("/health")

        self.assertEqual(probed, [])

    async def test_health_answers_while_the_secret_resolver_is_wedged(self) -> None:
        """Gotcha 4: a short-timeout probe must not break its own pipe."""
        def wedged(key, project=None):
            time.sleep(5)
            raise LookupError("never")

        client = await self.make_client(key_getter=wedged)
        started = time.monotonic()
        resp = await client.get("/health", timeout=ClientTimeout(total=2))
        elapsed = time.monotonic() - started
        self.assertEqual(resp.status, 200)
        self.assertLess(elapsed, 0.5, f"/health took {elapsed:.2f}s")

    async def test_health_answers_while_a_vendor_request_is_in_flight(self) -> None:
        """The wedged resolver runs off the event loop, so a concurrent
        /health is not queued behind it."""
        def wedged(key, project=None):
            time.sleep(1.0)
            raise LookupError("never")

        client = await self.make_client(key_getter=wedged)
        in_flight = asyncio.create_task(
            client.post(
                "/v1/messages", headers=self.auth(),
                json={"model": "claude-gw/glm-5.3", "messages": []},
            )
        )
        await asyncio.sleep(0.15)
        started = time.monotonic()
        resp = await client.get("/health", timeout=ClientTimeout(total=2))
        elapsed = time.monotonic() - started
        self.assertEqual(resp.status, 200)
        self.assertLess(elapsed, 0.5)
        blocked = await in_flight
        self.assertEqual(blocked.status, 503)

    async def test_health_contacts_no_upstream(self) -> None:
        await self.client.get("/health")
        self.assertEqual(self.vendor_up.requests, [])
        self.assertEqual(self.anthropic_up.requests, [])

    async def test_catalog_source_starts_unfetched_then_reports_the_truth(self) -> None:
        body = await (await self.client.get("/health")).json()
        self.assertEqual(
            body["catalog_source"],
            {"anthropic": SOURCE_UNFETCHED, "zai": SOURCE_UNFETCHED},
        )
        self.vendor_up.models_payload = {"data": [{"id": "glm-5.3"}]}
        self.anthropic_up.models_payload = None  # 500 -> static fallback
        await self.client.get("/v1/models", headers=self.auth())
        body = await (await self.client.get("/health")).json()
        self.assertEqual(body["catalog_source"]["zai"], SOURCE_LIVE)
        self.assertEqual(body["catalog_source"]["anthropic"], SOURCE_STATIC)

    async def test_oauth_state_is_reported_without_the_token(self) -> None:
        body = await (await self.client.get("/health")).json()
        self.assertTrue(body["oauth_present"])
        self.assertEqual(body["oauth_state"], "present")
        self.assertNotIn(FAKE_OAUTH, json.dumps(body))

    async def test_expired_login_reads_as_expired_not_absent(self) -> None:
        self.write_credentials(FAKE_OAUTH, expires_in_ms=-1)
        body = await (await self.client.get("/health")).json()
        self.assertFalse(body["oauth_present"])
        self.assertEqual(body["oauth_state"], "expired")

    async def test_the_serve_stale_state_is_empty_on_a_healthy_daemon(self) -> None:
        """Issue 12: ``vendor_keys_stale`` lists the vendors answered from
        the last-known-good store; with a resolving getter that is none."""
        body = await (await self.client.get("/health")).json()
        self.assertEqual(body["vendor_keys_stale"], [])


class ModelEchoTests(GatewayTestBase):
    """Issue 9: a vendor answer whose reported ``model`` is not the forwarded
    id is the alias-trap shape, and routing alone cannot see it."""

    #: The forwarded id for a request asking ``claude-gw/glm-5.3``: the
    #: namespace is the gateway's own invention and is stripped on the way out.
    FORWARDED = "glm-5.3"

    def _count(self, client: TestClient | None = None) -> int:
        app = (client or self.client).app
        return app[APP_KEY].model_echo_mismatches

    async def test_a_foreign_model_in_a_vendor_body_is_counted(self) -> None:
        # The stub's DEFAULT body is a mismatch shape ("model": "stub").
        self.vendor_up.messages_body = {
            "id": "msg_1", "model": "stub", "content": [],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self._count(), 1)

    async def test_a_matching_model_is_not_counted(self) -> None:
        self.vendor_up.messages_body = {
            "id": "msg_1", "model": self.FORWARDED, "content": [],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self._count(), 0)

    async def test_the_absent_model_field_is_silence_not_mismatch(self) -> None:
        """Whether Z.ai's streaming events carry the field at all is exactly
        what is UNVERIFIED from the register — so absence must not fire."""
        self.vendor_up.messages_body = {
            "id": "msg_1", "content": [],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self._count(), 0)

    async def test_a_foreign_model_in_a_streamed_message_start_is_counted(self) -> None:
        import json as _json

        self.vendor_up.stream_chunks = [
            b"event: message_start\ndata: "
            + _json.dumps({
                "type": "message_start",
                "message": {
                    "model": "glm-5.3-flash",
                    "usage": {"input_tokens": 4, "output_tokens": 0},
                },
            }).encode()
            + b"\n\n",
            b"event: message_delta\ndata: "
            + _json.dumps({
                "type": "message_delta",
                "usage": {"output_tokens": 2},
            }).encode()
            + b"\n\n",
            b"event: message_stop\ndata: {}\n\n",
        ]
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-gw/glm-5.3", "messages": [], "stream": True,
            },
        )
        self.assertEqual(resp.status, 200)
        await resp.read()
        self.assertEqual(self._count(), 1)

    async def test_the_warn_is_edge_triggered_per_triple(self) -> None:
        """A vendor that aliases does so on EVERY request; the counter is the
        rate and one WARN per distinct triple is the ceiling."""
        import logging as _logging

        import model_router.server as mod

        self.vendor_up.messages_body = {
            "id": "msg_1", "model": "stub", "content": [],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }
        with self.assertLogs(mod.logger, level=_logging.WARNING) as captured:
            for _ in range(2):
                resp = await self.client.post(
                    "/v1/messages",
                    headers=self.auth(),
                    json={"model": "claude-gw/glm-5.3", "messages": []},
                )
                self.assertEqual(resp.status, 200)
                await resp.read()
        trap_warnings = [
            line for line in captured.output if "alias-trap" in line
        ]
        self.assertEqual(len(trap_warnings), 1, captured.output)
        self.assertEqual(self._count(), 2)

    async def test_first_party_routes_are_exempt(self) -> None:
        """The alias trap is a vendor property; native is the definition of
        correct, and an Anthropic-shaped body saying its own model is not for
        this daemon to police."""
        self.anthropic_up.messages_body = {
            "id": "msg_1", "model": "something-else-entirely", "content": [],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self._count(), 0)

    async def test_health_carries_the_counter(self) -> None:
        self.vendor_up.messages_body = {
            "id": "msg_1", "model": "stub", "content": [],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        body = await (await self.client.get("/health")).json()
        self.assertEqual(body["model_echo_mismatches"], 1)


class HostTokenFileTests(GatewayTestBase):
    """Issue 10: /health shows the token file stamp the daemon LOADED beside
    the one on disk NOW, so the three 401 shapes are told apart without a
    restart."""

    def _token_file(self):
        path = self.root / "vct" / "model-gateway.token"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def test_without_a_sampled_stamp_the_field_is_null(self) -> None:
        body = await (await self.client.get("/health")).json()
        self.assertIsNone(body["host_token_file"])

    async def test_the_loaded_stamp_survives_a_rewrite_of_the_file(self) -> None:
        path = self._token_file()
        path.write_text("first-token", encoding="utf-8")
        loaded = host_token_stamp(path)
        client = await self.make_client(token_file_stamp=loaded)

        path.write_text("a-regenerated-longer-token", encoding="utf-8")
        body = await (await client.get("/health")).json()
        report = body["host_token_file"]
        # The LOADED half never changes: that is the whole diagnostic. The
        # current half states what is on disk NOW.
        self.assertEqual(report["path"], str(path))
        self.assertEqual(report["mtime_ns"], loaded["mtime_ns"])
        self.assertEqual(report["size"], loaded["size"])
        self.assertEqual(report["current_size"], len("a-regenerated-longer-token"))
        self.assertNotEqual(report["current_size"], report["size"])
        current = path.stat()
        self.assertEqual(report["current_mtime_ns"], current.st_mtime_ns)

    async def test_a_deleted_file_reports_null_current_half(self) -> None:
        path = self._token_file()
        path.write_text("first-token", encoding="utf-8")
        loaded = host_token_stamp(path)
        client = await self.make_client(token_file_stamp=loaded)

        path.unlink()
        body = await (await client.get("/health")).json()
        report = body["host_token_file"]
        self.assertEqual(report["size"], loaded["size"])
        self.assertIsNone(report["current_mtime_ns"])
        self.assertIsNone(report["current_size"])

    async def test_the_report_carries_no_token_content(self) -> None:
        path = self._token_file()
        path.write_text(HOST_TOKEN, encoding="utf-8")
        loaded = host_token_stamp(path)
        client = await self.make_client(token_file_stamp=loaded)
        body = await (await client.get("/health")).json()
        self.assertNotIn(HOST_TOKEN, json.dumps(body))


class CatalogSurfaceTests(GatewayTestBase):
    async def test_union_catalog_namespaces_only_the_vendor_entries(self) -> None:
        self.anthropic_up.models_payload = {
            "data": [{"id": "claude-opus-5", "display_name": "Opus 5"}],
        }
        self.vendor_up.models_payload = {"data": [{"id": "glm-5.3"}, {"id": "glm-5.1"}]}
        body = await (await self.client.get("/v1/models", headers=self.auth())).json()
        ids = [row["id"] for row in body["data"]]
        # The picker's spelling of a 1M first-party model is the ``[1m]`` one
        # (``window_rows`` default); what this test discriminates is the
        # NAMESPACE, which first-party rows never carry either way.
        self.assertIn("claude-opus-5[1m]", ids)
        self.assertIn("claude-gw/glm-5.3[1m]", ids)
        # Withheld FIRST by the truth filter (verified_ids); the default
        # ``latest`` filter would withhold it too, so this assertion cannot
        # discriminate — the discriminating coverage lives in
        # VerifiedIdsTests::test_the_shipped_subscription_row_publishes_exactly_the_verified_ids.
        # Named as withheld rather than dropped: a short picker must still
        # have an answer.
        self.assertIn("claude-gw/glm-5.1", body["_vct_catalog_hidden"])

    async def test_every_advertised_id_survives_client_side_discovery(self) -> None:
        """Claude Code keeps only ids containing claude/anthropic; an entry
        that fails this is invisible in the picker."""
        self.anthropic_up.models_payload = {"data": [{"id": "claude-opus-5"}]}
        self.vendor_up.models_payload = {"data": [{"id": "glm-5.3"}]}
        body = await (await self.client.get("/v1/models", headers=self.auth())).json()
        for row in body["data"]:
            lowered = row["id"].lower()
            self.assertTrue(
                "claude" in lowered or "anthropic" in lowered, row["id"],
            )

    async def test_a_row_withheld_from_the_picker_still_answers(self) -> None:
        """``window_rows`` decides what is ADVERTISED, never what the gateway
        will serve. The plain id of a 1M model is withheld from the picker by
        default and must keep routing: a session already pinned to it, or a
        user who types it, must not meet a 404 because a display knob moved.
        This is the same contract curated-hidden vendor ids have."""
        self.anthropic_up.models_payload = {"data": [{"id": "claude-opus-5"}]}
        self.vendor_up.models_payload = {"data": []}
        body = await (await self.client.get("/v1/models", headers=self.auth())).json()
        self.assertNotIn(
            "claude-opus-5", {row["id"] for row in body["data"]},
            "precondition: the default withholds the plain row",
        )
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(resp.status, 200)

    async def test_every_advertised_vendor_id_routes_back_to_that_vendor(self) -> None:
        """Round trip: whatever the picker shows must be selectable."""
        self.anthropic_up.models_payload = {"data": []}
        self.vendor_up.models_payload = {
            "data": [{"id": mid} for mid in ("glm-5.3", "glm-5.1", "glm-4.5-air")],
        }
        body = await (await self.client.get("/v1/models", headers=self.auth())).json()
        for row in body["data"]:
            resp = await self.client.post(
                "/v1/messages",
                headers=self.auth(),
                json={"model": row["id"], "messages": []},
            )
            self.assertEqual(resp.status, 200, row["id"])
        forwarded = {
            json.loads(r["body"])["model"] for r in self.vendor_up.message_requests
        }
        # ``glm-5.1`` is not posted because the picker never showed it — the
        # loop above walks ``body["data"]``. It is still SELECTABLE by name,
        # which is what the hidden list promises, so it is asserted there
        # rather than dropped from the test.
        self.assertEqual(forwarded, {"glm-5.3", "glm-4.5-air"})
        self.assertIn("claude-gw/glm-5.1", body["_vct_catalog_hidden"])

    async def test_static_fallback_is_marked_never_silent(self) -> None:
        self.anthropic_up.models_payload = None
        self.vendor_up.models_payload = None
        body = await (await self.client.get("/v1/models", headers=self.auth())).json()
        self.assertEqual(
            body["_vct_catalog_source"],
            {"anthropic": SOURCE_STATIC, "zai": SOURCE_STATIC},
        )
        self.assertTrue(body["data"])

    async def test_catalog_fetch_uses_the_right_credential_per_family(self) -> None:
        self.anthropic_up.models_payload = {"data": []}
        self.vendor_up.models_payload = {"data": []}
        await self.client.get("/v1/models", headers=self.auth())
        vendor_headers = self.vendor_up.requests[0]["headers"]
        anthropic_headers = self.anthropic_up.requests[0]["headers"]
        self.assertEqual(
            vendor_headers.get("Authorization"), f"Bearer {FAKE_VENDOR_KEY}",
        )
        self.assertEqual(
            anthropic_headers.get("Authorization"), f"Bearer {FAKE_OAUTH}",
        )
        self.assertNotIn(FAKE_VENDOR_KEY, json.dumps(dict(anthropic_headers)))

    async def test_a_live_catalog_is_cached(self) -> None:
        self.anthropic_up.models_payload = {"data": []}
        self.vendor_up.models_payload = {"data": [{"id": "glm-5.3"}]}
        await self.client.get("/v1/models", headers=self.auth())
        await self.client.get("/v1/models", headers=self.auth())
        self.assertEqual(len(self.vendor_up.requests), 1)

    async def test_a_static_fallback_is_retried_sooner_than_a_live_result(self) -> None:
        """The prototype cached the fallback for the full live TTL, so a
        one-minute outage cost six hours of a stale picker."""
        self.anthropic_up.models_payload = {"data": []}
        self.vendor_up.models_payload = None
        await self.client.get("/v1/models", headers=self.auth())
        self.assertEqual(len(self.vendor_up.requests), 1)
        await asyncio.sleep(1.1)  # static_retry_ttl_s is 1 in this fixture
        self.vendor_up.models_payload = {"data": [{"id": "glm-5.3"}]}
        body = await (await self.client.get("/v1/models", headers=self.auth())).json()
        self.assertEqual(len(self.vendor_up.requests), 2)
        self.assertEqual(body["_vct_catalog_source"]["zai"], SOURCE_LIVE)


class LoopbackGuardTests(unittest.IsolatedAsyncioTestCase):
    """The peer check, unit-tested through the middleware.

    A running server binds loopback, so the negative case cannot be produced
    over a real socket without binding a routable interface; the middleware is
    therefore exercised directly with a stub transport.
    """

    async def test_non_loopback_peer_is_refused(self) -> None:
        from model_router.server import loopback_only_middleware

        called = False

        async def handler(_request):
            nonlocal called
            called = True
            return web.Response(text="reached")

        response = await loopback_only_middleware(_FakeRequest("203.0.113.7"), handler)
        self.assertEqual(response.status, 403)
        self.assertFalse(called)

    async def test_undeterminable_peer_is_refused(self) -> None:
        """A gate that cannot confirm its precondition denies."""
        from model_router.server import loopback_only_middleware

        async def handler(_request):
            raise AssertionError("handler reached with an unknown peer")

        response = await loopback_only_middleware(_FakeRequest(None), handler)
        self.assertEqual(response.status, 403)

    async def test_loopback_peer_is_allowed(self) -> None:
        from model_router.server import loopback_only_middleware

        async def handler(_request):
            return web.Response(text="reached")

        for peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            with self.subTest(peer=peer):
                response = await loopback_only_middleware(
                    _FakeRequest(peer), handler,
                )
                self.assertEqual(response.status, 200)


class _FakeTransport:
    def __init__(self, peer):
        self._peer = peer

    def get_extra_info(self, name, default=None):
        if name == "peername":
            return (self._peer, 12345) if self._peer else None
        return default


class _FakeRequest:
    def __init__(self, peer):
        self.transport = _FakeTransport(peer) if peer is not None else None
        self.path = "/health"


def _code_only(path: Path) -> str:
    """Source with comments and docstrings removed.

    The portability scans below must read what the module DOES, not what it
    says about itself: several modules name ``flock`` / ``/proc`` / the
    prototype's old port precisely to record that they do not use them, and a
    naive substring scan would fail on its own documentation.

    Rule: drop COMMENT tokens and triple-quoted STRING tokens (docstrings);
    keep code and ordinary string literals, so a hardcoded path in a real
    literal is still caught.
    """
    import tokenize

    kept: list[str] = []
    with path.open("rb") as handle:
        for tok in tokenize.tokenize(handle.readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.string.lstrip("rbfuRBFU")[:3] in (
                '"""', "'''",
            ):
                continue
            kept.append(tok.string)
    return "\n".join(kept)


class NoPosixOnlyPrimitivesTests(unittest.TestCase):
    """The package must run unchanged on Windows and macOS."""

    FORBIDDEN = (
        "fcntl",
        "flock",
        "os.fork",
        "signal.SIGUSR",
        "/proc/",
        "os.getuid",
        "os.geteuid",
        "pwd.getpwuid",
        "os.O_NONBLOCK",
    )

    def test_no_posix_only_primitive_in_the_package(self) -> None:
        import model_router

        package_dir = Path(model_router.__file__).resolve().parent
        for path in sorted(package_dir.glob("*.py")):
            code = _code_only(path)
            for needle in self.FORBIDDEN:
                with self.subTest(module=path.name, needle=needle):
                    self.assertNotIn(needle, code)

    def test_no_hardcoded_path_separator_joins(self) -> None:
        """The v0.2.81 ``\\``-separator lesson: build paths with pathlib."""
        import model_router

        package_dir = Path(model_router.__file__).resolve().parent
        for path in sorted(package_dir.glob("*.py")):
            code = _code_only(path)
            for needle in ("/tmp/", "os.sep.join", 'joinpath("/'):
                with self.subTest(module=path.name, needle=needle):
                    self.assertNotIn(needle, code)

    def test_no_machine_specific_path_in_package_code(self) -> None:
        import model_router

        package_dir = Path(model_router.__file__).resolve().parent
        for path in sorted(package_dir.glob("*.py")):
            code = _code_only(path)
            for needle in ("/home/", "/Users/", "C:\\Users", "PROGETTI", "8787"):
                with self.subTest(module=path.name, needle=needle):
                    self.assertNotIn(needle, code)

    def test_no_machine_specific_path_in_shipped_data_files(self) -> None:
        """JSON has no comments to exempt, so it is scanned whole."""
        import model_router

        package_dir = Path(model_router.__file__).resolve().parent
        for path in sorted(package_dir.glob("*.json")):
            source = path.read_text(encoding="utf-8")
            for needle in ("/home/", "/Users/", "C:\\Users", "PROGETTI", "8787"):
                with self.subTest(data_file=path.name, needle=needle):
                    self.assertNotIn(needle, source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class VendorAuthStrikeTests(GatewayTestBase):
    """WP-6 review MAJOR-1: ``invalidate()`` was a credited mechanism with
    no production caller — a vendor-rejected key kept serving from the
    last-good store until the 6 h bound. Three consecutive 401/403s now
    invalidate it; one does not (an auth blip must not become an outage);
    any 2xx resets the count."""

    def test_three_consecutive_rejections_invalidate_the_stale_key(self) -> None:
        invalidated: list = []

        def _spy(vendor_id=None):
            invalidated.append(vendor_id)

        gw = self.client.app[APP_KEY]
        gw.keys.invalidate = _spy  # type: ignore[method-assign]
        for _ in range(3):
            gw.note_vendor_auth_rejection("zai")
        self.assertEqual(invalidated, ["zai"])
        self.assertEqual(gw._vendor_auth_strikes, {},
                         "strikes reset so a persistent rejection re-fires")

    def test_one_or_two_rejections_do_not_invalidate(self) -> None:
        invalidated: list = []

        def _spy(vendor_id=None):
            invalidated.append(vendor_id)

        gw = self.client.app[APP_KEY]
        gw.keys.invalidate = _spy  # type: ignore[method-assign]
        gw.note_vendor_auth_rejection("zai")
        gw.note_vendor_auth_rejection("zai")
        self.assertEqual(invalidated, [])
        self.assertEqual(gw._vendor_auth_strikes, {"zai": 2})

    def test_a_success_resets_the_strike_count(self) -> None:
        gw = self.client.app[APP_KEY]
        gw.note_vendor_auth_rejection("zai")
        gw.note_vendor_auth_rejection("zai")
        gw.note_vendor_success("zai")
        gw.note_vendor_auth_rejection("zai")
        self.assertEqual(gw._vendor_auth_strikes, {"zai": 1},
                         "the interleaved 2xx keeps a blip a blip")

    def test_the_echo_edge_set_is_bounded(self) -> None:
        """WP-6 review MINOR-1: ``forwarded`` is client-controlled, so the
        mismatch edge-set must not grow with uptime."""
        gw = self.client.app[APP_KEY]
        for i in range(300):
            gw.note_model_echo_mismatch("zai", f"m{i}", f"r{i}")
        self.assertLessEqual(len(gw._echo_logged), 256)
        self.assertEqual(gw.model_echo_mismatches, 300,
                         "the COUNTER stays exact; only the edge-set bounds")


class QwenVendorTests(GatewayTestBase):
    """The Token-Plan row, end to end: a live list on another base
    (``catalog_url``), the exclude-prefix filter over it, and the
    ``declared`` ``static_ids`` fallback for the keyless and fetch-failed
    moments.

    Built from the REAL shipped row with only the upstreams rewritten to the
    stub — everything else (namespace, exclude prefixes, static ids, key
    names, honest-naming exposure) is the row a user gets.
    """

    #: The compatible-mode list as the live probe served it (2026-09-22):
    #: The one home for these lists: tests/common/qwen_catalog.py.
    #: They were a verbatim second copy here until 2026-09-22.
    LIVE_MODELS = QWEN_LIVE_MODELS
    EXCLUDED = QWEN_EXCLUDED_SIX

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.qwen_up = _Upstream()
        qwen_url = await self.qwen_up.start()
        self.addAsyncCleanup(self.qwen_up.stop)
        # The stub serves both routes on its one base, so the OVERRIDE SHAPE
        # (a list endpoint on another base than the messages one) survives
        # the rewrite; every other field is the shipped row's.
        self.qwen = replace(
            VENDORS["qwen"],
            upstream=qwen_url,
            catalog_url=f"{qwen_url}/v1/models",
        )
        self.both = await self.make_client(vendors={
            self.vendor.vendor_id: self.vendor,
            self.qwen.vendor_id: self.qwen,
        })

    async def test_health_names_the_vendor_and_the_live_source(self) -> None:
        self.qwen_up.models_payload = self.LIVE_MODELS
        body = await (await self.both.get("/health")).json()
        self.assertIn("qwen", body["vendors"])
        # Before any catalog build the source is unfetched like any family...
        self.assertEqual(body["catalog_source"]["qwen"], SOURCE_UNFETCHED)
        # ...and opening the picker fetches the catalog_url override once —
        # the live list answers, and /health says which source applied.
        await self.both.get("/v1/models", headers=self.auth())
        body = await (await self.both.get("/health")).json()
        self.assertEqual(body["catalog_source"]["qwen"], SOURCE_LIVE)
        self.assertEqual(
            [r["path"] for r in self.qwen_up.requests], ["/v1/models"],
        )

    async def test_the_live_list_publishes_the_chat_ids_and_excludes_the_rest(self) -> None:
        self.qwen_up.models_payload = self.LIVE_MODELS
        body = await (await self.both.get("/v1/models", headers=self.auth())).json()
        ids = {
            row["id"] for row in body["data"]
            if row["id"].startswith("claude-gw/qwen/")
        }
        # The owner's advertised four qwen rows (nine declared minus three
        # latest-hidden minus two curated-hidden). The shared glm id carries
        # [1m] — the context table keys the bare id and that id's verified
        # window is 1M, whatever endpoint serves it.
        self.assertEqual(ids, {
            "claude-gw/qwen/qwen3.8-max", "claude-gw/qwen/qwen3.8-flash",
            "claude-gw/qwen/glm-5.3[1m]",
            "claude-gw/qwen/deepseek-v4.1-flash",
        })
        hidden = body["_vct_catalog_hidden"]
        # The three older same-family siblings plus the two curated-hidden
        # ids are withheld, namespaced and REPORTED — the filter narrows the
        # picker, never the router.
        for withheld in (
            "claude-gw/qwen/qwen3.7-max", "claude-gw/qwen/qwen3.6-flash",
            "claude-gw/qwen/glm-5.2[1m]",
            "claude-gw/qwen/qwen3.7-plus", "claude-gw/qwen/deepseek-v4-pro",
        ):
            self.assertIn(withheld, hidden)
        # The six excluded ids (five non-chat modalities plus one dated snapshot) are in NEITHER list: exclusion is not
        # withholding. `hidden` answers "which model am I missing", and a
        # voice/image/router alias is not a model anybody is missing.
        listed = [row["id"] for row in body["data"]] + hidden
        for excluded in self.EXCLUDED:
            for spelling in (
                excluded,
                f"claude-gw/qwen/{excluded}",
                f"claude-gw/qwen/{excluded}[1m]",
            ):
                self.assertNotIn(spelling, listed)

    async def test_a_keyless_picker_serves_the_declared_ids_without_a_fetch(self) -> None:
        keyless = await self.make_client(
            vendors={
                self.vendor.vendor_id: self.vendor,
                self.qwen.vendor_id: self.qwen,
            },
            key_getter=lambda key, project=None: None,
        )
        body = await (await keyless.get("/v1/models", headers=self.auth())).json()
        self.assertEqual(body["_vct_catalog_source"]["qwen"], SOURCE_DECLARED)
        ids = {
            row["id"] for row in body["data"]
            if row["id"].startswith("claude-gw/qwen/")
        }
        self.assertEqual(len(ids), 4)
        self.assertIn("claude-gw/qwen/qwen3.8-max", ids)
        # No key, no fetch — the picker must not require the key to exist.
        self.assertEqual(self.qwen_up.requests, [])

    async def test_a_failed_fetch_falls_back_to_the_declared_ids(self) -> None:
        """Fetch-failure WITH a key: the declared fallback answers, /health
        says which source applied, and the attempt was recorded — unlike the
        keyless path, this one retries on the short TTL."""
        self.qwen_up.models_payload = None  # the stub answers 500
        body = await (await self.both.get("/v1/models", headers=self.auth())).json()
        self.assertEqual(body["_vct_catalog_source"]["qwen"], SOURCE_DECLARED)
        ids = {row["id"] for row in body["data"]}
        self.assertIn("claude-gw/qwen/qwen3.8-max", ids)
        # The curated-hidden ids stay out of the picker under both filters
        # but remain reported and routable by name.
        self.assertIn("claude-gw/qwen/qwen3.7-plus", body["_vct_catalog_hidden"])
        # The default latest-only filter hides the older same-family
        # siblings — declared ids get no exemption from the owner's rule.
        self.assertNotIn("claude-gw/qwen/qwen3.6-flash", ids)
        self.assertIn("claude-gw/qwen/qwen3.6-flash", body["_vct_catalog_hidden"])
        self.assertEqual(len(self.qwen_up.requests), 1)

    async def test_messages_forward_to_the_token_plan_upstream(self) -> None:
        await self.both.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-gw/qwen/qwen3.8-max",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 8,
            },
        )
        self.assertEqual(len(self.qwen_up.message_requests), 1)
        forwarded = json.loads(self.qwen_up.requests[0]["body"])
        self.assertEqual(forwarded["model"], "qwen3.8-max")
        self.assertEqual(
            self.qwen_up.requests[0]["headers"].get("Authorization"),
            f"Bearer {FAKE_VENDOR_KEY}",
        )
        # The other upstream was not touched.
        self.assertEqual(self.vendor_up.message_requests, [])

    async def test_a_bare_id_of_the_other_upstream_routes_here(self) -> None:
        """``deepseek-v4-pro`` is a Token-Plan id; bare on the CLI it must
        land on the Token-Plan row, not on the subscription vendor."""
        await self.both.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "deepseek-v4-pro", "messages": [], "max_tokens": 1},
        )
        self.assertEqual(len(self.qwen_up.message_requests), 1)
        self.assertEqual(self.vendor_up.message_requests, [])


class VendorAuthClassificationTests(GatewayTestBase):
    """The addendum: a vendor 401/403 must be classified before it can feed
    the strike counter.

    A deprecated-but-still-listed model on a no-discovery endpoint answers
    403 ``access_denied`` while the key is perfectly good — three of those
    must NOT invalidate the key. Driven through the real proxy so the
    classification is pinned where it runs, not as a helper unit test.
    """

    async def _post(self) -> Any:
        return await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "max_tokens": 1},
        )

    async def test_a_key_rejecting_401_strikes_and_is_relayed_verbatim(self) -> None:
        self.vendor_up.messages_status = 401
        self.vendor_up.messages_body = {
            "type": "error",
            "error": {"type": "authentication_error", "message": "bad key"},
        }
        gw = self.client.app[APP_KEY]
        response = await self._post()
        self.assertEqual(response.status, 401)
        self.assertEqual(await response.json(), self.vendor_up.messages_body)
        self.assertEqual(gw._vendor_auth_strikes, {"zai": 1})

    async def test_an_oversized_auth_body_is_classified_and_relayed_verbatim(
        self,
    ) -> None:
        """The >64 KiB branch, which shipped untested.

        Two things must both hold when a vendor answers a rejection with a
        body past the hold bound: the classification still happens (from the
        held prefix — an access-denied 403 must NOT strike a healthy key just
        because the vendor was verbose), and the body reaches the client
        BYTE-FOR-BYTE, because editing a vendor's answer is precisely what
        this path exists not to do.
        """
        filler = "x" * (70 * 1024)
        self.vendor_up.messages_status = 403
        self.vendor_up.messages_body = {
            "type": "error",
            "error": {
                "type": "AccessDenied",
                "code": "access_denied",
                "message": "model deprecated",
                "detail": filler,
            },
        }
        gw = self.client.app[APP_KEY]
        with self.assertLogs("model_router.server", level=logging.WARNING) as cap:
            response = await self._post()
            self.assertEqual(response.status, 403)
            body = await response.read()
        # Proves the OVERSIZED path ran, not merely that the body was large:
        # without this the test would pass on the ordinary buffered branch.
        self.assertTrue(
            any("exceeds" in line for line in cap.output),
            f"the >64 KiB branch must announce itself: {cap.output}",
        )
        self.assertGreater(
            len(body), 64 * 1024, "precondition: past the hold bound",
        )
        self.assertEqual(
            json.loads(body), self.vendor_up.messages_body,
            "relayed verbatim — the gateway never edits a vendor's answer",
        )
        self.assertEqual(
            gw._vendor_auth_strikes, {},
            "classified from the held prefix: a model-level 403 is not "
            "evidence about the key, however long the body is",
        )

    async def test_an_oversized_key_level_rejection_still_strikes(self) -> None:
        """The other half of the same gate: size must not LOSE a real
        signal either. A 401 past the bound still counts against the key."""
        self.vendor_up.messages_status = 401
        self.vendor_up.messages_body = {
            "type": "error",
            "error": {
                "type": "authentication_error",
                "message": "bad key",
                "detail": "y" * (70 * 1024),
            },
        }
        gw = self.client.app[APP_KEY]
        response = await self._post()
        self.assertEqual(response.status, 401)
        self.assertEqual(gw._vendor_auth_strikes, {"zai": 1})

    async def test_a_403_of_an_unrecognised_shape_strikes(self) -> None:
        """Only the DOCUMENTED model-level shape is exempt; anything else is
        treated as evidence about the key (the pre-addendum behaviour)."""
        self.vendor_up.messages_status = 403
        self.vendor_up.messages_body = {
            "type": "error",
            "error": {"type": "permission_error", "message": "no access"},
        }
        gw = self.client.app[APP_KEY]
        response = await self._post()
        self.assertEqual(response.status, 403)
        self.assertEqual(gw._vendor_auth_strikes, {"zai": 1})

    async def test_a_model_level_403_does_not_strike_even_three_times(self) -> None:
        """The trap the classifier exists to defuse: the deprecated-model
        answer relayed verbatim, the strike counter untouched, three times
        over — the count that WOULD have invalidated a healthy key."""
        self.vendor_up.messages_status = 403
        self.vendor_up.messages_body = {
            "type": "error",
            "error": {
                "type": "AccessDenied",
                "code": "access_denied",
                "message": "model deprecated",
            },
        }
        invalidated: list = []

        def _spy(vendor_id=None):
            invalidated.append(vendor_id)

        gw = self.client.app[APP_KEY]
        gw.keys.invalidate = _spy  # type: ignore[method-assign]
        for _ in range(3):
            response = await self._post()
            self.assertEqual(response.status, 403)
            self.assertEqual(await response.json(), self.vendor_up.messages_body)
        self.assertEqual(gw._vendor_auth_strikes, {})
        self.assertEqual(invalidated, [])

    async def test_a_model_level_403_is_no_evidence_in_either_direction(self) -> None:
        """Neither strike nor success: an access-denied 403 must leave a
        pending strike count alone. Seed one real key-level strike, then
        interleave the model-level shape twice — the count neither grows
        (three would invalidate) nor resets (which would mask a dying key
        as healthy)."""
        gw = self.client.app[APP_KEY]
        self.vendor_up.messages_status = 401
        self.vendor_up.messages_body = {
            "type": "error",
            "error": {"type": "authentication_error", "message": "bad key"},
        }
        await self._post()
        self.assertEqual(gw._vendor_auth_strikes, {"zai": 1})

        self.vendor_up.messages_status = 403
        self.vendor_up.messages_body = {
            "type": "error",
            "error": {"type": "AccessDenied", "code": "access_denied"},
        }
        await self._post()
        await self._post()
        self.assertEqual(
            gw._vendor_auth_strikes, {"zai": 1},
            "the model-level 403s neither counted nor reset the strike",
        )
