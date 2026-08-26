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

    // ─── v0.2.52 V52-AD — host-wide GLOBAL toggle ───────────────────────
    //
    // Stored as a row in `module_settings` with `project_id IS NULL` —
    // see migration 034's docstring. The reader cascade is:
    //
    //     effective_enabled =
    //         per_project_setting (project_id = $project)
    //             .unwrap_or(global_default (project_id IS NULL))
    //             .unwrap_or(true)   -- fail-open
    //
    // The `enabled_for_project` setting_key is reused (NOT a new key) so
    // a single uninstall-time DELETE WHERE setting_key = ... still cleans
    // both per-project AND global rows.

    /// Read the GLOBAL (host-wide) enable flag for a module. Returns
    /// `None` when no global row exists (caller should fall back to the
    /// system default — typically `true`). Returns `Some(false)` only
    /// when the row exists AND its value is the JSON literal `false`.
    /// Malformed values fail open to `Some(true)` per the same contract
    /// as the per-project reader.
    pub fn module_global_enabled(
        &self,
        module_id: &str,
    ) -> Result<Option<bool>, String> {
        let guard = self.lock();
        let row: Option<String> = guard
            .query_row(
                "SELECT setting_value FROM module_settings
                  WHERE project_id IS NULL
                    AND module_id = ?1
                    AND setting_key = ?2",
                params![module_id, MODULE_ENABLED_FOR_PROJECT_KEY],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| format!("module_global_enabled read: {}", e))?;

        match row {
            None => Ok(None),
            Some(s) => match serde_json::from_str::<Value>(&s) {
                Ok(Value::Bool(b)) => Ok(Some(b)),
                // Malformed → fail open to enabled (matches per-project
                // contract: a corrupted setting never silently disables).
                Ok(_) | Err(_) => Ok(Some(true)),
            },
        }
    }

    /// Write the GLOBAL (host-wide) enable flag for a module. Idempotent
    /// upsert. Always writes a JSON boolean. The conflict target is the
    /// partial unique index `idx_ms_unique_global` (migration 034) so
    /// the standard `ON CONFLICT(project_id, module_id, setting_key)`
    /// shape used by `set_setting` would NOT trigger here — we use an
    /// explicit DELETE + INSERT to keep the upsert semantics clear and
    /// independent of the partial-index conflict target.
    pub fn module_set_global_enabled(
        &self,
        module_id: &str,
        enabled: bool,
    ) -> Result<(), String> {
        let encoded = serde_json::to_string(&Value::Bool(enabled))
            .map_err(|e| format!("module_set_global_enabled encode: {}", e))?;
        let guard = self.lock();
        // Single transaction: delete any pre-existing global row, then
        // insert the new one. Cheaper than reasoning about partial-
        // index ON CONFLICT semantics, and the partial unique index
        // still enforces correctness if a concurrent writer races.
        let tx = guard
            .unchecked_transaction()
            .map_err(|e| format!("module_set_global_enabled txn: {}", e))?;
        tx.execute(
            "DELETE FROM module_settings
              WHERE project_id IS NULL
                AND module_id = ?1
                AND setting_key = ?2",
            params![module_id, MODULE_ENABLED_FOR_PROJECT_KEY],
        )
        .map_err(|e| format!("module_set_global_enabled delete: {}", e))?;
        tx.execute(
            "INSERT INTO module_settings (project_id, module_id, setting_key, setting_value)
             VALUES (NULL, ?1, ?2, ?3)",
            params![module_id, MODULE_ENABLED_FOR_PROJECT_KEY, encoded],
        )
        .map_err(|e| format!("module_set_global_enabled insert: {}", e))?;
        tx.commit()
            .map_err(|e| format!("module_set_global_enabled commit: {}", e))?;
        Ok(())
    }

    /// Effective enable flag for a (project, module) pair using the
    /// v0.2.52 V52-AD cascade. Reader contract:
    ///
    /// 1. If a per-project row exists for `(project_id, module_id,
    ///    enabled_for_project)`, return its boolean value (fail-open
    ///    on malformed values).
    /// 2. Else if a GLOBAL row exists (`project_id IS NULL`), return
    ///    its boolean value (same fail-open contract).
    /// 3. Else return `true` (system default — fail-open).
    ///
    /// This is the function the hub resolver should call when deciding
    /// `rl_reranker_enabled_for_project`. The legacy
    /// `module_is_enabled_for_project` still works (collapses the
    /// cascade after step 1 → step 3) and is preserved for any caller
    /// that doesn't want the global default factored in.
    pub fn module_effective_enabled(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<bool, String> {
        // Step 1: per-project row (explicit override).
        if let Some(value) =
            self.get_setting(project_id, module_id, MODULE_ENABLED_FOR_PROJECT_KEY)?
        {
            return match value {
                Value::Bool(b) => Ok(b),
                // Malformed → fail open (matches per-project contract).
                _ => Ok(true),
            };
        }
        // Step 2: global fallback (host-wide default).
        if let Some(b) = self.module_global_enabled(module_id)? {
            return Ok(b);
        }
        // Step 3: system default.
        Ok(true)
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
        // v0.2.52 V52-AD — the table-level UNIQUE(project_id, module_id,
        // setting_key) was dropped by migration 034 (partial-index
        // replacement; see migration docstring). For SQLite's upsert
        // semantics to target the surviving partial index
        // `idx_ms_unique_per_project`, the ON CONFLICT clause must
        // include the partial index's WHERE predicate. Pre-034 callers
        // (passing a non-NULL project_id) behave identically — the
        // partial-index WHERE matches every such row.
        guard
            .execute(
                "INSERT INTO module_settings (project_id, module_id, setting_key, setting_value)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(project_id, module_id, setting_key)
                   WHERE project_id IS NOT NULL
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

    /// v0.2.91 WP-F2 — read ONE setting key across EVERY project, as booleans.
    ///
    /// Exists for the tray-preference re-homing migration: `tray_close_to_tray`
    /// and `tray_start_minimized` are launcher-GLOBAL window behaviours but the
    /// Preferences page persisted them per selected project, so the legacy
    /// value could be sitting under any project id. The migration adopts them
    /// into `app_state` once (see `quit_dialog::adopt_legacy_pref`).
    ///
    /// Non-boolean rows are skipped rather than coerced — a value the launcher
    /// never wrote is not evidence of a user's choice. Returns rows in
    /// `project_id` order for deterministic logging; the adoption decision
    /// itself is order-independent.
    pub fn find_all_project_settings_bool(
        &self,
        module_id: &str,
        key: &str,
    ) -> Result<Vec<bool>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT setting_value FROM module_settings
                  WHERE module_id = ?1 AND setting_key = ?2 AND project_id IS NOT NULL
               ORDER BY project_id ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![module_id, key], |r| r.get::<_, String>(0))
            .map_err(|e| format!("query: {}", e))?;

        let mut out = Vec::new();
        for row in rows {
            let raw = row.map_err(|e| format!("row: {}", e))?;
            if let Ok(Value::Bool(b)) = serde_json::from_str::<Value>(&raw) {
                out.push(b);
            }
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

    // ─── v0.2.52 V52-AD — global toggle tests ────────────────────────────

    /// Global default reads as `None` when no row exists. The hub
    /// resolver translates that to the system default (`true`).
    #[test]
    fn module_global_enabled_returns_none_when_row_absent() {
        let db = Db::open_in_memory().expect("in-memory db");
        let result = db.module_global_enabled("vct-rl-reranker").expect("read");
        assert!(
            result.is_none(),
            "absent global row must read as None (caller picks default)"
        );
    }

    /// Set false at global level → reads as Some(false). Set true →
    /// Some(true). Roundtrip pins the setter/reader contract for the
    /// `project_id IS NULL` rows added by migration 034.
    #[test]
    fn module_set_global_enabled_roundtrip() {
        let db = Db::open_in_memory().expect("in-memory db");

        db.module_set_global_enabled("vct-rl-reranker", false)
            .expect("write false");
        assert_eq!(
            db.module_global_enabled("vct-rl-reranker").unwrap(),
            Some(false),
            "explicit global disable must surface"
        );

        db.module_set_global_enabled("vct-rl-reranker", true)
            .expect("write true");
        assert_eq!(
            db.module_global_enabled("vct-rl-reranker").unwrap(),
            Some(true),
        );

        // Idempotent re-write of the same value (delete+insert path).
        db.module_set_global_enabled("vct-rl-reranker", true)
            .expect("write true again");
        assert_eq!(
            db.module_global_enabled("vct-rl-reranker").unwrap(),
            Some(true),
        );
    }

    /// `module_effective_enabled` cascade:
    ///   no per-project row, no global row → true (fail-open default)
    ///   no per-project row, global=false → false (global wins)
    ///   no per-project row, global=true  → true
    ///   per-project=true, global=false   → true (per-project overrides)
    ///   per-project=false, global=true   → false (per-project overrides)
    #[test]
    fn module_effective_enabled_cascade() {
        let (db, pid) = db_with_project("eff");

        // (a) Both absent → default true.
        assert!(
            db.module_effective_enabled(&pid, "vct-rl-reranker").unwrap(),
            "no rows → fail-open default true"
        );

        // (b) Global=false, no per-project → false (global default applies).
        db.module_set_global_enabled("vct-rl-reranker", false)
            .unwrap();
        assert!(
            !db.module_effective_enabled(&pid, "vct-rl-reranker").unwrap(),
            "global default false must propagate when no per-project row"
        );

        // (c) Per-project=true, global=false → true (per-project overrides).
        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", true)
            .unwrap();
        assert!(
            db.module_effective_enabled(&pid, "vct-rl-reranker").unwrap(),
            "per-project enable must override global disable"
        );

        // (d) Per-project=false, global=false → false (both agree).
        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", false)
            .unwrap();
        assert!(
            !db.module_effective_enabled(&pid, "vct-rl-reranker").unwrap(),
            "both false → false"
        );

        // (e) Per-project=false, global=true → false (per-project overrides).
        db.module_set_global_enabled("vct-rl-reranker", true)
            .unwrap();
        assert!(
            !db.module_effective_enabled(&pid, "vct-rl-reranker").unwrap(),
            "per-project disable must override global enable"
        );
    }

    /// Global rows are independent across distinct module_ids.
    #[test]
    fn module_global_enabled_isolation_between_modules() {
        let db = Db::open_in_memory().expect("in-memory db");

        db.module_set_global_enabled("vct-rl-reranker", false)
            .unwrap();
        db.module_set_global_enabled("vct-coordination", true)
            .unwrap();

        assert_eq!(
            db.module_global_enabled("vct-rl-reranker").unwrap(),
            Some(false),
        );
        assert_eq!(
            db.module_global_enabled("vct-coordination").unwrap(),
            Some(true),
        );
        // Third unrelated module reads None.
        assert!(db.module_global_enabled("vct-other").unwrap().is_none());
    }

    /// Global row survives a project deletion (FK cascade only fires
    /// for non-NULL project_id rows). Critical correctness property:
    /// dropping a project must NOT silently re-enable a globally-
    /// disabled module across the rest of the host.
    #[test]
    fn module_global_enabled_survives_project_delete() {
        let (db, pid) = db_with_project("survive");
        db.module_set_global_enabled("vct-rl-reranker", false)
            .unwrap();
        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", true)
            .unwrap();

        // Sanity: per-project row exists pre-delete.
        assert!(db
            .module_is_enabled_for_project(&pid, "vct-rl-reranker")
            .unwrap());

        // Delete the project (FK ON DELETE CASCADE wipes per-project
        // rows for that project_id, leaves NULL rows intact).
        db.lock()
            .execute(
                "DELETE FROM projects WHERE id = ?1",
                rusqlite::params![&pid],
            )
            .expect("delete project");

        // Global row still alive.
        assert_eq!(
            db.module_global_enabled("vct-rl-reranker").unwrap(),
            Some(false),
            "global row (project_id IS NULL) must NOT cascade-delete with a project"
        );
    }

    /// `module_clear_enabled_for_project_all` (the uninstall-time
    /// cleanup helper) ALSO clears the global row, because it uses
    /// `WHERE module_id = ? AND setting_key = ?` with no project_id
    /// filter. This is correct behaviour: when a module is uninstalled
    /// host-wide, all its enable rows (per-project + global) should go.
    #[test]
    fn module_clear_enabled_for_project_all_also_clears_global_row() {
        let db = Db::open_in_memory().expect("in-memory db");
        db.module_set_global_enabled("vct-rl-reranker", false)
            .unwrap();

        let removed = db
            .module_clear_enabled_for_project_all("vct-rl-reranker")
            .expect("clear");
        assert_eq!(removed, 1, "global row counted in the cleanup");
        assert!(
            db.module_global_enabled("vct-rl-reranker")
                .unwrap()
                .is_none(),
            "global row was cleared"
        );
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
