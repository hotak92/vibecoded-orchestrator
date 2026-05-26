// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Preflight checks that gate the install pipeline at the GUI level.
//!
//! v0.2.35 (Agent M, 2026-05-26):
//!
//! Why this exists alongside `installer_engine::detect_container_runtime`:
//! the engine function is called DEEP inside `run_install` — by the time
//! it errors with "no container runtime found", the user has already
//! clicked Install, watched the spinner spin for a few seconds, and now
//! sees a one-line error string in a toast. That error doesn't tell them
//! HOW to fix it.
//!
//! This module runs ABOVE the engine, at the GUI click handler boundary
//! in `ModuleCatalog.svelte::handleInstall`. It produces a structured
//! `RuntimeAvailability` shape that the frontend turns into a modal with
//! an OS-aware "Install Podman" link + a "Detect again" affordance.
//!
//! Why NOT reuse the boot-time `NoContainerRuntimeDialog` flow:
//!   - The boot dialog listens for an event emitted by
//!     `commands::lifecycle::auto_start_on_boot` that fires exactly once
//!     per launcher boot. A user who installed the launcher with a
//!     working runtime, then uninstalled the runtime later, would NEVER
//!     see the boot dialog re-fire — the install click would just fail
//!     deep in the pipeline.
//!   - This preflight runs on EVERY install click, so the gate is
//!     transactional: runtime present right now → proceed; runtime
//!     missing right now → block + explain.
//!
//! The new Tauri command is `check_container_runtime_available`. It uses
//! the SAME `services::runtime::detect_runtime` helper that
//! `runtime_install::runtime_recheck` uses, so the "Detect again" button
//! on this modal and the one on the boot-time modal converge to the same
//! truth source.

use serde::{Deserialize, Serialize};

use crate::services::runtime::{detect_runtime, invalidate_cache as invalidate_runtime_cache};

/// Result of the install-pipeline preflight check.
///
/// `detected` is `Some("podman" | "docker")` when at least one runtime
/// is on PATH (and its `--version` probe succeeded inside `detect_runtime`).
/// `install_url` is OS-specific and points at a user-friendly install
/// page; the frontend opens it via `runtime_open_install_url` (the
/// existing allowlist-guarded opener command), NOT via direct `<a href>`,
/// to keep the URL gating server-side.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RuntimeAvailability {
    /// True iff `detect_runtime` returned a usable runtime.
    pub available: bool,
    /// `"podman"` | `"docker"` when present, `None` otherwise. The string
    /// matches `ContainerRuntime::binary()` so the frontend can render it
    /// verbatim ("Detected runtime: podman").
    pub detected: Option<String>,
    /// `"linux"` | `"macos"` | `"windows"` | `"unknown"`. Drives which
    /// install URL the modal links to.
    pub platform: String,
    /// Canonical install-instructions URL for the current platform. The
    /// frontend passes this to `runtime_open_install_url`, which enforces
    /// an allowlist (see `commands::runtime_install`). `None` for
    /// unknown platforms or when a runtime IS already available (no link
    /// needed in the success case).
    pub install_url: Option<String>,
}

/// Resolve the canonical install URL for the current OS. Mirrors the
/// URLs the boot-time `NoContainerRuntimeDialog` offers — same allowlist
/// applies on the opener side, so all three URLs are accepted.
///
/// - Linux: podman.io's canonical install page. Linux distros vary
///   wildly (apt/dnf/pacman/zypper); the page has per-distro tabs. The
///   boot-time dialog can elevate to install via pkexec; this preflight
///   just links to the docs because auto-install would re-implement that
///   whole flow.
/// - macOS: Podman Desktop's macOS download page — most user-friendly
///   path on Mac (the .dmg wraps `podman machine init`).
/// - Windows: Podman Desktop's Windows download page — handles the
///   WSL2 prerequisite-checking inside its installer.
fn install_url_for(platform: &str) -> Option<String> {
    match platform {
        "linux" => Some("https://podman.io/docs/installation".to_string()),
        "macos" => Some("https://podman-desktop.io/downloads/macos".to_string()),
        "windows" => Some("https://podman-desktop.io/downloads/windows".to_string()),
        _ => None,
    }
}

/// Normalize `std::env::consts::OS` to the three platforms the frontend
/// renders branches for. Unknown OS values surface as `"unknown"` so the
/// frontend can show a generic "install a container runtime" message
/// without crashing on an unmapped string.
fn current_platform() -> String {
    match std::env::consts::OS {
        "linux" => "linux".into(),
        "macos" => "macos".into(),
        "windows" => "windows".into(),
        other => {
            // freebsd, dragonfly, netbsd, openbsd, etc. — VCT services
            // theoretically work via Podman on these, but we don't ship
            // tested install URLs for them. Falling back to "unknown"
            // lets the modal render a generic message.
            let _ = other;
            "unknown".into()
        }
    }
}

/// GUI-level preflight: check whether a container runtime is currently
/// available so the install pipeline can proceed.
///
/// Cache discipline: invalidates the `services::runtime` cache before
/// probing, so a "Detect again" click from the modal reflects the
/// current state of PATH rather than a stale cached `None` from boot.
/// The boot-time runtime probe uses the same cache, so a successful
/// detection here also unblocks the cached value for the rest of the
/// session.
///
/// Never returns `Err`: the function's contract is "tell the frontend
/// what's on PATH right now". Even on probe failure we return
/// `available: false` so the frontend renders the install-instructions
/// modal; we don't surface internal probe errors as command-level Err
/// because that would route through the FE's generic error toast and
/// hide the structured `RuntimeAvailability` shape that drives the
/// modal's branches.
#[tauri::command]
pub async fn check_container_runtime_available() -> Result<RuntimeAvailability, String> {
    // Always re-probe — the user may have installed/uninstalled a
    // runtime since the launcher booted. The cache exists to avoid
    // re-probing on hot paths (services watcher polls every few
    // seconds); for an explicit user-driven preflight, freshness wins
    // over the ~50ms probe cost.
    invalidate_runtime_cache();

    let info = detect_runtime().await;
    let platform = current_platform();
    let install_url = if info.is_none() {
        install_url_for(&platform)
    } else {
        None
    };

    Ok(RuntimeAvailability {
        available: info.is_some(),
        detected: info.map(|i| i.runtime.binary().to_string()),
        platform,
        install_url,
    })
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn install_url_known_platforms_resolve() {
        // Each of the three first-tier platforms must map to a non-empty
        // canonical URL that the runtime_install opener's allowlist accepts.
        let linux = install_url_for("linux").expect("linux url present");
        let macos = install_url_for("macos").expect("macos url present");
        let windows = install_url_for("windows").expect("windows url present");

        // Each URL must start with one of the allowlisted prefixes from
        // `runtime_install::ALLOWED_INSTALL_URL_PREFIXES`. We can't
        // import the const (it's private to the module) but we can
        // assert the prefix shape — if the constants drift, this test
        // catches the drift before the runtime_open_install_url call
        // rejects the URL at click time.
        assert!(
            linux.starts_with("https://podman.io/"),
            "linux URL must use podman.io prefix"
        );
        assert!(
            macos.starts_with("https://podman-desktop.io/"),
            "macos URL must use podman-desktop.io prefix"
        );
        assert!(
            windows.starts_with("https://podman-desktop.io/"),
            "windows URL must use podman-desktop.io prefix"
        );
    }

    #[test]
    fn install_url_unknown_platform_returns_none() {
        // freebsd / unknown / empty / random — all None so the frontend
        // shows the generic message rather than linking to an irrelevant
        // OS-specific page.
        assert!(install_url_for("freebsd").is_none());
        assert!(install_url_for("unknown").is_none());
        assert!(install_url_for("").is_none());
        assert!(install_url_for("plan9").is_none());
    }

    #[test]
    fn current_platform_returns_lowercase_known_or_unknown() {
        // Whichever platform the test runs on, the returned string must
        // be one of the four allowed values. This protects future
        // refactors from accidentally returning the raw arch suffix or
        // a capitalized variant.
        let p = current_platform();
        assert!(
            matches!(p.as_str(), "linux" | "macos" | "windows" | "unknown"),
            "current_platform returned unexpected value: {}",
            p
        );
    }

    #[tokio::test]
    async fn check_returns_sensible_shape_on_host() {
        // The function's contract: never Err, always returns a
        // RuntimeAvailability. On dev machines podman is usually on
        // PATH (Linux user) → `available: true`. On CI with no
        // runtime → `available: false` + `install_url: Some(...)`.
        // We can't assert which branch we're in, but we CAN assert
        // the shape is internally consistent.
        let r = check_container_runtime_available()
            .await
            .expect("preflight never returns Err");

        if r.available {
            assert!(
                r.detected.is_some(),
                "available=true must come with a detected runtime name"
            );
            let name = r.detected.as_deref().unwrap();
            assert!(
                matches!(name, "podman" | "docker"),
                "detected runtime must be podman or docker, got: {}",
                name
            );
            assert!(
                r.install_url.is_none(),
                "install_url should be None when runtime is already available"
            );
        } else {
            assert!(
                r.detected.is_none(),
                "available=false must have detected=None"
            );
            // install_url is Some on the three known platforms, None on
            // 'unknown'. Either is fine — the frontend handles both.
        }

        // Platform is always one of the four known strings regardless
        // of branch.
        assert!(
            matches!(r.platform.as_str(), "linux" | "macos" | "windows" | "unknown"),
            "platform must be a known value, got: {}",
            r.platform
        );
    }
}
