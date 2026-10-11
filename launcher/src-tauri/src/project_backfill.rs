//! Launcher-startup project-row backfill.
//!
//! v0.2.21 Step 19. Sweeps `projects` on every launcher startup and
//! ensures every row has the binding state the v0.2.21 resolver
//! endpoint (Step 14) expects to read.
//!
//! What we backfill:
//!   - `project_kg_bindings` with `role='primary'` — collection name
//!     derived from the project's canonical class prefix + the
//!     conventional `_KnowledgeGraph` suffix.
//!   - `project_codegraph_bindings` with a `collection_prefix` derived
//!     from the same canonical prefix.
//!
//! v0.2.102 W6-J: the sweep no longer seeds the per-project
//! `module_settings/orchestrator-core/active_embedding` (+ its
//! `active_embedding_source` marker) rows. ONE global embedding profile serves
//! every project (owner ruling 2026-10-11); those rows are RETIRED — existing
//! ones are left untouched and nothing reads them
//! (`project_env_settings::resolve_active_embedding_cascade`).
//!
//! What we DO NOT backfill (intentionally — these reflect user
//! choices and the access matrix is the source of truth):
//!   - `kg_collection_access`: per-project read/write/none per
//!     collection. Default access is "none" via absence of a row.
//!   - `codegraph_access`: per-project grants. Default is "no grants
//!     to or from anyone" via absence of rows.
//!
//! Soft-fail throughout. A bad project_naming::canonical_class_prefix
//! result (e.g. project name was all symbols and got rejected by the
//! regex) is logged and skipped — that single project just gets no
//! backfill, the rest of the sweep continues. The user can fix the
//! project name via the launcher GUI later and re-trigger backfill
//! by restarting the launcher.

use serde_json::Value as JsonValue;
use vct_launcher_core::db::Db;

/// Sweep every registered project and ensure binding rows + default
/// module_settings exist. Idempotent: rows that already exist are
/// left untouched.
///
/// Returns the number of projects actually mutated (rows added). Zero
/// on a clean run (no missing rows). Errors are aggregated into the
/// returned Vec but don't stop the sweep — Step 19's contract is
/// "best-effort backfill on startup", not "fail the launcher".
pub fn backfill_all_projects(db: &Db) -> BackfillReport {
    let mut report = BackfillReport::default();

    let projects = match db.list_projects() {
        Ok(rows) => rows,
        Err(e) => {
            report.errors.push(format!("list_projects: {}", e));
            return report;
        }
    };

    for p in projects {
        match backfill_one_project(db, &p) {
            Ok(touched) => report.touched_projects += touched as usize,
            Err(e) => report
                .errors
                .push(format!("project {}: {}", p.id, e)),
        }
    }

    report
}

/// Backfill outcome surfaced to the launcher's startup-banner logger.
#[derive(Debug, Default)]
pub struct BackfillReport {
    /// How many projects had at least one row added.
    pub touched_projects: usize,
    /// Non-fatal errors. The launcher logs them at WARNING level but
    /// doesn't surface them to the user — the GUI shows the same
    /// state via the per-project KgCodegraphTab if the user clicks
    /// in.
    pub errors: Vec<String>,
}

/// Returns true if any row was added for this project.
fn backfill_one_project(
    db: &Db,
    project: &vct_launcher_core::db::models::ProjectRow,
) -> Result<bool, String> {
    let mut touched = false;

    // 1. KG primary binding.
    let kg_bindings = db.list_project_kg_bindings(&project.id)?;
    let has_primary = kg_bindings
        .iter()
        .any(|b| b.role.eq_ignore_ascii_case("primary"));
    if !has_primary {
        let canonical = match crate::project_naming::canonical_class_prefix(&project.name) {
            Ok(c) => c,
            Err(e) => {
                // Naming rejected — skip this project's KG binding.
                // Don't fail the whole sweep.
                return Err(format!(
                    "cannot derive canonical class prefix from project name {:?}: {:?}",
                    project.name, e
                ));
            }
        };
        let collection_name = format!("{}_KnowledgeGraph", canonical);
        // Defaults match what install.py + the launcher's per-
        // project provisioning would set on fresh project creation.
        db.set_project_kg_binding(
            &project.id,
            "primary",
            &collection_name,
            Some(1024),
            None,            // kg_dir_path — resolved from project_path at read time
            None,            // weaviate_url — falls through to global default
            &JsonValue::Null, // config_json — no per-binding overrides
        )?;
        touched = true;
    }

    // 2. Codegraph binding.
    let cg_binding = db.get_project_codegraph_binding(&project.id)?;
    if cg_binding.is_none() {
        let canonical = match crate::project_naming::canonical_class_prefix(&project.name) {
            Ok(c) => c,
            Err(e) => {
                return Err(format!(
                    "cannot derive canonical class prefix from project name {:?}: {:?}",
                    project.name, e
                ));
            }
        };
        // Codegraph collections live under a prefix; the analyzer
        // appends `_CodeFunction`/`_CodeClass`/etc. to it.
        let prefix = canonical;
        db.set_project_codegraph_binding(
            &project.id,
            &prefix,
            Some("codesage-large-v2"),
            Some(2048),
            None,             // last_analyzed_commit
            None,             // last_analyzed_at
            true,             // enabled
            &JsonValue::Null, // config_json
        )?;
        touched = true;
    }

    Ok(touched)
}

#[cfg(test)]
mod tests {
    use super::*;
    use vct_launcher_core::db::models::ProjectHost;

    fn open_test_db() -> Db {
        Db::open_in_memory().expect("open in-memory db")
    }

    fn seed_project(db: &Db, name: &str) -> String {
        let id = uuid::Uuid::new_v4().to_string();
        let folder = format!("/tmp/test-{}", id);
        let row = db
            .insert_project(&id, name, &folder, ProjectHost::Base, name)
            .expect("insert_project");
        row.id
    }

    #[test]
    fn backfill_empty_db_returns_zero_touched() {
        let db = open_test_db();
        let r = backfill_all_projects(&db);
        assert_eq!(r.touched_projects, 0);
        assert!(r.errors.is_empty(), "errors: {:?}", r.errors);
    }

    #[test]
    fn backfill_seeds_kg_binding_when_absent() {
        let db = open_test_db();
        let pid = seed_project(&db, "TestProject");
        let r = backfill_all_projects(&db);
        assert_eq!(r.touched_projects, 1);
        assert!(r.errors.is_empty(), "errors: {:?}", r.errors);

        let bindings = db.list_project_kg_bindings(&pid).unwrap();
        let primary = bindings
            .iter()
            .find(|b| b.role.eq_ignore_ascii_case("primary"))
            .expect("primary binding seeded");
        assert_eq!(primary.collection_name, "TestProject_KnowledgeGraph");
    }

    #[test]
    fn backfill_is_idempotent() {
        let db = open_test_db();
        let _pid = seed_project(&db, "TestProject");
        let r1 = backfill_all_projects(&db);
        assert_eq!(r1.touched_projects, 1);
        let r2 = backfill_all_projects(&db);
        assert_eq!(r2.touched_projects, 0, "second run should be a no-op");
    }

    #[test]
    fn backfill_preserves_existing_user_binding() {
        let db = open_test_db();
        let pid = seed_project(&db, "TestProject");
        // User has already set a non-default collection name (e.g.,
        // migrating from another tool); backfill must NOT overwrite.
        db.set_project_kg_binding(
            &pid,
            "primary",
            "Custom_UserChosen_KG",
            Some(1024),
            None,
            None,
            &JsonValue::Null,
        )
        .unwrap();

        let r = backfill_all_projects(&db);
        // the codegraph binding still needs backfilling, so touched=1 —
        // but the KG binding name must be preserved.
        assert_eq!(r.touched_projects, 1);

        let bindings = db.list_project_kg_bindings(&pid).unwrap();
        let primary = bindings
            .iter()
            .find(|b| b.role.eq_ignore_ascii_case("primary"))
            .expect("primary binding still present");
        assert_eq!(
            primary.collection_name, "Custom_UserChosen_KG",
            "user-chosen collection name must NOT be overwritten by backfill"
        );
    }

    #[test]
    fn backfill_seeds_codegraph_binding() {
        let db = open_test_db();
        let pid = seed_project(&db, "TestProject");
        let _r = backfill_all_projects(&db);

        let cg = db
            .get_project_codegraph_binding(&pid)
            .unwrap()
            .expect("codegraph binding seeded");
        assert_eq!(cg.collection_prefix, "TestProject");
    }

    /// v0.2.102 W6-J: the sweep writes NO per-project active-embedding row
    /// (one global profile), and a retired row a pre-v0.2.102 launcher wrote
    /// is left exactly as it was (never drop user data). Red against the
    /// v0.2.71 sweep, which seeded `active_embedding` + `source=auto`.
    #[test]
    fn backfill_writes_no_per_project_embedding_row_and_keeps_retired_ones() {
        let db = open_test_db();
        let fresh = seed_project(&db, "FreshProject");
        let old = seed_project(&db, "OldProject");
        db.set_setting(
            &old,
            "orchestrator-core",
            "active_embedding",
            &JsonValue::String("openai".to_string()),
        )
        .unwrap();
        db.set_setting(
            &old,
            "orchestrator-core",
            "active_embedding_source",
            &JsonValue::String("user".to_string()),
        )
        .unwrap();

        let r = backfill_all_projects(&db);
        assert!(r.errors.is_empty(), "errors: {:?}", r.errors);
        for key in ["active_embedding", "active_embedding_source"] {
            assert!(
                db.get_setting(&fresh, "orchestrator-core", key).unwrap().is_none(),
                "{key} must not be seeded for a fresh project",
            );
        }
        assert_eq!(
            db.get_setting(&old, "orchestrator-core", "active_embedding").unwrap(),
            Some(JsonValue::String("openai".to_string())),
        );
        assert_eq!(
            db.get_setting(&old, "orchestrator-core", "active_embedding_source").unwrap(),
            Some(JsonValue::String("user".to_string())),
        );
    }

    #[test]
    fn backfill_reports_error_for_unparseable_project_name() {
        let db = open_test_db();
        // All-symbol name → canonical_class_prefix rejects it.
        let _pid = seed_project(&db, "###");
        let r = backfill_all_projects(&db);
        assert_eq!(r.touched_projects, 0);
        assert_eq!(r.errors.len(), 1, "errors: {:?}", r.errors);
        assert!(
            r.errors[0].contains("canonical class prefix"),
            "expected naming error, got: {}",
            r.errors[0]
        );
    }
}
