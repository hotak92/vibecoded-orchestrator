# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Async HTTP client for the vct-rl-reranker container (paid module).

The client is designed to be **always-safe to construct** even when
the paid container isn't installed. Free-tier behavior:

  * ``RL_SERVER_URL`` / ``RL_SERVER_PORT`` env unset → "disabled mode":
    ``cache_nodes`` returns the input nodes truncated to ``top_k``;
    ``rl_update`` is a no-op; ``health`` returns
    ``HealthResponse(ok=False, model='disabled')``.
  * Env set but connection refused / 5xx → per-call fallback to
    "no rerank" (same as disabled mode) but the instance is NOT
    permanently disabled; subsequent calls retry.

This preserves the v0.2.x ``feature_enabled('rl_retrieval')`` gate
semantics: free-tier users get plain Weaviate cosine ordering;
nothing crashes when the container is down.

Constructor takes ``text_dim`` (for query_emb sanity-checking) and
``active_embedding`` (forwarded on every request so the server can
log/tag the embedding source).
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

try:
    import httpx
except ImportError:  # pragma: no cover (httpx is a hard dep)
    httpx = None  # type: ignore[assignment]

from .schemas import (
    CacheNodesRequest,
    CacheNodesResponse,
    HealthResponse,
    NodeInput,
    RankedNode,
    RLUpdateRequest,
    RLUpdateResponse,
)

logger = logging.getLogger(__name__)

# Default request timeout — tight, to never block Claude's response.
# Matches the existing ``aiohttp.ClientTimeout(total=3.0)`` in
# weaviate_mcp/server.py::_rl_cache_and_rerank.
_DEFAULT_TIMEOUT = 3.0
_DEFAULT_HEALTH_TIMEOUT = 1.0


def _deprecation_warning() -> Optional[str]:
    """Build a one-line deprecation banner for inclusion in rerank-adjacent
    responses (v0.2.31 module-deprecation surface, Layer 2 — Claude-visible).

    Reads the four ``VCT_RL_MODULE_*`` env vars the launcher writes into
    ``.claude/settings.json env`` via
    ``commands::module_deprecation::apply_deprecation_state``. The four
    keys (set together; stripped together) are:

      * ``VCT_RL_MODULE_DEPRECATED=1`` (or absent)
      * ``VCT_RL_MODULE_DEPRECATION_MESSAGE=...`` (human-readable line)
      * ``VCT_RL_MODULE_DEPRECATION_DATE=YYYY-MM-DD`` (optional ISO date)
      * ``VCT_RL_MODULE_DEPRECATION_URL=https://...``  (optional URL)

    Returns:
        ``None`` when ``VCT_RL_MODULE_DEPRECATED`` is unset or not ``"1"``.
        Otherwise a single-line banner string ready to prepend to the
        retrieval response. The MCP server's hybrid_search formatter adds
        the actual separator before the JSON body.

    Throttling: NONE. Claude's context is per-turn, so the warning must
    appear on EVERY rerank-related response while deprecation is active.
    Suppressing repeats inside one process would silently drop the
    warning on every subsequent turn of the same Claude session.
    """
    if os.getenv("VCT_RL_MODULE_DEPRECATED") != "1":
        return None
    msg = os.getenv(
        "VCT_RL_MODULE_DEPRECATION_MESSAGE",
        "RL Reranker module is deprecated.",
    )
    date = os.getenv("VCT_RL_MODULE_DEPRECATION_DATE", "")
    url = os.getenv("VCT_RL_MODULE_DEPRECATION_URL", "")
    parts = [f"[DEPRECATION WARNING] {msg}"]
    if date:
        parts.append(f"EOL: {date}.")
    if url:
        parts.append(f"Migration guide: {url}")
    return " ".join(parts)


class RLClientError(Exception):
    """Base class for RL client errors."""


class RLClientUnreachableError(RLClientError):
    """Raised when the RL server cannot be reached.

    Distinct from generic ``RLClientError`` so callers that want to
    alert on persistent unavailability (vs a one-off 5xx) can branch.
    """


def _resolve_base_url() -> Optional[str]:
    """Resolve the RL server base URL from env vars.

    Reads ``RL_SERVER_URL`` first (canonical), then composes from
    ``RL_SERVER_PORT`` if only that's set (launcher writes both per
    the ``allocate_rl_port`` flow). Returns None for "disabled mode".
    """
    url = os.environ.get("RL_SERVER_URL", "").strip()
    if url:
        return url.rstrip("/")
    port = os.environ.get("RL_SERVER_PORT", "").strip()
    if port:
        return f"http://127.0.0.1:{port}"
    return None


class RLClient:
    """Async HTTP client for ``vct-rl-reranker``.

    Instance is **lightweight to construct**; the underlying
    ``httpx.AsyncClient`` is created lazily on first use and torn
    down via ``aclose()`` (or use as an async context manager).

    Attributes:
        base_url: Resolved server URL, or None when in disabled mode.
        text_dim: Configured text-embedding dim (for query_emb checks).
        active_embedding: Tag (qwen3 / arctic / openai / codesage)
            forwarded on every request so the server can keep its
            per-source log tagging in sync.
    """

    def __init__(
        self,
        *,
        text_dim: int = 1024,
        active_embedding: str = "qwen3",
        base_url: Optional[str] = None,
        timeout: float = _DEFAULT_TIMEOUT,
        client: Optional[Any] = None,  # httpx.AsyncClient or test mock
    ) -> None:
        # Allow explicit override of base_url (tests + advanced callers);
        # otherwise derive from env (None → disabled mode).
        self.base_url: Optional[str] = (
            base_url.rstrip("/") if base_url else _resolve_base_url()
        )
        self.text_dim = int(text_dim)
        self.active_embedding = str(active_embedding or "qwen3")
        self._timeout = float(timeout)
        # Injected client (test mocks); real client lazily constructed.
        self._client = client
        self._owns_client = client is None

        if self.base_url is None:
            logger.debug(
                "RLClient: disabled mode (no RL_SERVER_URL/RL_SERVER_PORT). "
                "cache_nodes will return inputs unchanged; rl_update is a no-op."
            )

    # ---- lifecycle ----------------------------------------------------

    async def __aenter__(self) -> "RLClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying httpx client if we own it."""
        if self._client is not None and self._owns_client:
            try:
                await self._client.aclose()
            except Exception:  # pragma: no cover (defensive)
                pass
        self._client = None

    def __repr__(self) -> str:
        if self.base_url is None:
            return (
                f"RLClient(disabled, text_dim={self.text_dim}, "
                f"active_embedding={self.active_embedding!r})"
            )
        return (
            f"RLClient(base_url={self.base_url!r}, text_dim={self.text_dim}, "
            f"active_embedding={self.active_embedding!r})"
        )

    @property
    def enabled(self) -> bool:
        """Whether the client is wired to a real server (not disabled mode)."""
        return self.base_url is not None

    # ---- public API ---------------------------------------------------

    async def cache_nodes(
        self,
        query: str,
        nodes: List[Any],
        top_k: int,
        *,
        task_id: str,
        query_emb: Optional[List[float]] = None,
        session_id: str = "",
    ) -> List[Any]:
        """Rerank ``nodes`` and return the top-k.

        Behavior:
            * Disabled mode → returns ``nodes[:top_k]`` unchanged.
            * Reachable + 200 → returns the server's ``top_k`` list.
            * Connection refused / timeout / 5xx → returns
              ``nodes[:top_k]`` unchanged. Logs at debug level; does
              NOT raise so the surrounding KG search keeps working.

        Args:
            query: Search query text.
            nodes: All over-fetched nodes (free-form dicts). Each
                should have at minimum ``title`` and ``score``.
            top_k: How many to return after reranking.
            task_id: Unique identifier linking this retrieval to a
                later ``rl_update`` call. Required.
            query_emb: Optional query embedding (sanity-checked
                against ``self.text_dim``).
            session_id: Optional Claude Code session id; forwarded to
                the container so server-side telemetry rows can be
                grouped by chat (v0.2.31 telemetry audit fix —
                container ships matching ``session_id`` kwarg in
                ``vct-rl-reranker`` v0.2.4+). Defaults to empty string
                for backward-compat with older containers (the field
                is ignored when absent).
        """
        # Defensive: drop misshaped query_emb rather than raising.
        if query_emb is not None and len(query_emb) != self.text_dim:
            logger.debug(
                "RLClient.cache_nodes: query_emb dim=%d != configured text_dim=%d; "
                "dropping query_emb from request",
                len(query_emb),
                self.text_dim,
            )
            query_emb = None

        if not self.enabled:
            return list(nodes[:top_k])

        # Build the request payload. ``nodes`` are passed as-is
        # (server tolerates extra fields).
        payload = {
            "task_id": task_id,
            "query": query,
            "nodes": list(nodes),
            "limit": int(top_k),
            "embedding_source": self.active_embedding,
            "active_embedding": self.active_embedding,
            "session_id": str(session_id or ""),
        }
        if query_emb is not None:
            payload["query_emb"] = query_emb

        try:
            data = await self._post_json("/cache_nodes", payload, timeout=self._timeout)
        except RLClientUnreachableError as exc:
            logger.debug("RLClient.cache_nodes: unreachable (%s); falling back to no-rerank", exc)
            return list(nodes[:top_k])
        except RLClientError as exc:
            logger.debug("RLClient.cache_nodes: error (%s); falling back to no-rerank", exc)
            return list(nodes[:top_k])

        top = data.get("top_k") or []
        if not isinstance(top, list):
            logger.debug("RLClient.cache_nodes: malformed top_k (%r); falling back", type(top))
            return list(nodes[:top_k])

        # Server may return more / fewer than requested; trim defensively.
        return list(top[:top_k])

    async def rl_update(
        self,
        task_ids: List[str],
        agent_output: str,
        *,
        task_type: Optional[str] = None,
    ) -> RLUpdateResponse:
        """Submit Claude's agent output for citation detection + online training.

        Disabled mode / unreachable / 5xx → returns
        ``RLUpdateResponse(ok=False)`` without raising. The caller
        treats this as fire-and-forget.

        Args:
            task_ids: One or more retrieval task_ids that share this
                agent output (e.g. multiple KG calls in one turn).
            agent_output: Claude's response text after the KG search.
            task_type: Optional category tag (logged server-side).
        """
        if not self.enabled:
            return RLUpdateResponse(ok=False, skipped="disabled")

        if not task_ids or not agent_output:
            return RLUpdateResponse(ok=True, skipped="no task_ids or agent_output")

        payload = {
            "task_ids": list(task_ids),
            "agent_output": agent_output,
        }
        if task_type:
            payload["task_type"] = task_type

        try:
            data = await self._post_json("/rl_update", payload, timeout=self._timeout)
        except RLClientUnreachableError as exc:
            logger.debug("RLClient.rl_update: unreachable (%s)", exc)
            return RLUpdateResponse(ok=False, error=str(exc))
        except RLClientError as exc:
            logger.debug("RLClient.rl_update: error (%s)", exc)
            return RLUpdateResponse(ok=False, error=str(exc))

        try:
            return RLUpdateResponse.model_validate(data)
        except Exception as exc:  # noqa: BLE001 — keep response permissive
            logger.debug("RLClient.rl_update: response shape mismatch (%s)", exc)
            return RLUpdateResponse(ok=bool(data.get("ok", False)))

    async def health(self) -> HealthResponse:
        """Probe the server's ``/health`` endpoint.

        Disabled mode → ``HealthResponse(ok=False, model='disabled')``.
        Unreachable → ``HealthResponse(ok=False, model='unreachable')``.
        """
        if not self.enabled:
            return HealthResponse(ok=False, model="disabled")

        try:
            data = await self._get_json("/health", timeout=_DEFAULT_HEALTH_TIMEOUT)
        except RLClientUnreachableError as exc:
            logger.debug("RLClient.health: unreachable (%s)", exc)
            return HealthResponse(ok=False, model="unreachable")
        except RLClientError as exc:
            logger.debug("RLClient.health: error (%s)", exc)
            return HealthResponse(ok=False, model="error")

        try:
            return HealthResponse.model_validate(data)
        except Exception:
            return HealthResponse(ok=bool(data.get("ok", False)),
                                  model=str(data.get("model", "unknown")))

    # ---- transport ----------------------------------------------------

    async def _ensure_client(self) -> Any:
        """Lazily construct the underlying httpx.AsyncClient."""
        if self._client is not None:
            return self._client
        if httpx is None:  # pragma: no cover
            raise RLClientError("httpx not installed; cannot make RL server requests")
        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def _post_json(
        self,
        path: str,
        json_body: dict,
        *,
        timeout: float,
    ) -> dict:
        """POST JSON, return parsed dict, or raise RLClientError on failure.

        Raises:
            RLClientUnreachableError: connect/timeout errors.
            RLClientError: HTTP non-2xx or JSON decode errors.
        """
        assert self.base_url is not None  # enabled-mode invariant
        client = await self._ensure_client()
        url = f"{self.base_url}{path}"
        try:
            resp = await client.post(url, json=json_body, timeout=timeout)
        except Exception as exc:
            # httpx.ConnectError / TimeoutException / ReadError — all
            # treated as "unreachable" so callers can branch.
            raise RLClientUnreachableError(f"POST {url} failed: {exc}") from exc

        if resp.status_code >= 500:
            raise RLClientUnreachableError(
                f"POST {url}: server error {resp.status_code}"
            )
        if resp.status_code >= 400:
            raise RLClientError(
                f"POST {url}: HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except Exception as exc:
            raise RLClientError(f"POST {url}: invalid JSON: {exc}") from exc

    async def _get_json(self, path: str, *, timeout: float) -> dict:
        """GET JSON, same error semantics as ``_post_json``."""
        assert self.base_url is not None
        client = await self._ensure_client()
        url = f"{self.base_url}{path}"
        try:
            resp = await client.get(url, timeout=timeout)
        except Exception as exc:
            raise RLClientUnreachableError(f"GET {url} failed: {exc}") from exc
        if resp.status_code >= 500:
            raise RLClientUnreachableError(
                f"GET {url}: server error {resp.status_code}"
            )
        if resp.status_code >= 400:
            raise RLClientError(
                f"GET {url}: HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except Exception as exc:
            raise RLClientError(f"GET {url}: invalid JSON: {exc}") from exc
