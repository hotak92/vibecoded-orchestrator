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

/// v0.2.49 Stream B — serde `default = ...` helper for additive boolean
/// fields whose safe missing-row default is `true` (e.g. the per-
/// project module enable toggle). Mirrors `default_schema_version`'s
/// forward-compat rationale: the response struct is Serialize-only in
/// production, but this helper keeps the Deserialize-friendly fallback
/// available for tests / cross-launcher integration code.
#[allow(dead_code)]
fn default_true_bool() -> bool {
    true
}

// ─── Router ──────────────────────────────────────────────────────

pub fn router() -> Router<LauncherDbHandle> {
    Router::new().route("/projects/{project_id}/config", get(project_config))
}

// ─── Error envelope (shared shape with modules_api) ──────────────

// v0.2.54 Track J: error_response moved to the shared
// `crate::http_error` module (was four byte-identical copies).
use crate::http_error::error_response;

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
    /// v0.2.46 Decision B — per-project READ gate for the shared KG.
    /// Symmetric mirror of ``shared_kg_write_disabled`` above. When
    /// ``true``, the MCP's ``_kg_collections_to_search`` drops
    /// ``SHARED_KG_COLLECTION`` from the hybrid_search /
    /// semantic_graph_search fan-out so this project stops searching
    /// the shared corpus. Read was unconditional pre-v0.2.46
    /// (asymmetric-by-default); v0.2.46 lets users opt OUT explicitly
    /// while keeping default ON.
    ///
    /// Additive field — pre-v0.2.46 Python clients see an unknown
    /// field and ignore it; the parser back-fills with ``false`` for
    /// pre-v0.2.46 hubs paired with v0.2.46+ clients. ``schema_version``
    /// stays at 1 because the field is defaultable client-side.
    shared_kg_read_disabled: bool,
    /// v0.2.40 R2 — RL Reranker per-project flags exposed for the
    /// in-container reader. Until v0.2.40 these three booleans were
    /// SETTER-only: the GUI checkboxes wrote them into
    /// ``module_settings`` (module_id = ``"vct-rl-reranker"``) but the
    /// RL container had no readback path, so flipping them produced no
    /// runtime effect. Exposing them through the resolver closes the
    /// loop — the container fetches the project's config on refresh
    /// and respects these three values. Per multi-Opus pre-push review
    /// (item 3, ``00-synthesis.md``).
    ///
    /// Semantics (mirror the docstrings on the setter commands in
    /// ``launcher/src-tauri/src/commands/rl_settings.rs``):
    ///
    /// * ``rl_use_global`` — "read-only global mode". When ``true``,
    ///   online training events from this project DO NOT update its
    ///   local model.
    /// * ``rl_online_training_disabled`` — freezes the local model AND
    ///   marks new events as log-only. Independent of
    ///   ``rl_use_global``.
    /// * ``rl_global_training_source_flag`` — opts this project's data
    ///   INTO the global model's retraining corpus.
    ///
    /// Default (missing row): ``false`` for all three. Matches the
    /// canonical default in :func:`get_bool_flag` (``rl_settings.rs``
    /// line 101: ``unwrap_or(false)``) — the GUI ships its checkboxes
    /// pre-unchecked, so an absent row must read back the same way the
    /// setters would write it on a fresh first-toggle.
    ///
    /// Additive field — pre-v0.2.40 Python clients see unknown fields
    /// and ignore them (the parser back-fills with ``false`` for
    /// pre-v0.2.40 hubs paired with v0.2.40+ clients). ``schema_version``
    /// stays at 1 because the field is defaultable client-side.
    rl_use_global: bool,
    rl_online_training_disabled: bool,
    rl_global_training_source_flag: bool,
    /// v0.2.49 Stream B — per-project enable toggle for the RL Reranker.
    /// The RL Reranker is a global-scope module (one install on the
    /// host, visible across every project); this flag is the per-project
    /// gate that decides whether the MCP client should issue rerank
    /// requests. Source: `module_settings(project_id, "vct-rl-reranker",
    /// "enabled_for_project")`. Default `true` when no row exists
    /// (fail-open: a corrupted setting never silently disables the
    /// reranker).
    ///
    /// Consumer: `claude_mcp_servers/weaviate_mcp/server.py::
    /// _rl_cache_and_rerank` reads this through
    /// `ProjectConfig.rl_reranker_enabled_for_project` and short-circuits
    /// the rerank path when `false` — the search returns base cosine
    /// order instead. The server-side telemetry path
    /// (`/data/logs/rl_events_<slug>.jsonl`) is untouched: that file
    /// is written by the RL container itself, not by the MCP, so
    /// disabling the client gate cannot drop training events.
    ///
    /// Additive field — pre-v0.2.49 Python clients see an unknown field
    /// and ignore it; the parser back-fills with `true` (the safe
    /// default) for pre-v0.2.49 hubs paired with v0.2.49+ clients.
    /// `schema_version` stays at 1 because the field is defaultable
    /// client-side.
    #[serde(default = "default_true_bool")]
    rl_reranker_enabled_for_project: bool,
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
    /// v0.2.47 — extra read-only paths that contribute to this project's
    /// codegraph collection. Sourced from
    /// `project_codegraph_extra_paths` (only `enabled=1` rows). Hooks
    /// (`.claude/hooks/code-graph-incremental.sh`) query this field via
    /// the resolver clients (bash/ps1/python) to decide whether an edit
    /// under an out-of-project path should re-trigger analyze for THIS
    /// project — so they don't talk to SQLite directly. The launcher GUI
    /// Identity tab "Extra codegraph paths" panel reads + mutates the
    /// underlying rows via the Tauri commands in
    /// `commands::project_codegraph_extras`.
    ///
    /// Additive field — pre-v0.2.47 Python/bash/ps1 clients see an
    /// unknown field and ignore it (the parser back-fills with an
    /// empty vector when missing, mirroring the established `diagrams_collection`
    /// / `shared_kg_read_disabled` empty-default-on-missing pattern).
    /// `schema_version` stays at 1 because the field is defaultable
    /// client-side.
    #[serde(default)]
    code_graph_extra_paths: Vec<CodeGraphExtraPath>,
    /// V52-AA (v0.2.52) — per-project RL Reranker container port.
    ///
    /// Sourced from `module_ports(project_id, "vct-rl-reranker", port)`
    /// (canonical SoT since migration 017 / v0.2.26). Returned as
    /// `Some(port)` when the supervisor has allocated a port for this
    /// project, `None` when no row exists (RL not installed for this
    /// project, OR allocator hasn't run yet).
    ///
    /// Closes the V52-AA env-propagation gap: pre-V52-AA the
    /// `RL_SERVER_PORT` env var was the ONLY channel for the MCP
    /// subprocess to learn the container port. The launcher writes the
    /// allocated port to `module_ports` but never propagates it to
    /// `.claude/settings.json env` or `.claude/env` (intentional — the
    /// value varies per-project and the global allowlist would force
    /// the wrong precedence per the H.1 design contract above). The
    /// canonical channel for per-project values is the hub-resolved
    /// `ProjectConfig`, so the MCP's `_get_rl_client` now falls back
    /// here when env is unset.
    ///
    /// Consumer:
    /// `claude_mcp_servers/weaviate_mcp/server.py::_get_rl_client` reads
    /// this field via `ProjectConfig.rl_server_port` and, when set,
    /// constructs the `RLClient` with `base_url=http://127.0.0.1:<port>`
    /// — overriding the env-only `_resolve_base_url()` default. Env vars
    /// (`RL_SERVER_URL` / `RL_SERVER_PORT`) still take precedence when
    /// set, preserving the existing override path for tests + dev users.
    ///
    /// Additive field — pre-V52-AA Python clients see an unknown field
    /// and ignore it; the parser back-fills with `None` for pre-V52-AA
    /// hubs paired with V52-AA+ clients. `schema_version` stays at 1
    /// because the field is defaultable client-side.
    #[serde(default)]
    rl_server_port: Option<u16>,
}

/// One enabled extra codegraph path for the resolver response.
///
/// Mirrors the relevant subset of `vct_launcher_core::db::codegraph_extras::CodegraphExtraPathRow`:
/// `path` for prefix matching, `enabled` (always `true` in resolver responses
/// — disabled rows are filtered out before serialisation), and
/// `last_indexed_commit` for clients that want to drive incremental
/// analyzer invocations with `--since-commit`. Other columns
/// (`project_id`, `label`, `added_at`, `last_indexed_at`) are launcher-GUI
/// concerns, not resolver concerns — kept out of the wire envelope to
/// limit the per-request payload size.
#[derive(Debug, Serialize)]
struct CodeGraphExtraPath {
    /// Absolute, canonicalised, cross-platform forward-slash form.
    /// Hooks substring-match the edited-file path against this prefix.
    path: String,
    /// Always `true` in resolver responses (disabled rows are filtered
    /// before populating the field). Kept on the wire so clients have a
    /// single shape regardless of source — and so a future read-side
    /// filter relaxation (e.g. "show disabled in the GUI fallback list")
    /// is a one-line change in the populator without a schema bump.
    enabled: bool,
    /// Git SHA at the most recent analyze, when known. `None` for
    /// non-git paths or paths that have not yet been analyzed. Resolver
    /// clients pass this through to `code-graph-analyze --since-commit
    /// <sha>` for incremental runs.
    last_indexed_commit: Option<String>,
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

    // 7a. shared_kg_read_disabled (v0.2.46 Decision B — module_settings
    // → orchestrator-core). Symmetric mirror of the write gate above.
    // Default false (reads allowed). Pre-v0.2.46 the read path was
    // unconditional, so no historical rows exist under any prior key —
    // no migration helper needed.
    let shared_kg_read_disabled = h
        .0
        .get_setting(&project.id, "orchestrator-core", "shared_kg_read_disabled")
        .ok()
        .flatten()
        .and_then(|v| v.as_bool())
        .unwrap_or(false);

    // 7b. RL Reranker per-project flags (v0.2.40 R2 — module_settings →
    // vct-rl-reranker). Same shape as the orchestrator-core read above;
    // module_id is "vct-rl-reranker" — the canonical id used by the
    // setter commands in launcher/src-tauri/src/commands/rl_settings.rs.
    // Default false on missing rows: matches the GUI's pre-unchecked
    // checkboxes and the setter helper's `unwrap_or(false)` contract.
    //
    // We deliberately read all three flags from the same module_id
    // (``vct-rl-reranker``) rather than from ``orchestrator-core`` —
    // they're RL-module-specific. Even though `vct-rl-reranker` is a
    // paid module that may not be installed on every project, reading
    // the absent rows simply returns the `false` default, which is the
    // correct semantic: "RL is not in special-mode for this project".
    let rl_use_global = h
        .0
        .get_setting(&project.id, "vct-rl-reranker", "rl_use_global")
        .ok()
        .flatten()
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let rl_online_training_disabled = h
        .0
        .get_setting(&project.id, "vct-rl-reranker", "rl_online_training_disabled")
        .ok()
        .flatten()
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let rl_global_training_source_flag = h
        .0
        .get_setting(&project.id, "vct-rl-reranker", "rl_global_training_source_flag")
        .ok()
        .flatten()
        .and_then(|v| v.as_bool())
        .unwrap_or(false);

    // v0.2.49 Stream B + v0.2.52 V52-AD — effective enable flag for the
    // RL Reranker (a global-scope module: one install on the host,
    // visible across every project). Cascade order:
    //
    //   1. Per-project row in `module_settings` for `(project_id,
    //      vct-rl-reranker, enabled_for_project)` — explicit override.
    //   2. Global row (`project_id IS NULL`) for `(vct-rl-reranker,
    //      enabled_for_project)` — host-wide default landed by V52-AD.
    //      install.py seeds this row to `false` on fresh installs so
    //      RL reranking is off until enough training data accumulates.
    //   3. System default `true` (fail-open).
    //
    // The MCP's `_rl_cache_and_rerank` gate consumes this field via
    // `ProjectConfig.rl_reranker_enabled_for_project` to decide whether
    // to call the rerank endpoint. When `false`, the MCP falls back to
    // base cosine ordering — no error, no missing-event log entry on
    // the server side (training logs are SERVER-driven by the RL
    // container's own JSONL writer; the client gate only suppresses
    // outbound requests).
    let rl_reranker_enabled_for_project = h
        .0
        .module_effective_enabled(&project.id, "vct-rl-reranker")
        .unwrap_or(true);

    // V52-AA (v0.2.52) — RL Reranker container port.
    // Reads `module_ports(project_id, "vct-rl-reranker", port)`, the
    // canonical SoT since migration 017 / v0.2.26. Returns `None` when
    // no row exists; the consumer (`_get_rl_client` in the MCP) handles
    // None by falling through to env-var resolution / disabled mode.
    // Soft-fail: on a DB error we log + return None rather than 500ing
    // the whole resolve — RL is a value-add, not a critical path.
    //
    // v0.2.61 (Option H re-audit B2-2): GLOBAL-scope honoring. After a module
    // migrates to global scope, ONE container serves all projects on
    // GLOBAL_RL_PORT (11443) and the per-project `module_ports` rows are gone
    // — so `get_project_rl_port` returns None for a global install. This is
    // the PRIMARY rerank channel when the MCP env doesn't pin RL_SERVER_PORT
    // (the default — mcp_registration.rs does not pin it), so a None here put
    // the MCP into disabled mode → rerank silently no-op'd for the exact
    // global deployment Option-H targets. Resolve the global port when a
    // global install row exists. (GLOBAL_RL_PORT mirrors the const in
    // module_supervisor.rs + launcher module_service.rs — kept in sync; the
    // hub can't import the launcher crate.)
    const GLOBAL_RL_PORT: u16 = 11443;
    let rl_server_port = if matches!(
        h.0.get_global_module_install("vct-rl-reranker"),
        Ok(Some(_))
    ) {
        Some(GLOBAL_RL_PORT)
    } else {
        match h.0.get_project_rl_port(&project.id) {
            Ok(p) => p,
            Err(e) => {
                eprintln!(
                    "[vct-hub] config_api: get_project_rl_port({}) failed: {}; returning None",
                    project.id, e
                );
                None
            }
        }
    };

    // 8. Resolve binding roles.
    let primary_kg = kg_bindings
        .iter()
        .find(|b| b.role == "primary")
        .map(|b| b.collection_name.clone());
    let shared_kg_collection_raw = kg_bindings
        .iter()
        .find(|b| b.role == "shared")
        .map(|b| b.collection_name.clone())
        .unwrap_or_default();
    // v0.2.46 Decision C — `development_collection` derives from the
    // primary KG collection via suffix-swap `_KnowledgeGraph` →
    // `_Development`, NOT from a `role='archive'` binding row.
    //
    // Background: pre-v0.2.46 the hub looked for `role='archive'` here,
    // but no installer / launcher / migration ever wrote such a row in
    // the current schema. The value was always empty in hub responses.
    // Meanwhile the launcher's own `populate()` in
    // `project_env_settings.rs` derives the dev name from the project
    // name via the same suffix-swap rule. The two consumers drifted.
    //
    // This change unifies on the suffix-swap rule (mirroring the
    // diagrams derivation 30 lines below). The actual derivation is
    // computed AFTER `kg_collection` is unwrapped from the Option, so
    // we have the post-binding-resolution name to swap on.
    //
    // The empty-string-on-missing fallback for non-canonical primary
    // names (rename case) uses `sanitize_collection_prefix(project.slug)`,
    // identical to the diagrams pattern at line ~524.

    // service_misconfigured gate: every registered project should
    // have a primary KG binding after the launcher's startup
    // backfill (parent plan §"Acceptance criterion" / step 19).
    // If we land here without one, the backfill hasn't run OR
    // failed silently — surface it loudly so resolver clients can
    // route to the warning path.
    let kg_collection_raw = match primary_kg {
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

    // NEW-2 (v0.2.53) — case-rebind ported from install.py's
    // `_resolve_existing_casing` (install.py:11848). When a
    // case-different sibling of a derived class name exists on disk in
    // Weaviate, adopt the on-disk casing rather than the canonical
    // casing from the launcher.db binding row. Without this, the hub
    // returns e.g. `VibeCodedOrchestrator_Development` (capital-C
    // canonical) while Weaviate has `Vibecodedorchestrator_Development`
    // (lowercase legacy), and `sync_knowledge_graph.py`'s
    // case-sensitive `.exists()` check fails → `.create()` is called →
    // Weaviate refuses with "class already exists, found similar class".
    //
    // We resolve `weaviate_url` here (earlier than the rest of the
    // service-URL block below) so the probe has a URL to call. The
    // probe is fail-open: if Weaviate is unreachable / responds with
    // garbage, the candidate name is echoed back unchanged.
    //
    // Reference: `.claude/context/audits/fabio-v0252-rootcause-2026-06-10.md`
    // Symptom B for the full root-cause walk.
    let local_cfg_for_probe = LocalConfig::load();
    let probe_weaviate_url = local_cfg_for_probe.weaviate_url.clone();

    let kg_collection = crate::weaviate_schema_probe::resolve_existing_casing_for_class(
        &probe_weaviate_url,
        &kg_collection_raw,
    )
    .await;

    let shared_kg_collection = if shared_kg_collection_raw.is_empty() {
        String::new()
    } else {
        crate::weaviate_schema_probe::resolve_existing_casing_for_class(
            &probe_weaviate_url,
            &shared_kg_collection_raw,
        )
        .await
    };

    // v0.2.46 Decision C — development collection name derives from the
    // primary KG via the canonical suffix swap (mirrors the diagrams
    // rule immediately below). When the primary's name doesn't end with
    // `_KnowledgeGraph` (e.g. a custom-rename install), fall back to a
    // slug-sanitized derivation.
    //
    // NEW-2 (v0.2.53) — the suffix-swap candidate is then rebound to
    // on-disk casing via `resolve_existing_casing_for_class`. The
    // rebind is the load-bearing fix for Fabio's Symptom B.
    let development_candidate = if kg_collection.ends_with("_KnowledgeGraph") {
        let basename = &kg_collection[..kg_collection.len() - "_KnowledgeGraph".len()];
        format!("{}_Development", basename)
    } else {
        format!("{}_Development", sanitize_collection_prefix(&project.slug))
    };
    let development_collection = crate::weaviate_schema_probe::resolve_existing_casing_for_class(
        &probe_weaviate_url,
        &development_candidate,
    )
    .await;

    // Diagrams collection name — derived from `kg_collection` once it's
    // unwrapped from the Option above. Suffix swap mirrors the Python
    // contract; the slug-sanitized fallback handles the non-canonical
    // rename case (primary binding doesn't end with `_KnowledgeGraph`).
    //
    // NEW-2 (v0.2.53) — same case-rebind treatment as development above.
    let diagrams_candidate = if kg_collection.ends_with("_KnowledgeGraph") {
        let basename = &kg_collection[..kg_collection.len() - "_KnowledgeGraph".len()];
        format!("{}_Diagrams", basename)
    } else {
        format!("{}_Diagrams", sanitize_collection_prefix(&project.slug))
    };
    let diagrams_collection = crate::weaviate_schema_probe::resolve_existing_casing_for_class(
        &probe_weaviate_url,
        &diagrams_candidate,
    )
    .await;

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
    //
    // NEW-2 (v0.2.53) — `local_cfg_for_probe` was already loaded above
    // for the case-rebind probe; reuse it to avoid a second TOML read.
    let weaviate_url = local_cfg_for_probe.weaviate_url;
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

    // 12. Project-extra codegraph paths (v0.2.47). Read enabled rows
    // only — disabled rows are kept in the DB for history + label
    // preservation but the resolver hides them so hooks / Python
    // clients don't try to match against paths the user has paused.
    // Service degradation: if the DB read fails (unlikely — same
    // connection that produced all 11 prior reads), log + treat the
    // field as empty rather than failing the resolver, since the
    // extras are an enrichment of the core response. The fail mode
    // we want to avoid is "one bug in a v0.2.47 path breaks every
    // resolver call".
    let code_graph_extra_paths: Vec<CodeGraphExtraPath> =
        match h.0.list_enabled_codegraph_extras(&project.id) {
            Ok(rows) => rows
                .into_iter()
                .map(|r| CodeGraphExtraPath {
                    path: r.path,
                    enabled: r.enabled,
                    last_indexed_commit: r.last_indexed_commit,
                })
                .collect(),
            Err(e) => {
                eprintln!(
                    "[vct-hub config_api] list_enabled_codegraph_extras failed for project {}: {} (returning empty)",
                    project.id, e
                );
                Vec::new()
            }
        };

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
        shared_kg_read_disabled,
        rl_use_global,
        rl_online_training_disabled,
        rl_global_training_source_flag,
        rl_reranker_enabled_for_project,
        claude_session_dir,
        retrieval_tuning,
        code_graph_extra_paths,
        rl_server_port,
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
/// its on-disk schema convention does (``Camel_Case_CodeFunction``),
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
///   * ``_`` → ``-``  (e.g. ``project_a`` → ``project-a``)
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
        // Underscored workspace paths (project_a, project_b) were the
        // root cause of the 97.7% orphan-citation rate.
        let p = StdPath::new("/home/user/code/project_a");
        let dir = claude_session_dir_for(p);
        assert_eq!(
            dir.file_name().unwrap().to_str().unwrap(),
            "-home-user-code-project-a",
            "slug must replace both '/' and '_' with '-'",
        );

        let p2 = StdPath::new("/home/user/code/project_b");
        let dir2 = claude_session_dir_for(p2);
        assert_eq!(
            dir2.file_name().unwrap().to_str().unwrap(),
            "-home-user-code-project-b",
        );
    }

    #[test]
    fn claude_session_dir_passthrough_without_underscores() {
        // Workspaces without underscores already worked in the pre-fix
        // implementation. Pin the non-regression to ensure the new
        // helper's behaviour matches the old inline string-replace for
        // the cases that were never broken.
        let p = StdPath::new("/home/user/code/vibecoded-orchestrator");
        let dir = claude_session_dir_for(p);
        assert_eq!(
            dir.file_name().unwrap().to_str().unwrap(),
            "-home-user-code-vibecoded-orchestrator",
        );
    }

    #[test]
    fn claude_session_dir_handles_dots() {
        // Verified against `~/.claude/projects/` on Linux: worktree
        // paths under `.claude/` are stored with `.` → `-` substitution
        // (e.g. `/home/u/project_a/.claude/worktrees/foo` becomes
        // `-home-u-project-a--claude-worktrees-foo`). The double-dash is
        // a natural consequence of the rule, not a separate special-case.
        let p = StdPath::new("/home/u/project_a/.claude/worktrees/foo");
        let dir = claude_session_dir_for(p);
        assert_eq!(
            dir.file_name().unwrap().to_str().unwrap(),
            "-home-u-project-a--claude-worktrees-foo",
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
        // v0.2.46 Decision B — symmetric READ gate. Default false on
        // a freshly-seeded project (no module_settings rows).
        assert_eq!(
            body.get("shared_kg_read_disabled").and_then(|v| v.as_bool()),
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

    /// v0.2.46 Decision C — `development_collection` derives from the
    /// primary KG via suffix-swap `_KnowledgeGraph` → `_Development`,
    /// NOT from a `role='archive'` binding row.
    ///
    /// Reason: pre-v0.2.46 the hub looked for `role='archive'` to fill
    /// this field, but no installer / launcher / migration ever wrote
    /// such a row in the current schema — the value was always empty in
    /// hub responses. The launcher's own `populate()` (in
    /// `project_env_settings.rs`) already derives the dev name from the
    /// project name; the hub was inconsistent. v0.2.46 unifies on the
    /// suffix-swap rule so hub and launcher agree.
    ///
    /// This test seeds a project with ONLY primary + shared (no archive
    /// row) and asserts `development_collection` is still populated
    /// from the primary's basename. The existing
    /// `config_happy_path_returns_full_envelope` test still passes
    /// because the fixture seeds primary too — the archive row it also
    /// seeds becomes irrelevant.
    #[tokio::test]
    async fn config_development_collection_derives_from_primary_kg() {
        let (base, h) = spawn_config_api_hub().await;
        // Seed primary + shared but NOT archive.
        let project_id = "p-no-archive";
        let slug = "myproject";
        let folder = format!("/tmp/test-config-project-{}", project_id);
        seed_project(&h.0, project_id, "Test Display Name", &folder, slug);
        h.0
            .set_project_kg_binding(
                project_id,
                "primary",
                "MyCustomKG_KnowledgeGraph",
                Some("qwen3-embedding:0.6b"),
                Some(1024),
                None,
                None,
                &empty_json_obj(),
            )
            .unwrap();
        h.0
            .set_project_kg_binding(
                project_id,
                "shared",
                "VibeCodedOrchestrator_KnowledgeGraph",
                Some("qwen3-embedding:0.6b"),
                Some(1024),
                None,
                None,
                &empty_json_obj(),
            )
            .unwrap();
        // Codegraph binding required (avoids the test hitting an
        // unrelated 503 from missing codegraph data).
        h.0
            .set_project_codegraph_binding(
                project_id,
                "MyCustomKG",
                Some("codesage-large-v2"),
                Some(2048),
                None,
                None,
                true,
                &empty_json_obj(),
            )
            .unwrap();

        let resp = reqwest::get(format!("{}/projects/{}/config", base, project_id))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        // development_collection MUST be derived from primary via
        // suffix-swap, NOT from a (non-existent) archive row.
        assert_eq!(
            body.get("development_collection").and_then(|v| v.as_str()),
            Some("MyCustomKG_Development"),
            "development_collection must derive from primary KG by \
             suffix-swap _KnowledgeGraph → _Development (Decision C)"
        );
    }

    /// v0.2.46 Decision C edge case — when primary's name does NOT end
    /// with `_KnowledgeGraph` (e.g. an old custom rename), the dev name
    /// falls back to `<sanitized_slug>_Development`, mirroring the
    /// diagrams derivation at line 524.
    #[tokio::test]
    async fn config_development_collection_falls_back_to_slug_for_non_canonical_primary() {
        let (base, h) = spawn_config_api_hub().await;
        let project_id = "p-non-canonical";
        let slug = "weirdproject";
        let folder = format!("/tmp/test-config-project-{}", project_id);
        seed_project(&h.0, project_id, "Weird Project", &folder, slug);
        // Primary name doesn't end with _KnowledgeGraph — non-canonical.
        h.0
            .set_project_kg_binding(
                project_id,
                "primary",
                "WeirdName_Custom",
                None,
                None,
                None,
                None,
                &empty_json_obj(),
            )
            .unwrap();
        h.0
            .set_project_codegraph_binding(
                project_id,
                "Weirdproject",
                Some("codesage-large-v2"),
                Some(2048),
                None,
                None,
                true,
                &empty_json_obj(),
            )
            .unwrap();

        let resp = reqwest::get(format!("{}/projects/{}/config", base, project_id))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        // Fallback: sanitize_collection_prefix(slug) + "_Development".
        // For slug "weirdproject" → "Weirdproject" (capitalized first letter).
        assert_eq!(
            body.get("development_collection").and_then(|v| v.as_str()),
            Some("Weirdproject_Development"),
            "non-canonical primary name (no _KnowledgeGraph suffix) should \
             fall back to slug-derived dev name, mirroring the diagrams rule"
        );
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
        assert_eq!(sanitize_diagrams_class_prefix("MyProject"), "MyProject");

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
        for input in &["FooBar", "VCODev", "MyProject", "MyProjectV2"] {
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

    /// v0.2.40 R2 — RL flag exposure (defaults branch).
    ///
    /// A project with no rows in ``module_settings`` for the
    /// ``vct-rl-reranker`` module must read back all three flags as
    /// ``false``. This mirrors :func:`get_bool_flag` in
    /// ``launcher/src-tauri/src/commands/rl_settings.rs`` (line 101):
    /// the setter's getter uses ``unwrap_or(false)`` on a missing
    /// row, and the hub's response surface MUST agree with that
    /// contract — otherwise the in-container reader would see a
    /// different default than the launcher's own admin code.
    #[tokio::test]
    async fn config_emits_rl_flag_defaults_when_module_settings_empty() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-rl-defaults", "myproject");

        let resp = reqwest::get(format!("{}/projects/p-rl-defaults/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        // All three flags present and false (no DB rows → unwrap_or(false)).
        assert_eq!(
            body.get("rl_use_global").and_then(|v| v.as_bool()),
            Some(false),
            "rl_use_global default must be false; body={}",
            body,
        );
        assert_eq!(
            body.get("rl_online_training_disabled").and_then(|v| v.as_bool()),
            Some(false),
            "rl_online_training_disabled default must be false; body={}",
            body,
        );
        assert_eq!(
            body.get("rl_global_training_source_flag").and_then(|v| v.as_bool()),
            Some(false),
            "rl_global_training_source_flag default must be false; body={}",
            body,
        );
    }

    /// v0.2.40 R2 — RL flag exposure (set → fetch round-trip).
    ///
    /// Writing each flag via ``set_setting`` under
    /// ``module_id = "vct-rl-reranker"`` (the exact path the GUI
    /// setter commands take — see ``rl_settings.rs::set_bool_flag``)
    /// must surface that value in the resolver response. Each flag
    /// lives at its own row, so independent writes do not collide.
    /// This is the core regression guard: without this assertion, a
    /// rename of the canonical key strings ("rl_use_global" etc.)
    /// would silently break the contract between the launcher's
    /// setters and the hub's reader.
    #[tokio::test]
    async fn config_emits_rl_flags_from_module_settings() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-rl-set", "myproject");

        // Mirror the GUI setter path exactly: module_id is the literal
        // "vct-rl-reranker" string, keys are the canonical names. Two
        // flags true + one false demonstrates per-row independence.
        h.0.set_setting(
            "p-rl-set",
            "vct-rl-reranker",
            "rl_use_global",
            &serde_json::Value::Bool(true),
        )
        .unwrap();
        h.0.set_setting(
            "p-rl-set",
            "vct-rl-reranker",
            "rl_online_training_disabled",
            &serde_json::Value::Bool(false),
        )
        .unwrap();
        h.0.set_setting(
            "p-rl-set",
            "vct-rl-reranker",
            "rl_global_training_source_flag",
            &serde_json::Value::Bool(true),
        )
        .unwrap();

        let resp = reqwest::get(format!("{}/projects/p-rl-set/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        assert_eq!(
            body.get("rl_use_global").and_then(|v| v.as_bool()),
            Some(true),
        );
        assert_eq!(
            body.get("rl_online_training_disabled").and_then(|v| v.as_bool()),
            Some(false),
        );
        assert_eq!(
            body.get("rl_global_training_source_flag").and_then(|v| v.as_bool()),
            Some(true),
        );
    }

    /// v0.2.40 R2 — single-field filter access on the new RL flags.
    /// The ``?key=`` filter operates on top-level fields by serde, so
    /// the additive RL flags fall out of the generic path automatically
    /// — but pin the contract anyway so a future struct refactor that
    /// nests them (don't!) would surface here loudly.
    #[tokio::test]
    async fn config_key_filter_returns_rl_use_global() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-rl-key", "myproject");
        h.0.set_setting(
            "p-rl-key",
            "vct-rl-reranker",
            "rl_use_global",
            &serde_json::Value::Bool(true),
        )
        .unwrap();

        let resp = reqwest::get(format!(
            "{}/projects/p-rl-key/config?key=rl_use_global",
            base
        ))
        .await
        .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        let obj = body.as_object().expect("object");
        assert_eq!(obj.len(), 1);
        assert_eq!(
            obj.get("rl_use_global").and_then(|v| v.as_bool()),
            Some(true)
        );
    }

    /// v0.2.49 Stream B — RL Reranker per-project enable toggle.
    ///
    /// A project with no row in ``module_settings`` for
    /// ``vct-rl-reranker / enabled_for_project`` reads back as `true`.
    /// This is the fail-open default — a freshly registered project
    /// must NOT silently disable a global module it hasn't opted out
    /// of. Mirrors the DB-layer reader's contract.
    #[tokio::test]
    async fn config_emits_rl_reranker_enabled_default_true() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-rl-enable-default", "myproject");

        let resp = reqwest::get(format!(
            "{}/projects/p-rl-enable-default/config",
            base
        ))
        .await
        .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(
            body.get("rl_reranker_enabled_for_project")
                .and_then(|v| v.as_bool()),
            Some(true),
            "default missing-row must read true; body={}",
            body,
        );
    }

    /// v0.2.49 Stream B — write → fetch round-trip for the RL enable
    /// toggle. Setting `false` via the canonical key flips the response;
    /// flipping back to `true` flips it again. Pins the contract between
    /// the Tauri setter (`module_set_enabled_for_project`) and the hub
    /// reader so a key-rename on one side immediately fails here.
    #[tokio::test]
    async fn config_emits_rl_reranker_enabled_from_module_settings() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-rl-enable-set", "myproject");

        // Disable the RL reranker for this project.
        h.0.module_set_enabled_for_project(
            "p-rl-enable-set",
            "vct-rl-reranker",
            false,
        )
        .unwrap();

        let resp = reqwest::get(format!(
            "{}/projects/p-rl-enable-set/config",
            base
        ))
        .await
        .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(
            body.get("rl_reranker_enabled_for_project")
                .and_then(|v| v.as_bool()),
            Some(false),
            "explicit disable must surface; body={}",
            body,
        );

        // Re-enable.
        h.0.module_set_enabled_for_project(
            "p-rl-enable-set",
            "vct-rl-reranker",
            true,
        )
        .unwrap();
        let resp = reqwest::get(format!(
            "{}/projects/p-rl-enable-set/config",
            base
        ))
        .await
        .expect("hub reachable");
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(
            body.get("rl_reranker_enabled_for_project")
                .and_then(|v| v.as_bool()),
            Some(true),
        );
    }

    /// v0.2.52 V52-AD — global-default cascade. When NO per-project row
    /// exists for `(project, vct-rl-reranker, enabled_for_project)`, the
    /// resolver must fall back to the global row (`project_id IS NULL`).
    /// install.py seeds this to `false` on fresh installs so RL
    /// reranking is off by default until training data accumulates.
    #[tokio::test]
    async fn config_falls_back_to_global_rl_reranker_default() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-rl-global", "myproject");

        // Step 1: no per-project row, no global row → fail-open true.
        let resp = reqwest::get(format!("{}/projects/p-rl-global/config", base))
            .await
            .expect("hub reachable");
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(
            body.get("rl_reranker_enabled_for_project")
                .and_then(|v| v.as_bool()),
            Some(true),
            "no rows → default true; body={}",
            body,
        );

        // Step 2: set global=false, no per-project row → false propagates.
        h.0.module_set_global_enabled("vct-rl-reranker", false)
            .unwrap();
        let resp = reqwest::get(format!("{}/projects/p-rl-global/config", base))
            .await
            .expect("hub reachable");
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(
            body.get("rl_reranker_enabled_for_project")
                .and_then(|v| v.as_bool()),
            Some(false),
            "global=false must propagate when no per-project override; body={}",
            body,
        );

        // Step 3: per-project=true overrides global=false.
        h.0.module_set_enabled_for_project(
            "p-rl-global",
            "vct-rl-reranker",
            true,
        )
        .unwrap();
        let resp = reqwest::get(format!("{}/projects/p-rl-global/config", base))
            .await
            .expect("hub reachable");
        let body: serde_json::Value = resp.json().await.expect("json body");
        assert_eq!(
            body.get("rl_reranker_enabled_for_project")
                .and_then(|v| v.as_bool()),
            Some(true),
            "per-project=true must override global=false; body={}",
            body,
        );
    }

    /// v0.2.46 Decision B — symmetric READ gate, default branch.
    ///
    /// A project with no row in ``module_settings`` for
    /// ``orchestrator-core / shared_kg_read_disabled`` must read back
    /// the field as ``false`` (reads allowed). Mirrors the write-gate
    /// default contract exactly.
    #[tokio::test]
    async fn config_emits_shared_kg_read_disabled_default_false() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-read-default", "myproject");

        let resp = reqwest::get(format!("{}/projects/p-read-default/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        assert_eq!(
            body.get("shared_kg_read_disabled").and_then(|v| v.as_bool()),
            Some(false),
            "shared_kg_read_disabled default must be false; body={}",
            body,
        );
    }

    /// v0.2.46 Decision B — symmetric READ gate, set → fetch round-trip.
    ///
    /// Writing the flag via the same DB path the GUI setter would use
    /// (`module_id = "orchestrator-core"`, key `shared_kg_read_disabled`)
    /// must surface that value in the resolver response. Pins the
    /// canonical key strings so a rename surfaces here loudly.
    #[tokio::test]
    async fn config_emits_shared_kg_read_disabled_when_set() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-read-set", "myproject");

        // Mirror the GUI setter path exactly: module_id is the literal
        // "orchestrator-core" string (same scope as the write gate).
        h.0.set_setting(
            "p-read-set",
            "orchestrator-core",
            "shared_kg_read_disabled",
            &serde_json::Value::Bool(true),
        )
        .unwrap();

        let resp = reqwest::get(format!("{}/projects/p-read-set/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        assert_eq!(
            body.get("shared_kg_read_disabled").and_then(|v| v.as_bool()),
            Some(true),
            "shared_kg_read_disabled set→fetch round-trip; body={}",
            body,
        );

        // Write gate stays at default false — confirms the two flags
        // are independent rows, not aliased to each other.
        assert_eq!(
            body.get("shared_kg_write_disabled").and_then(|v| v.as_bool()),
            Some(false),
            "write gate must NOT be flipped by setting the read gate",
        );
    }

    // ─── v0.2.47: code_graph_extra_paths field ──────────────────────────

    /// On a freshly-seeded project with NO extras rows, the resolver
    /// returns the field as an empty JSON array (NOT missing, NOT null).
    /// Hooks + Python clients rely on the field being present so they
    /// can iterate it unconditionally.
    #[tokio::test]
    async fn config_returns_empty_code_graph_extra_paths_by_default() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-no-extras", "myproject");

        let resp = reqwest::get(format!("{}/projects/p-no-extras/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        let arr = body
            .get("code_graph_extra_paths")
            .and_then(|v| v.as_array())
            .expect("code_graph_extra_paths present and is array");
        assert!(arr.is_empty(), "empty array on a project with no extras; got {:?}", arr);
    }

    /// Enabled extras appear in the response in `added_at DESC` order
    /// with `path`, `enabled`, and `last_indexed_commit` fields. The
    /// project_id, label, added_at, last_indexed_at columns are NOT
    /// projected (they are launcher-GUI concerns).
    #[tokio::test]
    async fn config_returns_enabled_code_graph_extra_paths() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-with-extras", "myproject");

        // Seed three extras: newest first when listed.
        h.0
            .add_codegraph_extra("p-with-extras", "/opt/sibling-a", Some("Sibling A"))
            .unwrap();
        h.0
            .add_codegraph_extra("p-with-extras", "/opt/sibling-b", None)
            .unwrap();
        h.0
            .update_codegraph_extra_last_indexed(
                "p-with-extras",
                "/opt/sibling-b",
                1_700_000_000_000,
                Some("cafebabe"),
            )
            .unwrap();

        let resp = reqwest::get(format!("{}/projects/p-with-extras/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        let arr = body
            .get("code_graph_extra_paths")
            .and_then(|v| v.as_array())
            .expect("code_graph_extra_paths present");
        assert_eq!(arr.len(), 2, "two enabled extras seeded; body={}", body);

        // All entries carry the three projected fields.
        for entry in arr {
            assert!(entry.get("path").is_some(), "path field present");
            assert_eq!(
                entry.get("enabled").and_then(|v| v.as_bool()),
                Some(true),
                "resolver-projected rows are always enabled=true",
            );
            // last_indexed_commit is Option<String>: null for un-analyzed,
            // string for analyzed.
            let lic = entry.get("last_indexed_commit");
            assert!(lic.is_some(), "last_indexed_commit key always present");
        }

        // Find the row for sibling-b — should carry the commit SHA we
        // recorded above.
        let b = arr
            .iter()
            .find(|e| e.get("path").and_then(|v| v.as_str()) == Some("/opt/sibling-b"))
            .expect("sibling-b row present");
        assert_eq!(
            b.get("last_indexed_commit").and_then(|v| v.as_str()),
            Some("cafebabe"),
        );

        // sibling-a was never analyzed → null commit.
        let a = arr
            .iter()
            .find(|e| e.get("path").and_then(|v| v.as_str()) == Some("/opt/sibling-a"))
            .expect("sibling-a row present");
        assert!(a.get("last_indexed_commit").unwrap().is_null());

        // Negative: launcher-only columns are NOT projected to the wire.
        for entry in arr {
            assert!(
                entry.get("project_id").is_none(),
                "project_id is a launcher concern, not a resolver-wire field"
            );
            assert!(
                entry.get("label").is_none(),
                "label is a launcher GUI concern, not a resolver-wire field"
            );
            assert!(
                entry.get("added_at").is_none(),
                "added_at is a launcher concern, not a resolver-wire field"
            );
            assert!(
                entry.get("last_indexed_at").is_none(),
                "last_indexed_at is a launcher concern, not a resolver-wire field"
            );
        }
    }

    /// Disabled rows are filtered OUT of the resolver response. They
    /// stay in the DB (for history + label preservation) but the hub
    /// hides them so hooks / Python clients don't try to match against
    /// paths the user has paused.
    #[tokio::test]
    async fn config_filters_disabled_code_graph_extra_paths() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-mixed", "myproject");

        h.0
            .add_codegraph_extra("p-mixed", "/opt/active", None)
            .unwrap();
        h.0
            .add_codegraph_extra("p-mixed", "/opt/paused", None)
            .unwrap();
        h.0
            .set_codegraph_extra_enabled("p-mixed", "/opt/paused", false)
            .unwrap();

        let resp = reqwest::get(format!("{}/projects/p-mixed/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        let arr = body
            .get("code_graph_extra_paths")
            .and_then(|v| v.as_array())
            .expect("code_graph_extra_paths present");
        assert_eq!(arr.len(), 1, "only the enabled row reaches the wire");
        assert_eq!(
            arr[0].get("path").and_then(|v| v.as_str()),
            Some("/opt/active")
        );

        // DB still has both rows — confirms the filter is at projection
        // time, not via auto-deletion.
        let all = h.0.list_codegraph_extras("p-mixed").unwrap();
        assert_eq!(all.len(), 2);
    }

    /// `?key=code_graph_extra_paths` returns just the new field
    /// (single-field projection). Validates the field flows through
    /// `single_field_response` via the serde Value path.
    #[tokio::test]
    async fn config_single_field_key_for_code_graph_extra_paths() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-key", "myproject");
        h.0
            .add_codegraph_extra("p-key", "/opt/x", None)
            .unwrap();

        let resp = reqwest::get(format!(
            "{}/projects/p-key/config?key=code_graph_extra_paths",
            base
        ))
        .await
        .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        // Single-field shape: object with exactly one key.
        let obj = body.as_object().expect("object");
        assert_eq!(obj.len(), 1);
        let arr = obj
            .get("code_graph_extra_paths")
            .and_then(|v| v.as_array())
            .expect("the requested field");
        assert_eq!(arr.len(), 1);
        assert_eq!(
            arr[0].get("path").and_then(|v| v.as_str()),
            Some("/opt/x")
        );
    }

    // ─── V52-AA (v0.2.52): rl_server_port field ──────────────────────

    /// On a freshly-seeded project with no row in ``module_ports`` for
    /// ``vct-rl-reranker``, the resolver returns ``rl_server_port`` as
    /// JSON null. The MCP client treats null/missing as "fall through
    /// to env-resolution / disabled mode" — exactly the pre-V52-AA
    /// behaviour, preserved for non-RL projects.
    #[tokio::test]
    async fn config_emits_rl_server_port_null_when_unallocated() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-no-rl-port", "myproject");

        let resp = reqwest::get(format!("{}/projects/p-no-rl-port/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        // Field MUST be present (serialised as JSON null) so the Python
        // parser's defaultable-on-missing path is exercised consistently.
        // Both shapes (missing key / explicit null) deserialize to None
        // on the Python side, but pinning the wire shape here documents
        // the contract for any future hub-shape audit.
        let v = body
            .get("rl_server_port")
            .expect("rl_server_port field present");
        assert!(
            v.is_null(),
            "rl_server_port must be JSON null when no allocation; got {:?}",
            v
        );
    }

    /// When the supervisor has allocated a port (write via the canonical
    /// ``set_project_rl_port`` helper, mirroring
    /// ``module_supervisor::ensure_rl_port_persisted``), the resolver
    /// surfaces the value as a JSON number. Pinning this contract closes
    /// the V52-AA env-propagation gap: the MCP's ``_get_rl_client``
    /// reads ``ProjectConfig.rl_server_port`` here and builds the client
    /// with ``base_url=http://127.0.0.1:<port>``.
    #[tokio::test]
    async fn config_emits_rl_server_port_when_allocated() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-rl-port-set", "myproject");

        // 11442 mirrors the canonical orchestrator-root allocation
        // documented in migrations/014_project_rl_port.sql. Any non-zero
        // u16 would satisfy the test; using a "real" value keeps the
        // failure mode obvious if someone breaks the field plumbing.
        h.0.set_project_rl_port("p-rl-port-set", 11442).unwrap();

        let resp = reqwest::get(format!("{}/projects/p-rl-port-set/config", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        assert_eq!(
            body.get("rl_server_port").and_then(|v| v.as_u64()),
            Some(11442),
            "rl_server_port must surface the allocated value; body={}",
            body,
        );
    }

    /// Single-field filter (``?key=rl_server_port``) returns just the
    /// new field, mirroring the existing ``rl_use_global`` /
    /// ``rl_reranker_enabled_for_project`` filter pattern. Bash/PS1
    /// resolver clients query single fields to avoid parsing the whole
    /// envelope; without this assertion a serde rename of the field
    /// would break the resolver clients without breaking the
    /// full-envelope tests.
    #[tokio::test]
    async fn config_key_filter_returns_rl_server_port() {
        let (base, h) = spawn_config_api_hub().await;
        seed_full_project(&h, "p-rl-port-key", "myproject");
        h.0.set_project_rl_port("p-rl-port-key", 11443).unwrap();

        let resp = reqwest::get(format!(
            "{}/projects/p-rl-port-key/config?key=rl_server_port",
            base
        ))
        .await
        .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        let obj = body.as_object().expect("object");
        assert_eq!(obj.len(), 1);
        assert_eq!(
            obj.get("rl_server_port").and_then(|v| v.as_u64()),
            Some(11443)
        );
    }

    // ──────────────────────────────────────────────────────────────────
    // NEW-2 (v0.2.53) — case-rebind integration tests.
    //
    // Reference: `.claude/context/audits/fabio-v0252-rootcause-2026-06-10.md`
    // Symptom B.
    //
    // These tests spin up a fake Weaviate alongside the hub and assert
    // the hub's resolver adopts the on-disk casing of case-different
    // sibling classes, mirroring install.py's `_resolve_existing_casing`
    // (install.py:11848).
    //
    // Env-var isolation note: the hub reads its Weaviate URL via
    // `LocalConfig::load()` which honours `VCT_WEAVIATE_URL`. We set
    // this once at the top of each test and the cache-reset path in
    // `weaviate_schema_probe::_reset_cache_for_test` keeps tests
    // independent.
    // ──────────────────────────────────────────────────────────────────

    /// Spin up a fake Weaviate that responds to GET /v1/schema with a
    /// `{"classes":[{"class":<name>}, ...]}` body for the given list.
    /// Returns the bound URL.
    async fn spawn_fake_weaviate(
        classes: Vec<String>,
    ) -> (String, tokio::task::JoinHandle<()>) {
        use axum::{routing::get, Json, Router as InnerRouter};
        let classes_payload: Vec<serde_json::Value> = classes
            .iter()
            .map(|c| serde_json::json!({ "class": c }))
            .collect();
        let body = serde_json::json!({ "classes": classes_payload });
        let app = InnerRouter::new().route(
            "/v1/schema",
            get(move || {
                let body = body.clone();
                async move { Json(body) }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("local_addr");
        let url = format!("http://{}", addr);
        let handle = tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        (url, handle)
    }

    /// NEW-2 — when Weaviate has a case-different sibling of the
    /// suffix-swap-derived development_collection name, the hub returns
    /// the on-disk casing rather than the canonical-capitalisation
    /// candidate. Reproduces Fabio's Symptom B fix.
    #[tokio::test]
    async fn dev_collection_case_rebind_adopts_on_disk_casing() {
        crate::weaviate_schema_probe::_reset_cache_for_test();

        // Fake Weaviate: lowercase-c on-disk class (Fabio's case).
        let (weaviate_url, _w) = spawn_fake_weaviate(vec![
            "Vibecodedorchestrator_KnowledgeGraph".to_string(),
            "Vibecodedorchestrator_Development".to_string(),
            "Vibecodedorchestrator_Diagrams".to_string(),
        ])
        .await;
        // SAFETY: setting env var for the duration of this test is OK
        // because LocalConfig::load() reads it on each call and our
        // probe cache is reset per test. Other tests that depend on
        // LocalConfig defaults are unaffected because (a) this test is
        // additive and (b) the probe is fail-open: even if a stale env
        // value leaked in, the probe would just echo back the candidate.
        std::env::set_var("VCT_WEAVIATE_URL", &weaviate_url);

        let (base, h) = spawn_config_api_hub().await;
        // Seed primary KG with capital-C canonical name (what
        // launcher.db stores after v0.2.23 B1 canonicalisation).
        let project_id = "p-case-rebind";
        let slug = "vibecodedorchestrator";
        let folder = format!("/tmp/test-config-project-{}", project_id);
        seed_project(&h.0, project_id, "VibeCoded Orchestrator", &folder, slug);
        h.0.set_project_kg_binding(
            project_id,
            "primary",
            "VibeCodedOrchestrator_KnowledgeGraph", // capital-C canonical
            Some("qwen3-embedding:0.6b"),
            Some(1024),
            None,
            None,
            &empty_json_obj(),
        )
        .unwrap();
        h.0.set_project_kg_binding(
            project_id,
            "shared",
            "VibeCodedOrchestrator_KnowledgeGraph",
            Some("qwen3-embedding:0.6b"),
            Some(1024),
            None,
            None,
            &empty_json_obj(),
        )
        .unwrap();

        let resp = reqwest::get(format!("{}/projects/{}/config", base, project_id))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        // Assertions: every returned class name follows the on-disk
        // casing (lowercase-c, lowercase-o), NOT the canonical
        // capital-C that the launcher.db binding row holds.
        assert_eq!(
            body.get("kg_collection").and_then(|v| v.as_str()),
            Some("Vibecodedorchestrator_KnowledgeGraph"),
            "kg_collection must adopt on-disk casing (Fabio's Symptom B)"
        );
        assert_eq!(
            body.get("development_collection").and_then(|v| v.as_str()),
            Some("Vibecodedorchestrator_Development"),
            "development_collection must adopt on-disk casing — the load-bearing assertion for Fabio's bug"
        );
        assert_eq!(
            body.get("diagrams_collection").and_then(|v| v.as_str()),
            Some("Vibecodedorchestrator_Diagrams"),
            "diagrams_collection must adopt on-disk casing (parity with development)"
        );
        assert_eq!(
            body.get("shared_kg_collection").and_then(|v| v.as_str()),
            Some("Vibecodedorchestrator_KnowledgeGraph"),
            "shared_kg_collection must adopt on-disk casing"
        );
        // Clean up — best-effort.
        std::env::remove_var("VCT_WEAVIATE_URL");
    }

    /// NEW-2 — when Weaviate has no matching class (fresh install),
    /// the hub returns the canonical-capitalisation candidate as-is.
    /// This is the no-rebind path and must keep working.
    #[tokio::test]
    async fn dev_collection_no_rebind_when_no_sibling() {
        crate::weaviate_schema_probe::_reset_cache_for_test();

        // Fake Weaviate: empty schema (fresh install).
        let (weaviate_url, _w) = spawn_fake_weaviate(vec![]).await;
        std::env::set_var("VCT_WEAVIATE_URL", &weaviate_url);

        let (base, h) = spawn_config_api_hub().await;
        let project_id = "p-no-rebind";
        let slug = "freshproject";
        let folder = format!("/tmp/test-config-project-{}", project_id);
        seed_project(&h.0, project_id, "Fresh Project", &folder, slug);
        h.0.set_project_kg_binding(
            project_id,
            "primary",
            "FreshProject_KnowledgeGraph",
            Some("qwen3-embedding:0.6b"),
            Some(1024),
            None,
            None,
            &empty_json_obj(),
        )
        .unwrap();

        let resp = reqwest::get(format!("{}/projects/{}/config", base, project_id))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        assert_eq!(
            body.get("kg_collection").and_then(|v| v.as_str()),
            Some("FreshProject_KnowledgeGraph"),
            "candidate name echoed unchanged when no sibling exists"
        );
        assert_eq!(
            body.get("development_collection").and_then(|v| v.as_str()),
            Some("FreshProject_Development"),
            "suffix-swap candidate echoed unchanged when no sibling exists"
        );
        std::env::remove_var("VCT_WEAVIATE_URL");
    }

    /// NEW-2 — when Weaviate is unreachable (network failure), the hub
    /// returns the canonical-capitalisation candidates as-is (fail-open).
    /// This must keep working so a transient Weaviate hiccup never
    /// breaks resolver responses.
    #[tokio::test]
    async fn dev_collection_unreachable_weaviate_fails_open() {
        crate::weaviate_schema_probe::_reset_cache_for_test();

        // Bind+drop a TCP listener to claim a port that's now closed.
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        let unreachable_url = format!("http://{}", addr);
        std::env::set_var("VCT_WEAVIATE_URL", &unreachable_url);

        let (base, h) = spawn_config_api_hub().await;
        let project_id = "p-unreach";
        let slug = "unreachable";
        let folder = format!("/tmp/test-config-project-{}", project_id);
        seed_project(&h.0, project_id, "Unreach", &folder, slug);
        h.0.set_project_kg_binding(
            project_id,
            "primary",
            "Unreach_KnowledgeGraph",
            Some("qwen3-embedding:0.6b"),
            Some(1024),
            None,
            None,
            &empty_json_obj(),
        )
        .unwrap();

        let resp = reqwest::get(format!("{}/projects/{}/config", base, project_id))
            .await
            .expect("hub reachable");
        // Hub itself must still 200 even with Weaviate down.
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");
        // Candidates echoed back unchanged — fail-open contract.
        assert_eq!(
            body.get("development_collection").and_then(|v| v.as_str()),
            Some("Unreach_Development")
        );
        std::env::remove_var("VCT_WEAVIATE_URL");
    }
}
