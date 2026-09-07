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
key resolvable).

Logging policy: never a body, never a credential. One line per request with
model, family, status, stream flag, byte count and elapsed time.
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
from .config import GatewayConfig
from .context_table import ContextTableLoader
from .fileperms import OwnerOnlyState
from .routing import Route, RouteError, route as route_model
from .secrets import VendorKeyResolver
from .vendors import (
    ANTHROPIC_FAMILY,
    DEFAULT_ANTHROPIC_VERSION,
    OAUTH_BETA,
    VENDORS,
    AnthropicFamily,
    Vendor,
    validate_registry,
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


def _error_body(kind: str, message: str) -> dict:
    """Anthropic-shaped error envelope, so the client renders our text."""
    return {"type": "error", "error": {"type": kind, "message": message}}


def _json_error(status: int, kind: str, message: str) -> web.Response:
    return web.json_response(_error_body(kind, message), status=status)


def _peer_is_loopback(request: web.Request) -> Optional[bool]:
    """True / False / None when the peer address cannot be determined."""
    transport = request.transport
    if transport is None:
        return None
    peer = transport.get_extra_info("peername")
    if not peer:
        return None
    host = peer[0] if isinstance(peer, (tuple, list)) else peer
    if not isinstance(host, str):
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
            "service": "vct-model-gateway",
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
    if not gateway.authorised(request):
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
    if not gateway.authorised(request):
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
    decision = route_model(requested_model, gateway.vendors, gateway.anthropic)
    if isinstance(decision, RouteError):
        logger.info(
            "model-gateway: refused model %r (%s)",
            requested_model, decision.reason,
        )
        return _json_error(decision.status, "invalid_request_error", decision.message)

    headers = gateway.forward_headers(request)
    if decision.is_anthropic:
        oauth = gateway.oauth.read()
        if oauth.token is None:
            return _json_error(401, "authentication_error", oauth.problem or "")
        headers["Authorization"] = f"Bearer {oauth.token}"
        beta = headers.get("anthropic-beta", "")
        if OAUTH_BETA not in beta:
            headers["anthropic-beta"] = f"{beta},{OAUTH_BETA}" if beta else OAUTH_BETA
    else:
        vendor = decision.vendor
        assert vendor is not None  # noqa: S101 — guaranteed by Route
        key_result = await gateway.keys.aresolve(vendor)
        if not key_result.key:
            return _json_error(
                503, "api_error", key_result.problem or "no vendor key available",
            )
        headers[vendor.auth_header] = f"{vendor.auth_scheme}{key_result.key}"

    headers["Content-Type"] = "application/json"
    headers.setdefault("anthropic-version", DEFAULT_ANTHROPIC_VERSION)

    # The routed name must be in the BYTES, not only in the router's copy.
    body = raw
    if decision.forward_model != requested_model:
        payload["model"] = decision.forward_model
        body = json.dumps(payload).encode("utf-8")

    # Canonical path, i.e. the trailing slash a lenient client may have sent
    # is not passed upstream. Query string IS passed through untouched.
    canonical_path = request.path.rstrip("/") or request.path
    url = f"{decision.upstream}{canonical_path}"
    if request.query_string:
        url = f"{url}?{request.query_string}"

    return await _proxy(request, gateway, decision, url, headers, body)


async def _proxy(
    request: web.Request,
    gateway: Gateway,
    decision: Route,
    url: str,
    headers: dict[str, str],
    body: bytes,
) -> web.StreamResponse:
    started = time.monotonic()
    try:
        upstream_ctx = gateway.session.post(
            url,
            data=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=gateway.config.upstream_timeout_s),
        )
    except RuntimeError as exc:  # session not started — a programming error
        logger.error("model-gateway: %s", exc)
        return _json_error(502, "api_error", str(exc))

    try:
        async with upstream_ctx as upstream:
            relay = gateway.relay_headers(upstream)
            content_type = upstream.headers.get("Content-Type", "application/json")
            is_stream = _STREAM_CHUNK_HINT in content_type

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
                    await response.write(chunk)
                    total += len(chunk)
            except (
                ConnectionResetError,
                ConnectionAbortedError,
                aiohttp.ClientConnectionError,
            ):
                # The CLIENT went away mid-stream (closed a tab, hit Esc).
                # That is routine, not an error: log it and stop writing.
                logger.info(
                    "model-gateway: %s -> %s client disconnected after %dB",
                    decision.forward_model, decision.family_id, total,
                )
                return response
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # The UPSTREAM broke mid-stream.
                logger.warning(
                    "model-gateway: %s -> %s stream ended early after %dB: %s",
                    decision.forward_model, decision.family_id, total, exc,
                )
                return response
            await response.write_eof()
            logger.info(
                "model-gateway: %s -> %s HTTP %d%s %dB %.1fs",
                decision.forward_model,
                decision.family_id,
                upstream.status,
                " stream" if is_stream else "",
                total,
                time.monotonic() - started,
            )
            return response
    except asyncio.TimeoutError:
        logger.warning(
            "model-gateway: %s -> %s timed out after %ss",
            decision.forward_model, decision.family_id,
            gateway.config.upstream_timeout_s,
        )
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
        return _json_error(
            502, "api_error", f"upstream {decision.upstream} unreachable: {exc}",
        )


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
