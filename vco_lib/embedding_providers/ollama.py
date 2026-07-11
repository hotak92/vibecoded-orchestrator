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

import os
from typing import Any

import requests

from vco_lib.embedding_providers._http import bounded_post

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


# v0.2.47 RL-7.5 (2026-06-04): default num_ctx we send to Ollama when the
# caller doesn't override. Used when the model isn't registered in
# MODEL_TOKEN_LIMITS (chunking.py). Conservative — matches the pre-v0.2.47
# default so unknown models still embed at 8k rather than the Ollama
# default of 4k (which silently truncates inputs > 4096 tokens).
_NUM_CTX_FALLBACK: int = 8192


def _num_ctx_for_model(model: str) -> int:
    """Resolve the ``num_ctx`` to send to Ollama for a given embedding model.

    Reads ``claude_mcp_servers.weaviate_mcp.chunking.MODEL_TOKEN_LIMITS``
    when importable (the canonical source of truth for "what num_ctx do
    we want for this model"), falling back to ``_NUM_CTX_FALLBACK``
    otherwise. The chunking module is the SoT because chunk sizes must
    match the context window we actually request — if they're out of
    sync, oversized chunks get silently truncated by Ollama at num_ctx
    and the embedding signal degrades.

    Soft-fail: the lookup is best-effort. Any import error or missing
    entry falls through to ``_NUM_CTX_FALLBACK`` (8192).
    """
    try:
        from claude_mcp_servers.weaviate_mcp.chunking import MODEL_TOKEN_LIMITS
    except Exception:
        return _NUM_CTX_FALLBACK
    val = MODEL_TOKEN_LIMITS.get(model)
    if val is None:
        # Partial match (e.g. caller passed "qwen3-embedding" while the dict
        # has both "qwen3-embedding" AND "qwen3-embedding:0.6b").
        for key, registered in MODEL_TOKEN_LIMITS.items():
            if key in model or model in key:
                val = registered
                break
    return int(val) if val is not None else _NUM_CTX_FALLBACK


# v0.2.77 Part 9 task 6: keep the embedding model resident in Ollama between
# calls. Ollama's default keep_alive is ~5 min; after any idle gap the next
# embed pays a ~1.9 s model reload (measured in the hook-latency audit
# 2026-07-11). Sending keep_alive on every embed request pins the model so the
# recurring reload tax vanishes. This is a REQUEST-level fix (works regardless
# of the compose env default) and it is IDEMPOTENT — Ollama refreshes the TTL on
# every request that carries it.
#
# Default "24h" (effectively "resident for the working day"). The user can
# override via VCO_OLLAMA_KEEP_ALIVE (any value Ollama accepts: a duration like
# "30m"/"2h", "-1" for never-evict, or "0" to opt back into immediate unload).
# An empty override string means "send no keep_alive" (defer to Ollama's own
# default / the OLLAMA_KEEP_ALIVE server env) — an explicit opt-out.
_KEEP_ALIVE_DEFAULT = "24h"


def _keep_alive() -> str | None:
    """Resolve the keep_alive value to attach to embed requests.

    Returns the string to send, or ``None`` to send no keep_alive field
    (explicit opt-out via an empty ``VCO_OLLAMA_KEEP_ALIVE``). Respecting an
    explicit user override is required by the task-6 spec.
    """
    val = os.environ.get("VCO_OLLAMA_KEEP_ALIVE")
    if val is None:
        return _KEEP_ALIVE_DEFAULT
    val = val.strip()
    if val == "":
        # Explicit opt-out: caller wants Ollama's own default behaviour.
        return None
    return val


def _with_keep_alive(body: dict[str, Any]) -> dict[str, Any]:
    """Return ``body`` with ``keep_alive`` added when one is configured.

    Mutates a copy, never the caller's dict. Adding the key is a no-op when
    the user opted out (``_keep_alive()`` returns ``None``).
    """
    ka = _keep_alive()
    if ka is None:
        return body
    out = dict(body)
    out["keep_alive"] = ka
    return out


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
        timeout: TOTAL per-request wall-clock deadline in seconds. Default
            60s when constructed bare; EmbeddingService threads the resolved
            ``VCT_EMBED_REQUEST_TIMEOUT_SECS`` value (180s default) in.
            v0.2.70 FIX A: embed POSTs go through :func:`bounded_post`, which
            enforces this as a *total* deadline for the whole request rather
            than the inter-byte read gap a scalar ``requests`` timeout gives —
            so a dribbling/wedged Ollama socket fails THIS chunk's request
            instead of hanging forever. Health/discovery GETs keep the plain
            scalar ``min(timeout, 5s)`` (a probe that dribbles isn't the wedge
            we're guarding, and clamping keeps liveness checks snappy).
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

    def embed(self, model: str, text: str, num_ctx: int | None = None) -> list[float]:
        """Embed a single text with the named model.

        Tries ``/api/embed`` first (newer Ollama), falls back to
        ``/api/embeddings`` on 404 (older Ollama).

        ``num_ctx`` controls the Ollama context window. When the caller
        passes ``None`` (the v0.2.47+ default), it's auto-resolved from
        ``MODEL_TOKEN_LIMITS`` in ``claude_mcp_servers.weaviate_mcp.chunking``
        so it matches the chunker's per-model target. Pre-v0.2.47 default
        was a hard 8192 which silently truncated longer inputs for
        qwen3-embedding (whose chunker preset wants up to ~13.5k).

        Raises:
            RuntimeError: On non-2xx responses other than the 404 that
                triggers the legacy fallback.
        """
        if num_ctx is None:
            num_ctx = _num_ctx_for_model(model)
        # Try the modern batched endpoint first.
        # v0.2.70 FIX A: bounded_post enforces a TOTAL per-request deadline so a
        # dribbling/wedged socket fails this single chunk instead of hanging.
        try:
            response = bounded_post(
                self.session,
                f"{self.base_url}/api/embed",
                # task 6: keep_alive pins the model resident (removes the ~1.9 s
                # reload after any idle gap). Idempotent — refreshes the TTL.
                json=_with_keep_alive({
                    "model": model,
                    "input": text,
                    "options": {"num_ctx": num_ctx},
                }),
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
        num_ctx: int | None = None,
    ) -> list[list[float]]:
        """Embed a batch of texts with the named model.

        On modern Ollama this is ONE HTTP call per batch (server handles
        the loop). On legacy Ollama (``/api/embed`` returns 404) we fall
        back to one HTTP call per text. Order is preserved in both
        cases.

        An empty ``texts`` list returns an empty list without making
        any HTTP call.

        ``num_ctx`` matches ``embed()``'s behavior — None auto-resolves
        via ``_num_ctx_for_model``. See ``embed`` docstring for rationale.

        Raises:
            RuntimeError: On non-2xx responses other than the 404 that
                triggers the legacy fallback.
        """
        if not texts:
            return []

        if num_ctx is None:
            num_ctx = _num_ctx_for_model(model)

        # v0.2.70 FIX A: a batch is still ONE HTTP request — the bounded total
        # deadline applies per-batch (the embed unit), never per-node.
        try:
            response = bounded_post(
                self.session,
                f"{self.base_url}/api/embed",
                # task 6: keep_alive pins the model resident across batches.
                json=_with_keep_alive({
                    "model": model,
                    "input": texts,
                    "options": {"num_ctx": num_ctx},
                }),
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
        # v0.2.70 FIX A: bounded total deadline, same per-request granularity.
        try:
            response = bounded_post(
                self.session,
                f"{self.base_url}/api/embeddings",
                # task 6: keep_alive on the legacy endpoint too.
                json=_with_keep_alive({"model": model, "prompt": text}),
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
