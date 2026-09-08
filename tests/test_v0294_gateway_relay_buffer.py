# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Buffering a vendor's JSON response: "at most n bytes", not "the next chunk".

``StreamReader.read(n)`` reads like a promise of n bytes and is not one.
aiohttp 3.x waits only until the buffer is non-empty and then returns what has
ARRIVED, trimmed to n — so a body still in flight across several TCP segments
(every response over about one segment, which is every real assistant turn
with a tool call in it) came back cut at whatever boundary the first wakeup
found. The consequences ran past the missed rewrite this branch exists for:

* ``json.loads`` fails on the fragment, so the normalisation is skipped
  silently and the CLIENT is handed a truncated JSON document;
* the fragment is relayed under the upstream's own headers, so what the client
  sees is a half document whose length does not match anything.

Both halves are pinned here: the accumulator's own contract against a reader
with the real semantics, and the whole relay against a stub upstream that
writes its body in several separated chunks.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import AsyncIterator, cast
from unittest import mock

import aiohttp

from model_router import server

from tests.test_model_router_server import GatewayTestBase


class _ChunkReader:
    """A ``StreamReader`` stand-in with aiohttp 3.x's ACTUAL read semantics.

    ``read(n)`` returns what has ARRIVED, trimmed to n — never n bytes waited
    for — which is the whole defect in one method. Modelling "one chunk has
    arrived" is the worst case of that and the one a slow upstream produces.
    The fake is the honest way to pin it: a real socket would make the split
    a timing race, and a test that only passes when the segments happen to
    arrive separately proves nothing on the run where they do not.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)

    async def read(self, n: int = -1) -> bytes:
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if 0 <= n < len(chunk):
            self.chunks.insert(0, chunk[n:])
            return chunk[:n]
        return chunk

    async def readany(self) -> bytes:
        return await self.read(-1)

    async def _iter(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self.readany()
            if not chunk:
                return
            yield chunk

    def iter_any(self) -> AsyncIterator[bytes]:
        return self._iter()


def _reader(chunks: list[bytes]) -> aiohttp.StreamReader:
    return cast(aiohttp.StreamReader, _ChunkReader(chunks))


class BufferBoundedTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_body_split_across_chunks_arrives_whole(self) -> None:
        chunks = [b'{"a":', b'1,"b":', b'2}']
        raw, overflowed = await server._buffer_bounded(_reader(chunks), 1024)
        self.assertEqual(raw, b'{"a":1,"b":2}')
        self.assertFalse(overflowed)

    async def test_one_read_call_would_have_returned_only_the_first_chunk(
        self,
    ) -> None:
        """The defect, stated as a property of the reader rather than a story.

        If this assertion ever fails, ``read(n)`` has become "n bytes" and the
        accumulator could be simplified — but until then the accumulator is
        the only correct shape.
        """
        reader = _ChunkReader([b'{"a":', b'1}'])
        self.assertEqual(await reader.read(1024), b'{"a":')

    async def test_an_empty_body_is_empty_and_not_an_overflow(self) -> None:
        raw, overflowed = await server._buffer_bounded(_reader([]), 8)
        self.assertEqual(raw, b"")
        self.assertFalse(overflowed)

    async def test_the_bound_is_on_what_is_held_and_reports_itself(self) -> None:
        raw, overflowed = await server._buffer_bounded(
            _reader([b"aaaa", b"bbbb", b"cccc"]), 6,
        )
        self.assertTrue(overflowed)
        self.assertEqual(
            raw, b"aaaabbbb",
            "the chunk that crossed the bound is KEPT: the caller writes it "
            "before streaming the rest, so no byte is read twice or dropped",
        )

    async def test_a_body_exactly_at_the_bound_is_not_an_overflow(self) -> None:
        raw, overflowed = await server._buffer_bounded(_reader([b"12345678"]), 8)
        self.assertFalse(overflowed)
        self.assertEqual(raw, b"12345678")


class ChunkedVendorJsonRelayTests(GatewayTestBase):
    """The same defect through the whole gateway, on a real socket.

    The stub writes its body in separated chunks, so the gateway's first read
    genuinely sees only part of it — the field shape, not a constructed one.
    """

    GAP_S = 0.05

    def _body(self) -> dict:
        return {
            "id": "msg_1",
            "model": "stub",
            "content": [
                {
                    "type": "server_tool_use",
                    "id": "call_abc123",
                    "name": "web_search",
                    "input": {"query": "x"},
                },
                {"type": "text", "text": "y" * 64},
            ],
        }

    def _arm(self, payload: dict, *, pieces: int = 3) -> bytes:
        raw = json.dumps(payload).encode("utf-8")
        step = max(1, len(raw) // pieces)
        self.vendor_up.json_chunks = [
            raw[i:i + step] for i in range(0, len(raw), step)
        ]
        self.vendor_up.stream_gap_s = self.GAP_S
        return raw

    async def _post(self):
        return await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )

    async def test_a_chunked_json_response_reaches_the_client_whole(self) -> None:
        self._arm(self._body())
        resp = await self._post()
        self.assertEqual(resp.status, 200)
        body = await resp.read()
        payload = json.loads(body)
        self.assertEqual(len(payload["content"]), 2)
        self.assertEqual(payload["content"][0]["id"], "srvtoolu_vct_call_abc123")
        self.assertEqual(payload["content"][1]["text"], "y" * 64)
        self.assertEqual(
            resp.headers.get("Content-Length"), str(len(body)),
            "a truncated relay also mislabels its own length",
        )

    async def test_a_chunked_response_needing_no_rewrite_is_still_whole(
        self,
    ) -> None:
        """The leave-alone half: nothing to normalise, still not truncated."""
        clean = {"id": "msg_2", "model": "stub", "content": [
            {"type": "text", "text": "z" * 128},
        ]}
        raw = self._arm(clean)
        resp = await self._post()
        self.assertEqual(await resp.read(), raw)

    async def test_an_oversized_chunked_response_is_relayed_complete(
        self,
    ) -> None:
        """Past the bound the bytes are relayed UNREWRITTEN — and entire.

        The prefix already read is written first and the remainder streamed,
        so the overflow path is a passthrough, not a discard.
        """
        raw = self._arm(self._body(), pieces=4)
        with mock.patch.object(server, "JSON_BUFFER_LIMIT_BYTES", 8):
            with self.assertLogs("model_router.server", level="WARNING"):
                resp = await self._post()
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            await resp.read(), raw,
            "over the bound the body is relayed verbatim, whole",
        )

    async def test_a_slow_upstream_does_not_shorten_the_body(self) -> None:
        """Two chunks a whole timeout-scale apart: still one document."""
        payload = {"id": "msg_3", "model": "stub", "content": []}
        raw = json.dumps(payload).encode("utf-8")
        self.vendor_up.json_chunks = [raw[:4], raw[4:]]
        self.vendor_up.stream_gap_s = 0.2
        resp = await self._post()
        self.assertEqual(await resp.read(), raw)


class QuotaPeekIsDeliberateTests(unittest.TestCase):
    """The one ``read(n)`` that stays, and the reason it may.

    ``_quota_response`` never relays the body it reads — it mines it for a
    reset hint and logs it at DEBUG — so a short read costs at most a hint
    that was in a later chunk. This pins the asymmetry so a future sweep does
    not "fix" it into an unbounded read of a body that is thrown away.
    """

    def test_only_the_quota_peek_still_calls_read(self) -> None:
        source = Path(server.__file__).read_text(encoding="utf-8")
        calls = [
            line.strip() for line in source.splitlines()
            if "upstream.content.read(" in line
        ]
        self.assertEqual(
            calls, ["raw = await upstream.content.read(_QUOTA_BODY_PEEK_BYTES)"],
            "a relayed body must be accumulated, not read() once",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
