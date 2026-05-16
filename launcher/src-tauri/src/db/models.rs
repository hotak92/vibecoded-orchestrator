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
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TierCacheRow {
    pub orchestrator_tier: String,
    pub module_licenses: serde_json::Value, // {module_id: {tier, expires_at}}
    pub last_validated: i64,
    pub last_error: Option<String>,
}
