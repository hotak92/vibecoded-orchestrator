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
"""
from __future__ import annotations

import urllib.request
from typing import Any

__all__ = ["open_probe"]


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Returning ``None`` from ``redirect_request`` makes urllib raise the
    3xx as an ``HTTPError`` instead of issuing a second request."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any,
                         newurl: str) -> None:
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirects())


def open_probe(url: str, timeout: float) -> Any:
    """``urlopen(url, timeout=…)`` without following redirects. Same return
    value and exceptions as ``urlopen`` (a 3xx raises ``HTTPError``)."""
    return _OPENER.open(url, timeout=timeout)  # noqa: S310 - service probe; scheme checked by callers
