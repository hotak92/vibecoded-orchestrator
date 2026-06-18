// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Hub-side infra-container watchdog (v0.2.62).
//!
//! ## Why this module exists (real incident)
//!
//! `vct-hub` is the always-on background service — it outlives the
//! launcher GUI. Until v0.2.62 it supervised only PAID-MODULE containers
//! (`module_supervisor.rs`), never the infra stack. The infra containers
//! — `weaviate` / `ollama` / `code_embed` (container names
//! `vco_weaviate` / `vco_ollama` / `vco_code_embed`, see
//! `infrastructure/docker-compose.yml`) — are brought up only at launcher
//! startup and by the `SessionStart` hook `ensure-containers.sh`. So when
//! an infra container died or was stopped mid-session, NOTHING restarted
//! it until the launcher itself was restarted. KG/code-graph search,
//! embeddings, and the whole MCP surface silently degrade in the
//! meantime.
//!
//! This module spawns a periodic `tokio` task from
//! `server.rs::start_hub_server()` that, every tick, checks each
//! canonical infra service and restarts it via the shared compose layer
//! (`vct_launcher_core::services::runtime`) when it is DOWN — but only
//! when VCO actually manages it and the user hasn't deliberately paused
//! or adopted it.
//!
//! ## Guard rails (the watchdog must never fight a deliberate decision)
//!
//! A service is restarted on a given tick ONLY when ALL hold (see
//! [`service_eligible_for_restart`]):
//!   1. the container is DOWN (not `running`), AND
//!   2. VCO MANAGES it — its adoption mode in `<vct_root>/services.toml`
//!      is `Unresolved` (the default; no entry). `Adopt` / `Parallel` /
//!      `Refuse` are the user's "leave it alone" / "I run my own copy"
//!      decisions and are NEVER touched (see
//!      `vct_launcher_core::services::adoption`), AND
//!   3. the service is NOT paused — no marker file at
//!      `<vct_root>/state/watchdog-paused/<service>` (see
//!      [`pause_marker_path`]). The pause marker is the explicit "stop
//!      restarting this" signal a deliberate `compose stop` can drop so
//!      the watchdog backs off; a RAW external stop with no marker DOES
//!      get restarted (that is the desired self-healing behavior), AND
//!   4. the service has not exhausted its crash-loop budget
//!      (see [`Backoff`]).
//!
//! ## Crash-loop backoff
//!
//! A flapping container (image missing, port already bound, OOM) must not
//! be hammered forever. Each service carries a [`Backoff`] with a
//! consecutive-failure counter. The wait between restart ATTEMPTS grows
//! exponentially (45s → 90s → 180s → … capped at ~30 min, see
//! [`backoff_delay_secs`]); after [`MAX_CONSECUTIVE_FAILURES`] failed
//! restarts in a row the watchdog GIVES UP on that service and logs a
//! single clear error (no infinite thrash). A successful restart resets
//! the counter and clears the give-up state.
//!
//! ## Soft-fail everywhere
//!
//! Any watchdog error (no runtime, compose spawn failure, unreadable
//! state file, missing orchestrator clone) is logged and swallowed — the
//! watchdog never panics, never crashes the hub, and never blocks the
//! serve loop. The task is detached; a single bad tick just means the
//! next tick tries again.
//!
//! ## Cross-platform
//!
//! All container ops go through `vct_launcher_core::services::runtime`
//! (podman/docker detection + compose-form) and `CommandExt::silent`
//! (no console-window flash on Windows). No new OS-specific assumptions.

use std::collections::HashMap;
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use tokio::process::Command;

use vct_launcher_core::db::models::ProjectHost;
use vct_launcher_core::process::CommandExt as _;
use vct_launcher_core::services::adoption::{self, AdoptionMode};
use vct_launcher_core::services::runtime::{detect_runtime, RuntimeInfo};

use crate::modules_api::LauncherDbHandle;

// ─── Constants ──────────────────────────────────────────────────────────

/// Default seconds between watchdog ticks. Overridable via
/// `VCT_HUB_INFRA_WATCHDOG_INTERVAL_SECS`. 45s balances "notice a dead
/// container reasonably fast" against "don't spam podman/docker inspect".
pub const DEFAULT_INTERVAL_SECS: u64 = 45;

/// Floor for the interval override — values below this (or 0) are coerced
/// up so a typo in the env can't turn the watchdog into a busy-loop.
pub const MIN_INTERVAL_SECS: u64 = 5;

/// Cap on the crash-loop backoff wait (~30 min). After enough
/// consecutive failures the per-attempt wait stops growing here.
pub const BACKOFF_CAP_SECS: u64 = 1800;

/// After this many consecutive failed restart attempts, the watchdog
/// gives up on a service and logs one clear error. Reset on any success.
pub const MAX_CONSECUTIVE_FAILURES: u32 = 5;

/// Env var that disables the whole watchdog when set to `0` / `false`
/// (case-insensitive). Any other value (or unset) → enabled.
pub const ENV_ENABLED: &str = "VCT_HUB_INFRA_WATCHDOG";

/// Env var overriding the tick interval (seconds).
pub const ENV_INTERVAL: &str = "VCT_HUB_INFRA_WATCHDOG_INTERVAL_SECS";

/// Canonical infra services the watchdog supervises, paired with the
/// container name the compose file assigns each (see
/// `infrastructure/docker-compose.yml`). This is the ONLY source of
/// service names the watchdog will act on — an arbitrary string can never
/// reach a `compose up` invocation. Kept in lockstep with
/// `canonical_services()` (launcher) / `canonical_service_skeletons()`
/// (hub `lifecycle_api`) / the compose file's `container_name:` fields.
///
/// `(compose_service_name, container_name)`.
pub const CANONICAL_INFRA_SERVICES: [(&str, &str); 3] = [
    ("weaviate", "vco_weaviate"),
    ("ollama", "vco_ollama"),
    ("code_embed", "vco_code_embed"),
];

// ─── Configuration ──────────────────────────────────────────────────────

/// Resolved watchdog configuration, read once at spawn time from the
/// environment.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WatchdogConfig {
    /// Whether the watchdog runs at all.
    pub enabled: bool,
    /// Seconds between ticks.
    pub interval: Duration,
}

impl WatchdogConfig {
    /// Build a config from the process environment, applying the
    /// opt-out + interval-floor rules. Pure aside from the env reads;
    /// the parsing logic is unit-tested via [`parse_enabled`] +
    /// [`parse_interval_secs`].
    pub fn from_env() -> Self {
        let enabled = parse_enabled(std::env::var(ENV_ENABLED).ok().as_deref());
        let interval_secs =
            parse_interval_secs(std::env::var(ENV_INTERVAL).ok().as_deref());
        WatchdogConfig {
            enabled,
            interval: Duration::from_secs(interval_secs),
        }
    }
}

/// Parse the `VCT_HUB_INFRA_WATCHDOG` opt-out value. `0` / `false` /
/// `no` / `off` (case-insensitive, trimmed) → disabled; everything else
/// (including unset / empty) → enabled. Enabled-by-default is the whole
/// point: the watchdog is the safety net.
pub fn parse_enabled(raw: Option<&str>) -> bool {
    match raw {
        None => true,
        Some(v) => {
            let norm = v.trim().to_lowercase();
            !matches!(norm.as_str(), "0" | "false" | "no" | "off")
        }
    }
}

/// Parse the interval override into a seconds value, applying the
/// [`MIN_INTERVAL_SECS`] floor. Unset / unparseable → default.
pub fn parse_interval_secs(raw: Option<&str>) -> u64 {
    match raw.and_then(|v| v.trim().parse::<u64>().ok()) {
        Some(n) if n >= MIN_INTERVAL_SECS => n,
        Some(_) => MIN_INTERVAL_SECS, // 0 / too-small → floor (no busy-loop)
        None => DEFAULT_INTERVAL_SECS,
    }
}

// ─── Pure decision logic ────────────────────────────────────────────────

/// THE core decision: should this service be restarted on this tick?
///
/// Restart iff DOWN **and** VCO-managed (`Unresolved`) **and** not paused.
/// `Adopt` / `Parallel` / `Refuse` are deliberate user decisions the
/// watchdog must never override; a running container needs nothing; a
/// paused service was explicitly told to stay down.
///
/// The crash-loop budget is applied separately by the caller (it depends
/// on mutable per-service state, not on this tick's observation).
pub fn service_eligible_for_restart(running: bool, mode: AdoptionMode, paused: bool) -> bool {
    if running {
        return false;
    }
    if paused {
        return false;
    }
    // Only `Unresolved` (== default / no services.toml entry) means
    // "VCO manages this". Every other mode is the user's call.
    matches!(mode, AdoptionMode::Unresolved)
}

/// Exponential-backoff wait (seconds) before the Nth restart attempt,
/// capped at [`BACKOFF_CAP_SECS`]. `failures` is the count of
/// consecutive failures SO FAR (0 → `base`, 1 → `2*base`, …). Saturating
/// arithmetic so a large `failures` can never overflow — it just pins to
/// the cap.
pub fn backoff_delay_secs(base: u64, failures: u32) -> u64 {
    // 2^failures, saturating: shift overflows past 63 → treat as huge.
    let multiplier: u64 = if failures >= 63 {
        u64::MAX
    } else {
        1u64 << failures
    };
    base.saturating_mul(multiplier).min(BACKOFF_CAP_SECS)
}

/// Per-service crash-loop state. NOT thread-safe by itself — the watchdog
/// owns a `HashMap<String, Backoff>` inside its single task, so no
/// locking is needed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Backoff {
    /// Consecutive failed restart attempts since the last success.
    pub consecutive_failures: u32,
    /// True once we have given up (hit [`MAX_CONSECUTIVE_FAILURES`]).
    /// While true the watchdog skips this service entirely until a
    /// future tick observes it running again (external recovery) →
    /// [`reset_on_success`].
    pub gave_up: bool,
}

impl Backoff {
    /// Has the watchdog exhausted its restart budget for this service?
    pub fn is_given_up(&self) -> bool {
        self.gave_up
    }

    /// Should the watchdog ATTEMPT a restart now, given `failures`
    /// attempts already made? Returns the required wait (seconds) since
    /// the last attempt that the caller must have observed, OR `None`
    /// when we've given up. The watchdog uses a fixed-interval tick, so
    /// it gates attempts by counting ticks; this helper expresses the
    /// schedule for tests + documentation.
    pub fn next_delay_secs(&self, base: u64) -> Option<u64> {
        if self.gave_up {
            return None;
        }
        Some(backoff_delay_secs(base, self.consecutive_failures))
    }

    /// Record a failed restart attempt. Increments the counter and flips
    /// `gave_up` once the budget is exhausted.
    pub fn record_failure(&mut self) {
        self.consecutive_failures = self.consecutive_failures.saturating_add(1);
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES {
            self.gave_up = true;
        }
    }

    /// Reset on a successful restart OR on observing the service running
    /// again (external recovery). Clears both the counter and the
    /// give-up flag so a recovered service is supervised normally again.
    pub fn reset_on_success(&mut self) {
        self.consecutive_failures = 0;
        self.gave_up = false;
    }
}

// ─── Pause-marker mechanism ──────────────────────────────────────────────
//
// We use a marker FILE rather than a launcher.db row, for three reasons:
//   1. The sibling decision file (`services.toml`) is already a
//      hand-editable file under `<vct_root>`; markers keep the
//      "deliberate stop" signal in the same launcher-independent place.
//   2. The watchdog must work when the launcher GUI is closed (the whole
//      point); a file the hub can stat needs no DB write contention with
//      the launcher's WAL handle.
//   3. A `compose stop` wrapper / a future "pause supervision" GUI
//      toggle can drop/remove the marker with a single `touch` / `rm`
//      — trivially scriptable.

/// Directory holding per-service pause markers.
pub fn pause_dir() -> PathBuf {
    vct_launcher_core::paths::vct_root_dir()
        .join("state")
        .join("watchdog-paused")
}

/// Path to the pause marker for `service`. Its mere EXISTENCE means
/// "paused" — content is ignored. `service` is always one of the
/// compiled-in canonical names, so it's a safe single path component.
pub fn pause_marker_path(service: &str) -> PathBuf {
    pause_dir().join(service)
}

/// Is `service` currently paused (marker file present)?
pub fn is_service_paused(service: &str) -> bool {
    pause_marker_path(service).exists()
}

// ─── Orchestrator-clone / compose-file resolution ────────────────────────

/// Locate `<orchestrator_clone>/infrastructure` so we can run compose
/// against `docker-compose.yml`. The hub has no `current_exe()`-walk
/// resolver (that lives launcher-side in `commands::installer`), so we
/// read the orchestrator-root project's `folder_path` from launcher.db
/// — the same row the launcher seeds at install. Returns `None` (soft)
/// when no orchestrator-root row exists or its folder is gone.
pub fn infrastructure_dir(db: &LauncherDbHandle) -> Option<PathBuf> {
    let rows = db.0.list_projects().ok()?;
    let root = rows
        .into_iter()
        .find(|p| p.host == ProjectHost::OrchestratorRoot)?;
    let dir = PathBuf::from(root.folder_path).join("infrastructure");
    // Confirm the compose file is actually there before we hand the dir
    // to compose — a stale/renamed clone shouldn't make us spawn a
    // doomed `compose up` every tick.
    if dir.join("docker-compose.yml").is_file() {
        Some(dir)
    } else {
        None
    }
}

// ─── Container status probe ──────────────────────────────────────────────

/// Is the named container running, per `<runtime> inspect --format
/// {{.State.Status}}`? Soft-fail: a missing container / failed inspect →
/// `false` (treat as down — the watchdog then evaluates whether to
/// restart, gated by the adoption + pause + backoff checks). Mirrors the
/// paid-module supervisor's `is_container_running` but takes the detected
/// runtime explicitly (no per-call re-detection).
pub async fn container_running(runtime: &RuntimeInfo, container_name: &str) -> bool {
    let output = Command::new(&runtime.binary_path)
        .silent()
        .args(["inspect", "--format", "{{.State.Status}}", container_name])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .output()
        .await;
    match output {
        Ok(out) if out.status.success() => {
            let stdout = String::from_utf8_lossy(&out.stdout);
            parse_running_status(&stdout)
        }
        // inspect failed (no such container) or spawn error → down.
        _ => false,
    }
}

/// Pure parser: `<runtime> inspect --format {{.State.Status}}` prints
/// exactly `running` (plus a trailing newline) for a live container.
/// Anything else (`exited`, `created`, empty, `Running`) is not-running.
pub fn parse_running_status(stdout: &str) -> bool {
    stdout.trim() == "running"
}

// ─── Restart action ──────────────────────────────────────────────────────

/// Restart ONE infra service via `<runtime> compose -f
/// infrastructure/docker-compose.yml up -d <service>`. Returns `Ok(())`
/// on a zero-exit compose, `Err(msg)` otherwise (caller records the
/// failure into the service's [`Backoff`]). Soft — never panics.
async fn restart_service(
    runtime: &RuntimeInfo,
    infra_dir: &std::path::Path,
    service: &str,
) -> Result<(), String> {
    let mut cmd = runtime.compose_command();
    cmd.args(["-f", "docker-compose.yml", "up", "-d", service]);
    cmd.current_dir(infra_dir);
    let output = cmd
        .output()
        .await
        .map_err(|e| format!("spawn {} compose: {}", runtime.runtime.display_name(), e))?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "{} compose up -d {} failed (status {}): {}",
            runtime.runtime.display_name(),
            service,
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        ))
    }
}

// ─── Spawn + tick loop ───────────────────────────────────────────────────

/// Spawn the infra watchdog as a detached `tokio` task. Called once from
/// `server.rs::start_hub_server()`. When disabled via the opt-out env,
/// logs that fact and spawns nothing.
pub fn spawn_infra_watchdog(db: LauncherDbHandle) {
    let config = WatchdogConfig::from_env();
    if !config.enabled {
        println!(
            "[vct-hub] infra watchdog DISABLED via {}=0; infra containers \
             will NOT be auto-restarted by the hub.",
            ENV_ENABLED
        );
        return;
    }
    println!(
        "[vct-hub] infra watchdog enabled (interval {}s); supervising {}.",
        config.interval.as_secs(),
        CANONICAL_INFRA_SERVICES
            .iter()
            .map(|(svc, _)| *svc)
            .collect::<Vec<_>>()
            .join(", ")
    );
    tokio::spawn(async move {
        run_watchdog_loop(db, config).await;
    });
}

/// The forever loop: sleep one interval, then run one tick. Sleeping
/// FIRST gives the launcher's own boot-time `services_start_all` +
/// `ensure-containers.sh` a head start so the watchdog isn't racing them
/// on the first few seconds of a cold start.
async fn run_watchdog_loop(db: LauncherDbHandle, config: WatchdogConfig) {
    // Per-service crash-loop state, owned by this single task.
    let mut backoffs: HashMap<String, Backoff> = HashMap::new();
    // Tick counter per service since its last attempt, so we honor the
    // exponential schedule on a fixed-interval ticker without sleeping
    // variable amounts. `attempts_since_wait[svc]` counts ticks elapsed
    // since the last restart attempt for that service.
    let mut ticks_since_attempt: HashMap<String, u64> = HashMap::new();

    let base = config.interval.as_secs().max(1);

    loop {
        tokio::time::sleep(config.interval).await;
        // One tick is fully soft-failed inside; a panic here would kill
        // the task (and only this task), but every fallible op already
        // returns/logs rather than panicking.
        run_one_tick(&db, base, &mut backoffs, &mut ticks_since_attempt).await;
    }
}

/// Execute a single watchdog tick across all canonical infra services.
/// Extracted from the loop so it stays small + so the orchestration is
/// readable. All I/O is soft-failed.
async fn run_one_tick(
    db: &LauncherDbHandle,
    base: u64,
    backoffs: &mut HashMap<String, Backoff>,
    ticks_since_attempt: &mut HashMap<String, u64>,
) {
    // Resolve the runtime once per tick (cached after first detect).
    let runtime = match detect_runtime().await {
        Some(r) => r,
        None => {
            // No podman/docker reachable — nothing the watchdog can do.
            // Quiet (one line) so logs don't fill on a runtime-less host.
            eprintln!(
                "[vct-hub] infra watchdog: no container runtime reachable this \
                 tick; will retry next interval."
            );
            return;
        }
    };

    // Resolve the compose dir once per tick.
    let infra_dir = match infrastructure_dir(db) {
        Some(d) => d,
        None => {
            eprintln!(
                "[vct-hub] infra watchdog: cannot locate the orchestrator clone's \
                 infrastructure/docker-compose.yml (no orchestrator-root project \
                 row, or its folder is gone); skipping this tick."
            );
            return;
        }
    };

    // Read the adoption state once per tick (cheap file read).
    let adoption_state = adoption::read();

    for (service, container) in CANONICAL_INFRA_SERVICES.iter() {
        let service = *service;
        let container = *container;

        let running = container_running(&runtime, container).await;

        // Observing the service UP resets its crash-loop state (covers
        // external recovery: a user / hook restarted it themselves).
        if running {
            if let Some(b) = backoffs.get_mut(service) {
                if b.consecutive_failures > 0 || b.gave_up {
                    b.reset_on_success();
                }
            }
            ticks_since_attempt.remove(service);
            continue;
        }

        let mode = adoption_state
            .get(service)
            .map(|s| s.mode)
            .unwrap_or(AdoptionMode::Unresolved);
        let paused = is_service_paused(service);

        if !service_eligible_for_restart(running, mode, paused) {
            // Down, but the user adopted / parallel'd / refused / paused
            // it. Leave it alone. (Quiet — this is the normal steady
            // state for a deliberately-external service.)
            continue;
        }

        // Down + VCO-managed + not paused. Apply the crash-loop budget.
        let backoff = backoffs.entry(service.to_string()).or_default();
        if backoff.is_given_up() {
            // Already gave up; the one-time error was logged when we
            // crossed the threshold. Stay silent until external recovery
            // (handled by the `running` branch above) re-enables us.
            continue;
        }

        // Honor the exponential schedule: only attempt when enough ticks
        // have elapsed since the last attempt for this service. First
        // sighting (no entry) attempts immediately.
        let required_wait = backoff.next_delay_secs(base).unwrap_or(base);
        let elapsed_ticks = *ticks_since_attempt.get(service).unwrap_or(&u64::MAX);
        let elapsed_secs = elapsed_ticks.saturating_mul(base);
        if elapsed_secs < required_wait {
            // Not yet time for the next attempt — count this tick.
            *ticks_since_attempt.entry(service.to_string()).or_insert(0) += 1;
            continue;
        }

        // Attempt the restart.
        eprintln!(
            "[vct-hub] infra watchdog: {} ({}) is DOWN and VCO-managed; \
             attempting restart (consecutive failures so far: {}).",
            service, container, backoff.consecutive_failures
        );
        match restart_service(&runtime, &infra_dir, service).await {
            Ok(()) => {
                println!(
                    "[vct-hub] infra watchdog: restart of {} ({}) issued \
                     successfully.",
                    service, container
                );
                backoff.reset_on_success();
                ticks_since_attempt.remove(service);
            }
            Err(e) => {
                backoff.record_failure();
                if backoff.is_given_up() {
                    eprintln!(
                        "[vct-hub] infra watchdog: GIVING UP on {} ({}) after {} \
                         consecutive failed restarts. Last error: {}. The hub \
                         will NOT retry until the service is seen running again \
                         (start it manually, or `touch {}` to silence the \
                         watchdog for it). ",
                        service,
                        container,
                        backoff.consecutive_failures,
                        e,
                        pause_marker_path(service).display(),
                    );
                } else {
                    eprintln!(
                        "[vct-hub] infra watchdog: restart of {} ({}) failed \
                         (attempt {} / {}): {}. Backing off.",
                        service,
                        container,
                        backoff.consecutive_failures,
                        MAX_CONSECUTIVE_FAILURES,
                        e
                    );
                }
                // Reset the tick counter so the next attempt waits the
                // (now larger) backoff window.
                ticks_since_attempt.insert(service.to_string(), 0);
            }
        }
    }
}

// ─── Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // ----- opt-out parsing -----

    #[test]
    fn parse_enabled_default_when_unset() {
        assert!(parse_enabled(None), "unset → enabled (safety net default)");
    }

    #[test]
    fn parse_enabled_disabled_tokens() {
        for token in ["0", "false", "FALSE", " no ", "Off", "off"] {
            assert!(
                !parse_enabled(Some(token)),
                "'{}' must disable the watchdog",
                token
            );
        }
    }

    #[test]
    fn parse_enabled_enabled_tokens() {
        for token in ["1", "true", "yes", "on", "", "anything", " 1 "] {
            assert!(
                parse_enabled(Some(token)),
                "'{}' must leave the watchdog enabled",
                token
            );
        }
    }

    // ----- interval parsing -----

    #[test]
    fn parse_interval_default_when_unset_or_garbage() {
        assert_eq!(parse_interval_secs(None), DEFAULT_INTERVAL_SECS);
        assert_eq!(parse_interval_secs(Some("not-a-number")), DEFAULT_INTERVAL_SECS);
        assert_eq!(parse_interval_secs(Some("")), DEFAULT_INTERVAL_SECS);
    }

    #[test]
    fn parse_interval_honors_valid_override() {
        assert_eq!(parse_interval_secs(Some("90")), 90);
        assert_eq!(parse_interval_secs(Some(" 120 ")), 120);
    }

    #[test]
    fn parse_interval_floors_too_small_values() {
        // 0 and tiny values must be floored so we never busy-loop.
        assert_eq!(parse_interval_secs(Some("0")), MIN_INTERVAL_SECS);
        assert_eq!(parse_interval_secs(Some("1")), MIN_INTERVAL_SECS);
        assert_eq!(parse_interval_secs(Some("4")), MIN_INTERVAL_SECS);
        // Exactly the floor is accepted as-is.
        assert_eq!(parse_interval_secs(Some("5")), MIN_INTERVAL_SECS);
    }

    // ----- core eligibility decision -----

    #[test]
    fn eligible_only_when_down_managed_and_not_paused() {
        // The one TRUE case: down + Unresolved (VCO-managed) + not paused.
        assert!(service_eligible_for_restart(false, AdoptionMode::Unresolved, false));
    }

    #[test]
    fn running_service_never_restarted() {
        for mode in [
            AdoptionMode::Unresolved,
            AdoptionMode::Adopt,
            AdoptionMode::Parallel,
            AdoptionMode::Refuse,
        ] {
            assert!(
                !service_eligible_for_restart(true, mode, false),
                "a running service must never be restarted (mode={:?})",
                mode
            );
        }
    }

    #[test]
    fn paused_service_never_restarted() {
        // Paused wins even when down + VCO-managed.
        assert!(!service_eligible_for_restart(false, AdoptionMode::Unresolved, true));
    }

    #[test]
    fn adopted_external_service_never_restarted() {
        // Adopt / Parallel / Refuse are deliberate user decisions — the
        // watchdog must never restart any of them, paused or not.
        for mode in [
            AdoptionMode::Adopt,
            AdoptionMode::Parallel,
            AdoptionMode::Refuse,
        ] {
            assert!(
                !service_eligible_for_restart(false, mode, false),
                "down external service (mode={:?}) must NOT be restarted",
                mode
            );
            assert!(
                !service_eligible_for_restart(false, mode, true),
                "down+paused external service (mode={:?}) must NOT be restarted",
                mode
            );
        }
    }

    // ----- backoff schedule -----

    #[test]
    fn backoff_schedule_is_exponential_then_capped() {
        let base = 45;
        // 45, 90, 180, 360, 720, 1440, then capped at 1800.
        assert_eq!(backoff_delay_secs(base, 0), 45);
        assert_eq!(backoff_delay_secs(base, 1), 90);
        assert_eq!(backoff_delay_secs(base, 2), 180);
        assert_eq!(backoff_delay_secs(base, 3), 360);
        assert_eq!(backoff_delay_secs(base, 4), 720);
        assert_eq!(backoff_delay_secs(base, 5), 1440);
        // 45 * 64 = 2880 > cap → cap.
        assert_eq!(backoff_delay_secs(base, 6), BACKOFF_CAP_SECS);
        assert_eq!(backoff_delay_secs(base, 7), BACKOFF_CAP_SECS);
    }

    #[test]
    fn backoff_delay_never_overflows() {
        // Pathologically large failure counts must saturate to the cap,
        // not panic on shift overflow / multiply overflow.
        assert_eq!(backoff_delay_secs(45, 62), BACKOFF_CAP_SECS);
        assert_eq!(backoff_delay_secs(45, 63), BACKOFF_CAP_SECS);
        assert_eq!(backoff_delay_secs(45, u32::MAX), BACKOFF_CAP_SECS);
        assert_eq!(backoff_delay_secs(u64::MAX, 1), BACKOFF_CAP_SECS);
    }

    // ----- give-up after N consecutive failures + reset on success -----

    #[test]
    fn gives_up_after_max_consecutive_failures() {
        let mut b = Backoff::default();
        assert!(!b.is_given_up());
        // 5 failures (MAX_CONSECUTIVE_FAILURES) → give up.
        for i in 1..=MAX_CONSECUTIVE_FAILURES {
            b.record_failure();
            if i < MAX_CONSECUTIVE_FAILURES {
                assert!(
                    !b.is_given_up(),
                    "must not give up before {} failures (at {})",
                    MAX_CONSECUTIVE_FAILURES,
                    i
                );
            }
        }
        assert!(
            b.is_given_up(),
            "must give up at exactly {} consecutive failures",
            MAX_CONSECUTIVE_FAILURES
        );
        assert_eq!(b.consecutive_failures, MAX_CONSECUTIVE_FAILURES);
        // Given-up service returns no next delay.
        assert_eq!(b.next_delay_secs(45), None);
    }

    #[test]
    fn reset_on_success_clears_counter_and_giveup() {
        let mut b = Backoff::default();
        for _ in 0..MAX_CONSECUTIVE_FAILURES {
            b.record_failure();
        }
        assert!(b.is_given_up());
        b.reset_on_success();
        assert_eq!(b.consecutive_failures, 0);
        assert!(!b.is_given_up());
        // After reset, the schedule starts over at `base`.
        assert_eq!(b.next_delay_secs(45), Some(45));
    }

    #[test]
    fn next_delay_tracks_failure_count() {
        let mut b = Backoff::default();
        assert_eq!(b.next_delay_secs(45), Some(45)); // 0 failures
        b.record_failure();
        assert_eq!(b.next_delay_secs(45), Some(90)); // 1 failure
        b.record_failure();
        assert_eq!(b.next_delay_secs(45), Some(180)); // 2 failures
    }

    // ----- status parsing -----

    #[test]
    fn parse_running_status_only_matches_running() {
        assert!(parse_running_status("running"));
        assert!(parse_running_status("running\n"));
        assert!(parse_running_status("  running  \n"));
        assert!(!parse_running_status("exited"));
        assert!(!parse_running_status("created"));
        assert!(!parse_running_status(""));
        assert!(!parse_running_status("Running")); // case-sensitive
        assert!(!parse_running_status("running x"));
    }

    // ----- canonical service list integrity -----

    #[test]
    fn canonical_services_map_to_vco_prefixed_containers() {
        // The watchdog must only ever touch `vco_*` containers — the
        // compose-defined infra stack. A typo here would let it act on
        // an unrelated container name.
        for (svc, container) in CANONICAL_INFRA_SERVICES.iter() {
            assert!(
                container.starts_with("vco_"),
                "container for service '{}' must be vco_-prefixed, got '{}'",
                svc,
                container
            );
        }
        // Exactly the three infra services, in the documented order.
        let names: Vec<&str> = CANONICAL_INFRA_SERVICES.iter().map(|(s, _)| *s).collect();
        assert_eq!(names, vec!["weaviate", "ollama", "code_embed"]);
    }

    // ----- config from_env round-trip (defaults) -----

    #[test]
    fn watchdog_config_defaults_are_sane() {
        // Don't mutate process env (other tests run in parallel) — just
        // assert the building blocks produce the documented defaults.
        let cfg = WatchdogConfig {
            enabled: parse_enabled(None),
            interval: Duration::from_secs(parse_interval_secs(None)),
        };
        assert!(cfg.enabled);
        assert_eq!(cfg.interval, Duration::from_secs(DEFAULT_INTERVAL_SECS));
    }
}
