# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""OpenAI embeddings adapter.

OpenAI's embeddings API has two surfaces this adapter cares about:

  * ``GET https://api.openai.com/v1/models/<model>`` — the validation
    probe. FREE (no tokens consumed, no billing entry) per
    `<https://platform.openai.com/docs/api-reference/models/retrieve>`_.
    Returns 200 with model metadata if the key has access to that exact
    model; 401 for invalid key; 404 for valid key but model inaccessible
    (project restriction or org without that tier); 403 for blocked; 429
    for rate-limited (treat as valid). This is the LOCKED validation
    method per the v0.2.18 plan — cheaper than the older
    ``POST /v1/embeddings`` "hi" probe (which IS billed, even at $0
    rounded).

  * ``POST https://api.openai.com/v1/embeddings`` — the actual embedding
    call. Accepts ``{"model": "text-embedding-3-small", "input": str | list[str]}``,
    returns ``{"data": [{"embedding": [...], "index": 0}, ...], "model": "...", "usage": {...}}``.
    OpenAI batches up to 2048 inputs per call but the server-side token
    limit is 300k tokens total — for safety we chunk at 100 items per
    call (well under any limit).

Models recognised here:

  * ``text-embedding-3-small`` — 1536 dim, ``$0.02/1M`` tokens. The
    locked default per v0.2.18 plan.
  * ``text-embedding-3-large`` — 3072 dim, ``$0.13/1M`` tokens. Exposed
    in the GUI dropdown for users who want the higher-quality vectors.
  * ``text-embedding-ada-002`` — 1536 dim. Legacy; kept here for users
    with existing collections.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

from vco_lib.embedding_providers._http import bounded_post

# Known OpenAI embedding models. Used by catalog discovery so the GUI
# can show dim before any call is made. Future models can be added
# here without code changes elsewhere — they'll surface in the dropdown
# automatically.
KNOWN_OPENAI_EMBEDDING_MODELS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}

# Max items per /v1/embeddings call. Server allows 2048 but we keep it
# conservative to avoid token-budget surprises (each input can be up to
# 8191 tokens, so 100 * 8191 = 819100 tokens — well under 300k server cap
# when most inputs are short).
MAX_BATCH_SIZE = 100

# How long to cache a successful validation result, in seconds. Catalog
# discovery can re-render frequently (every GUI page load); avoid
# hammering the API.
VALIDATION_CACHE_TTL = 30.0

API_BASE = "https://api.openai.com/v1"


@dataclass
class ValidationResult:
    """Outcome of an OpenAI key validation probe.

    ``valid`` is True iff the key proved usable for the probed model
    (HTTP 200 on ``GET /v1/models/<model>`` OR 429 rate-limited).
    ``reason`` carries the human-readable cause when ``valid`` is False
    or when ``rate_limited`` is True.
    """

    valid: bool
    reason: str | None = None
    rate_limited: bool = False
    http_status: int | None = None


class OpenAIAdapter:
    """Adapter for the OpenAI embeddings API.

    The adapter never persists the API key — it's passed in once at
    construction and lives only in the instance. ``EmbeddingService``
    is responsible for reading it from env (or keyring, via Rust
    sidecar) and feeding it here.

    Attributes:
        api_key: OpenAI API key (``sk-...`` or ``sk-proj-...``). Empty
            string is a valid "no key configured" sentinel and makes
            every method short-circuit to "not available".
        session: Injected ``requests.Session`` for connection pooling.
        timeout: TOTAL per-request wall-clock deadline in seconds. Default
            30s when constructed bare; EmbeddingService threads the resolved
            ``VCT_EMBED_REQUEST_TIMEOUT_SECS`` value in. v0.2.70 FIX A: the
            ``/v1/embeddings`` POST goes through :func:`bounded_post` so the
            deadline bounds the whole request (one batch = the embed unit),
            not the inter-byte read gap. Validation GETs keep the plain scalar.
    """

    def __init__(
        self,
        api_key: str,
        session: requests.Session,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or ""
        self.session = session
        self.timeout = timeout
        # validation cache: (model_id, ValidationResult, expires_at_monotonic)
        self._validation_cache: dict[str, tuple[ValidationResult, float]] = {}

    # ---- key validation (FREE) ---------------------------------------------

    def validate(
        self,
        model: str = "text-embedding-3-small",
    ) -> ValidationResult:
        """Validate the configured key via ``GET /v1/models/<model>``.

        This is the locked free-probe per the v0.2.18 design:

          * 200 → key valid AND can access the exact model.
          * 401 → invalid/revoked key (``valid=False``, reason="auth failed").
          * 403 → valid key but blocked (``valid=False``, reason="blocked").
          * 404 → valid key but model not accessible to this key
            (``valid=False``, reason="model not accessible").
          * 429 → rate-limited; treat as valid (``valid=True, rate_limited=True``).
          * other → network/server error (``valid=False``, reason="HTTP <code>").

        Results are cached for ``VALIDATION_CACHE_TTL`` seconds per
        model to avoid hammering the API on catalog re-renders.

        Returns:
            ValidationResult with ``valid`` set by the rules above.
            ``valid`` is False with reason "no API key configured" when
            ``self.api_key`` is empty.
        """
        if not self.api_key:
            return ValidationResult(
                valid=False,
                reason="no API key configured",
            )

        # Cache hit?
        now = time.monotonic()
        cached = self._validation_cache.get(model)
        if cached is not None:
            result, expires_at = cached
            if now < expires_at:
                return result

        try:
            response = self.session.get(
                f"{API_BASE}/models/{model}",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            result = ValidationResult(
                valid=False,
                reason=f"network error: {exc}",
            )
            # Don't cache network errors — they're transient.
            return result

        status = response.status_code
        if status == 200:
            result = ValidationResult(valid=True, http_status=200)
        elif status == 401:
            result = ValidationResult(
                valid=False,
                reason="auth failed",
                http_status=401,
            )
        elif status == 403:
            result = ValidationResult(
                valid=False,
                reason="key blocked for this model",
                http_status=403,
            )
        elif status == 404:
            result = ValidationResult(
                valid=False,
                reason="model not accessible to this key",
                http_status=404,
            )
        elif status == 429:
            result = ValidationResult(
                valid=True,
                rate_limited=True,
                reason="rate limited (treated as valid)",
                http_status=429,
            )
        else:
            result = ValidationResult(
                valid=False,
                reason=f"unexpected HTTP {status}: {response.text[:200]}",
                http_status=status,
            )

        self._validation_cache[model] = (result, now + VALIDATION_CACHE_TTL)
        return result

    def is_reachable(self) -> bool:
        """Return True if the key validates for ``text-embedding-3-small``.

        Convenience for catalog discovery — equivalent to
        ``validate().valid``.
        """
        return self.validate().valid

    # ---- embed --------------------------------------------------------------

    def embed(self, model: str, text: str) -> list[float]:
        """Embed a single text via ``POST /v1/embeddings``.

        Raises:
            RuntimeError: On non-2xx response or missing/empty key.
        """
        results = self.embed_batch(model, [text])
        return results[0]

    def embed_batch(
        self,
        model: str,
        texts: list[str],
    ) -> list[list[float]]:
        """Embed a batch of texts (auto-chunks at MAX_BATCH_SIZE).

        Empty input returns empty output without an HTTP call.

        Raises:
            RuntimeError: On non-2xx response or missing API key.
        """
        if not self.api_key:
            raise RuntimeError(
                "OpenAI API key not configured "
                "(OPENAI_API_KEY env or keyring entry)"
            )
        if not texts:
            return []

        if len(texts) <= MAX_BATCH_SIZE:
            return self._embed_chunk(model, texts)

        out: list[list[float]] = []
        for i in range(0, len(texts), MAX_BATCH_SIZE):
            out.extend(self._embed_chunk(model, texts[i : i + MAX_BATCH_SIZE]))
        return out

    def _embed_chunk(self, model: str, texts: list[str]) -> list[list[float]]:
        """One HTTP call to ``/v1/embeddings`` for a chunk ≤ MAX_BATCH_SIZE."""
        # v0.2.70 FIX A: bounded total deadline per embed request (= one batch).
        try:
            response = bounded_post(
                self.session,
                f"{API_BASE}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": model, "input": texts},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"OpenAI /v1/embeddings network error: {exc}") from exc

        if response.status_code != 200:
            raise RuntimeError(
                f"OpenAI /v1/embeddings returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"OpenAI /v1/embeddings returned non-JSON: {response.text[:500]}"
            ) from exc

        data = payload.get("data")
        if not isinstance(data, list):
            raise RuntimeError(
                f"OpenAI /v1/embeddings returned no data array: {payload!r}"
            )
        if len(data) != len(texts):
            raise RuntimeError(
                f"OpenAI /v1/embeddings returned {len(data)} embeddings "
                f"for {len(texts)} inputs — order cannot be reconstructed"
            )

        # OpenAI guarantees the `index` field matches the input position
        # so we sort defensively in case ordering ever changes.
        try:
            sorted_data = sorted(data, key=lambda d: int(d.get("index", 0)))
        except (TypeError, ValueError):
            sorted_data = data

        result: list[list[float]] = []
        for i, item in enumerate(sorted_data):
            vec = item.get("embedding")
            if not isinstance(vec, list):
                raise RuntimeError(
                    f"OpenAI /v1/embeddings returned malformed embedding at "
                    f"index {i}: {item!r}"
                )
            result.append([float(x) for x in vec])
        return result
