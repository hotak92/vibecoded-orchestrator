# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for VCThelpers.telemetry.

Covers:
    - PII scrubbing (paths, emails, tokens, IPs)
    - Consent flag gating (rl_data, routing_data, instinct_data, hardware)
    - VIBECODED_TELEMETRY=false short-circuit
    - Queue overflow eviction
    - Uploader retry logic (mocked HTTP)
    - Hardware detection does not crash on minimal systems

All tests isolate the HOME directory so nothing touches ~/.vibecoded/.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Ensure the repo root is importable.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _fresh_home() -> tempfile.TemporaryDirectory:
    """Return a TemporaryDirectory the caller should use as HOME."""
    return tempfile.TemporaryDirectory(prefix="vibecoded-test-")


def _reload_telemetry_modules():
    """Reload telemetry modules so they pick up the new HOME env."""
    import importlib
    import VCThelpers.telemetry.consent as consent_mod
    import VCThelpers.telemetry.queue as queue_mod
    import VCThelpers.telemetry.collector as collector_mod
    import VCThelpers.telemetry.uploader as uploader_mod
    import VCThelpers.telemetry.hardware as hardware_mod
    importlib.reload(consent_mod)
    importlib.reload(queue_mod)
    importlib.reload(collector_mod)
    importlib.reload(uploader_mod)
    importlib.reload(hardware_mod)
    return consent_mod, queue_mod, collector_mod, uploader_mod, hardware_mod


class PIIScrubTests(unittest.TestCase):
    def setUp(self) -> None:
        self._home_ctx = _fresh_home()
        self._home = self._home_ctx.__enter__()
        self._env_patch = mock.patch.dict(os.environ, {
            "HOME": self._home,
            "VIBECODED_TELEMETRY": "",
        }, clear=False)
        self._env_patch.start()
        _, _, self.collector_mod, _, _ = _reload_telemetry_modules()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._home_ctx.__exit__(None, None, None)

    def test_scrubs_linux_home_path(self) -> None:
        out = self.collector_mod._scrub_pii("/home/martino/Desktop/code.py")
        self.assertIn("<user>", out)
        self.assertNotIn("martino", out)

    def test_scrubs_macos_home_path(self) -> None:
        out = self.collector_mod._scrub_pii("/Users/alice/Projects/app.ts")
        self.assertIn("<user>", out)
        self.assertNotIn("alice", out)

    def test_scrubs_windows_home_path(self) -> None:
        out = self.collector_mod._scrub_pii(r"C:\Users\bob\AppData\local.json")
        self.assertIn("<user>", out)
        self.assertNotIn("bob", out)

    def test_scrubs_email(self) -> None:
        out = self.collector_mod._scrub_pii("Contact: user@example.com please")
        self.assertIn("<email>", out)
        self.assertNotIn("example.com", out)

    def test_scrubs_github_pat(self) -> None:
        out = self.collector_mod._scrub_pii("token=ghp_ABCDEFG1234567890abcdef")
        self.assertIn("<token>", out)
        self.assertNotIn("ghp_ABCDEFG", out)

    def test_scrubs_openai_key(self) -> None:
        out = self.collector_mod._scrub_pii("Bearer sk-ABCDEFG1234567890abcdef")
        self.assertIn("<token>", out)

    def test_scrubs_anthropic_key(self) -> None:
        out = self.collector_mod._scrub_pii("key=sk-ant-xyz1234567890abcdefgh")
        self.assertIn("<token>", out)

    def test_scrubs_jwt(self) -> None:
        # Realistic JWT: header.payload.signature, all base64url, 40+ char sig.
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        out = self.collector_mod._scrub_pii(f"auth={jwt}")
        self.assertIn("<token>", out)

    def test_scrubs_ipv4(self) -> None:
        out = self.collector_mod._scrub_pii("connecting to 192.168.1.42")
        self.assertIn("<ip>", out)
        self.assertNotIn("192.168.1.42", out)

    def test_scrubs_ipv6(self) -> None:
        out = self.collector_mod._scrub_pii("addr fe80::1ff:fe23:4567:890a here")
        self.assertIn("<ip>", out)

    def test_scrub_args_dict_keeps_keys(self) -> None:
        out = self.collector_mod._scrub_args({"file": "/home/martino/x.py", "mode": "r"})
        self.assertIn("file=", out)
        self.assertIn("mode=", out)
        self.assertIn("<user>", out)
        self.assertNotIn("martino", out)

    def test_scrub_args_truncates(self) -> None:
        long_val = "x" * 2000
        out = self.collector_mod._scrub_args({"blob": long_val})
        self.assertLessEqual(len(out), 500)
        self.assertTrue(out.endswith("..."))

    def test_hash_session_id_is_stable(self) -> None:
        a = self.collector_mod._hash_session_id("session-abc")
        b = self.collector_mod._hash_session_id("session-abc")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_hash_session_id_empty(self) -> None:
        self.assertEqual(self.collector_mod._hash_session_id(""), "")


class ConsentGatingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._home_ctx = _fresh_home()
        self._home = self._home_ctx.__enter__()
        # Explicit opt-in here: this class tests per-category consent gating
        # (rl_data, routing_data, instinct_data) on top of an active telemetry
        # opt-in. The default-OFF behaviour for an unset env var is asserted
        # separately in DefaultOffTests below.
        self._env_patch = mock.patch.dict(os.environ, {
            "HOME": self._home,
            "VIBECODED_TELEMETRY": "true",
        }, clear=False)
        self._env_patch.start()
        (self.consent_mod, self.queue_mod, self.collector_mod,
         _, _) = _reload_telemetry_modules()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._home_ctx.__exit__(None, None, None)

    def _write_consent(self, **flags) -> None:
        consent = {
            "consent_version": self.consent_mod.CONSENT_VERSION,
            "granted_at": "2026-01-01T00:00:00+00:00",
            "always_on": True,
            "rl_data": False,
            "routing_data": False,
            "instinct_data": False,
            "hardware": False,
        }
        consent.update(flags)
        self.consent_mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with self.consent_mod.CONFIG_FILE.open("w") as fh:
            json.dump(consent, fh)

    def test_always_on_session_start_with_explicit_optin(self) -> None:
        """With VIBECODED_TELEMETRY=true, always-on category bypasses per-flag
        consent (no consent file required) and the event is enqueued."""
        ok = self.collector_mod.collect_session_start(license_valid=True, license_tier="free")
        self.assertTrue(ok)
        q = self.queue_mod.get_queue()
        self.assertEqual(q.count_pending(), 1)

    def test_rl_blocked_without_consent(self) -> None:
        ok = self.collector_mod.collect_rl_retrieval([0.1, 0.2], [[0.3, 0.4]], [0.9], 12.0)
        self.assertFalse(ok)
        self.assertEqual(self.queue_mod.get_queue().count_pending(), 0)

    def test_rl_allowed_with_consent(self) -> None:
        self._write_consent(rl_data=True)
        ok = self.collector_mod.collect_rl_retrieval([0.1, 0.2], [[0.3, 0.4]], [0.9], 12.0)
        self.assertTrue(ok)
        self.assertEqual(self.queue_mod.get_queue().count_pending(), 1)

    def test_routing_blocked_without_consent(self) -> None:
        ok = self.collector_mod.collect_qlearning_routing(
            task_type="code", chosen_agent="coder", outcome="success",
            reward_signal=1.0, model_tier="sonnet",
        )
        self.assertFalse(ok)

    def test_routing_allowed_with_consent(self) -> None:
        self._write_consent(routing_data=True)
        ok = self.collector_mod.collect_qlearning_routing(
            task_type="code", chosen_agent="coder", outcome="success",
            reward_signal=1.0, model_tier="sonnet",
        )
        self.assertTrue(ok)

    def test_instinct_blocked_without_consent(self) -> None:
        ok = self.collector_mod.collect_instinct_event(
            tool_name="Read", args={"path": "/home/martino/x.py"}, outcome="ok",
        )
        self.assertFalse(ok)

    def test_instinct_allowed_with_consent_and_scrubs_args(self) -> None:
        self._write_consent(instinct_data=True)
        ok = self.collector_mod.collect_instinct_event(
            tool_name="Read",
            args={"path": "/home/martino/x.py", "token": "ghp_ABCDEFG1234567890abcdef"},
            outcome="ok",
            session_id="abc-123",
        )
        self.assertTrue(ok)
        rows = self.queue_mod.get_queue().recent_events(limit=5)
        self.assertEqual(len(rows), 1)
        payload = rows[0]["payload"]["payload"]
        self.assertIn("<user>", payload["args_summary"])
        self.assertIn("<token>", payload["args_summary"])
        self.assertNotIn("martino", payload["args_summary"])
        self.assertNotIn("ghp_", payload["args_summary"])
        # session_id is hashed, not raw.
        self.assertNotEqual(payload["session_id_hash"], "abc-123")
        self.assertEqual(len(payload["session_id_hash"]), 64)


class DefaultOffTests(unittest.TestCase):
    """Default-OFF policy: with no explicit VIBECODED_TELEMETRY env, telemetry
    is disabled. Mirrors the README promise and the .env default written by
    install.py. Belt-and-suspenders: collector AND uploader both default OFF.
    """

    def setUp(self) -> None:
        self._home_ctx = _fresh_home()
        self._home = self._home_ctx.__enter__()
        # Empty env (= default state) — no opt-in.
        self._env_patch = mock.patch.dict(os.environ, {
            "HOME": self._home,
            "VIBECODED_TELEMETRY": "",
        }, clear=False)
        self._env_patch.start()
        (_, self.queue_mod, self.collector_mod,
         self.uploader_mod, _) = _reload_telemetry_modules()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._home_ctx.__exit__(None, None, None)

    def test_collector_is_disabled_when_env_unset(self) -> None:
        """Even always-on events do NOT enqueue without opt-in."""
        self.assertFalse(self.collector_mod.telemetry_enabled())
        ok = self.collector_mod.collect_session_start(license_valid=True, license_tier="free")
        self.assertFalse(ok)
        self.assertEqual(self.queue_mod.get_queue().count_pending(), 0)

    def test_uploader_is_disabled_when_env_unset(self) -> None:
        """Defense in depth: even if rows somehow exist in the queue, the
        uploader refuses to ship them without an explicit opt-in."""
        self.assertTrue(self.uploader_mod._disabled())
        # Inject a row directly bypassing the collector gate.
        self.queue_mod.get_queue().enqueue("forced", {"x": 1})
        result = self.uploader_mod.upload_pending()
        self.assertEqual(result.uploaded_count, 0)
        self.assertEqual(result.error, "telemetry_disabled")

    def test_optin_truthy_values_enable(self) -> None:
        for v in ("true", "1", "yes", "on", "TRUE", "On"):
            with mock.patch.dict(os.environ, {"VIBECODED_TELEMETRY": v}):
                self.assertTrue(self.collector_mod.telemetry_enabled())
                self.assertFalse(self.uploader_mod._disabled())

    def test_optout_falsy_values_disable(self) -> None:
        for v in ("false", "0", "no", "off", "FALSE", "Off", ""):
            with mock.patch.dict(os.environ, {"VIBECODED_TELEMETRY": v}):
                self.assertFalse(self.collector_mod.telemetry_enabled())
                self.assertTrue(self.uploader_mod._disabled())

    def test_runtime_toggle_on_to_off_stops_events(self) -> None:
        """User-facing guarantee: flipping the master switch ON→OFF means
        no further events are collected. Mirrors `vct-cli telemetry off`
        flipping the env var and the launcher's Settings → Privacy
        master toggle.
        """
        # Start opted-in (env=true). Always-on events flow.
        with mock.patch.dict(os.environ, {"VIBECODED_TELEMETRY": "true"}):
            self.assertTrue(self.collector_mod.telemetry_enabled())
            ok = self.collector_mod.collect_session_start(
                license_valid=True, license_tier="free"
            )
            self.assertTrue(ok)
            count_after_on = self.queue_mod.get_queue().count_pending()
            self.assertGreaterEqual(count_after_on, 1)

        # Flip the master switch off (= what vct-cli telemetry off does).
        with mock.patch.dict(os.environ, {"VIBECODED_TELEMETRY": "false"}):
            self.assertFalse(self.collector_mod.telemetry_enabled())
            ok2 = self.collector_mod.collect_session_start(
                license_valid=True, license_tier="free"
            )
            self.assertFalse(ok2)
            # Queue size unchanged — no new event added after the toggle.
            count_after_off = self.queue_mod.get_queue().count_pending()
            self.assertEqual(count_after_off, count_after_on)
            # And the uploader will refuse to ship anything that's already
            # queued, so the user's choice to opt out is respected
            # immediately.
            self.assertTrue(self.uploader_mod._disabled())


class EnvOptOutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._home_ctx = _fresh_home()
        self._home = self._home_ctx.__enter__()

    def tearDown(self) -> None:
        self._home_ctx.__exit__(None, None, None)

    def test_false_shortcircuits_all_collection(self) -> None:
        with mock.patch.dict(os.environ, {
            "HOME": self._home,
            "VIBECODED_TELEMETRY": "false",
        }, clear=False):
            _, queue_mod, collector_mod, _, _ = _reload_telemetry_modules()
            # Also enable every consent flag to prove env overrides consent.
            consent_file = Path(self._home) / ".vibecoded" / "config.json"
            consent_file.parent.mkdir(parents=True, exist_ok=True)
            consent_file.write_text(json.dumps({
                "consent_version": "1.0",
                "always_on": True,
                "rl_data": True,
                "routing_data": True,
                "instinct_data": True,
                "hardware": True,
            }))

            self.assertFalse(collector_mod.collect_session_start(license_valid=True))
            self.assertFalse(collector_mod.collect_rl_retrieval([0.1], [[0.2]], [0.5], 1.0))
            self.assertFalse(collector_mod.collect_qlearning_routing(
                task_type="x", chosen_agent="a", outcome="ok",
                reward_signal=0.0, model_tier="haiku",
            ))
            self.assertFalse(collector_mod.collect_instinct_event(
                tool_name="Read", args={}, outcome="ok",
            ))
            self.assertEqual(queue_mod.get_queue().count_pending(), 0)


class QueueOverflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._home_ctx = _fresh_home()
        self._home = self._home_ctx.__enter__()
        self._env_patch = mock.patch.dict(os.environ, {
            "HOME": self._home,
            "VIBECODED_TELEMETRY": "",
        }, clear=False)
        self._env_patch.start()
        _, self.queue_mod, _, _, _ = _reload_telemetry_modules()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._home_ctx.__exit__(None, None, None)

    def test_overflow_drops_oldest_pending(self) -> None:
        db_path = Path(self._home) / ".vibecoded" / "telemetry_small.db"
        q = self.queue_mod.TelemetryQueue(db_path=db_path, max_size=5)
        for i in range(10):
            q.enqueue("test_event", {"i": i})
        total = q.count_total()
        self.assertLessEqual(total, 5)
        pending = q.pending_events(limit=10)
        # Oldest indices are dropped, so we should have the newest 5.
        retained = sorted(p["payload"]["i"] for p in pending)
        self.assertEqual(retained, [5, 6, 7, 8, 9])

    def test_overflow_prefers_uploaded_eviction(self) -> None:
        db_path = Path(self._home) / ".vibecoded" / "telemetry_mix.db"
        q = self.queue_mod.TelemetryQueue(db_path=db_path, max_size=5)
        # Fill and upload 3.
        for i in range(3):
            q.enqueue("test_event", {"i": i})
        early_ids = [p["id"] for p in q.pending_events(limit=3)]
        q.mark_uploaded(early_ids)
        # Add 5 more pending → should evict uploaded ones first.
        for i in range(3, 8):
            q.enqueue("test_event", {"i": i})
        # Only 5 pending survive (the new ones).
        pending = q.pending_events(limit=10)
        pending_i = sorted(p["payload"]["i"] for p in pending)
        self.assertEqual(pending_i, [3, 4, 5, 6, 7])

    def test_mark_uploaded_and_cleanup_old(self) -> None:
        db_path = Path(self._home) / ".vibecoded" / "cleanup.db"
        q = self.queue_mod.TelemetryQueue(db_path=db_path)
        for i in range(3):
            q.enqueue("test_event", {"i": i})
        ids = [p["id"] for p in q.pending_events()]
        marked = q.mark_uploaded(ids)
        self.assertEqual(marked, 3)
        self.assertEqual(q.count_pending(), 0)
        # Cleanup with retention=0 should remove them all.
        removed = q.cleanup_old(retention_seconds=0)
        self.assertEqual(removed, 3)
        self.assertEqual(q.count_total(), 0)


class UploaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._home_ctx = _fresh_home()
        self._home = self._home_ctx.__enter__()
        # Explicit opt-in: this class tests the upload mechanics (retries,
        # backoff, success/failure marking) — not the default-OFF gate
        # (covered separately in DefaultOffTests).
        self._env_patch = mock.patch.dict(os.environ, {
            "HOME": self._home,
            "VIBECODED_TELEMETRY": "true",
            "VIBECODED_TELEMETRY_URL": "https://stub.example/telemetry",
        }, clear=False)
        self._env_patch.start()
        _, self.queue_mod, self.collector_mod, self.uploader_mod, _ = _reload_telemetry_modules()
        self.queue = self.queue_mod.get_queue()
        # Seed an event.
        self.collector_mod.collect_session_start(license_valid=True, license_tier="free")

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._home_ctx.__exit__(None, None, None)

    def _mock_post(self, responses):
        """Return a stub _post_json that yields from `responses`."""
        it = iter(responses)

        def stub(url, body, timeout):
            try:
                return next(it)
            except StopIteration:
                return None, None, None
        return stub

    def test_success_200_marks_uploaded(self) -> None:
        self.assertEqual(self.queue.count_pending(), 1)
        with mock.patch.object(
            self.uploader_mod, "_post_json",
            side_effect=self._mock_post([(200, b'{"ok":true}', {})]),
        ):
            result = self.uploader_mod.upload_pending()
        self.assertIsNone(result.error)
        self.assertEqual(result.uploaded_count, 1)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.queue.count_pending(), 0)

    def test_retries_on_5xx_then_succeeds(self) -> None:
        responses = [
            (500, b"", {}),
            (503, b"", {}),
            (200, b'{"ok":true}', {}),
        ]
        with mock.patch.object(self.uploader_mod, "_post_json",
                               side_effect=self._mock_post(responses)), \
             mock.patch.object(self.uploader_mod.time, "sleep"):
            result = self.uploader_mod.upload_pending()
        self.assertEqual(result.uploaded_count, 1)
        self.assertEqual(result.attempts, 3)

    def test_gives_up_after_max_retries_network(self) -> None:
        responses = [(None, None, None)] * 10  # always fails
        with mock.patch.object(self.uploader_mod, "_post_json",
                               side_effect=self._mock_post(responses)), \
             mock.patch.object(self.uploader_mod.time, "sleep"):
            result = self.uploader_mod.upload_pending()
        self.assertEqual(result.uploaded_count, 0)
        self.assertTrue(result.retryable)
        self.assertEqual(result.attempts, 4)  # 1 initial + 3 retries
        # Event still in queue.
        self.assertEqual(self.queue.count_pending(), 1)

    def test_4xx_is_not_retryable(self) -> None:
        responses = [(400, b'{"error":"bad schema"}', {})]
        with mock.patch.object(self.uploader_mod, "_post_json",
                               side_effect=self._mock_post(responses)), \
             mock.patch.object(self.uploader_mod.time, "sleep"):
            result = self.uploader_mod.upload_pending()
        self.assertFalse(result.retryable)
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.attempts, 1)
        # Event still in queue for user inspection.
        self.assertEqual(self.queue.count_pending(), 1)

    def test_429_honors_retry_after(self) -> None:
        responses = [
            (429, b"", {"Retry-After": "2"}),
            (200, b'{"ok":true}', {}),
        ]
        sleep_calls = []

        def fake_sleep(sec):
            sleep_calls.append(sec)

        with mock.patch.object(self.uploader_mod, "_post_json",
                               side_effect=self._mock_post(responses)), \
             mock.patch.object(self.uploader_mod.time, "sleep", side_effect=fake_sleep):
            result = self.uploader_mod.upload_pending()
        self.assertEqual(result.uploaded_count, 1)
        # The first sleep was the Retry-After value (2s), not the default backoff.
        self.assertEqual(sleep_calls[0], 2.0)

    def test_disabled_env_returns_immediately(self) -> None:
        with mock.patch.dict(os.environ, {"VIBECODED_TELEMETRY": "false"}):
            result = self.uploader_mod.upload_pending()
        self.assertEqual(result.uploaded_count, 0)
        self.assertEqual(result.error, "telemetry_disabled")
        # Event remains queued.
        self.assertEqual(self.queue.count_pending(), 1)

    def test_empty_queue_no_error(self) -> None:
        self.queue.clear()
        result = self.uploader_mod.upload_pending()
        self.assertEqual(result.uploaded_count, 0)
        self.assertIsNone(result.error)


class PreLaunchStubDiversionTests(unittest.TestCase):
    """Reviewer A round-2: telemetry endpoint is unreleased; opted-in users
    must not silently 404 forever. Verify that when the resolved URL is
    DEFAULT_URL (the pre-launch stub), events are diverted to
    ~/.vibecoded/telemetry_pending.jsonl and removed from the live queue."""

    def setUp(self) -> None:
        self._home_ctx = _fresh_home()
        self._home = self._home_ctx.__enter__()
        self._env_patch = mock.patch.dict(os.environ, {
            "HOME": self._home,
            "VIBECODED_TELEMETRY": "true",
            # Critically: do NOT set VIBECODED_TELEMETRY_URL — we want the
            # default stub URL to be in effect for this test.
            "VIBECODED_TELEMETRY_URL": "",
        }, clear=False)
        self._env_patch.start()
        _, self.queue_mod, self.collector_mod, self.uploader_mod, _ = _reload_telemetry_modules()
        self.queue = self.queue_mod.get_queue()
        self.collector_mod.collect_session_start(license_valid=True, license_tier="free")

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._home_ctx.__exit__(None, None, None)

    def test_default_url_diverts_to_pending_jsonl(self) -> None:
        # Sanity: the resolved URL is the default.
        self.assertEqual(
            self.uploader_mod._resolve_endpoint(None),
            self.uploader_mod.DEFAULT_URL,
        )
        self.assertTrue(self.uploader_mod._is_pre_launch_stub_endpoint(
            self.uploader_mod.DEFAULT_URL))
        self.assertEqual(self.queue.count_pending(), 1)

        # Mock _post_json so any leak to the network would be visible —
        # the diversion path must NOT call it.
        with mock.patch.object(
            self.uploader_mod, "_post_json",
            side_effect=AssertionError("must not POST when endpoint is default stub"),
        ):
            result = self.uploader_mod.upload_pending()

        self.assertEqual(result.uploaded_count, 1)
        self.assertEqual(result.error, "endpoint_pending_deployment")
        # Event removed from live queue (so we don't infinite-retry).
        self.assertEqual(self.queue.count_pending(), 0)
        # Pending file contains exactly 1 JSON line.
        pending = Path(self._home) / ".vibecoded" / "telemetry_pending.jsonl"
        self.assertTrue(pending.exists(), "pending jsonl must be written")
        lines = pending.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        # And it's parseable JSON.
        ev = json.loads(lines[0])
        self.assertIn("event_type", ev)

    def test_explicit_url_does_not_divert(self) -> None:
        """Setting VIBECODED_TELEMETRY_URL to a real value bypasses the
        pre-launch diversion — operator has explicitly accepted live POSTs."""
        with mock.patch.dict(
            os.environ,
            {"VIBECODED_TELEMETRY_URL": "https://real.example/telemetry"},
        ):
            with mock.patch.object(
                self.uploader_mod, "_post_json",
                return_value=(200, b'{"ok":true}', {}),
            ) as posted:
                self.uploader_mod.upload_pending()
            self.assertTrue(posted.called, "explicit URL must POST normally")
        # Pending file NOT created.
        pending = Path(self._home) / ".vibecoded" / "telemetry_pending.jsonl"
        self.assertFalse(pending.exists(),
                         "pending file must not be created on explicit URL")


class HardwareDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._home_ctx = _fresh_home()
        self._home = self._home_ctx.__enter__()
        self._env_patch = mock.patch.dict(os.environ, {
            "HOME": self._home,
            "VIBECODED_TELEMETRY": "",
        }, clear=False)
        self._env_patch.start()
        _, _, _, _, self.hardware_mod = _reload_telemetry_modules()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._home_ctx.__exit__(None, None, None)

    def test_detect_hardware_never_crashes_without_tools(self) -> None:
        # Simulate minimal system: all subprocess calls fail.
        with mock.patch.object(self.hardware_mod, "_run", return_value=None):
            data = self.hardware_mod.detect_hardware(use_cache=False)
        self.assertIsInstance(data, dict)
        self.assertIn("cpu", data)
        self.assertIn("ram", data)
        self.assertIn("gpus", data)
        self.assertIsInstance(data["gpus"], list)

    def test_cache_returns_within_window(self) -> None:
        # First call writes cache.
        with mock.patch.object(self.hardware_mod, "_run", return_value=None):
            a = self.hardware_mod.detect_hardware(use_cache=False)
        # Second call should return the cached copy, not re-probe.
        called = {"n": 0}

        def boom(*args, **kwargs):
            called["n"] += 1
            return None
        with mock.patch.object(self.hardware_mod, "_detect_cpu", side_effect=boom), \
             mock.patch.object(self.hardware_mod, "_detect_ram", side_effect=boom), \
             mock.patch.object(self.hardware_mod, "_detect_gpus", side_effect=boom):
            b = self.hardware_mod.detect_hardware(use_cache=True)
        self.assertEqual(called["n"], 0)  # no re-probe
        self.assertEqual(a["_cached_at"], b["_cached_at"])

    def test_nvidia_gpu_parsed(self) -> None:
        def fake_run(cmd, timeout=2.0):
            if cmd and cmd[0] == "nvidia-smi":
                return "NVIDIA GeForce RTX 4090, 24564 MiB\n"
            return None
        with mock.patch.object(self.hardware_mod, "_run", side_effect=fake_run):
            gpus = self.hardware_mod._detect_gpus()
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["vendor"], "nvidia")
        self.assertIn("RTX 4090", gpus[0]["name"])


class ConsentFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._home_ctx = _fresh_home()
        self._home = self._home_ctx.__enter__()
        self._env_patch = mock.patch.dict(os.environ, {
            "HOME": self._home,
            "VIBECODED_TELEMETRY": "",
        }, clear=False)
        self._env_patch.start()
        (self.consent_mod, _, _, _, _) = _reload_telemetry_modules()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._home_ctx.__exit__(None, None, None)

    def test_non_interactive_stdin_defaults_to_always_on_only(self) -> None:
        with mock.patch.object(self.consent_mod, "_stdin_is_interactive", return_value=False):
            consent = self.consent_mod.prompt_consent_if_needed()
        self.assertTrue(consent["always_on"])
        self.assertFalse(consent["rl_data"])
        self.assertFalse(consent["routing_data"])
        self.assertFalse(consent["instinct_data"])
        self.assertFalse(consent["hardware"])
        self.assertTrue(self.consent_mod.CONFIG_FILE.exists())

    def test_env_false_writes_always_on_only(self) -> None:
        with mock.patch.dict(os.environ, {"VIBECODED_TELEMETRY": "false"}):
            consent = self.consent_mod.prompt_consent_if_needed()
        self.assertFalse(consent["rl_data"])
        self.assertFalse(consent["hardware"])

    def test_existing_consent_not_reprompted(self) -> None:
        # Pre-write consent with rl_data=True to confirm it's preserved.
        self.consent_mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with self.consent_mod.CONFIG_FILE.open("w") as fh:
            json.dump({
                "consent_version": self.consent_mod.CONSENT_VERSION,
                "granted_at": "2026-01-01T00:00:00+00:00",
                "always_on": True,
                "rl_data": True,
                "routing_data": False,
                "instinct_data": False,
                "hardware": False,
            }, fh)
        # Stdin is_interactive would normally trigger prompt; should skip.
        with mock.patch.object(self.consent_mod, "_stdin_is_interactive", return_value=True):
            consent = self.consent_mod.prompt_consent_if_needed()
        self.assertTrue(consent["rl_data"])

    def test_interactive_accept(self) -> None:
        with mock.patch.object(self.consent_mod, "_stdin_is_interactive", return_value=True), \
             mock.patch.object(self.consent_mod.sys.stdin, "readline", return_value="y\n"), \
             mock.patch("builtins.print"):
            consent = self.consent_mod.prompt_consent_if_needed(force=True)
        self.assertTrue(consent["rl_data"])
        self.assertTrue(consent["hardware"])

    def test_interactive_deny(self) -> None:
        with mock.patch.object(self.consent_mod, "_stdin_is_interactive", return_value=True), \
             mock.patch.object(self.consent_mod.sys.stdin, "readline", return_value="n\n"), \
             mock.patch("builtins.print"):
            consent = self.consent_mod.prompt_consent_if_needed(force=True)
        self.assertFalse(consent["rl_data"])
        self.assertFalse(consent["hardware"])


if __name__ == "__main__":
    unittest.main()
