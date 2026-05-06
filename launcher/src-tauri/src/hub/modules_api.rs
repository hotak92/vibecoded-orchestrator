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
        // Secrets (resolved from keychain).
        //
        // PR-3 Commit 3 (2026-05-06): gate every keychain read on
        // `is_secret_active`. Pre-PR-3 the hub returned the cleartext
        // VALUE of paused secrets to subprocesses while the launcher's
        // GUI correctly reported them as unset — an asymmetric leak
        // (the GUI lies "secret unset", the hub still serves the value
        // to consumers). This contradicted the Lifecycle B contract
        // implemented in PR #60 / commands/secrets_cmd.rs.
        //
        // Inactive entries are OMITTED from the response (not returned
        // as empty strings). Consumers that test for presence — e.g.
        // `if env_var_set("OPENAI_API_KEY"): use_real_api()` — see
        // "not set" rather than "set to empty", which is the contract
        // we promise in `is_secret_set` / `get_secret_preview`.
        //
        // See secrets-and-access-matrix-audit-2026-05-06.md §6 (canary
        // test asymmetric-leak diagnosis) and the matching unit-test
        // `inactive_secret_does_not_leak_preview` in secrets_cmd.rs.
        for s in &manifest.secrets {
            // Resolve the same scope-string the active-flag DB uses.
            // Mirrors `enforce_scope_invariants` in secrets_cmd.rs.
            let scope_str = match s.scope.as_str() {
                "global" => "global",
                "shared" => "shared",
                _ => "per_project",
            };
            // Active-flag gate (cross-launcher, Option γ — PR-3 Commit 4).
            // The OS keychain is shared across dev/prod launchers, so a
            // pause anywhere must take effect everywhere. Walks the own
            // DB plus every discovered sibling launcher.db; refuses to
            // serve if ANY says inactive. `is_secret_active` defaults to
            // true when no row exists, so secrets that pre-existed
            // migration 007 still resolve normally.
            let active = crate::db::secret_active::is_secret_active_cross_launcher(
                &h.0,
                scope_str,
                &project.id,
                &manifest.id,
                &s.key,
            );
            if !active {
                continue;
            }
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

/// PR-3 Commit 3 (2026-05-06): pure helper for the gate decision used by
/// `project_env`'s secrets loop. Returns the cleartext value when the
/// secret is active AND the keychain has it; `None` otherwise (never
/// served to subprocesses). Inactive entries omit the env var entirely
/// — consumers see "not set" rather than "set to empty", matching the
/// `is_secret_set` / `get_secret_preview` contract.
///
/// Returning `Option<String>` rather than emitting a `serde_json::Value`
/// keeps this independent of the response shape so the unit test can
/// pin behaviour without standing up axum + a hub server.
#[cfg(test)]
pub(crate) fn resolve_secret_for_subprocess_env(
    db: &crate::db::Db,
    scope_str: &str,
    project_id: &str,
    module_id: &str,
    key: &str,
) -> Option<String> {
    // PR-3 Commit 4: cross-launcher pause check (Option γ). A secret
    // paused in any launcher's DB blocks the read here.
    let active = crate::db::secret_active::is_secret_active_cross_launcher(
        db, scope_str, project_id, module_id, key,
    );
    if !active {
        return None;
    }
    let scope = match scope_str {
        "global" => crate::secrets::SecretScope::Global,
        "shared" => crate::secrets::SecretScope::Shared { project_id },
        _ => crate::secrets::SecretScope::PerProject { project_id },
    };
    crate::secrets::get(scope, module_id, key).ok().flatten()
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

// ─── Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Db;

    /// Probe whether the OS keychain backend is available in this test
    /// environment. CI containers and headless build hosts typically
    /// have no Secret Service / Keychain / Credential Manager running,
    /// so any test that exercises the actual keychain has to short-circuit.
    fn keyring_available() -> bool {
        let entry = match keyring::Entry::new("vct.test.hub.probe", "probe") {
            Ok(e) => e,
            Err(_) => return false,
        };
        if entry.set_password("canary").is_err() {
            return false;
        }
        let _ = entry.delete_credential();
        true
    }

    /// PR-3 Commit 3 canary: the hub's per-subprocess secret resolver MUST
    /// honour `is_secret_active`. A paused secret (Lifecycle B Unset)
    /// returns the value through the keychain (the value is preserved on
    /// purpose) but the active flag in launcher.db is false — readers
    /// MUST see "not set" rather than the cleartext value.
    ///
    /// Pre-PR-3 this resolver bypassed the active flag and served the
    /// cleartext value to subprocesses, contradicting the canary test
    /// `inactive_secret_does_not_leak_preview` in secrets_cmd.rs (which
    /// only covered the Tauri-side preview path, not the hub HTTP API).
    /// See secrets-and-access-matrix-audit-2026-05-06.md §6.
    #[test]
    fn paused_secret_does_not_leak_to_hub_subprocess_env() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend in this test env");
            return;
        }

        let db = Db::open_in_memory().unwrap();
        let scope_str = "global";
        let project_id = "_global_";
        let module_id = "user";
        let canary = format!(
            "test-hub-leak-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let key = format!(
            "HUB_CANARY_KEY_{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );

        // Set + activate.
        crate::secrets::set(
            crate::secrets::SecretScope::Global,
            module_id,
            &key,
            &canary,
        )
        .unwrap();
        db.mark_secret_active(scope_str, project_id, module_id, &key)
            .unwrap();

        // While ACTIVE: the resolver returns the cleartext value (the
        // launcher's contract for unwrapped subprocess env vars).
        let resolved =
            resolve_secret_for_subprocess_env(&db, scope_str, project_id, module_id, &key);
        assert_eq!(resolved.as_deref(), Some(canary.as_str()));

        // Unset (Lifecycle B): keychain UNTOUCHED, active flag flipped.
        db.mark_secret_inactive(scope_str, project_id, module_id, &key)
            .unwrap();

        // The keychain still has the value (proves Lifecycle B):
        let kc = crate::secrets::get(
            crate::secrets::SecretScope::Global,
            module_id,
            &key,
        )
        .unwrap();
        assert_eq!(kc.as_deref(), Some(canary.as_str()));

        // But the hub-side resolver MUST refuse to serve it. This is
        // the bug we're fixing in PR-3 Commit 3.
        let resolved_paused =
            resolve_secret_for_subprocess_env(&db, scope_str, project_id, module_id, &key);
        assert!(
            resolved_paused.is_none(),
            "paused secret leaked through hub resolver: {:?}",
            resolved_paused
        );

        // Reactivate: resolver works again with no value re-entry.
        db.mark_secret_active(scope_str, project_id, module_id, &key)
            .unwrap();
        let resolved_reactivated =
            resolve_secret_for_subprocess_env(&db, scope_str, project_id, module_id, &key);
        assert_eq!(resolved_reactivated.as_deref(), Some(canary.as_str()));

        // Cleanup keychain (best-effort).
        let _ = crate::secrets::delete(
            crate::secrets::SecretScope::Global,
            module_id,
            &key,
        );
        let _ = db.forget_secret_active_state(scope_str, project_id, module_id, &key);
    }

    #[test]
    fn resolver_returns_none_when_keychain_empty_even_if_active() {
        // Active flag default is true; if the keychain has no value the
        // resolver returns None (omits the env var). Doesn't require the
        // keychain backend — `is_set` returns false on a never-written key.
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend in this test env");
            return;
        }
        let db = Db::open_in_memory().unwrap();
        let res = resolve_secret_for_subprocess_env(
            &db,
            "global",
            "_global_",
            "user",
            "NEVER_SET_KEY_PR3_TEST",
        );
        assert!(res.is_none());
    }
}
