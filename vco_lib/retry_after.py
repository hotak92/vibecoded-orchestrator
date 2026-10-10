# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""ONE home for the client side of the CodeEmbed service's 503 contract.

v0.2.102 (round 2): the code-embedding service QUEUES requests instead of
shedding them, and every refusal it can still emit (queue-wait bound exceeded,
CUDA-OOM load budget exhausted, inference OOM) is a **503 with a Retry-After
header**. Two client retry layers exist — ``EmbeddingService._retry_once_on_503``
(sync, used by the analyzer / CLI / MCP query path) and the weaviate MCP's
``_inline_code_embed_http`` fallback (async); v0.2.102 W4-E adds the third,
:func:`retry_transient_embed` (the raw-HTTP fallback legs of the migrate /
search CLIs, which also retry connection-refused). All must honour the header
on the same policy, so the mechanism lives HERE once (CLAUDE.md "one concern,
one home"; the policy values each caller passes stay its own):

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
    "is_connection_error",
    "resolve_503_delay_scale",
    "resolve_503_tolerance",
    "resolve_503_interactive_tolerance",
    "resolve_refused_tolerance",
    "set_503_tolerance_override",
    "reset_503_tolerance_override",
    "compute_503_delay",
    "retry_transient_embed",
    "wait_until_reachable",
    "post_code_embed",
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

#: Default INTERACTIVE bound (seconds) on retrying a CONNECTION failure —
#: refused / no listener, i.e. the service is DOWN, not busy (v0.2.102 W4-G,
#: coordinator ruling 2026-10-09). A 503 means the request is queued
#: server-side and is waited on up to the interactive tolerance; a refusal
#: gets only a short bounded retry (enough to ride out a container restart)
#: and then fails loudly. Batch paths keep their long tolerance for both.
#: Override via ``VCO_EMBED_INTERACTIVE_REFUSED_TOLERANCE_SECS``.
DEFAULT_INTERACTIVE_REFUSED_TOLERANCE_SECS = 10.0

EMBED_503_TOLERANCE_ENV = "VCO_EMBED_503_TOLERANCE_SECS"
EMBED_INTERACTIVE_REFUSED_TOLERANCE_ENV = "VCO_EMBED_INTERACTIVE_REFUSED_TOLERANCE_SECS"
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


# Marker substrings (lowercased) of connection-refused style failures raised
# through to a generic RuntimeError by the embedding adapters (the adapters
# wrap ``requests.RequestException`` as ``"<name> network error: <cause>"``,
# so the cause's wording is all a type-less check can read).
_CONNECTION_MESSAGE_MARKERS: tuple[str, ...] = (
    "connection refused",
    "connection error",
    "connection reset",
    "cannot connect",
    "failed to establish",
    "connect call failed",
    "errno 111",
)


def is_connection_error(exc: BaseException) -> bool:
    """True when ``exc`` looks like a connection-level transport failure
    (refused / reset / could not establish) — v0.2.102 W4-E item 6.

    The code-embed service is ONE shared container; a request that lands
    while it is restarting (e.g. mid model-load) fails with connection
    refused, which is TRANSIENT — the owner rule says queued/busy embeds are
    retried on the same bounded schedule as a 503, never dropped on the
    first shot. Timeouts are deliberately NOT matched: a timeout is the
    bounded-post wedge guard firing, not a restart.

    Stdlib-only detection, no requests/httpx/aiohttp imports (this module is
    imported from adapters and inside the weaviate MCP process): any class
    in the MRO whose name contains "connect" (``ClientConnectorError``,
    ``requests.ConnectionError``, ``httpx.ConnectError``, builtin
    ``ConnectionError``/``ConnectionResetError``) or any marker substring in
    the message. Conservative: unknown exceptions fall through to False.
    """
    klass = type(exc)
    for k in klass.__mro__:
        if "connect" in k.__name__.lower():
            return True
    text = str(exc).lower()
    return any(marker in text for marker in _CONNECTION_MESSAGE_MARKERS)


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


def resolve_refused_tolerance(*, interactive: bool) -> float:
    """Total seconds a client keeps retrying a CONNECTION failure.

    THE one policy home for "service down" vs "service busy" (v0.2.102
    W4-G). Batch paths (``interactive=False``) treat a refusal like a 503 and
    get :func:`resolve_503_tolerance` — a restart mid model-load must not
    drop a backfill. Interactive paths (a query a user or a hook is waiting
    on) get ``VCO_EMBED_INTERACTIVE_REFUSED_TOLERANCE_SECS`` (default
    :data:`DEFAULT_INTERACTIVE_REFUSED_TOLERANCE_SECS`), clamped to the 503
    tolerance in force (never longer). Same parsing rules as the other
    knobs: malformed / negative → default, explicit ``0`` = fail-fast.
    """
    bound = resolve_503_tolerance()
    if not interactive:
        return bound
    raw = os.getenv(EMBED_INTERACTIVE_REFUSED_TOLERANCE_ENV)
    val: float = DEFAULT_INTERACTIVE_REFUSED_TOLERANCE_SECS
    if raw is not None:
        try:
            candidate = float(raw)
        except (TypeError, ValueError):
            candidate = DEFAULT_INTERACTIVE_REFUSED_TOLERANCE_SECS
        if candidate >= 0:
            val = candidate
    return min(val, bound)


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


def retry_transient_embed(fn, *args, log=None, interactive: bool = False):
    """Retry a single SYNC embed HTTP call on 503 / connection errors.

    v0.2.102 W4-E item 6 — the ONE home for the raw-HTTP embed clients that
    do not go through :meth:`EmbeddingService._retry_once_on_503` (the
    migrate script's and the search CLI's inline CodeEmbed fallback legs).
    The code-embed service is a shared container: a 503 means its queue-wait
    bound was exceeded, a connection error means it is restarting (e.g. mid
    model-load) — both transient, both retried on the SAME bounded policy as
    the service-backed paths (Retry-After honoured verbatim, fallback
    schedule, ``VCO_EMBED_503_TOLERANCE_SECS`` total wait, then the LAST
    error re-raises — a loud failure, never a silent drop). Non-transient
    errors re-raise immediately.

    Deliberately does NOT widen ``_retry_once_on_503`` itself: at the
    EmbeddingService layer a connection refusal must keep falling through to
    this path's async sibling (the weaviate-MCP inline fallback) rather than
    double-waiting two tolerances (the r3 double-tolerance regression).
    """
    import logging
    import time as _time

    logger = log or logging.getLogger(__name__)
    scale = resolve_503_delay_scale(base=DEFAULT_503_FALLBACK_BACKOFFS[0] or 1.0)
    legacy_bounded = scale == 0.0
    started = _time.monotonic()
    deadline = started + resolve_503_tolerance()
    # v0.2.102 W4-G: a refusal (service DOWN) gets its own, on interactive
    # paths much shorter, bound — see resolve_refused_tolerance.
    refused_tolerance = resolve_refused_tolerance(interactive=interactive)
    refused_deadline = started + refused_tolerance
    last_exc: "Exception | None" = None
    attempt = 0
    while True:
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 — inspect, then re-raise
            if not (is_503_error(exc) or is_connection_error(exc)):
                raise
            last_exc = exc
            attempt += 1
            if legacy_bounded:
                if attempt > len(DEFAULT_503_FALLBACK_BACKOFFS):
                    break  # finite attempts, zero delays (test harness mode)
                delay = 0.0
            else:
                if _time.monotonic() >= deadline:
                    break  # tolerance spent → re-raise below
                if not is_503_error(exc) and _time.monotonic() >= refused_deadline:
                    logger.error(
                        "embed backend unreachable (connection refused / no "
                        "listener) for %.0fs — giving up: %s",
                        refused_tolerance, exc,
                    )
                    break
                delay = compute_503_delay(
                    attempt, error_retry_after(exc), DEFAULT_503_FALLBACK_BACKOFFS, scale
                )
                if not is_503_error(exc):
                    # Never sleep past the refusal bound: the last retry
                    # lands AT it, then the next failure gives up.
                    delay = min(delay, max(0.0, refused_deadline - _time.monotonic()))
            logger.info(
                "embed backend transiently unavailable (503 / restarting): "
                "%s — retry %d after %.1fs",
                exc, attempt, delay,
            )
            if delay > 0:
                _time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def wait_until_reachable(probe, *, interactive: bool, log=None) -> bool:
    """Poll ``probe()`` until it answers True or the REFUSED bound elapses.

    v0.2.102 W6-B2 (owner ruling 2026-10-10): a down embedding container is
    waited on — the hub watchdog restarts it — on the SAME transient policy
    :func:`retry_transient_embed` applies to a refused connection
    (:func:`resolve_refused_tolerance`: ~10 s for an interactive query, the
    batch tolerance for an analyzer walk), then the caller fails LOUDLY. It
    never answers by switching to another embedder. Scale ``0``
    (``VCO_EMBED_503_RETRY_DELAY=0``) keeps the finite zero-delay test mode.
    Returns whether the probe answered.
    """
    import logging
    import time as _time

    logger = log or logging.getLogger(__name__)
    if probe():
        return True
    scale = resolve_503_delay_scale(base=DEFAULT_503_FALLBACK_BACKOFFS[0] or 1.0)
    legacy_bounded = scale == 0.0
    tolerance = resolve_refused_tolerance(interactive=interactive)
    deadline = _time.monotonic() + tolerance
    attempt = 0
    while True:
        attempt += 1
        if legacy_bounded:
            if attempt > len(DEFAULT_503_FALLBACK_BACKOFFS):
                return False
            delay = 0.0
        else:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                logger.error("embed backend unreachable for %.0fs — giving up", tolerance)
                return False
            delay = min(
                compute_503_delay(attempt, None, DEFAULT_503_FALLBACK_BACKOFFS, scale),
                remaining,
            )
        logger.info("embed backend unreachable (restarting?) — re-probe %d after %.1fs",
                    attempt, delay)
        if delay > 0:
            _time.sleep(delay)
        if probe():
            return True


def post_code_embed(
    code_embed_url: str, text: str, *, timeout: float = 60, interactive: bool = False, log=None,
) -> list:
    """Embed *text* through the code-embed service's raw ``/api/embeddings``
    route, retried on the shared transient policy (:func:`retry_transient_embed`).

    v0.2.102 W4-G — the ONE home for the legacy raw-HTTP CodeEmbed leg that
    the migrate script and the code-graph query CLI fall back to when the
    EmbeddingService is unavailable (W4-E §7: the CLI's copy posted once and
    dropped the embed on the first 503 / refused connection). A non-200 is
    raised as ``RuntimeError("... HTTP <code> ...")`` so ``is_503_error``
    classifies it; the last transient error re-raises once the tolerance is
    spent.

    ``interactive=True`` (the query CLI, which the context-injection hooks
    run under a seconds-scale budget) waits on a 503 up to
    :func:`resolve_503_interactive_tolerance` and on a refused connection
    only up to :func:`resolve_refused_tolerance` (~10 s) — the same split the
    weaviate-MCP query path uses.
    """
    import requests

    def _post():
        resp = requests.post(
            f"{code_embed_url}/api/embeddings",
            json={"model": "", "prompt": text},
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"CodeEmbed /api/embeddings returned HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        return resp.json()["embedding"]

    if not interactive:
        return retry_transient_embed(_post, log=log)
    token = set_503_tolerance_override(resolve_503_interactive_tolerance())
    try:
        return retry_transient_embed(_post, log=log, interactive=True)
    finally:
        reset_503_tolerance_override(token)
