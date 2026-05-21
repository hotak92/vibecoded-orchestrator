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
//! Storage note: `module_settings.project_id` is `NOT NULL REFERENCES
//! projects(id)` — there is no "module-global" row possible at the
//! DB level today. We therefore require a project_id for every
//! get/set call; module-global state would need a follow-up
//! migration (out of scope for Stream 2).

use serde::Serialize;
use std::path::PathBuf;
use tauri::{command, State};

use crate::db::Db;
use crate::manifest::{ConfigTab, ModuleManifest};

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
// We deliberately reuse the same scan paths as `commands::modules`
// (`<VCT_ROOT>/modules/*/vct-module.json`,
// `<VCT_ROOT>/bundled_manifests/*.json`, and the dev-only
// `<orchestrator_clone>/paid-modules/*/vct-module.json`). Copy-pasted
// here as a private helper rather than exporting from `modules.rs` to
// keep that module's surface small. If a third caller needs scanning,
// promote `catalog_scan_paths` to a shared util.

/// v0.2.23.1 refactor (2026-05-21): now takes `&Db` so the orchestrator
/// clone root can be resolved via `app_state['launcher.install_path']`
/// (sticky DB cache) instead of a baked-in heuristic. See
/// `installer::resolve_install_root_sync` for the rationale — short
/// version: `env!("CARGO_MANIFEST_DIR")` leaks the build-host path and
/// is wrong on shipped binaries.
fn manifest_scan_paths(db: &Db) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    let vct_root = crate::paths::vct_root_dir();
    {
        let modules = vct_root.join("modules");
        if modules.is_dir() {
            if let Ok(entries) = std::fs::read_dir(&modules) {
                for e in entries.flatten() {
                    let p = e.path().join("vct-module.json");
                    if p.is_file() {
                        paths.push(p);
                    }
                }
            }
        }
        let bundled = vct_root.join("bundled_manifests");
        if bundled.is_dir() {
            if let Ok(entries) = std::fs::read_dir(&bundled) {
                for e in entries.flatten() {
                    let p = e.path();
                    if p.extension().and_then(|s| s.to_str()) == Some("json") {
                        paths.push(p);
                    }
                }
            }
        }
    }

    // Resolve the orchestrator clone root via the shared sync helper.
    // No hardcoded paths in this binary — resolution is DB-cached
    // (`launcher.install_path` app_state key, written at first install
    // and validated on every read) with a `current_exe()` walk-up as
    // the fall-through. See `installer::resolve_install_root_sync` for
    // the full rationale incl. the CARGO_MANIFEST_DIR privacy leak we
    // deliberately removed. v0.2.23.1 fix (2026-05-21).
    let orchestrator_clone = crate::commands::installer::resolve_install_root_sync(db);
    if let Some(clone) = orchestrator_clone {
        let paid = clone.join("paid-modules");
        if paid.is_dir() {
            if let Ok(entries) = std::fs::read_dir(&paid) {
                for module_dir in entries.flatten() {
                    let p = module_dir.path().join("vct-module.json");
                    if p.is_file() {
                        paths.push(p);
                    }
                }
            }
        }
        // Stream 2 follow-up (v0.2.20, 2026-05-19): the orchestrator core
        // ships its own `gui.config_tab` in the repo-root manifest. The
        // launcher binary is shipped INSIDE this clone, so the manifest
        // lives at `<clone>/vct-module.json`. Discovery is the same
        // walk-up that `commands::modules::find_orchestrator_manifest`
        // does, but cached here as a single PathBuf push rather than a
        // current_exe walk — `orchestrator_clone` was just resolved above.
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
                eprintln!("[module_gui] skip {} (read error): {}", path.display(), e);
                continue;
            }
        };
        let manifest: ModuleManifest = match ModuleManifest::from_json(&raw) {
            Ok(m) => m,
            Err(e) => {
                eprintln!("[module_gui] skip {} (parse error): {}", path.display(), e);
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

/// Read a single setting value from the `module_settings` table. Returns
/// `Value::Null` when the row doesn't exist (matches the wire contract
/// the schema renderer expects: "no row" == "use the control's default").
///
/// `project_id` is required because `module_settings` has a non-null
/// FK to `projects`. Module-global state needs a follow-up migration.
#[command]
pub async fn get_module_setting(
    module_id: String,
    control_id: String,
    project_id: String,
    db: State<'_, Db>,
) -> Result<serde_json::Value, String> {
    if project_id.is_empty() {
        return Err(
            "get_module_setting: project_id required (module_settings table has \
             a non-null FK to projects). Module-global state not yet supported."
                .into(),
        );
    }
    match db.get_setting(&project_id, &module_id, &control_id)? {
        Some(v) => Ok(v),
        None => Ok(serde_json::Value::Null),
    }
}

/// Write a control's current value. Stored as JSON blob in
/// `module_settings.setting_value`. Upsert via the existing
/// `Db::set_setting` helper.
///
/// The schema-rendered tab calls this on every control change
/// regardless of whether the manifest declared an `on_change` Tauri
/// command — the generic persistence is the source of truth for "what
/// did the user pick"; module-specific `on_change` hooks are the
/// SIDE-EFFECT path (containers, files, services).
#[command]
pub async fn set_module_setting(
    module_id: String,
    control_id: String,
    value: serde_json::Value,
    project_id: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    if project_id.is_empty() {
        return Err(
            "set_module_setting: project_id required (module_settings table has \
             a non-null FK to projects). Module-global state not yet supported."
                .into(),
        );
    }
    db.set_setting(&project_id, &module_id, &control_id, &value)?;
    Ok(())
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
        let sites = [
            ProductionSite {
                file: "src/commands/module_gui.rs",
                fn_name: "manifest_scan_paths",
            },
            ProductionSite {
                file: "src/commands/modules.rs",
                fn_name: "catalog_scan_paths",
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
