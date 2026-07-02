//! Tauri commands exposing the launcher.db `app_state` key-value table
//! to the frontend. These replace direct localStorage reads/writes for
//! launcher-state flags that need to be isolated by VCT_STATE_DIR
//! (Bug 14 fix).
//!
//! Naming convention for keys: dotted, lowercase, ecosystem-prefixed.
//! Frontend should use stable constants, e.g.:
//!     const KEY_ONBOARDING_COMPLETE = "onboarding.complete";
//!     const KEY_TELEMETRY_TERMS_ACCEPTED = "telemetry.terms_accepted";
//!
//! `null` (None on the Rust side, `null` on the JS side) means "no row
//! exists" — i.e. apply default behaviour. Callers that need to
//! distinguish "user explicitly set to false" from "never set" MUST
//! check for null vs false.

use serde::Serialize;
use tauri::{command, State};

use crate::db::Db;

/// Result envelope for `get_app_state` so the frontend can reliably
/// distinguish "row absent" from "row present with empty value". A
/// plain `Option<String>` would also work but JSON-serialised `null`
/// is easy to misuse on the JS side; explicit `is_set` removes ambiguity.
#[derive(Debug, Serialize)]
pub struct AppStateGetResult {
    pub key: String,
    pub is_set: bool,
    pub value: Option<String>,
}

#[command]
pub async fn app_state_get(
    key: String,
    db: State<'_, Db>,
) -> Result<AppStateGetResult, String> {
    let raw = db.app_state_get(&key)?;
    Ok(AppStateGetResult {
        key,
        is_set: raw.is_some(),
        value: raw,
    })
}

/// F5 (v0.2.72): after a generic app_state write, re-project every
/// project's `.claude/{settings.json,env}` IF the key is one of the
/// machine-global inputs the env projection consumes (currently the
/// ACTIVE_EMBEDDING cascade keys — the Preferences page writes
/// `embedding.active_profile` through THIS generic command, not a
/// dedicated setter). The rewrite is what lets the settings watcher's
/// diff-guard fire the guarded MCP reload. Soft-fail: the DB write has
/// already committed; a projection hiccup is logged, never propagated.
///
/// Returns `None` when the key is not MCP-relevant (no refresh ran),
/// `Some(report)` otherwise — the split is what the unit tests pin.
fn maybe_reproject_after_app_state_write(
    db: &Db,
    key: &str,
) -> Option<crate::commands::projects_v2::RefreshAllProjectsEnvResult> {
    if !crate::commands::project_env_settings::app_state_key_triggers_env_reprojection(key) {
        return None;
    }
    let report = crate::commands::projects_v2::refresh_all_projects_env_with_db(db);
    for (name, err) in &report.failed {
        eprintln!(
            "[vct] warning: app_state_set({}) env re-projection failed for {}: {}",
            key, name, err
        );
    }
    Some(report)
}

/// Free-function core of `app_state_set` (testable without a Tauri
/// runtime). Writes the row, then conditionally re-projects env files —
/// see `maybe_reproject_after_app_state_write`.
pub fn app_state_set_with_db(
    db: &Db,
    key: &str,
    value: &str,
) -> Result<Option<crate::commands::projects_v2::RefreshAllProjectsEnvResult>, String> {
    db.app_state_set(key, value)?;
    Ok(maybe_reproject_after_app_state_write(db, key))
}

#[command]
pub async fn app_state_set(
    key: String,
    value: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    app_state_set_with_db(&db, &key, &value).map(|_| ())
}

/// Boolean convenience. Returns `null` when the row is absent (so the
/// frontend can apply default behaviour), `true`/`false` otherwise.
#[command]
pub async fn app_state_get_bool(
    key: String,
    db: State<'_, Db>,
) -> Result<Option<bool>, String> {
    db.app_state_get_bool(&key)
}

#[command]
pub async fn app_state_set_bool(
    key: String,
    value: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.app_state_set_bool(&key, value)?;
    // F5 (v0.2.72): same conditional re-projection as `app_state_set`.
    // The known MCP-relevant keys are string-valued, but the generic bool
    // surface must not become a bypass route.
    let _ = maybe_reproject_after_app_state_write(&db, &key);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// F5 (v0.2.72): the generic setter re-projects env for MCP-relevant
    /// keys and does NOT for unrelated launcher-state keys. The
    /// `Some`/`None` split is the observable proof of which branch ran.
    #[test]
    fn app_state_set_reprojects_only_for_mcp_relevant_keys() {
        let db = Db::open_in_memory().expect("in-memory db");

        // MCP-relevant: the ACTIVE_EMBEDDING cascade key the Preferences
        // page writes through the generic command.
        let report = app_state_set_with_db(
            &db,
            crate::commands::project_env_settings::APP_STATE_KEY_ACTIVE_EMBEDDING,
            "arctic",
        )
        .expect("write must succeed");
        assert!(
            report.is_some(),
            "embedding.active_profile write must trigger the env re-projection",
        );
        assert_eq!(
            db.app_state_get(
                crate::commands::project_env_settings::APP_STATE_KEY_ACTIVE_EMBEDDING
            )
            .unwrap()
            .as_deref(),
            Some("arctic"),
            "the authoritative DB write must land regardless of projection outcome",
        );

        // Not MCP-relevant: an ordinary launcher-state flag.
        let report = app_state_set_with_db(&db, "onboarding.complete", "true")
            .expect("write must succeed");
        assert!(
            report.is_none(),
            "non-MCP-relevant keys must not trigger a machine-global refresh",
        );
    }

    /// The refresh-all run over registered projects is observable through
    /// the returned report: a project whose folder does not exist lands in
    /// `skipped` — a value only the refresh path computes.
    #[test]
    fn app_state_set_refresh_report_covers_registered_projects() {
        let db = Db::open_in_memory().expect("in-memory db");
        db.insert_project(
            "p-f5",
            "F5Proj",
            "/nonexistent/f5-proj-folder",
            crate::db::models::ProjectHost::Base,
            "f5proj",
        )
        .expect("insert project");

        let report = app_state_set_with_db(
            &db,
            crate::commands::project_env_settings::APP_STATE_KEY_ACTIVE_EMBEDDING,
            "qwen3",
        )
        .expect("write must succeed")
        .expect("MCP-relevant key must produce a refresh report");
        assert!(
            report.skipped.contains(&"F5Proj".to_string()),
            "refresh-all must have iterated the registered projects \
             (missing-folder project lands in `skipped`); got {:?}",
            report,
        );
    }
}
