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

import json
import logging
import os
from dataclasses import dataclass
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


# ─── v0.2.73 RL-10: container protocol version negotiation ────────────
#
# The MCP RL client and the paid vct-rl-reranker container speak an HTTP
# wire contract (schemas.py). Historically the ONLY compatibility signal
# was ``ConfigDict(extra="allow")`` on both sides — a newer client's extra
# fields are silently ignored by an older container and vice-versa. That
# tolerance quietly hides genuine breakage: if the container's rerank
# response shape changes incompatibly, or the container assumes a
# different embedding space than the client feeds it (RL-2b: code
# citations are CodeSage 2048-dim while the container may assume qwen3
# 1024-dim), the client keeps POSTing and silently gets cosine order back
# with no signal to the paying user.
#
# ``PROTOCOL_VERSION`` is the wire-contract version THIS client implements.
# ``negotiate()`` probes ``/health`` and classifies the pairing:
#   * compatible          — container advertises a protocol the client
#                           supports (>= MIN_SERVER_PROTOCOL, <= our own).
#   * degraded_old_server — container is older than we require, OR omitted
#                           the field entirely (pre-RL-10). We still talk
#                           to it (extra=allow tolerance) but flag it.
#   * incompatible_new_server — container advertises a protocol NEWER than
#                           this client understands. Refuse rerank, use
#                           cosine, surface loudly (upgrade the client).
#   * embedding_space_mismatch — container's advertised embedding space
#                           does not match the client's active space
#                           (RL-2b). Refuse rerank for that space rather
#                           than train/query the wrong network.
# Bump PROTOCOL_VERSION when the request/response SHAPE changes in a way
# an older peer cannot tolerate. Additive optional fields do NOT need a
# bump (extra=allow handles them).
PROTOCOL_VERSION = 1
# The oldest container protocol this client will engage without flagging
# a degraded pairing. Pre-RL-10 containers advertise None → treated as v1.
MIN_SERVER_PROTOCOL = 1


@dataclass(frozen=True)
class NegotiationResult:
    """Outcome of a container version handshake (RL-10). Pure data.

    ``compatible`` is the single boolean the pipeline gates on. ``status``
    is a stable machine tag (compatible / degraded_old_server /
    incompatible_new_server / embedding_space_mismatch / unreachable /
    disabled) for rl-doctor + logs. ``detail`` is a human-readable line
    (never carries user data). ``server_protocol`` / ``server_embedding_*``
    echo what the container advertised (None when it did not / unreachable).
    """

    compatible: bool
    status: str
    detail: str
    server_protocol: Optional[int] = None
    server_embedding_dim: Optional[int] = None
    server_embedding_space: Optional[str] = None


# ─── v0.2.49: per-project routing header sanitization ────────────────
# Stream C's vct-rl-reranker v0.2.10 container reads the
# ``X-VCT-Project-ID`` header and uses the value VERBATIM as a
# filesystem path component (``/data/state/projects/<project_id>/``)
# and a JSONL filename suffix (``rl_events_<project_id>.jsonl``).
# Path-traversal risk: a malicious / malformed project_id value like
# ``../etc/passwd`` would land state files outside ``/data/state/``.
# Stream C's report explicitly flagged this as launcher-side
# responsibility: the container does not sanitise.
#
# This sanitizer is the defensive guard at the launcher → container
# seam. Accepts a small character set (UUID + alphanumeric + dash +
# underscore), rejects everything else. Length capped at 64 chars
# (UUID = 36; slugs we see in practice are <= 32; 64 is a generous
# headroom that still bounds filesystem path length).

import re

# Match UUID v4 case-insensitive OR alphanumeric/dash/underscore
# (slugs the launcher generates from project names). Length 1..64.
# Anchored — partial matches not accepted.
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def sanitize_project_id(value: Optional[str]) -> Optional[str]:
    """Return ``value`` if it's a safe-for-filesystem project identifier,
    else ``None``. Used to gate the ``X-VCT-Project-ID`` header sent to
    the vct-rl-reranker container (v0.2.10+).

    Safe characters: ASCII letters, digits, dash, underscore. Length
    1..64. Anything else (path separators, control chars, dots, slashes,
    spaces, NUL, unicode) → None.

    Returning None means "do NOT send the header at all" — the container
    falls back to the base model. This is the intended fail-mode: a
    malformed project_id should result in the base model being used,
    not in an error AND not in the malformed value reaching the
    container.

    Examples:
        sanitize_project_id("02fbc934-ada5-433c-b606-d1f56194035a")
            → "02fbc934-ada5-433c-b606-d1f56194035a"  (UUID v4 OK)
        sanitize_project_id("orchestrator-root")
            → "orchestrator-root"  (slug OK)
        sanitize_project_id("../etc/passwd")
            → None  (path traversal blocked)
        sanitize_project_id("project id")
            → None  (space blocked)
        sanitize_project_id(None) → None
        sanitize_project_id("") → None
        sanitize_project_id("a" * 65) → None  (length cap)
    """
    if not isinstance(value, str) or not value:
        return None
    if not _PROJECT_ID_RE.match(value):
        return None
    return value


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
        project_id: Optional[str] = None,
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

        # v0.2.73 RL-3: per-call rerank outcome surface. ``last_call_ok`` is
        # True only when the LAST cache_nodes call genuinely got a reranked
        # list back from the container; ``last_error`` carries the failure
        # tag otherwise ("disabled" / unreachable / HTTP 4xx text). The
        # pipeline reads these to (a) report ``rl_used`` accurately and (b)
        # surface + count fallbacks — a paying user silently degraded to
        # cosine by a container 4xx (e.g. the env-pin 409) now gets a signal.
        self.last_call_ok: bool = False
        self.last_error: Optional[str] = None
        self._warned_4xx: bool = False

        # v0.2.49: per-project routing header. Stored as the sanitized
        # value (or None if input was unsafe). The header is only sent
        # when this is non-None; container falls back to base model when
        # absent, which is the safe behaviour for malformed input.
        #
        # See sanitize_project_id() for the accepted character set.
        self._project_id: Optional[str] = sanitize_project_id(project_id)
        if project_id and self._project_id is None:
            logger.warning(
                "RLClient: rejected unsafe project_id %r; "
                "X-VCT-Project-ID header will NOT be sent (container "
                "will fall back to base model)",
                project_id,
            )

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

        # v0.2.73 RL-3: reset the per-call outcome surface up front so a
        # caller reading it after this call sees THIS call's result.
        self.last_call_ok = False
        self.last_error = None

        if not self.enabled:
            self.last_error = "disabled"
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
            # Routine transient class (container starting / stopped / 5xx) —
            # keep at debug; the pipeline-level fallback counter aggregates.
            self.last_error = str(exc)
            logger.debug("RLClient.cache_nodes: unreachable (%s); falling back to no-rerank", exc)
            return list(nodes[:top_k])
        except RLClientError as exc:
            # v0.2.73 RL-3: a 4xx is NOT transient — it means the container
            # actively refused (env-pin 409, contract mismatch, bad request)
            # and every subsequent call will refuse too: the paying user is
            # PERMANENTLY on cosine until it's fixed. Surface loudly once per
            # client instance, then degrade to debug.
            self.last_error = str(exc)
            if not self._warned_4xx:
                self._warned_4xx = True
                logger.warning(
                    "RLClient.cache_nodes: container REFUSED the rerank "
                    "request (%s). RL reranking is falling back to cosine "
                    "order and will keep doing so until the container/config "
                    "mismatch is resolved.", exc,
                )
            else:
                logger.debug("RLClient.cache_nodes: error (%s); falling back to no-rerank", exc)
            return list(nodes[:top_k])

        top = data.get("top_k") or []
        if not isinstance(top, list):
            self.last_error = f"malformed top_k ({type(top).__name__})"
            logger.debug("RLClient.cache_nodes: malformed top_k (%r); falling back", type(top))
            return list(nodes[:top_k])

        # Server may return more / fewer than requested; trim defensively.
        self.last_call_ok = True
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
            # v0.2.40 F1 — silent-correctness fix: mirror the cache_nodes
            # contract so the server can verify the training signal came
            # from the same embedding source that produced the original
            # retrieval candidates. Without this, an arctic2-sourced
            # /rl_update would silently train the qwen3-tagged network
            # (or vice versa). The server's RLUpdateRequest accepts the
            # field as an extra (ConfigDict(extra="allow")), so older
            # servers tolerate it; newer ones gate on it.
            "embedding_source": self.active_embedding,
            "active_embedding": self.active_embedding,
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

    async def rl_update_v3(
        self,
        task_id: str,
        *,
        nodes_packed: List[dict],
        query_emb: List[float],
        cosine_sims: dict,
        literal_cited: dict,
        cross_encoder_cited: Optional[dict] = None,
        task_type: Optional[str] = None,
    ) -> RLUpdateResponse:
        """v0.2.9 (C8) pre-packed payload contract for /rl_update.

        Container is pure-train: MCP supplies everything the container
        needs to apply the unified-target formula and step the gradient,
        with NO embedding/chunking/data-logger writes happening
        container-side. This client method is the mirror of the
        container's ``_rl_update`` handler + ``schedule_update`` +
        ``_update`` signatures (see retrieval_rl.py / rl_server.py in
        the paid module).

        Disabled mode / unreachable / 5xx → returns
        ``RLUpdateResponse(ok=False)`` without raising. Caller treats
        as fire-and-forget.

        Args:
            task_id: Single retrieval task_id this training event
                pertains to. (The wire still uses ``task_ids: List``
                for forward compatibility — multi-task batching is
                deferred to v0.2.10+.)
            nodes_packed: Per-node training records, each with
                ``{title, node_type (string name), n_emb,
                linked_embs, linked_type_names}``. Embeddings are
                already in the active source's space.
            query_emb: Query embedding vector in the active source's
                space.
            cosine_sims: ``{title: cos(answer_chunks, n_emb)}`` map.
                Computed by MCP from the completed answer.
            literal_cited: ``{title: bool}`` map — whether the node's
                title/slug/wikilink/file_path appears in the agent's
                answer (word-boundary regex). Training signal — boosts
                the BCE target via the unified-target formula.
            cross_encoder_cited: Optional ``{title: bool}`` map from a
                cross-encoder reranker (Qwen3-Reranker-4B). ``None`` in
                v0.2.9; the field is reserved for v0.2.10+ Pro-tier
                wiring. Composes with literal_cited via the same bonus.
            task_type: Optional category tag (logged server-side).
        """
        if not self.enabled:
            return RLUpdateResponse(ok=False, skipped="disabled")

        if not task_id or not nodes_packed or not query_emb:
            return RLUpdateResponse(ok=True, skipped="no task_id or nodes or query_emb")

        task_block: dict = {
            "nodes_packed": list(nodes_packed),
            "query_emb": list(query_emb),
            "cosine_sims": dict(cosine_sims),
            "literal_cited": dict(literal_cited),
            "cross_encoder_cited": dict(cross_encoder_cited) if cross_encoder_cited else None,
        }
        payload: dict = {
            "task_ids": [task_id],
            "tasks": {task_id: task_block},
            # v0.2.40 F1 cross-source guard (kept identical to
            # ``rl_update``): the server rejects if its active
            # embedding source doesn't match this field.
            "embedding_source": self.active_embedding,
            "active_embedding": self.active_embedding,
        }
        if task_type:
            payload["task_type"] = task_type

        try:
            data = await self._post_json("/rl_update", payload, timeout=self._timeout)
        except RLClientUnreachableError as exc:
            logger.debug("RLClient.rl_update_v3: unreachable (%s)", exc)
            return RLUpdateResponse(ok=False, error=str(exc))
        except RLClientError as exc:
            logger.debug("RLClient.rl_update_v3: error (%s)", exc)
            return RLUpdateResponse(ok=False, error=str(exc))

        try:
            return RLUpdateResponse.model_validate(data)
        except Exception as exc:  # noqa: BLE001 — keep response permissive
            logger.debug("RLClient.rl_update_v3: response shape mismatch (%s)", exc)
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

    async def negotiate(
        self,
        *,
        health: Optional[HealthResponse] = None,
    ) -> NegotiationResult:
        """RL-10: negotiate wire-contract + embedding-space compatibility.

        Probes ``/health`` (unless a pre-fetched ``health`` is passed — lets
        rl-doctor reuse a single probe) and classifies the client↔container
        pairing into a ``NegotiationResult``. This is READ-ONLY: it never
        mutates the client or the container, only reports whether the client
        should engage the reranker.

        Compatibility rules:
          * Disabled mode → ``disabled`` (compatible=False; nothing to talk to).
          * Unreachable / health not ok → ``unreachable`` (compatible=False).
          * Container protocol > this client's PROTOCOL_VERSION →
            ``incompatible_new_server`` (compatible=False): the container speaks
            a newer contract we don't understand; refuse rather than misparse.
          * Container protocol advertised but < MIN_SERVER_PROTOCOL, OR not
            advertised at all (pre-RL-10) → ``degraded_old_server``
            (compatible=True): we still talk to it via extra=allow tolerance,
            but flag the pairing so a Pro user can see it's on an old container.
          * Container advertises an embedding space that DISAGREES with this
            client's configured space (dim or source tag) →
            ``embedding_space_mismatch`` (compatible=False): the RL-2b hazard.
            Feeding a 2048-dim CodeSage query into a container whose head
            expects 1024-dim qwen3 (or vice-versa) trains/queries the wrong
            network; refuse for that space.
          * Otherwise → ``compatible`` (compatible=True).

        Never raises. A probe failure degrades to ``unreachable``.
        """
        if not self.enabled:
            return NegotiationResult(
                compatible=False,
                status="disabled",
                detail="RL client is in disabled mode (no RL_SERVER_URL/PORT).",
            )

        h = health if health is not None else await self.health()
        if not h.ok:
            return NegotiationResult(
                compatible=False,
                status="unreachable",
                detail=f"container health not ok (model={h.model!r}).",
            )

        server_proto = h.protocol_version  # None ⇒ pre-RL-10 container
        srv_dim = h.embedding_dim
        srv_space = h.embedding_space

        # Newer server than we understand → refuse (don't misparse a contract
        # we don't know).
        if server_proto is not None and server_proto > PROTOCOL_VERSION:
            return NegotiationResult(
                compatible=False,
                status="incompatible_new_server",
                detail=(
                    f"container protocol v{server_proto} is newer than this "
                    f"client's v{PROTOCOL_VERSION}; update the MCP/orchestrator."
                ),
                server_protocol=server_proto,
                server_embedding_dim=srv_dim,
                server_embedding_space=srv_space,
            )

        # RL-2b embedding-space guard. Only fires when BOTH sides declare a
        # space (the container advertised it AND the client has a non-default
        # text_dim/active_embedding). A container that doesn't advertise its
        # space is handled by the degraded path below (can't check → trust).
        if srv_dim is not None and srv_dim != self.text_dim:
            return NegotiationResult(
                compatible=False,
                status="embedding_space_mismatch",
                detail=(
                    f"container embedding dim {srv_dim} != client text_dim "
                    f"{self.text_dim} (RL-2b: mismatched embedding space; "
                    f"refusing rerank to avoid the wrong network)."
                ),
                server_protocol=server_proto,
                server_embedding_dim=srv_dim,
                server_embedding_space=srv_space,
            )
        if (
            srv_space is not None
            and self.active_embedding
            and srv_space != self.active_embedding
        ):
            return NegotiationResult(
                compatible=False,
                status="embedding_space_mismatch",
                detail=(
                    f"container embedding space {srv_space!r} != client "
                    f"active_embedding {self.active_embedding!r} (RL-2b)."
                ),
                server_protocol=server_proto,
                server_embedding_dim=srv_dim,
                server_embedding_space=srv_space,
            )

        # Old / non-advertising container: still usable (extra=allow), but flag.
        if server_proto is None or server_proto < MIN_SERVER_PROTOCOL:
            return NegotiationResult(
                compatible=True,
                status="degraded_old_server",
                detail=(
                    "container did not advertise a protocol version (pre-RL-10) "
                    if server_proto is None
                    else f"container protocol v{server_proto} < required "
                    f"v{MIN_SERVER_PROTOCOL} "
                )
                + "— engaging via best-effort tolerance.",
                server_protocol=server_proto,
                server_embedding_dim=srv_dim,
                server_embedding_space=srv_space,
            )

        return NegotiationResult(
            compatible=True,
            status="compatible",
            detail=(
                f"container protocol v{server_proto} compatible with client "
                f"v{PROTOCOL_VERSION}."
            ),
            server_protocol=server_proto,
            server_embedding_dim=srv_dim,
            server_embedding_space=srv_space,
        )

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

        # v0.2.49: per-project routing header. Sent only when the
        # constructor received a project_id that passed
        # sanitize_project_id(). The container uses this to look up
        # per-project fine-tuned model heads; absent → base model.
        #
        # v0.2.73 RL-10: ALWAYS advertise this client's wire-contract
        # protocol version so a container that DOES negotiate can pick a
        # compatible response shape (or reject an unsupported client). An
        # older, pre-RL-10 container simply ignores the header. Additive —
        # never breaks an existing peer.
        headers: dict = {"X-VCT-RL-Protocol": str(PROTOCOL_VERSION)}
        if self._project_id is not None:
            headers["X-VCT-Project-ID"] = self._project_id

        # v0.2.74 T5-1b (BLOCKER-2): coerce the body to a JSON-safe dict at the
        # single POST choke point BEFORE httpx serializes it. ``cache_nodes``
        # passes candidate node dicts VERBATIM (``payload["nodes"] =
        # list(nodes)``), and a candidate may embed a ``uuid.UUID`` (a links /
        # wikilink / enrichment field). httpx's ``json=`` path uses the stdlib
        # encoder, which raises ``TypeError: Object of type UUID is not JSON
        # serializable`` — the POST then fails, RLClient falls back to cosine
        # order, and the paying user is silently demoted per query.
        #
        # A ``json.dumps(..., default=str)`` → ``json.loads`` round-trip
        # coerces EVERY non-natively-serializable value (UUID, datetime, Path,
        # Decimal, …) to its ``str()`` — covering ALL POST bodies (cache_nodes
        # AND both rl_update variants), not just the one field we know about
        # today — while STILL handing httpx a plain dict via ``json=`` so the
        # content-type, length, and the test mocks' ``json=`` contract are all
        # unchanged. Already-string wire fields (session_id / task_id /
        # embedding_source) hit the native path and are byte-identical.
        try:
            safe_body = json.loads(json.dumps(json_body, default=str))
        except (TypeError, ValueError) as exc:
            # A value not even ``default=str`` can coerce (e.g. a bytes blob or
            # a circular ref) — treat as a client-side bad request so the
            # caller falls back to cosine rather than crashing the search.
            raise RLClientError(f"POST {url}: body not serializable: {exc}") from exc

        try:
            resp = await client.post(url, json=safe_body, headers=headers, timeout=timeout)
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
