// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Machine-global admission gate for embed-heavy update-all fan-out
//! (v0.2.77 Part 3 / 5c task 3).
//!
//! ## The incident this prevents
//!
//! A launcher "update all projects" over 6 projects fanned out N codegraph
//! analyzers + N kg-syncs at once against a single 16 GiB GPU. The code-embed
//! service's fixed semaphore shed 503s under the burst and 89 CodeFunction
//! rows were written VECTORLESS (`embed_revision=0`). The outer update loop
//! (`projects_v2::update_all_projects`) is serial, but each
//! `update_project_v2` fires-and-forgets `codegraph::spawn_initial_build` +
//! `kg_sync::spawn_initial_sync` (each `tokio::spawn`), so N projects → up to
//! ~2N concurrent GPU workloads with nothing bounding cross-project concurrency.
//!
//! ## The fix
//!
//! ONE process-global semaphore, sized from the hardware-derived
//! `embedding.update_all_max_parallel` app_state value (install.py task 2
//! seeds it; a GUI-tuned value overrides). BOTH embed-heavy spawned tasks
//! (`run_build_task`, `run_sync_task`) acquire a permit from this ONE
//! semaphore INSIDE the spawned task body — so `update_project_v2` stays
//! non-blocking (the outer loop keeps advancing and queueing tasks; tasks park
//! on `.await` for a permit). Piggybacks the existing pending/running DB
//! status rows: a queued task stays `pending`/`RUNNING` with a "queued" phase.
//!
//! ## Why ONE shared semaphore across BOTH pipelines
//!
//! Per the USER DESIGN RULING: codegraph and KG embedding workloads share ONE
//! memory device, so they must draw from ONE shared budget — NOT two
//! independent per-pipeline caps that only fit alone. A single machine-global
//! semaphore whose capacity is the per-project parallel cap admits at most
//! `update_all_max_parallel` projects' worth of embed work at a time,
//! regardless of which pipeline (or how many callers: update-all, retry,
//! create, boot-resume) spawned the tasks.
//!
//! ## Relationship to `KG_SYNC_SEMAPHORE`
//!
//! `kg_sync.rs` has a SEPARATE `KG_SYNC_SEMAPHORE` (const cap 1) that
//! serializes `sync_knowledge_graph.py` processes specifically. That's a
//! narrower single-flight lock on the KG re-embed subprocess. THIS gate is the
//! broader cross-pipeline admission cap. A KG task holds BOTH: it acquires the
//! admission permit first (cross-pipeline budget), then the KG single-flight
//! permit (serialize the python re-embed). Codegraph tasks hold only the
//! admission permit (the analyzer's own in-process embed concurrency is capped
//! by `CODE_EMBED_MAX_CONCURRENT` — task 2).
//!
//! ## Single-instance sufficiency
//!
//! The launcher is single-instance per user (`tauri_plugin_single_instance`),
//! so all fan-out originates in this one process → a process-global semaphore
//! caps machine-wide concurrency. (Same rationale as `KG_SYNC_SEMAPHORE`.)

use std::sync::{Arc, OnceLock};

use crate::db::app_state::DEFAULT_UPDATE_ALL_MAX_PARALLEL;
use crate::db::Db;

/// The process-global admission semaphore. Initialised ONCE on first
/// `acquire_update_all_admission` call from the resolved app_state cap; the
/// capacity is fixed for the process lifetime thereafter (a GUI change to the
/// cap takes effect on the next launcher start — same lifetime model as the
/// const `KG_SYNC_SEMAPHORE`; re-sizing a live semaphore is not supported by
/// `tokio::sync::Semaphore` without `add_permits`/`forget` bookkeeping we don't
/// need for a boot-time-resolved cap).
static UPDATE_ALL_ADMISSION: OnceLock<Arc<tokio::sync::Semaphore>> = OnceLock::new();

/// Resolve the admission capacity from app_state (soft-fail → default), used
/// to lazily size the semaphore on first acquire. Extracted so the sizing is
/// testable independently of the async acquire.
fn resolve_capacity(db: &Db) -> usize {
    // `get_update_all_max_parallel` already clamps to `1..=64` and degrades a
    // garbage/absent row to `DEFAULT_UPDATE_ALL_MAX_PARALLEL`. Belt-and-braces
    // floor of 1 here so the semaphore can never be constructed with 0 permits
    // (which would deadlock every task forever).
    let cap = db.get_update_all_max_parallel();
    if cap == 0 {
        DEFAULT_UPDATE_ALL_MAX_PARALLEL.max(1)
    } else {
        cap
    }
}

/// Lazily get-or-init the process-global admission semaphore, sizing it from
/// the DB cap on first call. Subsequent calls ignore `db` (the semaphore is
/// already sized) — the cap is a boot-time-resolved value by design.
fn admission_semaphore(db: &Db) -> Arc<tokio::sync::Semaphore> {
    UPDATE_ALL_ADMISSION
        .get_or_init(|| {
            let cap = resolve_capacity(db);
            eprintln!(
                "[vct] update-all embed-admission gate: capacity {} \
                 (from app_state '{}', default {})",
                cap,
                crate::db::app_state::UPDATE_ALL_MAX_PARALLEL_KEY,
                DEFAULT_UPDATE_ALL_MAX_PARALLEL,
            );
            Arc::new(tokio::sync::Semaphore::new(cap))
        })
        .clone()
}

/// Acquire a permit on the machine-global update-all admission semaphore,
/// parking (async) until one is free. Returns an owned permit whose `Drop`
/// releases it back to the queue (RAII — a panicking / early-returning task
/// still frees its slot). Callers MUST bind the returned permit for the whole
/// embed-heavy lifetime (acquire → subprocess → drain → return) so the slot is
/// held across the entire workload, not just the acquire.
///
/// `acquire_owned` only errors if the semaphore is `close()`d; we never close
/// it (it lives for the process lifetime), so the `expect` is unreachable in
/// practice — surfaced loudly rather than silently dropping the cap.
pub async fn acquire_update_all_admission(db: &Db) -> tokio::sync::OwnedSemaphorePermit {
    admission_semaphore(db)
        .acquire_owned()
        .await
        .expect("UPDATE_ALL_ADMISSION is never closed for the process lifetime")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_capacity_defaults_when_unset() {
        let db = Db::open_in_memory().expect("in-memory db");
        assert_eq!(resolve_capacity(&db), DEFAULT_UPDATE_ALL_MAX_PARALLEL);
    }

    #[test]
    fn resolve_capacity_reads_seeded_value() {
        let db = Db::open_in_memory().expect("in-memory db");
        db.app_state_set(crate::db::app_state::UPDATE_ALL_MAX_PARALLEL_KEY, "5")
            .unwrap();
        assert_eq!(resolve_capacity(&db), 5);
    }

    /// The admission semaphore must serialise to its capacity: N tasks racing
    /// through `acquire_update_all_admission` never exceed `cap` concurrent
    /// holders. (Mirrors the KG_SYNC_SEMAPHORE serialisation test.)
    ///
    /// NOTE: the process-global `OnceLock` means this test defines the
    /// semaphore capacity for the whole test process on first touch. To keep
    /// it deterministic regardless of test ordering, it drives the SAME
    /// public acquire path and asserts the *relative* invariant (peak
    /// concurrency <= the semaphore's actual capacity) rather than a hardcoded
    /// number.
    #[tokio::test]
    async fn admission_never_exceeds_capacity() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        let db = Db::open_in_memory().expect("in-memory db");
        // Touch the semaphore once to fix its capacity for this process.
        let cap = admission_semaphore(&db).available_permits();
        assert!(cap >= 1, "capacity must be at least 1");

        let live = Arc::new(AtomicUsize::new(0));
        let peak = Arc::new(AtomicUsize::new(0));
        let mut handles = Vec::new();
        for _ in 0..(cap * 4 + 3) {
            let db2 = Db::open_in_memory().expect("in-memory db");
            let live = live.clone();
            let peak = peak.clone();
            handles.push(tokio::spawn(async move {
                let _permit = acquire_update_all_admission(&db2).await;
                let now = live.fetch_add(1, Ordering::SeqCst) + 1;
                peak.fetch_max(now, Ordering::SeqCst);
                tokio::time::sleep(std::time::Duration::from_millis(5)).await;
                live.fetch_sub(1, Ordering::SeqCst);
                // `_permit` drops here, releasing the slot.
            }));
        }
        for h in handles {
            h.await.unwrap();
        }
        assert!(
            peak.load(Ordering::SeqCst) <= cap,
            "peak concurrency {} exceeded semaphore capacity {}",
            peak.load(Ordering::SeqCst),
            cap,
        );
        // All permits returned to the pool.
        assert_eq!(admission_semaphore(&db).available_permits(), cap);
    }
}
