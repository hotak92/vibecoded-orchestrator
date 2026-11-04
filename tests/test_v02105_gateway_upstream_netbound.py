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
import logging
import os
import socket
import time
import unittest
from dataclasses import replace
from typing import Any
from unittest import mock

import aiohttp
from aiohttp import web

from model_router import server as server_mod
from model_router.config import (
    DEFAULT_UPSTREAM_IDLE_TIMEOUT_S,
    DEFAULT_UPSTREAM_RETRY_WINDOW_S,
    UPSTREAM_HEADERS_TIMEOUT_ENV,
    UPSTREAM_HEADERS_TIMEOUT_MAX_S,
    UPSTREAM_RETRY_WINDOW_ENV,
    UPSTREAM_RETRY_WINDOW_MAX_S,
    UPSTREAM_UPLOAD_ALLOWANCE_S,
    GatewayConfig,
)
from model_router.server import APP_KEY
from tests.test_model_router_server import GatewayTestBase, _Upstream
from vco_lib.env_values import env_float

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


class _ReceivedThenResetUpstream(_Upstream):
    """Reads (and records) the WHOLE request, then aborts the TCP connection
    before sending any header — the shape in which the upstream already has,
    and may bill, the request. A gateway that re-sends here duplicates it."""

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        await self._record(request)
        if request.transport is not None:
            request.transport.abort()
        await asyncio.sleep(1)
        return web.Response(status=500)  # never reaches the client


class _SilentUpstream(_Upstream):
    """Reads the whole request, then says nothing for ``delay_s`` seconds
    before answering (non-streaming: headers only with the whole answer)."""

    def __init__(self, delay_s: float) -> None:
        super().__init__()
        self.delay_s = delay_s

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        await self._record(request)
        await asyncio.sleep(self.delay_s)
        return web.json_response(self.messages_body)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


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

    async def _client_for(self, upstream: _Upstream) -> Any:
        url = await upstream.start()
        self.addAsyncCleanup(upstream.stop)
        vendors = {self.vendor.vendor_id: replace(self.vendor, upstream=url)}
        return await self.make_client(vendors=vendors)

    def _post(self, client: Any, **extra: Any) -> Any:
        return client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
            **extra,
        )


class StalledAcquisitionTests(NetboundTestBase):
    """The incident itself: a resolution that stalls instead of failing."""

    async def test_a_stalled_resolver_ends_within_the_connect_bound_as_unreachable(self) -> None:
        # ONLY the connect bound can end this: the headers bound is left
        # far above the test's own wait, and the retry window is 0 so ONE
        # stalled attempt is the whole story. Remove `connect=` from the
        # request's ClientTimeout and this test hangs into its wait_for —
        # the 2026-11-04 incident, pinned.
        self._set_bound("upstream_connect_timeout_s", 0.3)
        self._set_bound("upstream_headers_timeout_s", 60)
        self._set_bound("upstream_retry_window_s", 0)
        client = await self._client_with_resolver(
            _NeverResolver(), vendors=self._localhost_vendors(),
        )
        started = time.monotonic()
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            resp = await asyncio.wait_for(self._post(client), timeout=6)
        elapsed = time.monotonic() - started
        self.assertEqual(resp.status, 502)
        self.assertLess(elapsed, 2.0)
        # The two answers a client retries on — the incident's session was
        # never given either.
        self.assertEqual(resp.headers.get("x-should-retry"), "true")
        self.assertIn("Retry-After", resp.headers)
        message = (await resp.json())["error"]["message"]
        # The phase is named honestly: never reached, after the CONNECT
        # bound — not "no response headers within <headers bound>".
        self.assertIn("unreachable: no connection within 0.3s", message)
        self.assertIn("connection pool, DNS or TCP connect", message)
        self.assertNotIn("response headers", message)
        self.assertFalse(
            [line for line in logs.output if "response headers" in line],
            logs.output,
        )

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
        # The connect bound is what ends it (headers far above the wait).
        self._set_bound("upstream_connect_timeout_s", 1.0)
        self._set_bound("upstream_retry_window_s", 0)
        self._set_bound("upstream_headers_timeout_s", 60)
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
            len([w for w in warnings if "unreachable (" in w]), 1,
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
        started = time.monotonic()
        resp = await asyncio.wait_for(self._post(client), timeout=15)
        self.assertEqual(resp.status, 200)
        body = await resp.read()
        elapsed = time.monotonic() - started
        self.assertEqual(body.count(b'"i":'), 40)
        # And it really did outlive the headers bound.
        self.assertGreater(elapsed, 0.4 * 2)


class NoResendOnceSentTests(NetboundTestBase):
    """H1: a failure after the request may have reached the upstream is
    answered at once — never re-sent, whatever the retry window."""

    async def test_an_upstream_that_received_the_body_then_reset_gets_one_copy(self) -> None:
        self._set_bound("upstream_retry_window_s", 3)
        upstream = _ReceivedThenResetUpstream()
        client = await self._client_for(upstream)
        started = time.monotonic()
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            resp = await asyncio.wait_for(self._post(client), timeout=10)
        elapsed = time.monotonic() - started
        self.assertEqual(resp.status, 502)
        # Exactly ONE upstream copy: the upstream had the whole request.
        self.assertEqual(len(upstream.message_requests), 1)
        # Answered at once, not after the retry window.
        self.assertLess(elapsed, 2.0)
        self.assertEqual(resp.headers.get("x-should-retry"), "true")
        message = (await resp.json())["error"]["message"]
        self.assertIn("dropped the connection before answering", message)
        self.assertFalse(
            [line for line in logs.output if "retrying inside" in line],
            logs.output,
        )

    async def test_a_headers_timeout_after_the_body_was_sent_is_not_resent(self) -> None:
        # The review's second shape: headers bound below the retry window.
        self._set_bound("upstream_headers_timeout_s", 0.5)
        self._set_bound("upstream_retry_window_s", 3)
        upstream = _SilentUpstream(delay_s=5)
        client = await self._client_for(upstream)
        resp = await asyncio.wait_for(self._post(client), timeout=10)
        self.assertEqual(resp.status, 502)
        self.assertEqual(len(upstream.message_requests), 1)
        message = (await resp.json())["error"]["message"]
        self.assertIn("sent no response headers within 0.5s", message)

    async def test_an_idle_bound_before_the_headers_says_went_quiet(self) -> None:
        # sock_read (the idle bound) firing before any header byte: its own
        # words, not the headers-bound wording, and never re-sent.
        self.config.upstream_idle_timeout_s = 0.4
        self._set_bound("upstream_retry_window_s", 3)
        upstream = _SilentUpstream(delay_s=5)
        client = await self._client_for(upstream)
        resp = await asyncio.wait_for(self._post(client), timeout=10)
        self.assertEqual(resp.status, 502)
        self.assertEqual(len(upstream.message_requests), 1)
        message = (await resp.json())["error"]["message"]
        self.assertIn("sent nothing for 0.4s", message)


class PreSendClassificationTests(unittest.TestCase):
    """The one predicate that decides what may be re-sent."""

    @staticmethod
    def _bare(exc_type: "type[BaseException]") -> BaseException:
        # isinstance is all the predicate reads; aiohttp's constructors need
        # a live connection key the table does not.
        factory: Any = exc_type
        return factory.__new__(exc_type)

    def test_only_never_connected_failures_are_retryable(self) -> None:
        retryable = [
            aiohttp.ClientConnectorError,
            aiohttp.ClientConnectorDNSError,
            aiohttp.ConnectionTimeoutError,
        ]
        never = [
            aiohttp.ServerDisconnectedError,
            aiohttp.ClientOSError,
            aiohttp.SocketTimeoutError,
            aiohttp.ServerTimeoutError,
            aiohttp.ClientConnectionResetError,
            aiohttp.ClientSSLError,
            aiohttp.ClientConnectorCertificateError,
            aiohttp.ClientConnectorSSLError,
            aiohttp.ClientPayloadError,
            server_mod.UpstreamHeadersTimeout,
            ConnectionResetError,
            OSError,
        ]
        for cls in retryable:
            with self.subTest(cls=cls.__name__):
                self.assertTrue(server_mod._is_pre_send_failure(self._bare(cls)))
        for cls in never:
            with self.subTest(cls=cls.__name__):
                self.assertFalse(server_mod._is_pre_send_failure(self._bare(cls)))


class HeadersBoundDefaultTests(NetboundTestBase):
    """M2: the headers bound never cuts a non-streaming completion earlier
    than the idle bound did before it existed."""

    def test_the_default_is_the_idle_bound_plus_the_upload_allowance(self) -> None:
        clean = {
            k: v for k, v in os.environ.items()
            if k not in (UPSTREAM_HEADERS_TIMEOUT_ENV, UPSTREAM_RETRY_WINDOW_ENV)
        }
        with mock.patch.dict(os.environ, clean, clear=True):
            from_env = GatewayConfig.from_env()
        for name, config in (("plain", GatewayConfig()), ("from_env", from_env)):
            with self.subTest(config=name):
                self.assertIsNone(config.upstream_headers_timeout_s)
                self.assertEqual(
                    config.headers_timeout_s(),
                    DEFAULT_UPSTREAM_IDLE_TIMEOUT_S + UPSTREAM_UPLOAD_ALLOWANCE_S,
                )
                self.assertGreaterEqual(config.headers_timeout_s(), 1800)

    def test_the_default_follows_the_idle_bound(self) -> None:
        config = GatewayConfig(upstream_idle_timeout_s=7)
        self.assertEqual(config.headers_timeout_s(), 7 + UPSTREAM_UPLOAD_ALLOWANCE_S)
        self.assertEqual(GatewayConfig(upstream_headers_timeout_s=3).headers_timeout_s(), 3)

    async def test_a_slow_non_streaming_answer_inside_the_idle_bound_is_not_cut(self) -> None:
        # Headers bound left at its default: a completion that thinks for
        # most of the idle bound before its headers must arrive.
        self.config.upstream_idle_timeout_s = 1.5
        self.assertIsNone(self.config.upstream_headers_timeout_s)
        upstream = _SilentUpstream(delay_s=1.0)
        client = await self._client_for(upstream)
        resp = await asyncio.wait_for(self._post(client), timeout=10)
        self.assertEqual(resp.status, 200, await resp.text())
        self.assertEqual(len(upstream.message_requests), 1)


class EnvKnobRangeTests(unittest.TestCase):
    """m2: no env value can make either bound unbounded."""

    def _config(self, **env: str) -> GatewayConfig:
        with mock.patch.dict(os.environ, env):
            return GatewayConfig.from_env()

    def test_non_finite_and_out_of_range_values_keep_the_defaults(self) -> None:
        bad = ["inf", "-inf", "nan", "1e12", "-1", "garbage", ""]
        for raw in bad:
            with self.subTest(raw=raw):
                config = self._config(**{
                    UPSTREAM_HEADERS_TIMEOUT_ENV: raw,
                    UPSTREAM_RETRY_WINDOW_ENV: raw,
                })
                self.assertIsNone(config.upstream_headers_timeout_s)
                self.assertEqual(
                    config.upstream_retry_window_s, DEFAULT_UPSTREAM_RETRY_WINDOW_S,
                )
        self.assertIsNone(self._config(**{UPSTREAM_HEADERS_TIMEOUT_ENV: "0"})
                          .upstream_headers_timeout_s)
        self.assertIsNone(self._config(**{
            UPSTREAM_HEADERS_TIMEOUT_ENV: str(UPSTREAM_HEADERS_TIMEOUT_MAX_S + 1),
        }).upstream_headers_timeout_s)
        self.assertEqual(self._config(**{
            UPSTREAM_RETRY_WINDOW_ENV: str(UPSTREAM_RETRY_WINDOW_MAX_S + 1),
        }).upstream_retry_window_s, DEFAULT_UPSTREAM_RETRY_WINDOW_S)

    def test_in_range_values_are_used(self) -> None:
        config = self._config(**{
            UPSTREAM_HEADERS_TIMEOUT_ENV: "900.5",
            UPSTREAM_RETRY_WINDOW_ENV: "0",
        })
        self.assertEqual(config.headers_timeout_s(), 900.5)
        self.assertEqual(config.upstream_retry_window_s, 0)
        config = self._config(**{
            UPSTREAM_HEADERS_TIMEOUT_ENV: str(UPSTREAM_HEADERS_TIMEOUT_MAX_S),
            UPSTREAM_RETRY_WINDOW_ENV: str(UPSTREAM_RETRY_WINDOW_MAX_S),
        })
        self.assertEqual(config.headers_timeout_s(), UPSTREAM_HEADERS_TIMEOUT_MAX_S)
        self.assertEqual(config.upstream_retry_window_s, UPSTREAM_RETRY_WINDOW_MAX_S)

    def test_env_float_finite_flag(self) -> None:
        # The shared reader's default behaviour is unchanged (its documented
        # copies must keep matching); `finite=True` is the opt-in.
        self.assertEqual(env_float("X", 1.0, env={"X": "inf"}, minimum=0), float("inf"))
        self.assertEqual(env_float("X", 1.0, env={"X": "inf"}, minimum=0, finite=True), 1.0)
        self.assertEqual(env_float("X", 1.0, env={"X": "nan"}, finite=True), 1.0)
        self.assertEqual(env_float("X", 1.0, env={"X": "-inf"}, finite=True), 1.0)
        self.assertEqual(env_float("X", 1.0, env={"X": "2.5"}, finite=True), 2.5)


class OutageEpisodeTests(unittest.TestCase):
    """m4: one WARNING per outage, and a later outage always gets its own."""

    def setUp(self) -> None:
        self.clock = _FakeClock()
        self.outages = server_mod._UpstreamOutages(clock=self.clock)
        self.exc = OSError("x")

    def _run(self, steps: list[str]) -> list[str]:
        with self.assertLogs(LOGGER, level="DEBUG") as logs:
            logging.getLogger(LOGGER).debug("start")
            for step in steps:
                if step == "fail":
                    self.outages.failure("zai", self.exc)
                elif step == "ok":
                    self.outages.recovery("zai")
                else:
                    self.clock.now += float(step)
        return [
            line.split(":", 1)[0] for line in logs.output
            if "start" not in line
        ]

    def test_two_outages_without_a_success_log_two_warnings(self) -> None:
        # Every request of the first outage gave up (window 0): no success
        # ever closed it, and the second must still be announced.
        levels = self._run(["fail", "1", "fail", "61", "fail"])
        self.assertEqual(levels, ["WARNING", "DEBUG", "WARNING"])

    def test_two_outages_with_recoveries_log_two_warnings_and_two_infos(self) -> None:
        levels = self._run(["fail", "ok", "61", "fail", "ok"])
        self.assertEqual(levels, ["WARNING", "INFO", "WARNING", "INFO"])

    def test_concurrent_failures_around_a_success_do_not_flap(self) -> None:
        levels = self._run(["fail", "1", "ok", "1", "fail", "1", "ok"])
        self.assertEqual(levels, ["WARNING", "INFO", "DEBUG"])

    def test_a_success_long_after_the_outage_announces_nothing(self) -> None:
        self.assertEqual(self._run(["fail", "61", "ok"]), ["WARNING"])
        self.assertEqual(self._run(["ok"]), [])


class OutageEpisodeWiringTests(NetboundTestBase):
    """m4 through the real request path: two outages, two WARNINGs."""

    async def test_two_outages_through_the_gateway_log_two_warnings(self) -> None:
        self._set_bound("upstream_retry_window_s", 0)
        client = await self._client_with_resolver(
            _FlakyResolver(failures=10), vendors=self._localhost_vendors(),
        )
        clock = _FakeClock()
        client.app[APP_KEY].upstream_outages._clock = clock
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            first = await asyncio.wait_for(self._post(client), timeout=10)
            clock.now += server_mod._UPSTREAM_OUTAGE_QUIET_S + 1
            second = await asyncio.wait_for(self._post(client), timeout=10)
        self.assertEqual((first.status, second.status), (502, 502))
        opened = [line for line in logs.output if "nothing was sent upstream" in line]
        self.assertEqual(len(opened), 2, logs.output)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
