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
//! canonical infra service and restarts it through the SAME GPU-aware
//! start path the launcher uses (the `launch-claude-mcp-stack.sh`
//! wrapper, with a launcher-faithful direct-compose fallback) when it is
//! DOWN — but only when VCO actually manages it and the user hasn't
//! deliberately paused or adopted it, and only when the user's global
//! auto-restart toggle is still enabled.
//!
//! ## Guard rails (the watchdog must never fight a deliberate decision)
//!
//! A tick is SKIPPED entirely when the user disabled auto-restart
//! (`launcher.services_watcher_enabled == false` in launcher.db — the
//! SAME toggle the launcher's own services watcher honors, so one
//! user-facing switch disables BOTH restarters; see [`watcher_enabled`]).
//!
//! Within an enabled tick, a service is restarted ONLY when ALL hold (see
//! [`service_eligible_for_restart`]):
//!   1. the container is DOWN (authoritatively not-running — a transient
//!      probe error SKIPS the service this tick rather than treating it as
//!      down, see [`ContainerProbe`]; this mirrors the launcher watcher's
//!      restart-storm guard), AND
//!   2. VCO MANAGES it — its launcher.db `service_endpoints` row is
//!      `vco_managed` and `enabled` (or there is no row yet: VCO's own
//!      stack). An `adopted_container` / `adopted_external` row is someone
//!      else's service and is NEVER touched; a stop by its owner is
//!      respected (see [`supervision_for`]). Until v0.2.97 this read
//!      `services.toml` and healed only `Unresolved` — inert on every
//!      machine where install.py had written `adopt` for VCO's OWN
//!      services, AND
//!   3. the service is NOT paused — no marker file at
//!      `<vct_root>/state/watchdog-paused/<service>` (the SHARED marker in
//!      `vct_launcher_core::services::watchdog_pause`, PRODUCED by the
//!      launcher's `service_stop` / `services_stop_all` commands and
//!      CONSUMED here). The pause marker is the explicit "stop restarting
//!      this" signal a deliberate stop drops so the watchdog backs off; a
//!      RAW external stop with no marker DOES get restarted (that is the
//!      desired self-healing behavior), AND
//!   4. the service is actually PART of this install — the GPU-only
//!      `code_embed` service legitimately does not exist on a CPU-only /
//!      `CODE_EMBED_BACKEND=ollama` host (`profiles: [gpu]` in the compose
//!      file), so the watchdog must never try to build/start it there (see
//!      [`code_embed_in_stack`]), AND
//!   5. the service has not exhausted its crash-loop budget
//!      (see [`Backoff`]).
//!
//! ## GPU-aware restart (BLOCKER-2 remediation)
//!
//! Infra containers `ollama` / `code_embed` need the GPU overlay
//! (`docker-compose.gpu.yml` / `podman-compose.gpu.yml`), `--profile gpu`,
//! and a CDI-readiness wait on NVIDIA+podman hosts. A raw
//! `compose -f docker-compose.yml up -d <svc>` (the pre-remediation
//! behavior) omits all three → ollama/code_embed heal CPU-only or fail
//! with `unresolvable CDI devices`. The watchdog therefore PREFERS the
//! same `launch-claude-mcp-stack.sh` wrapper the launcher prefers (it owns
//! runtime detection + NVIDIA probe + CDI-wait + overlay/profile/override
//! selection and is idempotent — `compose up -d` no-ops already-running
//! containers), handing it the ONE service to heal as `VCO_COMPOSE_SERVICES`
//! (v0.2.97). When the wrapper is not shipped, it falls back to direct
//! compose with the `up` argv from the one rule
//! (`vco_lib.service_lifecycle.compose_up_args`: `--no-deps`, `--profile
//! gpu` for code_embed) — see [`restart_service`].
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
//! NOTE (NIT-7): because the watchdog gates attempts by counting fixed
//! ticks (it does NOT sleep a variable amount), the backoff schedule is a
//! tick-quantized LOWER BOUND on the wait between attempts — the actual
//! wait is rounded UP to the next whole multiple of the tick interval. At
//! the default 45s interval the schedule lands exactly on tick
//! boundaries; a custom interval that doesn't evenly divide a delay just
//! means the Nth attempt fires on the first tick at or after the computed
//! delay.
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
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use tokio::process::Command;

use vct_launcher_core::db::models::ProjectHost;
use vct_launcher_core::process::CommandExt as _;
use vct_launcher_core::db::Db;
use vct_launcher_core::services::service_endpoints::{
    is_compose_managed, lifecycle_container, machine_row, zombie_action, CoreService, ZombieAction,
};
use vct_launcher_core::services::runtime::{detect_runtime_detailed, RuntimeDetection, RuntimeInfo};
use vct_launcher_core::services::watchdog_pause;

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
/// `canonical_services()` (launcher) and the compose file's
/// `container_name:` fields.
///
/// v0.2.92 (WP-12): NOT in lockstep with `canonical_service_skeletons()`
/// (hub `lifecycle_api`) any more — that list gained `model_gateway`, which
/// is a process rather than a `vco_*` container. This one must stay
/// containers-only, because every name in it can reach `compose up <name>`.
/// Pinned by `watchdog_never_supervises_the_model_gateway_process`.
///
/// v0.2.95 (R5c): the gateway IS supervised now — by
/// [`crate::gateway_watchdog`], a sibling task that probes `/health` and
/// heals through `python -m vco_lib.gateway_ensure`. It is a separate module
/// precisely so this list keeps its one-sentence meaning. "Absent from this
/// list" therefore means "healed by compose is the wrong verb for it", not
/// "nothing looks after it".
///
/// `(compose_service_name, container_name)`.
pub const CANONICAL_INFRA_SERVICES: [(&str, &str); 3] = [
    ("weaviate", "vco_weaviate"),
    ("ollama", "vco_ollama"),
    ("code_embed", "vco_code_embed"),
];

/// The GPU-only service — gated behind `profiles: [gpu]` in the compose
/// file. On a CPU-only / `CODE_EMBED_BACKEND=ollama` host the
/// `vco_code_embed` container legitimately does NOT exist, and the
/// watchdog must NEVER try to build/start it there (a stray `up code_embed`
/// would trigger a multi-GB CodeSage image build on a CPU box). See
/// [`code_embed_in_stack`].
pub const GPU_ONLY_SERVICE: &str = "code_embed";

/// app_state key (in launcher.db) that gates the LAUNCHER's own services
/// watcher (`launcher/src-tauri/src/services/watcher.rs`,
/// `APP_STATE_KEY_WATCHER_ENABLED`). The hub watchdog reads the SAME key so
/// ONE user-facing toggle (Preferences → Services) disables BOTH
/// restarters — otherwise a user who turned auto-restart OFF would still
/// get restarts from the hub, and the two restarters would race with
/// different give-up budgets (CONCERN-4). Default ENABLED when the row is
/// absent (matches the launcher watcher's default).
pub const APP_STATE_KEY_WATCHER_ENABLED: &str = "launcher.services_watcher_enabled";

/// Env vars naming the orchestrator install root, used as a fallback when
/// the launcher.db orchestrator-root row is absent/stale (CONCERN-5). These
/// are the SAME vars `templates/hooks/ensure-containers.sh` consults
/// (`VCT_ORCHESTRATOR_ROOT` first, then `VCT_INSTALL_ROOT`). The
/// `infrastructure/docker-compose.yml` existence guard still applies.
pub const ENV_ORCHESTRATOR_ROOT: &str = "VCT_ORCHESTRATOR_ROOT";
pub const ENV_INSTALL_ROOT: &str = "VCT_INSTALL_ROOT";

/// Env override for `CODE_EMBED_BACKEND`. When `ollama` (case-insensitive)
/// the CodeSage GPU service is NOT part of the stack — the host uses the
/// Ollama CPU-embedding fallback and `vco_code_embed` is never built. Any
/// other value (or unset) leaves the GPU-profile decision to the
/// runtime/overlay signals (see [`code_embed_in_stack`]).
pub const ENV_CODE_EMBED_BACKEND: &str = "CODE_EMBED_BACKEND";

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
    parse_interval_with(raw, DEFAULT_INTERVAL_SECS, MIN_INTERVAL_SECS)
}

/// The same parse with the default and the floor as arguments.
///
/// v0.2.95: extracted so [`crate::gateway_watchdog`] — which ticks faster
/// than the container watchdog and therefore has its own default — reads its
/// env var through THIS function rather than growing a second copy of
/// "unset means default, too small means floor, garbage means default". The
/// two watchdogs disagree about the numbers, never about the rule.
pub fn parse_interval_with(raw: Option<&str>, default_secs: u64, floor_secs: u64) -> u64 {
    match raw.and_then(|v| v.trim().parse::<u64>().ok()) {
        Some(n) if n >= floor_secs => n,
        Some(_) => floor_secs, // 0 / too-small → floor (no busy-loop)
        None => default_secs,
    }
}

// ─── Pure decision logic ────────────────────────────────────────────────

/// THE core decision: should this service be restarted on this tick?
///
/// Restart iff DOWN **and** compose-managed by VCO **and** not paused. An
/// adopted service is its owner's; a running container needs nothing; a
/// paused service was explicitly told to stay down.
///
/// The crash-loop budget is applied separately by the caller (it depends
/// on mutable per-service state, not on this tick's observation).
pub fn service_eligible_for_restart(running: bool, compose_managed: bool, paused: bool) -> bool {
    !running && !paused && compose_managed
}

/// What the watchdog may do for one service, read from its launcher.db
/// `service_endpoints` row (the ONE machine resolver).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Supervision {
    /// VCO's compose owns the container (`vco_managed` + `enabled`, or no
    /// row) — the only case the watchdog may heal.
    pub compose_managed: bool,
    /// The container to probe: the row's name, else the compose default.
    pub container: String,
    /// R7a F3: a row positively says VCO's compose owns this container
    /// ([`ZombieAction::Recreate`]). Without a row the ownership is UNKNOWN
    /// (the container may be the legacy compose project's, on a bind the
    /// installer's compose does not mount): the heal is a `start` BY NAME,
    /// and compose only CREATES a container that does not exist — never a
    /// compose `up` against an existing one (which re-creates it on the
    /// installer's config when that differs).
    pub recreate_ok: bool,
}

/// [`Supervision`] for `service` on this machine. `default_container` is
/// the compose file's name for it (from [`CANONICAL_INFRA_SERVICES`]).
pub fn supervision_for(db: &Db, service: &str, default_container: &str) -> Supervision {
    let svc = CoreService::from_name(service);
    let row = svc.and_then(|s| machine_row(db, s));
    Supervision {
        compose_managed: is_compose_managed(row.as_ref()),
        container: svc
            .and_then(|s| lifecycle_container(s, row.as_ref()))
            .unwrap_or_else(|| default_container.to_string()),
        recreate_ok: zombie_action(row.as_ref()) == ZombieAction::Recreate,
    }
}

/// Did `<runtime> start` fail because the container does not exist? (The
/// same authoritative wording [`classify_probe`] recognises.)
pub fn start_failed_as_missing(stderr: &str) -> bool {
    let lc = stderr.to_lowercase();
    lc.contains("no such container") || lc.contains("no such object") || lc.contains("not found")
}

/// Heal a service whose ownership is UNKNOWN (no row): `start` the existing
/// container BY NAME; only a container that does not exist is created
/// through compose (nothing exists to lose). Never an `up` against an
/// existing container.
async fn heal_by_name(
    runtime: &RuntimeInfo,
    infra_dir: &Path,
    service: &str,
    container: &str,
) -> Result<(), String> {
    let output = Command::new(&runtime.binary_path)
        .silent()
        .args(["start", container])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("spawn {} start: {}", runtime.runtime.display_name(), e))?;
    if output.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    if start_failed_as_missing(&stderr) {
        return restart_service(runtime, infra_dir, service).await;
    }
    Err(format!(
        "{} start {} failed (no service_endpoints row: VCO starts it by name only): {}",
        runtime.runtime.display_name(),
        container,
        stderr.trim()
    ))
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

// ─── Pause-marker mechanism (BLOCKER-1: shared consumer) ─────────────────
//
// The pause marker is a FILE under `<vct_root>/state/watchdog-paused/` —
// chosen over a launcher.db row because the watchdog must work with the
// launcher GUI closed (no DB write-contention with the launcher's WAL
// handle) and the signal lives next to `services.toml` under `<vct_root>`.
//
// The path logic now lives in the SHARED
// `vct_launcher_core::services::watchdog_pause` so the PRODUCER (the
// launcher's `service_stop` / `services_stop_all` commands) and this
// CONSUMER resolve the identical path. Before the BLOCKER-1 fix the marker
// had a consumer here but NO producer — a deliberate stop dropped no marker
// and got restarted within one tick. The launcher now creates the marker on
// a successful deliberate stop and removes it on a deliberate start.

/// Is `service` currently paused (shared marker file present)?
/// Thin delegation to the shared helper so consumer + producer agree.
pub fn is_service_paused(service: &str) -> bool {
    watchdog_pause::is_service_paused(service)
}

/// Path to the pause marker for `service` (for log messages). Delegates to
/// the shared helper.
pub fn pause_marker_path(service: &str) -> PathBuf {
    watchdog_pause::pause_marker_path(service)
}

// ─── code_embed (GPU-only) stack-membership gate (BLOCKER-2) ─────────────

/// Should the watchdog supervise `code_embed` on THIS host?
///
/// `code_embed` (the CodeSage GPU embedding service) is gated behind
/// `profiles: [gpu]` in `infrastructure/docker-compose.yml`. On a CPU-only
/// or `CODE_EMBED_BACKEND=ollama` host the `vco_code_embed` container
/// legitimately does NOT exist — the host uses the Ollama CPU-embedding
/// fallback. A blind `compose up code_embed` there would (a) try to BUILD
/// the multi-GB CodeSage image on a CPU box and (b) fail / waste resources.
/// So the watchdog must only target `code_embed` when the install actually
/// includes it.
///
/// Resolution (any one ⇒ excluded):
///   1. `CODE_EMBED_BACKEND=ollama` (case-insensitive) — explicit CPU
///      fallback. This is the authoritative "not in stack" signal install.py
///      writes to `.env` on a CPU-only host.
///
/// Otherwise: included. We deliberately DEFAULT-INCLUDE when the backend is
/// unset / `gpu`, because the restart path is the GPU-aware wrapper which
/// itself probes for NVIDIA + CDI and degrades to a CPU-only `up -d` WITHOUT
/// the gpu profile when no GPU is present — i.e. on a GPU-less host the
/// wrapper never enables the gpu profile, so `code_embed` is never built
/// even if we "target" it. The hard exclusion above is the belt; the
/// wrapper's own GPU probe is the suspenders. The container-probe also
/// returns `false` for a never-created `vco_code_embed`, but step 1 stops us
/// BEFORE we ever issue a build on an explicitly-CPU host.
pub fn code_embed_in_stack() -> bool {
    // Excluded iff CODE_EMBED_BACKEND is explicitly `ollama`
    // (case-insensitive, trimmed). Unset / `gpu` / anything else → included.
    !std::env::var(ENV_CODE_EMBED_BACKEND)
        .map(|v| v.trim().eq_ignore_ascii_case("ollama"))
        .unwrap_or(false)
}

/// True when `service` should be supervised on this host. Always true for
/// `weaviate` / `ollama`; for the GPU-only `code_embed` it defers to
/// [`code_embed_in_stack`].
pub fn service_in_stack(service: &str) -> bool {
    if service == GPU_ONLY_SERVICE {
        code_embed_in_stack()
    } else {
        true
    }
}

// ─── Orchestrator-clone / compose-file resolution (CONCERN-5) ────────────

/// Locate `<orchestrator_clone>/infrastructure` so we can run compose
/// against `docker-compose.yml`. The hub has no `current_exe()`-walk
/// resolver (that lives launcher-side in `commands::installer`), so we
/// resolve via two sources, in order, keeping the compose-file existence
/// guard on each:
///   1. The orchestrator-root project's `folder_path` from launcher.db —
///      the row the launcher seeds at install (read poison-tolerantly via
///      `list_projects_nonpanicking`, see CONCERN-6).
///   2. CONCERN-5 fallback: the `VCT_ORCHESTRATOR_ROOT` / `VCT_INSTALL_ROOT`
///      env vars (the SAME vars `ensure-containers.sh` uses) when the DB
///      row is absent/stale.
///
/// Returns `None` (soft) when neither source yields a directory containing
/// `docker-compose.yml` — a stale/renamed clone shouldn't make us spawn a
/// doomed `compose up` every tick.
pub fn infrastructure_dir(db: &LauncherDbHandle) -> Option<PathBuf> {
    // 1. launcher.db orchestrator-root row (non-panicking read).
    if let Ok(rows) = db.0.list_projects_nonpanicking() {
        if let Some(root) = rows
            .into_iter()
            .find(|p| p.host == ProjectHost::OrchestratorRoot)
        {
            if let Some(dir) = infrastructure_dir_from_root(Path::new(&root.folder_path)) {
                return Some(dir);
            }
        }
    }

    // 2. CONCERN-5 env fallback: VCT_ORCHESTRATOR_ROOT → VCT_INSTALL_ROOT.
    for env_key in [ENV_ORCHESTRATOR_ROOT, ENV_INSTALL_ROOT] {
        if let Ok(root) = std::env::var(env_key) {
            let root = root.trim();
            if !root.is_empty() {
                if let Some(dir) = infrastructure_dir_from_root(Path::new(root)) {
                    return Some(dir);
                }
            }
        }
    }

    None
}

/// Given an orchestrator root, return `<root>/infrastructure` IFF it
/// contains `docker-compose.yml`. Pure (filesystem stat only) so the
/// resolution priority in [`infrastructure_dir`] is unit-testable.
pub fn infrastructure_dir_from_root(root: &Path) -> Option<PathBuf> {
    let dir = root.join("infrastructure");
    if dir.join("docker-compose.yml").is_file() {
        Some(dir)
    } else {
        None
    }
}

// ─── Container status probe (CONCERN-3: probe-error storm guard) ─────────

/// Tri-state result of probing a container's status.
///
/// The pre-remediation probe collapsed EVERY failure (spawn error,
/// timeout, ambiguous output, AND an authoritative "no such container") to
/// a single `false` = "down". That meant a transient runtime hiccup (the
/// daemon momentarily unreachable) read all three infra containers as down
/// in one tick → a spurious restart storm. This tri-state distinguishes:
///
/// - `Running` — `.State.Status == running`.
/// - `NotRunning` — AUTHORITATIVELY down: inspect succeeded and reported
///   `exited` / `created` / `dead` / `paused` / `stopped`, OR reported
///   "no such container" (the container genuinely doesn't exist). Only this
///   state is restart-eligible.
/// - `ProbeError` — spawn failure, timeout, or ambiguous/empty output we
///   can't trust. The watchdog SKIPS the service this tick (no restart) —
///   mirroring the launcher watcher's storm guard in `services/watcher.rs`
///   (it skips the whole tick on a failed status probe).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ContainerProbe {
    Running,
    NotRunning,
    ProbeError,
}

/// Timeout for a single `inspect` probe. A hung daemon socket must not
/// stall the tick; on timeout we report `ProbeError` (skip), not `down`.
const PROBE_TIMEOUT: Duration = Duration::from_secs(5);

/// Probe the named container's status via `<runtime> inspect --format
/// {{.State.Status}}`, returning a [`ContainerProbe`]. Never panics.
///
/// "No such container" is detected from inspect's stderr (it exits
/// non-zero for a missing container) and mapped to AUTHORITATIVE
/// `NotRunning` — a never-created container IS legitimately down and
/// restart-eligible (subject to all the other gates). Every OTHER non-zero
/// exit, spawn error, or timeout maps to `ProbeError` (skip).
pub async fn probe_container(runtime: &RuntimeInfo, container_name: &str) -> ContainerProbe {
    let fut = Command::new(&runtime.binary_path)
        .silent()
        .args(["inspect", "--format", "{{.State.Status}}", container_name])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output();
    let output = match tokio::time::timeout(PROBE_TIMEOUT, fut).await {
        Ok(Ok(out)) => out,
        // Timeout → ambiguous, do NOT treat as down.
        Err(_) => return ContainerProbe::ProbeError,
        // Spawn error → ambiguous (runtime momentarily gone), do NOT treat
        // as down.
        Ok(Err(_)) => return ContainerProbe::ProbeError,
    };
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    classify_probe(output.status.success(), &stdout, &stderr)
}

/// Pure classifier for the inspect probe — extracted so the tri-state
/// decision is unit-testable without spawning a runtime.
///
/// `success` = inspect's exit status was zero; `stdout` = the
/// `.State.Status` line; `stderr` = inspect's stderr (used to recognize
/// the authoritative "no such container").
pub fn classify_probe(success: bool, stdout: &str, stderr: &str) -> ContainerProbe {
    if success {
        let status = stdout.trim();
        return match status {
            "running" => ContainerProbe::Running,
            // Authoritative not-running states from the OCI/podman/docker
            // state machine.
            "exited" | "created" | "dead" | "paused" | "stopped" | "removing" | "restarting" => {
                ContainerProbe::NotRunning
            }
            // Empty or any unrecognized token: ambiguous, skip rather than
            // risk a spurious restart on a runtime we don't understand.
            _ => ContainerProbe::ProbeError,
        };
    }
    // Non-zero exit. The one authoritative case is "no such container"
    // (the container genuinely doesn't exist → down + restart-eligible).
    let lc = stderr.to_lowercase();
    if lc.contains("no such container")
        || lc.contains("no such object")
        || lc.contains("not found")
    {
        return ContainerProbe::NotRunning;
    }
    // Any other non-zero exit (permission denied, daemon error, …) is
    // ambiguous — skip this tick.
    ContainerProbe::ProbeError
}

/// Back-compat pure parser retained for callers/tests: `<runtime> inspect
/// --format {{.State.Status}}` prints exactly `running` (plus a trailing
/// newline) for a live container.
pub fn parse_running_status(stdout: &str) -> bool {
    stdout.trim() == "running"
}

// ─── Restart action (BLOCKER-2: GPU-aware, via the launcher's path) ──────

/// Locate the `launch-claude-mcp-stack` wrapper relative to the
/// orchestrator clone (the `infra_dir`'s parent is the orchestrator root).
/// Mirrors the launcher's `find_stack_wrapper`: `.sh` on Linux/macOS,
/// `.ps1` on Windows; `None` when the wrapper isn't shipped (minimal
/// install) so the caller falls back to direct compose.
pub fn find_stack_wrapper(infra_dir: &Path) -> Option<PathBuf> {
    let root = infra_dir.parent()?;
    let script_name = if cfg!(target_os = "windows") {
        "launch-claude-mcp-stack.ps1"
    } else {
        "launch-claude-mcp-stack.sh"
    };
    let candidate = root.join("scripts").join(script_name);
    if candidate.is_file() {
        Some(candidate)
    } else {
        None
    }
}

/// Run the `launch-claude-mcp-stack` wrapper for ONE service.
///
/// This is the SAME tested GPU-aware path the launcher prefers: the
/// wrapper owns runtime detection, the NVIDIA probe, the CDI-readiness
/// wait, and the overlay/profile/override selection. The service list
/// travels as `VCO_COMPOSE_SERVICES` (v0.2.97): compose is never invoked
/// without an explicit list, so healing one VCO service can never create
/// compose copies of adopted ones (plan invariant I1). On a GPU-less host
/// the wrapper never enables the gpu profile, so `vco_code_embed` is never
/// built there.
///
/// Cross-OS dispatch mirrors the launcher's `run_stack_wrapper`:
///   - Linux/macOS: `bash <script>` (no reliance on the exec bit, which a
///     clone on a noexec partition may have lost).
///   - Windows: `powershell -NoProfile -ExecutionPolicy Bypass -File
///     <script>`.
///
/// We point `VCT_STACK_WORKING_DIR` at `infra_dir` so the wrapper finds the
/// compose file even if its own env-based resolution would land elsewhere,
/// and inherit `VCT_ORCHESTRATOR_ROOT` from `infra_dir`'s parent so
/// runtime.txt resolution matches.
async fn run_stack_wrapper(wrapper: &Path, infra_dir: &Path, service: &str) -> Result<(), String> {
    // `CommandExt::silent` takes ownership (returns Self), so build the
    // base command silent first, then chain args by &mut.
    let mut cmd = if cfg!(target_os = "windows") {
        let mut c = Command::new("powershell").silent();
        c.args([
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            wrapper.to_str().ok_or("non-UTF8 wrapper path")?,
        ]);
        c
    } else {
        let mut c = Command::new("bash").silent();
        c.arg(wrapper);
        c
    };
    // Point the wrapper at our resolved compose dir + orchestrator root so
    // its compose-file + runtime.txt resolution is deterministic.
    cmd.env("VCT_STACK_WORKING_DIR", infra_dir);
    cmd.env(ENV_COMPOSE_SERVICES, service);
    if let Some(root) = infra_dir.parent() {
        cmd.env(ENV_ORCHESTRATOR_ROOT, root);
        cmd.current_dir(root);
    }
    let output = cmd
        .output()
        .await
        .map_err(|e| format!("spawn launch-claude-mcp-stack wrapper: {}", e))?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "launch-claude-mcp-stack wrapper failed (status {}): {}",
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        ))
    }
}

/// Env var carrying the explicit compose service list to the
/// `launch-claude-mcp-stack` wrapper (space-separated). MUST MATCH the
/// wrapper's reader (`scripts/launch-claude-mcp-stack.{sh,ps1}`).
pub const ENV_COMPOSE_SERVICES: &str = "VCO_COMPOSE_SERVICES";

/// Build the direct-compose fallback argv (WITHOUT the leading compose
/// binary — that comes from `runtime.compose_command()`).
///
/// This is the wrapper-absent path: the `-f` chain, then `up_args` — the
/// `up` argv for the ONE service being healed from the ONE rule
/// (`vco_lib.service_lifecycle.compose_up_args` via
/// `vct_launcher_core::services::compose_args`: `--no-deps`, `--profile gpu`
/// for code_embed). Never a bare `up -d`, which would also create compose
/// copies of adopted services (plan invariant I1), and never a hand-built
/// `up -d <svc>`, which would pull code_embed's `depends_on: ollama` in.
/// NIT-8: when the user's `docker-compose.override.yml` exists in
/// `infra_dir` we add it explicitly with `-f` so the override isn't
/// dropped (compose's implicit auto-load is bypassed once we pass an
/// explicit `-f docker-compose.yml`). Pure → unit-testable.
pub fn build_fallback_compose_args(infra_dir: &Path, up_args: &[String]) -> Vec<String> {
    let mut args: Vec<String> = vec!["-f".into(), "docker-compose.yml".into()];
    // NIT-8: preserve a user override the same way the boot wrapper does.
    let override_path = infra_dir.join("docker-compose.override.yml");
    if override_path.is_file() {
        args.push("-f".into());
        args.push("docker-compose.override.yml".into());
    }
    args.extend(up_args.iter().cloned());
    args
}

/// Direct-compose fallback (wrapper not shipped): the rule's `up` argv for
/// `service`. The rule running is required — failing to compute the argv is
/// an error recorded into the backoff, never a hand-built fallback.
async fn restart_via_direct_compose(
    runtime: &RuntimeInfo,
    infra_dir: &Path,
    service: &str,
) -> Result<(), String> {
    let root = infra_dir.parent().ok_or("infrastructure dir has no parent")?;
    let python = vct_launcher_core::services::compose_args::rule_python()?;
    let up_args =
        vct_launcher_core::services::compose_args::compose_up_args(&python, root, &[service], false).await?;
    if up_args.is_empty() {
        return Ok(());
    }
    let mut cmd = runtime.compose_command();
    cmd.args(build_fallback_compose_args(infra_dir, &up_args));
    cmd.current_dir(infra_dir);
    let output = cmd
        .output()
        .await
        .map_err(|e| format!("spawn {} compose: {}", runtime.runtime.display_name(), e))?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "{} compose up -d failed (status {}): {}",
            runtime.runtime.display_name(),
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        ))
    }
}

/// Heal `service` through the SAME GPU-aware path the launcher uses:
/// PREFER the `launch-claude-mcp-stack` wrapper (GPU overlay +
/// `--profile gpu` + CDI-wait) with `service` as its explicit list; fall
/// back to a direct `compose up -d <service>` only when the wrapper isn't
/// shipped or fails. Returns `Ok(())` on success, `Err(msg)` otherwise
/// (caller records into [`Backoff`]). Soft — never panics.
async fn restart_service(
    runtime: &RuntimeInfo,
    infra_dir: &Path,
    service: &str,
) -> Result<(), String> {
    if let Some(wrapper) = find_stack_wrapper(infra_dir) {
        match run_stack_wrapper(&wrapper, infra_dir, service).await {
            Ok(()) => return Ok(()),
            Err(e) => {
                tracing::warn!(
                    service,
                    error = %e,
                    "[vct-hub] infra watchdog: launch-claude-mcp-stack wrapper \
                     failed while healing (falling back to direct compose)"
                );
                // Fall through to direct compose.
            }
        }
    }
    restart_via_direct_compose(runtime, infra_dir, service).await
}

// ─── Spawn + tick loop ───────────────────────────────────────────────────

/// Spawn the infra watchdog as a detached `tokio` task. Called once from
/// `server.rs::start_hub_server()`. When disabled via the opt-out env,
/// logs that fact and spawns nothing.
pub fn spawn_infra_watchdog(db: LauncherDbHandle) {
    let config = WatchdogConfig::from_env();
    if !config.enabled {
        tracing::info!(
            "[vct-hub] infra watchdog DISABLED via {}=0; infra containers \
             will NOT be auto-restarted by the hub.",
            ENV_ENABLED
        );
        return;
    }
    tracing::info!(
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

/// CONCERN-4: read the SHARED auto-restart toggle from launcher.db
/// (`launcher.services_watcher_enabled`). Returns `true` when the row is
/// absent (the launcher watcher's documented default) and on any read
/// error (fail-OPEN: a transient DB read failure must not silently disable
/// the safety net — but a poisoned mutex is recovered, not panicked on,
/// via the non-panicking accessor). When the user explicitly set the
/// toggle to `false`, BOTH the launcher watcher AND this hub watchdog stand
/// down — one switch, both restarters.
pub fn watcher_enabled(db: &LauncherDbHandle) -> bool {
    match db.0.app_state_get_bool_nonpanicking(APP_STATE_KEY_WATCHER_ENABLED) {
        Ok(Some(v)) => v,
        // Row absent → default ENABLED (matches the launcher watcher).
        Ok(None) => true,
        // Read error → fail-open (don't silently disable the safety net).
        Err(e) => {
            tracing::warn!(
                error = %e,
                "[vct-hub] infra watchdog: could not read {}; assuming ENABLED \
                 (fail-open).",
                APP_STATE_KEY_WATCHER_ENABLED
            );
            true
        }
    }
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

        // CONCERN-4: honor the user's auto-restart toggle. When disabled,
        // skip the whole tick (no probes, no restarts) so the hub watchdog
        // and the launcher's own watcher are governed by ONE switch and
        // never race with different give-up budgets.
        if !watcher_enabled(&db) {
            continue;
        }

        // One tick is fully soft-failed inside (CONCERN-6): every DB read
        // the tick performs goes through a poison-TOLERANT accessor
        // (`list_projects_nonpanicking` / `app_state_get_bool_nonpanicking`
        // / `lock_recover`) rather than `lock().expect("db mutex
        // poisoned")`, so a poisoned launcher.db mutex no longer kills this
        // detached task. The row reads go through `lock_recover` and
        // `detect_runtime_detailed()` is already non-panicking. The result is that the watchdog upholds
        // its "never crashes the hub" contract: a single bad tick logs and
        // the next tick tries again.
        run_one_tick(&db, base, &mut backoffs, &mut ticks_since_attempt).await;
    }
}

/// The last pin refusal the watchdog logged at `warn` — so a refusal that
/// holds tick after tick is said once, and again only when it changes.
static LAST_REFUSAL: std::sync::Mutex<Option<String>> = std::sync::Mutex::new(None);

/// The watchdog's line for a tick with no runtime to drive (v0.2.97 R11 L6),
/// and whether it is news (`warn`): a refused pin names the pin and why the
/// stale record was not switched ([`RuntimeDetection::refusal`]) — before, it
/// was a `debug` line without either. `last` holds the refusal last said.
pub(crate) fn no_runtime_line(
    detection: &RuntimeDetection,
    last: &std::sync::Mutex<Option<String>>,
) -> (bool, String) {
    let Some(refusal) = detection.refusal.as_deref() else {
        return (
            false,
            "[vct-hub] infra watchdog: no container runtime reachable this tick; will retry \
             next interval."
                .to_string(),
        );
    };
    let line = format!("[vct-hub] infra watchdog: supervising nothing — {refusal}");
    let mut g = last.lock().unwrap_or_else(|p| p.into_inner());
    let news = g.as_deref() != Some(refusal);
    if news {
        *g = Some(refusal.to_string());
    }
    (news, line)
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
    let detection = detect_runtime_detailed().await;
    let runtime = match detection.info.clone() {
        Some(r) => r,
        None => {
            // Nothing the watchdog can drive. A REFUSED PIN is said once
            // (warn) with WHY — the same refusal the launcher shows (R11 L6)
            // — and again only when it changes; a runtime-less host stays
            // quiet (debug) so logs don't fill.
            match no_runtime_line(&detection, &LAST_REFUSAL) {
                (true, line) => tracing::warn!("{}", line),
                (false, line) => tracing::debug!("{}", line),
            }
            return;
        }
    };

    // Resolve the compose dir once per tick.
    let infra_dir = match infrastructure_dir(db) {
        Some(d) => d,
        None => {
            tracing::debug!(
                "[vct-hub] infra watchdog: cannot locate the orchestrator clone's \
                 infrastructure/docker-compose.yml (no orchestrator-root project \
                 row, or its folder is gone); skipping this tick."
            );
            return;
        }
    };

    for (service, default_container) in CANONICAL_INFRA_SERVICES.iter() {
        let service = *service;
        // The row decides whether VCO may touch this service at all, and
        // which container is its (v0.2.97). An adopted service is left
        // alone BEFORE any probe — its owner's stop is respected.
        let supervision = supervision_for(&db.0, service, default_container);
        if !supervision.compose_managed {
            continue;
        }
        let container = supervision.container.as_str();

        // BLOCKER-2 gate: don't supervise a service that isn't part of THIS
        // install. The GPU-only `code_embed` legitimately doesn't exist on a
        // CPU-only / `CODE_EMBED_BACKEND=ollama` host; targeting it there
        // would trigger a multi-GB image build. (Quiet — normal on CPU
        // hosts.)
        if !service_in_stack(service) {
            continue;
        }

        // CONCERN-3: tri-state probe. A transient daemon hiccup
        // (ProbeError) SKIPS the service this tick rather than treating it
        // as down — avoiding a restart storm after probe errors. Only an
        // AUTHORITATIVE not-running reading is restart-eligible.
        let probe = probe_container(&runtime, container).await;
        match probe {
            ContainerProbe::Running => {
                // Observing the service UP resets its crash-loop state
                // (covers external recovery: a user / hook restarted it).
                if let Some(b) = backoffs.get_mut(service) {
                    if b.consecutive_failures > 0 || b.gave_up {
                        b.reset_on_success();
                    }
                }
                ticks_since_attempt.remove(service);
                continue;
            }
            ContainerProbe::ProbeError => {
                // Ambiguous reading — do NOT count it as down, do NOT
                // touch backoff/tick state. Skip and re-probe next tick.
                continue;
            }
            ContainerProbe::NotRunning => {
                // Fall through to the eligibility + backoff logic below.
            }
        }

        let paused = is_service_paused(service);

        // `running == false` here (authoritative NotRunning above).
        if !service_eligible_for_restart(false, supervision.compose_managed, paused) {
            // Down, but deliberately paused. Leave it alone.
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
        tracing::warn!(
            service,
            container,
            consecutive_failures = backoff.consecutive_failures,
            "[vct-hub] infra watchdog: service is DOWN and VCO-managed; \
             attempting restart."
        );
        let heal = if supervision.recreate_ok {
            restart_service(&runtime, &infra_dir, service).await
        } else {
            heal_by_name(&runtime, &infra_dir, service, container).await
        };
        match heal {
            Ok(()) => {
                tracing::info!(
                    service,
                    container,
                    "[vct-hub] infra watchdog: restart issued successfully."
                );
                backoff.reset_on_success();
                ticks_since_attempt.remove(service);
            }
            Err(e) => {
                backoff.record_failure();
                if backoff.is_given_up() {
                    tracing::error!(
                        service,
                        container,
                        consecutive_failures = backoff.consecutive_failures,
                        error = %e,
                        pause_marker = %pause_marker_path(service).display(),
                        "[vct-hub] infra watchdog: GIVING UP after repeated failed \
                         restarts. The hub will NOT retry until the service is seen \
                         running again (start it manually, or `touch` the pause \
                         marker to silence the watchdog for it)."
                    );
                } else {
                    tracing::warn!(
                        service,
                        container,
                        attempt = backoff.consecutive_failures,
                        max_attempts = MAX_CONSECUTIVE_FAILURES,
                        error = %e,
                        "[vct-hub] infra watchdog: restart failed. Backing off."
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
    /// R11 L6: a refused pin reaches the watchdog's log WITH why — once at
    /// `warn`, then quietly until it changes; no pin at all stays the quiet
    /// "no container runtime reachable".
    #[test]
    fn a_refused_pin_is_logged_with_why_once() {
        use vct_launcher_core::services::runtime::RuntimeDetection;
        let last = std::sync::Mutex::new(None);
        let refused = RuntimeDetection {
            info: None,
            not_switched: Some("podman holds none of VCO's data".into()),
            refusal: Some("pinned to docker (not switched: podman holds none of VCO's data)".into()),
        };
        let (news, line) = super::no_runtime_line(&refused, &last);
        assert!(news);
        assert!(line.contains("pinned to docker (not switched: podman holds none of VCO's data)"), "{line}");
        assert!(!super::no_runtime_line(&refused, &last).0, "the same refusal is said once");
        let other = RuntimeDetection { refusal: Some("pinned to podman".into()), ..RuntimeDetection::default() };
        assert!(super::no_runtime_line(&other, &last).0, "a changed refusal is news");
        let (news, line) = super::no_runtime_line(&RuntimeDetection::default(), &last);
        assert!(!news && line.contains("no container runtime reachable"), "{line}");
    }

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
        // The one TRUE case: down + compose-managed + not paused.
        assert!(service_eligible_for_restart(false, true, false));
    }

    #[test]
    fn running_service_never_restarted() {
        for managed in [true, false] {
            assert!(!service_eligible_for_restart(true, managed, false));
        }
    }

    #[test]
    fn paused_service_never_restarted() {
        assert!(!service_eligible_for_restart(false, true, true));
    }

    #[test]
    fn adopted_service_never_restarted() {
        for paused in [true, false] {
            assert!(!service_eligible_for_restart(false, false, paused));
        }
    }

    /// SE-4 red-proof (5): the watchdog's supervision comes from the ROWS.
    /// A `services.toml` saying `adopt` for every service (what install.py
    /// wrote for VCO's OWN services) is present, as on a re-installed
    /// machine. The `vco_managed` code_embed row is healed; the
    /// `adopted_container` Weaviate row and the `adopted_external` Ollama
    /// row are never touched. Red against the pre-v0.2.97 gate, which read
    /// `services.toml` (`adopt` ⇒ nothing is ever healed).
    #[test]
    fn supervision_follows_the_rows_not_services_toml() {
        use vct_launcher_core::db::service_endpoints::{EndpointMode, ServiceEndpointRow};
        let sd = vct_launcher_core::test_env::state_dir_guard();
        std::fs::write(
            sd.path().join("services.toml"),
            "[[services]]\nname = \"weaviate\"\nmode = \"adopt\"\n\n\
             [[services]]\nname = \"ollama\"\nmode = \"adopt\"\n\n\
             [[services]]\nname = \"code_embed\"\nmode = \"adopt\"\n",
        )
        .unwrap();
        let db = Db::open_in_memory().unwrap();
        let mut w = ServiceEndpointRow::new("weaviate", EndpointMode::AdoptedContainer, "localhost", 8081);
        w.grpc_port = Some(50052);
        w.container_name = Some("their_weaviate".into());
        db.service_endpoint_seed_for_tests(&w).unwrap();
        db.service_endpoint_seed_for_tests(&ServiceEndpointRow::new(
            "ollama",
            EndpointMode::AdoptedExternal,
            "localhost",
            11434,
        ))
        .unwrap();
        let mut c = ServiceEndpointRow::new("code_embed", EndpointMode::VcoManaged, "localhost", 11440);
        c.container_name = Some("vco_code_embed".into());
        db.service_endpoint_seed_for_tests(&c).unwrap();

        let weaviate = supervision_for(&db, "weaviate", "vco_weaviate");
        let ollama = supervision_for(&db, "ollama", "vco_ollama");
        let code_embed = supervision_for(&db, "code_embed", "vco_code_embed");
        assert!(!weaviate.compose_managed, "an adopted container is its owner's");
        assert!(!ollama.compose_managed, "an adopted URL has no lifecycle");
        assert!(code_embed.compose_managed, "VCO's own service is healed");
        assert!(service_eligible_for_restart(false, code_embed.compose_managed, false));
        assert!(!service_eligible_for_restart(false, weaviate.compose_managed, false));

        // A disabled vco_managed row (code_embed on a CPU host) is not healed.
        c.enabled = false;
        db.service_endpoint_seed_for_tests(&c).unwrap();
        assert!(!supervision_for(&db, "code_embed", "vco_code_embed").compose_managed);
    }

    /// R7a F3: with NO row the ownership is unknown — the heal may start the
    /// container by name (or create a missing one) but never compose `up`
    /// against an existing one. Red if `recreate_ok` reads "no row" as VCO's.
    #[test]
    fn a_service_without_a_row_is_healed_by_name_only() {
        use vct_launcher_core::db::service_endpoints::{EndpointMode, ServiceEndpointRow};
        // About the absent row itself (no request is made): without this the
        // test harness answers a sentinel row for an empty in-memory DB.
        let _allow = vct_launcher_core::services::service_endpoints::allow_compiled_default_on_this_thread();
        let db = Db::open_in_memory().unwrap();
        let none = supervision_for(&db, "ollama", "vco_ollama");
        assert!(none.compose_managed, "a missing container may still be created");
        assert!(!none.recreate_ok, "no row: never an `up` against an existing container");
        assert_eq!(none.container, "vco_ollama");
        let mut o = ServiceEndpointRow::new("ollama", EndpointMode::VcoManaged, "localhost", 11435);
        o.container_name = Some("vco_ollama".into());
        db.service_endpoint_seed_for_tests(&o).unwrap();
        assert!(supervision_for(&db, "ollama", "vco_ollama").recreate_ok);
        assert!(start_failed_as_missing("Error: no such container vco_ollama"));
        assert!(!start_failed_as_missing("Error: port 11435 is already allocated"));
    }

    /// Owner ruling Q1: the Weaviate "waiting for your choice" row
    /// (`vco_managed`, `enabled = 0`) is never healed — starting it would
    /// duplicate the third-party Weaviate the user is being asked about.
    #[test]
    fn the_waiting_weaviate_row_is_never_healed() {
        use vct_launcher_core::db::service_endpoints::{EndpointMode, ServiceEndpointRow};
        let db = Db::open_in_memory().unwrap();
        let mut w = ServiceEndpointRow::new("weaviate", EndpointMode::VcoManaged, "localhost", 8081);
        w.grpc_port = Some(50052);
        w.enabled = false;
        db.service_endpoint_seed_for_tests(&w).unwrap();
        let s = supervision_for(&db, "weaviate", "vco_weaviate");
        assert!(!s.compose_managed);
        assert!(!service_eligible_for_restart(false, s.compose_managed, false));
    }

    /// The row's container name is what gets probed (a vco_managed row may
    /// name its container; with no name the compose default applies).
    #[test]
    fn supervision_probes_the_rows_container() {
        use vct_launcher_core::db::service_endpoints::{EndpointMode, ServiceEndpointRow};
        let db = Db::open_in_memory().unwrap();
        let mut w = ServiceEndpointRow::new("weaviate", EndpointMode::VcoManaged, "localhost", 18081);
        w.grpc_port = Some(50053);
        w.container_name = Some("vco_weaviate_alt".into());
        db.service_endpoint_seed_for_tests(&w).unwrap();
        assert_eq!(supervision_for(&db, "weaviate", "vco_weaviate").container, "vco_weaviate_alt");
        // No row on a harness DB: the sentinel row (vco_managed, unnamed).
        assert_eq!(supervision_for(&db, "ollama", "vco_ollama").container, "vco_ollama");
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

    /// v0.2.92 (WP-12) LEAVE-ALONE: the model gateway is a PROCESS, and THIS
    /// watchdog only knows how to heal containers.
    ///
    /// `lifecycle_api::canonical_service_skeletons` gained a `model_gateway`
    /// row so the hub's `/services/status` reports it. This list is a
    /// SEPARATE allowlist and must not follow: every name here reaches a
    /// `compose up <service>` invocation, and `compose up model_gateway`
    /// against a compose file that has no such service is at best an error
    /// and at worst a build of something unrelated. The exclusion is
    /// structural — nothing to add — and this test is what keeps it that
    /// way when someone later "syncs the two lists".
    ///
    /// v0.2.95 (R5c) RESTATES rather than relaxes the rule. The gateway now
    /// HAS a hub supervisor — `gateway_watchdog`, which probes `/health` and
    /// heals through `python -m vco_lib.gateway_ensure` — and it is a sibling
    /// module for exactly the reason this assertion exists: the two share no
    /// probe, no heal and no gate, so the only thing a merged list would buy
    /// is a name in an allowlist that can no longer be read as "these reach
    /// compose". The second half of this test pins that the sibling exists,
    /// so "the watchdog does not do it" can never again be read as "nobody
    /// does".
    #[test]
    fn watchdog_never_supervises_the_model_gateway_process() {
        let names: Vec<&str> = CANONICAL_INFRA_SERVICES.iter().map(|(s, _)| *s).collect();
        assert!(
            !names.contains(&"model_gateway"),
            "model_gateway is a process, not a vco_* container; the watchdog \
             must not try to heal it with a compose invocation"
        );
        // The supervision it is excluded FROM has a home. Calling the
        // sibling's decision function is what proves the module is compiled
        // in — a comment naming it would not.
        assert_eq!(
            crate::gateway_watchdog::disposition_for("registered_but_unrunnable"),
            crate::gateway_watchdog::Disposition::Unrunnable,
            "the gateway's own supervisor must exist; this list's exclusion is \
             a division of labour, not an absence of one"
        );
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

    // ----- CONCERN-3: tri-state probe classifier (storm guard) -----

    #[test]
    fn classify_probe_running_only_on_exact_running() {
        assert_eq!(classify_probe(true, "running", ""), ContainerProbe::Running);
        assert_eq!(classify_probe(true, "running\n", ""), ContainerProbe::Running);
        assert_eq!(classify_probe(true, "  running \n", ""), ContainerProbe::Running);
    }

    #[test]
    fn classify_probe_authoritative_not_running_states() {
        for status in ["exited", "created", "dead", "paused", "stopped", "removing", "restarting"] {
            assert_eq!(
                classify_probe(true, status, ""),
                ContainerProbe::NotRunning,
                "status '{}' must be authoritatively NotRunning",
                status
            );
        }
    }

    #[test]
    fn classify_probe_no_such_container_is_authoritative_not_running() {
        // inspect exits non-zero with this stderr when the container has
        // never been created — that IS down + restart-eligible.
        assert_eq!(
            classify_probe(false, "", "Error: no such container: vco_weaviate"),
            ContainerProbe::NotRunning
        );
        assert_eq!(
            classify_probe(false, "", "Error response from daemon: No such object: vco_ollama"),
            ContainerProbe::NotRunning
        );
    }

    #[test]
    fn classify_probe_ambiguous_failures_are_probe_error() {
        // Non-zero exit that ISN'T "no such container" → ProbeError (skip),
        // NOT down. This is the storm guard: a transient daemon hiccup
        // must not be read as "all three containers down".
        assert_eq!(
            classify_probe(false, "", "permission denied while connecting to the daemon socket"),
            ContainerProbe::ProbeError
        );
        assert_eq!(
            classify_probe(false, "", "Cannot connect to the Docker daemon"),
            ContainerProbe::ProbeError
        );
        // Zero exit but empty / unrecognized status token → ambiguous.
        assert_eq!(classify_probe(true, "", ""), ContainerProbe::ProbeError);
        assert_eq!(classify_probe(true, "Running", ""), ContainerProbe::ProbeError);
        assert_eq!(classify_probe(true, "weird-state", ""), ContainerProbe::ProbeError);
    }

    // ----- BLOCKER-2: code_embed (GPU-only) stack-membership gate -----

    #[test]
    #[serial_test::serial]
    fn code_embed_excluded_when_backend_is_ollama() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let prev = std::env::var_os(ENV_CODE_EMBED_BACKEND);
        // Explicit CPU fallback → code_embed not in stack.
        for v in ["ollama", "OLLAMA", " Ollama "] {
            std::env::set_var(ENV_CODE_EMBED_BACKEND, v);
            assert!(!code_embed_in_stack(), "backend={:?} must exclude code_embed", v);
            assert!(!service_in_stack("code_embed"));
            // weaviate / ollama are always in stack regardless.
            assert!(service_in_stack("weaviate"));
            assert!(service_in_stack("ollama"));
        }
        match prev {
            Some(p) => std::env::set_var(ENV_CODE_EMBED_BACKEND, p),
            None => std::env::remove_var(ENV_CODE_EMBED_BACKEND),
        }
    }

    #[test]
    #[serial_test::serial]
    fn code_embed_included_when_backend_unset_or_gpu() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let prev = std::env::var_os(ENV_CODE_EMBED_BACKEND);
        std::env::remove_var(ENV_CODE_EMBED_BACKEND);
        assert!(code_embed_in_stack(), "unset backend defaults to in-stack");
        assert!(service_in_stack("code_embed"));
        std::env::set_var(ENV_CODE_EMBED_BACKEND, "gpu");
        assert!(code_embed_in_stack(), "gpu backend keeps code_embed in stack");
        match prev {
            Some(p) => std::env::set_var(ENV_CODE_EMBED_BACKEND, p),
            None => std::env::remove_var(ENV_CODE_EMBED_BACKEND),
        }
    }

    // ----- CONCERN-5: infrastructure_dir env-fallback resolution -----

    #[test]
    fn infrastructure_dir_from_root_requires_compose_file() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        // No infrastructure/docker-compose.yml yet → None.
        assert!(infrastructure_dir_from_root(root).is_none());
        // Create it → Some(<root>/infrastructure).
        let infra = root.join("infrastructure");
        std::fs::create_dir_all(&infra).unwrap();
        std::fs::write(infra.join("docker-compose.yml"), "services: {}\n").unwrap();
        let resolved = infrastructure_dir_from_root(root).expect("compose present → resolves");
        assert_eq!(resolved, infra);
    }

    // ----- NIT-8 + wrapper-absent fallback: direct-compose argv -----

    #[test]
    fn fallback_compose_args_prefix_the_rules_argv() {
        let dir = tempfile::tempdir().unwrap();
        let up: Vec<String> = ["up", "-d", "--no-deps", "weaviate"].iter().map(|s| s.to_string()).collect();
        let args = build_fallback_compose_args(dir.path(), &up);
        assert_eq!(args, vec!["-f", "docker-compose.yml", "up", "-d", "--no-deps", "weaviate"]);
    }

    /// SE-4 × SE-3 red-proof: the heal's compose argv — `-f` chain + the
    /// rule's `up` argv, computed exactly as `restart_via_direct_compose`
    /// does — carries `--no-deps` and names only the healed service
    /// (code_embed: never ollama, which may be adopted). Red against a
    /// hand-built `up -d <svc>`.
    #[tokio::test]
    async fn the_heal_argv_has_no_deps_and_names_only_the_healed_service() {
        let dir = tempfile::tempdir().unwrap();
        let checkout = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
        let python = vct_launcher_core::python_resolve::resolve_python_for_vco_lib_or("python3");
        let up = vct_launcher_core::services::compose_args::compose_up_args(&python, &checkout, &["code_embed"], false)
            .await
            .unwrap();
        let args = build_fallback_compose_args(dir.path(), &up);
        assert!(args.iter().any(|a| a == "--no-deps"), "{:?}", args);
        assert!(!args.iter().any(|a| a == "ollama" || a == "weaviate"), "{:?}", args);
        assert_eq!(
            args,
            vec!["-f", "docker-compose.yml", "--profile", "gpu", "up", "-d", "--no-deps", "code_embed"]
        );
    }

    #[test]
    fn fallback_compose_args_includes_override_when_present() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("docker-compose.override.yml"),
            "services: {}\n",
        )
        .unwrap();
        let up: Vec<String> = ["up", "-d", "--no-deps", "weaviate"].iter().map(|s| s.to_string()).collect();
        let args = build_fallback_compose_args(dir.path(), &up);
        // NIT-8: the override must be appended explicitly (LAST -f wins on
        // conflicts) since explicit `-f docker-compose.yml` bypasses
        // compose's implicit override auto-load.
        assert_eq!(
            args,
            vec![
                "-f",
                "docker-compose.yml",
                "-f",
                "docker-compose.override.yml",
                "up",
                "-d",
                "--no-deps",
                "weaviate"
            ]
        );
    }

    // ----- BLOCKER-2: stack-wrapper discovery relative to infra_dir -----

    #[test]
    fn find_stack_wrapper_resolves_relative_to_orchestrator_root() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let infra = root.join("infrastructure");
        std::fs::create_dir_all(&infra).unwrap();
        // No scripts/ yet → None.
        assert!(find_stack_wrapper(&infra).is_none());
        // Ship the wrapper.
        let scripts = root.join("scripts");
        std::fs::create_dir_all(&scripts).unwrap();
        let name = if cfg!(target_os = "windows") {
            "launch-claude-mcp-stack.ps1"
        } else {
            "launch-claude-mcp-stack.sh"
        };
        let wrapper = scripts.join(name);
        std::fs::write(&wrapper, "#!/usr/bin/env bash\n").unwrap();
        let found = find_stack_wrapper(&infra).expect("wrapper shipped → found");
        assert_eq!(found, wrapper);
    }

    // ----- BLOCKER-1: pause-marker consumer delegates to shared helper ---

    #[test]
    #[serial_test::serial]
    fn is_service_paused_reads_shared_marker() {
        // Redirect vct_root_dir at a temp dir + verify the consumer here
        // sees a marker the shared PRODUCER created (same path → wired).
        let _dir = vct_launcher_core::test_env::state_dir_guard();

        assert!(!is_service_paused("weaviate"));
        // Producer side (shared helper) drops the marker.
        watchdog_pause::create_pause_marker("weaviate").unwrap();
        // Consumer side (this module's delegation) must see it.
        assert!(is_service_paused("weaviate"));
        assert!(pause_marker_path("weaviate").ends_with("watchdog-paused/weaviate"));
        watchdog_pause::remove_pause_marker("weaviate").unwrap();
        assert!(!is_service_paused("weaviate"));
    }
}
