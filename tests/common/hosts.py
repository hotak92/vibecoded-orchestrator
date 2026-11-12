# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Exact host checks for tests that assert a text or a URL names a host.

``"api.anthropic.com" in text`` also passes for ``api.anthropic.com.evil.io``
or ``notapi.anthropic.community`` — a substring is not a host (CodeQL
py/incomplete-url-substring-sanitization). These helpers extract the
hostnames a text or URL actually names and compare them EXACTLY (or as the
domain itself / a subdomain of it).
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# A dotted hostname, not preceded or followed by another hostname character.
_HOSTNAME = re.compile(
    r"(?<![A-Za-z0-9.-])"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,})"
    r"(?![A-Za-z0-9-])"
)


def hostnames_in(text: str) -> set[str]:
    """Every dotted hostname ``text`` names, lower-cased."""
    return {m.group(1).lower() for m in _HOSTNAME.finditer(text or "")}


def in_domain(host: str, domain: str) -> bool:
    """``host`` IS ``domain`` or a subdomain of it (``wiki.x.org`` is in
    ``x.org``; ``evilx.org`` and ``x.org.evil.io`` are not)."""
    host, domain = (host or "").lower(), domain.lower()
    return host == domain or host.endswith("." + domain)


def names_host(text: str, host: str) -> bool:
    """``text`` names exactly ``host``."""
    return any(h == host.lower() for h in hostnames_in(text))


def names_domain(text: str, domain: str) -> bool:
    """``text`` names ``domain`` or one of its subdomains."""
    return any(in_domain(h, domain) for h in hostnames_in(text))


def url_host(url: str) -> str:
    """The host of ``url`` (lower-cased; ``""`` when it has none)."""
    return (urlsplit(url).hostname or "").lower()
