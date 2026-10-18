# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The subscription-out latch (v0.2.104, owner 2026-10-18).

With the opt-in subscription -> API-key fallback ON, every first-party
request used to try the subscription first, collect the quota refusal and
only then go to the key — one wasted round trip per turn for the rest of a
5-hour or weekly window. The owner's rule: once the gateway knows the
subscription is out, learn WHICH limit is out and UNTIL WHEN, and do not ask
the subscription again before then.

What arms a latch is NOT a second classifier: it is the existing OD-9
escalation — the ladder branch decided the refusal qualifies, and the
API-key step of the escalation (``server._api_key_escalation``) resolved a
key and is about to send. That step hands the refusal here
(:meth:`QuotaLatch.note_refusal`), and this module only reads WHICH limit and
WHEN it resets, from the strongest source present:

1. **The refusal's unified rate-limit headers** — the ones Claude Code itself
   reads: ``anthropic-ratelimit-unified-representative-claim`` names the
   binding limit (``five_hour`` -> ``5h``, every model; ``seven_day`` ->
   ``weekly``, every model; ``seven_day_<model>`` -> a per-model weekly limit
   that latches only the models whose id carries that word, e.g.
   ``seven_day_opus`` -> ``claude-opus-*``), and
   ``anthropic-ratelimit-unified-reset`` (epoch seconds) says until when; the
   per-window ``anthropic-ratelimit-unified-{5h,7d}-reset`` is used when the
   overall reset is absent. Without a claim, a per-window
   ``anthropic-ratelimit-unified-{5h,7d}-status: rejected`` (or a
   ``-utilization`` of 1.0 or more) names the window.
2. **The usage-windows snapshot** (:mod:`model_router.usage_windows`): a
   fresh ``5h`` / ``weekly`` reading at 100 % latches every model; a fresh
   per-model ``weekly:<Name>`` reading at 100 % latches the models it covers,
   and only when the refused model is one of them. Its ``resets_at`` is the
   time.
3. **A reset hint the refusal carries anywhere else** (``retry-after``, a
   body ``resets_at`` — read by the same helpers the quota message reads):
   the limit is ``unknown``, so only the refused model's own family is
   latched (a guess about the other models would spend money on them).
4. **Nothing**: a conservative latch of :data:`CONSERVATIVE_LATCH_S`, again
   for the refused model's family only.

A latch clears at its reset time, when a usage-windows reading taken AFTER
the latch shows the latched window back under 100 % (the usage endpoint keeps
refreshing while chats flow, so a conservative latch normally ends at the next
refresh), or when the key itself is refused (:meth:`QuotaLatch.key_refused`):
requests then go back to the subscription, so the user sees the real
refusal, and no new latch arms until the key is accepted again or the
cleared latch's own time has passed. Each latch and each clear is one INFO
line.

In memory only, for the process's lifetime: a restart forgets it and the next
request re-learns it with one refusal. Every method is synchronous, takes no
I/O and holds one lock for a few dict operations, so it is safe on the event
loop and from any thread.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Optional, Sequence

from .model_family import parse_model_id
from .quota import _find_reset_value, _reset_distance_s
from .usage_windows import Window, effective_percent

logger = logging.getLogger(__name__)

#: How long a latch holds when nothing says when the limit resets: one
#: usage-windows refresh interval including its jitter
#: (``usage_windows.DEFAULT_REFRESH_S`` 240 s + ``DEFAULT_JITTER_S`` 60 s), so
#: the next reading of the windows normally decides first — it clears the
#: latch as soon as it shows the window under 100 %. Short on purpose: an
#: unidentified refusal may have been a brief limiter, and every latched
#: request is billed to the Console key.
CONSERVATIVE_LATCH_S = 300.0

#: No latch outlives this, whatever a header says: the longest Anthropic
#: window is a week; an hour of slack covers clock skew. Never "forever".
MAX_LATCH_S = 7 * 24 * 3600 + 3600

LIMIT_5H = "5h"
LIMIT_WEEKLY = "weekly"
LIMIT_UNKNOWN = "unknown"
#: A per-model weekly limit is ``weekly:<Name>`` — the usage windows' own id.
LIMIT_WEEKLY_MODEL_PREFIX = "weekly:"

#: Scope of a latch on the 5h or the overall weekly limit.
SCOPE_ALL = "all"

SOURCE_REFUSAL_HEADERS = "refusal_headers"
SOURCE_USAGE_WINDOWS = "usage_windows"
SOURCE_RESET_HINT = "reset_hint"
SOURCE_CONSERVATIVE = "conservative"

_UNIFIED = "anthropic-ratelimit-unified-"
_CLAIM_HEADER = f"{_UNIFIED}representative-claim"
_RESET_HEADER = f"{_UNIFIED}reset"
#: Claim values for the windows every model shares.
_CLAIM_LIMITS = {"five_hour": LIMIT_5H, "seven_day": LIMIT_WEEKLY}
#: ``seven_day_<model>`` — a per-model weekly claim.
_PER_MODEL_CLAIM_PREFIX = "seven_day_"
#: Our limit -> the per-window header suffix (``usage_windows`` reads the same).
_WINDOW_HEADER_SUFFIX = {LIMIT_5H: "5h", LIMIT_WEEKLY: "7d"}


@dataclass(frozen=True)
class QuotaRefusal:
    """The subscription's refusal, as the ladder branch read it."""

    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class Latch:
    """One exhausted limit: which, for which models, until when (epoch s)."""

    limit: str
    scope: str
    until: float
    since: float
    source: str

    def covers(self, model: str) -> bool:
        return self.scope == SCOPE_ALL or self.scope in model_words(model)

    def as_dict(self) -> dict:
        return {
            "limit": self.limit,
            "scope": self.scope,
            "until": _iso(self.until),
            "since": _iso(self.since),
            "source": self.source,
        }

    def scope_text(self) -> str:
        return (
            "every Claude model" if self.scope == SCOPE_ALL
            else f"{self.scope.capitalize()} models"
        )


def _iso(epoch_s: float) -> str:
    return (
        datetime.fromtimestamp(epoch_s, timezone.utc).isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def model_words(model: str) -> frozenset[str]:
    """The lowercase name words of a model id (``claude-opus-5-5`` ->
    ``{"claude", "opus"}``), through the ONE id parser."""
    family = parse_model_id(model).family.lower()
    return frozenset(w for w in family.split("-") if w)


def model_scope(model: str) -> str:
    """The narrowest scope a latch on an unidentified limit takes: the
    model's own family word (``opus``), or the bare id when it has none."""
    family = parse_model_id(model).family.lower()
    words = [w for w in family.split("-") if w]
    return words[-1] if words else (model.strip().lower() or SCOPE_ALL)


def _lowered(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(k).lower(): str(v).strip() for k, v in headers.items()}


def _epoch(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number > 0 else None


def _window_limit(window_id: str) -> Optional[tuple[str, str]]:
    """``(limit, scope)`` for a usage-window id, or ``None``."""
    if window_id in (LIMIT_5H, LIMIT_WEEKLY):
        return window_id, SCOPE_ALL
    if window_id.startswith(LIMIT_WEEKLY_MODEL_PREFIX):
        name = window_id[len(LIMIT_WEEKLY_MODEL_PREFIX):].strip()
        if name:
            return window_id, name.lower()
    return None


def _from_headers(
    headers: Mapping[str, str], windows: Sequence[Window], now: float,
) -> list[tuple[str, str, Optional[float]]]:
    """``(limit, scope, until-or-None)`` the refusal's unified headers name."""
    h = _lowered(headers)
    claim = h.get(_CLAIM_HEADER, "").lower()
    overall = _epoch(h.get(_RESET_HEADER))
    if claim in _CLAIM_LIMITS:
        limit = _CLAIM_LIMITS[claim]
        until = overall or _epoch(h.get(f"{_UNIFIED}{_WINDOW_HEADER_SUFFIX[limit]}-reset"))
        return [(limit, SCOPE_ALL, until or _window_reset(windows, limit, now))]
    if claim.startswith(_PER_MODEL_CLAIM_PREFIX) and claim[len(_PER_MODEL_CLAIM_PREFIX):]:
        name = claim[len(_PER_MODEL_CLAIM_PREFIX):]
        limit = f"{LIMIT_WEEKLY_MODEL_PREFIX}{name.capitalize()}"
        return [(limit, name, overall or _window_reset(windows, limit, now))]
    found: list[tuple[str, str, Optional[float]]] = []
    for limit, suffix in _WINDOW_HEADER_SUFFIX.items():
        status = h.get(f"{_UNIFIED}{suffix}-status", "").lower()
        try:
            utilization = float(h.get(f"{_UNIFIED}{suffix}-utilization", ""))
        except ValueError:
            utilization = 0.0
        if status == "rejected" or utilization >= 1.0:
            until = _epoch(h.get(f"{_UNIFIED}{suffix}-reset")) or overall
            found.append((limit, SCOPE_ALL, until or _window_reset(windows, limit, now)))
    return found


def _window_reset(windows: Sequence[Window], limit: str, now: float) -> Optional[float]:
    for window in windows:
        if window.id.lower() == limit.lower() and window.resets_at and window.resets_at > now:
            return window.resets_at
    return None


def _from_windows(
    model: str, windows: Sequence[Window], now: float,
) -> list[tuple[str, str, Optional[float]]]:
    """Fresh usage-window readings at 100 % that apply to ``model``."""
    words = model_words(model)
    found: list[tuple[str, str, Optional[float]]] = []
    for window in windows:
        named = _window_limit(window.id)
        percent = effective_percent(window, now)
        if named is None or percent is None or percent < 100.0:
            continue
        limit, scope = named
        if scope != SCOPE_ALL and scope not in words:
            continue
        found.append((limit, scope, window.resets_at))
    return found


def latches_for_refusal(
    model: str,
    refusal: QuotaRefusal,
    windows: Sequence[Window],
    *,
    now: float,
) -> list[Latch]:
    """Which limit(s) a qualifying subscription refusal exhausted, for which
    models, until when — the four sources of the module docstring, strongest
    first. Pure; always answers at least one latch, never one without an end.
    """
    named = _from_headers(refusal.headers, windows, now)
    source = SOURCE_REFUSAL_HEADERS
    if not named:
        named = _from_windows(model, windows, now)
        source = SOURCE_USAGE_WINDOWS
    if not named:
        distance = _reset_distance_s(_find_reset_value(refusal.body, refusal.headers))
        until = now + distance if distance is not None and distance > 0 else None
        if until is None:
            until = _epoch(_lowered(refusal.headers).get(_RESET_HEADER))
        named = [(LIMIT_UNKNOWN, model_scope(model), until)]
        source = SOURCE_RESET_HINT if until is not None and until > now else SOURCE_CONSERVATIVE
    out: list[Latch] = []
    for limit, scope, until in named:
        if until is None or until <= now:
            # A limit with no usable end still gets a short one, never none
            # and never "forever".
            until = now + CONSERVATIVE_LATCH_S
            if source == SOURCE_RESET_HINT:
                source = SOURCE_CONSERVATIVE
        out.append(Latch(limit, scope, min(until, now + MAX_LATCH_S), now, source))
    return out


def _recovered(latch: Latch, windows: Iterable[Window], now: float) -> bool:
    """A reading taken AFTER the latch shows its window back under 100 %.

    ``unknown`` limits need the 5h AND the weekly window known and under
    100 % (and any per-model weekly window covering the scope too)."""
    fresh = {
        w.id.lower(): pct
        for w in windows
        if w.observed_at > latch.since
        and (pct := effective_percent(w, now)) is not None
    }
    if latch.limit == LIMIT_UNKNOWN:
        needed = [LIMIT_5H, LIMIT_WEEKLY] + [
            wid for wid in fresh
            if wid.startswith(LIMIT_WEEKLY_MODEL_PREFIX)
            and wid[len(LIMIT_WEEKLY_MODEL_PREFIX):] == latch.scope
        ]
    else:
        needed = [latch.limit.lower()]
    return all(wid in fresh and fresh[wid] < 100.0 for wid in needed)


class QuotaLatch:
    """The latches in force, keyed by ``(limit, scope)``. See the module."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._latches: dict[tuple[str, str], Latch] = {}
        #: After the key was refused, no new latch arms before this (epoch s)
        #: or before the key is accepted again — re-latching onto a refused
        #: key would hide the subscription's real refusal from the user.
        self._suppressed_until = 0.0

    def note_refusal(
        self, model: str, refusal: QuotaRefusal, windows: Sequence[Window],
    ) -> list[Latch]:
        """Arm the latch(es) a qualifying refusal of ``model`` implies;
        returns what was armed (empty while suppressed)."""
        now = self._clock()
        armed: list[Latch] = []
        with self._lock:
            if now < self._suppressed_until:
                return []
            for latch in latches_for_refusal(model, refusal, windows, now=now):
                key = (latch.limit, latch.scope)
                held = self._latches.get(key)
                if held is not None and held.until >= latch.until:
                    continue
                self._latches[key] = latch
                armed.append(latch)
        for latch in armed:
            logger.info(
                "model-gateway: subscription %s limit exhausted until %s; sending "
                "%s to the Claude API key (source=%s)",
                latch.limit, _iso(latch.until), latch.scope_text(), latch.source,
            )
        return armed

    def active_for(self, model: str, windows: Sequence[Window] = ()) -> Optional[Latch]:
        """The latch covering ``model`` now (the one that ends last), after
        clearing every latch that has ended or recovered."""
        self._expire(windows)
        with self._lock:
            covering = [item for item in self._latches.values() if item.covers(model)]
        return max(covering, key=lambda item: item.until) if covering else None

    def report(self, windows: Sequence[Window] = ()) -> list[dict]:
        """The latches in force, for ``/health`` and the usage snapshot."""
        self._expire(windows)
        with self._lock:
            latches = sorted(self._latches.values(), key=lambda item: (item.until, item.limit))
        return [item.as_dict() for item in latches]

    def key_refused(self) -> None:
        """The Claude API key was refused: clear everything and hold off."""
        now = self._clock()
        with self._lock:
            cleared = list(self._latches.values())
            self._latches.clear()
            self._suppressed_until = max(
                [now + CONSERVATIVE_LATCH_S] + [item.until for item in cleared],
            )
        for latch in cleared:
            self._log_clear(latch, "the Claude API key was refused")

    def key_accepted(self) -> None:
        """A 2xx on the API-key route: latching may arm again."""
        with self._lock:
            self._suppressed_until = 0.0

    def _expire(self, windows: Sequence[Window]) -> None:
        now = self._clock()
        cleared: list[tuple[Latch, str]] = []
        with self._lock:
            for key, latch in list(self._latches.items()):
                if now >= latch.until:
                    reason = "its reset time passed"
                elif _recovered(latch, windows, now):
                    reason = "the usage windows show it under 100 %"
                else:
                    continue
                del self._latches[key]
                cleared.append((latch, reason))
        for latch, reason in cleared:
            self._log_clear(latch, reason)

    @staticmethod
    def _log_clear(latch: Latch, reason: str) -> None:
        logger.info(
            "model-gateway: subscription %s latch cleared (%s); %s go back to "
            "the subscription",
            latch.limit, reason, latch.scope_text(),
        )


__all__ = [
    "CONSERVATIVE_LATCH_S",
    "LIMIT_5H",
    "LIMIT_UNKNOWN",
    "LIMIT_WEEKLY",
    "MAX_LATCH_S",
    "SCOPE_ALL",
    "Latch",
    "QuotaLatch",
    "QuotaRefusal",
    "latches_for_refusal",
    "model_scope",
    "model_words",
]
