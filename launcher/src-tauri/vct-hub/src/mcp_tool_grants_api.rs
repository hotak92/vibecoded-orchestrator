//! Per-project MCP tool-grant resolver routes (Phase 1.2 of the
//! diagrams-integration plan, 2026-05-24; generalised to any module-
//! shipped MCP in v0.2.34).
//!
//! Why this lives in vct-hub
//! -------------------------
//! A module-shipped MCP that exposes a per-tool allowlist spawns as a
//! subprocess of Claude Code and needs to resolve its per-project tool
//! allowlist on every `tools/list` request. It can't talk to
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
//!     "mcp_name": "code-reranker",
//!     "grants": {"rerank": true, "explain": false, ...},
//!     "defaults_applied": true|false
//!   }
//!   ```
//!   Returns 200 with the resolved grant map.
//!
//!   Resolution order (v0.2.34 Agent E — Phase 4 generalisation):
//!     1. Module-shipped defaults from `module_mcp_tool_defaults`
//!        (populated at module-install time from
//!        `manifest.mcp_registration.tool_allowlist`).
//!     2. Per-project overrides from `project_mcp_tool_grants` ALWAYS
//!        win — any row in that table replaces the corresponding
//!        default for that one project.
//!
//!   With no module-shipped defaults the grants map is EMPTY
//!   (failsafe-DENY for an unregistered MCP, not failsafe-ALLOW;
//!   matches the Phase 4 "new tools default off" rule). v0.2.102: the
//!   hardcoded fallback constants this route used to carry for the
//!   `mermaid` / `excalidraw` wrappers were removed with the dropped
//!   diagrams integrations (owner ruling 2026-10-07) — the wrappers
//!   are deleted, so nothing can legitimately query those names.
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
    // Module-shipped defaults (manifest.mcp_registration.
    // tool_allowlist → module_mcp_tool_defaults via migration 023).
    // With ZERO rows for `mcp_name` the map stays EMPTY — failsafe-DENY
    // for an unregistered MCP, never failsafe-ALLOW. (v0.2.102: the
    // hardcoded mermaid/excalidraw fallback that used to fill this arm
    // was removed with the deleted wrappers.)
    let mut grants: BTreeMap<String, bool> = BTreeMap::new();
    match h.0.list_mcp_tool_defaults(&mcp_name) {
        Ok(rows) => {
            for row in rows {
                grants.insert(row.tool_name, row.default_enabled);
            }
        }
        Err(e) => return err500(e),
    }

    // ─── Layer 2: apply per-project overrides ────────────────────────
    // Per-project rows in `project_mcp_tool_grants` ALWAYS override the
    // default. A row for a tool not in the default set is honoured too
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

    /// Seed a per-project tool-grant row the way the retired GUI writer
    /// (v0.2.101) used to — the route under test only READS the table.
    fn seed_tool_grant(db: &Db, project: &str, mcp: &str, tool: &str, enabled: bool) {
        db.lock()
            .execute(
                "INSERT INTO project_mcp_tool_grants
                 (project_id, mcp_name, tool_name, enabled)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(project_id, mcp_name, tool_name)
                 DO UPDATE SET enabled = excluded.enabled",
                rusqlite::params![project, mcp, tool, enabled as i32],
            )
            .unwrap();
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
    async fn returns_empty_grants_when_no_module_defaults_registered() {
        // v0.2.102: with the hardcoded mermaid/excalidraw fallback gone, a
        // query for a name with NO module-shipped defaults returns an empty
        // grants map (failsafe-DENY) — even for names the retired wrappers
        // once owned.
        let state = make_db_with_project("p1");
        let base = spawn_router(state).await;
        let client = reqwest::Client::new();
        for name in ["mermaid", "excalidraw"] {
            let resp = client
                .get(format!("{}/projects/p1/mcp-tool-grants/{}", base, name))
                .send()
                .await
                .expect("GET");
            assert_eq!(resp.status(), 200);
            let parsed: serde_json::Value = resp.json().await.unwrap();
            assert_eq!(parsed["mcp_name"], name);
            assert_eq!(parsed["defaults_applied"], true);
            assert!(
                parsed["grants"].as_object().unwrap().is_empty(),
                "{}: no module defaults → empty grants (failsafe-DENY), got: {}",
                name,
                parsed
            );
        }
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

    // ─── v0.2.34 Agent E (Phase 4 generalisation) tests ──────────────────
    //
    // The hub route consults `module_mcp_tool_defaults` and merges any
    // rows in `project_mcp_tool_grants`. (v0.2.102: the hardcoded-
    // constants fallback tests that used to sit above were removed with
    // the constants.) The tests below cover the GENERALISED path:
    //   * a non-bundled (paid) MCP whose defaults live ONLY in
    //     `module_mcp_tool_defaults` is fully served;
    //   * per-project overrides ALWAYS win over the defaults;
    //   * `defaults_applied: false` when ANY override exists.

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
        seed_tool_grant(&state.0, "p1", "code-reranker", "rerank", false);
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
        seed_tool_grant(&state.0, "p1", "code-reranker", "experimental_x", true);
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
