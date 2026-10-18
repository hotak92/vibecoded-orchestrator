# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The first-party Claude route billed to a Console API key — the pure parts.

Split out of :mod:`model_router.server` (v0.2.104) so that module does not
grow past its size budget: everything here is independent of a running
request — the header swap, the refused-key answer text, the launcher
settings cache — and is unit-testable without the proxy. The request-flow
half (the subscription fallback escalation, the refusal branch in the relay)
stays in ``server.py`` beside the code it threads through. No vendor
literals: the route's facts arrive as an
:class:`~model_router.vendors.AnthropicApiKey`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Generic, Mapping, Optional, TypeVar

from .vendors import (
    CONTEXT_1M_BETA,
    CONTEXT_1M_BETA_MARKER,
    OAUTH_BETA,
    AnthropicApiKey,
)

logger = logging.getLogger(__name__)


#: How often a :class:`CachedSetting` re-reads its launcher setting in the
#: background, whether or not any request arrives (review R17 MEDIUM-1: a
#: refresh triggered only by a request left the FIRST request after an idle
#: gap on the old value). Worst case for a flipped switch to apply: this
#: interval plus one launcher-DB read (milliseconds; at most the reader's
#: 5 s SQLite busy timeout on a locked DB) — about 5 s, 10 s at the very
#: worst — independent of traffic or idle time.
LAUNCHER_SETTING_TTL_S = 5.0

_T = TypeVar("_T")


class CachedSetting(Generic[_T]):
    """One launcher setting, refreshed OFF the event loop (reviews R15 LOW-1,
    R17 MEDIUM-1).

    The reader opens the launcher's SQLite DB — blocking file I/O that must
    never run on the loop every in-flight stream shares. So on the loop
    :meth:`get` never reads: it answers the cache. The cache is kept current
    by ONE periodic background task (:meth:`start_periodic`, started by
    ``Gateway.start`` after it awaited :meth:`refresh` to prime the value
    before the first request): every :attr:`interval_s` it re-reads through
    ``asyncio.to_thread``. So the value a request sees is never older than
    :data:`LAUNCHER_SETTING_TTL_S` plus one read, however long the gateway
    sat idle — the first request after a switch is flipped already sees it.

    Before any value exists (priming failed or has not run), :meth:`get`
    answers ``default`` — ``oauth`` / OFF, the choices that never bill a
    different pool. Never raises: a reader failure also answers ``default``.
    """

    def __init__(self, reader: Callable[[], _T], default: _T, what: str) -> None:
        self.reader = reader
        self.default = default
        self._what = what
        self._cache: Optional[tuple[float, _T]] = None
        self._periodic: Optional[asyncio.Task] = None
        #: Seconds between background re-reads (tests shorten it).
        self.interval_s = LAUNCHER_SETTING_TTL_S

    def _read(self) -> _T:
        try:
            return self.reader()
        except Exception:  # noqa: BLE001 — see the class docstring
            logger.exception(
                "model-gateway: could not read %s; using its default",
                self._what,
            )
            return self.default

    async def refresh(self) -> _T:
        """Re-read now, off the loop, and cache the answer."""
        value = await asyncio.to_thread(self._read)
        self._cache = (time.monotonic(), value)
        return value

    async def _refresh_forever(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the loop must outlive one bad read
                logger.exception(
                    "model-gateway: background refresh of %s failed; keeping "
                    "the last value", self._what,
                )

    def start_periodic(self) -> None:
        """Start the ONE background refresh task (idempotent)."""
        task = self._periodic
        if task is None or task.done():
            self._periodic = asyncio.get_running_loop().create_task(
                self._refresh_forever(), name="model-gateway-setting-refresh",
            )

    def get(self) -> _T:
        """The cached value; never reads on a running loop."""
        cached = self._cache
        if cached is not None:
            return cached[1]
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop (a synchronous caller such as ``--check``): nothing
            # shares this thread, so the read may run here.
            self._cache = (time.monotonic(), self._read())
            return self._cache[1]
        return self.default

    async def stop(self) -> None:
        """Cancel the background refresh (gateway shutdown)."""
        task = self._periodic
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — a cancelled refresh is the point
                pass

    def expire(self) -> None:
        """Forget the cached value (tests): ``get`` answers the default
        until the next refresh lands."""
        self._cache = None

#: The route label (access line ``route=``, ledger ``route``) of the
#: first-party route billed to the Console API key. Distinct from
#: ``anthropic`` so the ledger's per-route token totals never fold API-billed
#: turns into the subscription's.
API_KEY_ROUTE_LABEL = "anthropic-api"

#: Upstream statuses that mean Anthropic refused the CONSOLE KEY on the
#: API-key route (invalid / revoked key, organisation disabled, no access).
API_KEY_REFUSAL_STATUSES = (401, 403)


def api_key_refusal_is_key_level(status: int, raw: bytes) -> bool:
    """Whether a refusal condemns the KEY itself (review R17 LOW-1).

    A 401 is credential-level by definition. A 403 ``permission_error`` is
    often MODEL-level — a Console workspace restricting which models a key
    may call names the model (``"... model: claude-opus-5-5"``) — and says
    nothing about the key's other uses, so it must not mark the key
    ``rejected`` machine-wide. Such a 403 still gets the local answer for
    THAT request. A 403 that names no model is treated as key-level.
    """
    if status == 401:
        return True
    if status != 403:
        return False
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return True
    error = parsed.get("error") if isinstance(parsed, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return not (isinstance(message, str) and "model" in message.lower())


class ApiKeyRefused(Exception):
    """Raised by :func:`_proxy` on the subscription FALLBACK's key attempt,
    so the escalation can continue the ladder instead of relaying it."""

    def __init__(self, status: int) -> None:
        super().__init__(f"API key refused (HTTP {status})")
        self.status = status


#: Headers on the gateway's answer to a refused key: the Anthropic SDK reads
#: ``x-should-retry: false`` and does not retry (re-sending a key Anthropic
#: rejected cannot succeed). Claude Code itself may re-send a 503 ONCE with a
#: dispatch header (a latched, single re-send — no loop); that re-send gets
#: the same local answer, and refusals are not billed.
API_KEY_REFUSED_HEADERS = {"x-should-retry": "false"}

#: The client-facing status / error type for a refused key. Chosen
#: deliberately: NOT a 401/403 ``authentication_error``/``permission_error``,
#: which Claude Code reads as ITS login failing and answers by asking the user
#: to log in again — the login is fine; the gateway's key is what was
#: refused. A 503 ``api_error`` is a server-side condition the client shows
#: as a message, and :data:`API_KEY_REFUSED_HEADERS` stops the SDK retrying.
API_KEY_REFUSED_STATUS = 503
API_KEY_REFUSED_ERROR_TYPE = "api_error"


def api_key_refused_message(api: AnthropicApiKey, status: int, raw: bytes) -> str:
    """The text of the gateway's answer when Anthropic refuses the key on the
    EXPLICIT ``<namespace><id>`` route or under the global API-key mode.

    Names the secret and both remedies; quotes Anthropic's own error message
    (which never contains the key); the key itself never appears.
    """
    detail = ""
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            detail = f" Anthropic said: {error['message'][:300]}"
    except ValueError:
        pass
    secret = api.secret_keys[0]
    return (
        f"Anthropic refused the Claude API key the model gateway uses "
        f"({secret}, HTTP {status}) — your Claude login is not affected.{detail} "
        f"Replace the key in the launcher (Secrets → Shared → {secret}), or "
        "switch the launcher status bar to \"Anthropic: OAuth\" / pick a model "
        f"without the {api.namespace} prefix."
    )


def api_key_headers(
    headers: Mapping[str, str],
    api: AnthropicApiKey,
    key: str,
    *,
    one_m_requested: bool,
) -> dict[str, str]:
    """Forwarded headers for the first-party route billed to the API key.

    The ONE home of the auth swap, used by the explicit ``claude-gw/api/``
    route and by the subscription fallback alike. The client's headers travel
    as they arrived — ``user-agent``, ``x-app`` and every other header that
    identifies the client are NOT touched: how Anthropic classifies and bills
    this traffic is its decision, and a gateway that disguised the client
    would be making it instead. Exactly three things change:

    * any ``Authorization`` / ``x-api-key`` is dropped (the Claude login is
      not this route's credential — the fallback copies headers that carry
      it);
    * the Console key is set under ``api.auth_header``;
    * the OAuth beta is removed from ``anthropic-beta`` — it marks an OAuth
      request — while every other beta is kept, and the 1M-window beta is
      added when the requested id carried ``[1m]`` and the client did not
      already ask for the window.
    """
    out: dict[str, str] = {}
    beta_values: list[str] = []
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in ("authorization", "x-api-key"):
            continue
        if lowered == "anthropic-beta":
            beta_values.append(value)
            continue
        out[name] = value
    betas = [
        token.strip()
        for value in beta_values
        for token in value.split(",")
        if token.strip() and token.strip() != OAUTH_BETA
    ]
    if one_m_requested and not any(CONTEXT_1M_BETA_MARKER in b for b in betas):
        betas.append(CONTEXT_1M_BETA)
    if betas:
        out["anthropic-beta"] = ",".join(betas)
    out[api.auth_header] = key
    return out


