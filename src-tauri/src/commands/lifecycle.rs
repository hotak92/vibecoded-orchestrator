use tauri::{command, Emitter, State, Window};

use crate::registry::persist_service_registry;
use crate::state::AppManager;
use crate::types::{AppStatus, HealthStatus, LaunchConfig, ServiceEntry};

/// Launch an app subprocess. Returns PID on success.
/// Emits "app_status_changed" event when status transitions.
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
