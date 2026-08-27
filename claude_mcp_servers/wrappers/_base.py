# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Wrapper-MCP base class — proxies an upstream stdio MCP and filters
its tool surface per-project allowlist (Phase 1.2 of the diagrams-
integration plan, 2026-05-24).

Architecture
------------
A wrapper MCP is itself a stdio MCP from Claude's perspective. Internally
it:

  1. Spawns the upstream MCP as a child subprocess (stdio).
  2. Resolves the per-project tool allowlist from the launcher hub
     (`GET /api/v1/projects/{id}/mcp-tool-grants/{mcp_name}`).
  3. Caches the allowlist in-process (60 s TTL by default).
  4. Intercepts JSON-RPC ``tools/list`` responses on the way OUT to
     Claude and filters them to allowed tools only.
  5. Intercepts JSON-RPC ``tools/call`` requests on the way IN to the
     upstream and rejects (with MCP error -32601 "Method not found") any
     call for a tool that isn't allowed.
  6. Passes everything else through unchanged.

Why a raw JSON-RPC proxy and NOT FastMCP/the MCP server SDK
-----------------------------------------------------------
FastMCP forces you to register tools at startup time, which means we'd
have to call ``tools/list`` on the upstream, build a dispatch table, and
re-publish — that's three round-trips per session-start plus an
incompatible-with-streaming-responses layer of indirection. The raw
JSON-RPC framing (newline-delimited JSON, no MCP-specific schemas) is
ideal for what we need: stateless pass-through of well-formed JSON-RPC
2.0 messages with a tiny inspect-and-mutate window.

Failsafe modes
--------------
The wrapper MUST keep working when the launcher is OFF or the hub is
unreachable — Claude Code can be launched outside the launcher's
control (CLI mode, scripts, CI). Two soft-fail paths:

  * ``VCT_PROJECT_ID`` is unset AND the hub's ``by-path`` lookup
    fails → **"no project — allow all tools"** mode. Logs a WARNING to
    stderr but does NOT block tool use.
  * Hub HTTP call fails (connection refused / 5xx / timeout) →
    **"hub-down — allow all tools"** mode. Same fallback.

In both modes the upstream MCP is exposed verbatim. The user opts into
filtering by running the launcher; failing closed (block all tools) would
brick MCPs for non-launcher users, which the plan §6 explicitly avoids
("default state for existing projects is identical to pre-Phase-4
behaviour").

Subclass hooks
--------------
:meth:`WrapperMCP.validate_tool_call` — per-call validation (e.g.
scoped-path enforcement for the mermaid_proxy). Returns a string error
to reject; ``None`` to allow.

:meth:`WrapperMCP.post_tool_success` — async side-effect after a
successful tool call (e.g. trigger the diagram indexer). Errors are
logged but do not affect the response.

Future seam (NOT shipped in Phase 1.2)
--------------------------------------
The plan calls for the hub to broadcast grant changes so the wrapper
can invalidate the 60s cache early. We ship polling-only here (refetch
every 60s) and leave a ``_on_grant_broadcast()`` hook stub for when the
broadcast lands. See §4 Risk 6 in the plan.

Cross-OS
--------
* ``asyncio.create_subprocess_exec`` (NOT ``shell=True``) — avoids the
  shell-injection class entirely.
* npm wrappers (mermaid_proxy etc.) resolve ``npx`` via
  :func:`shutil.which`, which handles ``npx.cmd`` on Windows.
* The script-level ``python -m claude_mcp_servers.wrappers.mermaid_proxy``
  entry point works identically on Linux / macOS / Windows.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import aiohttp

# Local imports — both relative (when used as a package) and direct (when
# the wrapper is invoked as a script via `python -m ...`). Same pattern
# weaviate_mcp/server.py uses for `chunking`.
try:
    from vco_lib.paths import vct_root_dir
except ImportError:  # pragma: no cover — only triggered when sys.path is wrong
    _parent_dir = str(Path(__file__).resolve().parent.parent.parent)
    if _parent_dir not in sys.path:
        sys.path.insert(0, _parent_dir)
    from vco_lib.paths import vct_root_dir


logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────

#: Default hub port. Matches DEFAULT_HUB_PORT in
#: ``vco_lib/project_config.py`` and the Rust ``vct-hub::server`` constant.
DEFAULT_HUB_PORT: int = 7700

#: Default allowlist TTL in seconds. 60s is the plan's stated cadence
#: (§4 Risk 6). When the hub broadcast lands we'll invalidate earlier;
#: until then this is the staleness window the user pays for.
DEFAULT_ALLOWLIST_TTL_SECONDS: int = 60

#: Connect/read timeouts for hub calls. Localhost should answer in
#: sub-ms; 2s/5s leaves headroom for a slow / paging system without
#: making first-call hang.
_HUB_CONNECT_TIMEOUT: float = 2.0
_HUB_READ_TIMEOUT: float = 5.0

#: HTTP statuses that constitute a PROVABLE credential refusal — the only
#: trigger for the stale-env-token fallback (v0.2.91, WP-D item 4). 401 =
#: the bearer matched nothing; 403 = the bearer is real but refused on
#: this route (the global-token-on-``/env`` shape). Anything else is not
#: a credential problem and never triggers a retry.
_AUTH_REFUSAL_STATUSES: frozenset[int] = frozenset({401, 403})


def _retry_answer_is_definitive(status: int) -> bool:
    """May a stale-env RETRY's answer be adopted (and the pin latched off)?

    Only when it PROVES the fallback credential was accepted: ``2xx``, or
    ``404`` — which the hub answers only after its auth middleware
    accepted the bearer ("no project for this path" / "no grants row"), so
    it is a post-auth answer just like a 200.

    Everything else proves nothing about the credential. v0.2.91 wave-3
    (MINOR-1): before this, any non-401/403 retry answer was adopted, so a
    5xx following a 401 permanently latched the env pin off for this
    long-lived process and logged the definitive line on no evidence.
    """
    return 200 <= status < 300 or status == 404

#: The ONE definitive line emitted after a stale env token is overridden.
#: Byte-identical to ``vco_lib.project_config.STALE_ENV_TOKEN_MESSAGE``
#: and to the sh / ps1 / Rust mirrors (locked by
#: tests/test_stale_env_token_parity_v0291.py).
STALE_ENV_TOKEN_MESSAGE: str = (
    "stale VCT_HUB_TOKEN in env overridden by on-disk hub.token — "
    "run `unset VCT_HUB_TOKEN` or open a new shell"
)


# ─── Cached state shapes ──────────────────────────────────────────────────


@dataclass
class _AllowlistCacheEntry:
    """One slot of the allowlist cache.

    ``failsafe_mode`` lets the cache REMEMBER that we entered allow-all
    mode without re-spamming the hub every 60s for an unreachable
    service. Only when the TTL expires do we retry the hub.
    """
    grants: dict[str, bool]
    expires_at: float
    failsafe_mode: bool = False


# ─── Wrapper base class ───────────────────────────────────────────────────


class WrapperMCP:
    """Base class for wrapper MCPs.

    Subclass and pass the upstream argv + mcp_name. Override
    :meth:`validate_tool_call` and :meth:`post_tool_success` for per-MCP
    logic (e.g. scoped-path enforcement, indexer hooks).

    Args:
        mcp_name: The MCP name the launcher knows it by (e.g. ``"mermaid"``).
            Used as the hub-route segment and the cache key.
        upstream_argv: The exact argv to spawn the upstream MCP. The
            first element should be the executable; remaining items are
            args. We never run via shell — pass as a list.
        allowlist_cache_ttl: Seconds to cache the hub's allowlist
            response. 60 s matches the plan; tests pass 0 for synchronous
            invalidation.
    """

    def __init__(
        self,
        mcp_name: str,
        upstream_argv: list[str],
        allowlist_cache_ttl: int = DEFAULT_ALLOWLIST_TTL_SECONDS,
    ) -> None:
        if not mcp_name:
            raise ValueError("mcp_name must be non-empty")
        if not upstream_argv:
            raise ValueError("upstream_argv must be non-empty")

        self.mcp_name = mcp_name
        self.upstream_argv = list(upstream_argv)
        self.allowlist_cache_ttl = max(0, int(allowlist_cache_ttl))

        # Allowlist cache. Single-slot per process because each wrapper
        # is one MCP, one project at a time.
        self._allowlist_cache: Optional[_AllowlistCacheEntry] = None
        self._allowlist_lock = asyncio.Lock()

        # Hub-discovery cache (port + token). Re-read when the token
        # file changes or after a 401 forces invalidation.
        self._hub_port: Optional[int] = None
        self._hub_token: Optional[str] = None

        # v0.2.91 (WP-D item 4): latched TRUE once the hub has PROVABLY
        # refused this process's ``$VCT_HUB_TOKEN`` and the on-disk token
        # worked instead. A wrapper is long-lived (it outlives the shell
        # that spawned it), so without the latch every subsequent
        # invalidation would re-prefer the same dead env value and the
        # wrapper would 401 for its whole lifetime. Never set when
        # ``VCT_HUB_TOKEN_STRICT=1``.
        self._ignore_env_hub_token: bool = False
        # One definitive stderr/log line per process, not per request.
        self._stale_env_token_warned: bool = False

        # aiohttp session — created lazily on first use, closed on
        # subprocess shutdown.
        self._http_session: Optional[aiohttp.ClientSession] = None

    # ─── Subclass hooks ──────────────────────────────────────────────

    def validate_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        """Override to add per-call validation (e.g. scoped-path check).

        Return ``None`` to allow the call; return an error message
        string to reject it. The base class ALREADY enforces the
        allowlist before invoking this method, so subclasses can
        focus on argument-shape validation.

        The error string is returned to Claude verbatim inside the
        MCP error envelope.
        """
        return None

    async def post_tool_success(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Override to react to a successful tool call.

        Runs AFTER the upstream returned a successful result and AFTER
        the result was forwarded back to Claude. Errors raised here
        are logged but do NOT mutate the response (Claude already got
        the result; failing the hook would just lose the side-effect).

        Used by mermaid_proxy to enqueue the just-saved file into the
        diagram indexer (Phase 1.5.A — sibling).
        """

    # ─── Public entry point ──────────────────────────────────────────

    async def run(self) -> None:
        """Spawn the upstream and pump stdio.

        Runs forever until the upstream exits or our parent stdin
        closes (Claude disconnected). Clean shutdown: closes the
        upstream subprocess and the aiohttp session before returning.
        """
        proc = await asyncio.create_subprocess_exec(
            *self.upstream_argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # No `shell=True` — explicit argv list, no injection surface.
        )
        logger.info(
            "wrapper(%s): spawned upstream pid=%s argv=%s",
            self.mcp_name, proc.pid, self.upstream_argv,
        )

        # Three concurrent pumps: stdin→upstream, upstream→stdout, stderr→stderr.
        # gather() returns when ALL exit; we cancel siblings on first exit
        # so a half-broken upstream tears down cleanly.
        try:
            done, pending = await asyncio.wait(
                {
                    asyncio.create_task(self._pump_client_to_upstream(proc)),
                    asyncio.create_task(self._pump_upstream_to_client(proc)),
                    asyncio.create_task(self._pump_stderr(proc)),
                },
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            # Drain any remaining cancellations cleanly.
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            # Reap the upstream subprocess.
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            if self._http_session is not None:
                await self._http_session.close()
                self._http_session = None
            logger.info(
                "wrapper(%s): upstream exited rc=%s",
                self.mcp_name, proc.returncode,
            )

    # ─── Stdio pumps ─────────────────────────────────────────────────

    async def _pump_client_to_upstream(
        self,
        proc: asyncio.subprocess.Process,
    ) -> None:
        """Read JSON-RPC messages from our stdin, filter, forward to upstream."""
        assert proc.stdin is not None
        reader = await _stdin_reader()
        while True:
            line = await reader.readline()
            if not line:
                # Claude disconnected — close the upstream's stdin so it
                # exits its own read loop.
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                return
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Pass through malformed bytes verbatim — let upstream
                # decide. Logging at DEBUG to avoid noise.
                logger.debug(
                    "wrapper(%s): non-JSON line client→upstream (passing through)",
                    self.mcp_name,
                )
                proc.stdin.write(line)
                await proc.stdin.drain()
                continue

            handled = await self._maybe_intercept_request(msg, proc)
            if handled:
                continue
            # Pass-through case — write the original line so we preserve
            # whatever framing/whitespace the client sent.
            proc.stdin.write(line)
            await proc.stdin.drain()

    async def _pump_upstream_to_client(
        self,
        proc: asyncio.subprocess.Process,
    ) -> None:
        """Read JSON-RPC messages from upstream, filter, forward to stdout."""
        assert proc.stdout is not None
        out = sys.stdout.buffer
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.debug(
                    "wrapper(%s): non-JSON line upstream→client (passing through)",
                    self.mcp_name,
                )
                out.write(line)
                out.flush()
                continue

            filtered = await self._maybe_filter_response(msg)
            out.write((json.dumps(filtered) + "\n").encode("utf-8"))
            out.flush()

    async def _pump_stderr(
        self,
        proc: asyncio.subprocess.Process,
    ) -> None:
        """Forward upstream stderr to our stderr with a prefix.

        Claude Code shows MCP stderr in its logs; tagging it makes
        triage clear when multiple wrappers run side-by-side.
        """
        assert proc.stderr is not None
        prefix = f"[upstream {self.mcp_name}] ".encode("utf-8")
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            sys.stderr.buffer.write(prefix + line)
            sys.stderr.buffer.flush()

    # ─── Interception logic ──────────────────────────────────────────

    async def _maybe_intercept_request(
        self,
        msg: dict[str, Any],
        proc: asyncio.subprocess.Process,
    ) -> bool:
        """Inspect a client→upstream message; return True if handled (don't forward).

        Returning False means the caller should forward the message as
        normal pass-through.
        """
        method = msg.get("method")
        if method != "tools/call":
            return False

        params = msg.get("params") or {}
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        msg_id = msg.get("id")

        if not isinstance(tool_name, str):
            # Malformed tools/call — let upstream complain so the user
            # sees a real error path. Don't try to be helpful here.
            return False

        # Allowlist check FIRST (mechanical, cheap).
        allowed = await self._is_tool_allowed(tool_name)
        if not allowed:
            await self._write_error_response(
                msg_id,
                code=-32601,
                message=(
                    f"tool '{tool_name}' is not allowed for this project "
                    f"(MCP '{self.mcp_name}'). Enable it in the launcher's "
                    f"Permissions tab → MCP Tools."
                ),
            )
            return True

        # Per-call validation SECOND (subclass hook).
        err = self.validate_tool_call(tool_name, arguments)
        if err is not None:
            await self._write_error_response(
                msg_id,
                code=-32602,  # Invalid params — argument validation failed.
                message=err,
            )
            return True

        # Allowed AND validated — forward to upstream. Side-effect
        # hooks fire after the upstream replies (handled in
        # :meth:`_maybe_filter_response` via the in-flight tracker).
        self._track_in_flight_call(msg_id, tool_name, arguments)
        return False

    async def _maybe_filter_response(
        self,
        msg: dict[str, Any],
    ) -> dict[str, Any]:
        """Inspect upstream→client; filter tools/list, dispatch post-hooks."""
        # tools/list response: filter to allowed tools.
        result = msg.get("result")
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            tools = result["tools"]
            # Only filter the SHAPE that looks like a tools/list response
            # (a list where each entry is an object with a "name").
            if tools and all(isinstance(t, dict) and "name" in t for t in tools):
                allowed_names = await self._resolve_allowlist()
                filtered = [
                    t for t in tools
                    if allowed_names is None  # failsafe mode — let everything through
                    or allowed_names.get(t["name"], False)
                ]
                # Mutate a SHALLOW COPY so callers reading the original
                # msg dict still see the unfiltered list.
                new_result = dict(result)
                new_result["tools"] = filtered
                new_msg = dict(msg)
                new_msg["result"] = new_result
                return new_msg

        # tools/call response — fire the post-hook if this was an
        # in-flight call we tracked.
        msg_id = msg.get("id")
        tracked = self._take_in_flight_call(msg_id)
        if tracked is not None:
            tool_name, arguments = tracked
            # Only fire post-hook on SUCCESS (no "error" key, has result).
            if "error" not in msg and isinstance(msg.get("result"), dict):
                # Side-effect dispatcher: run in background so a slow
                # hook doesn't delay Claude's view of the response.
                asyncio.create_task(
                    self._safe_post_tool_success(tool_name, arguments, msg["result"])
                )

        return msg

    async def _safe_post_tool_success(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Run :meth:`post_tool_success` and swallow exceptions.

        Hook errors are diagnostics, not user-facing; they log but
        never bubble up because Claude already saw the response.
        """
        try:
            await self.post_tool_success(tool_name, arguments, result)
        except Exception as e:  # noqa: BLE001 — hook errors are diagnostic only
            logger.warning(
                "wrapper(%s): post_tool_success(%s) raised: %s",
                self.mcp_name, tool_name, e,
            )

    async def _write_error_response(
        self,
        msg_id: Any,
        code: int,
        message: str,
    ) -> None:
        """Write a JSON-RPC 2.0 error response directly to our stdout."""
        envelope = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": code,
                "message": message,
            },
        }
        sys.stdout.buffer.write((json.dumps(envelope) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()

    # ─── In-flight call tracker (for post_tool_success dispatch) ─────

    _IN_FLIGHT_KEY = "_wrapper_in_flight_calls"

    def _track_in_flight_call(
        self,
        msg_id: Any,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        if not hasattr(self, self._IN_FLIGHT_KEY):
            setattr(self, self._IN_FLIGHT_KEY, {})
        getattr(self, self._IN_FLIGHT_KEY)[_hashable_id(msg_id)] = (tool_name, arguments)

    def _take_in_flight_call(
        self,
        msg_id: Any,
    ) -> tuple[str, dict[str, Any]] | None:
        if not hasattr(self, self._IN_FLIGHT_KEY):
            return None
        return getattr(self, self._IN_FLIGHT_KEY).pop(_hashable_id(msg_id), None)

    # ─── Allowlist resolution ────────────────────────────────────────

    async def _is_tool_allowed(self, tool_name: str) -> bool:
        """Lookup allowlist for tool_name. Failsafe → True."""
        grants = await self._resolve_allowlist()
        if grants is None:
            return True  # failsafe: hub down OR no project bound
        return grants.get(tool_name, False)

    async def _resolve_allowlist(self) -> dict[str, bool] | None:
        """Returns the allowlist dict or None for failsafe-allow-all mode.

        Caches the result for ``allowlist_cache_ttl`` seconds.
        """
        async with self._allowlist_lock:
            now = time.monotonic()
            cached = self._allowlist_cache
            if cached is not None and cached.expires_at > now:
                return None if cached.failsafe_mode else cached.grants

            project_id = await self._resolve_project_id()
            if project_id is None:
                # R3 (code-review 2026-05-25): the no-project failsafe
                # opens the full upstream tool surface for the entire
                # cache window. Shorten the TTL aggressively (10 s
                # instead of the full allowlist_cache_ttl) AND escalate
                # the log to WARNING every miss — a user who hasn't
                # registered their project in the launcher should not
                # silently get an open allowlist for ~60 s after each
                # cache miss. Re-check the hub on every call after the
                # short cache expires so registration takes effect
                # within seconds of completing.
                no_project_ttl = min(10, self.allowlist_cache_ttl)
                logger.warning(
                    "wrapper(%s): no VCT_PROJECT_ID and hub by-path failed; "
                    "running in allow-all failsafe mode for %ds. Register "
                    "the project via the launcher to enable per-tool grants.",
                    self.mcp_name, no_project_ttl,
                )
                self._allowlist_cache = _AllowlistCacheEntry(
                    grants={},
                    expires_at=now + no_project_ttl,
                    failsafe_mode=True,
                )
                return None

            try:
                grants = await self._fetch_grants_from_hub(project_id)
            except _HubUnreachable as e:
                logger.warning(
                    "wrapper(%s): hub unreachable (%s); running in allow-all failsafe mode",
                    self.mcp_name, e,
                )
                self._allowlist_cache = _AllowlistCacheEntry(
                    grants={},
                    expires_at=now + self.allowlist_cache_ttl,
                    failsafe_mode=True,
                )
                return None

            self._allowlist_cache = _AllowlistCacheEntry(
                grants=grants,
                expires_at=now + self.allowlist_cache_ttl,
                failsafe_mode=False,
            )
            return grants

    # ─── Hub I/O ─────────────────────────────────────────────────────

    async def _resolve_project_id(self) -> str | None:
        """Resolve the project ID from env or via hub by-path lookup."""
        env_id = os.environ.get("VCT_PROJECT_ID", "").strip()
        if env_id:
            return env_id

        cwd = os.getcwd()
        try:
            return await self._fetch_project_id_by_path(cwd)
        except _HubUnreachable:
            return None

    async def _fetch_project_id_by_path(self, abs_path: str) -> str | None:
        """Resolve via GET /api/v1/projects/by-path?path=<abs_path>."""
        session = await self._get_http_session()
        port, token = self._get_hub_credentials()
        if port is None or token is None:
            return None
        url = f"http://127.0.0.1:{port}/api/v1/projects/by-path"

        async def attempt(bearer: str) -> tuple[int, Any]:
            # Body-parsing set is byte-identical to the pre-v0.2.91 flow:
            # 401 / 404 / 5xx short-circuit before `resp.json()`; every
            # other status (403 included) parses exactly as it did.
            async with session.get(
                url,
                params={"path": abs_path},
                headers={"Authorization": f"Bearer {bearer}"},
                timeout=aiohttp.ClientTimeout(
                    connect=_HUB_CONNECT_TIMEOUT,
                    total=_HUB_READ_TIMEOUT,
                ),
            ) as resp:
                if resp.status == 401 or resp.status == 404 or resp.status >= 500:
                    return resp.status, None
                return resp.status, await resp.json()

        try:
            status, body = await attempt(token)
            status, body = await self._maybe_retry_with_disk_token(
                status, body, attempt
            )
            if status == 401:
                # Token rotated — drop our cached creds and let next
                # call re-read.
                self._hub_port = None
                self._hub_token = None
                raise _HubUnreachable("hub returned 401 (token stale)")

            if status == 404:
                return None  # No project registered for this cwd.
            if status >= 500:
                raise _HubUnreachable(f"hub by-path returned {status}")
            # Modules_api returns ProjectSummary with `id` field.
            pid = body.get("id") or body.get("project_id")
            return str(pid) if pid else None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise _HubUnreachable(f"hub by-path call failed: {e}") from e

    async def _fetch_grants_from_hub(
        self,
        project_id: str,
    ) -> dict[str, bool]:
        """Fetch the per-project tool allowlist for this MCP."""
        session = await self._get_http_session()
        port, token = self._get_hub_credentials()
        if port is None or token is None:
            raise _HubUnreachable("hub credentials unavailable")
        url = (
            f"http://127.0.0.1:{port}/api/v1/projects/"
            f"{project_id}/mcp-tool-grants/{self.mcp_name}"
        )

        async def attempt(bearer: str) -> tuple[int, Any]:
            # Body-parsing set is byte-identical to the pre-v0.2.91 flow.
            async with session.get(
                url,
                headers={"Authorization": f"Bearer {bearer}"},
                timeout=aiohttp.ClientTimeout(
                    connect=_HUB_CONNECT_TIMEOUT,
                    total=_HUB_READ_TIMEOUT,
                ),
            ) as resp:
                if resp.status == 401 or resp.status == 404 or resp.status >= 500:
                    return resp.status, None
                return resp.status, await resp.json()

        try:
            status, body = await attempt(token)
            status, body = await self._maybe_retry_with_disk_token(
                status, body, attempt
            )
            if status == 401:
                self._hub_port = None
                self._hub_token = None
                raise _HubUnreachable("hub returned 401 (token stale)")
            if status == 404:
                # No grants table OR no project — treat as failsafe.
                raise _HubUnreachable("hub returned 404")
            if status >= 500:
                raise _HubUnreachable(f"hub grants returned {status}")
            grants = body.get("grants") or {}
            if not isinstance(grants, dict):
                raise _HubUnreachable(
                    f"hub grants response malformed: {type(grants).__name__}"
                )
            # Coerce values to bool (defensive — JSON sometimes
            # carries integers from older hub versions).
            return {str(k): bool(v) for k, v in grants.items()}
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise _HubUnreachable(f"hub grants call failed: {e}") from e

    # ─── Stale-env hub-token fallback (v0.2.91, WP-D item 4) ─────────
    #
    # MUST MATCH `vco_lib/project_config.py::_stale_env_token_fallback`
    # (the Python SSOT). We cannot call it directly here for the same
    # reason `_get_hub_credentials` re-implements `_discover_hub`: this
    # module is deliberately import-light + asyncio-friendly and must
    # keep working when vco_lib is not importable from the wrapper's
    # interpreter. The rules are locked by
    # tests/test_stale_env_token_parity_v0291.py.

    def _stale_env_token_fallback(self) -> str | None:
        """The on-disk token to retry with, or ``None`` to leave alone.

        Rules (identical to the SSOT): strict pin set → None; no env
        token → None; no readable on-disk token → None; on-disk equals
        env → None. Wrappers only ever hit GLOBAL-token routes
        (``/projects/by-path``, ``/projects/{id}/mcp-tool-grants/...``
        — neither is a per-project-token route, see the hub's
        ``auth.rs::per_project_token_route``), so there is no scoped
        variant to resolve here.
        """
        # Trimmed comparison to the literal "1" — the SSOT's spelling, so a
        # `VCT_HUB_TOKEN_STRICT=1\n` from a here-doc means the same thing in
        # every mirror.
        if os.environ.get("VCT_HUB_TOKEN_STRICT", "").strip() == "1":
            return None
        env_tok = os.environ.get("VCT_HUB_TOKEN", "").strip()
        if not env_tok:
            return None
        try:
            disk_tok = (
                (vct_root_dir() / "hub.token").read_text(encoding="utf-8").strip()
            )
        except (FileNotFoundError, OSError):
            return None
        if not disk_tok or disk_tok == env_tok:
            return None
        return disk_tok

    async def _maybe_retry_with_disk_token(
        self,
        status: int,
        body: Any,
        attempt: Any,
    ) -> tuple[int, Any]:
        """One bounded retry with the on-disk token after a provable refusal.

        Returns the retry's ``(status, body)`` only when it PROVES the
        fallback credential was accepted (see
        :func:`_retry_answer_is_definitive`), else the original pair
        verbatim — so every non-stale-token path stays byte-compatible
        with the pre-v0.2.91 flow. On adoption the env token is latched
        OFF for the rest of this (long-lived) process and one definitive
        line is logged.

        Nothing loops: at most one extra request per hub call.
        """
        if status not in _AUTH_REFUSAL_STATUSES:
            return status, body
        fallback = self._stale_env_token_fallback()
        if fallback is None:
            return status, body
        try:
            retry_status, retry_body = await attempt(fallback)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # The extra attempt could not complete — keep today's path.
            return status, body
        if not _retry_answer_is_definitive(retry_status):
            return status, body
        # Success: the env pin was the problem. Stop presenting it.
        self._ignore_env_hub_token = True
        self._hub_port = None
        self._hub_token = None
        if not self._stale_env_token_warned:
            self._stale_env_token_warned = True
            logger.warning("wrapper(%s): %s", self.mcp_name, STALE_ENV_TOKEN_MESSAGE)
        return retry_status, retry_body

    def _get_hub_credentials(self) -> tuple[int | None, str | None]:
        """Resolve (port, token) from env > on-disk > defaults.

        Mirrors :func:`vco_lib.project_config._discover_hub`. We don't
        import that helper because:
          * it's synchronous (requests-based, blocking) — we need to be
            asyncio-friendly here.
          * its caching semantics are tuned for short-lived script
            invocations (5 s TTL); the wrapper is long-lived and tracks
            its own freshness via the allowlist cache.

        Refresh-on-401: the caller invalidates ``_hub_port`` /
        ``_hub_token`` on 401 so the next call re-reads.
        """
        if self._hub_port is not None and self._hub_token is not None:
            return self._hub_port, self._hub_token

        # Port: env > file > default.
        #
        # F-8 corrupt-input contract — MUST MATCH the 4th mirror of the
        # triplet fixed by W2-E:
        #   * vco_lib/project_config.py::_discover_hub
        #   * templates/scripts/vct_project_config.sh::hub_port
        #   * templates/scripts/vct_project_config.ps1::Get-HubPort
        # A non-integer ``VCT_HUB_PORT``, a non-integer ``hub.port`` file, or
        # an unreadable ``hub.port`` (perm-denied) must NOT yield ``None`` —
        # the port has a sane default (7700). Warn once, fall through to the
        # default. Only a truly ABSENT file is the silent default path (the
        # normal env-only / dev case). This keeps all FOUR resolvers
        # identical on corrupt port input: warn + default, never a partial
        # resolution that silently disables the hub.
        port_env = os.environ.get("VCT_HUB_PORT", "").strip()
        if port_env:
            try:
                port: int | None = int(port_env)
            except ValueError:
                logger.warning(
                    "wrapper(%s): VCT_HUB_PORT=%r is not an integer; "
                    "using default %d",
                    self.mcp_name, port_env, DEFAULT_HUB_PORT,
                )
                port = DEFAULT_HUB_PORT
        else:
            port_file = vct_root_dir() / "hub.port"
            try:
                raw = port_file.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                port = DEFAULT_HUB_PORT
            except OSError as e:
                logger.warning(
                    "wrapper(%s): cannot read %s: %s; using default %d",
                    self.mcp_name, port_file, e, DEFAULT_HUB_PORT,
                )
                port = DEFAULT_HUB_PORT
            else:
                try:
                    port = int(raw) if raw else DEFAULT_HUB_PORT
                except ValueError:
                    logger.warning(
                        "wrapper(%s): %s contains non-integer content; "
                        "using default %d",
                        self.mcp_name, port_file, DEFAULT_HUB_PORT,
                    )
                    port = DEFAULT_HUB_PORT

        # Token: env > file > no-token (hub unreachable).
        #
        # F-8 corrupt-input contract — MUST MATCH the 4th mirror: the token
        # has NO sane default, so an absent/empty/unreadable ``hub.token`` is
        # not "warn-and-default" but "no token" → the hub is genuinely
        # unreachable. We return ``token = None`` (the caller's env-fallback /
        # unreachable path) and — for the UNREADABLE case only — warn first so
        # the diagnostic shape matches the sibling resolvers. The read failure
        # NEVER crashes with a raw OSError traceback.
        #
        # v0.2.91 (WP-D item 4): `_ignore_env_hub_token` latches TRUE
        # only after the hub PROVABLY refused this env value AND the
        # on-disk token worked. From then on this long-lived process
        # skips the env pin entirely — otherwise every invalidation
        # would re-prefer the dead value (the seam this fix closes).
        token_env = os.environ.get("VCT_HUB_TOKEN", "").strip()
        if token_env and self._ignore_env_hub_token:
            token_env = ""
        if token_env:
            token: str | None = token_env
        else:
            token_file = vct_root_dir() / "hub.token"
            try:
                token = token_file.read_text(encoding="utf-8").strip() or None
            except FileNotFoundError:
                token = None
            except OSError as e:
                logger.warning(
                    "wrapper(%s): cannot read %s: %s; treating as no token",
                    self.mcp_name, token_file, e,
                )
                token = None

        if port is not None and token:
            self._hub_port = port
            self._hub_token = token

        return port, token

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session


# ─── Internal helpers ─────────────────────────────────────────────────────


class _HubUnreachable(Exception):
    """Raised inside the wrapper when a hub call fails. Always caught by
    the caller and translated into ``failsafe_mode=True`` cache state."""


def _hashable_id(msg_id: Any) -> Any:
    """JSON-RPC ids are str | int | None. The dict tracker just needs
    something hashable; we coerce defensively in case an upstream
    decides to use a float."""
    if isinstance(msg_id, (str, int, type(None))):
        return msg_id
    return repr(msg_id)


# ─── Stdin reader factory ────────────────────────────────────────────────


async def _stdin_reader() -> "_StdinReader":
    """Return a stdin reader compatible with the current event loop.

    POSIX (SelectorEventLoop): wraps sys.stdin via connect_read_pipe →
    real asyncio.StreamReader.

    Windows (ProactorEventLoop, default on 3.8+): connect_read_pipe
    raises NotImplementedError for stdin (console handles need
    overlapped I/O bridging). Falls back to a thread-pool reader
    (R5 from code review 2026-05-25) — sys.stdin.readline runs in
    the default executor; the coroutine wraps awaitability.

    Both paths expose the same .readline() coroutine surface so callers
    don't branch on platform.
    """
    if sys.platform == "win32":
        return _ThreadedStdinReader()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    try:
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    except NotImplementedError:
        # Defensive: if a future event-loop policy lacks pipe support
        # even on POSIX, fall through to the threaded reader.
        return _ThreadedStdinReader()
    return _StreamReaderWrapper(reader)


class _StreamReaderWrapper:
    """Thin wrapper to give asyncio.StreamReader the same surface as
    _ThreadedStdinReader (just .readline()). Lets the caller code
    not care which backend it got."""

    def __init__(self, reader: asyncio.StreamReader):
        self._reader = reader

    async def readline(self) -> bytes:
        return await self._reader.readline()


class _ThreadedStdinReader:
    """Asyncio-compatible reader for Windows-style event loops where
    `connect_read_pipe(sys.stdin)` is unavailable. Runs the blocking
    `sys.stdin.buffer.readline()` in the default executor so the event
    loop can interleave other tasks.

    EOF: when readline returns empty bytes, that's the upstream-disconnect
    signal — propagated identically to the POSIX path.
    """

    async def readline(self) -> bytes:
        loop = asyncio.get_running_loop()
        # sys.stdin.buffer is the underlying binary stream; .readline()
        # blocks the executor thread until a newline or EOF. Returning
        # b'' on EOF matches asyncio.StreamReader.readline() semantics.
        return await loop.run_in_executor(None, sys.stdin.buffer.readline)


# Type alias for the union — the caller treats both as "a thing with
# an async readline returning bytes". Either backend's interface is
# minimal enough that a Protocol/ABC would be overkill.
_StdinReader = "_StreamReaderWrapper | _ThreadedStdinReader"


__all__ = [
    "WrapperMCP",
    "DEFAULT_ALLOWLIST_TTL_SECONDS",
    "DEFAULT_HUB_PORT",
    "STALE_ENV_TOKEN_MESSAGE",
]
