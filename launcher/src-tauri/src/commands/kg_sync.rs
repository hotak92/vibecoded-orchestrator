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
//! Idempotency: `sync_knowledge_graph.py` derives Weaviate UUIDs from the
//! node title; re-running on an already-synced project is a content-hash
//! upsert at the Weaviate layer, not a duplicate insert. Safe to invoke
//! repeatedly via `retry_kg_sync`.

use serde::Serialize;
use tauri::{command, AppHandle, Emitter, Manager, State};

use crate::commands::installer::find_local_repo_root;
use crate::commands::project_env_settings::{self, ProjectEnvSettings};
use crate::db::kg_syncs::{status as sync_status, KgSyncRow};
use crate::db::Db;
use vct_launcher_core::process::CommandExt as _;

const SYNC_EVENT: &str = "kg-sync-progress";

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
    pub docs_total: u32,
    pub docs_succeeded: u32,
    pub docs_failed: u32,
    pub error_message: Option<String>,
    pub log_tail: Option<String>,
    /// Live phase indicator. Only populated on `running` events emitted
    /// during the sync (e.g. "scan", "knowledge", "docs"). Always None
    /// for stored rows fetched via `get_kg_sync_status`.
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
            docs_total: row.docs_total,
            docs_succeeded: row.docs_succeeded,
            docs_failed: row.docs_failed,
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
    Ok(db.get_kg_sync(&project_id)?.map(KgSyncView::from_row))
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
    tokio::spawn(async move {
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
pub fn resume_pending_syncs(app: &AppHandle) -> (usize, usize) {
    let db = app.state::<Db>();

    // Phase 1: stale-running sweep.
    let swept = match db.mark_orphaned_running_kg_syncs_failed(
        "launcher crashed mid-run; click Retry to re-run",
    ) {
        Ok(n) => n,
        Err(e) => {
            eprintln!(
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
            eprintln!(
                "[vct] warning: kg-sync pending-list lookup failed: {}. \
                 Queued syncs (if any) will not auto-resume this boot.",
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
                    "[vct] warning: pending kg-sync references missing project {}; skipping",
                    pid
                );
                continue;
            }
            Err(e) => {
                eprintln!(
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
    docs_total: u32,
    docs_succeeded: u32,
    docs_failed: u32,
}

impl ProgressCounts {
    fn zero() -> Self {
        Self {
            kg_total: 0,
            kg_succeeded: 0,
            kg_failed: 0,
            docs_total: 0,
            docs_succeeded: 0,
            docs_failed: 0,
        }
    }
}

/// Default stall-watchdog timeout in seconds. Used when
/// `KG_SYNC_STALL_TIMEOUT_SECS` is unset or unparsable.
///
/// 300 s (5 min) is conservative for the slowest path we've seen in
/// practice: an Ollama node embedding a single ~8 KB body on a CPU-only
/// machine under heavy load takes ~30-60 s. A KG node that ingests a long
/// PDF appendix can produce ~6-10 embedding batches each at that latency,
/// so a full file with no intermediate stdout could plausibly take a few
/// minutes between progress lines. 300 s gives substantial headroom over
/// observed worst-case while still bounding the wedge-recovery window —
/// far better than the unbounded hangs that motivated the 2026-05-12 fix.
/// Override via env if hardware/network is unusually slow.
const DEFAULT_STALL_TIMEOUT_SECS: u64 = 300;

/// Stall-watchdog: resolved from `KG_SYNC_STALL_TIMEOUT_SECS` at task start.
/// 0 disables the watchdog entirely (escape hatch for benchmark / debug).
fn resolve_stall_timeout() -> Option<std::time::Duration> {
    let raw = std::env::var("KG_SYNC_STALL_TIMEOUT_SECS").ok();
    let secs = match raw.as_deref() {
        None => DEFAULT_STALL_TIMEOUT_SECS,
        Some(v) => match v.trim().parse::<u64>() {
            Ok(n) => n,
            Err(_) => {
                eprintln!(
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
/// stderr-side back-pressure deadlock. Stall watchdog runs alongside
/// via `tokio::time::timeout` on the channel `recv()`.
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
                // `send` returns Err if the receiver was dropped. That
                // can happen if the main loop bails on a stall — in
                // which case quietly stop draining; the subprocess will
                // be killed shortly anyway.
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
                } else if let Some((s, f)) = parse_summary_line(&line, "📊 KG:") {
                    counts.kg_succeeded = s;
                    counts.kg_failed = f;
                    kg_summary_seen = true;
                } else if let Some((s, f)) = parse_summary_line(&line, "📊 Docs:") {
                    counts.docs_succeeded = s;
                    counts.docs_failed = f;
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

    // Reap reader tasks. After a stall we've already dropped `rx`
    // (which causes outstanding `send` calls to return Err and the
    // tasks to break their loops) and called `start_kill`; on the
    // normal path the tasks have already finished. Either way, we
    // await them so they don't outlive this function.
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
    // "exited -1" message would be misleading.
    if stalled {
        let secs = stall_timeout.map(|d| d.as_secs()).unwrap_or(0);
        // Stall ⇒ reconcile optimistic counts: we DEFINITIVELY didn't
        // see the script's summary, by definition (no output at all
        // for >timeout seconds). Mirror the post-exit reconcile so
        // banner counts reflect reality rather than mid-flight intent.
        reconcile_optimistic_counts_on_crash(
            &mut counts,
            kg_summary_seen,
            docs_summary_seen,
        );
        return SubprocessOutcome {
            status: sync_status::FAILED.to_string(),
            counts,
            error_message: Some(format!(
                "kg-sync stalled (no output for {}s); subprocess killed. \
                 Set KG_SYNC_STALL_TIMEOUT_SECS to override (0 disables the watchdog).",
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
    }
    if !docs_summary_seen {
        counts.docs_succeeded = 0;
        counts.docs_failed = counts.docs_total;
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
        eprintln!(
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
        docs_total: counts.docs_total,
        docs_succeeded: counts.docs_succeeded,
        docs_failed: counts.docs_failed,
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

/// Look for `kg-sync` (POSIX) / `kg-sync.ps1` (Windows) in:
///   1. `<project>/.claude/scripts/`  — bundle-installed copy.
///   2. `$VCT_LAUNCHER_SCRIPTS_DIR/`  — env override (used by tests).
///   3. sibling-of-exe convention     — bundled launcher installs.
///   4. PATH lookup                   — system-wide.
///
/// Order + structure are a 1:1 mirror of
/// `commands::codegraph::resolve_analyzer_script`.
pub(crate) fn resolve_kg_sync_script(
    project_folder: &std::path::Path,
) -> Option<std::path::PathBuf> {
    let bin = if cfg!(windows) {
        "kg-sync.ps1"
    } else {
        "kg-sync"
    };

    // 1. Project-local
    let p1 = project_folder.join(".claude").join("scripts").join(bin);
    if p1.is_file() {
        return Some(p1);
    }

    // 2. Env override
    if let Ok(dir) = std::env::var("VCT_LAUNCHER_SCRIPTS_DIR") {
        let p2 = std::path::PathBuf::from(dir).join(bin);
        if p2.is_file() {
            return Some(p2);
        }
    }

    // 3. Sibling-of-exe convention
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            for hop in [".", "..", "../.."].iter() {
                let p3 = parent.join(hop).join(".claude").join("scripts").join(bin);
                if p3.is_file() {
                    return Some(p3);
                }
            }
        }
    }

    // 4. PATH lookup
    if let Ok(path) = std::env::var("PATH") {
        let sep = if cfg!(windows) { ';' } else { ':' };
        for d in path.split(sep) {
            let p4 = std::path::Path::new(d).join(bin);
            if p4.is_file() {
                return Some(p4);
            }
        }
    }
    None
}

/// Resolve the (program, base-args) pair for invoking the kg-sync script.
///
/// On Windows we drive the .ps1 wrapper through `powershell.exe -File`;
/// on POSIX we invoke the shell wrapper directly (it's chmod +x, with a
/// `#!/bin/bash` shebang). The caller then appends `--all` + any other
/// per-run args.
///
/// We pin `-ExecutionPolicy Bypass` on Windows because launcher installs
/// frequently land on machines with the default Restricted policy; the
/// script is bundled with VCO and trusted. Pattern matches install.ps1.
fn invocation_for(script: &std::path::Path) -> (std::path::PathBuf, Vec<String>) {
    if cfg!(windows) {
        (
            std::path::PathBuf::from("powershell.exe"),
            vec![
                "-NoProfile".to_string(),
                "-ExecutionPolicy".to_string(),
                "Bypass".to_string(),
                "-File".to_string(),
                script.to_string_lossy().to_string(),
            ],
        )
    } else {
        (script.to_path_buf(), Vec::new())
    }
}

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
/// emoji for robustness. Returns (succeeded, failed) when the line
/// matches, None otherwise.
fn parse_summary_line(line: &str, prefix: &str) -> Option<(u32, u32)> {
    let trimmed = line.trim();
    if !trimmed.starts_with(prefix) && !trimmed.contains(prefix.trim_start_matches("📊 ")) {
        return None;
    }
    let succeeded = extract_number_before(trimmed, "succeeded")?;
    let failed = extract_number_before(trimmed, "failed")?;
    Some((succeeded, failed))
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

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
        let (s, f) = parse_summary_line(line, "📊 KG:").expect("must parse");
        assert_eq!(s, 48);
        assert_eq!(f, 2);
    }

    #[test]
    fn parse_summary_docs_line() {
        let line = "📊 Docs: 12 succeeded, 0 failed";
        let (s, f) = parse_summary_line(line, "📊 Docs:").expect("must parse");
        assert_eq!(s, 12);
        assert_eq!(f, 0);
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

    #[test]
    fn invocation_for_picks_powershell_on_windows() {
        let script = std::path::Path::new("/x/.claude/scripts/kg-sync.ps1");
        let (program, args) = invocation_for(script);
        if cfg!(windows) {
            assert_eq!(program, std::path::PathBuf::from("powershell.exe"));
            assert!(args.contains(&"-File".to_string()));
            assert!(args.iter().any(|a| a.ends_with("kg-sync.ps1")));
        } else {
            assert_eq!(program, script);
            assert!(args.is_empty());
        }
    }

    #[test]
    fn resolve_kg_sync_finds_project_local_copy() {
        let d = tmpdir("resolve");
        let scripts = d.join(".claude").join("scripts");
        fs::create_dir_all(&scripts).unwrap();
        let bin = if cfg!(windows) { "kg-sync.ps1" } else { "kg-sync" };
        let p = scripts.join(bin);
        fs::write(&p, b"#!/usr/bin/env bash\necho ok\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = fs::metadata(&p).unwrap().permissions();
            perms.set_mode(0o755);
            fs::set_permissions(&p, perms).unwrap();
        }
        let resolved = resolve_kg_sync_script(&d).expect("must resolve");
        assert_eq!(resolved, p);
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
            docs_total: 0,
            docs_succeeded: 0,
            docs_failed: 0,
        };
        reconcile_optimistic_counts_on_crash(&mut counts, false, false);
        assert_eq!(counts.kg_succeeded, 0);
        assert_eq!(counts.kg_failed, 58);
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
            docs_total: 12,
            docs_succeeded: 7,
            docs_failed: 0,
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
            docs_total: 12,
            docs_succeeded: 11,
            docs_failed: 1,
        };
        reconcile_optimistic_counts_on_crash(&mut counts, true, true);
        assert_eq!(counts.kg_succeeded, 56);
        assert_eq!(counts.kg_failed, 2);
        assert_eq!(counts.docs_succeeded, 11);
        assert_eq!(counts.docs_failed, 1);
    }

    #[test]
    fn reconcile_handles_zero_total_docs_phase() {
        // No docs/ folder → docs_total=0; reset should not produce
        // weird counts.
        let mut counts = ProgressCounts {
            kg_total: 58,
            kg_succeeded: 17,
            kg_failed: 0,
            docs_total: 0,
            docs_succeeded: 0,
            docs_failed: 0,
        };
        reconcile_optimistic_counts_on_crash(&mut counts, false, false);
        assert_eq!(counts.kg_failed, 58);
        assert_eq!(counts.docs_failed, 0);
    }

    // ─── Bug-3 v0.2.x (2026-05-12): stall-watchdog timeout resolution ────
    //
    // Env vars are process-global; cargo runs unit tests in parallel by
    // default. These four tests mutate `KG_SYNC_STALL_TIMEOUT_SECS` so
    // they must serialize on a local mutex. We can't rely on the
    // single-thread assumption that codegraph.rs's older comment makes;
    // empirically these tests race when run with the default rayon-
    // sized pool. Using a poisoned-safe mutex pattern (grab the lock,
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
        assert_eq!(t, Some(std::time::Duration::from_secs(300)));
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
        assert_eq!(t, Some(std::time::Duration::from_secs(300)));
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

    #[cfg(unix)]
    #[tokio::test]
    async fn stall_watchdog_kills_silent_subprocess() {
        use std::time::Duration;
        use tokio::io::{AsyncBufReadExt, BufReader};
        use tokio::sync::mpsc;

        // `sleep 30` emits nothing on either pipe; the watchdog must
        // detect the stall and kill it. We use a 1-second watchdog to
        // keep the test fast.
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
                Ok(Some(_)) => continue,
                Ok(None) => break, // pipes closed without a stall
                Err(_) => {
                    stalled = true;
                    let _ = child.start_kill();
                    break;
                }
            }
        }
        let elapsed = started.elapsed();

        assert!(stalled, "watchdog must trip on a subprocess that emits nothing");
        // 1 s watchdog + small scheduler slack: must be well under the
        // 30-second sleep duration we asked the subprocess to run.
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
}
