// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! `GET /api/v1/chat-model-context` — read the chat-model context table
//! (v0.2.92, WP-11).
//!
//! ## Why the hub serves a table the gateway reads from a file
//!
//! The model gateway is deliberately hub-independent: it reads
//! `<vct_root>/model-gateway/chat_model_context.json`, which the launcher
//! exports on every mutation and on every boot. That covers the gateway.
//!
//! It does NOT cover a CLI or an agent that wants to know what the table
//! currently says — those would otherwise open `launcher.db` directly, which
//! is the thing the hub exists to stop (SQLite's WAL writer journal is
//! process-local; the launcher is the single writer; and a headless CLI
//! session may run while the launcher is closed). This route gives them the
//! same document over HTTP.
//!
//! ## READ-ONLY, on purpose
//!
//! There is no POST/PUT/DELETE here and there must not be one. The launcher
//! is the single writer for `launcher.db`; a hub write path would be a
//! SECOND writer, and the export-on-every-mutation invariant (the thing that
//! keeps the gateway's file in step with the table) lives in the launcher's
//! Tauri command layer. A hub mutation would silently skip it, leaving the
//! gateway serving a table the user can see is stale in the GUI — the exact
//! "shipped a preference nothing consumes" defect this package exists to
//! avoid. Edits go through the launcher's `chat_model_context_upsert` /
//! `_delete` / `_reseed` commands.
//!
//! ## Auth: the ORDINARY global-token regime
//!
//! `/api/v1/projects/{id}/env` and `/config` are the EXCEPTION in this hub —
//! they require a project-scoped `hub.token.<id>`. This route is not
//! per-project (a model's context window is a property of the model), so it
//! is a normal global-`hub.token` route like every other `/api/v1/*`
//! endpoint. A project-scoped token is refused here, and the test below pins
//! both halves of that so a future change to `per_project_token_route`
//! cannot silently widen or narrow this route.
//!
//! ## Wire shape
//!
//! Exactly the document the gateway's reader parses — same builder
//! (`vct_launcher_core::db::chat_model_context::export_document`), so the
//! HTTP body and the exported file can never drift into two shapes:
//!
//! ```json
//! {"schema_version": 1, "generated_at": "<ISO-8601 UTC>",
//!  "source": "launcher.db",
//!  "models": {"<full-model-id>": {"vendor": "...", "context_window": 0,
//!                                 "max_output": 0, "window_1m": false,
//!                                 "source": "...", "source_note": "..."}}}
//! ```
//!
//! `generated_at` is the time of THIS read, not of the last export: the body
//! is generated now, from the table, and saying otherwise would misreport
//! freshness.

use axum::{extract::State, http::StatusCode, response::IntoResponse, routing::get, Json, Router};

use vct_launcher_core::db::chat_model_context::{export_document, now_iso8601_utc};

use super::modules_api::LauncherDbHandle;
use crate::http_error::error_response;

pub fn router() -> Router<LauncherDbHandle> {
    Router::new().route("/chat-model-context", get(get_chat_model_context))
}

async fn get_chat_model_context(State(db): State<LauncherDbHandle>) -> impl IntoResponse {
    match db.0.list_chat_model_context() {
        Ok(rows) => Json(export_document(&rows, &now_iso8601_utc())).into_response(),
        Err(e) => error_response(
            StatusCode::INTERNAL_SERVER_ERROR,
            "chat_model_context_read_failed",
            format!(
                "could not read the chat-model context table from launcher.db: {e}. \
                 The launcher creates it on first boot (migration 043); if this \
                 persists, open the launcher once so the migration runs."
            ),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::auth::{require_auth, AuthState};
    use crate::project_tokens::ProjectTokenRegistry;
    use axum::http::StatusCode;
    use std::collections::HashMap;
    use std::sync::{Arc, Mutex};
    use vct_launcher_core::db::chat_model_context::ChatModelContextInput;
    use vct_launcher_core::db::Db;

    /// In-memory launcher.db. NEVER opens the user's real
    /// `~/.vct/launcher.db` and never spawns a hub process — the router is
    /// served in-process on an ephemeral port, exactly as `auth.rs`'s own
    /// tests do.
    fn db_handle() -> LauncherDbHandle {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        vct_launcher_core::db::migrations::apply(&conn).unwrap();
        LauncherDbHandle(Arc::new(Db(Mutex::new(conn))))
    }

    fn seed(db: &LauncherDbHandle) {
        db.0.upsert_chat_model_context(
            ChatModelContextInput {
                model_id: "glm-5.2".into(),
                vendor: "zai".into(),
                context_window: 1_000_000,
                max_output: 128_000,
                window_1m: true,
                source: "https://docs.z.ai/guides/llm/glm-5.2".into(),
                source_note: String::new(),
            },
            false,
        )
        .unwrap();
        db.0.upsert_chat_model_context(
            ChatModelContextInput {
                model_id: "glm-5.1".into(),
                vendor: "zai".into(),
                context_window: 200_000,
                max_output: 128_000,
                window_1m: false,
                source: "https://docs.z.ai/guides/llm/glm-5.1".into(),
                source_note: String::new(),
            },
            false,
        )
        .unwrap();
    }

    /// The route mounted the way `server.rs` mounts it: nested under
    /// `/api/v1`, inside the hub-wide auth layer, with a project-token
    /// registry present so the auth pair is exercised against the REAL
    /// middleware rather than a simplified stand-in.
    fn app(db: LauncherDbHandle, global_token: &str, projects: &[(&str, &str)]) -> Router {
        let mut map = HashMap::new();
        for (pid, tok) in projects {
            map.insert(pid.to_string(), tok.to_string());
        }
        Router::new()
            .nest("/api/v1", router().with_state(db))
            .layer(axum::middleware::from_fn(require_auth))
            .layer(axum::Extension(AuthState::new(global_token.to_string())))
            .layer(axum::Extension(ProjectTokenRegistry::from_map(map)))
    }

    async fn spawn(app: Router) -> String {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        format!("http://{}", addr)
    }

    #[tokio::test]
    async fn global_token_is_accepted_and_serves_the_export_shape() {
        let db = db_handle();
        seed(&db);
        let base = spawn(app(db, "global-token", &[("p1", "project-token")])).await;

        let resp = reqwest::Client::new()
            .get(format!("{}/api/v1/chat-model-context", base))
            .header("Authorization", "Bearer global-token")
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);

        let body: serde_json::Value = resp.json().await.unwrap();
        assert_eq!(body["schema_version"], serde_json::json!(1));
        assert_eq!(body["source"], serde_json::json!("launcher.db"));
        assert!(
            body["generated_at"].as_str().unwrap().ends_with('Z'),
            "generated_at must be ISO-8601 UTC"
        );
        let models = body["models"].as_object().unwrap();
        assert_eq!(models.len(), 2);
        assert_eq!(models["glm-5.2"]["window_1m"], serde_json::json!(true));
        assert_eq!(models["glm-5.1"]["window_1m"], serde_json::json!(false));
        // Same builder as the exported file ⇒ same key order.
        let keys: Vec<&String> = models.keys().collect();
        assert_eq!(keys, vec!["glm-5.1", "glm-5.2"]);
    }

    /// THE OTHER HALF OF THE PAIR: a per-project token — valid for
    /// `/projects/{id}/env` — is REFUSED here. This route is not
    /// per-project, so it lives under the ordinary global-token regime.
    #[tokio::test]
    async fn a_project_scoped_token_is_refused() {
        let base = spawn(app(
            db_handle(),
            "global-token",
            &[("p1", "project-token")],
        ))
        .await;

        let resp = reqwest::Client::new()
            .get(format!("{}/api/v1/chat-model-context", base))
            .header("Authorization", "Bearer project-token")
            .send()
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            StatusCode::UNAUTHORIZED,
            "a project-scoped token must not open a global route"
        );
    }

    #[tokio::test]
    async fn no_token_is_refused() {
        let base = spawn(app(db_handle(), "global-token", &[])).await;
        let resp = reqwest::get(format!("{}/api/v1/chat-model-context", base))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    /// An empty table is a valid document, not a 404 and not a null: a fresh
    /// install that has not yet been seeded still answers with a parseable
    /// body, which is what keeps a CLI consumer's error handling simple.
    #[tokio::test]
    async fn an_empty_table_serves_an_empty_models_object() {
        let base = spawn(app(db_handle(), "global-token", &[])).await;
        let resp = reqwest::Client::new()
            .get(format!("{}/api/v1/chat-model-context", base))
            .header("Authorization", "Bearer global-token")
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body: serde_json::Value = resp.json().await.unwrap();
        assert!(body["models"].as_object().unwrap().is_empty());
    }

    /// THE ROUTE IS ACTUALLY MOUNTED IN THE REAL SERVER.
    ///
    /// Every other test in this module builds its own router, so all of them
    /// keep passing if `server.rs` never nests this one — the route would be
    /// written, tested and unreachable. (Red-proofed: deleting the `.nest`
    /// call produced an unused-import WARNING and zero test failures.) This
    /// test reads the server's source at compile time and asserts the mount
    /// is there, which is the cheapest check that survives an unmount.
    ///
    /// Also asserts it is nested under the SAME `/api/v1` prefix as its
    /// siblings, and that it is inside the router that `apply_auth_layers`
    /// wraps — a route mounted after the auth stack would serve the table to
    /// an unauthenticated caller.
    #[test]
    fn the_route_is_mounted_in_the_real_hub_router() {
        let server_src = include_str!("server.rs");
        let mount = "chat_model_context_api::router().with_state(launcher_state.clone())";
        assert!(
            server_src.contains(mount),
            "server.rs no longer mounts the chat-model-context router; the \
             route exists but nothing can reach it"
        );

        // The mount must sit in the `routes` chain that is later handed to
        // `apply_auth_layers(routes, ...)`, not appended afterwards.
        let routes_block = server_src
            .split("let routes = axum::Router::new()")
            .nth(1)
            .expect("server.rs still builds `routes`")
            .split("let app = apply_auth_layers(")
            .next()
            .expect("server.rs still applies the auth layers");
        assert!(
            routes_block.contains(mount),
            "the chat-model-context route is mounted outside the router that \
             apply_auth_layers wraps — it would answer without a token"
        );
        assert!(
            routes_block.contains("\"/api/v1\",\n            chat_model_context_api::router()"),
            "the route must be nested under /api/v1 like its siblings"
        );
    }

    /// The route is READ-ONLY: writing methods are not routed, so a caller
    /// that tries to mutate through the hub gets a clean 405 rather than a
    /// second writer.
    #[tokio::test]
    async fn write_methods_are_not_routed() {
        let base = spawn(app(db_handle(), "global-token", &[])).await;
        for method in [reqwest::Method::POST, reqwest::Method::DELETE, reqwest::Method::PUT] {
            let resp = reqwest::Client::new()
                .request(method.clone(), format!("{}/api/v1/chat-model-context", base))
                .header("Authorization", "Bearer global-token")
                .send()
                .await
                .unwrap();
            assert_eq!(
                resp.status(),
                StatusCode::METHOD_NOT_ALLOWED,
                "{} must not be routed",
                method
            );
        }
    }
}
