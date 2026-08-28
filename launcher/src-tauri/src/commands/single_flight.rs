// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! v0.2.91 decision #26 — keyed, process-wide single-flight claim for
//! long-running destructive work. THE home for "refuse a second concurrent
//! run of X in this process".
//!
//! ## Why this primitive, and why the older neighbours do not fit
//!
//! Three other re-entrancy mechanisms exist and stay where they are:
//!
//!   * `project_setup::setup_in_flight_should_refuse` /
//!     `modules::install_in_flight_should_refuse` — a DB ROW is the lock.
//!     That works because each guards work that already owns a row
//!     (`project_setups`, `module_installs`) with a status and a start
//!     timestamp, which also survives a launcher restart. `update_all_projects`
//!     and `update_orchestrator_at` own no such row: the run is a traversal,
//!     not an entity.
//!   * `self_update::UPSTREAM_FETCH_LOCK` — a `tokio::sync::Mutex` held
//!     across the work, which SERIALISES (the second caller queues, then
//!     runs). Wrong semantics here: a queued second update-all would run the
//!     same destructive traversal a moment later, which is exactly the
//!     outcome the guard exists to prevent.
//!   * `embed_admission`'s `IN_FLIGHT_PAST_GATE` — a COUNTER admitting up to
//!     N concurrent embeds. An admission gate, not mutual exclusion.
//!
//! ## Extracted, not invented (v0.2.91)
//!
//! `projects_v2.rs` already carried this exact mechanism as `MigrateLockGuard`
//! + `MIGRATE_IN_FLIGHT` (DS-F2): a `LazyLock<Mutex<HashSet<String>>>`, RAII,
//! refuse-on-contention, keyed per project. Rather than ship a second copy
//! beside it, that one was moved here and its call site migrated — the
//! keys are namespaced ([`migrate_collections_key`]) so a project id can
//! never collide with an operation name. One mechanism, three call sites.
//!
//! Its conservative POISONING posture came along with it: a poisoned mutex
//! REFUSES rather than recovering. Poisoning would mean a panic while the
//! mutex is held, which cannot happen here (nothing but a set insert/remove
//! runs under it) — but if the impossible occurs, "do nothing rather than
//! guess" is the house rule for a claim protecting a destructive action.
//!
//! ## Deliberately NOT one key for every update
//!
//! `update_all_projects` (manifest-driven bundle reconcile over registered
//! projects) and `update_orchestrator_at` (orchestrator-clone refresh, gated
//! by `validate_source_repo`) are SEPARATE operations on separate targets —
//! see the boundary note at `installer.rs`'s `update_orchestrator_at`. They
//! get separate keys, so guarding one never blocks the other. Do not merge
//! them into a single "an update is running" flag.
//!
//! ## Scope of the guarantee
//!
//! Process-wide, not machine-wide. A second launcher process is already
//! prevented by the single-instance lock; this closes the in-process case the
//! GUI can actually produce (a modal reopened mid-run, a second window, a
//! button double-fire). A separate-process CLI race needs its own defence —
//! the migrate path's orphan-`__staging` recovery is that.

use std::collections::HashSet;
use std::sync::{LazyLock, Mutex};

/// Operation key: the update-all-projects traversal (`projects_v2.rs`).
pub const OP_UPDATE_ALL_PROJECTS: &str = "update_all_projects";

/// Operation key: the orchestrator-clone refresh (`installer.rs`).
pub const OP_UPDATE_ORCHESTRATOR_AT: &str = "update_orchestrator_at";

/// Key prefix for the per-project additive-migration claim (DS-F2). Prefixed
/// so a project id can never collide with an operation name above.
const MIGRATE_COLLECTIONS_PREFIX: &str = "migrate_collections:";

/// Claim key for a project's wet additive schema migration.
pub fn migrate_collections_key(project_id: &str) -> String {
    format!("{}{}", MIGRATE_COLLECTIONS_PREFIX, project_id)
}

/// The set of claims currently held.
static IN_FLIGHT: LazyLock<Mutex<HashSet<String>>> =
    LazyLock::new(|| Mutex::new(HashSet::new()));

/// RAII handle for a claim. Dropping it releases the claim — including on an
/// early `?` return or a panic unwind, which is why a claim cannot leak and
/// strand the operation for the rest of the process's life.
#[derive(Debug)]
pub struct SingleFlightGuard {
    key: String,
}

impl Drop for SingleFlightGuard {
    fn drop(&mut self) {
        if let Ok(mut set) = IN_FLIGHT.lock() {
            set.remove(&self.key);
        }
        // Poisoned: nothing safe to do. Cannot happen (see the module docs).
    }
}

/// Claim `key`, or return `None` when it is already claimed — or when the
/// lock is poisoned (conservative: never start destructive work we cannot
/// prove is unclaimed).
///
/// Sequential re-runs are always allowed: the previous guard's `Drop` has
/// released the key by the time the first call returns.
pub fn try_begin(key: impl Into<String>) -> Option<SingleFlightGuard> {
    let key = key.into();
    let mut set = match IN_FLIGHT.lock() {
        Ok(s) => s,
        Err(_) => return None,
    };
    if set.contains(&key) {
        return None;
    }
    set.insert(key.clone());
    Some(SingleFlightGuard { key })
}

/// True when `key` is currently claimed.
///
/// Test-only ON PURPOSE. A production caller that branched on this instead of
/// on [`try_begin`]'s result would be racing — the answer can change between
/// the probe and the act — and a check-then-act guard is not a guard. The
/// tests use it to observe claim/release, which is a different question from
/// "may I proceed".
#[cfg(test)]
fn is_in_flight(key: &str) -> bool {
    IN_FLIGHT
        .lock()
        .map(|s| s.contains(key))
        .unwrap_or(false)
}

/// The refusal a caller surfaces when the claim fails. One phrasing for every
/// guarded operation: what is already running, and what to do.
pub fn refusal_message(key: &str) -> String {
    format!(
        "{} is already running in this launcher — refusing to start a second \
         concurrent run. Wait for the current one to finish, then try again.",
        key
    )
}

/// Claim `key` or fail with [`refusal_message`]. The shape command bodies
/// use: `let _guard = single_flight::begin_or_refuse(OP_…)?;`
///
/// Callers that soft-skip rather than fail (the migrate path) use
/// [`try_begin`] and word their own warning.
pub fn begin_or_refuse(key: &str) -> Result<SingleFlightGuard, String> {
    try_begin(key).ok_or_else(|| refusal_message(key))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The guarded ops must be distinct keys — one lock for both would let a
    /// running orchestrator update block a project update-all (and the two
    /// are deliberately separate operations).
    #[test]
    fn guarded_operations_have_distinct_keys() {
        assert_ne!(OP_UPDATE_ALL_PROJECTS, OP_UPDATE_ORCHESTRATOR_AT);
    }

    /// Both sides of the gate, on ONE key:
    ///   * REFUSE — a second claim while the first is held fails;
    ///   * ALLOW — a sequential re-run after the guard drops succeeds.
    /// The leave-alone half is the one that matters: a guard that never
    /// released would break update-all permanently after its first use.
    #[test]
    fn second_concurrent_claim_refused_sequential_rerun_allowed() {
        const OP: &str = "test_op_concurrent";
        let first = try_begin(OP).expect("first claim must succeed");
        assert!(is_in_flight(OP));
        assert!(
            try_begin(OP).is_none(),
            "a second concurrent claim must be refused",
        );

        drop(first);
        assert!(!is_in_flight(OP), "dropping the guard releases the claim");
        let second = try_begin(OP).expect("sequential re-run must be allowed");
        drop(second);
    }

    /// Claims are per-key: holding one operation never blocks another.
    #[test]
    fn claims_do_not_block_other_operations() {
        const A: &str = "test_op_isolation_a";
        const B: &str = "test_op_isolation_b";
        let _a = try_begin(A).expect("claim A");
        let b = try_begin(B).expect("a different operation is unaffected");
        drop(b);
    }

    /// The absorbed per-project migrate claim (DS-F2) keeps its semantics:
    /// per-project isolation, and namespacing that cannot collide with an
    /// operation key even for a project literally named after one.
    #[test]
    fn migrate_keys_are_per_project_and_namespaced() {
        let a = migrate_collections_key("proj-a");
        let b = migrate_collections_key("proj-b");
        assert_ne!(a, b);
        assert_ne!(migrate_collections_key(OP_UPDATE_ALL_PROJECTS), OP_UPDATE_ALL_PROJECTS);

        let _held_a = try_begin(a.clone()).expect("first claim for A");
        assert!(try_begin(a.clone()).is_none(), "second claim for A refused");
        let held_b = try_begin(b).expect("a different project is unaffected");
        drop(held_b);

        // …and a migrate claim never blocks the update-all traversal.
        let unrelated = try_begin(OP_UPDATE_ALL_PROJECTS).expect("unrelated op");
        drop(unrelated);
    }

    /// `begin_or_refuse` returns the user-facing refusal, and that message
    /// names the operation (a bare "already running" tells the user nothing
    /// about which of the two update buttons they hit).
    #[test]
    fn begin_or_refuse_names_the_operation_in_its_error() {
        const OP: &str = "test_op_message";
        let _held = try_begin(OP).expect("claim");
        let err = begin_or_refuse(OP).expect_err("must refuse while held");
        assert!(err.contains(OP), "refusal must name the operation: {err}");
        assert!(err.contains("already running"), "unclear refusal: {err}");
    }

    /// A panic inside the guarded work must not strand the claim — the
    /// guard's Drop runs during unwind. Without this, one panicking
    /// update-all would disable the feature until the launcher restarts.
    #[test]
    fn panic_in_guarded_work_releases_the_claim() {
        const OP: &str = "test_op_panic";
        let result = std::panic::catch_unwind(|| {
            let _guard = try_begin(OP).expect("claim");
            panic!("simulated failure inside the guarded run");
        });
        assert!(result.is_err(), "the panic must have propagated");
        assert!(
            !is_in_flight(OP),
            "the claim must be released by unwinding, not stranded",
        );
        assert!(try_begin(OP).is_some(), "the operation is runnable again");
    }
}
