# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The subscription-out latch (v0.2.104, owner 2026-10-18).

With the opt-in subscription -> API-key fallback ON, every first-party
request used to try the subscription first, collect the quota refusal and
only then go to the key — one wasted round trip per turn for the rest of a
5-hour or weekly window. The owner's rule: once the gateway KNOWS the
subscription is out, learn WHICH limit is out and UNTIL WHEN, and do not ask
the subscription again before then.

**When it arms.** Only after the API key has SERVED the request the
subscription refused (a 2xx — ``server._api_key_escalation``): a latch never
points at a key that has not just worked. And only on POSITIVE evidence that
the subscription is out (review R23 MEDIUM-1). The OD-9 escalation of that
one request still happens on any qualifying refusal, but a plain rate-limit
429 — ``classify_quota``'s "we do not know" ``rate_limited`` verdict, with no
rejected window — does NOT latch. The evidence, strongest first:

1. **The refusal's unified rate-limit headers** (the ones Claude Code itself
   reads), honoured only when they say REJECTED:
   ``anthropic-ratelimit-unified-status: rejected`` plus
   ``anthropic-ratelimit-unified-representative-claim`` naming the binding
   limit (``five_hour`` -> ``5h``, every model; ``seven_day`` -> ``weekly``,
   every model; ``seven_day_<model>`` -> a per-model weekly limit that
   latches only the models whose id carries that word, e.g. ``seven_day_opus``
   -> ``claude-opus-*``), or a per-window
   ``anthropic-ratelimit-unified-{5h,7d}-status: rejected`` (or a
   ``-utilization`` of 1.0 or more). Until when: the first of
   ``anthropic-ratelimit-unified-reset`` and the per-window
   ``anthropic-ratelimit-unified-{5h,7d}-reset`` (epoch seconds) that lies in
   the future.
2. **The usage-windows snapshot** (:mod:`model_router.usage_windows`): a
   fresh ``5h`` / ``weekly`` reading at 100 % latches every model; a fresh
   per-model ``weekly:<Name>`` reading at 100 % latches the models it covers,
   and only when the refused model is one of them. Its ``resets_at`` is the
   time.
3. **The ``exhausted`` verdict** of :func:`model_router.quota.classify_quota`
   (a vendor exhaustion code, a reset more than ten minutes out, or
   exhaustion words with no reset) with none of the above: the limit is
   ``unknown``, so only the refused model's own family is latched, until the
   reset the refusal quotes or — none quoted — for
   :data:`CONSERVATIVE_LATCH_S`.

Every latch is capped by its limit (:func:`cap_for`): 5 h + 15 min for the
5-hour limit, 7 days + 1 hour for a weekly one — never "forever".

**When it clears.** At its reset time; when a usage-windows reading that
STARTED after the latch shows the latched window back under 100 %; or when
the key fails on a latched request (any refusal that is not the client's own
fault — 401/403, a 402/429 balance or rate refusal, a 5xx): that request goes
to the subscription so the user gets its real answer, every latch clears and
none re-arms until the key next answers 2xx. A per-model key 403 clears only
the latches covering that model. Each latch and each clear is one INFO line.

In memory only, for the process's lifetime: a restart forgets it and the next
request re-learns it with one refusal. Every method is synchronous, takes no
I/O and holds one lock for a few dict operations, so it is safe on the event
loop and from any thread; the gateway still calls each one through its
``_guarded`` helper, so a latch defect degrades to "no latch", never to a
failed request.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Optional, Sequence

from .model_family import parse_model_id
from .quota import CLASS_EXHAUSTED, _find_reset_value, _reset_distance_s
from .usage_windows import FETCH_TIMEOUT_S, Window, effective_percent

logger = logging.getLogger(__name__)

#: How long a latch holds when the evidence names no reset time — an
#: ``exhausted`` verdict quoting none, or a rejected window without its
#: reset: one usage-windows refresh interval including its jitter
#: (``usage_windows.DEFAULT_REFRESH_S`` 240 s + ``DEFAULT_JITTER_S`` 60 s), so
#: the next reading of the windows normally decides first. Short on purpose:
#: every latched request is billed to the Console key.
CONSERVATIVE_LATCH_S = 300.0

#: The per-limit caps (review R23 LOW-1): a 5-hour latch never outlives 5 h
#: plus 15 min of clock slack, a weekly one 7 days plus an hour, whatever a
#: header says (a millisecond epoch, a skewed clock).
MAX_5H_LATCH_S = 5 * 3600 + 900
MAX_LATCH_S = 7 * 24 * 3600 + 3600

#: A usage-window reading is counted as taken AFTER a latch only when it was
#: stamped later than this past the latch: the endpoint parser stamps the
#: reading when the fetch ENDS, and a fetch is bounded by
#: ``usage_windows.FETCH_TIMEOUT_S`` — so a reading stamped later than that
#: started after the latch (review R23 NIT-3).
RECOVERY_MARGIN_S = FETCH_TIMEOUT_S

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
    #: ``classify_quota``'s verdict, already computed by the ladder branch —
    #: passed in, never re-derived here.
    classification: str = ""


@dataclass(frozen=True)
class Latch:
    """One exhausted limit: which, for which models, until when (epoch s)."""

    limit: str
    scope: str
    until: float
    since: float
    source: str

    def covers(self, model: str) -> bool:
        return (
            self.scope == SCOPE_ALL
            or self.scope in model_words(model)
            or self.scope == parse_model_id(model).bare_id.lower()
        )

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


#: Family words that name no single model family: never a latch scope.
_GENERIC_WORDS = frozenset({"claude", "anthropic"})


def model_scope(model: str) -> str:
    """The narrowest scope a latch on an unidentified limit takes: the
    model's own family word (``opus``), else its bare id — never ``all`` and
    never a word every Claude id carries (review R23 NIT-1)."""
    parts = parse_model_id(model)
    words = [w for w in parts.family.lower().split("-") if w and w not in _GENERIC_WORDS]
    return words[-1] if words else (parts.bare_id.lower() or model.strip().lower())


def cap_for(limit: str) -> float:
    """The longest a latch on ``limit`` may last (:data:`MAX_5H_LATCH_S` /
    :data:`MAX_LATCH_S`)."""
    return MAX_5H_LATCH_S if limit == LIMIT_5H else MAX_LATCH_S


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


def _future(now: float, *values: Optional[float]) -> Optional[float]:
    """The first value that lies after ``now`` (review R23 NIT-2: a past
    overall reset must not shadow a valid per-window one)."""
    return next((v for v in values if v is not None and v > now), None)


def _from_headers(
    headers: Mapping[str, str], windows: Sequence[Window], now: float,
) -> list[tuple[str, str, Optional[float]]]:
    """``(limit, scope, until-or-None)`` the refusal's unified headers name —
    only windows the headers say are REJECTED (review R23 MEDIUM-1)."""
    h = _lowered(headers)
    overall_rejected = h.get(f"{_UNIFIED}status", "").lower() == "rejected"
    overall = _epoch(h.get(_RESET_HEADER))

    def rejected(suffix: str) -> bool:
        if h.get(f"{_UNIFIED}{suffix}-status", "").lower() == "rejected":
            return True
        try:
            return float(h.get(f"{_UNIFIED}{suffix}-utilization", "")) >= 1.0
        except ValueError:
            return False

    claim = h.get(_CLAIM_HEADER, "").lower()
    if claim in _CLAIM_LIMITS:
        limit = _CLAIM_LIMITS[claim]
        suffix = _WINDOW_HEADER_SUFFIX[limit]
        if overall_rejected or rejected(suffix):
            until = _future(now, overall, _epoch(h.get(f"{_UNIFIED}{suffix}-reset")),
                            _window_reset(windows, limit, now))
            return [(limit, SCOPE_ALL, until)]
    elif claim.startswith(_PER_MODEL_CLAIM_PREFIX) and claim[len(_PER_MODEL_CLAIM_PREFIX):]:
        if overall_rejected:
            name = claim[len(_PER_MODEL_CLAIM_PREFIX):]
            limit = f"{LIMIT_WEEKLY_MODEL_PREFIX}{name.capitalize()}"
            return [(limit, name, _future(now, overall, _window_reset(windows, limit, now)))]
    found: list[tuple[str, str, Optional[float]]] = []
    for limit, suffix in _WINDOW_HEADER_SUFFIX.items():
        if rejected(suffix):
            until = _future(now, _epoch(h.get(f"{_UNIFIED}{suffix}-reset")), overall,
                            _window_reset(windows, limit, now))
            found.append((limit, SCOPE_ALL, until))
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
    models, until when — the three sources of the module docstring, strongest
    first. Pure. EMPTY when there is no positive evidence the subscription is
    out (a plain rate-limited refusal): the request was still served by the
    key, but nothing latches. Never a latch without an end.
    """
    named = _from_headers(refusal.headers, windows, now)
    source = SOURCE_REFUSAL_HEADERS
    if not named:
        named = _from_windows(model, windows, now)
        source = SOURCE_USAGE_WINDOWS
    if not named:
        if refusal.classification != CLASS_EXHAUSTED:
            return []
        distance = _reset_distance_s(_find_reset_value(refusal.body, refusal.headers))
        until = _future(
            now,
            now + distance if distance is not None else None,
            _epoch(_lowered(refusal.headers).get(_RESET_HEADER)),
        )
        named = [(LIMIT_UNKNOWN, model_scope(model), until)]
        source = SOURCE_RESET_HINT if until is not None else SOURCE_CONSERVATIVE
    out: list[Latch] = []
    for limit, scope, until in named:
        if until is None or until <= now:
            until = now + CONSERVATIVE_LATCH_S
        out.append(Latch(limit, scope, min(until, now + cap_for(limit)), now, source))
    return out


def _recovered(latch: Latch, windows: Iterable[Window], now: float) -> bool:
    """A reading taken AFTER the latch shows its window back under 100 %.

    ``unknown`` limits need the 5h AND the weekly window known and under
    100 % (and any per-model weekly window covering the scope too)."""
    fresh = {
        w.id.lower(): pct
        for w in windows
        if w.observed_at > latch.since + RECOVERY_MARGIN_S
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
                "model-gateway: subscription %s until %s; sending %s to the "
                "Claude API key (source=%s)",
                "quota refusal names no limit" if latch.limit == LIMIT_UNKNOWN
                else f"{latch.limit} limit exhausted",
                _iso(latch.until), latch.scope_text(), latch.source,
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

    def key_refused(self, model: Optional[str] = None) -> None:
        """The Claude API key failed. Key-level (``model`` ``None``): clear
        every latch and hold off re-arming until the key next answers 2xx.
        Model-level (a per-model 403, review R23 LOW-3): clear only the
        latches covering ``model``, no hold-off."""
        now = self._clock()
        with self._lock:
            cleared = [
                (key, latch) for key, latch in self._latches.items()
                if model is None or latch.covers(model)
            ]
            for key, _latch in cleared:
                del self._latches[key]
            if model is None:
                self._suppressed_until = max(
                    [now + CONSERVATIVE_LATCH_S] + [latch.until for _k, latch in cleared],
                )
        for _key, latch in cleared:
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
    "MAX_5H_LATCH_S",
    "MAX_LATCH_S",
    "SCOPE_ALL",
    "Latch",
    "QuotaLatch",
    "QuotaRefusal",
    "cap_for",
    "latches_for_refusal",
    "model_scope",
    "model_words",
]
