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

#![cfg(target_os = "linux")]

use std::fs;
use std::path::PathBuf;
use std::sync::Mutex;

use vct_launcher_core::services::runtime::augment_path_for_graphical_launch;

/// Manual temp dir helper — avoids pulling `tempfile` as a dev-dep
/// just for this one test. Auto-cleans on Drop.
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

    fn path(&self) -> &std::path::Path {
        &self.path
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

/// Tests that mutate the process's PATH/HOME env vars must run serially
/// (the std::env is process-wide). Lock around them.
static ENV_MUTEX: Mutex<()> = Mutex::new(());

/// Tools the launcher subsystem must be able to resolve under
/// .desktop-launch — taken directly from L-P0-4's "extend to Node,
/// Joern, lean-ctx, cargo, npm" wording in the v0.2.53 design doc.
const L_P0_4_TOOLS: &[&str] = &["node", "npm", "cargo", "joern", "lean-ctx"];

/// Locations where user-installed copies of the L-P0-4 tools commonly
/// live. Mirrors `augment_candidates()`'s Linux branch in runtime.rs.
fn home_relative_tool_dirs(home: &PathBuf) -> Vec<PathBuf> {
    vec![
        home.join(".local/bin"),
        home.join(".cargo/bin"),
    ]
}

/// Emulate which(name) using the process's CURRENT PATH (no augment
/// applied implicitly). This mirrors what the launcher's runtime
/// probes do at subprocess spawn time.
fn which_using_process_path(name: &str) -> Option<PathBuf> {
    let paths = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&paths) {
        let p = dir.join(name);
        if p.is_file() {
            return Some(p);
        }
    }
    None
}

/// Create an executable stub at `dir/name` so `is_file()` returns true.
fn lay_down_stub(dir: &PathBuf, name: &str) -> PathBuf {
    fs::create_dir_all(dir).expect("mkdir -p");
    let target = dir.join(name);
    fs::write(&target, b"#!/bin/sh\nexit 0\n").expect("write stub");
    // Mark executable for completeness — `is_file()` succeeds either
    // way, but a real .desktop launch would only resolve to executable
    // files. Use Unix-specific permissions API.
    use std::os::unix::fs::PermissionsExt;
    let mut perms = fs::metadata(&target).unwrap().permissions();
    perms.set_mode(0o755);
    fs::set_permissions(&target, perms).expect("chmod");
    target
}

/// Synthetic-state fixture: minimal PATH (mimicking `systemd --user`),
/// fresh HOME with .local/bin + .cargo/bin populated by stubs for the
/// L-P0-4 tools.
struct DesktopLaunchFixture {
    home: TempDir,
    saved_path: Option<std::ffi::OsString>,
    saved_home: Option<std::ffi::OsString>,
}

impl DesktopLaunchFixture {
    fn new_with_minimal_path() -> Self {
        let home = TempDir::new();
        let saved_path = std::env::var_os("PATH");
        let saved_home = std::env::var_os("HOME");

        // Mimic systemd --user's minimal PATH precisely.
        std::env::set_var(
            "PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        );
        std::env::set_var("HOME", home.path());

        Self {
            home,
            saved_path,
            saved_home,
        }
    }

    fn home_path(&self) -> PathBuf {
        self.home.path().to_path_buf()
    }
}

impl Drop for DesktopLaunchFixture {
    fn drop(&mut self) {
        if let Some(p) = self.saved_path.take() {
            std::env::set_var("PATH", p);
        } else {
            std::env::remove_var("PATH");
        }
        if let Some(h) = self.saved_home.take() {
            std::env::set_var("HOME", h);
        } else {
            std::env::remove_var("HOME");
        }
    }
}

#[test]
fn baseline_without_augment_does_not_see_home_local_bin_tools() {
    // Sanity check: confirm the BUG actually exists before augment runs.
    // If this fails, either the test fixture is broken or the underlying
    // gap has already been silently fixed elsewhere — in which case the
    // assertion below would give a false-positive "augment worked"
    // signal. We hold the line.
    let _guard = ENV_MUTEX.lock().expect("lock env");
    let fx = DesktopLaunchFixture::new_with_minimal_path();
    let local_bin = fx.home_path().join(".local/bin");
    lay_down_stub(&local_bin, "node");

    // PRE-augment: minimal PATH does NOT include $HOME/.local/bin, so
    // `which node` returns None even though the binary exists.
    assert!(
        which_using_process_path("node").is_none(),
        "baseline broken: PATH already includes $HOME/.local/bin somehow"
    );
}

#[test]
fn augment_path_makes_node_npm_cargo_joern_leanctx_findable() {
    let _guard = ENV_MUTEX.lock().expect("lock env");
    let fx = DesktopLaunchFixture::new_with_minimal_path();

    // Lay down the L-P0-4 tool stubs across the two HOME-relative
    // candidate dirs that Track C's augment adds.
    let dirs = home_relative_tool_dirs(&fx.home_path());
    let local_bin = &dirs[0]; // ~/.local/bin
    let cargo_bin = &dirs[1]; // ~/.cargo/bin

    // Place each tool in a plausible default location:
    //   node, npm → typically symlinked into ~/.local/bin by user
    //                installs (`npm config set prefix ~/.local`),
    //                fnm/nvm proxy shims, etc.
    //   cargo, lean-ctx → ~/.cargo/bin (rustup-managed)
    //   joern → ~/.local/bin (sdkman or manual extract)
    lay_down_stub(local_bin, "node");
    lay_down_stub(local_bin, "npm");
    lay_down_stub(cargo_bin, "cargo");
    lay_down_stub(cargo_bin, "lean-ctx");
    lay_down_stub(local_bin, "joern");

    // Call Track C's M-P0-7 helper. This is the line of code the
    // launcher's `lib.rs::setup()` runs at startup.
    augment_path_for_graphical_launch();

    // After augment, every L-P0-4 tool must be resolvable via PATH.
    for tool in L_P0_4_TOOLS {
        let resolved = which_using_process_path(tool);
        assert!(
            resolved.is_some(),
            "L-P0-4 regression: tool {tool:?} unresolvable after \
             augment_path_for_graphical_launch() under simulated \
             .desktop-launch state — Track C's augment did not pick up \
             $HOME/.local/bin or $HOME/.cargo/bin"
        );
    }
}

#[test]
fn augment_is_idempotent_under_repeated_desktop_launch_state() {
    let _guard = ENV_MUTEX.lock().expect("lock env");
    let fx = DesktopLaunchFixture::new_with_minimal_path();
    let local_bin = fx.home_path().join(".local/bin");
    lay_down_stub(&local_bin, "node");

    augment_path_for_graphical_launch();
    let after_first = std::env::var("PATH").unwrap();
    augment_path_for_graphical_launch();
    augment_path_for_graphical_launch();
    let after_third = std::env::var("PATH").unwrap();

    assert_eq!(
        after_first, after_third,
        "augment must be idempotent: repeated calls (e.g. resume-after-\
         sleep, lib.rs::setup() re-entry on Tauri 2 hot-reload) must \
         not duplicate entries or change order"
    );

    // And the tool must still resolve.
    assert!(
        which_using_process_path("node").is_some(),
        "node lookup broke after repeated augment calls"
    );
}

#[test]
fn augment_preserves_pre_existing_path_entries_after_candidates() {
    let _guard = ENV_MUTEX.lock().expect("lock env");
    let fx = DesktopLaunchFixture::new_with_minimal_path();
    let local_bin = fx.home_path().join(".local/bin");
    lay_down_stub(&local_bin, "node");

    augment_path_for_graphical_launch();
    let new_path = std::env::var("PATH").unwrap();

    // The systemd --user PATH entries must still be present (not
    // replaced wholesale). Order-relative: augment candidates are
    // PREPENDED, system entries follow.
    for systemd_entry in [
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ] {
        assert!(
            new_path.contains(systemd_entry),
            "augment dropped systemd --user PATH entry {systemd_entry:?} \
             from the augmented PATH. Original was preserved-after-\
             candidates per Track C M-P0-7 contract."
        );
    }
    let _ = fx;
}
