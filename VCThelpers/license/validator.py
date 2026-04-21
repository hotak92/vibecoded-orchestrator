# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 VibeCoded Tools
"""License validation + feature gating for VibeCoded Tools Orchestrator.

Reads:
    VIBECODED_TIER         — free | pro | mao | enterprise (default: free)
    VIBECODED_LICENSE_KEY  — license UUID (set by launcher after activation)
    VIBECODED_LICENSE_URL  — Supabase validation endpoint
                             (default: https://api.vibecodedtools.it/validate)

Grace period:
    If the last successful remote validation was more than 3 days ago and we
    cannot reach the validation endpoint, the tier is degraded to 'free' and
    a human-readable message is written to ~/.vibecoded/license_status.txt.
    Nothing breaks — free-tier functionality continues to work.

This is a stub: the remote call is implemented but the Supabase endpoint is
expected to be deployed separately. Until then, the cached-result path
(`validate_license` with a stubbed response) is the working code path.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Optional

log = logging.getLogger(__name__)

Tier = Literal["free", "pro", "mao", "enterprise"]
TIER_ORDER: dict[Tier, int] = {"free": 0, "pro": 1, "mao": 2, "enterprise": 3}

# Features gated per tier. Order: most-restrictive tier that unlocks it.
TIER_FEATURES: dict[str, Tier] = {
    "knowledge_graph": "free",
    "code_graph": "free",
    "hooks": "free",
    "hybrid_search": "free",
    "rl_retrieval": "pro",
    "auto_update": "pro",
    "curated_agent_packs": "pro",
    "watermark_disabled": "pro",
    "multi_agent_orchestration": "mao",
    "soc2_compliance": "enterprise",
    "priority_support": "enterprise",
}

GRACE_PERIOD_SECONDS = 3 * 24 * 3600  # 3 days
CACHE_DIR = Path.home() / ".vibecoded"
CACHE_FILE = CACHE_DIR / "license_cache.json"
STATUS_FILE = CACHE_DIR / "license_status.txt"


@dataclass
class LicenseResult:
    tier: Tier
    valid: bool
    # ISO 8601 string from LS (e.g. "2027-04-18T00:00:00.000Z") or None for lifetime.
    # Kept as a string because we don't need arithmetic on it — just display.
    expires_at: Optional[str] = None
    last_validated_at: Optional[float] = None  # epoch seconds
    message: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "LicenseResult":
        return cls(**json.loads(raw))


def _machine_id_hash() -> str:
    """Stable, one-way hash of the machine's MAC address.

    Never returns raw hardware identifiers. The hash is the only identifier
    sent to the validation endpoint.
    """
    node = uuid.getnode()
    return hashlib.sha256(node.to_bytes(8, "big")).hexdigest()


def _write_status(message: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(message + "\n")


def _load_cached() -> Optional[LicenseResult]:
    if not CACHE_FILE.exists():
        return None
    try:
        return LicenseResult.from_json(CACHE_FILE.read_text())
    except (json.JSONDecodeError, TypeError):
        return None


def _save_cached(result: LicenseResult) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(result.to_json())


_DEFAULT_VALIDATE_URL = (
    "https://ltnlwhaxnpbiifordlbk.supabase.co/functions/v1/validate-tier"
)


def _remote_validate(key: str, machine_hash: str) -> Optional[LicenseResult]:
    """Call the Supabase /validate-tier edge function.

    The edge function wraps Lemon Squeezy's license validation:
        1. Calls LS /v1/licenses/validate to verify the key
        2. Calls LS /v1/licenses/activate with instance_name=machine_hash
           (LS handles machine binding + per-product instance limits)
        3. Maps variant_id → tier via server-side VARIANT_MAP
        4. Returns { valid, tier, expires_at, machine_count, machine_limit }

    Returns None on network error so the caller can fall back to the cached
    result within the grace period. Never raises.
    """
    url = os.environ.get("VIBECODED_LICENSE_URL", _DEFAULT_VALIDATE_URL)
    try:
        import urllib.request
        body = json.dumps({
            "license_key": key,
            "machine_id_hash": machine_hash,
        }).encode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read())
        # Machine-limit exceeded is still a "valid" license, just not usable here.
        if payload.get("error") == "instance_limit":
            return LicenseResult(
                tier="free",
                valid=False,
                last_validated_at=time.time(),
                message=(
                    "This license is already activated on the maximum number of "
                    "machines. Deactivate an old machine at "
                    "vibecodedtools.it/account or contact support."
                ),
            )
        return LicenseResult(
            tier=payload.get("tier", "free"),
            valid=bool(payload.get("valid", False)),
            expires_at=payload.get("expires_at"),
            last_validated_at=time.time(),
            message=payload.get("message", "Validated."),
        )
    except Exception as e:
        log.debug("Remote license validation failed: %s", e)
        return None


def validate_license(key: Optional[str] = None) -> LicenseResult:
    """Validate license key and return the current licensing result.

    Priority:
        1. `VIBECODED_TIER=free` env forces free tier (dev override).
        2. If no key → free tier.
        3. Remote validation → success → cache + return.
        4. Remote failure → check cache age:
             - within 3-day grace period → return cached tier.
             - beyond grace period → degrade to free tier with clear message.
    """
    tier_override = os.environ.get("VIBECODED_TIER", "").lower()
    if tier_override == "free":
        return LicenseResult(tier="free", valid=True, message="Free tier (env override).")

    key = key or os.environ.get("VIBECODED_LICENSE_KEY", "").strip()
    if not key:
        return LicenseResult(tier="free", valid=True, message="No license key — free tier.")

    remote = _remote_validate(key, _machine_id_hash())
    if remote is not None:
        _save_cached(remote)
        _write_status(f"License: {remote.tier} (validated {time.strftime('%Y-%m-%d %H:%M')})")
        return remote

    cached = _load_cached()
    now = time.time()
    if cached and cached.last_validated_at and (now - cached.last_validated_at) < GRACE_PERIOD_SECONDS:
        days_left = int((GRACE_PERIOD_SECONDS - (now - cached.last_validated_at)) // 86400)
        msg = f"License: {cached.tier} (offline, {days_left}d grace remaining)"
        _write_status(msg)
        return cached

    # Grace period exceeded — degrade gracefully, never break.
    msg = (
        "License validation unavailable for >3 days. Falling back to free tier. "
        "Run `vibecoded validate` or visit vibecodedtools.it/account when online. "
        "Free-tier features continue to work normally."
    )
    _write_status(msg)
    log.warning(msg)
    return LicenseResult(tier="free", valid=True, message=msg)


_cached_tier: Optional[Tier] = None


def get_tier(force_refresh: bool = False) -> Tier:
    """Return the currently active tier. Caches for the process lifetime."""
    global _cached_tier
    if _cached_tier is None or force_refresh:
        _cached_tier = validate_license().tier
    return _cached_tier


def require_tier(min_tier: Tier) -> bool:
    """Return True if the current tier is at least `min_tier`."""
    return TIER_ORDER[get_tier()] >= TIER_ORDER[min_tier]


def feature_enabled(feature: str) -> bool:
    """Return True if `feature` is available on the current tier.

    Unknown features default to True (fail-open for features not yet gated).
    """
    min_tier = TIER_FEATURES.get(feature)
    if min_tier is None:
        return True
    return require_tier(min_tier)


if __name__ == "__main__":
    # Quick diagnostic: `python -m VCThelpers.license.validator`
    result = validate_license()
    print(f"Tier: {result.tier}")
    print(f"Valid: {result.valid}")
    print(f"Message: {result.message}")
    print(f"Features unlocked:")
    for f in TIER_FEATURES:
        marker = "✓" if feature_enabled(f) else "✗"
        print(f"  {marker} {f}")
