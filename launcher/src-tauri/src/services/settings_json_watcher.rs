//! PR-42 (v0.2.12 / 2026-05-16): `.claude/settings.json` watcher.
//!
//! Watches every registered project's `.claude/settings.json` file for
//! modify events. When an event fires (debounced 500 ms to coalesce
//! multi-byte writes from editors), invokes the same SIGHUP-based
//! reload logic exposed by the `reload_mcps_sighup` Tauri command —
//! every running orchestrator MCP gets SIGHUP, exits cleanly, then
//! Claude Code respawns it with fresh env on the next request.
//!
//! Design notes
//! ============
//!
//! * **MCPs are per-Claude-Code-session, not per-project.** On this
//!   user's machine `~/.claude.json` is shared by every workspace; an
//!   MCP subprocess belongs to the Claude Code session that spawned
//!   it, NOT to the project whose settings.json triggered the reload.
//!   So the watcher just signals every matching PID it can find —
//!   targeting by project would be more surgical but is unnecessary
//!   given the user-facing semantics ("editing env auto-reloads MCPs").
//!
//! * **Debounce, not throttle.** Editors (VS Code, vim with backup
//!   files, etc.) commonly fire multiple Modify events in quick
//!   succession for a single save: temp-file write → rename → chmod.
//!   We want to coalesce those into ONE SIGHUP burst, hence the
//!   500 ms quiet window after the last event before firing.
//!
//! * **Soft-fail.** A watcher init error (unsupported FS, permission
//!   denied) MUST NOT take the launcher down. We log to stderr and
//!   the manual "Reload MCPs" button stays available as the fallback.
//!
//! * **POSIX-only auto-reload.** On Windows `kill -HUP` doesn't exist;
//!   the watcher still runs (the file-system layer works on every OS)
//!   but the reload call short-circuits with `posix_only_skipped: true`.
//!   Surfacing a Windows toast that says "settings.json changed —
//!   restart your Claude Code session" is out of scope for this PR;
//!   keep the watcher quiet on Windows.
//!
//! Lifecycle
//! =========
//!
//! Spawned once from `lib.rs::run()` setup hook. Owns a tokio task
//! that:
//!   1. Probes the launcher DB every 30 s for the project list.
//!   2. Maintains a `RecommendedWatcher` registered to each project's
//!      `.claude/settings.json` parent dir.
//!   3. Re-syncs the watch list when the project list changes (added,
//!      removed, folder moved).
//!   4. On every Modify event whose path ends in `.claude/settings.json`,
//!      schedules a debounced reload — sleep 500 ms, then call
//!      `reload_mcps_with(...)` directly (NOT through the Tauri
//!      command, which would need a State<Db> we don't have in the
//!      watcher's tokio task).
//!
//! The 30 s project-list re-poll is conservative; new projects appear
//! infrequently and the cost is one DB query.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use notify::{Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use tauri::{AppHandle, Manager, Runtime};
use tokio::sync::Mutex;

use crate::commands::maintenance;
use crate::db::Db;

/// Debounce window after the last Modify event before SIGHUP is fired.
/// Coalesces editor-multiwrite patterns into a single reload.
const DEBOUNCE_WINDOW: Duration = Duration::from_millis(500);

/// Re-poll cadence for the project list. Cheap (one SQLite SELECT) but
/// MCP-process startup costs dwarf this anyway.
const PROJECT_LIST_RE_POLL: Duration = Duration::from_secs(30);

/// Per-process state shared between the file-watcher's blocking
/// callback thread and the tokio task that schedules debounced reloads.
struct WatchState {
    /// Timestamp of the most recent settings.json modify event. The
    /// debounce task checks this and fires SIGHUP iff at least
    /// `DEBOUNCE_WINDOW` has elapsed since the last update.
    last_event: Mutex<Option<Instant>>,
    /// Set of project dirs we're currently watching, for incremental
    /// add/remove logic on project-list changes.
    watched: Mutex<HashSet<PathBuf>>,
}

impl WatchState {
    fn new() -> Self {
        Self {
            last_event: Mutex::new(None),
            watched: Mutex::new(HashSet::new()),
        }
    }
}

/// Spawn the settings.json watcher in the background.
///
/// Idempotent at the API level — `lib.rs::run()` calls this once per
/// launcher process. Soft-fails on `notify` init errors (logs +
/// returns); the manual "Reload MCPs" button stays as the fallback.
pub fn spawn<R: Runtime + 'static>(app: AppHandle<R>) {
    // Windows skip: the OS-level file watcher works, but the
    // downstream `reload_mcps_with` call will short-circuit because
    // `kill -HUP` is POSIX-only. No point burning a task on a no-op.
    if cfg!(windows) {
        eprintln!(
            "[settings_json_watcher] skipped on Windows (SIGHUP is POSIX-only); \
             use the manual 'Reload MCPs' button or restart your Claude Code session"
        );
        return;
    }

    tauri::async_runtime::spawn(async move {
        if let Err(e) = run_loop(app).await {
            eprintln!("[settings_json_watcher] loop exited: {} (manual reload still available)", e);
        }
    });
}

/// Watcher main loop. Holds the `notify::RecommendedWatcher` for the
/// task's lifetime (dropping it tears down the OS-level watch).
async fn run_loop<R: Runtime + 'static>(app: AppHandle<R>) -> Result<(), String> {
    let state = Arc::new(WatchState::new());

    // Channel that the notify callback writes Modify events into.
    // We use std::sync::mpsc because notify's callback is a `Fn`
    // closure executed on a non-tokio thread; the receiver side runs
    // on a tokio task that bridges into the debounce logic.
    let (tx, rx) = std::sync::mpsc::channel::<notify::Result<Event>>();

    let mut watcher: RecommendedWatcher = notify::recommended_watcher(move |res| {
        // Soft-fail: a send error means the receiver is gone, which
        // means we're shutting down. Drop silently.
        let _ = tx.send(res);
    })
    .map_err(|e| format!("notify::recommended_watcher: {}", e))?;

    // Initial project list sync. Failure here is non-fatal — we'll
    // retry on the next 30 s tick.
    if let Err(e) = sync_watches(&app, &mut watcher, &state).await {
        eprintln!("[settings_json_watcher] initial sync failed: {} (will retry)", e);
    }

    // Bridge: a blocking-thread receiver that pumps events into a
    // tokio-friendly channel. We can't directly `await rx.recv()`
    // because std::sync::mpsc::Receiver is sync-only.
    let (evt_tx, mut evt_rx) = tokio::sync::mpsc::unbounded_channel::<Event>();
    std::thread::spawn(move || {
        while let Ok(res) = rx.recv() {
            if let Ok(event) = res {
                if evt_tx.send(event).is_err() {
                    break; // tokio receiver dropped — we're shutting down
                }
            }
            // notify errors are swallowed — the watcher would log them
            // on its own backend, and a single FS-level error shouldn't
            // take down the whole watcher loop.
        }
    });

    let mut re_poll_deadline = Instant::now() + PROJECT_LIST_RE_POLL;
    loop {
        // Race three things:
        //   1. New filesystem event from notify (most frequent).
        //   2. The 30 s project-list re-poll tick (re-sync watches).
        //   3. (No explicit shutdown signal — the launcher exits will
        //      drop the AppHandle and the task naturally terminates
        //      when the next operation fails.)
        let now = Instant::now();
        let until_re_poll = re_poll_deadline.saturating_duration_since(now);

        tokio::select! {
            maybe_evt = evt_rx.recv() => {
                let Some(event) = maybe_evt else { break }; // sender dropped
                if !is_modify(&event) {
                    continue;
                }
                if !any_path_is_settings_json(&event.paths) {
                    continue;
                }
                schedule_debounced_reload(app.clone(), state.clone());
            }
            _ = tokio::time::sleep(until_re_poll) => {
                re_poll_deadline = Instant::now() + PROJECT_LIST_RE_POLL;
                if let Err(e) = sync_watches(&app, &mut watcher, &state).await {
                    eprintln!("[settings_json_watcher] re-sync failed: {} (will retry)", e);
                }
            }
        }
    }
    Ok(())
}

/// Sync the watcher's registered paths against the current project
/// list. Adds new project parent dirs, removes stale ones.
async fn sync_watches<R: Runtime + 'static>(
    app: &AppHandle<R>,
    watcher: &mut RecommendedWatcher,
    state: &WatchState,
) -> Result<(), String> {
    let db = app
        .try_state::<Db>()
        .ok_or_else(|| "launcher.db not available".to_string())?;
    let projects = db.list_projects().map_err(|e| format!("list_projects: {}", e))?;

    let desired: HashSet<PathBuf> = projects
        .iter()
        .map(|p| settings_json_dir_for(&p.folder_path))
        .filter(|p| p.is_dir())
        .collect();

    let mut watched = state.watched.lock().await;

    // Add new.
    for dir in desired.difference(&watched) {
        if let Err(e) = watcher.watch(dir, RecursiveMode::NonRecursive) {
            eprintln!(
                "[settings_json_watcher] watch({}) failed: {} (skipping)",
                dir.display(),
                e
            );
            continue;
        }
    }
    // Remove gone.
    for dir in watched.difference(&desired).cloned().collect::<Vec<_>>() {
        let _ = watcher.unwatch(&dir);
    }
    *watched = desired;
    Ok(())
}

/// `<project>/.claude/` — the parent dir of settings.json. We watch
/// the parent because some editors swap files via rename (which
/// `notify` reports as Create+Remove on the watched parent rather than
/// Modify on the watched file).
fn settings_json_dir_for(project_folder: &str) -> PathBuf {
    Path::new(project_folder).join(".claude")
}

/// True when the event represents a write/touch on a file. We
/// deliberately do NOT match Create/Remove/Rename here — those fire
/// during editor swap-saves and would cause spurious reloads if the
/// editor writes a backup file or a temp file in the same dir.
fn is_modify(event: &Event) -> bool {
    matches!(event.kind, EventKind::Modify(_))
}

/// True when at least one path in the event is `.claude/settings.json`.
/// We're watching the `.claude/` parent dir, so we'll also see events
/// for sibling files (hooks/*.sh, agents/*.md, etc.) — those must NOT
/// trigger an MCP reload.
fn any_path_is_settings_json(paths: &[PathBuf]) -> bool {
    paths.iter().any(|p| {
        p.file_name()
            .and_then(|n| n.to_str())
            .map(|n| n == "settings.json")
            .unwrap_or(false)
    })
}

/// Schedule a debounced reload. Updates `last_event` and spawns a
/// task that sleeps `DEBOUNCE_WINDOW` then checks whether more events
/// arrived in the meantime; only the LAST scheduled task actually
/// fires SIGHUP. (The earlier tasks see a newer `last_event` and bail.)
///
/// This is the lowest-cost debounce pattern that doesn't require a
/// JoinHandle accounting scheme — overlapping tasks are cheap (one
/// sleep + one timestamp compare each).
fn schedule_debounced_reload<R: Runtime + 'static>(app: AppHandle<R>, state: Arc<WatchState>) {
    let scheduled_at = Instant::now();
    let state_for_task = state.clone();
    tauri::async_runtime::spawn(async move {
        {
            let mut last = state.last_event.lock().await;
            *last = Some(scheduled_at);
        }
        tokio::time::sleep(DEBOUNCE_WINDOW).await;
        // If a newer event arrived during our sleep, that newer event's
        // task will handle the reload. Bail.
        {
            let last = state_for_task.last_event.lock().await;
            if *last != Some(scheduled_at) {
                return;
            }
        }
        // We're the most-recent-scheduled task and the debounce window
        // elapsed. Fire the reload.
        fire_reload(app).await;
    });
}

/// Invoke the same `reload_mcps_with` core that the Tauri command
/// uses. We DON'T call the Tauri command directly because:
///   * we don't have a `State<Db>` handy outside an `#[command]` fn,
///   * we want the watcher's audit log entry shape to be distinct so
///     forensics can tell auto-reload events apart from manual ones.
async fn fire_reload<R: Runtime + 'static>(app: AppHandle<R>) {
    // Run the pgrep/kill on a blocking thread — the system calls are
    // synchronous and short, but routing through `spawn_blocking` keeps
    // the tokio worker pool free for other tasks.
    let report = tokio::task::spawn_blocking(maintenance::reload_mcps_via_shell_for_watcher)
        .await
        .unwrap_or_else(|e| {
            eprintln!("[settings_json_watcher] reload task join failed: {}", e);
            maintenance::ReloadReport {
                signaled_count: 0,
                pids: Vec::new(),
                errors: vec![format!("watcher join error: {}", e)],
                posix_only_skipped: false,
            }
        });

    if !report.errors.is_empty() {
        eprintln!(
            "[settings_json_watcher] reload completed with errors: {:?}",
            report.errors
        );
    }
    if report.signaled_count > 0 {
        eprintln!(
            "[settings_json_watcher] auto-reload: signaled {} MCP process(es) [{:?}]",
            report.signaled_count, report.pids
        );
    }

    // Audit log via the DB if available. Soft-fail.
    if let Some(db) = app.try_state::<Db>() {
        let _ = db.audit(
            "settings_json_watcher_auto_reload",
            None,
            None,
            &serde_json::json!({
                "signaled_count": report.signaled_count,
                "pids": report.pids,
                "errors": report.errors,
            }),
        );
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use notify::event::{CreateKind, DataChange, ModifyKind};

    /// Construct a Modify event with the given paths. Uses the
    /// `Event::new(kind).add_path(p)` builder so we don't depend on
    /// `EventAttributes` visibility, which has varied across notify
    /// versions.
    fn modify_event(paths: Vec<PathBuf>) -> Event {
        let mut e = Event::new(EventKind::Modify(ModifyKind::Data(DataChange::Any)));
        for p in paths {
            e = e.add_path(p);
        }
        e
    }

    fn create_event(paths: Vec<PathBuf>) -> Event {
        let mut e = Event::new(EventKind::Create(CreateKind::File));
        for p in paths {
            e = e.add_path(p);
        }
        e
    }

    #[test]
    fn settings_json_dir_for_appends_claude_dir() {
        let p = settings_json_dir_for("/home/foo/proj");
        assert_eq!(p, PathBuf::from("/home/foo/proj/.claude"));
    }

    #[test]
    fn any_path_is_settings_json_matches_target_file() {
        assert!(any_path_is_settings_json(&[PathBuf::from(
            "/home/foo/proj/.claude/settings.json"
        )]));
    }

    #[test]
    fn any_path_is_settings_json_ignores_siblings() {
        // Sibling files in .claude/ MUST NOT trigger reload — we'd
        // re-spawn MCPs every time a hook script or agent .md was
        // edited otherwise.
        assert!(!any_path_is_settings_json(&[PathBuf::from(
            "/home/foo/proj/.claude/hooks/post-edit.sh"
        )]));
        assert!(!any_path_is_settings_json(&[PathBuf::from(
            "/home/foo/proj/.claude/agents/architect.md"
        )]));
    }

    #[test]
    fn any_path_is_settings_json_handles_empty_paths() {
        assert!(!any_path_is_settings_json(&[]));
    }

    #[test]
    fn is_modify_accepts_modify_events_only() {
        assert!(is_modify(&modify_event(vec![PathBuf::from(
            "/x/.claude/settings.json"
        )])));
        // Create events are NOT modify — editors creating temp files
        // during atomic save should not trigger reload directly.
        assert!(!is_modify(&create_event(vec![PathBuf::from(
            "/x/.claude/settings.json.tmp"
        )])));
    }
}
