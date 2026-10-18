# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 — Kimi's 5h usage window read reliably.

Two live defects (owner report 2026-10-18; gateway log from 2026-10-17):

* **The 5h window read unknown on a SUCCESSFUL fetch.** The usages body is
  proto3 JSON for ``limits[]`` and proto3 JSON omits a field at its default
  value, so an unused window's ``detail`` has ``limit`` and ``remaining`` but
  no ``used``. The parser read only ``used`` and called the window unknown —
  exactly when the owner had 100 % of it left. Now ``used`` is derived as
  ``limit - remaining``, and ``usages.limit_5h.used_ratio`` is the fallback
  when ``limits[]`` yields nothing; ``limits[].detail`` stays preferred when
  both read (the two disagree — see ``parse_kimi_usages``).
* **Most refreshes drew HTTP 403.** The gateway relayed the LAST client
  user-agent any refresh signal carried, and ``/usage/windows`` is polled by
  the launcher's usage card through ``Python-urllib`` — a UA the vendor's
  Cloudflare edge refuses ("error code: 1010"; live probe 2026-10-18: the
  same request answered 200 to ``claude-cli/...`` and ``curl/8.5.0``, 403 to
  ``Python-urllib/3.12``). Now the few most recent distinct relayed UAs are
  kept, a refused one is skipped for an hour, and the next one is tried in
  the same refresh. Still relayed, never invented.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from model_router import usage_windows as uw
from model_router.usage_windows import UsageWindows, parse_kimi_usages
from model_router.vendors import VENDORS

_FIXTURES = Path(__file__).parent / "fixtures" / "model_router"
#: LIVE 2026-10-18, ``GET https://api.kimi.ai/coding/v1/usages`` with the
#: owner's key, headers built by ``_kimi_quota_headers`` and a Claude Code
#: user-agent the gateway had recorded -> HTTP 200 with this JSON (no
#: account id in the body; nothing redacted).
KIMI_USAGES_1018 = json.loads(
    (_FIXTURES / "kimi-usages-2026-10-18.json").read_text(encoding="utf-8"),
)
KIMI_USAGES_1007 = json.loads(
    (_FIXTURES / "kimi-usages.json").read_text(encoding="utf-8"),
)
NOW_1018 = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc).timestamp()
NOW_1007 = datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc).timestamp()

CLAUDE_UA = "claude-cli/2.1.263 (external, claude-vscode, agent-sdk/0.3.263)"
LIBRARY_UA = "Python-urllib/3.12"
CURL_UA = "curl/8.5.0"


def _detail_payload(detail: Mapping[str, Any], *, ratio: Any = None) -> dict:
    payload: dict = {
        "limits": [{
            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": dict(detail),
        }],
    }
    if ratio is not None:
        payload["usages"] = {
            "limit_5h": {"used_ratio": ratio, "reset_time": "2026-10-18T06:13:09Z"},
        }
    return payload


def _five_hour(payload: Any, now: float = NOW_1018) -> uw.Window:
    _plan, windows = parse_kimi_usages(payload, now=now)
    return {w.id: w for w in windows}["5h"]


class KimiFiveHourShapeTests(unittest.TestCase):
    def test_both_live_shapes_read_the_detail_count(self) -> None:
        # 2026-10-18: detail 1 of 100 used, ratio 0.0135 — the detail wins.
        window = _five_hour(KIMI_USAGES_1018)
        self.assertEqual(window.percent, 1.0)
        self.assertIsNone(window.unknown_reason)
        self.assertEqual(
            window.resets_at,
            datetime(2026, 10, 18, 6, 13, 9, 844464, tzinfo=timezone.utc).timestamp(),
        )
        _plan, windows = parse_kimi_usages(KIMI_USAGES_1018, now=NOW_1018)
        by_id = {w.id: w for w in windows}
        self.assertEqual(by_id["monthly"].percent, 34.53)
        self.assertEqual(by_id["monthly_code"].percent, 34.53)
        # 2026-10-07: detail 9 of 100, ratio 0 — still 9 %.
        self.assertEqual(_five_hour(KIMI_USAGES_1007, NOW_1007).percent, 9.0)

    def test_an_unused_window_without_used_reads_zero_not_unknown(self) -> None:
        """proto3 JSON omits ``used`` at 0: limit and remaining say 0 %."""
        window = _five_hour(_detail_payload({
            "limit": "100", "remaining": "100",
            "resetTime": "2026-10-18T06:13:09.844464Z",
        }))
        self.assertEqual(window.percent, 0.0)
        self.assertIsNone(window.unknown_reason)

    def test_a_missing_used_is_derived_from_remaining(self) -> None:
        window = _five_hour(_detail_payload({"limit": "100", "remaining": "73"}))
        self.assertEqual(window.percent, 27.0)

    def test_the_ratio_is_the_fallback_when_limits_has_no_5h_reading(self) -> None:
        for payload in (
            # No limits[] at all.
            {"usages": {"limit_5h": {"used_ratio": 0.25,
                                     "reset_time": "2026-10-18T06:13:09Z"}}},
            # A 5h entry whose detail reads nothing (no used, no remaining).
            _detail_payload({"limit": "100"}, ratio=0.25),
            # A detail block that is missing altogether.
            {"limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"}}],
             "usages": {"limit_5h": {"used_ratio": 0.25}}},
        ):
            with self.subTest(payload=payload):
                window = _five_hour(payload)
                self.assertEqual(window.percent, 25.0)
                self.assertEqual(window.source, uw.SOURCE_KIMI_USAGES)

    def test_a_zero_ratio_fallback_reads_zero(self) -> None:
        window = _five_hour({"usages": {"limit_5h": {"used_ratio": 0}}})
        self.assertEqual(window.percent, 0.0)
        self.assertIsNone(window.unknown_reason)

    def test_the_detail_is_preferred_over_the_ratio_when_both_read(self) -> None:
        window = _five_hour(_detail_payload({"limit": "100", "remaining": "100"},
                                            ratio=0.4))
        self.assertEqual(window.percent, 0.0)

    def test_genuinely_missing_data_stays_unknown(self) -> None:
        for payload in (
            _detail_payload({"limit": "100"}),  # no used, no remaining, no ratio
            _detail_payload({"remaining": "100"}),  # no limit
            _detail_payload({"limit": "100", "remaining": "150"}),  # incoherent
            {"usages": {"limit_5h": {"reset_time": "2026-10-18T06:13:09Z"}}},
            {"usages": {"limit_5h": {"used_ratio": "lots"}}},
        ):
            with self.subTest(payload=payload):
                window = _five_hour(payload)
                self.assertIsNone(window.percent)
                self.assertEqual(window.unknown_reason, uw.UNKNOWN_NOT_REPORTED)


# ── which relayed user-agent the quota fetch carries ─────────────────────
class _EdgeFetch:
    """Answers like the vendor's edge: 403 to the refused user-agents."""

    def __init__(self, refused: "set[str]", body: Any = KIMI_USAGES_1018) -> None:
        self.refused = set(refused)
        self.body = body
        self.agents: list[Optional[str]] = []

    async def __call__(self, url: str, headers: Mapping[str, str]):
        agent = headers.get("User-Agent")
        self.agents.append(agent)
        if agent in self.refused:
            return 403, None
        return 200, self.body


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _service(fetch: _EdgeFetch, clock: _Clock) -> UsageWindows:
    kimi = VENDORS["kimi"]
    vendor = type(kimi)(**{**kimi.__dict__, "quota_url": "https://kimi.example/v1/usages"})

    async def vendor_key(_vendor: Any) -> Optional[str]:
        return "synthetic-key"

    return UsageWindows(
        anthropic_upstream="https://anthropic.example/",
        vendors={"kimi": vendor},
        oauth_token=lambda: None,
        vendor_key=vendor_key,
        fetch=fetch,
        ledger_path=lambda: None,
        clock=clock,
        rng=lambda: 0.5,
        resets_path=lambda: None,
    )


def _kimi(service: UsageWindows) -> dict:
    return next(v for v in service.snapshot()["vendors"] if v["id"] == "kimi")


class KimiUserAgentChoiceTests(unittest.IsolatedAsyncioTestCase):
    async def _signal(self, service: UsageWindows, *, chat: "Sequence[str]" = (),
                      reads: "Sequence[str]" = ()) -> None:
        for agent in chat:
            service.note_activity(agent)
        for agent in reads:
            service.request_refresh(agent)
        await service.stop()  # only the explicit refresh() below fetches

    async def test_a_refused_reader_ua_does_not_cost_the_reading(self) -> None:
        """The live defect: Claude Code chats, then the launcher card reads
        ``/usage/windows`` with Python-urllib. The refresh asks as the most
        recent UA, is refused, and tries the Claude Code UA in the SAME
        refresh."""
        clock = _Clock(NOW_1018)
        fetch = _EdgeFetch({LIBRARY_UA})
        service = _service(fetch, clock)
        await self._signal(service, chat=[CLAUDE_UA], reads=[LIBRARY_UA])
        await service.refresh()
        self.assertEqual(fetch.agents, [LIBRARY_UA, CLAUDE_UA])
        kimi = _kimi(service)
        self.assertEqual(kimi["state"], "ok")
        self.assertIsNone(kimi["problem"])
        by_id = {w["id"]: w for w in kimi["windows"]}
        self.assertEqual(by_id["5h"]["percent"], 1.0)

    async def test_a_refused_ua_is_skipped_until_its_retry_interval(self) -> None:
        clock = _Clock(NOW_1018)
        fetch = _EdgeFetch({LIBRARY_UA})
        service = _service(fetch, clock)
        await self._signal(service, chat=[CLAUDE_UA], reads=[LIBRARY_UA])
        await service.refresh()
        fetch.agents.clear()
        # The launcher keeps polling: the refused UA is the most recent again.
        await self._signal(service, reads=[LIBRARY_UA])
        clock.now += 10 * 60
        await service.refresh()
        self.assertEqual(fetch.agents, [CLAUDE_UA], "a refused UA is not re-asked")
        # Past the retry interval it is usable again, but the ACCEPTED UA is
        # asked first, so while that one keeps answering the refused reader
        # costs no request at all.
        fetch.agents.clear()
        clock.now += uw.REFUSED_USER_AGENT_RETRY_S
        await service.refresh()
        self.assertEqual(fetch.agents, [CLAUDE_UA])

    async def test_the_most_recent_accepted_ua_is_used_first(self) -> None:
        """Leave-alone: with every UA accepted, one fetch, most recent UA."""
        fetch = _EdgeFetch(set())
        service = _service(fetch, _Clock(NOW_1018))
        await self._signal(service, chat=[CLAUDE_UA], reads=[CURL_UA])
        await service.refresh()
        self.assertEqual(fetch.agents, [CURL_UA])

    async def test_every_ua_refused_says_so_and_keeps_the_last_reading(self) -> None:
        """Kimi's side: the edge refuses everything. The problem says so in
        plain words and the last good reading stays with its fetched_at."""
        clock = _Clock(NOW_1018)
        fetch = _EdgeFetch(set())
        service = _service(fetch, clock)
        await self._signal(service, chat=[CLAUDE_UA])
        await service.refresh()
        good_at = {w["id"]: w["fetched_at"] for w in _kimi(service)["windows"]}
        fetch.refused = {CLAUDE_UA, LIBRARY_UA}
        await self._signal(service, reads=[LIBRARY_UA])
        clock.now += 5 * 60
        await service.refresh()
        # The accepted UA is asked first, then the other one.
        self.assertEqual(fetch.agents, [CLAUDE_UA, CLAUDE_UA, LIBRARY_UA])
        kimi = _kimi(service)
        self.assertEqual(kimi["state"], "ok")
        self.assertIn("HTTP 403", kimi["problem"])
        self.assertIn("vendor's edge refused", kimi["problem"])
        self.assertIn("last good reading is kept", kimi["problem"])
        by_id = {w["id"]: w for w in kimi["windows"]}
        self.assertEqual(by_id["5h"]["percent"], 1.0)
        self.assertEqual({k: by_id[k]["fetched_at"] for k in by_id}, good_at)
        # Next refresh inside the retry interval: nothing is asked, and the
        # problem still names the refusal rather than a missing user-agent.
        fetch.agents.clear()
        clock.now += 5 * 60
        await service.refresh()
        self.assertEqual(fetch.agents, [])
        self.assertIn("refused (HTTP 403) every client user-agent",
                      _kimi(service)["problem"])

    async def test_no_ua_relayed_yet_is_still_not_asked(self) -> None:
        fetch = _EdgeFetch(set())
        service = _service(fetch, _Clock(NOW_1018))
        await service.refresh()
        self.assertEqual(fetch.agents, [])
        self.assertIn("no client user-agent relayed yet", _kimi(service)["problem"])

    async def test_only_a_few_distinct_uas_are_remembered(self) -> None:
        fetch = _EdgeFetch(set())
        service = _service(fetch, _Clock(NOW_1018))
        agents = [f"client-{n}/1.0" for n in range(uw.MAX_CLIENT_USER_AGENTS + 3)]
        await self._signal(service, chat=agents + [agents[-2]])
        self.assertEqual(len(service._client_user_agents), uw.MAX_CLIENT_USER_AGENTS)
        # A repeat moves to the front rather than duplicating.
        self.assertEqual(service._client_user_agents[-1], agents[-2])
        self.assertEqual(len(set(service._client_user_agents)),
                         uw.MAX_CLIENT_USER_AGENTS)


# ── the ledger FALLBACK ESTIMATE of the 5h window (owner 2026-10-18) ─────
RESET_1018 = datetime(2026, 10, 18, 6, 13, 9, 844464, tzinfo=timezone.utc).timestamp()


def _row(ts: str, route: str, **tokens: int) -> str:
    return json.dumps({"ts": ts, "route": route, "status": 200, **tokens}) + "\n"


class _LedgerCase(unittest.IsolatedAsyncioTestCase):
    """A synthetic ledger + reset file in a temp dir; no tests of its own."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self._tmp.name) / "gateway-usage.jsonl"
        self.ledger.write_text("".join((
            # Before the window (01:13:09Z start): ignored.
            _row("2026-10-18T01:00:00Z", "vendor:kimi", input_tokens=99_999),
            # Inside: input + cache-creation + output count, cache-read not.
            _row("2026-10-18T02:00:00Z", "vendor:kimi", input_tokens=500),
            _row("2026-10-18T03:49:05.811171Z", "vendor:kimi", input_tokens=1000,
                 cache_creation_input_tokens=200, cache_read_input_tokens=5000,
                 output_tokens=300),
            # Another route inside the window: ignored.
            _row("2026-10-18T03:00:00Z", "anthropic", input_tokens=77_777),
            _row("2026-10-18T03:10:00Z", "vendor:zai", input_tokens=55_555),
        )), encoding="utf-8")
        self.resets = Path(self._tmp.name) / "usage_window_resets.json"
        #: What the service's ledger_path answers (None = no ledger home).
        self.ledger_home: Optional[Path] = self.ledger
        self.clock = _Clock(NOW_1018)
        self.fetch = _EdgeFetch(set())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make(self, budget: Optional[int] = 100_000) -> UsageWindows:
        kimi = VENDORS["kimi"]
        vendor = type(kimi)(**{**kimi.__dict__, "quota_url": "https://kimi.example/v1/usages"})

        async def vendor_key(_vendor: Any) -> Optional[str]:
            return "synthetic-key"

        return UsageWindows(
            anthropic_upstream="https://anthropic.example/",
            vendors={"kimi": vendor},
            oauth_token=lambda: None,
            vendor_key=vendor_key,
            fetch=self.fetch,
            ledger_path=lambda: self.ledger_home,
            clock=self.clock,
            rng=lambda: 0.5,
            kimi_5h_budget=budget,
            resets_path=lambda: self.resets,
        )

    async def _good_then_refused(self, service: UsageWindows, *, after_s: float) -> None:
        service.note_activity(CLAUDE_UA)
        await service.stop()
        await service.refresh()  # a good live reading: reset 06:13:09Z
        self.fetch.refused = {CLAUDE_UA}
        self.clock.now += after_s
        await service.refresh()  # the edge refuses from now on

    @staticmethod
    def _five(service: UsageWindows) -> dict:
        return {w["id"]: w for w in _kimi(service)["windows"]}["5h"]


class KimiLedgerEstimateTests(_LedgerCase):
    async def test_the_live_reading_wins_while_it_is_known(self) -> None:
        service = self._make()
        service.note_activity(CLAUDE_UA)
        await service.stop()
        await service.refresh()
        five = self._five(service)
        self.assertEqual(five["source"], uw.SOURCE_KIMI_USAGES)
        self.assertEqual(five["percent"], 1.0)
        self.assertNotIn("estimate", five)

    async def test_the_estimate_stands_in_when_the_live_reading_is_unknown(self) -> None:
        service = self._make()
        await self._good_then_refused(service, after_s=uw.STALE_AFTER_S + 60)
        five = self._five(service)
        self.assertEqual(five["source"], uw.SOURCE_LEDGER_ESTIMATE)
        # 500 + (1000 + 200 + 300) of 100,000: the window's kimi rows only,
        # cache reads excluded, the row before the window start excluded.
        self.assertEqual(five["estimate"]["tokens"], 2000)
        self.assertEqual(five["estimate"]["requests"], 2)
        self.assertEqual(five["percent"], 2.0)
        self.assertIsNone(five["unknown_reason"])
        self.assertEqual(five["resets_at"], uw._iso(RESET_1018))
        self.assertEqual(five["estimate"]["window_start"],
                         uw._iso(RESET_1018 - uw.KIMI_5H_SPAN_S))
        self.assertEqual(five["estimate"]["scope"], "this_gateway")
        # The monthly windows are not estimated: they stay the vendor's.
        monthly = {w["id"]: w for w in _kimi(service)["windows"]}["monthly"]
        self.assertEqual(monthly["source"], uw.SOURCE_KIMI_USAGES)
        # Every surface marks it as an estimate.
        line = uw.render_line(service.snapshot())
        self.assertIn("5h ~2%", line)
        self.assertIn("5h ~2%", uw.label_suffixes(service.snapshot())["kimi"])

    async def test_the_window_rolls_forward_past_the_stored_reset(self) -> None:
        service = self._make()
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(_row("2026-10-18T06:30:00Z", "vendor:kimi",
                              input_tokens=3000, output_tokens=1000))
        # 04:00 -> 07:00: past the 06:13 reset; the next window ends 11:13.
        await self._good_then_refused(service, after_s=3 * 3600)
        five = self._five(service)
        self.assertEqual(five["source"], uw.SOURCE_LEDGER_ESTIMATE)
        self.assertEqual(five["resets_at"], uw._iso(RESET_1018 + uw.KIMI_5H_SPAN_S))
        self.assertEqual(five["estimate"]["tokens"], 4000)
        self.assertEqual(five["percent"], 4.0)

    def test_roll_window_end(self) -> None:
        span = uw.KIMI_5H_SPAN_S
        self.assertEqual(uw.roll_window_end(RESET_1018, RESET_1018 - 1, span), RESET_1018)
        self.assertEqual(uw.roll_window_end(RESET_1018, RESET_1018, span), RESET_1018 + span)
        self.assertEqual(
            uw.roll_window_end(RESET_1018, RESET_1018 + 2 * span + 5, span),
            RESET_1018 + 3 * span,
        )

    async def test_budget_zero_disables_the_estimate(self) -> None:
        service = self._make(budget=0)
        await self._good_then_refused(service, after_s=uw.STALE_AFTER_S + 60)
        five = self._five(service)
        self.assertIsNone(five["percent"])
        self.assertEqual(five["unknown_reason"], uw.UNKNOWN_STALE)
        self.assertNotIn("estimate", five)

    async def test_the_budget_env_is_read_when_none_is_injected(self) -> None:
        for raw, expected in (("0", None), ("50000", 4.0), ("garbage", 0.2)):
            with self.subTest(raw=raw), mock.patch.dict(
                os.environ, {uw.KIMI_5H_BUDGET_ENV: raw},
            ):
                self.clock.now = NOW_1018
                self.fetch.refused = set()
                service = self._make(budget=None)
                await self._good_then_refused(service, after_s=uw.STALE_AFTER_S + 60)
                self.assertEqual(self._five(service)["percent"], expected)

    async def test_no_reset_ever_seen_means_no_estimate(self) -> None:
        self.fetch.refused = {CLAUDE_UA}
        service = self._make()
        service.note_activity(CLAUDE_UA)
        await service.stop()
        await service.refresh()
        self.assertEqual(_kimi(service)["windows"], [])

    async def test_no_ledger_means_no_estimate(self) -> None:
        service = self._make()
        self.ledger_home = None  # the ledger has no home
        await self._good_then_refused(service, after_s=uw.STALE_AFTER_S + 60)
        five = self._five(service)
        self.assertIsNone(five["percent"])
        self.assertNotIn("estimate", five)

    def test_an_unknown_5h_reading_keeps_its_reset_time(self) -> None:
        window = _five_hour(_detail_payload({
            "limit": "100", "resetTime": "2026-10-18T06:13:09.844464Z",
        }))
        self.assertIsNone(window.percent)
        self.assertEqual(window.resets_at, RESET_1018)


class KimiResetPersistenceTests(_LedgerCase):
    """LOW-1 (R14A): the last 5h reset survives a daemon restart."""

    async def test_a_restarted_gateway_estimates_from_the_saved_reset(self) -> None:
        first = self._make()
        first.note_activity(CLAUDE_UA)
        await first.stop()
        await first.refresh()  # a good answer: reset 06:13:09.844464Z saved
        saved = json.loads(self.resets.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"5h_resets": {"kimi": RESET_1018}})
        if os.name == "posix":
            self.assertEqual(self.resets.stat().st_mode & 0o777, 0o600)
        # A new daemon, hours later, whose every fetch is refused: the saved
        # reset rolls forward on the fixed grid and the estimate stands.
        self.fetch.refused = {CLAUDE_UA}
        self.clock.now += 3 * 3600  # 07:00Z, past the saved reset
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(_row("2026-10-18T06:30:00Z", "vendor:kimi", input_tokens=5000))
        second = self._make()
        second.note_activity(CLAUDE_UA)
        await second.stop()
        await second.refresh()
        five = self._five(second)
        self.assertEqual(five["source"], uw.SOURCE_LEDGER_ESTIMATE)
        self.assertEqual(five["resets_at"], uw._iso(RESET_1018 + uw.KIMI_5H_SPAN_S))
        self.assertEqual(five["estimate"]["tokens"], 5000)

    async def test_an_unreadable_saved_file_is_an_empty_start(self) -> None:
        for body in ("not json", '{"5h_resets": "x"}', '{"5h_resets": {"kimi": "soon"}}', "[]"):
            with self.subTest(body=body):
                self.resets.write_text(body, encoding="utf-8")
                self.fetch.refused = {CLAUDE_UA}
                service = self._make()
                service.note_activity(CLAUDE_UA)
                await service.stop()
                await service.refresh()
                self.assertEqual(_kimi(service)["windows"], [])


class KimiAcceptedUserAgentTests(unittest.IsolatedAsyncioTestCase):
    """LOW-2 (R14A): the accepted UA is never evicted by unproven ones."""

    async def test_four_new_reader_uas_do_not_evict_the_accepted_one(self) -> None:
        clock = _Clock(NOW_1018)
        readers = [f"reader-{n}/1.0" for n in range(uw.MAX_CLIENT_USER_AGENTS)]
        fetch = _EdgeFetch(set(readers))
        service = _service(fetch, clock)
        service.note_activity(CLAUDE_UA)
        await service.stop()
        await service.refresh()  # CLAUDE_UA accepted
        for agent in readers:
            service.request_refresh(agent)
        await service.stop()
        self.assertIn(CLAUDE_UA, service._client_user_agents)
        self.assertEqual(len(service._client_user_agents), uw.MAX_CLIENT_USER_AGENTS)
        fetch.agents.clear()
        clock.now += 10 * 60
        await service.refresh()
        self.assertEqual(fetch.agents, [CLAUDE_UA], "the accepted UA is asked first")
        self.assertIsNone(_kimi(service)["problem"])

    async def test_a_refused_accepted_ua_loses_its_pin(self) -> None:
        clock = _Clock(NOW_1018)
        fetch = _EdgeFetch(set())
        service = _service(fetch, clock)
        service.note_activity(CLAUDE_UA)
        await service.stop()
        await service.refresh()
        self.assertEqual(service._accepted_user_agent, CLAUDE_UA)
        fetch.refused = {CLAUDE_UA}
        service.request_refresh(CURL_UA)
        await service.stop()
        fetch.agents.clear()
        clock.now += 10 * 60
        await service.refresh()
        self.assertEqual(fetch.agents, [CLAUDE_UA, CURL_UA])
        self.assertEqual(service._accepted_user_agent, CURL_UA)
        # Refused too: no UA keeps a pin it no longer earns.
        fetch.refused = {CLAUDE_UA, CURL_UA}
        clock.now += uw.REFUSED_USER_AGENT_RETRY_S + 60
        await service.refresh()
        self.assertIsNone(service._accepted_user_agent)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
