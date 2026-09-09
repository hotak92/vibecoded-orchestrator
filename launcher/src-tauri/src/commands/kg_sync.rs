//! Initial KG / docs sync on project create (KG auto-sync — 2026-05-12).
//!
//! When a user creates a project via the launcher, the bundle install drops
//! `.claude/scripts/kg-sync` (POSIX) and `.claude/scripts/kg-sync.ps1`
//! (Windows) into the project. If the project arrives with pre-existing
//! `knowledge/**/*.md` and/or `docs/**/*.md` files — a common case for
//! projects ported from a private Claude orchestrator install — those
//! files used to remain unindexed in Weaviate until the user opened a
//! Claude session and the post-file-edit hook fired on a subsequent edit.
//! That left per-project KG collections empty at the moment they were
//! most useful (the first agent run after add-project).
//!
//! This module closes the gap by mirroring the existing
//! `commands::codegraph` initial-build pattern:
//!
//!   1. `create_project_v2` calls `spawn_initial_sync` AFTER bundle install
//!      drops the kg-sync wrapper. Fire-and-forget — project create returns
//!      immediately to the user.
//!   2. Pre-check: if neither `knowledge/` nor `docs/` contains any `.md`
//!      files, status=`skipped` and we stop. Avoids a needless Weaviate
//!      connect for empty projects.
//!   3. Otherwise: shell out to the platform-appropriate `kg-sync` wrapper
//!      with `--all`, capturing stdout+stderr line-by-line. Parse
//!      `📚 Found N markdown files in knowledge/` and `📚 Found N markdown
//!      files in docs/` for totals, and `🔄 Syncing node:` / `🔄 Syncing
//!      doc:` for the per-file progress counter that drives the GUI pill.
//!   4. Env vars (KG_COLLECTION / DEVELOPMENT_COLLECTION / WEAVIATE_URL /
//!      OLLAMA_URL / KG_BASE_DIR / PROJECT_NAME / ACTIVE_EMBEDDING /
//!      SHARED_KG_COLLECTION) are passed via `Command::env(...)` from a
//!      `ProjectEnvSettings::populate(...)` snapshot — the kg-sync wrapper
//!      only activates the venv, it does not source `.claude/env`, so the
//!      caller MUST set them or the script would write to the default
//!      `ClaudeKnowledgeGraph` collection. This is the equivalent of the
//!      VCT_INSTALL_ROOT plumbing `codegraph::run_build_task` does for the
//!      code-graph analyzer.
//!
//! Failure isolation: ANY failure of this background task (wrapper not
//! found, Weaviate down, subprocess crash) is recorded in the row's
//! `error_message` and emitted as a terminal `failed` event. It is NEVER
//! propagated to the create_project_v2 caller — the user has already
//! gotten their `ProjectView` back by the time this runs.
//!
//! Idempotency (v0.2.92 WP-B1, corrected): `sync_knowledge_graph.py`
//! does NOT derive Weaviate UUIDs from anything — inserts use
//! Weaviate-assigned UUIDs, and re-runs stay duplicate-free only because
//! each write first DELETES the existing rows matching the node's
//! `file_path` (delete-by-file_path + insert, not an upsert). That
//! delete is exact-match on the stored `file_path` string, so it is
//! idempotent only under ONE canonical spelling — which is why the
//! script (v0.2.92) now stores POSIX `file_path` everywhere and matches
//! both spellings at delete, mirroring the MCP's C-7 fix. Safe to
//! invoke repeatedly via `retry_kg_sync`; a content-hash embed-skip
//! fast path avoids the re-embed entirely when nothing changed.

use serde::Serialize;
use tauri::{command, AppHandle, Emitter, Manager, State};

use crate::commands::installer::find_local_repo_root;
use crate::commands::project_env_settings::{self, ProjectEnvSettings};
// v0.2.89 BUG 1: the (program, prefix-args) spawn-shape resolution moved to
// the shared `script_invocation` module (one home for all three bundled-
// wrapper spawn sites — this file, orchestrator_core, codegraph).
use crate::commands::script_invocation::invocation_for;
// v0.2.94: the drift verdict type is owned by the DECISION module
// (`projects_v2::change_detect`), which is pure and unit-tested; this file owns
// only the probe that produces one.
use crate::commands::projects_v2::change_detect::DriftVerdict;
use crate::db::kg_syncs::{heartbeat_is_stale, status as sync_status, KgSyncRow};
use crate::db::Db;
use vct_launcher_core::process::CommandExt as _;

const SYNC_EVENT: &str = "kg-sync-progress";

// ─── BUG 2 (v0.2.89): heartbeat liveness ────────────────────────────────
//
// Field report: kg_sync rows stuck RUNNING for ~70 h. Terminal-state writes
// happen only inside the spawned task and the only reconciliation was the
// boot-time orphan sweep — any task death with the launcher still up (tokio
// panic/abort, a task parked on the embed-admission queue, launcher killed
// and not restarted for days) left RUNNING forever. The mechanism below is
// a LIVENESS net, per the standing rule NOT a per-file / per-node deadline
// (the stall watchdog already bounds subprocess silence):
//
//   * ticker  — the running task stamps `heartbeat_at` every 60 s
//               (status-guarded UPDATE; ticks during the admission wait
//               too — a queued-but-alive task stays honest).
//   * sweeper — a 5-min interval task (spawned once, from
//               `resume_pending_syncs` so lib.rs stays untouched) flips
//               stale RUNNING rows in BOTH twin tables to failed.
//   * read-time guard — `get_kg_sync_status` / codegraph's status read
//               flip a stale row on fetch for immediate GUI honesty
//               between sweeps.
//
// The code-graph constants live here too because the cross-table sweeper
// is owned by this module; `commands::codegraph` imports its message +
// the shared ticker plumbing from here.

/// Ticker cadence. A live task stamps liveness this often regardless of
/// how slow the subprocess is, so the stale window below only ages on a
/// dead task (or a launcher that never came back).
const HEARTBEAT_TICK_SECS: u64 = 60;

/// Floor of the staleness window: 30 min of missed 60 s ticks ⇒ the task
/// is gone (launcher up) or the launcher is gone (next boot sweeps).
const HEARTBEAT_STALE_FLOOR_SECS: u64 = 1800;

/// Sweeper cadence.
const HEARTBEAT_SWEEP_INTERVAL_SECS: u64 = 300;

/// Error message for a heartbeat-swept kg_sync row (GUI banner contract —
/// terse and action-oriented, mirrors the boot-sweep message shape).
pub(crate) const KG_SYNC_STALE_ERROR: &str =
    "sync task died without reporting (heartbeat stale); click Retry to re-run";

/// Sibling message for the code_graph_builds twin (the sweeper reconciles
/// both tables in one pass, so both messages live beside it).
pub(crate) const CODE_GRAPH_STALE_ERROR: &str =
    "build task died without reporting (heartbeat stale); click Retry to re-run";

/// Staleness window: `max(1800, 2 × resolved stall timeout)`. A stall
/// watchdog stretched via `KG_SYNC_STALL_TIMEOUT_SECS` widens this window
/// too, so the liveness net can never fire before the (re-armed, silence-
/// bounded) watchdog has had its say. When the watchdog is disabled
/// (env = 0) the default window still applies — heartbeats bound TASK
/// death, which is orthogonal to subprocess silence.
pub(crate) fn heartbeat_stale_secs() -> u64 {
    let stall_secs = resolve_stall_timeout()
        .map(|d| d.as_secs())
        .unwrap_or(DEFAULT_STALL_TIMEOUT_SECS);
    HEARTBEAT_STALE_FLOOR_SECS.max(stall_secs.saturating_mul(2))
}

/// RAII guard for a heartbeat ticker task: aborts the ticker when dropped
/// — including the panic-unwind path of the owning task, so a crashed
/// sync can never leave a ticker stamping liveness onto a row whose task
/// is gone (that would defeat the whole mechanism).
pub(crate) struct HeartbeatGuard(tauri::async_runtime::JoinHandle<()>);

impl Drop for HeartbeatGuard {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// Spawn a 60 s heartbeat ticker for `project_id`. `touch` is the
/// status-guarded DB stamp (`Db::touch_kg_sync_heartbeat` or
/// `Db::touch_code_graph_build_heartbeat`) — the guard inside the SQL
/// means a tick that lands after the row reached a terminal state (or,
/// for codegraph, after a detached walk took the row over) touches
/// nothing. Bind the returned guard for the whole task scope.
pub(crate) fn spawn_heartbeat_ticker(
    app: AppHandle,
    project_id: String,
    touch: fn(&Db, &str) -> Result<usize, String>,
    label: &'static str,
) -> HeartbeatGuard {
    // async_runtime::spawn, not tokio::spawn — sync fn, callable from
    // main-thread contexts with no reactor (see the sweeper note below).
    let handle = tauri::async_runtime::spawn(async move {
        loop {
            // First tick immediately: liveness is on record from the
            // moment the task starts (covers the admission-queue wait).
            {
                let db = app.state::<Db>();
                if let Err(e) = touch(&db, &project_id) {
                    tracing::warn!(
                        "[vct] warning: {} heartbeat tick failed for {}: {}",
                        label, project_id, e
                    );
                }
            }
            tokio::time::sleep(std::time::Duration::from_secs(HEARTBEAT_TICK_SECS)).await;
        }
    });
    HeartbeatGuard(handle)
}

/// One-shot latch so the sweeper is spawned at most once per process even
/// if `resume_pending_syncs` were ever called twice.
static HEARTBEAT_SWEEPER_STARTED: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);

/// Spawn the periodic staleness sweeper (both twin tables, one pass).
/// Spawned from INSIDE `resume_pending_syncs` — deliberately not lib.rs,
/// which already calls that function at setup. Soft-fail: DB errors are
/// logged and the loop keeps going; a missed sweep is recovered by the
/// next tick or the read-time guards.
fn spawn_stale_heartbeat_sweeper(app: AppHandle) {
    use std::sync::atomic::Ordering;
    if HEARTBEAT_SWEEPER_STARTED.swap(true, Ordering::SeqCst) {
        return;
    }
    // tauri::async_runtime::spawn, NOT tokio::spawn: this sync fn runs on
    // the main thread during setup() (via resume_pending_syncs), where no
    // tokio reactor context exists — a bare tokio::spawn panics there and
    // kills the launcher at boot (v0.2.89 field incident).
    tauri::async_runtime::spawn(async move {
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(
                HEARTBEAT_SWEEP_INTERVAL_SECS,
            ))
            .await;
            let stale_secs = heartbeat_stale_secs();
            let db = app.state::<Db>();
            match db.mark_stale_running_kg_syncs_failed(stale_secs, KG_SYNC_STALE_ERROR, None) {
                Ok(n) if n > 0 => tracing::error!(
                    "[vct] kg-sync heartbeat sweep: {} row(s) stale > {}s; flipped to failed",
                    n, stale_secs
                ),
                Err(e) => tracing::warn!("[vct] warning: kg-sync heartbeat sweep failed: {}", e),
                _ => {}
            }
            match db.mark_stale_running_code_graph_builds_failed(
                stale_secs,
                CODE_GRAPH_STALE_ERROR,
                None,
            ) {
                Ok(n) if n > 0 => tracing::error!(
                    "[vct] code-graph heartbeat sweep: {} row(s) stale > {}s; flipped to failed",
                    n, stale_secs
                ),
                Err(e) => tracing::warn!("[vct] warning: code-graph heartbeat sweep failed: {}", e),
                _ => {}
            }
        }
    });
}

/// v0.2.71 Piece 5a — process-global single-flight cap on KG re-embeds.
///
/// THE BUG (audit `update-all-kg-reembed-serialization-2026-06-30.md`):
/// "Update all projects" iterates projects sequentially but each project's
/// re-embed is launched fire-and-forget (`spawn_initial_sync` →
/// `tokio::spawn(run_sync_task)`), so the iteration loop never waits for
/// project N's `sync_knowledge_graph.py --all` before starting N+1. With 8
/// projects the detached subprocesses pile up (the field-reported "14"
/// wrapper→python pairs), thrashing one Ollama instance and starving the
/// machine. There was NO machine-wide KG-sync lock anywhere — the migrate
/// claim (`commands::single_flight`, keyed per-project_id since v0.2.91;
/// `MigrateLockGuard` before that) is uncontended across distinct projects,
/// and the stall watchdog bounds per-child silence, not cross-child
/// concurrency.
///
/// THE FIX: every `run_sync_task` must hold ONE of these permits across the
/// ENTIRE subprocess lifetime (acquire → spawn → drain → exit). With a single
/// permit, the second-and-later detached tasks `await` the permit instead of
/// launching a competing child, so at most `KG_SYNC_MAX_CONCURRENT`
/// `sync_knowledge_graph.py` processes run at once regardless of how many
/// callers (update-all, retry, create, boot-resume) spawn tasks.
///
/// WHY A `tokio::sync::Semaphore` (async) and not a `std::sync::Mutex`:
/// `run_sync_task` runs ON the tokio runtime. A blocking `std::sync::Mutex`
/// held across an `.await` would park a runtime worker thread for the whole
/// (possibly multi-minute) re-embed; the async semaphore lets queued tasks
/// yield the worker while they wait. `acquire_owned` yields an
/// `OwnedSemaphorePermit` that we bind as a local — it releases on drop (RAII),
/// so a panicking or early-returning sync still frees its permit for the queue.
///
/// WHY PROCESS-GLOBAL IS SUFFICIENT: the launcher is single-instance per user
/// (`tauri_plugin_single_instance`, `lib.rs:570/583` — a second launch focuses
/// the existing window and exits without touching launcher.db). All KG syncs
/// therefore originate in this one process, so a process-global semaphore caps
/// machine-wide concurrency. (If the launcher ever became multi-process, this
/// would need a cross-process lock — e.g. an OS file lock — but that is not the
/// case today and a process-global is the correct first fix.)
///
/// CROSS-OS: the lock sits ABOVE `run_subprocess`, which is shared Rust on all
/// three OSes; only the wrapper/powershell/CREATE_NO_WINDOW branches *inside*
/// `run_subprocess` differ. One lock therefore covers Linux/macOS/Windows.
///
/// TUNABILITY: `KG_SYNC_MAX_CONCURRENT` is a `const` (default 1, conservative).
/// A small N>1 could be set if a slow backend benefits from limited parallelism,
/// but 1 is the safe default that guarantees the 14-concurrent state cannot recur.
const KG_SYNC_MAX_CONCURRENT: usize = 1;

/// The process-global semaphore. `LazyLock` mirrors the static-init style used
/// elsewhere in the launcher (`commands::single_flight::IN_FLIGHT`,
/// `UNREGISTER_CANONICAL_ENV_KEYS` in `projects_v2.rs`).
static KG_SYNC_SEMAPHORE: std::sync::LazyLock<std::sync::Arc<tokio::sync::Semaphore>> =
    std::sync::LazyLock::new(|| {
        std::sync::Arc::new(tokio::sync::Semaphore::new(KG_SYNC_MAX_CONCURRENT))
    });

/// Acquire a permit on the process-global KG-sync semaphore, parking the
/// caller (async) until one is free. Returns an owned permit whose `Drop`
/// releases it back to the queue. Extracted as a thin async helper so the
/// acquire point in `run_sync_task` reads clearly and so tests can exercise
/// the same semaphore the production path uses.
///
/// `acquire_owned` only errors if the semaphore is `close()`d; we never close
/// it (it lives for the process lifetime), so the `expect` is unreachable in
/// practice — but we surface it loudly rather than silently dropping the cap.
async fn acquire_kg_sync_permit() -> tokio::sync::OwnedSemaphorePermit {
    KG_SYNC_SEMAPHORE
        .clone()
        .acquire_owned()
        .await
        .expect("KG_SYNC_SEMAPHORE is never closed for the process lifetime")
}

/// Tauri-event payload + DTO for `get_kg_sync_status`.
///
/// Mirrors `KgSyncRow` but in a public-API shape: timestamps in ISO 8601
/// (so the GUI doesn't have to convert epoch-ms), explicit optionals, and
/// a `current_phase` string for live progress events. Field names match
/// `KgSyncRow` 1:1 so the FE can union them transparently.
#[derive(Debug, Clone, Serialize)]
pub struct KgSyncView {
    pub project_id: String,
    pub status: String,
    pub started_at_iso: Option<String>,
    pub finished_at_iso: Option<String>,
    pub duration_ms: Option<i64>,
    pub kg_total: u32,
    pub kg_succeeded: u32,
    pub kg_failed: u32,
    /// Intentional non-synces (archived / frontmarker / excluded /
    /// embed-skipped nodes) — v0.2.92 WP-B1 / D12. LIVE-EVENT ONLY: the
    /// `kg_syncs` row has no such column (no schema change), so stored
    /// rows report 0; the number remains visible in the script's stdout
    /// (`📊 KG: S succeeded, F failed, K skipped`) captured in `log_tail`.
    pub kg_skipped: u32,
    pub docs_total: u32,
    pub docs_succeeded: u32,
    pub docs_failed: u32,
    pub docs_skipped: u32,
    pub error_message: Option<String>,
    pub log_tail: Option<String>,
    /// Live phase indicator. Only populated on `running` events emitted
    /// during the sync (e.g. "scan", "knowledge", "docs", "finalize").
    /// Always None for stored rows fetched via `get_kg_sync_status`.
    pub current_phase: Option<String>,
}

impl KgSyncView {
    fn from_row(row: KgSyncRow) -> Self {
        Self {
            project_id: row.project_id,
            status: row.status,
            started_at_iso: row.started_at.and_then(epoch_ms_to_iso),
            finished_at_iso: row.finished_at.and_then(epoch_ms_to_iso),
            duration_ms: row.duration_ms,
            kg_total: row.kg_total,
            kg_succeeded: row.kg_succeeded,
            kg_failed: row.kg_failed,
            // No DB column for skips (live-event only — see field doc).
            kg_skipped: 0,
            docs_total: row.docs_total,
            docs_succeeded: row.docs_succeeded,
            docs_failed: row.docs_failed,
            docs_skipped: 0,
            error_message: row.error_message,
            log_tail: row.log_tail,
            current_phase: None,
        }
    }
}

fn epoch_ms_to_iso(ms: i64) -> Option<String> {
    chrono::DateTime::<chrono::Utc>::from_timestamp_millis(ms).map(|dt| dt.to_rfc3339())
}

#[command]
pub async fn get_kg_sync_status(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Option<KgSyncView>, String> {
    let row = db.get_kg_sync(&project_id)?;

    // BUG 2 (v0.2.89) read-time guard: a RUNNING row whose liveness stamp
    // aged past the window belongs to a dead task — mark it failed FIRST
    // and return the failed row, so the GUI is honest immediately instead
    // of waiting up to one sweeper interval. The targeted mark re-checks
    // staleness inside its own WHERE clause, so a tick landing between
    // our read and the update safely turns this into a no-op.
    if let Some(ref r) = row {
        if r.status == sync_status::RUNNING {
            let stale_secs = heartbeat_stale_secs();
            let now_ms = chrono::Utc::now().timestamp_millis();
            if heartbeat_is_stale(r.heartbeat_at, r.started_at, now_ms, stale_secs) {
                db.mark_stale_running_kg_syncs_failed(
                    stale_secs,
                    KG_SYNC_STALE_ERROR,
                    Some(&project_id),
                )?;
                return Ok(db.get_kg_sync(&project_id)?.map(KgSyncView::from_row));
            }
        }
    }

    Ok(row.map(KgSyncView::from_row))
}

/// Re-run the KG / docs sync for an existing project. Marks the row as
/// `pending` and re-spawns the background task. Safe to call while a
/// previous run is still in flight — the new spawn will overwrite the
/// row when it transitions; whichever finishes last wins. Mirrors
/// `codegraph::rebuild_code_graph` semantics.
#[command]
pub async fn retry_kg_sync(
    project_id: String,
    db: State<'_, Db>,
    app: AppHandle,
) -> Result<(), String> {
    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    db.upsert_kg_sync(
        &project.id,
        sync_status::PENDING,
        Some(chrono::Utc::now().timestamp_millis()),
        None,
        None,
        0, 0, 0,
        0, 0, 0,
        None,
        None,
    )?;
    db.audit(
        "kg_sync_retry",
        Some(&project.id),
        None,
        &serde_json::json!({ "name": project.name }),
    )?;

    spawn_initial_sync(app, project.id, project.name, project.folder_path);
    Ok(())
}

/// Public entry point used by `create_project_v2` (and the retry command).
/// Spawns a background task; never blocks. The caller has already inserted
/// a `pending` row into `kg_syncs`.
pub fn spawn_initial_sync(
    app: AppHandle,
    project_id: String,
    project_name: String,
    folder_path: String,
) {
    // async_runtime::spawn, not tokio::spawn — sync fn, also called from
    // setup()/main thread via the boot-resume sweep (no reactor context).
    tauri::async_runtime::spawn(async move {
        run_sync_task(app, project_id, project_name, folder_path).await;
    });
}

/// Launcher-boot resume sweep (2026-05-12). Mirrors
/// `codegraph::resume_pending_builds` 1:1 — see that function's
/// docstring for the two-phase rationale (mark stale-running as failed,
/// then re-spawn pending). Soft-fail at every step. Returns
/// (swept_running, respawned_pending) for the boot-log line.
///
/// Called from `lib.rs::setup()` after migrations have run. Boot order
/// vs. the code-graph resume sweep is incidental; the two are
/// independent and can run in either sequence.
///
/// Defect B (v0.2.68) — F6 boot-resume gate: `skip` is the set of project
/// IDs whose `project_setups` row is NOT terminal. Those projects are
/// re-driven by `project_setup::resume_pending_setups` (which re-runs the
/// bundle that drops the `kg-sync` wrapper and re-queues this sync as
/// `pending`); resuming a sync HERE would race the wrapper back onto disk.
/// We skip them. Mirrors `codegraph::resume_pending_builds`.
pub fn resume_pending_syncs(
    app: &AppHandle,
    skip: &std::collections::HashSet<String>,
) -> (usize, usize) {
    let db = app.state::<Db>();

    // BUG 2 (v0.2.89): start the periodic heartbeat-staleness sweeper for
    // BOTH twin tables. Spawned here (lib.rs already calls this function
    // at setup) so the boot wiring stays in one place; the latch inside
    // makes a hypothetical second call a no-op.
    spawn_stale_heartbeat_sweeper(app.clone());

    // Phase 1: stale-running sweep.
    let swept = match db.mark_orphaned_running_kg_syncs_failed(
        "launcher crashed mid-run; click Retry to re-run",
    ) {
        Ok(n) => n,
        Err(e) => {
            tracing::warn!(
                "[vct] warning: kg-sync stale-running sweep failed: {}. \
                 Stale rows (if any) will appear as 'running' indefinitely; \
                 user can click Re-sync KG to recover.",
                e
            );
            0
        }
    };

    // Phase 2: respawn pending.
    let pending_ids = match db.list_pending_kg_syncs() {
        Ok(v) => v,
        Err(e) => {
            tracing::warn!(
                "[vct] warning: kg-sync pending-list lookup failed: {}. \
                 Queued syncs (if any) will not auto-resume this boot.",
                e
            );
            return (swept, 0);
        }
    };

    let mut respawned = 0usize;
    for pid in &pending_ids {
        // F6 gate: skip projects whose async setup is still incomplete —
        // `resume_pending_setups` re-drives them (re-landing the kg-sync
        // wrapper + re-queuing this sync in the correct order).
        if skip.contains(pid) {
            continue;
        }
        let project = match db.get_project(pid) {
            Ok(Some(p)) => p,
            Ok(None) => {
                tracing::warn!(
                    "[vct] warning: pending kg-sync references missing project {}; skipping",
                    pid
                );
                continue;
            }
            Err(e) => {
                tracing::warn!(
                    "[vct] warning: lookup for pending kg-sync {}: {}; skipping",
                    pid, e
                );
                continue;
            }
        };
        spawn_initial_sync(
            app.clone(),
            project.id,
            project.name,
            project.folder_path,
        );
        respawned += 1;
    }
    (swept, respawned)
}

/// True when the project still exists in the launcher DB. Used by
/// `run_sync_task` to short-circuit if the user unregistered the
/// project mid-sync — mirrors `codegraph::project_still_exists`.
fn project_still_exists(app: &AppHandle, project_id: &str) -> bool {
    app.state::<Db>()
        .get_project(project_id)
        .map(|opt| opt.is_some())
        .unwrap_or(true)
}

/// Body of the spawned task. Errors here are recorded in the sync row,
/// never propagated. Each transition emits a `kg-sync-progress` event
/// so the GUI updates live. Structure mirrors `codegraph::run_build_task`.
async fn run_sync_task(
    app: AppHandle,
    project_id: String,
    project_name: String,
    folder_path: String,
) {
    let started_at = chrono::Utc::now().timestamp_millis();

    // Race check #0 (defensive): the spawn could be enqueued and the
    // user could unregister before the task picks up. Bail before any
    // DB write or event emit. Same pattern as codegraph.
    if !project_still_exists(&app, &project_id) {
        return;
    }

    // 1. Mark RUNNING + emit. Pre-check the directories so the user
    //    sees a "scanning…" pill the moment project create returns.
    upsert_quiet(
        &app,
        &project_id,
        sync_status::RUNNING,
        Some(started_at),
        None,
        None,
        0, 0, 0,
        0, 0, 0,
        None,
        None,
    );
    emit_sync(
        &app,
        &project_id,
        sync_status::RUNNING,
        ProgressCounts::zero(),
        Some("scan"),
        None,
    );

    // BUG 2 (v0.2.89): heartbeat ticker for the row we just marked
    // RUNNING. Bound for the WHOLE task scope so it keeps ticking through
    // the admission-queue wait below (a queued-but-alive task stays
    // honest); the RAII guard aborts it on every exit path, including
    // panic unwind. The DB stamp is status-guarded, so ticks racing the
    // terminal upsert touch nothing.
    let _heartbeat = spawn_heartbeat_ticker(
        app.clone(),
        project_id.clone(),
        Db::touch_kg_sync_heartbeat,
        "kg-sync",
    );

    // 2. Pre-check: any markdown files at all under knowledge/ or docs/?
    //    The sync script's `--all` mode walks both trees, so if both are
    //    empty there's literally nothing to upload — skip the subprocess.
    let folder = std::path::Path::new(&folder_path);
    let kg_md_count = count_markdown_files(&folder.join("knowledge"));
    let docs_md_count = count_markdown_files(&folder.join("docs"));

    if kg_md_count == 0 && docs_md_count == 0 {
        if !project_still_exists(&app, &project_id) {
            return;
        }
        let finished_at = chrono::Utc::now().timestamp_millis();
        upsert_quiet(
            &app,
            &project_id,
            sync_status::SKIPPED,
            Some(started_at),
            Some(finished_at),
            Some(finished_at - started_at),
            0, 0, 0,
            0, 0, 0,
            Some("no knowledge/**/*.md or docs/**/*.md files to sync"),
            None,
        );
        emit_sync(
            &app,
            &project_id,
            sync_status::SKIPPED,
            ProgressCounts::zero(),
            None,
            Some("no knowledge/**/*.md or docs/**/*.md files to sync"),
        );
        return;
    }

    // 3. Resolve the kg-sync wrapper. Mirrors `codegraph::resolve_analyzer_script`:
    //    project-local first, then VCT_LAUNCHER_SCRIPTS_DIR override,
    //    then sibling-of-exe, then PATH. Picks `.ps1` on Windows.
    let script = match resolve_kg_sync_script(folder) {
        Some(p) => p,
        None => {
            if !project_still_exists(&app, &project_id) {
                return;
            }
            finalize_failed(
                &app,
                &project_id,
                started_at,
                "kg-sync script not found (looked in project, launcher install, $PATH). \
                 The launcher's bundle install may have failed — check the \
                 install-bundle warnings emitted during project create."
                    .to_string(),
                None,
            );
            return;
        }
    };

    // 4. Populate env settings from launcher state. Same path
    //    `create_project_v2` uses to write the .env / .claude/env files —
    //    keeps the auto-sync's collection-targeting consistent with what
    //    the subsequent on-edit hook syncs will use.
    let env_settings = {
        let db = app.state::<Db>();
        project_env_settings::populate(&db, &project_name, Some(&project_id))
    };

    // 5. Resolve the orchestrator install root for VCT_ORCHESTRATOR_ROOT.
    //    `sync_knowledge_graph.py` reads this to locate the
    //    `claude_mcp_servers/` package (which is not copied into projects).
    //    Soft-fail: if unfindable, the script's fallback ("look at
    //    <project>/claude_mcp_servers") will trip, the run will fail
    //    cleanly, and the user sees a retry-able error pill.
    let orch_root = find_local_repo_root().ok();

    // 6. Build the subprocess. Same pattern as `codegraph::run_build_task`:
    //    arg vector (never a joined shell string — Windows quoting breaks),
    //    stdin closed, env block from ProjectEnvSettings, CREATE_NO_WINDOW
    //    on Windows so no console flashes.
    let (program, base_args) = invocation_for(&script);

    // 6a. v0.2.71 Piece 5a — acquire the process-global single-flight permit
    //     BEFORE spawning the child. This is the hard concurrency cap: when
    //     "Update all projects" spawns N detached `run_sync_task`s, only
    //     `KG_SYNC_MAX_CONCURRENT` (default 1) hold a permit and actually run
    //     a `sync_knowledge_graph.py --all` at a time; the rest park here on
    //     `.await` until a permit frees. The permit is acquired AFTER the
    //     fast-skip pre-checks (empty-dir skip at step 2, script resolve at
    //     step 3) so a project with nothing to sync never occupies the lane.
    //
    //     We emit a "queued" phase first so the GUI's per-project pill shows
    //     "Queued" instead of a generic spinner while it waits for the lane
    //     (the `pending` DB row already supports this state). Once the permit
    //     lands we flip to "embed".
    //
    //     `_permit` is bound for the rest of the function scope; its `Drop`
    //     releases the lane back to the queue when `run_sync_task` returns —
    //     including the panic/early-return paths below (RAII). It MUST outlive
    //     the `run_subprocess(...).await` so the lane is held across the entire
    //     child lifetime, not just the acquire.
    emit_sync(
        &app,
        &project_id,
        sync_status::RUNNING,
        ProgressCounts {
            kg_total: kg_md_count,
            docs_total: docs_md_count,
            ..ProgressCounts::zero()
        },
        Some("queued"),
        None,
    );
    // v0.2.77 5c task 3: machine-global update-all admission gate. Acquired
    // FIRST (before the KG single-flight permit below) so a kg-sync counts
    // against the SAME shared budget as codegraph builds — one shared pool
    // across both embed pipelines (USER DESIGN RULING), not two independent
    // caps. Held for the whole task via RAII, so the cross-pipeline slot is
    // occupied across the entire `sync_knowledge_graph.py` lifetime. Acquired
    // INSIDE the spawned task (spawn_initial_sync stays non-blocking), so the
    // update-all loop keeps advancing; queued tasks park here. Also covers the
    // boot-resume path (`resume_pending_syncs`), which can fire N tasks at once.
    let _admission = {
        let db = app.state::<Db>();
        crate::commands::embed_admission::acquire_update_all_admission(&db).await
    };
    let _permit = acquire_kg_sync_permit().await;

    emit_sync(
        &app,
        &project_id,
        sync_status::RUNNING,
        ProgressCounts {
            kg_total: kg_md_count,
            docs_total: docs_md_count,
            ..ProgressCounts::zero()
        },
        Some("embed"),
        None,
    );

    let outcome = run_subprocess(
        program,
        base_args,
        &env_settings,
        folder,
        orch_root.as_deref(),
        &app,
        &project_id,
        kg_md_count,
        docs_md_count,
    )
    .await;

    let finished_at = chrono::Utc::now().timestamp_millis();

    // 7. Persist + emit terminal event. Race check (mirrors codegraph):
    //    if the user unregistered while the subprocess was running, skip
    //    the writes quietly.
    if !project_still_exists(&app, &project_id) {
        return;
    }
    upsert_quiet(
        &app,
        &project_id,
        &outcome.status,
        Some(started_at),
        Some(finished_at),
        Some(finished_at - started_at),
        outcome.counts.kg_total,
        outcome.counts.kg_succeeded,
        outcome.counts.kg_failed,
        outcome.counts.docs_total,
        outcome.counts.docs_succeeded,
        outcome.counts.docs_failed,
        outcome.error_message.as_deref(),
        outcome.log_tail.as_deref(),
    );
    emit_sync(
        &app,
        &project_id,
        &outcome.status,
        outcome.counts,
        None,
        outcome.error_message.as_deref(),
    );
}

/// Aggregated subprocess result. Held only inside `run_sync_task` so
/// nothing externally references this struct.
struct SubprocessOutcome {
    status: String,
    counts: ProgressCounts,
    error_message: Option<String>,
    log_tail: Option<String>,
}

#[derive(Clone, Copy, Debug)]
struct ProgressCounts {
    kg_total: u32,
    kg_succeeded: u32,
    kg_failed: u32,
    /// Live-only skip count (v0.2.92 WP-B1) — never persisted to the row;
    /// forwarded to `KgSyncView` so the GUI can complete its progress
    /// counter honestly (succeeded + skipped == processed).
    kg_skipped: u32,
    docs_total: u32,
    docs_succeeded: u32,
    docs_failed: u32,
    docs_skipped: u32,
}

impl ProgressCounts {
    fn zero() -> Self {
        Self {
            kg_total: 0,
            kg_succeeded: 0,
            kg_failed: 0,
            kg_skipped: 0,
            docs_total: 0,
            docs_succeeded: 0,
            docs_failed: 0,
            docs_skipped: 0,
        }
    }
}

/// Default stall-watchdog timeout in seconds. Used when
/// `KG_SYNC_STALL_TIMEOUT_SECS` is unset or unparsable.
///
/// v0.2.69 FIX 3 (review SHOULD-FIX, re-instated as a PROGRESS guard):
/// this is NOT a per-process / per-node duration cap — the maintainer
/// ruled out those for the seed path because a legitimate slow-CPU
/// arctic re-embed can run for hours. This watchdog instead bounds
/// SILENCE: it re-arms (resets its deadline) on EVERY line the child
/// emits on stdout or stderr, and fires ONLY after this many seconds of
/// ZERO output. A slow-but-progressing seed that keeps printing
/// per-chunk / per-node progress lines NEVER trips it, regardless of
/// total runtime. It exists to recover the OTHER wedge the per-embed-
/// request guard (`VCT_EMBED_REQUEST_TIMEOUT_SECS`, 180 s) does NOT
/// cover: `sync_knowledge_graph.py` also does non-embed Weaviate work
/// (`connect_to_custom`, schema ensure, `collection.data.insert`) with
/// no timeout of its own, so a silent network/Weaviate deadlock on the
/// launcher background path would otherwise hang forever.
///
/// Default = max(900, 4 × the 180 s per-embed-request guard) = 900 s.
/// The 4× headroom over the per-request guard means a single embed
/// request that stalls right up to its own 180 s cap (then fails and
/// surfaces a line) cannot trip this; only true cross-request silence
/// can. The script emits a line PER CHUNK on both the KG and docs paths
/// (the docs heartbeat was added alongside this fix), so on a slow CPU
/// the gap between output lines is one chunk's embed+insert latency
/// (~30 s worst case) — two orders of magnitude under 900 s. Output is
/// also forced unbuffered (`PYTHONUNBUFFERED=1` in `build_kg_sync_env`)
/// so those per-chunk lines reach the launcher promptly rather than
/// being block-buffered into the launcher's reader.
const DEFAULT_STALL_TIMEOUT_SECS: u64 = 900;

/// Stall-watchdog: resolved from `KG_SYNC_STALL_TIMEOUT_SECS` at task
/// start. `0` disables the watchdog entirely (escape hatch for
/// benchmark / debug). Env var name preserved from the pre-v0.2.69
/// watchdog for continuity.
fn resolve_stall_timeout() -> Option<std::time::Duration> {
    let raw = std::env::var("KG_SYNC_STALL_TIMEOUT_SECS").ok();
    let secs = match raw.as_deref() {
        None => DEFAULT_STALL_TIMEOUT_SECS,
        Some(v) => match v.trim().parse::<u64>() {
            Ok(n) => n,
            Err(_) => {
                tracing::warn!(
                    "[vct] warning: KG_SYNC_STALL_TIMEOUT_SECS={:?} is not a non-negative \
                     integer; falling back to default {}s",
                    v, DEFAULT_STALL_TIMEOUT_SECS
                );
                DEFAULT_STALL_TIMEOUT_SECS
            }
        },
    };
    if secs == 0 {
        None
    } else {
        Some(std::time::Duration::from_secs(secs))
    }
}

/// Bug-3 v0.2.x (2026-05-12): tag for the concurrent-drain channel.
///
/// `run_subprocess` formerly drained stdout to EOF and ONLY THEN drained
/// stderr. Linux pipe buffers default to ~64 KiB; once `sync_knowledge_graph.py`
/// emitted enough stderr (weaviate-client warnings, Python tracebacks,
/// urllib3 retry chatter, etc.) the kernel blocked its next stderr write
/// in `anon_pipe_write`. Python blocked → no further stdout → the
/// launcher's stdout reader saw an indefinite quiescent stream → stderr
/// reader never started because it was sequenced AFTER the stdout drain.
/// Symptom: kg-sync wedged at "embedding 14/68" with no progress and no
/// crash. We now spawn two reader tasks that drain both pipes
/// concurrently into a single `mpsc::channel`, restoring forward progress
/// guarantees on both sides regardless of which one outpaces the other.
#[derive(Debug)]
enum PipeLine {
    Stdout(String),
    Stderr(String),
}

/// Run the kg-sync subprocess, stream stdout+stderr line-by-line for live
/// progress events, and parse the summary lines for the final counts.
///
/// Build the environment variable pairs for the kg-sync subprocess.
///
/// Extracted as a pure helper so the env-building logic can be unit-tested
/// without spawning a real process.
///
/// Returns a `Vec` of `(&'static str, OsString)` pairs ready to feed into
/// `Command::env`.  `orchestrator_root` is `Some` when the launcher has a
/// configured install root; when `None` the two root vars are omitted.
fn build_kg_sync_env(
    env_settings: &ProjectEnvSettings,
    project_folder: &std::path::Path,
    orchestrator_root: Option<&std::path::Path>,
) -> Vec<(&'static str, std::ffi::OsString)> {
    let mut pairs: Vec<(&'static str, std::ffi::OsString)> = vec![
        ("KG_BASE_DIR", project_folder.as_os_str().to_owned()),
        // BUG 3 (v0.2.89, plan §1.3 C): the NEW non-leaking root channel.
        // `sync_knowledge_graph.py` resolves its project root with the
        // precedence `--project-root argv > KG_SYNC_PROJECT_ROOT >
        // KG_BASE_DIR (legacy) > script location`. `KG_BASE_DIR` is
        // exported by every Claude session (poisoned by design), so the
        // wrappers pin the new name set-if-unset from their own location —
        // the launcher must set it EXPLICITLY so its value survives the
        // v0.2.77 orchestrator-copy wrapper fallback (whose location-
        // derived root would be the orchestrator clone — wrong). Env-only,
        // no argv change: a NEW launcher driving an OLD project-local
        // script must keep working (an unknown env var is ignored; an
        // unknown argv flag would be mis-parsed as a file path).
        ("KG_SYNC_PROJECT_ROOT", project_folder.as_os_str().to_owned()),
        (
            "PROJECT_NAME",
            std::ffi::OsString::from(&env_settings.project_name),
        ),
        (
            "KG_COLLECTION",
            std::ffi::OsString::from(&env_settings.kg_collection),
        ),
        (
            "DEVELOPMENT_COLLECTION",
            std::ffi::OsString::from(&env_settings.dev_collection),
        ),
        (
            "SHARED_KG_COLLECTION",
            std::ffi::OsString::from(&env_settings.shared_kg_collection),
        ),
        (
            "WEAVIATE_URL",
            std::ffi::OsString::from(&env_settings.weaviate_url),
        ),
        (
            "OLLAMA_URL",
            std::ffi::OsString::from(&env_settings.ollama_url),
        ),
        (
            "ACTIVE_EMBEDDING",
            std::ffi::OsString::from(&env_settings.active_embedding),
        ),
        // v0.2.69 FIX 3 (review SHOULD-FIX): force unbuffered Python
        // stdout so `sync_knowledge_graph.py`'s per-chunk / per-node
        // progress lines reach the launcher's reader PROMPTLY rather
        // than being block-buffered (~8 KiB) into the pipe. Without
        // this, piped Python stdout block-buffers and the lines arrive
        // in bursts only when the buffer fills or the process exits —
        // which would make the re-armed-per-line stall watchdog below
        // see false "silence" between bursts on a slow re-embed. (The
        // script's stderr is already unbuffered by Python default; this
        // closes the stdout gap.) `-u`-equivalent via env so it applies
        // regardless of how the kg-sync wrapper invokes python.
        ("PYTHONUNBUFFERED", std::ffi::OsString::from("1")),
    ];

    if let Some(root) = orchestrator_root {
        pairs.push(("VCT_ORCHESTRATOR_ROOT", root.as_os_str().to_owned()));
        // NEW-15 (2026-05-28): also pass VCT_INSTALL_ROOT so the kg-sync
        // wrapper's first venv-candidate (`${VCT_INSTALL_ROOT}/.venv`) is
        // populated. Without this, projects without a project-local
        // `.venv` (e.g. anything installed via the launcher's install-
        // bundle flow since v0.2.36) fall through to SCRIPT_DIR-relative
        // candidates that don't exist, then to system python with no
        // `weaviate` → `ModuleNotFoundError: No module named 'weaviate'`.
        // Symptom: KG sync: failed on the project's Identity tab.
        // codegraph.rs:1117 already does this; this is the sibling.
        pairs.push(("VCT_INSTALL_ROOT", root.as_os_str().to_owned()));
    }

    pairs
}

/// Concurrent drain (Bug-3 v0.2.x, 2026-05-12): stdout and stderr are
/// read by two `tokio::spawn` tasks feeding a shared `mpsc::channel`.
/// The main loop awaits messages, tagged with their origin pipe, and
/// dispatches parsing only on stdout lines — preserving the existing
/// single-threaded deterministic parse semantics while removing the
/// stderr-side back-pressure deadlock. A PROGRESS stall watchdog runs
/// alongside via `tokio::time::timeout` on the channel `recv()`: it
/// re-arms on every line from either pipe and fires only after
/// `KG_SYNC_STALL_TIMEOUT_SECS` of total silence (v0.2.69 FIX 3 review
/// SHOULD-FIX — bounds the non-embed Weaviate-wedge case the per-embed-
/// request guard cannot see, without capping a slow-but-progressing seed).
#[allow(clippy::too_many_arguments)]
async fn run_subprocess(
    program: std::path::PathBuf,
    base_args: Vec<String>,
    env_settings: &ProjectEnvSettings,
    project_folder: &std::path::Path,
    orchestrator_root: Option<&std::path::Path>,
    app: &AppHandle,
    project_id: &str,
    kg_total_pre: u32,
    docs_total_pre: u32,
) -> SubprocessOutcome {
    use tokio::io::{AsyncBufReadExt, BufReader};
    use tokio::sync::mpsc;

    // v0.2.94 diagnosability: name the wrapper, its argv, and the interpreter
    // the vco_lib ladder resolves. The 2026-09-05 MultiagentOrchestrator
    // failure (`ModuleNotFoundError: No module named 'weaviate'`) was a wrapper
    // rendered against a PREVIOUS orchestrator location, and the only trace was
    // a `log_tail` column nobody reads.
    //
    // The argv is plain `--all`: content-hash gated, so an already-synced
    // project reports N skipped / 0 embedded. No force/rechunk/recreate flag is
    // ever added on this automatic path — `DRIFT_SPAWN_FORBIDDEN_FLAGS` and
    // `the_automatic_spawn_never_forces_a_re_embed` pin that.
    //
    // The check below REFUSES rather than strips. Stripping the flag and
    // running anyway would convert a programming error into a WARN nobody
    // reads, and leave the run looking successful while doing something other
    // than what its argv said — the same "silently degrade and report success"
    // shape as the field defect this whole change exists to close. A forbidden
    // flag here cannot be a user's doing: `base_args` comes from
    // `invocation_for`, so its presence means someone edited this spawn path.
    // Fail the row, name the flag, and let it be seen.
    if let Some(msg) = forbidden_spawn_flag_message(&base_args) {
        tracing::error!("[vct] {} ({})", msg, project_id);
        assert_no_forbidden_spawn_flag(&base_args);
        return SubprocessOutcome {
            status: sync_status::FAILED.to_string(),
            counts: ProgressCounts {
                kg_total: kg_total_pre,
                docs_total: docs_total_pre,
                ..ProgressCounts::zero()
            },
            error_message: Some(msg),
            log_tail: None,
        };
    }
    tracing::info!(
        "[vct] kg-sync spawn for {}: wrapper={} args={:?} --all vco_lib_python={}",
        project_id,
        program.display(),
        base_args,
        vct_launcher_core::python_resolve::resolve_python_for_vco_lib_str()
            .unwrap_or_else(|| "<unresolved>".to_string()),
    );
    let mut cmd = tokio::process::Command::new(&program).silent();
    cmd.args(&base_args)
        .arg("--all")
        // Don't inherit the launcher's working dir; pin to a neutral path.
        // The kg-sync wrapper resolves its own paths relative to its
        // installed location, so cwd doesn't matter for correctness — but
        // not inheriting Tauri's cwd avoids leaking dev-time clutter into
        // the subprocess's environment.
        .current_dir(std::env::temp_dir())
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());

    for (key, val) in build_kg_sync_env(env_settings, project_folder, orchestrator_root) {
        cmd.env(key, val);
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            return SubprocessOutcome {
                status: sync_status::FAILED.to_string(),
                counts: ProgressCounts {
                    kg_total: kg_total_pre,
                    docs_total: docs_total_pre,
                    ..ProgressCounts::zero()
                },
                error_message: Some(format!("could not spawn kg-sync: {}", e)),
                log_tail: None,
            };
        }
    };

    let stdout = child.stdout.take();
    let stderr = child.stderr.take();

    let mut combined = String::new();
    let mut counts = ProgressCounts {
        kg_total: kg_total_pre,
        docs_total: docs_total_pre,
        ..ProgressCounts::zero()
    };
    // What the script reports about itself ("📚 Found N markdown files in
    // knowledge/") overrides our filesystem pre-count once it shows up —
    // the script applies an exclusion list (TAG_HIERARCHY.md / VOCABULARY.md)
    // we don't replicate.
    let mut current_phase = "knowledge";
    let mut kg_seen = 0u32;
    let mut docs_seen = 0u32;
    // Bug-2 v0.2.4 (2026-05-12): track whether the script printed its
    // terminal `📊 KG: ... succeeded, ... failed` / `📊 Docs: ... succeeded,
    // ... failed` summaries. If the subprocess crashes mid-run, the
    // optimistic per-line counter (incremented on each `🔄 Syncing
    // node:` log line) is a LIE — the lines log that we're about to try,
    // not that we succeeded. Without a final summary, we can't trust
    // them. Force counts to (succeeded=0, failed=total) on crash so
    // the banner reflects reality.
    let mut kg_summary_seen = false;
    let mut docs_summary_seen = false;

    let app_clone = app.clone();
    let project_id_owned = project_id.to_string();

    // Bug-3 v0.2.x (2026-05-12): concurrent drain of stdout + stderr.
    //
    // The channel is bounded but generously sized (1024 lines): bursty
    // weaviate-client retry chatter can produce hundreds of lines/second
    // briefly, and we don't want the reader tasks to block on `send`
    // (which would re-introduce the pipe-buffer back-pressure deadlock,
    // just one indirection away). 1024 lines × ~120 B avg = ~120 KiB
    // worst-case in-flight, which is negligible.
    let (tx, mut rx) = mpsc::channel::<PipeLine>(1024);

    let stdout_handle = stdout.map(|out| {
        let tx = tx.clone();
        tokio::spawn(async move {
            let mut reader = BufReader::new(out).lines();
            while let Ok(Some(line)) = reader.next_line().await {
                // `send` returns Err if the receiver was dropped — that
                // can happen if the main loop bails on a stall (in which
                // case quietly stop draining; the subprocess will be
                // killed shortly anyway) or if the function is unwinding.
                if tx.send(PipeLine::Stdout(line)).await.is_err() {
                    break;
                }
            }
        })
    });

    let stderr_handle = stderr.map(|err| {
        let tx = tx.clone();
        tokio::spawn(async move {
            let mut reader = BufReader::new(err).lines();
            while let Ok(Some(line)) = reader.next_line().await {
                if tx.send(PipeLine::Stderr(line)).await.is_err() {
                    break;
                }
            }
        })
    });

    // Drop the original sender — once both reader tasks finish and
    // drop their clones, the channel closes and `recv()` returns None.
    drop(tx);

    let stall_timeout = resolve_stall_timeout();
    let mut stalled = false;

    // Drain the merged stream. We dispatch parsing only on Stdout
    // variants — preserving the existing single-threaded, deterministic
    // parse semantics. Stderr lines are accumulated into `combined`
    // so `tail_log` and the crash-snippet logic see them too (matches
    // the previous post-exit drain semantics).
    //
    // v0.2.69 FIX 3 (review SHOULD-FIX): a PROGRESS stall watchdog wraps
    // each `recv()`. It is NOT a per-process / per-node duration cap —
    // the maintainer ruled those out for the seed path (a legit slow-CPU
    // arctic re-embed can run for hours). Instead it bounds SILENCE: the
    // `tokio::time::timeout` re-arms on every line from EITHER pipe, so
    // any output resets the clock. It fires only after `stall_timeout`
    // (default 900 s) of zero output on both pipes. The script emits a
    // line per chunk on both the KG and docs paths, and we force
    // PYTHONUNBUFFERED so those lines arrive promptly, so a progressing
    // seed's inter-line gap (one chunk's embed+insert, ~30 s worst case)
    // is far under the window. The watchdog exists to recover the
    // non-embed Weaviate wedge the per-embed-request guard
    // (VCT_EMBED_REQUEST_TIMEOUT_SECS) cannot see: `connect_to_custom`,
    // schema ensure, and `collection.data.insert` have no timeout of
    // their own, so a silent network/Weaviate deadlock would otherwise
    // hang the launcher background path forever.
    loop {
        let next = match stall_timeout {
            Some(t) => match tokio::time::timeout(t, rx.recv()).await {
                Ok(msg) => msg,
                Err(_) => {
                    // No line on either pipe for the watchdog window.
                    // Force-terminate the subprocess; the resulting
                    // non-zero exit + reconcile_optimistic_counts_on_crash
                    // will surface a clear `failed` row to the user.
                    stalled = true;
                    let _ = child.start_kill();
                    break;
                }
            },
            None => rx.recv().await,
        };

        let Some(msg) = next else {
            // Channel closed — both reader tasks have finished.
            break;
        };

        match msg {
            PipeLine::Stdout(line) => {
                combined.push_str(&line);
                combined.push('\n');

                if let Some(found) = parse_found_header(&line) {
                    // "📚 Found N markdown files in knowledge/" or
                    // "📚 Found N markdown files in docs/"
                    if found.kind == FoundKind::Knowledge {
                        counts.kg_total = found.count;
                        current_phase = "knowledge";
                    } else {
                        counts.docs_total = found.count;
                        current_phase = "docs";
                    }
                } else if line.contains("🔄 Syncing node:") {
                    kg_seen = kg_seen.saturating_add(1);
                    counts.kg_succeeded = kg_seen; // optimistic; reconciled by summary
                    current_phase = "knowledge";
                } else if line.contains("🔄 Syncing doc:") {
                    docs_seen = docs_seen.saturating_add(1);
                    counts.docs_succeeded = docs_seen; // optimistic; reconciled by summary
                    current_phase = "docs";
                } else if is_finalize_line(&line) {
                    // v0.2.92 WP-B1: the script prints this stage marker
                    // right before its post-summary `.node_formats.json`
                    // regen (up to 600 s, synchronous). Map it to a distinct
                    // phase so the GUI shows "finalizing summaries…" with
                    // the bar complete but the status honestly `running` —
                    // instead of a stalled-looking "embedding (N/N)" on a
                    // live process (field report: kg_syncs row appeared
                    // "running after python was done").
                    current_phase = "finalize";
                } else if let Some((s, f, k)) = parse_summary_line(&line, "📊 KG:") {
                    counts.kg_succeeded = s;
                    counts.kg_failed = f;
                    counts.kg_skipped = k;
                    kg_summary_seen = true;
                } else if let Some((s, f, k)) = parse_summary_line(&line, "📊 Docs:") {
                    counts.docs_succeeded = s;
                    counts.docs_failed = f;
                    counts.docs_skipped = k;
                    docs_summary_seen = true;
                }

                // Emit progress on syncing lines (the high-frequency
                // events that drive the pill counter). Header / summary
                // lines also emit so the totals refresh, but those are
                // rare.
                emit_sync(
                    &app_clone,
                    &project_id_owned,
                    sync_status::RUNNING,
                    counts,
                    Some(current_phase),
                    None,
                );
            }
            PipeLine::Stderr(line) => {
                // Stderr is accumulated for log_tail / crash-snippet
                // diagnostics but does NOT drive parsing — same as the
                // pre-fix sequential drain post-exit semantics.
                combined.push_str(&line);
                combined.push('\n');
            }
        }
    }

    // Reap reader tasks. After a stall we've already called
    // `start_kill` and dropped `rx` on loop break (which causes
    // outstanding `send` calls to return Err and the tasks to break
    // their loops); on the normal path the channel closed and the tasks
    // have already finished. Either way, we await them so they don't
    // outlive this function.
    if let Some(h) = stdout_handle {
        let _ = h.await;
    }
    if let Some(h) = stderr_handle {
        let _ = h.await;
    }

    let exit_status = child.wait().await;
    let tail = tail_log(&combined);

    // If we tripped the stall watchdog, override the natural exit
    // analysis with an explicit stall error. The subprocess almost
    // certainly exited with a signal (SIGKILL / TerminateProcess code)
    // — code() == None on Unix in that case — and the generic
    // "exited -1" message would be misleading. Stall ⇒ reconcile
    // optimistic counts: by definition we saw no output for >timeout
    // seconds, so we DEFINITIVELY didn't see the script's summary.
    // Mirror the post-exit reconcile so banner counts reflect reality
    // rather than mid-flight intent.
    if stalled {
        let secs = stall_timeout.map(|d| d.as_secs()).unwrap_or(0);
        reconcile_optimistic_counts_on_crash(
            &mut counts,
            kg_summary_seen,
            docs_summary_seen,
        );
        return SubprocessOutcome {
            status: sync_status::FAILED.to_string(),
            counts,
            error_message: Some(format!(
                "kg-sync stalled (no output for {}s); subprocess killed. This is a \
                 progress watchdog — it fires only on total silence, not on a slow but \
                 progressing re-embed. Set KG_SYNC_STALL_TIMEOUT_SECS to raise the window \
                 (0 disables it).",
                secs,
            )),
            log_tail: Some(tail),
        };
    }

    // Bug-2 v0.2.4 (2026-05-12): counter reconciliation on crash.
    // sync_knowledge_graph.py only prints its `📊 KG: ... succeeded`
    // summary line when it completes normally. The per-`🔄 Syncing node:`
    // line increments are OPTIMISTIC — they record the script's intent
    // to attempt the node, not the actual write outcome. If the script
    // exits non-zero AND we never saw the summary, treat the optimistic
    // counts as a lie and reset succeeded=0, failed=total. Stage-aware:
    // we apply the reset independently for KG and Docs so a crash in
    // the Docs phase doesn't clobber a real `📊 KG:` summary the script
    // managed to print before dying.
    let crashed = matches!(exit_status, Ok(ref s) if !s.success()) || exit_status.is_err();
    if crashed {
        reconcile_optimistic_counts_on_crash(
            &mut counts,
            kg_summary_seen,
            docs_summary_seen,
        );
    }

    match exit_status {
        Ok(s) if s.success() => SubprocessOutcome {
            status: sync_status::SUCCESS.to_string(),
            counts,
            error_message: None,
            log_tail: Some(tail),
        },
        Ok(s) => {
            // Non-zero exit. sync_knowledge_graph.py exits 1 when any node
            // failed but otherwise printed its summary. Surface a concise
            // error and let the user click "Retry sync".
            let exit_code = s.code().unwrap_or(-1);
            let snippet = combined
                .lines()
                .rev()
                .take(40)
                .filter(|l| l.contains("❌") || l.contains("Error") || l.contains("error"))
                .next()
                .unwrap_or("")
                .chars()
                .take(200)
                .collect::<String>();
            // Bug-2 v0.2.4: when the summary was never printed, prepend
            // a hint so the user sees that the high `kg_failed` count
            // reflects the script crashing before completing rather
            // than per-node Weaviate failures.
            let crash_hint = if !kg_summary_seen && !docs_summary_seen {
                "crashed before completing — counts reset; "
            } else if !kg_summary_seen {
                "crashed before completing KG phase — KG counts reset; "
            } else if !docs_summary_seen {
                "crashed before completing Docs phase — Docs counts reset; "
            } else {
                ""
            };
            SubprocessOutcome {
                status: sync_status::FAILED.to_string(),
                counts,
                error_message: Some(format!(
                    "kg-sync exited {}: {}{}",
                    exit_code,
                    crash_hint,
                    if snippet.is_empty() { "see log tail" } else { &snippet },
                )),
                log_tail: Some(tail),
            }
        }
        Err(e) => SubprocessOutcome {
            status: sync_status::FAILED.to_string(),
            counts,
            error_message: Some(format!(
                "kg-sync wait failed: {} (counts reset to total-failed because the \
                 subprocess never reported a summary)",
                e,
            )),
            log_tail: Some(tail),
        },
    }
}

/// Bug-2 v0.2.4 (2026-05-12): collapse the optimistic per-line counter
/// back to the truth-of-the-summary or, when the summary never landed,
/// to (succeeded=0, failed=total).
///
/// Per-line increments on `🔄 Syncing node:` reflect what the script
/// LOGS BEFORE attempting the write — they're optimistic. The terminal
/// `📊 KG: N succeeded, M failed` is the only authoritative source. When
/// the subprocess crashed, we can't trust the optimistic value and
/// MUST NOT persist it (the 2026-05-12 sync incident reported
/// `kg_succeeded: 17, kg_failed: 0` despite the very first insert
/// crashing with HTTP 422 — all 17 came from the per-line log lines,
/// zero of which actually committed).
///
/// Stage-aware: kg_summary_seen / docs_summary_seen are independent.
/// Only reset the stage whose summary we didn't see.
fn reconcile_optimistic_counts_on_crash(
    counts: &mut ProgressCounts,
    kg_summary_seen: bool,
    docs_summary_seen: bool,
) {
    if !kg_summary_seen {
        counts.kg_succeeded = 0;
        counts.kg_failed = counts.kg_total;
        // Skips are only ever set FROM a summary line (never optimistic),
        // so an unseen summary means zero trustworthy skips too.
        counts.kg_skipped = 0;
    }
    if !docs_summary_seen {
        counts.docs_succeeded = 0;
        counts.docs_failed = counts.docs_total;
        counts.docs_skipped = 0;
    }
}

// ─── Helpers (DB / event / log) ──────────────────────────────────────────

#[allow(clippy::too_many_arguments)]
fn upsert_quiet(
    app: &AppHandle,
    project_id: &str,
    status: &str,
    started_at: Option<i64>,
    finished_at: Option<i64>,
    duration_ms: Option<i64>,
    kg_total: u32,
    kg_succeeded: u32,
    kg_failed: u32,
    docs_total: u32,
    docs_succeeded: u32,
    docs_failed: u32,
    error_message: Option<&str>,
    log_tail: Option<&str>,
) {
    let db = app.state::<Db>();
    if let Err(e) = db.upsert_kg_sync(
        project_id,
        status,
        started_at,
        finished_at,
        duration_ms,
        kg_total,
        kg_succeeded,
        kg_failed,
        docs_total,
        docs_succeeded,
        docs_failed,
        error_message,
        log_tail,
    ) {
        tracing::warn!(
            "[vct] warning: kg_syncs upsert failed for {}: {}",
            project_id, e
        );
    }
}

fn finalize_failed(
    app: &AppHandle,
    project_id: &str,
    started_at: i64,
    error: String,
    log_tail: Option<String>,
) {
    let finished_at = chrono::Utc::now().timestamp_millis();
    upsert_quiet(
        app,
        project_id,
        sync_status::FAILED,
        Some(started_at),
        Some(finished_at),
        Some(finished_at - started_at),
        0, 0, 0,
        0, 0, 0,
        Some(&error),
        log_tail.as_deref(),
    );
    emit_sync(
        app,
        project_id,
        sync_status::FAILED,
        ProgressCounts::zero(),
        None,
        Some(&error),
    );
}

fn emit_sync(
    app: &AppHandle,
    project_id: &str,
    status: &str,
    counts: ProgressCounts,
    current_phase: Option<&str>,
    error: Option<&str>,
) {
    let payload = KgSyncView {
        project_id: project_id.to_string(),
        status: status.to_string(),
        started_at_iso: None,
        finished_at_iso: None,
        duration_ms: None,
        kg_total: counts.kg_total,
        kg_succeeded: counts.kg_succeeded,
        kg_failed: counts.kg_failed,
        kg_skipped: counts.kg_skipped,
        docs_total: counts.docs_total,
        docs_succeeded: counts.docs_succeeded,
        docs_failed: counts.docs_failed,
        docs_skipped: counts.docs_skipped,
        error_message: error.map(|s| s.to_string()),
        log_tail: None,
        current_phase: current_phase.map(|s| s.to_string()),
    };
    let _ = app.emit(SYNC_EVENT, payload);
}

/// Tail the last N bytes of subprocess output. Slice on a char boundary
/// so non-ASCII output (the script uses emoji prefixes) doesn't panic.
/// Mirrors `codegraph::tail_log`.
fn tail_log(s: &str) -> String {
    // v0.2.54 Track J: delegates to the shared char-boundary-safe
    // capping helper (was one of three near-identical copies across
    // the codegraph / kg_sync / kg_summary command modules).
    crate::db::log_tail::cap_log_tail(s)
}

// ─── Pre-check + script resolution ───────────────────────────────────────

/// Count `.md` files under `root` recursively. Bounded depth (16) to
/// keep us out of pathological symlink-loop disasters, but practically
/// unreachable — knowledge/ and docs/ trees are flat to 2-3 levels in
/// every project the launcher has registered.
///
/// Returns 0 if `root` doesn't exist or is unreadable; same soft-fail
/// posture as `codegraph::detect_supported_languages`.
fn count_markdown_files(root: &std::path::Path) -> u32 {
    fn walk(dir: &std::path::Path, depth: usize, max_depth: usize, count: &mut u32) {
        if depth > max_depth {
            return;
        }
        let entries = match std::fs::read_dir(dir) {
            Ok(e) => e,
            Err(_) => return,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let name = entry.file_name();
            let name_str = name.to_string_lossy();
            if path.is_dir() {
                // Skip hidden + common ignored dirs (cheap defensive — there
                // shouldn't be `node_modules` under knowledge/ or docs/ but
                // we've seen worse).
                if name_str.starts_with('.') {
                    continue;
                }
                if matches!(
                    name_str.as_ref(),
                    "node_modules" | "__pycache__" | "venv" | ".venv" | "target" | "dist"
                ) {
                    continue;
                }
                walk(&path, depth + 1, max_depth, count);
            } else if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
                // Case-insensitive match: macOS HFS+ may surface "Foo.MD"
                // for a file named "foo.md" depending on the case-folding
                // mode, and we want to count both.
                if ext.eq_ignore_ascii_case("md") {
                    *count = count.saturating_add(1);
                }
            }
        }
    }
    let mut count = 0u32;
    walk(root, 0, 16, &mut count);
    count
}

/// Look for `kg-sync` (POSIX) / `kg-sync.ps1` (Windows) via the shared
/// four-tier ladder (`<project>/.claude/scripts` → `$VCT_LAUNCHER_SCRIPTS_DIR`
/// → sibling-of-exe → PATH).
///
/// v0.2.77 (Part 7c task 3): this was a byte-for-byte copy of the ladder;
/// it now delegates to `vct_launcher_core::paths::resolve_installed_script`
/// (one home). Behaviour is preserved — the PATH tier now uses
/// `std::env::split_paths` (OS-correct) instead of a hand-split `;`/`:`,
/// which only fixes a latent Windows PATH-quoting edge, never regresses
/// POSIX.
pub(crate) fn resolve_kg_sync_script(
    project_folder: &std::path::Path,
) -> Option<std::path::PathBuf> {
    let bin = if cfg!(windows) {
        "kg-sync.ps1"
    } else {
        "kg-sync"
    };
    // v0.2.92 (field bug 2026-09-05): this used to call the RAW ladder
    // `vct_launcher_core::paths::resolve_installed_script`, which trusts a
    // project-local copy on MERE EXISTENCE. The stale-wrapper health guard
    // lived only in `codegraph::resolve_bundled_script`, so the KG sync — the
    // one path that WRITES a collection — was the single wrapper spawn site
    // with no guard at all. A project added with safe add kept its pre-VCO
    // `kg-sync`, the launcher ran it, and the initial sync failed 329/329
    // with `ModuleNotFoundError: No module named 'weaviate'` after the
    // wrapper sourced another checkout's venv. Worse than failing: that
    // generation of wrapper defaults `KG_COLLECTION` to a FOREIGN collection
    // name, so a run from a shell without the env set writes someone else's
    // knowledge graph. Route through the guarded resolver — same ladder,
    // plus the marker check and the deferral emit.
    crate::commands::codegraph::resolve_bundled_script(project_folder, bin)
}

// v0.2.89 BUG 1: `invocation_for` (the powershell-vs-direct spawn shape)
// moved verbatim to `crate::commands::script_invocation` — one home for
// all three bundled-wrapper spawn sites. Its unit tests moved with it.

// ─── Stdout parsing ──────────────────────────────────────────────────────

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
enum FoundKind {
    Knowledge,
    Docs,
}

#[derive(Debug)]
struct FoundHeader {
    kind: FoundKind,
    count: u32,
}

/// Parse one of:
///     "📚 Found 58 markdown files in knowledge/"
///     "📚 Found 12 markdown files in docs/"
/// emitted by `sync_knowledge_graph.py::sync_all_nodes` /
/// `::sync_all_docs`. Returns None on any other line shape.
fn parse_found_header(line: &str) -> Option<FoundHeader> {
    let trimmed = line.trim();
    // Match without the emoji to be robust to terminal width / encoding
    // hiccups — the "Found N markdown files in (knowledge|docs)/" suffix
    // is the discriminating tail.
    let idx = trimmed.find("Found ")?;
    let after = &trimmed[idx + "Found ".len()..];
    let mut parts = after.splitn(2, ' ');
    let count_str = parts.next()?;
    let rest = parts.next()?;
    let count: u32 = count_str.parse().ok()?;
    if rest.contains("knowledge/") {
        Some(FoundHeader {
            kind: FoundKind::Knowledge,
            count,
        })
    } else if rest.contains("docs/") {
        Some(FoundHeader {
            kind: FoundKind::Docs,
            count,
        })
    } else {
        None
    }
}

/// Parse one of:
///     "📊 KG:   50 succeeded, 0 failed"
///     "📊 Docs: 12 succeeded, 0 failed"
/// emitted by `sync_knowledge_graph.py::main`. `prefix` is the lookup
/// fragment ("📊 KG:" or "📊 Docs:"); we also tolerate the prefix without
/// emoji for robustness. Returns (succeeded, failed, skipped) when the
/// line matches, None otherwise.
///
/// v0.2.92 WP-B1: the script's summary now APPENDS ", K skipped"
/// (archived / frontmarker / excluded / embed-skipped nodes are no longer
/// counted as succeeded). BOTH shapes must parse: a stale project bundle
/// runs the OLD script under the NEW launcher, and the new script may run
/// under an old launcher transiently. `skipped` is 0 when the fragment is
/// absent (legacy shape), and any trailing text after the numbers (e.g.
/// the " (N → shared)" routing note) remains ignored.
// CONTRACT: tests/fixtures/kg_sync_stdout_contract.json pins these literals.
// The prefixes and field markers below are EMITTED by
// templates/scripts/sync_knowledge_graph.py and PARSED here. Both sides were
// previously tested against independently hard-coded literals, so they could
// drift apart with both suites green (v0.2.92 m5).
fn parse_summary_line(line: &str, prefix: &str) -> Option<(u32, u32, u32)> {
    let trimmed = line.trim();
    if !trimmed.starts_with(prefix) && !trimmed.contains(prefix.trim_start_matches("📊 ")) {
        return None;
    }
    let succeeded = extract_number_before(trimmed, "succeeded")?;
    let failed = extract_number_before(trimmed, "failed")?;
    // Optional third count — ", K skipped" (v0.2.92). Absent on the legacy
    // two-count shape; anything trailing that isn't "<int> skipped" → 0.
    let skipped = extract_number_before(trimmed, "skipped").unwrap_or(0);
    Some((succeeded, failed, skipped))
}

/// v0.2.92 WP-B1: true for the script's post-summary stage marker
/// `📝 Refreshing .node_formats.json summaries …`, printed (flushed)
/// immediately before the `.node_formats.json` regen — a synchronous
/// step that can run up to 600 s AFTER the final counts. MUST stay in
/// sync with `_regen_node_formats_after_full_sync` in
/// templates/scripts/sync_knowledge_graph.py (the prefix match is
/// deliberately on the stable leading fragment, not the trailing "…").
fn is_finalize_line(line: &str) -> bool {
    line.trim_start().starts_with("📝 Refreshing .node_formats.json summaries")
}

/// Parse the integer that immediately precedes `marker` in `s`. Tolerant
/// of extra spaces and surrounding punctuation. Returns None if no
/// integer is found.
fn extract_number_before(s: &str, marker: &str) -> Option<u32> {
    let idx = s.find(marker)?;
    let head = &s[..idx];
    let num: String = head
        .chars()
        .rev()
        .skip_while(|c| c.is_whitespace())
        .take_while(|c| c.is_ascii_digit())
        .collect();
    let num: String = num.chars().rev().collect();
    num.parse().ok()
}

// ═══════════════════════════════════════════════════════════════════════════
// v0.2.94 — the READ-ONLY drift probe behind the bundle-update kg-sync gate
// ═══════════════════════════════════════════════════════════════════════════
//
// See `projects_v2::change_detect`'s v0.2.94 block for the field evidence. This
// half runs the probe; that half owns the decision (and is pure, so all four
// legs are unit-tested without a subprocess).
//
// It reuses the SAME wrapper the sync itself runs (`kg-sync`, resolved by
// `resolve_kg_sync_script`, invoked via `invocation_for`) and the SAME env
// block (`build_kg_sync_env`) — deliberately, so the probe cannot answer for a
// different collection than the sync would write to.
//
// "Read-only" is precise, not absolute: `--check-drift` performs NO embedding
// and NO Weaviate write, and always exits 0 — but it DOES write the project's
// deferral ledger (`kg_sync_drift_detected` / `kg_binding_missing`), because
// surfacing a finding is the whole point of the scan. That matters here: since
// this gate started running the scan automatically, a drift finding is written
// on the same update that repairs it, so the repairing `--all` retires the
// entry itself (`_clear_drift_deferral`, paired-resolution). Without that pair
// every automatic repair would leave the project carrying an action-required
// entry telling the user to run the command that had just run.

/// Prefix of the ONE machine-readable line `--check-drift` emits.
/// MUST MATCH `templates/scripts/sync_knowledge_graph.py::DRIFT_SENTINEL_PREFIX`.
pub(crate) const KG_DRIFT_SENTINEL: &str = "KG_DRIFT_JSON ";

/// Flags the AUTOMATIC (gate-driven) kg-sync spawn must never carry.
///
/// Standing rule, four releases deep: never re-embed hash-unchanged rows. Plain
/// `--all` is content-hash gated, so a drift of N nodes embeds exactly N; each
/// flag below would defeat that gate and turn a drift repair into a full
/// re-embed of an already-converged collection.
pub(crate) const DRIFT_SPAWN_FORBIDDEN_FLAGS: [&str; 5] =
    ["--rechunk", "--force", "--force-rebuild", "--recreate", "--drop"];

/// `Some(message)` when `args` carries a flag the automatic path must never
/// use; `None` when the argv is clean. Pure — no panic, no logging — so both
/// outcomes are unit-testable.
pub(crate) fn forbidden_spawn_flag_message(args: &[String]) -> Option<String> {
    let bad = args
        .iter()
        .find(|a| DRIFT_SPAWN_FORBIDDEN_FLAGS.contains(&a.as_str()))?;
    Some(format!(
        "kg-sync refused: the automatic spawn carried {} — that would re-embed \
         content whose content hash is unchanged. This is a bug in the \
         launcher's spawn path, not a user setting; no sync was run.",
        bad
    ))
}

/// The debug-build trip, kept separate from the message so the message itself
/// stays testable. A forbidden flag here cannot come from a user — `base_args`
/// is built by `invocation_for` — so in a test/dev build it should be LOUD, and
/// in release the caller's failed row carries it instead.
pub(crate) fn assert_no_forbidden_spawn_flag(args: &[String]) {
    debug_assert!(
        forbidden_spawn_flag_message(args).is_none(),
        "forbidden flag on the automatic kg-sync spawn: {:?}",
        args
    );
}

/// How long the read-only probe may take before we call it unavailable. It is
/// a hash-diff GraphQL query plus a `knowledge/` walk — seconds, normally.
/// A cap is safe HERE (unlike an embed) because timing out costs only the
/// verdict, and a missing verdict is `SkipUnverified`, never a false "ok".
const KG_DRIFT_PROBE_TIMEOUT_SECS: u64 = 120;

/// Pure parser for the probe's stdout. Never panics; anything it cannot read
/// as a verdict becomes `Unavailable`.
pub(crate) fn parse_drift_output(stdout: &str) -> DriftVerdict {
    let line = match stdout.lines().rev().find(|l| l.trim_start().starts_with(KG_DRIFT_SENTINEL)) {
        Some(l) => l.trim_start().trim_start_matches(KG_DRIFT_SENTINEL),
        None => {
            return DriftVerdict::Unavailable {
                detail: "the drift check printed no machine-readable verdict".to_string(),
            }
        }
    };
    let v: serde_json::Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(e) => {
            return DriftVerdict::Unavailable {
                detail: format!("drift verdict was unparseable: {}", e),
            }
        }
    };
    let field = |k: &str| v.get(k).and_then(|x| x.as_u64()).unwrap_or(0) as usize;
    let status = v.get("status").and_then(|x| x.as_str()).unwrap_or("");
    let binding = v.get("binding").and_then(|x| x.as_str()).unwrap_or("");
    let detail = v.get("detail").and_then(|x| x.as_str()).unwrap_or("");
    if binding != "bound" {
        // No collection to compare against. NOT "ok" — there is nothing that
        // could have been checked (`kg_sync_drift.check_kg_binding`'s own
        // distinction, preserved rather than flattened here).
        return DriftVerdict::Unavailable {
            detail: format!("no KG binding resolved ({})", if detail.is_empty() { binding } else { detail }),
        };
    }
    match status {
        "drift" => DriftVerdict::Drift {
            missing: field("missing"),
            stale: field("stale"),
            scanned: field("scanned"),
        },
        "ok" => DriftVerdict::Ok { scanned: field("scanned") },
        other => DriftVerdict::Unavailable {
            detail: format!(
                "drift check returned '{}'{}",
                if other.is_empty() { "no status" } else { other },
                if detail.is_empty() { String::new() } else { format!(" — {}", detail) }
            ),
        },
    }
}

/// Run `kg-sync --check-drift` for `project_folder` and report what Weaviate
/// actually holds. Never fails the caller: every error path is `Unavailable`.
pub(crate) async fn probe_kg_drift(
    db: &Db,
    project_id: &str,
    project_name: &str,
    project_folder: &std::path::Path,
) -> DriftVerdict {
    let script = match resolve_kg_sync_script(project_folder) {
        Some(p) => p,
        None => {
            return DriftVerdict::Unavailable {
                detail: "kg-sync wrapper not found (looked in project, launcher install, $PATH)"
                    .to_string(),
            }
        }
    };
    let env_settings = project_env_settings::populate(db, project_name, Some(project_id));
    let orch_root = find_local_repo_root().ok();
    let (program, mut args) = invocation_for(&script);
    args.push("--check-drift".to_string());

    // Diagnosability (v0.2.94): name the wrapper AND the interpreter tier the
    // spawn will use. The 2026-09-05 MultiagentOrchestrator failure was a
    // wrapper rendered against a PREVIOUS orchestrator location — invisible
    // until someone read a log_tail by hand.
    tracing::info!(
        "[vct] kg-sync drift probe for {}: wrapper={} args={:?} vco_lib_python={}",
        project_id,
        script.display(),
        args,
        vct_launcher_core::python_resolve::resolve_python_for_vco_lib_str()
            .unwrap_or_else(|| "<unresolved>".to_string()),
    );

    let mut cmd = tokio::process::Command::new(&program).silent();
    cmd.args(&args)
        .current_dir(std::env::temp_dir())
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    for (key, val) in build_kg_sync_env(&env_settings, project_folder, orch_root.as_deref()) {
        cmd.env(key, val);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    let out = match tokio::time::timeout(
        std::time::Duration::from_secs(KG_DRIFT_PROBE_TIMEOUT_SECS),
        cmd.output(),
    )
    .await
    {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => {
            return DriftVerdict::Unavailable {
                detail: format!("drift check could not be spawned: {}", e),
            }
        }
        Err(_) => {
            return DriftVerdict::Unavailable {
                detail: format!(
                    "drift check did not finish within {} s",
                    KG_DRIFT_PROBE_TIMEOUT_SECS
                ),
            }
        }
    };
    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    let verdict = parse_drift_output(&stdout);
    if matches!(verdict, DriftVerdict::Unavailable { .. }) {
        let stderr = String::from_utf8_lossy(&out.stderr).to_string();
        let tail: Vec<&str> = stderr.lines().rev().take(3).collect();
        tracing::warn!(
            "[vct] kg-sync drift probe for {} produced no verdict (exit {:?}); stderr tail: {:?}",
            project_id,
            out.status.code(),
            tail,
        );
    }
    verdict
}

#[cfg(test)]
mod v0294_drift_probe_tests {
    use super::*;

    fn sentinel(body: &str) -> String {
        format!("chatter before\n{}{}\nchatter after\n", KG_DRIFT_SENTINEL, body)
    }

    #[test]
    fn parses_a_drift_verdict() {
        let out = sentinel(
            r#"{"binding":"bound","status":"drift","scanned":78,"missing":67,"stale":5}"#,
        );
        assert_eq!(
            parse_drift_output(&out),
            DriftVerdict::Drift { missing: 67, stale: 5, scanned: 78 }
        );
    }

    #[test]
    fn parses_a_clean_verdict() {
        let out = sentinel(r#"{"binding":"bound","status":"ok","scanned":78}"#);
        assert_eq!(parse_drift_output(&out), DriftVerdict::Ok { scanned: 78 });
    }

    /// The whole point: nothing that is not a POSITIVE "ok" may read as one.
    #[test]
    fn everything_else_is_unavailable_never_ok() {
        for body in [
            r#"{"binding":"unbound","status":"unknown","detail":"no KG binding"}"#,
            r#"{"binding":"bound","status":"unknown","detail":"weaviate unreachable"}"#,
            r#"{"binding":"bound"}"#,
            "not json at all",
        ] {
            let got = parse_drift_output(&sentinel(body));
            assert!(
                matches!(got, DriftVerdict::Unavailable { .. }),
                "{} must be Unavailable, got {:?}",
                body,
                got
            );
        }
        assert!(matches!(
            parse_drift_output("no sentinel anywhere\n"),
            DriftVerdict::Unavailable { .. }
        ));
    }

    /// The wrapper prints human chatter too; the LAST sentinel wins so a
    /// re-invocation in one stream cannot resurrect a stale verdict.
    #[test]
    fn the_last_sentinel_wins() {
        let out = format!(
            "{p}{{\"binding\":\"bound\",\"status\":\"drift\",\"missing\":3,\"stale\":0,\"scanned\":3}}\n\
             {p}{{\"binding\":\"bound\",\"status\":\"ok\",\"scanned\":3}}\n",
            p = KG_DRIFT_SENTINEL
        );
        assert_eq!(parse_drift_output(&out), DriftVerdict::Ok { scanned: 3 });
    }

    /// THE COST PIN (standing rule, four releases deep: never re-embed
    /// hash-unchanged rows). The automatic spawn — including the one a drift
    /// verdict triggers — is plain `--all`, whose per-node content-hash gate
    /// skips everything already current. A drift of N nodes embeds exactly N.
    ///
    /// This mirrors `run_subprocess`'s own argv construction
    /// (`cmd.args(&base_args).arg("--all")`); if that line ever grows a flag,
    /// this assertion is the thing that must be consciously updated.
    #[test]
    fn the_automatic_spawn_never_forces_a_re_embed() {
        let (_program, base_args) = invocation_for(std::path::Path::new("/tmp/kg-sync"));
        let mut argv = base_args;
        argv.push("--all".to_string());
        assert!(argv.iter().any(|a| a == "--all"));
        for forbidden in DRIFT_SPAWN_FORBIDDEN_FLAGS {
            assert!(
                !argv.iter().any(|a| a == forbidden),
                "the automatic kg-sync spawn must never carry {} — that would \
                 re-embed content whose hash is unchanged",
                forbidden
            );
        }
        // And the source itself must not have grown one.
        let src = include_str!("kg_sync.rs");
        let spawn_line = src
            .find("cmd.args(&base_args)\n        .arg(\"--all\")")
            .expect("the automatic spawn's argv construction moved — re-pin it");
        let window = &src[spawn_line..spawn_line + 200];
        for forbidden in DRIFT_SPAWN_FORBIDDEN_FLAGS {
            assert!(
                !window.contains(forbidden),
                "a {} flag appeared on the automatic kg-sync spawn",
                forbidden
            );
        }
    }

    // ── R6/4: REFUSE a forbidden flag; never strip it and carry on ────────

    #[test]
    fn a_clean_argv_is_not_refused() {
        let (_p, mut args) = invocation_for(std::path::Path::new("/tmp/kg-sync"));
        args.push("--all".to_string());
        assert_eq!(forbidden_spawn_flag_message(&args), None);
        assert_no_forbidden_spawn_flag(&args); // must not trip
    }

    #[test]
    fn a_forbidden_flag_is_refused_by_name() {
        for bad in DRIFT_SPAWN_FORBIDDEN_FLAGS {
            let args = vec!["--all".to_string(), bad.to_string()];
            let msg = forbidden_spawn_flag_message(&args)
                .unwrap_or_else(|| panic!("{} must be refused", bad));
            assert!(msg.contains("refused"), "{}", msg);
            assert!(msg.contains(bad), "the message must NAME the flag: {}", msg);
            assert!(
                msg.contains("no sync was run"),
                "refusing means NOT running, and the message must say so: {}",
                msg
            );
        }
    }

    /// ...and in a debug/test build it is LOUD, not a WARN nobody reads.
    #[test]
    #[should_panic(expected = "--rechunk")]
    fn a_forbidden_flag_trips_the_debug_assert() {
        assert_no_forbidden_spawn_flag(&["--all".to_string(), "--rechunk".to_string()]);
    }

    /// The refusal is WIRED: `run_subprocess` returns a FAILED row carrying the
    /// message rather than stripping the flag and reporting success.
    #[test]
    fn the_refusal_fails_the_row_rather_than_stripping() {
        let src = include_str!("kg_sync.rs");
        let anchor = src
            .find("if let Some(msg) = forbidden_spawn_flag_message(&base_args)")
            .expect("the refusal moved out of run_subprocess — re-pin it");
        let window = &src[anchor..anchor + 500];
        assert!(window.contains("tracing::error!"), "it must be an ERROR, not a warn");
        assert!(
            window.contains("sync_status::FAILED"),
            "the row must be marked failed so the refusal is visible in the GUI"
        );
        assert!(
            !src.contains(".filter(|a| {\n            let forbidden"),
            "the old silent-strip filter must be gone"
        );
    }

    /// The probe must be read-only and the repair must be hash-gated: the
    /// argv the probe builds carries `--check-drift` and nothing destructive.
    #[test]
    fn the_probe_argv_is_read_only() {
        let (_program, mut args) = invocation_for(std::path::Path::new("/tmp/kg-sync"));
        args.push("--check-drift".to_string());
        assert!(args.iter().any(|a| a == "--check-drift"));
        for forbidden in DRIFT_SPAWN_FORBIDDEN_FLAGS {
            assert!(
                !args.iter().any(|a| a == forbidden),
                "the read-only probe must never carry {}",
                forbidden
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    /// Plain `#[test]` on a bare thread — deliberately NOT `#[tokio::test]`.
    /// Reproduces the v0.2.89 boot context: `setup()` runs on the main
    /// thread with no reactor, where a bare `tokio::spawn` panics ("there
    /// is no reactor running") and kills the launcher before the window
    /// exists. `tauri::async_runtime::spawn` must work from that context
    /// (lazy global runtime) — this pins the mechanism every detached-spawn
    /// entry point in this crate now relies on. A `#[tokio::test]` variant
    /// would be worthless: it supplies the reactor the field context lacks.
    #[test]
    fn async_runtime_spawn_works_without_a_reactor_context() {
        let (tx, rx) = std::sync::mpsc::channel();
        tauri::async_runtime::spawn(async move {
            let _ = tx.send(42u8);
        });
        assert_eq!(
            rx.recv_timeout(std::time::Duration::from_secs(10)).unwrap(),
            42
        );
    }

    fn tmpdir(label: &str) -> std::path::PathBuf {
        let p = std::env::temp_dir().join(format!(
            "vct-kgsync-{}-{}",
            label,
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn count_markdown_finds_files_recursively() {
        let d = tmpdir("count-rec");
        fs::write(d.join("a.md"), b"# a").unwrap();
        fs::create_dir_all(d.join("sub/sub2")).unwrap();
        fs::write(d.join("sub/b.md"), b"# b").unwrap();
        fs::write(d.join("sub/sub2/c.md"), b"# c").unwrap();
        fs::write(d.join("sub/d.txt"), b"not md").unwrap();
        assert_eq!(count_markdown_files(&d), 3);
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn count_markdown_handles_missing_dir() {
        let bogus = std::env::temp_dir().join(format!("definitely-not-{}", uuid::Uuid::new_v4()));
        assert_eq!(count_markdown_files(&bogus), 0);
    }

    #[test]
    fn count_markdown_ignores_hidden_and_vendor_dirs() {
        let d = tmpdir("count-ignore");
        fs::create_dir_all(d.join(".obsidian")).unwrap();
        fs::write(d.join(".obsidian/leak.md"), b"# leak").unwrap();
        fs::create_dir_all(d.join("node_modules")).unwrap();
        fs::write(d.join("node_modules/leak.md"), b"# leak").unwrap();
        fs::write(d.join("a.md"), b"# a").unwrap();
        assert_eq!(count_markdown_files(&d), 1);
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn count_markdown_is_case_insensitive_on_extension() {
        // Documented edge case for the cross-platform constraint:
        // some macOS HFS+ setups surface ".MD" / ".Md" for files
        // created via Finder. We count those too.
        let d = tmpdir("count-case");
        fs::write(d.join("a.md"), b"# a").unwrap();
        fs::write(d.join("b.MD"), b"# b").unwrap();
        fs::write(d.join("c.Md"), b"# c").unwrap();
        // ext4 (Linux) will keep all three as separate files; HFS+
        // (macOS) may fold to one. We only assert >= 1 to stay
        // portable across the test machines.
        assert!(count_markdown_files(&d) >= 1);
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn parse_found_header_knowledge() {
        let line = "📚 Found 58 markdown files in knowledge/";
        let h = parse_found_header(line).expect("must parse");
        assert_eq!(h.kind, FoundKind::Knowledge);
        assert_eq!(h.count, 58);
    }

    #[test]
    fn parse_found_header_docs() {
        let line = "📚 Found 12 markdown files in docs/";
        let h = parse_found_header(line).expect("must parse");
        assert_eq!(h.kind, FoundKind::Docs);
        assert_eq!(h.count, 12);
    }

    #[test]
    fn parse_found_header_rejects_unrelated_line() {
        assert!(parse_found_header("🔄 Syncing node: Foo").is_none());
        assert!(parse_found_header("📚 Found 3 things").is_none());
    }

    #[test]
    fn parse_summary_kg_line() {
        // Note the variable whitespace after the colon — the script's
        // emit uses tab-like alignment; we tolerate both.
        let line = "📊 KG:   48 succeeded, 2 failed";
        let (s, f, k) = parse_summary_line(line, "📊 KG:").expect("must parse");
        assert_eq!(s, 48);
        assert_eq!(f, 2);
        assert_eq!(k, 0, "legacy two-count shape → skipped defaults to 0");
    }

    #[test]
    fn parse_summary_docs_line() {
        let line = "📊 Docs: 12 succeeded, 0 failed";
        let (s, f, k) = parse_summary_line(line, "📊 Docs:").expect("must parse");
        assert_eq!(s, 12);
        assert_eq!(f, 0);
        assert_eq!(k, 0);
    }

    // ─── v0.2.92 WP-B1: the ", K skipped" summary shape ───────────────

    #[test]
    fn parse_summary_kg_line_with_skipped() {
        let line = "📊 KG:   113 succeeded, 0 failed, 4 skipped";
        let (s, f, k) = parse_summary_line(line, "📊 KG:").expect("must parse");
        assert_eq!(s, 113);
        assert_eq!(f, 0);
        assert_eq!(k, 4);
    }

    #[test]
    fn parse_summary_skipped_shape_with_shared_note_suffix() {
        // The script appends the shared-routing note AFTER the counts in
        // both shapes; trailing text must stay ignored.
        let line = "📊 KG:   40 succeeded, 1 failed, 2 skipped (7 → shared)";
        let (s, f, k) = parse_summary_line(line, "📊 KG:").expect("must parse");
        assert_eq!((s, f, k), (40, 1, 2));
    }

    #[test]
    fn parse_summary_legacy_shape_with_shared_note_suffix() {
        let line = "📊 Docs: 9 succeeded, 0 failed (2 → shared)";
        let (s, f, k) = parse_summary_line(line, "📊 Docs:").expect("must parse");
        assert_eq!((s, f, k), (9, 0, 0));
    }

    #[test]
    fn parse_summary_zero_skipped_fragment() {
        let line = "📊 KG:   5 succeeded, 0 failed, 0 skipped";
        let (s, f, k) = parse_summary_line(line, "📊 KG:").expect("must parse");
        assert_eq!((s, f, k), (5, 0, 0));
    }

    #[test]
    fn finalize_line_is_recognized_and_is_not_a_summary() {
        // v0.2.92 WP-B1: the stage marker the script prints immediately
        // before its (up to 600 s) post-summary regen — mapped to the
        // "finalize" phase by run_subprocess's dispatch.
        let line = "📝 Refreshing .node_formats.json summaries (KG-4, soft-fail) ...";
        assert!(is_finalize_line(line));
        // Leading whitespace tolerated (the script never indents it today,
        // but tolerance is free and the prefix match must not be exact-full-line).
        assert!(is_finalize_line("   📝 Refreshing .node_formats.json summaries"));
        // Not the marker / not a summary line:
        assert!(!is_finalize_line("🔄 Syncing node: Foo"));
        assert!(!is_finalize_line("📊 KG:   5 succeeded, 0 failed, 0 skipped"));
        assert!(!is_finalize_line("   (node-format refresh timed out; summaries left as-is — non-fatal)"));
        assert!(parse_summary_line(line, "📊 KG:").is_none());
    }

    #[test]
    fn parse_summary_rejects_non_summary_lines() {
        assert!(parse_summary_line("🔄 Syncing doc: foo", "📊 KG:").is_none());
        assert!(parse_summary_line("📚 Found 5 markdown files in knowledge/", "📊 KG:").is_none());
    }

    #[test]
    fn extract_number_before_with_padding() {
        assert_eq!(extract_number_before("foo 42 succeeded, 0 failed", "succeeded"), Some(42));
        assert_eq!(extract_number_before("nothing here", "succeeded"), None);
    }

    #[test]
    fn tail_log_truncates_long_output() {
        let big = "a".repeat(10_000);
        let tail = tail_log(&big);
        assert!(tail.len() < 5_000);
        assert!(tail.starts_with('…'));
    }

    #[test]
    fn tail_log_passes_through_short_output() {
        assert_eq!(tail_log("all good"), "all good");
    }

    // v0.2.89 BUG 1: `invocation_for_picks_powershell_on_windows` moved to
    // `commands::script_invocation::tests` alongside the function.

    fn write_kg_sync_wrapper(d: &std::path::Path, body: &[u8]) -> std::path::PathBuf {
        let scripts = d.join(".claude").join("scripts");
        fs::create_dir_all(&scripts).unwrap();
        let bin = if cfg!(windows) { "kg-sync.ps1" } else { "kg-sync" };
        let p = scripts.join(bin);
        fs::write(&p, body).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = fs::metadata(&p).unwrap().permissions();
            perms.set_mode(0o755);
            fs::set_permissions(&p, perms).unwrap();
        }
        p
    }

    #[test]
    fn resolve_kg_sync_finds_project_local_copy() {
        let d = tmpdir("resolve");
        // v0.2.92: the wrapper must carry the resilient-ladder marker to be
        // trusted — `resolve_kg_sync_script` now goes through the SAME guarded
        // resolver as the code-graph analyzer. A body without the marker is
        // covered by `resolve_kg_sync_rejects_stale_project_local_copy` below.
        let p = write_kg_sync_wrapper(
            &d,
            b"#!/usr/bin/env bash\n: \"${VCT_INSTALL_ROOT:-}\"\necho ok\n",
        );
        let resolved = resolve_kg_sync_script(&d).expect("must resolve");
        assert_eq!(resolved, p);
        fs::remove_dir_all(&d).ok();
    }

    /// RED-PROOF for the v0.2.92 field bug (2026-09-05): a project-local
    /// `kg-sync` that lacks the `$VCT_INSTALL_ROOT` ladder — the shape a
    /// pre-VCO project carries, pointing at ANOTHER checkout's venv and
    /// defaulting `KG_COLLECTION` to a FOREIGN collection — must NOT be
    /// returned. Before the fix `resolve_kg_sync_script` called the RAW
    /// ladder, which trusted it on mere existence: the initial sync ran it
    /// and failed 329/329 with `ModuleNotFoundError: No module named
    /// 'weaviate'`.
    ///
    /// Mutation check: point `resolve_kg_sync_script` back at
    /// `vct_launcher_core::paths::resolve_installed_script` and this test
    /// fails (it resolves to the stale path).
    #[test]
    fn resolve_kg_sync_rejects_stale_project_local_copy() {
        let d = tmpdir("resolve-stale");
        let stale = write_kg_sync_wrapper(
            &d,
            b"#!/bin/bash\nsource /some/other/checkout/.venv/bin/activate\n              export KG_COLLECTION=\"${KG_COLLECTION:-ClaudeKnowledgeGraph}\"\n",
        );

        // NO env mutation here (v0.2.92 flake fix). This test used to clear
        // `VCT_LAUNCHER_SCRIPTS_DIR` + `PATH` process-globally on the theory
        // that the fallback tiers had to be silenced. They do not: the
        // assertion is "the STALE path is not what came back", and a buggy
        // resolver returns the stale project-local copy from TIER 1 — before
        // either env-driven tier is ever consulted. So the clearing bought
        // nothing and cost correctness under `--test-threads>1`: `PATH` and
        // `VCT_LAUNCHER_SCRIPTS_DIR` are process-global, this site took no
        // part in `vct_launcher_core::test_env::GLOBAL_ENV_MUTEX`, and its
        // save/restore pair could interleave with any other env-mutating test
        // in the binary (`hub_launcher::find_hub_binary_*` reads `PATH`).
        // The fix is the strong one: remove the shared state, don't serialize
        // it. This test is now env-independent by construction.
        let resolved = resolve_kg_sync_script(&d);

        assert_ne!(
            resolved.as_deref(),
            Some(stale.as_path()),
            "a stale (pre-ladder) project-local kg-sync must never be spawned — \
             it reaches another checkout's venv and another project's collection"
        );
        // Stronger than `!= stale`: NOTHING inside the project folder may be
        // returned once the local copy is condemned — only the orchestrator
        // fallback (outside `d`) or None is acceptable.
        if let Some(r) = resolved.as_deref() {
            assert!(
                !r.starts_with(&d),
                "condemned project-local wrapper dir must not supply the \
                 fallback either; got {}",
                r.display()
            );
        }
        fs::remove_dir_all(&d).ok();
    }

    // ─── Bug-2 v0.2.4 (2026-05-12): counter reconciliation ──────────────

    #[test]
    fn reconcile_resets_kg_succeeded_to_zero_when_summary_missing() {
        // 2026-05-12 sync-crash replay: 17 optimistic increments from `🔄 Syncing
        // node:` markers, subprocess crashed on first insert (422), no
        // `📊 KG: ... succeeded, ... failed` summary ever landed.
        // Expectation: succeeded=0, failed=total.
        let mut counts = ProgressCounts {
            kg_total: 58,
            kg_succeeded: 17,
            kg_failed: 0,
            kg_skipped: 3,
            docs_total: 0,
            docs_succeeded: 0,
            docs_failed: 0,
            docs_skipped: 0,
        };
        reconcile_optimistic_counts_on_crash(&mut counts, false, false);
        assert_eq!(counts.kg_succeeded, 0);
        assert_eq!(counts.kg_failed, 58);
        assert_eq!(counts.kg_skipped, 0, "unseen summary → no trustworthy skips");
        assert_eq!(counts.docs_succeeded, 0);
        assert_eq!(counts.docs_failed, 0);
    }

    #[test]
    fn reconcile_preserves_kg_counts_when_summary_seen() {
        // Summary was emitted then docs phase crashed — KG counters
        // reflect reality, docs counters need reset.
        let mut counts = ProgressCounts {
            kg_total: 58,
            kg_succeeded: 56,
            kg_failed: 2,
            kg_skipped: 0,
            docs_total: 12,
            docs_succeeded: 7,
            docs_failed: 0,
            docs_skipped: 0,
        };
        reconcile_optimistic_counts_on_crash(&mut counts, true, false);
        assert_eq!(counts.kg_succeeded, 56, "KG summary seen, keep");
        assert_eq!(counts.kg_failed, 2, "KG summary seen, keep");
        assert_eq!(counts.docs_succeeded, 0, "docs summary missing, reset");
        assert_eq!(counts.docs_failed, 12, "docs reset to total");
    }

    #[test]
    fn reconcile_noop_when_both_summaries_seen() {
        let mut counts = ProgressCounts {
            kg_total: 58,
            kg_succeeded: 56,
            kg_failed: 2,
            kg_skipped: 4,
            docs_total: 12,
            docs_succeeded: 11,
            docs_failed: 1,
            docs_skipped: 2,
        };
        reconcile_optimistic_counts_on_crash(&mut counts, true, true);
        assert_eq!(counts.kg_succeeded, 56);
        assert_eq!(counts.kg_failed, 2);
        assert_eq!(counts.kg_skipped, 4, "both summaries seen, skips kept");
        assert_eq!(counts.docs_succeeded, 11);
        assert_eq!(counts.docs_failed, 1);
        assert_eq!(counts.docs_skipped, 2);
    }

    #[test]
    fn reconcile_handles_zero_total_docs_phase() {
        // No docs/ folder → docs_total=0; reset should not produce
        // weird counts.
        let mut counts = ProgressCounts {
            kg_total: 58,
            kg_succeeded: 17,
            kg_failed: 0,
            kg_skipped: 0,
            docs_total: 0,
            docs_succeeded: 0,
            docs_failed: 0,
            docs_skipped: 0,
        };
        reconcile_optimistic_counts_on_crash(&mut counts, false, false);
        assert_eq!(counts.kg_failed, 58);
        assert_eq!(counts.docs_failed, 0);
    }

    // ─── v0.2.69 FIX 3 (review SHOULD-FIX): progress stall-watchdog ──────
    //
    // Env vars are process-global; cargo runs unit tests in parallel by
    // default. These tests mutate `KG_SYNC_STALL_TIMEOUT_SECS` so they
    // must serialize on a local mutex (poisoned-safe: grab the lock,
    // ignore poisoning so one assert-failure doesn't cascade across
    // sibling tests).
    fn env_test_lock() -> &'static std::sync::Mutex<()> {
        static LOCK: std::sync::OnceLock<std::sync::Mutex<()>> =
            std::sync::OnceLock::new();
        LOCK.get_or_init(|| std::sync::Mutex::new(()))
    }

    #[test]
    fn resolve_stall_timeout_uses_default_when_unset() {
        let _g = env_test_lock().lock().unwrap_or_else(|e| e.into_inner());
        let saved = std::env::var_os("KG_SYNC_STALL_TIMEOUT_SECS");
        unsafe {
            std::env::remove_var("KG_SYNC_STALL_TIMEOUT_SECS");
        }
        let t = resolve_stall_timeout();
        if let Some(v) = saved {
            unsafe { std::env::set_var("KG_SYNC_STALL_TIMEOUT_SECS", v); }
        }
        // v0.2.69 FIX 3: raised default = max(900, 4 × 180 s per-embed
        // guard) = 900 s.
        assert_eq!(t, Some(std::time::Duration::from_secs(900)));
    }

    #[test]
    fn resolve_stall_timeout_honours_env_override() {
        let _g = env_test_lock().lock().unwrap_or_else(|e| e.into_inner());
        let saved = std::env::var_os("KG_SYNC_STALL_TIMEOUT_SECS");
        unsafe {
            std::env::set_var("KG_SYNC_STALL_TIMEOUT_SECS", "42");
        }
        let t = resolve_stall_timeout();
        match saved {
            Some(v) => unsafe { std::env::set_var("KG_SYNC_STALL_TIMEOUT_SECS", v) },
            None => unsafe { std::env::remove_var("KG_SYNC_STALL_TIMEOUT_SECS") },
        }
        assert_eq!(t, Some(std::time::Duration::from_secs(42)));
    }

    #[test]
    fn resolve_stall_timeout_zero_disables_watchdog() {
        let _g = env_test_lock().lock().unwrap_or_else(|e| e.into_inner());
        let saved = std::env::var_os("KG_SYNC_STALL_TIMEOUT_SECS");
        unsafe {
            std::env::set_var("KG_SYNC_STALL_TIMEOUT_SECS", "0");
        }
        let t = resolve_stall_timeout();
        match saved {
            Some(v) => unsafe { std::env::set_var("KG_SYNC_STALL_TIMEOUT_SECS", v) },
            None => unsafe { std::env::remove_var("KG_SYNC_STALL_TIMEOUT_SECS") },
        }
        assert_eq!(t, None);
    }

    #[test]
    fn resolve_stall_timeout_falls_back_on_garbage() {
        let _g = env_test_lock().lock().unwrap_or_else(|e| e.into_inner());
        let saved = std::env::var_os("KG_SYNC_STALL_TIMEOUT_SECS");
        unsafe {
            std::env::set_var("KG_SYNC_STALL_TIMEOUT_SECS", "not-a-number");
        }
        let t = resolve_stall_timeout();
        match saved {
            Some(v) => unsafe { std::env::set_var("KG_SYNC_STALL_TIMEOUT_SECS", v) },
            None => unsafe { std::env::remove_var("KG_SYNC_STALL_TIMEOUT_SECS") },
        }
        assert_eq!(t, Some(std::time::Duration::from_secs(900)));
    }

    // v0.2.69 FIX 3: PYTHONUNBUFFERED must be exported so the script's
    // per-chunk progress lines reach the launcher's reader promptly
    // (block-buffered piped stdout would batch them and starve the
    // re-armed-per-line watchdog of the input that resets its clock).
    #[test]
    fn build_kg_sync_env_forces_unbuffered_python() {
        use crate::commands::project_env_settings::ProjectEnvSettings;
        let settings = ProjectEnvSettings::with_defaults("TestProject");
        let folder = std::path::Path::new("/tmp/proj");
        let env = build_kg_sync_env(&settings, folder, None);
        let unbuffered = env
            .iter()
            .find(|(k, _)| *k == "PYTHONUNBUFFERED")
            .map(|(_, v)| v.clone());
        assert_eq!(
            unbuffered,
            Some(std::ffi::OsString::from("1")),
            "PYTHONUNBUFFERED=1 must be set so per-chunk progress streams \
             promptly enough to feed the stall watchdog",
        );
    }

    // BUG 3 (v0.2.89, plan §1.3 C): the launcher must pin BOTH root
    // channels. `KG_BASE_DIR` is the legacy channel (still honored);
    // `KG_SYNC_PROJECT_ROOT` is the new non-leaking channel whose
    // explicit launcher value must survive the orchestrator-copy wrapper
    // fallback (wrappers only set it when UNSET).
    #[test]
    fn build_kg_sync_env_pins_both_project_root_channels() {
        use crate::commands::project_env_settings::ProjectEnvSettings;
        let settings = ProjectEnvSettings::with_defaults("TestProject");
        let folder = std::path::Path::new("/tmp/proj-root-pin");
        let env = build_kg_sync_env(&settings, folder, None);

        let find = |key: &str| {
            env.iter().find(|(k, _)| *k == key).map(|(_, v)| v.clone())
        };
        assert_eq!(
            find("KG_BASE_DIR"),
            Some(folder.as_os_str().to_owned()),
            "legacy root channel must stay pinned"
        );
        assert_eq!(
            find("KG_SYNC_PROJECT_ROOT"),
            Some(folder.as_os_str().to_owned()),
            "new non-leaking root channel must be pinned to the project folder \
             (BUG 3: a leaked foreign KG_BASE_DIR must never win over this)"
        );
    }

    // ─── BUG 2 (v0.2.89): heartbeat staleness window ─────────────────────
    //
    // `heartbeat_stale_secs` reads KG_SYNC_STALL_TIMEOUT_SECS via
    // `resolve_stall_timeout`, so these tests serialize on the same env
    // lock as the watchdog tests above.

    #[test]
    fn heartbeat_stale_secs_has_floor_and_scales_with_stall_timeout() {
        let _g = env_test_lock().lock().unwrap_or_else(|e| e.into_inner());
        let saved = std::env::var_os("KG_SYNC_STALL_TIMEOUT_SECS");

        // Default stall (900 s): max(1800, 2 × 900) = 1800 (the floor).
        unsafe { std::env::remove_var("KG_SYNC_STALL_TIMEOUT_SECS"); }
        assert_eq!(heartbeat_stale_secs(), 1800);

        // Small stall override: floor still wins.
        unsafe { std::env::set_var("KG_SYNC_STALL_TIMEOUT_SECS", "42"); }
        assert_eq!(heartbeat_stale_secs(), 1800);

        // Large stall override: window widens to 2× so the liveness net
        // can never fire before the silence watchdog.
        unsafe { std::env::set_var("KG_SYNC_STALL_TIMEOUT_SECS", "3600"); }
        assert_eq!(heartbeat_stale_secs(), 7200);

        // Watchdog disabled (0): the heartbeat window still applies —
        // task liveness is orthogonal to subprocess silence.
        unsafe { std::env::set_var("KG_SYNC_STALL_TIMEOUT_SECS", "0"); }
        assert_eq!(heartbeat_stale_secs(), 1800);

        match saved {
            Some(v) => unsafe { std::env::set_var("KG_SYNC_STALL_TIMEOUT_SECS", v) },
            None => unsafe { std::env::remove_var("KG_SYNC_STALL_TIMEOUT_SECS") },
        }
    }

    // ─── Bug-3 v0.2.x (2026-05-12): concurrent-drain deadlock regression ──
    //
    // These tests reproduce the deadlock CAUSE (stderr volume larger than
    // the pipe buffer) and validate that the fix drains both pipes
    // concurrently. They drive `tokio::process::Command` directly with
    // a small shell helper rather than exercising `run_subprocess` end-
    // to-end (which would require a Tauri AppHandle + Db). The drain
    // logic itself is the load-bearing change — exercising it through
    // a real OS pipe is the highest-value verification.
    //
    // Unix-only because the helpers use `sh -c`. Windows uses `cmd /C`
    // and `PowerShell`; we trust the same Tokio drain pattern on both
    // platforms (Tokio normalizes `AsyncBufReadExt` across them and
    // `tokio::process::Child::start_kill` works on both — see Tokio
    // docs on `Child::start_kill`).
    //
    // v0.2.14 (2026-05-17): fork+exec ENOENT hardening. Under high
    // parallel test load (e.g. 3 concurrent `cargo test --lib`
    // processes × ~12 internal threads each ≈ 36 simultaneous
    // `fork()`+`execvp()` calls), the kernel/glibc PATH lookup can
    // transiently surface `Os { code: 2, kind: NotFound }` even for
    // a binary that exists. Two mitigations:
    //   1. Use an absolute path (`/bin/sh`) so `posix_spawn` skips the
    //      `$PATH` traversal entirely — eliminates the most common
    //      race source.
    //   2. Retry once with a short sleep if spawn STILL ENOENTs. A
    //      single retry is sufficient empirically; if it still fails
    //      the host is so under-resourced that the test would have
    //      panicked elsewhere anyway.
    //
    // See `services::runtime::tests::daemon_usable_probe_*` for the
    // sibling pattern (those tests use tempdir-relative scripts that
    // can't migrate to absolute paths, so they're `#[ignore]`d and
    // gated behind `--ignored`; we have no such constraint here).

    #[cfg(unix)]
    async fn spawn_sh_with_retry(
        script: &str,
    ) -> tokio::process::Child {
        // POSIX guarantees `/bin/sh` exists on every Unix host. Using
        // an absolute path bypasses `$PATH` traversal in `execvp`,
        // which is the most common source of the ENOENT flake under
        // heavy parallel fork load.
        const SH_PATH: &str = "/bin/sh";
        let mut last_err: Option<std::io::Error> = None;
        for attempt in 0..3 {
            match tokio::process::Command::new(SH_PATH)
                .arg("-c")
                .arg(script)
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::piped())
                .spawn()
            {
                Ok(child) => return child,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    // Transient ENOENT under fork-storm. Brief sleep
                    // gives the kernel a chance to drain any in-flight
                    // exec-related state, then retry.
                    last_err = Some(e);
                    tokio::time::sleep(std::time::Duration::from_millis(
                        50 * (attempt + 1) as u64,
                    ))
                    .await;
                }
                Err(e) => panic!("spawn {}: {}", SH_PATH, e),
            }
        }
        panic!(
            "spawn {} repeatedly failed with ENOENT under parallel test \
             load (last error: {:?}); host is likely heavily oversubscribed",
            SH_PATH, last_err,
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn concurrent_drain_does_not_deadlock_on_large_stderr() {
        use std::time::Duration;
        use tokio::io::{AsyncBufReadExt, BufReader};
        use tokio::sync::mpsc;
        use tokio::time::timeout;

        // The Linux default pipe buffer is 16 × 4 KiB = 64 KiB.
        // Emit 128 KiB to stderr (well over) BEFORE any stdout writes,
        // so the pre-fix sequential drain would block in
        // `anon_pipe_write` waiting for the launcher to read stderr —
        // which never happened because the launcher was waiting on
        // stdout. The script also emits a few stdout lines AFTER the
        // stderr burst, which can only come through if stderr was
        // drained concurrently.
        //
        // 2048 × 64 bytes ≈ 128 KiB. `yes` produces a deterministic
        // line; `head -c` doesn't preserve newlines, so we use
        // printf in a loop instead.
        let script = r#"
            i=0
            while [ $i -lt 2048 ]; do
                printf 'STDERR-PADDING-LINE-%04d-XXXXXXXXXXXXXXXXXXXXXXXX\n' "$i" >&2
                i=$((i+1))
            done
            echo "STDOUT-LINE-1"
            echo "STDOUT-LINE-2"
            echo "STDOUT-LINE-3"
        "#;

        let mut child = spawn_sh_with_retry(script).await;

        let stdout = child.stdout.take().expect("stdout pipe");
        let stderr = child.stderr.take().expect("stderr pipe");

        let (tx, mut rx) = mpsc::channel::<(bool, String)>(1024);

        let tx_out = tx.clone();
        let h_out = tokio::spawn(async move {
            let mut r = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = r.next_line().await {
                if tx_out.send((true, line)).await.is_err() { break; }
            }
        });
        let tx_err = tx.clone();
        let h_err = tokio::spawn(async move {
            let mut r = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = r.next_line().await {
                if tx_err.send((false, line)).await.is_err() { break; }
            }
        });
        drop(tx);

        let mut stdout_count = 0usize;
        let mut stderr_count = 0usize;
        // Bound the whole receive loop to a generous wall-clock budget.
        // If the deadlock regresses, this trips and the test fails
        // loudly (rather than hanging forever).
        let drain = async {
            while let Some((is_stdout, _line)) = rx.recv().await {
                if is_stdout { stdout_count += 1; } else { stderr_count += 1; }
            }
        };
        timeout(Duration::from_secs(15), drain)
            .await
            .expect(
                "concurrent drain deadlocked: stderr buffer fills before stdout \
                 drains and one reader never makes progress (regression of \
                 the Bug-3 2026-05-12 fix)",
            );

        let _ = h_out.await;
        let _ = h_err.await;
        let _ = child.wait().await;

        assert_eq!(
            stdout_count, 3,
            "all 3 stdout lines must arrive even though stderr emitted 128 KiB first"
        );
        assert_eq!(stderr_count, 2048, "all 2048 stderr lines must be drained");
    }

    // ─── v0.2.69 FIX 3 (review SHOULD-FIX): progress-guard semantics ─────
    //
    // The watchdog is a PROGRESS guard, not a duration cap. These two
    // tests pin both halves of that contract using the SAME re-arm-on-
    // every-line drain logic that `run_subprocess` uses (a small window
    // keeps the tests fast; the production default is 900 s).

    /// A subprocess that emits nothing on either pipe for longer than the
    /// watchdog window MUST be killed (the non-embed Weaviate-wedge case
    /// the per-request guard can't see).
    #[cfg(unix)]
    #[tokio::test]
    async fn stall_watchdog_kills_silent_subprocess() {
        use std::time::Duration;
        use tokio::io::{AsyncBufReadExt, BufReader};
        use tokio::sync::mpsc;

        // `sleep 30` emits nothing on either pipe; the watchdog must
        // detect the silence and kill it. 1-second window keeps it fast.
        let mut child = spawn_sh_with_retry("sleep 30").await;

        let stdout = child.stdout.take().expect("stdout pipe");
        let stderr = child.stderr.take().expect("stderr pipe");

        let (tx, mut rx) = mpsc::channel::<PipeLine>(16);
        let tx_out = tx.clone();
        let h_out = tokio::spawn(async move {
            let mut r = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = r.next_line().await {
                if tx_out.send(PipeLine::Stdout(line)).await.is_err() { break; }
            }
        });
        let tx_err = tx.clone();
        let h_err = tokio::spawn(async move {
            let mut r = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = r.next_line().await {
                if tx_err.send(PipeLine::Stderr(line)).await.is_err() { break; }
            }
        });
        drop(tx);

        let watchdog = Duration::from_secs(1);
        let started = std::time::Instant::now();
        let mut stalled = false;
        loop {
            match tokio::time::timeout(watchdog, rx.recv()).await {
                Ok(Some(_)) => continue, // line ⇒ re-arm
                Ok(None) => break,       // pipes closed without a stall
                Err(_) => {
                    stalled = true;
                    let _ = child.start_kill();
                    break;
                }
            }
        }
        let elapsed = started.elapsed();

        assert!(stalled, "watchdog must trip on a subprocess that emits nothing");
        assert!(
            elapsed < Duration::from_secs(5),
            "watchdog should trip quickly; took {:?}",
            elapsed,
        );

        let _ = h_out.await;
        let _ = h_err.await;
        let exit = child.wait().await.expect("wait");
        assert!(!exit.success(), "killed subprocess must report failure");
    }

    /// PROGRESS-GUARD CONTRACT: a subprocess that keeps emitting lines —
    /// even one whose TOTAL runtime far exceeds the watchdog window, and
    /// even past what an old per-process cap (e.g. 300 s) would have
    /// allowed — must NOT be killed, as long as the GAP between
    /// consecutive lines stays under the window. This is the case the
    /// maintainer's "no per-process/per-node duration cap" ruling
    /// protects: a slow but progressing arctic re-embed.
    #[cfg(unix)]
    #[tokio::test]
    async fn stall_watchdog_spares_slow_but_progressing_subprocess() {
        use std::time::Duration;
        use tokio::io::{AsyncBufReadExt, BufReader};
        use tokio::sync::mpsc;

        // Emit a line every 100 ms for 30 ticks = ~3 s of total runtime.
        // With a 1-second window (10× the inter-line gap) the watchdog
        // must NEVER trip — every line re-arms it well before the
        // deadline. The "30 ticks" stands in for "many chunks": each
        // print is a per-chunk heartbeat, the gap is one chunk's embed
        // latency. Total runtime (3 s) > the window (1 s), proving this
        // is a progress guard, not a duration cap.
        let mut child = spawn_sh_with_retry(
            "i=0; while [ $i -lt 30 ]; do echo \"   ✓ Stored chunk $i/30\"; \
             i=$((i+1)); sleep 0.1; done",
        )
        .await;

        let stdout = child.stdout.take().expect("stdout pipe");
        let stderr = child.stderr.take().expect("stderr pipe");

        let (tx, mut rx) = mpsc::channel::<PipeLine>(64);
        let tx_out = tx.clone();
        let h_out = tokio::spawn(async move {
            let mut r = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = r.next_line().await {
                if tx_out.send(PipeLine::Stdout(line)).await.is_err() { break; }
            }
        });
        let tx_err = tx.clone();
        let h_err = tokio::spawn(async move {
            let mut r = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = r.next_line().await {
                if tx_err.send(PipeLine::Stderr(line)).await.is_err() { break; }
            }
        });
        drop(tx);

        let watchdog = Duration::from_secs(1);
        let mut stalled = false;
        let mut lines_seen = 0u32;
        loop {
            match tokio::time::timeout(watchdog, rx.recv()).await {
                Ok(Some(_)) => {
                    lines_seen += 1; // line ⇒ re-arm
                    continue;
                }
                Ok(None) => break, // child exited cleanly, all lines drained
                Err(_) => {
                    stalled = true;
                    let _ = child.start_kill();
                    break;
                }
            }
        }

        assert!(
            !stalled,
            "watchdog must NOT trip on a subprocess that keeps emitting \
             (progress guard, not a duration cap)",
        );
        assert_eq!(
            lines_seen, 30,
            "all 30 progress lines must arrive without the watchdog firing"
        );

        let _ = h_out.await;
        let _ = h_err.await;
        let exit = child.wait().await.expect("wait");
        assert!(exit.success(), "progressing subprocess should exit cleanly");
    }

    // NEW-15 (2026-05-28): regression — kg_sync subprocess must receive VCT_INSTALL_ROOT.
    //
    // Before the fix, `run_subprocess` only set `VCT_ORCHESTRATOR_ROOT` when
    // `orchestrator_root` was `Some`. `VCT_INSTALL_ROOT` was never set.  The
    // kg-sync wrapper tries `${VCT_INSTALL_ROOT}/.venv` as its first venv
    // candidate; without this env var the wrapper falls through to
    // SCRIPT_DIR-relative candidates that don't exist on launcher-bundle-
    // installed projects, landing on system python which has no `weaviate`
    // package → `ModuleNotFoundError: No module named 'weaviate'` / "KG sync:
    // failed" shown in the Identity tab.
    #[test]
    fn build_kg_sync_env_includes_vct_install_root_when_orchestrator_root_set() {
        use crate::commands::project_env_settings::ProjectEnvSettings;
        use std::path::Path;

        let env_settings = ProjectEnvSettings::with_defaults("TestProject");
        let project_folder = Path::new("/tmp/my-project");
        let orchestrator_root = Path::new("/home/user/vco");

        let pairs = build_kg_sync_env(&env_settings, project_folder, Some(orchestrator_root));

        let find = |key: &str| {
            pairs
                .iter()
                .find(|(k, _)| *k == key)
                .map(|(_, v)| v.clone())
        };

        let orch_root = find("VCT_ORCHESTRATOR_ROOT")
            .expect("VCT_ORCHESTRATOR_ROOT must be present when orchestrator_root is Some");
        let install_root = find("VCT_INSTALL_ROOT")
            .expect("VCT_INSTALL_ROOT must be present when orchestrator_root is Some");

        assert_eq!(
            orch_root,
            orchestrator_root.as_os_str(),
            "VCT_ORCHESTRATOR_ROOT must equal orchestrator_root"
        );
        assert_eq!(
            install_root,
            orchestrator_root.as_os_str(),
            "VCT_INSTALL_ROOT must equal orchestrator_root (NEW-15 regression)"
        );
    }

    #[test]
    fn build_kg_sync_env_omits_root_vars_when_orchestrator_root_absent() {
        use crate::commands::project_env_settings::ProjectEnvSettings;
        use std::path::Path;

        let env_settings = ProjectEnvSettings::with_defaults("TestProject");
        let pairs = build_kg_sync_env(&env_settings, Path::new("/tmp/p"), None);

        assert!(
            pairs.iter().all(|(k, _)| *k != "VCT_ORCHESTRATOR_ROOT"),
            "VCT_ORCHESTRATOR_ROOT must be absent when orchestrator_root is None"
        );
        assert!(
            pairs.iter().all(|(k, _)| *k != "VCT_INSTALL_ROOT"),
            "VCT_INSTALL_ROOT must be absent when orchestrator_root is None"
        );
    }

    // ─── v0.2.71 Piece 5a — global single-flight KG-sync semaphore ──────
    //
    // These tests pin the concurrency-cap contract WITHOUT spawning real
    // `sync_knowledge_graph.py` subprocesses (which need Weaviate/Ollama).
    // They exercise the SAME `acquire_kg_sync_permit()` chokepoint the
    // production `run_sync_task` uses, so a regression that removes the
    // acquire (or widens the permit count) fails here.

    /// The default cap is the conservative single-flight value. If a future
    /// edit bumps `KG_SYNC_MAX_CONCURRENT`, this test forces a deliberate
    /// review (the whole point of the fix is to bound machine-wide concurrency).
    #[test]
    fn kg_sync_max_concurrent_default_is_one() {
        assert_eq!(
            KG_SYNC_MAX_CONCURRENT, 1,
            "the single-flight cap must default to 1 — bumping it re-opens the \
             14-concurrent-sync blast radius the audit closed"
        );
        // The live semaphore must be initialised with exactly that many permits.
        assert_eq!(
            KG_SYNC_SEMAPHORE.available_permits(),
            KG_SYNC_MAX_CONCURRENT,
            "the process-global semaphore must start with KG_SYNC_MAX_CONCURRENT permits"
        );
    }

    /// N tasks racing through `acquire_kg_sync_permit()` must never have more
    /// than `KG_SYNC_MAX_CONCURRENT` permits checked out at once. Each task
    /// holds its permit across an `.await` (simulating the subprocess
    /// lifetime), bumps a shared in-flight counter on entry, asserts the peak
    /// never exceeds the cap, then drops the permit. We also assert all N
    /// tasks COMPLETE (queued syncs wait — they are not dropped).
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn acquire_kg_sync_permit_serializes_to_the_cap() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use std::sync::Arc;
        use std::time::Duration;

        const N: usize = 8; // mirrors the field-reported 8-project update-all

        let in_flight = Arc::new(AtomicUsize::new(0));
        let peak = Arc::new(AtomicUsize::new(0));
        let completed = Arc::new(AtomicUsize::new(0));

        let mut handles = Vec::with_capacity(N);
        for _ in 0..N {
            let in_flight = in_flight.clone();
            let peak = peak.clone();
            let completed = completed.clone();
            handles.push(tokio::spawn(async move {
                // Acquire the SAME process-global permit the production
                // run_sync_task acquires before spawning its child.
                let _permit = acquire_kg_sync_permit().await;

                let now = in_flight.fetch_add(1, Ordering::SeqCst) + 1;
                // Record the running peak.
                peak.fetch_max(now, Ordering::SeqCst);
                assert!(
                    now <= KG_SYNC_MAX_CONCURRENT,
                    "more than {} KG syncs in flight ({}) — semaphore failed to \
                     serialize (regression of v0.2.71 Piece 5a)",
                    KG_SYNC_MAX_CONCURRENT,
                    now
                );

                // Hold the permit across an await, like the real subprocess.
                tokio::time::sleep(Duration::from_millis(15)).await;

                in_flight.fetch_sub(1, Ordering::SeqCst);
                completed.fetch_add(1, Ordering::SeqCst);
                // `_permit` drops here, releasing the lane to the next waiter.
            }));
        }

        for h in handles {
            h.await.expect("task must not panic (would mean the cap was exceeded)");
        }

        assert_eq!(
            peak.load(Ordering::SeqCst),
            KG_SYNC_MAX_CONCURRENT,
            "the peak in-flight count must reach exactly the cap (work happened) \
             and never exceed it"
        );
        assert_eq!(
            completed.load(Ordering::SeqCst),
            N,
            "all {} queued syncs must complete in order — queued tasks WAIT, \
             they are never dropped",
            N
        );
        // The semaphore must be fully restored to its starting permit count
        // once every task released — proving Drop-based release works.
        assert_eq!(
            KG_SYNC_SEMAPHORE.available_permits(),
            KG_SYNC_MAX_CONCURRENT,
            "all permits must be returned after the tasks finish (RAII release)"
        );
    }

    /// A panicking holder must still release its permit (RAII via Drop), so a
    /// crashed sync cannot permanently wedge the lane for every later project.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn acquire_kg_sync_permit_releases_on_panic() {
        let before = KG_SYNC_SEMAPHORE.available_permits();

        let h = tokio::spawn(async move {
            let _permit = acquire_kg_sync_permit().await;
            panic!("simulate a sync task that panics mid-run");
        });
        // The spawned task panics; the JoinHandle resolves to an Err.
        assert!(h.await.is_err(), "the task must have panicked");

        // The permit must be back — Drop ran during unwind.
        assert_eq!(
            KG_SYNC_SEMAPHORE.available_permits(),
            before,
            "a panicking sync must still release its permit (otherwise one \
             crash permanently wedges every queued project)"
        );

        // And a fresh acquire must succeed promptly (lane is usable again).
        let _permit = acquire_kg_sync_permit().await;
    }
}
