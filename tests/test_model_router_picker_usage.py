# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Subscription usage as TEXT in the ``/model`` picker labels.

``/v1/models`` appends a vendor subscription's usage to that vendor's rows,
e.g. ``glm-5.3 · Z.ai subscription · 1M ctx · 5h 10% · wk 72% used``. The
decisions pinned here, each with its own test:

* the text says USED, shortest window first, and omits whatever is unknown —
  a vendor with nothing known gets no suffix, never ``0%``;
* a reading older than one refresh interval carries ``(as of HH:MM)`` — a
  clock time, because the client freezes the label for the whole session;
* ``/v1/models`` reads the usage CACHE only: a hung vendor quota endpoint
  cannot delay the picker, and a cold cache answers without the text;
* only ``display_name`` changes — every ``id`` is byte-identical with the
  feature on and off;
* first-party rows are never decorated (Claude Code drops them in favour of
  its own built-in rows — see ``catalog.with_usage_labels``);
* ``VCT_MODEL_GATEWAY_PICKER_USAGE=off`` gives clean labels and schedules no
  usage refresh, and ``/health`` reports the mode in force.

No network: the vendor quota and Claude usage sources are a fake ``fetch``;
the catalogs are the loopback stubs of ``tests/test_model_router_server.py``.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest import mock

from model_router import config as gw_config
from model_router import usage_windows as uw
from model_router.catalog import CatalogEntry, with_usage_labels
from model_router.server import APP_KEY
from model_router.usage_windows import UsageWindows, label_suffix, label_suffixes
from model_router.vendors import Vendor
from tests.test_model_router_server import GatewayTestBase
from tests.test_model_router_usage_windows import (
    ZAI_PAYLOAD_2026,
    _FakeFetch,
    _row,
    _vendors,
    anthropic_payload_from,
)

T0 = datetime(2026, 9, 15, 12, 0, 30, tzinfo=timezone.utc).timestamp()
QUOTA_URL = "https://quota.example/limit"


def zai_payload_from(now: float) -> dict:
    """ZAI_PAYLOAD_2026 with resets relative to ``now``: the route tests run
    on the wall clock, and a fixed reset in the past reads — correctly — as
    unknown, which would drop the 5h window from every label."""
    payload = json.loads(json.dumps(ZAI_PAYLOAD_2026))
    for item, offset in zip(payload["data"]["limits"], (3 * 3600, 5 * 86400)):
        item["nextResetTime"] = int((now + offset) * 1000)
    return payload


def _window(window_id: str, label: str, percent: Optional[float], *, at: float = T0) -> dict:
    return {"id": window_id, "label": label, "kind": "percent", "percent": percent,
            "resets_at": None, "source": uw.SOURCE_ZAI_QUOTA,
            "fetched_at": uw._iso(at), "unknown_reason": None if percent is not None else "stale"}


def _tokens(count: int, *, at: float = T0, since: Optional[float] = None) -> dict:
    return {"tokens": count, "requests": 3, "unit": "tokens", "period": "month",
            "period_start": uw._iso(T0 - 14 * 86400),
            "counted_since": uw._iso(since) if since else None,
            "source": uw.SOURCE_LEDGER, "fetched_at": uw._iso(at)}


class LabelSuffixTests(unittest.TestCase):
    """The pure renderer, over snapshot-shaped vendor entries."""

    def suffix(self, vendor: dict, *, now: float = T0 + 5) -> str:
        return label_suffix(vendor, now=now, fresh_for_s=uw.DEFAULT_REFRESH_S)

    def test_known_windows_read_as_used_shortest_first(self) -> None:
        # Given weekly FIRST: the label still leads with the 5h window.
        vendor = {"windows": [_window("weekly", "wk", 72.0), _window("5h", "5h", 10.0)]}
        self.assertEqual(self.suffix(vendor), " · 5h 10% · wk 72% used")

    def test_an_unknown_window_is_left_out_never_zero(self) -> None:
        vendor = {"windows": [_window("5h", "5h", 10.0), _window("weekly", "wk", None)]}
        self.assertEqual(self.suffix(vendor), " · 5h 10% used")

    def test_nothing_known_means_no_suffix_at_all(self) -> None:
        for vendor in (
            {"windows": []},
            {"windows": [_window("5h", "5h", None), _window("weekly", "wk", None)]},
            {},
        ):
            self.assertEqual(self.suffix(vendor), "", vendor)

    def test_zero_percent_that_is_KNOWN_is_shown(self) -> None:
        # Unknown is absence; a real 0 % is a reading and is printed as one.
        vendor = {"windows": [_window("5h", "5h", 0.0)]}
        self.assertEqual(self.suffix(vendor), " · 5h 0% used")

    def test_a_reading_older_than_the_refresh_interval_names_its_clock_time(self) -> None:
        vendor = {"windows": [_window("5h", "5h", 10.0), _window("weekly", "wk", 72.0)]}
        fresh = self.suffix(vendor, now=T0 + uw.DEFAULT_REFRESH_S - 1)
        self.assertNotIn("as of", fresh)
        aged = self.suffix(vendor, now=T0 + uw.DEFAULT_REFRESH_S + 60)
        clock = time.strftime("%H:%M", time.localtime(T0))
        self.assertEqual(aged, f" · 5h 10% · wk 72% used (as of {clock})")

    def test_the_OLDEST_shown_reading_dates_the_label(self) -> None:
        old = T0 - 600
        vendor = {"windows": [_window("5h", "5h", 10.0), _window("weekly", "wk", 72.0, at=old)]}
        clock = time.strftime("%H:%M", time.localtime(old))
        self.assertTrue(self.suffix(vendor).endswith(f"(as of {clock})"))

    def test_tokens_for_a_vendor_with_no_quota_source(self) -> None:
        self.assertEqual(
            self.suffix({"windows": [], "tokens": _tokens(1_234_567)}),
            " · 1.2M tokens used this month",
        )

    def test_partial_month_coverage_names_its_span(self) -> None:
        since = datetime(2026, 9, 2, 10, tzinfo=timezone.utc).timestamp()
        moment = time.localtime(since)
        day = f"{time.strftime('%b', moment)} {moment.tm_mday}"
        self.assertEqual(
            self.suffix({"windows": [], "tokens": _tokens(1_234_567, since=since)}),
            f" · 1.2M tokens used since {day}",
        )

    def test_zero_or_stale_tokens_are_omitted(self) -> None:
        self.assertEqual(self.suffix({"tokens": _tokens(0)}), "")
        stale = {"tokens": _tokens(5_000, at=T0 - uw.STALE_AFTER_S - 60)}
        self.assertEqual(self.suffix(stale), "")

    def test_label_suffixes_ages_against_the_snapshot_itself(self) -> None:
        snapshot = {
            "generated_at": uw._iso(T0 + 5), "refresh_interval_s": 240,
            "vendors": [
                {"id": "zai", "windows": [_window("5h", "5h", 10.0)]},
                {"id": "qwen", "windows": [], "tokens": _tokens(950_000)},
                {"id": "empty", "windows": [_window("5h", "5h", None)]},
            ],
        }
        self.assertEqual(
            label_suffixes(snapshot),
            {"zai": " · 5h 10% used", "qwen": " · 950K tokens used this month"},
        )
        self.assertEqual(label_suffixes({"vendors": []}), {})


class ServiceSnapshotTests(unittest.IsolatedAsyncioTestCase):
    """End to end over a real ``UsageWindows``: the snapshot's own ageing
    feeds the label, so "stale ⇒ unknown ⇒ omitted" is proven, not assumed."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "gateway-usage.jsonl"
        self.ledger.write_text(
            _row("2026-08-30T10:00:00Z", "vendor:zai", input_tokens=1)
            + _row("2026-09-02T10:00:00Z", "vendor:qwen", input_tokens=1_000_000,
                   output_tokens=234_567),
            encoding="utf-8",
        )
        self.clock = [T0]
        self.fetch = _FakeFetch({
            "https://anthropic.example/api/oauth/usage": (200, anthropic_payload_from(T0)),
            QUOTA_URL: (200, ZAI_PAYLOAD_2026),
        })

        async def vendor_key(_vendor: Vendor) -> Optional[str]:
            return "zai-SYNTHETIC-key"

        self.service = UsageWindows(
            anthropic_upstream="https://anthropic.example/",
            vendors=_vendors(QUOTA_URL),
            oauth_token=lambda: "sk-ant-oat01-SYNTHETIC",
            vendor_key=vendor_key,
            fetch=self.fetch,
            ledger_path=lambda: self.ledger,
            clock=lambda: self.clock[0],
            rng=lambda: 0.5,
        )

    async def test_labels_track_the_snapshot_from_fresh_to_stale_to_gone(self) -> None:
        await self.service.refresh()
        fresh = label_suffixes(self.service.snapshot())
        self.assertEqual(fresh["zai"], " · 5h 10% · wk 72% used")
        # The ledger holds an August row, so the month is fully covered.
        self.assertEqual(fresh["qwen"], " · 1.2M tokens used this month")

        self.clock[0] = T0 + 600  # past the refresh interval, inside STALE_AFTER_S
        clock = time.strftime("%H:%M", time.localtime(T0))
        self.assertEqual(
            label_suffixes(self.service.snapshot())["zai"],
            f" · 5h 10% · wk 72% used (as of {clock})",
        )

        self.clock[0] = T0 + uw.STALE_AFTER_S + 60  # the snapshot now reads null
        self.assertNotIn("zai", label_suffixes(self.service.snapshot()))


class WithUsageLabelsTests(unittest.TestCase):
    def test_only_vendor_display_names_change(self) -> None:
        entries = [
            CatalogEntry(id="claude-opus-5-5[1m]", display_name="Claude Opus 5.5 (1M context)"),
            CatalogEntry(id="claude-gw/glm-5.3[1m]",
                         display_name="glm-5.3 · Z.ai subscription · 1M ctx", vendor_id="zai"),
            CatalogEntry(id="claude-gw/qwen/qwen3.8-max",
                         display_name="qwen3.8-max · QwenCloud Token Plan", vendor_id="qwen"),
        ]
        out = with_usage_labels(
            entries, {"anthropic": " · 5h 31% used", "zai": " · 5h 10% used"},
        )
        self.assertEqual([e.id for e in out], [e.id for e in entries])
        self.assertEqual(out[0].display_name, "Claude Opus 5.5 (1M context)")
        self.assertEqual(out[1].display_name, "glm-5.3 · Z.ai subscription · 1M ctx · 5h 10% used")
        self.assertIs(out[2], entries[2])  # no suffix known: untouched


class PickerRouteTests(GatewayTestBase):
    """``/v1/models`` through the real app, stubs for both catalogs."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.vendor = Vendor(**{**self.vendor.__dict__, "quota_url": QUOTA_URL})
        self.anthropic_up.models_payload = {
            "data": [{"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5",
                      "max_input_tokens": 1_000_000}],
        }
        self.vendor_up.models_payload = {"data": [{"id": "glm-5.3"}]}
        self.client = await self.make_client()
        self.gateway = self.client.app[APP_KEY]
        self.fetch = _FakeFetch({
            f"{self.anthropic.upstream}/api/oauth/usage": (200, anthropic_payload_from(time.time())),
            QUOTA_URL: (200, zai_payload_from(time.time())),
        })
        self.gateway.usage_windows._fetch = self.fetch
        self.addAsyncCleanup(self.gateway.usage_windows.stop)

    async def models(self, client=None) -> dict:
        # Bounded: a regression that made the route wait on the vendor must
        # FAIL here, not hang the suite.
        resp = await asyncio.wait_for(
            (client or self.client).get("/v1/models", headers=self.auth()), timeout=5,
        )
        self.assertEqual(resp.status, 200)
        return await resp.json()

    async def settle(self) -> None:
        while self.gateway.usage_windows.refreshing:
            await asyncio.sleep(0.01)

    def names(self, body: dict) -> dict:
        return {row["id"]: row["display_name"] for row in body["data"]}

    async def test_warm_cache_decorates_vendor_rows_only(self) -> None:
        await self.gateway.usage_windows.refresh()
        names = self.names(await self.models())
        self.assertEqual(
            names["claude-gw/glm-5.3[1m]"],
            "glm-5.3 · Z.ai subscription · 1M ctx · 5h 10% · wk 72% used",
        )
        # Claude usage IS known here (the fake answered) and still reaches no
        # first-party row: the client would discard it.
        claude = self.gateway.usage_windows.snapshot()["vendors"][0]
        self.assertTrue(any(w["percent"] is not None for w in claude["windows"]))
        self.assertEqual(names["claude-opus-5-5[1m]"], "Claude Opus 5.5 (1M context)")

    async def test_cold_cache_answers_without_text_and_schedules_a_refresh(self) -> None:
        self.fetch.gate = asyncio.Event()  # hold the refresh until we look
        first = self.names(await self.models())
        self.assertEqual(first["claude-gw/glm-5.3[1m]"], "glm-5.3 · Z.ai subscription · 1M ctx")
        self.assertTrue(self.gateway.usage_windows.refreshing)
        self.fetch.gate.set()
        await self.settle()
        second = self.names(await self.models())
        self.assertTrue(second["claude-gw/glm-5.3[1m]"].endswith(" · 5h 10% · wk 72% used"))

    async def test_models_never_waits_on_a_hung_vendor(self) -> None:
        self.fetch.gate = asyncio.Event()  # never set: the quota fetch hangs forever
        started = time.monotonic()
        resp = await asyncio.wait_for(
            self.client.get("/v1/models", headers=self.auth()), timeout=5,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(resp.status, 200)
        self.assertLess(elapsed, 2.0, f"/v1/models took {elapsed:.2f}s")
        self.assertTrue(self.gateway.usage_windows.refreshing)
        self.assertEqual(len(self.fetch.calls), 1)  # it started, and it is still hung

    async def test_ids_are_identical_with_the_feature_on_and_off(self) -> None:
        await self.gateway.usage_windows.refresh()
        on = await self.models()
        self.config.picker_usage = gw_config.PICKER_USAGE_OFF
        off = await self.models(await self.make_client())
        self.assertEqual([r["id"] for r in on["data"]], [r["id"] for r in off["data"]])
        self.assertNotEqual(self.names(on), self.names(off))

    async def test_a_stale_reading_carries_its_clock_time(self) -> None:
        await self.gateway.usage_windows.refresh()
        observed = time.time()
        self.gateway.usage_windows._clock = lambda: observed + 600
        self.fetch.gate = asyncio.Event()  # the refresh this read schedules stays in flight
        label = self.names(await self.models())["claude-gw/glm-5.3[1m]"]
        self.assertRegex(label, r" · 5h 10% · wk 72% used \(as of \d\d:\d\d\)$")

    async def test_opt_out_gives_clean_labels_and_fetches_nothing(self) -> None:
        self.config.picker_usage = gw_config.PICKER_USAGE_OFF
        client = await self.make_client()
        gateway = client.app[APP_KEY]
        recorder = _FakeFetch({QUOTA_URL: (200, ZAI_PAYLOAD_2026)})
        gateway.usage_windows._fetch = recorder
        names = self.names(await self.models(client))
        self.assertEqual(names["claude-gw/glm-5.3[1m]"], "glm-5.3 · Z.ai subscription · 1M ctx")
        self.assertFalse(gateway.usage_windows.refreshing)
        self.assertEqual(recorder.calls, [])
        # Even with a warm cache the labels stay clean.
        await gateway.usage_windows.refresh()
        names = self.names(await self.models(client))
        self.assertEqual(names["claude-gw/glm-5.3[1m]"], "glm-5.3 · Z.ai subscription · 1M ctx")

    async def test_health_reports_the_mode_in_force(self) -> None:
        body = await (await self.client.get("/health")).json()
        self.assertEqual(body["picker_usage"], gw_config.PICKER_USAGE_ON)


class PickerUsageKnobTests(unittest.TestCase):
    def resolve(self, value: Optional[str]) -> str:
        env = {k: v for k, v in os.environ.items() if k != "VCT_MODEL_GATEWAY_PICKER_USAGE"}
        if value is not None:
            env["VCT_MODEL_GATEWAY_PICKER_USAGE"] = value
        with mock.patch.dict(os.environ, env, clear=True):
            return gw_config.resolve_picker_usage()

    def test_default_is_on(self) -> None:
        self.assertEqual(self.resolve(None), gw_config.PICKER_USAGE_ON)
        self.assertEqual(self.resolve("  "), gw_config.PICKER_USAGE_ON)

    def test_off_and_its_boolean_spellings(self) -> None:
        for raw in ("off", " OFF ", "0", "false", "No"):
            self.assertEqual(self.resolve(raw), gw_config.PICKER_USAGE_OFF, raw)
        for raw in ("on", "1", "TRUE", "yes"):
            self.assertEqual(self.resolve(raw), gw_config.PICKER_USAGE_ON, raw)

    def test_a_typo_warns_and_keeps_the_daemon_up(self) -> None:
        with self.assertLogs("model_router.config", level="WARNING") as logs:
            self.assertEqual(self.resolve("offf"), gw_config.PICKER_USAGE_ON)
        self.assertIn("offf", logs.output[0])
        self.assertIn("on, off", logs.output[0])

    def test_from_env_reads_it(self) -> None:
        with mock.patch.dict(os.environ, {"VCT_MODEL_GATEWAY_PICKER_USAGE": "off"}):
            self.assertEqual(
                gw_config.GatewayConfig.from_env().picker_usage, gw_config.PICKER_USAGE_OFF,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
