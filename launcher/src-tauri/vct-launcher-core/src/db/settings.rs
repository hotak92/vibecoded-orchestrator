//! Module settings — non-sensitive per-project config values stored as JSON.
//! Secrets use the OS keychain (see `crate::secrets`), not this table.

use rusqlite::{params, OptionalExtension};
use serde_json::Value;

use super::Db;

/// Canonical key for the per-project enable flag of a (potentially
/// global-scope) module. v0.2.49 Stream B — closes the gap where a
/// module installed at global scope (e.g. `vct-rl-reranker` shared
/// across the host's projects) had no way to be silenced per-project.
///
/// Value semantics: stored as a JSON boolean (`true` / `false`).
/// Default when no row exists: `true` (enabled). The reader
/// (`module_is_enabled_for_project`) treats both "row absent" and
/// "row present but malformed" as enabled, so a corrupted setting
/// can never silently turn off a module the user expects to work.
///
/// Coordination with the per-project enable toggle for *project-scope*
/// modules:
///   * The legacy `module_installs.enabled` column already gates
///     per-project-installed modules — that surface stays unchanged.
///   * This key is the *additional* gate for global-scope modules
///     where a single install row exists (or none, when the module
///     binds to the orchestrator-root project) but per-project routing
///     decisions still need a yes/no flag.
///
/// See the v0.2.49 global-install-per-project-routing plan for the
/// design rationale.
pub const MODULE_ENABLED_FOR_PROJECT_KEY: &str = "enabled_for_project";

impl Db {
    /// Read the per-project enable flag for a module. Returns `true`
    /// when the row is absent, present with `true`, or present but
    /// malformed (fail-open: a corrupted setting never silently
    /// disables a module the user expects to work).
    ///
    /// Returns `false` only when the row exists AND its value is the
    /// JSON literal `false`.
    pub fn module_is_enabled_for_project(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<bool, String> {
        match self.get_setting(project_id, module_id, MODULE_ENABLED_FOR_PROJECT_KEY)? {
            None => Ok(true),
            Some(Value::Bool(b)) => Ok(b),
            // Malformed (string, number, null, object, array) — fail open.
            // The setter only ever writes Bool, so a non-Bool here means
            // either a hand-edited row or a schema-version mismatch.
            Some(_) => Ok(true),
        }
    }

    /// Write the per-project enable flag for a module. Idempotent
    /// upsert. Always writes a JSON boolean so the reader's strict
    /// `Bool` match path is the fast path.
    pub fn module_set_enabled_for_project(
        &self,
        project_id: &str,
        module_id: &str,
        enabled: bool,
    ) -> Result<(), String> {
        self.set_setting(
            project_id,
            module_id,
            MODULE_ENABLED_FOR_PROJECT_KEY,
            &Value::Bool(enabled),
        )
    }

    /// Delete the per-project enable flag row for a module across
    /// *every* project. Called from the uninstall path of a global-
    /// scope module so the seeded rows don't outlive the module.
    /// Returns the number of rows actually removed. Idempotent.
    pub fn module_clear_enabled_for_project_all(
        &self,
        module_id: &str,
    ) -> Result<usize, String> {
        let guard = self.lock();
        let removed = guard
            .execute(
                "DELETE FROM module_settings
                  WHERE module_id = ?1 AND setting_key = ?2",
                params![module_id, MODULE_ENABLED_FOR_PROJECT_KEY],
            )
            .map_err(|e| format!("module_clear_enabled_for_project_all: {}", e))?;
        Ok(removed)
    }
}

impl Db {
    pub fn get_setting(
        &self,
        project_id: &str,
        module_id: &str,
        key: &str,
    ) -> Result<Option<Value>, String> {
        let guard = self.lock();
        let row: Option<String> = guard
            .query_row(
                "SELECT setting_value FROM module_settings
                  WHERE project_id = ?1 AND module_id = ?2 AND setting_key = ?3",
                params![project_id, module_id, key],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| format!("get setting: {}", e))?;

        match row {
            None => Ok(None),
            Some(s) => serde_json::from_str(&s)
                .map(Some)
                .map_err(|e| format!("parse setting json: {}", e)),
        }
    }

    pub fn set_setting(
        &self,
        project_id: &str,
        module_id: &str,
        key: &str,
        value: &Value,
    ) -> Result<(), String> {
        let encoded = serde_json::to_string(value)
            .map_err(|e| format!("encode setting: {}", e))?;
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO module_settings (project_id, module_id, setting_key, setting_value)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(project_id, module_id, setting_key)
                 DO UPDATE SET setting_value = excluded.setting_value",
                params![project_id, module_id, key, encoded],
            )
            .map_err(|e| format!("set setting: {}", e))?;
        Ok(())
    }

    pub fn list_module_settings(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<Vec<(String, Value)>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT setting_key, setting_value FROM module_settings
                  WHERE project_id = ?1 AND module_id = ?2
               ORDER BY setting_key ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id, module_id], |r| {
                let key: String = r.get(0)?;
                let raw: String = r.get(1)?;
                Ok((key, raw))
            })
            .map_err(|e| format!("query: {}", e))?;

        let mut out = Vec::new();
        for row in rows {
            let (key, raw) = row.map_err(|e| format!("row: {}", e))?;
            let val: Value =
                serde_json::from_str(&raw).map_err(|e| format!("parse '{}': {}", key, e))?;
            out.push((key, val));
        }
        Ok(out)
    }

    pub fn clear_module_settings(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM module_settings WHERE project_id = ?1 AND module_id = ?2",
                params![project_id, module_id],
            )
            .map_err(|e| format!("clear settings: {}", e))?;
        Ok(())
    }

    /// Delete a single setting row. Idempotent: returns Ok(()) whether or
    /// not the row existed. Used by the SHARED_KG_OPT_OUT → SHARED_KG_WRITE_DISABLED
    /// migration helper to retire the legacy key after copying its value.
    pub fn delete_setting(
        &self,
        project_id: &str,
        module_id: &str,
        key: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM module_settings
                  WHERE project_id = ?1 AND module_id = ?2 AND setting_key = ?3",
                params![project_id, module_id, key],
            )
            .map_err(|e| format!("delete setting: {}", e))?;
        Ok(())
    }
}

// ─── v0.2.49 Stream B — per-project enable toggle tests ──────────────────
#[cfg(test)]
mod enable_toggle_tests {
    use super::*;
    use crate::db::models::ProjectHost;

    fn db_with_project(slug: &str) -> (Db, String) {
        let db = Db::open_in_memory().expect("in-memory db");
        let id = format!("proj-{}", slug);
        db.insert_project(
            &id,
            &format!("Test {}", slug),
            &format!("/tmp/{}", slug),
            ProjectHost::Base,
            slug,
        )
        .expect("insert project");
        (db, id)
    }

    /// Default (no row): module reads as enabled. This is the
    /// fail-open contract: a fresh install must never start in a
    /// disabled state just because the seeding step skipped this
    /// (project, module) pair.
    #[test]
    fn module_is_enabled_for_project_default_true_when_row_absent() {
        let (db, pid) = db_with_project("a");
        let enabled = db
            .module_is_enabled_for_project(&pid, "vct-rl-reranker")
            .expect("read");
        assert!(
            enabled,
            "absent row must read as enabled (fail-open default)"
        );
    }

    /// Set true → reads true. Set false → reads false. The setter
    /// is the canonical writer for the toggle.
    #[test]
    fn module_set_enabled_for_project_roundtrip() {
        let (db, pid) = db_with_project("b");

        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", true)
            .expect("write true");
        assert!(db
            .module_is_enabled_for_project(&pid, "vct-rl-reranker")
            .unwrap());

        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", false)
            .expect("write false");
        assert!(!db
            .module_is_enabled_for_project(&pid, "vct-rl-reranker")
            .unwrap());

        // Idempotent re-write of the same value.
        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", false)
            .expect("write false again");
        assert!(!db
            .module_is_enabled_for_project(&pid, "vct-rl-reranker")
            .unwrap());

        // Flip back.
        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", true)
            .expect("flip back");
        assert!(db
            .module_is_enabled_for_project(&pid, "vct-rl-reranker")
            .unwrap());
    }

    /// Toggles for distinct (project, module) pairs do not interfere.
    /// Guards against a regression where the SQL WHERE clause drops
    /// a discriminator and one project's disable silently affects
    /// another's enable.
    #[test]
    fn module_enabled_isolation_between_projects_and_modules() {
        let (db, pid_a) = db_with_project("iso-a");
        let pid_b = "proj-iso-b".to_string();
        db.insert_project(
            &pid_b,
            "Test iso-b",
            "/tmp/iso-b",
            ProjectHost::Base,
            "iso-b",
        )
        .unwrap();

        db.module_set_enabled_for_project(&pid_a, "vct-rl-reranker", false)
            .unwrap();
        db.module_set_enabled_for_project(&pid_a, "vct-coordination", true)
            .unwrap();
        db.module_set_enabled_for_project(&pid_b, "vct-rl-reranker", true)
            .unwrap();

        assert!(!db
            .module_is_enabled_for_project(&pid_a, "vct-rl-reranker")
            .unwrap());
        assert!(db
            .module_is_enabled_for_project(&pid_a, "vct-coordination")
            .unwrap());
        assert!(db
            .module_is_enabled_for_project(&pid_b, "vct-rl-reranker")
            .unwrap());
        // Unrelated (project_b, vct-coordination) untouched → default
        // true.
        assert!(db
            .module_is_enabled_for_project(&pid_b, "vct-coordination")
            .unwrap());
    }

    /// `module_clear_enabled_for_project_all` removes every row for a
    /// given module across all projects, leaving other modules' rows
    /// alone. This is the uninstall path's cleanup hook for global-
    /// scope modules.
    #[test]
    fn module_clear_enabled_for_project_all_removes_only_target_module() {
        let (db, pid_a) = db_with_project("clr-a");
        let pid_b = "proj-clr-b".to_string();
        db.insert_project(&pid_b, "B", "/tmp/clr-b", ProjectHost::Base, "clr-b")
            .unwrap();

        // Seed 2 projects × 2 modules so we can prove cross-row safety.
        db.module_set_enabled_for_project(&pid_a, "vct-rl-reranker", true)
            .unwrap();
        db.module_set_enabled_for_project(&pid_b, "vct-rl-reranker", false)
            .unwrap();
        db.module_set_enabled_for_project(&pid_a, "vct-coordination", false)
            .unwrap();
        db.module_set_enabled_for_project(&pid_b, "vct-coordination", true)
            .unwrap();

        let removed = db
            .module_clear_enabled_for_project_all("vct-rl-reranker")
            .expect("clear");
        assert_eq!(removed, 2, "two rl-reranker rows removed");

        // RL rows gone → default reads true everywhere.
        assert!(db
            .module_is_enabled_for_project(&pid_a, "vct-rl-reranker")
            .unwrap());
        assert!(db
            .module_is_enabled_for_project(&pid_b, "vct-rl-reranker")
            .unwrap());

        // Coordination rows untouched (still explicit values, not defaults).
        assert!(!db
            .module_is_enabled_for_project(&pid_a, "vct-coordination")
            .unwrap());
        assert!(db
            .module_is_enabled_for_project(&pid_b, "vct-coordination")
            .unwrap());

        // Re-clearing is a no-op (idempotent).
        let removed_again = db
            .module_clear_enabled_for_project_all("vct-rl-reranker")
            .expect("clear again");
        assert_eq!(removed_again, 0);
    }

    /// Malformed value (non-bool JSON) must read as enabled — fail-open
    /// per the docstring. The setter would never write this, but a
    /// hand-edited row or a stale schema version could.
    #[test]
    fn module_is_enabled_for_project_fail_open_on_malformed_value() {
        let (db, pid) = db_with_project("mal");

        // Inject a non-bool JSON value directly via the generic setter.
        db.set_setting(
            &pid,
            "vct-rl-reranker",
            MODULE_ENABLED_FOR_PROJECT_KEY,
            &serde_json::json!("disabled"),
        )
        .expect("inject string");

        let enabled = db
            .module_is_enabled_for_project(&pid, "vct-rl-reranker")
            .expect("read");
        assert!(
            enabled,
            "malformed value (string) must fail open to enabled — \
             matches the docstring contract"
        );

        // Same for null.
        db.set_setting(
            &pid,
            "vct-rl-reranker",
            MODULE_ENABLED_FOR_PROJECT_KEY,
            &serde_json::Value::Null,
        )
        .unwrap();
        assert!(db
            .module_is_enabled_for_project(&pid, "vct-rl-reranker")
            .unwrap());
    }

    /// Setter always writes a JSON boolean, regardless of caller
    /// hygiene. This is the contract that lets the reader's strict
    /// `Bool` match be the fast path. Verified by reading the raw
    /// stored value through `list_module_settings`.
    #[test]
    fn module_set_enabled_for_project_always_writes_boolean() {
        let (db, pid) = db_with_project("bool");
        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", true)
            .unwrap();
        let settings = db
            .list_module_settings(&pid, "vct-rl-reranker")
            .expect("list");
        let row = settings
            .iter()
            .find(|(k, _)| k == MODULE_ENABLED_FOR_PROJECT_KEY)
            .expect("row exists");
        assert!(matches!(row.1, Value::Bool(true)));

        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", false)
            .unwrap();
        let settings = db
            .list_module_settings(&pid, "vct-rl-reranker")
            .expect("list");
        let row = settings
            .iter()
            .find(|(k, _)| k == MODULE_ENABLED_FOR_PROJECT_KEY)
            .expect("row exists");
        assert!(matches!(row.1, Value::Bool(false)));
    }
}
