# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Ollama embedding adapter.

Ollama exposes two embedding endpoints with different shapes:

  * ``POST /api/embed`` (newer, v0.4+) — accepts ``{"model", "input"}``
    where ``input`` is ``str`` OR ``list[str]``, returns
    ``{"model", "embeddings": [[...], ...]}`` (always plural, always a
    list of lists, even for single-item input).
  * ``POST /api/embeddings`` (legacy, pre-v0.4) — accepts
    ``{"model", "prompt"}`` where ``prompt`` is a single ``str``,
    returns ``{"embedding": [...]}`` (single vector).

This adapter tries ``/api/embed`` first (one HTTP call per batch even
for multi-item inputs) and falls back to per-item ``/api/embeddings``
calls if the server returns 404 (= old Ollama). The fallback path is
N HTTP calls for a batch of N — slower but functionally equivalent.

Embedding-capable models on Ollama (as of 2026-05):

  * ``qwen3-embedding:0.6b`` — 1024 dim, the VCO default for KG.
    Requires ``options.num_ctx=8192`` to use its full 32k context.
  * ``snowflake-arctic-embed2:latest`` — 1024 dim, legacy default
    preserved by the multi-slot schema (``ollama_embed`` slot).
  * ``snowflake-arctic-embed-l-v2.0`` / ``arctic-embed:*`` — 1024 dim,
    on-prem arctic variants surfaced by the GUI dropdown.
  * ``mxbai-embed-large`` — 1024 dim.
  * ``unclemusclez/jina-embeddings-v2-base-code:latest`` — 768 dim,
    CPU code-embedding fallback when the GPU CodeEmbed service is
    not available.
  * ``nomic-embed-text`` — 768 dim.

The model list is queried dynamically from ``/api/tags`` so the user
sees whatever they have pulled — we don't hardcode it. The filter
predicate is "looks like an embedding model" (substring match on
common embedding-model name fragments).
"""

from __future__ import annotations

from typing import Any

import requests

# Model-name substrings that flag an Ollama model as embedding-capable.
# Ollama doesn't tell us via /api/tags whether a model can embed; the
# canonical signal is the model's metadata in /api/show, but probing
# every model individually is too slow for catalog discovery. The
# substring filter is good enough — false positives just show up
# greyed-out in the GUI dropdown if the embed call fails downstream.
_EMBEDDING_MODEL_HINTS: tuple[str, ...] = (
    "embed",       # qwen3-embedding, mxbai-embed, arctic-embed, ...
    "embedding",   # text-embedding-3-small, voyage-embedding, ...
)

# Per-model dim mappings for known embedding models. Used by catalog
# discovery when the dim can't be cheaply probed. Falls back to "unknown"
# (0) for models not in this list — the caller can probe by making a
# 1-token embed call if it really needs the dim.
KNOWN_OLLAMA_DIMS: dict[str, int] = {
    "qwen3-embedding:0.6b": 1024,
    "qwen3-embedding": 1024,
    "snowflake-arctic-embed2:latest": 1024,
    "snowflake-arctic-embed2": 1024,
    "snowflake-arctic-embed-l-v2.0": 1024,
    "snowflake-arctic-embed:latest": 1024,
    "snowflake-arctic-embed": 1024,
    "arctic-embed:l2": 1024,
    "arctic-embed": 1024,
    "mxbai-embed-large": 1024,
    "mxbai-embed-large:latest": 1024,
    "nomic-embed-text": 768,
    "nomic-embed-text:latest": 768,
    "unclemusclez/jina-embeddings-v2-base-code:latest": 768,
}


def looks_like_embedding_model(name: str) -> bool:
    """Return True if `name` looks like an Ollama embedding model."""
    lowered = name.lower()
    return any(hint in lowered for hint in _EMBEDDING_MODEL_HINTS)


class OllamaAdapter:
    """Adapter for Ollama's embedding endpoints.

    The adapter is stateless apart from its injected
    :class:`requests.Session` (used so callers can pool HTTP
    connections across many calls). It does NOT own the session —
    the caller is responsible for ``close()``.

    Attributes:
        base_url: Root URL of the Ollama HTTP API
            (e.g. ``"http://localhost:11435"``). No trailing slash.
        session: Injected ``requests.Session`` for connection pooling.
        timeout: Per-request timeout in seconds. Default 60s
            (embedding a 32k-token prompt on qwen3-embedding can take
            ~10s on CPU, so we give plenty of headroom).
    """

    def __init__(
        self,
        base_url: str,
        session: requests.Session,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.timeout = timeout

    # ---- health / discovery -------------------------------------------------

    def is_reachable(self) -> bool:
        """Return True if the Ollama server responds to ``GET /api/tags``."""
        try:
            response = self.session.get(
                f"{self.base_url}/api/tags",
                timeout=min(self.timeout, 5.0),
            )
        except requests.RequestException:
            return False
        return response.status_code == 200

    def list_models(self) -> list[dict[str, Any]]:
        """List all locally-pulled Ollama models via ``GET /api/tags``.

        Returns a list of dicts shaped like Ollama's response:
        ``[{"name": "qwen3-embedding:0.6b", "size": ..., "modified_at": ...}, ...]``.
        Returns an empty list on any error.
        """
        try:
            response = self.session.get(
                f"{self.base_url}/api/tags",
                timeout=min(self.timeout, 5.0),
            )
            response.raise_for_status()
        except requests.RequestException:
            return []
        try:
            data = response.json()
        except ValueError:
            return []
        return list(data.get("models", []))

    def list_embedding_models(self) -> list[dict[str, Any]]:
        """Subset of ``list_models()`` that look like embedding models."""
        return [m for m in self.list_models() if looks_like_embedding_model(str(m.get("name", "")))]

    # ---- embed --------------------------------------------------------------

    def embed(self, model: str, text: str, num_ctx: int = 8192) -> list[float]:
        """Embed a single text with the named model.

        Tries ``/api/embed`` first (newer Ollama), falls back to
        ``/api/embeddings`` on 404 (older Ollama). ``num_ctx`` is passed
        for qwen3-embedding compatibility — its actual capacity is 32k
        but Ollama defaults to 4096 (silent truncation).

        Raises:
            RuntimeError: On non-2xx responses other than the 404 that
                triggers the legacy fallback.
        """
        # Try the modern batched endpoint first.
        try:
            response = self.session.post(
                f"{self.base_url}/api/embed",
                json={
                    "model": model,
                    "input": text,
                    "options": {"num_ctx": num_ctx},
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Ollama /api/embed network error: {exc}") from exc

        if response.status_code == 404:
            # Old Ollama — fall back to /api/embeddings (single-item only).
            return self._embed_legacy(model, text)

        if response.status_code != 200:
            raise RuntimeError(
                f"Ollama /api/embed returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Ollama /api/embed returned non-JSON: {response.text[:500]}"
            ) from exc

        embeddings = payload.get("embeddings")
        if not embeddings or not isinstance(embeddings, list):
            raise RuntimeError(
                f"Ollama /api/embed returned no embeddings: {payload!r}"
            )
        first = embeddings[0]
        if not isinstance(first, list):
            raise RuntimeError(
                f"Ollama /api/embed returned malformed embeddings: {payload!r}"
            )
        return [float(x) for x in first]

    def embed_batch(
        self,
        model: str,
        texts: list[str],
        num_ctx: int = 8192,
    ) -> list[list[float]]:
        """Embed a batch of texts with the named model.

        On modern Ollama this is ONE HTTP call per batch (server handles
        the loop). On legacy Ollama (``/api/embed`` returns 404) we fall
        back to one HTTP call per text. Order is preserved in both
        cases.

        An empty ``texts`` list returns an empty list without making
        any HTTP call.

        Raises:
            RuntimeError: On non-2xx responses other than the 404 that
                triggers the legacy fallback.
        """
        if not texts:
            return []

        try:
            response = self.session.post(
                f"{self.base_url}/api/embed",
                json={
                    "model": model,
                    "input": texts,
                    "options": {"num_ctx": num_ctx},
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Ollama /api/embed network error: {exc}") from exc

        if response.status_code == 404:
            # Legacy Ollama: loop per-item over /api/embeddings.
            return [self._embed_legacy(model, t) for t in texts]

        if response.status_code != 200:
            raise RuntimeError(
                f"Ollama /api/embed returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Ollama /api/embed returned non-JSON: {response.text[:500]}"
            ) from exc

        embeddings = payload.get("embeddings")
        if not embeddings or not isinstance(embeddings, list):
            raise RuntimeError(
                f"Ollama /api/embed returned no embeddings: {payload!r}"
            )
        if len(embeddings) != len(texts):
            raise RuntimeError(
                f"Ollama /api/embed returned {len(embeddings)} embeddings "
                f"for {len(texts)} inputs — order cannot be reconstructed"
            )
        result: list[list[float]] = []
        for i, vec in enumerate(embeddings):
            if not isinstance(vec, list):
                raise RuntimeError(
                    f"Ollama /api/embed returned malformed embedding at "
                    f"index {i}: {vec!r}"
                )
            result.append([float(x) for x in vec])
        return result

    # ---- legacy fallback ----------------------------------------------------

    def _embed_legacy(self, model: str, text: str) -> list[float]:
        """Single-item embed via legacy ``/api/embeddings`` endpoint."""
        try:
            response = self.session.post(
                f"{self.base_url}/api/embeddings",
                json={"model": model, "prompt": text},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Ollama /api/embeddings network error: {exc}"
            ) from exc

        if response.status_code != 200:
            raise RuntimeError(
                f"Ollama /api/embeddings returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Ollama /api/embeddings returned non-JSON: {response.text[:500]}"
            ) from exc

        vec = payload.get("embedding")
        if not vec or not isinstance(vec, list):
            raise RuntimeError(
                f"Ollama /api/embeddings returned no embedding: {payload!r}"
            )
        return [float(x) for x in vec]
