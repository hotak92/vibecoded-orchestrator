# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Numeric tunables read from the environment — the one reader for each type.

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
(per hook call, per search). A reader that SAYS something about a bad value
(``ollama_generate.generate_num_ctx``, ``embedding_service`` request
timeout, ``kg_query_core._safe_float``) or gives a value an extra meaning
(``schema_migration_runner._resolve_edge_timeout``: ``<= 0`` = no timeout)
keeps a thin wrapper around these readers for that extra only.

:func:`env_float` (v0.2.102 W6-R5FIX) is the same reader for floats, with
one more bound: ``above`` — a STRICT lower bound (``value > above``, else
``default``), the "positive float" reading (``minimum`` is inclusive, and
``minimum=0`` would accept ``0.0``). Comparisons are the plain ``<`` / ``>``
the replaced readers used, so their handling of ``nan`` / ``inf`` is kept
exactly (``nan`` passes ``minimum``/``maximum`` and fails ``above``).

Stdlib-only, so it imports wherever ``vco_lib`` does. Code that cannot
import ``vco_lib`` keeps a documented copy that names this module and is
pinned by ``tests/test_v02102_env_values.py``:
``templates/scripts/summary_backends.py`` (``_int_env``, ``_float_env``),
``templates/scripts/generate-code-summary.py`` (``resolve_max_per_run`` /
``resolve_batch_size``) and ``claude_mcp_servers/code_embedding_service/server.py``
(``_env_int`` / ``_env_float``: that file also runs in the container image,
which has no ``vco_lib``).

Readers deliberately NOT routed here (v0.2.102 W6-R5FIX), each because its
grammar differs and routing it would change what it accepts:

* ``kg_reviewer_backend`` max-turns and ``codegraph_extras_refresh`` min
  interval — plain ASCII digits only, unstripped (``" 5"``, ``"+5"``,
  ``"1_000"`` are refused; the first must match its shell mirrors);
* module-level constants of the form ``int(os.getenv("X", "N"))`` (the
  ``GRPC_PORT`` family, ``CODE_EMBED_PORT``, the code-tier knobs in the
  weaviate MCP, ``RL_MIN_ANSWER_TOKENS_FOR_CITATION`` in ``rl_state``) —
  they RAISE on a malformed value at import, so moving them to a
  falls-back-to-default reader is a behaviour change, not a consolidation.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional, overload

__all__ = ["env_int", "env_float"]


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


def _raw_text(name: str, env: Optional[Mapping[str, str]]) -> Optional[str]:
    """The stripped value of ``name``, or ``None`` when unset / blank."""
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


@overload
def env_float(
    name: str,
    default: float,
    *,
    env: Optional[Mapping[str, str]] = None,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    above: Optional[float] = None,
    clamp: bool = False,
) -> float: ...


@overload
def env_float(
    name: str,
    default: None,
    *,
    env: Optional[Mapping[str, str]] = None,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    above: Optional[float] = None,
    clamp: bool = False,
) -> Optional[float]: ...


def env_float(
    name: str,
    default: Optional[float],
    *,
    env: Optional[Mapping[str, str]] = None,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    above: Optional[float] = None,
    clamp: bool = False,
) -> Optional[float]:
    """Read ``name`` from ``env`` (default: ``os.environ``) as a float.

    Same rules as :func:`env_int`: unset / blank / unparseable ->
    ``default``; ``minimum`` / ``maximum`` are inclusive and yield
    ``default`` (or, with ``clamp=True``, the bound crossed). ``above`` is a
    strict lower bound that always yields ``default`` (never clamped: there
    is no "just above" value to clamp to).
    """
    text = _raw_text(name, env)
    if text is None:
        return default
    try:
        value = float(text)
    except ValueError:
        return default
    if above is not None and not value > above:
        return default
    if minimum is not None and value < minimum:
        return minimum if clamp else default
    if maximum is not None and value > maximum:
        return maximum if clamp else default
    return value
