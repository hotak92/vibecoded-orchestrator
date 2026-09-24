// SPDX-License-Identifier: AGPL-3.0-or-later
//! v0.2.53 L-P0-4 (Track G3) — Linux .desktop-launch PATH augmentation
//! end-to-end contract.
//!
//! The `which_on_path()` helper in `runtime.rs` (private) is the bedrock
//! lookup that every subsequent runtime probe (`detect_podman`,
//! `detect_docker`, `detect_compose_form`, plus the launcher's
//! python3 / git / cargo / joern / lean-ctx spawns) ultimately uses.
//! It reads the calling process's PATH env var directly.
//!
//! On Linux, when the launcher is started by activating
//! `vct-launcher.desktop` from the GNOME / KDE menu (or by file-manager
//! double-click), the inherited PATH from `systemd --user` is minimal:
//!
//!     /usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
//!
//! Common user-installed tooling lives outside that PATH:
//!
//!     $HOME/.local/bin    — pipx, pip --user, manual installs
//!     $HOME/.cargo/bin    — rustup, cargo, lean-ctx
//!     /home/linuxbrew/.linuxbrew/bin  — Linuxbrew (joern, node)
//!     /snap/bin           — snap-installed CLIs
//!     /var/lib/flatpak/exports/bin    — flatpak CLI proxies
//!
//! Without a PATH augment at launcher startup, every lookup of `node`,
//! `npm`, `cargo`, `joern`, `lean-ctx` (when installed in the above
//! locations) would silently fail under .desktop launch.
//!
//! Track C's M-P0-7 (commit bb9c9daf in v0.2.53 chore/v0253-track-c)
//! adds `vct_launcher_core::services::runtime::augment_path_for_graphical_launch`
//! which prepends those candidate dirs and is called from
//! `lib.rs::setup()` BEFORE any subprocess spawn. Track G3's L-P0-4 is
//! the SAME ROOT CAUSE; we defer to Track C's helper rather than
//! duplicating the augment logic.
//!
//! This integration test asserts the end-to-end contract from a Track
//! G3 lens: given a synthetic minimal-PATH process state, calling
//! `augment_path_for_graphical_launch()` plus laying down fake binaries
//! in $HOME/.local/bin AND $HOME/.cargo/bin, the public PATH-driven
//! lookup must successfully resolve every binary the launcher needs:
//! node, npm, cargo, joern, lean-ctx.
//!
//! The test deliberately exercises the public surface only (no private
//! `which_on_path` access), so it survives Track C / Track G3
//! integration without coupling to internal symbols.
//!
//! ## Why this lives in Track G3 (not Track C)
//!
//! Track C's own integration test
//! (`tests/test_launcher_path_augmentation.rs`) verifies the augment
//! BEHAVIOR (idempotence, ordering, OS-specific candidates). This test
//! verifies the AUDIT CONTRACT named in L-P0-4 of
//! `linux-comprehensive-audit-2026-06-10.md`: the specific tools
//! `node`, `npm`, `cargo`, `joern`, `lean-ctx` are findable from a
//! .desktop-launched process state. Two angles, same root fix.

//! v0.2.97 review R6: every test computes the augmented PATH with the
//! PURE `augmented_path(minimal PATH, fixture HOME)` and resolves tools
//! against THAT value — a Rust test never sets the process `PATH` or
//! `HOME` (`tests/test_rust_tests_never_mutate_process_path.py`).

#![cfg(target_os = "linux")]

use std::ffi::{OsStr, OsString};
use std::fs;
use std::path::{Path, PathBuf};

use vct_launcher_core::services::runtime::augmented_path;

struct TempDir {
    path: PathBuf,
}

impl TempDir {
    fn new() -> Self {
        let mut p = std::env::temp_dir();
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        p.push(format!("vct-test-{}-{}", std::process::id(), nanos));
        fs::create_dir_all(&p).expect("mkdir tempdir");
        Self { path: p }
    }

    fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

const L_P0_4_TOOLS: &[&str] = &["node", "npm", "cargo", "joern", "lean-ctx"];

/// `systemd --user`'s minimal PATH, precisely.
const SYSTEMD_USER_PATH: &str = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";

fn home_relative_tool_dirs(home: &Path) -> Vec<PathBuf> {
    vec![home.join(".local/bin"), home.join(".cargo/bin")]
}

/// `which` against an explicit PATH value (never the process's).
fn which_in(path: &OsStr, name: &str) -> Option<PathBuf> {
    std::env::split_paths(path).map(|dir| dir.join(name)).find(|p| p.is_file())
}

fn lay_down_stub(dir: &Path, name: &str) -> PathBuf {
    fs::create_dir_all(dir).expect("mkdir -p");
    let p = dir.join(name);
    fs::write(&p, b"#!/bin/sh\nexit 0\n").expect("write stub");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut perm = fs::metadata(&p).expect("stat").permissions();
        perm.set_mode(0o755);
        fs::set_permissions(&p, perm).expect("chmod");
    }
    p
}

/// The PATH the launcher has after its startup augment under a
/// `.desktop` launch with `home` as `$HOME`.
fn desktop_launch_path(home: &Path) -> OsString {
    augmented_path(OsStr::new(SYSTEMD_USER_PATH), Some(home))
        .unwrap_or_else(|| OsString::from(SYSTEMD_USER_PATH))
}

#[test]
fn baseline_without_augment_does_not_see_home_local_bin_tools() {
    // Sanity check: confirm the BUG actually exists before augment runs.
    // A unique, host-absent stub name: `node`/`npm`/`cargo` are
    // pre-installed at /usr/local/bin on GitHub `ubuntu-latest` runners.
    let home = TempDir::new();
    lay_down_stub(&home.path().join(".local/bin"), "vct_l_p0_4_stub_marker");
    assert!(
        which_in(OsStr::new(SYSTEMD_USER_PATH), "vct_l_p0_4_stub_marker").is_none(),
        "baseline broken: PATH already includes $HOME/.local/bin somehow"
    );
}

#[test]
fn augment_path_makes_node_npm_cargo_joern_leanctx_findable() {
    let home = TempDir::new();
    let dirs = home_relative_tool_dirs(home.path());
    let local_bin = &dirs[0]; // ~/.local/bin
    let cargo_bin = &dirs[1]; // ~/.cargo/bin
    lay_down_stub(local_bin, "node");
    lay_down_stub(local_bin, "npm");
    lay_down_stub(cargo_bin, "cargo");
    lay_down_stub(cargo_bin, "lean-ctx");
    lay_down_stub(local_bin, "joern");

    let path = desktop_launch_path(home.path());
    for tool in L_P0_4_TOOLS {
        assert!(
            which_in(&path, tool).is_some(),
            "L-P0-4 regression: tool {tool:?} unresolvable after the startup \
             augment under simulated .desktop-launch state — it did not pick \
             up $HOME/.local/bin or $HOME/.cargo/bin"
        );
    }
}

#[test]
fn augment_is_idempotent_under_repeated_desktop_launch_state() {
    let home = TempDir::new();
    lay_down_stub(&home.path().join(".local/bin"), "node");

    let after_first = desktop_launch_path(home.path());
    assert!(
        augmented_path(&after_first, Some(home.path())).is_none(),
        "augment must be idempotent: repeated calls (e.g. resume-after-\
         sleep, lib.rs::setup() re-entry on Tauri 2 hot-reload) must \
         not duplicate entries or change order"
    );
    assert!(which_in(&after_first, "node").is_some(), "node lookup broke");
}

#[test]
fn augment_preserves_pre_existing_path_entries_after_candidates() {
    let home = TempDir::new();
    let new_path = desktop_launch_path(home.path()).to_string_lossy().to_string();
    // The systemd --user PATH entries must still be present (not replaced
    // wholesale); augment candidates are PREPENDED, system entries follow.
    for systemd_entry in ["/usr/local/bin", "/usr/bin", "/bin"] {
        assert!(
            new_path.contains(systemd_entry),
            "augment dropped systemd --user PATH entry {systemd_entry:?}"
        );
    }
}
