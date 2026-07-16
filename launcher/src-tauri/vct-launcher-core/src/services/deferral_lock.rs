// SPDX-License-Identifier: AGPL-3.0-or-later
//! Cross-writer file lock for the `UPDATE_DEFERRED.{md,json}` read-modify-write
//! cycle (v0.2.83 WP-B6).
//!
//! ## Why this module exists
//!
//! `UPDATE_DEFERRED.{md,json}` has many writer families. Most Rust launcher
//! writers DELEGATE to Python (`vco_lib.deferral_report` via a `python -c`
//! snippet in [`crate`]'s sibling launcher crate); v0.2.83 routed those
//! through the LOCKED Python emitter (`vco_lib.deferral_emit`), which holds an
//! exclusive `flock` on `<folder>/.claude/context/.update-deferred.lock` for
//! the whole read → mutate → write cycle. See `vco_lib/deferral_emit.py`.
//!
//! But a handful of launcher writers touch the file DIRECTLY with `std::fs`,
//! because they run precisely when Python cannot be assumed (mid-update, when
//! `install.py` never fired / the venv may be broken):
//!
//!   * `installer::write_update_resume_deferral` (full `UPDATE_DEFERRED.md`
//!     rewrite when a merge/rebase conflict halted the update).
//!   * `installer::clear_update_resume_deferral_if_solo` (read + delete the
//!     `.md` + `.json` sidecar when the resume entry is the only one).
//!   * `git_user_editable_merge::write_launcher_update_diverged_deferral`
//!     (full `UPDATE_DEFERRED.md` rewrite on a non-FF / binary-refresh-timeout
//!     launcher-side update failure).
//!   * `restart::clear_restart_deferral` (read + strip the
//!     `launcher_restart_required` section, then rewrite-or-delete the `.md` +
//!     `.json` sidecar).
//!
//! Without a shared lock, those direct writers can interleave with a concurrent
//! Python writer holding the SAME lock and drop entries (or clobber a fresh
//! write with a stale full-rewrite). This module gives the Rust side the SAME
//! lock the Python emitter uses — [`lock_folder`] acquires an exclusive
//! advisory lock on the identical path.
//!
//! ## Same lock token as Python
//!
//! [`LOCK_REL`] pins `.claude/context/.update-deferred.lock`, byte-identical to
//! `vco_lib.deferral_emit.LOCK_REL`. A cross-language parity test
//! (`tests/test_deferral_lock_parity.py`) string-pins the two so they can never
//! drift. POSIX `flock` is process-shared and advisory-but-cooperative: as long
//! as EVERY writer (Python + these four Rust functions) acquires this one lock
//! before its read-modify-write, they serialize.
//!
//! ## Best-effort, symmetric with the Python side
//!
//! [`crate::services::deferral_lock::exclusive_file_lock`]'s contract mirrors
//! `vco_lib.atomic.exclusive_file_lock` exactly:
//!
//!   * **POSIX**: a real `libc::flock(LOCK_EX)` on a sidecar lockfile. A second
//!     acquirer blocks until the first releases (on `Drop`, or on process
//!     death — the kernel releases the flock when the fd closes).
//!   * **Windows / any host where the lock cannot be taken**: the lockfile is
//!     still opened but locking is SKIPPED (best-effort no-lock), so the caller
//!     proceeds with NO mutual exclusion. This matches the Python side's
//!     `ImportError` / `OSError` fall-through — behaviour stays symmetric across
//!     the two languages so neither deadlocks the other on a platform without
//!     working advisory locks.
//!
//! A failure to open / lock never propagates: a deferral write is an FYI
//! mechanism and must never crash the operation that triggered it. [`lock_folder`]
//! therefore ALWAYS returns a guard (which may hold no real lock) rather than a
//! `Result` — the caller does its write inside the guard's lifetime either way.
//!
//! The lock idiom (POSIX `libc::flock` + RAII `Drop` release; `libc = "0.2"` is
//! already a workspace dependency) is the same one `secrets.rs`'s cross-process
//! keychain pace lock uses — no new crate dependency.

use std::path::{Path, PathBuf};

/// The shared lock token, relative to a managed project folder. Sits beside
/// `UPDATE_DEFERRED.{md,json}` under `.claude/context/` (which is git-ignored,
/// matching `UPDATE_DEFERRED.md`'s posture), so the lockfile never enters
/// version control.
///
/// MUST MATCH `vco_lib.deferral_emit.LOCK_REL`
/// (`.claude/context/.update-deferred.lock`). The parity test
/// `tests/test_deferral_lock_parity.py` pins this string against the Python
/// constant — if you change one, change both.
pub const LOCK_REL: &str = ".claude/context/.update-deferred.lock";

/// RAII holder of the exclusive advisory lock on the deferral lockfile.
///
/// Dropping the guard releases the lock (explicit `LOCK_UN` on POSIX; the kernel
/// also releases on fd close / process death, so a crash inside the locked block
/// can never leave the lock held past process exit). On a best-effort no-lock
/// platform the guard holds only the open file handle (or nothing) and `Drop`
/// is a no-op release.
///
/// The `_file` handle is kept alive for the guard's whole lifetime because on
/// POSIX the `fd` we locked is a borrow of it — closing the file would drop the
/// lock, so the file must outlive the guard's use.
pub struct DeferralLock {
    /// The open lockfile handle. `None` only when the lockfile could not be
    /// opened at all (best-effort: the caller still proceeds unlocked).
    _file: Option<std::fs::File>,
    /// On POSIX, the raw fd we called `flock(LOCK_EX)` on, so `Drop` can
    /// `flock(LOCK_UN)` the SAME fd. `None` when no real lock is held (open
    /// failed, flock failed, or a non-POSIX host).
    #[cfg(unix)]
    locked_fd: Option<std::os::unix::io::RawFd>,
}

#[cfg(unix)]
impl Drop for DeferralLock {
    fn drop(&mut self) {
        // Explicit unlock releases the advisory lock before the file handle's
        // own drop; the kernel also releases on close, but explicit LOCK_UN
        // makes the release deterministic w.r.t. drop reordering. Mirrors
        // `secrets.rs`'s `PaceLock` / `FileLock` idiom.
        if let Some(fd) = self.locked_fd {
            unsafe {
                libc::flock(fd, libc::LOCK_UN);
            }
        }
    }
}

/// Resolve the deferral lockfile for `folder` and acquire the shared exclusive
/// lock around the caller's read-modify-write of `UPDATE_DEFERRED.{md,json}`.
///
/// The returned [`DeferralLock`] guard must be held for the WHOLE
/// read → mutate → write cycle and dropped only after the last write completes,
/// so a concurrent writer (a Python emitter, or another Rust direct writer)
/// cannot interleave. Usage:
///
/// ```ignore
/// let _lock = deferral_lock::lock_folder(install_path);
/// // ... read UPDATE_DEFERRED.md, mutate, write it back ...
/// // lock released here when `_lock` drops.
/// ```
///
/// Best-effort throughout: on POSIX this blocks until the exclusive lock is
/// acquired; on Windows / any host without working advisory locks it degrades
/// to a no-lock guard (the caller proceeds unlocked, matching the Python side).
/// It NEVER returns an error — a deferral write must never be gated on the lock
/// being takeable.
pub fn lock_folder(folder: &Path) -> DeferralLock {
    let lock_path = deferral_lock_path(folder);
    exclusive_file_lock(&lock_path)
}

/// The absolute path of the deferral lockfile for `folder`
/// (`<folder>/.claude/context/.update-deferred.lock`). Exposed for tests /
/// callers that want to observe the exact path.
pub fn deferral_lock_path(folder: &Path) -> PathBuf {
    folder.join(LOCK_REL)
}

/// Acquire an exclusive advisory lock on `lock_path`, creating its parent
/// directory and the lockfile if absent. See [`DeferralLock`] for the contract.
///
/// POSIX: real `flock(LOCK_EX)` (blocks until acquired). Windows / no-flock
/// hosts: best-effort no-lock (the file is opened if possible, then the caller
/// proceeds without mutual exclusion). Always returns a guard.
pub fn exclusive_file_lock(lock_path: &Path) -> DeferralLock {
    // Create the parent dir so a fresh project folder (`.claude/context/` not
    // yet materialised) still locks rather than silently skipping.
    if let Some(parent) = lock_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }

    // Open (create-if-absent, never truncate) — the lockfile is a pure lock
    // token; its contents are irrelevant. Mirrors the Python side's `"a+"`.
    let file = std::fs::OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .truncate(false)
        .open(lock_path)
        .ok();

    acquire_lock_on_file(file)
}

#[cfg(unix)]
fn acquire_lock_on_file(file: Option<std::fs::File>) -> DeferralLock {
    use std::os::unix::io::AsRawFd;

    let Some(file) = file else {
        // Could not open the lockfile at all — best-effort: proceed unlocked.
        return DeferralLock {
            _file: None,
            locked_fd: None,
        };
    };

    let fd = file.as_raw_fd();
    // LOCK_EX blocks until acquired. A deferral write is a bounded file op, so
    // any wait here is bounded transitively by the holder's own short cycle.
    let rc = unsafe { libc::flock(fd, libc::LOCK_EX) };
    if rc != 0 {
        // flock failed (e.g. a filesystem without advisory-lock support) —
        // degrade to best-effort no-lock, matching the Python fall-through.
        return DeferralLock {
            _file: Some(file),
            locked_fd: None,
        };
    }

    DeferralLock {
        _file: Some(file),
        locked_fd: Some(fd),
    }
}

#[cfg(not(unix))]
fn acquire_lock_on_file(file: Option<std::fs::File>) -> DeferralLock {
    // Windows / non-POSIX: best-effort no-lock, symmetric with the Python
    // side's Windows degradation (`exclusive_file_lock` skips locking when
    // `fcntl` is unavailable). We still hold the open handle for the guard's
    // lifetime so callers see the same "guard is alive during the write" shape.
    DeferralLock { _file: file }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lock_rel_matches_python_constant() {
        // Structural pin: the Rust LOCK_REL must be the same POSIX-style path
        // the Python `vco_lib.deferral_emit.LOCK_REL` renders. The cross-
        // language parity test (tests/test_deferral_lock_parity.py) asserts the
        // Python side; this asserts the Rust literal is what that test expects.
        assert_eq!(LOCK_REL, ".claude/context/.update-deferred.lock");
    }

    #[test]
    fn deferral_lock_path_joins_under_claude_context() {
        let folder = Path::new("/tmp/some-project");
        let p = deferral_lock_path(folder);
        assert_eq!(
            p,
            Path::new("/tmp/some-project/.claude/context/.update-deferred.lock")
        );
    }

    #[test]
    fn lock_folder_creates_lockfile_and_returns_guard() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let guard = lock_folder(tmp.path());
        // The lockfile (and its parent dirs) must have been created.
        assert!(
            deferral_lock_path(tmp.path()).is_file(),
            "lockfile should be created on acquire"
        );
        drop(guard);
        // Lockfile persists after release (pure token; safe to leave on disk).
        assert!(deferral_lock_path(tmp.path()).is_file());
    }

    /// On POSIX, a second acquirer must BLOCK until the first releases. We
    /// prove serialization: hold the lock on the main thread, spawn a thread
    /// that tries to acquire the SAME folder's lock and records the time it
    /// succeeds; assert it only succeeds AFTER we release. ms-scaled.
    #[cfg(unix)]
    #[test]
    fn second_acquirer_blocks_until_first_releases() {
        use std::sync::mpsc;
        use std::time::{Duration, Instant};

        let tmp = tempfile::tempdir().expect("tempdir");
        let folder = tmp.path().to_path_buf();

        let guard = lock_folder(&folder);
        // Sanity: we hold a real lock on this platform (flock succeeded).
        assert!(
            guard.locked_fd.is_some(),
            "expected a real flock on this POSIX host"
        );

        let (tx, rx) = mpsc::channel::<Instant>();
        let folder2 = folder.clone();
        let handle = std::thread::spawn(move || {
            // This blocks on flock(LOCK_EX) until the main thread drops `guard`.
            let g = lock_folder(&folder2);
            let acquired_at = Instant::now();
            tx.send(acquired_at).expect("send acquire time");
            // Hold briefly, then release.
            drop(g);
        });

        // Give the child thread time to reach (and block on) the flock.
        std::thread::sleep(Duration::from_millis(120));
        // The child must NOT have acquired yet — the lock is held here.
        assert!(
            rx.try_recv().is_err(),
            "second acquirer acquired the lock while the first still held it"
        );

        let released_at = Instant::now();
        drop(guard); // release → the child's blocked flock now returns.

        let acquired_at = rx
            .recv_timeout(Duration::from_secs(5))
            .expect("child thread must acquire after release");
        assert!(
            acquired_at >= released_at,
            "child acquired ({acquired_at:?}) before we released ({released_at:?})"
        );
        handle.join().expect("child thread joins");
    }
}
