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
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from aiohttp import ClientTimeout, web
from aiohttp.test_utils import TestClient, TestServer

from model_router.auth import OAuthReader
from model_router.catalog import SOURCE_LIVE, SOURCE_STATIC, SOURCE_UNFETCHED
from model_router.config import GatewayConfig
from model_router.secrets import VendorKeyResolver
from model_router.server import create_app
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
            vendors={self.vendor.vendor_id: self.vendor},
            anthropic=self.anthropic,
            oauth_reader=OAuthReader(self.credentials),
            key_resolver=VendorKeyResolver(
                getter=overrides.pop("key_getter", self.key_getter),
            ),
            token_permissions=overrides.pop("token_permissions", "owner_only"),
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
        "context_table_source", "context_table_path", "oauth_present",
        "oauth_state", "oauth_expires_in_s", "vendors", "vendor_keys_cached",
        "token_file_permissions",
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


class CatalogSurfaceTests(GatewayTestBase):
    async def test_union_catalog_namespaces_only_the_vendor_entries(self) -> None:
        self.anthropic_up.models_payload = {
            "data": [{"id": "claude-opus-5", "display_name": "Opus 5"}],
        }
        self.vendor_up.models_payload = {"data": [{"id": "glm-5.3"}, {"id": "glm-5.1"}]}
        body = await (await self.client.get("/v1/models", headers=self.auth())).json()
        ids = [row["id"] for row in body["data"]]
        self.assertIn("claude-opus-5", ids)
        self.assertIn("claude-gw/glm-5.3[1m]", ids)
        self.assertIn("claude-gw/glm-5.1", ids)

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
        self.assertEqual(forwarded, {"glm-5.3", "glm-5.1", "glm-4.5-air"})

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
