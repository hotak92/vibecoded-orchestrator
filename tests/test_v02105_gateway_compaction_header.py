# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 (CL-4) — the usage ledger records ``x-claude-code-compaction``.

Claude Code 2.1.273 marks the requests it spends summarising a conversation
with that header. Recorded, a compaction turn stops looking like a user turn
nobody typed: the row says why a chat's context jumped.

The value is client-controlled and lands in a file, so it is normalised by
ONE function (:func:`model_router.usage.read_compaction_header`): whitespace
trimmed, control characters dropped, length capped. And the three
observations stay three: ``null`` (no header), ``""`` (present and blank),
and the value it sent.
"""
from __future__ import annotations

import json
import unittest
from typing import Any

from tests.test_model_router_server import GatewayTestBase

from model_router import usage as U
from model_router.server import APP_KEY

COMPACTION_HEADER = "x-claude-code-compaction"


class ReadCompactionHeaderTests(unittest.TestCase):
    def test_an_absent_header_is_null(self) -> None:
        self.assertIsNone(U.read_compaction_header({}))

    def test_a_present_but_blank_header_is_the_empty_string(self) -> None:
        """Present-and-blank is an observation; folding it into the null would
        lose the fact that the client marked the request."""
        self.assertEqual(U.read_compaction_header({COMPACTION_HEADER: "   "}), "")

    def test_the_value_is_trimmed_and_kept(self) -> None:
        self.assertEqual(
            U.read_compaction_header({COMPACTION_HEADER: "  true "}), "true",
        )

    def test_a_value_outside_the_known_vocabulary_is_recorded(self) -> None:
        """The client may extend the vocabulary; an unknown value is an
        observation, never a formatting error."""
        self.assertEqual(
            U.read_compaction_header({COMPACTION_HEADER: "manual"}), "manual",
        )

    def test_control_characters_never_reach_the_file(self) -> None:
        value = U.read_compaction_header({COMPACTION_HEADER: "au\x00to\r\n"})
        assert value is not None
        self.assertEqual(value, "auto")
        self.assertTrue(value.isprintable())

    def test_an_abusive_value_is_capped(self) -> None:
        value = U.read_compaction_header({COMPACTION_HEADER: "x" * 5000})
        assert value is not None
        self.assertEqual(len(value), 64)

    def test_a_non_string_header_reads_as_absent(self) -> None:
        self.assertIsNone(U.read_compaction_header({COMPACTION_HEADER: None}))  # type: ignore[dict-item]


class RecordTests(unittest.TestCase):
    def _record(self, **over: Any) -> U.UsageRecord:
        fields: dict[str, Any] = dict(
            session="chat-a", agent=None, parent_agent=None,
            requested="claude-opus-5", route="anthropic",
            forward="claude-opus-5", stream=True, status=200,
            totals={name: 1 for name in U.USAGE_FIELDS},
            usage_complete=True, window_actual=200_000, window_source="table",
        )
        fields.update(over)
        return U.build_record(**fields)

    def test_the_row_carries_the_header(self) -> None:
        row = json.loads(self._record(compaction="true").to_json())
        self.assertEqual(row["compaction"], "true")

    def test_an_ordinary_turn_stores_null_not_empty(self) -> None:
        row = json.loads(self._record().to_json())
        self.assertIn("compaction", row)
        self.assertIsNone(row["compaction"])

    def test_the_row_stays_one_line(self) -> None:
        record = self._record(compaction="true")
        self.assertNotIn("\n", record.to_json())


class CompactionThroughTheGatewayTests(GatewayTestBase):
    """Driven against the loopback stub upstream, like every ledger test."""

    async def rows(self) -> list[dict]:
        await self.client.app[APP_KEY].quiesce()
        if not self.usage_ledger_path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.usage_ledger_path.read_text(
                encoding="utf-8",
            ).splitlines()
            if line
        ]

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # A row is only written for a 2xx that REPORTED usage — a row of
        # zeros reads as "this chat is empty" and is deliberately declined
        # (see ``_submit_usage``). The stub states a real one.
        self.anthropic_up.messages_body = {
            "id": "msg_1",
            "model": "claude-opus-5",
            "content": [],
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }

    async def _post(self, **extra: str) -> None:
        resp = await self.client.post(
            "/v1/messages",
            headers={**self.auth(), **extra},
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(resp.status, 200)
        await resp.read()

    async def test_a_compaction_request_is_attributed_in_the_ledger(self) -> None:
        await self._post(**{COMPACTION_HEADER: "true"})
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["compaction"], "true")

    async def test_an_ordinary_turn_records_no_compaction(self) -> None:
        await self._post()
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["compaction"])

    async def test_the_header_changes_nothing_but_the_recorded_field(self) -> None:
        """Attribution, NOT routing: the row is otherwise identical with and
        without the header, so a compaction turn cannot quietly acquire a
        different route, model or window."""
        await self._post()
        await self._post(**{COMPACTION_HEADER: "true"})
        plain, marked = await self.rows()
        ignore = {"ts", "compaction"}
        self.assertEqual(
            {k: v for k, v in plain.items() if k not in ignore},
            {k: v for k, v in marked.items() if k not in ignore},
        )
        self.assertEqual(marked["compaction"], "true")
        self.assertIsNone(plain["compaction"])


class LedgerPathTests(unittest.TestCase):
    def test_the_field_is_declared_on_the_record(self) -> None:
        names = U.UsageRecord.__dataclass_fields__
        self.assertIn("compaction", names)
        self.assertIsNone(names["compaction"].default)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()