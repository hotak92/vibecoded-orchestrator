// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! `runtime.env_from_secrets`, read (v0.2.97, lane V round 2), and the ONE
//! permission-gated lookup of a module-declared secret.
//!
//! `docs/VCT_MODULE_MANIFEST_SPEC.md` §7 said a module's secrets are served
//! "via `runtime.env_from_secrets`"; no spawner injected them. This module is
//! the resolver the container/service spawns use
//! (`services::container_runtime::spawn_args_for_project` and the hub's global
//! spawn), and [`resolve_module_secret`] is the gate the hub's
//! `/projects/{id}/env` uses for the same declarations — one home, so a spawn
//! can never serve a secret `/env` would refuse.
//!
//! ## The gate (never bypassed)
//!
//! A secret resolves only when the launcher's per-(scope, key, requester)
//! active flag says so
//! (`db::secret_active::is_secret_active_cross_launcher_for_requester`, every
//! sibling launcher DB included) and the OS keychain holds a value. The
//! requester is the project the container serves; a GLOBAL container serves
//! every project, so it asks as [`REQUESTER_ANY`] — only a machine-wide pause
//! applies, and a `per-project` secret has no project to resolve for.
//! (Cross-project GRANTS govern a project reading another project's user
//! secrets; a module's own declared secret has none — `/env` resolves these
//! the same way.)
//!
//! ## Rules
//!
//! * Only keys in `runtime.env_from_secrets` are resolved and injected.
//! * A listed key with no `secrets[]` declaration has no scope, so it cannot
//!   be looked up: skipped with a warning.
//! * Paused, not granted, not set, empty, or a keychain error: SKIPPED with a
//!   log line naming the key and the reason — unless the declaration is
//!   `required` (the manifest default), in which case the start is REFUSED
//!   with that reason.
//! * Values never reach a log, an error, argv or a `Debug` print.
//!
//! [`REQUESTER_ANY`]: crate::db::secret_active::REQUESTER_ANY

use crate::db::secret_active::{is_secret_active_cross_launcher_for_requester, REQUESTER_ANY};
use crate::db::Db;
use crate::manifest::{ModuleManifest, SecretDecl};
use crate::secrets::{get_with_context, CallContext, KeychainError, SecretScope};

/// The keychain slot `project_id` of a `shared`-scope secret.
pub const SENTINEL_SHARED: &str = "_user_shared_";
/// The keychain slot `project_id` of a `global`-scope secret.
pub const SENTINEL_GLOBAL: &str = "_global_";

/// The outcome of one gated lookup. `Value` is the only variant carrying a
/// secret; its `Debug` is redacted.
pub enum SecretLookup {
    Value(String),
    /// The active flag refuses it for this requester (paused / not granted).
    Inactive,
    /// Active, but the keychain has no value.
    Missing,
    /// A `per-project` secret asked for with no project (a global spawn).
    NoProject,
    /// The keychain read failed (locked, daemon error). Detail only.
    ReadError(String),
}

impl std::fmt::Debug for SecretLookup {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SecretLookup::Value(_) => f.write_str("Value(<redacted>)"),
            SecretLookup::Inactive => f.write_str("Inactive"),
            SecretLookup::Missing => f.write_str("Missing"),
            SecretLookup::NoProject => f.write_str("NoProject"),
            SecretLookup::ReadError(e) => write!(f, "ReadError({e})"),
        }
    }
}

impl SecretLookup {
    fn reason(&self) -> &'static str {
        match self {
            SecretLookup::Value(_) => "resolved",
            SecretLookup::Inactive => "not active for this requester (paused, or not granted)",
            SecretLookup::Missing => "not set",
            SecretLookup::NoProject => "per-project secret, but a global container serves no single project",
            SecretLookup::ReadError(_) => "the keychain could not be read",
        }
    }
}

/// The gated lookup of one declared secret. `scope` is the manifest's
/// (`global` / `shared` / anything else = per-project); `requester` is the
/// consuming project's id, or `None` for a global container (asks as
/// [`REQUESTER_ANY`]). This is the logic of `/env`'s module loop, moved here
/// so both use it.
pub fn resolve_module_secret(
    db: &Db,
    module_id: &str,
    key: &str,
    scope: &str,
    requester: Option<&str>,
) -> SecretLookup {
    let (scope_str, slot): (&str, &str) = match (scope, requester) {
        ("global", _) => ("global", SENTINEL_GLOBAL),
        ("shared", _) => ("shared", SENTINEL_SHARED),
        (_, Some(project_id)) => ("per_project", project_id),
        (_, None) => return SecretLookup::NoProject,
    };
    let requester_id = requester.unwrap_or(REQUESTER_ANY);
    if !is_secret_active_cross_launcher_for_requester(db, scope_str, slot, module_id, key, requester_id) {
        return SecretLookup::Inactive;
    }
    let keychain_scope = match scope_str {
        "global" => SecretScope::Global,
        "shared" => SecretScope::Shared { project_id: SENTINEL_SHARED },
        _ => SecretScope::PerProject { project_id: slot },
    };
    match get_with_context(keychain_scope, module_id, key, CallContext::Background) {
        Ok(Some(v)) => SecretLookup::Value(v),
        Ok(None) => SecretLookup::Missing,
        Err(KeychainError::Locked) => SecretLookup::ReadError("keychain locked".into()),
        Err(KeychainError::Other(e)) => SecretLookup::ReadError(e),
    }
}

/// The resolver over an injected lookup. `Ok` holds the `(KEY, value)` pairs
/// to put in the spawned process's environment; `Err` refuses the start (a
/// `required` secret that did not resolve) and names only keys and reasons.
pub fn resolve_env_from_secrets_with(
    manifest: &ModuleManifest,
    lookup: impl Fn(&SecretDecl) -> SecretLookup,
) -> Result<Vec<(String, String)>, String> {
    let mut out: Vec<(String, String)> = Vec::new();
    for key in &manifest.runtime.env_from_secrets {
        if out.iter().any(|(k, _)| k == key) {
            continue;
        }
        if !crate::module_settings_env::is_env_var_name(key) {
            tracing::warn!(module_id = %manifest.id, key = %key,
                "[module_secrets_env] env_from_secrets names a key that is not an environment-variable name; not injected");
            continue;
        }
        let Some(decl) = manifest.secrets.iter().find(|s| &s.key == key) else {
            tracing::warn!(module_id = %manifest.id, key = %key,
                "[module_secrets_env] env_from_secrets lists a key with no secrets[] declaration (no scope to resolve it in); not injected");
            continue;
        };
        let found = lookup(decl);
        let found = match found {
            SecretLookup::Value(v) if v.trim().is_empty() => SecretLookup::Missing,
            other => other,
        };
        match found {
            SecretLookup::Value(v) => out.push((key.clone(), v)),
            other => {
                let detail = match &other {
                    SecretLookup::ReadError(e) => format!("{} ({e})", other.reason()),
                    _ => other.reason().to_string(),
                };
                if decl.required {
                    return Err(format!(
                        "module {} needs its required secret {key}, which is {detail}. \
                         Set or re-activate it in the launcher's Secrets panel, then start the module again.",
                        manifest.id
                    ));
                }
                tracing::info!(module_id = %manifest.id, key = %key, reason = %detail,
                    "[module_secrets_env] optional secret not injected");
            }
        }
    }
    Ok(out)
}

/// [`resolve_env_from_secrets_with`] through [`resolve_module_secret`]:
/// `project_id` is the project a per-project container serves, `None` for a
/// global container.
pub fn resolve_env_from_secrets(
    manifest: &ModuleManifest,
    project_id: Option<&str>,
    db: &Db,
) -> Result<Vec<(String, String)>, String> {
    resolve_env_from_secrets_with(manifest, |decl| {
        resolve_module_secret(db, &manifest.id, &decl.key, &decl.scope, project_id)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn manifest(listed: &[&str]) -> ModuleManifest {
        let raw = format!(
            r#"{{
              "id": "vct-sec", "name": "Sec", "version": "1.0.0", "category": "core",
              "license": {{ "required": false }},
              "install": {{ "method": "local", "install_dir": "{{VCT_MODULES}}/vct-sec" }},
              "secrets": [
                {{ "key": "API_TOKEN", "scope": "per-project" }},
                {{ "key": "OPT_TOKEN", "scope": "global", "required": false }},
                {{ "key": "SHARED_TOKEN", "scope": "shared", "required": false }},
                {{ "key": "NOT_LISTED", "scope": "global", "required": false }}
              ],
              "runtime": {{ "type": "container", "env_from_secrets": {} }}
            }}"#,
            serde_json::to_string(listed).unwrap()
        );
        ModuleManifest::from_json(&raw).expect("fixture parses")
    }

    fn pair(k: &str, v: &str) -> (String, String) {
        (k.to_string(), v.to_string())
    }

    /// Only listed keys are looked up and injected; an optional secret that
    /// is paused or unset is skipped; a listed-but-undeclared key is skipped.
    #[test]
    fn listed_only_and_optional_misses_are_skipped() {
        let m = manifest(&["API_TOKEN", "OPT_TOKEN", "SHARED_TOKEN", "UNDECLARED"]);
        let got = resolve_env_from_secrets_with(&m, |d| match d.key.as_str() {
            "API_TOKEN" => SecretLookup::Value("v-api".into()),
            "OPT_TOKEN" => SecretLookup::Inactive,
            "SHARED_TOKEN" => SecretLookup::Value("  ".into()),
            "NOT_LISTED" => panic!("an unlisted secret must never be looked up"),
            _ => SecretLookup::Missing,
        })
        .unwrap();
        assert_eq!(got, vec![pair("API_TOKEN", "v-api")]);
    }

    /// A `required` secret (the manifest default) that does not resolve
    /// refuses the start; the error names the key and reason, not a value.
    #[test]
    fn a_required_secret_that_does_not_resolve_refuses_the_start() {
        let m = manifest(&["OPT_TOKEN", "API_TOKEN"]);
        for miss in [SecretLookup::Inactive, SecretLookup::Missing, SecretLookup::ReadError("x".into())] {
            let reason = miss.reason();
            let cell = std::cell::RefCell::new(Some(miss));
            let err = resolve_env_from_secrets_with(&m, |d| match d.key.as_str() {
                "OPT_TOKEN" => SecretLookup::Value("secret-opt".into()),
                _ => cell.borrow_mut().take().unwrap(),
            })
            .unwrap_err();
            assert!(err.contains("API_TOKEN") && err.contains(reason), "{err}");
            assert!(!err.contains("secret-opt"), "{err}");
        }
    }

    /// End to end through the real gate + the (mock) keychain: a stored,
    /// active secret resolves; pausing it for the requesting project makes it
    /// unresolvable (the required one then refuses); a global spawn cannot
    /// resolve a per-project secret.
    #[test]
    fn the_real_gate_serves_active_and_refuses_paused_secrets() {
        let _state = crate::test_env::state_dir_guard_with(&[]);
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let db = Db::open_in_memory().unwrap();
        crate::secrets::set(SecretScope::PerProject { project_id: "p1" }, "vct-sec", "API_TOKEN", "v-api")
            .unwrap();
        crate::secrets::set(SecretScope::Global, "vct-sec", "OPT_TOKEN", "v-opt").unwrap();
        let m = manifest(&["API_TOKEN", "OPT_TOKEN"]);

        assert_eq!(
            resolve_env_from_secrets(&m, Some("p1"), &db).unwrap(),
            vec![pair("API_TOKEN", "v-api"), pair("OPT_TOKEN", "v-opt")]
        );

        // Pause the global optional secret for p1 only: skipped for p1.
        db.mark_secret_inactive_for_requester("global", SENTINEL_GLOBAL, "vct-sec", "OPT_TOKEN", "p1")
            .unwrap();
        assert_eq!(
            resolve_env_from_secrets(&m, Some("p1"), &db).unwrap(),
            vec![pair("API_TOKEN", "v-api")]
        );

        // Pause the required per-project secret: the start is refused.
        db.mark_secret_inactive_for_requester("per_project", "p1", "vct-sec", "API_TOKEN", "p1")
            .unwrap();
        let err = resolve_env_from_secrets(&m, Some("p1"), &db).unwrap_err();
        assert!(err.contains("API_TOKEN") && !err.contains("v-api"), "{err}");

        // A global spawn: the per-project secret has no project.
        assert!(matches!(
            resolve_module_secret(&db, "vct-sec", "API_TOKEN", "per-project", None),
            SecretLookup::NoProject
        ));
        assert_eq!(format!("{:?}", SecretLookup::Value("v".into())), "Value(<redacted>)");
    }
}
