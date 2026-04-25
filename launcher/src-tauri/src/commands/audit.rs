//! Audit log surface.
//!
//! The launcher already records mutating operations to the `audit_log`
//! table (via `Db::audit(...)` — see `db/access.rs`). This module exposes
//! a read endpoint so the `/audit` route can render a who-changed-what
//! table for NDA-bound consultant work.
//!
//! Schema (migration 001):
//!   audit_log(id, operation, project_id, module_id, detail [JSON], created_at)
//!
//! `detail` is stored as a JSON string and forwarded verbatim to the
//! frontend; callers can parse it client-side. We deliberately do NOT
//! flatten or interpret detail here — operations write whatever shape
//! makes sense for them and the table just shows what's there.

use serde::Serialize;
use tauri::{command, State};

use crate::db::Db;

#[derive(Debug, Serialize)]
pub struct AuditEvent {
    pub id: i64,
    pub operation: String,
    pub project_id: Option<String>,
    pub module_id: Option<String>,
    /// JSON string. Frontend may parse it for display.
    pub detail: String,
    /// OS user who performed the operation. "system" for pre-migration
    /// rows, "unknown" if the launcher could not resolve $USER.
    pub actor: String,
    /// Unix epoch milliseconds.
    pub created_at: i64,
}

/// List audit events, newest first.
///
/// `project_id`: optional filter — only events for this project.
/// `limit`: max rows to return; clamped to 1000 server-side.
#[command]
pub async fn list_audit_events(
    project_id: Option<String>,
    limit: Option<u32>,
    db: State<'_, Db>,
) -> Result<Vec<AuditEvent>, String> {
    let limit = limit.unwrap_or(200);
    db.audit_list(project_id.as_deref(), limit)
}
