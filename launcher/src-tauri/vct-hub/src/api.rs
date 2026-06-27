//! HTTP API routes for the VCT Hub.
//!
//! Base URL: http://localhost:7700/api/v1
//!
//! Routes:
//!   POST   /apps/register          — register an app with the hub
//!   DELETE /apps/{app_id}           — deregister an app
//!   POST   /apps/{app_id}/heartbeat — keep-alive ping
//!   GET    /apps                    — list all registered apps
//!   GET    /apps/{app_id}           — get one app's info
//!
//!   POST   /messages                — send a message
//!   GET    /messages/{recipient}    — poll messages for an app
//!   POST   /messages/{id}/ack       — mark message as read
//!
//!   POST   /data/register           — register a data source
//!   GET    /data/catalog             — query available data sources
//!
//!   GET    /health                   — hub health check

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{delete, get, post},
    Json, Router,
};
use serde::Deserialize;

use super::db::{self, Db};

pub fn router(db: Db) -> Router {
    Router::new()
        // Health
        .route("/health", get(health))
        // App registry
        .route("/apps", get(list_apps))
        .route("/apps/register", post(register_app))
        .route("/apps/{app_id}", delete(deregister_app))
        .route("/apps/{app_id}/heartbeat", post(heartbeat))
        // Messages
        .route("/messages", post(send_message))
        .route("/messages/{recipient}", get(poll_messages))
        .route("/messages/{id}/ack", post(ack_message))
        // Data catalog
        .route("/data/register", post(register_data))
        .route("/data/catalog", get(query_data_catalog))
        .with_state(db)
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

async fn health() -> impl IntoResponse {
    // v0.2.69 (hub-staleness home #3): expose the build fingerprint
    // alongside `version`. The compile-time workspace `version` is blind to
    // same-version-but-different-code builds; `build_fingerprint` carries a
    // git short-SHA (when baked by build.rs) so staleness is detectable
    // across same-version builds. `null` when no git SHA was available at
    // compile time (released tarball / CI without a checkout).
    Json(serde_json::json!({
        "status": "ok",
        "service": "vct-hub",
        "version": env!("CARGO_PKG_VERSION"),
        "build_fingerprint": crate::identity::build_fingerprint(),
    }))
}

// ---------------------------------------------------------------------------
// App registry
// ---------------------------------------------------------------------------

async fn list_apps(State(db): State<Db>) -> impl IntoResponse {
    match db::list_apps(&db) {
        Ok(apps) => Json(serde_json::json!({ "apps": apps })).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

async fn register_app(
    State(db): State<Db>,
    Json(reg): Json<db::AppRegistration>,
) -> impl IntoResponse {
    match db::register_app(&db, &reg) {
        Ok(()) => Json(serde_json::json!({ "ok": true, "app_id": reg.app_id })).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

async fn deregister_app(
    State(db): State<Db>,
    Path(app_id): Path<String>,
) -> impl IntoResponse {
    match db::deregister_app(&db, &app_id) {
        Ok(()) => Json(serde_json::json!({ "ok": true })).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

async fn heartbeat(
    State(db): State<Db>,
    Path(app_id): Path<String>,
) -> impl IntoResponse {
    match db::heartbeat_app(&db, &app_id) {
        Ok(()) => Json(serde_json::json!({ "ok": true })).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

// ---------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------

async fn send_message(
    State(db): State<Db>,
    Json(msg): Json<db::SendMessage>,
) -> impl IntoResponse {
    match db::send_message(&db, &msg) {
        Ok(id) => Json(serde_json::json!({ "ok": true, "message_id": id })).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

#[derive(Deserialize)]
struct PollQuery {
    topic: Option<String>,
    limit: Option<u32>,
}

async fn poll_messages(
    State(db): State<Db>,
    Path(recipient): Path<String>,
    Query(q): Query<PollQuery>,
) -> impl IntoResponse {
    let limit = q.limit.unwrap_or(50);
    match db::poll_messages(&db, &recipient, q.topic.as_deref(), limit) {
        Ok(msgs) => Json(serde_json::json!({ "messages": msgs, "count": msgs.len() })).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

async fn ack_message(
    State(db): State<Db>,
    Path(id): Path<i64>,
) -> impl IntoResponse {
    match db::ack_message(&db, id) {
        Ok(()) => Json(serde_json::json!({ "ok": true })).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

// ---------------------------------------------------------------------------
// Data catalog
// ---------------------------------------------------------------------------

async fn register_data(
    State(db): State<Db>,
    Json(entry): Json<db::DataEntry>,
) -> impl IntoResponse {
    match db::register_data(&db, &entry) {
        Ok(id) => Json(serde_json::json!({ "ok": true, "id": id })).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

#[derive(Deserialize)]
struct CatalogQuery {
    data_type: Option<String>,
    app_id: Option<String>,
}

async fn query_data_catalog(
    State(db): State<Db>,
    Query(q): Query<CatalogQuery>,
) -> impl IntoResponse {
    match db::query_data_catalog(&db, q.data_type.as_deref(), q.app_id.as_deref()) {
        Ok(entries) => Json(serde_json::json!({ "data": entries, "count": entries.len() })).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}
