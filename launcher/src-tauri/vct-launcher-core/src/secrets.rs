//! OS keychain wrapper for module secrets.
//!
//! Primary store: keychain (macOS Keychain / Windows Credential Manager /
//! Linux libsecret). Service namespace: `vct.{scope}.{module_id}.{key}`
//! where scope is `{project_id}` for per-project secrets, `global` for
//! machine-wide, `{project_id}.shared` for cross-module-per-project.
//!
//! ─── Architecture note (post-Fix-#3 cleanup, 0.1.7) ──────────────────
//!
//! Earlier in 0.1.7 (PR #171, "Fix #3") this module also mirrored a
//! hard-coded allowlist of well-known shared keys (`github_pat`,
//! `OPENAI_API_KEY`, etc.) to `~/.vct-secrets/<key>` files so bundled
//! MCP wrappers and hooks could read them with `cat`. That was an
//! escape hatch for wrappers that pre-dated the launcher's keychain
//! layer; it was rejected by the project owner because:
//!
//!   * It hard-codes a set of "blessed" keys that bypass the per-project
//!     active-flag gate (Lifecycle B). A paused secret in the launcher
//!     GUI was still readable on disk.
//!   * It materialises secret values to disk for any tool that scans
//!     `~/.vct-secrets/`, weakening the "keychain is authoritative"
//!     contract.
//!   * It scales by a hard-coded allowlist, not by the per-project
//!     access matrix the launcher already maintains.
//!
//! Replacement: the launcher's hub HTTP server (port 7700, see
//! `crate::hub::modules_api::project_env`) exposes
//! `GET /api/v1/projects/{id}/env`, which returns the active set of
//! (key, value) pairs the project is entitled to — keychain resolution
//! + active-flag gating + cross-launcher pause check, all in one
//! place. Bundled wrappers consume that endpoint via the shared
//! resolver helper at `templates/scripts/vct_secrets_resolve.sh`
//! (Bash) / `.ps1` (PowerShell). See `docs/MIGRATION-0.2.0.md`.
//!
//! Reads in this module go through the keychain only. There is no
//! file-side mirror anywhere in the launcher's set/delete paths.
//! One consumer path still writes file artefacts under `~/.vct-secrets/`,
//! an INTENTIONAL exception that's outside the launcher's runtime:
//!
//!   * `tools/vct-secrets/` — the user-facing `vct` CLI for set/get
//!     of arbitrary file-based secrets. This is a Phase 1 primitive
//!     used by external scripts that don't talk to the hub; it stays
//!     as-is.
//!
//! ─── 0.1.7 fork-readiness sweep (2026-05-08) ─────────────────────────
//!
//! `commands::installer::register_github_pat` previously wrote the
//! onboarding-wizard PAT to `~/.vct-secrets/shared/github_pat` instead
//! of going through this module. That was the one remaining
//! "the launcher writes a secret in plaintext to a file" path, flagged
//! as fork-blocking by the secrets-architecture audit. It now uses
//! `secrets::set(SecretScope::Shared { project_id: SENTINEL_SHARED },
//! "user", "github_pat", &token)` — see the doc comment on the
//! `register_github_pat` block in `commands/installer.rs` for the
//! migration semantics (`migrate_github_pat_file_to_keychain`,
//! gated behind the `app_state` flag `github_pat.file_to_keychain.v1`,
//! plus `migrate_github_pat_installer_to_user_module_id` gated behind
//! `github_pat.installer_to_user_module_id.v1`).
//!
//! 2026-05-10 (post-0.2.0 backlog #6): the module_id segment changed
//! from `"installer"` (which only `register_github_pat` used) to
//! `"user"` (the canonical user-bucket the SecretsPanel "Shared (this
//! user)" tab also writes to). Both writers now share one keychain
//! row, eliminating the stale-shadow row that appeared when a user
//! used both UI flows over time.
//!
//! That tuple is also what the env-pair builder in
//! `commands/projects_v2.rs::write_project_env_files` reads when
//! emitting `GITHUB_TOKEN` to per-project env files (replaces the
//! retired `git-credential-vct` helper).
//!
//! `commands::installer::*` is the only path that calls into this
//! module from outside `commands/secrets_cmd.rs`. This module remains
//! pure keychain.
//!
//! ─── Concurrency model (test vs runtime, 2026-05-08) ─────────────────
//!
//! Real-world keychain access from this module is concurrency-safe:
//! libsecret + gnome-keyring-daemon (Linux), Keychain Services (macOS),
//! and Credential Manager (Windows) all serialise their own internal
//! state, and reads are independent — two processes calling
//! `secrets::get` on different (or same) entries don't race. Writes
//! happen at GUI-rate (a user clicking "Save" in SecretsPanel, or the
//! OnboardingWizard registering a PAT once), so contention in
//! production is bounded by user click-rate, not parallelism.
//!
//! TESTS are different. `cargo test --lib` defaults to ~8 parallel
//! threads; multiple test modules write/delete the same keychain
//! entries (most pressingly `vct._user_shared_.shared.user/github_pat`
//! — and, for the module_id-consolidation migration tests, the legacy
//! `vct._user_shared_.shared.installer/github_pat` slot too)
//! within a few hundred milliseconds. The daemon side handles each
//! D-Bus request atomically, but under that write rate gnome-keyring
//! has been seen to return `keyring::Error::PlatformFailure` and to
//! SIGTRAP-crash, taking the SSH-agent integration down with it
//! (observed: 2026-05-08 and 2026-05-13 — the latter happened on the
//! user's machine DURING this very mitigation work and is what drove
//! the four-layer defence below).
//!
//! Four-layer defence:
//!
//!   1. **Single-threaded test execution** (`scripts/test-keychain-safe
//!      .{sh,ps1}`) — the canonical way to run the test suite locally
//!      AND in CI. Pinned via `RUST_TEST_THREADS=1` (forwarded as
//!      `-- --test-threads=1` to the test binary). The other three
//!      layers exist for defence-in-depth, but this is the load-bearing
//!      one — with parallel test execution, even the pacing + retry
//!      below have been seen to be insufficient because
//!      `keyring::Entry::new()` probe calls and `keyring_available()`
//!      helpers bypass the in-crate pacing layer.
//!
//!   2. **Test-side mutex** (`test_serialize::keychain_serialize_lock`,
//!      below) — process-wide mutex serialising every keychain-touching
//!      test in the launcher binary. Even with `--test-threads=1` this
//!      is still useful: it documents the contract at the call site and
//!      protects against accidental re-introduction of parallelism via
//!      `#[tokio::test(flavor = "multi_thread")]` or a future tooling
//!      change. Tests in `commands::dashboard`, `commands::installer::
//!      github_pat_keychain_tests`, `commands::desktop_shortcut`,
//!      `commands::secrets_cmd`, and `hub::modules_api` all acquire
//!      that mutex.
//!
//!   3. **Runtime rate-limit** (`paced_call`) — every `set`/`get`/
//!      `delete` call goes through a process-wide mutex that enforces
//!      ≥150ms between consecutive calls. This caps the rate at which
//!      the launcher hits the OS daemon to ~6.7 ops/sec — well below
//!      the threshold at which gnome-keyring 46.x has been observed to
//!      crash under repeated load. Pacing is invisible to single-shot
//!      callers (mutex contention adds <1ms for sparse callers) and is
//!      essential for the burst case (a watcher tick that re-reads 20
//!      project envs in succession). This protects PRODUCTION too —
//!      the launcher shares the daemon with the user's other apps.
//!
//!   4. **Retry with backoff** (`retry_with_backoff`) — on
//!      `PlatformFailure` (the daemon-hiccup error), up to THREE
//!      retries with progressive backoff (50ms, 250ms, 1000ms). The 1s
//!      tail gives the daemon time to respawn fully after a SIGTRAP
//!      before we give up. Permanent errors (`NoEntry`, `BadEncoding`,
//!      `TooLong`, `Invalid`, `Ambiguous`, `NoStorageAccess`) are NOT
//!      retried — retrying won't change their outcome.
//!
//! Layer 1 (single-threaded tests) is mandatory. Layers 2–4 are
//! defence-in-depth: they don't fully prevent a SIGTRAP under parallel
//! load (the keyring crate's `Entry::new()` and direct `set_password`/
//! `get_password` calls in test probe helpers bypass the pacing layer),
//! but they make local development less fragile and they harden the
//! production runtime against the same class of daemon-overload bug.
//!
//! Worst-case added latency on the failure path: 150ms pacing + 50ms
//! + 250ms + 1000ms = ~1.45s before propagating an error.

use keyring::Entry;

const SERVICE_PREFIX: &str = "vct";

#[derive(Debug, Clone, Copy)]
pub enum SecretScope<'a> {
    /// Per-project secret: one value per (project, module).
    PerProject { project_id: &'a str },
    /// Machine-wide secret shared across all projects.
    Global,
    /// Shared across modules within a single project.
    Shared { project_id: &'a str },
}

impl<'a> SecretScope<'a> {
    /// Build the full keychain service string for a (scope, module, key).
    ///
    /// Keychain backends key off (service, username). We use `service` as
    /// the full namespace and `username` as the secret key to keep entries
    /// discoverable in the OS credential manager UI.
    pub fn service_name(&self, module_id: &str) -> String {
        match self {
            SecretScope::PerProject { project_id } => {
                format!("{}.{}.{}", SERVICE_PREFIX, project_id, module_id)
            }
            SecretScope::Global => {
                format!("{}.global.{}", SERVICE_PREFIX, module_id)
            }
            SecretScope::Shared { project_id } => {
                format!("{}.{}.shared.{}", SERVICE_PREFIX, project_id, module_id)
            }
        }
    }
}

// v0.2.72 (P9) rationale (kept for the maintainer): `Entry::new` is itself a
// D-Bus Secret-Service negotiation on Linux (`sync-secret-service`).
// Constructing it OUTSIDE the pacing layer meant a burst of N secret ops fired
// N unpaced D-Bus calls, which crashed gnome-keyring 46.1 on Ubuntu 24.04 under
// concurrency (SIGTRAP, apport-mislabelled "SSH Key Agent closed unexpectedly").
// NOT a leak — VCO's retry recovered, but the crash still popped a dialog. So
// `set`/`get`/`delete` build the `Entry::new(&service, &key)` INSIDE their
// `retry_with_backoff` closure, routing construction through the same
// `paced_call` (150ms process-wide spacing) + progressive backoff as the op.
// v0.2.76 (A4): the shared `entry_result` construction helper was inlined into
// those closures so each owns its `service` String and the whole closure is
// 'static + Send for the bounded-timeout worker; the structural guard
// `p9_source_shape_entry_construction_inside_retried_closure` pins the shape.

/// 0.1.7 H1 (2026-05-08): retry on transient daemon-hiccup errors.
/// 2026-05-13 upgrade: 1 retry → 3 attempts with progressive backoff
/// after a fresh SIGTRAP crash on the user's machine confirmed the
/// 1-retry budget wasn't enough to ride out a daemon respawn.
///
/// Only `keyring::Error::PlatformFailure` is treated as transient.
/// `NoStorageAccess` (credential store locked / access-denied),
/// `NoEntry` (semantic miss), `BadEncoding` / `TooLong` / `Invalid`
/// / `Ambiguous` (permanent shape errors) all propagate immediately —
/// retrying any of them won't change the outcome and would just add
/// latency to every error.
fn is_transient(err: &keyring::Error) -> bool {
    matches!(err, keyring::Error::PlatformFailure(_))
}

/// Progressive backoff schedule for `retry_with_backoff`. The last entry
/// (1000ms) is long enough for the OS daemon to respawn after a SIGTRAP
/// — observed respawn time on Linux + gnome-keyring 46.1 is ~200-400ms,
/// so 1s leaves comfortable headroom.
///
/// Total worst-case wait across all backoffs: 50 + 250 + 1000 = 1300ms.
/// On top of that the call itself may take up to ~1s waiting for D-Bus,
/// so the failure-path budget is ~2.5s before propagating.
const BACKOFF_SCHEDULE: [std::time::Duration; 3] = [
    std::time::Duration::from_millis(50),
    std::time::Duration::from_millis(250),
    std::time::Duration::from_millis(1000),
];

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
/// Test-visible: `MIN_CALL_SPACING` can be overridden inside `#[cfg(test)]`
/// via `with_test_spacing` to keep the test suite fast. Production
/// callers always pay the full 150ms.
fn paced_call<T>(f: impl FnOnce() -> T) -> T {
    let spacing = current_spacing();
    {
        let mut last = LAST_KEYRING_CALL.lock().unwrap_or_else(|p| p.into_inner());
        if let Some(prev) = *last {
            let elapsed = prev.elapsed();
            if elapsed < spacing {
                std::thread::sleep(spacing - elapsed);
            }
        }
        *last = Some(std::time::Instant::now());
    }
    f()
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

/// Run `f` with rate-limiting + progressive-backoff retries on transient
/// `keyring::Error`. Each attempt goes through `paced_call`, so the
/// daemon sees a steady-state ≤6.7 ops/sec even when many callers are
/// active concurrently.
///
/// Attempt budget = `BACKOFF_SCHEDULE.len() + 1` = 4 (1 initial + 3
/// retries). The schedule is consumed in order; on attempt N (1-indexed,
/// N > 1) we sleep `BACKOFF_SCHEDULE[N - 2]` before running `f` again.
///
/// The closure is `FnMut` because each call re-issues a D-Bus / native
/// API call — there is no shared state captured between attempts.
/// Test seam: callers can pass a closure whose body counts invocations
/// to verify "at most 4 attempts" without standing up a real keychain.
fn retry_with_backoff<T>(mut f: impl FnMut() -> keyring::Result<T>) -> keyring::Result<T> {
    let mut last_err: Option<keyring::Error> = None;
    for attempt in 0..=BACKOFF_SCHEDULE.len() {
        if attempt > 0 {
            std::thread::sleep(BACKOFF_SCHEDULE[attempt - 1]);
        }
        match paced_call(&mut f) {
            Ok(v) => return Ok(v),
            Err(e) if is_transient(&e) => {
                last_err = Some(e);
                continue;
            }
            Err(e) => return Err(e),
        }
    }
    // All attempts exhausted on transient errors. Propagate the last one.
    Err(last_err.expect("loop runs at least once"))
}

// ─── Bounded-timeout execution primitive (v0.2.76 A4) ─────────────────────────
//
// The pacing/backoff layers above serialize and retry keychain calls, but the
// underlying `keyring` op (`Entry::new` D-Bus negotiation, `set_password`,
// `get_password`, `delete_credential`) can block UNBOUNDEDLY when the OS Secret
// Service is slow or wedged (observed live: gnome-keyring hanging a keychain
// call for 13+ minutes at ~0% CPU). A blocked op hangs whatever called it — the
// hub `/env` value read, a launcher GUI keychain command, or a test — with no
// timeout anywhere in the stack.
//
// This primitive wraps keychain execution in a bounded timeout, running the op
// on ONE dedicated long-lived worker thread fed by a request channel. Design
// points:
//
//   * ONE worker thread (not thread-per-op): a timed-out op leaves its job
//     stuck ON THE WORKER; we do NOT spawn a fresh thread per call (that would
//     leak one abandoned thread per timeout). Instead, while the worker is
//     stuck the NEXT op fails FAST with a distinct "keychain worker stuck"
//     error until the wedged op returns and the worker drains.
//   * The timeout wraps OUTSIDE pacing/backoff (v0.2.72 P9 semantics
//     unchanged) — the job closure IS the full retry_with_backoff call.
//   * On timeout the caller gets an `Err`; every caller already handles `Err`
//     (hub /env → key not served; GUI set → error toast; never a panic).

/// Per-op keychain timeout. Generous: a healthy Secret Service answers in
/// milliseconds; the pacing + backoff worst case is ~1.45s, so 10s leaves
/// ample headroom for a slow-but-alive daemon while still bounding a wedged
/// one. A wedged daemon crosses this and the op fails loudly instead of
/// hanging forever.
const KEYCHAIN_OP_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(10);

/// Error returned when a keychain op could not complete within the bound.
#[derive(Debug)]
pub enum KeychainTimeout {
    /// This op itself exceeded `KEYCHAIN_OP_TIMEOUT`.
    TimedOut,
    /// A PRIOR op is still stuck on the worker (the Secret Service has not
    /// unwedged); this op failed fast rather than queue behind it.
    WorkerStuck,
    /// The worker thread/channel is unavailable (should not happen in
    /// practice; treated conservatively as a failure).
    WorkerUnavailable,
}

impl std::fmt::Display for KeychainTimeout {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            KeychainTimeout::TimedOut => write!(
                f,
                "keychain operation timed out — Secret Service unresponsive \
                 (exceeded {}s)",
                KEYCHAIN_OP_TIMEOUT.as_secs()
            ),
            KeychainTimeout::WorkerStuck => write!(
                f,
                "keychain worker stuck — a prior keychain operation is still \
                 blocked on an unresponsive Secret Service; try again once it \
                 recovers"
            ),
            KeychainTimeout::WorkerUnavailable => {
                write!(f, "keychain worker unavailable")
            }
        }
    }
}

type KeychainJob = Box<dyn FnOnce() + Send + 'static>;

/// Lazily-started worker: owns the single thread that actually touches the OS
/// keychain. `in_flight` > 0 means a job is currently executing (possibly
/// wedged); a new caller checks it to fast-fail rather than queue behind a
/// stuck op.
struct KeychainWorker {
    tx: std::sync::mpsc::Sender<KeychainJob>,
    in_flight: std::sync::Arc<std::sync::atomic::AtomicUsize>,
}

fn keychain_worker() -> &'static KeychainWorker {
    use std::sync::OnceLock;
    static WORKER: OnceLock<KeychainWorker> = OnceLock::new();
    WORKER.get_or_init(|| {
        let (tx, rx) = std::sync::mpsc::channel::<KeychainJob>();
        let in_flight = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let worker_in_flight = in_flight.clone();
        // Detached; lives for the process. A wedged op parks THIS thread (not a
        // fresh one per call) until the daemon unwedges, then it drains the
        // queue.
        std::thread::Builder::new()
            .name("vct-keychain-worker".into())
            .spawn(move || {
                while let Ok(job) = rx.recv() {
                    worker_in_flight.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                    job();
                    worker_in_flight.fetch_sub(1, std::sync::atomic::Ordering::SeqCst);
                }
            })
            .expect("spawn keychain worker thread");
        KeychainWorker { tx, in_flight }
    })
}

/// Run a keychain op with a bounded timeout on the shared worker thread.
///
/// `f` is the full op INCLUDING pacing/backoff — the timeout wraps outside
/// them (A4d). Returns `Ok(f())` when it completes within
/// `KEYCHAIN_OP_TIMEOUT`; `Err(WorkerStuck)` immediately if a prior op is
/// still blocked; `Err(TimedOut)` if this op exceeds the bound.
pub(crate) fn run_keychain_with_timeout<T, F>(f: F) -> Result<T, KeychainTimeout>
where
    T: Send + 'static,
    F: FnOnce() -> T + Send + 'static,
{
    use std::sync::atomic::Ordering;

    let worker = keychain_worker();
    // Fast-fail if the worker is already busy on a (possibly wedged) prior op.
    // Single-worker means a queued job would otherwise inherit the prior op's
    // stall; failing fast keeps callers responsive until the daemon recovers.
    if worker.in_flight.load(Ordering::SeqCst) > 0 {
        return Err(KeychainTimeout::WorkerStuck);
    }
    let (result_tx, result_rx) = std::sync::mpsc::sync_channel::<T>(1);
    let job: KeychainJob = Box::new(move || {
        let value = f();
        // Ignore send error: if the caller already timed out and dropped the
        // receiver, the result is simply discarded.
        let _ = result_tx.send(value);
    });
    if worker.tx.send(job).is_err() {
        return Err(KeychainTimeout::WorkerUnavailable);
    }
    match result_rx.recv_timeout(current_keychain_timeout()) {
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

/// ONE shared public probe: is the OS keychain backend reachable *and
/// responsive*? Writes + deletes a canary under a private namespace, bounded
/// by the same timeout primitive (a wedged Secret Service → `false`, the
/// conservative default). Replaces the three duplicated `keyring_available()`
/// test copies (installer.rs / openai_cmd.rs / vct-hub modules_api.rs) and is
/// safe to call from any crate that depends on vct-launcher-core.
pub fn keyring_probe_available() -> bool {
    run_keychain_with_timeout(|| {
        let entry = match Entry::new("vct.probe.keyring_available", "probe") {
            Ok(e) => e,
            Err(_) => return false,
        };
        if entry.set_password("canary").is_err() {
            return false;
        }
        let _ = entry.delete_credential();
        true
    })
    .unwrap_or(false)
}

pub fn set(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
    value: &str,
) -> Result<(), String> {
    #[cfg(any(test, debug_assertions))]
    if let Some(result) = for_tests::mock_set(scope, module_id, key, value) {
        return result;
    }
    // v0.2.76 (A4): own the (service, key, value) so the job closure is
    // 'static + Send, then run it on the bounded-timeout worker. The timeout
    // wraps OUTSIDE pacing/backoff — the closure IS the full retry_with_backoff
    // call (v0.2.72 P9 semantics unchanged inside).
    let service = scope.service_name(module_id);
    let key = key.to_string();
    let value = value.to_string();
    run_keychain_with_timeout(move || {
        // v0.2.72 (P9): build the Entry INSIDE the closure so `Entry::new`'s
        // D-Bus construction shares the same paced_call + backoff as the op.
        retry_with_backoff(|| Entry::new(&service, &key)?.set_password(&value))
            .map_err(|err| format!("keyring set: {}", err))
    })
    .map_err(|to| format!("keyring set: {}", to))??;
    Ok(())
}

pub fn get(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
) -> Result<Option<String>, String> {
    #[cfg(any(test, debug_assertions))]
    if let Some(result) = for_tests::mock_get(scope, module_id, key) {
        return result;
    }
    // v0.2.76 (A4): own (service, key) → 'static + Send job → bounded-timeout
    // worker. keyring::Error is not Send-safe to carry across the channel
    // uniformly, so classify NoEntry INSIDE the job and return a plain
    // Result<Option<String>, String>.
    let service = scope.service_name(module_id);
    let key = key.to_string();
    run_keychain_with_timeout(move || {
        // v0.2.72 (P9): build the Entry INSIDE the closure.
        match retry_with_backoff(|| Entry::new(&service, &key)?.get_password()) {
            Ok(v) => Ok(Some(v)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(err) => Err(format!("keyring get: {}", err)),
        }
    })
    .map_err(|to| format!("keyring get: {}", to))?
}

pub fn is_set(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
) -> Result<bool, String> {
    Ok(get(scope, module_id, key)?.is_some())
}

pub fn delete(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
) -> Result<(), String> {
    #[cfg(any(test, debug_assertions))]
    if for_tests::mock_delete(scope, module_id, key) {
        return Ok(());
    }
    // v0.2.76 (A4): own (service, key) → 'static + Send job → bounded-timeout
    // worker. NoEntry (already gone) is treated as success INSIDE the job.
    let service = scope.service_name(module_id);
    let key = key.to_string();
    run_keychain_with_timeout(move || {
        // v0.2.72 (P9): build the Entry INSIDE the closure.
        match retry_with_backoff(|| Entry::new(&service, &key)?.delete_credential()) {
            Ok(()) => Ok(()),
            Err(keyring::Error::NoEntry) => Ok(()), // already gone — treat as success
            Err(err) => Err(format!("keyring delete: {}", err)),
        }
    })
    .map_err(|to| format!("keyring delete: {}", to))?
}

/// Return a masked preview of a non-sensitive value (never for sensitive
/// secrets — those must return only presence booleans).
pub fn mask_preview(value: &str) -> String {
    let trimmed = value.chars().collect::<Vec<_>>();
    if trimmed.len() <= 8 {
        return "•".repeat(trimmed.len().max(4));
    }
    let head: String = trimmed[..4].iter().collect();
    let tail: String = trimmed[trimmed.len().saturating_sub(3)..].iter().collect();
    format!("{}•••{}", head, tail)
}

// ─── Test serialisation ──────────────────────────────────────────────────
//
// 0.1.7 H1 fork-readiness sweep (2026-05-08): multiple test modules
// (`commands::installer::github_pat_keychain_tests`,
// `commands::dashboard::tests`, `hub::modules_api::tests`) all write
// to overlapping OS-keychain slots — most prominently
// `vct._user_shared_.shared.user/github_pat` (post-2026-05-10 unification;
// pre-fix this was `installer/github_pat`), which is the canonical slot
// every github_pat consumer reads from. Running those modules' tests
// in parallel makes the keychain reads non-deterministic: test A writes
// canary X, test B writes canary Y, test A asserts and reads back B's
// canary.
//
// Each module previously used its OWN private `static SERIALIZE` mutex,
// which only solved within-module races. To close the cross-module gap
// we expose a single process-wide mutex here. Tests that touch any
// keychain slot the launcher's runtime code reads — most pressingly
// the github_pat slot — should call `tests::keychain_serialize_lock()`
// (or use the convenience `with_keychain_lock` wrapper) before any
// `secrets::set/get/delete`.
//
// Why a TEST-ONLY mutex instead of locking inside `secrets::set/get`:
// the runtime keychain ops are inherently atomic (one D-Bus / Keychain
// API call each), and adding a runtime mutex would needlessly serialise
// independent secret reads in production. The race is purely a
// test-isolation problem.
//
// v0.2.14 (2026-05-17): cross-process file-lock layer added on top of
// the in-process mutex. Reproducer: running `cargo test --lib` three
// times in parallel (e.g. from three terminals during release validation,
// or from a developer's worktree-per-agent workflow) spawns three
// separate test binaries. Each binary has its own copy of
// KEYCHAIN_SERIALIZE — so the in-process mutex serialises within one
// binary but does nothing across binaries. The OS keychain itself is
// shared per-OS-user (libsecret on Linux, Keychain on macOS, Credential
// Manager on Windows), so the three binaries trample each other's
// canaries on the `vct._user_shared_.shared.user/github_pat` slot.
//
// The fix layers an `flock(LOCK_EX)` on `/tmp/vct-keychain-test.lock`
// (Unix) / `%TEMP%\vct-keychain-test.lock` (Windows) on top of the
// in-process mutex. Two binaries running concurrently now serialise at
// the kernel level: only the holder of the file lock can run keychain
// tests; the others block on `flock` until released.
//
// Failure handling: if `flock` itself fails (extremely rare — would
// require `/tmp` to be unwritable), we degrade to in-process-only
// serialisation and log a one-line warning to stderr. Tests still run;
// they just MAY flake under cross-process parallelism on that broken
// host. We chose this over a hard panic so a misconfigured developer
// laptop doesn't break every test in the binary.

// v0.2.21 Step 3d: gated on `cfg(test)` OR `cfg(feature = "test-support")`
// so launcher's test crate (a separate compilation unit) can still
// access this module via the `test-support` feature flag declared in
// its dev-dependencies. Production builds (no features, not test
// profile) still exclude this entirely.
#[cfg(any(test, debug_assertions))]
pub mod test_serialize {
    use std::sync::{Mutex, MutexGuard};

    static KEYCHAIN_SERIALIZE: Mutex<()> = Mutex::new(());

    /// Combined guard holding BOTH the in-process mutex (drops first)
    /// and the cross-process file lock (drops second when its Drop
    /// runs after the field-ordering rule).
    ///
    /// Drop order in Rust is field-declaration order — `_proc_lock`
    /// drops first (releases the in-process mutex), then `_file_lock`
    /// (releases the kernel-level file lock). This is the correct
    /// order: we want other in-process readers to proceed BEFORE we
    /// hand the cross-process baton to a sibling test binary.
    pub struct KeychainGuard {
        _proc_lock: MutexGuard<'static, ()>,
        _file_lock: Option<file_lock::FileLock>,
    }

    /// Acquire the process-wide keychain-test mutex. Recovers from
    /// poisoning (a prior test panic mid-keychain-write leaves the
    /// mutex poisoned but the keychain itself is consistent — this
    /// matches the pattern used by `paths.rs::tests::SERIALIZE`).
    ///
    /// Hold for the duration of any test that mutates a keychain slot
    /// the launcher's runtime code reads (currently the most contended
    /// is `vct._user_shared_.shared.user/github_pat`, post-2026-05-10
    /// module_id unification — pre-fix this was the `installer/` slot).
    /// Release happens automatically on drop.
    ///
    /// Returns a [`KeychainGuard`] (not a raw `MutexGuard`) since
    /// v0.2.14 (2026-05-17) — the guard also holds a cross-process
    /// file lock so concurrent `cargo test` invocations from different
    /// terminals don't race on the OS-shared keychain slot.
    ///
    /// The return type is opaque (`KeychainGuard` only exposes Drop),
    /// so callers that previously held a `MutexGuard<'static, ()>`
    /// continue to work as long as they only relied on the Drop
    /// behaviour. The struct deliberately does NOT impl `Deref<Target=()>`
    /// because the value `()` is uninteresting; if a caller needs to
    /// pattern-match on the guard type, the field is also called `_lock`
    /// in the test modules' EnvGuard structs which take this by value.
    pub fn keychain_serialize_lock() -> KeychainGuard {
        // Acquire the cross-process file lock FIRST. If we acquired
        // the in-process mutex first and then blocked on flock(), we'd
        // hold the in-process mutex across the blocking-syscall wait,
        // pinning every other sibling-test thread in the same binary
        // for no reason. flock-first inverts that: only the thread
        // that's about to run gets the in-process mutex.
        let file_lock = file_lock::acquire();
        let proc_lock = KEYCHAIN_SERIALIZE
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        KeychainGuard {
            _proc_lock: proc_lock,
            _file_lock: file_lock,
        }
    }

    // ── Cross-process file lock (v0.2.14, 2026-05-17) ──────────────────
    //
    // Minimal flock-on-/tmp implementation. Why not pull in `fs2` or
    // `file-lock` from crates.io: both are single-purpose ~200-line
    // crates that wrap `flock(2)`/`LockFileEx`, and our launcher already
    // has `libc` as a Unix dep. The 40-line direct-libc wrapper here
    // costs less than an added crate and stays test-only.
    pub(super) mod file_lock {
        /// RAII guard for an exclusive cross-process lock on a well-known
        /// path. Dropping the guard releases the lock.
        pub struct FileLock {
            #[cfg(unix)]
            _file: std::fs::File,
            #[cfg(unix)]
            fd: std::os::unix::io::RawFd,
            #[cfg(windows)]
            _file: std::fs::File,
        }

        #[cfg(unix)]
        impl Drop for FileLock {
            fn drop(&mut self) {
                // LOCK_UN explicitly releases; the kernel also releases
                // on fd close, but explicit unlock makes the release
                // happen before the file handle's drop reordering.
                unsafe {
                    libc::flock(self.fd, libc::LOCK_UN);
                }
            }
        }

        /// Acquire the cross-process lock. Returns None on platforms
        /// or hosts where flock fails — tests then fall back to
        /// in-process-only serialisation with a logged warning.
        #[cfg(unix)]
        pub fn acquire() -> Option<FileLock> {
            use std::os::unix::io::AsRawFd;
            use std::path::PathBuf;

            let lock_path: PathBuf = std::env::temp_dir().join("vct-keychain-test.lock");
            // OpenOptions::create(true).write(true) lets every test
            // binary on this host share the same lockfile regardless
            // of who created it first. World-writable mode would be
            // a security concern on multi-user hosts, but `/tmp` is
            // already sticky+world-writable; we don't relax that.
            let file = match std::fs::OpenOptions::new()
                .create(true)
                .write(true)
                .truncate(false)
                .open(&lock_path)
            {
                Ok(f) => f,
                Err(e) => {
                    eprintln!(
                        "[vct-tests] WARN: cannot open keychain lockfile {:?}: {} \
                         (falling back to in-process-only serialisation; \
                         cross-process tests may flake)",
                        lock_path, e,
                    );
                    return None;
                }
            };
            let fd = file.as_raw_fd();
            // LOCK_EX = exclusive lock; blocks until acquired. Cargo
            // tests have no inherent wall-clock budget per test, so
            // blocking is acceptable — the alternative (LOCK_NB +
            // busy-poll) would just burn CPU.
            let rc = unsafe { libc::flock(fd, libc::LOCK_EX) };
            if rc != 0 {
                let err = std::io::Error::last_os_error();
                eprintln!(
                    "[vct-tests] WARN: flock(LOCK_EX) failed on {:?}: {} \
                     (falling back to in-process-only serialisation; \
                     cross-process tests may flake)",
                    lock_path, err,
                );
                return None;
            }
            Some(FileLock { _file: file, fd })
        }

        #[cfg(windows)]
        impl Drop for FileLock {
            fn drop(&mut self) {
                // Closing the file handle releases the Windows lock.
                // Explicit no-op here; relying on `_file`'s Drop.
            }
        }

        /// Windows variant — uses `LockFileEx`. NOTE: keychain tests
        /// are primarily run on Linux CI; on Windows the launcher
        /// uses Credential Manager which has different concurrency
        /// semantics. We still implement cross-process locking on
        /// Windows for parity, since developers running `cargo test`
        /// on Windows from multiple terminals would hit the same
        /// class of race.
        #[cfg(windows)]
        pub fn acquire() -> Option<FileLock> {
            use std::os::windows::io::AsRawHandle;
            use std::path::PathBuf;

            let lock_path: PathBuf = std::env::temp_dir().join("vct-keychain-test.lock");
            let file = match std::fs::OpenOptions::new()
                .create(true)
                .write(true)
                .truncate(false)
                .open(&lock_path)
            {
                Ok(f) => f,
                Err(e) => {
                    eprintln!(
                        "[vct-tests] WARN: cannot open keychain lockfile {:?}: {} \
                         (falling back to in-process-only serialisation)",
                        lock_path, e,
                    );
                    return None;
                }
            };
            let handle = file.as_raw_handle();
            // LockFileEx LOCKFILE_EXCLUSIVE_LOCK = 0x2.
            // Locking range: 0 to u32::MAX bytes (covers entire file).
            #[repr(C)]
            #[derive(Default)]
            struct Overlapped {
                internal: usize,
                internal_high: usize,
                offset: u32,
                offset_high: u32,
                event: *mut std::ffi::c_void,
            }
            extern "system" {
                fn LockFileEx(
                    h: *mut std::ffi::c_void,
                    flags: u32,
                    reserved: u32,
                    n_bytes_low: u32,
                    n_bytes_high: u32,
                    overlapped: *mut Overlapped,
                ) -> i32;
            }
            const LOCKFILE_EXCLUSIVE_LOCK: u32 = 0x2;
            let mut ov = Overlapped {
                event: std::ptr::null_mut(),
                ..Default::default()
            };
            let ok = unsafe {
                LockFileEx(
                    handle as *mut _,
                    LOCKFILE_EXCLUSIVE_LOCK,
                    0,
                    u32::MAX,
                    0,
                    &mut ov,
                )
            };
            if ok == 0 {
                let err = std::io::Error::last_os_error();
                eprintln!(
                    "[vct-tests] WARN: LockFileEx failed on {:?}: {} \
                     (falling back to in-process-only serialisation)",
                    lock_path, err,
                );
                return None;
            }
            Some(FileLock { _file: file })
        }
    }
}

// ─── Test mock keychain seam ──────────────────────────────────────────────
//
// v0.2.42 W5-TEST3a: a thread-local in-memory HashMap that shadows the OS
// keychain for tests. The production code path is completely unchanged —
// the `#[cfg(any(test, debug_assertions))]` guards in `get`/`set`/`delete`
// above are the only intrusion into the production functions, and they only
// divert when the thread-local mock is active (i.e., after `enable_mock()`).
//
// Gated on `cfg(any(test, debug_assertions))` (matching `test_serialize`'s
// gate) so that when `vct-launcher-core` is compiled as a debug-mode
// dependency of the main launcher test binary, the module is visible and
// the mock intercept in get/set/delete is active. Pure production release
// builds (no debug_assertions) exclude this entirely.
//
// Map key: `(service_name(module_id), key)` — identical to what the OS
// keychain uses as its (service, username) pair — so the mock faithfully
// mirrors the keychain namespace without needing to store SecretScope
// (which carries a lifetime and can't live in a static).
//
// Usage:
//   secrets::for_tests::enable_mock();
//   secrets::for_tests::clear_mock();
//   // ... call secrets::get / set / delete — all go through the map ...
//   secrets::for_tests::disable_mock();
//
// RAII pattern (preferred):
//   let _g = secrets::for_tests::MockGuard::new();

#[cfg(any(test, debug_assertions))]
pub mod for_tests {
    use super::SecretScope;
    use std::cell::RefCell;
    use std::collections::{HashMap, HashSet};

    // Thread-local store: Some(map) when mock is active, None when inactive.
    thread_local! {
        static MOCK_STORE: RefCell<Option<HashMap<(String, String), String>>> =
            const { RefCell::new(None) };

        // Keys registered to fail on the next `mock_set` call.
        // Entry is consumed (one-shot) when the failure fires.
        static MOCK_FAIL_KEYS: RefCell<HashSet<String>> =
            RefCell::new(HashSet::new());
    }

    /// Enable the thread-local mock keychain for this thread.
    ///
    /// After this call, `secrets::get/set/delete` on this thread will
    /// operate on the in-memory map instead of the OS keychain. Calling
    /// `enable_mock` when the mock is already active is a no-op (preserves
    /// any entries already in the map).
    pub fn enable_mock() {
        MOCK_STORE.with(|cell| {
            let mut slot = cell.borrow_mut();
            if slot.is_none() {
                *slot = Some(HashMap::new());
            }
        });
    }

    /// Disable the thread-local mock keychain for this thread.
    ///
    /// After this call, `secrets::get/set/delete` fall through to the OS
    /// keychain again. Safe to call when the mock isn't active. Also clears
    /// any pending fail-keys so they don't leak to subsequent mock sessions.
    pub fn disable_mock() {
        MOCK_STORE.with(|cell| {
            *cell.borrow_mut() = None;
        });
        MOCK_FAIL_KEYS.with(|cell| cell.borrow_mut().clear());
    }

    /// Empty the thread-local mock store without disabling it.
    ///
    /// Also clears any pending fail-keys registered via `fail_next_set`.
    ///
    /// Useful for resetting state between test cases that share a mock
    /// lifecycle, without the overhead of `disable_mock` + `enable_mock`.
    pub fn clear_mock() {
        MOCK_STORE.with(|cell| {
            if let Some(map) = cell.borrow_mut().as_mut() {
                map.clear();
            }
        });
        MOCK_FAIL_KEYS.with(|cell| cell.borrow_mut().clear());
    }

    /// Register `key` (keychain username) to fail on the **next** `secrets::set`
    /// call that targets it. The failure is one-shot: after firing it is
    /// removed from the fail-set. Subsequent `secrets::set` calls with the
    /// same key succeed normally.
    ///
    /// The mock must already be active (call after `enable_mock()` or inside a
    /// `MockGuard` scope). Registering a fail-key when the mock is inactive is
    /// a no-op — the `secrets::set` path that checks the fail-set is only
    /// reached when `MOCK_STORE` is `Some`.
    ///
    /// # Usage
    /// ```no_run
    /// use vct_launcher_core::secrets::{self, SecretScope};
    /// let _g = secrets::for_tests::MockGuard::new();
    /// secrets::for_tests::fail_next_set("vct.mod.username");
    /// let result = secrets::set(SecretScope::Global, "mod", "username", "val");
    /// assert!(result.is_err(), "keychain write must fail");
    /// ```
    pub fn fail_next_set(key: &str) {
        MOCK_FAIL_KEYS.with(|cell| {
            cell.borrow_mut().insert(key.to_string());
        });
    }

    /// Snapshot all entries currently in the mock store.
    ///
    /// Returns `None` when the mock isn't active. Useful for assertions
    /// that need to inspect the full state of the in-memory keychain.
    pub fn snapshot() -> Option<Vec<((String, String), String)>> {
        MOCK_STORE.with(|cell| {
            cell.borrow().as_ref().map(|map| {
                map.iter()
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect()
            })
        })
    }

    /// RAII guard: enables the mock on construction, disables+clears on drop.
    ///
    /// ```no_run
    /// use vct_launcher_core::secrets;
    /// let _g = secrets::for_tests::MockGuard::new();
    /// // mock is active for the duration of this scope
    /// ```
    pub struct MockGuard;

    impl MockGuard {
        pub fn new() -> Self {
            enable_mock();
            clear_mock();
            MockGuard
        }
    }

    impl Drop for MockGuard {
        fn drop(&mut self) {
            disable_mock();
        }
    }

    // ── Internal helpers called from secrets::get/set/delete ──────────────

    /// Called from `secrets::set` when `cfg(test)`.
    ///
    /// Returns:
    /// - `None`       — mock is inactive; caller falls through to the real keychain.
    /// - `Some(Ok(()))` — mock was active and handled the write successfully.
    /// - `Some(Err(…))` — mock was active and injected a failure (key was in
    ///   the fail-set via `fail_next_set`). The fail entry is consumed (one-shot).
    pub(super) fn mock_set(
        scope: SecretScope<'_>,
        module_id: &str,
        key: &str,
        value: &str,
    ) -> Option<Result<(), String>> {
        MOCK_STORE.with(|cell| {
            let mut slot = cell.borrow_mut();
            match slot.as_mut() {
                None => None,
                Some(map) => {
                    let service = scope.service_name(module_id);
                    // Check if this key has a registered failure (one-shot).
                    let should_fail = MOCK_FAIL_KEYS.with(|fail_cell| {
                        fail_cell.borrow_mut().remove(key)
                    });
                    if should_fail {
                        return Some(Err(format!(
                            "mock keychain set failure injected for key: {key}"
                        )));
                    }
                    map.insert((service, key.to_string()), value.to_string());
                    Some(Ok(()))
                }
            }
        })
    }

    /// Called from `secrets::get` when `cfg(test)`. Returns `Some(Ok(…))`
    /// / `Some(Ok(None))` when the mock is active; `None` when inactive
    /// (caller falls through to the real keychain).
    pub(super) fn mock_get(
        scope: SecretScope<'_>,
        module_id: &str,
        key: &str,
    ) -> Option<Result<Option<String>, String>> {
        MOCK_STORE.with(|cell| {
            let slot = cell.borrow();
            slot.as_ref().map(|map| {
                let service = scope.service_name(module_id);
                Ok(map.get(&(service, key.to_string())).cloned())
            })
        })
    }

    /// Called from `secrets::delete` when `cfg(test)`. Returns `true` if
    /// the mock was active and handled the delete (caller must return
    /// Ok(())). Returns `false` when the mock is inactive.
    pub(super) fn mock_delete(
        scope: SecretScope<'_>,
        module_id: &str,
        key: &str,
    ) -> bool {
        MOCK_STORE.with(|cell| {
            let mut slot = cell.borrow_mut();
            match slot.as_mut() {
                None => false,
                Some(map) => {
                    let service = scope.service_name(module_id);
                    map.remove(&(service, key.to_string()));
                    true
                }
            }
        })
    }
}

// ─── Tests ───────────────────────────────────────────────────────────────
//
// Pure-function tests for keychain plumbing. The previous Fix #3 test
// module (`bridge_tests::*`) was removed alongside the bridge — those
// tests pinned behaviour that no longer exists.
//
// Keychain-touching round-trip tests live in `commands::secrets_cmd::tests`
// and `hub::modules_api::tests`; replicating them here would just
// duplicate the same `keyring_available()` short-circuit.

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn service_name_per_project_includes_project_id() {
        let scope = SecretScope::PerProject { project_id: "p1" };
        assert_eq!(scope.service_name("mod"), "vct.p1.mod");
    }

    #[test]
    fn service_name_global_uses_global_segment() {
        let scope = SecretScope::Global;
        assert_eq!(scope.service_name("mod"), "vct.global.mod");
    }

    #[test]
    fn service_name_shared_includes_shared_segment() {
        let scope = SecretScope::Shared { project_id: "p1" };
        assert_eq!(scope.service_name("mod"), "vct.p1.shared.mod");
    }

    #[test]
    fn mask_preview_short_value_is_fully_masked() {
        assert_eq!(mask_preview(""), "••••");
        assert_eq!(mask_preview("abc"), "••••");
        assert_eq!(mask_preview("12345678"), "••••••••");
    }

    #[test]
    fn mask_preview_long_value_keeps_head_and_tail() {
        let masked = mask_preview("ghp_1234567890abcdef");
        // 4 head chars + bullets + 3 tail chars
        assert!(masked.starts_with("ghp_"));
        assert!(masked.ends_with("def"));
        assert!(masked.contains("•••"));
        // Raw value must not appear verbatim.
        assert!(!masked.contains("ghp_1234567890abcdef"));
    }

    // ─── Retry / rate-limit pins (2026-05-13 upgrade) ─────────────────────
    //
    // Pin the retry + rate-limit contract independently of any real
    // keychain backend. Tests drive `retry_with_backoff` with a counter-
    // backed closure and use `TestSpacingGuard` to compress the 150ms
    // production spacing to a near-zero value so test wall-clock cost
    // stays bounded.

    /// `is_transient` only fires for `PlatformFailure` — the exact error
    /// class gnome-keyring returns when the daemon is overloaded /
    /// mid-respawn. Permanent errors (`NoEntry`, `BadEncoding`, …)
    /// must NOT be classified as transient or every NoEntry lookup
    /// would pay 1+ seconds of retry penalty.
    #[test]
    fn is_transient_only_matches_platform_failure() {
        let boxed: Box<dyn std::error::Error + Send + Sync> = "test daemon hiccup".into();
        assert!(is_transient(&keyring::Error::PlatformFailure(boxed)));
        assert!(!is_transient(&keyring::Error::NoEntry));
        assert!(!is_transient(&keyring::Error::BadEncoding(vec![0xff, 0xfe])));
        assert!(!is_transient(&keyring::Error::TooLong(
            "service".into(),
            255,
        )));
        assert!(!is_transient(&keyring::Error::Invalid(
            "service".into(),
            "empty".into(),
        )));
        let boxed_nsa: Box<dyn std::error::Error + Send + Sync> = "store locked".into();
        assert!(!is_transient(&keyring::Error::NoStorageAccess(boxed_nsa)));
    }

    /// `retry_with_backoff`: an Ok on the first attempt is returned
    /// without invoking the closure a second time.
    #[test]
    fn retry_with_backoff_returns_first_ok_without_retry() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _g = TestSpacingGuard::new(std::time::Duration::from_millis(0));
        let mut calls = 0;
        let result: keyring::Result<i32> = retry_with_backoff(|| {
            calls += 1;
            Ok(42)
        });
        assert_eq!(result.unwrap(), 42);
        assert_eq!(calls, 1, "Ok on first attempt must not retry");
    }

    /// `retry_with_backoff`: recovers from a SINGLE transient error on
    /// the first attempt. Returns on the second attempt's Ok.
    #[test]
    fn retry_with_backoff_recovers_from_one_transient() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _g = TestSpacingGuard::new(std::time::Duration::from_millis(0));
        let mut calls = 0;
        let result: keyring::Result<&'static str> = retry_with_backoff(|| {
            calls += 1;
            if calls == 1 {
                let boxed: Box<dyn std::error::Error + Send + Sync> = "first hiccup".into();
                Err(keyring::Error::PlatformFailure(boxed))
            } else {
                Ok("recovered")
            }
        });
        assert_eq!(result.unwrap(), "recovered");
        assert_eq!(calls, 2, "one transient → one retry → Ok");
    }

    /// `retry_with_backoff`: recovers from THREE consecutive transient
    /// errors on the fourth attempt. This is the worst-case happy path
    /// the 2026-05-13 upgrade exists to handle — a SIGTRAP'd daemon
    /// that takes the full backoff schedule (50+250+1000ms) to respawn.
    #[test]
    fn retry_with_backoff_recovers_from_three_transients() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _g = TestSpacingGuard::new(std::time::Duration::from_millis(0));
        let mut calls = 0;
        let result: keyring::Result<&'static str> = retry_with_backoff(|| {
            calls += 1;
            if calls <= 3 {
                let boxed: Box<dyn std::error::Error + Send + Sync> = "hiccup".into();
                Err(keyring::Error::PlatformFailure(boxed))
            } else {
                Ok("recovered")
            }
        });
        assert_eq!(result.unwrap(), "recovered");
        assert_eq!(calls, 4, "three transients + one Ok = 4 attempts");
    }

    /// `retry_with_backoff`: FOUR consecutive transients propagates the
    /// last error. Caps the attempt budget — no infinite retry loop.
    /// 4 attempts = 1 initial + len(BACKOFF_SCHEDULE) retries.
    #[test]
    fn retry_with_backoff_caps_at_attempt_budget() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _g = TestSpacingGuard::new(std::time::Duration::from_millis(0));
        let mut calls = 0;
        let result: keyring::Result<()> = retry_with_backoff(|| {
            calls += 1;
            let msg = format!("hiccup #{}", calls);
            let boxed: Box<dyn std::error::Error + Send + Sync> = msg.into();
            Err(keyring::Error::PlatformFailure(boxed))
        });
        assert!(matches!(result, Err(keyring::Error::PlatformFailure(_))));
        assert_eq!(
            calls,
            BACKOFF_SCHEDULE.len() + 1,
            "exactly {} attempts on persistent transient failure",
            BACKOFF_SCHEDULE.len() + 1
        );
    }

    /// Permanent errors take exactly ONE attempt — no backoff cost.
    /// `NoEntry` is the dominant outcome for `is_set(unwritten_key)`;
    /// retrying any of these would burn wall-clock for no benefit.
    #[test]
    fn retry_with_backoff_does_not_retry_permanent_error() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _g = TestSpacingGuard::new(std::time::Duration::from_millis(0));
        // The lock + 0ms override together prevent any prior test's
        // LAST_KEYRING_CALL from causing pacing delay here — current
        // spacing is 0ms, so paced_call returns immediately regardless
        // of how recent the prior call was.
        let mut calls = 0;
        let start = std::time::Instant::now();
        let result: keyring::Result<i32> = retry_with_backoff(|| {
            calls += 1;
            Err(keyring::Error::NoEntry)
        });
        let elapsed = start.elapsed();
        assert!(matches!(result, Err(keyring::Error::NoEntry)));
        assert_eq!(calls, 1, "permanent error must not retry");
        // Generous bound — even on a slow CI runner a single closure
        // call + paced_call mutex acquisition should complete in <100ms.
        assert!(
            elapsed < std::time::Duration::from_millis(100),
            "permanent-error path must not pay the backoff cost; got {:?}",
            elapsed
        );
    }

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

    /// Backoff schedule shape is fixed: 3 entries, monotonically
    /// increasing, last entry ≥1s. Catches a regression where someone
    /// shortens the tail and the daemon-respawn case stops being covered.
    #[test]
    fn backoff_schedule_shape_is_pinned() {
        assert_eq!(BACKOFF_SCHEDULE.len(), 3);
        for w in BACKOFF_SCHEDULE.windows(2) {
            assert!(w[0] < w[1], "backoff must be monotonically increasing");
        }
        assert!(
            BACKOFF_SCHEDULE[BACKOFF_SCHEDULE.len() - 1]
                >= std::time::Duration::from_millis(1000),
            "tail must be ≥1s to cover gnome-keyring SIGTRAP respawn"
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

    // ─── Mock keychain seam tests (v0.2.42 W5-TEST3a) ─────────────────────
    //
    // These tests pin the in-memory mock layer WITHOUT touching the OS
    // keychain. They are the ground-truth for `for_tests::enable_mock` /
    // `disable_mock` / `clear_mock` / `snapshot` contract.

    /// get-after-set returns the stored value.
    #[test]
    fn mock_get_after_set_returns_value() {
        let _g = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        set(scope, "mod", "k1", "secret_value").unwrap();
        let v = get(scope, "mod", "k1").unwrap();
        assert_eq!(v.as_deref(), Some("secret_value"));
    }

    /// delete removes the entry; subsequent get returns None.
    #[test]
    fn mock_delete_removes_entry() {
        let _g = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        set(scope, "mod", "k2", "to_delete").unwrap();
        delete(scope, "mod", "k2").unwrap();
        let v = get(scope, "mod", "k2").unwrap();
        assert!(v.is_none(), "entry must be gone after delete");
    }

    /// disable_mock causes get/set/delete to fall through (no longer
    /// intercepted). Verified by checking that `snapshot()` returns None
    /// after disable — the mock map is gone.
    #[test]
    fn mock_disabled_snapshot_returns_none() {
        for_tests::enable_mock();
        for_tests::clear_mock();
        let snap = for_tests::snapshot();
        assert!(snap.is_some(), "mock active → snapshot Some");
        for_tests::disable_mock();
        let snap = for_tests::snapshot();
        assert!(snap.is_none(), "mock inactive → snapshot None");
    }

    /// clear_mock empties the map without disabling it.
    #[test]
    fn mock_clear_resets_entries_while_keeping_mock_active() {
        let _g = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        set(scope, "mod", "k3", "v3").unwrap();
        assert!(get(scope, "mod", "k3").unwrap().is_some());
        for_tests::clear_mock();
        assert!(
            get(scope, "mod", "k3").unwrap().is_none(),
            "entry must be gone after clear"
        );
        // Mock still active — snapshot is Some([]).
        assert_eq!(for_tests::snapshot().unwrap().len(), 0);
    }

    /// PerProject and Global scopes store entries independently — the
    /// service_name differs, so no cross-scope collision.
    #[test]
    fn mock_scope_isolation_per_project_vs_global() {
        let _g = for_tests::MockGuard::new();
        let per = SecretScope::PerProject { project_id: "proj_iso" };
        let global = SecretScope::Global;
        set(per, "mod", "key", "per_val").unwrap();
        set(global, "mod", "key", "global_val").unwrap();
        assert_eq!(get(per, "mod", "key").unwrap().as_deref(), Some("per_val"));
        assert_eq!(
            get(global, "mod", "key").unwrap().as_deref(),
            Some("global_val")
        );
    }

    /// snapshot returns all entries currently in the mock store.
    #[test]
    fn mock_snapshot_enumerates_all_entries() {
        let _g = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        set(scope, "m", "a", "1").unwrap();
        set(scope, "m", "b", "2").unwrap();
        let snap = for_tests::snapshot().expect("mock active");
        assert_eq!(snap.len(), 2);
    }

    // ─── fail_next_set tests (v0.2.42 D2) ─────────────────────────────────

    /// fail_next_set causes the next secrets::set for that key to return Err.
    /// The entry is one-shot: a subsequent set for the same key succeeds.
    #[test]
    fn mock_fail_next_set_is_one_shot() {
        let _g = for_tests::MockGuard::new();
        let scope = SecretScope::Global;

        for_tests::fail_next_set("target_key");

        // First call fails (failure consumed).
        let first = set(scope, "mod", "target_key", "val1");
        assert!(first.is_err(), "first set must fail after fail_next_set");

        // Second call succeeds (fail-set is now empty for this key).
        let second = set(scope, "mod", "target_key", "val2");
        assert!(second.is_ok(), "second set must succeed (one-shot exhausted)");

        // Entry was written by the successful second call.
        assert_eq!(
            get(scope, "mod", "target_key").unwrap().as_deref(),
            Some("val2")
        );
    }

    /// fail_next_set is key-scoped: only the targeted key fails; other keys
    /// in the same mock session are unaffected.
    #[test]
    fn mock_fail_next_set_scoped_to_targeted_key() {
        let _g = for_tests::MockGuard::new();
        let scope = SecretScope::Global;

        for_tests::fail_next_set("fail_key");

        // Unregistered key succeeds normally.
        set(scope, "mod", "ok_key", "ok_val").unwrap();
        assert_eq!(
            get(scope, "mod", "ok_key").unwrap().as_deref(),
            Some("ok_val"),
            "ok_key must not be affected by fail registered for fail_key"
        );

        // Registered key still fails (not yet consumed).
        let result = set(scope, "mod", "fail_key", "val");
        assert!(result.is_err(), "fail_key must fail as registered");

        // Failure doesn't write to the store.
        assert!(
            get(scope, "mod", "fail_key").unwrap().is_none(),
            "failed write must not leave a stale entry"
        );
    }

    // ─── P9: Entry::new() D-Bus construction is paced (v0.2.72) ───────────
    //
    // Before P9, `set`/`get`/`delete` did `let e = entry(...)?;` OUTSIDE
    // the pacing layer, then only paced the op. A burst of N ops fired N
    // UNPACED `Entry::new()` D-Bus calls → crashed gnome-keyring 46.1 on
    // Ubuntu 24.04. The fix builds the Entry INSIDE the `retry_with_backoff`
    // closure so construction AND op share one `paced_call`. The real
    // keychain can't be hit deterministically in unit tests, so these pin
    // the STRUCTURAL property via a counting seam that mirrors the
    // production hot-path shape (construct-then-op inside the paced
    // closure), matching the pattern of `retry_with_backoff_caps_at_*`.

    /// A burst of N hot-path calls (each = construct-then-op inside ONE
    /// paced closure) incurs exactly N `paced_call` gates, and the
    /// min-spacing contract holds across the construction+op pair. If
    /// construction were unpaced (pre-P9), the wall-clock floor below
    /// could be met with fewer gates; here we prove every closure
    /// invocation — which now wraps BOTH the D-Bus construction and the
    /// op — pays the spacing.
    #[test]
    fn p9_burst_paces_construction_and_op_together() {
        let _lock = test_serialize::keychain_serialize_lock();
        let spacing = std::time::Duration::from_millis(20);
        let _g = TestSpacingGuard::new(spacing);

        // Model the post-P9 hot-path closure: construction (a D-Bus call)
        // followed by the op (a second D-Bus call), BOTH inside the single
        // closure that `retry_with_backoff` drives through `paced_call`.
        let construct_calls = std::cell::Cell::new(0usize);
        let op_calls = std::cell::Cell::new(0usize);
        let hot_path = || -> keyring::Result<()> {
            // construction seam (stands in for `entry_result(...)?`)
            construct_calls.set(construct_calls.get() + 1);
            // op seam (stands in for e.set_password / get_password / …)
            op_calls.set(op_calls.get() + 1);
            Ok(())
        };

        // Establish the pacing baseline, then time a burst of 4.
        let n = 4usize;
        paced_call(|| 0); // burn first slot so the first timed call pays spacing
        let start = std::time::Instant::now();
        for _ in 0..n {
            retry_with_backoff(hot_path).unwrap();
        }
        let elapsed = start.elapsed();

        // Construction and op are invoked once per successful call, in
        // lock-step — proving they live inside the SAME closure (if
        // construction had stayed outside, op_calls could diverge under
        // retry).
        assert_eq!(construct_calls.get(), n, "one construction per call");
        assert_eq!(op_calls.get(), n, "one op per call");
        assert_eq!(
            construct_calls.get(),
            op_calls.get(),
            "construction and op must be paced together (same closure)"
        );

        // N paced closure invocations after the burned baseline means at
        // least N spacing gaps of `spacing` each. Allow 5ms/gate jitter
        // slack on slow CI.
        let min_expected = spacing
            .checked_mul(n as u32)
            .unwrap()
            .saturating_sub(std::time::Duration::from_millis(5 * n as u64));
        assert!(
            elapsed >= min_expected,
            "burst of {} paced closures must take ≥{:?}; got {:?}",
            n,
            min_expected,
            elapsed
        );
    }

    /// A construction error inside the hot-path closure is a retryable
    /// `keyring::Error` — the `?` on `entry_result(...)?` propagates it as
    /// the closure's return, so a TRANSIENT construction failure (the
    /// D-Bus negotiation hiccuping mid-respawn) rides the same backoff as
    /// a transient op failure. Pins that the closure signature
    /// (`FnMut() -> keyring::Result<T>`) carries construction errors and
    /// that `Entry::new`-shaped `PlatformFailure` is retried, not dropped.
    #[test]
    fn p9_transient_construction_error_is_retried() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _g = TestSpacingGuard::new(std::time::Duration::from_millis(0));

        let mut attempts = 0;
        // Mirror the hot path: the construction step fails transiently on
        // the first attempt (as `entry_result(...)?` would), succeeds
        // after, then the op runs.
        let result: keyring::Result<&'static str> = retry_with_backoff(|| {
            attempts += 1;
            if attempts == 1 {
                // construction hiccup — same class gnome-keyring returns
                let boxed: Box<dyn std::error::Error + Send + Sync> =
                    "Entry::new D-Bus negotiation hiccup".into();
                return Err(keyring::Error::PlatformFailure(boxed));
            }
            // construction succeeded → op runs
            Ok("stored")
        });

        assert_eq!(result.unwrap(), "stored");
        assert_eq!(
            attempts, 2,
            "transient construction error must be retried, then op runs"
        );
    }

    /// A PERMANENT construction error (e.g. `Invalid` service name) takes
    /// exactly one attempt — it is not retried, matching how a permanent
    /// op error behaves. Pins that routing construction through the retry
    /// layer does NOT accidentally start retrying permanent shape errors.
    #[test]
    fn p9_permanent_construction_error_not_retried() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _g = TestSpacingGuard::new(std::time::Duration::from_millis(0));

        let mut attempts = 0;
        let result: keyring::Result<()> = retry_with_backoff(|| {
            attempts += 1;
            Err(keyring::Error::Invalid(
                "service".into(),
                "empty".into(),
            ))
        });

        assert!(matches!(result, Err(keyring::Error::Invalid(_, _))));
        assert_eq!(attempts, 1, "permanent construction error must not retry");
    }

    // ─── v0.2.76 (A4): bounded-timeout worker primitive ───────────────────

    /// Normal op passes through unchanged: a fast closure completes and its
    /// value is returned (leave-alone case).
    #[test]
    fn keychain_timeout_passes_through_fast_op() {
        let _lock = test_serialize::keychain_serialize_lock();
        let out = run_keychain_with_timeout(|| 40 + 2);
        assert!(matches!(out, Ok(42)), "fast op must pass through: {:?}", out);
    }

    /// ACT: a deliberately-blocking op times out within the (shortened) bound
    /// and returns `TimedOut`; the NEXT op fast-fails `WorkerStuck` while the
    /// prior job is still parked on the worker. Then the blocking op is
    /// released so the worker drains (no cross-test pollution).
    #[test]
    fn keychain_timeout_blocks_then_worker_stuck_then_recovers() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _tg = TestKeychainTimeoutGuard::new(std::time::Duration::from_millis(100));

        // The blocking op parks on this channel until the test releases it.
        let (release_tx, release_rx) = std::sync::mpsc::channel::<()>();
        let release_rx = std::sync::Mutex::new(release_rx);

        let blocked = run_keychain_with_timeout(move || {
            // Block until released — models a wedged Secret Service call.
            let _ = release_rx.lock().unwrap().recv();
            7
        });
        assert!(
            matches!(blocked, Err(KeychainTimeout::TimedOut)),
            "blocking op must time out: {:?}",
            blocked
        );

        // The worker is still parked on the prior job → the next op fast-fails.
        let stuck = run_keychain_with_timeout(|| 1);
        assert!(
            matches!(stuck, Err(KeychainTimeout::WorkerStuck)),
            "a queued op must fast-fail while the worker is stuck: {:?}",
            stuck
        );

        // Release the parked job; the worker drains and recovers.
        release_tx.send(()).unwrap();
        // Poll until the worker is idle again (in_flight back to 0), then a
        // fresh op must pass through. Bounded loop so a regression can't hang.
        let mut recovered = false;
        for _ in 0..200 {
            std::thread::sleep(std::time::Duration::from_millis(10));
            if let Ok(v) = run_keychain_with_timeout(|| 99) {
                assert_eq!(v, 99);
                recovered = true;
                break;
            }
        }
        assert!(recovered, "worker must recover after the wedged op drains");
    }

    /// The shared probe returns `false` (conservative default) when the
    /// underlying op would exceed the bound — proven here by driving the
    /// timeout primitive with a blocking closure under a short bound. (The
    /// real `keyring_probe_available` uses the same primitive; we don't touch
    /// the OS keychain in unit tests.)
    #[test]
    fn keychain_probe_timeout_is_false() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _tg = TestKeychainTimeoutGuard::new(std::time::Duration::from_millis(80));

        let (release_tx, release_rx) = std::sync::mpsc::channel::<()>();
        let release_rx = std::sync::Mutex::new(release_rx);

        // Same shape as keyring_probe_available's body: op → bool, timeout → false.
        let probe = run_keychain_with_timeout(move || {
            let _ = release_rx.lock().unwrap().recv();
            true
        })
        .unwrap_or(false);
        assert!(!probe, "a probe that exceeds the bound must be false");

        // Drain so the worker recovers for later tests.
        release_tx.send(()).unwrap();
        for _ in 0..200 {
            std::thread::sleep(std::time::Duration::from_millis(10));
            if run_keychain_with_timeout(|| ()).is_ok() {
                break;
            }
        }
    }

    /// P9 STRUCTURAL GUARD (v0.2.72 pre-gate audit F4; v0.2.76 A4 reshaped).
    /// The pacing tests above drive `retry_with_backoff` with STAND-IN
    /// closures, so reverting the P9 fix in the production fns (hoisting the
    /// Entry construction back OUT of the retried closure) would keep them
    /// green. This test pins the SHIPPED source shape instead: every
    /// Entry-construction call site in `set`/`get`/`delete` must appear
    /// INSIDE a `retry_with_backoff` closure.
    ///
    /// v0.2.76 A4 changed the spelling: `set`/`get`/`delete` now own the
    /// service string and build `Entry::new(&service, &key)?` directly inside
    /// the retried closure (the whole `retry_with_backoff(...)` is itself run
    /// on the bounded-timeout worker). The invariant pinned is unchanged —
    /// "construction lives INSIDE the paced+retried closure" — only the needle
    /// tracks the new spelling. Whitespace is stripped so rustfmt reflows
    /// can't break the match; the needle is assembled from SPLIT literals so
    /// this test's own source can never match it.
    #[test]
    fn p9_source_shape_entry_construction_inside_retried_closure() {
        let src = include_str!("secrets.rs");
        let flat: String = src.chars().filter(|c| !c.is_whitespace()).collect();

        // The retried closure constructs the Entry as its first act, one per
        // hot-path fn. Needle assembled from SPLIT literals (never contiguous
        // in this test's own source) so the whitespace-stripped scan below
        // cannot match THIS line.
        let paced_needle =
            String::from("retry_with_backoff(||Entry::new(&service,") + "&key)?";

        let paced = flat.matches(paced_needle.as_str()).count();
        assert_eq!(
            paced, 3,
            "set/get/delete must each construct the keyring Entry INSIDE \
             the retry_with_backoff closure (found {paced} paced \
             construction sites, expected 3) — an unpaced `Entry::new` burst \
             is the exact gnome-keyring crash P9 fixed"
        );
    }
}
