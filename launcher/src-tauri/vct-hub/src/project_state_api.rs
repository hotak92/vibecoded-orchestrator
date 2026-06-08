//! HTTP routes for per-project orchestrator state.
//!
//! Mirrors the Tauri commands in `crate::commands::project_state_cmd` so
//! headless callers (install.py, project bootstrap scripts, the
//! `vibecoded` CLI) can register state without going through the
//! desktop UI.
//!
//! Bound to 127.0.0.1 only (see `hub::server::start_hub_server`). Auth
//! is deferred — same security posture as `modules_api.rs`.

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

async fn patch_hook(
    State(h): State<LauncherDbHandle>,
    Path((_project_id, hook_id)): Path<(String, i64)>,
    Json(body): Json<PatchHookBody>,
) -> impl IntoResponse {
    if let Some(en) = body.enabled {
        if let Err(e) = h.0.set_project_hook_enabled(hook_id, en) {
            return err400(e);
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

#[derive(Debug, Deserialize)]
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
    #[serde(default)]
    is_set: bool,
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
        Ok(row) => Json(row).into_response(),
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
        Ok(row) => Json(row).into_response(),
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
