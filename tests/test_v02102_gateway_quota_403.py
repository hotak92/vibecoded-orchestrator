# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 fix round: a vendor 403 that IS a quota refusal must not strike the key.

The incident (observed live four times, 2026-10-07..11): a vendor answers
HTTP 403 with a body saying "You've reached your 5-hour usage limit".
``_auth_rejection_response` classified it as a KEY-level rejection — an auth
strike that can invalidate the last-good key store — and relayed the 403, so
Claude Code rendered "Failed to authenticate" for what was a quota window.

The fix routes such an answer through the quota machinery BEFORE the auth
classification (``model_router.quota.is_quota_refusal`` reads the same
evidence helpers ``classify_quota`` uses): the client gets the same
429-naming-the-vendor answer — and the same fallback-ladder behaviour — a
vendor 429/402 gets, with NO auth strike. A 401 is never quota-routed, and a
403 without quota evidence classifies exactly as before.
"""
from __future__ import annotations

import unittest

from model_router import server as srv
from model_router.quota import (
    CLASS_EXHAUSTED,
    CLIENT_QUOTA_STATUS,
    classify_quota,
    is_quota_refusal,
)
from tests.test_v02102_gateway_routing import HAIKU, LadderTestBase
from tests.test_model_router_server import GatewayTestBase

ZAI_GLM = "claude-gw/glm-5.3"

#: The observed live shape: prose only — no vendor code, no reset key — so
#: the detector's answer rides on the shared "usage limit" exhaustion marker.
USAGE_LIMIT_403 = {
    "type": "error",
    "error": {
        "type": "permission_error",
        "message": "You've reached your 5-hour usage limit.",
    },
}

#: A genuine credential refusal: no quota evidence anywhere in the body.
INVALID_KEY_403 = {
    "type": "error",
    "error": {
        "type": "permission_error",
        "message": "invalid authentication credentials",
    },
}


class QuotaRefusalDetectionTests(unittest.TestCase):
    """The detector is the same evidence machinery the classifier uses."""

    def test_a_usage_limit_body_is_a_quota_refusal_whatever_the_status(self) -> None:
        body = b'{"error":{"message":"You\'ve reached your 5-hour usage limit."}}'
        self.assertTrue(is_quota_refusal(403, body))
        self.assertTrue(is_quota_refusal(429, body))

    def test_an_exhaustion_code_body_is_a_quota_refusal(self) -> None:
        self.assertTrue(is_quota_refusal(403, b'{"error":{"code":"1310"}}'))

    def test_a_readable_reset_time_is_quota_evidence(self) -> None:
        body = b'{"error":{"message":"limited","reset_time":1893456000}}'
        self.assertTrue(is_quota_refusal(403, body))

    def test_a_quota_status_is_a_quota_refusal_whatever_the_body(self) -> None:
        self.assertTrue(is_quota_refusal(429, b""))
        self.assertTrue(is_quota_refusal(402, b""))

    def test_a_credential_refusal_body_is_not_a_quota_refusal(self) -> None:
        self.assertFalse(is_quota_refusal(403, b'{"error":{"message":"bad key"}}'))
        self.assertFalse(is_quota_refusal(403, b""))

    def test_the_usage_limit_marker_is_exhaustion_evidence(self) -> None:
        body = b'{"error":{"message":"You\'ve reached your 5-hour usage limit."}}'
        self.assertEqual(classify_quota(403, body), CLASS_EXHAUSTED)


class Quota403Tests(GatewayTestBase):
    """Driven through the real proxy, on the no-rung zai route."""

    async def _post(self, model: str = ZAI_GLM):
        return await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": model, "messages": [], "max_tokens": 1},
        )

    async def test_a_usage_limit_403_becomes_a_429_naming_the_vendor_no_strike(
        self,
    ) -> None:
        """The headline case: same answer a 429 gets, strike counter untouched."""
        self.vendor_up.messages_status = 403
        self.vendor_up.messages_body = USAGE_LIMIT_403
        gw = self.client.app[srv.APP_KEY]
        response = await self._post()
        self.assertEqual(response.status, CLIENT_QUOTA_STATUS)
        data = await response.json()
        self.assertEqual(data["error"]["type"], "rate_limit_error")
        # The sentence names the UPSTREAM status (403), so support can tell
        # the disguise from an ordinary 429.
        self.assertIn("(HTTP 403)", data["error"]["message"])
        self.assertIn("z.ai", data["error"]["message"].lower())
        self.assertEqual(gw._vendor_auth_strikes, {})
        # EXHAUSTED verdicts are re-probed once, exactly like a 429: two
        # upstream attempts, then the substituted answer.
        self.assertEqual(len(self.vendor_up.message_requests), 2)

    async def test_a_genuine_invalid_key_403_still_strikes_and_relays(self) -> None:
        self.vendor_up.messages_status = 403
        self.vendor_up.messages_body = INVALID_KEY_403
        gw = self.client.app[srv.APP_KEY]
        response = await self._post()
        self.assertEqual(response.status, 403)
        self.assertEqual(await response.json(), INVALID_KEY_403)
        self.assertEqual(gw._vendor_auth_strikes, {"zai": 1})

    async def test_the_access_denied_model_403_stays_unchanged(self) -> None:
        """The documented model-level exemption keeps its verbatim relay."""
        body = {
            "type": "error",
            "error": {"type": "AccessDenied", "code": "access_denied"},
        }
        self.vendor_up.messages_status = 403
        self.vendor_up.messages_body = body
        gw = self.client.app[srv.APP_KEY]
        response = await self._post()
        self.assertEqual(response.status, 403)
        self.assertEqual(await response.json(), body)
        self.assertEqual(gw._vendor_auth_strikes, {})

    async def test_a_401_is_unaffected_even_with_quota_words(self) -> None:
        """A 401 is never quota-routed: key-level by definition, whatever the
        body says (the quota carve-out exists only for the 403 disguise)."""
        self.vendor_up.messages_status = 401
        self.vendor_up.messages_body = USAGE_LIMIT_403
        gw = self.client.app[srv.APP_KEY]
        response = await self._post()
        self.assertEqual(response.status, 401)
        self.assertEqual(await response.json(), USAGE_LIMIT_403)
        self.assertEqual(gw._vendor_auth_strikes, {"zai": 1})


class Quota403LadderTests(LadderTestBase):
    """The disguise gets the ladder a 429 gets: the next rung answers.

    A vendor route is laddered only when the attempt ARRIVED by fallback
    (batch-6 B-1), so the walk here is the full one: a first-party 429
    escalates to the zai rung, whose 403-in-disguise must escalate onward to
    qwen exactly as a 429 on that rung would."""

    async def test_a_usage_limit_403_on_a_laddered_rung_escalates(self) -> None:
        self.refuse(self.anthropic_up)  # first-party 429 exhausted → rung 1
        self.vendor_up.messages_status = 403
        self.vendor_up.messages_body = USAGE_LIMIT_403
        self.qwen_up.messages_body = {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "qwen3.8-flash",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        gw = self.client.app[srv.APP_KEY]
        response = await self.client.post(
            "/v1/messages",
            headers=self.chat(),
            json=self.ask(HAIKU),
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["model"], "qwen3.8-flash")
        self.assertEqual(len(self.anthropic_up.message_requests), 1)
        self.assertEqual(len(self.vendor_up.message_requests), 1)
        self.assertEqual(len(self.qwen_up.message_requests), 1)
        self.assertEqual(gw._vendor_auth_strikes, {})
