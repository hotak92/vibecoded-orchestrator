# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.77 Part 9 task 6 — Ollama keep_alive on embed requests.

Ollama's default keep_alive is ~5 min; after any idle gap the next embed pays
a ~1.9 s model reload (hook-latency audit 2026-07-11). Sending keep_alive on
every embed request pins the model resident. These tests pin:

  - _keep_alive() default is "24h"
  - VCO_OLLAMA_KEEP_ALIVE overrides the value
  - an EMPTY VCO_OLLAMA_KEEP_ALIVE is an explicit opt-out (no field sent)
  - _with_keep_alive adds the field (default) and is a no-op on opt-out
  - the actual embed request body carries keep_alive (integration via a mock
    session), and honors the opt-out
  - the shipped compose.yaml default is no longer the eviction-prone 30s
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from vco_lib.embedding_providers import ollama as ollama_mod

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_env():
    saved = os.environ.get("VCO_OLLAMA_KEEP_ALIVE")
    if "VCO_OLLAMA_KEEP_ALIVE" in os.environ:
        del os.environ["VCO_OLLAMA_KEEP_ALIVE"]
    yield
    if saved is None:
        os.environ.pop("VCO_OLLAMA_KEEP_ALIVE", None)
    else:
        os.environ["VCO_OLLAMA_KEEP_ALIVE"] = saved


def test_keep_alive_default_is_24h():
    assert ollama_mod._keep_alive() == "24h"


def test_keep_alive_env_override():
    os.environ["VCO_OLLAMA_KEEP_ALIVE"] = "2h"
    assert ollama_mod._keep_alive() == "2h"


def test_keep_alive_env_never_evict():
    os.environ["VCO_OLLAMA_KEEP_ALIVE"] = "-1"
    assert ollama_mod._keep_alive() == "-1"


def test_empty_env_is_opt_out():
    os.environ["VCO_OLLAMA_KEEP_ALIVE"] = ""
    assert ollama_mod._keep_alive() is None


def test_with_keep_alive_adds_field_by_default():
    body = ollama_mod._with_keep_alive({"model": "m", "input": "x"})
    assert body["keep_alive"] == "24h"
    # original-shape keys preserved
    assert body["model"] == "m" and body["input"] == "x"


def test_with_keep_alive_noop_on_opt_out():
    os.environ["VCO_OLLAMA_KEEP_ALIVE"] = ""
    body = ollama_mod._with_keep_alive({"model": "m"})
    assert "keep_alive" not in body


def test_with_keep_alive_does_not_mutate_caller_dict():
    src = {"model": "m"}
    ollama_mod._with_keep_alive(src)
    assert "keep_alive" not in src, "must not mutate the caller's dict"


class _FakeResp:
    status_code = 200

    def json(self):
        return {"embeddings": [[0.1, 0.2, 0.3]]}

    @property
    def text(self):
        return ""


def test_embed_request_body_carries_keep_alive():
    """Integration: OllamaAdapter.embed sends keep_alive in the POST body."""
    captured = {}

    def _fake_bounded_post(session, url, json=None, timeout=None):  # noqa: A002
        captured["json"] = json
        return _FakeResp()

    with mock.patch.object(ollama_mod, "bounded_post", _fake_bounded_post):
        adapter = ollama_mod.OllamaAdapter("http://x:11435", session=mock.MagicMock())
        adapter.embed("qwen3-embedding:0.6b", "hello", num_ctx=8192)

    assert captured["json"].get("keep_alive") == "24h", captured["json"]


def test_embed_request_honors_opt_out():
    os.environ["VCO_OLLAMA_KEEP_ALIVE"] = ""
    captured = {}

    def _fake_bounded_post(session, url, json=None, timeout=None):  # noqa: A002
        captured["json"] = json
        return _FakeResp()

    with mock.patch.object(ollama_mod, "bounded_post", _fake_bounded_post):
        adapter = ollama_mod.OllamaAdapter("http://x:11435", session=mock.MagicMock())
        adapter.embed("qwen3-embedding:0.6b", "hello", num_ctx=8192)

    assert "keep_alive" not in captured["json"], captured["json"]


def test_compose_default_not_eviction_prone():
    """The shipped Ollama compose default must not be the 30s that evicts the
    embedding model between injections."""
    compose = (REPO_ROOT / "claude_mcp_servers" / "compose.yaml").read_text("utf-8")
    assert 'OLLAMA_KEEP_ALIVE: "30s"' not in compose, (
        "compose OLLAMA_KEEP_ALIVE reverted to the eviction-prone 30s default"
    )
    assert 'OLLAMA_KEEP_ALIVE: "24h"' in compose, (
        "compose OLLAMA_KEEP_ALIVE should pin the model resident (24h)"
    )
