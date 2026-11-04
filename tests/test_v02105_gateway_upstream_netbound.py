# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — every pre-header phase of an upstream request is bounded.

The 2026-11-04 03:08 incident: DNS resolution failed transiently, the
gateway answered 502s the clients retried, and then one retry was ACCEPTED
and never answered — no access line, its TCP connection ESTABLISHED for
hours, the chat hung until VS Code was restarted. Root cause: the upstream
request used ``aiohttp.ClientTimeout(total=None, sock_connect=30,
sock_read=idle)``. ``sock_connect`` bounds ONLY the TCP handshake; DNS
resolution and connection-pool acquisition are bounded solely by
``connect`` (unset) and ``total`` (deliberately ``None``, because a healthy
completion may stream for many minutes). A resolution that STALLED instead
of failing therefore had no bound at all.

This file pins the three properties the fix must keep simultaneously:

1. a stalled resolver/connector ENDS within the bound with the retryable
   502 (never a silent hang);
2. a transient failure that recovers within the retry window is invisible
   to the client (200, one WARNING per episode, one INFO on recovery);
3. nothing AFTER the response headers is retried or cut — a long streaming
   body outlives the headers bound untouched, and a body that dies
   mid-stream is never re-sent.

The bounds are set on the config by name (``hasattr``-guarded) so the same
test run against the PRE-FIX source demonstrates the hang itself: the
fields do not exist there, the request keeps the old unbounded behaviour,
and the test's own ``wait_for`` is what fails.
"""

from __future__ import annotations

import asyncio
import socket
import unittest
from dataclasses import replace
from typing import Any

import aiohttp
from aiohttp import web

from model_router.server import APP_KEY
from tests.test_model_router_server import GatewayTestBase, _Upstream

#: Logger name in model_router.server is its module ``__name__``.
LOGGER = "model_router.server"


class _NeverResolver:
    """A resolver whose getaddrinfo never returns — the incident's stall."""

    async def resolve(self, host: str, port: int = 0, family: int = 0) -> list[dict[str, Any]]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover


class _FlakyResolver:
    """Fails N resolutions with an OSError, then resolves normally."""

    def __init__(self, failures: int) -> None:
        self._left = failures

    async def resolve(self, host: str, port: int = 0, family: int = 0) -> list[dict[str, Any]]:
        if self._left > 0:
            self._left -= 1
            raise OSError("simulated name-resolution failure")
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM,
        )
        return [
            {
                "hostname": host,
                "host": info[4][0],
                "port": info[4][1],
                "family": info[0],
                "proto": info[1],
                "flags": info[2],
            }
            for info in infos
        ]


class _DyingUpstream(_Upstream):
    """Sends response headers and one chunk, then kills the connection."""

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        await self._record(request)
        response = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream"},
        )
        response.enable_chunked_encoding()
        await response.prepare(request)
        await response.write(b"data: {}\n\n")
        if request.transport is not None:
            request.transport.abort()
        return response


class NetboundTestBase(GatewayTestBase):
    """The stock base, plus bound-setting and resolver injection helpers."""

    def _set_bound(self, name: str, value: float) -> None:
        """Set a v0.2.105 config field, skipping it on pre-fix source.

        The skip is what makes the red-proof honest: against the source
        before the fix the request keeps the unbounded behaviour and the
        test fails by hanging into its own ``wait_for``, not by tripping
        over a missing attribute.
        """
        if hasattr(self.config, name):
            setattr(self.config, name, value)

    def _localhost_vendors(self) -> dict[str, Any]:
        """The base vendor with its upstream on ``localhost``.

        aiohttp's connector short-circuits IP-literal hosts without ever
        consulting the resolver, so the resolver injection below is only
        observable against a NAME.
        """
        local = self.vendor.upstream.replace("127.0.0.1", "localhost")
        return {self.vendor.vendor_id: replace(self.vendor, upstream=local)}

    async def _client_with_resolver(
        self, resolver: Any, *, vendors: dict[str, Any] | None = None,
    ) -> Any:
        client = await self.make_client(
            vendors=vendors if vendors is not None else {"zai": self.vendor},
        )
        gateway = client.app[APP_KEY]
        await gateway._session.close()
        gateway._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=resolver),
        )
        return client

    def _post(self, client: Any, **extra: Any) -> Any:
        return client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
            **extra,
        )


class StalledAcquisitionTests(NetboundTestBase):
    """The incident itself: a resolution that stalls instead of failing."""

    async def test_a_stalled_resolver_ends_within_the_bound_with_the_retryable_error(self) -> None:
        # Headers bound well inside the test's own wait_for; retry window 0
        # so ONE stalled attempt is the whole story.
        self._set_bound("upstream_headers_timeout_s", 0.4)
        self._set_bound("upstream_retry_window_s", 0)
        client = await self._client_with_resolver(
            _NeverResolver(), vendors=self._localhost_vendors(),
        )
        # The test's own bound is the red: pre-fix, nothing bounds the
        # stalled resolution and this wait_for is what fails.
        with self.assertLogs(LOGGER, level="WARNING"):
            resp = await asyncio.wait_for(self._post(client), timeout=6)
        self.assertEqual(resp.status, 502)
        # The two answers a client retries on — the incident's session was
        # never given either.
        self.assertEqual(resp.headers.get("x-should-retry"), "true")
        self.assertIn("Retry-After", resp.headers)
        body = await resp.json()
        self.assertIn("upstream", body["error"]["message"])

    async def test_a_stalled_catalog_fetch_stays_bounded_too(self) -> None:
        """Every upstream call site is bounded — the catalog's ``total``
        covers DNS as well, so a stalled resolver degrades to the static
        fallback instead of hanging the picker."""
        self.config.catalog_timeout_s = 1
        client = await self._client_with_resolver(
            _NeverResolver(), vendors=self._localhost_vendors(),
        )
        resp = await asyncio.wait_for(
            client.get("/v1/models", headers=self.auth()), timeout=6,
        )
        self.assertEqual(resp.status, 200)

    async def test_health_shows_a_stuck_acquisition_by_name_and_age(self) -> None:
        """The diagnostic the incident lacked: while one request is stuck
        reaching an upstream, /health says so — count, age, name."""
        self._set_bound("upstream_connect_timeout_s", 1.0)
        self._set_bound("upstream_retry_window_s", 0)
        self._set_bound("upstream_headers_timeout_s", 3.0)
        client = await self._client_with_resolver(
            _NeverResolver(), vendors=self._localhost_vendors(),
        )
        stuck = asyncio.create_task(asyncio.wait_for(self._post(client), timeout=10))
        await asyncio.sleep(0.3)
        body = await (await client.get("/health")).json()
        flight = body["upstream_in_flight"]
        self.assertGreaterEqual(flight["count"], 1)
        self.assertGreaterEqual(flight["oldest_age_s"], 0.0)
        self.assertEqual(flight["oldest_label"], "zai")
        resp = await stuck
        self.assertEqual(resp.status, 502)
        body = await (await client.get("/health")).json()
        self.assertEqual(body["upstream_in_flight"]["count"], 0)
        self.assertIsNone(body["upstream_in_flight"]["oldest_age_s"])
        self.assertIsNone(body["upstream_in_flight"]["oldest_label"])


class TransientRetryTests(NetboundTestBase):
    """A brief outage is invisible to the chat."""

    async def test_failures_that_recover_within_the_window_answer_200(self) -> None:
        self._set_bound("upstream_retry_window_s", 5)
        self._set_bound("upstream_headers_timeout_s", 5)
        client = await self._client_with_resolver(
            _FlakyResolver(failures=2), vendors=self._localhost_vendors(),
        )
        # INFO captures the WARNING above it (assertLogs is level->floor).
        with self.assertLogs(LOGGER, level="INFO") as logs:
            resp = await asyncio.wait_for(self._post(client), timeout=15)
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.vendor_up.message_requests), 1)
        # ONE warning per episode (not per attempt: two attempts failed),
        # one recovery INFO — and nothing at ERROR. (assertLogs prefixes
        # each captured record with its level and logger.)
        warnings = [line for line in logs.output if line.startswith("WARNING:")]
        infos = [line for line in logs.output if line.startswith("INFO:")]
        self.assertEqual(
            len([w for w in warnings if "transient network failure" in w]), 1,
            logs.output,
        )
        self.assertEqual(
            len([i for i in infos if "reachable again" in i]), 1, logs.output,
        )
        self.assertFalse(
            [e for e in logs.output if e.startswith("ERROR:")], logs.output,
        )


class NoRetryAfterHeadersTests(NetboundTestBase):
    """Retrying stops where the response starts."""

    async def test_a_stream_that_dies_after_headers_is_never_resent(self) -> None:
        # A generous retry window is the point: if anything retried a
        # post-headers failure, this count would say so.
        self._set_bound("upstream_retry_window_s", 5)
        self._set_bound("upstream_headers_timeout_s", 5)
        dying = _DyingUpstream()
        url = await dying.start()
        self.addAsyncCleanup(dying.stop)
        vendors = {self.vendor.vendor_id: replace(self.vendor, upstream=url)}
        client = await self._client_with_resolver(_FlakyResolver(0), vendors=vendors)
        resp = await asyncio.wait_for(self._post(client), timeout=10)
        self.assertEqual(resp.status, 200)
        with self.assertRaises(aiohttp.ClientPayloadError):
            await resp.read()
        # Headers had arrived, bytes had started: one attempt, no retry.
        self.assertEqual(len(dying.message_requests), 1)

    async def test_a_long_streaming_body_outlives_the_headers_bound_untouched(self) -> None:
        # Headers bound a FRACTION of the stream's total length: the bound
        # must apply to reaching the headers only, never to the body.
        self._set_bound("upstream_headers_timeout_s", 0.4)
        self._set_bound("upstream_retry_window_s", 0)
        self.vendor_up.stream_chunks = [
            f'data: {{"i": {i}}}\n\n'.encode() for i in range(40)
        ]
        self.vendor_up.stream_gap_s = 0.05  # ~2 s of streaming, gaps << idle bound
        client = await self.make_client()
        resp = await asyncio.wait_for(self._post(client), timeout=15)
        self.assertEqual(resp.status, 200)
        body = await resp.read()
        self.assertEqual(body.count(b'"i":'), 40)
        # And it really was longer than the headers bound.
        self.assertGreater(len(body), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
