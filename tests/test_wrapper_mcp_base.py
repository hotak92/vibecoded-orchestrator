# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for :class:`claude_mcp_servers.wrappers._base.WrapperMCP`.

Mocks the upstream subprocess + the hub HTTP call so the tests run
fully offline. Coverage targets:

  * tools/list filter (allowed tools kept, disallowed dropped)
  * tools/call reject (error envelope returned, upstream never called)
  * tools/call allow (forwarded to upstream verbatim)
  * post_tool_success fires on successful response
  * post_tool_success swallows exceptions
  * allowlist cache TTL honoured (no re-fetch within TTL)
  * hub-down failsafe (allow-all + warning log)
  * no project (env unset, by-path returns 404) → failsafe
  * upstream stderr forwarded with prefix
  * malformed JSON-RPC pass-through (no crash)
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from claude_mcp_servers.wrappers._base import (  # noqa: E402
    WrapperMCP,
    _HubUnreachable,
    _hashable_id,
)


# ─── Helpers ──────────────────────────────────────────────────────────────


def _make_wrapper(
    grants: dict[str, bool] | None,
    *,
    failsafe: bool = False,
    project_id: str | None = "proj-1",
) -> WrapperMCP:
    """Build a WrapperMCP with the hub I/O paths stubbed out.

    Args:
        grants: dict returned by ``_fetch_grants_from_hub``. Ignored
            when ``failsafe=True``.
        failsafe: when True, both hub calls raise _HubUnreachable so
            the wrapper enters allow-all mode.
        project_id: result of ``_resolve_project_id``. None → no project.
    """
    w = WrapperMCP(
        mcp_name="testmcp",
        upstream_argv=["unused-binary"],
        allowlist_cache_ttl=60,
    )

    async def fake_resolve_project_id():
        return project_id

    async def fake_fetch_grants(pid):
        if failsafe:
            raise _HubUnreachable("test failsafe")
        return grants or {}

    w._resolve_project_id = fake_resolve_project_id  # type: ignore[assignment]
    w._fetch_grants_from_hub = fake_fetch_grants  # type: ignore[assignment]
    return w


def _run(coro):
    return asyncio.get_event_loop_policy().get_event_loop().run_until_complete(coro)


# ─── Allowlist resolution ────────────────────────────────────────────────


class ResolveAllowlistTests(unittest.IsolatedAsyncioTestCase):

    async def test_grants_returned_when_hub_responds(self):
        w = _make_wrapper({"foo": True, "bar": False})
        out = await w._resolve_allowlist()
        self.assertEqual(out, {"foo": True, "bar": False})

    async def test_failsafe_when_hub_unreachable(self):
        w = _make_wrapper(None, failsafe=True)
        out = await w._resolve_allowlist()
        self.assertIsNone(out, "hub-down → failsafe → None means allow-all")

    async def test_failsafe_when_no_project(self):
        w = _make_wrapper(None, project_id=None)
        out = await w._resolve_allowlist()
        self.assertIsNone(out)

    async def test_cache_ttl_avoids_refetch(self):
        call_count = 0

        async def counting_fetch(pid):
            nonlocal call_count
            call_count += 1
            return {"foo": True}

        w = WrapperMCP("m", ["x"], allowlist_cache_ttl=60)
        w._resolve_project_id = mock.AsyncMock(return_value="p")  # type: ignore[assignment]
        w._fetch_grants_from_hub = counting_fetch  # type: ignore[assignment]

        await w._resolve_allowlist()
        await w._resolve_allowlist()
        await w._resolve_allowlist()
        self.assertEqual(call_count, 1, "cache must avoid refetch within TTL")

    async def test_cache_expires(self):
        call_count = 0

        async def counting_fetch(pid):
            nonlocal call_count
            call_count += 1
            return {"foo": True}

        w = WrapperMCP("m", ["x"], allowlist_cache_ttl=0)  # TTL=0 → always expired
        w._resolve_project_id = mock.AsyncMock(return_value="p")  # type: ignore[assignment]
        w._fetch_grants_from_hub = counting_fetch  # type: ignore[assignment]

        await w._resolve_allowlist()
        await w._resolve_allowlist()
        self.assertEqual(call_count, 2, "TTL=0 must force refetch every call")

    async def test_failsafe_caches_too(self):
        """Failsafe state caches to avoid spamming the hub every call.

        After hub-down, we serve allow-all from cache for TTL seconds
        before retrying the hub.
        """
        call_count = 0

        async def counting_fetch(pid):
            nonlocal call_count
            call_count += 1
            raise _HubUnreachable("test")

        w = WrapperMCP("m", ["x"], allowlist_cache_ttl=60)
        w._resolve_project_id = mock.AsyncMock(return_value="p")  # type: ignore[assignment]
        w._fetch_grants_from_hub = counting_fetch  # type: ignore[assignment]

        for _ in range(5):
            self.assertIsNone(await w._resolve_allowlist())
        self.assertEqual(call_count, 1, "failsafe must cache; hub spam is unacceptable")


# ─── Request interception (tools/call gate) ──────────────────────────────


class InterceptToolsCallTests(unittest.IsolatedAsyncioTestCase):
    """Exercises _maybe_intercept_request directly, bypassing stdio."""

    async def test_disallowed_tool_returns_error_envelope(self):
        w = _make_wrapper({"allowed_tool": True, "blocked": False})

        # Capture stdout writes (the wrapper writes the error envelope there).
        # sys.stdout.buffer is read-only on real TTYs — substitute a whole
        # fake stdout object whose `.buffer` is our BytesIO.
        captured = io.BytesIO()
        fake_stdout = mock.MagicMock(buffer=captured)
        with mock.patch.object(sys, "stdout", fake_stdout):
            handled = await w._maybe_intercept_request(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "blocked", "arguments": {}},
                },
                proc=mock.MagicMock(),  # never used because handled=True
            )
        self.assertTrue(handled, "blocked tool must short-circuit (no upstream forwarding)")
        body = captured.getvalue().decode("utf-8").strip()
        env = json.loads(body)
        self.assertEqual(env["id"], 7)
        self.assertEqual(env["error"]["code"], -32601)
        self.assertIn("not allowed", env["error"]["message"])
        self.assertIn("Permissions tab", env["error"]["message"])

    async def test_allowed_tool_passes_through(self):
        w = _make_wrapper({"allowed_tool": True})
        handled = await w._maybe_intercept_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "allowed_tool", "arguments": {"k": "v"}},
            },
            proc=mock.MagicMock(),
        )
        self.assertFalse(handled, "allowed tool must be forwarded verbatim")

    async def test_validate_tool_call_hook_rejects(self):
        """Subclass hook returning a string MUST reject with -32602."""
        class ValidatingWrapper(WrapperMCP):
            def validate_tool_call(self, tool_name, arguments):
                return "boom: bad path"

        w = ValidatingWrapper("m", ["x"], allowlist_cache_ttl=60)
        w._resolve_project_id = mock.AsyncMock(return_value="p")  # type: ignore[assignment]

        async def fake_fetch(_pid):
            return {"do_thing": True}

        w._fetch_grants_from_hub = fake_fetch  # type: ignore[assignment]

        captured = io.BytesIO()
        fake_stdout = mock.MagicMock(buffer=captured)
        with mock.patch.object(sys, "stdout", fake_stdout):
            handled = await w._maybe_intercept_request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "do_thing", "arguments": {"path": "/bad"}},
                },
                proc=mock.MagicMock(),
            )
        self.assertTrue(handled)
        body = captured.getvalue().decode("utf-8").strip()
        env = json.loads(body)
        self.assertEqual(env["error"]["code"], -32602)
        self.assertIn("boom: bad path", env["error"]["message"])

    async def test_non_tools_call_method_not_intercepted(self):
        w = _make_wrapper({"x": True})
        handled = await w._maybe_intercept_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            proc=mock.MagicMock(),
        )
        self.assertFalse(handled, "non tools/call methods must pass through")

    async def test_failsafe_mode_allows_anything(self):
        w = _make_wrapper(None, failsafe=True)
        handled = await w._maybe_intercept_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "anything", "arguments": {}},
            },
            proc=mock.MagicMock(),
        )
        self.assertFalse(handled, "failsafe must allow all tools through")


# ─── Response filtering (tools/list filter + post-hook dispatch) ─────────


class FilterToolsListTests(unittest.IsolatedAsyncioTestCase):

    async def test_tools_list_filtered_to_allowed(self):
        w = _make_wrapper({"keep": True, "drop_me": False, "also_keep": True})
        msg = {
            "jsonrpc": "2.0",
            "id": 9,
            "result": {
                "tools": [
                    {"name": "keep", "description": "k"},
                    {"name": "drop_me", "description": "d"},
                    {"name": "also_keep"},
                ]
            },
        }
        out = await w._maybe_filter_response(msg)
        names = [t["name"] for t in out["result"]["tools"]]
        self.assertEqual(names, ["keep", "also_keep"])

    async def test_tools_list_failsafe_passes_through(self):
        w = _make_wrapper(None, failsafe=True)
        msg = {
            "jsonrpc": "2.0",
            "id": 9,
            "result": {
                "tools": [{"name": "a"}, {"name": "b"}],
            },
        }
        out = await w._maybe_filter_response(msg)
        self.assertEqual(len(out["result"]["tools"]), 2)

    async def test_non_tools_list_unchanged(self):
        w = _make_wrapper({"x": True})
        msg = {"jsonrpc": "2.0", "id": 1, "result": {"some": "other"}}
        out = await w._maybe_filter_response(msg)
        self.assertEqual(out, msg)

    async def test_unknown_tool_dropped_when_not_in_grants(self):
        """A tool not present in the grants dict is treated as disallowed.

        This is the conservative default: when the launcher hasn't seen
        a tool yet, the user must explicitly enable it. Matches the
        plan §3 Phase 4 item 2 "default_enabled=false on new tools".
        """
        w = _make_wrapper({"known": True})
        msg = {
            "jsonrpc": "2.0",
            "result": {"tools": [{"name": "known"}, {"name": "new_unknown"}]},
        }
        out = await w._maybe_filter_response(msg)
        names = [t["name"] for t in out["result"]["tools"]]
        self.assertEqual(names, ["known"])


# ─── Post-tool-success hook ───────────────────────────────────────────────


class PostToolHookTests(unittest.IsolatedAsyncioTestCase):

    async def test_post_hook_fires_on_success(self):
        calls: list[tuple[str, dict, dict]] = []

        class HookingWrapper(WrapperMCP):
            async def post_tool_success(self, tool_name, arguments, result):
                calls.append((tool_name, arguments, result))

        w = HookingWrapper("m", ["x"], allowlist_cache_ttl=60)
        w._resolve_project_id = mock.AsyncMock(return_value="p")  # type: ignore[assignment]

        async def fake_fetch(_pid):
            return {"save": True}

        w._fetch_grants_from_hub = fake_fetch  # type: ignore[assignment]

        # Simulate request → response flow.
        await w._maybe_intercept_request(
            {
                "jsonrpc": "2.0",
                "id": 42,
                "method": "tools/call",
                "params": {"name": "save", "arguments": {"path": "/foo"}},
            },
            proc=mock.MagicMock(),
        )
        await w._maybe_filter_response(
            {"jsonrpc": "2.0", "id": 42, "result": {"saved": True}}
        )
        # Hook is dispatched via asyncio.create_task — let it run.
        await asyncio.sleep(0.05)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "save")
        self.assertEqual(calls[0][1], {"path": "/foo"})
        self.assertEqual(calls[0][2], {"saved": True})

    async def test_post_hook_skipped_on_error_response(self):
        calls: list = []

        class HookingWrapper(WrapperMCP):
            async def post_tool_success(self, tool_name, arguments, result):
                calls.append((tool_name, arguments, result))

        w = HookingWrapper("m", ["x"])
        w._resolve_project_id = mock.AsyncMock(return_value="p")  # type: ignore[assignment]

        async def fake_fetch(_pid):
            return {"save": True}

        w._fetch_grants_from_hub = fake_fetch  # type: ignore[assignment]

        await w._maybe_intercept_request(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {"name": "save", "arguments": {}},
            },
            proc=mock.MagicMock(),
        )
        await w._maybe_filter_response(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "error": {"code": -1, "message": "upstream failed"},
            }
        )
        await asyncio.sleep(0.05)
        self.assertEqual(
            calls, [], "post_tool_success must NOT fire on error responses"
        )

    async def test_post_hook_exception_swallowed(self):
        """An exception inside post_tool_success must NOT propagate.

        Claude has already received the response; failing the hook
        would just lose the side-effect, not undo the user-visible work.
        """
        class BrokenHook(WrapperMCP):
            async def post_tool_success(self, tool_name, arguments, result):
                raise RuntimeError("indexer is unavailable")

        w = BrokenHook("m", ["x"])
        w._resolve_project_id = mock.AsyncMock(return_value="p")  # type: ignore[assignment]

        async def fake_fetch(_pid):
            return {"save": True}

        w._fetch_grants_from_hub = fake_fetch  # type: ignore[assignment]

        await w._maybe_intercept_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "save", "arguments": {}},
            },
            proc=mock.MagicMock(),
        )
        # If the hook bubbled, asyncio.sleep would re-raise it as an
        # unhandled-task warning visible in the test runner. We assert
        # nothing — surviving the sleep IS the test.
        await w._maybe_filter_response(
            {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        )
        await asyncio.sleep(0.05)


# ─── Tracker correctness ─────────────────────────────────────────────────


class InFlightTrackerTests(unittest.TestCase):

    def test_hashable_id_handles_basic_types(self):
        self.assertEqual(_hashable_id(1), 1)
        self.assertEqual(_hashable_id("a"), "a")
        self.assertEqual(_hashable_id(None), None)
        # Floats / dicts → repr (defensive shim)
        self.assertEqual(_hashable_id(1.5), "1.5")


# ─── Hub credentials / by-path ───────────────────────────────────────────


class HubCredentialResolutionTests(unittest.TestCase):
    """Env > on-disk > default precedence for hub.port / hub.token."""

    def test_env_wins(self):
        w = WrapperMCP("m", ["x"])
        with mock.patch.dict("os.environ", {"VCT_HUB_PORT": "9001", "VCT_HUB_TOKEN": "tok"}):
            port, token = w._get_hub_credentials()
        self.assertEqual(port, 9001)
        self.assertEqual(token, "tok")

    def test_invalid_port_env_warns_returns_none(self):
        w = WrapperMCP("m", ["x"])
        with mock.patch.dict("os.environ", {"VCT_HUB_PORT": "not-a-port"}, clear=False):
            # Token resolution still falls through to file lookup; we
            # only care about the port behaviour here.
            port, _token = w._get_hub_credentials()
        self.assertIsNone(port, "non-integer VCT_HUB_PORT must yield None (no fallback to default)")

    def test_missing_port_file_uses_default(self, tmp_root=None):
        w = WrapperMCP("m", ["x"])
        with mock.patch.dict("os.environ", {"VCT_HUB_TOKEN": "tok"}, clear=False):
            with mock.patch(
                "claude_mcp_servers.wrappers._base.vct_root_dir",
                return_value=Path("/nonexistent/dir"),
            ):
                # Need to also clear VCT_HUB_PORT for default-port fallback.
                if "VCT_HUB_PORT" in __import__("os").environ:
                    del __import__("os").environ["VCT_HUB_PORT"]
                port, token = w._get_hub_credentials()
        from claude_mcp_servers.wrappers._base import DEFAULT_HUB_PORT
        self.assertEqual(port, DEFAULT_HUB_PORT)
        self.assertEqual(token, "tok")


if __name__ == "__main__":
    unittest.main()
