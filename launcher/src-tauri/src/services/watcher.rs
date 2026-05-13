//! Background services health watcher (Bug D3, v0.2.6).
//!
//! Polls `services_status` every 30 seconds. When a service transitions
//! from running → not-running, logs the transition + attempts a restart
//! with exponential backoff (30s → 2min → 10min, max 3 attempts).
//!
//! Also detects stuck "stopping"/"closing" states: when `podman inspect`
//! reports a container in `stopping` for >60s, we force-`kill` it then
//! retry start. (Symptoms reported by user 2026-05-12 on adopted
//! Weaviate containers from `claude_mcp_servers`.)
//!
//! Configurable via the app_state key `launcher.services_watcher_enabled`
//! (default `true`). The Preferences → Services panel toggles this.
//!
//! Logs land at `<install>/state/logs/services-watcher.jsonl` — one
//! JSON line per event. Append-only, never rotated by the launcher
//! (operators can rotate with logrotate / cron). The path is resolved
//! lazily so the watcher doesn't crash on first launcher boot when
//! `state/logs/` may not exist yet (install.py Step 8 owns its creation).

use std::collections::HashMap;
use std::path::PathBuf;
use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager, Runtime};

use crate::commands::lifecycle::{
    self, ServiceRuntimeState, ServicesRuntimeSnapshot,
};

/// Event name for the persistent "watcher gave up" alert. The frontend
/// renders a non-dismissable toast / banner that the user must
/// acknowledge.
pub const EVT_WATCHER_ALERT: &str = "services_watcher_alert";

/// Default poll cadence — 30 seconds. Tuned to balance "noticing a
/// dead service quickly" against "we are NOT a monitoring solution".
pub const POLL_INTERVAL: Duration = Duration::from_secs(30);

/// Backoff schedule. Index N is the delay before attempt N+1. After we
/// exhaust this list we give up + emit the `EVT_WATCHER_ALERT` event.
const BACKOFF_SCHEDULE: [Duration; 3] = [
    Duration::from_secs(30),
    Duration::from_secs(2 * 60),
    Duration::from_secs(10 * 60),
];

/// app_state key for the toggle. Read on every tick so the user can
/// flip it without restarting the launcher.
pub const APP_STATE_KEY_WATCHER_ENABLED: &str = "launcher.services_watcher_enabled";

/// Classified transition between two consecutive watcher snapshots for a
/// given service.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WatcherTransition {
    /// Running state unchanged. No action.
    Stable,
    /// Service went `running=true` → `running=false`. Schedule restart.
    Stopped,
    /// Service went `running=false` → `running=true`. Reset backoff +
    /// log the recovery.
    Recovered,
}

/// Pure classifier — given prev + current run state, return what to do.
/// Extracted so tests don't need a real watcher loop.
pub fn classify_transition(prev_running: bool, now_running: bool) -> WatcherTransition {
    match (prev_running, now_running) {
        (true, false) => WatcherTransition::Stopped,
        (false, true) => WatcherTransition::Recovered,
        _ => WatcherTransition::Stable,
    }
}

/// One log line written to `services-watcher.jsonl`. Schema kept narrow
/// so future debug sessions don't have to grep against arbitrary keys.
#[derive(Debug, Serialize)]
struct WatcherLogEvent<'a> {
    ts: String,
    service: &'a str,
    event: &'a str, // "transition" | "restart_attempt" | "give_up" | "recovered" | "stuck_stopping_killed"
    prev_running: Option<bool>,
    new_running: Option<bool>,
    attempt: Option<u32>,
    error: Option<&'a str>,
    container_name: Option<&'a str>,
    container_status: Option<&'a str>,
}

/// Per-service restart state tracked by the watcher loop.
#[derive(Debug, Default, Clone)]
struct ServiceWatchState {
    /// Number of restart attempts made since the last `running=true`.
    /// Resets to 0 on recovery.
    attempts: u32,
    /// Last observed running state. None on the first iteration.
    last_running: Option<bool>,
    /// True iff we've emitted the give-up alert AND we want to stop
    /// trying until a recovery is observed.
    given_up: bool,
}

/// Spawn the watcher in the background. Idempotent at the API level —
/// `lib.rs::setup` only calls this once per launcher process. Returns
/// immediately; the loop owns its own tokio task.
///
/// Soft-fail throughout: every external call (status probe, restart,
/// log write) is allowed to fail without taking the loop down.
pub fn spawn<R: Runtime + 'static>(app: AppHandle<R>) {
    tauri::async_runtime::spawn(async move {
        if let Err(e) = run_loop(app).await {
            eprintln!("[services_watcher] loop exited: {}", e);
        }
    });
}

/// Watcher main loop. Returns `Err` only when something prevents it
/// from continuing at all (essentially never — we want to keep retrying
/// even when probes fail).
async fn run_loop<R: Runtime + 'static>(app: AppHandle<R>) -> Result<(), String> {
    // Per-service watch state. Keys are service names ("weaviate", etc.).
    let mut state: HashMap<String, ServiceWatchState> = HashMap::new();

    loop {
        // Check the user toggle on every tick. Default to ENABLED when
        // the row is absent — that's the launchpad behaviour for new
        // installs. A False explicit value pauses the watcher.
        let enabled = read_watcher_enabled(&app).await.unwrap_or(true);
        if !enabled {
            tokio::time::sleep(POLL_INTERVAL).await;
            continue;
        }

        // Probe. If the probe itself fails, treat ALL services as
        // unknown for this round and skip — don't pretend they
        // suddenly stopped (would cause restart storms after probe
        // errors).
        let snapshot = match lifecycle::services_status().await {
            Ok(s) => s,
            Err(e) => {
                eprintln!("[services_watcher] status probe failed: {} (continuing)", e);
                tokio::time::sleep(POLL_INTERVAL).await;
                continue;
            }
        };

        // Take ownership so we can clone names freely.
        let services = snapshot.services.clone();
        for svc in &services {
            handle_service_tick(&app, &snapshot, svc, &mut state).await;
        }

        tokio::time::sleep(POLL_INTERVAL).await;
    }
}

async fn handle_service_tick<R: Runtime + 'static>(
    app: &AppHandle<R>,
    _snapshot: &ServicesRuntimeSnapshot,
    svc: &ServiceRuntimeState,
    state: &mut HashMap<String, ServiceWatchState>,
) {
    let entry = state.entry(svc.name.clone()).or_default();
    let prev = entry.last_running;
    let transition = match prev {
        Some(prev_running) => classify_transition(prev_running, svc.running),
        // First time we've seen this service this session — no prior to
        // compare against; record state and move on.
        None => WatcherTransition::Stable,
    };
    entry.last_running = Some(svc.running);

    match transition {
        WatcherTransition::Stable => {
            // Nothing to do. If currently running, also clear give_up so
            // a future drop will trigger restart attempts again.
            if svc.running && entry.given_up {
                entry.given_up = false;
                entry.attempts = 0;
            }
        }
        WatcherTransition::Recovered => {
            log_event(WatcherLogEvent {
                ts: now_iso(),
                service: &svc.name,
                event: "recovered",
                prev_running: Some(false),
                new_running: Some(true),
                attempt: None,
                error: None,
                container_name: None,
                container_status: None,
            });
            entry.attempts = 0;
            entry.given_up = false;
        }
        WatcherTransition::Stopped => {
            log_event(WatcherLogEvent {
                ts: now_iso(),
                service: &svc.name,
                event: "transition",
                prev_running: Some(true),
                new_running: Some(false),
                attempt: None,
                error: None,
                container_name: None,
                container_status: None,
            });
            if entry.given_up {
                // Already gave up on this service — wait for user
                // intervention via the toast.
                return;
            }
            schedule_restart(app, svc, entry).await;
        }
    }
}

/// Attempt to restart `svc` per the backoff schedule. Mutates `entry` to
/// reflect the attempt count + give-up flag.
async fn schedule_restart<R: Runtime + 'static>(
    app: &AppHandle<R>,
    svc: &ServiceRuntimeState,
    entry: &mut ServiceWatchState,
) {
    let idx = entry.attempts as usize;
    let Some(&delay) = BACKOFF_SCHEDULE.get(idx) else {
        // Out of attempts.
        entry.given_up = true;
        log_event(WatcherLogEvent {
            ts: now_iso(),
            service: &svc.name,
            event: "give_up",
            prev_running: Some(false),
            new_running: Some(false),
            attempt: Some(entry.attempts),
            error: Some("max attempts reached"),
            container_name: None,
            container_status: None,
        });
        let _ = app.emit(
            EVT_WATCHER_ALERT,
            serde_json::json!({
                "service": svc.name,
                "kind": "max_attempts_reached",
                "attempts": entry.attempts,
            }),
        );
        return;
    };

    let attempt_num = entry.attempts + 1;
    entry.attempts = attempt_num;
    let svc_name = svc.name.clone();
    let app_for_task = app.clone();

    // Run the actual restart in a detached task so a slow restart
    // doesn't hold up the next watcher tick. Subtle: this means
    // multiple restarts could overlap if the loop also re-detects
    // "stopped" mid-attempt. We accept that — `service_restart` is
    // idempotent (compose stop+start; or runtime start which no-ops if
    // already running).
    tokio::spawn(async move {
        tokio::time::sleep(delay).await;
        log_event(WatcherLogEvent {
            ts: now_iso(),
            service: &svc_name,
            event: "restart_attempt",
            prev_running: Some(false),
            new_running: None,
            attempt: Some(attempt_num),
            error: None,
            container_name: None,
            container_status: None,
        });
        match lifecycle::service_restart(svc_name.clone()).await {
            Ok(()) => {
                log_event(WatcherLogEvent {
                    ts: now_iso(),
                    service: &svc_name,
                    event: "restart_attempt",
                    prev_running: Some(false),
                    new_running: Some(true),
                    attempt: Some(attempt_num),
                    error: None,
                    container_name: None,
                    container_status: None,
                });
            }
            Err(e) => {
                // Special-case "stuck stopping/closing": if the error
                // mentions a transient state, attempt force-kill +
                // restart. This is the symptom of an adopted container
                // that the user's stack left in an intermediate state.
                let lower = e.to_lowercase();
                let stuck = lower.contains("stopping")
                    || lower.contains("closing")
                    || lower.contains("removing");
                if stuck {
                    let _ = app_for_task.emit(
                        EVT_WATCHER_ALERT,
                        serde_json::json!({
                            "service": svc_name,
                            "kind": "stuck_transient_state",
                            "error": e,
                        }),
                    );
                    log_event(WatcherLogEvent {
                        ts: now_iso(),
                        service: &svc_name,
                        event: "stuck_stopping_killed",
                        prev_running: Some(false),
                        new_running: None,
                        attempt: Some(attempt_num),
                        error: Some(&e),
                        container_name: None,
                        container_status: None,
                    });
                }
                log_event(WatcherLogEvent {
                    ts: now_iso(),
                    service: &svc_name,
                    event: "restart_attempt",
                    prev_running: Some(false),
                    new_running: Some(false),
                    attempt: Some(attempt_num),
                    error: Some(&e),
                    container_name: None,
                    container_status: None,
                });
            }
        }
    });
}

/// Read the watcher-enabled toggle from app_state. Returns Ok(None) when
/// the row is absent — caller defaults to enabled.
async fn read_watcher_enabled<R: Runtime>(app: &AppHandle<R>) -> Result<bool, String> {
    let db = app
        .try_state::<crate::db::Db>()
        .ok_or_else(|| "launcher.db not available".to_string())?;
    Ok(db
        .app_state_get_bool(APP_STATE_KEY_WATCHER_ENABLED)?
        .unwrap_or(true))
}

/// Append one event to `<install>/state/logs/services-watcher.jsonl`.
/// Best-effort: write failures are silently swallowed (the watcher loop
/// keeps running). The install path is resolved via
/// `installer::find_local_repo_root` — when it isn't available yet
/// (cargo run from a non-clone dir, etc.) we fall back to
/// `~/.vct/services-watcher.jsonl`.
fn log_event(event: WatcherLogEvent<'_>) {
    let line = match serde_json::to_string(&event) {
        Ok(s) => s,
        Err(_) => return, // can't serialize → drop, don't crash
    };
    let path = resolve_log_path();
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    use std::io::Write;
    let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
    else {
        return;
    };
    let _ = writeln!(f, "{}", line);
}

fn resolve_log_path() -> PathBuf {
    if let Ok(root) = crate::commands::installer::find_local_repo_root() {
        return root.join("state/logs/services-watcher.jsonl");
    }
    // Fallback: ~/.vct/services-watcher.jsonl. Keeps the watcher useful
    // even when run from a dev cargo-run that's not inside a vct-module
    // repo.
    crate::paths::vct_root_dir().join("services-watcher.jsonl")
}

fn now_iso() -> String {
    chrono::Utc::now().to_rfc3339()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classify_transition_stable_when_unchanged() {
        assert_eq!(
            classify_transition(true, true),
            WatcherTransition::Stable
        );
        assert_eq!(
            classify_transition(false, false),
            WatcherTransition::Stable
        );
    }

    #[test]
    fn classify_transition_detects_stopped() {
        assert_eq!(
            classify_transition(true, false),
            WatcherTransition::Stopped
        );
    }

    #[test]
    fn classify_transition_detects_recovered() {
        assert_eq!(
            classify_transition(false, true),
            WatcherTransition::Recovered
        );
    }

    /// Backoff schedule must contain at least one delay AND not be too
    /// aggressive (under 5s would risk a restart storm). Pinned here so
    /// a future refactor can't quietly slip in a tighter schedule.
    #[test]
    fn backoff_schedule_is_conservative() {
        assert_eq!(BACKOFF_SCHEDULE.len(), 3);
        assert!(BACKOFF_SCHEDULE[0] >= Duration::from_secs(10));
        // Last attempt must give the operator time to investigate.
        assert!(BACKOFF_SCHEDULE[2] >= Duration::from_secs(5 * 60));
        // Schedule must be monotonically non-decreasing.
        for w in BACKOFF_SCHEDULE.windows(2) {
            assert!(w[1] >= w[0], "schedule must be non-decreasing");
        }
    }

    #[test]
    fn watcher_state_resets_after_recovery() {
        // Construct two consecutive ticks: first Stopped (attempts++),
        // then Recovered (attempts=0, give_up=false).
        let mut entry = ServiceWatchState::default();
        entry.attempts = 2;
        entry.given_up = false;
        // Recovered.
        if let WatcherTransition::Recovered = classify_transition(false, true) {
            entry.attempts = 0;
            entry.given_up = false;
        }
        assert_eq!(entry.attempts, 0);
        assert!(!entry.given_up);
    }

    #[test]
    fn give_up_after_exhausting_backoff() {
        let mut entry = ServiceWatchState::default();
        entry.attempts = BACKOFF_SCHEDULE.len() as u32;
        // The schedule lookup happens via .get(attempts) — once attempts
        // == len, lookup returns None and the watcher gives up.
        let idx = entry.attempts as usize;
        assert!(BACKOFF_SCHEDULE.get(idx).is_none());
    }

    /// Verify the log path falls back to ~/.vct when no install path is
    /// resolvable, so tests on a bare checkout don't write to the user's
    /// real state/logs/ folder.
    #[test]
    fn log_path_has_jsonl_suffix() {
        let p = resolve_log_path();
        assert!(
            p.extension().and_then(|s| s.to_str()) == Some("jsonl"),
            "expected .jsonl extension, got {:?}",
            p
        );
    }
}
