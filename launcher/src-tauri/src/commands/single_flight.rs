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
//!     owns no such row: the run is a traversal, not an entity.
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
//! projects) and the orchestrator update (`update_run::run_update`, the
//! launcher's own clone) are SEPARATE operations on separate targets. They
//! get separate keys, so guarding one never blocks the other. Do not merge
//! them into a single "an update is running" flag. (v0.2.100, owner Q1: a
//! third key, for the per-clone file-copy command, was retired with that
//! command.)
//!
//! ## Scope of the guarantee
//!
//! The keyed claim is process-wide, not machine-wide: it closes the in-process
//! case the GUI can actually produce (a modal reopened mid-run, a second
//! window, a button double-fire).
//!
//! v0.2.100 (WP-03a, L2-F18): the orchestrator-clone update ALSO takes a
//! machine-wide claim, [`acquire_update_lock`] — `<vct_root>/update.lock`
//! holding the owner's pid, reaped when that pid is dead (the same posture as
//! `lib.rs::reap_stale_install_py_lock`). The single-instance lock does not
//! cover a second launcher binary run from another copy or a hub-side updater,
//! and nothing else stopped two processes interleaving a `git pull` and an
//! `install.py --update` on one tree. The in-process claim is taken FIRST, so
//! within one process the file lock is never contended by itself.

use std::collections::HashSet;
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex};

/// Operation key: the update-all-projects traversal (`projects_v2.rs`).
pub const OP_UPDATE_ALL_PROJECTS: &str = "update_all_projects";

/// Operation key: the post-update model-gateway restart
/// (`gateway_freshness::model_gateway_restart_stale`). A second Continue while
/// one restart is in flight would end every agent session a second time.
pub const OP_GATEWAY_RESTART: &str = "model_gateway_restart";

/// Operation key: an in-place update of the ORCHESTRATOR CLONE — held by
/// `update_run::run_update` (every surface and every kind since v0.2.100) and
/// by the conflict resolvers that hand their claim over to it
/// (`update_run::run_update_claimed`). One key for the launcher's own clone:
/// two would let a badge click and a Preferences click interleave a `git pull`
/// and an `install.py --update` on one tree — the catastrophic case (prior
/// review §4.8), not an inconvenience.
///
/// The pipeline's `UpdateInProgressGuard` does NOT close this: its lockfile is
/// a signal the MCP servers read to exit 75, written soft-fail and never
/// consulted as a claim, so it cannot refuse a second run. This can.
pub const OP_UPDATE_ORCHESTRATOR_CLONE: &str = "orchestrator_update";

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

/// Serialises the TESTS that take [`OP_UPDATE_ORCHESTRATOR_CLONE`].
///
/// v0.2.95 phase 3. [`IN_FLIGHT`] is process-global and `cargo test` runs a
/// binary's tests on parallel threads, so two tests that both claim this key
/// race: whichever loses sees a refusal it did not stage, or a claim it
/// expected to be free. That is not hypothetical — this key now has callers'
/// tests in three modules (`single_flight`, `installer`'s collision resolver,
/// and anything the next surface adds).
///
/// Every test that claims this key must hold this lock for the duration. It is
/// deliberately NOT a production mechanism: the production answer to
/// contention is the refusal itself.
#[cfg(test)]
pub(crate) static ORCHESTRATOR_CLAIM_TEST_LOCK: Mutex<()> = Mutex::new(());

/// Claim the orchestrator-clone update, or fail with the refusal.
///
/// The ONE entry point for [`OP_UPDATE_ORCHESTRATOR_CLONE`], and it exists
/// rather than having both commands write `begin_or_refuse(OP_…)` because the
/// invariant that matters is not "each command takes a claim" but "both take
/// the SAME claim". Spelled as two call sites naming a constant, that survives
/// only as long as nobody adds a second constant; spelled as one function, the
/// two cannot disagree. Prior review §4.8 is what a disagreement costs: a
/// MenuBar click and a Preferences click interleaving a `git pull` and an
/// `install.py --update` on one tree.
pub fn begin_orchestrator_update_or_refuse() -> Result<SingleFlightGuard, String> {
    begin_or_refuse(OP_UPDATE_ORCHESTRATOR_CLONE)
}

// ---------------------------------------------------------------------------
// v0.2.100 (WP-03a, L2-F18) — the machine-wide orchestrator-update claim
// ---------------------------------------------------------------------------

/// Basename of the cross-process update claim under `vct_root_dir()`.
pub const UPDATE_LOCK_BASENAME: &str = "update.lock";

/// RAII holder of `<vct_root>/update.lock`. Dropping it removes the file —
/// but only while the file still names THIS holder's pid, so a guard can never
/// delete a lock another process legitimately re-took after a reap.
#[derive(Debug)]
pub struct UpdateLockGuard {
    path: PathBuf,
    pid: u32,
}

impl Drop for UpdateLockGuard {
    fn drop(&mut self) {
        if lock_holder_pid(&self.path) == Some(self.pid) {
            let _ = std::fs::remove_file(&self.path);
        }
    }
}

/// The pid on the first line of `path`, when the file exists and parses.
fn lock_holder_pid(path: &Path) -> Option<u32> {
    std::fs::read_to_string(path)
        .ok()?
        .lines()
        .next()?
        .trim()
        .parse()
        .ok()
}

/// What an existing lock file tells us about its holder. Pure (the liveness
/// probe is injected) so both arms of the reap — the destructive act on a dead
/// holder and the refusal on a live one — are unit-tested.
#[derive(Debug, Clone, PartialEq, Eq)]
enum ExistingLock {
    /// Empty file: nobody claimed it. Reap.
    Empty,
    /// First line is not a pid: we cannot prove it is stale. Refuse.
    Malformed(String),
    /// A pid that is ours (a guard of this process whose Drop could not
    /// remove the file) or dead. Reap.
    Stale(u32),
    /// A live foreign pid. Refuse.
    Live(u32),
}

fn classify_existing_lock(content: &str, own_pid: u32, is_alive: &dyn Fn(u32) -> bool) -> ExistingLock {
    let Some(first) = content.lines().next().map(str::trim).filter(|l| !l.is_empty()) else {
        return ExistingLock::Empty;
    };
    match first.parse::<u32>() {
        Err(_) => ExistingLock::Malformed(first.to_string()),
        // Our own pid can only be a leftover of THIS process: the in-process
        // claim (`begin_orchestrator_update_or_refuse`) is taken first, so no
        // live holder inside this process can exist while we try.
        Ok(pid) if pid == own_pid => ExistingLock::Stale(pid),
        Ok(pid) if is_alive(pid) => ExistingLock::Live(pid),
        Ok(pid) => ExistingLock::Stale(pid),
    }
}

/// Take the machine-wide orchestrator-update claim at
/// `<vct_root>/update.lock`, reaping a dead holder.
pub fn acquire_update_lock() -> Result<UpdateLockGuard, String> {
    acquire_update_lock_at(
        &vct_launcher_core::paths::vct_root_dir().join(UPDATE_LOCK_BASENAME),
        std::process::id(),
        &vct_launcher_core::process::pid_is_alive,
    )
}

/// [`acquire_update_lock`] with the path, own pid and liveness probe injected.
///
/// Creation is `create_new` (atomic on every supported filesystem), so two
/// processes racing for a free or just-reaped lock cannot both win: the loser
/// sees `AlreadyExists`, classifies the winner as a live holder and refuses.
pub fn acquire_update_lock_at(
    path: &Path,
    own_pid: u32,
    is_alive: &dyn Fn(u32) -> bool,
) -> Result<UpdateLockGuard, String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("could not create {}: {}", parent.display(), e))?;
    }
    // Two attempts: the second is only ever reached after a reap.
    for _ in 0..2 {
        match std::fs::OpenOptions::new().write(true).create_new(true).open(path) {
            Ok(mut file) => {
                let body = format!("{}\n{}\n", own_pid, chrono::Utc::now().timestamp());
                if let Err(e) = file.write_all(body.as_bytes()) {
                    let _ = std::fs::remove_file(path);
                    return Err(format!("could not write {}: {}", path.display(), e));
                }
                return Ok(UpdateLockGuard {
                    path: path.to_path_buf(),
                    pid: own_pid,
                });
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                let content = std::fs::read_to_string(path).unwrap_or_default();
                match classify_existing_lock(&content, own_pid, is_alive) {
                    ExistingLock::Empty | ExistingLock::Stale(_) => {
                        tracing::info!(
                            "[vct] update.lock: reaping a stale claim at {} ({:?})",
                            path.display(),
                            content.lines().next().unwrap_or("")
                        );
                        if let Err(e) = std::fs::remove_file(path) {
                            if e.kind() != std::io::ErrorKind::NotFound {
                                return Err(format!(
                                    "a stale update claim at {} could not be removed ({}); \
                                     delete the file and try again",
                                    path.display(),
                                    e
                                ));
                            }
                        }
                    }
                    ExistingLock::Live(pid) => {
                        return Err(format!(
                            "Another process (pid {}) is already updating this orchestrator — \
                             refusing to start a second update. Wait for it to finish. If no \
                             update is running, the claim at {} is left over from a process \
                             whose pid was reused: delete that file and try again.",
                            pid,
                            path.display()
                        ));
                    }
                    ExistingLock::Malformed(first) => {
                        return Err(format!(
                            "The update claim at {} does not name a process (first line {:?}), \
                             so the launcher cannot tell whether another update is running. If \
                             none is, delete that file and try again.",
                            path.display(),
                            first
                        ));
                    }
                }
            }
            Err(e) => return Err(format!("could not create {}: {}", path.display(), e)),
        }
    }
    Err(format!(
        "the update claim at {} was taken by another process while this one was reaping a \
         stale claim — refusing; try again once that update finishes",
        path.display()
    ))
}

// ─── v0.2.100 (WP-15, W3R-06): one bundle engine per project folder ─────────
//
// A DIFFERENT question from the claims above, so a different primitive. Every
// launcher path that runs `install-bundle` on a project folder — the per-project
// update, "Update all" (which calls it per project) and the module toggle's
// background delivery — reaches `projects_v2::run_install_bundle_core`, and two
// engines on one `.claude/.vco-manifest.json` are last-writer-wins: the loser's
// adoptions and orphan decisions are recorded by neither. Refusing would be
// wrong here (a toggle refused during "Update all" can lose its delivery when
// the update already passed that project), and the engine is idempotent, so the
// second caller WAITS for its turn and then runs against the first one's
// result. The key is the folder, so different projects never wait on each other.

static BUNDLE_ENGINE_TURNS: LazyLock<
    Mutex<std::collections::HashMap<String, std::sync::Arc<tokio::sync::Mutex<()>>>>,
> = LazyLock::new(|| Mutex::new(std::collections::HashMap::new()));

fn bundle_engine_key(folder: &Path) -> String {
    std::fs::canonicalize(folder)
        .unwrap_or_else(|_| folder.to_path_buf())
        .to_string_lossy()
        .into_owned()
}

/// Wait for, then hold, the bundle-engine turn for `folder` (released on drop).
pub async fn bundle_engine_turn(folder: &Path) -> tokio::sync::OwnedMutexGuard<()> {
    let slot = {
        let mut map = BUNDLE_ENGINE_TURNS
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        map.entry(bundle_engine_key(folder))
            .or_insert_with(|| std::sync::Arc::new(tokio::sync::Mutex::new(())))
            .clone()
    };
    slot.lock_owned().await
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The guarded ops must be distinct keys — one lock for both would let a
    /// running orchestrator update block a project update-all (and the two
    /// are deliberately separate operations).
    #[tokio::test]
    async fn bundle_engine_turns_serialise_one_folder_not_two() {
        let a = tempfile::tempdir().expect("tempdir");
        let b = tempfile::tempdir().expect("tempdir");
        let held = bundle_engine_turn(a.path()).await;
        let a_path = a.path().to_path_buf();
        let waiter = tokio::spawn(async move {
            let _t = bundle_engine_turn(&a_path).await;
        });
        // A different project is never held up by `a`.
        tokio::time::timeout(std::time::Duration::from_secs(5), bundle_engine_turn(b.path()))
            .await
            .expect("another folder must not wait");
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        assert!(!waiter.is_finished(), "same folder must wait for the turn");
        drop(held);
        tokio::time::timeout(std::time::Duration::from_secs(5), waiter)
            .await
            .expect("released turn must be taken")
            .expect("join");
    }

    #[test]
    fn guarded_operations_have_distinct_keys() {
        // A gateway restart must never block (or be blocked by) an update.
        for other in [OP_UPDATE_ALL_PROJECTS, OP_UPDATE_ORCHESTRATOR_CLONE] {
            assert_ne!(OP_GATEWAY_RESTART, other);
        }
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

    /// BOTH ARMS of the guard the two update commands now share — and the
    /// point is the arm that REFUSES, because until v0.2.95 neither command was
    /// guarded at all and the second click simply ran.
    ///
    /// The commands themselves cannot be driven from a unit test (Tauri
    /// `AppHandle` + `Window` + a live clone), which is exactly why the claim
    /// is taken through ONE function instead of two literal call sites: what
    /// this asserts about `begin_orchestrator_update_or_refuse` holds for every
    /// caller of it by construction.
    #[test]
    fn the_orchestrator_update_claim_refuses_a_second_holder_and_frees_afterwards() {
        let _serial = ORCHESTRATOR_CLAIM_TEST_LOCK
            .lock()
            .unwrap_or_else(|p| p.into_inner());

        // ACT arm: the first caller proceeds, the second is refused — a
        // Preferences update while the MenuBar update is mid-`install.py`.
        let first = begin_orchestrator_update_or_refuse().expect("first update must proceed");
        let refused = begin_orchestrator_update_or_refuse()
            .expect_err("a second concurrent orchestrator update must be refused");
        assert!(
            refused.contains(OP_UPDATE_ORCHESTRATOR_CLONE),
            "the refusal must name what is already running: {refused}"
        );

        // LEAVE-ALONE arm: the claim is not a latch. A guard that never
        // released would break BOTH update buttons for the rest of the
        // process's life after the first successful update — worse than the
        // race it prevents.
        drop(first);
        let second = begin_orchestrator_update_or_refuse()
            .expect("a sequential re-run must be allowed once the first finished");
        drop(second);
    }

    /// The orchestrator claim must not collide with the update-all key, or a
    /// running orchestrator update would block an unrelated update-all.
    #[test]
    fn the_orchestrator_update_key_is_distinct_from_update_all() {
        assert_ne!(OP_UPDATE_ORCHESTRATOR_CLONE, OP_UPDATE_ALL_PROJECTS);
    }

    // ---- v0.2.100 WP-03a: the cross-process update.lock --------------------

    /// ACT arm of the reap: a lock left by a DEAD pid is removed and the claim
    /// is taken; the guard's Drop removes the file again.
    #[test]
    fn update_lock_reaps_a_dead_holder_and_takes_the_claim() {
        let td = tempfile::tempdir().unwrap();
        let path = td.path().join(UPDATE_LOCK_BASENAME);
        std::fs::write(&path, "4242\n1700000000\n").unwrap();
        let dead = |_pid: u32| false;
        let guard = acquire_update_lock_at(&path, 777, &dead).expect("dead holder is reaped");
        assert_eq!(lock_holder_pid(&path), Some(777), "the file now names the new holder");
        drop(guard);
        assert!(!path.exists(), "dropping the guard releases the claim");
    }

    /// LEAVE-ALONE arm: a LIVE foreign holder is never reaped — the second
    /// claimant is refused and the holder's file is byte-identical afterwards.
    #[test]
    fn update_lock_refuses_a_live_second_holder_and_leaves_its_file_alone() {
        let td = tempfile::tempdir().unwrap();
        let path = td.path().join(UPDATE_LOCK_BASENAME);
        let alive_4242 = |pid: u32| pid == 4242;
        let first = acquire_update_lock_at(&path, 4242, &alive_4242).expect("first claim");
        let before = std::fs::read_to_string(&path).unwrap();

        let err = acquire_update_lock_at(&path, 777, &alive_4242)
            .expect_err("a second process must be refused while the first is alive");
        assert!(err.contains("pid 4242"), "the refusal names the holder: {err}");
        assert!(err.contains(&path.display().to_string()), "and the file: {err}");
        assert_eq!(std::fs::read_to_string(&path).unwrap(), before, "holder's claim untouched");

        drop(first);
        let again = acquire_update_lock_at(&path, 777, &alive_4242)
            .expect("a sequential claim succeeds once the holder released");
        drop(again);
    }

    /// A malformed claim cannot be proven stale — refuse, touch nothing.
    #[test]
    fn update_lock_refuses_a_malformed_claim_without_deleting_it() {
        let td = tempfile::tempdir().unwrap();
        let path = td.path().join(UPDATE_LOCK_BASENAME);
        std::fs::write(&path, "not-a-pid\n").unwrap();
        let err = acquire_update_lock_at(&path, 777, &|_| false).expect_err("malformed refuses");
        assert!(err.contains("not-a-pid"), "{err}");
        assert!(path.exists(), "a claim we cannot interpret is never deleted");
    }

    /// Our own pid in the file is a leftover of this process (the in-process
    /// claim precedes this one) — reaped even though the pid is alive.
    #[test]
    fn update_lock_reaps_its_own_leftover_and_an_empty_file() {
        let td = tempfile::tempdir().unwrap();
        let path = td.path().join(UPDATE_LOCK_BASENAME);
        std::fs::write(&path, "777\n1\n").unwrap();
        let g = acquire_update_lock_at(&path, 777, &|_| true).expect("own leftover reaped");
        drop(g);
        std::fs::write(&path, "").unwrap();
        let g = acquire_update_lock_at(&path, 777, &|_| true).expect("empty file reaped");
        drop(g);
    }

    /// A guard never deletes a claim it no longer owns.
    #[test]
    fn update_lock_drop_leaves_a_foreign_claim_alone() {
        let td = tempfile::tempdir().unwrap();
        let path = td.path().join(UPDATE_LOCK_BASENAME);
        let g = acquire_update_lock_at(&path, 777, &|_| false).unwrap();
        std::fs::write(&path, "4242\n1\n").unwrap();
        drop(g);
        assert_eq!(lock_holder_pid(&path), Some(4242));
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
