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
//!   - `module_settings` keys: `active_embedding` (derived from the
//!     machine's hardware pick `app_state[default_text_embedding]`; falls
//!     to `qwen3` when the pick is absent/unmapped) PLUS its provenance
//!     companion `active_embedding_source` (v0.2.71 T-B-emb). NEW rows are
//!     seeded with `source="auto"`; legacy rows missing the marker get a
//!     `source="auto"` backfill (value left untouched — the resolver
//!     derives non-user rows from the global default); a `source="user"`
//!     row (a deliberate Settings-tab pick) is NEVER touched. This
//!     supersedes the brittle v0.2.69 FIX 1 "==qwen3" self-heal heuristic
//!     (provenance, not value, now drives resolution).
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

use crate::commands::openai_cmd::APP_STATE_DEFAULT_TEXT_EMBED;
use crate::commands::project_env_settings::{
    active_profile_for_model, ACTIVE_EMBEDDING_SETTING_KEY, ACTIVE_EMBEDDING_SOURCE_AUTO,
    ACTIVE_EMBEDDING_SOURCE_SETTING_KEY, ACTIVE_EMBEDDING_SOURCE_USER, ORCHESTRATOR_CORE_MODULE_ID,
};

/// The conservative `active_embedding` seed when the hardware pick is absent
/// or maps to no known profile. Stamped together with a `source=auto` marker
/// so the resolver knows this is an inherited default, not a deliberate pick.
const DEFAULT_AUTO_ACTIVE_EMBEDDING: &str = "qwen3";

/// Resolve the per-project `active_embedding` seed from the machine's
/// hardware pick.
///
/// Reads `app_state[default_text_embedding]` and maps it via the shared
/// `active_profile_for_model` table (single source — see
/// `project_env_settings.rs`). Returns `None` when the hardware pick is
/// absent or maps to no known profile: callers then keep the conservative
/// `"qwen3"` default rather than stamping a guessed profile (a wrong slot
/// indexes the KG against the wrong vector — the 2026-04-30 audit bug class).
fn derive_active_embedding_seed(db: &Db) -> Option<&'static str> {
    let model_id = db.app_state_get(APP_STATE_DEFAULT_TEXT_EMBED).ok().flatten()?;
    active_profile_for_model(&model_id)
}

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
            Some("qwen3-embedding:0.6b"),
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

    // 3. active_embedding default + provenance marker — v0.2.71 T-B-emb
    //    (supersedes the brittle v0.2.69 FIX 1 "==qwen3" self-heal heuristic).
    //
    // The production env writer (`config_projection.project_env_from_db`)
    // reads THIS `module_settings/orchestrator-core/active_embedding` row,
    // NOT the canonical `app_state[embedding.active_profile]` key.
    //
    // PROVENANCE MODEL: a companion `active_embedding_source` row records
    // whether the value is a deliberate user pick ("user", written by the
    // Settings-tab picker) or an inherited auto-seed ("auto", written here).
    // The resolver (`resolve_active_embedding_cascade`) makes a "user" row
    // STICKY and treats "auto" / legacy-no-marker as "inherit the global
    // default". So the backfill no longer needs to guess from the value —
    // it just (a) seeds NEW rows with source=auto, and (b) NEVER touches a
    // source=user row.
    //
    // (a) NEW row (no value row yet): seed the value from the hardware pick
    //     (`app_state[default_text_embedding]` → profile), falling to "qwen3"
    //     only when the pick is absent / unmapped (conservative — never stamp
    //     a guess). Stamp source=auto alongside.
    //
    // (b) EXISTING row WITHOUT a source=user marker (a prior auto-seed OR a
    //     legacy NO-marker row): the value itself is irrelevant to resolution
    //     now (auto/legacy both inherit the global default), so we DON'T
    //     rewrite the value. We only BACKFILL the missing source=auto marker
    //     so the provenance is explicit going forward. We do NOT flip the
    //     stored value — the cascade ignores it for non-user rows.
    //
    // (c) EXISTING source=user row: left ENTIRELY untouched (sticky pick).
    let existing_source = db
        .get_setting(&project.id, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SOURCE_SETTING_KEY)?
        .and_then(|v| v.as_str().map(String::from));
    if existing_source.as_deref() == Some(ACTIVE_EMBEDDING_SOURCE_USER) {
        // (c) Deliberate user pick — never reseed or re-mark.
        return Ok(touched);
    }

    let existing_value =
        db.get_setting(&project.id, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SETTING_KEY)?;
    let derived_seed = derive_active_embedding_seed(db);

    if existing_value.is_none() {
        // (a) Fresh seed.
        let seed = derived_seed.unwrap_or(DEFAULT_AUTO_ACTIVE_EMBEDDING);
        db.set_setting(
            &project.id,
            ORCHESTRATOR_CORE_MODULE_ID,
            ACTIVE_EMBEDDING_SETTING_KEY,
            &JsonValue::String(seed.to_string()),
        )?;
        db.set_setting(
            &project.id,
            ORCHESTRATOR_CORE_MODULE_ID,
            ACTIVE_EMBEDDING_SOURCE_SETTING_KEY,
            &JsonValue::String(ACTIVE_EMBEDDING_SOURCE_AUTO.to_string()),
        )?;
        touched = true;
    } else if existing_source.is_none() {
        // (b) Legacy value row with NO source marker — backfill the marker
        //     to make the (already-effective) "inherit global" provenance
        //     explicit. The stored value is intentionally NOT rewritten; the
        //     cascade derives from the global default for non-user rows.
        db.set_setting(
            &project.id,
            ORCHESTRATOR_CORE_MODULE_ID,
            ACTIVE_EMBEDDING_SOURCE_SETTING_KEY,
            &JsonValue::String(ACTIVE_EMBEDDING_SOURCE_AUTO.to_string()),
        )?;
        touched = true;
    }
    // else: value present + source already "auto" → fully backfilled, no-op.

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
            Some("qwen3-embedding:0.6b"),
            Some(1024),
            None,
            None,
            &JsonValue::Null,
        )
        .unwrap();

        let r = backfill_all_projects(&db);
        // codegraph + active_embedding still need backfilling, so
        // touched=1 — but the KG binding name must be preserved.
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

    #[test]
    fn backfill_seeds_default_active_embedding_without_hardware_pick() {
        // No `default_text_embedding` app_state key → the conservative
        // qwen3 default is retained (never guess), with a source=auto marker.
        let db = open_test_db();
        let pid = seed_project(&db, "TestProject");
        let _r = backfill_all_projects(&db);

        let s = db
            .get_setting(&pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SETTING_KEY)
            .unwrap()
            .expect("active_embedding seeded");
        assert_eq!(s, "qwen3");
        let src = db
            .get_setting(&pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SOURCE_SETTING_KEY)
            .unwrap()
            .expect("source marker seeded");
        assert_eq!(src, ACTIVE_EMBEDDING_SOURCE_AUTO);
    }

    #[test]
    fn backfill_seeds_active_embedding_derived_from_hardware_pick() {
        // A fresh project's seed must derive from the machine's hardware
        // pick (arctic), NOT a blanket "qwen3", and be marked source=auto.
        let db = open_test_db();
        db.app_state_set(APP_STATE_DEFAULT_TEXT_EMBED, "snowflake-arctic-embed2:latest")
            .unwrap();
        let pid = seed_project(&db, "Example_Arctic");
        let _r = backfill_all_projects(&db);

        let s = db
            .get_setting(&pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SETTING_KEY)
            .unwrap()
            .expect("active_embedding seeded");
        assert_eq!(s, "arctic");
        let src = db
            .get_setting(&pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SOURCE_SETTING_KEY)
            .unwrap()
            .expect("source marker seeded");
        assert_eq!(src, ACTIVE_EMBEDDING_SOURCE_AUTO);
    }

    #[test]
    fn backfill_unknown_hardware_pick_seeds_qwen3() {
        // Conservative guard: an unmapped hardware pick must NOT stamp a
        // guessed profile on a NEW row — qwen3 default is used (source=auto).
        let db = open_test_db();
        db.app_state_set(APP_STATE_DEFAULT_TEXT_EMBED, "some-future-model")
            .unwrap();
        let pid = seed_project(&db, "Example_Unknown");
        let _r = backfill_all_projects(&db);

        let s = db
            .get_setting(&pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SETTING_KEY)
            .unwrap()
            .expect("active_embedding seeded");
        assert_eq!(s, "qwen3");
    }

    #[test]
    fn backfill_legacy_no_marker_row_gets_auto_marker_value_untouched() {
        // v0.2.71 T-B-emb: a legacy "qwen3" value row with NO source marker
        // (a pre-v0.2.71 auto-seed) gets a source=auto marker backfilled. The
        // VALUE is intentionally NOT rewritten (the cascade derives from the
        // global default for non-user rows — see resolve_active_embedding_cascade).
        let db = open_test_db();
        db.app_state_set(APP_STATE_DEFAULT_TEXT_EMBED, "snowflake-arctic-embed2:latest")
            .unwrap();
        let pid = seed_project(&db, "Example_Legacy");
        // Simulate the pre-v0.2.71 backfill having stamped qwen3 with no marker.
        db.set_setting(
            &pid,
            ORCHESTRATOR_CORE_MODULE_ID,
            ACTIVE_EMBEDDING_SETTING_KEY,
            &JsonValue::String("qwen3".to_string()),
        )
        .unwrap();

        let _r = backfill_all_projects(&db);

        // Value unchanged (qwen3) — provenance, not value, drives resolution now.
        let s = db
            .get_setting(&pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SETTING_KEY)
            .unwrap()
            .expect("active_embedding present");
        assert_eq!(s, "qwen3", "legacy value row must NOT be rewritten");
        // ...but the source marker is now explicitly auto.
        let src = db
            .get_setting(&pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SOURCE_SETTING_KEY)
            .unwrap()
            .expect("source marker backfilled");
        assert_eq!(src, ACTIVE_EMBEDDING_SOURCE_AUTO);
    }

    #[test]
    fn backfill_never_touches_source_user_row() {
        // v0.2.71 T-B-emb: a deliberate Settings-tab user pick (source=user)
        // is NEVER reseeded or re-marked, even when the hardware pick differs.
        let db = open_test_db();
        db.app_state_set(APP_STATE_DEFAULT_TEXT_EMBED, "snowflake-arctic-embed2:latest")
            .unwrap();
        let pid = seed_project(&db, "Example_UserPick");
        // User deliberately chose openai via the picker (value + source=user).
        db.set_setting(
            &pid,
            ORCHESTRATOR_CORE_MODULE_ID,
            ACTIVE_EMBEDDING_SETTING_KEY,
            &JsonValue::String("openai".to_string()),
        )
        .unwrap();
        db.set_setting(
            &pid,
            ORCHESTRATOR_CORE_MODULE_ID,
            ACTIVE_EMBEDDING_SOURCE_SETTING_KEY,
            &JsonValue::String(ACTIVE_EMBEDDING_SOURCE_USER.to_string()),
        )
        .unwrap();

        let _r = backfill_all_projects(&db);

        let s = db
            .get_setting(&pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SETTING_KEY)
            .unwrap()
            .expect("active_embedding present");
        assert_eq!(s, "openai", "explicit user pick must be preserved");
        let src = db
            .get_setting(&pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SOURCE_SETTING_KEY)
            .unwrap()
            .expect("source present");
        assert_eq!(src, ACTIVE_EMBEDDING_SOURCE_USER, "user marker must be preserved");
    }

    #[test]
    fn backfill_fully_marked_auto_row_is_no_op() {
        // A row already carrying value + source=auto is fully backfilled —
        // the second run must be a no-op (preserves idempotency).
        let db = open_test_db();
        db.app_state_set(APP_STATE_DEFAULT_TEXT_EMBED, "qwen3-embedding:0.6b")
            .unwrap();
        let pid = seed_project(&db, "Example_Qwen3");

        // First run seeds value + source=auto (plus KG + codegraph bindings).
        let _r1 = backfill_all_projects(&db);
        let r2 = backfill_all_projects(&db);
        assert_eq!(r2.touched_projects, 0, "second run must be a no-op");

        let s = db
            .get_setting(&pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SETTING_KEY)
            .unwrap()
            .expect("active_embedding present");
        assert_eq!(s, "qwen3");
        let src = db
            .get_setting(&pid, ORCHESTRATOR_CORE_MODULE_ID, ACTIVE_EMBEDDING_SOURCE_SETTING_KEY)
            .unwrap()
            .expect("source present");
        assert_eq!(src, ACTIVE_EMBEDDING_SOURCE_AUTO);
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
