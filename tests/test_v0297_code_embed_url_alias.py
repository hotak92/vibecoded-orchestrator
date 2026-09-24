# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 (lane Y): the ``CODE_EMBED_URL`` client alias.

The projection emits both ``CODE_EMBED_URL`` and ``CODE_EMBED_SERVICE_URL``
(same value, by construction); until v0.2.97 the ONE client resolver
(``vco_lib.code_embed_image.service_base_url``) accepted only the latter, so
an environment that carried just the alias silently fell back to the
compiled default ``http://localhost:11440``. The order is now:

    explicit → ``CODE_EMBED_SERVICE_URL`` → ``CODE_EMBED_URL``
    → ``http://localhost:<CODE_EMBED_PORT|11440>``

Every live client leaf (the MCP ``embeddings`` module, the one-shot
migration script) resolves through the same home.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _no_url_env(monkeypatch):
    """Start every case from a clean URL env (port knob kept per-test)."""
    monkeypatch.delenv("CODE_EMBED_SERVICE_URL", raising=False)
    monkeypatch.delenv("CODE_EMBED_URL", raising=False)
    monkeypatch.delenv("CODE_EMBED_PORT", raising=False)


def test_alias_is_used_when_service_url_is_unset(monkeypatch):
    from vco_lib.code_embed_image import service_base_url

    monkeypatch.setenv("CODE_EMBED_URL", "http://alias-only:11441/")
    assert service_base_url() == "http://alias-only:11441"


def test_service_url_wins_over_the_alias(monkeypatch):
    from vco_lib.code_embed_image import service_base_url

    monkeypatch.setenv("CODE_EMBED_SERVICE_URL", "http://canonical:2")
    monkeypatch.setenv("CODE_EMBED_URL", "http://alias:1")
    assert service_base_url() == "http://canonical:2"


def test_an_empty_service_url_is_unset_not_a_value(monkeypatch):
    """Empty string must not shadow the alias (same rule as every leg)."""
    from vco_lib.code_embed_image import service_base_url

    monkeypatch.setenv("CODE_EMBED_SERVICE_URL", "   ")
    monkeypatch.setenv("CODE_EMBED_URL", "http://alias:3")
    assert service_base_url() == "http://alias:3"


def test_alias_beats_the_port_knob(monkeypatch):
    from vco_lib.code_embed_image import service_base_url

    monkeypatch.setenv("CODE_EMBED_URL", "http://alias:4")
    monkeypatch.setenv("CODE_EMBED_PORT", "12345")
    assert service_base_url() == "http://alias:4"


def test_port_knob_still_applies_with_no_url(monkeypatch):
    from vco_lib.code_embed_image import service_base_url

    monkeypatch.setenv("CODE_EMBED_PORT", "12345")
    assert service_base_url() == "http://localhost:12345"


def test_alias_value_gets_the_health_suffix_stripped(monkeypatch):
    from vco_lib.code_embed_image import service_base_url

    monkeypatch.setenv("CODE_EMBED_URL", "http://alias:5/health")
    assert service_base_url() == "http://alias:5"


def test_the_mcp_client_leaf_resolves_through_the_shared_home(monkeypatch):
    """``weaviate_mcp.embeddings.CODE_EMBED_SERVICE_URL`` comes from the ONE
    resolver — with only the alias set it must NOT answer the compiled
    default (it did before v0.2.97: it read ``CODE_EMBED_SERVICE_URL``
    alone, inline)."""
    monkeypatch.setenv("CODE_EMBED_URL", "http://alias-leaf:9")
    from vco_lib.code_embed_image import service_base_url
    from weaviate_mcp import embeddings

    embeddings = importlib.reload(embeddings)
    assert embeddings.CODE_EMBED_SERVICE_URL == "http://alias-leaf:9"
    assert embeddings.CODE_EMBED_SERVICE_URL == service_base_url()
