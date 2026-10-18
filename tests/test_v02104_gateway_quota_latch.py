# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 — the subscription-out latch (owner 2026-10-18).

With the opt-in subscription -> API-key fallback ON, a qualifying
subscription quota refusal (the existing OD-9 escalation) now also records
WHICH limit is out and UNTIL WHEN; until then, first-party requests in scope
go straight to the Claude API key with no subscription attempt. A 5h or
overall-weekly limit latches every Claude model; a per-model weekly limit
only the models it covers. Clears at the reset time, on a usage-window
reading under 100 % taken after the latch, or when the key is refused.

No network: loopback stubs. Every key is an obvious fake.
"""
from __future__ import annotations

import json
import logging
import time
import unittest
from typing import Any

from model_router import quota_latch as ql
from model_router import server as srv
from model_router import usage_windows as uw
from model_router.usage_windows import Window
from tests.test_v02102_gateway_routing import EXHAUSTED, RATE_LIMITED, _success
from tests.test_v02104_gateway_api_key_route import (
    FAKE_API_KEY,
    HAIKU,
    OPUS,
    ApiKeyGatewayBase,
    _header,
)

SONNET = "claude-sonnet-5-5"
FABLE = "claude-fable-5-1"
NOW = 1_800_000_000.0
LATCH_LOGGER = "model_router.quota_latch"


def _claim(claim: str, reset: float, **extra: str) -> dict[str, str]:
    return {
        "anthropic-ratelimit-unified-status": "rejected",
        "anthropic-ratelimit-unified-representative-claim": claim,
        "anthropic-ratelimit-unified-reset": str(int(reset)),
        **extra,
    }


def _refusal(headers: dict[str, str] | None = None, body: bytes = RATE_LIMITED) -> ql.QuotaRefusal:
    return ql.QuotaRefusal(429, headers or {}, body)


def _window(wid: str, percent: float | None, *, resets: float | None, seen: float) -> Window:
    return Window(wid, wid, percent, resets, uw.SOURCE_OAUTH_USAGE, seen)


# ══════════════════════════════════════════════════════════════════════════
# Deriving limit / scope / until (pure)
# ══════════════════════════════════════════════════════════════════════════
class DerivationTests(unittest.TestCase):

    def test_a_5h_claim_latches_every_model_until_the_unified_reset(self) -> None:
        [latch] = ql.latches_for_refusal(
            OPUS, _refusal(_claim("five_hour", NOW + 3600)), [], now=NOW,
        )
        self.assertEqual((latch.limit, latch.scope), ("5h", "all"))
        self.assertEqual(latch.until, NOW + 3600)
        self.assertEqual(latch.source, ql.SOURCE_REFUSAL_HEADERS)
        for model in (OPUS, HAIKU, SONNET):
            self.assertTrue(latch.covers(model))

    def test_a_weekly_claim_latches_every_model(self) -> None:
        [latch] = ql.latches_for_refusal(
            HAIKU, _refusal(_claim("seven_day", NOW + 86400 * 3)), [], now=NOW,
        )
        self.assertEqual((latch.limit, latch.scope), ("weekly", "all"))

    def test_a_per_model_weekly_claim_latches_only_that_model(self) -> None:
        [latch] = ql.latches_for_refusal(
            OPUS, _refusal(_claim("seven_day_opus", NOW + 86400)), [], now=NOW,
        )
        self.assertEqual((latch.limit, latch.scope), ("weekly:Opus", "opus"))
        self.assertTrue(latch.covers(OPUS))
        self.assertTrue(latch.covers("claude-opus-5-5[1m]"))
        self.assertFalse(latch.covers(SONNET))
        self.assertFalse(latch.covers(HAIKU))

    def test_the_per_window_reset_serves_when_the_overall_one_is_absent(self) -> None:
        headers = {
            "anthropic-ratelimit-unified-representative-claim": "five_hour",
            "anthropic-ratelimit-unified-5h-reset": str(int(NOW + 1800)),
        }
        [latch] = ql.latches_for_refusal(OPUS, _refusal(headers), [], now=NOW)
        self.assertEqual(latch.until, NOW + 1800)

    def test_without_a_claim_a_rejected_window_names_the_limit(self) -> None:
        headers = {
            "anthropic-ratelimit-unified-7d-status": "rejected",
            "anthropic-ratelimit-unified-7d-reset": str(int(NOW + 7200)),
            "anthropic-ratelimit-unified-5h-utilization": "0.4",
        }
        [latch] = ql.latches_for_refusal(OPUS, _refusal(headers), [], now=NOW)
        self.assertEqual((latch.limit, latch.scope, latch.until), ("weekly", "all", NOW + 7200))

    def test_the_usage_windows_name_the_limit_when_the_refusal_does_not(self) -> None:
        windows = [
            _window("5h", 100.0, resets=NOW + 900, seen=NOW - 10),
            _window("weekly", 40.0, resets=NOW + 86400, seen=NOW - 10),
        ]
        [latch] = ql.latches_for_refusal(OPUS, _refusal(), windows, now=NOW)
        self.assertEqual((latch.limit, latch.scope, latch.until), ("5h", "all", NOW + 900))
        self.assertEqual(latch.source, ql.SOURCE_USAGE_WINDOWS)

    def test_a_per_model_window_applies_only_to_a_model_it_covers(self) -> None:
        windows = [_window("weekly:Fable", 100.0, resets=NOW + 86400, seen=NOW - 10)]
        [fable] = ql.latches_for_refusal(FABLE, _refusal(), windows, now=NOW)
        self.assertEqual((fable.limit, fable.scope), ("weekly:Fable", "fable"))
        self.assertFalse(fable.covers(OPUS))
        # An Opus refusal is not explained by the Fable window.
        [opus] = ql.latches_for_refusal(OPUS, _refusal(), windows, now=NOW)
        self.assertEqual(opus.limit, ql.LIMIT_UNKNOWN)

    def test_a_stale_window_is_not_evidence(self) -> None:
        windows = [_window("5h", 100.0, resets=NOW + 900, seen=NOW - uw.STALE_AFTER_S - 1)]
        [latch] = ql.latches_for_refusal(OPUS, _refusal(), windows, now=NOW)
        self.assertEqual(latch.limit, ql.LIMIT_UNKNOWN)

    def test_a_reset_hint_alone_latches_only_the_refused_family(self) -> None:
        [latch] = ql.latches_for_refusal(
            OPUS, _refusal({"retry-after": "1200"}), [], now=NOW,
        )
        self.assertEqual((latch.limit, latch.scope), (ql.LIMIT_UNKNOWN, "opus"))
        self.assertEqual(latch.source, ql.SOURCE_RESET_HINT)
        self.assertAlmostEqual(latch.until, NOW + 1200, delta=5)
        self.assertFalse(latch.covers(SONNET))

    def test_no_reset_time_gets_the_conservative_short_latch(self) -> None:
        [latch] = ql.latches_for_refusal(OPUS, _refusal(), [], now=NOW)
        self.assertEqual(latch.until, NOW + ql.CONSERVATIVE_LATCH_S)
        self.assertEqual(latch.source, ql.SOURCE_CONSERVATIVE)
        self.assertEqual(latch.scope, "opus")
        # A named limit with no end gets the same short latch, never none.
        [named] = ql.latches_for_refusal(
            OPUS, _refusal({"anthropic-ratelimit-unified-representative-claim": "five_hour"}),
            [], now=NOW,
        )
        self.assertEqual((named.limit, named.until), ("5h", NOW + ql.CONSERVATIVE_LATCH_S))

    def test_no_latch_outlives_the_cap(self) -> None:
        [latch] = ql.latches_for_refusal(
            OPUS, _refusal(_claim("seven_day", NOW + 90 * 86400)), [], now=NOW,
        )
        self.assertEqual(latch.until, NOW + ql.MAX_LATCH_S)


# ══════════════════════════════════════════════════════════════════════════
# The latch's life (fake clock)
# ══════════════════════════════════════════════════════════════════════════
class LatchLifecycleTests(unittest.TestCase):

    def setUp(self) -> None:
        self.clock = [NOW]
        self.latch = ql.QuotaLatch(clock=lambda: self.clock[0])

    def test_arming_and_clearing_each_log_one_info_line(self) -> None:
        with self.assertLogs(LATCH_LOGGER, level="INFO") as cm:
            self.latch.note_refusal(OPUS, _refusal(_claim("five_hour", NOW + 600)), [])
            self.clock[0] = NOW + 601
            self.assertIsNone(self.latch.active_for(OPUS))
        self.assertEqual(len(cm.output), 2)
        self.assertIn("subscription 5h limit exhausted until", cm.output[0])
        self.assertIn("sending every Claude model to the Claude API key", cm.output[0])
        self.assertIn("latch cleared (its reset time passed)", cm.output[1])

    def test_it_clears_at_the_reset_time(self) -> None:
        self.latch.note_refusal(OPUS, _refusal(_claim("five_hour", NOW + 600)), [])
        self.clock[0] = NOW + 599
        self.assertIsNotNone(self.latch.active_for(HAIKU))
        self.clock[0] = NOW + 600
        self.assertIsNone(self.latch.active_for(HAIKU))
        self.assertEqual(self.latch.report(), [])

    def test_it_clears_when_a_later_window_reading_is_under_100(self) -> None:
        self.latch.note_refusal(OPUS, _refusal(_claim("five_hour", NOW + 3600)), [])
        self.clock[0] = NOW + 100
        before = [_window("5h", 20.0, resets=NOW + 3600, seen=NOW - 5)]
        self.assertIsNotNone(self.latch.active_for(OPUS, before), "older reading ignored")
        still_full = [_window("5h", 100.0, resets=NOW + 3600, seen=NOW + 50)]
        self.assertIsNotNone(self.latch.active_for(OPUS, still_full))
        recovered = [_window("5h", 30.0, resets=NOW + 3600, seen=NOW + 90)]
        self.assertIsNone(self.latch.active_for(OPUS, recovered))

    def test_an_unknown_limit_needs_both_windows_under_100(self) -> None:
        self.latch.note_refusal(OPUS, _refusal(), [])
        self.clock[0] = NOW + 10
        half = [_window("5h", 30.0, resets=NOW + 3600, seen=NOW + 5)]
        self.assertIsNotNone(self.latch.active_for(OPUS, half))
        both = half + [_window("weekly", 50.0, resets=NOW + 86400, seen=NOW + 5)]
        self.assertIsNone(self.latch.active_for(OPUS, both))

    def test_a_refused_key_clears_and_holds_off_until_accepted(self) -> None:
        self.latch.note_refusal(OPUS, _refusal(_claim("five_hour", NOW + 3600)), [])
        self.latch.key_refused()
        self.assertIsNone(self.latch.active_for(OPUS))
        self.assertEqual(
            self.latch.note_refusal(OPUS, _refusal(_claim("five_hour", NOW + 3600)), []), [],
        )
        self.latch.key_accepted()
        self.assertTrue(
            self.latch.note_refusal(OPUS, _refusal(_claim("five_hour", NOW + 3600)), []),
        )

    def test_a_per_model_latch_and_a_5h_latch_coexist(self) -> None:
        self.latch.note_refusal(OPUS, _refusal(_claim("seven_day_opus", NOW + 86400)), [])
        self.assertIsNone(self.latch.active_for(SONNET))
        self.latch.note_refusal(SONNET, _refusal(_claim("five_hour", NOW + 600)), [])
        self.assertEqual(self.latch.active_for(OPUS).limit, "weekly:Opus")  # ends last
        self.assertEqual(self.latch.active_for(SONNET).limit, "5h")
        self.assertEqual([r["limit"] for r in self.latch.report()], ["5h", "weekly:Opus"])


class RenderTests(unittest.TestCase):

    def test_the_line_names_the_latch(self) -> None:
        until = time.time() + 3600
        snapshot = {"vendors": [{
            "id": "anthropic", "label": "Claude",
            "windows": [{"id": "5h", "label": "5h", "percent": 100.0, "source": "x"}],
            "api_key_latch": [ql.Latch("5h", "all", until, until - 10, "x").as_dict()],
        }]}
        clock = time.strftime("%H:%M", time.localtime(int(until)))
        self.assertEqual(uw.render_line(snapshot), f"Claude 5h 100% → API key until {clock}")

    def test_a_per_model_latch_names_the_model_and_a_far_reset_the_day(self) -> None:
        until = time.time() + 3 * 86400
        text = uw.latch_text([ql.Latch("weekly:Opus", "opus", until, 0, "x").as_dict()])
        self.assertTrue(text.startswith("→ Opus on API key until "), text)
        self.assertIn(time.strftime("%a", time.localtime(int(until))), text)

    def test_a_latch_with_no_known_window_still_renders(self) -> None:
        until = time.time() + 60
        snapshot = {"vendors": [{
            "id": "anthropic", "label": "Claude", "windows": [],
            "api_key_latch": [ql.Latch("unknown", "opus", until, 0, "x").as_dict()],
        }]}
        self.assertTrue(uw.render_line(snapshot).startswith("Claude → Opus on API key until"))

    def test_no_latch_renders_exactly_as_before(self) -> None:
        snapshot = {"vendors": [{
            "id": "anthropic", "label": "Claude",
            "windows": [{"id": "5h", "label": "5h", "percent": 31.0, "source": "x"}],
        }]}
        self.assertEqual(uw.render_line(snapshot), "Claude 5h 31%")


# ══════════════════════════════════════════════════════════════════════════
# End to end through the gateway
# ══════════════════════════════════════════════════════════════════════════
class LatchGatewayTests(ApiKeyGatewayBase):

    def refuse_then_serve(self, headers: dict[str, str]) -> None:
        """One subscription refusal, then 200s (the stub repeats its last
        entry, so every later attempt — on either credential — succeeds)."""
        ok = json.dumps(_success(OPUS)).encode()
        self.first.script = [(429, headers, RATE_LIMITED), (200, {}, ok)]

    async def post(self, client: Any, model: str) -> Any:
        return await client.post("/v1/messages", json=self.ask(model), headers=self.headers())

    def auths(self) -> list[str]:
        return [
            "key" if _header(r["headers"], "x-api-key") == FAKE_API_KEY else "sub"
            for r in self.first.message_requests
        ]

    async def test_a_5h_refusal_sends_every_model_straight_to_the_key(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        self.refuse_then_serve(_claim("five_hour", time.time() + 3600))
        self.assertEqual((await self.post(client, OPUS)).status, 200)
        self.assertEqual(self.auths(), ["sub", "key"])
        with self.assertLogs("model_router.server", level="INFO") as cm:
            self.assertEqual((await self.post(client, OPUS)).status, 200)
            self.assertEqual((await self.post(client, HAIKU)).status, 200)
        # ZERO subscription attempts while latched.
        self.assertEqual(self.auths(), ["sub", "key", "key", "key"])
        self.assertIn("note=quota_latched latch=5h scope=all", "\n".join(cm.output))
        rows = await self.rows(client)
        self.assertEqual([r["latched"] for r in rows], [False, True, True])
        self.assertTrue(all(r["auth"] == "api_key" for r in rows))
        self.assertEqual(rows[1]["fallback_from"], f"anthropic:{OPUS}")
        self.assertEqual(rows[2]["fallback_from"], f"anthropic:{HAIKU}")

    async def test_a_per_model_weekly_refusal_latches_only_that_model(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        self.refuse_then_serve(_claim("seven_day_opus", time.time() + 86400))
        await self.post(client, OPUS)
        await self.post(client, OPUS)
        self.assertEqual(self.auths(), ["sub", "key", "key"])
        await self.post(client, SONNET)
        self.assertEqual(self.auths(), ["sub", "key", "key", "sub"])

    async def test_fallback_off_the_latch_changes_nothing(self) -> None:
        client = await self.client_with(key=True, fallback=False)
        gateway = client.app[srv.APP_KEY]
        gateway.quota_latch.note_refusal(OPUS, _refusal(_claim("five_hour", time.time() + 3600)), [])
        self.first.script = [(429, {}, EXHAUSTED)]
        resp = await self.post(client, OPUS)
        self.assertEqual(resp.status, 429)
        self.assertEqual(await resp.read(), EXHAUSTED)
        self.assertEqual(self.auths(), ["sub"])

    async def test_it_clears_at_the_reset_time_and_asks_the_subscription_again(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        gateway = client.app[srv.APP_KEY]
        clock = [time.time()]
        gateway.quota_latch._clock = lambda: clock[0]
        self.refuse_then_serve(_claim("five_hour", clock[0] + 600))
        await self.post(client, OPUS)
        await self.post(client, OPUS)
        self.assertEqual(self.auths(), ["sub", "key", "key"])
        clock[0] += 601
        await self.post(client, OPUS)
        self.assertEqual(self.auths(), ["sub", "key", "key", "sub"])

    async def test_it_clears_when_the_usage_windows_recover(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        gateway = client.app[srv.APP_KEY]
        self.refuse_then_serve(_claim("five_hour", time.time() + 3600))
        await self.post(client, OPUS)
        self.assertTrue(gateway.quota_latch.report())
        gateway.usage_windows.observe_anthropic_headers({
            "anthropic-ratelimit-unified-5h-utilization": "0.1",
            "anthropic-ratelimit-unified-5h-reset": str(int(time.time() + 3600)),
        })
        await self.post(client, OPUS)
        self.assertEqual(self.auths(), ["sub", "key", "sub"])

    async def test_a_refused_key_clears_the_latch_and_the_user_sees_the_subscription(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        gateway = client.app[srv.APP_KEY]
        gateway.quota_latch.note_refusal(OPUS, _refusal(_claim("five_hour", time.time() + 3600)), [])
        denied = b'{"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}'
        self.first.script = [(401, {}, denied), (429, {}, EXHAUSTED), (401, {}, denied)]
        with self.assertLogs("model_router.quota_latch", level="INFO") as cm:
            resp = await self.post(client, OPUS)
        self.assertEqual(resp.status, 429)
        self.assertEqual(await resp.read(), EXHAUSTED)
        # The latched key attempt, then the subscription, then the ordinary
        # fallback's own key attempt (today's behaviour) — and no re-latch.
        self.assertEqual(self.auths(), ["key", "sub", "key"])
        self.assertEqual(gateway.quota_latch.report(), [])
        self.assertIn("the Claude API key was refused", "\n".join(cm.output))

    async def test_health_and_the_usage_line_show_the_latch(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        self.refuse_then_serve(_claim("seven_day_opus", time.time() + 7200))
        await self.post(client, OPUS)
        auth = (await (await client.get("/health")).json())["anthropic_auth"]
        [latch] = auth["quota_latch"]
        self.assertEqual((latch["limit"], latch["scope"]), ("weekly:Opus", "opus"))
        self.assertTrue(latch["until"].endswith("Z"))
        line = await (await client.get(
            "/usage/windows?format=line", headers=self.auth(),
        )).text()
        self.assertIn("→ Opus on API key until", line)
        snap = await (await client.get("/usage/windows", headers=self.auth())).json()
        claude = next(v for v in snap["vendors"] if v["id"] == "anthropic")
        self.assertEqual(claude["api_key_latch"][0]["limit"], "weekly:Opus")
        self.assertTrue(claude["api_key_latch_text"].startswith("→ Opus on API key until"))

    async def test_without_a_latch_health_reports_an_empty_list(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        auth = (await (await client.get("/health")).json())["anthropic_auth"]
        self.assertEqual(auth["quota_latch"], [])


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
