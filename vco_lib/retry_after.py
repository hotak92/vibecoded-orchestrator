# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""ONE home for the client side of the CodeEmbed service's 503 contract.

v0.2.102 (round 2): the code-embedding service QUEUES requests instead of
shedding them, and every refusal it can still emit (queue-wait bound exceeded,
CUDA-OOM load budget exhausted, inference OOM) is a **503 with a Retry-After
header**. Two client retry layers exist — ``EmbeddingService._retry_once_on_503``
(sync, used by the analyzer / CLI / MCP query path) and the weaviate MCP's
``_inline_code_embed_http`` fallback (async). Both must honour the header on the
same policy, so the mechanism lives HERE once (CLAUDE.md "one concern, one
home"; the policy values each caller passes stay its own):

* :func:`parse_retry_after` — the header value (delta-seconds or HTTP-date).
* :func:`error_retry_after` — pull a Retry-After off a raised error (attribute
  first, attached response headers second).
* :func:`is_503_error` — the long-standing "503" detection (status attribute OR
  message substring, so every provider's error shape keeps working).
* :func:`resolve_503_delay_scale` / :func:`resolve_503_tolerance` — the two env
  knobs bounding the retries.
* :func:`resolve_503_interactive_tolerance` + the override pair
  (:func:`set_503_tolerance_override` / :func:`reset_503_tolerance_override`)
  — the INTERACTIVE bound (v0.2.102 r3). The batch tolerance (420 s) is right
  for analyzer / sync / backfill callers, but an INTERACTIVE caller — the
  weaviate-MCP ``search_code_graph`` query embedding a user's turn waits on —
  must fail fast instead of silently blocking for minutes. The override is a
  ``contextvars.ContextVar`` so the interactive bound reaches the PRODUCTION
  retry loop (``_retry_once_on_503`` resolves its tolerance per call through
  :func:`resolve_503_tolerance`) without threading a parameter through every
  batch call site; ``asyncio.to_thread`` copies the context, so it also holds
  inside the MCP's worker-thread leg.

The constants below encode the TWO cross-process invariants of the contract;
if the server defaults move, these must move with them (the server side lives
in ``claude_mcp_servers/code_embedding_service/server.py``):

``CODE_EMBED_CLIENT_TIMEOUT_SECS``
    A client request timeout MUST EXCEED the server's queue wait
    (``CODE_EMBED_QUEUE_WAIT_SECS``, default 600 s) — otherwise a client
    aborts a request the server is still legitimately holding in its FIFO
    queue, which is a drop by another name. 660 s = 600 s + margin.
``DEFAULT_503_TOLERANCE_SECS``
    The total time a client keeps retrying a 503 MUST be at least the server's
    CUDA-OOM load budget (``CODE_EMBED_LOAD_OOM_RETRIES`` ×
    ``CODE_EMBED_LOAD_OOM_BACKOFF_SECS``, default 12 × 30 s = 360 s) — so a
    retrying caller rides out one full server-side recovery cycle instead of
    giving up mid-budget. 420 s > 360 s.
``DEFAULT_503_INTERACTIVE_TOLERANCE_SECS``
    The same retry, seen from an INTERACTIVE call site (a tool call the user's
    turn blocks on), gives up MUCH sooner: a minutes-scale silent wait is a
    worse outcome than a fast, actionable error. It is a BOUND, never a raise:
    the effective interactive tolerance is ``min(interactive knob, batch
    tolerance)`` so a user-set batch fail-fast is never lengthened.

Stdlib-only and dependency-free by design: it is imported from the embedding
provider adapters, from ``embedding_service``, and from inside the weaviate MCP
process — none of which may pay an import cycle or a heavy dependency for a
retry policy.
"""
from __future__ import annotations

import os
import time
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

__all__ = [
    "CODE_EMBED_CLIENT_TIMEOUT_SECS",
    "DEFAULT_503_TOLERANCE_SECS",
    "DEFAULT_503_INTERACTIVE_TOLERANCE_SECS",
    "DEFAULT_503_FALLBACK_BACKOFFS",
    "EMBED_503_TOLERANCE_ENV",
    "EMBED_503_INTERACTIVE_TOLERANCE_ENV",
    "EMBED_503_RETRY_DELAY_ENV",
    "parse_retry_after",
    "error_retry_after",
    "is_503_error",
    "resolve_503_delay_scale",
    "resolve_503_tolerance",
    "resolve_503_interactive_tolerance",
    "set_503_tolerance_override",
    "reset_503_tolerance_override",
    "compute_503_delay",
]

#: Per-request wall-clock deadline for CodeEmbed HTTP clients. MUST exceed the
#: server's ``CODE_EMBED_QUEUE_WAIT_SECS`` default (600 s) — see module docstring.
CODE_EMBED_CLIENT_TIMEOUT_SECS = 660.0

#: Default total time a client keeps retrying a 503 (seconds). MUST be ≥ the
#: server's default CUDA-OOM load budget (12 × 30 s = 360 s) — see module
#: docstring. Override via ``VCO_EMBED_503_TOLERANCE_SECS``.
DEFAULT_503_TOLERANCE_SECS = 420.0

#: Default INTERACTIVE bound (seconds) — the tolerance an interactive call
#: site (the weaviate-MCP query-embedding path) uses instead of the batch one.
#: See the module docstring; override via
#: ``VCO_EMBED_503_INTERACTIVE_TOLERANCE_SECS``.
DEFAULT_503_INTERACTIVE_TOLERANCE_SECS = 90.0

EMBED_503_TOLERANCE_ENV = "VCO_EMBED_503_TOLERANCE_SECS"
EMBED_503_INTERACTIVE_TOLERANCE_ENV = "VCO_EMBED_503_INTERACTIVE_TOLERANCE_SECS"
#: Back-compat C-9 knob: SCALES computed delays proportionally; ``0`` means
#: zero-delay legacy-bounded mode (see :func:`resolve_503_delay_scale`).
EMBED_503_RETRY_DELAY_ENV = "VCO_EMBED_503_RETRY_DELAY"

#: Interactive-bound override for :func:`resolve_503_tolerance` (v0.2.102 r3).
#: Set by the weaviate-MCP query path around its ``EmbeddingService`` call so
#: the PRODUCTION retry loop (``_retry_once_on_503``, which resolves its
#: tolerance per call) is bounded at the interactive tolerance — without a
#: parameter threaded through every batch call site. Task/context-local by
#: construction, so concurrent tool calls cannot cross-talk; ``to_thread``
#: copies the context into the worker thread.
_503_TOLERANCE_OVERRIDE: "ContextVar[Optional[float]]" = ContextVar(
    "vco_embed_503_tolerance_override", default=None
)

#: Fallback schedule (seconds) used when a 503 carries NO usable Retry-After —
#: the v0.2.77 5c backoff, kept as the ONE canonical tuple
#: (``embedding_service._EMBED_503_RETRY_BACKOFFS`` is an alias of this, and the
#: weaviate-MCP inline fallback passes it too — no third copy). The last entry
#: repeats for attempts past the end of the schedule.
DEFAULT_503_FALLBACK_BACKOFFS: "tuple[float, ...]" = (2.0, 5.0, 10.0)


def parse_retry_after(raw: Any) -> Optional[float]:
    """Parse an HTTP ``Retry-After`` value into seconds (≥ 0), or ``None``.

    Accepts the two forms RFC 9110 defines — delta-seconds and an HTTP-date —
    plus a plain number (an adapter that already parsed the header). Anything
    unparseable is ``None`` ("no usable hint"), never an exception: a retry
    layer must not fail because a server sent a malformed header.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):  # bool is an int subclass — never a valid header
        return None
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())


def error_retry_after(exc: BaseException) -> Optional[float]:
    """Retry-After carried by a raised error, in seconds, or ``None``.

    Reads, in order: a ``retry_after`` attribute (the CodeEmbed adapter
    attaches it — the sanctioned channel), then ``Retry-After`` on an attached
    ``response``/``headers`` object (requests-style exceptions). Never raises.
    """
    direct = parse_retry_after(getattr(exc, "retry_after", None))
    if direct is not None:
        return direct
    headers = getattr(exc, "headers", None)
    response = getattr(exc, "response", None)
    if headers is None and response is not None:
        headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")  # type: ignore[union-attr]
    except AttributeError:
        return None
    return parse_retry_after(raw)


def is_503_error(exc: BaseException) -> bool:
    """True when ``exc`` represents an HTTP 503.

    Keeps the long-standing detection contract of
    ``EmbeddingService._retry_once_on_503`` — the "503" message substring —
    and adds the structured ``status_code`` attribute the CodeEmbed adapter
    now attaches. Non-503 errors are NEVER retryable through this path.
    """
    status = getattr(exc, "status_code", None)
    if status == 503:
        return True
    return "503" in str(exc)


def resolve_503_tolerance() -> float:
    """Total seconds a client keeps retrying 503s (``VCO_EMBED_503_TOLERANCE_SECS``).

    An in-context interactive override (see :func:`set_503_tolerance_override`)
    takes precedence — the MCP query path sets it so the retry loop it invokes
    is bounded at the interactive tolerance instead of the batch one. Malformed
    or negative → the default (a knob must not disable the guard by accident).
    An explicit ``0`` is honoured: retry-less fail-fast for callers that want
    the pre-v0.2.102 tight behaviour.
    """
    override = _503_TOLERANCE_OVERRIDE.get()
    if override is not None:
        return override
    raw = os.getenv(EMBED_503_TOLERANCE_ENV)
    if raw is None:
        return DEFAULT_503_TOLERANCE_SECS
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_503_TOLERANCE_SECS
    if val < 0:
        return DEFAULT_503_TOLERANCE_SECS
    return val


def resolve_503_interactive_tolerance() -> float:
    """The INTERACTIVE bound on 503 retries (``VCO_EMBED_503_INTERACTIVE_TOLERANCE_SECS``).

    Same parsing rules as :func:`resolve_503_tolerance` (malformed/negative →
    default, explicit ``0`` = fail-fast), but the result is CLAMPED to the
    batch tolerance — the interactive bound may shorten a user-configured
    global tolerance, never lengthen it: ``min(interactive, batch)``.
    """
    raw = os.getenv(EMBED_503_INTERACTIVE_TOLERANCE_ENV)
    val: float = DEFAULT_503_INTERACTIVE_TOLERANCE_SECS
    if raw is not None:
        try:
            candidate = float(raw)
        except (TypeError, ValueError):
            candidate = DEFAULT_503_INTERACTIVE_TOLERANCE_SECS
        if candidate >= 0:
            val = candidate
    return min(val, resolve_503_tolerance())


def set_503_tolerance_override(tolerance_secs: float) -> "Token[Optional[float]]":
    """Bound ``resolve_503_tolerance`` to ``tolerance_secs`` in this context.

    Used by INTERACTIVE call sites (the weaviate-MCP query-embedding path)
    around the ``EmbeddingService`` call whose internal retry loop resolves
    its tolerance per call. Returns the token ``reset_503_tolerance_override``
    needs — always pair set/reset in ``try``/``finally``.
    """
    return _503_TOLERANCE_OVERRIDE.set(float(tolerance_secs))


def reset_503_tolerance_override(token: "Token[Optional[float]]") -> None:
    """Undo :func:`set_503_tolerance_override` (pass the token it returned)."""
    _503_TOLERANCE_OVERRIDE.reset(token)


def resolve_503_delay_scale(base: float = 2.0) -> Optional[float]:
    """Scale factor from ``VCO_EMBED_503_RETRY_DELAY`` (back-compat C-9 knob).

    Semantics preserved from ``_retry_once_on_503``: the value REPLACES the
    first scheduled backoff (``base``, default the schedule's 2.0 s) and every
    other delay — including a server-provided Retry-After — is multiplied by
    ``value / base``. ``0`` → scale 0.0 → every delay is 0, which callers treat
    as "legacy bounded mode" (finite attempts, no sleeps — used by tests).
    Unset or malformed → ``None`` (delays used unscaled).
    """
    raw = os.getenv(EMBED_503_RETRY_DELAY_ENV)
    if raw is None:
        return None
    try:
        first = float(raw)
    except (TypeError, ValueError):
        return None  # malformed → unscaled (a knob must not be a kill switch)
    if first < 0:
        first = 0.0
    return first / (base or 1.0)


def monotonic_deadline(tolerance_secs: float) -> float:
    """``time.monotonic()`` deadline for a retry loop with the given tolerance."""
    return time.monotonic() + max(0.0, tolerance_secs)


def compute_503_delay(
    attempt: int,
    retry_after: Optional[float],
    schedule: "tuple[float, ...] | list[float]" = DEFAULT_503_FALLBACK_BACKOFFS,
    scale: Optional[float] = None,
    jitter_frac: float = 0.0,
) -> float:
    """Seconds to wait before retry ``attempt`` (1-based) after a 503.

    The ONE shared delay policy for both 503-retry loops
    (``EmbeddingService._retry_once_on_503`` sync, the weaviate-MCP
    ``_inline_code_embed_http`` async): the server's Retry-After is honoured
    VERBATIM when present (no jitter — it is the server's own recovery
    estimate), else fall back to ``schedule`` (last entry repeats) with optional
    jitter to de-synchronise lock-stepping callers. ``scale`` (the back-compat
    ``VCO_EMBED_503_RETRY_DELAY`` factor) multiplies either. Never negative.
    """
    if retry_after is not None:
        delay = float(retry_after)
        if scale is not None:
            delay *= scale
        return max(0.0, delay)
    if not schedule:
        return 0.0
    idx = min(max(attempt, 1) - 1, len(schedule) - 1)
    delay = float(schedule[idx])
    if scale is not None:
        delay *= scale
    if jitter_frac > 0.0 and delay > 0.0:
        import random

        delay += random.uniform(0.0, delay * jitter_frac)
    return max(0.0, delay)
