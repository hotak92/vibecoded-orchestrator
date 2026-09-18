// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Hub-side supervision of the MODEL GATEWAY process (v0.2.95, R5c).
//!
//! ## The eight hours this exists for
//!
//! 2026-09-10: an `install.py --update` re-rendered the gateway's login unit
//! with an interpreter that cannot import `model_router`. The unit was
//! `enabled`, systemd retried it five times, `StartLimitBurst` parked it in
//! `failed`, and NOTHING looked again — a parked unit stays parked until
//! someone runs `reset-failed`. The launcher's toggle said "registered", and
//! every vendor request failed for eight hours. The user requirement out of
//! that day was three sentences: *"make sure the gateway gets auto-started
//! with hub/launcher/VS Code"*, *"we also should not have multiple running
//! instances"*, *"the hub process should auto-restart it if it crashes + log
//! the warning/issue"*.
//!
//! The session-start half of that lives in the shipped
//! `session-start-ensure-hub.{sh,ps1}` hook. This module is the other half:
//! the hub is the always-on detached service (ruling R20), so it is the
//! natural supervisor for a process that must outlive any one editor session.
//!
//! ## Why a sibling module and not a row in [`crate::infra_watchdog`]
//!
//! `infra_watchdog::CANONICAL_INFRA_SERVICES` is an ALLOWLIST with one
//! invariant: **every name in it reaches a `compose up <name>`**. That is
//! what `watchdog_never_supervises_the_model_gateway_process` pins, and the
//! exclusion is structural rather than a preference — the gateway is a
//! process, `compose up model_gateway` matches no service in
//! `infrastructure/docker-compose.yml`, and an opt-in flag on that list would
//! turn a one-sentence invariant ("these are container names") into a
//! two-sentence one ("these are container names, except the rows where the
//! flag is set"), which is the shape that gets mis-synced later.
//!
//! Nothing else is shared either: the probe is HTTP rather than a container
//! inspect, the heal is a Python CLI rather than compose, and the gate is
//! "is this registration runnable" rather than "did the user adopt this
//! container". What the two DO share — the opt-out/interval parsing and the
//! one user-facing auto-restart toggle — is imported from that module rather
//! than re-typed here.
//!
//! ## One tick
//!
//! 1. **`/health` on the resolved port.** Answering as `vct-model-gateway`
//!    means there is nothing to do, and that is the steady state, so the
//!    common tick spawns no subprocess at all.
//! 2. **A miss spends one budgeted attempt on
//!    `python -m vco_lib.gateway_ensure ensure --json`** — the SAME entry
//!    point the SessionStart hook calls. No start logic is re-implemented
//!    here: that module owns the per-OS sequence (`systemctl --user
//!    reset-failed` *then* `start` on Linux, `launchctl kickstart` on macOS,
//!    `schtasks /Run` on Windows) and every leave-alone case, including the
//!    two that matter most to a background supervisor:
//!    * **not registered** — it does nothing. The gateway is opt-in; nothing
//!      here may create a registration.
//!    * **already running** — it does nothing, decided from the DAEMON's own
//!      pid file. That is the single-instance guard R20 says to reuse; this
//!      module adds no second one and never signals a process.
//!
//!    Calling `ensure` rather than `status` first is deliberate: `ensure`
//!    BEGINS with the same status read, so a separate call would double the
//!    subprocess cost and open a window in which the state changes between
//!    the two answers. Its `state` field reports which leave-alone it took,
//!    so "I looked" and "I started it" stay distinguishable in the log.
//! 3. **`registered_but_unrunnable` is reported, never retried.** Starting a
//!    registration whose entry point cannot run is the one action that
//!    provably cannot help, and it is the 2026-09-10 state.
//!
//! ## Bounded, so it cannot fight systemd
//!
//! The shipped unit already bounds its own crash loop
//! (`StartLimitIntervalSec=600` / `StartLimitBurst=5`), and the heal path
//! calls `reset-failed`, which CLEARS that bound. Left unbounded, that pair
//! is a restart loop with no ceiling at all. So the budget here is
//! deliberately TIGHTER than systemd's: [`MAX_ENSURE_ATTEMPTS`] attempts per
//! [`ATTEMPT_WINDOW_SECS`], then the supervisor gives up, persists a
//! condition the launcher renders, and logs one line saying so. It resumes
//! only when a later tick observes the gateway serving again.
//!
//! ## Soft-fail
//!
//! Every error is logged and swallowed: no `unwrap`, no panic, no early
//! return that kills the loop. A detached task that dies is a safety net that
//! is gone without saying so.

use std::path::{Path, PathBuf};
use std::time::Duration;

use vct_launcher_core::process::CommandExt as _;

use crate::infra_watchdog::{parse_enabled, parse_interval_with, watcher_enabled};
use crate::modules_api::LauncherDbHandle;

// ─── Gateway file/port constants ────────────────────────────────────────
//
// ONE Rust home: `vct_launcher_core::services::model_gateway_port` (v0.2.95).
// This module carried a mirror of them until the launcher's copy and this one
// were collapsed there — see that module's header for why the (A) tier (ask
// the Python resolver) is refused on this path specifically: the whole design
// property of step 1 below is that a healthy machine spawns NO subprocess, and
// a per-tick interpreter start for a two-file read would defeat it. The (C)
// tier's obligation — a parity test that reads `model_router/config.py` and
// fails when a literal moves — now lives with the constants, once, instead of
// once per copy.
//
// Re-exported rather than referenced through the path everywhere, so the many
// uses below (and the tests) read unchanged.
pub use vct_launcher_core::services::model_gateway_port::{
    read_port_file, resolve_port, DEFAULT_GATEWAY_PORT, GATEWAY_SERVICE,
    LAST_PORT_BASENAME, PORT_BASENAME, PORT_ENV,
};

// ─── Watchdog configuration ─────────────────────────────────────────────

/// Env var that disables this supervisor when set to `0`/`false`/`no`/`off`.
/// Separate from the infra watchdog's switch: a user may well want container
/// healing without a background process being started for them.
pub const ENV_ENABLED: &str = "VCT_HUB_GATEWAY_WATCHDOG";

/// Env var overriding the tick interval (seconds).
pub const ENV_INTERVAL: &str = "VCT_HUB_GATEWAY_WATCHDOG_INTERVAL_SECS";

/// Seconds between ticks. 30 s: a loopback `connect()` costs microseconds,
/// and half a minute of a dead gateway is a request or two, not an afternoon.
pub const DEFAULT_INTERVAL_SECS: u64 = 30;

/// Floor for the interval override, so a typo cannot make this a busy-loop.
pub const MIN_INTERVAL_SECS: u64 = 5;

/// Ensure invocations allowed per [`ATTEMPT_WINDOW_SECS`]. Three: enough for
/// a start that loses a race with a slow state dir, few enough that the
/// composite of this and systemd's own `Restart=` stays bounded.
pub const MAX_ENSURE_ATTEMPTS: u32 = 3;

/// The window the attempt budget is counted over (10 minutes).
pub const ATTEMPT_WINDOW_SECS: u64 = 600;

/// How long the supervisor stops asking Python after learning the gateway is
/// not registered (or is disabled by env). The `/health` probe keeps running
/// — it is free — so a gateway that appears is noticed immediately; this only
/// bounds the subprocess on a machine that never opted in.
pub const DORMANT_RECHECK_SECS: u64 = 600;

/// Wall-clock cap on one `vco_lib.gateway_ensure` call. Its own verify step
/// runs the registered argv with `--version` under a 20 s bound and the init
/// tool adds a few seconds; 60 s is that with room, and a hung interpreter is
/// killed rather than left holding the tick.
const ENSURE_TIMEOUT: Duration = Duration::from_secs(60);

/// `/health` probe timeout. Loopback answers or refuses in microseconds; this
/// only bounds a blackholed 127.0.0.1.
const HEALTH_TIMEOUT: Duration = Duration::from_millis(1500);

/// `app_state` key holding the hub's last gateway verdict, as JSON.
///
/// The launcher READS it: `commands::model_gateway` puts it on the Services
/// card, so "the hub tried three times and stopped" is visible where the user
/// looks rather than only in a log the detached hub writes to /dev/null.
/// Written only on give-up and DELETED the moment a tick sees the gateway
/// serving, so it can never outlive the condition it describes.
pub const APP_STATE_KEY_CONDITION: &str = "hub.model_gateway.condition";

/// Audit operation recorded beside the app_state row. Same posture as
/// `module_supervisor`'s `module_hub_reachability_failed`: the hub's stderr is
/// not a durable surface, so a give-up also lands in `audit_log`, which the
/// launcher's `/audit` route renders.
pub const AUDIT_OP_GAVE_UP: &str = "model_gateway_supervisor_gave_up";

/// Resolved configuration, read once at spawn time.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GatewayWatchdogConfig {
    pub enabled: bool,
    pub interval: Duration,
}

impl GatewayWatchdogConfig {
    pub fn from_env() -> Self {
        GatewayWatchdogConfig {
            enabled: parse_enabled(std::env::var(ENV_ENABLED).ok().as_deref()),
            interval: Duration::from_secs(parse_interval_with(
                std::env::var(ENV_INTERVAL).ok().as_deref(),
                DEFAULT_INTERVAL_SECS,
                MIN_INTERVAL_SECS,
            )),
        }
    }
}

// ─── The /health probe ──────────────────────────────────────────────────

/// What a tick saw on the port.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HealthProbe {
    /// A gateway answered `/health` and named itself.
    Serving,
    /// Something answered, but it is not a gateway. NOT restart-eligible: a
    /// start would hand the daemon a port it cannot have, and it would then
    /// fall back to a different one — a background task must not silently
    /// move where this machine's gateway lives.
    Foreign,
    /// The connection was REFUSED: authoritatively nothing there.
    Silent,
    /// Timeout, or a body that could not be read. Skipped — the infra
    /// watchdog's `ProbeError` lesson: collapsing "I could not tell" into
    /// "down" is how a probe storm becomes a restart storm.
    Ambiguous,
}

/// Pure classifier for the body of a `/health` answer, so the decision is
/// testable without a socket. `service` is the payload's `service` field
/// (`None` when the body did not parse).
pub fn classify_health(success: bool, service: Option<&str>) -> HealthProbe {
    if !success {
        // `/health` is unauthenticated and always answers 200, so a non-2xx
        // means SOMETHING is listening that is not this gateway.
        return HealthProbe::Foreign;
    }
    match service {
        Some(s) if s == GATEWAY_SERVICE => HealthProbe::Serving,
        Some(_) => HealthProbe::Foreign,
        None => HealthProbe::Ambiguous,
    }
}

/// Probe `http://127.0.0.1:<port>/health`. Never panics.
pub async fn probe_health(port: u16) -> HealthProbe {
    let client = match reqwest::Client::builder().timeout(HEALTH_TIMEOUT).build() {
        Ok(c) => c,
        Err(_) => return HealthProbe::Ambiguous,
    };
    let url = format!("http://127.0.0.1:{}/health", port);
    match client.get(&url).send().await {
        Ok(resp) => {
            let success = resp.status().is_success();
            let body = resp.json::<serde_json::Value>().await.ok();
            let service = body
                .as_ref()
                .and_then(|v| v.get("service"))
                .and_then(|v| v.as_str())
                .map(str::to_string);
            classify_health(success, service.as_deref())
        }
        Err(e) if e.is_connect() => HealthProbe::Silent,
        Err(_) => HealthProbe::Ambiguous,
    }
}

// ─── Reading `vco_lib.gateway_ensure` ───────────────────────────────────

/// The fields this module reads out of `gateway_ensure ensure --json`.
///
/// `#[serde(default)]` on every field for the same reason the launcher's
/// `/health` parsing carries it: a Python side one version ahead must not
/// make the supervisor unable to read its own answer.
#[derive(Debug, Clone, Default, serde::Deserialize, PartialEq, Eq)]
pub struct EnsureOutcome {
    #[serde(default)]
    pub state: String,
    #[serde(default)]
    pub reason: String,
    #[serde(default)]
    pub unit_path: Option<String>,
    /// The per-OS commands the ensure actually invoked — `systemctl --user
    /// reset-failed` + `start`, `launchctl kickstart`, `schtasks /Run`. Read
    /// only to log what was done; nothing here parses them for meaning.
    #[serde(default)]
    pub commands: Vec<Vec<String>>,
}

/// Parse the ensure CLI's stdout. `None` when it is not a JSON object.
pub fn parse_ensure_json(stdout: &str) -> Option<EnsureOutcome> {
    serde_json::from_str::<EnsureOutcome>(stdout.trim()).ok()
}

/// What the supervisor does about a reported state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Disposition {
    /// Nothing to supervise — the opt-in was never taken, or the user starts
    /// VCO daemons by hand. Silent, and the Python call backs off.
    Dormant,
    /// The daemon's own guard says the process is alive; `/health` just did
    /// not answer. Starting or wedged — either way this module does not kill
    /// a process it did not start.
    AliveNotServing,
    /// A start was requested through the init system.
    Started,
    /// Registered, and its entry point cannot run. Never retried.
    Unrunnable,
    /// Registered and runnable, but no init tool could be invoked.
    StartFailed,
    /// A state this version does not know. Reported, acted on by nothing.
    Unknown,
}

/// Map a `gateway_ensure` state word to what this supervisor does.
///
/// The words are `vco_lib.gateway_ensure.GatewayState`'s values; that enum is
/// the one home and this is a reader of it, not a second copy of its rules.
pub fn disposition_for(state: &str) -> Disposition {
    match state {
        "not_registered" | "disabled_by_env" => Disposition::Dormant,
        "running" => Disposition::AliveNotServing,
        "started" => Disposition::Started,
        "registered_but_unrunnable" => Disposition::Unrunnable,
        "start_failed" => Disposition::StartFailed,
        // `registered_not_running` cannot come back from `ensure` (it starts
        // that one and reports `started`), but it IS what `status` reports,
        // and a defensive reader must not treat an unexpected word as an
        // instruction.
        _ => Disposition::Unknown,
    }
}

// ─── The attempt budget (pure, clock injected) ──────────────────────────

/// Per-loop supervision state. Owned by the single task, so no locking.
///
/// The clock is passed in as monotonic SECONDS rather than read here, so
/// every branch — including "the window rolled over" — is unit-testable
/// without sleeping.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SupervisorState {
    /// Ensure invocations inside the current window.
    pub attempts: u32,
    /// When the current window opened.
    pub window_started: Option<u64>,
    /// Budget exhausted, or an unrunnable registration seen. No further
    /// ensure until the gateway is observed serving.
    pub gave_up: bool,
    /// Suppress the Python call until this second (not registered / kill
    /// switch).
    pub dormant_until: Option<u64>,
    /// The last probe reading, so a steady state logs once rather than every
    /// tick.
    pub last_probe: Option<HealthProbe>,
    /// A condition row is on disk and must be cleared on recovery.
    pub condition_persisted: bool,
}

impl SupervisorState {
    /// May the supervisor spend an ensure invocation now?
    pub fn may_attempt(&self, now: u64) -> bool {
        if self.gave_up {
            return false;
        }
        if matches!(self.dormant_until, Some(until) if now < until) {
            return false;
        }
        match self.window_started {
            // A window that has rolled over is a fresh budget.
            Some(start) if now.saturating_sub(start) < ATTEMPT_WINDOW_SECS => {
                self.attempts < MAX_ENSURE_ATTEMPTS
            }
            _ => true,
        }
    }

    /// Count one invocation, opening or rolling the window as needed, and
    /// flip `gave_up` when the budget is spent.
    pub fn record_attempt(&mut self, now: u64) {
        let rolled = match self.window_started {
            Some(start) => now.saturating_sub(start) >= ATTEMPT_WINDOW_SECS,
            None => true,
        };
        if rolled {
            self.window_started = Some(now);
            self.attempts = 1;
        } else {
            self.attempts = self.attempts.saturating_add(1);
        }
        if self.attempts >= MAX_ENSURE_ATTEMPTS {
            self.gave_up = true;
        }
    }

    /// Un-count an attempt that turned out not to be one (the machine has no
    /// registration, so nothing was tried).
    pub fn refund_attempt(&mut self) {
        self.attempts = self.attempts.saturating_sub(1);
        if self.attempts < MAX_ENSURE_ATTEMPTS {
            self.gave_up = false;
        }
    }

    /// Stop asking Python for a while (nothing is registered here).
    pub fn go_dormant(&mut self, now: u64) {
        self.dormant_until = Some(now.saturating_add(DORMANT_RECHECK_SECS));
    }

    /// An unrunnable registration: give up immediately. Retrying is the one
    /// action that provably cannot help.
    pub fn give_up(&mut self) {
        self.gave_up = true;
    }

    /// The gateway is serving. Everything resets — including the give-up, so
    /// a gateway repaired by an `install.py --update` is supervised again
    /// without restarting the hub.
    ///
    /// Returns `true` when a persisted condition must now be cleared.
    pub fn note_serving(&mut self) -> bool {
        let had_condition = self.condition_persisted;
        self.attempts = 0;
        self.window_started = None;
        self.gave_up = false;
        self.dormant_until = None;
        self.condition_persisted = false;
        had_condition
    }

    /// Should this probe reading be logged? True on a transition only, so a
    /// machine with a foreign service on the port does not fill the log.
    pub fn probe_changed(&mut self, probe: HealthProbe) -> bool {
        let changed = self.last_probe != Some(probe);
        self.last_probe = Some(probe);
        changed
    }
}

// ─── Persisting the condition the launcher renders ──────────────────────

/// The JSON body of [`APP_STATE_KEY_CONDITION`].
pub fn condition_json(state: &str, reason: &str, attempts: u32, port: u16, ts_ms: i64) -> String {
    serde_json::json!({
        "state": state,
        "reason": reason,
        "attempts": attempts,
        "port": port,
        "observed_at_ms": ts_ms,
    })
    .to_string()
}

/// Run a launcher.db write that locks with the PANICKING accessor, without
/// letting a poisoned mutex take this detached task down with it.
///
/// v0.2.95: the `app_state` writes no longer need this — `Db::
/// app_state_set_nonpanicking` and `Db::app_state_delete_like_nonpanicking`
/// now sit beside `app_state_get_bool_nonpanicking` in `vct-launcher-core`,
/// which is where the poison-tolerance belongs: with the accessor, not with
/// every caller that has to remember to wrap it.
///
/// What is left is `Db::audit`, whose `audit_as` implementation locks with
/// `lock().expect("db mutex poisoned")` and also writes `change_log` rows
/// through further locking calls. A poison-tolerant `audit` is therefore a
/// larger change than a one-line sibling (it would duplicate that whole
/// sequence), and this module's contract — never take the hub down — has to
/// hold regardless. So the guard stays, for exactly one caller, and its
/// docstring now says which.
fn soft_db_write(label: &str, f: impl FnOnce() -> Result<(), String>) {
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(f)) {
        Ok(Ok(())) => {}
        Ok(Err(e)) => tracing::warn!(error = %e, "[vct-hub] gateway watchdog: {} failed", label),
        Err(_) => tracing::warn!(
            "[vct-hub] gateway watchdog: {} panicked (poisoned launcher.db mutex); \
             supervision continues.",
            label
        ),
    }
}

fn persist_condition(db: &LauncherDbHandle, state: &str, reason: &str, attempts: u32, port: u16) {
    let body = condition_json(
        state,
        reason,
        attempts,
        port,
        chrono::Utc::now().timestamp_millis(),
    );
    // The poison-tolerant writer does the containment now (v0.2.95), so this
    // path has no `catch_unwind` around it at all — the accessor cannot
    // panic. A failure is still only logged: a supervisor that could not
    // record its verdict must keep supervising.
    if let Err(e) = db.0.app_state_set_nonpanicking(APP_STATE_KEY_CONDITION, &body) {
        tracing::warn!(
            error = %e,
            "[vct-hub] gateway watchdog: persisting the gateway condition failed"
        );
    }
    let handle = db.0.clone();
    let detail = serde_json::json!({
        "state": state,
        "reason": reason,
        "attempts": attempts,
        "port": port,
    });
    soft_db_write("recording the gateway audit row", move || {
        handle.audit(AUDIT_OP_GAVE_UP, None, None, &detail)
    });
}

fn clear_condition(db: &LauncherDbHandle) {
    if let Err(e) = db
        .0
        .app_state_delete_like_nonpanicking(APP_STATE_KEY_CONDITION)
    {
        tracing::warn!(
            error = %e,
            "[vct-hub] gateway watchdog: clearing the gateway condition failed"
        );
    }
}

// ─── Invoking the ONE ensure path ───────────────────────────────────────

/// The orchestrator clone root, for the subprocess `cwd` (`vco_lib` is an
/// in-tree namespace package, so `python -m vco_lib.X` resolves via cwd).
///
/// `orchestrator_manifest::find_orchestrator_manifest` is the ONE resolver —
/// `hooks_enforcement` calls the same function and only wraps the failure in
/// an HTTP error type.
fn orchestrator_root() -> Option<PathBuf> {
    vct_launcher_core::orchestrator_manifest::find_orchestrator_manifest()
        .and_then(|p| p.parent().map(Path::to_path_buf))
}

/// `python -m vco_lib.gateway_ensure ensure --json [--folder ROOT]`.
///
/// `--folder` is passed only when the root carries a `.claude/` directory:
/// that argument is where the ledger row for an unrunnable registration
/// lands, and the Python side refuses to create `.claude/` somewhere the user
/// did not ask for. Passing the orchestrator root means the row appears in
/// the project whose `install.py --update` is the fix.
async fn run_gateway_ensure(root: &Path, python: &Path) -> Result<EnsureOutcome, String> {
    let mut cmd = tokio::process::Command::new(python).silent();
    cmd.arg("-m")
        .arg("vco_lib.gateway_ensure")
        .arg("ensure")
        .arg("--json");
    if root.join(".claude").is_dir() {
        cmd.arg("--folder").arg(root.as_os_str());
    }
    cmd.current_dir(root);
    cmd.stdin(std::process::Stdio::null());
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());

    let output = match tokio::time::timeout(ENSURE_TIMEOUT, cmd.output()).await {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => return Err(format!("cannot run {}: {}", python.display(), e)),
        Err(_) => {
            return Err(format!(
                "vco_lib.gateway_ensure did not finish within {} s",
                ENSURE_TIMEOUT.as_secs()
            ))
        }
    };
    let stdout = String::from_utf8_lossy(&output.stdout);
    parse_ensure_json(&stdout).ok_or_else(|| {
        format!(
            "vco_lib.gateway_ensure returned no readable JSON. stdout: {} stderr: {}",
            stdout.trim(),
            String::from_utf8_lossy(&output.stderr).trim()
        )
    })
}

// ─── Spawn + loop ───────────────────────────────────────────────────────

/// Spawn the gateway supervisor as a detached task. Called once from
/// [`crate::server::start_hub_server`].
///
/// DELIVERY (ruling R17): there is nothing to register. The supervisor is
/// code inside the `vct-hub` binary, so every install that refreshes that
/// binary — which `install.py --update` does, after stopping the hub so the
/// swap is not blocked, and before restarting it — has supervision on the
/// next hub start. No new hook, no settings.json entry, no manifest row, no
/// user step. It is ON by default, like the infra watchdog, with the same
/// shape of env opt-out.
pub fn spawn_gateway_watchdog(db: LauncherDbHandle) {
    let config = GatewayWatchdogConfig::from_env();
    if !config.enabled {
        tracing::info!(
            "[vct-hub] model-gateway watchdog DISABLED via {}=0; a gateway that \
             dies will NOT be restarted by the hub.",
            ENV_ENABLED
        );
        return;
    }
    tracing::info!(
        interval_secs = config.interval.as_secs(),
        max_attempts = MAX_ENSURE_ATTEMPTS,
        window_secs = ATTEMPT_WINDOW_SECS,
        "[vct-hub] model-gateway watchdog enabled (probes /health; heals through \
         `python -m vco_lib.gateway_ensure`)."
    );
    tokio::spawn(async move {
        run_gateway_loop(db, config).await;
    });
}

/// Monotonic seconds since the loop started — the clock the budget counts in.
fn now_secs(started: std::time::Instant) -> u64 {
    started.elapsed().as_secs()
}

async fn run_gateway_loop(db: LauncherDbHandle, config: GatewayWatchdogConfig) {
    let started = std::time::Instant::now();
    let mut state = SupervisorState::default();
    loop {
        // Sleep FIRST: the SessionStart hook and the login unit are the
        // cold-start path, and racing them on the first seconds of a boot
        // would spend budget on a gateway that is already coming up.
        tokio::time::sleep(config.interval).await;

        // ONE user-facing switch. `launcher.services_watcher_enabled` is the
        // Preferences toggle the launcher's own services watcher and the
        // infra watchdog already honour; a user who turned auto-restart off
        // must not get restarts from a third restarter.
        if !watcher_enabled(&db) {
            continue;
        }
        run_one_tick(&db, &mut state, now_secs(started)).await;
    }
}

/// One tick. Every I/O failure is soft; nothing here can return an error.
async fn run_one_tick(db: &LauncherDbHandle, state: &mut SupervisorState, now: u64) {
    let port = resolve_port();
    let probe = probe_health(port).await;
    let changed = state.probe_changed(probe);

    match probe {
        HealthProbe::Serving => {
            if state.note_serving() {
                clear_condition(db);
                tracing::info!(
                    port,
                    "[vct-hub] model-gateway watchdog: the gateway is serving again; \
                     the persisted 'gateway down' condition has been cleared."
                );
            } else if changed {
                tracing::debug!(port, "[vct-hub] model-gateway watchdog: serving.");
            }
            return;
        }
        HealthProbe::Foreign => {
            if changed {
                tracing::warn!(
                    port,
                    "[vct-hub] model-gateway watchdog: something is listening on port \
                     {} that is not a VCO gateway. Leaving it alone — starting the \
                     gateway now would move it to a fallback port behind your back.",
                    port
                );
            }
            return;
        }
        HealthProbe::Ambiguous => {
            // Could not tell. Never treated as down.
            tracing::debug!(
                port,
                "[vct-hub] model-gateway watchdog: /health was unreadable this tick; \
                 re-probing next interval."
            );
            return;
        }
        HealthProbe::Silent => {}
    }

    if !state.may_attempt(now) {
        return;
    }

    let Some(root) = orchestrator_root() else {
        if changed {
            tracing::debug!(
                "[vct-hub] model-gateway watchdog: cannot locate the orchestrator \
                 clone (vct-module.json) from the running hub binary; skipping."
            );
        }
        return;
    };
    let Some(python) = vct_launcher_core::python_resolve::resolve_python_for_vco_lib() else {
        if changed {
            tracing::warn!(
                "[vct-hub] model-gateway watchdog: no Python interpreter for vco_lib \
                 (checked $VCT_VENV and the install venvs), so the gateway cannot be \
                 ensured. This is a broken install, not a missing gateway."
            );
        }
        return;
    };

    // Counted BEFORE the call: a call that hangs or fails to spawn must spend
    // budget too, or a permanently broken interpreter is an unbounded loop.
    state.record_attempt(now);
    let budget_spent = state.gave_up;

    match run_gateway_ensure(&root, &python).await {
        Err(e) => {
            tracing::warn!(
                port,
                attempt = state.attempts,
                error = %e,
                "[vct-hub] model-gateway watchdog: could not ask vco_lib.gateway_ensure."
            );
            if budget_spent {
                persist_condition(db, "ensure_unavailable", &e, state.attempts, port);
                state.condition_persisted = true;
            }
        }
        Ok(out) => match disposition_for(&out.state) {
            Disposition::Dormant => {
                // Silent and successful: the gateway is opt-in and nothing
                // here may register one. The attempt is refunded — a machine
                // that never opted in has spent no budget.
                state.refund_attempt();
                state.go_dormant(now);
                tracing::debug!(
                    state = %out.state,
                    "[vct-hub] model-gateway watchdog: nothing registered to supervise."
                );
            }
            Disposition::Started => {
                tracing::info!(
                    port,
                    attempt = state.attempts,
                    commands = ?out.commands,
                    "[vct-hub] model-gateway watchdog: the gateway was not answering; \
                     a start was requested through its login registration."
                );
                if budget_spent {
                    persist_condition(db, &out.state, &out.reason, state.attempts, port);
                    state.condition_persisted = true;
                }
            }
            Disposition::AliveNotServing => {
                tracing::warn!(
                    port,
                    reason = %out.reason,
                    "[vct-hub] model-gateway watchdog: the gateway process is alive but \
                     /health did not answer — it is starting, or it is wedged. Not \
                     signalled: this supervisor never kills a process it did not start."
                );
                if budget_spent {
                    persist_condition(db, &out.state, &out.reason, state.attempts, port);
                    state.condition_persisted = true;
                }
            }
            Disposition::Unrunnable => {
                // The 2026-09-10 state. One warning naming the cause, one
                // persisted condition, and no further attempts: the entry
                // point baked into the registration is what fails, so
                // starting it again cannot help.
                tracing::warn!(
                    port,
                    unit = out.unit_path.as_deref().unwrap_or("<unknown>"),
                    reason = %out.reason,
                    "[vct-hub] model-gateway watchdog: the gateway is REGISTERED BUT \
                     CANNOT RUN. Not retrying — re-run `python install.py --update` \
                     from the orchestrator root, which re-renders and verifies the \
                     registration."
                );
                persist_condition(db, &out.state, &out.reason, state.attempts, port);
                state.condition_persisted = true;
                state.give_up();
            }
            Disposition::StartFailed => {
                tracing::warn!(
                    port,
                    attempt = state.attempts,
                    reason = %out.reason,
                    "[vct-hub] model-gateway watchdog: the start could not be issued."
                );
                if budget_spent {
                    persist_condition(db, &out.state, &out.reason, state.attempts, port);
                    state.condition_persisted = true;
                }
            }
            Disposition::Unknown => {
                tracing::warn!(
                    state = %out.state,
                    reason = %out.reason,
                    "[vct-hub] model-gateway watchdog: vco_lib.gateway_ensure reported a \
                     state this hub does not know. Doing nothing."
                );
            }
        },
    }

    if state.gave_up && state.condition_persisted {
        tracing::error!(
            port,
            attempts = state.attempts,
            "[vct-hub] model-gateway watchdog: GIVING UP. No further start attempts \
             until the gateway is seen serving again; the launcher's Services card \
             shows the recorded reason."
        );
    }
}

// ─── Tests ──────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // ----- config -----

    #[test]
    fn config_defaults_are_the_documented_ones() {
        let cfg = GatewayWatchdogConfig {
            enabled: parse_enabled(None),
            interval: Duration::from_secs(parse_interval_with(
                None,
                DEFAULT_INTERVAL_SECS,
                MIN_INTERVAL_SECS,
            )),
        };
        assert!(cfg.enabled, "enabled by default — it is the safety net");
        assert_eq!(cfg.interval, Duration::from_secs(30));
    }

    #[test]
    fn interval_override_is_floored_not_trusted() {
        assert_eq!(
            parse_interval_with(Some("0"), DEFAULT_INTERVAL_SECS, MIN_INTERVAL_SECS),
            MIN_INTERVAL_SECS
        );
        assert_eq!(
            parse_interval_with(Some("90"), DEFAULT_INTERVAL_SECS, MIN_INTERVAL_SECS),
            90
        );
        assert_eq!(
            parse_interval_with(Some("junk"), DEFAULT_INTERVAL_SECS, MIN_INTERVAL_SECS),
            DEFAULT_INTERVAL_SECS
        );
    }

    #[test]
    fn the_two_watchdogs_have_separate_switches() {
        assert_ne!(
            ENV_ENABLED,
            crate::infra_watchdog::ENV_ENABLED,
            "container healing and process supervision are different consents"
        );
    }

    // ----- /health classification -----

    #[test]
    fn classify_health_act_and_leave_alone() {
        assert_eq!(
            classify_health(true, Some(GATEWAY_SERVICE)),
            HealthProbe::Serving
        );
        // Someone else's service on our port is NOT our gateway being down.
        assert_eq!(
            classify_health(true, Some("some-other-service")),
            HealthProbe::Foreign
        );
        assert_eq!(classify_health(false, None), HealthProbe::Foreign);
        // 200 with a body that did not parse: ambiguous, never "down".
        assert_eq!(classify_health(true, None), HealthProbe::Ambiguous);
    }

    // ----- port resolution -----
    //
    // The reader's damage tolerance and the parity pin against
    // `model_router/config.py` moved WITH the constants into
    // `vct_launcher_core::services::model_gateway_port` (v0.2.95) and are
    // tested there, once, rather than once per consumer. What stays here is
    // the property this module depends on: the names it re-exports resolve to
    // that home, so a future edit cannot quietly reintroduce a local copy.

    #[test]
    fn the_port_constants_are_the_shared_cores() {
        use vct_launcher_core::services::model_gateway_port as home;
        assert_eq!(DEFAULT_GATEWAY_PORT, home::DEFAULT_GATEWAY_PORT);
        assert_eq!(PORT_ENV, home::PORT_ENV);
        assert_eq!(PORT_BASENAME, home::PORT_BASENAME);
        assert_eq!(LAST_PORT_BASENAME, home::LAST_PORT_BASENAME);
        assert_eq!(GATEWAY_SERVICE, home::GATEWAY_SERVICE);
        // Not vacuous: a `pub use` of a name this module also DECLARED would
        // not compile, so the only way to reintroduce a local copy is to drop
        // the re-export — at which point these comparisons are against two
        // real values and fail the moment they differ. Which is the whole
        // failure this extraction removes.
    }

    /// The repo root, from the crate manifest dir. `None` when the sources are
    /// not beside the build (a packaged binary).
    fn repo_root() -> Option<PathBuf> {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .and_then(Path::parent)
            .map(Path::to_path_buf)
    }

    // ----- the ensure contract -----

    #[test]
    fn parse_ensure_json_reads_the_contract_and_survives_new_fields() {
        let out = parse_ensure_json(
            r#"{"state":"started","reason":"start requested","unit_path":"/u/x.service",
                "commands":[["systemctl","--user","start","vct-model-gateway.service"]],
                "a_field_from_a_newer_python":42}"#,
        )
        .expect("a JSON object must parse");
        assert_eq!(out.state, "started");
        assert_eq!(out.commands[0][0], "systemctl");
        assert_eq!(out.unit_path.as_deref(), Some("/u/x.service"));
    }

    #[test]
    fn parse_ensure_json_refuses_non_json() {
        assert!(parse_ensure_json("Traceback (most recent call last):").is_none());
        assert!(parse_ensure_json("").is_none());
    }

    #[test]
    fn every_documented_gateway_state_has_a_disposition() {
        // The words are `vco_lib.gateway_ensure.GatewayState`'s values.
        assert_eq!(disposition_for("not_registered"), Disposition::Dormant);
        assert_eq!(disposition_for("disabled_by_env"), Disposition::Dormant);
        assert_eq!(disposition_for("running"), Disposition::AliveNotServing);
        assert_eq!(disposition_for("started"), Disposition::Started);
        assert_eq!(
            disposition_for("registered_but_unrunnable"),
            Disposition::Unrunnable
        );
        assert_eq!(disposition_for("start_failed"), Disposition::StartFailed);
        assert_eq!(
            disposition_for("registered_not_running"),
            Disposition::Unknown,
            "ensure never returns it; an unexpected word is not an instruction"
        );
        assert_eq!(disposition_for("something_new"), Disposition::Unknown);
    }

    /// R12/R14 tri-OS: the per-OS shape reaches this module only as data —
    /// the `commands` the ensure ran. The DECISION must be identical for all
    /// three, and none of them may turn an unrunnable registration into a
    /// start.
    #[test]
    fn the_decision_is_the_same_on_all_three_init_systems() {
        let linux = r#"{"state":"started","reason":"start requested","commands":
            [["systemctl","--user","reset-failed","vct-model-gateway.service"],
             ["systemctl","--user","start","vct-model-gateway.service"]]}"#;
        let macos = r#"{"state":"started","reason":"start requested","commands":
            [["launchctl","kickstart","gui/501/com.vibecodedtools.model-gateway"]]}"#;
        let windows = r#"{"state":"started","reason":"start requested","commands":
            [["schtasks","/Run","/TN","VCT Model Gateway"]]}"#;
        for (os, payload) in [("linux", linux), ("macos", macos), ("windows", windows)] {
            let out = parse_ensure_json(payload).unwrap_or_else(|| panic!("{} payload", os));
            assert_eq!(
                disposition_for(&out.state),
                Disposition::Started,
                "{}: a start is a start on every init system",
                os
            );
            assert!(!out.commands.is_empty(), "{}: the commands are reported", os);
        }

        // The leave-alone half, with each OS's own artefact path in the
        // payload. None of them is ever restarted.
        for (os, unit) in [
            (
                "linux",
                "/home/u/.config/systemd/user/vct-model-gateway.service",
            ),
            (
                "macos",
                "/Users/u/Library/LaunchAgents/com.vibecodedtools.model-gateway.plist",
            ),
            (
                "windows",
                "C:\\Users\\u\\AppData\\Local\\VCT\\model-gateway-task.xml",
            ),
        ] {
            let payload = serde_json::json!({
                "state": "registered_but_unrunnable",
                "reason": "the registered entry point cannot run",
                "unit_path": unit,
            })
            .to_string();
            let out = parse_ensure_json(&payload).unwrap();
            assert_eq!(
                disposition_for(&out.state),
                Disposition::Unrunnable,
                "{}: an unrunnable registration is never started",
                os
            );
            assert_eq!(out.unit_path.as_deref(), Some(unit));
        }
    }

    // ----- the budget (act AND leave-alone) -----

    #[test]
    fn a_fresh_supervisor_attempts_immediately() {
        let st = SupervisorState::default();
        assert!(st.may_attempt(0), "the first miss is acted on at once");
    }

    #[test]
    fn the_budget_stops_at_three_attempts_in_the_window() {
        let mut st = SupervisorState::default();
        for tick in 0..MAX_ENSURE_ATTEMPTS {
            let now = u64::from(tick) * 30;
            assert!(st.may_attempt(now), "attempt {} must be allowed", tick + 1);
            st.record_attempt(now);
        }
        assert!(st.gave_up, "the budget is spent");
        assert!(
            !st.may_attempt(30 * u64::from(MAX_ENSURE_ATTEMPTS)),
            "a fourth attempt inside the window must NOT happen — that is what \
             keeps this from clearing systemd's own start limit forever"
        );
        // …and still not, much later: give-up is cleared only by observing the
        // gateway serving.
        assert!(!st.may_attempt(ATTEMPT_WINDOW_SECS * 10));
    }

    #[test]
    fn a_rolled_window_is_a_fresh_budget_when_we_have_not_given_up() {
        let mut st = SupervisorState::default();
        st.record_attempt(0);
        st.record_attempt(30);
        assert!(!st.gave_up, "two of three used");
        assert!(
            st.may_attempt(ATTEMPT_WINDOW_SECS + 1),
            "the window rolled over"
        );
        st.record_attempt(ATTEMPT_WINDOW_SECS + 1);
        assert_eq!(st.attempts, 1, "the counter restarts with the window");
    }

    #[test]
    fn serving_again_restores_supervision_and_asks_for_the_clear() {
        let mut st = SupervisorState::default();
        st.record_attempt(0);
        st.record_attempt(30);
        st.record_attempt(60);
        st.condition_persisted = true;
        assert!(st.gave_up);

        assert!(st.note_serving(), "a persisted condition must be cleared");
        assert!(!st.gave_up, "a repaired gateway is supervised again");
        assert_eq!(st.attempts, 0);
        assert!(st.may_attempt(90));
        assert!(
            !st.note_serving(),
            "a second serving tick has nothing left to clear — no repeated writes"
        );
    }

    #[test]
    fn an_unrunnable_registration_is_never_retried() {
        let mut st = SupervisorState::default();
        st.record_attempt(0);
        st.give_up();
        assert!(!st.may_attempt(1));
        assert!(!st.may_attempt(ATTEMPT_WINDOW_SECS * 100));
    }

    #[test]
    fn an_unregistered_machine_spends_no_budget() {
        let mut st = SupervisorState::default();
        for tick in 0..MAX_ENSURE_ATTEMPTS {
            let now = u64::from(tick) * DORMANT_RECHECK_SECS;
            assert!(st.may_attempt(now));
            st.record_attempt(now);
            // What the Dormant branch does.
            st.refund_attempt();
            st.go_dormant(now);
        }
        assert!(!st.gave_up, "nothing was ever tried, so nothing was spent");
        assert_eq!(st.attempts, 0);
    }

    #[test]
    fn dormancy_suppresses_the_python_call_but_not_forever() {
        let mut st = SupervisorState::default();
        st.go_dormant(100);
        assert!(!st.may_attempt(101), "an unregistered machine is left alone");
        assert!(!st.may_attempt(100 + DORMANT_RECHECK_SECS - 1));
        assert!(
            st.may_attempt(100 + DORMANT_RECHECK_SECS),
            "a gateway registered mid-session is picked up on the recheck"
        );
    }

    #[test]
    fn probe_transitions_are_logged_once_not_every_tick() {
        let mut st = SupervisorState::default();
        assert!(st.probe_changed(HealthProbe::Foreign), "first sighting");
        assert!(
            !st.probe_changed(HealthProbe::Foreign),
            "steady state is quiet"
        );
        assert!(st.probe_changed(HealthProbe::Silent), "a change speaks");
    }

    // ----- the persisted condition -----

    #[test]
    fn condition_json_carries_what_the_card_renders() {
        let body = condition_json("registered_but_unrunnable", "cannot import", 3, 11460, 42);
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["state"], "registered_but_unrunnable");
        assert_eq!(v["reason"], "cannot import");
        assert_eq!(v["attempts"], 3);
        assert_eq!(v["port"], 11460);
        assert_eq!(v["observed_at_ms"], 42);
    }

    #[test]
    fn the_condition_key_is_the_one_the_launcher_reads() {
        // The reader is `commands::model_gateway` (launcher crate). A key
        // written and never read is a promise with no mechanism; this pins the
        // string both sides use.
        let Some(repo) = repo_root() else { return };
        let reader = repo.join("launcher/src-tauri/src/commands/model_gateway.rs");
        if !reader.is_file() {
            return;
        }
        let src = std::fs::read_to_string(reader).unwrap();
        assert!(
            src.contains(APP_STATE_KEY_CONDITION),
            "nothing in the launcher reads `{}` — either wire the reader or stop \
             writing the row",
            APP_STATE_KEY_CONDITION
        );
    }

    #[test]
    fn soft_db_write_survives_a_poisoned_mutex() {
        // The act: a panicking write is contained, and the caller continues.
        soft_db_write("a panicking write", || panic!("db mutex poisoned"));
        // The leave-alone: an ordinary error is reported, not escalated.
        soft_db_write("a failing write", || Err("no such table".to_string()));
        // Reaching here at all is the assertion: neither unwound.
    }
}
