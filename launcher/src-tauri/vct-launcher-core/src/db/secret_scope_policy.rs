// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Env-migration scope policy (GAP-1) + shared-secrets read gate (GAP-2).
//!
//! ## S1 — `decide_env_migration_scope`
//!
//! The SINGLE place that decides WHERE a `.env`-migrated secret lands. Every
//! caller (the hub `/secrets/migrate` handler, the GUI import panel, the
//! install.py CLI arm via the hub) forwards a `project_id` only — none of
//! them re-implement the rule. Python has NO mirror of this rule at all: it
//! reaches the decision via the hub API (A-level of the A>B>C sharing rule).
//!
//! Decision table:
//!
//! | `project_id` input          | result                                   |
//! |-----------------------------|------------------------------------------|
//! | `None`                      | `Shared` — preserves the V47-C           |
//! |                             | pre-registration contract (install.py    |
//! |                             | runs on a fresh adopt before the project |
//! |                             | is registered, so it has no id to send)  |
//! | `Some(id)`, project unknown | `Err(project_not_found)` — LOUD FAIL,    |
//! |                             | nothing written (an explicit-but-unknown |
//! |                             | id is a caller bug; guessing Shared      |
//! |                             | would recreate the cross-tenant leak)    |
//! | `Some(id)`, host = root     | `Shared` — orchestrator-root secrets are |
//! |                             | legitimately machine-wide (user-stated)  |
//! | `Some(id)`, host = base/mao | `PerProject(id)` — the owning project's  |
//! |                             | scope                                    |
//!
//! ## S2 — `shared_secrets_read_disabled`
//!
//! The bulk opt-out gate, mirroring the shared-KG read gate. Backed by
//! `module_settings(project_id, ORCHESTRATOR_CORE_MODULE_ID,
//! SHARED_SECRETS_READ_DISABLED)` — the SAME storage + canonical module_id
//! the KG gates use (see `db/module_settings_keys.rs`). Soft-fail: a missing
//! row / parse error / DB error all resolve to `false` (reads allowed), the
//! conservative default that never breaks a project that never toggled it.

use crate::db::models::ProjectHost;
use crate::db::module_settings_keys::{
    ORCHESTRATOR_CORE_MODULE_ID, SETTING_KEY_SHARED_SECRETS_READ_DISABLED,
};
use crate::db::Db;

/// Where a `.env`-migrated secret should land. `PerProject` carries the
/// owning project's id so the caller writes the correct keychain slot +
/// active-flag row without re-deriving anything.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EnvMigrationScope {
    /// The user-shared bucket (`SecretScope::Shared { SENTINEL_SHARED }`).
    Shared,
    /// The named project's own bucket (`SecretScope::PerProject { id }`).
    PerProject(String),
}

/// Decide the migration scope for a `.env`-scraped secret set.
///
/// See the module docstring for the full decision table. `project_id = None`
/// is the ONLY path that yields `Shared` for a caller that could have sent an
/// id — it exists solely for the pre-registration install.py contract. An
/// explicit-but-unknown id is a hard error (nothing is written) rather than a
/// silent Shared write, because a silent Shared write is exactly the
/// cross-tenant leak this policy exists to prevent.
pub fn decide_env_migration_scope(
    db: &Db,
    project_id: Option<&str>,
) -> Result<EnvMigrationScope, String> {
    let Some(id) = project_id else {
        // Pre-registration fresh adopt: V47-C original contract → Shared.
        return Ok(EnvMigrationScope::Shared);
    };

    let row = db
        .get_project(id)?
        .ok_or_else(|| format!("project_not_found: {}", id))?;

    match row.host {
        // Orchestrator-root secrets are legitimately machine-wide.
        ProjectHost::OrchestratorRoot => Ok(EnvMigrationScope::Shared),
        // Every regular project owns its migrated secrets.
        ProjectHost::Base | ProjectHost::Mao => Ok(EnvMigrationScope::PerProject(id.to_string())),
    }
}

/// Read the per-project SHARED_SECRETS_READ_DISABLED gate. Defaults to
/// `false` (shared reads allowed) on a missing row, a non-bool value, or any
/// DB error — the conservative soft-fail posture (never break a project that
/// never opted out). Byte-comparable in shape to `get_shared_kg_read_disabled`.
pub fn shared_secrets_read_disabled(db: &Db, project_id: &str) -> bool {
    db.get_setting(
        project_id,
        ORCHESTRATOR_CORE_MODULE_ID,
        SETTING_KEY_SHARED_SECRETS_READ_DISABLED,
    )
    .ok()
    .flatten()
    .and_then(|v| v.as_bool())
    .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;

    fn seed_project(db: &Db, id: &str, host: ProjectHost) {
        db.insert_project(id, id, &format!("/tmp/{}", id), host, id)
            .expect("insert_project");
    }

    // ─── S1: decide_env_migration_scope ───────────────────────────────

    #[test]
    fn none_project_id_stays_shared() {
        let db = Db::open_in_memory().unwrap();
        assert_eq!(
            decide_env_migration_scope(&db, None).unwrap(),
            EnvMigrationScope::Shared
        );
    }

    #[test]
    fn base_project_routes_per_project() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p-base", ProjectHost::Base);
        assert_eq!(
            decide_env_migration_scope(&db, Some("p-base")).unwrap(),
            EnvMigrationScope::PerProject("p-base".to_string())
        );
    }

    #[test]
    fn mao_project_routes_per_project() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p-mao", ProjectHost::Mao);
        assert_eq!(
            decide_env_migration_scope(&db, Some("p-mao")).unwrap(),
            EnvMigrationScope::PerProject("p-mao".to_string())
        );
    }

    #[test]
    fn orchestrator_root_stays_shared() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p-root", ProjectHost::OrchestratorRoot);
        assert_eq!(
            decide_env_migration_scope(&db, Some("p-root")).unwrap(),
            EnvMigrationScope::Shared
        );
    }

    #[test]
    fn unknown_project_id_errors_and_names_the_id() {
        let db = Db::open_in_memory().unwrap();
        let err = decide_env_migration_scope(&db, Some("nope")).unwrap_err();
        assert!(err.contains("project_not_found"), "err={}", err);
        assert!(err.contains("nope"), "err should name the id: {}", err);
        // The error must NEVER carry a secret value — it only has the id.
        assert!(!err.contains("value"), "err leaked a value shape: {}", err);
    }

    // ─── S2: shared_secrets_read_disabled ─────────────────────────────

    #[test]
    fn gate_defaults_false_when_no_row() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1", ProjectHost::Base);
        assert!(!shared_secrets_read_disabled(&db, "p1"));
    }

    #[test]
    fn gate_reads_true_after_set() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1", ProjectHost::Base);
        db.set_setting(
            "p1",
            ORCHESTRATOR_CORE_MODULE_ID,
            SETTING_KEY_SHARED_SECRETS_READ_DISABLED,
            &serde_json::Value::Bool(true),
        )
        .unwrap();
        assert!(shared_secrets_read_disabled(&db, "p1"));
    }

    #[test]
    fn gate_soft_fails_false_on_malformed_value() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1", ProjectHost::Base);
        // A non-bool JSON value must not panic — it reads back as false.
        db.set_setting(
            "p1",
            ORCHESTRATOR_CORE_MODULE_ID,
            SETTING_KEY_SHARED_SECRETS_READ_DISABLED,
            &serde_json::Value::String("not-a-bool".to_string()),
        )
        .unwrap();
        assert!(!shared_secrets_read_disabled(&db, "p1"));
    }

    /// The gate must be addressed at the canonical orchestrator-core id, NOT
    /// the legacy `"__project__"` sentinel — else it would repeat the KG-gate
    /// split-brain for secrets. A row written at `"__project__"` must NOT be
    /// seen by the gate.
    #[test]
    fn gate_ignores_legacy_project_sentinel_rows() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1", ProjectHost::Base);
        db.set_setting(
            "p1",
            "__project__",
            SETTING_KEY_SHARED_SECRETS_READ_DISABLED,
            &serde_json::Value::Bool(true),
        )
        .unwrap();
        // Canonical id has no row → gate stays false.
        assert!(!shared_secrets_read_disabled(&db, "p1"));
    }
}
