# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Vendor keys, resolved at request time through the sanctioned chain.

Three properties this module exists to guarantee:

**A key never reaches argv.** ``/proc/<pid>/cmdline`` is world-readable on
Linux and the equivalent is queryable on Windows, which is why VCO has a
secrets primitive at all. Resolution happens in-process through
``vco_lib.agent_secrets.get`` (hub -> file store -> project ``.env``); no
subprocess is spawned, so there is no command line to leak.

**A key never reaches a log, an error or a file under a project tree.** The
value is returned to exactly one caller, which puts it in one outbound header.
:class:`KeyResult` carries the value and, separately, a ``problem`` string
built only from key NAMES and store locations. ``tests/test_model_router_secrets.py``
asserts that a synthetic value never appears in any message this module
produces, in the log record it emits, or in ``repr()`` of the result.

**Resolution is LAZY and never blocks the event loop.** The first vendor
request resolves the key, not startup — so the daemon comes up on a machine
whose hub is down, and picks the key up later without a restart.
``agent_secrets.get`` performs blocking HTTP and file I/O, so the server calls
:meth:`VendorKeyResolver.aresolve`, which runs it on a worker thread. A
blocking call in the handler would stall ``/health`` for every concurrent
request — the exact failure the non-blocking-health rule exists to prevent.

The import of ``vco_lib.agent_secrets`` is deliberately deferred to first use
rather than done at module scope, so that importing this module (for the
packaging smoke check, or on a machine mid-install) cannot fail on it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .vendors import Vendor

logger = logging.getLogger(__name__)

#: Failures are cached for at most this long, so a burst of parallel requests
#: on a keyless machine does not become a burst of resolver calls, while a key
#: the user adds in the GUI is still picked up within seconds.
NEGATIVE_TTL_CAP_S = 30

SecretGetter = Callable[..., str]


@dataclass
class KeyResult:
    """Outcome of one resolution attempt.

    ``key`` is the only place a value ever appears. ``problem`` is built from
    key names and store paths exclusively.
    """

    key: Optional[str] = None
    problem: Optional[str] = None
    #: ``resolved`` / ``cached`` / ``missing``.
    state: str = "missing"
    #: Which declared key name answered. Names are not secrets.
    resolved_from: Optional[str] = None

    def __repr__(self) -> str:  # pragma: no cover - exercised via assertions
        # Never let a value reach a traceback, a pytest diff or a log record
        # that formats the object.
        return (
            f"KeyResult(state={self.state!r}, resolved_from={self.resolved_from!r}, "
            f"key={'<set>' if self.key else None!r}, problem={self.problem!r})"
        )


@dataclass
class _CacheEntry:
    result: KeyResult
    fetched_at: float


def _default_getter() -> SecretGetter:
    """Import ``vco_lib.agent_secrets.get`` on first use.

    A failure here means a broken install (``install.py`` pip-installs the
    root distribution before this one), so it is raised, not swallowed.
    """
    from vco_lib.agent_secrets import get as _get  # noqa: PLC0415 — deliberate

    return _get


class VendorKeyResolver:
    """Per-vendor key resolution with a small time-bounded cache."""

    def __init__(
        self,
        *,
        project: Optional[str] = None,
        ttl_s: int = 300,
        getter: Optional[SecretGetter] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._project = project
        self._ttl_s = max(1, int(ttl_s))
        self._getter = getter
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}

    def cached_vendor_ids(self) -> tuple[str, ...]:
        """Vendor ids with a live key in cache. Used by ``/health``; touches
        no store, so it cannot block."""
        now = self._clock()
        return tuple(
            sorted(
                vendor_id
                for vendor_id, entry in self._cache.items()
                if entry.result.key and now - entry.fetched_at < self._ttl_s
            )
        )

    def invalidate(self, vendor_id: Optional[str] = None) -> None:
        if vendor_id is None:
            self._cache.clear()
        else:
            self._cache.pop(vendor_id, None)

    def resolve(self, vendor: Vendor) -> KeyResult:
        """Blocking resolution. Callers on the event loop use :meth:`aresolve`."""
        now = self._clock()
        entry = self._cache.get(vendor.vendor_id)
        if entry is not None:
            ttl = self._ttl_s if entry.result.key else min(self._ttl_s, NEGATIVE_TTL_CAP_S)
            if now - entry.fetched_at < ttl:
                if entry.result.key:
                    return KeyResult(
                        key=entry.result.key,
                        state="cached",
                        resolved_from=entry.result.resolved_from,
                    )
                return entry.result

        result = self._resolve_uncached(vendor)
        self._cache[vendor.vendor_id] = _CacheEntry(result=result, fetched_at=now)
        return result

    async def aresolve(self, vendor: Vendor) -> KeyResult:
        """Resolve off the event loop.

        A cache hit is answered inline (no thread hop) so the common path
        costs nothing; only a real store lookup is offloaded.
        """
        now = self._clock()
        entry = self._cache.get(vendor.vendor_id)
        if entry is not None and entry.result.key:
            if now - entry.fetched_at < self._ttl_s:
                return KeyResult(
                    key=entry.result.key,
                    state="cached",
                    resolved_from=entry.result.resolved_from,
                )
        return await asyncio.to_thread(self.resolve, vendor)

    def _resolve_uncached(self, vendor: Vendor) -> KeyResult:
        getter = self._getter
        if getter is None:
            try:
                getter = _default_getter()
            except Exception as exc:  # noqa: BLE001 — reported, never swallowed
                logger.error(
                    "model-gateway: secrets resolver unavailable for vendor %s: %s",
                    vendor.vendor_id,
                    exc,
                )
                return KeyResult(
                    problem=(
                        "the vct-secrets resolver could not be imported "
                        f"({exc}). This is a broken install: re-run "
                        "`python install.py`."
                    ),
                )
            self._getter = getter

        failures: list[str] = []
        for key_name in vendor.secret_keys:
            try:
                value = getter(key_name, project=self._project)
            except Exception as exc:  # noqa: BLE001 — every resolver error is a miss
                # agent_secrets raises a family of typed errors (not found,
                # access denied, hub unreachable, keychain locked). All of them
                # mean "no key from this name"; the TYPE is preserved in the
                # message so the user sees whether it was absent or refused.
                # Relaying the exception text is safe on the value axis: every
                # message that family constructs is built from key NAMES, store
                # PATHS and hub state (read in vco_lib/agent_secrets.py), never
                # from a resolved value — and on this branch no value exists.
                failures.append(f"{key_name}: {type(exc).__name__}: {exc}")
                continue
            if value and value.strip():
                logger.info(
                    "model-gateway: resolved key %r for vendor %s",
                    key_name,
                    vendor.vendor_id,
                )
                return KeyResult(
                    key=value.strip(),
                    state="resolved",
                    resolved_from=key_name,
                )
            failures.append(f"{key_name}: resolved but empty")

        scope = (
            f"project {self._project!r}"
            if self._project
            else "the shared scope (set VCT_MODEL_GATEWAY_SECRET_PROJECT to "
            "look in a project scope instead)"
        )
        problem = (
            f"no key found for vendor {vendor.vendor_id!r}. Tried "
            f"{', '.join(repr(k) for k in vendor.secret_keys)} in {scope}. "
            "Add it in the launcher's Secrets panel, or with "
            f"`vct set --shared --key {vendor.secret_keys[0]}`; the gateway "
            "picks it up within its key-cache TTL without a restart. "
            f"Resolver detail: {'; '.join(failures) if failures else 'no attempt made'}"
        )
        logger.warning(
            "model-gateway: no key for vendor %s (tried %s)",
            vendor.vendor_id,
            ", ".join(vendor.secret_keys),
        )
        return KeyResult(problem=problem)


__all__ = ["KeyResult", "NEGATIVE_TTL_CAP_S", "SecretGetter", "VendorKeyResolver"]
