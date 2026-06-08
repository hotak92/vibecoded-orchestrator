//! Row-shaped types returned by db/*.rs helpers.
//!
//! These are internal — the types sent over Tauri IPC (with convenience
//! fields like `module_count`) live in `crate::types::api`.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum ProjectHost {
    Base,
    Mao,
    /// The orchestrator clone itself, auto-registered as a project row at
    /// launcher startup so it can participate in FK-strict subsystems
    /// (codegraph access grants, KG bindings, per-project MCP, etc.).
    /// Backed by SQL migration 013 (extends the `projects.host` CHECK).
    ///
    /// Wire format MUST be the exact string `"orchestrator_root"` — the
    /// `rename_all = "lowercase"` rule alone would map this variant to
    /// `"orchestratorroot"` (no separator), which the DB CHECK would
    /// reject. The explicit `rename` here keeps the JSON/serde round-
    /// trip aligned with the DB column value the migration accepts.
    #[serde(rename = "orchestrator_root")]
    OrchestratorRoot,
}

impl ProjectHost {
    pub fn as_str(&self) -> &'static str {
        match self {
            ProjectHost::Base => "base",
            ProjectHost::Mao => "mao",
            ProjectHost::OrchestratorRoot => "orchestrator_root",
        }
    }
    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "base" => Some(ProjectHost::Base),
            "mao" => Some(ProjectHost::Mao),
            "orchestrator_root" => Some(ProjectHost::OrchestratorRoot),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectRow {
    pub id: String,
    pub name: String,
    pub folder_path: String,
    pub host: ProjectHost,
    pub slug: String,
    pub created_at: i64,
    pub updated_at: i64,
    /// Per-project RL reranker server port (Pro-tier vct-rl-reranker module,
    /// v0.2.21 / migration 014). `None` for projects created before the
    /// migration ran AND for projects where `set_project_rl_port` has not
    /// run yet. Allocated in 11500..=11900 on first RL install for the
    /// project (fixed 11442 for `host='orchestrator_root'`). Persisted via
    /// `Db::set_project_rl_port`.
    ///
    /// B2 / single-writer principle (v0.2.21 Step 3 decision): this column
    /// is HUB-WRITABLE / launcher-readable. The Phase 1E supervisor
    /// (relocated to vct-hub::module_supervisor in Step 24 commit b)
    /// allocates and writes; the launcher GUI only reads to render port
    /// in the dashboard. Direct writes from launcher commands are
    /// intentionally not exposed.
    #[serde(default)]
    pub rl_port: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum ModuleStatus {
    Installing,
    Installed,
    Running,
    Stopped,
    Error,
    /// v0.2.33 (Agent C): on-disk manifest at `~/.vct/modules/<id>/`
    /// was found missing by the startup reconciler. Distinct from
    /// `Error` because the recovery action is Reinstall (not Restart) —
    /// the underlying artifact is gone and `apply_migrations` /
    /// `start_container` can't recover from a missing manifest.
    /// Surfaced in the catalog tile as kind=`broken` with a Reinstall
    /// CTA. Wire format: `"broken"` (matches the CHECK constraint
    /// introduced by migration 021).
    Broken,
}

impl ModuleStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            ModuleStatus::Installing => "installing",
            ModuleStatus::Installed => "installed",
            ModuleStatus::Running => "running",
            ModuleStatus::Stopped => "stopped",
            ModuleStatus::Error => "error",
            ModuleStatus::Broken => "broken",
        }
    }
    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "installing" => Some(ModuleStatus::Installing),
            "installed" => Some(ModuleStatus::Installed),
            "running" => Some(ModuleStatus::Running),
            "stopped" => Some(ModuleStatus::Stopped),
            "error" => Some(ModuleStatus::Error),
            "broken" => Some(ModuleStatus::Broken),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModuleInstallRow {
    pub id: String,
    /// v0.2.49 Stream A: nullable to support `install.scope = "global"`.
    /// `None` ⇒ global install (exactly one row per machine for this
    /// module; per-project routing happens inside the container). `Some(_)`
    /// ⇒ per-project install (the v0.2.20–v0.2.48 behaviour).
    ///
    /// The DB column was made nullable by migration 027 (Stream A,
    /// v0.2.49). Backwards compat: pre-v0.2.49 callers that constructed
    /// rows with `project_id: "some-uuid".into()` now need to use
    /// `Some("some-uuid".into())`.
    pub project_id: Option<String>,
    pub module_id: String,
    pub module_version: String,
    pub install_path: String,
    pub status: ModuleStatus,
    pub enabled: bool,
    pub installed_at: i64,
    pub last_started_at: Option<i64>,
    pub last_error: Option<String>,
    /// Resolved container name for container-runtime modules (migration 015,
    /// Phase 1E). `None` for non-container modules (git_clone / local) and
    /// for container modules whose start path hasn't run yet. Populated by
    /// the Phase 1E supervisor (now in `vct-hub::module_supervisor`)
    /// immediately after `podman run -d --name <resolved>` succeeds; read
    /// by the launcher's startup hook + the uninstall path to enumerate
    /// containers.
    ///
    /// B2 / single-writer principle (v0.2.21 Step 3 decision): this column
    /// is HUB-WRITABLE / launcher-readable. The launcher GUI never writes
    /// it — only the hub-side `module_supervisor` does, via the proxy path
    /// added in Step 24 commit b.
    #[serde(default)]
    pub container_name: Option<String>,

    /// v0.2.49 Step F MF3 follow-up (migration 032): Weaviate collection
    /// names this module declares it writes to, denormalized from the
    /// manifest's `kg_collections` field at install time. Empty Vec when
    /// the manifest doesn't declare the field (the common case — most
    /// modules don't expose any KG).
    ///
    /// Read by `populate_kg_collection_access_for_project` to back-fill
    /// access rows on every new project create (the inverse of item #13
    /// which seeds existing projects at module-install time). Storing
    /// in the launcher DB avoids re-parsing the on-disk manifest from
    /// the hot path; survives manifest file deletion / corruption.
    #[serde(default)]
    pub kg_collections: Vec<String>,
}

// `WeightsStateRow` removed in v0.2.31 (Agent J): launcher-side
// `module_weights_state` table dropped by migration 020; weights state
// is now container-owned in `rl_weights_state` (shipped by vct-rl-
// reranker v0.2.6 via its module-shipped migration `db/0002_*.sql`).
// Launcher reads go through the hub's
// `/api/v1/modules/.../rows/rl_weights_state/...` typed REST surface.

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TierCacheRow {
    pub orchestrator_tier: String,
    pub module_licenses: serde_json::Value, // {module_id: {tier, expires_at}}
    pub last_validated: i64,
    pub last_error: Option<String>,
}
