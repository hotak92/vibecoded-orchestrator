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
from typing import Any, Callable, Mapping, Optional

import aiohttp
from aiohttp import web

from . import __version__
from .auth import OAuthReader, token_matches
from .catalog import CatalogService, to_models_response
from .config import SERVICE_NAME, GatewayConfig
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
#: bytes and the relayed headers consistent.
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

#: How much of a vendor's quota-refusal body is read before it is replaced.
#: Bounded because the body is never relayed — it exists here only to find a
#: reset hint and to be logged at DEBUG, and an unbounded read of a body we
#: are going to discard is a denial-of-service surface for free.
_QUOTA_BODY_PEEK_BYTES = 64 * 1024


def _error_body(kind: str, message: str) -> dict:
    """Anthropic-shaped error envelope, so the client renders our text."""
    return {"type": "error", "error": {"type": kind, "message": message}}


def _json_error(status: int, kind: str, message: str) -> web.Response:
    return web.json_response(_error_body(kind, message), status=status)


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


async def health_handler(request: web.Request) -> web.Response:
    """Liveness. Cached state plus one ``stat``; never blocks.

    Emitted fields — this list and the code below are kept in step by
    ``test_health_reports_exactly_the_documented_fields``:

    ``ok`` (bool), ``service``, ``version``, ``port``, ``host``,
    ``catalog_source`` (family -> ``live``/``static``/``unfetched``/
    ``unavailable``),
    ``context_table_source``, ``context_table_path``, ``oauth_present``
    (bool), ``oauth_state`` (``present``/``expired``/``absent``/
    ``unreadable``), ``vendors``, ``vendor_keys_cached``,
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


async def messages_handler(request: web.Request) -> web.StreamResponse:
    """Proxy ``/v1/messages`` and ``/v1/messages/count_tokens``."""
    gateway: Gateway = request.app[APP_KEY]
    # Taken BEFORE the auth check, so every outcome below — including the
    # two that answer without reading the body — measures the same thing:
    # elapsed since this request reached the handler.
    started = time.monotonic()
    if not gateway.authorised(request):
        _log_unauthorised(gateway, request, started=started)
        return _unauthorised()

    raw = await request.read()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("body is not a JSON object")
    except ValueError as exc:
        return _json_error(
            400, "invalid_request_error", f"unreadable request body ({exc})",
        )

    requested_model = payload.get("model") or ""
    stream_requested = bool(payload.get("stream"))
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
    forward_payload: dict = payload
    mutated = False
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
        forward_payload, repair = sanitise_for_anthropic(forward_payload)
        if repair.changed:
            mutated = True
            logger.info(
                "model-gateway: repaired inherited tool blocks before the "
                "first-party route (%s) at message index(es) %s",
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
        forward_payload, restored = restore_vendor_ids(
            forward_payload, gateway.id_map(vendor),
        )
        if restored:
            mutated = True

    headers["Content-Type"] = "application/json"
    headers.setdefault("anthropic-version", DEFAULT_ANTHROPIC_VERSION)

    # The routed name must be in the BYTES, not only in the router's copy.
    if decision.forward_model != requested_model:
        forward_payload = dict(forward_payload)
        forward_payload["model"] = decision.forward_model
        mutated = True
    body = json.dumps(forward_payload).encode("utf-8") if mutated else raw

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
    )


async def _proxy(
    request: web.Request,
    gateway: Gateway,
    decision: Route,
    url: str,
    headers: dict[str, str],
    body: bytes,
    *,
    requested_model: str = "",
    stream_requested: bool = False,
    started: Optional[float] = None,
) -> web.StreamResponse:
    """Forward one request upstream and relay the answer.

    On a stream, the SSE rewriter's held tail is flushed after the last
    upstream chunk — except on the client-disconnect path, where it is
    deliberately dropped: there is no longer a client to deliver it to, and
    writing to a closed transport would replace a routine "user hit Esc" with
    an exception in the log.
    """
    started = time.monotonic() if started is None else started
    route_label = _route_label(decision)

    def access(status: int, *, stream: bool, extra: str = "") -> None:
        logger.info(
            _access_line(
                requested=requested_model or decision.forward_model,
                route=route_label,
                forward=decision.forward_model,
                status=status,
                started=started,
                stream=stream,
                extra=extra,
            )
        )

    try:
        upstream_ctx = gateway.session.post(
            url,
            data=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=gateway.config.upstream_timeout_s),
        )
    except RuntimeError as exc:  # session not started — a programming error
        logger.error("model-gateway: %s", exc)
        access(502, stream=stream_requested)
        return _json_error(502, "api_error", str(exc))

    try:
        async with upstream_ctx as upstream:
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
            try:
                async for chunk in upstream.content.iter_any():
                    out = rewriter.feed(chunk) if rewriter is not None else chunk
                    if out:
                        await response.write(out)
                        total += len(out)
                if rewriter is not None:
                    tail = rewriter.flush()
                    if tail:
                        await response.write(tail)
                        total += len(tail)
            except (
                ConnectionResetError,
                ConnectionAbortedError,
                aiohttp.ClientConnectionError,
            ):
                # The CLIENT went away mid-stream (closed a tab, hit Esc).
                # That is routine, not an error: log it and stop writing.
                access(
                    upstream.status,
                    stream=is_stream,
                    extra=f"bytes={total} note=client_disconnected",
                )
                return response
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # The UPSTREAM broke mid-stream.
                logger.warning(
                    "model-gateway: %s -> %s stream ended early after %dB: %s",
                    decision.forward_model, decision.family_id, total, exc,
                )
                access(
                    upstream.status,
                    stream=is_stream,
                    extra=f"bytes={total} note=upstream_ended_early",
                )
                return response
            await response.write_eof()
            if rewriter is not None:
                _log_repair(
                    decision.family_id, rewriter.stats, rewriter.blocks_suppressed,
                )
            access(upstream.status, stream=is_stream, extra=f"bytes={total}")
            return response
    except asyncio.TimeoutError:
        logger.warning(
            "model-gateway: %s -> %s timed out after %ss",
            decision.forward_model, decision.family_id,
            gateway.config.upstream_timeout_s,
        )
        access(502, stream=stream_requested, extra="note=upstream_timeout")
        return _json_error(
            502,
            "api_error",
            f"upstream {decision.upstream} did not answer within "
            f"{gateway.config.upstream_timeout_s}s",
        )
    except aiohttp.ClientError as exc:
        logger.warning(
            "model-gateway: %s -> %s unreachable: %s",
            decision.forward_model, decision.family_id, exc,
        )
        access(502, stream=stream_requested, extra="note=upstream_unreachable")
        return _json_error(
            502, "api_error", f"upstream {decision.upstream} unreachable: {exc}",
        )


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
    return web.json_response(body, status=CLIENT_QUOTA_STATUS)


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
    if isinstance(payload, dict):
        patched, stats = normalise_vendor_response(payload, gateway.id_map(vendor))
        if stats.changed:
            _log_repair(vendor.vendor_id, stats)
            out = json.dumps(patched, ensure_ascii=False).encode("utf-8")
    access(upstream.status, stream=False, extra=f"bytes={len(out)}")
    return web.Response(status=upstream.status, headers=relay, body=out)


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
    app = web.Application(middlewares=[loopback_only_middleware])
    app[APP_KEY] = gateway

    _add_route(app, "GET", "/health", health_handler, name="health")
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


__all__ = ["APP_KEY", "Gateway", "create_app", "loopback_only_middleware"]
