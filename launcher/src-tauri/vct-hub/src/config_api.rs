//! Hub route exposing the per-project config resolver:
//! `GET /api/v1/projects/{id}/config`.
//!
//! This is the central piece of the v0.2.21 "hook-token-audit" fix.
//! Every hook / MCP / CLI script eventually calls this endpoint to
//! learn its project's KG collection, codegraph prefix, embeddings,
//! access matrix, etc. — replacing the brittle env-var thread that
//! the audit (`docs/HOOK_TOKEN_AUDIT_2026-05-20.md`) flagged as the
//! source of the "Found 0 results" symptoms.
//!
//! ─── Design doc ──────────────────────────────────────────────────
//!
//! The authoritative contract for this handler lives in
//! `.claude/context/plans/v0.2.21-resolver-design.md` (§1). The
//! companion sibling for SECRETS is `modules_api::project_env` —
//! same auth, same discovery, same fallback discipline. The two
//! endpoints share `LauncherDbHandle` state and a router layer; the
//! resolver clients in templates/scripts/ treat them as a matched
//! pair.
//!
//! ─── Five SQL reads, not one JOIN ────────────────────────────────
//!
//! Per the design phase gap-find: the access-matrix tables and
//! KG/codegraph bindings are 1-to-N per project (a project has
//! multiple binding rows — primary + shared + archive — and
//! multiple access rows). Expressing the full payload as a single
//! JOIN would either explode into a cartesian product or require
//! GROUP_CONCAT trickery that loses type fidelity. Five tightly-
//! scoped reads is the spec, executed sequentially under the same
//! short-lived SQLite mutex. Each read is sub-millisecond on
//! localhost; the whole assembly is well under the 30 s budget.
//!
//! ─── Access-matrix discipline ────────────────────────────────────
//!
//! `kg_access_list` reflects ONLY rows where
//! `kg_collection_access.access_level IN ('read','write')`. Rows
//! with `access_level='none'` are filtered. The project's own
//! primary collection is added implicitly even if no matrix row
//! exists (a project always has full access to itself).
//!
//! Symmetric rule for `codegraph_access_list`: reflects only
//! `codegraph_access` rows where the grantor granted `read` to this
//! project (and the project's own slug is added implicitly).
//!
//! ─── 503 vs 500 ──────────────────────────────────────────────────
//!
//! When a project row exists but has no primary KG binding (the
//! launcher-startup backfill hasn't run, or failed), the response
//! would be useless — the caller would have to know to ignore an
//! empty `kg_collection`. Instead the handler returns
//! `503 service_misconfigured` so resolver clients can route to a
//! loud-warning path that surfaces the fixable state to the user.
//! This is distinct from `500 internal_error` (unexpected DB
//! failure, not user-fixable).

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::get,
    Json, Router,
};
use serde::{Deserialize, Serialize};

use rusqlite::params;
use vct_launcher_core::config::LocalConfig;

use super::modules_api::LauncherDbHandle;
use super::retrieval_tuning_io::{read_tuning, RetrievalTuning};

// ─── Defaults shared with the launcher / docker-compose stack ────
//
// These mirror the "Default ports" line in CLAUDE.md and the
// compiled defaults under `claude_mcp_servers/`. They are surfaced
// in the resolver response so a fresh project that hasn't been
// further customised still resolves to a working Ollama / gRPC
// endpoint pair. Env-var overrides (set by the launcher when it
// boots a non-default stack) win over the defaults.
const DEFAULT_OLLAMA_URL: &str = "http://localhost:11435";
const DEFAULT_GRPC_PORT: u16 = 50052;

// ─── Resolver protocol version (v0.2.22 Item #2) ─────────────────
//
// The schema_version field on `ProjectConfigResponse` is a
// forward-compat anchor: a future hub release that adds a new
// REQUIRED field (one that old clients can't gracefully default)
// bumps this constant; clients log a one-line stderr warning when
// they see a version higher than they know about so the user has
// a diagnostic for "I upgraded the launcher but my hooks behave
// oddly". Adding OPTIONAL fields (defaultable client-side) does
// NOT bump the version — that's been the contract from v0.2.21
// onward (retrieval_tuning was an additive field at version 1).
//
// MUST stay in lock-step with `RESOLVER_PROTOCOL_VERSION` in
// `vco_lib/project_config.py`. When bumping here, bump there in the
// same commit.
const RESOLVER_PROTOCOL_VERSION: u8 = 1;

/// Default helper for `#[serde(default = ...)]` on
/// `ProjectConfigResponse::schema_version`. Returns the current
/// protocol version. `#[allow(dead_code)]` because the struct
/// derives `Serialize` only today (no Deserialize call path);
/// the helper is wired through the serde attribute and would
/// become live the moment a future codepath needs to round-trip
/// a response back into the struct (cross-launcher integration
/// tests, replay tooling, etc.). Kept for that forward-compat
/// hook — same rationale as `schema_version` itself.
#[allow(dead_code)]
fn default_schema_version() -> u8 {
    RESOLVER_PROTOCOL_VERSION
}

// ─── Router ──────────────────────────────────────────────────────

pub fn router() -> Router<LauncherDbHandle> {
    Router::new().route("/projects/{project_id}/config", get(project_config))
}

// ─── Error envelope (shared shape with modules_api) ──────────────

fn error_response(
    status: StatusCode,
    code: &str,
    message: impl Into<String>,
) -> axum::response::Response {
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

fn db_error_response(context: &str, raw: String) -> axum::response::Response {
    eprintln!("[vct-hub] {} failed: {}", context, raw);
    error_response(
        StatusCode::INTERNAL_SERVER_ERROR,
        "internal_error",
        format!("{} failed", context),
    )
}

// ─── Request / response shapes ───────────────────────────────────

#[derive(Debug, Deserialize, Default)]
struct ProjectConfigQuery {
    /// Single-field filter. When set, the response is the
    /// single-field envelope `{<field>: <value>}`. Empty / whitespace
    /// values yield 400; unknown field names yield 404.
    key: Option<String>,
}

#[derive(Debug, Serialize)]
struct EmbeddingModels {
    text: String,
    code: String,
}

#[derive(Debug, Serialize)]
struct ProjectConfigResponse {
    /// Resolver protocol version (v0.2.22 Item #2). Starts at 1.
    /// Clients that see a value higher than their compiled-in
    /// `RESOLVER_PROTOCOL_VERSION` emit a one-line stderr warning;
    /// they still parse the response (additive fields default
    /// client-side). The Python/bash/ps1 clients all treat
    /// unknown top-level fields as ignorable so future hubs that
    /// add fields under the SAME version stay wire-compatible.
    ///
    /// The `#[serde(default = ...)]` attribute is a no-op on the
    /// current Serialize-only struct but stays in place so that if
    /// a future codepath ever needs to Deserialize a response (e.g.
    /// cross-launcher round-trip in an integration test), the
    /// missing-field case lands on the compiled default rather
    /// than failing to parse. The helper returns
    /// `RESOLVER_PROTOCOL_VERSION` so the default tracks bumps.
    #[serde(default = "default_schema_version")]
    schema_version: u8,
    project_id: String,
    project_path: String,
    project_slug: String,
    project_display_name: String,
    /// Alias for `project_slug` — emitted because the legacy
    /// `code-graph-query --project ...` callers (and several hooks
    /// that grep their env) expect this exact field name. Same value
    /// as `project_slug`; eases migration. See design doc §1.3.
    code_graph_project: String,
    code_graph_collection_prefix: String,
    kg_collection: String,
    shared_kg_collection: String,
    development_collection: String,
    active_embedding: String,
    embedding_models: EmbeddingModels,
    kg_access_list: Vec<String>,
    codegraph_access_list: Vec<String>,
    weaviate_url: String,
    ollama_url: String,
    grpc_port: u16,
    shared_kg_write_disabled: bool,
    /// v0.2.22 Item #13 — global retrieval thresholds. Sourced from
    /// `<vct_root_dir>/retrieval-tuning.toml` (written by the launcher
    /// GUI's Retrieval Tuning panel). The nested object keeps the
    /// top-level surface flat-friendly for the existing `?key=` filter
    /// (callers needing the whole block use `?key=retrieval_tuning`).
    /// The `schema_version` of the parent envelope stays 1 — these
    /// fields are additive (new readers see them; old readers ignore
    /// them).
    retrieval_tuning: RetrievalTuning,
}

// ─── Handler ─────────────────────────────────────────────────────

async fn project_config(
    State(h): State<LauncherDbHandle>,
    Path(project_id): Path<String>,
    Query(q): Query<ProjectConfigQuery>,
) -> impl IntoResponse {
    // Pre-flight: ?key=<empty> is rejected BEFORE we hit the DB so
    // a malformed client doesn't get charged a SQLite mutex round
    // for a request that was always going to 400.
    if let Some(want) = q.key.as_deref() {
        if want.trim().is_empty() {
            return error_response(
                StatusCode::BAD_REQUEST,
                "invalid_request",
                "query parameter `key` must be non-empty",
            );
        }
    }

    // 1. Identity row. Accept either a UUID or a slug as path-arg;
    // resolver clients (Step 16) pass through whatever the consumer
    // gave them and don't always know which form they have. Try
    // ID-lookup first (the common case from a /projects/by-path round-
    // trip), fall back to slug if absent. Per plan §"Acceptance
    // criterion" property (1), the launcher-root project is reachable
    // via the well-known slug `orchestrator-root` even on a fresh
    // install where the caller has no UUID yet — that's also why the
    // slug fallback exists here rather than as a separate endpoint.
    let project = match h.0.get_project(&project_id) {
        Ok(Some(p)) => p,
        Ok(None) => match h.0.get_project_by_slug(&project_id) {
            Ok(Some(p)) => p,
            Ok(None) => {
                return error_response(
                    StatusCode::NOT_FOUND,
                    "project_not_found",
                    format!("project {} not found (tried both id and slug)", project_id),
                );
            }
            Err(e) => return db_error_response("get project by slug (config)", e),
        },
        Err(e) => return db_error_response("get project (config)", e),
    };

    // 2. KG bindings (multi-row: primary + shared + archive).
    let kg_bindings = match h.0.list_project_kg_bindings(&project.id) {
        Ok(v) => v,
        Err(e) => return db_error_response("list kg bindings", e),
    };

    // 3. Codegraph binding (single row or none).
    let cg_binding = match h.0.get_project_codegraph_binding(&project.id) {
        Ok(v) => v,
        Err(e) => return db_error_response("get codegraph binding", e),
    };

    // 4. KG access matrix.
    let kg_access_rows = match h.0.kg_list_access(&project.id) {
        Ok(v) => v,
        Err(e) => return db_error_response("list kg access", e),
    };

    // 5. Codegraph access matrix (grantee = this project) joined to
    // grantor slug. We could compose this from existing helpers
    // (`codegraph_list_grants_to` + per-row `get_project`), but a
    // single JOIN keeps the read set small and avoids N+1 round-
    // trips through the SQLite mutex. The JOIN is defined inline
    // rather than in vct-launcher-core/db/access.rs because this is
    // the only caller — moving it would add API surface without a
    // second consumer to justify the move. If a second caller
    // appears in v0.2.22+, promote it then.
    let cg_access_slugs = match list_codegraph_grantor_slugs_for_grantee(&h.0, &project.id) {
        Ok(v) => v,
        Err(e) => return db_error_response("list codegraph access", e),
    };

    // 6. active_embedding (module_settings → orchestrator-core).
    // Default 'qwen3' matches the launcher's compiled default.
    let active_embedding = h
        .0
        .get_setting(&project.id, "orchestrator-core", "active_embedding")
        .ok()
        .flatten()
        .and_then(|v| v.as_str().map(String::from))
        .unwrap_or_else(|| "qwen3".to_string());

    // 7. shared_kg_write_disabled (module_settings → orchestrator-core).
    // Default false — match the access-matrix audit's "asymmetric
    // shared-KG access" model where reads are always allowed but
    // writes can be locally gated.
    let shared_kg_write_disabled = h
        .0
        .get_setting(&project.id, "orchestrator-core", "shared_kg_write_disabled")
        .ok()
        .flatten()
        .and_then(|v| v.as_bool())
        .unwrap_or(false);

    // 8. Resolve binding roles.
    let primary_kg = kg_bindings
        .iter()
        .find(|b| b.role == "primary")
        .map(|b| b.collection_name.clone());
    let shared_kg_collection = kg_bindings
        .iter()
        .find(|b| b.role == "shared")
        .map(|b| b.collection_name.clone())
        .unwrap_or_default();
    let development_collection = kg_bindings
        .iter()
        .find(|b| b.role == "archive")
        .map(|b| b.collection_name.clone())
        .unwrap_or_default();

    // service_misconfigured gate: every registered project should
    // have a primary KG binding after the launcher's startup
    // backfill (parent plan §"Acceptance criterion" / step 19).
    // If we land here without one, the backfill hasn't run OR
    // failed silently — surface it loudly so resolver clients can
    // route to the warning path.
    let kg_collection = match primary_kg {
        Some(name) => name,
        None => {
            return error_response(
                StatusCode::SERVICE_UNAVAILABLE,
                "service_misconfigured",
                format!(
                    "project {} has no primary KG binding; run launcher backfill or fix in GUI",
                    project.id
                ),
            );
        }
    };

    // Codegraph collection prefix: bind row first, slug-derived
    // fallback otherwise. Matches the Python analyzer's
    // `_sanitize_collection_prefix`; the launcher's
    // `project_naming::canonical_class_prefix` is the canonical
    // spec but lives in the launcher crate (not core), so we inline
    // an ASCII-safe sanitiser here — used ONLY for the fallback
    // path. The Cargo workspace's `project_naming_parity` test
    // pins the canonical version; this inline copy is intentionally
    // simple because the fallback fires only when a project hasn't
    // run codegraph analysis yet (no bind row), in which case any
    // ASCII-safe prefix is acceptable as a placeholder.
    let code_graph_collection_prefix = cg_binding
        .as_ref()
        .map(|b| b.collection_prefix.clone())
        .unwrap_or_else(|| sanitize_collection_prefix(&project.slug));

    // KG access list: filter access_level='none' out, add own
    // primary collection (always implicit), sort + dedup.
    let mut kg_access_list: Vec<String> = kg_access_rows
        .iter()
        .filter(|(_, level)| level == "read" || level == "write")
        .map(|(coll, _)| coll.clone())
        .collect();
    if !kg_access_list.iter().any(|c| c == &kg_collection) {
        kg_access_list.push(kg_collection.clone());
    }
    kg_access_list.sort();
    kg_access_list.dedup();

    // Codegraph access list: grantor slugs only, plus own slug.
    let mut codegraph_access_list = cg_access_slugs;
    if !codegraph_access_list.iter().any(|s| s == &project.slug) {
        codegraph_access_list.push(project.slug.clone());
    }
    codegraph_access_list.sort();
    codegraph_access_list.dedup();

    // Embeddings: from binding rows when present, otherwise the
    // launcher's compiled defaults.
    let text_embedding = kg_bindings
        .iter()
        .find(|b| b.role == "primary")
        .and_then(|b| b.embedding_model.clone())
        .unwrap_or_else(|| "qwen3-embedding:0.6b".to_string());
    let code_embedding = cg_binding
        .as_ref()
        .and_then(|b| b.embedding_model.clone())
        .unwrap_or_else(|| "CodeSage-Large-v2".to_string());

    // Service URLs: weaviate_url goes through LocalConfig (env +
    // vct-config.toml + compiled default). Ollama URL + gRPC port
    // are not (yet) in LocalConfig — they ride env var → compiled
    // default. When LocalConfig grows fields for these in a future
    // release, swap them in without breaking the wire contract.
    let local_cfg = LocalConfig::load();
    let weaviate_url = local_cfg.weaviate_url;
    let ollama_url = std::env::var("VCT_OLLAMA_URL")
        .ok()
        .filter(|v| !v.is_empty())
        .or_else(|| std::env::var("OLLAMA_URL").ok().filter(|v| !v.is_empty()))
        .unwrap_or_else(|| DEFAULT_OLLAMA_URL.to_string());
    let grpc_port = std::env::var("VCT_GRPC_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .or_else(|| std::env::var("GRPC_PORT").ok().and_then(|v| v.parse().ok()))
        .unwrap_or(DEFAULT_GRPC_PORT);

    // Symlink / UNC path normalization (defense-in-depth). The
    // launcher canonicalises folder_path at registration time, but
    // a follow-on rename / symlink-introduction can leave a
    // non-canonical value in the DB. Re-canonicalise here so the
    // resolver's `project_path` always returns the user-visible
    // canonical form. Best-effort — if canonicalize fails (the
    // folder was deleted, the user is on a network share that
    // refuses canonicalisation, etc.) we return the DB value
    // verbatim so the resolver still works.
    let project_path = dunce::canonicalize(&project.folder_path)
        .ok()
        .and_then(|p| p.to_str().map(String::from))
        .unwrap_or_else(|| project.folder_path.clone());

    // 10. Retrieval tuning — soft-read of the global TOML written by
    // the launcher's Retrieval Tuning panel. Missing / malformed file
    // → calibrated defaults; never errors the resolver out.
    let retrieval_tuning = read_tuning();

    let response = ProjectConfigResponse {
        schema_version: RESOLVER_PROTOCOL_VERSION,
        project_id: project.id.clone(),
        project_path,
        project_slug: project.slug.clone(),
        project_display_name: project.name.clone(),
        code_graph_project: project.slug.clone(),
        code_graph_collection_prefix,
        kg_collection,
        shared_kg_collection,
        development_collection,
        active_embedding,
        embedding_models: EmbeddingModels {
            text: text_embedding,
            code: code_embedding,
        },
        kg_access_list,
        codegraph_access_list,
        weaviate_url,
        ollama_url,
        grpc_port,
        shared_kg_write_disabled,
        retrieval_tuning,
    };

    // 9. ?key= filter — pull a single top-level field by name.
    // Nested-path access (`?key=embedding_models.text`) is NOT
    // supported by design; clients fetch the whole nested object
    // and pick locally.
    if let Some(want) = q.key.as_deref() {
        let want = want.trim();
        return single_field_response(&response, want);
    }

    Json(response).into_response()
}

/// Take a `ProjectConfigResponse`, look up `field`, and either
/// return `200 {field: value}` or `404 field_not_found`.
fn single_field_response(
    response: &ProjectConfigResponse,
    field: &str,
) -> axum::response::Response {
    // Serialize through a generic Value so we don't need a `match
    // field { "project_id" => ... }` arm per field — kept in lock-
    // step with the struct via serde rather than a hand-written
    // dispatch table that would silently drift.
    let value = match serde_json::to_value(response) {
        Ok(v) => v,
        Err(e) => {
            return db_error_response(
                "serialize project config response",
                format!("serde: {}", e),
            );
        }
    };
    let obj = match value.as_object() {
        Some(o) => o,
        None => {
            return error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "internal_error",
                "serialised config is not an object",
            );
        }
    };
    match obj.get(field) {
        Some(v) => {
            let mut single = serde_json::Map::new();
            single.insert(field.to_string(), v.clone());
            Json(serde_json::Value::Object(single)).into_response()
        }
        None => error_response(
            StatusCode::NOT_FOUND,
            "field_not_found",
            format!("field {:?} is not in the project config response", field),
        ),
    }
}

/// JOIN over `codegraph_access` (grantee filter) + `projects`
/// (grantor slug lookup). Returns the list of grantor slugs whose
/// codegraph this project may query.
///
/// This is inlined here rather than added to
/// `vct-launcher-core/src/db/access.rs` because it's the only
/// caller of this particular shape and the launcher GUI uses a
/// different access pattern (per-grantor lookup, not bulk). When a
/// second caller materialises, promote this to a core helper.
fn list_codegraph_grantor_slugs_for_grantee(
    db: &vct_launcher_core::db::Db,
    grantee_project_id: &str,
) -> Result<Vec<String>, String> {
    let guard = db.lock();
    let mut stmt = guard
        .prepare(
            "SELECT p.slug
               FROM codegraph_access ca
               JOIN projects p ON p.id = ca.grantor_project_id
              WHERE ca.grantee_project_id = ?1
                AND ca.access_level = 'read'",
        )
        .map_err(|e| format!("prepare list_codegraph_grantor_slugs: {}", e))?;
    let rows = stmt
        .query_map(params![grantee_project_id], |r| r.get::<_, String>(0))
        .map_err(|e| format!("query list_codegraph_grantor_slugs: {}", e))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|e| format!("collect list_codegraph_grantor_slugs: {}", e))
}

/// Inline ASCII-safe slug → class-prefix sanitiser.
///
/// Used ONLY for the fallback when `project_codegraph_bindings`
/// has no row for this project (codegraph hasn't been analysed
/// yet). The launcher's `project_naming::canonical_class_prefix`
/// is the spec'd version; we don't depend on it here because that
/// module lives in the Tauri-side launcher crate, not in
/// vct-launcher-core, and hauling it into core to satisfy a
/// fallback path would expand the workspace's public-API surface
/// area for no gain. The fallback only fires before first
/// analysis; once analysis runs, the binding row carries the
/// canonical prefix and this function is bypassed.
///
/// Algorithm (mirrors `_sanitize_collection_prefix` in the Python
/// analyzer):
///   1. Replace non-alphanumeric ASCII chars with `_`.
///   2. Capitalize the first character.
///   3. If empty after step 1, return `Project`.
fn sanitize_collection_prefix(slug: &str) -> String {
    let mut out = String::with_capacity(slug.len());
    for ch in slug.chars() {
        if ch.is_ascii_alphanumeric() {
            out.push(ch);
        } else {
            out.push('_');
        }
    }
    let trimmed = out.trim_matches('_');
    if trimmed.is_empty() {
        return "Project".to_string();
    }
    let mut chars = trimmed.chars();
    let first = chars.next().unwrap().to_ascii_uppercase();
    let mut result = String::with_capacity(trimmed.len());
    result.push(first);
    result.extend(chars);
    result
}

// ─── Tests ────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use axum::Router;
    use std::sync::Arc;
    use vct_launcher_core::db::Db;

    /// Seed a minimal project row. Mirrors the helper in
    /// modules_api.rs::tests so the test fixtures stay symmetric
    /// between the two endpoints.
    fn seed_project(db: &Db, id: &str, name: &str, folder: &str, slug: &str) {
        let now = chrono::Utc::now().timestamp_millis();
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                 VALUES (?1, ?2, ?3, 'base', ?4, ?5, ?5)",
                rusqlite::params![id, name, folder, slug, now],
            )
            .unwrap();
    }

    /// Spawn the config_api router on a random local port; return
    /// (base_url, handle). Mirrors `spawn_modules_api_hub` in
    /// modules_api.rs::tests.
    async fn spawn_config_api_hub() -> (String, LauncherDbHandle) {
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

    fn empty_json_obj() -> serde_json::Value {
        serde_json::Value::Object(serde_json::Map::new())
    }

    /// Set up a fully-bound project with primary + shared + archive
    /// KG bindings and a codegraph binding. Used as the canonical
    /// "happy path" fixture across the HTTP tests.
    fn seed_full_project(handle: &LauncherDbHandle, id: &str, slug: &str) {
        // Disambiguate folder_path per-id so two projects can coexist in
        // the same in-memory DB (projects.folder_path has a UNIQUE
        // constraint at the migration level).
        let folder = format!("/tmp/test-config-project-{}", id);
        seed_project(&handle.0, id, "Test Display Name", &folder, slug);
        handle
            .0
            .set_project_kg_binding(
                id,
                "primary",
                &format!("{}_KnowledgeGraph", capitalize(slug)),
                Some("qwen3-embedding:0.6b"),
                Some(1024),
                None,
                None,
                &empty_json_obj(),
            )
            .unwrap();
        handle
            .0
            .set_project_kg_binding(
                id,
                "shared",
                "VibecodedOrchestrator_KnowledgeGraph",
                Some("qwen3-embedding:0.6b"),
                Some(1024),
                None,
                None,
                &empty_json_obj(),
            )
            .unwrap();
        handle
            .0
            .set_project_kg_binding(
                id,
                "archive",
                &format!("{}_Development", capitalize(slug)),
                Some("qwen3-embedding:0.6b"),
                Some(1024),
                None,
                None,
                &empty_json_obj(),
            )
            .unwrap();
        handle
            .0
            .set_project_codegraph_binding(
                id,
                &capitalize(slug),
                Some("CodeSage-Large-v2"),
                Some(2048),
                None,
                None,
                true,
                &empty_json_obj(),
            )
            .unwrap();
    }

    fn capitalize(slug: &str) -> String {
        let mut c = slug.chars();
        match c.next() {
            Some(first) => first.to_ascii_uppercase().to_string() + c.as_str(),
            None => String::new(),
        }
    }

    #[test]
    fn sanitize_collection_prefix_basic() {
        assert_eq!(sanitize_collection_prefix("myproject"), "Myproject");
        assert_eq!(sanitize_collection_prefix("my-project"), "My_project");
        assert_eq!(sanitize_collection_prefix("my project"), "My_project");
        assert_eq!(sanitize_collection_prefix("MyProject"), "MyProject");
        assert_eq!(sanitize_collection_prefix(""), "Project");
        // Pure punctuation collapses to underscores → trim → empty → fallback.
        assert_eq!(sanitize_collection_prefix("---"), "Project");
        // Numeric-only is allowed (Weaviate would reject this server-side; the
        // fallback fires before analysis runs, so the prefix is provisional
        // and gets replaced once the binding row lands).
        assert_eq!(sanitize_collection_prefix("123"), "123");
    }

    #[tokio::test]
    async fn config_returns_404_for_unknown_project() {
        let (base, _h) = spawn_config_api_hub().await;
        let resp = reqwest::get(format!("{}/projects/ghost-id/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 404);
        let body: serde_json::Value = resp.json().await.expect("json body");
        let err = body.get("error").expect("error envelope");
        assert_eq!(
            err.get("code").and_then(|v| v.as_str()),
            Some("project_not_found")
        );
    }

    #[tokio::test]
    async fn config_returns_503_when_no_primary_kg_binding() {
        let (base, h) = spawn_config_api_hub().await;
        seed_project(&h.0, "p-no-kg", "Test", "/tmp/no-kg", "no-kg");
        // No KG binding rows inserted → resolver should refuse and emit
        // service_misconfigured per design doc §1.5.
        let resp = reqwest::get(format!("{}/projects/p-no-kg/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 503);
        let body: serde_json::Value = resp.json().await.expect("json body");
        let err = body.get("error").expect("error envelope");
        assert_eq!(
            err.get("code").and_then(|v| v.as_str()),
            Some("service_misconfigured")
        );
        assert!(err
            .get("message")
            .and_then(|v| v.as_str())
            .map(|s| s.contains("primary KG binding"))
            .unwrap_or(false));
    }

    #[tokio::test]
    async fn config_happy_path_returns_full_envelope() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-happy", "myproject");

        let resp = reqwest::get(format!("{}/projects/p-happy/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        assert_eq!(body.get("project_id").and_then(|v| v.as_str()), Some("p-happy"));
        assert_eq!(body.get("project_slug").and_then(|v| v.as_str()), Some("myproject"));
        // code_graph_project is the legacy alias of project_slug.
        assert_eq!(
            body.get("code_graph_project").and_then(|v| v.as_str()),
            Some("myproject")
        );
        assert_eq!(
            body.get("kg_collection").and_then(|v| v.as_str()),
            Some("Myproject_KnowledgeGraph")
        );
        assert_eq!(
            body.get("shared_kg_collection").and_then(|v| v.as_str()),
            Some("VibecodedOrchestrator_KnowledgeGraph")
        );
        assert_eq!(
            body.get("development_collection").and_then(|v| v.as_str()),
            Some("Myproject_Development")
        );
        assert_eq!(
            body.get("code_graph_collection_prefix").and_then(|v| v.as_str()),
            Some("Myproject")
        );
        // active_embedding defaults to 'qwen3' when module_settings is empty.
        assert_eq!(
            body.get("active_embedding").and_then(|v| v.as_str()),
            Some("qwen3")
        );
        assert_eq!(
            body.get("shared_kg_write_disabled").and_then(|v| v.as_bool()),
            Some(false)
        );

        // Embedding nested object.
        let em = body.get("embedding_models").expect("embedding_models");
        assert_eq!(em.get("text").and_then(|v| v.as_str()), Some("qwen3-embedding:0.6b"));
        assert_eq!(em.get("code").and_then(|v| v.as_str()), Some("CodeSage-Large-v2"));

        // kg_access_list: with no kg_collection_access rows, the project's own
        // primary collection is still added implicitly (project always has full
        // access to itself per design doc §1.3 note).
        let kg_list = body
            .get("kg_access_list")
            .and_then(|v| v.as_array())
            .expect("kg_access_list");
        let kg_strs: Vec<&str> = kg_list.iter().filter_map(|v| v.as_str()).collect();
        assert!(kg_strs.contains(&"Myproject_KnowledgeGraph"));

        // codegraph_access_list: own slug is always present.
        let cg_list = body
            .get("codegraph_access_list")
            .and_then(|v| v.as_array())
            .expect("codegraph_access_list");
        let cg_strs: Vec<&str> = cg_list.iter().filter_map(|v| v.as_str()).collect();
        assert!(cg_strs.contains(&"myproject"));
    }

    #[tokio::test]
    async fn config_access_matrix_filters_none_rows() {
        // Design doc §1.8 (access-matrix discipline) — rows with
        // access_level='none' must not appear in kg_access_list.
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-deny", "myproject");

        // Grant: read on a peer collection, none on another.
        h.0.kg_set_access("p-deny", "Peer_KnowledgeGraph", "read")
            .unwrap();
        h.0.kg_set_access("p-deny", "Denied_KnowledgeGraph", "none")
            .unwrap();

        let resp = reqwest::get(format!("{}/projects/p-deny/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        let kg_list: Vec<String> = body
            .get("kg_access_list")
            .and_then(|v| v.as_array())
            .expect("kg_access_list")
            .iter()
            .filter_map(|v| v.as_str().map(String::from))
            .collect();

        assert!(
            kg_list.contains(&"Peer_KnowledgeGraph".to_string()),
            "expected Peer_KnowledgeGraph in access list, got: {:?}",
            kg_list
        );
        assert!(
            kg_list.contains(&"Myproject_KnowledgeGraph".to_string()),
            "expected own primary in access list, got: {:?}",
            kg_list
        );
        assert!(
            !kg_list.contains(&"Denied_KnowledgeGraph".to_string()),
            "Denied_KnowledgeGraph (access_level='none') leaked: {:?}",
            kg_list
        );
    }

    #[tokio::test]
    async fn config_codegraph_access_list_resolves_grantor_slugs() {
        // Two projects; project A grants project B read access to A's
        // codegraph. B's resolver response must list A's slug.
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "proj-a", "project-a");
        seed_full_project(&h, "proj-b", "project-b");
        h.0.codegraph_grant("proj-a", "proj-b", "read").unwrap();

        let resp = reqwest::get(format!("{}/projects/proj-b/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        let cg_list: Vec<String> = body
            .get("codegraph_access_list")
            .and_then(|v| v.as_array())
            .expect("codegraph_access_list")
            .iter()
            .filter_map(|v| v.as_str().map(String::from))
            .collect();

        assert!(cg_list.contains(&"project-a".to_string()));
        assert!(cg_list.contains(&"project-b".to_string()));

        // Inverse: A's response must NOT contain B's slug (no grant the other way).
        let resp_a = reqwest::get(format!("{}/projects/proj-a/config", base))
            .await
            .expect("hub reachable");
        let body_a: serde_json::Value = resp_a.json().await.expect("json body");
        let cg_list_a: Vec<String> = body_a
            .get("codegraph_access_list")
            .and_then(|v| v.as_array())
            .expect("codegraph_access_list")
            .iter()
            .filter_map(|v| v.as_str().map(String::from))
            .collect();
        assert!(cg_list_a.contains(&"project-a".to_string()));
        assert!(!cg_list_a.contains(&"project-b".to_string()));
    }

    #[tokio::test]
    async fn config_key_filter_returns_single_field_envelope() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-key", "myproject");

        let resp = reqwest::get(format!(
            "{}/projects/p-key/config?key=kg_collection",
            base
        ))
        .await
        .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        // Single-field envelope shape: {"kg_collection": "..."} with NO other keys.
        let obj = body.as_object().expect("object");
        assert_eq!(obj.len(), 1);
        assert_eq!(
            obj.get("kg_collection").and_then(|v| v.as_str()),
            Some("Myproject_KnowledgeGraph")
        );
    }

    #[tokio::test]
    async fn config_key_filter_with_nested_object_returns_nested() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-nested", "myproject");

        let resp = reqwest::get(format!(
            "{}/projects/p-nested/config?key=embedding_models",
            base
        ))
        .await
        .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        let inner = body.get("embedding_models").expect("nested");
        assert_eq!(inner.get("text").and_then(|v| v.as_str()), Some("qwen3-embedding:0.6b"));
        assert_eq!(inner.get("code").and_then(|v| v.as_str()), Some("CodeSage-Large-v2"));
    }

    #[tokio::test]
    async fn config_key_filter_returns_400_on_empty_key() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-empty-key", "myproject");

        // `?key=` with no value, or `?key= ` (just whitespace), should
        // 400 before any DB read. Note: reqwest URL-encodes the space.
        let resp = reqwest::get(format!("{}/projects/p-empty-key/config?key=", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 400);
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(
            body.get("error")
                .and_then(|e| e.get("code"))
                .and_then(|v| v.as_str()),
            Some("invalid_request")
        );
    }

    #[tokio::test]
    async fn config_key_filter_returns_404_for_unknown_field() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-unknown-key", "myproject");

        let resp = reqwest::get(format!(
            "{}/projects/p-unknown-key/config?key=does_not_exist",
            base
        ))
        .await
        .expect("hub reachable");
        assert_eq!(resp.status(), 404);
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(
            body.get("error")
                .and_then(|e| e.get("code"))
                .and_then(|v| v.as_str()),
            Some("field_not_found")
        );
    }

    #[tokio::test]
    async fn config_emits_retrieval_tuning_defaults_when_file_missing() {
        // v0.2.22 Item #13. When <vct_root_dir>/retrieval-tuning.toml
        // is absent, the resolver returns the calibrated defaults from
        // knowledge/concepts/score-driven-retrieval-tiers.md.
        //
        // VCT_STATE_DIR is process-wide; the parent test harness in
        // vct-launcher-core::paths::tests already serialises mutation,
        // but here we set it to a fresh tempdir (with no .toml in it)
        // BEFORE spawning the hub so the global resolver path lands
        // in a guaranteed-empty directory.
        let tmp = tempfile::TempDir::new().unwrap();
        let prev_state = std::env::var_os("VCT_STATE_DIR");
        std::env::set_var("VCT_STATE_DIR", tmp.path());

        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-tuning-default", "myproject");

        let resp = reqwest::get(format!(
            "{}/projects/p-tuning-default/config",
            base
        ))
        .await
        .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        let rt = body
            .get("retrieval_tuning")
            .expect("retrieval_tuning present");

        // Defaults from score-driven-retrieval-tiers.md.
        assert!(
            (rt.get("code_graph_score_floor").and_then(|v| v.as_f64()).unwrap() - 0.35).abs()
                < 1e-9
        );
        assert!(
            (rt.get("kg_tier_min").and_then(|v| v.as_f64()).unwrap() - 0.42).abs() < 1e-9
        );
        assert!(
            (rt.get("kg_tier_single_chunk").and_then(|v| v.as_f64()).unwrap() - 0.55).abs()
                < 1e-9
        );
        assert!(
            (rt.get("kg_tier_three_chunks").and_then(|v| v.as_f64()).unwrap() - 0.65).abs()
                < 1e-9
        );
        assert!(
            (rt.get("kg_tier_full").and_then(|v| v.as_f64()).unwrap() - 0.75).abs() < 1e-9
        );

        match prev_state {
            Some(v) => std::env::set_var("VCT_STATE_DIR", v),
            None => std::env::remove_var("VCT_STATE_DIR"),
        }
    }

    #[tokio::test]
    async fn config_emits_retrieval_tuning_from_disk() {
        // v0.2.22 Item #13. When <vct_root_dir>/retrieval-tuning.toml
        // exists with valid values, the resolver returns those values
        // verbatim (no defaulting / no clamping).
        let tmp = tempfile::TempDir::new().unwrap();
        let prev_state = std::env::var_os("VCT_STATE_DIR");
        std::env::set_var("VCT_STATE_DIR", tmp.path());
        std::fs::write(
            tmp.path().join("retrieval-tuning.toml"),
            "\
code_graph_score_floor = 0.4
kg_tier_min = 0.5
kg_tier_single_chunk = 0.6
kg_tier_three_chunks = 0.7
kg_tier_full = 0.8
",
        )
        .unwrap();

        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-tuning-custom", "myproject");

        let resp = reqwest::get(format!(
            "{}/projects/p-tuning-custom/config",
            base
        ))
        .await
        .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        let rt = body
            .get("retrieval_tuning")
            .expect("retrieval_tuning present");

        assert!(
            (rt.get("code_graph_score_floor").and_then(|v| v.as_f64()).unwrap() - 0.4).abs()
                < 1e-9
        );
        assert!(
            (rt.get("kg_tier_min").and_then(|v| v.as_f64()).unwrap() - 0.5).abs() < 1e-9
        );
        assert!(
            (rt.get("kg_tier_full").and_then(|v| v.as_f64()).unwrap() - 0.8).abs() < 1e-9
        );

        match prev_state {
            Some(v) => std::env::set_var("VCT_STATE_DIR", v),
            None => std::env::remove_var("VCT_STATE_DIR"),
        }
    }

    #[tokio::test]
    async fn config_key_filter_returns_retrieval_tuning() {
        // Single-field filter on the new nested object must return the
        // whole RetrievalTuning struct (the resolver's ?key= filter
        // operates on top-level fields and returns nested objects as-is).
        let tmp = tempfile::TempDir::new().unwrap();
        let prev_state = std::env::var_os("VCT_STATE_DIR");
        std::env::set_var("VCT_STATE_DIR", tmp.path());

        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-key-tuning", "myproject");

        let resp = reqwest::get(format!(
            "{}/projects/p-key-tuning/config?key=retrieval_tuning",
            base
        ))
        .await
        .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        let obj = body.as_object().expect("object");
        assert_eq!(obj.len(), 1);
        let nested = obj.get("retrieval_tuning").expect("nested");
        assert!(
            (nested.get("kg_tier_min").and_then(|v| v.as_f64()).unwrap() - 0.42).abs() < 1e-9
        );

        match prev_state {
            Some(v) => std::env::set_var("VCT_STATE_DIR", v),
            None => std::env::remove_var("VCT_STATE_DIR"),
        }
    }

    #[tokio::test]
    async fn config_response_carries_schema_version_field() {
        // v0.2.22 Item #2 — forward-compat anchor. Every successful
        // resolver response MUST carry `schema_version` so a future
        // client paired with an older hub (or a hub paired with an
        // older client) can degrade with a one-line warning rather
        // than silently mis-parsing. Pinned to 1 at v0.2.21/v0.2.22;
        // bumps go through the comment block at the top of this
        // file AND `RESOLVER_PROTOCOL_VERSION` in
        // `vco_lib/project_config.py`.
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-schema", "myproject");

        let resp = reqwest::get(format!("{}/projects/p-schema/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        assert_eq!(
            body.get("schema_version").and_then(|v| v.as_u64()),
            Some(RESOLVER_PROTOCOL_VERSION as u64),
            "schema_version must be present and equal to RESOLVER_PROTOCOL_VERSION; \
             body={}",
            body,
        );
    }

    #[tokio::test]
    async fn config_key_filter_returns_schema_version() {
        // `?key=schema_version` is a single-field filter on the new
        // top-level field — must work the same as any other top-
        // level field. Useful for a future client that wants to
        // probe just the version before deciding which fields to
        // ask for.
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-schema-key", "myproject");

        let resp = reqwest::get(format!(
            "{}/projects/p-schema-key/config?key=schema_version",
            base
        ))
        .await
        .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        let obj = body.as_object().expect("object");
        assert_eq!(obj.len(), 1);
        assert_eq!(
            obj.get("schema_version").and_then(|v| v.as_u64()),
            Some(RESOLVER_PROTOCOL_VERSION as u64),
        );
    }

    #[tokio::test]
    async fn config_emits_active_embedding_from_module_settings() {
        // When module_settings has an explicit value, it overrides the
        // default 'qwen3'.
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-openai", "myproject");
        h.0.set_setting(
            "p-openai",
            "orchestrator-core",
            "active_embedding",
            &serde_json::Value::String("openai".to_string()),
        )
        .unwrap();
        h.0.set_setting(
            "p-openai",
            "orchestrator-core",
            "shared_kg_write_disabled",
            &serde_json::Value::Bool(true),
        )
        .unwrap();

        let resp = reqwest::get(format!("{}/projects/p-openai/config", base))
            .await
            .expect("hub reachable");
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(
            body.get("active_embedding").and_then(|v| v.as_str()),
            Some("openai")
        );
        assert_eq!(
            body.get("shared_kg_write_disabled").and_then(|v| v.as_bool()),
            Some(true)
        );
    }
}
