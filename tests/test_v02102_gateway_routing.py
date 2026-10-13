# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 lane W3-B — gateway routing: fallback ladder (A4/F2), clear_thinking (A8), NB-07.

Three shipped behaviours, each pinned red-first against the pre-fix tree:

* **A4/F2 — the owner's fallback ladder (OD-9).** A Claude haiku-slot request
  whose first-party answer is a qualifying 429 (the P299:106/:406 incident:
  the client rendered a bare "Claude API Error" toast and the session sat
  idle) escalates to glm-5.3-flash on z.ai; a z.ai quota verdict on that rung
  escalates to qwen3.8-flash on QwenCloud; the last rung's refusal IS the
  client's answer. Trigger (OD-9 verbatim): fall back on a 429 WITHOUT a
  readable ``retry-after`` or with a quota-exhausted body; on a 429 WITH
  ``retry-after`` <= 30 s (env-tunable) retry the same rung once, then fall
  back. R15: every fallback is visible — the response names the model that
  answered, the access line carries ``note=model_fallback fallback_from=…``,
  and the usage ledger row carries ``fallback_from``.
* **A8 — ``thinking.clear_thinking`` (OD-10).** A cited per-row SEED field
  (``clear_thinking``; default absent → not sent) merged into a request that
  already carries a ``thinking`` param. The seed's glm-5.3-flash row carries
  ``false``; a row without the field sends nothing extra.
* **NB-07 — suffixed dev builds in the gateway self-test.** ``0.2.102+abc``
  compares as its release part (equal-or-newer), not as unrankable.

X2 (z.ai ``tool_stream``) has NO code here on purpose: no cached vendor
citation in the repo addresses a ``tool_stream`` request parameter either
way, so the lane stopped on that sub-item and reported "needs vendor doc"
(the shipped captures — zai-stream/zai-websearch — show today's no-param
streamed tool calls already answered 200, so there is no observed failure to
fix, but absence of a requirement is not evidence of one).

No network: the upstreams are the loopback stubs from
:mod:`tests.test_model_router_server`.
"""
from __future__ import annotations

import dataclasses
import json
import unittest
from typing import Any, Optional

from aiohttp import web

from model_router import routing
from model_router.context_table import load_seed
from model_router.routing import Route
from model_router.server import (
    LADDER_FALLBACK,
    LADDER_RETRY,
    _apply_clear_thinking,
    _ladder_verdict,
)
from model_router.usage import build_record
from model_router.vendors import FALLBACK_LADDER, VENDORS
from tests.test_model_router_server import FAKE_VENDOR_KEY, GatewayTestBase, _Upstream

LOGGER = "model_router.server"

HAIKU = "claude-haiku-5-5"
ZAI_GLM = "claude-gw/glm-5.3"
ZAI_FLASH = "claude-gw/glm-5.3-flash"
QWEN_FLASH = "claude-gw/qwen/qwen3.8-flash"

#: A quota-exhausted refusal shape (the z.ai 1310 code, verbatim incident
#: form). Chosen over a first-party spelling because ``classify_quota`` reads
#: it through the vendor's own code — the strongest evidence tier there is.
EXHAUSTED = b'{"error":{"code":"1310","message":"Weekly/Monthly Limit Exhausted"}}'
#: A rate-limited shape with NO readable reset: generic type, no code words.
RATE_LIMITED = b'{"error":{"type":"rate_limit_error"}}'


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


class _SeqUpstream(_Upstream):
    """A stub whose ``/v1/messages`` answers a scripted sequence.

    Each request pops the next ``(status, headers, body)``; the last entry
    repeats, so a test that expects N upstream attempts scripts N (or N-1)
    entries and the count of recorded requests is the assertion.
    """

    def __init__(self) -> None:
        super().__init__()
        self.script: list[tuple[int, dict[str, str], bytes]] = []

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        if self.script:
            await self._record(request)
            entry = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            status, headers, raw = entry
            return web.Response(
                body=raw, status=status, headers=headers,
                content_type="application/json",
            )
        return await super()._messages(request)


# ══════════════════════════════════════════════════════════════════════════
# A4/F2 — the ladder table and the pure route hook
# ══════════════════════════════════════════════════════════════════════════
def _first_party(model: str) -> Route:
    return Route(
        upstream="https://api.anthropic.com",
        forward_model=model,
        vendor=None,
        family_id="anthropic",
        is_anthropic=True,
    )


class FallbackRouteTests(unittest.TestCase):
    """The pure half: WHERE each rung escalates to. No I/O, no vendor literal."""

    def test_haiku_slot_escalates_to_zai_flash(self) -> None:
        rung = routing.fallback_route(_first_party(HAIKU))
        self.assertIsNotNone(rung)
        assert rung is not None  # noqa: S101 — narrowed for the type checker
        self.assertIs(rung.vendor, VENDORS["zai"])
        self.assertEqual(rung.forward_model, "glm-5.3-flash")
        self.assertFalse(rung.is_anthropic)
        self.assertEqual(rung.family_id, "zai")

    def test_any_haiku_generation_is_the_slot(self) -> None:
        # A substring marker, so the generation the client spells is one rung.
        for model in ("claude-haiku-5-5", "claude-haiku-4-5", "claude-haiku-9", "haiku-3"):
            self.assertIsNotNone(
                routing.fallback_route(_first_party(model)), model,
            )

    def test_zai_flash_escalates_to_qwen_flash(self) -> None:
        first = routing.fallback_route(_first_party(HAIKU))
        assert first is not None  # noqa: S101
        second = routing.fallback_route(first)
        self.assertIsNotNone(second)
        assert second is not None  # noqa: S101
        self.assertIs(second.vendor, VENDORS["qwen"])
        self.assertEqual(second.forward_model, "qwen3.8-flash")

    def test_the_last_rung_has_no_fallback(self) -> None:
        second = routing.fallback_route(routing.fallback_route(_first_party(HAIKU)))
        assert second is not None  # noqa: S101
        self.assertIsNone(routing.fallback_route(second))

    def test_a_non_haiku_first_party_request_has_no_rung(self) -> None:
        self.assertIsNone(routing.fallback_route(_first_party("claude-opus-5-5")))

    def test_an_unrelated_vendor_route_has_no_rung(self) -> None:
        kimi = VENDORS["kimi"]
        self.assertIsNone(
            routing.fallback_route(
                Route(
                    upstream=kimi.upstream, forward_model="k3", vendor=kimi,
                    family_id="kimi", is_anthropic=False,
                ),
            ),
        )

    def test_a_target_missing_from_the_callers_registry_is_no_ladder(self) -> None:
        # A test/embedder registry without the target row: no half-route, the
        # original refusal stands.
        self.assertIsNone(
            routing.fallback_route(_first_party(HAIKU), vendors={"kimi": VENDORS["kimi"]}),
        )


class LadderTableIntegrityTests(unittest.TestCase):
    """Every rung names a real, servable id — data, checked like the registry."""

    def test_every_target_is_a_model_its_row_is_known_to_serve(self) -> None:
        for rung in FALLBACK_LADDER:
            with self.subTest(rung=rung):
                vendor = VENDORS.get(rung.to_vendor_id)
                self.assertIsNotNone(vendor, rung.to_vendor_id)
                assert vendor is not None  # noqa: S101
                served = {
                    *vendor.verified_ids,
                    *vendor.static_ids,
                    *vendor.catalog_hide_ids,
                }
                self.assertIn(rung.to_model_id.lower(), {
                    i.lower() for i in served
                })
                # A vendor rung must LEAVE an id its own row serves, or the
                # ladder would be keyed on a route that cannot occur.
                if rung.from_vendor_id:
                    source = VENDORS[rung.from_vendor_id]
                    self.assertIn(rung.from_model.lower(), {
                        i.lower() for i in (
                            *source.verified_ids, *source.static_ids,
                        )
                    })


# ══════════════════════════════════════════════════════════════════════════
# A4/F2 — the OD-9 trigger verdict
# ══════════════════════════════════════════════════════════════════════════
class LadderVerdictTests(unittest.TestCase):

    def test_a_quota_exhausted_body_falls_back(self) -> None:
        self.assertEqual(
            _ladder_verdict(429, EXHAUSTED, {}),
            (LADDER_FALLBACK, None),
        )

    def test_a_402_is_a_balance_condition_and_falls_back(self) -> None:
        self.assertEqual(_ladder_verdict(402, b"{}", {}), (LADDER_FALLBACK, None))

    def test_a_429_without_retry_after_falls_back(self) -> None:
        self.assertEqual(
            _ladder_verdict(429, RATE_LIMITED, {}),
            (LADDER_FALLBACK, None),
        )

    def test_a_short_retry_after_is_retried(self) -> None:
        action, delay = _ladder_verdict(
            429, RATE_LIMITED, {"Retry-After": "1"},
        )
        self.assertEqual(action, LADDER_RETRY)
        self.assertIsNotNone(delay)
        assert delay is not None  # noqa: S101
        self.assertAlmostEqual(delay, 1.0, places=3)

    def test_a_zero_retry_after_is_retried(self) -> None:
        """B-2: ``retry-after: 0`` means "retry immediately" — a free
        same-rung retry, not a reason to spend the next vendor's quota."""
        action, delay = _ladder_verdict(
            429, RATE_LIMITED, {"Retry-After": "0"},
        )
        self.assertEqual(action, LADDER_RETRY)
        self.assertIsNotNone(delay)
        assert delay is not None  # noqa: S101
        self.assertEqual(delay, 0.0)

    def test_a_window_longer_than_the_threshold_falls_back(self) -> None:
        self.assertEqual(
            _ladder_verdict(429, RATE_LIMITED, {"Retry-After": "31"}),
            (LADDER_FALLBACK, None),
        )


# ══════════════════════════════════════════════════════════════════════════
# A4/F2 — the request path
# ══════════════════════════════════════════════════════════════════════════
class LadderTestBase(GatewayTestBase):
    """The stock fixture plus a qwen stub, so the whole ladder is walkable."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.qwen_up = _Upstream()
        qwen_url = await self.qwen_up.start()
        self.addAsyncCleanup(self.qwen_up.stop)
        # Full registry rows (verified/static ids included), upstream only
        # repointed at the loopback stubs — the ladder keys on real ids.
        self.vendor = dataclasses.replace(
            VENDORS["zai"],
            upstream=str(self.vendor_up.server.make_url("")).rstrip("/"),
        )
        self.qwen = dataclasses.replace(VENDORS["qwen"], upstream=qwen_url)
        self.client = await self.make_client(
            vendors={"zai": self.vendor, "qwen": self.qwen},
        )

    def chat(self, session: str = "chat-1") -> dict[str, str]:
        return {**self.auth(), "x-claude-code-session-id": session}

    def ask(self, model: str, **payload: Any) -> dict[str, Any]:
        return {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            **payload,
        }

    async def rows(self, client: Any = None) -> list[dict]:
        """The ledger rows once the gateway that SERVED the request is quiet.

        W6-B2 last round: pass the client a test built itself — quiescing
        ``self.client``'s gateway while the request ran on another one is the
        race the reprobe test flaked on (both write one ledger file)."""
        from model_router import server as srv

        await (client or self.client).app[srv.APP_KEY].quiesce()  # handlers finished + appends drained
        path = self.usage_ledger_path
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def refuse(self, up: _Upstream, body: bytes = EXHAUSTED) -> None:
        up.messages_status = 429
        up.messages_raw = body
        up.content_type = "application/json"


class FallbackLadderTests(LadderTestBase):

    async def test_quota_exhausted_haiku_escalates_to_zai_flash(self) -> None:
        """The red-proof case: a fake first-party 429 in the quota-exhausted
        shape → the next rung is called, and BOTH the response and the ledger
        name the model that actually answered."""
        self.refuse(self.anthropic_up)
        self.vendor_up.messages_body = _success("glm-5.3-flash")
        resp = await self.client.post(
            "/v1/messages", json=self.ask(HAIKU), headers=self.chat(),
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["model"], "glm-5.3-flash")
        # The rung received a full request: its own model id, its own key.
        self.assertEqual(len(self.vendor_up.message_requests), 1)
        sent = json.loads(self.vendor_up.message_requests[0]["body"])
        self.assertEqual(sent["model"], "glm-5.3-flash")
        self.assertEqual(
            self.vendor_up.message_requests[0]["headers"]["Authorization"],
            f"Bearer {FAKE_VENDOR_KEY}",
        )
        # One first-party attempt — the verdict was OD-9-qualifying, so no
        # same-rung retry was spent.
        self.assertEqual(len(self.anthropic_up.message_requests), 1)
        # R15: the ledger row names the model that answered AND the rung the
        # request escaped.
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["requested"], HAIKU)
        self.assertEqual(rows[0]["forward"], "glm-5.3-flash")
        self.assertIn(f"anthropic:{HAIKU}", rows[0]["fallback_from"])

    async def test_the_escalated_attempt_is_visible_on_the_access_line(self) -> None:
        self.refuse(self.anthropic_up)
        self.vendor_up.messages_body = _success("glm-5.3-flash")
        with self.assertLogs(LOGGER, level="INFO") as cm:
            resp = await self.client.post(
                "/v1/messages", json=self.ask(HAIKU), headers=self.chat(),
            )
        self.assertEqual(resp.status, 200)
        joined = "\n".join(cm.output)
        self.assertIn("note=model_fallback", joined)
        self.assertIn(f"fallback_from=anthropic:{HAIKU}", joined)

    async def test_a_transient_429_with_short_retry_after_retries_once(self) -> None:
        """OD-9's leave-alone: a short window is waited out on the SAME rung —
        one retry, no fallback, no second vendor's quota spent."""
        anthropic = _SeqUpstream()
        url = await anthropic.start()
        self.addAsyncCleanup(anthropic.stop)
        from model_router import server as srv

        gateway_app = self.client.app
        gateway_app[srv.APP_KEY].anthropic = dataclasses.replace(
            gateway_app[srv.APP_KEY].anthropic, upstream=url,
        )
        anthropic.script = [
            (429, {"Retry-After": "1"}, RATE_LIMITED),
            (200, {}, json.dumps(_success(HAIKU)).encode()),
        ]
        self.vendor_up.messages_body = _success("glm-5.3-flash")
        resp = await self.client.post(
            "/v1/messages", json=self.ask(HAIKU), headers=self.chat(),
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["model"], HAIKU)
        # One retry on the same rung, and the ladder never armed a rung call.
        self.assertEqual(len(anthropic.message_requests), 2)
        self.assertEqual(len(self.vendor_up.message_requests), 0)
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fallback_from"], "")

    async def test_a_zero_retry_after_retries_once_without_falling_back(self) -> None:
        """B-2 end-to-end: ``retry-after: 0`` spends the free same-rung retry,
        not the next rung's quota."""
        anthropic = _SeqUpstream()
        url = await anthropic.start()
        self.addAsyncCleanup(anthropic.stop)
        from model_router import server as srv

        gateway_app = self.client.app
        gateway_app[srv.APP_KEY].anthropic = dataclasses.replace(
            gateway_app[srv.APP_KEY].anthropic, upstream=url,
        )
        anthropic.script = [
            (429, {"Retry-After": "0"}, RATE_LIMITED),
            (200, {}, json.dumps(_success(HAIKU)).encode()),
        ]
        self.vendor_up.messages_body = _success("glm-5.3-flash")
        resp = await self.client.post(
            "/v1/messages", json=self.ask(HAIKU), headers=self.chat(),
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(anthropic.message_requests), 2)
        self.assertEqual(len(self.vendor_up.message_requests), 0)

    async def test_a_second_refusal_after_the_retry_falls_back(self) -> None:
        """Retry once, THEN fall back — the second short-window refusal stops
        the same-rung path and escalates."""
        anthropic = _SeqUpstream()
        url = await anthropic.start()
        self.addAsyncCleanup(anthropic.stop)
        from model_router import server as srv

        gateway_app = self.client.app
        gateway_app[srv.APP_KEY].anthropic = dataclasses.replace(
            gateway_app[srv.APP_KEY].anthropic, upstream=url,
        )
        anthropic.script = [
            (429, {"Retry-After": "1"}, RATE_LIMITED),
            (429, {"Retry-After": "1"}, RATE_LIMITED),
        ]
        self.vendor_up.messages_body = _success("glm-5.3-flash")
        resp = await self.client.post(
            "/v1/messages", json=self.ask(HAIKU), headers=self.chat(),
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(anthropic.message_requests), 2)
        self.assertEqual(len(self.vendor_up.message_requests), 1)
        rows = await self.rows()
        self.assertEqual(rows[0]["forward"], "glm-5.3-flash")
        self.assertTrue(rows[0]["fallback_from"])

    async def test_all_rungs_exhausted_answers_the_original_status(self) -> None:
        """Never a fabricated success: every rung refuses, and the client sees
        429 — the quota envelope of the LAST rung, which names the vendor that
        refused and why."""
        self.refuse(self.anthropic_up)
        self.refuse(self.vendor_up)
        self.refuse(self.qwen_up)
        resp = await self.client.post(
            "/v1/messages", json=self.ask(HAIKU), headers=self.chat(),
        )
        self.assertEqual(resp.status, 429)
        data = await resp.json()
        self.assertEqual(data["error"]["type"], "rate_limit_error")
        self.assertIn("QwenCloud", data["error"]["message"])
        # Every rung was tried exactly once (the last rung keeps its shipped
        # exhausted re-probe, so qwen answers twice).
        self.assertEqual(len(self.anthropic_up.message_requests), 1)
        self.assertEqual(len(self.vendor_up.message_requests), 1)
        self.assertEqual(len(self.qwen_up.message_requests), 2)

    async def test_a_rung_without_a_key_relays_the_original_refusal(self) -> None:
        """A rung that cannot be served (no vendor key resolved) is skipped
        and the FIRST-PARTY refusal itself reaches the client — never a
        fabricated success, and never a 503 invented by the ladder."""
        client = await self.make_client(
            vendors={"zai": self.vendor, "qwen": self.qwen},
            key_getter=lambda key, project=None: (
                "" if key in ("glm_api_key", "zai_api_key") else FAKE_VENDOR_KEY
            ),
        )
        self.refuse(self.anthropic_up)
        resp = await client.post(
            "/v1/messages", json=self.ask(HAIKU), headers=self.chat(),
        )
        self.assertEqual(resp.status, 429)
        self.assertEqual(await resp.read(), EXHAUSTED)
        self.assertEqual(len(self.vendor_up.message_requests), 0)

    async def test_zai_quota_verdict_on_a_fallback_rung_escalates_to_qwen(self) -> None:
        """The second edge, walked ONLY because this attempt ARRIVED at
        glm-5.3-flash by fallback (B-1, coordinator 2026-10-07): the chain
        haiku → glm-flash (refused) → qwen-flash answers visibly."""
        self.refuse(self.anthropic_up)
        self.refuse(self.vendor_up)
        self.qwen_up.messages_body = _success("qwen3.8-flash")
        resp = await self.client.post(
            "/v1/messages", json=self.ask(HAIKU), headers=self.chat(),
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["model"], "qwen3.8-flash")
        sent = json.loads(self.qwen_up.message_requests[0]["body"])
        self.assertEqual(sent["model"], "qwen3.8-flash")
        self.assertEqual(len(self.anthropic_up.message_requests), 1)
        self.assertEqual(len(self.vendor_up.message_requests), 1)
        rows = await self.rows()
        self.assertEqual(rows[0]["forward"], "qwen3.8-flash")
        self.assertIn("zai:glm-5.3-flash", rows[0]["fallback_from"])

    async def test_a_reprobed_last_rung_success_keeps_fallback_from(self) -> None:
        """B-1R (batch-7): the last rung's shipped EXHAUSTED re-probe can be
        the attempt that finally answers. Its ledger row must keep the
        ladder's ``fallback_from`` — a re-probe that succeeds is still the
        remapped request, and losing the field there is the one place a
        fallback could land invisible (R15)."""
        qwen = _SeqUpstream()
        qwen_url = await qwen.start()
        self.addAsyncCleanup(qwen.stop)
        client = await self.make_client(
            vendors={
                "zai": self.vendor,
                "qwen": dataclasses.replace(VENDORS["qwen"], upstream=qwen_url),
            },
        )
        self.refuse(self.anthropic_up)
        self.refuse(self.vendor_up)
        qwen.script = [
            (429, {}, EXHAUSTED),
            (200, {}, json.dumps(_success("qwen3.8-flash")).encode()),
        ]
        resp = await client.post(
            "/v1/messages", json=self.ask(HAIKU), headers=self.chat(),
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["model"], "qwen3.8-flash")
        # The ladder walked once; the re-probe is the second qwen attempt.
        self.assertEqual(len(self.anthropic_up.message_requests), 1)
        self.assertEqual(len(self.vendor_up.message_requests), 1)
        self.assertEqual(len(qwen.message_requests), 2)
        rows = await self.rows(client)
        self.assertEqual(rows[0]["forward"], "qwen3.8-flash")
        self.assertEqual(rows[0]["fallback_from"], "vendor:zai:glm-5.3-flash")

    async def test_a_user_picked_zai_flash_429_is_relayed_not_rerouted(self) -> None:
        """B-1 (coordinator decision, 2026-10-07): the vendor→vendor edge is
        the LADDER's, not a general proxy. A user who PICKED
        claude-gw/glm-5.3-flash made a deliberate capability/cost choice;
        its quota refusal is relayed (the client's own retry owns recovery)
        and qwen is never called."""
        self.refuse(self.vendor_up)
        resp = await self.client.post(
            "/v1/messages", json=self.ask(ZAI_FLASH), headers=self.chat(),
        )
        self.assertEqual(resp.status, 429)
        data = await resp.json()
        self.assertEqual(data["error"]["type"], "rate_limit_error")
        self.assertIn("Z.ai", data["error"]["message"])
        self.assertEqual(len(self.qwen_up.message_requests), 0)

    async def test_a_non_haiku_first_party_429_is_relayed_verbatim(self) -> None:
        """Leave-alone: the ladder is the haiku slot's, not a general proxy —
        a first-party 429 for any other model is relayed exactly as before."""
        self.refuse(self.anthropic_up)
        resp = await self.client.post(
            "/v1/messages", json=self.ask("claude-opus-5-5"), headers=self.chat(),
        )
        self.assertEqual(resp.status, 429)
        self.assertEqual(await resp.read(), EXHAUSTED)
        self.assertEqual(len(self.vendor_up.message_requests), 0)

    async def test_a_non_rung_vendor_model_does_not_escalate(self) -> None:
        """Leave-alone: glm-5.3 (not a rung) keeps the shipped quota envelope
        and never touches another vendor."""
        self.refuse(self.vendor_up)
        resp = await self.client.post(
            "/v1/messages", json=self.ask(ZAI_GLM), headers=self.chat(),
        )
        self.assertEqual(resp.status, 429)
        data = await resp.json()
        self.assertIn("Z.ai", data["error"]["message"])
        self.assertEqual(len(self.qwen_up.message_requests), 0)

    async def test_the_last_rung_keeps_its_own_quota_answer(self) -> None:
        self.refuse(self.qwen_up)
        resp = await self.client.post(
            "/v1/messages", json=self.ask(QWEN_FLASH), headers=self.chat(),
        )
        self.assertEqual(resp.status, 429)
        data = await resp.json()
        self.assertIn("QwenCloud", data["error"]["message"])
        self.assertEqual(len(self.vendor_up.message_requests), 0)
        self.assertEqual(len(self.anthropic_up.message_requests), 0)


# ══════════════════════════════════════════════════════════════════════════
# A8 — thinking.clear_thinking (OD-10)
# ══════════════════════════════════════════════════════════════════════════
class LedgerQuiesceTests(LadderTestBase):
    """v0.2.102 W6-B2 last round — the reprobe-test flake, root-caused.

    A request's ledger row is submitted at its terminal point, which runs AFTER
    the client has the whole body (the upstream response still closes, an
    await, before ``access()``). A reader that only drained the queued appends
    could therefore run before the submit and find no row — under full-suite
    load ``rows[0]`` raised IndexError. :meth:`Gateway.quiesce` waits for the
    in-flight handlers first; ``inflight_middleware`` counts them."""

    async def test_quiesce_waits_for_a_handler_still_past_its_response(self) -> None:
        import asyncio

        from model_router import server as srv

        gateway = self.client.app[srv.APP_KEY]
        done: list = []

        async def _terminal_point_later() -> None:
            await asyncio.sleep(0.05)
            done.append("submitted")
            gateway.request_finished()

        gateway.request_started()
        task = asyncio.create_task(_terminal_point_later())
        await gateway.usage.drain()
        self.assertEqual(done, [], "drain alone returns before the late submit (the race)")
        await gateway.quiesce()
        self.assertEqual(done, ["submitted"])
        await task

    async def test_the_usage_submit_happens_while_the_request_is_counted(self) -> None:
        """Wiring: the middleware covers the terminal point, so quiesce sees it."""
        from model_router import server as srv

        gateway = self.client.app[srv.APP_KEY]
        seen: list = []
        real = srv._submit_usage

        def _spy(*a, **kw):
            seen.append(gateway._inflight)
            return real(*a, **kw)

        srv._submit_usage = _spy
        self.addCleanup(setattr, srv, "_submit_usage", real)
        self.anthropic_up.messages_body = _success(HAIKU)
        resp = await self.client.post("/v1/messages", json=self.ask(HAIKU), headers=self.chat())
        self.assertEqual(resp.status, 200)
        await resp.read()
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(seen and seen[0] >= 1, seen)
        self.assertEqual(gateway._inflight, 0)


class _SeedContext:
    """The one thing :func:`_apply_clear_thinking` needs from a gateway."""

    def __init__(self) -> None:
        self.context = type(
            "Ctx", (), {"current": staticmethod(lambda: load_seed())},
        )()


class ClearThinkingSeedTests(unittest.TestCase):

    def test_glm_5_3_flash_carries_the_cited_field(self) -> None:
        row = load_seed().lookup("glm-5.3-flash")
        self.assertIsNotNone(row)
        assert row is not None  # noqa: S101
        self.assertIs(row.clear_thinking, False)

    def test_rows_without_the_field_answer_not_sent(self) -> None:
        table = load_seed()
        self.assertIsNone(table.clear_thinking("glm-5.3"))
        self.assertIsNone(table.clear_thinking("claude-opus-5-5"))

    def test_a_non_boolean_value_is_dropped_not_guessed(self) -> None:
        # A stringly "false" in a seed row is dropped to "not sent" — the
        # value reaches the request bytes, so nothing is coerced.
        from pathlib import Path

        from model_router.context_table import _parse

        payload = {
            "schema_version": 1,
            "models": {
                "m": {
                    "vendor": "v",
                    "context_window": 1,
                    "max_output": 1,
                    "window_1m": False,
                    "source": "https://example.invalid/model",
                    "clear_thinking": "false",
                },
            },
        }
        rows, _uncited, _tombstones = _parse(payload, Path("crafted"))
        self.assertIsNone(rows["m"].clear_thinking)


class ClearThinkingShapingTests(unittest.TestCase):

    def setUp(self) -> None:
        self.gateway = _SeedContext()

    def test_a_glm_5_3_flash_request_carries_clear_thinking_false(self) -> None:
        payload = {"thinking": {"budget_tokens": 2048}, "model": "glm-5.3-flash"}
        shaped, changed = _apply_clear_thinking(payload, self.gateway, "glm-5.3-flash")
        self.assertTrue(changed)
        self.assertIs(shaped["thinking"]["clear_thinking"], False)

    def test_a_row_without_the_field_sends_nothing_extra(self) -> None:
        payload = {"thinking": {"budget_tokens": 2048}, "model": "glm-5.3"}
        shaped, changed = _apply_clear_thinking(payload, self.gateway, "glm-5.3")
        self.assertFalse(changed)
        self.assertIs(shaped, payload)
        self.assertNotIn("clear_thinking", shaped["thinking"])

    def test_a_request_without_a_thinking_param_is_untouched(self) -> None:
        payload = {"model": "glm-5.3-flash", "messages": []}
        shaped, changed = _apply_clear_thinking(
            payload, self.gateway, "glm-5.3-flash",
        )
        self.assertFalse(changed)
        self.assertIs(shaped, payload)
        self.assertNotIn("thinking", shaped)

    def test_an_existing_value_is_not_duplicated(self) -> None:
        payload = {
            "thinking": {"budget_tokens": 2048, "clear_thinking": False},
            "model": "glm-5.3-flash",
        }
        shaped, changed = _apply_clear_thinking(payload, self.gateway, "glm-5.3-flash")
        self.assertFalse(changed)


class ClearThinkingRouteTests(LadderTestBase):

    async def test_the_field_reaches_the_vendor_on_a_real_request(self) -> None:
        self.vendor_up.messages_body = _success("glm-5.3-flash")
        with self.assertLogs(LOGGER, level="INFO") as cm:
            resp = await self.client.post(
                "/v1/messages",
                json=self.ask(ZAI_FLASH, thinking={"budget_tokens": 2048}),
                headers=self.chat(),
            )
        self.assertEqual(resp.status, 200)
        sent = json.loads(self.vendor_up.message_requests[0]["body"])
        self.assertIs(sent["thinking"]["clear_thinking"], False)
        self.assertIn("note=clear_thinking_applied", "\n".join(cm.output))

    async def test_a_model_without_the_row_sends_no_field(self) -> None:
        self.vendor_up.messages_body = _success("glm-5.3")
        resp = await self.client.post(
            "/v1/messages",
            json=self.ask(ZAI_GLM, thinking={"budget_tokens": 2048}),
            headers=self.chat(),
        )
        self.assertEqual(resp.status, 200)
        sent = json.loads(self.vendor_up.message_requests[0]["body"])
        self.assertNotIn("clear_thinking", sent["thinking"])


# ══════════════════════════════════════════════════════════════════════════
# R15 — the ledger's fallback_from field
# ══════════════════════════════════════════════════════════════════════════
class UsageFallbackFieldTests(unittest.TestCase):

    @staticmethod
    def _record(**overrides: Any):
        kwargs: dict[str, Any] = {
            "session": "s",
            "agent": None,
            "parent_agent": None,
            "requested": HAIKU,
            "route": "vendor:zai",
            "forward": "glm-5.3-flash",
            "stream": False,
            "status": 200,
            "totals": {"input_tokens": 10, "output_tokens": 2},
            "usage_complete": True,
            "window_actual": 1_000_000,
            "window_source": "row",
        }
        kwargs.update(overrides)
        return build_record(**kwargs)

    def test_a_fallback_row_carries_the_rung_it_escaped(self) -> None:
        record = self._record(fallback_from=f"anthropic:{HAIKU}")
        self.assertEqual(record.fallback_from, f"anthropic:{HAIKU}")
        self.assertIn('"fallback_from"', record.to_json())

    def test_an_ordinary_row_carries_the_empty_default(self) -> None:
        record = self._record()
        self.assertEqual(record.fallback_from, "")


# ══════════════════════════════════════════════════════════════════════════
# NB-07 — suffixed dev builds in the gateway self-test
# ══════════════════════════════════════════════════════════════════════════
class SuffixedBuildVersionTests(unittest.TestCase):

    def _tuple(self, text: str) -> Optional[tuple[int, int, int]]:
        from vco_lib.vscode_settings import _version_tuple

        return _version_tuple(text)

    def test_a_local_suffix_compares_as_its_release_part(self) -> None:
        self.assertEqual(self._tuple("0.2.102+abc"), (0, 2, 102))
        self.assertEqual(self._tuple("v0.2.102+local"), (0, 2, 102))

    def test_a_suffixed_running_build_is_not_outdated(self) -> None:
        # The exact NB-07 red case: 0.2.102+abc vs 0.2.102 must not read as
        # older (nor as unknown) — the comparison in _dogfood_version ranks
        # the two keys produced here.
        running, mine = self._tuple("0.2.102+abc"), self._tuple("0.2.102")
        self.assertIsNotNone(running)
        self.assertIsNotNone(mine)
        self.assertFalse(running < mine)  # type: ignore[operator]

    def test_a_suffixed_newer_build_is_newer(self) -> None:
        running, mine = self._tuple("0.2.103+dev.1"), self._tuple("0.2.102")
        self.assertGreater(running, mine)  # type: ignore[type-var]

    def test_a_suffixed_older_build_is_still_outdated(self) -> None:
        running, mine = self._tuple("0.2.101+abc"), self._tuple("0.2.102")
        self.assertLess(running, mine)  # type: ignore[type-var]

    def test_every_other_malformed_shape_is_still_unranked(self) -> None:
        for bad in ("0.2.102rc1", "0.2", "1.2.3.4", "+abc", "0.2.102+", ""):
            self.assertIsNone(self._tuple(bad), bad)
