# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for v0.2.40 L1 multi-key licensing (per-paid-module keys).

Covers the Python-side contract for the multi-key model:

  T1 — `is_module_licensed(module_id)` reads the per-module overlay in
       `~/.vibecoded/license_cache.json` without crashing on missing /
       malformed files. Returns True only for entries with a non-free
       `tier`.
  T2 — `feature_enabled(feature, module_id=...)` falls through to the
       per-module overlay when the orchestrator tier alone wouldn't
       satisfy the feature gate. A user on orchestrator tier=free with
       a per-module RL key unlocks `rl_retrieval`.
  T3 — Setting a key for module A's overlay entry does not affect
       module B's per-module state. The two are independent.

The fixture rebuilds the validator module per test with `HOME` redirected
to a tmp dir, so test cache writes never touch the developer's real
`~/.vibecoded/`.
"""
from __future__ import annotations

import importlib
import json
import time
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_validator(tmp_path, monkeypatch):
    """Reload the validator module with HOME redirected to a tmp dir.

    Mirrors the fixture in `test_license_validator.py` — module-level
    constants (CACHE_DIR, CACHE_FILE, …) capture `Path.home()` at import
    time so we monkeypatch HOME BEFORE re-importing.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # Wipe leaked tier-overrides / keys from the host env so the test
    # exercises a clean default state.
    for var in (
        "VIBECODED_TIER",
        "VIBECODED_LICENSE_KEY",
        "VIBECODED_LICENSE_URL",
        "VCT_VALIDATE_TIER_URL",
    ):
        monkeypatch.delenv(var, raising=False)

    import VCThelpers.license.validator as v
    importlib.reload(v)
    # Belt-and-braces: also clear the in-process tier cache so the test's
    # first `feature_enabled` call doesn't return a leaked-cached tier.
    v._cached_tier = None
    yield v
    v._cached_tier = None


def _write_license_cache(
    validator,
    *,
    tier: str = "free",
    valid: bool = True,
    module_licenses: dict | None = None,
):
    """Write a synthetic `~/.vibecoded/license_cache.json` mirroring the
    shape the Rust launcher's `license_refresh` + `validate_module_license`
    produce.

    The Python validator's `is_module_licensed` reads this file directly
    — same wire contract as the Phase 3A token gateway.
    """
    validator.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "tier": tier,
        "valid": valid,
        "expires_at": None,
        "last_validated_at": time.time(),
        "message": "Validated.",
    }
    if module_licenses is not None:
        payload["module_licenses"] = module_licenses
    validator.CACHE_FILE.write_text(json.dumps(payload))


# ─────────────────────────────────────────────────────────────────────────────
# T1 — is_module_licensed reads the overlay
# ─────────────────────────────────────────────────────────────────────────────


class TestIsModuleLicensed:
    def test_returns_false_when_cache_file_missing(self, fresh_validator):
        # No cache file at all — fail-closed, no crash.
        assert fresh_validator.is_module_licensed("vct-rl-reranker") is False

    def test_returns_false_when_overlay_field_absent(self, fresh_validator):
        # Legacy cache shape (pre-v0.2.31) doesn't carry `module_licenses`.
        _write_license_cache(fresh_validator, tier="pro", valid=True)
        # No module_licenses entry → not licensed via the overlay path.
        assert fresh_validator.is_module_licensed("vct-rl-reranker") is False

    def test_returns_true_for_pro_tier_module_entry(self, fresh_validator):
        _write_license_cache(
            fresh_validator,
            tier="free",
            valid=True,
            module_licenses={
                "vct-rl-reranker": {
                    "tier": "pro",
                    "source": "per-module",
                },
            },
        )
        assert fresh_validator.is_module_licensed("vct-rl-reranker") is True

    def test_returns_false_for_free_tier_module_entry(self, fresh_validator):
        # An entry that exists but has tier='free' is NOT considered a
        # paid module entitlement (mirrors the Rust gate logic).
        _write_license_cache(
            fresh_validator,
            tier="free",
            valid=True,
            module_licenses={
                "vct-rl-reranker": {"tier": "free"},
            },
        )
        assert fresh_validator.is_module_licensed("vct-rl-reranker") is False

    def test_returns_false_for_malformed_json(self, fresh_validator):
        # Cache file present but unparseable — fail-closed, no crash.
        fresh_validator.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fresh_validator.CACHE_FILE.write_text("{ this isn't valid JSON")
        assert fresh_validator.is_module_licensed("vct-rl-reranker") is False

    def test_returns_false_for_non_dict_root(self, fresh_validator):
        # Defensive: a hand-edited cache file that's a JSON array, not
        # an object. Must not crash, must not lie about entitlement.
        fresh_validator.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fresh_validator.CACHE_FILE.write_text("[1, 2, 3]")
        assert fresh_validator.is_module_licensed("vct-rl-reranker") is False

    def test_returns_false_when_module_id_blank(self, fresh_validator):
        # An empty module_id is a caller bug — refuse rather than try
        # to match an empty string against the overlay.
        _write_license_cache(
            fresh_validator,
            tier="pro",
            valid=True,
            module_licenses={"vct-rl-reranker": {"tier": "pro"}},
        )
        assert fresh_validator.is_module_licensed("") is False

    def test_unrelated_module_not_affected_by_neighbour_entitlement(
        self, fresh_validator
    ):
        # T3 mirror: per-module entries are independent. Setting one
        # entry must not bleed into other modules.
        _write_license_cache(
            fresh_validator,
            tier="free",
            valid=True,
            module_licenses={
                "vct-rl-reranker": {"tier": "pro"},
            },
        )
        assert fresh_validator.is_module_licensed("vct-rl-reranker") is True
        # No entry for vct-mao → not licensed despite the sibling.
        assert fresh_validator.is_module_licensed("vct-mao") is False


# ─────────────────────────────────────────────────────────────────────────────
# T2 — feature_enabled honours per-module gates when caller passes module_id
# ─────────────────────────────────────────────────────────────────────────────


class TestFeatureEnabledModulePath:
    def test_free_tier_no_per_module_key_returns_false(self, fresh_validator):
        # Legacy behaviour: free tier + no per-module key + Pro-only
        # feature → not enabled.
        _write_license_cache(fresh_validator, tier="free", valid=True)
        assert (
            fresh_validator.feature_enabled(
                "rl_retrieval", module_id="vct-rl-reranker"
            )
            is False
        )

    def test_free_tier_with_per_module_pro_key_unlocks_feature(
        self, fresh_validator
    ):
        # L1 new behaviour: free orchestrator tier + per-module pro key
        # → feature unlocked through the overlay path.
        _write_license_cache(
            fresh_validator,
            tier="free",
            valid=True,
            module_licenses={
                "vct-rl-reranker": {"tier": "pro", "source": "per-module"},
            },
        )
        assert (
            fresh_validator.feature_enabled(
                "rl_retrieval", module_id="vct-rl-reranker"
            )
            is True
        )

    def test_pro_tier_short_circuits_module_path(
        self, fresh_validator, monkeypatch
    ):
        # Pro-tier user never reaches the overlay path — the legacy
        # tier check satisfies first. The overlay can be empty (or
        # absent) and the feature still unlocks.
        # Avoid hitting the network when get_tier() runs.
        import urllib.request
        from unittest import mock

        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(
            {"valid": True, "tier": "pro", "message": "ok"}
        ).encode()
        resp.status = 200
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "GOOD-UUID")
        with mock.patch.object(
            urllib.request, "urlopen", return_value=resp
        ):
            assert fresh_validator.get_tier(force_refresh=True) == "pro"
        # No module_licenses overlay needed — Pro tier alone unlocks
        # the feature.
        assert (
            fresh_validator.feature_enabled(
                "rl_retrieval", module_id="vct-rl-reranker"
            )
            is True
        )
        # And the bare-call (no module_id) also returns True.
        assert fresh_validator.feature_enabled("rl_retrieval") is True

    def test_backward_compat_no_module_id_kwarg(self, fresh_validator):
        # Pre-L1 call sites pass only the feature name. Behaviour MUST
        # match the v0.2.39 contract for the default-args path.
        _write_license_cache(fresh_validator, tier="free", valid=True)
        # Pro-gated feature on free tier → False (legacy).
        assert fresh_validator.feature_enabled("rl_retrieval") is False
        # Unknown feature → fail-open True (legacy).
        assert fresh_validator.feature_enabled("hypothetical_new") is True

    def test_module_id_is_ignored_for_unknown_features(self, fresh_validator):
        # Unknown features hit the fail-open path BEFORE the overlay
        # check, so passing module_id doesn't change the answer.
        _write_license_cache(fresh_validator, tier="free", valid=True)
        assert (
            fresh_validator.feature_enabled(
                "hypothetical_new", module_id="vct-some-module"
            )
            is True
        )


# ─────────────────────────────────────────────────────────────────────────────
# T3 — module entries are independent (single source of truth)
# ─────────────────────────────────────────────────────────────────────────────


class TestPerModuleIndependence:
    def test_setting_module_a_overlay_does_not_grant_module_b_feature(
        self, fresh_validator
    ):
        # Mirror of the Rust-side
        # `license_keys_per_module_rows_are_independent` test on the
        # Python overlay path. A future per-module gated feature for
        # module B must still report False when only A is licensed.
        _write_license_cache(
            fresh_validator,
            tier="free",
            valid=True,
            module_licenses={
                "vct-rl-reranker": {"tier": "pro", "source": "per-module"},
            },
        )
        # Module A entitled → rl_retrieval unlocked.
        assert (
            fresh_validator.feature_enabled(
                "rl_retrieval", module_id="vct-rl-reranker"
            )
            is True
        )
        # Module B has no entry → the same feature gated by module B
        # must report False.
        assert fresh_validator.is_module_licensed("vct-mao") is False

    def test_removing_module_a_overlay_does_not_drop_module_b(
        self, fresh_validator
    ):
        # Seed with two entries.
        _write_license_cache(
            fresh_validator,
            tier="free",
            valid=True,
            module_licenses={
                "vct-rl-reranker": {"tier": "pro"},
                "vct-mao": {"tier": "mao"},
            },
        )
        assert fresh_validator.is_module_licensed("vct-rl-reranker") is True
        assert fresh_validator.is_module_licensed("vct-mao") is True
        # Now remove just module A from the overlay (mirrors what the
        # Rust `clear_module_license_key` does).
        _write_license_cache(
            fresh_validator,
            tier="free",
            valid=True,
            module_licenses={"vct-mao": {"tier": "mao"}},
        )
        assert fresh_validator.is_module_licensed("vct-rl-reranker") is False
        # B's overlay entry survives.
        assert fresh_validator.is_module_licensed("vct-mao") is True


# ─────────────────────────────────────────────────────────────────────────────
# Legacy single-key compatibility
# ─────────────────────────────────────────────────────────────────────────────


class TestLegacyCompat:
    def test_legacy_single_key_pro_user_unaffected(
        self, fresh_validator, monkeypatch
    ):
        """A v0.2.39 install with one VIBECODED_LICENSE_KEY validating
        to Pro tier MUST keep working after upgrade — every existing
        feature_enabled call site continues to gate by orchestrator
        tier alone. The L1 changes are PURELY ADDITIVE (the per-module
        overlay path opens up NEW unlock cases; it never closes off
        existing ones)."""
        import urllib.request
        from unittest import mock

        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(
            {"valid": True, "tier": "pro", "message": "ok"}
        ).encode()
        resp.status = 200
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        monkeypatch.setenv("VIBECODED_LICENSE_KEY", "LEGACY-PRO-UUID")
        with mock.patch.object(
            urllib.request, "urlopen", return_value=resp
        ):
            # First call performs the remote validation + caches Pro.
            assert fresh_validator.get_tier(force_refresh=True) == "pro"
        # The bare feature_enabled call returns True (legacy contract).
        assert fresh_validator.feature_enabled("rl_retrieval") is True
        # And the new module_id kwarg still returns True (Pro tier
        # short-circuits before the overlay path).
        assert (
            fresh_validator.feature_enabled(
                "rl_retrieval", module_id="vct-rl-reranker"
            )
            is True
        )
