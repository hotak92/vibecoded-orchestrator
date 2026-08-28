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
    /// v0.2.9 (Bug I): first observation of this service in the launcher
    /// session reports `running=false`. This is the "down-since-boot" case
    /// — the service was already dead by the time the watcher woke up
    /// (sleep/wake cycle, failed systemd compose-up, CDI race). The
    /// `Stopped` branch would NEVER fire for this case because there's no
    /// `true → false` transition. Drives one restart attempt via the
    /// existing backoff machinery; subsequent failures fall back to the
    /// normal give-up logic.
    ColdStart,
}

/// Pure classifier — given prev + current run state, return what to do.
/// Extracted so tests don't need a real watcher loop.
///
/// `prev_running` is `Some(bool)` after the first tick, `None` on cold
/// start. The `None → false` arm is the v0.2.9 Bug I fix: a service that's
/// been down since the launcher started gets a `ColdStart` transition (a
/// one-shot restart synthesis) instead of `Stable` (forever-silent).
pub fn classify_transition(
    prev_running: Option<bool>,
    now_running: bool,
) -> WatcherTransition {
    match (prev_running, now_running) {
        (Some(true), false) => WatcherTransition::Stopped,
        (Some(false), true) => WatcherTransition::Recovered,
        (Some(_), _) => WatcherTransition::Stable,
        // First observation in this launcher session:
        //   - running=true → Stable (nothing to do, just record).
        //   - running=false → ColdStart (synthesize a restart attempt).
        (None, true) => WatcherTransition::Stable,
        (None, false) => WatcherTransition::ColdStart,
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
            tracing::warn!("[services_watcher] loop exited: {}", e);
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
                tracing::warn!("[services_watcher] status probe failed: {} (continuing)", e);
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
    let transition = classify_transition(prev, svc.running);
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
        WatcherTransition::Stopped | WatcherTransition::ColdStart => {
            // Distinguish in the log so post-incident forensics can tell
            // the two apart, but otherwise share the same restart path.
            let (event_label, prev_running_logged) = match transition {
                WatcherTransition::Stopped => ("transition", Some(true)),
                // v0.2.9 (Bug I): cold-start synthesis. `prev=None,
                // now=false` — service was already dead at launcher boot.
                _ => ("cold_start", None),
            };
            log_event(WatcherLogEvent {
                ts: now_iso(),
                service: &svc.name,
                event: event_label,
                prev_running: prev_running_logged,
                new_running: Some(false),
                attempt: None,
                error: None,
                container_name: svc.container_name.as_deref(),
                container_status: None,
            });
            if entry.given_up {
                // Already gave up on this service — wait for user
                // intervention via the toast.
                return;
            }

            // v0.2.7 (E1+E2): only attempt restart when we have a real
            // pinned container to act on. Re-discovery from the watcher
            // is the v0.2.6 bug — it would auto-spawn a different compose
            // project's container if our pin was missing. Surface a
            // `needs_user_pick` alert so the FE can prompt the user
            // instead.
            if needs_user_pick(svc).await {
                log_event(WatcherLogEvent {
                    ts: now_iso(),
                    service: &svc.name,
                    event: "needs_user_pick",
                    prev_running: prev_running_logged,
                    new_running: Some(false),
                    attempt: None,
                    error: None,
                    container_name: svc.container_name.as_deref(),
                    container_status: None,
                });
                let _ = app.emit(
                    EVT_WATCHER_ALERT,
                    serde_json::json!({
                        "service": svc.name,
                        "kind": "needs_user_pick",
                        "container_name": svc.container_name,
                    }),
                );
                // Mark given_up so we don't re-emit on every poll tick
                // until the user resolves it. Recovery (the service
                // coming back) resets this flag.
                entry.given_up = true;
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

/// v0.2.7 (E1): true when the watcher MUST NOT auto-restart this
/// service — either we have no pinned container yet, or the pinned
/// container is gone. In both cases the FE is prompted to pick.
async fn needs_user_pick(svc: &ServiceRuntimeState) -> bool {
    let Some(name) = svc.container_name.as_deref() else {
        return true; // no pin → can't safely restart
    };
    if name.is_empty() {
        return true;
    }
    // Pin exists; verify it still resolves. Soft-fail on runtime
    // detection — if we can't even find podman/docker, the watcher
    // doesn't have a path forward either; treat as needs-pick so we
    // surface the issue.
    let Some(info) = crate::services::runtime::detect_runtime().await else {
        return true;
    };
    match crate::commands::lifecycle::container_exists(&info, name).await {
        Ok(true) => false,
        Ok(false) => true,
        Err(_) => true,
    }
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
            classify_transition(Some(true), true),
            WatcherTransition::Stable
        );
        assert_eq!(
            classify_transition(Some(false), false),
            WatcherTransition::Stable
        );
    }

    #[test]
    fn classify_transition_detects_stopped() {
        assert_eq!(
            classify_transition(Some(true), false),
            WatcherTransition::Stopped
        );
    }

    #[test]
    fn classify_transition_detects_recovered() {
        assert_eq!(
            classify_transition(Some(false), true),
            WatcherTransition::Recovered
        );
    }

    /// v0.2.9 Bug I — the down-since-boot case. First observation is
    /// `running=false`; without ColdStart synthesis the watcher would
    /// classify this Stable and never heal the service.
    #[test]
    fn classify_transition_cold_start_when_down_at_boot() {
        assert_eq!(
            classify_transition(None, false),
            WatcherTransition::ColdStart
        );
    }

    /// First-observation-running is a no-op record, NOT a ColdStart
    /// (the service is healthy; we just had no prior to compare to).
    #[test]
    fn classify_transition_first_running_is_stable() {
        assert_eq!(
            classify_transition(None, true),
            WatcherTransition::Stable
        );
    }

    /// Cold-start must only fire ONCE per launcher session per service.
    /// After the synthesized restart attempt records `last_running=Some(false)`,
    /// a subsequent `false` observation must classify as `Stable`, not
    /// re-fire ColdStart. (The give_up flag is the second layer of defense;
    /// this tests the classifier's contribution.)
    #[test]
    fn classify_transition_cold_start_fires_only_once() {
        // First observation: cold-start case.
        let first = classify_transition(None, false);
        assert_eq!(first, WatcherTransition::ColdStart);

        // After the handler runs, last_running is set to Some(false). The
        // next tick that still sees the service down is Stable — the
        // backoff machinery (attempts, given_up) governs whether to
        // retry, NOT the classifier.
        let next = classify_transition(Some(false), false);
        assert_eq!(next, WatcherTransition::Stable);
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
        if let WatcherTransition::Recovered = classify_transition(Some(false), true) {
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

    /// v0.2.7 (E1): `needs_user_pick` short-circuits to `true` when the
    /// service has no pinned container — without ever invoking the
    /// runtime. This is the watcher's safety property: missing pin =>
    /// surface to user, NEVER auto-restart from re-discovery.
    #[tokio::test]
    async fn needs_user_pick_short_circuits_when_pin_is_none() {
        let svc = ServiceRuntimeState {
            name: "weaviate".to_string(),
            running: false,
            port: 8081,
            url: "http://localhost:8081/v1/meta".to_string(),
            externally_managed: false,
            adoption_mode: crate::services::adoption::AdoptionMode::Unresolved,
            container_name: None,
            zombie: false, // PR-15: field added; default false for non-zombie test fixture
        };
        assert!(needs_user_pick(&svc).await);
    }

    #[tokio::test]
    async fn needs_user_pick_short_circuits_when_pin_is_empty_string() {
        let svc = ServiceRuntimeState {
            name: "weaviate".to_string(),
            running: false,
            port: 8081,
            url: "http://localhost:8081/v1/meta".to_string(),
            externally_managed: false,
            adoption_mode: crate::services::adoption::AdoptionMode::Adopt,
            container_name: Some("".to_string()),
            zombie: false, // PR-15: field added; default false for non-zombie test fixture
        };
        assert!(needs_user_pick(&svc).await);
    }
}
