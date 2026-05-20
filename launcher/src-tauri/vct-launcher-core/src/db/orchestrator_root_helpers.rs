//! Orchestrator-root project-row helpers.
//!
//! v0.2.21 (Step 3d): hoisted from `launcher::commands::orchestrator_root`
//! to satisfy Rust's orphan rule — the launcher cannot `impl Db { ... }`
//! once `Db` lives in this core crate. The methods here are pure DB
//! queries with no Tauri-runtime dependencies, so moving them into core
//! is the right home regardless.
//!
//! The launcher's `commands::orchestrator_root` retains its Tauri-command
//! surface + the `ensure_orchestrator_root` function (which still depends
//! on launcher-only modules like `commands::modules` and
//! `commands::projects_v2`). Only the `Db` extension methods moved.

use crate::db::Db;

impl Db {
    /// True iff a row with `host='orchestrator_root'` exists. Cheap
    /// existence check used by `ensure_orchestrator_root` to short-
    /// circuit before any disk I/O.
    pub fn has_orchestrator_root_project(&self) -> Result<bool, String> {
        let guard = self.lock();
        let count: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM projects WHERE host = 'orchestrator_root'",
                [],
                |r| r.get(0),
            )
            .map_err(|e| format!("count orchestrator_root rows: {}", e))?;
        Ok(count > 0)
    }
}
