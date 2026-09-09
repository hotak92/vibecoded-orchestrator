# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Through the gateway is never worse than native.

That is the whole invariant, and on 2026-09-09 it did not hold: a stream was
cut at 601 s by a total timeout no upstream has, a body was refused at 1 MiB,
a rewrite bug would have become a 500 the SDK retries ten times at the user's
expense, and a stream that broke mid-flight was closed CLEANLY — so the client
read a truncated answer as a complete one and never retried.

Every case here is a hostile upstream or a hostile body, and every assertion
is "the client got what it would have got talking to that same upstream
directly". Where the gateway is allowed to differ (a namespaced model id, a
repaired tool id) the difference is spelled out.

The stubs are real aiohttp servers on loopback (``tests/test_model_router_server``
supplies them), so timeouts, chunking and header handling are exercised rather
than mocked.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

import aiohttp
from aiohttp import web

from model_router import server as srv
from model_router.__main__ import SERVER_LIMITS
from tests.test_model_router_server import FAKE_OAUTH, GatewayTestBase, _Upstream
from tests.test_v0294_gateway_access_log import LOGGER, _fields

#: Past aiohttp's 1 MiB default, inside the 32 MiB rewrite buffer.
BIG = 2 * 1024 * 1024
#: Past the rewrite buffer as well: the streamed path, at a size a real
#: transcript with images reaches.
HUGE = 40 * 1024 * 1024
STUB_LIMIT = 64 * 1024 * 1024


class _ChaosUpstream(_Upstream):
    """The shared stub, with modes for the ways an upstream misbehaves.

    A subclass rather than a second fake: every other property under test
    (header filtering, chunking, the recorded body) has to keep matching the
    stub the rest of the suite uses, and a private copy would drift.
    """

    def __init__(self) -> None:
        super().__init__()
        #: ``None`` | ``abort_mid_stream`` | ``slow_stream`` | ``stall``
        self.mode: str | None = None
        self.slow_events = 0
        self.slow_gap_s = 0.0
        self.catalog_delay_s = 0.0
        self.extra_headers: dict[str, str] = {}
        #: When set, ANY other bearer is answered 401 — the shape of a login
        #: that expired while a session was running.
        self.accept_bearer: str | None = None
        #: Called as the stub answers, so a test can rotate the credentials
        #: file MID-FLIGHT rather than before the request.
        self.on_request = None

    def app(self) -> web.Application:
        app = super().app()
        # The stubs carry aiohttp's own 1 MiB default; the bodies under test
        # are deliberately larger, and a 413 from a STUB would pass these
        # tests for the wrong reason.
        app._client_max_size = STUB_LIMIT
        return app

    async def start(self) -> str:
        """Serve with the daemon's own parser limits.

        `api.anthropic.com` accepts the 16 KiB `anthropic-beta` a real
        session sends; a stub on aiohttp's 8190-byte default does not, and
        its 400 — correctly relayed — would read as the GATEWAY refusing the
        header it had just been fixed to accept.
        """
        from aiohttp.test_utils import TestServer

        self.server = TestServer(self.app())
        await self.server.start_server(**SERVER_LIMITS)
        return str(self.server.make_url("")).rstrip("/")

    async def _models(self, request: web.Request) -> web.Response:
        if self.catalog_delay_s:
            await asyncio.sleep(self.catalog_delay_s)
        return await super()._models(request)

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        if self.accept_bearer is not None and request.headers.get(
            "Authorization", "",
        ) != f"Bearer {self.accept_bearer}":
            await self._record(request)
            if self.on_request is not None:
                self.on_request()
            return web.json_response(
                {"type": "error", "error": {"type": "authentication_error"}},
                status=401,
            )
        if self.mode == "abort_mid_stream":
            await self._record(request)
            response = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"},
            )
            response.enable_chunked_encoding()
            await response.prepare(request)
            await response.write(b"event: message_start\ndata: {}\n\n")
            # Kill the connection without an end: the shape of a real
            # upstream dying mid-answer.
            transport = request.transport
            if transport is not None:
                transport.abort()
            raise ConnectionResetError("stub aborted the stream")
        if self.mode == "slow_stream":
            await self._record(request)
            response = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"},
            )
            response.enable_chunked_encoding()
            await response.prepare(request)
            for i in range(self.slow_events):
                await response.write(
                    f"event: ping\ndata: {{\"i\":{i}}}\n\n".encode(),
                )
                await asyncio.sleep(self.slow_gap_s)
            await response.write(b"event: message_stop\ndata: {}\n\n")
            await response.write_eof()
            return response
        response = await super()._messages(request)
        # Headers an SDK acts on ride on the ordinary (non-stream) answers.
        # Attached here rather than by re-registering a route, because the
        # stub's router is frozen once it is serving.
        if self.extra_headers and isinstance(response, web.Response):
            response.headers.update(self.extra_headers)
        return response


def _payload(nbytes: int, *, model: str = "claude-gw/glm-5.3", char: str = "x") -> bytes:
    return json.dumps(
        {
            "model": model,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": char * nbytes}],
        }
    ).encode("utf-8")


class ChaosBase(GatewayTestBase):
    """`GatewayTestBase`, with chaos-capable stubs on both upstreams."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.vendor_up.stop()
        await self.anthropic_up.stop()
        self.vendor_up = _ChaosUpstream()
        self.anthropic_up = _ChaosUpstream()
        vendor_url = await self.vendor_up.start()
        anthropic_url = await self.anthropic_up.start()
        self.addAsyncCleanup(self.vendor_up.stop)
        self.addAsyncCleanup(self.anthropic_up.stop)
        self.vendor = type(self.vendor)(
            **{
                **{f: getattr(self.vendor, f) for f in self.vendor.__dataclass_fields__},
                "upstream": vendor_url,
            }
        )
        self.anthropic = type(self.anthropic)(
            **{
                **{
                    f: getattr(self.anthropic, f)
                    for f in self.anthropic.__dataclass_fields__
                },
                "upstream": anthropic_url,
            }
        )
        self.client = await self.make_client()

    def headers(self, **extra: str) -> dict[str, str]:
        return {**self.auth(), "Content-Type": "application/json", **extra}


# ── 1, 13 — bodies reach the upstream unchanged, and never grow ──────────

class BodyFidelityTests(ChaosBase):
    async def test_a_two_mib_ascii_body_is_forwarded_no_longer_than_it_came(
        self,
    ) -> None:
        sent = _payload(BIG)
        resp = await self.client.post(
            "/v1/messages", headers=self.headers(), data=sent,
        )
        self.assertEqual(resp.status, 200, await resp.text())
        got = self.vendor_up.message_requests[0]["body"]
        # The ONLY licensed difference is the namespace coming off the id.
        self.assertEqual(json.loads(got)["model"], "glm-5.3")
        self.assertLessEqual(
            len(got), len(sent),
            "a re-encode must never inflate a body the client sized correctly",
        )
        self.assertEqual(json.loads(got)["messages"], json.loads(sent)["messages"])

    async def test_a_non_ascii_body_is_not_escaped_into_three_times_its_size(
        self,
    ) -> None:
        """`ensure_ascii=True` turned every è into six bytes.

        A 12 MB Italian or Japanese transcript then crossed the upstream's
        32 MB limit that the client had sized correctly — a 413 that happens
        through the gateway only.
        """
        sent = _payload(256 * 1024, char="è")
        resp = await self.client.post(
            "/v1/messages", headers=self.headers(), data=sent,
        )
        self.assertEqual(resp.status, 200)
        got = self.vendor_up.message_requests[0]["body"]
        self.assertLessEqual(len(got), len(sent))
        self.assertEqual(
            json.loads(got)["messages"][0]["content"],
            json.loads(sent)["messages"][0]["content"],
        )

    async def test_a_forty_mib_body_reaches_the_upstream_byte_identical(self) -> None:
        """Past the rewrite buffer: streamed, and nothing may be lost."""
        sent = _payload(HUGE, model="claude-opus-5")
        resp = await self.client.post(
            "/v1/messages", headers=self.headers(), data=sent,
        )
        self.assertEqual(resp.status, 200, await resp.text())
        got = self.anthropic_up.message_requests[0]["body"]
        self.assertEqual(len(got), len(sent))
        self.assertEqual(got, sent)

    async def test_count_tokens_carries_the_same_body(self) -> None:
        sent = _payload(BIG)
        resp = await self.client.post(
            "/v1/messages/count_tokens", headers=self.headers(), data=sent,
        )
        self.assertEqual(resp.status, 200)
        self.assertLessEqual(
            len(self.vendor_up.message_requests[0]["body"]), len(sent),
        )


# ── 2 — a body the gateway cannot read is the upstream's to judge ────────

class UnreadableBodyTests(ChaosBase):
    async def test_a_broken_body_gets_the_upstreams_verdict_not_a_500(self) -> None:
        self.anthropic_up.messages_status = 400
        self.anthropic_up.messages_body = {"error": {"type": "invalid_request_error"}}
        resp = await self.client.post(
            "/v1/messages", headers=self.headers(), data=b'{"model": "claude',
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.content_type, "application/json")
        self.assertEqual(
            self.anthropic_up.message_requests[0]["body"], b'{"model": "claude',
        )

    async def test_a_two_thousand_deep_body_does_not_crash_the_handler(self) -> None:
        """`json.loads` raises RecursionError, not ValueError, past ~1000.

        Uncaught it is a 500 the SDK retries ten times — ten vendor-billed
        requests for a body that was never going to work. Forwarded, the
        upstream says so once.
        """
        deep = b"[" * 2000 + b"1" + b"]" * 2000
        self.anthropic_up.messages_status = 400
        resp = await self.client.post(
            "/v1/messages", headers=self.headers(), data=deep,
        )
        self.assertEqual(resp.status, 400)
        self.assertNotEqual(resp.status, 500)
        self.assertEqual(self.anthropic_up.message_requests[0]["body"], deep)

    async def test_count_tokens_treats_a_broken_body_the_same_way(self) -> None:
        self.anthropic_up.messages_status = 422
        resp = await self.client.post(
            "/v1/messages/count_tokens", headers=self.headers(), data=b"{nope",
        )
        self.assertEqual(resp.status, 422)


# ── 3, 4, 5 — a broken REPAIR may not break the request ──────────────────

class RewriteFailureTests(ChaosBase):
    async def test_a_request_rewrite_that_raises_forwards_the_raw_body(self) -> None:
        def explode(*_args: Any, **_kwargs: Any):
            raise RuntimeError("synthetic repair defect")

        with unittest.mock.patch.object(srv, "restore_vendor_ids", explode):
            sent = _payload(64)
            resp = await self.client.post(
                "/v1/messages", headers=self.headers(), data=sent,
            )
        self.assertEqual(resp.status, 200, await resp.text())
        got = self.vendor_up.message_requests[0]["body"]
        # The routed id still lands — that rewrite is not the broken one —
        # and the messages are the client's, untouched.
        self.assertEqual(json.loads(got)["model"], "glm-5.3")
        self.assertEqual(json.loads(got)["messages"], json.loads(sent)["messages"])

    async def test_a_first_party_repair_that_raises_still_serves(self) -> None:
        def explode(*_args: Any, **_kwargs: Any):
            raise RuntimeError("synthetic repair defect")

        with unittest.mock.patch.object(srv, "sanitise_for_anthropic", explode):
            resp = await self.client.post(
                "/v1/messages",
                headers=self.headers(),
                data=_payload(64, model="claude-opus-5"),
            )
        self.assertEqual(resp.status, 200, await resp.text())
        self.assertEqual(len(self.anthropic_up.message_requests), 1)

    async def test_a_stream_rewriter_that_raises_relays_every_byte(self) -> None:
        """Chunk 2 explodes: the stream continues, verbatim, to message_stop.

        Held-back bytes included — the rewriter buffers a partial event, and
        dropping that on the way out would truncate the turn to save an id.
        """
        chunks = [
            b"event: message_start\ndata: {\"type\":\"message_start\"}\n\n",
            b"event: content_block_delta\ndata: {\"type\":\"content_block_delta\"}\n\n",
            b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
        ]
        self.vendor_up.stream_chunks = chunks
        calls = {"n": 0}
        real_feed = srv.SseIdRewriter.feed

        def flaky(self_, chunk):  # noqa: ANN001 — a patched bound method
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("synthetic rewriter defect")
            return real_feed(self_, chunk)

        with unittest.mock.patch.object(srv.SseIdRewriter, "feed", flaky):
            resp = await self.client.post(
                "/v1/messages",
                headers=self.headers(),
                json={"model": "claude-gw/glm-5.3", "messages": [], "stream": True},
            )
            body = await resp.read()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b"".join(chunks))
        self.assertIn(b"message_stop", body)

    async def test_a_vendor_json_normaliser_that_raises_relays_the_body(self) -> None:
        self.vendor_up.messages_body = {"id": "msg_1", "content": [{"type": "text"}]}

        def explode(*_args: Any, **_kwargs: Any):
            raise RuntimeError("synthetic normaliser defect")

        with unittest.mock.patch.object(srv, "normalise_vendor_response", explode):
            resp = await self.client.post(
                "/v1/messages", headers=self.headers(), json={
                    "model": "claude-gw/glm-5.3", "messages": [],
                },
            )
            body = await resp.json()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, self.vendor_up.messages_body)


# ── 6, 7 — upstream failures arrive as the upstream wrote them ───────────

class UpstreamFailureTests(ChaosBase):
    #: Headers an SDK acts on. Dropping any of them turns a retryable, timed
    #: refusal into a bare failure the client cannot reason about.
    ACTIONABLE = {
        "request-id": "req_synthetic_1",
        "retry-after": "17",
        "x-should-retry": "true",
        "anthropic-ratelimit-requests-remaining": "0",
        "anthropic-ratelimit-requests-reset": "2026-09-09T16:00:00Z",
    }

    async def test_a_500_is_relayed_with_every_actionable_header(self) -> None:
        self.anthropic_up.messages_status = 500
        self.anthropic_up.messages_body = {
            "type": "error", "error": {"type": "api_error"},
        }
        self.anthropic_up.extra_headers = dict(self.ACTIONABLE)
        resp = await self.client.post(
            "/v1/messages", headers=self.headers(),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(resp.status, 500)
        for key, value in self.ACTIONABLE.items():
            self.assertEqual(resp.headers.get(key), value, f"{key} was dropped")

    async def test_count_tokens_relays_the_same_failure(self) -> None:
        self.anthropic_up.messages_status = 529
        self.anthropic_up.extra_headers = {"retry-after": "3"}
        resp = await self.client.post(
            "/v1/messages/count_tokens", headers=self.headers(),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(resp.status, 529)
        self.assertEqual(resp.headers.get("retry-after"), "3")

    async def test_an_unreachable_upstream_is_a_retryable_json_502(self) -> None:
        await self.vendor_up.stop()
        self.vendor_up.server = None
        resp = await self.client.post(
            "/v1/messages", headers=self.headers(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 502)
        self.assertEqual(resp.content_type, "application/json")
        self.assertEqual(resp.headers.get("x-should-retry"), "true")
        body = await resp.json()
        self.assertEqual(body["type"], "error")


# ── 8 — a truncated answer must LOOK truncated ───────────────────────────

class TruncationTests(ChaosBase):
    async def test_an_upstream_abort_reaches_the_client_as_a_broken_read(
        self,
    ) -> None:
        """Not a clean 200 with a missing ``message_stop``.

        A well-formed EOF is indistinguishable from a complete answer: the
        client shows a partial turn and never retries. Native sees a
        premature close and does retry, so the gateway must produce the same.
        """
        self.vendor_up.mode = "abort_mid_stream"
        resp = await self.client.post(
            "/v1/messages",
            headers=self.headers(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "stream": True},
        )
        self.assertEqual(resp.status, 200)
        with self.assertRaises(
            (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError),
        ):
            await resp.read()


# ── 9, 10 — long and slow are not failures ───────────────────────────────

class LongStreamTests(ChaosBase):
    async def test_a_large_sse_stream_is_relayed_complete(self) -> None:
        block = b"event: content_block_delta\ndata: " + b"y" * 65_000 + b"\n\n"
        count = 160  # ~10 MB, in event-shaped frames
        self.vendor_up.stream_chunks = [
            *([block] * count),
            b"event: message_stop\ndata: {}\n\n",
        ]
        resp = await self.client.post(
            "/v1/messages",
            headers=self.headers(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "stream": True},
        )
        body = await resp.read()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body.count(b"content_block_delta"), count)
        self.assertTrue(body.endswith(b"event: message_stop\ndata: {}\n\n"))


class IdleTimeoutTests(ChaosBase):
    """The bound is IDLENESS, never total elapsed time."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.config.upstream_idle_timeout_s = 0.5
        self.client = await self.make_client()

    async def test_a_stream_far_longer_than_the_bound_completes(self) -> None:
        # 15 events, 0.1 s apart: 1.5 s elapsed against a 0.5 s bound. Under
        # the old `total=` reading this is a cut stream; under an idle one it
        # is an ordinary slow answer.
        self.vendor_up.mode = "slow_stream"
        self.vendor_up.slow_events = 15
        self.vendor_up.slow_gap_s = 0.1
        resp = await self.client.post(
            "/v1/messages",
            headers=self.headers(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "stream": True},
        )
        body = await resp.read()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body.count(b"event: ping"), 15)
        self.assertIn(b"message_stop", body)

    async def test_a_gap_longer_than_the_bound_breaks_the_stream(self) -> None:
        """Going quiet still ends it — an idle bound that never fires is none."""
        # The first event goes out immediately (so the response IS started
        # and the client is reading), then the upstream goes quiet for longer
        # than the idle bound.
        self.vendor_up.mode = "slow_stream"
        self.vendor_up.slow_events = 1
        self.vendor_up.slow_gap_s = 1.2
        resp = await self.client.post(
            "/v1/messages",
            headers=self.headers(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "stream": True},
        )
        with self.assertRaises(
            (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError),
        ):
            await resp.read()


# ── the client that leaves, and the login that rotates ───────────────────

class ClientCancellationTests(ChaosBase):
    """`handler_cancellation=True` cancels the handler; the log must survive it.

    `CancelledError` is a BaseException, so it passes straight through every
    `except (ClientError, TimeoutError)` in the relay. Without a guard, the
    single most common real ending — the user hit Esc — was the one outcome
    that wrote NO access line, and the `client_disconnected` branches only
    ever fired in tests that closed a socket without cancelling.
    """

    async def test_a_client_that_leaves_mid_stream_is_still_logged(self) -> None:
        self.vendor_up.mode = "slow_stream"
        self.vendor_up.slow_events = 40
        self.vendor_up.slow_gap_s = 0.05
        with self.assertLogs(LOGGER, level="INFO") as captured:
            resp = await self.client.post(
                "/v1/messages",
                headers=self.headers(),
                json={"model": "claude-gw/glm-5.3", "messages": [], "stream": True},
            )
            self.assertEqual(resp.status, 200)
            await resp.content.read(10)
            resp.close()  # the client goes away mid-answer
            # Let the server observe the cancellation and write its line.
            for _ in range(40):
                await asyncio.sleep(0.05)
                if any("client_disconnected" in m for m in captured.output):
                    break
        line = next(
            (m for m in captured.output if "client_disconnected" in m), None,
        )
        self.assertIsNotNone(line, captured.output)
        fields = _fields(line)
        self.assertEqual(fields["route"], "vendor:zai")
        self.assertTrue(fields["bytes"].isdigit())


class OAuthRereadTests(ChaosBase):
    """A login rotated by a native client mid-request must not cost the turn.

    The gateway never refreshes the Claude login — it reads the file the CLI
    writes — so its only honest move after a 401 is native's own: read the
    file again and adopt whatever is there now.
    """

    async def test_a_rotated_credential_is_picked_up_and_retried(self) -> None:
        rotated = f"{FAKE_OAUTH}-rotated-and-longer"
        self.anthropic_up.accept_bearer = rotated
        # The stub rewrites the credentials file as it answers the first 401 —
        # the mid-flight rotation this exists for.
        self.anthropic_up.on_request = lambda: self.write_credentials(
            rotated, expires_in_ms=3_600_000,
        )
        with self.assertLogs(LOGGER, level="INFO") as captured:
            resp = await self.client.post(
                "/v1/messages", headers=self.headers(),
                json={"model": "claude-opus-5", "messages": []},
            )
        self.assertEqual(resp.status, 200, await resp.text())
        calls = self.anthropic_up.message_requests
        self.assertEqual(len(calls), 2, "one 401, then the retry")
        self.assertEqual(calls[0]["headers"]["Authorization"], f"Bearer {FAKE_OAUTH}")
        self.assertEqual(calls[1]["headers"]["Authorization"], f"Bearer {rotated}")
        self.assertTrue(
            any("oauth_reread_retry" in m for m in captured.output), captured.output,
        )

    async def test_an_unchanged_credential_is_not_retried(self) -> None:
        """A genuinely dead login answers 401 once — never in a loop."""
        self.anthropic_up.accept_bearer = "some-other-token"
        resp = await self.client.post(
            "/v1/messages", headers=self.headers(),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(resp.status, 401)
        self.assertEqual(len(self.anthropic_up.message_requests), 1)

    async def test_a_streamed_body_is_never_retried(self) -> None:
        """An over-buffer body is already consumed; it cannot be sent twice."""
        self.config.rewrite_buffer_bytes = 64 * 1024
        client = await self.make_client()
        self.anthropic_up.accept_bearer = "some-other-token"
        self.anthropic_up.on_request = lambda: self.write_credentials(
            f"{FAKE_OAUTH}-rotated-and-longer", expires_in_ms=3_600_000,
        )
        resp = await client.post(
            "/v1/messages",
            headers=self.headers(),
            data=_payload(256 * 1024, model="claude-opus-5"),
        )
        self.assertEqual(resp.status, 401)
        self.assertEqual(
            len(self.anthropic_up.message_requests), 1,
            "a consumed stream must not be replayed",
        )


class UnroutableBodyTests(ChaosBase):
    """The one refusal left, and it must actually REACH the client."""

    async def test_the_message_reaches_a_client_still_uploading(self) -> None:
        """The refusal is only useful if the SENTENCE arrives.

        The gateway decides while the client is mid-upload, so what the user
        gets is a race between the answer and the connection. This pins the
        contract — status, envelope and the sentence naming the fix — rather
        than the drain that precedes it: with aiohttp's client the message
        arrives either way (measured, see `_drain`), and a test that claimed
        to prove the drain would be crediting a mechanism it cannot see.
        """
        self.config.rewrite_buffer_bytes = 64 * 1024
        client = await self.make_client()
        body = json.dumps(
            {
                "messages": [{"role": "user", "content": "y" * (1024 * 1024)}],
                "model": "claude-opus-5",
            }
        ).encode("utf-8")
        resp = await client.post(
            "/v1/messages", headers=self.headers(), data=body,
        )
        self.assertEqual(resp.status, 400)
        payload = await resp.json()
        self.assertIn("model", payload["error"]["message"])
        self.assertEqual(self.anthropic_up.message_requests, [])


# ── 11 — concurrent streams do not mix ───────────────────────────────────

class ConcurrencyTests(ChaosBase):
    async def test_eight_parallel_streams_keep_their_own_ids_and_indexes(
        self,
    ) -> None:
        async def one(i: int) -> bytes:
            resp = await self.client.post(
                "/v1/messages",
                headers=self.headers(),
                json={
                    "model": "claude-gw/glm-5.3",
                    "messages": [{"role": "user", "content": f"q{i}"}],
                    "stream": True,
                },
            )
            return await resp.read()

        self.vendor_up.stream_chunks = [
            b"event: message_start\ndata: {\"type\":\"message_start\"}\n\n",
            b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
        ]
        bodies = await asyncio.gather(*(one(i) for i in range(8)))
        self.assertEqual(len(bodies), 8)
        for body in bodies:
            self.assertIn(b"message_start", body)
            self.assertIn(b"message_stop", body)
        # Every request reached the upstream, and each carries its own text —
        # a shared buffer would have crossed them.
        sent = [r["body"] for r in self.vendor_up.message_requests]
        self.assertEqual(len(sent), 8)
        self.assertEqual(
            sorted(json.loads(b)["messages"][0]["content"] for b in sent),
            sorted(f"q{i}" for i in range(8)),
        )


# ── 12 — a real client's headers fit ─────────────────────────────────────

class HeaderLimitTests(ChaosBase):
    """A session with many betas sends ~16 KiB of ``anthropic-beta``."""

    async def make_client_with_limits(self, **limits):
        from aiohttp.test_utils import TestClient, TestServer

        app = srv.create_app(
            self.config,
            vendors={self.vendor.vendor_id: self.vendor},
            anthropic=self.anthropic,
            oauth_reader=self.client.app[srv.APP_KEY].oauth,
            key_resolver=self.client.app[srv.APP_KEY].keys,
        )
        # The parser limits are the SERVER's; `TestServer.start_server`
        # forwards them to the same `AppRunner` kwargs `web.run_app` uses.
        server = TestServer(app)
        await server.start_server(**limits)
        client = TestClient(server)
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def test_a_sixteen_kib_header_reaches_the_upstream(self) -> None:
        # Served with the daemon's OWN limits, imported rather than retyped:
        # a copy here would pin the copy.
        client = await self.make_client_with_limits(**SERVER_LIMITS)
        beta = ",".join(f"beta-feature-{i}-2026-01-01" for i in range(700))
        self.assertGreater(len(beta), 16 * 1024)
        resp = await client.post(
            "/v1/messages",
            headers={**self.headers(), "anthropic-beta": beta},
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 200, await resp.text())
        self.assertIn(
            "beta-feature-559-2026-01-01",
            self.vendor_up.message_requests[0]["headers"]["anthropic-beta"],
        )

    async def test_the_defaults_would_have_refused_it(self) -> None:
        """The red half: aiohttp's 8190-byte default answers a bare 400."""
        client = await self.make_client_with_limits()
        beta = ",".join(f"beta-feature-{i}-2026-01-01" for i in range(700))
        resp = await client.post(
            "/v1/messages",
            headers={**self.headers(), "anthropic-beta": beta},
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 400)
        self.assertNotEqual(resp.content_type, "application/json")


# ── 14 — a stalling vendor may not hold the picker ───────────────────────

class CatalogTests(ChaosBase):
    async def test_a_hanging_vendor_does_not_hold_the_model_list(self) -> None:
        self.anthropic_up.models_payload = {"data": [{"id": "claude-opus-5"}]}
        self.vendor_up.catalog_delay_s = 30
        loop = asyncio.get_running_loop()
        started = loop.time()
        resp = await self.client.get("/v1/models", headers=self.auth())
        elapsed = loop.time() - started
        self.assertEqual(resp.status, 200)
        self.assertLess(elapsed, 12, "the picker waited on a dead vendor")
        body = await resp.json()
        ids = [entry["id"] for entry in body["data"]]
        self.assertIn("claude-opus-5", ids, "the healthy family must still list")


# ── 15 — the gateway never answers in a shape the SDK cannot parse ───────

class ContentTypeTests(ChaosBase):
    async def test_every_gateway_authored_answer_is_json(self) -> None:
        """`text/plain` is what an unhandled exception looks like.

        The SDK parses any non-2xx as JSON, so a plain-text body surfaces as
        a parser error with the real status lost — which is how "the gateway
        is broken" gets reported for what was a 401.
        """
        cases = [
            ("no token", {}, {"model": "claude-opus-5", "messages": []}, 401),
            ("bad model", self.headers(), {"model": "gpt-9", "messages": []}, 400),
            ("no route", self.headers(), None, 404),
        ]
        for label, headers, payload, expected in cases:
            with self.subTest(case=label):
                if payload is None:
                    resp = await self.client.get("/v1/nonsense", headers=headers)
                else:
                    resp = await self.client.post(
                        "/v1/messages", headers=headers, json=payload,
                    )
                self.assertEqual(resp.status, expected)
                self.assertEqual(resp.content_type, "application/json")
                self.assertEqual((await resp.json())["type"], "error")

    async def test_the_preconnect_probe_is_answered_like_the_real_api(self) -> None:
        """`HEAD /api/hello` is the client's warm-up; native answers 200."""
        resp = await self.client.head("/api/hello")
        self.assertEqual(resp.status, 200)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
