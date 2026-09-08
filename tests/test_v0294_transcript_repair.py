# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The deterministic tool-block rewrite, and ``vco fix-transcript`` on top of it.

Every expectation in here is anchored to a LIVE probe against
``api.anthropic.com`` (2026-09-08, through the gateway, ``count_tokens`` on
``claude-haiku-4-5-20251001``) rather than to a reading of the docs:

* ``server_tool_use.id = "call_abc123"`` -> 400, pattern ``^srvtoolu_``;
* the same block with ``id = "srvtoolu_vct_call_abc123"`` and ``name =
  "web_search"`` -> **200**, so normalising the id is sufficient for a name
  Anthropic knows;
* ``name = "analyze_image"`` with an already-conforming id -> 400 on the NAME,
  which is why a non-portable block has to be STRIPPED and cannot be rescued;
* an orphan ``web_search_tool_result`` -> 400, which is why the result goes
  with the block it references;
* a plain ``tool_use`` id of ``call_…`` -> 200, which is why rewriting those
  is about consistency and not survival.

The file-level tests use fixtures shaped like the real session ``.jsonl``
(``type`` / ``message.content`` / ``uuid`` / ``parentUuid``) and NEVER touch a
real session.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from vco_lib import transcript_repair as tr
from vco_lib.cli import fix_transcript as ft


def _stu(block_id: str, name: str = "analyze_image") -> dict:
    return {"type": "server_tool_use", "id": block_id, "name": name,
            "input": {"image": "x"}}


def _stu_result(ref: str, kind: str = "web_search_tool_result") -> dict:
    return {"type": kind, "tool_use_id": ref, "content": []}


class IdShapeTests(unittest.TestCase):
    def test_sanitise_keeps_word_characters_and_collapses_the_rest(self) -> None:
        self.assertEqual(tr.sanitise_id_body("call_5f46-ab.cd"), "call_5f46_ab_cd")

    def test_only_server_tool_ids_are_rewritten(self) -> None:
        """A plain ``tool_use`` id is left alone — Anthropic accepts it."""
        repairer = tr.TranscriptRepairer()
        blocks, changed = repairer.repair_content(
            [{"type": "tool_use", "id": "call_9a.b", "name": "R", "input": {}}],
        )
        self.assertFalse(changed)
        self.assertEqual(blocks[0]["id"], "call_9a.b")

    def test_normalise_is_idempotent(self) -> None:
        once = tr.normalise_id("call_abc123", tr.SERVER_TOOL_ID_PREFIX)
        self.assertEqual(once, "srvtoolu_vct_call_abc123")
        self.assertEqual(tr.normalise_id(once, tr.SERVER_TOOL_ID_PREFIX), once)

    def test_normalise_does_not_double_prefix_a_dirty_conforming_id(self) -> None:
        self.assertEqual(
            tr.normalise_id("srvtoolu_a.b", tr.SERVER_TOOL_ID_PREFIX),
            "srvtoolu_vct_a_b",
        )

    def test_our_output_carries_the_marker_and_still_conforms(self) -> None:
        """The marker is what makes "did WE write this id?" decidable, and it
        has to cost nothing: it lives inside Anthropic's own charset."""
        out = tr.normalise_id("call_abc", tr.SERVER_TOOL_ID_PREFIX)
        self.assertTrue(
            out.startswith(f"{tr.SERVER_TOOL_ID_PREFIX}{tr.GATEWAY_ID_MARKER}"),
        )
        self.assertTrue(tr.id_conforms(out, tr.SERVER_TOOL_ID_PREFIX))
        self.assertFalse(tr.minted_by_anthropic(out))

    def test_the_marker_is_inserted_after_any_prefix(self) -> None:
        """``normalise_id`` takes the prefix as an argument; the marker
        follows whichever one it is given."""
        self.assertEqual(
            tr.normalise_id("call_abc", "toolu_"), "toolu_vct_call_abc",
        )
        self.assertFalse(tr.minted_by_anthropic("mcptoolu_vct_x"))

    def test_a_vendor_id_that_merely_begins_with_the_marker_is_distinct(
        self,
    ) -> None:
        """Idempotence is keyed on PREFIX + marker together, so ``vct_foo``
        and ``foo`` cannot collide into one entry of the reverse map."""
        first = tr.normalise_id("vct_foo", tr.SERVER_TOOL_ID_PREFIX)
        second = tr.normalise_id("foo", tr.SERVER_TOOL_ID_PREFIX)
        self.assertNotEqual(first, second)
        self.assertEqual(tr.normalise_id(first, tr.SERVER_TOOL_ID_PREFIX), first)

    def test_minted_by_anthropic_separates_theirs_from_ours(self) -> None:
        for theirs in ("srvtoolu_01AbCdEf", "mcptoolu_01Xyz"):
            with self.subTest(theirs=theirs):
                self.assertTrue(tr.minted_by_anthropic(theirs))
        for ours in ("srvtoolu_vct_call_abc", "mcptoolu_vct_x", "toolu_vct_x"):
            with self.subTest(ours=ours):
                self.assertFalse(tr.minted_by_anthropic(ours))
        for neither in ("call_abc", "", None, 7):
            with self.subTest(neither=neither):
                self.assertFalse(tr.minted_by_anthropic(neither))

    def test_a_degenerate_id_still_satisfies_the_pattern(self) -> None:
        """``[a-zA-Z0-9_]+`` needs at least one character; ``""`` has none.

        Punctuation-only ids DO collide (``...`` and ``///`` both sanitise to
        ``___``) — an unavoidable consequence of "collapse to underscore",
        and harmless: the reverse map, not the id, is what carries the way
        back to the vendor's exact spelling.
        """
        for raw in ("", "...", "///"):
            with self.subTest(raw=raw):
                out = tr.normalise_id(raw, tr.SERVER_TOOL_ID_PREFIX)
                self.assertTrue(tr.id_conforms(out, tr.SERVER_TOOL_ID_PREFIX))
        self.assertTrue(
            tr.normalise_id("", tr.SERVER_TOOL_ID_PREFIX).startswith(
                f"{tr.SERVER_TOOL_ID_PREFIX}{tr.GATEWAY_ID_MARKER}x",
            ),
        )

    def test_conformance_matches_anthropics_pattern(self) -> None:
        self.assertTrue(tr.id_conforms("srvtoolu_vct_a1_B2", tr.SERVER_TOOL_ID_PREFIX))
        for bad in ("call_abc", "srvtoolu_", "srvtoolu_a-b", "SRVTOOLU_a"):
            with self.subTest(bad=bad):
                self.assertFalse(tr.id_conforms(bad, tr.SERVER_TOOL_ID_PREFIX))

    def test_the_portable_name_set_is_anthropics_own_list(self) -> None:
        """Verbatim from the validator's 400. Not a guess, not a superset."""
        self.assertEqual(
            tr.PORTABLE_SERVER_TOOL_NAMES,
            frozenset(
                {
                    "web_search",
                    "web_fetch",
                    "code_execution",
                    "bash_code_execution",
                    "text_editor_code_execution",
                    "tool_search_tool_regex",
                    "tool_search_tool_bm25",
                }
            ),
        )

    def test_a_client_tool_result_is_not_a_server_tool_result(self) -> None:
        self.assertFalse(tr.is_server_tool_result_type("tool_result"))
        self.assertTrue(tr.is_server_tool_result_type("web_search_tool_result"))
        self.assertTrue(tr.is_server_tool_result_type("analyze_image_tool_result"))


class BlockRewriteTests(unittest.TestCase):
    def test_a_portable_block_keeps_its_place_with_a_conforming_id(self) -> None:
        repairer = tr.TranscriptRepairer()
        blocks, changed = repairer.repair_content(
            [
                {"type": "text", "text": "searching"},
                _stu("call_abc123", "web_search"),
                _stu_result("call_abc123"),
            ]
        )
        self.assertTrue(changed)
        self.assertEqual([b["type"] for b in blocks],
                         ["text", "server_tool_use", "web_search_tool_result"])
        self.assertEqual(blocks[1]["id"], "srvtoolu_vct_call_abc123")
        self.assertEqual(blocks[2]["tool_use_id"], "srvtoolu_vct_call_abc123")
        self.assertEqual(repairer.stats.blocks_stripped, 0)

    def test_a_non_portable_block_and_its_result_are_stripped(self) -> None:
        """``analyze_image`` 400s on the NAME even with a perfect id."""
        repairer = tr.TranscriptRepairer()
        blocks, changed = repairer.repair_content(
            [
                {"type": "text", "text": "looking at it"},
                _stu("call_5f460651ce3144e4ac3132bf", "analyze_image"),
                _stu_result("call_5f460651ce3144e4ac3132bf"),
            ]
        )
        self.assertTrue(changed)
        self.assertEqual([b["type"] for b in blocks], ["text"])
        self.assertEqual(repairer.stats.blocks_stripped, 1)
        self.assertEqual(repairer.stats.results_stripped, 1)
        self.assertEqual(repairer.stats.stripped_names, {"analyze_image"})

    def test_a_vendor_result_block_of_an_unknown_type_is_stripped(self) -> None:
        """The id is not the only thing the validator checks.

        A vendor result block whose producer is not in this transcript used
        to be returned untouched — but Anthropic knows a closed set of result
        TYPES and rejects the rest, so it went on poisoning the session in
        exactly the way the id rewrite was written to stop.
        """
        repairer = tr.TranscriptRepairer()
        blocks, changed = repairer.repair_content(
            [
                {"type": "text", "text": "x"},
                _stu_result("call_5f460651ce", "analyze_image_tool_result"),
            ]
        )
        self.assertTrue(changed)
        self.assertEqual([b["type"] for b in blocks], ["text"])
        self.assertEqual(repairer.stats.results_stripped, 1)
        self.assertIn("analyze_image_tool_result", repairer.stats.stripped_names)

    def test_a_vendor_block_we_already_normalised_is_still_strippable(
        self,
    ) -> None:
        """The residual the marker closes.

        Before the marker, an id WE wrote (``srvtoolu_call_…``) was
        byte-identical to one Anthropic minted, so a vendor result block
        whose id an earlier pass had normalised — and whose producer is gone
        — read as first-party and was kept, 400 and all. The marker makes
        the two decidable.
        """
        repairer = tr.TranscriptRepairer()
        blocks, changed = repairer.repair_content(
            [
                {"type": "text", "text": "x"},
                _stu_result(
                    "srvtoolu_vct_call_5f46", "analyze_image_tool_result",
                ),
            ]
        )
        self.assertTrue(changed)
        self.assertEqual([b["type"] for b in blocks], ["text"])
        self.assertEqual(repairer.stats.results_stripped, 1)

    def test_our_own_orphan_result_goes_too(self) -> None:
        """Same residual, the other gate: a KNOWN type, our id, no producer."""
        repairer = tr.TranscriptRepairer()
        blocks, changed = repairer.repair_content(
            [_stu_result("srvtoolu_vct_call_gone", "web_search_tool_result")]
        )
        self.assertTrue(changed)
        self.assertEqual(blocks, [])
        self.assertEqual(repairer.stats.results_stripped, 1)

    def test_an_already_repaired_pair_survives_a_second_pass(self) -> None:
        """And the trap the marker would have set if "ours" alone decided.

        A transcript this module already repaired carries our ids on BOTH
        blocks. "Not Anthropic-minted ⇒ orphan" would strip the result of a
        pair that is perfectly consistent — on every later request, because
        ``sanitise_for_anthropic`` runs on all of them. Membership in the
        producers seen so far is the test, not the id's shape.
        """
        repairer = tr.TranscriptRepairer()
        original = [
            _stu("srvtoolu_vct_call_abc", "web_search"),
            _stu_result("srvtoolu_vct_call_abc", "web_search_tool_result"),
        ]
        blocks, changed = repairer.repair_content(original)
        self.assertFalse(changed, repairer.stats.summary())
        self.assertIs(blocks, original)

    def test_a_result_before_its_producer_is_an_orphan(self) -> None:
        """Anthropic's wording is "before it", and the producer set is filled
        in block order, so ordering is checked by construction."""
        repairer = tr.TranscriptRepairer()
        blocks, changed = repairer.repair_content(
            [
                _stu_result("srvtoolu_vct_call_late", "web_search_tool_result"),
                _stu("srvtoolu_vct_call_late", "web_search"),
            ]
        )
        self.assertTrue(changed)
        self.assertEqual([b["type"] for b in blocks], ["server_tool_use"])

    def test_an_unknown_type_with_an_anthropic_minted_id_is_kept(self) -> None:
        """The asymmetry, deliberately.

        A ``srvtoolu_``/``mcptoolu_`` id says Anthropic's own side produced
        this. An unfamiliar type beside one means the table below is out of
        date — a server tool shipped after it was written — and removing it
        would break a working transcript in the way this module prevents.
        """
        repairer = tr.TranscriptRepairer()
        original = [_stu_result("srvtoolu_01AbC", "some_future_tool_result")]
        blocks, changed = repairer.repair_content(original)
        self.assertFalse(changed)
        self.assertIs(blocks, original)

    def test_every_portable_result_type_survives(self) -> None:
        """LEAVE-ALONE half, across the whole table."""
        for kind in sorted(tr.PORTABLE_SERVER_TOOL_RESULT_TYPES):
            with self.subTest(kind=kind):
                repairer = tr.TranscriptRepairer()
                ref = (
                    "mcptoolu_native1"
                    if kind == "mcp_tool_result"
                    else "srvtoolu_native1"
                )
                original = [_stu_result(ref, kind)]
                blocks, changed = repairer.repair_content(original)
                self.assertFalse(changed)
                self.assertIs(blocks, original)

    def test_the_result_types_are_a_cited_table_not_a_derivation(self) -> None:
        """``f"{name}_tool_result"`` invented two types and missed two.

        Both tool-search tools report through ONE ``tool_search_tool_result``,
        and the MCP connector's ``mcp_tool_result`` is not derived from any
        server-tool NAME at all — so the derivation stripped real Anthropic
        blocks out of healthy transcripts on every first-party request.
        """
        self.assertIn("tool_search_tool_result", tr.PORTABLE_SERVER_TOOL_RESULT_TYPES)
        self.assertIn("mcp_tool_result", tr.PORTABLE_SERVER_TOOL_RESULT_TYPES)
        for invented in (
            "tool_search_tool_regex_tool_result",
            "tool_search_tool_bm25_tool_result",
        ):
            with self.subTest(invented=invented):
                self.assertNotIn(invented, tr.PORTABLE_SERVER_TOOL_RESULT_TYPES)
        self.assertNotIn(
            "analyze_image_tool_result", tr.PORTABLE_SERVER_TOOL_RESULT_TYPES,
        )
        # Every accepted producer NAME maps to a result type, so a name can
        # never be accepted while its result is unrepresentable.
        for name in tr.PORTABLE_SERVER_TOOL_NAMES:
            with self.subTest(name=name):
                self.assertIn(name, tr.SERVER_TOOL_RESULT_TYPE_BY_NAME)
        self.assertEqual(
            tr.PORTABLE_SERVER_TOOL_RESULT_TYPES,
            frozenset(tr.SERVER_TOOL_RESULT_TYPE_BY_NAME.values()),
        )

    def test_a_normalised_tool_search_result_keeps_its_place(self) -> None:
        """Where the TABLE does the work rather than the minted-id gate.

        A vendor implementing Anthropic's tool-search names hands us
        ``call_…`` ids: the producer's id is rewritten, so its result's
        reference is not minted and the type check is what decides. The
        derived spelling (``tool_search_tool_regex_tool_result``) is not the
        one Anthropic uses, so the derivation stripped the result and left
        the producer dangling — a 400 manufactured by the repair.
        """
        repairer = tr.TranscriptRepairer()
        blocks, changed = repairer.repair_content(
            [
                _stu("call_ts1", "tool_search_tool_regex"),
                _stu_result("call_ts1", "tool_search_tool_result"),
            ]
        )
        self.assertTrue(changed)
        self.assertEqual(
            [b["type"] for b in blocks],
            ["server_tool_use", "tool_search_tool_result"],
        )
        self.assertEqual(blocks[0]["id"], "srvtoolu_vct_call_ts1")
        self.assertEqual(blocks[1]["tool_use_id"], "srvtoolu_vct_call_ts1")
        self.assertEqual(repairer.stats.results_stripped, 0)

    def test_the_mcp_connector_pair_survives(self) -> None:
        """``mcp_tool_use`` / ``mcp_tool_result`` with ``mcptoolu_`` ids are
        first-party traffic and must pass through untouched — the narrower
        ``srvtoolu_``-only orphan rule was stripping the result."""
        repairer = tr.TranscriptRepairer()
        original = [
            {"type": "mcp_tool_use", "id": "mcptoolu_01Xyz",
             "name": "echo", "server_name": "demo", "input": {}},
            {"type": "mcp_tool_result", "tool_use_id": "mcptoolu_01Xyz",
             "is_error": False, "content": [{"type": "text", "text": "ok"}]},
        ]
        blocks, changed = repairer.repair_content(original, role="assistant")
        self.assertFalse(changed, repairer.stats.summary())
        self.assertIs(blocks, original)

    def test_a_vendor_bound_payload_keeps_its_own_result_types(self) -> None:
        """``strip_nonportable=False`` is the vendor direction: the vendor's
        own tool names and result types are perfectly valid there."""
        repairer = tr.TranscriptRepairer(strip_nonportable=False)
        original = [
            _stu_result("srvtoolu_conforming1", "analyze_image_tool_result"),
        ]
        blocks, changed = repairer.repair_content(original)
        self.assertFalse(changed)
        self.assertIs(blocks, original)

    def test_an_orphan_server_tool_result_is_stripped(self) -> None:
        repairer = tr.TranscriptRepairer()
        blocks, changed = repairer.repair_content(
            [{"type": "text", "text": "x"}, _stu_result("call_nothing")]
        )
        self.assertTrue(changed)
        self.assertEqual([b["type"] for b in blocks], ["text"])
        self.assertEqual(repairer.stats.results_stripped, 1)

    def test_an_anthropic_native_result_is_left_alone(self) -> None:
        """Conforming and unmapped = somebody else's valid content."""
        repairer = tr.TranscriptRepairer()
        original = [_stu_result("srvtoolu_native1")]
        blocks, changed = repairer.repair_content(original)
        self.assertFalse(changed)
        self.assertIs(blocks, original)

    def test_a_server_tool_and_its_user_result_stay_consistent(self) -> None:
        repairer = tr.TranscriptRepairer()
        messages, changed = repairer.repair_messages(
            [
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": [
                    {"type": "server_tool_use", "id": "call_9a.b",
                     "name": "web_search", "input": {}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "call_9a.b",
                     "content": "ok"},
                ]},
            ]
        )
        self.assertTrue(changed)
        new_id = messages[1]["content"][0]["id"]
        self.assertEqual(new_id, "srvtoolu_vct_call_9a_b")
        self.assertEqual(messages[2]["content"][0]["tool_use_id"], new_id)
        self.assertEqual(repairer.stats.touched_indexes, [1, 2])

    def test_an_assistant_tool_result_is_stripped(self) -> None:
        """Live 400: ``tool_result`` blocks can only be in ``user`` messages —
        so an assistant-side one is unrepresentable whatever its id."""
        repairer = tr.TranscriptRepairer()
        messages, changed = repairer.repair_messages(
            [
                {"role": "assistant", "content": [
                    {"type": "text", "text": "I see"},
                    {"type": "tool_result", "tool_use_id": "call_x",
                     "content": "a cat"},
                ]},
            ]
        )
        self.assertTrue(changed)
        self.assertEqual(
            [b["type"] for b in messages[0]["content"]], ["text"],
        )
        self.assertEqual(repairer.stats.results_stripped, 1)

    def test_a_user_tool_result_for_a_dropped_block_is_stripped(self) -> None:
        repairer = tr.TranscriptRepairer()
        messages, changed = repairer.repair_messages(
            [
                {"role": "assistant", "content": [_stu("call_5f46")]},
                {"role": "user", "content": [
                    {"type": "text", "text": "next"},
                    {"type": "tool_result", "tool_use_id": "call_5f46",
                     "content": "a cat"},
                ]},
            ]
        )
        self.assertTrue(changed)
        self.assertEqual(
            [b["type"] for b in messages[-1]["content"]], ["text"],
        )

    def test_an_emptied_message_is_dropped_in_flight(self) -> None:
        repairer = tr.TranscriptRepairer()
        messages, changed = repairer.repair_messages(
            [
                {"role": "user", "content": "look"},
                {"role": "assistant", "content": [
                    _stu("call_5f46"),
                    {"type": "tool_result", "tool_use_id": "call_5f46",
                     "content": "a cat"},
                ]},
                {"role": "user", "content": "and?"},
            ]
        )
        self.assertTrue(changed)
        self.assertEqual([m["role"] for m in messages], ["user", "user"])
        self.assertEqual(repairer.stats.messages_dropped, 1)

    def test_an_emptied_message_can_be_placeholdered_instead(self) -> None:
        repairer = tr.TranscriptRepairer()
        message, changed, drop = repairer.repair_message(
            {"role": "assistant", "content": [_stu("call_5f46")]},
            empty=tr.EMPTY_PLACEHOLDER,
        )
        self.assertTrue(changed)
        self.assertFalse(drop)
        self.assertEqual(
            message["content"],
            [{"type": "text", "text": tr.EMPTY_PLACEHOLDER_TEXT}],
        )

    def test_a_clean_transcript_is_returned_untouched_and_identical(self) -> None:
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        ]
        repairer = tr.TranscriptRepairer()
        out, changed = repairer.repair_messages(messages)
        self.assertFalse(changed)
        self.assertIs(out, messages)
        self.assertFalse(repairer.stats.changed)

    def test_string_content_is_not_a_tool_carrier(self) -> None:
        repairer = tr.TranscriptRepairer()
        out, changed = repairer.repair_content("plain text")
        self.assertFalse(changed)
        self.assertEqual(out, "plain text")

    def test_strip_disabled_keeps_a_vendor_block_but_still_fixes_the_id(self) -> None:
        repairer = tr.TranscriptRepairer(strip_nonportable=False)
        blocks, changed = repairer.repair_content([_stu("call_x", "analyze_image")])
        self.assertTrue(changed)
        self.assertEqual(blocks[0]["id"], "srvtoolu_vct_call_x")
        self.assertEqual(repairer.stats.blocks_stripped, 0)


class ReverseMapTests(unittest.TestCase):
    def test_the_map_records_new_to_original(self) -> None:
        id_map: dict[str, str] = {}
        repairer = tr.TranscriptRepairer(id_map=id_map)
        repairer.repair_content([_stu("call_a.b", "web_search")])
        self.assertEqual(id_map, {"srvtoolu_vct_call_a_b": "call_a.b"})

    def test_the_map_holds_server_tool_ids_only(self) -> None:
        """It has to outlive the session (a client re-sends its whole history
        every turn), so it must not also carry every ordinary tool call."""
        id_map: dict[str, str] = {}
        repairer = tr.TranscriptRepairer(id_map=id_map)
        repairer.repair_content(
            [
                {"type": "tool_use", "id": "call_ordinary", "name": "R"},
                _stu("call_server", "web_search"),
            ]
        )
        self.assertEqual(list(id_map.values()), ["call_server"])

    def test_restore_puts_the_vendors_ids_back_everywhere(self) -> None:
        id_map = {"srvtoolu_vct_call_a_b": "call_a.b"}
        payload = {
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "server_tool_use", "id": "srvtoolu_vct_call_a_b",
                     "name": "web_search"},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "srvtoolu_vct_call_a_b"},
                ]},
            ],
        }
        restored, count = tr.restore_ids(payload, id_map)
        self.assertEqual(count, 2)
        self.assertEqual(restored["messages"][0]["content"][0]["id"], "call_a.b")
        self.assertEqual(
            restored["messages"][1]["content"][0]["tool_use_id"], "call_a.b",
        )

    def test_restore_is_a_no_op_when_nothing_matches(self) -> None:
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        restored, count = tr.restore_ids(payload, {"srvtoolu_x": "call_x"})
        self.assertEqual(count, 0)
        self.assertIs(restored, payload)

    def test_the_round_trip_is_lossless_for_a_lossy_id(self) -> None:
        """Sanitising ``a.b`` -> ``a_b`` is lossy; the MAP is what makes the
        way back exact, which is why it exists at all."""
        id_map: dict[str, str] = {}
        repairer = tr.TranscriptRepairer(id_map=id_map)
        blocks, _ = repairer.repair_content([_stu("call_a.b", "web_search")])
        back, _ = tr.restore_ids({"content": blocks}, id_map)
        self.assertEqual(back["content"][0]["id"], "call_a.b")


def _entry(kind: str, content, uuid: str, parent: str | None = None) -> dict:
    return {
        "parentUuid": parent,
        "uuid": uuid,
        "type": kind,
        "timestamp": "2026-09-08T00:00:00.000Z",
        "sessionId": "0000-fixture",
        "message": {"id": f"msg_{uuid}", "role": kind, "content": content},
    }


class SessionFileTests(unittest.TestCase):
    """Fixtures shaped like the real jsonl. No real session is ever opened."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="v0294-tr-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _write(self, entries: list[dict]) -> Path:
        path = self.root / "session.jsonl"
        path.write_text(
            "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8",
        )
        return path

    def _poisoned(self) -> list[dict]:
        """THE field shape, from the poisoned session's own jsonl.

        Verbatim structure of lines 665429-665437: ONE content block per
        entry, every block sharing the same ``message.id``, and the vendor
        built-in's result written as a plain ``tool_result`` INSIDE the
        assistant message (not as a ``*_tool_result`` block). Two
        ``analyze_image`` calls, text blocks between them. Fabricating a
        tidier shape here is how the first pass missed the assistant-side
        ``tool_result`` entirely.
        """
        mid = "msg_01FieldShape"
        entries = [_entry("user", "look at these two screenshots", "u1")]
        blocks = [
            [{"type": "text", "text": "I'll look at the first one."}],
            [_stu("call_5f460651ce3144e4ac3132bf", "analyze_image")],
            [{"type": "text", "text": "Now the second."}],
            [_stu("call_be36f1a29d0b4c7e9a1d5e3f", "analyze_image")],
            [{"type": "text", "text": "Both are screenshots."}],
            [{"type": "tool_result",
              "tool_use_id": "call_5f460651ce3144e4ac3132bf",
              "content": "a terminal window"}],
            [{"type": "tool_result",
              "tool_use_id": "call_be36f1a29d0b4c7e9a1d5e3f",
              "content": "a browser window"}],
        ]
        parent = "u1"
        for index, content in enumerate(blocks):
            uuid = f"a{index + 1}"
            entry = _entry("assistant", content, uuid, parent)
            entry["message"]["id"] = mid
            entries.append(entry)
            parent = uuid
        entries.append(
            _entry(
                "user",
                [{"type": "text", "text": "thanks"}],
                "u2",
                parent,
            )
        )
        return entries

    def test_repair_strips_the_poison_and_keeps_the_rest(self) -> None:
        path = self._write(self._poisoned())
        result = tr.repair_file(path)
        # 9 entries: 1 user + 7 assistant + 1 user. Four are touched — the two
        # server_tool_use lines and the two tool_result lines that follow them.
        self.assertEqual(result.entries_total, 9)
        self.assertEqual(result.entries_touched, 4)
        self.assertEqual(result.stats.blocks_stripped, 2)
        self.assertEqual(result.stats.results_stripped, 2)

        entries = [json.loads(line) for line in path.read_text().splitlines()]
        blob = json.dumps(entries)
        self.assertNotIn("analyze_image", blob)
        self.assertNotIn("call_5f460651ce3144e4ac3132bf", blob)
        self.assertNotIn("call_be36f1a29d0b4c7e9a1d5e3f", blob)
        # The emptied lines keep their place in the uuid chain, carrying the
        # placeholder rather than an empty content list.
        placeholders = [
            e for e in entries
            if e["message"]["content"] == [
                {"type": "text", "text": tr.EMPTY_PLACEHOLDER_TEXT}
            ]
        ]
        self.assertEqual(len(placeholders), 4)
        self.assertEqual([e["uuid"] for e in entries[1:8]],
                         [f"a{i}" for i in range(1, 8)])

    def test_the_repaired_field_shape_has_no_assistant_tool_result(self) -> None:
        path = self._write(self._poisoned())
        tr.repair_file(path)
        for line in path.read_text().splitlines():
            entry = json.loads(line)
            if entry["type"] != "assistant":
                continue
            for block in entry["message"]["content"]:
                self.assertNotEqual(block.get("type"), "tool_result")

    def test_uuids_and_envelope_keys_survive(self) -> None:
        path = self._write(self._poisoned())
        before = [json.loads(line) for line in path.read_text().splitlines()]
        tr.repair_file(path)
        after = [json.loads(line) for line in path.read_text().splitlines()]
        for old, new in zip(before, after):
            self.assertEqual(old["uuid"], new["uuid"])
            self.assertEqual(old["parentUuid"], new["parentUuid"])
            self.assertEqual(old["timestamp"], new["timestamp"])
            self.assertEqual(old["sessionId"], new["sessionId"])
            self.assertEqual(old["message"]["id"], new["message"]["id"])

    def test_the_backup_holds_the_original_bytes(self) -> None:
        path = self._write(self._poisoned())
        original = path.read_bytes()
        result = tr.repair_file(path)
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.read_bytes(), original)
        self.assertNotEqual(path.read_bytes(), original)

    def test_two_repairs_in_the_same_second_keep_both_backups(self) -> None:
        """Second granularity in the name, so the collision is reachable.

        The backup writer refuses to overwrite an existing backup — losing
        the pre-repair bytes is the one thing a backup exists to prevent —
        so the NAME has to be free before the link is attempted. Without
        that, a re-run inside the same second failed with FileExistsError
        (better than the silent clobber it replaced, but still a break).
        """
        path = self._write(self._poisoned())
        original = path.read_bytes()
        first = tr.repair_file(path)
        assert first.backup_path is not None

        # Re-poison the SAME path within the same second and repair again.
        path.write_bytes(original)
        with mock.patch.object(tr.time, "strftime", return_value="20260908T120000"):
            second = tr.repair_file(path)
        assert second.backup_path is not None
        self.assertNotEqual(second.backup_path, first.backup_path)
        self.assertTrue(first.backup_path.is_file())
        self.assertEqual(second.backup_path.read_bytes(), original)

    def test_the_backup_name_is_free_before_it_is_used(self) -> None:
        """Unit half: an occupied name is stepped over, not reused."""
        path = self._write(self._poisoned())
        with mock.patch.object(tr.time, "strftime", return_value="20260908T120000"):
            first = tr._free_backup_path(path)
            first.write_text("taken", encoding="utf-8")
            second = tr._free_backup_path(path)
        self.assertEqual(first.name, f"{path.name}.bak-20260908T120000")
        self.assertEqual(second.name, f"{path.name}.bak-20260908T120000-2")

    def test_a_vendor_web_search_pair_is_removed_from_the_file(self) -> None:
        """``fix-transcript`` repairs a file so ANTHROPIC will accept it, so
        it takes the same vendor-origin strip the in-flight path does — a
        pair whose result needs an ``encrypted_content`` blob only Anthropic
        issues cannot be repaired into validity, only removed."""
        entry = _entry("assistant", "", "a1")
        entry["message"]["content"] = [
            {"type": "text", "text": "searching"},
            {"type": "server_tool_use", "id": "call_probe1",
             "name": "web_search", "input": {}},
            {"type": "web_search_tool_result", "tool_use_id": "call_probe1",
             "content": []},
        ]
        path = self._write([entry])
        result = tr.repair_file(path)
        self.assertEqual(result.entries_touched, 1)
        repaired = json.loads(path.read_text().splitlines()[0])
        self.assertEqual(
            [b["type"] for b in repaired["message"]["content"]], ["text"],
        )

    def test_a_clean_file_is_left_byte_identical_with_no_backup(self) -> None:
        path = self._write([_entry("user", "hi", "u1")])
        original = path.read_bytes()
        result = tr.repair_file(path)
        self.assertEqual(result.entries_touched, 0)
        self.assertIsNone(result.backup_path)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_dry_run_writes_nothing_and_still_reports(self) -> None:
        path = self._write(self._poisoned())
        original = path.read_bytes()
        result = tr.repair_file(path, dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.entries_touched, 4)
        self.assertEqual(result.stats.blocks_stripped, 2)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_an_unparseable_line_is_carried_through_verbatim(self) -> None:
        path = self.root / "session.jsonl"
        path.write_text(
            json.dumps(_entry("user", "hi", "u1")) + "\n"
            + "{ this is not json\n"
            + json.dumps(self._poisoned()[1]) + "\n",
            encoding="utf-8",
        )
        result = tr.repair_file(path)
        self.assertEqual(result.unparseable_lines, 1)
        self.assertIn("{ this is not json", path.read_text())

    def test_untouched_lines_are_not_reserialised(self) -> None:
        """A line the repair does not change keeps its exact spacing."""
        path = self.root / "session.jsonl"
        quirky = '{"type":"user","uuid":"u1","message":{"role":"user","content":"hi"}}'
        path.write_text(
            quirky + "\n" + json.dumps(self._poisoned()[1]) + "\n", encoding="utf-8",
        )
        tr.repair_file(path)
        self.assertEqual(path.read_text().splitlines()[0], quirky)

    def test_bytes_that_are_not_utf8_survive_untouched_lines(self) -> None:
        """``errors="replace"`` would rewrite them as U+FFFD — a repair tool
        must not edit bytes it was not asked to touch."""
        path = self.root / "session.jsonl"
        good = json.dumps(_entry("user", "hi", "u1")).encode() + b"\n"
        broken = (
            b'{"type":"user","uuid":"u9","message":'
            b'{"role":"user","content":"caf\xe9"}}\n'
        )
        poisoned = json.dumps(self._poisoned()[2]).encode() + b"\n"
        path.write_bytes(good + broken + poisoned)
        tr.repair_file(path)
        self.assertIn(b"caf\xe9", path.read_bytes())

    def test_crlf_line_endings_are_preserved_on_changed_lines(self) -> None:
        path = self.root / "session.jsonl"
        path.write_bytes(
            json.dumps(_entry("user", "hi", "u1")).encode() + b"\r\n"
            + json.dumps(self._poisoned()[2]).encode() + b"\r\n"
        )
        tr.repair_file(path)
        raw = path.read_bytes()
        self.assertEqual(raw.count(b"\r\n"), 2)
        self.assertNotIn(b"analyze_image", raw)

    def test_a_final_line_without_a_newline_does_not_grow_one(self) -> None:
        path = self.root / "session.jsonl"
        path.write_bytes(json.dumps(self._poisoned()[2]).encode())
        tr.repair_file(path)
        self.assertFalse(path.read_bytes().endswith(b"\n"))

    def test_repair_is_idempotent(self) -> None:
        path = self._write(self._poisoned())
        tr.repair_file(path)
        first = path.read_bytes()
        second_result = tr.repair_file(path)
        self.assertEqual(second_result.entries_touched, 0)
        self.assertEqual(path.read_bytes(), first)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="v0294-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_the_subcommand_is_registered_on_the_vco_parser(self) -> None:
        """A CLI nobody can invoke is a promise, not a command."""
        from vco_lib.cli.__main__ import _build_parser

        parser = _build_parser()
        actions = [
            a for a in parser._actions if hasattr(a, "choices") and a.choices
        ]
        names: set[str] = set()
        for action in actions:
            names |= set(action.choices)  # type: ignore[arg-type]
        self.assertIn("fix-transcript", names)

    def test_a_missing_file_is_exit_2_not_a_traceback(self) -> None:
        self.assertEqual(ft.run(self.root / "nope.jsonl"), ft.EXIT_BAD_ARGS)

    def test_run_reports_and_repairs(self) -> None:
        path = self.root / "s.jsonl"
        path.write_text(
            json.dumps(
                _entry("assistant", [_stu("call_x", "analyze_image")], "a1"),
            ) + "\n",
            encoding="utf-8",
        )
        out = StringIO()
        code = ft.run(path, out=out)
        self.assertEqual(code, ft.EXIT_OK)
        self.assertIn("blocks stripped    : 1", out.getvalue())
        self.assertIn("backup", out.getvalue())

    def test_json_output_is_machine_readable(self) -> None:
        path = self.root / "s.jsonl"
        path.write_text(
            json.dumps(_entry("assistant", [_stu("call_x")], "a1")) + "\n",
            encoding="utf-8",
        )
        out = StringIO()
        ft.run(path, dry_run=True, as_json=True, out=out)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["blocks_stripped"], 1)
        self.assertEqual(payload["stripped_names"], ["analyze_image"])
        self.assertTrue(payload["dry_run"])
        self.assertIsNone(payload["backup"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
