// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Codegraph retrieval settings — v0.2.72 P1 (machine-global floors) + P5
//! (per-project `.claude/`-indexing toggle).
//!
//! Two value classes, two storage backends, mirroring the v0.2.71 plumbing:
//!
//!   * **P1 floors** are MACHINE-GLOBAL, stored in the flat `app_state`
//!     key→TEXT table exactly like `embedding.active_profile`. The two keys
//!     (`codegraph.retrieval_floor` default 0.16, `codegraph.post_rerank_floor`
//!     default 0.22) gate code-graph retrieval: `retrieval_floor` is the
//!     pre-rerank seed cutoff, `post_rerank_floor` the final cutoff after
//!     reranking. `set_codegraph_floors` validates the `0.0..=1.0` range,
//!     writes both keys, then triggers env re-projection so the analyzer /
//!     MCP subprocesses pick up the projected `VCO_CODE_GRAPH_RETRIEVAL_FLOOR` /
//!     `VCO_CODE_GRAPH_POST_RERANK_FLOOR` env vars.
//!
//!   * **P5 `.claude`-index toggle** is PER-PROJECT, stored in
//!     `module_settings(project_id, "orchestrator-core",
//!     "codegraph_index_dot_claude")` — the same table + module scope the
//!     v0.2.71 T-B-flags dual-write toggle uses. When no row exists the
//!     DEFAULT is HOST-BASED: the orchestrator clone itself
//!     (`ProjectHost::OrchestratorRoot`) defaults to TRUE (index its
//!     `.claude/` tooling, which IS the product's source); every other host
//!     defaults to FALSE (an app project's `.claude/` is generated tooling,
//!     noise in its code graph). Only an explicit user toggle writes a row.
//!
//! COORDINATION:
//!   * T-FLOOR owns projecting the floor app_state keys into env
//!     (`vco_lib/config_projection.py`). This module only WRITES the values
//!     and triggers `refresh_project_env`; if the projection does not yet
//!     read `codegraph.retrieval_floor` / `codegraph.post_rerank_floor`,
//!     T-FLOOR/integrator must add that read.
//!   * T-SCOPE (analyze_code_graph.py + codegraph.rs rebuild) reads the
//!     per-project bool via `get_project_codegraph_index_dot_claude` (or the
//!     projected env / hub config) to scope the `.claude/` walk.
//!   * generate_handler! wiring in `lib.rs` is integrator-only; the four
//!     `#[tauri::command]` fns below are `pub` and must be added there.

// v0.2.72 integrator: the four `#[tauri::command]` fns are now wired into
// `lib.rs`'s `generate_handler!`, and `resolve_codegraph_index_dot_claude` is
// called from `commands::codegraph::run_build_task` — so the pre-merge
// module-scope `#![allow(dead_code)]` is removed (nothing here is dead now).

use tauri::State;

use crate::db::Db;
use vct_launcher_core::db::models::ProjectHost;

/// Canonical `module_id` the per-project `.claude`-index flag lives under.
/// Orchestrator-core scope (not module-specific): whether to index the
/// `.claude/` tooling tree is an orchestrator-wide code-graph concern.
pub const ORCHESTRATOR_CORE_MODULE_ID: &str = "orchestrator-core";

/// Setting key for the per-project "index `.claude/` tooling into this
/// project's code graph" flag. Read (via the projected env / hub config) by
/// the T-SCOPE analyzer scope logic.
pub const CODEGRAPH_INDEX_DOT_CLAUDE_KEY: &str = "codegraph_index_dot_claude";

// ─── P1: machine-global retrieval floors ─────────────────────────────────

/// Wire type for the floor getter — a named pair so the frontend does not
/// depend on tuple field order.
#[derive(Debug, Clone, serde::Serialize)]
pub struct CodegraphFloors {
    /// Pre-rerank seed cutoff (`codegraph.retrieval_floor`).
    pub retrieval: f64,
    /// Final cutoff after reranking (`codegraph.post_rerank_floor`).
    pub post_rerank: f64,
}

/// Read the two machine-global codegraph floors. Missing / unparseable rows
/// resolve to the compiled-in defaults (soft-fail — see the DB getters).
#[tauri::command]
pub async fn get_codegraph_floors(db: State<'_, Db>) -> Result<CodegraphFloors, String> {
    Ok(CodegraphFloors {
        retrieval: db.get_codegraph_retrieval_floor()?,
        post_rerank: db.get_codegraph_post_rerank_floor()?,
    })
}

/// Persist both machine-global codegraph floors and re-project every
/// project's env so the CLI/hook/MCP subprocesses pick up the new values.
///
/// Validation (`0.0..=1.0`, finite) lives in `Db::set_codegraph_floors`; a
/// bad value returns Err BEFORE any write lands. After a successful write we
/// re-project ALL projects (the floors are machine-global — every project's
/// projected env carries them), best-effort: a projection hiccup on one
/// project is a warning, not a failure of the DB write that already
/// succeeded.
#[tauri::command]
pub async fn set_codegraph_floors(
    retrieval: f64,
    post_rerank: f64,
    app: tauri::AppHandle,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_codegraph_floors(retrieval, post_rerank)?;
    // Machine-global change → re-project every project's env. Soft-fail: the
    // authoritative DB write already landed; the projection helper never
    // returns Err (it accumulates per-project warnings/failures into a
    // result struct) so a projection hiccup can't roll back or hard-fail the
    // DB write. Log any accumulated problems for diagnosis.
    //
    // F3 (v0.2.72): the refresh runs N serial Python subprocesses (30 s cap
    // each) — route it through spawn_blocking so it doesn't park a tokio
    // worker for the duration. Join errors are soft-fail too (write landed).
    if let Err(e) = crate::commands::blocking::run_with_db_on_blocking_pool(
        app,
        "set_codegraph_floors env re-projection",
        |db| {
            let refresh =
                crate::commands::projects_v2::refresh_all_projects_env_with_db(db);
            if !refresh.global_warnings.is_empty() || !refresh.failed.is_empty() {
                eprintln!(
                    "[vct] set_codegraph_floors: env re-projection warnings: \
                     global={:?} failed={:?}",
                    refresh.global_warnings, refresh.failed
                );
            }
        },
    )
    .await
    {
        eprintln!(
            "[vct] warning: set_codegraph_floors: {} (DB write already committed)",
            e
        );
    }
    Ok(())
}

// ─── P5: per-project `.claude`-index toggle ──────────────────────────────

/// Host-based default for the `.claude`-index flag when no per-project row
/// exists: the orchestrator clone itself indexes its `.claude/` tooling
/// (that IS the product source); every other project excludes it.
fn default_index_dot_claude(host: &ProjectHost) -> bool {
    matches!(host, ProjectHost::OrchestratorRoot)
}

/// Resolve the effective per-project `.claude`-index flag: an explicit
/// per-project row wins; otherwise the host-based default applies.
///
/// Exposed as a plain fn (not just the command) so T-SCOPE / other Rust
/// call-sites can resolve the value without a Tauri `State` wrapper.
pub fn resolve_codegraph_index_dot_claude(
    db: &Db,
    project_id: &str,
) -> Result<bool, String> {
    if let Some(v) = db.get_setting(
        project_id,
        ORCHESTRATOR_CORE_MODULE_ID,
        CODEGRAPH_INDEX_DOT_CLAUDE_KEY,
    )? {
        if let Some(b) = v.as_bool() {
            return Ok(b);
        }
        // Malformed row (non-bool) — fall through to the host default rather
        // than trusting a corrupt value.
    }
    let host = db
        .get_project(project_id)?
        .map(|p| p.host)
        .unwrap_or(ProjectHost::Base);
    Ok(default_index_dot_claude(&host))
}

/// Read the effective per-project `.claude`-index flag (explicit row, else
/// host-based default).
#[tauri::command]
pub async fn get_project_codegraph_index_dot_claude(
    project_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    if project_id.is_empty() {
        return Err(
            "get_project_codegraph_index_dot_claude: project_id required".into(),
        );
    }
    resolve_codegraph_index_dot_claude(&db, &project_id)
}

/// Set the per-project `.claude`-index flag, then re-project this project's
/// env so the analyzer picks up the new scope on its next walk.
#[tauri::command]
pub async fn set_project_codegraph_index_dot_claude(
    project_id: String,
    enabled: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    if project_id.is_empty() {
        return Err(
            "set_project_codegraph_index_dot_claude: project_id required".into(),
        );
    }
    db.set_setting(
        &project_id,
        ORCHESTRATOR_CORE_MODULE_ID,
        CODEGRAPH_INDEX_DOT_CLAUDE_KEY,
        &serde_json::Value::Bool(enabled),
    )?;
    // Per-project change → re-project just this project. Soft-fail: the DB
    // write already landed; a projection hiccup is a warning.
    if let Err(e) =
        crate::commands::projects_v2::refresh_project_env_with_db(&db, &project_id)
    {
        eprintln!(
            "[vct] set_project_codegraph_index_dot_claude: env re-projection \
             warning for {project_id}: {e}"
        );
    }
    Ok(())
}

// ─── Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use vct_launcher_core::db::models::ProjectHost;

    fn insert_project(db: &Db, id: &str, host: ProjectHost) {
        db.insert_project(
            id,
            &format!("Project {id}"),
            &format!("/tmp/project-{id}"),
            host,
            &format!("project-{id}"),
        )
        .expect("insert project");
    }

    // ── P1 floors ──

    #[test]
    fn floors_default_then_round_trip() {
        let db = Db::open_in_memory().expect("in-memory db");
        // Command-level default reads mirror the DB getters.
        let floors = CodegraphFloors {
            retrieval: db.get_codegraph_retrieval_floor().unwrap(),
            post_rerank: db.get_codegraph_post_rerank_floor().unwrap(),
        };
        assert_eq!(floors.retrieval, 0.16);
        assert_eq!(floors.post_rerank, 0.22);

        db.set_codegraph_floors(0.25, 0.40).unwrap();
        assert_eq!(db.get_codegraph_retrieval_floor().unwrap(), 0.25);
        assert_eq!(db.get_codegraph_post_rerank_floor().unwrap(), 0.40);
    }

    #[test]
    fn floors_reject_out_of_range() {
        let db = Db::open_in_memory().expect("in-memory db");
        assert!(db.set_codegraph_floors(1.2, 0.2).is_err());
        assert!(db.set_codegraph_floors(0.2, -0.5).is_err());
    }

    // ── P5 per-project toggle default-resolution ──

    #[test]
    fn default_index_dot_claude_is_host_based() {
        assert!(
            default_index_dot_claude(&ProjectHost::OrchestratorRoot),
            "orchestrator-root defaults to indexing .claude/",
        );
        assert!(
            !default_index_dot_claude(&ProjectHost::Base),
            "a base project defaults to EXCLUDING .claude/",
        );
        assert!(
            !default_index_dot_claude(&ProjectHost::Mao),
            "a mao project defaults to EXCLUDING .claude/",
        );
    }

    #[test]
    fn resolve_uses_host_default_when_no_row() {
        let db = Db::open_in_memory().expect("in-memory db");
        insert_project(&db, "root", ProjectHost::OrchestratorRoot);
        insert_project(&db, "app", ProjectHost::Base);

        assert!(
            resolve_codegraph_index_dot_claude(&db, "root").unwrap(),
            "orchestrator-root project defaults TRUE",
        );
        assert!(
            !resolve_codegraph_index_dot_claude(&db, "app").unwrap(),
            "non-root project defaults FALSE",
        );
    }

    #[test]
    fn explicit_row_overrides_host_default() {
        let db = Db::open_in_memory().expect("in-memory db");
        insert_project(&db, "root", ProjectHost::OrchestratorRoot);
        insert_project(&db, "app", ProjectHost::Base);

        // Turn the root project OFF and the app project ON — both flip away
        // from their host defaults.
        db.set_setting(
            "root",
            ORCHESTRATOR_CORE_MODULE_ID,
            CODEGRAPH_INDEX_DOT_CLAUDE_KEY,
            &serde_json::Value::Bool(false),
        )
        .unwrap();
        db.set_setting(
            "app",
            ORCHESTRATOR_CORE_MODULE_ID,
            CODEGRAPH_INDEX_DOT_CLAUDE_KEY,
            &serde_json::Value::Bool(true),
        )
        .unwrap();

        assert!(!resolve_codegraph_index_dot_claude(&db, "root").unwrap());
        assert!(resolve_codegraph_index_dot_claude(&db, "app").unwrap());
    }

    #[test]
    fn resolve_missing_project_falls_back_to_base_default() {
        // No project row at all → treat as Base (default FALSE) rather than
        // erroring: the resolver is best-effort and never blocks a render.
        let db = Db::open_in_memory().expect("in-memory db");
        assert!(!resolve_codegraph_index_dot_claude(&db, "ghost").unwrap());
    }
}
