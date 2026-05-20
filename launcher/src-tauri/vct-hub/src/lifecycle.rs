//! Lifecycle action handlers — wire the CLI commands to the lockfile +
//! HTTP IPC pieces.
//!
//! v0.2.21 Step 5. Each `run_*` function returns a `LifecycleResult`
//! that the binary's `main` translates into an exit code.

use std::process::Command as StdCommand;
use std::time::Duration;

use crate::lockfile::{self, AcquireOutcome};
use vct_launcher_core::process::pid_is_alive;

/// Process-level outcome of a lifecycle action.
#[derive(Debug)]
pub enum LifecycleResult {
    /// Action succeeded; exit 0.
    Ok,
    /// Action succeeded with a status code other than 0 (used by
    /// `--status` to distinguish running / not-running / stale).
    OkExit(i32),
    /// Action failed. The string is printed to stderr.
    Err(String),
}

/// `vct-hub --start-if-not-running`.
///
/// Probe the lockfile; if the owner is alive, exit 0 immediately
/// (idempotent — many call sites: SessionStart hook, .vscode/tasks.
/// json, launcher GUI startup, install.py post-install). If no live
/// owner, spawn a detached child of ourselves with `--foreground` and
/// return immediately so the parent process can exit. The child takes
/// over and runs as a daemon.
///
/// Race-window note: between probing and spawning, another caller
/// could also start the hub. The child's own `acquire()` (in
/// server::start_hub_server's flow — Step 6 wires it) detects that
/// and exits. So duplicate spawns self-correct without any external
/// coordination.
pub fn start_if_not_running() -> LifecycleResult {
    if let Some(pid) = lockfile::read_pid() {
        if pid_is_alive(pid) {
            eprintln!("[vct-hub] already running (pid {}); nothing to do", pid);
            return LifecycleResult::Ok;
        }
        eprintln!(
            "[vct-hub] stale lockfile (pid {} is dead); cleaning up",
            pid
        );
        let _ = lockfile::release();
    }

    let exe = match std::env::current_exe() {
        Ok(p) => p,
        Err(e) => return LifecycleResult::Err(format!("cannot resolve own path: {}", e)),
    };

    // Spawn detached. The child inherits our env (VCT_STATE_DIR,
    // VCT_HUB_PORT) so it lands in the same state-root.
    //
    // Unix: setsid() in a pre-exec hook so the child gets its own
    // session and survives parent SIGHUP.
    //
    // Windows: CREATE_NEW_PROCESS_GROUP + DETACHED_PROCESS so the
    // child detaches from this console. We set them via the
    // `windows::process::CommandExt::creation_flags` extension that
    // ships with `std`.
    let mut cmd = StdCommand::new(&exe);
    cmd.arg("--foreground");
    // Drop stdio so the child doesn't inherit pipes that prevent
    // the parent's caller from completing (think: SessionStart hook
    // running this from a Claude Code subshell — if we kept stdout/
    // stderr open, the parent wouldn't return until the child
    // wrote/closed).
    cmd.stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // Safety: setsid() is async-signal-safe per POSIX. We call it
        // in the pre_exec hook (forked child, pre-exec), where only
        // async-signal-safe calls are permitted. No allocations, no
        // mutex acquisition, no Rust runtime work.
        unsafe {
            cmd.pre_exec(|| {
                // Detach from controlling terminal + parent session.
                if libc::setsid() == -1 {
                    // setsid only fails if we're already a session
                    // leader (EPERM). Harmless.
                    let _ = std::io::Error::last_os_error();
                }
                Ok(())
            });
        }
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // 0x00000008 = DETACHED_PROCESS, 0x00000200 = CREATE_NEW_PROCESS_GROUP.
        // Combining both: child runs without a console window and is
        // immune to the parent's Ctrl-C signal.
        cmd.creation_flags(0x00000008 | 0x00000200);
    }

    match cmd.spawn() {
        Ok(child) => {
            eprintln!(
                "[vct-hub] spawned detached background hub (pid {})",
                child.id()
            );
            LifecycleResult::Ok
        }
        Err(e) => LifecycleResult::Err(format!("failed to spawn vct-hub --foreground: {}", e)),
    }
}

/// `vct-hub --stop`.
///
/// Read the lockfile. If absent → no hub running → exit 0. If present,
/// send the running hub a TERM signal (Unix) or terminate by handle
/// (Windows), then poll the lockfile for up to 10 s waiting for the
/// child to release it.
///
/// We don't go through HTTP `/api/v1/internal/shutdown` yet — Step 15
/// wires that endpoint. Until then, signal-based shutdown is the only
/// IPC the hub binary understands, which is fine because the server's
/// main task listens on SIGTERM/SIGINT (see `bin/main.rs`).
pub fn stop() -> LifecycleResult {
    let Some(pid) = lockfile::read_pid() else {
        eprintln!("[vct-hub] no lockfile; assuming hub is not running");
        return LifecycleResult::Ok;
    };

    if !pid_is_alive(pid) {
        eprintln!(
            "[vct-hub] lockfile present but owner pid {} is dead; cleaning up",
            pid
        );
        let _ = lockfile::release();
        return LifecycleResult::Ok;
    }

    // Send the polite shutdown signal.
    #[cfg(unix)]
    {
        // SIGTERM. Async-signal-safe; kill() returns -1 on failure.
        let rc = unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
        if rc != 0 {
            return LifecycleResult::Err(format!(
                "kill({}, SIGTERM): {}",
                pid,
                std::io::Error::last_os_error()
            ));
        }
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{OpenProcess, TerminateProcess, PROCESS_TERMINATE};
        // Safety: thin FFI calls. We close every handle we open. The
        // pid was validated alive via pid_is_alive above; race with a
        // newly-recycled pid is acceptable (we'd terminate an unrelated
        // process, which is the same risk every signal-based stop has —
        // POSIX has the identical race).
        unsafe {
            let handle = OpenProcess(PROCESS_TERMINATE, 0, pid);
            if handle.is_null() {
                return LifecycleResult::Err(format!(
                    "OpenProcess({}, TERMINATE): {}",
                    pid,
                    std::io::Error::last_os_error()
                ));
            }
            // Exit code 0 signals graceful exit; the hub's signal
            // handler would have used the same code.
            if TerminateProcess(handle, 0) == 0 {
                let e = std::io::Error::last_os_error();
                CloseHandle(handle);
                return LifecycleResult::Err(format!("TerminateProcess({}): {}", pid, e));
            }
            CloseHandle(handle);
        }
    }

    // Poll up to 10 s for the lockfile to disappear (which happens
    // when the child's graceful-shutdown path runs lockfile::release).
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    while std::time::Instant::now() < deadline {
        if !pid_is_alive(pid) {
            // The process is gone. The graceful-shutdown path SHOULD
            // have removed the lockfile; if it didn't, do it now.
            let _ = lockfile::release();
            eprintln!("[vct-hub] stopped (pid {})", pid);
            return LifecycleResult::Ok;
        }
        std::thread::sleep(Duration::from_millis(100));
    }

    LifecycleResult::Err(format!(
        "hub pid {} did not exit within 10 s of SIGTERM",
        pid
    ))
}

/// `vct-hub --status`.
///
/// Single-line stdout output + structured exit code:
///   running pid=<N>      → exit 0
///   not-running          → exit 1
///   stale pid=<N>        → exit 2
pub fn status() -> LifecycleResult {
    match lockfile::read_pid() {
        Some(pid) if pid_is_alive(pid) => {
            println!("running pid={}", pid);
            LifecycleResult::Ok
        }
        Some(pid) => {
            println!("stale pid={}", pid);
            LifecycleResult::OkExit(2)
        }
        None => {
            println!("not-running");
            LifecycleResult::OkExit(1)
        }
    }
}

/// Convenience: claim the lockfile or report why we can't. Used by the
/// `Foreground` path in `main.rs` before binding the listener.
pub fn try_acquire_or_exit() -> Result<(), LifecycleResult> {
    match lockfile::acquire() {
        Ok(AcquireOutcome::Claimed) => Ok(()),
        Ok(AcquireOutcome::AlreadyRunning { pid }) => {
            Err(LifecycleResult::Err(format!(
                "another vct-hub is already running (pid {}); refusing to start. \
                 Use `vct-hub --stop` to shut it down first.",
                pid
            )))
        }
        Err(e) => Err(LifecycleResult::Err(format!(
            "failed to acquire lockfile: {}",
            e
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // The actual lifecycle integration is exercised by the lockfile
    // module's tests + the server's auth tests + a separate process-
    // level smoke test (Step 23 lands an integration test that spawns
    // the real binary and asserts start/status/stop transitions).
    //
    // Here we only cover the pure-function pieces: the `status`
    // mapping is deterministic given a lockfile state.

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
    fn status_reports_not_running_when_no_lockfile() {
        with_state_dir(|_root| {
            match status() {
                LifecycleResult::OkExit(1) => {}
                other => panic!("expected OkExit(1), got {:?}", other),
            }
        });
    }

    #[test]
    fn status_reports_running_when_own_pid_is_in_lockfile() {
        with_state_dir(|_root| {
            lockfile::write_pid(std::process::id()).unwrap();
            match status() {
                LifecycleResult::Ok => {}
                other => panic!("expected Ok, got {:?}", other),
            }
        });
    }

    #[test]
    fn status_reports_stale_when_sentinel_pid_in_lockfile() {
        with_state_dir(|_root| {
            // u32::MAX is rejected by pid_is_alive as a sentinel.
            lockfile::write_pid(u32::MAX).unwrap();
            match status() {
                LifecycleResult::OkExit(2) => {}
                other => panic!("expected OkExit(2), got {:?}", other),
            }
        });
    }
}
