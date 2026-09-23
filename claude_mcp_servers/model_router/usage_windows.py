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
QwenCloud  monthly credits, NO source   the gateway ledger's tokens for the
                                        current month — tokens, never a %
========== ============================ ======================================

Four rules shape everything below.

**Unknown is not zero.** Every source is undocumented and one of them changed
its schema during 2026. A window that is missing, unparseable, older than
:data:`STALE_AFTER_S` or past its own reset time reads ``percent: null`` with
the reason beside it — never 0 % (reads "plenty left") and never 100 % (reads
"blocked"). A vendor with no programmatic quota gets a token count LABELLED as
tokens, never an invented percentage.

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
from typing import Any, Awaitable, Callable, Mapping, Optional

import aiohttp

from .vendors import OAUTH_BETA, Vendor, vendor_display_name

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

ANTHROPIC_USAGE_PATH = "/api/oauth/usage"

SOURCE_OAUTH_USAGE = "oauth_usage"
SOURCE_UNIFIED_HEADERS = "unified_headers"
SOURCE_ZAI_QUOTA = "zai_quota"
SOURCE_LEDGER = "gateway_ledger"

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

    Called on an executor thread, never on the event loop.
    """

    def __init__(self) -> None:
        self._identity: Optional[tuple[int, int]] = None
        self._offset = 0
        self._period: Optional[float] = None
        self._first_ts: Optional[float] = None
        self._totals: dict[str, list[int]] = {}

    def _reset(self, identity: Optional[tuple[int, int]], period: float) -> None:
        self._identity = identity
        self._offset = 0
        self._period = period
        self._first_ts = None
        self._totals = {}

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
                self._consume(chunk[: end + 1], period)
                self._offset += end + 1
        counted_since = (
            self._first_ts if self._first_ts is not None and self._first_ts > period else None
        )
        return period, counted_since, {k: (v[0], v[1]) for k, v in self._totals.items()}

    def _consume(self, blob: bytes, period: float) -> None:
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
            if ts < period:
                continue
            route = row.get("route")
            if not isinstance(route, str):
                continue
            tokens = 0
            for name in ("input_tokens", "cache_creation_input_tokens",
                         "cache_read_input_tokens", "output_tokens"):
                value = row.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    tokens += value
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
        self._header_windows: dict[str, Window] = {}
        self._usage: dict[str, VendorUsage] = {
            ANTHROPIC_ID: VendorUsage(ANTHROPIC_ID, ANTHROPIC_LABEL, "Claude subscription"),
        }
        for vendor in vendors.values():
            self._usage[vendor.vendor_id] = VendorUsage(
                vendor.vendor_id,
                vendor.short_name.strip() or vendor_display_name(vendor),
                vendor_display_name(vendor),
            )

    # ── passive capture (request path: sync, cheap, never raises) ───────
    def observe_anthropic_headers(self, headers: Mapping[str, str]) -> None:
        """Take the unified rate-limit headers off a relayed first-party answer."""
        for window in parse_unified_headers(headers, now=self._clock()):
            self._header_windows[window.id] = window

    # ── refresh scheduling ───────────────────────────────────────────────
    @property
    def refreshing(self) -> bool:
        return self._task is not None and not self._task.done()

    def request_refresh(self) -> bool:
        """A READ wants the numbers: schedule a background refresh if one is
        due. Returns whether it did; never awaits the refresh."""
        return self._schedule_if_due()

    def note_activity(self) -> bool:
        """Chat traffic just passed through: keep the cache warm.

        Called synchronously from ``/v1/messages`` on every request, so it is
        O(1) and never awaits: a timestamp, the due-check, and at most once
        per interval a ``create_task``. Returns whether it scheduled one.
        """
        self._last_activity = self._clock()
        return self._schedule_if_due()

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
        status, payload = await self._fetch(
            vendor.quota_url,
            # RAW key, no scheme: the documented community usage and the
            # 2026-09-23 probe agree, and the endpoint is not the vendor's
            # Anthropic-shaped API, so ``auth_scheme`` does not apply.
            {"Authorization": key, "Accept-Language": "en-US,en"},
        )
        self._apply(
            usage, status, payload, vendor.vendor_id,
            lambda body: parse_zai_quota(body, now=self._clock()),
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
        for vendor in self._vendors.values():
            if vendor.quota_url:
                continue
            usage = self._usage[vendor.vendor_id]
            tokens, requests = totals.get(f"vendor:{vendor.vendor_id}", (0, 0))
            usage.tokens = TokenWindow(tokens, requests, period, since, now)
            usage.state = STATE_OK if path is not None else STATE_UNKNOWN
            usage.problem = None if path is not None else "the usage ledger has no home"

    # ── reading ──────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        """The JSON-ready snapshot, aged at read time. Pure read of memory."""
        now = self._clock()
        vendors = []
        for usage in self._usage.values():
            windows = usage.windows
            if usage.id == ANTHROPIC_ID:
                windows = _merge_headers(windows, self._header_windows)
            entry: dict[str, Any] = {
                "id": usage.id,
                "label": usage.label,
                "name": usage.name,
                "state": usage.state,
                "problem": usage.problem,
                "plan": usage.plan,
                "windows": [_window_dict(w, now) for w in windows],
            }
            if usage.id == ANTHROPIC_ID and usage.state != STATE_OK and any(
                w["percent"] is not None for w in entry["windows"]
            ):
                # The endpoint failed but the relayed headers answered.
                entry["state"] = STATE_OK
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
def format_tokens(count: int) -> str:
    """``1234567`` -> ``1.2M``; ``950000`` -> ``950K``; ``830`` -> ``830``."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M".replace(".0M", "M")
    if count >= 1_000:
        return f"{count / 1_000:.0f}K"
    return str(count)


def _known(window: Mapping[str, Any]) -> bool:
    percent = window.get("percent")
    return isinstance(percent, (int, float)) and not isinstance(percent, bool)


def _window_text(window: Mapping[str, Any]) -> str:
    """``5h 31%`` — the one spelling of a known window, shared by the status
    line and the picker label so the two cannot round differently."""
    return f"{window['label']} {window['percent']:.0f}%"


def render_line(snapshot: Mapping[str, Any]) -> str:
    """One compact line, e.g. ``Claude 5h 31% · wk 27% · Fable 12% │ GLM 5h 10%``.

    Silent degrade: an unknown window is left out, and a vendor with nothing
    known is left out entirely — the line never says "error" and never shows
    a guess. The ONE renderer, so the bash and PowerShell status-line scripts
    cannot drift from each other: both print what this returns.
    """
    segments: list[str] = []
    for vendor in snapshot.get("vendors") or ():
        parts = [_window_text(w) for w in vendor.get("windows") or () if _known(w)]
        tokens = vendor.get("tokens")
        if isinstance(tokens, Mapping) and isinstance(tokens.get("tokens"), int) and tokens["tokens"] > 0:
            parts.append(f"{format_tokens(tokens['tokens'])} tok/mo")
        if parts:
            segments.append(f"{vendor.get('label', vendor.get('id', '?'))} " + " · ".join(parts))
    return " │ ".join(segments)


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


def _local_day(epoch_s: float) -> str:
    """``Sep 2`` — built by hand because ``%-d`` does not exist on Windows."""
    moment = time.localtime(epoch_s)
    return f"{time.strftime('%b', moment)} {moment.tm_mday}"


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
      known gets no suffix at all — never ``0%``.

    A vendor with no quota source (QwenCloud) shows the ledger's token total
    instead — ``1.2M tokens used this month`` — when it is positive and
    fresh. When the ledger can only vouch for part of the month (it began
    mid-month, or rotated), the span it covers is named rather than passed
    off as the month's: ``1.2M tokens used since Sep 2``.
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
    tokens = vendor.get("tokens")
    if not text and isinstance(tokens, Mapping):
        count = tokens.get("tokens")
        seen = _reset_epoch(tokens.get("fetched_at"))
        if (
            isinstance(count, int) and not isinstance(count, bool) and count > 0
            and seen is not None and now - seen <= STALE_AFTER_S
        ):
            since = _reset_epoch(tokens.get("counted_since"))
            span = "this month" if since is None else f"since {_local_day(since)}"
            text = f"{format_tokens(count)} tokens used {span}"
            observed.append(seen)
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
    "DEFAULT_REFRESH_S",
    "LedgerMonthCounter",
    "STALE_AFTER_S",
    "UsageWindows",
    "format_tokens",
    "label_suffix",
    "label_suffixes",
    "month_start",
    "parse_anthropic_usage",
    "parse_unified_headers",
    "parse_zai_quota",
    "render_line",
    "session_fetcher",
]
