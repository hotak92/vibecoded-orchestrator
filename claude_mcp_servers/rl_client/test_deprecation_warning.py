# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for ``rl_client._deprecation_warning`` (v0.2.31).

Pins the env-driven banner contract for the module-deprecation surface
Layer 2 — see ``.claude/context/plans/rl-deprecation-warning-surface-spec-2026-05-23.md``.

These tests run with `pytest claude_mcp_servers/rl_client/` so they're
discovered by the per-package sweep + the project-level sweep.
"""
from __future__ import annotations

import os

import pytest

from rl_client.client import _deprecation_warning


# Each test fully owns the four env keys it cares about. The fixture
# guarantees they're cleared on entry + exit so test order does not
# leak state.
DEPRECATION_KEYS = (
    "VCT_RL_MODULE_DEPRECATED",
    "VCT_RL_MODULE_DEPRECATION_MESSAGE",
    "VCT_RL_MODULE_DEPRECATION_DATE",
    "VCT_RL_MODULE_DEPRECATION_URL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Strip every deprecation env key before each test; restored on teardown."""
    for k in DEPRECATION_KEYS:
        monkeypatch.delenv(k, raising=False)
    yield


def test_returns_none_when_env_unset():
    assert _deprecation_warning() is None


def test_returns_none_when_flag_is_not_exactly_1(monkeypatch):
    # The flag is treated strictly — "true", "yes", "TRUE" all return None.
    # Only the canonical "1" value flips the warning on. This matches the
    # launcher's writer (always writes "1") and prevents flaky env
    # detection from misclassifying e.g. a hand-typed "true".
    monkeypatch.setenv("VCT_RL_MODULE_DEPRECATED", "true")
    assert _deprecation_warning() is None
    monkeypatch.setenv("VCT_RL_MODULE_DEPRECATED", "yes")
    assert _deprecation_warning() is None
    monkeypatch.setenv("VCT_RL_MODULE_DEPRECATED", "0")
    assert _deprecation_warning() is None
    # Empty string is also off (a launcher strip leaves the env var
    # absent rather than empty, but defensive parsing).
    monkeypatch.setenv("VCT_RL_MODULE_DEPRECATED", "")
    assert _deprecation_warning() is None


def test_returns_default_banner_when_only_flag_set(monkeypatch):
    """With just the flag, fall back to the canonical default message."""
    monkeypatch.setenv("VCT_RL_MODULE_DEPRECATED", "1")
    out = _deprecation_warning()
    assert out is not None
    assert out.startswith("[DEPRECATION WARNING] ")
    assert "RL Reranker module is deprecated." in out
    # No EOL / migration phrases when those env vars are absent.
    assert "EOL:" not in out
    assert "Migration guide:" not in out


def test_includes_custom_message(monkeypatch):
    monkeypatch.setenv("VCT_RL_MODULE_DEPRECATED", "1")
    monkeypatch.setenv(
        "VCT_RL_MODULE_DEPRECATION_MESSAGE",
        "RL Reranker is being replaced by the v3 retrieval pipeline.",
    )
    out = _deprecation_warning()
    assert out is not None
    assert "RL Reranker is being replaced by the v3 retrieval pipeline." in out


def test_includes_eol_date_when_set(monkeypatch):
    monkeypatch.setenv("VCT_RL_MODULE_DEPRECATED", "1")
    monkeypatch.setenv("VCT_RL_MODULE_DEPRECATION_DATE", "2026-12-01")
    out = _deprecation_warning()
    assert out is not None
    assert "EOL: 2026-12-01." in out


def test_includes_migration_url_when_set(monkeypatch):
    monkeypatch.setenv("VCT_RL_MODULE_DEPRECATED", "1")
    monkeypatch.setenv(
        "VCT_RL_MODULE_DEPRECATION_URL",
        "https://docs.example.com/rl-migration",
    )
    out = _deprecation_warning()
    assert out is not None
    assert "Migration guide: https://docs.example.com/rl-migration" in out


def test_full_banner_assembly(monkeypatch):
    """Smoke test: all four env vars set → a single-line banner with all
    three pieces in canonical order."""
    monkeypatch.setenv("VCT_RL_MODULE_DEPRECATED", "1")
    monkeypatch.setenv(
        "VCT_RL_MODULE_DEPRECATION_MESSAGE",
        "RL Reranker is deprecated as of 2026-12-01.",
    )
    monkeypatch.setenv("VCT_RL_MODULE_DEPRECATION_DATE", "2026-12-01")
    monkeypatch.setenv(
        "VCT_RL_MODULE_DEPRECATION_URL",
        "https://docs.example.com/rl-migration",
    )
    out = _deprecation_warning()
    assert out is not None
    # Single-line invariant — Claude's hybrid_search response renders the
    # banner verbatim; embedded newlines would break formatting.
    assert "\n" not in out
    # Canonical key phrases all present.
    assert out.startswith("[DEPRECATION WARNING] ")
    assert "RL Reranker is deprecated as of 2026-12-01." in out
    assert "EOL: 2026-12-01." in out
    assert "Migration guide: https://docs.example.com/rl-migration" in out


def test_empty_optional_env_values_are_ignored(monkeypatch):
    """Setting DATE / URL to empty strings should not introduce empty
    phrases (`EOL: .` / `Migration guide: `). The launcher strips empty
    optional values; defensive parsing handles them anyway."""
    monkeypatch.setenv("VCT_RL_MODULE_DEPRECATED", "1")
    monkeypatch.setenv("VCT_RL_MODULE_DEPRECATION_DATE", "")
    monkeypatch.setenv("VCT_RL_MODULE_DEPRECATION_URL", "")
    out = _deprecation_warning()
    assert out is not None
    assert "EOL:" not in out
    assert "Migration guide:" not in out
