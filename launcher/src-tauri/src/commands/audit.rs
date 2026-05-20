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

use tauri::{command, State};

use crate::db::Db;

// v0.2.21 (Step 3d): AuditEvent moved to vct-launcher-core so
// `Db::audit_list` can return it without a reverse-dependency on the
// launcher's commands module. Re-export keeps the original import path
// (`crate::commands::audit::AuditEvent`) valid for any caller — the
// frontend bindings + lib.rs registration both continue to compile.
pub use vct_launcher_core::db::audit_types::AuditEvent;

/// List audit events, newest first.
///
/// All filters are pushed into SQLite via `Db::audit_list` so the wire
/// payload only carries rows that match. This previously returned a
/// 500-event window for the browser to filter; that fell over once
/// audit logs grew past a few thousand events.
///
/// Parameters:
///   * `project_id` — exact-match project filter (optional).
///   * `actor` — exact-match actor filter (optional).
///   * `since_ms` / `until_ms` — inclusive epoch-ms bounds (optional).
///   * `search` — substring match against operation OR detail (optional).
///   * `limit` — max rows; clamped to 10000 server-side; default 500.
#[command]
pub async fn list_audit_events(
    project_id: Option<String>,
    actor: Option<String>,
    since_ms: Option<i64>,
    until_ms: Option<i64>,
    search: Option<String>,
    limit: Option<u32>,
    db: State<'_, Db>,
) -> Result<Vec<AuditEvent>, String> {
    let limit = limit.unwrap_or(500);
    db.audit_list(
        project_id.as_deref(),
        actor.as_deref(),
        since_ms,
        until_ms,
        search.as_deref(),
        limit,
    )
}
