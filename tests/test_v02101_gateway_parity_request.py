# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 gateway vendor-parity — REQUEST shaping (items 13a, 13c, 13e, 13f).

Four request-side gaps from the 2026-10-03 parity audit
(``.claude/context/reviews/GATEWAY-VENDOR-PARITY-2026-10-03.md``), each closed
here and each pinned by a test. Live captures are replayed verbatim from
``tests/fixtures/model_router/`` (auth-stripped at capture); vendor shapes are
never invented.

* **(a) Effort translation** — ``claude-gw/qwen/glm-5.3`` at effort ``medium``
  (the owner's default subagent effort) is a live HTTP 400
  (``medium-qwen-glm.body``: "'reasoning_effort' must be one of: 'low','high',
  'max'"). One per-(vendor, model) table (``model_router.effort``) rewrites
  ONLY a value the vendor rejects; accepted values stay byte-identical and a
  vendor-side collapse is recorded as data, never rewritten.
* **(c) Cross-route thinking blocks** — a vendor ``thinking`` block replayed to
  the first-party route is HTTP 400 "Invalid `signature` in `thinking` block"
  (``anthropic-vendor-thinking.body``). Vendor signatures are marked on the way
  out (``vct_`` prefix) and stripped from history bound for Anthropic; the
  vendor route gets its own signature back.
* **(e) Anthropic server tools on vendor routes** — z.ai 500s
  (``zai-websearch.body``), qwen silently ignores them (``qwen-websearch.body``).
  A server-tools-only request is refused with guidance; a mixed request has the
  server tools stripped and still succeeds.
* **(f) Text-only models** — glm-5.3 cannot see an image and says so
  (``zai-image.body``: "I cannot see images from URLs"). A per-model
  ``text_only`` catalog flag replaces each image block with a short text note.

The owner rule behind every case: the gateway must NEVER cause a chat failure,
so each transform runs through ``server._guarded`` — on any exception the
client's own bytes go on unchanged and the access line carries
``note=rewrite_failed``. No network: the upstreams are the loopback stubs from
:mod:`tests.test_model_router_server`.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import vco_lib.transcript_repair as tr
from model_router import effort as E
from model_router import tool_ids as ti
from model_router.context_table import load_seed
from model_router.server import (
    TEXT_ONLY_IMAGE_NOTE,
    _partition_server_tools,
    _replace_image_blocks,
    _server_tools_refusal,
    _strip_images_for_text_only,
    _strip_server_tools,
)
from model_router.vendors import VENDORS

from tests.test_model_router_server import GatewayTestBase

FIXTURES = Path(__file__).parent / "fixtures" / "model_router"
LOGGER = "model_router.server"

#: A genuine Anthropic thinking signature is a LONG mixed-case base64 blob —
#: never pure lowercase hex, never empty. Shaped like the real thing so the
#: "keep it byte-identical" assertions are honest.
ANTHROPIC_SIGNATURE = (
    "EqQBCgIYAhIkAc1xYzLongBase64BlobThatIsObviouslyNotVendorHex9kQ2xh/Zg=="
)
#: z.ai's shape: 24 lowercase-hex chars (verbatim from ``zai-image.body``).
ZAI_SIGNATURE = "d43125f46f424ef1b880535c"


def _fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.body").read_bytes()


def _access_fields(line: str) -> dict[str, str]:
    """Parse ``k=v`` pairs out of one access line (ids arrive repr-quoted)."""
    body = line.split("model-gateway: ", 1)[-1]
    out: dict[str, str] = {}
    for part in body.split(" "):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        out[key] = value
    return out


def _sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")


# ══════════════════════════════════════════════════════════════════════════
# item (a) — effort translation
# ══════════════════════════════════════════════════════════════════════════
class EffortTableIntegrityTests(unittest.TestCase):
    """Every row of the table is internally consistent (data, one home)."""

    def test_every_row_is_self_consistent(self) -> None:
        for key, policy in E.EFFORT_POLICIES.items():
            with self.subTest(route=key):
                # A value cannot be both accepted (byte-identical) and
                # translated (rewritten) — that is a contradiction.
                self.assertFalse(
                    set(policy.translate) & policy.accepted,
                    f"{key}: translate keys overlap accepted",
                )
                # The replacement must be a value the vendor accepts, or the
                # rewrite would land on another rejection.
                self.assertTrue(
                    set(policy.translate.values()) <= policy.accepted,
                    f"{key}: a translate target is not accepted",
                )
                # A vendor-side collapse is something the endpoint ACCEPTS and
                # maps itself, so its key must be an accepted value (it is
                # never rewritten by us).
                self.assertTrue(
                    set(policy.collapses) <= policy.accepted,
                    f"{key}: a collapsed value is not in accepted",
                )
                self.assertTrue(policy.doc_url.startswith("http"), key)

    def test_the_one_measured_rejection_is_qwen_glm_5_3(self) -> None:
        """gap 1: the only route with a live 400 is the only one that rewrites."""
        rewriting = {
            key for key, p in E.EFFORT_POLICIES.items() if p.translate
        }
        self.assertEqual(rewriting, {("qwen", "glm-5.3")})
        self.assertEqual(
            E.EFFORT_POLICIES[("qwen", "glm-5.3")].translate,
            {"medium": "high", "xhigh": "max"},
        )

    def test_qwen_glm_accepted_set_matches_the_doc(self) -> None:
        # QwenCloud: glm-5.3 valid = low/high/max, "any other value errors".
        self.assertEqual(
            E.EFFORT_POLICIES[("qwen", "glm-5.3")].accepted,
            frozenset({"low", "high", "max"}),
        )

    def test_collapses_are_recorded_not_rewritten(self) -> None:
        # qwen deepseek: low/medium -> high is the VENDOR's mapping (doc), so
        # it is data here and translate stays empty.
        deepseek = E.EFFORT_POLICIES[("qwen", "deepseek-v4.1-flash")]
        self.assertEqual(deepseek.translate, {})
        self.assertEqual(
            deepseek.collapses, {"low": "high", "medium": "high", "xhigh": "max"},
        )
        qwen_max = E.EFFORT_POLICIES[("qwen", "qwen3.8-max")]
        self.assertEqual(qwen_max.translate, {})
        self.assertEqual(qwen_max.collapses, {"high": "xhigh", "max": "xhigh"})


class TranslateEffortUnitTests(unittest.TestCase):
    """``translate_effort`` — the one rewrite, per row and per field form."""

    def test_medium_on_qwen_glm_is_rewritten_to_high(self) -> None:
        out, changed = E.translate_effort(
            {"model": "glm-5.3", "output_config": {"effort": "medium"}},
            "qwen", "glm-5.3",
        )
        self.assertTrue(changed)
        self.assertEqual(out["output_config"]["effort"], "high")

    def test_xhigh_on_qwen_glm_is_rewritten_to_max(self) -> None:
        out, changed = E.translate_effort(
            {"output_config": {"effort": "xhigh"}}, "qwen", "glm-5.3",
        )
        self.assertTrue(changed)
        self.assertEqual(out["output_config"]["effort"], "max")

    def test_accepted_values_are_byte_identical(self) -> None:
        for value in ("low", "high", "max"):
            payload = {"output_config": {"effort": value}}
            out, changed = E.translate_effort(payload, "qwen", "glm-5.3")
            self.assertFalse(changed, value)
            self.assertIs(out, payload, f"{value}: accepted must not be copied")

    def test_medium_on_zai_is_accepted_and_untouched(self) -> None:
        # medium-zai-glm.body: medium -> HTTP 200 on z.ai. No rewrite.
        payload = {"output_config": {"effort": "medium"}}
        out, changed = E.translate_effort(payload, "zai", "glm-5.3")
        self.assertFalse(changed)
        self.assertIs(out, payload)

    def test_collapsed_values_are_not_rewritten(self) -> None:
        # qwen deepseek/qwen3.8 accept these and collapse them server-side.
        for vendor_model in (("qwen", "deepseek-v4.1-flash"), ("qwen", "qwen3.8-max")):
            for value in E.EFFORT_VALUES:
                payload = {"output_config": {"effort": value}}
                out, changed = E.translate_effort(payload, *vendor_model)
                self.assertFalse(changed, f"{vendor_model} {value}")
                self.assertIs(out, payload)

    def test_a_route_with_no_policy_is_untouched(self) -> None:
        payload = {"output_config": {"effort": "medium"}}
        out, changed = E.translate_effort(payload, "zai", "some-unknown-model")
        self.assertFalse(changed)
        self.assertIs(out, payload)

    def test_both_field_forms_current_build(self) -> None:
        """output_config.effort is rewritten; the adaptive thinking block is not."""
        payload = {
            "output_config": {"effort": "medium"},
            "thinking": {"type": "enabled", "budget_tokens": 4000},
        }
        out, changed = E.translate_effort(payload, "qwen", "glm-5.3")
        self.assertTrue(changed)
        self.assertEqual(out["output_config"]["effort"], "high")
        self.assertEqual(out["thinking"], {"type": "enabled", "budget_tokens": 4000})

    def test_both_field_forms_older_build_budget_only(self) -> None:
        """An older build sends only thinking.budget_tokens: nothing to translate."""
        payload = {"thinking": {"type": "enabled", "budget_tokens": 8000}}
        out, changed = E.translate_effort(payload, "qwen", "glm-5.3")
        self.assertFalse(changed)
        self.assertIs(out, payload)

    def test_a_non_dict_or_missing_output_config_is_untouched(self) -> None:
        self.assertEqual(E.translate_effort(None, "qwen", "glm-5.3"), (None, False))
        payload = {"model": "glm-5.3"}
        self.assertEqual(
            E.translate_effort(payload, "qwen", "glm-5.3"), (payload, False),
        )

    def test_an_unknown_effort_value_is_left_alone(self) -> None:
        # No evidence either way -> never invent a translation.
        payload = {"output_config": {"effort": "ultra"}}
        out, changed = E.translate_effort(payload, "qwen", "glm-5.3")
        self.assertFalse(changed)
        self.assertIs(out, payload)

    def test_every_row_accepted_noop_and_rejected_rewritten(self) -> None:
        """Walk EVERY row: accepted values are byte-identical, translate keys
        are rewritten to their target. This is the 'every row of the table'
        coverage — a new row is exercised without a new test."""
        for (vendor_id, model_id), policy in E.EFFORT_POLICIES.items():
            for value in sorted(policy.accepted):
                payload = {"output_config": {"effort": value}}
                out, changed = E.translate_effort(payload, vendor_id, model_id)
                self.assertFalse(
                    changed, f"{vendor_id}/{model_id} accepted {value} rewritten",
                )
                self.assertIs(out, payload, f"{vendor_id}/{model_id} {value} copied")
            for rejected, target in policy.translate.items():
                payload = {"output_config": {"effort": rejected}}
                out, changed = E.translate_effort(payload, vendor_id, model_id)
                self.assertTrue(changed, f"{vendor_id}/{model_id} {rejected}")
                self.assertEqual(
                    out["output_config"]["effort"], target,
                    f"{vendor_id}/{model_id} {rejected}->{target}",
                )


class QwenVendorTestBase(GatewayTestBase):
    """The harness stub upstream, but wired as the qwen row."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        stub_url = self.vendor.upstream
        self.vendor = replace(VENDORS["qwen"], upstream=stub_url)
        self.client = await self.make_client()

    async def _forwarded(self) -> dict:
        return json.loads(self.vendor_up.requests[0]["body"])


class EffortRouteIntegrationTests(QwenVendorTestBase):
    """The rewrite on the wire, through the full HTTP relay."""

    async def test_medium_on_qwen_glm_reaches_the_vendor_as_high(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-gw/qwen/glm-5.3",
                "messages": [{"role": "user", "content": "hi"}],
                "output_config": {"effort": "medium"},
            },
        )
        forwarded = await self._forwarded()
        self.assertEqual(forwarded["model"], "glm-5.3")
        self.assertEqual(forwarded["output_config"]["effort"], "high")

    async def test_accepted_effort_reaches_the_vendor_unchanged(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-gw/qwen/glm-5.3",
                "messages": [{"role": "user", "content": "hi"}],
                "output_config": {"effort": "low"},
            },
        )
        self.assertEqual((await self._forwarded())["output_config"]["effort"], "low")

    async def test_effort_translation_is_noted_on_the_access_line(self) -> None:
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await self.client.post(
                "/v1/messages",
                headers=self.auth(),
                json={
                    "model": "claude-gw/qwen/glm-5.3",
                    "messages": [{"role": "user", "content": "hi"}],
                    "output_config": {"effort": "medium"},
                },
            )
        line = next(m for m in captured.output if "requested=" in m)
        self.assertIn("note=effort_translated", line)

    async def test_zai_medium_is_forwarded_unchanged(self) -> None:
        # The zai harness row: medium is accepted live, so it is not rewritten.
        self.vendor = replace(VENDORS["zai"], upstream=self.vendor.upstream)
        self.client = await self.make_client()
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-gw/glm-5.3",
                "messages": [{"role": "user", "content": "hi"}],
                "output_config": {"effort": "medium"},
            },
        )
        forwarded = json.loads(self.vendor_up.requests[0]["body"])
        self.assertEqual(forwarded["output_config"]["effort"], "medium")

    async def test_first_party_effort_is_never_touched(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-opus-5",
                "messages": [{"role": "user", "content": "hi"}],
                "output_config": {"effort": "medium"},
            },
        )
        forwarded = json.loads(self.anthropic_up.requests[0]["body"])
        self.assertEqual(forwarded["output_config"]["effort"], "medium")

    async def test_an_effort_exception_relays_the_clients_own_bytes(self) -> None:
        # _guarded: a defect in the rewrite must not fail the chat. Patched at
        # the server's import site, which is the name _guarded actually calls.
        with mock.patch(
            "model_router.server.translate_effort",
            side_effect=RuntimeError("synthetic"),
        ):
            with self.assertLogs(LOGGER, level="INFO") as captured:
                resp = await self.client.post(
                    "/v1/messages",
                    headers=self.auth(),
                    json={
                        "model": "claude-gw/qwen/glm-5.3",
                        "messages": [{"role": "user", "content": "hi"}],
                        "output_config": {"effort": "medium"},
                    },
                )
        self.assertEqual(resp.status, 200)
        # The client's own value goes on unchanged (today's behaviour).
        forwarded = json.loads(self.vendor_up.requests[0]["body"])
        self.assertEqual(forwarded["output_config"]["effort"], "medium")
        self.assertIn("note=rewrite_failed", "\n".join(captured.output))


# ══════════════════════════════════════════════════════════════════════════
# item (c) — cross-route thinking blocks
# ══════════════════════════════════════════════════════════════════════════
class ThinkingSignatureUnitTests(unittest.TestCase):
    """The vendor-origin discriminator and the mark/unmark round-trip."""

    def test_zai_hex_signature_is_vendor_origin(self) -> None:
        self.assertTrue(tr.thinking_signature_is_vendor_origin(ZAI_SIGNATURE))

    def test_qwen_empty_signature_is_vendor_origin(self) -> None:
        self.assertTrue(tr.thinking_signature_is_vendor_origin(""))

    def test_a_marked_signature_is_vendor_origin(self) -> None:
        self.assertTrue(
            tr.thinking_signature_is_vendor_origin("vct_" + ANTHROPIC_SIGNATURE),
        )

    def test_a_genuine_anthropic_signature_is_kept(self) -> None:
        self.assertFalse(
            tr.thinking_signature_is_vendor_origin(ANTHROPIC_SIGNATURE),
        )

    def test_a_missing_or_non_string_signature_is_vendor_origin(self) -> None:
        self.assertTrue(tr.thinking_signature_is_vendor_origin(None))
        self.assertTrue(tr.thinking_signature_is_vendor_origin(123))

    def test_mark_unmark_round_trips(self) -> None:
        for sig in (ZAI_SIGNATURE, "", "f177146cea1e4cfaa6137e3a"):
            marked = tr.mark_thinking_signature(sig)
            self.assertTrue(marked.startswith(tr.THINKING_SIGNATURE_MARKER))
            self.assertEqual(tr.unmark_thinking_signature(marked), sig)

    def test_marking_is_idempotent(self) -> None:
        once = tr.mark_thinking_signature(ZAI_SIGNATURE)
        self.assertEqual(tr.mark_thinking_signature(once), once)

    def test_unmark_payload_walk_restores_the_original(self) -> None:
        payload = {
            "messages": [{
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "t",
                     "signature": "vct_" + ZAI_SIGNATURE},
                    {"type": "text", "text": "hi"},
                ],
            }],
        }
        out, count = tr.unmark_thinking_signatures(payload)
        self.assertEqual(count, 1)
        self.assertEqual(out["messages"][0]["content"][0]["signature"], ZAI_SIGNATURE)

    def test_unmark_is_a_noop_when_nothing_is_marked(self) -> None:
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        out, count = tr.unmark_thinking_signatures(payload)
        self.assertEqual(count, 0)
        self.assertIs(out, payload)


class SanitiseThinkingUnitTests(unittest.TestCase):
    """``sanitise_for_anthropic`` strips vendor thinking, keeps Anthropic's."""

    def _assistant(self, signature: str) -> dict:
        return {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "reasoning", "signature": signature},
                {"type": "text", "text": "answer"},
            ],
        }

    def test_a_zai_vendor_block_is_stripped(self) -> None:
        payload = {"messages": [{"role": "user", "content": "q"}, self._assistant(ZAI_SIGNATURE)]}
        out, stats = ti.sanitise_for_anthropic(payload)
        self.assertEqual(stats.thinking_blocks_stripped, 1)
        content = out["messages"][1]["content"]
        self.assertEqual([b["type"] for b in content], ["text"])

    def test_a_qwen_empty_signature_block_is_stripped(self) -> None:
        payload = {"messages": [self._assistant("")]}
        out, stats = ti.sanitise_for_anthropic(payload)
        self.assertEqual(stats.thinking_blocks_stripped, 1)
        self.assertEqual(
            [b["type"] for b in out["messages"][0]["content"]], ["text"],
        )

    def test_a_marked_block_is_stripped(self) -> None:
        payload = {"messages": [self._assistant("vct_" + ZAI_SIGNATURE)]}
        _out, stats = ti.sanitise_for_anthropic(payload)
        self.assertEqual(stats.thinking_blocks_stripped, 1)

    def test_a_genuine_anthropic_block_is_kept_byte_identical(self) -> None:
        message = self._assistant(ANTHROPIC_SIGNATURE)
        payload = {"messages": [message]}
        out, stats = ti.sanitise_for_anthropic(payload)
        self.assertEqual(stats.thinking_blocks_stripped, 0)
        self.assertFalse(stats.changed)
        self.assertIs(out, payload)  # same object: nothing copied
        self.assertEqual(out["messages"][0]["content"][0]["signature"], ANTHROPIC_SIGNATURE)

    def test_the_captured_anthropic_400_is_what_this_prevents(self) -> None:
        # anthropic-vendor-thinking.body is the live rejection this closes.
        body = json.loads(_fixture("anthropic-vendor-thinking"))
        self.assertIn("Invalid `signature` in `thinking` block",
                      body["error"]["message"])

    def test_stripping_the_last_thinking_of_a_tool_loop_drops_the_thinking_param(
        self,
    ) -> None:
        """The edge case: Anthropic needs a leading thinking block on a
        tool-use turn while thinking is enabled, so the least-invasive VALID
        request is this one sent WITHOUT ``thinking``."""
        payload = {
            "thinking": {"type": "enabled", "budget_tokens": 4000},
            "messages": [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "t", "signature": ZAI_SIGNATURE},
                    {"type": "tool_use", "id": "toolu_1", "name": "x", "input": {}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
                ]},
            ],
        }
        out, stats = ti.sanitise_for_anthropic(payload)
        self.assertEqual(stats.thinking_blocks_stripped, 1)
        self.assertTrue(stats.thinking_param_dropped)
        self.assertNotIn("thinking", out)
        # The tool_use turn survives, just without its vendor thinking block.
        self.assertEqual(
            [b["type"] for b in out["messages"][1]["content"]], ["tool_use"],
        )

    def test_thinking_param_is_kept_when_the_loop_has_no_tool_use(self) -> None:
        payload = {
            "thinking": {"type": "enabled", "budget_tokens": 4000},
            "messages": [
                {"role": "user", "content": "q"},
                self._assistant(ZAI_SIGNATURE),  # text turn, no tool_use
            ],
        }
        out, stats = ti.sanitise_for_anthropic(payload)
        self.assertEqual(stats.thinking_blocks_stripped, 1)
        self.assertFalse(stats.thinking_param_dropped)
        self.assertIn("thinking", out)

    def test_thinking_param_is_kept_when_thinking_is_not_enabled(self) -> None:
        payload = {
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "t", "signature": ZAI_SIGNATURE},
                    {"type": "tool_use", "id": "toolu_1", "name": "x", "input": {}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
                ]},
            ],
        }
        out, stats = ti.sanitise_for_anthropic(payload)
        self.assertFalse(stats.thinking_param_dropped)
        self.assertNotIn("thinking", out)  # never had one

    def test_a_genuine_leading_thinking_keeps_the_thinking_param(self) -> None:
        payload = {
            "thinking": {"type": "enabled", "budget_tokens": 4000},
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "t",
                     "signature": ANTHROPIC_SIGNATURE},
                    {"type": "tool_use", "id": "toolu_1", "name": "x", "input": {}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
                ]},
            ],
        }
        out, stats = ti.sanitise_for_anthropic(payload)
        self.assertEqual(stats.thinking_blocks_stripped, 0)
        self.assertFalse(stats.thinking_param_dropped)
        self.assertIn("thinking", out)


class ThinkingRouteIntegrationTests(GatewayTestBase):
    """The mark/strip/unmark across the two routes, through the relay."""

    async def test_first_party_route_strips_a_vendor_thinking_block(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-opus-5",
                "messages": [
                    {"role": "user", "content": "q"},
                    {"role": "assistant", "content": [
                        {"type": "thinking", "thinking": "t",
                         "signature": ZAI_SIGNATURE},
                        {"type": "text", "text": "a"},
                    ]},
                ],
            },
        )
        forwarded = json.loads(self.anthropic_up.requests[0]["body"])
        types = [b["type"] for b in forwarded["messages"][1]["content"]]
        self.assertEqual(types, ["text"])

    async def test_first_party_route_keeps_a_genuine_thinking_block(self) -> None:
        raw = json.dumps({
            "model": "claude-opus-5",
            "messages": [{"role": "assistant", "content": [
                {"type": "thinking", "thinking": "t",
                 "signature": ANTHROPIC_SIGNATURE},
                {"type": "text", "text": "a"},
            ]}],
        }).encode("utf-8")
        await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "Content-Type": "application/json"},
            data=raw,
        )
        # Nothing to repair -> the client's exact bytes go upstream.
        self.assertEqual(self.anthropic_up.requests[0]["body"], raw)

    async def test_vendor_route_gets_its_own_signature_back(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-gw/glm-5.3",
                "messages": [{"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "t",
                     "signature": "vct_" + ZAI_SIGNATURE},
                    {"type": "text", "text": "a"},
                ]}],
            },
        )
        forwarded = json.loads(self.vendor_up.requests[0]["body"])
        self.assertEqual(
            forwarded["messages"][0]["content"][0]["signature"], ZAI_SIGNATURE,
        )

    async def test_a_streamed_vendor_signature_is_marked_on_the_way_out(self) -> None:
        stream = (
            _sse("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "thinking", "thinking": "",
                                  "signature": ""}})
            + _sse("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "signature_delta", "signature": ZAI_SIGNATURE}})
            + _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        )
        self.vendor_up.stream_chunks = [stream]
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "stream": True},
        )
        body = await resp.read()
        self.assertIn(f'"signature":"vct_{ZAI_SIGNATURE}"'.encode(), body)

    async def test_a_chunked_signature_gets_exactly_one_marker(self) -> None:
        stream = (
            _sse("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "signature_delta", "signature": "abc"}})
            + _sse("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "signature_delta", "signature": "def"}})
        )
        self.vendor_up.stream_chunks = [stream]
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "stream": True},
        )
        body = (await resp.read()).decode()
        # The client concatenates the two deltas, so exactly ONE marker may be
        # added (on the first): "vct_abc" + "def" -> "vct_abcdef", which
        # un-marks to the original "abcdef". A marker on both would corrupt it.
        self.assertIn('"signature":"vct_abc"', body)
        self.assertEqual(body.count("vct_"), 1)
        self.assertNotIn("vct_def", body)

    async def test_a_buffered_vendor_thinking_block_is_marked(self) -> None:
        self.vendor_up.messages_raw = json.dumps({
            "id": "m", "type": "message", "role": "assistant", "model": "glm-5.3",
            "content": [{"type": "thinking", "thinking": "t",
                         "signature": ZAI_SIGNATURE}],
        }).encode("utf-8")
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        body = json.loads(await resp.read())
        self.assertEqual(
            body["content"][0]["signature"], "vct_" + ZAI_SIGNATURE,
        )

    async def test_the_captured_zai_thinking_stream_is_marked(self) -> None:
        # The real capture: signature f177146cea1e4cfaa6137e3a via signature_delta.
        self.vendor_up.stream_chunks = [_fixture("zai-thinking")]
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "stream": True},
        )
        body = await resp.read()
        self.assertIn(b'"signature":"vct_f177146cea1e4cfaa6137e3a"', body)

    async def test_a_first_party_stream_is_never_marked(self) -> None:
        stream = _sse("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "signature_delta", "signature": ANTHROPIC_SIGNATURE}})
        self.anthropic_up.stream_chunks = [stream]
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-opus-5", "messages": [], "stream": True},
        )
        body = await resp.read()
        self.assertEqual(body, stream)  # byte-identical, no marker
        self.assertNotIn(b"vct_", body)


# ══════════════════════════════════════════════════════════════════════════
# item (e) — Anthropic server tools on vendor routes
# ══════════════════════════════════════════════════════════════════════════
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}
ORDINARY_TOOL = {
    "name": "get_weather",
    "description": "weather",
    "input_schema": {"type": "object", "properties": {}},
}


class ServerToolTypeUnitTests(unittest.TestCase):
    def test_server_tool_types_are_recognised(self) -> None:
        for tool_type in (
            "web_search_20250305", "web_fetch_20250910", "code_execution_20250522",
            "bash_code_execution_20250124", "text_editor_code_execution_20250124",
            "tool_search_tool_regex_20251119",
        ):
            self.assertTrue(tr.is_server_tool_type(tool_type), tool_type)

    def test_ordinary_and_absent_types_are_not_server_tools(self) -> None:
        self.assertFalse(tr.is_server_tool_type("custom"))
        self.assertFalse(tr.is_server_tool_type(None))
        self.assertFalse(tr.is_server_tool_type("get_weather"))
        # bash_code_execution must not be caught by the code_execution prefix.
        self.assertTrue(tr.is_server_tool_type("bash_code_execution_20250124"))


class ServerToolPartitionUnitTests(unittest.TestCase):
    def test_only_server_tools(self) -> None:
        server, ordinary = _partition_server_tools({"tools": [WEB_SEARCH_TOOL]})
        self.assertEqual(len(server), 1)
        self.assertEqual(ordinary, [])

    def test_mixed_tools(self) -> None:
        server, ordinary = _partition_server_tools(
            {"tools": [WEB_SEARCH_TOOL, ORDINARY_TOOL]},
        )
        self.assertEqual(len(server), 1)
        self.assertEqual(ordinary, [ORDINARY_TOOL])

    def test_no_tools_is_none(self) -> None:
        self.assertIsNone(_partition_server_tools({"messages": []}))
        self.assertIsNone(_partition_server_tools({"tools": []}))

    def test_strip_removes_only_server_tools(self) -> None:
        payload = {"tools": [WEB_SEARCH_TOOL, ORDINARY_TOOL]}
        out, count = _strip_server_tools(payload)
        self.assertEqual(count, 1)
        self.assertEqual(out["tools"], [ORDINARY_TOOL])

    def test_strip_is_a_noop_without_server_tools(self) -> None:
        payload = {"tools": [ORDINARY_TOOL]}
        out, count = _strip_server_tools(payload)
        self.assertEqual(count, 0)
        self.assertIs(out, payload)


class ServerToolRouteIntegrationTests(GatewayTestBase):
    async def test_only_server_tools_is_refused_naming_the_vendor(self) -> None:
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-gw/glm-5.3",
                "messages": [{"role": "user", "content": "search Rome"}],
                "tools": [WEB_SEARCH_TOOL],
            },
        )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertIn("Z.ai", body["error"]["message"])
        self.assertIn("WebSearch", body["error"]["message"])
        # Refused before proxying: the vendor never sees it.
        self.assertEqual(self.vendor_up.message_requests, [])

    async def test_the_refusal_names_webfetch_and_curl_as_still_working(self) -> None:
        text = _server_tools_refusal("Z.ai")
        self.assertIn("WebFetch", text)
        self.assertIn("curl", text)

    async def test_mixed_tools_strips_the_server_tool_and_forwards(self) -> None:
        with self.assertLogs(LOGGER, level="INFO") as captured:
            resp = await self.client.post(
                "/v1/messages",
                headers=self.auth(),
                json={
                    "model": "claude-gw/glm-5.3",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": [WEB_SEARCH_TOOL, ORDINARY_TOOL],
                },
            )
        self.assertEqual(resp.status, 200)
        forwarded = json.loads(self.vendor_up.requests[0]["body"])
        self.assertEqual(forwarded["tools"], [ORDINARY_TOOL])
        line = next(m for m in captured.output if "requested=" in m)
        self.assertIn("note=server_tools_stripped=1", line)

    async def test_a_mixed_request_is_never_failed(self) -> None:
        # The ordinary tool still works, so the request must succeed.
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-gw/glm-5.3",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [WEB_SEARCH_TOOL, ORDINARY_TOOL],
            },
        )
        self.assertEqual(resp.status, 200)

    async def test_first_party_server_tools_are_forwarded_untouched(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-opus-5",
                "messages": [{"role": "user", "content": "search"}],
                "tools": [WEB_SEARCH_TOOL],
            },
        )
        forwarded = json.loads(self.anthropic_up.requests[0]["body"])
        self.assertEqual(forwarded["tools"], [WEB_SEARCH_TOOL])

    async def test_the_captured_vendor_websearch_answers_are_the_evidence(self) -> None:
        # z.ai 500s; qwen answers 200 but with no search (model says so).
        zai = json.loads(_fixture("zai-websearch"))
        self.assertEqual(zai["type"], "error")
        qwen = json.loads(_fixture("qwen-websearch"))
        thinking = qwen["content"][0]["thinking"]
        self.assertIn("don't have internet", thinking)


# ══════════════════════════════════════════════════════════════════════════
# item (f) — text-only models
# ══════════════════════════════════════════════════════════════════════════
IMAGE_BLOCK = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
}


class TextOnlyCatalogDataTests(unittest.TestCase):
    """The flag is DATA in the catalog seed, pinned to the vendor docs."""

    def test_text_only_rows_are_exactly_the_documented_set(self) -> None:
        seed = load_seed()
        flagged = sorted(
            model_id for model_id, row in seed.rows.items() if row.text_only
        )
        # glm-5.3: z.ai "supports text-only inputs". deepseek-v4-pro /
        # deepseek-v4-flash-0731: QwenCloud "All other DeepSeek models accept
        # text input only" (only deepseek-v4.1-flash takes images). A NEW row
        # here needs its own vendor-doc citation in the seed.
        self.assertEqual(
            flagged, ["deepseek-v4-flash-0731", "deepseek-v4-pro", "glm-5.3"],
        )

    def test_the_verified_vision_models_are_not_flagged(self) -> None:
        seed = load_seed()
        for model_id in (
            "glm-5.3-flash",   # z.ai VLM: "Native multimodal input"
            "qwen3.8-max",     # QwenCloud Vision-page image examples
            "qwen3.8-flash",
            "deepseek-v4.1-flash",  # "accepts both text and image input"
        ):
            self.assertFalse(seed.rows[model_id].text_only, model_id)

    def test_the_flag_defaults_false(self) -> None:
        import dataclasses
        field = {
            f.name: f for f in dataclasses.fields(
                load_seed().rows["claude-opus-5"],
            )
        }["text_only"]
        self.assertIs(field.default, False)


class TextOnlyImageUnitTests(unittest.TestCase):
    def test_a_top_level_image_is_replaced(self) -> None:
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "what is this"}, IMAGE_BLOCK,
        ]}]
        out, count = _replace_image_blocks(messages, "[omitted]")
        self.assertEqual(count, 1)
        types = [b["type"] for b in out[0]["content"]]
        self.assertEqual(types, ["text", "text"])
        self.assertEqual(out[0]["content"][1]["text"], "[omitted]")

    def test_a_nested_image_in_a_tool_result_is_replaced(self) -> None:
        messages = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [IMAGE_BLOCK]},
        ]}]
        out, count = _replace_image_blocks(messages, "[omitted]")
        self.assertEqual(count, 1)
        self.assertEqual(
            out[0]["content"][0]["content"][0], {"type": "text", "text": "[omitted]"},
        )

    def test_no_image_is_a_noop(self) -> None:
        messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        out, count = _replace_image_blocks(messages, "[omitted]")
        self.assertEqual(count, 0)
        self.assertIs(out, messages)

    def test_strip_images_for_text_only_patches_the_payload(self) -> None:
        payload = {"model": "glm-5.3",
                   "messages": [{"role": "user", "content": [IMAGE_BLOCK]}]}
        out, count = _strip_images_for_text_only(payload, "glm-5.3")
        self.assertEqual(count, 1)
        self.assertEqual(
            out["messages"][0]["content"][0]["text"],
            TEXT_ONLY_IMAGE_NOTE.format(model="glm-5.3"),
        )

    def test_the_captured_glm_5_3_answer_is_the_evidence(self) -> None:
        # zai-image.body: the model was sent an image and could not see it.
        body = json.loads(_fixture("zai-image"))
        self.assertIn("cannot see images", body["content"][0]["thinking"])


class TextOnlyRouteIntegrationTests(GatewayTestBase):
    async def test_an_image_to_a_text_only_model_is_replaced(self) -> None:
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await self.client.post(
                "/v1/messages",
                headers=self.auth(),
                json={
                    "model": "claude-gw/glm-5.3",
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": "what is this"}, IMAGE_BLOCK,
                    ]}],
                },
            )
        forwarded = json.loads(self.vendor_up.requests[0]["body"])
        types = [b["type"] for b in forwarded["messages"][0]["content"]]
        self.assertEqual(types, ["text", "text"])
        self.assertIn("text-only", forwarded["messages"][0]["content"][1]["text"])
        line = next(m for m in captured.output if "requested=" in m)
        self.assertIn("note=text_only_images_omitted=1", line)

    async def test_an_image_to_a_vision_model_is_untouched(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-gw/glm-5.3-flash",
                "messages": [{"role": "user", "content": [IMAGE_BLOCK]}],
            },
        )
        forwarded = json.loads(self.vendor_up.requests[0]["body"])
        self.assertEqual(forwarded["messages"][0]["content"], [IMAGE_BLOCK])

    async def test_an_image_on_the_first_party_route_is_untouched(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-opus-5",
                "messages": [{"role": "user", "content": [IMAGE_BLOCK]}],
            },
        )
        forwarded = json.loads(self.anthropic_up.requests[0]["body"])
        self.assertEqual(forwarded["messages"][0]["content"], [IMAGE_BLOCK])

    async def test_a_text_only_model_with_no_image_is_untouched(self) -> None:
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-gw/glm-5.3",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        forwarded = json.loads(self.vendor_up.requests[0]["body"])
        self.assertEqual(forwarded["messages"][0]["content"], "hi")

    async def test_a_text_only_lookup_exception_relays_the_image(self) -> None:
        # _guarded: a defect must not fail the chat; the image goes on (today).
        with mock.patch(
            "model_router.server._apply_text_only_images",
            side_effect=RuntimeError("synthetic"),
        ):
            with self.assertLogs(LOGGER, level="INFO") as captured:
                resp = await self.client.post(
                    "/v1/messages",
                    headers=self.auth(),
                    json={
                        "model": "claude-gw/glm-5.3",
                        "messages": [{"role": "user", "content": [IMAGE_BLOCK]}],
                    },
                )
        self.assertEqual(resp.status, 200)
        forwarded = json.loads(self.vendor_up.requests[0]["body"])
        self.assertEqual(forwarded["messages"][0]["content"], [IMAGE_BLOCK])
        self.assertIn("note=rewrite_failed", "\n".join(captured.output))

    async def test_qwen_route_glm_5_3_is_also_text_only(self) -> None:
        # The flag is a property of the MODEL: one row answers for both routes.
        stub_url = self.vendor.upstream
        self.vendor = replace(VENDORS["qwen"], upstream=stub_url)
        self.client = await self.make_client()
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={
                "model": "claude-gw/qwen/glm-5.3",
                "messages": [{"role": "user", "content": [IMAGE_BLOCK]}],
            },
        )
        forwarded = json.loads(self.vendor_up.requests[0]["body"])
        self.assertEqual(forwarded["messages"][0]["content"][0]["type"], "text")


if __name__ == "__main__":
    unittest.main()
