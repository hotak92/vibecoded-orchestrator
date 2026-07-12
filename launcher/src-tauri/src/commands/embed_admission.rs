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
//! ## v0.2.79 §B — live available-RAM memory gate layered on the fixed semaphore
//!
//! The fixed semaphore above is a COARSE worst-case cap resolved ONCE at boot
//! from a DETECTED-TOTAL memory figure (install-time). On a workstation whose
//! RAM is shared with OTHER users / processes, a boot-time-total cap can still
//! admit more embed workers than the LIVE free memory can hold — the total
//! hasn't changed, but other tenants have since consumed it. So after acquiring
//! a semaphore permit, a task now ALSO passes through a live-memory gate: read
//! `MemAvailable` (Linux `/proc/meminfo`, the kernel's own allocatable-without-
//! swapping estimate) and, if it is below the per-project embed footprint with
//! headroom, PARK (async sleep-poll) and re-read until memory frees — rather
//! than proceed and risk the OOM/thrash the coarse cap can't see.
//!
//! Design decisions (v0.2.79 §B review B.1/B.2/B.3):
//!   * **Semaphore stays fixed, gate is layered (B.2).** A `tokio::sync::
//!     Semaphore` cannot be live-resized; we do NOT try. The semaphore remains
//!     the coarse cap; the memory gate is a per-acquire WAIT layered AFTER it.
//!     Lower-risk than replacing the semaphore with a bespoke admission loop.
//!   * **Live `MemAvailable` is already fleet-aware (B.1).** It is sampled
//!     AFTER prior workers allocated, so it already reflects their footprint.
//!     We do NOT keep a reservation ledger / subtract our own workers' footprint
//!     again — that would double-count and starve the gate to the floor forever.
//!     (The only residual is the admitted-but-not-yet-allocated TOCTOU window;
//!     the 0.8 headroom below + the code-embed service's own 503-shed absorb a
//!     small over-admit. We deliberately do NOT build a ledger for it.)
//!   * **Scope: the admission gate ONLY (B.3).** The code-embed service reads
//!     `CODE_EMBED_MAX_CONCURRENT` once at process start from `.env`; it can't
//!     re-read per request, so we leave that seed as detected-total and touch
//!     only this Rust runtime gate.
//!   * **VRAM branch unchanged.** VRAM is 80%-of-total on a workstation GPU (no
//!     other consumers) — that path is not gated here. This memory gate targets
//!     the RAM-shared case; when the footprint reads as effectively zero (remote
//!     API model) the gate is a no-op.
//!   * **Floor 1, never deadlock at zero.** A task that finds itself the ONLY
//!     one past the gate proceeds regardless of the memory reading (the machine
//!     must always make progress on at least one embed). Everyone else parks
//!     until memory frees. And if `MemAvailable` can't be read at all
//!     (non-Linux, missing `/proc`, parse failure), the gate PROCEEDS
//!     (conservative leave-alone: never block on an unknown).
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

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::Duration;

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

/// Count of tasks currently PAST the live-memory gate (i.e. between "gate
/// admitted" and "permit dropped"). Used ONLY to enforce the floor-1
/// invariant: a task that observes this at 0 proceeds regardless of the memory
/// reading, so the machine always makes progress on at least one embed even
/// when `MemAvailable` never rises to the footprint. Incremented the instant a
/// task clears the gate; decremented on permit `Drop` via the guard below.
static IN_FLIGHT_PAST_GATE: AtomicUsize = AtomicUsize::new(0);

/// Per-project embed footprint (in MiB) the live-memory gate compares
/// `MemAvailable` against, before the 0.8 headroom factor. This is the coarse
/// marginal RAM one admitted project's embed work (codegraph analyze + kg-sync)
/// needs to run without thrashing.
///
/// WHY a Rust constant (not a mirror of the Python `_MODEL_FOOTPRINT_GB`
/// table): the authoritative footprint table is channel-B shared config in
/// `vco_lib.embedding_selection`; the Rust side deliberately does NOT mirror it
/// (see that module's header). The persisted `update_all_max_parallel` app_state
/// row is an integer COUNT, not a GB figure, so there is no footprint value to
/// read from the DB. This gate only needs a CONSERVATIVE per-project marginal
/// figure to throttle finer than the coarse count-based semaphore; a single
/// tunable default suffices. `1536` MiB (1.5 GiB) matches the Python
/// `_UNKNOWN_FOOTPRINT_GB` conservative mid-size-model base (1.5 GB) — it is a
/// deliberate over-estimate so the gate errs toward parking (safe) rather than
/// over-admitting (the 503-storm this whole feature prevents). Override via
/// `VCT_EMBED_MODEL_FOOTPRINT_MB` for exotic hosts.
const DEFAULT_MODEL_FOOTPRINT_MB: u64 = 1536;

/// Headroom factor from the shared user formula (leave 20% for fragmentation,
/// framework/CUDA context, HTTP buffers, the OS). MUST match the Python
/// `_MEMORY_SAFETY_FACTOR = 0.8` so both sides reason about the same margin.
const MEMORY_SAFETY_FACTOR: f64 = 0.8;

/// Poll interval when a task is parked at the memory gate waiting for RAM to
/// free. Short enough that a task admits promptly once memory frees, long
/// enough that the park loop is negligible overhead. Not user-tunable — a
/// fixed back-pressure cadence.
const MEMORY_GATE_POLL: Duration = Duration::from_millis(500);

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

/// Resolve the per-project embed footprint the memory gate uses, in MiB.
/// `VCT_EMBED_MODEL_FOOTPRINT_MB` overrides the compiled default; a garbage /
/// zero / absent value falls back to [`DEFAULT_MODEL_FOOTPRINT_MB`]. A `0`
/// override effectively DISABLES the gate (footprint 0 → `MemAvailable` is
/// always >= 0 → always proceed) — the documented escape hatch.
fn model_footprint_mb() -> u64 {
    match std::env::var("VCT_EMBED_MODEL_FOOTPRINT_MB") {
        Ok(raw) => raw.trim().parse::<u64>().unwrap_or(DEFAULT_MODEL_FOOTPRINT_MB),
        Err(_) => DEFAULT_MODEL_FOOTPRINT_MB,
    }
}

/// Read `MemAvailable` from Linux `/proc/meminfo`, in MiB. Returns `None` on
/// any failure (non-Linux — no `/proc/meminfo`; unreadable; unparseable; the
/// field absent). `None` means "unknown" → the gate PROCEEDS (conservative
/// leave-alone: never block on a reading we could not take).
///
/// `MemAvailable` (kernel-computed since Linux 3.14) is the kernel's own
/// estimate of memory allocatable to a new workload WITHOUT swapping — it
/// already accounts for reclaimable page-cache and prior allocations, which is
/// exactly the live fleet-aware figure §B wants (B.1). We do NOT read `MemFree`
/// (which understates by excluding reclaimable cache).
#[cfg(target_os = "linux")]
fn read_mem_available_mib() -> Option<u64> {
    let contents = std::fs::read_to_string("/proc/meminfo").ok()?;
    parse_mem_available_mib(&contents)
}

/// Non-Linux hosts have no `/proc/meminfo`; there is no portable equivalent we
/// wire here. Return `None` → the gate proceeds (floor-1 leave-alone). macOS /
/// Windows workstations are the VRAM-branch case (GPU embed), which §B leaves
/// ungated anyway; the RAM-shared gate is a Linux-server concern.
#[cfg(not(target_os = "linux"))]
fn read_mem_available_mib() -> Option<u64> {
    None
}

/// Parse `MemAvailable` (reported in kB by the kernel) out of `/proc/meminfo`
/// text, converting to MiB. Split out from the file read so tests can feed
/// synthetic meminfo bodies without a real `/proc`. Returns `None` when the
/// `MemAvailable:` line is absent or malformed.
///
/// REUSES the single-source meminfo field parser
/// `installer::parse_meminfo_field_kb` (CLAUDE.md: extract-before-duplicate —
/// there is exactly ONE strip-prefix/parse loop for `/proc/meminfo` in the
/// launcher, shared by install-time `MemTotal` detection and this runtime
/// `MemAvailable` gate). This wrapper only adds the kB→MiB conversion the gate
/// reasons in.
#[cfg(any(target_os = "linux", test))]
fn parse_mem_available_mib(meminfo: &str) -> Option<u64> {
    crate::commands::installer::parse_meminfo_field_kb(meminfo, "MemAvailable:")
        .map(|kb| kb / 1024)
}

/// A live-memory sampler: returns `MemAvailable` in MiB, or `None` for
/// "unknown → proceed". Boxed as a trait object / fn so tests can inject a
/// deterministic reader (the real one reads `/proc/meminfo`; tests script a
/// sequence) — the gate logic stays hermetic and does NOT depend on the real
/// system's memory.
type MemSampler<'a> = dyn Fn() -> Option<u64> + Send + Sync + 'a;

/// RAII guard: increments [`IN_FLIGHT_PAST_GATE`] on construction (a task has
/// cleared the memory gate) and decrements on `Drop` (its permit is being
/// released). Bundled INTO the returned permit wrapper so the count tracks the
/// exact "past the gate, holding a slot" population that the floor-1 rule keys
/// on. Order matters: this guard must drop when the permit drops, which is why
/// it is stored alongside the permit in [`AdmissionPermit`].
struct InFlightGuard;

impl InFlightGuard {
    fn enter() -> Self {
        IN_FLIGHT_PAST_GATE.fetch_add(1, Ordering::SeqCst);
        InFlightGuard
    }
}

impl Drop for InFlightGuard {
    fn drop(&mut self) {
        IN_FLIGHT_PAST_GATE.fetch_sub(1, Ordering::SeqCst);
    }
}

/// What `acquire_update_all_admission` returns: the semaphore permit PLUS the
/// in-flight guard, so both release together when the caller drops it. Callers
/// bind this for the whole embed-heavy lifetime exactly as before — it is a
/// drop-in replacement for the bare `OwnedSemaphorePermit`.
pub struct AdmissionPermit {
    _permit: tokio::sync::OwnedSemaphorePermit,
    _in_flight: InFlightGuard,
}

/// The core memory-gate decision, factored out and parameterised on the sampler
/// so it is unit-testable without a real `/proc/meminfo`. Given the current
/// in-flight-past-gate count and a memory sample, decide whether a task may
/// proceed NOW.
///
/// Proceed when ANY of:
///   * footprint requirement is effectively zero (gate disabled / remote model),
///   * we are the only candidate in flight (`in_flight == 0`) — FLOOR 1, the
///     machine must always make progress on at least one embed,
///   * the memory sample is `None` (unknown → conservative proceed), or
///   * `available_mib >= footprint_mib * 0.8` (enough live RAM with headroom).
/// Otherwise PARK.
fn should_proceed(available: Option<u64>, in_flight: usize, footprint_mib: u64) -> bool {
    if footprint_mib == 0 {
        return true; // gate disabled / zero-footprint remote model
    }
    if in_flight == 0 {
        return true; // floor 1: never block the only task
    }
    let available_mib = match available {
        None => return true,   // unknown reading → conservative proceed
        Some(v) => v,
    };
    let required = (footprint_mib as f64 * MEMORY_SAFETY_FACTOR) as u64;
    available_mib >= required
}

/// Park-poll on the live-memory gate until [`should_proceed`] returns true,
/// then return. Parameterised on the sampler AND the poll interval so the whole
/// loop is deterministic under test: prod passes [`MEMORY_GATE_POLL`]; tests
/// inject a scripted sampler + a near-zero interval so the loop completes in
/// microseconds without a real `/proc` or wall-clock delay. The floor-1 rule is
/// re-checked every iteration against the LIVE in-flight count, so a task that
/// becomes the sole candidate (others finished) is admitted even if memory
/// never freed.
async fn wait_for_memory_gate(sampler: &MemSampler<'_>, footprint_mib: u64, poll: Duration) {
    loop {
        let in_flight = IN_FLIGHT_PAST_GATE.load(Ordering::SeqCst);
        if should_proceed(sampler(), in_flight, footprint_mib) {
            return;
        }
        tokio::time::sleep(poll).await;
    }
}

/// Acquire a permit on the machine-global update-all admission semaphore,
/// parking (async) until one is free, THEN passing through the live-memory gate
/// (v0.2.79 §B) — parking further until live `MemAvailable` can hold this
/// project's embed footprint (or this task is the sole in-flight candidate, the
/// floor-1 guarantee). Returns an [`AdmissionPermit`] whose `Drop` releases
/// both the semaphore slot AND the in-flight count (RAII — a panicking /
/// early-returning task still frees its slot). Callers MUST bind the returned
/// permit for the whole embed-heavy lifetime (acquire → subprocess → drain →
/// return) so the slot is held across the entire workload, not just the acquire.
///
/// `acquire_owned` only errors if the semaphore is `close()`d; we never close
/// it (it lives for the process lifetime), so the `expect` is unreachable in
/// practice — surfaced loudly rather than silently dropping the cap.
pub async fn acquire_update_all_admission(db: &Db) -> AdmissionPermit {
    let permit = admission_semaphore(db)
        .acquire_owned()
        .await
        .expect("UPDATE_ALL_ADMISSION is never closed for the process lifetime");

    // Live-memory gate (v0.2.79 §B). The real sampler reads /proc/meminfo; the
    // gate proceeds immediately when RAM is sufficient, when this is the sole
    // in-flight task (floor 1), or when the reading is unknown. The in-flight
    // guard is entered ONLY once we clear the gate, so a parked task does NOT
    // count itself toward the floor-1 population (it must not keep OTHER parked
    // tasks from ever becoming "the only one").
    let real_sampler: &MemSampler = &read_mem_available_mib;
    wait_for_memory_gate(real_sampler, model_footprint_mb(), MEMORY_GATE_POLL).await;
    let in_flight = InFlightGuard::enter();

    AdmissionPermit {
        _permit: permit,
        _in_flight: in_flight,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    /// Reset the process-global in-flight counter between tests. The counter is
    /// a `static` shared across the whole test binary; tests that assert on it
    /// serialise via `GATE_TEST_LOCK` and zero it at entry so ordering can't
    /// leak state. (The semaphore `OnceLock` capacity is intentionally NOT
    /// reset — it's fixed-for-process by design; the memory-gate tests drive
    /// the pure helpers directly and don't touch the semaphore.)
    static GATE_TEST_LOCK: Mutex<()> = Mutex::new(());

    fn reset_in_flight() {
        IN_FLIGHT_PAST_GATE.store(0, Ordering::SeqCst);
    }

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

    // ── /proc/meminfo parsing ───────────────────────────────────────────

    #[test]
    fn parse_mem_available_extracts_and_converts_to_mib() {
        // 12 GiB reported in kB → 12288 MiB.
        let meminfo = "MemTotal:       32768000 kB\n\
                       MemFree:         1048576 kB\n\
                       MemAvailable:   12582912 kB\n\
                       Buffers:          123456 kB\n";
        assert_eq!(parse_mem_available_mib(meminfo), Some(12582912 / 1024));
    }

    #[test]
    fn parse_mem_available_absent_field_is_none() {
        // Older kernels / a truncated read with no MemAvailable line.
        let meminfo = "MemTotal:       32768000 kB\nMemFree: 1048576 kB\n";
        assert_eq!(parse_mem_available_mib(meminfo), None);
    }

    #[test]
    fn parse_mem_available_malformed_is_none() {
        let meminfo = "MemAvailable:   not-a-number kB\n";
        assert_eq!(parse_mem_available_mib(meminfo), None);
    }

    // ── the pure gate decision ──────────────────────────────────────────

    #[test]
    fn gate_proceeds_when_footprint_zero() {
        // Zero footprint (gate disabled / remote model) → always proceed even
        // with others in flight and no memory.
        assert!(should_proceed(Some(0), 4, 0));
    }

    #[test]
    fn gate_proceeds_when_sole_candidate_even_if_memory_low() {
        // Floor 1: in_flight == 0 → proceed regardless of the (tiny) reading.
        assert!(should_proceed(Some(1), 0, 4096));
    }

    #[test]
    fn gate_proceeds_when_memory_unknown() {
        // Unknown reading (None) with others in flight → conservative proceed.
        assert!(should_proceed(None, 3, 4096));
    }

    #[test]
    fn gate_proceeds_when_enough_memory() {
        // footprint 2048 MiB * 0.8 = 1638 required; 4096 available → proceed.
        assert!(should_proceed(Some(4096), 3, 2048));
    }

    #[test]
    fn gate_parks_when_memory_insufficient_and_others_in_flight() {
        // footprint 4096 * 0.8 = 3276 required; only 1000 available AND another
        // task already in flight (so floor-1 does not apply) → PARK.
        assert!(!should_proceed(Some(1000), 1, 4096));
    }

    #[test]
    fn gate_headroom_boundary_is_respected() {
        // Exactly at the 0.8 threshold proceeds; one MiB below parks.
        let footprint = 5000u64;
        let required = (footprint as f64 * MEMORY_SAFETY_FACTOR) as u64; // 4000
        assert!(should_proceed(Some(required), 2, footprint));
        assert!(!should_proceed(Some(required - 1), 2, footprint));
    }

    // ── env override for the footprint ──────────────────────────────────

    #[test]
    fn footprint_env_override_and_fallback() {
        let _g = GATE_TEST_LOCK.lock().unwrap();
        // Explicit override honoured.
        std::env::set_var("VCT_EMBED_MODEL_FOOTPRINT_MB", "2048");
        assert_eq!(model_footprint_mb(), 2048);
        // Garbage → default.
        std::env::set_var("VCT_EMBED_MODEL_FOOTPRINT_MB", "banana");
        assert_eq!(model_footprint_mb(), DEFAULT_MODEL_FOOTPRINT_MB);
        // Zero override (disable gate) is honoured as 0.
        std::env::set_var("VCT_EMBED_MODEL_FOOTPRINT_MB", "0");
        assert_eq!(model_footprint_mb(), 0);
        std::env::remove_var("VCT_EMBED_MODEL_FOOTPRINT_MB");
        assert_eq!(model_footprint_mb(), DEFAULT_MODEL_FOOTPRINT_MB);
    }

    // ── the async park loop (injected sampler; hermetic) ────────────────

    /// A scripted sampler: yields values from a queue on each call, repeating
    /// the last value once the queue drains. Lets a test model "memory is low,
    /// then frees" deterministically without a real /proc.
    fn scripted_sampler(values: Vec<Option<u64>>) -> impl Fn() -> Option<u64> + Send + Sync {
        let idx = AtomicUsize::new(0);
        move || {
            let i = idx.fetch_add(1, Ordering::SeqCst);
            let n = values.len();
            if n == 0 {
                return None;
            }
            values[i.min(n - 1)]
        }
    }

    /// Near-zero poll interval for the async gate tests: the scripted sampler
    /// is what drives the loop to termination, so the sleep is only a yield —
    /// keep it tiny so the whole test runs in microseconds on the real clock
    /// (no `tokio::time` pause feature needed).
    const TEST_POLL: Duration = Duration::from_millis(1);

    /// The gate PARKS while available < footprint, then PROCEEDS once the
    /// scripted sampler reports memory has freed. Deterministic: we hold the
    /// in-flight count at 1 (so floor-1 does not short-circuit) and script the
    /// sampler to report low → low → plenty. The scripted sampler GUARANTEES
    /// termination regardless of wall-clock timing (the 3rd read admits).
    #[tokio::test]
    async fn memory_gate_parks_then_proceeds_when_memory_frees() {
        let _g = GATE_TEST_LOCK.lock().unwrap();
        reset_in_flight();
        // Simulate ONE other task already past the gate so floor-1 is inactive.
        IN_FLIGHT_PAST_GATE.store(1, Ordering::SeqCst);

        let footprint = 4096u64; // required = 3276 MiB
        // low, low, then enough → the loop must park twice then admit.
        let sampler = scripted_sampler(vec![Some(500), Some(500), Some(8192)]);
        let s: &MemSampler = &sampler;

        wait_for_memory_gate(s, footprint, TEST_POLL).await;

        // Restore the shared counter for other tests.
        IN_FLIGHT_PAST_GATE.store(0, Ordering::SeqCst);
    }

    /// Floor-1 leave-alone: with NO other task in flight, the gate returns
    /// immediately even though the sampler always reports catastrophically low
    /// memory — the machine must never deadlock at zero embeds.
    #[tokio::test]
    async fn memory_gate_never_deadlocks_at_zero() {
        let _g = GATE_TEST_LOCK.lock().unwrap();
        reset_in_flight(); // in_flight == 0 → floor-1 active

        let footprint = 100_000u64; // absurd requirement no sample satisfies
        let sampler = scripted_sampler(vec![Some(1)]); // always ~nothing free
        let s: &MemSampler = &sampler;

        // Must return promptly (floor-1), NOT park forever.
        wait_for_memory_gate(s, footprint, TEST_POLL).await;
    }

    /// Unreadable memory (sampler always `None`) with others in flight →
    /// proceed (floor-1 leave-alone on an unknown reading), never park forever.
    #[tokio::test]
    async fn memory_gate_proceeds_when_unreadable() {
        let _g = GATE_TEST_LOCK.lock().unwrap();
        reset_in_flight();
        IN_FLIGHT_PAST_GATE.store(2, Ordering::SeqCst); // floor-1 inactive

        let sampler = scripted_sampler(vec![None]); // always unknown
        let s: &MemSampler = &sampler;
        wait_for_memory_gate(s, 4096, TEST_POLL).await;

        IN_FLIGHT_PAST_GATE.store(0, Ordering::SeqCst);
    }

    // ── the full public acquire path + RAII in-flight accounting ────────

    /// The admission semaphore must serialise to its capacity: N tasks racing
    /// through `acquire_update_all_admission` never exceed `cap` concurrent
    /// holders. (Mirrors the KG_SYNC_SEMAPHORE serialisation test.) Also
    /// verifies the in-flight-past-gate count returns to 0 after all permits
    /// drop (RAII accounting is balanced).
    ///
    /// NOTE: the process-global `OnceLock` means this test defines the
    /// semaphore capacity for the whole test process on first touch. To keep
    /// it deterministic regardless of test ordering, it drives the SAME
    /// public acquire path and asserts the *relative* invariant (peak
    /// concurrency <= the semaphore's actual capacity) rather than a hardcoded
    /// number. The default footprint gate is a no-op here because the real
    /// /proc/meminfo on a CI host has plenty free; even if it parked, floor-1
    /// guarantees forward progress so the test still terminates.
    #[tokio::test]
    async fn admission_never_exceeds_capacity() {
        let _g = GATE_TEST_LOCK.lock().unwrap();
        reset_in_flight();

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
                // `_permit` drops here, releasing the slot AND the in-flight
                // guard.
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
        // RAII in-flight accounting balanced back to zero.
        assert_eq!(IN_FLIGHT_PAST_GATE.load(Ordering::SeqCst), 0);
    }
}
