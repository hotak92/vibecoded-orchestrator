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
}

impl ModuleStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            ModuleStatus::Installing => "installing",
            ModuleStatus::Installed => "installed",
            ModuleStatus::Running => "running",
            ModuleStatus::Stopped => "stopped",
            ModuleStatus::Error => "error",
        }
    }
    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "installing" => Some(ModuleStatus::Installing),
            "installed" => Some(ModuleStatus::Installed),
            "running" => Some(ModuleStatus::Running),
            "stopped" => Some(ModuleStatus::Stopped),
            "error" => Some(ModuleStatus::Error),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModuleInstallRow {
    pub id: String,
    pub project_id: String,
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
}

/// Single row of `module_weights_state` (migration 016).
///
/// Tracks the per-(project × module × embedding_source) state for the RL
/// reranker's downloadable model weights:
///   * `version` — the locally-active weights version string (server-issued)
///   * `last_checked_at` — unix-ms of the last `/rl-latest-version` poll
///     attempt (success OR failure — observers want to know we tried)
///   * `last_finetuned_at` — unix-ms of the last successful local fine-tune
///
/// Embedding source is stored as a free-form string (qwen3, arctic, openai,
/// future…) — the launcher never enum-constrains it; the container picks
/// the matching `.pt` based on the `ACTIVE_EMBEDDING` env var.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct WeightsStateRow {
    pub project_id: String,
    pub module_id: String,
    pub embedding_source: String,
    pub version: String,
    pub last_checked_at: i64,
    pub last_finetuned_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TierCacheRow {
    pub orchestrator_tier: String,
    pub module_licenses: serde_json::Value, // {module_id: {tier, expires_at}}
    pub last_validated: i64,
    pub last_error: Option<String>,
}
