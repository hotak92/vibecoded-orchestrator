//! OS keychain wrapper for module secrets.
//!
//! Secrets NEVER touch the filesystem or the SQLite DB. They live in:
//!   - macOS: Keychain
//!   - Windows: Credential Manager
//!   - Linux: libsecret (GNOME Keyring / KWallet)
//!
//! Service namespace: `vct.{scope}.{module_id}.{key}` where scope is
//! `{project_id}` for per-project secrets, `global` for machine-wide,
//! `{project_id}.shared` for cross-module-per-project.

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
        .map_err(|err| format!("keyring set: {}", err))
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
