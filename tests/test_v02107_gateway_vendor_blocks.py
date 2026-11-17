# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""GW-1: vendor server-tool blocks must not poison a portable transcript.

All fixtures are synthetic and scrubbed. Their shapes come from the offline
block-shape table in REGISTER-GATEWAY.md, not from a live session file.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from model_router import tool_ids as ti
from vco_lib import transcript_repair as tr
from vco_lib.cli import fix_transcript as ft

from tests.test_model_router_server import GatewayTestBase


FIXTURE_URL = "https://example.invalid/read?q=fixture"


def _mcp_pair(block_id: str = "call_fixture") -> list[dict]:
    return [
        {
            "type": "mcp_tool_use",
            "id": block_id,
            "server_name": "web_reader",
            "name": "webReader",
            "input": {"url": FIXTURE_URL},
        },
        {
            "type": "mcp_tool_result",
            "tool_use_id": block_id,
            "is_error": False,
            "content": [{"type": "text", "text": f"Read {FIXTURE_URL}"}],
        },
    ]


def _server_pair(name: str, block_id: str = "call_fixture") -> list[dict]:
    return [
        {
            "type": "server_tool_use",
            "id": block_id,
            "name": name,
            "input": {"query": FIXTURE_URL},
        },
        {
            "type": tr.SERVER_TOOL_RESULT_TYPE_BY_NAME.get(
                name, f"{name}_tool_result",
            ),
            "tool_use_id": block_id,
            "content": [{"type": "text", "text": "synthetic result"}],
        },
    ]


def _request(blocks: list[dict], *, role: str = "assistant") -> dict:
    return {"messages": [{"role": role, "content": blocks}]}


def _response(blocks: list[dict]) -> dict:
    return {"type": "message", "role": "assistant", "content": blocks}


def _sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


def _stream(blocks: list[dict]) -> bytes:
    out = bytearray()
    for index, block in enumerate(blocks):
        start = dict(block)
        if block.get("type") in ("mcp_tool_use", "server_tool_use"):
            start["input"] = {}
        out += _sse("content_block_start", {
            "type": "content_block_start",
            "index": index,
            "content_block": start,
        })
        if block.get("type") in ("mcp_tool_use", "server_tool_use"):
            out += _sse("content_block_delta", {
                "type": "content_block_delta",
                "index": index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(block.get("input", {})),
                },
            })
        out += _sse("content_block_stop", {
            "type": "content_block_stop", "index": index,
        })
    return bytes(out)


def _rewind(stream: bytes, chunk_size: int = 7) -> tuple[bytes, ti.RepairStats]:
    rewriter = ti.SseIdRewriter(id_map=ti.BoundedIdMap())
    out = bytearray()
    for offset in range(0, len(stream), chunk_size):
        out += rewriter.feed(stream[offset:offset + chunk_size])
    out += rewriter.flush()
    return bytes(out), rewriter.stats


def _stream_text(raw: bytes) -> str:
    parts: list[str] = []
    for line in raw.decode().splitlines():
        if not line.startswith("data: "):
            continue
        event = json.loads(line[6:])
        block = event.get("content_block")
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        delta = event.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            parts.append(delta.get("text", ""))
    return "".join(parts)


def _assert_only_text_notes(case: unittest.TestCase, blocks: list[dict]) -> None:
    case.assertTrue(blocks)
    case.assertTrue(all(block.get("type") == "text" for block in blocks), blocks)
    case.assertIn(FIXTURE_URL, "".join(block.get("text", "") for block in blocks))


class FourSeamTests(unittest.TestCase):
    def test_no_vendor_mcp_block_survives_and_the_url_survives(self) -> None:
        response, _ = ti.normalise_vendor_response(
            _response(_mcp_pair()), ti.BoundedIdMap(),
        )
        _assert_only_text_notes(self, response["content"])

        anthropic, _ = ti.sanitise_for_anthropic(_request(_mcp_pair()))
        _assert_only_text_notes(self, anthropic["messages"][0]["content"])

        vendor, _ = ti.sanitise_for_vendor(_request(_mcp_pair()))
        _assert_only_text_notes(self, vendor["messages"][0]["content"])

        raw, stats = _rewind(_stream(_mcp_pair()))
        self.assertTrue(stats.changed)
        self.assertNotIn("mcp_tool_", raw.decode())
        self.assertIn(FIXTURE_URL, _stream_text(raw))

    def test_native_mcp_pair_is_byte_identical_after_every_seam(self) -> None:
        pair = _mcp_pair("mcptoolu_01Fixture")
        original_response = _response(pair)
        response, stats = ti.normalise_vendor_response(
            original_response, ti.BoundedIdMap(),
        )
        self.assertFalse(stats.changed)
        self.assertIs(response, original_response)

        original_request = _request(pair)
        anthropic, stats = ti.sanitise_for_anthropic(original_request)
        self.assertFalse(stats.changed)
        self.assertIs(anthropic, original_request)
        vendor, stats = ti.sanitise_for_vendor(original_request)
        self.assertFalse(stats.changed)
        self.assertIs(vendor, original_request)

        original_stream = _stream(pair)
        raw, stats = _rewind(original_stream)
        self.assertFalse(stats.changed)
        self.assertEqual(raw, original_stream)

    def test_sanitising_twice_is_sanitising_once(self) -> None:
        adapters = (
            (
                "buffered response",
                lambda value: ti.normalise_vendor_response(value, ti.BoundedIdMap()),
                _response(_mcp_pair()),
            ),
            ("Anthropic request", ti.sanitise_for_anthropic, _request(_mcp_pair())),
            ("vendor request", ti.sanitise_for_vendor, _request(_mcp_pair())),
        )
        for name, adapter, original in adapters:
            with self.subTest(adapter=name):
                once, first_stats = adapter(original)
                twice, second_stats = adapter(once)
                self.assertTrue(first_stats.changed)
                self.assertFalse(second_stats.changed, second_stats.summary())
                self.assertEqual(twice, once)

        once, _ = _rewind(_stream(_mcp_pair()))
        twice, stats = _rewind(once)
        self.assertFalse(stats.changed)
        self.assertEqual(twice, once)


class RegisterBlockShapeTests(unittest.TestCase):
    """One test per row of REGISTER-GATEWAY.md's block-shape table."""

    def test_nonportable_server_tool_with_vendor_id(self) -> None:
        blocks = _server_pair("analyze_image")
        response, _ = ti.normalise_vendor_response(
            _response(blocks), ti.BoundedIdMap(),
        )
        _assert_only_text_notes(self, response["content"])
        anthropic, _ = ti.sanitise_for_anthropic(_request(blocks))
        self.assertEqual(anthropic["messages"], [])
        vendor, _ = ti.sanitise_for_vendor(_request(blocks))
        _assert_only_text_notes(self, vendor["messages"][0]["content"])

    def test_portable_server_tool_with_vendor_id(self) -> None:
        blocks = _server_pair("web_search")
        response, _ = ti.normalise_vendor_response(
            _response(blocks), ti.BoundedIdMap(),
        )
        _assert_only_text_notes(self, response["content"])
        anthropic, _ = ti.sanitise_for_anthropic(_request(blocks))
        self.assertEqual(anthropic["messages"], [])
        vendor, _ = ti.sanitise_for_vendor(_request(blocks))
        _assert_only_text_notes(self, vendor["messages"][0]["content"])

    def test_unknown_tool_result_with_non_anthropic_id(self) -> None:
        block = {
            "type": "future_tool_result", "tool_use_id": "call_future",
            "content": [{"type": "text", "text": FIXTURE_URL}],
        }
        response, _ = ti.normalise_vendor_response(
            _response([block]), ti.BoundedIdMap(),
        )
        _assert_only_text_notes(self, response["content"])
        anthropic, _ = ti.sanitise_for_anthropic(_request([block]))
        self.assertEqual(anthropic["messages"], [])
        vendor, _ = ti.sanitise_for_vendor(_request([block]))
        _assert_only_text_notes(self, vendor["messages"][0]["content"])

    def test_tool_result_inside_an_assistant_message(self) -> None:
        block = {
            "type": "tool_result", "tool_use_id": "call_fixture",
            "content": f"Read {FIXTURE_URL}",
        }
        response, _ = ti.normalise_vendor_response(
            _response([block]), ti.BoundedIdMap(),
        )
        _assert_only_text_notes(self, response["content"])
        anthropic, _ = ti.sanitise_for_anthropic(_request([block]))
        self.assertEqual(anthropic["messages"], [])
        vendor, _ = ti.sanitise_for_vendor(_request([block]))
        _assert_only_text_notes(self, vendor["messages"][0]["content"])

    def test_vendor_mcp_pair(self) -> None:
        # The full four-seam assertion lives in FourSeamTests; this row also
        # checks the recovery path's conversion counters.
        repairer = tr.TranscriptRepairer(strip_vendor_origin=True)
        repaired, changed = repairer.repair_content(_mcp_pair(), role="assistant")
        self.assertTrue(changed)
        _assert_only_text_notes(self, repaired)
        self.assertEqual(repairer.stats.blocks_converted, 1)
        self.assertEqual(repairer.stats.results_converted, 1)

    def test_unknown_type_outside_the_allow_list(self) -> None:
        block = {"type": "vendor_future_block", "payload": {"url": FIXTURE_URL}}
        response, _ = ti.normalise_vendor_response(
            _response([block]), ti.BoundedIdMap(),
        )
        _assert_only_text_notes(self, response["content"])
        anthropic, _ = ti.sanitise_for_anthropic(_request([block]))
        _assert_only_text_notes(self, anthropic["messages"][0]["content"])
        vendor, _ = ti.sanitise_for_vendor(_request([block]))
        _assert_only_text_notes(self, vendor["messages"][0]["content"])

    def test_unknown_type_beside_an_anthropic_id_is_conservatively_kept(self) -> None:
        block = {
            "type": "future_tool_result", "tool_use_id": "srvtoolu_01Future",
            "content": [],
        }
        original = _request([block])
        anthropic, stats = ti.sanitise_for_anthropic(original)
        self.assertFalse(stats.changed)
        self.assertIs(anthropic, original)
        vendor, stats = ti.sanitise_for_vendor(original)
        self.assertFalse(stats.changed)
        self.assertIs(vendor, original)

    def test_marked_vendor_thinking(self) -> None:
        block = {"type": "thinking", "thinking": "synthetic", "signature": "vct_abc123"}
        anthropic, _ = ti.sanitise_for_anthropic(_request([block, {"type": "text", "text": "ok"}]))
        self.assertEqual([b["type"] for b in anthropic["messages"][0]["content"]], ["text"])
        original = _request([block])
        vendor, stats = ti.sanitise_for_vendor(original)
        self.assertFalse(stats.changed)
        self.assertIs(vendor, original)

    def test_unmarked_long_nonhex_thinking_is_not_guessed_at(self) -> None:
        # Without the gateway marker this shape is indistinguishable from a
        # genuine signature. The GW-1 allow-list must not add a shape heuristic.
        block = {"type": "thinking", "thinking": "synthetic", "signature": "A" * 96}
        original = _request([block])
        anthropic, stats = ti.sanitise_for_anthropic(original)
        self.assertFalse(stats.changed)
        self.assertIs(anthropic, original)
        vendor, stats = ti.sanitise_for_vendor(original)
        self.assertFalse(stats.changed)
        self.assertIs(vendor, original)

    def test_redacted_thinking_is_allow_listed(self) -> None:
        block = {"type": "redacted_thinking", "data": "synthetic-opaque-data"}
        original = _request([block])
        vendor, stats = ti.sanitise_for_vendor(original)
        self.assertFalse(stats.changed)
        self.assertIs(vendor, original)
        response, stats = ti.normalise_vendor_response(
            _response([block]), ti.BoundedIdMap(),
        )
        self.assertFalse(stats.changed)
        self.assertIs(response["content"][0], block)

    def test_anthropic_advisor_pair_survives_but_vendor_advisor_does_not(self) -> None:
        native = _server_pair("advisor", "srvtoolu_01Advisor")
        original = _request(native)
        anthropic, stats = ti.sanitise_for_anthropic(original)
        self.assertFalse(stats.changed, stats.summary())
        self.assertIs(anthropic, original)
        vendor, stats = ti.sanitise_for_vendor(original)
        self.assertFalse(stats.changed, stats.summary())
        self.assertIs(vendor, original)

        foreign, _ = ti.sanitise_for_anthropic(_request(_server_pair("advisor")))
        self.assertEqual(foreign["messages"], [])

    def test_plain_tool_use_caller_and_input_survive(self) -> None:
        block = {
            "type": "tool_use", "id": "call_fixture", "name": "synthetic_tool",
            "caller": {"type": "code_execution_20260101", "tool_id": "srvtoolu_01Caller"},
            "input": {
                "type": "thinking", "signature": "vct_input_only",
                "id": "srvtoolu_vct_input_only", "url": FIXTURE_URL,
            },
        }
        original = _request([block])
        vendor, stats = ti.sanitise_for_vendor(original)
        self.assertFalse(stats.changed)
        self.assertIs(vendor, original)
        anthropic, stats = ti.sanitise_for_anthropic(original)
        self.assertFalse(stats.changed)
        self.assertIs(anthropic, original)

    def test_system_role_and_tool_reference_are_not_rewritten(self) -> None:
        message = {
            "role": "system",
            "content": [{"type": "tool_reference", "tool_name": "synthetic_tool"}],
        }
        original = {"messages": [message]}
        anthropic, stats = ti.sanitise_for_anthropic(original)
        self.assertFalse(stats.changed)
        self.assertIs(anthropic, original)
        vendor, stats = ti.sanitise_for_vendor(original)
        self.assertFalse(stats.changed)
        self.assertIs(vendor, original)


class ToolInputIsolationTests(unittest.TestCase):
    def test_id_restore_and_unmark_never_enter_tool_inputs_tools_or_system(self) -> None:
        mapped = "srvtoolu_vct_call_fixture"
        input_copy = {
            "id": mapped,
            "signature": "vct_input_only",
            "nested": [{"tool_use_id": mapped}],
        }
        payload = {
            "system": [{"type": "thinking", "signature": "vct_system"}],
            "tools": [{"id": mapped, "signature": "vct_tool_definition"}],
            "messages": [{
                "role": "assistant",
                "content": [
                    {"type": "server_tool_use", "id": mapped, "name": "web_search"},
                    {"type": "thinking", "thinking": "t", "signature": "vct_content"},
                    {"type": "tool_use", "id": "call_plain", "input": input_copy},
                ],
            }],
        }
        restored, restore_count = tr.restore_ids(payload, {mapped: "call_fixture"})
        self.assertEqual(restore_count, 1)
        self.assertEqual(restored["messages"][0]["content"][0]["id"], "call_fixture")
        self.assertEqual(restored["messages"][0]["content"][2]["input"], input_copy)
        self.assertEqual(restored["tools"], payload["tools"])

        unmarked, unmark_count = tr.unmark_thinking_signatures(payload)
        self.assertEqual(unmark_count, 1)
        self.assertEqual(
            unmarked["messages"][0]["content"][1]["signature"], "content",
        )
        self.assertEqual(unmarked["messages"][0]["content"][2]["input"], input_copy)
        self.assertEqual(unmarked["system"], payload["system"])
        self.assertEqual(unmarked["tools"], payload["tools"])


class FixTranscriptFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gw1-fixture-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _blob_entry(self) -> dict:
        blocks = [
            {"type": "text", "text": "synthetic prefix"},
            *_mcp_pair("call_first"),
            {"type": "text", "text": "synthetic middle"},
            {"type": "text", "text": "synthetic bridge"},
            *_mcp_pair("call_second"),
            {"type": "text", "text": "synthetic suffix"},
            {"type": "tool_use", "id": "call_plain", "name": "synthetic_tool",
             "input": {"url": FIXTURE_URL}},
        ]
        return {
            "type": "api-request-blob",
            "sessionId": "synthetic-session",
            "hash": "synthetic-hash-not-recomputed",
            "message": {"role": "assistant", "content": blocks},
        }

    def test_blob_rows_are_repaired_and_summary_counts_stay_accurate(self) -> None:
        path = self.root / "synthetic.jsonl"
        path.write_text(json.dumps(self._blob_entry()) + "\n", encoding="utf-8")
        out = StringIO()
        self.assertEqual(ft.run(path, out=out), ft.EXIT_OK)
        summary = out.getvalue()
        self.assertIn("blocks converted  : 2", summary)
        self.assertIn("results converted  : 2", summary)
        repaired = json.loads(path.read_text().splitlines()[0])
        content = repaired["message"]["content"]
        self.assertNotIn("mcp_tool_", json.dumps(content))
        self.assertIn(FIXTURE_URL, json.dumps(content))
        self.assertEqual(content[-1]["type"], "tool_use")
        self.assertEqual(content[-1]["input"], {"url": FIXTURE_URL})
        self.assertEqual(repaired["hash"], "synthetic-hash-not-recomputed")

        before = path.read_bytes()
        second = tr.repair_file(path)
        self.assertEqual(second.entries_touched, 0)
        self.assertEqual(path.read_bytes(), before)

    def test_json_summary_exposes_conversion_counts(self) -> None:
        path = self.root / "synthetic.jsonl"
        path.write_text(json.dumps(self._blob_entry()) + "\n", encoding="utf-8")
        out = StringIO()
        self.assertEqual(ft.run(path, dry_run=True, as_json=True, out=out), ft.EXIT_OK)
        summary = json.loads(out.getvalue())
        self.assertEqual(summary["blocks_converted"], 2)
        self.assertEqual(summary["results_converted"], 2)

    def test_emptied_thinking_entry_has_an_accurate_placeholder(self) -> None:
        entry = {
            "type": "assistant", "uuid": "synthetic-uuid",
            "parentUuid": "synthetic-parent",
            "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "synthetic", "signature": "vct_abc"},
            ]},
        }
        path = self.root / "thinking.jsonl"
        path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        tr.repair_file(path)
        repaired = json.loads(path.read_text().splitlines()[0])
        self.assertEqual(repaired["parentUuid"], "synthetic-parent")
        self.assertEqual(repaired["message"]["content"], [
            {"type": "text", "text": "[vendor thinking block removed by vco fix-transcript]"},
        ])


class VendorRequestIntegrationTests(GatewayTestBase):
    async def test_vendor_branch_sanitises_before_forwarding(self) -> None:
        response = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", **_request(_mcp_pair())},
        )
        self.assertEqual(response.status, 200)
        forwarded = json.loads(self.vendor_up.message_requests[-1]["body"])
        content = forwarded["messages"][0]["content"]
        _assert_only_text_notes(self, content)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
