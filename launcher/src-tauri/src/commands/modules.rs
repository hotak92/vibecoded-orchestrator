//! Module installation + lifecycle commands.
//!
//! Orchestrates: catalog lookup, license gating, manifest parse, installer
//! engine invocation, DB row writes, event emission.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use tauri::{command, AppHandle, Emitter, State};
use uuid::Uuid;

use crate::db::models::{ModuleInstallRow, ModuleStatus, ProjectHost};
use crate::db::Db;
use crate::installer_engine;
use crate::manifest::{ModuleManifest, PlaceholderCtx};

// ─── Catalog entry surface ──────────────────────────────────────────────

#[derive(Debug, Clone, Serialize)]
pub struct ModuleCatalogEntry {
    pub id: String,
    pub name: String,
    pub version: String,
    pub description: String,
    pub category: String,
    pub tags: Vec<String>,
    pub license_required: bool,
    pub license_variant_ids: Vec<String>,
    pub min_orchestrator_tier: String,
    pub compatibility_hosts: Vec<String>,
    pub is_licensed: bool,
    pub manifest_source: String, // path or URL the manifest was loaded from
    /// Bug 16: how the launcher should render this entry.
    ///   - "bundled"      = bundled with the launcher itself, always installed,
    ///                      cannot be uninstalled (e.g. the launcher).
    ///   - "available"    = catalog-listed, not installed yet, has Install action.
    ///   - "installed"    = installed, can be reconfigured / uninstalled.
    ///   - "subcomponent" = ships with a parent module, no separate install,
    ///                      offers a Dashboard CTA.
    pub kind: String,
    /// For subcomponents: which parent module they ship with. Empty otherwise.
    #[serde(default)]
    pub parent_id: String,
    /// Optional dashboard route for subcomponents (e.g. "/kg", "/codegraph").
    #[serde(default)]
    pub cta_route: String,
}

impl ModuleCatalogEntry {
    fn from_manifest(m: &ModuleManifest, is_licensed: bool, source: String) -> Self {
        Self {
            id: m.id.clone(),
            name: m.name.clone(),
            version: m.version.clone(),
            description: m.description.clone(),
            category: format!("{:?}", m.category).to_lowercase(),
            tags: m.tags.clone(),
            license_required: m.license.required,
            license_variant_ids: m.license.variant_ids.clone(),
            min_orchestrator_tier: m.license.min_orchestrator_tier.clone(),
            compatibility_hosts: m.compatibility.hosts.clone(),
            is_licensed,
            manifest_source: source,
            kind: "available".into(),
            parent_id: String::new(),
            cta_route: String::new(),
        }
    }
}

/// Bug 16: minimal manifest the launcher reads from `vct-module.json` at the
/// repo root. The full ModuleManifest schema is for installable modules; the
/// orchestrator core is not installable as a module — it IS the orchestrator —
/// so it ships its own slim shape.
#[derive(Debug, Deserialize)]
struct OrchestratorManifest {
    #[serde(default = "default_orch_id")]
    id: String,
    #[serde(default = "default_orch_name")]
    name: String,
    version: String,
    description: String,
    #[serde(default)]
    components: Vec<OrchestratorComponent>,
}

#[derive(Debug, Deserialize)]
struct OrchestratorComponent {
    id: String,
    name: String,
    description: String,
}

fn default_orch_id() -> String {
    "orchestrator".into()
}
fn default_orch_name() -> String {
    "VibeCoded Orchestrator".into()
}

/// Find `vct-module.json` at the repo root. We walk up from
/// CARGO_MANIFEST_DIR (the launcher/src-tauri/) to the repo root. Only used
/// at runtime in dev/source builds; release builds will have to ship the
/// JSON inside the bundled resources, but for now the dev build path is what
/// matters since users run `npm run tauri:dev` against the repo checkout.
fn find_orchestrator_manifest() -> Option<PathBuf> {
    let here = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut p = here.as_path();
    loop {
        let candidate = p.join("vct-module.json");
        if candidate.is_file() {
            return Some(candidate);
        }
        match p.parent() {
            Some(parent) => p = parent,
            None => return None,
        }
    }
}

fn read_orchestrator_manifest() -> Option<OrchestratorManifest> {
    let path = find_orchestrator_manifest()?;
    let raw = std::fs::read_to_string(&path).ok()?;
    serde_json::from_str(&raw).ok()
}

/// Bug 16: launcher's own version, sourced from CARGO_PKG_VERSION at compile
/// time. Always reflects the running binary, not whatever package.json
/// happens to say.
fn launcher_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// Bug 16: detect whether the orchestrator core is "installed" by checking
/// whether any project in the launcher DB has host=base. This is the same
/// signal used elsewhere — projects with the base host imply a working
/// orchestrator install.
fn orchestrator_installed(db: &Db) -> bool {
    db.list_projects()
        .map(|rows| rows.iter().any(|p| matches!(p.host, ProjectHost::Base)))
        .unwrap_or(false)
}

/// Bug 16: built-in catalog entries that always render in /modules even
/// when no installable modules are present. Reflects real repo state:
/// launcher version comes from CARGO_PKG_VERSION, orchestrator + components
/// from `vct-module.json` at repo root, install state from the launcher DB.
fn builtin_catalog_entries(db: &Db) -> Vec<ModuleCatalogEntry> {
    let mut out = Vec::new();

    // 1. The launcher itself — always "bundled" (the running process).
    out.push(ModuleCatalogEntry {
        id: "vct-launcher".into(),
        name: "VCT Launcher".into(),
        version: launcher_version().into(),
        description:
            "Desktop launcher: manages orchestrator runtime, projects, secrets, licensing. \
             Bundled with the orchestrator install."
                .into(),
        category: "launcher".into(),
        tags: vec!["bundled".into(), "free".into()],
        license_required: false,
        license_variant_ids: vec![],
        min_orchestrator_tier: "free".into(),
        compatibility_hosts: vec!["base".into(), "mao".into()],
        is_licensed: true,
        manifest_source: "builtin".into(),
        kind: "bundled".into(),
        parent_id: String::new(),
        cta_route: String::new(),
    });

    // 2. Orchestrator core + 3-4. its sub-components, sourced from
    //    `vct-module.json`. If the manifest is missing, we still emit the
    //    core entry with version "dev" so the catalog isn't empty.
    let manifest = read_orchestrator_manifest();
    let (orch_version, orch_desc, components): (String, String, Vec<OrchestratorComponent>) =
        if let Some(m) = manifest {
            (m.version, m.description, m.components)
        } else {
            (
                "dev".into(),
                "The core: hooks, agents, skills, KG + Code Graph, 4 MCP servers. \
                 Runs locally in Podman/Docker."
                    .into(),
                vec![
                    OrchestratorComponent {
                        id: "knowledge-graph".into(),
                        name: "Knowledge Graph".into(),
                        description: "Markdown-based KG, Weaviate-backed embeddings.".into(),
                    },
                    OrchestratorComponent {
                        id: "code-graph".into(),
                        name: "Code Graph".into(),
                        description: "AST analysis with Tree-sitter, optional Joern.".into(),
                    },
                ],
            )
        };

    let installed = orchestrator_installed(db);
    out.push(ModuleCatalogEntry {
        id: "orchestrator".into(),
        name: "VibeCoded Orchestrator".into(),
        version: orch_version.clone(),
        description: orch_desc,
        category: "core".into(),
        tags: vec!["free".into(), "agpl-3.0".into()],
        license_required: false,
        license_variant_ids: vec![],
        min_orchestrator_tier: "free".into(),
        compatibility_hosts: vec!["base".into()],
        is_licensed: true,
        manifest_source: "builtin".into(),
        kind: if installed { "installed".into() } else { "available".into() },
        parent_id: String::new(),
        cta_route: String::new(),
    });

    for comp in components {
        let route = match comp.id.as_str() {
            "knowledge-graph" => "/kg",
            "code-graph" => "/codegraph",
            _ => "",
        };
        out.push(ModuleCatalogEntry {
            id: comp.id,
            name: comp.name,
            version: orch_version.clone(),
            description: comp.description,
            category: "subcomponent".into(),
            tags: vec!["free".into(), "bundled-with-orchestrator".into()],
            license_required: false,
            license_variant_ids: vec![],
            min_orchestrator_tier: "free".into(),
            compatibility_hosts: vec!["base".into()],
            is_licensed: true,
            manifest_source: "builtin".into(),
            kind: "subcomponent".into(),
            parent_id: "orchestrator".into(),
            cta_route: route.into(),
        });
    }

    out
}

// ─── Catalog discovery ──────────────────────────────────────────────────

/// Locations the launcher scans for vct-module.json files.
///
/// For v1 this is just the local modules directory (installed modules have
/// their manifest at the root of their install dir). A future version adds
/// `https://registry.vibecodedtools.it/modules.json` for discovering
/// uninstalled modules too.
fn catalog_scan_paths() -> Vec<PathBuf> {
    let mut paths = Vec::new();
    if let Some(home) = directories::UserDirs::new() {
        let modules = home.home_dir().join(".vct").join("modules");
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
        // Also scan bundled-with-launcher manifests, if present.
        let bundled = home.home_dir().join(".vct").join("bundled_manifests");
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
    paths
}

fn is_module_licensed(manifest: &ModuleManifest, db: &Db) -> bool {
    if !manifest.license.required {
        return true;
    }
    let cache = match db.get_tier_cache() {
        Ok(c) => c,
        Err(_) => return false,
    };
    // 1. Orchestrator-tier satisfies?
    let tier_rank = |t: &str| match t {
        "free" => 0,
        "pro" => 1,
        "mao" => 2,
        "enterprise" => 3,
        _ => 0,
    };
    if tier_rank(&cache.orchestrator_tier) >= tier_rank(&manifest.license.min_orchestrator_tier)
        && manifest.license.min_orchestrator_tier != "free"
    {
        return true;
    }
    // 2. Module-specific license?
    if let Some(entry) = cache.module_licenses.get(&manifest.id) {
        if entry.get("tier").is_some() {
            return true;
        }
    }
    // 3. No gate matched.
    manifest.license.variant_ids.is_empty() // if no variants declared, treat as free
}

#[command]
pub async fn list_module_catalog(db: State<'_, Db>) -> Result<Vec<ModuleCatalogEntry>, String> {
    // Bug 16: built-in entries (launcher + orchestrator + KG + code graph)
    // come first — they're always present and reflect real repo state.
    let mut out = builtin_catalog_entries(&db);
    for path in catalog_scan_paths() {
        let raw = match std::fs::read_to_string(&path) {
            Ok(s) => s,
            Err(_) => continue,
        };
        let manifest = match ModuleManifest::from_json(&raw) {
            Ok(m) => m,
            Err(e) => {
                eprintln!("[catalog] skip {}: {}", path.display(), e);
                continue;
            }
        };
        // Skip if a built-in already covers this id (defensive against a
        // user dropping a `vct-module.json` named "orchestrator" in
        // ~/.vct/bundled_manifests/).
        if out.iter().any(|e| e.id == manifest.id) {
            continue;
        }
        let licensed = is_module_licensed(&manifest, &db);
        out.push(ModuleCatalogEntry::from_manifest(
            &manifest,
            licensed,
            path.display().to_string(),
        ));
    }
    Ok(out)
}

fn find_manifest(module_id: &str) -> Result<(ModuleManifest, PathBuf), String> {
    for path in catalog_scan_paths() {
        let raw = std::fs::read_to_string(&path).unwrap_or_default();
        if let Ok(m) = ModuleManifest::from_json(&raw) {
            if m.id == module_id {
                return Ok((m, path));
            }
        }
    }
    Err(format!("module {} not in catalog", module_id))
}

// ─── Install / Uninstall ────────────────────────────────────────────────

#[command]
pub async fn install_module_for_project(
    app: AppHandle,
    project_id: String,
    module_id: String,
    db: State<'_, Db>,
) -> Result<ModuleInstallRow, String> {
    // 1. Project exists + get host
    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    // 2. Manifest lookup
    let (manifest, manifest_path) = find_manifest(&module_id)?;

    // 3. Host compatibility
    let host_str = project.host.as_str();
    if !manifest.is_compatible_with_host(host_str) {
        return Err(format!(
            "module {} not compatible with host '{}' (supports: {:?})",
            module_id, host_str, manifest.compatibility.hosts
        ));
    }

    // 4. License gate
    if !is_module_licensed(&manifest, &db) {
        return Err(format!(
            "module {} requires a license (variant_ids: {:?} or orchestrator tier >= {})",
            module_id, manifest.license.variant_ids, manifest.license.min_orchestrator_tier
        ));
    }

    // 5. Insert pending row
    let install_id = Uuid::new_v4().to_string();
    let ctx = PlaceholderCtx::new(&module_id);
    let install_dir = ctx.resolve_install_dir(&manifest.install.install_dir);
    let row = db.insert_module_install(
        &install_id,
        &project_id,
        &module_id,
        &manifest.version,
        &install_dir.display().to_string(),
    )?;
    db.audit(
        "module_install_start",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({
            "version": manifest.version,
            "manifest_source": manifest_path.display().to_string(),
        }),
    )?;

    // 6. Run install engine
    match installer_engine::run_install(&app, &manifest, &ctx, &project_id).await {
        Ok(resolved_dir) => {
            db.set_module_status(&project_id, &module_id, ModuleStatus::Installed, None)?;
            db.audit(
                "module_install_done",
                Some(&project_id),
                Some(&module_id),
                &serde_json::json!({ "install_dir": resolved_dir.display().to_string() }),
            )?;
            let _ = app.emit(
                "module://install-complete",
                serde_json::json!({
                    "project_id": project_id,
                    "module_id": module_id,
                    "success": true,
                }),
            );
            Ok(ModuleInstallRow {
                status: ModuleStatus::Installed,
                ..row
            })
        }
        Err(e) => {
            db.set_module_status(
                &project_id,
                &module_id,
                ModuleStatus::Error,
                Some(e.clone()),
            )?;
            let _ = app.emit(
                "module://install-complete",
                serde_json::json!({
                    "project_id": project_id,
                    "module_id": module_id,
                    "success": false,
                    "error": e,
                }),
            );
            Err(e)
        }
    }
}

#[command]
pub async fn uninstall_module_v2(
    project_id: String,
    module_id: String,
    purge_data: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    let row = db
        .get_module_install(&project_id, &module_id)?
        .ok_or_else(|| format!("module {} not installed for project {}", module_id, project_id))?;

    // Best-effort filesystem cleanup. Failures here don't fail the command —
    // the DB row removal is the source of truth for "installed or not".
    let install_path = PathBuf::from(&row.install_path);
    if install_path.exists() {
        if let Err(e) = tokio::fs::remove_dir_all(&install_path).await {
            eprintln!("[uninstall] remove_dir_all {}: {}", install_path.display(), e);
        }
    }

    if purge_data {
        // Scrub {VCT_DATA}/{MODULE_ID}/ when user explicitly asked to wipe.
        if let Some(home) = directories::UserDirs::new() {
            let data_dir = home.home_dir().join(".vct").join("data").join(&module_id);
            if data_dir.exists() {
                let _ = tokio::fs::remove_dir_all(&data_dir).await;
            }
        }
    }

    db.delete_module_install(&project_id, &module_id)?;
    db.clear_module_settings(&project_id, &module_id)?;
    db.audit(
        "module_uninstall",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({ "purge_data": purge_data }),
    )?;
    Ok(())
}

// ─── Listing + status ───────────────────────────────────────────────────

#[command]
pub async fn list_installed_modules(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<ModuleInstallRow>, String> {
    db.list_module_installs_for_project(&project_id)
}

#[derive(Debug, Serialize)]
pub struct ModuleStatusView {
    pub status: String,
    pub enabled: bool,
    pub installed_at: i64,
    pub last_started_at: Option<i64>,
    pub last_error: Option<String>,
}

#[command]
pub async fn module_status_v2(
    project_id: String,
    module_id: String,
    db: State<'_, Db>,
) -> Result<Option<ModuleStatusView>, String> {
    let row = db.get_module_install(&project_id, &module_id)?;
    Ok(row.map(|r| ModuleStatusView {
        status: r.status.as_str().to_string(),
        enabled: r.enabled,
        installed_at: r.installed_at,
        last_started_at: r.last_started_at,
        last_error: r.last_error,
    }))
}

#[command]
pub async fn set_module_enabled_v2(
    project_id: String,
    module_id: String,
    enabled: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_module_enabled(&project_id, &module_id, enabled)?;
    db.audit(
        "module_enabled_toggle",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({ "enabled": enabled }),
    )?;
    Ok(())
}

// ─── Ignore `ProjectHost` import warning shim ─────────────────────────
#[allow(dead_code)]
fn _unused_host_import(_h: ProjectHost) {}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StartStopReq {
    pub project_id: String,
    pub module_id: String,
}
