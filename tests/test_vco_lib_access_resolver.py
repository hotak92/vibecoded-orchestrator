# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for vco_lib.access_resolver (v0.2.49 Phase 8 item #21).

Pins the fail-open contract: hub unreachable / auth-failed / 404 /
malformed response must return "write" + emit a metric + log a
rate-limited WARNING. Closed-circuit policy would brick all KG writes
during a launcher restart; the fail-open contract is load-bearing for
UX.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock
import urllib.error

import pytest

from vco_lib import access_resolver


@pytest.fixture(autouse=True)
def isolate_warn_state():
    """Reset the rate-limit state between tests so warnings emit predictably."""
    access_resolver._WARN_STATE.clear()
    yield
    access_resolver._WARN_STATE.clear()


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point VCT_STATE_DIR at a tmp dir so metric/warn JSONLs don't pollute the real state."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def hub_token(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Write a hub.token file so the resolver doesn't bail on no-token."""
    token = "vct_admin_test_token_12345"
    (state_dir / "hub.token").write_text(token, encoding="utf-8")
    monkeypatch.delenv("VCT_HUB_TOKEN", raising=False)
    return token


# ─── Happy paths ────────────────────────────────────────────────────────

def test_hub_returns_write_level(state_dir: Path, hub_token: str):
    """Hub responds 200 {"level":"write"} → resolver returns "write"."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"level": "write"}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        result = access_resolver.check_access_level("p1", "MyKG")

    assert result == "write"
    # No fail-open emission for happy path.
    assert not (state_dir / "cache" / "dropped_writes.jsonl").exists()


def test_hub_returns_read_level(state_dir: Path, hub_token: str):
    """Hub responds 200 {"level":"read"} → resolver returns "read" (no fail-open)."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"level": "read"}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        result = access_resolver.check_access_level("p1", "MyKG")

    assert result == "read"
    assert not (state_dir / "cache" / "dropped_writes.jsonl").exists()


def test_hub_returns_none_level(state_dir: Path, hub_token: str):
    """Hub responds 200 {"level":"none"} → resolver returns "none"."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"level": "none"}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        result = access_resolver.check_access_level("p1", "MyKG")

    assert result == "none"


def test_is_write_allowed_convenience(state_dir: Path, hub_token: str):
    """is_write_allowed returns True only when level == 'write'."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"level": "write"}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        assert access_resolver.is_write_allowed("p1", "MyKG") is True

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"level": "read"}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        assert access_resolver.is_write_allowed("p1", "MyKG") is False


# ─── Fail-open contract ─────────────────────────────────────────────────

def test_fail_open_when_no_hub_token(state_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """No hub.token file + no VCT_HUB_TOKEN env → fail-open with 'no_hub_token' reason."""
    monkeypatch.delenv("VCT_HUB_TOKEN", raising=False)
    # No hub.token file in state_dir.

    result = access_resolver.check_access_level("p1", "MyKG")
    assert result == "write"
    # Metric emitted.
    metric_file = state_dir / "cache" / "dropped_writes.jsonl"
    assert metric_file.exists()
    rows = [json.loads(line) for line in metric_file.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["reason"] == "no_hub_token"
    assert rows[0]["fail_open"] is True
    assert rows[0]["project_id"] == "p1"
    assert rows[0]["collection"] == "MyKG"


def test_fail_open_when_hub_401(state_dir: Path, hub_token: str):
    """Hub returns 401 → fail-open with 'hub_auth_401' reason."""
    err = urllib.error.HTTPError(
        url="http://127.0.0.1:7700", code=401, msg="Unauthorized", hdrs=None, fp=None
    )
    with patch("urllib.request.urlopen", side_effect=err):
        result = access_resolver.check_access_level("p1", "MyKG")

    assert result == "write"
    rows = [
        json.loads(line)
        for line in (state_dir / "cache" / "dropped_writes.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert rows[0]["reason"] == "hub_auth_401"


def test_fail_open_when_hub_404(state_dir: Path, hub_token: str):
    """Hub returns 404 → fail-open with 'hub_404_no_row' (over-grant safer than block)."""
    err = urllib.error.HTTPError(
        url="http://127.0.0.1:7700", code=404, msg="Not Found", hdrs=None, fp=None
    )
    with patch("urllib.request.urlopen", side_effect=err):
        result = access_resolver.check_access_level("p1", "UnknownKG")

    assert result == "write"
    rows = [
        json.loads(line)
        for line in (state_dir / "cache" / "dropped_writes.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert rows[0]["reason"] == "hub_404_no_row"


def test_fail_open_when_hub_5xx(state_dir: Path, hub_token: str):
    """Hub returns 503 → fail-open with 'hub_5xx_503'."""
    err = urllib.error.HTTPError(
        url="http://127.0.0.1:7700", code=503, msg="Unavailable", hdrs=None, fp=None
    )
    with patch("urllib.request.urlopen", side_effect=err):
        result = access_resolver.check_access_level("p1", "MyKG")

    assert result == "write"
    rows = [
        json.loads(line)
        for line in (state_dir / "cache" / "dropped_writes.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert rows[0]["reason"] == "hub_5xx_503"


def test_fail_open_when_url_error(state_dir: Path, hub_token: str):
    """Hub connection refused (URLError) → fail-open."""
    err = urllib.error.URLError(ConnectionRefusedError("nope"))
    with patch("urllib.request.urlopen", side_effect=err):
        result = access_resolver.check_access_level("p1", "MyKG")

    assert result == "write"
    rows = [
        json.loads(line)
        for line in (state_dir / "cache" / "dropped_writes.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert rows[0]["reason"].startswith("url_error_")


def test_fail_open_when_malformed_json(state_dir: Path, hub_token: str):
    """Hub returns 200 with non-JSON body → fail-open."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"not json{{}"
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        result = access_resolver.check_access_level("p1", "MyKG")

    assert result == "write"
    rows = [
        json.loads(line)
        for line in (state_dir / "cache" / "dropped_writes.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert rows[0]["reason"] == "hub_malformed_json"


def test_fail_open_when_malformed_level_string(state_dir: Path, hub_token: str):
    """Hub returns 200 {"level": "invalid_level"} → fail-open (strict allowlist)."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"level": "admin"}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        result = access_resolver.check_access_level("p1", "MyKG")

    assert result == "write"
    rows = [
        json.loads(line)
        for line in (state_dir / "cache" / "dropped_writes.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert rows[0]["reason"] == "hub_malformed_level"


def test_fail_open_when_missing_project_id(state_dir: Path, hub_token: str):
    """Empty project_id → fail-open WITHOUT metric noise (no-context case)."""
    result = access_resolver.check_access_level("", "MyKG")
    assert result == "write"
    # No metric written for the no-context case.
    metric_file = state_dir / "cache" / "dropped_writes.jsonl"
    assert not metric_file.exists()


def test_fail_open_when_missing_collection(state_dir: Path, hub_token: str):
    """Empty collection name → fail-open WITHOUT metric noise."""
    result = access_resolver.check_access_level("p1", "")
    assert result == "write"
    metric_file = state_dir / "cache" / "dropped_writes.jsonl"
    assert not metric_file.exists()


# ─── Rate limiting ──────────────────────────────────────────────────────

def test_warnings_are_rate_limited(state_dir: Path, hub_token: str, caplog):
    """Repeated same-reason fail-opens within 5 min emit ONE WARNING."""
    import logging
    caplog.set_level(logging.WARNING, logger="vco.access_resolver")

    err = urllib.error.HTTPError(
        url="http://127.0.0.1:7700", code=503, msg="X", hdrs=None, fp=None
    )
    with patch("urllib.request.urlopen", side_effect=err):
        for _ in range(5):
            access_resolver.check_access_level("p1", "MyKG")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "expected exactly 1 WARNING due to rate-limit"


def test_warnings_bypassed_under_debug(
    state_dir: Path, hub_token: str, monkeypatch: pytest.MonkeyPatch, caplog
):
    """VCO_HOOK_DEBUG=1 bypasses the rate-limit (every emission warns)."""
    import logging
    monkeypatch.setenv("VCO_HOOK_DEBUG", "1")
    caplog.set_level(logging.WARNING, logger="vco.access_resolver")

    err = urllib.error.HTTPError(
        url="http://127.0.0.1:7700", code=503, msg="X", hdrs=None, fp=None
    )
    with patch("urllib.request.urlopen", side_effect=err):
        for _ in range(3):
            access_resolver.check_access_level("p1", "MyKG")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3, "VCO_HOOK_DEBUG=1 should bypass rate-limit"


def test_different_reasons_emit_separate_warnings(state_dir: Path, hub_token: str, caplog):
    """Different reason keys produce separate WARNINGs even within the 5-min window."""
    import logging
    caplog.set_level(logging.WARNING, logger="vco.access_resolver")

    err_5xx = urllib.error.HTTPError(
        url="http://127.0.0.1:7700", code=503, msg="X", hdrs=None, fp=None
    )
    err_404 = urllib.error.HTTPError(
        url="http://127.0.0.1:7700", code=404, msg="X", hdrs=None, fp=None
    )

    with patch("urllib.request.urlopen", side_effect=err_5xx):
        access_resolver.check_access_level("p1", "MyKG")
    with patch("urllib.request.urlopen", side_effect=err_404):
        access_resolver.check_access_level("p1", "MyKG")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2, f"expected 2 distinct-reason warnings, got {len(warnings)}"


# ─── v0.2.49 Step F SF8 (L4-S2) — log rotation ─────────────────────────────


def test_rotation_truncates_when_oversized(state_dir: Path, hub_token: str):
    """Pin SF8: when dropped_writes.jsonl exceeds 1 MiB, the next emit
    triggers a rotation that truncates the file to the most-recent 100
    rows. Pre-fix the file grew unbounded.
    """
    cache_dir = state_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    jsonl = cache_dir / "dropped_writes.jsonl"

    # Pre-seed >1 MiB of dummy rows. Empirically each row here is ~180
    # bytes (depends on the index padding); 7000 rows is comfortably
    # over the 1 MiB threshold.
    with jsonl.open("w", encoding="utf-8") as fh:
        for i in range(7000):
            fh.write(
                f'{{"ts":1700000000,"project_id":"p{i:06d}","collection":'
                f'"VeryLongCollectionNameToBlowUpTheLineSize_KnowledgeGraph_{i:06d}",'
                f'"reason":"hub_unreachable_simulated_for_size","fail_open":true}}\n'
            )
    pre_size = jsonl.stat().st_size
    assert pre_size > 1_048_576, f"pre-seed must be >1 MiB, got {pre_size}"

    # Trigger an emit (which calls _maybe_rotate_jsonl post-append).
    err = urllib.error.HTTPError(
        url="http://127.0.0.1:7700", code=503, msg="X", hdrs=None, fp=None
    )
    with patch("urllib.request.urlopen", side_effect=err):
        access_resolver.check_access_level("rotate_test_project", "RotateTestKG")

    # Post-emit: file truncated to ~100 rows (the tail-keep window).
    with jsonl.open("r", encoding="utf-8") as fh:
        lines = fh.readlines()
    assert (
        len(lines) <= 101  # 100 tail + 1 just-appended (in case append landed after rotate)
    ), f"expected <=101 lines post-rotation, got {len(lines)}"
    # The just-emitted row must survive rotation (it's the tail). Note:
    # the resolver writes via `json.dumps` which adds spaces around the
    # `:` separator by default — match the literal output.
    assert any(
        "rotate_test_project" in l for l in lines
    ), "the just-emitted row must survive rotation"


def test_rotation_no_op_when_under_threshold(state_dir: Path, hub_token: str):
    """Pin SF8 idempotence: small file is left untouched by the rotation
    check. The file must not be rewritten on every emit; that would be
    pointless I/O.
    """
    cache_dir = state_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    jsonl = cache_dir / "dropped_writes.jsonl"
    # Seed a few small rows.
    initial_content = '{"ts":1,"project_id":"a","collection":"K","reason":"r","fail_open":true}\n' * 10
    with jsonl.open("w", encoding="utf-8") as fh:
        fh.write(initial_content)

    # Call the rotation helper directly — must be no-op below threshold.
    access_resolver._maybe_rotate_jsonl(jsonl)

    # File content unchanged.
    with jsonl.open("r", encoding="utf-8") as fh:
        assert fh.read() == initial_content
