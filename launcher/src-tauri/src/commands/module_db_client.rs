// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Launcher-side HTTP client for the hub's module-DB REST surface
//! (Agent I, v0.2.31; routes in `vct_hub::module_db_api`).
//!
//! ## Why this lives in the launcher
//!
//! The launcher dashboard widget needs to display container-owned
//! state (e.g. `rl_weights_state.local_version`) without the
//! launcher writing to that table itself. The hub exposes typed REST
//! endpoints with per-(module, project) bearer-token auth; this file
//! is the thin client that:
//!
//!   1. Reads/refreshes the per-(module, project) shared secret from
//!      `module_access_tokens` (issues a fresh one via
//!      `module_db::issue_module_access_token` if missing or expired).
//!   2. Issues the HTTP GET against the hub.
//!   3. Returns the raw JSON for the frontend to render.
//!
//! Same code path the container takes (bearer token + URL) — this
//! validates the rows API end-to-end and demonstrates that the
//! launcher and container talk to the hub through the same surface,
//! per the Single-Writer Principle restoration in v0.2.31.
//!
//! ## Soft-fail
//!
//! All errors flow back as `Result::Err(String)` strings. The dashboard
//! widget catches and renders a fallback ("—" or "Container not
//! running") instead of crashing the page. We never panic, and we never
//! cache the token in a static — re-reading from launcher.db on every
//! call is microseconds and lets a token rotation propagate transparently.

use std::time::Duration;

use serde_json::Value;
use tauri::{command, State};

use crate::commands::module_db::DEFAULT_TOKEN_TTL_MS;
use crate::db::Db;

/// Bounded HTTP-call timeout for hub reads. 5 s matches the existing
/// timeouts used by the dashboard's other probes (e.g. `/state_summary`
/// in `module_service.rs` is 2 s; this one is slightly higher because
/// the hub's DB layer takes the rusqlite lock).
const HUB_READ_TIMEOUT_SECS: u64 = 5;

/// Margin (ms) below the token's `expires_at` at which we proactively
/// refresh. Avoids racing the hub's expiry check on the very last
/// millisecond. 60 s is generous; tokens have a 1-hour TTL on issue.
const TOKEN_REFRESH_MARGIN_MS: i64 = 60_000;

/// Read the hub.port file. Same pattern as `commands::hub_proxy::hub_port`.
fn hub_port() -> Result<u16, String> {
    let path = crate::paths::vct_root_dir().join("hub.port");
    let raw = std::fs::read_to_string(&path)
        .map_err(|e| format!("read hub.port: {}", e))?;
    raw.trim()
        .parse::<u16>()
        .map_err(|e| format!("parse hub.port: {}", e))
}

/// Generate a hex-encoded 32-byte random token from the OS CSPRNG.
///
/// Mirrors the helper in `commands::module_db::generate_token_hex` —
/// duplicated here (3 lines) to keep `module_db_client` self-contained
/// without re-exporting private helpers. v0.2.32 should move both into
/// a shared crate.
// v0.2.54 Track J amend: delegate to vct-launcher-core::services::boot_token
// (same OsRng + hex shape; v0.2.32-era "move to shared crate" comment closed).
fn generate_token_hex() -> Result<String, String> {
    vct_launcher_core::services::boot_token::generate_token()
}

/// Get a usable per-(module, project) bearer token. Reads
/// `module_access_tokens` first; if the row is missing or expired (or
/// within the refresh margin) we re-issue inline.
///
/// We don't delegate to `module_db::issue_module_access_token` because
/// that's a `#[tauri::command]` taking `State<'_, Db>` — calling it
/// from another command requires cloning the State guard, which Tauri
/// doesn't support. Inlining the upsert keeps the dependency graph
/// clean and the SQL identical.
fn get_or_issue_token(
    db: &Db,
    module_id: &str,
    project_id: &str,
) -> Result<String, String> {
    let now = chrono::Utc::now().timestamp_millis();

    // Try the cached row first.
    let cached: Option<(String, i64)> = {
        let guard = db.lock();
        guard
            .query_row(
                "SELECT token_secret, expires_at FROM module_access_tokens \
                 WHERE module_id = ?1 AND project_id = ?2",
                rusqlite::params![module_id, project_id],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?)),
            )
            .ok()
    };

    if let Some((secret, expires_at)) = cached {
        if expires_at > now + TOKEN_REFRESH_MARGIN_MS {
            return Ok(secret);
        }
        // Falls through to re-issue.
    }

    // No row, or near-expiry. Re-issue inline (same SQL as
    // `module_db::issue_module_access_token`).
    let secret = generate_token_hex()?;
    let expires_at = now + DEFAULT_TOKEN_TTL_MS;
    {
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO module_access_tokens \
                    (module_id, project_id, token_secret, issued_at, expires_at) \
                 VALUES (?1, ?2, ?3, ?4, ?5) \
                 ON CONFLICT(module_id, project_id) DO UPDATE SET \
                    token_secret = excluded.token_secret, \
                    issued_at = excluded.issued_at, \
                    expires_at = excluded.expires_at",
                rusqlite::params![module_id, project_id, &secret, now, expires_at],
            )
            .map_err(|e| format!("upsert module_access_tokens: {}", e))?;
    }
    Ok(secret)
}

/// Tauri command: read a single keyed row from the hub's module-DB
/// REST surface and return the raw JSON.
///
/// URL: `GET /api/v1/modules/{module_id}/db/projects/{project_id}/rows/{table}/{key}`
///      (optionally with `?fields=col1,col2` projection).
///
/// Wire shape for the frontend (return value):
///   - On 200: the JSON object as the hub serialised it (e.g.
///     `{"local_version":"v1","last_finetuned_at":1700000000000}`).
///   - On 404 (row not found / no migrations applied): `null`.
///   - On any other error: `Result::Err(String)` with a one-line
///     reason. The Svelte dashboard catches this and renders a
///     gray "Container not running" / "—" fallback.
///
/// Soft-fail throughout: hub down, token unobtainable, parse failures
/// all surface as `Err(String)`. The dashboard renders the fallback
/// state without crashing.
#[command]
pub async fn module_db_read_row(
    module_id: String,
    project_id: String,
    table: String,
    key: String,
    fields: Option<Vec<String>>,
    db: State<'_, Db>,
) -> Result<Option<Value>, String> {
    // Tauri-command shim — see `module_db_read_row_with_fields_inner`.
    module_db_read_row_with_fields_inner(module_id, project_id, table, key, fields, db.inner())
        .await
}

/// v0.2.33 (Agent D): non-Tauri-command form callable from
/// chained_action's `tauri_command` step dispatcher. Returns
/// `serde_json::Value` (not `Option<Value>`) so the dispatcher can
/// thread the response into the next step's body — `None` is
/// represented as `Value::Null` over the dispatch wire.
pub async fn module_db_read_row_inner(
    module_id: String,
    project_id: String,
    table: String,
    key: String,
    db: &Db,
) -> Result<Value, String> {
    match module_db_read_row_with_fields_inner(module_id, project_id, table, key, None, db).await? {
        Some(value) => Ok(value),
        None => Ok(Value::Null),
    }
}

/// Shared implementation backing both `module_db_read_row` (the Tauri
/// command, returns `Option<Value>` to distinguish 404 from server
/// error at the frontend) and `module_db_read_row_inner` (the
/// dispatcher-callable form, collapses 404 to `Value::Null`).
async fn module_db_read_row_with_fields_inner(
    module_id: String,
    project_id: String,
    table: String,
    key: String,
    fields: Option<Vec<String>>,
    db: &Db,
) -> Result<Option<Value>, String> {
    let port = hub_port()?;
    let token = get_or_issue_token(db, &module_id, &project_id)?;

    let mut url = format!(
        "http://127.0.0.1:{}/api/v1/modules/{}/db/projects/{}/rows/{}/{}",
        port, module_id, project_id, table, key,
    );
    if let Some(cols) = fields.as_ref() {
        if !cols.is_empty() {
            url.push_str("?fields=");
            url.push_str(&cols.join(","));
        }
    }

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(HUB_READ_TIMEOUT_SECS))
        .build()
        .map_err(|e| format!("http client: {}", e))?;

    let resp = client
        .get(&url)
        .bearer_auth(&token)
        .send()
        .await
        .map_err(|e| format!("hub GET: {}", e))?;

    let status = resp.status();
    if status.as_u16() == 404 {
        // Row not found OR module has no migrations applied yet —
        // both are normal states the dashboard handles by rendering
        // a placeholder. Return `None` so the frontend can branch
        // without parsing the error body.
        return Ok(None);
    }
    if !status.is_success() {
        // Try to extract a useful detail from the body; fall back to
        // status code only.
        let body = resp.text().await.unwrap_or_default();
        let trimmed = body.chars().take(200).collect::<String>();
        return Err(format!("hub returned {}: {}", status.as_u16(), trimmed));
    }

    let body: Value = resp
        .json()
        .await
        .map_err(|e| format!("parse hub body: {}", e))?;
    Ok(Some(body))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_refresh_margin_is_positive_and_subsecond_of_ttl() {
        // Sanity: margin should fit comfortably inside the 1-hour TTL
        // set by `module_db::DEFAULT_TOKEN_TTL_MS`, otherwise the
        // refresh-eagerly path triggers on every call.
        assert!(TOKEN_REFRESH_MARGIN_MS > 0);
        assert!(TOKEN_REFRESH_MARGIN_MS < crate::commands::module_db::DEFAULT_TOKEN_TTL_MS);
    }

    #[test]
    fn hub_read_timeout_is_bounded() {
        // We don't want a hung hub to block the dashboard load
        // indefinitely. 5 s is the upper bound we promise.
        assert!(HUB_READ_TIMEOUT_SECS > 0);
        assert!(HUB_READ_TIMEOUT_SECS <= 10);
    }
}
