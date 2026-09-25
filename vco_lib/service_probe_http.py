# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE way VCO opens an HTTP probe of a service endpoint (v0.2.97).

A probe asks one question — "does *this* endpoint answer as Weaviate /
Ollama / code-embed?" — so it must never be answered by some OTHER endpoint.
``urllib.request.urlopen`` follows 3xx redirects by default: an unrelated app
on 8080 (a dev proxy, a login page) answering ``302 https://elsewhere/…``
would make the probe leave the endpoint it was asked about — off loopback,
through whatever proxy the session exports — and read that answer as the
service's. Every service probe (the detector's ``default_fetch``, the
lifecycle's JSON health fetch, the adoption health check) opens its URL here
instead: a redirect is not followed, it surfaces as the 3xx
:class:`urllib.error.HTTPError` the callers already treat as "answers, but
not as the service".

The launcher/hub probe clients hold the same rule in Rust
(``vct_launcher_core::services::probe_http``).

A probe of THIS machine also never goes through a proxy (v0.2.97, R8 G10 —
the Python half of the launcher's R7b F19): urllib reads ``HTTP_PROXY`` and,
unlike a browser, does not exempt ``127.0.0.1`` / ``localhost`` on its own,
so with a proxy exported a loopback probe was answered by the proxy. A URL
whose host is loopback (:func:`is_loopback_host`) is opened without one; any
other host keeps the environment's proxy (it may be the only route to it).
"""
from __future__ import annotations

import ipaddress
import urllib.parse
import urllib.request
from typing import Any

__all__ = ["is_loopback_host", "is_loopback_url", "open_probe"]


def is_loopback_host(host: str) -> bool:
    """Is *host* a loopback spelling (this machine)? ``localhost`` in any case
    (loopback by RFC 6761), any ``127.0.0.0/8`` address, ``::1`` with or
    without brackets, and the empty host (the rows' compiled default). Pure:
    no name is resolved — a name that merely RESOLVES to loopback
    (``localhost.localdomain``, an ``/etc/hosts`` alias) is not loopback here,
    so the answer never depends on the resolver at the moment of the call.

    MUST MATCH ``vct_launcher_core::services::service_endpoints::is_loopback_host``
    (Rust); ``tests/test_v0297_r8_g10_probe_loopback.py`` runs that function's
    own test table against this one."""
    bare = (host or "").strip().lstrip("[").rstrip("]")
    if not bare:
        return True
    if bare.isascii() and bare.lower() == "localhost":
        return True
    if "%" in bare:  # a zone id: Rust's address parser rejects it
        return False
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        return ip == ipaddress.IPv6Address("::1")  # Rust: Ipv6Addr::is_loopback
    return ip.is_loopback


def is_loopback_url(url: str) -> bool:
    """Is *url*'s host this machine (:func:`is_loopback_host`)? A URL with no
    host (or that does not parse) is not."""
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        return False
    return bool(host) and is_loopback_host(host or "")


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Returning ``None`` from ``redirect_request`` makes urllib raise the
    3xx as an ``HTTPError`` instead of issuing a second request."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any,
                         newurl: str) -> None:
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirects())
#: For a loopback URL: no proxy (an empty ProxyHandler replaces the
#: environment's), no redirects.
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirects())


def open_probe(url: str, timeout: float) -> Any:
    """``urlopen(url, timeout=…)`` without following redirects, and without a
    proxy when *url* is loopback. Same return value and exceptions as
    ``urlopen`` (a 3xx raises ``HTTPError``)."""
    opener = _LOOPBACK_OPENER if is_loopback_url(url) else _OPENER
    return opener.open(url, timeout=timeout)  # noqa: S310 - service probe; scheme checked by callers
