//! Keychain pacing + bounded-timeout worker machinery for [`crate::secrets`].
//!
//! Extracted wholesale from `secrets.rs` in v0.2.102 (review N2, the
//! file-size house rule) — a PURE MOVE, no behaviour change. Contents:
//!
//! * the in-process rate limiter (`paced_call`, ≥150ms process-wide
//!   spacing) and its test seam (`TestSpacingGuard`),
//! * the cross-process flock pacer (`cross_process_pace`, one
//!   `<vct_root>/keyring.pace` lock across every VCO process) and its
//!   test seams (`TestPacePathGuard`, and `TestBatonPaceGuard`, which moves a
//!   keychain test's pacing off the production file — v0.2.102 W6-KC),
//! * the bounded keychain worker: a single worker thread fed by a channel,
//!   busy-vs-hung classification (`KEYCHAIN_OP_TIMEOUT` /
//!   `KEYCHAIN_INTERACTIVE_STUCK_AFTER`), abandoned-worker recycling
//!   (`ABANDONED_WORKER_OUTSTANDING`, `WorkerStuck`) and the
//!   `KeychainTimeout` error surface `secrets` embeds in its error details.
//!
//! `secrets` re-exports the items its call sites and tests already name, so
//! every existing `secrets::…` path keeps resolving. The guarded
//! `set`/`get`/`delete` chokepoint, `retry_with_backoff`, the read-session
//! memo, the lock-state probe and the mock seam remain in `secrets.rs`.

use crate::secrets::CallContext;

/// Minimum spacing between consecutive keychain calls, process-wide.
/// 2026-05-13: introduced after a SIGTRAP crash of gnome-keyring under
/// burst load. ~6.7 calls/sec is well below the threshold at which the
/// daemon has been observed to crash and is invisible to any caller
/// that doesn't burst (single-shot calls pay <1ms mutex acquisition).
const MIN_CALL_SPACING: std::time::Duration = std::time::Duration::from_millis(150);

/// Process-wide last-call timestamp. Updated by `paced_call` on every
/// keychain hit. The `Mutex<Option<Instant>>` is initialised lazily on
/// first call.
static LAST_KEYRING_CALL: std::sync::Mutex<Option<std::time::Instant>> =
    std::sync::Mutex::new(None);

/// Enforce a minimum inter-call spacing of `MIN_CALL_SPACING` and then
/// run `f`. The mutex is held only long enough to read/update the
/// timestamp — `f` runs OUTSIDE the lock so concurrent callers serialise
/// on the spacing requirement, not on the closure body. The pacing is
/// applied to every attempt inside `retry_with_backoff`, so a retry pays
/// the 150ms gate again — that's intentional, the daemon needs the
/// same minimum break before each request.
///
/// v0.2.84 (D8.3, release-gap fix): the last-call timestamp is recorded
/// AFTER `f` COMPLETES, not before it. The pre-fix code stamped op-START,
/// so an op that itself took ≥ `MIN_CALL_SPACING` (a slow daemon read)
/// left ~0 idle gap between one op's END and the next op's START —
/// sustained back-to-back daemon traffic under full pacing compliance,
/// which is the load the P7 gnome-keyring SIGTRAP reproduced under. By
/// stamping op-END, the NEXT call spaces off the previous op's COMPLETION
/// so the daemon always sees ≥ `MIN_CALL_SPACING` of true idle between
/// consecutive requests. The spacing VALUE is unchanged (150ms).
///
/// Test-visible: `MIN_CALL_SPACING` can be overridden inside `#[cfg(test)]`
/// via `with_test_spacing` to keep the test suite fast. Production
/// callers always pay the full 150ms.
pub(crate) fn paced_call<T>(f: impl FnOnce() -> T) -> T {
    let spacing = current_spacing();
    {
        // First lock acquisition: WAIT out the remaining spacing from the
        // previous op's END. The wait happens under the lock so concurrent
        // in-process callers serialise on the spacing requirement (contract
        // unchanged). We deliberately do NOT stamp a start-time here — the
        // stamp is written after `f` below (D8.3).
        let last = LAST_KEYRING_CALL.lock().unwrap_or_else(|p| p.into_inner());
        if let Some(prev) = *last {
            let elapsed = prev.elapsed();
            if elapsed < spacing {
                std::thread::sleep(spacing - elapsed);
            }
        }
    }
    // `f` runs OUTSIDE the lock (contract unchanged — concurrent callers
    // serialise on spacing, not on the closure body).
    let result = f();
    {
        // Second (brief) acquisition: record the op's COMPLETION time so the
        // NEXT call measures its idle gap from when THIS op finished, not
        // when it started (D8.3). In production `paced_call` runs on the
        // single-threaded keychain worker inside the held cross-process
        // flock, so no concurrent writer races this update.
        let mut last = LAST_KEYRING_CALL.lock().unwrap_or_else(|p| p.into_inner());
        *last = Some(std::time::Instant::now());
    }
    result
}

#[cfg(any(test, debug_assertions))]
static TEST_SPACING_OVERRIDE: std::sync::Mutex<Option<std::time::Duration>> =
    std::sync::Mutex::new(None);

#[cfg(any(test, debug_assertions))]
fn current_spacing() -> std::time::Duration {
    TEST_SPACING_OVERRIDE
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .unwrap_or(MIN_CALL_SPACING)
}

#[cfg(not(any(test, debug_assertions)))]
fn current_spacing() -> std::time::Duration {
    MIN_CALL_SPACING
}

/// Test-only RAII guard that swaps the pacing duration for the lifetime
/// of the guard. Restores the previous value (usually `None` → production
/// 150ms) on drop. Tests that exercise the rate-limit semantics use this
/// to keep wall-clock cost low while still proving the contract.
#[cfg(any(test, debug_assertions))]
pub struct TestSpacingGuard {
    prev: Option<std::time::Duration>,
}

#[cfg(any(test, debug_assertions))]
impl TestSpacingGuard {
    pub fn new(spacing: std::time::Duration) -> Self {
        let mut slot = TEST_SPACING_OVERRIDE.lock().unwrap_or_else(|p| p.into_inner());
        let prev = *slot;
        *slot = Some(spacing);
        Self { prev }
    }
}

#[cfg(any(test, debug_assertions))]
impl Drop for TestSpacingGuard {
    fn drop(&mut self) {
        let mut slot = TEST_SPACING_OVERRIDE.lock().unwrap_or_else(|p| p.into_inner());
        *slot = self.prev;
    }
}

/// Test-only RAII guard pinning the cross-process pace file to an isolated
/// path (usually a `tempfile::TempDir` child) for the guard's lifetime.
/// Restores the previous override on drop. Unix-only (the pace module is
/// `cfg(unix)`).
#[cfg(all(unix, any(test, debug_assertions)))]
pub struct TestPacePathGuard {
    prev: Option<std::path::PathBuf>,
}

#[cfg(all(unix, any(test, debug_assertions)))]
impl TestPacePathGuard {
    pub fn new(path: std::path::PathBuf) -> Self {
        cross_process_pace::reset_warn_latch_for_test();
        let prev = cross_process_pace::set_test_pace_path(Some(path));
        Self { prev }
    }
    /// Read-back of the last-call timestamp the pace file holds (nanos).
    pub fn pace_timestamp(&self) -> Option<u128> {
        cross_process_pace::read_pace_timestamp_for_test()
    }
    /// How many degrade-warns were emitted since this guard reset the latch.
    pub fn warn_count(&self) -> usize {
        cross_process_pace::WARN_COUNT.load(std::sync::atomic::Ordering::SeqCst)
    }
}

#[cfg(all(unix, any(test, debug_assertions)))]
impl Drop for TestPacePathGuard {
    fn drop(&mut self) {
        cross_process_pace::set_test_pace_path(self.prev.take());
        cross_process_pace::reset_warn_latch_for_test();
    }
}

// ─── Cross-PROCESS pacing (v0.2.82 WP-4a / G5), unix-only ─────────────────────
//
// `paced_call` above serializes keychain hits WITHIN one process. The G5
// incident showed that's not enough: "Update all projects" runs the launcher
// GUI + vct-hub (+ CLI + tests) as SEPARATE processes, each with its own
// `LAST_KEYRING_CALL` pacer, so the daemon still saw a machine-level burst and
// SIGTRAPed. This layer adds a kernel-level `flock(EXCLUSIVE)` on
// `<vct_root>/keyring.pace` HELD ACROSS the keychain op, so at most ONE VCO
// process touches gnome-keyring at a time machine-wide.
//
// Mechanics per call:
//   1. flock(EXCLUSIVE) the pace file, bounded by `PACE_LOCK_ACQUIRE_DEADLINE`
//      (LOCK_NB in a poll loop — NOT a blocking LOCK_EX; see the P0 note below).
//   2. Read the last-call timestamp stored in the file; sleep any remaining
//      `MIN_CALL_SPACING` (cross-process spacing, mirrors the in-process gate).
//   3. Write the new timestamp.
//   4. Run the op WHILE STILL HOLDING the lock (so no sibling process can
//      interleave a concurrent daemon request).
//   5. Drop the guard → flock(LOCK_UN). Crash-safety: the kernel releases the
//      lock on process death, so a crashed holder never wedges the others.
//
// Fallbacks (each logs EXACTLY ONE warn, then degrades to in-process pacing =
// today's behaviour):
//   * pace file uncreatable (root dir unwritable) → per-process pacing only.
//   * lock still held by a sibling VCO process after
//     `PACE_LOCK_ACQUIRE_DEADLINE` → per-process pacing only.
//   * non-unix (windows-native / apple-native have no crashy daemon) →
//     per-process pacing only (the whole module is `cfg(unix)`; the
//     `run_with_cross_process_pace` shim is a pass-through elsewhere).
//
// ─── Why the acquire is BOUNDED (P0 availability fix) ─────────────────────────
//
// Step 1 used to be a plain blocking `flock(fd, LOCK_EX)` justified by: "a held
// keychain op is bounded by the caller's timeout worker, so the wait here is
// bounded transitively." That justification was FALSE, and the failure it
// allowed wedged the launcher AND the hub simultaneously with no self-heal:
//
//   * `KEYCHAIN_OP_TIMEOUT` bounds how long the CALLER waits for a result. It
//     does NOT bound the op: `run_keychain_with_timeout` gives up on the
//     result channel and returns `TimedOut`, but nothing cancels the job — the
//     worker thread keeps running it, still holding this flock.
//   * The op itself has no bound when it reaches an interactive unlock prompt.
//     dbus-secret-service polls a prompt for `timeout.unwrap_or(ONE_YEAR)`
//     seconds (`prompt.rs::execute_prompt`), and a human at a passphrase dialog
//     is in any case slower than the 10s caller budget.
//
// So ONE unlock prompt parked the holder's single keychain worker while it held
// this flock; every sibling VCO process then blocked FOREVER in step 1, its own
// worker consumed, so every later keychain call in that process fast-failed
// `WorkerStuck` permanently. Restarting the GUI did not help (it re-blocked on
// the still-held flock); only killing the holder helped, because process death
// is what released the lock.
//
// What is guaranteed NOW, and under which assumption:
//   * GUARANTEED unconditionally: this acquire returns within
//     `PACE_LOCK_ACQUIRE_DEADLINE`, so a waiter's worker thread is ALWAYS
//     released — no cross-process wedge can outlive that deadline, whatever the
//     holder is doing (prompt, wedged daemon, SIGSTOP).
//   * ASSUMED (checkable, not inherited): that losing the lock costs only
//     PACING, never correctness. That holds because the guard is inert data —
//     `run_with_cross_process_pace` runs `op` identically with or without it,
//     and only the completion STAMP is skipped when it is absent (we must not
//     write the file we do not own). In-process `paced_call` still spaces this
//     process's own calls. If a future change makes `op` depend on holding the
//     guard, this deadline stops being safe and must be revisited.
//
// The `MIN_CALL_SPACING` test override (`with_test_spacing` / `TestSpacingGuard`)
// extends here too via `current_spacing()`, so the suite stays fast.
#[cfg(unix)]
pub(crate) mod cross_process_pace {
    use std::io::{Read, Seek, SeekFrom, Write};
    use std::os::unix::io::AsRawFd;
    use std::path::PathBuf;

    /// Where the cross-process pace/lock file lives. One per user, alongside
    /// the other launcher state (`hub.pid`, `hub.token`, …) so every VCO
    /// process resolves the SAME path. Resolution reuses `paths::vct_root_dir`
    /// (honours `VCT_STATE_DIR`), so a dev launcher and a prod launcher pace
    /// against their own root — correct, since they hit distinct state but the
    /// SAME OS keychain… note: the OS keychain is shared per-OS-user
    /// regardless of VCT_STATE_DIR. To serialize against the daemon (not just
    /// within one root) the lock path is intentionally NOT under vct_root when
    /// an override is present for tests; production always uses vct_root.
    ///
    /// Resolution order (the two test layers exist only in test/debug builds,
    /// and both are empty unless a test installed them):
    ///   1. an explicit test pin ([`super::TestPacePathGuard`]) — a test that
    ///      exercises the real pacing on its own temp file;
    ///   2. the keychain-test baton's file ([`TEST_BATON_PACE_FILE_NAME`],
    ///      installed by `secrets::test_serialize::keychain_serialize_lock`
    ///      while a keychain test holds it — v0.2.102 W6-KC);
    ///   3. the production file, [`production_pace_file_path`].
    ///
    /// A release build has no test layer at all: it compiles to the production
    /// file only.
    #[cfg(any(test, debug_assertions))]
    pub(crate) fn pace_file_path() -> PathBuf {
        resolve_pace_file_path(test_pace_path_override(), test_baton_pace_path())
    }

    #[cfg(not(any(test, debug_assertions)))]
    pub(crate) fn pace_file_path() -> PathBuf {
        production_pace_file_path()
    }

    /// The resolution rule of [`pace_file_path`], given the two test layers'
    /// current values (pure, so a test can pin "no layer installed → the
    /// production file" without racing another thread's baton).
    #[cfg(any(test, debug_assertions))]
    pub(crate) fn resolve_pace_file_path(
        explicit_pin: Option<PathBuf>,
        baton: Option<PathBuf>,
    ) -> PathBuf {
        explicit_pin.or(baton).unwrap_or_else(production_pace_file_path)
    }

    /// THE production pace/lock file: `<vct_root>/keyring.pace`, i.e.
    /// `~/.vct/keyring.pace`, or `$VCT_STATE_DIR/keyring.pace` when set. Every
    /// shipped VCO process (launcher, hub, CLI) paces on this one file.
    pub(crate) fn production_pace_file_path() -> PathBuf {
        crate::paths::vct_root_dir().join("keyring.pace")
    }

    /// One-shot warn de-dup for the UNUSABLE-pace-file degrade: we emit that
    /// warn at most once per process to avoid log spam on a persistently-
    /// unwritable root. `false` → not yet warned.
    static WARNED_ONCE_UNUSABLE: std::sync::atomic::AtomicBool =
        std::sync::atomic::AtomicBool::new(false);

    /// One-shot warn de-dup for the CONTENDED-lock degrade (the acquire
    /// deadline fired). Deliberately a SEPARATE latch from
    /// `WARNED_ONCE_UNUSABLE`: the two degrades have different causes and
    /// different urgency, and a single shared latch would let an early,
    /// benign, static "pace file uncreatable" warn permanently SWALLOW the
    /// report of a real cross-process wedge — silently conflating "could not
    /// acquire the lock" with "there was never a lock to acquire". Each cause
    /// gets its own one-shot.
    static WARNED_ONCE_CONTENDED: std::sync::atomic::AtomicBool =
        std::sync::atomic::AtomicBool::new(false);

    /// Test-visible count of warns actually emitted (so T20 can assert
    /// "exactly one warn" without scraping stderr). Incremented in lock-step
    /// with the real `tracing::warn!`, for BOTH latches.
    #[cfg(any(test, debug_assertions))]
    pub(crate) static WARN_COUNT: std::sync::atomic::AtomicUsize =
        std::sync::atomic::AtomicUsize::new(0);

    /// Emit `msg` at WARN exactly once per `latch`, counting it for tests.
    fn warn_once_latched(latch: &std::sync::atomic::AtomicBool, msg: &str) {
        if !latch.swap(true, std::sync::atomic::Ordering::SeqCst) {
            #[cfg(any(test, debug_assertions))]
            WARN_COUNT.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            tracing::warn!("[vct-secrets] {msg}");
        }
    }

    /// One-shot warn for "the pace file itself is unusable" (open/create
    /// failed, or flock returned a hard error).
    fn warn_once(msg: &str) {
        warn_once_latched(&WARNED_ONCE_UNUSABLE, msg);
    }

    /// One-shot warn for "another VCO process still holds the pace lock past
    /// the acquire deadline". Separate latch — see `WARNED_ONCE_CONTENDED`.
    fn warn_once_contended(msg: &str) {
        warn_once_latched(&WARNED_ONCE_CONTENDED, msg);
    }

    /// Test-only reset of BOTH one-shot warn latches (so consecutive test cases
    /// each get a fresh "have we warned yet" state).
    #[cfg(any(test, debug_assertions))]
    pub(crate) fn reset_warn_latch_for_test() {
        WARNED_ONCE_UNUSABLE.store(false, std::sync::atomic::Ordering::SeqCst);
        WARNED_ONCE_CONTENDED.store(false, std::sync::atomic::Ordering::SeqCst);
        WARN_COUNT.store(0, std::sync::atomic::Ordering::SeqCst);
    }

    /// Upper bound on how long [`acquire_and_space`] waits for the exclusive
    /// pace lock before degrading to in-process pacing.
    ///
    /// Chosen to sit BETWEEN the two timescales it has to separate:
    ///   * ABOVE legitimate contention. A healthy holder keeps the lock for one
    ///     `retry_with_backoff` chain — worst case 4 attempts × (150ms pacing +
    ///     a ≤2s D-Bus call) + 1.3s of backoff ≈ 10s, and a realistic
    ///     "Update all projects" burst queues a few seconds of short ops. 30s
    ///     leaves ~3× headroom, so ordinary contention still WAITS and pacing
    ///     is preserved — the G5 gnome-keyring SIGTRAP protection is not
    ///     traded away for the common case.
    ///   * BELOW human scale. The wedge this bounds is a holder parked on an
    ///     interactive unlock prompt (or a hung daemon), which lasts tens of
    ///     seconds to forever. 30s means a sibling process's keychain worker is
    ///     released long before a person finishes at a passphrase dialog.
    ///
    /// Deliberately SHORTER than `SHARED_MAX_PROMPT_TIMEOUT_SECS` (the holder's
    /// prompt bound, 120s): when a human really is typing, the right outcome is
    /// that the SIBLING degrades to unpaced — which costs nothing real, since a
    /// process sitting on a modal prompt is generating no daemon traffic to
    /// pace against — rather than that the human gets cut off.
    ///
    /// Note this is NOT the "no global install timeout" rule's territory: that
    /// rule forbids deadlines on WORK of unpredictable length. This bounds a
    /// WAIT FOR A LOCK, where the fallback (proceed unpaced) is a documented,
    /// correctness-preserving degrade.
    const PACE_LOCK_ACQUIRE_DEADLINE: std::time::Duration =
        std::time::Duration::from_secs(30);

    /// Poll interval while waiting for the contended pace lock. `LOCK_NB` never
    /// parks, so we sleep between attempts instead of busy-spinning. The
    /// uncontended path never sleeps (the first attempt succeeds), and a
    /// contended holder keeps the lock ≥ `MIN_CALL_SPACING` (150ms), so a 25ms
    /// granularity adds no measurable latency while costing ≤1200 wakeups
    /// across the full 30s deadline.
    const PACE_LOCK_ACQUIRE_POLL: std::time::Duration =
        std::time::Duration::from_millis(25);

    #[cfg(any(test, debug_assertions))]
    static TEST_PACE_DEADLINE: std::sync::Mutex<Option<std::time::Duration>> =
        std::sync::Mutex::new(None);

    #[cfg(any(test, debug_assertions))]
    fn current_pace_lock_deadline() -> std::time::Duration {
        TEST_PACE_DEADLINE
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .unwrap_or(PACE_LOCK_ACQUIRE_DEADLINE)
    }

    #[cfg(not(any(test, debug_assertions)))]
    fn current_pace_lock_deadline() -> std::time::Duration {
        PACE_LOCK_ACQUIRE_DEADLINE
    }

    /// Test-only RAII guard shortening the pace-lock acquire deadline so the
    /// contention tests run in milliseconds instead of 30s. Restores the
    /// previous value on drop.
    ///
    /// `#[cfg(test)]`, not `any(test, debug_assertions)`: the only callers are
    /// this file's tests, so a plain debug build would report it dead. The READ
    /// side (`TEST_PACE_DEADLINE` / `current_pace_lock_deadline`) stays
    /// `any(test, debug_assertions)` so a debug build still compiles — it just
    /// always observes `None`, i.e. exactly the production deadline.
    #[cfg(test)]
    pub(crate) struct TestPaceDeadlineGuard {
        prev: Option<std::time::Duration>,
    }

    #[cfg(test)]
    impl TestPaceDeadlineGuard {
        pub(crate) fn new(deadline: std::time::Duration) -> Self {
            let mut slot = TEST_PACE_DEADLINE.lock().unwrap_or_else(|p| p.into_inner());
            let prev = std::mem::replace(&mut *slot, Some(deadline));
            Self { prev }
        }
    }

    #[cfg(test)]
    impl Drop for TestPaceDeadlineGuard {
        fn drop(&mut self) {
            let mut slot = TEST_PACE_DEADLINE.lock().unwrap_or_else(|p| p.into_inner());
            *slot = self.prev.take();
        }
    }

    /// Outcome of a bounded exclusive-flock acquire. Three states, deliberately
    /// distinct: "I own it", "someone else still owns it" and "the lock itself
    /// is broken" are different facts and the caller warns differently for each.
    /// Collapsing `Contended` into `Failed` would report a healthy-but-busy
    /// sibling as a broken pace file.
    #[derive(Debug)]
    pub(crate) enum FlockAcquire {
        /// `fd` now holds LOCK_EX. Caller owns the unlock.
        Acquired,
        /// Still held by another open file description when the deadline
        /// expired. Nothing was acquired; there is nothing to unlock.
        Contended,
        /// `flock` returned a real error (EBADF / EINVAL / ENOLCK …). Carries
        /// the OS error for the warn message.
        Failed(std::io::Error),
    }

    /// Take LOCK_EX on `fd`, waiting at most `deadline`.
    ///
    /// SSOT for every exclusive pace-lock acquire in this module, so the bound
    /// lives in one place and the unit tests that drive this function cover
    /// the runtime path. (v0.2.102 W6-KC retired the second caller, the T-1
    /// test harness that held the production flock for a whole test.)
    ///
    /// Uses `LOCK_EX | LOCK_NB` in a poll loop rather than a blocking `LOCK_EX`
    /// precisely because a blocking acquire is uninterruptible from user space:
    /// once parked there is no deadline, no signal handling, and no way to give
    /// up — which is how one held lock turned into a permanent two-process
    /// wedge (see the module header). `EINTR` retries immediately (a signal is
    /// not contention) but still respects the same deadline.
    pub(crate) fn flock_exclusive_within(
        fd: std::os::unix::io::RawFd,
        deadline: std::time::Duration,
        poll: std::time::Duration,
    ) -> FlockAcquire {
        let start = std::time::Instant::now();
        loop {
            let rc = unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) };
            if rc == 0 {
                return FlockAcquire::Acquired;
            }
            let err = std::io::Error::last_os_error();
            let code = err.raw_os_error().unwrap_or(0);
            let interrupted = code == libc::EINTR;
            if !interrupted && code != libc::EWOULDBLOCK && code != libc::EAGAIN {
                // A genuine flock failure (bad fd, no lock support on this fs).
                return FlockAcquire::Failed(err);
            }
            // Held by someone else (or we were signalled). Either way we are
            // still waiting — and the DEADLINE APPLIES TO BOTH. An `EINTR`
            // fast-path that skipped this check would be an unbounded spin
            // under a signal storm: the very shape being removed here. (In
            // practice `LOCK_NB` has nothing to interrupt, but "shouldn't
            // happen in practice" is what the previous comment on this code
            // assumed, and it was wrong.)
            if start.elapsed() >= deadline {
                return FlockAcquire::Contended;
            }
            // Retry immediately after a signal (no contention was observed);
            // back off politely when the lock is genuinely held.
            if !interrupted {
                std::thread::sleep(poll);
            }
        }
    }

    #[cfg(any(test, debug_assertions))]
    static TEST_PACE_PATH: std::sync::Mutex<Option<PathBuf>> = std::sync::Mutex::new(None);

    #[cfg(any(test, debug_assertions))]
    fn test_pace_path_override() -> Option<PathBuf> {
        TEST_PACE_PATH.lock().unwrap_or_else(|p| p.into_inner()).clone()
    }

    /// Test-only: pin the pace file path (RAII via [`super::TestPacePathGuard`]).
    #[cfg(any(test, debug_assertions))]
    pub(crate) fn set_test_pace_path(p: Option<PathBuf>) -> Option<PathBuf> {
        let mut slot = TEST_PACE_PATH.lock().unwrap_or_else(|p| p.into_inner());
        std::mem::replace(&mut *slot, p)
    }

    // ── Keychain-test baton pace file (v0.2.102 W6-KC) ─────────────────────
    //
    // A keychain test (one holding `keychain_serialize_lock`) paces its keychain
    // ops on a TEST file, never on the production `keyring.pace`. The file is
    // per-host (the OS temp dir), so concurrent test binaries — parallel lanes,
    // two checkouts — still space their real daemon calls against EACH OTHER,
    // one op at a time, exactly the way the shipped processes pace against each
    // other on the production file.
    //
    // The redirect is per-guard, not per-binary: a test that runs a paced
    // keychain op WITHOUT holding the guard (or code compiled outside this
    // crate's `cfg(test)`, where the baton layer does not exist) still
    // resolves to the production file. Every keychain-touching test takes
    // the guard; `keychain_serialize_lock_never_holds_the_production_pace_lock`
    // pins the redirect for those that do.
    //
    // Why not the production file (what v0.2.83 T-1 did — it held the
    // production flock for the WHOLE test): the hub waits up to
    // `PACE_LOCK_ACQUIRE_DEADLINE` (30s) for that flock but answers its caller
    // after `KEYCHAIN_OP_TIMEOUT` (10s), so any test run stalled the user's
    // live hub and its clients saw a busy keychain (2026-10-13 incident:
    // gateway 503s). T-1's original reason — tests and a running launcher
    // writing the SAME keychain slot — is gone since the hermetic
    // `vct-test-<pid>` service namespace (2026-09-17), so tests and the user's
    // processes no longer share any keychain entry to serialize on.

    /// File name of the keychain-test baton's pace file, in the OS temp dir.
    #[cfg(any(test, debug_assertions))]
    pub(crate) const TEST_BATON_PACE_FILE_NAME: &str = "vct-keychain-test.pace";

    #[cfg(any(test, debug_assertions))]
    static TEST_BATON_PACE_PATH: std::sync::Mutex<Option<PathBuf>> = std::sync::Mutex::new(None);

    #[cfg(any(test, debug_assertions))]
    fn test_baton_pace_path() -> Option<PathBuf> {
        TEST_BATON_PACE_PATH.lock().unwrap_or_else(|p| p.into_inner()).clone()
    }

    /// Where the keychain-test baton paces: `<temp_dir>/vct-keychain-test.pace`.
    #[cfg(any(test, debug_assertions))]
    pub(crate) fn test_baton_pace_file_path() -> PathBuf {
        std::env::temp_dir().join(TEST_BATON_PACE_FILE_NAME)
    }

    /// RAII: while alive, this process's keychain ops pace on
    /// [`test_baton_pace_file_path`] instead of the production file. Held by
    /// `secrets::test_serialize::KeychainGuard`, installed only once that guard
    /// holds the in-process keychain mutex (so two waiting tests can never
    /// clear each other's slot). It takes NO lock itself: each keychain op
    /// still takes the (test) flock around that one op, as in production.
    #[cfg(any(test, debug_assertions))]
    pub(crate) struct TestBatonPaceGuard {
        prev: Option<PathBuf>,
    }

    #[cfg(any(test, debug_assertions))]
    impl TestBatonPaceGuard {
        pub(crate) fn install() -> Self {
            let mut slot = TEST_BATON_PACE_PATH.lock().unwrap_or_else(|p| p.into_inner());
            let prev = slot.replace(test_baton_pace_file_path());
            Self { prev }
        }
    }

    #[cfg(any(test, debug_assertions))]
    impl Drop for TestBatonPaceGuard {
        fn drop(&mut self) {
            let mut slot = TEST_BATON_PACE_PATH.lock().unwrap_or_else(|p| p.into_inner());
            *slot = self.prev.take();
        }
    }

    /// Test-only: read back the stored last-call timestamp (nanos) from the
    /// pace file, so T20 can assert two paced calls wrote monotonic stamps.
    #[cfg(any(test, debug_assertions))]
    pub(crate) fn read_pace_timestamp_for_test() -> Option<u128> {
        let path = pace_file_path();
        std::fs::read_to_string(path).ok()?.trim().parse::<u128>().ok()
    }

    /// RAII holder of the exclusive cross-process file lock. Dropping releases
    /// it (explicit LOCK_UN, kernel also releases on close/death). The `_file`
    /// handle is held only to keep `fd` valid for the guard's lifetime — the
    /// fd is a borrow of it (mirrors `test_serialize::file_lock::FileLock`).
    struct PaceLock {
        _file: std::fs::File,
        fd: std::os::unix::io::RawFd,
    }

    impl Drop for PaceLock {
        fn drop(&mut self) {
            unsafe {
                libc::flock(self.fd, libc::LOCK_UN);
            }
        }
    }

    impl PaceLock {
        /// Stamp the pace file with the CURRENT time (nanos since the UNIX
        /// epoch), overwriting the previous value. Called by
        /// `run_with_cross_process_pace` AFTER the keychain op completes, while
        /// the flock is STILL HELD, so the next process (which reads this stamp
        /// under its own flock acquisition) spaces off when WE FINISHED the op
        /// — a true daemon idle gap between one op's END and the next op's
        /// START (v0.2.84 D8.3). The pre-fix code stamped op-START inside
        /// `acquire_and_space`, so a slow op (≥ spacing) left ~0 gap.
        fn record_completion_timestamp(&mut self) {
            if let Ok(since_epoch) =
                std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
            {
                let _ = self._file.seek(SeekFrom::Start(0));
                let _ = self._file.set_len(0);
                let _ = write!(self._file, "{}", since_epoch.as_nanos());
                let _ = self._file.flush();
            }
        }
    }

    /// Acquire the exclusive lock and enforce cross-process spacing. Returns
    /// the held lock guard on success, or `None` when we could not take the
    /// lock — the pace file is uncreatable, `flock` failed outright, or another
    /// VCO process still held it at `PACE_LOCK_ACQUIRE_DEADLINE`. The caller
    /// then degrades to in-process pacing.
    ///
    /// `None` is a SUPPORTED degrade, not an error path: it costs cross-process
    /// PACING only. `run_with_cross_process_pace` runs the op identically
    /// either way, and the only other effect is that the completion stamp is
    /// skipped — correctly, since a process that does not hold the lock must
    /// not write the file another process is reading under it.
    ///
    /// `spacing` is threaded in (not read from the parent module directly) so
    /// the test override applies identically to the cross-process gate.
    fn acquire_and_space(spacing: std::time::Duration) -> Option<PaceLock> {
        let path = pace_file_path();
        // The pace file lives under vct_root; create the dir if absent so a
        // fresh install (root not yet mkdir'd) still paces rather than warning.
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let mut file = match std::fs::OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .truncate(false)
            .open(&path)
        {
            Ok(f) => f,
            Err(e) => {
                warn_once(&format!(
                    "cannot open keyring pace file {path:?}: {e} — cross-process \
                     keyring pacing disabled (in-process pacing still active)"
                ));
                return None;
            }
        };
        let fd = file.as_raw_fd();
        // BOUNDED exclusive acquire (P0). What this guarantees: we return
        // within `deadline` no matter what the current holder is doing — the
        // calling keychain worker thread is therefore always released, so one
        // process parked on an unlock prompt can never wedge another. What it
        // does NOT guarantee: that we got the lock. On expiry we degrade to
        // in-process pacing, which is safe ONLY because the guard is inert
        // data (see the module header's "what is guaranteed NOW" note) — a
        // future change that makes `op` depend on holding it invalidates this.
        //
        // A blocking `LOCK_EX` here was the wedge: it cannot be deadlined from
        // user space, and the comment that justified it ("bounded transitively
        // by the caller's timeout worker") confused the CALLER's 10s wait for
        // a result with a bound on the OP — `run_keychain_with_timeout` gives
        // up on the channel but never cancels the job, so the holder kept this
        // lock for as long as its prompt stayed open.
        let deadline = current_pace_lock_deadline();
        match flock_exclusive_within(fd, deadline, PACE_LOCK_ACQUIRE_POLL) {
            FlockAcquire::Acquired => {}
            FlockAcquire::Contended => {
                // NOT "nothing to pace" — we know a sibling VCO process holds
                // the lock and we chose to give up waiting. Say exactly that,
                // on its own one-shot latch so this can never be swallowed by
                // an earlier unrelated degrade warn.
                warn_once_contended(&format!(
                    "another VCO process has held the keyring pace lock \
                     {path:?} for more than {}s (typically a keychain unlock \
                     prompt awaiting the user, or a wedged Secret Service) — \
                     proceeding WITHOUT cross-process pacing for this \
                     operation so this process is not blocked (in-process \
                     pacing still active). This warns once per process.",
                    deadline.as_secs_f32()
                ));
                return None;
            }
            FlockAcquire::Failed(err) => {
                warn_once(&format!(
                    "flock(LOCK_EX) failed on {path:?}: {err} — cross-process \
                     keyring pacing disabled (in-process pacing still active)"
                ));
                return None;
            }
        }
        // We do all I/O on the ORIGINAL `file` (whose fd `fd` we locked), then
        // MOVE it into the guard so the very fd holding the flock stays open
        // for the guard's whole lifetime and `Drop` LOCK_UNs that same fd.
        // (A dup'd clone would share the lock but leave `Drop` unlocking a
        // closed fd — a silent no-op — so we deliberately don't clone.)
        //
        // Read the stored last-call timestamp (nanos since the UNIX epoch, a
        // process-shared reference) and sleep the remaining spacing.
        let now = std::time::SystemTime::now();
        let mut buf = String::new();
        let _ = file.seek(SeekFrom::Start(0));
        if file.read_to_string(&mut buf).is_ok() {
            if let Ok(prev_nanos) = buf.trim().parse::<u128>() {
                if let Ok(now_since_epoch) = now.duration_since(std::time::UNIX_EPOCH) {
                    let now_nanos = now_since_epoch.as_nanos();
                    if now_nanos > prev_nanos {
                        let elapsed = std::time::Duration::from_nanos(
                            (now_nanos - prev_nanos).min(u64::MAX as u128) as u64,
                        );
                        if elapsed < spacing {
                            std::thread::sleep(spacing - elapsed);
                        }
                    }
                    // If now <= prev (clock skew / same instant), fall through:
                    // the completion stamp written after the op (below) still
                    // records a fresh timestamp, spacing best-effort.
                }
            }
        }
        // v0.2.84 (D8.3): we DELIBERATELY do NOT write a timestamp here. The
        // pre-fix code stamped op-START at this point, which — for an op that
        // itself took ≥ MIN_CALL_SPACING — left ~0 idle gap between one op's
        // END and the next op's START. The stamp is now written by
        // `run_with_cross_process_pace` AFTER the op completes (op-END), while
        // this same flock is still held, via `PaceLock::record_completion_timestamp`.
        Some(PaceLock { _file: file, fd })
    }

    /// Run `op` under the cross-process pace/lock. The lock is HELD for the
    /// whole `op` so no sibling VCO process interleaves a concurrent daemon
    /// request. When the lock could not be taken — file unusable, flock error,
    /// or a sibling still holding it at the acquire deadline — `op` still runs
    /// (degraded to in-process pacing): a soft-fail, never a hard block on the
    /// user's secret read, and never a skipped or repeated op. The op runs
    /// EXACTLY ONCE and its value is returned verbatim on both paths; the only
    /// difference is that the degraded path writes no completion stamp,
    /// because we do not own the file.
    pub(super) fn run_with_cross_process_pace<T>(op: impl FnOnce() -> T) -> T {
        let spacing = super::current_spacing();
        // Hold the guard across `op`; drop (unlock) after.
        let mut guard = acquire_and_space(spacing);
        let result = op();
        // v0.2.84 (D8.3): stamp the pace file with the op's COMPLETION time
        // WHILE the flock is still held, so the next process spaces off when we
        // FINISHED (true daemon idle gap), not when we started. On the degraded
        // path (`acquire_and_space` returned `None` — unusable pace file,
        // flock error, or a sibling still holding it at the deadline) there is
        // no stamp to write; in-process pacing carries the gap (unchanged
        // soft-fail behaviour).
        if let Some(g) = guard.as_mut() {
            g.record_completion_timestamp();
        }
        // `guard` drops here → flock(LOCK_UN), after the completion stamp.
        result
    }
}

/// Cross-process pacing shim. On unix, serializes the keychain op across ALL
/// VCO processes via a held flock (see `cross_process_pace`). On non-unix it's
/// a pass-through (those platforms lack the crashy daemon). Called from inside
/// the bounded-timeout worker, wrapping the full `retry_with_backoff` op.
///
/// CORRECTION (P0): this used to claim "so a wedged flock or op is still
/// bounded by `KEYCHAIN_OP_TIMEOUT`". It is not. `KEYCHAIN_OP_TIMEOUT` bounds
/// how long the CALLER waits on the result channel; `run_keychain_with_timeout`
/// abandons the result but never cancels the job, so a wedged flock or op keeps
/// running on the worker thread — which is exactly how a single held lock
/// wedged two processes indefinitely. What actually bounds the worker is the
/// acquire deadline inside `cross_process_pace` (30s) plus, on Linux, the
/// Secret-Service prompt bound (`SHARED_MAX_PROMPT_TIMEOUT_SECS`, 120s). The
/// caller-side timeout only bounds the caller. (v0.2.102: a job that does
/// overrun no longer blocks the PROCESS — the next submission starts a fresh
/// worker; see `submit_keychain_job`.)
#[inline]
pub(crate) fn with_cross_process_pace<T>(op: impl FnOnce() -> T) -> T {
    #[cfg(unix)]
    {
        cross_process_pace::run_with_cross_process_pace(op)
    }
    #[cfg(not(unix))]
    {
        op()
    }
}

// ─── Bounded keychain worker (v0.2.76 A4; busy-vs-stuck + recycle v0.2.102) ────
//
// The pacing layer above and `secrets::retry_with_backoff` serialize and retry
// keychain calls, but the
// underlying op (`KeychainEntry::new` D-Bus negotiation, set / get / delete)
// can block for a long time when the OS Secret Service is slow or wedged
// (observed live: gnome-keyring hanging a call for 13+ minutes at ~0% CPU).
// Every keychain op therefore runs on a dedicated worker thread fed by a
// channel, and the caller waits on a result channel with a deadline.
//
// v0.2.102 (hub keychain "wedge", 2026-10-07):
//
//   * BUSY IS NOT STUCK. The pre-v0.2.102 admission check failed every caller
//     FAST with "keychain worker stuck" whenever the worker was running ANY
//     job. A healthy job holds the worker for ~150ms (the pacing gap), so two
//     overlapping hub `/env` requests made one of them fail every key in ~1ms
//     each — 17k+ "stuck" lines a day against a healthy Secret Service. Now a
//     caller arriving while the worker is busy simply QUEUES and waits for its
//     result, up to `KEYCHAIN_OP_TIMEOUT`.
//   * A GENUINELY HUNG OP NO LONGER POISONS THE PROCESS. A job running longer
//     than its stuck threshold (`KEYCHAIN_OP_TIMEOUT` for Background ops,
//     `KEYCHAIN_INTERACTIVE_STUCK_AFTER` for ops that may sit on a user unlock
//     prompt) makes the next submission abandon that worker — it exits once its
//     op returns and its queue drains — and start a fresh one. At most
//     `ABANDONED_WORKER_CAP` abandoned workers may be outstanding; while the
//     cap is full (and nothing is old enough to retire), a further hang is
//     reported as `WorkerStuck` instead of spawning yet another thread.
//     v0.2.102 R2: an abandoned worker whose op outlives
//     `VCT_KEYCHAIN_WORKER_RETIRE_AFTER_SECS` (default 600s) is retired from
//     that accounting, so a PERMANENT hang no longer answers `WorkerStuck`
//     forever. A retired worker whose op never returns stays parked on its
//     thread — under a permanently wedged backend that is at most ~1 parked
//     thread per retire interval, and it is accounted out of the cap. The
//     replacement never
//     waits on the persistent Secret-Service connection the hung op holds
//     (`secrets_ss_connection::with_connection` falls back to a private
//     session when the shared one stays held).
//   * The timeout wraps OUTSIDE pacing/backoff (v0.2.72 P9 semantics
//     unchanged) — the job closure IS the full retry_with_backoff call.
//   * Every failure's `Display` is one of the deterministic strings
//     [`is_keychain_unavailable_detail`] recognises, so consumers classify it
//     as keychain UNAVAILABILITY (retry later) — never as a missing key.

/// Per-op keychain timeout: how long a caller waits for its result (queue wait
/// included), and the stuck threshold for a Background op. A healthy Secret
/// Service answers in milliseconds and the pacing + backoff worst case is
/// ~1.45s, so 10s leaves ample headroom.
pub(crate) const KEYCHAIN_OP_TIMEOUT: std::time::Duration =
    std::time::Duration::from_secs(10);

/// Stuck threshold for an op that may legitimately wait on a human at an
/// unlock prompt (Interactive set/get/delete, the canary probe). Exceeds the
/// shared connection's 120s prompt bound so a person typing a passphrase is
/// never treated as a hang (pinned by
/// `interactive_stuck_threshold_outlasts_prompt_bound` — that pin test lives
/// in `secrets.rs`, beside the lock-probe tests it complements, because it
/// reads `secrets_ss_connection::SHARED_MAX_PROMPT_TIMEOUT_SECS` and the K-3
/// guard confines that module path to `secrets.rs` / `lib.rs`).
pub(crate) const KEYCHAIN_INTERACTIVE_STUCK_AFTER: std::time::Duration =
    std::time::Duration::from_secs(150);

/// Error returned when a keychain op could not complete. Every variant means
/// "the keychain is temporarily UNAVAILABLE", never "the key is absent".
#[derive(Debug)]
pub enum KeychainTimeout {
    /// No result within `KEYCHAIN_OP_TIMEOUT` (queue wait included).
    TimedOut,
    /// The running op is past its stuck threshold and the abandoned-worker
    /// cap is reached (nothing old enough to retire), so no replacement can
    /// be started.
    WorkerStuck,
    /// The worker thread/channel is unavailable (spawn failure, or the job was
    /// dropped). Treated conservatively as a failure.
    WorkerUnavailable,
}

impl std::fmt::Display for KeychainTimeout {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            KeychainTimeout::TimedOut => write!(
                f,
                "keychain operation timed out — Secret Service unresponsive \
                 (exceeded {}s)",
                current_keychain_timeout().as_secs()
            ),
            KeychainTimeout::WorkerStuck => write!(
                f,
                "keychain worker stuck — prior keychain operations are still \
                 blocked on an unresponsive Secret Service; try again once it \
                 recovers"
            ),
            KeychainTimeout::WorkerUnavailable => {
                write!(f, "keychain worker unavailable")
            }
        }
    }
}

/// THE one predicate recognising a keychain-UNAVAILABILITY error detail — the
/// deterministic `Display` strings of [`KeychainTimeout`], which `set_raw` /
/// `get_with_context` / `delete_with_context` embed verbatim in their error
/// details. No other error source produces these substrings, so a match is
/// positive identification. Consumers: the hub's degraded classification and
/// log level, `db::secret_active`, and (via `for_tests`) every
/// keychain-touching test that skips on a slow daemon.
pub fn is_keychain_unavailable_detail(e: &str) -> bool {
    e.contains("keychain operation timed out")
        || e.contains("keychain worker stuck")
        || e.contains("keychain worker unavailable")
}

type KeychainJob = Box<dyn FnOnce() + Send + 'static>;

struct QueuedJob {
    run: KeychainJob,
    stuck_after: std::time::Duration,
}

type RunningSlot = std::sync::Arc<std::sync::Mutex<Option<(std::time::Instant, std::time::Duration)>>>;

/// The live worker. `running` is `Some((start, stuck_after))` while a job
/// executes; `abandoned` is set when a submission gives up on it.
struct WorkerHandle {
    tx: std::sync::mpsc::Sender<QueuedJob>,
    running: RunningSlot,
    abandoned: std::sync::Arc<std::sync::atomic::AtomicBool>,
    /// Shared with the thread: set to the hung op's start instant when this
    /// worker is abandoned; cleared by the thread on exit (R2 accounting).
    hung_since: AbandonedSlot,
}

static KEYCHAIN_WORKER: std::sync::Mutex<Option<WorkerHandle>> = std::sync::Mutex::new(None);

fn spawn_keychain_worker() -> Option<WorkerHandle> {
    use std::sync::atomic::Ordering;
    let (tx, rx) = std::sync::mpsc::channel::<QueuedJob>();
    let running: RunningSlot = std::sync::Arc::new(std::sync::Mutex::new(None));
    let abandoned = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let hung_since: AbandonedSlot = std::sync::Arc::new(std::sync::Mutex::new(None));
    let w_running = running.clone();
    let w_abandoned = abandoned.clone();
    let w_hung_since = hung_since.clone();
    let spawned = std::thread::Builder::new()
        .name("vct-keychain-worker".into())
        .spawn(move || {
            // Runs until its sender is gone AND the queue is drained: the live
            // worker's sender lives in KEYCHAIN_WORKER; an abandoned worker's
            // sender was dropped at abandonment, so it finishes its (hung) op,
            // runs what was already queued behind it, then exits.
            while let Ok(job) = rx.recv() {
                *w_running.lock().unwrap_or_else(|p| p.into_inner()) =
                    Some((std::time::Instant::now(), job.stuck_after));
                // A panicking op must not kill the worker with `running` left
                // set (that would read as a hang); its caller sees the dropped
                // result channel as `WorkerUnavailable`.
                let run = job.run;
                let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(run));
                *w_running.lock().unwrap_or_else(|p| p.into_inner()) = None;
            }
            if w_abandoned.load(Ordering::SeqCst) {
                // R2: close this worker's accounting slot, then purge every
                // closed slot (only an exiting thread closes its own).
                *w_hung_since.lock().unwrap_or_else(|p| p.into_inner()) = None;
                ABANDONED_WORKERS
                    .lock()
                    .unwrap_or_else(|p| p.into_inner())
                    .retain(|s| s.lock().unwrap_or_else(|p| p.into_inner()).is_some());
                tracing::info!("[vct-secrets] the abandoned keychain worker's op returned; it exited");
            }
        });
    match spawned {
        Ok(_) => Some(WorkerHandle {
            tx,
            running,
            abandoned,
            hung_since,
        }),
        Err(e) => {
            tracing::warn!(error = %e, "[vct-secrets] could not spawn the keychain worker thread");
            None
        }
    }
}

/// Hand `job` to the worker: replace a hung worker first if needed.
fn submit_keychain_job(job: QueuedJob) -> Result<(), KeychainTimeout> {
    use std::sync::atomic::Ordering;
    let mut slot = KEYCHAIN_WORKER.lock().unwrap_or_else(|p| p.into_inner());
    let mut hung: Option<(std::time::Instant, std::time::Duration)> = None;
    if let Some(h) = slot.as_ref() {
        let running = h.running.lock().unwrap_or_else(|p| p.into_inner());
        if let Some((start, stuck_after)) = *running {
            let age = start.elapsed();
            if age >= stuck_after {
                hung = Some((start, age));
            }
        }
    }
    if let Some((start, age)) = hung {
        // R2: first write off abandoned workers whose hung op has outlived
        // the retirement limit, so ONE permanent hang cannot answer
        // `WorkerStuck` forever.
        retire_stale_abandoned_workers();
        if ABANDONED_WORKERS
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .len()
            >= ABANDONED_WORKER_CAP
        {
            // The abandoned-worker cap is reached and nothing is old enough
            // to retire: the backend is unresponsive. Do not spawn another
            // thread — answer the honest, retryable `WorkerStuck`.
            return Err(KeychainTimeout::WorkerStuck);
        }
        if let Some(old) = slot.take() {
            old.abandoned.store(true, Ordering::SeqCst);
            *old.hung_since.lock().unwrap_or_else(|p| p.into_inner()) = Some(start);
            ABANDONED_WORKERS
                .lock()
                .unwrap_or_else(|p| p.into_inner())
                .push(old.hung_since.clone());
            tracing::warn!(
                op_age_secs = age.as_secs(),
                "[vct-secrets] keychain op hung past its stuck threshold — starting a \
                 fresh keychain worker (the hung op finishes on its own thread; it is \
                 written off after {}s if it never returns)",
                current_worker_retire_after().as_secs()
            );
            // `old` drops here → its sender closes, so it exits once drained.
        }
    }
    if slot.is_none() {
        *slot = spawn_keychain_worker();
    }
    let Some(handle) = slot.as_ref() else {
        return Err(KeychainTimeout::WorkerUnavailable);
    };
    if handle.tx.send(job).is_err() {
        // The worker thread is gone (should not happen — it only exits when its
        // sender is dropped). Forget it so the next op respawns.
        *slot = None;
        return Err(KeychainTimeout::WorkerUnavailable);
    }
    Ok(())
}

/// Run a Background keychain op on the shared worker. See
/// [`run_keychain_with_timeout_ctx`].
pub(crate) fn run_keychain_with_timeout<T, F>(f: F) -> Result<T, KeychainTimeout>
where
    T: Send + 'static,
    F: FnOnce() -> T + Send + 'static,
{
    run_keychain_with_timeout_ctx(CallContext::Background, f)
}

/// Run a keychain op on the shared worker with a bounded wait.
///
/// `f` is the full op INCLUDING pacing/backoff — the timeout wraps outside
/// them (A4d). A busy worker means "wait in line", never an error. `ctx` only
/// selects the stuck threshold used to decide whether a still-running op is
/// hung (an Interactive op may sit on a user prompt). Returns `Ok(f())` on
/// completion within `KEYCHAIN_OP_TIMEOUT`; `Err(TimedOut)` otherwise (the op
/// is not cancelled — it completes on the worker and its result is dropped);
/// `Err(WorkerStuck)` when the backend is unresponsive past one replacement.
pub(crate) fn run_keychain_with_timeout_ctx<T, F>(
    ctx: CallContext,
    f: F,
) -> Result<T, KeychainTimeout>
where
    T: Send + 'static,
    F: FnOnce() -> T + Send + 'static,
{
    let op_timeout = current_keychain_timeout();
    let stuck_after = match ctx {
        CallContext::Background => op_timeout,
        CallContext::Interactive => KEYCHAIN_INTERACTIVE_STUCK_AFTER.max(op_timeout),
    };
    let (result_tx, result_rx) = std::sync::mpsc::sync_channel::<T>(1);
    submit_keychain_job(QueuedJob {
        run: Box::new(move || {
            // Ignore send error: a caller that already gave up dropped the
            // receiver, and the result is simply discarded.
            let _ = result_tx.send(f());
        }),
        stuck_after,
    })?;
    match result_rx.recv_timeout(op_timeout) {
        Ok(value) => Ok(value),
        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => Err(KeychainTimeout::TimedOut),
        Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
            Err(KeychainTimeout::WorkerUnavailable)
        }
    }
}


#[cfg(any(test, debug_assertions))]
static TEST_KEYCHAIN_TIMEOUT_OVERRIDE: std::sync::Mutex<Option<std::time::Duration>> =
    std::sync::Mutex::new(None);

#[cfg(any(test, debug_assertions))]
fn current_keychain_timeout() -> std::time::Duration {
    TEST_KEYCHAIN_TIMEOUT_OVERRIDE
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .unwrap_or(KEYCHAIN_OP_TIMEOUT)
}

#[cfg(not(any(test, debug_assertions)))]
fn current_keychain_timeout() -> std::time::Duration {
    KEYCHAIN_OP_TIMEOUT
}

/// Test-only RAII guard shortening the keychain timeout so timeout tests run
/// fast. Restores the previous value on drop.
#[cfg(any(test, debug_assertions))]
pub struct TestKeychainTimeoutGuard {
    prev: Option<std::time::Duration>,
}

#[cfg(any(test, debug_assertions))]
impl TestKeychainTimeoutGuard {
    pub fn new(timeout: std::time::Duration) -> Self {
        let mut slot = TEST_KEYCHAIN_TIMEOUT_OVERRIDE
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        let prev = *slot;
        *slot = Some(timeout);
        Self { prev }
    }
}

#[cfg(any(test, debug_assertions))]
impl Drop for TestKeychainTimeoutGuard {
    fn drop(&mut self) {
        let mut slot = TEST_KEYCHAIN_TIMEOUT_OVERRIDE
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        *slot = self.prev;
    }
}


// ─── Abandoned-worker retirement (v0.2.102 R2, owner 2026-10-07) ─────────────
//
// Pre-R2, ONE outstanding abandoned worker made every later hang answer
// `WorkerStuck` FOREVER (R2 of the v0.2.102 review): if the hung op never
// returned, the process stayed unavailable until restart. Now an abandoned
// worker whose hung op has outlived `current_worker_retire_after()` is
// RETIRED — written off the outstanding list (one WARNING per retirement) —
// so the next submission may start a fresh worker. The cap
// (`ABANDONED_WORKER_CAP`) bounds concurrently abandoned threads; at the cap,
// with nothing old enough to retire, the honest `WorkerStuck` 503 is kept.
// Retirement is accounting only: the retired thread is never killed, and its
// late result lands in its own (already dropped) result channel, never in a
// different job (pinned by `a_late_result_from_a_retired_worker_is_discarded`).

/// Cap on concurrently abandoned (hung) worker threads. Deliberately tiny:
/// each abandoned thread is a leaked-until-it-returns resource; past the cap
/// the honest `WorkerStuck` 503 is the answer, not another thread.
const ABANDONED_WORKER_CAP: usize = 2;

/// Default retirement limit for an abandoned worker's hung op: 10 minutes.
/// Must EXCEED every INNER bound so a legitimately slow op is never written
/// off: the worst legitimate chain is a `retry_with_backoff` interactive op —
/// 4 attempts x the 120 s Secret-Service prompt bound + 1.3 s backoff + 0.6 s
/// pacing ≈ 482 s — plus queue wait; 600 s clears it and every smaller bound
/// (the 150 s Interactive stuck threshold, the 30 s pace-lock acquire
/// deadline). Env-tunable: `VCT_KEYCHAIN_WORKER_RETIRE_AFTER_SECS`.
const WORKER_RETIRE_AFTER_DEFAULT_SECS: u64 = 600;

#[cfg(any(test, debug_assertions))]
static TEST_WORKER_RETIRE_AFTER_OVERRIDE: std::sync::Mutex<Option<std::time::Duration>> =
    std::sync::Mutex::new(None);

fn production_worker_retire_after() -> std::time::Duration {
    static LIMIT: std::sync::OnceLock<std::time::Duration> = std::sync::OnceLock::new();
    *LIMIT.get_or_init(|| {
        match std::env::var("VCT_KEYCHAIN_WORKER_RETIRE_AFTER_SECS") {
            Ok(raw) => match raw.trim().parse::<u64>() {
                Ok(secs) => std::time::Duration::from_secs(secs.max(1)),
                Err(_) => {
                    tracing::debug!(
                        value = %raw,
                        "VCT_KEYCHAIN_WORKER_RETIRE_AFTER_SECS is not a whole number of \
                         seconds — using the 600s default"
                    );
                    std::time::Duration::from_secs(WORKER_RETIRE_AFTER_DEFAULT_SECS)
                }
            },
            Err(_) => std::time::Duration::from_secs(WORKER_RETIRE_AFTER_DEFAULT_SECS),
        }
    })
}

#[cfg(any(test, debug_assertions))]
fn current_worker_retire_after() -> std::time::Duration {
    TEST_WORKER_RETIRE_AFTER_OVERRIDE
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .unwrap_or_else(production_worker_retire_after)
}

#[cfg(not(any(test, debug_assertions)))]
fn current_worker_retire_after() -> std::time::Duration {
    production_worker_retire_after()
}

/// One slot per abandoned worker that is still outstanding: `Some(start)`
/// carries the instant its hung op began. Shared with the worker thread,
/// which clears its own slot to `None` when the op finally returns; the
/// thread's exit hook then purges `None` slots from [`ABANDONED_WORKERS`].
type AbandonedSlot = std::sync::Arc<std::sync::Mutex<Option<std::time::Instant>>>;

/// The outstanding abandoned workers. Lock order: a holder of
/// `KEYCHAIN_WORKER` may take this lock (submission path); nothing takes them
/// in the reverse order (the worker-exit hook takes only this one).
static ABANDONED_WORKERS: std::sync::Mutex<Vec<AbandonedSlot>> = std::sync::Mutex::new(Vec::new());

/// Write off abandoned workers whose hung op has outlived the retirement
/// limit. One WARNING per retirement — the operator's signal that the backend
/// wedged long enough to be abandoned AND stayed wedged past the limit.
fn retire_stale_abandoned_workers() {
    let limit = current_worker_retire_after();
    let mut workers = ABANDONED_WORKERS.lock().unwrap_or_else(|p| p.into_inner());
    let now = std::time::Instant::now();
    workers.retain(|slot| {
        let hung_since = slot.lock().unwrap_or_else(|p| p.into_inner());
        match *hung_since {
            Some(start) => {
                let age = now.duration_since(start);
                if age > limit {
                    tracing::warn!(
                        op_age_secs = age.as_secs(),
                        retire_after_secs = limit.as_secs(),
                        "[vct-secrets] abandoned keychain worker's op has outlived the \
                         retirement limit — writing it off (the thread is left to finish \
                         on its own; its late result is discarded)"
                    );
                    false
                } else {
                    true
                }
            }
            // `None`: the thread is exiting right now and its exit hook (or a
            // concurrent retain) removes the slot; keep it harmless here.
            None => false,
        }
    });
}

/// Test/diagnostic: is any abandoned (hung) keychain worker still outstanding?
/// `#[cfg(any(test, debug_assertions))]` (its original gate): the only callers
/// are tests and the `secrets` re-export, both under the same gate — a release
/// build compiles neither.
#[cfg(any(test, debug_assertions))]
pub fn abandoned_keychain_worker_outstanding() -> bool {
    !ABANDONED_WORKERS
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .is_empty()
}


/// `#[cfg(test)]`, not `any(test, debug_assertions)` (the TestPaceDeadlineGuard
/// pattern): the only callers are this file's tests, so a plain debug build
/// would report it dead. The READ side (`TEST_WORKER_RETIRE_AFTER_OVERRIDE` /
/// `current_worker_retire_after`) stays `any(test, debug_assertions)` so a
/// debug build still compiles — it just always observes `None`, i.e. exactly
/// the production limit.
#[cfg(test)]
pub(crate) struct TestWorkerRetireAfterGuard {
    prev: Option<std::time::Duration>,
}

#[cfg(test)]
impl TestWorkerRetireAfterGuard {
    pub fn new(limit: std::time::Duration) -> Self {
        let mut slot = TEST_WORKER_RETIRE_AFTER_OVERRIDE
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        let prev = *slot;
        *slot = Some(limit);
        Self { prev }
    }
}

#[cfg(test)]
impl Drop for TestWorkerRetireAfterGuard {
    fn drop(&mut self) {
        let mut slot = TEST_WORKER_RETIRE_AFTER_OVERRIDE
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        *slot = self.prev;
    }
}


// `pub(crate)`: `secrets.rs`'s staying tests (the canary-probe timeout leg
// and the busy-worker independence leg) reuse `parked_op` /
// `wait_worker_recovered` from here — one home, no duplicated helpers.
#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::secrets::retry_with_backoff;
    use crate::secrets::test_serialize;

    /// `paced_call`: enforces the spacing between consecutive calls. We
    /// use a 50ms test override and run two calls back-to-back; the
    /// second must wait ≥40ms (giving a 10ms scheduler-jitter margin).
    #[test]
    fn paced_call_enforces_min_spacing() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _g = TestSpacingGuard::new(std::time::Duration::from_millis(50));
        // Burn the first slot — establishes LAST_KEYRING_CALL.
        paced_call(|| 0);
        let start = std::time::Instant::now();
        paced_call(|| 0);
        let elapsed = start.elapsed();
        assert!(
            elapsed >= std::time::Duration::from_millis(40),
            "expected ≥40ms gap, got {:?}",
            elapsed
        );
    }

    /// `paced_call`: a single call after a quiescent gap pays no
    /// spacing cost — only consecutive bursts trigger the pacing. This
    /// is the dominant production case (user clicks a button; one secret
    /// read happens; no pacing needed). Uses a small override + small
    /// sleep so the test doesn't pollute the scheduler for timing-
    /// sensitive sibling tests (kg_sync subprocess watchdogs).
    #[test]
    fn paced_call_skips_spacing_when_idle() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _g = TestSpacingGuard::new(std::time::Duration::from_millis(20));
        // Establish a baseline, then wait past the spacing window.
        paced_call(|| 0);
        std::thread::sleep(std::time::Duration::from_millis(30));
        let start = std::time::Instant::now();
        paced_call(|| 0);
        let elapsed = start.elapsed();
        assert!(
            elapsed < std::time::Duration::from_millis(10),
            "post-idle call must not pay spacing cost; got {:?}",
            elapsed
        );
    }

    /// Production spacing must be ≥150ms. The constant is the
    /// load-bearing one — `paced_call` reads it via `current_spacing()`
    /// outside `#[cfg(test)]`. Pinned so a future "speed up production"
    /// patch has to come back and re-evaluate the daemon-load
    /// trade-off.
    #[test]
    fn min_call_spacing_is_at_least_150ms() {
        assert!(MIN_CALL_SPACING >= std::time::Duration::from_millis(150));
    }

    /// Normal op passes through unchanged: a fast closure completes and its
    /// value is returned (leave-alone case).
    #[test]
    fn keychain_timeout_passes_through_fast_op() {
        let _lock = test_serialize::keychain_serialize_lock();
        let out = run_keychain_with_timeout(|| 40 + 2);
        assert!(matches!(out, Ok(42)), "fast op must pass through: {:?}", out);
    }

    /// Blocking test op: parks until `release` fires (models a hung Secret
    /// Service call) and returns `v`.
    pub(crate) fn parked_op(
        release: std::sync::mpsc::Receiver<()>,
        v: i32,
    ) -> impl FnOnce() -> i32 + Send + 'static {
        let release = std::sync::Mutex::new(release);
        move || {
            let _ = release.lock().unwrap().recv();
            v
        }
    }

    /// Poll (bounded) until no abandoned worker is outstanding and a fresh op
    /// passes — leaves the global worker clean for the next test.
    pub(crate) fn wait_worker_recovered() {
        for _ in 0..400 {
            if !abandoned_keychain_worker_outstanding()
                && run_keychain_with_timeout(|| ()).is_ok()
            {
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        panic!("keychain worker did not recover");
    }

    /// v0.2.102 T1 — BUSY IS NOT STUCK (red-first: the pre-v0.2.102 admission
    /// failed every caller that arrived while ANY job ran with `WorkerStuck`).
    /// Eight concurrent callers, each op holding the worker ~30ms (the shape of
    /// a paced healthy read): every one must succeed, in line.
    #[test]
    fn concurrent_healthy_callers_all_succeed() {
        let _lock = test_serialize::keychain_serialize_lock();
        let handles: Vec<_> = (0..8)
            .map(|i| {
                std::thread::spawn(move || {
                    run_keychain_with_timeout(move || {
                        std::thread::sleep(std::time::Duration::from_millis(30));
                        i
                    })
                })
            })
            .collect();
        for (i, h) in handles.into_iter().enumerate() {
            let out = h.join().expect("caller thread");
            assert!(
                matches!(out, Ok(v) if v == i),
                "concurrent caller {i} must wait its turn and succeed, got {out:?}"
            );
        }
    }

    /// v0.2.102 T2 — a GENUINELY hung op does not poison the process
    /// (red-first: pre-v0.2.102 the next op failed `WorkerStuck` until the
    /// hung op returned — possibly never). The hung op times out; the next op
    /// starts a fresh worker and succeeds while the hung one is still parked.
    #[test]
    fn hung_op_starts_fresh_worker_and_next_op_succeeds() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _tg = TestKeychainTimeoutGuard::new(std::time::Duration::from_millis(100));
        let (release_tx, release_rx) = std::sync::mpsc::channel::<()>();

        let blocked = run_keychain_with_timeout(parked_op(release_rx, 7));
        assert!(
            matches!(blocked, Err(KeychainTimeout::TimedOut)),
            "the hung op must time out: {blocked:?}"
        );
        // The caller's 100ms wait starts at submission, a hair before the job
        // starts running; let the job's own age cross the threshold too.
        std::thread::sleep(std::time::Duration::from_millis(30));
        let next = run_keychain_with_timeout(|| 99);
        assert!(
            matches!(next, Ok(99)),
            "the next op must run on a FRESH worker, not inherit the hang: {next:?}"
        );
        assert!(
            abandoned_keychain_worker_outstanding(),
            "the hung worker is abandoned (still parked), not reused"
        );

        release_tx.send(()).unwrap();
        wait_worker_recovered();
    }

    /// v0.2.102 T3 + R2 — bounded: abandoned workers are capped at
    /// `ABANDONED_WORKER_CAP` (2). With the default retirement limit (far
    /// beyond this test's timeline), the FIRST two hangs are each abandoned
    /// and served by fresh workers; once the cap is full, a further hang is
    /// reported as `WorkerStuck` instead of spawning thread after thread.
    /// Releasing everything lets the process recover fully.
    #[test]
    fn hangs_past_the_abandoned_worker_cap_answer_stuck_without_new_threads() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _tg = TestKeychainTimeoutGuard::new(std::time::Duration::from_millis(100));
        let (tx1, rx1) = std::sync::mpsc::channel::<()>();
        let (tx2, rx2) = std::sync::mpsc::channel::<()>();
        let (tx3, rx3) = std::sync::mpsc::channel::<()>();

        assert!(matches!(
            run_keychain_with_timeout(parked_op(rx1, 1)),
            Err(KeychainTimeout::TimedOut)
        ));
        std::thread::sleep(std::time::Duration::from_millis(150));
        // Starts worker #2 (worker #1 abandoned), which then hangs as well.
        assert!(matches!(
            run_keychain_with_timeout(parked_op(rx2, 2)),
            Err(KeychainTimeout::TimedOut)
        ));
        std::thread::sleep(std::time::Duration::from_millis(150));
        // This submission abandons worker #2 (2 outstanding after it — the
        // cap counts ABANDONED workers, and #2 joins #1 only now) and serves
        // the op on a fresh worker #3.
        let third = run_keychain_with_timeout(|| 3);
        assert!(
            matches!(third, Ok(3)),
            "with room under the cap a fresh worker must serve the op: {third:?}"
        );
        // Worker #3 now hangs too: with 2 already abandoned, the cap is full.
        assert!(matches!(
            run_keychain_with_timeout(parked_op(rx3, 4)),
            Err(KeychainTimeout::TimedOut)
        ));
        std::thread::sleep(std::time::Duration::from_millis(150));
        assert!(
            abandoned_keychain_worker_outstanding(),
            "precondition: the cap is full of still-hung abandoned workers"
        );
        let fifth = run_keychain_with_timeout(|| 5);
        assert!(
            matches!(fifth, Err(KeychainTimeout::WorkerStuck)),
            "a hang past the abandoned-worker cap must be reported, not answered \
             with yet another thread: {fifth:?}"
        );
        let detail = format!("keyring get: {}", KeychainTimeout::WorkerStuck);
        assert!(is_keychain_unavailable_detail(&detail));

        tx1.send(()).unwrap();
        tx2.send(()).unwrap();
        tx3.send(()).unwrap();
        wait_worker_recovered();
    }

    /// T20 — cross-process flock pacing: two sequential paced calls through the
    /// file gate write MONOTONIC timestamps to the pace file, and the second
    /// pays the (test-shrunk) spacing. Uses an isolated temp pace path so no
    /// real vct_root is touched.
    #[cfg(unix)]
    #[test]
    fn cross_process_pace_writes_monotonic_timestamps() {
        let _lock = test_serialize::keychain_serialize_lock();
        let dir = tempfile::tempdir().expect("tempdir");
        let guard = TestPacePathGuard::new(dir.path().join("keyring.pace"));
        let _sp = TestSpacingGuard::new(std::time::Duration::from_millis(30));

        // First paced op establishes a timestamp.
        with_cross_process_pace(|| ());
        let t1 = guard.pace_timestamp().expect("pace file has a timestamp");

        // Second op back-to-back must pay ≥~20ms spacing and write a later ts.
        let start = std::time::Instant::now();
        with_cross_process_pace(|| ());
        let elapsed = start.elapsed();
        let t2 = guard.pace_timestamp().expect("pace file still has a timestamp");

        assert!(t2 >= t1, "pace timestamps must be monotonic: {t2} >= {t1}");
        assert!(
            elapsed >= std::time::Duration::from_millis(20),
            "back-to-back paced op must wait ~spacing; got {:?}",
            elapsed
        );
        assert_eq!(guard.warn_count(), 0, "healthy pace path must not warn");
    }

    /// T20 (fallback leg) — an UNWRITABLE pace path degrades to in-process
    /// pacing with EXACTLY ONE warn, and the op still runs (soft-fail, never a
    /// hard block on the user's secret read).
    #[cfg(unix)]
    #[test]
    fn cross_process_pace_unwritable_path_warns_once_and_proceeds() {
        let _lock = test_serialize::keychain_serialize_lock();
        // A file used AS a directory parent → create_dir_all + open both fail.
        let dir = tempfile::tempdir().expect("tempdir");
        let blocker = dir.path().join("iamafile");
        std::fs::write(&blocker, b"x").unwrap();
        // pace path = <file>/sub/keyring.pace — parent creation fails because
        // `blocker` is a regular file, not a directory.
        let guard = TestPacePathGuard::new(blocker.join("sub").join("keyring.pace"));

        let mut ran = false;
        with_cross_process_pace(|| ran = true);
        assert!(ran, "op must still run when the pace file is unwritable");
        assert_eq!(
            guard.warn_count(),
            1,
            "an unwritable pace path must warn exactly once (one-shot latch)"
        );

        // A second attempt must NOT warn again (one-shot).
        with_cross_process_pace(|| ());
        assert_eq!(guard.warn_count(), 1, "the degrade warn is one-shot per latch");
    }

    // ─── P0: the pace-lock acquire must be BOUNDED ────────────────────────────
    //
    // A blocking `flock(LOCK_EX)` here let ONE process holding the lock (parked
    // on an interactive unlock prompt, which dbus-secret-service polls for up
    // to a YEAR) permanently wedge every OTHER VCO process: their keychain
    // worker threads blocked in the acquire forever, so every later keychain
    // call in those processes fast-failed `WorkerStuck` with no self-heal.
    // Restarting the GUI did not help — only killing the lock HOLDER did,
    // because process death is what released the lock.
    //
    // These pins cover the decision on BOTH sides: acquire when the lock is (or
    // becomes) available, degrade when it does not — and prove the degrade
    // costs pacing only, never the caller's result.

    /// Hold an exclusive flock on `path` for `hold` on a background thread,
    /// from a SEPARATE open file description (flock locks are per-OFD, so a
    /// second `open()` in this same process contends exactly like a sibling
    /// launcher/hub process would).
    ///
    /// The returned receiver fires once the lock is genuinely held, so a test
    /// never races the holder's startup. The holder releases on its OWN timer
    /// rather than on a signal from the test: that way a REGRESSION to the
    /// blocking acquire makes the test FAIL (it waits out the hold, then
    /// acquires, then busts the assertions) instead of deadlocking the suite.
    #[cfg(unix)]
    fn hold_flock_for(
        path: std::path::PathBuf,
        hold: std::time::Duration,
    ) -> (
        std::thread::JoinHandle<()>,
        std::sync::mpsc::Receiver<()>,
    ) {
        use std::os::unix::io::AsRawFd;
        let (tx, rx) = std::sync::mpsc::channel::<()>();
        let handle = std::thread::spawn(move || {
            let file = std::fs::OpenOptions::new()
                .create(true)
                .read(true)
                .write(true)
                .truncate(false)
                .open(&path)
                .expect("holder opens the pace file");
            let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) };
            assert_eq!(rc, 0, "holder must take the exclusive lock");
            tx.send(()).expect("signal that the lock is held");
            std::thread::sleep(hold);
            unsafe {
                libc::flock(file.as_raw_fd(), libc::LOCK_UN);
            }
        });
        (handle, rx)
    }

    /// LEAVE-ALONE leg: with the lock FREE, the bounded acquire behaves exactly
    /// as the blocking one did — it takes the lock immediately, paces, stamps
    /// the completion timestamp, and does NOT warn. Guards against
    /// over-correcting into a fix that degrades eagerly (a bare `LOCK_NB` with
    /// no poll loop, or a zero deadline) and quietly loses cross-process pacing
    /// for everyone.
    #[cfg(unix)]
    #[test]
    fn pace_lock_uncontended_acquires_promptly_and_stamps() {
        let _lock = test_serialize::keychain_serialize_lock();
        let dir = tempfile::tempdir().expect("tempdir");
        let guard = TestPacePathGuard::new(dir.path().join("keyring.pace"));
        let _sp = TestSpacingGuard::new(std::time::Duration::from_millis(5));
        let deadline = std::time::Duration::from_millis(400);
        let _dl = cross_process_pace::TestPaceDeadlineGuard::new(deadline);

        let mut calls = 0u32;
        let start = std::time::Instant::now();
        let out = with_cross_process_pace(|| {
            calls += 1;
            "value".to_string()
        });
        let elapsed = start.elapsed();

        assert_eq!(out, "value", "the op's value is returned verbatim");
        assert_eq!(calls, 1, "the op runs exactly once");
        assert!(
            elapsed < deadline,
            "an UNCONTENDED acquire must succeed on the first non-blocking \
             attempt, not wait out the deadline; took {elapsed:?}"
        );
        assert!(
            guard.pace_timestamp().is_some(),
            "holding the lock must still stamp the completion timestamp"
        );
        assert_eq!(
            guard.warn_count(),
            0,
            "a healthy uncontended acquire must not warn (no phantom degrade)"
        );
    }

    /// ACT leg (the P0 fix): when another process holds the lock past the
    /// acquire deadline, we WAIT the deadline, then DEGRADE — we do not block
    /// until the holder releases. The op still runs exactly once, its value is
    /// returned verbatim, the degrade is reported exactly once, and the pace
    /// file we do NOT own is left byte-identical.
    ///
    /// RED-PROOF: restore the blocking `flock(fd, libc::LOCK_EX)` in
    /// `acquire_and_space` and this test fails on three counts — `elapsed`
    /// reaches the full 2.5s hold (busting the `elapsed < HOLD` bound), the
    /// warn count is 0 (the wedge was silent), and the pace file is
    /// overwritten with a real completion stamp.
    #[cfg(unix)]
    #[test]
    fn pace_lock_contended_past_deadline_degrades_bounded_and_warns_once() {
        let _lock = test_serialize::keychain_serialize_lock();
        let dir = tempfile::tempdir().expect("tempdir");
        let pace = dir.path().join("keyring.pace");
        // Seed a recognisable stamp so we can prove the degraded path does not
        // write a file it does not hold the lock on.
        std::fs::write(&pace, b"12345").expect("seed the pace file");
        let guard = TestPacePathGuard::new(pace.clone());
        let _sp = TestSpacingGuard::new(std::time::Duration::from_millis(5));
        let deadline = std::time::Duration::from_millis(300);
        let _dl = cross_process_pace::TestPaceDeadlineGuard::new(deadline);

        // Hold far longer than the deadline — this stands in for a sibling
        // process parked on an unlock prompt.
        const HOLD: std::time::Duration = std::time::Duration::from_millis(2500);
        let (holder, held) = hold_flock_for(pace.clone(), HOLD);
        held.recv_timeout(std::time::Duration::from_secs(5))
            .expect("holder thread must take the lock");

        let mut calls = 0u32;
        let start = std::time::Instant::now();
        let out = with_cross_process_pace(|| {
            calls += 1;
            "secret-shaped-value".to_string()
        });
        let elapsed = start.elapsed();

        assert!(
            elapsed >= deadline,
            "the acquire must genuinely WAIT its budget before degrading \
             (ordinary contention should still get paced); waited {elapsed:?}"
        );
        assert!(
            elapsed < HOLD,
            "the acquire must give up at the deadline, NOT block until the \
             holder releases; waited {elapsed:?} against a {HOLD:?} hold — \
             this is the wedge that took down both the launcher and the hub"
        );
        assert_eq!(
            out, "secret-shaped-value",
            "the degrade must not alter the caller's result"
        );
        assert_eq!(calls, 1, "the degrade must not skip or repeat the op");
        assert_eq!(
            guard.warn_count(),
            1,
            "giving up on the lock must be OBSERVABLE — 'could not acquire' is \
             not the same fact as 'nothing to pace'"
        );
        assert_eq!(
            std::fs::read_to_string(&pace).expect("read pace file"),
            "12345",
            "the degraded path must NOT write the pace file — another process \
             holds the lock and is reading it under that lock"
        );

        // One-shot: a second degrade in the same process does not warn again.
        let out2 = with_cross_process_pace(|| "again".to_string());
        assert_eq!(out2, "again");
        assert_eq!(
            guard.warn_count(),
            1,
            "the contended-degrade warn is one-shot per process"
        );

        holder.join().expect("holder thread joins");
    }

    /// LEAVE-ALONE leg: a lock held only BRIEFLY (released well within the
    /// deadline) must be waited for and then acquired NORMALLY — no degrade, no
    /// warn, and a real completion stamp proving we held it. This is what keeps
    /// the G5 gnome-keyring protection intact for ordinary contention, which is
    /// the overwhelmingly common case.
    ///
    /// RED-PROOF: shrink the deadline to `Duration::ZERO` (i.e. a bare
    /// `LOCK_NB` with no poll loop) and this fails — warn count 1, no stamp.
    #[cfg(unix)]
    #[test]
    fn pace_lock_released_within_deadline_acquires_without_degrading() {
        let _lock = test_serialize::keychain_serialize_lock();
        let dir = tempfile::tempdir().expect("tempdir");
        let pace = dir.path().join("keyring.pace");
        // A stamp of 1ns-since-epoch: any REAL completion stamp is astronomically
        // larger, so a strict `>` proves the acquired path wrote it.
        std::fs::write(&pace, b"1").expect("seed the pace file");
        let guard = TestPacePathGuard::new(pace.clone());
        let _sp = TestSpacingGuard::new(std::time::Duration::from_millis(5));
        let deadline = std::time::Duration::from_secs(5);
        let _dl = cross_process_pace::TestPaceDeadlineGuard::new(deadline);

        const HOLD: std::time::Duration = std::time::Duration::from_millis(400);
        let (holder, held) = hold_flock_for(pace.clone(), HOLD);
        held.recv_timeout(std::time::Duration::from_secs(5))
            .expect("holder thread must take the lock");

        let mut calls = 0u32;
        let start = std::time::Instant::now();
        let out = with_cross_process_pace(|| {
            calls += 1;
            7u32
        });
        let elapsed = start.elapsed();

        assert_eq!(out, 7);
        assert_eq!(calls, 1);
        assert!(
            elapsed >= HOLD - std::time::Duration::from_millis(80),
            "the acquire must WAIT for a briefly-held lock rather than \
             degrading on first contention; returned after only {elapsed:?}"
        );
        assert!(
            elapsed < deadline,
            "and it must acquire as soon as the holder releases, well inside \
             the deadline; took {elapsed:?}"
        );
        assert_eq!(
            guard.warn_count(),
            0,
            "a lock released inside the deadline is NOT a degrade — warning \
             here would cry wolf on ordinary contention"
        );
        assert!(
            guard.pace_timestamp().unwrap_or(0) > 1,
            "we really held the lock, so the completion stamp must have been \
             written (proving this was an acquire, not a silent degrade)"
        );

        holder.join().expect("holder thread joins");
    }

    /// The contended-degrade warn must have its OWN one-shot latch: an earlier,
    /// unrelated "pace file unusable" warn must not silently swallow the report
    /// of a real cross-process wedge. Same conflation family as `[]`-on-
    /// transport-failure meaning "absent" — two different facts must not share
    /// one suppression bit.
    ///
    /// RED-PROOF: point the contended branch at the shared `warn_once` (one
    /// latch for both causes) and the final count stays 1 instead of 2.
    #[cfg(unix)]
    #[test]
    fn contended_degrade_warn_is_not_swallowed_by_unusable_pace_warn() {
        let _lock = test_serialize::keychain_serialize_lock();
        let dir = tempfile::tempdir().expect("tempdir");
        let _sp = TestSpacingGuard::new(std::time::Duration::from_millis(5));
        let deadline = std::time::Duration::from_millis(200);
        let _dl = cross_process_pace::TestPaceDeadlineGuard::new(deadline);

        // (1) Trip the UNUSABLE-pace-file degrade: a regular file used as a
        // directory parent makes both create_dir_all and open fail.
        let blocker = dir.path().join("iamafile");
        std::fs::write(&blocker, b"x").expect("write blocker file");
        let guard = TestPacePathGuard::new(blocker.join("sub").join("keyring.pace"));
        with_cross_process_pace(|| ());
        assert_eq!(
            guard.warn_count(),
            1,
            "the unusable-pace-file degrade warns once"
        );

        // (2) WITHOUT resetting the latches (that is the whole point), switch to
        // a usable but CONTENDED pace file and degrade again.
        let pace = dir.path().join("keyring.pace");
        let prev = cross_process_pace::set_test_pace_path(Some(pace.clone()));
        let (holder, held) = hold_flock_for(pace.clone(), std::time::Duration::from_millis(1500));
        held.recv_timeout(std::time::Duration::from_secs(5))
            .expect("holder thread must take the lock");
        with_cross_process_pace(|| ());
        let after = cross_process_pace::WARN_COUNT.load(std::sync::atomic::Ordering::SeqCst);
        // Restore the path override before asserting so a failure still leaves
        // the global seam in the state `guard`'s Drop expects.
        cross_process_pace::set_test_pace_path(prev);
        holder.join().expect("holder thread joins");

        assert_eq!(
            after, 2,
            "a contended-lock degrade must still report itself after an \
             unrelated degrade warn already fired — separate one-shot latches"
        );
    }

    /// CALLER-LEVEL: the degrade must cost pacing ONLY. Drives the exact
    /// production sandwich `with_cross_process_pace(|| retry_with_backoff(op))`
    /// — the middle layer is the only thing that differs between the acquired
    /// and degraded paths — and asserts the caller receives an IDENTICAL
    /// outcome either way, for a hit, a genuine miss, and a transient blip that
    /// recovers on retry. A degrade that dropped, duplicated, or reclassified
    /// the op would show up here as a difference between the two columns.
    #[cfg(unix)]
    #[test]
    fn degraded_pace_gives_the_caller_identical_results_to_the_acquired_path() {
        let _lock = test_serialize::keychain_serialize_lock();
        let dir = tempfile::tempdir().expect("tempdir");
        let pace = dir.path().join("keyring.pace");
        let _guard = TestPacePathGuard::new(pace.clone());
        let _sp = TestSpacingGuard::new(std::time::Duration::from_millis(1));
        let _dl = cross_process_pace::TestPaceDeadlineGuard::new(
            std::time::Duration::from_millis(150),
        );

        // The three outcomes a real keychain op can produce, exercised through
        // the real `retry_with_backoff` (so pacing, classification and the
        // attempt budget are the production ones).
        fn exercise() -> (
            (keyring::Result<String>, u32),
            (keyring::Result<String>, u32),
            (keyring::Result<String>, u32),
        ) {
            let mut hit_calls = 0u32;
            let hit = with_cross_process_pace(|| {
                retry_with_backoff(|| {
                    hit_calls += 1;
                    Ok("s3cret-value".to_string())
                })
            });
            let mut miss_calls = 0u32;
            let miss = with_cross_process_pace(|| {
                retry_with_backoff(|| {
                    miss_calls += 1;
                    Err(keyring::Error::NoEntry)
                })
            });
            let mut blip_calls = 0u32;
            let blip = with_cross_process_pace(|| {
                retry_with_backoff(|| {
                    blip_calls += 1;
                    if blip_calls == 1 {
                        Err(keyring::Error::PlatformFailure(Box::new(
                            std::io::Error::other("daemon hiccup"),
                        )))
                    } else {
                        Ok("recovered-value".to_string())
                    }
                })
            });
            (
                (hit, hit_calls),
                (miss, miss_calls),
                (blip, blip_calls),
            )
        }

        // Column A: lock free → acquired path.
        let (a_hit, a_miss, a_blip) = exercise();

        // Column B: lock held for the whole run → degraded path.
        let (holder, held) = hold_flock_for(pace.clone(), std::time::Duration::from_secs(3));
        held.recv_timeout(std::time::Duration::from_secs(5))
            .expect("holder thread must take the lock");
        let (b_hit, b_miss, b_blip) = exercise();
        holder.join().expect("holder thread joins");

        assert_eq!(
            a_hit.0.as_deref().ok(),
            Some("s3cret-value"),
            "acquired path returns the value"
        );
        assert_eq!(
            b_hit.0.as_deref().ok(),
            a_hit.0.as_deref().ok(),
            "the DEGRADED path must return the same value — losing the pace \
             lock must never lose or alter a secret"
        );
        assert_eq!(
            (a_hit.1, b_hit.1),
            (1, 1),
            "one attempt on both paths (no dropped or duplicated keychain op)"
        );

        assert!(
            matches!(a_miss.0, Err(keyring::Error::NoEntry))
                && matches!(b_miss.0, Err(keyring::Error::NoEntry)),
            "a genuine miss stays a miss on both paths — a degrade must not \
             turn 'absent' into an error or vice versa"
        );
        assert_eq!((a_miss.1, b_miss.1), (1, 1), "a miss is not retried");

        assert_eq!(
            b_blip.0.as_deref().ok(),
            a_blip.0.as_deref().ok(),
            "a transient blip recovers identically on both paths"
        );
        assert_eq!(
            (a_blip.1, b_blip.1),
            (2, 2),
            "same attempt budget on both paths (degrading must not change \
             retry semantics)"
        );
    }

    // ─── v0.2.84 (D8.3): pacing RELEASE-GAP regression pins ───────────────────
    //
    // The pre-fix code stamped the pace timestamp at op-START. An op that
    // itself ran ≥ spacing (a slow daemon read) therefore left ~0 idle between
    // one op's END and the next op's START — the sustained back-to-back daemon
    // traffic the P7 SIGTRAP reproduced under. The fix stamps op-END, so the
    // next op always spaces ≥ spacing from the previous op's COMPLETION.
    //
    // These pins FAIL pre-fix: pre-fix the measured gap is ~0.

    /// REGRESSION PIN (cross-process): an injected op that sleeps > spacing
    /// makes the NEXT op observe ≥ spacing of true idle measured from the FIRST
    /// op's COMPLETION. Fails pre-fix (start-stamped ⇒ ~0 gap).
    #[cfg(unix)]
    #[test]
    fn cross_process_pace_gap_measured_from_op_end_release_gap_d83() {
        let _lock = test_serialize::keychain_serialize_lock();
        let dir = tempfile::tempdir().expect("tempdir");
        let _pace = TestPacePathGuard::new(dir.path().join("keyring.pace"));
        let spacing = std::time::Duration::from_millis(60);
        let _sp = TestSpacingGuard::new(spacing);

        // op1 runs LONGER than spacing, then records the instant it FINISHED.
        let mut op1_end: Option<std::time::Instant> = None;
        with_cross_process_pace(|| {
            std::thread::sleep(spacing * 2);
            op1_end = Some(std::time::Instant::now());
        });
        let op1_end = op1_end.expect("op1 recorded its completion instant");

        // op2 back-to-back records the instant its body STARTED. The pace layer
        // must have made op2 wait ≥ spacing from op1's END before running it.
        let mut op2_start: Option<std::time::Instant> = None;
        with_cross_process_pace(|| {
            op2_start = Some(std::time::Instant::now());
        });
        let op2_start = op2_start.expect("op2 recorded its start instant");

        let gap = op2_start.saturating_duration_since(op1_end);
        // Allow a small scheduler slack below the nominal spacing.
        assert!(
            gap >= spacing - std::time::Duration::from_millis(15),
            "op2 must start ≥ ~spacing ({spacing:?}) after op1 END; \
             gap was {gap:?} (pre-fix start-stamp regression = ~0)"
        );
    }

    /// REGRESSION PIN (in-process `paced_call`): same release-gap semantics for
    /// the per-process pacer. An op sleeping > spacing ⇒ the next `paced_call`
    /// waits ≥ spacing measured from the first op's completion. Fails pre-fix.
    #[test]
    fn paced_call_gap_measured_from_op_end_release_gap_d83() {
        let _lock = test_serialize::keychain_serialize_lock();
        let spacing = std::time::Duration::from_millis(50);
        let _sp = TestSpacingGuard::new(spacing);

        // Establish a baseline op END far enough back that the FIRST op below
        // pays no spacing (idle gap already exceeded).
        paced_call(|| ());
        std::thread::sleep(spacing * 2);

        // op1 runs longer than spacing and records its completion instant.
        let mut op1_end: Option<std::time::Instant> = None;
        paced_call(|| {
            std::thread::sleep(spacing * 2);
            op1_end = Some(std::time::Instant::now());
        });
        let op1_end = op1_end.expect("op1 completion recorded");

        // op2 back-to-back: its body must start ≥ spacing after op1's END.
        let mut op2_start: Option<std::time::Instant> = None;
        paced_call(|| {
            op2_start = Some(std::time::Instant::now());
        });
        let op2_start = op2_start.expect("op2 start recorded");

        let gap = op2_start.saturating_duration_since(op1_end);
        assert!(
            gap >= spacing - std::time::Duration::from_millis(12),
            "in-process paced_call must space op2 ≥ ~spacing ({spacing:?}) from \
             op1 END; gap was {gap:?} (pre-fix start-stamp regression = ~0)"
        );
    }


    /// v0.2.102 R2 (owner 2026-10-07) — SELF-HEAL: an abandoned worker whose
    /// hung op has outlived the retirement limit is written off. Today the
    /// SECOND abandoned hang makes every later op answer `WorkerStuck`
    /// FOREVER; with retirement, a stale abandoned worker no longer blocks a
    /// fresh replacement.
    #[test]
    fn a_retired_abandoned_worker_stops_answering_stuck_forever() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _tg = TestKeychainTimeoutGuard::new(std::time::Duration::from_millis(100));
        let _rg = TestWorkerRetireAfterGuard::new(std::time::Duration::from_millis(250));
        let (tx1, rx1) = std::sync::mpsc::channel::<()>();
        let (tx2, rx2) = std::sync::mpsc::channel::<()>();

        // Worker #1 hangs; its caller times out; the next submission abandons it.
        assert!(matches!(
            run_keychain_with_timeout(parked_op(rx1, 1)),
            Err(KeychainTimeout::TimedOut)
        ));
        std::thread::sleep(std::time::Duration::from_millis(150));
        // Worker #2 hangs the same way (cap not reached: it is abandoned too).
        assert!(matches!(
            run_keychain_with_timeout(parked_op(rx2, 2)),
            Err(KeychainTimeout::TimedOut)
        ));
        std::thread::sleep(std::time::Duration::from_millis(150));
        // Worker #1's hung op started ~500ms ago — past the 250ms retirement
        // limit — so it is RETIRED and this op must run on a FRESH worker
        // instead of answering WorkerStuck (the pre-R2 behaviour).
        let third = run_keychain_with_timeout(|| 3);
        assert!(
            matches!(third, Ok(3)),
            "a retired abandoned worker must no longer poison the process: {third:?}"
        );
        tx1.send(()).unwrap();
        tx2.send(()).unwrap();
        wait_worker_recovered();
    }

    /// The abandoned op's late result lands in ITS OWN (dropped) result
    /// channel — it can never be delivered to a different job.
    #[test]
    fn a_late_result_from_a_retired_worker_is_discarded() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _tg = TestKeychainTimeoutGuard::new(std::time::Duration::from_millis(100));
        let _rg = TestWorkerRetireAfterGuard::new(std::time::Duration::from_millis(250));
        let (tx1, rx1) = std::sync::mpsc::channel::<()>();
        let (tx2, rx2) = std::sync::mpsc::channel::<()>();

        assert!(matches!(
            run_keychain_with_timeout(parked_op(rx1, 111)),
            Err(KeychainTimeout::TimedOut)
        ));
        std::thread::sleep(std::time::Duration::from_millis(150));
        assert!(matches!(
            run_keychain_with_timeout(parked_op(rx2, 222)),
            Err(KeychainTimeout::TimedOut)
        ));
        std::thread::sleep(std::time::Duration::from_millis(150));
        let third = run_keychain_with_timeout(|| 777);
        assert!(matches!(third, Ok(777)), "fresh worker serves the new job: {third:?}");
        // The abandoned op FINISHES now, returning 111 into its own dropped
        // channel — nothing may observe it.
        tx1.send(()).unwrap();
        let fourth = run_keychain_with_timeout(|| 888);
        assert!(matches!(fourth, Ok(888)), "a later job gets ITS OWN result: {fourth:?}");
        tx2.send(()).unwrap();
        wait_worker_recovered();
    }
}
