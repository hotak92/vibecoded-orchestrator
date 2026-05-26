# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for VCThelpers.license.validator.

Covers the OSS-launch fail-open contract:
  - No license → free tier, no crash
  - Any non-200 from validate-tier → free tier
  - Network error → cached tier within grace, free past grace
  - Cached result inside grace window → no remote call
  - Mocked 200 success → tier from server
  - All key sources (arg, env, file) resolve correctly

Tests fully isolate filesystem state (HOME redirected per-test) and never
touch the real network — the urllib request is monkey-patched.
"""
from __future__ import annotations

import importlib
import io
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from unittest import mock

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_validator(tmp_path, monkeypatch):
    """Reload the validator module with HOME redirected to a tmp dir.

    Module-level constants (CACHE_DIR, KEY_FILE, …) capture Path.home() at
    import time, so we monkeypatch HOME *before* re-importing.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # Wipe any leaked tier-overrides / keys from the host env.
    for var in (
        "VIBECODED_TIER",
        "VIBECODED_LICENSE_KEY",
        "VIBECODED_LICENSE_URL",
    ):
        monkeypatch.delenv(var, raising=False)

    # Force re-import so module-level Path.home() picks up the new HOME.
    import VCThelpers.license.validator as v
    importlib.reload(v)
    # Belt-and-braces: also reset the in-process tier cache.
    v._cached_tier = None
    yield v
    # Best-effort teardown — tmp_path is cleaned up by pytest anyway.
    v._cached_tier = None


def _fake_http_response(status: int, body: dict) -> mock.MagicMock:
    """Build a context-manager-shaped mock that mimics urllib's response."""
    raw = json.dumps(body).encode()
    resp = mock.MagicMock()
    resp.read.return_value = raw
    resp.status = status
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


# ──────────────────────────────────────────────────────────────────────────────
# Key resolution
# ──────────────────────────────────────────────────────────────────────────────


class TestKeyResolution:
    def test_no_key_anywhere_returns_free(self, fresh_validator):
        result = fresh_validator.validate_license()
        assert result.tier == "free"
        assert result.valid is True
        assert "No license key" in result.message

    def test_env_var_takes_priority_over_file(self, fresh_validator, tmp_path, monkeypatch):
        # Drop a file key.
        key_dir = tmp_path / ".vct-secrets"
        key_dir.mkdir(parents=True)
        (key_dir / "license_key").write_text("FILE-KEY-UUID")
        # And an env key.
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "ENV-KEY-UUID")

        # Capture which key the validator hands to _remote_validate.
        seen: dict = {}

        def fake_remote(key, machine_hash):
            seen["key"] = key
            return None  # network failure → falls back to cache (none) → free

        monkeypatch.setattr(fresh_validator, "_remote_validate", fake_remote)
        fresh_validator.validate_license()
        assert seen["key"] == "ENV-KEY-UUID"

    def test_file_key_used_when_env_unset(self, fresh_validator, tmp_path, monkeypatch):
        key_dir = tmp_path / ".vct-secrets"
        key_dir.mkdir(parents=True)
        (key_dir / "license_key").write_text("  FILE-KEY-UUID\n")

        seen: dict = {}

        def fake_remote(key, machine_hash):
            seen["key"] = key
            return None

        monkeypatch.setattr(fresh_validator, "_remote_validate", fake_remote)
        fresh_validator.validate_license()
        assert seen["key"] == "FILE-KEY-UUID"  # whitespace stripped

    def test_explicit_arg_takes_priority(self, fresh_validator, monkeypatch):
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "ENV-KEY")
        seen: dict = {}

        def fake_remote(key, machine_hash):
            seen["key"] = key
            return None

        monkeypatch.setattr(fresh_validator, "_remote_validate", fake_remote)
        fresh_validator.validate_license(key="ARG-KEY")
        assert seen["key"] == "ARG-KEY"

    def test_tier_override_free_short_circuits(self, fresh_validator, monkeypatch):
        monkeypatch.setenv("VIBECODED_TIER", "free")
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "PRO-LOOKING-KEY")

        # Should NOT call remote.
        called = mock.MagicMock()
        monkeypatch.setattr(fresh_validator, "_remote_validate", called)

        result = fresh_validator.validate_license()
        assert result.tier == "free"
        called.assert_not_called()

    def test_tier_override_pro_is_ignored(self, fresh_validator, monkeypatch):
        """Env-claimed paid tiers are never trusted without a validated key."""
        monkeypatch.setenv("VIBECODED_TIER", "pro")  # malicious env
        # No key → falls through to "no key, free tier".
        result = fresh_validator.validate_license()
        assert result.tier == "free"


# ──────────────────────────────────────────────────────────────────────────────
# Remote validation outcomes
# ──────────────────────────────────────────────────────────────────────────────


class TestRemoteValidation:
    def test_invalid_key_returns_free(self, fresh_validator, monkeypatch):
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "BAD-UUID")

        err = urllib.error.HTTPError(
            url="x", code=401, msg="Unauthorized", hdrs={},
            fp=io.BytesIO(json.dumps({"message": "Invalid key."}).encode()),
        )
        with mock.patch.object(urllib.request, "urlopen", side_effect=err):
            result = fresh_validator.validate_license()
        assert result.tier == "free"
        assert result.valid is False
        assert "Invalid" in result.message

    def test_pro_response_returns_pro(self, fresh_validator, monkeypatch):
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "GOOD-UUID")

        resp = _fake_http_response(200, {
            "valid": True,
            "tier": "pro",
            "expires_at": "2027-04-01T00:00:00Z",
            "message": "Validated.",
        })
        with mock.patch.object(urllib.request, "urlopen", return_value=resp):
            result = fresh_validator.validate_license()
        assert result.tier == "pro"
        assert result.valid is True
        assert result.expires_at == "2027-04-01T00:00:00Z"

    def test_network_error_with_no_cache_falls_back_to_free(
        self, fresh_validator, monkeypatch
    ):
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "GOOD-UUID")

        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("dns failure"),
        ):
            result = fresh_validator.validate_license()
        assert result.tier == "free"

    def test_unknown_tier_coerced_to_free(self, fresh_validator, monkeypatch):
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "GOOD-UUID")
        resp = _fake_http_response(200, {"valid": True, "tier": "platinum"})
        with mock.patch.object(urllib.request, "urlopen", return_value=resp):
            result = fresh_validator.validate_license()
        assert result.tier == "free"

    def test_5xx_falls_back_to_cache(self, fresh_validator, monkeypatch):
        """5xx is treated as transient — caller falls back to cached result."""
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "GOOD-UUID")
        # Pre-seed the cache with a recent pro validation.
        cached = fresh_validator.LicenseResult(
            tier="pro",
            valid=True,
            expires_at="2027-01-01T00:00:00Z",
            last_validated_at=time.time() - 60,  # 1 min ago
            message="Validated.",
        )
        fresh_validator._save_cached(cached)

        err = urllib.error.HTTPError(
            url="x", code=503, msg="Bad Gateway", hdrs={}, fp=io.BytesIO(b""),
        )
        with mock.patch.object(urllib.request, "urlopen", side_effect=err):
            result = fresh_validator.validate_license()
        assert result.tier == "pro"
        assert "grace remaining" in (
            (fresh_validator.STATUS_FILE.read_text() if fresh_validator.STATUS_FILE.exists() else "")
        )

    def test_cache_beyond_grace_period_degrades_to_free(
        self, fresh_validator, monkeypatch
    ):
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "GOOD-UUID")
        # Cache is 4 days old → past 3-day grace.
        ancient = fresh_validator.LicenseResult(
            tier="pro",
            valid=True,
            last_validated_at=time.time() - (4 * 24 * 3600),
            message="Validated long ago.",
        )
        fresh_validator._save_cached(ancient)

        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            result = fresh_validator.validate_license()
        assert result.tier == "free"
        assert "grace" in result.message.lower() or "validation unavailable" in result.message.lower()

    def test_instance_limit_returns_free_with_message(self, fresh_validator, monkeypatch):
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "GOOD-UUID")
        resp = _fake_http_response(200, {
            "error": "instance_limit",
            "message": "Too many machines.",
        })
        with mock.patch.object(urllib.request, "urlopen", return_value=resp):
            result = fresh_validator.validate_license()
        assert result.tier == "free"
        assert result.valid is False
        assert "Too many machines." in result.message


# ──────────────────────────────────────────────────────────────────────────────
# Caching, no-network short-circuits
# ──────────────────────────────────────────────────────────────────────────────


class TestCaching:
    def test_successful_validation_writes_cache(self, fresh_validator, monkeypatch):
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "GOOD-UUID")
        resp = _fake_http_response(200, {
            "valid": True, "tier": "pro", "message": "ok",
        })
        with mock.patch.object(urllib.request, "urlopen", return_value=resp):
            fresh_validator.validate_license()

        assert fresh_validator.CACHE_FILE.exists()
        loaded = fresh_validator.LicenseResult.from_json(
            fresh_validator.CACHE_FILE.read_text()
        )
        assert loaded.tier == "pro"

    def test_machine_id_hash_is_stable_and_opaque(self, fresh_validator, monkeypatch):
        # Pin the input via the override so the test is host-agnostic.
        # The contract under test is shape + determinism, not host-id
        # resolution.
        monkeypatch.setenv("VCT_MACHINE_ID_OVERRIDE", "test-stable-and-opaque-fixture")
        a = fresh_validator._machine_id_hash()
        b = fresh_validator._machine_id_hash()
        assert a == b
        assert len(a) == 64  # sha256 hex
        # Should never leak raw host-id chars; it's a hex digest.
        assert all(c in "0123456789abcdef" for c in a)

    def test_machine_id_hash_uses_override_env_when_set(self, fresh_validator, monkeypatch):
        # v0.2.36: the VCT_MACHINE_ID_OVERRIDE env var fully replaces the
        # platform host-id source. Pinning a known input lets us assert
        # the *exact* hash, which locks the algorithm change from
        # "sha256(8-byte-MAC)" to "sha256(host-id-utf8)" — and verifies
        # the Python implementation matches the Rust mirror.
        import hashlib
        monkeypatch.setenv("VCT_MACHINE_ID_OVERRIDE", "vct-test-fixture-001")
        expected = hashlib.sha256(b"vct-test-fixture-001").hexdigest()
        assert fresh_validator._machine_id_hash() == expected

    def test_machine_id_hash_ignores_empty_override(self, fresh_validator, monkeypatch):
        # v0.2.36: a stray `export VCT_MACHINE_ID_OVERRIDE=` must NOT
        # silently change every machine to sha256(""). The real platform
        # source is consulted instead.
        import hashlib
        empty_hash = hashlib.sha256(b"").hexdigest()
        monkeypatch.setenv("VCT_MACHINE_ID_OVERRIDE", "")
        actual = fresh_validator._machine_id_hash()
        assert actual != empty_hash, "empty override leaked through as the host id"
        assert len(actual) == 64

    def test_machine_id_hash_uses_no_platform_sentinel_when_sources_fail(
        self, fresh_validator, monkeypatch
    ):
        # v0.2.36: when every platform source fails, the function MUST
        # still return a well-formed 64-char hex string (the
        # /rebind-admin-token regex requires `^[0-9a-f]{64}$`). We mock
        # the resolver to return None and verify the sentinel branch.
        import hashlib
        monkeypatch.delenv("VCT_MACHINE_ID_OVERRIDE", raising=False)
        monkeypatch.setattr(fresh_validator, "_read_platform_host_id", lambda: None)
        expected = hashlib.sha256(b"vct-no-platform-host-id-v0.2.36").hexdigest()
        assert fresh_validator._machine_id_hash() == expected

    def test_machine_id_hash_linux_branch_reads_etc_machine_id(
        self, fresh_validator, monkeypatch, tmp_path
    ):
        # v0.2.36: end-to-end test of the Linux branch — point the
        # Path() reader at a tmpdir-rooted /etc/machine-id and verify
        # the resulting hash matches sha256 of the fake content. Skips
        # gracefully on non-Linux hosts so the test suite stays portable.
        import platform as _platform
        if _platform.system() != "Linux":
            import pytest
            pytest.skip("Linux-branch test (current platform: %s)" % _platform.system())
        import hashlib
        # Pre-empt the read by intercepting Path.read_text via a focused
        # closure rather than touching the real /etc — the test must
        # not require root.
        fake_id = "abcdef0123456789abcdef0123456789"
        monkeypatch.setattr(
            fresh_validator,
            "_read_linux_machine_id",
            lambda: fake_id,
        )
        # Strip the override so we go through the real platform dispatcher.
        monkeypatch.delenv("VCT_MACHINE_ID_OVERRIDE", raising=False)
        expected = hashlib.sha256(fake_id.encode("utf-8")).hexdigest()
        assert fresh_validator._machine_id_hash() == expected


# ──────────────────────────────────────────────────────────────────────────────
# Public API: get_tier / require_tier / feature_enabled / license_status
# ──────────────────────────────────────────────────────────────────────────────


class TestPublicAPI:
    def test_get_tier_caches_within_process(self, fresh_validator, monkeypatch):
        # First call: no key → free.
        assert fresh_validator.get_tier() == "free"
        # Even if env changes, cached value sticks until force_refresh.
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "PRO-UUID")
        resp = _fake_http_response(200, {"valid": True, "tier": "pro"})
        with mock.patch.object(urllib.request, "urlopen", return_value=resp):
            assert fresh_validator.get_tier() == "free"           # cached
            assert fresh_validator.get_tier(force_refresh=True) == "pro"

    def test_require_tier(self, fresh_validator):
        assert fresh_validator.require_tier("free") is True
        assert fresh_validator.require_tier("pro") is False  # default is free

    def test_feature_enabled_unknown_feature_defaults_true(self, fresh_validator):
        assert fresh_validator.feature_enabled("hypothetical_future_feature") is True

    def test_feature_enabled_pro_feature_off_for_free(self, fresh_validator):
        assert fresh_validator.feature_enabled("rl_retrieval") is False

    def test_license_status_no_key(self, fresh_validator):
        s = fresh_validator.license_status()
        assert s["tier"] == "free"
        assert s["has_key"] is False
        assert s["key_source"] == "none"
        assert s["cached"] is False

    def test_license_status_env_key(self, fresh_validator, monkeypatch):
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "SOME-UUID")
        # Avoid hitting the network when get_tier() runs.
        monkeypatch.setattr(fresh_validator, "_remote_validate", lambda *a, **k: None)
        s = fresh_validator.license_status()
        assert s["has_key"] is True
        assert s["key_source"] == "env"

    def test_license_status_file_key(self, fresh_validator, tmp_path, monkeypatch):
        key_dir = tmp_path / ".vct-secrets"
        key_dir.mkdir(parents=True)
        (key_dir / "license_key").write_text("FILE-UUID")
        monkeypatch.setattr(fresh_validator, "_remote_validate", lambda *a, **k: None)
        s = fresh_validator.license_status()
        assert s["has_key"] is True
        assert s["key_source"] == "file"


# ──────────────────────────────────────────────────────────────────────────────
# Endpoint URL: must NOT leak internal infra
# ──────────────────────────────────────────────────────────────────────────────


class TestAdminTier:
    """Bug 33: admin tier end-to-end on the client side.

    The server-side decision (variant_id → admin) lives in the Deno tests
    at launcher/supabase/functions/_shared/variant_map_test.ts. Here we
    exercise the Python validator's behavior assuming the server returned
    tier=admin (i.e. it consulted LS_ADMIN_VARIANT_IDS and matched).
    """

    def test_admin_response_returns_admin_tier(self, fresh_validator, monkeypatch):
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "ADMIN-UUID")
        resp = _fake_http_response(200, {
            "valid": True,
            "tier": "admin",
            "expires_at": None,
            "message": "Validated.",
            "is_admin": True,
            "unlock_all_modules": True,
            "dev_features_enabled": True,
        })
        with mock.patch.object(urllib.request, "urlopen", return_value=resp):
            result = fresh_validator.validate_license()
        assert result.tier == "admin"
        assert result.valid is True

    def test_is_admin_true_when_server_classifies_admin(
        self, fresh_validator, monkeypatch
    ):
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "ADMIN-UUID")
        resp = _fake_http_response(200, {
            "valid": True,
            "tier": "admin",
            "is_admin": True,
            "message": "Validated.",
        })
        with mock.patch.object(urllib.request, "urlopen", return_value=resp):
            assert fresh_validator.get_tier(force_refresh=True) == "admin"
        assert fresh_validator.is_admin() is True

    def test_is_admin_false_for_pro(self, fresh_validator, monkeypatch):
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "PRO-UUID")
        resp = _fake_http_response(200, {"valid": True, "tier": "pro"})
        with mock.patch.object(urllib.request, "urlopen", return_value=resp):
            fresh_validator.get_tier(force_refresh=True)
        assert fresh_validator.is_admin() is False

    def test_is_admin_false_when_no_key(self, fresh_validator):
        # No license at all → free → not admin.
        assert fresh_validator.is_admin() is False

    def test_admin_satisfies_require_tier_enterprise(
        self, fresh_validator, monkeypatch
    ):
        """Admin is treated as a strict superset of enterprise."""
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "ADMIN-UUID")
        resp = _fake_http_response(200, {"valid": True, "tier": "admin"})
        with mock.patch.object(urllib.request, "urlopen", return_value=resp):
            fresh_validator.get_tier(force_refresh=True)
        assert fresh_validator.require_tier("enterprise") is True
        assert fresh_validator.require_tier("mao") is True
        assert fresh_validator.require_tier("pro") is True


class TestEndpointSafety:
    def test_default_url_is_https(self, fresh_validator):
        # Per the 2026-05-06 security review:
        # `.claude/context/supabase-license-security-review-2026-05-06.md`
        # — the prior "must contain vibecodedtools.it / must not contain
        # supabase.co" guards were defending against information disclosure
        # of the project ID, but the project ID is ALREADY publicly
        # disclosed in launcher/supabase/config.toml which ships in the AGPL
        # source repo. URL secrecy in the binary was theatre.
        # Real license-validation hardening (env-var allowlist, rate limit,
        # signed cache) is tracked separately as F14/F1/F15 hardening
        # tickets — URL secrecy doesn't help against any of them.
        url = fresh_validator._DEFAULT_VALIDATE_URL
        assert url.startswith("https://"), f"default URL must be HTTPS: {url}"

    def test_env_override_wins(self, fresh_validator, monkeypatch):
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "GOOD-UUID")
        monkeypatch.setenv("VIBECODED_LICENSE_URL", "https://example.test/v")

        seen: dict = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            return _fake_http_response(200, {"valid": True, "tier": "pro"})

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            fresh_validator.validate_license()
        assert seen["url"] == "https://example.test/v"

    def test_vct_validate_tier_url_env_override_wins(self, fresh_validator, monkeypatch):
        """The Rust launcher's env var (VCT_VALIDATE_TIER_URL) is honored as
        a fallback when VIBECODED_LICENSE_URL is unset. Reviewer A round-2
        flagged a Python-vs-Rust env-var divergence; this test pins the
        Python side to honor either name so a single env can drive both."""
        # Make sure the Python-only var is NOT set.
        monkeypatch.delenv("VIBECODED_LICENSE_URL", raising=False)
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "GOOD-UUID")
        monkeypatch.setenv("VCT_VALIDATE_TIER_URL", "https://staging.test/validate-tier")

        seen: dict = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            return _fake_http_response(200, {"valid": True, "tier": "pro"})

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            fresh_validator.validate_license()
        assert seen["url"] == "https://staging.test/validate-tier"

    def test_vibecoded_license_url_takes_precedence_over_vct_var(
        self, fresh_validator, monkeypatch
    ):
        """When BOTH env vars are set, VIBECODED_LICENSE_URL wins (the
        Python path's own historical name). This avoids surprise after
        users adopted VIBECODED_LICENSE_URL pre-launcher."""
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "GOOD-UUID")
        monkeypatch.setenv("VIBECODED_LICENSE_URL", "https://python.win/v")
        monkeypatch.setenv("VCT_VALIDATE_TIER_URL", "https://rust.lose/v")

        seen: dict = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            return _fake_http_response(200, {"valid": True, "tier": "pro"})

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            fresh_validator.validate_license()
        assert seen["url"] == "https://python.win/v"

    def test_default_url_uses_validate_tier_path(self, fresh_validator):
        """The default URL must point at /validate-tier (matches Rust launcher
        and Supabase function path). Reviewer A: prior default was /validate
        which 404s against the actual deployed edge function."""
        url = fresh_validator._DEFAULT_VALIDATE_URL
        assert url.endswith("/validate-tier"), (
            f"default URL must end with /validate-tier (matches Supabase "
            f"function source at launcher/supabase/functions/validate-tier/), "
            f"got {url!r}"
        )
