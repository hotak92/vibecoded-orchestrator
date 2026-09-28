# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The access line's ``images=N`` — a count, never a content byte.

The gateway forwards request bodies verbatim, so an image cannot be lost
inside it — but nothing recorded whether one arrived at all, which made the
decisive question ("did the client send the image?") unanswerable after the
fact. These tests pin the diagnostic that closes that gap:

* one top-level ``type == "image"`` block counts 1;
* an image nested inside a ``tool_result`` block's own ``content[]`` counts —
  that is how a subagent that read an image file sends it;
* a request with NO image still emits the field, as ``images=0``: an absent
  field cannot distinguish "no image" from "counter not running";
* a body the gateway cannot parse yields the sentinel ``images=?`` and the
  request still proceeds — a diagnostic must never cost a chat;
* the body that reaches the upstream is byte-identical to what the client
  sent: the count reads the already-parsed object and the forwarded bytes
  are untouched by it.
"""
from __future__ import annotations

import json
import logging
import re
import unittest

from tests.test_model_router_server import GatewayTestBase  # noqa: I001

ACCESS_LOGGER = "model_router.server"


def _image(data: str = "aGk=") -> dict:
    """One Anthropic-shaped base64 image block; the data is not a real png."""
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
    }


class ImageCountTests(GatewayTestBase):
    """``images=N`` on the access line, asserted against the logged record."""

    async def _post(self, **post_kwargs: object) -> tuple[object, str]:
        """POST /v1/messages and return (response, the one access line)."""
        with self.assertLogs(ACCESS_LOGGER, level=logging.INFO) as captured:
            resp = await self.client.post("/v1/messages", **post_kwargs)
        lines = [
            out for out in captured.output if "model-gateway: requested=" in out
        ]
        self.assertTrue(lines, "no access line was logged for the request")
        return resp, lines[0]

    async def test_a_top_level_image_counts_one(self) -> None:
        payload = {
            "model": "claude-opus-5",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        _image(),
                    ],
                },
            ],
            "max_tokens": 8,
        }
        resp, line = await self._post(headers=self.auth(), json=payload)
        self.assertEqual(resp.status, 200)
        self.assertIn(" images=1", line)

    async def test_an_image_nested_in_a_tool_result_counts(self) -> None:
        payload = {
            "model": "claude-opus-5",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01",
                            "content": [_image()],
                        },
                    ],
                },
            ],
            "max_tokens": 8,
        }
        resp, line = await self._post(headers=self.auth(), json=payload)
        self.assertEqual(resp.status, 200)
        self.assertIn(" images=1", line)

    async def test_top_level_and_nested_images_count_together(self) -> None:
        payload = {
            "model": "claude-opus-5",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        _image(),
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_02",
                            "content": [_image(), _image()],
                        },
                    ],
                },
            ],
            "max_tokens": 8,
        }
        resp, line = await self._post(headers=self.auth(), json=payload)
        self.assertEqual(resp.status, 200)
        self.assertIn(" images=3", line)

    async def test_no_images_counts_zero_and_the_field_is_still_emitted(
        self,
    ) -> None:
        payload = {
            "model": "claude-opus-5",
            # Plain-string content: not every message carries a block array.
            "messages": [{"role": "user", "content": "just text"}],
            "max_tokens": 8,
        }
        resp, line = await self._post(headers=self.auth(), json=payload)
        self.assertEqual(resp.status, 200)
        # The FIELD is present — "no image" is evidence (images=0), not
        # silence, and only "could not count" may read images=?.
        self.assertIn(" images=0", line)
        self.assertIsNotNone(re.search(r" images=\d+", line))

    async def test_an_undecodable_body_yields_the_sentinel_and_proceeds(
        self,
    ) -> None:
        raw = b"{not json at all"
        resp, line = await self._post(
            headers={**self.auth(), "Content-Type": "application/json"},
            data=raw,
        )
        self.assertEqual(resp.status, 200)
        self.assertIn(" images=?", line)
        # The request still proceeded: the first-party upstream got the bytes.
        self.assertEqual(self.anthropic_up.message_requests[0]["body"], raw)

    async def test_the_forwarded_body_is_byte_identical_with_an_image(
        self,
    ) -> None:
        raw = json.dumps(
            {
                "model": "claude-opus-5",
                "messages": [
                    {"role": "user", "content": [_image(data="QQ==")]},
                ],
                "max_tokens": 8,
            },
        ).encode("utf-8")
        resp, line = await self._post(
            headers={**self.auth(), "Content-Type": "application/json"},
            data=raw,
        )
        self.assertEqual(resp.status, 200)
        self.assertIn(" images=1", line)
        # Requirement: count from the parsed copy, never re-serialise what
        # goes upstream — so the BYTES the stub received are the client's.
        forwarded = self.anthropic_up.message_requests[0]["body"]
        self.assertIsInstance(forwarded, bytes)
        self.assertEqual(forwarded, raw)
        self.assertIn(b'"data": "QQ=="', forwarded)

    async def test_a_routing_refusal_carries_the_count_it_refused_on(self) -> None:
        """A refused request still CARRIED its images, so the count is evidence.

        This outcome returns before the proxy, and ``_proxy`` is where the
        field is otherwise assembled — so without it here the one reader who
        most needs the count (someone asking why a request was refused) can
        tell "carried none" from "was never counted" no better than before the
        sentinel existed.
        """
        payload = {
            # No vendor serves this id, so routing refuses it.
            "model": "gpt-9",
            "messages": [{"role": "user", "content": [_image()]}],
            "max_tokens": 8,
        }
        resp, line = await self._post(headers=self.auth(), json=payload)
        self.assertEqual(resp.status, 400)
        self.assertIn("route=refused", line)
        self.assertIn(" images=1", line)

    async def test_a_gateway_side_refusal_carries_the_count_too(self) -> None:
        """The other refusal arm, which never reaches ``_proxy`` either."""
        client = await self.make_client(
            key_getter=lambda key, project=None: (_ for _ in ()).throw(
                LookupError("absent"),
            ),
        )
        with self.assertLogs(ACCESS_LOGGER, level=logging.INFO) as captured:
            resp = await client.post(
                "/v1/messages",
                headers=self.auth(),
                json={
                    "model": "claude-gw/glm-5.3",
                    "messages": [{"role": "user", "content": [_image()]}],
                    "max_tokens": 8,
                },
            )
        lines = [
            out for out in captured.output if "model-gateway: requested=" in out
        ]
        self.assertTrue(lines, "no access line was logged for the request")
        self.assertEqual(resp.status, 503)
        self.assertIn("reason=vendor_key_unavailable", lines[0])
        self.assertIn(" images=1", lines[0])

    async def test_a_refusal_of_an_unparseable_body_reads_the_sentinel(
        self,
    ) -> None:
        """``?``, not silence: the refusal arm keeps the distinction too.

        The body is unreadable AND the request is refused, so nothing was
        counted and nothing could have been — which is exactly the state ``?``
        is for. A missing field here would read the same as ``images=0``.
        """
        self.write_credentials(None, expires_in_ms=3_600_000)
        resp, line = await self._post(
            headers={**self.auth(), "Content-Type": "application/json"},
            data=b"{not json at all",
        )
        self.assertEqual(resp.status, 401)
        self.assertIn("reason=claude_login_unavailable", line)
        self.assertIn(" images=?", line)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
