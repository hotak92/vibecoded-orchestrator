# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
r"""W3-B0 (S16-10 + S16-11(b)) — the vendor context ceiling, end to end.

The incident (2026-10-06): a qwen3.8-max subagent ran to 983,981 tokens
(ledger), then died permanently on the vendor's HTTP 400 ``InvalidParameter``
``"Range of input length should be [1, 983616]"``. Two things were wrong at
once, and both are fixed and pinned here:

1. **the client never compacted.** Claude Code budgets 1,000,000 tokens for
   any ``[1m]`` id, and the vendor's real ceiling is 983,616 — so proactive
   compaction never fired; and the vendor's error shape is not one the
   client's classifier maps to ``prompt_too_long``, so the REACTIVE
   compaction never fired either. The fix is the seed row (983,616, see
   ``tests/test_model_router_context_table.py``) plus a request-path GUARD
   that refuses, before the vendor is called, in Anthropic's own
   prompt-too-long envelope — the exact shape the client recovers from by
   compacting.
2. **the vendor's refusal was untranslatable.** When a request the guard's
   estimate let through DOES overflow, the native error is re-emitted as the
   same envelope, with the ceiling read out of the vendor's own message.

The envelope's message must stay parseable by the client's regex
(``prompt is too long[^0-9]*(\d+)\s*tokens?\s*>\s*(\d+)``): every assertion
here reads it back with that regex rather than with an equality that could
pass while drifting out of the shape the client actually parses.

Fixture provenance: the two ``qwen-overflow-400*.body`` fixtures are
RECONSTRUCTED from the incident record — status 400, code ``InvalidParameter``
and the message text are what the record states; the envelope fields around
them are the compatible-mode shape and no live capture of the body exists.
Nothing here asserts those surrounding fields.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
import unittest
from pathlib import Path
from typing import Any

from model_router import server as srv
from model_router.vendors import VENDORS, context_overflow_maximum

from tests.test_model_router_server import GatewayTestBase

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_router"
OVERFLOW_JSON = (FIXTURES / "qwen-overflow-400.body").read_bytes()
OVERFLOW_SSE = (FIXTURES / "qwen-overflow-400-sse.body").read_bytes()

#: What the client's error classifier parses out of the message (read from
#: the shipped CLI's native binary; see the W3-B0 brief). Every envelope
#: this module emits must satisfy it.
PROMPT_TOO_LONG_RE = re.compile(
    r"prompt is too long[^0-9]*(\d+)\s*tokens?\s*>\s*(\d+)",
)

QWEN_MAX = "claude-gw/qwen/qwen3.8-max"

#: The real ceiling the seed now cites for qwen3.8-max (S16-10).
QWEN_WINDOW = 983_616


class ContextCeilingTestBase(GatewayTestBase):
    """The stock fixture, rewired so the QWEN vendor row serves the stub.

    The stock base wires the zai row to the loopback stub; the guard and the
    translation are vendor-generic but the incident is qwen's, and the qwen
    row is the one carrying the overflow markers and the 983,616 ceiling.
    """

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        stub_url = str(self.vendor_up.server.make_url("")).rstrip("/")
        self.qwen = dataclasses.replace(VENDORS["qwen"], upstream=stub_url)
        # Both stub vendors registered: the incident model is qwen's, but
        # the smallest real window among guarded rows is glm-5.1's 200K
        # (zai row), which is where a falsely-judged screenshot bites first.
        self.client = await self.make_client(
            vendors={"zai": self.vendor, "qwen": self.qwen},
        )

    def chat(self, session: str = "chat-1", **extra: str) -> dict[str, str]:
        return {**self.auth(), "x-claude-code-session-id": session, **extra}

    async def rows(self) -> list[dict]:
        await self.client.app[srv.APP_KEY].usage.drain()
        path = self.usage_ledger_path
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]


    def body(
        self, content: str, model: str = QWEN_MAX, stream: bool = True,
    ) -> dict[str, Any]:
        return {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "stream": stream,
        }

    #: ~1.05M tokens at bytes/4 — past the RAW window, refused uncalibrated.
    HUGE = "y" * 4_200_000

    def vendor_stream(self, input_tokens: int, output_tokens: int) -> list[bytes]:
        """A stub stream reporting the given usage in both events."""
        start_usage = json.dumps({
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 1,
        }).encode()
        delta_usage = json.dumps({
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": output_tokens,
        }).encode()
        return [
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"id":"m1","usage":'
            + start_usage
            + b'}}\n\n',
            b'event: message_delta\n'
            b'data: {"type":"message_delta","delta":{},"usage":'
            + delta_usage
            + b'}\n\n',
            b"event: message_stop\n\n",
        ]

    async def turn(
        self, session: str, content: str, *,
        input_tokens: int, output_tokens: int,
    ) -> None:
        """One accepted vendor turn that reports the given usage."""
        self.vendor_up.stream_chunks = self.vendor_stream(
            input_tokens, output_tokens,
        )
        resp = await self.client.post(
            "/v1/messages",
            headers=self.chat(session),
            json=self.body(content),
        )
        self.assertEqual(resp.status, 200, "calibration turn must succeed")
        await resp.read()


class GuardTests(ContextCeilingTestBase):
    """The request-path guard: refuse what cannot fit, forward what can.

    Since the CRITICAL-1 fix the estimate always describes the CURRENT
    request — its own body size times the chat's measured tokens-per-byte —
    never the previous turn's absolute ``context_after`` (which only rises
    and bricked sessions). The scenarios below pin both directions of that.
    """

    async def test_over_the_raw_window_is_refused_with_the_exact_envelope(self) -> None:
        """The guard's whole job: the client must see a shape it recovers
        from, not a vendor 400 it will retry unchanged. Uncalibrated (first
        request of a chat), so the threshold is the RAW window — the
        bytes/4 fallback over-estimates and the translation is the net."""
        with self.assertLogs(srv.logger, level="WARNING") as logs:
            resp = await self.client.post(
                "/v1/messages",
                headers=self.chat("guard-1"),
                json=self.body(self.HUGE),
            )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        match = PROMPT_TOO_LONG_RE.search(body["error"]["message"])
        self.assertIsNotNone(match, body["error"]["message"])
        assert match is not None
        self.assertEqual(int(match.group(2)), QWEN_WINDOW)
        self.assertGreater(int(match.group(1)), QWEN_WINDOW)
        # The vendor was never called — the refusal is the point.
        self.assertEqual(self.vendor_up.message_requests, [])
        self.assertTrue(any("context guard" in line for line in logs.output))

    async def test_under_the_ceiling_is_forwarded(self) -> None:
        self.vendor_up.stream_chunks = [b"event: message_stop\n\n"]
        resp = await self.client.post(
            "/v1/messages",
            headers=self.chat("guard-2"),
            json=self.body("hi"),
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.vendor_up.message_requests), 1)

    async def test_the_first_party_route_is_never_guarded(self) -> None:
        """Anthropic answers natively with this very envelope; a guard here
        could only pre-empt a correct answer."""
        self.anthropic_up.stream_chunks = [b"event: message_stop\n\n"]
        resp = await self.client.post(
            "/v1/messages",
            headers=self.chat("guard-3"),
            json=self.body(self.HUGE, model="claude-opus-5"),
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.anthropic_up.message_requests), 1)

    async def test_a_stale_high_turn_does_not_brick_the_chat(self) -> None:
        """CRITICAL-1, red-proofed: one turn at ~981k vendor-reported tokens
        used to make every later request in the session refusable — including
        the 5-token post-compaction one, because the old guard keyed on the
        previous turn's absolute ``context_after``, which refused requests
        also cannot lower. The calibrated estimate describes THIS request:
        tiny body, tiny estimate, forwarded — and the vendor's own refusal,
        translated, remains the net for what genuinely does not fit."""
        await self.turn(
            "guard-4", "hi", input_tokens=975_000, output_tokens=6_000,
        )
        rows = await self.rows()
        self.assertEqual(rows[-1]["context_after"], 981_000)

        # The post-compaction request: a few tokens of its own.
        self.vendor_up.stream_chunks = self.vendor_stream(50, 10)
        resp = await self.client.post(
            "/v1/messages",
            headers=self.chat("guard-4"),
            json=self.body("summarised so far: ok"),
        )
        self.assertEqual(resp.status, 200, "the chat must recover")
        self.assertEqual(len(self.vendor_up.message_requests), 2)

    async def test_the_compaction_request_passes_on_its_own_bytes(self) -> None:
        """The client's summarisation request carries the whole conversation,
        so its body is large — but the chat's measured density says it fits
        the real window, and the guard forwards it (recovery must complete).
        The bytes/4 fallback alone would refuse this same body."""
        # Calibrate at ~0.2 tokens/byte: 100k reported tokens over a ~500k
        # body. (Ratio below bytes/4's 0.25 — a cache-heavy chat.)
        await self.turn(
            "guard-5", "c" * 500_000, input_tokens=100_000, output_tokens=500,
        )
        # The compaction request: ~4.5M bytes -> ~900k calibrated tokens
        # (fits 975,424), where bytes/4 says ~1.13M (would refuse).
        self.vendor_up.stream_chunks = self.vendor_stream(1, 1)
        resp = await self.client.post(
            "/v1/messages",
            headers=self.chat("guard-5"),
            json=self.body("z" * 4_500_000),
        )
        self.assertEqual(resp.status, 200, "compaction must complete")
        self.assertEqual(len(self.vendor_up.message_requests), 2)

    async def test_a_calibrated_over_ceiling_request_is_refused(self) -> None:
        """The other direction: with a calibration, THIS request's own bytes
        say it cannot fit, and the refusal carries the exact envelope with
        the window-minus-margin threshold."""
        # Calibrate at a density that clamps to the 0.5 band ceiling.
        await self.turn(
            "guard-6", "c" * 400_000, input_tokens=300_000, output_tokens=500,
        )
        with self.assertLogs(srv.logger, level="WARNING"):
            resp = await self.client.post(
                "/v1/messages",
                headers=self.chat("guard-6"),
                json=self.body("x" * 2_200_000),
            )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        match = PROMPT_TOO_LONG_RE.search(body["error"]["message"])
        self.assertIsNotNone(match, body["error"]["message"])
        assert match is not None
        # Calibrated threshold: window minus the default 8192 margin.
        self.assertEqual(int(match.group(2)), QWEN_WINDOW - 8192)
        # ~2.2M body at the clamped 0.5 tokens/byte.
        self.assertGreater(int(match.group(1)), QWEN_WINDOW - 8192)
        # Only the calibration turn reached the vendor.
        self.assertEqual(len(self.vendor_up.message_requests), 1)

    async def test_an_uncalibrated_request_between_margin_and_window_forwards(self) -> None:
        """Bias toward forwarding: bytes/4 over-estimates, the translation is
        the net, so without a calibration only the RAW window refuses. A
        body estimating in the 975,424..983,616 band goes through."""
        self.vendor_up.stream_chunks = self.vendor_stream(1, 1)
        resp = await self.client.post(
            "/v1/messages",
            headers=self.chat("guard-7"),
            json=self.body("w" * 3_920_000),
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.vendor_up.message_requests), 1)

    async def test_sessions_do_not_share_calibration(self) -> None:
        """The calibration is per (session, agent): chat A's measured density
        must not refuse chat B's first request, and B's uncalibrated body —
        the same one A is refused for — forwards under the raw-window rule."""
        await self.turn(
            "guard-A", "c" * 400_000, input_tokens=300_000, output_tokens=500,
        )
        # A: same density, a body that cannot fit -> refused (calibrated).
        with self.assertLogs(srv.logger, level="WARNING"):
            resp_a = await self.client.post(
                "/v1/messages",
                headers=self.chat("guard-A"),
                json=self.body("x" * 2_200_000),
            )
        self.assertEqual(resp_a.status, 400)

        # B: no calibration of its own -> bytes/4 (~550k) under the raw
        # window -> forwarded.
        self.vendor_up.stream_chunks = self.vendor_stream(1, 1)
        resp_b = await self.client.post(
            "/v1/messages",
            headers=self.chat("guard-B"),
            json=self.body("x" * 2_200_000),
        )
        self.assertEqual(resp_b.status, 200)

        # A sibling AGENT of A does not inherit A's calibration either.
        self.vendor_up.stream_chunks = self.vendor_stream(1, 1)
        resp_sub = await self.client.post(
            "/v1/messages",
            headers={
                **self.chat("guard-A"),
                "x-claude-code-agent-id": "sub-1",
            },
            json=self.body("x" * 2_200_000),
        )
        self.assertEqual(resp_sub.status, 200)


    async def test_an_image_heavy_body_is_not_judged_on_a_text_calibration(self) -> None:
        """MEDIUM-1: base64 image bytes are ~1000x denser than the text a
        ratio is measured on — a text-calibrated density applied to a vision
        request refuses a body the vendor would serve (and compaction cannot
        shrink images, so the refusal would repeat forever). Such a body is
        estimated UNCALIBRATED, and it is not a calibration sample either."""
        # Calibrate on text: 3.3 MB body, 1M vendor-reported tokens
        # (ratio ~0.303 — the honest text density of a tool-heavy chat).
        await self.turn(
            "guard-8", "c" * 3_300_000, input_tokens=1_000_000,
            output_tokens=500,
        )
        rows = await self.rows()
        self.assertGreater(rows[-1]["request_bytes"], 0)

        # The vision request: one base64 image block inside a ~3.5 MB body.
        # The text calibration would estimate ~1.06M tokens (refused); real
        # tokens are a few thousand. The raw-window rule forwards it.
        self.vendor_up.stream_chunks = self.vendor_stream(5_000, 100)
        resp = await self.client.post(
            "/v1/messages",
            headers=self.chat("guard-8"),
            json={
                "model": QWEN_MAX,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "A" * 3_500_000,
                            },
                        },
                    ],
                }],
                "stream": True,
            },
        )
        self.assertEqual(resp.status, 200, "vision turn must reach the vendor")
        self.assertEqual(len(self.vendor_up.message_requests), 2)
        # The image turn is NOT a calibration sample: its row records no
        # measurable body, so later text turns do not inherit its density.
        rows = await self.rows()
        self.assertEqual(rows[-1]["request_bytes"], 0)

    async def test_a_binary_bearing_body_is_never_judged_at_all(self) -> None:
        """MEDIUM-R: every estimate the guard can make for a base64-bearing
        body is a text arithmetic that over-states it by orders of
        magnitude — on a 200K-window model ONE screenshot (~800 KB of
        base64) would be falsely refused, and compaction cannot shrink an
        image to fix it. Such bodies are forwarded unjudged; the
        vendor-overflow translation is the net."""
        def image_body(model: str, size: int) -> dict[str, Any]:
            return {
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "A" * size,
                            },
                        },
                    ],
                }],
                "stream": True,
            }

        # glm-5.1, the smallest guarded window (200K, zai row): a ~1 MB
        # screenshot bytes/4-estimates at ~250k tokens — over the RAW
        # window, a false refusal under any text rule.
        self.vendor_up.stream_chunks = self.vendor_stream(5_000, 100)
        resp = await self.client.post(
            "/v1/messages",
            headers=self.chat("guard-M1"),
            json=image_body("claude-gw/glm-5.1", 1_000_000),
        )
        self.assertEqual(resp.status, 200, "a screenshot is not 250k tokens")
        self.assertEqual(len(self.vendor_up.message_requests), 1)

        # The qwen shape: a multi-MB image read past the 983,616 raw window
        # on bytes/4 — same forwarding.
        resp = await self.client.post(
            "/v1/messages",
            headers=self.chat("guard-M2"),
            json=image_body(QWEN_MAX, 4_200_000),
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.vendor_up.message_requests), 2)


class GuardTableOverrideTests(GatewayTestBase):
    """L-1: the guard's window comes from the ACTIVE table; when a launcher
    export carries a different figure than the shipped seed for a guarded
    model, ONE INFO line names both — the export still wins per row."""

    async def test_an_exported_window_that_differs_from_the_seed_logs_once(self) -> None:
        from model_router import catalog as cat

        cat.reset_log_once(srv.LOG_ONCE_GUARD_TABLE_OVERRIDE)
        self.addCleanup(
            cat.reset_log_once, srv.LOG_ONCE_GUARD_TABLE_OVERRIDE,
        )
        # A pre-v0.2.102-style export: qwen3.8-max still at 1,000,000.
        (self.root / "chat_model_context.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-10-01T00:00:00Z",
                    "source": "launcher.db",
                    "models": {
                        "qwen3.8-max": {
                            "vendor": "qwen",
                            "context_window": 1_000_000,
                            "max_output": 128_000,
                            "window_1m": True,
                            "source": "https://docs.qwencloud.com/x",
                        },
                    },
                    "tombstones": [],
                },
            ),
            encoding="utf-8",
        )
        stub_url = str(self.vendor_up.server.make_url("")).rstrip("/")
        qwen = dataclasses.replace(VENDORS["qwen"], upstream=stub_url)
        client = await self.make_client(vendors={"qwen": qwen})
        self.vendor_up.stream_chunks = [b"event: message_stop\n\n"]
        with self.assertLogs(srv.logger, level=logging.INFO) as logs:
            resp = await client.post(
                "/v1/messages",
                headers={**self.auth(), "x-claude-code-session-id": "t-1"},
                json={
                    "model": "claude-gw/qwen/qwen3.8-max",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
            await resp.read()
            # A second request says nothing more: once per model.
            resp = await client.post(
                "/v1/messages",
                headers={**self.auth(), "x-claude-code-session-id": "t-2"},
                json={
                    "model": "claude-gw/qwen/qwen3.8-max",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
            await resp.read()
        self.assertEqual(resp.status, 200)
        noted = [line for line in logs.output if "shipped seed says" in line]
        self.assertEqual(len(noted), 1, logs.output)
        self.assertIn("983616", noted[0])
        self.assertIn("1000000", noted[0])


class GuardStandDownTests(ContextCeilingTestBase):
    """MEDIUM-2: a guard that refuses the client's reactive-compaction
    request — whose body carries ≈ the refused history — blocks its own
    recovery path. The guard stands down for a body that follows a refusal
    without meaningfully shrinking, once, and lets the vendor answer."""

    async def test_the_compaction_shaped_retry_is_forwarded_once(self) -> None:
        # Calibrate at ~0.285: 968k reported tokens over a ~3.4 MB body.
        await self.turn(
            "guard-9", "c" * 3_400_000, input_tokens=968_000,
            output_tokens=500,
        )
        # A request estimating ~982k — past the 975,424 calibrated ceiling,
        # under the 983,616 raw window (so the calibrated path is what
        # refuses it).
        refused = {
            "model": QWEN_MAX,
            "messages": [{"role": "user", "content": "x" * 3_450_000}],
            "stream": True,
        }
        with self.assertLogs(srv.logger, level="WARNING"):
            resp = await self.client.post(
                "/v1/messages", headers=self.chat("guard-9"), json=refused,
            )
        self.assertEqual(resp.status, 400)

        # The compaction request: ≈ the same body (the client resends the
        # history with a summarisation instruction). The guard stands down —
        # one INFO line — and the vendor answers; it reports ~980k tokens,
        # the band the vendor serves and the guard's margin refuses.
        self.vendor_up.stream_chunks = self.vendor_stream(980_000, 100)
        with self.assertLogs(srv.logger, level="INFO") as logs:
            resp = await self.client.post(
                "/v1/messages",
                headers=self.chat("guard-9"),
                json={
                    "model": QWEN_MAX,
                    "messages": [{
                        "role": "user",
                        "content": "summarise: " + "x" * 3_450_000,
                    }],
                    "stream": True,
                },
            )
        self.assertEqual(resp.status, 200, "the recovery must go through")
        self.assertEqual(len(self.vendor_up.message_requests), 2)
        self.assertTrue(any("stood down" in line for line in logs.output))

        # The stand-down is ONCE, not a switch off: the next same-size body
        # is judged again (and refused — it is over the calibrated ceiling).
        with self.assertLogs(srv.logger, level="WARNING"):
            resp = await self.client.post(
                "/v1/messages", headers=self.chat("guard-9"), json=refused,
            )
        self.assertEqual(resp.status, 400)

    async def test_a_meaningfully_smaller_body_does_not_trigger_the_stand_down(self) -> None:
        """A body that SHRANK (a compaction that worked, or a new topic)
        resumes the ordinary rule — the stand-down exists for the
        not-smaller shape only. Calibrated at the clamped 0.5 so a body 7%
        smaller still estimates over the ceiling."""
        await self.turn(
            "guard-B", "c" * 400_000, input_tokens=300_000, output_tokens=500,
        )
        with self.assertLogs(srv.logger, level="WARNING"):
            resp = await self.client.post(
                "/v1/messages",
                headers=self.chat("guard-B"),
                json=self.body("x" * 2_200_000),
            )
        self.assertEqual(resp.status, 400)
        # ~7% smaller — past the 5% shrink threshold, still over the
        # calibrated ceiling: judged again, refused again, no stand-down.
        self.vendor_up.stream_chunks = self.vendor_stream(10, 10)
        with self.assertLogs(srv.logger, level="WARNING") as logs:
            resp = await self.client.post(
                "/v1/messages",
                headers=self.chat("guard-B"),
                json=self.body("y" * 2_050_000),
            )
        self.assertEqual(resp.status, 400)
        self.assertFalse(any("stood down" in line for line in logs.output))


class OverflowTranslationTests(ContextCeilingTestBase):
    """The vendor's own overflow refusal, re-emitted recoverable."""

    async def test_a_json_overflow_refusal_is_translated(self) -> None:
        self.vendor_up.messages_status = 400
        self.vendor_up.messages_raw = OVERFLOW_JSON
        resp = await self.client.post(
            "/v1/messages",
            headers=self.chat("over-1"),
            json={
                "model": QWEN_MAX,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertEqual(body["type"], "error")
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        match = PROMPT_TOO_LONG_RE.search(body["error"]["message"])
        self.assertIsNotNone(match, body["error"]["message"])
        assert match is not None
        # The maximum is the VENDOR'S own figure, out of its own message.
        self.assertEqual(match.group(2), str(QWEN_WINDOW))

    async def test_an_sse_overflow_refusal_is_translated(self) -> None:
        """The streamed shape: an ``event: error`` body must not survive as
        SSE frames the client's JSON error path never parses."""
        self.vendor_up.messages_status = 400
        self.vendor_up.messages_raw = OVERFLOW_SSE
        self.vendor_up.content_type = "text/event-stream"
        resp = await self.client.post(
            "/v1/messages",
            headers=self.chat("over-2"),
            json={
                "model": QWEN_MAX,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        match = PROMPT_TOO_LONG_RE.search(body["error"]["message"])
        self.assertIsNotNone(match, body["error"]["message"])

    async def test_a_translated_refusal_names_the_previous_turns_count(self) -> None:
        """``N tokens`` is the BEST known estimate: the ledger's last
        vendor-reported context_after for the chat, when there is one."""
        stream = (
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"id":"m1","usage":'
            b'{"input_tokens":400000,"cache_creation_input_tokens":0,'
            b'"cache_read_input_tokens":0,"output_tokens":1}}}\n\n'
            b'event: message_delta\n'
            b'data: {"type":"message_delta","delta":{},"usage":'
            b'{"input_tokens":400000,"cache_creation_input_tokens":0,'
            b'"cache_read_input_tokens":0,"output_tokens":900}}\n\n'
            b"event: message_stop\n\n"
        )
        self.vendor_up.stream_chunks = [stream]
        resp = await self.client.post(
            "/v1/messages",
            headers=self.chat("over-3"),
            json={
                "model": QWEN_MAX,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        await resp.read()

        self.vendor_up.stream_chunks = None
        self.vendor_up.messages_status = 400
        self.vendor_up.messages_raw = OVERFLOW_JSON
        resp = await self.client.post(
            "/v1/messages",
            headers=self.chat("over-3"),
            json={
                "model": QWEN_MAX,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        match = PROMPT_TOO_LONG_RE.search(body["error"]["message"])
        self.assertIsNotNone(match, body["error"]["message"])
        assert match is not None
        self.assertEqual(match.group(1), "400900")

    async def test_an_unrelated_vendor_400_relays_unchanged(self) -> None:
        """Only the overflow shape is translated; every other error is the
        vendor's own answer, byte for byte."""
        unrelated = b'{"error":{"code":"InvalidParameter","message":"bad tools"}}'
        self.vendor_up.messages_status = 400
        self.vendor_up.messages_raw = unrelated
        resp = await self.client.post(
            "/v1/messages",
            headers=self.chat("over-4"),
            json={
                "model": QWEN_MAX,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(await resp.read(), unrelated)


class OverflowMarkerTests(unittest.TestCase):
    """The detection itself, data-driven off the vendor row."""

    def test_the_qwen_shape_yields_the_vendor_ceiling(self) -> None:
        self.assertEqual(
            context_overflow_maximum(VENDORS["qwen"], OVERFLOW_JSON),
            QWEN_WINDOW,
        )
        self.assertEqual(
            context_overflow_maximum(VENDORS["qwen"], OVERFLOW_SSE),
            QWEN_WINDOW,
        )

    def test_vendors_without_a_declared_shape_answer_none(self) -> None:
        """z.ai and Kimi have no documented overflow shape in the repo —
        empty markers mean "relay verbatim", never a guess."""
        for vendor_id in ("zai", "kimi"):
            self.assertIsNone(
                context_overflow_maximum(
                    VENDORS[vendor_id], OVERFLOW_JSON,
                ),
                vendor_id,
            )

    def test_a_marker_without_a_parseable_range_answers_none(self) -> None:
        self.assertIsNone(
            context_overflow_maximum(
                VENDORS["qwen"],
                b'{"error":{"message":"Range of input length should be enormous"}}',
            ),
        )
