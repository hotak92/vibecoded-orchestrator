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
//! 2. `issue_module_access_token(module_id, project_id)` — issues a
//!    fresh per-(module, project) shared secret for hub bearer auth.
//!    Called by the launcher when starting a module container — the
//!    secret is threaded into the container env as `VCT_MODULE_TOKEN`.
//!    Persisted in `module_access_tokens` (migration 019) with a 1h
//!    TTL. The container refreshes via the hub's
//!    `POST /api/v1/modules/{id}/token/refresh` route before expiry.
//!
//! Both commands are soft-fail at the Tauri layer: a structured
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

#[derive(Debug, Clone, serde::Serialize)]
pub struct AccessTokenIssued {
    pub module_id: String,
    pub project_id: String,
    pub token: String,
    pub expires_at_ms: i64,
}

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

/// Issue a fresh shared-secret access token for the
/// (module_id, project_id) pair. Replaces any existing token for the
/// same pair (caller is expected to thread the new value into the
/// container's env on next start).
///
/// v0.2.31 ships with a per-install shared-secret pattern; v0.2.32
/// migrates to JWT-signed claims with refresh tokens. The Tauri
/// command shape stays the same.
#[tauri::command]
pub async fn issue_module_access_token(
    db: State<'_, Db>,
    module_id: String,
    project_id: String,
) -> Result<AccessTokenIssued, String> {
    let secret = generate_token_hex().map_err(|e| format!("OS CSPRNG: {}", e))?;
    let now = chrono::Utc::now().timestamp_millis();
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
                rusqlite::params![&module_id, &project_id, &secret, now, expires_at],
            )
            .map_err(|e| format!("upsert module_access_tokens: {}", e))?;
    }

    Ok(AccessTokenIssued {
        module_id,
        project_id,
        token: secret,
        expires_at_ms: expires_at,
    })
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

/// Generate a hex-encoded 32-byte random token from the OS CSPRNG.
///
/// v0.2.54 Track J amend: delegates to
/// `vct_launcher_core::services::boot_token::generate_token` — the
/// "future v0.2.32 refactor" predicted in the prior comment.
fn generate_token_hex() -> Result<String, String> {
    vct_launcher_core::services::boot_token::generate_token()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_hex_is_64_lowercase_hex_chars() {
        let t = generate_token_hex().expect("rng ok");
        assert_eq!(t.len(), TOKEN_BYTES * 2);
        assert!(t.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    }

    #[test]
    fn two_calls_return_different_tokens() {
        let t1 = generate_token_hex().expect("rng ok");
        let t2 = generate_token_hex().expect("rng ok");
        assert_ne!(t1, t2, "tokens must differ");
    }
}
