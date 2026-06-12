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
        // ── mermaid (v0.2.33 Phase 1.2 — diagrams integration) ───────────
        //
        // Wrapper MCP at `claude_mcp_servers/wrappers/mermaid_proxy.py`
        // (see `mcp_registration.rs:436-460` for the ~/.claude.json
        // registration). Proxies the npm `claude-mermaid` package with
        // path-allowlist enforcement to keep diagram writes inside
        // `.claude/diagrams/`. Listed here so the launcher's global MCP
        // Servers tab + per-project Permissions tab can render
        // enable/disable cards (v0.2.34 Agent D fix — without this entry
        // both surfaces were blind to the wrapper).
        //
        // configurable: false — no user-editable env (the wrapper's
        // PYTHONPATH is resolved by mcp_registration at write time).
        McpServerConfig {
            id: "mermaid".to_string(),
            name: "Mermaid diagrams".to_string(),
            description: "Render and save Mermaid diagrams (`render`, `save_diagram`). Wraps the npm `claude-mermaid` package via a Python proxy that enforces the `.claude/diagrams/` scoped-path boundary. Used by the launcher's Diagrams tab and by Claude when asked to sketch flowcharts / architecture / GUI diagrams.".to_string(),
            enabled: true, // Default-enabled — bundled diagrams feature is opt-out, mirrors `set_project_module_enabled('diagrams', true)` seeded on project create
            command: "python".to_string(),
            args: vec![
                "-m".to_string(),
                "claude_mcp_servers.wrappers.mermaid_proxy".to_string(),
            ],
            env: HashMap::new(),
            min_tier: OrchestratorTier::Free,
            port: None, // stdio MCP
            configurable: false,
            settings: HashMap::new(),
        },
        // ── excalidraw (v0.2.33 Phase 2 — diagrams integration) ──────────
        //
        // Wrapper MCP at `claude_mcp_servers/wrappers/excalidraw_proxy.py`
        // (see `mcp_registration.rs:461-489`). Proxies the in-tree
        // vendored `excalidraw-mcp-server` fork with the same path
        // allowlist as the mermaid wrapper. Same launcher-visibility
        // story as mermaid: listed here so the global MCP Servers tab +
        // per-project Permissions tab show it (v0.2.34 Agent D fix).
        McpServerConfig {
            id: "excalidraw".to_string(),
            name: "Excalidraw diagrams".to_string(),
            description: "Create and edit Excalidraw scenes (`create_scene`, `add_element`, ...). Wraps an in-tree fork of `excalidraw-mcp-server` via a Python proxy that enforces the `.claude/diagrams/` scoped-path boundary. Used by the launcher's Diagrams tab embedded editor and by Claude when asked to sketch freehand whiteboard visuals.".to_string(),
            enabled: true, // Default-enabled — same rationale as mermaid above
            command: "python".to_string(),
            args: vec![
                "-m".to_string(),
                "claude_mcp_servers.wrappers.excalidraw_proxy".to_string(),
            ],
            env: HashMap::new(),
            min_tier: OrchestratorTier::Free,
            port: None, // stdio MCP
            configurable: false,
            settings: HashMap::new(),
        },
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    /// v0.2.34 Agent D: regression test — the default MCP catalog MUST
    /// include the diagrams wrapper MCPs so the launcher's global
    /// "MCP Servers" tab + per-project Permissions tab render them.
    /// Without this entry both surfaces were blind to mermaid/excalidraw
    /// even though `mcp_registration.rs` wrote them into ~/.claude.json
    /// (the catalog and the on-disk registration are two separate
    /// surfaces; both must list the MCP).
    #[test]
    fn default_mcp_servers_includes_diagrams_wrappers() {
        let servers = default_mcp_servers();
        let ids: Vec<&str> = servers.iter().map(|s| s.id.as_str()).collect();
        assert!(ids.contains(&"mermaid"), "default catalog must include mermaid wrapper MCP (got {:?})", ids);
        assert!(ids.contains(&"excalidraw"), "default catalog must include excalidraw wrapper MCP (got {:?})", ids);
    }

    /// v0.2.34 Agent D: shape regression — the diagrams wrappers are
    /// stdio MCPs (no port), free tier, default-enabled, not
    /// user-configurable (no per-MCP env). Locks the catalog entry's
    /// observable contract so a future refactor can't silently change
    /// the per-project Permissions tab's behaviour.
    #[test]
    fn diagrams_wrapper_entries_have_expected_shape() {
        let servers = default_mcp_servers();
        for id in &["mermaid", "excalidraw"] {
            let entry = servers
                .iter()
                .find(|s| s.id == *id)
                .unwrap_or_else(|| panic!("missing MCP entry for {}", id));
            assert!(entry.enabled, "{} should be default-enabled (opt-out diagrams feature)", id);
            assert_eq!(entry.min_tier, OrchestratorTier::Free, "{} should be free-tier", id);
            assert!(entry.port.is_none(), "{} is stdio MCP, port must be None", id);
            assert!(!entry.configurable, "{} has no user-editable env, configurable=false", id);
            assert!(entry.settings.is_empty(), "{} settings map should be empty", id);
            assert!(!entry.command.is_empty(), "{} command should be set", id);
        }
    }

    /// The catalog should now expose exactly the five default MCPs:
    /// weaviate-kg, search, playwright, mermaid, excalidraw. If a sixth
    /// is added, bump this count + the contains_diagrams_wrappers test
    /// will still pass. Pre-v0.2.34 this was 3.
    #[test]
    fn default_mcp_servers_count_is_five() {
        let servers = default_mcp_servers();
        assert_eq!(
            servers.len(),
            5,
            "expected 5 default MCPs (weaviate-kg, search, playwright, mermaid, excalidraw), got {} — if a new MCP was added, bump this count",
            servers.len()
        );
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

