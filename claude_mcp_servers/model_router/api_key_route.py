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


#: The INTERVAL (not a time-to-live: nothing expires on it) at which a
#: :class:`CachedSetting` re-reads its launcher setting in the background,
#: whether or not any request arrives (review R17 MEDIUM-1). A flipped switch
#: applies within this interval plus one launcher-DB read: milliseconds
#: normally; a read that waits on a locked DB gives up after the reader's 5 s
#: busy timeout, the PREVIOUS value is kept (never the default — review R19
#: LOW-1), and the next tick retries. So: about 5 s, 10 s when one read hits
#: a lock, and longer only while the launcher DB stays unreadable — during
#: which the gateway keeps applying the last value it read.
LAUNCHER_SETTING_REFRESH_S = 5.0

_T = TypeVar("_T")


class _Unreadable:
    """A failed read — not a value (see :class:`CachedSetting`)."""

    def __init__(self, error: BaseException) -> None:
        self.error = error


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
    :data:`LAUNCHER_SETTING_REFRESH_S` plus one read, however long the gateway
    sat idle — the first request after a switch is flipped already sees it.

    A reader that RAISES (the launcher DB exists but cannot be read) is not
    an answer: :meth:`refresh` keeps the cached value and logs once per
    failure streak (and once on recovery). An absent row or DB IS an answer
    (the reader returns the default itself). Before any value exists
    (priming failed or has not run), :meth:`get` answers ``default`` —
    ``oauth`` / OFF, the choices that never bill a different pool. Never
    raises.
    """

    def __init__(self, reader: Callable[[], _T], default: _T, what: str) -> None:
        self.reader = reader
        self.default = default
        self._what = what
        self._cache: Optional[tuple[float, _T]] = None
        self._periodic: Optional[asyncio.Task] = None
        #: Inside a streak of failed reads (one log line per streak).
        self._failing = False
        #: Seconds between background re-reads (tests shorten it).
        self.interval_s = LAUNCHER_SETTING_REFRESH_S

    def _read(self) -> "_T | _Unreadable":
        try:
            return self.reader()
        except Exception as exc:  # noqa: BLE001 — see the class docstring
            return _Unreadable(exc)

    def _settle(self, value: "_T | _Unreadable") -> _T:
        """Apply one read: cache an answer; on a failure keep the cache."""
        if isinstance(value, _Unreadable):
            if not self._failing:
                self._failing = True
                logger.warning(
                    "model-gateway: could not read %s (%s); keeping %s until "
                    "it reads again", self._what, value.error,
                    "its last value" if self._cache is not None
                    else "its default",
                )
            return self._cache[1] if self._cache is not None else self.default
        if self._failing:
            self._failing = False
            logger.info("model-gateway: %s reads again", self._what)
        self._cache = (time.monotonic(), value)
        return value

    async def refresh(self) -> _T:
        """Re-read now, off the loop; returns the value now in force."""
        return self._settle(await asyncio.to_thread(self._read))

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
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop: a synchronous caller (``vct-model-gateway --check``)
            # shares this thread with nothing, so it reads the row directly
            # — every call, nothing cached for it (it has no refresh task).
            value = self._read()
            if isinstance(value, _Unreadable):
                return self._cache[1] if self._cache is not None else self.default
            return value
        cached = self._cache
        return cached[1] if cached is not None else self.default

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
    """Whether a refusal condemns the KEY itself (reviews R17 LOW-1, R19 NIT-2).

    * 401 — credential-level by definition: yes.
    * 403 — yes only for Anthropic's ``permission_error`` whose message names
      no model. Claude Code itself treats a ``permission_error`` 403 whose
      message carries ``model:`` as a MODEL refusal (a workspace restricting
      which models a key may call); this check is wider — any "model" in the
      message — so the colon-less phrasing ("The requested model is not
      available in your workspace") counts as model-level too. A 403 of
      another type, or not JSON at all (an edge/WAF page), is not about the
      key either.

    The bias is deliberate: when in doubt the key is NOT condemned. A wrong
    "available" costs one explicit local 503 per request, quoting Anthropic's
    own text; a wrong "rejected" tells the user to replace a working key and
    disables the launcher's API-key choice. The request that got the 403
    receives the local answer either way.
    """
    if status == 401:
        return True
    if status != 403:
        return False
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return False
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(error, dict) or error.get("type") != "permission_error":
        return False
    message = error.get("message")
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




def launcher_settings(
) -> "tuple[CachedSetting[bool], CachedSetting[str]]":
    """The two launcher settings the first-party route reads, built ONCE here
    for every consumer (the gateway, ``--check``): the opt-in subscription
    fallback (default OFF) and the global auth mode (default ``oauth``)."""
    from .config import (  # noqa: PLC0415 — config imports vco_lib
        ANTHROPIC_AUTH_OAUTH,
        anthropic_auth_mode,
        api_key_fallback_enabled,
    )

    return (
        CachedSetting(api_key_fallback_enabled, False, "the API-key fallback setting"),
        CachedSetting(anthropic_auth_mode, ANTHROPIC_AUTH_OAUTH, "the Anthropic auth mode setting"),
    )
