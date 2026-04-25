//! Frontend-facing commands for the change_log table (P7 concurrency
//! invalidation). The frontend polls `poll_changes(since)` every few
//! seconds; whenever new rows come back, it invalidates the affected
//! stores. See docs/CONCURRENCY_INVALIDATION.md for the wire-up.
//!
//! Writes happen elsewhere — this module is read-only for the frontend.

use tauri::{command, State};

use crate::db::change_log::ChangeRow;
use crate::db::Db;

/// Returns every change_log entry with seq strictly greater than `since`.
/// First call from the frontend should pass `0` to get the current
/// floor (or call `current_change_seq` first to skip the historical
/// noise).
#[command]
pub async fn poll_changes(since: i64, db: State<'_, Db>) -> Result<Vec<ChangeRow>, String> {
    db.changes_since(since)
}

/// Returns the current head of the change_log. Frontend caches this and
/// uses it as the starting cursor. Avoids replaying ancient changes
/// when the launcher has been running for hours.
#[command]
pub async fn current_change_seq(db: State<'_, Db>) -> Result<i64, String> {
    db.current_change_seq()
}
