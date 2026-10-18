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
from pathlib import Path
from typing import Any

from model_router import quota_latch as ql
from model_router import server as srv
from model_router import usage_windows as uw
from model_router.usage_windows import Window
from tests.test_v02102_gateway_routing import EXHAUSTED, RATE_LIMITED, _success
from tests.test_v02104_gateway_api_key_route import (
    API_NS,
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


def _refusal(
    headers: dict[str, str] | None = None, body: bytes = RATE_LIMITED,
    classification: str = "rate_limited",
) -> ql.QuotaRefusal:
    return ql.QuotaRefusal(429, headers or {}, body, classification)


def _exhausted(headers: dict[str, str] | None = None) -> ql.QuotaRefusal:
    """A refusal ``classify_quota`` called ``exhausted`` (no named window)."""
    return _refusal(headers, EXHAUSTED, ql.CLASS_EXHAUSTED)


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
            "anthropic-ratelimit-unified-status": "rejected",
            "anthropic-ratelimit-unified-representative-claim": "five_hour",
            "anthropic-ratelimit-unified-5h-reset": str(int(NOW + 1800)),
        }
        [latch] = ql.latches_for_refusal(OPUS, _refusal(headers), [], now=NOW)
        self.assertEqual(latch.until, NOW + 1800)

    def test_a_past_overall_reset_does_not_shadow_a_future_window_reset(self) -> None:
        """R23 NIT-2."""
        headers = _claim("five_hour", NOW - 60, **{
            "anthropic-ratelimit-unified-5h-reset": str(int(NOW + 1800)),
        })
        [latch] = ql.latches_for_refusal(OPUS, _refusal(headers), [], now=NOW)
        self.assertEqual(latch.until, NOW + 1800)

    def test_a_claim_without_a_rejected_status_is_not_evidence(self) -> None:
        """R23 MEDIUM-1: the claim header alone (status allowed / absent)."""
        for status in ("allowed", "allowed_warning", None):
            headers = _claim("five_hour", NOW + 3600)
            if status is None:
                del headers["anthropic-ratelimit-unified-status"]
            else:
                headers["anthropic-ratelimit-unified-status"] = status
            with self.subTest(status=status):
                self.assertEqual(ql.latches_for_refusal(OPUS, _refusal(headers), [], now=NOW), [])

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
        # An Opus refusal is not explained by the Fable window: no evidence
        # at all for a plain rate limit, the family latch for an exhaustion.
        self.assertEqual(ql.latches_for_refusal(OPUS, _refusal(), windows, now=NOW), [])
        [opus] = ql.latches_for_refusal(OPUS, _exhausted(), windows, now=NOW)
        self.assertEqual(opus.limit, ql.LIMIT_UNKNOWN)

    def test_a_stale_window_is_not_evidence(self) -> None:
        windows = [_window("5h", 100.0, resets=NOW + 900, seen=NOW - uw.STALE_AFTER_S - 1)]
        self.assertEqual(ql.latches_for_refusal(OPUS, _refusal(), windows, now=NOW), [])

    def test_a_reset_hint_alone_latches_only_the_refused_family(self) -> None:
        [latch] = ql.latches_for_refusal(
            OPUS, _refusal({"retry-after": "1200"}, EXHAUSTED, ql.CLASS_EXHAUSTED), [], now=NOW,
        )
        self.assertEqual((latch.limit, latch.scope), (ql.LIMIT_UNKNOWN, "opus"))
        self.assertEqual(latch.source, ql.SOURCE_RESET_HINT)
        self.assertAlmostEqual(latch.until, NOW + 1200, delta=5)
        self.assertFalse(latch.covers(SONNET))

    def test_a_plain_rate_limited_refusal_never_latches(self) -> None:
        """R23 MEDIUM-1: classify_quota's "we do not know" verdict."""
        self.assertEqual(ql.latches_for_refusal(OPUS, _refusal(), [], now=NOW), [])
        self.assertEqual(
            ql.latches_for_refusal(OPUS, _refusal({"retry-after": "1200"}), [], now=NOW), [],
        )

    def test_an_exhausted_verdict_with_no_reset_gets_the_conservative_latch(self) -> None:
        [latch] = ql.latches_for_refusal(OPUS, _exhausted(), [], now=NOW)
        self.assertEqual(latch.until, NOW + ql.CONSERVATIVE_LATCH_S)
        self.assertEqual(latch.source, ql.SOURCE_CONSERVATIVE)
        self.assertEqual(latch.scope, "opus")
        # A rejected named limit with no end gets the same short latch.
        headers = _claim("five_hour", NOW)
        del headers["anthropic-ratelimit-unified-reset"]
        [named] = ql.latches_for_refusal(OPUS, _refusal(headers), [], now=NOW)
        self.assertEqual((named.limit, named.until), ("5h", NOW + ql.CONSERVATIVE_LATCH_S))

    def test_no_latch_outlives_the_cap(self) -> None:
        [latch] = ql.latches_for_refusal(
            OPUS, _refusal(_claim("seven_day", NOW + 90 * 86400)), [], now=NOW,
        )
        self.assertEqual(latch.until, NOW + ql.MAX_LATCH_S)

    def test_a_5h_latch_is_capped_at_5h_plus_margin(self) -> None:
        """R23 LOW-1: a bogus far reset on a five_hour claim."""
        [latch] = ql.latches_for_refusal(
            OPUS, _refusal(_claim("five_hour", NOW + 3 * 86400)), [], now=NOW,
        )
        self.assertEqual(latch.until, NOW + ql.MAX_5H_LATCH_S)
        self.assertLessEqual(ql.MAX_5H_LATCH_S, 5 * 3600 + 900)

    def test_a_scope_never_widens_to_every_claude_model(self) -> None:
        """R23 NIT-1."""
        self.assertEqual(ql.model_scope("claude-5"), "claude-5")
        [latch] = ql.latches_for_refusal("claude-5", _exhausted(), [], now=NOW)
        self.assertTrue(latch.covers("claude-5"))
        self.assertFalse(latch.covers(SONNET))


# ══════════════════════════════════════════════════════════════════════════
# The REAL usage-windows shape (live capture, 2026-10-18)
# ══════════════════════════════════════════════════════════════════════════
LIVE_WINDOWS = (
    Path(__file__).resolve().parent / "fixtures" / "model_router"
    / "anthropic-usage-windows-live-2026-10-18.json"
)


def _live_windows(**percent: float) -> tuple[list[Window], float]:
    """The captured Claude windows as the gateway holds them (``Window``),
    with ``percent`` overrides by window id; and the capture's own clock."""
    capture = json.loads(LIVE_WINDOWS.read_text(encoding="utf-8"))
    now = uw._reset_epoch(capture["generated_at"])
    assert now is not None
    windows = []
    for row in capture["vendor"]["windows"]:
        seen = uw._reset_epoch(row["fetched_at"])
        assert seen is not None
        windows.append(Window(
            row["id"], row["label"], percent.get(row["id"], row["percent"]),
            uw._reset_epoch(row["resets_at"]), row["source"], seen,
        ))
    return windows, now


class LiveWindowsTests(unittest.TestCase):

    def test_the_capture_carries_5h_weekly_and_a_per_model_window(self) -> None:
        windows, _now = _live_windows()
        self.assertEqual([w.id for w in windows], ["5h", "weekly", "weekly:Fable"])
        self.assertTrue(all(w.resets_at for w in windows))

    def test_the_live_case_18_94_percent_and_a_bare_429_does_not_latch(self) -> None:
        """R23 MEDIUM-1, the lane's own live capture: 5h 18 % / weekly 94 %
        and a bare 429 with no unified headers -> no latch."""
        windows, now = _live_windows()
        self.assertEqual(ql.latches_for_refusal(HAIKU, _refusal(), windows, now=now), [])
        [latch] = ql.latches_for_refusal(OPUS, _exhausted(), windows, now=now)
        self.assertEqual((latch.limit, latch.source), (ql.LIMIT_UNKNOWN, ql.SOURCE_CONSERVATIVE))

    def test_a_full_per_model_window_latches_only_its_model_until_its_reset(self) -> None:
        windows, now = _live_windows(**{"weekly:Fable": 100.0})
        [latch] = ql.latches_for_refusal(FABLE, _refusal(), windows, now=now)
        self.assertEqual((latch.limit, latch.scope), ("weekly:Fable", "fable"))
        self.assertEqual(latch.until, uw._reset_epoch("2026-10-20T05:00:00Z"))
        self.assertFalse(latch.covers(OPUS))

    def test_a_full_weekly_window_latches_every_model(self) -> None:
        windows, now = _live_windows(weekly=100.0)
        [latch] = ql.latches_for_refusal(HAIKU, _refusal(), windows, now=now)
        self.assertEqual((latch.limit, latch.scope, latch.source), ("weekly", "all", ql.SOURCE_USAGE_WINDOWS))
        self.assertTrue(latch.covers(SONNET))

    def test_a_later_real_reading_under_100_clears_an_unknown_latch(self) -> None:
        windows, now = _live_windows()
        clock = [now - 60]
        latch = ql.QuotaLatch(clock=lambda: clock[0])
        self.assertTrue(latch.note_refusal(OPUS, _exhausted(), []))
        clock[0] = now
        self.assertIsNone(latch.active_for(OPUS, windows))


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
        self.assertNotIn("names no limit", cm.output[0])
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
        self.latch.note_refusal(OPUS, _exhausted(), [])
        self.clock[0] = NOW + 30
        half = [_window("5h", 30.0, resets=NOW + 3600, seen=NOW + 20)]
        self.assertIsNotNone(self.latch.active_for(OPUS, half))
        both = half + [_window("weekly", 50.0, resets=NOW + 86400, seen=NOW + 20)]
        self.assertIsNone(self.latch.active_for(OPUS, both))

    def test_a_reading_whose_fetch_may_have_started_before_the_latch_is_ignored(self) -> None:
        """R23 NIT-3: stamped within the fetch timeout after the latch."""
        self.latch.note_refusal(OPUS, _refusal(_claim("five_hour", NOW + 3600)), [])
        self.clock[0] = NOW + 30
        early = [_window("5h", 10.0, resets=NOW + 3600, seen=NOW + uw.FETCH_TIMEOUT_S - 1)]
        self.assertIsNotNone(self.latch.active_for(OPUS, early))
        later = [_window("5h", 10.0, resets=NOW + 3600, seen=NOW + uw.FETCH_TIMEOUT_S + 1)]
        self.assertIsNone(self.latch.active_for(OPUS, later))

    def test_a_model_level_key_refusal_clears_only_that_models_latches(self) -> None:
        """R23 LOW-3."""
        self.latch.note_refusal(OPUS, _refusal(_claim("seven_day_opus", NOW + 86400)), [])
        self.latch.note_refusal(SONNET, _exhausted(), [])
        self.latch.key_refused(OPUS)
        self.assertIsNone(self.latch.active_for(OPUS))
        self.assertIsNotNone(self.latch.active_for(SONNET))
        # No hold-off: a later refusal arms again.
        self.assertTrue(
            self.latch.note_refusal(OPUS, _refusal(_claim("seven_day_opus", NOW + 86400)), []),
        )

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
        # A reading stamped past the recovery margin (its fetch started
        # after the latch).
        later = time.time() + ql.RECOVERY_MARGIN_S + 5
        gateway.usage_windows._clock = lambda: later
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

    # ── review R23 fix round ──────────────────────────────────────────────
    BALANCE = (
        b'{"type":"error","error":{"type":"invalid_request_error",'
        b'"message":"Your credit balance is too low"}}'
    )

    def ok(self, model: str = OPUS) -> bytes:
        return json.dumps(_success(model)).encode()

    def pre_arm(self, gateway: Any) -> None:
        self.assertTrue(gateway.quota_latch.note_refusal(
            OPUS, _refusal(_claim("five_hour", time.time() + 3600)), [],
        ))

    async def test_a_key_402_on_the_first_fallback_never_latches(self) -> None:
        """R23 HIGH-1 repro test_a + LOW-2: sub 429 (rejected 5h) -> key
        402. No latch; the client gets a 429 naming the key, never a 402."""
        client = await self.client_with(key=True, fallback=True)
        gateway = client.app[srv.APP_KEY]
        self.first.script = [
            (429, _claim("five_hour", time.time() + 3600), RATE_LIMITED),
            (402, {}, self.BALANCE),
        ]
        resp = await self.post(client, OPUS)
        self.assertEqual(resp.status, 429)
        body = await resp.json()
        self.assertEqual(body["error"]["type"], "rate_limit_error")
        self.assertIn("ANTHROPIC_API_KEY_GATEWAY", body["error"]["message"])
        self.assertIn("credit balance is too low", body["error"]["message"])
        self.assertNotIn(FAKE_API_KEY, json.dumps(body))
        self.assertEqual(self.auths(), ["sub", "key"])
        self.assertEqual(gateway.quota_latch.report(), [])

    async def test_a_latched_request_whose_key_fails_is_served_by_the_subscription(self) -> None:
        """R23 HIGH-1: key 402 / 429 / 5xx on the latched path -> the
        subscription serves THAT request, the latch clears, and none
        re-arms until a key 2xx."""
        for status in (402, 429, 500, 529):
            with self.subTest(status=status):
                client = await self.client_with(key=True, fallback=True)
                gateway = client.app[srv.APP_KEY]
                self.pre_arm(gateway)
                self.first.requests.clear()
                self.first.script = [(status, {}, self.BALANCE), (200, {}, self.ok())]
                resp = await self.post(client, OPUS)
                self.assertEqual(resp.status, 200)
                self.assertEqual(self.auths(), ["key", "sub"])
                self.assertEqual(gateway.quota_latch.report(), [])
                rows = await self.rows(client)
                self.assertEqual(rows[-1]["auth"], "oauth")
                await client.close()
                self.usage_ledger_path.unlink(missing_ok=True)

    async def test_after_a_key_failure_a_later_key_2xx_arms_again(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        gateway = client.app[srv.APP_KEY]
        self.pre_arm(gateway)
        self.first.script = [(402, {}, self.BALANCE), (200, {}, self.ok())]
        await self.post(client, OPUS)
        self.assertEqual(gateway.quota_latch.report(), [])
        self.refuse_then_serve(_claim("five_hour", time.time() + 3600))
        self.assertEqual((await self.post(client, OPUS)).status, 200)
        self.assertTrue(gateway.quota_latch.report())

    async def test_a_bare_rate_limit_429_falls_back_once_but_never_latches(self) -> None:
        """R23 MEDIUM-1 end to end (the lane's live case): OD-9 still sends
        THAT request to the key; the next one asks the subscription first."""
        client = await self.client_with(key=True, fallback=True)
        gateway = client.app[srv.APP_KEY]
        self.refuse_then_serve({})
        self.assertEqual((await self.post(client, OPUS)).status, 200)
        self.assertEqual(gateway.quota_latch.report(), [])
        await self.post(client, OPUS)
        self.assertEqual(self.auths(), ["sub", "key", "sub"])

    async def test_a_latch_lookup_defect_degrades_to_no_latch(self) -> None:
        """R23 MEDIUM-2."""
        client = await self.client_with(key=True, fallback=True)
        gateway = client.app[srv.APP_KEY]
        self.pre_arm(gateway)

        def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("latch defect")

        gateway.quota_latch.active_for = boom
        self.first.script = [(200, {}, self.ok())]
        with self.assertLogs("model_router.server", level="ERROR"):
            resp = await self.post(client, OPUS)
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.auths(), ["sub"])

    async def test_a_latch_arming_defect_still_serves_the_request(self) -> None:
        """R23 MEDIUM-2."""
        client = await self.client_with(key=True, fallback=True)
        gateway = client.app[srv.APP_KEY]

        def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("latch defect")

        gateway.quota_latch.note_refusal = boom
        self.refuse_then_serve(_claim("five_hour", time.time() + 3600))
        with self.assertLogs("model_router.server", level="ERROR"):
            resp = await self.post(client, OPUS)
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.auths(), ["sub", "key"])
        self.assertEqual(gateway.quota_latch.report(), [])

    async def test_a_per_model_key_403_clears_only_that_models_latch(self) -> None:
        """R23 LOW-3, end to end."""
        client = await self.client_with(key=True, fallback=True)
        gateway = client.app[srv.APP_KEY]
        gateway.quota_latch.note_refusal(
            OPUS, _refusal(_claim("seven_day_opus", time.time() + 86400)), [],
        )
        gateway.quota_latch.note_refusal(SONNET, _exhausted(), [])
        denied = (
            b'{"type":"error","error":{"type":"permission_error",'
            b'"message":"model: claude-opus-5-5 is not available to this workspace"}}'
        )
        self.first.script = [(403, {}, denied), (200, {}, self.ok())]
        resp = await self.post(client, OPUS)
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.auths(), ["key", "sub"])
        self.assertEqual([r["scope"] for r in gateway.quota_latch.report()], ["sonnet"])

    async def test_an_explicit_route_key_402_reaches_the_client_as_429(self) -> None:
        """R23 LOW-2 (pre-existing): the explicit API-key route too."""
        client = await self.client_with(key=True, fallback=False)
        self.first.script = [(402, {}, self.BALANCE)]
        resp = await self.post(client, f"{API_NS}{OPUS}")
        self.assertEqual(resp.status, 429)
        self.assertIn("out of credits", (await resp.json())["error"]["message"])

    async def test_without_a_latch_health_reports_an_empty_list(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        auth = (await (await client.get("/health")).json())["anthropic_auth"]
        self.assertEqual(auth["quota_latch"], [])


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
