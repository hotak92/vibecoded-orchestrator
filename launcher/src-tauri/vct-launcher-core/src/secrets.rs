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

use crate::secret_value_shape;
// `keyring::Entry` remains the keychain primitive on Windows / macOS (their
// native backends do not exhibit the connect-per-op daemon fragility the Linux
// arm mitigates). On Linux the hot-path builds a `KeychainEntry` (below) that
// routes through the process-wide persistent Secret-Service connection instead.
#[cfg(not(target_os = "linux"))]
use keyring::Entry;

const SERVICE_PREFIX: &str = "vct";

// ─── Keychain primitive abstraction (v0.3.0 WP-K) ─────────────────────────────
//
// The `set`/`get`/`delete` hot paths construct a `KeychainEntry` INSIDE the
// `retry_with_backoff` closure (the P9 invariant: construction shares the same
// paced_call + backoff as the op). `KeychainEntry` has one job — present the
// exact `new(&service, &key)? -> { set_password / get_password /
// delete_credential }` shape both platform arms need, so the closure spelling
// (and the P9 structural pin) is identical everywhere:
//
//   * Linux  → delegates to `secrets_ss_connection`, which reuses ONE
//              process-wide D-Bus Secret-Service session for get/set/delete AND
//              the Background lock probe (K-2), so connect-per-op churn is cut,
//              not merely halved. Errors are mapped to `keyring::Error` so the
//              caller's transient/permanent classification (`is_transient`),
//              retry loop, and `NoEntry` handling are byte-identical to the
//              pre-persistent-connection path.
//   * others → a thin newtype over `keyring::Entry` (unchanged behaviour).
//
// The methods return `keyring::Result<…>` so `retry_with_backoff`'s
// `FnMut() -> keyring::Result<T>` closure signature is unchanged on both arms.
// The raw keychain write primitive `.set_password(` therefore still appears
// ONLY in this file (the Linux D-Bus writer in `secrets_ss_connection` uses the
// dbus `Item::set_secret`, a different needle), preserving the A4 chokepoint
// guard.

/// A single keychain entry handle for one `(service, key)` pair. See the module
/// section comment above for the two platform arms.
struct KeychainEntry {
    #[cfg(target_os = "linux")]
    service: String,
    #[cfg(target_os = "linux")]
    key: String,
    #[cfg(not(target_os = "linux"))]
    inner: Entry,
}

/// Map a persistent-connection op error to the `keyring::Error` the caller's
/// retry/classification layer already understands. A transient failure becomes
/// `PlatformFailure` (retried by `retry_with_backoff`); a permanent failure
/// becomes `NoStorageAccess` (NON-transient — never retried, so a locked /
/// prompt-dismissed store can't re-pop the dialog via a retry, exactly as the
/// keyring path behaves). `NoEntry` maps to `keyring::Error::NoEntry` so the
/// get/delete `NoEntry` arms fire unchanged.
#[cfg(target_os = "linux")]
fn ss_error_to_keyring(err: crate::secrets_ss_connection::SsOpError) -> keyring::Error {
    use crate::secrets_ss_connection::SsOpError;
    match err {
        SsOpError::NoEntry => keyring::Error::NoEntry,
        SsOpError::Transient(_) => keyring::Error::PlatformFailure(Box::new(err)),
        SsOpError::Permanent(_) => keyring::Error::NoStorageAccess(Box::new(err)),
    }
}

impl KeychainEntry {
    /// Construct a handle for `(service, key)`. On Linux this is cheap (it just
    /// owns the two strings — the D-Bus session is the shared persistent one,
    /// touched only when an op runs); on other platforms it builds a
    /// `keyring::Entry` exactly as before.
    #[cfg(target_os = "linux")]
    fn new(service: &str, key: &str) -> keyring::Result<Self> {
        Ok(Self {
            service: service.to_string(),
            key: key.to_string(),
        })
    }

    #[cfg(not(target_os = "linux"))]
    fn new(service: &str, key: &str) -> keyring::Result<Self> {
        Ok(Self {
            inner: Entry::new(service, key)?,
        })
    }

    #[cfg(target_os = "linux")]
    fn set_password(&self, value: &str) -> keyring::Result<()> {
        crate::secrets_ss_connection::write_secret(&self.service, &self.key, value)
            .map_err(ss_error_to_keyring)
    }

    #[cfg(not(target_os = "linux"))]
    fn set_password(&self, value: &str) -> keyring::Result<()> {
        self.inner.set_password(value)
    }

    #[cfg(target_os = "linux")]
    fn get_password(&self) -> keyring::Result<String> {
        crate::secrets_ss_connection::read_secret(&self.service, &self.key)
            .map_err(ss_error_to_keyring)
    }

    #[cfg(not(target_os = "linux"))]
    fn get_password(&self) -> keyring::Result<String> {
        self.inner.get_password()
    }

    #[cfg(target_os = "linux")]
    fn delete_credential(&self) -> keyring::Result<()> {
        crate::secrets_ss_connection::remove_secret(&self.service, &self.key)
            .map_err(ss_error_to_keyring)
    }

    #[cfg(not(target_os = "linux"))]
    fn delete_credential(&self) -> keyring::Result<()> {
        self.inner.delete_credential()
    }
}

/// Best-effort graceful close of the persistent keychain connection. On Linux
/// this drains the process-wide Secret-Service session (one clean disconnect at
/// a controlled moment, instead of an abrupt teardown mid-op). No-op elsewhere.
/// Called from the hub shutdown path and the launcher exit event.
///
/// Exit is BOUNDED, not unconditional: `secrets_ss_connection::shutdown` uses a
/// `try_lock` with a short (≤250ms) deadline, so if the keychain worker is
/// mid-op — including parked on an unbounded user unlock prompt — the drain is
/// SKIPPED and the process teardown closes the socket abruptly (today's
/// pre-persistent-connection behaviour). It therefore never stalls exit waiting
/// on a prompt. No-op on Windows / macOS.
pub fn shutdown_keychain_connection() {
    #[cfg(target_os = "linux")]
    crate::secrets_ss_connection::shutdown();
}

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

// ─── Per-request secret-read session (v0.2.84 D8.2, memory-only) ─────────────
//
// The P7 gnome-keyring SIGTRAP reproduced under `install-bundle --update` +
// `Update all projects`, where the hub `/env` route resolves EVERY active
// secret key ONCE PER PROJECT — so a shared key like `github_pat` is read from
// the daemon N times per update-all (once per project), all back-to-back. A
// per-REQUEST read-through memo collapses the module/bundled/user loops'
// overlapping keys down to ONE daemon read per distinct key per request,
// cutting the daemon traffic that drove the crash.
//
// Hard invariants (pinned by `session_is_memory_only_no_persistence`):
//   * MEMORY-ONLY. The memo lives in a thread-local `SessionState`; it is
//     NEVER written to any file, NEVER stored in a `static` beyond the session
//     object, and NEVER survives the request. A persistent or cross-run secret
//     cache is FORBIDDEN — a stale secret served after a rotation is a
//     correctness AND security defect.
//   * WRITE-THROUGH HONEST. A module-level generation counter is bumped by
//     every successful `set`/`delete`. Each memo entry records the generation
//     it was cached at; a lookup is a HIT only when the recorded generation
//     still equals the current one. So a `set`/`delete` anywhere (this session
//     or another thread) invalidates the memo — a subsequent read re-hits the
//     keychain and sees the new value.
//   * REQUEST-SCOPED. `SecretReadSession` is RAII: `new()` installs a fresh
//     empty memo (saving any outer one for nesting); `Drop` restores the outer
//     value, dropping this session's memo. Outside any session, reads behave
//     exactly as pre-v0.2.84 (no memo consulted, no entry recorded).
//   * ERRORS ARE NOT MEMOIZED. Only a successful `Ok(Some)` / `Ok(None)` is
//     cached. A transient keychain `Err` must stay retryable and must keep
//     flipping the hub's honest `keychain_degraded` flag on every occurrence.
//   * LOCK POSTURE UNCHANGED. The memo is consulted AFTER the Background
//     lock-probe gate (G5/T18 posture byte-identical): a locked Background call
//     still short-circuits to `KeychainError::Locked` and never serves a cached
//     value. The memo only skips the Entry construction + daemon round-trip on
//     an already-successful read within the same request.

/// Process-wide monotonic generation counter. Bumped by every successful
/// keychain `set`/`delete`. `SecretReadSession` memo entries are tagged with
/// the generation at which they were cached and are only served while that tag
/// still matches — so any write/delete transparently invalidates the memo
/// (write-through honesty). Never reset; wraps far beyond any realistic run.
static SECRET_GENERATION: std::sync::atomic::AtomicU64 =
    std::sync::atomic::AtomicU64::new(0);

/// Read the current secret generation (see [`SECRET_GENERATION`]).
#[inline]
fn current_secret_generation() -> u64 {
    SECRET_GENERATION.load(std::sync::atomic::Ordering::SeqCst)
}

/// Bump the secret generation, invalidating every outstanding memo entry.
/// Called from the `set`/`delete` chokepoints on a SUCCESSFUL write/delete
/// (both the real keychain path and the test mock path).
#[inline]
fn bump_secret_generation() {
    SECRET_GENERATION.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
}

/// A single cached read outcome: the resolved value (`Some`) or a genuine
/// key-not-present miss (`None`), tagged with the generation at which it was
/// captured. Errors are never cached, so only successful outcomes appear here.
#[derive(Clone)]
struct MemoEntry {
    /// `Some(value)` for a resolved secret; `None` for a genuine miss.
    value: Option<String>,
    /// Generation counter value at capture time; the entry is a valid hit
    /// only while this equals [`current_secret_generation`].
    gen: u64,
}

/// The per-session read-through memo. Keyed by `(service_name, key)` — the same
/// (scope, module_id) → service string + key tuple that uniquely identifies a
/// keychain row.
#[derive(Default)]
struct SessionState {
    memo: std::collections::HashMap<(String, String), MemoEntry>,
}

thread_local! {
    /// The ambient session for the current thread, if any. `Some` for the
    /// lifetime of a [`SecretReadSession`] guard; `None` otherwise (reads then
    /// behave exactly as pre-v0.2.84). Held in a `RefCell` so the read hot-path
    /// can both consult and populate it. NEVER a global `static` value store —
    /// the whole point is that it is torn down per request.
    static SECRET_SESSION: std::cell::RefCell<Option<SessionState>> =
        const { std::cell::RefCell::new(None) };
}

/// RAII per-request secret-read session. While alive, every
/// [`get_with_context`] call on this thread consults (and populates) a
/// memory-only read-through memo, so repeated reads of the same
/// `(scope, module_id, key)` within one request hit the OS keychain exactly
/// ONCE. Dropping the guard tears the memo down (nothing persists).
///
/// Nesting is supported: a `new()` saves any outer session and restores it on
/// `Drop`, so an inner scope gets a fresh memo without corrupting the outer
/// one. The hub creates exactly one per `/env` request; there is no other
/// caller today.
///
/// The guard is intentionally `!Send` (it borrows a thread-local): a session
/// is valid only for a contiguous synchronous read sequence on one thread,
/// which is exactly how the hub `/env` handler uses it (no `.await` between the
/// resolution loops). Do NOT hold one across an `.await`.
#[must_use = "a SecretReadSession only memoizes reads while it is alive"]
pub struct SecretReadSession {
    /// The outer session (if any) to restore on drop — supports nesting.
    prev: Option<SessionState>,
    /// A `SecretReadSession` must not cross threads (it borrows a thread-local).
    _not_send: std::marker::PhantomData<*const ()>,
}

impl SecretReadSession {
    /// Begin a fresh memory-only read session on the current thread. Any
    /// existing session is saved and restored on drop (nesting).
    pub fn new() -> Self {
        let prev = SECRET_SESSION.with(|cell| {
            cell.borrow_mut().replace(SessionState::default())
        });
        Self {
            prev,
            _not_send: std::marker::PhantomData,
        }
    }

    /// Test-only: how many distinct `(service, key)` entries this thread's
    /// active session has memoized. `None` when no session is active.
    #[cfg(any(test, debug_assertions))]
    pub fn memoized_len() -> Option<usize> {
        SECRET_SESSION.with(|cell| cell.borrow().as_ref().map(|s| s.memo.len()))
    }
}

impl Default for SecretReadSession {
    fn default() -> Self {
        Self::new()
    }
}

impl Drop for SecretReadSession {
    fn drop(&mut self) {
        // Restore the outer session (or clear to None). This DROPS this
        // session's memo — the memory-only guarantee: nothing survives the
        // request.
        SECRET_SESSION.with(|cell| {
            *cell.borrow_mut() = self.prev.take();
        });
    }
}

/// Look up `(service, key)` in the active session's memo, if any. Returns
/// `Some(value)` only for a still-current entry (generation matches). A missing
/// entry, a stale entry (generation bumped by a `set`/`delete`), or no active
/// session all yield `None` → the caller performs a real keychain read.
fn session_memo_lookup(service: &str, key: &str) -> Option<Option<String>> {
    SECRET_SESSION.with(|cell| {
        let session = cell.borrow();
        let session = session.as_ref()?;
        let entry = session.memo.get(&(service.to_string(), key.to_string()))?;
        if entry.gen == current_secret_generation() {
            Some(entry.value.clone())
        } else {
            None
        }
    })
}

/// Record a SUCCESSFUL read outcome (`Some(value)` or a genuine `None` miss)
/// into the active session's memo, tagged with `read_gen` — the generation
/// SNAPSHOTTED BY THE CALLER *before* the underlying keychain read began. No-op
/// when no session is active. Errors are never recorded here (callers only call
/// this on `Ok`).
///
/// v0.2.84 (review F2 — TOCTOU close): the entry MUST carry the PRE-READ
/// generation, never `current_secret_generation()` read here at store time. If a
/// concurrent `set`/`delete` bumps the generation DURING the read (between the
/// caller's snapshot and this store), tagging with the post-bump value would make
/// the stale pre-write outcome look CURRENT — a subsequent `session_memo_lookup`
/// (which compares `entry.gen == current_secret_generation()`) would then serve
/// the value the caller read *before* the concurrent write. Tagging with the
/// pre-read snapshot means the mid-read bump leaves `entry.gen < current`, so the
/// entry reads as stale and the next lookup re-hits the keychain. Write-through
/// honesty holds even across a read that races a write.
fn session_memo_store(service: &str, key: &str, value: &Option<String>, read_gen: u64) {
    SECRET_SESSION.with(|cell| {
        if let Some(session) = cell.borrow_mut().as_mut() {
            session.memo.insert(
                (service.to_string(), key.to_string()),
                MemoEntry {
                    value: value.clone(),
                    gen: read_gen,
                },
            );
        }
    });
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

// ─── Call context + lock-state honesty (v0.2.82 WP-4a / G5) ───────────────
//
// The four-layer defence above (single-threaded tests + mutex + pacing +
// backoff + bounded worker) prevents/rides-out a gnome-keyring SIGTRAP under
// burst. It does NOT address the SECOND half of the G5 incident: after the
// daemon crash-restarts, the login collection comes back LOCKED, and the next
// Secret-Service access from ANY code path pops the OS unlock dialog. A
// BACKGROUND path (daily weights poll, hub `/env` resolution for hooks/MCPs,
// coordination page-mount fetch) that trips that dialog does so with zero user
// context — the user sees a bare "unlock your keyring" prompt they can't place.
// keyring-rs maps a dismissed prompt to `Error::Prompt` → `NoStorageAccess`,
// which `is_transient` correctly treats as NON-transient (no retry storm), but
// the dialog still popped and the caller still failed opaquely.
//
// Fix: classify every call as Interactive or Background. A Background call on
// Linux PROBES the default collection's `Locked` property FIRST — a pure D-Bus
// property read (`org.freedesktop.Secret.Collection.Locked`) that never
// prompts and never attempts an unlock — and, when locked, returns the
// distinct `KeychainError::Locked` state IMMEDIATELY, WITHOUT constructing a
// `keyring::Entry` (Entry construction is itself a session negotiation that a
// background read must not perform against a locked store). Interactive calls
// skip the probe and proceed exactly as before — a user who just clicked
// "reveal secret" has the context to answer an unlock dialog.
//
// No new retry / auto-unlock / auto-heal is added (standing rule): a locked
// keychain is an honest terminal state surfaced to the caller, not something
// this layer tries to fix.

/// Whether a keychain access originates from an explicit user action on a
/// secrets-bearing surface (`Interactive`) or from a background poll / spawn /
/// hub-resolution / page-mount fetch (`Background`).
///
/// The distinction drives ONE behavioural difference: a `Background` call on
/// Linux probes the collection lock state first and short-circuits to
/// `KeychainError::Locked` (no Entry construction, no prompt) when the store is
/// locked. `Interactive` never probes — the user is present and can answer an
/// OS unlock dialog if one appears.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CallContext {
    /// Explicit user click on a secrets-bearing action (SecretsPanel
    /// save/reveal, OnboardingWizard, license activation). May prompt.
    Interactive,
    /// Poll / spawn / hub-resolution / page-mount fetch. Must never prompt;
    /// probes lock state and soft-fails `Locked` instead.
    Background,
}

/// A keychain access outcome that distinguishes a LOCKED store from every
/// other failure, so callers (and the hub `/env` route) can surface honest
/// states — `keychain_locked` vs `keychain_error` vs the ordinary
/// key-not-present miss — instead of collapsing them all into one opaque
/// `Err(String)`.
///
/// This is deliberately NOT `keyring::Error` (which isn't uniformly `Send`
/// across the bounded-timeout worker channel and carries no launcher-level
/// "locked" notion — a dismissed prompt is `NoStorageAccess`, indistinguishable
/// at that layer from an access-denied ACL).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum KeychainError {
    /// The OS keychain / login collection is LOCKED. A background read
    /// detected this via the lock-state probe and refused to prompt. The
    /// remedy is a user unlock, not a retry.
    Locked,
    /// Any other keychain failure (daemon error after retries, timeout,
    /// worker wedged, shape error). Carries a human-readable detail string;
    /// NEVER a secret value.
    Other(String),
}

impl std::fmt::Display for KeychainError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            KeychainError::Locked => write!(
                f,
                "OS keychain is locked — unlock your login keychain or open \
                 the launcher to restore secret resolution"
            ),
            KeychainError::Other(detail) => write!(f, "{}", detail),
        }
    }
}

impl std::error::Error for KeychainError {}

/// Probe whether the OS default Secret-Service collection is currently LOCKED,
/// WITHOUT constructing a `keyring::Entry` and WITHOUT ever prompting.
///
/// Returns:
///   * `Some(true)`  — collection is locked (a background read must soft-fail).
///   * `Some(false)` — collection is unlocked (proceed).
///   * `None`        — UNKNOWN: the probe could not determine lock state
///                     (no Secret-Service backend, D-Bus unreachable, non-Linux
///                     platform). Callers proceed as before — we cannot be
///                     stricter without breaking non-SecretService setups.
///
/// Implementation (Linux): fetches the default collection and reads its
/// `Locked` D-Bus property. Both are plain reads (`ReadAlias` + `Get Locked`) —
/// they neither unlock nor prompt. v0.3.0 (K-2): the reads go through the SHARED
/// persistent Secret-Service session (the one get/set/delete reuse), so a
/// Background probe no longer opens a fresh throw-away session per read; only if
/// that shared connect fails does it fall back to a fresh
/// `EncryptionType::Plain` + ZERO-max-prompt-timeout session. The whole probe
/// runs inside the bounded-timeout worker so a wedged D-Bus daemon yields `None`
/// (UNKNOWN) rather than hanging.
///
/// macOS / Windows: always `None` (Unlocked-equivalent → proceed). The
/// apple-native ACL prompt model and Windows Credential Manager have different
/// semantics that need their own investigation (see DEFERRALS D4 in the
/// v0.2.82 plan). Documented here so a future maintainer doesn't mistake the
/// `None` for an oversight.
#[cfg(target_os = "linux")]
pub fn probe_default_collection_locked() -> Option<bool> {
    // Allow tests to inject a deterministic lock state without a live D-Bus.
    #[cfg(any(test, debug_assertions))]
    if let Some(forced) = test_probe_override() {
        return forced;
    }
    // Run the D-Bus property read on the bounded-timeout worker: a wedged
    // Secret Service must not hang the probe. A timeout / worker-stuck →
    // UNKNOWN (None), the conservative "proceed as today" default.
    run_keychain_with_timeout(probe_default_collection_locked_blocking).unwrap_or(None)
}

/// The blocking body of the Linux lock probe (runs on the worker thread).
/// Separated so the timeout wrapper stays thin and the D-Bus calls are all in
/// one place. Any error in the negotiation / property read → `None` (UNKNOWN).
///
/// v0.3.0 (K-2): route the probe through the SHARED persistent connection first,
/// so a Background read no longer opens a fresh throw-away Secret-Service
/// session per probe (the connect/disconnect churn WP-K exists to cut). The
/// probe is two pure D-Bus reads (`ReadAlias` + `Get Locked`) that can NEVER
/// unlock or prompt (see `secrets_ss_connection::probe_default_collection_locked`),
/// so the shared connection's no-max-prompt-timeout does not risk a dialog. If
/// the shared connect itself fails (transient/permanent), fall back to the
/// ORIGINAL ephemeral 0-timeout session so the probe is no less capable than
/// before.
#[cfg(target_os = "linux")]
fn probe_default_collection_locked_blocking() -> Option<bool> {
    // Preferred path: reuse the shared session. `Ok(Some/None)` is a definite
    // answer / UNKNOWN; `Err(_)` (shared connect or D-Bus failure) drops through
    // to the ephemeral fallback below.
    match crate::secrets_ss_connection::probe_default_collection_locked() {
        Ok(answer) => return answer,
        Err(_) => { /* fall through to the ephemeral probe */ }
    }
    probe_default_collection_locked_ephemeral()
}

/// Fallback lock probe on a fresh, isolated 0-max-prompt-timeout session — the
/// original WP-4a probe. Used only when the shared connection is unavailable
/// (so we never leave the Background gate blind just because the persistent
/// session could not be established). `Plain` avoids a Diffie-Hellman handshake;
/// timeout 0 guarantees no unlock prompt can appear (belt-and-suspenders, since
/// the reads here also cannot prompt).
#[cfg(target_os = "linux")]
fn probe_default_collection_locked_ephemeral() -> Option<bool> {
    use dbus_secret_service::{EncryptionType, SecretService};
    let ss = SecretService::connect_with_max_prompt_timeout(EncryptionType::Plain, 0).ok()?;
    let collection = ss.get_default_collection().ok()?;
    // `is_locked()` is a pure `Get` of the `Locked` property — no unlock, no
    // prompt. An error here (e.g. no default collection) → UNKNOWN.
    collection.is_locked().ok()
}

/// Non-Linux platforms have no Secret-Service `Locked` probe. Always UNKNOWN →
/// callers proceed as before. (macOS apple-native / Windows Credential Manager
/// prompt semantics are out of scope this release — DEFERRALS D4.)
#[cfg(not(target_os = "linux"))]
pub fn probe_default_collection_locked() -> Option<bool> {
    None
}

/// Test seam: force the lock probe to a fixed answer so the Background-context
/// path can be exercised without a live D-Bus. `Some(Some(true))` = locked,
/// `Some(Some(false))` = unlocked, `Some(None)` = UNKNOWN, `None` = no override
/// (use the real probe). RAII guard is [`TestProbeGuard`].
#[cfg(any(test, debug_assertions))]
static TEST_PROBE_OVERRIDE: std::sync::Mutex<Option<Option<bool>>> =
    std::sync::Mutex::new(None);

#[cfg(any(test, debug_assertions))]
fn test_probe_override() -> Option<Option<bool>> {
    *TEST_PROBE_OVERRIDE
        .lock()
        .unwrap_or_else(|p| p.into_inner())
}

/// Test-only RAII guard forcing [`probe_default_collection_locked`] to a fixed
/// answer for the guard's lifetime. Restores the previous value on drop.
#[cfg(any(test, debug_assertions))]
pub struct TestProbeGuard {
    prev: Option<Option<bool>>,
}

#[cfg(any(test, debug_assertions))]
impl TestProbeGuard {
    /// `locked`: `Some(true)` = locked, `Some(false)` = unlocked, `None` =
    /// UNKNOWN (probe indeterminate).
    pub fn new(locked: Option<bool>) -> Self {
        let mut slot = TEST_PROBE_OVERRIDE.lock().unwrap_or_else(|p| p.into_inner());
        let prev = *slot;
        *slot = Some(locked);
        Self { prev }
    }
}

#[cfg(any(test, debug_assertions))]
impl Drop for TestProbeGuard {
    fn drop(&mut self) {
        let mut slot = TEST_PROBE_OVERRIDE.lock().unwrap_or_else(|p| p.into_inner());
        *slot = self.prev;
    }
}

/// Test seam: counts `keyring::Entry` constructions inside the guarded get/set/
/// delete hot paths. A Background call that short-circuits on a locked probe
/// must NOT increment this (T18). Only compiled in test/debug builds.
#[cfg(any(test, debug_assertions))]
pub static ENTRY_CONSTRUCTION_COUNT: std::sync::atomic::AtomicUsize =
    std::sync::atomic::AtomicUsize::new(0);

/// Record one `Entry::new` construction (test/debug builds only). Called from
/// the production hot-path closures so the count reflects real construction,
/// not a test double.
#[cfg(any(test, debug_assertions))]
#[inline]
fn note_entry_construction() {
    ENTRY_CONSTRUCTION_COUNT.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
}

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
///
/// v0.2.82 (WP-4a / G5c) — WHY the prompt path can't be reached by a retry:
/// keyring-rs maps a DISMISSED OS unlock prompt to `Error::Prompt` →
/// `NoStorageAccess`. Because `NoStorageAccess` is NON-transient here, a
/// dismissed prompt (or a locked store) propagates on the FIRST attempt — the
/// `PlatformFailure` retry loop is never entered for it, so no retry can
/// re-pop the unlock dialog. (Prompts arise only from unlock ATTEMPTS; the
/// lock-state probe added in WP-4a reads the `Locked` property and never
/// attempts an unlock, so it can't prompt either.) A dismissed prompt is a
/// final answer. Pinned by `is_transient_no_storage_access_is_not_retried_g5c`.
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
fn paced_call<T>(f: impl FnOnce() -> T) -> T {
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
//   1. flock(EXCLUSIVE) the pace file (blocks until we own it).
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
//   * non-unix (windows-native / apple-native have no crashy daemon) →
//     per-process pacing only (the whole module is `cfg(unix)`; the
//     `run_with_cross_process_pace` shim is a pass-through elsewhere).
//
// The `MIN_CALL_SPACING` test override (`with_test_spacing` / `TestSpacingGuard`)
// extends here too via `current_spacing()`, so the suite stays fast.
#[cfg(unix)]
mod cross_process_pace {
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
    fn pace_file_path() -> PathBuf {
        // Test seam: let a test pin the pace file to an isolated temp path so
        // it neither touches the user's real vct_root nor races with the
        // process-global `VCT_STATE_DIR` env.
        #[cfg(any(test, debug_assertions))]
        if let Some(p) = test_pace_path_override() {
            return p;
        }
        crate::paths::vct_root_dir().join("keyring.pace")
    }

    /// One-shot warn de-dup: we emit the "degraded to in-process pacing" warn
    /// at most once per process to avoid log spam on a persistently-unwritable
    /// root. `false` → not yet warned.
    static WARNED_ONCE: std::sync::atomic::AtomicBool =
        std::sync::atomic::AtomicBool::new(false);

    /// Test-visible count of warns actually emitted (so T20 can assert
    /// "exactly one warn" without scraping stderr). Incremented in lock-step
    /// with the real `eprintln`.
    #[cfg(any(test, debug_assertions))]
    pub(in crate::secrets) static WARN_COUNT: std::sync::atomic::AtomicUsize =
        std::sync::atomic::AtomicUsize::new(0);

    fn warn_once(msg: &str) {
        if !WARNED_ONCE.swap(true, std::sync::atomic::Ordering::SeqCst) {
            #[cfg(any(test, debug_assertions))]
            WARN_COUNT.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            eprintln!("[vct-secrets] WARN: {msg}");
        }
    }

    /// Test-only reset of the one-shot warn latch (so consecutive test cases
    /// each get a fresh "have we warned yet" state).
    #[cfg(any(test, debug_assertions))]
    pub(in crate::secrets) fn reset_warn_latch_for_test() {
        WARNED_ONCE.store(false, std::sync::atomic::Ordering::SeqCst);
        WARN_COUNT.store(0, std::sync::atomic::Ordering::SeqCst);
    }

    #[cfg(any(test, debug_assertions))]
    static TEST_PACE_PATH: std::sync::Mutex<Option<PathBuf>> = std::sync::Mutex::new(None);

    #[cfg(any(test, debug_assertions))]
    fn test_pace_path_override() -> Option<PathBuf> {
        TEST_PACE_PATH.lock().unwrap_or_else(|p| p.into_inner()).clone()
    }

    /// Test-only: pin the pace file path (RAII via [`super::TestPacePathGuard`]).
    #[cfg(any(test, debug_assertions))]
    pub(in crate::secrets) fn set_test_pace_path(p: Option<PathBuf>) -> Option<PathBuf> {
        let mut slot = TEST_PACE_PATH.lock().unwrap_or_else(|p| p.into_inner());
        std::mem::replace(&mut *slot, p)
    }

    /// Test-only: read back the stored last-call timestamp (nanos) from the
    /// pace file, so T20 can assert two paced calls wrote monotonic stamps.
    #[cfg(any(test, debug_assertions))]
    pub(in crate::secrets) fn read_pace_timestamp_for_test() -> Option<u128> {
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

    /// T-1 (v0.2.83), TEST/DEBUG ONLY: set true while a keychain test holds the
    /// production `keyring.pace` flock via `test_serialize::keychain_serialize_lock`.
    /// `acquire_and_space` then SKIPS re-acquiring the (non-reentrant) flock so a
    /// test that holds the lock AND calls the real `secrets::set` — which runs
    /// `with_cross_process_pace` on the worker thread — does NOT deadlock against
    /// its own held lock. Compiled out entirely in release builds (the whole
    /// `if` below is `#[cfg(any(test, debug_assertions))]`), so PRODUCTION paths
    /// are byte-identical to pre-T-1.
    #[cfg(any(test, debug_assertions))]
    pub(in crate::secrets) static TEST_HOLDS_PRODUCTION_PACE:
        std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

    /// Acquire the exclusive lock and enforce cross-process spacing. Returns
    /// the held lock guard on success, or `None` if the pace file is
    /// uncreatable / flock fails (caller then degrades to in-process pacing).
    ///
    /// `spacing` is threaded in (not read from the parent module directly) so
    /// the test override applies identically to the cross-process gate.
    fn acquire_and_space(spacing: std::time::Duration) -> Option<PaceLock> {
        let path = pace_file_path();
        // T-1 reentrancy skip (test/debug only; compiled out in release). If a
        // keychain test already holds the production pace flock (via
        // `keychain_serialize_lock`) AND this call resolves to the SAME real
        // production file, re-acquiring it here (on the worker thread, a
        // different fd) would block forever — flock is not reentrant across fds.
        // Skip only in that exact case: a test that pins the pace path to its own
        // temp file (`TestPacePathGuard`) is exercising the REAL pacing on an
        // ISOLATED file and must NOT be skipped (no deadlock — different file).
        #[cfg(any(test, debug_assertions))]
        if TEST_HOLDS_PRODUCTION_PACE.load(std::sync::atomic::Ordering::SeqCst)
            && test_pace_path_override().is_none()
        {
            return None;
        }
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
        // LOCK_EX blocks until acquired. A held keychain op is bounded by the
        // caller's timeout worker, so the wait here is bounded transitively.
        let rc = unsafe { libc::flock(fd, libc::LOCK_EX) };
        if rc != 0 {
            let err = std::io::Error::last_os_error();
            warn_once(&format!(
                "flock(LOCK_EX) failed on {path:?}: {err} — cross-process \
                 keyring pacing disabled (in-process pacing still active)"
            ));
            return None;
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
    /// request. On any file-lock failure, `op` still runs (degraded to
    /// in-process pacing) — a soft-fail, never a hard block on the user's
    /// secret read.
    pub(super) fn run_with_cross_process_pace<T>(op: impl FnOnce() -> T) -> T {
        let spacing = super::current_spacing();
        // Hold the guard across `op`; drop (unlock) after.
        let mut guard = acquire_and_space(spacing);
        let result = op();
        // v0.2.84 (D8.3): stamp the pace file with the op's COMPLETION time
        // WHILE the flock is still held, so the next process spaces off when we
        // FINISHED (true daemon idle gap), not when we started. On the degraded
        // path (`acquire_and_space` returned `None` — unwritable pace file or
        // the T-1 reentrancy skip) there is no stamp to write; in-process
        // pacing carries the gap (unchanged soft-fail behaviour).
        if let Some(g) = guard.as_mut() {
            g.record_completion_timestamp();
        }
        // `guard` drops here → flock(LOCK_UN), after the completion stamp.
        result
    }

    /// T-1 (v0.2.83), TEST/DEBUG ONLY: RAII holder of the PRODUCTION
    /// `<vct_root>/keyring.pace` flock for a keychain test's whole lifetime, so a
    /// `cargo test --workspace` process serializes against a RUNNING launcher
    /// (which paces on that SAME file via `run_with_cross_process_pace`) — the
    /// live-launcher-vs-test race that flaked `github_pat_keychain_tests`.
    ///
    /// While held, `TEST_HOLDS_PRODUCTION_PACE` is set so the test's OWN real
    /// `secrets::set` (which runs `acquire_and_space` on the worker thread) skips
    /// re-acquiring the non-reentrant flock instead of self-deadlocking. Drop
    /// clears the flag and releases the flock (kernel also releases on close).
    ///
    /// Returns `None` on any flock failure (unwritable root / no flock support) —
    /// the caller degrades to the test lockfile + in-process serialization
    /// (pre-T-1 behaviour; a soft degrade, never a block).
    #[cfg(any(test, debug_assertions))]
    pub(in crate::secrets) struct TestProductionPaceGuard {
        _lock: PaceLock,
    }

    #[cfg(any(test, debug_assertions))]
    impl Drop for TestProductionPaceGuard {
        fn drop(&mut self) {
            // Clear the reentrancy flag FIRST, then `_lock`'s Drop LOCK_UNs.
            TEST_HOLDS_PRODUCTION_PACE
                .store(false, std::sync::atomic::Ordering::SeqCst);
        }
    }

    #[cfg(any(test, debug_assertions))]
    pub(in crate::secrets) fn acquire_production_pace_lock_for_test(
    ) -> Option<TestProductionPaceGuard> {
        use std::os::unix::io::AsRawFd;
        // The REAL production pace path (bypass any test path override — the
        // live launcher only ever locks this file).
        let path = crate::paths::vct_root_dir().join("keyring.pace");
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let file = std::fs::OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .truncate(false)
            .open(&path)
            .ok()?;
        let fd = file.as_raw_fd();
        // Blocking exclusive lock — the SAME LOCK_EX the launcher's
        // `acquire_and_space` uses, so we queue behind its held-lock windows.
        let rc = unsafe { libc::flock(fd, libc::LOCK_EX) };
        if rc != 0 {
            return None;
        }
        // Now that WE hold it, set the flag so this process's nested
        // `acquire_and_space` (the test's own `set`) skips re-acquisition.
        TEST_HOLDS_PRODUCTION_PACE.store(true, std::sync::atomic::Ordering::SeqCst);
        Some(TestProductionPaceGuard {
            _lock: PaceLock { _file: file, fd },
        })
    }
}

/// Cross-process pacing shim. On unix, serializes the keychain op across ALL
/// VCO processes via a held flock (see `cross_process_pace`). On non-unix it's
/// a pass-through (those platforms lack the crashy daemon). Called from inside
/// the bounded-timeout worker, wrapping the full `retry_with_backoff` op, so a
/// wedged flock or op is still bounded by `KEYCHAIN_OP_TIMEOUT`.
#[inline]
fn with_cross_process_pace<T>(op: impl FnOnce() -> T) -> T {
    #[cfg(unix)]
    {
        cross_process_pace::run_with_cross_process_pace(op)
    }
    #[cfg(not(unix))]
    {
        op()
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
        // v0.3.0 (WP-K): the probe uses the SAME `KeychainEntry` primitive as
        // the real ops, so on Linux it exercises the persistent Secret-Service
        // connection (and reflects its reachability) rather than opening a
        // separate one-shot session.
        let entry = match KeychainEntry::new("vct.probe.keyring_available", "probe") {
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

// ─── Backend availability (v0.2.82 CI fix) ───────────────────────────────
//
// "keychain_degraded" (hub /env → 503 `keychain_error`) means a keychain
// that NORMALLY WORKS failed a read. On hosts with NO Secret Service
// backend at all (headless CI, servers), every keychain read errors by
// construction — that is not degradation, it is the pre-v0.2.82
// no-keychain reality: consumers must fall through to the legacy miss path
// (file store / `key_not_active`). Memoized once per process: backend
// availability is a property of the host session, not of individual reads
// (a daemon that dies mid-run is real degradation and stays reported).

#[cfg(any(test, debug_assertions))]
static TEST_BACKEND_AVAILABILITY_OVERRIDE: std::sync::Mutex<Option<bool>> =
    std::sync::Mutex::new(None);

/// Test-only RAII guard forcing `keychain_backend_available()` to a fixed
/// value (mirrors `TestProbeGuard`). Lets degraded-state tests assert the
/// 503 envelopes on headless CI where the real backend is absent, and lets
/// the no-backend regression test simulate headless on a desktop.
#[cfg(any(test, debug_assertions))]
pub struct TestBackendAvailabilityGuard {
    prev: Option<bool>,
}

#[cfg(any(test, debug_assertions))]
impl TestBackendAvailabilityGuard {
    pub fn new(available: bool) -> Self {
        let mut slot = TEST_BACKEND_AVAILABILITY_OVERRIDE
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        let prev = *slot;
        *slot = Some(available);
        Self { prev }
    }
}

#[cfg(any(test, debug_assertions))]
impl Drop for TestBackendAvailabilityGuard {
    fn drop(&mut self) {
        let mut slot = TEST_BACKEND_AVAILABILITY_OVERRIDE
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        *slot = self.prev;
    }
}

/// Whether this host has a WORKING keychain backend at all (memoized).
/// See the module-section comment above for the degraded-vs-no-backend
/// distinction this powers.
pub fn keychain_backend_available() -> bool {
    #[cfg(any(test, debug_assertions))]
    {
        if let Some(v) = *TEST_BACKEND_AVAILABILITY_OVERRIDE
            .lock()
            .unwrap_or_else(|p| p.into_inner())
        {
            return v;
        }
    }
    static AVAILABLE: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *AVAILABLE.get_or_init(keyring_probe_available)
}

// ─── Write chokepoint (v0.2.80 A4) ────────────────────────────────────────
//
// Every production keychain write funnels through `set` on every OS. Before
// v0.2.80 the shape guard lived only at the app-crate READ boundaries; the
// actual writers (GUI `set_secret_v2`, hub `/migrate`, ~35 call-sites) reached
// `set` unguarded, so a blob pasted in the GUI or POSTed to `/migrate` was
// laundered into the keychain. Guarding per-call-site is the N-copies
// anti-pattern; instead the guard lives HERE, at the one function every writer
// must pass through, so it is structurally unbypassable.
//
//   * `set`                    — the default write: refuses a blob-shaped value.
//   * `set_allowing_multiline` — the opt-out for a caller that has vouched the
//                                value legitimately spans multiple lines (a
//                                manifest-declared multi-line secret). It still
//                                rejects control chars + an over-long github_pat
//                                (allow_multiline does NOT bypass those).
//   * `set_raw`                — PRIVATE. The bare keychain write, no shape
//                                guard. Only `set` / `set_allowing_multiline`
//                                may reach it, so no writer can skip the guard.
//
// The value NEVER appears in an error: the guard surfaces the reason slug +
// byte-length only.

/// Guarded keychain write — the default write path for a single-well-formed
/// secret. Rejects a blob-shaped value (embedded newline with no recognised
/// structure, a `KEY=value` continuation line, a control char, or an over-long
/// `github_pat`) BEFORE it can reach the OS keychain, returning a metadata-only
/// error (reason slug + byte-length, never the value).
///
/// A legit multi-line secret (PEM/cert/OpenSSH — matched by the predicate's
/// allowlist) passes. A caller that must store a *non-allowlisted* multi-line
/// value it has independently vouched for uses [`set_allowing_multiline`].
pub fn set(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
    value: &str,
) -> Result<(), String> {
    if let Err(reason) =
        secret_value_shape::is_single_line_secret(value, false, key)
    {
        // Metadata only: reason slug + byte-length. NEVER the value.
        return Err(format!(
            "keyring set refused: value shape {} ({} bytes)",
            reason,
            value.len()
        ));
    }
    // Writes are Interactive in practice (a user saved a secret). No Background
    // write site exists today; if one appears, add a `set_with_context` twin
    // rather than defaulting a background write to Interactive.
    set_raw(scope, module_id, key, value, CallContext::Interactive)
        .map_err(|e| e.to_string())
}

/// Guarded keychain write for a caller-vouched MULTI-LINE value.
///
/// Same as [`set`] but passes `allow_multiline = true` to the shape predicate:
/// an unrecognised multi-line value (one that isn't a PEM/cert the allowlist
/// already accepts) is permitted because the CALLER has established, out of
/// band, that a multi-line value is legitimate here (e.g. a module manifest
/// whose `validation_regex` matched the pasted value). The control-char and
/// over-long-`github_pat` gates are NOT bypassed by `allow_multiline` — a
/// control char or a 200+char single-line github_pat is still refused. Use this
/// ONLY where a multi-line value is provably legitimate; the default
/// [`set`] is the safe choice everywhere else.
pub fn set_allowing_multiline(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
    value: &str,
) -> Result<(), String> {
    if let Err(reason) =
        secret_value_shape::is_single_line_secret(value, true, key)
    {
        // Metadata only: reason slug + byte-length. NEVER the value.
        return Err(format!(
            "keyring set refused: value shape {} ({} bytes)",
            reason,
            value.len()
        ));
    }
    set_raw(scope, module_id, key, value, CallContext::Interactive)
        .map_err(|e| e.to_string())
}

/// The bare keychain write — PRIVATE, no shape guard. Reached ONLY through the
/// guarded [`set`] / [`set_allowing_multiline`] wrappers, so every writer pays
/// the guard. The `mock_set` early-return is here (not in the wrappers) so it
/// fires for BOTH guarded paths and so a guard-failure `Err` from the wrappers
/// short-circuits before any mock/keychain interaction.
///
/// v0.2.82 (WP-4a): honours `ctx`. A `Background` write against a LOCKED store
/// short-circuits to `KeychainError::Locked` BEFORE any Entry construction.
/// (Writes are almost always `Interactive` in practice — a user saved a
/// secret — but the parameter is threaded uniformly so the whole surface obeys
/// the same lock policy.)
fn set_raw(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
    value: &str,
    ctx: CallContext,
) -> Result<(), KeychainError> {
    // Lock gate FIRST — a locked Background write short-circuits before any
    // keychain-access stage (mock or real).
    if let Some(locked) = background_lock_gate(ctx) {
        return Err(locked);
    }
    #[cfg(any(test, debug_assertions))]
    note_entry_construction();
    #[cfg(any(test, debug_assertions))]
    if let Some(result) = for_tests::mock_set(scope, module_id, key, value) {
        result.map_err(KeychainError::Other)?;
        // v0.2.84 (D8.2): a successful write bumps the generation so any live
        // session memo entry for this (or any) key is invalidated — write-
        // through honesty on the mock path too.
        bump_secret_generation();
        return Ok(());
    }
    // v0.2.76 (A4): own the (service, key, value) so the job closure is
    // 'static + Send, then run it on the bounded-timeout worker. The timeout
    // wraps OUTSIDE pacing/backoff — the closure IS the full retry_with_backoff
    // call (v0.2.72 P9 semantics unchanged inside). v0.2.82 (WP-4a): the
    // cross-process flock wraps OUTSIDE retry_with_backoff so the lock is held
    // across every attempt but INSIDE the timeout worker so a wedged flock is
    // still bounded.
    let service = scope.service_name(module_id);
    let key = key.to_string();
    let value = value.to_string();
    run_keychain_with_timeout(move || {
        with_cross_process_pace(|| {
            // v0.2.72 (P9): build the entry INSIDE the closure so construction
            // shares the same paced_call + backoff as the op. v0.3.0 (WP-K): on
            // Linux `KeychainEntry` routes through the persistent Secret-Service
            // connection (no per-op connect); elsewhere it wraps `keyring::Entry`.
            retry_with_backoff(|| KeychainEntry::new(&service, &key)?.set_password(&value))
                .map_err(|err| format!("keyring set: {}", err))
        })
    })
    .map_err(|to| KeychainError::Other(format!("keyring set: {}", to)))?
    .map_err(KeychainError::Other)?;
    // v0.2.84 (D8.2): successful real write — invalidate outstanding memo
    // entries (write-through honesty). Reached only after both `?` above pass.
    bump_secret_generation();
    Ok(())
}

/// Lock-state gate for a `Background` call. Returns `Some(KeychainError::Locked)`
/// when the context is `Background` AND the lock probe reports LOCKED — so the
/// caller short-circuits WITHOUT constructing a `keyring::Entry` and WITHOUT
/// prompting. `Interactive` calls, and `Background` calls where the probe is
/// UNKNOWN (`None`) or reports unlocked, return `None` → proceed as before.
///
/// A `None` probe result (no Secret-Service backend, D-Bus unreachable,
/// non-Linux) means we cannot be stricter without breaking non-SecretService
/// setups, so we proceed and let the ordinary error path handle any failure.
fn background_lock_gate(ctx: CallContext) -> Option<KeychainError> {
    if ctx != CallContext::Background {
        return None;
    }
    match probe_default_collection_locked() {
        Some(true) => Some(KeychainError::Locked),
        // Unlocked → proceed. UNKNOWN → proceed (can't be stricter); a debug
        // line aids diagnosis without spamming production logs.
        Some(false) => None,
        None => {
            #[cfg(debug_assertions)]
            eprintln!(
                "[vct-secrets] debug: lock-state probe indeterminate; \
                 proceeding with background keychain read"
            );
            None
        }
    }
}

/// Read a secret, choosing the [`CallContext`]. A `Background` read against a
/// LOCKED store returns `Err(KeychainError::Locked)` WITHOUT constructing an
/// `Entry` or prompting; `Interactive` reads proceed unconditionally.
///
/// Returns `Ok(None)` for a genuine key-not-present miss (distinct from
/// `Err(Locked)` / `Err(Other)`).
pub fn get_with_context(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
    ctx: CallContext,
) -> Result<Option<String>, KeychainError> {
    // Lock gate FIRST — a Background read against a locked store must not even
    // reach the keychain-access stage (mock or real). This ordering is what
    // T18's construction-counting seam pins: a locked Background call leaves
    // the count at 0. The gate stays BEFORE the session memo (v0.2.84 D8.4):
    // a locked store is an honest terminal state, never overridden by a cached
    // value, so the resilience posture is byte-identical.
    if let Some(locked) = background_lock_gate(ctx) {
        return Err(locked);
    }
    // v0.2.84 (D8.2): per-request read-through memo. A HIT returns the cached
    // outcome WITHOUT constructing an Entry or hitting the daemon — so a memo
    // hit does NOT increment the entry-construction count (correct: no keychain
    // access happened). Consulted AFTER the lock gate (posture unchanged) and
    // only when a `SecretReadSession` is active on this thread; outside a
    // session `session_memo_lookup` is always `None` → pre-v0.2.84 behaviour.
    let service = scope.service_name(module_id);
    if let Some(cached) = session_memo_lookup(&service, key) {
        return Ok(cached);
    }
    // v0.2.84 (review F2 — TOCTOU close): SNAPSHOT the generation NOW, BEFORE the
    // underlying keychain read starts, and tag the memo entry with this value.
    // A concurrent `set`/`delete` that bumps the generation DURING the read then
    // leaves `read_gen < current_secret_generation()`, so the entry is stale on
    // the next `session_memo_lookup` and the following read re-hits the keychain
    // — the memo can never serve a value the read observed *before* a racing
    // write. (Tagging at store time would capture the post-bump generation and
    // mis-classify the stale value as current.)
    let read_gen = current_secret_generation();
    // Test/debug seam: past the lock gate + memo miss we have committed to a
    // keychain access (mock OR real Entry construction). Placed BEFORE the mock
    // check so it fires on both the mock-proceed and real-proceed paths; T18
    // asserts it stays 0 for a locked Background call and increments once for a
    // proceeding call.
    #[cfg(any(test, debug_assertions))]
    note_entry_construction();
    #[cfg(any(test, debug_assertions))]
    if let Some(result) = for_tests::mock_get(scope, module_id, key) {
        let outcome = result.map_err(KeychainError::Other)?;
        // v0.2.84 (review F2): fire the mid-read hook AFTER the value has been
        // read but BEFORE the memo store — the exact TOCTOU window. A test
        // installs a hook that performs a concurrent `set`/`delete` (bumping the
        // generation) to prove the entry, tagged with the PRE-read generation,
        // is invalidated even though `outcome` already captured the pre-write
        // value. No-op in prod.
        #[cfg(any(test, debug_assertions))]
        for_tests::fire_mid_read_hook();
        // Cache the successful mock outcome (value or genuine miss) so the
        // session memo path is exercised under the test mock exactly as it is
        // against the real keychain. Tagged with the PRE-read generation (F2).
        session_memo_store(&service, key, &outcome, read_gen);
        return Ok(outcome);
    }
    // v0.2.76 (A4): own (service, key) → 'static + Send job → bounded-timeout
    // worker. keyring::Error is not Send-safe to carry across the channel
    // uniformly, so classify NoEntry INSIDE the job and return a plain
    // Result<Option<String>, String>. v0.2.84 (D8.2): the outer `service`
    // String + `key` &str are retained for the post-read memo store, so the
    // closure gets its OWN shadowed owned copies (spelling `Entry::new(&service,
    // &key)` preserved verbatim for the P9 structural guard).
    let outcome = {
        let service = service.clone();
        let key = key.to_string();
        run_keychain_with_timeout(move || {
            with_cross_process_pace(|| {
                // v0.2.72 (P9): build the entry INSIDE the closure. v0.3.0
                // (WP-K): `KeychainEntry` = persistent connection on Linux.
                match retry_with_backoff(|| KeychainEntry::new(&service, &key)?.get_password()) {
                    Ok(v) => Ok(Some(v)),
                    Err(keyring::Error::NoEntry) => Ok(None),
                    Err(err) => Err(format!("keyring get: {}", err)),
                }
            })
        })
        .map_err(|to| KeychainError::Other(format!("keyring get: {}", to)))?
        .map_err(KeychainError::Other)?
    };
    // v0.2.84 (review F2): mid-read hook, real-keychain path — same TOCTOU
    // window (value read, not yet stored). No-op in prod / when unarmed.
    #[cfg(any(test, debug_assertions))]
    for_tests::fire_mid_read_hook();
    // Memoize the SUCCESSFUL outcome only (errors already propagated via `?`, so
    // they are never cached — a transient failure stays retryable and keeps
    // flipping the hub's honest degraded flag on every occurrence). Tagged with
    // the PRE-read generation snapshot (F2) so a write that raced this read
    // invalidates the entry rather than being masked by it.
    session_memo_store(&service, key, &outcome, read_gen);
    Ok(outcome)
}

/// Presence check with an explicit [`CallContext`]. `Background` against a
/// locked store → `Err(KeychainError::Locked)` (NOT `Ok(false)` — a locked
/// store cannot honestly claim a key is absent).
pub fn is_set_with_context(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
    ctx: CallContext,
) -> Result<bool, KeychainError> {
    Ok(get_with_context(scope, module_id, key, ctx)?.is_some())
}

/// Delete with an explicit [`CallContext`]. `Background` against a locked store
/// → `Err(KeychainError::Locked)` (deletes are essentially always Interactive,
/// but the surface is uniform).
pub fn delete_with_context(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
    ctx: CallContext,
) -> Result<(), KeychainError> {
    // Lock gate FIRST — a locked Background delete short-circuits before any
    // keychain-access stage (mock or real).
    if let Some(locked) = background_lock_gate(ctx) {
        return Err(locked);
    }
    #[cfg(any(test, debug_assertions))]
    note_entry_construction();
    #[cfg(any(test, debug_assertions))]
    if for_tests::mock_delete(scope, module_id, key) {
        // v0.2.84 (D8.2): a successful delete bumps the generation so any live
        // session memo entry is invalidated (write-through honesty, mock path).
        bump_secret_generation();
        return Ok(());
    }
    // v0.2.76 (A4): own (service, key) → 'static + Send job → bounded-timeout
    // worker. NoEntry (already gone) is treated as success INSIDE the job.
    let service = scope.service_name(module_id);
    let key = key.to_string();
    run_keychain_with_timeout(move || {
        with_cross_process_pace(|| {
            // v0.2.72 (P9): build the entry INSIDE the closure. v0.3.0 (WP-K):
            // `KeychainEntry` = persistent connection on Linux.
            match retry_with_backoff(|| KeychainEntry::new(&service, &key)?.delete_credential()) {
                Ok(()) => Ok(()),
                Err(keyring::Error::NoEntry) => Ok(()), // already gone — treat as success
                Err(err) => Err(format!("keyring delete: {}", err)),
            }
        })
    })
    .map_err(|to| KeychainError::Other(format!("keyring delete: {}", to)))?
    .map_err(KeychainError::Other)?;
    // v0.2.84 (D8.2): successful real delete — invalidate outstanding memo
    // entries (write-through honesty). Reached only after both `?` above pass.
    bump_secret_generation();
    Ok(())
}

/// Read a secret (Interactive context — the back-compat default). Existing
/// call-sites keep working unchanged; Background call-sites are migrated to
/// [`get_with_context`] explicitly (see the WP-4a call-site classification).
pub fn get(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
) -> Result<Option<String>, String> {
    get_with_context(scope, module_id, key, CallContext::Interactive)
        .map_err(|e| e.to_string())
}

/// Presence check (Interactive context — back-compat default).
pub fn is_set(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
) -> Result<bool, String> {
    is_set_with_context(scope, module_id, key, CallContext::Interactive)
        .map_err(|e| e.to_string())
}

/// Delete (Interactive context — back-compat default).
pub fn delete(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
) -> Result<(), String> {
    delete_with_context(scope, module_id, key, CallContext::Interactive)
        .map_err(|e| e.to_string())
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

    /// Combined guard holding the in-process mutex, the test-only cross-process
    /// file lock, AND (T-1, v0.2.83) the PRODUCTION `<vct_root>/keyring.pace`
    /// flock so a `cargo test --workspace` process serializes against a RUNNING
    /// launcher (which paces on that same file), not just against sibling test
    /// binaries.
    ///
    /// Drop order in Rust is field-declaration order — `_proc_lock` drops first
    /// (releases the in-process mutex), then `_file_lock` (the test lockfile),
    /// then `_prod_pace_guard` (the production pace flock + reentrancy flag)
    /// LAST. Correct order: let in-process readers proceed first, hand the
    /// test-binary baton on next, and only THEN release the production baton to
    /// a waiting launcher.
    pub struct KeychainGuard {
        _proc_lock: MutexGuard<'static, ()>,
        _file_lock: Option<file_lock::FileLock>,
        /// T-1: production keyring.pace flock (unix; `None` on any flock
        /// failure — degrades to the test lockfile only). Field only exists on
        /// unix (the `cross_process_pace` module is `cfg(unix)`).
        #[cfg(unix)]
        _prod_pace_guard: Option<super::cross_process_pace::TestProductionPaceGuard>,
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
        // T-1 (v0.2.83): acquire the PRODUCTION keyring.pace flock FIRST so we
        // serialize against a running launcher before taking the test-side
        // batons. This is safe against self-deadlock: while held it sets
        // `TEST_HOLDS_PRODUCTION_PACE`, so the test's OWN real `secrets::set`
        // (which runs `acquire_and_space` on the worker thread) SKIPS the nested
        // flock re-acquire instead of blocking on it. (unix only; `None` on
        // flock failure → degrades to the test lockfile.)
        #[cfg(unix)]
        let prod_pace_guard =
            super::cross_process_pace::acquire_production_pace_lock_for_test();

        // Acquire the cross-process test lockfile next. If we acquired
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
            #[cfg(unix)]
            _prod_pace_guard: prod_pace_guard,
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

        // v0.2.82 (WP-4a): keys registered to make the next `mock_get` return
        // a NON-lock keychain Err (models a transient daemon read failure mid-
        // request). One-shot, consumed on fire. Used to drive the hub `/env`
        // `keychain_error` (503) vs `key_not_active` (404) distinction (T19).
        static MOCK_FAIL_GET_KEYS: RefCell<HashSet<String>> =
            RefCell::new(HashSet::new());

        // v0.2.84 (review F2): a one-shot callback fired by `get_with_context`
        // in the TOCTOU window between its pre-read generation snapshot and the
        // memo store. A test installs a hook that performs a concurrent
        // `set`/`delete` (bumping the generation) to prove the entry, tagged
        // with the PRE-read generation, is invalidated. Consumed on fire so it
        // only perturbs ONE read.
        #[allow(clippy::type_complexity)]
        static MID_READ_HOOK: RefCell<Option<Box<dyn FnOnce()>>> =
            const { RefCell::new(None) };
    }

    /// Register a one-shot callback to run inside `get_with_context`'s TOCTOU
    /// window (after it snapshots the generation, before it stores the memo
    /// entry). Models a concurrent write landing DURING a read. Consumed on
    /// fire. Test-only.
    pub fn set_mid_read_hook(hook: Box<dyn FnOnce()>) {
        MID_READ_HOOK.with(|cell| {
            *cell.borrow_mut() = Some(hook);
        });
    }

    /// Fire (and consume) the registered mid-read hook, if any. Called from
    /// `get_with_context` under `cfg(any(test, debug_assertions))`; a no-op when
    /// no hook is registered.
    pub(super) fn fire_mid_read_hook() {
        let hook = MID_READ_HOOK.with(|cell| cell.borrow_mut().take());
        if let Some(hook) = hook {
            hook();
        }
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
        MOCK_FAIL_GET_KEYS.with(|cell| cell.borrow_mut().clear());
        // v0.2.84 (F2): drop any un-fired mid-read hook so it never leaks into
        // a subsequent mock session on this thread.
        MID_READ_HOOK.with(|cell| *cell.borrow_mut() = None);
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
        MOCK_FAIL_GET_KEYS.with(|cell| cell.borrow_mut().clear());
        // v0.2.84 (F2): also drop any pending mid-read hook.
        MID_READ_HOOK.with(|cell| *cell.borrow_mut() = None);
    }

    /// Register `key` to make the **next** `secrets::get` (any context) targeting
    /// it return a NON-lock keychain `Err` (a `KeychainError::Other`). Models a
    /// transient daemon read failure DURING a request — distinct from a
    /// key-not-present miss. One-shot: consumed when it fires.
    ///
    /// The mock must be active. Used by the hub `/env` tests (T19) to drive the
    /// `keychain_error` (503) vs `key_not_active` (404) distinction.
    pub fn fail_next_get(key: &str) {
        MOCK_FAIL_GET_KEYS.with(|cell| {
            cell.borrow_mut().insert(key.to_string());
        });
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
                // v0.2.82: injected one-shot read failure (models a transient
                // daemon error mid-request, NOT a miss). Consumed on fire.
                let should_fail = MOCK_FAIL_GET_KEYS.with(|fc| fc.borrow_mut().remove(key));
                if should_fail {
                    return Err(format!(
                        "mock keychain get failure injected for key: {key}"
                    ));
                }
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

    // ─── v0.3.0 (WP-K): persistent Secret-Service connection surface ──────────

    /// The graceful-close entry point exists and is a safe no-op to call
    /// repeatedly (idempotent), so the hub-shutdown and launcher-exit call sites
    /// can invoke it unconditionally. On Linux it drains the persistent
    /// connection (the module's own test pins the slot-clearing); on other
    /// platforms it is a no-op. Here we prove it is callable and idempotent from
    /// the public surface without touching a real keychain.
    #[test]
    fn shutdown_keychain_connection_is_idempotent_public_noop_safe() {
        // Serialize with keychain tests: on Linux this touches the module's
        // process-wide connection slot, which other tests may lazily populate.
        let _lock = test_serialize::keychain_serialize_lock();
        shutdown_keychain_connection();
        shutdown_keychain_connection();
        #[cfg(target_os = "linux")]
        {
            // After the drain the module's connection slot is empty (a
            // subsequent real op would lazily reconnect). We assert via the
            // module's own idempotent shutdown, which leaves the slot cleared.
            crate::secrets_ss_connection::shutdown();
        }
    }

    /// A locked Background read STILL short-circuits to `Locked` without any
    /// keychain access, regardless of the persistent-connection routing — the
    /// lock-probe gate sits BEFORE `KeychainEntry` construction on every arm, so
    /// WP-K did not weaken the G5 lock posture. (Mock-backed; no real daemon.)
    #[test]
    fn wpk_lock_gate_still_precedes_persistent_connection_path() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _g = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        set(scope, "mod", "k", "v").unwrap();
        ENTRY_CONSTRUCTION_COUNT.store(0, std::sync::atomic::Ordering::SeqCst);
        let _probe = TestProbeGuard::new(Some(true)); // locked
        assert_eq!(
            get_with_context(scope, "mod", "k", CallContext::Background),
            Err(KeychainError::Locked),
            "a locked Background read must still short-circuit under WP-K"
        );
        assert_eq!(
            ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst),
            0,
            "the lock gate must precede any KeychainEntry construction"
        );
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

    /// K-2 STRUCTURAL PIN: the Background lock probe must route through the
    /// SHARED persistent connection FIRST, and only fall back to a fresh
    /// ephemeral session when the shared connect fails — so a Background read no
    /// longer opens a throw-away Secret-Service session per probe (the churn WP-K
    /// exists to cut). A behavioural pin needs a live daemon (the env-gated
    /// wiring test in the persistent-connection module covers that); this
    /// daemon-free source-shape pin asserts the SHIPPED routing: the blocking
    /// probe body CALLS the shared-connection probe fn (a genuine call site,
    /// counted after stripping comments) and preserves the ephemeral fallback fn.
    ///
    /// The scan strips `//` comments per line (so doc-comment mentions of the
    /// name don't count) and the two needles are assembled from split literals
    /// that never appear contiguously in this test's own source — so the ONLY
    /// contiguous CODE occurrences are the real production call site and the real
    /// fallback fn definition. FAIL-ON-REVERT: dropping the shared-connection
    /// call and going straight to a per-probe ephemeral connect (the pre-K-2
    /// shape) removes the only counted call site → 0 → reds this test.
    #[cfg(target_os = "linux")]
    #[test]
    fn k2_lock_probe_routes_through_shared_connection_before_ephemeral() {
        let src = include_str!("secrets.rs");

        // Strip each line's `//` comment tail, then whitespace-flatten the CODE.
        // (Doc comments mentioning the probe fn name must NOT count as call
        // sites.)
        fn code_before_line_comment(line: &str) -> &str {
            match line.find("//") {
                Some(idx) => &line[..idx],
                None => line,
            }
        }
        let code_flat: String = src
            .lines()
            .map(code_before_line_comment)
            .collect::<String>()
            .chars()
            .filter(|c| !c.is_whitespace())
            .collect();

        // Needle for the shared-connection call SITE (note the trailing `(` — a
        // call, not a mention). Assembled from split literals so no line in THIS
        // test's own code can form it contiguously.
        let shared_call_needle = String::from("secrets_ss_")
            + "connection::probe_default_collection_"
            + "locked(";
        let shared_hits = code_flat.matches(shared_call_needle.as_str()).count();
        assert_eq!(
            shared_hits, 1,
            "the Background lock probe must CALL the shared persistent-connection \
             probe fn exactly once (found {shared_hits} call sites, expected 1); \
             without it the probe reopens a throw-away session per read — the \
             connect/disconnect churn K-2 removes"
        );

        // The ephemeral 0-timeout session must survive as a named FALLBACK fn
        // (not inlined onto the hot path). Needle assembled from split literals.
        let ephemeral_fn_needle = String::from("fnprobe_default_collection_")
            + "locked_ephemeral(";
        assert!(
            code_flat.contains(ephemeral_fn_needle.as_str()),
            "the ephemeral 0-timeout probe session must be preserved as a named \
             FALLBACK fn so the probe is never left blind when the shared connect \
             fails"
        );
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

        // The retried closure constructs the entry as its first act, one per
        // hot-path fn. Needle assembled from SPLIT literals (never contiguous
        // in this test's own source) so the whitespace-stripped scan below
        // cannot match THIS line. v0.3.0 (WP-K): the construction spelling is
        // now `KeychainEntry::new` (the platform-abstracting primitive that, on
        // Linux, routes through the persistent Secret-Service connection); the
        // pinned invariant — "construction lives INSIDE the paced+retried
        // closure" — is unchanged, only the needle tracks the new spelling.
        let paced_needle =
            String::from("retry_with_backoff(||KeychainEntry::new(&service,") + "&key)?";

        let paced = flat.matches(paced_needle.as_str()).count();
        assert_eq!(
            paced, 3,
            "set/get/delete must each construct the KeychainEntry INSIDE \
             the retry_with_backoff closure (found {paced} paced \
             construction sites, expected 3) — an unpaced entry-construction \
             burst is the exact gnome-keyring crash P9 fixed"
        );
    }

    // ─── v0.2.80 A4: the `set` write chokepoint is guarded ────────────────
    //
    // These pin the INVARIANT the A4 refactor exists to enforce: every
    // production keychain write funnels through the guarded `set` /
    // `set_allowing_multiline`, and a blob is refused BEFORE it reaches the
    // keychain. The unit legs drive the guard through the thread-local mock
    // (no OS keychain). The source-scan leg proves the guard is structurally
    // unbypassable — no production code outside this file writes the keychain
    // via the raw `keyring` primitive.

    /// The default guarded `set` refuses a blob (a `KEY=value` continuation
    /// line after a token on line 0) with a metadata-only error — reason slug
    /// present, value ABSENT — and does NOT write the mock store. A plain
    /// single-line value and a legit PEM both pass and DO write.
    #[test]
    fn set_refuses_blob_and_accepts_single_line_and_pem() {
        let _g = for_tests::MockGuard::new();
        let scope = SecretScope::Global;

        // ACT: a blob is refused before it reaches the keychain.
        let blob = "tok\nKEY=v";
        let err = set(scope, "mod", "blob_key", blob)
            .expect_err("blob-shaped value must be refused by the guard");
        assert!(
            err.contains("blob_key_eq_continuation"),
            "error must carry the reason slug; got {err:?}"
        );
        assert!(
            !err.contains("KEY=v") && !err.contains("tok"),
            "the value must NEVER appear in the error; got {err:?}"
        );
        // Refusal happens before the write — nothing landed in the store.
        assert!(
            get(scope, "mod", "blob_key").unwrap().is_none(),
            "a refused blob must not be written to the keychain"
        );

        // LEAVE-ALONE: a plain single-line value passes and is written.
        set(scope, "mod", "plain", "plain-token-123").unwrap();
        assert_eq!(
            get(scope, "mod", "plain").unwrap().as_deref(),
            Some("plain-token-123")
        );

        // LEAVE-ALONE: a legit multi-line PEM passes the allowlist under the
        // DEFAULT guard (no opt-out needed) and is written verbatim.
        let pem =
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIB\nAAAA\n-----END RSA PRIVATE KEY-----";
        set(scope, "deploy", "deploy_key", pem).unwrap();
        assert_eq!(get(scope, "deploy", "deploy_key").unwrap().as_deref(), Some(pem));
    }

    /// `set_allowing_multiline` accepts a caller-vouched multi-line value that
    /// the default guard would refuse — BUT it still rejects a control char and
    /// an over-long `github_pat` (those gates are not bypassed by
    /// `allow_multiline`).
    #[test]
    fn set_allowing_multiline_accepts_vouched_but_still_blocks_control_and_long_pat() {
        let _g = for_tests::MockGuard::new();
        let scope = SecretScope::Global;

        // A non-allowlisted multi-line value: refused by default `set`,
        // accepted by the opt-out.
        let vouched = "line-one\nline-two-no-blob-signature";
        assert!(
            set(scope, "mod", "ml_default", vouched).is_err(),
            "default set must refuse an unrecognised multi-line value"
        );
        set_allowing_multiline(scope, "mod", "ml_optout", vouched)
            .expect("opt-out must accept a caller-vouched multi-line value");
        assert_eq!(
            get(scope, "mod", "ml_optout").unwrap().as_deref(),
            Some(vouched)
        );

        // Control char: STILL refused under the opt-out.
        let ctrl = "a\u{7}b";
        let err = set_allowing_multiline(scope, "mod", "ctrl", ctrl)
            .expect_err("control char must be refused even under allow_multiline");
        assert!(
            err.contains("control_char"),
            "control-char refusal must carry its slug; got {err:?}"
        );
        assert!(get(scope, "mod", "ctrl").unwrap().is_none());

        // Over-long single-line github_pat: STILL refused under the opt-out.
        let long_pat = "ghp_".to_string() + &"A".repeat(210);
        let err = set_allowing_multiline(scope, "mod", "github_pat", &long_pat)
            .expect_err("over-long github_pat must be refused even under allow_multiline");
        assert!(
            err.contains("github_pat_over_200"),
            "over-long github_pat refusal must carry its slug; got {err:?}"
        );
        assert!(get(scope, "mod", "github_pat").unwrap().is_none());
    }

    /// STRUCTURAL ENFORCEMENT (v0.2.80 A4 / audit §5.1). The unit legs above
    /// drive `set`; but a future edit could re-introduce a raw keychain write
    /// that bypasses the guard entirely — `keyring::Entry::new(..).set_password`
    /// straight to the OS store. This test walks the WHOLE launcher Rust source
    /// (app crate + vct-launcher-core + vct-hub) and asserts that the ONLY file
    /// which calls the raw keychain-write primitive `.set_password(` is THIS
    /// file (`secrets.rs`). Every other production write must go through
    /// `secrets::set` / `secrets::set_allowing_multiline`, which are guarded.
    ///
    /// How the negative is guaranteed: if someone adds
    /// `Entry::new(svc, k)?.set_password(v)` in, say, `secrets_cmd.rs` to skip
    /// the guard, that file now contains `.set_password(` and the scan below
    /// pushes it into `offenders`, failing the test. (Verified while writing
    /// this test by temporarily grepping for `.set_password(` across the tree:
    /// the only production match is in `secrets.rs`; comments elsewhere say
    /// `set_password` WITHOUT the `(` and so don't match the `.set_password(`
    /// needle.)
    #[test]
    fn set_password_is_only_called_from_secrets_rs() {
        use std::path::{Path, PathBuf};

        // `CARGO_MANIFEST_DIR` for this crate = launcher/src-tauri/vct-launcher-core.
        // The three crate source roots live under launcher/src-tauri/{src,
        // vct-launcher-core/src, vct-hub/src}. Walk up one level to the
        // src-tauri root, then scan each crate's `src`.
        let core_manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let src_tauri_root = core_manifest
            .parent()
            .expect("vct-launcher-core has a parent (src-tauri)")
            .to_path_buf();

        let scan_roots = [
            src_tauri_root.join("src"),                    // app crate
            src_tauri_root.join("vct-launcher-core").join("src"),
            src_tauri_root.join("vct-hub").join("src"),
        ];

        // The raw keychain-write primitive. A `.set_password(` call is a
        // DIRECT keychain write; the only legitimate site is this file's
        // `set_raw` + `keyring_probe_available` canary.
        const RAW_WRITE_NEEDLE: &str = ".set_password(";
        // The one file allowed to contain it.
        const ALLOWED_FILE: &str = "secrets.rs";

        fn collect_rs_files(dir: &Path, out: &mut Vec<PathBuf>) {
            let entries = match std::fs::read_dir(dir) {
                Ok(e) => e,
                Err(_) => return,
            };
            for entry in entries.flatten() {
                let path = entry.path();
                if path.is_dir() {
                    collect_rs_files(&path, out);
                } else if path.extension().and_then(|e| e.to_str()) == Some("rs") {
                    out.push(path);
                }
            }
        }

        let mut files: Vec<PathBuf> = Vec::new();
        for root in &scan_roots {
            collect_rs_files(root, &mut files);
        }
        assert!(
            files.len() > 50,
            "source scan found only {} .rs files — the scan roots are wrong \
             ({:?}); refusing to pass a scan that walked nothing",
            files.len(),
            scan_roots
        );

        // Detect the needle only in CODE, not in `//` line comments or `///`
        // doc comments (several files mention the historical
        // `Entry::new(..).set_password("canary")` shape in prose). For each
        // line we scan only the portion before the first `//`. This is a
        // deliberately simple lexer — it does not model string literals that
        // themselves contain `//` — which is sound here because a real keychain
        // write is `expr.set_password(arg)` code, never buried inside a string,
        // and this file's own `RAW_WRITE_NEEDLE` literal lives after a `//`-free
        // `const` line (so `secrets.rs` still self-matches as the allowed site).
        fn code_before_line_comment(line: &str) -> &str {
            match line.find("//") {
                Some(idx) => &line[..idx],
                None => line,
            }
        }

        let mut offenders: Vec<String> = Vec::new();
        let mut saw_allowed = false;
        for path in &files {
            let is_allowed = path.file_name().and_then(|n| n.to_str()) == Some(ALLOWED_FILE);
            let text = match std::fs::read_to_string(path) {
                Ok(t) => t,
                Err(_) => continue,
            };
            let has_code_write = text
                .lines()
                .any(|line| code_before_line_comment(line).contains(RAW_WRITE_NEEDLE));
            if has_code_write {
                if is_allowed {
                    saw_allowed = true;
                } else {
                    offenders.push(path.display().to_string());
                }
            }
        }

        assert!(
            saw_allowed,
            "expected `{}` in {} (the guarded chokepoint's own keychain write) \
             — scan may be reading the wrong tree",
            RAW_WRITE_NEEDLE, ALLOWED_FILE
        );
        assert!(
            offenders.is_empty(),
            "raw keychain write `{}` found OUTSIDE {} — every production write \
             must go through the guarded `secrets::set` / \
             `secrets::set_allowing_multiline` chokepoint, not a bare \
             `keyring::Entry` primitive. Offending files:\n  {}",
            RAW_WRITE_NEEDLE,
            ALLOWED_FILE,
            offenders.join("\n  ")
        );
    }

    /// K-3 CHOKEPOINT EROSION GUARD. `secrets_ss_connection` introduced a SECOND
    /// raw keychain-write path on Linux — `Item::set_secret(` (update in place)
    /// and `Collection::create_item(` (create). Those needles are NOT the
    /// `.set_password(` primitive the A4 scan above pins, so without this test a
    /// future caller in vct-hub or the app crate could acquire an unguarded
    /// write path (bypassing the value-shape guard, pacing, backoff, lock gate,
    /// memo invalidation, and the test mock) and the A4 scan would not see it —
    /// exactly the erosion the v0.2.80 secrets-set-chokepoint lesson locked
    /// against. This test asserts two invariants across the WHOLE launcher Rust
    /// tree:
    ///   1. the raw dbus write primitives `.set_secret(` / `create_item(` appear
    ///      ONLY in `secrets_ss_connection.rs`;
    ///   2. the `secrets_ss_connection::` module path is referenced ONLY from
    ///      `secrets.rs` (the guarded chokepoint) and `lib.rs` (the `mod`
    ///      declaration) — so the module cannot grow a caller elsewhere.
    /// Needles are assembled from SPLIT literals (never contiguous in this test's
    /// own source) so this file cannot self-match its own scan.
    #[test]
    fn raw_ss_write_primitives_and_module_confined_to_secrets_and_lib() {
        use std::path::{Path, PathBuf};

        let core_manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let src_tauri_root = core_manifest
            .parent()
            .expect("vct-launcher-core has a parent (src-tauri)")
            .to_path_buf();
        let scan_roots = [
            src_tauri_root.join("src"), // app crate
            src_tauri_root.join("vct-launcher-core").join("src"),
            src_tauri_root.join("vct-hub").join("src"),
        ];

        fn collect_rs_files(dir: &Path, out: &mut Vec<PathBuf>) {
            let entries = match std::fs::read_dir(dir) {
                Ok(e) => e,
                Err(_) => return,
            };
            for entry in entries.flatten() {
                let path = entry.path();
                if path.is_dir() {
                    collect_rs_files(&path, out);
                } else if path.extension().and_then(|e| e.to_str()) == Some("rs") {
                    out.push(path);
                }
            }
        }
        fn code_before_line_comment(line: &str) -> &str {
            match line.find("//") {
                Some(idx) => &line[..idx],
                None => line,
            }
        }

        let mut files: Vec<PathBuf> = Vec::new();
        for root in &scan_roots {
            collect_rs_files(root, &mut files);
        }
        assert!(
            files.len() > 50,
            "source scan found only {} .rs files — the scan roots are wrong ({:?})",
            files.len(),
            scan_roots
        );

        // Needles assembled from split literals so this test's own source never
        // matches them. `set_secret_needle` = ".set_secret(" ; `create_item_needle`
        // = "create_item(" ; `module_needle` = "secrets_ss_connection::".
        let set_secret_needle = String::from(".set_") + "secret(";
        let create_item_needle = String::from("create_") + "item(";
        let module_needle = String::from("secrets_ss_") + "connection::";

        // The one file allowed to contain the raw dbus write primitives.
        const WRITE_ALLOWED: &str = "secrets_ss_connection.rs";
        // The only two files allowed to reference the module path.
        const MODULE_ALLOWED: [&str; 2] = ["secrets.rs", "lib.rs"];

        let mut write_offenders: Vec<String> = Vec::new();
        let mut module_offenders: Vec<String> = Vec::new();
        let mut saw_write_allowed = false;
        let mut saw_module_ref = false;
        for path in &files {
            let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
            let text = match std::fs::read_to_string(path) {
                Ok(t) => t,
                Err(_) => continue,
            };
            let has_raw_write = text.lines().any(|line| {
                let code = code_before_line_comment(line);
                code.contains(set_secret_needle.as_str())
                    || code.contains(create_item_needle.as_str())
            });
            if has_raw_write {
                if name == WRITE_ALLOWED {
                    saw_write_allowed = true;
                } else {
                    write_offenders.push(path.display().to_string());
                }
            }
            let has_module_ref = text
                .lines()
                .any(|line| code_before_line_comment(line).contains(module_needle.as_str()));
            if has_module_ref {
                saw_module_ref = true;
                if !MODULE_ALLOWED.contains(&name) {
                    module_offenders.push(path.display().to_string());
                }
            }
        }

        assert!(
            saw_write_allowed,
            "expected the raw dbus write primitive in {WRITE_ALLOWED} (the Linux \
             persistent-connection writer's own site) — scan may be reading the \
             wrong tree"
        );
        assert!(
            write_offenders.is_empty(),
            "raw dbus keychain write primitive (the persistent-connection \
             item-set / collection-create-item calls) found OUTSIDE \
             {WRITE_ALLOWED} — every production write must go through the guarded \
             `secrets::set` chokepoint, not a bare persistent-connection \
             primitive. Offending files:\n  {}",
            write_offenders.join("\n  ")
        );
        assert!(
            saw_module_ref,
            "expected `secrets_ss_connection::` to be referenced from \
             `secrets.rs` — scan may be reading the wrong tree"
        );
        assert!(
            module_offenders.is_empty(),
            "`secrets_ss_connection::` referenced OUTSIDE {MODULE_ALLOWED:?} — the \
             persistent-connection module is `pub(crate)` and must only be reached \
             from the guarded `secrets` chokepoint (its `lib.rs` `mod` decl \
             aside). Offending files:\n  {}",
            module_offenders.join("\n  ")
        );
    }

    // ─── v0.2.82 WP-4a (G5): lock-state honesty + cross-process pacing ────────

    /// T18 — the headline. A `Background` read against a FAKE-LOCKED store
    /// returns `KeychainError::Locked` WITHOUT reaching the keychain-access
    /// stage — proven by the construction-counting seam staying at 0. The
    /// SAME fake lock under `Interactive` PROCEEDS (skips the probe), reaching
    /// the access stage (count increments) and resolving via the mock.
    ///
    /// FAIL-ON-BASE: base has no `CallContext` / no probe — a background read
    /// would construct an Entry (or hit the mock) regardless of lock state, so
    /// `Locked` is unreachable there. This test does not compile on base
    /// (the API doesn't exist), which IS the fail-on-base proof for the new
    /// surface; the behavioural fail-on-base is demonstrated in the throwaway
    /// base worktree run recorded in the WP-4a report.
    #[test]
    fn background_read_on_locked_store_short_circuits_without_entry() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _g = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        // Seed a value so a proceeding read would find something.
        set(scope, "mod", "k", "v").unwrap();

        // ACT: fake the store LOCKED, do a Background read.
        ENTRY_CONSTRUCTION_COUNT.store(0, std::sync::atomic::Ordering::SeqCst);
        let _probe = TestProbeGuard::new(Some(true)); // locked
        let bg = get_with_context(scope, "mod", "k", CallContext::Background);
        assert_eq!(
            bg,
            Err(KeychainError::Locked),
            "background read on a locked store must return Locked"
        );
        assert_eq!(
            ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst),
            0,
            "a locked Background read must NOT reach the keychain-access stage \
             (no Entry construction / no mock hit)"
        );

        // LEAVE-ALONE: Interactive with the SAME fake lock proceeds — it does
        // not probe, reaches the access stage (count increments), resolves via
        // the mock.
        let inter = get_with_context(scope, "mod", "k", CallContext::Interactive);
        assert_eq!(inter, Ok(Some("v".to_string())));
        assert_eq!(
            ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst),
            1,
            "an Interactive read proceeds to the access stage exactly once"
        );
    }

    /// A Background read where the probe reports UNLOCKED proceeds normally.
    #[test]
    fn background_read_on_unlocked_store_proceeds() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _g = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        set(scope, "mod", "k2", "v2").unwrap();
        let _probe = TestProbeGuard::new(Some(false)); // unlocked
        assert_eq!(
            get_with_context(scope, "mod", "k2", CallContext::Background),
            Ok(Some("v2".to_string())),
        );
    }

    /// A Background read where the probe is INDETERMINATE (UNKNOWN → None)
    /// proceeds — we cannot be stricter without breaking non-SecretService
    /// setups.
    #[test]
    fn background_read_on_unknown_probe_proceeds() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _g = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        set(scope, "mod", "k3", "v3").unwrap();
        let _probe = TestProbeGuard::new(None); // UNKNOWN
        assert_eq!(
            get_with_context(scope, "mod", "k3", CallContext::Background),
            Ok(Some("v3".to_string())),
        );
    }

    /// `is_set_with_context` under a locked Background probe returns
    /// `Err(Locked)` — NOT `Ok(false)`. A locked store cannot honestly claim a
    /// key is absent (that would be a silent downgrade).
    #[test]
    fn background_is_set_on_locked_store_is_locked_not_false() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _g = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        let _probe = TestProbeGuard::new(Some(true));
        assert_eq!(
            is_set_with_context(scope, "mod", "absent", CallContext::Background),
            Err(KeychainError::Locked),
        );
    }

    /// Back-compat: the un-suffixed `get` behaves as `Interactive` — it does
    /// NOT probe, so a fake-locked store still resolves via the mock.
    #[test]
    fn plain_get_is_interactive_and_ignores_lock_probe() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _g = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        set(scope, "mod", "bc", "bcv").unwrap();
        let _probe = TestProbeGuard::new(Some(true)); // locked, but Interactive ignores it
        assert_eq!(get(scope, "mod", "bc").unwrap().as_deref(), Some("bcv"));
    }

    /// `KeychainError::Locked` renders an honest, actionable message; the
    /// `Other` variant carries its detail verbatim.
    #[test]
    fn keychain_error_display_is_honest() {
        assert!(KeychainError::Locked
            .to_string()
            .contains("keychain is locked"));
        assert_eq!(
            KeychainError::Other("boom".into()).to_string(),
            "boom"
        );
    }

    /// T22 — `is_transient` matrix pin, extended for the G5c invariant:
    /// `NoStorageAccess` (where keyring maps a dismissed unlock Prompt) is
    /// NON-transient, so the PlatformFailure retry path can NEVER be reached by
    /// a locked/prompt-dismissed store — no retry can re-pop the dialog.
    #[test]
    fn is_transient_no_storage_access_is_not_retried_g5c() {
        let nsa: Box<dyn std::error::Error + Send + Sync> = "store locked / prompt dismissed".into();
        assert!(
            !is_transient(&keyring::Error::NoStorageAccess(nsa)),
            "NoStorageAccess (dismissed-prompt / locked) must be NON-transient \
             so no retry re-pops the unlock dialog (G5c)"
        );
        let pf: Box<dyn std::error::Error + Send + Sync> = "daemon hiccup".into();
        assert!(
            is_transient(&keyring::Error::PlatformFailure(pf)),
            "only PlatformFailure is transient"
        );
        assert!(!is_transient(&keyring::Error::NoEntry));
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

    /// T-1 (v0.2.83): `keychain_serialize_lock()` holds the PRODUCTION
    /// `keyring.pace` flock while alive (serializing against a running launcher),
    /// and sets `TEST_HOLDS_PRODUCTION_PACE` so a nested `acquire_and_space`
    /// (the test's own real `set`, run on the worker thread) does NOT deadlock.
    ///
    /// Proof of exclusivity: with the guard held, a NON-BLOCKING flock on the
    /// same real pace file from a separate fd fails (EWOULDBLOCK); after drop it
    /// succeeds. Proof of no-self-deadlock: while the guard is held,
    /// `with_cross_process_pace(op)` (the SAME wrapper `secrets::set` uses)
    /// returns promptly — the reentrancy skip fires — rather than hanging on the
    /// held flock. The pace file is the real `<vct_root>/keyring.pace`; touching
    /// it is benign (the launcher creates/locks it anyway) and the keychain lock
    /// serializes every keychain test so only one holds it at a time.
    #[cfg(unix)]
    #[test]
    fn keychain_serialize_lock_holds_pace_and_is_reentrant_for_nested_ops() {
        use std::os::unix::io::AsRawFd;

        let pace_path = crate::paths::vct_root_dir().join("keyring.pace");

        let probe_blocked;
        let nested_ran;
        {
            let _guard = test_serialize::keychain_serialize_lock();
            if !pace_path.exists() {
                eprintln!(
                    "[vct-tests] keyring.pace absent after guard (root unwritable) \
                     — skipping T-1 exclusivity assertion"
                );
                return;
            }
            // (a) A separate fd cannot take the exclusive lock while we hold it.
            let probe = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(&pace_path)
                .expect("open pace file for probe");
            let rc = unsafe {
                libc::flock(probe.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB)
            };
            probe_blocked = rc != 0;
            if rc == 0 {
                unsafe { libc::flock(probe.as_raw_fd(), libc::LOCK_UN) };
            }

            // (b) NO self-deadlock: the SAME wrapper secrets::set uses runs
            // promptly under the held guard (reentrancy skip fires). If the skip
            // were absent, this call would block forever on the held flock and
            // the test would hang (caught by the harness timeout, but the assert
            // documents intent).
            let mut ran = false;
            with_cross_process_pace(|| ran = true);
            nested_ran = ran;
            // _guard drops here → clears the flag, releases the flock.
        }

        // After drop, the flag is cleared and the flock is free.
        assert!(
            !cross_process_pace::TEST_HOLDS_PRODUCTION_PACE
                .load(std::sync::atomic::Ordering::SeqCst),
            "the reentrancy flag must be cleared once the guard drops"
        );
        let probe = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&pace_path)
            .expect("open pace file post-drop");
        let rc_after =
            unsafe { libc::flock(probe.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
        let probe_after = rc_after == 0;
        if rc_after == 0 {
            unsafe { libc::flock(probe.as_raw_fd(), libc::LOCK_UN) };
        }

        assert!(
            probe_blocked,
            "while the guard is held, the production keyring.pace flock must be \
             EXCLUSIVE (a concurrent launcher acquire must block)"
        );
        assert!(
            nested_ran,
            "a nested with_cross_process_pace op must run (no self-deadlock via \
             the reentrancy skip) while the guard holds the pace flock"
        );
        assert!(
            probe_after,
            "after the guard drops, the pace flock must be free for the next \
             acquirer (no leaked lock)"
        );
    }

    /// The Linux lock probe, run for real against whatever Secret-Service (if
    /// any) is on the test host, must NEVER panic and must return one of the
    /// three documented states. Ignored in headless CI (no D-Bus session);
    /// run locally with `--ignored` for a live check.
    #[cfg(target_os = "linux")]
    #[test]
    #[ignore = "requires a live D-Bus session bus; run locally with --ignored"]
    fn live_lock_probe_returns_a_tristate() {
        let _lock = test_serialize::keychain_serialize_lock();
        // No override → the real D-Bus probe. Must be Some(true)/Some(false)/None.
        let r = probe_default_collection_locked();
        assert!(
            matches!(r, Some(true) | Some(false) | None),
            "probe returned an impossible value: {r:?}"
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

    // ─── v0.2.84 (D8.2): SecretReadSession memo tests ─────────────────────────

    /// Dedupe: the SAME (scope, module_id, key) requested twice inside one
    /// session performs exactly ONE underlying keychain read (Entry
    /// construction). The second read is served from the memo — proven by the
    /// entry-construction counter staying flat across the second call.
    #[test]
    fn session_dedupes_repeated_reads_one_underlying_read() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _mock = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        set(scope, "mod", "k1", "v1").unwrap();

        let session = SecretReadSession::new();
        // Counter baseline AFTER opening the session (the set above and any
        // prior activity are excluded).
        let base = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst);

        let first = get(scope, "mod", "k1").unwrap();
        assert_eq!(first.as_deref(), Some("v1"));
        let after_first = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst);
        assert_eq!(
            after_first - base,
            1,
            "first read in a session must perform exactly one underlying read"
        );

        let second = get(scope, "mod", "k1").unwrap();
        assert_eq!(second.as_deref(), Some("v1"));
        let after_second = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst);
        assert_eq!(
            after_second, after_first,
            "second read of the same key in one session must be served from the \
             memo (zero additional underlying reads)"
        );
        assert_eq!(SecretReadSession::memoized_len(), Some(1));
        drop(session);
    }

    /// A genuine key-not-present MISS is also memoized: two reads of a missing
    /// key perform one underlying read, and both return `Ok(None)`.
    #[test]
    fn session_memoizes_genuine_miss() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _mock = for_tests::MockGuard::new();
        let scope = SecretScope::Global;

        let _session = SecretReadSession::new();
        let base = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst);
        assert_eq!(get(scope, "mod", "absent").unwrap(), None);
        assert_eq!(get(scope, "mod", "absent").unwrap(), None);
        let delta = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst) - base;
        assert_eq!(delta, 1, "a memoized miss reads the keychain exactly once");
    }

    /// Generation bump: a `set` DURING an active session invalidates the memo,
    /// so the NEXT read of that key hits the keychain again and sees the new
    /// value (write-through honesty).
    #[test]
    fn session_set_bumps_generation_and_invalidates_memo() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _mock = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        set(scope, "mod", "k1", "v1").unwrap();

        let _session = SecretReadSession::new();
        assert_eq!(get(scope, "mod", "k1").unwrap().as_deref(), Some("v1"));

        // Rotate the value mid-session — this must bump the generation and make
        // the cached "v1" stale.
        set(scope, "mod", "k1", "v2").unwrap();

        let base = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst);
        let after = get(scope, "mod", "k1").unwrap();
        let delta = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst) - base;
        assert_eq!(
            after.as_deref(),
            Some("v2"),
            "post-write read must see the rotated value, not the stale memo"
        );
        assert_eq!(
            delta, 1,
            "the stale memo entry must force a fresh underlying read after a set"
        );
    }

    /// A `delete` mid-session likewise bumps the generation; the next read
    /// re-hits the keychain and now observes the miss.
    #[test]
    fn session_delete_bumps_generation_and_invalidates_memo() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _mock = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        set(scope, "mod", "k1", "v1").unwrap();

        let _session = SecretReadSession::new();
        assert_eq!(get(scope, "mod", "k1").unwrap().as_deref(), Some("v1"));
        delete(scope, "mod", "k1").unwrap();
        assert_eq!(
            get(scope, "mod", "k1").unwrap(),
            None,
            "after a delete the memo is invalidated and the read observes the miss"
        );
    }

    /// v0.2.84 (review F2 — TOCTOU): a concurrent `set` that lands DURING a read
    /// (between the pre-read generation snapshot and the memo store) must
    /// INVALIDATE the entry, so the NEXT read re-hits the keychain and sees the
    /// rotated value — the memo can never serve the value the read observed
    /// *before* the racing write. FAILS WITHOUT THE FIX: if the memo entry were
    /// tagged with the generation read at STORE time, it would capture the
    /// post-bump generation and mask the rotation (the second read would serve
    /// the stale "v1").
    #[test]
    fn session_concurrent_set_mid_read_invalidates_memo_toctou() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _mock = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        set(scope, "mod", "k1", "v1").unwrap();

        let _session = SecretReadSession::new();

        // Arm a one-shot hook that fires INSIDE get_with_context's TOCTOU window
        // (value already read into `outcome`, not yet stored). It rotates the
        // value — a concurrent write landing mid-read — which bumps the
        // generation. The first read still returns the pre-write "v1" (it was
        // captured before the hook fired), but the memo entry must be tagged
        // with the PRE-read generation, so it is stale immediately.
        for_tests::set_mid_read_hook(Box::new(|| {
            set(SecretScope::Global, "mod", "k1", "v2").unwrap();
        }));

        // First read: returns the value observed at read time ("v1"), then the
        // hook rotates to "v2" before the store commits.
        let first = get(scope, "mod", "k1").unwrap();
        assert_eq!(
            first.as_deref(),
            Some("v1"),
            "the read captured the pre-write value (hook fires after the read)"
        );

        // The memo entry, tagged with the PRE-read generation, is now stale
        // (the hook bumped the generation). The next read MUST re-hit the
        // keychain and observe the rotated value.
        let base = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst);
        let second = get(scope, "mod", "k1").unwrap();
        let delta = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst) - base;
        assert_eq!(
            second.as_deref(),
            Some("v2"),
            "a write that raced the read must invalidate the memo — the next \
             read must see the rotated value, not the stale pre-write outcome"
        );
        assert_eq!(
            delta, 1,
            "the stale (pre-read-generation-tagged) entry must force a fresh \
             underlying read after the mid-read write"
        );
    }

    /// F2 companion — the mid-read write need not touch the SAME key. Because the
    /// generation counter is process-wide, a concurrent write to ANY secret
    /// during our read of `k1` bumps it; the pre-read-generation tag then marks
    /// our `k1` entry stale. Proves the invalidation is generation-driven (not
    /// value-diff-driven) and that a racing write to a sibling key does not
    /// silently poison the memo with a value tagged as current.
    #[test]
    fn session_concurrent_sibling_write_mid_read_invalidates_memo() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _mock = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        set(scope, "mod", "k1", "v1").unwrap();

        let _session = SecretReadSession::new();

        // Hook writes a DIFFERENT key mid-read of k1 — still bumps the global
        // generation. k1's value is unchanged, but its memo entry (pre-read gen)
        // must still be invalidated by the generation bump.
        for_tests::set_mid_read_hook(Box::new(|| {
            set(SecretScope::Global, "mod", "sibling", "sv").unwrap();
        }));

        assert_eq!(get(scope, "mod", "k1").unwrap().as_deref(), Some("v1"));

        let base = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst);
        assert_eq!(get(scope, "mod", "k1").unwrap().as_deref(), Some("v1"));
        let delta = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst) - base;
        assert_eq!(
            delta, 1,
            "a generation bump from a sibling-key write during the read must \
             invalidate the k1 entry (generation-driven, not value-driven)"
        );
    }

    /// Drop clears the session: after the guard drops, a repeated read of the
    /// same key performs a FRESH underlying read (the memo did not persist).
    /// Also proves a NEW session snapshots the CURRENT generation.
    #[test]
    fn session_drop_clears_memo() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _mock = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        set(scope, "mod", "k1", "v1").unwrap();

        {
            let _session = SecretReadSession::new();
            assert_eq!(get(scope, "mod", "k1").unwrap().as_deref(), Some("v1"));
            assert_eq!(SecretReadSession::memoized_len(), Some(1));
        } // session dropped here

        // No active session → memo helpers report None and reads are un-memoized.
        assert_eq!(SecretReadSession::memoized_len(), None);

        let session2 = SecretReadSession::new();
        let base = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst);
        assert_eq!(get(scope, "mod", "k1").unwrap().as_deref(), Some("v1"));
        let delta = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst) - base;
        assert_eq!(
            delta, 1,
            "a fresh session must re-read the keychain (the prior session's memo \
             did not survive its Drop)"
        );
        drop(session2);
    }

    /// Outside any session, reads are NEVER memoized: two consecutive reads of
    /// the same key each perform an underlying read (pre-v0.2.84 behaviour).
    #[test]
    fn no_session_means_no_memoization() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _mock = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        set(scope, "mod", "k1", "v1").unwrap();
        // No SecretReadSession in scope.
        let base = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst);
        assert_eq!(get(scope, "mod", "k1").unwrap().as_deref(), Some("v1"));
        assert_eq!(get(scope, "mod", "k1").unwrap().as_deref(), Some("v1"));
        let delta = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst) - base;
        assert_eq!(delta, 2, "without a session, each read hits the keychain");
    }

    /// Nesting: an inner session gets a fresh memo and restores the outer one on
    /// drop — the outer memo is not corrupted.
    #[test]
    fn session_nesting_restores_outer() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _mock = for_tests::MockGuard::new();
        let scope = SecretScope::Global;
        set(scope, "mod", "outer", "vo").unwrap();

        let _outer = SecretReadSession::new();
        assert_eq!(get(scope, "mod", "outer").unwrap().as_deref(), Some("vo"));
        assert_eq!(SecretReadSession::memoized_len(), Some(1));
        {
            let _inner = SecretReadSession::new();
            // Fresh memo for the inner scope.
            assert_eq!(SecretReadSession::memoized_len(), Some(0));
            assert_eq!(get(scope, "mod", "outer").unwrap().as_deref(), Some("vo"));
            assert_eq!(SecretReadSession::memoized_len(), Some(1));
        }
        // Outer memo restored intact (still has its single entry).
        assert_eq!(SecretReadSession::memoized_len(), Some(1));
    }

    /// A3 (non-root + per-project): the memo is keyed by the FULL
    /// `(service, key)` tuple, so within one session two DIFFERENT per-project
    /// scopes (distinct project ids, e.g. non-root projects) that share a key
    /// NAME do NOT collide — each resolves to its own value with its own single
    /// underlying read. Guards against a memo keyed on the bare key name (which
    /// would bleed one project's secret into another).
    #[test]
    fn session_keys_on_full_scope_no_cross_project_collision() {
        let _lock = test_serialize::keychain_serialize_lock();
        let _mock = for_tests::MockGuard::new();
        // Two distinct NON-ROOT per-project scopes, same key name.
        let scope_a = SecretScope::PerProject { project_id: "non-root-proj-a" };
        let scope_b = SecretScope::PerProject { project_id: "non-root-proj-b" };
        set(scope_a, "user", "TOKEN", "value-A").unwrap();
        set(scope_b, "user", "TOKEN", "value-B").unwrap();

        let _session = SecretReadSession::new();
        // First reads: two distinct tuples → two underlying reads, correct values.
        assert_eq!(get(scope_a, "user", "TOKEN").unwrap().as_deref(), Some("value-A"));
        assert_eq!(get(scope_b, "user", "TOKEN").unwrap().as_deref(), Some("value-B"));
        assert_eq!(
            SecretReadSession::memoized_len(),
            Some(2),
            "distinct per-project scopes must occupy distinct memo slots"
        );

        // Repeat reads: served from memo, each still its OWN value (no bleed).
        let base = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst);
        assert_eq!(get(scope_a, "user", "TOKEN").unwrap().as_deref(), Some("value-A"));
        assert_eq!(get(scope_b, "user", "TOKEN").unwrap().as_deref(), Some("value-B"));
        let delta = ENTRY_CONSTRUCTION_COUNT.load(std::sync::atomic::Ordering::SeqCst) - base;
        assert_eq!(delta, 0, "both repeat reads must be memo hits (zero new reads)");
    }

    /// STRUCTURAL never-persist pin (D8.2 hard invariant): the session module
    /// stores secret values ONLY inside the thread-local `SECRET_SESSION`
    /// `SessionState` — never in a file, never in a value-bearing `static`. This
    /// pin greps the SOURCE of this module so a future edit that persists the
    /// memo (a file write, or a `static` value store) trips it. It complements
    /// the behavioural Drop-clears test above with a source-shape guarantee.
    #[test]
    fn session_is_memory_only_no_persistence() {
        let src = include_str!("secrets.rs");

        // Analyse ONLY the session region between two source sentinels, so this
        // test's own body (which necessarily names the forbidden tokens) is
        // excluded from every scan. The region spans the `struct SessionState`
        // declaration through the end-of-session marker; every session helper
        // (memo lookup/store, the `SecretReadSession` RAII guard, the
        // thread_local slot) lives inside it.
        let start = src
            .find("struct SessionState")
            .expect("SessionState marker present");
        let end = src
            .find("// v0.2.72 (P9) rationale")
            .expect("end-of-session-region marker present");
        assert!(end > start, "session region markers must be ordered");
        let session_region = &src[start..end];

        // Guard 1: the session memo store must be a THREAD-LOCAL, not a plain
        // cross-thread `static`. The only `SECRET_SESSION:` declaration in the
        // region must sit INSIDE a `thread_local!` block (a plain module-level
        // `static SECRET_SESSION: RefCell<…>` wouldn't even compile — RefCell is
        // !Sync — but the source pin documents the intent explicitly). Needle
        // assembled from SPLIT literals so this test's own body can't match the
        // scan (mirrors the P9 guard idiom).
        let tl_idx = session_region
            .find("thread_local!")
            .expect("the session memo must live in a thread_local! block");
        let decl_needle = String::from("SECRET_SESSION") + ":";
        let decl_idx = session_region
            .find(decl_needle.as_str())
            .expect("SECRET_SESSION slot must be declared in the region");
        assert!(
            tl_idx < decl_idx,
            "SECRET_SESSION must be declared INSIDE the thread_local! block \
             (a plain cross-thread `static` would outlive the request and cross \
             threads — forbidden)"
        );

        // Guard 2: no file-write / filesystem API is used anywhere in the
        // SESSION machinery. The pace file legitimately writes a TIMESTAMP (not
        // a secret) but that lives OUTSIDE this region; the session helpers must
        // touch no filesystem — a secret memo is MEMORY-ONLY.
        for forbidden in [
            "std::fs::",
            "File::",
            "OpenOptions",
            "write!(",
            "fs::write",
            ".flush()",
        ] {
            assert!(
                !session_region.contains(forbidden),
                "session region must not perform filesystem I/O; found {forbidden:?} \
                 — a secret memo must be MEMORY-ONLY (D8.2)"
            );
        }

        // Guard 3: the generation counter exists (write-through invalidation is
        // wired) and set/delete bump it. Split-literal needle so the assert text
        // itself can't satisfy the whole-file scan.
        let gen_static_needle = String::from("static ") + "SECRET_GENERATION";
        assert!(
            src.contains(gen_static_needle.as_str())
                && src.contains("fn bump_secret_generation"),
            "the write-through generation counter must exist and be bumped"
        );
    }
}
