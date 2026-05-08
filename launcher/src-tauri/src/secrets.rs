//! OS keychain wrapper for module secrets.
//!
//! Primary store: keychain (macOS Keychain / Windows Credential Manager /
//! Linux libsecret). Service namespace: `vct.{scope}.{module_id}.{key}`
//! where scope is `{project_id}` for per-project secrets, `global` for
//! machine-wide, `{project_id}.shared` for cross-module-per-project.
//!
//! P1-C bridge (2026-05-08): for a hard-coded allowlist of well-known
//! shared keys (GitHub PAT, Vercel/Supabase/IONOS tokens, OpenAI/Anthropic
//! API keys), `set` and `delete` ALSO mirror to `~/.vct-secrets/...` files
//! (mode 0o600 on Unix). This is the contract the bundled MCP wrappers and
//! hooks rely on — they read directly from the file paths because they
//! predate the launcher's keychain layer. Without the mirror, GUI-set
//! secrets are silently invisible to those wrappers (the original bug).
//!
//! The bridge is one-way (keychain → file). The OS keychain remains
//! authoritative; the file is a derived view kept in sync. Reads go
//! through the keychain only — the file is never consulted by this
//! module. Unknown keys (anything not on the allowlist) are NEVER
//! mirrored, so the bridge can't accidentally leak module-internal
//! secrets to the filesystem.

use keyring::Entry;
use std::path::PathBuf;

const SERVICE_PREFIX: &str = "vct";

// ─── P1-C: keychain → ~/.vct-secrets/ bridge ─────────────────────────────
//
// Closed allowlist. Keep in sync with the keys consumed by:
//   * `.claude/hooks/*` (read from $HOME/.vct-secrets/shared/<key>)
//   * `claude_mcp_servers/*` wrappers that pre-date the launcher keychain
//   * `~/.bashrc` / shell rc snippets that source these files
// Adding a new key here is a security-relevant change — it materialises
// the value to disk for any tool that scans `~/.vct-secrets/`.
const BRIDGE_SHARED_KEYS: &[&str] = &[
    "github_pat",
    "GITHUB_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "VERCEL_TOKEN",
    "SUPABASE_TOKEN",
    "IONOS_TOKEN",
];

/// Returns true if the given key should be mirrored to `~/.vct-secrets/`
/// when written under the Shared scope. The match is exact / case-sensitive
/// — `GITHUB_TOKEN` and `github_pat` are deliberately separate entries
/// because hooks consume both shapes (legacy + new).
pub(crate) fn is_bridged_shared_key(key: &str) -> bool {
    BRIDGE_SHARED_KEYS.contains(&key)
}

/// Resolve the `~/.vct-secrets/` root. Honours `VCT_SECRETS_DIR` env var
/// for test isolation; falls through to the canonical `$HOME/.vct-secrets`
/// for production. Returns `None` only when neither HOME nor the override
/// is resolvable (extremely unusual — the launcher always has a UserDirs
/// in production).
fn vct_secrets_root() -> Option<PathBuf> {
    if let Ok(custom) = std::env::var("VCT_SECRETS_DIR") {
        if !custom.is_empty() {
            return Some(PathBuf::from(custom));
        }
    }
    directories::UserDirs::new().map(|u| u.home_dir().join(".vct-secrets"))
}

/// Resolve the bridge file path for a given (scope, key) pair. Returns
/// `None` for scopes / keys that aren't bridged, or when the secrets root
/// can't be resolved.
///
/// Layout (matches the existing GitHub-PAT path written by
/// `commands::installer::register_github_pat`):
///   * Shared:      `~/.vct-secrets/shared/<key>`
///   * PerProject:  `~/.vct-secrets/projects/<project_id>/<key>`
///   * Global / Shared-as-project-scoped: not bridged.
pub(crate) fn bridge_path_for(scope: SecretScope<'_>, key: &str) -> Option<PathBuf> {
    let root = vct_secrets_root()?;
    match scope {
        SecretScope::Shared { project_id: _ } if is_bridged_shared_key(key) => {
            Some(root.join("shared").join(key))
        }
        SecretScope::PerProject { project_id } if is_bridged_shared_key(key) => {
            // Per-project layout: `~/.vct-secrets/projects/<project_id>/<key>`.
            // Hooks running inside a project terminal pick this up via the
            // same lookup pattern as the Shared bridge.
            Some(root.join("projects").join(project_id).join(key))
        }
        // Global scope and non-allowlisted keys: NEVER bridged. Global
        // secrets are launcher-internal (license tokens etc.) and should
        // not materialise to disk.
        _ => None,
    }
}

/// Mirror a value to its bridge file with mode 0o600 on Unix. Idempotent
/// and best-effort: a write failure here MUST NOT fail the keychain
/// operation that triggered it (the keychain is authoritative).
fn write_bridge_file(path: &std::path::Path, value: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("mkdir {}: {}", parent.display(), e))?;
    }
    std::fs::write(path, value).map_err(|e| format!("write {}: {}", path.display(), e))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let perms = std::fs::Permissions::from_mode(0o600);
        std::fs::set_permissions(path, perms)
            .map_err(|e| format!("chmod {}: {}", path.display(), e))?;
    }
    // Best-effort tighten the parent directory too (mode 0o700).
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Some(parent) = path.parent() {
            if let Ok(meta) = std::fs::metadata(parent) {
                let mut perms = meta.permissions();
                perms.set_mode(0o700);
                let _ = std::fs::set_permissions(parent, perms);
            }
        }
    }
    Ok(())
}

/// Remove a bridge file. Best-effort: missing-file is treated as success.
fn remove_bridge_file(path: &std::path::Path) -> Result<(), String> {
    if !path.exists() {
        return Ok(());
    }
    std::fs::remove_file(path).map_err(|e| format!("remove {}: {}", path.display(), e))
}

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
    // P1-C bridge (2026-05-08): if the key is on the well-known allowlist,
    // ALSO mirror to `~/.vct-secrets/...` so bundled MCP wrappers that
    // pre-date the launcher keychain see the value. Best-effort — a bridge
    // write failure does not roll back the keychain set (the keychain is
    // authoritative).
    if let Some(bridge_path) = bridge_path_for(scope, key) {
        if let Err(e) = write_bridge_file(&bridge_path, value) {
            // Don't fail the caller, but make the failure visible in
            // launcher logs so a stale bridge can be diagnosed.
            eprintln!(
                "[vct] warning: keychain → ~/.vct-secrets/ bridge write failed for {}: {}. \
                 Hooks/wrappers reading the file path may use a stale value.",
                bridge_path.display(),
                e
            );
        }
    }
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
    let kc_result = match e.delete_credential() {
        Ok(()) => Ok(()),
        Err(keyring::Error::NoEntry) => Ok(()), // already gone — treat as success
        Err(err) => Err(format!("keyring delete: {}", err)),
    };
    // Symmetric P1-C bridge teardown — drop the file even if the keychain
    // delete reported NoEntry, so a previously-bridged-but-now-orphaned
    // file gets cleaned up. Best-effort.
    if let Some(bridge_path) = bridge_path_for(scope, key) {
        if let Err(e) = remove_bridge_file(&bridge_path) {
            eprintln!(
                "[vct] warning: keychain → ~/.vct-secrets/ bridge delete failed for {}: {}",
                bridge_path.display(),
                e
            );
        }
    }
    kc_result
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

// ─── P1-C bridge tests (2026-05-08) ──────────────────────────────────────
//
// `set_secret_v2` / `delete_secret_v2` flow through `secrets::set` /
// `secrets::delete`, which mirror to `~/.vct-secrets/<...>` for keys on
// the well-known allowlist. These tests pin:
//   * Bridge writes happen for allowlisted shared keys
//   * Bridge writes do NOT happen for non-allowlisted keys
//   * Symmetric delete removes the file
//   * Per-project scope writes to projects/<id>/<key>
//
// Tests use `VCT_SECRETS_DIR` env override + a process-wide Mutex to
// isolate from real user state. Keychain-touching paths probe via
// `keyring_available()`; CI hosts without a backend skip silently.

#[cfg(test)]
mod bridge_tests {
    use super::*;
    use std::sync::Mutex;

    static SERIALIZE: Mutex<()> = Mutex::new(());

    fn keyring_available() -> bool {
        let entry = match keyring::Entry::new("vct.test.bridge.probe", "probe") {
            Ok(e) => e,
            Err(_) => return false,
        };
        if entry.set_password("canary").is_err() {
            return false;
        }
        let _ = entry.delete_credential();
        true
    }

    struct EnvGuard {
        prev: Option<std::ffi::OsString>,
        _lock: std::sync::MutexGuard<'static, ()>,
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            match self.prev.take() {
                Some(v) => std::env::set_var("VCT_SECRETS_DIR", v),
                None => std::env::remove_var("VCT_SECRETS_DIR"),
            }
        }
    }

    fn isolate_secrets_dir() -> (PathBuf, EnvGuard) {
        let lock = SERIALIZE.lock().unwrap();
        let dir = std::env::temp_dir().join(format!(
            "vct-secrets-bridge-{}",
            uuid::Uuid::new_v4().simple()
        ));
        let prev = std::env::var_os("VCT_SECRETS_DIR");
        std::env::set_var("VCT_SECRETS_DIR", &dir);
        (dir, EnvGuard { prev, _lock: lock })
    }

    #[test]
    fn test_set_secret_v2_auto_bridges_known_shared_keys_to_vct_secrets_files() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend");
            return;
        }
        let (dir, _g) = isolate_secrets_dir();

        let canary = format!("bridge-canary-{}", uuid::Uuid::new_v4().simple());
        let scope = SecretScope::Shared { project_id: "_user_shared_" };
        // Use a unique module_id per test to avoid collisions with
        // other parallel keychain-touching tests.
        let module = format!("test-bridge-mod-{}", uuid::Uuid::new_v4().simple());

        for key in &["GITHUB_TOKEN", "OPENAI_API_KEY", "VERCEL_TOKEN"] {
            set(scope, &module, key, &canary).expect("set");
            let bridge = dir.join("shared").join(key);
            assert!(
                bridge.exists(),
                "bridge file missing for allowlisted key {}: {}",
                key,
                bridge.display()
            );
            let content = std::fs::read_to_string(&bridge).unwrap();
            assert_eq!(content, canary, "bridge content mismatch for {}", key);
            // 0o600 on Unix.
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let mode = std::fs::metadata(&bridge).unwrap().permissions().mode() & 0o777;
                assert_eq!(mode, 0o600, "bridge file mode for {}: {:o}", key, mode);
            }
            // Cleanup keychain entry per-iteration so the test stays self-contained.
            let _ = delete(scope, &module, key);
        }
    }

    #[test]
    fn test_set_secret_v2_does_not_bridge_unknown_keys() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend");
            return;
        }
        let (dir, _g) = isolate_secrets_dir();

        let scope = SecretScope::Shared { project_id: "_user_shared_" };
        let module = format!("test-allowlist-{}", uuid::Uuid::new_v4().simple());
        let canary = format!("opaque-canary-{}", uuid::Uuid::new_v4().simple());

        // A key that LOOKS plausible but is NOT on the allowlist.
        for key in &["RANDOM_API_KEY", "MY_CUSTOM_TOKEN", "INTERNAL_SECRET"] {
            set(scope, &module, key, &canary).expect("set");
            let bridge = dir.join("shared").join(key);
            assert!(
                !bridge.exists(),
                "bridge file unexpectedly created for non-allowlisted key {}: {}",
                key,
                bridge.display()
            );
            // The directory itself may also stay un-created.
            // (Other test cases create the `shared/` subdir; we only
            // assert the specific file doesn't exist.)
            let _ = delete(scope, &module, key);
        }
    }

    #[test]
    fn test_delete_secret_v2_removes_bridge_file() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend");
            return;
        }
        let (dir, _g) = isolate_secrets_dir();

        let scope = SecretScope::Shared { project_id: "_user_shared_" };
        let module = format!("test-delete-{}", uuid::Uuid::new_v4().simple());
        let key = "GITHUB_TOKEN";
        let canary = format!("delete-canary-{}", uuid::Uuid::new_v4().simple());

        set(scope, &module, key, &canary).expect("set");
        let bridge = dir.join("shared").join(key);
        assert!(bridge.exists(), "precondition: bridge file should exist");

        delete(scope, &module, key).expect("delete");
        assert!(
            !bridge.exists(),
            "bridge file not removed after delete: {}",
            bridge.display()
        );
    }

    #[test]
    fn test_per_project_bridge_writes_correct_path() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend");
            return;
        }
        let (dir, _g) = isolate_secrets_dir();

        let project_id = "test-project-id-42";
        let scope = SecretScope::PerProject { project_id };
        let module = format!("test-pp-{}", uuid::Uuid::new_v4().simple());
        let key = "GITHUB_TOKEN";
        let canary = format!("pp-canary-{}", uuid::Uuid::new_v4().simple());

        set(scope, &module, key, &canary).expect("set");
        let bridge = dir.join("projects").join(project_id).join(key);
        assert!(
            bridge.exists(),
            "per-project bridge file missing at {}",
            bridge.display()
        );
        let content = std::fs::read_to_string(&bridge).unwrap();
        assert_eq!(content, canary);

        // Symmetric delete cleans up.
        delete(scope, &module, key).expect("delete");
        assert!(!bridge.exists(), "per-project bridge not deleted");
    }

    /// Global-scope secrets are NEVER bridged (they're launcher-internal,
    /// e.g. license tokens — should not materialise on disk).
    #[test]
    fn test_global_scope_does_not_bridge() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend");
            return;
        }
        let (dir, _g) = isolate_secrets_dir();

        let scope = SecretScope::Global;
        let module = format!("test-global-{}", uuid::Uuid::new_v4().simple());
        let key = "GITHUB_TOKEN"; // even an allowlisted key
        let canary = format!("global-canary-{}", uuid::Uuid::new_v4().simple());

        set(scope, &module, key, &canary).expect("set");
        let bridge = dir.join("shared").join(key);
        assert!(
            !bridge.exists(),
            "Global-scope secret leaked to ~/.vct-secrets/: {}",
            bridge.display()
        );

        let _ = delete(scope, &module, key);
    }

    /// Pure unit test for the allowlist predicate — no keychain needed.
    #[test]
    fn allowlist_pure_predicate() {
        assert!(is_bridged_shared_key("github_pat"));
        assert!(is_bridged_shared_key("GITHUB_TOKEN"));
        assert!(is_bridged_shared_key("OPENAI_API_KEY"));
        assert!(is_bridged_shared_key("ANTHROPIC_API_KEY"));
        assert!(is_bridged_shared_key("VERCEL_TOKEN"));
        assert!(is_bridged_shared_key("SUPABASE_TOKEN"));
        assert!(is_bridged_shared_key("IONOS_TOKEN"));
        // Negatives — must be exact match (case-sensitive).
        assert!(!is_bridged_shared_key("github_token"));
        assert!(!is_bridged_shared_key("GITHUB_PAT"));
        assert!(!is_bridged_shared_key("MY_GITHUB_TOKEN"));
        assert!(!is_bridged_shared_key(""));
        assert!(!is_bridged_shared_key("GITHUB_TOKEN_2"));
    }

    /// `bridge_path_for` returns `None` for the no-op cases (Global
    /// scope, non-allowlisted keys), so the call sites can simply check
    /// the `Option` instead of duplicating the predicate.
    #[test]
    fn bridge_path_for_returns_none_for_no_op_cases() {
        let (_dir, _g) = isolate_secrets_dir();

        // Global never bridges, even for allowlisted keys.
        assert!(bridge_path_for(SecretScope::Global, "GITHUB_TOKEN").is_none());

        // Shared with a non-allowlisted key never bridges.
        assert!(
            bridge_path_for(
                SecretScope::Shared { project_id: "_user_shared_" },
                "RANDOM_KEY",
            )
            .is_none()
        );

        // Shared with allowlisted key DOES bridge.
        assert!(
            bridge_path_for(
                SecretScope::Shared { project_id: "_user_shared_" },
                "GITHUB_TOKEN",
            )
            .is_some()
        );

        // Per-project with allowlisted key DOES bridge.
        assert!(
            bridge_path_for(
                SecretScope::PerProject { project_id: "p1" },
                "OPENAI_API_KEY",
            )
            .is_some()
        );
    }
}
