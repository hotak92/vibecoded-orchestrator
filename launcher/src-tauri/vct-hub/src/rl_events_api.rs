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
//!   POST   /api/v1/rl/events/prune      — prune events by age / row-cap (RL-5 retention)
//!
//! Auth: standard hub.token bearer middleware applies (mounted INSIDE the
//! hub-wide `auth::require_auth` layer in `server.rs`).
//!
//! Soft-fail discipline: every error returns `Err(StatusCode + JSON envelope)`.
//! The Python writer treats non-2xx as data loss (no retry queue, no JSONL
//! fallback — per the locked decision 2026-06-04).

use axum::{
    extract::{DefaultBodyLimit, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};

use super::modules_api::LauncherDbHandle;

/// Explicit request-body cap for the RL-events ingest routes (16 MiB).
///
/// WHY THE LIMIT IS 16 MiB (deliberate, not axum's 2 MB default): a single RL
/// event carries the full v3 payload_json — a query embedding, per-node node
/// embeddings + near-chunk `linked_embs`, and (on citation events) the
/// answer-chunk embeddings. Under dual-write both embedding spaces are logged.
/// A wide code retrieval (many nodes × 2048-dim CodeSage vectors) or an
/// answer-heavy citation can legitimately exceed axum's 2 MB `Json` default,
/// which would 413 the POST and lose the label SILENTLY (the Python writer
/// soft-fails a rejected POST). Per the user rule "move the limit, never the
/// data": we raise the CAP to fit the real data rather than trimming the event.
///
/// Trust posture that makes 16 MiB safe: these routes are localhost-only
/// (the hub binds loopback by default), token-authed (the hub-wide
/// `auth::require_auth` bearer layer wraps this router in `server.rs`), and
/// written only by the trusted first-party MCP writer — not an open internet
/// surface where a large body cap would be a DoS vector.
///
/// The Python emitter (`telemetry_writer.py::_wrap_for_hub`) still carries a
/// client-side trim guard, but that is a PATHOLOGICAL-CASE BACKSTOP only —
/// its default cap sits just under THIS 16 MiB (see
/// `_HUB_PAYLOAD_MAX_BYTES_DEFAULT`). Normal events never approach either cap;
/// the trim exists so a genuinely pathological event degrades to a loud
/// warning + still-trainable core label rather than a silent 413.
const RL_EVENTS_MAX_BODY_BYTES: usize = 16 * 1024 * 1024;

pub fn router() -> Router<LauncherDbHandle> {
    Router::new()
        // The `/rl/events` method-router (POST + GET) gets the raised body
        // limit — the `.layer` wraps the whole MethodRouter, so GET shares the
        // 16 MiB cap too (harmless: GET bodies are ignored). `/count` and
        // `/prune` are separate routes and keep axum's default (their bodies are
        // tiny query/prune params).
        .route(
            "/rl/events",
            post(post_event)
                .get(list_events)
                .layer(DefaultBodyLimit::max(RL_EVENTS_MAX_BODY_BYTES)),
        )
        .route("/rl/events/count", get(count_events))
        .route("/rl/events/prune", post(prune_events))
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
    //
    // V52-M (v0.2.52): accept the new pre/post outcome event types alongside
    // the original retrieval/citation pair. Outcome events carry no embedding
    // context and have payload-shape that's distinct from retrieval/citation;
    // they're consumed by the offline RL trainer as the "label" half of
    // (retrieval, outcome) training pairs. See:
    //   - claude_mcp_servers/rl_client/outcome_emit.py (writer side)
    //   - templates/hooks/post-bash-context-record.{sh,ps1}
    //   - templates/hooks/post-edit-outcome.{sh,ps1}
    //   - templates/hooks/pre-bash-context-inject.{sh,ps1}
    let allowed_event_types = ["retrieval", "citation", "bash_outcome", "edit_outcome", "pre_bash"];
    if !allowed_event_types.contains(&body.event_type.as_str()) {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({
                "error": {
                    "code": "bad_event_type",
                    "message": "event_type must be one of 'retrieval', 'citation', 'bash_outcome', 'edit_outcome', 'pre_bash'",
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
            tracing::error!(error = %e, "[vct-hub] post_rl_event insert failed");
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
    /// RL-14 (v0.2.75): quarantined rows are EXCLUDED by default — this GET
    /// is the offline trainer's read path and poisoned rows must never
    /// re-enter the corpus silently. Inspection surfaces opt in explicitly.
    #[serde(default)]
    pub include_quarantined: Option<bool>,
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
    /// RL-14: unix-ms when the row was quarantined; null = clean.
    pub quarantined_at: Option<i64>,
    /// RL-14: stable machine tag (e.g. `score_out_of_range`).
    pub quarantine_reason: Option<String>,
}

impl From<vct_launcher_core::db::rl_events::RlEvent> for RlEventOut {
    fn from(e: vct_launcher_core::db::rl_events::RlEvent) -> Self {
        RlEventOut {
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
            quarantined_at: e.quarantined_at,
            quarantine_reason: e.quarantine_reason,
        }
    }
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
        q.include_quarantined.unwrap_or(false),
    ) {
        Ok(rows) => {
            let out: Vec<RlEventOut> = rows.into_iter().map(RlEventOut::from).collect();
            (StatusCode::OK, Json(out)).into_response()
        }
        Err(e) => {
            tracing::error!(error = %e, "[vct-hub] list_rl_events failed");
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
    /// RL-14 (v0.2.75): absent → count ALL rows (the Identity-tab badge's
    /// pre-RL-14 semantics); `true` → only quarantined rows (rl-doctor's
    /// report); `false` → only clean rows.
    #[serde(default)]
    pub quarantined: Option<bool>,
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
        q.quarantined,
    ) {
        Ok(n) => (StatusCode::OK, Json(CountEventsResponse { count: n })).into_response(),
        Err(e) => {
            tracing::error!(error = %e, "[vct-hub] count_rl_events failed");
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

// ─── POST /api/v1/rl/events/prune ──────────────────────────────────────

/// Prune-request body (RL-5 retention). ALL fields optional — the Python
/// retention driver omits fields it isn't bounding on.
///
///   * `cutoff_ms` — delete events with `ts < cutoff_ms` (age bound).
///   * `max_rows`  — keep only the newest `max_rows` rows (row-cap bound).
///   * `project_id` — scope the prune to one project; absent → all projects.
///
/// An empty body `{}` (all None) is a valid no-op: the DB method deletes
/// nothing and returns 0. This matches the writer's soft-fail contract and
/// guards against ever wiping the corpus on a misfired prune.
#[derive(Debug, Deserialize)]
pub struct PruneEventsBody {
    #[serde(default)]
    pub cutoff_ms: Option<i64>,
    #[serde(default)]
    pub max_rows: Option<i64>,
    #[serde(default)]
    pub project_id: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct PruneEventsResponse {
    pub ok: bool,
    pub deleted: u64,
}

/// Serializes prune passes issued through THIS hub (MINOR-2, wave-4).
///
/// `prune_rl_events` deliberately drops the DB lock between selecting its
/// victims and deleting them (a multi-MB gzip + fsync must not block the
/// launcher's other DB users for its whole duration). Two near-simultaneous
/// POSTs — two writer processes each past their own per-process hourly throttle
/// — could therefore select the SAME victims and both publish a sidecar for
/// them, since the loser's delete-by-id removes 0 rows and 0 is not an error.
/// The trainer would then read those rows twice.
///
/// Belt one: passes through this hub simply never overlap. Belt two lives in
/// `prune_rl_events` itself (a pass that deleted nothing discards its own
/// sidecar) and is the one that holds for a prune issued by a DIFFERENT
/// process, which no in-process mutex can reach.
static PRUNE_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

async fn prune_events(
    State(h): State<LauncherDbHandle>,
    Json(body): Json<PruneEventsBody>,
) -> impl IntoResponse {
    // Held for the whole archive-then-delete pass, released at end of handler.
    let _serialize = PRUNE_LOCK.lock().await;
    // R1 (v0.2.91): the prune is ARCHIVE-THEN-DELETE. The archive directory is
    // resolved HERE, hub-side (`$RL_EVENTS_ARCHIVE_DIR`, else
    // `<VCT_STATE_DIR or ~/.vct>/rl_archive`) and NOT taken from the request
    // body — a caller-supplied path would be an arbitrary-write surface on an
    // authed localhost route, and the deletion authority must not depend on a
    // caller remembering to name an archive. A failed archive makes
    // `prune_rl_events` return Err → this handler 500s and NOTHING is deleted.
    let archive_dir = vct_launcher_core::db::rl_events::rl_archive_dir();
    match h.0.prune_rl_events(
        body.cutoff_ms,
        body.max_rows,
        body.project_id.as_deref(),
        &archive_dir,
    ) {
        Ok(deleted) => (
            StatusCode::OK,
            Json(PruneEventsResponse { ok: true, deleted }),
        )
            .into_response(),
        Err(e) => {
            tracing::error!(error = %e, "[vct-hub] prune_rl_events failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({
                    "error": {
                        "code": "prune_failed",
                        "message": "rl_events prune failed",
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

    /// R1 (v0.2.91): pin `$RL_EVENTS_ARCHIVE_DIR` at a temp dir for the life of
    /// a prune test so the hermetic suite never deposits a retention sidecar in
    /// the developer's real `~/.vct/rl_archive`. `--test-threads=1` (pinned by
    /// `scripts/test-keychain-safe.sh`) makes the process-global env mutation
    /// safe here. Restores the prior value on drop.
    struct ArchiveDirGuard {
        _dir: tempfile::TempDir,
        prev: Option<String>,
        path: std::path::PathBuf,
    }

    impl ArchiveDirGuard {
        fn new() -> Self {
            let dir = tempfile::tempdir().expect("temp archive dir");
            let key = vct_launcher_core::db::rl_events::RL_ARCHIVE_DIR_ENV;
            let prev = std::env::var(key).ok();
            std::env::set_var(key, dir.path());
            let path = dir.path().to_path_buf();
            Self { _dir: dir, prev, path }
        }

        /// Published (non-`.pending`) archive sidecars currently in the dir.
        fn published(&self) -> Vec<std::path::PathBuf> {
            let suffix = vct_launcher_core::db::rl_events::RL_ARCHIVE_SUFFIX;
            std::fs::read_dir(&self.path)
                .map(|entries| {
                    entries
                        .flatten()
                        .map(|e| e.path())
                        .filter(|p| {
                            p.file_name()
                                .map(|n| n.to_string_lossy().ends_with(suffix))
                                .unwrap_or(false)
                        })
                        .collect()
                })
                .unwrap_or_default()
        }
    }

    impl Drop for ArchiveDirGuard {
        fn drop(&mut self) {
            let key = vct_launcher_core::db::rl_events::RL_ARCHIVE_DIR_ENV;
            match &self.prev {
                Some(v) => std::env::set_var(key, v),
                None => std::env::remove_var(key),
            }
        }
    }

    /// Post `n` events with monotonically increasing ts, all `event_type`
    /// retrieval, no project scope. Returns nothing; caller re-queries count.
    async fn seed_events(base: &str, client: &reqwest::Client, n: i64) {
        for i in 0..n {
            let body = serde_json::json!({
                "event_type": "retrieval",
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
    }

    async fn count(base: &str, client: &reqwest::Client) -> i64 {
        let resp = client
            .get(format!("{}/rl/events/count", base))
            .send()
            .await
            .unwrap();
        let v: serde_json::Value = resp.json().await.unwrap();
        v["count"].as_i64().unwrap()
    }

    #[tokio::test]
    async fn prune_cutoff_deletes_and_reports_count() {
        let archive = ArchiveDirGuard::new();
        let base = spawn_test_hub().await;
        let client = reqwest::Client::new();
        // ts values 1000..1004.
        seed_events(&base, &client, 5).await;
        assert_eq!(count(&base, &client).await, 5);

        // Cutoff 1003 → delete ts < 1003 (ts 1000,1001,1002 = 3 rows).
        let resp = client
            .post(format!("{}/rl/events/prune", base))
            .json(&serde_json::json!({ "cutoff_ms": 1003_i64 }))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), reqwest::StatusCode::OK);
        let v: serde_json::Value = resp.json().await.unwrap();
        assert_eq!(v["ok"], true);
        assert_eq!(v["deleted"], 3);
        assert_eq!(count(&base, &client).await, 2);
        // R1: the route archives BEFORE deleting — a published sidecar must
        // exist for the rows that just left the table.
        assert_eq!(
            archive.published().len(),
            1,
            "prune route must publish exactly one archive sidecar, found {:?}",
            archive.published()
        );
    }

    #[tokio::test]
    async fn prune_max_rows_keeps_newest() {
        let archive = ArchiveDirGuard::new();
        let base = spawn_test_hub().await;
        let client = reqwest::Client::new();
        seed_events(&base, &client, 5).await;

        let resp = client
            .post(format!("{}/rl/events/prune", base))
            .json(&serde_json::json!({ "max_rows": 2_i64 }))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), reqwest::StatusCode::OK);
        let v: serde_json::Value = resp.json().await.unwrap();
        assert_eq!(v["deleted"], 3);
        assert_eq!(count(&base, &client).await, 2);
        assert_eq!(archive.published().len(), 1, "row-cap prune must archive too");
    }

    /// MINOR-2 (wave-4): two prune POSTs that arrive together must not both
    /// archive the same rows. `prune_rl_events` drops the DB lock for the
    /// gzip+fsync, so without serialization both passes could select the same
    /// victims and publish a sidecar each — the loser's delete removing 0 rows
    /// is `Ok`, not an error — and the trainer would count those rows twice.
    ///
    /// The invariant asserted is the OUTCOME one: whatever the interleaving, the
    /// rows leave the table exactly once and exactly one sidecar carries them.
    #[tokio::test]
    async fn concurrent_prunes_publish_one_sidecar_not_two() {
        let archive = ArchiveDirGuard::new();
        let base = spawn_test_hub().await;
        let client = reqwest::Client::new();
        seed_events(&base, &client, 6).await;
        assert_eq!(count(&base, &client).await, 6);

        let body = serde_json::json!({ "cutoff_ms": 10_000_i64 });
        let first = client
            .post(format!("{}/rl/events/prune", base))
            .json(&body)
            .send();
        let second = client
            .post(format!("{}/rl/events/prune", base))
            .json(&body)
            .send();
        let (a, b) = tokio::join!(first, second);
        let (a, b) = (a.unwrap(), b.unwrap());
        assert_eq!(a.status(), reqwest::StatusCode::OK);
        assert_eq!(b.status(), reqwest::StatusCode::OK);
        let va: serde_json::Value = a.json().await.unwrap();
        let vb: serde_json::Value = b.json().await.unwrap();

        let total = va["deleted"].as_u64().unwrap() + vb["deleted"].as_u64().unwrap();
        assert_eq!(total, 6, "every row must be deleted exactly once");
        assert_eq!(count(&base, &client).await, 0);
        assert_eq!(
            archive.published().len(),
            1,
            "two sidecars for one victim set is the double-count, found {:?}",
            archive.published(),
        );
    }

    /// RL-14 (v0.2.75): quarantined rows are excluded from the trainer's GET
    /// by default, visible with include_quarantined=true, and countable via
    /// quarantined=true (rl-doctor's read).
    #[tokio::test]
    async fn quarantined_rows_excluded_from_default_list_and_countable() {
        // Spawn with a SHARED handle (unlike `spawn_test_hub`) so the test
        // can run the Db-side marking pass directly — marking is not a route.
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
        let base = format!("http://{}/api/v1", addr);
        let client = reqwest::Client::new();

        for (task, payload) in [
            ("poisoned", r#"{"nodes":[{"title":"A","score":10.37}]}"#),
            ("clean", r#"{"nodes":[{"title":"A","score":0.9}]}"#),
        ] {
            let body = serde_json::json!({
                "event_type": "retrieval",
                "schema_version": 3,
                "ts_ms": 1_000,
                "task_id": task,
                "payload_json": payload,
            });
            let resp = client
                .post(format!("{}/rl/events", base))
                .json(&body)
                .send()
                .await
                .unwrap();
            assert_eq!(resp.status(), reqwest::StatusCode::OK);
        }

        // Mark the historical poison class.
        let marked = handle.0.backfill_quarantine_out_of_range(7_777).unwrap();
        assert_eq!(marked, 1);

        // Default GET (training read): only the clean row.
        let rows: serde_json::Value = client
            .get(format!("{}/rl/events?limit=10", base))
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        let arr = rows.as_array().unwrap();
        assert_eq!(arr.len(), 1);
        assert_eq!(arr[0]["task_id"], "clean");

        // Inspection GET: both rows, with the marker fields populated.
        let rows: serde_json::Value = client
            .get(format!("{}/rl/events?limit=10&include_quarantined=true", base))
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        let arr = rows.as_array().unwrap();
        assert_eq!(arr.len(), 2);
        let poisoned = arr
            .iter()
            .find(|r| r["task_id"] == "poisoned")
            .expect("poisoned row visible on inspection read");
        assert_eq!(poisoned["quarantined_at"], 7_777);
        assert_eq!(poisoned["quarantine_reason"], "score_out_of_range");

        // Count: quarantined=true (rl-doctor) / absent (badge, all rows).
        let v: serde_json::Value = client
            .get(format!("{}/rl/events/count?quarantined=true", base))
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        assert_eq!(v["count"], 1);
        let v: serde_json::Value = client
            .get(format!("{}/rl/events/count", base))
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        assert_eq!(v["count"], 2, "badge semantics unchanged: all rows");
    }

    /// Item-1 (WP-Q): the RL-events POST route carries an EXPLICIT 16 MiB body
    /// limit — a legitimately large event (embedding-heavy payload_json) that
    /// exceeds axum's 2 MB `Json` default must be ACCEPTED, while a genuinely
    /// pathological body over 16 MiB is rejected with 413 (never silently). We
    /// build the oversized payload_json with a large filler string so the
    /// SERIALIZED request body crosses the boundary being tested.
    #[tokio::test]
    async fn post_event_accepts_over_2mib_and_rejects_over_16mib() {
        let base = spawn_test_hub().await;
        let client = reqwest::Client::new();

        // Helper: build a POST body whose payload_json contains a filler of
        // `payload_bytes` bytes, so the serialized request body is ~that size
        // plus the small envelope overhead.
        let make_body = |payload_bytes: usize| {
            let filler = "x".repeat(payload_bytes);
            // Valid JSON payload_json string carrying the filler in a field.
            let payload_json = format!(r#"{{"event":"retrieval","filler":"{}"}}"#, filler);
            serde_json::json!({
                "event_type": "retrieval",
                "schema_version": 3,
                "ts_ms": 1_700_000_000_000_i64,
                "task_id": "big-event",
                "payload_json": payload_json,
            })
        };

        // ~3 MiB body: over axum's 2 MB default, well under 16 MiB → ACCEPTED.
        let resp = client
            .post(format!("{}/rl/events", base))
            .json(&make_body(3 * 1024 * 1024))
            .send()
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            reqwest::StatusCode::OK,
            "a ~3 MiB event (>2 MB axum default, <16 MiB cap) must be accepted, \
             not 413'd — moving the limit, not the data"
        );

        // ~17 MiB body: over the explicit 16 MiB cap → 413 Payload Too Large.
        let resp = client
            .post(format!("{}/rl/events", base))
            .json(&make_body(17 * 1024 * 1024))
            .send()
            .await
            .unwrap();
        assert_eq!(
            resp.status(),
            reqwest::StatusCode::PAYLOAD_TOO_LARGE,
            "a >16 MiB body must be rejected with 413 (a loud cap, never silent \
             data loss beyond the deliberate limit)"
        );
    }

    #[tokio::test]
    async fn prune_empty_body_is_noop_returns_200_deleted_zero() {
        // The critical safety round-trip: `{}` must NOT delete anything.
        let archive = ArchiveDirGuard::new();
        let base = spawn_test_hub().await;
        let client = reqwest::Client::new();
        seed_events(&base, &client, 4).await;

        let resp = client
            .post(format!("{}/rl/events/prune", base))
            .json(&serde_json::json!({}))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), reqwest::StatusCode::OK);
        let v: serde_json::Value = resp.json().await.unwrap();
        assert_eq!(v["ok"], true);
        assert_eq!(v["deleted"], 0);
        // Corpus untouched.
        assert_eq!(count(&base, &client).await, 4);
        // R1: a no-op prune deletes nothing, so it must also archive nothing —
        // an empty sidecar per hourly no-op pass would litter the archive dir.
        assert!(
            archive.published().is_empty(),
            "no-op prune must not publish an archive sidecar, found {:?}",
            archive.published()
        );
    }
}
