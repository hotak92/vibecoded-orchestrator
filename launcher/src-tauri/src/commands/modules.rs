//! Module installation + lifecycle commands.
//!
//! Orchestrates: catalog lookup, license gating, manifest parse, installer
//! engine invocation, DB row writes, event emission.
//!
//! v0.2.33 (Agent B, L0a refactor): the catalog data flow was inverted.
//! Pre-v0.2.33 `list_module_catalog_impl` scanned `paid-modules/*/` on
//! disk for catalog metadata, which failed for real-user installs that
//! never had the manifest. Now: catalog metadata comes from the public
//! L0 endpoint (`module_catalog_client::cached_module_catalog`); on-disk
//! manifests at `~/.vct/modules/<id>/vct-module.json` (written by
//! Agent C's post-install extract) are read ONLY for installed
//! modules' dispatcher data (config_tab, runtime, db). The dev
//! affordance (scanning `<install_root>/paid-modules/`) remains
//! available but gated behind `VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH=1`.
//!
//! `find_manifest` was split into `resolve_install_metadata` (L0-driven,
//! used at install time before the image is pulled) and
//! `find_installed_manifest` (on-disk, used at dispatch/update/uninstall
//! time after the image has been pulled + manifest extracted).

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use tauri::{command, AppHandle, Emitter, State};
use uuid::Uuid;

use crate::commands::installed_modules::{
    dev_catalog_passthrough_enabled, dev_paid_modules_paths, paid_modules_dir_exists,
};
use crate::commands::module_catalog_client::{L0CatalogModule, L0CatalogResponse};
use crate::db::models::{ModuleInstallRow, ModuleStatus, ProjectHost};
use crate::db::Db;
use crate::installer_engine;
use crate::manifest::{ModuleManifest, PlaceholderCtx, UninstallBlock};
use crate::secrets::{self, SecretScope};

/// `app_state` key recording whether the user has dismissed the
/// "Found dev paid-modules" hint. Set to `"true"` after dismissal;
/// absent or `"false"` means the hint is still active.
pub const APP_STATE_KEY_DEV_AFFORDANCE_DISMISSED: &str =
    "module_catalog.dev_affordance_dismissed";

// ─── Catalog entry surface ──────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
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
    ///   - "bundled"          = bundled with the launcher itself, always installed,
    ///                          cannot be uninstalled (e.g. the launcher).
    ///   - "available"        = catalog-listed, not installed yet, has Install action.
    ///   - "installed"        = installed, can be reconfigured / uninstalled.
    ///   - "update_available" = installed AND L0 advertises a newer version.
    ///                          Renders the "Update" action. v0.2.33+.
    ///   - "broken"           = `module_installs.status='broken'` (reconciler
    ///                          marked the on-disk manifest missing). Renders
    ///                          a Reinstall CTA. v0.2.33+.
    ///   - "subcomponent"     = ships with a parent module, no separate install,
    ///                          offers a Dashboard CTA.
    ///   - "coming_soon"      = announced, not yet shipped. Rendered with a
    ///                          "Coming Soon" badge + Learn-more CTA, no Install.
    ///                          Reserved for items with a public roadmap commitment;
    ///                          do NOT use for vapor.
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
    /// v0.2.33: when the underlying `module_installs` row is present but
    /// no longer matches an L0 entry, the launcher renders an
    /// "No longer available in catalog" warning badge. Empty string
    /// for the common case (entry IS in L0, or entry is a builtin).
    #[serde(default)]
    pub catalog_warning: String,
    /// NEW-3 (2026-05-28): the module's `runtime.type` as declared in its
    /// manifest. Populated from `from_manifest`; empty string for L0-only
    /// entries and builtins. The Svelte tile uses this to decide whether to
    /// render a "Start" button when `container_name = NULL`.
    #[serde(default)]
    pub runtime_type: String,
    /// v0.2.49 Stream A integration (Bug D / Path 1, coordinated via
    /// vct-coordination msg 177 from main chat 2026-06-07).
    ///
    /// Exposes the manifest's `install.scope` field as a string so the
    /// Svelte tile renderer can branch the per-project badge variants
    /// (Bug D from V3 handoff). Values:
    ///
    ///   - `"per_project"` (default) — legacy install model: one install
    ///     row + one container per project. Tile shows per-project status
    ///     ("installed in THIS project" / "available", etc.).
    ///   - `"global"` — v0.2.49+ model: one install row machine-wide
    ///     (`project_id IS NULL`), one container with bare module-id name,
    ///     per-project enable toggle via Stream B's `module_settings`
    ///     entry. Tile shows "installed (available in any project)"
    ///     when no per-project enable row exists OR shows the
    ///     enable-toggle directly.
    ///
    /// Source: `manifest.install.scope` for `from_manifest` entries.
    /// L0 entries: populated from `l0.install.scope` when present;
    /// defaults to `"per_project"` for pre-v0.2.49 L0 catalogs that
    /// don't carry the field (the `#[serde(default)]` on
    /// `L0Install.scope` keeps those valid).
    /// Builtins (launcher, orchestrator, subcomponents): always
    /// `"per_project"` — those are conceptually per-workspace.
    #[serde(default)]
    pub install_scope: String,
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
            catalog_warning: String::new(),
            // NEW-3 (2026-05-28): expose runtime type so the tile can gate
            // the "Start" button on container/service modules.
            runtime_type: m.runtime.r#type.clone(),
            // v0.2.49 Stream A integration: expose install.scope so the
            // Svelte tile can render per-project badge variants
            // correctly for global-scope modules (Bug D).
            install_scope: m.install.scope.as_str().to_string(),
        }
    }

    /// v0.2.33: build a catalog entry from an L0 record, the resolved
    /// licensed-state, and the resolved `kind`. The L0 record is the
    /// authoritative source for catalog-display fields; the caller
    /// determines `kind` by looking at `module_installs`.
    fn from_l0(
        l0: &L0CatalogModule,
        is_licensed: bool,
        kind: &str,
        version_override: Option<&str>,
    ) -> Self {
        Self {
            id: l0.id.clone(),
            name: l0.name.clone(),
            // For an `installed` or `update_available` tile we want to
            // report the version the user actually has; L0 reports the
            // latest published version. The caller passes
            // `version_override = Some(installed_version)` for those
            // two kinds.
            version: version_override.unwrap_or(&l0.version).to_string(),
            description: l0.description.clone(),
            category: l0.category.clone(),
            tags: l0.tags.clone(),
            license_required: l0.license_required,
            license_variant_ids: l0.license_variant_ids.clone(),
            min_orchestrator_tier: l0.min_orchestrator_tier.clone(),
            compatibility_hosts: l0.compatibility.hosts.clone(),
            is_licensed,
            manifest_source: format!("L0:{}", l0.id),
            kind: kind.into(),
            parent_id: String::new(),
            cta_route: String::new(),
            coming_soon_tier: String::new(),
            coming_soon_target: String::new(),
            deprecated: l0.deprecated,
            deprecation_message: l0.deprecation_message.clone(),
            deprecation_eol_date: l0.deprecation_eol_date.clone(),
            deprecation_migration_url: l0.deprecation_migration_url.clone(),
            catalog_warning: String::new(),
            // L0 catalog records don't carry runtime metadata; the
            // installed manifest path fills this in when available.
            runtime_type: String::new(),
            // v0.2.49 Stream A integration: L0Install carries an
            // optional `scope` field (added in lockstep with the
            // manifest-side InstallScope). Default "per_project"
            // preserves pre-v0.2.49 L0 catalogs that don't carry
            // the field at all.
            install_scope: l0.install.scope.as_str().to_string(),
        }
    }
}

// ─── L0-driven catalog response envelope ───────────────────────────────
//
// v0.2.33: the Tauri command shape changed from `Vec<ModuleCatalogEntry>`
// to `CatalogResponse { modules, l0_status, parse_errors,
// dev_affordance_hint }`. The front-end deconstructs `.modules` for the
// existing render path and `.l0_status` / `.parse_errors` /
// `.dev_affordance_hint` for the new banners/toasts (Agent E renders).

/// What the launcher tells the renderer about the L0 catalog fetch.
/// Agent E uses this to render the "Couldn't reach catalog" banner +
/// the stale-cache "Cached X minutes ago" indicator.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum L0Status {
    /// Fresh (or recently-fetched-within-TTL) L0 envelope succeeded.
    Ok {
        fetched_at: String,
        modules_count: usize,
    },
    /// L0 fetch failed but a cached value (possibly stale) is being
    /// served. The renderer should display a quiet "Catalog cached X
    /// ago" indicator + retry CTA.
    Stale {
        cached_fetched_at: String,
        last_error: String,
    },
    /// L0 fetch failed AND there is no cached value to fall back on.
    /// Catalog renders builtins only + a louder "Couldn't reach catalog
    /// server" banner.
    Unavailable { error: String },
}

/// A single failure in the catalog-build pipeline that the user should
/// be told about. Two sources today:
///   * `source: "L0:<endpoint>"` — the L0 envelope was malformed.
///   * `source: "<file path>"` — an on-disk `vct-module.json` failed
///     to parse via `ModuleManifest::from_json`.
///
/// Agent E surfaces these as the "1 module manifest couldn't be parsed"
/// banner.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ManifestParseError {
    pub module_id: String,
    pub source: String,
    pub error: String,
}

/// One-shot hint to the dev who has a `<install_root>/paid-modules/`
/// directory but hasn't opted into the dev passthrough. Surfaces as
/// the toast: "Found dev paid-modules at <path>. Set
/// VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH=1 to enable them."
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DevAffordanceHint {
    pub paid_modules_path: String,
    pub env_var_name: String,
}

/// The full v0.2.33 catalog response. Replaces the v0.2.32-era bare
/// `Vec<ModuleCatalogEntry>` signature.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CatalogResponse {
    pub modules: Vec<ModuleCatalogEntry>,
    pub l0_status: L0Status,
    pub parse_errors: Vec<ManifestParseError>,
    /// `Some(_)` exactly when `paid-modules/` exists at the install
    /// root AND `VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH` is unset AND
    /// the user hasn't dismissed the hint. Otherwise `None`.
    pub dev_affordance_hint: Option<DevAffordanceHint>,
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
        catalog_warning: String::new(),
        install_scope: "per_project".into(),
        runtime_type: String::new(),
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
        catalog_warning: String::new(),
        runtime_type: String::new(),
        install_scope: "per_project".into(),
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
            catalog_warning: String::new(),
            runtime_type: String::new(),
            install_scope: "per_project".into(),
        });
    }

    // v0.2.33 (Agent B, L0a): the hardcoded vct-rl-reranker placeholder
    // entry was removed. The launcher no longer ships any paid-module
    // catalog metadata baked into the binary — paid modules come from
    // L0 (`module_catalog_client::cached_module_catalog`). The 4 real
    // builtins above (launcher, orchestrator, knowledge-graph,
    // code-graph) ARE the launcher (they don't live behind L0 because
    // they aren't dynamically published; their version comes from
    // CARGO_PKG_VERSION / repo-root `vct-module.json`).

    out
}

// ─── License gate ───────────────────────────────────────────────────────

/// v0.2.33 (Agent B, L0a): the license-relevant projection of a
/// module's gate fields, shared by the legacy `is_module_licensed`
/// (called with a full `ModuleManifest`) and the new L0-driven catalog
/// builder (called with `L0CatalogModule` data). Keeping this in one
/// struct prevents drift between the two paths.
pub struct LicenseGateInput<'a> {
    pub module_id: &'a str,
    pub required: bool,
    pub min_orchestrator_tier: &'a str,
    pub variant_ids: &'a [String],
}

/// Tier-ordering ladder. Admin maps to 4 (strict superset of enterprise)
/// per docs/features/06-license-and-commercial.md §"Tier ordering" +
/// db/tier.rs:40. Unknown strings fall through to rank=0 so attacker-
/// supplied tier values can't silently escalate.
fn tier_rank(t: &str) -> u32 {
    match t {
        "free" => 0,
        "pro" => 1,
        "mao" => 2,
        "enterprise" => 3,
        "admin" => 4,
        _ => 0,
    }
}

/// v0.2.33 license-gate that consumes the narrow `LicenseGateInput`
/// shape. The legacy `is_module_licensed(&ModuleManifest, &Db)`
/// shim below calls this with manifest-derived fields so existing
/// callers keep working unchanged.
pub fn is_module_licensed_v2(input: LicenseGateInput, db: &Db) -> bool {
    if !input.required {
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
    // by feature gates. v0.2.22 fixed the admin-rank gap (was falling
    // through to rank=0); v0.2.33 just refactored the input shape.
    if tier_rank(&cache.orchestrator_tier) >= tier_rank(input.min_orchestrator_tier)
        && input.min_orchestrator_tier != "free"
    {
        return true;
    }
    // 2. Module-specific license?
    if let Some(entry) = cache.module_licenses.get(input.module_id) {
        if entry.get("tier").is_some() {
            return true;
        }
    }
    // 3. No gate matched. Treat as free if no variants declared (older
    //    manifests omit the field; we default to permissive there).
    input.variant_ids.is_empty()
}

/// Legacy thin-shim — v0.2.22 callers that have a full `ModuleManifest`
/// keep working unchanged. Internally delegates to
/// `is_module_licensed_v2` with manifest-derived fields.
pub(crate) fn is_module_licensed(manifest: &ModuleManifest, db: &Db) -> bool {
    is_module_licensed_v2(
        LicenseGateInput {
            module_id: &manifest.id,
            required: manifest.license.required,
            min_orchestrator_tier: &manifest.license.min_orchestrator_tier,
            variant_ids: &manifest.license.variant_ids,
        },
        db,
    )
}

// ─── Catalog discovery (L0-driven, v0.2.33) ───────────────────────────

/// The Tauri command surface. Reads the L0 catalog (cached 15min) via
/// `cached_module_catalog` and merges with builtin entries + installed-
/// state from `module_installs`. The renderer expects the new
/// `CatalogResponse` shape — see the struct doc.
#[command]
pub async fn list_module_catalog(db: State<'_, Db>) -> Result<CatalogResponse, String> {
    // The async L0 fetch can't run inside the sync `_impl` helper, so
    // we drive it here and pass the result into the impl. Tests use
    // `list_module_catalog_impl_with_l0` directly with a synthetic
    // envelope; production uses `cached_module_catalog`.
    let l0_outcome = crate::commands::module_catalog_client::cached_module_catalog(&db).await;
    Ok(list_module_catalog_impl_with_l0(&db, l0_outcome))
}

/// Test-friendly synchronous variant. Takes an already-resolved L0
/// outcome (Ok or Err) and produces the full `CatalogResponse`. The
/// `#[command]` shell above resolves the outcome via the real
/// `cached_module_catalog`; unit tests can pass synthetic envelopes
/// (including the `Err` branch) without standing up an HTTP mock.
pub(crate) fn list_module_catalog_impl_with_l0(
    db: &Db,
    l0_outcome: Result<L0CatalogResponse, String>,
) -> CatalogResponse {
    let mut modules = builtin_catalog_entries(db);
    let mut parse_errors: Vec<ManifestParseError> = Vec::new();
    let mut l0_modules: Vec<L0CatalogModule> = Vec::new();
    let l0_status: L0Status;

    match l0_outcome {
        Ok(envelope) => {
            // Capture status BEFORE moving `envelope.modules` out.
            l0_status = L0Status::Ok {
                fetched_at: envelope.fetched_at.clone(),
                modules_count: envelope.modules.len(),
            };
            l0_modules = envelope.modules;
        }
        Err(fetch_err) => {
            // Best-effort: if a stale cache exists, the client already
            // returned it as Ok — we only land here when there's
            // truly no data. Surface the failure to the renderer.
            l0_status = L0Status::Unavailable { error: fetch_err };
        }
    }

    // Walk every L0 entry, merge with installed-state.
    for l0 in &l0_modules {
        let is_licensed = is_module_licensed_v2(
            LicenseGateInput {
                module_id: &l0.id,
                required: l0.license_required,
                min_orchestrator_tier: &l0.min_orchestrator_tier,
                variant_ids: &l0.license_variant_ids,
            },
            db,
        );

        // Look at module_installs for any project-keyed install of this
        // module_id. We use `list_module_installs_with_status` for each
        // candidate status — small constant number of queries.
        let install_state = lookup_install_state(db, &l0.id);

        let (kind, version_override): (&str, Option<&str>) = match &install_state {
            InstallState::None => ("available", None),
            InstallState::Installed { version } => {
                if version == &l0.version {
                    ("installed", Some(version.as_str()))
                } else if semver_less(version, &l0.version) {
                    // L0 is newer than installed → update available.
                    ("update_available", Some(version.as_str()))
                } else {
                    // Installed is newer than (or equal to a previous-
                    // rolled-back) L0. Silent per review §J4-d.
                    ("installed", Some(version.as_str()))
                }
            }
            InstallState::Broken { version } => ("broken", Some(version.as_str())),
            InstallState::Pending { status, version } => {
                // Pending / installing / running / stopped / error all
                // surface as their own kind string; the catalog tile
                // just renders the badge. Treat anything non-`broken`
                // and non-`installed` as the raw status name so the
                // UI can decide.
                (status.as_str(), Some(version.as_str()))
            }
        };

        modules.push(ModuleCatalogEntry::from_l0(l0, is_licensed, kind, version_override));
    }

    // Walk installed rows for any module_id NOT present in L0 (deprecated /
    // withdrawn). Render as kind=installed with the catalog warning.
    let l0_ids: std::collections::HashSet<&str> =
        l0_modules.iter().map(|m| m.id.as_str()).collect();
    for missing in installed_module_ids_not_in_set(db, &l0_ids) {
        // Skip ids that are already present in `modules` (builtins
        // bear the same id as a hypothetical paid module — defensive
        // dedupe; in practice this doesn't trigger because builtin
        // ids and paid ids are disjoint).
        if modules.iter().any(|e| e.id == missing.module_id) {
            continue;
        }
        // Try to read the on-disk manifest for display fields; fall
        // back to a synthetic entry if it's missing or malformed.
        let (entry, maybe_err) = installed_only_entry(db, &missing);
        if let Some(err) = maybe_err {
            parse_errors.push(err);
        }
        modules.push(entry);
    }

    // Dev-affordance: merge `<install_root>/paid-modules/*` manifests on
    // top of the L0 results so a module author working on the next
    // version locally sees their in-progress manifest in the catalog
    // tile. Only runs when VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH=1.
    // Precedence: dev manifest WINS over L0 for the same module_id
    // (overrides version + description, etc.) because the dev is
    // explicitly opted in.
    for path in dev_paid_modules_paths(db) {
        match read_and_parse_manifest(&path) {
            Ok(manifest) => {
                let licensed = is_module_licensed(&manifest, db);
                let live = ModuleCatalogEntry::from_manifest(
                    &manifest,
                    licensed,
                    path.display().to_string(),
                );
                if let Some(existing_idx) = modules.iter().position(|e| e.id == manifest.id) {
                    let preserved_kind = modules[existing_idx].kind.clone();
                    let preserved_parent_id = modules[existing_idx].parent_id.clone();
                    let preserved_cta_route = modules[existing_idx].cta_route.clone();
                    let preserved_coming_soon_tier =
                        modules[existing_idx].coming_soon_tier.clone();
                    let preserved_coming_soon_target =
                        modules[existing_idx].coming_soon_target.clone();
                    modules[existing_idx] = ModuleCatalogEntry {
                        kind: preserved_kind,
                        parent_id: preserved_parent_id,
                        cta_route: preserved_cta_route,
                        coming_soon_tier: preserved_coming_soon_tier,
                        coming_soon_target: preserved_coming_soon_target,
                        ..live
                    };
                } else {
                    modules.push(live);
                }
            }
            Err((path_disp, err)) => {
                parse_errors.push(ManifestParseError {
                    module_id: String::new(),
                    source: path_disp,
                    error: err,
                });
            }
        }
    }

    // Dev-affordance toast: only fires when paid-modules/ exists, env
    // var is NOT set, and user hasn't dismissed.
    let dev_affordance_hint = build_dev_affordance_hint(db);

    CatalogResponse {
        modules,
        l0_status,
        parse_errors,
        dev_affordance_hint,
    }
}

/// What `module_installs` says about a given module_id (aggregated
/// across all projects). For multi-project setups we return the
/// first installed-state row we find — the catalog tile is project-
/// agnostic (it shows "RL is installed somewhere"), and the renderer
/// re-asserts via `list_installed_modules` per-project for the
/// enable/disable toggle.
enum InstallState {
    None,
    Installed { version: String },
    Broken { version: String },
    Pending { status: String, version: String },
}

fn lookup_install_state(db: &Db, module_id: &str) -> InstallState {
    // installed > broken > anything-else > none
    for status in &["installed", "broken", "installing", "running", "stopped", "error"] {
        if let Ok(rows) = db.list_module_installs_with_status(status) {
            for row in rows {
                if row.module_id == module_id {
                    return match *status {
                        "installed" => InstallState::Installed {
                            version: row.module_version,
                        },
                        "broken" => InstallState::Broken {
                            version: row.module_version,
                        },
                        other => InstallState::Pending {
                            status: other.into(),
                            version: row.module_version,
                        },
                    };
                }
            }
        }
    }
    InstallState::None
}

/// Modules that have an installed-state row but aren't in the given L0
/// set. Returns metadata (id + version) so the caller can render the
/// "No longer available in catalog" warning.
struct InstalledLegacyEntry {
    module_id: String,
    module_version: String,
}

fn installed_module_ids_not_in_set(
    db: &Db,
    l0_ids: &std::collections::HashSet<&str>,
) -> Vec<InstalledLegacyEntry> {
    let mut out = Vec::new();
    for status in &["installed", "broken"] {
        if let Ok(rows) = db.list_module_installs_with_status(status) {
            for row in rows {
                if l0_ids.contains(row.module_id.as_str()) {
                    continue;
                }
                // De-dup across multiple projects: keep the first occurrence.
                if out
                    .iter()
                    .any(|e: &InstalledLegacyEntry| e.module_id == row.module_id)
                {
                    continue;
                }
                out.push(InstalledLegacyEntry {
                    module_id: row.module_id,
                    module_version: row.module_version,
                });
            }
        }
    }
    out
}

/// Best-effort: try to read the on-disk manifest at
/// `~/.vct/modules/<id>/vct-module.json` for full display fields.
/// Falls back to a synthetic entry if the file is missing or malformed.
/// Returns the entry + an optional parse error (which is propagated to
/// `CatalogResponse.parse_errors`).
fn installed_only_entry(
    _db: &Db,
    legacy: &InstalledLegacyEntry,
) -> (ModuleCatalogEntry, Option<ManifestParseError>) {
    let manifest_path = crate::paths::vct_root_dir()
        .join("modules")
        .join(&legacy.module_id)
        .join("vct-module.json");
    let mut parse_err: Option<ManifestParseError> = None;
    let entry: ModuleCatalogEntry = if manifest_path.is_file() {
        match std::fs::read_to_string(&manifest_path) {
            Ok(raw) => match ModuleManifest::from_json(&raw) {
                Ok(m) => ModuleCatalogEntry::from_manifest(
                    &m,
                    // No L0 row → no license gate context. Render as
                    // licensed (the user already installed it, refusing
                    // to render is worse than showing a possibly-stale
                    // licensed=true).
                    true,
                    manifest_path.display().to_string(),
                ),
                Err(e) => {
                    parse_err = Some(ManifestParseError {
                        module_id: legacy.module_id.clone(),
                        source: manifest_path.display().to_string(),
                        error: e,
                    });
                    synthetic_legacy_entry(legacy)
                }
            },
            Err(e) => {
                parse_err = Some(ManifestParseError {
                    module_id: legacy.module_id.clone(),
                    source: manifest_path.display().to_string(),
                    error: format!("read: {}", e),
                });
                synthetic_legacy_entry(legacy)
            }
        }
    } else {
        synthetic_legacy_entry(legacy)
    };

    // Mark with the legacy warning so the renderer shows the badge.
    let mut entry = entry;
    entry.kind = "installed".into();
    entry.catalog_warning =
        "This module is installed but no longer available in the catalog. \
         It will continue to work but won't receive updates."
            .into();
    (entry, parse_err)
}

fn synthetic_legacy_entry(legacy: &InstalledLegacyEntry) -> ModuleCatalogEntry {
    // Construct a minimal placeholder when neither L0 nor the on-disk
    // manifest is available. Uses the module_id + version straight
    // from the DB row. Direct-construct rather than going through
    // `from_manifest` to avoid having to build a `ModuleManifest`
    // (which requires `InstallBlock` + `RuntimeBlock` field values
    // we don't have).
    ModuleCatalogEntry {
        id: legacy.module_id.clone(),
        name: legacy.module_id.clone(),
        version: legacy.module_version.clone(),
        description: String::new(),
        category: "paid-independent".into(),
        tags: Vec::new(),
        license_required: false,
        license_variant_ids: Vec::new(),
        min_orchestrator_tier: "free".into(),
        compatibility_hosts: Vec::new(),
        is_licensed: true,
        manifest_source: "installed (synthetic)".into(),
        kind: "installed".into(),
        parent_id: String::new(),
        cta_route: String::new(),
        coming_soon_tier: String::new(),
        coming_soon_target: String::new(),
        deprecated: false,
        deprecation_message: String::new(),
        deprecation_eol_date: String::new(),
        deprecation_migration_url: String::new(),
        catalog_warning: String::new(),
        runtime_type: String::new(),
        // v0.2.49: synthetic legacy entries fall back to per_project
        // because we don't have manifest data to determine scope.
        // If the user is on a legacy install of a now-global-scope
        // module, the auto-migration in Stream A will rewrite the
        // install row on next launcher boot; until then the tile
        // renders with the per_project shape, which matches the
        // current install row's project_id != NULL state.
        install_scope: "per_project".into(),
    }
}

/// Coarse semver `a < b` test (matches `ModuleCatalog.svelte::semverLess`'s
/// shape so renderer + catalog agree). Splits on '.', parses the leading
/// integer of each segment, lex-compares.
fn semver_less(a: &str, b: &str) -> bool {
    let parse = |v: &str| -> Vec<u64> {
        v.split('.')
            .map(|s| {
                let digits: String = s.chars().take_while(|c| c.is_ascii_digit()).collect();
                digits.parse::<u64>().unwrap_or(0)
            })
            .collect()
    };
    let aa = parse(a);
    let bb = parse(b);
    for i in 0..aa.len().max(bb.len()) {
        let x = aa.get(i).copied().unwrap_or(0);
        let y = bb.get(i).copied().unwrap_or(0);
        if x < y {
            return true;
        }
        if x > y {
            return false;
        }
    }
    false
}

fn read_and_parse_manifest(path: &std::path::Path) -> Result<ModuleManifest, (String, String)> {
    let raw = std::fs::read_to_string(path).map_err(|e| {
        (
            path.display().to_string(),
            format!("read {}: {}", path.display(), e),
        )
    })?;
    ModuleManifest::from_json(&raw).map_err(|e| {
        (
            path.display().to_string(),
            format!("parse {}: {}", path.display(), e),
        )
    })
}

fn build_dev_affordance_hint(db: &Db) -> Option<DevAffordanceHint> {
    if dev_catalog_passthrough_enabled() {
        // User has explicitly opted in; no hint needed.
        return None;
    }
    let dismissed = db
        .app_state_get(APP_STATE_KEY_DEV_AFFORDANCE_DISMISSED)
        .ok()
        .flatten()
        .map(|v| v == "true")
        .unwrap_or(false);
    if dismissed {
        return None;
    }
    let paid_modules = paid_modules_dir_exists(db)?;
    Some(DevAffordanceHint {
        paid_modules_path: paid_modules.display().to_string(),
        env_var_name: crate::commands::installed_modules::DEV_CATALOG_PASSTHROUGH_ENV.into(),
    })
}

/// Tauri command: mark the dev-affordance hint as dismissed. The
/// renderer calls this once the user clicks "Got it" on the toast.
/// Subsequent `list_module_catalog` calls will return
/// `dev_affordance_hint = None`.
#[command]
pub async fn dismiss_dev_affordance_hint(db: State<'_, Db>) -> Result<(), String> {
    db.app_state_set(APP_STATE_KEY_DEV_AFFORDANCE_DISMISSED, "true")?;
    Ok(())
}

// ─── L9 manifest-parse-error logger (v0.2.33, Agent E) ─────────────────
//
// `CatalogResponse.parse_errors` already surfaces failures to the GUI
// (yellow banner + modal). For postmortem we also append each error to
// `<install>/state/logs/launcher_errors.jsonl` (one JSON object per line)
// so support pings can quote the entry without screenshotting the modal.
//
// Fallback path: when no install root is resolvable we land in
// `~/.vct/launcher_errors.jsonl`. Mirrors the services-watcher pattern.

/// Resolve the canonical JSONL log path. Mirrors
/// `services::watcher::resolve_log_path` so both logs sit beside each
/// other under `state/logs/` when an install root exists.
fn resolve_launcher_errors_log_path() -> PathBuf {
    if let Ok(root) = crate::commands::installer::find_local_repo_root() {
        return root.join("state/logs/launcher_errors.jsonl");
    }
    // Fallback when run outside an install (cargo run, CI shell).
    crate::paths::vct_root_dir().join("launcher_errors.jsonl")
}

/// One line written to `launcher_errors.jsonl`. The schema is
/// narrow-on-purpose: `ts` (RFC 3339 UTC), `kind` (event family),
/// then the entry's fields. Future event families bolt on with
/// their own `kind` value; consumers filter by `kind` first.
#[derive(Debug, Clone, Serialize)]
struct LauncherErrorLogEntry<'a> {
    ts: String,
    kind: &'a str,
    module_id: &'a str,
    source: &'a str,
    error: &'a str,
}

/// Append a single `manifest_parse_error` event to the given JSONL
/// log path. Best-effort: write failures are swallowed (the log is
/// debugging aid, not load-bearing). Returns the path that was passed
/// in for convenience (so callers can chain).
///
/// Pulled out from `append_manifest_parse_error_log` so tests can
/// exercise it directly with a tempdir-rooted path without depending
/// on the live `find_local_repo_root` resolver.
fn append_manifest_parse_error_log_at(path: &std::path::Path, err: &ManifestParseError) {
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let entry = LauncherErrorLogEntry {
        ts: chrono::Utc::now().to_rfc3339(),
        kind: "manifest_parse_error",
        module_id: &err.module_id,
        source: &err.source,
        error: &err.error,
    };
    let line = match serde_json::to_string(&entry) {
        Ok(s) => s,
        Err(_) => return,
    };
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
    {
        let _ = writeln!(f, "{}", line);
    }
}

/// Production entry-point: resolve the canonical log path and append
/// the error there. Wraps `append_manifest_parse_error_log_at` with
/// the path resolution so callers in the Tauri command don't need to
/// know about the install-root vs `~/.vct/` fallback.
fn append_manifest_parse_error_log(err: &ManifestParseError) -> PathBuf {
    let path = resolve_launcher_errors_log_path();
    append_manifest_parse_error_log_at(&path, err);
    path
}

/// Tauri command: persist a batch of manifest-parse errors to the
/// launcher's JSONL postmortem log. The renderer calls this once per
/// `loadCatalog` round-trip when `parse_errors` is non-empty.
///
/// Soft-fail throughout: filesystem errors are swallowed (the log is
/// debugging aid, not load-bearing). Returns the resolved log path
/// purely so the renderer can quote it in the modal footer.
#[command]
pub async fn log_manifest_parse_errors(
    errors: Vec<ManifestParseError>,
) -> Result<String, String> {
    if errors.is_empty() {
        return Ok(resolve_launcher_errors_log_path().display().to_string());
    }
    let mut last_path = PathBuf::new();
    for err in &errors {
        last_path = append_manifest_parse_error_log(err);
    }
    Ok(last_path.display().to_string())
}

// ─── Pre-install vs post-install manifest split (v0.2.33) ──────────────
//
// `find_manifest` was a single function serving both install-time
// (where the manifest had to come from on-disk pre-install scanning,
// which is the bug v0.2.33 fixes) AND dispatch-time (where we WANT
// the on-disk extracted manifest). The new shape:
//
//   * `resolve_install_metadata` — pre-install. Returns the L0
//     install-time slice for use by `install_module_for_project` to
//     drive container_pull. Does NOT return a `ModuleManifest`; it
//     returns the narrower `L0CatalogModule` because we don't have
//     the full manifest yet (it lives in the image).
//
//   * `find_installed_manifest` — post-install. Reads
//     `~/.vct/modules/<id>/vct-module.json` (written by Agent C's
//     `extract_manifest_from_image`). Returns the full
//     `ModuleManifest` + the PathBuf, for `update_module_for_project`,
//     `uninstall_module_v2`, dispatcher resume paths, etc.

/// v0.2.33 pre-install lookup. Reads the cached L0 envelope and
/// returns the entry matching `module_id`, or an Err if the catalog
/// is unavailable / the module isn't listed.
///
/// Sync wrapper: this is called from sync helpers (the installer
/// engine isn't async at this point in its lifecycle). Internally it
/// uses `tauri::async_runtime::block_on` against the async
/// `cached_module_catalog`. If the runtime can't be entered (e.g.
/// we're already inside Tauri's reactor without a current handle),
/// returns an Err.
///
/// v0.2.33: declared `pub` for the v0.2.34 follow-up (the cold-start
/// install path that builds a thin ModuleManifest from this slice).
/// The current `install_path_manifest_lookup` doesn't call it yet —
/// install still uses on-disk-extracted-or-dev-paid-modules.
#[allow(dead_code)] // consumed by v0.2.34 cold-start install path
pub fn resolve_install_metadata(db: &Db, module_id: &str) -> Result<L0CatalogModule, String> {
    // Try the in-DB cache directly without invoking the network. If
    // it's fresh we get the value synchronously; if it's stale or
    // absent we surface a helpful error pointing at
    // refresh_module_catalog. This avoids the block_on complexity
    // entirely while keeping the install path snappy when the user
    // has just visited the Modules tab (cache fresh).
    let raw = db
        .app_state_get(crate::commands::module_catalog_client::APP_STATE_KEY_CATALOG)
        .map_err(|e| format!("read catalog cache: {}", e))?
        .ok_or_else(|| {
            format!(
                "module {} not available in catalog cache; \
                 visit the Modules tab or call refresh_module_catalog first",
                module_id,
            )
        })?;
    let envelope: L0CatalogResponse = serde_json::from_str(&raw)
        .map_err(|e| format!("parse cached catalog: {}", e))?;
    envelope
        .modules
        .into_iter()
        .find(|m| m.id == module_id)
        .ok_or_else(|| format!("module {} not in L0 catalog", module_id))
}

/// v0.2.33 post-install lookup. Reads
/// `~/.vct/modules/<module_id>/vct-module.json` (the file Agent C's
/// `extract_manifest_from_image` writes). Errors if the file is
/// missing or fails to parse — which the catalog refactor surfaces as
/// a parse_errors entry to the renderer (banner).
pub fn find_installed_manifest(
    _db: &Db,
    module_id: &str,
) -> Result<(ModuleManifest, PathBuf), String> {
    let path = crate::paths::vct_root_dir()
        .join("modules")
        .join(module_id)
        .join("vct-module.json");
    if !path.is_file() {
        return Err(format!(
            "module {} has no installed manifest at {}",
            module_id,
            path.display()
        ));
    }
    let raw = std::fs::read_to_string(&path)
        .map_err(|e| format!("read {}: {}", path.display(), e))?;
    let m = ModuleManifest::from_json(&raw)
        .map_err(|e| format!("parse {}: {}", path.display(), e))?;
    if m.id != module_id {
        return Err(format!(
            "manifest at {} declares id={:?} but caller asked for {:?}",
            path.display(),
            m.id,
            module_id,
        ));
    }
    Ok((m, path))
}

/// v0.2.33 install-path manifest lookup. Tries the on-disk extracted
/// manifest first (re-install / reinstall-from-broken case), then
/// falls back to the dev-affordance scan when the passthrough env var
/// is set. Returns Err when neither source has a matching manifest.
///
/// Used by `uninstall_module_v2` which (by definition) never needs the
/// L0-synth cold-start branch — uninstall only ever runs against a
/// module that's already installed (so the on-disk extract is present).
/// `install_module_for_project` / `update_module_for_project` use
/// `resolve_manifest_for_install` instead, which adds the L0-synth
/// fallback for the cold-start case.
fn install_path_manifest_lookup(
    db: &Db,
    module_id: &str,
) -> Result<(ModuleManifest, PathBuf), String> {
    if let Ok(pair) = find_installed_manifest(db, module_id) {
        return Ok(pair);
    }
    if dev_catalog_passthrough_enabled() {
        for path in dev_paid_modules_paths(db) {
            if let Ok(raw) = std::fs::read_to_string(&path) {
                if let Ok(m) = ModuleManifest::from_json(&raw) {
                    if m.id == module_id {
                        return Ok((m, path));
                    }
                }
            }
        }
    }
    Err(format!(
        "module {} has no on-disk manifest available. Either the \
         module has never been installed (use install_module_for_project, \
         which falls back to L0-synth on cold-start), or its install \
         was incomplete (no `~/.vct/modules/{}/vct-module.json`).",
        module_id, module_id,
    ))
}

/// Which lookup branch produced the manifest in
/// [`resolve_manifest_for_install`]. Carried alongside the manifest so
/// callers (`install_module_for_project`, `update_module_for_project`)
/// can audit-log where the install metadata came from and surface
/// useful diagnostics on the install-complete event.
///
/// The variants are ordered by preference — `Installed` wins over `Dev`
/// wins over `L0Synth`. The audit row carries the source variant verbatim
/// so post-incident triage can answer "was this a cold-start install or a
/// re-install from on-disk?" without grepping logs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ManifestSource {
    /// On-disk extracted manifest at
    /// `~/.vct/modules/<id>/vct-module.json` — produced by Agent C's
    /// post-pull `extract_manifest_from_image`. Carries the absolute
    /// path for audit logging.
    Installed(PathBuf),
    /// Dev-affordance: `<install_root>/paid-modules/<id>/vct-module.json`,
    /// gated by `VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH=1`. Used by module
    /// developers running the launcher against a local
    /// pre-publish working copy of their module.
    Dev(PathBuf),
    /// Synthesized from the cached L0 install-slice — the v0.2.33 B2
    /// cold-start path. The synth is in-memory only; Agent C's extract
    /// will overwrite the on-disk file immediately after `container_pull`
    /// succeeds.
    L0Synth,
}

impl ManifestSource {
    /// Stringification for the audit row. Stable identifiers — don't
    /// rename without coordinating with the audit-log consumers.
    pub(crate) fn as_audit_str(&self) -> String {
        match self {
            ManifestSource::Installed(p) => format!("installed:{}", p.display()),
            ManifestSource::Dev(p) => format!("dev:{}", p.display()),
            ManifestSource::L0Synth => "l0-synth".to_string(),
        }
    }
}

/// v0.2.45 V45-C: tiny semver parser used by `resolve_manifest_for_install`
/// to compare the on-disk manifest version against the L0 catalog version.
///
/// We don't pull in the `semver` crate just for this — splitting on `.` and
/// parsing three `u64` components covers every published module version
/// (v0.2.7, v0.2.8, 1.0.0, …). Pre-release and build-metadata suffixes are
/// not supported; if a version string carries one (e.g. "0.2.8-rc1") we
/// return None and the caller's safety net (`None` → on-disk wins) keeps
/// behaviour conservative — we won't synthesize from a version we can't
/// confidently compare.
fn parse_semver(s: &str) -> Option<(u64, u64, u64)> {
    let s = s.trim().trim_start_matches('v');
    let mut parts = s.split('.');
    let major = parts.next()?.parse::<u64>().ok()?;
    let minor = parts.next()?.parse::<u64>().ok()?;
    let patch_raw = parts.next()?;
    // Reject anything with a pre-release / build-metadata suffix on the
    // patch component (e.g. "0.2.8-rc1", "0.2.8+build42"). The safety net
    // (None → on-disk wins) covers these — we'd rather honour the
    // user's last-installed version than guess at suffix ordering.
    if patch_raw.chars().any(|c| !c.is_ascii_digit()) {
        return None;
    }
    let patch = patch_raw.parse::<u64>().ok()?;
    // Reject trailing components ("0.2.8.4") — same reason: ambiguous
    // ordering semantics. Pure 3-component versions only.
    if parts.next().is_some() {
        return None;
    }
    Some((major, minor, patch))
}

/// v0.2.33 B2: three-phase install manifest resolver. Returns the
/// manifest the installer engine should consume plus a tag describing
/// where it came from.
///
/// Phase order (first one that succeeds wins):
///   1. **Installed**: on-disk extracted manifest at
///      `~/.vct/modules/<id>/vct-module.json`. Wins for re-installs +
///      reinstalls-from-broken (the prior install already extracted the
///      real manifest; we have authoritative data on disk).
///   2. **Dev**: `<install_root>/paid-modules/<id>/vct-module.json`,
///      gated on `VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH=1`. Wins for
///      module-author workflows running against a local working copy
///      before publishing the GHCR image.
///   3. **L0Synth**: synthesise a thin `ModuleManifest` from the cached
///      L0 install-slice (see `l0_manifest_synth`). This is the
///      cold-start path — closes the v0.2.33 G-J3-a gap from the
///      architecture review. The synth lives only for the duration of
///      this install; Agent C's `extract_manifest_from_image` REPLACES
///      it on disk immediately after `container_pull` succeeds.
///
/// Cold-start preconditions for phase 3:
///   * The catalog cache must contain an entry for `module_id` (set by
///     the Modules-tab visit or a `refresh_module_catalog` call).
///   * The L0 install-slice must be complete (image non-empty etc. —
///     see `synthesize_install_manifest_from_l0` for the full guard).
///
/// If phase 3 fails (cache empty, module not in L0, malformed slice),
/// the error message names the missing precondition so the user sees
/// "visit the Modules tab first" rather than a confusing low-level
/// failure.
pub(crate) fn resolve_manifest_for_install(
    db: &Db,
    module_id: &str,
) -> Result<(ModuleManifest, ManifestSource), String> {
    // Phase 1: on-disk extracted manifest.
    //
    // v0.2.45 V45-C: the original v0.2.33 contract was "on-disk wins
    // unconditionally". That was the right call before the catalog-refresh
    // model (a re-install was supposed to drive against the manifest the
    // user last actually pulled, not invisibly morph into an upgrade).
    //
    // After v0.2.44, the L0 catalog cache is the source of truth for "what
    // version should this module be at right now", and the catalog-refresh
    // path populates it deterministically before the install retry runs.
    // The new contract: phase 1 still wins for re-installs at the SAME or
    // OLDER version (catalog stale / no upgrade available), but when L0
    // advertises a strictly newer semver, we fall through to phase 3
    // (L0Synth) so the retry pulls the published version the user clicked
    // Install for.
    //
    // Audit-logged at decision time via `module_manifest_resolved` so the
    // post-incident triage path can see which version was on disk, which
    // version L0 advertised, and which branch we took. Safety net: any
    // parse failure on either version string → phase 1 wins (we don't
    // synthesize from a malformed L0 entry).
    match find_installed_manifest(db, module_id) {
        Ok((on_disk_m, on_disk_path)) => {
            let on_disk_v = on_disk_m.version.clone();
            // resolve_install_metadata is cache-only / no-network. Err
            // (e.g. catalog cache empty) maps to "no L0 version known" —
            // we honour the on-disk manifest in that case.
            let l0_v_opt = resolve_install_metadata(db, module_id)
                .ok()
                .map(|l0| l0.version);
            let l0_is_newer = match (
                parse_semver(&on_disk_v),
                l0_v_opt.as_deref().and_then(parse_semver),
            ) {
                (Some(od), Some(l0)) => l0 > od,
                _ => false, // any parse failure → on-disk wins (safety net)
            };
            if l0_is_newer {
                eprintln!(
                    "[v0.2.45 V45-C] L0 catalog has newer version for {}: \
                     on_disk={} l0={} — synthesizing from L0",
                    module_id,
                    on_disk_v,
                    l0_v_opt.as_deref().unwrap_or("?"),
                );
                let _ = db.audit(
                    "module_manifest_resolved",
                    None,
                    Some(module_id),
                    &serde_json::json!({
                        "module_id": module_id,
                        "on_disk_version": on_disk_v,
                        "l0_version": l0_v_opt,
                        "decision": "l0_newer_synthesized",
                    }),
                );
                // Fall through to phase 2 / phase 3 — do NOT return here.
            } else {
                let _ = db.audit(
                    "module_manifest_resolved",
                    None,
                    Some(module_id),
                    &serde_json::json!({
                        "module_id": module_id,
                        "on_disk_version": on_disk_v,
                        "l0_version": l0_v_opt,
                        "decision": "on_disk_winner",
                    }),
                );
                return Ok((on_disk_m, ManifestSource::Installed(on_disk_path)));
            }
        }
        Err(_) => {
            // No on-disk manifest at all — fall through to phase 2/3.
        }
    }

    // Phase 2: dev affordance.
    if dev_catalog_passthrough_enabled() {
        for path in dev_paid_modules_paths(db) {
            if let Ok(raw) = std::fs::read_to_string(&path) {
                if let Ok(m) = ModuleManifest::from_json(&raw) {
                    if m.id == module_id {
                        return Ok((m, ManifestSource::Dev(path)));
                    }
                }
            }
        }
    }

    // Phase 3: cold-start synth from L0 install-slice.
    //
    // resolve_install_metadata reads `app_state[module_catalog.cache]`
    // synchronously — no network hit. The cache is populated by the
    // Modules-tab mount + the `↻` refresh button. When the user clicks
    // Install directly from a stale Home tile WITHOUT visiting Modules
    // first, the cache may be empty — in which case resolve_install_metadata
    // surfaces "visit the Modules tab or call refresh_module_catalog
    // first", which is actionable for the user.
    let l0_slice = crate::commands::modules::resolve_install_metadata(db, module_id)?;
    let synth = crate::commands::l0_manifest_synth::synthesize_install_manifest_from_l0(
        &l0_slice,
    )?;
    Ok((synth, ManifestSource::L0Synth))
}

/// Compatibility shim for module_service's restart path. Same
/// behaviour as `find_installed_manifest` but discards the source
/// path since the caller only needs the parsed manifest.
///
/// v0.2.33: prefers the installed manifest under `~/.vct/modules/<id>/`.
/// Falls back to the dev-affordance path (`paid-modules/<id>/`) ONLY
/// when the env var is set — preserves the local-dev workflow for
/// module-authoring sessions.
pub fn find_manifest_for_resume(db: &Db, module_id: &str) -> Option<ModuleManifest> {
    if let Ok((m, _)) = find_installed_manifest(db, module_id) {
        return Some(m);
    }
    // Dev passthrough fallback. Only activated when the env var is on
    // — production users don't reach this branch.
    if !dev_catalog_passthrough_enabled() {
        return None;
    }
    for path in dev_paid_modules_paths(db) {
        if let Ok(raw) = std::fs::read_to_string(&path) {
            if let Ok(m) = ModuleManifest::from_json(&raw) {
                if m.id == module_id {
                    return Some(m);
                }
            }
        }
    }
    None
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

    // 2. Manifest lookup — v0.2.33 B2 cold-start synth wired in.
    //
    // Resolves the manifest in three phases (first match wins):
    //   a. on-disk extracted manifest at `~/.vct/modules/<id>/`
    //      (re-install / reinstall-from-broken case).
    //   b. dev-affordance `<install_root>/paid-modules/<id>/` when
    //      `VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH=1` (module-author
    //      workflows running against a local working copy).
    //   c. cold-start: synthesise a thin ModuleManifest from the
    //      cached L0 install-slice. Closes the G-J3-a gap from the
    //      v0.2.33 architecture review — a true first-install on a
    //      real-user machine where neither the extracted on-disk
    //      manifest nor the dev paid-modules/ exists. After
    //      `container_pull` succeeds, Agent C's
    //      `extract_manifest_from_image` writes the REAL manifest to
    //      `~/.vct/modules/<id>/vct-module.json` — the synth is
    //      replaced in-flight, never persisted.
    let (manifest, manifest_source) = resolve_manifest_for_install(&db, &module_id)?;

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
    //
    // v0.2.49 Stream A: branch on `install.scope`. Global-scope modules
    // (e.g. RL Reranker v0.2.10+) get a single machine-wide row with
    // project_id=NULL via `insert_global_module_install`. Per-project
    // modules retain the v0.2.20–v0.2.48 path. The `is_global` flag
    // threads through every status / lifecycle write below so the right
    // accessor is selected.
    let install_id = Uuid::new_v4().to_string();
    let ctx = PlaceholderCtx::new(&module_id);
    let install_dir = ctx.resolve_install_dir(&manifest.install.install_dir);
    let is_global = manifest.install.scope.is_global();
    let row = if is_global {
        db.insert_global_module_install(
            &install_id,
            &module_id,
            &manifest.version,
            &install_dir.display().to_string(),
        )?
    } else {
        db.insert_module_install(
            &install_id,
            &project_id,
            &module_id,
            &manifest.version,
            &install_dir.display().to_string(),
        )?
    };

    // v0.2.49 Step F MF3 (migration 032): persist the manifest's
    // `kg_collections` declaration into the launcher DB at install
    // time. Read by `populate_kg_collection_access_for_project` on
    // every new-project create to back-fill access rows for already-
    // installed global modules (the inverse of item #13 below). The
    // launcher DB is the authoritative state — downstream consumers
    // never re-parse the on-disk manifest from the hot path.
    //
    // Wired for BOTH global + per-project installs so per-project
    // modules that might declare kg_collections in the future are
    // covered. Soft-fail per the helper's contract (the audit log
    // captures any error; install proceeds).
    if let Err(e) = db.set_module_kg_collections(
        &row.id,
        manifest.kg_collections.as_deref(),
    ) {
        db.audit(
            "module_kg_collections_persist_failed",
            Some(&project_id),
            Some(&module_id),
            &serde_json::json!({"install_id": row.id, "error": e}),
        )?;
    }

    // v0.2.49 item #13 (M-3): if a global module declares KG collections,
    // seed access rows for every project at install time. Uses
    // `kg_seed_access` (INSERT OR IGNORE) so any user-configured
    // downgrades survive re-installs. Per-project modules don't go
    // through this path — their access matrix is owned by the
    // per-project populate at project-create time.
    if is_global {
        if let Some(collections) = manifest.kg_collections.as_ref() {
            if !collections.is_empty() {
                let mut populate_report =
                    crate::commands::project_state_populate::PopulateReport::default();
                crate::commands::project_state_populate::populate_kg_collection_access_for_global_module(
                    collections,
                    &db,
                    &mut populate_report,
                );
                db.audit(
                    "kg_access_seeded_global_module",
                    None,
                    Some(&module_id),
                    &serde_json::json!({
                        "collections": collections,
                        "rows_inserted": populate_report.kg_access_rows_inserted,
                        "warnings": populate_report.warnings,
                    }),
                )?;
            }
        }
    }

    db.audit(
        "module_install_start",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({
            "version": manifest.version,
            "manifest_source": manifest_source.as_audit_str(),
        }),
    )?;

    // 6. Run install engine.
    //
    // v0.2.20: probe the persisted hardware snapshot for the current
    // GpuMode so container_pull can pick a per-variant image tag (when
    // the manifest declares `runtime.gpu_image_variants`).
    //
    // v0.2.34 (Agent B): the persisted snapshot MUST be fresh +
    // structurally complete here, otherwise a partial schema (e.g. the
    // the v0.2.20 → v0.2.21 upgrade path where `gpu_mode_decided`
    // was added without backfilling existing snapshots) would let an
    // RTX 4080 SUPER host serde-default to `GpuMode::Cpu` and pull the
    // `-cpu` image variant. `ensure_fresh_hardware_snapshot_for_install`
    // runs `redetect_hardware` synchronously here with a soft-fail
    // fallback to the last-known persisted snapshot — install never
    // blocks on a transient probe failure (nvidia-smi briefly missing,
    // etc.). When neither a probe nor a last-known snapshot is
    // available, we fall back to `GpuMode::Cpu` (the pre-v0.2.34
    // behaviour) so first-time installs on hosts where `nvidia-smi`
    // failed at every boot still complete — they just get the CPU
    // image, which is the safe degradation path.
    let gpu_mode =
        crate::commands::installer::ensure_fresh_hardware_snapshot_for_install(db.inner())
            .await
            .map(|snap| snap.gpu_mode_decided)
            .unwrap_or(crate::commands::gpu_policy::GpuMode::Cpu);

    // NEW-1 (2026-05-28): read the L0 catalog's pull_token_endpoint so
    // installer_engine can prefer it over the L1 manifest's value.
    // Soft-fail: if the catalog cache is empty (user never opened Modules tab)
    // fall back to None — the L1 value is used as before. The ContainerPull
    // method may then fail with a placeholder URL, but the user gets a clear
    // error rather than a silent wrong-URL 403.
    let l0_pull_token_endpoint: Option<String> = resolve_install_metadata(&db, &module_id)
        .ok()
        .map(|l0| l0.install.container.pull_token_endpoint);

    match installer_engine::run_install(&app, &manifest, &ctx, &project_id, gpu_mode, &db, l0_pull_token_endpoint.as_deref()).await {
        Ok(resolved_dir) => {
            // NEW-3.D (2026-05-28): validate manifest contract for container modules
            // before marking the row as Installed. Error-severity warnings (e.g.
            // missing install.container.image) block the install with a clear message
            // and set status=Error so the GUI shows the failure. Deprecation-severity
            // warnings (e.g. missing container_name_template) are logged + audited
            // and do NOT block — backward compatible with existing RL Reranker manifests.
            {
                use crate::manifest::{WarningSeverity};
                let warnings = manifest.validate_for_container_start();
                let errors: Vec<_> = warnings.iter()
                    .filter(|w| w.severity == WarningSeverity::Error)
                    .collect();
                if !errors.is_empty() {
                    let error_summary = errors.iter()
                        .map(|w| format!("{}: {}", w.field, w.message))
                        .collect::<Vec<_>>()
                        .join("; ");
                    let msg = format!("manifest validation failed: {}", error_summary);
                    if is_global {
                        db.set_global_module_status(
                            &module_id,
                            ModuleStatus::Error,
                            Some(msg.clone()),
                        )?;
                    } else {
                        db.set_module_status(
                            &project_id,
                            &module_id,
                            ModuleStatus::Error,
                            Some(msg.clone()),
                        )?;
                    }
                    return Err(format!("Install rejected for module {}: {}", module_id, msg));
                }
                for w in warnings.iter().filter(|w| w.severity == WarningSeverity::Deprecation) {
                    eprintln!(
                        "[install] manifest deprecation for {}: {}: {}",
                        module_id, w.field, w.message
                    );
                    let _ = db.audit(
                        "module_install_manifest_deprecation",
                        Some(&project_id),
                        Some(&module_id),
                        &serde_json::json!({
                            "field": w.field,
                            "message": w.message,
                        }),
                    );
                }
            }
            if is_global {
                db.set_global_module_status(&module_id, ModuleStatus::Installed, None)?;
                db.audit(
                    "module_install_done",
                    None,
                    Some(&module_id),
                    &serde_json::json!({
                        "install_dir": resolved_dir.display().to_string(),
                        "scope": "global",
                    }),
                )?;
            } else {
                db.set_module_status(&project_id, &module_id, ModuleStatus::Installed, None)?;
                db.audit(
                    "module_install_done",
                    Some(&project_id),
                    Some(&module_id),
                    &serde_json::json!({ "install_dir": resolved_dir.display().to_string() }),
                )?;
            }

            // v0.2.34 Agent E (Phase 4 generalisation): reconcile this
            // module's MCP tool-allowlist defaults into
            // `module_mcp_tool_defaults`. The hub's
            // `/mcp-tool-grants/{mcp_name}` route reads these rows when
            // assembling a wrapper's allowlist, merging with per-project
            // overrides from `project_mcp_tool_grants`. Soft-fail: a DB
            // error here is logged but doesn't break the install
            // (defaults can be reconciled again on next install/update).
            reconcile_module_tool_allowlist(&manifest, &module_id, &db);

            // v0.2.49 Stream B: when a global-scope module finishes
            // installing, seed `enabled=true` rows in `module_settings`
            // for every existing project so the module is on by default
            // across the host. The seeding loop is a no-op for
            // per-project-scope modules (the legacy default) — the
            // helper short-circuits via `install_scope_is_global()`.
            //
            // Soft-fail throughout: the helper logs per-row failures
            // and returns the success count. We audit the count for
            // forensic trace.
            if manifest.install_scope_is_global() {
                let seeded = crate::commands::module_enabled
                    ::seed_enabled_rows_for_new_global_module(&db, &manifest, &module_id);
                let _ = db.audit(
                    "module_global_enable_seeded_on_install",
                    Some(&project_id),
                    Some(&module_id),
                    &serde_json::json!({ "projects_seeded": seeded }),
                );
            }

            // v0.2.43 V0243-17: post-install assertion — if the manifest
            // declares mcp_registration.tool_allowlist, at least 1 row
            // MUST be present in module_mcp_tool_defaults after reconcile.
            // Log forensic evidence either way so failures are traceable
            // even when `reconcile_module_tool_allowlist` soft-failed
            // silently (it only eprintln!s, it does not return Err).
            if let Some(mcp_reg) = manifest.mcp_registration.as_ref() {
                if let Some(allowlist) = mcp_reg.tool_allowlist.as_ref() {
                    if !allowlist.is_empty() {
                        match db.list_mcp_tool_defaults(&mcp_reg.mcp_name) {
                            Ok(defaults) if !defaults.is_empty() => {
                                eprintln!(
                                    "[v0.2.43/V0243-17] {} (mcp={}): tool_allowlist \
                                     declares {} tool(s); {} row(s) confirmed in \
                                     module_mcp_tool_defaults",
                                    module_id,
                                    mcp_reg.mcp_name,
                                    allowlist.len(),
                                    defaults.len(),
                                );
                            }
                            Ok(_empty) => {
                                // Zero rows despite a non-empty allowlist — reconcile
                                // soft-failed. Audit the anomaly for later forensics;
                                // do NOT fail the install (rows can be reconciled on
                                // next update / restart).
                                eprintln!(
                                    "[v0.2.43/V0243-17] WARN: {} (mcp={}): tool_allowlist \
                                     has {} tool(s) but module_mcp_tool_defaults has 0 rows \
                                     — reconcile_module_tool_allowlist may have soft-failed. \
                                     Hub will fall back to hardcoded defaults until next \
                                     reconcile.",
                                    module_id, mcp_reg.mcp_name, allowlist.len(),
                                );
                                let _ = db.audit(
                                    "module_mcp_tool_defaults_empty_after_install",
                                    Some(&project_id),
                                    Some(&module_id),
                                    &serde_json::json!({
                                        "mcp_name": mcp_reg.mcp_name,
                                        "expected_tool_count": allowlist.len(),
                                        "actual_row_count": 0,
                                    }),
                                );
                            }
                            Err(e) => {
                                eprintln!(
                                    "[v0.2.43/V0243-17] {} (mcp={}): list_mcp_tool_defaults \
                                     query failed: {}",
                                    module_id, mcp_reg.mcp_name, e,
                                );
                            }
                        }
                    }
                }
            }

            // Phase 1E: per-project container lifecycle. For
            // container_pull modules we resolve `runtime.container_name_
            // template`, allocate an `rl_port` if not yet set, and
            // spawn the container via `module_service`. Soft-fail throughout:
            // the install row stays at status=installed even when the
            // container start fails — the user can hit Restart from the
            // dashboard. Surfaces the error via audit + a non-blocking
            // toast event so the failure mode is visible without
            // rolling back the install.
            // NEW-3 (2026-05-28): widened from `== "container"` to also
            // admit `"service"` — both types declare a long-running
            // daemon that should auto-start after install. `"cli"` /
            // `"mcp_stdio"` / `"mcp_http"` are deliberately excluded
            // (invoked on-demand, not persisted as containers).
            let resolved_container_name = if manifest.install.method
                == crate::manifest::InstallMethod::ContainerPull
                && matches!(manifest.runtime.r#type.as_str(), "container" | "service")
            {
                // v0.2.49 Stream A: select global vs per-project start
                // path. The global path doesn't take a ProjectRow; the
                // container has no `{project_slug}` substitution and
                // listens on the machine-wide `GLOBAL_RL_PORT`.
                let start_result = if is_global {
                    crate::commands::module_service::start_global_container_after_install(
                        &manifest, &db,
                    )
                    .await
                } else {
                    crate::commands::module_service::start_container_after_install(
                        &manifest, &project, &db,
                    )
                    .await
                };
                match start_result
                {
                    Ok(name) => {
                        // v0.2.40 R5: first-install auto-download of
                        // default weights. After the container starts
                        // for the RL Reranker, trigger a one-shot
                        // weights download so the project doesn't run
                        // on baked-in qwen3-only weights for up to 24h
                        // until the user manually clicks "Download
                        // default weights" or the daily poll fires.
                        //
                        // Gates:
                        //   - module_id == RL_RERANKER_MODULE_ID — the
                        //     only module today that ships weights.
                        //     Future modules with weights will need
                        //     this gate widened (likely manifest-driven
                        //     `runtime.weights_provider != None`); for
                        //     v0.2.40 the explicit id-check is the
                        //     narrowest correct scope.
                        //   - `is_module_licensed` — paid-tier only.
                        //     The manifest "Download default weights"
                        //     button is already hidden for free-tier
                        //     per `module_default_weights.rs:50-54`,
                        //     so the auto-trigger mirrors that policy.
                        //     Note: `is_module_licensed` was already
                        //     checked above (line 1341) as the install
                        //     gate, but we re-check here defensively
                        //     in case the tier rotated mid-install.
                        //
                        // Detached spawn: the download is 50-500 MB
                        // and can take minutes on a slow connection.
                        // We emit `module://install-complete` BELOW
                        // (after this match), and the user-visible
                        // install is declared done at that point. The
                        // weights download streams in the background.
                        //
                        // Soft-fail throughout: every error path in
                        // `apply_default_weights_after_install` is
                        // logged + recorded as
                        // `module_settings.weights_download_deferred=
                        // true` (per-(project,module)) so the GUI
                        // tile can render "click Download default
                        // weights to refresh". The install row stays
                        // installed; the container is already running.
                        //
                        // Depends on (R4): the Supabase edge function
                        // `rl-latest-weights` must be deployed for
                        // this to succeed end-to-end. If R4 hasn't
                        // shipped, the download soft-fails with a
                        // clear 404 message and the deferred-flag is
                        // set. See multi-Opus pre-push review item 5.
                        if module_id == crate::commands::module_service::RL_RERANKER_MODULE_ID
                            && is_module_licensed(&manifest, &db)
                        {
                            let app_clone = app.clone();
                            let project_id_clone = project_id.clone();
                            let module_id_clone = module_id.clone();
                            let manifest_clone = manifest.clone();
                            tauri::async_runtime::spawn(async move {
                                use tauri::Manager;
                                let db_state: tauri::State<'_, Db> = app_clone.state();
                                let db_ref: &Db = db_state.inner();
                                // v0.2.42 RT-6: re-check license INSIDE the
                                // detached spawn, not just at spawn-gate time.
                                // A user can deactivate their license between
                                // the outer `is_module_licensed` check (which
                                // runs synchronously before the spawn) and the
                                // actual download attempt (which runs in this
                                // background task, potentially seconds later).
                                // Without this check the auto-trigger would
                                // proceed for a de-licensed user and then set
                                // `weights_download_deferred=true` on failure —
                                // polluting the module tile's state with a
                                // misleading "download pending" hint for a
                                // user who chose the free tier.
                                if !is_module_licensed(&manifest_clone, db_ref) {
                                    eprintln!(
                                        "[module_default_weights] R5 auto-trigger: \
                                         skipping (license revoked between install-gate \
                                         and spawn) for module {} project {}",
                                        module_id_clone, project_id_clone
                                    );
                                    return;
                                }
                                // soft-fail wrapper internally; we
                                // discard the Result either way.
                                let _ = crate::commands::module_default_weights::
                                    apply_default_weights_after_install(
                                        &module_id_clone,
                                        &project_id_clone,
                                        db_ref,
                                        &app_clone,
                                    )
                                    .await;
                            });
                        }
                        Some(name)
                    }
                    Err(e) => {
                        eprintln!(
                            "[module_service] start_container_after_install failed (install row stays installed): {}",
                            e
                        );
                        // NEW-3.C (2026-05-28): persist the error to
                        // module_installs.last_error so the GUI tile renders a
                        // clear failure state instead of "installed but no
                        // container" silent-fail.
                        if is_global {
                            let _ = db.set_global_module_last_error(&module_id, Some(&e));
                        } else {
                            let _ = db.set_module_last_error(
                                &project_id,
                                &module_id,
                                Some(&e),
                            );
                        }
                        // v0.2.45 V45-E: ALSO flip status to 'error' so V44-G4
                        // auto-retry can heal the row on the next
                        // orchestrator-update. Pre-v0.2.45 the status stayed
                        // 'installed' on a container-start-failure (only
                        // last_error was set), leaving the row in:
                        //   status='installed' + last_error != NULL + container_name = NULL
                        // which is invisible to V44-G4's
                        //   status IN ('error', 'broken')
                        // predicate (see module_service::retry_failed_module_installs
                        // around line 2057). The install itself succeeded
                        // (image is on disk after a clean podman pull), but
                        // the post-install container start failed — flipping
                        // status to Error funnels the user to the same
                        // recovery path as a true install-time failure
                        // (Reinstall / auto-retry) instead of stranding the
                        // row in a half-state that only manual GUI clicks
                        // can recover from.
                        if is_global {
                            let _ = db.set_global_module_status(
                                &module_id,
                                ModuleStatus::Error,
                                Some(e.clone()),
                            );
                        } else {
                            let _ = db.set_module_status(
                                &project_id,
                                &module_id,
                                ModuleStatus::Error,
                                Some(e.clone()),
                            );
                        }
                        let _ = app.emit(
                            "module://container-start-failed",
                            serde_json::json!({
                                "project_id": if is_global { None } else { Some(&project_id) },
                                "module_id": module_id,
                                "scope": if is_global { "global" } else { "per_project" },
                                "error": e,
                            }),
                        );
                        let _ = db.audit(
                            "module_container_start_failed",
                            if is_global { None } else { Some(&project_id) },
                            Some(&module_id),
                            &serde_json::json!({
                                "error": e,
                                "scope": if is_global { "global" } else { "per_project" },
                            }),
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
                    "project_id": if is_global { None } else { Some(&project_id) },
                    "module_id": module_id,
                    "success": true,
                    "scope": if is_global { "global" } else { "per_project" },
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
            if is_global {
                db.set_global_module_status(
                    &module_id,
                    ModuleStatus::Error,
                    Some(e.clone()),
                )?;
            } else {
                db.set_module_status(
                    &project_id,
                    &module_id,
                    ModuleStatus::Error,
                    Some(e.clone()),
                )?;
            }
            let _ = app.emit(
                "module://install-complete",
                serde_json::json!({
                    "project_id": if is_global { None } else { Some(&project_id) },
                    "module_id": module_id,
                    "success": false,
                    "scope": if is_global { "global" } else { "per_project" },
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
    // v0.2.45 V45-F: warm the L0 catalog before resolve_manifest_for_install
    // (V45-C) does its on-disk-vs-L0 version comparison.
    // resolve_install_metadata reads app_state[module_catalog.cache]
    // synchronously — if that row is empty/expired, V45-C's "L0 has newer
    // version" branch silently falls back to the on-disk manifest's version
    // (which is the WHOLE POINT we're trying to avoid for the "publish new
    // version, then run Update" flow).
    //
    // cached_module_catalog respects the 15-min TTL: this is a no-op if
    // the cache is already fresh (typical Modules-tab navigation case),
    // a single HTTP fetch + cache-write otherwise. Soft-fail
    // (`let _ = ...`) — any catalog-fetch error MUST NOT block the
    // per-project update; the resolve_manifest_for_install path will
    // fall back to whatever cache state exists (and the stale-fallback
    // inside cached_module_catalog itself also keeps the surface graceful
    // on transient network drops).
    //
    // Foundation for every future paid module: every update path goes
    // through this entry point, so freshness at the L0 layer is now a
    // pre-condition of the resolve step rather than a "if the user
    // happens to have visited Modules tab recently" lottery.
    let _ = crate::commands::module_catalog_client::cached_module_catalog(&db).await;

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
    //
    // v0.2.33 B2: update path uses the same three-phase resolver as
    // install. In practice the on-disk extracted manifest from the
    // PRIOR version is already present (we're updating, not first-
    // installing), so phase 1 wins. The L0-synth phase 3 is a safety
    // net for the "module_installs row exists but on-disk file went
    // missing" case (e.g. user manually rm'd ~/.vct/modules/<id>/) —
    // Agent C's reconciler should mark such rows broken at startup,
    // but if the row survived we'd rather drive update from L0 than
    // hard-fail.
    //
    // Note: the synth's version will be the L0 CURRENT version, which
    // is what we want for an update (`installer_engine::run_upgrade`
    // reads `manifest.version` to pick the new tag). The previous
    // version stays in `previous_install.module_version` for the
    // audit row.
    let _project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;
    let (manifest, manifest_source) = resolve_manifest_for_install(&db, &module_id)?;

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
            "manifest_source": manifest_source.as_audit_str(),
        }),
    )?;

    let ctx = PlaceholderCtx::new(&module_id);
    // v0.2.34 (Agent B): same freshness invariant as `install_module_for_project`
    // — a version upgrade is just as susceptible to the v0.2.20-style schema
    // gap as a first-time install, and a user updating from v0.2.7 to v0.2.8
    // on an RTX 4080 SUPER host with a stale snapshot would still pull the
    // wrong variant. Same soft-fail fallback chain: fresh probe → last-known
    // → `GpuMode::Cpu`.
    let gpu_mode =
        crate::commands::installer::ensure_fresh_hardware_snapshot_for_install(db.inner())
            .await
            .map(|snap| snap.gpu_mode_decided)
            .unwrap_or(crate::commands::gpu_policy::GpuMode::Cpu);

    // NEW-1 (2026-05-28): same L0 override as install_module_for_project.
    let l0_pull_token_endpoint: Option<String> = resolve_install_metadata(&db, &module_id)
        .ok()
        .map(|l0| l0.install.container.pull_token_endpoint);

    match installer_engine::run_upgrade(
        &app,
        &manifest,
        &previous_install,
        &ctx,
        &project_id,
        gpu_mode,
        &db,
        l0_pull_token_endpoint.as_deref(),
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
            // v0.2.34 Agent E (Phase 4 generalisation): reconcile the
            // tool_allowlist set on update too. v0.2.7 → v0.2.8 might
            // ADD or REMOVE tools — `reconcile_mcp_tool_defaults` does
            // delete-then-insert inside a transaction so the rows
            // match the new manifest.
            reconcile_module_tool_allowlist(&manifest, &module_id, &db);
            // v0.2.43 V0243-17: same post-reconcile forensic assertion as
            // the install path (see above). Log anomalies but never block.
            if let Some(mcp_reg) = manifest.mcp_registration.as_ref() {
                if let Some(allowlist) = mcp_reg.tool_allowlist.as_ref() {
                    if !allowlist.is_empty() {
                        match db.list_mcp_tool_defaults(&mcp_reg.mcp_name) {
                            Ok(defaults) if !defaults.is_empty() => {
                                eprintln!(
                                    "[v0.2.43/V0243-17] update: {} (mcp={}): {} row(s) in \
                                     module_mcp_tool_defaults (expected >= 1 for {} tool(s))",
                                    module_id, mcp_reg.mcp_name,
                                    defaults.len(), allowlist.len(),
                                );
                            }
                            Ok(_) => {
                                eprintln!(
                                    "[v0.2.43/V0243-17] WARN: update: {} (mcp={}): \
                                     tool_allowlist non-empty but 0 rows in \
                                     module_mcp_tool_defaults after reconcile",
                                    module_id, mcp_reg.mcp_name,
                                );
                                let _ = db.audit(
                                    "module_mcp_tool_defaults_empty_after_update",
                                    Some(&project_id),
                                    Some(&module_id),
                                    &serde_json::json!({
                                        "mcp_name": mcp_reg.mcp_name,
                                        "expected_tool_count": allowlist.len(),
                                        "actual_row_count": 0,
                                    }),
                                );
                            }
                            Err(e) => {
                                eprintln!(
                                    "[v0.2.43/V0243-17] update: {} (mcp={}): \
                                     list_mcp_tool_defaults failed: {}",
                                    module_id, mcp_reg.mcp_name, e,
                                );
                            }
                        }
                    }
                }
            }
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
    // v0.2.49 Stream A: check for a GLOBAL row first. If a module is
    // installed as global (project_id IS NULL), the per-project lookup
    // returns None even when the module is plainly installed; we route
    // through the global accessor instead. The caller may pass any
    // project_id (typically the current one); for global rows it's
    // informational only — global uninstalls are machine-wide.
    if let Some(global_row) = db.get_global_module_install(&module_id)? {
        return uninstall_global_module(global_row, module_id, purge_data, &db).await;
    }

    let row = db
        .get_module_install(&project_id, &module_id)?
        .ok_or_else(|| format!("module {} not installed for project {}", module_id, project_id))?;

    // Look up the manifest. On miss, fall back to legacy hardcoded behaviour
    // with a warning — never fail the uninstall over a missing manifest.
    //
    // v0.2.33: prefer the extracted post-install manifest at
    // `~/.vct/modules/<id>/vct-module.json` (`find_installed_manifest`),
    // with the same dev-affordance fallback as `install_path_manifest_lookup`
    // so a dev uninstalling from the co-located paid-modules clone
    // still gets `UninstallBlock` honoured.
    let manifest_opt = match install_path_manifest_lookup(&db, &module_id) {
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
    // v0.2.34 Agent E (Phase 4 generalisation): drop any MCP tool
    // defaults this module owned. Per-project overrides in
    // `project_mcp_tool_grants` are LEFT IN PLACE — they belong to the
    // project, not the module, and may apply again if the module is
    // reinstalled.
    if let Err(e) = db.clear_mcp_tool_defaults_for_module(&module_id) {
        eprintln!(
            "[uninstall] clear_mcp_tool_defaults_for_module({}) failed: {}",
            module_id, e
        );
    }
    // v0.2.49 Stream B: drop every per-project `enabled_for_project`
    // row for this module so a future reinstall starts clean (no stale
    // `false` lingering for a project that disabled the module). We
    // call this unconditionally — the helper short-circuits to 0 deletes
    // when the module had no rows (project-scope modules don't get the
    // toggle), so there's no need to gate on `install_scope_is_global()`
    // here. Idempotent and safe even when the manifest is missing (which
    // is why `clear_module_settings` above already runs unconditionally).
    let cleared_enable_rows = crate::commands::module_enabled
        ::clear_enabled_rows_for_uninstalled_module(&db, &module_id);
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
            "cleared_enabled_for_project_rows": cleared_enable_rows,
        }),
    )?;
    Ok(())
}

/// v0.2.49 Stream A: uninstall a GLOBAL-scope module.
///
/// Mirrors `uninstall_module_v2` for the per-project case but routes
/// every DB read/write through the global accessors AND removes the
/// single bare-id container instead of N per-project containers. Audit
/// log uses `project_id = None` to flag the global scope.
async fn uninstall_global_module(
    row: crate::db::models::ModuleInstallRow,
    module_id: String,
    purge_data: bool,
    db: &Db,
) -> Result<(), String> {
    // Look up the manifest. On miss, fall back to legacy hardcoded
    // behaviour (mirrors the per-project path).
    let manifest_opt = match install_path_manifest_lookup(db, &module_id) {
        Ok((m, _)) => Some(m),
        Err(e) => {
            eprintln!(
                "[uninstall] manifest for {} not in catalog ({}); falling back to legacy \
                 hardcoded behaviour (remove install_dir, no MCP deregister, no secret wipe).",
                module_id, e,
            );
            None
        }
    };

    let uninstall_block: UninstallBlock = manifest_opt
        .as_ref()
        .and_then(|m| m.uninstall.clone())
        .unwrap_or_else(default_uninstall_block);

    // Stop + remove the single global container (when present) BEFORE
    // dropping the install row.
    if let Some(container_name) = row.container_name.as_deref() {
        if !container_name.is_empty() {
            if let Err(e) = crate::commands::module_service::stop_container_for_project(
                container_name,
            )
            .await
            {
                eprintln!(
                    "[uninstall] global stop_container_for_project({}) failed: {}",
                    container_name, e
                );
            }
        }
    }

    let install_path = PathBuf::from(&row.install_path);
    let ctx = PlaceholderCtx::new(&module_id).with_install_dir(install_path.clone());

    if uninstall_block.remove_install_dir && install_path.exists() {
        let preserved =
            stash_preserve_paths(&install_path, &uninstall_block.preserve_paths, &ctx).await;
        if let Err(e) = tokio::fs::remove_dir_all(&install_path).await {
            eprintln!(
                "[uninstall] global remove_dir_all {}: {}",
                install_path.display(),
                e
            );
        }
        if !preserved.is_empty() {
            if let Err(e) = restore_preserved_paths(&install_path, preserved).await {
                eprintln!(
                    "[uninstall] global restore_preserved_paths failed: {}",
                    e
                );
            }
        }
    } else if !uninstall_block.remove_install_dir {
        eprintln!(
            "[uninstall] manifest.uninstall.remove_install_dir=false; leaving {} on disk.",
            install_path.display(),
        );
    }

    // MCP deregistration — same surface as per-project path.
    if uninstall_block.deregister_mcp {
        if let Some(mcp) = manifest_opt
            .as_ref()
            .and_then(|m| m.mcp_registration.as_ref())
        {
            if let Some(home) = directories::UserDirs::new() {
                let target = home.home_dir().join(".claude.json");
                if let Err(e) =
                    crate::mcp_registration::deregister_mcp(&target, &mcp.mcp_name)
                {
                    eprintln!(
                        "[uninstall] global deregister_mcp({}) failed: {}",
                        mcp.mcp_name, e
                    );
                }
            }
        }
    }

    // Secret cleanup — global secrets only (per-project secrets are
    // never attached to global modules). v0.2.49 Stream A scope: skip
    // per-project secret cleanup for global modules; future iteration
    // may add a sweep across every project_id when clear_secrets=true.
    if uninstall_block.clear_secrets {
        if let Some(manifest) = manifest_opt.as_ref() {
            for decl in &manifest.secrets {
                if decl.scope.as_str() == "global" {
                    if let Err(e) =
                        secrets::delete(SecretScope::Global, &module_id, &decl.key)
                    {
                        eprintln!(
                            "[uninstall] global secrets::delete({}/{}) failed: {}",
                            module_id, decl.key, e
                        );
                    }
                }
            }
        }
    }

    if purge_data {
        let data_dir = crate::paths::vct_root_dir().join("data").join(&module_id);
        if data_dir.exists() {
            let _ = tokio::fs::remove_dir_all(&data_dir).await;
        }
    }

    db.delete_global_module_install(&module_id)?;
    // module_settings for global modules: the `(project_id, module_id)`
    // settings can still exist per-project (Stream B's per-project
    // enable toggle). Clearing them on uninstall is delegated to
    // Stream B's path — global uninstall here drops the install row +
    // container only.
    //
    // TODO(stream-B): on global uninstall, sweep
    // `module_settings(project_id, module_id, 'enabled_for_project')`
    // for every project — but that's Stream B's surface.
    if let Err(e) = db.clear_mcp_tool_defaults_for_module(&module_id) {
        eprintln!(
            "[uninstall] global clear_mcp_tool_defaults_for_module({}) failed: {}",
            module_id, e
        );
    }
    db.audit(
        "module_uninstall",
        None,
        Some(&module_id),
        &serde_json::json!({
            "purge_data": purge_data,
            "remove_install_dir": uninstall_block.remove_install_dir,
            "preserve_paths_count": uninstall_block.preserve_paths.len(),
            "deregister_mcp": uninstall_block.deregister_mcp,
            "clear_secrets": uninstall_block.clear_secrets,
            "manifest_found": manifest_opt.is_some(),
            "scope": "global",
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

/// v0.2.34 Agent E (Phase 4 generalisation): write the manifest's
/// `mcp_registration.tool_allowlist` block into `module_mcp_tool_defaults`.
/// Soft-fail: errors are logged but never block the install/update.
///
/// When the manifest declares no `mcp_registration` block, or declares
/// one without a `tool_allowlist`, this helper short-circuits with no
/// DB write — preserves the v0.2.33 contract for modules that haven't
/// adopted the new field yet.
///
/// When the manifest DOES declare a tool_allowlist, reconcile fully
/// replaces the (mcp_name, module_id) slice in
/// `module_mcp_tool_defaults`: new tools land with their
/// `default_enabled` value, removed tools disappear, unchanged tools
/// are upserted in-place. Per-project overrides in
/// `project_mcp_tool_grants` are LEFT IN PLACE — they belong to the
/// project, not the module.
fn reconcile_module_tool_allowlist(
    manifest: &ModuleManifest,
    module_id: &str,
    db: &Db,
) {
    let mcp = match manifest.mcp_registration.as_ref() {
        Some(m) => m,
        None => return,
    };
    let allowlist = match mcp.tool_allowlist.as_ref() {
        Some(list) => list,
        None => return,
    };
    let now_ms = chrono::Utc::now().timestamp_millis();
    let entries: Vec<(String, bool, Option<String>)> = allowlist
        .iter()
        .map(|e| (e.tool.clone(), e.default_enabled, e.description.clone()))
        .collect();
    match db.reconcile_mcp_tool_defaults(&mcp.mcp_name, module_id, &entries, now_ms) {
        Ok(n) => {
            eprintln!(
                "[v0.2.34] reconciled {} MCP tool default(s) for {} (mcp={})",
                n, module_id, mcp.mcp_name
            );
        }
        Err(e) => {
            // Don't return the error — the install / update succeeded;
            // the defaults can be reconciled again next time.
            eprintln!(
                "[v0.2.34] reconcile_mcp_tool_defaults({}, {}) failed: {} \
                 (install/update continues; hub will fall back to hardcoded \
                  defaults until next reconcile)",
                mcp.mcp_name, module_id, e
            );
        }
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

    // v0.2.33 (Agent B, L0a): deleted —
    // `builtin_catalog_lists_rl_reranker_as_available_paid_module`. The
    // hardcoded vct-rl-reranker entry was removed from
    // `builtin_catalog_entries`; paid-module metadata now comes from L0
    // (`module_catalog_client::cached_module_catalog`). The new
    // `list_module_catalog_renders_l0_module_as_available_when_uninstalled`
    // test (further down) covers the same contract.

    // v0.2.33 (Agent B, L0a): deleted —
    // `catalog_matches_on_disk_manifest_when_present`. The pattern it
    // pinned (hardcoded builtin entry ↔ on-disk paid-modules manifest
    // agreement) no longer exists. Catalog metadata for paid modules
    // comes from L0; the on-disk manifest is post-install only. The
    // shape the test enforced is fundamentally obsolete.

    // v0.2.33 (Agent B, L0a): deleted the three v0.2.32 #A merge tests:
    //   - list_module_catalog_overrides_builtin_with_on_disk_manifest_version
    //   - list_module_catalog_overrides_builtin_with_live_license_resolution
    //   - list_module_catalog_preserves_builtin_kind_and_cta_route_for_overridden_entries
    // plus their `MERGE_TEST_LOCK` + `run_catalog_with_bundled_manifest`
    // helper. They exercised a shape (on-disk-manifest-overrides-builtin
    // merge loop) that no longer exists — catalog entries for paid
    // modules now come from L0 and the builtin set does NOT include a
    // RL placeholder.
    //
    // The v0.2.33 list_module_catalog tests below cover the equivalent
    // contracts via the new L0-driven path:
    //   * list_module_catalog_renders_l0_module_as_available_when_uninstalled
    //   * list_module_catalog_admin_tier_paid_module_is_licensed
    //   * list_module_catalog_free_tier_paid_module_is_not_licensed
    //   * list_module_catalog_renders_update_available_when_l0_newer_than_installed
    //   * etc.

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

    // ─── v0.2.33 (Agent B, L0a) — L0-driven list_module_catalog tests ─────
    //
    // The new tests exercise `list_module_catalog_impl_with_l0` directly:
    // it takes an already-resolved L0 outcome (so we can pass mock
    // envelopes — including Ok(empty), Ok(populated), Err) and produces
    // the full `CatalogResponse`. No HTTP server stand-up needed.
    //
    // Tests serialize on `CATALOG_TEST_LOCK` because some of them set
    // `VCT_STATE_DIR` + `VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH` env
    // vars; those mutations are process-wide.

    use crate::commands::module_catalog_client::{
        L0CatalogModule, L0CatalogResponse, L0Compatibility, L0Install,
        L0InstallContainer,
    };

    static CATALOG_TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    /// Build a canonical L0CatalogModule for vct-rl-reranker at the given
    /// version. Used by every L0-driven test below to keep the fixture
    /// shape consistent.
    fn fake_l0_rl(version: &str) -> L0CatalogModule {
        L0CatalogModule {
            id: "vct-rl-reranker".into(),
            name: "RL Reranker".into(),
            version: version.into(),
            description: "RL-based reranker".into(),
            category: "paid-independent".into(),
            tags: vec!["pro".into()],
            homepage: String::new(),
            publisher: String::new(),
            license_required: true,
            min_orchestrator_tier: "pro".into(),
            // Non-empty so `is_module_licensed_v2` doesn't fall through
            // to its variant_ids.is_empty() "treat as free" branch —
            // pinning is_licensed=false for free-tier users.
            license_variant_ids: vec!["fake-variant".into()],
            trial_days: None,
            compatibility: L0Compatibility {
                hosts: vec!["base".into(), "mao".into(), "orchestrator_root".into()],
                min_launcher_version: None,
            },
            install: L0Install {
                method: "container_pull".into(),
                container: L0InstallContainer {
                    image: "ghcr.io/hotak92/vct-rl-reranker".into(),
                    tag_from_version: true,
                    registry: Some("ghcr.io".into()),
                    pull_token_endpoint: "https://example/pull-token".into(),
                    pull_token_method: "POST".into(),
                },
                scope: crate::manifest::InstallScope::PerProject,
            },
            requirements: None,
            runtime_hints: None,
            deprecated: false,
            deprecation_message: String::new(),
            deprecation_eol_date: String::new(),
            deprecation_migration_url: String::new(),
            post_install_manifest_path: "vct-module.json".into(),
        }
    }

    fn ok_envelope(modules: Vec<L0CatalogModule>) -> Result<L0CatalogResponse, String> {
        Ok(L0CatalogResponse {
            schema_version: 1,
            fetched_at: "2026-05-25T00:00:00Z".into(),
            modules,
        })
    }

    /// Acquire the catalog-test mutex AND redirect `VCT_STATE_DIR` to a
    /// fresh tempdir. Returns the lock guard + tempdir; the caller drops
    /// both at end-of-test to clean up.
    fn isolate_state() -> (
        std::sync::MutexGuard<'static, ()>,
        tempfile::TempDir,
        Option<String>,
        Option<String>,
    ) {
        // Poison-tolerant lock acquisition: if a prior test panicked
        // mid-test, the lock is poisoned but the data inside (unit
        // tuple) is intact — we just take the guard anyway.
        let lock = CATALOG_TEST_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let tmp = tempfile::tempdir().expect("tempdir");
        let prev_state = std::env::var("VCT_STATE_DIR").ok();
        std::env::set_var("VCT_STATE_DIR", tmp.path());
        let prev_dev = std::env::var("VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH").ok();
        std::env::remove_var("VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH");
        (lock, tmp, prev_state, prev_dev)
    }

    fn restore_env(prev_state: Option<String>, prev_dev: Option<String>) {
        match prev_state {
            Some(v) => std::env::set_var("VCT_STATE_DIR", v),
            None => std::env::remove_var("VCT_STATE_DIR"),
        }
        match prev_dev {
            Some(v) => std::env::set_var("VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH", v),
            None => std::env::remove_var("VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH"),
        }
    }

    /// Seed a project + an installed module_install row so the catalog's
    /// "kind from module_installs" branch has something to read.
    fn seed_install(db: &Db, module_id: &str, version: &str, status: ModuleStatus) -> String {
        let project_id = format!("proj-{}", module_id);
        db.insert_project(
            &project_id,
            "Test Project",
            "/tmp/test",
            ProjectHost::Base,
            "test-project",
        )
        .expect("insert project");
        let install_id = uuid::Uuid::new_v4().to_string();
        db.insert_module_install(
            &install_id,
            &project_id,
            module_id,
            version,
            &format!("/tmp/install/{}", module_id),
        )
        .expect("insert install");
        db.set_module_install_status(&install_id, status.as_str())
            .expect("flip status");
        project_id
    }

    /// Test 1: L0 returns empty modules list → catalog renders only the
    /// 4 builtin entries. No paid modules surface.
    #[test]
    fn list_module_catalog_returns_only_builtins_when_l0_empty() {
        let (_lock, _tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();
        let response = list_module_catalog_impl_with_l0(&db, ok_envelope(Vec::new()));

        // Builtin set: vct-launcher, orchestrator, knowledge-graph, code-graph.
        let ids: Vec<&str> = response.modules.iter().map(|e| e.id.as_str()).collect();
        assert!(ids.contains(&"vct-launcher"), "missing launcher: {:?}", ids);
        assert!(ids.contains(&"orchestrator"), "missing orchestrator: {:?}", ids);
        assert!(ids.contains(&"knowledge-graph"), "missing KG: {:?}", ids);
        assert!(ids.contains(&"code-graph"), "missing code-graph: {:?}", ids);
        // No vct-rl-reranker (placeholder removed in v0.2.33).
        assert!(
            !ids.contains(&"vct-rl-reranker"),
            "vct-rl-reranker placeholder must NOT be present when L0 is empty; \
             v0.2.33 removed the hardcoded entry"
        );
        // L0 status is Ok with 0 modules.
        match response.l0_status {
            L0Status::Ok { modules_count, .. } => assert_eq!(modules_count, 0),
            other => panic!("expected Ok status, got {:?}", other),
        }

        restore_env(prev_state, prev_dev);
    }

    /// Test 2: L0 returns RL with version=0.2.7, no install row →
    /// kind=available, version=0.2.7.
    #[test]
    fn list_module_catalog_renders_l0_module_as_available_when_uninstalled() {
        let (_lock, _tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();
        let response = list_module_catalog_impl_with_l0(
            &db,
            ok_envelope(vec![fake_l0_rl("0.2.7")]),
        );

        let rl = response
            .modules
            .iter()
            .find(|e| e.id == "vct-rl-reranker")
            .expect("vct-rl-reranker must be in catalog after L0 advertises it");
        assert_eq!(rl.kind, "available", "no install row → kind=available");
        assert_eq!(rl.version, "0.2.7", "version must come from L0");
        assert_eq!(rl.min_orchestrator_tier, "pro");
        assert!(rl.license_required);
        assert_eq!(rl.compatibility_hosts.len(), 3);
        // manifest_source must reflect L0, not a file path.
        assert!(
            rl.manifest_source.starts_with("L0:"),
            "manifest_source should be 'L0:<id>' for L0-sourced entries; got {:?}",
            rl.manifest_source,
        );

        restore_env(prev_state, prev_dev);
    }

    /// Test 3: admin tier + L0 paid module → is_licensed=true (L10 regression).
    #[test]
    fn list_module_catalog_admin_tier_paid_module_is_licensed() {
        let (_lock, _tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();
        db.set_tier_cache("admin", &serde_json::json!({}), None)
            .expect("set admin tier");

        let response = list_module_catalog_impl_with_l0(
            &db,
            ok_envelope(vec![fake_l0_rl("0.2.7")]),
        );

        let rl = response
            .modules
            .iter()
            .find(|e| e.id == "vct-rl-reranker")
            .expect("vct-rl-reranker must be present");
        assert!(
            rl.is_licensed,
            "admin tier must auto-license paid modules; pre-v0.2.33 \
             hardcoded is_licensed=false shadowed the live check, so admin \
             users saw 'Activate License' on a module they had universal \
             access to (L2/L10 regression guard)"
        );

        restore_env(prev_state, prev_dev);
    }

    /// Test 4: free tier + L0 paid module → is_licensed=false → button
    /// reads "Activate license" on the renderer side.
    #[test]
    fn list_module_catalog_free_tier_paid_module_is_not_licensed() {
        let (_lock, _tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();
        db.set_tier_cache("free", &serde_json::json!({}), None)
            .expect("set free tier");

        let response = list_module_catalog_impl_with_l0(
            &db,
            ok_envelope(vec![fake_l0_rl("0.2.7")]),
        );

        let rl = response
            .modules
            .iter()
            .find(|e| e.id == "vct-rl-reranker")
            .expect("vct-rl-reranker must be present");
        assert!(
            !rl.is_licensed,
            "free tier must NOT unlock a pro-tier paid module; if this \
             fails the activation flow won't fire and the user can't \
             install"
        );
        assert!(rl.license_required, "L0 says license_required=true");

        restore_env(prev_state, prev_dev);
    }

    /// Test 5: installed v0.2.7, L0 v0.2.8 → kind=update_available.
    #[test]
    fn list_module_catalog_renders_update_available_when_l0_newer_than_installed() {
        let (_lock, _tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();
        let _ = seed_install(&db, "vct-rl-reranker", "0.2.7", ModuleStatus::Installed);

        let response = list_module_catalog_impl_with_l0(
            &db,
            ok_envelope(vec![fake_l0_rl("0.2.8")]),
        );

        let rl = response
            .modules
            .iter()
            .find(|e| e.id == "vct-rl-reranker")
            .expect("entry present");
        assert_eq!(rl.kind, "update_available", "L0 newer than installed");
        assert_eq!(
            rl.version, "0.2.7",
            "the catalog version reports the installed version; the renderer \
             reads it from the install row and compares to L0's latest"
        );

        restore_env(prev_state, prev_dev);
    }

    /// Test 6: installed v0.2.7, L0 v0.2.7 → kind=installed.
    #[test]
    fn list_module_catalog_renders_installed_when_versions_match() {
        let (_lock, _tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();
        let _ = seed_install(&db, "vct-rl-reranker", "0.2.7", ModuleStatus::Installed);

        let response = list_module_catalog_impl_with_l0(
            &db,
            ok_envelope(vec![fake_l0_rl("0.2.7")]),
        );

        let rl = response
            .modules
            .iter()
            .find(|e| e.id == "vct-rl-reranker")
            .expect("entry present");
        assert_eq!(rl.kind, "installed", "versions match → installed");
        assert_eq!(rl.version, "0.2.7");

        restore_env(prev_state, prev_dev);
    }

    /// Test 7: install row with status='broken' → kind=broken (reconciler
    /// already flipped the row; catalog must reflect it).
    #[test]
    fn list_module_catalog_renders_broken_when_status_is_broken() {
        let (_lock, _tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();
        let _ = seed_install(&db, "vct-rl-reranker", "0.2.7", ModuleStatus::Broken);

        let response = list_module_catalog_impl_with_l0(
            &db,
            ok_envelope(vec![fake_l0_rl("0.2.7")]),
        );

        let rl = response
            .modules
            .iter()
            .find(|e| e.id == "vct-rl-reranker")
            .expect("entry present");
        assert_eq!(
            rl.kind, "broken",
            "module_installs.status='broken' (reconciler flipped) must \
             surface as kind='broken' so the renderer shows the Reinstall \
             CTA"
        );

        restore_env(prev_state, prev_dev);
    }

    /// Test 8: L0 unreachable with no cache → l0_status=Unavailable, only
    /// builtins in the modules list, no panic.
    #[test]
    fn list_module_catalog_handles_l0_unavailable_with_no_cache() {
        let (_lock, _tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();
        let response = list_module_catalog_impl_with_l0(
            &db,
            Err("network: connection refused".into()),
        );

        match response.l0_status {
            L0Status::Unavailable { error } => {
                assert!(error.contains("connection refused"), "must carry the underlying error: {}", error);
            }
            other => panic!("expected Unavailable, got {:?}", other),
        }
        // Builtins still render.
        let ids: Vec<&str> = response.modules.iter().map(|e| e.id.as_str()).collect();
        assert!(ids.contains(&"vct-launcher"));
        assert!(ids.contains(&"orchestrator"));
        // No paid modules.
        assert!(!ids.contains(&"vct-rl-reranker"));

        restore_env(prev_state, prev_dev);
    }

    /// Test 9: L0 unreachable with stale cache present → the client
    /// returns the stale value as Ok, so this test verifies that
    /// when the L0 layer surfaces stale-as-Ok, we render the stale
    /// modules + the renderer treats it as Stale (rather than Ok).
    ///
    /// Note: the actual "stale" classification lives in
    /// `module_catalog_client::cached_module_catalog`; the
    /// `_impl_with_l0` boundary above only sees Ok/Err. We pin the
    /// integration by asserting: if Ok(envelope) is passed, the
    /// modules render (regardless of whether the cache was stale).
    /// The "stale-fallback returns Ok" contract is tested in
    /// module_catalog_client.rs's own tests.
    #[test]
    fn list_module_catalog_handles_l0_unavailable_with_stale_cache() {
        let (_lock, _tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();
        // Simulate `cached_module_catalog` returning Ok with a stale
        // envelope (the client layer would have already classified the
        // result and surfaced it as Ok, see the cache layer test in
        // module_catalog_client::tests).
        let response = list_module_catalog_impl_with_l0(
            &db,
            ok_envelope(vec![fake_l0_rl("0.2.6")]), // stale cached version
        );

        // Modules from the stale cache STILL render — this is the
        // user-visible contract: a slightly-stale catalog is better
        // than an empty one. The l0_status reads Ok here because the
        // client's stale-fallback layer remaps stale-but-served to Ok
        // (with the older fetched_at) — that mapping is verified in
        // module_catalog_client's own tests.
        let rl = response
            .modules
            .iter()
            .find(|e| e.id == "vct-rl-reranker")
            .expect("stale cache contents must still render");
        assert_eq!(rl.version, "0.2.6");

        restore_env(prev_state, prev_dev);
    }

    /// Test 10: installed row for a module NOT in L0 → render as
    /// kind=installed + catalog_warning explaining "no longer available".
    #[test]
    fn list_module_catalog_includes_uninstalled_legacy_module_with_warning() {
        let (_lock, _tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();
        // Install a hypothetical legacy module that L0 no longer lists.
        let _ = seed_install(&db, "vct-legacy", "0.1.0", ModuleStatus::Installed);

        let response = list_module_catalog_impl_with_l0(
            &db,
            ok_envelope(vec![fake_l0_rl("0.2.7")]), // legacy NOT in L0
        );

        let legacy = response
            .modules
            .iter()
            .find(|e| e.id == "vct-legacy")
            .expect("legacy module still in catalog because module_installs has it");
        assert_eq!(legacy.kind, "installed");
        assert!(
            !legacy.catalog_warning.is_empty(),
            "legacy installed module must carry a catalog_warning so the \
             renderer can show the 'no longer available' badge"
        );
        assert!(
            legacy.catalog_warning.to_lowercase().contains("no longer")
                || legacy.catalog_warning.to_lowercase().contains("not in"),
            "warning text must signal removal: {:?}",
            legacy.catalog_warning
        );

        restore_env(prev_state, prev_dev);
    }

    /// Test 11: `resolve_install_metadata` reads from the L0 cache and
    /// returns the install slice.
    #[test]
    fn resolve_install_metadata_returns_l0_slice() {
        let (_lock, _tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();
        // Seed the catalog cache the way `cached_module_catalog` would.
        let envelope = L0CatalogResponse {
            schema_version: 1,
            fetched_at: "2026-05-25T00:00:00Z".into(),
            modules: vec![fake_l0_rl("0.2.7")],
        };
        let serialized = serde_json::to_string(&envelope).unwrap();
        db.app_state_set(
            crate::commands::module_catalog_client::APP_STATE_KEY_CATALOG,
            &serialized,
        )
        .expect("write cache");

        let slice = resolve_install_metadata(&db, "vct-rl-reranker")
            .expect("must find the L0 slice when cache is populated");
        assert_eq!(slice.id, "vct-rl-reranker");
        assert_eq!(slice.version, "0.2.7");
        assert_eq!(slice.install.container.image, "ghcr.io/hotak92/vct-rl-reranker");

        let missing = resolve_install_metadata(&db, "vct-not-in-l0");
        assert!(missing.is_err(), "unknown module must Err");

        restore_env(prev_state, prev_dev);
    }

    /// Test 12: `find_installed_manifest` reads `~/.vct/modules/<id>/`.
    #[test]
    fn find_installed_manifest_reads_on_disk_path() {
        let (_lock, tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();
        // Place a minimal valid manifest at the expected path.
        let module_dir = tmp.path().join("modules").join("vct-test-installed");
        std::fs::create_dir_all(&module_dir).unwrap();
        let manifest_json = serde_json::json!({
            "manifest_version": 1,
            "id": "vct-test-installed",
            "name": "Test Installed",
            "version": "0.3.0",
            "description": "fixture",
            "category": "paid-independent",
            "license": {"required": false, "min_orchestrator_tier": "free"},
            "compatibility": {"hosts": ["base"]},
            "install": {"method": "container_pull"},
            "runtime": {"type": "service", "command": "echo", "args": []}
        });
        std::fs::write(
            module_dir.join("vct-module.json"),
            manifest_json.to_string(),
        )
        .unwrap();

        let (manifest, path) = find_installed_manifest(&db, "vct-test-installed")
            .expect("must find on-disk manifest");
        assert_eq!(manifest.id, "vct-test-installed");
        assert_eq!(manifest.version, "0.3.0");
        assert!(path.ends_with("vct-module.json"));

        restore_env(prev_state, prev_dev);
    }

    /// Test 13: `find_installed_manifest` returns Err when the on-disk
    /// file is missing.
    #[test]
    fn find_installed_manifest_returns_err_when_missing() {
        let (_lock, _tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();
        let res = find_installed_manifest(&db, "vct-never-installed");
        assert!(
            res.is_err(),
            "missing on-disk manifest must Err so callers can surface \
             a parse_errors entry to the renderer"
        );
        let msg = res.unwrap_err();
        assert!(
            msg.contains("no installed manifest"),
            "error must signal missing-manifest: {}",
            msg
        );

        restore_env(prev_state, prev_dev);
    }

    /// Plant a synthetic install-root under `<tmp>/install_root/` that
    /// passes `installer::check_install_status` (so the resolver caches
    /// it and `paid_modules_dir_exists` can find the paid-modules dir).
    /// Returns the install_root PathBuf.
    fn plant_install_root(tmp_dir: &std::path::Path, db: &Db) -> PathBuf {
        let install_root = tmp_dir.join("install_root");
        std::fs::create_dir_all(install_root.join("paid-modules")).unwrap();
        // check_install_status requires both files + a satisfied
        // manifest OR a .venv/ — we go with the .venv fallback because
        // it's the cheaper path (no JSON to serialise).
        std::fs::write(install_root.join("CLAUDE.md"), "# stub").unwrap();
        std::fs::write(install_root.join("install.py"), "# stub").unwrap();
        std::fs::create_dir_all(install_root.join(".venv")).unwrap();
        db.app_state_set("launcher.install_path", install_root.to_str().unwrap())
            .unwrap();
        install_root
    }

    /// Test 14: dev affordance hint fires when paid-modules/ exists but
    /// the env var isn't set.
    #[test]
    fn dev_affordance_hint_emitted_when_paid_modules_exists_without_env_var() {
        let (_lock, tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();
        // Create the dev paid-modules dir under a synthetic install_root.
        let _install_root = plant_install_root(tmp.path(), &db);

        // Env var NOT set (isolate_state cleared it).
        assert!(!dev_catalog_passthrough_enabled());

        let response = list_module_catalog_impl_with_l0(&db, ok_envelope(Vec::new()));
        let hint = response
            .dev_affordance_hint
            .as_ref()
            .expect("hint must fire when paid-modules/ exists + env var unset + not dismissed");
        assert_eq!(hint.env_var_name, "VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH");
        assert!(hint.paid_modules_path.ends_with("paid-modules"));

        restore_env(prev_state, prev_dev);
    }

    /// Test 15: dev affordance hint is suppressed after dismissal.
    #[test]
    fn dev_affordance_hint_suppressed_after_dismissal() {
        let (_lock, tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();
        let _install_root = plant_install_root(tmp.path(), &db);

        // Mark dismissed via the same app_state key the Tauri command writes.
        db.app_state_set(APP_STATE_KEY_DEV_AFFORDANCE_DISMISSED, "true")
            .unwrap();

        let response = list_module_catalog_impl_with_l0(&db, ok_envelope(Vec::new()));
        assert!(
            response.dev_affordance_hint.is_none(),
            "after dismissal the hint must NOT re-fire on subsequent catalog reads"
        );

        restore_env(prev_state, prev_dev);
    }

    /// Test 16: dev paid-modules scan only runs when env var is set, and
    /// merges with L0 results (dev WINS for same module_id).
    #[test]
    fn dev_paid_modules_scan_only_runs_with_env_var_set() {
        let (_lock, tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();

        // Plant a synthetic install-root with a dev manifest.
        let install_root = plant_install_root(tmp.path(), &db);
        let dev_dir = install_root.join("paid-modules").join("vct-rl-reranker");
        std::fs::create_dir_all(&dev_dir).unwrap();
        let dev_manifest = serde_json::json!({
            "manifest_version": 1,
            "id": "vct-rl-reranker",
            "name": "RL Reranker (DEV)",
            "version": "9.9.9",
            "description": "dev fixture",
            "category": "paid-independent",
            "license": {"required": true, "min_orchestrator_tier": "pro"},
            "compatibility": {"hosts": ["base", "mao", "orchestrator_root"]},
            "install": {
                "method": "container_pull",
                "container": {
                    "image": "ghcr.io/hotak92/vct-rl-reranker",
                    "pull_token_endpoint": "https://example/token"
                }
            },
            "runtime": {"type": "service", "command": "echo", "args": []}
        });
        std::fs::write(dev_dir.join("vct-module.json"), dev_manifest.to_string()).unwrap();

        // Phase 1: env var UNSET → only L0 results visible.
        std::env::remove_var("VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH");
        let response_off = list_module_catalog_impl_with_l0(
            &db,
            ok_envelope(vec![fake_l0_rl("0.2.7")]),
        );
        let rl_off = response_off
            .modules
            .iter()
            .find(|e| e.id == "vct-rl-reranker")
            .expect("L0 entry present");
        assert_eq!(
            rl_off.version, "0.2.7",
            "with env var unset, the L0 version wins; dev paid-modules \
             must NOT bleed into production behaviour"
        );

        // Phase 2: env var ON → dev manifest wins for the same id.
        std::env::set_var("VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH", "1");
        let response_on = list_module_catalog_impl_with_l0(
            &db,
            ok_envelope(vec![fake_l0_rl("0.2.7")]),
        );
        let rl_on = response_on
            .modules
            .iter()
            .find(|e| e.id == "vct-rl-reranker")
            .expect("entry present");
        assert_eq!(
            rl_on.version, "9.9.9",
            "with passthrough on, the dev manifest overrides the L0 \
             record for the same module_id — explicit opt-in semantics"
        );

        restore_env(prev_state, prev_dev);
    }

    // ─── v0.2.33 B2: cold-start install resolver integration ────────────
    //
    // These three tests pin the three-phase manifest resolution
    // contract that closes the G-J3-a gap from the v0.2.33 architecture
    // review. They exercise `resolve_manifest_for_install` directly —
    // the unit under test — rather than spinning up
    // `installer_engine::run_install` (which would try to invoke
    // podman). The boundary is deliberate: the resolver hands the
    // synthesized manifest to the installer engine; what the engine
    // does with it is covered by the engine's own tests.

    /// Test 5 (cold-start): no on-disk manifest, no dev passthrough,
    /// but the L0 cache holds a valid entry → resolver returns the
    /// synthesised manifest tagged `ManifestSource::L0Synth`. This is
    /// the load-bearing case for the G-J3-a fix: a real-user machine
    /// installing a paid module for the first time, the manifest is
    /// inside the (unpulled) image, only the L0 install-slice is on
    /// hand.
    #[test]
    fn cold_start_install_flow_uses_l0_synth_when_no_installed_or_dev_manifest() {
        let (_lock, _tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();

        // Seed the catalog cache the way `cached_module_catalog` would
        // after a successful fetch from the L0 edge function.
        let envelope = L0CatalogResponse {
            schema_version: 1,
            fetched_at: "2026-05-25T00:00:00Z".into(),
            modules: vec![fake_l0_rl("0.2.7")],
        };
        db.app_state_set(
            crate::commands::module_catalog_client::APP_STATE_KEY_CATALOG,
            &serde_json::to_string(&envelope).unwrap(),
        )
        .expect("seed catalog cache");

        // Pre-condition checks: phase 1 (on-disk) must MISS, phase 2
        // (dev) must MISS. We've redirected VCT_STATE_DIR to a fresh
        // tempdir via isolate_state, so `~/.vct/modules/` is empty
        // under that prefix. And isolate_state cleared the dev env var.
        assert!(
            find_installed_manifest(&db, "vct-rl-reranker").is_err(),
            "test pre-cond: no on-disk manifest"
        );
        assert!(
            !dev_catalog_passthrough_enabled(),
            "test pre-cond: dev passthrough OFF"
        );

        // Exercise the resolver.
        let (manifest, source) =
            resolve_manifest_for_install(&db, "vct-rl-reranker")
                .expect("L0-synth must succeed when catalog cache is populated");
        assert_eq!(source, ManifestSource::L0Synth, "must take phase 3");

        // The synthesised manifest carries the L0 install-slice — enough
        // for installer_engine::run_install to drive container_pull.
        // Phase 3 of `installer_engine::run_install_inner` would consume
        // these fields IF we proceeded to actually pull (we don't —
        // that'd require podman).
        assert_eq!(manifest.id, "vct-rl-reranker");
        assert_eq!(manifest.version, "0.2.7");
        assert_eq!(
            manifest.install.method,
            crate::manifest::InstallMethod::ContainerPull
        );
        let c = manifest
            .install
            .container
            .as_ref()
            .expect("synth carries install.container");
        assert_eq!(c.image, "ghcr.io/hotak92/vct-rl-reranker");
        assert!(c.tag_from_version);
        // License gate is_module_licensed (called immediately after the
        // resolver in install_module_for_project) reads these — pinning
        // them confirms the gate sees the L0-derived values, not some
        // accidental builtin default.
        assert!(manifest.license.required);
        assert_eq!(manifest.license.min_orchestrator_tier, "pro");

        // Audit-string format must encode the cold-start path so
        // post-incident triage can identify L0-synth installs.
        assert_eq!(source.as_audit_str(), "l0-synth");

        restore_env(prev_state, prev_dev);
    }

    /// Test 6 (re-install preference, v0.2.45 V45-C amended):
    ///
    /// Originally this test pinned "on-disk wins unconditionally even when
    /// L0 is newer", on the rationale that re-install must NOT silently
    /// morph into an upgrade. V45-C reverses that contract for the
    /// strictly-newer case: when L0 advertises a strictly newer semver
    /// (e.g. catalog refresh moved RL Reranker from 0.2.7 → 0.2.8), the
    /// retry-install path is expected to pull the new version — that's
    /// what the user clicked Install for.
    ///
    /// This test now pins the EQUAL-versions branch (L0 stale or unchanged
    /// relative to on-disk): on-disk still wins so a re-install of the
    /// same version doesn't re-pull. The strictly-newer branch is covered
    /// by `test_v0245_l0_wins_when_strictly_newer` below.
    #[test]
    fn cold_start_install_prefers_installed_manifest_when_l0_not_newer() {
        let (_lock, tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();

        // Seed L0 cache with v0.2.7 (SAME as what's on disk → on-disk wins).
        let envelope = L0CatalogResponse {
            schema_version: 1,
            fetched_at: "2026-05-25T00:00:00Z".into(),
            modules: vec![fake_l0_rl("0.2.7")],
        };
        db.app_state_set(
            crate::commands::module_catalog_client::APP_STATE_KEY_CATALOG,
            &serde_json::to_string(&envelope).unwrap(),
        )
        .unwrap();

        // Plant an installed manifest at v0.2.7 under the
        // VCT_STATE_DIR-redirected modules tree. The contents mirror
        // what Agent C's extract step would have written after a real
        // pull.
        let module_dir = tmp.path().join("modules").join("vct-rl-reranker");
        std::fs::create_dir_all(&module_dir).unwrap();
        let installed_manifest = serde_json::json!({
            "manifest_version": 1,
            "id": "vct-rl-reranker",
            "name": "RL Reranker",
            "version": "0.2.7",
            "description": "installed-on-disk fixture",
            "category": "paid-independent",
            "license": {
                "required": true,
                "variant_ids": ["x"],
                "min_orchestrator_tier": "pro"
            },
            "compatibility": {"hosts": ["base", "mao", "orchestrator_root"]},
            "install": {
                "method": "container_pull",
                "container": {
                    "image": "ghcr.io/hotak92/vct-rl-reranker",
                    "pull_token_endpoint": "https://example/pull-token"
                }
            },
            "runtime": {"type": "container", "command": ""}
        });
        std::fs::write(
            module_dir.join("vct-module.json"),
            installed_manifest.to_string(),
        )
        .unwrap();

        let (manifest, source) =
            resolve_manifest_for_install(&db, "vct-rl-reranker")
                .expect("must resolve");
        // Phase 1 wins — must NOT be L0Synth.
        match &source {
            ManifestSource::Installed(p) => {
                assert!(
                    p.ends_with("vct-module.json"),
                    "installed source path must point at the extract: {}",
                    p.display()
                );
            }
            other => panic!(
                "expected ManifestSource::Installed (L0 = on_disk), got \
                 {:?} — phase-1 preference is broken (would silently \
                 turn a re-install into a no-op re-pull against the \
                 user's expectations)",
                other,
            ),
        }
        // The version MUST be the on-disk v0.2.7. If this ever fails it
        // means phase 3 leaked in front of phase 1 even for equal versions.
        assert_eq!(
            manifest.version, "0.2.7",
            "on-disk version must win when L0 is equal — re-install \
             must not synthesize when nothing has changed"
        );

        restore_env(prev_state, prev_dev);
    }

    /// Test 7 (dev preference): when the on-disk manifest is absent BUT
    /// the dev paid-modules/ exists AND `VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH=1`,
    /// the dev manifest wins (phase 2). Without the env var, phase 2 is
    /// skipped entirely and phase 3 (L0-synth) takes over — that branch
    /// is covered by test 5. This test pins the dev-preference contract
    /// specifically.
    #[test]
    fn cold_start_install_prefers_dev_paid_modules_when_env_var_set() {
        let (_lock, tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();

        // Seed L0 cache (would be phase 3 if dev didn't win).
        let envelope = L0CatalogResponse {
            schema_version: 1,
            fetched_at: "2026-05-25T00:00:00Z".into(),
            modules: vec![fake_l0_rl("0.2.7")],
        };
        db.app_state_set(
            crate::commands::module_catalog_client::APP_STATE_KEY_CATALOG,
            &serde_json::to_string(&envelope).unwrap(),
        )
        .unwrap();

        // Plant a dev paid-modules entry with a sentinel version that
        // we can distinguish from the L0 version (0.2.7 → 9.9.9).
        let install_root = plant_install_root(tmp.path(), &db);
        let dev_dir = install_root.join("paid-modules").join("vct-rl-reranker");
        std::fs::create_dir_all(&dev_dir).unwrap();
        let dev_manifest = serde_json::json!({
            "manifest_version": 1,
            "id": "vct-rl-reranker",
            "name": "RL Reranker (DEV)",
            "version": "9.9.9",
            "description": "dev paid-modules fixture",
            "category": "paid-independent",
            "license": {
                "required": true,
                "variant_ids": ["x"],
                "min_orchestrator_tier": "pro"
            },
            "compatibility": {"hosts": ["base", "mao", "orchestrator_root"]},
            "install": {
                "method": "container_pull",
                "container": {
                    "image": "ghcr.io/hotak92/vct-rl-reranker",
                    "pull_token_endpoint": "https://example/pull-token"
                }
            },
            "runtime": {"type": "container", "command": ""}
        });
        std::fs::write(
            dev_dir.join("vct-module.json"),
            dev_manifest.to_string(),
        )
        .unwrap();

        // Phase A: env var UNSET → phase 2 skipped, phase 3 (L0-synth)
        // takes over and we'd see v0.2.7.
        std::env::remove_var("VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH");
        let (manifest_off, source_off) =
            resolve_manifest_for_install(&db, "vct-rl-reranker")
                .expect("L0-synth path");
        assert_eq!(
            source_off,
            ManifestSource::L0Synth,
            "env unset → dev branch is skipped, phase 3 fires"
        );
        assert_eq!(
            manifest_off.version, "0.2.7",
            "L0-synth version wins when dev branch is gated off"
        );

        // Phase B: env var ON → dev branch fires, v9.9.9 wins.
        std::env::set_var("VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH", "1");
        let (manifest_on, source_on) =
            resolve_manifest_for_install(&db, "vct-rl-reranker")
                .expect("dev path");
        match &source_on {
            ManifestSource::Dev(p) => {
                assert!(
                    p.ends_with("vct-module.json"),
                    "dev source path must point at paid-modules manifest: {}",
                    p.display()
                );
                // Audit-string format must distinguish dev from
                // installed (operationally critical — a Dev-source
                // install means the user's running with the env var
                // set, which we want visible post-incident).
                assert!(
                    source_on.as_audit_str().starts_with("dev:"),
                    "audit string must be tagged 'dev:', got {}",
                    source_on.as_audit_str()
                );
            }
            other => panic!(
                "expected ManifestSource::Dev when env var is set, got {:?}",
                other,
            ),
        }
        assert_eq!(
            manifest_on.version, "9.9.9",
            "dev manifest version must win over L0 when the env var is set"
        );

        restore_env(prev_state, prev_dev);
    }

    // ─── L9 manifest-parse-error JSONL logger (v0.2.33, Agent E) ──────
    //
    // Two regression guards for `append_manifest_parse_error_log`:
    //   * a single call writes a well-formed JSON line carrying the
    //     entry's fields + a `ts` timestamp + the `manifest_parse_error`
    //     kind tag;
    //   * repeated calls append (don't overwrite) so multi-error
    //     batches survive a single catalog round-trip.
    //
    // We don't assert on the resolved path beyond "ends with
    // launcher_errors.jsonl" — the fallback vs install-root branch is
    // covered by `resolve_log_path`'s own tests in the watcher.

    #[test]
    fn log_manifest_parse_error_writes_jsonl_entry() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let log_path = tmp.path().join("launcher_errors.jsonl");
        let err = ManifestParseError {
            module_id: "vct-rl-reranker".into(),
            source: "L0:/functions/v1/module-catalog".into(),
            error: "missing field `version`".into(),
        };
        append_manifest_parse_error_log_at(&log_path, &err);

        let raw = std::fs::read_to_string(&log_path).expect("read jsonl");
        let lines: Vec<&str> = raw.lines().collect();
        assert_eq!(lines.len(), 1, "expected one line, got {:?}", lines);
        let parsed: serde_json::Value =
            serde_json::from_str(lines[0]).expect("each line must be valid JSON");
        assert_eq!(parsed["kind"], "manifest_parse_error");
        assert_eq!(parsed["module_id"], "vct-rl-reranker");
        assert_eq!(parsed["source"], "L0:/functions/v1/module-catalog");
        assert_eq!(parsed["error"], "missing field `version`");
        assert!(
            parsed["ts"].as_str().is_some_and(|s| !s.is_empty()),
            "ts must be a non-empty string, got {:?}",
            parsed["ts"],
        );
    }

    #[test]
    fn log_manifest_parse_error_appends_not_overwrites() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let log_path = tmp.path().join("launcher_errors.jsonl");
        let first = ManifestParseError {
            module_id: "module-a".into(),
            source: "/tmp/a/vct-module.json".into(),
            error: "first failure".into(),
        };
        let second = ManifestParseError {
            module_id: "module-b".into(),
            source: "L0:/functions/v1/module-catalog".into(),
            error: "second failure".into(),
        };
        append_manifest_parse_error_log_at(&log_path, &first);
        append_manifest_parse_error_log_at(&log_path, &second);

        let raw = std::fs::read_to_string(&log_path).expect("read jsonl");
        let lines: Vec<&str> = raw.lines().collect();
        assert_eq!(
            lines.len(),
            2,
            "expected two lines after two appends; got {:?}",
            lines,
        );

        let a: serde_json::Value = serde_json::from_str(lines[0]).expect("line 1 json");
        let b: serde_json::Value = serde_json::from_str(lines[1]).expect("line 2 json");
        assert_eq!(a["module_id"], "module-a");
        assert_eq!(b["module_id"], "module-b");
        assert_eq!(a["error"], "first failure");
        assert_eq!(b["error"], "second failure");
    }

    // ─── v0.2.45 V45-C: resolve_manifest_for_install version-compare ────
    //
    // Behaviour pinned: when phase 1 (on-disk manifest) is present AND
    // the L0 catalog cache holds an entry for the same module, the
    // resolver compares semver and falls through to phase 3 (L0Synth)
    // iff L0 is strictly newer. Every other branch (L0 absent / equal /
    // older / unparseable / on-disk-unparseable) honours the on-disk
    // manifest. Safety net: parse failures default to on-disk-wins so we
    // never synthesize from a version we can't confidently compare.

    /// Helper: plant an on-disk vct-module.json at the given version.
    /// Mirrors what Agent C's `extract_manifest_from_image` would write
    /// after `container_pull` succeeded. Only the fields the resolver
    /// touches matter — the rest are filled to ModuleManifest's required
    /// shape.
    fn plant_installed_manifest(tmp: &std::path::Path, version: &str) {
        let module_dir = tmp.join("modules").join("vct-rl-reranker");
        std::fs::create_dir_all(&module_dir).unwrap();
        let installed_manifest = serde_json::json!({
            "manifest_version": 1,
            "id": "vct-rl-reranker",
            "name": "RL Reranker",
            "version": version,
            "description": "v0.2.45 V45-C fixture",
            "category": "paid-independent",
            "license": {
                "required": true,
                "variant_ids": ["x"],
                "min_orchestrator_tier": "pro"
            },
            "compatibility": {"hosts": ["base", "mao", "orchestrator_root"]},
            "install": {
                "method": "container_pull",
                "container": {
                    "image": "ghcr.io/hotak92/vct-rl-reranker",
                    "pull_token_endpoint": "https://example/pull-token"
                }
            },
            "runtime": {"type": "container", "command": ""}
        });
        std::fs::write(
            module_dir.join("vct-module.json"),
            installed_manifest.to_string(),
        )
        .unwrap();
    }

    /// Case 1: on-disk manifest present, L0 catalog cache empty →
    /// `resolve_install_metadata` returns Err → l0_v_opt is None →
    /// l0_is_newer = false → phase 1 wins.
    #[test]
    fn test_v0245_on_disk_wins_when_l0_unavailable() {
        let (_lock, tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();

        // Plant ONLY the on-disk manifest — leave catalog cache empty.
        plant_installed_manifest(tmp.path(), "0.2.7");
        // Sanity: cache is empty → resolve_install_metadata returns Err.
        assert!(
            resolve_install_metadata(&db, "vct-rl-reranker").is_err(),
            "test pre-cond: L0 cache must be empty for this branch",
        );

        let (manifest, source) =
            resolve_manifest_for_install(&db, "vct-rl-reranker")
                .expect("phase 1 must succeed when L0 is absent");
        match &source {
            ManifestSource::Installed(_) => {}
            other => panic!(
                "L0 unavailable → on-disk must win, got {:?}",
                other,
            ),
        }
        assert_eq!(manifest.version, "0.2.7");

        restore_env(prev_state, prev_dev);
    }

    /// Case 2: on-disk v0.2.7, L0 v0.2.7 (identical) → l0_is_newer = false →
    /// phase 1 wins. Re-install of the same version must not synthesize.
    #[test]
    fn test_v0245_on_disk_wins_when_versions_equal() {
        let (_lock, tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();

        // Both versions match.
        plant_installed_manifest(tmp.path(), "0.2.7");
        let envelope = L0CatalogResponse {
            schema_version: 1,
            fetched_at: "2026-06-02T00:00:00Z".into(),
            modules: vec![fake_l0_rl("0.2.7")],
        };
        db.app_state_set(
            crate::commands::module_catalog_client::APP_STATE_KEY_CATALOG,
            &serde_json::to_string(&envelope).unwrap(),
        )
        .unwrap();

        let (manifest, source) =
            resolve_manifest_for_install(&db, "vct-rl-reranker")
                .expect("phase 1 must succeed for equal versions");
        match &source {
            ManifestSource::Installed(_) => {}
            other => panic!(
                "equal versions → on-disk must win, got {:?}",
                other,
            ),
        }
        assert_eq!(manifest.version, "0.2.7");

        restore_env(prev_state, prev_dev);
    }

    /// Case 3 (THE bug fix): on-disk v0.2.7, L0 v0.2.8 → l0_is_newer =
    /// true → fall through to phase 3 → ManifestSource::L0Synth + L0
    /// version wins. This is exactly the user-reported failure mode:
    /// catalog-refresh moves RL Reranker 0.2.7 → 0.2.8, user clicks
    /// Retry, install should pull 0.2.8 not 0.2.7.
    #[test]
    fn test_v0245_l0_wins_when_strictly_newer() {
        let (_lock, tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();

        plant_installed_manifest(tmp.path(), "0.2.7");
        let envelope = L0CatalogResponse {
            schema_version: 1,
            fetched_at: "2026-06-02T00:00:00Z".into(),
            modules: vec![fake_l0_rl("0.2.8")],
        };
        db.app_state_set(
            crate::commands::module_catalog_client::APP_STATE_KEY_CATALOG,
            &serde_json::to_string(&envelope).unwrap(),
        )
        .unwrap();

        let (manifest, source) =
            resolve_manifest_for_install(&db, "vct-rl-reranker")
                .expect("phase 3 must succeed when L0 is newer + cache populated");
        assert_eq!(
            source,
            ManifestSource::L0Synth,
            "L0 strictly newer must take phase 3 (L0Synth), got {:?}",
            source,
        );
        // L0Synth carries the L0 version — the user-visible bug was that
        // retry was pulling the stale 0.2.7 against expectations.
        assert_eq!(
            manifest.version, "0.2.8",
            "L0Synth must carry the L0 version, not the on-disk one",
        );
        assert_eq!(source.as_audit_str(), "l0-synth");

        restore_env(prev_state, prev_dev);
    }

    /// Case 4: on-disk v0.2.8, L0 v0.2.7 (catalog stale relative to disk)
    /// → l0_is_newer = false → phase 1 wins. This safeguards against a
    /// stale-catalog edge case: the user installed a hotfix manually,
    /// L0 hasn't been refreshed yet. On-disk reflects truth.
    #[test]
    fn test_v0245_on_disk_wins_when_l0_older() {
        let (_lock, tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();

        plant_installed_manifest(tmp.path(), "0.2.8");
        let envelope = L0CatalogResponse {
            schema_version: 1,
            fetched_at: "2026-06-02T00:00:00Z".into(),
            modules: vec![fake_l0_rl("0.2.7")],
        };
        db.app_state_set(
            crate::commands::module_catalog_client::APP_STATE_KEY_CATALOG,
            &serde_json::to_string(&envelope).unwrap(),
        )
        .unwrap();

        let (manifest, source) =
            resolve_manifest_for_install(&db, "vct-rl-reranker")
                .expect("phase 1 must succeed when L0 is older");
        match &source {
            ManifestSource::Installed(_) => {}
            other => panic!(
                "L0 older than on-disk → on-disk must win, got {:?} \
                 (would imply downgrade-on-retry)",
                other,
            ),
        }
        assert_eq!(manifest.version, "0.2.8");

        restore_env(prev_state, prev_dev);
    }

    /// Case 5 (safety net): on-disk version unparseable ("abc"), L0 v0.2.8
    /// → parse_semver returns None on on-disk → l0_is_newer = false →
    /// phase 1 wins. We never synthesize from L0 when we can't confidently
    /// compare versions; honour the user's last-installed manifest.
    #[test]
    fn test_v0245_on_disk_wins_when_parse_fails() {
        let (_lock, tmp, prev_state, prev_dev) = isolate_state();
        let db = open_db();

        // Plant an on-disk manifest with an unparseable version string.
        // ModuleManifest::from_json accepts any non-empty string for
        // version — the SchemaVersion check is on `manifest_version`, not
        // on `version` (which is free-form).
        plant_installed_manifest(tmp.path(), "abc");
        let envelope = L0CatalogResponse {
            schema_version: 1,
            fetched_at: "2026-06-02T00:00:00Z".into(),
            modules: vec![fake_l0_rl("0.2.8")],
        };
        db.app_state_set(
            crate::commands::module_catalog_client::APP_STATE_KEY_CATALOG,
            &serde_json::to_string(&envelope).unwrap(),
        )
        .unwrap();

        let (manifest, source) =
            resolve_manifest_for_install(&db, "vct-rl-reranker")
                .expect("phase 1 safety-net must succeed when parse fails");
        match &source {
            ManifestSource::Installed(_) => {}
            other => panic!(
                "unparseable on-disk version → safety net says on-disk \
                 wins (refuse to synthesize from L0 when versions are \
                 incomparable), got {:?}",
                other,
            ),
        }
        assert_eq!(manifest.version, "abc");

        restore_env(prev_state, prev_dev);
    }

    // ─── parse_semver unit tests ─────────────────────────────────────────

    /// Pin the parser's accept-set against a representative range of
    /// version strings: stable releases, leading-v prefix, multi-digit
    /// components. Pure 3-component digits-only must all parse.
    #[test]
    fn test_v0245_parse_semver_accepts_canonical_forms() {
        assert_eq!(parse_semver("0.2.7"), Some((0, 2, 7)));
        assert_eq!(parse_semver("0.2.8"), Some((0, 2, 8)));
        assert_eq!(parse_semver("v0.2.45"), Some((0, 2, 45)));
        assert_eq!(parse_semver("1.0.0"), Some((1, 0, 0)));
        assert_eq!(parse_semver("12.34.56"), Some((12, 34, 56)));
        assert_eq!(parse_semver("  0.2.7  "), Some((0, 2, 7)));
    }

    /// Pin the parser's reject-set against everything the safety net is
    /// supposed to bail on: pre-release suffixes, build-metadata, missing
    /// components, trailing components, non-numeric components. Any None
    /// here is the signal for `l0_is_newer = false` → on-disk wins.
    #[test]
    fn test_v0245_parse_semver_rejects_uncertain_forms() {
        // Pre-release / build-metadata suffixes — ordering is ambiguous.
        assert_eq!(parse_semver("0.2.8-rc1"), None);
        assert_eq!(parse_semver("0.2.8+build42"), None);
        // Missing patch.
        assert_eq!(parse_semver("0.2"), None);
        // Trailing component (CalVer-style).
        assert_eq!(parse_semver("0.2.8.4"), None);
        // Non-numeric components.
        assert_eq!(parse_semver("abc"), None);
        assert_eq!(parse_semver("0.a.0"), None);
        // Empty.
        assert_eq!(parse_semver(""), None);
    }

    /// Pin the strict-ordering predicate that drives the
    /// `l0_is_newer` decision: tuple comparison gives the lexicographic
    /// semver ordering for free, but the tests double-check the cases
    /// that matter for the v0.2.7 → v0.2.8 fix path.
    #[test]
    fn test_v0245_parse_semver_ordering_is_strict() {
        let a = parse_semver("0.2.7").unwrap();
        let b = parse_semver("0.2.8").unwrap();
        assert!(b > a, "0.2.8 must be strictly greater than 0.2.7");
        assert!(a < b);
        assert!(!(a > b));
        // Equal is NOT greater.
        let c = parse_semver("0.2.7").unwrap();
        assert!(!(a > c));
        // Minor / major bumps.
        assert!(parse_semver("0.3.0").unwrap() > parse_semver("0.2.99").unwrap());
        assert!(parse_semver("1.0.0").unwrap() > parse_semver("0.99.99").unwrap());
    }

    // ─── v0.2.49 Bug D / Path 1 (install_scope exposure) ───────────

    /// `ModuleCatalogEntry.install_scope` carries the manifest's
    /// `install.scope` field as a string so the Svelte tile can render
    /// per-project badge variants vs global-scope tile variants
    /// (Bug D). The shape is the wire contract between Rust (this
    /// crate) and Svelte (`launcher/src/lib/components/`).
    ///
    /// Pin the serialized JSON shape so a future renames /
    /// schema-evolution change breaks loudly here, NOT silently at the
    /// tile renderer's "unrecognised scope" branch.
    #[test]
    fn test_v0249_module_catalog_entry_install_scope_serializes_as_string() {
        let entry = ModuleCatalogEntry {
            id: "test".into(),
            name: "Test".into(),
            version: "0.1.0".into(),
            description: String::new(),
            category: "paid".into(),
            tags: vec![],
            license_required: true,
            license_variant_ids: vec![],
            min_orchestrator_tier: "pro".into(),
            compatibility_hosts: vec![],
            is_licensed: true,
            manifest_source: "test".into(),
            kind: "available".into(),
            parent_id: String::new(),
            cta_route: String::new(),
            coming_soon_tier: String::new(),
            coming_soon_target: String::new(),
            deprecated: false,
            deprecation_message: String::new(),
            deprecation_eol_date: String::new(),
            deprecation_migration_url: String::new(),
            catalog_warning: String::new(),
            runtime_type: String::new(),
            install_scope: "global".into(),
        };
        let json = serde_json::to_value(&entry).expect("serialize");
        assert_eq!(
            json["install_scope"], "global",
            "install_scope must serialize as snake_case string on the \
             wire so the Svelte tile (Bug D) can branch on it"
        );
    }

    /// Bug D's tile renderer treats absent / legacy entries as
    /// `per_project`. Round-trip pin: a JSON catalog payload missing
    /// the field deserializes with `install_scope = ""` (the serde
    /// default), which the tile must treat as equivalent to
    /// `per_project`. This pins the deserialization path so the
    /// `#[serde(default)]` annotation stays present.
    #[test]
    fn test_v0249_module_catalog_entry_install_scope_missing_field_defaults_empty() {
        // Construct a minimal JSON payload WITHOUT the install_scope
        // field — simulating an older launcher version or hand-rolled
        // payload from a test fixture.
        let json = serde_json::json!({
            "id": "test",
            "name": "Test",
            "version": "0.1.0",
            "description": "",
            "category": "paid",
            "tags": [],
            "license_required": true,
            "license_variant_ids": [],
            "min_orchestrator_tier": "pro",
            "compatibility_hosts": [],
            "is_licensed": true,
            "manifest_source": "test",
            "kind": "available",
        });
        let entry: ModuleCatalogEntry =
            serde_json::from_value(json).expect("deserialize without install_scope");
        // Field is absent → serde fills with `String::default()` = "".
        // The Svelte tile (Bug D) must treat "" as equivalent to
        // "per_project" (the safe legacy default).
        assert_eq!(
            entry.install_scope, "",
            "missing install_scope must deserialize as empty string \
             (then tile renderer treats it as per_project)"
        );
    }

    /// Builtin entries (launcher, orchestrator, subcomponents) all
    /// declare `install_scope = "per_project"` because they're
    /// conceptually per-workspace. Pin this so a future refactor that
    /// auto-derives scope from somewhere else doesn't silently flip
    /// the builtin entries to "global" (which would break the Bug D
    /// tile renderer's expectations).
    #[test]
    fn test_v0249_builtin_catalog_entries_are_per_project_scope() {
        let tmp = tempfile::tempdir().expect("tempdir");
        std::env::set_var("VCT_STATE_DIR", tmp.path());
        let db = Db::open().expect("open db");
        let entries = builtin_catalog_entries(&db);
        std::env::remove_var("VCT_STATE_DIR");
        assert!(!entries.is_empty(), "builtin catalog must be non-empty");
        for entry in &entries {
            assert_eq!(
                entry.install_scope, "per_project",
                "builtin entry {} should be per_project, got {}",
                entry.id, entry.install_scope
            );
        }
    }
}
