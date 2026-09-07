# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Input-coverage reporting for Ollama embeds via ``prompt_eval_count``.

SCOPE (corrected v0.2.92 round-3): this module can prove an input was seen
WHOLE (count < window). It cannot prove the opposite — a count EQUAL to the
window means the window was filled, which an exact fit and a truncation
produce identically. Since every embed now sends ``truncate: false`` (an
over-window input is refused, not silently truncated), truncation is known
from the REFUSAL and from whether the caller bounded the text itself — not
from this number.

Ollama's ``/api/embed`` response (and its legacy ``/api/embeddings`` sibling)
carries ``prompt_eval_count`` — the TRUE number of tokens the runner actually
embedded. When that count equals the ``num_ctx`` we sent, the input was
TRUNCATED at the window (Ollama still returns HTTP 200 and pins the count
there); when it is below the window, the whole input fit. This build has no
``/api/tokenize`` endpoint (404), so the count on calls we already make is
the only EXACT measurement available — the char-ratio budget in
``vco_lib.embedding_service`` remains the ESTIMATE for the paths where the
count cannot be obtained.

``OllamaAdapter.embed`` returns only the vector and discards the rest of the
payload. Rather than fork its request/parse ladder into a second copy (the
drift hazard the mirror rules exist to prevent),
:class:`TruncationAwareOllamaAdapter` SUBCLASSES it, reuses the inherited
``embed`` verbatim, and recovers the count by observing the responses on the
session the adapter already owns: the injected ``requests.Session`` is wrapped
in a delegating proxy that records ``prompt_eval_count`` keyed by
``(endpoint, input text)``.

Why KEYED and not a "last response" slot: ``bounded_post`` runs the actual
``session.post`` on a shared executor WORKER thread, so the record happens on
a different thread than the caller — and the embedding service shares one
session across its thread pool, so a single last-response slot would let one
thread's embed overwrite another's between the POST and the count read. The
input text is a deterministic correlation key: ``embed_with_truncation`` knows
exactly which text it passed, and only INT counts are ever recorded (a failed
or count-less response never clobbers a previously recorded good value).
"""

from __future__ import annotations

import threading
from typing import Any

from requests import Session

from vco_lib.embedding_providers.ollama import OllamaAdapter, _num_ctx_for_model

__all__ = ["TruncationAwareOllamaAdapter"]

#: Bounded memory: recorded counts are keyed by full input texts, so the map
#: is capped (FIFO eviction, same pattern as the service's embed memo).
_MAX_RECORDED_COUNTS = 128


class _ResponseCapturingSession:
    """Delegating ``requests.Session`` proxy recording per-input token counts.

    Records ``prompt_eval_count`` from single-item embed POST bodies, keyed by
    ``(url, input text)`` — see the module docstring for why a plain
    last-response slot is not safe here (executor-thread displacement +
    session sharing). Only INT counts are recorded; batch bodies (``input``
    is a list) are skipped: one aggregate count cannot answer per-item
    truncation. Everything except ``post`` is delegated untouched (``get``
    for health / discovery, ``close``, headers, ...) so the wrapping is
    invisible to the inherited adapter code.
    """

    def __init__(self, inner: Session) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, str], int] = {}

    def post(self, url: str, *args: Any, **kwargs: Any) -> Any:
        response = self._inner.post(url, *args, **kwargs)
        try:
            self._record(url, kwargs, response)
        except Exception:  # noqa: BLE001 — recording must never break the embed
            pass
        return response

    def _record(self, url: str, post_kwargs: dict, response: Any) -> None:
        body = post_kwargs.get("json")
        if not isinstance(body, dict):
            return
        # Modern /api/embed bodies key the text as "input"; the legacy
        # /api/embeddings fallback keys it as "prompt". Record either shape.
        text = body.get("input")
        if not isinstance(text, str):
            text = body.get("prompt")
        if not isinstance(text, str):
            return  # batch body — no per-item count to record
        count = _extract_prompt_eval_count(response)
        if count is None:
            return  # never clobber a previously recorded good value
        key = (str(url), text)
        with self._lock:
            if len(self._counts) >= _MAX_RECORDED_COUNTS and key not in self._counts:
                self._counts.pop(next(iter(self._counts)))
            self._counts[key] = count

    def prompt_eval_count_for(self, url_suffix: str, text: str) -> "int | None":
        """The recorded count for the most recent POST of ``text`` to a URL
        ending in ``url_suffix`` (or None when nothing was recorded)."""
        with self._lock:
            for (url, key_text), count in self._counts.items():
                if key_text == text and url.endswith(url_suffix):
                    return count
            return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _extract_prompt_eval_count(response: Any) -> "int | None":
    """Best-effort ``prompt_eval_count`` off a response object (or None).

    ``requests`` caches response content, so parsing here after (or before)
    the adapter's own ``.json()`` call is safe.
    """
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 — best-effort by contract
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("prompt_eval_count")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


class TruncationAwareOllamaAdapter(OllamaAdapter):
    """``OllamaAdapter`` that can report EXACT input truncation per embed.

    The inherited ``embed`` / ``embed_batch`` / health / discovery methods are
    used VERBATIM — this subclass adds no second copy of the HTTP ladder. The
    one addition is :meth:`embed_with_truncation`, which runs the inherited
    single-item embed and then looks up the ``prompt_eval_count`` the session
    proxy recorded for THAT call (works for both the modern ``/api/embed``
    endpoint and the legacy ``/api/embeddings`` 404 fallback — whichever POST
    the inherited path issued is the one recorded under its URL).
    """

    def __init__(self, base_url: str, session: Session, timeout: float = 60.0) -> None:
        super().__init__(
            base_url=base_url,
            # The proxy DELEGATES to a real Session rather than subclassing it,
            # so it duck-types the base class's declared `session` parameter.
            # (The rule name here was `argType` until v0.2.92 — not a pyright
            # rule, so the suppression suppressed nothing and the error stood.)
            session=_ResponseCapturingSession(session),  # pyright: ignore[reportArgumentType]
            timeout=timeout,
        )

    def embed_with_truncation(
        self,
        model: str,
        text: str,
        num_ctx: int | None = None,
    ) -> "tuple[list[float], bool | None]":
        """Embed one text and report whether Ollama SAW the whole input.

        Returns ``(vector, truncated)`` where ``truncated`` is:

          * ``False`` — EXACT: ``prompt_eval_count`` came back BELOW the
            window, so the runner demonstrably saw the whole input.
          * ``None`` when the count EQUALS the window — see below. This is
            deliberately not ``True``.
          * ``None`` — no usable count was recorded for this call (older
            Ollama on the legacy endpoint, unexpected payload shape). The
            caller must fall back to its char-ratio estimate; this is the
            "response not available" leg.

        ``num_ctx=None`` auto-resolves exactly like the inherited ``embed``
        (same resolver, same table), so the window compared against is always
        the window that was actually sent.

        Raises exactly what the inherited ``embed`` raises (network error,
        non-2xx, malformed payload) — no new failure modes, so a caller's
        existing exception handling is unchanged.
        """
        window = num_ctx if num_ctx is not None else _num_ctx_for_model(model)
        vector = self.embed(model, text, num_ctx=window)
        if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
            return vector, None
        capturing = self.session
        lookup = (
            capturing.prompt_eval_count_for
            if isinstance(capturing, _ResponseCapturingSession)
            else None
        )
        if lookup is None:
            return vector, None
        count = (
            lookup("/api/embed", text)
            or lookup("/api/embeddings", text)
        )
        if count is None:
            return vector, None
        if count < window:
            # Demonstrably whole: the runner reported fewer tokens than the
            # window, so nothing was dropped.
            return vector, False
        # count == window is AMBIGUOUS and must not be reported as truncation
        # (v0.2.92 round-3). It means the window was FILLED, which is either an
        # exact fit or a truncation — and the two are indistinguishable from
        # this number alone.
        #
        # Under the shipped configuration it is almost always an exact fit:
        # every embed request now sends ``truncate: false``, so an over-window
        # input is REFUSED with HTTP 400 rather than returning 200 with a
        # pinned count. A 200 that reaches this line therefore describes an
        # input the runner accepted whole.
        #
        # Returning True here would have been a false positive on exactly the
        # inputs that fit best. Returning None hands the caller its
        # char-ratio estimate, which is the honest "could not determine".
        return vector, None
