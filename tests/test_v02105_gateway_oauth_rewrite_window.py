# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — a briefly EMPTY Claude login token is not a logout.

Live incident 2026-10-28 00:07:06-00:10:26: every first-party request answered
401 ``claude_login_unavailable``. A Claude CLI process had rewritten
``~/.claude/.credentials.json`` with an EMPTY ``accessToken`` (the "has no
access token" branch of :class:`model_router.auth.OAuthReader`), and three
minutes later a CLI wrote a fresh token. Nothing was wrong with the login.

Three promises are pinned here, and each has an act side and a leave-alone
side:

* a re-parse that finds no token, while the last token this reader PARSED is
  still unexpired, keeps serving it — and says so ONCE, without a token value;
* an EXPIRED last-known-good token is NOT served (the serve-stale rule, same
  as the vendor key resolver's);
* a machine that never had a login keeps the original "run ``claude`` to log
  in" message — the transient wording must never become the advice a
  genuinely logged-out user gets;
* one bounded re-read (:meth:`OAuthReader.read_with_brief_wait`) survives a
  rewrite that lands a few hundred milliseconds after the request.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.common.env import EnvIsolationMixin
from tests.test_model_router_server import FAKE_OAUTH, GatewayTestBase

from model_router import auth

FAKE_REFRESHED = "v02105-oauth-refreshed-synthetic-not-a-real-token"


class _TmpCase(EnvIsolationMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="v02105-oauth-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / ".credentials.json"

    def _write(self, token: str | None, *, expires_in_ms: int = 3_600_000) -> None:
        section: dict[str, object] = {}
        if token is not None:
            section["accessToken"] = token
        section["expiresAt"] = int(time.time() * 1000) + expires_in_ms
        self.path.write_text(json.dumps({"claudeAiOauth": section}), encoding="utf-8")

    def _write_raw(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")


class LastKnownGoodTests(_TmpCase):
    def test_absent_after_present_serves_the_unexpired_last_good(self) -> None:
        """The incident, replayed: token, then an empty token, same file."""
        self._write(FAKE_OAUTH)
        reader = auth.OAuthReader(self.path)
        self.assertEqual(reader.read().token, FAKE_OAUTH)

        self._write(None)  # the CLI's rewrite, with the token field blank
        state = reader.read()
        self.assertEqual(state.token, FAKE_OAUTH)
        self.assertTrue(state.from_last_good)
        self.assertEqual(state.state, "present")
        self.assertIsNone(state.problem)

    def test_unreadable_after_present_serves_the_last_good(self) -> None:
        self._write(FAKE_OAUTH)
        reader = auth.OAuthReader(self.path)
        reader.read()
        self._write_raw("{not json")  # caught mid-write
        self.assertEqual(reader.read().token, FAKE_OAUTH)

    def test_the_transition_is_logged_once_and_carries_no_token(self) -> None:
        self._write(FAKE_OAUTH)
        reader = auth.OAuthReader(self.path)
        reader.read()
        self._write(None)
        with self.assertLogs("model_router.auth", level="INFO") as logs:
            reader.read()
            reader.read()
            reader.read()
        lines = [line for line in logs.output if "last-known-good" in line]
        self.assertEqual(len(lines), 1, logs.output)
        self.assertNotIn(FAKE_OAUTH, "\n".join(logs.output))

    def test_a_fresh_token_ends_the_last_good_service(self) -> None:
        self._write(FAKE_OAUTH)
        reader = auth.OAuthReader(self.path)
        reader.read()
        self._write(None)
        self.assertTrue(reader.read().from_last_good)
        self._write(FAKE_REFRESHED)
        state = reader.read()
        self.assertEqual(state.token, FAKE_REFRESHED)
        self.assertFalse(state.from_last_good)

    def test_an_expired_last_good_is_never_served(self) -> None:
        """Past the token's own expiry there is nothing to serve: the answer
        is the ordinary 401, not a token the upstream would refuse anyway."""
        self._write(FAKE_OAUTH, expires_in_ms=1_000)
        reader = auth.OAuthReader(self.path)
        self.assertEqual(reader.read().token, FAKE_OAUTH)
        self._write(None, expires_in_ms=1_000)
        now_ms = int(time.time() * 1000)
        state = reader.read(now_ms=now_ms + 10 * 60 * 1000)
        self.assertIsNone(state.token)
        self.assertEqual(state.state, "absent")

    def test_a_deleted_file_is_still_absent(self) -> None:
        """The last-good store covers a REWRITE of the file, not its removal:
        a deleted credentials file stays an absent login (a logout must not be
        silently undone). The bounded re-read covers the write window."""
        self._write(FAKE_OAUTH)
        reader = auth.OAuthReader(self.path)
        reader.read()
        self.path.unlink()
        self.assertIsNone(reader.read().token)


class TransientWordingTests(_TmpCase):
    def test_a_future_expiry_with_a_blank_token_is_transient_on_a_cold_read(self) -> None:
        """A gateway that started DURING a rewrite has no last-good token; the
        file's own unexpired ``expiresAt`` is what still proves the login
        exists."""
        self._write(None)
        state = auth.OAuthReader(self.path).read()
        self.assertIsNone(state.token)
        self.assertTrue(state.retry_soon)
        self.assertIn("TRANSIENT", state.problem or "")
        self.assertNotIn("`claude`", state.problem or "")

    def test_never_present_keeps_the_original_login_message(self) -> None:
        """No expiry, no token, nothing ever seen: this machine has no login,
        and "retry shortly" would leave the user waiting forever."""
        self._write_raw(json.dumps({"claudeAiOauth": {}}))
        state = auth.OAuthReader(self.path).read()
        self.assertIsNone(state.token)
        self.assertFalse(state.retry_soon)
        self.assertIn("`claude`", state.problem or "")

    def test_a_blank_file_with_no_section_keeps_the_original_message(self) -> None:
        self._write_raw("{}")
        state = auth.OAuthReader(self.path).read()
        self.assertFalse(state.retry_soon)
        self.assertIn("`claude`", state.problem or "")

    def test_the_transient_spelling_still_names_what_was_wrong(self) -> None:
        """``/health`` readers and logs keep the machine-readable state; the
        prose only gains the "this is transient" half."""
        self._write(None)
        state = auth.OAuthReader(self.path).read()
        self.assertEqual(state.state, "absent")
        self.assertIn("no access token", state.problem or "")

    def test_a_good_login_is_never_rewritten_as_transient(self) -> None:
        self._write(FAKE_OAUTH)
        state = auth.OAuthReader(self.path).read()
        self.assertFalse(state.retry_soon)
        self.assertIsNone(state.problem)


class BoundedWaitTests(_TmpCase):
    def test_a_rewrite_landing_just_after_the_read_is_picked_up(self) -> None:
        self._write(None)
        reader = auth.OAuthReader(self.path)
        timer = threading.Timer(0.2, self._write, args=(FAKE_REFRESHED,))
        timer.start()
        self.addCleanup(timer.cancel)
        state = reader.read_with_brief_wait()
        self.assertEqual(state.token, FAKE_REFRESHED)

    def test_a_present_token_does_not_wait_at_all(self) -> None:
        self._write(FAKE_OAUTH)
        reader = auth.OAuthReader(self.path)
        started = time.monotonic()
        self.assertEqual(reader.read_with_brief_wait().token, FAKE_OAUTH)
        self.assertLess(time.monotonic() - started, 0.3)

    def test_a_file_that_never_comes_back_gives_up_at_the_budget(self) -> None:
        self._write(None)
        reader = auth.OAuthReader(self.path)
        with mock.patch.object(auth, "_REREAD_BUDGET_S", 0.3), \
                mock.patch.object(auth, "_REREAD_INTERVAL_S", 0.1):
            started = time.monotonic()
            state = reader.read_with_brief_wait()
            elapsed = time.monotonic() - started
        self.assertIsNone(state.token)
        self.assertTrue(state.retry_soon)
        self.assertLess(elapsed, 2.0, "the wait must stay bounded")


class ConcurrentReadersTests(_TmpCase):
    """Fix round 1 (H1): the request path reads OFF the loop
    (:meth:`OAuthReader.aread_with_brief_wait`) while ``/health`` and the
    other routes read ON it. A reader that arrives while another is mid-parse
    must get an answer, never an internal error (a 500 is a chat failure)."""

    def test_a_read_that_interleaves_a_parse_gets_the_token(self) -> None:
        """The reviewer's repro: a second ``read()`` runs while the first is
        inside the file read (the point where the GIL is released)."""
        self._write(FAKE_OAUTH)
        reader = auth.OAuthReader(self.path)
        original = Path.read_text
        inner: dict[str, object] = {}

        def interleaving_read_text(path_self, *args, **kwargs):
            Path.read_text = original  # type: ignore[method-assign]
            try:
                inner["state"] = reader.read()
            except BaseException as exc:  # noqa: BLE001 — the defect under test
                inner["error"] = exc
            return original(path_self, *args, **kwargs)

        with mock.patch.object(Path, "read_text", interleaving_read_text):
            outer = reader.read()
        self.assertNotIn("error", inner, inner.get("error"))
        self.assertEqual(getattr(inner["state"], "token", None), FAKE_OAUTH)
        self.assertEqual(outer.token, FAKE_OAUTH)

    def test_a_reader_on_another_thread_waits_for_the_parse(self) -> None:
        self._write(FAKE_OAUTH)
        reader = auth.OAuthReader(self.path)
        original = Path.read_text
        entered = threading.Event()
        release = threading.Event()
        parsing_thread: dict[str, int] = {}

        def slow_read_text(path_self, *args, **kwargs):
            if threading.get_ident() == parsing_thread.get("id"):
                entered.set()
                release.wait(5)
            return original(path_self, *args, **kwargs)

        results: dict[str, object] = {}

        def run(name: str) -> None:
            if name == "parser":
                parsing_thread["id"] = threading.get_ident()
            try:
                results[name] = reader.read()
            except BaseException as exc:  # noqa: BLE001 — the defect under test
                results[name] = exc

        with mock.patch.object(Path, "read_text", slow_read_text):
            parser = threading.Thread(target=run, args=("parser",))
            parser.start()
            self.assertTrue(entered.wait(5), "the parse never started")
            other = threading.Thread(target=run, args=("loop",))
            other.start()
            other.join(0.3)
            release.set()
            parser.join(5)
            other.join(5)
        for name in ("parser", "loop"):
            got = results.get(name)
            self.assertIsInstance(got, auth.OAuthState, f"{name}: {got!r}")
            self.assertEqual(getattr(got, "token", None), FAKE_OAUTH, name)


class LastGoodIsBoundedTests(_TmpCase):
    """Fix round 1 (H3): the last-known-good store bridges a REWRITE window,
    never a logout. Act side: a blank token minutes after a good read is
    still served (``LastKnownGoodTests``). Leave-alone sides below."""

    def _seen_then(self, now_ms: int) -> auth.OAuthReader:
        self._write(FAKE_OAUTH, expires_in_ms=8 * 3_600_000)
        reader = auth.OAuthReader(self.path)
        self.assertEqual(reader.read(now_ms=now_ms).token, FAKE_OAUTH)
        return reader

    def test_a_removed_oauth_section_is_a_logout_not_a_rewrite(self) -> None:
        now = int(time.time() * 1000)
        reader = self._seen_then(now)
        self._write_raw(json.dumps({"other": 1}))  # what a logout leaves
        state = reader.read(now_ms=now + 1_000)
        self.assertIsNone(state.token)
        self.assertFalse(state.from_last_good)
        self.assertFalse(state.retry_soon)
        self.assertIn("`claude`", state.problem or "")

    def test_a_blank_token_long_after_the_last_good_read_is_not_served(self) -> None:
        now = int(time.time() * 1000)
        reader = self._seen_then(now)
        self._write(None, expires_in_ms=8 * 3_600_000)
        state = reader.read(now_ms=now + 3 * 3_600_000)
        self.assertIsNone(state.token)
        self.assertFalse(state.from_last_good)

    def test_the_bound_is_the_recent_login_window(self) -> None:
        now = int(time.time() * 1000)
        reader = self._seen_then(now)
        self._write(None, expires_in_ms=8 * 3_600_000)
        inside = reader.read(now_ms=now + auth._RECENT_LOGIN_MS)
        self.assertEqual(inside.token, FAKE_OAUTH)
        outside = reader.read(now_ms=now + auth._RECENT_LOGIN_MS + 1)
        self.assertIsNone(outside.token)

    def test_half_written_json_long_after_is_not_served(self) -> None:
        now = int(time.time() * 1000)
        reader = self._seen_then(now)
        self._write_raw("{not json")
        state = reader.read(now_ms=now + 6 * 60 * 1000)
        self.assertIsNone(state.token)
        self.assertFalse(state.retry_soon)
        self.assertIn("`claude`", state.problem or "")

    @unittest.skipIf(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        "needs POSIX permissions enforced on this user",
    )
    def test_a_permission_refusal_is_never_served_from_the_store(self) -> None:
        now = int(time.time() * 1000)
        reader = self._seen_then(now)
        self.path.chmod(0)
        self.addCleanup(self.path.chmod, 0o600)
        state = reader.read(now_ms=now + 1_000)
        self.assertIsNone(state.token)
        self.assertEqual(state.state, "unreadable")
        self.assertFalse(state.retry_soon)
        self.assertIn("`claude`", state.problem or "")


class ExpiredIsNotTransientTests(_TmpCase):
    """Fix round 1 (M1): a token that lapsed on its own is not a rewrite."""

    def test_an_expired_token_seen_minutes_ago_gets_the_login_message(self) -> None:
        now = int(time.time() * 1000)
        self._write(FAKE_OAUTH, expires_in_ms=1_000)
        reader = auth.OAuthReader(self.path)
        self.assertEqual(reader.read(now_ms=now).token, FAKE_OAUTH)
        state = reader.read(now_ms=now + 60_000)
        self.assertEqual(state.state, "expired")
        self.assertFalse(state.retry_soon)
        self.assertIn("`claude`", state.problem or "")
        self.assertNotIn("TRANSIENT", state.problem or "")

    def test_an_expired_token_does_not_take_the_bounded_wait(self) -> None:
        now = int(time.time() * 1000)
        self._write(FAKE_OAUTH, expires_in_ms=1_000)
        reader = auth.OAuthReader(self.path)
        reader.read(now_ms=now)
        started = time.monotonic()
        state = reader.read_with_brief_wait(now_ms=now + 60_000)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(state.state, "expired")

    def test_a_blank_token_minutes_after_a_good_read_stays_transient(self) -> None:
        """Leave-alone: the rewrite shape keeps its transient verdict."""
        now = int(time.time() * 1000)
        self._write(FAKE_OAUTH, expires_in_ms=1_000)
        reader = auth.OAuthReader(self.path)
        reader.read(now_ms=now)
        self._write_raw(json.dumps({"claudeAiOauth": {"accessToken": ""}}))
        state = reader.read(now_ms=now + 60_000)
        self.assertTrue(state.retry_soon)
        self.assertIn("TRANSIENT", state.problem or "")


class GatewaySurvivesTheRewriteWindowTests(GatewayTestBase):
    """End to end: the token the file carried a moment ago is the token the
    upstream receives."""

    async def test_a_request_after_the_rewrite_still_reaches_the_upstream(self) -> None:
        first = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(first.status, 200)
        # The CLI's rewrite: same file, empty token, login not expired.
        self.write_credentials(None, expires_in_ms=3_600_000)
        second = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(second.status, 200)
        forwarded = self.anthropic_up.message_requests[-1]["headers"]
        self.assertEqual(forwarded.get("Authorization"), f"Bearer {FAKE_OAUTH}")

    async def test_a_machine_with_no_login_gets_the_login_message(self) -> None:
        self.credentials.write_text(
            json.dumps({"claudeAiOauth": {}}), encoding="utf-8",
        )
        client = await self.make_client()  # a COLD reader: nothing was parsed
        resp = await client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-opus-5", "messages": []},
        )
        self.assertEqual(resp.status, 401)
        body = await resp.json()
        self.assertIn("no access token", json.dumps(body))
        self.assertIn("`claude`", json.dumps(body))

    async def test_health_never_takes_the_bounded_wait(self) -> None:
        """A liveness probe must answer DURING a rewrite: the waiting re-read
        is opt-in on the request path and must not leak into ``/health``.
        Proved by making the waiting variant explode — not by reading it."""
        boom = mock.Mock(side_effect=AssertionError("/health waited on a rewrite"))
        with mock.patch.object(auth.OAuthReader, "aread_with_brief_wait", boom), \
                mock.patch.object(auth.OAuthReader, "read_with_brief_wait", boom):
            self.write_credentials(None, expires_in_ms=3_600_000)
            resp = await self.client.get("/health")
        self.assertEqual(resp.status, 200)
        payload = await resp.json()
        self.assertFalse(payload["oauth_present"])
        self.assertEqual(payload["oauth_state"], "absent")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()