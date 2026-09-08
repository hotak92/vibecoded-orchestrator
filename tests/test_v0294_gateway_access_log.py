# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""One access line per request — the evidence the field incident did not have.

For four hours a session answered from a vendor model nobody had selected. The
gateway's log could not say so: it recorded the FORWARDED name only, so the
one question that mattered — "what did the client ask for?" — had to be
answered by scanning a 951 MB transcript.

Every assertion here is about that line existing, carrying ``requested``, and
carrying no body and no credential.
"""
from __future__ import annotations

import json
import unittest

from tests.test_model_router_server import HOST_TOKEN, GatewayTestBase

LOGGER = "model_router.server"


def _fields(line: str) -> dict[str, str]:
    """Parse ``k=v k=v`` out of one access line.

    Model ids arrive ``repr()``-quoted (a newline in a client-supplied id
    would otherwise forge a second log line), so the quotes come off here.
    """
    body = line.split("model-gateway: ", 1)[-1]
    out = {}
    for part in body.split(" "):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        out[key] = value
    return out


class AccessLogTests(GatewayTestBase):
    async def test_a_vendor_request_logs_every_documented_field(self) -> None:
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await self.client.post(
                "/v1/messages",
                headers=self.auth(),
                json={"model": "claude-gw/glm-5.3[1m]", "messages": []},
            )
        line = next(m for m in captured.output if "requested=" in m)
        fields = _fields(line)
        self.assertEqual(fields["requested"], "claude-gw/glm-5.3[1m]")
        self.assertEqual(fields["route"], "vendor:zai")
        self.assertEqual(fields["forward"], "glm-5.3")
        self.assertEqual(fields["status"], "200")
        self.assertEqual(fields["stream"], "false")
        self.assertTrue(fields["ms"].isdigit())

    async def test_the_first_party_route_is_labelled_anthropic(self) -> None:
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await self.client.post(
                "/v1/messages",
                headers=self.auth(),
                json={"model": "claude-opus-5", "messages": []},
            )
        fields = _fields(next(m for m in captured.output if "requested=" in m))
        self.assertEqual(fields["route"], "anthropic")
        self.assertEqual(fields["forward"], "claude-opus-5")

    async def test_a_streaming_request_is_marked_as_such(self) -> None:
        self.vendor_up.stream_chunks = [b"event: ping\ndata: {}\n\n"]
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await self.client.post(
                "/v1/messages",
                headers=self.auth(),
                json={"model": "claude-gw/glm-5.3", "messages": [],
                      "stream": True},
            )
        fields = _fields(next(m for m in captured.output if "requested=" in m))
        self.assertEqual(fields["stream"], "true")

    async def test_a_refusal_is_logged_with_its_reason(self) -> None:
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await self.client.post(
                "/v1/messages",
                headers=self.auth(),
                json={"model": "gpt-9", "messages": []},
            )
        line = next(m for m in captured.output if "requested=" in m)
        self.assertIn("refused", line)
        fields = _fields(line)
        self.assertEqual(fields["requested"], "gpt-9")
        self.assertEqual(fields["route"], "refused")
        self.assertEqual(fields["status"], "400")
        self.assertEqual(fields["reason"], "unknown_model")

    async def test_a_claude_id_aimed_at_a_vendor_is_logged_as_refused(self) -> None:
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await self.client.post(
                "/v1/messages",
                headers=self.auth(),
                json={"model": "claude-gw/claude-opus-5", "messages": []},
            )
        fields = _fields(next(m for m in captured.output if "requested=" in m))
        self.assertEqual(fields["reason"], "claude_id_to_vendor")

    async def test_a_missing_vendor_key_still_produces_a_line(self) -> None:
        client = await self.make_client(
            key_getter=lambda key, project=None: (_ for _ in ()).throw(
                LookupError("absent"),
            ),
        )
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await client.post(
                "/v1/messages",
                headers=self.auth(),
                json={"model": "claude-gw/glm-5.3", "messages": []},
            )
        fields = _fields(next(m for m in captured.output if "requested=" in m))
        self.assertEqual(fields["status"], "503")
        self.assertEqual(fields["reason"], "vendor_key_unavailable")

    async def test_a_missing_claude_login_still_produces_a_line(self) -> None:
        self.write_credentials(None, expires_in_ms=3_600_000)
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await self.client.post(
                "/v1/messages",
                headers=self.auth(),
                json={"model": "claude-opus-5", "messages": []},
            )
        fields = _fields(next(m for m in captured.output if "requested=" in m))
        self.assertEqual(fields["status"], "401")
        self.assertEqual(fields["reason"], "claude_login_unavailable")

    async def test_an_unreachable_upstream_still_produces_a_line(self) -> None:
        await self.vendor_up.stop()
        self.vendor_up.server = None
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await self.client.post(
                "/v1/messages",
                headers=self.auth(),
                json={"model": "claude-gw/glm-5.3", "messages": []},
            )
        fields = _fields(next(m for m in captured.output if "requested=" in m))
        self.assertEqual(fields["status"], "502")

    async def test_a_newline_in_the_model_id_cannot_forge_a_second_line(self) -> None:
        forged = "claude-gw/glm-5.3\nmodel-gateway: requested=x status=200"
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await self.client.post(
                "/v1/messages",
                headers=self.auth(),
                json={"model": forged, "messages": []},
            )
        line = next(m for m in captured.output if "requested=" in m)
        self.assertIn("\\n", line, "the newline is escaped, not emitted raw")
        self.assertEqual(len(line.splitlines()), 1)

    async def test_the_line_carries_neither_a_body_nor_a_credential(self) -> None:
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await self.client.post(
                "/v1/messages",
                headers=self.auth(),
                json={
                    "model": "claude-gw/glm-5.3",
                    "messages": [
                        {"role": "user", "content": "a-secret-sentence-in-the-body"},
                    ],
                },
            )
        blob = "\n".join(captured.output)
        self.assertNotIn("a-secret-sentence-in-the-body", blob)
        self.assertNotIn(HOST_TOKEN, blob)
        self.assertNotIn("Bearer", blob)

    async def test_a_request_refused_at_the_door_is_logged_too(self) -> None:
        """The last terminal outcome the log was silent about.

        A 401 returned before any line was written, which made an
        unauthenticated probe invisible — and "the gateway is not answering
        me" is exactly the report that sends someone to this log.
        """
        with self.assertLogs(LOGGER, level="INFO") as captured:
            resp = await self.client.post(
                "/v1/messages", json={"model": "claude-opus-5", "messages": []},
            )
        self.assertEqual(resp.status, 401)
        line = next(m for m in captured.output if "requested=" in m)
        fields = _fields(line)
        self.assertEqual(fields["route"], "refused")
        self.assertEqual(fields["status"], "401")
        self.assertEqual(fields["reason"], "unauthorised")
        self.assertEqual(fields["method"], "POST")
        self.assertEqual(fields["path"], "/v1/messages")
        self.assertEqual(fields["requested"], "-", "the body is never read")

    async def test_the_catalog_route_refuses_in_the_same_shape(self) -> None:
        """Leaving /v1/models silent would move the gap, not close it — and
        the catalog call is the FIRST one a misconfigured client makes."""
        with self.assertLogs(LOGGER, level="INFO") as captured:
            resp = await self.client.get("/v1/models")
        self.assertEqual(resp.status, 401)
        line = next(m for m in captured.output if "requested=" in m)
        fields = _fields(line)
        self.assertEqual(fields["reason"], "unauthorised")
        self.assertEqual(fields["method"], "GET")
        self.assertEqual(fields["path"], "/v1/models")

    async def test_the_401_line_carries_no_header_and_no_token(self) -> None:
        """It is the one line an UNCREDENTIALED caller can cause, so what it
        may contain is the narrowest question in this file."""
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await self.client.post(
                "/v1/messages",
                headers={"Authorization": "Bearer wrong-token-9e1f"},
                json={"model": "x", "messages": [{"role": "user",
                                                  "content": "secret-body"}]},
            )
        blob = "\n".join(captured.output)
        self.assertNotIn("wrong-token-9e1f", blob)
        self.assertNotIn("Bearer", blob)
        self.assertNotIn("secret-body", blob)
        self.assertNotIn(HOST_TOKEN, blob)

    async def test_a_burst_of_probes_is_capped_and_says_how_many(self) -> None:
        """The cap exists because this is the only line a caller without the
        token can make us write, and the daemon's log is not rotated while it
        runs. Suppression is COUNTED, never silent."""
        from model_router import server as srv

        with self.assertLogs(LOGGER, level="INFO") as captured:
            for _ in range(5):
                await self.client.post("/v1/messages", json={"model": "x"})
        lines = [m for m in captured.output if "reason=unauthorised" in m]
        self.assertEqual(len(lines), 1, f"five probes, one line: {lines}")

        # Past the window the next probe is logged, and it says what it hid.
        gateway = self.client.server.app[srv.APP_KEY]
        for peer in list(gateway._unauthorised_seen):
            when, count = gateway._unauthorised_seen[peer]
            gateway._unauthorised_seen[peer] = (
                when - srv.UNAUTHORISED_LOG_WINDOW_S - 1, count,
            )
        with self.assertLogs(LOGGER, level="INFO") as captured:
            await self.client.post("/v1/messages", json={"model": "x"})
        line = next(m for m in captured.output if "reason=unauthorised" in m)
        self.assertIn("suppressed=4", line)

    async def test_the_vendor_upstream_never_sees_the_local_host_token(self) -> None:
        """Adjacent to the log rule and cheap to pin while we are here."""
        await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        headers = self.vendor_up.message_requests[-1]["headers"]
        self.assertNotIn(HOST_TOKEN, json.dumps(dict(headers)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
