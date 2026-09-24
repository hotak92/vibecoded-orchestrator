// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! A module manifest's `settings` block, made enforceable (v0.2.97).
//!
//! `docs/VCT_MODULE_MANIFEST_SPEC.md` §8 says manifest settings are
//! "user-editable via the launcher GUI; persisted in the generic
//! `module_settings` KV store". Before this module no launcher surface read
//! the `settings` block at all — a bundled module's settings
//! (`launcher/bundled_manifests/`, e.g. `vct-hub-api`'s machine-wide
//! `VCT_HUB_PORT`) were writable only from Rust code, and nothing checked a
//! written value against the manifest's `type` / `min` / `max` / `options` /
//! `validation`.
//!
//! This is the ONE home for:
//!
//! * the declared scope of a setting (`per-project` vs machine-wide `global`)
//!   and where a write for it lands ([`resolve_setting_target`]);
//! * value validation against the declaration ([`validate_setting_value`]) —
//!   the launcher UI checks too (`launcher/src/lib/module-settings.ts`, for
//!   immediate feedback), but the write command never trusts it: every write
//!   of a declared setting goes through [`write_module_setting`];
//! * the settings the bundled manifests declare ([`bundled_module_settings`]),
//!   which the launcher's Preferences → Modules page lists.
//!
//! The UI's mirror of [`validate_setting_value`] is locked to this one by the
//! shared case table `tests/fixtures/setting_validation_cases.json`, which both
//! test suites run (must match `launcher/src/lib/module-settings.ts`).
//!
//! `validation_cmd` is NOT run here: a settings write never executes a
//! manifest-supplied command.

use serde::Serialize;
use serde_json::Value;

use crate::db::Db;
use crate::manifest::{ModuleManifest, SettingDecl};
use crate::module_setting_bindings::{self, SettingBinding};

/// One value per project (the default).
pub const SCOPE_PER_PROJECT: &str = "per-project";
/// One machine-wide value — the `module_settings` row with no project.
pub const SCOPE_GLOBAL: &str = "global";

/// Parse-time check of a declaration's `scope` (called from
/// `ModuleManifest::from_json`). `per_project` (the spelling `install.scope`
/// uses) is accepted as `per-project`: before v0.2.97 this field was ignored,
/// so a published manifest carrying it must not start failing to parse.
pub fn check_setting_scope(decl: &SettingDecl) -> Result<(), String> {
    match decl.scope.as_str() {
        SCOPE_PER_PROJECT | "per_project" | SCOPE_GLOBAL => Ok(()),
        other => Err(format!(
            "setting '{}' has invalid scope '{}' (expected \"{}\" or \"{}\")",
            decl.key, other, SCOPE_PER_PROJECT, SCOPE_GLOBAL
        )),
    }
}

/// True for a machine-wide (`scope: "global"`) setting.
pub fn is_global(decl: &SettingDecl) -> bool {
    decl.scope == SCOPE_GLOBAL
}

fn label(decl: &SettingDecl) -> &str {
    if decl.prompt.trim().is_empty() {
        &decl.key
    } else {
        &decl.prompt
    }
}

/// Check `value` against its declaration. `Ok(())` means the value may be
/// stored; `Err` carries a one-line, user-facing reason.
///
/// Rules (must match `checkSettingValue` in `launcher/src/lib/module-settings.ts`):
/// * `integer` — a JSON whole number (never a numeric string), within
///   `min` / `max` when declared;
/// * `boolean` — a JSON boolean;
/// * `string` / `path` — a JSON string; `required` refuses a blank one; a
///   non-blank one must be one of `options` (when declared) and must match
///   `validation` (when declared — an unanchored regex search);
/// * `multiselect` — a JSON array of strings, each one of `options` (when
///   declared); `required` refuses an empty array;
/// * any other `type` is refused — a value for a type this launcher does not
///   know cannot be checked.
pub fn validate_setting_value(decl: &SettingDecl, value: &Value) -> Result<(), String> {
    let name = label(decl);
    match decl.r#type.as_str() {
        "integer" => {
            let n = match value {
                Value::Number(n) => n
                    .as_i64()
                    .or_else(|| n.as_f64().filter(|f| f.fract() == 0.0 && f.abs() < 9.0e15).map(|f| f as i64)),
                _ => None,
            };
            let Some(n) = n else {
                return Err(format!("{name}: must be a whole number"));
            };
            if let Some(min) = decl.min {
                if n < min {
                    return Err(format!("{name}: must be at least {min}"));
                }
            }
            if let Some(max) = decl.max {
                if n > max {
                    return Err(format!("{name}: must be at most {max}"));
                }
            }
            Ok(())
        }
        "boolean" => match value {
            Value::Bool(_) => Ok(()),
            _ => Err(format!("{name}: must be true or false")),
        },
        "string" | "path" => {
            let Value::String(s) = value else {
                return Err(format!("{name}: must be text"));
            };
            if s.trim().is_empty() {
                return if decl.required {
                    Err(format!("{name}: is required"))
                } else {
                    Ok(())
                };
            }
            if !decl.options.is_empty() && !decl.options.iter().any(|o| o == s) {
                return Err(format!("{name}: must be one of {}", decl.options.join(", ")));
            }
            if let Some(pattern) = decl.validation.as_deref().filter(|p| !p.is_empty()) {
                let re = regex::Regex::new(pattern).map_err(|e| {
                    format!("{name}: the module's validation pattern is invalid ({e})")
                })?;
                if !re.is_match(s) {
                    return Err(format!("{name}: does not match the required format {pattern}"));
                }
            }
            Ok(())
        }
        "multiselect" => {
            let Value::Array(items) = value else {
                return Err(format!("{name}: must be a list"));
            };
            if items.is_empty() && decl.required {
                return Err(format!("{name}: pick at least one"));
            }
            for item in items {
                let Value::String(s) = item else {
                    return Err(format!("{name}: every choice must be text"));
                };
                if !decl.options.is_empty() && !decl.options.iter().any(|o| o == s) {
                    return Err(format!("{name}: '{s}' is not one of {}", decl.options.join(", ")));
                }
            }
            Ok(())
        }
        other => Err(format!("{name}: unsupported setting type '{other}'")),
    }
}

/// The settings one module declares, as the launcher's settings editor
/// receives them.
#[derive(Debug, Clone, Serialize)]
pub struct DeclaredModuleSettings {
    pub module_id: String,
    pub name: String,
    pub settings: Vec<SettingDecl>,
}

/// The declared settings of every manifest in `manifests` that declares any.
pub fn declared_settings_of(manifests: &[ModuleManifest]) -> Vec<DeclaredModuleSettings> {
    manifests
        .iter()
        .filter(|m| !m.settings.is_empty())
        .map(|m| DeclaredModuleSettings {
            module_id: m.id.clone(),
            name: m.name.clone(),
            settings: m.settings.clone(),
        })
        .collect()
}

/// The settings the launcher's BUNDLED manifests declare
/// ([`crate::bundled_manifests::BUNDLED_MANIFESTS`], embedded at compile
/// time). A manifest that does not parse is skipped — a test pins that every
/// one parses.
pub fn bundled_module_settings() -> Vec<DeclaredModuleSettings> {
    let manifests: Vec<ModuleManifest> = crate::bundled_manifests::BUNDLED_MANIFESTS
        .iter()
        .filter_map(|(_, body)| ModuleManifest::from_json(body).ok())
        .collect();
    declared_settings_of(&manifests)
}

/// The declaration of `module_id`'s `key`, if `modules` holds one.
pub fn find_setting<'a>(
    modules: &'a [DeclaredModuleSettings],
    module_id: &str,
    key: &str,
) -> Option<&'a SettingDecl> {
    modules
        .iter()
        .find(|m| m.module_id == module_id)
        .and_then(|m| m.settings.iter().find(|s| s.key == key))
}

/// Which `module_settings` row a read or write addresses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SettingTarget {
    /// The machine-wide row (`project_id IS NULL`).
    Global,
    /// One project's row.
    Project(String),
}

/// Route a WRITE. A declared `global` setting lands in the machine-wide row
/// and refuses a project (a per-project row for it would be read by nothing);
/// everything else — a declared per-project setting, or a value no manifest
/// declares (a `gui.config_tab` control's state) — needs a project.
pub fn resolve_setting_target(
    decl: Option<&SettingDecl>,
    module_id: &str,
    key: &str,
    project_id: Option<&str>,
) -> Result<SettingTarget, String> {
    let project = project_id.map(str::trim).filter(|p| !p.is_empty());
    match (decl.is_some_and(is_global), project) {
        (true, None) => Ok(SettingTarget::Global),
        (true, Some(_)) => Err(format!(
            "{module_id}/{key} is a machine-wide setting — save it without a project"
        )),
        (false, Some(p)) => Ok(SettingTarget::Project(p.to_string())),
        (false, None) => Err(format!(
            "{module_id}/{key} is a per-project setting — a project is required"
        )),
    }
}

/// Which manifest a declaration came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DeclOrigin {
    /// A manifest bundled with the launcher — present for every project.
    Bundled,
    /// An installed (catalog) module's manifest — present only where the
    /// module is installed / enabled ([`projects_offering_module`]).
    Installed,
}

/// A setting declaration and where it came from.
#[derive(Debug, Clone)]
pub struct FoundSetting {
    pub decl: SettingDecl,
    pub origin: DeclOrigin,
}

impl FoundSetting {
    /// Its [`SettingBinding`]: the bundled table's row, or
    /// [`module_setting_bindings::CATALOG_BINDING`] for an installed module.
    /// A bundled setting missing from the table is treated as stored (a test
    /// keeps the table complete).
    pub fn binding(&self, module_id: &str) -> SettingBinding {
        match self.origin {
            DeclOrigin::Bundled => module_setting_bindings::bundled_binding(module_id, &self.decl.key)
                .unwrap_or(module_setting_bindings::CATALOG_BINDING),
            DeclOrigin::Installed => module_setting_bindings::CATALOG_BINDING,
        }
    }
}

/// The projects an installed (catalog) module's per-project settings may be
/// edited for: every project with an ENABLED per-project install row, plus —
/// when the module is installed machine-wide (a global install row) — every
/// project the enable cascade (`Db::module_effective_enabled`) leaves it on
/// for. Sorted, deduplicated. A project outside this list has no use for the
/// module's settings, so no row is offered or written for it.
pub fn projects_offering_module(db: &Db, module_id: &str) -> Result<Vec<String>, String> {
    let mut out: Vec<String> = db
        .list_per_project_installs_for_module(module_id)?
        .into_iter()
        .filter(|r| r.enabled)
        .filter_map(|r| r.project_id)
        .collect();
    if db.get_global_module_install(module_id)?.is_some() {
        for p in db.list_projects()? {
            if db.module_effective_enabled(&p.id, module_id)? {
                out.push(p.id);
            }
        }
    }
    out.sort();
    out.dedup();
    Ok(out)
}

/// One declared setting as the settings editor lists it.
#[derive(Debug, Clone, Serialize)]
pub struct ListedSetting {
    #[serde(flatten)]
    pub decl: SettingDecl,
    pub binding: SettingBinding,
}

/// One module's settings as the settings editor lists them.
#[derive(Debug, Clone, Serialize)]
pub struct ListedModuleSettings {
    pub module_id: String,
    pub name: String,
    pub origin: DeclOrigin,
    /// The projects its per-project settings may be edited for; `None` =
    /// every project (a bundled module).
    pub projects: Option<Vec<String>>,
    pub settings: Vec<ListedSetting>,
}

/// Every module whose settings the editor lists: the bundled modules (for
/// every project), then each installed (catalog) module in `installed` that
/// declares settings and is installed somewhere (for the projects
/// [`projects_offering_module`] names). A module id that is bundled is never
/// listed twice.
pub fn list_module_settings(
    db: &Db,
    installed: &[ModuleManifest],
) -> Result<Vec<ListedModuleSettings>, String> {
    let mut out: Vec<ListedModuleSettings> = bundled_module_settings()
        .into_iter()
        .map(|m| ListedModuleSettings {
            settings: m
                .settings
                .into_iter()
                .map(|decl| ListedSetting {
                    binding: module_setting_bindings::bundled_binding(&m.module_id, &decl.key)
                        .unwrap_or(module_setting_bindings::CATALOG_BINDING),
                    decl,
                })
                .collect(),
            module_id: m.module_id,
            name: m.name,
            origin: DeclOrigin::Bundled,
            projects: None,
        })
        .collect();
    for m in declared_settings_of(installed) {
        if out.iter().any(|o| o.module_id == m.module_id) {
            continue;
        }
        let installed_anywhere = db.get_global_module_install(&m.module_id)?.is_some()
            || !db.list_per_project_installs_for_module(&m.module_id)?.is_empty();
        if !installed_anywhere {
            continue;
        }
        out.push(ListedModuleSettings {
            projects: Some(projects_offering_module(db, &m.module_id)?),
            settings: m
                .settings
                .into_iter()
                .map(|decl| ListedSetting { decl, binding: module_setting_bindings::CATALOG_BINDING })
                .collect(),
            module_id: m.module_id,
            name: m.name,
            origin: DeclOrigin::Installed,
        });
    }
    Ok(out)
}

/// Validate (when declared) and store one setting value — the write path of
/// the `set_module_setting` command. Nothing is written when any check
/// refuses:
/// * a bundled setting whose live value lives elsewhere
///   ([`SettingBinding::Elsewhere`]) is never stored here — no second copy;
/// * the value must pass [`validate_setting_value`];
/// * the scope routing of [`resolve_setting_target`];
/// * an installed module's per-project setting only for a project that
///   module is installed / enabled in ([`projects_offering_module`]).
pub fn write_module_setting(
    db: &Db,
    found: Option<&FoundSetting>,
    module_id: &str,
    key: &str,
    project_id: Option<&str>,
    value: &Value,
) -> Result<(), String> {
    if let Some(f) = found {
        if let SettingBinding::Elsewhere { home, .. } = f.binding(module_id) {
            return Err(format!("{module_id}/{key} is not stored in module settings: {home}"));
        }
        validate_setting_value(&f.decl, value)?;
    }
    let decl = found.map(|f| &f.decl);
    let target = resolve_setting_target(decl, module_id, key, project_id)?;
    if let (Some(f), SettingTarget::Project(p)) = (found, &target) {
        if f.origin == DeclOrigin::Installed && !projects_offering_module(db, module_id)?.contains(p) {
            return Err(format!(
                "{module_id} is not installed or enabled for this project — its settings \
                 are edited only where it is"
            ));
        }
    }
    match target {
        SettingTarget::Global => db.set_global_setting(module_id, key, value),
        SettingTarget::Project(p) => db.set_setting(&p, module_id, key, value),
    }
}

/// Read one setting value — the read path of the `get_module_setting`
/// command. A declared `global` setting always reads the machine-wide row
/// (the value in effect), whether or not a project is passed.
pub fn read_module_setting(
    db: &Db,
    found: Option<&FoundSetting>,
    module_id: &str,
    key: &str,
    project_id: Option<&str>,
) -> Result<Option<Value>, String> {
    let decl = found.map(|f| &f.decl);
    if decl.is_some_and(is_global) {
        return db.get_global_setting(module_id, key);
    }
    match resolve_setting_target(decl, module_id, key, project_id)? {
        SettingTarget::Global => db.get_global_setting(module_id, key),
        SettingTarget::Project(p) => db.get_setting(&p, module_id, key),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn decl(v: Value) -> SettingDecl {
        serde_json::from_value(v).expect("decl parses")
    }

    fn db_with_project() -> (Db, String) {
        let db = Db::open_in_memory().expect("db");
        let pid = "p-settings-1".to_string();
        db.insert_project(&pid, "P", "/tmp/p", crate::db::models::ProjectHost::Base, "p")
            .expect("project");
        (db, pid)
    }

    fn hub_port() -> SettingDecl {
        let modules = bundled_module_settings();
        find_setting(&modules, "vct-hub-api", "VCT_HUB_PORT").cloned().expect("declared")
    }

    fn bundled(module_id: &str, key: &str) -> FoundSetting {
        let modules = bundled_module_settings();
        FoundSetting {
            decl: find_setting(&modules, module_id, key).cloned().expect("declared"),
            origin: DeclOrigin::Bundled,
        }
    }

    /// An installed (catalog) module: the session-state manifest under
    /// another id, so it declares two per-project integer settings.
    fn catalog_manifest() -> ModuleManifest {
        let (_, body) = crate::bundled_manifests::BUNDLED_MANIFESTS
            .iter()
            .find(|(n, _)| *n == "vct-session-state.json")
            .unwrap();
        let body = body.replace("\"id\": \"vct-session-state\"", "\"id\": \"vct-test-catalog\"");
        ModuleManifest::from_json(&body).expect("catalog fixture parses")
    }

    fn catalog(key: &str) -> FoundSetting {
        let m = catalog_manifest();
        FoundSetting {
            decl: m.settings.iter().find(|s| s.key == key).cloned().unwrap(),
            origin: DeclOrigin::Installed,
        }
    }

    /// p1 has the catalog module installed + enabled, p2 installed but
    /// disabled, p3 never installed.
    fn db_with_catalog_installs() -> Db {
        let db = Db::open_in_memory().expect("db");
        for p in ["p1", "p2", "p3"] {
            db.insert_project(p, p, &format!("/tmp/{p}"), crate::db::models::ProjectHost::Base, p).unwrap();
        }
        db.insert_module_install("i1", "p1", "vct-test-catalog", "1.0.0", "/tmp/i1").unwrap();
        db.insert_module_install("i2", "p2", "vct-test-catalog", "1.0.0", "/tmp/i2").unwrap();
        db.set_module_enabled("p2", "vct-test-catalog", false).unwrap();
        db
    }

    /// Round 2 (decision 3): a bundled setting whose live value has another
    /// home is never stored here — the write is refused and no row appears.
    #[test]
    fn a_setting_bound_elsewhere_is_never_stored() {
        let (db, pid) = db_with_project();
        let kg = bundled("vct-kg", "KG_COLLECTION");
        let err = write_module_setting(&db, Some(&kg), "vct-kg", "KG_COLLECTION", Some(&pid), &json!("X_KnowledgeGraph"))
            .unwrap_err();
        assert!(err.contains("Identity tab"), "{err}");
        assert_eq!(db.get_setting(&pid, "vct-kg", "KG_COLLECTION").unwrap(), None);
        let url = bundled("vct-kg", "WEAVIATE_URL");
        assert!(write_module_setting(&db, Some(&url), "vct-kg", "WEAVIATE_URL", None, &json!("http://x:1")).is_err());
        assert_eq!(db.get_global_setting("vct-kg", "WEAVIATE_URL").unwrap(), None);
        // The stored ones still write.
        let port = bundled("vct-hub-api", "VCT_HUB_PORT");
        assert!(write_module_setting(&db, Some(&port), "vct-hub-api", "VCT_HUB_PORT", None, &json!(8802)).is_ok());
    }

    /// Round 2 (decision 1): an installed module's settings are listed, for
    /// exactly the projects it is installed AND enabled in.
    #[test]
    fn installed_module_settings_are_listed_for_the_projects_it_is_in() {
        let db = db_with_catalog_installs();
        let listed = list_module_settings(&db, &[catalog_manifest()]).unwrap();
        let cat = listed.iter().find(|m| m.module_id == "vct-test-catalog").expect("listed");
        assert_eq!(cat.origin, DeclOrigin::Installed);
        assert_eq!(cat.projects, Some(vec!["p1".to_string()]), "p2 is disabled, p3 never installed");
        assert_eq!(
            cat.settings.iter().map(|s| s.decl.key.as_str()).collect::<Vec<_>>(),
            vec!["CONTEXT_STATE_MAX_LINES", "MEMORY_MAX_LINES"]
        );
        assert!(cat.settings.iter().all(|s| s.binding.is_stored()));
        // The bundled modules come first, for every project, with bindings.
        let kg = listed.iter().find(|m| m.module_id == "vct-kg").unwrap();
        assert_eq!(kg.projects, None);
        assert!(!kg.settings.iter().any(|s| s.binding.is_stored()), "every vct-kg field lives elsewhere");
        // A manifest whose module is installed nowhere is not listed.
        let empty = Db::open_in_memory().unwrap();
        let listed = list_module_settings(&empty, &[catalog_manifest()]).unwrap();
        assert!(!listed.iter().any(|m| m.module_id == "vct-test-catalog"));
    }

    /// A GLOBAL install offers every project the enable cascade leaves the
    /// module on for.
    #[test]
    fn a_global_install_offers_every_enabled_project() {
        let db = Db::open_in_memory().unwrap();
        for p in ["p1", "p2"] {
            db.insert_project(p, p, &format!("/tmp/{p}"), crate::db::models::ProjectHost::Base, p).unwrap();
        }
        db.insert_global_module_install("g1", "vct-test-catalog", "1.0.0", "/tmp/g1").unwrap();
        assert_eq!(projects_offering_module(&db, "vct-test-catalog").unwrap(), vec!["p1", "p2"]);
        db.module_set_enabled_for_project("p2", "vct-test-catalog", false).unwrap();
        assert_eq!(projects_offering_module(&db, "vct-test-catalog").unwrap(), vec!["p1"]);
    }

    /// The write gate for an installed module: validated, and only for a
    /// project it is installed + enabled in — no orphan row elsewhere.
    #[test]
    fn an_installed_modules_setting_is_validated_and_refused_outside_its_projects() {
        let db = db_with_catalog_installs();
        let f = catalog("MEMORY_MAX_LINES");
        write_module_setting(&db, Some(&f), "vct-test-catalog", "MEMORY_MAX_LINES", Some("p1"), &json!(300)).unwrap();
        assert_eq!(db.get_setting("p1", "vct-test-catalog", "MEMORY_MAX_LINES").unwrap(), Some(json!(300)));
        let err = write_module_setting(&db, Some(&f), "vct-test-catalog", "MEMORY_MAX_LINES", Some("p1"), &json!(9999))
            .unwrap_err();
        assert!(err.contains("at most 2000"), "{err}");
        for p in ["p2", "p3"] {
            let err = write_module_setting(&db, Some(&f), "vct-test-catalog", "MEMORY_MAX_LINES", Some(p), &json!(300))
                .unwrap_err();
            assert!(err.contains("not installed or enabled"), "{p}: {err}");
            assert_eq!(db.get_setting(p, "vct-test-catalog", "MEMORY_MAX_LINES").unwrap(), None, "{p}");
        }
    }

    /// The shared case table — the SAME file `module-settings.test.ts` runs,
    /// so the UI's check and this one accept and refuse the same values.
    #[test]
    fn the_shared_case_table_passes() {
        let raw = include_str!("../tests/fixtures/setting_validation_cases.json");
        let cases: Vec<Value> = serde_json::from_str(raw).expect("case table parses");
        assert!(cases.len() >= 20, "the table covers every type, accept and reject");
        let mut accepted = 0;
        for case in &cases {
            let d = decl(case["decl"].clone());
            let ok = case["ok"].as_bool().expect("ok is a bool");
            let got = validate_setting_value(&d, &case["value"]);
            assert_eq!(got.is_ok(), ok, "case {}: {:?}", case["name"], got);
            if ok {
                accepted += 1;
            }
        }
        assert!(accepted > 0 && accepted < cases.len(), "both outcomes are exercised");
    }

    #[test]
    fn integer_bounds_accept_and_reject() {
        let d = hub_port();
        assert!(validate_setting_value(&d, &json!(7700)).is_ok());
        assert!(validate_setting_value(&d, &json!(1024)).is_ok());
        assert!(validate_setting_value(&d, &json!(65535)).is_ok());
        assert!(validate_setting_value(&d, &json!(80)).unwrap_err().contains("at least 1024"));
        assert!(validate_setting_value(&d, &json!(70000)).unwrap_err().contains("at most 65535"));
        assert!(validate_setting_value(&d, &json!("7700")).is_err(), "a numeric string is not an integer");
        assert!(validate_setting_value(&d, &json!(7700.5)).is_err());
    }

    #[test]
    fn options_and_pattern_accept_and_reject() {
        let d = decl(json!({"key": "B", "type": "string", "options": ["gpu", "ollama"], "required": true}));
        assert!(validate_setting_value(&d, &json!("gpu")).is_ok());
        assert!(validate_setting_value(&d, &json!("cpu")).unwrap_err().contains("one of gpu, ollama"));
        assert!(validate_setting_value(&d, &json!("")).unwrap_err().contains("required"));
        let p = decl(json!({"key": "U", "type": "string", "validation": "^https?://"}));
        assert!(validate_setting_value(&p, &json!("http://x")).is_ok());
        assert!(validate_setting_value(&p, &json!("ftp://x")).is_err());
        assert!(validate_setting_value(&p, &json!("")).is_ok(), "blank optional is 'unset'");
    }

    /// Every bundled declaration is listed, and the scope the manifests
    /// declare comes through: the hub port is machine-wide, the
    /// session-state thresholds are per project.
    #[test]
    fn bundled_settings_are_listed_with_their_scope() {
        let modules = bundled_module_settings();
        let ids: Vec<&str> = modules.iter().map(|m| m.module_id.as_str()).collect();
        for id in ["vct-code-embedding", "vct-codegraph", "vct-hub-api", "vct-kg", "vct-session-state"] {
            assert!(ids.contains(&id), "{id} declares settings: {ids:?}");
        }
        assert!(!ids.contains(&"vct-search"), "a module with no settings is not listed");
        assert!(is_global(&hub_port()));
        let ctx = find_setting(&modules, "vct-session-state", "CONTEXT_STATE_MAX_LINES").unwrap();
        assert!(!is_global(ctx));
        let globals: Vec<&str> = modules
            .iter()
            .flat_map(|m| m.settings.iter())
            .filter(|s| is_global(s))
            .map(|s| s.key.as_str())
            .collect();
        assert_eq!(
            globals,
            vec!["CODE_EMBED_BACKEND", "CODE_EMBED_DEVICE", "CODE_EMBED_PORT", "CODE_EMBED_BACKEND", "VCT_HUB_PORT", "SHARED_KG_COLLECTION", "WEAVIATE_URL"],
            "machine-wide: the hub port and the machine's service settings"
        );
    }

    #[test]
    fn an_unknown_scope_is_refused_at_parse_time() {
        let bad = decl(json!({"key": "X", "scope": "shared"}));
        assert!(check_setting_scope(&bad).is_err());
        assert!(check_setting_scope(&decl(json!({"key": "X"}))).is_ok(), "default is per-project");
        assert!(check_setting_scope(&decl(json!({"key": "X", "scope": "global"}))).is_ok());
        let alias = decl(json!({"key": "X", "scope": "per_project"}));
        assert!(check_setting_scope(&alias).is_ok() && !is_global(&alias), "install.scope spelling");
        // Wired into the real parser: the hub manifest with its scope typo'd.
        let (_, body) = crate::bundled_manifests::BUNDLED_MANIFESTS
            .iter()
            .find(|(n, _)| *n == "vct-hub-api.json")
            .unwrap();
        assert!(body.contains("\"scope\": \"global\""), "the hub port declares its scope");
        let typo = body.replace("\"scope\": \"global\"", "\"scope\": \"machine\"");
        let err = ModuleManifest::from_json(&typo).unwrap_err().to_string();
        assert!(err.contains("invalid scope 'machine'"), "{err}");
    }

    #[test]
    fn routing_global_and_per_project() {
        let g = hub_port();
        let p = decl(json!({"key": "K"}));
        assert_eq!(resolve_setting_target(Some(&g), "m", "k", None), Ok(SettingTarget::Global));
        assert_eq!(resolve_setting_target(Some(&g), "m", "k", Some("")), Ok(SettingTarget::Global));
        assert!(resolve_setting_target(Some(&g), "m", "k", Some("p1")).unwrap_err().contains("machine-wide"));
        assert_eq!(
            resolve_setting_target(Some(&p), "m", "k", Some("p1")),
            Ok(SettingTarget::Project("p1".into()))
        );
        assert!(resolve_setting_target(Some(&p), "m", "k", None).unwrap_err().contains("project is required"));
        // Undeclared (a config_tab control's state): the legacy per-project rule.
        assert!(resolve_setting_target(None, "m", "k", None).is_err());
        assert_eq!(resolve_setting_target(None, "m", "k", Some("p1")), Ok(SettingTarget::Project("p1".into())));
    }

    /// The end-to-end point of the feature: a write of the hub port through
    /// the command's path lands in the row the hub reads at start
    /// (`Db::get_global_setting`, vct-hub `server.rs::bind_port`) — and an
    /// out-of-range value writes nothing.
    #[test]
    fn a_global_write_lands_in_the_row_the_hub_reads_and_a_bad_one_writes_nothing() {
        let (db, pid) = db_with_project();
        let d = bundled("vct-hub-api", "VCT_HUB_PORT");
        write_module_setting(&db, Some(&d), "vct-hub-api", "VCT_HUB_PORT", None, &json!(8802)).unwrap();
        assert_eq!(db.get_global_setting("vct-hub-api", "VCT_HUB_PORT").unwrap(), Some(json!(8802)));
        assert_eq!(
            read_module_setting(&db, Some(&d), "vct-hub-api", "VCT_HUB_PORT", Some(&pid)).unwrap(),
            Some(json!(8802)),
            "the global value is the one in effect for every project"
        );

        let err = write_module_setting(&db, Some(&d), "vct-hub-api", "VCT_HUB_PORT", None, &json!(80)).unwrap_err();
        assert!(err.contains("at least 1024"), "{err}");
        let err = write_module_setting(&db, Some(&d), "vct-hub-api", "VCT_HUB_PORT", Some(&pid), &json!(9000))
            .unwrap_err();
        assert!(err.contains("machine-wide"), "{err}");
        assert_eq!(db.get_global_setting("vct-hub-api", "VCT_HUB_PORT").unwrap(), Some(json!(8802)), "unchanged");
        assert_eq!(db.get_setting(&pid, "vct-hub-api", "VCT_HUB_PORT").unwrap(), None, "no dead project row");
    }

    /// A per-project write lands in the project's row — the one the hub's
    /// `/env` serves — and is validated too.
    #[test]
    fn a_per_project_write_lands_in_the_project_row_and_is_validated() {
        let (db, pid) = db_with_project();

        let d = &bundled("vct-session-state", "CONTEXT_STATE_MAX_LINES");
        write_module_setting(&db, Some(d), "vct-session-state", "CONTEXT_STATE_MAX_LINES", Some(&pid), &json!(800))
            .unwrap();
        assert_eq!(db.get_setting(&pid, "vct-session-state", "CONTEXT_STATE_MAX_LINES").unwrap(), Some(json!(800)));
        assert!(write_module_setting(&db, Some(d), "vct-session-state", "CONTEXT_STATE_MAX_LINES", Some(&pid), &json!(5))
            .is_err());
        assert!(write_module_setting(&db, Some(d), "vct-session-state", "CONTEXT_STATE_MAX_LINES", None, &json!(800))
            .is_err());
        assert_eq!(db.get_setting(&pid, "vct-session-state", "CONTEXT_STATE_MAX_LINES").unwrap(), Some(json!(800)));
        assert_eq!(db.get_global_setting("vct-session-state", "CONTEXT_STATE_MAX_LINES").unwrap(), None);
    }

    /// An undeclared key (a `gui.config_tab` control's state) keeps the
    /// pre-v0.2.97 behaviour: stored per project, not validated.
    #[test]
    fn an_undeclared_key_is_stored_unvalidated_per_project() {
        let (db, pid) = db_with_project();
        write_module_setting(&db, None, "vct-rl-reranker", "rl_use_global", Some(&pid), &json!({"any": "shape"}))
            .unwrap();
        assert_eq!(
            read_module_setting(&db, None, "vct-rl-reranker", "rl_use_global", Some(&pid)).unwrap(),
            Some(json!({"any": "shape"}))
        );
    }
}
