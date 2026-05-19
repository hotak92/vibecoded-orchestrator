# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""CodeEmbed FastAPI service adapter.

The CodeEmbed service is a thin FastAPI wrapper around
sentence-transformers, running in ``claude_mcp_servers/code_embedding_service``
(or the equivalent container) on port 11440 by default. It exposes:

  * ``GET /health`` →
    ``{"status": "ok", "backend": "...", "model": "...", "dim": N, ...}``
  * ``POST /embed`` with ``{"texts": [...], "is_query": false}`` →
    ``{"embeddings": [[...]], "dim": N, "count": N, "backend": "...", "model": "..."}``
    Max 256 texts per call (server-side limit).
  * ``POST /api/embeddings`` (Ollama-compatible single-item shim) with
    ``{"model": "", "prompt": "..."}`` → ``{"embedding": [...]}``

This adapter ALWAYS prefers ``/embed`` (native batched) over the
Ollama-compat shim. The shim is only used by upstream scripts that
want a drop-in for Ollama; new code paths through ``EmbeddingService``
use ``/embed`` directly because it returns the model + dim metadata
we need for cataloguing.

Loaded model is discovered at construction time (cached after first
``/health`` probe). The service can be configured to run on CPU
(qwen3-embedding fallback) or GPU (CodeSage-Large-v2); the adapter
doesn't care — it reads whatever ``backend`` and ``model`` the service
reports.
"""

from __future__ import annotations

from typing import Any

import requests

# Maximum batch size in one HTTP call. The server enforces 256, we use
# the same constant so callers can chunk preemptively.
MAX_BATCH_SIZE = 256


class CodeEmbedAdapter:
    """Adapter for the CodeEmbed FastAPI service.

    Attributes:
        base_url: Root URL of the CodeEmbed HTTP API
            (e.g. ``"http://localhost:11440"``). No trailing slash.
        session: Injected ``requests.Session`` for connection pooling.
            The caller owns ``close()``.
        timeout: Per-request timeout in seconds. Default 120s — embedding
            256 functions on CodeSage-Large-v2 on a slow GPU can take
            ~30s, and the queue can add more wait.
    """

    def __init__(
        self,
        base_url: str,
        session: requests.Session,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.timeout = timeout
        self._health_cache: dict[str, Any] | None = None

    # ---- health / discovery -------------------------------------------------

    def health(self) -> dict[str, Any] | None:
        """Return cached health response, or None if the service is unreachable.

        Health is cached after first successful call — the loaded model
        won't change without a service restart. To force a re-probe,
        call :meth:`invalidate_health` first.
        """
        if self._health_cache is not None:
            return self._health_cache

        try:
            response = self.session.get(
                f"{self.base_url}/health",
                timeout=min(self.timeout, 5.0),
            )
        except requests.RequestException:
            return None

        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if payload.get("status") != "ok":
            return None
        self._health_cache = payload
        return payload

    def invalidate_health(self) -> None:
        """Drop the cached health response — next ``health()`` re-probes."""
        self._health_cache = None

    def is_reachable(self) -> bool:
        """Return True if ``/health`` reports a healthy service."""
        return self.health() is not None

    @property
    def model_name(self) -> str | None:
        """Loaded model name reported by ``/health``, or None if unreachable."""
        h = self.health()
        return h.get("model") if h else None

    @property
    def model_dim(self) -> int | None:
        """Embedding dimension reported by ``/health``, or None if unreachable."""
        h = self.health()
        if h is None:
            return None
        dim = h.get("dim")
        try:
            return int(dim) if dim is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def backend(self) -> str | None:
        """Reported backend (``"gpu"`` / ``"ollama"`` / ``"cpu"`` / ...)."""
        h = self.health()
        return h.get("backend") if h else None

    # ---- embed --------------------------------------------------------------

    def embed(self, text: str, is_query: bool = False) -> list[float]:
        """Embed a single text.

        Always goes through the batched ``/embed`` endpoint (one-item
        batch). The ``is_query`` flag is passed through to the service
        — sentence-transformers code-embedding models use a different
        prefix for query vs document at inference time, so callers
        should set ``is_query=True`` when embedding a search query.

        Raises:
            RuntimeError: On non-2xx response or malformed payload.
        """
        results = self.embed_batch([text], is_query=is_query)
        return results[0]

    def embed_batch(
        self,
        texts: list[str],
        is_query: bool = False,
    ) -> list[list[float]]:
        """Embed a batch of texts (auto-chunks at MAX_BATCH_SIZE).

        An empty list returns an empty list without an HTTP call.

        Raises:
            RuntimeError: On non-2xx response or malformed payload.
        """
        if not texts:
            return []

        if len(texts) <= MAX_BATCH_SIZE:
            return self._embed_chunk(texts, is_query=is_query)

        # Split into MAX_BATCH_SIZE-sized chunks; preserve order.
        out: list[list[float]] = []
        for i in range(0, len(texts), MAX_BATCH_SIZE):
            chunk = texts[i : i + MAX_BATCH_SIZE]
            out.extend(self._embed_chunk(chunk, is_query=is_query))
        return out

    def _embed_chunk(
        self,
        texts: list[str],
        is_query: bool,
    ) -> list[list[float]]:
        """One HTTP call to ``/embed`` for a chunk ≤ MAX_BATCH_SIZE."""
        try:
            response = self.session.post(
                f"{self.base_url}/embed",
                json={"texts": texts, "is_query": is_query},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"CodeEmbed /embed network error: {exc}") from exc

        if response.status_code != 200:
            raise RuntimeError(
                f"CodeEmbed /embed returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"CodeEmbed /embed returned non-JSON: {response.text[:500]}"
            ) from exc

        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list):
            raise RuntimeError(
                f"CodeEmbed /embed returned no embeddings: {payload!r}"
            )
        if len(embeddings) != len(texts):
            raise RuntimeError(
                f"CodeEmbed /embed returned {len(embeddings)} embeddings "
                f"for {len(texts)} inputs — order cannot be reconstructed"
            )
        result: list[list[float]] = []
        for i, vec in enumerate(embeddings):
            if not isinstance(vec, list):
                raise RuntimeError(
                    f"CodeEmbed /embed returned malformed embedding at "
                    f"index {i}: {vec!r}"
                )
            result.append([float(x) for x in vec])
        return result
