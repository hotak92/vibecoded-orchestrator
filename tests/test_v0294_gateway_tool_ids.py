# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tool-id normalisation on all three gateway paths, plus the SSE rewriter.

The unit half drives :mod:`model_router.tool_ids` directly; the integration
half drives the whole aiohttp app against the same stub upstreams the rest of
the server suite uses (:class:`tests.test_model_router_server.GatewayTestBase`
— reused rather than re-implemented, so a change in how the gateway is wired
cannot leave a second, stale harness behind).

What is pinned, and the field consequence of each:

* a vendor response carrying a ``call_…`` SERVER-tool id reaches the CLIENT
  with a ``srvtoolu_…`` one — without this the transcript is poisoned at the
  moment it is written and every later Anthropic request 400s. Plain
  ``tool_use`` ids are deliberately NOT rewritten: Anthropic accepts them as
  they are, so rewriting would only enlarge the map that has to be kept;
* a vendor ``server_tool_use`` Anthropic cannot represent is stripped WITH its
  result — normalising its id is not enough, the name itself is rejected;
* the vendor gets its OWN ids back on the next request;
* an inherited transcript is repaired on the way to Anthropic, so an ALREADY
  poisoned session recovers instead of returning an opaque 400;
* SSE: ids fixed inside ``content_block_start``, dropped blocks suppressed,
  indexes renumbered contiguously, and an untouched stream relayed byte for
  byte.
"""
from __future__ import annotations

import json
import unittest

from vco_lib import transcript_repair as tr

from model_router import tool_ids as ti

from tests.test_model_router_server import GatewayTestBase


def _sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")


class BoundedIdMapTests(unittest.TestCase):
    def test_it_evicts_the_oldest_insertion(self) -> None:
        id_map = ti.BoundedIdMap(max_entries=2)
        id_map["a"] = "1"
        id_map["b"] = "2"
        id_map["c"] = "3"
        self.assertEqual(dict(id_map), {"b": "2", "c": "3"})

    def test_rewriting_a_key_does_not_evict(self) -> None:
        id_map = ti.BoundedIdMap(max_entries=2)
        id_map["a"] = "1"
        id_map["b"] = "2"
        id_map["a"] = "1b"
        self.assertEqual(dict(id_map), {"a": "1b", "b": "2"})

    def test_the_cap_is_never_zero(self) -> None:
        self.assertEqual(ti.BoundedIdMap(max_entries=0).max_entries, 1)


class PayloadAdapterTests(unittest.TestCase):
    def test_a_vendor_response_is_normalised_and_remembered(self) -> None:
        id_map = ti.BoundedIdMap()
        payload, stats = ti.normalise_vendor_response(
            {
                "type": "message",
                "content": [
                    {"type": "server_tool_use", "id": "call_a.b",
                     "name": "web_search", "input": {}},
                ],
            },
            id_map,
        )
        self.assertEqual(payload["content"][0]["id"], "srvtoolu_vct_call_a_b")
        self.assertEqual(stats.ids_rewritten, 1)
        self.assertEqual(id_map["srvtoolu_vct_call_a_b"], "call_a.b")

    def test_a_plain_tool_use_id_is_left_alone(self) -> None:
        """Anthropic does not pattern-check ``tool_use.id`` — a ``call_…`` one
        returned 200 in the probe — so rewriting it buys nothing and would put
        every tool call of a session into the bounded map."""
        id_map = ti.BoundedIdMap()
        payload, stats = ti.normalise_vendor_response(
            {
                "type": "message",
                "content": [
                    {"type": "tool_use", "id": "call_a.b", "name": "R",
                     "input": {}},
                ],
            },
            id_map,
        )
        self.assertEqual(payload["content"][0]["id"], "call_a.b")
        self.assertFalse(stats.changed)
        self.assertEqual(dict(id_map), {})

    def test_a_clean_vendor_response_is_the_same_object(self) -> None:
        original = {"type": "message", "content": [{"type": "text", "text": "x"}]}
        payload, stats = ti.normalise_vendor_response(original, ti.BoundedIdMap())
        self.assertIs(payload, original)
        self.assertFalse(stats.changed)

    def test_an_inherited_transcript_is_repaired_for_anthropic(self) -> None:
        payload, stats = ti.sanitise_for_anthropic(
            {
                "model": "claude-opus-5",
                "messages": [
                    {"role": "assistant", "content": [
                        {"type": "text", "text": "hi"},
                        {"type": "server_tool_use", "id": "call_5f46",
                         "name": "analyze_image", "input": {}},
                        {"type": "web_search_tool_result",
                         "tool_use_id": "call_5f46", "content": []},
                    ]},
                ],
            }
        )
        content = payload["messages"][0]["content"]
        self.assertEqual([b["type"] for b in content], ["text"])
        self.assertEqual(stats.touched_indexes, [0])
        self.assertEqual(stats.blocks_stripped, 1)


class FirstPartyRequestSurvivalTests(unittest.TestCase):
    """``sanitise_for_anthropic`` runs on EVERY Anthropic-bound request.

    That makes a false positive here expensive in a way the vendor path's
    is not: a healthy first-party transcript, never near a vendor, is
    rewritten on its way out. Deriving the result types from the tool names
    did exactly that — it invented two spellings that do not exist and missed
    ``tool_search_tool_result`` and ``mcp_tool_result``, both of which were
    then stripped, leaving their producers dangling and manufacturing the
    400 this module exists to prevent.
    """

    @staticmethod
    def _turn(*blocks: dict) -> dict:
        return {"messages": [{"role": "assistant", "content": list(blocks)}]}

    def test_every_documented_result_type_survives_untouched(self) -> None:
        for name, kind in sorted(tr.SERVER_TOOL_RESULT_TYPE_BY_NAME.items()):
            with self.subTest(name=name, kind=kind):
                producer_type = (
                    "mcp_tool_use" if name == "mcp_tool_use" else "server_tool_use"
                )
                ref = (
                    "mcptoolu_01Xyz"
                    if producer_type == "mcp_tool_use"
                    else "srvtoolu_01Abc"
                )
                payload = self._turn(
                    {"type": producer_type, "id": ref, "name": name, "input": {}},
                    {"type": kind, "tool_use_id": ref, "content": []},
                )
                repaired, stats = ti.sanitise_for_anthropic(payload)
                self.assertFalse(stats.changed, stats.summary())
                self.assertIs(repaired, payload)

    def test_an_mcp_result_is_not_read_as_an_orphan(self) -> None:
        """``mcptoolu_`` does not match ``^srvtoolu_``, so the orphan rule
        was deleting a first-party block on every request."""
        payload = self._turn(
            {"type": "mcp_tool_result", "tool_use_id": "mcptoolu_01Xyz",
             "is_error": False, "content": [{"type": "text", "text": "ok"}]},
        )
        repaired, stats = ti.sanitise_for_anthropic(payload)
        self.assertFalse(stats.changed, stats.summary())
        self.assertIs(repaired, payload)

    def test_a_vendor_origin_web_search_pair_is_stripped_despite_the_name(
        self,
    ) -> None:
        """The 2026-09-09 probe, in one test.

        ``web_search`` is a name Anthropic accepts, so the name rule kept
        this pair and normalised its id. Anthropic then rejected the RESULT:
        ``encrypted_content: Field required``, and a value we invent is
        ``Invalid encrypted_content``. The pair has no valid form on this
        route, so keeping it means shipping a 400 into the history — which
        is the failure this module exists to prevent, arrived at by the
        repair itself.
        """
        payload = self._turn(
            {"type": "text", "text": "searching"},
            {"type": "server_tool_use", "id": "call_probe1",
             "name": "web_search", "input": {"query": "x"}},
            {"type": "web_search_tool_result", "tool_use_id": "call_probe1",
             "content": [{"type": "web_search_result",
                          "url": "https://example.com/a", "title": "A"}]},
        )
        repaired, stats = ti.sanitise_for_anthropic(payload)
        self.assertTrue(stats.changed)
        self.assertEqual(
            [b["type"] for b in repaired["messages"][0]["content"]], ["text"],
        )
        self.assertEqual(stats.blocks_stripped, 1)
        self.assertEqual(stats.results_stripped, 1)

    def test_a_pair_we_already_normalised_is_stripped_too(self) -> None:
        """Our own marker says vendor-origin as loudly as ``call_…`` does."""
        payload = self._turn(
            {"type": "server_tool_use", "id": "srvtoolu_vct_call_probe1",
             "name": "web_search", "input": {}},
            {"type": "web_search_tool_result",
             "tool_use_id": "srvtoolu_vct_call_probe1", "content": []},
        )
        repaired, stats = ti.sanitise_for_anthropic(payload)
        self.assertTrue(stats.changed)
        self.assertEqual(repaired["messages"], [])

    def test_the_vendor_response_path_KEEPS_the_same_pair(self) -> None:
        """The destination decides, and this one is not Anthropic.

        The client's next turn on a vendor route goes back to the same
        vendor, which understands its own blocks. Stripping here would take
        the tool context out of a conversation that was working; the strip
        belongs on the Anthropic-bound path, where the 400 is.
        """
        id_map = ti.BoundedIdMap()
        payload, stats = ti.normalise_vendor_response(
            {
                "type": "message",
                "content": [
                    {"type": "server_tool_use", "id": "call_probe1",
                     "name": "web_search", "input": {}},
                    {"type": "web_search_tool_result",
                     "tool_use_id": "call_probe1", "content": []},
                ],
            },
            id_map,
        )
        self.assertEqual(
            [b["type"] for b in payload["content"]],
            ["server_tool_use", "web_search_tool_result"],
        )
        self.assertEqual(payload["content"][0]["id"], "srvtoolu_vct_call_probe1")
        self.assertEqual(
            payload["content"][1]["tool_use_id"], "srvtoolu_vct_call_probe1",
        )
        self.assertEqual(stats.blocks_stripped, 0)

    def test_a_vendor_result_is_still_stripped_on_this_path(self) -> None:
        """ACT half: the survival above is not "strip nothing"."""
        payload = self._turn(
            {"type": "text", "text": "looking"},
            {"type": "analyze_image_tool_result",
             "tool_use_id": "call_5f460651ce", "content": []},
        )
        repaired, stats = ti.sanitise_for_anthropic(payload)
        self.assertTrue(stats.changed)
        self.assertEqual(
            [b["type"] for b in repaired["messages"][0]["content"]], ["text"],
        )


class SseRewriterTests(unittest.TestCase):
    def test_an_untouched_stream_is_byte_identical(self) -> None:
        stream = (
            _sse("message_start", {"type": "message_start", "message": {}})
            + _sse("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""}})
            + _sse("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "hi"}})
            + _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
            + _sse("message_stop", {"type": "message_stop"})
        )
        rewriter = ti.SseIdRewriter(id_map=ti.BoundedIdMap())
        out = rewriter.feed(stream) + rewriter.flush()
        self.assertEqual(out, stream)
        self.assertFalse(rewriter.stats.changed)

    def test_ids_in_content_block_start_are_normalised(self) -> None:
        id_map = ti.BoundedIdMap()
        rewriter = ti.SseIdRewriter(id_map=id_map)
        out = rewriter.feed(
            _sse("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "server_tool_use", "id": "call_abc",
                                  "name": "web_search", "input": {}}})
        ) + rewriter.flush()
        self.assertIn(b'"id": "srvtoolu_vct_call_abc"', out)
        self.assertEqual(id_map["srvtoolu_vct_call_abc"], "call_abc")

    def test_a_split_chunk_boundary_is_handled(self) -> None:
        event = _sse("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "server_tool_use", "id": "call_abc",
                              "name": "web_search"}})
        rewriter = ti.SseIdRewriter(id_map=ti.BoundedIdMap())
        first = rewriter.feed(event[:20])
        self.assertEqual(first, b"")
        rest = rewriter.feed(event[20:]) + rewriter.flush()
        self.assertIn(b"srvtoolu_vct_call_abc", rest)

    def test_crlf_frames_are_split_and_re_emitted_as_crlf(self) -> None:
        """SSE allows CRLF. Matching only LF+LF buffered such a stream to the
        32 MB limit and then relayed it unrepaired."""
        stream = (
            b"event: content_block_start\r\n"
            b'data: {"type":"content_block_start","index":0,"content_block":'
            b'{"type":"server_tool_use","id":"call_abc","name":"web_search"}}'
            b"\r\n\r\n"
        )
        rewriter = ti.SseIdRewriter(id_map=ti.BoundedIdMap())
        out = rewriter.feed(stream) + rewriter.flush()
        self.assertIn(b"srvtoolu_vct_call_abc", out)
        self.assertTrue(out.endswith(b"\r\n\r\n"))
        self.assertNotIn(b"\n\n", out.replace(b"\r\n\r\n", b""))

    def test_lone_cr_frames_are_split_and_re_emitted_as_cr(self) -> None:
        """A CR is a line terminator too (EventSource spec: CR, LF or CRLF).

        Knowing only LF meant a CR-framed stream never produced a single
        event boundary: it buffered to the 32 MB limit and was then relayed
        unrepaired — the exact failure the CRLF case had already cost once.
        """
        poisoned = (
            b"event: content_block_start\r"
            b'data: {"type":"content_block_start","index":0,"content_block":'
            b'{"type":"server_tool_use","id":"call_abc","name":"web_search"}}'
            b"\r\r"
        )
        trailing = b'event: ping\rdata: {"type":"ping"}\r\r'
        rewriter = ti.SseIdRewriter(id_map=ti.BoundedIdMap())
        out = rewriter.feed(poisoned + trailing)
        self.assertIn(b"srvtoolu_vct_call_abc", out)
        self.assertTrue(out.endswith(b"\r\r"))
        self.assertNotIn(b"\n", out, "a CR stream must not acquire LFs")
        self.assertEqual(
            rewriter.flush(), trailing,
            "the trailing CR is ambiguous while more bytes may come, so the "
            "last event waits — and at EOF it is split, not guessed at",
        )

    def test_the_final_cr_framed_event_is_rewritten_at_flush(self) -> None:
        """EOF settles the ambiguity, so the last event is NOT lost to it.

        Holding it back until ``flush`` and then emitting it verbatim left a
        CR-only stream with exactly one unrepaired event — the last — which
        is the one carrying ``message_delta``/``content_block_start`` ids in
        a single-tool turn.
        """
        last = (
            b"event: content_block_start\r"
            b'data: {"type":"content_block_start","index":0,"content_block":'
            b'{"type":"server_tool_use","id":"call_zzz","name":"web_search"}}'
            b"\r\r"
        )
        id_map = ti.BoundedIdMap()
        rewriter = ti.SseIdRewriter(id_map=id_map)
        held = rewriter.feed(last)
        self.assertEqual(held, b"", "ambiguous while more bytes may arrive")
        tail = rewriter.flush()
        self.assertIn(b"srvtoolu_vct_call_zzz", tail)
        self.assertTrue(tail.endswith(b"\r\r"))
        self.assertEqual(id_map["srvtoolu_vct_call_zzz"], "call_zzz")

    def test_an_incomplete_final_event_is_still_flushed_verbatim(self) -> None:
        """LEAVE-ALONE half: no boundary at all is not a boundary at EOF."""
        rewriter = ti.SseIdRewriter(id_map=ti.BoundedIdMap())
        self.assertEqual(rewriter.feed(b'data: {"type":"ping"}\r'), b"")
        self.assertEqual(rewriter.flush(), b'data: {"type":"ping"}\r')

    def test_a_crlf_line_inside_an_event_is_not_a_boundary(self) -> None:
        """The backtracking trap, pinned.

        With a bare ``\\r`` alternative the engine reads one ``\\r\\n`` as
        "CR then LF" — two terminators — so EVERY CRLF line ending inside an
        event splits it, and a ``data:`` line is separated from its
        ``event:`` line.
        """
        rewriter = ti.SseIdRewriter(id_map=ti.BoundedIdMap())
        self.assertEqual(
            rewriter.feed(b"event: ping\r\ndata: "), b"",
            "an event with one CRLF line ending is not yet complete",
        )

    def test_a_crlf_boundary_split_across_chunks_is_not_split_early(self) -> None:
        """The ambiguity the lone-CR support introduces, and its resolution.

        A buffer ending in ``\\r`` is either a lone-CR terminator or the first
        half of a CRLF. Guessing "lone CR" cuts the event one byte early and
        leaves a stray LF at the head of the next one; waiting for the byte
        costs one chunk.
        """
        event = (
            b"event: ping\r\n"
            b'data: {"type":"ping"}'
            b"\r\n\r\n"
        )
        rewriter = ti.SseIdRewriter(id_map=ti.BoundedIdMap())
        first = rewriter.feed(event[:-1])
        self.assertEqual(first, b"", "held: the last \\r may still become \\r\\n")
        rest = rewriter.feed(event[-1:])
        self.assertEqual(first + rest + rewriter.flush(), event)

    def test_a_mixed_terminator_event_keeps_every_line_ending(self) -> None:
        """Each line keeps ITS OWN ending, including the rewritten one."""
        stream = (
            b"event: content_block_start\r\n"
            b"id: 7\n"
            b'data: {"type":"content_block_start","index":0,"content_block":'
            b'{"type":"server_tool_use","id":"call_abc","name":"web_search"}}\r\n'
            b"\r\n"
        )
        rewriter = ti.SseIdRewriter(id_map=ti.BoundedIdMap())
        out = rewriter.feed(stream) + rewriter.flush()
        self.assertIn(b"srvtoolu_vct_call_abc", out)
        self.assertIn(b"event: content_block_start\r\n", out)
        self.assertIn(b"id: 7\n", out)
        self.assertTrue(out.rstrip().endswith(b"}}"), out)
        self.assertIn(b"}}\r\n\r\n", out, "the data line kept its own CRLF")

    def test_an_untouched_cr_framed_stream_is_byte_identical(self) -> None:
        """Verbatim re-emission holds for every framing, not just LF."""
        stream = (
            b'event: message_start\rdata: {"type":"message_start"}\r\r'
            b'event: message_stop\rdata: {"type":"message_stop"}\r\r'
        )
        rewriter = ti.SseIdRewriter(id_map=ti.BoundedIdMap())
        self.assertEqual(rewriter.feed(stream) + rewriter.flush(), stream)

    def test_an_assistant_tool_result_follows_its_dropped_producer(self) -> None:
        """THE field shape: the vendor writes its built-in's result as a plain
        ``tool_result`` in the ASSISTANT message. Leaving it behind is a live
        400 — ``tool_result blocks can only be in user messages``."""
        stream = (
            _sse("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "server_tool_use", "id": "call_5f46",
                                  "name": "analyze_image", "input": {}}})
            + _sse("content_block_start", {
                "type": "content_block_start", "index": 1,
                "content_block": {"type": "tool_result",
                                  "tool_use_id": "call_5f46",
                                  "content": "a cat"}})
            + _sse("content_block_start", {
                "type": "content_block_start", "index": 2,
                "content_block": {"type": "tool_result",
                                  "tool_use_id": "call_never_seen",
                                  "content": "orphan"}})
            + _sse("content_block_start", {
                "type": "content_block_start", "index": 3,
                "content_block": {"type": "text", "text": ""}})
        )
        rewriter = ti.SseIdRewriter(id_map=ti.BoundedIdMap())
        out = (rewriter.feed(stream) + rewriter.flush()).decode()
        self.assertNotIn("tool_result", out)
        self.assertNotIn("call_5f46", out)
        # The second one's producer is not in this stream at all: it goes
        # because an assistant message may not carry a tool_result AT ALL,
        # which is a separate live 400 from the dropped-producer rule.
        self.assertNotIn("call_never_seen", out)
        self.assertIn('"type": "text"', out)
        indexes = [
            json.loads(line[len("data: "):])["index"]
            for line in out.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(indexes, [0], "the survivor is renumbered to 0")

    def test_a_dropped_block_takes_its_events_and_renumbers_the_rest(self) -> None:
        """The whole point: a vendor built-in never reaches the transcript."""
        stream = (
            _sse("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "server_tool_use", "id": "call_5f46",
                                  "name": "analyze_image", "input": {}}})
            + _sse("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "{}"}})
            + _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
            + _sse("content_block_start", {
                "type": "content_block_start", "index": 1,
                "content_block": {"type": "text", "text": ""}})
            + _sse("content_block_delta", {
                "type": "content_block_delta", "index": 1,
                "delta": {"type": "text_delta", "text": "a cat"}})
            + _sse("content_block_stop", {"type": "content_block_stop", "index": 1})
        )
        rewriter = ti.SseIdRewriter(id_map=ti.BoundedIdMap())
        out = (rewriter.feed(stream) + rewriter.flush()).decode()

        self.assertNotIn("analyze_image", out)
        self.assertNotIn("call_5f46", out)
        self.assertIn("a cat", out)
        indexes = [
            json.loads(line[len("data: "):])["index"]
            for line in out.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(indexes, [0, 0, 0], "the surviving block is renumbered to 0")
        self.assertEqual(rewriter.stats.blocks_stripped, 1)
        self.assertEqual(rewriter.blocks_suppressed, 1)
        self.assertNotIn("\n\n\n", out, "a suppressed event leaves no blank event")

    def test_a_server_tool_result_block_follows_its_dropped_producer(self) -> None:
        stream = (
            _sse("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "server_tool_use", "id": "call_x",
                                  "name": "analyze_image", "input": {}}})
            + _sse("content_block_start", {
                "type": "content_block_start", "index": 1,
                "content_block": {"type": "analyze_image_tool_result",
                                  "tool_use_id": "call_x", "content": []}})
        )
        rewriter = ti.SseIdRewriter(id_map=ti.BoundedIdMap())
        out = (rewriter.feed(stream) + rewriter.flush()).decode()
        self.assertEqual(out, "")
        self.assertEqual(rewriter.stats.results_stripped, 1)

    def test_a_non_json_event_is_passed_through(self) -> None:
        raw = b": keep-alive comment\n\n"
        rewriter = ti.SseIdRewriter(id_map=ti.BoundedIdMap())
        self.assertEqual(rewriter.feed(raw), raw)

    def test_a_trailing_partial_event_is_flushed_not_dropped(self) -> None:
        rewriter = ti.SseIdRewriter(id_map=ti.BoundedIdMap())
        self.assertEqual(rewriter.feed(b"data: {\"type\":\"ping\"}"), b"")
        self.assertEqual(rewriter.flush(), b"data: {\"type\":\"ping\"}")


class VendorToolIdIntegrationTests(GatewayTestBase):
    """The whole app, against the stub upstreams."""

    async def test_a_vendor_json_response_reaches_the_client_normalised(self) -> None:
        self.vendor_up.messages_body = {
            "id": "msg_1",
            "type": "message",
            "model": "glm-5.3",
            "content": [
                {"type": "text", "text": "here"},
                {"type": "server_tool_use", "id": "call_5f460651ce",
                 "name": "web_search", "input": {"q": "x"}},
                {"type": "web_search_tool_result",
                 "tool_use_id": "call_5f460651ce", "content": []},
            ],
        }
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["content"][1]["id"], "srvtoolu_vct_call_5f460651ce")
        self.assertEqual(
            body["content"][2]["tool_use_id"], "srvtoolu_vct_call_5f460651ce",
        )

    async def test_a_vendor_builtin_is_stripped_before_it_reaches_the_client(self) -> None:
        self.vendor_up.messages_body = {
            "id": "msg_1",
            "type": "message",
            "content": [
                {"type": "text", "text": "a screenshot of a cat"},
                {"type": "server_tool_use", "id": "call_5f46",
                 "name": "analyze_image", "input": {}},
                {"type": "web_search_tool_result", "tool_use_id": "call_5f46",
                 "content": []},
            ],
        }
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        body = await resp.json()
        self.assertEqual([b["type"] for b in body["content"]], ["text"])

    async def test_the_vendor_gets_its_own_ids_back(self) -> None:
        self.vendor_up.messages_body = {
            "id": "msg_1",
            "type": "message",
            "content": [
                {"type": "server_tool_use", "id": "call_a.b",
                 "name": "web_search", "input": {}},
            ],
        }
        first = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        rewritten = (await first.json())["content"][0]["id"]
        self.assertEqual(rewritten, "srvtoolu_vct_call_a_b")

        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-gw/glm-5.3",
                "messages": [
                    {"role": "assistant", "content": [
                        {"type": "server_tool_use", "id": rewritten,
                         "name": "web_search"},
                    ]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": rewritten},
                    ]},
                ],
            },
        )
        forwarded = json.loads(self.vendor_up.message_requests[-1]["body"])
        self.assertEqual(forwarded["messages"][0]["content"][0]["id"], "call_a.b")
        self.assertEqual(
            forwarded["messages"][1]["content"][0]["tool_use_id"], "call_a.b",
        )

    async def test_an_inherited_poisoned_transcript_is_repaired_in_flight(self) -> None:
        """The dead-chat case: the client re-sends the poison every turn."""
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-opus-5",
                "messages": [
                    {"role": "user", "content": "look"},
                    {"role": "assistant", "content": [
                        {"type": "text", "text": "I see"},
                        {"type": "server_tool_use", "id": "call_5f46",
                         "name": "analyze_image", "input": {}},
                        {"type": "web_search_tool_result",
                         "tool_use_id": "call_5f46", "content": []},
                    ]},
                    {"role": "user", "content": "and?"},
                ],
            },
        )
        self.assertEqual(resp.status, 200)
        forwarded = json.loads(self.anthropic_up.message_requests[-1]["body"])
        blocks = forwarded["messages"][1]["content"]
        self.assertEqual([b["type"] for b in blocks], ["text"])

    async def test_the_field_shape_is_repaired_in_flight(self) -> None:
        """The poisoned session's OWN structure, not a tidier reconstruction.

        One content block per assistant turn, all sharing a ``message.id``,
        and the vendor built-in's result as a plain ``tool_result`` inside the
        assistant message. Live: this exact body returns 200 through the
        repaired gateway and 400 without it (``tool_result blocks can only be
        in user messages``, after the id error is fixed).
        """
        stu = "call_5f460651ce3144e4ac3132bf"
        stu2 = "call_be36f1a29d0b4c7e9a1d5e3f"
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-opus-5",
                "messages": [
                    {"role": "user", "content": "look at these two"},
                    {"role": "assistant", "content": [
                        {"type": "text", "text": "First one."}]},
                    {"role": "assistant", "content": [
                        {"type": "server_tool_use", "id": stu,
                         "name": "analyze_image", "input": {}}]},
                    {"role": "assistant", "content": [
                        {"type": "text", "text": "Now the second."}]},
                    {"role": "assistant", "content": [
                        {"type": "server_tool_use", "id": stu2,
                         "name": "analyze_image", "input": {}}]},
                    {"role": "assistant", "content": [
                        {"type": "tool_result", "tool_use_id": stu,
                         "content": "a terminal"}]},
                    {"role": "assistant", "content": [
                        {"type": "tool_result", "tool_use_id": stu2,
                         "content": "a browser"}]},
                    {"role": "user", "content": "what did you see?"},
                ],
            },
        )
        forwarded = json.loads(self.anthropic_up.message_requests[-1]["body"])
        blob = json.dumps(forwarded)
        self.assertNotIn("analyze_image", blob)
        self.assertNotIn(stu, blob)
        self.assertNotIn(stu2, blob)
        self.assertNotIn("tool_result", blob)
        # The four emptied turns are dropped, not forwarded with content: [].
        self.assertEqual(
            [m["role"] for m in forwarded["messages"]],
            ["user", "assistant", "assistant", "user"],
        )
        for message in forwarded["messages"]:
            self.assertTrue(message["content"], "no empty content survives")

    async def test_a_clean_anthropic_request_is_forwarded_unchanged(self) -> None:
        payload = {
            "model": "claude-opus-5",
            "messages": [{"role": "user", "content": "hi"}],
        }
        await self.client.post("/v1/messages", headers=self.auth(), json=payload)
        forwarded = json.loads(self.anthropic_up.message_requests[-1]["body"])
        self.assertEqual(forwarded, payload)

    async def test_a_vendor_sse_stream_is_normalised_on_the_wire(self) -> None:
        self.vendor_up.stream_chunks = [
            _sse("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "server_tool_use", "id": "call_5f46",
                                  "name": "analyze_image", "input": {}}}),
            _sse("content_block_start", {
                "type": "content_block_start", "index": 1,
                "content_block": {"type": "server_tool_use", "id": "call_zz",
                                  "name": "web_search", "input": {}}}),
            _sse("message_stop", {"type": "message_stop"}),
        ]
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "stream": True},
        )
        body = (await resp.read()).decode()
        self.assertNotIn("analyze_image", body)
        self.assertIn("srvtoolu_vct_call_zz", body)
        self.assertIn("message_stop", body)

    async def test_an_anthropic_sse_stream_is_relayed_untouched(self) -> None:
        chunks = [
            _sse("content_block_start", {
                "type": "content_block_start", "index": 3,
                "content_block": {"type": "text", "text": ""}}),
            _sse("message_stop", {"type": "message_stop"}),
        ]
        self.anthropic_up.stream_chunks = list(chunks)
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-opus-5", "messages": [], "stream": True},
        )
        self.assertEqual(await resp.read(), b"".join(chunks))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
