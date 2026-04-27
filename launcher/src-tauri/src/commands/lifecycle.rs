use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use tauri::{command, AppHandle, Emitter, State, Window};

use crate::commands::installer::{
    DEFAULT_CODE_EMBED_PORT, DEFAULT_OLLAMA_PORT, DEFAULT_WEAVIATE_PORT,
};
use crate::registry::persist_service_registry;
use crate::services::adoption::{
    self, AdoptionMode, AdoptionState, ServiceAdoption,
};
use crate::services::runtime::{detect_runtime, invalidate_cache as invalidate_runtime_cache, RuntimeInfo};
use crate::state::AppManager;
use crate::types::{AppStatus, HealthStatus, LaunchConfig, ServiceEntry};

/// Launch an app subprocess. Returns PID on success.
/// Emits "app_status_changed" event when status transitions.
///
/// Removed from invoke_handler 2026-04-27 — zero FE/Hub consumers. Retained
/// under #[allow(dead_code)] for the launch_app suite (launch/kill/status/
/// health) until a packaged-app launcher is reintroduced.
#[allow(dead_code)]
#[command]
pub async fn launch_app(
    config: LaunchConfig,
    state: State<'_, AppManager>,
    window: Window,
) -> Result<u32, String> {
    let app_id = config.app_id.clone();

    // Guard: already running
    {
        let guard = state.0.lock().unwrap();
        if let Some(proc) = guard.get(&app_id) {
            if proc.entry.status == AppStatus::Running {
                return Err(format!(
                    "App '{}' is already running (PID {})",
                    app_id,
                    proc.entry.pid.unwrap_or(0)
                ));
            }
        }
    }

    // Emit "starting" status
    let _ = window.emit(
        "app_status_changed",
        ServiceEntry {
            app_id: app_id.clone(),
            status: AppStatus::Starting,
            pid: None,
            port: config.port,
            health_url: config.health_url.clone(),
            install_path: Some(config.executable.clone()),
            version: None,
            active_project: config.project_id.clone(),
            error_message: None,
            started_at: None,
        },
    );

    // Build and spawn subprocess
    let mut cmd = std::process::Command::new(&config.executable);
    cmd.args(&config.args);
    for (k, v) in &config.env {
        cmd.env(k, v);
    }
    if let Some(ref ws) = config.workspace_path {
        cmd.current_dir(ws);
    }

    let child = cmd
        .spawn()
        .map_err(|e| format!("Failed to spawn '{}': {}", config.executable, e))?;
    let pid = child.id();
    let started_at = chrono::Utc::now().to_rfc3339();

    let entry = ServiceEntry {
        app_id: app_id.clone(),
        status: AppStatus::Running,
        pid: Some(pid),
        port: config.port,
        health_url: config.health_url.clone(),
        install_path: Some(config.executable.clone()),
        version: None,
        active_project: config.project_id,
        error_message: None,
        started_at: Some(started_at),
    };

    {
        let mut guard = state.0.lock().unwrap();
        guard.insert(
            app_id.clone(),
            crate::state::AppProcess {
                child,
                entry: entry.clone(),
            },
        );
    }

    persist_service_registry(&state).await?;

    let _ = window.emit("app_status_changed", entry);

    Ok(pid)
}

/// Gracefully kill a running app.
#[allow(dead_code)]
#[command]
pub async fn kill_app(
    app_id: String,
    state: State<'_, AppManager>,
    window: Window,
) -> Result<(), String> {
    let entry = {
        let mut guard = state.0.lock().unwrap();
        let proc = guard
            .get_mut(&app_id)
            .ok_or_else(|| format!("App '{}' not found in registry", app_id))?;

        proc.child
            .kill()
            .map_err(|e| format!("Kill failed: {}", e))?;
        let _ = proc.child.wait();

        proc.entry.status = AppStatus::Stopped;
        proc.entry.pid = None;
        proc.entry.clone()
    }; // guard dropped here

    persist_service_registry(&state).await?;
    let _ = window.emit("app_status_changed", entry);

    Ok(())
}

/// Get current status of a single app.
/// Checks if the process is still alive (crash detection).
#[allow(dead_code)]
#[command]
pub fn get_app_status(app_id: String, state: State<'_, AppManager>) -> ServiceEntry {
    let mut guard = state.0.lock().unwrap();
    match guard.get_mut(&app_id) {
        Some(proc) => {
            match proc.child.try_wait() {
                Ok(Some(exit_status)) => {
                    proc.entry.status = if exit_status.success() {
                        AppStatus::Stopped
                    } else {
                        AppStatus::Error
                    };
                    proc.entry.pid = None;
                    proc.entry.error_message = Some(format!("Exited: {}", exit_status));
                }
                Ok(None) => {} // Still running
                Err(e) => {
                    proc.entry.status = AppStatus::Error;
                    proc.entry.error_message = Some(e.to_string());
                }
            }
            proc.entry.clone()
        }
        None => ServiceEntry {
            app_id,
            status: AppStatus::Stopped,
            pid: None,
            port: None,
            health_url: None,
            install_path: None,
            version: None,
            active_project: None,
            error_message: None,
            started_at: None,
        },
    }
}

/// Get status of ALL apps tracked in the registry.
#[allow(dead_code)]
#[command]
pub fn get_all_app_statuses(state: State<'_, AppManager>) -> Vec<ServiceEntry> {
    let mut guard = state.0.lock().unwrap();
    guard
        .values_mut()
        .map(|proc| {
            if let Ok(Some(exit_status)) = proc.child.try_wait() {
                proc.entry.status = if exit_status.success() {
                    AppStatus::Stopped
                } else {
                    AppStatus::Error
                };
                proc.entry.pid = None;
            }
            proc.entry.clone()
        })
        .collect()
}

/// HTTP health check against a running app's /health endpoint (3s timeout).
#[allow(dead_code)]
#[command]
pub async fn check_app_health(app_id: String, health_url: String) -> HealthStatus {
    let start = std::time::Instant::now();
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(3))
        .build()
        .unwrap();

    match client.get(&health_url).send().await {
        Ok(resp) if resp.status().is_success() => {
            let latency_ms = start.elapsed().as_millis() as u64;
            let body: serde_json::Value = resp.json().await.unwrap_or_default();
            HealthStatus {
                app_id,
                healthy: true,
                status_text: body["status"].as_str().map(String::from),
                version: body["version"].as_str().map(String::from),
                uptime_ms: body["uptime_ms"].as_u64(),
                latency_ms: Some(latency_ms),
            }
        }
        Ok(resp) => HealthStatus {
            app_id,
            healthy: false,
            status_text: Some(format!("HTTP {}", resp.status())),
            version: None,
            uptime_ms: None,
            latency_ms: Some(start.elapsed().as_millis() as u64),
        },
        Err(e) => HealthStatus {
            app_id,
            healthy: false,
            status_text: Some(e.to_string()),
            version: None,
            uptime_ms: None,
            latency_ms: None,
        },
    }
}

/// Ping health endpoints of ALL running apps in parallel.
#[allow(dead_code)]
#[command]
pub async fn check_all_health(state: State<'_, AppManager>) -> Result<Vec<HealthStatus>, String> {
    let entries: Vec<ServiceEntry> = {
        let guard = state.0.lock().unwrap();
        guard
            .values()
            .filter(|p| p.entry.status == AppStatus::Running && p.entry.health_url.is_some())
            .map(|p| p.entry.clone())
            .collect()
    };

    let futures: Vec<_> = entries
        .into_iter()
        .map(|entry| {
            let url = entry.health_url.clone().unwrap();
            check_app_health(entry.app_id, url)
        })
        .collect();

    Ok(futures::future::join_all(futures).await)
}

// ---------------------------------------------------------------------------
// Shared-container lifecycle (Podman/Docker compose).
//
// These commands drive `<runtime> compose ...` (or `<runtime>-compose ...`
// — see services/runtime.rs) against the shared
// `infrastructure/docker-compose.yml`. They are the lifecycle backbone for:
//
//   - Auto-start on launcher boot (lib.rs)
//   - Tray "Start/Stop services" buttons (front-end)
//   - Quit confirmation's "Quit and stop services" (parallel agent)
//
// Coordination notes for parallel agents:
//
//   - Tray-pill agent: probes the same three ports directly via
//     `probe_one()` in tray.rs. We don't share a probe function. The tray
//     pill DOES read the Tauri command shape below if it ever needs more
//     detail than its inline probe — feel free to call `services_status`
//     for richer data.
//
//   - Quit-confirmation agent: calls `services_stop_all`. Idempotent:
//     succeeds even when nothing is up.
//
//   - The existing `commands::installer::detect_existing_services` is
//     PRESERVED unchanged — the OnboardingWizard and SettingsPanel
//     consume its specific shape. The richer `services_status` below is
//     a NEW command, not a refactor of the old one.
// ---------------------------------------------------------------------------

/// Per-service runtime status. Returned by `services_status` for each of
/// Weaviate / Ollama / code_embed.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServiceRuntimeState {
    /// `"weaviate"` | `"ollama"` | `"code_embed"`.
    pub name: String,
    /// True iff the canonical health URL responded 2xx/3xx.
    pub running: bool,
    /// Host port the service is bound to. Includes the user's
    /// `Mode::Parallel` override if they chose to run alongside an
    /// external service; defaults otherwise.
    pub port: u16,
    /// Canonical URL the launcher probed (or routes to, for adopted
    /// services). Empty string if neither apply.
    pub url: String,
    /// True when something is responding on the port but the launcher
    /// did NOT start it (no record in `services.toml`, or
    /// `Mode::Adopt`/`Mode::Refuse`).
    pub externally_managed: bool,
    /// Mirror of the persisted adoption mode. Frontend shows this in
    /// the Services preferences screen.
    pub adoption_mode: AdoptionMode,
}

/// Aggregate snapshot returned by `services_status`. Mirrors the shape
/// the tray pill / Services panel both consume.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServicesRuntimeSnapshot {
    pub services: Vec<ServiceRuntimeState>,
    /// Detected container runtime (`"podman"` | `"docker"` | `null`).
    pub runtime: Option<String>,
    /// True when the launcher is on macOS/Windows AND Podman is the
    /// runtime AND `podman machine` reports no running machine. The
    /// frontend uses this to show "Start Podman Machine" CTA.
    pub needs_podman_machine_start: bool,
    /// True when at least one service is responding but is not managed
    /// by us (= externally_managed). Frontend uses this to gate the
    /// adoption prompt.
    pub has_unresolved_external: bool,
}

/// All three canonical service names + their health URL templates.
/// Keep in sync with `installer::detect_existing_services`.
fn canonical_services() -> [(&'static str, u16, fn(u16) -> String); 3] {
    [
        ("weaviate", DEFAULT_WEAVIATE_PORT, |p| {
            format!("http://localhost:{}/v1/.well-known/ready", p)
        }),
        ("ollama", DEFAULT_OLLAMA_PORT, |p| {
            format!("http://localhost:{}/api/tags", p)
        }),
        ("code_embed", DEFAULT_CODE_EMBED_PORT, |p| {
            format!("http://localhost:{}/health", p)
        }),
    ]
}

/// HTTP probe with 2s timeout. Returns true on 2xx/3xx.
async fn probe_url(url: &str) -> bool {
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
    {
        Ok(c) => c,
        Err(_) => return false,
    };
    matches!(client.get(url).send().await, Ok(r) if r.status().as_u16() < 400)
}

/// Resolve the compose directory — `<repo_root>/infrastructure`. Errors
/// when we can't locate the orchestrator repo (e.g. the binary isn't
/// shipped with the source tree).
fn compose_dir() -> Result<PathBuf, String> {
    let root = crate::commands::installer::find_local_repo_root()?;
    Ok(root.join("infrastructure"))
}

/// Get the per-service effective port — falls back to the canonical
/// default unless the user picked `Mode::Parallel`, in which case we
/// honor the recorded `parallel_port`.
fn effective_port(name: &str, default_port: u16, state: &AdoptionState) -> u16 {
    state
        .get(name)
        .filter(|s| s.mode == AdoptionMode::Parallel)
        .and_then(|s| s.parallel_port)
        .unwrap_or(default_port)
}

/// Read the launcher-managed services snapshot. Probes all three
/// services concurrently; total wall time is bounded by the slowest
/// 2s probe.
#[command]
pub async fn services_status() -> Result<ServicesRuntimeSnapshot, String> {
    let adoption_state = adoption::read();

    // Build per-service probe URLs honoring any parallel-port overrides.
    let mut probes: Vec<(String, u16, String)> = Vec::new(); // (name, port, url)
    for (name, default_port, url_for) in canonical_services() {
        let port = effective_port(name, default_port, &adoption_state);
        probes.push((name.to_string(), port, url_for(port)));
    }

    // Concurrent probes.
    let probe_futures: Vec<_> = probes
        .iter()
        .map(|(_, _, url)| probe_url(url))
        .collect();
    let results = futures::future::join_all(probe_futures).await;

    let runtime_info = detect_runtime().await;

    let mut services: Vec<ServiceRuntimeState> = Vec::new();
    let mut has_unresolved = false;
    for ((name, port, url), running) in probes.into_iter().zip(results.into_iter()) {
        let entry = adoption_state.get(&name);
        let mode = entry.map(|s| s.mode).unwrap_or(AdoptionMode::Unresolved);
        // "Externally managed" =  the service is up AND we either haven't
        // resolved adoption yet OR the user picked Adopt/Refuse. Mode
        // == Parallel means we manage our own copy ourselves.
        let externally_managed = running
            && matches!(
                mode,
                AdoptionMode::Adopt | AdoptionMode::Refuse | AdoptionMode::Unresolved
            );
        if running && mode == AdoptionMode::Unresolved {
            has_unresolved = true;
        }
        services.push(ServiceRuntimeState {
            name,
            running,
            port,
            url,
            externally_managed,
            adoption_mode: mode,
        });
    }

    Ok(ServicesRuntimeSnapshot {
        services,
        runtime: runtime_info
            .as_ref()
            .map(|r| r.runtime.binary().to_string()),
        needs_podman_machine_start: runtime_info
            .as_ref()
            .map(|r| r.needs_machine_start)
            .unwrap_or(false),
        has_unresolved_external: has_unresolved,
    })
}

/// Helper: translate a tokio process result into a launcher error
/// string. Captures stderr so the frontend can show real failure
/// messages instead of "compose up failed (status 1)".
async fn run_compose<I, S>(info: &RuntimeInfo, args: I) -> Result<(), String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<std::ffi::OsStr>,
{
    let dir = compose_dir()?;
    let mut cmd = info.compose_command();
    cmd.args(args);
    cmd.current_dir(&dir);
    let output = cmd
        .output()
        .await
        .map_err(|e| format!("spawn {} compose: {}", info.runtime.display_name(), e))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "{} compose failed (status {}): {}",
            info.runtime.display_name(),
            output.status,
            stderr.trim()
        ));
    }
    Ok(())
}

/// Bring up the shared compose stack. Idempotent: a second call when
/// containers already run is a fast no-op. Skips services the user
/// chose to Adopt or Refuse (those are someone else's; we don't touch
/// them).
#[command]
pub async fn services_start_all() -> Result<(), String> {
    let info = detect_runtime()
        .await
        .ok_or("No container runtime found. Install Podman or Docker.")?;
    let adoption_state = adoption::read();

    // Compute the subset of canonical services that we manage. Anything
    // the user adopted/refused is NOT in the up list.
    let mut managed: Vec<&str> = Vec::new();
    for (name, _, _) in canonical_services() {
        let mode = adoption_state.get(name).map(|s| s.mode).unwrap_or(AdoptionMode::Unresolved);
        if matches!(mode, AdoptionMode::Adopt | AdoptionMode::Refuse) {
            continue;
        }
        managed.push(name);
    }

    if managed.is_empty() {
        // All services are externally managed — nothing to start.
        return Ok(());
    }

    // `up -d` with no service args = "everything in the file"; with
    // explicit names = "only these". When the entire stack is managed
    // we use the no-arg form so future compose additions don't get
    // silently skipped.
    let mut args: Vec<String> = vec!["up".into(), "-d".into()];
    if managed.len() < canonical_services().len() {
        for n in &managed {
            args.push((*n).to_string());
        }
    }
    run_compose(&info, args).await
}

/// Stop the shared compose stack WITHOUT removing volumes (no `-v`
/// flag — that would destroy data). Idempotent: succeeds even when
/// nothing is up. Used by Quit-confirmation's "Quit and stop services"
/// button.
#[command]
pub async fn services_stop_all() -> Result<(), String> {
    let info = detect_runtime()
        .await
        .ok_or("No container runtime found.")?;
    // `compose stop` halts containers but leaves them defined. `compose
    // down` would also remove the containers. Either preserves volumes
    // (we explicitly never pass --volumes / -v). We use `stop` so a
    // subsequent `up -d` is fast — it just restarts the same containers.
    run_compose(&info, ["stop"]).await
}

/// Restart all services. `compose restart` does this atomically; we
/// don't need the down+up dance.
#[command]
pub async fn services_restart_all() -> Result<(), String> {
    let info = detect_runtime()
        .await
        .ok_or("No container runtime found.")?;
    run_compose(&info, ["restart"]).await
}

/// Start a single service by canonical name. Errors if the user has
/// Adopted/Refused it (we don't manage someone else's containers).
#[command]
pub async fn service_start(name: String) -> Result<(), String> {
    validate_service_name(&name)?;
    let info = detect_runtime()
        .await
        .ok_or("No container runtime found.")?;
    let state = adoption::read();
    let mode = state.get(&name).map(|s| s.mode).unwrap_or(AdoptionMode::Unresolved);
    if matches!(mode, AdoptionMode::Adopt | AdoptionMode::Refuse) {
        return Err(format!(
            "{} is externally managed (mode={:?}); the launcher does not control it.",
            name, mode
        ));
    }
    run_compose(&info, ["up", "-d", &name]).await
}

/// Stop a single service.
#[command]
pub async fn service_stop(name: String) -> Result<(), String> {
    validate_service_name(&name)?;
    let info = detect_runtime()
        .await
        .ok_or("No container runtime found.")?;
    let state = adoption::read();
    let mode = state.get(&name).map(|s| s.mode).unwrap_or(AdoptionMode::Unresolved);
    if matches!(mode, AdoptionMode::Adopt | AdoptionMode::Refuse) {
        return Err(format!(
            "{} is externally managed (mode={:?}); the launcher does not control it.",
            name, mode
        ));
    }
    run_compose(&info, ["stop", &name]).await
}

/// Restart a single service.
#[command]
pub async fn service_restart(name: String) -> Result<(), String> {
    validate_service_name(&name)?;
    let info = detect_runtime()
        .await
        .ok_or("No container runtime found.")?;
    let state = adoption::read();
    let mode = state.get(&name).map(|s| s.mode).unwrap_or(AdoptionMode::Unresolved);
    if matches!(mode, AdoptionMode::Adopt | AdoptionMode::Refuse) {
        return Err(format!(
            "{} is externally managed (mode={:?}); the launcher does not control it.",
            name, mode
        ));
    }
    run_compose(&info, ["restart", &name]).await
}

/// Hard-coded allowlist — only the three canonical services are valid
/// targets. Prevents arbitrary string injection into `compose <verb>
/// <name>` from a malicious frontend (or a typo'd page).
fn validate_service_name(name: &str) -> Result<(), String> {
    match name {
        "weaviate" | "ollama" | "code_embed" => Ok(()),
        _ => Err(format!(
            "unknown service '{}'; expected weaviate | ollama | code_embed",
            name
        )),
    }
}

// ---------------------------------------------------------------------------
// Adoption-state Tauri commands (read/write `~/.vct/services.toml`)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AdoptionDecision {
    pub name: String,
    pub mode: AdoptionMode,
    /// Required when `mode == Parallel`. The frontend probes for a free
    /// port before calling this command.
    pub parallel_port: Option<u16>,
    /// External URL captured at decision time (for display). Optional —
    /// `services_status` will refresh it on next probe anyway.
    pub external_url: Option<String>,
}

/// Persist the user's adopt-vs-parallel choice for ONE service.
/// Frontend calls this in response to the `vct-external-services-detected`
/// event. The launcher does NOT auto-apply parallel overrides here — the
/// user must subsequently click Start to bring services up on the new
/// port (or rely on the next launcher boot's auto-start).
#[command]
pub async fn services_set_adoption(decision: AdoptionDecision) -> Result<(), String> {
    validate_service_name(&decision.name)?;
    if decision.mode == AdoptionMode::Parallel && decision.parallel_port.is_none() {
        return Err("parallel mode requires parallel_port".into());
    }
    let mut state = adoption::read();
    state.upsert(ServiceAdoption {
        name: decision.name,
        mode: decision.mode,
        external_url: decision.external_url,
        parallel_port: decision.parallel_port,
    });
    adoption::write(&state)
}

/// Clear adoption decisions so the launcher re-prompts on next boot.
/// Called from the Services preferences "Re-detect" button.
#[command]
pub async fn services_reset_adoption() -> Result<(), String> {
    adoption::write(&AdoptionState::default())?;
    invalidate_runtime_cache();
    Ok(())
}

/// Read the current adoption state. Used by the preferences screen.
#[command]
pub async fn services_get_adoption() -> Result<AdoptionState, String> {
    Ok(adoption::read())
}

/// Probe a list of candidate ports and return the first one that is
/// not bound. The frontend calls this when the user picks "Run parallel
/// on different port" so we offer a sensible default in the dialog.
///
/// Bind-test approach: try `TcpListener::bind`. If it succeeds the port
/// is free at this exact moment (TOCTOU caveat — by the time we run
/// `compose up` someone else might have grabbed it; in practice the
/// race window is microseconds and the user can re-pick if it fails).
#[command]
pub async fn services_find_free_port(start: u16, end: u16) -> Result<u16, String> {
    use std::net::TcpListener;
    if start > end {
        return Err(format!("invalid range {}..{}", start, end));
    }
    for port in start..=end {
        if TcpListener::bind(("127.0.0.1", port)).is_ok() {
            return Ok(port);
        }
    }
    Err(format!("no free port found in range {}..{}", start, end))
}

// ---------------------------------------------------------------------------
// Auto-start on launcher boot
//
// Called from lib.rs::run() after tray init. Emits frontend events so
// the UI can show progress without blocking the window from rendering.
// ---------------------------------------------------------------------------

/// Frontend event names. Centralized here so tray.rs and the Services
/// page subscribe to the same strings.
pub const EVT_EXTERNAL_DETECTED: &str = "vct-external-services-detected";
pub const EVT_LIFECYCLE_PROGRESS: &str = "vct-services-lifecycle";

#[derive(Debug, Clone, Serialize)]
pub struct LifecycleProgress {
    /// `"detecting_runtime"` | `"runtime_missing"` | `"starting"` |
    /// `"started"` | `"start_failed"` | `"stopping"` | `"stopped"`.
    pub phase: String,
    pub message: String,
}

/// Auto-start the shared services on launcher boot. Non-blocking: spawned
/// in the background by `lib.rs::run` so the window can render
/// immediately. Surface progress via the `vct-services-lifecycle` event.
///
/// Behavior:
///   1. Detect runtime. Missing → emit `runtime_missing`, return.
///   2. Snapshot status. If everything is already up AND not externally
///      managed → no-op.
///   3. If externally-managed services with unresolved adoption →
///      emit `vct-external-services-detected` with the list. Frontend
///      shows the dialog; user picks; user clicks Start manually after.
///   4. Else → call `services_start_all`. Emit `started` or `start_failed`.
pub async fn auto_start_on_boot(app: AppHandle) {
    let _ = app.emit(
        EVT_LIFECYCLE_PROGRESS,
        LifecycleProgress {
            phase: "detecting_runtime".into(),
            message: "Detecting container runtime…".into(),
        },
    );

    let info = match detect_runtime().await {
        Some(i) => i,
        None => {
            let _ = app.emit(
                EVT_LIFECYCLE_PROGRESS,
                LifecycleProgress {
                    phase: "runtime_missing".into(),
                    message: "No container runtime found. Install Podman or Docker to run VCT services.".into(),
                },
            );
            return;
        }
    };

    if info.needs_machine_start {
        let _ = app.emit(
            EVT_LIFECYCLE_PROGRESS,
            LifecycleProgress {
                phase: "runtime_missing".into(),
                message: format!(
                    "Podman is installed but no machine is running. Run `podman machine start` and re-detect."
                ),
            },
        );
        return;
    }

    let snapshot = match services_status().await {
        Ok(s) => s,
        Err(e) => {
            let _ = app.emit(
                EVT_LIFECYCLE_PROGRESS,
                LifecycleProgress {
                    phase: "start_failed".into(),
                    message: format!("status probe failed: {}", e),
                },
            );
            return;
        }
    };

    // Externally-managed services with unresolved adoption → prompt and bail.
    let unresolved: Vec<ServiceRuntimeState> = snapshot
        .services
        .iter()
        .filter(|s| s.running && s.adoption_mode == AdoptionMode::Unresolved)
        .cloned()
        .collect();
    if !unresolved.is_empty() {
        let _ = app.emit(EVT_EXTERNAL_DETECTED, unresolved);
        // We do NOT auto-start anything else — the user might have
        // adopted ALL services and we'd race against the dialog.
        return;
    }

    // Determine if anything we manage is actually down.
    let any_down = snapshot.services.iter().any(|s| {
        !s.running
            && !matches!(
                s.adoption_mode,
                AdoptionMode::Adopt | AdoptionMode::Refuse
            )
    });
    if !any_down {
        let _ = app.emit(
            EVT_LIFECYCLE_PROGRESS,
            LifecycleProgress {
                phase: "started".into(),
                message: "Services already running.".into(),
            },
        );
        return;
    }

    let _ = app.emit(
        EVT_LIFECYCLE_PROGRESS,
        LifecycleProgress {
            phase: "starting".into(),
            message: format!(
                "Starting VCT services via {}…",
                info.runtime.display_name()
            ),
        },
    );
    match services_start_all().await {
        Ok(()) => {
            let _ = app.emit(
                EVT_LIFECYCLE_PROGRESS,
                LifecycleProgress {
                    phase: "started".into(),
                    message: "Services up.".into(),
                },
            );
        }
        Err(e) => {
            let _ = app.emit(
                EVT_LIFECYCLE_PROGRESS,
                LifecycleProgress {
                    phase: "start_failed".into(),
                    message: e,
                },
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod services_lifecycle_tests {
    use super::*;

    #[test]
    fn validate_service_name_accepts_canonical() {
        assert!(validate_service_name("weaviate").is_ok());
        assert!(validate_service_name("ollama").is_ok());
        assert!(validate_service_name("code_embed").is_ok());
    }

    #[test]
    fn validate_service_name_rejects_unknown() {
        assert!(validate_service_name("postgres").is_err());
        // Critical: must reject anything that could be used for argument
        // injection. Compose passes the string positionally so an empty
        // string would be silently dropped, but a "; rm -rf /" must
        // never reach the binary even if compose tokenizes safely.
        assert!(validate_service_name("").is_err());
        assert!(validate_service_name("weaviate; rm -rf /").is_err());
        assert!(validate_service_name("../etc/passwd").is_err());
    }

    #[test]
    fn effective_port_falls_back_to_default_when_no_override() {
        let state = AdoptionState::default();
        assert_eq!(effective_port("weaviate", 8081, &state), 8081);
    }

    #[test]
    fn effective_port_uses_parallel_port_when_set() {
        let mut state = AdoptionState::default();
        state.upsert(ServiceAdoption {
            name: "weaviate".into(),
            mode: AdoptionMode::Parallel,
            external_url: None,
            parallel_port: Some(8091),
        });
        assert_eq!(effective_port("weaviate", 8081, &state), 8091);
    }

    #[test]
    fn effective_port_ignores_parallel_port_for_adopted_service() {
        // If the user adopted, we route to the canonical port (where
        // their existing service lives). Parallel port only applies in
        // Parallel mode.
        let mut state = AdoptionState::default();
        state.upsert(ServiceAdoption {
            name: "weaviate".into(),
            mode: AdoptionMode::Adopt,
            external_url: Some("http://localhost:8081".into()),
            parallel_port: Some(8091), // ignored under Adopt mode
        });
        assert_eq!(effective_port("weaviate", 8081, &state), 8081);
    }

    #[tokio::test]
    async fn find_free_port_returns_in_range() {
        // The 65000-65535 range is almost always free on dev boxes.
        let port = services_find_free_port(65_000, 65_535)
            .await
            .expect("expected a free port in 65000..65535");
        assert!((65_000..=65_535).contains(&port));
    }

    #[tokio::test]
    async fn find_free_port_rejects_invalid_range() {
        let err = services_find_free_port(2000, 1000).await.unwrap_err();
        assert!(err.contains("invalid range"));
    }
}
