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
    ///   - "coming_soon"  = announced, not yet shipped. Rendered with a
    ///                      "Coming Soon" badge + Learn-more CTA, no Install.
    ///                      Reserved for items with a public roadmap commitment;
    ///                      do NOT use for vapor.
    pub kind: String,
    /// For subcomponents: which parent module they ship with. Empty otherwise.
    #[serde(default)]
    pub parent_id: String,
    /// Optional dashboard route for subcomponents (e.g. "/kg", "/codegraph").
    #[serde(default)]
    pub cta_route: String,
    /// For `kind == "coming_soon"`: the tier this will ship under
    /// (e.g. "pro", "mao"). Empty for everything else.
    #[serde(default)]
    pub coming_soon_tier: String,
    /// For `kind == "coming_soon"`: optional target shipping window
    /// (e.g. "Q3 2026"). Empty when no public commitment exists.
    #[serde(default)]
    pub coming_soon_target: String,
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
            coming_soon_tier: String::new(),
            coming_soon_target: String::new(),
        }
    }
}

// Bug 16 minimal-manifest types + `vct-module.json` walker moved to
// `vct-launcher-core::orchestrator_manifest` in v0.2.21 Step 4a so the
// detached vct-hub binary can consume them too. The launcher still
// reaches them through the same crate-local paths via these re-exports.
pub(crate) use vct_launcher_core::orchestrator_manifest::{
    find_orchestrator_manifest, read_orchestrator_manifest, OrchestratorComponent,
};

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
        coming_soon_tier: String::new(),
        coming_soon_target: String::new(),
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
        coming_soon_tier: String::new(),
        coming_soon_target: String::new(),
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
            coming_soon_tier: String::new(),
            coming_soon_target: String::new(),
        });
    }

    // 5. RL Reranker — Pro-tier paid module (vct-rl-reranker, v0.1.0).
    //    Flipped from `coming_soon` to `available` in Phase 1C (2026-05-16)
    //    once the manifest + container-pull installer recipe landed. Real
    //    manifest lives in the orchestrator clone at
    //    `paid-modules/vct-rl-reranker/vct-module.json` during dev; in
    //    production the launcher fetches it via Phase 3C's
    //    /rl-latest-version Supabase edge function (catalog discovery
    //    against an authenticated registry — never bundled in the AGPL
    //    release because the manifest declares the private GHCR image
    //    coords + token gateway URL).
    //
    //    `kind: "available"` makes the UI render an Install button.
    //    `is_licensed` is computed dynamically from the tier cache in
    //    list_module_catalog (this hardcoded placeholder ships as
    //    is_licensed=false; reality is filled in at catalog-build time).
    //
    //    Pre-flip there was a unit test asserting "exactly one coming_soon
    //    entry, must be rl-reranker". That test was updated in this PR to
    //    assert "zero coming_soon entries — anything we previously gated
    //    behind coming_soon is now real". See
    //    builtin_catalog_lists_zero_coming_soon_entries below.
    out.push(ModuleCatalogEntry {
        id: "rl-reranker".into(),
        name: "RL Reranker".into(),
        version: "0.1.0".into(),
        description:
            "Reinforcement-learning reranker for KG + Code Graph retrieval. \
             Per-embedding-source neural networks personalize on your local \
             citation patterns. Ships pre-trained; auto-fine-tunes on your \
             data after each weekly model refresh. Pro tier required."
                .into(),
        category: "paid-independent".into(),
        tags: vec!["pro".into(), "reranking".into(), "reinforcement-learning".into()],
        license_required: true,
        license_variant_ids: vec![],
        min_orchestrator_tier: "pro".into(),
        compatibility_hosts: vec!["base".into(), "orchestrator_root".into()],
        is_licensed: false,
        manifest_source: "paid-modules/vct-rl-reranker/vct-module.json".into(),
        kind: "available".into(),
        parent_id: String::new(),
        cta_route: String::new(),
        coming_soon_tier: String::new(),
        coming_soon_target: String::new(),
    });

    out
}

// ─── Catalog discovery ──────────────────────────────────────────────────

/// Locations the launcher scans for vct-module.json files.
///
/// For v1 this is just the local modules directory (installed modules have
/// their manifest at the root of their install dir). A future version adds
/// `https://registry.vibecodedtools.it/modules.json` for discovering
/// uninstalled modules too.
///
/// Phase 1C (2026-05-16): added a dev-only scan of `<orchestrator_clone>/
/// paid-modules/*/vct-module.json` so the RL Reranker manifest is
/// discoverable on a dev machine even before the production discovery
/// path (Phase 3C: `/rl-latest-version` Supabase edge function) lands.
/// In a real user install the orchestrator clone is read-only and the
/// paid-modules dir doesn't exist there, so this path is a no-op for
/// non-dev users.
fn catalog_scan_paths() -> Vec<PathBuf> {
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
        // Also scan bundled-with-launcher manifests, if present.
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

    // Dev-only: scan <orchestrator_clone>/paid-modules/*/vct-module.json.
    // Resolved from VCT_INSTALL_ROOT (set by the launcher at startup to the
    // active orchestrator clone) OR from $CARGO_MANIFEST_DIR's grandparent
    // (works at `cargo test` time). Never panics — missing dir is silent.
    let orchestrator_clone = std::env::var_os("VCT_INSTALL_ROOT")
        .map(PathBuf::from)
        .or_else(|| {
            // Fallback for dev: walk up from cargo manifest dir to repo root.
            Some(
                PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .parent()? // launcher/
                    .parent()? // repo root
                    .to_path_buf(),
            )
        });
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

    // 6. Run install engine.
    //
    // v0.2.20: probe the persisted hardware snapshot for the current
    // GpuMode so container_pull can pick a per-variant image tag (when
    // the manifest declares `runtime.gpu_image_variants`). Falls back
    // to `Cpu` when no snapshot exists yet — safe default; user can
    // redetect-hardware later and reinstall to switch variants.
    let gpu_mode = crate::commands::installer::read_persisted_hardware_snapshot(db.inner())
        .ok()
        .flatten()
        .map(|snap| snap.gpu_mode_decided)
        .unwrap_or(crate::commands::gpu_policy::GpuMode::Cpu);

    match installer_engine::run_install(&app, &manifest, &ctx, &project_id, gpu_mode).await {
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
        let data_dir = crate::paths::vct_root_dir().join("data").join(&module_id);
        if data_dir.exists() {
            let _ = tokio::fs::remove_dir_all(&data_dir).await;
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

// ─── Per-module start/stop ─────────────────────────────────────────────
//
// Modules in this codebase are the installable orchestrator add-ons (RL
// Reranker, etc., listed in `modules/catalog`). Independent of the
// underlying container services (Weaviate / Ollama / etc., which live
// in `commands/lifecycle.rs::services_*`). A module is "running" when
// the orchestrator is actively using it for a given project; "stopped"
// means the binding stays in place but no work is dispatched to it.
//
// V1 implementation: state-only. We update the ModuleStatus row from
// Running ↔ Stopped and emit an audit event. A future iteration will
// hook process supervision (per-module sidecar processes, pause/resume
// signalling, etc.) — but for V1 the orchestrator runtime checks this
// status flag before dispatching, so the toggle is observable end-to-end
// without any process-management plumbing.
//
// Idempotent: calling start on an already-Running module is a no-op
// (status row update is upserted; audit row records the request).

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StartStopReq {
    pub project_id: String,
    pub module_id: String,
}

#[command]
pub async fn module_start_v2(
    req: StartStopReq,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_module_status(&req.project_id, &req.module_id, ModuleStatus::Running, None)?;
    db.audit(
        "module_start",
        Some(&req.project_id),
        Some(&req.module_id),
        &serde_json::json!({}),
    )?;
    Ok(())
}

#[command]
pub async fn module_stop_v2(
    req: StartStopReq,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_module_status(&req.project_id, &req.module_id, ModuleStatus::Stopped, None)?;
    db.audit(
        "module_stop",
        Some(&req.project_id),
        Some(&req.module_id),
        &serde_json::json!({}),
    )?;
    Ok(())
}

// ─── Ignore `ProjectHost` import warning shim ─────────────────────────
//
// `ProjectHost` is imported by `use crate::db::models` at the top of
// this file because several other functions in this module use it.
// Rust's unused-warning is per-import-line, so we keep this shim to
// silence the warning when `ProjectHost` is referenced only inside
// the function-bodies the compiler considers "external" to its
// staticanalysis.
#[allow(dead_code)]
fn _unused_host_import(_h: ProjectHost) {}

#[cfg(test)]
mod tests {
    //! Reviewer-B fix-8 audit: the home page used to advertise 6 vapor
    //! "sister apps" (Transcrypt / Arzillibus / ConvertiFacile / DataWeave /
    //! FormCraft / PixelSnap). After Bug 16 the source of truth is
    //! `list_module_catalog`, so the home page renders only what this list
    //! returns. These tests pin the contract:
    //!   - exactly one entry of each real built-in (launcher, orchestrator,
    //!     KG, Code Graph)
    //!   - exactly ONE coming-soon entry: RL Reranker
    //!   - no entry whose id matches any of the historical vapor names
    use super::*;
    use crate::db::Db;

    fn open_db() -> Db {
        Db::open_in_memory().expect("in-memory db")
    }

    #[test]
    fn builtin_catalog_contains_all_four_real_entries() {
        let db = open_db();
        let entries = builtin_catalog_entries(&db);
        let ids: Vec<&str> = entries.iter().map(|e| e.id.as_str()).collect();

        assert!(ids.contains(&"vct-launcher"), "missing vct-launcher: {:?}", ids);
        assert!(ids.contains(&"orchestrator"), "missing orchestrator: {:?}", ids);
        // KG + Code Graph are sub-components of orchestrator.
        assert!(
            ids.contains(&"knowledge-graph"),
            "missing knowledge-graph: {:?}",
            ids
        );
        assert!(ids.contains(&"code-graph"), "missing code-graph: {:?}", ids);
    }

    /// Regression guard for the v0.1.6 hardcoded-version bug (fix-3, v0.2.13).
    ///
    /// `list_module_catalog` must always return an entry for `id="orchestrator"`
    /// with a non-empty version string. Before fix-3, the Store page was
    /// showing the hardcoded `version: '0.1.6'` whenever the catalog was empty
    /// or the orchestrator entry had an empty version. The catalog entry is
    /// now always present (built-in, sourced from `vct-module.json`) and the
    /// Store page renders the version only when non-empty.
    #[test]
    fn builtin_catalog_orchestrator_entry_has_non_empty_version() {
        let db = open_db();
        let entries = builtin_catalog_entries(&db);
        let orch = entries
            .iter()
            .find(|e| e.id == "orchestrator")
            .expect("orchestrator entry must be present in builtin catalog");
        assert!(
            !orch.version.is_empty(),
            "orchestrator catalog entry must have a non-empty version string; \
             got empty string (vct-module.json missing or malformed?). \
             The Store page falls back to the static `version` field in `allApps` \
             when the catalog returns no version — leaving it empty prevents \
             showing a stale hardcoded string."
        );
    }

    /// The vct-launcher entry must also carry the live CARGO_PKG_VERSION,
    /// not a hardcoded string. Regression guard added alongside fix-3.
    #[test]
    fn builtin_catalog_launcher_entry_has_non_empty_version() {
        let db = open_db();
        let entries = builtin_catalog_entries(&db);
        let launcher = entries
            .iter()
            .find(|e| e.id == "vct-launcher")
            .expect("vct-launcher entry must be present in builtin catalog");
        assert!(
            !launcher.version.is_empty(),
            "vct-launcher catalog entry must have a non-empty version string (CARGO_PKG_VERSION); \
             got empty. If this fires in CI, check that the Cargo.toml `[package] version` is set."
        );
    }

    #[test]
    fn builtin_catalog_lists_zero_coming_soon_entries() {
        // Phase 1C (2026-05-16): rl-reranker was promoted from `coming_soon`
        // to `available` once its manifest landed at
        // `paid-modules/vct-rl-reranker/vct-module.json` and the launcher's
        // installer gained the ContainerPull recipe. The pre-flip test
        // pinned "exactly one coming_soon entry, must be rl-reranker" —
        // now we pin the inverse: no vapor entries should resurface.
        // If a future PR re-introduces a `coming_soon` entry, force the
        // author to justify it by failing this test.
        let db = open_db();
        let entries = builtin_catalog_entries(&db);
        let coming: Vec<&ModuleCatalogEntry> =
            entries.iter().filter(|e| e.kind == "coming_soon").collect();
        assert_eq!(
            coming.len(),
            0,
            "expected zero coming-soon entries; got {}: {:?}. \
             If you intentionally added a new vapor entry, update this test \
             and document the public roadmap commitment in the entry's \
             coming_soon_target field.",
            coming.len(),
            coming.iter().map(|e| &e.id).collect::<Vec<_>>()
        );
    }

    #[test]
    fn builtin_catalog_lists_rl_reranker_as_available_paid_module() {
        // Phase 1C: confirm rl-reranker is now `available` with the right
        // tier, host compatibility, and version. This is the inverse of
        // the old `lists_exactly_one_coming_soon` test — it pins the
        // post-flip state instead.
        let db = open_db();
        let entries = builtin_catalog_entries(&db);
        let rl = entries
            .iter()
            .find(|e| e.id == "rl-reranker")
            .expect("rl-reranker entry must be present");

        assert_eq!(rl.kind, "available", "rl-reranker should be installable");
        assert_eq!(rl.version, "0.1.0", "matches manifest.version");
        assert_eq!(rl.min_orchestrator_tier, "pro");
        assert!(rl.license_required);
        assert_eq!(rl.category, "paid-independent");
        assert!(
            rl.compatibility_hosts.contains(&"base".to_string()),
            "must support base-host projects"
        );
        assert!(
            rl.compatibility_hosts.contains(&"orchestrator_root".to_string()),
            "must support orchestrator-root project (VCO_dev itself)"
        );
        assert!(
            rl.manifest_source.contains("vct-rl-reranker"),
            "manifest_source should reference the paid-modules path"
        );
    }

    #[test]
    fn builtin_catalog_contains_no_vapor_module_ids() {
        // Names reviewer B specifically called out as vapor on the home page.
        // Belt-and-suspenders: even if a future commit ever re-adds them,
        // this guard fails the build.
        let db = open_db();
        let entries = builtin_catalog_entries(&db);
        let forbidden = [
            "transcrypt",
            "arzillibus",
            "convertifacile",
            "dataweave",
            "formcraft",
            "pixelsnap",
            "telegram",
            "mao",
            "orchestrator-pro", // marketing card disguised as a module
        ];
        for e in &entries {
            assert!(
                !forbidden.contains(&e.id.as_str()),
                "FORBIDDEN: vapor module id '{}' resurfaced in builtin_catalog_entries",
                e.id
            );
        }
    }

    // ─── Bundled manifest schema regression tests (0.1.7 fork-readiness) ──
    //
    // The bundled manifests under `launcher/bundled_manifests/` declare the
    // secret keys that the launcher's hub `/projects/{id}/env` endpoint
    // exposes to bundled MCP wrappers (e.g. `claude_mcp_servers/search_mcp/
    // wrapper.sh`). Pre-0.1.7 the search MCP manifest declared
    // `GITHUB_TOKEN` (uppercase, scope=global), but the wrapper queries the
    // resolver for `github_pat` (lowercase, scope=shared). That mismatch
    // meant the resolver always returned `key_not_active`, so the wrapper
    // fell through to its `VCT_LEGACY_FILE_FALLBACK=1` path on every call.
    //
    // These tests pin:
    //   * The bundled `vct-search.json` parses as a valid `ModuleManifest`.
    //   * The `github_pat` secret is declared with `scope: "shared"` —
    //     matching what `wrapper.sh` queries and what
    //     `commands/installer.rs::register_github_pat` migrates to the
    //     OS keychain. The legacy `~/.vct-secrets/shared/github_pat`
    //     file path is read as a fallback during the one-time keychain
    //     migration (gated by APP_STATE_KEY_GITHUB_PAT_MIGRATED, landed
    //     in 0.2.0). Post-migration the keychain entry is the source of
    //     truth; the file lingers as documentation of past state.
    //   * `runtime.env_from_secrets` references `github_pat` (not the
    //     legacy `GITHUB_TOKEN`), so when launcher-managed runtime
    //     injection lands it picks up the right keychain entry.
    //
    // If any of these assertions fail, the resolver path will silently
    // 404 again — same regression we just fixed.
    fn bundled_search_manifest_path() -> std::path::PathBuf {
        // CARGO_MANIFEST_DIR is /<repo>/launcher/src-tauri at compile time.
        // The bundled manifest tree lives at /<repo>/launcher/bundled_manifests.
        std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("bundled_manifests")
            .join("vct-search.json")
    }

    #[test]
    fn bundled_search_manifest_parses_cleanly() {
        let path = bundled_search_manifest_path();
        let raw = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
        let parsed = ModuleManifest::from_json(&raw)
            .unwrap_or_else(|e| panic!("vct-search.json failed to parse: {}", e));
        assert_eq!(parsed.id, "vct-search");
    }

    #[test]
    fn bundled_search_manifest_does_not_declare_github_pat_post_v0_2_11() {
        // PR-14a (v0.2.11) dropped `search_code` from the search MCP
        // (redundant with Claude's native WebSearch + site:github.com).
        // PR-14b removed `github_pat` from vct-search.json's secrets
        // list because no surviving search-MCP tool needs it.
        //
        // Regression guard: if a future PR re-introduces a `github_pat`
        // secret declaration in vct-search.json (or the uppercase
        // legacy `GITHUB_TOKEN` shape), fail loudly — the search MCP
        // no longer has a GitHub-querying tool that would consume it,
        // so the secret would be requested from the user for nothing.
        //
        // (`github_pat` is still legitimately requested by the
        // OnboardingWizard `register_github_pat` flow for git-push
        // auth + other modules; this test only asserts that the
        // search MCP doesn't claim it.)
        let path = bundled_search_manifest_path();
        let raw = std::fs::read_to_string(&path).expect("read vct-search.json");
        let parsed = ModuleManifest::from_json(&raw).expect("parse vct-search.json");

        assert!(
            !parsed.secrets.iter().any(|s| s.key == "github_pat"),
            "vct-search.json must NOT declare a `github_pat` secret in v0.2.11+ \
             (search_code tool dropped — no consumer left). If you re-added a \
             GitHub-querying tool, document why this guard is being inverted."
        );
        assert!(
            !parsed.secrets.iter().any(|s| s.key == "GITHUB_TOKEN"),
            "vct-search.json must NOT re-declare GITHUB_TOKEN (legacy uppercase) — \
             search MCP no longer queries GitHub at all in v0.2.11+."
        );
    }

    #[test]
    fn bundled_search_manifest_runtime_env_matches_secret_keys() {
        let path = bundled_search_manifest_path();
        let raw = std::fs::read_to_string(&path).expect("read vct-search.json");
        let parsed = ModuleManifest::from_json(&raw).expect("parse vct-search.json");

        // Every key in `runtime.env_from_secrets` must correspond to an
        // actual `secrets[].key` declaration. Mismatches mean the
        // launcher-managed runtime would try to inject from a keychain
        // entry that nothing writes to — silent breakage.
        let declared: std::collections::HashSet<&str> =
            parsed.secrets.iter().map(|s| s.key.as_str()).collect();
        for env_key in &parsed.runtime.env_from_secrets {
            assert!(
                declared.contains(env_key.as_str()),
                "runtime.env_from_secrets references {:?} but no matching `secrets[].key` is declared. \
                 Declared keys: {:?}",
                env_key,
                declared,
            );
        }
    }
}
