//! Single-instance lockfile + atomic start/stop state machine.
//!
//! v0.2.21 Step 5. The hub writes its PID to
//! `<vct_root_dir>/hub.pid` on startup and refuses to start if another
//! live PID owns the file. `vct-hub --start-if-not-running` is the
//! canonical entry point — exits 0 whether the hub started fresh OR
//! was already running.
//!
//! Failure model:
//! - PID file missing → no hub running, claim it.
//! - PID file present, owner alive → another hub running; exit 0 if
//!   `--start-if-not-running`, error otherwise.
//! - PID file present, owner dead → stale; remove + reclaim.
//! - PID file present, unreadable → stale-equivalent; remove + reclaim
//!   (file was corrupted by a partial write).
//!
//! We deliberately do NOT use OS file-lock advisory locks (flock /
//! LockFileEx). Those are released on process death by the OS, so a
//! crashed hub leaves no trace — defeating the purpose of using the
//! lockfile to detect "is the hub still around" from outside. Plain
//! "PID file with liveness probe" is more debuggable: a user can
//! `cat ~/.vct/hub.pid` and see whether the PID matches.
//!
//! The lockfile is written with mode 0o600 on Unix — same posture as
//! `hub.token`. It contains a single integer (the PID) plus an
//! optional trailing newline. Future versions may extend the format
//! (started_at, version, port), in which case the FIRST line stays
//! the PID for backward compat with this parser.

use std::path::PathBuf;

use vct_launcher_core::paths::vct_root_dir;
use vct_launcher_core::process::pid_is_alive;

/// File where the hub persists its PID on startup.
pub const PID_FILE: &str = "hub.pid";

/// Path to the lockfile under the launcher's state-root.
pub fn pid_path() -> PathBuf {
    vct_root_dir().join(PID_FILE)
}

/// Outcome of attempting to acquire the lockfile.
#[derive(Debug, PartialEq, Eq)]
pub enum AcquireOutcome {
    /// We claimed the lockfile; safe to start the server.
    Claimed,
    /// Another live hub already holds the lockfile.
    AlreadyRunning { pid: u32 },
}

/// Read the PID currently recorded in the lockfile, if any.
///
/// Returns `None` if the file is missing, empty, or unparseable. The
/// caller is responsible for the alive-vs-stale decision.
pub fn read_pid() -> Option<u32> {
    let raw = std::fs::read_to_string(pid_path()).ok()?;
    let first = raw.lines().next()?.trim();
    if first.is_empty() {
        return None;
    }
    first.parse::<u32>().ok()
}

/// Try to claim the lockfile for the current process.
///
/// Sequence:
/// 1. Read existing PID. If absent → write our PID and return Claimed.
/// 2. If present + alive → return AlreadyRunning. Don't touch the file.
/// 3. If present + dead → overwrite with our PID, return Claimed.
///
/// Race window: between steps 2 and 3 a second hub could also see the
/// stale file. Both would overwrite with their PID; whichever runs
/// last wins. To make that race harmless, the binder downstream of
/// this function also tries `TcpListener::bind` on the hub port —
/// EADDRINUSE on the bind reveals the duplicate. So lockfile + bind
/// together form the atomic check.
pub fn acquire() -> Result<AcquireOutcome, String> {
    if let Some(existing_pid) = read_pid() {
        if pid_is_alive(existing_pid) {
            return Ok(AcquireOutcome::AlreadyRunning { pid: existing_pid });
        }
        // Dead — stale lockfile. Fall through to overwrite.
    }
    write_pid(std::process::id())?;
    Ok(AcquireOutcome::Claimed)
}

/// Persist a PID to the lockfile with mode 0o600 on Unix.
///
/// Same write-pattern as `hub.token` (single syscall O_CREAT|O_TRUNC|
/// O_WRONLY with the mode set in `OpenOptions::mode`, avoiding the
/// chmod-after-create TOCTOU window).
pub fn write_pid(pid: u32) -> Result<(), String> {
    let path = pid_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("create_dir_all {}: {}", parent.display(), e))?;
    }

    #[cfg(unix)]
    {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;

        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&path)
            .map_err(|e| format!("open {}: {}", path.display(), e))?;
        writeln!(f, "{}", pid).map_err(|e| format!("write {}: {}", path.display(), e))?;
    }

    #[cfg(not(unix))]
    {
        std::fs::write(&path, format!("{}\n", pid))
            .map_err(|e| format!("write {}: {}", path.display(), e))?;
    }

    Ok(())
}

/// Remove the lockfile. Called by the binary's graceful-shutdown path
/// (`--stop` IPC, SIGTERM/SIGINT). Best-effort: a leftover lockfile
/// from a kill -9 is detected at next acquire() as "stale" anyway.
pub fn release() -> Result<(), String> {
    let path = pid_path();
    match std::fs::remove_file(&path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(format!("remove {}: {}", path.display(), e)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // The lockfile-touching tests mutate VCT_STATE_DIR at process
    // scope. Serialise them so parallel cargo-test runs don't observe
    // each other's pid files. Same pattern as auth::tests.
    static SERIALIZE: Mutex<()> = Mutex::new(());

    fn with_state_dir<F: FnOnce(&std::path::Path)>(f: F) {
        let _g = SERIALIZE.lock().unwrap_or_else(|p| p.into_inner());
        let tmp = tempfile::tempdir().expect("tempdir");
        // Safety: tests are serialized by SERIALIZE; no thread
        // concurrently observes/mutates VCT_STATE_DIR.
        unsafe {
            std::env::set_var("VCT_STATE_DIR", tmp.path());
        }
        f(tmp.path());
        unsafe {
            std::env::remove_var("VCT_STATE_DIR");
        }
    }

    #[test]
    fn read_pid_returns_none_when_file_absent() {
        with_state_dir(|_root| {
            assert_eq!(read_pid(), None);
        });
    }

    #[test]
    fn write_then_read_pid_round_trips() {
        with_state_dir(|root| {
            write_pid(12345).expect("write");
            assert_eq!(read_pid(), Some(12345));
            let raw = std::fs::read_to_string(root.join(PID_FILE)).unwrap();
            assert!(raw.starts_with("12345"), "first token must be pid: {:?}", raw);

            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let mode = std::fs::metadata(root.join(PID_FILE))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777;
                assert_eq!(mode, 0o600, "pid file must be 0o600, got {:o}", mode);
            }
        });
    }

    #[test]
    fn read_pid_ignores_trailing_garbage() {
        with_state_dir(|root| {
            std::fs::write(root.join(PID_FILE), "999\nextra-line-future-format\n").unwrap();
            assert_eq!(read_pid(), Some(999));
        });
    }

    #[test]
    fn read_pid_returns_none_on_empty_file() {
        with_state_dir(|root| {
            std::fs::write(root.join(PID_FILE), "").unwrap();
            assert_eq!(read_pid(), None);
        });
    }

    #[test]
    fn read_pid_returns_none_on_unparseable_content() {
        with_state_dir(|root| {
            std::fs::write(root.join(PID_FILE), "not-a-pid\n").unwrap();
            assert_eq!(read_pid(), None);
        });
    }

    #[test]
    fn acquire_claims_when_no_lockfile() {
        with_state_dir(|_root| {
            let outcome = acquire().expect("acquire");
            assert_eq!(outcome, AcquireOutcome::Claimed);
            // Our PID should now be in the file.
            assert_eq!(read_pid(), Some(std::process::id()));
        });
    }

    #[test]
    fn acquire_reports_already_running_when_owner_alive() {
        with_state_dir(|_root| {
            // Our own pid is, by definition, alive.
            let me = std::process::id();
            write_pid(me).expect("seed pid");
            match acquire().expect("acquire") {
                AcquireOutcome::AlreadyRunning { pid } => assert_eq!(pid, me),
                AcquireOutcome::Claimed => {
                    panic!("Should have reported already-running for pid {}", me)
                }
            }
        });
    }

    #[test]
    fn acquire_reclaims_when_owner_dead() {
        with_state_dir(|root| {
            // u32::MAX is rejected by pid_is_alive as a sentinel → "dead"
            // → acquire should reclaim.
            write_pid(u32::MAX).expect("seed stale");
            let outcome = acquire().expect("acquire");
            assert_eq!(outcome, AcquireOutcome::Claimed);
            assert_eq!(
                read_pid(),
                Some(std::process::id()),
                "lockfile at {} should now hold our pid",
                root.join(PID_FILE).display()
            );
        });
    }

    #[test]
    fn release_is_idempotent() {
        with_state_dir(|_root| {
            release().expect("release on absent");
            write_pid(42).expect("write");
            release().expect("release on present");
            assert_eq!(read_pid(), None);
            release().expect("release on absent again");
        });
    }
}
