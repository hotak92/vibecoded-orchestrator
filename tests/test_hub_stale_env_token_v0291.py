# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-D item 4 — stale-env hub-token fallback (Python SSOT).

THE SEAM
--------
Every resolver prefers ``$VCT_HUB_TOKEN`` over ``<vct_root>/hub.token``.
The hub regenerates ``hub.token`` on every start, so a shell that
exported the token BEFORE an update holds a value the hub refuses — and
``_get_with_401_retry``'s re-discovery re-preferred that same dead env
value, making the retry a no-op in precisely that scenario.

THE CONTRACT PINNED HERE
------------------------
* On a PROVABLE refusal (401/403) with a provably-stale env pin, ONE
  retry presents the ON-DISK token (scoped when the route is
  per-project), and on success prints ONE definitive stderr line.
* ``VCT_HUB_TOKEN_STRICT=1`` disables the fallback entirely (the
  hermeticity guard — harnesses that pin a bad token still see the 401).
* Every leave-alone case (no env pin, identical tokens, non-refusal
  status, retry also refused) keeps the pre-v0.2.91 behaviour and
  response object, because exit codes / error text are a contract.

All fixtures are synthetic; the tokens below are obviously fake.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vco_lib import project_config


ENV_TOKEN = "stale-env-token-0000-not-a-real-secret"
DISK_TOKEN = "fresh-disk-token-1111-not-a-real-secret"
SCOPED_TOKEN = "scoped-disk-token-2222-not-a-real-secret"
PROJECT_ID = "11111111-2222-3333-4444-555555555555"


def _make_response(status_code: int, body=None) -> mock.Mock:
    resp = mock.Mock()
    resp.status_code = status_code
    resp.text = json.dumps(body) if body is not None else ""
    resp.json = mock.Mock(return_value=body)
    return resp


def _bearers(session: mock.Mock) -> list[str]:
    """The Authorization header presented on each GET, in order."""
    out = []
    for call in session.get.call_args_list:
        out.append(call.kwargs["headers"]["Authorization"])
    return out


class _StaleTokenBase(unittest.TestCase):
    """Temp state dir + stubbed session; no disk or network outside it."""

    def setUp(self) -> None:
        project_config._test_clear_cache()
        self._td = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._td.name)
        (self.state_dir / "hub.token").write_text(DISK_TOKEN, encoding="utf-8")
        self._env = mock.patch.dict(
            os.environ,
            {
                "VCT_STATE_DIR": str(self.state_dir),
                "VCT_HUB_PORT": "9999",
                "VCT_HUB_TOKEN": ENV_TOKEN,
            },
        )
        self._env.start()
        os.environ.pop("VCT_HUB_TOKEN_STRICT", None)
        self.session = mock.Mock(spec=["get", "close", "mount"])
        self._session_patch = mock.patch.object(
            project_config, "_http_session", return_value=self.session
        )
        self._session_patch.start()

    def tearDown(self) -> None:
        self._session_patch.stop()
        self._env.stop()
        self._td.cleanup()
        project_config._test_clear_cache()

    def _get(self):
        return project_config._get_with_401_retry(
            lambda port, token: f"http://127.0.0.1:{port}/api/v1/projects",
        )

    def _get_project_route(self):
        return project_config._get_with_401_retry(
            lambda port, token: (
                f"http://127.0.0.1:{port}/api/v1/projects/{PROJECT_ID}/env"
            ),
            project_id=PROJECT_ID,
        )


# ─── The decision function ──────────────────────────────────────────────


class DecisionFunctionTest(_StaleTokenBase):
    def test_returns_disk_token_when_env_pin_is_stale(self) -> None:
        self.assertEqual(project_config._stale_env_token_fallback(), DISK_TOKEN)

    def test_strict_pin_disables_the_fallback(self) -> None:
        with mock.patch.dict(os.environ, {"VCT_HUB_TOKEN_STRICT": "1"}):
            self.assertIsNone(project_config._stale_env_token_fallback())

    def test_no_env_token_means_no_fallback(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "VCT_HUB_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(project_config._stale_env_token_fallback())

    def test_identical_tokens_mean_no_fallback(self) -> None:
        with mock.patch.dict(os.environ, {"VCT_HUB_TOKEN": DISK_TOKEN}):
            self.assertIsNone(project_config._stale_env_token_fallback())

    def test_absent_disk_token_means_no_fallback(self) -> None:
        (self.state_dir / "hub.token").unlink()
        self.assertIsNone(project_config._stale_env_token_fallback())

    def test_scoped_token_preferred_for_a_per_project_route(self) -> None:
        (self.state_dir / f"hub.token.{PROJECT_ID}").write_text(
            SCOPED_TOKEN, encoding="utf-8"
        )
        self.assertEqual(
            project_config._stale_env_token_fallback(PROJECT_ID), SCOPED_TOKEN
        )
        # …and the GLOBAL route still resolves the global token.
        self.assertEqual(project_config._stale_env_token_fallback(), DISK_TOKEN)


# ─── The 401 path ───────────────────────────────────────────────────────


class Retry401Test(_StaleTokenBase):
    def test_retry_presents_the_disk_token_and_warns_once(self) -> None:
        """RED-PROOF: pre-fix the retry re-presented the stale ENV token
        (``_discover_hub`` re-prefers env), so the second bearer was
        identical to the first and the 401 stood."""
        self.session.get.side_effect = [
            _make_response(401, {"error": {"code": "unauthorized"}}),
            _make_response(200, {"ok": True}),
        ]
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            _bearers(self.session),
            [f"Bearer {ENV_TOKEN}", f"Bearer {DISK_TOKEN}"],
            "the retry must present the ON-DISK token, not the stale pin",
        )
        self.assertIn(project_config.STALE_ENV_TOKEN_MESSAGE, err.getvalue())

    def test_warning_is_emitted_once_per_process(self) -> None:
        self.session.get.side_effect = [
            _make_response(401), _make_response(200, {"ok": True}),
            _make_response(401), _make_response(200, {"ok": True}),
        ]
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            self._get()
            self._get()
        self.assertEqual(
            err.getvalue().count(project_config.STALE_ENV_TOKEN_MESSAGE), 1
        )

    def test_strict_pin_keeps_todays_401_path(self) -> None:
        """LEAVE-ALONE: with the guard set, both attempts present the
        pinned token and the caller still sees the 401."""
        self.session.get.side_effect = [
            _make_response(401, {"error": {"code": "unauthorized"}}),
            _make_response(401, {"error": {"code": "unauthorized"}}),
        ]
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"VCT_HUB_TOKEN_STRICT": "1"}):
            with mock.patch("sys.stderr", err):
                resp = self._get()
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(
            _bearers(self.session),
            [f"Bearer {ENV_TOKEN}", f"Bearer {ENV_TOKEN}"],
            "a strict pin must never be replaced by the on-disk token",
        )
        self.assertNotIn(project_config.STALE_ENV_TOKEN_MESSAGE, err.getvalue())

    def test_no_env_pin_keeps_the_cache_invalidation_retry(self) -> None:
        """LEAVE-ALONE: without an env pin the pre-existing rotation
        retry still runs (re-reading hub.token), unchanged."""
        env = {k: v for k, v in os.environ.items() if k != "VCT_HUB_TOKEN"}
        self.session.get.side_effect = [
            _make_response(401), _make_response(200, {"ok": True}),
        ]
        with mock.patch.dict(os.environ, env, clear=True):
            resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            _bearers(self.session),
            [f"Bearer {DISK_TOKEN}", f"Bearer {DISK_TOKEN}"],
        )

    def test_scoped_token_used_on_a_per_project_route(self) -> None:
        (self.state_dir / f"hub.token.{PROJECT_ID}").write_text(
            SCOPED_TOKEN, encoding="utf-8"
        )
        self.session.get.side_effect = [
            _make_response(401), _make_response(200, {"ok": True}),
        ]
        with mock.patch("sys.stderr", io.StringIO()):
            resp = self._get_project_route()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            _bearers(self.session),
            [f"Bearer {ENV_TOKEN}", f"Bearer {SCOPED_TOKEN}"],
            "a per-project route must fall back to the SCOPED on-disk token",
        )

    def test_fallback_that_also_401s_returns_the_refusal(self) -> None:
        self.session.get.side_effect = [
            _make_response(401), _make_response(401, {"error": {"code": "x"}}),
        ]
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            resp = self._get()
        self.assertEqual(resp.status_code, 401)
        self.assertNotIn(project_config.STALE_ENV_TOKEN_MESSAGE, err.getvalue())


# ─── The 403 path ───────────────────────────────────────────────────────


class Retry403Test(_StaleTokenBase):
    def test_stale_pin_gets_one_extra_attempt(self) -> None:
        self.session.get.side_effect = [
            _make_response(403, {"error": {"code": "forbidden"}}),
            _make_response(200, {"ok": True}),
        ]
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            _bearers(self.session),
            [f"Bearer {ENV_TOKEN}", f"Bearer {DISK_TOKEN}"],
        )
        self.assertIn(project_config.STALE_ENV_TOKEN_MESSAGE, err.getvalue())

    def test_without_a_stale_pin_403_is_never_retried(self) -> None:
        """LEAVE-ALONE: a 403 is an authorization decision, not a
        rotation artefact — exactly ONE request, as before v0.2.91."""
        forbidden = _make_response(403, {"error": {"code": "forbidden"}})
        self.session.get.side_effect = [forbidden]
        with mock.patch.dict(os.environ, {"VCT_HUB_TOKEN": DISK_TOKEN}):
            resp = self._get()
        self.assertIs(resp, forbidden)
        self.assertEqual(self.session.get.call_count, 1)

    def test_strict_pin_keeps_todays_403_path(self) -> None:
        forbidden = _make_response(403, {"error": {"code": "forbidden"}})
        self.session.get.side_effect = [forbidden]
        with mock.patch.dict(os.environ, {"VCT_HUB_TOKEN_STRICT": "1"}):
            resp = self._get()
        self.assertIs(resp, forbidden)
        self.assertEqual(self.session.get.call_count, 1)

    def test_fallback_that_is_also_refused_returns_the_original(self) -> None:
        original = _make_response(403, {"error": {"code": "forbidden"}})
        self.session.get.side_effect = [
            original, _make_response(401, {"error": {"code": "unauthorized"}}),
        ]
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            resp = self._get()
        self.assertIs(
            resp, original,
            "a failed fallback must leave the caller's error path byte-identical",
        )
        self.assertNotIn(project_config.STALE_ENV_TOKEN_MESSAGE, err.getvalue())


# ─── Only a DEFINITIVE retry answer may be adopted ──────────────────────


class RetryAnswerAdoptionTest(_StaleTokenBase):
    """v0.2.91 wave-3 (MINOR-1). RED pre-fix on both legs: any non-401/403
    retry answer was adopted, so a hub that refused the stale pin and then
    hiccuped a 5xx printed the definitive line and handed the caller
    ``hub returned 503`` in place of the truthful 401/403."""

    def test_a_5xx_on_the_401_retry_returns_the_original_401(self) -> None:
        original = _make_response(401, {"error": {"code": "unauthorized"}})
        self.session.get.side_effect = [
            original, _make_response(503, {"error": {"code": "keychain_locked"}}),
        ]
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            resp = self._get()
        self.assertIs(
            resp, original,
            "a 5xx proves nothing about the credential — keep the refusal",
        )
        self.assertNotIn(project_config.STALE_ENV_TOKEN_MESSAGE, err.getvalue())

    def test_a_5xx_on_the_403_retry_returns_the_original_403(self) -> None:
        original = _make_response(403, {"error": {"code": "forbidden"}})
        self.session.get.side_effect = [original, _make_response(503)]
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            resp = self._get()
        self.assertIs(resp, original)
        self.assertNotIn(project_config.STALE_ENV_TOKEN_MESSAGE, err.getvalue())

    def test_a_404_on_the_retry_IS_adopted(self) -> None:
        """LEAVE-ALONE half: the hub answers 404 only AFTER its auth
        middleware accepted the bearer, so it PROVES the fallback worked —
        and the caller gets the precise ProjectNotFound instead of a 401."""
        not_found = _make_response(404, {"error": {"code": "project_not_found"}})
        self.session.get.side_effect = [_make_response(401), not_found]
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            resp = self._get()
        self.assertIs(resp, not_found)
        self.assertIn(project_config.STALE_ENV_TOKEN_MESSAGE, err.getvalue())

    def test_the_decision_function_is_the_one_home(self) -> None:
        for ok in (200, 204, 299, 404):
            self.assertTrue(project_config._retry_answer_is_definitive(ok), ok)
        for no in (400, 401, 403, 429, 500, 502, 503, 0):
            self.assertFalse(project_config._retry_answer_is_definitive(no), no)


# ─── Non-refusal statuses never trigger anything ────────────────────────


class NoRefusalTest(_StaleTokenBase):
    def test_success_makes_exactly_one_request(self) -> None:
        self.session.get.side_effect = [_make_response(200, {"ok": True})]
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.session.get.call_count, 1)
        self.assertEqual(err.getvalue(), "")

    def test_404_is_not_a_credential_problem(self) -> None:
        not_found = _make_response(404, {"error": {"code": "project_not_found"}})
        self.session.get.side_effect = [not_found]
        resp = self._get()
        self.assertIs(resp, not_found)
        self.assertEqual(self.session.get.call_count, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
