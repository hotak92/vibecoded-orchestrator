// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Hub HTTP routes for RL telemetry events (migration 025 / v0.2.47).
//!
//! Replaces the JSONL corpus at `~/.claude/retrieval_rl_data/rl_events.jsonl`
//! with a queryable, indexed SQLite table. The Python writer in
//! `claude_mcp_servers/rl_client/hub_writer.py` POSTs every v3 event to
//! `POST /api/v1/rl/events`; this handler parses the JSON, pulls the
//! indexed columns, and calls `Db::insert_rl_event` (vct-launcher-core).
//!
//! Routes:
//!
//!   POST   /api/v1/rl/events            — insert one event (used by MCP)
//!   GET    /api/v1/rl/events            — list events (dashboard / offline trainer)
//!   GET    /api/v1/rl/events/count      — count events (Identity-tab badge)
//!
//! Auth: standard hub.token bearer middleware applies (mounted INSIDE the
//! hub-wide `auth::require_auth` layer in `server.rs`).
//!
//! Soft-fail discipline: every error returns `Err(StatusCode + JSON envelope)`.
//! The Python writer treats non-2xx as data loss (no retry queue, no JSONL
//! fallback — per the locked decision 2026-06-04).

use axum::{
    extract::{Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};

use super::modules_api::LauncherDbHandle;

pub fn router() -> Router<LauncherDbHandle> {
    Router::new()
        .route("/rl/events", post(post_event).get(list_events))
        .route("/rl/events/count", get(count_events))
}

// ─── POST /api/v1/rl/events ────────────────────────────────────────────

/// Incoming event body. The full v3 event JSON is in `payload_json`;
/// the indexed columns must match the corresponding fields inside it.
/// The hub does NOT cross-validate that the indexed columns agree with
/// the embedded JSON — the writer is trusted (single source of truth
/// for one record, and the auth gate already established who's writing).
#[derive(Debug, Deserialize)]
pub struct PostEventBody {
    pub event_type: String,
    pub schema_version: i64,
    /// Unix epoch millis; captured at the writer side at event time, not
    /// at hub-arrival time (the latter could lag by buffering).
    pub ts_ms: i64,
    #[serde(default)]
    pub project_id: Option<String>,
    #[serde(default)]
    pub project_name: Option<String>,
    pub task_id: String,
    #[serde(default)]
    pub task_type: Option<String>,
    #[serde(default)]
    pub embedding_source: Option<String>,
    #[serde(default)]
    pub embedding_dim: Option<i64>,
    #[serde(default)]
    pub embedding_model: Option<String>,
    pub payload_json: String,
}

#[derive(Debug, Serialize)]
pub struct PostEventResponse {
    pub ok: bool,
    pub id: i64,
}

async fn post_event(
    State(h): State<LauncherDbHandle>,
    Json(body): Json<PostEventBody>,
) -> impl IntoResponse {
    // Cheap input validation. Bad event_type / missing task_id should 400,
    // not 500 — these are writer bugs we want surfaced loudly.
    if body.event_type != "retrieval" && body.event_type != "citation" {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({
                "error": {
                    "code": "bad_event_type",
                    "message": "event_type must be 'retrieval' or 'citation'",
                }
            })),
        )
            .into_response();
    }
    if body.task_id.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({
                "error": {
                    "code": "missing_task_id",
                    "message": "task_id is required",
                }
            })),
        )
            .into_response();
    }
    if body.payload_json.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({
                "error": {
                    "code": "missing_payload",
                    "message": "payload_json is required",
                }
            })),
        )
            .into_response();
    }

    match h.0.insert_rl_event(
        &body.event_type,
        body.schema_version,
        body.ts_ms,
        body.project_id.as_deref(),
        body.project_name.as_deref(),
        &body.task_id,
        body.task_type.as_deref(),
        body.embedding_source.as_deref(),
        body.embedding_dim,
        body.embedding_model.as_deref(),
        &body.payload_json,
    ) {
        Ok(id) => (StatusCode::OK, Json(PostEventResponse { ok: true, id })).into_response(),
        Err(e) => {
            eprintln!("[vct-hub] post_rl_event insert failed: {}", e);
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({
                    "error": {
                        "code": "insert_failed",
                        "message": "rl_events insert failed",
                    }
                })),
            )
                .into_response()
        }
    }
}

// ─── GET /api/v1/rl/events ─────────────────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct ListEventsQuery {
    pub project_id: Option<String>,
    pub event_type: Option<String>,
    pub since_ms: Option<i64>,
    pub until_ms: Option<i64>,
    pub limit: Option<u32>,
}

#[derive(Debug, Serialize)]
pub struct RlEventOut {
    pub id: i64,
    pub event_type: String,
    pub schema_version: i64,
    pub ts_ms: i64,
    pub project_id: Option<String>,
    pub project_name: Option<String>,
    pub task_id: String,
    pub task_type: Option<String>,
    pub embedding_source: Option<String>,
    pub embedding_dim: Option<i64>,
    pub embedding_model: Option<String>,
    pub payload_json: String,
}

async fn list_events(
    State(h): State<LauncherDbHandle>,
    Query(q): Query<ListEventsQuery>,
) -> impl IntoResponse {
    let limit = q.limit.unwrap_or(500).min(10_000);
    match h.0.list_rl_events(
        q.project_id.as_deref(),
        q.event_type.as_deref(),
        q.since_ms,
        q.until_ms,
        limit,
    ) {
        Ok(rows) => {
            let out: Vec<RlEventOut> = rows
                .into_iter()
                .map(|e| RlEventOut {
                    id: e.id,
                    event_type: e.event_type,
                    schema_version: e.schema_version,
                    ts_ms: e.ts_ms,
                    project_id: e.project_id,
                    project_name: e.project_name,
                    task_id: e.task_id,
                    task_type: e.task_type,
                    embedding_source: e.embedding_source,
                    embedding_dim: e.embedding_dim,
                    embedding_model: e.embedding_model,
                    payload_json: e.payload_json,
                })
                .collect();
            (StatusCode::OK, Json(out)).into_response()
        }
        Err(e) => {
            eprintln!("[vct-hub] list_rl_events failed: {}", e);
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({
                    "error": {
                        "code": "list_failed",
                        "message": "rl_events list failed",
                    }
                })),
            )
                .into_response()
        }
    }
}

// ─── GET /api/v1/rl/events/count ───────────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct CountEventsQuery {
    pub project_id: Option<String>,
    pub event_type: Option<String>,
    pub since_ms: Option<i64>,
}

#[derive(Debug, Serialize)]
pub struct CountEventsResponse {
    pub count: i64,
}

async fn count_events(
    State(h): State<LauncherDbHandle>,
    Query(q): Query<CountEventsQuery>,
) -> impl IntoResponse {
    match h.0.count_rl_events(
        q.project_id.as_deref(),
        q.event_type.as_deref(),
        q.since_ms,
    ) {
        Ok(n) => (StatusCode::OK, Json(CountEventsResponse { count: n })).into_response(),
        Err(e) => {
            eprintln!("[vct-hub] count_rl_events failed: {}", e);
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({
                    "error": {
                        "code": "count_failed",
                        "message": "rl_events count failed",
                    }
                })),
            )
                .into_response()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use vct_launcher_core::db::Db;

    /// Spawn the router on a random local port. Mirrors the
    /// `modules_api::tests::spawn_modules_api_hub` pattern so the test
    /// dependency footprint matches the rest of the hub.
    async fn spawn_test_hub() -> String {
        let db = Db::open_in_memory().expect("in-memory db");
        let handle = LauncherDbHandle(Arc::new(db));
        let app: Router =
            Router::new().nest("/api/v1", router().with_state(handle.clone()));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("local_addr");
        tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        format!("http://{}/api/v1", addr)
    }

    #[tokio::test]
    async fn post_event_then_list_round_trips() {
        let base = spawn_test_hub().await;
        let client = reqwest::Client::new();

        let body = serde_json::json!({
            "event_type": "retrieval",
            "schema_version": 3,
            "ts_ms": 1_700_000_000_000_i64,
            "project_name": "VCO_dev",
            "task_id": "abc",
            "task_type": "mcp_interactive",
            "embedding_source": "qwen3",
            "embedding_dim": 1024,
            "embedding_model": "qwen3-embedding:0.6b",
            "payload_json": "{\"event\":\"retrieval\",\"schema_version\":3}",
        });
        let resp = client
            .post(format!("{}/rl/events", base))
            .json(&body)
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), reqwest::StatusCode::OK);

        let resp = client
            .get(format!("{}/rl/events?limit=10", base))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), reqwest::StatusCode::OK);
        let arr: serde_json::Value = resp.json().await.unwrap();
        assert_eq!(arr.as_array().unwrap().len(), 1);
        assert_eq!(arr[0]["task_id"], "abc");
        assert_eq!(arr[0]["embedding_model"], "qwen3-embedding:0.6b");
    }

    #[tokio::test]
    async fn bad_event_type_returns_400() {
        let base = spawn_test_hub().await;
        let client = reqwest::Client::new();
        let body = serde_json::json!({
            "event_type": "bogus",
            "schema_version": 3,
            "ts_ms": 1,
            "task_id": "x",
            "payload_json": "{}",
        });
        let resp = client
            .post(format!("{}/rl/events", base))
            .json(&body)
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), reqwest::StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn missing_task_id_returns_400() {
        let base = spawn_test_hub().await;
        let client = reqwest::Client::new();
        let body = serde_json::json!({
            "event_type": "retrieval",
            "schema_version": 3,
            "ts_ms": 1,
            "task_id": "",
            "payload_json": "{}",
        });
        let resp = client
            .post(format!("{}/rl/events", base))
            .json(&body)
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), reqwest::StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn count_endpoint_returns_correct_total() {
        let base = spawn_test_hub().await;
        let client = reqwest::Client::new();
        for i in 0..3_i64 {
            let body = serde_json::json!({
                "event_type": if i % 2 == 0 { "retrieval" } else { "citation" },
                "schema_version": 3,
                "ts_ms": 1_000 + i,
                "task_id": format!("task-{}", i),
                "payload_json": "{}",
            });
            let _ = client
                .post(format!("{}/rl/events", base))
                .json(&body)
                .send()
                .await
                .unwrap();
        }

        let resp = client
            .get(format!("{}/rl/events/count", base))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), reqwest::StatusCode::OK);
        let v: serde_json::Value = resp.json().await.unwrap();
        assert_eq!(v["count"], 3);

        let resp = client
            .get(format!("{}/rl/events/count?event_type=citation", base))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), reqwest::StatusCode::OK);
        let v: serde_json::Value = resp.json().await.unwrap();
        assert_eq!(v["count"], 1);
    }
}
