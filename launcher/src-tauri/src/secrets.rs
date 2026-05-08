//! OS keychain wrapper for module secrets.
//!
//! Primary store: keychain (macOS Keychain / Windows Credential Manager /
//! Linux libsecret). Service namespace: `vct.{scope}.{module_id}.{key}`
//! where scope is `{project_id}` for per-project secrets, `global` for
//! machine-wide, `{project_id}.shared` for cross-module-per-project.
//!
//! ─── Architecture note (post-Fix-#3 cleanup, 0.1.7) ──────────────────
//!
//! Earlier in 0.1.7 (PR #171, "Fix #3") this module also mirrored a
//! hard-coded allowlist of well-known shared keys (`github_pat`,
//! `OPENAI_API_KEY`, etc.) to `~/.vct-secrets/<key>` files so bundled
//! MCP wrappers and hooks could read them with `cat`. That was an
//! escape hatch for wrappers that pre-dated the launcher's keychain
//! layer; it was rejected by the project owner because:
//!
//!   * It hard-codes a set of "blessed" keys that bypass the per-project
//!     active-flag gate (Lifecycle B). A paused secret in the launcher
//!     GUI was still readable on disk.
//!   * It materialises secret values to disk for any tool that scans
//!     `~/.vct-secrets/`, weakening the "keychain is authoritative"
//!     contract.
//!   * It scales by a hard-coded allowlist, not by the per-project
//!     access matrix the launcher already maintains.
//!
//! Replacement: the launcher's hub HTTP server (port 7700, see
//! `crate::hub::modules_api::project_env`) exposes
//! `GET /api/v1/projects/{id}/env`, which returns the active set of
//! (key, value) pairs the project is entitled to — keychain resolution
//! + active-flag gating + cross-launcher pause check, all in one
//! place. Bundled wrappers consume that endpoint via the shared
//! resolver helper at `templates/scripts/vct_secrets_resolve.sh`
//! (Bash) / `.ps1` (PowerShell). See `docs/MIGRATION-0.1.7.md`.
//!
//! Reads in this module go through the keychain only. There is no
//! file-side mirror anywhere in the launcher's set/delete paths.
//! Two consumer paths still write file artefacts under
//! `~/.vct-secrets/`, both INTENTIONAL exceptions to keep the legacy
//! file-reader contract alive while bundled wrappers are migrated:
//!
//!   * `commands::installer::register_github_pat` writes the
//!     onboarding-wizard PAT to `~/.vct-secrets/shared/github_pat`.
//!     This is the user-facing "register a GitHub PAT" flow that
//!     pre-dates Fix #3; it has its own dedicated UI and is NOT a
//!     keychain mirror — the value is only stored once, on the file.
//!     Hub-resolver migration of that flow is tracked in the GUI
//!     audit's "Open question #1".
//!
//!   * `tools/vct-secrets/` — the user-facing `vct` CLI for set/get
//!     of arbitrary file-based secrets. This is a Phase 1 primitive
//!     used by external scripts that don't talk to the hub; it stays
//!     as-is.
//!
//! Neither of those paths is invoked from `secrets::set` /
//! `secrets::delete`. This module is pure keychain.

use keyring::Entry;

const SERVICE_PREFIX: &str = "vct";

#[derive(Debug, Clone, Copy)]
pub enum SecretScope<'a> {
    /// Per-project secret: one value per (project, module).
    PerProject { project_id: &'a str },
    /// Machine-wide secret shared across all projects.
    Global,
    /// Shared across modules within a single project.
    Shared { project_id: &'a str },
}

impl<'a> SecretScope<'a> {
    /// Build the full keychain service string for a (scope, module, key).
    ///
    /// Keychain backends key off (service, username). We use `service` as
    /// the full namespace and `username` as the secret key to keep entries
    /// discoverable in the OS credential manager UI.
    pub fn service_name(&self, module_id: &str) -> String {
        match self {
            SecretScope::PerProject { project_id } => {
                format!("{}.{}.{}", SERVICE_PREFIX, project_id, module_id)
            }
            SecretScope::Global => {
                format!("{}.global.{}", SERVICE_PREFIX, module_id)
            }
            SecretScope::Shared { project_id } => {
                format!("{}.{}.shared.{}", SERVICE_PREFIX, project_id, module_id)
            }
        }
    }
}

fn entry(scope: SecretScope<'_>, module_id: &str, key: &str) -> Result<Entry, String> {
    let service = scope.service_name(module_id);
    Entry::new(&service, key).map_err(|e| format!("keyring entry for {}/{}: {}", service, key, e))
}

pub fn set(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
    value: &str,
) -> Result<(), String> {
    let e = entry(scope, module_id, key)?;
    e.set_password(value)
        .map_err(|err| format!("keyring set: {}", err))?;
    Ok(())
}

pub fn get(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
) -> Result<Option<String>, String> {
    let e = entry(scope, module_id, key)?;
    match e.get_password() {
        Ok(v) => Ok(Some(v)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(err) => Err(format!("keyring get: {}", err)),
    }
}

pub fn is_set(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
) -> Result<bool, String> {
    Ok(get(scope, module_id, key)?.is_some())
}

pub fn delete(
    scope: SecretScope<'_>,
    module_id: &str,
    key: &str,
) -> Result<(), String> {
    let e = entry(scope, module_id, key)?;
    match e.delete_credential() {
        Ok(()) => Ok(()),
        Err(keyring::Error::NoEntry) => Ok(()), // already gone — treat as success
        Err(err) => Err(format!("keyring delete: {}", err)),
    }
}

/// Return a masked preview of a non-sensitive value (never for sensitive
/// secrets — those must return only presence booleans).
pub fn mask_preview(value: &str) -> String {
    let trimmed = value.chars().collect::<Vec<_>>();
    if trimmed.len() <= 8 {
        return "•".repeat(trimmed.len().max(4));
    }
    let head: String = trimmed[..4].iter().collect();
    let tail: String = trimmed[trimmed.len().saturating_sub(3)..].iter().collect();
    format!("{}•••{}", head, tail)
}

// ─── Tests ───────────────────────────────────────────────────────────────
//
// Pure-function tests for keychain plumbing. The previous Fix #3 test
// module (`bridge_tests::*`) was removed alongside the bridge — those
// tests pinned behaviour that no longer exists.
//
// Keychain-touching round-trip tests live in `commands::secrets_cmd::tests`
// and `hub::modules_api::tests`; replicating them here would just
// duplicate the same `keyring_available()` short-circuit.

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn service_name_per_project_includes_project_id() {
        let scope = SecretScope::PerProject { project_id: "p1" };
        assert_eq!(scope.service_name("mod"), "vct.p1.mod");
    }

    #[test]
    fn service_name_global_uses_global_segment() {
        let scope = SecretScope::Global;
        assert_eq!(scope.service_name("mod"), "vct.global.mod");
    }

    #[test]
    fn service_name_shared_includes_shared_segment() {
        let scope = SecretScope::Shared { project_id: "p1" };
        assert_eq!(scope.service_name("mod"), "vct.p1.shared.mod");
    }

    #[test]
    fn mask_preview_short_value_is_fully_masked() {
        assert_eq!(mask_preview(""), "••••");
        assert_eq!(mask_preview("abc"), "••••");
        assert_eq!(mask_preview("12345678"), "••••••••");
    }

    #[test]
    fn mask_preview_long_value_keeps_head_and_tail() {
        let masked = mask_preview("ghp_1234567890abcdef");
        // 4 head chars + bullets + 3 tail chars
        assert!(masked.starts_with("ghp_"));
        assert!(masked.ends_with("def"));
        assert!(masked.contains("•••"));
        // Raw value must not appear verbatim.
        assert!(!masked.contains("ghp_1234567890abcdef"));
    }
}
