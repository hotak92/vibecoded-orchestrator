# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""W-TRANSPORT / W-OLLAMA / W-HUB-PORT (v0.2.97 R7): the conftest pins hold.

Companion of `tests/test_v0294_live_weaviate_optin_canary.py` for the four
localhost-default keys the flash-tests sweep found unpinned: the gRPC
transport (`GRPC_PORT` / `WEAVIATE_GRPC_PORT`, default 50052 = the LIVE
Weaviate gRPC), `OLLAMA_URL` (default 11435 = the LIVE Ollama) and
`VCT_HUB_PORT` (default 7700 = the LIVE hub). A test body asserting the pin
is the red-proof half: before the conftest block landed, none of these keys
was set and each resolver walked its live default.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vco_lib.fixture_class_guard import UNROUTABLE_SENTINEL_URL  # noqa: E402


def test_the_grpc_transport_is_pinned_to_the_discard_port() -> None:
    assert os.environ.get("GRPC_PORT") == "9"
    assert os.environ.get("WEAVIATE_GRPC_PORT") == "9"


def test_ollama_url_is_pinned_to_the_discard_sentinel() -> None:
    assert os.environ.get("OLLAMA_URL") == UNROUTABLE_SENTINEL_URL


def test_the_hub_port_is_pinned_suite_wide() -> None:
    assert os.environ.get("VCT_HUB_PORT") == "9"


def test_a_test_that_sets_its_own_value_overrides_the_pin(
    monkeypatch,
) -> None:
    """The pin is a default, not a cage: a test may point a key at its own
    fixture (monkeypatch restores the pin afterwards)."""
    monkeypatch.setenv("VCT_HUB_PORT", "8123")
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:8124")
    assert os.environ["VCT_HUB_PORT"] == "8123"
    assert os.environ["OLLAMA_URL"] == "http://127.0.0.1:8124"
