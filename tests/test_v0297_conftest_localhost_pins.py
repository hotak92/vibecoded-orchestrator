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


# ── fix round 3 (item 8c): a leaked pin key fails the TEST THAT LEAKED IT ───
#
# The pin used to be re-established silently after every test, so a leaked
# GRPC_PORT never failed its own test — only a later victim, order-dependent
# (review B-LOW-2: "Weaviate's HTTP and gRPC ports must differ" in a test
# that never touches ports). The guard now names the leaker and restores.

import subprocess  # noqa: E402 — used by the child-pytest wiring below

from tests import conftest as _conftest  # noqa: E402
from tests.common.child_env import child_env  # noqa: E402


def test_the_pin_leak_helper_is_pure_and_names_the_key(monkeypatch) -> None:
    """ACT: a changed key is named with both spellings; LEAVE-ALONE: pins
    in place report nothing."""
    assert _conftest._localhost_pin_leak("X::test_a") is None
    monkeypatch.setenv("GRPC_PORT", "50052")
    leak = _conftest._localhost_pin_leak("X::test_a")
    assert leak is not None and "GRPC_PORT: '50052' (pin '9')" in leak
    assert "X::test_a left a pinned localhost-backend env key" in leak
    assert "mock.patch.dict" in leak


def _run_child(tmp_path: Path, name: str, body: str) -> str:
    (tmp_path / name).write_text(body, encoding="utf-8")
    env = child_env()
    env.pop("VCO_TEST_ALLOW_REAL_STATE", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "tests.conftest", str(tmp_path / name)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
        timeout=300,
    )
    return f"rc={proc.returncode}\n{proc.stdout}{proc.stderr}"


def test_the_guard_fails_a_test_that_leaks_grpc_port(tmp_path: Path) -> None:
    """Red before fix round 3: the leak was silently re-pinned and a LATER
    test failed instead. Now the leaking test is failed by name and the
    next test sees the pin again."""
    out = _run_child(tmp_path, "test_grpc_leak_probe.py", (
        "import os\n"
        "def test_a_leaks():\n    os.environ['GRPC_PORT'] = '50052'\n"
        "def test_b_sees_the_pin():\n    assert os.environ['GRPC_PORT'] == '9'\n"
        "def test_c_monkeypatch_is_fine(monkeypatch):\n"
        "    monkeypatch.setenv('WEAVIATE_GRPC_PORT', '50053')\n"
    ))
    assert "rc=0" not in out, out[-2000:]
    assert ("LocalhostPinLeak" in out
            and "test_a_leaks left a pinned localhost-backend env key"
            in out), out[-2000:]
    assert "GRPC_PORT: '50052' (pin '9')" in out, out[-2000:]
    # The guard restores after reporting: the next test sees the pin, and a
    # monkeypatched key is no leak.
    assert "3 passed, 1 error" in out, out[-800:]
