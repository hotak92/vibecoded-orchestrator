# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Integer tunables read from the environment — the one reader.

v0.2.102 W6-FIN: VCO had a private ``_env_int`` / ``_int_env`` /
``_positive_int_env`` in a dozen modules, each the same few lines with small
differences (whether 0 or a negative value is accepted, whether a value out
of range falls back to the default or is clamped). :func:`env_int` is the one
home; each caller states its range through ``minimum`` / ``maximum`` /
``clamp`` so its behaviour is visible at the call site.

Rules, identical for every caller:

* unset, empty or whitespace-only → ``default``;
* not an integer after stripping (``"abc"``, ``"1.5"``) → ``default``;
* below ``minimum`` / above ``maximum`` → ``default``, or the bound itself
  when ``clamp=True``;
* otherwise the integer.

Silent by design: a bad tunable never raises and never logs here — every
variant this replaced behaved that way, and several are read on hot paths
(per hook call, per search).

Stdlib-only, so it imports wherever ``vco_lib`` does. Scripts that are
stdlib-only by contract and cannot import ``vco_lib`` keep a documented copy
that names this module and is pinned by ``tests/test_v02102_env_values.py``.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional, overload

__all__ = ["env_int"]


@overload
def env_int(
    name: str,
    default: int,
    *,
    env: Optional[Mapping[str, str]] = None,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    clamp: bool = False,
) -> int: ...


@overload
def env_int(
    name: str,
    default: None,
    *,
    env: Optional[Mapping[str, str]] = None,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    clamp: bool = False,
) -> Optional[int]: ...


def env_int(
    name: str,
    default: Optional[int],
    *,
    env: Optional[Mapping[str, str]] = None,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    clamp: bool = False,
) -> Optional[int]:
    """Read ``name`` from ``env`` (default: ``os.environ``) as an int.

    ``minimum`` / ``maximum`` are inclusive bounds. A value outside them
    yields ``default`` — or, with ``clamp=True``, the bound it crossed.
    ``minimum=1`` is the common "positive integer" reading.
    """
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None:
        return default
    text = str(raw).strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return minimum if clamp else default
    if maximum is not None and value > maximum:
        return maximum if clamp else default
    return value
