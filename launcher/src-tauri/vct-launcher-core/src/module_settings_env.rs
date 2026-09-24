// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! `runtime.env_from_settings`, read (v0.2.97, lane V).
//!
//! `docs/VCT_MODULE_MANIFEST_SPEC.md` §8 says a module's settings are
//! "injected via `runtime.env_from_settings`". Until v0.2.97 nothing read the
//! field: the container spawners passed `env_fixed` + `env_derived` only.
//! This is the ONE resolver of the list, used by every place VCO itself
//! starts a module process — the container/service spawns in
//! `vct_hub::module_supervisor` (per-project and global) and the launcher's
//! per-project spawn (`commands::module_service`). They hand the pairs to
//! `services::container_runtime::build_podman_run_args_with_env` /
//! `build_podman_run_args_global`, which put them on `podman run` as `-e`.
//!
//! Rules, per listed key, in list order (a repeated key counts once):
//!
//! * Only keys in `runtime.env_from_settings` are injected — a declared
//!   setting that is not listed is never put in the spawned env.
//! * Value: the project's `module_settings` row (when the spawn is for a
//!   project and the declaration is not `scope: "global"`), else the
//!   machine-wide row (`project_id IS NULL`), else the declaration's
//!   `default`. A JSON string is used as is; any other JSON value as its JSON
//!   text (`11440`, `true`) — the same conversion the hub's `/env` uses. A
//!   `null` / missing value injects nothing.
//! * A key that is not a portable environment-variable name
//!   (`[A-Za-z_][A-Za-z0-9_]*`) is skipped with a warning.
//!
//! Settings are not secrets (secrets are `secrets[]`, resolved from the
//! keychain), but callers still never log the resulting pairs.

use serde_json::Value;

use crate::db::Db;
use crate::manifest::ModuleManifest;

/// True for a name every OS accepts as an environment variable.
pub fn is_env_var_name(name: &str) -> bool {
    let mut chars = name.chars();
    match chars.next() {
        Some(c) if c.is_ascii_alphabetic() || c == '_' => {}
        _ => return false,
    }
    chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// A stored/default setting value as an env value; `None` for `null`.
fn env_value(v: &Value) -> Option<String> {
    match v {
        Value::Null => None,
        Value::String(s) => Some(s.clone()),
        other => Some(other.to_string()),
    }
}

/// The resolver, over an injected lookup. `lookup(Some(project_id), key)`
/// answers the project's row, `lookup(None, key)` the machine-wide row.
pub fn resolve_env_from_settings_with(
    manifest: &ModuleManifest,
    project_id: Option<&str>,
    lookup: impl Fn(Option<&str>, &str) -> Option<Value>,
) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for key in &manifest.runtime.env_from_settings {
        if out.iter().any(|(k, _)| k == key) {
            continue;
        }
        if !is_env_var_name(key) {
            tracing::warn!(
                module_id = %manifest.id,
                key = %key,
                "[module_settings_env] runtime.env_from_settings names a key that is not an \
                 environment-variable name; not injected"
            );
            continue;
        }
        let decl = manifest.settings.iter().find(|s| &s.key == key);
        let machine_wide_only = decl.is_some_and(crate::module_settings_schema::is_global);
        let project_value = match project_id {
            Some(pid) if !machine_wide_only => lookup(Some(pid), key),
            _ => None,
        };
        let value = project_value
            .and_then(|v| env_value(&v))
            .or_else(|| lookup(None, key).and_then(|v| env_value(&v)))
            .or_else(|| decl.and_then(|d| env_value(&d.default)));
        if let Some(v) = value {
            out.push((key.clone(), v));
        }
    }
    out
}

/// [`resolve_env_from_settings_with`] against the launcher DB. A row that
/// cannot be read or parsed counts as absent (the next source then applies)
/// and is logged — a spawn never fails because one setting row is corrupt.
pub fn resolve_env_from_settings(
    manifest: &ModuleManifest,
    project_id: Option<&str>,
    db: &Db,
) -> Vec<(String, String)> {
    resolve_env_from_settings_with(manifest, project_id, |pid, key| {
        let read = match pid {
            Some(pid) => db.get_setting(pid, &manifest.id, key),
            None => db.get_global_setting(&manifest.id, key),
        };
        match read {
            Ok(v) => v,
            Err(e) => {
                tracing::warn!(
                    module_id = %manifest.id,
                    key = %key,
                    error = %e,
                    "[module_settings_env] setting row unreadable; using the next source"
                );
                None
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn manifest(listed: &[&str], settings: &str) -> ModuleManifest {
        let listed_json = serde_json::to_string(listed).unwrap();
        let raw = format!(
            r#"{{
              "id": "vct-example", "name": "Example", "version": "1.0.0",
              "category": "core", "license": {{ "required": false }},
              "install": {{ "method": "local", "install_dir": "{{VCT_MODULES}}/vct-example" }},
              "settings": {settings},
              "runtime": {{ "type": "cli", "env_from_settings": {listed_json} }}
            }}"#
        );
        ModuleManifest::from_json(&raw).expect("fixture parses")
    }

    const SETTINGS: &str = r#"[
      { "key": "MODE", "type": "string", "default": "fast" },
      { "key": "PORT", "type": "integer", "default": 11440 },
      { "key": "MACHINE", "type": "string", "default": "m-default", "scope": "global" },
      { "key": "UNLISTED", "type": "string", "default": "never" },
      { "key": "NULLISH", "type": "string", "required": false }
    ]"#;

    type Table = HashMap<(Option<String>, String), Value>;

    fn rows(pairs: &[(Option<&str>, &str, Value)]) -> Table {
        pairs
            .iter()
            .map(|(p, k, v)| ((p.map(str::to_string), k.to_string()), v.clone()))
            .collect()
    }

    fn resolve(m: &ModuleManifest, project: Option<&str>, table: &Table) -> Vec<(String, String)> {
        resolve_env_from_settings_with(m, project, |p, k| {
            table.get(&(p.map(str::to_string), k.to_string())).cloned()
        })
    }

    fn pair(k: &str, v: &str) -> (String, String) {
        (k.to_string(), v.to_string())
    }

    /// Project row → machine-wide row → default, per key; an unlisted
    /// declared setting never appears; a `null` default injects nothing.
    #[test]
    fn listed_settings_resolve_project_then_machine_then_default() {
        let m = manifest(&["MODE", "PORT", "MACHINE", "NULLISH"], SETTINGS);
        let table = rows(&[
            (Some("p1"), "MODE", Value::String("slow".into())),
            (None, "MODE", Value::String("machine-mode".into())),
            (None, "PORT", serde_json::json!(12000)),
            (Some("p1"), "UNLISTED", Value::String("leak".into())),
        ]);
        assert_eq!(
            resolve(&m, Some("p1"), &table),
            vec![pair("MODE", "slow"), pair("PORT", "12000"), pair("MACHINE", "m-default")]
        );
        // Another project: no row of its own → the machine-wide value.
        assert_eq!(resolve(&m, Some("p2"), &table)[0], pair("MODE", "machine-mode"));
        // A global spawn (no project) never reads a project row.
        assert_eq!(resolve(&m, None, &table)[0], pair("MODE", "machine-mode"));
    }

    /// A `scope: "global"` setting ignores a project row, even when one
    /// exists (e.g. written before the declaration said global).
    #[test]
    fn a_global_setting_ignores_project_rows() {
        let m = manifest(&["MACHINE"], SETTINGS);
        let table = rows(&[
            (Some("p1"), "MACHINE", Value::String("project-copy".into())),
            (None, "MACHINE", Value::String("the-one".into())),
        ]);
        assert_eq!(resolve(&m, Some("p1"), &table), vec![pair("MACHINE", "the-one")]);
    }

    /// Nothing listed → nothing injected, whatever is stored or declared.
    #[test]
    fn an_empty_list_injects_nothing() {
        let m = manifest(&[], SETTINGS);
        let table = rows(&[(Some("p1"), "MODE", Value::String("slow".into()))]);
        assert!(resolve(&m, Some("p1"), &table).is_empty());
    }

    /// A listed key without a declaration still takes a stored value (a
    /// published manifest may list a key it never declared) and has no
    /// default; a non-env-name key and a repeat are dropped.
    #[test]
    fn undeclared_invalid_and_repeated_keys() {
        let m = manifest(&["ACTIVE_EMBEDDING", "BAD-NAME", "MODE", "MODE", "UNSET"], SETTINGS);
        let table = rows(&[(None, "ACTIVE_EMBEDDING", Value::String("qwen3".into()))]);
        assert_eq!(
            resolve(&m, Some("p1"), &table),
            vec![pair("ACTIVE_EMBEDDING", "qwen3"), pair("MODE", "fast")]
        );
        assert!(is_env_var_name("_A1") && !is_env_var_name("1A") && !is_env_var_name("A=B"));
    }

    /// The DB-backed form reads the real `module_settings` machine-wide row.
    #[test]
    fn db_backed_resolver_reads_the_machine_wide_row() {
        let db = Db::open_in_memory().expect("db");
        let m = manifest(&["MODE"], SETTINGS);
        assert_eq!(resolve_env_from_settings(&m, None, &db), vec![pair("MODE", "fast")]);
        db.set_global_setting("vct-example", "MODE", &Value::String("stored".into())).unwrap();
        assert_eq!(
            resolve_env_from_settings(&m, Some("no-such-project"), &db),
            vec![pair("MODE", "stored")]
        );
    }
}
