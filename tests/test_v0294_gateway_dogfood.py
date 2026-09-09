# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Prove the gateway before pointing a panel at it.

Every gateway defect of this cycle reached a user because "started" and
"pointed" were reported from the gateway ANSWERING, never from it answering
CORRECTLY: a multi-MiB request came back 413, a stream was cut at 601 s, a repair
bug would have become a 500 the SDK retries ten times. One real request,
compared against the same request to api.anthropic.com, would have caught the
first of those before the switch said "done".

So `dogfood_gateway` sends that request both ways and REFUSES to point when
the answers differ. These tests drive it against two loopback stubs — one
standing in for the gateway, one for the native endpoint — so the comparison
itself is exercised, not mocked, and no money is spent.
"""
from __future__ import annotations

import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
from unittest import mock

from vco_lib import vscode_settings as vs

HOST_TOKEN = "dogfood-host-token-not-a-real-secret"
ACCESS_TOKEN = "dogfood-oauth-access-token-synthetic"


class _Endpoint(BaseHTTPRequestHandler):
    """One stub endpoint. Behaviour comes from the server's `plan`."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # noqa: D102 — silence the stub
        pass

    def _send(self, status: int, payload, *, raw: bytes | None = None) -> None:
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's contract
        plan = self.server.plan
        if self.path.startswith("/health"):
            self._send(200, {"ok": True, "service": "vct-model-gateway",
                             "version": plan.get("version", "99.0.0")})
            return
        if self.path.startswith("/v1/models"):
            self._send(200, {"data": plan.get("models", [{"id": "claude-opus-5"}])})
            return
        self._send(404, {"error": "no route"})

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's contract
        plan = self.server.plan
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.server.seen.append(
            {"path": self.path, "len": len(body),
             "auth": self.headers.get("Authorization", ""),
             "beta": self.headers.get("anthropic-beta", "")}
        )
        if plan.get("status_override"):
            self._send(plan["status_override"], {"error": "synthetic"})
            return
        if self.path.startswith("/v1/messages/count_tokens"):
            tokens = plan.get("tokens")
            if tokens is None:
                # The honest default: a token count derived from the body, so
                # the two stubs agree only when they saw the same bytes.
                tokens = len(body) // 4
            self._send(200, {"input_tokens": tokens})
            return
        raw = plan.get("stream_body", b"data: {\"stop_reason\":\"max_tokens\"}\n\n"
                                     b"event: message_stop\ndata: {}\n\n")
        self._send(200, None, raw=raw)


class _Stub:
    """A loopback endpoint with a mutable behaviour plan."""

    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Endpoint)
        self.server.plan = {}
        self.server.seen = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def plan(self) -> dict:
        return self.server.plan

    @property
    def seen(self) -> list:
        return self.server.seen

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class DogfoodTests(unittest.TestCase):
    """The proof, driven against two stubs — gateway and 'native'."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="dogfood-")
        self.addCleanup(self.tmp.cleanup)
        self.credentials = Path(self.tmp.name) / ".credentials.json"
        self.credentials.write_text(
            json.dumps(
                {"claudeAiOauth": {
                    "accessToken": ACCESS_TOKEN,
                    "expiresAt": int(time() * 1000) + 3_600_000,
                }}
            ),
            encoding="utf-8",
        )
        self.gateway = _Stub()
        self.native = _Stub()
        self.addCleanup(self.gateway.stop)
        self.addCleanup(self.native.stop)
        # The native half is the ONE seam: everything else about that call —
        # headers, body, parsing — is the shipped code.
        patcher = mock.patch.object(
            vs,
            "_native_connection",
            lambda timeout: http.client.HTTPConnection(
                "127.0.0.1", self.native.port, timeout=timeout,
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_proof(self, **kwargs) -> dict:
        return vs.dogfood_gateway(
            self.gateway.port,
            HOST_TOKEN,
            credentials_file=self.credentials,
            timeout=5.0,
            **kwargs,
        )

    # ── the happy path ──────────────────────────────────────────────────

    def test_matching_answers_pass_and_the_bodies_really_travelled(self) -> None:
        out = self.run_proof()
        self.assertEqual(out["status"], "ok", out)
        self.assertTrue(out["ok"])
        self.assertIsNone(out["reason"])

        # Both endpoints saw BOTH bodies, at the sizes that matter: past the
        # 1 MiB default that caused the incident, and the accented one still
        # ACCENTED (an `ensure_ascii` re-encode would make it plain ASCII at
        # six times the length, proving nothing about non-ASCII).
        for stub in (self.gateway, self.native):
            sizes = [r["len"] for r in stub.seen]
            self.assertEqual(len(sizes), 2, stub.seen)
            self.assertGreater(max(sizes), 1024 * 1024)
        self.assertEqual(
            [r["len"] for r in self.gateway.seen],
            [r["len"] for r in self.native.seen],
            "both endpoints must receive the same bytes",
        )
        self.assertLess(out["elapsed_s"], vs.DOGFOOD_TOTAL_BUDGET_S)

    def test_the_native_leg_presents_the_oauth_beta_a_panel_would(self) -> None:
        """Otherwise the two calls differ in more than the endpoint."""
        self.run_proof()
        native_call = self.native.seen[0]
        self.assertIn(vs.DOGFOOD_OAUTH_BETA, native_call["beta"])
        self.assertTrue(native_call["auth"].startswith("Bearer "))
        # The gateway leg presents the HOST token, never the Claude login.
        self.assertEqual(self.gateway.seen[0]["auth"], f"Bearer {HOST_TOKEN}")
        self.assertNotIn(ACCESS_TOKEN, self.gateway.seen[0]["auth"])

    # ── the refusals, one per case ──────────────────────────────────────

    def test_a_gateway_413_on_a_big_body_refuses_the_point(self) -> None:
        """The 2026-09-09 defect, exactly: 413 through the gateway alone."""
        self.gateway.plan["status_override"] = 413
        out = self.run_proof()
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["reason"], "dogfood:ascii_body")
        self.assertIn("413", out["message"])

    def test_a_differing_token_count_refuses_the_point(self) -> None:
        """A mangled body counts differently — silently, without it."""
        self.gateway.plan["tokens"] = 11
        self.native.plan["tokens"] = 12
        out = self.run_proof()
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["reason"], "dogfood:ascii_body")

    def test_an_empty_picker_refuses_the_point(self) -> None:
        self.gateway.plan["models"] = [{"id": "claude-gw/glm-5.3"}]
        out = self.run_proof()
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["reason"], "dogfood:models")
        self.assertIn("none first-party", out["message"])

    def test_an_older_daemon_than_this_install_refuses_the_point(self) -> None:
        """A gateway left running from a previous install has none of the fixes."""
        self.gateway.plan["version"] = "0.0.1"
        out = self.run_proof()
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["reason"], "dogfood:version")
        self.assertIn("Restart the gateway", out["message"])

    def test_the_accented_body_is_actually_accented_on_the_wire(self) -> None:
        """`ensure_ascii=True` would make it plain ASCII at six times the size.

        The case is named for non-ASCII handling; if every `è` leaves as
        `\\u00e8` the proof sends ASCII, tests nothing it claims to, and
        quietly costs six times the upload it budgeted for.
        """
        self.run_proof()
        accented = self.gateway.seen[1]["len"]
        # UTF-8: two bytes per `è`. Escaped: six.
        self.assertLess(accented, 3 * 1024 * 1024)
        self.assertGreater(accented, 1024 * 1024)

    def test_a_gateway_transport_error_is_evidence_only_when_native_answers(
        self,
    ) -> None:
        """A slow uplink must not read as a broken daemon.

        The gateway leg carries the upload, so its socket timeout can fire on
        the NETWORK. It is only evidence when the native leg proved, in the
        same budget, that the network was fine — which is what this asserts,
        and what the next test asserts the absence of.
        """
        self.gateway.stop()
        out = self.run_proof()
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["reason"], "dogfood:ascii_body")
        self.assertIn("api.anthropic.com answered the same body", out["message"])

    def test_both_endpoints_failing_is_a_skip_not_a_refusal(self) -> None:
        """Nothing was learned about the gateway, so nothing is claimed."""
        self.gateway.stop()
        self.native.stop()
        out = self.run_proof()
        self.assertEqual(out["status"], "skipped")
        self.assertIsNone(out["reason"])

    # ── the two "cannot run" cases, which must NOT block the user ───────

    def test_no_claude_login_skips_rather_than_refusing(self) -> None:
        """A vendor-only machine has a working gateway and no login."""
        self.credentials.write_text("{}", encoding="utf-8")
        out = self.run_proof()
        self.assertEqual(out["status"], "skipped")
        self.assertIsNone(out["reason"])
        self.assertFalse(out["ok"])
        self.assertEqual(self.gateway.seen, [], "nothing was sent")

    def test_an_unreachable_native_endpoint_skips(self) -> None:
        self.native.stop()
        out = self.run_proof()
        self.assertEqual(out["status"], "skipped")
        self.assertIsNone(out["reason"])

    def test_an_expired_login_is_a_skip_not_a_false_pass(self) -> None:
        self.credentials.write_text(
            json.dumps(
                {"claudeAiOauth": {
                    "accessToken": ACCESS_TOKEN,
                    "expiresAt": int(time() * 1000) - 1000,
                }}
            ),
            encoding="utf-8",
        )
        out = self.run_proof()
        self.assertEqual(out["status"], "skipped")

    # ── the paid case is opt-in, and off by default ─────────────────────

    def test_the_streamed_comparison_does_not_run_by_default(self) -> None:
        out = self.run_proof()
        self.assertNotIn("stream", [c["case"] for c in out["cases"]])
        for stub in (self.gateway, self.native):
            self.assertTrue(
                all(r["path"].endswith("count_tokens") for r in stub.seen),
                "a default run must not spend money",
            )

    def test_the_streamed_comparison_runs_when_asked(self) -> None:
        out = self.run_proof(cost_free_only=False)
        self.assertEqual(out["status"], "ok", out)
        self.assertIn("stream", [c["case"] for c in out["cases"]])
        self.assertTrue(
            any(r["path"] == "/v1/messages" for r in self.gateway.seen),
        )

    def test_a_stream_without_message_stop_refuses(self) -> None:
        self.gateway.plan["stream_body"] = b"data: {\"type\":\"ping\"}\n\n"
        out = self.run_proof(cost_free_only=False)
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["reason"], "dogfood:stream")
        self.assertIn("message_stop", out["message"])

    # ── the credential never leaves the function ────────────────────────

    def test_no_token_value_appears_in_the_result(self) -> None:
        """The result is emitted as JSON by the CLI and read by the GUI."""
        out = self.run_proof()
        blob = json.dumps(out)
        self.assertNotIn(ACCESS_TOKEN, blob)
        self.assertNotIn(HOST_TOKEN, blob)


class BudgetParityTests(unittest.TestCase):
    """The proof's own budget, and what a caller must allow for.

    Named by `DOGFOOD_TIMEOUT` in
    `launcher/src-tauri/src/commands/model_gateway.rs`, which reads these two
    constants out of this module: the launcher's deadline must exceed
    `TOTAL + one CALL`, or it kills a run that was about to answer.
    """

    def test_the_budget_bounds_every_leg_not_just_the_bodies(self) -> None:
        source = Path(vs.__file__).read_text(encoding="utf-8")
        # One check per leg: two bodies, models, version, stream.
        self.assertGreaterEqual(
            source.count("> DOGFOOD_TOTAL_BUDGET_S"), 3,
            "each leg must check the budget before spending a call on it",
        )

    def test_the_worst_case_is_one_call_past_the_budget(self) -> None:
        self.assertGreater(vs.DOGFOOD_TOTAL_BUDGET_S, vs.DOGFOOD_CALL_TIMEOUT_S)
        worst = vs.DOGFOOD_TOTAL_BUDGET_S + vs.DOGFOOD_CALL_TIMEOUT_S
        self.assertEqual(worst, 28.0)

    def test_the_bodies_fit_the_per_call_budget_on_a_modest_uplink(self) -> None:
        """10 Mbit up is a normal home connection; four uploads must fit.

        The gateway leg's `getresponse()` covers the gateway's OWN upload to
        Anthropic as well, so each body crosses the uplink twice inside one
        `DOGFOOD_CALL_TIMEOUT_S`.
        """
        megabits_per_s = 10.0
        for size in (vs.DOGFOOD_ASCII_BYTES, vs.DOGFOOD_ACCENTED_BYTES * 2):
            seconds = (size * 8 / 1_000_000) / megabits_per_s * 2
            self.assertLess(
                seconds, vs.DOGFOOD_CALL_TIMEOUT_S,
                f"{size} bytes needs {seconds:.1f}s of uplink per call",
            )


class PointRefusesOnMismatchTests(unittest.TestCase):
    """`point` must not write into a panel it just proved broken."""

    def test_a_refused_proof_stops_the_write(self) -> None:
        with TemporaryDirectory(prefix="dogfood-point-") as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text("{}", encoding="utf-8")
            refusal = {
                "action": "dogfood_gateway",
                "ok": False,
                "status": "refused",
                "reason": "dogfood:ascii_body",
                "message": "the same 6291456-byte request answered HTTP 413",
                "cases": [],
                "elapsed_s": 0.1,
            }
            with mock.patch.object(vs, "probe_gateway", lambda **_: vs.GATEWAY_RUNNING), \
                 mock.patch.object(vs, "resolve_host_token", lambda: "tok"), \
                 mock.patch.object(vs, "resolve_gateway_ports", lambda: (11436,)), \
                 mock.patch.object(vs, "dogfood_gateway", lambda *a, **k: refusal), \
                 mock.patch.object(vs, "point_at_gateway") as writer:
                code = vs.main(["point", "--path", str(settings)])
            self.assertEqual(code, 1)
            writer.assert_not_called()
            self.assertEqual(settings.read_text(encoding="utf-8"), "{}")

    def test_a_passing_proof_lets_the_write_through(self) -> None:
        with TemporaryDirectory(prefix="dogfood-point-") as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text("{}", encoding="utf-8")
            passing = {"status": "ok", "ok": True, "reason": None, "message": "",
                       "cases": [], "elapsed_s": 0.1}
            with mock.patch.object(vs, "probe_gateway", lambda **_: vs.GATEWAY_RUNNING), \
                 mock.patch.object(vs, "resolve_host_token", lambda: "tok"), \
                 mock.patch.object(vs, "resolve_gateway_ports", lambda: (11436,)), \
                 mock.patch.object(vs, "dogfood_gateway", lambda *a, **k: passing):
                code = vs.main(["point", "--path", str(settings)])
            self.assertEqual(code, 0)
            written = json.loads(settings.read_text(encoding="utf-8"))
            self.assertIn(vs.ENV_BLOCK_KEY, written)

    def test_a_gateway_that_is_not_running_is_not_proved(self) -> None:
        """Pointing at a gateway you have not started yet is legitimate."""
        with TemporaryDirectory(prefix="dogfood-point-") as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text("{}", encoding="utf-8")
            calls = []
            with mock.patch.object(vs, "probe_gateway", lambda **_: vs.GATEWAY_STOPPED), \
                 mock.patch.object(vs, "resolve_host_token", lambda: "tok"), \
                 mock.patch.object(vs, "resolve_gateway_ports", lambda: (11436,)), \
                 mock.patch.object(
                     vs, "dogfood_gateway", lambda *a, **k: calls.append(a) or {},
                 ):
                code = vs.main(["point", "--path", str(settings)])
            self.assertEqual(code, 0)
            self.assertEqual(calls, [], "nothing to prove against a stopped gateway")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
