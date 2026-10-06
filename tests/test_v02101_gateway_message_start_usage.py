# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ``message_start`` usage floor for vendors that under-report it.

Claude Code stamps each content block's transcript entry with the usage the
stream carried at ``message_start``. Measured 2026-10-03 (parity audit gap 3,
live captures copied verbatim into ``tests/fixtures/model_router/``):

* z.ai streams open with ``{"input_tokens": 0, "output_tokens": 0}`` and no
  cache fields — 110 of 598 GLM assistant transcript entries on this machine
  carried ALL-ZERO usage, and the VS Code agent row keeps "resetting";
* the qwen endpoint opens with a small non-zero ``input_tokens`` that
  under-reads the final ``message_delta`` figure (4 at the start, 36 in the
  delta on the captured turn) and no cache fields;
* Anthropic's own stream opens with every field, truthfully.

The fix is a data flag per vendor (``vendors.py``'s
``partial_message_start_usage``) driving one mechanism: the SSE rewriter lifts
``message_start``'s ``input_tokens`` to a FLOOR estimate — the larger of the
ledger's last real input+cache total for the same agent/session identity and
the request's own bytes/4 floor. It never lowers a figure, never touches the
cache fields, and only runs on flagged vendor routes, so:

1. a first-party (Anthropic) stream is byte-identical to the upstream's;
2. a flagged vendor event that already reports at least the floor is too;
3. the ledger still records the VENDOR's real final figures — the splice is
   a positive that ``message_delta``'s real positive replaces
   (last-positive-wins), and no estimate is ever planted in a cache field a
   later real zero could not overwrite;
4. any exception in the rewrite abandons it (``_guarded``) and relays the
   original bytes — a chat can lose a cosmetic number, never its answer;
5. non-streamed responses are untouched (they carry real usage natively —
   the captured ``*-nonstream`` and ``*-tool`` bodies prove it).

The fixture-driven classes replay the REAL captured vendor bytes through the
full HTTP relay; the synthetic classes pin the estimate ladder and the
failure modes the captures cannot provoke. No network: the upstreams are the
loopback stubs from :mod:`tests.test_model_router_server`.
"""
from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, cast
from unittest import mock

from model_router import tool_ids as ti
from model_router import usage as U
from model_router.server import (
    APP_KEY,
    COUNT_TOKENS_PATH,
    Gateway,
    RequestFacts,
    _message_start_usage_estimate,
)
from model_router.vendors import VENDORS

from tests.test_model_router_server import GatewayTestBase

FIXTURES = Path(__file__).parent / "fixtures" / "model_router"

#: The fixture streams per flagged vendor route. ``qwen-cache-control`` is a
#: deepseek stream whose request carried cache_control markers; its response
#: shape is the one under test here, so it rides the same assertions.
ZAI_STREAM_FIXTURES = (
    "zai-stream", "zai-thinking", "zai-flash-thinking", "zai-1m-stream",
)
QWEN_STREAM_FIXTURES = (
    "qwen-deepseek-stream", "qwen-deepseek-thinking", "qwen-max-stream",
    "qwen-cache-control",
)

#: A system prompt big enough that the bytes/4 floor dwarfs every captured
#: message_start figure (z.ai's 0, qwen's 4), so the expected splice value is
#: the floor and nothing else.
BIG_SYSTEM = "S" * 8_000


def _fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.body").read_bytes()


def _unmark_thinking(body: bytes) -> bytes:
    """Strip the gateway thinking-signature marker item (c) adds on the way out.

    v0.2.101 item (c) marks a vendor ``thinking`` signature with a ``vct_``
    prefix in the relayed response, so a captured vendor body that carries a
    thinking block is no longer byte-identical to what the gateway now relays —
    it differs by exactly that marker. This test is about the ``message_start``
    usage splice, so the orthogonal mark is removed before the verbatim
    comparison (the marking itself is asserted in
    ``tests/test_v02101_gateway_parity_request.py``). The captures are compact
    JSON, which is also how the gateway re-serialises a body it edited, so the
    marker is the only difference.
    """
    return body.replace(b'"signature":"vct_', b'"signature":"')


def _request_body_bytes(payload: dict) -> bytes:
    """Exactly the bytes aiohttp's ``json=`` sends for ``payload``.

    ``JsonPayload`` serialises with ``json.dumps(value).encode("utf-8")``,
    so the server's ``raw`` — and therefore the bytes/4 floor it reads off
    those bytes — is reproducible here without guessing.
    """
    return json.dumps(payload).encode("utf-8")


def _stub_gateway(ledger: U.UsageLedger) -> Gateway:
    """``_message_start_usage_estimate`` reads ``gateway.usage`` and nothing
    else, so a stub standing in for the full daemon is honest here."""
    return cast(Gateway, SimpleNamespace(usage=ledger))


def _events(body: bytes) -> list[bytes]:
    """The raw SSE events of a body, separators projected away."""
    events, _rest = U.split_sse_events(body, final=True)
    return events


def _message_start_usage(body: bytes) -> Optional[dict]:
    """The ``message_start`` event's usage block, or ``None``."""
    for raw in _events(body):
        payload = U.event_payload(raw)
        if payload is not None and payload.get("type") == "message_start":
            message = payload.get("message")
            if isinstance(message, dict):
                usage = message.get("usage")
                return usage if isinstance(usage, dict) else None
    return None


def _message_delta_usage(body: bytes) -> Optional[dict]:
    for raw in _events(body):
        payload = U.event_payload(raw)
        if payload is not None and payload.get("type") == "message_delta":
            usage = payload.get("usage")
            return usage if isinstance(usage, dict) else None
    return None


def _sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")


#: The measured z.ai shape: zeros up front, the real figures in the delta.
ZAI_ZERO_STREAM = (
    _sse("message_start", {
        "type": "message_start",
        "message": {"id": "msg_1", "model": "glm-5.3", "content": [],
                    "usage": {"input_tokens": 0, "output_tokens": 0}},
    })
    + _sse("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""}})
    + _sse("content_block_delta", {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "ok"}})
    + _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    + _sse("message_delta", {
        "type": "message_delta", "delta": {"stop_reason": "end_turn"},
        "usage": {"input_tokens": 2048, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 64_000, "output_tokens": 99},
    })
    + _sse("message_stop", {"type": "message_stop"})
)

ANTHROPIC_STREAM = (
    _sse("message_start", {
        "type": "message_start",
        "message": {"id": "msg_2", "model": "claude-opus-5", "content": [],
                    "usage": {"input_tokens": 1200,
                              "cache_creation_input_tokens": 300,
                              "cache_read_input_tokens": 90_000,
                              "output_tokens": 1}},
    })
    + _sse("content_block_delta", {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "hi"}})
    + _sse("message_delta", {
        "type": "message_delta", "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": 450}})
    + _sse("message_stop", {"type": "message_stop"})
)


class FlaggedVendorTestBase(GatewayTestBase):
    """The harness vendor (zai-shaped) WITH the flag the registry row carries.

    ``GatewayTestBase`` rebuilds the zai row field by field and predates this
    flag, so the flagged shape is restored here rather than in the shared
    base — every other suite keeps testing the unflagged rewriter.
    """

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.vendor = replace(self.vendor, partial_message_start_usage=True)
        self.client = await self.make_client()

    def post_body(self, *, model: str = "claude-gw/glm-5.3") -> dict:
        return {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "system": BIG_SYSTEM,
            "stream": True,
        }


class ZaiZeroMessageStartTests(FlaggedVendorTestBase):
    """The measured defect: zeros in, a positive floor out."""

    async def test_zero_message_start_is_lifted_to_the_floor(self) -> None:
        self.vendor_up.stream_chunks = [ZAI_ZERO_STREAM]
        payload = self.post_body()
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(), json=payload,
        )
        self.assertEqual(resp.status, 200)
        body = await resp.read()
        floor = U.count_tokens_estimate(
            payload, raw=_request_body_bytes(payload),
        )
        self.assertIsNotNone(floor)
        usage = _message_start_usage(body)
        self.assertIsNotNone(usage)
        assert usage is not None and floor is not None
        self.assertEqual(usage["input_tokens"], floor)
        self.assertGreater(usage["input_tokens"], 0)
        # The delta is the vendor's and stays the vendor's.
        self.assertEqual(
            _message_delta_usage(body),
            _message_delta_usage(ZAI_ZERO_STREAM),
        )

    async def test_ledger_records_the_vendors_real_final_figures(self) -> None:
        self.vendor_up.stream_chunks = [ZAI_ZERO_STREAM]
        resp = await self.client.post(
            "/v1/messages",
            headers={
                **self.auth(),
                "x-claude-code-session-id": "sess-real",
                "x-claude-code-agent-id": "agent-real",
            },
            json=self.post_body(),
        )
        self.assertEqual(resp.status, 200)
        await resp.read()
        gateway = self.client.app[APP_KEY]
        await gateway.usage.drain()
        rows = [
            json.loads(line)
            for line in self.usage_ledger_path.read_text(
                encoding="utf-8",
            ).splitlines()
            if line.strip()
        ]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # The delta's REAL figures, not the spliced floor: the estimate rode
        # message_start, and last-positive-wins replaced it.
        self.assertEqual(row["input_tokens"], 2048)
        self.assertEqual(row["cache_creation_input_tokens"], 0)
        self.assertEqual(row["cache_read_input_tokens"], 64_000)
        self.assertEqual(row["output_tokens"], 99)
        self.assertTrue(row["usage_complete"])

    async def test_ledger_floor_wins_over_the_bytes_estimate(self) -> None:
        gateway = self.client.app[APP_KEY]
        gateway.usage.submit(
            U.build_record(
                session="sess-floor", agent="agent-floor", parent_agent=None,
                requested="claude-gw/glm-5.3", route="vendor:zai",
                forward="glm-5.3", stream=True, status=200,
                totals={
                    "input_tokens": 100, "cache_creation_input_tokens": 50,
                    "cache_read_input_tokens": 100_000, "output_tokens": 200,
                },
                usage_complete=True,
                window_actual=200_000, window_source="table",
                now=datetime(2026, 10, 3, tzinfo=timezone.utc),
            ),
        )
        self.vendor_up.stream_chunks = [ZAI_ZERO_STREAM]
        resp = await self.client.post(
            "/v1/messages",
            headers={
                **self.auth(),
                "x-claude-code-session-id": "sess-floor",
                "x-claude-code-agent-id": "agent-floor",
            },
            json=self.post_body(),
        )
        body = await resp.read()
        usage = _message_start_usage(body)
        assert usage is not None
        # context_after = 100 + 50 + 100_000 + 200 — far above the bytes/4
        # floor of this small request, so the ledger's real figure wins.
        self.assertEqual(usage["input_tokens"], 100_350)

    async def test_non_streamed_vendor_response_is_untouched(self) -> None:
        raw = json.dumps({
            "id": "msg_3", "type": "message", "role": "assistant",
            "model": "glm-5.3", "content": [{"type": "text", "text": "Hi"}],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }).encode("utf-8")
        self.vendor_up.messages_raw = raw
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.read(), raw)

    async def test_a_rewrite_exception_relays_the_original_bytes(self) -> None:
        self.vendor_up.stream_chunks = [ZAI_ZERO_STREAM]
        with mock.patch.object(
            ti.SseIdRewriter, "_on_message_start",
            side_effect=RuntimeError("synthetic splice failure"),
        ):
            resp = await self.client.post(
                "/v1/messages", headers=self.auth(), json=self.post_body(),
            )
            self.assertEqual(resp.status, 200)
            body = await resp.read()
        # The chat keeps its answer: the vendor's own bytes, zeros and all,
        # which is exactly today's behaviour.
        self.assertEqual(body, ZAI_ZERO_STREAM)


class TruthfulRouteByteIdentityTests(FlaggedVendorTestBase):
    """Routes the splice must NOT touch — byte-identical relay."""

    async def test_anthropic_stream_is_byte_identical(self) -> None:
        self.anthropic_up.stream_chunks = [ANTHROPIC_STREAM]
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-opus-5", "messages": [], "stream": True},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.read(), ANTHROPIC_STREAM)

    async def test_flagged_vendor_at_or_above_the_floor_is_byte_identical(
        self,
    ) -> None:
        # The stale-flag guard: a vendor that starts reporting truthfully
        # exceeds the floor and is relayed verbatim despite the flag.
        honest = ZAI_ZERO_STREAM.replace(
            b'"input_tokens": 0, "output_tokens": 0',
            b'"input_tokens": 999999, "output_tokens": 0',
        )
        self.assertNotEqual(honest, ZAI_ZERO_STREAM)
        self.vendor_up.stream_chunks = [honest]
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(), json=self.post_body(),
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.read(), honest)


class QwenRouteTests(FlaggedVendorTestBase):
    """The qwen row carries the same flag: PARTIAL, not zero (gap 3)."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.vendor = replace(VENDORS["qwen"], upstream=self.vendor.upstream)
        self.client = await self.make_client()

    async def test_partial_message_start_is_lifted_to_the_floor(self) -> None:
        partial = (
            _sse("message_start", {
                "type": "message_start",
                "message": {"id": "msg_q", "model": "deepseek-v4.1-flash",
                            "content": [],
                            "usage": {"input_tokens": 4,
                                      "output_tokens": 0}},
            })
            + _sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"input_tokens": 36, "output_tokens": 17,
                          "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0}})
            + _sse("message_stop", {"type": "message_stop"})
        )
        self.vendor_up.stream_chunks = [partial]
        payload = {
            "model": "claude-gw/qwen/deepseek-v4.1-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "system": BIG_SYSTEM,
            "stream": True,
        }
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(), json=payload,
        )
        self.assertEqual(resp.status, 200)
        body = await resp.read()
        floor = U.count_tokens_estimate(
            payload, raw=_request_body_bytes(payload),
        )
        usage = _message_start_usage(body)
        assert usage is not None and floor is not None
        self.assertEqual(usage["input_tokens"], floor)
        self.assertEqual(
            _message_delta_usage(body), _message_delta_usage(partial),
        )

    async def test_truthful_qwen_stream_is_byte_identical(self) -> None:
        honest = (
            _sse("message_start", {
                "type": "message_start",
                "message": {"id": "msg_q2", "model": "qwen3.8-max",
                            "content": [],
                            "usage": {"input_tokens": 999_999,
                                      "output_tokens": 0}},
            })
            + _sse("message_stop", {"type": "message_stop"})
        )
        self.vendor_up.stream_chunks = [honest]
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-gw/qwen/qwen3.8-max",
                "messages": [{"role": "user", "content": "hi"}],
                "system": BIG_SYSTEM,
                "stream": True,
            },
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.read(), honest)


class FixtureStreamTests(FlaggedVendorTestBase):
    """The REAL captured vendor streams, replayed through the full relay.

    Captured live 2026-10-03 by the parity-audit lane (auth-stripped at
    capture; verified free of credential material before being copied into
    the repo). For every flagged-vendor capture: the relayed
    ``message_start`` carries the floor, EVERY other event is byte-identical
    to the capture, and the final ``message_delta`` still carries the
    vendor's own real figures.
    """

    async def _assert_spliced(self, fixture: str, model: str) -> None:
        original = _fixture(fixture)
        self.vendor_up.stream_chunks = [original]
        payload = self.post_body(model=model)
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(), json=payload,
        )
        self.assertEqual(resp.status, 200, fixture)
        relayed = await resp.read()
        floor = U.count_tokens_estimate(
            payload, raw=_request_body_bytes(payload),
        )
        assert floor is not None

        start_usage = _message_start_usage(relayed)
        self.assertIsNotNone(start_usage, fixture)
        assert start_usage is not None
        self.assertEqual(start_usage["input_tokens"], floor, fixture)
        self.assertGreater(start_usage["input_tokens"], 0, fixture)

        # The delta — the vendor's real final figures — survives untouched.
        self.assertEqual(
            _message_delta_usage(relayed),
            _message_delta_usage(original),
            fixture,
        )

        # Every event that is not the spliced message_start is byte-identical,
        # EXCEPT a signature_delta: v0.2.101 item (c) marks a vendor thinking
        # signature on the way out (a "vct_" prefix), which re-serialises that
        # one event. The mark is orthogonal to the message_start splice under
        # test here and is asserted in
        # tests/test_v02101_gateway_parity_request.py.
        original_events = _events(original)
        relayed_events = _events(relayed)
        self.assertEqual(len(original_events), len(relayed_events), fixture)
        touched = 0
        for orig, got in zip(original_events, relayed_events):
            payload_o = U.event_payload(orig)
            if (
                payload_o is not None
                and payload_o.get("type") == "message_start"
            ):
                touched += 1
                continue
            delta = payload_o.get("delta") if payload_o is not None else None
            if isinstance(delta, dict) and delta.get("type") == "signature_delta":
                continue
            self.assertEqual(orig, got, f"{fixture}: non-message_start event")
        self.assertEqual(touched, 1, fixture)

    async def test_zai_captured_streams(self) -> None:
        for fixture in ZAI_STREAM_FIXTURES:
            with self.subTest(fixture=fixture):
                await self._assert_spliced(fixture, "claude-gw/glm-5.3")

    async def test_qwen_captured_streams(self) -> None:
        qwen = replace(VENDORS["qwen"], upstream=self.vendor.upstream)
        client = await self.make_client(vendors={"qwen": qwen})
        self.client = client
        for fixture in QWEN_STREAM_FIXTURES:
            with self.subTest(fixture=fixture):
                await self._assert_spliced(
                    fixture, "claude-gw/qwen/deepseek-v4.1-flash",
                )

    async def test_anthropic_captured_stream_is_byte_identical(self) -> None:
        original = _fixture("anthropic-stream")
        self.anthropic_up.stream_chunks = [original]
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-opus-5", "messages": [], "stream": True},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.read(), original)


class FixtureNonStreamTests(FlaggedVendorTestBase):
    """Captured NON-streamed bodies pass through the flagged route untouched.

    The splice is a STREAM repair: a buffered vendor body carries its real
    usage natively (the captures show it — ``zai-tool`` reports
    ``input_tokens: 145``), and the relay policy for JSON is tool-id
    normalisation only.
    """

    async def _assert_verbatim(self, fixture: str, model: str) -> None:
        original = _fixture(fixture)
        self.vendor_up.messages_raw = original
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": model,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(resp.status, 200, fixture)
        # A buffered thinking block's signature carries the item-(c) marker on
        # the way out; un-mark it so this compares the splice-free relay.
        self.assertEqual(_unmark_thinking(await resp.read()), original, fixture)

    async def test_zai_captured_non_streamed_bodies(self) -> None:
        for fixture in ("zai-nonstream", "zai-tool"):
            with self.subTest(fixture=fixture):
                await self._assert_verbatim(fixture, "claude-gw/glm-5.3")

    async def test_qwen_captured_non_streamed_bodies(self) -> None:
        self.vendor = replace(VENDORS["qwen"], upstream=self.vendor.upstream)
        self.client = await self.make_client()
        for fixture in ("qwen-deepseek-nonstream", "qwen-deepseek-tool"):
            with self.subTest(fixture=fixture):
                await self._assert_verbatim(
                    fixture, "claude-gw/qwen/deepseek-v4.1-flash",
                )

    async def test_anthropic_captured_non_streamed_bodies(self) -> None:
        for fixture in ("anthropic-nonstream", "anthropic-tool"):
            with self.subTest(fixture=fixture):
                original = _fixture(fixture)
                self.anthropic_up.messages_raw = original
                resp = await self.client.post(
                    "/v1/messages",
                    headers=self.auth(),
                    json={"model": "claude-opus-5", "messages": []},
                )
                self.assertEqual(resp.status, 200, fixture)
                self.assertEqual(await resp.read(), original, fixture)

    async def test_captured_count_bodies_still_parse(self) -> None:
        # The captures went THROUGH the gateway, so the vendor count bodies
        # already carry the zero-guard's label; replaying them re-labels to
        # the same value. What is pinned here is that the real shapes parse
        # and answer 200 — the guard itself is tested in
        # tests/test_v0295_gateway_usage_ledger.py.
        for fixture in ("zai-count", "qwen-deepseek-count", "qwen-max-count"):
            with self.subTest(fixture=fixture):
                self.vendor_up.messages_raw = _fixture(fixture)
                resp = await self.client.post(
                    COUNT_TOKENS_PATH,
                    headers=self.auth(),
                    json={"model": "claude-gw/glm-5.3", "messages": []},
                )
                self.assertEqual(resp.status, 200, fixture)
                body = await resp.json()
                self.assertGreater(body["input_tokens"], 0, fixture)


class SseRewriterMessageStartUnitTests(unittest.TestCase):
    """The splice itself, event by event."""

    def _splice(self, usage: Any, estimate: Optional[int]) -> bytes:
        rewriter = ti.SseIdRewriter(message_start_usage=estimate)
        event = _sse("message_start", {
            "type": "message_start",
            "message": {"id": "m", "usage": usage},
        })
        return rewriter.feed(event) + rewriter.flush()

    def _usage_of(self, out: bytes) -> Any:
        payload = U.event_payload(_events(out)[0])
        assert payload is not None
        return payload["message"]["usage"]

    def test_zero_is_lifted_to_the_estimate(self) -> None:
        out = self._splice({"input_tokens": 0, "output_tokens": 0}, 1_500)
        usage = self._usage_of(out)
        self.assertEqual(usage["input_tokens"], 1_500)
        # output_tokens is the vendor's and stays the vendor's.
        self.assertEqual(usage["output_tokens"], 0)

    def test_partial_below_the_floor_is_lifted(self) -> None:
        out = self._splice({"input_tokens": 4, "output_tokens": 0}, 36)
        self.assertEqual(self._usage_of(out)["input_tokens"], 36)

    def test_at_or_above_the_floor_is_verbatim(self) -> None:
        event = _sse("message_start", {
            "type": "message_start",
            "message": {"id": "m",
                        "usage": {"input_tokens": 2_000, "output_tokens": 0}},
        })
        rewriter = ti.SseIdRewriter(message_start_usage=1_500)
        self.assertEqual(rewriter.feed(event) + rewriter.flush(), event)

    def test_no_estimate_is_verbatim(self) -> None:
        event = _sse("message_start", {
            "type": "message_start",
            "message": {"id": "m",
                        "usage": {"input_tokens": 0, "output_tokens": 0}},
        })
        rewriter = ti.SseIdRewriter()
        self.assertEqual(rewriter.feed(event) + rewriter.flush(), event)

    def test_cache_fields_are_never_touched(self) -> None:
        # A planted cache estimate could never be corrected by the vendor's
        # real zero in message_delta ("a later zero never overwrites an
        # earlier positive"), so the splice must not write one. EVER.
        out = self._splice(
            {"input_tokens": 0, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0, "output_tokens": 0},
            1_500,
        )
        usage = self._usage_of(out)
        self.assertEqual(usage["cache_creation_input_tokens"], 0)
        self.assertEqual(usage["cache_read_input_tokens"], 0)

    def test_a_non_integer_input_is_left_alone(self) -> None:
        event = _sse("message_start", {
            "type": "message_start",
            "message": {"id": "m", "usage": {"input_tokens": True}},
        })
        rewriter = ti.SseIdRewriter(message_start_usage=1_500)
        self.assertEqual(rewriter.feed(event) + rewriter.flush(), event)

    def test_other_event_types_are_untouched(self) -> None:
        event = _sse("message_delta", {
            "type": "message_delta",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })
        rewriter = ti.SseIdRewriter(message_start_usage=1_500)
        self.assertEqual(rewriter.feed(event) + rewriter.flush(), event)


class EstimateLadderUnitTests(unittest.TestCase):
    """``context_floor`` and ``_message_start_usage_estimate`` — the ladder."""

    def _ledger(self) -> U.UsageLedger:
        tmp = tempfile.TemporaryDirectory(prefix="ctx-floor-")
        self.addCleanup(tmp.cleanup)
        return U.UsageLedger(path=Path(tmp.name) / "ledger.jsonl")

    def _record(
        self, *, session: str, agent: Optional[str], context: dict[str, int],
        ts: datetime,
    ) -> U.UsageRecord:
        return U.build_record(
            session=session, agent=agent, parent_agent=None,
            requested="claude-gw/glm-5.3", route="vendor:zai",
            forward="glm-5.3", stream=True, status=200, totals=context,
            usage_complete=True, window_actual=None, window_source="unknown",
            now=ts,
        )

    def test_agent_match_is_exact_and_the_sibling_agent_is_not_used(self) -> None:
        ledger = self._ledger()
        t0 = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
        t1 = datetime(2026, 10, 3, 13, tzinfo=timezone.utc)
        ledger.submit(self._record(
            session="s1", agent="a1",
            context={"input_tokens": 1_000, "output_tokens": 350}, ts=t0,
        ))
        self.assertEqual(
            ledger.context_floor(session="s1", agent="a1"), 1_350,
        )
        # A LARGER sibling turn in the same session. The per-chat map holds
        # only the newest row, so a session-only (or agent-preferring-with-
        # fallback) lookup answers with THIS subagent's forked size — the
        # cross-agent leak this test forbids. a1's own row must still win.
        ledger.submit(self._record(
            session="s1", agent="a2",
            context={"input_tokens": 150_000, "output_tokens": 1_000}, ts=t1,
        ))
        self.assertEqual(ledger.context_floor(session="s1", agent="a2"), 151_000)
        self.assertEqual(
            ledger.context_floor(session="s1", agent="a1"), 1_350,
            "a sibling agent's larger context must not be borrowed",
        )
        # Another chat never leaks into this one's floor.
        self.assertIsNone(ledger.context_floor(session="other", agent="a9"))

    def test_session_read_when_no_agent(self) -> None:
        ledger = self._ledger()
        ledger.submit(self._record(
            session="s2", agent=None,
            context={"input_tokens": 500, "output_tokens": 0},
            ts=datetime(2026, 10, 3, tzinfo=timezone.utc),
        ))
        self.assertEqual(ledger.context_floor(session="s2", agent=None), 500)

    def test_missing_session_has_no_ledger_floor(self) -> None:
        """No session → no conversation to key on → no floor, ever.

        The agent id alone must NOT be matched across chats: a client that
        reuses agent labels would otherwise borrow another session's size.
        The bytes estimate still applies, because the caller supplies it.
        """
        ledger = self._ledger()
        ledger.submit(self._record(
            session="s4", agent="a7",
            context={"input_tokens": 40_000, "output_tokens": 0},
            ts=datetime(2026, 10, 3, tzinfo=timezone.utc),
        ))
        self.assertIsNone(ledger.context_floor(session=None, agent="a7"))
        self.assertIsNone(ledger.context_floor(session=None, agent=None))

    def test_no_match_or_no_positive_is_none(self) -> None:
        ledger = self._ledger()
        self.assertIsNone(ledger.context_floor(session="nope", agent=None))
        ledger.submit(self._record(
            session="s3", agent=None,
            context={"input_tokens": 0, "output_tokens": 0},
            ts=datetime(2026, 10, 3, tzinfo=timezone.utc),
        ))
        self.assertIsNone(ledger.context_floor(session="s3", agent=None))

    def _facts(self, estimate: Optional[int]) -> RequestFacts:
        return RequestFacts(
            session="s1", agent="a1", parent_agent=None,
            count_tokens=False, count_estimate=estimate,
        )

    def test_the_larger_of_ledger_floor_and_bytes_estimate_wins(self) -> None:
        ledger = self._ledger()
        ledger.submit(self._record(
            session="s1", agent="a1",
            context={"input_tokens": 10_000, "output_tokens": 500},
            ts=datetime(2026, 10, 3, tzinfo=timezone.utc),
        ))
        gateway = _stub_gateway(ledger)
        self.assertEqual(
            _message_start_usage_estimate(gateway, self._facts(800)), 10_500,
        )
        self.assertEqual(
            _message_start_usage_estimate(gateway, self._facts(99_999)),
            99_999,
        )

    def test_empty_ledger_falls_back_to_the_bytes_estimate(self) -> None:
        gateway = _stub_gateway(self._ledger())
        self.assertEqual(
            _message_start_usage_estimate(gateway, self._facts(432)), 432,
        )

    def test_nothing_to_say_is_none(self) -> None:
        gateway = _stub_gateway(self._ledger())
        self.assertIsNone(_message_start_usage_estimate(gateway, self._facts(None)))
        self.assertIsNone(_message_start_usage_estimate(gateway, None))


class VendorFlagDataTests(unittest.TestCase):
    """The flag is DATA: pin exactly the rows the measurements flagged."""

    def test_flagged_rows_are_exactly_the_measured_vendors(self) -> None:
        flagged = {
            vendor_id
            for vendor_id, vendor in VENDORS.items()
            if vendor.partial_message_start_usage
        }
        # z.ai: all-zero message_start (110/598 transcript entries).
        # qwen: partial message_start (under-reported input, no cache fields).
        # Any NEW row here needs its own live capture in
        # tests/fixtures/model_router/ — the flag is a claim about measured
        # vendor behaviour, not a guess.
        self.assertEqual(flagged, {"zai", "qwen"})

    def test_unflagged_is_the_default(self) -> None:
        field = {
            f.name: f for f in dataclasses.fields(VENDORS["zai"])
        }["partial_message_start_usage"]
        self.assertIs(field.default, False)


if __name__ == "__main__":
    unittest.main()
