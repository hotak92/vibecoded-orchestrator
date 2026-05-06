# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""License validation + feature gating for VibeCoded Tools Orchestrator.

Reads (in priority order, first match wins):
    VIBECODED_TIER         — free | pro | mao | enterprise (default: free)
                             Setting this to 'free' forces free tier; any other
                             value is ignored (we never trust env-var-claimed
                             paid tiers without a validated key).
    VIBECODED_LICENSE_KEY  — 36-char UUID. Set by the launcher after
                             activation, or by tooling/tests directly.
    ~/.vct-secrets/shared/license_key  — fallback file (chmod 600, plain UUID,
                             no trailing whitespace). Used by headless installs
                             where the launcher hasn't run. Legacy flat layout
                             ~/.vct-secrets/license_key is still honored as a
                             fallback for backward compat.
    VIBECODED_LICENSE_URL  — Supabase /validate-tier edge function URL.
                             Defaults to the production deployment.

Grace period:
    If the last successful remote validation was more than 3 days ago and we
    cannot reach the validation endpoint, the tier is degraded to 'free' and
    a human-readable message is written to ~/.vibecoded/license_status.txt.
    Nothing breaks — free-tier functionality continues to work.

Public API:
    get_tier(force_refresh=False) -> Tier
    require_tier(min_tier) -> bool
    feature_enabled(feature) -> bool
    validate_license(key=None) -> LicenseResult
    license_status() -> dict   # for CLI / launcher introspection

Network policy:
    Fail-OPEN to free tier on any transport failure. Never block startup,
    never raise. The Supabase edge function is the single source of truth
    for tier mapping; the orchestrator only knows {free, pro, mao,
    enterprise}.
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

Tier = Literal["free", "pro", "mao", "enterprise", "admin"]
# Bug 33: `admin` is a server-classified tier (not present in any
# public variant map). Treated as a strict superset of `enterprise`
# for feature-gate purposes. Validation goes through the same
# Supabase /validate-tier edge function as every other tier — there
# is no env-var bypass and no local signature-verification shortcut.
# A patched client can self-claim `tier=admin` locally, but every
# server-gated capability (paid module artifact downloads, signed-URL
# gateway) re-validates against the JWT issued by validate-tier, so
# self-claimed admin yields nothing the AGPL source doesn't already.
TIER_ORDER: dict[Tier, int] = {
    "free": 0,
    "pro": 1,
    "mao": 2,
    "enterprise": 3,
    "admin": 4,
}

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

# Fallback license-key location for headless installs (no launcher).
# Convention: same dir as other VCT secrets, file named `license_key`,
# chmod 600, contains only the UUID (no JSON, no trailing newline matters —
# we strip it).
#
# Phase 1 layout (preferred): ~/.vct-secrets/shared/license_key
# Legacy flat layout (fallback): ~/.vct-secrets/license_key
KEY_FILE_SHARED = Path.home() / ".vct-secrets" / "shared" / "license_key"
KEY_FILE_FLAT = Path.home() / ".vct-secrets" / "license_key"
# Back-compat export for any external code that imported KEY_FILE directly:
KEY_FILE = KEY_FILE_FLAT


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
    # Default = Supabase project's canonical /validate-tier function URL.
    #
    # The earlier default `https://api.vibecodedtools.it/validate-tier` was
    # wishful thinking — that DNS record was never created (verified
    # 2026-05-06: `dig api.vibecodedtools.it` returns NXDOMAIN; only the
    # apex `vibecodedtools.it` resolves, and it points at Vercel for the
    # marketing site). Every license refresh from default config returned
    # NXDOMAIN and silently fell back to the 3-day cache grace.
    #
    # Two reasons not to fix by adding the DNS:
    #   1. Cross-subdomain risk: the apex serves the website on Vercel.
    #      Putting the API on api.<apex> shares cookie scope and CORS
    #      surface with the marketing site.
    #   2. DNS-config drift: one IONOS panel mistake and we're back to
    #      NXDOMAIN. The Supabase URL is stable as long as the project
    #      exists.
    #
    # Operators override via VIBECODED_LICENSE_URL or VCT_VALIDATE_TIER_URL
    # (both honored) for staging/dev or for a future custom-domain plan
    # (e.g. api.vct.cloud, kept distinct from the website apex).
    #
    # Mirrors `launcher/src-tauri/src/commands/licensing.rs::DEFAULT_VALIDATE_TIER_URL`.
    "https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/validate-tier"
)


_NETWORK_TIMEOUT_SECONDS = 8


class _RemoteOutcome:
    """Sentinel for distinguishing network failure from a definitive 'free' verdict.

    `_remote_validate` returns:
      - `LicenseResult` (decisive: trust the server, overwrite cache)
      - `None`          (transport failure: caller should fall back to cache)
    """


def _remote_validate(key: str, machine_hash: str) -> Optional[LicenseResult]:
    """Call the Supabase /validate-tier edge function.

    The edge function wraps Lemon Squeezy's license validation:
        1. Calls LS /v1/licenses/validate to verify the key
        2. Calls LS /v1/licenses/activate with instance_name=machine_hash
           (LS handles machine binding + per-product instance limits)
        3. Maps variant_id → tier via server-side VARIANT_MAP
        4. Returns { valid, tier, expires_at, machine_count, machine_limit }

    Returns:
        LicenseResult — definitive answer (200 with tier, 401 invalid-key, 200
                        with `error: instance_limit`). Caller MUST cache it.
        None          — network error / 5xx. Caller falls back to cache within
                        the 3-day grace window.

    Never raises.
    """
    # Honor either env var: VIBECODED_LICENSE_URL is the historical Python
    # name; VCT_VALIDATE_TIER_URL is the Rust launcher's name (commands/
    # licensing.rs). Either one wins over the default; if both are set,
    # VIBECODED_LICENSE_URL takes precedence (Python path's own var).
    url = (
        os.environ.get("VIBECODED_LICENSE_URL")
        or os.environ.get("VCT_VALIDATE_TIER_URL")
        or _DEFAULT_VALIDATE_URL
    )
    timeout = _NETWORK_TIMEOUT_SECONDS
    try:
        import urllib.error
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
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as http_err:
            # 4xx: try to parse the JSON body for a structured error response.
            # 5xx: treat as transport failure → fall back to cache.
            status = http_err.code
            try:
                raw = http_err.read()
            except Exception:
                raw = b""
            if status >= 500:
                log.debug("validate-tier %s; falling back to cache", status)
                return None

        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            log.warning("validate-tier returned non-JSON body (status=%s)", status)
            return None

        # 401 → invalid / expired / disabled. Definitive: drop to free,
        # overwrite cache so a previously-valid Pro tier doesn't linger.
        if status == 401:
            return LicenseResult(
                tier="free",
                valid=False,
                last_validated_at=time.time(),
                message=str(payload.get("message", "Invalid or expired license.")),
            )

        # Machine-limit exceeded — license is valid but not usable here.
        if payload.get("error") == "instance_limit":
            return LicenseResult(
                tier="free",
                valid=False,
                last_validated_at=time.time(),
                message=str(payload.get(
                    "message",
                    "This license is already activated on the maximum number of "
                    "machines. Deactivate an old machine at "
                    "vibecodedtools.it/account or contact support.",
                )),
            )

        # 400 (malformed request) → log and treat as free; don't mask a client
        # bug as a transient network blip.
        if status == 400:
            log.warning("validate-tier rejected request: %s", payload.get("message"))
            return LicenseResult(
                tier="free",
                valid=False,
                last_validated_at=time.time(),
                message=str(payload.get("message", "Invalid request.")),
            )

        tier_value = payload.get("tier", "free")
        if tier_value not in TIER_ORDER:
            log.warning("validate-tier returned unknown tier=%r; coercing to free", tier_value)
            tier_value = "free"

        return LicenseResult(
            tier=tier_value,
            valid=bool(payload.get("valid", False)),
            expires_at=payload.get("expires_at"),
            last_validated_at=time.time(),
            message=str(payload.get("message", "Validated.")),
        )
    except Exception as e:
        # urllib raises URLError for DNS/timeout/connection refused. Any other
        # unexpected exception also lands here so we never break startup.
        log.debug("Remote license validation failed: %s", e)
        return None


def _read_key_file() -> str:
    """Read the license key from the fallback file, if present.

    Tries the Phase 1 layout (`~/.vct-secrets/shared/license_key`) first, then
    falls back to the legacy flat layout (`~/.vct-secrets/license_key`).
    Returns empty string on any error (missing, unreadable, empty). Never raises.
    """
    for path in (KEY_FILE_SHARED, KEY_FILE_FLAT):
        try:
            if path.exists():
                return path.read_text().strip()
        except (OSError, UnicodeDecodeError) as e:
            log.debug("Could not read %s: %s", path, e)
    return ""


def validate_license(key: Optional[str] = None) -> LicenseResult:
    """Validate license key and return the current licensing result.

    Priority:
        1. `VIBECODED_TIER=free` env forces free tier (dev override).
        2. Key resolution: explicit arg → env var → ``~/.vct-secrets/shared/license_key`` (preferred) or ``~/.vct-secrets/license_key`` (legacy fallback).
        3. No key → free tier.
        4. Remote validation → success → cache + return.
        5. Remote failure → check cache age:
             - within 3-day grace period → return cached tier.
             - beyond grace period → degrade to free tier with clear message.
    """
    tier_override = os.environ.get("VIBECODED_TIER", "").lower()
    if tier_override == "free":
        return LicenseResult(tier="free", valid=True, message="Free tier (env override).")

    if not key:
        key = os.environ.get("VIBECODED_LICENSE_KEY", "").strip()
    if not key:
        key = _read_key_file()
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


def is_admin() -> bool:
    """Bug 33: True iff the server classified this license as admin.

    Admin is a server-only tier. Validation goes through the same
    Supabase /validate-tier edge function as every other tier — the
    server consults the `LS_ADMIN_VARIANT_IDS` runtime env var to
    decide whether a license belongs to the admin variant. There is
    NO env-var bypass and NO local signature shortcut. Patching this
    function to always return True will unlock client-side dev
    affordances (admin sidebar, ADMIN badge, pre-release modules in
    the catalog) but will NOT unlock server-gated capabilities like
    paid module artifact downloads — the signed-URL gateway
    re-validates the JWT issued by validate-tier on every request.

    Cached for the process lifetime via `get_tier()`'s cache.
    """
    return get_tier() == "admin"


def feature_enabled(feature: str) -> bool:
    """Return True if `feature` is available on the current tier.

    Unknown features default to True (fail-open for features not yet gated).
    """
    min_tier = TIER_FEATURES.get(feature)
    if min_tier is None:
        return True
    return require_tier(min_tier)


def license_status() -> dict:
    """Return a structured snapshot of the current licensing state.

    Intended for CLI / launcher introspection. Never raises. Does NOT trigger
    a remote call — only inspects environment + cache + status file. Call
    ``validate_license(force_refresh=True)`` first if you need a fresh check.

    Returns a dict with at minimum:
        tier             — current effective tier (free | pro | mao | enterprise)
        has_key          — bool, whether any key source resolved a value
        key_source       — "env" | "file" | "argument" | "none"
        cached           — bool, whether a cached LicenseResult exists
        cache_age_days   — int or None, age of cached.last_validated_at
        in_grace_period  — bool, cached + within GRACE_PERIOD_SECONDS
        status_message   — last human-readable status line, if any
    """
    env_key = os.environ.get("VIBECODED_LICENSE_KEY", "").strip()
    file_key = _read_key_file()
    if env_key:
        key_source = "env"
        has_key = True
    elif file_key:
        key_source = "file"
        has_key = True
    else:
        key_source = "none"
        has_key = False

    cached = _load_cached()
    cache_age_days: Optional[int] = None
    in_grace = False
    if cached and cached.last_validated_at:
        delta = time.time() - cached.last_validated_at
        cache_age_days = int(delta // 86400)
        in_grace = delta < GRACE_PERIOD_SECONDS

    status_message = ""
    try:
        if STATUS_FILE.exists():
            status_message = STATUS_FILE.read_text().strip()
    except OSError:
        pass

    return {
        "tier": get_tier(),
        "has_key": has_key,
        "key_source": key_source,
        "cached": cached is not None,
        "cache_age_days": cache_age_days,
        "in_grace_period": in_grace,
        "status_message": status_message,
    }


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
