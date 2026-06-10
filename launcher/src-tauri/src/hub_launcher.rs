//! Launcher-side helper to bring up the detached `vct-hub` binary.
//!
//! v0.2.21 Step 6. Replaces the in-process `hub::server::start_hub_
//! server` call that Step 4 stubbed out. The launcher remains
//! responsible for ENSURING the hub is running (so users opening the
//! GUI see the hub immediately even on a clean machine where install.
//! py hasn't yet registered boot-time auto-start), but it does NOT
//! own the hub's lifecycle past spawning — `vct-hub --stop` from
//! launcher quit would defeat the "hub outlives launcher GUI" goal.
//!
//! Discovery chain (must match the SessionStart hook in
//! `templates/hooks/session-start-ensure-hub.sh`):
//!   1. `$VCT_HUB_BIN` env override (highest priority — dev builds).
//!   2. First `vct-hub` on PATH.
//!   3. `$HOME/.vct/bin/vct-hub` (install.py default install location).
//!   4. `<orchestrator_root>/launcher/dist/<arch>/vct-hub` (sibling of
//!      this launcher binary; populated by `build-bundled-launcher.sh`).
//!   5. `<orchestrator_root>/launcher/dist/vct-hub` (arch-less fallback).
//!
//! Invocation: `vct-hub --start-if-not-running`. The CLI returns 0
//! whether the hub started fresh OR was already running; both are
//! success states for us.
//!
//! Soft-fail throughout: a missing binary, a failed spawn, or a non-
//! zero exit are all just `eprintln!` warnings — never block the
//! launcher GUI from coming up. The hub being unavailable degrades
//! the launcher to "hub-unavailable mode" (resolver falls back to
//! env vars; supervisor doesn't run) but the GUI still works.

use std::path::PathBuf;
use std::process::{Command, Stdio};

/// Find the vct-hub binary on disk, returning the first hit from the
/// documented discovery chain. Returns `None` if no candidate exists.
pub fn find_hub_binary() -> Option<PathBuf> {
    // 1. Explicit override.
    if let Ok(p) = std::env::var("VCT_HUB_BIN") {
        let path = PathBuf::from(&p);
        if is_executable(&path) {
            return Some(path);
        }
        eprintln!(
            "[vct] VCT_HUB_BIN set to {} but not executable; falling through",
            p
        );
    }

    // 2. PATH lookup.
    if let Some(on_path) = find_on_path("vct-hub") {
        return Some(on_path);
    }

    // 3. User-install location.
    if let Some(home) = std::env::var_os("HOME").map(PathBuf::from) {
        let candidate = home.join(".vct").join("bin").join(hub_binary_name());
        if is_executable(&candidate) {
            return Some(candidate);
        }
    }
    // Windows: USERPROFILE rather than HOME.
    #[cfg(windows)]
    if let Some(profile) = std::env::var_os("USERPROFILE").map(PathBuf::from) {
        let candidate = profile.join(".vct").join("bin").join(hub_binary_name());
        if is_executable(&candidate) {
            return Some(candidate);
        }
    }

    // 4 + 5. In-tree dist relative to the running launcher binary.
    // Both shipped (4-level walk-up) and dev (`cargo run`) layouts are
    // handled by the discovery in `commands::modules::
    // find_orchestrator_manifest`, but we don't have access to its
    // current_exe walking here; instead we re-derive both candidates.
    //
    // v0.2.53 test-isolation gate: when running under `cargo test`,
    // `current_exe()` points into `target/debug/deps/` whose grandparent
    // is `target/debug/` — and sibling cargo invocations leave a real
    // `vct-hub` binary there. That makes deterministic "no hub anywhere"
    // tests impossible (find_hub_binary_returns_none_when_nothing_resolves
    // + ensure_hub_running_reports_binary_not_found_in_clean_env +
    // find_hub_binary_falls_through_when_override_is_not_executable).
    // Setting `VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY=1` skips steps 4+5;
    // production code never sets this so it's a no-op there.
    if std::env::var_os("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY").is_some() {
        return None;
    }
    if let Ok(exe) = std::env::current_exe() {
        // Sibling layout: same dir as the launcher binary contains
        // vct-hub too (this is what `build-bundled-launcher.sh`
        // produces).
        if let Some(parent) = exe.parent() {
            let sibling = parent.join(hub_binary_name());
            if is_executable(&sibling) {
                return Some(sibling);
            }
            // 5. arch-less fallback one dir up (some packaging layouts).
            if let Some(grandparent) = parent.parent() {
                let fallback = grandparent.join(hub_binary_name());
                if is_executable(&fallback) {
                    return Some(fallback);
                }
            }
        }
    }

    None
}

/// `vct-hub` on POSIX, `vct-hub.exe` on Windows.
fn hub_binary_name() -> &'static str {
    if cfg!(windows) {
        "vct-hub.exe"
    } else {
        "vct-hub"
    }
}

/// Is `path` an executable regular file? On Unix we also check the
/// owner-execute bit; on Windows we only check existence + is_file
/// (file association determines runnability).
fn is_executable(path: &std::path::Path) -> bool {
    let Ok(meta) = std::fs::metadata(path) else {
        return false;
    };
    if !meta.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        meta.permissions().mode() & 0o111 != 0
    }
    #[cfg(not(unix))]
    {
        true
    }
}

/// Walk `$PATH` looking for `name` (with `.exe` suffix on Windows).
fn find_on_path(name: &str) -> Option<PathBuf> {
    let Some(path_env) = std::env::var_os("PATH") else {
        return None;
    };
    let needle = if cfg!(windows) && !name.ends_with(".exe") {
        format!("{}.exe", name)
    } else {
        name.to_string()
    };
    for dir in std::env::split_paths(&path_env) {
        let candidate = dir.join(&needle);
        if is_executable(&candidate) {
            return Some(candidate);
        }
    }
    None
}

/// Outcome of an attempted start.
#[derive(Debug, PartialEq, Eq)]
pub enum SpawnOutcome {
    /// `vct-hub --start-if-not-running` returned 0 (started fresh or
    /// was already running — both success).
    Started,
    /// Binary not found on this machine; degraded mode.
    BinaryNotFound,
    /// Binary found but exec failed (permissions, missing libraries).
    SpawnFailed(String),
    /// Spawn succeeded but `--start-if-not-running` exited non-zero.
    HubReportedError(i32),
}

/// Attempt to bring up the detached vct-hub. Best-effort; never
/// returns Err — the launcher's setup must continue even if the hub
/// can't start (see module docs for the "degraded mode" contract).
pub fn ensure_hub_running() -> SpawnOutcome {
    let Some(bin) = find_hub_binary() else {
        eprintln!(
            "[vct] vct-hub binary not found on this machine; \
             launcher will run in hub-unavailable degraded mode. \
             Set VCT_HUB_BIN or run install.py to deploy it."
        );
        return SpawnOutcome::BinaryNotFound;
    };
    eprintln!("[vct] auto-starting vct-hub from {}", bin.display());

    // Invoke synchronously so we know whether the spawn succeeded.
    // `--start-if-not-running` itself spawns a detached child and
    // returns quickly (within ~100ms on the smoke test in Step 5);
    // it does NOT block waiting for the hub to bind a port.
    //
    // We deliberately drop stdio so any noise from the child doesn't
    // pollute the launcher's logs. The hub writes its own log.
    //
    // CREATE_NO_WINDOW (0x08000000) on Windows: without it the vct-hub
    // child spawned from a `windows_subsystem = "windows"` parent
    // allocates a fresh conhost.exe console that flashes on screen for
    // the hub's ~100ms startup window. ensure_hub_running is called once
    // at every launcher boot, so this is one of the visible-flash sources
    // we audited 2026-05-26.
    let mut cmd = Command::new(&bin);
    cmd.arg("--start-if-not-running")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000);
    }
    let result = cmd.status();

    match result {
        Ok(status) if status.success() => SpawnOutcome::Started,
        Ok(status) => {
            let code = status.code().unwrap_or(-1);
            eprintln!(
                "[vct] vct-hub --start-if-not-running exited {}; degraded mode",
                code
            );
            SpawnOutcome::HubReportedError(code)
        }
        Err(e) => {
            let msg = format!("{}", e);
            eprintln!(
                "[vct] failed to spawn vct-hub from {}: {}; degraded mode",
                bin.display(),
                msg
            );
            SpawnOutcome::SpawnFailed(msg)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    static SERIALIZE: Mutex<()> = Mutex::new(());

    fn with_env<F: FnOnce()>(vars: &[(&str, Option<&str>)], f: F) {
        let _g = SERIALIZE.lock().unwrap_or_else(|p| p.into_inner());
        let saved: Vec<(String, Option<std::ffi::OsString>)> = vars
            .iter()
            .map(|(k, _)| (k.to_string(), std::env::var_os(k)))
            .collect();
        for (k, v) in vars {
            unsafe {
                match v {
                    Some(val) => std::env::set_var(k, val),
                    None => std::env::remove_var(k),
                }
            }
        }
        f();
        for (k, v) in saved {
            unsafe {
                match v {
                    Some(val) => std::env::set_var(&k, val),
                    None => std::env::remove_var(&k),
                }
            }
        }
    }

    #[test]
    fn is_executable_returns_false_for_missing_path() {
        assert!(!is_executable(std::path::Path::new(
            "/definitely/not/a/real/path/vct-hub"
        )));
    }

    #[test]
    fn is_executable_returns_false_for_directory() {
        let tmp = tempfile::tempdir().unwrap();
        assert!(!is_executable(tmp.path()));
    }

    #[test]
    fn find_hub_binary_returns_explicit_override_when_executable() {
        let tmp = tempfile::tempdir().unwrap();
        let exe = tmp.path().join("vct-hub-fake");
        std::fs::write(&exe, "#!/bin/sh\nexit 0\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&exe, std::fs::Permissions::from_mode(0o755)).unwrap();
        }

        with_env(
            &[
                ("VCT_HUB_BIN", Some(exe.to_str().unwrap())),
                ("PATH", Some("/nonexistent-dir")),
                ("HOME", Some("/nonexistent-home")),
            ],
            || {
                let found = find_hub_binary().expect("override resolves");
                assert_eq!(found, exe);
            },
        );
    }

    #[test]
    fn find_hub_binary_falls_through_when_override_is_not_executable() {
        let tmp = tempfile::tempdir().unwrap();
        let nonexec = tmp.path().join("does-not-exist");
        with_env(
            &[
                ("VCT_HUB_BIN", Some(nonexec.to_str().unwrap())),
                ("PATH", Some("/nonexistent-dir")),
                ("HOME", Some("/nonexistent-home")),
                // v0.2.53: disable current_exe()-based discovery so the
                // `target/debug/vct-hub` binary other cargo runs leave behind
                // doesn't poison this test. Production never sets this var.
                ("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1")),
            ],
            || {
                // No legitimate hub anywhere → None.
                assert_eq!(find_hub_binary(), None);
            },
        );
    }

    #[test]
    fn find_hub_binary_returns_none_when_nothing_resolves() {
        with_env(
            &[
                ("VCT_HUB_BIN", None),
                ("PATH", Some("/nonexistent-dir")),
                ("HOME", Some("/nonexistent-home")),
                ("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1")),
            ],
            || {
                assert_eq!(find_hub_binary(), None);
            },
        );
    }

    #[test]
    fn ensure_hub_running_reports_binary_not_found_in_clean_env() {
        with_env(
            &[
                ("VCT_HUB_BIN", None),
                ("PATH", Some("/nonexistent-dir")),
                ("HOME", Some("/nonexistent-home")),
                ("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1")),
            ],
            || {
                assert_eq!(ensure_hub_running(), SpawnOutcome::BinaryNotFound);
            },
        );
    }

    #[test]
    fn hub_binary_name_picks_per_platform_extension() {
        let n = hub_binary_name();
        #[cfg(windows)]
        assert_eq!(n, "vct-hub.exe");
        #[cfg(not(windows))]
        assert_eq!(n, "vct-hub");
    }
}
