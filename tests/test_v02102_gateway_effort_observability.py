# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 lane W6-H — the gateway records the effort level (register item 2026-10-10).

The access log line and the usage-ledger row recorded nothing about the
effort selector a request carried, before or after the gateway's per-vendor
translation (:mod:`model_router.effort`). A ``medium`` request answered at
``high`` (qwen-route glm-5.3, the live 400 that built the translator) left
no trace of either value, so "which effort did this lane actually run at?"
was unanswerable from the gateway's own records.

The pair, on every route (first-party and every vendor):

* ``effort_requested`` — what the CLIENT sent: ``output_config.effort``
  verbatim, or the older build's ``thinking.budget_tokens`` spelled
  ``budget_tokens=<n>``;
* ``effort_forwarded`` — what the gateway sent UPSTREAM, read off the
  payload after every shaping pass.

Absent → ``null`` / ``-``, never a guess: an unparseable or over-buffer
body never learns either value.

Driven here against the loopback stub upstreams from
:mod:`tests.test_model_router_server` — the full HTTP relay, the real access
line, the real ledger file. No network.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from typing import Any

from model_router.effort import requested_effort
from model_router.usage import build_record, effort_extra
from tests.test_model_router_server import GatewayTestBase

LOGGER = "model_router.server"


def _success(model: str) -> dict[str, Any]:
    """A non-stream 200 that reports usage (so the ledger writes a row)."""
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }


# ═══════════════════════════════════════════════════════════════════════════
# The reader — ONE home for both spellings
# ═══════════════════════════════════════════════════════════════════════════


class RequestedEffortTests(unittest.TestCase):

    def test_output_config_effort_is_returned_verbatim(self) -> None:
        self.assertEqual(
            requested_effort({"output_config": {"effort": "medium"}}),
            "medium",
        )

    def test_the_older_budget_tokens_spelling_is_labelled(self) -> None:
        self.assertEqual(
            requested_effort({"thinking": {"budget_tokens": 512}}),
            "budget_tokens=512",
        )

    def test_a_string_effort_wins_over_a_budget(self) -> None:
        # Current builds send BOTH; the named selector is the effort.
        self.assertEqual(
            requested_effort({
                "output_config": {"effort": "high"},
                "thinking": {"budget_tokens": 8192},
            }),
            "high",
        )

    def test_absent_is_none_never_a_guess(self) -> None:
        self.assertIsNone(requested_effort({"messages": []}))
        self.assertIsNone(requested_effort({"output_config": {}}))
        self.assertIsNone(requested_effort({"output_config": {"effort": 3}}))
        self.assertIsNone(requested_effort("not a dict"))
        self.assertIsNone(requested_effort(None))

    def test_a_boolean_is_not_a_token_budget(self) -> None:
        self.assertIsNone(
            requested_effort({"thinking": {"budget_tokens": True}}),
        )


class EffortExtraTests(unittest.TestCase):
    """The access-line pair, in ONE shape on every proxied line."""

    def test_both_values_are_printed(self) -> None:
        self.assertEqual(
            effort_extra("medium", "high"), "effort_req=medium effort_fwd=high",
        )

    def test_absent_is_a_dash_never_an_invented_value(self) -> None:
        self.assertEqual(effort_extra(None, None), "effort_req=- effort_fwd=-")
        self.assertEqual(effort_extra(None, "low"), "effort_req=- effort_fwd=low")


class BuildRecordTests(unittest.TestCase):

    def _record(self, **kwargs: Any):
        return build_record(
            session="s",
            agent=None,
            parent_agent=None,
            requested="claude-gw/qwen/glm-5.3",
            route="vendor:qwen",
            forward="glm-5.3",
            stream=False,
            status=200,
            totals={"input_tokens": 1, "output_tokens": 1},
            usage_complete=True,
            window_actual=None,
            window_source="unknown",
            **kwargs,
        )

    def test_the_pair_is_carried_and_additive(self) -> None:
        record = self._record(
            effort_requested="medium", effort_forwarded="high",
        )
        row = json.loads(record.to_json())
        self.assertEqual(row["effort_requested"], "medium")
        self.assertEqual(row["effort_forwarded"], "high")

    def test_absent_effort_is_null_not_a_default(self) -> None:
        row = json.loads(self._record().to_json())
        self.assertIsNone(row["effort_requested"])
        self.assertIsNone(row["effort_forwarded"])

    def test_the_budget_spelling_round_trips_as_a_string(self) -> None:
        row = json.loads(
            self._record(
                effort_requested="budget_tokens=512",
                effort_forwarded="budget_tokens=512",
            ).to_json()
        )
        self.assertEqual(row["effort_requested"], "budget_tokens=512")


# ═══════════════════════════════════════════════════════════════════════════
# The relay: the pair on the access line and in the ledger, per route
# ═══════════════════════════════════════════════════════════════════════════


class EffortObservabilityTestBase(GatewayTestBase):
    """The stock fixture, rewired as the qwen row, with a usage-bearing stub."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from model_router.vendors import VENDORS

        self.vendor = replace(VENDORS["qwen"], upstream=self.vendor.upstream)
        self.client = await self.make_client()
        self.vendor_up.messages_body = _success("glm-5.3")

    def chat(self, session: str = "chat-effort") -> dict[str, str]:
        return {**self.auth(), "x-claude-code-session-id": session}

    async def rows(self) -> list[dict]:
        from model_router import server as srv

        await self.client.app[srv.APP_KEY].quiesce()
        path = self.usage_ledger_path
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    async def post(self, payload: dict[str, Any]):
        resp = await self.client.post(
            "/v1/messages", json=payload, headers=self.chat(),
        )
        assert resp.status == 200, (resp.status, await resp.text())
        return resp


class EffortOnTheLedgerTests(EffortObservabilityTestBase):

    async def test_translation_is_recorded_on_the_qwen_route(self) -> None:
        """medium → high: BOTH sides of the rewrite land in the row."""
        await self.post({
            "model": "claude-gw/qwen/glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
            "output_config": {"effort": "medium"},
        })
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["effort_requested"], "medium")
        self.assertEqual(rows[0]["effort_forwarded"], "high")

    async def test_an_accepted_value_is_recorded_unchanged(self) -> None:
        await self.post({
            "model": "claude-gw/qwen/glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
            "output_config": {"effort": "low"},
        })
        rows = await self.rows()
        self.assertEqual(rows[0]["effort_requested"], "low")
        self.assertEqual(rows[0]["effort_forwarded"], "low")

    async def test_absent_effort_is_null_on_the_row(self) -> None:
        await self.post({
            "model": "claude-gw/qwen/glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
        })
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["effort_requested"])
        self.assertIsNone(rows[0]["effort_forwarded"])

    async def test_the_budget_spelling_is_recorded_on_the_row(self) -> None:
        await self.post({
            "model": "claude-gw/qwen/glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"budget_tokens": 2048},
        })
        rows = await self.rows()
        self.assertEqual(rows[0]["effort_requested"], "budget_tokens=2048")
        # No policy translates budget_tokens (effort.py module rule), so the
        # forwarded value is the client's own.
        self.assertEqual(rows[0]["effort_forwarded"], "budget_tokens=2048")

    async def test_first_party_route_records_the_pair_too(self) -> None:
        self.anthropic_up.messages_body = _success("claude-opus-5")
        await self.post({
            "model": "claude-opus-5",
            "messages": [{"role": "user", "content": "hi"}],
            "output_config": {"effort": "medium"},
        })
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["route"], "anthropic")
        self.assertEqual(rows[0]["effort_requested"], "medium")
        # The first-party route translates nothing.
        self.assertEqual(rows[0]["effort_forwarded"], "medium")

    async def test_zai_route_records_the_pair(self) -> None:
        from model_router.vendors import VENDORS

        self.vendor = replace(VENDORS["zai"], upstream=self.vendor.upstream)
        self.client = await self.make_client()
        self.vendor_up.messages_body = _success("glm-5.3")
        await self.post({
            "model": "claude-gw/glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
            "output_config": {"effort": "medium"},
        })
        rows = await self.rows()
        self.assertEqual(rows[0]["effort_requested"], "medium")
        # zai accepts medium live — nothing is rewritten.
        self.assertEqual(rows[0]["effort_forwarded"], "medium")


class EffortOnTheAccessLineTests(EffortObservabilityTestBase):

    async def _line(self, payload: dict[str, Any]) -> str:
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await self.post(payload)
        return next(m for m in captured.output if "requested=" in m)

    async def test_the_pair_rides_the_access_line(self) -> None:
        line = await self._line({
            "model": "claude-gw/qwen/glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
            "output_config": {"effort": "medium"},
        })
        self.assertIn("effort_req=medium", line)
        self.assertIn("effort_fwd=high", line)
        # The translation note stays beside it.
        self.assertIn("note=effort_translated", line)

    async def test_absent_effort_is_a_dash_on_the_access_line(self) -> None:
        line = await self._line({
            "model": "claude-gw/qwen/glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertIn("effort_req=-", line)
        self.assertIn("effort_fwd=-", line)
