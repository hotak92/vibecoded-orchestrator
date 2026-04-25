//! `vct-module.json` parsing + validation + placeholder resolution.
//!
//! Spec reference: `docs/VCT_MODULE_MANIFEST_SPEC.md` in the Claude
//! Orchestrator meta-project (not shipped in this repo).
//!
//! The parser is deliberately permissive about unknown top-level fields
//! (forward compatibility) but strict about required ones. Unrecognized
//! values for enumerated fields (host, runtime type, etc.) are rejected
//! early so they don't produce confusing errors deeper in the install flow.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

// ─── Top-level manifest type ────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModuleManifest {
    #[serde(default)]
    pub manifest_version: u32,

    pub id: String,
    pub name: String,
    pub version: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub publisher: Option<String>,
    #[serde(default)]
    pub homepage: Option<String>,
    #[serde(default)]
    pub repository: Option<String>,
    #[serde(default)]
    pub icon: Option<String>,

    pub category: ModuleCategory,
    #[serde(default)]
    pub tags: Vec<String>,

    #[serde(default)]
    pub compatibility: Compatibility,

    #[serde(default)]
    pub license: LicenseBlock,

    #[serde(default)]
    pub requirements: Requirements,

    pub install: InstallBlock,

    #[serde(default)]
    pub secrets: Vec<SecretDecl>,

    #[serde(default)]
    pub settings: Vec<SettingDecl>,

    pub runtime: RuntimeBlock,

    #[serde(default)]
    pub mcp_registration: Option<McpRegistration>,

    #[serde(default)]
    pub setup_wizard: Option<SetupWizard>,

    #[serde(default)]
    pub upgrade: Option<UpgradeBlock>,

    #[serde(default)]
    pub telemetry: Option<serde_json::Value>,

    #[serde(default)]
    pub uninstall: Option<UninstallBlock>,

    #[serde(default)]
    pub provides: Vec<serde_json::Value>,
    #[serde(default)]
    pub consumes: Vec<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "kebab-case")]
pub enum ModuleCategory {
    Core,
    PaidOrchestrator,
    PaidIndependent,
    Community,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct Compatibility {
    #[serde(default = "default_hosts")]
    pub hosts: Vec<String>,
    #[serde(default)]
    pub min_launcher_version: Option<String>,
}
fn default_hosts() -> Vec<String> {
    vec!["base".into(), "mao".into()]
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct LicenseBlock {
    #[serde(default)]
    pub required: bool,
    #[serde(default)]
    pub r#type: Option<String>,
    #[serde(default)]
    pub variant_ids: Vec<String>,
    #[serde(default = "default_min_tier")]
    pub min_orchestrator_tier: String,
    #[serde(default)]
    pub trial_days: u32,
}
fn default_min_tier() -> String {
    "free".into()
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct Requirements {
    #[serde(default)]
    pub os: Vec<String>,
    #[serde(default)]
    pub python: Option<String>,
    #[serde(default)]
    pub node: Option<String>,
    #[serde(default)]
    pub memory_mb: u64,
    #[serde(default)]
    pub disk_mb: u64,
    #[serde(default)]
    pub network: Vec<String>,
    #[serde(default)]
    pub gpu: bool,
    #[serde(default)]
    pub depends_on: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallBlock {
    pub method: InstallMethod,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub r#ref: Option<String>,
    #[serde(default = "default_install_dir")]
    pub install_dir: String,
    #[serde(default)]
    pub post_install: Vec<CommandSpec>,
}
fn default_install_dir() -> String {
    "{VCT_MODULES}/{MODULE_ID}".into()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum InstallMethod {
    GitClone,
    Tarball,
    Pypi,
    Npm,
    Local,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CommandSpec {
    pub cmd: String,
    #[serde(default)]
    pub cwd: Option<String>,
    #[serde(default = "default_timeout")]
    pub timeout_s: u64,
    #[serde(default)]
    pub platform_cmd: HashMap<String, String>,
    #[serde(default)]
    #[serde(rename = "_note")]
    pub note: Option<String>,
}
fn default_timeout() -> u64 {
    120
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SecretDecl {
    pub key: String,
    #[serde(default)]
    pub prompt: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub example: Option<String>,
    #[serde(default)]
    pub validation: Option<String>,
    #[serde(default = "default_true")]
    pub required: bool,
    #[serde(default = "default_scope_per_project")]
    pub scope: String, // "global" | "per-project" | "shared"
    #[serde(default)]
    pub sensitive: bool,
}
fn default_true() -> bool {
    true
}
fn default_scope_per_project() -> String {
    "per-project".into()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SettingDecl {
    pub key: String,
    #[serde(default)]
    pub prompt: String,
    #[serde(default)]
    pub description: String,
    #[serde(default = "default_setting_type")]
    pub r#type: String, // "string" | "integer" | "boolean" | "multiselect" | "path"
    #[serde(default)]
    pub default: serde_json::Value,
    #[serde(default)]
    pub default_by_platform: HashMap<String, serde_json::Value>,
    #[serde(default)]
    pub options: Vec<String>,
    #[serde(default)]
    pub validation: Option<String>,
    #[serde(default)]
    pub validation_cmd: Option<String>,
    #[serde(default)]
    pub required: bool,
    #[serde(default)]
    pub min: Option<i64>,
    #[serde(default)]
    pub max: Option<i64>,
}
fn default_setting_type() -> String {
    "string".into()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeBlock {
    pub r#type: String, // "mcp_stdio" | "mcp_http" | "service" | "cli"
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub platform_command: HashMap<String, String>,
    #[serde(default)]
    pub cwd: Option<String>,
    #[serde(default)]
    pub env_from_secrets: Vec<String>,
    #[serde(default)]
    pub env_from_settings: Vec<String>,
    #[serde(default)]
    pub env_fixed: HashMap<String, String>,
    #[serde(default)]
    pub health_check: Option<HealthCheck>,
    #[serde(default)]
    pub auto_restart: bool,
    #[serde(default)]
    pub log_file: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HealthCheck {
    pub r#type: String, // "stdio_ping" | "http_get"
    #[serde(default = "default_timeout")]
    pub timeout_s: u64,
    #[serde(default = "default_interval")]
    pub interval_s: u64,
    #[serde(default)]
    pub url: Option<String>,
}
fn default_interval() -> u64 {
    30
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct McpRegistration {
    #[serde(default = "default_true")]
    pub enabled_by_default: bool,
    pub mcp_name: String,
    #[serde(default = "default_target_all")]
    pub target_projects: serde_json::Value, // "all" | "none" | ["path"]
    #[serde(default = "default_user_scope")]
    pub scope: String, // "user" | "project"
}
fn default_target_all() -> serde_json::Value {
    serde_json::Value::String("all".into())
}
fn default_user_scope() -> String {
    "user".into()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SetupWizard {
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub platform_command: HashMap<String, String>,
    #[serde(default)]
    pub env_from_secrets: Vec<String>,
    #[serde(default)]
    pub env_from_settings: Vec<String>,
    #[serde(default)]
    pub success_marker: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpgradeBlock {
    #[serde(default = "default_upgrade_strategy")]
    pub strategy: String,
    #[serde(default)]
    pub pre_upgrade: Vec<CommandSpec>,
    #[serde(default)]
    pub post_upgrade: Vec<CommandSpec>,
    #[serde(default)]
    pub migration_script: Option<String>,
}
fn default_upgrade_strategy() -> String {
    "git_pull".into()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UninstallBlock {
    #[serde(default = "default_true")]
    pub remove_install_dir: bool,
    #[serde(default)]
    pub preserve_paths: Vec<String>,
    #[serde(default = "default_true")]
    pub deregister_mcp: bool,
    #[serde(default)]
    pub clear_secrets: bool,
}

// ─── Parsing ─────────────────────────────────────────────────────────────

impl ModuleManifest {
    /// Parse + sanity-check a manifest from a JSON string.
    ///
    /// Validates required fields and enum values. Callers perform
    /// side-effecting validations (file existence, license availability)
    /// separately — this function never touches the filesystem or network.
    pub fn from_json(raw: &str) -> Result<Self, String> {
        let mut m: Self = serde_json::from_str(raw)
            .map_err(|e| format!("manifest JSON parse: {}", e))?;

        if m.id.is_empty() {
            return Err("manifest.id is required".into());
        }
        if !m.id.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-') {
            return Err(format!("manifest.id '{}' must be kebab-case (lowercase, digits, hyphens)", m.id));
        }
        if m.name.is_empty() {
            return Err("manifest.name is required".into());
        }
        if m.version.is_empty() {
            return Err("manifest.version is required".into());
        }

        // Drop empty hosts; default kicks in.
        if m.compatibility.hosts.is_empty() {
            m.compatibility.hosts = default_hosts();
        }
        for h in &m.compatibility.hosts {
            if !matches!(h.as_str(), "base" | "mao" | "standalone") {
                return Err(format!("manifest.compatibility.hosts contains invalid value '{}'", h));
            }
        }

        if m.license.required && m.license.variant_ids.is_empty()
            && m.license.min_orchestrator_tier == "free" {
            return Err("manifest.license.required=true but no variant_ids and min_orchestrator_tier=free — contradictory".into());
        }
        if !matches!(
            m.license.min_orchestrator_tier.as_str(),
            "free" | "pro" | "mao" | "enterprise"
        ) {
            return Err(format!(
                "manifest.license.min_orchestrator_tier invalid: '{}'",
                m.license.min_orchestrator_tier
            ));
        }

        for s in &m.secrets {
            if !matches!(s.scope.as_str(), "global" | "per-project" | "shared") {
                return Err(format!("secret '{}' has invalid scope '{}'", s.key, s.scope));
            }
        }

        if !matches!(
            m.runtime.r#type.as_str(),
            "mcp_stdio" | "mcp_http" | "service" | "cli"
        ) {
            return Err(format!("runtime.type '{}' not recognized", m.runtime.r#type));
        }

        Ok(m)
    }

    /// Returns true if this manifest is installable on the given host.
    pub fn is_compatible_with_host(&self, host: &str) -> bool {
        self.compatibility.hosts.iter().any(|h| h == host)
    }
}

// ─── Placeholder resolution ──────────────────────────────────────────────

/// Runtime environment for resolving placeholder strings.
///
/// Builders fill in the fields they know; `resolve` substitutes `{TOKEN}`
/// patterns. Unknown tokens pass through unchanged so a typo in a
/// manifest is visible in the error message that eventually surfaces.
#[derive(Debug, Clone)]
pub struct PlaceholderCtx {
    pub vct_root: PathBuf,
    pub vct_modules: PathBuf,
    pub vct_data: PathBuf,
    pub vct_logs: PathBuf,
    pub install_dir: Option<PathBuf>,
    pub module_id: String,
    pub hostname: String,
    pub user: String,
    pub home: PathBuf,
    pub appdata: Option<PathBuf>, // Windows %APPDATA%
}

impl PlaceholderCtx {
    pub fn new(module_id: &str) -> Self {
        let home = directories::UserDirs::new()
            .map(|d| d.home_dir().to_path_buf())
            .unwrap_or_else(|| PathBuf::from("/"));
        let vct_root = home.join(".vct");
        Self {
            vct_modules: vct_root.join("modules"),
            vct_data: vct_root.join("data"),
            vct_logs: vct_root.join("logs"),
            vct_root,
            install_dir: None,
            module_id: module_id.to_string(),
            hostname: gethostname::gethostname().to_string_lossy().to_string(),
            user: std::env::var("USER")
                .or_else(|_| std::env::var("USERNAME"))
                .unwrap_or_else(|_| "user".to_string()),
            home: home.clone(),
            appdata: std::env::var_os("APPDATA").map(PathBuf::from),
        }
    }

    pub fn with_install_dir(mut self, dir: PathBuf) -> Self {
        self.install_dir = Some(dir);
        self
    }

    /// Substitute `{TOKEN}` patterns in a string.
    pub fn resolve(&self, s: &str) -> String {
        let mut out = s.to_string();
        let replacements: Vec<(&str, String)> = vec![
            ("{VCT_ROOT}", self.vct_root.display().to_string()),
            ("{VCT_MODULES}", self.vct_modules.display().to_string()),
            ("{VCT_DATA}", self.vct_data.display().to_string()),
            ("{VCT_LOGS}", self.vct_logs.display().to_string()),
            (
                "{install_dir}",
                self.install_dir
                    .as_ref()
                    .map(|p| p.display().to_string())
                    .unwrap_or_else(|| format!("{{install_dir-unresolved:{}}}", self.module_id)),
            ),
            ("{MODULE_ID}", self.module_id.clone()),
            ("{HOSTNAME}", self.hostname.clone()),
            ("{USER}", self.user.clone()),
            ("{HOME}", self.home.display().to_string()),
            (
                "{APPDATA}",
                self.appdata
                    .as_ref()
                    .map(|p| p.display().to_string())
                    .unwrap_or_else(|| self.home.display().to_string()),
            ),
        ];
        for (token, value) in replacements {
            out = out.replace(token, &value);
        }
        out
    }

    /// Resolve install_dir from a manifest string and return it as a PathBuf
    /// (without needing to set `install_dir` first).
    pub fn resolve_install_dir(&self, raw: &str) -> PathBuf {
        PathBuf::from(self.resolve(raw))
    }
}

/// Security: refuse install_dir paths that escape `~/.vct/modules/`.
///
/// Symlinks resolved via `canonicalize` — if the user has no such
/// directory yet we canonicalize the parent and append the module name.
pub fn validate_install_dir(candidate: &Path, allowed_root: &Path) -> Result<(), String> {
    let abs = if candidate.is_absolute() {
        candidate.to_path_buf()
    } else {
        return Err(format!("install_dir must be absolute: {}", candidate.display()));
    };

    // If the exact path doesn't exist yet, canonicalize the closest existing
    // ancestor (avoid the "directory doesn't exist" canonicalize failure).
    let mut probe = abs.as_path();
    let canonical_base = loop {
        match probe.canonicalize() {
            Ok(p) => break p,
            Err(_) => match probe.parent() {
                Some(p) => probe = p,
                None => return Err("install_dir has no canonicalizable ancestor".into()),
            },
        }
    };
    let canonical_root = allowed_root
        .canonicalize()
        .unwrap_or_else(|_| allowed_root.to_path_buf());

    if !canonical_base.starts_with(&canonical_root) {
        return Err(format!(
            "install_dir {} escapes allowed root {}",
            candidate.display(),
            allowed_root.display()
        ));
    }
    Ok(())
}
