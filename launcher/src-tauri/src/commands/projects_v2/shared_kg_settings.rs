//! Shared-KG per-project setting accessors (read path + legacy migration).
//!
//! Verbatim extraction (v0.2.77 Part 7d) of the idempotent legacy-key
//! migration (`_migrate_shared_kg_setting`) and the shared-KG read/write/opt-out
//! getters (`get_shared_kg_write_disabled`, `get_shared_kg_read_disabled`,
//! `get_shared_kg_opt_out`) that previously lived inline in `projects_v2.rs`.
//! The `SETTING_KEY_SHARED_KG_READ_DISABLED` constant travels with its sole
//! reader. Behaviour is unchanged; the facade re-exports every symbol (the
//! getters are consumed cross-file by project_env_settings.rs via the
//! `projects_v2::` path; the setter Tauri commands stay in the facade and
//! reach these + the constant through the glob re-export).
//!
//! The other SETTING_KEY_SHARED_KG_* constants + KG_GATE_MODULE_ID
//! stay in the facade and are pulled in via `super::`.

use crate::db::Db;

use super::{
    KG_GATE_MODULE_ID, SETTING_KEY_SHARED_KG_OPT_OUT_LEGACY,
    SETTING_KEY_SHARED_KG_WRITE_DISABLED,
};

/// One-shot, idempotent migration: if a DB row exists under the LEGACY key
/// (`shared_kg_opt_out`) but NOT under the canonical key
/// (`shared_kg_write_disabled`), copy it across and delete the legacy row.
/// Safe to call from any read path.
///
/// Returns the migrated value (Some(bool)) if a migration occurred,
/// Some(canonical_value) if the canonical row already existed, or None when
/// neither row exists. Callers usually just discard the return — the side
/// effect on the DB is the point.
pub(crate) fn _migrate_shared_kg_setting(db: &Db, project_id: &str) -> Result<Option<bool>, String> {
    // Canonical row wins outright — drop any stale legacy row to avoid
    // confusing future reads.
    if let Some(canonical) =
        db.get_setting(project_id, KG_GATE_MODULE_ID, SETTING_KEY_SHARED_KG_WRITE_DISABLED)?
    {
        // Best-effort cleanup of legacy row; never fail the migration over it.
        let _ = db.delete_setting(
            project_id,
            KG_GATE_MODULE_ID,
            SETTING_KEY_SHARED_KG_OPT_OUT_LEGACY,
        );
        return Ok(Some(canonical.as_bool().unwrap_or(false)));
    }

    // Otherwise check the legacy row and forward it.
    if let Some(legacy) =
        db.get_setting(project_id, KG_GATE_MODULE_ID, SETTING_KEY_SHARED_KG_OPT_OUT_LEGACY)?
    {
        let bool_val = legacy.as_bool().unwrap_or(false);
        db.set_setting(
            project_id,
            KG_GATE_MODULE_ID,
            SETTING_KEY_SHARED_KG_WRITE_DISABLED,
            &serde_json::Value::Bool(bool_val),
        )?;
        let _ = db.delete_setting(
            project_id,
            KG_GATE_MODULE_ID,
            SETTING_KEY_SHARED_KG_OPT_OUT_LEGACY,
        );
        tracing::info!(
            "[vct] migrated project setting `shared_kg_opt_out` → \
             `shared_kg_write_disabled` for project {}",
            project_id
        );
        return Ok(Some(bool_val));
    }
    Ok(None)
}

/// Read the current SHARED_KG_WRITE_DISABLED toggle from the DB. Defaults to
/// `false` (writes allowed) when no row exists. Triggers a one-shot migration
/// from the legacy `shared_kg_opt_out` key if present — idempotent on repeat
/// calls.
pub fn get_shared_kg_write_disabled(db: &Db, project_id: &str) -> Result<bool, String> {
    Ok(_migrate_shared_kg_setting(db, project_id)?.unwrap_or(false))
}

/// v0.2.46 Decision B — per-project setting key for the symmetric READ
/// gate (`SHARED_KG_READ_DISABLED`). When `true`, the project's env
/// surfaces carry `SHARED_KG_READ_DISABLED=true`, which the MCP's
/// `_kg_collections_to_search` reads to drop the shared collection from
/// the hybrid_search / semantic_graph_search fan-out. No legacy alias —
/// pre-v0.2.46 the read path was unconditional, so there's no
/// historical key to honour.
pub const SETTING_KEY_SHARED_KG_READ_DISABLED: &str = "shared_kg_read_disabled";

/// v0.2.46 Decision B — read the current SHARED_KG_READ_DISABLED toggle
/// from the DB. Defaults to `false` (reads allowed) when no row exists.
/// No legacy-alias migration because the key is new — pre-v0.2.46 the
/// read path was unconditional, so no DB row could exist under a prior
/// name. Symmetric mirror of `get_shared_kg_write_disabled` in shape +
/// default semantics.
pub fn get_shared_kg_read_disabled(db: &Db, project_id: &str) -> Result<bool, String> {
    let val = db
        .get_setting(
            project_id,
            KG_GATE_MODULE_ID,
            SETTING_KEY_SHARED_KG_READ_DISABLED,
        )?
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    Ok(val)
}

/// Deprecated alias of `get_shared_kg_write_disabled`. Will be removed once
/// the legacy command + env var are dropped (target: 2026-08).
#[deprecated(
    since = "0.2.46",
    note = "Use `get_shared_kg_write_disabled` — the toggle now gates WRITES \
            only. Reads of the shared KG are always on."
)]
#[allow(dead_code)]
pub fn get_shared_kg_opt_out(db: &Db, project_id: &str) -> Result<bool, String> {
    get_shared_kg_write_disabled(db, project_id)
}

