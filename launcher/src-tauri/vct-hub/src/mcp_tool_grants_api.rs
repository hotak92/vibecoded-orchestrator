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
//!   * Same auth + 127.0.0.1 binding as the rest of the hub API.
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
//!   Returns 200 with the resolved grant map. When the project has
//!   no per-tool overrides in `project_mcp_tool_grants` (Phase 1.1
//!   sibling table), falls back to a hardcoded default for the
//!   wrapped MCP — see `_default_allowlist_for` below.
//!
//!   Returns 404 when the project doesn't exist.
//!   Returns 200 with `{"defaults_applied": true, "grants": <defaults>}`
//!   when the project exists but the grants table is empty / hasn't
//!   shipped yet (Phase 1.1 sibling).
//!
//! TODO-Phase-4 (marker for the future sibling): replace the hardcoded
//! `MERMAID_DEFAULT_ALLOWLIST` with a lookup against
//! `bundled_tool_defaults.toml` + the `mcp_tool_defaults` SQLite table.
//! When that lands, this module turns into a thin pass-through.
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
// TODO-Phase-4: move to `bundled_tool_defaults.toml`. Mirror the Python
// loader pattern used for bundled_mcp_versions.toml so both the Rust
// hub and any Python tooling parse the SAME file.

/// Default per-tool allowlist for the Mermaid wrapper. Mirrors plan
/// §3 Phase 1 item 5: the three "save / render / validate" tools are
/// enabled; the optional ones are off by default and require explicit
/// opt-in. Sorted alphabetically so a future audit can diff it
/// against the wrapper's known surface without churn.
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

/// Look up the hardcoded default allowlist for a given MCP name.
/// Returns an empty slice for an unknown MCP — the wrapper will see
/// an empty grants map and block everything except via tools the
/// project explicitly enables (failsafe-DENY for unknown MCPs, not
/// failsafe-ALLOW; matches Phase 4 "new tools default off" rule).
fn _default_allowlist_for(mcp_name: &str) -> &'static [(&'static str, bool)] {
    match mcp_name {
        "mermaid" => MERMAID_DEFAULT_ALLOWLIST,
        "excalidraw" => EXCALIDRAW_DEFAULT_ALLOWLIST,
        // Phase 4 expands this match arm.
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

    // Phase 1.1 sibling owns `project_mcp_tool_grants`. Until it
    // lands, the launcher_core Db has no `list_project_mcp_tool_grants`
    // accessor; we fall through to defaults unconditionally. The
    // grants table merge happens in the same place when the sibling
    // adds the accessor — see the integration note in the module
    // doc-comment above.
    //
    // Pre-merge contract: this branch ALWAYS returns
    // `defaults_applied: true`. Post-merge, it returns false when
    // the DB has at least one row for (project_id, mcp_name).
    let defaults = _default_allowlist_for(&mcp_name);
    let grants: BTreeMap<String, bool> = defaults
        .iter()
        .map(|(name, en)| (name.to_string(), *en))
        .collect();

    let body = McpToolGrantsResponse {
        mcp_name: mcp_name.clone(),
        grants,
        defaults_applied: true,
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
}
