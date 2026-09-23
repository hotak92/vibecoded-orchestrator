# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Subscription usage windows (``model_router.usage_windows``, ``/usage/windows``).

No network: every source is a fake ``fetch`` or a loopback stub upstream. The
payloads below are the SHAPES probed live on 2026-09-23 (values changed), plus
the older Z.ai schema, because both endpoints are undocumented and one of them
already changed once.

The decisions pinned here, each with its own test:

* unknown is never 0 % or 100 % — missing, unparseable, stale and past-reset
  windows all read ``percent: null`` with the reason beside them;
* a READ of the snapshot never waits on a vendor, and ``/v1/messages`` never
  triggers a vendor usage call at all;
* the passive unified headers are read off relayed first-party answers;
* no token or key ever reaches the snapshot, the rendered line or the log;
* the route requires the host token.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from aiohttp import web

from model_router import usage_windows as uw
from model_router.server import APP_KEY
from model_router.usage_windows import (
    LedgerMonthCounter,
    UsageWindows,
    month_start,
    parse_anthropic_usage,
    parse_unified_headers,
    parse_zai_quota,
    render_line,
)
from model_router.vendors import VENDORS, Vendor, validate_registry
from tests.test_model_router_server import (
    FAKE_OAUTH,
    FAKE_VENDOR_KEY,
    HOST_TOKEN,
    GatewayTestBase,
    _Upstream,
)

NOW = 1_790_000_000.0  # 2026-09-22T… UTC; any fixed instant works

#: The /api/oauth/usage shape, as probed (values changed).
ANTHROPIC_PAYLOAD: dict = {
    "five_hour": {"utilization": 31.0, "resets_at": "2026-09-23T04:40:00.103974+00:00"},
    "seven_day": {"utilization": 27.0, "resets_at": "2026-09-29T05:00:00.104002+00:00"},
    "seven_day_opus": None,
    "limits": [
        {"kind": "session", "group": "session", "percent": 31,
         "resets_at": "2026-09-23T04:40:00.103974+00:00", "scope": None},
        {"kind": "weekly_all", "group": "weekly", "percent": 27,
         "resets_at": "2026-09-29T05:00:00.104002+00:00", "scope": None},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 12,
         "resets_at": "2026-09-29T05:00:00.104274+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
    ],
}

#: The Z.ai monitor shape as probed 2026-09-23 (CREDIT_LIMIT generation).
ZAI_PAYLOAD_2026: dict = {
    "code": 200, "msg": "Operation successful", "success": True,
    "data": {
        "level": "pro",
        "limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "usage": 12000,
             "currentValue": 1290, "remaining": 10709, "percentage": 10,
             "nextResetTime": 1790151193672},
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "usage": 60000,
             "currentValue": 43467, "remaining": 16532, "percentage": 72,
             "nextResetTime": 1790267499983},
        ],
    },
}

#: The older Z.ai generation: a TOKENS_LIMIT 5h row, the monthly MCP-call
#: TIME_LIMIT row, and NO weekly row.
ZAI_PAYLOAD_OLD: dict = {
    "code": 200, "success": True,
    "data": {
        "limits": [
            {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "usage": 40000000,
             "currentValue": 10000000, "remaining": 30000000, "percentage": 25,
             "nextResetTime": 1790151193672},
            {"type": "TIME_LIMIT", "unit": 5, "number": 1, "usage": 1000,
             "currentValue": 900, "remaining": 100, "percentage": 90},
        ],
    },
}

def anthropic_payload_from(now: float) -> dict:
    """ANTHROPIC_PAYLOAD with resets relative to ``now`` — for the tests that
    run on the wall clock, so no fixture date can expire under them."""
    def at(seconds: float) -> str:
        return datetime.fromtimestamp(now + seconds, tz=timezone.utc).isoformat()

    payload = json.loads(json.dumps(ANTHROPIC_PAYLOAD))
    payload["five_hour"]["resets_at"] = at(3 * 3600)
    payload["seven_day"]["resets_at"] = at(5 * 86400)
    for item, offset in zip(payload["limits"], (3 * 3600, 5 * 86400, 5 * 86400)):
        item["resets_at"] = at(offset)
    return payload


SECRET_OAUTH = "sk-ant-oat01-SYNTHETIC-usage-windows-oauth"
SECRET_KEY = "zai-SYNTHETIC-usage-windows-key.abc123"


def _by_id(windows) -> dict:
    return {w.id: w for w in windows}


class AnthropicParsingTests(unittest.TestCase):
    def test_limits_array_with_scoped_model_window(self) -> None:
        windows = _by_id(parse_anthropic_usage(ANTHROPIC_PAYLOAD, now=NOW))
        self.assertEqual(windows["5h"].percent, 31.0)
        self.assertEqual(windows["weekly"].percent, 27.0)
        self.assertEqual(windows["weekly"].label, "wk")
        fable = windows["weekly:Fable"]
        self.assertEqual((fable.label, fable.percent), ("Fable", 12.0))
        self.assertAlmostEqual(
            fable.resets_at,
            datetime(2026, 9, 29, 5, 0, 0, 104274, tzinfo=timezone.utc).timestamp(),
        )
        self.assertEqual(fable.source, uw.SOURCE_OAUTH_USAGE)

    def test_legacy_blocks_used_when_limits_absent(self) -> None:
        payload = {k: v for k, v in ANTHROPIC_PAYLOAD.items() if k != "limits"}
        windows = _by_id(parse_anthropic_usage(payload, now=NOW))
        self.assertEqual(windows["5h"].percent, 31.0)
        self.assertEqual(windows["weekly"].percent, 27.0)
        self.assertNotIn("weekly:Fable", windows)

    def test_missing_weekly_is_unknown_never_zero(self) -> None:
        payload = {"five_hour": {"utilization": 5.0, "resets_at": None}}
        windows = _by_id(parse_anthropic_usage(payload, now=NOW))
        self.assertEqual(windows["5h"].percent, 5.0)
        self.assertIsNone(windows["weekly"].percent)
        self.assertEqual(windows["weekly"].unknown_reason, uw.UNKNOWN_NOT_REPORTED)

    def test_garbage_values_are_unknown(self) -> None:
        payload = {"limits": [
            {"kind": "session", "percent": "31"},
            {"kind": "weekly_all", "percent": True},
            {"kind": "weekly_scoped", "percent": 3, "scope": {"model": {"display_name": ""}}},
            "not-a-dict",
        ]}
        windows = _by_id(parse_anthropic_usage(payload, now=NOW))
        self.assertIsNone(windows["5h"].percent)
        self.assertIsNone(windows["weekly"].percent)
        self.assertEqual(set(windows), {"5h", "weekly"})
        self.assertEqual(
            {w.id for w in parse_anthropic_usage(None, now=NOW)}, {"5h", "weekly"},
        )


class UnifiedHeaderTests(unittest.TestCase):
    def test_fraction_becomes_percent_and_reset_is_epoch_seconds(self) -> None:
        windows = _by_id(parse_unified_headers({
            "anthropic-ratelimit-unified-5h-utilization": "0.25",
            "anthropic-ratelimit-unified-5h-reset": "1790001000",
            "anthropic-ratelimit-unified-7d-utilization": "0.6",
        }, now=NOW))
        self.assertEqual(windows["5h"].percent, 25.0)
        self.assertEqual(windows["5h"].resets_at, 1790001000.0)
        self.assertEqual(windows["weekly"].percent, 60.0)
        self.assertIsNone(windows["weekly"].resets_at)
        self.assertEqual(windows["5h"].source, uw.SOURCE_UNIFIED_HEADERS)

    def test_absent_or_garbage_headers_yield_nothing(self) -> None:
        self.assertEqual(parse_unified_headers({}, now=NOW), [])
        self.assertEqual(parse_unified_headers({
            "anthropic-ratelimit-unified-5h-utilization": "nan-ish",
            "anthropic-ratelimit-unified-7d-utilization": "-1",
        }, now=NOW), [])


class ZaiParsingTests(unittest.TestCase):
    def test_2026_credit_limit_schema(self) -> None:
        level, windows = parse_zai_quota(ZAI_PAYLOAD_2026, now=NOW)
        windows_by_id = _by_id(windows)
        self.assertEqual(level, "pro")
        self.assertEqual(windows_by_id["5h"].percent, 10.0)
        self.assertEqual(windows_by_id["weekly"].percent, 72.0)
        self.assertAlmostEqual(windows_by_id["5h"].resets_at, 1790151193.672)

    def test_older_tokens_limit_schema_weekly_is_unknown(self) -> None:
        level, windows = parse_zai_quota(ZAI_PAYLOAD_OLD, now=NOW)
        windows_by_id = _by_id(windows)
        self.assertIsNone(level)
        self.assertEqual(windows_by_id["5h"].percent, 25.0)
        # No weekly row: unknown, not 0 %, and the monthly MCP-call row is
        # NOT mistaken for it.
        self.assertIsNone(windows_by_id["weekly"].percent)
        self.assertEqual(windows_by_id["weekly"].unknown_reason, uw.UNKNOWN_NOT_REPORTED)

    def test_percentage_derived_from_current_value_when_missing(self) -> None:
        payload = json.loads(json.dumps(ZAI_PAYLOAD_2026))
        del payload["data"]["limits"][1]["percentage"]
        _, windows = parse_zai_quota(payload, now=NOW)
        self.assertEqual(_by_id(windows)["weekly"].percent, 72.4)

    def test_refused_answer_yields_no_numbers(self) -> None:
        for payload in (
            {"code": 401, "success": False, "msg": "token expired", "data": None},
            {"success": False, "data": ZAI_PAYLOAD_2026["data"]},
            "nonsense",
            None,
        ):
            _, windows = parse_zai_quota(payload, now=NOW)
            self.assertEqual([w.percent for w in windows], [None, None], payload)


class EffectiveAgeingTests(unittest.TestCase):
    def test_stale_and_past_reset_read_unknown(self) -> None:
        fresh = uw.Window("5h", "5h", 40.0, NOW + 3600, uw.SOURCE_ZAI_QUOTA, NOW)
        self.assertEqual(uw._effective(fresh, NOW + 60), (40.0, None))
        self.assertEqual(
            uw._effective(fresh, NOW + uw.STALE_AFTER_S + 1), (None, uw.UNKNOWN_STALE),
        )
        past = uw.Window("5h", "5h", 40.0, NOW + 10, uw.SOURCE_ZAI_QUOTA, NOW)
        self.assertEqual(uw._effective(past, NOW + 11), (None, uw.UNKNOWN_RESET_PASSED))


def _row(ts: str, route: str, **tokens: int) -> str:
    base = {"input_tokens": 0, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "output_tokens": 0}
    base.update(tokens)
    return json.dumps({"ts": ts, "route": route, **base}) + "\n"


class LedgerMonthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "gateway-usage.jsonl"
        # Mid-month, far from any boundary in any timezone.
        self.now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc).timestamp()

    def test_totals_this_month_per_route_incrementally(self) -> None:
        self.path.write_text(
            _row("2026-08-20T10:00:00Z", "vendor:qwen", input_tokens=999)
            + _row("2026-09-02T10:00:00Z", "vendor:qwen", input_tokens=100,
                   cache_read_input_tokens=50, output_tokens=10)
            + _row("2026-09-03T10:00:00Z", "anthropic", input_tokens=7),
            encoding="utf-8",
        )
        counter = LedgerMonthCounter()
        period, since, totals = counter.scan(self.path, self.now)
        self.assertEqual(period, month_start(self.now))
        self.assertIsNone(since)  # the file reaches back past the month start
        self.assertEqual(totals["vendor:qwen"], (160, 1))
        self.assertEqual(totals["anthropic"], (7, 1))
        # A torn line (no newline yet) is not consumed...
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(_row("2026-09-04T10:00:00Z", "vendor:qwen", output_tokens=40)[:-9])
        _, _, totals = counter.scan(self.path, self.now)
        self.assertEqual(totals["vendor:qwen"], (160, 1))
        # ...until it is complete, and only the new bytes are read.
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(_row("2026-09-04T10:00:00Z", "vendor:qwen", output_tokens=40)[-9:])
        _, _, totals = counter.scan(self.path, self.now)
        self.assertEqual(totals["vendor:qwen"], (200, 2))

    def test_rotation_rescans_and_coverage_is_labelled(self) -> None:
        self.path.write_text(
            _row("2026-09-02T10:00:00Z", "vendor:qwen", input_tokens=100) * 5,
            encoding="utf-8",
        )
        counter = LedgerMonthCounter()
        counter.scan(self.path, self.now)
        # Rotation keeps a SHORTER tail whose oldest row is mid-month.
        replacement = self.path.with_suffix(".tmp")
        replacement.write_text(
            _row("2026-09-10T10:00:00Z", "vendor:qwen", input_tokens=5), encoding="utf-8",
        )
        os.replace(replacement, self.path)
        _, since, totals = counter.scan(self.path, self.now)
        self.assertEqual(totals["vendor:qwen"], (5, 1))
        self.assertEqual(
            since, datetime(2026, 9, 10, 10, tzinfo=timezone.utc).timestamp(),
        )

    def test_missing_file_is_empty(self) -> None:
        _, since, totals = LedgerMonthCounter().scan(self.path, self.now)
        self.assertEqual((since, totals), (None, {}))


class _FakeFetch:
    """Records every call; answers from a per-URL table; can block."""

    def __init__(self, answers: Mapping[str, "tuple[Optional[int], Any]"]) -> None:
        self.answers = dict(answers)
        self.calls: list[tuple[str, dict]] = []
        self.gate: Optional[asyncio.Event] = None

    async def __call__(self, url: str, headers: Mapping[str, str]):
        self.calls.append((url, dict(headers)))
        if self.gate is not None:
            await self.gate.wait()
        for prefix, answer in self.answers.items():
            if url.startswith(prefix):
                return answer
        return None, None


def _vendors(quota_url: str = "https://quota.example/limit") -> dict:
    zai = VENDORS["zai"]
    qwen = VENDORS["qwen"]
    return {
        "zai": Vendor(**{**zai.__dict__, "quota_url": quota_url}),
        "qwen": qwen,
    }


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "gateway-usage.jsonl"
        self.clock = [datetime(2026, 9, 15, 12, tzinfo=timezone.utc).timestamp()]
        self.oauth: Optional[str] = SECRET_OAUTH
        self.key: Optional[str] = SECRET_KEY
        self.fetch = _FakeFetch({
            "https://anthropic.example/api/oauth/usage": (200, ANTHROPIC_PAYLOAD),
            "https://quota.example/limit": (200, ZAI_PAYLOAD_2026),
        })

    def make(self, **overrides) -> UsageWindows:
        async def vendor_key(_vendor: Vendor) -> Optional[str]:
            return self.key

        # Resets far in the future so only STALE_AFTER_S can age a reading.
        return UsageWindows(
            anthropic_upstream="https://anthropic.example/",
            vendors=overrides.pop("vendors", _vendors()),
            oauth_token=lambda: self.oauth,
            vendor_key=vendor_key,
            fetch=overrides.pop("fetch", self.fetch),
            ledger_path=lambda: self.ledger,
            clock=lambda: self.clock[0],
            rng=lambda: 0.5,
            **overrides,
        )

    def _write_ledger(self) -> None:
        self.ledger.write_text(
            _row("2026-09-02T10:00:00Z", "vendor:qwen", input_tokens=1_000_000,
                 output_tokens=234_567)
            + _row("2026-09-02T11:00:00Z", "vendor:zai", input_tokens=5),
            encoding="utf-8",
        )

    async def test_full_refresh_renders_the_documented_line(self) -> None:
        self._write_ledger()
        service = self.make()
        self.clock[0] = datetime(2026, 9, 23, 3, tzinfo=timezone.utc).timestamp()
        await service.refresh()
        snap = service.snapshot()
        self.assertEqual(
            render_line(snap),
            "Claude 5h 31% · wk 27% · Fable 12% │ GLM 5h 10% · wk 72% │ Qwen 1.2M tok/mo",
        )
        vendors = {v["id"]: v for v in snap["vendors"]}
        self.assertEqual(vendors["zai"]["plan"], "pro")
        qwen_tokens = vendors["qwen"]["tokens"]
        self.assertEqual(qwen_tokens["unit"], "tokens")
        self.assertEqual(qwen_tokens["tokens"], 1_234_567)
        self.assertEqual(qwen_tokens["source"], uw.SOURCE_LEDGER)
        self.assertNotIn("percent", qwen_tokens)  # never an invented %
        for vendor in vendors.values():
            for window in vendor["windows"]:
                self.assertIn("fetched_at", window)
                self.assertIn("source", window)

    async def test_request_headers_carry_the_credentials_correctly(self) -> None:
        await self.make().refresh()
        by_url = {url: headers for url, headers in self.fetch.calls}
        anthropic = by_url["https://anthropic.example/api/oauth/usage"]
        self.assertEqual(anthropic["Authorization"], f"Bearer {SECRET_OAUTH}")
        self.assertIn("oauth-2025-04-20", anthropic["anthropic-beta"])
        # Z.ai's monitor endpoint takes the RAW key, no scheme.
        self.assertEqual(by_url["https://quota.example/limit"]["Authorization"], SECRET_KEY)
        # A vendor with no quota_url is never fetched.
        self.assertEqual(len(self.fetch.calls), 2)

    async def test_failure_is_unknown_never_zero(self) -> None:
        fetch = _FakeFetch({"https://anthropic.example": (500, None),
                            "https://quota.example": (None, None)})
        service = self.make(fetch=fetch)
        await service.refresh()
        snap = service.snapshot()
        for vendor in snap["vendors"]:
            if vendor["id"] == "qwen":
                continue
            self.assertEqual(vendor["state"], uw.STATE_UNKNOWN, vendor)
            self.assertEqual(vendor["windows"], [])
        self.assertEqual(render_line(snap), "")

    async def test_unreadable_shape_is_unknown(self) -> None:
        fetch = _FakeFetch({"https://anthropic.example": (200, {"totally": "new"}),
                            "https://quota.example": (200, {"code": 200, "data": {"limits": []}})})
        service = self.make(fetch=fetch)
        await service.refresh()
        states = {v["id"]: v["state"] for v in service.snapshot()["vendors"]}
        self.assertEqual(states["anthropic"], uw.STATE_UNKNOWN)
        self.assertEqual(states["zai"], uw.STATE_UNKNOWN)

    async def test_previous_reading_survives_a_failure_then_ages_out(self) -> None:
        service = self.make()
        await service.refresh()
        self.fetch.answers = {"https://anthropic.example": (503, None),
                              "https://quota.example": (503, None)}
        self.clock[0] += 300
        await service.refresh()
        snap = service.snapshot()
        claude = snap["vendors"][0]
        zai = next(v for v in snap["vendors"] if v["id"] == "zai")
        self.assertEqual(zai["windows"][0]["percent"], 10.0)  # kept, not blanked
        self.assertIn("HTTP 503", zai["problem"])
        self.assertIn("HTTP 503", claude["problem"])
        self.clock[0] += uw.STALE_AFTER_S + 1
        zai = next(v for v in service.snapshot()["vendors"] if v["id"] == "zai")
        self.assertEqual({w["percent"] for w in zai["windows"]}, {None})
        self.assertEqual({w["unknown_reason"] for w in zai["windows"]}, {uw.UNKNOWN_STALE})

    async def test_no_credentials_reads_unconfigured(self) -> None:
        self.oauth = None
        self.key = None
        service = self.make()
        await service.refresh()
        states = {v["id"]: v["state"] for v in service.snapshot()["vendors"]}
        self.assertEqual(states["anthropic"], uw.STATE_UNCONFIGURED)
        self.assertEqual(states["zai"], uw.STATE_UNCONFIGURED)
        self.assertEqual(self.fetch.calls, [])

    async def test_newer_headers_win_and_alone_make_claude_known(self) -> None:
        service = self.make()
        service.observe_anthropic_headers({
            "anthropic-ratelimit-unified-5h-utilization": "0.4",
            "anthropic-ratelimit-unified-5h-reset": str(int(self.clock[0] + 3600)),
        })
        claude = service.snapshot()["vendors"][0]
        self.assertEqual(claude["state"], uw.STATE_OK)
        self.assertEqual(claude["windows"][0]["percent"], 40.0)
        self.assertEqual(claude["windows"][0]["source"], uw.SOURCE_UNIFIED_HEADERS)
        # An endpoint reading taken LATER wins over the older header.
        self.clock[0] = datetime(2026, 9, 23, 3, tzinfo=timezone.utc).timestamp()
        await service.refresh()
        self.assertEqual(service.snapshot()["vendors"][0]["windows"][0]["source"],
                         uw.SOURCE_OAUTH_USAGE)
        # And a header observed after that wins again.
        self.clock[0] += 5
        service.observe_anthropic_headers({"anthropic-ratelimit-unified-5h-utilization": "0.5"})
        first = service.snapshot()["vendors"][0]["windows"][0]
        self.assertEqual((first["percent"], first["source"]), (50.0, uw.SOURCE_UNIFIED_HEADERS))

    async def test_request_refresh_is_throttled_jittered_and_deduplicated(self) -> None:
        self.fetch.gate = asyncio.Event()
        service = self.make(refresh_s=100.0, jitter_s=40.0)
        self.assertTrue(service.request_refresh())
        self.assertFalse(service.request_refresh())  # in flight
        self.assertTrue(service.snapshot()["refreshing"])
        self.fetch.gate.set()
        await asyncio.sleep(0)
        while service.refreshing:
            await asyncio.sleep(0.01)
        self.clock[0] += 119  # 100 + 0.5*40 = 120 s until due
        self.assertFalse(service.request_refresh())
        self.clock[0] += 1
        self.assertTrue(service.request_refresh())
        await service.stop()
        self.assertFalse(service.refreshing)

    # ── warm while chats flow (owner decision 2026-09-23) ────────────────
    async def _settle(self, service: UsageWindows) -> None:
        await asyncio.sleep(0)
        while service.refreshing:
            await asyncio.sleep(0.001)

    async def test_activity_schedules_a_refresh_without_awaiting_it(self) -> None:
        service = self.make()
        # Synchronous and O(1): a plain call, not a coroutine, and the
        # vendors have not been asked by the time it returns.
        self.assertTrue(service.note_activity())
        self.assertTrue(service.refreshing)
        self.assertEqual(self.fetch.calls, [])
        await self._settle(service)
        self.assertEqual(len(self.fetch.calls), 2)  # Claude + Z.ai, once each
        snap = service.snapshot()
        self.assertIsNotNone(snap["last_activity_at"])
        self.assertIsNotNone(snap["last_refresh_at"])

    async def test_idle_gateway_makes_no_vendor_calls(self) -> None:
        # A short real interval, so a timer — if anyone ever adds one —
        # would have fired many times during the sleep below.
        service = self.make(refresh_s=0.01, jitter_s=0.0)
        self.assertTrue(service.note_activity())
        await self._settle(service)
        after_traffic = len(self.fetch.calls)
        for _ in range(20):  # hours of simulated idle time
            self.clock[0] += 3600
            await asyncio.sleep(0.01)
        self.assertEqual(len(self.fetch.calls), after_traffic)
        self.assertFalse(service.refreshing)

    async def test_steady_traffic_refreshes_once_per_interval(self) -> None:
        # 240 s + 0.5 * 60 s of jitter = due every 270 s. A request every 10 s
        # for 20 minutes is 121 signals and must be 5 refreshes (t = 0, 270,
        # 540, 810, 1080), not 121.
        service = self.make(refresh_s=240.0, jitter_s=60.0)
        scheduled = 0
        for _ in range(121):
            scheduled += service.note_activity()
            await self._settle(service)
            self.clock[0] += 10
        self.assertEqual(scheduled, 5)
        self.assertEqual(len(self.fetch.calls), 10)

    async def test_a_burst_during_a_hung_refresh_stays_single_flight(self) -> None:
        self.fetch.gate = asyncio.Event()  # the refresh hangs
        service = self.make(refresh_s=1.0, jitter_s=0.0)
        self.assertTrue(service.note_activity())
        await asyncio.sleep(0)
        for _ in range(500):
            self.clock[0] += 60  # long past due, still one in flight
            self.assertFalse(service.note_activity())
            self.assertFalse(service.request_refresh())
        self.assertEqual(len(self.fetch.calls), 1)  # stuck on the first fetch
        self.fetch.gate.set()
        await self._settle(service)
        self.assertEqual(len(self.fetch.calls), 2)
        await service.stop()

    async def test_secrets_never_in_snapshot_line_or_log(self) -> None:
        self._write_ledger()
        with self.assertLogs("model_router", level=logging.DEBUG) as captured:
            logging.getLogger("model_router").debug("anchor")
            for answers in (
                {"https://anthropic.example": (200, ANTHROPIC_PAYLOAD),
                 "https://quota.example": (200, ZAI_PAYLOAD_2026)},
                {"https://anthropic.example": (401, None),
                 "https://quota.example": (403, None)},
                {"https://anthropic.example": (200, {"weird": SECRET_OAUTH[:4]}),
                 "https://quota.example": (None, None)},
            ):
                service = self.make(fetch=_FakeFetch(answers))
                await service.refresh()
                blob = json.dumps(service.snapshot()) + render_line(service.snapshot())
                for secret in (SECRET_OAUTH, SECRET_KEY):
                    self.assertNotIn(secret, blob)
        logged = "\n".join(captured.output)
        for secret in (SECRET_OAUTH, SECRET_KEY):
            self.assertNotIn(secret, logged)


class RegistryTests(unittest.TestCase):
    def test_shipped_rows(self) -> None:
        self.assertEqual(VENDORS["zai"].quota_url,
                         "https://api.z.ai/api/monitor/usage/quota/limit")
        self.assertIsNone(VENDORS["qwen"].quota_url)
        self.assertEqual((VENDORS["zai"].short_name, VENDORS["qwen"].short_name),
                         ("GLM", "Qwen"))

    def test_relative_quota_url_refused(self) -> None:
        bad = Vendor(**{**VENDORS["zai"].__dict__, "quota_url": "/api/monitor"})
        with self.assertRaises(ValueError):
            validate_registry({"zai": bad})


class _HeaderUpstream(_Upstream):
    """A first-party stub whose answers carry the unified headers."""

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        await self._record(request)
        return web.json_response(
            {"id": "msg_1", "model": "claude-x", "content": [],
             "usage": {"input_tokens": 3, "output_tokens": 1}},
            headers={
                "anthropic-ratelimit-unified-5h-utilization": "0.33",
                "anthropic-ratelimit-unified-5h-reset": str(int(time.time()) + 3600),
                "anthropic-ratelimit-unified-7d-utilization": "0.21",
                "anthropic-ratelimit-unified-7d-reset": str(int(time.time()) + 86400),
            },
        )

    def app(self) -> web.Application:
        app = super().app()
        app.router.add_get("/api/oauth/usage", self._oauth_usage)
        return app

    async def _oauth_usage(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.json_response(anthropic_payload_from(time.time()))


class RouteTests(GatewayTestBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # Swap BOTH stubs for ones that send the unified headers: the
        # first-party one so the tap has something to read, the vendor one so
        # a test can prove the tap ignores a vendor answer that carries them.
        await self.anthropic_up.stop()
        await self.vendor_up.stop()
        self.anthropic_up = _HeaderUpstream()
        self.vendor_up = _HeaderUpstream()
        url = await self.anthropic_up.start()
        vendor_url = await self.vendor_up.start()
        self.addAsyncCleanup(self.anthropic_up.stop)
        self.addAsyncCleanup(self.vendor_up.stop)
        self.anthropic = type(self.anthropic)(**{**self.anthropic.__dict__, "upstream": url})
        self.vendor = Vendor(**{**self.vendor.__dict__, "upstream": vendor_url})
        self.client = await self.make_client()
        self.gateway = self.client.server.app[APP_KEY]

    async def test_route_requires_the_host_token(self) -> None:
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            resp = await self.client.get("/usage/windows", headers=headers)
            self.assertEqual(resp.status, 401)
        resp = await self.client.get("/usage/windows?format=line")
        self.assertEqual(resp.status, 401)

    async def test_json_and_line_answers(self) -> None:
        resp = await self.client.get("/usage/windows", headers=self.auth())
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual([v["id"] for v in body["vendors"]], ["anthropic", "zai"])
        # The first read scheduled a refresh; once it lands the line is real.
        while self.gateway.usage_windows.refreshing:
            await asyncio.sleep(0.01)
        resp = await self.client.get("/usage/windows/?format=line", headers=self.auth())
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.content_type.startswith("text/plain"))
        text = await resp.text()
        self.assertTrue(text.startswith("Claude "), text)
        self.assertIn("Fable 12%", text)
        for secret in (FAKE_OAUTH, FAKE_VENDOR_KEY, HOST_TOKEN):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, json.dumps(body))

    async def test_read_never_waits_on_a_vendor(self) -> None:
        blocked = _FakeFetch({})
        blocked.gate = asyncio.Event()  # never set: the fetch hangs forever
        self.gateway.usage_windows._fetch = blocked
        started = time.monotonic()
        resp = await asyncio.wait_for(
            self.client.get("/usage/windows", headers=self.auth()), timeout=5,
        )
        self.assertEqual(resp.status, 200)
        self.assertLess(time.monotonic() - started, 2.0)
        body = await resp.json()
        self.assertTrue(body["refreshing"])
        self.assertEqual(body["vendors"][0]["state"], uw.STATE_PENDING)
        await self.gateway.usage_windows.stop()  # the hung task is cancelled

    async def _chat(self, model: str = "claude-x"):
        return await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "Content-Type": "application/json"},
            json={"model": model, "max_tokens": 5,
                  "messages": [{"role": "user", "content": "hi"}]},
        )

    async def test_chat_traffic_schedules_one_refresh_and_headers_are_captured(self) -> None:
        recorder = _FakeFetch({})
        recorder.gate = asyncio.Event()  # the refresh hangs until released
        self.gateway.usage_windows._fetch = recorder
        for _ in range(3):
            resp = await self._chat()
            self.assertEqual(resp.status, 200)
            await resp.read()
        # Warm while chats flow: the traffic scheduled a refresh, ONE, which
        # is still in flight — the three answers above did not wait for it.
        self.assertTrue(self.gateway.usage_windows.refreshing)
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(len(self.anthropic_up.message_requests), 3)
        snap = self.gateway.usage_windows.snapshot()
        self.assertIsNotNone(snap["last_activity_at"])
        recorder.gate.set()
        claude = snap["vendors"][0]
        by_id = {w["id"]: w for w in claude["windows"]}
        self.assertEqual(by_id["5h"]["percent"], 33.0)
        self.assertEqual(by_id["weekly"]["percent"], 21.0)
        self.assertEqual(by_id["5h"]["source"], uw.SOURCE_UNIFIED_HEADERS)

    async def test_vendor_answers_are_not_read_as_claude_windows(self) -> None:
        # The vendor stub sends the SAME header names; they must not feed
        # Claude's windows — the tap is first-party only. The activity-driven
        # refresh is held so only the tap could fill the windows.
        held = _FakeFetch({})
        held.gate = asyncio.Event()
        self.gateway.usage_windows._fetch = held
        resp = await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "Content-Type": "application/json"},
            json={"model": "claude-gw/glm-5.3", "max_tokens": 5,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        await resp.read()
        self.assertEqual(self.gateway.usage_windows.snapshot()["vendors"][0]["windows"], [])

    async def test_a_hanging_refresh_never_delays_a_chat(self) -> None:
        hung = _FakeFetch({})
        hung.gate = asyncio.Event()  # never released
        self.gateway.usage_windows._fetch = hung
        for _ in range(3):
            started = time.monotonic()
            resp = await asyncio.wait_for(self._chat(), timeout=5)
            body = await resp.json()
            self.assertEqual(resp.status, 200)
            self.assertEqual(body["id"], "msg_1")
            self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(len(hung.calls), 1)  # still the first, still hung
        await self.gateway.usage_windows.stop()

    async def test_a_failing_refresh_or_signal_never_fails_a_chat(self) -> None:
        async def exploding(url: str, headers: Mapping[str, str]):
            raise RuntimeError("usage source blew up")

        self.gateway.usage_windows._fetch = exploding
        with self.assertLogs("model_router", level=logging.ERROR):
            resp = await self._chat()
            self.assertEqual(resp.status, 200)
            await resp.read()
            while self.gateway.usage_windows.refreshing:
                await asyncio.sleep(0.01)

        def broken_signal() -> bool:
            raise RuntimeError("defect in the activity signal")

        self.gateway.usage_windows.note_activity = broken_signal  # type: ignore[method-assign]
        with self.assertLogs("model_router", level=logging.ERROR):
            resp = await self._chat()
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["id"], "msg_1")

    async def test_stop_cancels_an_inflight_refresh(self) -> None:
        blocked = _FakeFetch({})
        blocked.gate = asyncio.Event()
        self.gateway.usage_windows._fetch = blocked
        self.assertTrue(self.gateway.usage_windows.request_refresh())
        await asyncio.sleep(0.05)
        await self.gateway.stop()
        self.assertFalse(self.gateway.usage_windows.refreshing)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
