// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Tauri commands wrapping `vct_launcher_core::db::module_db_migrations`
//! (v0.2.31). Two surfaces:
//!
//! 1. `apply_module_db_migrations(module_id)` — manual repair / re-apply
//!    audit. Resolves the module's manifest + install dir from the
//!    launcher catalog, runs the apply pass, returns the structured
//!    report to the GUI. Used by the dashboard's "Repair module DB"
//!    surface for when the install-time apply soft-failed.
//!
//! 2. [`get_or_issue_module_token`] — the ONE launcher-side issuer of the
//!    per-(module, project) shared secret used as a bearer token for the
//!    hub's module-DB REST surface. Reads `module_access_tokens`
//!    (migration 019) and re-issues (1h TTL) when the row is missing or
//!    within the refresh margin of expiry. Callers: `module_db_client`
//!    (the RL widget's `module_db_read_row`) and `module_default_weights`
//!    (the hub upsert after a global-weights download). The RL container
//!    does NOT use these rows: it receives an in-memory identity token
//!    minted by the hub (`vct-hub/src/module_identity.rs`).
//!
//! The retired `issue_module_access_token` Tauri command (v0.2.100) was a
//! third, uncalled copy of the same upsert.
//!
//! The command here is soft-fail at the Tauri layer: a structured
//! `Result<_, String>` carries the error message into the GUI without
//! crashing the launcher.

use std::path::PathBuf;

use tauri::State;

use crate::db::module_db_migrations::{
    apply_module_db_migrations as core_apply, MigrationReport,
};
use crate::db::Db;
use crate::manifest::ModuleManifest;

/// Default token TTL on issue: 1 hour. The container refreshes via the
/// hub's refresh endpoint before this elapses; v0.2.32 will swap to
/// JWT-signed claims with the same TTL contract.
pub const DEFAULT_TOKEN_TTL_MS: i64 = 60 * 60 * 1000;

/// Token-bytes length we generate. 32 bytes = 256 bits, hex-encoded to
/// 64 chars. Matches the hub-auth token shape so any future migration
/// to a single token surface is purely a wiring change.
/// v0.2.54 Track J: now a re-export of the canonical
/// `vct_launcher_core::services::boot_token::TOKEN_BYTES` so the const
/// lives at exactly one address. Only consumed by this module's own
/// `#[cfg(test)]` block today — the `pub` keeps the import path
/// available for future callers.
#[allow(dead_code)]
pub const TOKEN_BYTES: usize = vct_launcher_core::services::boot_token::TOKEN_BYTES;

/// Manually apply module-shipped DB migrations for `module_id`.
///
/// Looks up the module's install dir + parsed manifest via the
/// catalog scan helper in `commands::modules`, then invokes the
/// shared apply mechanism in `vct_launcher_core::db::module_db_migrations`.
///
/// Returns the structured report (applied / skipped / errors lists).
/// The GUI surfaces the errors verbatim — they're already user-facing
/// strings naming the offending file + the actionable next step.
#[tauri::command]
pub async fn apply_module_db_migrations(
    db: State<'_, Db>,
    module_id: String,
) -> Result<MigrationReport, String> {
    // Resolve manifest + install_dir.
    let (manifest, install_dir) =
        resolve_manifest_and_install_dir(db.inner(), &module_id)?;

    // Apply. The Tauri command runs on a tokio worker; the apply does
    // blocking SQLite work synchronously. This is fine — apply is
    // bounded (a few small SQL files), and the manual-repair surface
    // isn't latency-sensitive (user clicked a button).
    let report = core_apply(db.inner(), &module_id, &install_dir, &manifest)?;
    Ok(report)
}

/// Margin (ms) below the token's `expires_at` at which a cached token is
/// re-issued rather than returned. Avoids racing the hub's expiry check on
/// the very last millisecond. 60 s is generous; tokens have a 1-hour TTL.
pub const TOKEN_REFRESH_MARGIN_MS: i64 = 60_000;

/// Get a usable per-(module, project) bearer token: the cached
/// `module_access_tokens` row while it is fresh, otherwise a newly
/// generated secret upserted over it (same pair, so the old token stops
/// working). The ONE implementation — `module_db_client` and
/// `module_default_weights` both call it.
pub fn get_or_issue_module_token(
    db: &Db,
    module_id: &str,
    project_id: &str,
) -> Result<String, String> {
    let now = chrono::Utc::now().timestamp_millis();

    let cached: Option<(String, i64)> = {
        let guard = db.lock();
        guard
            .query_row(
                "SELECT token_secret, expires_at FROM module_access_tokens \
                 WHERE module_id = ?1 AND project_id = ?2",
                rusqlite::params![module_id, project_id],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?)),
            )
            .ok()
    };
    if let Some((secret, expires_at)) = cached {
        if expires_at > now + TOKEN_REFRESH_MARGIN_MS {
            return Ok(secret);
        }
        // Near expiry: fall through to re-issue.
    }

    let secret = vct_launcher_core::services::boot_token::generate_token()
        .map_err(|e| format!("OS CSPRNG: {}", e))?;
    let expires_at = now + DEFAULT_TOKEN_TTL_MS;
    {
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO module_access_tokens \
                    (module_id, project_id, token_secret, issued_at, expires_at) \
                 VALUES (?1, ?2, ?3, ?4, ?5) \
                 ON CONFLICT(module_id, project_id) DO UPDATE SET \
                    token_secret = excluded.token_secret, \
                    issued_at = excluded.issued_at, \
                    expires_at = excluded.expires_at",
                rusqlite::params![module_id, project_id, &secret, now, expires_at],
            )
            .map_err(|e| format!("upsert module_access_tokens: {}", e))?;
    }
    Ok(secret)
}

// ─── Internals ──────────────────────────────────────────────────────────

/// Resolve a module's parsed manifest + on-disk install dir by ID.
///
/// Strategy: scan the bundled-manifests + ~/.vct/modules dir for a
/// manifest matching `module_id`, then resolve install_dir from the
/// catalog row (the same one `commands::modules::find_manifest` already
/// uses internally). On miss, return a structured error.
fn resolve_manifest_and_install_dir(
    db: &Db,
    module_id: &str,
) -> Result<(ModuleManifest, PathBuf), String> {
    // We reach into `commands::modules` because the catalog-scan helper
    // already exists there and is wired to find_manifest_for_resume.
    // Using it from a sibling commands module is fine — same crate.
    let manifest = crate::commands::modules::find_manifest_for_resume(db, module_id)
        .ok_or_else(|| format!("module '{}' not found in catalog", module_id))?;

    // Resolve install_dir via the PlaceholderCtx (same as installer_engine
    // does at install time). The {VCT_MODULES}/{install_dir} substitution
    // is what landed in `module_installs.install_path` at install time,
    // so we COULD pull it from the DB row instead. Prefer the live
    // resolution so manual-repair stays consistent with the install-
    // time resolution; on platforms where {VCT_MODULES} differs (e.g.
    // user moved their ~/.vct), the live resolution follows the user.
    let ctx = crate::manifest::PlaceholderCtx::new(module_id);
    let install_dir = ctx.resolve_install_dir(&manifest.install.install_dir);
    Ok((manifest, install_dir))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn token_row(db: &Db, m: &str, p: &str) -> Option<(String, i64)> {
        db.lock()
            .query_row(
                "SELECT token_secret, expires_at FROM module_access_tokens \
                 WHERE module_id = ?1 AND project_id = ?2",
                rusqlite::params![m, p],
                |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)),
            )
            .ok()
    }

    #[test]
    fn issues_a_64_hex_token_when_none_exists() {
        let db = Db::open_in_memory().unwrap();
        let t = get_or_issue_module_token(&db, "m", "p").expect("issue");
        assert_eq!(t.len(), TOKEN_BYTES * 2);
        assert!(t.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
        assert_eq!(token_row(&db, "m", "p").unwrap().0, t, "the issued token is persisted");
    }

    /// Leave-alone: a fresh cached token is returned unchanged.
    #[test]
    fn a_fresh_token_is_reused_not_reissued() {
        let db = Db::open_in_memory().unwrap();
        let t1 = get_or_issue_module_token(&db, "m", "p").unwrap();
        let t2 = get_or_issue_module_token(&db, "m", "p").unwrap();
        assert_eq!(t1, t2);
    }

    /// Act: a token inside the refresh margin is replaced (same pair, one row).
    #[test]
    fn a_near_expiry_token_is_reissued_over_the_same_row() {
        let db = Db::open_in_memory().unwrap();
        let t1 = get_or_issue_module_token(&db, "m", "p").unwrap();
        let near = chrono::Utc::now().timestamp_millis() + TOKEN_REFRESH_MARGIN_MS - 1_000;
        db.lock()
            .execute(
                "UPDATE module_access_tokens SET expires_at = ?1 WHERE module_id='m' AND project_id='p'",
                rusqlite::params![near],
            )
            .unwrap();
        let t2 = get_or_issue_module_token(&db, "m", "p").unwrap();
        assert_ne!(t1, t2, "tokens must differ after a re-issue");
        let n: i64 = db
            .lock()
            .query_row("SELECT COUNT(*) FROM module_access_tokens", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 1);
        assert!(token_row(&db, "m", "p").unwrap().1 > near + TOKEN_REFRESH_MARGIN_MS);
    }

    /// Distinct pairs get distinct tokens.
    #[test]
    fn distinct_pairs_get_distinct_tokens() {
        let db = Db::open_in_memory().unwrap();
        let a = get_or_issue_module_token(&db, "m", "p1").unwrap();
        let b = get_or_issue_module_token(&db, "m", "p2").unwrap();
        assert_ne!(a, b);
    }
}
