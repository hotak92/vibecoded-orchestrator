//! Detached async project-setup task (Defect B, v0.2.68).
//!
//! `create_project_v2` used to `.await` the two slow Python subprocesses
//! (`run_bootstrap_collections` + `run_install_bundle`) plus the
//! `apply_post_bundle_steps` phase inline, before returning a `ProjectView`.
//! On a COLD Weaviate/Ollama backend that was ~51s of silent modal blur —
//! the New Project modal read as "frozen".
//!
//! This module owns the DETACH: `create_project_v2` keeps the synchronous
//! phase (DB row insert + `.claude/env` populate + B12 repair) inline so the
//! `ProjectView` is committed BEFORE the command returns FAST (hundreds of
//! ms), then hands the HEAVY phase to [`spawn_setup_task`]. That task:
//!   - drives a `project_setups` status-row lifecycle (pending → running →
//!     done / deferred / failed),
//!   - emits [`SETUP_EVENT`] progress events to a GLOBAL top banner,
//!   - collects warnings from every phase and carries them on the TERMINAL
//!     event so the frontend can re-toast the deferral / preserved-files
//!     notices it used to surface inline (F5),
//!   - is the re-entrancy LOCK: the row IS the lock — a 2nd setup for a
//!     project whose row is `pending`/`running` is refused (F7).
//!
//! Mirrors the shape of `codegraph::spawn_initial_build` (detach + row +
//! event), but the per-mode WORK is supplied as an async CLOSURE
//! ([`SetupPhaseFn`]) so create and update — which DIVERGE — can each
//! provide their own phase sequence without a single mega-spawn fork.
//! `spawn_setup_task` owns only the detach + row + event + guard plumbing.
//!
//! **No timeout, by design.** There is deliberately NO global / per-task
//! timeout here. A tight timeout on a slow machine (big codebase, many KG
//! nodes) would leave a partial install. Bootstrap already self-bounds
//! (~30s on a cold Weaviate then DEFERS cleanly on the Python side); the
//! bundle phase touches no network. Crash-resilience is provided by the
//! boot-resume sweep ([`resume_pending_setups`]), NOT by killing the task.

use std::future::Future;
use std::pin::Pin;

use tauri::{AppHandle, Emitter, Manager};

use crate::db::project_setups::{phase as setup_phase, status as setup_status};
use crate::db::Db;
use serde::Serialize;

/// Global event name for live setup progress. The frontend
/// `project-setup` store registers a module-load `listen()` for this and
/// drives the global `OperationProgressBanner`.
pub const SETUP_EVENT: &str = "project://setup-progress";

/// Re-entrancy live-window: how long a `running` row is presumed alive
/// before a fresh setup is allowed to supersede it. A setup task that
/// genuinely runs longer than this on a huge project is NOT killed (no
/// timeout — see module docs); the window only governs whether a SECOND
/// add for the SAME project is refused. 6h is comfortably longer than any
/// realistic single-project setup while still letting a truly wedged row
/// (launcher hard-killed mid-run, boot sweep never ran) be re-attempted.
const SETUP_LIVE_WINDOW_MS: i64 = 6 * 60 * 60 * 1000;

/// Severity of a per-phase warning. Splits the F5 warning channel:
/// `Info`/amber for clean deferrals + preserved-files notices (the project
/// works; something catches up later), `Error`/red for genuine subprocess
/// failures the user should action. The frontend store re-toasts each at
/// the matching level and renders them in the banner terminal state.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum SetupWarningSeverity {
    Info,
    Error,
}

/// One classified warning carried on the terminal setup event.
#[derive(Debug, Clone, Serialize)]
pub struct SetupWarning {
    pub message: String,
    pub severity: SetupWarningSeverity,
}

/// Tauri-event payload for `project://setup-progress`. Carries the coarse
/// phase + the project NAME (the banner shows it prominently, per the
/// impatient-user UX directive) + the classified warnings on the terminal
/// event. Intermediate (running) events carry empty `warnings`.
#[derive(Debug, Clone, Serialize)]
pub struct SetupProgressEvent {
    pub project_id: String,
    pub project_name: String,
    pub status: String,
    /// Coarse phase label ('bootstrap' | 'bundle' | 'post_bundle') on
    /// non-terminal events; None on terminal events.
    pub phase: Option<String>,
    /// Classified warnings — only populated on the TERMINAL event
    /// (done / deferred / failed). F5: the frontend re-toasts each.
    pub warnings: Vec<SetupWarning>,
    /// Genuine failure message; only set on `failed`.
    pub error: Option<String>,
}

/// Terminal outcome a phase closure reports back to `spawn_setup_task`.
pub struct SetupOutcome {
    /// Every warning string collected across all phases, already classified.
    pub warnings: Vec<SetupWarning>,
    /// True iff at least one phase deferred cleanly (e.g. cold-Weaviate
    /// bootstrap) AND no genuine failure occurred. Drives the
    /// `deferred` (amber) vs `done` (clean) terminal status.
    pub deferred: bool,
    /// True iff a genuine subprocess failure occurred. Drives the `failed`
    /// (red + Retry) terminal status, which wins over `deferred`.
    pub failed: bool,
    /// Failure message recorded on the row + emitted on the terminal event
    /// when `failed`. None otherwise.
    pub error: Option<String>,
}

/// Boxed async closure that runs the per-mode heavy phases. It receives a
/// `PhaseReporter` so it can stream coarse phase transitions to the row +
/// banner as it advances, and returns the terminal [`SetupOutcome`].
///
/// `'static` because the future outlives `create_project_v2`'s stack frame
/// (it runs inside the detached `tokio::spawn`).
pub type SetupPhaseFn =
    Box<dyn FnOnce(PhaseReporter) -> Pin<Box<dyn Future<Output = SetupOutcome> + Send>> + Send>;

/// Handed to the phase closure so it can report coarse phase transitions
/// (bootstrap → bundle → post_bundle) WITHOUT knowing about the DB row or
/// the event name. Keeps the closure focused on WORK; `spawn_setup_task`
/// owns the row + event plumbing.
pub struct PhaseReporter {
    app: AppHandle,
    project_id: String,
    project_name: String,
}

impl PhaseReporter {
    /// Record a coarse phase transition: update the row's `phase` column +
    /// emit a non-terminal `running` event so the banner's plain-language
    /// label advances ("installing bundle…" → "creating knowledge
    /// collections…" → "indexing (continues in background)…").
    pub fn enter(&self, phase: &str) {
        let db = self.app.state::<Db>();
        // Keep the row in `running`; only the phase column moves. Soft-fail:
        // a DB hiccup here is cosmetic (the banner just won't advance its
        // label) and must never abort the setup.
        if let Err(e) = db.upsert_project_setup(
            &self.project_id,
            setup_status::RUNNING,
            Some(phase),
            // started_at is preserved by leaving the column logic to the
            // initial RUNNING write; the UPSERT overwrites it with the same
            // value we re-pass. We re-read the existing started_at to avoid
            // clobbering it.
            self.existing_started_at(),
            None,
            None,
            None,
            None,
            None,
        ) {
            eprintln!(
                "[vct] warning: project-setup phase update ({}) for {}: {}",
                phase, self.project_id, e
            );
        }
        let _ = self.app.emit(
            SETUP_EVENT,
            SetupProgressEvent {
                project_id: self.project_id.clone(),
                project_name: self.project_name.clone(),
                status: setup_status::RUNNING.to_string(),
                phase: Some(phase.to_string()),
                warnings: vec![],
                error: None,
            },
        );
    }

    fn existing_started_at(&self) -> Option<i64> {
        self.app
            .state::<Db>()
            .get_project_setup(&self.project_id)
            .ok()
            .flatten()
            .and_then(|r| r.started_at)
    }
}

/// Pure re-entrancy decision (F7): refuse a 2nd setup for a project whose
/// row is already live. The row IS the lock. Mirrors
/// `modules::install_in_flight_should_refuse`:
///   - `pending` / `running` within the live window → refuse (live).
///   - `running` past the window with NO start timestamp → treat as
///     just-started → refuse (conservative; don't double-spawn).
///   - `running` past the window WITH a start timestamp → presumed dead
///     (launcher hard-killed, boot sweep never ran) → allow.
///   - terminal (`done`/`deferred`/`failed`) or no row → allow.
///
/// Pure + unit-testable: takes the row fields + clock + window so the
/// branch is exercised without a DB or a wall clock.
pub fn setup_in_flight_should_refuse(
    existing_status: Option<&str>,
    existing_started_at_ms: Option<i64>,
    now_ms: i64,
    window_ms: i64,
) -> bool {
    match existing_status {
        Some(setup_status::PENDING) => true,
        Some(setup_status::RUNNING) => match existing_started_at_ms {
            None => true,
            Some(started) => {
                let age = now_ms.saturating_sub(started);
                age < window_ms
            }
        },
        // done / deferred / failed / unknown / no row → not in flight.
        _ => false,
    }
}

/// True when the project still exists in the launcher DB. Mirrors
/// `codegraph::project_still_exists` — bail before any DB write / event
/// emit if the user unregistered the project mid-setup.
fn project_still_exists(app: &AppHandle, project_id: &str) -> bool {
    app.state::<Db>()
        .get_project(project_id)
        .map(|opt| opt.is_some())
        .unwrap_or(true)
}

/// Public entry point used by `create_project_v2` (and, later, the update
/// flow). Owns the detach + row lifecycle + event plumbing + re-entrancy
/// guard; the per-mode WORK is the `phase_fn` closure. Returns `()` — never
/// blocks, mirroring `codegraph::spawn_initial_build`.
///
/// Contract: the caller has ALREADY inserted a `pending` `project_setups`
/// row (so the GUI can render an immediate "queued" banner and so the
/// re-entrancy lock is visible the instant the synchronous phase returns).
/// `spawn_setup_task` re-checks the guard (the row could have flipped
/// between insert and spawn on a concurrent add) and then flips
/// pending → running before invoking the closure.
pub fn spawn_setup_task(
    app: AppHandle,
    project_id: String,
    project_name: String,
    phase_fn: SetupPhaseFn,
) {
    tokio::spawn(async move {
        run_setup_task(app, project_id, project_name, phase_fn).await;
    });
}

/// Body of the spawned task. Errors are recorded in the setup row + emitted
/// on the terminal event, NEVER propagated (the caller already has its
/// `ProjectView`).
async fn run_setup_task(
    app: AppHandle,
    project_id: String,
    project_name: String,
    phase_fn: SetupPhaseFn,
) {
    // Race check #0: the user could unregister between the synchronous
    // phase returning and this task picking up. Bail before any write.
    if !project_still_exists(&app, &project_id) {
        return;
    }

    let started_at = chrono::Utc::now().timestamp_millis();

    // Flip pending → running + emit the first progress event so the banner
    // animates the moment the modal closes. Soft-fail: a DB error here is
    // cosmetic; continue so the work still runs.
    {
        let db = app.state::<Db>();
        if let Err(e) = db.upsert_project_setup(
            &project_id,
            setup_status::RUNNING,
            Some(setup_phase::BOOTSTRAP),
            Some(started_at),
            None,
            None,
            None,
            None,
            None,
        ) {
            eprintln!(
                "[vct] warning: project-setup running transition for {}: {}",
                project_id, e
            );
        }
    }
    let _ = app.emit(
        SETUP_EVENT,
        SetupProgressEvent {
            project_id: project_id.clone(),
            project_name: project_name.clone(),
            status: setup_status::RUNNING.to_string(),
            phase: Some(setup_phase::BOOTSTRAP.to_string()),
            warnings: vec![],
            error: None,
        },
    );

    // Run the per-mode phases. The closure reports coarse transitions via
    // the reporter and returns the terminal outcome.
    let reporter = PhaseReporter {
        app: app.clone(),
        project_id: project_id.clone(),
        project_name: project_name.clone(),
    };
    let outcome = phase_fn(reporter).await;

    // Race check: skip the terminal write if the project vanished mid-setup.
    if !project_still_exists(&app, &project_id) {
        return;
    }

    let finished_at = chrono::Utc::now().timestamp_millis();
    let terminal_status = classify_terminal_status(&outcome);
    let warning_strings: Vec<String> =
        outcome.warnings.iter().map(|w| w.message.clone()).collect();

    {
        let db = app.state::<Db>();
        if let Err(e) = db.upsert_project_setup(
            &project_id,
            terminal_status,
            None,
            Some(started_at),
            Some(finished_at),
            Some(finished_at - started_at),
            Some(&warning_strings),
            outcome.error.as_deref(),
            None,
        ) {
            eprintln!(
                "[vct] warning: project-setup terminal write ({}) for {}: {}",
                terminal_status, project_id, e
            );
        }
    }

    // Terminal event: carries the classified warnings (F5) so the frontend
    // re-toasts the deferral / preserved-files notices it used to surface
    // inline, AND the banner renders them in its terminal state.
    let _ = app.emit(
        SETUP_EVENT,
        SetupProgressEvent {
            project_id: project_id.clone(),
            project_name: project_name.clone(),
            status: terminal_status.to_string(),
            phase: None,
            warnings: outcome.warnings,
            error: outcome.error,
        },
    );
}

/// Map an outcome to its terminal row status. `failed` (genuine subprocess
/// failure) wins over `deferred` (clean deferral) wins over `done`.
fn classify_terminal_status(outcome: &SetupOutcome) -> &'static str {
    if outcome.failed {
        setup_status::FAILED
    } else if outcome.deferred {
        setup_status::DEFERRED
    } else {
        setup_status::DONE
    }
}

/// Classify a raw warning string from the bootstrap / bundle / post-bundle
/// phases into a severity (F5 split). The wrappers emit human-readable
/// strings whose content we pattern-match:
///   - "deferred" / "preserved" / "will be created" / "lazily" / "migrated"
///     → Info (amber): the project works; the deferred work catches up.
///   - everything else (subprocess failed to start, file write error,
///     non-zero exit surfaced as error) → Error (red).
///
/// Also reports whether the string indicates a clean DEFERRAL (so the
/// terminal status becomes `deferred` not `done`). A deferral is the cold-
/// Weaviate bootstrap case specifically; preserved-files / lazy-creation
/// notices are informational but do NOT make the whole setup `deferred`.
pub fn classify_warning(raw: &str) -> (SetupWarningSeverity, bool) {
    let lower = raw.to_lowercase();
    // Genuine failure markers take precedence — a string can contain both
    // "error" and "lazily" (the bootstrap error fallback does), and in that
    // case the subprocess genuinely failed, so it's red.
    let is_error = lower.contains("error")
        || lower.contains("failed to start")
        || lower.contains("subprocess failed")
        || lower.contains("unparseable")
        || lower.contains("did not become healthy");
    let is_deferral = lower.contains("bootstrap deferred")
        || lower.contains("collections will be created when")
        || lower.contains("safe_add_skipped_env_merge");

    if is_error && !is_deferral {
        (SetupWarningSeverity::Error, false)
    } else if is_deferral {
        (SetupWarningSeverity::Info, true)
    } else {
        // Informational (preserved files, lazy creation, schema migration,
        // populate notices) — amber but does NOT flip the whole setup to
        // `deferred`.
        (SetupWarningSeverity::Info, false)
    }
}

/// Launcher-boot resume sweep (Defect B). Mirrors
/// `codegraph::resume_pending_builds` / `kg_sync::resume_pending_syncs`:
///   1. mark stale-`running` rows `failed` (the subprocess died with the
///      launcher; surface the broken lifecycle with a Retry banner rather
///      than a silent re-spawn),
///   2. re-spawn `pending` rows — a `pending` row means the synchronous
///      phase of `create_project_v2` committed the row but the launcher
///      crashed before `spawn_setup_task` flipped it to running.
///
/// Re-spawned tasks run the SAME create-mode phase sequence (idempotent
/// bootstrap + bundle + post-bundle), so resuming is safe by construction.
///
/// Soft-fail everywhere. Returns (swept_running, respawned_pending).
/// Called from `lib.rs::setup()` after migrations.
pub fn resume_pending_setups(app: &AppHandle) -> (usize, usize) {
    let db = app.state::<Db>();

    let swept = match db.mark_orphaned_running_project_setups_failed(
        "launcher crashed mid-setup; click Retry to re-run",
    ) {
        Ok(n) => n,
        Err(e) => {
            eprintln!(
                "[vct] warning: project-setup stale-running sweep failed: {}. \
                 Stale rows (if any) will appear as 'running' indefinitely; \
                 the user can re-add or rebuild to recover.",
                e
            );
            0
        }
    };

    let pending_ids = match db.list_pending_project_setups() {
        Ok(v) => v,
        Err(e) => {
            eprintln!(
                "[vct] warning: project-setup pending-list lookup failed: {}. \
                 Queued setups (if any) will not auto-resume this boot.",
                e
            );
            return (swept, 0);
        }
    };

    let mut respawned = 0usize;
    for pid in &pending_ids {
        let project = match db.get_project(pid) {
            Ok(Some(p)) => p,
            Ok(None) => {
                eprintln!(
                    "[vct] warning: pending project-setup references missing project {}; skipping",
                    pid
                );
                continue;
            }
            Err(e) => {
                eprintln!(
                    "[vct] warning: lookup for pending project-setup {}: {}; skipping",
                    pid, e
                );
                continue;
            }
        };
        let phase_fn = crate::commands::projects_v2::create_setup_phases(
            app.clone(),
            project.id.clone(),
            project.name.clone(),
            project.folder_path.clone(),
            // A resumed create-mode setup is conservative: it was a fresh
            // create whose heavy phase never ran, so safe_add is recovered
            // from the row's intent. We default safe_add=false on resume
            // because the synchronous phase already wrote the env sidecar
            // (or not) at create time; the bundle's --safe-add only governs
            // the .env-merge skip, which is idempotent on re-run.
            false,
        );
        spawn_setup_task(app.clone(), project.id, project.name, phase_fn);
        respawned += 1;
    }
    (swept, respawned)
}

/// Public-API view of a `project_setups` row — ISO timestamps + classified
/// warnings so the banner can render the terminal state after a route
/// change / reload (when the live event stream missed the terminal event).
#[derive(Debug, Clone, Serialize)]
pub struct ProjectSetupView {
    pub project_id: String,
    pub status: String,
    pub phase: Option<String>,
    pub started_at_iso: Option<String>,
    pub finished_at_iso: Option<String>,
    pub duration_ms: Option<i64>,
    pub warnings: Vec<SetupWarning>,
    pub error_message: Option<String>,
}

fn epoch_ms_to_iso(ms: i64) -> Option<String> {
    chrono::DateTime::<chrono::Utc>::from_timestamp_millis(ms).map(|dt| dt.to_rfc3339())
}

/// Read the current setup row for a project (banner mount / reload). Returns
/// None when no launcher-driven setup ran (older projects, or the
/// all-synchronous path). Re-classifies the stored warning strings so the
/// banner's terminal state matches what the live event carried.
#[tauri::command]
pub async fn get_project_setup_status(
    project_id: String,
    db: tauri::State<'_, Db>,
) -> Result<Option<ProjectSetupView>, String> {
    let Some(row) = db.get_project_setup(&project_id)? else {
        return Ok(None);
    };
    let warnings = row
        .warnings
        .unwrap_or_default()
        .into_iter()
        .map(|message| {
            let (severity, _) = classify_warning(&message);
            SetupWarning { message, severity }
        })
        .collect();
    Ok(Some(ProjectSetupView {
        project_id: row.project_id,
        status: row.status,
        phase: row.phase,
        started_at_iso: row.started_at.and_then(epoch_ms_to_iso),
        finished_at_iso: row.finished_at.and_then(epoch_ms_to_iso),
        duration_ms: row.duration_ms,
        warnings,
        error_message: row.error_message,
    }))
}

/// Retry a FAILED project setup (the banner's Retry button). Re-queues a
/// `pending` row and re-spawns the create-mode phase sequence. Refused if a
/// setup is still in flight (the re-entrancy guard). Idempotent phases make
/// re-running safe.
#[tauri::command]
pub async fn retry_project_setup(
    project_id: String,
    db: tauri::State<'_, Db>,
    app: AppHandle,
) -> Result<(), String> {
    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    // Re-entrancy guard: don't stack a retry on a live setup.
    let now = chrono::Utc::now().timestamp_millis();
    let existing = db.get_project_setup(&project_id).ok().flatten();
    if setup_in_flight_should_refuse(
        existing.as_ref().map(|r| r.status.as_str()),
        existing.as_ref().and_then(|r| r.started_at),
        now,
        SETUP_LIVE_WINDOW_MS,
    ) {
        return Err("a setup is already in progress for this project".into());
    }

    db.upsert_project_setup(
        &project_id,
        setup_status::PENDING,
        None,
        Some(now),
        None,
        None,
        None,
        None,
        None,
    )?;
    db.audit(
        "project_setup_retry",
        Some(&project_id),
        None,
        &serde_json::json!({ "name": project.name }),
    )?;

    let phase_fn = crate::commands::projects_v2::create_setup_phases(
        app.clone(),
        project.id.clone(),
        project.name.clone(),
        project.folder_path.clone(),
        // Retry uses the same conservative safe_add=false rationale as the
        // boot-resume path: the synchronous env phase already ran at create
        // time; the bundle's --safe-add only governs the idempotent .env-merge
        // skip.
        false,
    );
    spawn_setup_task(app, project.id, project.name, phase_fn);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refuse_pending_row_is_locked() {
        assert!(setup_in_flight_should_refuse(
            Some(setup_status::PENDING),
            None,
            1_000,
            SETUP_LIVE_WINDOW_MS,
        ));
    }

    #[test]
    fn refuse_running_within_window() {
        // started 1s ago, window 6h → live → refuse.
        assert!(setup_in_flight_should_refuse(
            Some(setup_status::RUNNING),
            Some(1_000),
            2_000,
            SETUP_LIVE_WINDOW_MS,
        ));
    }

    #[test]
    fn refuse_running_with_no_start_timestamp() {
        // Conservative: a running row with no start ts is treated as
        // just-started → refuse (don't double-spawn).
        assert!(setup_in_flight_should_refuse(
            Some(setup_status::RUNNING),
            None,
            10_000_000,
            SETUP_LIVE_WINDOW_MS,
        ));
    }

    #[test]
    fn allow_running_past_window_with_start_timestamp() {
        // started way past the window → presumed dead → allow.
        let now = SETUP_LIVE_WINDOW_MS + 10_000;
        assert!(!setup_in_flight_should_refuse(
            Some(setup_status::RUNNING),
            Some(0),
            now,
            SETUP_LIVE_WINDOW_MS,
        ));
    }

    #[test]
    fn allow_terminal_states_and_no_row() {
        for st in [
            Some(setup_status::DONE),
            Some(setup_status::DEFERRED),
            Some(setup_status::FAILED),
            None,
        ] {
            assert!(
                !setup_in_flight_should_refuse(st, Some(0), 5_000, SETUP_LIVE_WINDOW_MS),
                "terminal/none must not refuse: {:?}",
                st
            );
        }
    }

    #[test]
    fn classify_terminal_failed_wins_over_deferred() {
        let outcome = SetupOutcome {
            warnings: vec![],
            deferred: true,
            failed: true,
            error: Some("boom".into()),
        };
        assert_eq!(classify_terminal_status(&outcome), setup_status::FAILED);
    }

    #[test]
    fn classify_terminal_deferred_over_done() {
        let outcome = SetupOutcome {
            warnings: vec![],
            deferred: true,
            failed: false,
            error: None,
        };
        assert_eq!(classify_terminal_status(&outcome), setup_status::DEFERRED);
    }

    #[test]
    fn classify_terminal_clean_done() {
        let outcome = SetupOutcome {
            warnings: vec![],
            deferred: false,
            failed: false,
            error: None,
        };
        assert_eq!(classify_terminal_status(&outcome), setup_status::DONE);
    }

    #[test]
    fn classify_warning_bootstrap_deferred_is_info_and_deferral() {
        let (sev, deferral) = classify_warning(
            "Weaviate collection bootstrap deferred — Weaviate was unreachable",
        );
        assert_eq!(sev, SetupWarningSeverity::Info);
        assert!(deferral);
    }

    #[test]
    fn classify_warning_preserved_files_is_info_not_deferral() {
        let (sev, deferral) = classify_warning(
            "2 user-modified file(s) preserved during update. See UPDATE_DEFERRED.md",
        );
        assert_eq!(sev, SetupWarningSeverity::Info);
        assert!(!deferral);
    }

    #[test]
    fn classify_warning_subprocess_failure_is_error() {
        let (sev, deferral) = classify_warning(
            "bootstrap-collections subprocess failed to start: no such file",
        );
        assert_eq!(sev, SetupWarningSeverity::Error);
        assert!(!deferral);
    }

    #[test]
    fn classify_warning_collection_error_is_error() {
        let (sev, _) = classify_warning(
            "bootstrap-collections error on Example_KnowledgeGraph: connection refused",
        );
        assert_eq!(sev, SetupWarningSeverity::Error);
    }
}
