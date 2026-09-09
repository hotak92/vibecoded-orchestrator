# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A conversation bigger than 1 MiB must reach its model.

2026-09-09, from the gateway's own access log: every ``POST /v1/messages``
Claude Code sent through the daemon came back 413 — from the daemon, not from
Anthropic, and the same conversation worked natively. The gateway built its
application with aiohttp's DEFAULT ``client_max_size`` of 1 MiB while
``messages_handler`` did ``await request.read()``, so any request JSON past
roughly 250K tokens of context (far less once images are pasted) could not be
served at all. Claude Code renders EVERY 413 as "Request too large (max 32MB).
Accumulated images and attachments…", so the gateway's own refusal read as the
user's transcript being at fault, and the conversation was blamed for hours.

The ruling this file pins is stronger than "raise the limit": **the gateway
never refuses a request for its size.** aiohttp is given no ceiling, the id
rewrite is bounded instead by what the daemon is willing to HOLD, and a body
past that bound is streamed upstream unrewritten so that the upstream's own
answer — a completion, or Anthropic's own 413 — is what the client sees. A
proxy that answers what the upstream would have served is a failure the user
cannot route around.

Red-proof: :class:`LargeBodyTests` fails on the pre-fix gateway, with 413.
"""
from __future__ import annotations

import json
import os
import threading
import unittest
from unittest import mock

from model_router.config import REWRITE_BUFFER_LIMIT_BYTES, GatewayConfig
from model_router.server import (
    HEAD_SCAN_BYTES,
    REWRITE_BUFFER_NOTE,
    UNBOUNDED_CLIENT_MAX_SIZE,
    _shallow_top_level_fields,
    _splice_literal,
    create_app,
)

# The access line's ``k=v`` parser has ONE home in the suite; a second copy
# would drift from the very shape it is meant to be pinning.
from tests.test_v0294_gateway_access_log import LOGGER, _fields
from tests.test_model_router_server import GatewayTestBase

#: Comfortably past aiohttp's 1 MiB default and small enough to move over
#: loopback twice (client -> gateway -> stub) without slowing the suite.
BIG_BODY_BYTES = 2 * 1024 * 1024

#: What the STUB upstreams will accept. They are ordinary aiohttp apps, so
#: they carry the very 1 MiB default this file is about; lifting it keeps them
#: out of the way, since a 413 from a stub would pass a test about the
#: gateway for entirely the wrong reason.
STUB_BODY_LIMIT = 16 * 1024 * 1024


def _payload_of(nbytes: int, *, model: str = "claude-gw/glm-5.3") -> bytes:
    """A valid Anthropic-shaped request whose encoding is about ``nbytes``."""
    return json.dumps(
        {
            "model": model,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "x" * nbytes}],
        }
    ).encode("utf-8")


class _BigBodyBase(GatewayTestBase):
    """The shared harness with the stubs' own request ceilings lifted."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        for upstream in (self.vendor_up, self.anthropic_up):
            assert upstream.server is not None
            # aiohttp reads `client_max_size` per request, so setting it on
            # the already-running stub app is enough. Re-declaring the stub's
            # routes here to pass the argument at construction would mirror
            # the harness, and a mirrored stub is one that stops matching.
            upstream.server.app._client_max_size = STUB_BODY_LIMIT


class LargeBodyTests(_BigBodyBase):
    """RED-PROOF: on the shipped 0.2.93 gateway these answer 413."""

    async def test_a_two_mib_body_is_served_and_rewritten(self) -> None:
        raw = _payload_of(BIG_BODY_BYTES)
        self.assertGreater(len(raw), 1024 * 1024, "the body must exceed 1 MiB")
        resp = await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "Content-Type": "application/json"},
            data=raw,
        )
        self.assertEqual(resp.status, 200, await resp.text())
        forwarded = self.vendor_up.message_requests
        self.assertEqual(len(forwarded), 1)
        self.assertGreater(len(forwarded[0]["body"]), 1024 * 1024)
        # Still the ordinary path: 2 MiB is well inside the rewrite buffer,
        # so the namespace is stripped in the bytes the vendor receives.
        self.assertEqual(json.loads(forwarded[0]["body"])["model"], "glm-5.3")

    async def test_count_tokens_takes_the_same_body_as_messages(self) -> None:
        """The route Claude Code calls FIRST on a long conversation.

        A ceiling on one of the two would refuse the size probe and leave the
        completion looking fine in isolation.
        """
        resp = await self.client.post(
            "/v1/messages/count_tokens",
            headers={**self.auth(), "Content-Type": "application/json"},
            data=_payload_of(BIG_BODY_BYTES),
        )
        self.assertEqual(resp.status, 200, await resp.text())
        self.assertEqual(
            self.vendor_up.message_requests[0]["path"], "/v1/messages/count_tokens",
        )


class OverTheRewriteBufferTests(_BigBodyBase):
    """Past the buffer the request is STREAMED on, never refused."""

    #: Small on purpose. The property under test is "the configured bound is
    #: what switches paths"; pushing 32 MiB through loopback to prove it would
    #: only make the suite slower.
    BUFFER = 64 * 1024
    OVERSIZE = 256 * 1024

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.config.rewrite_buffer_bytes = self.BUFFER
        self.client = await self.make_client()

    async def _post_oversize(self, **kwargs):
        return await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "Content-Type": "application/json"},
            data=_payload_of(self.OVERSIZE, **kwargs),
        )

    async def test_it_is_forwarded_whole_with_only_the_id_spliced(self) -> None:
        """One edit, and it is the one only the gateway can make.

        ``claude-gw/`` is the gateway's OWN namespace — no vendor has heard of
        it — so forwarding it verbatim would make the gateway the author of
        the 400 that came back. Everything else in the body is the client's
        and stays byte-identical.
        """
        sent = _payload_of(self.OVERSIZE)
        resp = await self._post_oversize()
        self.assertEqual(resp.status, 200, await resp.text())

        forwarded = self.vendor_up.message_requests
        self.assertEqual(len(forwarded), 1)
        body = forwarded[0]["body"]
        self.assertEqual(json.loads(body)["model"], "glm-5.3")
        # Whole: head + streamed remainder, differing from what the client
        # sent by exactly the namespace that was spliced out of the id.
        self.assertEqual(len(body), len(sent) - len("claude-gw/"))
        self.assertEqual(
            body, sent.replace(b'"claude-gw/glm-5.3"', b'"glm-5.3"', 1),
        )

    async def test_the_one_m_suffix_is_handled_exactly_as_the_normal_path(
        self,
    ) -> None:
        """Same id upstream, same beta header, whichever path it took.

        ``[1m]`` is Claude Code's client-side spelling and the 1M window is
        bought with the ``context-1m`` BETA HEADER (see
        :mod:`model_router.routing`), so the suffix must come out of the body
        on both paths and the header must go on in both. Comparing the two
        rather than asserting a literal is what keeps them from drifting.
        """
        model = "claude-fable-5-1[1m]"
        small = json.dumps({"model": model, "messages": [], "max_tokens": 8})
        await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "Content-Type": "application/json"},
            data=small.encode("utf-8"),
        )
        await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "Content-Type": "application/json"},
            data=_payload_of(self.OVERSIZE, model=model),
        )
        normal, over_buffer = self.anthropic_up.message_requests
        self.assertLess(len(normal["body"]), self.BUFFER, "the first one fit")
        self.assertGreater(len(over_buffer["body"]), self.BUFFER)

        self.assertEqual(json.loads(normal["body"])["model"], "claude-fable-5-1")
        self.assertEqual(
            json.loads(over_buffer["body"])["model"],
            json.loads(normal["body"])["model"],
        )
        self.assertIn("context-1m", normal["headers"]["anthropic-beta"])
        self.assertEqual(
            over_buffer["headers"]["anthropic-beta"],
            normal["headers"]["anthropic-beta"],
        )

    async def test_an_id_needing_no_splice_is_left_alone(self) -> None:
        """A bare first-party id is already what the upstream expects."""
        sent = _payload_of(self.OVERSIZE, model="claude-opus-5")
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await self.client.post(
                "/v1/messages",
                headers={**self.auth(), "Content-Type": "application/json"},
                data=sent,
            )
        self.assertEqual(self.anthropic_up.message_requests[0]["body"], sent)
        line = next(m for m in captured.output if "requested=" in m)
        self.assertIn("model_id=verbatim", line)

    async def test_the_upstream_status_is_what_the_client_gets(self) -> None:
        self.vendor_up.messages_status = 400
        self.vendor_up.messages_body = {"error": {"message": "vendor said no"}}
        resp = await self._post_oversize()
        self.assertEqual(resp.status, 400)
        self.assertIn("vendor said no", await resp.text())

    async def test_the_access_line_says_it_was_streamed_through(self) -> None:
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await self._post_oversize()
        line = next(m for m in captured.output if "requested=" in m)
        self.assertIn(REWRITE_BUFFER_NOTE, line)
        self.assertIn("forwarded_unrewritten", line)
        self.assertIn(f"bytes>={self.BUFFER}", line)
        # ``forwarded_unrewritten`` is about the TOOL-ID repair; the model id
        # is spliced, and the line says which of the two happened.
        self.assertIn("model_id=spliced", line)
        fields = _fields(line)
        self.assertEqual(fields["requested"], "claude-gw/glm-5.3")
        self.assertEqual(fields["route"], "vendor:zai")
        self.assertEqual(fields["status"], "200")

    async def test_the_first_party_route_streams_through_too(self) -> None:
        """Anthropic's own login path — the one the field defect broke."""
        sent = _payload_of(self.OVERSIZE, model="claude-opus-5")
        resp = await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "Content-Type": "application/json"},
            data=sent,
        )
        self.assertEqual(resp.status, 200, await resp.text())
        forwarded = self.anthropic_up.message_requests
        self.assertEqual(len(forwarded), 1)
        self.assertEqual(forwarded[0]["body"], sent)
        self.assertTrue(
            forwarded[0]["headers"].get("Authorization", "").startswith("Bearer "),
            "the Claude login still authorises the streamed request",
        )

    async def test_a_body_whose_model_is_unreadable_is_the_only_refusal(self) -> None:
        """Unroutable, not too big — and it says so.

        The model sits after a messages array longer than the head scan, so
        the gateway cannot tell which upstream (or whose credential) the
        request is for. Refusing THAT is not refusing a size: the same body
        with an early ``model`` is forwarded, which the test above proves.
        """
        body = json.dumps(
            {
                "messages": [{"role": "user", "content": "y" * self.OVERSIZE}],
                "model": "claude-gw/glm-5.3",
            }
        ).encode("utf-8")
        with self.assertLogs(LOGGER, level="INFO") as captured:
            resp = await self.client.post(
                "/v1/messages",
                headers={**self.auth(), "Content-Type": "application/json"},
                data=body,
            )
        self.assertEqual(resp.status, 400)
        payload = await resp.json()
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        self.assertIn("model", payload["error"]["message"])
        fields = _fields(next(m for m in captured.output if "requested=" in m))
        self.assertEqual(fields["reason"], "model_unreadable_in_head")
        self.assertEqual(self.vendor_up.message_requests, [])
        self.assertEqual(self.anthropic_up.message_requests, [])


class UpstreamRefusalRelayTests(_BigBodyBase):
    """When a 413 IS the right answer it is the upstream's, verbatim."""

    async def test_an_upstream_413_reaches_the_client_unchanged(self) -> None:
        upstream_body = json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "request_too_large",
                    "message": "Request exceeds the maximum allowed size",
                },
            }
        ).encode("utf-8")
        self.anthropic_up.messages_status = 413
        self.anthropic_up.messages_raw = upstream_body
        resp = await self.client.post(
            "/v1/messages",
            headers={**self.auth(), "Content-Type": "application/json"},
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(resp.status, 413)
        self.assertEqual(await resp.read(), upstream_body)
        self.assertEqual(
            json.loads(await resp.read())["error"]["type"], "request_too_large",
            "the vendor's own error type survives the relay",
        )


class HeadScanTests(unittest.TestCase):
    """Routing an unparsed body is exact or it is nothing.

    A regex would send somebody's conversation to the wrong upstream with the
    wrong key; these pin that only PROVABLY top-level fields are read.
    """

    def test_the_flat_head_is_read(self) -> None:
        fields = _shallow_top_level_fields(
            b'{"model": "claude-opus-5", "stream": true, "max_tokens": 8}'
        )
        self.assertEqual(fields["model"].value, "claude-opus-5")
        self.assertEqual(fields["stream"].value, "true")
        self.assertEqual(fields["max_tokens"].value, "8")

    def test_a_nested_model_is_not_mistaken_for_the_requests_own(self) -> None:
        fields = _shallow_top_level_fields(
            b'{"messages": [{"role": "user", "content": {"model": "WRONG"}}],'
            b' "model": "claude-gw/glm-5.3"}'
        )
        self.assertEqual(fields["model"].value, "claude-gw/glm-5.3")

    def test_a_model_inside_a_string_is_not_read(self) -> None:
        fields = _shallow_top_level_fields(
            b'{"system": "say \\"model\\": \\"WRONG\\" back", "model": "right"}'
        )
        self.assertEqual(fields["model"].value, "right")

    def test_an_escaped_id_is_decoded_as_json_decodes_it(self) -> None:
        self.assertEqual(
            _shallow_top_level_fields(b'{"model": "a\\u002db"}')["model"].value,
            "a-b",
        )

    def test_a_truncated_head_yields_what_it_proved(self) -> None:
        fields = _shallow_top_level_fields(b'{"model": "claude-opus-5", "messa')
        self.assertEqual(fields["model"].value, "claude-opus-5")

    def test_a_field_past_the_scan_window_is_simply_absent(self) -> None:
        body = json.dumps(
            {"messages": [{"content": "z" * (HEAD_SCAN_BYTES + 1024)}],
             "model": "claude-opus-5"}
        ).encode("utf-8")
        self.assertNotIn("model", _shallow_top_level_fields(body))

    def test_the_span_bounds_the_literal_a_splice_replaces(self) -> None:
        """The span is what makes the over-buffer path able to fix the id.

        It covers the QUOTED literal, so the replacement is written by
        ``json.dumps`` rather than by string surgery inside the quotes.
        """
        body = b'{"model": "claude-gw/glm-5.3", "messages": []}'
        field = _shallow_top_level_fields(body)["model"]
        self.assertEqual(body[field.start:field.end], b'"claude-gw/glm-5.3"')
        self.assertEqual(
            _splice_literal(body, field, "glm-5.3"),
            b'{"model": "glm-5.3", "messages": []}',
        )

    def test_a_splice_survives_an_escaped_neighbour(self) -> None:
        """Nothing around the literal moves — spans are byte offsets."""
        body = b'{"system": "\\u00e8 \\"quoted\\"", "model": "claude-gw/x"}'
        field = _shallow_top_level_fields(body)["model"]
        spliced = _splice_literal(body, field, "x")
        self.assertEqual(json.loads(spliced)["model"], "x")
        self.assertEqual(
            json.loads(spliced)["system"], json.loads(body)["system"],
        )

    def test_a_malformed_body_cannot_hang_the_walk(self) -> None:
        """The bare-literal scan must always MOVE.

        A ``]`` at depth 1 matches no branch and stops the literal scan on
        its first byte, so the walk re-examined it forever — on the event
        loop. One such request (any oversized body with a stray bracket)
        froze the whole daemon: every other session on the machine included.
        Run in a thread with a deadline, because a regression here does not
        fail, it hangs.
        """
        shapes = [
            b'{"a":1]',
            b'{"a":]',
            b'{"model":"x",]',
            b'{"a":1,"b":]}',
            b'{"model":"x"]}',
            b'{]',
        ]
        for shape in shapes:
            with self.subTest(body=shape):
                done: list = []

                def walk(payload: bytes = shape) -> None:
                    done.append(_shallow_top_level_fields(payload))

                worker = threading.Thread(target=walk, daemon=True)
                worker.start()
                worker.join(timeout=5)
                self.assertTrue(
                    done, f"_shallow_top_level_fields hung on {shape!r}",
                )

    def test_a_malformed_body_after_a_readable_model_keeps_what_it_proved(
        self,
    ) -> None:
        """Stopping early is not forgetting: the id before the damage stands."""
        fields = _shallow_top_level_fields(b'{"model":"claude-opus-5","a":1]')
        self.assertEqual(fields["model"].value, "claude-opus-5")

    def test_a_non_object_body_yields_nothing(self) -> None:
        self.assertEqual(_shallow_top_level_fields(b'["model", "x"]'), {})
        self.assertEqual(_shallow_top_level_fields(b""), {})


class LimitWiringTests(unittest.TestCase):
    """The constant, the knob, and what aiohttp is actually told."""

    def test_the_application_imposes_no_ceiling(self) -> None:
        """`_client_max_size` is aiohttp's only accessor for it (3.13.5).

        Zero is its "no limit"; the pre-fix app, built without the argument,
        reports 1 MiB here — which is what made the field defect reachable.
        The VALUE is pinned here and the BEHAVIOUR by :class:`LargeBodyTests`,
        because a future aiohttp could keep the spelling and change the rule.
        """
        self.assertEqual(UNBOUNDED_CLIENT_MAX_SIZE, 0)
        app = create_app(GatewayConfig(token="t"))
        self.assertEqual(app._client_max_size, UNBOUNDED_CLIENT_MAX_SIZE)

    def test_the_rewrite_buffer_sits_above_anthropics_request_ceiling(self) -> None:
        """32 MiB — just above the documented 32 MB, on the safe side.

        Rounding up is the point: every body the first-party upstream can
        accept is one the gateway still rewrites, and only bodies upstream
        would refuse anyway take the streaming path.
        """
        self.assertEqual(REWRITE_BUFFER_LIMIT_BYTES, 32 * 1024 * 1024)
        self.assertGreater(REWRITE_BUFFER_LIMIT_BYTES, 32_000_000)
        self.assertEqual(
            GatewayConfig(token="t").rewrite_buffer_bytes, REWRITE_BUFFER_LIMIT_BYTES,
        )

    def test_the_env_knob_reaches_the_config(self) -> None:
        with mock.patch.dict(
            os.environ, {"VCT_MODEL_GATEWAY_REWRITE_BUFFER_BYTES": "5000"},
            clear=False,
        ):
            self.assertEqual(GatewayConfig.from_env().rewrite_buffer_bytes, 5000)

    def test_a_junk_knob_falls_back_to_the_default(self) -> None:
        """A typo must not be able to reach an unbounded buffer."""
        for junk in ("", "lots", "-1", "0"):
            with self.subTest(junk=junk):
                with mock.patch.dict(
                    os.environ,
                    {"VCT_MODEL_GATEWAY_REWRITE_BUFFER_BYTES": junk},
                    clear=False,
                ):
                    self.assertEqual(
                        GatewayConfig.from_env().rewrite_buffer_bytes,
                        REWRITE_BUFFER_LIMIT_BYTES,
                    )

    def test_the_compact_json_shape_has_exactly_one_home(self) -> None:
        """Three call sites re-encode JSON; one of them forgetting is the bug.

        `ensure_ascii=True` inflates non-ASCII threefold and the default
        separators add a byte per field — either one can push a body the
        client sized correctly past the upstream's limit. Identity, not
        equality: two dicts that happen to match today are two dicts to keep
        in step.
        """
        from model_router import server as srv
        from model_router.tool_ids import COMPACT_JSON

        self.assertIs(srv.COMPACT_JSON, COMPACT_JSON)
        self.assertEqual(
            COMPACT_JSON, {"ensure_ascii": False, "separators": (",", ":")},
        )

    def test_the_two_thirty_two_mib_constants_stay_separate(self) -> None:
        """Same number, different reasons to move — see both docstrings."""
        from model_router.tool_ids import SSE_BUFFER_LIMIT_BYTES

        self.assertEqual(SSE_BUFFER_LIMIT_BYTES, REWRITE_BUFFER_LIMIT_BYTES)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
