use serde::{Deserialize, Serialize};
use std::collections::HashMap;

// --- App status ---

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum AppStatus {
    Running,
    Stopped,
    Starting,
    Error,
    Downloading,
    Installing,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServiceEntry {
    pub app_id: String,
    pub status: AppStatus,
    pub pid: Option<u32>,
    pub port: Option<u16>,
    pub health_url: Option<String>,
    pub install_path: Option<String>,
    pub version: Option<String>,
    pub active_project: Option<String>,
    pub error_message: Option<String>,
    pub started_at: Option<String>,
}

// --- Download progress ---
//
// TODO: wire — defined for emitting download-progress events during
// module install (used by the install-progress UI panel). Today the
// install path streams `InstallProgress` events instead (see
// `installer_engine.rs`). DownloadProgress is structurally distinct
// (per-byte download metrics vs per-step install metrics) and is
// reserved for the planned multi-source module download path
// (Lemon Squeezy CDN downloads, etc.).
#[allow(dead_code)]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DownloadProgress {
    pub app_id: String,
    pub bytes_downloaded: u64,
    pub total_bytes: u64,
    pub percentage: f32,
    pub stage: String,
}

// --- Orchestrator tier ---

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum OrchestratorTier {
    Free,
    Pro,
    Mao,
}

impl OrchestratorTier {
    /// Derive tier from user's activated app list (highest wins).
    pub fn from_apps(apps: &[String]) -> Self {
        if apps.iter().any(|a| a == "mao") {
            OrchestratorTier::Mao
        } else if apps.iter().any(|a| a == "orchestrator-pro") {
            OrchestratorTier::Pro
        } else {
            OrchestratorTier::Free
        }
    }

    pub fn can_auto_update(&self) -> bool {
        matches!(self, OrchestratorTier::Pro | OrchestratorTier::Mao)
    }

    pub fn can_disable_watermark(&self) -> bool {
        matches!(self, OrchestratorTier::Pro | OrchestratorTier::Mao)
    }

    pub fn has_rl_retrieval(&self) -> bool {
        matches!(self, OrchestratorTier::Pro | OrchestratorTier::Mao)
    }

    pub fn has_curated_agents(&self) -> bool {
        matches!(self, OrchestratorTier::Pro | OrchestratorTier::Mao)
    }

    pub fn has_mao(&self) -> bool {
        matches!(self, OrchestratorTier::Mao)
    }
}

// --- MCP server config ---

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct McpServerConfig {
    /// Unique ID: "weaviate-kg", "ollama", "search", "code-embed", "transcrypt-live", etc.
    pub id: String,
    pub name: String,
    pub description: String,
    pub enabled: bool,
    /// Command to run the server (relative to orchestrator install path)
    pub command: String,
    pub args: Vec<String>,
    pub env: HashMap<String, String>,
    /// Minimum tier required to use this MCP
    pub min_tier: OrchestratorTier,
    /// Port this server listens on (for health check)
    pub port: Option<u16>,
    /// Whether this MCP can be user-configured (vs system-managed)
    pub configurable: bool,
    /// User-editable settings (key-value pairs shown in UI)
    pub settings: HashMap<String, McpSetting>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct McpSetting {
    pub label: String,
    pub value: String,
    pub setting_type: McpSettingType,
    pub description: String,
    /// If true, user can change this in the dashboard
    pub editable: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum McpSettingType {
    Text,
    Number,
    Bool,
    Select,
    Path,
    Secret,
}

// --- Orchestrator feature config (persisted to disk) ---

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrchestratorConfig {
    pub install_path: String,
    pub tier: OrchestratorTier,
    pub watermark_enabled: bool,
    pub auto_update_enabled: bool,
    pub rl_retrieval_enabled: bool,
    pub mcp_servers: Vec<McpServerConfig>,
    pub telemetry_enabled: bool,
    pub telemetry_anonymous_usage: bool,
}

impl Default for OrchestratorConfig {
    fn default() -> Self {
        Self {
            install_path: String::new(),
            tier: OrchestratorTier::Free,
            watermark_enabled: true,
            auto_update_enabled: false,
            rl_retrieval_enabled: false,
            mcp_servers: default_mcp_servers(),
            telemetry_enabled: true,
            telemetry_anonymous_usage: true,
        }
    }
}

fn default_mcp_servers() -> Vec<McpServerConfig> {
    vec![
        McpServerConfig {
            id: "weaviate-kg".to_string(),
            name: "Knowledge & Code Graph".to_string(),
            description: "Semantic + structural search across KG nodes and code entities".to_string(),
            enabled: true,
            command: "claude_mcp_servers/weaviate_mcp/server.py".to_string(),
            args: vec![],
            env: HashMap::new(),
            min_tier: OrchestratorTier::Free,
            port: None, // Runs as stdio MCP, not HTTP
            configurable: true,
            settings: HashMap::from([
                ("KG_COLLECTION".to_string(), McpSetting {
                    label: "KG Collection".to_string(),
                    value: "KnowledgeGraph".to_string(),
                    setting_type: McpSettingType::Text,
                    description: "Weaviate collection name for knowledge graph".to_string(),
                    editable: true,
                }),
                ("DEVELOPMENT_COLLECTION".to_string(), McpSetting {
                    label: "Development Collection".to_string(),
                    value: "Development".to_string(),
                    setting_type: McpSettingType::Text,
                    description: "Weaviate collection for project documentation".to_string(),
                    editable: true,
                }),
            ]),
        },
        // NOTE: Ollama MCP server (chat / read_document / read_image) was
        // removed from the default install in v0.2.11 — those tools are
        // redundant with Claude's native capabilities. Ollama as embedding
        // infrastructure (Weaviate vectorizers) is unchanged; it continues
        // to run as a container service. Users who had the ollama MCP entry
        // in ~/.claude.json will see a deferral notice from install.py
        // guiding manual cleanup.
        McpServerConfig {
            id: "search".to_string(),
            name: "Academic Paper Search".to_string(),
            // v0.2.11: Search MCP simplified to search_papers only.
            // SearXNG (web_search) and GitHub code-search (search_code)
            // removed. Only OPENALEX_EMAIL env var needed going forward.
            description: "Academic paper search via OpenAlex (240 M papers) and arXiv CS/ML preprints. Uses search_papers tool only.".to_string(),
            enabled: true,
            command: "claude_mcp_servers/search_mcp/server.py".to_string(),
            args: vec![],
            env: HashMap::new(),
            min_tier: OrchestratorTier::Free,
            port: None,
            configurable: true,
            settings: HashMap::from([
                ("OPENALEX_EMAIL".to_string(), McpSetting {
                    label: "OpenAlex Email".to_string(),
                    value: String::new(),
                    setting_type: McpSettingType::Text,
                    description: "Optional email for OpenAlex polite-pool priority (higher rate limits, no token needed).".to_string(),
                    editable: true,
                }),
            ]),
        },
        // NOTE: `code-embed` (CodeSage-Large-v2 container at port 11440) is
        // NOT an MCP — it's a backend HTTP service consumed by `weaviate-kg`
        // for code-graph embeddings. It lives in the Services tab, not the
        // MCP registry. Removed from this list 2026-05-13 after surfacing as
        // "global off" in the per-project Permissions tab (misclassification:
        // the per-project toggle has no semantic meaning since weaviate-kg's
        // codegraph features need the service either way). The container is
        // managed via `services.toml` (adopt mode); the launcher tray
        // (`tray.rs::services` list) correctly tracks it as a service.
        McpServerConfig {
            id: "playwright".to_string(),
            name: "Browser automation".to_string(),
            description: "Browser automation, screenshots, and GUI testing via @playwright/mcp (Microsoft, Apache-2.0). Auto-installed via npx; Chromium (~150 MB) is cached during first-install. Set VCT_SKIP_PLAYWRIGHT=1 to skip the eager browser download.".to_string(),
            enabled: true, // Default-enabled — Playwright is generally useful and Chromium is cached during first-install
            command: "npx".to_string(),
            args: vec!["-y".to_string(), "@playwright/mcp@latest".to_string()],
            env: HashMap::new(),
            min_tier: OrchestratorTier::Free, // Available to all tiers
            port: None, // stdio-based MCP, no HTTP port
            configurable: false, // No user-editable settings on day one (can be added later)
            settings: HashMap::new(),
        },
    ]
}

// --- Projects ---

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Project {
    pub id: String,
    pub name: String,
    pub local_path: String,
    pub apps: Vec<String>,
    pub config: serde_json::Value,
    pub created_at: String,
    pub updated_at: String,
    pub synced_to_cloud: bool,
}

