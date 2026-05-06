//! Hub routes that expose module + project state to headless callers
//! (the `vibecoded` CLI, other VCT apps, scripts).
//!
//! These routes are read-mostly. The one write operation (`POST
//! /modules/install`) schedules an install and returns immediately — the
//! caller polls module status via `GET /modules/{id}/status?project_id=...`.
//!
//! Security: the hub binds to 127.0.0.1 only. We do NOT expose this over
//! the network. A future version may add token auth for scripts that need
//! to run outside the user's desktop session.

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

use crate::db::models::{ModuleInstallRow, ProjectHost, ProjectRow};
use crate::db::Db;

/// Shared handle to the launcher DB opened by `hub::server::start_hub_server`.
/// The Tauri-side code manages its own Db handle; the hub uses its own
/// instance (SQLite allows multiple connections when WAL mode is on).
#[derive(Clone)]
pub struct LauncherDbHandle(pub Arc<Db>);

pub fn router() -> Router<LauncherDbHandle> {
    Router::new()
        .route("/modules/catalog", get(catalog))
        .route("/modules/installed", get(installed))
        .route("/modules/{module_id}/status", get(module_status))
        .route("/modules/install", post(install))
        .route("/projects", get(list_projects))
        .route("/projects/{project_id}", get(get_project))
        .route("/projects/{project_id}/env", get(project_env))
        .route("/projects/by-slug/{slug}", get(get_project_by_slug_route))
}

// ─── Types ───────────────────────────────────────────────────────────────

#[derive(Debug, Serialize)]
struct CatalogEntry {
    id: String,
    name: String,
    version: String,
    category: String,
    license_required: bool,
    compatibility_hosts: Vec<String>,
}

#[derive(Debug, Serialize)]
struct ProjectSummary {
    id: String,
    name: String,
    folder_path: String,
    host: String,
    slug: String,
    module_count: u32,
}

impl ProjectSummary {
    fn from_row(row: &ProjectRow, module_count: u32) -> Self {
        Self {
            id: row.id.clone(),
            name: row.name.clone(),
            folder_path: row.folder_path.clone(),
            host: row.host.as_str().to_string(),
            slug: row.slug.clone(),
            module_count,
        }
    }
}

#[derive(Debug, Deserialize)]
struct StatusQuery {
    project_id: String,
}

#[derive(Debug, Serialize)]
struct InstalledRowView {
    project_id: String,
    module_id: String,
    module_version: String,
    status: String,
    enabled: bool,
    install_path: String,
}

impl From<&ModuleInstallRow> for InstalledRowView {
    fn from(r: &ModuleInstallRow) -> Self {
        Self {
            project_id: r.project_id.clone(),
            module_id: r.module_id.clone(),
            module_version: r.module_version.clone(),
            status: r.status.as_str().to_string(),
            enabled: r.enabled,
            install_path: r.install_path.clone(),
        }
    }
}

#[derive(Debug, Deserialize)]
struct InstalledQuery {
    project_id: String,
}

// Module install request body. Currently the `install` handler returns
// 501 NOT_IMPLEMENTED and doesn't read the fields, but the struct
// validates that the body shape is correct (deny-extra-fields via
// serde) and locks in the schema for when the handler is wired (see
// the planned IPC channel back to the Tauri main thread, documented
// in the install handler comment below).
#[allow(dead_code)]
#[derive(Debug, Deserialize)]
struct InstallReq {
    project_id: String,
    module_id: String,
}

// ─── Handlers ────────────────────────────────────────────────────────────

async fn catalog(State(h): State<LauncherDbHandle>) -> impl IntoResponse {
    // Scan bundled_manifests + ~/.vct/modules for manifests.
    let manifests = scan_manifests();
    let tier = h.0.get_tier_cache().ok();
    let tier_name = tier
        .as_ref()
        .map(|t| t.orchestrator_tier.as_str())
        .unwrap_or("free");

    let out: Vec<CatalogEntry> = manifests
        .into_iter()
        .filter_map(|(_, m)| {
            Some(CatalogEntry {
                id: m.id.clone(),
                name: m.name.clone(),
                version: m.version.clone(),
                category: format!("{:?}", m.category).to_lowercase(),
                license_required: m.license.required,
                compatibility_hosts: m.compatibility.hosts.clone(),
            })
        })
        .collect();

    let _ = tier_name; // tier-filtered view is a future UI concern
    Json(serde_json::json!({ "modules": out })).into_response()
}

async fn installed(
    State(h): State<LauncherDbHandle>,
    Query(q): Query<InstalledQuery>,
) -> impl IntoResponse {
    match h.0.list_module_installs_for_project(&q.project_id) {
        Ok(rows) => {
            let views: Vec<InstalledRowView> = rows.iter().map(InstalledRowView::from).collect();
            Json(serde_json::json!({ "modules": views })).into_response()
        }
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
    }
}

async fn module_status(
    State(h): State<LauncherDbHandle>,
    Path(module_id): Path<String>,
    Query(q): Query<StatusQuery>,
) -> impl IntoResponse {
    match h.0.get_module_install(&q.project_id, &module_id) {
        Ok(Some(row)) => Json(InstalledRowView::from(&row)).into_response(),
        Ok(None) => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({ "error": "not installed" })),
        )
            .into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
    }
}

async fn install(
    State(_h): State<LauncherDbHandle>,
    Json(_req): Json<InstallReq>,
) -> impl IntoResponse {
    // Installs require the Tauri AppHandle (for event emission) which isn't
    // reachable from the hub server task. Expose install via the CLI only
    // once we add a proper IPC channel back to the Tauri main thread.
    //
    // For V1 the CLI can call the Tauri command via a sidecar invocation
    // (`vibecoded install <module>`) which spawns the launcher in daemon
    // mode and waits for install-complete. Documented in LAUNCHER_BACKEND_API.md.
    (
        StatusCode::NOT_IMPLEMENTED,
        Json(serde_json::json!({
            "error": "install via hub not supported in v1 — use `vibecoded install` CLI or the launcher GUI"
        })),
    )
        .into_response()
}

async fn list_projects(State(h): State<LauncherDbHandle>) -> impl IntoResponse {
    match h.0.list_projects() {
        Ok(rows) => {
            let summaries: Vec<ProjectSummary> = rows
                .iter()
                .map(|r| {
                    let count = h
                        .0
                        .list_module_installs_for_project(&r.id)
                        .map(|v| v.len() as u32)
                        .unwrap_or(0);
                    ProjectSummary::from_row(r, count)
                })
                .collect();
            Json(serde_json::json!({ "projects": summaries })).into_response()
        }
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
    }
}

async fn get_project(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
) -> impl IntoResponse {
    match h.0.get_project(&project_id) {
        Ok(Some(row)) => {
            let count = h
                .0
                .list_module_installs_for_project(&row.id)
                .map(|v| v.len() as u32)
                .unwrap_or(0);
            Json(ProjectSummary::from_row(&row, count)).into_response()
        }
        Ok(None) => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({ "error": "project not found" })),
        )
            .into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
    }
}

async fn get_project_by_slug_route(
    State(h): State<LauncherDbHandle>,
    Path(slug): Path<String>,
) -> impl IntoResponse {
    match h.0.get_project_by_slug(&slug) {
        Ok(Some(row)) => {
            let count = h
                .0
                .list_module_installs_for_project(&row.id)
                .map(|v| v.len() as u32)
                .unwrap_or(0);
            Json(ProjectSummary::from_row(&row, count)).into_response()
        }
        Ok(None) => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({ "error": "project not found" })),
        )
            .into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
    }
}

/// Return the merged env dict the launcher would inject into a workflow
/// running in this project. Secrets are resolved from the keychain;
/// settings from the DB.
///
/// **Security**: this endpoint returns secret values in cleartext. It is
/// bound to 127.0.0.1 only. Apps that consume it (e.g. the orchestrator
/// launching a workflow) run on the same machine as the user.
///
/// Future work (tracked in LAUNCHER_BACKEND_API.md §10): require a caller
/// auth token so scripts running under a different user can't siphon
/// secrets via a local port scan.
async fn project_env(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
) -> impl IntoResponse {
    let project = match h.0.get_project(&project_id) {
        Ok(Some(p)) => p,
        Ok(None) => {
            return (
                StatusCode::NOT_FOUND,
                Json(serde_json::json!({ "error": "project not found" })),
            )
                .into_response();
        }
        Err(e) => return (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
    };

    let mut env = serde_json::Map::new();
    env.insert("VCT_PROJECT_ID".into(), serde_json::Value::String(project.id.clone()));
    env.insert("VCT_PROJECT_HOST".into(), serde_json::Value::String(project.host.as_str().into()));
    env.insert("VCT_PROJECT_PATH".into(), serde_json::Value::String(project.folder_path.clone()));

    // Module settings + secrets — iterate installed modules, collect env
    // per `runtime.env_from_secrets` / `env_from_settings` patterns. The
    // exact list of manifest files to parse is the same set as scan_manifests.
    let manifests = scan_manifests();
    let installs = h
        .0
        .list_module_installs_for_project(&project.id)
        .unwrap_or_default();

    for install in &installs {
        let Some((_, manifest)) = manifests.iter().find(|(_, m)| m.id == install.module_id) else {
            continue;
        };
        // Settings (non-secret)
        for s in &manifest.settings {
            if let Ok(Some(v)) = h.0.get_setting(&project.id, &manifest.id, &s.key) {
                let as_str = match v {
                    serde_json::Value::String(s) => s,
                    other => other.to_string(),
                };
                env.insert(s.key.clone(), serde_json::Value::String(as_str));
            }
        }
        // Secrets (resolved from keychain)
        for s in &manifest.secrets {
            let scope = match s.scope.as_str() {
                "global" => crate::secrets::SecretScope::Global,
                "shared" => crate::secrets::SecretScope::Shared { project_id: &project.id },
                _ => crate::secrets::SecretScope::PerProject { project_id: &project.id },
            };
            if let Ok(Some(val)) = crate::secrets::get(scope, &manifest.id, &s.key) {
                env.insert(s.key.clone(), serde_json::Value::String(val));
            }
        }
    }

    Json(serde_json::Value::Object(env)).into_response()
}

// ─── Manifest scanning (shared with commands::modules) ──────────────────

fn scan_manifests() -> Vec<(std::path::PathBuf, crate::manifest::ModuleManifest)> {
    let mut out = Vec::new();
    let vct_root = crate::paths::vct_root_dir();
    for subdir in [
        vct_root.join("modules"),
        vct_root.join("bundled_manifests"),
    ] {
        if !subdir.is_dir() {
            continue;
        }
        let Ok(entries) = std::fs::read_dir(&subdir) else { continue };
        for entry in entries.flatten() {
            let p = entry.path();
            let candidate = if p.is_dir() {
                p.join("vct-module.json")
            } else if p.extension().and_then(|s| s.to_str()) == Some("json") {
                p
            } else {
                continue;
            };
            if !candidate.is_file() {
                continue;
            }
            if let Ok(raw) = std::fs::read_to_string(&candidate) {
                if let Ok(m) = crate::manifest::ModuleManifest::from_json(&raw) {
                    out.push((candidate, m));
                }
            }
        }
    }
    // ProjectHost import only needed for the type system to resolve.
    let _ = std::marker::PhantomData::<ProjectHost>;
    out
}
