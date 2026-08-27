# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-D item 4 — stale-env hub-token fallback in the wrapper MCPs.

A wrapper MCP is LONG-LIVED: it outlives the shell that spawned it. So a
stale exported ``VCT_HUB_TOKEN`` used to poison it for its whole
lifetime — the 401 handler invalidated the cached credentials, and
``_get_hub_credentials`` promptly re-preferred the same dead env value.

Pinned here:
  * a PROVABLE refusal (401/403) with a provably-stale env pin retries
    ONCE with the on-disk token, and on success LATCHES the env pin off
    for the rest of the process (so call #2 does not 401 again);
  * ``VCT_HUB_TOKEN_STRICT=1`` disables the fallback;
  * every leave-alone case makes exactly ONE request and keeps the
    pre-v0.2.91 behaviour.

Fully offline: a fake aiohttp session, synthetic tokens.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from claude_mcp_servers.wrappers._base import (  # noqa: E402
    STALE_ENV_TOKEN_MESSAGE,
    WrapperMCP,
    _HubUnreachable,
)


ENV_TOKEN = "stale-env-token-0000-not-a-real-secret"
DISK_TOKEN = "fresh-disk-token-1111-not-a-real-secret"


class _FakeResp:
    def __init__(self, status: int, body):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._body


class _FakeSession:
    """Records the bearer of every GET and serves canned responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.bearers: list[str] = []

    def get(self, url, **kwargs):
        self.bearers.append(kwargs["headers"]["Authorization"])
        return self._responses.pop(0)


class _WrapperStaleTokenBase(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp(prefix="vct-wrapper-stale-"))
        (self.state / "hub.token").write_text(DISK_TOKEN, encoding="utf-8")
        self._env = mock.patch.dict(
            os.environ,
            {"VCT_HUB_PORT": "9001", "VCT_HUB_TOKEN": ENV_TOKEN},
            clear=False,
        )
        self._env.start()
        os.environ.pop("VCT_HUB_TOKEN_STRICT", None)
        self._root = mock.patch(
            "claude_mcp_servers.wrappers._base.vct_root_dir",
            return_value=self.state,
        )
        self._root.start()

    def tearDown(self) -> None:
        self._root.stop()
        self._env.stop()
        shutil.rmtree(self.state, ignore_errors=True)

    def _wrapper(self, session: _FakeSession) -> WrapperMCP:
        w = WrapperMCP(mcp_name="testmcp", upstream_argv=["unused"])
        w._get_http_session = mock.AsyncMock(return_value=session)  # type: ignore[method-assign]
        return w


class GrantsFetchTests(_WrapperStaleTokenBase):
    def test_401_retries_with_disk_token_and_latches_the_pin_off(self) -> None:
        """RED-PROOF: pre-fix the wrapper raised _HubUnreachable here and
        the next call re-read the SAME stale env token."""
        session = _FakeSession([
            _FakeResp(401, None),
            _FakeResp(200, {"grants": {"tool_a": True}}),
        ])
        w = self._wrapper(session)
        with self.assertLogs("claude_mcp_servers.wrappers._base", "WARNING") as cm:
            grants = asyncio.run(w._fetch_grants_from_hub("proj-1"))
        self.assertEqual(grants, {"tool_a": True})
        self.assertEqual(
            session.bearers,
            [f"Bearer {ENV_TOKEN}", f"Bearer {DISK_TOKEN}"],
            "the retry must present the ON-DISK token",
        )
        self.assertTrue(any(STALE_ENV_TOKEN_MESSAGE in m for m in cm.output))
        # The latch: a long-lived wrapper must stop re-reading the dead
        # env value on every subsequent credential resolution.
        self.assertTrue(w._ignore_env_hub_token)
        port, token = w._get_hub_credentials()
        self.assertEqual(port, 9001)
        self.assertEqual(token, DISK_TOKEN)

    def test_403_retries_too(self) -> None:
        session = _FakeSession([
            _FakeResp(403, {"error": {"code": "forbidden"}}),
            _FakeResp(200, {"grants": {"tool_b": False}}),
        ])
        w = self._wrapper(session)
        with self.assertLogs("claude_mcp_servers.wrappers._base", "WARNING"):
            grants = asyncio.run(w._fetch_grants_from_hub("proj-1"))
        self.assertEqual(grants, {"tool_b": False})
        self.assertEqual(len(session.bearers), 2)

    def test_strict_pin_disables_the_retry(self) -> None:
        session = _FakeSession([_FakeResp(401, None)])
        w = self._wrapper(session)
        with mock.patch.dict(os.environ, {"VCT_HUB_TOKEN_STRICT": "1"}):
            with self.assertRaises(_HubUnreachable):
                asyncio.run(w._fetch_grants_from_hub("proj-1"))
        self.assertEqual(session.bearers, [f"Bearer {ENV_TOKEN}"])
        self.assertFalse(w._ignore_env_hub_token)

    def test_identical_tokens_make_exactly_one_request(self) -> None:
        session = _FakeSession([_FakeResp(401, None)])
        w = self._wrapper(session)
        with mock.patch.dict(os.environ, {"VCT_HUB_TOKEN": DISK_TOKEN}):
            with self.assertRaises(_HubUnreachable):
                asyncio.run(w._fetch_grants_from_hub("proj-1"))
        self.assertEqual(len(session.bearers), 1)

    def test_retry_that_is_also_refused_keeps_the_401_path(self) -> None:
        session = _FakeSession([_FakeResp(401, None), _FakeResp(401, None)])
        w = self._wrapper(session)
        with self.assertRaises(_HubUnreachable):
            asyncio.run(w._fetch_grants_from_hub("proj-1"))
        self.assertEqual(len(session.bearers), 2)
        self.assertFalse(
            w._ignore_env_hub_token,
            "a failed retry must NOT latch the env pin off",
        )

    def test_retry_that_5xxs_does_not_latch_the_pin_off(self) -> None:
        """v0.2.91 wave-3 (MINOR-1). RED pre-fix: ANY non-401/403 retry
        answer was adopted, so a 503 after the 401 latched the env pin off
        for this LONG-LIVED wrapper and logged the definitive line — on an
        answer that proves nothing about the credential."""
        session = _FakeSession([_FakeResp(401, None), _FakeResp(503, None)])
        w = self._wrapper(session)
        with self.assertRaises(_HubUnreachable) as ctx:
            asyncio.run(w._fetch_grants_from_hub("proj-1"))
        self.assertIn("401", str(ctx.exception),
                      "the ORIGINAL refusal must be what the caller sees")
        self.assertEqual(len(session.bearers), 2)
        self.assertFalse(w._ignore_env_hub_token)
        self.assertFalse(w._stale_env_token_warned)

    def test_retry_that_404s_IS_adopted(self) -> None:
        """LEAVE-ALONE half: 404 is a POST-AUTH answer (the hub routes only
        after its auth middleware accepted the bearer), so it proves the
        fallback worked and the pin is latched off."""
        session = _FakeSession([_FakeResp(401, None), _FakeResp(404, None)])
        w = self._wrapper(session)
        with self.assertLogs("claude_mcp_servers.wrappers._base", "WARNING"):
            with self.assertRaises(_HubUnreachable) as ctx:
                asyncio.run(w._fetch_grants_from_hub("proj-1"))
        self.assertIn("404", str(ctx.exception))
        self.assertTrue(w._ignore_env_hub_token)

    def test_success_makes_exactly_one_request(self) -> None:
        session = _FakeSession([_FakeResp(200, {"grants": {}})])
        w = self._wrapper(session)
        self.assertEqual(asyncio.run(w._fetch_grants_from_hub("proj-1")), {})
        self.assertEqual(len(session.bearers), 1)

    def test_404_is_not_a_credential_problem(self) -> None:
        session = _FakeSession([_FakeResp(404, None)])
        w = self._wrapper(session)
        with self.assertRaises(_HubUnreachable):
            asyncio.run(w._fetch_grants_from_hub("proj-1"))
        self.assertEqual(len(session.bearers), 1)


class ByPathTests(_WrapperStaleTokenBase):
    def test_401_retries_with_disk_token(self) -> None:
        session = _FakeSession([
            _FakeResp(401, None),
            _FakeResp(200, {"id": "proj-9"}),
        ])
        w = self._wrapper(session)
        with self.assertLogs("claude_mcp_servers.wrappers._base", "WARNING"):
            pid = asyncio.run(w._fetch_project_id_by_path("/tmp/whatever"))
        self.assertEqual(pid, "proj-9")
        self.assertEqual(
            session.bearers, [f"Bearer {ENV_TOKEN}", f"Bearer {DISK_TOKEN}"]
        )

    def test_404_still_means_no_project(self) -> None:
        session = _FakeSession([_FakeResp(404, None)])
        w = self._wrapper(session)
        self.assertIsNone(asyncio.run(w._fetch_project_id_by_path("/tmp/x")))
        self.assertEqual(len(session.bearers), 1)

    def test_403_without_a_stale_pin_keeps_todays_shape(self) -> None:
        """LEAVE-ALONE: pre-v0.2.91 a 403 body was parsed and yielded
        None (no ``id`` field). That must not change."""
        session = _FakeSession([_FakeResp(403, {"error": {"code": "forbidden"}})])
        w = self._wrapper(session)
        with mock.patch.dict(os.environ, {"VCT_HUB_TOKEN": DISK_TOKEN}):
            self.assertIsNone(asyncio.run(w._fetch_project_id_by_path("/tmp/x")))
        self.assertEqual(len(session.bearers), 1)


class DecisionFunctionTests(_WrapperStaleTokenBase):
    def test_rules(self) -> None:
        w = WrapperMCP("m", ["x"])
        self.assertEqual(w._stale_env_token_fallback(), DISK_TOKEN)
        with mock.patch.dict(os.environ, {"VCT_HUB_TOKEN_STRICT": "1"}):
            self.assertIsNone(w._stale_env_token_fallback())
        with mock.patch.dict(os.environ, {"VCT_HUB_TOKEN": DISK_TOKEN}):
            self.assertIsNone(w._stale_env_token_fallback())
        (self.state / "hub.token").unlink()
        self.assertIsNone(w._stale_env_token_fallback())

    def test_no_env_pin_means_no_fallback(self) -> None:
        w = WrapperMCP("m", ["x"])
        env = {k: v for k, v in os.environ.items() if k != "VCT_HUB_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(w._stale_env_token_fallback())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
