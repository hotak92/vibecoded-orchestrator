// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! Tauri surface for a project's infrastructure declarations (v0.2.102 W5-A,
//! M3) — the Identity tab's "Infrastructure this project owns" panel
//! (`launcher/src/lib/project-state/InfraDeclarationsPanel.svelte`).
//!
//! Storage, validation and the readers are
//! `vct_launcher_core::db::infra_declarations` (migration 049). This layer
//! adds the audit row and the env re-projection: `config_projection` writes
//! the project's declarations into its `.claude/settings.json` `env`
//! (`VCT_EXTERNAL_COLLECTIONS`, `VCT_UNMANAGED_CONTAINERS`,
//! `VCT_SSRF_EXTRA_LOCAL_PORTS`), which is how the WebFetch SSRF guard of
//! THAT project learns its extra local ports. The DB write is authoritative;
//! a projection hiccup is a warning, never a failed save (the
//! `reproject_env_soft` contract).

use serde::Serialize;
use tauri::{command, AppHandle, State};

use crate::db::Db;
use vct_launcher_core::db::infra_declarations::{InfraDeclarations, KINDS};

/// What a save returns: the stored declarations (re-read, so the panel
/// renders the truth) and any projection warning.
#[derive(Debug, Clone, Serialize)]
pub struct InfraDeclarationsWriteResult {
    pub declarations: InfraDeclarations,
    pub warnings: Vec<String>,
}

/// The project's declarations, grouped by kind.
#[command]
pub async fn get_project_infra_declarations(
    project_id: String,
    db: State<'_, Db>,
) -> Result<InfraDeclarations, String> {
    if project_id.trim().is_empty() {
        return Err("get_project_infra_declarations: project_id required".into());
    }
    db.list_infra_declarations(&project_id)
}

/// Replace the project's declarations of ONE kind (`external_collection`,
/// `unmanaged_container` or `local_port`) with `values`. An invalid value
/// refuses the whole write and names it; nothing is half-applied.
#[command]
pub async fn set_project_infra_declarations(
    project_id: String,
    kind: String,
    values: Vec<String>,
    app: AppHandle,
    db: State<'_, Db>,
) -> Result<InfraDeclarationsWriteResult, String> {
    set_project_infra_declarations_with_db(&db, &project_id, &kind, &values)?;
    let mut warnings = Vec::new();
    let pid = project_id.clone();
    // The re-projection is a Python subprocess: the blocking pool, never a
    // tokio worker (the dual-flags precedent). Soft-fail throughout.
    match crate::commands::blocking::run_with_db_on_blocking_pool(
        app,
        "set_project_infra_declarations env re-projection",
        move |db| crate::commands::projects_v2::reproject_env_soft(db, &pid).warnings,
    )
    .await
    {
        Ok(w) => warnings.extend(w),
        Err(e) => warnings.push(format!("env re-projection did not run: {e} (the declarations are saved)")),
    }
    Ok(InfraDeclarationsWriteResult { declarations: db.list_infra_declarations(&project_id)?, warnings })
}

/// The DB half of [`set_project_infra_declarations`] (validation, write,
/// audit) — separated so it is testable without a Tauri runtime.
pub fn set_project_infra_declarations_with_db(
    db: &Db,
    project_id: &str,
    kind: &str,
    values: &[String],
) -> Result<Vec<String>, String> {
    if project_id.trim().is_empty() {
        return Err("set_project_infra_declarations: project_id required".into());
    }
    if !KINDS.contains(&kind) {
        return Err(format!("unknown declaration kind '{kind}' (expected one of {})", KINDS.join(", ")));
    }
    let stored = db.set_infra_declarations(project_id, kind, values)?;
    let _ = db.audit(
        "project_infra_declarations_set",
        Some(project_id),
        None,
        &serde_json::json!({ "kind": kind, "values": stored }),
    );
    Ok(stored)
}

#[cfg(test)]
mod tests {
    use super::*;
    use vct_launcher_core::db::infra_declarations::{KIND_EXTERNAL_COLLECTION, KIND_UNMANAGED_CONTAINER};
    use vct_launcher_core::db::models::ProjectHost;

    #[test]
    fn the_db_half_validates_writes_and_audits() {
        let db = Db::open_in_memory().unwrap();
        let slug = db.generate_unique_slug("peer-project").unwrap();
        db.insert_project("p1", "peer-project", "/tmp/w5a-p1", ProjectHost::Base, &slug).unwrap();
        let stored =
            set_project_infra_declarations_with_db(&db, "p1", KIND_EXTERNAL_COLLECTION, &["peerRuntime_Memory".into()])
                .unwrap();
        assert_eq!(stored, vec!["PeerRuntime_Memory"]);
        assert!(set_project_infra_declarations_with_db(&db, "p1", "bogus", &[]).is_err());
        assert!(set_project_infra_declarations_with_db(&db, "", KIND_UNMANAGED_CONTAINER, &[]).is_err());
        let audited: i64 = db
            .lock()
            .query_row(
                "SELECT COUNT(*) FROM audit_log WHERE operation = 'project_infra_declarations_set'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(audited, 1);
    }
}
