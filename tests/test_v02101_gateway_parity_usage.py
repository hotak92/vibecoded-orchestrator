# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 gateway-parity usage fix (item 13d).

One defect from ``reviews/GATEWAY-VENDOR-PARITY-2026-10-03.md``:

* **13d — the access line could not tell a reported zero from a silence.**
  ``server.py`` promises "a ``-`` for anything the response did not report",
  but ``UsageAccumulator.totals()`` defaults unseen fields to 0, so a zai turn
  logged ``cache_c=0`` although z.ai never sent that field. The line now
  dashes each field the response never reported (the accumulator already
  tracks the ``_seen`` set), while the ledger and every other ``totals()``
  consumer keep their zeros.

No network: the streams are the live captures under
``tests/fixtures/model_router/``.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from model_router import usage as U

_HERE = Path(__file__).resolve()
_FIXTURES = _HERE.parent / "fixtures" / "model_router"


def _fixture(name: str) -> bytes:
    return (_FIXTURES / f"{name}.body").read_bytes()


def _sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")


def _accumulate(raw: bytes) -> U.UsageAccumulator:
    acc = U.UsageAccumulator(stream=True)
    acc.feed(raw)
    acc.close()
    return acc


class AccessLinePerFieldTests(unittest.TestCase):
    """A field counts as reported only if the response actually sent it."""

    def test_a_zai_turn_dashes_the_cache_creation_it_never_sent(self) -> None:
        """The live zai capture: ``message_delta`` carries in/out/cache_r and
        NEVER cache_creation. Before the fix the line read ``cache_c=0`` — a
        default dressed up as an observation."""
        acc = _accumulate(_fixture("zai-stream"))
        self.assertNotIn("cache_creation_input_tokens", acc.totals().reported)
        self.assertEqual(
            U.access_extra(acc.totals(), seen=acc.saw_anything),
            "in=18 cache_c=- cache_r=0 out=21 ctx=18",
        )

    def test_an_anthropic_turn_reports_all_four_numbers(self) -> None:
        acc = _accumulate(_fixture("anthropic-stream"))
        self.assertEqual(
            sorted(acc.totals().reported),
            [
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "input_tokens",
                "output_tokens",
            ],
        )
        self.assertEqual(
            U.access_extra(acc.totals(), seen=acc.saw_anything),
            "in=13 cache_c=0 cache_r=0 out=5 ctx=13",
        )

    def test_an_explicit_zero_is_an_observation_and_is_printed(self) -> None:
        """A response that says ``0`` is not silent — a reported zero is ``0``,
        not ``-``."""
        raw = _sse("message_start", {
            "type": "message_start",
            "message": {"usage": {
                "input_tokens": 0, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "output_tokens": 0,
            }},
        })
        acc = _accumulate(raw)
        self.assertEqual(
            U.access_extra(acc.totals(), seen=acc.saw_anything),
            "in=0 cache_c=0 cache_r=0 out=0 ctx=0",
        )

    def test_ctx_is_dashed_when_no_context_field_was_reported(self) -> None:
        """``ctx`` sums the reported context fields only; with none of them
        sent there is nothing to sum and a ``0`` would be an invention."""
        raw = _sse("message_delta", {
            "type": "message_delta", "usage": {"output_tokens": 7},
        })
        acc = _accumulate(raw)
        self.assertEqual(
            U.access_extra(acc.totals(), seen=acc.saw_anything),
            "in=- cache_c=- cache_r=- out=7 ctx=-",
        )

    def test_totals_still_fills_unseen_fields_with_zero_for_the_ledger(self) -> None:
        """The fix must not change what every OTHER consumer reads: the merged
        mapping still carries a zero for an unseen field, so the ledger row and
        ``build_record`` are byte-for-byte what they were."""
        acc = _accumulate(_fixture("zai-stream"))
        totals = acc.totals()
        self.assertEqual(totals["cache_creation_input_tokens"], 0)
        record = U.build_record(
            session="s", agent=None, parent_agent=None,
            requested="claude-gw/zai/glm-5.3", route="zai", forward="glm-5.3",
            stream=True, status=200, totals=totals,
            usage_complete=acc.complete, window_actual=200_000, window_source="table",
        )
        self.assertEqual(record.cache_creation_input_tokens, 0)
        self.assertEqual(record.context_tokens, 18)  # 18 + 0 + 0

    def test_a_plain_mapping_keeps_the_old_whole_group_rule(self) -> None:
        """A caller that hands over numbers without provenance is unchanged."""
        self.assertEqual(
            U.access_extra(
                {
                    "input_tokens": 10, "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 3, "output_tokens": 4,
                },
                seen=True,
            ),
            "in=10 cache_c=2 cache_r=3 out=4 ctx=15",
        )
        self.assertEqual(
            U.access_extra({}, seen=False),
            "in=- cache_c=- cache_r=- out=- ctx=-",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()