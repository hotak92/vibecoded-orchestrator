use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use tauri::command;

use crate::types::{McpServerConfig, McpSetting, OrchestratorConfig, OrchestratorTier};

// ---------------------------------------------------------------------------
// Config persistence: ~/.vct/orchestrator.json
// ---------------------------------------------------------------------------

fn config_path() -> PathBuf {
    directories::UserDirs::new()
        .map(|d| d.home_dir().join(".vct").join("orchestrator.json"))
        .unwrap_or_else(|| PathBuf::from(".vct/orchestrator.json"))
}

fn load_config() -> OrchestratorConfig {
    let path = config_path();
    if !path.exists() {
        return OrchestratorConfig::default();
    }
    let data = std::fs::read_to_string(&path).unwrap_or_default();
    serde_json::from_str(&data).unwrap_or_default()
}

async fn save_config(config: &OrchestratorConfig) -> Result<(), String> {
    let path = config_path();
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await.ok();
    }
    let json = serde_json::to_string_pretty(config)
        .map_err(|e| format!("Serialize config: {}", e))?;
    tokio::fs::write(&path, json)
        .await
        .map_err(|e| format!("Write config: {}", e))?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Tier & feature gating
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FeatureFlags {
    pub tier: OrchestratorTier,
    pub can_auto_update: bool,
    pub can_disable_watermark: bool,
    pub has_rl_retrieval: bool,
    pub has_curated_agents: bool,
    pub has_mao: bool,
}

/// Get feature flags for the current user tier.
/// `user_apps` comes from Supabase profile (the frontend passes it).
#[command]
pub fn get_feature_flags(user_apps: Vec<String>) -> FeatureFlags {
    let tier = OrchestratorTier::from_apps(&user_apps);
    FeatureFlags {
        can_auto_update: tier.can_auto_update(),
        can_disable_watermark: tier.can_disable_watermark(),
        has_rl_retrieval: tier.has_rl_retrieval(),
        has_curated_agents: tier.has_curated_agents(),
        has_mao: tier.has_mao(),
        tier,
    }
}

// ---------------------------------------------------------------------------
// Orchestrator config
// ---------------------------------------------------------------------------

/// Load the full orchestrator config.
#[command]
pub fn get_orchestrator_config() -> OrchestratorConfig {
    load_config()
}

/// Save the full orchestrator config.
#[command]
pub async fn save_orchestrator_config(config: OrchestratorConfig) -> Result<(), String> {
    save_config(&config).await
}

/// Update a single top-level config field.
#[command]
pub async fn update_orchestrator_setting(key: String, value: String, user_apps: Vec<String>) -> Result<OrchestratorConfig, String> {
    let tier = OrchestratorTier::from_apps(&user_apps);
    let mut config = load_config();

    match key.as_str() {
        "watermark_enabled" => {
            let val: bool = value.parse().map_err(|_| "Invalid bool")?;
            // Free tier cannot disable watermark
            if !val && !tier.can_disable_watermark() {
                return Err("Upgrade to Pro to disable the watermark".to_string());
            }
            config.watermark_enabled = val;
        }
        "auto_update_enabled" => {
            let val: bool = value.parse().map_err(|_| "Invalid bool")?;
            if val && !tier.can_auto_update() {
                return Err("Upgrade to Pro for auto-updates".to_string());
            }
            config.auto_update_enabled = val;
        }
        "rl_retrieval_enabled" => {
            let val: bool = value.parse().map_err(|_| "Invalid bool")?;
            if val && !tier.has_rl_retrieval() {
                return Err("Upgrade to Pro for RL-scored retrieval".to_string());
            }
            config.rl_retrieval_enabled = val;
        }
        "telemetry_enabled" => {
            config.telemetry_enabled = value.parse().map_err(|_| "Invalid bool")?;
        }
        "telemetry_anonymous_usage" => {
            config.telemetry_anonymous_usage = value.parse().map_err(|_| "Invalid bool")?;
        }
        "install_path" => {
            config.install_path = value;
        }
        _ => return Err(format!("Unknown setting: {}", key)),
    }

    save_config(&config).await?;
    Ok(config)
}

// ---------------------------------------------------------------------------
// MCP server management
// ---------------------------------------------------------------------------

/// Get all MCP server configurations.
#[command]
pub fn get_mcp_servers() -> Vec<McpServerConfig> {
    load_config().mcp_servers
}

/// Toggle an MCP server on/off.
#[command]
pub async fn toggle_mcp_server(mcp_id: String, enabled: bool, user_apps: Vec<String>) -> Result<Vec<McpServerConfig>, String> {
    let tier = OrchestratorTier::from_apps(&user_apps);
    let mut config = load_config();

    let server = config.mcp_servers.iter_mut()
        .find(|s| s.id == mcp_id)
        .ok_or_else(|| format!("MCP server '{}' not found", mcp_id))?;

    // Check tier requirement
    if enabled && !tier_meets_requirement(&tier, &server.min_tier) {
        return Err(format!(
            "MCP '{}' requires {:?} tier or higher",
            server.name, server.min_tier
        ));
    }

    server.enabled = enabled;
    save_config(&config).await?;

    // Apply to Claude Code settings.json
    apply_mcp_to_claude_settings(&config).await?;

    Ok(config.mcp_servers)
}

/// Update an MCP server setting (e.g., change port, URL, collection name).
#[command]
pub async fn update_mcp_setting(
    mcp_id: String,
    setting_key: String,
    setting_value: String,
) -> Result<McpServerConfig, String> {
    let mut config = load_config();

    let server = config.mcp_servers.iter_mut()
        .find(|s| s.id == mcp_id)
        .ok_or_else(|| format!("MCP server '{}' not found", mcp_id))?;

    let setting = server.settings.get_mut(&setting_key)
        .ok_or_else(|| format!("Setting '{}' not found on MCP '{}'", setting_key, mcp_id))?;

    if !setting.editable {
        return Err(format!("Setting '{}' is not editable", setting_key));
    }

    setting.value = setting_value;
    let updated = server.clone();

    save_config(&config).await?;
    apply_mcp_to_claude_settings(&config).await?;

    Ok(updated)
}

/// Register a new custom MCP server (e.g., user adds Transcrypt live MCP).
///
/// Side effects:
/// 1. Persists the server entry into `~/.vct/orchestrator.json` (so the
///    launcher remembers it across restarts).
/// 2. Patches `~/.claude.json` `mcpServers.<id>` with a full
///    `{type, command, args, env}` block. This is the file Claude Code
///    reads at startup; without this step the server only existed in
///    the launcher's mental model.
/// 3. Re-applies the legacy env injection into the orchestrator's
///    `.claude/settings.json` for backward compat with the dashboard.
#[command]
pub async fn add_custom_mcp_server(server: McpServerConfig) -> Result<Vec<McpServerConfig>, String> {
    let mut config = load_config();

    // Check for duplicate ID
    if config.mcp_servers.iter().any(|s| s.id == server.id) {
        return Err(format!("MCP server '{}' already exists", server.id));
    }

    let id = server.id.clone();
    let entry = mcp_server_to_claude_entry(&server);

    config.mcp_servers.push(server);
    save_config(&config).await?;

    // Patch the user-scope Claude config so the new MCP is actually
    // visible to Claude Code. Failures here are surfaced — a silent
    // no-op was the original "add doesn't work" bug.
    let target = crate::mcp_registration::user_claude_json();
    crate::mcp_registration::register_mcp(&target, &id, &entry)?;

    apply_mcp_to_claude_settings(&config).await?;

    Ok(config.mcp_servers)
}

/// Convert a launcher McpServerConfig into the JSON shape Claude Code
/// expects under `mcpServers.<name>`. Filters out empty env entries so
/// the resulting JSON is clean.
fn mcp_server_to_claude_entry(server: &crate::types::McpServerConfig) -> serde_json::Value {
    let env: serde_json::Map<String, serde_json::Value> = server
        .env
        .iter()
        .filter(|(_, v)| !v.is_empty())
        .map(|(k, v)| (k.clone(), serde_json::Value::String(v.clone())))
        .collect();

    serde_json::json!({
        "type": "stdio",
        "command": server.command,
        "args": server.args,
        "env": env,
    })
}

/// Remove a custom MCP server.
#[command]
pub async fn remove_mcp_server(mcp_id: String) -> Result<Vec<McpServerConfig>, String> {
    let mut config = load_config();

    // Don't allow removing built-in servers
    let builtin = ["weaviate-kg", "ollama", "search", "code-embed"];
    if builtin.contains(&mcp_id.as_str()) {
        return Err(format!("Cannot remove built-in MCP server '{}'. Disable it instead.", mcp_id));
    }

    config.mcp_servers.retain(|s| s.id != mcp_id);
    save_config(&config).await?;

    // Mirror the removal into ~/.claude.json so Claude Code stops
    // launching the server on next start.
    let target = crate::mcp_registration::user_claude_json();
    let _ = crate::mcp_registration::deregister_mcp(&target, &mcp_id);

    apply_mcp_to_claude_settings(&config).await?;

    Ok(config.mcp_servers)
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn tier_meets_requirement(user_tier: &OrchestratorTier, required: &OrchestratorTier) -> bool {
    let tier_level = |t: &OrchestratorTier| match t {
        OrchestratorTier::Free => 0,
        OrchestratorTier::Pro => 1,
        OrchestratorTier::Mao => 2,
    };
    tier_level(user_tier) >= tier_level(required)
}

/// Write enabled MCP servers to the orchestrator's .claude/settings.json
/// so Claude Code picks them up.
async fn apply_mcp_to_claude_settings(config: &OrchestratorConfig) -> Result<(), String> {
    let install_path = PathBuf::from(&config.install_path);
    let settings_path = install_path.join(".claude").join("settings.json");

    if !settings_path.exists() {
        // Not installed yet — skip silently
        return Ok(());
    }

    let data = tokio::fs::read_to_string(&settings_path)
        .await
        .map_err(|e| format!("Read settings: {}", e))?;

    let mut settings: serde_json::Value =
        serde_json::from_str(&data).unwrap_or(serde_json::json!({}));

    // Build env block from enabled MCP servers
    let env = settings
        .get_mut("env")
        .and_then(|v| v.as_object_mut());

    if let Some(env_map) = env {
        // Inject MCP server settings into env
        for server in &config.mcp_servers {
            if server.enabled {
                for (key, setting) in &server.settings {
                    env_map.insert(key.clone(), serde_json::Value::String(setting.value.clone()));
                }
            }
        }

        // Watermark setting
        env_map.insert(
            "VCT_WATERMARK".to_string(),
            serde_json::Value::String(config.watermark_enabled.to_string()),
        );
    }

    let json = serde_json::to_string_pretty(&settings)
        .map_err(|e| format!("Serialize settings: {}", e))?;
    tokio::fs::write(&settings_path, json)
        .await
        .map_err(|e| format!("Write settings: {}", e))?;

    Ok(())
}
