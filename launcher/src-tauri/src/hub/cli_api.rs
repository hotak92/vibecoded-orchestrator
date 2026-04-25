//! CLI-focused hub routes.
//!
//! These routes back the `vct` CLI (P6). They expose operations that
//! were previously only callable via the GUI's Tauri commands. The
//! routes are bound to 127.0.0.1 only, like the rest of the hub.
//!
//! Design choice: rather than scatter endpoints across modules, the
//! CLI-facing routes live here as a single, self-documenting surface.
//! GUI endpoints elsewhere stay focused on the GUI's needs.
//!
//! Routes:
//!   POST   /cli/projects              — create a project
//!   PATCH  /cli/projects/{id|slug}    — rename a project
//!   DELETE /cli/projects/{id|slug}    — delete a project
//!   GET    /cli/audit                 — list audit events (filters via query)
//!   GET    /cli/license               — current tier / license info
//!   POST   /cli/license/activate      — activate a license key
//!   POST   /cli/license/deactivate    — clear local license state
//!   POST   /cli/license/refresh       — force tier re-validation
//!   GET    /cli/hooks/{project_id}    — list hooks for a project
//!   PATCH  /cli/hooks/{hook_id}       — toggle/edit a hook (project_id in body)
//!   GET    /cli/telemetry             — telemetry status
//!   POST   /cli/telemetry/consent     — set consent on/off

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{delete, get, patch, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use super::modules_api::LauncherDbHandle;
use crate::db::models::ProjectHost;

pub fn router() -> Router<LauncherDbHandle> {
    Router::new()
        // Projects
        .route("/cli/projects", post(create_project))
        .route("/cli/projects/{id_or_slug}", patch(rename_project).delete(delete_project))
        // Audit
        .route("/cli/audit", get(list_audit))
        // License
        .route("/cli/license", get(get_license))
        .route("/cli/license/activate", post(activate_license))
        .route("/cli/license/deactivate", post(deactivate_license))
        // Hooks
        .route("/cli/hooks/{project_id}", get(list_hooks_for_project))
        .route("/cli/hooks/{hook_id}/enabled", patch(set_hook_enabled))
        // Telemetry
        .route("/cli/telemetry", get(telemetry_status))
        .route("/cli/telemetry/consent", post(set_telemetry_consent))
}

// ─── Helpers ────────────────────────────────────────────────────────────

fn resolve_project<F>(h: &LauncherDbHandle, id_or_slug: &str, then: F) -> axum::response::Response
where
    F: FnOnce(crate::db::models::ProjectRow) -> axum::response::Response,
{
    let resolved = h
        .0
        .get_project(id_or_slug)
        .ok()
        .flatten()
        .or_else(|| h.0.get_project_by_slug(id_or_slug).ok().flatten());
    match resolved {
        Some(p) => then(p),
        None => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({ "error": "project not found" })),
        )
            .into_response(),
    }
}

// ─── Projects ───────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct CreateProjectReq {
    name: String,
    folder_path: String,
    host: String, // "base" | "mao"
}

#[derive(Serialize)]
struct ProjectOut {
    id: String,
    name: String,
    slug: String,
    folder_path: String,
    host: String,
    module_count: u32,
}

async fn create_project(
    State(h): State<LauncherDbHandle>,
    Json(req): Json<CreateProjectReq>,
) -> impl IntoResponse {
    let host = match ProjectHost::from_str(&req.host) {
        Some(h) => h,
        None => {
            return (
                StatusCode::BAD_REQUEST,
                Json(serde_json::json!({ "error": format!("invalid host: {}", req.host) })),
            )
                .into_response();
        }
    };
    let path = std::path::Path::new(&req.folder_path);
    if !path.is_dir() {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": "folder_path must be an existing directory" })),
        )
            .into_response();
    }
    let id = Uuid::new_v4().to_string();
    let slug = match h.0.generate_unique_slug(&req.name) {
        Ok(s) => s,
        Err(e) => return (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
    };
    let row = match h.0.insert_project(&id, &req.name, &req.folder_path, host.clone(), &slug) {
        Ok(r) => r,
        Err(e) => return (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
    };
    let _ = h.0.audit(
        "project_create",
        Some(&row.id),
        None,
        &serde_json::json!({ "host": host.as_str(), "name": req.name, "slug": slug, "via": "cli" }),
    );
    Json(ProjectOut {
        id: row.id,
        name: row.name,
        slug: row.slug,
        folder_path: row.folder_path,
        host: row.host.as_str().to_string(),
        module_count: 0,
    })
    .into_response()
}

#[derive(Deserialize)]
struct RenameReq {
    new_name: String,
}

async fn rename_project(
    State(h): State<LauncherDbHandle>,
    Path(id_or_slug): Path<String>,
    Json(req): Json<RenameReq>,
) -> axum::response::Response {
    resolve_project(&h, &id_or_slug, |project| {
        let new_slug = match h.0.generate_unique_slug(&req.new_name) {
            Ok(s) => s,
            Err(e) => return (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
        };
        if let Err(e) = h.0.rename_project(&project.id, &req.new_name, Some(&new_slug)) {
            return (StatusCode::INTERNAL_SERVER_ERROR, e).into_response();
        }
        let _ = h.0.audit(
            "project_rename",
            Some(&project.id),
            None,
            &serde_json::json!({ "old": project.name, "new": req.new_name, "via": "cli" }),
        );
        let row = match h.0.get_project(&project.id) {
            Ok(Some(r)) => r,
            _ => return (StatusCode::INTERNAL_SERVER_ERROR, "post-rename fetch failed").into_response(),
        };
        let count = h.0.list_module_installs_for_project(&row.id).map(|v| v.len() as u32).unwrap_or(0);
        Json(ProjectOut {
            id: row.id,
            name: row.name,
            slug: row.slug,
            folder_path: row.folder_path,
            host: row.host.as_str().to_string(),
            module_count: count,
        })
        .into_response()
    })
}

async fn delete_project(
    State(h): State<LauncherDbHandle>,
    Path(id_or_slug): Path<String>,
) -> axum::response::Response {
    resolve_project(&h, &id_or_slug, |project| {
        let _ = h.0.audit("project_delete", Some(&project.id), None, &serde_json::json!({ "via": "cli" }));
        match h.0.delete_project(&project.id) {
            Ok(()) => Json(serde_json::json!({ "ok": true })).into_response(),
            Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
        }
    })
}

// ─── Audit ──────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct AuditQuery {
    project_id: Option<String>,
    project_slug: Option<String>,
    since_ms: Option<i64>,
    limit: Option<u32>,
}

async fn list_audit(
    State(h): State<LauncherDbHandle>,
    Query(q): Query<AuditQuery>,
) -> impl IntoResponse {
    // Resolve slug -> id if requested
    let pid = if let Some(slug) = &q.project_slug {
        match h.0.get_project_by_slug(slug) {
            Ok(Some(p)) => Some(p.id),
            _ => {
                return (
                    StatusCode::NOT_FOUND,
                    Json(serde_json::json!({ "error": "project (slug) not found" })),
                )
                    .into_response()
            }
        }
    } else {
        q.project_id.clone()
    };
    let limit = q.limit.unwrap_or(200).min(1000);
    let _ = q.since_ms; // not currently supported by audit_list; CLI filters client-side
    match h.0.audit_list(pid.as_deref(), limit) {
        Ok(rows) => {
            let count = rows.len();
            Json(serde_json::json!({ "events": rows, "count": count })).into_response()
        }
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
    }
}

// ─── License ────────────────────────────────────────────────────────────

async fn get_license(State(h): State<LauncherDbHandle>) -> impl IntoResponse {
    match h.0.get_tier_cache() {
        Ok(tier) => Json(serde_json::json!({
            "orchestrator_tier": tier.orchestrator_tier,
            "module_licenses": tier.module_licenses,
            "last_validated": tier.last_validated,
            "last_error": tier.last_error,
        }))
        .into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
    }
}

#[derive(Deserialize)]
struct ActivateReq {
    key: String,
}

async fn activate_license(
    State(h): State<LauncherDbHandle>,
    Json(req): Json<ActivateReq>,
) -> impl IntoResponse {
    // Hub does NOT call the remote validate-tier function — that
    // requires Tauri-side network access with the configured Supabase
    // env. The CLI activation flow stores the key and queues a refresh
    // for the GUI to pick up. Document this in the CLI help text.
    let _ = h.0.audit(
        "license_activate",
        None,
        None,
        &serde_json::json!({ "via": "cli", "queued": true }),
    );
    // Persist the key to ~/.vct/license.key for the launcher to pick up.
    let key_path = directories::UserDirs::new()
        .map(|d| d.home_dir().join(".vct").join("license.key"));
    if let Some(p) = key_path {
        if let Some(parent) = p.parent() {
            std::fs::create_dir_all(parent).ok();
        }
        if let Err(e) = std::fs::write(&p, &req.key) {
            return (StatusCode::INTERNAL_SERVER_ERROR, format!("write license.key: {}", e)).into_response();
        }
    }
    Json(serde_json::json!({
        "ok": true,
        "queued": true,
        "note": "License key saved to ~/.vct/license.key. Open the launcher GUI to validate against the licensing service."
    }))
    .into_response()
}

async fn deactivate_license(State(h): State<LauncherDbHandle>) -> impl IntoResponse {
    let key_path = directories::UserDirs::new()
        .map(|d| d.home_dir().join(".vct").join("license.key"));
    if let Some(p) = key_path {
        let _ = std::fs::remove_file(&p);
    }
    let _ = h.0.audit("license_deactivate", None, None, &serde_json::json!({ "via": "cli" }));
    Json(serde_json::json!({ "ok": true })).into_response()
}

// ─── Hooks ──────────────────────────────────────────────────────────────

async fn list_hooks_for_project(
    State(h): State<LauncherDbHandle>,
    Path(id_or_slug): Path<String>,
) -> axum::response::Response {
    resolve_project(&h, &id_or_slug, |project| {
        match h.0.list_project_hooks(&project.id) {
            Ok(rows) => {
                let count = rows.len();
                Json(serde_json::json!({ "hooks": rows, "count": count })).into_response()
            }
            Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
        }
    })
}

#[derive(Deserialize)]
struct HookEnabledReq {
    project_id: Option<String>,
    enabled: bool,
}

async fn set_hook_enabled(
    State(h): State<LauncherDbHandle>,
    Path(hook_id): Path<i64>,
    Json(req): Json<HookEnabledReq>,
) -> impl IntoResponse {
    match h.0.set_project_hook_enabled(hook_id, req.enabled) {
        Ok(()) => {
            let _ = h.0.audit(
                "hook_set_enabled",
                req.project_id.as_deref(),
                None,
                &serde_json::json!({ "hook_id": hook_id, "enabled": req.enabled, "via": "cli" }),
            );
            Json(serde_json::json!({ "ok": true })).into_response()
        }
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
    }
}

// ─── Telemetry ──────────────────────────────────────────────────────────

async fn telemetry_status(_state: State<LauncherDbHandle>) -> impl IntoResponse {
    // Read consent from ~/.vct/telemetry.json — cheap, no DB call.
    let path = directories::UserDirs::new()
        .map(|d| d.home_dir().join(".vct").join("telemetry.json"));
    let consent = path
        .and_then(|p| std::fs::read_to_string(&p).ok())
        .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
        .and_then(|v| v.get("consent").and_then(|c| c.as_bool()))
        .unwrap_or(false);
    Json(serde_json::json!({ "consent": consent })).into_response()
}

#[derive(Deserialize)]
struct TelemetryConsentReq {
    consent: bool,
}

async fn set_telemetry_consent(
    State(h): State<LauncherDbHandle>,
    Json(req): Json<TelemetryConsentReq>,
) -> impl IntoResponse {
    let path = directories::UserDirs::new()
        .map(|d| d.home_dir().join(".vct").join("telemetry.json"));
    if let Some(p) = path {
        if let Some(parent) = p.parent() {
            std::fs::create_dir_all(parent).ok();
        }
        let body = serde_json::json!({ "consent": req.consent });
        if let Err(e) = std::fs::write(&p, body.to_string()) {
            return (StatusCode::INTERNAL_SERVER_ERROR, format!("write telemetry.json: {}", e)).into_response();
        }
    }
    let _ = h.0.audit(
        "telemetry_consent",
        None,
        None,
        &serde_json::json!({ "consent": req.consent, "via": "cli" }),
    );
    Json(serde_json::json!({ "ok": true, "consent": req.consent })).into_response()
}
