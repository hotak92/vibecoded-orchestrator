# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The launcher Home "Model providers" switch, honoured by the RUNNING gateway
(v0.2.106).

The owner (2026-11-10): "I should be able to manually enable/disable providers
with the projects/gateway auto-updating on what I set." Until v0.2.106 the
switch reached the bundle (definitions parked) and the rendered guidance, but
the gateway kept serving a switched-off vendor until… never: nothing under
``model_router/`` read the row. This module is the read.

The setting itself lives in ``vco_lib.model_providers`` (one ``launcher.db``
``app_state`` row, a JSON array of the OFF vendor ids) — this module adds NO
second implementation of the encoding (rung A of the cross-language ladder);
it only caches the answer.

Two rules, both inherited from the setting's own module and restated because
the gateway is where they bite:

* **Never a DB read per request.** The answer is cached for a short TTL
  (``VCT_MODEL_GATEWAY_PROVIDER_SWITCH_TTL_S``, default
  :data:`DEFAULT_TTL_S` seconds; ``0`` disables the cache). A flip lands
  within one TTL — no restart, no bundle update — and the hot path pays at
  most one sqlite read per TTL window.
* **A read failure disables NOTHING.** Switching a provider off is a
  subtraction (models hidden, requests refused), so a failed read must never
  invent one: an exception, an unreadable DB or a corrupt row answers the
  empty set, and that answer is cached too (a locked DB must not be hammered
  once per request either). This is ``vco_lib.model_providers``' soft-fail
  rule carried into the daemon.

The cache is process-global: every handler in the one gateway process shares
it. Tests inject the reader/clock and call :func:`reset`.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, FrozenSet, Optional

logger = logging.getLogger("vct.model_gateway.provider_switch")

#: Env knob for the cache TTL (seconds). ``0`` reads through every call.
#: Named in the gateway's own ``VCT_MODEL_GATEWAY_*`` family.
ENV_TTL_S = "VCT_MODEL_GATEWAY_PROVIDER_SWITCH_TTL_S"

#: Default cache TTL: a flip is live within two seconds, and the hot path
#: pays at most one launcher.db read per window.
DEFAULT_TTL_S = 2.0

Reader = Callable[[], FrozenSet[str]]

_cache_at: float = 0.0
_cache_ready = False
_cache_value: FrozenSet[str] = frozenset()


def reset() -> None:
    """Drop the cached answer (tests; the next call re-reads)."""
    global _cache_at, _cache_ready, _cache_value
    _cache_at = 0.0
    _cache_ready = False
    _cache_value = frozenset()


def _ttl_s() -> float:
    from vco_lib.env_values import env_float

    return env_float(ENV_TTL_S, DEFAULT_TTL_S, minimum=0.0) or 0.0


def _read_live() -> FrozenSet[str]:
    """The setting's own reader (imported lazily: the gateway package must
    stay importable without ``vco_lib`` on sys.path until a request path
    actually needs the switch)."""
    from vco_lib.model_providers import disabled_vendors as _read

    return frozenset(_read())


def disabled_vendors(
    *,
    now: Optional[Callable[[], float]] = None,
    ttl_s: Optional[float] = None,
) -> FrozenSet[str]:
    """The vendor ids switched OFF in the launcher, TTL-cached.

    Any read failure answers the EMPTY set (see the module docstring): the
    gateway never takes a provider away on a setting it could not read.
    """
    global _cache_at, _cache_ready, _cache_value
    clock = now or time.monotonic
    ttl = _ttl_s() if ttl_s is None else ttl_s
    moment = clock()
    if _cache_ready and ttl > 0.0 and moment - _cache_at < ttl:
        return _cache_value
    try:
        value = frozenset(_read_live())
    except Exception as exc:  # noqa: BLE001 — a read never disables a provider
        logger.warning(
            "model-gateway: the provider switch could not be read (%s); "
            "treating every provider as enabled", type(exc).__name__)
        value = frozenset()
    _cache_at = moment
    _cache_ready = True
    _cache_value = value
    return value


def refusal_text(vendor_id: str, display: str) -> str:
    """The one sentence every gateway refusal surface shares.

    Names the cause (the user switched the provider off) and the remedy (where
    to switch it back on), so a refused request, a picker row's absence and a
    pre-flight denial all explain themselves the same way. ``display`` is the
    vendor's human name (``model_router.vendors.vendor_display_name``).
    """
    shown = display.strip() or vendor_id
    return (
        f"the {shown} provider ({vendor_id!r}) is switched off in the VCO "
        f"launcher (Home → Model providers). Switch it back on there to use "
        f"its models again."
    )


__all__ = [
    "DEFAULT_TTL_S",
    "ENV_TTL_S",
    "disabled_vendors",
    "refusal_text",
    "reset",
]
