// SPDX-License-Identifier: AGPL-3.0-or-later
//! v0.2.53 M-P0-7: launcher PATH augmentation integration tests.
//!
//! Verifies the public `augment_path_for_graphical_launch()` helper
//! exposed by `vct_launcher_core::services::runtime`. The same helper is
//! called from the launcher's `setup()` hook (lib.rs) before any
//! subprocess spawn.
//!
//! Why integration tests in addition to the in-module unit tests:
//!   - These exercise the helper through the crate's PUBLIC API as the
//!     launcher binary sees it (`vct_launcher_core::services::runtime::
//!     augment_path_for_graphical_launch`).
//!   - They guard against accidental visibility regression (someone
//!     making the helper `pub(crate)` while refactoring would silently
//!     break the launcher's setup call but keep the unit tests passing).
//!
//! Cross-OS behaviour summary:
//!   - macOS: prepends `/opt/homebrew/{bin,sbin}`, `$HOME/.cargo/bin`,
//!     `$HOME/.local/bin`.
//!   - Linux: prepends `$HOME/.local/bin`, `$HOME/.cargo/bin`,
//!     `/home/linuxbrew/.linuxbrew/bin`, `/snap/bin`,
//!     `/var/lib/flatpak/exports/bin`.
//!   - Windows: no-op (Explorer-launched apps inherit user PATH via
//!     registry).

use serial_test::serial;
use std::path::PathBuf;
use vct_launcher_core::services::runtime::augment_path_for_graphical_launch;

/// Calling augment twice does not duplicate entries — entries already
/// present on PATH after the first call are skipped on the second.
#[test]
#[serial]
fn augment_is_idempotent_via_public_api() {
    let original = std::env::var_os("PATH");
    std::env::set_var("PATH", "/usr/bin:/bin");

    augment_path_for_graphical_launch();
    let first = std::env::var_os("PATH").unwrap_or_default();

    augment_path_for_graphical_launch();
    let second = std::env::var_os("PATH").unwrap_or_default();

    assert_eq!(
        first, second,
        "second augment call must not modify PATH again"
    );

    match original {
        Some(p) => std::env::set_var("PATH", p),
        None => std::env::remove_var("PATH"),
    }
}

/// Entries already in the original PATH must appear in the post-augment
/// PATH AND in their original relative order. Augment-added entries
/// must come before the original entries (PREPEND semantics).
#[test]
#[serial]
fn augment_preserves_original_path_order() {
    let original = std::env::var_os("PATH");
    std::env::set_var("PATH", "/zzz_marker_a:/zzz_marker_b");

    augment_path_for_graphical_launch();
    let after = std::env::var_os("PATH").unwrap_or_default();
    let parts: Vec<PathBuf> = std::env::split_paths(&after).collect();

    let pos_a = parts
        .iter()
        .position(|p| p == &PathBuf::from("/zzz_marker_a"));
    let pos_b = parts
        .iter()
        .position(|p| p == &PathBuf::from("/zzz_marker_b"));

    assert!(pos_a.is_some(), "marker_a must still be on PATH");
    assert!(pos_b.is_some(), "marker_b must still be on PATH");
    assert!(
        pos_a.unwrap() < pos_b.unwrap(),
        "marker_a must precede marker_b after augment (original order preserved)"
    );

    match original {
        Some(p) => std::env::set_var("PATH", p),
        None => std::env::remove_var("PATH"),
    }
}

/// OS-specific candidate set must be present after augment. Asserts the
/// platform-specific contract documented in the helper's doc comment.
#[test]
#[serial]
fn augment_includes_expected_os_specific_directories() {
    let original_path = std::env::var_os("PATH");
    let original_home = std::env::var_os("HOME");

    std::env::set_var("HOME", "/tmp/vct-augment-integration-home");
    std::env::set_var("PATH", "/usr/bin:/bin");

    augment_path_for_graphical_launch();
    let after = std::env::var_os("PATH").unwrap_or_default();
    let parts: Vec<PathBuf> = std::env::split_paths(&after).collect();

    #[cfg(target_os = "macos")]
    {
        for required in &[
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/tmp/vct-augment-integration-home/.cargo/bin",
            "/tmp/vct-augment-integration-home/.local/bin",
        ] {
            assert!(
                parts.iter().any(|p| p == &PathBuf::from(required)),
                "macOS augment must include {required}; PATH={parts:?}"
            );
        }
    }

    #[cfg(target_os = "linux")]
    {
        for required in &[
            "/tmp/vct-augment-integration-home/.local/bin",
            "/tmp/vct-augment-integration-home/.cargo/bin",
            "/home/linuxbrew/.linuxbrew/bin",
            "/snap/bin",
            "/var/lib/flatpak/exports/bin",
        ] {
            assert!(
                parts.iter().any(|p| p == &PathBuf::from(required)),
                "Linux augment must include {required}; PATH={parts:?}"
            );
        }
    }

    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        // Windows + other targets: augment must be a no-op so the
        // baseline PATH is unchanged.
        assert_eq!(
            parts,
            vec![PathBuf::from("/usr/bin"), PathBuf::from("/bin")],
            "non-{{macOS, Linux}} augment must be a no-op"
        );
    }

    match original_path {
        Some(p) => std::env::set_var("PATH", p),
        None => std::env::remove_var("PATH"),
    }
    match original_home {
        Some(h) => std::env::set_var("HOME", h),
        None => std::env::remove_var("HOME"),
    }
}
