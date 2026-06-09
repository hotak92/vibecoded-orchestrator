// v0.2.52 V52-AH (Fabio bug 1, 2026-06-09): Windows binary lock fix.
//
// Background
// ----------
// `update_orchestrator` in installer.rs runs `git pull` to fetch new
// orchestrator sources. After the pull lands, the dist binary on disk
// (e.g. `launcher/dist/windows-x64/vct-launcher.exe`) is the NEW
// version but the running launcher PID still holds the OLD .exe open
// via Windows mandatory file locking. Pre-pull-rename helps for the
// launcher itself (its .exe is renamed aside before pull), but the
// metadata.json is updated to the new version and the dist .exe is
// the same old bytes for both pre-rename and post-pull when the
// pull-side overwrite is silently skipped due to the lock.
//
// Symptom Fabio reported: the "Restart" banner relaunches the SAME
// stale binary repeatedly → infinite loop.
//
// This module is the launcher-side half of the stage1 updater pattern.
// It is invoked from the SUCCESSFUL post-pull path of
// `update_orchestrator` (after the new metadata.json is on disk + the
// dist binary is verifiably the new version) when running on Windows.
//
// Flow (Windows only)
// -------------------
// 1. `prepare_windows_update_handoff` writes `<vct_root>/update.lock.json`
//    with the running launcher's PID + the absolute paths of the
//    binaries that need swapping.
// 2. Launcher spawns `vct-updater.exe` DETACHED, passing the lock path
//    as argv[1].
// 3. Launcher exits (the caller — typically the wrapper around
//    `restart_launcher` in installer.rs — calls `app.exit(0)`).
// 4. Updater takes over (see `vct-updater/src/main.rs`):
//    a. Polls OpenProcess(parent_pid) until the launcher exits.
//    b. MoveFileExW(REPLACE_EXISTING) for each pending swap.
//    c. Spawns the new launcher detached.
//    d. Deletes the lock file.
//
// Soft-fail
// ---------
// EVERY error path here returns to the caller without spawning the
// updater. The caller (installer.rs) then falls through to the
// existing `restart_launcher` flow, which on Windows will produce
// the v0.2.51 "Please close launcher first" UX — same WORST case as
// today, never worse.
//
// On non-Windows, all functions in this module return Ok(()) without
// doing anything (the POSIX rename pattern in installer.rs already
// handles binary swap correctly).

use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use tauri::command;
use vct_launcher_core::paths::vct_root_dir;

/// File name of the JSON lock the launcher writes for the updater.
/// Placed under `~/.vct/` so the updater can find it without an argv
/// path if needed (current contract still uses argv).
pub const UPDATE_LOCK_FILE: &str = "update.lock.json";

/// Stage1 updater binary name (Windows). The release build of
/// vct-updater is shipped at `launcher/dist/windows-x64/vct-updater.exe`
/// alongside vct-launcher.exe and vct-hub.exe.
#[cfg(target_os = "windows")]
pub const UPDATER_BIN: &str = "vct-updater.exe";

#[cfg(not(target_os = "windows"))]
#[allow(dead_code)] // POSIX path never reads UPDATER_BIN; kept for parity + tests
pub const UPDATER_BIN: &str = "vct-updater";

/// Mirror of the same struct in `vct-updater/src/main.rs`. Both sides
/// share the JSON wire contract; keep these two in sync.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SwapEntry {
    /// Canonical absolute path of the binary to overwrite. The updater
    /// will look for `<target>.new` and rename it to `<target>`.
    pub target: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpdateLock {
    /// PID of the running launcher requesting the swap.
    pub parent_pid: u32,

    /// Binaries to swap. Each entry's `<target>.new` sibling (if any)
    /// is renamed to `<target>` by the updater.
    pub swaps: Vec<SwapEntry>,

    /// Optional path to spawn after all swaps complete.
    #[serde(default)]
    pub relaunch: Option<PathBuf>,

    /// ISO 8601 timestamp set by the launcher when writing this lock.
    /// Used by the new launcher's boot-time recovery to discard stale
    /// locks (>10 min old → crashed updater).
    #[serde(default)]
    pub started_at: Option<String>,
}

/// Result returned to the FE when `prepare_windows_update_handoff`
/// successfully wrote the lock + spawned the updater.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct HandoffResult {
    /// True iff the stage1 handoff actually fired (i.e. we wrote the
    /// lock + spawned vct-updater.exe). The caller MUST proceed to
    /// `app.exit(0)` only when this is true; otherwise it must fall
    /// back to the legacy `restart_launcher` flow.
    pub handoff_active: bool,

    /// Absolute path of the lock file (for diagnostics / forensic).
    pub lock_path: Option<PathBuf>,

    /// Reason the handoff was skipped, when `handoff_active=false`.
    /// Examples: "non-windows", "updater_missing", "no_swaps_needed".
    pub skip_reason: Option<String>,
}

/// Prepare the Windows stage1 update handoff.
///
/// Writes `~/.vct/update.lock.json` describing the binary swaps and
/// spawns `vct-updater.exe` DETACHED. The caller is expected to call
/// `app.exit(0)` immediately afterward when `HandoffResult.handoff_active`
/// is true; the updater will then perform the swap + relaunch.
///
/// `install_root` is the orchestrator clone root (the parent of
/// `launcher/dist/<arch>/`).
///
/// Returns `Ok(HandoffResult { handoff_active: true, .. })` on success.
/// Returns `Ok(HandoffResult { handoff_active: false, skip_reason, .. })`
/// when the handoff was intentionally skipped (POSIX, updater binary
/// absent, etc.) — caller should fall back to legacy flow.
/// Returns `Err(String)` ONLY for truly unrecoverable errors (e.g.
/// install_root not a directory).
#[command]
pub async fn prepare_windows_update_handoff(
    install_root: String,
) -> Result<HandoffResult, String> {
    let install_root_path = PathBuf::from(&install_root);
    if !install_root_path.is_dir() {
        return Err(format!(
            "install_root not a directory: {}",
            install_root_path.display()
        ));
    }

    // POSIX: noop. The rename pattern in installer.rs already handles
    // running-binary overwrite via inode ref-counting. Returning
    // skip_reason="non-windows" lets the caller fall through to the
    // existing `restart_launcher` flow with no behaviour change.
    #[cfg(not(target_os = "windows"))]
    {
        return Ok(HandoffResult {
            handoff_active: false,
            lock_path: None,
            skip_reason: Some("non-windows".to_string()),
        });
    }

    #[cfg(target_os = "windows")]
    {
        // Resolve the dist directory containing the binaries that need
        // swapping. Layout mirrors install.py::_launcher_binary_relative_path.
        let dist_dir = install_root_path
            .join("launcher")
            .join("dist")
            .join("windows-x64");

        let launcher_target = dist_dir.join("vct-launcher.exe");
        let hub_target = dist_dir.join("vct-hub.exe");
        let updater_path = dist_dir.join(UPDATER_BIN);

        // If vct-updater.exe is missing from the dist tree (e.g. older
        // orchestrator clone that doesn't ship it yet), fall back. The
        // launcher's caller will hit the existing v0.2.51 behaviour
        // (Restart banner → user-initiated restart → maybe-locked retry).
        if !updater_path.is_file() {
            return Ok(HandoffResult {
                handoff_active: false,
                lock_path: None,
                skip_reason: Some(format!(
                    "updater_missing: {}",
                    updater_path.display()
                )),
            });
        }

        // Build the swap list. We only include entries where a
        // `<target>.new` sibling exists; if neither launcher nor hub
        // has a staged update, there's nothing for the updater to do.
        // (The launcher itself was renamed pre-pull on Windows; its
        // canonical path holds the new bytes already, and the .old-<pid>
        // sibling is the locked stale binary.)
        let mut swaps: Vec<SwapEntry> = Vec::new();
        for candidate in [&launcher_target, &hub_target] {
            let staged = with_new_suffix(candidate);
            if staged.is_file() {
                swaps.push(SwapEntry {
                    target: candidate.clone(),
                });
            }
        }

        if swaps.is_empty() {
            return Ok(HandoffResult {
                handoff_active: false,
                lock_path: None,
                skip_reason: Some("no_swaps_needed".to_string()),
            });
        }

        let lock = UpdateLock {
            parent_pid: std::process::id(),
            swaps,
            relaunch: Some(launcher_target.clone()),
            started_at: Some(chrono::Utc::now().to_rfc3339()),
        };

        let lock_path = vct_root_dir().join(UPDATE_LOCK_FILE);
        write_lock_file(&lock_path, &lock)?;

        match spawn_updater(&updater_path, &lock_path) {
            Ok(()) => Ok(HandoffResult {
                handoff_active: true,
                lock_path: Some(lock_path),
                skip_reason: None,
            }),
            Err(e) => {
                // Spawn failed — clean up the lock so the new launcher's
                // boot recovery doesn't think a handoff is in-flight.
                let _ = fs::remove_file(&lock_path);
                Ok(HandoffResult {
                    handoff_active: false,
                    lock_path: None,
                    skip_reason: Some(format!("spawn_failed: {}", e)),
                })
            }
        }
    }
}

/// Compute the `<path>.new` staging filename. For `foo.exe`, returns
/// `foo.exe.new`. For `foo` (no extension), returns `foo.new`. This
/// matches the convention used by the updater binary; keep in sync.
#[allow(dead_code)] // exercised by tests + the Windows path
fn with_new_suffix(path: &Path) -> PathBuf {
    let parent = path.parent().unwrap_or_else(|| Path::new(""));
    let name = path
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("");
    parent.join(format!("{}.new", name))
}

/// Atomic write: render to a `.tmp` sibling, then rename onto the
/// canonical name. Prevents a SIGTERM mid-write from leaving a
/// corrupt lock file that the updater couldn't parse.
#[allow(dead_code)] // exercised by tests + the Windows path
fn write_lock_file(path: &Path, lock: &UpdateLock) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("mkdir {}: {}", parent.display(), e))?;
    }
    let tmp = path.with_extension("json.tmp");
    let content = serde_json::to_string_pretty(lock)
        .map_err(|e| format!("serialize lock: {}", e))?;
    fs::write(&tmp, &content).map_err(|e| format!("write tmp {}: {}", tmp.display(), e))?;
    fs::rename(&tmp, path).map_err(|e| format!("rename {}: {}", path.display(), e))?;
    Ok(())
}

/// Spawn `vct-updater.exe` DETACHED with the lock path as argv[1].
/// The updater inherits no stdin/stdout/stderr and runs in its own
/// process group so the launcher's `app.exit(0)` doesn't tear it down.
#[cfg(target_os = "windows")]
fn spawn_updater(updater_path: &Path, lock_path: &Path) -> Result<(), String> {
    use std::os::windows::process::CommandExt;
    use std::process::{Command, Stdio};

    const CREATE_NEW_PROCESS_GROUP: u32 = 0x00000200;
    const DETACHED_PROCESS: u32 = 0x00000008;

    let mut cmd = Command::new(updater_path);
    cmd.arg(lock_path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS);

    cmd.spawn()
        .map(|_child| ())
        .map_err(|e| format!("spawn updater: {}", e))
}

#[cfg(not(target_os = "windows"))]
#[allow(dead_code)]
fn spawn_updater(_updater_path: &Path, _lock_path: &Path) -> Result<(), String> {
    // POSIX: not used (prepare_windows_update_handoff short-circuits
    // with skip_reason="non-windows" before reaching here).
    Ok(())
}

// -----------------------------------------------------------------------------
// Boot-time recovery (Component 4)
// -----------------------------------------------------------------------------

/// Maximum age of an `update.lock.json` before the launcher treats it
/// as stale (= the updater crashed mid-swap). 10 minutes is generous;
/// a real handoff completes in <5s.
const STALE_LOCK_MAX_AGE_SECS: i64 = 600;

/// Result reported to the FE by the boot recovery probe. Wired into
/// the launcher's `setup` callback so the FE can render a one-shot
/// "Updated to vX" toast or a "Update failed" diagnostic.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct UpdateRecoveryReport {
    /// True iff a lock file was found AND it appeared healthy (fresh,
    /// parseable). The FE renders a "success" toast in this case.
    pub recovered: bool,

    /// True iff a lock file was found but was stale or unparseable.
    /// The FE renders an "update may have failed — please verify"
    /// diagnostic in this case (with a "see update.log" link).
    pub stale_or_invalid: bool,

    /// Absolute path of the lock file we found (for the FE link).
    pub lock_path: Option<PathBuf>,

    /// Why the lock was rejected (when stale_or_invalid=true).
    pub reason: Option<String>,
}

/// Called from the launcher's `setup` callback once per process start.
/// Reads `~/.vct/update.lock.json` if present + deletes it (one-shot).
pub fn poll_update_lock_on_boot() -> UpdateRecoveryReport {
    let lock_path = vct_root_dir().join(UPDATE_LOCK_FILE);
    if !lock_path.is_file() {
        return UpdateRecoveryReport::default();
    }

    // Read + parse.
    let content = match fs::read_to_string(&lock_path) {
        Ok(c) => c,
        Err(e) => {
            // Best-effort cleanup — if we can't even read it, delete
            // so it doesn't haunt future starts.
            let _ = fs::remove_file(&lock_path);
            return UpdateRecoveryReport {
                stale_or_invalid: true,
                lock_path: Some(lock_path),
                reason: Some(format!("read failed: {}", e)),
                ..Default::default()
            };
        }
    };
    let lock: UpdateLock = match serde_json::from_str(&content) {
        Ok(l) => l,
        Err(e) => {
            let _ = fs::remove_file(&lock_path);
            return UpdateRecoveryReport {
                stale_or_invalid: true,
                lock_path: Some(lock_path),
                reason: Some(format!("parse failed: {}", e)),
                ..Default::default()
            };
        }
    };

    // Age check: if started_at is older than STALE_LOCK_MAX_AGE_SECS,
    // the updater crashed (or never ran). Don't claim "recovered" in
    // that case; surface a diagnostic so the user knows the update may
    // be incomplete.
    let is_stale = match lock.started_at.as_deref() {
        Some(ts) => match chrono::DateTime::parse_from_rfc3339(ts) {
            Ok(dt) => {
                let age_secs = (chrono::Utc::now() - dt.with_timezone(&chrono::Utc))
                    .num_seconds();
                age_secs > STALE_LOCK_MAX_AGE_SECS
            }
            Err(_) => true, // unparseable timestamp → treat as stale
        },
        None => true, // missing timestamp → can't tell age → treat as stale
    };

    // Always delete the lock file after processing (one-shot).
    let _ = fs::remove_file(&lock_path);

    if is_stale {
        UpdateRecoveryReport {
            stale_or_invalid: true,
            lock_path: Some(lock_path),
            reason: Some("lock_file_too_old_or_undated".to_string()),
            ..Default::default()
        }
    } else {
        UpdateRecoveryReport {
            recovered: true,
            lock_path: Some(lock_path),
            ..Default::default()
        }
    }
}

/// Tauri command exposing `poll_update_lock_on_boot` to the FE so a
/// post-boot frontend can re-query (the setup callback also calls
/// this and emits the result as a Tauri event; this command is the
/// pull alternative for the FE's update tab).
#[command]
pub async fn get_update_recovery_report() -> Result<UpdateRecoveryReport, String> {
    Ok(poll_update_lock_on_boot())
}

// -----------------------------------------------------------------------------
// Tests
// -----------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn isolated_state_dir() -> (TempDir, std::path::PathBuf) {
        let td = tempfile::tempdir().expect("tempdir");
        // Override VCT_STATE_DIR so vct_root_dir() returns td.path().
        std::env::set_var("VCT_STATE_DIR", td.path());
        let p = td.path().to_path_buf();
        (td, p)
    }

    #[test]
    fn lock_file_roundtrip_pretty_format() {
        let (_td, dir) = isolated_state_dir();
        let path = dir.join(UPDATE_LOCK_FILE);

        let lock = UpdateLock {
            parent_pid: 4242,
            swaps: vec![SwapEntry {
                target: PathBuf::from("C:\\foo\\vct-launcher.exe"),
            }],
            relaunch: Some(PathBuf::from("C:\\foo\\vct-launcher.exe")),
            started_at: Some("2026-06-09T18:30:00+00:00".to_string()),
        };
        write_lock_file(&path, &lock).expect("write");

        // Round-trip via the updater's parser shape (same struct).
        let content = fs::read_to_string(&path).expect("read");
        let back: UpdateLock = serde_json::from_str(&content).expect("parse");
        assert_eq!(back.parent_pid, 4242);
        assert_eq!(back.swaps.len(), 1);
        assert!(back.relaunch.is_some());
    }

    #[test]
    fn poll_update_lock_returns_default_when_no_lock() {
        let (_td, _dir) = isolated_state_dir();
        let report = poll_update_lock_on_boot();
        assert!(!report.recovered);
        assert!(!report.stale_or_invalid);
        assert!(report.lock_path.is_none());
    }

    #[test]
    fn poll_update_lock_marks_stale_when_old() {
        let (_td, dir) = isolated_state_dir();
        let path = dir.join(UPDATE_LOCK_FILE);

        // Write a lock with a 1-hour-old timestamp.
        let one_hour_ago = chrono::Utc::now() - chrono::Duration::seconds(3600);
        let lock = UpdateLock {
            parent_pid: 1,
            swaps: vec![],
            relaunch: None,
            started_at: Some(one_hour_ago.to_rfc3339()),
        };
        write_lock_file(&path, &lock).expect("write");

        let report = poll_update_lock_on_boot();
        assert!(!report.recovered);
        assert!(report.stale_or_invalid);
        assert_eq!(report.reason.as_deref(), Some("lock_file_too_old_or_undated"));

        // Lock file should be deleted after processing.
        assert!(!path.is_file());
    }

    #[test]
    fn poll_update_lock_marks_recovered_when_fresh() {
        let (_td, dir) = isolated_state_dir();
        let path = dir.join(UPDATE_LOCK_FILE);

        let lock = UpdateLock {
            parent_pid: 1,
            swaps: vec![],
            relaunch: None,
            started_at: Some(chrono::Utc::now().to_rfc3339()),
        };
        write_lock_file(&path, &lock).expect("write");

        let report = poll_update_lock_on_boot();
        assert!(report.recovered);
        assert!(!report.stale_or_invalid);
    }

    #[test]
    fn poll_update_lock_handles_unparseable() {
        let (_td, dir) = isolated_state_dir();
        let path = dir.join(UPDATE_LOCK_FILE);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, "{ not json").expect("write garbage");

        let report = poll_update_lock_on_boot();
        assert!(!report.recovered);
        assert!(report.stale_or_invalid);
        assert!(report.reason.unwrap().contains("parse failed"));
        assert!(!path.is_file()); // cleaned up
    }

    #[test]
    fn with_new_suffix_adds_dot_new() {
        assert_eq!(
            with_new_suffix(Path::new("/foo/vct-launcher.exe")),
            PathBuf::from("/foo/vct-launcher.exe.new")
        );
        assert_eq!(
            with_new_suffix(Path::new("/foo/vct-launcher")),
            PathBuf::from("/foo/vct-launcher.new")
        );
    }

    #[cfg(not(target_os = "windows"))]
    #[tokio::test]
    async fn prepare_handoff_skips_on_posix() {
        // On POSIX the command always returns handoff_active=false
        // (the rename pattern in installer.rs handles binary swap).
        let td = tempfile::tempdir().unwrap();
        let result = prepare_windows_update_handoff(td.path().to_string_lossy().to_string())
            .await
            .expect("ok");
        assert!(!result.handoff_active);
        assert_eq!(result.skip_reason.as_deref(), Some("non-windows"));
    }

    #[tokio::test]
    async fn prepare_handoff_errors_on_missing_install_root() {
        let result = prepare_windows_update_handoff(
            "/this/path/does/not/exist/anywhere".to_string(),
        )
        .await;
        assert!(result.is_err());
    }
}
