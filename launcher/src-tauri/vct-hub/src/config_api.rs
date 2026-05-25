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

use std::path::{Path as StdPath, PathBuf};

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
    ///
    /// ⚠️ DO NOT use this as the Weaviate write-target prefix. The
    /// slug is sanitised independently by the analyzer's
    /// `_sanitize_collection_prefix`, producing a prefix that may
    /// DIVERGE from the launcher's `project_codegraph_bindings.collection_prefix`
    /// (e.g. slug `orchestrator-root` → `Orchestrator_root`, but the
    /// binding row says `VibeCodedOrchestrator`). v0.2.23 split: use
    /// `code_graph_collection_prefix` for the write target, keep
    /// `code_graph_project` for codegraph-access matrix joins where
    /// the slug is the actual key. See knowledge/concepts/
    /// multi-codebase-code-graph-detection.md for the v0.2.22→v0.2.23
    /// post-rename codegraph reconciliation story.
    code_graph_project: String,
    /// Canonical Weaviate write-target prefix sourced from
    /// `project_codegraph_bindings.collection_prefix`. This is the
    /// single source of truth for hooks + the analyzer. Falls back to
    /// the slug-sanitised version when no binding row exists (i.e.
    /// before the project has been analysed for the first time).
    code_graph_collection_prefix: String,
    kg_collection: String,
    shared_kg_collection: String,
    development_collection: String,
    /// Per-project Weaviate diagrams collection (Phase 1.5 — Diagrams
    /// Integration, fix/a1-indexing-pipeline 2026-05-25). Derived from
    /// `kg_collection` by swapping the `_KnowledgeGraph` suffix for
    /// `_Diagrams`. Falls back to `<slug-sanitized>_Diagrams` when the
    /// primary KG binding's collection name doesn't end with
    /// `_KnowledgeGraph` (a non-default rename pattern). Consumed by
    /// `claude_mcp_servers/weaviate_mcp/server.py::DIAGRAMS_COLLECTION`
    /// for the hybrid_search diagrams fan-out and by
    /// `vco_lib.diagram_indexer::index_diagram_async` for the Weaviate
    /// upsert target.
    ///
    /// Additive field — pre-fix Python clients see an unknown field and
    /// ignore it (the Python ProjectConfig parser back-fills with an
    /// empty string when missing, mirroring the existing `shared_kg_collection`
    /// / `development_collection` empty-string-on-missing pattern).
    diagrams_collection: String,
    active_embedding: String,
    embedding_models: EmbeddingModels,
    kg_access_list: Vec<String>,
    codegraph_access_list: Vec<String>,
    /// v0.2.34 A7 — peer-project diagrams collection names this
    /// project may search. Sourced from the ``diagram_access`` table
    /// (grantor side joined to ``projects.name`` for the grantee =
    /// this project), then sanitised + suffixed to canonical Weaviate
    /// class names (``<SanitizedName>_Diagrams``).
    ///
    /// Discrete from ``kg_access_list``: pre-v0.2.34 the MCP fell
    /// back to the KG list (wrong granularity). The MCP now reads
    /// this field via ``ProjectConfig.diagrams_access_list`` with
    /// no KG fallback. Additive field — pre-v0.2.34 clients see an
    /// unknown field and ignore it; pre-v0.2.34 hubs paired with
    /// v0.2.34+ clients fall back to the env CSV
    /// (``VCT_DIAGRAMS_ACCESS_LIST`` — written by the Python
    /// ``config_projection`` contract via the same JOIN).
    diagrams_access_list: Vec<String>,
    weaviate_url: String,
    ollama_url: String,
    grpc_port: u16,
    shared_kg_write_disabled: bool,
    /// v0.2.31 — absolute path to Claude Code's per-workspace session-
    /// transcript directory (``~/.claude/projects/<slug>/``). The
    /// launcher computes this once from ``projects.folder_path`` using
    /// :func:`claude_session_dir_for` (canonical slug rule). Consumers
    /// that need to find Claude's session-jsonl files for a workspace
    /// (e.g. the RL citation-monitor in ``claude_mcp_servers/
    /// weaviate_mcp/server.py``) read this field rather than re-
    /// implementing the slug rule inline.
    ///
    /// The directory may not exist on disk yet for a fresh workspace
    /// that hasn't been opened in Claude Code — consumers must check
    /// ``Path::exists`` themselves. Additive field — pre-v0.2.31
    /// clients see an unknown field and ignore it. See
    /// ``knowledge/concepts/launcher-as-router.md`` for the broader
    /// "launcher-is-source-of-truth" pattern.
    claude_session_dir: String,
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

    // 5b. Diagrams access matrix (v0.2.34 A7). Same JOIN shape as the
    // codegraph variant above, but reads ``diagram_access`` and
    // pulls ``projects.name`` (display name) rather than ``slug`` —
    // the diagrams collection-naming rule keys on the canonicalised
    // project NAME (the indexer writes ``<SanitizedName>_Diagrams``
    // rows into Weaviate). Returns the already-canonical Weaviate
    // class names so the MCP can use them as-is (mirrors the
    // kg_access_list contract: hub returns canonical class names,
    // env-fallback returns raw names + MCP sanitises).
    let diagrams_access_list_raw =
        match list_diagram_grantor_names_for_grantee(&h.0, &project.id) {
            Ok(v) => v,
            Err(e) => return db_error_response("list diagram access", e),
        };
    let mut diagrams_access_list: Vec<String> = diagrams_access_list_raw
        .iter()
        .map(|name| format!("{}_Diagrams", sanitize_diagrams_class_prefix(name)))
        .collect();
    diagrams_access_list.sort();
    diagrams_access_list.dedup();

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

    // Diagrams collection (Phase 1.5 — fix/a1-indexing-pipeline 2026-05-25).
    // No dedicated kg_bindings role yet; derive from the primary KG
    // collection via the canonical suffix swap (`_KnowledgeGraph` →
    // `_Diagrams`). Mirrors the Python rule in
    // `vco_lib.config_projection::project_env_from_db` and
    // `vco_lib.project_init::derive_project_collection_names` so the
    // indexer (Python) and the MCP resolver (Python via hub) agree on
    // the same canonical name. Computed after `kg_collection` is
    // unwrapped below so we have the post-binding-resolution name.

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

    // Diagrams collection name — derived from `kg_collection` once it's
    // unwrapped from the Option above. Suffix swap mirrors the Python
    // contract; the slug-sanitized fallback handles the non-canonical
    // rename case (primary binding doesn't end with `_KnowledgeGraph`).
    let diagrams_collection = if kg_collection.ends_with("_KnowledgeGraph") {
        let basename = &kg_collection[..kg_collection.len() - "_KnowledgeGraph".len()];
        format!("{}_Diagrams", basename)
    } else {
        format!("{}_Diagrams", sanitize_collection_prefix(&project.slug))
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

    // 11. Claude session-transcript directory (v0.2.31). Computed from
    // the canonical (post-dunce-canonicalisation) project_path. Pure
    // function — the slug rule mirrors Anthropic's Claude Code rule:
    // `/` + `_` + `.` → `-`. See `claude_session_dir_for` doc-comment
    // for the rationale + open questions (space / unicode).
    let claude_session_dir = claude_session_dir_for(StdPath::new(&project_path))
        .to_string_lossy()
        .into_owned();

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
        diagrams_collection,
        active_embedding,
        embedding_models: EmbeddingModels {
            text: text_embedding,
            code: code_embedding,
        },
        kg_access_list,
        codegraph_access_list,
        diagrams_access_list,
        weaviate_url,
        ollama_url,
        grpc_port,
        shared_kg_write_disabled,
        claude_session_dir,
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

/// JOIN over ``diagram_access`` (grantee filter) + ``projects``
/// (grantor name lookup). Returns the list of grantor project NAMES
/// (display names — ``projects.name``) whose diagrams this project
/// may search.
///
/// v0.2.34 A7. Mirrors ``list_codegraph_grantor_slugs_for_grantee``
/// in shape but reads from a different access-matrix table and
/// pulls the display NAME rather than ``slug`` — the diagrams
/// indexer's collection-naming rule keys on the canonicalised
/// project name, not the slug. Caller is expected to sanitise +
/// suffix the returned names into ``<Sanitized>_Diagrams`` class
/// names; this helper stays close to the raw DB shape so a future
/// caller that wants names for a different purpose (audit panel,
/// UI rendering) can consume them directly.
///
/// Inlined here rather than promoted to vct-launcher-core for the
/// same reason as the codegraph sibling: single caller today.
/// Parameterised SQL — no string concat.
fn list_diagram_grantor_names_for_grantee(
    db: &vct_launcher_core::db::Db,
    grantee_project_id: &str,
) -> Result<Vec<String>, String> {
    let guard = db.lock();
    let mut stmt = guard
        .prepare(
            "SELECT p.name
               FROM diagram_access da
               JOIN projects p ON p.id = da.grantor_project_id
              WHERE da.grantee_project_id = ?1
                AND da.access_level = 'read'
              ORDER BY p.name",
        )
        .map_err(|e| format!("prepare list_diagram_grantor_names: {}", e))?;
    let rows = stmt
        .query_map(params![grantee_project_id], |r| r.get::<_, String>(0))
        .map_err(|e| format!("query list_diagram_grantor_names: {}", e))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|e| format!("collect list_diagram_grantor_names: {}", e))
}

/// Sanitiser for a project display name → Weaviate class prefix used
/// in diagrams collection names (``<Sanitized>_Diagrams``).
///
/// **Canonical rule** (cross-language, locked 2026-05-25 by cr-b2):
/// mirrors the Python ``vco_lib.project_init.sanitize_for_weaviate_class``
/// — the documented source-of-truth per
/// ``derive_project_collection_names``'s docstring. Replaces the
/// pre-cr-b2 underscore-replace algorithm that diverged from Python
/// for any project name containing non-alphanumeric characters
/// (spaces, hyphens, dots). The divergence silently broke
/// cross-project diagrams visibility on first invocation — the
/// indexer wrote under one class, the MCP searched a different one,
/// the hub's ``diagrams_access_list`` pointed at a third.
///
/// Rule (must match Python's ``sanitize_for_weaviate_class``):
///   1. Split on any non-alphanumeric run (``[^A-Za-z0-9]+`` —
///      treats ``_``, ``-``, space, dot, etc. as separators).
///   2. PascalCase each surviving part (uppercase first char,
///      preserve rest).
///   3. Concatenate (NO joiner — no underscore between parts).
///   4. If nothing survives OR the result starts with a non-letter,
///      fall back to ``"vct"`` (lowercase — Weaviate uppercases the
///      first char on POST regardless, and the prefix flags the
///      class as installer-managed).
///
/// Examples (pinned by ``tests/fixtures/diagrams_class_name_parity.json``):
///   ``"Foo Bar"``        → ``"FooBar"``     (was ``"Foo_Bar"`` pre-cr-b2)
///   ``"my-project_v2"``  → ``"MyProjectV2"`` (was ``"My_project_v2"``)
///   ``"VCODev"``         → ``"VCODev"``     (round-trips identically)
///   ``"étude"``          → ``"Tude"``       (non-ASCII stripped, matches Python)
///   ``"123abc"``         → ``"vct"``        (leading digit invalid → fallback)
///   ``"!!!"``            → ``"vct"``        (all-symbol → empty → fallback)
///
/// **Cross-language parity** is verified by
/// ``launcher/src-tauri/tests/diagrams_class_name_parity.rs`` (Rust)
/// and ``tests/test_diagrams_class_name_parity.py`` (Python), both
/// consuming the shared JSON fixture at
/// ``tests/fixtures/diagrams_class_name_parity.json``.
///
/// Distinct from ``sanitize_collection_prefix`` (slug → codegraph
/// prefix) above: that one is a separate algorithm for the codegraph
/// fallback path (replaces non-alnum with ``_``, preserves
/// underscores, capitalises first char) and is only used when the
/// codegraph binding row hasn't been written yet. The two functions
/// are deliberately distinct — codegraph keeps underscores because
/// its on-disk schema convention does (``SimRacing_AI_CodeFunction``),
/// diagrams strips them because ``sanitize_for_weaviate_class`` does.
fn sanitize_diagrams_class_prefix(project_name: &str) -> String {
    const FALLBACK: &str = "vct";
    // Step 1 + 2: split on non-alphanumeric runs; PascalCase each part
    // (uppercase first char, preserve the rest verbatim). Mirrors Python's
    // `re.split(r"[^A-Za-z0-9]+", ...)` followed by `p[:1].upper() + p[1:]`.
    let mut pascal = String::with_capacity(project_name.len());
    let mut in_part = false;
    let mut first_char_of_part = true;
    for ch in project_name.chars() {
        if ch.is_ascii_alphanumeric() {
            if !in_part {
                in_part = true;
                first_char_of_part = true;
            }
            if first_char_of_part {
                // Uppercase first char of each part (ASCII-only matches
                // Python's behaviour exactly for the chars we accept;
                // non-ASCII chars are already filtered by the alnum check
                // above, so the codepath never sees them here).
                for upper in ch.to_uppercase() {
                    pascal.push(upper);
                }
                first_char_of_part = false;
            } else {
                pascal.push(ch);
            }
        } else {
            // Non-alphanumeric → separator; end current part.
            in_part = false;
        }
    }

    // Step 3 + 4: fallback if empty or doesn't start with a letter.
    // Python's `sanitize_for_weaviate_class` falls back to lowercase
    // `"vct"` (Weaviate uppercases the first char on POST regardless).
    if pascal.is_empty() {
        return FALLBACK.to_string();
    }
    let first = pascal.chars().next().expect("pascal non-empty above");
    if !first.is_ascii_alphabetic() {
        return FALLBACK.to_string();
    }
    pascal
}

/// Compute Claude Code's session-jsonl directory for a workspace.
///
/// Rust counterpart of :func:`vco_lib.project_config.claude_session_dir_for`.
/// Both implementations MUST stay in lock-step — drift would mean a
/// hub-resolved value disagrees with the MCP fallback, defeating the
/// purpose of routing the lookup through the hub in the first place.
///
/// Verified rule (against ``~/.claude/projects/`` on Linux, 2026-05-23,
/// against Claude Code 2.1.143):
///
///   * ``/`` → ``-``  (path separator)
///   * ``_`` → ``-``  (e.g. ``VCO_dev`` → ``VCO-dev``)
///   * ``.`` → ``-``  (e.g. ``.claude/worktrees`` → ``-claude-worktrees``)
///
/// Returns ``~/.claude/projects/<slug>/`` as a ``PathBuf``. The returned
/// path may not exist on disk yet for a fresh workspace; callers must
/// check ``Path::exists`` themselves.
///
/// Uses the same ``directories::UserDirs`` HOME-resolution pattern as
/// `vct_launcher_core::paths::vct_root_dir` (cross-OS). If that
/// returns ``None`` (no home directory configured — extremely rare;
/// only happens in stripped-down container envs), the helper returns
/// a relative path under ``.claude/projects/`` so the resolver still
/// emits a non-empty value rather than panicking.
fn claude_session_dir_for(workspace_path: &StdPath) -> PathBuf {
    let workspace_str = workspace_path.to_string_lossy();
    let slug: String = workspace_str
        .chars()
        .map(|c| match c {
            '/' | '_' | '.' => '-',
            other => other,
        })
        .collect();
    let home = directories::UserDirs::new()
        .map(|d| d.home_dir().to_path_buf())
        .unwrap_or_else(|| PathBuf::from(""));
    home.join(".claude").join("projects").join(slug)
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
                "VibeCodedOrchestrator_KnowledgeGraph",
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
    fn claude_session_dir_handles_underscores() {
        // v0.2.31 regression test: the citation-monitor bug was caused
        // by an inline slug computation that only handled `/` → `-`.
        // Claude Code's actual rule ALSO converts `_` (and `.`) → `-`.
        // Underscored workspace paths (VCO_dev, AI_hive) were the
        // root cause of the 97.7% orphan-citation rate.
        let p = StdPath::new("/home/user/Desktop/PROGETTI/VCO_dev");
        let dir = claude_session_dir_for(p);
        assert_eq!(
            dir.file_name().unwrap().to_str().unwrap(),
            "-home-user-Desktop-PROGETTI-VCO-dev",
            "slug must replace both '/' and '_' with '-'",
        );

        let p2 = StdPath::new("/home/user/Desktop/PROGETTI/AI_hive");
        let dir2 = claude_session_dir_for(p2);
        assert_eq!(
            dir2.file_name().unwrap().to_str().unwrap(),
            "-home-user-Desktop-PROGETTI-AI-hive",
        );
    }

    #[test]
    fn claude_session_dir_passthrough_without_underscores() {
        // Workspaces without underscores already worked in the pre-fix
        // implementation. Pin the non-regression to ensure the new
        // helper's behaviour matches the old inline string-replace for
        // the cases that were never broken.
        let p = StdPath::new("/home/user/Desktop/PROGETTI/vibecoded-orchestrator");
        let dir = claude_session_dir_for(p);
        assert_eq!(
            dir.file_name().unwrap().to_str().unwrap(),
            "-home-user-Desktop-PROGETTI-vibecoded-orchestrator",
        );
    }

    #[test]
    fn claude_session_dir_handles_dots() {
        // Verified against `~/.claude/projects/` on Linux: worktree
        // paths under `.claude/` are stored with `.` → `-` substitution
        // (e.g. `/home/u/VCO_dev/.claude/worktrees/foo` becomes
        // `-home-u-VCO-dev--claude-worktrees-foo`). The double-dash is
        // a natural consequence of the rule, not a separate special-case.
        let p = StdPath::new("/home/u/VCO_dev/.claude/worktrees/foo");
        let dir = claude_session_dir_for(p);
        assert_eq!(
            dir.file_name().unwrap().to_str().unwrap(),
            "-home-u-VCO-dev--claude-worktrees-foo",
        );
    }

    #[tokio::test]
    async fn config_response_carries_claude_session_dir_field() {
        // v0.2.31 — every successful resolver response MUST carry the
        // `claude_session_dir` field so the RL citation-monitor (and
        // future consumers) can look up Claude's session-transcript
        // directory without re-implementing the slug rule.
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-session", "myproject");

        let resp = reqwest::get(format!("{}/projects/p-session/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        let csd = body
            .get("claude_session_dir")
            .and_then(|v| v.as_str())
            .expect("claude_session_dir present and is string");

        // The seed inserts folder_path = "/tmp/test-config-project-p-session".
        // Verify the value contains the underscore-substituted slug
        // (`p_session` doesn't appear because the seeded folder has a `-`,
        // not `_`, but the trailing path component is exercised end-to-end).
        assert!(
            csd.ends_with("-tmp-test-config-project-p-session"),
            "expected slug ending with '-tmp-test-config-project-p-session', got: {}",
            csd,
        );
        // And it must be anchored under .claude/projects/.
        assert!(
            csd.contains(".claude") || csd.contains(".claude/projects"),
            "expected path under .claude/projects/, got: {}",
            csd,
        );
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
            Some("VibeCodedOrchestrator_KnowledgeGraph")
        );
        assert_eq!(
            body.get("development_collection").and_then(|v| v.as_str()),
            Some("Myproject_Development")
        );
        // Phase 1.5 — fix/a1-indexing-pipeline 2026-05-25. Diagrams
        // collection is derived from the primary KG via the canonical
        // `_KnowledgeGraph` → `_Diagrams` suffix swap.
        assert_eq!(
            body.get("diagrams_collection").and_then(|v| v.as_str()),
            Some("Myproject_Diagrams")
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

    /// Seed a project with an explicit display NAME (distinct from the
    /// slug). The default `seed_full_project` helper hard-codes
    /// "Test Display Name" which is fine for tests that only care
    /// about IDs / slugs, but A7's diagrams resolver reads
    /// `projects.name` and sanitises it into a class prefix — so
    /// per-test distinct names are needed for the cross-grant assertions.
    fn seed_project_with_distinct_name(
        handle: &LauncherDbHandle,
        id: &str,
        name: &str,
        slug: &str,
    ) {
        let folder = format!("/tmp/test-config-project-{}", id);
        let now = chrono::Utc::now().timestamp_millis();
        let guard = handle.0.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                 VALUES (?1, ?2, ?3, 'base', ?4, ?5, ?5)",
                rusqlite::params![id, name, folder, slug, now],
            )
            .unwrap();
        drop(guard);
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
                "VibeCodedOrchestrator_KnowledgeGraph",
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

    #[tokio::test]
    async fn config_diagrams_access_list_resolves_grantor_names() {
        // v0.2.34 A7 — independent diagrams access matrix. Project A
        // grants project B read access to A's diagrams; B's resolver
        // response must list A's *_Diagrams collection name. The
        // grant uses `set_diagram_access` (project-id-based) and the
        // hub joins back to projects.name + sanitises.
        let (base, h) = spawn_config_api_hub().await;
        seed_project_with_distinct_name(&h, "proj-a", "ProjectA", "project-a");
        seed_project_with_distinct_name(&h, "proj-b", "ProjectB", "project-b");
        h.0.set_diagram_access("proj-a", "proj-b", "read").unwrap();

        let resp = reqwest::get(format!("{}/projects/proj-b/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        let dg_list: Vec<String> = body
            .get("diagrams_access_list")
            .and_then(|v| v.as_array())
            .expect("diagrams_access_list present")
            .iter()
            .filter_map(|v| v.as_str().map(String::from))
            .collect();
        // ProjectA → sanitised "ProjectA" + "_Diagrams"
        assert!(
            dg_list.contains(&"ProjectA_Diagrams".to_string()),
            "expected ProjectA_Diagrams, got: {:?}",
            dg_list,
        );

        // Inverse: A's response must NOT contain B's diagrams collection
        // (no grant the other way).
        let resp_a = reqwest::get(format!("{}/projects/proj-a/config", base))
            .await
            .expect("hub reachable");
        let body_a: serde_json::Value = resp_a.json().await.expect("json body");
        let dg_list_a: Vec<String> = body_a
            .get("diagrams_access_list")
            .and_then(|v| v.as_array())
            .expect("diagrams_access_list present")
            .iter()
            .filter_map(|v| v.as_str().map(String::from))
            .collect();
        assert!(
            !dg_list_a.contains(&"ProjectB_Diagrams".to_string()),
            "ProjectB_Diagrams should NOT be in proj-a's list: {:?}",
            dg_list_a,
        );
    }

    #[tokio::test]
    async fn config_diagrams_access_list_independent_of_kg_access() {
        // Granular bug guard: granting KG access alone must NOT leak
        // diagrams visibility, and vice versa. Pre-v0.2.34 the MCP
        // piggybacked VCT_KG_ACCESS_LIST → granting KG leaked diagrams.
        let (base, h) = spawn_config_api_hub().await;
        seed_project_with_distinct_name(&h, "p-a", "ProjectA", "project-a");
        seed_project_with_distinct_name(&h, "p-b", "ProjectB", "project-b");
        // KG-only grant (A → B).
        h.0.kg_set_access("p-b", "ProjectA_KnowledgeGraph", "read")
            .unwrap();

        let resp = reqwest::get(format!("{}/projects/p-b/config", base))
            .await
            .expect("hub reachable");
        let body: serde_json::Value = resp.json().await.expect("json body");
        let dg_list: Vec<String> = body
            .get("diagrams_access_list")
            .and_then(|v| v.as_array())
            .expect("diagrams_access_list present")
            .iter()
            .filter_map(|v| v.as_str().map(String::from))
            .collect();
        assert!(
            dg_list.is_empty(),
            "KG-only grant must NOT populate diagrams_access_list, got: {:?}",
            dg_list,
        );
    }

    #[test]
    fn sanitize_diagrams_class_prefix_matches_python_canonical_rule() {
        // v0.2.34 cr-b2 (2026-05-25): rule is now Python's canonical
        // `vco_lib.project_init.sanitize_for_weaviate_class` (split on
        // any non-alphanumeric run, PascalCase each part, concatenate).
        // Replaces the pre-cr-b2 underscore-replace algorithm that
        // diverged from the indexer's writer-side naming for any
        // non-alphanumeric input. Cross-language parity for the wider
        // input set is pinned by `diagrams_class_name_parity.rs`
        // (integration test) against the shared JSON fixture.

        // All-alphanumeric inputs (round-trip unchanged — these passed
        // pre-cr-b2 too, but are pinned here as smoke).
        assert_eq!(sanitize_diagrams_class_prefix("Foo"), "Foo");
        assert_eq!(sanitize_diagrams_class_prefix("foo"), "Foo");
        assert_eq!(sanitize_diagrams_class_prefix("VCODev"), "VCODev");
        assert_eq!(sanitize_diagrams_class_prefix("SD15"), "SD15");

        // Non-alphanumeric inputs (THE bug being fixed — these are the
        // cases pre-cr-b2 returned divergent results for, silently
        // breaking cross-project diagrams visibility).
        assert_eq!(sanitize_diagrams_class_prefix("Foo Bar"), "FooBar");
        assert_eq!(sanitize_diagrams_class_prefix("foo-bar"), "FooBar");
        assert_eq!(sanitize_diagrams_class_prefix("My_Project"), "MyProject");
        assert_eq!(sanitize_diagrams_class_prefix("my-project_v2"), "MyProjectV2");
        assert_eq!(sanitize_diagrams_class_prefix("Foo.Bar"), "FooBar");
        assert_eq!(sanitize_diagrams_class_prefix("Foo--Bar"), "FooBar");
        assert_eq!(sanitize_diagrams_class_prefix("  spaced  out  "), "SpacedOut");
        assert_eq!(
            sanitize_diagrams_class_prefix("Foo Bar 2026-05-25"),
            "FooBar20260525"
        );

        // Empty / all-symbol / leading-digit → fallback "vct" (Weaviate
        // uppercases first char on POST regardless).
        assert_eq!(sanitize_diagrams_class_prefix(""), "vct");
        assert_eq!(sanitize_diagrams_class_prefix("---"), "vct");
        assert_eq!(sanitize_diagrams_class_prefix("!!!"), "vct");
        assert_eq!(sanitize_diagrams_class_prefix("..."), "vct");
        assert_eq!(sanitize_diagrams_class_prefix("12_project"), "vct");
        assert_eq!(sanitize_diagrams_class_prefix("123abc"), "vct");

        // Unicode: non-ASCII chars are treated as separators (matches
        // Python's `[^A-Za-z0-9]+` behaviour). `étude` → `["tude"]` →
        // `"Tude"` (the `é` is stripped). Documented as expected
        // behaviour in both the Python canonical and this port.
        assert_eq!(sanitize_diagrams_class_prefix("étude"), "Tude");
        assert_eq!(sanitize_diagrams_class_prefix("α-beta"), "Beta");

        // Inputs with only leading/trailing non-alnum still have valid
        // surviving parts — `_only_` → `["only"]` → `"Only"` (NOT
        // fallback — there's a valid PascalCase result).
        assert_eq!(sanitize_diagrams_class_prefix("_only_"), "Only");

        // Idempotency: sanitiser output must be a fixed point.
        for input in &["FooBar", "VCODev", "SD15", "MyProjectV2"] {
            let once = sanitize_diagrams_class_prefix(input);
            let twice = sanitize_diagrams_class_prefix(&once);
            assert_eq!(once, twice, "Not idempotent for {:?}", input);
        }
    }

    /// Cross-language parity test: load the shared JSON fixture (also
    /// consumed by ``tests/test_diagrams_class_name_parity.py`` on the
    /// Python side) and assert that the Rust sanitiser produces the
    /// EXACT same output for every fixture row.
    ///
    /// Mechanism choice: in-process fixture-driven assertion. Cheaper
    /// than the alternative (spinning up a cargo run binary or a full
    /// end-to-end seed-project-and-read-back-from-DB test), and the
    /// pure-function nature of ``sanitize_diagrams_class_prefix`` means
    /// we don't gain anything from going through the DB layer for this
    /// particular parity check (the access-list code path already has
    /// its own integration test that hits the DB).
    ///
    /// Fixture path resolution: ``CARGO_MANIFEST_DIR`` at test time is
    /// ``<repo>/launcher/src-tauri/vct-hub/``. The fixture lives at
    /// ``<repo>/tests/fixtures/diagrams_class_name_parity.json`` —
    /// three ``parent()`` calls to climb out of ``vct-hub/src-tauri/launcher/``.
    #[test]
    fn diagrams_class_name_parity_with_python_fixture() {
        use std::path::PathBuf;

        #[derive(serde::Deserialize)]
        struct Fixture {
            #[serde(rename = "_format_version", default)]
            format_version: u32,
            cases: Vec<(String, String)>,
            fallback_cases: Vec<(String, String)>,
            unicode_cases: Vec<(String, String)>,
        }

        let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        // <vct-hub> -> <src-tauri> -> <launcher> -> <repo>
        let repo_root = manifest_dir
            .parent()
            .and_then(|p| p.parent())
            .and_then(|p| p.parent())
            .expect("CARGO_MANIFEST_DIR doesn't have three parents — unexpected build layout");

        let fixture_path = repo_root
            .join("tests")
            .join("fixtures")
            .join("diagrams_class_name_parity.json");
        assert!(
            fixture_path.exists(),
            "Parity fixture missing: {} — this file is shared with \
             tests/test_diagrams_class_name_parity.py",
            fixture_path.display(),
        );

        let raw = std::fs::read_to_string(&fixture_path)
            .unwrap_or_else(|e| panic!("read fixture {}: {}", fixture_path.display(), e));
        let fix: Fixture = serde_json::from_str(&raw)
            .unwrap_or_else(|e| panic!("parse fixture {}: {}", fixture_path.display(), e));

        assert_eq!(
            fix.format_version, 1,
            "Fixture _format_version != 1 — Python parity test may not \
             know how to parse this version; coordinate the bump across \
             both sides",
        );

        let mut failures: Vec<String> = Vec::new();
        let all = fix
            .cases
            .iter()
            .chain(fix.fallback_cases.iter())
            .chain(fix.unicode_cases.iter());
        for (input, expected) in all {
            let actual = sanitize_diagrams_class_prefix(input);
            if actual != *expected {
                failures.push(format!(
                    "  sanitize_diagrams_class_prefix({:?}) = {:?}, fixture says {:?}",
                    input, actual, expected,
                ));
            }
        }

        assert!(
            failures.is_empty(),
            "Rust diagrams sanitiser diverges from fixture in {} case(s):\n{}\n\
             If this divergence is intentional, update the fixture, the \
             Python canonical (vco_lib.project_init.sanitize_for_weaviate_class), \
             AND the Python MCP fallback (claude_mcp_servers/weaviate_mcp/server.py::\
             _sanitize_collection_prefix) in the same commit.",
            failures.len(),
            failures.join("\n"),
        );
    }

    /// End-to-end DB seeding check: a project whose display name
    /// contains non-alphanumeric chars must produce the canonical
    /// ``<Pascal>_Diagrams`` class name when looked up through the
    /// real hub resolver (``GET /api/v1/projects/{id}/config``).
    ///
    /// Pre-cr-b2 this test would have caught the bug: seeding "Foo Bar"
    /// would have produced the divergent ``Foo_Bar_Diagrams`` rather
    /// than the canonical ``FooBar_Diagrams``.
    #[tokio::test]
    async fn config_diagrams_access_list_handles_non_alnum_grantor_name() {
        let (base, h) = spawn_config_api_hub().await;
        // Grantor display name has a SPACE — the canary for the cr-b2 bug.
        seed_project_with_distinct_name(&h, "p-spaced", "Foo Bar", "p-spaced");
        seed_project_with_distinct_name(&h, "p-grantee", "Grantee", "p-grantee");
        h.0.set_diagram_access("p-spaced", "p-grantee", "read")
            .unwrap();

        let resp = reqwest::get(format!("{}/projects/p-grantee/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        let dg_list: Vec<String> = body
            .get("diagrams_access_list")
            .and_then(|v| v.as_array())
            .expect("diagrams_access_list present")
            .iter()
            .filter_map(|v| v.as_str().map(String::from))
            .collect();

        // Post-cr-b2: must be the canonical PascalCase-concat form.
        // Pre-cr-b2 would have produced "Foo_Bar_Diagrams" (underscore).
        assert!(
            dg_list.contains(&"FooBar_Diagrams".to_string()),
            "expected canonical FooBar_Diagrams (cr-b2 canonical), got: {:?}. \
             If this fails with 'Foo_Bar_Diagrams', sanitize_diagrams_class_prefix \
             reverted to the pre-cr-b2 underscore-replace algorithm.",
            dg_list,
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
