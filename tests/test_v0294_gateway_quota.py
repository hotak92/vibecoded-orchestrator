# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Vendor quota exhaustion: the ONE status the gateway does not relay verbatim.

Field incident (2026-09-08): a vendor answered ``429 [1310] Weekly/Monthly
Limit Exhausted`` for four hours. The gateway relayed it byte for byte, the
user — whose client was pointed at ``127.0.0.1`` — read it as an ANTHROPIC
limit, and stopped working while a Claude subscription sat idle in the same
picker. Every assertion here exists to make that specific misreading
impossible: the vendor is NAMED, the remedy is stated, and the message says in
so many words that Anthropic was not billed.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from model_router import quota
from model_router.vendors import VENDORS, Vendor, vendor_display_name

from tests.test_model_router_server import GatewayTestBase


def _exhausted(vendor: str = "Acme", hint: str | None = None, status: int = 429):
    return quota.quota_message(
        vendor, hint, status=status, classification=quota.CLASS_EXHAUSTED,
    )


def _limited(vendor: str = "Acme", hint: str | None = None, status: int = 429):
    return quota.quota_message(
        vendor, hint, status=status, classification=quota.CLASS_RATE_LIMITED,
    )


class MessageShapeTests(unittest.TestCase):
    def test_the_message_names_the_vendor_the_remedy_and_the_bill(self) -> None:
        message = _exhausted()
        self.assertTrue(message.startswith("Acme quota exhausted"))
        self.assertIn("/model", message)
        self.assertIn("not billed to Anthropic", message)

    def test_the_rate_limited_sentence_does_not_claim_exhaustion(self) -> None:
        """A thirty-second limiter must not read as "your week is spent"."""
        message = _limited()
        self.assertIn("Acme rate-limited or out of quota", message)
        self.assertNotIn("exhausted", message)
        self.assertIn("retry shortly", message)

    def test_both_sentences_keep_the_remedy_and_the_bill(self) -> None:
        """The two facts the field incident was about are true either way."""
        for message in (_exhausted(), _limited()):
            with self.subTest(message=message):
                self.assertIn("/model", message)
                self.assertIn("not billed to Anthropic", message)

    def test_the_upstream_status_is_in_the_message(self) -> None:
        """The client always sees 429; only this says which status caused it."""
        self.assertIn("(HTTP 402)", _exhausted(status=402))
        self.assertIn("(HTTP 429)", _limited(status=429))

    def test_a_reset_hint_is_included_when_the_vendor_supplies_one(self) -> None:
        self.assertIn(
            ", resets 2026-09-15T00:00:00Z",
            _exhausted(hint="2026-09-15T00:00:00Z"),
        )

    def test_no_hint_means_no_clause_rather_than_a_guess(self) -> None:
        self.assertNotIn("resets", _exhausted())

    def test_the_envelope_is_anthropic_shaped(self) -> None:
        body = quota.quota_error_body(
            "Acme", None, status=429, classification=quota.CLASS_EXHAUSTED,
        )
        self.assertEqual(body["type"], "error")
        self.assertEqual(body["error"]["type"], "rate_limit_error")

    def test_the_classification_cannot_be_claimed_by_omission(self) -> None:
        """No default: a caller must SAY which of the two it means."""
        with self.assertRaises(TypeError):
            quota.quota_message("Acme")  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            quota.quota_error_body("Acme")  # type: ignore[call-arg]


class ClassificationTests(unittest.TestCase):
    """Which sentence, and on what evidence.

    The rule is one-directional: EXHAUSTED needs positive evidence,
    RATE_LIMITED is what "we cannot tell" says out loud.
    """

    def test_the_field_body_is_exhaustion(self) -> None:
        """The 2026-09-08 body, verbatim."""
        body = b'{"error":{"code":"1310","message":"Weekly/Monthly Limit Exhausted"}}'
        self.assertEqual(quota.classify_quota(429, body, {}), quota.CLASS_EXHAUSTED)

    def test_the_bare_vendor_code_alone_is_exhaustion(self) -> None:
        self.assertEqual(
            quota.classify_quota(429, b"[1310] Weekly/Monthly Limit Exhausted", {}),
            quota.CLASS_EXHAUSTED,
        )

    def test_the_bracketed_code_ranks_as_a_CODE_not_as_prose(self) -> None:
        """One fact, two spellings, one tier.

        ``{"code":"1310"}`` and ``[1310] …`` are the same vendor saying the
        same thing; ranking the bracketed form with the prose made the answer
        depend on whether the vendor happened to send an envelope. The
        30-second ``retry-after`` below is a pacing hint, not the reset of a
        weekly allowance, and must not outrank the code.
        """
        self.assertEqual(
            quota.classify_quota(
                429,
                b"[1310] Weekly/Monthly Limit Exhausted",
                {"retry-after": "30"},
            ),
            quota.CLASS_EXHAUSTED,
        )
        self.assertEqual(
            quota.classify_quota(
                429, b'{"error":{"code":"1310"}}', {"retry-after": "30"},
            ),
            quota.CLASS_EXHAUSTED,
            "the two spellings must reach the same verdict",
        )

    def test_a_bare_number_is_not_a_code(self) -> None:
        """``[1310]`` is bracketed on purpose: an unrelated 1310 is not it."""
        self.assertEqual(
            quota.classify_quota(
                429, b"served 1310 requests this minute", {"retry-after": "30"},
            ),
            quota.CLASS_RATE_LIMITED,
        )

    def test_the_code_alone_in_json_is_exhaustion(self) -> None:
        """A vendor that sends only its code has still said it."""
        self.assertEqual(
            quota.classify_quota(429, b'{"error":{"code":"1310"}}', {}),
            quota.CLASS_EXHAUSTED,
        )
        self.assertEqual(
            quota.classify_quota(429, b'{"error":{"code":1310}}', {}),
            quota.CLASS_EXHAUSTED,
        )

    def test_a_402_is_exhaustion_whatever_the_body_says(self) -> None:
        """Payment Required is a balance, and waiting does not clear it."""
        self.assertEqual(
            quota.classify_quota(402, b'{"error":"balance"}', {}),
            quota.CLASS_EXHAUSTED,
        )

    def test_a_short_retry_after_is_a_rate_limit(self) -> None:
        self.assertEqual(
            quota.classify_quota(429, b"{}", {"Retry-After": "30"}),
            quota.CLASS_RATE_LIMITED,
        )

    def test_the_vendors_rate_limit_code_is_not_exhaustion(self) -> None:
        """Z.ai 1302 is a per-minute limiter; only 1310 is the allowance."""
        body = b'{"error":{"code":"1302","message":"API request rate limit"}}'
        self.assertEqual(
            quota.classify_quota(429, body, {}), quota.CLASS_RATE_LIMITED,
        )

    def test_a_long_reset_is_exhaustion(self) -> None:
        """Nothing that resets in a day is a rate-limit window."""
        self.assertEqual(
            quota.classify_quota(429, b"{}", {"Retry-After": "86400"}),
            quota.CLASS_EXHAUSTED,
        )

    def test_a_distant_absolute_reset_is_exhaustion(self) -> None:
        far = (
            datetime.now(timezone.utc) + timedelta(days=3)
        ).isoformat().replace("+00:00", "Z")
        body = json.dumps({"error": {"reset_at": far}}).encode()
        self.assertEqual(quota.classify_quota(429, body, {}), quota.CLASS_EXHAUSTED)

    def test_a_near_absolute_reset_is_a_rate_limit(self) -> None:
        soon = (
            datetime.now(timezone.utc) + timedelta(seconds=45)
        ).isoformat().replace("+00:00", "Z")
        body = json.dumps({"error": {"reset_at": soon}}).encode()
        self.assertEqual(
            quota.classify_quota(429, body, {}), quota.CLASS_RATE_LIMITED,
        )

    def test_no_evidence_at_all_is_a_rate_limit_not_a_guess(self) -> None:
        """THE conservative default: absence of evidence is not evidence."""
        self.assertEqual(
            quota.classify_quota(429, b"", {}), quota.CLASS_RATE_LIMITED,
        )
        self.assertEqual(
            quota.classify_quota(429, b"<html>go away</html>", {}),
            quota.CLASS_RATE_LIMITED,
        )

    def test_an_unreadable_hint_is_no_evidence(self) -> None:
        self.assertEqual(
            quota.classify_quota(429, b"{}", {"Retry-After": "next tuesday"}),
            quota.CLASS_RATE_LIMITED,
        )

    def test_a_readable_short_window_wins_over_generic_words(self) -> None:
        """The evidence ORDER, and the case that fixed it.

        "Quota exceeded" is the vocabulary of BOTH situations, and a
        per-minute limiter says it constantly. A reset time answers the
        question directly, so it decides — including deciding RATE_LIMITED.
        """
        self.assertEqual(
            quota.classify_quota(
                429, b'{"message":"quota exceeded"}', {"Retry-After": "30"},
            ),
            quota.CLASS_RATE_LIMITED,
        )

    def test_a_per_minute_limiter_that_says_quota_exceeded(self) -> None:
        """Verbatim shape of the message that was mis-classified."""
        body = json.dumps(
            {"error": {"message": "Quota exceeded: 60 requests per minute"}}
        ).encode()
        self.assertEqual(
            quota.classify_quota(429, body, {"Retry-After": "30"}),
            quota.CLASS_RATE_LIMITED,
        )

    def test_the_words_still_decide_when_there_is_no_reset_to_read(
        self,
    ) -> None:
        """Step 4: weaker evidence, still evidence, when nothing outranks it."""
        self.assertEqual(
            quota.classify_quota(429, b'{"message":"quota exceeded"}', {}),
            quota.CLASS_EXHAUSTED,
        )

    def test_the_vendor_code_outranks_even_a_short_window(self) -> None:
        """Step 1: a CODE is the vendor classifying its own refusal.

        A weekly allowance that also carries ``Retry-After: 30`` (a retry
        pacing hint, not the reset) must still read as exhaustion.
        """
        self.assertEqual(
            quota.classify_quota(
                429, b'{"error":{"code":"1310"}}', {"Retry-After": "30"},
            ),
            quota.CLASS_EXHAUSTED,
        )

    def test_a_retry_after_header_wins_and_renders_as_seconds(self) -> None:
        self.assertEqual(
            quota.find_reset_hint(b"{}", {"Retry-After": "120"}), "in 120s",
        )

    def test_a_reset_key_is_found_anywhere_in_the_body(self) -> None:
        body = json.dumps(
            {"error": {"code": "1310", "detail": {"reset_at": "2026-09-15T00:00:00Z"}}}
        ).encode()
        self.assertEqual(quota.find_reset_hint(body, {}), "2026-09-15T00:00:00Z")

    def test_an_opaque_body_yields_no_hint(self) -> None:
        self.assertIsNone(
            quota.find_reset_hint(b"[1310] Weekly/Monthly Limit Exhausted", {}),
        )

    def test_an_epoch_reset_renders_as_a_time_not_a_delay(self) -> None:
        """1757300000 is a timestamp, not "in 1757300000 seconds"."""
        hint = quota.find_reset_hint(b'{"error":{"reset_at":1757300000}}', {})
        assert hint is not None
        self.assertTrue(hint.startswith("2025-"), hint)
        self.assertTrue(hint.endswith("Z"), hint)

    def test_a_small_number_is_still_a_delay(self) -> None:
        self.assertEqual(
            quota.find_reset_hint(b'{"retry_after":90}', {}), "in 90s",
        )

    def test_a_numeric_string_epoch_is_also_a_time(self) -> None:
        hint = quota.find_reset_hint(b"{}", {"Retry-After": "1757300000"})
        assert hint is not None
        self.assertTrue(hint.startswith("2025-"), hint)

    def test_there_is_no_sse_error_helper(self) -> None:
        """Deleted with the SSE branch: an SDK parses a non-2xx body as JSON
        whatever the request asked for, so an SSE frame here surfaces as a
        parser exception and the typed error is lost."""
        self.assertFalse(hasattr(quota, "sse_error_event"))

    def test_the_display_name_falls_back_to_the_suffix(self) -> None:
        row = Vendor(
            vendor_id="acme",
            display_suffix=" · Acme plan",
            namespace="claude-acme/",
            upstream="https://api.acme.example",
            secret_keys=("k",),
            bare_id_prefixes=(),
        )
        self.assertEqual(vendor_display_name(row), "Acme plan")
        self.assertEqual(vendor_display_name(VENDORS["zai"]), "Z.ai")


class QuotaRelayTests(GatewayTestBase):
    async def _post(self, *, stream: bool):
        return await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": [], "stream": stream},
        )

    async def test_a_vendor_429_becomes_a_named_local_429(self) -> None:
        self.vendor_up.messages_status = 429
        self.vendor_up.messages_raw = (
            b'{"error":{"code":"1310","message":"Weekly/Monthly Limit Exhausted"}}'
        )
        resp = await self._post(stream=False)
        self.assertEqual(resp.status, 429)
        body = await resp.json()
        self.assertEqual(body["error"]["type"], "rate_limit_error")
        self.assertIn("Z.ai quota exhausted", body["error"]["message"])
        self.assertIn("not billed to Anthropic", body["error"]["message"])
        self.assertNotIn("1310", body["error"]["message"])

    async def test_a_vendor_402_is_the_same_situation_and_the_same_429(self) -> None:
        self.vendor_up.messages_status = 402
        self.vendor_up.messages_raw = b'{"error":"balance"}'
        resp = await self._post(stream=False)
        self.assertEqual(resp.status, quota.CLIENT_QUOTA_STATUS)
        message = (await resp.json())["error"]["message"]
        self.assertIn("quota exhausted", message)
        self.assertIn(
            "(HTTP 402)", message,
            "the client always sees 429; only the text can name the cause",
        )

    async def test_a_rate_limit_does_not_tell_the_user_to_switch_family(
        self,
    ) -> None:
        """A per-minute limiter clears itself. Sending the user to another
        model family for thirty seconds is wrong advice, and it was being
        given to every 429 the vendor returned."""
        self.vendor_up.messages_status = 429
        self.vendor_up.messages_raw = (
            b'{"error":{"code":"1302","message":"API request rate limit"}}'
        )
        resp = await self._post(stream=False)
        self.assertEqual(resp.status, 429)
        message = (await resp.json())["error"]["message"]
        self.assertIn("Z.ai rate-limited or out of quota", message)
        self.assertNotIn("exhausted", message)
        self.assertIn("retry shortly", message)
        self.assertIn("not billed to Anthropic", message)

    async def test_the_access_line_records_which_class_was_decided(self) -> None:
        """So a "it told me to switch models" report is answerable from the
        log, which deliberately does not keep the body."""
        self.vendor_up.messages_status = 429
        self.vendor_up.messages_raw = b'{"error":{"code":"1302"}}'
        with self.assertLogs("model_router.server", level="INFO") as captured:
            await self._post(stream=False)
        self.assertTrue(
            any("quota_class=rate_limited" in line for line in captured.output),
            captured.output,
        )

    async def test_a_streaming_client_gets_json_not_sse(self) -> None:
        """The SDK parses any non-2xx body as JSON, whatever it asked for.

        An ``event: error`` frame therefore reaches ``JSON.parse`` as
        ``event: error…`` and throws: the typed ``rate_limit_error`` — the
        whole point of the substitution — is lost, and the user sees a parser
        exception instead of the sentence naming the vendor.
        """
        self.vendor_up.messages_status = 429
        self.vendor_up.messages_raw = b'{"error":{"code":"1310"}}'
        resp = await self._post(stream=True)
        self.assertEqual(resp.status, 429)
        self.assertIn("application/json", resp.headers["Content-Type"])
        self.assertNotIn("event-stream", resp.headers["Content-Type"])
        payload = await resp.json()
        self.assertEqual(payload["error"]["type"], "rate_limit_error")
        self.assertIn("Z.ai quota exhausted", payload["error"]["message"])
        self.assertNotIn("event:", json.dumps(payload))

    async def test_the_reset_hint_reaches_the_client(self) -> None:
        self.vendor_up.messages_status = 429
        self.vendor_up.messages_raw = json.dumps(
            {"error": {"reset_at": "2026-09-15T00:00:00Z"}}
        ).encode()
        resp = await self._post(stream=False)
        self.assertIn(
            "resets 2026-09-15T00:00:00Z", (await resp.json())["error"]["message"],
        )

    async def test_the_first_party_route_is_not_rewritten(self) -> None:
        """An Anthropic 429 IS an Anthropic limit — relay it verbatim."""
        self.anthropic_up.messages_status = 429
        self.anthropic_up.messages_raw = b'{"type":"error","error":{"x":1}}'
        resp = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(resp.status, 429)
        self.assertEqual(await resp.read(), self.anthropic_up.messages_raw)

    async def test_the_upstream_body_is_not_relayed_to_the_client(self) -> None:
        self.vendor_up.messages_status = 429
        self.vendor_up.messages_raw = b'{"secret_ish":"vendor internals"}'
        resp = await self._post(stream=False)
        self.assertNotIn("vendor internals", (await resp.read()).decode())

    async def test_the_upstream_body_reaches_the_log_at_debug(self) -> None:
        self.vendor_up.messages_status = 429
        self.vendor_up.messages_raw = b'{"error":{"code":"1310"}}'
        with self.assertLogs("model_router.server", level="DEBUG") as captured:
            await self._post(stream=False)
        self.assertTrue(
            any("1310" in line and "DEBUG" in line for line in captured.output),
            captured.output,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
