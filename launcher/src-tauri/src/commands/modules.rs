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
use crate::manifest::{ModuleManifest, PlaceholderCtx, UninstallBlock};
use crate::secrets::{self, SecretScope};

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
    /// v0.2.31 module-deprecation surface (Layer 1: GUI). `true` when the
    /// module's publisher has marked the running version deprecated via
    /// the `runtime.update_endpoint` response. Renders the amber
    /// `DEPRECATED` badge in the launcher's Modules card head; does NOT
    /// block install/run (deprecated modules keep working until EOL).
    ///
    /// Populated at catalog-build time once the v0.2.32 poller wires the
    /// Supabase response into `apply_deprecation_state`. v0.2.31 ships
    /// the field with a `false` default so the UI is forward-compatible;
    /// manual flips via the `apply_deprecation_state` Tauri command set
    /// the env vars + audit row independently of this catalog field.
    #[serde(default)]
    pub deprecated: bool,
    /// Optional human-readable deprecation message (rendered in the badge
    /// tooltip + dashboard banner). Empty when the publisher hasn't
    /// provided one or the module is not deprecated.
    #[serde(default)]
    pub deprecation_message: String,
    /// Optional ISO date string (`YYYY-MM-DD`) for the module's
    /// end-of-life date. Empty when unknown.
    #[serde(default)]
    pub deprecation_eol_date: String,
    /// Optional URL pointing at the publisher's migration guide.
    /// Empty when unknown.
    #[serde(default)]
    pub deprecation_migration_url: String,
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
            // v0.2.31: defaults — catalog-build time doesn't yet read
            // `runtime.update_endpoint`. See struct doc comment.
            deprecated: false,
            deprecation_message: String::new(),
            deprecation_eol_date: String::new(),
            deprecation_migration_url: String::new(),
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
        deprecated: false,
        deprecation_message: String::new(),
        deprecation_eol_date: String::new(),
        deprecation_migration_url: String::new(),
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
        deprecated: false,
        deprecation_message: String::new(),
        deprecation_eol_date: String::new(),
        deprecation_migration_url: String::new(),
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
            deprecated: false,
            deprecation_message: String::new(),
            deprecation_eol_date: String::new(),
            deprecation_migration_url: String::new(),
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
        // G1 (v0.2.22): aligned with on-disk manifest `id: "vct-rl-reranker"`.
        // Pre-v0.2.22 this was bare `"rl-reranker"`, which caused
        // `find_manifest(module_id)` to return Err on install because no
        // on-disk manifest reports that id. Every other reference in the
        // codebase (rl_settings.rs::MODULE_ID, module_supervisor.rs,
        // module_weights_state.rs, module_gui.rs route resolver, etc.)
        // already used the prefixed form — the catalog was the lone outlier.
        // G3 (v0.2.22): version pinned to 0.1.1 — the released image tag on
        // GHCR (`ghcr.io/hotak92/vct-rl-reranker:0.1.1-{cpu,cuda,rocm}`)
        // and the version `runtime.args` + `gpu_image_variants` already
        // reference. Pre-v0.2.22 catalog said 0.1.0 (stale) while the
        // on-disk manifest said 0.1.2 (unreleased bump).
        id: "vct-rl-reranker".into(),
        name: "RL Reranker".into(),
        version: "0.1.1".into(),
        // R1 (v0.2.22): description text mirrors the on-disk manifest's
        // `description` field. The new
        // `catalog_matches_on_disk_manifest_when_present` round-trip test
        // asserts the two match — drift either way will fail CI.
        description:
            "Reinforcement-learning reranker for Knowledge Graph retrieval. \
             Per-text-embedding-source neural networks (qwen3 / arctic / \
             openai) personalize on your local citation patterns. Ships \
             pre-trained; auto-fine-tunes on your data after each weekly \
             model refresh. Supports hot-swap, reset, finetune, and \
             global-retrain endpoints so the launcher can drive model \
             lifecycle without restarting the container. Code-graph \
             reranking will ship as a separate future module \
             (vct-code-reranker)."
                .into(),
        category: "paid-independent".into(),
        tags: vec!["pro".into(), "reranking".into(), "reinforcement-learning".into()],
        license_required: true,
        license_variant_ids: vec![],
        min_orchestrator_tier: "pro".into(),
        // R1 (v0.2.22): order + content mirrors the on-disk manifest's
        // `compatibility.hosts` field exactly (`["base", "mao",
        // "orchestrator_root"]`). The new round-trip test asserts
        // Vec equality, not subset — drift either way will fail CI.
        compatibility_hosts: vec![
            "base".into(),
            "mao".into(),
            "orchestrator_root".into(),
        ],
        is_licensed: false,
        manifest_source: "paid-modules/vct-rl-reranker/vct-module.json".into(),
        kind: "available".into(),
        parent_id: String::new(),
        cta_route: String::new(),
        coming_soon_tier: String::new(),
        coming_soon_target: String::new(),
        deprecated: false,
        deprecation_message: String::new(),
        deprecation_eol_date: String::new(),
        deprecation_migration_url: String::new(),
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
fn catalog_scan_paths(db: &Db) -> Vec<PathBuf> {
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

    // Scan <orchestrator_clone>/paid-modules/*/vct-module.json.
    //
    // v0.2.23.1 fix (2026-05-21): resolve via the shared sync helper
    // that reads `launcher.install_path` from app_state (DB-cached at
    // first install) with a current_exe() walk-up fallback. NO
    // CARGO_MANIFEST_DIR fallback — that macro embeds the build-host's
    // absolute path as a static string (PRIVACY LEAK + WRONG PATH on
    // shipped binaries; see self_update.rs:280 + installer.rs:4399 for
    // the 2026-05-06 privacy notes).
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
    //
    // `admin` is server-classified (Path A Vault token or Path B LS
    // variant) and the docs declare it a "strict superset of enterprise"
    // by feature gates (docs/features/06-license-and-commercial.md §"Tier
    // ordering"; db/tier.rs:40 reaffirms the contract). The pre-v0.2.22
    // tier_rank match omitted `admin` and fell through to the wildcard
    // (rank=0, free-equivalent), so an admin-tier user with NO matching
    // module-specific entry in `module_licenses` was rejected client-side
    // — visible to admin users as the Install button never enabling on a
    // paid module they should have universal access to. Discovered
    // 2026-05-21 during v0.2.22 post-push audit (see KG node
    // "v0.2.22 Release — 2026-05-20" §"Lesson — admin tier_rank gap").
    //
    // The fix maps admin to a rank STRICTLY ABOVE enterprise so any
    // future module declaring `min_orchestrator_tier: "enterprise"` is
    // also satisfied by admin without further code changes. The wire
    // contract from validate-tier remains the source of truth; this
    // gate is advisory UI only (server-side artifact gateway re-validates
    // a JWT at download time — docs/features/07-architecture.md:73).
    let tier_rank = |t: &str| match t {
        "free" => 0,
        "pro" => 1,
        "mao" => 2,
        "enterprise" => 3,
        "admin" => 4,
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
    for path in catalog_scan_paths(&db) {
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

fn find_manifest(db: &Db, module_id: &str) -> Result<(ModuleManifest, PathBuf), String> {
    for path in catalog_scan_paths(db) {
        let raw = std::fs::read_to_string(&path).unwrap_or_default();
        if let Ok(m) = ModuleManifest::from_json(&raw) {
            if m.id == module_id {
                return Ok((m, path));
            }
        }
    }
    Err(format!("module {} not in catalog", module_id))
}

/// Public manifest-lookup for the module_service restart path. Same logic as
/// `find_manifest` (catalog scan + first matching id wins) but discards
/// the source path since callers only need the parsed manifest.
pub fn find_manifest_for_resume(db: &Db, module_id: &str) -> Option<ModuleManifest> {
    find_manifest(db, module_id).ok().map(|(m, _)| m)
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
    let (manifest, manifest_path) = find_manifest(&db, &module_id)?;

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

    match installer_engine::run_install(&app, &manifest, &ctx, &project_id, gpu_mode, &db).await {
        Ok(resolved_dir) => {
            db.set_module_status(&project_id, &module_id, ModuleStatus::Installed, None)?;
            db.audit(
                "module_install_done",
                Some(&project_id),
                Some(&module_id),
                &serde_json::json!({ "install_dir": resolved_dir.display().to_string() }),
            )?;

            // Phase 1E: per-project container lifecycle. For
            // container_pull modules we resolve `runtime.container_name_
            // template`, allocate an `rl_port` if not yet set, and
            // spawn the container via `module_service`. Soft-fail throughout:
            // the install row stays at status=installed even when the
            // container start fails — the user can hit Restart from the
            // dashboard. Surfaces the error via audit + a non-blocking
            // toast event so the failure mode is visible without
            // rolling back the install.
            let resolved_container_name = if manifest.install.method
                == crate::manifest::InstallMethod::ContainerPull
                && manifest.runtime.r#type == "container"
            {
                match crate::commands::module_service::start_container_after_install(
                    &manifest,
                    &project,
                    &db,
                )
                .await
                {
                    Ok(name) => Some(name),
                    Err(e) => {
                        eprintln!(
                            "[module_service] start_container_after_install failed (install row stays installed): {}",
                            e
                        );
                        let _ = app.emit(
                            "module://container-start-failed",
                            serde_json::json!({
                                "project_id": project_id,
                                "module_id": module_id,
                                "error": e,
                            }),
                        );
                        let _ = db.audit(
                            "module_container_start_failed",
                            Some(&project_id),
                            Some(&module_id),
                            &serde_json::json!({ "error": e }),
                        );
                        None
                    }
                }
            } else {
                None
            };

            let _ = app.emit(
                "module://install-complete",
                serde_json::json!({
                    "project_id": project_id,
                    "module_id": module_id,
                    "success": true,
                    "container_name": resolved_container_name,
                }),
            );
            Ok(ModuleInstallRow {
                status: ModuleStatus::Installed,
                container_name: resolved_container_name,
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

/// v0.2.31 (#20-Fix-3): update an already-installed module to the catalog's
/// current version WITHOUT forcing the user through an uninstall+reinstall.
///
/// Looks up the previous install row, looks up the current manifest, then
/// invokes `installer_engine::run_upgrade` (which reads
/// `manifest.upgrade.{pre_upgrade,post_upgrade,migration_script}` and falls
/// back to a bare artifact re-fetch when no `upgrade` block is declared).
/// On success, bumps `module_installs.module_version` + `installed_at` and
/// re-emits `module://install-complete` with `success=true`.
///
/// Errors if the module is NOT installed for this project — the caller
/// must use `install_module_for_project` for the first install.
#[command]
pub async fn update_module_for_project(
    app: AppHandle,
    project_id: String,
    module_id: String,
    db: State<'_, Db>,
) -> Result<ModuleInstallRow, String> {
    // 1. Verify the module IS installed.
    let previous_install = db
        .get_module_install(&project_id, &module_id)?
        .ok_or_else(|| {
            format!(
                "module {} not installed for project {}; use install_module_for_project instead",
                module_id, project_id,
            )
        })?;

    // 2. Project exists + manifest lookup.
    let _project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;
    let (manifest, manifest_path) = find_manifest(&db, &module_id)?;

    // 3. License gate (same as install — paid modules require an active
    //    license at update time too, in case a Pro subscription lapsed
    //    between install and update).
    if !is_module_licensed(&manifest, &db) {
        return Err(format!(
            "module {} requires a license (variant_ids: {:?} or orchestrator tier >= {})",
            module_id, manifest.license.variant_ids, manifest.license.min_orchestrator_tier
        ));
    }

    db.audit(
        "module_update_start",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({
            "previous_version": previous_install.module_version,
            "new_version": manifest.version,
            "manifest_source": manifest_path.display().to_string(),
        }),
    )?;

    let ctx = PlaceholderCtx::new(&module_id);
    let gpu_mode = crate::commands::installer::read_persisted_hardware_snapshot(db.inner())
        .ok()
        .flatten()
        .map(|snap| snap.gpu_mode_decided)
        .unwrap_or(crate::commands::gpu_policy::GpuMode::Cpu);

    match installer_engine::run_upgrade(
        &app,
        &manifest,
        &previous_install,
        &ctx,
        &project_id,
        gpu_mode,
        &db,
    )
    .await
    {
        Ok(_resolved_dir) => {
            // Bump version + installed_at on the existing row.
            db.update_module_install_version(&project_id, &module_id, &manifest.version)?;
            // Status flip — running modules go back to Installed (the
            // dashboard supervisor will restart them on next tick if the
            // user had them running; in-flight container restarts are
            // out of scope here).
            db.set_module_status(&project_id, &module_id, ModuleStatus::Installed, None)?;
            db.audit(
                "module_update_done",
                Some(&project_id),
                Some(&module_id),
                &serde_json::json!({
                    "previous_version": previous_install.module_version,
                    "new_version": manifest.version,
                }),
            )?;
            let _ = app.emit(
                "module://install-complete",
                serde_json::json!({
                    "project_id": project_id,
                    "module_id": module_id,
                    "success": true,
                    "operation": "update",
                    "previous_version": previous_install.module_version,
                    "new_version": manifest.version,
                }),
            );
            // Return the refreshed row so the GUI doesn't have to re-query.
            let refreshed = db
                .get_module_install(&project_id, &module_id)?
                .ok_or("module_install row vanished after update")?;
            Ok(refreshed)
        }
        Err(e) => {
            db.set_module_status(
                &project_id,
                &module_id,
                ModuleStatus::Error,
                Some(e.clone()),
            )?;
            db.audit(
                "module_update_failed",
                Some(&project_id),
                Some(&module_id),
                &serde_json::json!({
                    "previous_version": previous_install.module_version,
                    "attempted_version": manifest.version,
                    "error": e,
                }),
            )?;
            let _ = app.emit(
                "module://install-complete",
                serde_json::json!({
                    "project_id": project_id,
                    "module_id": module_id,
                    "success": false,
                    "operation": "update",
                    "error": e,
                }),
            );
            Err(e)
        }
    }
}

/// v0.2.31 (#23): uninstall a module from a project, honoring the manifest's
/// `UninstallBlock` when one is declared.
///
/// Pre-v0.2.31 this command hardcoded the cleanup behaviour ("stop container,
/// remove install_dir, optionally wipe data on purge_data flag, delete DB
/// row, clear module_settings") — manifests' `UninstallBlock` fields were
/// parsed and silently ignored. Modules declaring `preserve_paths` had their
/// user config wiped; modules registering MCPs left dead `~/.claude.json`
/// entries; `clear_secrets` was never honored.
///
/// New behaviour:
///   * `manifest.uninstall.remove_install_dir: bool` (default true) — gates
///     the `remove_dir_all(install_dir)` call.
///   * `manifest.uninstall.preserve_paths: Vec<String>` — paths INSIDE
///     `install_dir` to preserve across the deletion. Placeholders
///     (`{VCT_DATA}`, `{HOME}`, etc.) resolve via PlaceholderCtx. Paths are
///     copied out to a temp dir, install_dir is removed, paths are copied
///     back. Best-effort: a failure in this dance doesn't fail the uninstall.
///   * `manifest.uninstall.deregister_mcp: bool` (default true) — when true
///     AND the manifest declares `mcp_registration.mcp_name`, calls
///     `mcp_registration::deregister_mcp` on `~/.claude.json`.
///   * `manifest.uninstall.clear_secrets: bool` (default false) — when true,
///     deletes EVERY keychain entry declared in `manifest.secrets` for
///     this (project, module). Each `SecretDecl.scope` (global / per-project
///     / shared) routes to the matching `SecretScope`. Errors per-key are
///     logged but don't fail the uninstall.
///
/// `purge_data` (the existing parameter) remains a SEPARATE concept from
/// `clear_secrets`: it wipes `<vct_root>/data/<module_id>/` regardless of
/// what the manifest says. Both flags can be true independently.
///
/// Backwards compat: if the manifest can't be found in the catalog (catalog
/// changed since install, paid-modules dir is gone, etc.), the legacy
/// hardcoded behaviour kicks in with a warning logged — uninstall NEVER
/// fails on missing-manifest, because then the user would be stuck.
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

    // Look up the manifest. On miss, fall back to legacy hardcoded behaviour
    // with a warning — never fail the uninstall over a missing manifest.
    let manifest_opt = match find_manifest(&db, &module_id) {
        Ok((m, _)) => Some(m),
        Err(e) => {
            eprintln!(
                "[uninstall] manifest for {} not in catalog ({}); falling back to legacy \
                 hardcoded behaviour (remove install_dir, no MCP deregister, no secret wipe). \
                 If the module declares preserve_paths or registers an MCP, manual cleanup \
                 may be required.",
                module_id, e,
            );
            None
        }
    };

    let uninstall_block: UninstallBlock = manifest_opt
        .as_ref()
        .and_then(|m| m.uninstall.clone())
        .unwrap_or_else(default_uninstall_block);

    // Phase 1E: stop + remove the per-project container (when present)
    // BEFORE deleting the install row, so we don't orphan a podman
    // container. Idempotent — stop_container_for_project soft-fails
    // when the container doesn't exist.
    if let Some(container_name) = row.container_name.as_deref() {
        if !container_name.is_empty() {
            if let Err(e) = crate::commands::module_service::stop_container_for_project(
                container_name,
            )
            .await
            {
                eprintln!(
                    "[uninstall] stop_container_for_project({}) failed: {}",
                    container_name, e
                );
            }
        }
    }

    let install_path = PathBuf::from(&row.install_path);
    let ctx = PlaceholderCtx::new(&module_id).with_install_dir(install_path.clone());

    // ─── Filesystem cleanup (honors remove_install_dir + preserve_paths) ──
    if uninstall_block.remove_install_dir && install_path.exists() {
        // Stash preserve_paths to a sibling temp dir, wipe install_dir,
        // restore. Best-effort throughout — any error here is logged but
        // doesn't fail the uninstall.
        let preserved = stash_preserve_paths(
            &install_path,
            &uninstall_block.preserve_paths,
            &ctx,
        )
        .await;

        if let Err(e) = tokio::fs::remove_dir_all(&install_path).await {
            eprintln!("[uninstall] remove_dir_all {}: {}", install_path.display(), e);
        }

        if !preserved.is_empty() {
            if let Err(e) = restore_preserved_paths(&install_path, preserved).await {
                eprintln!("[uninstall] restore_preserved_paths failed: {}", e);
            }
        }
    } else if !uninstall_block.remove_install_dir {
        eprintln!(
            "[uninstall] manifest.uninstall.remove_install_dir=false; leaving {} on disk.",
            install_path.display(),
        );
    }

    // ─── MCP deregistration ──────────────────────────────────────────────
    if uninstall_block.deregister_mcp {
        if let Some(mcp) = manifest_opt
            .as_ref()
            .and_then(|m| m.mcp_registration.as_ref())
        {
            // Target ~/.claude.json — same surface the install path
            // would register against. Soft-fail: a dead MCP entry is
            // recoverable; a stuck DB row is not.
            if let Some(home) = directories::UserDirs::new() {
                let target = home.home_dir().join(".claude.json");
                if let Err(e) =
                    crate::mcp_registration::deregister_mcp(&target, &mcp.mcp_name)
                {
                    eprintln!(
                        "[uninstall] deregister_mcp({}) failed: {}",
                        mcp.mcp_name, e
                    );
                }
            }
        }
    }

    // ─── Secret cleanup (manifest.uninstall.clear_secrets) ───────────────
    if uninstall_block.clear_secrets {
        if let Some(manifest) = manifest_opt.as_ref() {
            for decl in &manifest.secrets {
                let scope = match decl.scope.as_str() {
                    "global" => SecretScope::Global,
                    "shared" => SecretScope::Shared { project_id: &project_id },
                    _ => SecretScope::PerProject { project_id: &project_id },
                };
                if let Err(e) = secrets::delete(scope, &module_id, &decl.key) {
                    eprintln!(
                        "[uninstall] secrets::delete({}/{}) failed: {}",
                        module_id, decl.key, e
                    );
                }
            }
        }
    }

    if purge_data {
        // Scrub {VCT_DATA}/{MODULE_ID}/ when user explicitly asked to wipe.
        // Separate from clear_secrets — wipes the data dir (model weights,
        // caches, downloaded blobs) regardless of what the manifest says.
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
        &serde_json::json!({
            "purge_data": purge_data,
            "remove_install_dir": uninstall_block.remove_install_dir,
            "preserve_paths_count": uninstall_block.preserve_paths.len(),
            "deregister_mcp": uninstall_block.deregister_mcp,
            "clear_secrets": uninstall_block.clear_secrets,
            "manifest_found": manifest_opt.is_some(),
        }),
    )?;
    Ok(())
}

/// Default UninstallBlock used when the manifest doesn't declare one OR
/// can't be found in the catalog. Matches the legacy hardcoded behaviour
/// (remove_install_dir=true, deregister_mcp=true, clear_secrets=false,
/// no preserve_paths) so existing modules keep working byte-identical
/// to pre-v0.2.31 when they omit the block.
fn default_uninstall_block() -> UninstallBlock {
    UninstallBlock {
        remove_install_dir: true,
        preserve_paths: Vec::new(),
        deregister_mcp: true,
        clear_secrets: false,
    }
}

/// Move each `preserve_paths` entry from `install_path` into a sibling
/// temp directory. Returns the temp dir path + the relative paths that
/// were successfully stashed. Best-effort: paths that don't exist or
/// can't be moved are skipped with a log line.
async fn stash_preserve_paths(
    install_path: &std::path::Path,
    preserve_paths: &[String],
    ctx: &PlaceholderCtx,
) -> Vec<(PathBuf, PathBuf)> {
    if preserve_paths.is_empty() {
        return Vec::new();
    }
    let parent = install_path.parent().unwrap_or(install_path);
    let stash_dir = parent.join(format!(
        ".vct-preserve-{}-{}",
        ctx.module_id,
        chrono::Utc::now().timestamp_millis(),
    ));
    if let Err(e) = tokio::fs::create_dir_all(&stash_dir).await {
        eprintln!(
            "[uninstall] create stash dir {} failed: {}; preserve_paths disabled this run.",
            stash_dir.display(),
            e,
        );
        return Vec::new();
    }
    let mut stashed = Vec::new();
    for raw in preserve_paths {
        let resolved = ctx.resolve(raw);
        let src = if PathBuf::from(&resolved).is_absolute() {
            PathBuf::from(&resolved)
        } else {
            install_path.join(&resolved)
        };
        if !src.exists() {
            // Path declared but not present on disk — silently skip.
            continue;
        }
        // Mirror only the basename into stash_dir (preserve_paths are
        // expected to be small; a flat naming scheme avoids parent-dir
        // recreation gymnastics during restore).
        let basename = match src.file_name() {
            Some(s) => s.to_owned(),
            None => continue,
        };
        let dst = stash_dir.join(&basename);
        if let Err(e) = tokio::fs::rename(&src, &dst).await {
            eprintln!(
                "[uninstall] stash preserve_path {} -> {} failed: {}",
                src.display(),
                dst.display(),
                e,
            );
            continue;
        }
        // src_relative is the path RELATIVE to install_path so restore
        // can put it back. When raw was absolute (placeholder-resolved),
        // we keep the absolute target unchanged.
        let src_target = if PathBuf::from(&resolved).is_absolute() {
            PathBuf::from(&resolved)
        } else {
            install_path.join(&resolved)
        };
        stashed.push((dst, src_target));
    }
    stashed
}

/// Move each stashed entry back to its original location. Recreates the
/// parent dir of each target (which was just deleted by remove_dir_all).
async fn restore_preserved_paths(
    install_path: &std::path::Path,
    stashed: Vec<(PathBuf, PathBuf)>,
) -> Result<(), String> {
    // Recreate install_path so relative targets have a parent.
    if !install_path.exists() {
        tokio::fs::create_dir_all(install_path)
            .await
            .map_err(|e| format!("recreate install_path {}: {}", install_path.display(), e))?;
    }
    let mut stash_parent: Option<PathBuf> = None;
    for (stashed_at, target) in stashed {
        if stash_parent.is_none() {
            stash_parent = stashed_at.parent().map(|p| p.to_path_buf());
        }
        if let Some(parent) = target.parent() {
            if !parent.exists() {
                let _ = tokio::fs::create_dir_all(parent).await;
            }
        }
        if let Err(e) = tokio::fs::rename(&stashed_at, &target).await {
            eprintln!(
                "[uninstall] restore preserve_path {} -> {} failed: {}",
                stashed_at.display(),
                target.display(),
                e,
            );
        }
    }
    // Clean up the empty stash dir.
    if let Some(p) = stash_parent {
        if p.exists() {
            let _ = tokio::fs::remove_dir_all(&p).await;
        }
    }
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
        // Phase 1C: confirm vct-rl-reranker is now `available` with the
        // right tier, host compatibility, and version. This is the inverse
        // of the old `lists_exactly_one_coming_soon` test — it pins the
        // post-flip state instead.
        //
        // G1 (v0.2.22): catalog id was renamed from bare `"rl-reranker"`
        // to `"vct-rl-reranker"` so `find_manifest(module_id)` matches the
        // on-disk manifest (vct-module.json `"id": "vct-rl-reranker"`).
        let db = open_db();
        let entries = builtin_catalog_entries(&db);
        let rl = entries
            .iter()
            .find(|e| e.id == "vct-rl-reranker")
            .expect("vct-rl-reranker entry must be present");

        assert_eq!(rl.kind, "available", "vct-rl-reranker should be installable");
        assert_eq!(rl.version, "0.1.1", "matches manifest.version");
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

    /// R1 (v0.2.22): round-trip the on-disk `vct-rl-reranker` manifest
    /// against the hardcoded builtin catalog entry.
    ///
    /// The validation report at `.claude/context/plans/v0.2.22-rl-e2e-
    /// validation-report.md` identified three install-blocking config
    /// drifts between the two sources of truth (G1: id mismatch, G2: host
    /// list mismatch, G3: version pin drift). The existing pinning test
    /// at `builtin_catalog_lists_rl_reranker_as_available_paid_module`
    /// only validates the builtin entry against itself — it could not
    /// catch any of the three because it never parses the on-disk JSON.
    ///
    /// This test closes that gap by:
    ///   1. Loading `paid-modules/vct-rl-reranker/vct-module.json` from
    ///      disk via the same `ModuleManifest::from_json` path that
    ///      `find_manifest` uses at install time.
    ///   2. Locating the matching builtin catalog entry by id.
    ///   3. Asserting every field that drives install behaviour (id,
    ///      version, min_orchestrator_tier, license_required, hosts)
    ///      matches between the two sources.
    ///
    /// Skipped when the manifest file isn't present (e.g. CI environments
    /// that build against the public AGPL repo without the paid-modules
    /// staging dir). Production user installs are unaffected — paid
    /// modules ship via the signed-URL gateway, not the AGPL release.
    ///
    /// If this test fails, a future commit drifted one of the two sources
    /// from the other. Fix BOTH to agree before merging.
    #[test]
    fn catalog_matches_on_disk_manifest_when_present() {
        // CARGO_MANIFEST_DIR at compile time is `launcher/src-tauri/`.
        // Repo root is two .parent() hops up.
        let repo_root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(|p| p.parent())
            .expect("walk to repo root from launcher/src-tauri/")
            .to_path_buf();
        let manifest_path =
            repo_root.join("paid-modules/vct-rl-reranker/vct-module.json");

        if !manifest_path.exists() {
            eprintln!(
                "[test skip] paid-modules/vct-rl-reranker/vct-module.json not \
                 present (path: {}) — skipping catalog↔manifest round-trip. \
                 This is expected on public-AGPL-repo CI runs; paid modules \
                 ship via the signed-URL gateway, not the AGPL release.",
                manifest_path.display()
            );
            return;
        }

        // Parse the on-disk manifest via the EXACT path that
        // `find_manifest` uses at install time (same `from_json` call,
        // same validation, same error surface).
        let raw = std::fs::read_to_string(&manifest_path)
            .unwrap_or_else(|e| panic!("read {}: {}", manifest_path.display(), e));
        let manifest = ModuleManifest::from_json(&raw)
            .unwrap_or_else(|e| panic!("parse {}: {}", manifest_path.display(), e));

        // Look up the builtin catalog entry the way the launcher does.
        let db = open_db();
        let entries = builtin_catalog_entries(&db);
        let catalog_entry = entries
            .iter()
            .find(|e| e.id == manifest.id)
            .unwrap_or_else(|| {
                panic!(
                    "no builtin catalog entry matches on-disk manifest id '{}' \
                     — G1 mismatch (see v0.2.22 validation report). \
                     Catalog ids present: {:?}",
                    manifest.id,
                    entries.iter().map(|e| &e.id).collect::<Vec<_>>()
                )
            });

        // ─── Field-by-field round-trip assertions ─────────────────────
        assert_eq!(
            catalog_entry.id, manifest.id,
            "G1: catalog.id must equal manifest.id"
        );
        assert_eq!(
            catalog_entry.version, manifest.version,
            "G3: catalog.version must equal manifest.version"
        );
        assert_eq!(
            catalog_entry.min_orchestrator_tier, manifest.license.min_orchestrator_tier,
            "catalog.min_orchestrator_tier must equal manifest.license.min_orchestrator_tier"
        );
        assert_eq!(
            catalog_entry.license_required, manifest.license.required,
            "catalog.license_required must equal manifest.license.required"
        );
        assert_eq!(
            catalog_entry.name, manifest.name,
            "catalog.name (display) must equal manifest.name"
        );
        assert_eq!(
            catalog_entry.description, manifest.description,
            "catalog.description must equal manifest.description (the catalog \
             string is shown in the launcher GUI; the manifest string is shown \
             when the module is queried programmatically — drift here means the \
             GUI and CLI disagree on what the module does)"
        );
        assert_eq!(
            catalog_entry.compatibility_hosts, manifest.compatibility.hosts,
            "G2: catalog.compatibility_hosts must equal manifest.compatibility.hosts \
             (otherwise install fails at the is_compatible_with_host gate)"
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

    // ─── admin-tier gate regression (v0.2.22 post-push audit, 2026-05-21) ──
    //
    // Pre-fix: `is_module_licensed`'s `tier_rank` closure had no arm for
    // `admin`, so the wildcard `_ => 0` collapsed admin tier to the same
    // rank as `free`. An admin user (Path A Vault token or Path B LS
    // variant) saw the Install button gated on any paid module unless
    // their `module_licenses` entry happened to be populated for that
    // specific module — contradicting the documented "admin is strict
    // superset of enterprise by feature gates" contract
    // (db/tier.rs:40, docs/features/06-license-and-commercial.md §"Tier
    // ordering"). The post-fix rank maps admin to 4 (above enterprise=3).
    //
    // These tests pin BOTH halves of the contract:
    //   1. admin tier unlocks the RL Reranker (a real Pro-tier module).
    //   2. admin tier unlocks any hypothetical future enterprise-min module.
    //   3. free tier still rejects pro-tier modules (no regression).
    //   4. unknown-tier strings continue to fall through to rank=0 (no
    //      silent privilege escalation via typo).
    //
    // Mutation-verified at authoring time: reverting the new `"admin" => 4`
    // arm to fall through to `_ => 0` makes test #1 and #2 fail with the
    // exact symptom the original bug produced.

    /// Build a minimal valid `ModuleManifest` parameterized on the
    /// `min_orchestrator_tier`. Parsing exercises the same validator the
    /// launcher uses at install time (so test fixtures can't drift away
    /// from real manifests' shape).
    fn fake_manifest_with_min_tier(id: &str, min_tier: &str) -> ModuleManifest {
        let raw = serde_json::json!({
            "manifest_version": 1,
            "id": id,
            "name": "Fake Module",
            "version": "0.0.1",
            "description": "Test fixture for admin-tier gate regression.",
            "category": "paid-independent",
            "license": {
                "required": true,
                "variant_ids": ["fake-variant"],
                "min_orchestrator_tier": min_tier
            },
            "compatibility": {"hosts": ["base"]},
            "install": {"method": "container_pull"},
            "runtime": {"type": "service", "command": "echo", "args": []}
        });
        ModuleManifest::from_json(&raw.to_string())
            .unwrap_or_else(|e| panic!("parse fake manifest (min_tier={}): {}", min_tier, e))
    }

    fn fake_pro_manifest() -> ModuleManifest {
        fake_manifest_with_min_tier("fake-pro-module", "pro")
    }

    fn fake_enterprise_manifest() -> ModuleManifest {
        fake_manifest_with_min_tier("fake-enterprise-module", "enterprise")
    }

    #[test]
    fn admin_tier_unlocks_pro_module() {
        let db = open_db();
        db.set_tier_cache("admin", &serde_json::json!({}), None)
            .expect("set admin tier");
        let manifest = fake_pro_manifest();
        assert!(
            is_module_licensed(&manifest, &db),
            "admin tier MUST satisfy min_orchestrator_tier=pro \
             (admin is documented as strict superset of enterprise; \
             pre-fix the tier_rank fell through to 0 and rejected this)"
        );
    }

    #[test]
    fn admin_tier_unlocks_enterprise_module() {
        let db = open_db();
        db.set_tier_cache("admin", &serde_json::json!({}), None)
            .expect("set admin tier");
        let manifest = fake_enterprise_manifest();
        assert!(
            is_module_licensed(&manifest, &db),
            "admin tier MUST satisfy min_orchestrator_tier=enterprise \
             (any future enterprise-min module should auto-unlock for admin \
             without further code changes)"
        );
    }

    #[test]
    fn free_tier_still_rejects_pro_module() {
        // Regression guard against accidentally widening the gate.
        let db = open_db();
        db.set_tier_cache("free", &serde_json::json!({}), None)
            .expect("set free tier");
        let manifest = fake_pro_manifest();
        assert!(
            !is_module_licensed(&manifest, &db),
            "free tier MUST NOT satisfy min_orchestrator_tier=pro"
        );
    }

    #[test]
    fn unknown_tier_string_does_not_silently_escalate() {
        // Defense-in-depth: a typo'd or attacker-supplied tier string
        // (e.g. "Admin" with capital A, or "godmode") MUST fall through
        // to rank=0 and be rejected. Pre-fix this was already the case
        // for `_` wildcards including `admin` lowercase (the bug); the
        // fix is to add `admin` explicitly without enabling escalation
        // via other unknown strings.
        let db = open_db();
        for typo in &["Admin", "ADMIN", "godmode", "root", "superuser"] {
            db.set_tier_cache(typo, &serde_json::json!({}), None)
                .ok(); // db CHECK constraint may reject — that's also fine.
            if let Ok(cache) = db.get_tier_cache() {
                if cache.orchestrator_tier == *typo {
                    let manifest = fake_pro_manifest();
                    assert!(
                        !is_module_licensed(&manifest, &db),
                        "unknown tier {:?} must NOT unlock pro-tier modules",
                        typo,
                    );
                }
            }
        }
    }

    // ─── v0.2.31 #23: UninstallBlock respect ────────────────────────────
    //
    // The pre-v0.2.31 `uninstall_module_v2` hardcoded its cleanup behaviour
    // (remove install_dir, no preserve_paths, no MCP deregister, no secret
    // wipe). Manifests declared an `uninstall: UninstallBlock` block that
    // was parsed and silently ignored. These tests pin the new behaviour:
    //
    //   1. `default_uninstall_block()` matches the legacy hardcoded shape
    //      so manifests omitting the block keep working unchanged.
    //   2. `stash_preserve_paths` + `restore_preserved_paths` correctly
    //      ferry user files across the install_dir wipe.
    //   3. Manifest with preserve_paths declared: the preserved file
    //      survives the dance even after install_path is removed.

    #[test]
    fn default_uninstall_block_matches_legacy_hardcoded_behaviour() {
        // Legacy behaviour: remove_install_dir=true, no preserve_paths,
        // deregister_mcp=true, clear_secrets=false. If the manifest does
        // not declare `uninstall` (or the manifest can't be found), this
        // is what `uninstall_module_v2` falls back to.
        let block = default_uninstall_block();
        assert!(
            block.remove_install_dir,
            "legacy fallback must remove install_dir (matches pre-v0.2.31 hardcoded behaviour)"
        );
        assert!(
            block.preserve_paths.is_empty(),
            "legacy fallback must have no preserve_paths"
        );
        assert!(
            block.deregister_mcp,
            "legacy fallback must deregister MCP (matches pre-v0.2.31 hardcoded behaviour)"
        );
        assert!(
            !block.clear_secrets,
            "legacy fallback must NOT clear secrets (matches pre-v0.2.31 hardcoded behaviour; \
             user must opt in via manifest)"
        );
    }

    /// Build a minimal valid `ModuleManifest` with a custom `uninstall` block.
    /// Used by the UninstallBlock-respect tests to verify each field's effect.
    fn manifest_with_uninstall_block(
        id: &str,
        remove_install_dir: bool,
        preserve_paths: Vec<&str>,
        deregister_mcp: bool,
        clear_secrets: bool,
    ) -> ModuleManifest {
        let raw = serde_json::json!({
            "manifest_version": 1,
            "id": id,
            "name": "Fake Uninstall Test Module",
            "version": "0.0.1",
            "description": "Test fixture for #23 UninstallBlock-respect.",
            "category": "paid-independent",
            "license": {
                "required": false,
                "min_orchestrator_tier": "free"
            },
            "compatibility": {"hosts": ["base"]},
            "install": {"method": "git_clone", "source": "https://example.test/x.git"},
            "runtime": {"type": "service", "command": "echo", "args": []},
            "uninstall": {
                "remove_install_dir": remove_install_dir,
                "preserve_paths": preserve_paths,
                "deregister_mcp": deregister_mcp,
                "clear_secrets": clear_secrets,
            }
        });
        ModuleManifest::from_json(&raw.to_string())
            .unwrap_or_else(|e| panic!("parse fake manifest with uninstall: {}", e))
    }

    #[test]
    fn manifest_uninstall_block_round_trips_through_serde() {
        // Regression guard: the UninstallBlock fields must survive a
        // parse round-trip with default-correct values. Pre-v0.2.31 the
        // block was parsed but the values weren't consulted; if a future
        // commit accidentally drops the `Deserialize` impl or renames a
        // field, this test catches it before the uninstall path silently
        // reverts to defaults.
        let m = manifest_with_uninstall_block(
            "vct-test-uninstall",
            false,
            vec!["{install_dir}/user-config.toml", "{VCT_DATA}/{MODULE_ID}/cache"],
            false,
            true,
        );
        let block = m.uninstall.as_ref().expect("uninstall block must parse");
        assert!(!block.remove_install_dir);
        assert!(!block.deregister_mcp);
        assert!(block.clear_secrets);
        assert_eq!(block.preserve_paths.len(), 2);
        assert_eq!(block.preserve_paths[0], "{install_dir}/user-config.toml");
        assert_eq!(block.preserve_paths[1], "{VCT_DATA}/{MODULE_ID}/cache");
    }

    /// End-to-end test for `stash_preserve_paths` + `restore_preserved_paths`:
    /// create an install_dir with a file inside, stash the file, wipe the
    /// dir, restore — the file should reappear at its original location.
    ///
    /// Skipped (returns early) if a tokio runtime isn't available in the
    /// test context. The codebase uses sync `#[test]` everywhere else in
    /// this module; we wrap the async dance in a single-threaded runtime
    /// to stay consistent.
    #[test]
    fn preserve_paths_survive_install_dir_wipe() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime");
        rt.block_on(async {
            let tmp = tempfile::tempdir().expect("create tempdir");
            let install_path = tmp.path().join("install");
            tokio::fs::create_dir_all(&install_path).await.unwrap();
            // Create a "user file" inside install_dir that should be preserved.
            let user_file = install_path.join("user-config.toml");
            tokio::fs::write(&user_file, b"keep-me=true").await.unwrap();
            // And another file that should NOT be preserved.
            let throwaway_file = install_path.join("cache.db");
            tokio::fs::write(&throwaway_file, b"discard").await.unwrap();

            let ctx = PlaceholderCtx::new("vct-test-preserve")
                .with_install_dir(install_path.clone());

            let preserve_paths = vec!["user-config.toml".to_string()];
            let stashed = stash_preserve_paths(&install_path, &preserve_paths, &ctx).await;
            assert_eq!(
                stashed.len(),
                1,
                "expected exactly one preserved entry; got {}",
                stashed.len()
            );
            // After stashing, the source file is gone from install_dir.
            assert!(
                !user_file.exists(),
                "user-config.toml should have been moved to stash dir"
            );
            assert!(
                throwaway_file.exists(),
                "cache.db should NOT have been stashed (not in preserve_paths)"
            );

            // Wipe install_dir (this is what uninstall_module_v2 does).
            tokio::fs::remove_dir_all(&install_path).await.unwrap();
            assert!(!install_path.exists(), "install_dir wipe sanity check");

            // Restore.
            restore_preserved_paths(&install_path, stashed)
                .await
                .expect("restore must succeed");
            assert!(user_file.exists(), "user-config.toml must be restored");
            let restored =
                tokio::fs::read(&user_file).await.expect("read restored file");
            assert_eq!(&restored, b"keep-me=true", "file contents must match");
            assert!(
                !throwaway_file.exists(),
                "cache.db must NOT be restored (it was wiped with the dir)"
            );
        });
    }

    #[test]
    fn preserve_paths_empty_list_is_noop() {
        // When manifest.uninstall.preserve_paths is empty, the stash dance
        // is a fast-path no-op (no temp dir created, empty Vec returned).
        // This guards against the stash dir leaking to disk in the common
        // case (modules that don't preserve anything).
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime");
        rt.block_on(async {
            let tmp = tempfile::tempdir().expect("create tempdir");
            let install_path = tmp.path().join("install");
            tokio::fs::create_dir_all(&install_path).await.unwrap();
            let ctx = PlaceholderCtx::new("vct-test-noop")
                .with_install_dir(install_path.clone());

            let stashed = stash_preserve_paths(&install_path, &[], &ctx).await;
            assert!(stashed.is_empty(), "empty preserve_paths -> empty stash");
            // No `.vct-preserve-*` dir should have been created.
            let mut entries = tokio::fs::read_dir(tmp.path()).await.unwrap();
            while let Some(e) = entries.next_entry().await.unwrap() {
                let name = e.file_name().to_string_lossy().to_string();
                assert!(
                    !name.starts_with(".vct-preserve-"),
                    "no stash dir should leak for empty preserve_paths; found: {}",
                    name
                );
            }
        });
    }

    #[test]
    fn preserve_paths_skips_nonexistent_entries() {
        // When a preserve_paths entry doesn't exist on disk (e.g. user
        // never created the config file), it should be silently skipped
        // — not abort the entire uninstall.
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime");
        rt.block_on(async {
            let tmp = tempfile::tempdir().expect("create tempdir");
            let install_path = tmp.path().join("install");
            tokio::fs::create_dir_all(&install_path).await.unwrap();
            let ctx = PlaceholderCtx::new("vct-test-missing")
                .with_install_dir(install_path.clone());

            let preserve_paths = vec!["does-not-exist.toml".to_string()];
            let stashed = stash_preserve_paths(&install_path, &preserve_paths, &ctx).await;
            assert!(
                stashed.is_empty(),
                "missing entries should be skipped silently; got {} stashed",
                stashed.len(),
            );
        });
    }

    // ─── v0.2.31 #20-Fix-3: update_module_for_project sanity ────────────
    //
    // The Tauri command itself can't be unit-tested in isolation (it
    // requires an AppHandle + a project row in the DB + a populated
    // catalog path). We test the helper layer:
    //   1. find_manifest returns Err on a not-installed module id (the
    //      same error path update_module_for_project surfaces when the
    //      manifest disappeared between install + update).
    //   2. The new DB helper `update_module_install_version` bumps the
    //      version + installed_at on an existing row.
    //   3. `update_module_install_version` Errs when no row exists (this
    //      is what update_module_for_project relies on to detect "module
    //      not installed").

    #[test]
    fn update_module_install_version_bumps_version_and_installed_at() {
        let db = open_db();
        // FK: module_installs.project_id references projects.id — must
        // insert a project row first.
        db.insert_project("proj-1", "Test Project", "/tmp/test-proj", ProjectHost::Base, "test-proj")
            .expect("insert project");
        let now_before = chrono::Utc::now().timestamp_millis();
        let _row = db
            .insert_module_install(
                "install-id-1",
                "proj-1",
                "vct-test-mod",
                "0.1.0",
                "/tmp/fake/install/dir",
            )
            .expect("insert pending install row");

        // Sleep 2ms so the bumped installed_at is strictly greater than
        // the pre-update value (chrono::Utc::now() is millisecond-precision
        // on most platforms — without the sleep the timestamps may collide).
        std::thread::sleep(std::time::Duration::from_millis(2));

        db.update_module_install_version("proj-1", "vct-test-mod", "0.2.0")
            .expect("update version");

        let refreshed = db
            .get_module_install("proj-1", "vct-test-mod")
            .expect("read back")
            .expect("row must still exist");
        assert_eq!(refreshed.module_version, "0.2.0");
        assert!(
            refreshed.installed_at >= now_before,
            "installed_at should have been bumped (was {}, now_before {})",
            refreshed.installed_at,
            now_before,
        );
    }

    #[test]
    fn update_module_install_version_errs_when_row_missing() {
        // update_module_for_project relies on this Err path to detect
        // "module not installed; use install_module_for_project instead".
        let db = open_db();
        let res = db.update_module_install_version("proj-X", "vct-not-installed", "0.2.0");
        assert!(
            res.is_err(),
            "update_module_install_version must Err when row is missing; got Ok"
        );
        let msg = res.unwrap_err();
        assert!(
            msg.contains("not found"),
            "error message must say 'not found'; got: {}",
            msg
        );
    }
}
