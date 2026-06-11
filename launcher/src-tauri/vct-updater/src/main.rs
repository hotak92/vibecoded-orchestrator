// vct-updater — Stage1 updater for the VCT launcher (v0.2.52 V52-AH).
//
// See `Cargo.toml` for the design rationale. TL;DR: this binary exists
// to perform the Windows binary swap during orchestrator update, after
// the running launcher has exited and released its mandatory file
// locks. On POSIX systems the swap pattern in `installer.rs` already
// works (advisory locks); this binary is a no-op there.
//
// Invocation contract
// -------------------
// The launcher writes `<vct_root>/update.lock.json`:
//
// {
//   "parent_pid": 12345,
//   "swaps": [
//     {"target": "C:\\...\\launcher\\dist\\windows-x64\\vct-launcher.exe"},
//     {"target": "C:\\...\\launcher\\dist\\windows-x64\\vct-hub.exe"}
//   ],
//   "relaunch": "C:\\...\\launcher\\dist\\windows-x64\\vct-launcher.exe",
//   "started_at": "2026-06-09T18:30:00Z"
// }
//
// Then spawns `vct-updater.exe <path-to-update.lock.json>` DETACHED
// and exits. The updater:
//
//   1. Reads the lock JSON.
//   2. Waits for `parent_pid` to exit (handle-poll on Windows, no-op
//      otherwise).
//   3. For each swap entry: if a `<target>.new` sibling exists, do
//      `MoveFileEx(<target>.new → <target>, REPLACE_EXISTING | WRITE_THROUGH)`.
//      If no `.new` sibling, skip — the file is already the canonical
//      payload (e.g. the rename pattern already succeeded for one file).
//   4. If `relaunch` is set, spawn it detached.
//   5. Delete the lock JSON.
//
// Soft-fail throughout: every step writes its outcome to
// `<vct_root>/update.log` (next to the lock file) for forensic recovery.
// On any unrecoverable error, exit non-zero — the new launcher's
// boot-time recovery (see `lib.rs::poll_update_lock_on_boot`) will
// surface a toast.

use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
#[cfg(target_os = "windows")]
use std::time::{Duration, Instant};
#[cfg(not(target_os = "windows"))]
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// Maximum time (in seconds) we'll wait for the parent process to exit.
/// 30s is generous — typical launcher shutdown is sub-second; this
/// guards against a hung parent that would block the updater forever.
#[cfg(target_os = "windows")]
const PARENT_WAIT_TIMEOUT_SECS: u64 = 30;

/// Polling interval while waiting for the parent.
#[cfg(target_os = "windows")]
const PARENT_WAIT_POLL_MS: u64 = 200;

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SwapEntry {
    /// Canonical absolute path of the binary to overwrite. The updater
    /// will look for `<target>.new` and rename it to `<target>`.
    target: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct UpdateLock {
    /// PID of the launcher that requested this update. The updater
    /// waits for this PID to exit before performing the swap.
    parent_pid: u32,

    /// Binaries to swap. Each entry's `target.new` sibling (if present)
    /// is renamed to `target`.
    swaps: Vec<SwapEntry>,

    /// Optional path to spawn after all swaps complete. Typically the
    /// new launcher binary so the user transparently sees the update
    /// finish.
    #[serde(default)]
    relaunch: Option<PathBuf>,

    /// ISO 8601 timestamp set by the launcher when it wrote the lock.
    /// Used by the new launcher's boot-time recovery to detect stale
    /// locks (>10 min → discard).
    #[serde(default)]
    started_at: Option<String>,
}

/// v0.2.54 Track C (C-4): authoritative swap outcome, written to
/// `<vct_root>/update.result.json` AFTER the swaps and BEFORE the
/// relaunch spawn. Mirror of `UpdateOutcome` in
/// `launcher/src-tauri/src/commands/update_handoff.rs` — keep the JSON
/// wire contract in sync.
///
/// Why before the relaunch: the relaunched launcher's boot probe reads
/// this file. Writing it first guarantees the probe finds it no matter
/// how fast the new launcher boots — the pre-v0.2.54 design (probe
/// infers outcome from the LOCK file the updater deletes microseconds
/// after spawning the relaunch) made the success toast a
/// microseconds-vs-seconds race the launcher always lost.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct UpdateOutcome {
    /// True iff every swap succeeded (swap_failures == 0).
    success: bool,
    /// Number of swap entries attempted.
    swaps_attempted: usize,
    /// Number of swap entries that FAILED.
    swap_failures: usize,
    /// Completion timestamp, `unix:<epoch-seconds>` format. Informational
    /// only — the launcher's probe never parses it (deliberately, so this
    /// crate stays dependency-light: no chrono).
    #[serde(default)]
    completed_at: Option<String>,
    /// Optional human-readable detail (first failure, relaunch error).
    #[serde(default)]
    detail: Option<String>,
}

/// Atomic-ish write of the outcome file (tmp + rename). Best-effort:
/// failure is recorded in the log buffer by the caller.
#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
fn write_outcome(path: &Path, outcome: &UpdateOutcome) -> Result<(), String> {
    let json = serde_json::to_string_pretty(outcome)
        .map_err(|e| format!("serialize outcome: {}", e))?;
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, &json).map_err(|e| format!("write {}: {}", tmp.display(), e))?;
    fs::rename(&tmp, path).map_err(|e| format!("rename {}: {}", path.display(), e))
}

fn main() -> ExitCode {
    // Cmdline: vct-updater <path-to-update.lock.json>
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("vct-updater: usage: vct-updater <update.lock.json>");
        return ExitCode::from(2);
    }
    let lock_path = PathBuf::from(&args[1]);

    let lock = match read_lock(&lock_path) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("vct-updater: failed to read lock {}: {}", lock_path.display(), e);
            return ExitCode::from(3);
        }
    };

    let log_path = lock_path.with_file_name("update.log");
    let mut log = String::new();
    log.push_str(&format!("vct-updater started: pid={}, lock={}\n", std::process::id(), lock_path.display()));
    log.push_str(&format!("parent_pid={}\n", lock.parent_pid));
    log.push_str(&format!("swaps={}\n", lock.swaps.len()));

    // On POSIX, the rename pattern already works. We log + exit Ok.
    #[cfg(not(target_os = "windows"))]
    {
        log.push_str("non-Windows host: no-op (POSIX rename pattern handles this case)\n");
        let _ = fs::write(&log_path, &log);
        return ExitCode::SUCCESS;
    }

    // Windows path: wait for parent, swap, record outcome, relaunch.
    #[cfg(target_os = "windows")]
    {
        match wait_for_parent_exit(lock.parent_pid) {
            Ok(elapsed) => {
                log.push_str(&format!("parent {} exited after {:?}\n", lock.parent_pid, elapsed));
            }
            Err(WaitError::Timeout) => {
                log.push_str(&format!(
                    "parent {} did NOT exit within {}s — aborting swap (binary still locked)\n",
                    lock.parent_pid, PARENT_WAIT_TIMEOUT_SECS,
                ));
                // v0.2.54 Track C (C-5): delete the lock on the timeout
                // abort. Nothing was swapped — the canonical binaries
                // are unchanged and the legacy deferral path covers
                // user comms — so an orphaned lock here only produced a
                // SPURIOUS "update may have failed" toast on a boot
                // >10 min later (e.g. install.py-spawned updater #1
                // timing out while the launcher was still mid-
                // WaitForBinaryRefresh, pre-C-5).
                log.push_str("timeout abort: removing lock (no swaps performed)\n");
                let _ = fs::write(&log_path, &log);
                let _ = fs::remove_file(&lock_path);
                return ExitCode::from(4);
            }
            Err(WaitError::AlreadyGone) => {
                log.push_str(&format!("parent {} already gone (handle invalid)\n", lock.parent_pid));
            }
        }

        // Perform each swap.
        let mut swap_failures = 0_usize;
        let mut first_failure_detail: Option<String> = None;
        for entry in &lock.swaps {
            match swap_binary(&entry.target) {
                Ok(SwapResult::Swapped) => {
                    log.push_str(&format!("swap OK: {}\n", entry.target.display()));
                }
                Ok(SwapResult::NoOpMissingNew) => {
                    log.push_str(&format!(
                        "swap skipped (no <target>.new sibling found): {}\n",
                        entry.target.display(),
                    ));
                }
                Err(e) => {
                    log.push_str(&format!("swap FAILED {}: {}\n", entry.target.display(), e));
                    if first_failure_detail.is_none() {
                        first_failure_detail =
                            Some(format!("{}: {}", entry.target.display(), e));
                    }
                    swap_failures += 1;
                }
            }
        }

        // v0.2.54 Track C (C-4): write the authoritative outcome file
        // BEFORE spawning the relaunch, so the relaunched launcher's
        // boot probe always finds it (closes the race where the lock
        // was deleted microseconds after the relaunch spawn and the
        // probe found nothing). The relaunch result is appended to the
        // log but does NOT change `success` — a failed relaunch with
        // successful swaps is still a successful UPDATE (the user's
        // next manual launch runs the new binary and sees the toast).
        let outcome = UpdateOutcome {
            success: swap_failures == 0,
            swaps_attempted: lock.swaps.len(),
            swap_failures,
            completed_at: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .ok()
                .map(|d| format!("unix:{}", d.as_secs())),
            detail: first_failure_detail,
        };
        let result_path = lock_path.with_file_name("update.result.json");
        match write_outcome(&result_path, &outcome) {
            Ok(()) => {
                log.push_str(&format!(
                    "outcome recorded: success={} swap_failures={} → {}\n",
                    outcome.success,
                    outcome.swap_failures,
                    result_path.display(),
                ));
            }
            Err(e) => {
                log.push_str(&format!("outcome write FAILED: {}\n", e));
            }
        }

        // v0.2.54 Track C (C-4): delete the lock ONLY on full success.
        // On swap failure the lock stays alongside the result file:
        // the boot probe consumes both (failure toast), and if the
        // relaunch never happens, a later manual boot still finds the
        // trail instead of silence.
        if swap_failures == 0 {
            let _ = fs::remove_file(&lock_path);
            log.push_str("lock removed (full success)\n");
        } else {
            log.push_str("lock KEPT (swap failure — boot probe will consume it)\n");
        }

        // Relaunch the launcher if requested. Spawned even on swap
        // failure: the canonical binary is still the runnable OLD
        // bytes, and relaunching it is what surfaces the failure toast
        // (the alternative — no launcher at all — hides the failure).
        let mut relaunch_failed = false;
        if let Some(relaunch) = lock.relaunch.as_deref() {
            match spawn_detached(relaunch) {
                Ok(()) => {
                    log.push_str(&format!("relaunch spawned: {}\n", relaunch.display()));
                }
                Err(e) => {
                    log.push_str(&format!("relaunch FAILED {}: {}\n", relaunch.display(), e));
                    relaunch_failed = true;
                }
            }
        }

        // Always write the log last — forensic trail includes every step.
        let _ = fs::write(&log_path, &log);

        if swap_failures > 0 || relaunch_failed {
            return ExitCode::from(5);
        }
        ExitCode::SUCCESS
    }
}

fn read_lock(path: &Path) -> Result<UpdateLock, String> {
    let content = fs::read_to_string(path).map_err(|e| format!("read: {}", e))?;
    serde_json::from_str(&content).map_err(|e| format!("parse: {}", e))
}

/// Outcome of a single swap attempt.
#[allow(dead_code)] // Swapped is constructed only on Windows
enum SwapResult {
    /// `<target>.new` was found and successfully renamed to `<target>`.
    Swapped,
    /// `<target>.new` did not exist — nothing to do. This is OK; one
    /// of the original swap attempts may have already succeeded
    /// (e.g. the launcher itself was renamed pre-pull on Windows).
    NoOpMissingNew,
}

// -----------------------------------------------------------------------------
// Windows-only logic (parent wait + MoveFileEx + CreateProcess)
// -----------------------------------------------------------------------------

#[cfg(target_os = "windows")]
enum WaitError {
    Timeout,
    AlreadyGone,
}

#[cfg(target_os = "windows")]
fn wait_for_parent_exit(pid: u32) -> Result<Duration, WaitError> {
    use windows_sys::Win32::Foundation::{CloseHandle, WAIT_OBJECT_0, WAIT_TIMEOUT};
    use windows_sys::Win32::System::Threading::{OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE};

    let started = Instant::now();
    let deadline = started + Duration::from_secs(PARENT_WAIT_TIMEOUT_SECS);

    // SAFETY: OpenProcess is safe to call with any DWORD pid. A NULL
    // return indicates the process is gone or we lack permissions.
    // Either way we can't wait on it — treat as "already gone" so the
    // swap proceeds.
    let handle = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, pid) };
    if handle.is_null() {
        return Err(WaitError::AlreadyGone);
    }

    loop {
        // Poll in small chunks so we can check our own deadline cleanly.
        // SAFETY: handle is valid (non-null check above) until we call
        // CloseHandle.
        let wait_result = unsafe { WaitForSingleObject(handle, PARENT_WAIT_POLL_MS as u32) };
        if wait_result == WAIT_OBJECT_0 {
            // Parent terminated.
            unsafe { CloseHandle(handle) };
            return Ok(started.elapsed());
        }
        if wait_result != WAIT_TIMEOUT {
            // Unexpected wait state (handle invalidated mid-poll, etc.).
            // Treat as "already gone" so we proceed with the swap.
            unsafe { CloseHandle(handle) };
            return Err(WaitError::AlreadyGone);
        }
        if Instant::now() >= deadline {
            unsafe { CloseHandle(handle) };
            return Err(WaitError::Timeout);
        }
    }
}

#[cfg(target_os = "windows")]
fn swap_binary(target: &Path) -> Result<SwapResult, String> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Foundation::GetLastError;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };

    let staged = target.with_extension(format!(
        "{}.new",
        target
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or("")
    ));
    // Normalize: we want <target>.new, not <stem>.<ext>.new — fix when
    // the extension was non-empty.
    let staged = if let Some(ext) = target.extension().and_then(|s| s.to_str()) {
        let mut s = target.to_path_buf();
        s.set_extension(format!("{}.new", ext));
        s
    } else {
        staged
    };

    if !staged.exists() {
        return Ok(SwapResult::NoOpMissingNew);
    }

    // Convert paths to UTF-16 null-terminated strings (Windows API
    // requirement). encode_wide() yields the code units, we append the
    // null terminator manually.
    let staged_w: Vec<u16> = staged.as_os_str().encode_wide().chain(std::iter::once(0)).collect();
    let target_w: Vec<u16> = target.as_os_str().encode_wide().chain(std::iter::once(0)).collect();

    let flags = MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH;

    // SAFETY: both pointers point to null-terminated UTF-16 strings
    // valid for the duration of the call (they're owned by `staged_w`
    // and `target_w` Vec<u16>s).
    let ok = unsafe { MoveFileExW(staged_w.as_ptr(), target_w.as_ptr(), flags) };

    if ok == 0 {
        let err = unsafe { GetLastError() };
        return Err(format!("MoveFileExW failed: GetLastError={}", err));
    }
    Ok(SwapResult::Swapped)
}

#[cfg(target_os = "windows")]
fn spawn_detached(exe: &Path) -> Result<(), String> {
    use std::os::windows::process::CommandExt;
    use std::process::{Command, Stdio};

    const CREATE_NEW_PROCESS_GROUP: u32 = 0x00000200;
    const DETACHED_PROCESS: u32 = 0x00000008;

    let mut cmd = Command::new(exe);
    cmd.stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS);

    cmd.spawn()
        .map(|_child| ())
        .map_err(|e| format!("spawn {}: {}", exe.display(), e))
}

// -----------------------------------------------------------------------------
// Cross-platform stubs for non-Windows (keep the binary compiling but
// inactive on POSIX so we don't have to gate the entire workspace by
// target_os).
// -----------------------------------------------------------------------------

#[cfg(not(target_os = "windows"))]
#[allow(dead_code)]
enum WaitError {
    Timeout,
    AlreadyGone,
}

#[cfg(not(target_os = "windows"))]
#[allow(dead_code)]
fn wait_for_parent_exit(_pid: u32) -> Result<Duration, WaitError> {
    // POSIX: not used (main() returns early). Stub for compile parity.
    Err(WaitError::AlreadyGone)
}

#[cfg(not(target_os = "windows"))]
#[allow(dead_code)]
fn swap_binary(_target: &Path) -> Result<SwapResult, String> {
    // POSIX: not used.
    Ok(SwapResult::NoOpMissingNew)
}

#[cfg(not(target_os = "windows"))]
#[allow(dead_code)]
fn spawn_detached(_exe: &Path) -> Result<(), String> {
    // POSIX: not used.
    Ok(())
}

// -----------------------------------------------------------------------------
// Tests
// -----------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lock_roundtrip_minimal() {
        let lock = UpdateLock {
            parent_pid: 12345,
            swaps: vec![SwapEntry {
                target: PathBuf::from("/tmp/vct-launcher.exe"),
            }],
            relaunch: None,
            started_at: None,
        };
        let json = serde_json::to_string(&lock).unwrap();
        let back: UpdateLock = serde_json::from_str(&json).unwrap();
        assert_eq!(back.parent_pid, 12345);
        assert_eq!(back.swaps.len(), 1);
        assert!(back.relaunch.is_none());
    }

    #[test]
    fn lock_roundtrip_with_relaunch() {
        let lock = UpdateLock {
            parent_pid: 9999,
            swaps: vec![
                SwapEntry { target: PathBuf::from("/tmp/a.exe") },
                SwapEntry { target: PathBuf::from("/tmp/b.exe") },
            ],
            relaunch: Some(PathBuf::from("/tmp/a.exe")),
            started_at: Some("2026-06-09T18:30:00Z".to_string()),
        };
        let json = serde_json::to_string(&lock).unwrap();
        let back: UpdateLock = serde_json::from_str(&json).unwrap();
        assert_eq!(back.swaps.len(), 2);
        assert_eq!(back.relaunch.as_deref(), Some(Path::new("/tmp/a.exe")));
        assert_eq!(back.started_at.as_deref(), Some("2026-06-09T18:30:00Z"));
    }

    #[test]
    fn lock_parse_missing_optional_fields() {
        // Forward-compat: launcher might emit a lock with no relaunch
        // and no started_at. Should parse cleanly.
        let json = r#"{"parent_pid": 1, "swaps": []}"#;
        let lock: UpdateLock = serde_json::from_str(json).unwrap();
        assert_eq!(lock.parent_pid, 1);
        assert!(lock.swaps.is_empty());
        assert!(lock.relaunch.is_none());
        assert!(lock.started_at.is_none());
    }

    #[test]
    fn lock_parse_rejects_invalid_json() {
        let result: Result<UpdateLock, _> = serde_json::from_str("{ not json");
        assert!(result.is_err());
    }

    // v0.2.54 Track C (C-4): outcome wire-contract round-trip. The
    // launcher-side mirror (`commands/update_handoff.rs::UpdateOutcome`)
    // must parse exactly what this side writes.
    #[test]
    fn outcome_roundtrip() {
        let outcome = UpdateOutcome {
            success: false,
            swaps_attempted: 2,
            swap_failures: 1,
            completed_at: Some("unix:1760000000".to_string()),
            detail: Some("C:\\x\\vct-hub.exe: MoveFileExW failed: GetLastError=32".to_string()),
        };
        let json = serde_json::to_string(&outcome).unwrap();
        let back: UpdateOutcome = serde_json::from_str(&json).unwrap();
        assert!(!back.success);
        assert_eq!(back.swaps_attempted, 2);
        assert_eq!(back.swap_failures, 1);
        assert!(back.detail.unwrap().contains("GetLastError=32"));
    }

    #[test]
    fn outcome_parse_tolerates_missing_optionals() {
        let json = r#"{"success": true, "swaps_attempted": 1, "swap_failures": 0}"#;
        let outcome: UpdateOutcome = serde_json::from_str(json).unwrap();
        assert!(outcome.success);
        assert!(outcome.completed_at.is_none());
        assert!(outcome.detail.is_none());
    }

    #[test]
    fn write_outcome_lands_atomically_at_path() {
        let td = tempfile::tempdir().unwrap();
        let path = td.path().join("update.result.json");
        let outcome = UpdateOutcome {
            success: true,
            swaps_attempted: 1,
            swap_failures: 0,
            completed_at: None,
            detail: None,
        };
        write_outcome(&path, &outcome).expect("write");
        assert!(path.is_file());
        // No .tmp left behind.
        assert!(!path.with_extension("json.tmp").exists());
        let back: UpdateOutcome =
            serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert!(back.success);
    }
}
