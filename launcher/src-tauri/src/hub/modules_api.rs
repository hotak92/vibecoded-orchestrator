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

/// Sentinel project_id used by the launcher when scope is `shared`.
/// Mirrors `commands::secrets_cmd::SENTINEL_SHARED` (which is private to
/// that module). Pinned here as a module-private const because the hub's
/// `/projects/{id}/env` resolver needs to look up shared-scope keychain
/// entries at this fixed slot — the same slot
/// `commands::installer::register_github_pat` writes to and the same slot
/// the SecretsPanel "Shared (this user)" tab targets.
///
/// 0.1.7 fork-readiness sweep (item H1, 2026-05-08): pre-fix, this
/// resolver passed `&project.id` (the real UUID) into
/// `SecretScope::Shared { project_id }`, which produced a per-project
/// keychain service-name (`vct.<UUID>.shared.<module>`). That was
/// inconsistent with everything else in the launcher: writers (the
/// SecretsPanel + `register_github_pat`) put shared secrets at
/// `vct._user_shared_.shared.<module>`, but this reader looked at
/// `vct.<UUID>.shared.<module>` — guaranteed miss. The fix: route every
/// `Shared`-scope keychain lookup through SENTINEL_SHARED, matching the
/// writer side. Per-project shared entries (legacy, before SENTINEL_SHARED
/// existed) are no longer reachable via this resolver, but no in-tree
/// code path writes that shape after PR #60.
const SENTINEL_SHARED: &str = "_user_shared_";

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
        .route("/projects/by-path", get(get_project_by_path_route))
}

// ─── Error envelope ─────────────────────────────────────────────────────
//
// Stable JSON error shape consumed by the bundled secrets-resolver helper
// (`templates/scripts/vct_secrets_resolve.sh|.ps1`) and any third-party
// caller. Errors used to be raw strings (returned as `text/plain`) which
// forced consumers to read the HTTP status to disambiguate. The envelope
// makes the failure mode machine-parseable without breaking 200-OK
// responses (those keep their existing flat-object shape).

fn error_response(status: StatusCode, code: &str, message: impl Into<String>) -> axum::response::Response {
    (
        status,
        Json(serde_json::json!({
            "error": {
                "code": code,
                "message": message.into(),
            }
        })),
    )
        .into_response()
}

/// Internal-server-error helper that takes a `String` from the DB layer,
/// logs it for the launcher operator, and returns a generic envelope to
/// the caller. Never echoes the raw DB error verbatim — those messages
/// can include path prefixes, schema hints, or sqlite filenames that we
/// don't want to leak across the localhost boundary even on 127.0.0.1.
fn db_error_response(context: &str, raw: String) -> axum::response::Response {
    eprintln!("[vct-hub] {} failed: {}", context, raw);
    error_response(
        StatusCode::INTERNAL_SERVER_ERROR,
        "internal_error",
        format!("{} failed", context),
    )
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
        Err(e) => db_error_response("list module installs", e),
    }
}

async fn module_status(
    State(h): State<LauncherDbHandle>,
    Path(module_id): Path<String>,
    Query(q): Query<StatusQuery>,
) -> impl IntoResponse {
    match h.0.get_module_install(&q.project_id, &module_id) {
        Ok(Some(row)) => Json(InstalledRowView::from(&row)).into_response(),
        Ok(None) => error_response(
            StatusCode::NOT_FOUND,
            "module_not_installed",
            format!("module {} not installed for project {}", module_id, q.project_id),
        ),
        Err(e) => db_error_response("get module install", e),
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
        Err(e) => db_error_response("list projects", e),
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
        Ok(None) => error_response(
            StatusCode::NOT_FOUND,
            "project_not_found",
            format!("project {} not found", project_id),
        ),
        Err(e) => db_error_response("get project", e),
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
        Ok(None) => error_response(
            StatusCode::NOT_FOUND,
            "project_not_found",
            format!("project with slug {:?} not found", slug),
        ),
        Err(e) => db_error_response("get project by slug", e),
    }
}

#[derive(Debug, Deserialize)]
struct ByPathQuery {
    path: String,
}

/// Resolve a folder path → registered project. Used by bundled wrappers
/// that know their cwd but not the project's hex UUID. Matches by the
/// canonical (lossless-canonicalized when possible) absolute path; falls
/// back to a literal-string match when canonicalization fails (e.g. the
/// path doesn't exist on disk anymore but the project is still registered
/// in launcher.db with the original path).
///
/// Returns 404 when no project owns the given path; 400 when the path
/// query parameter is missing or empty.
async fn get_project_by_path_route(
    State(h): State<LauncherDbHandle>,
    Query(q): Query<ByPathQuery>,
) -> impl IntoResponse {
    let raw = q.path.trim();
    if raw.is_empty() {
        return error_response(
            StatusCode::BAD_REQUEST,
            "missing_path",
            "query parameter `path` must be a non-empty absolute path",
        );
    }

    let projects = match h.0.list_projects() {
        Ok(p) => p,
        Err(e) => return db_error_response("list projects (by-path)", e),
    };

    let canonical_query = std::fs::canonicalize(raw)
        .ok()
        .and_then(|p| p.to_str().map(|s| s.to_string()));

    let matched = projects.iter().find(|p| {
        if p.folder_path == raw {
            return true;
        }
        // Best-effort canonical match — bridges the case where the
        // launcher stored a non-canonical path (e.g. with a trailing
        // slash, a symlinked prefix, or a relative-to-home variant).
        if let Some(qcan) = canonical_query.as_deref() {
            if let Ok(reg_can) = std::fs::canonicalize(&p.folder_path) {
                if reg_can.to_str() == Some(qcan) {
                    return true;
                }
            }
        }
        false
    });

    match matched {
        Some(row) => {
            let count = h
                .0
                .list_module_installs_for_project(&row.id)
                .map(|v| v.len() as u32)
                .unwrap_or(0);
            Json(ProjectSummary::from_row(row, count)).into_response()
        }
        None => error_response(
            StatusCode::NOT_FOUND,
            "project_not_found",
            format!("no registered project at path {:?}", raw),
        ),
    }
}

/// Return the merged env dict the launcher would inject into a workflow
/// running in this project. Secrets are resolved from the keychain;
/// settings from the DB.
///
/// ─── Response contract (stable, consumed by `vct_secrets_resolve`) ───
///
/// On success (200): a flat JSON object mapping env-var names to string
/// values. Always includes:
///   * `VCT_PROJECT_ID`   — the project's UUID
///   * `VCT_PROJECT_HOST` — the host kind (`base`, etc.)
///   * `VCT_PROJECT_PATH` — the folder path on disk
///
/// Plus, for every module installed for this project, every (key,
/// value) pair the manifest declares as `settings` or `secrets` AND
/// (for secrets) is `active=true` per the cross-launcher active-flag
/// gate. Inactive secrets are OMITTED (not present as null / empty
/// string). Consumers that test for presence get a clean "not set"
/// signal rather than "set to empty".
///
/// On failure: a JSON envelope `{error: {code, message}}` with the
/// HTTP status set:
///   * 404 `project_not_found`     — no project with the given id
///   * 500 `internal_error`        — DB read failed (logged on the
///                                   launcher side; opaque to caller)
///
/// Filtering by query parameter:
///   * `?key=NAME` — return only that single env var (still wrapped
///     in the flat-object shape). Returns 404 if the project exists
///     but the key isn't active for it. Useful for the resolver
///     helper which needs a single value per call.
///
/// **Security**: this endpoint returns secret values in cleartext. It is
/// bound to 127.0.0.1 only. Apps that consume it (e.g. the orchestrator
/// launching a workflow) run on the same machine as the user.
///
/// Future work (tracked in LAUNCHER_BACKEND_API.md §10): require a caller
/// auth token so scripts running under a different user can't siphon
/// secrets via a local port scan.
#[derive(Debug, Deserialize, Default)]
struct ProjectEnvQuery {
    /// Optional single-key filter. When set, the response only includes
    /// the named env var (returning 404 if it isn't active for this
    /// project). Used by the `vct_secrets_resolve` helper.
    key: Option<String>,
}

async fn project_env(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
    Query(q): Query<ProjectEnvQuery>,
) -> impl IntoResponse {
    let project = match h.0.get_project(&project_id) {
        Ok(Some(p)) => p,
        Ok(None) => {
            return error_response(
                StatusCode::NOT_FOUND,
                "project_not_found",
                format!("project {} not found", project_id),
            );
        }
        Err(e) => return db_error_response("get project (env)", e),
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
            // 0.1.7 fork-readiness sweep (item H1): the active-flag gate
            // and the keychain lookup MUST use the same `project_id` slot
            // the writer used. For shared scope that's SENTINEL_SHARED
            // (`_user_shared_`); for global it's SENTINEL_GLOBAL
            // (`_global_`); for per-project it's the real project UUID.
            // Pre-H1 this code path passed `&project.id` for shared scope
            // too, which yielded a per-project keychain key the writers
            // never touched — guaranteed miss. See module-level
            // `SENTINEL_SHARED` doc-comment for the full rationale.
            let lookup_project_id: &str = match s.scope.as_str() {
                "global" => "_global_",
                "shared" => SENTINEL_SHARED,
                _ => &project.id,
            };
            // Active-flag gate (cross-launcher, per-requester — 0.2.1
            // migration 009). The consuming project's id is the requester
            // so a per-project pause on a shared/global secret takes
            // effect even though the keychain row is shared. The
            // `_for_requester` variant follows the same lookup contract
            // as the legacy gate (literal-requester row → `*` sentinel
            // fallback → default-active when no row exists), so secrets
            // that pre-date migration 009 still resolve normally.
            let active = crate::db::secret_active::is_secret_active_cross_launcher_for_requester(
                &h.0,
                scope_str,
                lookup_project_id,
                &manifest.id,
                &s.key,
                &project.id,
            );
            if !active {
                continue;
            }
            let scope = match s.scope.as_str() {
                "global" => crate::secrets::SecretScope::Global,
                "shared" => crate::secrets::SecretScope::Shared { project_id: SENTINEL_SHARED },
                _ => crate::secrets::SecretScope::PerProject { project_id: &project.id },
            };
            if let Ok(Some(val)) = crate::secrets::get(scope, &manifest.id, &s.key) {
                env.insert(s.key.clone(), serde_json::Value::String(val));
            }
        }
    }

    // 0.1.7 fork-readiness sweep (item H1, 2026-05-08): also process the
    // orchestrator's own `vct-module.json::bundled_secrets` block. The
    // orchestrator core is not installable as a module — it IS the
    // launcher — so it has no row in `module_installs`, but it still
    // needs to declare the secrets the launcher itself manages
    // (`github_pat` from the OnboardingWizard, etc.) so the hub can
    // resolve them for every base-host project without the user having
    // to install a separate module first.
    //
    // Same scope-string + active-flag-gate + keychain-scope mapping as
    // the per-module loop above — kept inline rather than factored into
    // a helper so the two code paths stay byte-comparable in code review.
    // The deduplication step prevents an orchestrator-bundled key from
    // overwriting an installed module's value (an installed module's
    // declaration takes precedence — the user explicitly opted into it).
    if let Some(orch_manifest) = crate::commands::modules::read_orchestrator_manifest() {
        for bs in &orch_manifest.bundled_secrets {
            // Skip if an installed module already populated this key. Pins
            // installed-module-wins so the orchestrator's bundled
            // declarations are a default, not an override.
            if env.contains_key(&bs.key) {
                continue;
            }
            let scope_str = match bs.scope.as_str() {
                "global" => "global",
                "shared" => "shared",
                _ => "per_project",
            };
            let lookup_project_id: &str = match bs.scope.as_str() {
                "global" => "_global_",
                "shared" => SENTINEL_SHARED,
                _ => &project.id,
            };
            let active = crate::db::secret_active::is_secret_active_cross_launcher_for_requester(
                &h.0,
                scope_str,
                lookup_project_id,
                &bs.module_id,
                &bs.key,
                &project.id,
            );
            let scope = match bs.scope.as_str() {
                "global" => crate::secrets::SecretScope::Global,
                "shared" => crate::secrets::SecretScope::Shared { project_id: SENTINEL_SHARED },
                _ => crate::secrets::SecretScope::PerProject { project_id: &project.id },
            };
            if active {
                if let Ok(Some(val)) = crate::secrets::get(scope, &bs.module_id, &bs.key) {
                    if !val.trim().is_empty() {
                        env.insert(bs.key.clone(), serde_json::Value::String(val));
                        continue;
                    }
                }
            }

            // 2026-05-10 (post-0.2.0 backlog #6): legacy slot fallback
            // for `github_pat`. 0.2.0 launchers wrote the wizard PAT at
            // `shared.installer/github_pat`; post-fix the canonical
            // path is `shared.user/github_pat`. Until the user runs
            // `register_github_pat` again (which triggers
            // `migrate_github_pat_installer_to_user_module_id` to
            // consolidate the slots), reads through the hub fall back
            // to the legacy slot so existing tokens stay reachable
            // across the upgrade. Once the migration has run the
            // legacy slot is empty and this branch is a no-op.
            if bs.scope == "shared" && bs.key == "github_pat" && bs.module_id == "user" {
                let legacy_module_id = "installer";
                let legacy_active = crate::db::secret_active::is_secret_active_cross_launcher_for_requester(
                    &h.0,
                    scope_str,
                    lookup_project_id,
                    legacy_module_id,
                    &bs.key,
                    &project.id,
                );
                if legacy_active {
                    if let Ok(Some(val)) = crate::secrets::get(scope, legacy_module_id, &bs.key) {
                        if !val.trim().is_empty() {
                            env.insert(bs.key.clone(), serde_json::Value::String(val));
                        }
                    }
                }
            }
        }
    }

    // 0.2.1: cross-project grants resolution (migration 009 § secret_grants).
    //
    // Walk every grant where this project is the GRANTEE — i.e. another
    // project (the OWNER) has explicitly granted us read access to one
    // of its per_project secrets. Each grant row resolves to a keychain
    // read against the OWNER's per_project bucket, gated on the same
    // per-(secret × requester) active flag the owned-secrets loop above
    // uses. The grantee can self-opt-out by pausing the secret for its
    // own project_id — the grant row stays so the owner sees "B has
    // paused this grant" without losing the relationship.
    //
    // Grants are `per_project` scope only by schema (the CHECK on
    // secret_grants enforces this), so we don't need to dispatch on
    // `scope` here — the keychain read is always against
    // `SecretScope::PerProject { project_id: <owner> }`.
    //
    // Precedence: an installed-module declaration with the same key
    // wins (the `if env.contains_key` guard mirrors the orchestrator-
    // bundled loop above). Same rationale: explicit module declarations
    // are an opt-in contract; grants are an additive sharing surface.
    if let Ok(grants) = h.0.list_grants_by_grantee(&project.id) {
        for g in &grants {
            if env.contains_key(&g.key) {
                continue;
            }
            let active = crate::db::secret_active::is_secret_active_cross_launcher_for_requester(
                &h.0,
                "per_project",
                &g.owner_project_id,
                &g.module_id,
                &g.key,
                &project.id,
            );
            if !active {
                continue;
            }
            let scope = crate::secrets::SecretScope::PerProject {
                project_id: &g.owner_project_id,
            };
            if let Ok(Some(val)) = crate::secrets::get(scope, &g.module_id, &g.key) {
                env.insert(g.key.clone(), serde_json::Value::String(val));
            }
        }
    }

    // Optional single-key filter — used by the `vct_secrets_resolve`
    // helper so it doesn't have to ship the full env dict over the
    // wire (and so a missing key gets a clean 404 rather than the
    // helper having to grep the map itself).
    if let Some(want) = q.key.as_deref() {
        let want = want.trim();
        if want.is_empty() {
            return error_response(
                StatusCode::BAD_REQUEST,
                "invalid_key",
                "query parameter `key` must be non-empty",
            );
        }
        return match env.get(want) {
            Some(v) => {
                let mut single = serde_json::Map::new();
                single.insert(want.to_string(), v.clone());
                Json(serde_json::Value::Object(single)).into_response()
            }
            None => error_response(
                StatusCode::NOT_FOUND,
                "key_not_active",
                format!(
                    "key {:?} is not active for project {} (not declared by any installed module, \
                     or paused via the secret active-flag)",
                    want, project.id
                ),
            ),
        };
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

    // ─── HTTP-level endpoint tests (post-Fix-#3 hardening, 0.1.7) ─────
    //
    // These tests bring up the modules_api router on a random local port
    // and exercise the response contract via real HTTP requests. They
    // pin:
    //   * `GET /projects/{id}/env` returns the documented flat-object
    //     shape with VCT_PROJECT_* keys baked in.
    //   * `GET /projects/{id}/env` returns a 404 + JSON envelope for
    //     unknown projects (not a raw string body).
    //   * `GET /projects/{id}/env?key=NAME` returns 404 +
    //     `key_not_active` envelope when the key isn't active for the
    //     project (no installed module declares it).
    //   * `GET /projects/by-path?path=...` resolves a folder path to a
    //     registered project; returns 404 envelope when no match.
    //   * `GET /projects/by-path` (no path) returns 400 + `missing_path`.
    //
    // Pattern mirrors `cli_api.rs::cli_kg_integration_tests::spawn_test_hub`.
    // Tests that would need to set up keychain values to verify secret
    // resolution are intentionally omitted — that path is already
    // covered by `paused_secret_does_not_leak_to_hub_subprocess_env`
    // above, which tests the same gate at a lower level without
    // requiring a real keychain backend in CI.

    use axum::Router;
    use std::sync::Arc;

    async fn spawn_modules_api_hub() -> (String, LauncherDbHandle) {
        let db = Db::open_in_memory().expect("in-memory db");
        let handle = LauncherDbHandle(Arc::new(db));
        let app: Router =
            Router::new().nest("/api/v1", super::router().with_state(handle.clone()));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("local_addr");
        tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        (format!("http://{}/api/v1", addr), handle)
    }

    fn seed_project(db: &Db, id: &str, name: &str, folder: &str) {
        let now = chrono::Utc::now().timestamp_millis();
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                 VALUES (?1, ?2, ?3, 'base', ?2, ?4, ?4)",
                rusqlite::params![id, name, folder, now],
            )
            .unwrap();
    }

    #[tokio::test]
    async fn project_env_returns_404_envelope_for_unknown_project() {
        let (base, _h) = spawn_modules_api_hub().await;
        let resp = reqwest::get(format!("{}/projects/ghost-id/env", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 404);
        let body: serde_json::Value = resp.json().await.expect("json body");
        let err = body.get("error").expect("error envelope");
        assert_eq!(
            err.get("code").and_then(|v| v.as_str()),
            Some("project_not_found"),
            "expected project_not_found envelope, got: {}",
            body
        );
        // The envelope MUST carry a non-empty message so wrapper scripts
        // can surface it to the user without bespoke string formatting.
        assert!(err
            .get("message")
            .and_then(|v| v.as_str())
            .map(|s| !s.is_empty())
            .unwrap_or(false));
    }

    #[tokio::test]
    async fn project_env_includes_baked_in_vct_project_keys_for_registered_project() {
        let (base, h) = spawn_modules_api_hub().await;
        seed_project(&h.0, "p-test-1", "Test Project", "/tmp/test-project-1");

        let resp = reqwest::get(format!("{}/projects/p-test-1/env", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        // Documented baked-in keys.
        assert_eq!(
            body.get("VCT_PROJECT_ID").and_then(|v| v.as_str()),
            Some("p-test-1")
        );
        assert_eq!(
            body.get("VCT_PROJECT_PATH").and_then(|v| v.as_str()),
            Some("/tmp/test-project-1")
        );
        assert_eq!(
            body.get("VCT_PROJECT_HOST").and_then(|v| v.as_str()),
            Some("base")
        );
        // No installed modules → no other keys, but the structure must
        // still be a flat object (not an array, not a wrapped envelope).
        assert!(body.is_object());
    }

    #[tokio::test]
    async fn project_env_filter_by_key_returns_404_when_key_not_active() {
        let (base, h) = spawn_modules_api_hub().await;
        seed_project(&h.0, "p-test-2", "Test Project Two", "/tmp/test-project-2");

        // Asking for a never-declared key → 404 with key_not_active envelope.
        let resp = reqwest::get(format!(
            "{}/projects/p-test-2/env?key=GITHUB_TOKEN",
            base
        ))
        .await
        .expect("hub reachable");
        assert_eq!(resp.status(), 404);
        let body: serde_json::Value = resp.json().await.expect("json body");
        let err = body.get("error").expect("error envelope");
        assert_eq!(
            err.get("code").and_then(|v| v.as_str()),
            Some("key_not_active")
        );
    }

    #[tokio::test]
    async fn project_env_filter_by_key_returns_baked_in_vct_project_id() {
        // The baked-in VCT_PROJECT_* keys are always "active" for a
        // registered project, so a key=VCT_PROJECT_ID query MUST round-trip
        // a 200-OK with that single key — proves the filter path works
        // end-to-end without needing a manifest declaration.
        let (base, h) = spawn_modules_api_hub().await;
        seed_project(&h.0, "p-test-3", "Test Project Three", "/tmp/test-project-3");

        let resp = reqwest::get(format!(
            "{}/projects/p-test-3/env?key=VCT_PROJECT_ID",
            base
        ))
        .await
        .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(
            body.get("VCT_PROJECT_ID").and_then(|v| v.as_str()),
            Some("p-test-3")
        );
        // Filter mode → ONLY the requested key, no VCT_PROJECT_HOST or
        // VCT_PROJECT_PATH bleed-through.
        assert!(
            body.get("VCT_PROJECT_PATH").is_none(),
            "filter leaked VCT_PROJECT_PATH: {}",
            body
        );
    }

    #[tokio::test]
    async fn project_env_filter_rejects_empty_key_with_400() {
        let (base, h) = spawn_modules_api_hub().await;
        seed_project(&h.0, "p-test-4", "Test Project Four", "/tmp/test-project-4");
        let resp = reqwest::get(format!(
            "{}/projects/p-test-4/env?key=",
            base
        ))
        .await
        .expect("hub reachable");
        assert_eq!(resp.status(), 400);
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(
            body.get("error")
                .and_then(|e| e.get("code"))
                .and_then(|v| v.as_str()),
            Some("invalid_key")
        );
    }

    #[tokio::test]
    async fn by_path_resolves_registered_project_to_id() {
        let (base, h) = spawn_modules_api_hub().await;
        // Use a tempdir so canonicalize works even on macOS where
        // /tmp is a symlink to /private/tmp.
        let tmp = std::env::temp_dir().join(format!(
            "vct-by-path-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        let folder = tmp.to_string_lossy().to_string();
        seed_project(&h.0, "p-bypath", "By Path", &folder);

        let resp = reqwest::Client::new()
            .get(format!("{}/projects/by-path", base))
            .query(&[("path", folder.as_str())])
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(body.get("id").and_then(|v| v.as_str()), Some("p-bypath"));
        assert_eq!(
            body.get("folder_path").and_then(|v| v.as_str()),
            Some(folder.as_str())
        );

        // Cleanup — best effort.
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[tokio::test]
    async fn by_path_returns_404_envelope_for_unregistered_path() {
        let (base, _h) = spawn_modules_api_hub().await;
        let resp = reqwest::Client::new()
            .get(format!("{}/projects/by-path", base))
            .query(&[("path", "/nonexistent/never-registered/path")])
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 404);
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(
            body.get("error")
                .and_then(|e| e.get("code"))
                .and_then(|v| v.as_str()),
            Some("project_not_found")
        );
    }

    #[tokio::test]
    async fn by_path_returns_400_when_path_query_missing_or_empty() {
        let (base, _h) = spawn_modules_api_hub().await;
        // Empty path.
        let resp = reqwest::Client::new()
            .get(format!("{}/projects/by-path", base))
            .query(&[("path", "")])
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 400);
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(
            body.get("error")
                .and_then(|e| e.get("code"))
                .and_then(|v| v.as_str()),
            Some("missing_path")
        );
    }

    // ─── H1 (0.1.7 fork-readiness sweep, 2026-05-08) ──────────────────────
    //
    // The resolver path was returning `key_not_active` for `github_pat`
    // because two architectural mismatches lined up:
    //
    //   1. The hub's `project_env` resolver passed `&project.id` (the
    //      real UUID) into `SecretScope::Shared { project_id }`, producing
    //      a keychain service-name `vct.<UUID>.shared.<module>`. But
    //      the writer side (`commands::installer::register_github_pat`,
    //      the SecretsPanel "Shared (this user)" tab) writes at
    //      `vct._user_shared_.shared.<module>`. Guaranteed miss.
    //
    //      (post-0.2.0 backlog #6, 2026-05-10): the `<module>` segment
    //      itself was also divergent — `register_github_pat` wrote
    //      `installer/`, the SecretsPanel "Shared" tab wrote `user/`.
    //      Both writers now use `user/` (`GITHUB_PAT_MODULE_ID`); the
    //      hub-side bundled_secrets manifest entry below also pins
    //      `module_id="user"` so the lookup tuple matches.
    //
    //   2. Even after fixing the lookup-slot, the orchestrator core had
    //      no `secrets[]` declarations the hub could iterate over —
    //      `vct-module.json` is parsed as the slim `OrchestratorManifest`
    //      shape, not the full `ModuleManifest`. Without an installed
    //      `vct-search` row, the hub had no manifest entry to match.
    //
    // H1 fixes both:
    //   * Hub maps `scope='shared'` keychain lookups to SENTINEL_SHARED
    //     (`_user_shared_`) — see `SENTINEL_SHARED` const at the top of
    //     this module.
    //   * `OrchestratorManifest::bundled_secrets` lets the orchestrator
    //     core declare its own secrets the hub iterates alongside
    //     installed-module manifests.
    //
    // These tests pin both halves: the keychain-lookup tests use the
    // pure helper `resolve_secret_for_subprocess_env` (which no longer
    // requires a project_id matching the writer's slot — the shared
    // slot semantics live up at the manifest-loop level), and the
    // HTTP tests use the in-tree `vct-module.json` (which `find_orchestrator_manifest`
    // resolves by walking up from `current_exe()` — in tests that
    // lands at the repo root just like in prod).
    //
    // Test isolation: every test below writes to the SAME keychain slot
    // (`vct._user_shared_.shared.user/github_pat` — post-2026-05-10;
    // pre-fix this was `installer/github_pat`). That's the slot
    // `vct-module.json::bundled_secrets` declares, and we can't use a
    // different slot without forking the JSON for tests. Tests
    // serialise via the process-wide
    // `crate::secrets::test_serialize::keychain_serialize_lock`, which
    // is the SAME mutex used by `commands::installer::github_pat_keychain_tests`
    // and `commands::dashboard::tests`. That closes the cross-module
    // race where parallel keychain writes to the same slot would
    // overwrite each other's canaries.

    /// Acquire the process-wide keychain mutex. See
    /// `crate::secrets::test_serialize::keychain_serialize_lock` for
    /// the rationale.
    fn h1_lock() -> std::sync::MutexGuard<'static, ()> {
        crate::secrets::test_serialize::keychain_serialize_lock()
    }

    /// Helper: write a value into the OS keychain at the SENTINEL_SHARED
    /// slot the H1 fix uses. Only call from keyring-available test paths.
    fn write_shared_keychain_canary(module_id: &str, key: &str, value: &str) {
        crate::secrets::set(
            crate::secrets::SecretScope::Shared {
                project_id: "_user_shared_",
            },
            module_id,
            key,
            value,
        )
        .expect("keychain set");
    }

    /// Helper: clean up a keychain entry written by `write_shared_keychain_canary`.
    fn delete_shared_keychain_canary(module_id: &str, key: &str) {
        let _ = crate::secrets::delete(
            crate::secrets::SecretScope::Shared {
                project_id: "_user_shared_",
            },
            module_id,
            key,
        );
    }

    /// H1 (item H1, 2026-05-08): the hub's `/projects/{id}/env?key=github_pat`
    /// must resolve through the keychain entry the OnboardingWizard +
    /// SecretsPanel "Shared (this user)" tab write to (`vct._user_shared_.
    /// shared.user/github_pat`, post-2026-05-10 unification — pre-fix
    /// this was `installer/github_pat`), surfaced via the orchestrator's
    /// `vct-module.json::bundled_secrets` declaration.
    ///
    /// Pre-fix: returned 404 + `key_not_active` because the resolver
    /// looked at `vct.<project.id>.shared.<...>/github_pat` (a
    /// per-project slot the writer never touches).
    ///
    /// Skipped without an OS keychain backend (CI containers).
    #[tokio::test]
    async fn hub_project_env_resolves_shared_secret_via_sentinel_keychain() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend in this test env");
            return;
        }
        let _lock = h1_lock();

        // Best-effort cleanup from any prior test that crashed before
        // its tail cleanup ran — `delete` returns Ok on NoEntry.
        delete_shared_keychain_canary("user", "github_pat");

        // Fresh canary so a leftover from a previous run doesn't
        // accidentally pass the test. A timestamp suffix keeps it unique.
        let ts = chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0);
        let canary = format!("h1-shared-resolver-canary-{}", ts);
        // Write at the SENTINEL_SHARED slot — same shape the launcher's
        // writer uses.
        write_shared_keychain_canary("user", "github_pat", &canary);

        let (base, h) = spawn_modules_api_hub().await;
        seed_project(&h.0, "h1-proj", "H1 Test Project", "/tmp/h1-test-project");

        // The orchestrator's `vct-module.json` (resolved by
        // `find_orchestrator_manifest()` walking up from current_exe())
        // declares `bundled_secrets[].key = "github_pat", scope =
        // "shared", module_id = "user"`. The hub iterates that
        // list during `project_env` and looks up the keychain at the
        // SENTINEL_SHARED slot.
        let resp = reqwest::get(format!(
            "{}/projects/h1-proj/env?key=github_pat",
            base
        ))
        .await
        .expect("hub reachable");

        // Cleanup keychain BEFORE asserting so a failure case doesn't
        // strand a live PAT-shaped string in the user's OS keychain.
        let resp_status = resp.status();
        let resp_body: serde_json::Value = resp.json().await.expect("json body");
        delete_shared_keychain_canary("user", "github_pat");

        assert_eq!(
            resp_status, 200,
            "hub returned non-200 for github_pat lookup; body: {}",
            resp_body
        );
        assert_eq!(
            resp_body
                .get("github_pat")
                .and_then(|v| v.as_str()),
            Some(canary.as_str()),
            "hub returned a 200 but the value didn't round-trip through the SENTINEL_SHARED slot; body: {}",
            resp_body
        );
    }

    /// H1: the `bundled_secrets` block in `vct-module.json` is read end-to-end
    /// (parse → resolve → emit). This pins the schema contract — if a
    /// future commit changes `OrchestratorManifest`'s deserializer or
    /// drops the `bundled_secrets` field from the on-disk JSON, this
    /// test catches it before fork users hit the regression.
    ///
    /// Skipped without an OS keychain backend (CI containers).
    #[tokio::test]
    async fn orchestrator_manifest_bundled_secrets_surface_via_hub() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend in this test env");
            return;
        }
        let _lock = h1_lock();
        delete_shared_keychain_canary("user", "github_pat");

        // Sanity: the on-disk manifest has a github_pat declaration. If
        // someone strips this, every fork user's resolver path silently
        // returns key_not_active again. Fail loudly.
        let m = crate::commands::modules::read_orchestrator_manifest()
            .expect("vct-module.json must be discoverable from current_exe()");
        let pat_decl = m
            .bundled_secrets
            .iter()
            .find(|bs| bs.key == "github_pat")
            .expect(
                "vct-module.json::bundled_secrets[] must declare `github_pat` \
                 (scope=shared, module_id=user). Without it, the hub's \
                 /projects/{id}/env resolver has no manifest entry for \
                 github_pat and returns key_not_active.",
            );
        assert_eq!(
            pat_decl.scope, "shared",
            "github_pat declared in bundled_secrets must use scope=shared (matches register_github_pat)"
        );
        assert_eq!(
            pat_decl.module_id, "user",
            "github_pat declared in bundled_secrets must use module_id=user (matches register_github_pat's GITHUB_PAT_MODULE_ID const, post-2026-05-10 unification with the SecretsPanel UI_MODULE_BUCKET)"
        );

        // End-to-end: keychain → hub → response body.
        let ts = chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0);
        let canary = format!("h1-bundled-canary-{}", ts);
        write_shared_keychain_canary("user", "github_pat", &canary);

        let (base, h) = spawn_modules_api_hub().await;
        seed_project(
            &h.0,
            "h1-bundled-proj",
            "H1 Bundled Test",
            "/tmp/h1-bundled-test-project",
        );
        let resp = reqwest::get(format!(
            "{}/projects/h1-bundled-proj/env?key=github_pat",
            base
        ))
        .await
        .expect("hub reachable");
        let status = resp.status();
        let body: serde_json::Value = resp.json().await.expect("json body");
        delete_shared_keychain_canary("user", "github_pat");

        assert_eq!(status, 200, "expected 200 with github_pat resolved; body: {}", body);
        assert_eq!(
            body.get("github_pat").and_then(|v| v.as_str()),
            Some(canary.as_str())
        );
    }

    /// H1: a paused shared secret (Lifecycle B Unset on the SENTINEL_SHARED
    /// row) MUST NOT leak through the hub's resolver even though the
    /// keychain still holds the value. This is the canary test for the
    /// active-flag gate after the H1 fix swapped the lookup slot —
    /// pre-fix the gate was checked at `(scope='shared', project_id=<UUID>)`
    /// (always default-active because no row), so the hub would still
    /// have served the value if the lookup-slot bug got fixed in
    /// isolation. The active-flag gate must use SENTINEL_SHARED too.
    ///
    /// Skipped without an OS keychain backend.
    #[tokio::test]
    async fn hub_resolver_honours_pause_on_sentinel_shared_active_flag() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend in this test env");
            return;
        }
        let _lock = h1_lock();
        delete_shared_keychain_canary("user", "github_pat");

        let ts = chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0);
        let canary = format!("h1-pause-canary-{}", ts);
        write_shared_keychain_canary("user", "github_pat", &canary);

        let (base, h) = spawn_modules_api_hub().await;
        seed_project(
            &h.0,
            "h1-pause-proj",
            "H1 Pause Test",
            "/tmp/h1-pause-test-project",
        );

        // Mark the SENTINEL_SHARED row INACTIVE — the launcher's GUI
        // would reach this state when the user clicked "Unset" on the
        // shared github_pat entry. The keychain value stays put
        // (Lifecycle B), only the active flag flips.
        h.0.mark_secret_inactive("shared", "_user_shared_", "user", "github_pat")
            .unwrap();

        let resp = reqwest::get(format!(
            "{}/projects/h1-pause-proj/env?key=github_pat",
            base
        ))
        .await
        .expect("hub reachable");
        let status = resp.status();
        let body: serde_json::Value = resp.json().await.expect("json body");

        // Cleanup BEFORE asserting so a failure doesn't strand state.
        h.0.forget_secret_active_state("shared", "_user_shared_", "user", "github_pat")
            .unwrap();
        delete_shared_keychain_canary("user", "github_pat");

        // Hub MUST refuse to serve the value: the active-flag gate is
        // honoured at the SENTINEL_SHARED slot. Returns 404 + key_not_active.
        assert_eq!(
            status, 404,
            "paused shared secret leaked through hub resolver: status={}, body={}",
            status, body
        );
        assert_eq!(
            body.get("error").and_then(|e| e.get("code")).and_then(|v| v.as_str()),
            Some("key_not_active"),
            "expected key_not_active envelope; body: {}",
            body
        );
    }

    /// post-0.2.0 backlog #6 (2026-05-10): the hub's bundled_secrets
    /// resolver falls back to the legacy `installer/` keychain slot
    /// when the new `user/` slot is empty. This is the upgrade-window
    /// guarantee: a 0.2.0 user whose PAT lives at the legacy slot
    /// keeps getting their token from the hub until they trigger
    /// `register_github_pat` again (which runs the consolidation
    /// migration). Without this fallback, every 0.2.0 install would
    /// silently lose `GITHUB_TOKEN` from its env files until the user
    /// noticed and re-registered.
    ///
    /// Skipped without an OS keychain backend.
    #[tokio::test]
    async fn hub_resolver_falls_back_to_legacy_installer_slot_when_user_slot_empty() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend in this test env");
            return;
        }
        let _lock = h1_lock();

        // Wipe BOTH slots so a residue from a prior test doesn't
        // mask the fallback path.
        delete_shared_keychain_canary("user", "github_pat");
        delete_shared_keychain_canary("installer", "github_pat");

        // Seed the LEGACY slot only — simulates a 0.2.0 install that
        // hasn't run the module_id migration yet.
        let ts = chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0);
        let canary = format!("h1-legacy-fallback-{}", ts);
        write_shared_keychain_canary("installer", "github_pat", &canary);

        let (base, h) = spawn_modules_api_hub().await;
        seed_project(
            &h.0,
            "h1-legacy-fb-proj",
            "H1 Legacy Fallback",
            "/tmp/h1-legacy-fb-test-project",
        );
        let resp = reqwest::get(format!(
            "{}/projects/h1-legacy-fb-proj/env?key=github_pat",
            base
        ))
        .await
        .expect("hub reachable");
        let status = resp.status();
        let body: serde_json::Value = resp.json().await.expect("json body");

        // Cleanup BEFORE asserting so a failure doesn't strand a
        // PAT-shaped string in the user's OS keychain.
        delete_shared_keychain_canary("installer", "github_pat");
        delete_shared_keychain_canary("user", "github_pat");

        assert_eq!(
            status, 200,
            "hub must return 200 with the legacy-slot PAT during the upgrade window; body: {}",
            body
        );
        assert_eq!(
            body.get("github_pat").and_then(|v| v.as_str()),
            Some(canary.as_str()),
            "hub must surface the legacy `installer/` slot value when `user/` is empty; body: {}",
            body
        );
    }

    /// post-0.2.0 backlog #6 (2026-05-10): the legacy fallback is
    /// SUPPRESSED when the new `user/` slot has its OWN value. The new
    /// slot wins (later-write semantics). Pins the precedence contract.
    ///
    /// Skipped without an OS keychain backend.
    #[tokio::test]
    async fn hub_resolver_user_slot_wins_when_both_slots_populated() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend in this test env");
            return;
        }
        let _lock = h1_lock();

        delete_shared_keychain_canary("user", "github_pat");
        delete_shared_keychain_canary("installer", "github_pat");

        let ts = chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0);
        let new_canary = format!("h1-new-wins-{}", ts);
        let old_canary = format!("h1-old-loses-{}", ts);
        // Both slots populated.
        write_shared_keychain_canary("user", "github_pat", &new_canary);
        write_shared_keychain_canary("installer", "github_pat", &old_canary);

        let (base, h) = spawn_modules_api_hub().await;
        seed_project(
            &h.0,
            "h1-precedence-fb-proj",
            "H1 Precedence Fallback",
            "/tmp/h1-precedence-fb-test-project",
        );
        let resp = reqwest::get(format!(
            "{}/projects/h1-precedence-fb-proj/env?key=github_pat",
            base
        ))
        .await
        .expect("hub reachable");
        let status = resp.status();
        let body: serde_json::Value = resp.json().await.expect("json body");

        delete_shared_keychain_canary("user", "github_pat");
        delete_shared_keychain_canary("installer", "github_pat");

        assert_eq!(status, 200, "expected 200; body: {}", body);
        assert_eq!(
            body.get("github_pat").and_then(|v| v.as_str()),
            Some(new_canary.as_str()),
            "user slot must win when both slots are populated; got body: {}",
            body
        );
    }

    /// H1: when an installed module also declares `github_pat`, that
    /// declaration takes precedence over the orchestrator's bundled one.
    /// The orchestrator's bundled_secrets are a DEFAULT — they fill in
    /// only when no installed module already populated the key. Pins
    /// the dedup contract documented in `project_env`'s comment.
    ///
    /// We exercise the negative side: an installed module's manifest
    /// (in `bundled_manifests/`) that declares `github_pat` is the
    /// ONLY source the hub considers. If that manifest's declaration
    /// got the lookup right, we get the value; if it got it wrong, we
    /// get nothing — the orchestrator's bundled fallback does NOT run
    /// to mask the bug. This isolates "module manifest wins" from
    /// "orchestrator fallback works".
    ///
    /// Skipped without an OS keychain backend.
    #[tokio::test]
    async fn installed_module_secret_takes_precedence_over_orchestrator_bundled() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend in this test env");
            return;
        }
        let _lock = h1_lock();
        delete_shared_keychain_canary("user", "github_pat");

        // Step 1: write the canary at the SENTINEL_SHARED slot. Both an
        // installed-module manifest and the orchestrator's bundled list
        // could resolve it; we want to prove the installed module's
        // declaration is what's being read, not the bundled fallback.
        let ts = chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0);
        let canary = format!("h1-precedence-canary-{}", ts);
        write_shared_keychain_canary("user", "github_pat", &canary);

        let (base, h) = spawn_modules_api_hub().await;
        seed_project(
            &h.0,
            "h1-precedence-proj",
            "H1 Precedence Test",
            "/tmp/h1-precedence-test-project",
        );

        // No installed modules in this test (the in-memory DB is fresh).
        // The orchestrator's bundled_secrets path SHOULD fire and return
        // the value. This is the positive test for the fallback —
        // when no module is installed, the bundled list still works.
        let resp = reqwest::get(format!(
            "{}/projects/h1-precedence-proj/env?key=github_pat",
            base
        ))
        .await
        .expect("hub reachable");
        let status = resp.status();
        let body: serde_json::Value = resp.json().await.expect("json body");
        delete_shared_keychain_canary("user", "github_pat");

        assert_eq!(
            status, 200,
            "orchestrator bundled fallback didn't fire; body: {}",
            body
        );
        assert_eq!(
            body.get("github_pat").and_then(|v| v.as_str()),
            Some(canary.as_str())
        );
    }
}
