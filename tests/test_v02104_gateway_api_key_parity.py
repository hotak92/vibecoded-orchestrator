# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 — the Claude API-key route behaves like the OAuth route.

Owner 2026-10-18: "make sure effort is wired too" and "make sure context
windows are correctly handled". Checked on EVERY API-key path — the explicit
``claude-gw/api/<id>`` route, the global API-key MODE, the subscription
fallback retry and the subscription-out latched path — against the OAuth
route as the reference:

* effort: ``output_config.effort`` and the ``thinking`` block (adaptive, or the
  older ``budget_tokens``) reach Anthropic byte-equal (the first-party rule in
  ``effort.py`` is "forward as is"), and the ledger's ``effort_requested`` /
  ``effort_forwarded`` are filled;
* context: the ledger's window fields match the OAuth row for the same model
  (the explicit route's namespaced id used to miss the context table —
  ``window_actual`` read ``unknown`` on every API-key row, found live
  2026-10-18), the ``[1m]`` id sends the 1M beta with the key,
  ``count_tokens`` goes through the key route, a "prompt is too long" 400 is
  relayed verbatim (as on OAuth: the client handles Anthropic's own shape),
  and a streamed answer's ``usage`` is relayed untouched.

The live run (scratch gateway on :11461, 2026-10-18) that these mirror:
Haiku 5.5 accepted ``effort: low`` and ``effort: high`` on the key (200);
``count_tokens`` answered ``{"input_tokens": 13}``; a ``[1m]`` Sonnet 5.5
request with the 1M beta was ACCEPTED for this Console key (200, tiny prompt
— whether the org's long-context tier serves >200K prompts was not probed).

No network: loopback stubs; every key is an obvious fake.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from model_router import server as srv
from model_router.config import ANTHROPIC_AUTH_API_KEY
from model_router.model_family import parse_model_id
from model_router.routing import api_key_remainder
from model_router.vendors import CONTEXT_1M_BETA_MARKER
from tests.test_v02102_gateway_routing import RATE_LIMITED, _SeqUpstream, _success
from tests.test_v02104_gateway_api_key_route import (
    API_NS,
    FAKE_API_KEY,
    HAIKU,
    OPUS,
    ApiKeyGatewayBase,
    _header,
)
from tests.test_v02104_gateway_quota_latch import _claim, _refusal

PATHS = ("explicit", "mode", "fallback", "latched")
EFFORT_FIELDS = ("output_config", "thinking")

_SSE = [
    b'event: message_start\ndata: {"type":"message_start","message":{"id":"m",'
    b'"type":"message","role":"assistant","model":"claude-opus-5-5","content":[],'
    b'"usage":{"input_tokens":1200,"cache_creation_input_tokens":300,'
    b'"cache_read_input_tokens":4000,"output_tokens":1}}}\n\n',
    b'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
    b'"content_block":{"type":"text","text":""}}\n\n',
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":"ok"}}\n\n',
    b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
    b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":'
    b'"end_turn"},"usage":{"output_tokens":7}}\n\n',
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
]


class _StreamingSeq(_SeqUpstream):
    """``_SeqUpstream`` whose scripted body may be a LIST of SSE chunks."""

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        if self.script and isinstance(self.script[0][2], list):
            await self._record(request)
            entry = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            status, _headers, chunks = entry
            response = web.StreamResponse(
                status=status, headers={"Content-Type": "text/event-stream"},
            )
            response.enable_chunked_encoding()
            await response.prepare(request)
            for chunk in chunks:
                await response.write(chunk)
            await response.write_eof()
            return response
        return await super()._messages(request)


class ParityBase(ApiKeyGatewayBase):

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # Swap in the streaming-capable stub at the same address.
        await self.first.stop()
        self.first = _StreamingSeq()
        url = await self.first.start()
        self.addAsyncCleanup(self.first.stop)
        import dataclasses
        self.anthropic = dataclasses.replace(self.anthropic, upstream=url)

    async def client_for(self, path: str) -> Any:
        client = await self.client_with(key=True, fallback=path in ("fallback", "latched"))
        gateway = client.app[srv.APP_KEY]
        if path == "mode":
            gateway.anthropic_auth.reader = lambda: ANTHROPIC_AUTH_API_KEY
            await gateway.anthropic_auth.refresh()
        if path == "latched":
            gateway.quota_latch.note_refusal(
                OPUS, _refusal(_claim("five_hour", time.time() + 3600)), [],
            )
        return client

    @staticmethod
    def model_for(path: str, model: str = OPUS) -> str:
        return f"{API_NS}{model}" if path == "explicit" else model

    def answers(self, path: str, ok: Any) -> None:
        """The first-party stub's script for ``path``: the fallback path
        first gets the subscription's quota refusal."""
        self.first.script = (
            [(429, {}, RATE_LIMITED), (200, {}, ok)] if path == "fallback"
            else [(200, {}, ok)]
        )

    def key_requests(self) -> list[dict]:
        return [
            r for r in self.first.message_requests
            if _header(r["headers"], "x-api-key") == FAKE_API_KEY
        ]

    async def send(self, client: Any, path: str, payload: dict, *, url: str = "/v1/messages",
                   extra_headers: dict | None = None) -> Any:
        return await client.post(
            url, json=payload, headers={**self.headers(), **(extra_headers or {})},
        )


class EffortParityTests(ParityBase):

    async def _check(self, path: str, extra: dict, expect: str) -> None:
        client = await self.client_for(path)
        self.answers(path, json.dumps(_success(OPUS)).encode())
        payload = {**self.ask(self.model_for(path)), "max_tokens": 64, **extra}
        resp = await self.send(client, path, payload)
        self.assertEqual(resp.status, 200, path)
        [sent] = self.key_requests()
        forwarded = json.loads(sent["body"])
        for field in EFFORT_FIELDS:
            self.assertEqual(forwarded.get(field), extra.get(field), f"{path}: {field}")
        [row] = await self.rows(client)
        self.assertEqual(row["auth"], "api_key")
        self.assertEqual((row["effort_requested"], row["effort_forwarded"]), (expect, expect), path)
        await client.close()

    async def test_output_config_effort_and_adaptive_thinking_on_every_path(self) -> None:
        extra = {"output_config": {"effort": "high"}, "thinking": {"type": "adaptive"}}
        for path in PATHS:
            with self.subTest(path=path):
                self.first.requests.clear()
                self.usage_ledger_path.unlink(missing_ok=True)
                await self._check(path, extra, "high")

    async def test_budget_tokens_thinking_on_every_path(self) -> None:
        extra = {"thinking": {"type": "enabled", "budget_tokens": 2048}}
        for path in PATHS:
            with self.subTest(path=path):
                self.first.requests.clear()
                self.usage_ledger_path.unlink(missing_ok=True)
                await self._check(path, extra, "budget_tokens=2048")

    async def test_every_effort_level_is_forwarded_as_is(self) -> None:
        for level in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(level=level):
                self.first.requests.clear()
                self.usage_ledger_path.unlink(missing_ok=True)
                await self._check("explicit", {"output_config": {"effort": level}}, level)


class ContextParityTests(ParityBase):

    async def _oauth_row(self, model: str) -> dict:
        client = await self.client_with(key=True, fallback=False)
        self.first.script = [(200, {}, json.dumps(_success(model)).encode())]
        await self.send(client, "oauth", self.ask(model))
        [row] = await self.rows(client)
        await client.close()
        self.usage_ledger_path.unlink(missing_ok=True)
        self.first.requests.clear()
        return row

    async def test_window_fields_match_the_oauth_row_on_every_path(self) -> None:
        reference = await self._oauth_row(OPUS)
        self.assertIsNotNone(reference["window_actual"])
        fields = ("window_actual", "window_source", "context_tokens", "pct_actual")
        for path in PATHS:
            with self.subTest(path=path):
                client = await self.client_for(path)
                self.answers(path, json.dumps(_success(OPUS)).encode())
                await self.send(client, path, self.ask(self.model_for(path)))
                [row] = await self.rows(client)
                self.assertEqual(row["auth"], "api_key")
                for field in fields:
                    self.assertEqual(row[field], reference[field], f"{path}: {field}")
                # The client budgets a NAMESPACED id it does not natively know
                # at its default; a plain Claude id (mode/fallback/latched)
                # keeps the OAuth row's client window.
                expect_client = 200_000 if path == "explicit" else reference["window_client"]
                self.assertEqual(row["window_client"], expect_client, path)
                await client.close()
                self.usage_ledger_path.unlink(missing_ok=True)
                self.first.requests.clear()

    async def test_the_1m_id_sends_the_beta_with_the_key_and_budgets_1m(self) -> None:
        client = await self.client_for("explicit")
        self.answers("explicit", json.dumps(_success(OPUS)).encode())
        await self.send(client, "explicit", self.ask(f"{API_NS}{OPUS}[1m]"))
        [sent] = self.key_requests()
        self.assertIn(CONTEXT_1M_BETA_MARKER, _header(sent["headers"], "anthropic-beta") or "")
        self.assertEqual(json.loads(sent["body"])["model"], OPUS)
        [row] = await self.rows(client)
        self.assertEqual(row["window_client"], 1_000_000)
        self.assertEqual(row["window_actual"], 1_000_000)

    async def test_the_clients_own_1m_beta_rides_the_key_on_every_path(self) -> None:
        beta = "claude-code-20250219,context-1m-2025-08-07"
        for path in PATHS:
            with self.subTest(path=path):
                client = await self.client_for(path)
                self.answers(path, json.dumps(_success(OPUS)).encode())
                await self.send(client, path, self.ask(self.model_for(path)),
                                extra_headers={"anthropic-beta": beta})
                [sent] = self.key_requests()
                self.assertIn("context-1m-2025-08-07", _header(sent["headers"], "anthropic-beta"))
                [row] = await self.rows(client)
                self.assertEqual(row["window_client"], 1_000_000, path)
                await client.close()
                self.usage_ledger_path.unlink(missing_ok=True)
                self.first.requests.clear()

    async def test_count_tokens_goes_through_the_key_route(self) -> None:
        for path in ("explicit", "mode"):
            with self.subTest(path=path):
                client = await self.client_for(path)
                self.first.script = [(200, {}, b'{"input_tokens":13}')]
                resp = await self.send(
                    client, path, {"model": self.model_for(path, HAIKU),
                                   "messages": [{"role": "user", "content": "Say hi."}]},
                    url="/v1/messages/count_tokens",
                )
                self.assertEqual(resp.status, 200)
                self.assertEqual(await resp.json(), {"input_tokens": 13})
                [sent] = self.first.message_requests
                self.assertEqual(sent["path"], "/v1/messages/count_tokens")
                self.assertEqual(_header(sent["headers"], "x-api-key"), FAKE_API_KEY)
                self.assertIsNone(_header(sent["headers"], "authorization"))
                self.assertEqual(json.loads(sent["body"])["model"], HAIKU)
                await client.close()
                self.first.requests.clear()

    async def test_prompt_too_long_is_relayed_verbatim_as_on_oauth(self) -> None:
        overflow = (
            b'{"type":"error","error":{"type":"invalid_request_error",'
            b'"message":"prompt is too long: 1000500 tokens > 1000000 maximum"}}'
        )
        for path in ("explicit", "mode", "latched"):
            with self.subTest(path=path):
                client = await self.client_for(path)
                self.first.script = [(400, {}, overflow)]
                resp = await self.send(client, path, self.ask(self.model_for(path)))
                self.assertEqual(resp.status, 400)
                self.assertEqual(await resp.read(), overflow)
                self.assertEqual(len(self.first.message_requests), 1, path)
                await client.close()
                self.first.requests.clear()

    async def test_streamed_usage_is_relayed_untouched(self) -> None:
        for path in PATHS:
            with self.subTest(path=path):
                client = await self.client_for(path)
                self.answers(path, list(_SSE))
                payload = {**self.ask(self.model_for(path)), "stream": True}
                resp = await self.send(client, path, payload)
                self.assertEqual(resp.status, 200)
                self.assertEqual(await resp.read(), b"".join(_SSE), path)
                [row] = await self.rows(client)
                self.assertEqual(
                    (row["input_tokens"], row["cache_creation_input_tokens"],
                     row["cache_read_input_tokens"], row["output_tokens"]),
                    (1200, 300, 4000, 7), path,
                )
                self.assertEqual(row["auth"], "api_key")
                await client.close()
                self.usage_ledger_path.unlink(missing_ok=True)
                self.first.requests.clear()


class NamespaceReadingTests(ParityBase):

    def test_the_api_namespace_reads_the_first_party_id(self) -> None:
        self.assertEqual(api_key_remainder(f"{API_NS}{OPUS}[1m]"), f"{OPUS}[1m]")
        self.assertIsNone(api_key_remainder("claude-gw/glm-5.3"))
        self.assertIsNone(api_key_remainder(OPUS))
        parts = parse_model_id(f"{API_NS}{OPUS}[1m]")
        self.assertEqual((parts.family, parts.bare_id, parts.one_m), ("claude-opus", OPUS, True))


LIVE_API = (
    Path(__file__).resolve().parent / "fixtures" / "model_router"
    / "anthropic-api-key-live-2026-10-18.json"
)


class LiveApiKeyShapeTests(ParityBase):
    """Replays of the 2026-10-18 live captures on the API-key route."""

    def live(self) -> dict:
        return json.loads(LIVE_API.read_text(encoding="utf-8"))

    async def test_the_key_routes_org_rate_limit_headers_never_feed_the_usage_windows(self) -> None:
        live = self.live()
        # Live: an API-key answer carries the ORGANISATION's rate-limit
        # headers and none of the subscription's unified ones.
        self.assertEqual(live["unified_headers_on_api_key_route"], [])
        client = await self.client_for("explicit")
        body = json.dumps({**_success(HAIKU), "usage": live["success"]["usage"]}).encode()
        # Plus a unified header, as a guard: even if one ever appeared on a
        # key answer it describes no subscription window.
        headers = {**live["success"]["ratelimit_headers"],
                   "anthropic-ratelimit-unified-5h-utilization": "0.99"}
        self.first.script = [(200, headers, body)]
        resp = await self.send(client, "explicit", self.ask(f"{API_NS}{HAIKU}"))
        self.assertEqual(resp.status, 200)
        gateway = client.app[srv.APP_KEY]
        self.assertEqual(gateway.usage_windows._header_windows, {})
        [row] = await self.rows(client)
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (13, 8))

    async def test_the_real_invalid_model_404_is_relayed_and_leaves_the_key_usable(self) -> None:
        live = self.live()["invalid_model"]
        raw = json.dumps(live["body"]).encode()
        client = await self.client_for("explicit")
        self.first.script = [(live["status"], {}, raw)]
        resp = await self.send(client, "explicit", self.ask(live["request_model"]))
        self.assertEqual(resp.status, 404)
        self.assertEqual(await resp.read(), raw)
        auth = (await (await client.get("/health")).json())["anthropic_auth"]
        self.assertNotEqual(auth["api_key"], "rejected")
