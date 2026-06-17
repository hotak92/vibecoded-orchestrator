//! Per-project MCP tool-grant resolver routes (Phase 1.2 of the
//! diagrams-integration plan, 2026-05-24).
//!
//! Why this lives in vct-hub
//! -------------------------
//! The wrapper MCPs (claude_mcp_servers/wrappers/_base.py +
//! mermaid_proxy.py + future Phase 4 wrappers) spawn as subprocesses
//! of Claude Code and need to resolve their per-project tool
//! allowlist on every `tools/list` request. They can't talk to
//! launcher.db directly because:
//!
//!   1. SQLite WAL is process-local for the writer journal; multiple
//!      writers from independent subprocesses race against the Tauri
//!      app's own writes.
//!   2. The launcher might not be running (CLI-only sessions); the
//!      hub may or may not be either, but the wrapper has an
//!      established failsafe-allow-all path for hub-unreachable that
//!      it does NOT have for SQLite-locked.
//!
//! Routing through the hub gives us:
//!
//!   * Single writer (launcher) + multiple read-only callers (wrappers).
//!   * Same bearer-token auth as the rest of the hub API (which binds
//!     `0.0.0.0` since v0.2.61 — the token, not the bind address, is the
//!     gate; see `server::start_hub_server`).
//!   * Cross-OS HTTP rather than cross-OS SQLite handle sharing.
//!
//! Routes
//! ------
//! `GET /api/v1/projects/{project_id}/mcp-tool-grants/{mcp_name}` →
//!   ```json
//!   {
//!     "mcp_name": "mermaid",
//!     "grants": {"render": true, "save_diagram": true, ...},
//!     "defaults_applied": true|false
//!   }
//!   ```
//!   Returns 200 with the resolved grant map.
//!
//!   Resolution order (v0.2.34 Agent E — Phase 4 generalisation):
//!     1. Module-shipped defaults from `module_mcp_tool_defaults`
//!        (populated at module-install time from
//!        `manifest.mcp_registration.tool_allowlist`).
//!     2. Hardcoded fallback defaults for orchestrator-bundled MCPs
//!        (`mermaid`, `excalidraw`) when no module defaults are
//!        registered — preserves v0.2.33 behaviour for projects whose
//!        bundled MCPs have never been part of a paid-module install
//!        cycle.
//!     3. Per-project overrides from `project_mcp_tool_grants` ALWAYS
//!        win — any row in that table replaces the corresponding
//!        default for that one project.
//!
//!   The `defaults_applied` boolean stays true ONLY when ZERO per-project
//!   override rows exist for `(project_id, mcp_name)`. Used by the
//!   launcher GUI to show a "(defaults)" badge next to the tool list.
//!
//!   Returns 404 when the project doesn't exist.
//!
//! `GET /api/v1/projects/by-path?path=<abs-path>` is INTENTIONALLY NOT
//! re-implemented here — it already lives in `modules_api.rs::get_
//! project_by_path_route`. The wrapper MCP uses that existing route
//! for project resolution; this module is grants-only.

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    routing::get,
    Json, Router,
};
use serde::Serialize;
use std::collections::BTreeMap;

use super::modules_api::LauncherDbHandle;

// ─── Default allowlists ──────────────────────────────────────────────────
//
// v0.2.34 Agent E (Phase 4 generalisation): defaults can now also be
// supplied by ANY module's `manifest.mcp_registration.tool_allowlist`
// block, which the launcher's install flow writes into
// `module_mcp_tool_defaults` (migration 023). The hardcoded constants
// below remain as a FALLBACK for orchestrator-bundled MCPs (mermaid,
// excalidraw) so they keep working even when no module-install cycle
// has run yet — i.e. on a fresh launcher install where no paid module
// has been touched.
//
// Resolution: module-shipped defaults (from the DB) take priority over
// hardcoded constants when both exist for the same `mcp_name`. The
// hub's GET handler queries the DB first and falls back to the const
// only when the DB has zero rows.

/// Hardcoded fallback per-tool allowlist for the Mermaid wrapper.
/// Used when no module has registered defaults for `"mermaid"` in
/// `module_mcp_tool_defaults`. Mirrors plan §3 Phase 1 item 5: the
/// three "save / render / validate" tools are enabled; the optional
/// ones are off by default and require explicit opt-in. Sorted
/// alphabetically so a future audit can diff it against the wrapper's
/// known surface without churn.
const MERMAID_DEFAULT_ALLOWLIST: &[(&str, bool)] = &[
    ("export_png", false),  // Chromium-dependent; lazy enable.
    ("list_themes", false), // Low value; can re-enable per project.
    ("render", true),
    ("save_diagram", true),
    ("validate_syntax", true),
];

/// Default per-tool allowlist for the Excalidraw wrapper (Phase 2,
/// 2026-05-25). Adapted to the ACTUAL tool surface of the vendored
/// `excalidraw-mcp-server@2.0.0` upstream (the plan §3 Phase 2 item 3
/// guessed `create_scene` / `read_scene` / `save_scene` but the real
/// v2.0.0 API is element-centric — the plan anticipated this revision).
///
/// Default ON (the basic scene-construction surface a project usually
/// wants):
///   create_element, update_element, delete_element, query_elements,
///   batch_create_elements, get_resource, read_me, create_view,
///   align_elements, distribute_elements.
///
/// Default OFF (require explicit opt-in via the launcher's Permissions
/// tab):
///   - export_scene — Chromium-dependent for PNG export + can also be
///     used to write to arbitrary disk paths (`.svg` / `.png`). Same
///     posture as Mermaid's export_png.
///   - group_elements / ungroup_elements / lock_elements /
///     unlock_elements — niche; opt-in for projects that need grouping.
///   - create_from_mermaid — overlaps with the Mermaid MCP's
///     `render` / `save_diagram` tools; using Mermaid directly avoids
///     an extra round-trip and keeps the two MCPs' surfaces orthogonal.
///
/// Sorted alphabetically (a future audit can diff against the wrapper's
/// known surface without churn).
const EXCALIDRAW_DEFAULT_ALLOWLIST: &[(&str, bool)] = &[
    ("align_elements", true),
    ("batch_create_elements", true),
    ("create_element", true),
    ("create_from_mermaid", false), // Overlaps with Mermaid MCP.
    ("create_view", true),
    ("delete_element", true),
    ("distribute_elements", true),
    ("export_scene", false), // Chromium/PNG path + arbitrary disk write; lazy enable.
    ("get_resource", true),
    ("group_elements", false),  // Niche; opt-in.
    ("lock_elements", false),   // Niche; opt-in.
    ("query_elements", true),
    ("read_me", true),
    ("ungroup_elements", false), // Niche; opt-in.
    ("unlock_elements", false),  // Niche; opt-in.
    ("update_element", true),
];

/// Look up the hardcoded fallback default allowlist for a given MCP
/// name. Returns an empty slice for an unknown MCP — the wrapper will
/// see an empty grants map and block everything except via tools the
/// project explicitly enables (failsafe-DENY for unknown MCPs, not
/// failsafe-ALLOW; matches Phase 4 "new tools default off" rule).
///
/// v0.2.34 (Agent E): this constant-table lookup is now the FALLBACK
/// path. `get_mcp_tool_grants` first consults `module_mcp_tool_defaults`
/// (manifest-driven) and only walks here when zero rows exist for
/// `mcp_name`. The match arms continue to enumerate orchestrator-
/// bundled MCPs that don't ship as installable modules.
fn _default_allowlist_for(mcp_name: &str) -> &'static [(&'static str, bool)] {
    match mcp_name {
        "mermaid" => MERMAID_DEFAULT_ALLOWLIST,
        "excalidraw" => EXCALIDRAW_DEFAULT_ALLOWLIST,
        // Other MCPs supply defaults via their manifest's tool_allowlist
        // block (v0.2.34 Agent E). Empty fallback when not registered.
        _ => &[],
    }
}

// ─── Routes ──────────────────────────────────────────────────────────────

pub fn router() -> Router<LauncherDbHandle> {
    Router::new()
        .route(
            "/projects/{project_id}/mcp-tool-grants/{mcp_name}",
            get(get_mcp_tool_grants),
        )
}

// ─── Response shape ──────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize)]
struct McpToolGrantsResponse {
    mcp_name: String,
    /// Map of tool_name → enabled. Sorted by key in the wire response
    /// so test fixtures are stable (BTreeMap is the obvious choice).
    grants: BTreeMap<String, bool>,
    /// True when no per-project rows exist in `project_mcp_tool_grants`
    /// and the response is the hardcoded default. The wrapper logs
    /// this at DEBUG; the launcher GUI uses it to show a "(defaults)"
    /// badge next to the tool list.
    defaults_applied: bool,
}

// ─── Helpers ─────────────────────────────────────────────────────────────

fn err500(e: String) -> axum::response::Response {
    (StatusCode::INTERNAL_SERVER_ERROR, Json(serde_json::json!({ "error": e }))).into_response()
}

fn err404(e: String) -> axum::response::Response {
    (StatusCode::NOT_FOUND, Json(serde_json::json!({ "error": e }))).into_response()
}

// ─── Handler ─────────────────────────────────────────────────────────────

async fn get_mcp_tool_grants(
    State(h): State<LauncherDbHandle>,
    Path((project_id, mcp_name)): Path<(String, String)>,
) -> impl IntoResponse {
    // Verify the project exists before we synthesise defaults — a
    // typo'd project_id should 404, not silently return defaults the
    // caller will assume are real.
    match h.0.get_project(&project_id) {
        Ok(Some(_)) => {}
        Ok(None) => {
            return err404(format!("project not found: {}", project_id));
        }
        Err(e) => return err500(e),
    }

    // ─── Layer 1: assemble defaults ──────────────────────────────────
    // Prefer module-shipped defaults (manifest.mcp_registration.
    // tool_allowlist → module_mcp_tool_defaults via migration 023).
    // Fall back to the hardcoded constants only when the DB carries
    // ZERO rows for `mcp_name`.
    let mut grants: BTreeMap<String, bool> = BTreeMap::new();
    match h.0.list_mcp_tool_defaults(&mcp_name) {
        Ok(rows) if !rows.is_empty() => {
            for row in rows {
                grants.insert(row.tool_name, row.default_enabled);
            }
        }
        Ok(_) => {
            // No module-shipped defaults — fall back to the hardcoded
            // table for orchestrator-bundled MCPs.
            for (name, en) in _default_allowlist_for(&mcp_name) {
                grants.insert(name.to_string(), *en);
            }
        }
        Err(e) => return err500(e),
    }

    // ─── Layer 2: apply per-project overrides ────────────────────────
    // Per-project rows in `project_mcp_tool_grants` ALWAYS override the
    // default (whether the default came from the DB or the hardcoded
    // table). A row for a tool not in the default set is honoured too
    // — useful when a user toggles an experimental tool the manifest
    // hasn't yet declared.
    let project_overrides = match h.0.list_project_mcp_tools(&project_id, &mcp_name) {
        Ok(rows) => rows,
        Err(e) => return err500(e),
    };
    let has_overrides = !project_overrides.is_empty();
    for row in project_overrides {
        grants.insert(row.tool_name, row.enabled);
    }

    let body = McpToolGrantsResponse {
        mcp_name: mcp_name.clone(),
        grants,
        // `defaults_applied: true` ⇔ NO per-project overrides exist.
        // The GUI uses this to show a "(defaults)" badge.
        defaults_applied: !has_overrides,
    };
    Json(body).into_response()
}

// ═══════════════════════════════════════════════════════════════════════
// Tests
// ═══════════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use vct_launcher_core::db::models::ProjectHost;
    use vct_launcher_core::db::Db;

    fn make_db_with_project(project_id: &str) -> LauncherDbHandle {
        let db = Db::open_in_memory().expect("in-memory db");
        let slug = db.generate_unique_slug("acme").unwrap();
        let folder = if cfg!(windows) { r"C:\tmp\x" } else { "/tmp/x" };
        db.insert_project(project_id, "Acme", folder, ProjectHost::Base, &slug)
            .unwrap();
        LauncherDbHandle(Arc::new(db))
    }

    /// Bind on a random port, spawn the router, return the base URL.
    /// Same shape `auth.rs` and `modules_api.rs` tests use; avoids a
    /// `tower` dev-dep just for `tower::oneshot`.
    async fn spawn_router(state: LauncherDbHandle) -> String {
        let app = router().with_state(state);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            axum::serve(listener, app).await.ok();
        });
        format!("http://127.0.0.1:{}", port)
    }

    #[tokio::test]
    async fn returns_default_allowlist_for_known_mcp() {
        let state = make_db_with_project("p1");
        let base = spawn_router(state).await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!("{}/projects/p1/mcp-tool-grants/mermaid", base))
            .send()
            .await
            .expect("GET");
        assert_eq!(resp.status(), 200);
        let parsed: serde_json::Value = resp.json().await.unwrap();

        assert_eq!(parsed["mcp_name"], "mermaid");
        assert_eq!(parsed["defaults_applied"], true);
        // Plan §3 Phase 1 item 5 defaults:
        assert_eq!(parsed["grants"]["render"], true);
        assert_eq!(parsed["grants"]["save_diagram"], true);
        assert_eq!(parsed["grants"]["validate_syntax"], true);
        assert_eq!(parsed["grants"]["export_png"], false);
        assert_eq!(parsed["grants"]["list_themes"], false);
    }

    #[tokio::test]
    async fn returns_empty_grants_for_unknown_mcp_on_known_project() {
        let state = make_db_with_project("p1");
        let base = spawn_router(state).await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!(
                "{}/projects/p1/mcp-tool-grants/never-shipped",
                base
            ))
            .send()
            .await
            .expect("GET");
        assert_eq!(resp.status(), 200);
        let parsed: serde_json::Value = resp.json().await.unwrap();
        // Empty grants map + defaults_applied=true → wrapper interprets
        // as "block everything" (failsafe-DENY for unknown MCPs).
        assert_eq!(parsed["mcp_name"], "never-shipped");
        assert_eq!(parsed["defaults_applied"], true);
        assert!(parsed["grants"].as_object().unwrap().is_empty());
    }

    #[tokio::test]
    async fn returns_404_for_unknown_project() {
        let state = make_db_with_project("p1");
        let base = spawn_router(state).await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!("{}/projects/ghost/mcp-tool-grants/mermaid", base))
            .send()
            .await
            .expect("GET");
        assert_eq!(resp.status(), 404);
        let parsed: serde_json::Value = resp.json().await.unwrap();
        assert!(parsed["error"].as_str().unwrap().contains("project not found"));
    }

    #[test]
    fn mermaid_default_allowlist_matches_plan_spec() {
        // Spec from plan §3 Phase 1 item 5:
        //   render ✓, save_diagram ✓, validate_syntax ✓,
        //   list_themes ✗, export_png ✗
        let expected: Vec<(&str, bool)> = vec![
            ("export_png", false),
            ("list_themes", false),
            ("render", true),
            ("save_diagram", true),
            ("validate_syntax", true),
        ];
        assert_eq!(MERMAID_DEFAULT_ALLOWLIST.to_vec(), expected);
        // Catches a future PR that adds entries out of order.
        let mut sorted = MERMAID_DEFAULT_ALLOWLIST.to_vec();
        sorted.sort_by_key(|(name, _)| *name);
        assert_eq!(
            sorted,
            MERMAID_DEFAULT_ALLOWLIST.to_vec(),
            "MERMAID_DEFAULT_ALLOWLIST must be sorted by tool name"
        );
    }

    #[test]
    fn default_allowlist_for_unknown_returns_empty_slice() {
        assert!(_default_allowlist_for("playwright").is_empty());
        assert!(_default_allowlist_for("").is_empty());
    }

    #[tokio::test]
    async fn returns_default_allowlist_for_excalidraw() {
        let state = make_db_with_project("p1");
        let base = spawn_router(state).await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!("{}/projects/p1/mcp-tool-grants/excalidraw", base))
            .send()
            .await
            .expect("GET");
        assert_eq!(resp.status(), 200);
        let parsed: serde_json::Value = resp.json().await.unwrap();

        assert_eq!(parsed["mcp_name"], "excalidraw");
        assert_eq!(parsed["defaults_applied"], true);
        // Core scene-construction tools enabled by default:
        assert_eq!(parsed["grants"]["create_element"], true);
        assert_eq!(parsed["grants"]["update_element"], true);
        assert_eq!(parsed["grants"]["delete_element"], true);
        assert_eq!(parsed["grants"]["query_elements"], true);
        assert_eq!(parsed["grants"]["get_resource"], true);
        // Lazy/niche tools disabled by default:
        assert_eq!(parsed["grants"]["export_scene"], false);
        assert_eq!(parsed["grants"]["group_elements"], false);
        assert_eq!(parsed["grants"]["create_from_mermaid"], false);
    }

    #[test]
    fn excalidraw_default_allowlist_is_sorted_and_unique() {
        // Catches a future PR that adds an entry out of order or twice.
        let mut sorted = EXCALIDRAW_DEFAULT_ALLOWLIST.to_vec();
        sorted.sort_by_key(|(name, _)| *name);
        assert_eq!(
            sorted,
            EXCALIDRAW_DEFAULT_ALLOWLIST.to_vec(),
            "EXCALIDRAW_DEFAULT_ALLOWLIST must be sorted by tool name"
        );
        let mut names: Vec<&str> = EXCALIDRAW_DEFAULT_ALLOWLIST
            .iter().map(|(n, _)| *n).collect();
        names.sort_unstable();
        names.dedup();
        assert_eq!(
            names.len(),
            EXCALIDRAW_DEFAULT_ALLOWLIST.len(),
            "EXCALIDRAW_DEFAULT_ALLOWLIST contains duplicate entries",
        );
    }

    #[test]
    fn excalidraw_allowlist_covers_v2_upstream_tool_surface() {
        // Phase 2 contract: every tool exposed by the vendored
        // excalidraw-mcp-server@2.0.0 must have an entry here so the
        // wrapper's allowlist filter has an explicit policy for it
        // (no implicit "default-allow because not listed").
        //
        // Source: `grep -hoE "server\\.tool\\('[a-z_]+'"
        // claude_mcp_servers/excalidraw_mcp_fork/dist/mcp/index.js`
        // captured 2026-05-25. If an upstream bump adds tools, this
        // assertion fails until they're explicitly classified.
        let upstream_v2_tools: &[&str] = &[
            "align_elements",
            "batch_create_elements",
            "create_element",
            "create_from_mermaid",
            "create_view",
            "delete_element",
            "distribute_elements",
            "export_scene",
            "get_resource",
            "group_elements",
            "lock_elements",
            "query_elements",
            "read_me",
            "ungroup_elements",
            "unlock_elements",
            "update_element",
        ];
        let listed: std::collections::BTreeSet<&str> =
            EXCALIDRAW_DEFAULT_ALLOWLIST.iter().map(|(n, _)| *n).collect();
        for tool in upstream_v2_tools {
            assert!(
                listed.contains(tool),
                "v2.0.0 upstream tool `{}` is not classified in \
                 EXCALIDRAW_DEFAULT_ALLOWLIST — add an explicit entry",
                tool,
            );
        }
        assert_eq!(
            listed.len(),
            upstream_v2_tools.len(),
            "EXCALIDRAW_DEFAULT_ALLOWLIST has entries beyond v2.0.0 \
             upstream surface — drop them or bump the upstream-tools \
             reference list in this test if v2.x added them",
        );
    }

    // ─── v0.2.34 Agent E (Phase 4 generalisation) tests ──────────────────
    //
    // The hub route now consults `module_mcp_tool_defaults` and merges
    // any rows in `project_mcp_tool_grants`. The existing tests above
    // exercise the FALLBACK path (hardcoded constants + no per-project
    // rows). The tests below cover the GENERALISED path:
    //   * module-shipped defaults take priority over the hardcoded
    //     constants when both exist;
    //   * a non-bundled (paid) MCP whose defaults live ONLY in
    //     `module_mcp_tool_defaults` is fully served;
    //   * per-project overrides ALWAYS win over either default source;
    //   * `defaults_applied: false` when ANY override exists.

    #[tokio::test]
    async fn module_shipped_defaults_take_priority_over_hardcoded() {
        // A paid module re-skins the mermaid wrapper with a different
        // policy (export_png on by default; list_themes still off; the
        // module ADDS a new "lint" tool with default off). The DB
        // values must win — the hardcoded MERMAID_DEFAULT_ALLOWLIST is
        // only consulted when the DB has zero rows.
        let state = make_db_with_project("p1");
        state
            .0
            .reconcile_mcp_tool_defaults(
                "mermaid",
                "third-party-diagrams",
                &[
                    ("export_png".to_string(), true, None),
                    ("list_themes".to_string(), false, None),
                    ("render".to_string(), true, None),
                    ("save_diagram".to_string(), true, None),
                    ("validate_syntax".to_string(), true, None),
                    ("lint".to_string(), false, None),
                ],
                123,
            )
            .unwrap();
        let base = spawn_router(state).await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!("{}/projects/p1/mcp-tool-grants/mermaid", base))
            .send()
            .await
            .expect("GET");
        assert_eq!(resp.status(), 200);
        let parsed: serde_json::Value = resp.json().await.unwrap();
        // Module-shipped row overrides hardcoded entry.
        assert_eq!(parsed["grants"]["export_png"], true);
        // Newly-added tool surfaces.
        assert_eq!(parsed["grants"]["lint"], false);
        // No per-project rows → defaults_applied stays true.
        assert_eq!(parsed["defaults_applied"], true);
    }

    #[tokio::test]
    async fn module_shipped_defaults_for_non_bundled_mcp() {
        // A paid module ships a brand-new MCP (no hardcoded fallback
        // exists). The route must still return its declared defaults.
        let state = make_db_with_project("p1");
        state
            .0
            .reconcile_mcp_tool_defaults(
                "code-reranker",
                "vct-rl-reranker",
                &[
                    ("rerank".to_string(), true, Some("re-rank results".into())),
                    ("explain".to_string(), false, None),
                ],
                100,
            )
            .unwrap();
        let base = spawn_router(state).await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!(
                "{}/projects/p1/mcp-tool-grants/code-reranker",
                base
            ))
            .send()
            .await
            .expect("GET");
        assert_eq!(resp.status(), 200);
        let parsed: serde_json::Value = resp.json().await.unwrap();
        assert_eq!(parsed["mcp_name"], "code-reranker");
        assert_eq!(parsed["grants"]["rerank"], true);
        assert_eq!(parsed["grants"]["explain"], false);
        assert_eq!(parsed["defaults_applied"], true);
    }

    #[tokio::test]
    async fn per_project_overrides_win_over_module_defaults() {
        // Module defaults rerank=on; the user disabled it for THIS
        // project. Hub response must show rerank=false AND
        // defaults_applied=false.
        let state = make_db_with_project("p1");
        state
            .0
            .reconcile_mcp_tool_defaults(
                "code-reranker",
                "vct-rl-reranker",
                &[
                    ("rerank".to_string(), true, None),
                    ("explain".to_string(), false, None),
                ],
                100,
            )
            .unwrap();
        state
            .0
            .set_mcp_tool_enabled("p1", "code-reranker", "rerank", false)
            .unwrap();
        let base = spawn_router(state).await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!(
                "{}/projects/p1/mcp-tool-grants/code-reranker",
                base
            ))
            .send()
            .await
            .expect("GET");
        let parsed: serde_json::Value = resp.json().await.unwrap();
        assert_eq!(parsed["grants"]["rerank"], false);
        // Tool NOT overridden keeps its default.
        assert_eq!(parsed["grants"]["explain"], false);
        // ANY override flips defaults_applied to false.
        assert_eq!(parsed["defaults_applied"], false);
    }

    #[tokio::test]
    async fn per_project_override_for_tool_not_in_defaults_surfaces() {
        // User toggles a tool the manifest hasn't declared (e.g. an
        // experimental tool the upstream surfaces but the wrapper
        // author didn't anticipate). The merge must STILL include it
        // — the user's row in `project_mcp_tool_grants` is the
        // source of truth.
        let state = make_db_with_project("p1");
        state
            .0
            .set_mcp_tool_enabled("p1", "code-reranker", "experimental_x", true)
            .unwrap();
        let base = spawn_router(state).await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!(
                "{}/projects/p1/mcp-tool-grants/code-reranker",
                base
            ))
            .send()
            .await
            .expect("GET");
        let parsed: serde_json::Value = resp.json().await.unwrap();
        assert_eq!(parsed["grants"]["experimental_x"], true);
        assert_eq!(parsed["defaults_applied"], false);
    }

    #[tokio::test]
    async fn reconcile_removes_old_tool_on_module_update() {
        // v0.2.7 of a module ships [tool_a, tool_b]; v0.2.8 drops
        // tool_b. After re-reconcile the hub response must NOT include
        // tool_b. Per-project overrides for tool_b survive in the DB
        // (we don't soft-delete them) but since `grants` is composed
        // from defaults + overrides, the override surfaces ONLY if a
        // row exists. If the user never toggled tool_b, it disappears.
        let state = make_db_with_project("p1");
        state
            .0
            .reconcile_mcp_tool_defaults(
                "fancy-mcp",
                "fancy-module",
                &[
                    ("tool_a".to_string(), true, None),
                    ("tool_b".to_string(), false, None),
                ],
                100,
            )
            .unwrap();
        // Re-reconcile with tool_b removed.
        state
            .0
            .reconcile_mcp_tool_defaults(
                "fancy-mcp",
                "fancy-module",
                &[("tool_a".to_string(), true, None)],
                200,
            )
            .unwrap();
        let base = spawn_router(state).await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!("{}/projects/p1/mcp-tool-grants/fancy-mcp", base))
            .send()
            .await
            .expect("GET");
        let parsed: serde_json::Value = resp.json().await.unwrap();
        assert!(parsed["grants"].get("tool_b").is_none());
        assert_eq!(parsed["grants"]["tool_a"], true);
    }
}
