// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! F3 (v0.2.72 pre-gate audit): run DB-touching env re-projections on the
//! blocking pool instead of a tokio async worker.
//!
//! `refresh_all_projects_env_with_db` (and the single-project
//! `refresh_project_env_with_db` it wraps per project) shells out to
//! `python -m vco_lib.config_projection` — N serial subprocesses with a
//! 30 s cap EACH. Several async Tauri commands used to call it directly on
//! the runtime, parking a tokio worker for potentially minutes on a large
//! project list and starving unrelated commands. This helper is the ONE
//! home for the fix: resolve the Tauri-managed [`Db`] INSIDE a
//! `spawn_blocking` task and run the caller's closure there.
//!
//! Contract:
//!   * The closure runs on the blocking pool with `&Db` resolved from the
//!     app handle (so no `'static` borrow gymnastics at call sites).
//!   * A join failure (panicked task) or missing Db state surfaces as
//!     `Err(String)` — the CALLER decides whether that's fatal (a DB write
//!     lived inside the closure) or soft-fail-loggable (the authoritative
//!     write already committed before the closure ran).
//!   * The sync `_with_db` helpers keep their signatures — they are still
//!     called from sync contexts (boot hooks, tests); only the async
//!     command bodies route through here.

use tauri::Manager;

use crate::db::Db;

/// Run `f(&Db)` on the blocking pool, resolving the Tauri-managed [`Db`]
/// inside the task. See the module docs for the contract.
///
/// `context` labels the join-error message so a panicked projection task
/// is attributable in logs.
pub async fn run_with_db_on_blocking_pool<R, T, F>(
    app: tauri::AppHandle<R>,
    context: &'static str,
    f: F,
) -> Result<T, String>
where
    R: tauri::Runtime,
    T: Send + 'static,
    F: FnOnce(&Db) -> T + Send + 'static,
{
    match tauri::async_runtime::spawn_blocking(move || -> Result<T, String> {
        let db = app
            .try_state::<Db>()
            .ok_or_else(|| format!("{context}: launcher.db state not available"))?;
        Ok(f(db.inner()))
    })
    .await
    {
        Ok(inner) => inner,
        Err(e) => Err(format!("{context}: blocking task join failed: {e}")),
    }
}
