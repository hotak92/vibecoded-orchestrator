//! HTTP routes for per-project orchestrator state.
//!
//! Mirrors the Tauri commands in `crate::commands::project_state_cmd` so
//! headless callers (install.py, project bootstrap scripts, the
//! `vibecoded` CLI) can register state without going through the
//! desktop UI.
//!
//! Served on `0.0.0.0` since v0.2.61 (see `hub::server::start_hub_server`
//! for why — global-module container reachability). The access control is
//! the bearer token (`auth::require_auth`), NOT the bind address — same
//! security posture as `modules_api.rs`.

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{delete, get, patch, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::Value as JsonValue;

use super::modules_api::LauncherDbHandle;
use vct_launcher_core::db::access::AccessLevel;

pub fn router() -> Router<LauncherDbHandle> {
    Router::new()
        .route("/projects/{project_id}/state", get(snapshot))
        .route("/projects/{project_id}/agents", get(list_agents).post(register_agent))
        .route(
            "/projects/{project_id}/agents/{agent_name}",
            patch(patch_agent).delete(delete_agent),
        )
        .route("/projects/{project_id}/skills", get(list_skills).post(register_skill))
        .route(
            "/projects/{project_id}/skills/{skill_name}",
            patch(patch_skill).delete(delete_skill),
        )
        .route("/projects/{project_id}/hooks", get(list_hooks).post(register_hook))
        .route("/projects/{project_id}/hooks/{hook_id}", patch(patch_hook).delete(delete_hook))
        .route(
            "/projects/{project_id}/permissions",
            get(list_permissions).post(add_permission),
        )
        .route("/projects/{project_id}/permissions/{perm_id}", delete(delete_permission))
        .route("/projects/{project_id}/secrets", get(list_secrets).post(set_secret))
        .route("/projects/{project_id}/secrets/{secret_key}", delete(delete_secret))
        .route("/projects/{project_id}/kg-binding", post(set_kg_binding))
        .route("/projects/{project_id}/codegraph-binding", post(set_codegraph_binding))
        // v0.2.49 access-matrix Phase 8 (Stream W4) — WRITE-path gate.
        // Hooks + MCP server consult this endpoint before allowing a
        // write into a Weaviate collection. Returns the project's
        // effective access level for the collection.
        .route(
            "/projects/{project_id}/access/{collection}",
            get(get_collection_access),
        )
        // v0.2.49 access-matrix Step F (NEW for Q2 user directive):
        // list-writable variant of the access endpoint. Consumed by
        // the MCP server's deny-branch enrichment to tell the LLM
        // caller WHICH collections the project DOES have write access
        // to (so the deny response is self-resolving instead of
        // opaque). Query param `level=write` is required; future
        // expansion could add `level=read` if a use case emerges.
        .route(
            "/projects/{project_id}/access",
            get(list_collection_access_by_level),
        )
}

// ─── Helpers ─────────────────────────────────────────────────────────────

fn err500(e: String) -> axum::response::Response {
    (StatusCode::INTERNAL_SERVER_ERROR, Json(serde_json::json!({ "error": e }))).into_response()
}
fn err400(e: String) -> axum::response::Response {
    (StatusCode::BAD_REQUEST, Json(serde_json::json!({ "error": e }))).into_response()
}

// ─── Snapshot ────────────────────────────────────────────────────────────

async fn snapshot(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
) -> impl IntoResponse {
    // Verify the project exists before returning a snapshot. The
    // per-table helpers below (list_project_agents etc.) all happily
    // return [] for an unknown project_id — callers couldn't tell
    // an empty real project apart from a stale/wrong ID. Reported
    // 2026-04-28 by the gui-tester agent.
    match h.0.get_project(&project_id) {
        Ok(Some(_)) => {}
        Ok(None) => {
            return (
                StatusCode::NOT_FOUND,
                Json(serde_json::json!({ "error": format!("project not found: {}", project_id) })),
            )
                .into_response();
        }
        Err(e) => return err500(e),
    }
    match h.0.get_project_state_snapshot(&project_id) {
        Ok(s) => Json(s).into_response(),
        Err(e) => err500(e),
    }
}

// ─── Agents ──────────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct RegisterAgentBody {
    agent_name: String,
    source: String,
    source_module: Option<String>,
    model: Option<String>,
    file_path: Option<String>,
    #[serde(default)]
    config: JsonValue,
}

async fn list_agents(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
) -> impl IntoResponse {
    match h.0.list_project_agents(&project_id) {
        Ok(rows) => Json(rows).into_response(),
        Err(e) => err500(e),
    }
}

async fn register_agent(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
    Json(body): Json<RegisterAgentBody>,
) -> impl IntoResponse {
    match h.0.register_project_agent(
        &project_id,
        &body.agent_name,
        &body.source,
        body.source_module.as_deref(),
        body.model.as_deref(),
        body.file_path.as_deref(),
        &body.config,
    ) {
        Ok(row) => Json(row).into_response(),
        Err(e) => err400(e),
    }
}

#[derive(Debug, Deserialize)]
struct PatchAgentBody {
    enabled: Option<bool>,
}

async fn patch_agent(
    State(h): State<LauncherDbHandle>,
    Path((project_id, agent_name)): Path<(String, String)>,
    Json(body): Json<PatchAgentBody>,
) -> impl IntoResponse {
    if let Some(en) = body.enabled {
        if let Err(e) = h.0.set_project_agent_enabled(&project_id, &agent_name, en) {
            return err400(e);
        }
    }
    StatusCode::NO_CONTENT.into_response()
}

async fn delete_agent(
    State(h): State<LauncherDbHandle>,
    Path((project_id, agent_name)): Path<(String, String)>,
) -> impl IntoResponse {
    match h.0.unregister_project_agent(&project_id, &agent_name) {
        Ok(()) => StatusCode::NO_CONTENT.into_response(),
        Err(e) => err500(e),
    }
}

// ─── Skills ──────────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct RegisterSkillBody {
    skill_name: String,
    source: String,
    source_module: Option<String>,
    model: Option<String>,
    file_path: Option<String>,
    #[serde(default)]
    config: JsonValue,
}

async fn list_skills(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
) -> impl IntoResponse {
    match h.0.list_project_skills(&project_id) {
        Ok(rows) => Json(rows).into_response(),
        Err(e) => err500(e),
    }
}

async fn register_skill(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
    Json(body): Json<RegisterSkillBody>,
) -> impl IntoResponse {
    match h.0.register_project_skill(
        &project_id,
        &body.skill_name,
        &body.source,
        body.source_module.as_deref(),
        body.model.as_deref(),
        body.file_path.as_deref(),
        &body.config,
    ) {
        Ok(row) => Json(row).into_response(),
        Err(e) => err400(e),
    }
}

#[derive(Debug, Deserialize)]
struct PatchSkillBody {
    enabled: Option<bool>,
}

async fn patch_skill(
    State(h): State<LauncherDbHandle>,
    Path((project_id, skill_name)): Path<(String, String)>,
    Json(body): Json<PatchSkillBody>,
) -> impl IntoResponse {
    if let Some(en) = body.enabled {
        if let Err(e) = h.0.set_project_skill_enabled(&project_id, &skill_name, en) {
            return err400(e);
        }
    }
    StatusCode::NO_CONTENT.into_response()
}

async fn delete_skill(
    State(h): State<LauncherDbHandle>,
    Path((project_id, skill_name)): Path<(String, String)>,
) -> impl IntoResponse {
    match h.0.unregister_project_skill(&project_id, &skill_name) {
        Ok(()) => StatusCode::NO_CONTENT.into_response(),
        Err(e) => err500(e),
    }
}

// ─── Hooks ───────────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct RegisterHookBody {
    event: String,
    #[serde(default)]
    matcher: String,
    command: String,
    #[serde(default = "default_source_project")]
    source: String,
    source_module: Option<String>,
    timeout_ms: Option<i64>,
    #[serde(default)]
    config: JsonValue,
}
fn default_source_project() -> String {
    "project".into()
}

async fn list_hooks(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
) -> impl IntoResponse {
    match h.0.list_project_hooks(&project_id) {
        Ok(rows) => Json(rows).into_response(),
        Err(e) => err500(e),
    }
}

async fn register_hook(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
    Json(body): Json<RegisterHookBody>,
) -> impl IntoResponse {
    match h.0.register_project_hook(
        &project_id,
        &body.event,
        &body.matcher,
        &body.command,
        &body.source,
        body.source_module.as_deref(),
        body.timeout_ms,
        &body.config,
    ) {
        Ok(row) => Json(row).into_response(),
        Err(e) => err400(e),
    }
}

#[derive(Debug, Deserialize)]
struct PatchHookBody {
    enabled: Option<bool>,
}

/// Toggle a hook's enforcement (v0.2.91 wave 5 residual close): drives the
/// real `vco_lib.hooks_settings` writer through
/// `hooks_enforcement::enforce_hook_toggle` — an actual edit to
/// `<project>/.claude/settings.json`, not the pre-fix `Db::
/// set_project_hook_enabled` mirror-only `UPDATE` that nothing downstream
/// read. See `crate::hooks_enforcement` for why the hub carries its own
/// (documented) caller of that writer rather than reusing the launcher's.
async fn patch_hook(
    State(h): State<LauncherDbHandle>,
    Path((project_id, hook_id)): Path<(String, i64)>,
    Json(body): Json<PatchHookBody>,
) -> impl IntoResponse {
    if let Some(en) = body.enabled {
        if let Err(e) =
            crate::hooks_enforcement::enforce_hook_toggle(&h.0, &project_id, hook_id, en).await
        {
            return e.into_response();
        }
    }
    StatusCode::NO_CONTENT.into_response()
}

async fn delete_hook(
    State(h): State<LauncherDbHandle>,
    Path((_project_id, hook_id)): Path<(String, i64)>,
) -> impl IntoResponse {
    match h.0.unregister_project_hook(hook_id) {
        Ok(()) => StatusCode::NO_CONTENT.into_response(),
        Err(e) => err500(e),
    }
}

// ─── Permissions ─────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct AddPermissionBody {
    subject: String,
    kind: String,
    value: String,
    #[serde(default)]
    config: JsonValue,
}

async fn list_permissions(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
) -> impl IntoResponse {
    match h.0.list_project_permissions(&project_id) {
        Ok(rows) => Json(rows).into_response(),
        Err(e) => err500(e),
    }
}

async fn add_permission(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
    Json(body): Json<AddPermissionBody>,
) -> impl IntoResponse {
    match h.0.add_project_permission(
        &project_id,
        &body.subject,
        &body.kind,
        &body.value,
        &body.config,
    ) {
        Ok(row) => Json(row).into_response(),
        Err(e) => err400(e),
    }
}

async fn delete_permission(
    State(h): State<LauncherDbHandle>,
    Path((_project_id, perm_id)): Path<(String, i64)>,
) -> impl IntoResponse {
    match h.0.delete_project_permission(perm_id) {
        Ok(()) => StatusCode::NO_CONTENT.into_response(),
        Err(e) => err500(e),
    }
}

// ─── Secret references ───────────────────────────────────────────────────

/// Declaration body for a project secret ref (metadata only — never a
/// value).
///
/// v0.2.73 footgun fix: `is_set` is `Option<bool>` — omitted means
/// "preserve the stored flag" (pre-fix, an omitted `is_set` deserialized
/// to `false` and the upsert reset 1→0, detaching the GUI's saved-value
/// display). `deny_unknown_fields` turns guessed fields (e.g. an
/// `"active": true` some callers invented) into a 422 instead of a
/// silent swallow.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SetSecretBody {
    secret_key: String,
    resolution: String,
    file_path: Option<String>,
    env_name: Option<String>,
    source_module: Option<String>,
    #[serde(default)]
    required_for: Vec<String>,
    #[serde(default)]
    description: String,
    is_set: Option<bool>,
}

async fn list_secrets(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
) -> impl IntoResponse {
    match h.0.list_project_secret_refs(&project_id) {
        Ok(rows) => Json(rows).into_response(),
        Err(e) => err500(e),
    }
}

async fn set_secret(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
    Json(body): Json<SetSecretBody>,
) -> impl IntoResponse {
    match h.0.set_project_secret_ref(
        &project_id,
        &body.secret_key,
        &body.resolution,
        body.file_path.as_deref(),
        body.env_name.as_deref(),
        body.source_module.as_deref(),
        &body.required_for,
        &body.description,
        body.is_set,
    ) {
        Ok(row) => Json(row).into_response(),
        Err(e) => err400(e),
    }
}

async fn delete_secret(
    State(h): State<LauncherDbHandle>,
    Path((project_id, secret_key)): Path<(String, String)>,
) -> impl IntoResponse {
    match h.0.delete_project_secret_ref(&project_id, &secret_key) {
        Ok(()) => StatusCode::NO_CONTENT.into_response(),
        Err(e) => err500(e),
    }
}

// ─── KG / Codegraph bindings ─────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct SetKgBindingBody {
    #[serde(default = "default_kg_role")]
    role: String,
    collection_name: String,
    embedding_model: Option<String>,
    embedding_dim: Option<i64>,
    kg_dir_path: Option<String>,
    weaviate_url: Option<String>,
    #[serde(default)]
    config: JsonValue,
}
fn default_kg_role() -> String {
    "primary".into()
}

/// Serialize a binding row and flag it with `"reprojection_required":
/// true` (v0.2.72 R3, F5 residual).
///
/// WHY the flag instead of re-projecting here: the launcher OWNS env
/// projection — `.claude/{settings.json,env}` are written by
/// `python -m vco_lib.config_projection`, which the launcher-side
/// commands invoke via `projects_v2::reproject_env_soft` /
/// `refresh_all_projects_env_with_db`. The hub process has NO
/// python-spawn pattern (its only subprocesses are its own binary,
/// systemctl/launchctl/schtasks boot glue, and container runtimes) and
/// no reliable view of the projection interpreter/venv, so spawning the
/// projection from here would be a new, fragile cross-process contract.
/// Instead the mutation response is marked machine-readably: a caller
/// that mutates bindings through the hub REST surface must follow up
/// with a re-projection (e.g. `python -m vco_lib.config_projection
/// apply --project <folder>` from the project's environment, or any
/// launcher-driven refresh) or the on-disk `.claude/settings.json` stays
/// stale until an unrelated write. No in-repo production caller uses
/// these endpoints today (they exist for headless callers: install.py
/// clones, bootstrap scripts, the `vibecoded` CLI) — the flag makes the
/// contract explicit for those external callers.
fn binding_response_with_reprojection_flag<T: serde::Serialize>(
    row: &T,
) -> axum::response::Response {
    let mut body = serde_json::to_value(row).unwrap_or_else(|_| serde_json::json!({}));
    if let Some(obj) = body.as_object_mut() {
        obj.insert(
            "reprojection_required".to_string(),
            serde_json::Value::Bool(true),
        );
    }
    Json(body).into_response()
}

async fn set_kg_binding(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
    Json(body): Json<SetKgBindingBody>,
) -> impl IntoResponse {
    // v0.2.49 access-matrix Phase 4 (item #9 / M-7): route through
    // `_with_root_sync` so the hub-driven binding write picks up the
    // orchestrator-root primary↔shared mirror AND the atomic
    // kg_collection_access rename. The plain `set_project_kg_binding`
    // bypasses both — leaving the access matrix stale whenever a
    // collection_name changes via this endpoint, and silently breaking
    // the orchestrator-root self-heal when the hub is the writer (e.g.
    // CLI-driven flows). The `_with_root_sync` variant needs the
    // project's slug to detect the orchestrator-root case, so we look
    // it up first; failing the lookup before any DB write is the right
    // failure mode (caller sees 400, no half-state).
    //
    // v0.2.72 R3 (F5 residual): this endpoint mutates rows that the env
    // projection derives KG_COLLECTION / SHARED_KG_COLLECTION from, but
    // the hub cannot run the projection (see
    // `binding_response_with_reprojection_flag`) — the response is
    // flagged so callers know `.claude/settings.json` is stale until
    // they re-project.
    let project_slug = match h.0.get_project(&project_id) {
        Ok(Some(row)) => row.slug,
        Ok(None) => return err400(format!("project {} not found", project_id)),
        Err(e) => return err400(format!("project lookup: {}", e)),
    };
    match h.0.set_project_kg_binding_with_root_sync(
        &project_id,
        &project_slug,
        &body.role,
        &body.collection_name,
        body.embedding_model.as_deref(),
        body.embedding_dim,
        body.kg_dir_path.as_deref(),
        body.weaviate_url.as_deref(),
        &body.config,
    ) {
        Ok(row) => binding_response_with_reprojection_flag(&row),
        Err(e) => err400(e),
    }
}

#[derive(Debug, Deserialize)]
struct SetCodegraphBindingBody {
    collection_prefix: String,
    embedding_model: Option<String>,
    embedding_dim: Option<i64>,
    last_analyzed_commit: Option<String>,
    last_analyzed_at: Option<i64>,
    #[serde(default = "default_true")]
    enabled: bool,
    #[serde(default)]
    config: JsonValue,
}
fn default_true() -> bool {
    true
}

async fn set_codegraph_binding(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
    Json(body): Json<SetCodegraphBindingBody>,
) -> impl IntoResponse {
    // v0.2.72 R3 (F5 residual): same staleness contract as
    // `set_kg_binding` above — since R2 the env projection derives
    // CODE_GRAPH_PROJECT from `project_codegraph_bindings.
    // collection_prefix`, so a hub-driven prefix write leaves
    // `.claude/settings.json` stale until the caller re-projects. The
    // response is flagged accordingly (see
    // `binding_response_with_reprojection_flag`).
    match h.0.set_project_codegraph_binding(
        &project_id,
        &body.collection_prefix,
        body.embedding_model.as_deref(),
        body.embedding_dim,
        body.last_analyzed_commit.as_deref(),
        body.last_analyzed_at,
        body.enabled,
        &body.config,
    ) {
        Ok(row) => binding_response_with_reprojection_flag(&row),
        Err(e) => err400(e),
    }
}

// ─── Access matrix (v0.2.49 Phase 8, Stream W4) ───────────────────────────

/// v0.2.49 access-matrix Phase 8 / Stream W4 — WRITE-path gate
/// endpoint. Returns the project's effective access level for a
/// specific Weaviate collection.
///
/// Consumed by:
///   - `.claude/hooks/post-file-edit.sh` (KG sync write path, bash
///     resolver client at `templates/scripts/vct_access_check.sh`)
///   - `claude_mcp_servers/weaviate_mcp/server.py::store_knowledge_node`
///     (via `vco_lib/access_resolver.py`)
///
/// Both clients fail-open on transport errors (hub unreachable,
/// timeout, auth fail) — that's a deliberate degradation contract,
/// NOT enforced at this endpoint. The endpoint itself returns the
/// authoritative answer when reachable + correctly authed.
///
/// Response shape:
///
/// ```json
/// { "level": "read" | "write" | "none" }
/// ```
///
/// Sources the level from `db::access::kg_get_access(project_id,
/// collection)`. Semantics:
///   - **Row exists**: return its `access_level` verbatim.
///   - **Row absent**: return `"none"`. The plan's F-2a default
///     ("a project owns its primary + shared bindings → Write;
///     everything else → Denied") manifests at the resolver layer
///     (`db::access::resolve_default_access_level`), but the
///     access-matrix WRITE-path gate consults persisted state only
///     — projects MUST have an explicit access row written by either
///     the project-create populate path, the M-3 global-module
///     populate path, or a user-driven UI mutation. A missing row
///     means "no relationship has been established" → deny.
///
/// Error cases:
///   - `404 Not Found` when the project_id is unknown. Caller
///     should NOT cache; project might be created by a subsequent
///     request.
///   - `500 Internal Server Error` on a DB error. Caller's
///     fail-open path takes over.
///
/// Auth: bearer token (via `auth` middleware applied at the router
/// composition site in `lib.rs`/`server.rs`). 401 on missing/wrong
/// token, same as every other `/api/v1/*` route except `/health`.
///
/// Latency: pure in-process SQLite query against an indexed PK
/// (`project_id`, `collection_name`). Sub-millisecond on a warm
/// page cache; ~5ms cold. Clients use a 5-second timeout but the
/// real ceiling is bounded by hub crate concurrency, not query
/// time.
async fn get_collection_access(
    State(h): State<LauncherDbHandle>,
    Path((project_id, collection)): Path<(String, String)>,
) -> impl IntoResponse {
    // Pre-check: project must exist. Returning a default `none` for
    // an unknown project would let callers race a created-then-
    // deleted project window without noticing. 404 is the right
    // failure mode; clients fail-open on it (same as transport
    // failures) so this is just a diagnostic signal, not a behavior
    // change.
    match h.0.get_project(&project_id) {
        Ok(Some(_)) => {}
        Ok(None) => {
            return (
                StatusCode::NOT_FOUND,
                Json(serde_json::json!({
                    "error": format!("project not found: {}", project_id)
                })),
            )
                .into_response();
        }
        Err(e) => return err500(e),
    }

    // v0.2.49 Step F SB3 (L2-SB2 SHIP-BLOCKER): row-absent fallback
    // consults `resolve_default_access_level` instead of returning a
    // literal "none". Pre-fix the literal "none" diverged from the
    // resolver's F-2a output (Write for own primary/shared bindings)
    // — so a partial-populate failure could leave a project with no
    // access row → hub returns "none" → MCP blocks the project's own
    // KG writes. The resolver is the load-bearing semantic; hub now
    // exposes the same.
    //
    // Step F SF3 (L2-SF2): on the row-present branch, round-trip the
    // persisted string through `AccessLevel::from_str_strict` →
    // `as_str()` so any future schema-CHECK weakening (or DB-layer
    // bug that stores an invalid string) surfaces as 500 here rather
    // than leaking through to clients. The happy path emits the same
    // bytes as before (Strict parse on valid input + as_str returns
    // the original string).
    match h.0.kg_get_access(&project_id, &collection) {
        Ok(Some(level)) => match AccessLevel::from_str_strict(&level) {
            Ok(parsed) => Json(serde_json::json!({ "level": parsed.as_str() })).into_response(),
            Err(e) => err500(format!(
                "kg_collection_access row for ({}, {}) has invalid \
                 access_level value '{}': {}. The DB schema CHECK \
                 should have rejected this — investigate.",
                project_id, collection, level, e,
            )),
        },
        Ok(None) => match h.0.resolve_default_access_level(&project_id, &collection) {
            Ok(default_level) => {
                Json(serde_json::json!({ "level": default_level.as_str() })).into_response()
            }
            Err(e) => err500(format!("resolve_default_access_level: {}", e)),
        },
        Err(e) => err500(e),
    }
}

// ─── List-by-level variant (v0.2.49 Step F, Q2 enrichment) ──────────────

#[derive(Debug, Deserialize)]
struct AccessLevelQuery {
    level: String,
}

/// v0.2.49 access-matrix Step F (Q2 enrichment) — list-writable
/// variant of the access endpoint.
///
/// Returns the collections this project has access to at the
/// requested level. Consumed by the MCP server's deny-branch
/// enrichment in `claude_mcp_servers/weaviate_mcp/server.py::
/// store_knowledge_node` to render a self-resolving error
/// response ("you can write to: X, Y, Z; to gain write on Foo,
/// adjust via Launcher → Identity → Manage access") instead of
/// the opaque pre-fix "Access matrix denies write" message.
///
/// Per user Q2 directive (verbatim, 2026-06-08): "in that
/// warning tell Claude also where it has the permission to
/// write to".
///
/// Response shape:
///
/// ```json
/// { "collections": ["Foo_KnowledgeGraph", "Foo_Development", "VibeCodedOrchestrator_KnowledgeGraph"] }
/// ```
///
/// Behaviour:
///   - `?level=write` → collections where `access_level = 'write'`
///   - `?level=read`  → collections where `access_level = 'read'`
///   - `?level=none`  → collections where `access_level = 'none'`
///     (explicit denies — useful for debugging the matrix, not
///     consumed by the WRITE gate at present)
///   - Any other value (or missing) → 400 with allowlist error
///
/// Sorted ascending by `collection_name` (matches `kg_list_access`
/// underlying sort). Empty array if the project has no rows at
/// that level (a fresh project pre-populate, or a project all of
/// whose collections are at a different level).
///
/// Error cases:
///   - `400 Bad Request` when `level` is missing or not in
///     `{read, write, none}`.
///   - `404 Not Found` when project_id is unknown. Caller's
///     fail-open path takes over (same posture as the singular
///     access endpoint).
///   - `500 Internal Server Error` on DB error.
///
/// Auth: bearer token via the `auth` middleware (same posture as
/// every `/api/v1/*` route except `/health`).
///
/// Latency: one indexed SQLite query against `kg_collection_access`
/// (filtered by `project_id` + `access_level`). Sub-millisecond on
/// warm cache. Clients on the deny path use a 2-second timeout
/// because this fires from inside `store_knowledge_node`'s error
/// branch — adding 5+ seconds of latency to an already-failed
/// write would be poor UX. Endpoint itself is fast enough that
/// the tight client timeout is conservative.
async fn list_collection_access_by_level(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
    Query(q): Query<AccessLevelQuery>,
) -> impl IntoResponse {
    // Validate level against the AccessLevel enum's wire-stable values.
    // Strict allowlist — anything else is a client bug surfaced as 400,
    // not silently mapped to "none" (which would mask the bug).
    if !matches!(q.level.as_str(), "read" | "write" | "none") {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({
                "error": format!(
                    "invalid level '{}': must be one of 'read', 'write', 'none'",
                    q.level
                )
            })),
        )
            .into_response();
    }

    // Pre-check: project must exist. Same posture as
    // `get_collection_access` — 404 lets the caller fail-open with a
    // diagnostic signal.
    match h.0.get_project(&project_id) {
        Ok(Some(_)) => {}
        Ok(None) => {
            return (
                StatusCode::NOT_FOUND,
                Json(serde_json::json!({
                    "error": format!("project not found: {}", project_id)
                })),
            )
                .into_response();
        }
        Err(e) => return err500(e),
    }

    match h.0.kg_list_access(&project_id) {
        Ok(rows) => {
            let collections: Vec<String> = rows
                .into_iter()
                .filter_map(|(coll, lvl)| if lvl == q.level { Some(coll) } else { None })
                .collect();
            Json(serde_json::json!({ "collections": collections })).into_response()
        }
        Err(e) => err500(e),
    }
}

// ─── Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use vct_launcher_core::db::Db;

    // v0.2.73 is_set footgun fix — see `SetSecretBody`'s doc-comment.
    // HTTP harness mirrors `modules_api.rs::tests::spawn_modules_api_hub`.

    async fn spawn_project_state_hub() -> (String, LauncherDbHandle) {
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

    /// serde contract: an omitted `is_set` deserializes to `None`
    /// (preserve), NOT `false`. Pre-fix `#[serde(default)] is_set: bool`
    /// made every declaration-only POST reset the flag.
    #[test]
    fn set_secret_body_omitted_is_set_is_none() {
        let body: SetSecretBody = serde_json::from_value(serde_json::json!({
            "secret_key": "EXAMPLE_API_TOKEN",
            "resolution": "keychain-per-project"
        }))
        .expect("minimal body must deserialize");
        assert_eq!(body.is_set, None, "omitted is_set must be None (preserve)");

        let body: SetSecretBody = serde_json::from_value(serde_json::json!({
            "secret_key": "EXAMPLE_API_TOKEN",
            "resolution": "keychain-per-project",
            "is_set": true
        }))
        .unwrap();
        assert_eq!(body.is_set, Some(true));
    }

    /// serde contract: unknown fields are rejected (`deny_unknown_fields`).
    /// Pre-fix a guessed `"active": true` was silently swallowed.
    #[test]
    fn set_secret_body_rejects_unknown_fields() {
        let res = serde_json::from_value::<SetSecretBody>(serde_json::json!({
            "secret_key": "EXAMPLE_API_TOKEN",
            "resolution": "keychain-per-project",
            "active": true
        }));
        assert!(res.is_err(), "unknown field `active` must be rejected");
    }

    /// HTTP end-to-end: an unknown field in the POST body → 422 (axum's
    /// Json extractor surfaces the serde error), and the row is NOT
    /// created.
    #[tokio::test]
    async fn set_secret_unknown_field_returns_422_and_writes_nothing() {
        let (base, h) = spawn_project_state_hub().await;
        seed_project(&h.0, "ps-proj-1", "PS Test One", "/tmp/ps-proj-1");

        let resp = reqwest::Client::new()
            .post(format!("{}/projects/ps-proj-1/secrets", base))
            .json(&serde_json::json!({
                "secret_key": "EXAMPLE_API_TOKEN",
                "resolution": "keychain-per-project",
                "active": true
            }))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(
            resp.status(),
            422,
            "unknown field must produce 422, not silent acceptance"
        );
        let refs = h.0.list_project_secret_refs("ps-proj-1").unwrap();
        assert!(refs.is_empty(), "rejected body must not create a row");
    }

    /// HTTP end-to-end: a declaration-only re-POST (no `is_set`) must
    /// PRESERVE the stored flag — the footgun was a redeclare resetting
    /// 1→0 so the GUI claimed the saved value was unset.
    #[tokio::test]
    async fn set_secret_redeclare_without_is_set_preserves_flag() {
        let (base, h) = spawn_project_state_hub().await;
        seed_project(&h.0, "ps-proj-2", "PS Test Two", "/tmp/ps-proj-2");
        let client = reqwest::Client::new();

        // Value-set path marks the flag explicitly.
        let resp = client
            .post(format!("{}/projects/ps-proj-2/secrets", base))
            .json(&serde_json::json!({
                "secret_key": "EXAMPLE_API_TOKEN",
                "resolution": "keychain-per-project",
                "source_module": "user",
                "is_set": true
            }))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);

        // Declaration-only redeclare: is_set omitted.
        let resp = client
            .post(format!("{}/projects/ps-proj-2/secrets", base))
            .json(&serde_json::json!({
                "secret_key": "EXAMPLE_API_TOKEN",
                "resolution": "keychain-per-project",
                "source_module": "user",
                "description": "redeclared"
            }))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(
            body.get("is_set").and_then(|v| v.as_bool()),
            Some(true),
            "returned row must reflect the PRESERVED flag; body: {}",
            body
        );

        let refs = h.0.list_project_secret_refs("ps-proj-2").unwrap();
        assert_eq!(refs.len(), 1);
        assert!(refs[0].is_set, "redeclare without is_set must not reset 1→0");
        assert_eq!(refs[0].description, "redeclared");

        // Explicit false still clears (the value-clear path).
        let resp = client
            .post(format!("{}/projects/ps-proj-2/secrets", base))
            .json(&serde_json::json!({
                "secret_key": "EXAMPLE_API_TOKEN",
                "resolution": "keychain-per-project",
                "source_module": "user",
                "is_set": false
            }))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let refs = h.0.list_project_secret_refs("ps-proj-2").unwrap();
        assert!(!refs[0].is_set, "explicit is_set=false must still clear");
    }

    // ─── PATCH /projects/{id}/hooks/{hook_id} — real enforcement ────────
    //
    // v0.2.91 wave 5 residual close. Pre-fix this route called
    // `Db::set_project_hook_enabled(hook_id, enabled)` — a bare mirror
    // `UPDATE` — so the HTTP round trip below would return 204 while
    // `.claude/settings.json` never changed. These tests exercise the
    // actual HTTP surface (not `hooks_enforcement::enforce_hook_toggle`
    // directly, which the sibling `hooks_enforcement` test module already
    // covers) — the thing that would have caught the placebo is a real
    // request against the mounted router.

    // Canonical `json.dumps(..., indent=2)` shape (what `write_settings`
    // always emits) — NOT arbitrary formatting. `disable` reformats the
    // file to this shape on its own, so a byte-for-byte round-trip
    // assertion after disable+enable only holds when the fixture starts
    // already in canonical form (same reason `hooks_enforcement.rs`'s
    // `SETTINGS_JSON` fixture is indented this way).
    const HOOK_SETTINGS_JSON: &str = r#"{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash .claude/hooks/cost-tracker.sh",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
"#;

    /// Seed a project rooted at a REAL tempdir with a `.claude/settings.json`
    /// on disk plus the matching `project_hooks` mirror row, and return the
    /// hook's numeric id — the shape `patch_hook` needs end to end (a real
    /// file to edit, a real row to resolve `hook_id` from).
    fn seed_project_with_real_hook(db: &Db, project_id: &str) -> (tempfile::TempDir, i64) {
        let td = tempfile::TempDir::new().unwrap();
        let claude = td.path().join(".claude");
        std::fs::create_dir_all(&claude).unwrap();
        std::fs::write(claude.join("settings.json"), HOOK_SETTINGS_JSON).unwrap();
        seed_project(db, project_id, project_id, &td.path().to_string_lossy());
        let row = db
            .register_project_hook(
                project_id,
                "Stop",
                "",
                "bash .claude/hooks/cost-tracker.sh",
                "project",
                None,
                None,
                &serde_json::json!({}),
            )
            .unwrap();
        (td, row.id)
    }

    #[tokio::test]
    async fn patch_hook_disable_edits_settings_json_on_disk() {
        let (base, h) = spawn_project_state_hub().await;
        let (td, hook_id) = seed_project_with_real_hook(&h.0, "hook-http-1");
        let settings_path = td.path().join(".claude").join("settings.json");
        let before = std::fs::read_to_string(&settings_path).unwrap();

        let resp = reqwest::Client::new()
            .patch(format!("{}/projects/hook-http-1/hooks/{}", base, hook_id))
            .json(&serde_json::json!({ "enabled": false }))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 204, "body: {:?}", resp.text().await);

        let after = std::fs::read_to_string(&settings_path).unwrap();
        assert_ne!(
            after, before,
            "the HTTP round trip must edit settings.json — this is the fix, not just \
             a 204 status"
        );
        assert!(
            !after.contains("cost-tracker.sh"),
            "the disabled hook's entry must be gone from the file: {}",
            after
        );
        assert!(
            h.0.get_parked_project_hook_entry("hook-http-1", "Stop", "", "bash .claude/hooks/cost-tracker.sh")
                .unwrap()
                .is_some(),
            "the removed entry must be parked for an exact re-enable"
        );
    }

    #[tokio::test]
    async fn patch_hook_enable_after_disable_round_trips_via_http() {
        let (base, h) = spawn_project_state_hub().await;
        let (td, hook_id) = seed_project_with_real_hook(&h.0, "hook-http-2");
        let settings_path = td.path().join(".claude").join("settings.json");
        let before = std::fs::read_to_string(&settings_path).unwrap();
        let client = reqwest::Client::new();

        let resp = client
            .patch(format!("{}/projects/hook-http-2/hooks/{}", base, hook_id))
            .json(&serde_json::json!({ "enabled": false }))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 204);

        let resp = client
            .patch(format!("{}/projects/hook-http-2/hooks/{}", base, hook_id))
            .json(&serde_json::json!({ "enabled": true }))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 204, "body: {:?}", resp.text().await);

        let after = std::fs::read_to_string(&settings_path).unwrap();
        assert_eq!(after, before, "re-enable via HTTP restores the exact original bytes");
    }

    /// Leave-alone: an unknown `hook_id` for a real project must 404 with a
    /// structured body, and must not touch that project's settings.json.
    #[tokio::test]
    async fn patch_hook_unknown_hook_id_404s_and_writes_nothing() {
        let (base, h) = spawn_project_state_hub().await;
        let (td, _hook_id) = seed_project_with_real_hook(&h.0, "hook-http-3");
        let settings_path = td.path().join(".claude").join("settings.json");
        let before = std::fs::read_to_string(&settings_path).unwrap();

        let resp = reqwest::Client::new()
            .patch(format!("{}/projects/hook-http-3/hooks/{}", base, 999_999_i64))
            .json(&serde_json::json!({ "enabled": false }))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 404);
        let body: serde_json::Value = resp.json().await.expect("json");
        assert_eq!(body["error"]["code"], "hook_not_found", "body: {}", body);
        assert_eq!(std::fs::read_to_string(&settings_path).unwrap(), before);
    }

    /// Leave-alone: a sibling route this change did not touch
    /// (agent enable/disable — a DIFFERENT, already-real FS-move
    /// enforcement mechanism, see `Db::set_project_agent_enabled`) keeps
    /// working exactly as before. Guards against the router wiring change
    /// for hooks having collaterally broken an unrelated route.
    #[tokio::test]
    async fn patch_agent_route_is_unaffected_by_the_hooks_enforcement_change() {
        let (base, h) = spawn_project_state_hub().await;
        let td = tempfile::TempDir::new().unwrap();
        std::fs::create_dir_all(td.path().join(".claude").join("agents")).unwrap();
        let agent_path = td.path().join(".claude").join("agents").join("demo.md");
        std::fs::write(&agent_path, "# demo agent\n").unwrap();
        seed_project(&h.0, "hook-http-4", "hook-http-4", &td.path().to_string_lossy());
        h.0.register_project_agent(
            "hook-http-4",
            "demo",
            "project",
            None,
            None,
            Some(&agent_path.to_string_lossy()),
            &serde_json::json!({}),
        )
        .unwrap();

        let resp = reqwest::Client::new()
            .patch(format!("{}/projects/hook-http-4/agents/demo", base))
            .json(&serde_json::json!({ "enabled": false }))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 204, "body: {:?}", resp.text().await);
        assert!(
            !agent_path.exists(),
            "agent disable still moves the file (unrelated, pre-existing mechanism)"
        );
        assert!(td.path().join(".claude").join("agents.disabled").join("demo.md").exists());
    }
}
