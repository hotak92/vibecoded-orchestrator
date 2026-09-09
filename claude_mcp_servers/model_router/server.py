# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The aiohttp application: one Anthropic-shaped endpoint, two model families.

Routes (each also served with a trailing slash — see :func:`_add_route`)::

    GET  /health                    liveness; no auth, no blocking work
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

Bodies are otherwise not read, with one further exception in the same spirit:
on a vendor route the tool ids in the response are normalised
(:mod:`model_router.tool_ids`), because a vendor's ``call_…`` id in a
``server_tool_use`` block kills every LATER Anthropic request in that session
and the transcript is append-only, so relaying it faithfully is relaying a
booby trap.

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
is what the CLIENT asked for. That field is the one the field incident could
not answer: the gateway logged only the forwarded name, so a session that
silently changed models left no evidence of what had been selected.

"Every request" includes the ones refused at the door. A 401 used to return
before any line was written, which made an unauthenticated probe the single
terminal outcome the log was silent about — and it is the one a user asks
about ("the gateway is not answering me"). It is logged in the same shape,
rate-capped per peer (:data:`UNAUTHORISED_LOG_WINDOW_S`) because it is the
only line a caller WITHOUT the host token can cause.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
from typing import Any, AsyncIterator, Callable, Mapping, NamedTuple, Optional

import aiohttp
from aiohttp import web

from . import __version__
from .auth import OAuthReader, token_matches
from .catalog import CatalogService, to_models_response
from .config import SERVICE_NAME, UPSTREAM_CONNECT_TIMEOUT_S, GatewayConfig
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


#: What a GATEWAY-side 502 carries. Anthropic's SDKs read ``x-should-retry``
#: to decide whether a status is worth another attempt, and every 502 this
#: daemon writes is transient by construction — the upstream refused the
#: connection, or went quiet. Native sees a connection error there and
#: retries; without this header the same condition through the gateway is a
#: hard failure, which is the invariant ("never worse than native") breaking
#: on the most ordinary flake there is.
RETRYABLE_HEADERS = {"x-should-retry": "true"}


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
    ) -> None:
        validate_registry(vendors)
        self.config = config
        self.vendors = vendors
        self.anthropic = anthropic
        self.oauth = oauth_reader or OAuthReader(config.credentials_file)
        self.keys = key_resolver or VendorKeyResolver(
            project=config.secret_project, ttl_s=config.key_ttl_s,
        )
        self.context = ContextTableLoader(config.context_table_file)
        self.token_permissions = token_permissions
        self._session: Optional[aiohttp.ClientSession] = None
        #: Per-vendor rewritten-id -> vendor-id maps. Per PROCESS and bounded:
        #: the client echoes an id back one turn later, so the map only has to
        #: outlive a request, and an unbounded one would grow with uptime.
        self._id_maps: dict[str, BoundedIdMap] = {}
        #: peer -> (when its last 401 line was written, how many since).
        self._unauthorised_seen: dict[str, tuple[float, int]] = {}
        self.catalog = CatalogService(
            vendors=vendors,
            anthropic=anthropic,
            fetch_json=self._fetch_json,
            oauth_token=lambda: self.oauth.read().token,
            vendor_key=self._catalog_vendor_key,
            live_ttl_s=config.catalog_ttl_s,
            static_ttl_s=config.static_retry_ttl_s,
        )

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
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

    ``ok`` (bool), ``service``, ``version``, ``port``, ``host``,
    ``catalog_source`` (family -> ``live``/``static``/``unfetched``/
    ``unavailable``),
    ``context_table_source``, ``context_table_path``, ``oauth_present``
    (bool), ``oauth_state`` (``present``/``expired``/``absent``/
    ``unreadable``), ``oauth_expires_in_s`` (int seconds, negative once past,
    ``null`` when the file states no expiry), ``vendors``,
    ``vendor_keys_cached``,
    ``token_file_permissions`` (``owner_only``/``broader``/``unknown``,
    sampled at startup — probing it here would shell out on Windows).
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
            "port": gateway.config.port,
            "host": gateway.config.host,
            "catalog_source": gateway.catalog.sources(),
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
            "token_file_permissions": gateway.token_permissions,
        }
    )


async def models_handler(request: web.Request) -> web.Response:
    gateway: Gateway = request.app[APP_KEY]
    started = time.monotonic()
    if not gateway.authorised(request):
        # The SAME line as the messages route's refusal: leaving this one
        # silent would move the gap one endpoint along rather than close it,
        # and the picker's catalog call is the request a misconfigured
        # client makes FIRST.
        _log_unauthorised(gateway, request, started=started)
        return _unauthorised()
    table = gateway.context.current()
    entries, sources = await gateway.catalog.union(
        advertise_1m=table.advertise_1m,
    )
    logger.info(
        "model-gateway: /v1/models -> %d entries (%s)",
        len(entries),
        ", ".join(f"{k}={v}" for k, v in sorted(sources.items())),
    )
    return web.json_response(to_models_response(entries, sources))


def _route_label(decision: Route) -> str:
    """``anthropic`` or ``vendor:<id>`` — the field the access log turns on."""
    return "anthropic" if decision.is_anthropic else f"vendor:{decision.family_id}"


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
    return f"{line} {extra}" if extra else line


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
                "it to the first-party upstream unread", exc,
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
    canonical_path = request.path.rstrip("/") or request.path
    url = f"{decision.upstream}{canonical_path}"
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
    """
    started = time.monotonic() if started is None else started
    route_label = _route_label(decision)
    #: Set when a RESPONSE-side repair was abandoned (:func:`_guarded`). A
    #: plain local read by the closure below, because the failure can happen
    #: at any point of the relay while the line is written at the end — and
    #: one request must still mean one line.
    rewrite_failed = False

    def access(status: int, *, stream: bool, extra: str = "") -> None:
        parts = [part for part in (extra, note) if part]
        # Once, however many passes were abandoned: the handler may already
        # have put it in `note` for a REQUEST-side failure, and one line
        # saying the same thing twice reads as two events.
        if rewrite_failed and "note=rewrite_failed" not in note:
            parts.append("note=rewrite_failed")
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
            if vendor is not None and upstream.status in QUOTA_STATUSES:
                return await _quota_response(
                    upstream, vendor, stream_requested, access,
                )

            relay = gateway.relay_headers(upstream)
            content_type = upstream.headers.get("Content-Type", "application/json")
            is_stream = _STREAM_CHUNK_HINT in content_type

            if (
                vendor is not None
                and not is_stream
                and upstream.status not in (204, 304)
                and "json" in content_type.lower()
            ):
                return await _relay_vendor_json(
                    request, gateway, vendor, upstream, relay, access,
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
                            decision.forward_model, decision.family_id, total, exc,
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
            decision.forward_model, decision.family_id,
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
            decision.forward_model, decision.family_id, exc,
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


async def _quota_response(
    upstream: aiohttp.ClientResponse,
    vendor: Vendor,
    stream_requested: bool,
    access: Callable[..., None],
) -> web.StreamResponse:
    """Replace a vendor's quota refusal with one that names the vendor.

    The ONE documented exception to verbatim relay (see
    :mod:`model_router.quota` for the incident). The upstream body is read —
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
        raw.decode("utf-8", "replace"),
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
            out = json.dumps(patched, **COMPACT_JSON).encode("utf-8")
    # Relaying the vendor's own bytes is the fallback: a cosmetic id costs
    # the NEXT request a repair, a 500 costs this one entirely.
    extra = f"bytes={len(out)}"
    access(
        upstream.status,
        stream=False,
        extra=f"{extra} note=rewrite_failed" if failed else extra,
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
        "/health, /v1/models, /v1/messages and /v1/messages/count_tokens.",
    )


def create_app(
    config: GatewayConfig,
    *,
    vendors: Mapping[str, Vendor] = VENDORS,
    anthropic: AnthropicFamily = ANTHROPIC_FAMILY,
    oauth_reader: Optional[OAuthReader] = None,
    key_resolver: Optional[VendorKeyResolver] = None,
    token_permissions: OwnerOnlyState = "unknown",
) -> web.Application:
    """Build the application. Pure: binds nothing, starts no I/O."""
    gateway = Gateway(
        config,
        vendors=vendors,
        anthropic=anthropic,
        oauth_reader=oauth_reader,
        key_resolver=key_resolver,
        token_permissions=token_permissions,
    )
    app = web.Application(
        middlewares=[loopback_only_middleware],
        client_max_size=UNBOUNDED_CLIENT_MAX_SIZE,
    )
    app[APP_KEY] = gateway

    _add_route(app, "GET", "/health", health_handler, name="health")
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
    "RETRYABLE_HEADERS",
    "REWRITE_BUFFER_NOTE",
    "UNBOUNDED_CLIENT_MAX_SIZE",
    "Gateway",
    "create_app",
    "loopback_only_middleware",
]
