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
    // v0.2.54 Track H (P0-5): `from_apps` was REMOVED. It derived the
    // tier from the Supabase `profiles.apps` list the frontend passed in
    // — but license-key activation (the canonical ActivationModal →
    // keychain → /validate-tier → `tier_cache` flow) never writes to
    // `profiles.apps`, so every Pro customer who activated via license
    // key was classified Free by the dashboard. Tier resolution now
    // reads `db.get_tier_cache()` (the same row `license_get_tier`
    // serves) and ranks slugs via `licensing::tier_rank`.

    /// Lowercase wire slug for this tier — same string serde emits
    /// (`#[serde(rename_all = "lowercase")]`), usable with
    /// `licensing::tier_rank` without a serialization round-trip.
    pub fn as_slug(&self) -> &'static str {
        match self {
            OrchestratorTier::Free => "free",
            OrchestratorTier::Pro => "pro",
            OrchestratorTier::Mao => "mao",
        }
    }
}

// --- MCP server config ---

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct McpServerConfig {
    /// Unique ID: "weaviate-kg", "ollama", "search", "code-embed", "ecosystem-app-1-live", etc.
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

// v0.2.54 Track H: `watermark_enabled` was REMOVED. The flag only fed a
// `VCT_WATERMARK` env emission in `apply_mcp_to_claude_settings` that
// nothing ever read — the watermark feature never shipped a consumer.
// Existing orchestrator.json files that still carry the key parse fine
// (serde ignores unknown fields).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrchestratorConfig {
    pub install_path: String,
    pub tier: OrchestratorTier,
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
        // to run as a container service. The `search` (paper-search) MCP was
        // deleted outright in v0.2.101; the `mermaid`/`excalidraw` diagram
        // wrappers were retired from default registration in the same release.
        // See docs/features/02-mcps-and-agents.md.
        // NOTE: `code-embed` (CodeSage-Large-v2 container at port 11440) is
        // NOT an MCP — it's a backend HTTP service consumed by `weaviate-kg`
        // for code-graph embeddings. It lives in the Services tab, not the
        // MCP registry. Removed from this list 2026-05-13 after surfacing as
        // "global off" in the per-project Permissions tab (misclassification:
        // the per-project toggle has no semantic meaning since weaviate-kg's
        // codegraph features need the service either way). Where it runs is
        // its launcher.db `service_endpoints` row (v0.2.97; code-embed is
        // always VCO-managed); the launcher tray tracks it as a service.
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
        // NOTE (v0.2.101): the `mermaid` / `excalidraw` diagram wrapper MCPs
        // were removed from this catalog. Their default registration was
        // retired (mcp_registration.rs / install_mcp.py) and the Permissions
        // tab no longer ships toggle cards for them; an install that already
        // has the entries keeps them. The launcher's Diagrams tab never used
        // these MCPs (it renders Mermaid in the webview and lists diagrams via
        // Tauri commands), so it is unaffected.
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    /// v0.2.101: the default MCP catalog is exactly the builder set
    /// (weaviate-kg + playwright). `search` was deleted and the diagram
    /// wrapper MCPs (`mermaid`/`excalidraw`) were retired from default
    /// registration — the catalog must NOT list them, or the Permissions tab
    /// shows cards for servers no install path registers (the pre-v0.2.101
    /// false-promise shape). Red-proof: re-add either id → this fails.
    #[test]
    fn default_mcp_servers_is_the_builder_set() {
        let servers = default_mcp_servers();
        let ids: Vec<&str> = servers.iter().map(|s| s.id.as_str()).collect();
        assert_eq!(ids, vec!["weaviate-kg", "playwright"]);
        for retired in ["search", "mermaid", "excalidraw"] {
            assert!(
                !ids.contains(&retired),
                "retired MCP `{}` must not be in the default catalog (got {:?})",
                retired,
                ids
            );
        }
    }

    /// Shape contract for the two remaining default MCPs: stdio, free tier,
    /// default-enabled, and a non-empty command.
    #[test]
    fn default_mcp_servers_have_expected_shape() {
        let servers = default_mcp_servers();
        for entry in &servers {
            assert!(entry.enabled, "{} should be default-enabled", entry.id);
            assert_eq!(entry.min_tier, OrchestratorTier::Free, "{} should be free-tier", entry.id);
            assert!(entry.port.is_none(), "{} is stdio MCP, port must be None", entry.id);
            assert!(!entry.command.is_empty(), "{} command should be set", entry.id);
        }
    }
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

