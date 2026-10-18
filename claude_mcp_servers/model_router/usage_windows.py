# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Subscription usage windows per vendor — ONE home, cached, never on a request path.

Why this exists. With the VS Code panel pointed at the gateway, Claude Code
classifies the session as API-key auth and its Account & Usage view prices
every token at API list rates — a dollar figure that means nothing on a
subscription. The real numbers are the subscriptions' own windows, and this
module is the one place that knows how to read them:

========== ============================ ======================================
vendor     windows                      source (all UNDOCUMENTED by the vendor)
========== ============================ ======================================
Anthropic  5h, weekly, weekly per model ``GET <upstream>/api/oauth/usage``
                                        (Bearer = the Claude login this daemon
                                        already reads) + the passive
                                        ``anthropic-ratelimit-unified-*``
                                        headers on every relayed answer
Z.ai       5h, weekly                   ``Vendor.quota_url`` (raw key, no
                                        scheme)
QwenCloud  none — no quota source       the gateway ledger's routed-token total
                                        for the current month, COLLECTED into
                                        the snapshot's ``tokens`` block and
                                        never rendered (owner ruling 2026-09-28)
Kimi Code  5h, monthly, monthly code    ``Vendor.quota_url`` (``x-api-key``
                                        raw, the relayed client user-agent —
                                        the same Cloudflare edge gate as its
                                        messages endpoint, live 2026-10-07);
                                        when its 5h reading is unknown or
                                        stale, a ``~`` ESTIMATE from this
                                        gateway's ledger against a token
                                        budget (``ledger_estimate``)
========== ============================ ======================================

Four rules shape everything below.

**Unknown is not zero.** Every source is undocumented and one of them changed
its schema during 2026. A window that is missing, unparseable, older than
:data:`STALE_AFTER_S` or past its own reset time reads ``percent: null`` with
the reason beside it — never 0 % (reads "plenty left") and never 100 % (reads
"blocked"). A vendor with no programmatic quota has no window at all: the
gateway's routed-token total for the month is COLLECTED into the snapshot's
``tokens`` block (``?format=json`` carries it) and deliberately NOT rendered
in the status line or the picker — it measures what this one gateway routed,
not the account's plan usage (owner ruling 2026-09-28).

**Never on a request path.** Nothing here is awaited by ``/v1/messages``. The
passive header capture is a synchronous dict parse of headers the relay has
already received. The fetches run in ONE background task, scheduled by
exactly two signals and nothing else:

* a READ of the snapshot (``/usage/windows``, the picker's ``/v1/models``)
  that finds the cache due — stale-while-revalidate: the reader is answered
  from memory immediately;
* CHAT TRAFFIC (owner decision 2026-09-23, "warm while chats flow"): every
  ``/v1/messages`` calls :meth:`UsageWindows.note_activity`, a synchronous
  O(1) timestamp plus the same due-check. Picker labels are frozen when a
  session starts, so a session started during active use must find fresh
  numbers already cached rather than trigger the first fetch itself.

There is no timer. A gateway with no chat traffic and no readers makes no
vendor calls at all; with traffic, at most one refresh per jittered interval
(one in flight, ever) — a burst of requests schedules one task, not one per
request. The jitter keeps two readers from synchronising a vendor's rate
limiter.

**No second secret resolver.** The Claude token comes from the gateway's own
:class:`model_router.auth.OAuthReader`, the vendor key from its own
:class:`model_router.secrets.VendorKeyResolver`; both are injected as
callables. A token or key is placed in exactly one outbound header and appears
in nothing this module returns, raises or logs — ``problem`` strings are built
from vendor ids and HTTP statuses only (asserted by the tests).

**Cached state is bounded.** One snapshot per vendor, one incremental ledger
cursor; no per-request growth.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

import aiohttp

from vco_lib.atomic import atomic_write_json
from vco_lib.env_values import env_int

from .config import usage_resets_path
from .vendors import (
    DEFAULT_ANTHROPIC_VERSION,
    OAUTH_BETA,
    Vendor,
    vendor_display_name,
)

logger = logging.getLogger(__name__)

#: Seconds between refreshes of the vendor sources, before jitter. Modest on
#: purpose: the endpoints are undocumented and the numbers move slowly.
DEFAULT_REFRESH_S = 240.0
#: Up to this many seconds are added to each interval at random.
DEFAULT_JITTER_S = 60.0
#: A reading older than this is shown as unknown rather than as a number that
#: silently stopped updating (a source that has been failing for half an hour
#: is not "31 %" any more — it is "we do not know").
STALE_AFTER_S = 30 * 60
#: Per-fetch bound. The refresh task runs off every request path, so this only
#: bounds how long one refresh can take, never how long a user waits.
FETCH_TIMEOUT_S = 10.0
#: Largest quota answer read. Both real answers are under 4 KiB.
MAX_BODY_BYTES = 256 * 1024
#: Distinct client user-agents remembered for a user-agent-gated quota
#: endpoint (the kimi row), most recent first when asked.
MAX_CLIENT_USER_AGENTS = 4
#: How long a client user-agent the quota endpoint REFUSED (HTTP 403 at the
#: vendor's edge) is skipped before it is offered again. A library UA (the
#: launcher's ``Python-urllib`` read of ``/usage/windows``) is refused every
#: time; a real client's UA refused by a transient edge rule must come back.
REFUSED_USER_AGENT_RETRY_S = 60 * 60

#: The Kimi 5h FALLBACK ESTIMATE's token budget (owner 2026-10-18: "Kimi's
#: budget is pretty much 1M tokens over a 5h window", read off the vendor's
#: website against this gateway's own subagent traffic). ``0`` disables the
#: estimate. Read once, when :class:`UsageWindows` is built at daemon start.
KIMI_5H_BUDGET_ENV = "VCT_MODEL_GATEWAY_KIMI_5H_TOKEN_BUDGET"
DEFAULT_KIMI_5H_TOKEN_BUDGET = 1_000_000
#: Kimi's 5h windows sit on a FIXED 5-hour grid: the 2026-10-07 probe reset
#: at 05:13:09.844464Z and the 2026-10-18 one at 06:13:09.844464Z, exactly
#: 53 x 5 h apart — so a past reset rolls forward in 5 h steps.
KIMI_5H_SPAN_S = 5 * 60 * 60
#: How far back the ledger reader keeps individual rows for window estimates:
#: one 5h window plus slack.
RECENT_LEDGER_KEEP_S = 6 * 60 * 60
#: The ledger's four token fields, in the order a recent row stores them.
LEDGER_TOKEN_FIELDS: tuple[str, ...] = (
    "input_tokens", "cache_creation_input_tokens",
    "cache_read_input_tokens", "output_tokens",
)
#: Which ledger fields the Kimi 5h estimate counts. CALIBRATION (2026-10-18):
#: the only evidence is two live readings 14 s apart in a window whose
#: ledger held 2 then 3 kimi rows (the 2026-10-07 window predates the
#: ledger's oldest row). ``limit_5h.used_ratio`` read 0.0135 then 0.015;
#: the ledger held input 44,491 / output 488 / cache-read 38,656, then input
#: 45,257 / output 962 / cache-read 82,944 (cache-creation 0 throughout).
#: No field set fits both with one budget — input+output implies 3.3M then
#: 3.1M per window but 0.83M for the step between; adding cache-read implies
#: 6.2M then 8.6M — and Kimi's detail count ("1 of 100") is too coarse to
#: help. The evidence cannot decide, so the rule is input + cache-creation
#: + output (cache reads excluded), as the owner asked in that case. Against
#: the 1M default it read ~4.5 % where Kimi said 1.35 %: the estimate errs
#: HIGH, the safe side for deciding whether a lane may run.
KIMI_5H_ESTIMATE_FIELDS: tuple[str, ...] = (
    "input_tokens", "cache_creation_input_tokens", "output_tokens",
)

ANTHROPIC_USAGE_PATH = "/api/oauth/usage"

SOURCE_OAUTH_USAGE = "oauth_usage"
SOURCE_UNIFIED_HEADERS = "unified_headers"
SOURCE_ZAI_QUOTA = "zai_quota"
SOURCE_KIMI_USAGES = "kimi_usages"
SOURCE_LEDGER = "gateway_ledger"
#: A window percent ESTIMATED from this gateway's own ledger (the Kimi 5h
#: fallback) — never the vendor's reading; rendered with a ``~``.
SOURCE_LEDGER_ESTIMATE = "ledger_estimate"

#: Vendor states. ``pending`` = no refresh has completed yet; ``unconfigured``
#: = no credential to ask with; ``unknown`` = asked and got nothing usable.
STATE_OK = "ok"
STATE_PENDING = "pending"
STATE_UNCONFIGURED = "unconfigured"
STATE_UNKNOWN = "unknown"

#: Why a percent window reads ``null``.
UNKNOWN_NOT_REPORTED = "not_reported"
UNKNOWN_STALE = "stale"
UNKNOWN_RESET_PASSED = "reset_passed"

ANTHROPIC_ID = "anthropic"
ANTHROPIC_LABEL = "Claude"

#: Z.ai window identity: ``(unit, number)`` -> ``(window id, label)``. unit 3
#: is hours, unit 6 weeks (probed 2026-09-23). Anything else — the older
#: monthly ``TIME_LIMIT`` MCP-call quota, a unit this table has never seen — is
#: not a model window and is skipped rather than guessed at.
_ZAI_WINDOWS: dict[tuple[int, int], tuple[str, str]] = {
    (3, 5): ("5h", "5h"),
    (6, 1): ("weekly", "wk"),
}
#: Both generations of the Z.ai model-quota type.
_ZAI_MODEL_TYPES = frozenset({"CREDIT_LIMIT", "TOKENS_LIMIT"})

Fetch = Callable[[str, Mapping[str, str]], Awaitable["tuple[Optional[int], Any]"]]


# ── value helpers ────────────────────────────────────────────────────────
def _iso(epoch_s: float) -> str:
    return (
        datetime.fromtimestamp(epoch_s, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _percent(value: Any) -> Optional[float]:
    """A finite, non-negative number, else ``None``. Booleans are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return round(number, 1)


def _number(value: Any) -> Optional[float]:
    """Like :func:`_percent` but also accepts a NUMERIC STRING, unrounded.

    Kimi's usages endpoint reports ``used`` / ``limit`` as strings ("9",
    "100" — probe 2026-10-07), and the percent derived from them must not
    be rounded before the division.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _fraction_percent(value: Any) -> Optional[float]:
    """A 0–1 fraction (Kimi's ``used_ratio``) as a percent, else ``None``.

    Two decimals, not :func:`_percent`'s one: a monthly ratio as small as
    the probe's 0.0029 must survive as 0.29 % rather than round to 0.3.
    """
    number = _number(value)
    if number is None:
        return None
    return round(number * 100.0, 2)


def _reset_epoch(value: Any, *, millis: bool = False) -> Optional[float]:
    """An ISO-8601 string or an epoch number (seconds, or ms with ``millis``)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            return None
        return number / 1000.0 if millis else number
    if isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    return None


# ── the data ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Window:
    """One usage window as observed. ``observed_at`` is epoch seconds."""

    id: str
    label: str
    percent: Optional[float]
    resets_at: Optional[float]
    source: str
    observed_at: float
    #: The reason ``percent`` is ``None`` AT OBSERVATION (``not_reported``);
    #: staleness is decided later, at read time, by :func:`_effective`.
    unknown_reason: Optional[str] = None


@dataclass(frozen=True)
class TokenWindow:
    """Tokens a vendor with no quota source used this month, per the ledger."""

    tokens: int
    requests: int
    period_start: float
    counted_since: Optional[float]
    observed_at: float


@dataclass
class VendorUsage:
    id: str
    label: str
    name: str
    state: str = STATE_PENDING
    problem: Optional[str] = None
    plan: Optional[str] = None
    windows: list[Window] = field(default_factory=list)
    tokens: Optional[TokenWindow] = None


def _unknown(window_id: str, label: str, source: str, now: float) -> Window:
    return Window(window_id, label, None, None, source, now, UNKNOWN_NOT_REPORTED)


# ── parsers (pure) ───────────────────────────────────────────────────────
def parse_anthropic_usage(payload: Any, *, now: float) -> list[Window]:
    """Windows from ``/api/oauth/usage``. Always yields ``5h`` and ``weekly``.

    ``limits[]`` is the newer shape and the only one carrying the per-model
    weekly scope (``weekly_scoped`` + ``scope.model.display_name``, e.g.
    "Fable"); ``five_hour`` / ``seven_day`` are read when ``limits[]`` does not
    name a window. Both report PERCENT (0–100) — the probe on 2026-09-23 read
    ``utilization: 31.0`` beside ``percent: 31``. A missing window is present
    and unknown, never absent: a reader must be able to tell "no weekly limit
    reported" from "weekly limit at 0 %".
    """
    if not isinstance(payload, Mapping):
        return [_unknown("5h", "5h", SOURCE_OAUTH_USAGE, now),
                _unknown("weekly", "wk", SOURCE_OAUTH_USAGE, now)]
    found: dict[str, Window] = {}
    scoped: list[Window] = []
    limits = payload.get("limits")
    if isinstance(limits, list):
        for item in limits:
            if not isinstance(item, Mapping):
                continue
            kind = item.get("kind")
            pct = _percent(item.get("percent"))
            reset = _reset_epoch(item.get("resets_at"))
            if kind == "session":
                found.setdefault("5h", Window("5h", "5h", pct, reset, SOURCE_OAUTH_USAGE, now))
            elif kind == "weekly_all":
                found.setdefault(
                    "weekly", Window("weekly", "wk", pct, reset, SOURCE_OAUTH_USAGE, now),
                )
            elif kind == "weekly_scoped":
                scope = item.get("scope")
                model = scope.get("model") if isinstance(scope, Mapping) else None
                name = model.get("display_name") if isinstance(model, Mapping) else None
                if isinstance(name, str) and name.strip():
                    name = name.strip()
                    scoped.append(
                        Window(f"weekly:{name}", name, pct, reset, SOURCE_OAUTH_USAGE, now),
                    )
    for key, window_id, label in (("five_hour", "5h", "5h"), ("seven_day", "weekly", "wk")):
        if window_id in found and found[window_id].percent is not None:
            continue
        block = payload.get(key)
        if isinstance(block, Mapping):
            pct = _percent(block.get("utilization"))
            if pct is not None:
                found[window_id] = Window(
                    window_id, label, pct, _reset_epoch(block.get("resets_at")),
                    SOURCE_OAUTH_USAGE, now,
                )
    out: list[Window] = []
    for window_id, label in (("5h", "5h"), ("weekly", "wk")):
        window = found.get(window_id) or _unknown(window_id, label, SOURCE_OAUTH_USAGE, now)
        if window.percent is None:
            window = replace(window, unknown_reason=UNKNOWN_NOT_REPORTED)
        out.append(window)
    return out + scoped


#: ``anthropic-ratelimit-unified-<suffix>-*`` -> (window id, label).
_HEADER_WINDOWS: tuple[tuple[str, str, str], ...] = (
    ("5h", "5h", "5h"),
    ("7d", "weekly", "wk"),
)


def parse_unified_headers(headers: Mapping[str, str], *, now: float) -> list[Window]:
    """Windows from the passive ``anthropic-ratelimit-unified-*`` headers.

    The header utilization is a FRACTION (0–1) — the client itself renders it
    as ``Math.round(utilization * 100)`` — and the reset is epoch seconds. Only
    windows whose utilization parses are returned: a response without the
    headers (a vendor answer, an error page) must not blank a good reading.
    """
    out: list[Window] = []
    for suffix, window_id, label in _HEADER_WINDOWS:
        raw = headers.get(f"anthropic-ratelimit-unified-{suffix}-utilization")
        if raw is None:
            continue
        try:
            fraction = float(str(raw).strip())
        except ValueError:
            continue
        if not math.isfinite(fraction) or fraction < 0:
            continue
        reset: Optional[float] = None
        raw_reset = headers.get(f"anthropic-ratelimit-unified-{suffix}-reset")
        if raw_reset is not None:
            try:
                reset = _reset_epoch(float(str(raw_reset).strip()))
            except ValueError:
                reset = None
        out.append(
            Window(window_id, label, round(fraction * 100.0, 1), reset,
                   SOURCE_UNIFIED_HEADERS, now),
        )
    return out


def parse_zai_quota(payload: Any, *, now: float) -> "tuple[Optional[str], list[Window]]":
    """``(plan level, windows)`` from Z.ai's monitor endpoint. Always yields
    ``5h`` and ``weekly``.

    Two generations are read: the 2026-09 shape (``CREDIT_LIMIT`` rows for
    both windows) and the older one (``TOKENS_LIMIT`` for 5h, no weekly row —
    which reads as weekly UNKNOWN, not as a weekly at 0 %). A row whose
    ``percentage`` is missing but whose ``currentValue`` / ``usage`` are both
    present is derived from them — the same answer, from the same source.
    ``success: false`` or a non-200 ``code`` yields no numbers at all.
    """
    level: Optional[str] = None
    found: dict[str, Window] = {}
    data: Any = None
    if isinstance(payload, Mapping):
        ok = payload.get("success") is not False and payload.get("code", 200) in (200, "200", 0, "0")
        data = payload.get("data") if ok else None
        if data is None and ok and isinstance(payload.get("limits"), list):
            data = payload
    if isinstance(data, Mapping):
        raw_level = data.get("level")
        if isinstance(raw_level, str) and raw_level.strip():
            level = raw_level.strip()
        limits = data.get("limits")
        for item in limits if isinstance(limits, list) else ():
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("type") or "").upper()
            if kind not in _ZAI_MODEL_TYPES:
                continue
            unit, number = item.get("unit"), item.get("number")
            if isinstance(unit, bool) or isinstance(number, bool):
                continue
            if not isinstance(unit, (int, str)) or not isinstance(number, (int, str)):
                continue
            try:
                ident = (int(unit), int(number))
            except ValueError:
                continue
            mapped = _ZAI_WINDOWS.get(ident)
            if mapped is None or mapped[0] in found:
                continue
            pct = _percent(item.get("percentage"))
            if pct is None:
                used = _percent(item.get("currentValue"))
                cap = _percent(item.get("usage"))
                if used is not None and cap:
                    pct = round(used * 100.0 / cap, 1)
            found[mapped[0]] = Window(
                mapped[0], mapped[1], pct,
                _reset_epoch(item.get("nextResetTime"), millis=True),
                SOURCE_ZAI_QUOTA, now,
                None if pct is not None else UNKNOWN_NOT_REPORTED,
            )
    windows = [
        found.get(window_id) or _unknown(window_id, label, SOURCE_ZAI_QUOTA, now)
        for window_id, label in _ZAI_WINDOWS.values()
    ]
    return level, windows


#: Kimi window identity, in snapshot/render order. The 5h id/label match the
#: Z.ai convention (``("5h", "5h")``) so every consumer that already renders
#: that window — the status line, the picker suffix, the launcher card —
#: renders Kimi's the same way.
_KIMI_WINDOWS: tuple[tuple[str, str], ...] = (
    ("5h", "5h"),
    ("monthly", "mo"),
    ("monthly_code", "mo-code"),
)
#: The 5h window's ``window`` block in Kimi's answer: duration 300 with
#: ``TIME_UNIT_MINUTE`` (5 x 60 minutes — probe 2026-10-07).
_KIMI_5H_DURATION_MINUTES = 300
_KIMI_TIME_UNIT_MINUTE = "TIME_UNIT_MINUTE"


def _kimi_5h_from_detail(detail: Mapping[str, Any]) -> Optional[float]:
    """The 5h percent from one ``limits[].detail`` block, else ``None``.

    The block is proto3 JSON (``TIME_UNIT_MINUTE`` enum names, int64 as
    strings), and proto3 JSON OMITS a field at its default value: an unused
    window carries ``limit`` and ``remaining`` but no ``used`` at all. So
    ``used`` is read when present, else derived as ``limit - remaining``
    when BOTH of those are present (an unused window: remaining == limit,
    0 %). With neither, nothing is known and nothing is invented.
    """
    cap = _number(detail.get("limit"))
    if cap is None or cap <= 0:
        return None
    used = _number(detail.get("used"))
    if used is None:
        remaining = _number(detail.get("remaining"))
        if remaining is None or remaining > cap:
            return None
        used = cap - remaining
    return round(used * 100.0 / cap, 1)


def parse_kimi_usages(payload: Any, *, now: float) -> "tuple[Optional[str], list[Window]]":
    """``(None, windows)`` from Kimi Code's usages endpoint. Always yields
    ``5h``, ``monthly`` and ``monthly_code``.

    The live shape (probes 2026-10-07 and 2026-10-18 with the owner's key,
    saved as ``tests/fixtures/model_router/kimi-usages.json`` and
    ``kimi-usages-2026-10-18.json``): ``limits[]`` carries the 5h window —
    ``window.duration`` 300, ``timeUnit TIME_UNIT_MINUTE`` — whose
    ``detail`` reports ``limit`` / ``used`` / ``remaining`` as STRINGS and a
    ``resetTime``; ``usages.limit_5h`` / ``limit_month_total`` /
    ``limit_month_code`` carry a ``used_ratio`` FRACTION (x100 here) and a
    ``reset_time``.

    The 5h percent, in order of preference:

    1. ``limits[].detail`` — ``used / limit``, or ``(limit - remaining) /
       limit`` when ``used`` is absent (proto3 JSON omits a zero ``used``:
       an unused window must read 0 %, see :func:`_kimi_5h_from_detail`).
       Preferred because the two readings DISAGREE: 2026-10-07
       ``limit_5h.used_ratio`` read 0 while the detail read 9 of 100 used,
       2026-10-18 the ratio read 0.0135 against 1 of 100. The detail is
       the count the vendor enforces.
    2. ``usages.limit_5h.used_ratio`` — ONLY when ``limits[]`` yields no
       readable 5h window (no matching entry, or a detail with neither
       ``used`` nor ``remaining``), so a missing detail never leaves the
       window unknown while the vendor did report a ratio.

    The monthly windows have only the one reading. Like every quota source
    this is undocumented, so each field is read defensively: a missing or
    unparseable window is present and unknown, never absent, and never a
    number nobody reported. An unknown 5h window still carries the reset
    time the answer named, when it named one: the ledger estimate
    (:meth:`UsageWindows._refresh_ledger`) needs the window's edges even
    when its count is missing.
    """
    found: dict[str, Window] = {}
    reset_hint: Optional[float] = None
    if isinstance(payload, Mapping):
        limits = payload.get("limits")
        if isinstance(limits, list):
            for item in limits:
                if not isinstance(item, Mapping):
                    continue
                window = item.get("window")
                if not isinstance(window, Mapping):
                    continue
                if window.get("timeUnit") != _KIMI_TIME_UNIT_MINUTE:
                    continue
                if _number(window.get("duration")) != _KIMI_5H_DURATION_MINUTES:
                    continue
                detail = item.get("detail")
                if not isinstance(detail, Mapping):
                    continue
                pct = _kimi_5h_from_detail(detail)
                if pct is None:
                    reset_hint = reset_hint or _reset_epoch(detail.get("resetTime"))
                    continue
                found.setdefault("5h", Window(
                    "5h", "5h", pct,
                    _reset_epoch(detail.get("resetTime")),
                    SOURCE_KIMI_USAGES, now, None,
                ))
        usages = payload.get("usages")
        if isinstance(usages, Mapping):
            for key, window_id, label in (
                # limit_5h is the FALLBACK for the 5h window: setdefault
                # keeps a reading limits[] already produced (see above).
                ("limit_5h", "5h", "5h"),
                ("limit_month_total", "monthly", "mo"),
                ("limit_month_code", "monthly_code", "mo-code"),
            ):
                block = usages.get(key)
                if not isinstance(block, Mapping):
                    continue
                pct = _fraction_percent(block.get("used_ratio"))
                if pct is None:
                    if window_id == "5h":
                        reset_hint = reset_hint or _reset_epoch(block.get("reset_time"))
                    continue
                found.setdefault(window_id, Window(
                    window_id, label, pct,
                    _reset_epoch(block.get("reset_time")),
                    SOURCE_KIMI_USAGES, now, None,
                ))
    if "5h" not in found and reset_hint is not None:
        found["5h"] = Window(
            "5h", "5h", None, reset_hint, SOURCE_KIMI_USAGES, now,
            UNKNOWN_NOT_REPORTED,
        )
    windows = [
        found.get(window_id) or _unknown(window_id, label, SOURCE_KIMI_USAGES, now)
        for window_id, label in _KIMI_WINDOWS
    ]
    return None, windows


# ── the per-vendor quota dialect (how each endpoint is asked and read) ────
def _zai_quota_headers(key: str, user_agent: Optional[str]) -> Optional[dict[str, str]]:
    """Z.ai's monitor endpoint: the RAW key in ``Authorization``, no scheme.

    The documented community usage and the 2026-09-23 probe agree, and the
    endpoint is not the vendor's Anthropic-shaped API, so ``auth_scheme``
    does not apply. The relayed client user-agent is not sent: this
    endpoint has no user-agent gate.
    """
    return {"Authorization": key, "Accept-Language": "en-US,en"}


def _kimi_quota_headers(key: str, user_agent: Optional[str]) -> Optional[dict[str, str]]:
    """Kimi's usages endpoint: ``x-api-key`` raw, plus the relayed client UA.

    The vendor row's own auth shape. The endpoint sits behind the same
    Cloudflare gate as the vendor's messages endpoint (403 "error code:
    1010" to a library user-agent, 200 to a client one — live probe
    2026-10-07, and the intermittent 403s the background refresh drew
    07:48-08:25 that day), and the vendor's terms forbid passing the
    gateway off as a different client, so a user-agent is RELAYED, never
    invented. ``None`` — the skip signal — when no client user-agent has
    been relayed yet (v0.2.102, X2): a UA-less fetch is a fetch the edge
    refuses, so it is not sent at all and the vendor reads unknown with the
    reason beside it, instead of 403-ing on every refresh until the first
    chat or read relays one.

    Not every relayed user-agent is one the edge accepts: ``/usage/windows``
    is also read by the launcher's usage card through ``Python-urllib``,
    which the edge refuses (403 "error code: 1010", live 2026-10-18, while
    ``claude-cli/...`` and ``curl/...`` answered 200). WHICH relayed UA is
    sent is :meth:`UsageWindows._refresh_quota_vendor`'s choice — the one
    the edge last accepted, else the most recent one it has not refused.
    """
    if not user_agent:
        return None
    return {
        "x-api-key": key,
        "anthropic-version": DEFAULT_ANTHROPIC_VERSION,
        "User-Agent": user_agent,
    }


#: One dialect per quota vendor, keyed by vendor id: ``(headers builder,
#: payload parser)``. The headers builder answers ``None`` when there is no
#: HONEST way to ask right now (a user-agent-gated endpoint before any
#: client user-agent has been relayed — the fetch is then SKIPPED, see
#: :meth:`UsageWindows._refresh_quota_vendor`). A vendor with a
#: ``quota_url`` and no entry speaks the
#: Z.ai dialect — the one shape this module had before there was a second,
#: and the shape a synthetic test row still exercises. Vendor names are
#: allowed HERE (not in routing/catalog/secrets): this module is where the
#: quota shapes live, beside ``parse_zai_quota``.
_QUOTA_DIALECTS: Mapping[
    str,
    tuple[
        Callable[[str, Optional[str]], Optional[dict[str, str]]],
        Callable[..., "tuple[Optional[str], list[Window]]"],
    ],
] = {
    "kimi": (_kimi_quota_headers, parse_kimi_usages),
}
_ZAI_QUOTA_DIALECT = (_zai_quota_headers, parse_zai_quota)


# ── the ledger's month (for vendors with no quota source) ────────────────
def month_start(now: float) -> float:
    """Local midnight on the first of the current month, as epoch seconds."""
    local = datetime.fromtimestamp(now).astimezone()
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


class LedgerMonthCounter:
    """Per-route token totals for the current month, read INCREMENTALLY.

    The ledger (:mod:`model_router.usage`) is append-only JSONL rotated to its
    tail past a size cap. Each scan reads only the bytes added since the last
    one; a rotation (new file identity, or a file shorter than the cursor) or
    a new month restarts from the top. Only complete lines are consumed, so a
    row being appended while this reads is counted next time, never torn.

    ``counted_since`` is the earliest row the file still holds when that is
    later than the month start — the ledger rotated, or the gateway is newer
    than the month — so the total is labelled as covering only that span
    rather than presented as the whole month.

    The same single pass also keeps the rows of the last
    :data:`RECENT_LEDGER_KEEP_S` individually (route, timestamp, the four
    token fields) for :meth:`tokens_between` — the Kimi 5h estimate's
    reading. One reader, one parse: a window that straddles the month start
    still sees its rows, because a month change rescans the file from the
    top and recent rows are kept whatever their month.

    ``scan`` runs on an executor thread, never on the event loop; the
    refresh is single-flight, so :meth:`tokens_between` (called after the
    scan returned) never races it.
    """

    def __init__(self) -> None:
        self._identity: Optional[tuple[int, int]] = None
        self._offset = 0
        self._period: Optional[float] = None
        self._first_ts: Optional[float] = None
        self._totals: dict[str, list[int]] = {}
        #: ``(ts, route, (input, cache_creation, cache_read, output))`` for
        #: rows newer than ``now - RECENT_LEDGER_KEEP_S`` at the last scan.
        self._recent: list[tuple[float, str, tuple[int, ...]]] = []

    def _reset(self, identity: Optional[tuple[int, int]], period: float) -> None:
        self._identity = identity
        self._offset = 0
        self._period = period
        self._first_ts = None
        self._totals = {}
        self._recent = []

    def scan(self, path: Optional[Path], now: float) -> "tuple[float, Optional[float], dict[str, tuple[int, int]]]":
        """``(period_start, counted_since, {route: (tokens, requests)})``."""
        period = month_start(now)
        if path is None:
            self._reset(None, period)
            return period, None, {}
        try:
            st = os.stat(path)
        except OSError:
            self._reset(None, period)
            return period, None, {}
        identity = (st.st_dev, st.st_ino)
        if identity != self._identity or st.st_size < self._offset or period != self._period:
            self._reset(identity, period)
        if st.st_size > self._offset:
            with open(path, "rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read(st.st_size - self._offset)
            end = chunk.rfind(b"\n")
            if end >= 0:
                self._consume(chunk[: end + 1], period, now - RECENT_LEDGER_KEEP_S)
                self._offset += end + 1
        keep_from = now - RECENT_LEDGER_KEEP_S
        self._recent = [row for row in self._recent if row[0] >= keep_from]
        counted_since = (
            self._first_ts if self._first_ts is not None and self._first_ts > period else None
        )
        return period, counted_since, {k: (v[0], v[1]) for k, v in self._totals.items()}

    def tokens_between(
        self, route: str, start: float, end: float, fields: "tuple[str, ...]",
    ) -> "tuple[int, int]":
        """``(tokens, requests)`` the ledger holds for ``route`` with
        ``start <= ts < end``, counting only ``fields`` (names from
        :data:`LEDGER_TOKEN_FIELDS`). Covers only what the last scan kept:
        rows newer than :data:`RECENT_LEDGER_KEEP_S` before it."""
        picks = [LEDGER_TOKEN_FIELDS.index(name) for name in fields]
        tokens = requests = 0
        for ts, row_route, counts in self._recent:
            if row_route != route or not start <= ts < end:
                continue
            tokens += sum(counts[i] for i in picks)
            requests += 1
        return tokens, requests

    def _consume(self, blob: bytes, period: float, keep_from: float) -> None:
        for raw in blob.splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(row, Mapping):
                continue
            ts = _reset_epoch(row.get("ts"))
            if ts is None:
                continue
            if self._first_ts is None or ts < self._first_ts:
                self._first_ts = ts
            route = row.get("route")
            if not isinstance(route, str):
                continue
            counts = tuple(
                value if isinstance(value, int) and not isinstance(value, bool)
                and value > 0 else 0
                for value in (row.get(name) for name in LEDGER_TOKEN_FIELDS)
            )
            if ts >= keep_from:
                self._recent.append((ts, route, counts))
            if ts < period:
                continue
            tokens = sum(counts)
            bucket = self._totals.setdefault(route, [0, 0])
            bucket[0] += tokens
            bucket[1] += 1


# ── HTTP ─────────────────────────────────────────────────────────────────
def session_fetcher(
    session: Callable[[], aiohttp.ClientSession],
    *,
    timeout_s: float = FETCH_TIMEOUT_S,
) -> Fetch:
    """A :data:`Fetch` over the gateway's shared client session.

    Returns ``(status, parsed JSON)``; ``(status, None)`` for a non-200 or an
    unreadable body; ``(None, None)`` when nothing answered. Never raises.
    """

    async def fetch(url: str, headers: Mapping[str, str]) -> "tuple[Optional[int], Any]":
        try:
            async with session().get(
                url,
                headers=dict(headers),
                timeout=aiohttp.ClientTimeout(total=timeout_s),
                allow_redirects=False,
            ) as resp:
                if resp.status != 200:
                    return resp.status, None
                body = await resp.content.read(MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    return resp.status, None
                try:
                    return resp.status, json.loads(body)
                except ValueError:
                    return resp.status, None
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, OSError):
            return None, None

    return fetch


# ── the service ──────────────────────────────────────────────────────────
class UsageWindows:
    """The cached per-vendor snapshot, its refresh task and the header tap."""

    def __init__(
        self,
        *,
        anthropic_upstream: str,
        vendors: Mapping[str, Vendor],
        oauth_token: Callable[[], Optional[str]],
        vendor_key: Callable[[Vendor], Awaitable[Optional[str]]],
        fetch: Fetch,
        ledger_path: Callable[[], Optional[Path]],
        refresh_s: float = DEFAULT_REFRESH_S,
        jitter_s: float = DEFAULT_JITTER_S,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
        kimi_5h_budget: Optional[int] = None,
        resets_path: Callable[[], Optional[Path]] = usage_resets_path,
    ) -> None:
        self._anthropic_upstream = anthropic_upstream.rstrip("/")
        self._vendors = vendors
        self._oauth_token = oauth_token
        self._vendor_key = vendor_key
        self._fetch = fetch
        self._ledger_path = ledger_path
        self._refresh_s = refresh_s
        self._jitter_s = jitter_s
        self._clock = clock
        self._rng = rng
        self._ledger = LedgerMonthCounter()
        self._task: Optional[asyncio.Task] = None
        self._next_due = 0.0
        self._last_refresh: Optional[float] = None
        self._last_activity: Optional[float] = None
        #: Distinct client user-agents relayed by refresh signals, most
        #: recent LAST, at most :data:`MAX_CLIENT_USER_AGENTS`.
        self._client_user_agents: list[str] = []
        #: The user-agent the quota edge last ACCEPTED (HTTP 200): asked
        #: first, and never evicted by newer unproven ones.
        self._accepted_user_agent: Optional[str] = None
        #: user-agent -> when a user-agent-gated quota endpoint refused it.
        self._refused_user_agents: dict[str, float] = {}
        self._header_windows: dict[str, Window] = {}
        #: The ledger FALLBACK ESTIMATE per vendor id: ``{id: budget}`` for
        #: every vendor that has one (only kimi). ``kimi_5h_budget`` is the
        #: injected value; ``None`` reads :data:`KIMI_5H_BUDGET_ENV` (a
        #: non-negative int; garbage keeps the default). ``0`` disables it.
        budget = (
            env_int(KIMI_5H_BUDGET_ENV, DEFAULT_KIMI_5H_TOKEN_BUDGET, minimum=0)
            if kimi_5h_budget is None
            else max(0, int(kimi_5h_budget))
        )
        self._estimate_budgets: dict[str, int] = (
            {"kimi": budget} if budget > 0 and "kimi" in vendors else {}
        )
        #: The last 5h reset time a good vendor answer named, per vendor id.
        #: PERSISTED (``resets_path``, owner-only): the grid is fixed, so one
        #: reset seen before a restart still rolls forward to today's window
        #: — and the estimate exists for the case where no fresh answer
        #: arrives (the vendor refusing the gateway).
        self._resets_path = resets_path
        self._last_5h_reset: dict[str, float] = self._load_resets()
        #: The last computed estimate per vendor id.
        self._estimates: dict[str, _Estimate] = {}
        self._usage: dict[str, VendorUsage] = {
            ANTHROPIC_ID: VendorUsage(ANTHROPIC_ID, ANTHROPIC_LABEL, "Claude subscription"),
        }
        #: The subscription-out latch's report (``QuotaLatch.report`` dicts),
        #: set by the gateway; the snapshot carries it on the Claude entry as
        #: ``api_key_latch`` and :func:`render_line` names it.
        self.latch_report: Callable[[], list[dict]] = lambda: []
        for vendor in vendors.values():
            self._usage[vendor.vendor_id] = VendorUsage(
                vendor.vendor_id,
                vendor.short_name.strip() or vendor_display_name(vendor),
                vendor_display_name(vendor),
            )

    # ── passive capture (request path: sync, cheap, never raises) ───────
    def anthropic_readings(self) -> list[Window]:
        """The Claude windows as last observed (endpoint and relayed headers
        merged, newest known wins) — a pure read of memory, for the quota
        latch (:mod:`model_router.quota_latch`)."""
        return _merge_headers(self._usage[ANTHROPIC_ID].windows, self._header_windows)

    def observe_anthropic_headers(self, headers: Mapping[str, str]) -> None:
        """Take the unified rate-limit headers off a relayed first-party answer."""
        for window in parse_unified_headers(headers, now=self._clock()):
            self._header_windows[window.id] = window

    # ── refresh scheduling ───────────────────────────────────────────────
    @property
    def refreshing(self) -> bool:
        return self._task is not None and not self._task.done()

    def request_refresh(self, user_agent: Optional[str] = None) -> bool:
        """A READ wants the numbers: schedule a background refresh if one is
        due. ``user_agent`` is the reading client's own, when the read is an
        HTTP request — relayed onto a quota fetch whose vendor gates on it
        (see :func:`_kimi_quota_headers`). Returns whether it scheduled a
        refresh; never awaits it.
        """
        self._remember_client_user_agent(user_agent)
        return self._schedule_if_due()

    def note_activity(self, user_agent: Optional[str] = None) -> bool:
        """Chat traffic just passed through: keep the cache warm.

        Called synchronously from ``/v1/messages`` on every request, so it is
        O(1) and never awaits: a timestamp, the due-check, and at most once
        per interval a ``create_task``. ``user_agent`` is that client's own,
        relayed as for :meth:`request_refresh`. Returns whether it scheduled
        one.
        """
        self._last_activity = self._clock()
        self._remember_client_user_agent(user_agent)
        return self._schedule_if_due()

    def _remember_client_user_agent(self, user_agent: Optional[str]) -> None:
        """Remember the client user-agent a refresh signal carried.

        A quota endpoint behind a client-identity gate (the kimi row) is
        asked as a client whose read or traffic reached this gateway — the
        same relay rule as the catalog fetch, and the honest one: the
        gateway never invents a user-agent. The few most recent DISTINCT
        ones are kept (not just the last): the readers of this snapshot are
        not all clients the vendor's edge accepts, and a refused one must
        not displace the one it does accept. A signal carrying none keeps
        what earlier ones saw. Never logged, never snapshotted.
        """
        if not user_agent:
            return
        agents = self._client_user_agents
        if user_agent in agents:
            agents.remove(user_agent)
        agents.append(user_agent)
        # Evict the OLDEST unproven one, never the accepted one: four
        # distinct readers (curl, Python-urllib, a browser, a second
        # launcher) must not push out the client UA the edge accepts.
        while len(agents) > MAX_CLIENT_USER_AGENTS:
            victim = next(
                (a for a in agents if a != self._accepted_user_agent), agents[0],
            )
            agents.remove(victim)

    def _usable_user_agents(self, now: float) -> list[str]:
        """Remembered client user-agents the quota edge has not refused
        within :data:`REFUSED_USER_AGENT_RETRY_S`: the last ACCEPTED one
        first, then the rest most recent first."""
        usable = []
        for agent in reversed(self._client_user_agents):
            refused_at = self._refused_user_agents.get(agent)
            if refused_at is not None and now - refused_at < REFUSED_USER_AGENT_RETRY_S:
                continue
            self._refused_user_agents.pop(agent, None)
            usable.append(agent)
        accepted = self._accepted_user_agent
        if accepted in usable:
            usable.remove(accepted)
            usable.insert(0, accepted)
        return usable

    def _schedule_if_due(self) -> bool:
        """The ONE gate both signals go through. Due means: never refreshed,
        or the jittered interval since the last START has elapsed — and no
        refresh is in flight (single-flight: a hung refresh delays the next
        one, it never stacks a second beside it)."""
        now = self._clock()
        if self.refreshing or now < self._next_due:
            return False
        self._next_due = now + self._refresh_s + self._rng() * self._jitter_s
        self._task = asyncio.get_running_loop().create_task(self._refresh_guarded())
        return True

    async def _refresh_guarded(self) -> None:
        try:
            await self.refresh()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a usage readout may never take the daemon down
            logger.exception("model-gateway: usage-window refresh failed")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutting down
                pass

    # ── the fetches ──────────────────────────────────────────────────────
    async def refresh(self) -> None:
        """Refresh every source once. Each source fails on its own."""
        await self._refresh_anthropic()
        for vendor in self._vendors.values():
            if vendor.quota_url:
                await self._refresh_quota_vendor(vendor)
        await self._refresh_ledger()
        self._last_refresh = self._clock()

    async def _refresh_anthropic(self) -> None:
        usage = self._usage[ANTHROPIC_ID]
        token = self._oauth_token()
        if not token:
            usage.problem = "no usable Claude login (see oauth_state in /health)"
            if not usage.windows:
                usage.state = STATE_UNCONFIGURED
            return
        status, payload = await self._fetch(
            f"{self._anthropic_upstream}{ANTHROPIC_USAGE_PATH}",
            {
                "Authorization": f"Bearer {token}",
                "anthropic-beta": OAUTH_BETA,
                "Content-Type": "application/json",
            },
        )
        self._apply(usage, status, payload, ANTHROPIC_ID, self._parse_anthropic)

    def _parse_anthropic(self, payload: Any) -> "tuple[Optional[str], list[Window]]":
        return None, parse_anthropic_usage(payload, now=self._clock())

    async def _refresh_quota_vendor(self, vendor: Vendor) -> None:
        usage = self._usage[vendor.vendor_id]
        key = await self._vendor_key(vendor)
        if not key:
            usage.problem = (
                f"no key resolved for {vendor.vendor_id} "
                f"(tried: {', '.join(vendor.secret_keys)})"
            )
            if not usage.windows:
                usage.state = STATE_UNCONFIGURED
            return
        assert vendor.quota_url is not None  # noqa: S101 — caller filters on it
        headers_for, parse = _QUOTA_DIALECTS.get(
            vendor.vendor_id, _ZAI_QUOTA_DIALECT,
        )
        plain = headers_for(key, None)
        if plain is not None:
            # No user-agent gate (the Z.ai dialect): one plain fetch.
            status, payload = await self._fetch(vendor.quota_url, plain)
            self._apply(
                usage, status, payload, vendor.vendor_id,
                lambda body: parse(body, now=self._clock()),
            )
            return
        # A user-agent-gated endpoint: asked as a relayed client, never an
        # invented one (vendor ToS). With none relayed yet the fetch is NOT
        # sent (v0.2.102, X2) — it would 403 at the edge on every refresh.
        candidates = self._usable_user_agents(self._clock())
        if not candidates:
            _note_problem(
                usage,
                f"{vendor.vendor_id} usage source not asked: no client "
                "user-agent relayed yet, and the vendor's edge refuses "
                "library user-agents"
                if not self._client_user_agents
                else f"{vendor.vendor_id} usage source not asked: the "
                "vendor's edge refused (HTTP 403) every client user-agent "
                "relayed so far; retried after "
                f"{REFUSED_USER_AGENT_RETRY_S // 60} min or when a new one "
                "is relayed" + _kept_reading_note(usage),
            )
            return
        # Most recent first; a 403 marks that user-agent refused and the
        # next one is tried in the SAME refresh, so a refused reader (the
        # launcher's Python-urllib) costs one extra request per retry
        # interval and never the reading.
        status: Optional[int] = None
        payload: Any = None
        for agent in candidates:
            headers = headers_for(key, agent)
            if headers is None:  # the dialect declined even with a UA
                continue
            status, payload = await self._fetch(vendor.quota_url, headers)
            if status == 200:
                self._accepted_user_agent = agent
            if status != 403:
                break
            self._refused_user_agents[agent] = self._clock()
            if self._accepted_user_agent == agent:
                self._accepted_user_agent = None
        if status == 403:
            _note_problem(
                usage,
                f"{vendor.vendor_id} usage source answered HTTP 403 to every "
                f"relayed client user-agent ({len(candidates)} tried) — "
                "the vendor's edge refused them" + _kept_reading_note(usage),
            )
            return
        self._apply(
            usage, status, payload, vendor.vendor_id,
            lambda body: parse(body, now=self._clock()),
        )

    def _apply(
        self,
        usage: VendorUsage,
        status: Optional[int],
        payload: Any,
        who: str,
        parse: Callable[[Any], "tuple[Optional[str], list[Window]]"],
    ) -> None:
        if status != 200 or payload is None:
            _note_problem(
                usage,
                f"{who} usage source did not answer"
                if status is None
                else f"{who} usage source answered HTTP {status}",
            )
            # A previous good reading stays; _effective() ages it out.
            return
        plan, windows = parse(payload)
        if who in self._estimate_budgets:
            for window in windows:
                if window.id == "5h" and window.resets_at is not None:
                    if self._last_5h_reset.get(who) != window.resets_at:
                        self._last_5h_reset[who] = window.resets_at
                        self._save_resets()
        if not any(w.percent is not None for w in windows):
            _note_problem(
                usage, f"{who} usage source answered in a shape this gateway cannot read",
            )
            return
        usage.windows = windows
        usage.plan = plan
        usage.problem = None
        usage.state = STATE_OK

    async def _refresh_ledger(self) -> None:
        now = self._clock()
        path = self._ledger_path()
        loop = asyncio.get_running_loop()
        try:
            period, since, totals = await loop.run_in_executor(
                None, self._ledger.scan, path, now,
            )
        except (OSError, ValueError) as exc:
            logger.info("model-gateway: usage ledger month scan failed: %s", exc)
            return
        self._refresh_estimates(now, have_ledger=path is not None)
        for vendor in self._vendors.values():
            if vendor.quota_url:
                continue
            usage = self._usage[vendor.vendor_id]
            tokens, requests = totals.get(f"vendor:{vendor.vendor_id}", (0, 0))
            usage.tokens = TokenWindow(tokens, requests, period, since, now)
            usage.state = STATE_OK if path is not None else STATE_UNKNOWN
            usage.problem = None if path is not None else "the usage ledger has no home"

    # ── the persisted 5h reset times ────────────────────────────────────
    def _load_resets(self) -> dict[str, float]:
        """The reset times a previous daemon saved, for vendors that still
        have an estimate. A missing, unreadable or malformed file is an
        empty start — never an error: the next good answer rewrites it."""
        path = self._resets_path()
        if path is None or not self._estimate_budgets:
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        resets = data.get("5h_resets") if isinstance(data, Mapping) else None
        if not isinstance(resets, Mapping):
            return {}
        out: dict[str, float] = {}
        for vendor_id in self._estimate_budgets:
            epoch = _reset_epoch(resets.get(vendor_id))
            if epoch is not None:
                out[vendor_id] = epoch
        return out

    def _save_resets(self) -> None:
        """Persist the reset times, owner-only (mode 0600, like the daemon's
        token, pid and port files). Best effort: a failed write is one log
        line and the estimate simply restarts from the next good answer."""
        path = self._resets_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                path,
                {"5h_resets": dict(self._last_5h_reset)},
                mode=0o600,
            )
        except OSError as exc:
            logger.info("model-gateway: could not save the 5h reset times: %s", exc)

    def _refresh_estimates(self, now: float, *, have_ledger: bool) -> None:
        """Recompute each vendor's 5h ledger estimate from the scan just made.

        The window is the 5 h ending at the last reset a good answer named,
        rolled forward in 5 h steps once that reset has passed; with no reset
        ever seen there is no window and no estimate. ``used`` is what THIS
        gateway routed to the vendor inside it (:data:`KIMI_5H_ESTIMATE_FIELDS`)
        — another machine's, or the vendor's own app's, traffic is invisible
        here, so the estimate can only read low for that reason (and reads
        high for the calibration reason given at that constant). With no
        ledger there is no count at all — no estimate, never a 0 %.
        """
        if not have_ledger:
            self._estimates.clear()
            return
        for vendor_id, budget in self._estimate_budgets.items():
            reset = self._last_5h_reset.get(vendor_id)
            if reset is None:
                continue
            end = roll_window_end(reset, now, KIMI_5H_SPAN_S)
            start = end - KIMI_5H_SPAN_S
            tokens, requests = self._ledger.tokens_between(
                f"vendor:{vendor_id}", start, end, KIMI_5H_ESTIMATE_FIELDS,
            )
            self._estimates[vendor_id] = _Estimate(
                start, end, tokens, requests, budget, now,
            )

    def _estimate_dict(self, vendor_id: str, now: float) -> Optional[dict]:
        """The estimated 5h window as a snapshot window, or ``None`` when
        there is no estimate, its window has ended, or it is stale."""
        estimate = self._estimates.get(vendor_id)
        if estimate is None or now >= estimate.end or now - estimate.computed_at > STALE_AFTER_S:
            return None
        return {
            "id": "5h",
            "label": "5h",
            "kind": "percent",
            "percent": round(estimate.tokens * 100.0 / estimate.budget, 1),
            "resets_at": _iso(estimate.end),
            "source": SOURCE_LEDGER_ESTIMATE,
            "fetched_at": _iso(estimate.computed_at),
            "unknown_reason": None,
            "estimate": {
                "tokens": estimate.tokens,
                "requests": estimate.requests,
                "budget": estimate.budget,
                "window_start": _iso(estimate.start),
                "counted": list(KIMI_5H_ESTIMATE_FIELDS),
                "scope": "this_gateway",
            },
        }

    # ── reading ──────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        """The JSON-ready snapshot, aged at read time. Pure read of memory."""
        now = self._clock()
        vendors = []
        for usage in self._usage.values():
            windows = usage.windows
            if usage.id == ANTHROPIC_ID:
                windows = self.anthropic_readings()
            entry: dict[str, Any] = {
                "id": usage.id,
                "label": usage.label,
                "name": usage.name,
                "state": usage.state,
                "problem": usage.problem,
                "plan": usage.plan,
                "windows": [_window_dict(w, now) for w in windows],
            }
            estimated = self._estimate_dict(usage.id, now)
            if estimated is not None:
                # FALLBACK ONLY: a live 5h reading that is known always wins.
                rows = entry["windows"]
                index = next((i for i, w in enumerate(rows) if w["id"] == "5h"), None)
                if index is None:
                    rows.insert(0, estimated)
                elif rows[index]["percent"] is None:
                    rows[index] = estimated
            if usage.id == ANTHROPIC_ID and usage.state != STATE_OK and any(
                w["percent"] is not None for w in entry["windows"]
            ):
                # The endpoint failed but the relayed headers answered.
                entry["state"] = STATE_OK
            latches = self.latch_report() if usage.id == ANTHROPIC_ID else []
            if latches:
                # v0.2.104: the subscription-out latch — Claude requests
                # (all, or one model's) go straight to the API key until then.
                # The text is rendered HERE (the one renderer, local clock)
                # so the launcher card shows it verbatim, never a mirror.
                entry["api_key_latch"] = latches
                entry["api_key_latch_text"] = latch_text(latches)
            if usage.tokens is not None:
                t = usage.tokens
                entry["tokens"] = {
                    "tokens": t.tokens,
                    "requests": t.requests,
                    "unit": "tokens",
                    "period": "month",
                    "period_start": _iso(t.period_start),
                    "counted_since": _iso(t.counted_since) if t.counted_since else None,
                    "source": SOURCE_LEDGER,
                    "fetched_at": _iso(t.observed_at),
                }
            vendors.append(entry)
        return {
            "generated_at": _iso(now),
            "last_refresh_at": _iso(self._last_refresh) if self._last_refresh else None,
            "last_activity_at": _iso(self._last_activity) if self._last_activity else None,
            "refreshing": self.refreshing,
            "refresh_interval_s": int(self._refresh_s),
            "vendors": vendors,
        }


@dataclass(frozen=True)
class _Estimate:
    """One ledger estimate of a vendor's 5h window (epoch seconds)."""

    start: float
    end: float
    tokens: int
    requests: int
    budget: int
    computed_at: float


def roll_window_end(reset: float, now: float, span_s: float) -> float:
    """The end of the fixed-grid window holding ``now``: ``reset`` while it
    is still ahead, else ``reset`` moved forward in whole ``span_s`` steps to
    the first point strictly after ``now``."""
    if reset > now:
        return reset
    return reset + (math.floor((now - reset) / span_s) + 1) * span_s


def _kept_reading_note(usage: VendorUsage) -> str:
    """Tell the reader a failed refresh did not erase the last good reading
    (each window keeps its own ``fetched_at``; :func:`_effective` ages it)."""
    return "; the last good reading is kept" if usage.windows else ""


def _note_problem(usage: VendorUsage, problem: str) -> None:
    """Record a failed read. Logged on CHANGE only: a source that fails every
    refresh is one line, not one per interval. Never names a credential."""
    if usage.problem != problem:
        logger.info("model-gateway: %s", problem)
    usage.problem = problem
    if not usage.windows:
        usage.state = STATE_UNKNOWN


def _merge_headers(endpoint: list[Window], headers: Mapping[str, Window]) -> list[Window]:
    """Prefer whichever reading of ``5h`` / ``weekly`` is NEWER and known."""
    out = list(endpoint)
    ids = [w.id for w in out]
    for window_id, header in headers.items():
        if window_id in ids:
            index = ids.index(window_id)
            current = out[index]
            if current.percent is None or header.observed_at > current.observed_at:
                out[index] = header
        else:
            out.insert(0 if window_id == "5h" else min(1, len(out)), header)
            ids = [w.id for w in out]
    return out


def _effective(window: Window, now: float) -> "tuple[Optional[float], Optional[str]]":
    if window.percent is None:
        return None, window.unknown_reason or UNKNOWN_NOT_REPORTED
    if now - window.observed_at > STALE_AFTER_S:
        return None, UNKNOWN_STALE
    if window.resets_at is not None and now >= window.resets_at:
        return None, UNKNOWN_RESET_PASSED
    return window.percent, None


def effective_percent(window: Window, now: float) -> Optional[float]:
    """The reading as it stands at ``now`` — ``None`` when unknown, stale or
    past its reset: the same aging the snapshot applies (the quota latch
    reads windows through this, never their raw ``percent``)."""
    return _effective(window, now)[0]


def _window_dict(window: Window, now: float) -> dict:
    percent, reason = _effective(window, now)
    return {
        "id": window.id,
        "label": window.label,
        "kind": "percent",
        "percent": percent,
        "resets_at": _iso(window.resets_at) if window.resets_at else None,
        "source": window.source,
        "fetched_at": _iso(window.observed_at),
        "unknown_reason": reason,
    }


# ── the one-line rendering (the status-line scripts print this verbatim) ─
def _known(window: Mapping[str, Any]) -> bool:
    percent = window.get("percent")
    return isinstance(percent, (int, float)) and not isinstance(percent, bool)


def _window_text(window: Mapping[str, Any]) -> str:
    """``5h 31%`` — the one spelling of a known window, shared by the status
    line and the picker label so the two cannot round differently. A ledger
    ESTIMATE reads ``5h ~31%``: the ``~`` is the one mark that it is this
    gateway's count against a budget, not the vendor's reading."""
    mark = "~" if window.get("source") == SOURCE_LEDGER_ESTIMATE else ""
    return f"{window['label']} {mark}{window['percent']:.0f}%"


def render_line(snapshot: Mapping[str, Any]) -> str:
    """One compact line, e.g. ``Claude 5h 31% · wk 27% · Fable 12% │ GLM 5h 10%``.

    Silent degrade: an unknown window is left out, and a vendor with nothing
    known is left out entirely — the line never says "error". The one number
    that is not a vendor reading, the Kimi 5h ledger estimate, is marked
    ``~`` (see :func:`_window_text`). The ONE renderer, so the bash and PowerShell status-line scripts
    cannot drift from each other: both print what this returns.

    WINDOWS only. A vendor's ledger total is collected and still rides the
    snapshot (``?format=json`` carries it) but is not rendered anywhere — it
    measures what this one gateway relayed, not the account's plan usage. The
    picker label and the launcher card had already dropped it; this renderer
    was the last surface still printing it, and the same fact shown in one
    place and not the others is worse than either choice made everywhere.
    """
    segments: list[str] = []
    for vendor in snapshot.get("vendors") or ():
        parts = [_window_text(w) for w in vendor.get("windows") or () if _known(w)]
        latch = latch_text(vendor.get("api_key_latch") or ())
        if parts or latch:
            text = " · ".join(parts)
            segments.append(
                f"{vendor.get('label', vendor.get('id', '?'))} "
                + (f"{text} {latch}" if text and latch else text or latch),
            )
    return " │ ".join(segments)


def latch_text(latches: "Sequence[Mapping[str, Any]]") -> str:
    """``→ API key until 14:00`` (every model) / ``→ Opus on API key until
    Mon 14:00`` (one model's weekly limit) for the snapshot's
    ``api_key_latch`` entries, or ``""``. Local clock time; the weekday is
    added when the reset is more than 20 hours away."""
    parts: list[str] = []
    for latch in latches:
        until = _reset_epoch(latch.get("until"))
        if until is None:
            continue
        scope = str(latch.get("scope") or "")
        fmt = "%a %H:%M" if until - time.time() > 20 * 3600 else "%H:%M"
        who = "" if scope in ("", "all") else f"{scope.capitalize()} on "
        parts.append(f"→ {who}API key until {time.strftime(fmt, time.localtime(until))}")
    return " · ".join(parts)


# ── the picker-label suffix (what /v1/models appends to a vendor row) ────
#: Shortest window first — the one that resets soonest — then the week, then
#: any per-model week. The owner's own example reads ``5h 10% · wk 72% used``.
_WINDOW_ORDER = {"5h": 0, "weekly": 1}


def _window_rank(window_id: str) -> int:
    if window_id in _WINDOW_ORDER:
        return _WINDOW_ORDER[window_id]
    return 2 if window_id.startswith("weekly:") else 3


def _local_clock(epoch_s: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch_s))


def label_suffix(vendor: Mapping[str, Any], *, now: float, fresh_for_s: float) -> str:
    """``" · 5h 10% · wk 72% used"`` for one snapshot vendor, or ``""``.

    What a picker row carries after its own name. The client fetches
    ``/v1/models`` once per session start and never again, so this text is
    FROZEN for the life of the session — which decides three things:

    * **used, never remaining**, and said once at the end so no window reads
      as the other kind;
    * **the age is a clock time, not a duration**: when the reading is older
      than one refresh interval at the moment it is served, ``(as of 14:05)``
      is appended — "12m old" would still be printed an hour later, while a
      clock time stays true for as long as the label is on screen;
    * **unknown is absence**: an unknown or stale window is left out (the
      snapshot has already aged it to ``null``) and a vendor with nothing
      known gets no suffix at all — never ``0%``. A vendor with no quota
    source has nothing to show: the gateway's own token ledger is COLLECTED
    (it stays in the snapshot) but never rendered — it measures what this
    one gateway routed, not the account's plan usage.
    """
    windows = sorted(
        (w for w in vendor.get("windows") or () if _known(w)),
        key=lambda w: _window_rank(str(w.get("id") or "")),
    )
    observed = [
        seen for seen in (_reset_epoch(w.get("fetched_at")) for w in windows)
        if seen is not None
    ]
    text = " · ".join(_window_text(w) for w in windows) + " used" if windows else ""
    if not text:
        return ""
    if observed and now - min(observed) > fresh_for_s:
        text += f" (as of {_local_clock(min(observed))})"
    return f" · {text}"


def label_suffixes(snapshot: Mapping[str, Any]) -> dict[str, str]:
    """``{vendor id: suffix}`` for every vendor with something known.

    Pure: ages against the snapshot's own ``generated_at`` and
    ``refresh_interval_s``, so it needs no clock of its own and cannot
    disagree with the snapshot it renders.
    """
    now = _reset_epoch(snapshot.get("generated_at"))
    interval = snapshot.get("refresh_interval_s")
    if now is None or not isinstance(interval, (int, float)) or isinstance(interval, bool):
        return {}
    out: dict[str, str] = {}
    for vendor in snapshot.get("vendors") or ():
        vendor_id = vendor.get("id")
        suffix = label_suffix(vendor, now=now, fresh_for_s=float(interval))
        if isinstance(vendor_id, str) and suffix:
            out[vendor_id] = suffix
    return out


__all__ = [
    "DEFAULT_JITTER_S",
    "DEFAULT_KIMI_5H_TOKEN_BUDGET",
    "DEFAULT_REFRESH_S",
    "KIMI_5H_BUDGET_ENV",
    "KIMI_5H_ESTIMATE_FIELDS",
    "LedgerMonthCounter",
    "SOURCE_LEDGER_ESTIMATE",
    "STALE_AFTER_S",
    "UsageWindows",
    "label_suffix",
    "label_suffixes",
    "month_start",
    "parse_anthropic_usage",
    "parse_kimi_usages",
    "parse_unified_headers",
    "parse_zai_quota",
    "effective_percent",
    "latch_text",
    "render_line",
    "roll_window_end",
    "session_fetcher",
]
