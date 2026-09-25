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
//! Cross-OS behaviour summary (the table `vco_lib/tool_search_dirs.toml`;
//! every entry is added only when the PATH lacks it):
//!   - macOS: prepends `/opt/homebrew/{bin,sbin}`, `$HOME/.cargo/bin`,
//!     `$HOME/.local/bin` (the v0.2.53 graphical-launch list); appends the
//!     v0.2.97 runtime locations (`~/bin`, `/usr/local/bin`,
//!     `/opt/podman/bin`, Docker Desktop, MacPorts, `/usr/bin`).
//!   - Linux: prepends `$HOME/.local/bin`, `$HOME/.cargo/bin`,
//!     `/home/linuxbrew/.linuxbrew/bin`, `/snap/bin`,
//!     `/var/lib/flatpak/exports/bin`; appends `~/bin`, `/usr/local/bin`,
//!     `/usr/bin`.
//!   - Windows: appends the Docker Desktop / Podman installer directories
//!     (v0.2.97; Explorer-launched apps inherit the user PATH via registry).
//!
//! v0.2.97 review R6: the tests drive the PURE half, `augmented_path`,
//! with an explicit PATH and HOME — a Rust test never sets the process
//! `PATH` (`tests/test_rust_tests_never_mutate_process_path.py`). The
//! mutating wrapper is one `set_var` over it, called from `lib.rs`.

use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};
use vct_launcher_core::services::runtime::augmented_path;

// The public wrapper `lib.rs::setup()` calls must stay public too.
#[allow(dead_code)]
const _WRAPPER_IS_PUBLIC: fn() = vct_launcher_core::services::runtime::augment_path_for_graphical_launch;

/// The PATH after one augment of `current` with `home`.
fn after_augment(current: &str, home: &str) -> OsString {
    augmented_path(OsStr::new(current), Some(Path::new(home)))
        .unwrap_or_else(|| OsString::from(current))
}

/// Calling augment twice does not duplicate entries — entries already
/// present on PATH after the first call are skipped on the second.
#[test]
fn augment_is_idempotent_via_public_api() {
    let first = after_augment("/usr/bin:/bin", "/tmp/vct-augment-integration-home");
    assert!(
        augmented_path(&first, Some(Path::new("/tmp/vct-augment-integration-home"))).is_none(),
        "second augment call must not modify PATH again"
    );
}

/// Entries already in the original PATH must appear in the post-augment
/// PATH AND in their original relative order (the graphical-launch entries
/// go ahead of them, the runtime locations behind them — v0.2.97 R10).
#[test]
fn augment_preserves_original_path_order() {
    let after = after_augment("/zzz_marker_a:/zzz_marker_b", "/tmp/vct-augment-integration-home");
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
}

/// OS-specific candidate set must be present after augment. Asserts the
/// platform-specific contract documented in the helper's doc comment.
#[test]
fn augment_includes_expected_os_specific_directories() {
    let after = after_augment("/usr/bin:/bin", "/tmp/vct-augment-integration-home");
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
        // Windows + other targets (v0.2.97 R9/R10): the baseline PATH,
        // unchanged, then exactly the shared table's entries for this OS
        // (the container runtimes' installer dirs, when their variables are
        // set — every one `append`).
        use vct_launcher_core::services::runtime::{
            current_os_key, tool_search_entries_for, Placement,
        };
        // The baseline split the way THIS OS splits a PATH (";" on Windows).
        let mut want: Vec<PathBuf> =
            std::env::split_paths(std::ffi::OsStr::new("/usr/bin:/bin")).collect();
        for (d, placement) in tool_search_entries_for(
            current_os_key(),
            Some("/tmp/vct-augment-integration-home"),
            &|k| std::env::var(k).ok(),
        ) {
            assert_eq!(placement, Placement::Append, "{d}");
            want.push(PathBuf::from(d));
        }
        assert_eq!(parts, want, "non-{{macOS, Linux}} augment: the PATH, then table entries");
    }
}
