// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Copyright (C) VibeCoded Tools — licensed under AGPL-3.0-or-later.
//
//! Shared license + machine-binding helpers for the launcher GUI and
//! vct-hub. Both crates need to:
//!
//!   * Read the user's activated license key out of the OS keychain
//!     (same row, same scope, same username) — `read_license_key_from_keychain`.
//!   * Derive a stable, one-way machine identifier (the only thing that
//!     crosses the wire to the pull-token gateway / validate-tier edge
//!     function) — `machine_id_hash`.
//!
//! ## Why this module exists (v0.2.49)
//!
//! Pre-v0.2.49 these helpers lived in
//! `launcher/src-tauri/src/commands/licensing.rs`, reachable from
//! `installer_engine::request_pull_token`. The hub-side supervisor's
//! `start_container_for_module` (`vct-hub/src/module_supervisor.rs`)
//! could not call them — different crate, no path dependency from the
//! launcher binary INTO the hub crate (the dependency goes the other
//! way through `vct-launcher-core`). The Phase 3 hub-side supervisor
//! auth port (v0.2.49) needs the same keychain read + machine-id hash
//! so the hub's pre-pull-with-auth path matches the launcher's
//! byte-for-byte.
//!
//! ## Invariants preserved across the move
//!
//!   * **Wire contract**: `machine_id_hash` still returns 64-char
//!     lowercase hex (sha256). Same algorithm `validate-tier`
//!     and `rl-artifact-url` Supabase edge functions expect. Never
//!     returns the raw OS identifier — the hash is the only thing that
//!     crosses the wire.
//!   * **Keychain location**: `read_license_key_from_keychain` reads
//!     from `SecretScope::Global` + `LICENSE_MODULE_ID` + the canonical
//!     `keychain_username_for(ORCHESTRATOR_MODULE_ID)` username. Same
//!     SHA `service_name(LICENSE_MODULE_ID)` resolves to in the
//!     launcher's previous home, so both crates see the same row.
//!   * **Fallback semantics**: if every platform source for the host id
//!     fails, hashes the `vct-no-platform-host-id-v0.2.36` sentinel
//!     (kept verbatim — bumping the version would break the validator's
//!     forensic check against `admin_auth_log`).
//!   * **Test override**: `VCT_MACHINE_ID_OVERRIDE` env var still
//!     short-circuits the platform read. Empty value treated as "not set".

use sha2::{Digest, Sha256};

use crate::db::license_keys::{keychain_username_for, ORCHESTRATOR_MODULE_ID};
use crate::secrets::{self, SecretScope};

/// Service-scope identifier used by `secrets::get/set` for the license-
/// key row. Same value the launcher used pre-v0.2.49; promoted to
/// `pub` here so out-of-crate callers can express the same scope when
/// reading directly via `secrets::get`.
pub const LICENSE_MODULE_ID: &str = "licensing";

/// Test-only override env var. When set with a non-empty value,
/// `machine_id_hash()` uses the override verbatim (as the bytes to
/// hash). Production code MUST NOT set this; the existence of the var
/// overrides whatever the host actually reports. Documented as a test
/// seam so reviewers don't grep for it and think it's a security
/// backdoor.
pub const MACHINE_ID_OVERRIDE_ENV: &str = "VCT_MACHINE_ID_OVERRIDE";

/// Read the currently-activated license key from the OS keychain.
/// Returns `Ok(Some(key))` when present, `Ok(None)` when the user has
/// not activated (free tier), and `Err` on keychain access failure.
///
/// Shared between launcher-side `commands::licensing::license_refresh`
/// and the pull-token gateway path. v0.2.49: also shared with the hub-
/// side supervisor's pre-pull-with-auth flow so both crates pull from
/// the same row.
///
/// L1.M (v0.2.40) note kept verbatim: the one-time migration in
/// `ensure_legacy_orchestrator_row_migrated` rewrites the keychain
/// entry from the legacy username (`VIBECODED_LICENSE_KEY`) to the
/// canonical one (`license_key____orchestrator__`) at launcher boot,
/// so by the time this reader is called the value lives at the
/// canonical username.
pub fn read_license_key_from_keychain() -> Result<Option<String>, String> {
    secrets::get(
        SecretScope::Global,
        LICENSE_MODULE_ID,
        &keychain_username_for(ORCHESTRATOR_MODULE_ID),
    )
}

/// Stable, one-way machine identifier sent to `/validate-tier` and
/// `/rl-artifact-url`. Returns 64-char lowercase hex (sha256). Never
/// returns the raw OS identifier — the hash is the only thing that
/// crosses the wire.
///
/// Mirrors `VCThelpers/license/validator.py::_machine_id_hash`. The two
/// implementations MUST agree on every byte for cross-language tier
/// checks to bind to the same machine.
///
/// Fallback semantics: if every platform source fails, hashes a fixed
/// sentinel so the function still returns a well-formed 64-char hex
/// string (preserves the rebind-admin-token regex contract
/// `^[0-9a-f]{64}$`). Server-side, all such hosts collide on the same
/// hash — acceptable degraded behaviour, surfaces as a machine-mismatch
/// the user can resolve via rebind.
pub fn machine_id_hash() -> String {
    let id = read_platform_host_id().unwrap_or_else(|| {
        // Sentinel for "no platform identifier available". Distinct
        // from a real hash so a forensic check against `admin_auth_log`
        // can recognise the degraded path. v0.2.36 string kept
        // verbatim — bumping the version breaks the forensic match.
        "vct-no-platform-host-id-v0.2.36".to_string()
    });
    let mut hasher = Sha256::new();
    hasher.update(id.as_bytes());
    hex::encode(hasher.finalize())
}

/// Read the platform-stable host identifier as a `String` (the raw
/// input to the sha256 hash). Returns `None` only when every supported
/// source fails on the current OS — that's the trigger for the
/// deterministic sentinel fallback in `machine_id_hash`.
///
/// Cross-platform compilation: each `#[cfg(target_os = "...")]` branch
/// is independent. The Windows branch uses `winreg` (declared as a
/// cfg(windows) target dep); the macOS branch shells out via std (no
/// extra dep); the Linux branch is a plain file read.
fn read_platform_host_id() -> Option<String> {
    // Test override always wins, regardless of OS. Empty value treated
    // as "not set" so a stray export with an empty rhs doesn't change
    // the hash to sha256("").
    if let Ok(v) = std::env::var(MACHINE_ID_OVERRIDE_ENV) {
        if !v.is_empty() {
            return Some(v);
        }
    }

    #[cfg(target_os = "windows")]
    {
        read_windows_machine_guid()
    }

    #[cfg(target_os = "macos")]
    {
        read_macos_platform_uuid()
    }

    #[cfg(target_os = "linux")]
    {
        read_linux_machine_id()
    }

    #[cfg(not(any(target_os = "windows", target_os = "macos", target_os = "linux")))]
    {
        // BSDs / Solaris / unknown: no stable algorithm we trust. Fall
        // through to the deterministic sentinel hash. The user can
        // still set `VCT_MACHINE_ID_OVERRIDE` (handled above) to pin a
        // value.
        None
    }
}

#[cfg(target_os = "windows")]
fn read_windows_machine_guid() -> Option<String> {
    // HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid is a REG_SZ
    // value (GUID string) set by Windows during install. Survives NIC
    // changes, user-account changes, and even motherboard replacement
    // (it's registry-resident, not hardware-derived). Only an OS
    // reinstall or explicit registry edit changes it.
    use winreg::enums::{HKEY_LOCAL_MACHINE, KEY_READ};
    use winreg::RegKey;

    let hklm = RegKey::predef(HKEY_LOCAL_MACHINE);
    let subkey = hklm
        .open_subkey_with_flags(r"SOFTWARE\Microsoft\Cryptography", KEY_READ)
        .ok()?;
    let guid: String = subkey.get_value("MachineGuid").ok()?;
    let trimmed = guid.trim();
    if trimmed.is_empty() {
        return None;
    }
    Some(trimmed.to_string())
}

#[cfg(target_os = "macos")]
fn read_macos_platform_uuid() -> Option<String> {
    // `ioreg -rd1 -c IOPlatformExpertDevice` dumps the
    // IOPlatformExpertDevice entry; the line `"IOPlatformUUID" =
    // "<HWUUID>"` is what we want. Shelling out is the simplest path —
    // `ioreg` is part of the base system on every macOS install (no
    // extra dep, no IOKit FFI).
    let output = std::process::Command::new("ioreg")
        .args(["-rd1", "-c", "IOPlatformExpertDevice"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let stdout = String::from_utf8(output.stdout).ok()?;
    for line in stdout.lines() {
        if let Some(rest) = line.split_once("\"IOPlatformUUID\"") {
            if let Some(after_eq) = rest.1.split_once('=') {
                let raw = after_eq.1.trim();
                let unquoted = raw.trim_matches('"').trim();
                if !unquoted.is_empty() {
                    return Some(unquoted.to_string());
                }
            }
        }
    }
    None
}

#[cfg(target_os = "linux")]
fn read_linux_machine_id() -> Option<String> {
    // /etc/machine-id is the systemd standard (set at install or first
    // boot; 32-char hex). Fallback to /var/lib/dbus/machine-id covers
    // pre-systemd and non-systemd distros that still ship dbus.
    for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"] {
        if let Ok(content) = std::fs::read_to_string(path) {
            let trimmed = content.trim();
            if !trimmed.is_empty() {
                return Some(trimmed.to_string());
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Serialize tests that mutate the `VCT_MACHINE_ID_OVERRIDE` env
    /// var so they don't race against each other (or against
    /// `read_platform_host_id` on the same thread).
    fn env_lock() -> std::sync::MutexGuard<'static, ()> {
        use std::sync::{Mutex, OnceLock};
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| Mutex::new(()))
            .lock()
            .unwrap_or_else(|p| p.into_inner())
    }

    /// Helper: set the override env var, run `f`, restore previous
    /// value (or remove). Returns whatever `f` returns.
    fn with_override<F, R>(value: &str, f: F) -> R
    where
        F: FnOnce() -> R,
    {
        let _g = env_lock();
        let previous = std::env::var(MACHINE_ID_OVERRIDE_ENV).ok();
        std::env::set_var(MACHINE_ID_OVERRIDE_ENV, value);
        let out = f();
        match previous {
            Some(v) => std::env::set_var(MACHINE_ID_OVERRIDE_ENV, v),
            None => std::env::remove_var(MACHINE_ID_OVERRIDE_ENV),
        }
        out
    }

    /// Wire-contract regression: machine_id_hash always returns 64
    /// lowercase hex characters AND is deterministic for the same
    /// input.
    #[test]
    fn machine_id_hash_is_64_char_lowercase_hex_and_deterministic() {
        let h = with_override("test-host-id-vct-v0.2.49", machine_id_hash);
        assert_eq!(h.len(), 64, "hash must be 64 chars, got: {}", h.len());
        assert!(
            h.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()),
            "hash must be all-lowercase hex, got: {:?}",
            h
        );
        let again = with_override("test-host-id-vct-v0.2.49", machine_id_hash);
        assert_eq!(h, again, "hash must be deterministic");
    }

    /// Test override env var actually drives the hash input — without
    /// this we couldn't pin behaviour cross-OS.
    #[test]
    fn machine_id_hash_uses_override_env_when_set() {
        let a = with_override("input-A", machine_id_hash);
        let b = with_override("input-B", machine_id_hash);
        assert_ne!(a, b, "different overrides must produce different hashes");
    }

    /// Empty override == "not set". Verifies the early-return check
    /// in `read_platform_host_id` so a stray empty assignment doesn't
    /// accidentally hash sha256(""), which would collide every host.
    #[test]
    fn machine_id_hash_ignores_empty_override() {
        // Empty override → fall through to real platform read. We can't
        // pin the exact hash (depends on host), but we CAN assert it's
        // not the sha256 of an empty string.
        let empty_sha = {
            let mut h = Sha256::new();
            h.update(b"");
            hex::encode(h.finalize())
        };
        let real = with_override("", machine_id_hash);
        assert_ne!(real, empty_sha, "empty override must not collide with sha256(\"\")");
    }

    /// On a real CI host (no override), the function still returns a
    /// well-formed 64-char hex. Either the platform read succeeds (real
    /// /etc/machine-id on Linux; HKLM\Cryptography on Windows;
    /// IOPlatformExpertDevice on macOS) OR we fall through to the
    /// sentinel — in both cases the output shape is invariant.
    #[test]
    fn machine_id_hash_real_platform_returns_well_formed_hex() {
        let _g = env_lock();
        let previous = std::env::var(MACHINE_ID_OVERRIDE_ENV).ok();
        std::env::remove_var(MACHINE_ID_OVERRIDE_ENV);
        let h = machine_id_hash();
        match previous {
            Some(v) => std::env::set_var(MACHINE_ID_OVERRIDE_ENV, v),
            None => std::env::remove_var(MACHINE_ID_OVERRIDE_ENV),
        }
        assert_eq!(h.len(), 64);
        assert!(h.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    }
}
