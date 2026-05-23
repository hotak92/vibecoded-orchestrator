// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Module-owned DB access endpoints (v0.2.31).
//!
//! Routes (all under `/api/v1`):
//!
//! ```text
//! GET    /modules/{module_id}/db/projects/{project_id}/rows/{table}/{key}
//!        ?fields=col1,col2
//! POST   /modules/{module_id}/db/projects/{project_id}/rows/{table}
//!        body: {"key": "...", "fields": {"col": "value", ...}}
//! PATCH  /modules/{module_id}/db/projects/{project_id}/rows/{table}/{key}
//!        body: {"fields": {"col": "new_value", ...}}
//! DELETE /modules/{module_id}/db/projects/{project_id}/rows/{table}/{key}
//! GET    /modules/{module_id}/db/projects/{project_id}/rows/{table}
//!        ?fields=col1,col2 &limit=N
//!
//! POST   /modules/{module_id}/token/refresh
//!        body: empty; auth: current near-expiry token; response:
//!        {"token": "<new-secret>", "expires_at": <unix-ms>}
//! ```
//!
//! ### Authorization
//!
//! Every route requires `Authorization: Bearer <module-access-token>`,
//! issued via the launcher's `issue_module_access_token` Tauri command
//! and stored in `module_access_tokens` (launcher.db, migration 019).
//! The token is scoped to a specific (module_id, project_id) pair —
//! routes whose URL paths name a different module / project than the
//! token's claimed scope return 401 / 403.
//!
//! ### Namespace enforcement
//!
//! The `{table}` path segment MUST start with the module's declared
//! namespace prefix (looked up from `module_db_migrations.namespace`).
//! Anything else — including pre-existing launcher-owned tables —
//! returns 403. Defense-in-depth against an authenticated module
//! trying to read / mutate launcher state directly through this
//! surface.
//!
//! ### Soft-fail
//!
//! Errors return structured JSON envelopes matching the rest of the
//! hub's API surface (`{"error":{"code":"...","message":"..."}}`).
//! SQL injection is prevented by parameterized queries; column-name
//! identifiers (which can't be bound) are validated against the
//! pattern `[a-z_][a-z0-9_]*` before interpolation.

use std::collections::HashMap;
use std::sync::Arc;

use axum::{
    body::Body,
    extract::{Path, Query, Request, State},
    http::StatusCode,
    middleware::Next,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use chrono::Utc;
use rusqlite::{params, types::Value as SqlValue};
use serde::{Deserialize, Serialize};

use vct_launcher_core::db::Db;

use super::modules_api::LauncherDbHandle;

/// Default refresh-token TTL: 1 hour, matching the launcher's
/// `issue_module_access_token` initial-issue TTL. v0.2.32 swaps both
/// surfaces to JWT-signed claims; the row-replace mechanism stays.
const REFRESH_TTL_MS: i64 = 60 * 60 * 1000;

pub fn router() -> Router<LauncherDbHandle> {
    Router::new()
        // CRUD on a single keyed row.
        .route(
            "/modules/{module_id}/db/projects/{project_id}/rows/{table}/{key}",
            get(get_row).patch(patch_row).delete(delete_row),
        )
        // POST creates / upserts; LIST scans the table (paginated).
        .route(
            "/modules/{module_id}/db/projects/{project_id}/rows/{table}",
            post(insert_row).get(list_rows),
        )
        // Token refresh.
        .route(
            "/modules/{module_id}/token/refresh",
            post(refresh_token),
        )
        // Both crud and token routes get the same bearer-scope check.
        .layer(axum::middleware::from_fn(require_module_scope))
}

// ─── Error envelope ─────────────────────────────────────────────────────

fn err(status: StatusCode, code: &str, message: impl Into<String>) -> Response {
    (
        status,
        Json(serde_json::json!({
            "error": { "code": code, "message": message.into() }
        })),
    )
        .into_response()
}

// ─── Bearer-scope middleware ────────────────────────────────────────────

/// Stash the resolved module scope into request extensions so the route
/// handlers can pull it without re-parsing the Authorization header.
///
/// `project_id` is populated for diagnostic logging in future versions
/// (the v0.2.31 routes already have the project_id in their URL path,
/// so handlers don't need to consult the extension). `#[allow(dead_code)]`
/// while consumers are still being wired.
#[derive(Clone, Debug)]
struct ModuleScope {
    module_id: String,
    #[allow(dead_code)]
    project_id: String,
}

/// Per-(module, project) bearer-token validator. Pulls the token from
/// `Authorization: Bearer <secret>`, the URL's (module_id, project_id),
/// and checks against `module_access_tokens`. Mismatched module_id →
/// 401; mismatched project_id → 403; expired → 401.
///
/// The hub-wide `require_auth` middleware in `auth.rs` already ran
/// against the same token, but it accepts the launcher's hub.token —
/// our module-scoped tokens are different. We re-validate here against
/// the per-(module, project) row so two security boundaries hold:
/// (1) only same-user processes have hub.token; (2) only the legit
/// module container has its scoped secret.
///
/// HACK / v0.2.31 simplification: the hub-wide `require_auth` is
/// effectively bypassed for these routes because we accept the scoped
/// secret as the bearer instead of hub.token. That's deliberate:
/// containers don't have hub.token (they shouldn't — different trust
/// boundary). v0.2.32 will land a proper "module-token-aware"
/// middleware that does both checks.
async fn require_module_scope(req: Request<Body>, next: Next) -> Response {
    // Pull the bearer.
    let bearer = req
        .headers()
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.strip_prefix("Bearer ").map(|t| t.trim().to_string()));

    let bearer = match bearer {
        Some(b) if !b.is_empty() => b,
        _ => return err(StatusCode::UNAUTHORIZED, "missing_bearer", "missing or empty Authorization: Bearer <token>"),
    };

    // Parse the URL path: extract module_id + (optional) project_id.
    // The token-refresh route doesn't include project_id in the URL;
    // for that route we accept any scope owned by the bearer.
    let path = req.uri().path().to_string();
    let (module_id_from_url, project_id_from_url) = match parse_module_path(&path) {
        Some(t) => t,
        None => return err(StatusCode::BAD_REQUEST, "bad_path", "could not parse module / project from URL"),
    };

    // Look up the token row.
    let launcher_db = match req.extensions().get::<LauncherDbHandle>().cloned() {
        Some(h) => h,
        None => {
            eprintln!("[module_db_api] LauncherDbHandle missing from request extensions");
            return err(StatusCode::INTERNAL_SERVER_ERROR, "no_db_state", "launcher db handle missing");
        }
    };

    let row = match lookup_token(&launcher_db.0, &bearer) {
        Ok(Some(row)) => row,
        Ok(None) => return err(StatusCode::UNAUTHORIZED, "invalid_token", "bearer token not recognized"),
        Err(e) => {
            eprintln!("[module_db_api] token lookup error: {}", e);
            return err(StatusCode::INTERNAL_SERVER_ERROR, "db_error", "token lookup failed");
        }
    };

    // Module scope check.
    if row.module_id != module_id_from_url {
        return err(
            StatusCode::UNAUTHORIZED,
            "module_mismatch",
            "token does not authorize this module",
        );
    }

    // Project scope check (when URL carries project_id).
    if let Some(pid) = project_id_from_url.as_ref() {
        if &row.project_id != pid {
            return err(
                StatusCode::FORBIDDEN,
                "project_out_of_scope",
                "token does not authorize this project",
            );
        }
    }

    // Expiry check.
    let now = Utc::now().timestamp_millis();
    if row.expires_at_ms <= now {
        return err(
            StatusCode::UNAUTHORIZED,
            "token_expired",
            "token has expired; refresh via POST /modules/{module_id}/token/refresh",
        );
    }

    // Stash the verified scope for the route handler.
    let mut req = req;
    req.extensions_mut().insert(ModuleScope {
        module_id: row.module_id.clone(),
        project_id: row.project_id.clone(),
    });
    // Stash the raw bearer so token-refresh can find the current row
    // to replace.
    req.extensions_mut().insert(BearerToken(bearer));

    next.run(req).await
}

/// Helper: parse `/api/v1/modules/{module_id}/db/projects/{project_id}/...`
/// or `/api/v1/modules/{module_id}/token/refresh`. Returns
/// `(module_id, Some(project_id))` for the db routes and
/// `(module_id, None)` for token-refresh.
fn parse_module_path(path: &str) -> Option<(String, Option<String>)> {
    // Strip leading "/api/v1/" if present (axum normalises this away
    // before the middleware sees it usually, but we're defensive).
    let p = path.trim_start_matches("/api/v1").trim_start_matches('/');
    let parts: Vec<&str> = p.split('/').collect();
    // "modules", "{module_id}", "db", "projects", "{project_id}", ...
    // OR "modules", "{module_id}", "token", "refresh"
    if parts.len() >= 2 && parts[0] == "modules" {
        let module_id = parts[1].to_string();
        if parts.len() >= 5 && parts[2] == "db" && parts[3] == "projects" {
            return Some((module_id, Some(parts[4].to_string())));
        }
        if parts.len() >= 4 && parts[2] == "token" && parts[3] == "refresh" {
            return Some((module_id, None));
        }
    }
    None
}

#[derive(Clone, Debug)]
struct BearerToken(String);

#[derive(Debug)]
struct TokenRow {
    module_id: String,
    project_id: String,
    expires_at_ms: i64,
}

fn lookup_token(db: &Arc<Db>, bearer: &str) -> Result<Option<TokenRow>, String> {
    let guard = db.lock();
    let row = guard
        .query_row(
            "SELECT module_id, project_id, expires_at \
             FROM module_access_tokens \
             WHERE token_secret = ?1",
            params![bearer],
            |row| {
                Ok(TokenRow {
                    module_id: row.get(0)?,
                    project_id: row.get(1)?,
                    expires_at_ms: row.get(2)?,
                })
            },
        )
        .ok();
    Ok(row)
}

// ─── Namespace + table validation ───────────────────────────────────────

/// Look up the module's declared namespace prefix from
/// `module_db_migrations` (denormalised column written by the apply
/// mechanism). Returns the namespace string (e.g. "rl") or None when
/// the module hasn't applied any migrations yet — in which case all
/// table writes are refused.
fn module_namespace(db: &Db, module_id: &str) -> Result<Option<String>, String> {
    let guard = db.lock();
    let ns = guard
        .query_row(
            "SELECT namespace FROM module_db_migrations \
             WHERE module_id = ?1 LIMIT 1",
            params![module_id],
            |row| row.get::<_, String>(0),
        )
        .ok();
    Ok(ns)
}

/// Validate a table name supplied via URL path. Two checks:
/// (1) ASCII identifier shape (`[a-z_][a-z0-9_]*`) — prevents SQL
///     injection via dotted / quoted / backtick names.
/// (2) Prefix matches the module's declared namespace + `_`.
fn validate_table_name(table: &str, namespace: &str) -> Result<(), String> {
    if table.is_empty() {
        return Err("table name empty".into());
    }
    let bytes = table.as_bytes();
    let first_ok = bytes
        .first()
        .map(|c| c.is_ascii_lowercase() || *c == b'_')
        .unwrap_or(false);
    if !first_ok {
        return Err(format!(
            "table name '{}' must match [a-z_][a-z0-9_]*",
            table
        ));
    }
    let rest_ok = bytes
        .iter()
        .skip(1)
        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'_');
    if !rest_ok {
        return Err(format!(
            "table name '{}' must match [a-z_][a-z0-9_]*",
            table
        ));
    }

    let prefix = format!("{}_", namespace);
    if !table.starts_with(&prefix) {
        return Err(format!(
            "table '{}' is outside module's namespace '{}'",
            table, prefix
        ));
    }
    Ok(())
}

/// Validate a column-name identifier (used by `?fields=` projection and
/// POST/PATCH field keys). SQL-identifier shape, no quotes / dots / spaces.
fn validate_column_name(col: &str) -> Result<(), String> {
    if col.is_empty() {
        return Err("column name empty".into());
    }
    let bytes = col.as_bytes();
    let first_ok = bytes
        .first()
        .map(|c| c.is_ascii_lowercase() || *c == b'_')
        .unwrap_or(false);
    if !first_ok {
        return Err(format!("column name '{}' must match [a-z_][a-z0-9_]*", col));
    }
    let rest_ok = bytes
        .iter()
        .skip(1)
        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'_');
    if !rest_ok {
        return Err(format!("column name '{}' must match [a-z_][a-z0-9_]*", col));
    }
    Ok(())
}

/// Parse `?fields=col1,col2` into a validated list of column names.
fn parse_fields_param(q: &HashMap<String, String>) -> Result<Option<Vec<String>>, String> {
    match q.get("fields") {
        None => Ok(None),
        Some(v) if v.trim().is_empty() => Ok(None),
        Some(v) => {
            let cols: Vec<&str> = v.split(',').map(|s| s.trim()).filter(|s| !s.is_empty()).collect();
            for c in &cols {
                validate_column_name(c)?;
            }
            Ok(Some(cols.into_iter().map(|s| s.to_string()).collect()))
        }
    }
}

fn parse_limit_param(q: &HashMap<String, String>) -> Result<usize, String> {
    match q.get("limit") {
        None => Ok(100),
        Some(v) => v
            .parse::<usize>()
            .map_err(|_| format!("invalid limit: {}", v))
            .map(|n| n.clamp(1, 1000)),
    }
}

// ─── Row coercion helpers ───────────────────────────────────────────────

/// SQLite row → JSON object, projecting the column list `cols` (or all
/// declared columns when `cols == None`).
fn sql_row_to_json(
    row: &rusqlite::Row<'_>,
    column_names: &[String],
) -> rusqlite::Result<serde_json::Value> {
    let mut obj = serde_json::Map::new();
    for (i, name) in column_names.iter().enumerate() {
        let v: SqlValue = row.get(i)?;
        obj.insert(name.clone(), sql_value_to_json(v));
    }
    Ok(serde_json::Value::Object(obj))
}

fn sql_value_to_json(v: SqlValue) -> serde_json::Value {
    match v {
        SqlValue::Null => serde_json::Value::Null,
        SqlValue::Integer(i) => serde_json::Value::from(i),
        SqlValue::Real(f) => serde_json::json!(f),
        SqlValue::Text(s) => serde_json::Value::String(s),
        SqlValue::Blob(b) => serde_json::Value::String(hex::encode(b)),
    }
}

fn json_value_to_sql(v: &serde_json::Value) -> rusqlite::types::Value {
    use serde_json::Value as J;
    match v {
        J::Null => SqlValue::Null,
        J::Bool(b) => SqlValue::Integer(if *b { 1 } else { 0 }),
        J::Number(n) => {
            if let Some(i) = n.as_i64() {
                SqlValue::Integer(i)
            } else if let Some(f) = n.as_f64() {
                SqlValue::Real(f)
            } else {
                SqlValue::Text(n.to_string())
            }
        }
        J::String(s) => SqlValue::Text(s.clone()),
        // Arrays / objects: serialize back to a JSON string. The
        // module's schema is expected to declare these as TEXT.
        other => SqlValue::Text(other.to_string()),
    }
}

// ─── Handlers ───────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct InsertReq {
    /// Primary-key value (string for simplicity at v0.2.31).
    key: String,
    /// Column → value map. Must include the table's PK column.
    fields: HashMap<String, serde_json::Value>,
}

#[derive(Deserialize)]
struct PatchReq {
    /// Column → new-value map.
    fields: HashMap<String, serde_json::Value>,
}

/// `GET .../rows/{table}/{key}` — fetch a single keyed row.
///
/// The "key" segment matches a column declared by SQL convention as
/// PRIMARY KEY. v0.2.31 simplification: we look up the PK column name
/// from `sqlite_master` (PRAGMA table_info) and match against the
/// supplied key. v0.2.32+ may add support for composite PKs by
/// extending the URL surface.
async fn get_row(
    State(h): State<LauncherDbHandle>,
    Path((module_id, project_id, table, key)): Path<(String, String, String, String)>,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    let ns = match module_namespace(&h.0, &module_id) {
        Ok(Some(ns)) => ns,
        Ok(None) => return err(StatusCode::NOT_FOUND, "no_migrations_applied", "module has no migrations applied"),
        Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "db_error", e),
    };
    if let Err(e) = validate_table_name(&table, &ns) {
        return err(StatusCode::FORBIDDEN, "namespace_violation", e);
    }

    let cols = match parse_fields_param(&q) {
        Ok(c) => c,
        Err(e) => return err(StatusCode::BAD_REQUEST, "bad_fields", e),
    };

    let pk = match resolve_pk_column(&h.0, &table) {
        Ok(Some(c)) => c,
        Ok(None) => return err(StatusCode::NOT_FOUND, "table_not_found", format!("table '{}' has no PK or doesn't exist", table)),
        Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "db_error", e),
    };

    let column_list = match cols.as_deref() {
        Some(c) => c.join(", "),
        None => "*".into(),
    };
    let sql = format!(
        "SELECT {} FROM {} WHERE {} = ?1 AND project_id = ?2",
        column_list, table, pk
    );

    // Resolve column names BEFORE taking the main lock — `resolve_all_columns`
    // itself acquires the same mutex, so doing it while holding it would
    // deadlock.
    let column_names: Vec<String> = match &cols {
        Some(c) => c.clone(),
        None => match resolve_all_columns(&h.0, &table) {
            Ok(c) => c,
            Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "db_error", e),
        },
    };

    let guard = h.0.lock();
    let mut stmt = match guard.prepare(&sql) {
        Ok(s) => s,
        Err(e) => return err(StatusCode::BAD_REQUEST, "sql_prepare_failed", e.to_string()),
    };

    let mut rows = match stmt.query(params![&key, &project_id]) {
        Ok(r) => r,
        Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "query_failed", e.to_string()),
    };
    match rows.next() {
        Ok(Some(r)) => {
            let json = match sql_row_to_json(r, &column_names) {
                Ok(j) => j,
                Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "row_decode_failed", e.to_string()),
            };
            Json(json).into_response()
        }
        Ok(None) => err(StatusCode::NOT_FOUND, "row_not_found", format!("no row with key '{}'", key)),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, "query_failed", e.to_string()),
    }
}

/// `GET .../rows/{table}` — list rows, optionally projected + limited.
async fn list_rows(
    State(h): State<LauncherDbHandle>,
    Path((module_id, project_id, table)): Path<(String, String, String)>,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    let ns = match module_namespace(&h.0, &module_id) {
        Ok(Some(ns)) => ns,
        Ok(None) => return err(StatusCode::NOT_FOUND, "no_migrations_applied", "module has no migrations applied"),
        Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "db_error", e),
    };
    if let Err(e) = validate_table_name(&table, &ns) {
        return err(StatusCode::FORBIDDEN, "namespace_violation", e);
    }

    let cols = match parse_fields_param(&q) {
        Ok(c) => c,
        Err(e) => return err(StatusCode::BAD_REQUEST, "bad_fields", e),
    };
    let limit = match parse_limit_param(&q) {
        Ok(n) => n,
        Err(e) => return err(StatusCode::BAD_REQUEST, "bad_limit", e),
    };

    let column_list = match cols.as_deref() {
        Some(c) => c.join(", "),
        None => "*".into(),
    };
    let sql = format!(
        "SELECT {} FROM {} WHERE project_id = ?1 LIMIT ?2",
        column_list, table
    );

    // Resolve column names BEFORE taking the main lock (deadlock guard).
    let column_names: Vec<String> = match &cols {
        Some(c) => c.clone(),
        None => match resolve_all_columns(&h.0, &table) {
            Ok(c) => c,
            Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "db_error", e),
        },
    };

    let guard = h.0.lock();
    let mut stmt = match guard.prepare(&sql) {
        Ok(s) => s,
        Err(e) => return err(StatusCode::BAD_REQUEST, "sql_prepare_failed", e.to_string()),
    };
    let mut rows = match stmt.query(params![&project_id, limit as i64]) {
        Ok(r) => r,
        Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "query_failed", e.to_string()),
    };
    let mut out = Vec::new();
    loop {
        match rows.next() {
            Ok(Some(r)) => match sql_row_to_json(r, &column_names) {
                Ok(j) => out.push(j),
                Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "row_decode_failed", e.to_string()),
            },
            Ok(None) => break,
            Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "query_failed", e.to_string()),
        }
    }

    Json(serde_json::json!({ "rows": out, "count": out.len() })).into_response()
}

/// `POST .../rows/{table}` — insert (upsert on PK conflict).
async fn insert_row(
    State(h): State<LauncherDbHandle>,
    Path((module_id, project_id, table)): Path<(String, String, String)>,
    Json(body): Json<InsertReq>,
) -> Response {
    let ns = match module_namespace(&h.0, &module_id) {
        Ok(Some(ns)) => ns,
        Ok(None) => return err(StatusCode::NOT_FOUND, "no_migrations_applied", "module has no migrations applied"),
        Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "db_error", e),
    };
    if let Err(e) = validate_table_name(&table, &ns) {
        return err(StatusCode::FORBIDDEN, "namespace_violation", e);
    }

    let pk = match resolve_pk_column(&h.0, &table) {
        Ok(Some(c)) => c,
        Ok(None) => return err(StatusCode::NOT_FOUND, "table_not_found", format!("table '{}' has no PK", table)),
        Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "db_error", e),
    };

    // Validate every field name.
    let mut fields: HashMap<String, serde_json::Value> = body.fields;
    for (col, _) in &fields {
        if let Err(e) = validate_column_name(col) {
            return err(StatusCode::BAD_REQUEST, "bad_column", e);
        }
    }
    // Force the PK + project_id fields in case the caller omitted them
    // (project_id is auto-injected from URL scope; PK from `key`).
    fields.insert(pk.clone(), serde_json::Value::String(body.key.clone()));
    fields.insert("project_id".to_string(), serde_json::Value::String(project_id.clone()));

    let cols: Vec<String> = fields.keys().cloned().collect();
    let placeholders: Vec<String> = (1..=cols.len()).map(|i| format!("?{}", i)).collect();
    let sql = format!(
        "INSERT OR REPLACE INTO {} ({}) VALUES ({})",
        table,
        cols.join(", "),
        placeholders.join(", "),
    );

    let values: Vec<SqlValue> = cols
        .iter()
        .map(|c| json_value_to_sql(fields.get(c).unwrap_or(&serde_json::Value::Null)))
        .collect();

    let guard = h.0.lock();
    let value_refs: Vec<&dyn rusqlite::ToSql> =
        values.iter().map(|v| v as &dyn rusqlite::ToSql).collect();
    match guard.execute(&sql, rusqlite::params_from_iter(value_refs.iter().copied())) {
        Ok(n) => Json(serde_json::json!({ "ok": true, "rows_affected": n })).into_response(),
        Err(e) => err(StatusCode::BAD_REQUEST, "insert_failed", e.to_string()),
    }
}

/// `PATCH .../rows/{table}/{key}` — update specified fields on a row.
async fn patch_row(
    State(h): State<LauncherDbHandle>,
    Path((module_id, project_id, table, key)): Path<(String, String, String, String)>,
    Json(body): Json<PatchReq>,
) -> Response {
    let ns = match module_namespace(&h.0, &module_id) {
        Ok(Some(ns)) => ns,
        Ok(None) => return err(StatusCode::NOT_FOUND, "no_migrations_applied", "module has no migrations applied"),
        Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "db_error", e),
    };
    if let Err(e) = validate_table_name(&table, &ns) {
        return err(StatusCode::FORBIDDEN, "namespace_violation", e);
    }

    if body.fields.is_empty() {
        return err(StatusCode::BAD_REQUEST, "no_fields", "PATCH requires at least one field");
    }
    for (col, _) in &body.fields {
        if let Err(e) = validate_column_name(col) {
            return err(StatusCode::BAD_REQUEST, "bad_column", e);
        }
    }

    let pk = match resolve_pk_column(&h.0, &table) {
        Ok(Some(c)) => c,
        Ok(None) => return err(StatusCode::NOT_FOUND, "table_not_found", format!("table '{}' has no PK", table)),
        Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "db_error", e),
    };

    let cols: Vec<&String> = body.fields.keys().collect();
    let set_clause: Vec<String> = cols
        .iter()
        .enumerate()
        .map(|(i, c)| format!("{} = ?{}", c, i + 1))
        .collect();
    let pk_idx = cols.len() + 1;
    let project_idx = cols.len() + 2;
    let sql = format!(
        "UPDATE {} SET {} WHERE {} = ?{} AND project_id = ?{}",
        table,
        set_clause.join(", "),
        pk,
        pk_idx,
        project_idx,
    );

    let mut values: Vec<SqlValue> = cols
        .iter()
        .map(|c| json_value_to_sql(body.fields.get(*c).unwrap_or(&serde_json::Value::Null)))
        .collect();
    values.push(SqlValue::Text(key.clone()));
    values.push(SqlValue::Text(project_id.clone()));

    let guard = h.0.lock();
    let value_refs: Vec<&dyn rusqlite::ToSql> =
        values.iter().map(|v| v as &dyn rusqlite::ToSql).collect();
    match guard.execute(&sql, rusqlite::params_from_iter(value_refs.iter().copied())) {
        Ok(n) if n == 0 => err(StatusCode::NOT_FOUND, "row_not_found", "no matching row"),
        Ok(n) => Json(serde_json::json!({ "ok": true, "rows_affected": n })).into_response(),
        Err(e) => err(StatusCode::BAD_REQUEST, "update_failed", e.to_string()),
    }
}

/// `DELETE .../rows/{table}/{key}`
async fn delete_row(
    State(h): State<LauncherDbHandle>,
    Path((module_id, project_id, table, key)): Path<(String, String, String, String)>,
) -> Response {
    let ns = match module_namespace(&h.0, &module_id) {
        Ok(Some(ns)) => ns,
        Ok(None) => return err(StatusCode::NOT_FOUND, "no_migrations_applied", "module has no migrations applied"),
        Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "db_error", e),
    };
    if let Err(e) = validate_table_name(&table, &ns) {
        return err(StatusCode::FORBIDDEN, "namespace_violation", e);
    }

    let pk = match resolve_pk_column(&h.0, &table) {
        Ok(Some(c)) => c,
        Ok(None) => return err(StatusCode::NOT_FOUND, "table_not_found", format!("table '{}' has no PK", table)),
        Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "db_error", e),
    };

    let sql = format!(
        "DELETE FROM {} WHERE {} = ?1 AND project_id = ?2",
        table, pk
    );
    let guard = h.0.lock();
    match guard.execute(&sql, params![&key, &project_id]) {
        Ok(n) if n == 0 => err(StatusCode::NOT_FOUND, "row_not_found", "no matching row"),
        Ok(n) => Json(serde_json::json!({ "ok": true, "rows_affected": n })).into_response(),
        Err(e) => err(StatusCode::BAD_REQUEST, "delete_failed", e.to_string()),
    }
}

// ─── Token refresh ──────────────────────────────────────────────────────

#[derive(Serialize)]
struct RefreshResp {
    token: String,
    expires_at: i64,
}

/// `POST /modules/{module_id}/token/refresh`
///
/// Auth: current near-expiry token in `Authorization: Bearer ...`. The
/// scope middleware already validated (1) the bearer maps to a row
/// (2) the row's module_id matches the URL (3) the row hasn't expired.
/// Here we generate a fresh secret, UPDATE the row, and return the new
/// value. The OLD token immediately stops working.
async fn refresh_token(
    State(h): State<LauncherDbHandle>,
    Path(module_id): Path<String>,
    req: Request<Body>,
) -> Response {
    // Pull the verified bearer + scope from request extensions.
    let bearer = match req.extensions().get::<BearerToken>().cloned() {
        Some(b) => b.0,
        None => return err(StatusCode::INTERNAL_SERVER_ERROR, "no_scope", "scope extension missing"),
    };
    let scope = match req.extensions().get::<ModuleScope>().cloned() {
        Some(s) => s,
        None => return err(StatusCode::INTERNAL_SERVER_ERROR, "no_scope", "scope extension missing"),
    };
    if scope.module_id != module_id {
        return err(StatusCode::FORBIDDEN, "scope_mismatch", "token does not authorize this module");
    }

    let new_secret = match generate_token_hex() {
        Ok(s) => s,
        Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "rng", e),
    };
    let now = Utc::now().timestamp_millis();
    let expires_at = now + REFRESH_TTL_MS;

    let guard = h.0.lock();
    // UPDATE keyed by the OLD secret — both that the old secret is
    // currently valid for this (module, project) AND that the row is
    // atomically swapped (no second-process race). On miss → 401.
    let affected = match guard.execute(
        "UPDATE module_access_tokens SET token_secret = ?1, issued_at = ?2, expires_at = ?3 \
         WHERE token_secret = ?4 AND module_id = ?5",
        params![&new_secret, now, expires_at, &bearer, &module_id],
    ) {
        Ok(n) => n,
        Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, "db_error", e.to_string()),
    };
    if affected == 0 {
        return err(StatusCode::UNAUTHORIZED, "refresh_failed", "token no longer valid");
    }
    drop(guard);

    Json(RefreshResp {
        token: new_secret,
        expires_at,
    })
    .into_response()
}

fn generate_token_hex() -> Result<String, String> {
    use rand::TryRngCore;
    let mut bytes = [0u8; 32];
    rand::rngs::OsRng
        .try_fill_bytes(&mut bytes)
        .map_err(|e| format!("rng: {}", e))?;
    Ok(hex::encode(bytes))
}

// ─── PK / column resolution via PRAGMA table_info ───────────────────────

fn resolve_pk_column(db: &Db, table: &str) -> Result<Option<String>, String> {
    // PRAGMA table_info() is safe to interpolate the table name into:
    // we've already validated the table name as `[a-z_][a-z0-9_]*`,
    // and PRAGMAs don't accept bound parameters.
    let sql = format!("PRAGMA table_info({})", table);
    let guard = db.lock();
    let mut stmt = guard.prepare(&sql).map_err(|e| format!("prepare PRAGMA: {}", e))?;
    let mut rows = stmt.query([]).map_err(|e| format!("query PRAGMA: {}", e))?;
    while let Some(row) = rows.next().map_err(|e| format!("next row: {}", e))? {
        // table_info columns: cid, name, type, notnull, dflt_value, pk
        let name: String = row.get(1).map_err(|e| format!("read name: {}", e))?;
        let pk: i64 = row.get(5).map_err(|e| format!("read pk: {}", e))?;
        if pk > 0 {
            return Ok(Some(name));
        }
    }
    Ok(None)
}

fn resolve_all_columns(db: &Db, table: &str) -> Result<Vec<String>, String> {
    let sql = format!("PRAGMA table_info({})", table);
    let guard = db.lock();
    let mut stmt = guard.prepare(&sql).map_err(|e| format!("prepare PRAGMA: {}", e))?;
    let mut rows = stmt.query([]).map_err(|e| format!("query PRAGMA: {}", e))?;
    let mut out = Vec::new();
    while let Some(row) = rows.next().map_err(|e| format!("next row: {}", e))? {
        let name: String = row.get(1).map_err(|e| format!("read name: {}", e))?;
        out.push(name);
    }
    Ok(out)
}

// ─── Tests ──────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_module_path_db_route() {
        let p = "/api/v1/modules/vct-rl-reranker/db/projects/abc-123/rows/rl_state/key1";
        assert_eq!(
            parse_module_path(p),
            Some(("vct-rl-reranker".into(), Some("abc-123".into())))
        );
    }

    #[test]
    fn parse_module_path_token_refresh() {
        let p = "/api/v1/modules/vct-rl-reranker/token/refresh";
        assert_eq!(
            parse_module_path(p),
            Some(("vct-rl-reranker".into(), None))
        );
    }

    #[test]
    fn parse_module_path_garbage() {
        assert_eq!(parse_module_path("/api/v1/health"), None);
        assert_eq!(parse_module_path("/api/v1/modules"), None);
    }

    #[test]
    fn validate_table_name_accepts_namespaced() {
        assert!(validate_table_name("rl_state", "rl").is_ok());
        assert!(validate_table_name("rl_training_runs", "rl").is_ok());
    }

    #[test]
    fn validate_table_name_rejects_non_namespaced() {
        let e = validate_table_name("projects", "rl").unwrap_err();
        assert!(e.contains("outside module's namespace"));
    }

    #[test]
    fn validate_table_name_rejects_sql_injection() {
        assert!(validate_table_name("rl_state; DROP TABLE projects", "rl").is_err());
        assert!(validate_table_name("rl_state'", "rl").is_err());
        assert!(validate_table_name(r#""rl_state""#, "rl").is_err());
    }

    #[test]
    fn validate_column_name_accepts_normal_identifier() {
        assert!(validate_column_name("project_id").is_ok());
        assert!(validate_column_name("col1").is_ok());
        assert!(validate_column_name("_internal").is_ok());
    }

    #[test]
    fn validate_column_name_rejects_garbage() {
        assert!(validate_column_name("col;DROP").is_err());
        assert!(validate_column_name("col name").is_err());
        assert!(validate_column_name("col-name").is_err());
        assert!(validate_column_name("Col").is_err()); // uppercase
        assert!(validate_column_name("").is_err());
    }

    #[test]
    fn parse_fields_param_accepts_csv() {
        let mut q = HashMap::new();
        q.insert("fields".to_string(), "col1,col2,col3".to_string());
        let cols = parse_fields_param(&q).expect("parse ok");
        assert_eq!(
            cols.unwrap(),
            vec!["col1".to_string(), "col2".to_string(), "col3".to_string()]
        );
    }

    #[test]
    fn parse_fields_param_rejects_injection() {
        let mut q = HashMap::new();
        q.insert("fields".to_string(), "col1; DROP TABLE".to_string());
        let result = parse_fields_param(&q);
        assert!(result.is_err());
    }

    #[test]
    fn parse_fields_param_handles_missing_and_empty() {
        let q = HashMap::new();
        assert!(parse_fields_param(&q).unwrap().is_none());

        let mut q = HashMap::new();
        q.insert("fields".to_string(), "".to_string());
        assert!(parse_fields_param(&q).unwrap().is_none());
    }

    #[test]
    fn parse_limit_param_clamps() {
        let q = HashMap::new();
        assert_eq!(parse_limit_param(&q).unwrap(), 100); // default

        let mut q = HashMap::new();
        q.insert("limit".to_string(), "50".to_string());
        assert_eq!(parse_limit_param(&q).unwrap(), 50);

        let mut q = HashMap::new();
        q.insert("limit".to_string(), "5000".to_string());
        assert_eq!(parse_limit_param(&q).unwrap(), 1000); // clamped

        let mut q = HashMap::new();
        q.insert("limit".to_string(), "0".to_string());
        assert_eq!(parse_limit_param(&q).unwrap(), 1); // clamped low
    }

    #[test]
    fn token_hex_shape() {
        let t = generate_token_hex().unwrap();
        assert_eq!(t.len(), 64);
        assert!(t.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    }

    #[test]
    fn sql_value_to_json_round_trip_basic_shapes() {
        assert_eq!(sql_value_to_json(SqlValue::Null), serde_json::Value::Null);
        assert_eq!(
            sql_value_to_json(SqlValue::Integer(42)),
            serde_json::Value::from(42)
        );
        assert_eq!(
            sql_value_to_json(SqlValue::Text("hello".into())),
            serde_json::Value::String("hello".into())
        );
    }
}

// ─── Integration tests ──────────────────────────────────────────────────
//
// End-to-end hub tests: spin up a real Router, seed a (module, project,
// token, rl_state-shaped table) fixture, exercise the routes through
// reqwest. Pattern matches `modules_api::tests::spawn_modules_api_hub`.

#[cfg(test)]
mod integration_tests {
    use super::*;

    use axum::Router;

    /// Spin up a router wired with both `module_db_api::router()` AND the
    /// `LauncherDbHandle` extension that `require_module_scope` consults.
    /// Returns `(base_url, db_handle, module_id, project_id, token)`.
    async fn spawn(
        module_id: &str,
        project_id: &str,
        namespace: &str,
    ) -> (String, LauncherDbHandle, String) {
        let db = Db::open_in_memory().expect("in-memory db");
        let handle = LauncherDbHandle(Arc::new(db));

        // Seed a row in `module_db_migrations` so the namespace lookup
        // succeeds. Apply pass DDL is mocked by raw INSERT — the goal
        // is to exercise the hub routes, not the apply mechanism (that's
        // covered in vct_launcher_core tests).
        {
            let guard = handle.0.lock();
            let now = chrono::Utc::now().timestamp_millis();
            guard
                .execute(
                    "INSERT INTO module_db_migrations \
                        (module_id, filename, sha256, namespace, applied_at) \
                     VALUES (?1, '0001_test.sql', 'deadbeef', ?2, ?3)",
                    rusqlite::params![module_id, namespace, now],
                )
                .unwrap();

            // Create a real table the routes can operate on. Hand-shaped
            // — the apply mechanism would normally do this; we skip it
            // for test isolation.
            let create = format!(
                "CREATE TABLE {ns}_state (
                    project_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT,
                    PRIMARY KEY (key)
                )",
                ns = namespace,
            );
            guard.execute(&create, []).unwrap();
        }

        // Issue a real token.
        let token = generate_token_hex().expect("rng");
        let now = chrono::Utc::now().timestamp_millis();
        let expires_at = now + REFRESH_TTL_MS;
        {
            let guard = handle.0.lock();
            guard
                .execute(
                    "INSERT INTO module_access_tokens \
                        (module_id, project_id, token_secret, issued_at, expires_at) \
                     VALUES (?1, ?2, ?3, ?4, ?5)",
                    rusqlite::params![module_id, project_id, &token, now, expires_at],
                )
                .unwrap();
        }

        // Build a minimal router (no hub-wide auth — we want to exercise
        // the module-scope middleware in isolation).
        let app: Router = Router::new()
            .nest("/api/v1", router().with_state(handle.clone()))
            .layer(axum::Extension(handle.clone()));

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("local_addr");
        tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });

        (format!("http://{}/api/v1", addr), handle, token)
    }

    fn auth_header(token: &str) -> reqwest::header::HeaderMap {
        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert(
            reqwest::header::AUTHORIZATION,
            format!("Bearer {}", token).parse().unwrap(),
        );
        headers
    }

    #[tokio::test]
    async fn get_row_returns_404_for_missing_key() {
        let (base, _h, token) = spawn("test-mod", "proj1", "rl").await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!("{}/modules/test-mod/db/projects/proj1/rows/rl_state/nonexistent", base))
            .headers(auth_header(&token))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 404);
    }

    #[tokio::test]
    async fn post_then_get_round_trip() {
        let (base, _h, token) = spawn("test-mod", "proj1", "rl").await;
        let client = reqwest::Client::new();

        let resp = client
            .post(format!("{}/modules/test-mod/db/projects/proj1/rows/rl_state", base))
            .headers(auth_header(&token))
            .json(&serde_json::json!({
                "key": "k1",
                "fields": { "value": "hello-world" }
            }))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 200, "POST failed: {}", resp.text().await.unwrap());

        let resp = client
            .get(format!("{}/modules/test-mod/db/projects/proj1/rows/rl_state/k1", base))
            .headers(auth_header(&token))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.unwrap();
        assert_eq!(body["value"], "hello-world");
        assert_eq!(body["key"], "k1");
    }

    #[tokio::test]
    async fn patch_updates_specified_fields() {
        let (base, _h, token) = spawn("test-mod", "proj1", "rl").await;
        let client = reqwest::Client::new();

        client
            .post(format!("{}/modules/test-mod/db/projects/proj1/rows/rl_state", base))
            .headers(auth_header(&token))
            .json(&serde_json::json!({
                "key": "k1",
                "fields": { "value": "v1" }
            }))
            .send()
            .await
            .unwrap();

        let resp = client
            .patch(format!("{}/modules/test-mod/db/projects/proj1/rows/rl_state/k1", base))
            .headers(auth_header(&token))
            .json(&serde_json::json!({ "fields": { "value": "v2" } }))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 200);

        let resp = client
            .get(format!("{}/modules/test-mod/db/projects/proj1/rows/rl_state/k1", base))
            .headers(auth_header(&token))
            .send()
            .await
            .unwrap();
        let body: serde_json::Value = resp.json().await.unwrap();
        assert_eq!(body["value"], "v2");
    }

    #[tokio::test]
    async fn delete_removes_row() {
        let (base, _h, token) = spawn("test-mod", "proj1", "rl").await;
        let client = reqwest::Client::new();

        client
            .post(format!("{}/modules/test-mod/db/projects/proj1/rows/rl_state", base))
            .headers(auth_header(&token))
            .json(&serde_json::json!({
                "key": "k1",
                "fields": { "value": "hi" }
            }))
            .send()
            .await
            .unwrap();

        let resp = client
            .delete(format!("{}/modules/test-mod/db/projects/proj1/rows/rl_state/k1", base))
            .headers(auth_header(&token))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 200);

        let resp = client
            .get(format!("{}/modules/test-mod/db/projects/proj1/rows/rl_state/k1", base))
            .headers(auth_header(&token))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 404);
    }

    #[tokio::test]
    async fn auth_missing_returns_401() {
        let (base, _h, _token) = spawn("test-mod", "proj1", "rl").await;
        let resp = reqwest::get(format!(
            "{}/modules/test-mod/db/projects/proj1/rows/rl_state/key1",
            base
        ))
        .await
        .unwrap();
        assert_eq!(resp.status(), 401);
    }

    #[tokio::test]
    async fn auth_wrong_module_returns_401() {
        // Token issued for test-mod; request hits other-mod.
        let (base, _h, token) = spawn("test-mod", "proj1", "rl").await;

        // Seed migration for other-mod so the route doesn't 404 on
        // "no_migrations_applied" before the auth check.
        {
            let guard = _h.0.lock();
            guard
                .execute(
                    "INSERT INTO module_db_migrations \
                        (module_id, filename, sha256, namespace, applied_at) \
                     VALUES ('other-mod', '0001.sql', 'abc', 'foo', 0)",
                    [],
                )
                .unwrap();
        }

        let client = reqwest::Client::new();
        let resp = client
            .get(format!("{}/modules/other-mod/db/projects/proj1/rows/foo_x/k1", base))
            .headers(auth_header(&token))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 401);
    }

    #[tokio::test]
    async fn auth_wrong_project_returns_403() {
        // Token scope = (test-mod, proj1); request hits proj2.
        let (base, _h, token) = spawn("test-mod", "proj1", "rl").await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!("{}/modules/test-mod/db/projects/proj2/rows/rl_state/k1", base))
            .headers(auth_header(&token))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 403);
    }

    #[tokio::test]
    async fn projection_returns_only_requested_columns() {
        let (base, _h, token) = spawn("test-mod", "proj1", "rl").await;
        let client = reqwest::Client::new();
        client
            .post(format!("{}/modules/test-mod/db/projects/proj1/rows/rl_state", base))
            .headers(auth_header(&token))
            .json(&serde_json::json!({
                "key": "k1",
                "fields": { "value": "v1" }
            }))
            .send()
            .await
            .unwrap();

        let resp = client
            .get(format!(
                "{}/modules/test-mod/db/projects/proj1/rows/rl_state/k1?fields=value",
                base
            ))
            .headers(auth_header(&token))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.unwrap();
        assert_eq!(body["value"], "v1");
        // The projected response should NOT include other columns.
        assert!(body.get("key").is_none() || body["key"].is_null(),
                "projected response must omit non-requested columns; got: {}", body);
    }

    #[tokio::test]
    async fn projection_rejects_invalid_column_name() {
        let (base, _h, token) = spawn("test-mod", "proj1", "rl").await;
        let client = reqwest::Client::new();
        let resp = client
            .get(format!(
                "{}/modules/test-mod/db/projects/proj1/rows/rl_state/k1?fields=col1;DROP%20TABLE",
                base
            ))
            .headers(auth_header(&token))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 400);
    }

    #[tokio::test]
    async fn refresh_token_issues_new_secret_invalidates_old() {
        let (base, _h, token) = spawn("test-mod", "proj1", "rl").await;
        let client = reqwest::Client::new();

        let resp = client
            .post(format!("{}/modules/test-mod/token/refresh", base))
            .headers(auth_header(&token))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.unwrap();
        let new_token = body["token"].as_str().expect("new token").to_string();
        assert_ne!(new_token, token, "refresh must yield a different token");

        // Old token must now fail (UPDATE keyed by old secret already
        // replaced the row).
        let resp = client
            .get(format!(
                "{}/modules/test-mod/db/projects/proj1/rows/rl_state/anything",
                base
            ))
            .headers(auth_header(&token))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 401);

        // New token works.
        let resp = client
            .get(format!(
                "{}/modules/test-mod/db/projects/proj1/rows/rl_state/anything",
                base
            ))
            .headers(auth_header(&new_token))
            .send()
            .await
            .unwrap();
        // 404 is fine — the row doesn't exist; but NOT 401 (auth ok).
        assert_eq!(resp.status(), 404);
    }

    #[tokio::test]
    async fn refresh_token_rejects_invalid_bearer() {
        let (base, _h, _token) = spawn("test-mod", "proj1", "rl").await;
        let client = reqwest::Client::new();
        let resp = client
            .post(format!("{}/modules/test-mod/token/refresh", base))
            .headers(auth_header("not-a-real-token"))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 401);
    }

    #[tokio::test]
    async fn list_rows_returns_all_for_project() {
        let (base, _h, token) = spawn("test-mod", "proj1", "rl").await;
        let client = reqwest::Client::new();
        for k in ["k1", "k2", "k3"] {
            client
                .post(format!("{}/modules/test-mod/db/projects/proj1/rows/rl_state", base))
                .headers(auth_header(&token))
                .json(&serde_json::json!({
                    "key": k,
                    "fields": { "value": format!("v-{}", k) }
                }))
                .send()
                .await
                .unwrap();
        }

        let resp = client
            .get(format!("{}/modules/test-mod/db/projects/proj1/rows/rl_state", base))
            .headers(auth_header(&token))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.unwrap();
        assert_eq!(body["count"].as_u64().unwrap(), 3);
    }

    #[tokio::test]
    async fn cross_namespace_write_rejected_with_403() {
        let (base, _h, token) = spawn("test-mod", "proj1", "rl").await;
        let client = reqwest::Client::new();
        // Try to POST to a non-namespaced table.
        let resp = client
            .post(format!("{}/modules/test-mod/db/projects/proj1/rows/projects", base))
            .headers(auth_header(&token))
            .json(&serde_json::json!({ "key": "k1", "fields": {} }))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 403);
    }
}
