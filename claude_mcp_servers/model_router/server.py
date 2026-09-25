# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The aiohttp application: one Anthropic-shaped endpoint, two model families.

Routes (each also served with a trailing slash — see :func:`_add_route`)::

    GET  /health                    liveness; no auth, no blocking work
    GET  /usage                     per-chat token accounting, newest row per
                                    chat (``?session=<id>`` filters to one)
    GET  /usage/windows             subscription usage windows per vendor,
                                    from cache (``?format=line`` = one line of
                                    text; :mod:`model_router.usage_windows`)
    GET  /v1/models                 union catalog
    POST /v1/messages               proxied, streaming or not
    POST /v1/messages/count_tokens  proxied

Four failure modes were each paid for once during the field proving of this
design. Every one has a named test so it cannot come back.

1. **Match on the QUERY-STRIPPED path.** The client calls
   ``/v1/models?limit=…``. A route that compares the raw request target
   against ``"/v1/models"`` never matches, and the connection closes with no
   response at all — which the client reports as "issue with the selected
   model", with ZERO requests logged upstream. aiohttp routes on the path and
   ignores the query, which is the correct behaviour; the test
   ``test_models_served_with_query_string`` pins it so a future hand-rolled
   dispatcher cannot silently reintroduce it. Trailing slashes are registered
   explicitly for the same reason.
2. **REWRITE the model in the forwarded body.** Stripping the namespace in the
   router while forwarding the original bytes makes the upstream reject a
   model code it has never heard of. The body is re-encoded with the routed
   name; ``test_prefix_is_rewritten_in_forwarded_body`` reads the bytes the
   stub upstream actually received.
3. **Default ``anthropic-version``.** Claude Code always sends it; curl, a
   test and a minimal SDK do not, and both upstreams reject a request without
   it. Defaulting it turns a confusing relayed 400 into a working call.
4. **``/health`` does no blocking work.** No secret resolution, no subprocess,
   no upstream probe — a short-timeout health probe must not be able to break
   its own pipe. It reports cached state plus one ``stat`` of the credentials
   file.

Relay policy: upstream 4xx/5xx are relayed VERBATIM (status and body) so the
vendor's own error text reaches the user. Only four statuses are the
gateway's own: 401 (host token or Claude login), 400 (routing or an unreadable
body), 403 (non-loopback peer), 502/503 (upstream unreachable, or no vendor
key resolvable) — plus ONE documented exception, a vendor 402/429, which
becomes a 429 naming the vendor (:mod:`model_router.quota` has the incident
that bought it).

Bodies are otherwise not EDITED, with two exceptions, both in the same spirit
and both confined to a vendor route:

* the tool ids in the response are normalised (:mod:`model_router.tool_ids`),
  because a vendor's ``call_…`` id in a ``server_tool_use`` block kills every
  LATER Anthropic request in that session and the transcript is append-only,
  so relaying it faithfully is relaying a booby trap;
* a ``count_tokens`` answer of zero tokens for a conversation that plainly has
  some is replaced by the gateway's own estimate and LABELLED with where the
  number came from (:func:`model_router.usage.guard_count_tokens`). A zero
  there is worse than having no counter at all, because the client's own
  fallback would have been positive — the proxy invariant breaking in the one
  direction this daemon may not allow.

Neither edit is ever made to a first-party response: adding or changing a
field Anthropic did not send is itself "worse than native".

One body is READ without being edited: the ``usage`` block of every relayed
answer — see the usage policy below.

Body-size policy: **the gateway never refuses a request for its size.** A
proxy that answers what the upstream would have served is a failure the user
cannot route around, and this one was exactly that on 2026-09-09: aiohttp's
DEFAULT ``client_max_size`` is 1 MiB and this handler buffers the body (it
rewrites ids in it), so every ``POST /v1/messages`` of a conversation past
roughly 250K tokens of context — far less with images — came back 413 from the
gateway itself while the same conversation worked natively. Claude Code renders
ANY 413 as "Request too large (max 32MB). Accumulated images and attachments…",
so the daemon's own refusal read as the user's transcript being at fault.

Hence two decisions that must be read together. The application is built with
:data:`UNBOUNDED_CLIENT_MAX_SIZE`, so aiohttp imposes no ceiling at all; and
the id rewrite — which needs the whole body in memory — is bounded instead by
:data:`model_router.config.REWRITE_BUFFER_LIMIT_BYTES`. A body past that bound
is NOT parsed and NOT held: the buffered head is forwarded followed by the rest
of the client's stream, and whatever the upstream answers (200, or its own 413)
reaches the client verbatim. The ONE edit on that path is the model id literal,
spliced to the routed name because the ``claude-gw/`` namespace and the ``[1m]``
suffix are the gateway's own invention and no upstream knows them. The tool-id
repair is skipped; it is a repair, and the request is the point.

Logging policy: never a body (DEBUG-only for a quota refusal), never a
credential. ONE line per request in ONE shape —
``requested=… route=… forward=… status=… ms=… stream=…`` — where ``requested``
is what the CLIENT asked for. Every line a relayed request writes then carries
the turn's token counts in the same five fields,
``in=… cache_c=… cache_r=… out=… ctx=…``, with a ``-`` for anything the
response did not report: a zero and a silence are different observations and
the log is the place that has to keep telling them apart. That field is the one the field incident could
not answer: the gateway logged only the forwarded name, so a session that
silently changed models left no evidence of what had been selected.

"Every request" includes the ones refused at the door. A 401 used to return
before any line was written, which made an unauthenticated probe the single
terminal outcome the log was silent about — and it is the one a user asks
about ("the gateway is not answering me"). It is logged in the same shape,
rate-capped per peer (:data:`UNAUTHORISED_LOG_WINDOW_S`) because it is the
only line a caller WITHOUT the host token can cause.

Usage policy: every 2xx that REPORTS tokens is accounted, per chat, for EVERY
model — :mod:`model_router.usage` reads the ``usage`` block out of the bytes
already being relayed and writes one JSONL row. The chat is identified by the
``x-claude-code-session-id`` header, so two conversations and their subagents
separate without a body ever being opened for it. That accounting is for
CONTEXT, never for cost: nothing in this daemon prices a token.

Three consequences are deliberate. A response that reports NO usage (a 204, a
body in a shape this gateway does not read) gets its access line and no row —
a row of zeros in a context monitor reads as "this chat is empty", which is
the one wrong answer that costs the user something. ``count_tokens`` is never
accounted: it is a question about a conversation, not a turn in one. And a
non-2xx is logged only, because the tokens a refusal reports are not context.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping, NamedTuple, Optional

import aiohttp
from aiohttp import web

from . import __version__
from .auth import OAuthReader, host_token_stamp, token_matches
from .catalog import (
    CatalogEntry,
    CatalogService,
    resolve_window,
    to_models_response,
    with_usage_labels,
)
from .config import (
    PICKER_USAGE_ON,
    SERVICE_NAME,
    UPSTREAM_CONNECT_TIMEOUT_S,
    GatewayConfig,
)
from .context_table import ContextTableLoader
from .fileperms import OwnerOnlyState
from .quota import (
    CLIENT_QUOTA_STATUS,
    QUOTA_STATUSES,
    classify_quota,
    find_reset_hint,
    quota_error_body,
)
from .routing import Route, RouteError, route as route_model
from .secrets import VendorKeyResolver
from .source_identity import source_sha as _package_source_sha
from .usage_windows import (
    UsageWindows,
    label_suffixes,
    render_line,
    session_fetcher,
)
from .usage import (
    UsageAccumulator,
    UsageLedger,
    access_extra,
    build_record,
    count_tokens_estimate,
    guard_count_tokens,
    note_count_substitution,
    read_identity,
)
from .tool_ids import (
    COMPACT_JSON,
    JSON_BUFFER_LIMIT_BYTES,
    BoundedIdMap,
    RepairStats,
    SseIdRewriter,
    normalise_vendor_response,
    restore_vendor_ids,
    sanitise_for_anthropic,
)
from .vendors import (
    ANTHROPIC_FAMILY,
    DEFAULT_ANTHROPIC_VERSION,
    OAUTH_BETA,
    VENDORS,
    AnthropicFamily,
    Vendor,
    validate_registry,
    vendor_display_name,
)

logger = logging.getLogger(__name__)

#: Digest of the package source THIS process was started from, taken once at
#: import (``model_router.source_identity``). Reported on ``/health`` so the
#: host can PROVE whether a running gateway is behind its checkout after an
#: update rewrote the editable install underneath it
#: (``vco_lib.gateway_freshness``). Computed here, not per request: hashing the
#: files at request time would hash the NEW files and report a stale daemon as
#: current, which is the one answer this exists to never give.
SOURCE_SHA: Optional[str] = _package_source_sha(Path(__file__).resolve().parent)

#: Request headers never forwarded upstream. ``authorization`` / ``x-api-key``
#: are dropped because the client presents the LOCAL host token there, and
#: forwarding it upstream would leak it to the vendor. ``accept-encoding`` is
#: dropped so the client library's own negotiation governs, keeping the relayed
#: bytes and the relayed headers consistent. ``content-encoding`` is dropped
#: for the mirror-image reason on the REQUEST: aiohttp has already
#: decompressed the body by the time it reaches the handler, so forwarding
#: the header would tell the upstream to gunzip plain JSON — a 400 that only
#: happens through the gateway.
_HOP_BY_HOP_REQUEST = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "authorization",
        "x-api-key",
        "accept-encoding",
        "content-encoding",
    }
)

#: Response headers never relayed back. ``content-encoding``/``content-length``
#: are dropped because the client library decompresses for us, so the bytes we
#: write no longer match those headers.
_HOP_BY_HOP_RESPONSE = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "content-encoding",
    }
)

_STREAM_CHUNK_HINT = "event-stream"

#: The beta header that actually buys the 1M context window. The ``[1m]``
#: suffix on a model id is a CLIENT-side spelling (see
#: :func:`model_router.routing.route`); ``api.anthropic.com`` 404s on it and
#: reads the window off this header instead. Added only when the requested id
#: carried the suffix and the client did not send the header itself.
CONTEXT_1M_BETA = "context-1m-2025-08-07"

#: Passed to aiohttp as the application's ``client_max_size``. Zero is
#: aiohttp's "no limit" (``web_request.BaseRequest.read`` guards the size
#: check with ``if self._client_max_size:``), and no limit is the correct
#: value here: the gateway must never be the party that refuses a request for
#: its size — see the body-size policy in this module's docstring. What the
#: gateway BUFFERS is bounded separately, by
#: :data:`model_router.config.REWRITE_BUFFER_LIMIT_BYTES`, and overrunning
#: that bound streams the request instead of refusing it.
#:
#: ``tests/test_v0294_gateway_body_limit.py`` pins the BEHAVIOUR, not just the
#: value: a body well past aiohttp's 1 MiB default is served end to end, so an
#: aiohttp release that gave 0 some other meaning fails there.
UNBOUNDED_CLIENT_MAX_SIZE = 0

#: The access-line field that says a request was served WITHOUT the id
#: rewrite because it outgrew the buffer. One spelling, in one place: it is
#: what an operator greps for after "why did this call carry a namespaced
#: model id upstream?".
REWRITE_BUFFER_NOTE = "note=body_over_rewrite_buffer"

#: What :func:`_guarded` returns when a rewrite raised, so the caller can
#: tell "the pass produced nothing" from "the pass is broken". A unique
#: object, not ``None``: empty ``bytes`` is an ordinary result from
#: :meth:`SseIdRewriter.feed` (it holds a partial event back), and conflating
#: the two would abandon the rewrite on every buffered chunk.
_ABANDON = object()

#: How much of an over-buffer body is walked for the routing fields. The
#: top-level ``model`` sits in the flat head of every request a client
#: actually sends, so this is generous; it is bounded at all because the walk
#: is a Python-level scan and the buffer it runs on can be tens of MiB.
HEAD_SCAN_BYTES = 64 * 1024

#: How much of a vendor's quota-refusal body is read before it is replaced.
#: Bounded because the body is never relayed — it exists here only to find a
#: reset hint and to be logged at DEBUG, and an unbounded read of a body we
#: are going to discard is a denial-of-service surface for free.
_QUOTA_BODY_PEEK_BYTES = 64 * 1024

#: How much of a vendor 401/403 body is HELD while classifying it (the body
#: IS relayed — :func:`_auth_rejection_response` — so an overflow is not
#: discarded but streamed verbatim past the bound; the limit bounds memory,
#: not the relay).
_AUTH_BODY_LIMIT_BYTES = 64 * 1024


#: What a GATEWAY-side 502 carries. Anthropic's SDKs read ``x-should-retry``
#: to decide whether a status is worth another attempt, and every 502 this
#: daemon writes is transient by construction — the upstream refused the
#: connection, or went quiet. Native sees a connection error there and
#: retries; without this header the same condition through the gateway is a
#: hard failure, which is the invariant ("never worse than native") breaking
#: on the most ordinary flake there is.
RETRYABLE_HEADERS = {"x-should-retry": "true"}

#: WP-6 review MAJOR-1/MINOR-1 caps. Auth strikes: one 401 is a blip,
#: three consecutive is a dead key (rotated/revoked) that must not keep
#: serving from the last-good store. Echo-log cap: ``forwarded`` is
#: client-controlled, so the triple edge-set is bounded like ``_id_maps``.
VENDOR_AUTH_STRIKE_LIMIT = 3
_ECHO_LOG_CAP = 256


def _error_body(kind: str, message: str) -> dict:
    """Anthropic-shaped error envelope, so the client renders our text."""
    return {"type": "error", "error": {"type": kind, "message": message}}


def _json_error(
    status: int,
    kind: str,
    message: str,
    headers: Optional[Mapping[str, str]] = None,
) -> web.Response:
    return web.json_response(
        _error_body(kind, message),
        status=status,
        headers=dict(headers) if headers else None,
    )


def _guarded(fn: Callable[..., Any], *args: Any, what: str) -> Any:
    """Run a REWRITE; on ANY exception log once and return :data:`_ABANDON`.

    Every rewrite in this module is optional by nature — it repairs somebody
    else's bytes so that a later request does not break. Unguarded, a bug in
    one leaves the handler with an unhandled exception, which aiohttp turns
    into a 500 ``text/plain``; Anthropic's SDK retries a 500 up to ten times,
    so one defect becomes ten vendor-billed requests and the user is told
    nothing useful. Native, against the same upstream, would simply have
    received the upstream's own answer.

    The caller therefore always has an answer to "what would I have had if
    this pass had never existed?", and gives it to the request.
    ``Exception`` deliberately, not a named list: the point is that NO defect
    in a repair may take a request down, and a repair cannot know in advance
    which bug it will have.
    """
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 — see the docstring: this is the point
        logger.exception(
            "model-gateway: %s failed; continuing without it", what,
        )
        return _ABANDON


def _peer_host(request: web.Request) -> Optional[str]:
    """The peer's address as a string, or ``None`` when it cannot be read."""
    transport = request.transport
    if transport is None:
        return None
    peer = transport.get_extra_info("peername")
    if not peer:
        return None
    host = peer[0] if isinstance(peer, (tuple, list)) else peer
    return host if isinstance(host, str) else None


def _peer_is_loopback(request: web.Request) -> Optional[bool]:
    """True / False / None when the peer address cannot be determined."""
    host = _peer_host(request)
    if host is None:
        return None
    # An IPv4-mapped IPv6 peer arrives as "::ffff:127.0.0.1".
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return bool(address.is_loopback)


@web.middleware
async def loopback_only_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Any],
) -> web.StreamResponse:
    """Refuse any request whose peer is not, provably, loopback.

    The gateway binds loopback, so this is belt-and-braces — but the host
    token authorises proxying under the user's Claude login and paid vendor
    subscription, and a bind misconfiguration must not be the only thing
    standing between that and the network. Undeterminable peer is REFUSED,
    not allowed: a security gate that cannot confirm its precondition denies.
    """
    verdict = _peer_is_loopback(request)
    if verdict is True:
        return await handler(request)
    reason = (
        "the peer address is not a loopback address"
        if verdict is False
        else "the peer address could not be determined"
    )
    logger.warning("model-gateway: refused a request — %s", reason)
    return _json_error(
        403,
        "permission_error",
        f"the model gateway serves loopback clients only ({reason}).",
    )


class Gateway:
    """Everything the handlers need, built once at startup."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        vendors: Mapping[str, Vendor] = VENDORS,
        anthropic: AnthropicFamily = ANTHROPIC_FAMILY,
        oauth_reader: Optional[OAuthReader] = None,
        key_resolver: Optional[VendorKeyResolver] = None,
        token_permissions: OwnerOnlyState = "unknown",
        token_file_stamp: Optional[Mapping[str, object]] = None,
    ) -> None:
        validate_registry(vendors)
        self.config = config
        self.vendors = vendors
        self.anthropic = anthropic
        self.oauth = oauth_reader or OAuthReader(config.credentials_file)
        self.keys = key_resolver or VendorKeyResolver(
            project=config.secret_project,
            ttl_s=config.key_ttl_s,
            # The DAEMON wants its secret scope diagnosed when a key comes
            # back empty (R5b); an injected resolver keeps the quiet default,
            # so nothing a test or an embedder builds reaches the hub on its
            # own. See `VendorKeyResolver.probe_scope`.
            probe_scope_on_miss=True,
            # Issue 12: keep answering with the last-known-good key while a
            # failed resolution is within this bound — the 2026-09-20 update
            # stopped the hub (by design) and nine 503s followed for a key
            # that was fine. An injected resolver keeps its own default.
            serve_stale_max_age_s=config.key_stale_max_age_s,
        )
        self.context = ContextTableLoader(config.context_table_file)
        self.token_permissions = token_permissions
        #: The token file stamp sampled by the DAEMON at startup (issue 10).
        #: ``create_app`` stays pure — binds nothing, stats nothing — so the
        #: caller that has already read the token takes this one extra
        #: observation, exactly like ``token_permissions``.
        self._token_file_stamp: Optional[Mapping[str, object]] = token_file_stamp
        self._session: Optional[aiohttp.ClientSession] = None
        #: Per-vendor rewritten-id -> vendor-id maps. Per PROCESS and bounded:
        #: the client echoes an id back one turn later, so the map only has to
        #: outlive a request, and an unbounded one would grow with uptime.
        self._id_maps: dict[str, BoundedIdMap] = {}
        #: peer -> (when its last 401 line was written, how many since).
        self._unauthorised_seen: dict[str, tuple[float, int]] = {}
        #: Issue 9: vendor responses whose reported ``model`` was not the id
        #: this daemon forwarded. Bounded WARN edge-state beside it.
        self._model_echo_mismatches = 0
        # WP-6 review MINOR-1: ``forwarded`` is client-controlled, so the
        # triple set is unbounded across uptime. Dict-as-ordered-set with a
        # FIFO cap mirrors the ``_id_maps`` bounding rationale one field up.
        self._echo_logged: dict[tuple[str, str, str], None] = {}
        # WP-6 review MAJOR-1: consecutive per-vendor auth rejections. At
        # VENDOR_AUTH_STRIKE_LIMIT the last-good key is invalidated —
        # serve-stale must not outlive a key the vendor itself rejects. A
        # single 401 does NOT invalidate (an auth blip would become an
        # outage); VENDOR_AUTH_STRIKE_LIMIT consecutive do, and any 2xx
        # resets the count.
        self._vendor_auth_strikes: dict[str, int] = {}
        #: Per-chat token accounting. Built here rather than in ``create_app``
        #: so a handler reaches it the same way it reaches every other piece
        #: of shared state, and so a test can swap it on the gateway object.
        #: It resolves its own file path lazily and never on the request path.
        self.usage = UsageLedger()
        self.catalog = CatalogService(
            vendors=vendors,
            anthropic=anthropic,
            fetch_json=self._fetch_json,
            oauth_token=lambda: self.oauth.read().token,
            vendor_key=self._catalog_vendor_key,
            live_ttl_s=config.catalog_ttl_s,
            static_ttl_s=config.static_retry_ttl_s,
        )
        #: Subscription usage windows (``/usage/windows``). Its secrets come
        #: through THIS gateway's reader and resolver — no second resolver —
        #: and its fetches run in a background task a read schedules, never
        #: on a request path. See :mod:`model_router.usage_windows`.
        self.usage_windows = UsageWindows(
            anthropic_upstream=anthropic.upstream,
            vendors=vendors,
            oauth_token=lambda: self.oauth.read().token,
            vendor_key=self._catalog_vendor_key,
            fetch=session_fetcher(lambda: self.session),
            ledger_path=lambda: self.usage.path,
        )

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        # Queued ledger appends first: they are scheduled on the loop's
        # executor and a shutdown that tore the loop down under them would
        # drop the last rows of the session — the ones a user looking at a
        # context monitor cares most about. Bounded by the number of requests
        # still in flight, which at shutdown is none or nearly none.
        await self.usage.drain()
        # Before the session closes: an in-flight usage refresh holds it.
        await self.usage_windows.stop()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise RuntimeError(
                "model-gateway: HTTP session used before start(); the app "
                "factory registers start() as an on_startup hook",
            )
        return self._session

    # ── helpers used by the catalog ──────────────────────────────────────
    async def _fetch_json(
        self, url: str, headers: Mapping[str, str],
    ) -> Optional[dict]:
        try:
            async with self.session.get(
                url,
                headers=dict(headers),
                timeout=aiohttp.ClientTimeout(total=self.config.catalog_timeout_s),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "model-gateway: catalog fetch %s returned HTTP %s",
                        url, resp.status,
                    )
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("model-gateway: catalog fetch %s failed: %s", url, exc)
            return None

    async def _catalog_vendor_key(self, vendor: Vendor) -> Optional[str]:
        result = await self.keys.aresolve(vendor)
        return result.key

    # ── issue 9: model echo assertion ────────────────────────────────────
    def note_model_echo_mismatch(
        self, vendor_id: str, forwarded: str, reported: str,
    ) -> None:
        """Count one vendor answer whose reported ``model`` is not the id
        this daemon forwarded (issue 9).

        The shape is the alias trap (see the zai row in
        :mod:`model_router.vendors`): a vendor that maps a requested name
        onto a different model server-side still answers 200, so routing
        alone cannot see it — only comparing the echoed id can. The
        comparison is made in the terminal ``access`` closure of
        :func:`_proxy`, so it covers both the streamed and the buffered
        vendor shapes, and it deliberately does NOT fire when the response
        carries no ``model`` at all: absence is silence, not a mismatch
        (same discipline as usage).

        The COUNTER increments on every occurrence — a rate over time is
        the whole point — while the WARN is EDGE-TRIGGERED per
        ``(vendor, forwarded, reported)``: a vendor that aliases does so on
        every request, and a per-request WARN is a storm wearing a warning
        label. The mismatch is never repaired; the answer already went out.
        """
        self._model_echo_mismatches += 1
        triple = (vendor_id, forwarded, reported)
        if triple in self._echo_logged:
            return
        if len(self._echo_logged) >= _ECHO_LOG_CAP:
            self._echo_logged.pop(next(iter(self._echo_logged)), None)
        self._echo_logged[triple] = None
        logger.warning(
            "model-gateway: vendor %s was forwarded %s and its response "
            "reports model %s — the alias-trap shape (see the zai row in "
            "model_router.vendors). The answer was already relayed; "
            "counting it as model_echo_mismatches in /health.",
            vendor_id,
            _log_safe(forwarded),
            _log_safe(reported),
        )

    @property
    def model_echo_mismatches(self) -> int:
        """How many vendor answers echoed a model this daemon did not send.

        Zero on a healthy daemon; surfaced in ``/health`` beside the ledger
        counters.
        """
        return self._model_echo_mismatches

    # ── WP-6 review MAJOR-1: vendor auth rejections vs the last-good key ──
    def note_vendor_auth_rejection(self, vendor_id: str) -> None:
        """Count one KEY-level vendor auth rejection; at the strike limit,
        invalidate the last-good key.

        Serve-stale exists for the hub-down window (issue 12), where the
        key is FINE and resolution is what failed. The mirror-image case
        is a key the VENDOR is rejecting while resolution also fails
        (rotated or revoked): without this, the stale key serves until the
        6 h bound — ``invalidate()`` was a credited mechanism with no
        production caller. One 401 does not invalidate (an auth blip would
        become a hard outage); ``VENDOR_AUTH_STRIKE_LIMIT`` consecutive
        do, with one WARN per invalidation. After invalidation the strikes
        reset, so a persistently-rejected key re-fires every
        ``VENDOR_AUTH_STRIKE_LIMIT`` requests — visible, not silent.

        The caller classifies first: only KEY-level rejections
        (:func:`_auth_rejection_is_key_level`) reach this method. A 403
        whose body says the MODEL is access-denied is not evidence about
        the key and must not feed this counter — three requests for
        deprecated-but-listed models would otherwise invalidate a healthy
        key.
        """
        n = self._vendor_auth_strikes.get(vendor_id, 0) + 1
        if n < VENDOR_AUTH_STRIKE_LIMIT:
            self._vendor_auth_strikes[vendor_id] = n
            return
        self.keys.invalidate(vendor_id)
        self._vendor_auth_strikes.pop(vendor_id, None)
        logger.warning(
            "model-gateway: vendor %s rejected the key %d consecutive "
            "times — last-good store invalidated (serve-stale cannot "
            "outlive a key the vendor rejects)",
            _log_safe(vendor_id), n,
        )

    def note_vendor_success(self, vendor_id: str) -> None:
        """A 2xx from the vendor clears its auth-strike count (a blip
        never accumulates across an interleaved success)."""
        self._vendor_auth_strikes.pop(vendor_id, None)

    # ── issue 10: host-token file diagnostic ─────────────────────────────
    def host_token_file_report(self) -> Optional[dict]:
        """The token file stamp THIS process loaded, beside the current one.

        Decides among the three 401 shapes of issue 10 without restarting
        anything:

        * ``path`` differs from the file the failing client read → the
          starter and the client resolved different ``VCT_STATE_DIR``s
          (shape 1); reconcile the env, restart.
        * same ``path``, loaded stamp differs from ``current_mtime_ns``/
          ``current_size`` → the file was deleted and regenerated AFTER this
          daemon started (shape 2; the single-instance guard never fires for
          it because the port never left). Remedy: restart the gateway —
          honest message, deliberately no auto-heal.
        * same stamp → the client presented wrong content (shape 3).

        ``None`` for an app built without a sampled stamp (every test, any
        embedder): the diagnostic is the daemon's to give.
        """
        loaded = self._token_file_stamp
        if not loaded:
            return None
        report: dict[str, object] = dict(loaded)
        current = host_token_stamp(Path(str(loaded.get("path", ""))))
        report["current_mtime_ns"] = current["mtime_ns"] if current else None
        report["current_size"] = current["size"] if current else None
        return report

    # ── request plumbing ─────────────────────────────────────────────────
    def authorised(self, request: web.Request) -> bool:
        presented = request.headers.get("Authorization", "")
        if presented.startswith("Bearer "):
            presented = presented[len("Bearer "):].strip()
        if token_matches(presented, self.config.token):
            return True
        return token_matches(
            request.headers.get("x-api-key", "").strip(), self.config.token,
        )

    @staticmethod
    def forward_headers(request: web.Request) -> dict[str, str]:
        return {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in _HOP_BY_HOP_REQUEST
        }

    def note_unauthorised(self, peer: str, now: float) -> Optional[int]:
        """Rate gate for the 401 access line. See :data:`UNAUTHORISED_LOG_WINDOW_S`.

        Returns ``None`` to stay silent, or the number of refusals suppressed
        since this peer's last logged one (0 when none) — so the caller can
        say so on the line it does write.

        Per GATEWAY, not per process: two apps in one interpreter (the test
        suite) must not share a window, and the state is keyed by peer, which
        on a loopback-only daemon is a set of at most two.
        """
        last, suppressed = self._unauthorised_seen.get(peer, (0.0, 0))
        if last and now - last < UNAUTHORISED_LOG_WINDOW_S:
            self._unauthorised_seen[peer] = (last, suppressed + 1)
            return None
        self._unauthorised_seen[peer] = (now, 0)
        return suppressed

    def id_map(self, vendor: Vendor) -> BoundedIdMap:
        """The rewritten-id -> vendor-id map for ``vendor``, created on demand."""
        existing = self._id_maps.get(vendor.vendor_id)
        if existing is None:
            existing = BoundedIdMap()
            self._id_maps[vendor.vendor_id] = existing
        return existing

    @staticmethod
    def relay_headers(upstream: aiohttp.ClientResponse) -> dict[str, str]:
        return {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in _HOP_BY_HOP_RESPONSE
        }


#: Typed application key (aiohttp warns on bare-string keys since 3.9).
APP_KEY: web.AppKey[Gateway] = web.AppKey("vct_model_gateway", Gateway)

#: Handle on the one-shot secret-scope probe the DAEMON schedules at startup
#: (``model_router.__main__._probe_secret_scope_at_startup``). It lives here,
#: beside :data:`APP_KEY`, for two reasons: every app key this package uses is
#: typed and declared in ONE home — a bare string raises ``NotAppKeyWarning``
#: and the gateway's gate set runs warnings as errors — and ``__main__``
#: deliberately imports nothing heavier than the stdlib at module scope, so it
#: cannot name a ``web.AppKey`` of its own without breaking ``--version`` on a
#: half-installed machine. A reference must be held at all: an un-referenced
#: task can be garbage-collected mid-run and the probe would silently never
#: happen.
SCOPE_PROBE_TASK_KEY: "web.AppKey[asyncio.Task]" = web.AppKey(
    "vct_secret_scope_probe", asyncio.Task,
)


def _add_route(
    app: web.Application,
    method: str,
    path: str,
    handler: Callable[[web.Request], Any],
    *,
    name: str,
) -> None:
    """Register ``path`` AND ``path + '/'``.

    aiohttp already ignores the query string when matching, which is what
    keeps ``/v1/models?limit=20`` working; the trailing-slash variant is not
    automatic, and a client that sends one would otherwise get a 404 that
    looks like "the gateway does not implement models".
    """
    app.router.add_route(method, path, handler, name=name)
    app.router.add_route(method, path + "/", handler, name=f"{name}_slash")


def _expires_in_s(expires_at_ms: int) -> Optional[int]:
    """Seconds until an epoch-millisecond expiry, or ``None`` when unstated.

    Signed: a login that expired ten minutes ago reports ``-600``, which is
    information, where a floor at zero would make "just expired" and "expired
    yesterday" the same reading. ``None`` means the credentials file states no
    expiry at all — not "expires now".
    """
    if not expires_at_ms:
        return None
    return int(expires_at_ms / 1000 - time.time())


async def health_handler(request: web.Request) -> web.Response:
    """Liveness. Cached state plus one ``stat``; never blocks.

    Emitted fields — this list and the code below are kept in step by
    ``test_health_reports_exactly_the_documented_fields``:

    ``ok`` (bool), ``service``, ``version``,
    ``source_sha`` (sha256 of the package source this process was started
    from, hashed once at import by ``model_router.source_identity``; ``null``
    when it could not be read, which callers treat as unknown. Its PRESENCE is
    itself evidence: every daemon since it was added emits the key, so a
    ``/health`` without it is positively an older daemon),
    ``port``, ``host``,
    ``catalog_source`` (family -> ``live``/``static``/``declared``/
    ``unfetched``/``unavailable``),
    ``catalog_filter`` (``latest``/``all`` — which versions of a family reach
    the picker), ``window_rows`` (``one_m_only``/``both`` — whether a 1M
    first-party model shows its plain row beside its ``[1m]`` one),
    ``picker_usage`` (``on``/``off`` — whether vendor rows in ``/v1/models``
    carry their subscription's usage in the label; the mode IN FORCE, so an
    unrecognised knob value that fell back to the default shows here),
    ``catalog_hidden`` (int, how many rows the last catalog build
    withheld under that filter; ``0`` before the picker has ever opened, which
    reads the same as "nothing hidden" and correctly so),
    ``context_table_source``, ``context_table_path``, ``oauth_present``
    (bool), ``oauth_state`` (``present``/``expired``/``absent``/
    ``unreadable``), ``oauth_expires_in_s`` (int seconds, negative once past,
    ``null`` when the file states no expiry), ``vendors``,
    ``vendor_keys_cached``,
    ``vendor_keys_stale`` (vendor ids currently answered from the
    last-known-good store — issue 12's serve-stale state; empty on a healthy
    daemon, non-empty exactly while resolution fails for a vendor whose key
    resolved recently enough to keep serving),
    ``secret_scope`` (``project`` — the scope vendor keys resolve in, which is
    the pin when there is one and this process's working directory when there
    is not, because that is what the resolver itself falls back to;
    ``resolvable`` — ``true`` when that scope maps to a hub project id,
    ``false`` when it does not, ``null`` when nothing has probed it yet, which
    is a different claim and is reported as one; ``reason`` — why, in key names
    and paths only. Read from CACHE: the verdict is produced at startup and
    refreshed on a key miss, never by this route, so a scope probe can never
    make a liveness check block. It exists because ``vendors`` beside an empty
    ``vendor_keys_cached`` reads like "no key configured yet", while the actual
    2026-09-10 state was "this daemon's scope cannot see any key you
    configure"),
    ``model_echo_mismatches`` (int, how many vendor answers reported a
    ``model`` that is not the id this daemon forwarded — issue 9's
    alias-trap counter. Zero on a healthy daemon; each distinct
    ``(vendor, forwarded, reported)`` also WARNs once),
    ``token_file_permissions`` (``owner_only``/``broader``/``unknown``,
    sampled at startup — probing it here would shell out on Windows),
    ``host_token_file`` (the token file THIS process loaded — ``path``,
    ``mtime_ns``, ``size`` sampled at startup, beside ``current_mtime_ns`` /
    ``current_size`` from one fresh ``stat``. The pair distinguishes the three
    401 shapes of issue 10: a ``path`` the failing client does not recognise
    means starter and client resolved different ``VCT_STATE_DIR``s; the same
    path with a different stamp means the file was deleted and regenerated
    after this daemon started (restart to fix); the same stamp means the client
    presented wrong content. ``null`` when no stamp was sampled at startup),
    ``usage_ledger`` (``path`` — where per-chat token rows land, ``null`` on
    an install where the metrics home could not be resolved; ``rows_written``,
    how many rows THIS process has appended; ``last_write_ts``, the ``ts`` of
    the newest one, ``null`` before the first. Two in-memory counters and a
    path string that is resolved once and remembered: no file is opened, no
    directory is created and nothing is probed, which is what lets a liveness
    probe carry it).
    """
    gateway: Gateway = request.app[APP_KEY]
    oauth = gateway.oauth.read()
    table = gateway.context.current()
    return web.json_response(
        {
            "ok": True,
            # From config, not a literal: the startup probe compares against
            # the same constant to tell this daemon from any other listener.
            "service": SERVICE_NAME,
            "version": __version__,
            # Taken at import, never recomputed: see SOURCE_SHA.
            "source_sha": SOURCE_SHA,
            "port": gateway.config.port,
            "host": gateway.config.host,
            "catalog_source": gateway.catalog.sources(),
            # Both read from CACHED state — neither builds a catalog. A
            # liveness probe that could block on two upstream fetches is the
            # thing /health exists not to be.
            "catalog_filter": gateway.config.catalog_filter,
            "window_rows": gateway.config.window_rows,
            "picker_usage": gateway.config.picker_usage,
            "catalog_hidden": gateway.catalog.hidden_count(),
            "context_table_source": table.source,
            "context_table_path": str(table.path) if table.path else None,
            "oauth_present": oauth.present,
            "oauth_state": oauth.state,
            # The number the launcher card needs to warn BEFORE the gateway
            # goes dark. A panel pointed here presents a host token, not the
            # Claude login, so nothing in this process refreshes that login —
            # only a native client does. Reporting "present" up to the second
            # it expires is therefore true and useless; the countdown is what
            # lets the GUI say "re-login within 25 minutes" while there is
            # still time to act.
            "oauth_expires_in_s": _expires_in_s(oauth.expires_at_ms),
            "vendors": sorted(gateway.vendors.keys()),
            "vendor_keys_cached": list(gateway.keys.cached_vendor_ids()),
            # Issue 12: the vendors currently served from the last-known-good
            # store. One cached set read — no store is touched, and the
            # negative-cache / serve-stale state stays visible through a hub
            # blip instead of reading as "no key".
            "vendor_keys_stale": list(gateway.keys.serving_stale_ids()),
            # Cached verdict only — `scope_status` touches no store. The two
            # fields above say WHICH vendors exist and which have a live key;
            # this one says whether a key could be found at all.
            "secret_scope": gateway.keys.scope_status().to_dict(),
            # In-memory counter — the alias-tripwire of issue 9.
            "model_echo_mismatches": gateway.model_echo_mismatches,
            "token_file_permissions": gateway.token_permissions,
            # One fresh `stat` beside the startup-sampled stamp: the pair is
            # the issue-10 diagnostic (loaded vs current), and a stat is the
            # only filesystem work /health does besides the credentials one.
            "host_token_file": gateway.host_token_file_report(),
            # Cached counters and a resolved path — no directory is created
            # and no file is opened, so /health's "never blocks" holds.
            "usage_ledger": gateway.usage.health(),
        }
    )


async def usage_handler(request: web.Request) -> web.Response:
    """Per-chat token accounting: the newest row for every chat seen.

    ``{"sessions": {<session-id>: <row>}, "ledger_path": …, "rows_written": N}``
    where a row is one :class:`model_router.usage.UsageRecord`.
    ``?session=<id>`` narrows it to one chat, which is what a monitor watching
    a single conversation should ask for rather than fetching every chat and
    discarding all but one.

    Host-token authorised like ``/v1/models`` and ``/v1/messages``, and behind
    the same loopback middleware: the rows name model ids, chat ids and token
    counts for every conversation on this machine. ``/health`` is the only
    unauthenticated route and it deliberately carries counters, not rows.

    Answers from MEMORY. The ledger's map is updated synchronously as each
    request finishes while the file append is scheduled off the request path,
    so a poll immediately after a turn sees that turn — the ``rows_written``
    counter beside it is the one that lags, and it says what it means: rows
    that have reached the FILE.

    A chat that sent no ``x-claude-code-session-id`` header is absent here and
    present in the file: its tokens are real, but "no chat" is not a key, and
    bucketing every such request under one synthetic id would merge unrelated
    callers into a conversation that never happened.
    """
    gateway: Gateway = request.app[APP_KEY]
    started = time.monotonic()
    if not gateway.authorised(request):
        _log_unauthorised(gateway, request, started=started)
        return _unauthorised()
    only = request.query.get("session") or None
    ledger = gateway.usage
    path = ledger.path
    return web.json_response(
        {
            "sessions": ledger.sessions(only),
            "ledger_path": str(path) if path is not None else None,
            "rows_written": ledger.rows_written,
        }
    )


async def usage_windows_handler(request: web.Request) -> web.Response:
    """Subscription usage windows per vendor, from the gateway's cache.

    JSON by default (:meth:`model_router.usage_windows.UsageWindows.snapshot`);
    ``?format=line`` answers ``text/plain`` with the ONE rendering the
    status-line scripts print verbatim (empty when nothing is known yet).

    Host-token authorised and loopback-only like ``/usage``: it names the
    user's subscription plan and consumption. Answers from MEMORY: a read that
    finds the cache due schedules one background refresh and returns what is
    cached now, so this route can never wait on a vendor.
    """
    gateway: Gateway = request.app[APP_KEY]
    started = time.monotonic()
    if not gateway.authorised(request):
        _log_unauthorised(gateway, request, started=started)
        return _unauthorised()
    windows = gateway.usage_windows
    windows.request_refresh()
    snapshot = windows.snapshot()
    if request.query.get("format") == "line":
        return web.Response(text=render_line(snapshot), content_type="text/plain")
    return web.json_response(snapshot)


async def models_handler(request: web.Request) -> web.Response:
    """The picker catalog; vendor labels carry subscription usage when known.

    The usage text (``picker_usage``, on by default) comes from the
    :class:`model_router.usage_windows.UsageWindows` CACHE and nothing else:
    this route may schedule a background refresh but never awaits one, so a
    stalled vendor quota endpoint cannot delay the picker. A cold or stale
    cache answers without the text. The refresh is requested BEFORE the
    catalog is built, so on a daemon whose catalog needs fetching the usage
    reading gets that long to land and the answer may already carry it.
    Claude Code fetches this route once per session start, so the label is a
    snapshot of that moment (see :func:`model_router.usage_windows.label_suffix`).
    """
    gateway: Gateway = request.app[APP_KEY]
    started = time.monotonic()
    if not gateway.authorised(request):
        # The SAME line as the messages route's refusal: leaving this one
        # silent would move the gap one endpoint along rather than close it,
        # and the picker's catalog call is the request a misconfigured
        # client makes FIRST.
        _log_unauthorised(gateway, request, started=started)
        return _unauthorised()
    usage_labels = gateway.config.picker_usage == PICKER_USAGE_ON
    if usage_labels:
        gateway.usage_windows.request_refresh()
    table = gateway.context.current()
    catalog = await gateway.catalog.union(
        table=table,
        catalog_filter=gateway.config.catalog_filter,
        window_rows=gateway.config.window_rows,
    )
    entries = catalog.entries
    if usage_labels:
        entries = with_usage_labels(
            entries, label_suffixes(gateway.usage_windows.snapshot()),
        )
    logger.info(
        "model-gateway: /v1/models -> %d entries (%s)%s",
        len(catalog.entries),
        ", ".join(f"{k}={v}" for k, v in sorted(catalog.sources.items())),
        # Named in the SAME line as the counts, because "the picker is short"
        # and "the gateway hid some rows" are the same observation and
        # reading them from two places is how they get blamed on each other.
        (
            f", {len(catalog.hidden)} hidden by "
            f"catalog={gateway.config.catalog_filter}"
            f"/rows={gateway.config.window_rows}"
            if catalog.hidden else ""
        ),
    )
    return web.json_response(
        to_models_response(entries, catalog.sources, catalog.hidden),
    )


#: The one route that is a QUESTION about a conversation rather than a turn
#: in it. Named once, because two things key off it — the usage ledger skips
#: it, and the vendor zero-guard fires only on it — and a second spelling is
#: how those two would drift apart.
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"


def _canonical_path(request: web.Request) -> str:
    """The request path without the trailing slash a lenient client may send."""
    return request.path.rstrip("/") or request.path


@dataclass(frozen=True)
class RequestFacts:
    """What the handler learned that the RELAY needs after the fact.

    Assembled once in :func:`messages_handler` and carried into
    :func:`_proxy`, because both things in here are read out of the REQUEST
    (its headers, its body) and acted on when the RESPONSE ends — and a relay
    that had to re-derive them would be re-reading a body it has already
    forwarded.

    Frozen: these are observations of one request, and a mutable carrier is
    how the second request in a keep-alive connection ends up labelled with
    the first one's chat.
    """

    #: ``x-claude-code-session-id`` — the chat. ``None`` when the client sent
    #: none (curl, an SDK, a probe), which is recorded rather than invented.
    session: Optional[str]
    #: ``x-claude-code-agent-id`` — present only on a SUBAGENT's requests.
    agent: Optional[str]
    #: ``x-claude-code-parent-agent-id`` — present on a nested agent's.
    parent_agent: Optional[str]
    #: This request is :data:`COUNT_TOKENS_PATH`.
    count_tokens: bool
    #: The gateway's own bytes/4 floor over the client's ``messages`` +
    #: ``system``, for the vendor zero-guard. ``None`` when there is nothing
    #: to count, or when the body was never parsed (the over-buffer path).
    count_estimate: Optional[int]


def _route_label(decision: Route) -> str:
    """``anthropic`` or ``vendor:<id>`` — the field the access log turns on."""
    return "anthropic" if decision.is_anthropic else f"vendor:{decision.family_id}"


def _actual_window(
    gateway: "Gateway", requested: str,
) -> "tuple[Optional[int], str]":
    """The REAL window of ``requested``, and which step of the resolver said so.

    Routed through :func:`model_router.catalog.resolve_window` rather than
    re-reading the table here, so a tombstone, a damaged row and the
    positive-integer rule all mean the same thing on this path as they do in
    the picker.

    The entry is SYNTHETIC — id only — which confines the answer to the
    resolver's first two steps: the tombstone check and the table. That is
    deliberate. The upstream and family-floor steps need a built catalog, and
    building one can fetch two upstreams; a request path that could do that
    would put a vendor's outage inside every user turn. A model the table does
    not name therefore reads ``unknown`` and the row's ``pct_actual`` is
    ``null``, which is the honest answer rather than a guessed one.
    """
    table = gateway.context.current()
    entry = CatalogEntry(id=requested, display_name=requested)
    resolution = resolve_window(entry, table=table, family_floor=None)
    return resolution.window, resolution.source


def _submit_usage(
    gateway: "Gateway",
    decision: Route,
    facts: Optional[RequestFacts],
    *,
    requested: str,
    route: str,
    status: int,
    stream: bool,
    totals: Mapping[str, int],
    usage_complete: bool,
) -> None:
    """Write one ledger row for a finished request, or decline to.

    Declines in three cases, each for its own reason (the module docstring's
    usage policy states them together): a ``count_tokens`` call, which is a
    question about a conversation and not a turn in one; a non-2xx, whose
    numbers are not context; and a 2xx that reported no usage at all, because
    a row of zeros in a context monitor reads as "this chat is empty".
    """
    if facts is None or facts.count_tokens:
        return
    if not 200 <= status < 300:
        return
    window_actual, window_source = _actual_window(gateway, requested)
    record = build_record(
        session=facts.session,
        agent=facts.agent,
        parent_agent=facts.parent_agent,
        requested=requested,
        route=route,
        forward=decision.forward_model,
        stream=stream,
        status=status,
        totals=totals,
        usage_complete=usage_complete,
        window_actual=window_actual,
        window_source=window_source,
    )
    try:
        loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover — handlers always have a loop
        loop = None
    gateway.usage.submit(record, loop=loop)


def _log_safe(value: object) -> str:
    """``value`` rendered so it can never be more than part of ONE log line.

    A log line is a record with a shape, so a value that carries a newline
    does not merely look odd in it — it ENDS that record and writes the next
    one itself, and the next one can read ``requested='claude-opus-5'
    route=native status=200`` for a call that never happened. Control
    characters do the same job more quietly: a CR rewinds the line on a
    terminal, an ESC can repaint what is already on it.

    So CR and LF become the two characters that SPELL them, and every other
    non-printable becomes ``\\xNN`` / ``\\uNNNN``. The value stays readable —
    a forged id reads as an id with a ``\\n`` in it, which is itself the
    evidence that someone tried — while the record stays one line. A value
    that was never hostile passes through unchanged, which is why this can
    sit on the shared builder instead of at every call site.
    """
    text = (
        str(value)
        .replace("\r\n", "\\r\\n")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return "".join(
        ch
        if ch.isprintable()
        else (f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else f"\\u{ord(ch):04x}")
        for ch in text
    )


def _access_line(
    *,
    requested: str,
    route: str,
    forward: str,
    status: int,
    started: float,
    stream: bool,
    extra: str = "",
) -> str:
    """One line per request, in ONE shape, with no body and no credential.

    The incident this closes had a gateway that logged nothing per request:
    when a session started answering from the wrong upstream there was no
    record of which model the CLIENT had asked for, so a four-hour model flip
    could only be reconstructed from a 951 MB transcript. ``requested`` is
    therefore the first field — it is the one the log could not previously
    answer, and it differs from ``forward`` exactly when the gateway did
    something (namespace stripped, ``[1m]`` removed).
    """
    elapsed_ms = int((time.monotonic() - started) * 1000)
    # repr(), not the bare string: the model id is CLIENT-controlled, and a
    # newline in it would forge a second log line — a request could then
    # write a convincing "requested=… status=200" entry for a call that never
    # happened. repr escapes the newline and quotes the value, so a forged id
    # is visible AS an id.
    line = (
        f"model-gateway: requested={(requested or '-')!r} route={route} "
        f"forward={(forward or '-')!r} status={status} ms={elapsed_ms} "
        f"stream={str(bool(stream)).lower()}"
    )
    # The scrub lives HERE, on the one builder every access line goes through,
    # rather than at the five call sites — ``extra`` is assembled by callers
    # out of request paths, methods and refusal reasons, and "one request, one
    # line" has to hold for all of them or it holds for none. repr() above
    # already escapes a newline inside the model ids; this closes the fields
    # it does not reach.
    return _log_safe(f"{line} {extra}" if extra else line)


#: How often ONE peer's unauthorised refusals reach the log.
#:
#: Every other access line in this file is caused by a caller that ALREADY
#: holds the host token, so it is self-limiting: to make the daemon write a
#: line you must first have the capability. A 401 line is the only one an
#: uncredentialed local process can cause, and the daemon's own log is not
#: rotated while it runs — so without a cap, "make the refusal visible" is
#: also "let anything on this machine grow my log for free". One line per
#: peer per minute keeps a probe visible and bounds the amplification.
#:
#: Nothing is dropped silently: the next line that IS emitted carries
#: ``suppressed=N``, so a burst reads as a burst rather than as one probe.
UNAUTHORISED_LOG_WINDOW_S = 60.0


def _log_unauthorised(
    gateway: "Gateway", request: web.Request, *, started: float,
) -> None:
    """ONE access line for a request refused at the door. Rate-capped.

    The same shape as every other terminal outcome, which is the whole
    point: "one line per request" has to include the request that never got
    past the token check, or an unauthenticated probe is the single thing
    the log stays silent about — and that is the one a user asks about.

    Carries the METHOD and the PATH and nothing else. No header values, no
    token bytes (not a prefix, not a length), no body — on this path the
    body is never even read. The path is ``repr``'d for the same reason the
    model id is: it is client-controlled, and a newline in it would forge a
    second log line.
    """
    peer = _peer_host(request) or "-"
    suppressed = gateway.note_unauthorised(peer, time.monotonic())
    if suppressed is None:
        return
    extra = f"reason=unauthorised method={request.method} path={request.path!r}"
    if suppressed:
        extra = f"{extra} suppressed={suppressed}"
    logger.info(
        _access_line(
            requested="-",
            route="refused",
            forward="-",
            status=401,
            started=started,
            stream=False,
            extra=extra,
        )
    )


def _unauthorised() -> web.Response:
    return _json_error(
        401,
        "authentication_error",
        "missing or wrong model-gateway host token. Point the client at this "
        "gateway through the launcher, or read the token path with "
        "`vct-model-gateway --print-token-path`.",
    )


def _read_json_string(
    buf: bytes, start: int, end: int,
) -> "tuple[Optional[str], int]":
    """The JSON string literal starting at ``buf[start]``, and where it ends.

    ``start`` must be the opening quote. Escapes are honoured while looking
    for the closing one — an escaped quote inside a value must not end it —
    and the literal is decoded by :mod:`json` itself rather than by hand, so
    an escaped id is read exactly as the upstream will read it. Returns
    ``(None, …)`` when the literal is truncated or invalid, which is a normal
    outcome here: the buffer is a PREFIX of the body.
    """
    i = start + 1
    while i < end:
        c = buf[i]
        if c == 0x5C:  # backslash — the next byte is escaped, whatever it is
            i += 2
            continue
        if c == 0x22:  # the closing quote
            try:
                text = json.loads(buf[start:i + 1])
            except (ValueError, UnicodeDecodeError):
                return None, i + 1
            return (text if isinstance(text, str) else None), i + 1
        i += 1
    return None, end


class HeadField(NamedTuple):
    """A top-level field read out of a body the gateway did not parse.

    ``value`` is the decoded text. ``start`` and ``end`` bound the RAW literal
    inside the buffer — quotes included for a string — so a caller can splice
    a replacement into the bytes without re-encoding, or even parsing, the
    body around it. That is the whole reason the span is carried: on the
    over-buffer path there is no parsed body to edit, and the one edit the
    gateway still owes the request is a substitution of exactly this literal
    (see :func:`_splice_literal`).
    """

    value: str
    start: int
    end: int


def _splice_literal(buf: bytes, field: HeadField, replacement: str) -> bytes:
    """``buf`` with ``field``'s literal replaced by ``replacement``, encoded.

    The ONE edit made to a body that outgrew the rewrite buffer, and it is
    made because the thing being replaced is the GATEWAY's own invention: the
    ``claude-gw/`` namespace and Claude Code's ``[1m]`` suffix are client-side
    spellings that no upstream has ever heard of (see
    :mod:`model_router.routing`). Forwarding them verbatim would be forwarding
    a request only the gateway could have broken — the opposite of the reason
    this path exists. Everything else stays untouched: the tool-id repair is a
    correction of a VENDOR's output and skipping it costs a cosmetic id, not
    the request.

    ``json.dumps`` writes the replacement, so an id needing escapes is encoded
    the way the upstream's parser reads it rather than by string surgery.
    """
    return (
        buf[:field.start]
        + json.dumps(replacement).encode("utf-8")
        + buf[field.end:]
    )


def _shallow_top_level_fields(head: bytes) -> "dict[str, HeadField]":
    """The top-level scalar fields of a JSON object, read from its head.

    Used on ONE path: a request body too big to hold, where the gateway still
    has to know which upstream and which credential it is for. It reads only
    what it can prove is at the top level — a ``"model"`` nested inside a
    message, or one inside a string, is at a depth this walk tracks and is
    never mistaken for the request's own. That exactness is the point: a
    regex would send somebody's conversation to the wrong vendor, with the
    wrong key, on a body it never parsed.

    Duplicate top-level keys are read LAST-wins, which matches every JSON
    parser an upstream will use — but only within the scanned window: a
    second ``"model"`` sitting past :data:`HEAD_SCAN_BYTES` is invisible here
    while the upstream would honour it. A body with two top-level ``model``
    keys is malformed by convention rather than by grammar, and the outcome
    (the first one routes, the upstream sees the second) is documented rather
    than guarded, because guarding it would mean parsing the whole body —
    which is the thing this path exists to avoid.

    Values come back as :class:`HeadField` — the decoded text plus the span
    of the raw literal, which is what makes a splice possible (a string's
    contents, quotes excluded, but a span that INCLUDES them; a bare literal's
    spelling, e.g. ``"true"``). Containers are skipped, so nothing nested is
    returned. Missing is missing: a field sitting after a container longer
    than :data:`HEAD_SCAN_BYTES` is simply not found, and the caller decides
    what an absence means.
    """
    out: "dict[str, HeadField]" = {}
    end = min(len(head), HEAD_SCAN_BYTES)
    i = 0
    while i < end and head[i] in b" \t\r\n":
        i += 1
    if i >= end or head[i] != 0x7B:  # not a JSON object
        return out
    i += 1
    depth = 1
    expect_key = True
    key: Optional[str] = None
    while i < end:
        c = head[i]
        if c in b" \t\r\n":
            i += 1
            continue
        if c == 0x22:  # a string literal
            literal_start = i
            text, i = _read_json_string(head, i, end)
            if depth != 1:
                continue  # inside a container: read past it, keep nothing
            if text is None:
                return out  # truncated mid-literal; nothing after it is sound
            if expect_key:
                key = text
            else:
                if key is not None:
                    out[key] = HeadField(text, literal_start, i)
                key = None
            continue
        if depth == 1:
            if c == 0x3A:  # ':'
                expect_key = False
                i += 1
                continue
            if c == 0x2C:  # ','
                expect_key = True
                key = None
                i += 1
                continue
            if c == 0x7D:  # '}' — the whole object fit inside the head
                return out
            if c in b"{[":
                depth += 1
                i += 1
                continue
            start = i  # a bare literal: number, true, false, null
            while i < end and head[i] not in b",}] \t\r\n":
                i += 1
            if i == start:
                # NO PROGRESS. The byte here is one this scan has no branch
                # for and the literal scan stops on immediately — a ``]`` at
                # depth 1, i.e. a malformed body (``{"a":1]``, ``{"model":]``).
                # Continuing would re-examine the same byte forever, on the
                # EVENT LOOP: one such request froze the whole daemon, every
                # other session included. A body this broken has no readable
                # model, which is the caller's "cannot route" case, so
                # stopping here is also the honest answer.
                return out
            if key is not None and not expect_key:
                out[key] = HeadField(
                    head[start:i].decode("ascii", "replace"), start, i,
                )
            key = None
            continue
        if c in b"{[":
            depth += 1
        elif c in b"}]":
            depth -= 1
            if depth == 1:
                key = None  # the value that opened it is finished
        i += 1
    return out


async def _drain(content: aiohttp.StreamReader) -> int:
    """Read and discard whatever is left of a request body. Never raises.

    Precautionary, and measured rather than assumed: answering while the
    client is still uploading is the classic way a carefully-worded error is
    replaced by a connection reset in the client's write path. Against
    aiohttp's OWN client it is NOT reproducible — 8 MiB and 24 MiB bodies
    answered early both arrive complete, with no ``Connection: close`` — so
    this is not credited with fixing a symptom seen here. It is kept because
    the clients that actually talk to this daemon are not aiohttp (Node's
    undici, curl, the Python SDK), the cost is one pass over bytes already on
    the wire, and "finish reading the request before answering it" is the
    behaviour every HTTP client is written against.

    Errors are swallowed on purpose: the client hanging up mid-drain is the
    normal way this ends, and there is nothing left to report it to.
    """
    dropped = 0
    try:
        async for chunk in content.iter_any():
            dropped += len(chunk)
    except (
        ConnectionResetError,
        ConnectionAbortedError,
        aiohttp.ClientError,
        asyncio.TimeoutError,
    ):
        pass
    return dropped


async def _stream_after(
    head: bytes, content: aiohttp.StreamReader,
) -> AsyncIterator[bytes]:
    """The bytes already buffered, then the rest of the client's body.

    :func:`_buffer_bounded` stops holding at the chunk that crossed the bound
    and leaves the remainder in the reader, so this replays the head and then
    follows the stream: no byte is read twice and none is dropped.
    """
    if head:
        yield head
    async for chunk in content.iter_any():
        yield chunk


async def messages_handler(request: web.Request) -> web.StreamResponse:
    """Proxy ``/v1/messages`` and ``/v1/messages/count_tokens``.

    The body is BUFFERED, not streamed through: the routed model name has to
    be in the forwarded bytes, and on a vendor route the tool ids have to be
    restored, so on the ordinary path the gateway is not a pass-through. What
    it will hold to do that is
    :attr:`model_router.config.GatewayConfig.rewrite_buffer_bytes` (default
    :data:`model_router.config.REWRITE_BUFFER_LIMIT_BYTES`, 32 MiB — above
    Anthropic's own documented request ceiling, so every body the first-party
    upstream can accept is rewritten).

    Past that bound the request is still SERVED, because the gateway refuses
    nothing for size (see the body-size policy in the module docstring). The
    body is not parsed and not held: the buffered head is forwarded and the
    rest of the client's stream follows it, with no rewrite, and the upstream's
    own answer is relayed. Two consequences, both deliberate:

    * only the routing fields are read out of the head
      (:func:`_shallow_top_level_fields`) — enough to choose the upstream and
      the credential, and nothing more. A body whose ``model`` is not readable
      there cannot be routed at all and is the ONE size-related refusal left
      (400, ``reason=model_unreadable_in_head``): it is unroutable, not too
      big;
    * the body is forwarded as it arrived apart from ONE substitution: the
      model id literal is spliced to the routed name
      (:func:`_splice_literal`), because the ``claude-gw/`` namespace and the
      ``[1m]`` suffix are the gateway's OWN spellings and no upstream knows
      them. The tool-id repair does not run — it needs the whole body, and it
      corrects a vendor's output rather than the request. So an oversized
      request is answered by the upstream, on its merits, which is the point
      of this path.
    """
    gateway: Gateway = request.app[APP_KEY]
    # Taken BEFORE the auth check, so every outcome below — including the
    # two that answer without reading the body — measures the same thing:
    # elapsed since this request reached the handler.
    started = time.monotonic()
    if not gateway.authorised(request):
        _log_unauthorised(gateway, request, started=started)
        return _unauthorised()
    # "Warm while chats flow" (owner, 2026-09-23): a synchronous O(1) signal
    # that may schedule ONE background usage refresh per interval. Nothing is
    # awaited here, and a defect in it is guarded like every optional pass —
    # it can never delay, fail or alter this request.
    _guarded(gateway.usage_windows.note_activity, what="usage-window activity signal")

    buffer_limit = gateway.config.rewrite_buffer_bytes
    raw, over_buffer = await _buffer_bounded(request.content, buffer_limit)
    payload: Optional[dict] = None
    #: The body could not be parsed at all, so nothing about it is known and
    #: nothing about it is changed — see the ``except`` below.
    unparseable = False
    #: Where the model id sits in ``raw``, on the over-buffer path only. The
    #: span is what lets the routed name reach the upstream without the body
    #: being parsed — see :func:`_splice_literal`.
    model_field: Optional[HeadField] = None
    if over_buffer:
        # Deliberately NOT parsed and deliberately NOT refused: the body is
        # past what this daemon will hold, so it goes upstream as a stream and
        # only the fields that decide WHERE are read out of the head.
        head = _shallow_top_level_fields(raw)
        model_field = head.get("model")
        requested_model = model_field.value if model_field is not None else ""
        # Best effort, and only for the log: on a real oversized body the
        # ``stream`` flag usually sits after the messages array, past the
        # window this scan looks at.
        stream_field = head.get("stream")
        stream_requested = (
            stream_field is not None and stream_field.value == "true"
        )
        if not requested_model:
            logger.info(
                _access_line(
                    requested="-",
                    route="refused",
                    forward="-",
                    status=400,
                    started=started,
                    stream=stream_requested,
                    extra=(
                        f"reason=model_unreadable_in_head "
                        f"bytes>={buffer_limit} scan={HEAD_SCAN_BYTES}"
                    ),
                )
            )
            # Finish reading before answering — see `_drain` for what that
            # is and is not evidenced to prevent.
            await _drain(request.content)
            return _json_error(
                400,
                "invalid_request_error",
                "this request body is larger than the model gateway will "
                f"hold ({buffer_limit} bytes) and its `model` field is not in "
                f"the first {HEAD_SCAN_BYTES} bytes, so there is no way to "
                "tell which upstream it is for. Nothing here is refused for "
                "its size — a body this large is forwarded unrewritten as "
                "soon as the model can be read. Send `model` as an early "
                "field.",
            )
    else:
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("body is not a JSON object")
        except (ValueError, RecursionError) as exc:
            # NOT a gateway 400. Native sends whatever the client produced and
            # the API judges it, so a body this daemon cannot parse — a
            # truncated write, a 2000-deep structure that blows the recursive
            # decoder, an encoding the client and json disagree about — is
            # forwarded to the first-party upstream exactly as it arrived,
            # with the client's own headers, and ITS verdict is relayed. The
            # gateway inventing a 400 here would be the one thing that cannot
            # happen natively: a refusal the user cannot appeal to anyone.
            logger.info(
                "model-gateway: request body not parseable (%s); forwarding "
                "it to the first-party upstream unread", _log_safe(exc),
            )
            unparseable = True
            requested_model = ""
            stream_requested = False
        else:
            payload = parsed
            requested_model = payload.get("model") or ""
            stream_requested = bool(payload.get("stream"))

    decision: "Route | RouteError"
    if unparseable:
        # There is no model to route on, so the route is the one that needs
        # none: the user's own first-party account, which is where an
        # unrouted Claude Code request goes natively.
        decision = Route(
            upstream=gateway.anthropic.upstream,
            forward_model="",
            vendor=None,
            family_id=gateway.anthropic.family_id,
            is_anthropic=True,
        )
    else:
        decision = route_model(requested_model, gateway.vendors, gateway.anthropic)
    if isinstance(decision, RouteError):
        logger.info(
            _access_line(
                requested=str(requested_model),
                route="refused",
                forward="-",
                status=decision.status,
                started=started,
                stream=stream_requested,
                extra=f"reason={decision.reason}",
            )
        )
        return _json_error(decision.status, "invalid_request_error", decision.message)

    def log_local(status: int, reason: str) -> None:
        """A gateway-side refusal, in the SAME shape as every other outcome.

        "One line per request" has to mean every terminal outcome, refusals
        included — a request that dies here (no Claude login, no vendor key)
        is exactly the kind that gets reported as "the gateway is broken",
        and it must not be the one case the log is silent about.
        """
        logger.info(
            _access_line(
                requested=str(requested_model),
                route=_route_label(decision),
                forward=decision.forward_model,
                status=status,
                started=started,
                stream=stream_requested,
                extra=f"reason={reason}",
            )
        )

    # Everything the RELAY will need about this request once the RESPONSE
    # ends. Assembled here because this is the last point at which both the
    # client's headers and its parsed body are in hand.
    session, agent, parent_agent = read_identity(request.headers)
    is_count_tokens = _canonical_path(request) == COUNT_TOKENS_PATH
    facts = RequestFacts(
        session=session,
        agent=agent,
        parent_agent=parent_agent,
        count_tokens=is_count_tokens,
        # Only a VENDOR count_tokens can need it, and computing it otherwise
        # would serialise a whole conversation for an answer nobody reads.
        count_estimate=(
            count_tokens_estimate(payload)
            if is_count_tokens and not decision.is_anthropic
            else None
        ),
    )

    headers = gateway.forward_headers(request)
    # ``None`` on the over-buffer path — there is no parsed body to rewrite,
    # and every rewrite below is guarded by that rather than by re-testing the
    # size.
    forward_payload: Optional[dict] = payload
    mutated = False
    #: A repair pass was abandoned (:func:`_guarded`). It rides to the access
    #: line in ``note`` rather than being logged separately, so one request
    #: still means one line.
    rewrite_failed = False
    if decision.is_anthropic:
        oauth = gateway.oauth.read()
        if oauth.token is None:
            log_local(401, "claude_login_unavailable")
            return _json_error(401, "authentication_error", oauth.problem or "")
        headers["Authorization"] = f"Bearer {oauth.token}"
        beta = headers.get("anthropic-beta", "")
        if OAUTH_BETA not in beta:
            beta = f"{beta},{OAUTH_BETA}" if beta else OAUTH_BETA
        # The 1M window is a beta header, not a model name (routing strips the
        # suffix). Every beta the client sent is kept; this only ADDS, and only
        # when the client did not already ask for the window itself.
        if decision.one_m_requested and "context-1m" not in beta:
            beta = f"{beta},{CONTEXT_1M_BETA}" if beta else CONTEXT_1M_BETA
        if beta:
            headers["anthropic-beta"] = beta

        # An older session can still carry vendor-shaped tool blocks in its
        # HISTORY. Anthropic's validator rejects the whole request for them,
        # with a message naming a message index the user cannot act on — and
        # since the transcript is append-only the session never recovers. So
        # they are repaired in flight rather than relayed into a dead end.
        if forward_payload is not None:
            repaired = _guarded(
                sanitise_for_anthropic,
                forward_payload,
                what="first-party tool-block repair",
            )
            if repaired is _ABANDON:
                # The client's own bytes go on unchanged and Anthropic's
                # validator gets the last word — which is what native gets.
                rewrite_failed = True
            else:
                forward_payload, repair = repaired
                if repair.changed:
                    mutated = True
                    logger.info(
                        "model-gateway: repaired inherited tool blocks before "
                        "the first-party route (%s) at message index(es) %s",
                        repair.summary(),
                        ", ".join(str(i) for i in repair.touched_indexes) or "-",
                    )
    else:
        vendor = decision.vendor
        assert vendor is not None  # noqa: S101 — guaranteed by Route
        key_result = await gateway.keys.aresolve(vendor)
        if not key_result.key:
            log_local(503, "vendor_key_unavailable")
            return _json_error(
                503, "api_error", key_result.problem or "no vendor key available",
            )
        headers[vendor.auth_header] = f"{vendor.auth_scheme}{key_result.key}"
        # Hand the vendor back its OWN ids: we rewrote them on the way out.
        if forward_payload is not None:
            restored_pair = _guarded(
                restore_vendor_ids,
                forward_payload,
                gateway.id_map(vendor),
                what="vendor id restoration",
            )
            if restored_pair is _ABANDON:
                rewrite_failed = True
            else:
                forward_payload, restored = restored_pair
                if restored:
                    mutated = True

    headers["Content-Type"] = "application/json"
    headers.setdefault("anthropic-version", DEFAULT_ANTHROPIC_VERSION)

    body: "bytes | AsyncIterator[bytes]"
    note = "note=rewrite_failed" if rewrite_failed else ""
    if unparseable:
        # Byte for byte, headers and all. The upstream is the only party that
        # can say whether these bytes are a request.
        body = raw
        note = f"{note} note=unparseable_body_forwarded".strip()
    elif forward_payload is None:
        # Streamed on, with ONE edit. The tool-id repair does not run (that is
        # what ``forwarded_unrewritten`` says), because it needs the whole body
        # and skipping it costs a cosmetic id. The model id IS fixed, because
        # the namespace and the ``[1m]`` suffix are the gateway's own
        # client-side spellings and no upstream knows them: leaving them in
        # would make the gateway the author of the failure it is here to
        # avoid. ``decision.forward_model`` is the same value the ordinary
        # path writes into the body, so both paths send the upstream the same
        # id — and on the first-party route the ``[1m]`` window travels as the
        # ``anthropic-beta`` header set above, which this path already gets.
        head_bytes = raw
        model_note = "model_id=verbatim"
        if model_field is not None and decision.forward_model != requested_model:
            head_bytes = _splice_literal(
                raw, model_field, decision.forward_model,
            )
            model_note = "model_id=spliced"
        body = _stream_after(head_bytes, request.content)
        note = (
            f"{REWRITE_BUFFER_NOTE} bytes>={buffer_limit} "
            f"forwarded_unrewritten {model_note}"
        )
    else:
        # The routed name must be in the BYTES, not only in the router's copy.
        if decision.forward_model != requested_model:
            forward_payload = dict(forward_payload)
            forward_payload["model"] = decision.forward_model
            mutated = True
        body = (
            json.dumps(forward_payload, **COMPACT_JSON).encode("utf-8")
            if mutated
            else raw
        )

    # Canonical path, i.e. the trailing slash a lenient client may have sent
    # is not passed upstream. Query string IS passed through untouched.
    url = f"{decision.upstream}{_canonical_path(request)}"
    if request.query_string:
        url = f"{url}?{request.query_string}"

    return await _proxy(
        request,
        gateway,
        decision,
        url,
        headers,
        body,
        requested_model=str(requested_model),
        stream_requested=stream_requested,
        started=started,
        note=note,
        facts=facts,
    )


def _reread_oauth_headers(
    gateway: "Gateway", headers: dict[str, str],
) -> "Optional[dict[str, str]]":
    """Headers carrying a NEWER Claude login, or ``None`` if nothing changed.

    The gateway never refreshes the login itself — it only reads the file the
    Claude CLI writes (see :mod:`model_router.auth`) — but a native client on
    the same machine refreshes it roughly every eight hours, and it does so
    behind a lock and a compare-and-swap that a second implementation must
    not race. What this daemon CAN do is what the native client does after a
    401 of its own: read the file again and adopt whatever is there now.

    That closes the ordinary case — a long-lived gateway holding a token that
    expired mid-session while a native window had already rotated it —
    without owning any part of the refresh protocol. ``None`` means the file
    still holds the token we just used, so a retry would only repeat the 401.
    """
    fresh = gateway.oauth.read()
    if not fresh.token:
        return None
    presented = headers.get("Authorization", "")
    if presented == f"Bearer {fresh.token}":
        return None
    updated = dict(headers)
    updated["Authorization"] = f"Bearer {fresh.token}"
    return updated


async def _proxy(
    request: web.Request,
    gateway: Gateway,
    decision: Route,
    url: str,
    headers: dict[str, str],
    body: "bytes | AsyncIterator[bytes]",
    *,
    requested_model: str = "",
    stream_requested: bool = False,
    started: Optional[float] = None,
    note: str = "",
    allow_oauth_retry: bool = True,
    facts: Optional[RequestFacts] = None,
) -> web.StreamResponse:
    """Forward one request upstream and relay the answer.

    ``body`` is bytes on the ordinary path and an async iterator when the
    request outgrew the rewrite buffer — aiohttp sends the latter chunked,
    which is one reason ``content-length`` is dropped from the forwarded
    headers in every case. ``note`` rides along on every access line this call
    writes, so a streamed-through request is identifiable from the log alone
    rather than only from the absence of a rewrite.

    Exactly one condition is retried, and only when the body is bytes: a
    first-party 401 whose credentials file has since changed
    (:func:`_reread_oauth_headers`). ``allow_oauth_retry`` is the recursion
    bound — the retried call sets it False, so a genuinely dead login answers
    401 rather than looping.

    On a stream, the SSE rewriter's held tail is flushed after the last
    upstream chunk — except on the client-disconnect path, where it is
    deliberately dropped: there is no longer a client to deliver it to, and
    writing to a closed transport would replace a routine "user hit Esc" with
    an exception in the log.

    ``facts`` carries the chat id and the ``count_tokens`` flags the ledger
    and the zero-guard need; ``None`` means "do not account this request",
    which is what an internal caller without a client request gets.
    Accounting reads a COPY of the bytes already going to the client — after
    the rewriter, so it observes exactly what the client observes — and every
    entry point is behind :func:`_guarded`, so a defect in it abandons the
    accounting and not the stream.
    """
    started = time.monotonic() if started is None else started
    route_label = _route_label(decision)
    #: Set when a RESPONSE-side repair was abandoned (:func:`_guarded`). A
    #: plain local read by the closure below, because the failure can happen
    #: at any point of the relay while the line is written at the end — and
    #: one request must still mean one line.
    rewrite_failed = False
    #: Reads the ``usage`` block out of the relayed bytes. ``None`` until the
    #: response's content type says whether it is a stream, and ``None`` again
    #: the moment a :func:`_guarded` call abandons it.
    accumulator: Optional[UsageAccumulator] = None

    def access(status: int, *, stream: bool, extra: str = "") -> None:
        nonlocal accumulator
        parts = [part for part in (extra, note) if part]
        # Once, however many passes were abandoned: the handler may already
        # have put it in `note` for a REQUEST-side failure, and one line
        # saying the same thing twice reads as two events.
        if rewrite_failed and "note=rewrite_failed" not in note:
            parts.append("note=rewrite_failed")
        # THE terminal point of this request, so it is also where the
        # accounting is closed: one access line and at most one ledger row,
        # written from the same place, can never disagree about how a request
        # ended. A truncated stream and a client that walked away both arrive
        # here, and both are real token spends.
        totals: Mapping[str, int] = {}
        seen = False
        complete = False
        if accumulator is not None:
            if _guarded(accumulator.close, what="usage accounting") is _ABANDON:
                accumulator = None
            else:
                totals = accumulator.totals()
                seen = accumulator.saw_anything
                complete = accumulator.complete
        parts.append(access_extra(totals, seen=seen))
        logger.info(
            _access_line(
                requested=requested_model or decision.forward_model,
                route=route_label,
                forward=decision.forward_model,
                status=status,
                started=started,
                stream=stream,
                extra=" ".join(parts),
            )
        )
        if seen:
            # A lambda because ``_guarded`` forwards positional arguments
            # only, and every argument here is keyword-named on purpose:
            # ``status``/``stream``/``totals`` are three values a positional
            # call would be free to transpose.
            _guarded(
                lambda: _submit_usage(
                    gateway,
                    decision,
                    facts,
                    requested=requested_model or decision.forward_model,
                    route=route_label,
                    status=status,
                    stream=stream,
                    totals=totals,
                    usage_complete=complete,
                ),
                what="usage ledger",
            )
        # Issue 9 — model echo assertion, at the ONE terminal point every
        # response ends at, so both the streamed and the buffered vendor
        # shapes are covered. Compared here rather than in ``_submit_usage``
        # because that fires only under ``seen`` (a usage-less 2xx would
        # escape the assertion) and only after the decline gates. Absence of
        # the field is silence, not mismatch — the same discipline the usage
        # block uses — so a vendor that does not echo a model never counts.
        # First-party answers are exempt: the alias trap is a vendor
        # property (``vendors.py``), and native is the definition of correct.
        if (
            accumulator is not None
            and 200 <= status < 300
            and decision.vendor is not None
            and decision.forward_model
        ):
            reported = accumulator.reported_model
            if reported is not None and reported != decision.forward_model:
                gateway.note_model_echo_mismatch(
                    decision.vendor.vendor_id,
                    decision.forward_model,
                    reported,
                )

    try:
        upstream_ctx = gateway.session.post(
            url,
            data=body,
            headers=headers,
            # NO total budget, on purpose: a completion runs as long as it
            # runs, and a total one cut a healthy 601 s stream on 2026-09-09.
            # What is bounded is CONNECTING and going quiet — the two
            # conditions that actually mean the upstream is not coming back.
            timeout=aiohttp.ClientTimeout(
                total=None,
                sock_connect=UPSTREAM_CONNECT_TIMEOUT_S,
                sock_read=gateway.config.upstream_idle_timeout_s,
            ),
        )
    except RuntimeError as exc:  # session not started — a programming error
        logger.error("model-gateway: %s", exc)
        access(502, stream=stream_requested)
        return _json_error(502, "api_error", str(exc), headers=RETRYABLE_HEADERS)

    try:
        async with upstream_ctx as upstream:
            if (
                allow_oauth_retry
                and decision.is_anthropic
                and upstream.status == 401
                and isinstance(body, (bytes, bytearray))
            ):
                # The login may have been refreshed by a native client while
                # this request was in flight, and the credentials file is the
                # shared truth: re-reading it costs one stat. Bytes only,
                # because a streamed body is already consumed and cannot be
                # sent twice — that request carries the 401 to the client,
                # which is what native does when its own retry is impossible.
                # `allow_oauth_retry=False` below is what bounds this at one.
                refreshed = _reread_oauth_headers(gateway, headers)
                if refreshed is not None:
                    return await _proxy(
                        request,
                        gateway,
                        decision,
                        url,
                        refreshed,
                        body,
                        requested_model=requested_model,
                        stream_requested=stream_requested,
                        started=started,
                        note=f"{note} note=oauth_reread_retry".strip(),
                        allow_oauth_retry=False,
                    )
            vendor = decision.vendor
            # WP-6 review MAJOR-1, refined by the no-discovery-vendor
            # addendum: a vendor 401/403 must be CLASSIFIED before it can
            # touch the strike counter, and classifying means reading the
            # body — which consumes it. So this branch owns the whole
            # response: it counts key-level rejections (three consecutive
            # invalidate the last-good key; any 2xx resets via the elif
            # below), exempts the documented model-level access-denial
            # shape, and relays the vendor's own answer verbatim either
            # way. Status sets here are disjoint from the quota branch's.
            if vendor is not None and upstream.status in (401, 403):
                return await _auth_rejection_response(
                    request, gateway, vendor, upstream, access,
                )
            if vendor is not None and 200 <= upstream.status < 300:
                gateway.note_vendor_success(vendor.vendor_id)
            if vendor is not None and upstream.status in QUOTA_STATUSES:
                return await _quota_response(
                    upstream, vendor, stream_requested, access,
                )

            relay = gateway.relay_headers(upstream)
            if decision.is_anthropic:
                # Passive read of the unified rate-limit headers the answer
                # already carries: a dict parse, no I/O, guarded like every
                # other optional pass so it can never cost the request.
                _guarded(
                    gateway.usage_windows.observe_anthropic_headers,
                    upstream.headers,
                    what="usage-window header capture",
                )
            content_type = upstream.headers.get("Content-Type", "application/json")
            is_stream = _STREAM_CHUNK_HINT in content_type

            # Built here, where the framing is finally known: an SSE body is
            # read event by event, a JSON one is buffered (bounded) and parsed
            # at the end. Not built at all when ``facts`` says this request is
            # not accounted — the work is small, but so is the reason to do it.
            if facts is not None and not facts.count_tokens:
                accumulator = UsageAccumulator(stream=is_stream)

            if (
                vendor is not None
                and not is_stream
                and upstream.status not in (204, 304)
                and "json" in content_type.lower()
            ):
                return await _relay_vendor_json(
                    request,
                    gateway,
                    vendor,
                    upstream,
                    relay,
                    access,
                    decision=decision,
                    facts=facts,
                    accumulator=accumulator,
                )

            rewriter = (
                SseIdRewriter(id_map=gateway.id_map(vendor))
                if vendor is not None and is_stream
                else None
            )

            response = web.StreamResponse(status=upstream.status, headers=relay)
            # The relayed Content-Type header carries the type; we only choose
            # the framing. Chunked is invalid for a body-less status, so it is
            # enabled only where a body is legal.
            if upstream.status not in (204, 304):
                response.enable_chunked_encoding()
            await response.prepare(request)

            # Past prepare(), the status line is on the wire: an error here
            # can no longer become a JSON error response. Stop honestly and
            # log instead — an SSE stream that ends early is what the client
            # sees, which is the truth.
            total = 0

            async def send(data: bytes) -> bool:
                """Write to the CLIENT. ``False`` once it has gone away."""
                try:
                    await response.write(data)
                except (
                    ConnectionResetError,
                    ConnectionAbortedError,
                    aiohttp.ClientConnectionError,
                ):
                    return False
                return True

            # Reading upstream and writing to the client are separated on
            # purpose, because the SAME exception type means opposite things
            # on the two sides and one `try` around both cannot tell them
            # apart. `ServerTimeoutError` is an `asyncio.TimeoutError` AND an
            # `aiohttp.ClientConnectionError`, so an upstream that went quiet
            # was being logged as "the client left" and answered with a CLEAN
            # end — the silent truncation this whole path exists to prevent.
            # Which side raised is the only reliable discriminator, and it is
            # structural.
            chunks = upstream.content.iter_any().__aiter__()
            try:
                while True:
                    try:
                        chunk = await chunks.__anext__()
                    except StopAsyncIteration:
                        break
                    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                        # The UPSTREAM broke or went quiet mid-stream. Returning
                        # the prepared response here would let aiohttp finish it
                        # NORMALLY — the chunked terminator goes out and the
                        # client reads a clean 200 that merely has no
                        # ``message_stop``: a truncated answer it has no reason to
                        # retry, which is strictly worse than native, where the
                        # same failure arrives as a premature close and IS
                        # retried. So the connection is aborted, which is that
                        # premature close.
                        logger.warning(
                            "model-gateway: %s -> %s stream ended early after %dB: %s",
                            _log_safe(decision.forward_model), decision.family_id,
                            total, _log_safe(exc),
                        )
                        access(
                            upstream.status,
                            stream=is_stream,
                            extra=f"bytes={total} note=upstream_ended_early",
                        )
                        _abort_connection(request)
                        return response

                    if rewriter is None:
                        out = chunk
                    else:
                        out = _guarded(rewriter.feed, chunk, what="SSE id rewrite")
                        if out is _ABANDON:
                            # Abandon the REWRITE, not the stream: what the
                            # rewriter was holding is upstream's, already off the
                            # socket, so it goes out ahead of this chunk and
                            # everything after is relayed verbatim.
                            out = rewriter.take_pending() + chunk
                            rewriter = None
                            rewrite_failed = True
                    if out:
                        # AFTER the rewrite and BEFORE the write: the
                        # accumulator must see exactly the bytes the client
                        # sees, and it must not be able to delay them. It
                        # returns nothing — these bytes are already committed.
                        if accumulator is not None and _guarded(
                            accumulator.feed, out, what="usage accounting",
                        ) is _ABANDON:
                            accumulator = None
                        if not await send(out):
                            # The CLIENT went away (closed a tab, hit Esc). Routine,
                            # not an error, and NOT something to abort over: there
                            # is no longer anyone to tell.
                            access(
                                upstream.status,
                                stream=is_stream,
                                extra=f"bytes={total} note=client_disconnected",
                            )
                            return response
                        total += len(out)

                if rewriter is not None:
                    tail = _guarded(rewriter.flush, what="SSE id flush")
                    if tail is _ABANDON:
                        tail = rewriter.take_pending()
                        rewrite_failed = True
                    if tail:
                        if accumulator is not None and _guarded(
                            accumulator.feed, tail, what="usage accounting",
                        ) is _ABANDON:
                            accumulator = None
                        if not await send(tail):
                            access(
                                upstream.status,
                                stream=is_stream,
                                extra=f"bytes={total} note=client_disconnected",
                            )
                            return response
                        total += len(tail)

                try:
                    await response.write_eof()
                except (
                    ConnectionResetError,
                    ConnectionAbortedError,
                    aiohttp.ClientConnectionError,
                ):
                  access(
                      upstream.status,
                      stream=is_stream,
                      extra=f"bytes={total} note=client_disconnected",
                  )
                  return response
            except asyncio.CancelledError:
                # ``handler_cancellation=True`` (see ``run_app`` in
                # ``model_router.__main__``) cancels this handler the moment
                # the CLIENT goes away — which is the whole point, it drops
                # the upstream request instead of burning quota on an answer
                # nobody will read. But ``CancelledError`` is a
                # BaseException: it passes straight through every
                # ``except (ClientError, TimeoutError)`` below, so without
                # this the most common real-world ending — the user hit Esc —
                # was the ONE outcome that wrote no access line at all, and
                # the `client_disconnected` branches only ever fired in
                # tests. Logged, then re-raised: swallowing a cancellation
                # would leave the task running against a dead connection.
                access(
                    upstream.status,
                    stream=is_stream,
                    extra=f"bytes={total} note=client_disconnected",
                )
                raise
            if rewriter is not None:
                _log_repair(
                    decision.family_id, rewriter.stats, rewriter.blocks_suppressed,
                )
            access(upstream.status, stream=is_stream, extra=f"bytes={total}")
            return response
    except asyncio.TimeoutError:
        logger.warning(
            "model-gateway: %s -> %s went quiet for %ss",
            _log_safe(decision.forward_model), decision.family_id,
            gateway.config.upstream_idle_timeout_s,
        )
        access(502, stream=stream_requested, extra="note=upstream_timeout")
        return _json_error(
            502,
            "api_error",
            f"upstream {decision.upstream} sent nothing for "
            f"{gateway.config.upstream_idle_timeout_s}s",
            headers=RETRYABLE_HEADERS,
        )
    except aiohttp.ClientError as exc:
        logger.warning(
            "model-gateway: %s -> %s unreachable: %s",
            _log_safe(decision.forward_model), decision.family_id, _log_safe(exc),
        )
        access(502, stream=stream_requested, extra="note=upstream_unreachable")
        return _json_error(
            502,
            "api_error",
            f"upstream {decision.upstream} unreachable: {exc}",
            headers=RETRYABLE_HEADERS,
        )


def _abort_connection(request: web.Request) -> None:
    """Kill the connection so a half-written response READS as half-written.

    The only honest ending for a response whose body stopped arriving after
    the status line went out. aiohttp would otherwise close the chunked body
    cleanly, and a truncated SSE stream ending in a well-formed EOF is
    indistinguishable, to the client, from a complete one — it stops, shows a
    partial answer and never retries. An aborted transport surfaces as
    ``ClientPayloadError`` / a premature close, which is what the same
    upstream failure looks like natively and what every client already knows
    how to handle.

    Best-effort by design: no transport (a test double, a connection already
    gone) means there is nothing left to abort.
    """
    transport = request.transport
    if transport is None:
        return
    try:
        transport.abort()
    except Exception:  # noqa: BLE001 — a cleanup path may never raise
        logger.debug("model-gateway: transport abort failed", exc_info=True)


def _log_repair(family_id: str, stats: RepairStats, suppressed: int = 0) -> None:
    """One line per RESPONSE that needed repairing — not one per block.

    Both response paths (buffered JSON and SSE) come through here, so "log
    once" is a property of one function rather than a convention two call
    sites happen to share.
    """
    if not stats.changed:
        return
    logger.info(
        "model-gateway: normalised vendor tool ids on %s (%s suppressed_blocks=%d)",
        family_id, stats.summary(), suppressed,
    )


def _auth_rejection_is_key_level(status: int, raw: bytes) -> bool:
    """Is a vendor 401/403 about the KEY, or about the MODEL?

    The two feed different machinery and must not be conflated:

    * KEY-level (401, or a 403 about the credential) increments the
      auth-strike counter — three consecutive and the last-good key store is
      invalidated, because a key the VENDOR rejects must not keep serving
      stale.
    * MODEL-level (HTTP 403 ``AccessDenied``, error code ``access_denied``)
      means the model is gated or deprecated while the key is perfectly
      good. Feeding those to the strike counter would invalidate a healthy
      key after three requests for models the vendor itself still lists —
      the exact trap this classifier exists to defuse.

    Only the DOCUMENTED model-level shape is exempted: the vendor's own
    client docs describe deprecated models answering 403 with that code, and
    :mod:`model_router.quota` established the house rule that a claim of
    this size needs positive evidence in the body. An unparseable body, or a
    403 whose error shape is unrecognised, counts as key-level — which is
    the pre-addendum behaviour, no worse — because the strike counter's
    purpose (protecting the last-good store) must not silently switch off on
    a body the gateway could not read.
    """
    if status == 401:
        return True
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        return True
    if not isinstance(payload, dict):
        return True
    error = payload.get("error")
    if not isinstance(error, dict):
        return True
    code = str(error.get("code") or "").strip().lower()
    error_type = str(error.get("type") or "").strip().lower()
    return "access_denied" not in (code, error_type)


async def _auth_rejection_response(
    request: web.Request,
    gateway: "Gateway",
    vendor: Vendor,
    upstream: aiohttp.ClientResponse,
    access: Callable[..., None],
) -> web.StreamResponse:
    """Classify a vendor 401/403, then relay the vendor's own answer verbatim.

    The body has to be read to classify it (see
    :func:`_auth_rejection_is_key_level`), and an aiohttp ``StreamReader``
    cannot be un-read — so this branch OWNS the response end to end, exactly
    like :func:`_quota_response` owns the quota statuses. Past the hold bound
    the held bytes are written and the remainder streamed, verbatim, the
    relay shape :func:`_relay_vendor_json` uses for oversized bodies: the
    classification is ours, the answer is the vendor's, and neither editing
    the other is the bug this whole function avoids.
    """
    relay = gateway.relay_headers(upstream)
    raw, overflowed = await _buffer_bounded(
        upstream.content, _AUTH_BODY_LIMIT_BYTES,
    )
    key_level = _auth_rejection_is_key_level(upstream.status, raw)
    if key_level:
        gateway.note_vendor_auth_rejection(vendor.vendor_id)
        auth_class = "key"
    else:
        logger.info(
            "model-gateway: vendor %s refused the MODEL, not the key "
            "(access_denied 403; likely a deprecated id the vendor still "
            "lists) — relayed without an auth strike",
            _log_safe(vendor.vendor_id),
        )
        auth_class = "model_access_denied"
    if overflowed:
        logger.warning(
            "model-gateway: %s auth rejection exceeds %d bytes; classified "
            "from the held prefix and relayed verbatim",
            vendor.vendor_id, _AUTH_BODY_LIMIT_BYTES,
        )
        response = web.StreamResponse(status=upstream.status, headers=relay)
        response.enable_chunked_encoding()
        await response.prepare(request)
        total = len(raw)
        await response.write(raw)
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
            total += len(chunk)
        await response.write_eof()
        access(
            upstream.status, stream=False,
            extra=f"note=vendor_auth auth_class={auth_class}",
        )
        return response
    access(
        upstream.status,
        stream=False,
        extra=f"bytes={len(raw)} note=vendor_auth auth_class={auth_class}",
    )
    return web.Response(status=upstream.status, headers=relay, body=raw)


async def _quota_response(
    upstream: aiohttp.ClientResponse,
    vendor: Vendor,
    stream_requested: bool,
    access: Callable[..., None],
) -> web.StreamResponse:
    """Replace a vendor's quota refusal with one that names the vendor.

    The ONE documented exception to relaying a vendor's ERROR verbatim, and
    the only place a status is rewritten (see :mod:`model_router.quota` for
    the incident). The upstream body is read —
    bounded — for a reset hint and then logged at DEBUG, so support can still
    see the vendor's own words without them reaching a user who would read
    them as an Anthropic limit.

    The same body decides WHICH sentence is sent:
    :func:`model_router.quota.classify_quota` claims exhaustion only on
    positive evidence, so a thirty-second rate limit no longer tells the user
    to abandon the model family.

    JSON for a streaming request too, deliberately: an error status is not
    part of the stream. The SDK parses any non-2xx body as JSON whatever the
    request asked for, so an SSE frame here would surface as a parser
    exception with the typed ``rate_limit_error`` lost — the opposite of the
    point. ``stream_requested`` survives only as a field in the access line.
    """
    # A PEEK, deliberately, and the one place ``read(n)``'s real semantics
    # (what has ARRIVED, trimmed to n — see :func:`_buffer_bounded`) are
    # harmless: this body is never relayed, so a short read costs at most a
    # reset hint that was still in flight.
    raw = await upstream.content.read(_QUOTA_BODY_PEEK_BYTES)
    logger.debug(
        "model-gateway: %s quota refusal HTTP %d body=%s",
        vendor.vendor_id, upstream.status,
        # A body the gateway did not author, on a log line: same forging shape
        # as a client-controlled model id, one actor further out.
        _log_safe(raw.decode("utf-8", "replace")),
    )
    hint = find_reset_hint(raw, upstream.headers)
    classification = classify_quota(upstream.status, raw, upstream.headers)
    body = quota_error_body(
        vendor_display_name(vendor),
        hint,
        status=upstream.status,
        classification=classification,
    )
    access(
        CLIENT_QUOTA_STATUS,
        stream=stream_requested,
        # The classification is in the LOG as well as in the message: when a
        # user reports "it told me to switch models", the line says whether
        # the gateway had evidence for that or fell back to the weaker
        # sentence, without needing the body it deliberately did not keep.
        extra=(
            f"note=vendor_quota upstream_status={upstream.status} "
            f"quota_class={classification}"
        ),
    )
    # The vendor's own wait, relayed: the BODY is replaced (that is this
    # function's whole job), but "when may I try again?" is machine-readable
    # advice the client acts on, and dropping it turns a 30 s rate limit into
    # a guess. ``x-should-retry`` says the same thing to an SDK that reads it
    # rather than the header.
    quota_headers = dict(RETRYABLE_HEADERS)
    retry_after = upstream.headers.get("Retry-After")
    if retry_after:
        quota_headers["Retry-After"] = retry_after
    return web.json_response(
        body, status=CLIENT_QUOTA_STATUS, headers=quota_headers,
    )


async def _buffer_bounded(
    content: aiohttp.StreamReader, limit: int,
) -> tuple[bytes, bool]:
    """Accumulate up to ``limit`` bytes; report whether the body outgrew it.

    ``StreamReader.read(n)`` is NOT "n bytes". aiohttp 3.x waits only until
    the buffer is non-empty and then returns what is BUFFERED, trimmed to n
    (``_read_nowait``), so a body still arriving in TCP segments comes back
    at whatever boundary the first wakeup found — and a truncated JSON body
    either fails to parse (the rewrite is skipped and a HALF document is
    relayed) or, worse, parses as a prefix that happens to be valid.
    Iterating to EOF is the only shape that means "at most n bytes" rather
    than "as much as had arrived".

    Returns ``(buffer, overflowed)``. On overflow the buffer holds everything
    read so far INCLUDING the chunk that crossed the bound, so the caller can
    write it and then stream the remainder: no byte is read twice, none is
    dropped, and the bound is on what is HELD, not on what is relayed.
    """
    buffer = bytearray()
    async for chunk in content.iter_any():
        buffer += chunk
        if len(buffer) > limit:
            return bytes(buffer), True
    return bytes(buffer), False


async def _relay_vendor_json(
    request: web.Request,
    gateway: Gateway,
    vendor: Vendor,
    upstream: aiohttp.ClientResponse,
    relay: dict[str, str],
    access: Callable[..., None],
    *,
    decision: Optional[Route] = None,
    facts: Optional[RequestFacts] = None,
    accumulator: Optional[UsageAccumulator] = None,
) -> web.StreamResponse:
    """Buffer a vendor's JSON response, normalise its tool ids, relay it.

    Buffering is confined to this branch — a vendor route, a JSON
    content-type — and bounded by
    :data:`model_router.tool_ids.JSON_BUFFER_LIMIT_BYTES`. Past the bound the
    bytes are relayed unrewritten with a warning: a proxy must not become a
    memory sink over a cosmetic id, and the streaming path (which is what
    Claude Code actually uses) has no such buffer at all.

    The accumulation goes through :func:`_buffer_bounded` rather than one
    ``read(limit + 1)`` because that call reads ONE chunk, not ``limit + 1``
    bytes: a response split across TCP segments came back truncated, which is
    a relayed half-document, not a missed rewrite.

    Two things ride on the body already being held here, and neither is worth
    a second copy of it:

    * the ``usage`` block is handed to ``accumulator`` rather than re-buffered;
    * a ``count_tokens`` answer is checked for the vendor zero (see
      :func:`model_router.usage.guard_count_tokens`) and labelled with where
      its number came from. That label is added on the VENDOR route only —
      decorating a first-party response with a field Anthropic never sent is
      the "worse than native" this gateway exists not to be.
    """
    raw, overflowed = await _buffer_bounded(
        upstream.content, JSON_BUFFER_LIMIT_BYTES,
    )
    if overflowed:
        logger.warning(
            "model-gateway: %s response exceeds %d bytes; relayed without "
            "tool-id normalisation",
            vendor.vendor_id, JSON_BUFFER_LIMIT_BYTES,
        )
        response = web.StreamResponse(status=upstream.status, headers=relay)
        response.enable_chunked_encoding()
        await response.prepare(request)
        total = len(raw)
        await response.write(raw)
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
            total += len(chunk)
        await response.write_eof()
        access(upstream.status, stream=False, extra=f"bytes={total}")
        return response

    out = raw
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = None
    if accumulator is not None:
        # The bytes are already in hand, so this is a parse and not a copy.
        _guarded(accumulator.observe_body, raw, what="usage accounting")
    #: The body as it currently stands, through both edits below. ``out`` is
    #: only re-serialised when this object stops being the one ``raw``
    #: decoded to, which is what keeps an untouched response byte-identical.
    body_obj: Any = payload
    normalised = _guarded(
        normalise_vendor_response,
        payload,
        gateway.id_map(vendor),
        what="vendor response normalisation",
    ) if isinstance(payload, dict) else None
    failed = normalised is _ABANDON
    if normalised is not None and not failed:
        patched, stats = normalised
        if stats.changed:
            _log_repair(vendor.vendor_id, stats)
            body_obj = patched
            out = json.dumps(patched, **COMPACT_JSON).encode("utf-8")
    note = " note=rewrite_failed" if failed else ""
    if (
        facts is not None
        and facts.count_tokens
        and 200 <= upstream.status < 300
    ):
        guarded = _guarded(
            guard_count_tokens,
            body_obj,
            facts.count_estimate,
            what="count_tokens zero-guard",
        )
        if guarded is not _ABANDON:
            labelled, substituted = guarded
            if labelled is not body_obj:
                body_obj = labelled
                out = json.dumps(labelled, **COMPACT_JSON).encode("utf-8")
            if substituted:
                note = f"{note} note=count_tokens_estimated"
                _guarded(
                    note_count_substitution,
                    vendor.vendor_id,
                    decision.forward_model if decision is not None else "-",
                    what="count_tokens substitution notice",
                )
    # Relaying the vendor's own bytes is the fallback: a cosmetic id costs
    # the NEXT request a repair, a 500 costs this one entirely.
    access(
        upstream.status,
        stream=False,
        extra=f"bytes={len(out)}{note}",
    )
    return web.Response(status=upstream.status, headers=relay, body=out)


async def hello_handler(request: web.Request) -> web.Response:
    """The client's preconnect probe: ``HEAD /api/hello``, answered 200 empty.

    Claude Code opens the connection and warms DNS/TCP with this before the
    first real call. Against ``api.anthropic.com`` it gets a 200; here it used
    to land on the catch-all 404, which the client counts as a failed
    endpoint check on every session start — a difference from native visible
    in the client's own diagnostics for no reason at all.

    No auth: it carries no information and reveals none. It is answered for
    GET as well, because a probe that guesses the verb should not get a
    different verdict.
    """
    return web.Response(status=200)


async def not_found_handler(request: web.Request) -> web.Response:
    return _json_error(
        404,
        "not_found_error",
        f"the model gateway has no route for {request.path!r}. It serves "
        "/health, /usage, /usage/windows, /v1/models, /v1/messages and "
        "/v1/messages/count_tokens.",
    )


def create_app(
    config: GatewayConfig,
    *,
    vendors: Mapping[str, Vendor] = VENDORS,
    anthropic: AnthropicFamily = ANTHROPIC_FAMILY,
    oauth_reader: Optional[OAuthReader] = None,
    key_resolver: Optional[VendorKeyResolver] = None,
    token_permissions: OwnerOnlyState = "unknown",
    token_file_stamp: Optional[Mapping[str, object]] = None,
) -> web.Application:
    """Build the application. Pure: binds nothing, starts no I/O."""
    gateway = Gateway(
        config,
        vendors=vendors,
        anthropic=anthropic,
        oauth_reader=oauth_reader,
        key_resolver=key_resolver,
        token_permissions=token_permissions,
        token_file_stamp=token_file_stamp,
    )
    app = web.Application(
        middlewares=[loopback_only_middleware],
        client_max_size=UNBOUNDED_CLIENT_MAX_SIZE,
    )
    app[APP_KEY] = gateway

    _add_route(app, "GET", "/health", health_handler, name="health")
    _add_route(app, "GET", "/usage", usage_handler, name="usage")
    _add_route(
        app, "GET", "/usage/windows", usage_windows_handler, name="usage_windows",
    )
    _add_route(app, "HEAD", "/api/hello", hello_handler, name="hello_head")
    _add_route(app, "GET", "/api/hello", hello_handler, name="hello_get")
    _add_route(app, "GET", "/v1/models", models_handler, name="models")
    _add_route(app, "POST", "/v1/messages", messages_handler, name="messages")
    _add_route(
        app,
        "POST",
        "/v1/messages/count_tokens",
        messages_handler,
        name="count_tokens",
    )
    app.router.add_route("*", "/{tail:.*}", not_found_handler)

    async def _on_startup(_: web.Application) -> None:
        await gateway.start()

    async def _on_cleanup(_: web.Application) -> None:
        await gateway.stop()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


__all__ = [
    "APP_KEY",
    "COUNT_TOKENS_PATH",
    "RETRYABLE_HEADERS",
    "REWRITE_BUFFER_NOTE",
    "SCOPE_PROBE_TASK_KEY",
    "UNBOUNDED_CLIENT_MAX_SIZE",
    "Gateway",
    "RequestFacts",
    "create_app",
    "loopback_only_middleware",
    "usage_handler",
    "usage_windows_handler",
]
