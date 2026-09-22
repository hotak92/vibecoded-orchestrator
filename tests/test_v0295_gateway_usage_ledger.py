# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Per-chat token accounting: the merge, the record, the ledger, the routes.

The owner's requirement (2026-09-16) is one sentence — "the overall amount of
tokens present in the current chat's context", per chat, for EVERY model, "even
in presence of subagents or multiple chats going through the gateway" — and it
decomposes into four things that can each be wrong on their own:

1. **the merge.** Two upstreams report usage in opposite halves of the stream,
   so a reader that believes either event alone loses one of them. Both shapes
   are driven here, plus the two orderings a naive last-wins gets wrong.
2. **the arithmetic and the two windows.** ``context_after`` is the number a
   monitor watches; ``pct_client`` vs ``pct_actual`` is the gap between what
   the CLIENT budgets (keyed on the requested id) and what the model really
   has (the catalog resolver).
3. **the ledger.** Append-only, rotated, LRU-bounded, and off the request path
   — including the promise that a row lands even when the stream died.
4. **the relay is untouched.** The accumulator sees a copy; an accumulator
   that raises abandons the accounting and not the stream, and a stream with
   accounting on is byte-identical to one without.

Plus the ``count_tokens`` zero-guard, which is a different promise on the same
bytes: a vendor that answers ``{"input_tokens": 0}`` for a real conversation is
WORSE than no counter at all, because the client's own fallback estimate would
have been positive.

No real port outside the loopback stubs :mod:`tests.test_model_router_server`
already builds, and no network.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from model_router import usage as U
from model_router.routing import Route
from model_router.server import (
    APP_KEY,
    COUNT_TOKENS_PATH,
    RequestFacts,
    _submit_usage,
)

from tests.test_model_router_server import GatewayTestBase


def _sse(event: str, payload: dict) -> bytes:
    """One LF-framed SSE event, the shape both upstreams actually write."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")


def _message_start(**usage: Any) -> bytes:
    """``message_start`` — Anthropic nests the usage inside the message."""
    return _sse(
        "message_start",
        {"type": "message_start", "message": {"id": "msg_1", "usage": usage}},
    )


def _message_delta(**usage: Any) -> bytes:
    """``message_delta`` — the usage sits at the TOP level here."""
    return _sse(
        "message_delta",
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": usage},
    )


ANTHROPIC_STREAM = (
    _message_start(
        input_tokens=1200,
        cache_creation_input_tokens=300,
        cache_read_input_tokens=90_000,
        output_tokens=1,
    )
    + _sse("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""}})
    + _sse("content_block_delta", {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "hi"}})
    + _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    + _message_delta(output_tokens=450)
    + _sse("message_stop", {"type": "message_stop"})
)

#: The measured vendor shape: ZEROS up front, the real figures in the delta.
VENDOR_STREAM = (
    _message_start(
        input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=0,
    )
    + _sse("content_block_delta", {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "ok"}})
    + _message_delta(
        input_tokens=2048,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=64_000,
        output_tokens=99,
    )
    + _sse("message_stop", {"type": "message_stop"})
)


def _feed(accumulator: U.UsageAccumulator, data: bytes, *, chunk: int = 0) -> None:
    if chunk <= 0:
        accumulator.feed(data)
    else:
        for i in range(0, len(data), chunk):
            accumulator.feed(data[i:i + chunk])
    accumulator.close()


# ─────────────────────────── 1. the merge ────────────────────────────────
class AccumulatorMergeTests(unittest.TestCase):
    def test_the_anthropic_shape_is_read_from_both_events(self) -> None:
        acc = U.UsageAccumulator(stream=True)
        _feed(acc, ANTHROPIC_STREAM)
        self.assertEqual(
            acc.totals(),
            {
                "input_tokens": 1200,
                "cache_creation_input_tokens": 300,
                "cache_read_input_tokens": 90_000,
                # message_delta's 450 replaces message_start's placeholder 1.
                "output_tokens": 450,
            },
        )
        self.assertTrue(acc.complete)

    def test_the_vendor_zeros_then_delta_shape_is_read(self) -> None:
        """The half of the field evidence a message_start-only reader loses."""
        acc = U.UsageAccumulator(stream=True)
        _feed(acc, VENDOR_STREAM)
        self.assertEqual(acc.totals()["input_tokens"], 2048)
        self.assertEqual(acc.totals()["cache_read_input_tokens"], 64_000)
        self.assertEqual(acc.totals()["output_tokens"], 99)
        self.assertTrue(acc.complete)

    def test_a_later_zero_does_not_overwrite_an_earlier_positive(self) -> None:
        """ACT half of the rule above: it is not simply last-wins."""
        acc = U.UsageAccumulator(stream=True)
        _feed(
            acc,
            _message_start(input_tokens=5000, output_tokens=1)
            + _message_delta(input_tokens=0, output_tokens=7),
        )
        self.assertEqual(acc.totals()["input_tokens"], 5000)
        self.assertEqual(acc.totals()["output_tokens"], 7)

    def test_a_delta_repeating_the_input_fields_is_honoured(self) -> None:
        """Newer API versions repeat input in message_delta; a positive wins."""
        acc = U.UsageAccumulator(stream=True)
        _feed(
            acc,
            _message_start(input_tokens=10, cache_read_input_tokens=0)
            + _message_delta(
                input_tokens=11, cache_read_input_tokens=42, output_tokens=3,
            ),
        )
        self.assertEqual(acc.totals()["input_tokens"], 11)
        self.assertEqual(acc.totals()["cache_read_input_tokens"], 42)

    def test_a_truncated_stream_is_incomplete_but_not_empty(self) -> None:
        """``message_start`` alone reports all four Anthropic fields, so a
        field census would call this complete. It is not: the final
        ``output_tokens`` never arrived, and a monitor must be able to
        distrust the row."""
        stream = ANTHROPIC_STREAM[: ANTHROPIC_STREAM.index(b"message_delta") - 20]
        acc = U.UsageAccumulator(stream=True)
        _feed(acc, stream)
        self.assertFalse(acc.complete)
        self.assertTrue(acc.saw_anything)
        self.assertEqual(acc.totals()["input_tokens"], 1200)
        # The placeholder from message_start, not a final count.
        self.assertEqual(acc.totals()["output_tokens"], 1)

    def test_a_vendor_that_never_reports_the_cache_fields_is_incomplete(self) -> None:
        """The OTHER half of ``complete``: the zeros in such a row are this
        module's default, not the upstream's statement."""
        acc = U.UsageAccumulator(stream=True)
        _feed(
            acc,
            _message_start(input_tokens=5) + _message_delta(output_tokens=6),
        )
        self.assertFalse(acc.complete)
        self.assertEqual(acc.totals()["cache_read_input_tokens"], 0)

    def test_a_stream_cut_before_any_usage_saw_nothing(self) -> None:
        acc = U.UsageAccumulator(stream=True)
        _feed(acc, b'event: message_start\ndata: {"type":"message_st')
        self.assertFalse(acc.saw_anything)
        self.assertFalse(acc.complete)

    def test_an_event_whose_terminator_never_arrived_is_still_read(self) -> None:
        """A lost blank line costs a terminator, not the tokens it framed."""
        acc = U.UsageAccumulator(stream=True)
        _feed(acc, _message_delta(output_tokens=77).rstrip(b"\n"))
        self.assertEqual(acc.totals()["output_tokens"], 77)

    def test_a_byte_at_a_time_feed_reads_the_same_thing(self) -> None:
        """Chunk boundaries are the upstream's business, not the reader's."""
        whole = U.UsageAccumulator(stream=True)
        _feed(whole, ANTHROPIC_STREAM)
        split = U.UsageAccumulator(stream=True)
        _feed(split, ANTHROPIC_STREAM, chunk=7)
        self.assertEqual(whole.totals(), split.totals())

    def test_a_crlf_framed_stream_is_read(self) -> None:
        crlf = ANTHROPIC_STREAM.replace(b"\n", b"\r\n")
        acc = U.UsageAccumulator(stream=True)
        _feed(acc, crlf)
        self.assertEqual(acc.totals()["output_tokens"], 450)

    def test_a_non_streamed_body_is_read(self) -> None:
        acc = U.UsageAccumulator(stream=False)
        _feed(acc, json.dumps({
            "id": "msg_1", "type": "message", "content": [],
            "usage": {
                "input_tokens": 12, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "output_tokens": 34,
            },
        }).encode("utf-8"))
        self.assertEqual(acc.totals()["input_tokens"], 12)
        self.assertEqual(acc.totals()["output_tokens"], 34)
        self.assertTrue(acc.complete)

    def test_a_non_streamed_body_past_the_bound_is_not_held(self) -> None:
        acc = U.UsageAccumulator(stream=False)
        acc.feed(b"x" * (U.USAGE_BODY_LIMIT_BYTES + 1))
        acc.close()
        self.assertFalse(acc.saw_anything)

    def test_a_boolean_is_not_a_token_count(self) -> None:
        """``True`` is an ``int`` in Python; recording it as 1 token would be
        a plausible-looking wrong number, which is the worst kind."""
        acc = U.UsageAccumulator(stream=True)
        _feed(acc, _message_delta(output_tokens=True, input_tokens=5))
        self.assertEqual(acc.totals()["output_tokens"], 0)
        self.assertEqual(acc.totals()["input_tokens"], 5)


# ──────────────── 1b. the reported model (issue 9's echo) ────────────────
class ReportedModelTests(unittest.TestCase):
    """``reported_model`` feeds the echo assertion in the terminal ``access``
    closure: it must read EXACTLY the shapes the vendor writes — the same
    walk ``usage_block`` does — and treat absence as silence, not mismatch.
    """

    def test_the_streamed_message_start_model_is_read_from_the_nest(self) -> None:
        acc = U.UsageAccumulator(stream=True)
        _feed(acc, _sse("message_start", {
            "type": "message_start",
            "message": {"id": "msg_1", "model": "glm-5.3",
                        "usage": {"input_tokens": 4, "output_tokens": 0}},
        }))
        self.assertEqual(acc.reported_model, "glm-5.3")

    def test_a_top_level_model_in_a_stream_event_is_read_too(self) -> None:
        acc = U.UsageAccumulator(stream=True)
        _feed(acc, _message_delta(output_tokens=2))
        self.assertIsNone(acc.reported_model)
        _feed(acc, _sse("message_delta", {
            "type": "message_delta", "model": "glm-5.3", "usage": {"output_tokens": 2},
        }))
        self.assertEqual(acc.reported_model, "glm-5.3")

    def test_the_last_non_empty_model_wins(self) -> None:
        acc = U.UsageAccumulator(stream=True)
        _feed(acc, _sse("message_start", {
            "type": "message_start",
            "message": {"model": "glm-5.3", "usage": {"input_tokens": 4}},
        }) + _sse("message_delta", {
            "type": "message_delta", "model": "glm-5.3-flash", "usage": {"output_tokens": 2},
        }))
        self.assertEqual(acc.reported_model, "glm-5.3-flash")

    def test_the_non_streamed_body_model_is_read_from_the_top(self) -> None:
        acc = U.UsageAccumulator(stream=False)
        _feed(acc, json.dumps({
            "id": "msg_1", "model": "glm-5.3", "content": [],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }).encode("utf-8"))
        self.assertEqual(acc.reported_model, "glm-5.3")

    def test_absence_is_none_never_a_value(self) -> None:
        acc = U.UsageAccumulator(stream=True)
        _feed(acc, VENDOR_STREAM)
        self.assertIsNone(acc.reported_model)

    def test_an_empty_string_is_absent(self) -> None:
        acc = U.UsageAccumulator(stream=False)
        _feed(acc, json.dumps({
            "id": "msg_1", "model": "", "content": [],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }).encode("utf-8"))
        self.assertIsNone(acc.reported_model)

    def test_reported_model_id_mirrors_the_usage_block_walk(self) -> None:
        """Direct first, then nested — the one reader for both shapes."""
        self.assertEqual(U.reported_model_id({"model": "a"}), "a")
        self.assertEqual(U.reported_model_id({"message": {"model": "b"}}), "b")
        self.assertEqual(
            U.reported_model_id({"model": "a", "message": {"model": "b"}}), "a",
        )
        self.assertIsNone(U.reported_model_id({"model": ""}))
        self.assertIsNone(U.reported_model_id({"model": 7}))
        self.assertIsNone(U.reported_model_id({}))


# ──────────────────── 2. the record and the two windows ──────────────────
class RecordTests(unittest.TestCase):
    def _record(self, requested: str, **totals: int) -> U.UsageRecord:
        merged = {name: 0 for name in U.USAGE_FIELDS}
        merged.update(totals)
        return U.build_record(
            session="sess-1",
            agent=None,
            parent_agent=None,
            requested=requested,
            route="anthropic",
            forward=requested,
            stream=True,
            status=200,
            totals=merged,
            usage_complete=True,
            window_actual=totals.pop("_window", None),
            window_source="table",
            now=datetime(2026, 9, 16, 4, 5, 6, tzinfo=timezone.utc),
        )

    def test_context_tokens_is_what_the_model_was_given(self) -> None:
        record = self._record(
            "claude-opus-5",
            input_tokens=1000,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=50_000,
            output_tokens=400,
        )
        self.assertEqual(record.context_tokens, 51_200)
        # What the NEXT turn carries — the number a monitor watches.
        self.assertEqual(record.context_after, 51_600)

    def test_a_plain_id_budgets_200k_and_a_1m_id_budgets_1m(self) -> None:
        plain = self._record("claude-opus-5", input_tokens=100_000)
        suffixed = self._record("claude-opus-5[1m]", input_tokens=100_000)
        self.assertEqual(plain.window_client, 200_000)
        self.assertEqual(suffixed.window_client, 1_000_000)
        self.assertEqual(plain.pct_client, 50.0)
        self.assertEqual(suffixed.pct_client, 10.0)

    def test_an_unknown_actual_window_reports_null_not_zero(self) -> None:
        """0% would read as "plenty of room", which is the one wrong answer
        that costs the user something."""
        record = U.build_record(
            session=None, agent=None, parent_agent=None,
            requested="claude-gw/glm-9.9", route="vendor:zai",
            forward="glm-9.9", stream=True, status=200,
            totals={name: 0 for name in U.USAGE_FIELDS},
            usage_complete=False, window_actual=None, window_source="unknown",
        )
        self.assertIsNone(record.pct_actual)
        self.assertIsNotNone(record.pct_client)

    def test_the_two_percentages_disagree_when_the_windows_do(self) -> None:
        """The whole point of carrying both: a 1M model asked for WITHOUT the
        suffix is budgeted at 200K by the client and compacts five times too
        early, and that is visible in one row."""
        record = U.build_record(
            session="s", agent=None, parent_agent=None,
            requested="claude-fable-5-1", route="anthropic",
            forward="claude-fable-5-1", stream=True, status=200,
            totals={
                "input_tokens": 150_000, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "output_tokens": 0,
            },
            usage_complete=True, window_actual=1_000_000, window_source="table",
        )
        self.assertEqual(record.pct_client, 75.0)
        self.assertEqual(record.pct_actual, 15.0)

    def test_the_timestamp_is_utc_and_iso(self) -> None:
        record = self._record("claude-opus-5", input_tokens=1)
        self.assertEqual(record.ts, "2026-09-16T04:05:06Z")

    def test_a_row_round_trips_through_json(self) -> None:
        record = self._record("claude-opus-5", input_tokens=7, output_tokens=8)
        parsed = json.loads(record.to_json())
        self.assertEqual(parsed["input_tokens"], 7)
        self.assertEqual(parsed["session"], "sess-1")
        self.assertEqual(parsed["window_source"], "table")


# ─────────────────────── 3. headers → identity ───────────────────────────
class IdentityTests(unittest.TestCase):
    def test_the_three_headers_are_read(self) -> None:
        session, agent, parent = U.read_identity({
            U.SESSION_HEADER: "chat-a",
            U.AGENT_HEADER: "agent-b",
            U.PARENT_AGENT_HEADER: "agent-a",
        })
        self.assertEqual((session, agent, parent), ("chat-a", "agent-b", "agent-a"))

    def test_a_missing_session_is_none_not_a_synthetic_key(self) -> None:
        session, agent, parent = U.read_identity({})
        self.assertIsNone(session)
        self.assertIsNone(agent)
        self.assertIsNone(parent)

    def test_a_blank_header_identifies_nothing(self) -> None:
        session, _, _ = U.read_identity({U.SESSION_HEADER: "   "})
        self.assertIsNone(session)

    def test_a_client_controlled_id_is_length_capped(self) -> None:
        session, _, _ = U.read_identity({U.SESSION_HEADER: "x" * 5000})
        assert session is not None
        self.assertEqual(len(session), 200)


# ──────────────────────────── 4. the ledger ──────────────────────────────
class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wp9-usage-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "metrics" / "gateway-usage.jsonl"

    def _record(self, session: str | None, **over: Any) -> U.UsageRecord:
        fields: dict[str, Any] = dict(
            session=session, agent=None, parent_agent=None,
            requested="claude-opus-5", route="anthropic",
            forward="claude-opus-5", stream=True, status=200,
            totals={name: 1 for name in U.USAGE_FIELDS},
            usage_complete=True, window_actual=200_000, window_source="table",
        )
        fields.update(over)
        return U.build_record(**fields)

    def test_rows_are_appended_one_per_line(self) -> None:
        ledger = U.UsageLedger(self.path)
        ledger.submit(self._record("a"))
        ledger.submit(self._record("b"))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[1])["session"], "b")
        self.assertEqual(ledger.rows_written, 2)

    def test_a_row_with_no_session_still_reaches_the_file(self) -> None:
        """Its tokens are real; it is only absent from the per-CHAT map,
        because "no chat" is not a key."""
        ledger = U.UsageLedger(self.path)
        ledger.submit(self._record(None))
        self.assertEqual(ledger.rows_written, 1)
        self.assertEqual(ledger.sessions(), {})

    def test_the_session_map_keeps_the_NEWEST_row_per_chat(self) -> None:
        ledger = U.UsageLedger(self.path)
        ledger.submit(self._record("a", forward="first"))
        ledger.submit(self._record("a", forward="second"))
        self.assertEqual(list(ledger.sessions()), ["a"])
        self.assertEqual(ledger.sessions()["a"]["forward"], "second")

    def test_the_session_map_is_lru_bounded(self) -> None:
        ledger = U.UsageLedger(self.path, max_sessions=3)
        for name in ("a", "b", "c", "d"):
            ledger.submit(self._record(name))
        self.assertEqual(list(ledger.sessions()), ["b", "c", "d"])
        # Every row is still in the FILE — the bound is on memory, not on
        # the record.
        self.assertEqual(
            len(self.path.read_text(encoding="utf-8").splitlines()), 4,
        )

    def test_a_refreshed_chat_moves_to_the_end_of_the_lru(self) -> None:
        ledger = U.UsageLedger(self.path, max_sessions=2)
        ledger.submit(self._record("a"))
        ledger.submit(self._record("b"))
        ledger.submit(self._record("a"))
        ledger.submit(self._record("c"))
        self.assertEqual(list(ledger.sessions()), ["a", "c"])

    def test_the_file_is_truncated_to_its_newest_rows_at_the_bound(self) -> None:
        """What ``vco_lib.atomic.rotate_tail_lines`` promises, asserted here.

        ONE file — no ``.1`` sibling and no generations — kept under the cap,
        with the newest rows surviving. The promise is deliberately not "no
        row is ever discarded": this is a context monitor, and the rows a
        monitor reads are the recent ones.
        """
        row_bytes = len(self._record("s0").to_json()) + 1
        cap = row_bytes * 8
        ledger = U.UsageLedger(self.path, max_bytes=cap)
        for i in range(12):
            ledger.submit(self._record(f"s{i}"))

        live = [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
        ]
        # It actually rotated: rows were discarded...
        self.assertLess(len(live), 12, "nothing was truncated")
        # ...the NEWEST survived...
        self.assertEqual(live[-1]["session"], "s11")
        # ...the file is under the cap...
        self.assertLessEqual(self.path.stat().st_size, cap)
        # ...every row still counted as written...
        self.assertEqual(ledger.rows_written, 12)
        # ...and there is exactly ONE file. A ``.1`` sibling here would mean
        # the ledger had gone back to rolling its own generations.
        self.assertEqual(
            [f.name for f in self.path.parent.iterdir()], [self.path.name],
        )

    def test_rotation_goes_through_the_shared_routine(self) -> None:
        """Wiring pinned by BEHAVIOUR, not by a source scan: the ledger must
        call the home rather than re-roll a truncation of its own."""
        row_bytes = len(self._record("s0").to_json()) + 1
        ledger = U.UsageLedger(self.path, max_bytes=row_bytes * 8)
        with mock.patch(
            "vco_lib.atomic.rotate_tail_lines", return_value=False,
        ) as rotator:
            for i in range(12):
                ledger.submit(self._record(f"s{i}"))
        self.assertTrue(rotator.called, "the shared rotator was never called")
        kwargs = rotator.call_args.kwargs
        self.assertEqual(kwargs["max_bytes"], row_bytes * 8)
        self.assertEqual(kwargs["keep_lines"], ledger._keep_lines())
        # ``in_place`` must stay False: this writer opens and closes the file
        # per append, so nothing holds a descriptor across the swap and the
        # atomic replace leaves no window in which a reader sees a half log.
        self.assertFalse(kwargs["in_place"])
        # The stubbed rotator did nothing, and every row was still written:
        # rotation failing may never cost a row.
        self.assertEqual(ledger.rows_written, 12)
        self.assertEqual(
            len(self.path.read_text(encoding="utf-8").splitlines()), 12,
        )

    def test_keep_lines_is_about_a_quarter_of_the_cap(self) -> None:
        """The documented figure, at the shipped default: 24 049 lines."""
        ledger = U.UsageLedger(self.path)
        self.assertEqual(ledger._keep_lines(), 24_049)
        surviving = ledger._keep_lines() * U.LEDGER_ROW_BYTES
        self.assertAlmostEqual(
            surviving / U.LEDGER_MAX_BYTES, 0.25, places=2,
        )

    def test_a_cap_smaller_than_one_row_still_keeps_the_newest(self) -> None:
        """``keep_lines=0`` would empty the file on every write.

        Rotation runs BEFORE the append, so the steady state under an absurd
        cap is the one kept row plus the one just written — never zero, and
        the newest is always the last line. That is the property a monitor
        depends on; the byte cap is the courtesy.
        """
        ledger = U.UsageLedger(self.path, max_bytes=10)
        self.assertEqual(ledger._keep_lines(), 1)
        for name in ("a", "b", "c"):
            ledger.submit(self._record(name))
        live = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(live), 2)
        self.assertEqual(json.loads(live[-1])["session"], "c")
        self.assertEqual(ledger.rows_written, 3)

    def test_rotation_is_skipped_loudly_when_vco_lib_is_missing(self) -> None:
        """A broken install costs the size bound, never a row — and says so
        once rather than growing a file in silence."""
        ledger = U.UsageLedger(self.path, max_bytes=10)
        real_import = __import__

        def no_atomic(name, *args, **kwargs):
            if name == "vco_lib.atomic":
                raise ImportError("no vco_lib in this venv")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", no_atomic):
            with self.assertLogs("model_router.usage", level="WARNING") as logs:
                ledger.submit(self._record("a"))
                ledger.submit(self._record("b"))
        self.assertEqual(len(logs.output), 1, "warned more than once")
        self.assertEqual(ledger.rows_written, 2)
        self.assertEqual(
            len(self.path.read_text(encoding="utf-8").splitlines()), 2,
        )

    def test_a_filter_narrows_to_one_chat(self) -> None:
        ledger = U.UsageLedger(self.path)
        ledger.submit(self._record("a"))
        ledger.submit(self._record("b"))
        self.assertEqual(list(ledger.sessions("b")), ["b"])
        self.assertEqual(ledger.sessions("nope"), {})

    def test_an_unwritable_ledger_never_raises_at_the_caller(self) -> None:
        """A full disk costs a row, never an answer."""
        ledger = U.UsageLedger(self.path)
        with mock.patch(
            "model_router.usage.open", side_effect=OSError("disk full"),
        ):
            with self.assertLogs("model_router.usage", level="ERROR"):
                ledger.submit(self._record("a"))
        self.assertEqual(ledger.rows_written, 0)

    def test_health_reports_the_path_and_the_counters(self) -> None:
        ledger = U.UsageLedger(self.path)
        self.assertEqual(ledger.health()["rows_written"], 0)
        self.assertIsNone(ledger.health()["last_write_ts"])
        ledger.submit(self._record("a"))
        health = ledger.health()
        self.assertEqual(health["path"], str(self.path))
        self.assertEqual(health["rows_written"], 1)
        self.assertTrue(health["last_write_ts"].endswith("Z"))

    def test_the_path_comes_from_the_one_metrics_home(self) -> None:
        with mock.patch.dict(os.environ, {"VCT_STATE_DIR": self.tmp.name}):
            ledger = U.UsageLedger()
            assert ledger.path is not None
            self.assertEqual(ledger.path.name, U.LEDGER_BASENAME)
            self.assertEqual(ledger.path.parent.name, "metrics")
            self.assertEqual(str(ledger.path.parent.parent), self.tmp.name)

    def test_a_broken_install_disables_the_ledger_loudly_not_silently(self) -> None:
        """``vco_lib`` missing means a broken install, so it is REPORTED — and
        it still may not take a request down, so the answer is ``None`` rather
        than an exception."""
        real_import = __import__

        def no_vco_lib(name, *args, **kwargs):
            if name.startswith("vco_lib"):
                raise ImportError("no vco_lib in this venv")
            return real_import(name, *args, **kwargs)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VCT_STATE_DIR", None)
            with mock.patch.object(U, "_METRICS_DIR_WARNED", False):
                with mock.patch("builtins.__import__", no_vco_lib):
                    with self.assertLogs("model_router.usage", level="WARNING"):
                        ledger = U.UsageLedger()
                        self.assertIsNone(ledger.path)
        # And a submit against it is a no-op, not a crash.
        ledger.submit(self._record("a"))
        self.assertEqual(ledger.rows_written, 0)

    def test_VCT_STATE_DIR_still_answers_without_vco_lib(self) -> None:
        """The degraded state is recoverable without a reinstall."""
        real_import = __import__

        def no_vco_lib(name, *args, **kwargs):
            if name.startswith("vco_lib"):
                raise ImportError("no vco_lib in this venv")
            return real_import(name, *args, **kwargs)

        with mock.patch.dict(os.environ, {"VCT_STATE_DIR": self.tmp.name}):
            with mock.patch("builtins.__import__", no_vco_lib):
                ledger = U.UsageLedger()
                assert ledger.path is not None
                self.assertEqual(
                    ledger.path, Path(self.tmp.name) / "metrics" / U.LEDGER_BASENAME,
                )


class LedgerOffTheHotPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_append_runs_on_the_executor_and_drain_waits(self) -> None:
        """The handler schedules; it does not wait for the disk."""
        tmp = tempfile.TemporaryDirectory(prefix="wp9-usage-loop-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "gateway-usage.jsonl"
        ledger = U.UsageLedger(path)
        record = U.build_record(
            session="a", agent=None, parent_agent=None,
            requested="claude-opus-5", route="anthropic",
            forward="claude-opus-5", stream=True, status=200,
            totals={name: 2 for name in U.USAGE_FIELDS},
            usage_complete=True, window_actual=200_000, window_source="table",
        )
        ledger.submit(record, loop=asyncio.get_running_loop())
        # The in-memory view is truthful IMMEDIATELY — that is what /usage
        # answers from — while the file may not have the row yet.
        self.assertEqual(list(ledger.sessions()), ["a"])
        await ledger.drain()
        self.assertEqual(ledger.rows_written, 1)
        self.assertTrue(path.is_file())


# ─────────────────────── 5. the access-line fields ───────────────────────
class AccessExtraTests(unittest.TestCase):
    def test_every_field_is_present_in_one_shape(self) -> None:
        line = U.access_extra(
            {
                "input_tokens": 10, "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 3, "output_tokens": 4,
            },
            seen=True,
        )
        self.assertEqual(line, "in=10 cache_c=2 cache_r=3 out=4 ctx=15")

    def test_a_response_that_reported_nothing_reads_as_dashes(self) -> None:
        """A zero and a silence are different observations."""
        self.assertEqual(
            U.access_extra({}, seen=False),
            "in=- cache_c=- cache_r=- out=- ctx=-",
        )


# ────────────────────── 6. count_tokens zero-guard ───────────────────────
class CountTokensEstimateTests(unittest.TestCase):
    def test_an_empty_request_has_nothing_to_count(self) -> None:
        self.assertIsNone(U.count_tokens_estimate({"model": "x"}))
        self.assertIsNone(U.count_tokens_estimate({"messages": []}))
        self.assertIsNone(U.count_tokens_estimate(None))

    def test_a_real_conversation_estimates_at_least_one_token(self) -> None:
        estimate = U.count_tokens_estimate(
            {"messages": [{"role": "user", "content": "hello there"}]},
        )
        assert estimate is not None
        self.assertGreater(estimate, 0)

    def test_the_system_prompt_counts_too(self) -> None:
        without = U.count_tokens_estimate({"messages": [{"role": "user", "content": "x"}]})
        with_system = U.count_tokens_estimate({
            "messages": [{"role": "user", "content": "x"}],
            "system": "you are a careful assistant",
        })
        assert without is not None and with_system is not None
        self.assertGreater(with_system, without)


class CountTokensGuardTests(unittest.TestCase):
    def test_a_vendor_zero_on_a_real_body_is_replaced(self) -> None:
        body, substituted = U.guard_count_tokens({"input_tokens": 0}, 17)
        self.assertTrue(substituted)
        self.assertEqual(body["input_tokens"], 17)
        self.assertEqual(body[U.COUNT_SOURCE_FIELD], U.COUNT_SOURCE_ESTIMATE)

    def test_a_real_vendor_answer_is_relayed_and_labelled(self) -> None:
        body, substituted = U.guard_count_tokens({"input_tokens": 512}, 17)
        self.assertFalse(substituted)
        self.assertEqual(body["input_tokens"], 512)
        self.assertEqual(body[U.COUNT_SOURCE_FIELD], U.COUNT_SOURCE_VENDOR)

    def test_a_zero_for_an_empty_body_is_the_right_answer(self) -> None:
        body, substituted = U.guard_count_tokens({"input_tokens": 0}, None)
        self.assertFalse(substituted)
        self.assertEqual(body["input_tokens"], 0)
        self.assertEqual(body[U.COUNT_SOURCE_FIELD], U.COUNT_SOURCE_VENDOR)

    def test_a_body_that_is_not_a_count_is_not_labelled(self) -> None:
        """The label is a claim about a number; there is no number here."""
        body, substituted = U.guard_count_tokens({"error": {"type": "x"}}, 17)
        self.assertFalse(substituted)
        self.assertNotIn(U.COUNT_SOURCE_FIELD, body)

    def test_the_warning_fires_once_per_vendor_and_model(self) -> None:
        # The say-it-once registry has ONE home since 2026-09-22
        # (model_router.catalog), so isolation goes through its supported
        # reset seam rather than by patching a module-local set that no
        # longer exists.
        from model_router.catalog import reset_log_once

        reset_log_once(U.LOG_ONCE_COUNT_SUBSTITUTION)
        self.addCleanup(reset_log_once, U.LOG_ONCE_COUNT_SUBSTITUTION)
        if True:
            self.assertTrue(U.note_count_substitution("zai", "glm-5.3"))
            self.assertFalse(U.note_count_substitution("zai", "glm-5.3"))
            self.assertTrue(U.note_count_substitution("zai", "glm-5.3-flash"))


# ─────────────────── 7. end to end, through the relay ────────────────────
class UsageThroughTheGatewayTests(GatewayTestBase):
    """Driven against the loopback stub upstreams, not a mocked client."""

    def chat(self, session: str = "chat-1", **extra: str) -> dict[str, str]:
        return {**self.auth(), U.SESSION_HEADER: session, **extra}

    async def rows(self) -> list[dict]:
        """Every row on DISK, after the queued appends have landed."""
        await self.client.app[APP_KEY].usage.drain()
        if not self.usage_ledger_path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.usage_ledger_path.read_text(
                encoding="utf-8",
            ).splitlines()
            if line
        ]

    async def test_a_vendor_stream_is_accounted_per_chat(self) -> None:
        self.vendor_up.stream_chunks = [VENDOR_STREAM]
        resp = await self.client.post(
            "/v1/messages",
            headers=self.chat("chat-A"),
            json={"model": "claude-gw/glm-5.3", "messages": [], "stream": True},
        )
        self.assertEqual(resp.status, 200)
        await resp.read()
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["session"], "chat-A")
        self.assertEqual(row["route"], "vendor:zai")
        self.assertEqual(row["forward"], "glm-5.3")
        self.assertEqual(row["input_tokens"], 2048)
        self.assertEqual(row["cache_read_input_tokens"], 64_000)
        self.assertEqual(row["output_tokens"], 99)
        self.assertEqual(row["context_tokens"], 66_048)
        self.assertEqual(row["context_after"], 66_147)
        self.assertTrue(row["usage_complete"])

    async def test_a_first_party_stream_is_accounted(self) -> None:
        self.anthropic_up.stream_chunks = [ANTHROPIC_STREAM]
        resp = await self.client.post(
            "/v1/messages",
            headers=self.chat("chat-B"),
            json={"model": "claude-opus-5", "messages": [], "stream": True},
        )
        await resp.read()
        rows = await self.rows()
        self.assertEqual(rows[-1]["input_tokens"], 1200)
        self.assertEqual(rows[-1]["output_tokens"], 450)
        self.assertEqual(rows[-1]["route"], "anthropic")

    async def test_two_chats_and_a_subagent_are_kept_apart(self) -> None:
        """The owner's requirement in one test: each chat gets its own
        monitoring, subagents included."""
        self.anthropic_up.stream_chunks = [ANTHROPIC_STREAM]
        for session, agent in (
            ("chat-A", None), ("chat-B", None), ("chat-A", "sub-1"),
        ):
            headers = self.chat(session)
            if agent is not None:
                headers[U.AGENT_HEADER] = agent
                headers[U.PARENT_AGENT_HEADER] = "root"
            resp = await self.client.post(
                "/v1/messages", headers=headers,
                json={"model": "claude-opus-5", "messages": [], "stream": True},
            )
            await resp.read()
        body = await (await self.client.get("/usage", headers=self.auth())).json()
        self.assertEqual(sorted(body["sessions"]), ["chat-A", "chat-B"])
        # chat-A's newest row is the SUBAGENT's — the subagent's tokens are
        # part of that chat's context, and its own ids are recorded beside it.
        self.assertEqual(body["sessions"]["chat-A"]["agent"], "sub-1")
        self.assertEqual(body["sessions"]["chat-A"]["parent_agent"], "root")
        self.assertIsNone(body["sessions"]["chat-B"]["agent"])

    async def test_a_request_without_a_session_header_is_filed_not_invented(self) -> None:
        self.anthropic_up.stream_chunks = [ANTHROPIC_STREAM]
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-opus-5", "messages": [], "stream": True},
        )
        await resp.read()
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["session"])
        body = await (await self.client.get("/usage", headers=self.auth())).json()
        self.assertEqual(body["sessions"], {})

    async def test_a_non_streamed_vendor_answer_is_accounted(self) -> None:
        self.vendor_up.messages_body = {
            "id": "msg_1", "type": "message", "content": [],
            "usage": {
                "input_tokens": 77, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "output_tokens": 5,
            },
        }
        resp = await self.client.post(
            "/v1/messages", headers=self.chat("chat-J"),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 200)
        rows = await self.rows()
        self.assertEqual(rows[-1]["input_tokens"], 77)
        self.assertFalse(rows[-1]["stream"])

    async def test_a_non_streamed_first_party_answer_is_accounted(self) -> None:
        self.anthropic_up.messages_body = {
            "id": "msg_1", "type": "message", "content": [],
            "usage": {
                "input_tokens": 9, "cache_creation_input_tokens": 1,
                "cache_read_input_tokens": 2, "output_tokens": 3,
            },
        }
        resp = await self.client.post(
            "/v1/messages", headers=self.chat("chat-K"),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(resp.status, 200)
        rows = await self.rows()
        self.assertEqual(rows[-1]["context_tokens"], 12)
        self.assertEqual(rows[-1]["context_after"], 15)

    async def test_the_1m_spelling_changes_the_client_window_not_the_tokens(self) -> None:
        self.vendor_up.stream_chunks = [VENDOR_STREAM]
        resp = await self.client.post(
            "/v1/messages", headers=self.chat("chat-M"),
            json={"model": "claude-gw/glm-5.3[1m]", "messages": [], "stream": True},
        )
        await resp.read()
        row = (await self.rows())[-1]
        self.assertEqual(row["requested"], "claude-gw/glm-5.3[1m]")
        self.assertEqual(row["window_client"], 1_000_000)
        self.assertEqual(row["input_tokens"], 2048)

    async def test_the_actual_window_comes_from_the_catalog_resolver(self) -> None:
        """A table row answers; a model nobody tabulated reads unknown rather
        than being guessed at on the request path."""
        self.anthropic_up.stream_chunks = [ANTHROPIC_STREAM]
        resp = await self.client.post(
            "/v1/messages", headers=self.chat("chat-W"),
            json={"model": "claude-opus-5", "messages": [], "stream": True},
        )
        await resp.read()
        row = (await self.rows())[-1]
        self.assertEqual(row["window_source"], "table")
        self.assertIsNotNone(row["window_actual"])
        self.assertIsNotNone(row["pct_actual"])

        self.anthropic_up.stream_chunks = [ANTHROPIC_STREAM]
        resp = await self.client.post(
            "/v1/messages", headers=self.chat("chat-X"),
            json={"model": "claude-not-a-real-model-9", "messages": [],
                  "stream": True},
        )
        await resp.read()
        row = (await self.rows())[-1]
        self.assertEqual(row["window_source"], "unknown")
        self.assertIsNone(row["window_actual"])
        self.assertIsNone(row["pct_actual"])

    async def test_a_truncated_stream_still_writes_what_it_saw(self) -> None:
        """The tokens were spent whether or not the answer arrived."""
        cut = ANTHROPIC_STREAM[: ANTHROPIC_STREAM.index(b"message_delta") - 20]
        self.anthropic_up.stream_chunks = [cut]
        resp = await self.client.post(
            "/v1/messages", headers=self.chat("chat-T"),
            json={"model": "claude-opus-5", "messages": [], "stream": True},
        )
        await resp.read()
        row = (await self.rows())[-1]
        self.assertFalse(row["usage_complete"])
        self.assertEqual(row["input_tokens"], 1200)

    async def test_a_response_reporting_no_usage_writes_no_row(self) -> None:
        """A row of zeros in a context monitor reads as "this chat is empty"."""
        self.anthropic_up.messages_body = {"id": "m", "content": []}
        resp = await self.client.post(
            "/v1/messages", headers=self.chat("chat-Z"),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(await self.rows(), [])

    async def test_a_relayed_error_is_logged_and_not_accounted(self) -> None:
        self.vendor_up.messages_status = 400
        self.vendor_up.messages_body = {"type": "error", "error": {}}
        with self.assertLogs("model_router.server", level="INFO") as logs:
            resp = await self.client.post(
                "/v1/messages", headers=self.chat("chat-E"),
                json={"model": "claude-gw/glm-5.3", "messages": []},
            )
        self.assertEqual(resp.status, 400)
        self.assertEqual(await self.rows(), [])
        self.assertTrue(
            any("status=400" in line and "in=-" in line for line in logs.output),
            logs.output,
        )

    async def test_count_tokens_is_never_accounted(self) -> None:
        """It is a question about a conversation, not a turn in one."""
        self.vendor_up.messages_body = {"input_tokens": 1234}
        resp = await self.client.post(
            COUNT_TOKENS_PATH, headers=self.chat("chat-C"),
            json={"model": "claude-gw/glm-5.3",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(await self.rows(), [])

    async def test_the_access_line_carries_the_usage_fields(self) -> None:
        self.vendor_up.stream_chunks = [VENDOR_STREAM]
        with self.assertLogs("model_router.server", level="INFO") as logs:
            resp = await self.client.post(
                "/v1/messages", headers=self.chat(),
                json={"model": "claude-gw/glm-5.3", "messages": [],
                      "stream": True},
            )
            await resp.read()
        relayed = [line for line in logs.output if "status=200" in line]
        self.assertTrue(relayed, logs.output)
        self.assertIn(
            "in=2048 cache_c=0 cache_r=64000 out=99 ctx=66048", relayed[-1],
        )


class RecordGateTests(GatewayTestBase):
    """The three cases :func:`_submit_usage` DECLINES, driven directly.

    The end-to-end tests above cannot pin these. The relay declines twice for
    the same request — it does not even build an accumulator for a
    ``count_tokens`` call, and the record gate declines it again — so removing
    either guard alone leaves those tests green. A guard whose removal nothing
    notices is not a guard, so the gate is driven on its own, both sides.
    """

    def _facts(self, **over: Any) -> RequestFacts:
        fields: dict[str, Any] = dict(
            session="chat-G", agent=None, parent_agent=None,
            count_tokens=False, count_estimate=None,
        )
        fields.update(over)
        return RequestFacts(**fields)

    def _decision(self) -> Route:
        return Route(
            upstream="http://127.0.0.1:1",
            forward_model="claude-opus-5",
            vendor=None,
            family_id="anthropic",
            is_anthropic=True,
        )

    async def _submit(self, facts: RequestFacts | None, status: int = 200) -> int:
        gateway = self.client.app[APP_KEY]
        before = gateway.usage.rows_written
        _submit_usage(
            gateway,
            self._decision(),
            facts,
            requested="claude-opus-5",
            route="anthropic",
            status=status,
            stream=True,
            totals={name: 5 for name in U.USAGE_FIELDS},
            usage_complete=True,
        )
        await gateway.usage.drain()
        return gateway.usage.rows_written - before

    async def test_an_ordinary_2xx_turn_is_recorded(self) -> None:
        """The ACT half: the three declines below are not "record nothing"."""
        self.assertEqual(await self._submit(self._facts()), 1)

    async def test_a_count_tokens_call_is_declined(self) -> None:
        self.assertEqual(await self._submit(self._facts(count_tokens=True)), 0)

    async def test_a_non_2xx_is_declined(self) -> None:
        self.assertEqual(await self._submit(self._facts(), status=429), 0)

    async def test_a_request_with_no_facts_is_declined(self) -> None:
        """An internal caller with no client request accounts nothing."""
        self.assertEqual(await self._submit(None), 0)


class RelayIsUntouchedTests(GatewayTestBase):
    async def test_the_relayed_bytes_are_byte_identical_with_accounting_on(self) -> None:
        """The accumulator takes a COPY: the client's stream is the one it
        would have received if this module did not exist."""
        self.anthropic_up.stream_chunks = [ANTHROPIC_STREAM]
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-opus-5", "messages": [], "stream": True},
        )
        self.assertEqual(await resp.read(), ANTHROPIC_STREAM)

    async def test_an_untouched_vendor_json_body_is_still_relayed_verbatim(self) -> None:
        raw = b'{"id":"msg_1","content":[],"usage":{"input_tokens":3}}'
        self.vendor_up.messages_raw = raw
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(await resp.read(), raw)

    async def test_an_accumulator_that_raises_does_not_break_the_stream(self) -> None:
        """Guarded like every other pass: the answer the client gets is the
        one it would have got if the accounting had never existed."""
        self.anthropic_up.stream_chunks = [ANTHROPIC_STREAM]
        with mock.patch.object(
            U.UsageAccumulator, "feed", side_effect=RuntimeError("boom"),
        ):
            with self.assertLogs("model_router.server", level="ERROR"):
                resp = await self.client.post(
                    "/v1/messages", headers=self.chat_headers(),
                    json={"model": "claude-opus-5", "messages": [],
                          "stream": True},
                )
                body = await resp.read()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, ANTHROPIC_STREAM)

    async def test_a_close_that_raises_does_not_break_the_response(self) -> None:
        self.anthropic_up.messages_body = {
            "usage": {"input_tokens": 4, "output_tokens": 1},
        }
        with mock.patch.object(
            U.UsageAccumulator, "close", side_effect=RuntimeError("boom"),
        ):
            with self.assertLogs("model_router.server", level="ERROR"):
                resp = await self.client.post(
                    "/v1/messages", headers=self.chat_headers(),
                    json={"model": "claude-opus-5", "messages": []},
                )
        self.assertEqual(resp.status, 200)

    def chat_headers(self) -> dict[str, str]:
        return {**self.auth(), U.SESSION_HEADER: "chat-guard"}


class CountTokensThroughTheGatewayTests(GatewayTestBase):
    async def _count(self, body: dict, **payload: Any) -> dict:
        self.vendor_up.messages_body = body
        resp = await self.client.post(
            COUNT_TOKENS_PATH,
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", **payload},
        )
        self.assertEqual(resp.status, 200)
        return await resp.json()

    async def test_a_vendor_zero_becomes_the_gateway_estimate(self) -> None:
        answered = await self._count(
            {"input_tokens": 0},
            messages=[{"role": "user", "content": "how many tokens is this?"}],
        )
        self.assertGreater(answered["input_tokens"], 0)
        self.assertEqual(
            answered[U.COUNT_SOURCE_FIELD], U.COUNT_SOURCE_ESTIMATE,
        )

    async def test_a_real_vendor_count_is_relayed_and_labelled(self) -> None:
        answered = await self._count(
            {"input_tokens": 17},
            messages=[{"role": "user", "content": "how many tokens is this?"}],
        )
        self.assertEqual(answered["input_tokens"], 17)
        self.assertEqual(answered[U.COUNT_SOURCE_FIELD], U.COUNT_SOURCE_VENDOR)

    async def test_a_zero_for_an_empty_body_is_relayed_as_zero(self) -> None:
        answered = await self._count({"input_tokens": 0}, messages=[])
        self.assertEqual(answered["input_tokens"], 0)
        self.assertEqual(answered[U.COUNT_SOURCE_FIELD], U.COUNT_SOURCE_VENDOR)

    async def test_the_first_party_route_is_relayed_untouched(self) -> None:
        """Decorating an Anthropic response with a field it never sent is the
        "worse than native" this gateway exists not to be."""
        self.anthropic_up.messages_body = {"input_tokens": 0}
        resp = await self.client.post(
            COUNT_TOKENS_PATH,
            headers=self.auth(),
            json={"model": "claude-opus-5",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        answered = await resp.json()
        self.assertEqual(answered, {"input_tokens": 0})
        self.assertNotIn(U.COUNT_SOURCE_FIELD, answered)

    async def test_the_substitution_is_logged_once_per_model(self) -> None:
        # The say-it-once registry has ONE home since 2026-09-22
        # (model_router.catalog), so isolation goes through its supported
        # reset seam rather than by patching a module-local set that no
        # longer exists.
        from model_router.catalog import reset_log_once

        reset_log_once(U.LOG_ONCE_COUNT_SUBSTITUTION)
        self.addCleanup(reset_log_once, U.LOG_ONCE_COUNT_SUBSTITUTION)
        if True:
            with self.assertLogs("model_router.usage", level="WARNING") as logs:
                await self._count(
                    {"input_tokens": 0},
                    messages=[{"role": "user", "content": "one"}],
                )
                await self._count(
                    {"input_tokens": 0},
                    messages=[{"role": "user", "content": "two"}],
                )
        self.assertEqual(len(logs.output), 1, logs.output)


# ───────────────────────── 8. the /usage route ───────────────────────────
class UsageRouteTests(GatewayTestBase):
    async def test_it_needs_the_host_token(self) -> None:
        resp = await self.client.get("/usage")
        self.assertEqual(resp.status, 401)

    async def test_it_reports_the_ledger_path_and_the_row_count(self) -> None:
        self.anthropic_up.stream_chunks = [ANTHROPIC_STREAM]
        resp = await self.client.post(
            "/v1/messages",
            headers={**self.auth(), U.SESSION_HEADER: "chat-1"},
            json={"model": "claude-opus-5", "messages": [], "stream": True},
        )
        await resp.read()
        await self.client.app[APP_KEY].usage.drain()
        body = await (await self.client.get("/usage", headers=self.auth())).json()
        self.assertEqual(set(body), {"sessions", "ledger_path", "rows_written"})
        self.assertEqual(body["rows_written"], 1)
        self.assertEqual(body["ledger_path"], str(self.usage_ledger_path))
        self.assertEqual(body["sessions"]["chat-1"]["output_tokens"], 450)

    async def test_the_session_query_narrows_to_one_chat(self) -> None:
        self.anthropic_up.stream_chunks = [ANTHROPIC_STREAM]
        for session in ("chat-1", "chat-2"):
            resp = await self.client.post(
                "/v1/messages",
                headers={**self.auth(), U.SESSION_HEADER: session},
                json={"model": "claude-opus-5", "messages": [], "stream": True},
            )
            await resp.read()
        both = await (await self.client.get("/usage", headers=self.auth())).json()
        self.assertEqual(sorted(both["sessions"]), ["chat-1", "chat-2"])
        one = await (
            await self.client.get("/usage?session=chat-2", headers=self.auth())
        ).json()
        self.assertEqual(list(one["sessions"]), ["chat-2"])

    async def test_it_is_served_with_a_trailing_slash(self) -> None:
        resp = await self.client.get("/usage/", headers=self.auth())
        self.assertEqual(resp.status, 200)

    async def test_it_is_named_in_the_404_hint(self) -> None:
        body = await (
            await self.client.get("/v1/nonsense", headers=self.auth())
        ).json()
        self.assertIn("/usage", body["error"]["message"])

    async def test_health_carries_the_ledger_block(self) -> None:
        body = await (await self.client.get("/health")).json()
        self.assertEqual(
            set(body["usage_ledger"]), {"path", "rows_written", "last_write_ts"},
        )
        self.assertEqual(body["usage_ledger"]["path"], str(self.usage_ledger_path))
        self.assertEqual(body["usage_ledger"]["rows_written"], 0)
        self.assertIsNone(body["usage_ledger"]["last_write_ts"])

    async def test_health_ledger_counters_move_with_the_rows(self) -> None:
        self.anthropic_up.stream_chunks = [ANTHROPIC_STREAM]
        resp = await self.client.post(
            "/v1/messages",
            headers={**self.auth(), U.SESSION_HEADER: "chat-1"},
            json={"model": "claude-opus-5", "messages": [], "stream": True},
        )
        await resp.read()
        await self.client.app[APP_KEY].usage.drain()
        body = await (await self.client.get("/health")).json()
        self.assertEqual(body["usage_ledger"]["rows_written"], 1)
        self.assertTrue(body["usage_ledger"]["last_write_ts"].endswith("Z"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
