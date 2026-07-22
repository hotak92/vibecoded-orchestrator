// AGPL-3.0 — part of the VibeCoded Orchestrator launcher core.
//
//! Process-wide persistent Secret-Service connection for the Linux keychain
//! arm.
//!
//! ─── Why this module exists ────────────────────────────────────────────────
//!
//! The `keyring` crate's secret-service backend (v3.6.3, `sync-secret-service`)
//! opens a FRESH D-Bus Secret-Service session on EVERY operation:
//! `SsCredential::set_secret` and `map_matching_items` (which backs get/delete)
//! each call `SecretService::connect(EncryptionType::Plain)`, negotiate a
//! session, do the op, then drop the connection — one connect + one client
//! disconnect per op. Across an "Update all projects" run over several projects
//! that is dozens of connect/disconnect cycles, each one a chance for a
//! client-disconnect-mid-dispatch race in the OS Secret-Service daemon.
//!
//! This module holds ONE lazily-created, process-wide `SecretService`
//! connection (mutex-guarded, reconnect-on-broken) that the get / set / delete
//! ops AND the Background lock probe (`probe_default_collection_locked`, K-2)
//! reuse, so across an "Update all projects" run the daemon sees a long-lived
//! client session reused across those ops rather than a fresh connect +
//! disconnect for each one. Reusing the session + one graceful close on
//! shutdown = fewer disconnect races. Two caveats keep this HONEST rather than
//! absolute:
//!   * the probe keeps an EPHEMERAL 0-max-prompt-timeout session as a FALLBACK,
//!     used only when the shared connect fails (so the Background gate is never
//!     left blind) — under normal operation the shared session is reused;
//!   * the shared connection can still be torn down out from under us, so every
//!     op reconnects-on-broken.
//! This is a MITIGATION of an upstream Secret-Service fragility, not a
//! correctness guarantee: the caller's pacing / backoff / bounded-timeout guards
//! remain the load-bearing safety net.
//!
//! ─── On-disk compatibility (load-bearing) ──────────────────────────────────
//!
//! The item attribute scheme, label format, service-wide + legacy-default
//! collection search, ambiguity handling, and item creation here are a faithful
//! port of `keyring` 3.6.3's `secret_service` backend so that items previously
//! written by `keyring::Entry` resolve UNCHANGED and items written here are
//! readable by any future `keyring::Entry` path. Concretely:
//!   * attributes: `service`, `username`, `target` (default `"default"`),
//!     `application` = `"rust-keyring"`.
//!   * label: `"{user}@{service}:{target} (keyring v{VER})"` where `VER` tracks
//!     the keyring crate version we are compatible with.
//!   * secret bytes: the value's UTF-8 bytes with content-type `text/plain`;
//!     reads decode via `String::from_utf8` (identical to keyring's
//!     `decode_password`).
//!   * search: match on `(target, service, username)`; on a zero-count match in
//!     the default target, fall back to a default-collection search WITHOUT the
//!     `target` attribute (keyring's v1-legacy compatibility path).
//! If the keyring crate's on-disk scheme changes, `KEYRING_COMPAT_VERSION` and
//! this port must be revisited together.
//!
//! ─── Scope ─────────────────────────────────────────────────────────────────
//!
//! Linux-only (`cfg(target_os = "linux")`). The Windows / macOS keychain arms
//! keep using `keyring::Entry` (their native backends do not exhibit the
//! connect-per-op daemon fragility this addresses). The caller (`secrets.rs`)
//! cfg-gates which path it takes.

#![cfg(target_os = "linux")]

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use dbus_secret_service::{EncryptionType, SecretService};

/// The keyring-crate version whose on-disk secret-service scheme this port is
/// compatible with. Kept as a constant (not `env!`) because it describes the
/// EXTERNAL crate we interoperate with, not this crate's own version. This must
/// track the exact pinned `keyring` dependency version so a new item's label
/// matches keyring's byte-for-byte: keyring stamps `(keyring v{CARGO_PKG_VERSION})`
/// and the pinned dep is 3.6.3 (M-6 — verified on-host that real keyring labels
/// end "(keyring v3.6.3)"). Bump when the `keyring` dependency version bumps AND
/// when its secret-service backend changes its attribute or label scheme (verify
/// against `keyring`'s `SsCredential`).
const KEYRING_COMPAT_VERSION: &str = "3.6.3";

/// The `application` attribute keyring stamps on every item it creates. Matched
/// verbatim so our items are indistinguishable from keyring-written ones.
const APPLICATION_ATTR: &str = "rust-keyring";

/// A keychain operation error, kept independent of `keyring::Error` /
/// `dbus_secret_service::Error` so `secrets.rs` can classify it uniformly.
/// Carries only a reason category + a metadata-only detail string — NEVER a
/// secret value.
#[derive(Debug)]
pub(crate) enum SsOpError {
    /// A genuine key-not-present miss (maps to keyring's `NoEntry`).
    NoEntry,
    /// A transient daemon-side failure (D-Bus hiccup, mid-respawn). The caller's
    /// `retry_with_backoff` treats this as retryable, mirroring how it treats
    /// `keyring::Error::PlatformFailure`.
    Transient(String),
    /// A permanent failure (locked store, dismissed prompt, ambiguous match,
    /// bad encoding). NOT retried — retrying would not change the outcome (and,
    /// for a locked/prompt case, must never re-pop the unlock dialog).
    Permanent(String),
}

impl std::fmt::Display for SsOpError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SsOpError::NoEntry => write!(f, "no matching keychain entry"),
            SsOpError::Transient(d) => write!(f, "{d}"),
            SsOpError::Permanent(d) => write!(f, "{d}"),
        }
    }
}

impl std::error::Error for SsOpError {}

impl SsOpError {
    /// Whether the caller's retry loop should retry this error. Only the
    /// transient class is retryable — the same contract `secrets.rs::is_transient`
    /// applies to `keyring::Error::PlatformFailure`.
    pub(crate) fn is_transient(&self) -> bool {
        matches!(self, SsOpError::Transient(_))
    }
}

/// Map a raw `dbus_secret_service::Error` to our transient/permanent split,
/// mirroring keyring's `decode_error`: `Locked` / `NoResult` / `Prompt` are
/// non-transient (they map to keyring's `NoStorageAccess`, which `is_transient`
/// treats as permanent so no retry re-pops a prompt); everything else is a
/// platform failure (transient).
fn classify(err: dbus_secret_service::Error) -> SsOpError {
    use dbus_secret_service::Error as E;
    match err {
        E::Locked | E::NoResult | E::Prompt => SsOpError::Permanent(err.to_string()),
        other => SsOpError::Transient(other.to_string()),
    }
}

/// Test-visible count of how many times a NEW `SecretService` connection was
/// actually created (the "factory"). A persistent connection means this stays
/// at 1 across many ops (plus one more per reconnect). Pinned by the
/// connection-reuse tests. Always compiled (cheap atomic) so the count is
/// available to the caller's tests without a separate cfg.
pub(crate) static CONNECTION_FACTORY_COUNT: AtomicU64 = AtomicU64::new(0);

/// TEST/DEBUG seam: force the NEXT `with_connection` op to observe a broken
/// connection so the reconnect-on-broken path is exercised deterministically
/// without tearing down a real daemon session. One-shot: consumed (swapped back
/// to `false`) when it fires. `false` = no injection pending; `true` = inject
/// once on the next op (N-1).
#[cfg(any(test, debug_assertions))]
static INJECT_BROKEN_ONCE: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);

/// TEST seam: arm a one-shot broken-connection injection. The next
/// `with_connection` call drops the held connection (as if the daemon closed
/// it) and reconnects before running the op — so the factory count increments
/// and the op still succeeds against the fresh connection.
///
/// `#[cfg(test)]` (not `any(test, debug_assertions)`): the ONLY caller is the
/// live-smoke test. The read side (`INJECT_BROKEN_ONCE.swap` in
/// `with_connection`) stays `any(test, debug_assertions)` so a plain debug build
/// still compiles — it just always observes `false` (no injection), which is
/// exactly the production behaviour.
#[cfg(test)]
pub(crate) fn inject_broken_connection_once() {
    INJECT_BROKEN_ONCE.store(true, Ordering::SeqCst);
}

/// The process-wide held connection. `None` until first use or after a
/// shutdown / broken-connection reset. Guarded by a `Mutex` so at most one op
/// touches the daemon session at a time (the caller ALSO serialises via its
/// pacing flock + single keychain worker thread; this mutex is the in-module
/// backstop that keeps the shared `SecretService` sound).
static CONNECTION: Mutex<Option<SecretService>> = Mutex::new(None);

// ─── Reuse + reconnect state machine (SSOT, connection-type-generic) ──────────
//
// The reuse/reconnect DECISION LOGIC lives once in `run_reusing_connection`,
// generic over the connection type `C` — so the daemon-free unit tests exercise
// the EXACT same control flow the real path runs, with a fake `C` (a counter),
// rather than a mirror that could drift. The real `with_connection` is a thin
// instantiation with `C = SecretService`.

/// Run `op` against a reused, lazily-created connection held in `slot`,
/// creating it via `connect` on first use and RECONNECTING ONCE if the held
/// connection is (or is injected as) broken. `factory_count` is bumped on every
/// successful (re)connect.
///
/// Policy (two attempts): on the FIRST attempt, a transient connect failure OR
/// a transient op failure drops the held connection and retries once (a stale
/// session may have gone bad); the SECOND attempt's outcome propagates.
/// Permanent errors (locked / prompt-dismissed / no-entry) propagate
/// immediately without a reconnect. `inject_broken` (checked once, up front)
/// discards any held connection before the first attempt so the reconnect path
/// is observable deterministically.
fn run_reusing_connection<C, T>(
    slot: &mut Option<C>,
    factory_count: &AtomicU64,
    inject_broken: bool,
    mut connect: impl FnMut() -> Result<C, SsOpError>,
    mut op: impl FnMut(&C) -> Result<T, SsOpError>,
) -> Result<T, SsOpError> {
    // A one-shot broken-connection injection discards any held connection so the
    // ensure-connected step must rebuild (factory count increments).
    if inject_broken {
        *slot = None;
    }

    for attempt in 0..2 {
        // Ensure a live connection is present (lazy create / reconnect).
        if slot.is_none() {
            match connect() {
                Ok(c) => {
                    factory_count.fetch_add(1, Ordering::SeqCst);
                    *slot = Some(c);
                }
                Err(e) => {
                    // First attempt + transient: try once more; else propagate.
                    // N-2: this retry is IMMEDIATE (two back-to-back connects in
                    // one closure) — deliberately no inter-attempt sleep here.
                    // It is bounded at exactly 2 connects, and the CALLER's
                    // `retry_with_backoff` (150ms-paced, up to 3 attempts) is the
                    // layer that spaces out repeated failures; adding a sleep
                    // here would slow the common single-transient-blip recovery
                    // for no additional safety.
                    if attempt == 0 && e.is_transient() {
                        continue;
                    }
                    return Err(e);
                }
            }
        }

        let conn = slot.as_ref().expect("connection ensured above");
        match op(conn) {
            Ok(v) => return Ok(v),
            Err(e) => {
                // Transient failure on the FIRST attempt may mean the held
                // session went stale: drop it and reconnect once. Permanent
                // errors (and any failure on the retry) propagate.
                if attempt == 0 && e.is_transient() {
                    *slot = None;
                    continue;
                }
                return Err(e);
            }
        }
    }
    // The loop always returns inside the body (both attempts either return, or
    // the first `continue`s into the second iteration which returns).
    unreachable!("run_reusing_connection loop returns on every path")
}

/// Run `op` against the shared, persistent Secret-Service connection. Thin
/// instantiation of the generic reuse/reconnect SSOT (`run_reusing_connection`)
/// with the real `SecretService` connector. The connection is held only for the
/// duration of `op` (Collection / Item handles borrow `&SecretService`), so all
/// D-Bus work happens inside the mutex-guarded scope. `op` receives
/// `&SecretService` and returns `Result<T, SsOpError>`.
fn with_connection<T>(
    op: impl FnMut(&SecretService) -> Result<T, SsOpError>,
) -> Result<T, SsOpError> {
    let mut guard = CONNECTION.lock().unwrap_or_else(|p| p.into_inner());

    #[cfg(any(test, debug_assertions))]
    let inject = INJECT_BROKEN_ONCE.swap(false, Ordering::SeqCst);
    #[cfg(not(any(test, debug_assertions)))]
    let inject = false;

    run_reusing_connection(
        &mut guard,
        &CONNECTION_FACTORY_COUNT,
        inject,
        || SecretService::connect(EncryptionType::Plain).map_err(classify),
        op,
    )
}

/// Best-effort graceful close of the persistent connection. Drops the held
/// `SecretService` (its `Drop` closes the D-Bus session cleanly), so a
/// subsequent op lazily reconnects. Called from the hub shutdown path and the
/// launcher exit event.
///
/// EXIT MUST NEVER STALL. The keychain worker holds `CONNECTION` for the FULL
/// duration of an op, and an op can include `ensure_unlocked` / `unlock`, which
/// can raise a user unlock prompt; `SecretService::connect(Plain)` sets no
/// max-prompt-timeout, so that prompt wait is unbounded (only plain D-Bus method
/// calls are bounded at 2s). A caller `KEYCHAIN_OP_TIMEOUT` bounds the CALLER,
/// not the worker — so if we took a BLOCKING `CONNECTION.lock()` here, quitting
/// while an interactive unlock prompt is open would block `RunEvent::Exit` /
/// hub shutdown until the user answers the prompt. To guarantee exit is bounded
/// we `try_lock()` with a short deadline: if we win the lock we drain (a real
/// graceful close); if the worker is mid-op past the deadline we SKIP the drain
/// and let the process teardown close the socket abruptly — which is exactly
/// the pre-persistent-connection behaviour, and the "best-effort" the plan
/// specifies. Idempotent; on a poisoned mutex we still clear the slot.
pub(crate) fn shutdown() {
    // Bounded, best-effort drain. `try_lock` never parks, so we can never
    // inherit a mid-op prompt wait. Poll briefly (≤ SHUTDOWN_DRAIN_DEADLINE) so
    // a just-finishing op still gets a clean close, but a genuinely stuck op
    // (open unlock prompt) is left to the abrupt teardown rather than stalling
    // exit.
    let deadline = std::time::Instant::now() + SHUTDOWN_DRAIN_DEADLINE;
    loop {
        match CONNECTION.try_lock() {
            Ok(mut guard) => {
                // Dropping the SecretService closes its D-Bus connection
                // cleanly — one graceful client disconnect at a controlled
                // moment instead of an abrupt teardown mid-op.
                *guard = None;
                return;
            }
            Err(std::sync::TryLockError::Poisoned(p)) => {
                // Poisoned: an op panicked mid-drain. Recover the guard and
                // still clear the slot (the connection is suspect anyway).
                *p.into_inner() = None;
                return;
            }
            Err(std::sync::TryLockError::WouldBlock) => {
                if std::time::Instant::now() >= deadline {
                    // Worker is still mid-op past the deadline (e.g. parked on
                    // an unlock prompt). SKIP the graceful drain — exiting
                    // without it degrades to today's abrupt-close behaviour and
                    // never stalls exit. The OS reclaims the D-Bus socket on
                    // process teardown.
                    return;
                }
                std::thread::sleep(SHUTDOWN_DRAIN_POLL);
            }
        }
    }
}

/// Upper bound on how long [`shutdown`] will wait to win the connection lock
/// before giving up the graceful drain. Small enough that exit is effectively
/// immediate; large enough that a normally-finishing in-flight op yields the
/// lock and gets a clean close. Deliberately well under the "never block exit
/// >~1s" budget.
const SHUTDOWN_DRAIN_DEADLINE: std::time::Duration = std::time::Duration::from_millis(250);

/// Poll interval while [`shutdown`] waits for the lock. `try_lock` never parks,
/// so we sleep briefly between attempts rather than busy-spinning.
const SHUTDOWN_DRAIN_POLL: std::time::Duration = std::time::Duration::from_millis(10);

/// Build the keyring-compatible item attribute map for a (service, key) pair.
/// `key` is the secret-service `username`; `service` is the full VCT service
/// namespace. The `target` is always `"default"` (this crate never creates
/// entries in a non-default collection), matching how `secrets.rs` uses
/// `keyring::Entry::new(service, key)` (which defaults the target to
/// `"default"`).
fn item_attributes<'a>(service: &'a str, key: &'a str) -> HashMap<&'a str, &'a str> {
    let mut attrs = HashMap::new();
    attrs.insert("service", service);
    attrs.insert("username", key);
    attrs.insert("target", "default");
    attrs.insert("application", APPLICATION_ATTR);
    attrs
}

/// Search attributes: the subset we match on. When `omit_target` is true we
/// drop the `target` key (the legacy default-collection fallback that finds
/// v1-style items written without a `target` attribute).
fn search_attributes<'a>(service: &'a str, key: &'a str, omit_target: bool) -> HashMap<&'a str, &'a str> {
    let mut attrs = HashMap::new();
    if !omit_target {
        attrs.insert("target", "default");
    }
    attrs.insert("service", service);
    attrs.insert("username", key);
    attrs
}

/// The keyring item label for a (service, key): `"{user}@{service}:{target}
/// (keyring v{VER})"`. Only used when CREATING a new item; the exact string is
/// cosmetic (GUI display only — labels are never searched) but kept
/// byte-identical to keyring's (which stamps `(keyring v3.6.3)`) so mixed
/// launcher-written and keyring-written items look consistent in Seahorse (M-6).
fn item_label(service: &str, key: &str) -> String {
    format!("{key}@{service}:default (keyring v{KEYRING_COMPAT_VERSION})")
}

/// Locate the unique item matching (service, key) against the given connection.
/// Mirrors keyring's `map_matching_items` with `require_unique = true`: zero
/// matches → `NoEntry` (after the legacy default-collection fallback), multiple
/// matches → permanent "ambiguous", one match → the item is handed to `f`.
///
/// Unlock semantics match keyring exactly: on the MAIN service-wide search the
/// single match is `ensure_unlocked()`ed before `f` (keyring `unlock()`s locked
/// matches, no-ops on unlocked ones); on the LEGACY default-collection fallback
/// `f` is applied DIRECTLY with NO unlock (keyring's `map_matching_legacy_items`
/// never unlocks — M-5).
///
/// The closure form keeps the borrow of the returned `Item` inside the caller's
/// scope (Item borrows `&SecretService`), so we hand the resolved item to `f`
/// rather than returning it.
fn with_matching_item<T>(
    ss: &SecretService,
    service: &str,
    key: &str,
    f: impl FnOnce(&dbus_secret_service::Item<'_>) -> Result<T, SsOpError>,
) -> Result<T, SsOpError> {
    let attrs = search_attributes(service, key, false);
    let found = ss.search_items(attrs).map_err(classify)?;
    let count = found.unlocked.len() + found.locked.len();

    if count == 0 {
        // Legacy fallback: search the default collection WITHOUT the target
        // attribute (finds keyring-v1-style items). Matches keyring 3.2.1+.
        let collection = ss.get_default_collection().map_err(classify)?;
        let legacy_attrs = search_attributes(service, key, true);
        let legacy = collection.search_items(legacy_attrs).map_err(classify)?;
        return match legacy.len() {
            0 => Err(SsOpError::NoEntry),
            1 => {
                // M-5: keyring's `map_matching_legacy_items` applies `f` to the
                // matched item DIRECTLY — it never `unlock()`s a legacy item
                // (only the MAIN search unlocks its locked matches;
                // keyring-3.6.3 secret_service.rs:417-420). Matching that: do
                // NOT `ensure_unlocked()` here. A locked legacy item lets the op
                // fail `Locked` → `Permanent`/`NoStorageAccess`, exactly as
                // keyring behaves — so the Interactive path never raises an
                // unlock prompt on the legacy branch where keyring would only
                // have errored.
                f(&legacy[0])
            }
            _ => Err(SsOpError::Permanent(
                "ambiguous keychain match (legacy default collection)".to_string(),
            )),
        };
    }

    if count > 1 {
        return Err(SsOpError::Permanent(
            "ambiguous keychain match (multiple items)".to_string(),
        ));
    }

    // Exactly one match, across unlocked + locked.
    let item = found
        .unlocked
        .first()
        .or_else(|| found.locked.first())
        .expect("count == 1 guarantees one item");
    item.ensure_unlocked().map_err(classify)?;
    f(item)
}

/// Read a secret by (service, key). Returns `Ok(Some(value))` on a hit,
/// `Err(SsOpError::NoEntry)` on a genuine miss (the caller maps this to
/// `Ok(None)`). Uses the shared persistent connection.
pub(crate) fn read_secret(service: &str, key: &str) -> Result<String, SsOpError> {
    with_connection(|ss| {
        with_matching_item(ss, service, key, |item| {
            let bytes = item.get_secret().map_err(classify)?;
            String::from_utf8(bytes).map_err(|_| {
                SsOpError::Permanent("keychain value is not valid UTF-8".to_string())
            })
        })
    })
}

/// Write a secret by (service, key). If a unique matching item exists, its
/// secret is updated in place (preferred, matches keyring); otherwise a new
/// item is created in the default collection with the keyring-compatible
/// attributes + label. Uses the shared persistent connection.
pub(crate) fn write_secret(service: &str, key: &str, value: &str) -> Result<(), SsOpError> {
    with_connection(|ss| {
        // Prefer updating an existing unique item (keyring semantics).
        let update = with_matching_item(ss, service, key, |item| {
            item.set_secret(value.as_bytes(), "text/plain")
                .map_err(classify)
        });
        match update {
            Ok(()) => Ok(()),
            // No existing item → create one in the default collection.
            Err(SsOpError::NoEntry) => {
                let collection = ss.get_default_collection().map_err(classify)?;
                if collection.is_locked().map_err(classify)? {
                    collection.unlock().map_err(classify)?;
                }
                let label = item_label(service, key);
                let attrs = item_attributes(service, key);
                collection
                    .create_item(
                        label.as_str(),
                        attrs,
                        value.as_bytes(),
                        true, // replace
                        "text/plain",
                    )
                    .map_err(classify)?;
                Ok(())
            }
            Err(e) => Err(e),
        }
    })
}

/// Delete a secret by (service, key). A genuine miss is reported as
/// `Err(SsOpError::NoEntry)`; the caller treats that as success (idempotent
/// delete), mirroring how the keyring path maps `NoEntry` to `Ok(())`. Uses the
/// shared persistent connection.
pub(crate) fn remove_secret(service: &str, key: &str) -> Result<(), SsOpError> {
    with_connection(|ss| {
        with_matching_item(ss, service, key, |item| item.delete().map_err(classify))
    })
}

/// Read the `Locked` state of the default collection through the SHARED
/// persistent connection — the same session the get/set/delete ops reuse — so
/// the Background lock probe no longer opens a fresh throw-away Secret-Service
/// session per read (K-2: that connect/disconnect churn is what WP-K exists to
/// cut).
///
/// PROMPT SAFETY (why routing this through the no-max-prompt-timeout shared
/// connection is safe): the probe is exactly two pure D-Bus reads —
/// `get_default_collection()` is a `ReadAlias` method call and `is_locked()` is
/// a `Get` of the `Locked` property (dbus-secret-service 4.1.0
/// `collection.rs:41`, `lib.rs:261`). NEITHER unlocks NOR raises a prompt, so
/// the `max_prompt_timeout = 0` isolation the ephemeral probe session used is
/// not needed here — there is no prompt for a timeout to cancel. (The shared
/// connection's `connect(Plain)` sets `timeout: None`; that only matters for op
/// paths that call `unlock`/`ensure_unlocked`, which the probe never does.)
///
/// Returns `Ok(Some(locked))` on a definite answer, `Ok(None)` when there is no
/// default collection (`NoResult` → UNKNOWN), and `Err(_)` on a D-Bus failure so
/// the caller can fall back to the ephemeral 0-timeout probe. Runs inside the
/// caller's bounded-timeout worker like every other op.
pub(crate) fn probe_default_collection_locked() -> Result<Option<bool>, SsOpError> {
    with_connection(|ss| {
        match ss.get_default_collection() {
            Ok(collection) => collection.is_locked().map(Some).map_err(classify),
            // No default collection → we cannot determine a lock state; report
            // UNKNOWN rather than an error (nothing to reconnect for).
            Err(dbus_secret_service::Error::NoResult) => Ok(None),
            Err(e) => Err(classify(e)),
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Attribute + label parity with keyring 3.6.3's `SsCredential`: the exact
    /// controlled attributes and label format are what make our items
    /// indistinguishable from keyring-written ones. If keyring's scheme ever
    /// drifts, this pin flags that the port needs revisiting.
    #[test]
    fn attributes_and_label_match_keyring_scheme() {
        let attrs = item_attributes("vct.p1.mod", "k");
        assert_eq!(attrs.get("service"), Some(&"vct.p1.mod"));
        assert_eq!(attrs.get("username"), Some(&"k"));
        assert_eq!(attrs.get("target"), Some(&"default"));
        assert_eq!(attrs.get("application"), Some(&"rust-keyring"));

        // Search WITH target includes it; the legacy fallback omits it.
        let with_t = search_attributes("vct.p1.mod", "k", false);
        assert_eq!(with_t.get("target"), Some(&"default"));
        let no_t = search_attributes("vct.p1.mod", "k", true);
        assert!(!no_t.contains_key("target"));
        assert_eq!(no_t.get("service"), Some(&"vct.p1.mod"));

        // Label format matches keyring's `{user}@{service}:{target} (keyring v..)`
        // byte-for-byte — keyring 3.6.3 stamps `(keyring v3.6.3)` via
        // `env!("CARGO_PKG_VERSION")` (M-6). Kept identical so mixed launcher-
        // and keyring-written items render consistently in Seahorse.
        let label = item_label("vct.p1.mod", "k");
        assert_eq!(label, "k@vct.p1.mod:default (keyring v3.6.3)");
    }

    /// The transient/permanent split matches keyring's `decode_error`: a locked
    /// / prompt-dismissed / no-result error is NON-transient (so no retry
    /// re-pops the unlock dialog), everything else is transient (retryable).
    #[test]
    fn classify_matches_keyring_decode_error_split() {
        assert!(matches!(
            classify(dbus_secret_service::Error::Locked),
            SsOpError::Permanent(_)
        ));
        assert!(matches!(
            classify(dbus_secret_service::Error::Prompt),
            SsOpError::Permanent(_)
        ));
        assert!(matches!(
            classify(dbus_secret_service::Error::NoResult),
            SsOpError::Permanent(_)
        ));
        assert!(matches!(
            classify(dbus_secret_service::Error::Parse),
            SsOpError::Transient(_)
        ));
        // is_transient agrees.
        assert!(classify(dbus_secret_service::Error::Parse).is_transient());
        assert!(!classify(dbus_secret_service::Error::Locked).is_transient());
    }

    /// `shutdown()` is idempotent and clears the real held slot: calling it with
    /// no connection present is a no-op, and after it the slot is `None` (so the
    /// next op reconnects). Exercises the actual production drain entry point on
    /// the real `CONNECTION` static (no daemon needed — the slot starts empty).
    #[test]
    fn shutdown_is_idempotent_and_clears_slot() {
        // Force a known state: no connection.
        {
            let mut g = CONNECTION.lock().unwrap_or_else(|p| p.into_inner());
            *g = None;
        }
        shutdown(); // no-op on an empty slot — must not panic.
        shutdown(); // still idempotent.
        let g = CONNECTION.lock().unwrap_or_else(|p| p.into_inner());
        assert!(g.is_none(), "shutdown must leave the connection slot empty");
    }

    /// M-5 STRUCTURAL PIN: keyring's `map_matching_legacy_items` applies `f`
    /// DIRECTLY and never unlocks a legacy item — only the MAIN service-wide
    /// search unlocks its locked matches. So `item.ensure_unlocked()` must appear
    /// EXACTLY ONCE in this module (the main-search branch of `with_matching_item`);
    /// re-adding it to the legacy branch would diverge from keyring and could
    /// raise an unlock prompt on the Interactive path where keyring only errors.
    /// The scan strips `//` comments (so the M-5 explanatory comment doesn't
    /// count) and assembles the needle from split literals so it never
    /// self-matches.
    #[test]
    fn legacy_branch_does_not_unlock_matching_keyring() {
        let src = include_str!("secrets_ss_connection.rs");
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
        // Needle for the item unlock CALL, assembled from split literals so no
        // line in THIS test's own code (comment-stripped) can form it.
        let unlock_needle = String::from("item.ensure_") + "unlocked(";
        let hits = code_flat.matches(unlock_needle.as_str()).count();
        assert_eq!(
            hits, 1,
            "the item-unlock call must appear exactly once (the MAIN-search \
             branch of with_matching_item); found {hits}. keyring never unlocks a \
             LEGACY-fallback item (secret_service.rs:417-420) — re-adding an \
             unlock to the legacy branch diverges from keyring and risks an \
             unlock prompt where keyring would only error (M-5)"
        );
    }

    /// K-1: `shutdown()` NEVER STALLS even when the connection lock is held by a
    /// concurrent op. The keychain worker holds `CONNECTION` for the full op
    /// duration, and an op can park on an unbounded user unlock prompt; a
    /// blocking `CONNECTION.lock()` in `shutdown()` would then block exit until
    /// the prompt resolves. The fix uses a bounded `try_lock` with a ≤250ms
    /// deadline. This test holds the lock on another thread for longer than that
    /// deadline and asserts `shutdown()` RETURNS PROMPTLY (well under the held
    /// duration) rather than blocking until the holder releases.
    ///
    /// FAIL-ON-BLOCKING-LOCK: revert `shutdown()` to a blocking `CONNECTION.lock()`
    /// and this test blocks for the FULL hold duration (~600ms), busting the
    /// bound assertion — the exact stall the fix removes.
    #[test]
    fn shutdown_does_not_block_when_connection_is_held() {
        use std::sync::mpsc;
        use std::time::{Duration, Instant};

        // Ensure a known starting state.
        {
            let mut g = CONNECTION.lock().unwrap_or_else(|p| p.into_inner());
            *g = None;
        }

        // Hold the connection lock on a background thread for HOLD_MS — longer
        // than shutdown's drain deadline — simulating a worker parked mid-op
        // (e.g. on an unlock prompt).
        const HOLD_MS: u64 = 600;
        let (acquired_tx, acquired_rx) = mpsc::channel::<()>();
        let (release_tx, release_rx) = mpsc::channel::<()>();
        let holder = std::thread::spawn(move || {
            let _guard = CONNECTION.lock().unwrap_or_else(|p| p.into_inner());
            acquired_tx.send(()).expect("signal lock acquired");
            // Hold until told to release (or the hold window elapses).
            let _ = release_rx.recv_timeout(Duration::from_millis(HOLD_MS));
            // guard drops here
        });
        // Wait until the holder actually owns the lock before we probe shutdown.
        acquired_rx
            .recv_timeout(Duration::from_secs(2))
            .expect("holder thread must acquire the connection lock");

        // Call shutdown() while the lock is HELD. It must give up the drain at
        // its deadline and return, NOT block for the full HOLD_MS.
        let start = Instant::now();
        shutdown();
        let elapsed = start.elapsed();
        assert!(
            elapsed < Duration::from_millis(HOLD_MS),
            "shutdown() must return before the lock holder releases (bounded \
             try_lock), but it took {elapsed:?} (>= {HOLD_MS}ms hold) — a \
             blocking CONNECTION.lock() would stall exit like this"
        );

        // Let the holder finish and clean up.
        let _ = release_tx.send(());
        holder.join().expect("holder thread joins");
        // Drain again now that the lock is free (idempotent).
        shutdown();
        let g = CONNECTION.lock().unwrap_or_else(|p| p.into_inner());
        assert!(g.is_none(), "final drain leaves the slot empty");
    }

    /// DRAIN FORCES RECONNECT: after an op establishes a connection (factory 1),
    /// draining the slot (the exact effect `shutdown()` has on the real
    /// `CONNECTION`) makes the NEXT op reconnect (factory 2). Pins that the drain
    /// actually tears the held connection down rather than leaking it — so the
    /// graceful-close is a real close, not a no-op. Driven on the generic SSOT
    /// with a fake connection (daemon-free) so it is CI-neutral. FAIL-ON-NO-DRAIN:
    /// if the drain did not clear the slot, the second op would REUSE the held
    /// connection and the factory count would stay at 1.
    #[test]
    fn drain_forces_reconnect_on_next_op() {
        let mut slot: Option<FakeConn> = None;
        let factory = AtomicU64::new(0);
        let next_id = std::cell::Cell::new(0u64);
        let connect = || {
            let id = next_id.get();
            next_id.set(id + 1);
            Ok(FakeConn(id))
        };

        // Op 1 establishes the connection.
        run_reusing_connection(&mut slot, &factory, false, connect, |c| Ok(c.0)).unwrap();
        assert_eq!(factory.load(Ordering::SeqCst), 1);
        assert!(slot.is_some(), "op 1 must have established a connection");

        // DRAIN: exactly what `shutdown()` does to the real slot.
        slot = None;

        // Op 2 must reconnect (factory increments, fresh id).
        let id = run_reusing_connection(&mut slot, &factory, false, connect, |c| Ok(c.0)).unwrap();
        assert_eq!(
            factory.load(Ordering::SeqCst),
            2,
            "after a drain the next op must RECONNECT (factory 1 → 2), proving \
             the graceful close is a real teardown, not a no-op"
        );
        assert_eq!(id, 1, "the reconnected op runs against a fresh connection");
    }

    // ─── Reuse + reconnect decision-logic (daemon-free, via the generic SSOT) ──
    //
    // These drive `run_reusing_connection` — the SAME state machine the real
    // `with_connection` instantiates — with a FAKE connection type (a marker),
    // so the reuse (factory-once), reconnect-on-broken, and error-propagation
    // contracts are pinned WITHOUT a live Secret Service. Because they share the
    // production control flow (not a mirror), a regression in the real reconnect
    // policy also breaks these.

    /// A fake connection: an opaque marker so the state machine can hold, reuse,
    /// and reconstruct it. Carries a small id so a test can prove reuse (same id
    /// across ops) vs reconnect (new id after a broken injection).
    #[derive(Debug, PartialEq, Eq)]
    struct FakeConn(u64);

    /// CONNECTION REUSE: across N ops with no failures and no injection, the
    /// connect factory is invoked exactly ONCE — the held connection is reused.
    /// FAIL-ON-PER-OP: if `with_connection` reconstructed per op (the pre-WP-K
    /// churn), this factory count would be N, not 1.
    #[test]
    fn factory_invoked_once_across_many_ops() {
        let mut slot: Option<FakeConn> = None;
        let factory = AtomicU64::new(0);
        let next_id = std::cell::Cell::new(0u64);

        let n = 25;
        for _ in 0..n {
            let out: Result<u64, SsOpError> = run_reusing_connection(
                &mut slot,
                &factory,
                false, // no broken injection
                || {
                    let id = next_id.get();
                    next_id.set(id + 1);
                    Ok(FakeConn(id))
                },
                |c| Ok(c.0), // op just reads the conn id — always succeeds
            );
            assert!(out.is_ok());
        }
        assert_eq!(
            factory.load(Ordering::SeqCst),
            1,
            "the connection factory must be invoked exactly ONCE across {n} ops \
             (reuse); a per-op reconnect would make this {n}"
        );
        // The held connection is the first one (id 0), reused throughout.
        assert_eq!(slot, Some(FakeConn(0)));
    }

    /// RECONNECT ON INJECTED BROKEN: a one-shot broken injection discards the
    /// held connection before the op, so the factory runs again and the op still
    /// succeeds against the fresh connection. Factory count = 2 across two ops
    /// (one initial connect + one reconnect).
    #[test]
    fn reconnect_on_injected_broken_connection() {
        let mut slot: Option<FakeConn> = None;
        let factory = AtomicU64::new(0);
        let next_id = std::cell::Cell::new(0u64);
        let connect = || {
            let id = next_id.get();
            next_id.set(id + 1);
            Ok(FakeConn(id))
        };

        // Op 1: fresh connect (factory → 1), conn id 0.
        let first: Result<u64, SsOpError> =
            run_reusing_connection(&mut slot, &factory, false, connect, |c| Ok(c.0));
        assert_eq!(first.unwrap(), 0);
        assert_eq!(factory.load(Ordering::SeqCst), 1);

        // Op 2 WITH broken injection: held conn discarded → reconnect (factory
        // → 2), conn id 1, op still succeeds against the new connection.
        let second: Result<u64, SsOpError> =
            run_reusing_connection(&mut slot, &factory, true, connect, |c| Ok(c.0));
        assert_eq!(
            second.unwrap(),
            1,
            "after a broken-connection injection the op runs against a FRESH \
             connection (new id)"
        );
        assert_eq!(
            factory.load(Ordering::SeqCst),
            2,
            "a broken-connection injection must force exactly one reconnect"
        );
    }

    /// RECONNECT ON TRANSIENT OP FAILURE: a transient op error on the FIRST
    /// attempt drops the held connection and retries once against a reconnect;
    /// the retry succeeds. Factory = 2 (initial + reconnect), op invoked twice.
    #[test]
    fn transient_op_failure_triggers_one_reconnect_then_succeeds() {
        let mut slot: Option<FakeConn> = None;
        let factory = AtomicU64::new(0);
        let next_id = std::cell::Cell::new(0u64);
        let op_calls = std::cell::Cell::new(0u32);

        let out: Result<u64, SsOpError> = run_reusing_connection(
            &mut slot,
            &factory,
            false,
            || {
                let id = next_id.get();
                next_id.set(id + 1);
                Ok(FakeConn(id))
            },
            |c| {
                let call = op_calls.get();
                op_calls.set(call + 1);
                if call == 0 {
                    // First attempt: transient failure → drop + reconnect.
                    Err(SsOpError::Transient("daemon hiccup".into()))
                } else {
                    Ok(c.0) // second attempt on the fresh connection: success.
                }
            },
        );
        assert_eq!(out.unwrap(), 1, "the retry runs against the reconnected conn");
        assert_eq!(op_calls.get(), 2, "op is attempted twice (initial + retry)");
        assert_eq!(
            factory.load(Ordering::SeqCst),
            2,
            "a transient op failure must trigger exactly one reconnect"
        );
    }

    /// PERMANENT OP FAILURE DOES NOT RECONNECT: a permanent error (locked /
    /// prompt-dismissed) propagates immediately, with NO reconnect and NO retry —
    /// so a locked store can never re-pop the unlock dialog via this layer.
    #[test]
    fn permanent_op_failure_does_not_reconnect() {
        let mut slot: Option<FakeConn> = None;
        let factory = AtomicU64::new(0);
        let op_calls = std::cell::Cell::new(0u32);

        let out: Result<u64, SsOpError> = run_reusing_connection(
            &mut slot,
            &factory,
            false,
            || Ok(FakeConn(0)),
            |_c| {
                op_calls.set(op_calls.get() + 1);
                Err(SsOpError::Permanent("store locked".into()))
            },
        );
        assert!(matches!(out, Err(SsOpError::Permanent(_))));
        assert_eq!(op_calls.get(), 1, "a permanent error must not be retried");
        assert_eq!(
            factory.load(Ordering::SeqCst),
            1,
            "a permanent op failure must NOT trigger a reconnect (no dialog re-pop)"
        );
    }

    /// NoEntry is permanent-classed: it propagates without a reconnect (the
    /// caller maps it to Ok(None) for get / Ok(()) for delete). Guards against a
    /// miss accidentally spinning the reconnect loop.
    #[test]
    fn no_entry_does_not_reconnect() {
        let mut slot: Option<FakeConn> = None;
        let factory = AtomicU64::new(0);
        let op_calls = std::cell::Cell::new(0u32);

        let out: Result<u64, SsOpError> = run_reusing_connection(
            &mut slot,
            &factory,
            false,
            || Ok(FakeConn(0)),
            |_c| {
                op_calls.set(op_calls.get() + 1);
                Err(SsOpError::NoEntry)
            },
        );
        assert!(matches!(out, Err(SsOpError::NoEntry)));
        assert_eq!(op_calls.get(), 1, "a genuine miss must not be retried");
        assert_eq!(factory.load(Ordering::SeqCst), 1);
    }

    /// LIVE SMOKE (ignored; requires an UNLOCKED Secret Service on the host).
    /// Run locally with `cargo test -p vct-launcher-core -- --ignored
    /// live_persistent_connection`. Performs N rapid real reads/writes/deletes
    /// through the shared connection and asserts the factory ran a SMALL,
    /// bounded number of times (ONE per fresh process, plus at most the
    /// reconnects the run itself induces) rather than once-per-op. CI-neutral:
    /// `#[ignore]` keeps it out of the gating run (headless CI has no unlocked
    /// daemon), and it never runs against the user's real keychain unguarded in
    /// the normal suite.
    #[test]
    #[ignore = "requires an unlocked Secret Service; run locally with --ignored"]
    fn live_persistent_connection_reuses_one_session_across_many_ops() {
        // Reset the factory counter and clear any held connection so the count
        // reflects only this run.
        CONNECTION_FACTORY_COUNT.store(0, Ordering::SeqCst);
        shutdown();

        let service = "vct.probe.wpk_live_smoke";
        let key = "smoke_key";
        // Seed a value.
        write_secret(service, key, "smoke-value").expect("write against live daemon");

        // N rapid reads through the shared connection.
        let n = 30;
        for i in 0..n {
            // Halfway through, inject a broken connection so the REAL
            // `with_connection` reconnect path is exercised against the daemon
            // (factory increments once, the op still succeeds).
            if i == n / 2 {
                inject_broken_connection_once();
            }
            let v = read_secret(service, key).expect("read against live daemon");
            assert_eq!(v, "smoke-value");
        }
        // Clean up.
        let _ = remove_secret(service, key);

        // The persistent connection means the factory ran a small, bounded
        // number of times — NOT once per op (which would be > n). Expected: one
        // initial connect + one from the injected reconnect; allow a little
        // slack for daemon hiccups.
        let factory = CONNECTION_FACTORY_COUNT.load(Ordering::SeqCst);
        assert!(
            (2..=5).contains(&factory),
            "the shared connection must be REUSED across {n} ops with exactly one \
             injected reconnect (factory ran {factory} times; expected ~2); a \
             per-op connect would make this > {n}"
        );
        shutdown();
    }

    /// A persistent transient failure across BOTH attempts propagates (bounded
    /// at two attempts here; the CALLER's `retry_with_backoff` applies the full
    /// paced backoff on top). Op invoked twice, one reconnect, then the second
    /// transient error propagates.
    #[test]
    fn persistent_transient_failure_propagates_after_one_reconnect() {
        let mut slot: Option<FakeConn> = None;
        let factory = AtomicU64::new(0);
        let op_calls = std::cell::Cell::new(0u32);

        let out: Result<u64, SsOpError> = run_reusing_connection(
            &mut slot,
            &factory,
            false,
            || Ok(FakeConn(0)),
            |_c| {
                op_calls.set(op_calls.get() + 1);
                Err(SsOpError::Transient("still hiccuping".into()))
            },
        );
        assert!(matches!(out, Err(SsOpError::Transient(_))));
        assert_eq!(op_calls.get(), 2, "bounded at two attempts inside this layer");
        assert_eq!(
            factory.load(Ordering::SeqCst),
            2,
            "one reconnect between the two transient attempts"
        );
    }

    /// M-7 WIRING PIN (env-gated, NOT `#[ignore]`). The daemon-free tests above
    /// drive the GENERIC `run_reusing_connection` with a `FakeConn`, so a
    /// regression INSIDE the production `with_connection` — e.g. resetting the
    /// real `CONNECTION` static per call, or pointing at a fresh `AtomicU64`
    /// instead of `CONNECTION_FACTORY_COUNT` — would leave every default-battery
    /// test green; only the `#[ignore]`d live smoke would catch it. This test
    /// pins that PRODUCTION wiring (real `read_secret` → real `with_connection` →
    /// real `CONNECTION` slot + real `CONNECTION_FACTORY_COUNT`): two real reads
    /// must increment the factory AT MOST ONCE (the slot is reused), never twice.
    ///
    /// It is a normal `#[test]` (so it runs in the DEFAULT `cargo test`, unlike
    /// the `#[ignore]`d smoke) but NO-OPS unless `VCT_TEST_LIVE_KEYCHAIN=1` is
    /// set, so CI stays daemon-free and the user's real keychain is never touched
    /// unguarded. Add `VCT_TEST_LIVE_KEYCHAIN=1` to the release-runbook local
    /// gate to make the real-wiring assertion part of the local battery.
    #[test]
    fn with_connection_reuses_the_real_static_slot_env_gated() {
        if std::env::var("VCT_TEST_LIVE_KEYCHAIN").as_deref() != Ok("1") {
            // Daemon-free default: no-op (the documented wiring pin is the live
            // smoke; this leg activates only on the opt-in local gate).
            return;
        }
        // Real wiring: reset the real static + counter, then do two real reads of
        // a key that need not exist — a MISS still goes through `with_connection`
        // and establishes/reuses the shared `CONNECTION`. The factory must run at
        // most once across the two reads (reuse), never twice (per-op reconnect).
        CONNECTION_FACTORY_COUNT.store(0, Ordering::SeqCst);
        shutdown();
        let service = "vct.probe.wpk_wiring_pin";
        let key = "wiring_key_absent";
        let _ = read_secret(service, key); // may be NoEntry — we only care that it connects
        let after_first = CONNECTION_FACTORY_COUNT.load(Ordering::SeqCst);
        let _ = read_secret(service, key);
        let after_second = CONNECTION_FACTORY_COUNT.load(Ordering::SeqCst);
        assert!(
            after_first >= 1,
            "the first real read must establish the shared connection (factory \
             ran {after_first} times; expected >= 1)"
        );
        assert_eq!(
            after_first, after_second,
            "the SECOND real read must REUSE the shared `CONNECTION` static — the \
             factory count must not increase (was {after_first}, now \
             {after_second}); an increase means `with_connection` is not reusing \
             the real slot"
        );
        shutdown();
    }
}
