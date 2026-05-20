//! Tauri-commands wrapping the `module_weights_state` table (migration
//! 016, Phase 3C). The DB plumbing lives in `db/module_weights_state.rs`;
//! this file just exposes it to the frontend.
//!
//! Why a separate file from `db/`: the orchestrator's convention is that
//! every Tauri command (anything `#[tauri::command]`) lives under
//! `commands/`. The DB helpers in `db/` are intentionally not directly
//! addressable from the JS side — they take `&Db` not `State<'_, Db>`,
//! so the Tauri runtime can't auto-inject the managed handle. This
//! wrapper layer is the canonical "JS reachable" surface.

use serde::{Deserialize, Serialize};
use tauri::{command, State};

use crate::db::models::WeightsStateRow;
use crate::db::Db;

// ─── Wire types ─────────────────────────────────────────────────────────

/// JS-facing view of a single module_weights_state row. Same shape as
/// the DB-side `WeightsStateRow`; we duplicate the type here so the
/// `commands` layer doesn't leak DB-layer details into the IPC schema.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WeightsStateView {
    pub project_id: String,
    pub module_id: String,
    pub embedding_source: String,
    pub version: String,
    pub last_checked_at: i64,
    pub last_finetuned_at: i64,
}

impl From<WeightsStateRow> for WeightsStateView {
    fn from(r: WeightsStateRow) -> Self {
        Self {
            project_id: r.project_id,
            module_id: r.module_id,
            embedding_source: r.embedding_source,
            version: r.version,
            last_checked_at: r.last_checked_at,
            last_finetuned_at: r.last_finetuned_at,
        }
    }
}

// ─── Commands ──────────────────────────────────────────────────────────

/// Read the state row for a (project_id, module_id, embedding_source)
/// triple. Returns `None` (serialised as JSON `null`) when no row exists.
#[command]
pub async fn get_weights_state(
    project_id: String,
    module_id: String,
    embedding_source: String,
    db: State<'_, Db>,
) -> Result<Option<WeightsStateView>, String> {
    db.get_weights_state(&project_id, &module_id, &embedding_source)
        .map(|opt| opt.map(WeightsStateView::from))
}

/// Upsert a full state row. Frontend rarely calls this directly — the
/// narrower `set_*` helpers are preferred for column-scoped writes —
/// but it's exposed for migration / test harness use.
#[command]
pub async fn upsert_weights_state(
    project_id: String,
    module_id: String,
    embedding_source: String,
    version: String,
    last_checked_at: i64,
    last_finetuned_at: i64,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.upsert_weights_state(
        &project_id,
        &module_id,
        &embedding_source,
        &version,
        last_checked_at,
        last_finetuned_at,
    )
}

/// Stamp `last_checked_at = now()`. Frontend uses this when the user
/// hits the manual "Check for update" button — the launcher records the
/// attempt before the (possibly slow) network call so the dashboard's
/// "last checked" indicator updates immediately.
#[command]
pub async fn set_weights_state_last_checked_at(
    project_id: String,
    module_id: String,
    embedding_source: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_last_checked_at(&project_id, &module_id, &embedding_source)
}

/// Stamp `last_finetuned_at = now()`. Called after a successful local
/// fine-tune so the dashboard shows the user when the last training
/// pass ran.
#[command]
pub async fn set_weights_state_last_finetuned_at(
    project_id: String,
    module_id: String,
    embedding_source: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_last_finetuned_at(&project_id, &module_id, &embedding_source)
}

/// Persist a new locally-active weights version. Called when the user
/// accepts a downloaded weights update (Phase 4A: `apply_weights_update`
/// with choice = Skip or Now).
#[command]
pub async fn set_weights_state_version(
    project_id: String,
    module_id: String,
    embedding_source: String,
    version: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_weights_version(&project_id, &module_id, &embedding_source, &version)
}

/// List every weights state row for a project. Used by the Phase 4B
/// dashboard widget to show the full per-embedding-source matrix.
#[command]
pub async fn list_weights_state_for_project(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<WeightsStateView>, String> {
    let rows = db.list_weights_state_for_project(&project_id)?;
    Ok(rows.into_iter().map(WeightsStateView::from).collect())
}
