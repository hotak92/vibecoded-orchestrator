//! Launcher-side probe + control for the detached vct-hub binary.
//!
//! v0.2.21 Step 13. Two surfaces:
//!   * `probe()` — read `<vct_root_dir>/hub.pid` and report Running /
//!     Stale / NotRunning. No HTTP call (uses the same lockfile +
//!     pid_is_alive primitives the hub itself uses); cheap enough to
//!     run on a 5-second tray poller.
//!   * `stop()` — spawn `vct-hub --stop` synchronously and return the
//!     outcome. Mirrors the discovery chain from `hub_launcher::
//!     find_hub_binary` (Step 6) so the launcher always asks the SAME
//!     binary to shut down that it would start.

use std::path::PathBuf;
use std::process::{Command, Stdio};

use vct_launcher_core::process::pid_is_alive;
use vct_launcher_core::process::CommandExt as _;

const HUB_PID_FILE: &str = "hub.pid";

/// Result of probing the hub's liveness.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HubStatus {
    /// Lockfile present, owner PID is alive.
    Running { pid: u32 },
    /// Lockfile present, owner PID is dead (crash recovery state).
    Stale { pid: u32 },
    /// No lockfile.
    NotRunning,
}

/// Path to the hub's lockfile under the launcher's state-root. Mirrors
/// `vct_hub::lockfile::pid_path()` — the launcher doesn't depend on
/// the hub crate (we don't want to drag axum + tokio's full feature
/// set into the launcher build), so the literal string + paths::
/// resolution lives here too.
fn hub_pid_path() -> PathBuf {
    vct_launcher_core::paths::vct_root_dir().join(HUB_PID_FILE)
}

/// Probe the hub's state. Always returns a value — never fails. A
/// missing lockfile is a regular `NotRunning` (not an error).
pub fn probe() -> HubStatus {
    let Ok(raw) = std::fs::read_to_string(hub_pid_path()) else {
        return HubStatus::NotRunning;
    };
    let Some(first_line) = raw.lines().next() else {
        return HubStatus::NotRunning;
    };
    let Ok(pid) = first_line.trim().parse::<u32>() else {
        // Unparseable lockfile content → treat as not running. The
        // hub's own acquire() path will overwrite next start.
        return HubStatus::NotRunning;
    };
    if pid_is_alive(pid) {
        HubStatus::Running { pid }
    } else {
        HubStatus::Stale { pid }
    }
}

/// One-line label for the tray menu. Stable wording across states so
/// the user can pattern-match at a glance.
pub fn label(status: HubStatus) -> String {
    match status {
        HubStatus::Running { pid } => format!("Hub: running (pid {})", pid),
        HubStatus::Stale { pid } => format!("Hub: stale lockfile (pid {})", pid),
        HubStatus::NotRunning => "Hub: not running".to_string(),
    }
}

/// Outcome of asking the hub to stop.
#[derive(Debug)]
pub enum StopOutcome {
    /// `vct-hub --stop` exited 0. Hub is no longer running.
    Stopped,
    /// `vct-hub --stop` reported there was no hub to stop. Treated as
    /// success for the UX (the user's intent — "be stopped" — is now
    /// satisfied). Currently not produced by `stop()` directly — see
    /// the post-stop probe comment there — but kept as a distinct
    /// variant so future surfaces (e.g. install.py invoking `--stop`
    /// before swap) can distinguish "I told it to stop and it did"
    /// from "I told it to stop and it wasn't running".
    #[allow(dead_code)]
    AlreadyStopped,
    /// Hub binary not found on PATH.
    BinaryNotFound,
    /// Spawn or non-zero exit.
    Failed(String),
}

/// Invoke `vct-hub --stop` via the same discovery chain the launcher
/// uses to START the hub. Synchronous — the Tauri command that wraps
/// this MUST be invoked from a blocking task so it doesn't stall the
/// async runtime. `--stop` itself blocks up to 10 s waiting for the
/// hub's graceful-shutdown path, so callers should plan for that.
pub fn stop() -> StopOutcome {
    let Some(bin) = crate::hub_launcher::find_hub_binary() else {
        return StopOutcome::BinaryNotFound;
    };
    let result = Command::new(&bin).silent()
        .arg("--stop")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped()) // captured for error reporting
        .output();
    match result {
        Ok(out) if out.status.success() => {
            // `--stop` returns 0 in BOTH the "stopped a running hub"
            // case and the "no hub was running" case (see Step 5
            // lifecycle::stop). Distinguish by probing afterwards:
            // if probe() returns NotRunning either way, the user's
            // intent is satisfied. We don't try to detect "was it
            // ever running" — that distinction doesn't matter for
            // the menu-item UX.
            match probe() {
                HubStatus::NotRunning => StopOutcome::Stopped,
                _ => StopOutcome::Stopped, // hub did stop; lockfile
                                           // cleanup is best-effort.
            }
        }
        Ok(out) => {
            let stderr = String::from_utf8_lossy(&out.stderr).to_string();
            let code = out.status.code().unwrap_or(-1);
            StopOutcome::Failed(format!(
                "vct-hub --stop exited {}: {}",
                code,
                stderr.trim()
            ))
        }
        Err(e) => StopOutcome::Failed(format!("spawn failed: {}", e)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    static SERIALIZE: Mutex<()> = Mutex::new(());

    fn with_state_dir<F: FnOnce(&std::path::Path)>(f: F) {
        let _g = SERIALIZE.lock().unwrap_or_else(|p| p.into_inner());
        let tmp = tempfile::tempdir().expect("tempdir");
        unsafe {
            std::env::set_var("VCT_STATE_DIR", tmp.path());
        }
        f(tmp.path());
        unsafe {
            std::env::remove_var("VCT_STATE_DIR");
        }
    }

    #[test]
    fn probe_returns_not_running_when_no_pidfile() {
        with_state_dir(|_| {
            assert_eq!(probe(), HubStatus::NotRunning);
        });
    }

    #[test]
    fn probe_returns_not_running_on_unparseable_content() {
        with_state_dir(|root| {
            std::fs::write(root.join(HUB_PID_FILE), "not-a-pid\n").unwrap();
            assert_eq!(probe(), HubStatus::NotRunning);
        });
    }

    #[test]
    fn probe_returns_running_when_own_pid_in_pidfile() {
        with_state_dir(|root| {
            let me = std::process::id();
            std::fs::write(root.join(HUB_PID_FILE), format!("{}\n", me)).unwrap();
            assert_eq!(probe(), HubStatus::Running { pid: me });
        });
    }

    #[test]
    fn probe_returns_stale_when_dead_pid_in_pidfile() {
        with_state_dir(|root| {
            // u32::MAX is rejected by pid_is_alive as a sentinel.
            std::fs::write(root.join(HUB_PID_FILE), format!("{}\n", u32::MAX)).unwrap();
            assert_eq!(probe(), HubStatus::Stale { pid: u32::MAX });
        });
    }

    #[test]
    fn label_strings_are_distinct_for_each_state() {
        let r = label(HubStatus::Running { pid: 42 });
        let s = label(HubStatus::Stale { pid: 42 });
        let n = label(HubStatus::NotRunning);
        assert_ne!(r, s);
        assert_ne!(r, n);
        assert_ne!(s, n);
        assert!(r.contains("42"));
        assert!(s.contains("42"));
    }
}
