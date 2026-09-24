//! Stream 2 (2026-05-19): module-contributed GUI surfaces.
//!
//! Each installed module may declare a `gui.config_tab` block in its
//! `vct-module.json`. The launcher's Sidebar fetches the merged list
//! via `get_module_nav_items` and renders one nav entry per module
//! that has a populated config tab. The full `ConfigTab` schema is
//! shipped through alongside the nav metadata so the renderer
//! (`ModuleConfigTab.svelte`) has everything it needs without a
//! second Tauri round-trip.
//!
//! Generic state persistence (Part F): `get_module_setting` /
//! `set_module_setting` proxy any control's current value through
//! the existing `module_settings` table (already JSON-blob KV). The
//! schema-rendered tab uses these as its default backing store; modules
//! declaring `on_change` Tauri commands receive change notifications
//! ON TOP of the generic persistence (not as a replacement).
//!
//! Soft-fail philosophy: a broken or unreadable manifest must NOT break
//! the sidebar for other modules. We log + skip per-module.
//!
//! Storage note (v0.2.97): migration 034 made `module_settings.project_id`
//! nullable, and a manifest `settings` entry may declare `"scope": "global"`
//! (one machine-wide value, e.g. `vct-hub-api`'s `VCT_HUB_PORT`). The two
//! commands below route by the DECLARATION
//! (`vct_launcher_core::module_settings_schema`): a declared global setting is
//! read/written in the project-less row and refuses a project on write; a
//! declared per-project setting, and any key no manifest declares (a
//! `gui.config_tab` control's state), needs a project. Every write of a
//! declared setting is validated against the manifest's type / min / max /
//! options / validation — the UI checks too, but this is the gate.

use serde::Serialize;
use std::path::PathBuf;
use tauri::{command, State};

use crate::db::Db;
use crate::manifest::{ConfigTab, ModuleManifest};
use vct_launcher_core::module_setting_bindings::{LiveSource, SettingBinding, BUNDLED_SETTING_BINDINGS};
use vct_launcher_core::module_settings_schema::{self, DeclOrigin, FoundSetting};

// ─── Wire types ─────────────────────────────────────────────────────────

/// One entry in the sidebar's module-contributed nav group. Carries the
/// full `ConfigTab` schema so the renderer can stay route-driven (no
/// per-route fetch needed once the sidebar loads).
#[derive(Debug, Clone, Serialize)]
pub struct ModuleNavItem {
    pub module_id: String,
    pub title: String,
    pub icon: Option<String>,
    /// Resolved route slug. Defaults to `"/modules/<module_id>/config"`
    /// when the manifest doesn't override `config_tab.route`.
    pub route: String,
    pub config_tab: ConfigTab,
}

// ─── Manifest discovery ─────────────────────────────────────────────────
//
// v0.2.33 (Agent B): manifest enumeration moved to the shared
// `commands::installed_modules` helper. This file previously contained
// a copy-pasted scan duplicating `modules.rs::catalog_scan_paths`.
//
// We compose two layers:
//   1. `installed_module_manifest_paths(db)` — post-install + bundled
//      (the shared helper).
//   2. dev-affordance: `<install_root>/paid-modules/<id>/vct-module.json`
//      (gated behind `VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH=1`).
//   3. `<install_root>/vct-module.json` — orchestrator-core's own
//      manifest, which ships `gui.config_tab` for the core dashboard
//      surface (KG / code-graph controls). This is NOT a paid module;
//      it's the launcher's own config-tab feed.

/// v0.2.23.1 refactor (2026-05-21): now takes `&Db` so the orchestrator
/// clone root can be resolved via `app_state['launcher.install_path']`
/// (sticky DB cache) instead of a baked-in heuristic. See
/// `installer::resolve_install_root_sync` for the rationale — short
/// version: `env!("CARGO_MANIFEST_DIR")` leaks the build-host path and
/// is wrong on shipped binaries.
///
/// v0.2.33: composes the shared `installed_module_manifest_paths` +
/// gated dev-affordance + orchestrator-root manifest. The 30+ lines
/// of fs::read_dir gymnastics that used to live here are gone.
fn manifest_scan_paths(db: &Db) -> Vec<PathBuf> {
    let mut paths = crate::commands::installed_modules::installed_module_manifest_paths(db);
    paths.extend(crate::commands::installed_modules::dev_paid_modules_paths(db));

    // Orchestrator-core's own config-tab feed. Lives at
    // `<install_root>/vct-module.json`. NOT a paid module — this is
    // the launcher's own dashboard surface (KG / code-graph controls).
    // No env-var gate because it's always relevant (it IS the launcher).
    if let Some(clone) = crate::commands::installer::resolve_install_root_sync(db) {
        let root_manifest = clone.join("vct-module.json");
        if root_manifest.is_file() {
            paths.push(root_manifest);
        }
    }
    paths
}

/// Resolve `config_tab.route` with the default rule documented in
/// `ConfigTab::route`'s rustdoc. Returns a route slug starting with `/`.
fn resolve_route(module_id: &str, route: Option<&str>) -> String {
    match route {
        Some(r) if r.starts_with('/') => r.to_string(),
        _ => format!("/modules/{}/config", module_id),
    }
}

// ─── Commands ──────────────────────────────────────────────────────────

/// Returns one `ModuleNavItem` per discovered manifest that declares a
/// `gui.config_tab`. Sorted by `module_id` for stable ordering. Per-
/// manifest failures (unreadable, malformed) are logged + skipped so a
/// single bad file can't break the sidebar for every other module.
#[command]
pub async fn get_module_nav_items(
    db: State<'_, Db>,
) -> Result<Vec<ModuleNavItem>, String> {
    let mut items: Vec<ModuleNavItem> = Vec::new();
    let mut seen_ids: std::collections::HashSet<String> = std::collections::HashSet::new();

    for path in manifest_scan_paths(&db) {
        let raw = match std::fs::read_to_string(&path) {
            Ok(s) => s,
            Err(e) => {
                tracing::warn!("[module_gui] skip {} (read error): {}", path.display(), e);
                continue;
            }
        };
        let manifest: ModuleManifest = match ModuleManifest::from_json(&raw) {
            Ok(m) => m,
            Err(e) => {
                tracing::warn!("[module_gui] skip {} (parse error): {}", path.display(), e);
                continue;
            }
        };
        if !seen_ids.insert(manifest.id.clone()) {
            // Duplicate id (e.g. same module found via both bundled
            // and paid-modules paths). Keep the first occurrence.
            continue;
        }
        let Some(gui) = manifest.gui else { continue };
        let Some(config_tab) = gui.config_tab else { continue };

        let route = resolve_route(&manifest.id, config_tab.route.as_deref());
        items.push(ModuleNavItem {
            module_id: manifest.id.clone(),
            title: config_tab.title.clone(),
            icon: config_tab.icon.clone(),
            route,
            config_tab,
        });
    }

    items.sort_by(|a, b| a.module_id.cmp(&b.module_id));
    Ok(items)
}

// ─── Generic per-control state (Part F) ─────────────────────────────────

/// The manifest declaration of `module_id`'s `key`, if any manifest the
/// launcher knows declares it: the BUNDLED manifests embedded in this binary
/// first (authoritative for the core modules), then the installed / catalog
/// manifests on disk ([`manifest_scan_paths`]). `None` = an undeclared key
/// (a `gui.config_tab` control's state).
fn declared_setting(db: &Db, module_id: &str, key: &str) -> Option<FoundSetting> {
    let bundled = module_settings_schema::bundled_module_settings();
    if let Some(d) = module_settings_schema::find_setting(&bundled, module_id, key) {
        return Some(FoundSetting { decl: d.clone(), origin: DeclOrigin::Bundled });
    }
    for path in manifest_scan_paths(db) {
        let Ok(raw) = std::fs::read_to_string(&path) else { continue };
        let Ok(manifest) = ModuleManifest::from_json(&raw) else { continue };
        if manifest.id != module_id {
            continue;
        }
        if let Some(d) = manifest.settings.into_iter().find(|s| s.key == key) {
            return Some(FoundSetting { decl: d, origin: DeclOrigin::Installed });
        }
    }
    None
}

/// The parsed manifests of the INSTALLED (catalog) modules — the
/// post-install copies under `<vct root>/modules/` (the bundled directory is
/// skipped: the embedded bundled manifests are listed separately). A file
/// that does not parse is skipped with a warning.
fn installed_manifests(db: &Db) -> Vec<ModuleManifest> {
    let bundled_dir = crate::paths::vct_root_dir().join("bundled_manifests");
    crate::commands::installed_modules::installed_module_manifest_paths(db)
        .into_iter()
        .filter(|p| !p.starts_with(&bundled_dir))
        .filter_map(|p| {
            let raw = std::fs::read_to_string(&p).ok()?;
            ModuleManifest::from_json(&raw)
                .map_err(|e| tracing::warn!("[module_gui] skip {} (parse error): {}", p.display(), e))
                .ok()
        })
        .collect()
}

/// Read a single setting value from the `module_settings` table. Returns
/// `Value::Null` when the row doesn't exist (matches the wire contract
/// the schema renderer expects: "no row" == "use the control's default").
///
/// `project_id`: required for a per-project or undeclared key; ignored for a
/// declared machine-wide (`scope: "global"`) setting, which always reads the
/// project-less row — the value in effect.
#[command]
pub async fn get_module_setting(
    module_id: String,
    control_id: String,
    project_id: Option<String>,
    db: State<'_, Db>,
) -> Result<serde_json::Value, String> {
    read_setting(&db, &module_id, &control_id, project_id.as_deref())
}

/// Write a control's (or a declared setting's) current value. Stored as a
/// JSON blob in `module_settings.setting_value`.
///
/// The schema-rendered tab calls this on every control change
/// regardless of whether the manifest declared an `on_change` Tauri
/// command — the generic persistence is the source of truth for "what
/// did the user pick"; module-specific `on_change` hooks are the
/// SIDE-EFFECT path (containers, files, services).
///
/// v0.2.97: when a manifest DECLARES `control_id` in its `settings`, the
/// value is validated against that declaration and routed by its scope
/// (machine-wide → the project-less row, and `project_id` must be absent;
/// per-project → the project's row). A refused value writes nothing.
#[command]
pub async fn set_module_setting(
    module_id: String,
    control_id: String,
    value: serde_json::Value,
    project_id: Option<String>,
    db: State<'_, Db>,
) -> Result<(), String> {
    write_setting(&db, &module_id, &control_id, project_id.as_deref(), &value)
}

/// Every module whose manifest `settings` the Preferences → Modules page's
/// "Module settings" editor lists: the BUNDLED core modules (for every
/// project), then each INSTALLED catalog module that declares settings (with
/// the projects it is installed + enabled in). Each setting carries its
/// binding (`module_setting_bindings`): stored → editable through
/// [`get_module_setting`] / [`set_module_setting`]; elsewhere → shown
/// read-only with [`module_setting_live_values`].
#[command]
pub async fn list_module_settings(
    db: State<'_, Db>,
) -> Result<Vec<module_settings_schema::ListedModuleSettings>, String> {
    module_settings_schema::list_module_settings(&db, &installed_manifests(&db))
}

/// The live value of one setting whose home is not `module_settings`.
#[derive(Debug, Clone, Serialize)]
pub struct LiveSettingValue {
    pub module_id: String,
    pub key: String,
    /// `None` when it cannot be resolved (see `note`).
    pub value: Option<String>,
    /// Why the value is missing, or where a default came from.
    pub note: Option<String>,
}

/// The LIVE values of the bundled settings bound elsewhere
/// ([`SettingBinding::Elsewhere`]), resolved from their canonical homes —
/// the panel shows them read-only. `project_id` is needed only for the
/// per-project KG collection.
#[command]
pub async fn module_setting_live_values(
    project_id: Option<String>,
    db: State<'_, Db>,
) -> Result<Vec<LiveSettingValue>, String> {
    Ok(live_values(&db, project_id.as_deref()))
}

/// The body of [`module_setting_live_values`].
fn live_values(db: &Db, project_id: Option<&str>) -> Vec<LiveSettingValue> {
    BUNDLED_SETTING_BINDINGS
        .iter()
        .filter_map(|(module_id, key, binding)| match binding {
            SettingBinding::Elsewhere { live, .. } => {
                let (value, note) = resolve_live(db, *live, project_id);
                Some(LiveSettingValue { module_id: (*module_id).into(), key: (*key).into(), value, note })
            }
            SettingBinding::Stored { .. } => None,
        })
        .collect()
}

/// Resolve one [`LiveSource`] through the SAME function its real reader
/// uses (no re-derivation here).
fn resolve_live(db: &Db, live: LiveSource, project_id: Option<&str>) -> (Option<String>, Option<String>) {
    match live {
        LiveSource::ProjectKgCollection => {
            let Some(pid) = project_id.filter(|p| !p.is_empty()) else {
                return (None, Some("Pick a project to see its KG collection.".into()));
            };
            match db.get_project(pid) {
                Ok(Some(row)) => {
                    let c = crate::collection_naming::resolve_project_collections(
                        db,
                        Some(pid),
                        &row.name,
                        Some(&row.slug),
                    );
                    (Some(c.kg), None)
                }
                Ok(None) => (None, Some(format!("No project {pid}."))),
                Err(e) => (None, Some(format!("Could not read the project: {e}"))),
            }
        }
        LiveSource::SharedKgCollection => {
            match crate::commands::project_state_populate::shared_kg_binding::resolve_shared_kg_collection(db) {
                Some(name) => (Some(name), None),
                None => (None, Some("No shared KG collection is set on this computer.".into())),
            }
        }
        LiveSource::WeaviateUrl => (
            Some(vct_launcher_core::services::service_endpoints::machine_weaviate_url(db)),
            None,
        ),
        LiveSource::CodeEmbedPort => (
            Some(crate::commands::project_env_settings::resolve_code_embed_port(db).to_string()),
            None,
        ),
        LiveSource::CodeEmbedBackend => code_embed_backend(db),
        LiveSource::CodeEmbedDevice => {
            (Some("auto".into()), Some("Fixed by the service's compose file.".into()))
        }
    }
}

/// `CODE_EMBED_BACKEND` as docker-compose gives it to the code-embedding
/// container: the orchestrator's `infrastructure/.env` (written by the
/// installer, `vco_lib.compose_env`), else the compose default `gpu`.
fn code_embed_backend(db: &Db) -> (Option<String>, Option<String>) {
    let Some(root) = crate::commands::installer::resolve_install_root_sync(db) else {
        return (None, Some("The orchestrator folder could not be located.".into()));
    };
    code_embed_backend_from(&root.join("infrastructure").join(".env"))
}

fn code_embed_backend_from(env_file: &std::path::Path) -> (Option<String>, Option<String>) {
    match crate::commands::claude_env::read_key(env_file, "CODE_EMBED_BACKEND") {
        Ok(Some(v)) if !v.trim().is_empty() => (Some(v.trim().trim_matches('"').to_string()), None),
        Ok(_) => (
            Some("gpu".into()),
            Some("Not set in infrastructure/.env — the compose default applies.".into()),
        ),
        Err(e) => (None, Some(format!("Could not read infrastructure/.env: {e}"))),
    }
}

/// The body of [`get_module_setting`] (no Tauri `State`, so tests call it).
fn read_setting(
    db: &Db,
    module_id: &str,
    key: &str,
    project_id: Option<&str>,
) -> Result<serde_json::Value, String> {
    let found = declared_setting(db, module_id, key);
    module_settings_schema::read_module_setting(db, found.as_ref(), module_id, key, project_id)
        .map(|v| v.unwrap_or(serde_json::Value::Null))
        .map_err(|e| format!("get_module_setting: {e}"))
}

/// The body of [`set_module_setting`] (no Tauri `State`, so tests call it).
fn write_setting(
    db: &Db,
    module_id: &str,
    key: &str,
    project_id: Option<&str>,
    value: &serde_json::Value,
) -> Result<(), String> {
    let found = declared_setting(db, module_id, key);
    module_settings_schema::write_module_setting(db, found.as_ref(), module_id, key, project_id, value)
        .map_err(|e| format!("set_module_setting: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Db;

    fn open_db_with_project() -> (Db, String) {
        let db = Db::open_in_memory().expect("in-memory db");
        let project_id = uuid::Uuid::new_v4().to_string();
        db.insert_project(
            &project_id,
            "Test Project",
            "/tmp/test",
            crate::db::models::ProjectHost::Base,
            "test-project",
        )
        .expect("insert project");
        (db, project_id)
    }

    /// `resolve_route` falls back to `/modules/<id>/config` when the
    /// manifest doesn't override, and accepts `/`-prefixed overrides.
    /// Non-rooted overrides (a typo like `"modules/foo"`) fall back to
    /// the default to avoid producing relative routes that the Svelte
    /// router would treat as appended to the current path.
    #[test]
    fn resolve_route_uses_default_when_unset() {
        assert_eq!(
            resolve_route("vct-rl-reranker", None),
            "/modules/vct-rl-reranker/config"
        );
    }

    #[test]
    fn resolve_route_accepts_rooted_override() {
        assert_eq!(
            resolve_route("foo", Some("/custom/path")),
            "/custom/path"
        );
    }

    #[test]
    fn resolve_route_rejects_non_rooted_and_falls_back() {
        assert_eq!(
            resolve_route("foo", Some("custom/path")),
            "/modules/foo/config",
            "non-rooted route must fall back to the default to avoid \
             relative-path bugs in the Svelte router"
        );
    }

    /// get_module_setting returns Value::Null when no row exists,
    /// avoiding the Option<Value> wire shape (TS would have to handle
    /// `undefined` separately). The schema renderer treats null as
    /// "use the control's declared default".
    #[test]
    fn get_module_setting_returns_null_for_missing_row() {
        let (db, project_id) = open_db_with_project();
        // Direct DB read path: simulate the command's body without
        // Tauri's State wrapping.
        let result = db
            .get_setting(&project_id, "test-mod", "missing-control")
            .expect("query");
        assert!(result.is_none(), "DB layer returns None");

        // Command body: when None at DB level, command returns Value::Null
        let body_result = match db
            .get_setting(&project_id, "test-mod", "missing-control")
            .unwrap()
        {
            Some(v) => v,
            None => serde_json::Value::Null,
        };
        assert!(body_result.is_null());
    }

    /// v0.2.97: the COMMAND bodies route a bundled module's declared setting
    /// by its manifest scope and validate it. The hub port (machine-wide)
    /// lands in the project-less row the hub reads at start; an out-of-range
    /// port, or one sent with a project, is refused and writes nothing.
    #[test]
    fn set_module_setting_validates_and_routes_the_machine_wide_hub_port() {
        let (db, project_id) = open_db_with_project();
        let port = serde_json::json!(8802);
        write_setting(&db, "vct-hub-api", "VCT_HUB_PORT", None, &port).expect("valid global write");
        assert_eq!(db.get_global_setting("vct-hub-api", "VCT_HUB_PORT").unwrap(), Some(port.clone()));
        assert_eq!(
            read_setting(&db, "vct-hub-api", "VCT_HUB_PORT", Some(&project_id)).unwrap(),
            port,
            "any project reads the machine-wide value"
        );

        let err = write_setting(&db, "vct-hub-api", "VCT_HUB_PORT", None, &serde_json::json!(80)).unwrap_err();
        assert!(err.contains("at least 1024"), "{err}");
        let err = write_setting(&db, "vct-hub-api", "VCT_HUB_PORT", None, &serde_json::json!("8803")).unwrap_err();
        assert!(err.contains("whole number"), "{err}");
        let err = write_setting(&db, "vct-hub-api", "VCT_HUB_PORT", Some(&project_id), &serde_json::json!(8804))
            .unwrap_err();
        assert!(err.contains("machine-wide"), "{err}");
        assert_eq!(db.get_global_setting("vct-hub-api", "VCT_HUB_PORT").unwrap(), Some(port), "unchanged");
        assert_eq!(db.get_setting(&project_id, "vct-hub-api", "VCT_HUB_PORT").unwrap(), None);
    }

    /// Round 2: a bundled setting whose live value lives elsewhere (here
    /// vct-kg's KG_COLLECTION — the project's KG binding) is refused by the
    /// command: no second stored copy.
    #[test]
    fn set_module_setting_refuses_a_setting_that_lives_elsewhere() {
        let (db, project_id) = open_db_with_project();
        let err = write_setting(&db, "vct-kg", "KG_COLLECTION", Some(&project_id), &serde_json::json!("X_KnowledgeGraph"))
            .unwrap_err();
        assert!(err.contains("not stored in module settings"), "{err}");
        assert_eq!(db.get_setting(&project_id, "vct-kg", "KG_COLLECTION").unwrap(), None);
        let err = write_setting(&db, "vct-code-embedding", "CODE_EMBED_BACKEND", None, &serde_json::json!("ollama"))
            .unwrap_err();
        assert!(err.contains("infrastructure/.env"), "{err}");
    }

    /// The live values come from the real homes: the project's KG binding
    /// rule, the code-embed port resolver, the fixed device — one entry per
    /// setting bound elsewhere, and none for a stored one.
    #[test]
    fn live_values_resolve_from_the_canonical_homes() {
        let (db, project_id) = open_db_with_project();
        let values = live_values(&db, Some(&project_id));
        let keys: Vec<String> = values.iter().map(|v| format!("{}/{}", v.module_id, v.key)).collect();
        let expected_elsewhere: Vec<String> = BUNDLED_SETTING_BINDINGS
            .iter()
            .filter(|(_, _, b)| !b.is_stored())
            .map(|(m, k, _)| format!("{m}/{k}"))
            .collect();
        assert_eq!(keys, expected_elsewhere);
        assert!(!keys.iter().any(|k| k.ends_with("VCT_HUB_PORT")), "stored settings are edited, not shown live");

        let get = |m: &str, k: &str| values.iter().find(|v| v.module_id == m && v.key == k).unwrap().clone();
        let row = db.get_project(&project_id).unwrap().unwrap();
        let expected_kg =
            crate::collection_naming::resolve_project_collections(&db, Some(&project_id), &row.name, Some(&row.slug)).kg;
        assert_eq!(get("vct-kg", "KG_COLLECTION").value, Some(expected_kg));
        assert_eq!(
            get("vct-code-embedding", "CODE_EMBED_PORT").value,
            Some(crate::commands::project_env_settings::resolve_code_embed_port(&db).to_string())
        );
        assert_eq!(get("vct-code-embedding", "CODE_EMBED_DEVICE").value.as_deref(), Some("auto"));
        // Without a project the per-project KG collection is not guessed.
        let no_project = live_values(&db, None);
        let kg = no_project.iter().find(|v| v.key == "KG_COLLECTION").unwrap();
        assert!(kg.value.is_none() && kg.note.is_some());
    }

    /// CODE_EMBED_BACKEND is read from the file compose reads.
    #[test]
    fn code_embed_backend_reads_the_infrastructure_env_file() {
        let dir = tempfile::tempdir().unwrap();
        let f = dir.path().join(".env");
        assert_eq!(code_embed_backend_from(&f).0.as_deref(), Some("gpu"), "absent → compose default");
        std::fs::write(&f, "# managed\nCODE_EMBED_BACKEND=ollama\n").unwrap();
        assert_eq!(code_embed_backend_from(&f), (Some("ollama".to_string()), None));
    }

    /// A per-project bundled setting needs a project, is validated, and
    /// lands in that project's row (the one the hub's `/env` serves).
    #[test]
    fn set_module_setting_validates_a_per_project_bundled_setting() {
        let (db, project_id) = open_db_with_project();
        write_setting(&db, "vct-session-state", "MEMORY_MAX_LINES", Some(&project_id), &serde_json::json!(150))
            .expect("valid");
        assert_eq!(
            db.get_setting(&project_id, "vct-session-state", "MEMORY_MAX_LINES").unwrap(),
            Some(serde_json::json!(150))
        );
        assert!(write_setting(&db, "vct-session-state", "MEMORY_MAX_LINES", Some(&project_id), &serde_json::json!(5000))
            .unwrap_err()
            .contains("at most 2000"));
        assert!(write_setting(&db, "vct-session-state", "MEMORY_MAX_LINES", None, &serde_json::json!(150))
            .unwrap_err()
            .contains("project is required"));
        let err = write_setting(&db, "vct-session-state", "MEMORY_MAX_LINES", Some(&project_id), &serde_json::json!("150"))
            .unwrap_err();
        assert!(err.contains("whole number"), "{err}");
        assert_eq!(
            db.get_setting(&project_id, "vct-session-state", "MEMORY_MAX_LINES").unwrap(),
            Some(serde_json::json!(150)),
            "a refused write left the stored value alone"
        );
    }

    /// An undeclared key (a config_tab control's state) keeps the old
    /// contract: any JSON, per project, and a project is still required.
    #[test]
    fn set_module_setting_leaves_undeclared_config_tab_state_unvalidated() {
        let (db, project_id) = open_db_with_project();
        let v = serde_json::json!({ "selected": ["a"] });
        write_setting(&db, "vct-rl-reranker", "global_train_projects", Some(&project_id), &v).expect("stored");
        assert_eq!(read_setting(&db, "vct-rl-reranker", "global_train_projects", Some(&project_id)).unwrap(), v);
        assert!(write_setting(&db, "vct-rl-reranker", "global_train_projects", None, &v).is_err());
        assert!(read_setting(&db, "vct-rl-reranker", "global_train_projects", Some("")).is_err());
    }

    /// set_module_setting + get_module_setting round-trip via the
    /// existing `module_settings` table. Confirms a control's value
    /// persists across calls (no in-memory caching).
    #[test]
    fn set_then_get_module_setting_round_trips() {
        let (db, project_id) = open_db_with_project();
        let val = serde_json::json!({ "selected": ["a", "b", "c"], "enabled": true });

        db.set_setting(&project_id, "vct-rl-reranker", "global_train_projects", &val)
            .expect("set");

        let got = db
            .get_setting(&project_id, "vct-rl-reranker", "global_train_projects")
            .expect("get");
        assert_eq!(got, Some(val));
    }

    /// v0.2.23.1 regression (2026-05-21): pin the contract that the
    /// two manifest-scanning helpers (`manifest_scan_paths` in this
    /// file + `catalog_scan_paths` in `modules.rs`) resolve the
    /// orchestrator clone root via `installer::resolve_install_root_sync`
    /// — NOT via `env!("CARGO_MANIFEST_DIR")`. That macro embeds the
    /// build-host's absolute path as a static string in the binary
    /// (PRIVACY LEAK, `--remap-path-prefix` does NOT rewrite it) AND is
    /// wrong on shipped binaries (build-time path != runtime path).
    /// The canonical resolver reads `app_state['launcher.install_path']`
    /// (sticky cache written at first install) with a `current_exe()`
    /// walk-up fall-through. See installer.rs:31-49 + self_update.rs:275-288
    /// for the 2026-05-06 privacy notes that established this
    /// discipline.
    ///
    /// Source-level positive-contract check: both helpers MUST reference
    /// `resolve_install_root_sync` AND MUST NOT contain a bare
    /// `env!("CARGO_MANIFEST_DIR")` outside doc comments.
    #[test]
    fn production_code_does_not_use_cargo_manifest_dir_for_path_resolution() {
        // Two surfaces under audit. Each must:
        //   (a) Contain the canonical helper-call expression, AND
        //   (b) NOT contain a bare CARGO_MANIFEST_DIR use in code
        //       (doc comments at `///` are fine — they document the
        //       privacy rationale).
        struct ProductionSite {
            file: &'static str,
            fn_name: &'static str,
        }
        // v0.2.33 (Agent B): `catalog_scan_paths` in modules.rs was
        // removed and the duplicated `manifest_scan_paths` in this file
        // was shrunk to call the shared helper in `installed_modules`.
        // The canonical production site now lives in
        // `commands::installed_modules::dev_paid_modules_paths` — pin
        // it here so the privacy discipline (no CARGO_MANIFEST_DIR
        // baked into shipped binaries) survives the refactor.
        let sites = [
            ProductionSite {
                file: "src/commands/installed_modules.rs",
                fn_name: "dev_paid_modules_paths",
            },
        ];
        let repo_root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        // NOTE: the env!("CARGO_MANIFEST_DIR") use right above is the
        // only such use in this test file. It's compile-time-only path
        // resolution to FIND the source files under audit. It does
        // not bake into a production code path.

        let mut violations: Vec<String> = Vec::new();
        for site in &sites {
            let path = repo_root.join(site.file);
            let body = std::fs::read_to_string(&path)
                .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));

            // Find the function body: from `fn <name>(...)` to the
            // matching `}` of the immediately-following opening `{`.
            let fn_marker = format!("fn {}(", site.fn_name);
            let fn_pos = match body.find(&fn_marker) {
                Some(p) => p,
                None => {
                    violations.push(format!(
                        "{}: {} not found — the regression test is stale, update it.",
                        site.file, site.fn_name,
                    ));
                    continue;
                }
            };
            // Find the opening `{` of the body.
            let body_open = match body[fn_pos..].find('{') {
                Some(off) => fn_pos + off,
                None => {
                    violations.push(format!(
                        "{}: {} has no body opening brace.",
                        site.file, site.fn_name
                    ));
                    continue;
                }
            };
            // Balanced-brace scan to find the matching `}`. Skips
            // strings + comments to handle Rust source faithfully.
            let bytes = body.as_bytes();
            let mut depth = 1i32;
            let mut i = body_open + 1;
            let mut in_string = false;
            let mut in_line_comment = false;
            let mut in_block_comment = false;
            let mut escape = false;
            while i < bytes.len() && depth > 0 {
                let c = bytes[i];
                if in_line_comment {
                    if c == b'\n' {
                        in_line_comment = false;
                    }
                } else if in_block_comment {
                    if c == b'*' && i + 1 < bytes.len() && bytes[i + 1] == b'/' {
                        in_block_comment = false;
                        i += 1;
                    }
                } else if in_string {
                    if escape {
                        escape = false;
                    } else if c == b'\\' {
                        escape = true;
                    } else if c == b'"' {
                        in_string = false;
                    }
                } else if c == b'/' && i + 1 < bytes.len() && bytes[i + 1] == b'/' {
                    in_line_comment = true;
                    i += 1;
                } else if c == b'/' && i + 1 < bytes.len() && bytes[i + 1] == b'*' {
                    in_block_comment = true;
                    i += 1;
                } else if c == b'"' {
                    in_string = true;
                } else if c == b'{' {
                    depth += 1;
                } else if c == b'}' {
                    depth -= 1;
                }
                i += 1;
            }
            let body_end = i;
            let fn_body = &body[body_open..body_end];

            // (a) Canonical call must appear.
            if !fn_body.contains("resolve_install_root_sync") {
                violations.push(format!(
                    "{}::{}: body does not reference \
                     `installer::resolve_install_root_sync` — the canonical \
                     install-root resolver was bypassed.",
                    site.file, site.fn_name,
                ));
            }
            // (b) No CARGO_MANIFEST_DIR in the function body.
            // Filter out `///` doc comments that may be ABOVE the
            // function (we scanned only the body, so this is just a
            // belt-and-braces check on `//` line comments inside it).
            for (idx, line) in fn_body.lines().enumerate() {
                let trimmed = line.trim();
                if trimmed.starts_with("//") {
                    continue;
                }
                if line.contains("env!(\"CARGO_MANIFEST_DIR\")")
                    || line.contains("option_env!(\"CARGO_MANIFEST_DIR\")")
                {
                    violations.push(format!(
                        "{}::{} body line {}: uses CARGO_MANIFEST_DIR — \
                         use installer::resolve_install_root_sync(db) instead. \
                         Line: {}",
                        site.file,
                        site.fn_name,
                        idx + 1,
                        trimmed,
                    ));
                }
            }
        }

        assert!(
            violations.is_empty(),
            "v0.2.23.1 regression — install-root resolution discipline \
             violated:\n{}",
            violations.join("\n")
        );
    }
}
