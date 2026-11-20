// SPDX-License-Identifier: AGPL-3.0-or-later
//! Per-user launcher instance lock — taken as the FIRST thing `run()` does
//! after the CLI dispatch, before any start-up work (v0.2.107 LAUNCH-ONCE).
//!
//! ## Why the single-instance plugin is not enough
//!
//! `tauri-plugin-single-instance` refuses a second launcher only inside
//! `tauri::Builder`, i.e. AFTER everything `run()` does before the builder:
//! opening and migrating `launcher.db`, the orchestrator-root registration,
//! the project backfill, the boot-time exports. And on Windows its own check
//! has a gap: when the first launcher's mutex exists but its message window
//! does not yet (the first one is still booting), the plugin lets the second
//! one run in full. A 2026-11 Windows field report had exactly that — two
//! launchers started together at boot, both ran the whole start-up (DB, root
//! KG seed, engine autostart, the hub started twice), and quitting one with
//! "Quit and stop services" stopped Weaviate under the other.
//!
//! ## The mechanism
//!
//! An exclusive OS file lock (`std::fs::File::try_lock` — `flock` on Unix,
//! `LockFileEx` on Windows, the same call on all three OSes) on
//! `<vct_root>/state/launcher-instance.lock`, held for the life of the
//! process. The kernel drops it when the process exits, crash and kill
//! included: there is no stale-lock state and no TTL. It is keyed on the
//! state root, which is what a launcher's state (its `launcher.db`) hangs
//! off.
//!
//! A process that does not get the lock exits before touching anything —
//! except that a user launching the app again expects the running launcher
//! to come to the front. That hand-off is the plugin's (D-Bus on Linux, a
//! window message on Windows, a socket on macOS), and it is only safe once
//! the lock holder has REGISTERED the plugin: a second process that ran the
//! plugin first would take the plugin's name and make the holder hand off to
//! IT. So the holder writes `launcher-instance.ready` (its pid) from `setup`,
//! which Tauri runs after plugin initialisation, and a user launch waits —
//! bounded — for that marker before handing off. A tray-only start
//! (`--start-hidden`, the session-start ensure) and a relaunch child that has
//! already waited out its parent never hand off: they just exit.
//!
//! The marker is a separate file because a Windows byte-range lock blocks
//! reads of the locked file itself. A marker left by a dead launcher is
//! ignored (its pid is not alive) and the next holder deletes it on acquire.

use std::fs::{File, OpenOptions};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::Duration;

/// The lock file, under `<vct_root>/state/`.
pub(crate) const LOCK_BASENAME: &str = "launcher-instance.lock";
/// The holder's "plugin registered" marker (its pid), beside the lock.
pub(crate) const READY_BASENAME: &str = "launcher-instance.ready";

/// How long a user launch waits for the holder to become ready before it
/// gives up (the holder's pre-builder start-up normally takes seconds).
pub(crate) const HAND_OFF_WAIT: Duration = Duration::from_secs(60);
/// Poll cadence of that wait.
pub(crate) const HAND_OFF_POLL: Duration = Duration::from_millis(250);

pub(crate) fn lock_path(root: &Path) -> PathBuf {
    root.join("state").join(LOCK_BASENAME)
}

pub(crate) fn ready_path(root: &Path) -> PathBuf {
    root.join("state").join(READY_BASENAME)
}

/// What one attempt at the lock answered.
#[derive(Debug)]
pub(crate) enum LockAttempt {
    /// This process now holds the lock (keep the handle alive).
    Acquired(File),
    /// Another live process holds it.
    Busy,
    /// The lock could not be asked at all (unwritable state dir, a
    /// filesystem without locks). Named, never fatal.
    Unavailable(String),
}

/// One non-blocking attempt. Creates `<root>/state/` and the lock file.
pub(crate) fn try_acquire(root: &Path) -> LockAttempt {
    let path = lock_path(root);
    if let Some(parent) = path.parent() {
        if let Err(e) = std::fs::create_dir_all(parent) {
            return LockAttempt::Unavailable(format!("cannot create {}: {}", parent.display(), e));
        }
    }
    let file = match OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .truncate(false)
        .open(&path)
    {
        Ok(f) => f,
        Err(e) => {
            return LockAttempt::Unavailable(format!("cannot open {}: {}", path.display(), e))
        }
    };
    match file.try_lock() {
        Ok(()) => LockAttempt::Acquired(file),
        Err(std::fs::TryLockError::WouldBlock) => LockAttempt::Busy,
        Err(std::fs::TryLockError::Error(e)) => {
            LockAttempt::Unavailable(format!("cannot lock {}: {}", path.display(), e))
        }
    }
}

/// Is the CURRENT holder ready for a hand-off? The marker must name a live
/// process other than this one.
pub(crate) fn holder_ready(root: &Path, is_alive: impl Fn(u32) -> bool) -> bool {
    let Ok(raw) = std::fs::read_to_string(ready_path(root)) else {
        return false;
    };
    match raw.trim().parse::<u32>() {
        Ok(pid) if pid != 0 && pid != std::process::id() => is_alive(pid),
        _ => false,
    }
}

/// What `run()` does next. PURE — every input is passed in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Step {
    /// Carry on with the start-up: this process is the launcher.
    Primary,
    /// Run the single-instance plugin alone, so it hands this launch to the
    /// running launcher (focus), then exit.
    HandOff,
    /// Exit at once, touching nothing.
    Exit,
    /// Sleep one poll, then ask again.
    Wait,
}

/// The decision. `lock` is `Some(true)` acquired, `Some(false)` busy, `None`
/// unavailable.
pub(crate) fn decide(
    lock: Option<bool>,
    hidden: bool,
    relaunch_child: bool,
    holder_ready: bool,
    waited: Duration,
    bound: Duration,
) -> Step {
    match lock {
        // Held — or not askable, in which case the plugin stays the guard,
        // exactly as before this lock existed (logged by the caller).
        Some(true) | None => Step::Primary,
        Some(false) if hidden || relaunch_child => Step::Exit,
        Some(false) if holder_ready => Step::HandOff,
        Some(false) if waited >= bound => Step::Exit,
        Some(false) => Step::Wait,
    }
}

/// The handle, kept for the life of the process, and the root it was taken
/// under (where `mark_ready` writes).
static HELD: OnceLock<(File, PathBuf)> = OnceLock::new();

/// Keep the lock for the life of the process and clear a marker a previous
/// (dead) holder left behind.
pub(crate) fn hold(file: File, root: PathBuf) {
    let _ = std::fs::remove_file(ready_path(&root));
    let _ = HELD.set((file, root));
}

/// Called from `setup` (after plugin initialisation): this launcher can now
/// receive a hand-off. Best-effort; no-op when the lock is not held.
pub(crate) fn mark_ready() {
    let Some((_, root)) = HELD.get() else {
        return;
    };
    let path = ready_path(root);
    let tmp = path.with_extension("ready.tmp");
    if std::fs::write(&tmp, format!("{}\n", std::process::id())).is_ok() {
        if std::fs::rename(&tmp, &path).is_err() {
            let _ = std::fs::remove_file(&tmp);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const B: Duration = HAND_OFF_WAIT;
    const Z: Duration = Duration::ZERO;

    #[test]
    fn the_holder_carries_on_and_an_unaskable_lock_degrades_to_the_plugin() {
        assert_eq!(decide(Some(true), true, true, false, Z, B), Step::Primary);
        assert_eq!(decide(None, false, false, false, Z, B), Step::Primary);
    }

    #[test]
    fn a_tray_only_start_or_a_relaunch_child_that_finds_the_lock_busy_exits() {
        assert_eq!(decide(Some(false), true, false, true, Z, B), Step::Exit);
        assert_eq!(decide(Some(false), false, true, true, Z, B), Step::Exit);
    }

    #[test]
    fn a_user_launch_waits_for_the_holder_then_hands_off_or_gives_up() {
        assert_eq!(decide(Some(false), false, false, false, Z, B), Step::Wait);
        assert_eq!(decide(Some(false), false, false, true, Z, B), Step::HandOff);
        assert_eq!(decide(Some(false), false, false, false, B, B), Step::Exit);
    }

    fn scratch(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!(
            "vct-instance-lock-{}-{}",
            name,
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&d);
        d
    }

    #[test]
    fn a_second_attempt_is_busy_while_the_first_handle_lives_and_free_after() {
        let root = scratch("busy");
        let first = match try_acquire(&root) {
            LockAttempt::Acquired(f) => f,
            other => panic!("first attempt must acquire, got {:?}", other),
        };
        assert!(matches!(try_acquire(&root), LockAttempt::Busy));
        drop(first); // what process exit does
        assert!(matches!(try_acquire(&root), LockAttempt::Acquired(_)));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn the_ready_marker_counts_only_for_a_live_other_process() {
        let root = scratch("ready");
        std::fs::create_dir_all(root.join("state")).unwrap();
        assert!(!holder_ready(&root, |_| true), "no marker");
        std::fs::write(ready_path(&root), "4242\n").unwrap();
        assert!(holder_ready(&root, |p| p == 4242));
        assert!(!holder_ready(&root, |_| false), "a dead holder's marker");
        std::fs::write(ready_path(&root), format!("{}\n", std::process::id())).unwrap();
        assert!(!holder_ready(&root, |_| true), "our own pid");
        std::fs::write(ready_path(&root), "garbage").unwrap();
        assert!(!holder_ready(&root, |_| true));
        let _ = std::fs::remove_dir_all(&root);
    }
}
