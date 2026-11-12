# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``tests/common/hosts.py``: exact host checks (the substring checks they
replace passed for look-alike hosts — CodeQL py/incomplete-url-substring-
sanitization)."""
from __future__ import annotations

from tests.common.hosts import hostnames_in, in_domain, names_domain, names_host, url_host


def test_names_host_is_exact():
    assert names_host("see https://api.anthropic.com/v1/models.", "api.anthropic.com")
    assert names_host("API.Anthropic.com is the endpoint", "api.anthropic.com")
    for lookalike in ("https://api.anthropic.com.evil.io/", "xapi.anthropic.com",
                      "api.anthropic.community", "api-anthropic.com"):
        assert not names_host(lookalike, "api.anthropic.com"), lookalike


def test_names_domain_takes_subdomains_only():
    assert names_domain("https://wiki.alpinelinux.org/wiki/Nvidia", "alpinelinux.org")
    assert names_domain("alpinelinux.org", "alpinelinux.org")
    assert not names_domain("https://evilalpinelinux.org/", "alpinelinux.org")
    assert not names_domain("https://alpinelinux.org.evil.io/", "alpinelinux.org")


def test_url_host_and_in_domain():
    assert url_host("https://api.anthropic.com/v1/models?x=1") == "api.anthropic.com"
    assert url_host("not a url") == ""
    assert in_domain("api.anthropic.com", "anthropic.com")
    assert not in_domain("anthropic.com.evil.io", "anthropic.com")
    assert hostnames_in("a.b.cd and D.ef, x.y") == {"a.b.cd", "d.ef"}
