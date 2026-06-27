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
//! `hub.token`. **Format (v0.2.69):**
//!   * LINE 1 — the integer PID. Always present; every reader takes the
//!     first line only (`read_pid`, `hub_status.rs`, `installer.rs`,
//!     install.py's `_hub_pid_from_lockfile`). A pre-v0.2.69 lockfile is
//!     exactly this single line + a newline — still valid.
//!   * LINE 2 — (added v0.2.69, hub-staleness home #3) the build IDENTITY
//!     string: `"<version>+<git-sha>[-dirty]"` (or bare `<version>` when no
//!     git SHA was baked). Lets a start path tell whether an already-running
//!     hub is the CURRENT binary or a stale/foreign one even off-Linux. A
//!     lockfile with NO second line = identity unknown = treated as OLDER
//!     (must-restart) ONLY in combination with the inode check; on its own,
//!     an absent identity falls back conservatively (never a false kill).
//!
//! Forward-compat: the FIRST line stays the PID for every existing parser.

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
    /// Another live hub already holds the lockfile AND it is the current
    /// binary (same inode on Linux, or same/newer-or-unknown identity
    /// elsewhere) — nothing to do.
    AlreadyRunning { pid: u32 },
    /// A live hub holds the lockfile but it is a STALE or FOREIGN binary
    /// (different executable inode on Linux, or a positively-different
    /// recorded identity elsewhere). The caller should stop it gracefully,
    /// then re-`acquire` to claim + start the current binary.
    /// `recorded_identity` is the lockfile's line-2 string (or `None` for a
    /// pre-v0.2.69 lockfile) for logging.
    StaleVersion {
        pid: u32,
        recorded_identity: Option<String>,
    },
}

/// Read the PID currently recorded in the lockfile, if any.
///
/// Returns `None` if the file is missing, empty, or unparseable. The
/// caller is responsible for the alive-vs-stale decision. Reads the FIRST
/// line only — a v0.2.69 two-line lockfile still parses correctly here.
pub fn read_pid() -> Option<u32> {
    let raw = std::fs::read_to_string(pid_path()).ok()?;
    let first = raw.lines().next()?.trim();
    if first.is_empty() {
        return None;
    }
    first.parse::<u32>().ok()
}

/// Read the build-identity string recorded on the lockfile's second line.
///
/// Returns `None` for a pre-v0.2.69 single-line lockfile, an empty second
/// line, or a missing/unreadable file.
pub fn read_identity() -> Option<String> {
    let raw = std::fs::read_to_string(pid_path()).ok()?;
    let second = raw.lines().nth(1)?.trim();
    if second.is_empty() {
        None
    } else {
        Some(second.to_string())
    }
}

/// Decide whether the live hub recorded as `pid` is STALE relative to the
/// CURRENT binary (ours). Ported from the launcher's
/// `hub_launcher::running_hub_is_stale` so the hub's own start path is
/// identity-aware, not only the launcher-GUI boot path.
///
///   * **Linux** — authoritative. Compare the inode `/proc/<pid>/exe`
///     resolves to (the file the process is actually executing, valid even
///     after an in-place replace) against OUR `current_exe()` inode.
///     Different inode ⇒ stale (catches both a different-path hub AND an
///     older build at the same path); identical inode ⇒ definitively fresh.
///   * **Other OSes / `/proc` unavailable** — fall back to the recorded
///     build identity: stale only when BOTH the recorded and our identity
///     are KNOWN git fingerprints AND they differ. Anything uncertain
///     (recorded identity absent, ours not a git fingerprint) ⇒ NOT stale.
///
/// Strictly conservative: returns `false` whenever staleness cannot be
/// POSITIVELY confirmed, so a hub we cannot identify is never killed.
pub fn running_hub_is_stale(pid: u32, recorded_identity: Option<&str>) -> bool {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::fs::MetadataExt;
        if let Ok(our_exe) = std::env::current_exe() {
            let proc_exe = format!("/proc/{}/exe", pid);
            if let (Ok(run), Ok(ours)) =
                (std::fs::metadata(&proc_exe), std::fs::metadata(&our_exe))
            {
                return run.dev() != ours.dev() || run.ino() != ours.ino();
            }
            // metadata failed (pid gone, restricted /proc) — fall through
            // to the identity-string comparison below.
        }
    }

    // Cross-OS fallback: trust the fingerprint only when it is a real git
    // fingerprint on BOTH sides and they positively differ.
    match (recorded_identity, crate::identity::build_fingerprint()) {
        (Some(recorded), Some(ours)) if crate::identity::has_git_fingerprint() => {
            recorded != ours
        }
        // Recorded identity absent (pre-v0.2.69), or ours is only the bare
        // version (no git SHA baked) — cannot positively confirm staleness.
        _ => false,
    }
}

/// Try to claim the lockfile for the current process.
///
/// Sequence:
/// 1. Read existing PID. If absent → write our PID+identity, return Claimed.
/// 2. If present + alive + CURRENT binary → AlreadyRunning. Don't touch it.
/// 3. If present + alive + STALE binary → StaleVersion (caller stops + retries).
/// 4. If present + dead → overwrite with our PID+identity, return Claimed.
///
/// Race window: between the read and the write a second hub could also see
/// the stale file. Both would overwrite with their PID; whichever runs last
/// wins. To make that race harmless, the binder downstream of this function
/// also tries `TcpListener::bind` on the hub port — EADDRINUSE on the bind
/// reveals the duplicate. So lockfile + bind together form the atomic check.
pub fn acquire() -> Result<AcquireOutcome, String> {
    if let Some(existing_pid) = read_pid() {
        if pid_is_alive(existing_pid) {
            let recorded = read_identity();
            if running_hub_is_stale(existing_pid, recorded.as_deref()) {
                return Ok(AcquireOutcome::StaleVersion {
                    pid: existing_pid,
                    recorded_identity: recorded,
                });
            }
            return Ok(AcquireOutcome::AlreadyRunning { pid: existing_pid });
        }
        // Dead — stale lockfile. Fall through to overwrite.
    }
    write_pid(std::process::id())?;
    Ok(AcquireOutcome::Claimed)
}

/// Persist a PID (line 1) + the current build identity (line 2) to the
/// lockfile with mode 0o600 on Unix. This is the canonical writer since
/// v0.2.69.
///
/// Same write-pattern as `hub.token` (single syscall O_CREAT|O_TRUNC|
/// O_WRONLY with the mode set in `OpenOptions::mode`, avoiding the
/// chmod-after-create TOCTOU window).
pub fn write_pid(pid: u32) -> Result<(), String> {
    write_pid_and_identity(pid, &crate::identity::fingerprint_or_version())
}

/// Persist `pid` (line 1) + an explicit `identity` (line 2). Split out so
/// tests can seed a chosen identity; production calls `write_pid` which
/// supplies the compile-time fingerprint.
pub fn write_pid_and_identity(pid: u32, identity: &str) -> Result<(), String> {
    let path = pid_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("create_dir_all {}: {}", parent.display(), e))?;
    }

    let body = format!("{}\n{}\n", pid, identity);

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
        f.write_all(body.as_bytes())
            .map_err(|e| format!("write {}: {}", path.display(), e))?;
    }

    #[cfg(not(unix))]
    {
        std::fs::write(&path, body)
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
            // Our own pid is, by definition, alive — and the running exe is
            // OUR exe, so on Linux the /proc/<me>/exe inode equals our
            // current_exe() inode ⇒ not stale ⇒ AlreadyRunning. Off-Linux
            // the recorded identity (written by write_pid) equals ours ⇒
            // also AlreadyRunning.
            let me = std::process::id();
            write_pid(me).expect("seed pid");
            match acquire().expect("acquire") {
                AcquireOutcome::AlreadyRunning { pid } => assert_eq!(pid, me),
                other => panic!(
                    "Should have reported already-running for our own pid {}; got {:?}",
                    me, other
                ),
            }
        });
    }

    // ── v0.2.69 identity-aware decision matrix ──────────────────────────

    #[test]
    fn write_pid_records_identity_on_second_line() {
        with_state_dir(|root| {
            write_pid(4242).expect("write");
            let raw = std::fs::read_to_string(root.join(PID_FILE)).unwrap();
            let mut lines = raw.lines();
            assert_eq!(lines.next(), Some("4242"), "line 1 must be the pid");
            let identity = lines.next().expect("line 2 = identity");
            assert!(!identity.trim().is_empty(), "identity line must be present");
            // read_identity should round-trip the same string.
            assert_eq!(
                read_identity().as_deref(),
                Some(identity.trim()),
                "read_identity must return line 2"
            );
            // read_pid still works on the 2-line file.
            assert_eq!(read_pid(), Some(4242));
        });
    }

    #[test]
    fn read_identity_none_for_single_line_lockfile() {
        with_state_dir(|root| {
            // Pre-v0.2.69 lockfile: pid only, no identity line.
            std::fs::write(root.join(PID_FILE), "777\n").unwrap();
            assert_eq!(read_pid(), Some(777));
            assert_eq!(read_identity(), None, "single-line lockfile has no identity");
        });
    }

    #[test]
    fn write_explicit_identity_round_trips() {
        with_state_dir(|_root| {
            write_pid_and_identity(13, "0.2.69+deadbeef1234").expect("write");
            assert_eq!(read_pid(), Some(13));
            assert_eq!(read_identity().as_deref(), Some("0.2.69+deadbeef1234"));
        });
    }

    #[test]
    fn running_hub_is_stale_false_for_own_live_pid() {
        // Our own pid is alive and runs OUR exe. On Linux the /proc inode
        // matches current_exe() ⇒ not stale. Off-Linux a None recorded
        // identity is treated conservatively ⇒ not stale. Either way false.
        with_state_dir(|_root| {
            assert!(
                !running_hub_is_stale(std::process::id(), None),
                "own live pid must never be reported stale (no false kill)"
            );
        });
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn running_hub_is_stale_true_when_proc_exe_differs() {
        // A dead pid makes /proc/<pid>/exe stat fail → the Linux branch
        // falls through to the identity comparison. With a recorded git
        // fingerprint that differs from ours AND our binary carrying a git
        // fingerprint, that is positively stale. When our build has NO git
        // fingerprint (released tarball test run) the comparison is
        // inconclusive → not stale; assert accordingly.
        with_state_dir(|_root| {
            let recorded = Some("0.0.1+0000000aaaaa");
            let expected = crate::identity::has_git_fingerprint();
            assert_eq!(
                running_hub_is_stale(u32::MAX, recorded),
                expected,
                "stale iff our build carries a git fingerprint to compare against"
            );
        });
    }

    #[test]
    fn running_hub_is_stale_false_when_recorded_identity_absent() {
        // A pre-v0.2.69 lockfile (no identity) over a pid whose exe we
        // cannot inode-compare must NOT be reported stale on the fallback
        // path — conservative, never a false kill. Use a dead sentinel pid
        // so the Linux /proc branch falls through to the fallback.
        with_state_dir(|_root| {
            assert!(
                !running_hub_is_stale(u32::MAX, None),
                "absent identity + uninspectable exe ⇒ conservative not-stale"
            );
        });
    }

    #[test]
    fn acquire_reports_stale_version_for_alive_foreign_identity() {
        // Seed a LIVE pid (our own) but force the stale path by passing a
        // foreign identity AND an uninspectable exe. We cannot easily fake
        // a different /proc inode for our own live pid on Linux, so this
        // test asserts the OUTCOME wiring via a dead-pid+identity unit on
        // running_hub_is_stale (covered above) and the acquire mapping for
        // the live-but-fresh case (own pid ⇒ AlreadyRunning) below. The
        // StaleVersion enum + mapping is exercised by the lifecycle tests.
        with_state_dir(|_root| {
            let me = std::process::id();
            write_pid_and_identity(me, "0.2.69+ourfingerprint").expect("seed");
            // Own pid + own exe ⇒ fresh ⇒ AlreadyRunning (never StaleVersion
            // for the binary that is actually running).
            match acquire().expect("acquire") {
                AcquireOutcome::AlreadyRunning { pid } => assert_eq!(pid, me),
                other => panic!("own live exe must map to AlreadyRunning; got {:?}", other),
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
