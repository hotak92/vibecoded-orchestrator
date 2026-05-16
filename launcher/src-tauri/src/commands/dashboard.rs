use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use tauri::command;

use crate::types::{McpServerConfig, McpSettingType, OrchestratorConfig, OrchestratorTier};

// ---------------------------------------------------------------------------
// MCP secret keychain routing (P1-B, 2026-05-08)
// ---------------------------------------------------------------------------
//
// Settings flagged `setting_type == Secret` MUST NOT land in
// `~/.vct/orchestrator.json` or in `<install>/.claude/settings.json env` as
// plaintext. Route them through the OS keychain instead.
//
// Service namespace: `vct.global.mcp.<mcp_id>` (SecretScope::Global with
// module_id=`"mcp.<id>"`). Keep this constant — migration code reads from it.
const MCP_SECRET_MODULE_PREFIX: &str = "mcp.";

/// Build the (scope, module_id) tuple this module uses for storing MCP-server
/// secrets in the OS keychain. Centralised so `update_mcp_setting`,
/// `apply_mcp_to_claude_settings`, and the migration helper agree.
fn mcp_secret_module_id(mcp_id: &str) -> String {
    format!("{}{}", MCP_SECRET_MODULE_PREFIX, mcp_id)
}

// ---------------------------------------------------------------------------
// Config persistence: ~/.vct/orchestrator.json
// ---------------------------------------------------------------------------

fn config_path() -> PathBuf {
    crate::paths::vct_root_dir().join("orchestrator.json")
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
///
/// Side effects (P1-A fix, 2026-05-08):
/// 1. Persists `enabled` flag into `~/.vct/orchestrator.json`.
/// 2. Mirrors into `~/.claude.json mcpServers.<id>`:
///      - `enabled=true` → register the entry (Claude Code spawns the server).
///      - `enabled=false` → deregister the entry (Claude Code stops spawning).
/// 3. Re-runs `apply_mcp_to_claude_settings` so the orchestrator install's
///    `.claude/settings.json env` block reflects only enabled MCP settings.
///
/// Without step 2 the GUI would say "off" while Claude Code kept launching
/// the disabled server (the original bug). Mirrors the pattern in
/// `add_custom_mcp_server` / `remove_mcp_server`.
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
    // Snapshot the entry shape for ~/.claude.json BEFORE we drop the &mut.
    let entry = mcp_server_to_claude_entry(server);
    save_config(&config).await?;

    // Mirror the toggle into ~/.claude.json so Claude Code actually
    // honours the GUI flip. Soft-fail: a write hiccup must not roll back
    // the launcher's own config (the user already saw the toggle land).
    let target = crate::mcp_registration::user_claude_json();
    if enabled {
        crate::mcp_registration::register_mcp(&target, &mcp_id, &entry)?;
    } else {
        crate::mcp_registration::deregister_mcp(&target, &mcp_id)?;
    }

    // Apply to Claude Code settings.json (env block in orchestrator install).
    apply_mcp_to_claude_settings(&config).await?;

    Ok(config.mcp_servers)
}

/// Update an MCP server setting (e.g., change port, URL, collection name).
///
/// P1-B fix (2026-05-08): when `setting.setting_type == Secret`, the value
/// is routed through the OS keychain (`secrets::set` under
/// `SecretScope::Global` / `module_id = "mcp.<mcp_id>"`) instead of being
/// persisted into `~/.vct/orchestrator.json`. The JSON value is cleared
/// (empty string) so Secret material never lands in launcher settings on
/// disk. Non-Secret settings keep the existing JSON-only persistence.
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

    if setting.setting_type == McpSettingType::Secret {
        // Route to keychain. We persist an EMPTY string in the JSON config
        // so Secret material never sits at rest in `~/.vct/orchestrator.json`.
        // An empty string also means `apply_mcp_to_claude_settings` skips
        // the env emission for this key (existing `if !value.is_empty()` is
        // gone — we now skip explicitly via the type check). The keychain
        // is the authoritative store; the consuming MCP server is expected
        // to read its own secrets via the launcher hub's
        // `GET /api/v1/projects/{id}/env` endpoint (resolved via the shared
        // helper `templates/scripts/vct_secrets_resolve.sh`), which gates
        // every read on the per-project active flag.
        let scope = crate::secrets::SecretScope::Global;
        let module_id = mcp_secret_module_id(&mcp_id);
        if setting_value.is_empty() {
            // Empty string from the GUI = "clear this secret". Delete from
            // keychain and leave the JSON value empty.
            let _ = crate::secrets::delete(scope, &module_id, &setting_key);
        } else {
            crate::secrets::set(scope, &module_id, &setting_key, &setting_value)?;
        }
        setting.value = String::new();
    } else {
        setting.value = setting_value;
    }
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

    // Don't allow removing built-in servers.
    // Note: "ollama" was removed from the default MCP list in v0.2.11
    // (Ollama MCP deprecated; Ollama infrastructure unchanged).
    let builtin = ["weaviate-kg", "search", "playwright"];
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
// One-shot migration: plaintext Secret values → keychain (P1-B, 2026-05-08)
// ---------------------------------------------------------------------------

/// `app_state` flag — set after the migration runs successfully so a launcher
/// upgrade only does the sweep once. Stale launchers (pre-fix) never write
/// this row, so the first post-fix start finds it `None` and runs the sweep.
const APP_STATE_KEY_MCP_SECRETS_MIGRATED: &str = "mcp_secrets.plaintext_to_keychain.v1";

/// Sweep `~/.vct/orchestrator.json` once: any non-empty `value` on a
/// Secret-typed setting is moved into the OS keychain (same namespace
/// `update_mcp_setting` writes to) and cleared from the JSON config.
///
/// Idempotent and self-gated:
///   * If the app_state flag is already set → returns immediately.
///   * If the config file doesn't exist → flips the flag and returns.
///   * Per-secret keychain write failures don't abort the migration; we
///     leave that one secret alone (so the user retains the plaintext
///     copy in JSON until they re-set it via the GUI), and keep going.
///
/// Soft-fail by design: a migration hiccup must NEVER block launcher
/// startup. The caller logs warnings; the app boots regardless.
pub fn migrate_plaintext_mcp_secrets_to_keychain(
    db: &crate::db::Db,
) -> Result<MigrationReport, String> {
    let mut report = MigrationReport::default();

    // Already migrated? skip silently.
    let already = db
        .app_state_get_bool(APP_STATE_KEY_MCP_SECRETS_MIGRATED)
        .ok()
        .flatten()
        .unwrap_or(false);
    if already {
        report.already_done = true;
        return Ok(report);
    }

    let path = config_path();
    if !path.exists() {
        // Nothing to migrate — flip the flag so we don't re-scan on every boot.
        db.app_state_set_bool(APP_STATE_KEY_MCP_SECRETS_MIGRATED, true)?;
        return Ok(report);
    }

    let raw = std::fs::read_to_string(&path)
        .map_err(|e| format!("read {}: {}", path.display(), e))?;
    let mut config: OrchestratorConfig = match serde_json::from_str(&raw) {
        Ok(c) => c,
        Err(e) => {
            // Don't try to repair a corrupt config — record the warning,
            // skip the migration, do NOT flip the flag (so a future fix
            // can retry).
            return Err(format!("parse {}: {}", path.display(), e));
        }
    };

    let mut config_dirty = false;
    for server in config.mcp_servers.iter_mut() {
        for (key, setting) in server.settings.iter_mut() {
            if setting.setting_type != McpSettingType::Secret {
                continue;
            }
            if setting.value.is_empty() {
                continue;
            }
            // Move to keychain.
            let module_id = mcp_secret_module_id(&server.id);
            match crate::secrets::set(
                crate::secrets::SecretScope::Global,
                &module_id,
                key,
                &setting.value,
            ) {
                Ok(()) => {
                    setting.value = String::new();
                    config_dirty = true;
                    report.migrated_keys.push(format!("{}/{}", server.id, key));
                }
                Err(e) => {
                    // Keychain failure (no Secret Service / Keychain on this
                    // host) — leave the JSON value as is. The user retains
                    // their secret; they can re-set it through the GUI once
                    // the keychain backend comes back. We do NOT flip the
                    // flag in this case so a future boot retries.
                    report.skipped_keys.push(format!("{}/{}: {}", server.id, key, e));
                }
            }
        }
    }

    if config_dirty {
        let json = serde_json::to_string_pretty(&config)
            .map_err(|e| format!("serialize: {}", e))?;
        std::fs::write(&path, json)
            .map_err(|e| format!("write {}: {}", path.display(), e))?;
    }

    // Only flip the flag if EVERY found secret was migrated successfully.
    // A partial migration leaves the flag unset so a later boot re-tries.
    if report.skipped_keys.is_empty() {
        db.app_state_set_bool(APP_STATE_KEY_MCP_SECRETS_MIGRATED, true)?;
        report.flag_set = true;
    }

    Ok(report)
}

#[derive(Debug, Default)]
pub struct MigrationReport {
    pub already_done: bool,
    pub migrated_keys: Vec<String>,
    pub skipped_keys: Vec<String>,
    pub flag_set: bool,
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
        // Inject MCP server settings into env.
        //
        // P1-B fix (2026-05-08): Secret-typed settings are NEVER emitted
        // here — they live in the OS keychain (see `update_mcp_setting`)
        // and the consuming MCP server is expected to read them via the
        // launcher hub's `/api/v1/projects/{id}/env` endpoint (resolved
        // via the shared `vct_secrets_resolve` helper). Emitting the
        // empty placeholder here would mask a real keychain miss as
        // "secret = empty string", which is worse than absent.
        for server in &config.mcp_servers {
            if server.enabled {
                for (key, setting) in &server.settings {
                    if setting.setting_type == McpSettingType::Secret {
                        continue;
                    }
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

// ---------------------------------------------------------------------------
// Tests (P1-A, P1-B — 2026-05-08)
// ---------------------------------------------------------------------------
//
// These tests pin the GUI-correctness fixes in this file:
//
//   * `toggle_mcp_server` mirrors enable/disable into `~/.claude.json
//     mcpServers.<id>` so Claude Code stops/starts spawning the server
//     in lockstep with the GUI flip. Without this mirror the GUI lied —
//     a "disabled" MCP kept running, a "newly-enabled" MCP didn't start.
//
//   * `update_mcp_setting` routes Secret-typed values to the OS keychain
//     instead of `~/.vct/orchestrator.json` plaintext, and
//     `apply_mcp_to_claude_settings` omits Secret values from the env
//     block emission. Plus a first-run migration sweep moves any
//     pre-fix plaintext secrets into the keychain.
//
// Test isolation pattern:
//   - `VCT_STATE_DIR` overrides the launcher's state root → temp dir,
//     so the test's orchestrator.json doesn't touch the real user's.
//   - `HOME` is overridden so `user_claude_json()` resolves into the
//     temp dir → the test's ~/.claude.json doesn't touch the real one.
//   - A process-wide Mutex serialises tests that mutate these env vars
//     so parallel runs don't observe each other.
//   - Keychain-touching tests probe via `keyring_available()`; CI
//     hosts without an OS keychain backend skip silently.

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{McpSetting, McpSettingType};
    use std::collections::HashMap;
    use std::sync::Mutex;

    // Process-wide serialisation for env-var-mutating tests. `HOME` and
    // `VCT_STATE_DIR` are global; if two tests change them concurrently
    // they race. The launcher's `paths.rs::tests` uses the same pattern.
    //
    // 0.1.7 H1: kept for backward-compat with any future test in this
    // module that wants intra-module-only serialisation. Production
    // tests now route through the shared
    // `crate::secrets::test_serialize::keychain_serialize_lock` so they
    // serialise against installer + modules_api hub-resolver tests too.
    #[allow(dead_code)]
    static SERIALIZE: Mutex<()> = Mutex::new(());

    fn keyring_available() -> bool {
        let entry = match keyring::Entry::new("vct.test.dashboard.probe", "probe") {
            Ok(e) => e,
            Err(_) => return false,
        };
        if entry.set_password("canary").is_err() {
            return false;
        }
        let _ = entry.delete_credential();
        true
    }

    /// Set up a temp dir as the launcher's state root + the user's HOME.
    /// Returns a guard that restores prior env on drop and the temp path.
    /// Run the closure under the SERIALIZE mutex.
    struct EnvGuard {
        prev_state: Option<std::ffi::OsString>,
        prev_home: Option<std::ffi::OsString>,
        _lock: std::sync::MutexGuard<'static, ()>,
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            match self.prev_state.take() {
                Some(v) => std::env::set_var("VCT_STATE_DIR", v),
                None => std::env::remove_var("VCT_STATE_DIR"),
            }
            match self.prev_home.take() {
                Some(v) => std::env::set_var("HOME", v),
                None => std::env::remove_var("HOME"),
            }
        }
    }

    fn setup_temp_env() -> (std::path::PathBuf, EnvGuard) {
        // 0.1.7 H1 fork-readiness sweep (2026-05-08): use the process-wide
        // keychain mutex from `crate::secrets::test_serialize` so the
        // dashboard plaintext-migration tests serialise against installer
        // and modules_api hub-resolver tests that hit the same keychain
        // slots. The pre-H1 module-private `SERIALIZE` only blocked
        // intra-module races — see the docstring on the new shared mutex
        // in `secrets.rs::test_serialize` for the cross-module rationale.
        let lock = crate::secrets::test_serialize::keychain_serialize_lock();
        let tmp = std::env::temp_dir().join(format!(
            "vct-dashboard-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        let prev_state = std::env::var_os("VCT_STATE_DIR");
        let prev_home = std::env::var_os("HOME");
        std::env::set_var("VCT_STATE_DIR", &tmp);
        std::env::set_var("HOME", &tmp);
        // Note: previous Fix #3 also isolated `VCT_SECRETS_DIR` here so
        // the keychain → ~/.vct-secrets/ bridge stayed in the temp dir.
        // The bridge has been removed in 0.1.7 (the launcher hub's
        // `/projects/{id}/env` endpoint replaces it); secrets::set/delete
        // are pure-keychain again, so no per-test secrets-root isolation
        // is needed here.
        let guard = EnvGuard {
            prev_state,
            prev_home,
            _lock: lock,
        };
        (tmp, guard)
    }

    /// Pre-seed `~/.vct/orchestrator.json` with a default config so
    /// `toggle_mcp_server` finds the built-in MCPs. Returns the path.
    fn seed_default_config(state_dir: &std::path::Path) -> std::path::PathBuf {
        std::fs::create_dir_all(state_dir).unwrap();
        let config = OrchestratorConfig::default();
        let path = state_dir.join("orchestrator.json");
        std::fs::write(
            &path,
            serde_json::to_string_pretty(&config).unwrap(),
        )
        .unwrap();
        path
    }

    fn user_apps_free() -> Vec<String> {
        Vec::new()
    }

    fn read_claude_json(home: &std::path::Path) -> serde_json::Value {
        let p = home.join(".claude.json");
        if !p.exists() {
            return serde_json::json!({});
        }
        let raw = std::fs::read_to_string(&p).unwrap();
        serde_json::from_str(&raw).unwrap_or_else(|_| serde_json::json!({}))
    }

    fn rt() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
    }

    // ─── Fix #1: toggle_mcp_server mirrors to ~/.claude.json ─────────────

    /// Bug repro for P1-A: `toggle_mcp_server(enabled=false)` flipped the
    /// flag in orchestrator.json but left `~/.claude.json mcpServers.<id>`
    /// in place, so Claude Code kept spawning the "disabled" server.
    /// Post-fix: the entry is removed from ~/.claude.json on disable.
    ///
    /// v0.2.11: was `ollama`; now uses `search` (Ollama MCP removed from
    /// the default install — see types.rs `default_mcp_servers` comment).
    #[test]
    fn test_toggle_mcp_server_off_removes_from_claude_json() {
        let (home, _guard) = setup_temp_env();
        seed_default_config(&home);

        // Pre-seed ~/.claude.json with the search entry registered (mimic
        // post-install state where the launcher had already mirrored
        // every default-enabled server during install).
        let claude_json = home.join(".claude.json");
        std::fs::write(
            &claude_json,
            serde_json::to_string_pretty(&serde_json::json!({
                "mcpServers": {
                    "search": {
                        "type": "stdio",
                        "command": "claude_mcp_servers/search_mcp/server.py",
                        "args": [],
                        "env": {}
                    }
                }
            })).unwrap(),
        )
        .unwrap();

        rt().block_on(async {
            toggle_mcp_server("search".to_string(), false, user_apps_free())
                .await
                .expect("toggle_mcp_server off");
        });

        let cj = read_claude_json(&home);
        assert!(
            cj["mcpServers"].get("search").is_none(),
            "expected `search` removed from ~/.claude.json mcpServers, got: {}",
            cj["mcpServers"]
        );

        // And the launcher's own config carries enabled=false.
        let cfg_text = std::fs::read_to_string(home.join("orchestrator.json")).unwrap();
        let cfg: serde_json::Value = serde_json::from_str(&cfg_text).unwrap();
        let search = cfg["mcp_servers"]
            .as_array()
            .unwrap()
            .iter()
            .find(|s| s["id"] == "search")
            .expect("search entry");
        assert_eq!(search["enabled"], serde_json::Value::Bool(false));
    }

    /// Inverse of the off-case: toggle on must register the entry back
    /// into ~/.claude.json with the canonical {type, command, args, env}
    /// shape so Claude Code starts spawning it.
    ///
    /// (v0.2.5: previously used `code-embed`. v0.2.11: was `ollama`; now
    /// uses `search` after Ollama MCP was removed from the default install.
    /// Exercise the toggle-on path by first flipping `search` off in the
    /// seeded config and then toggling it back on.)
    #[test]
    fn test_toggle_mcp_server_on_re_registers_in_claude_json() {
        let (home, _guard) = setup_temp_env();
        let cfg_path = seed_default_config(&home);

        // Flip `search` to disabled in the seeded config so the toggle-on
        // call has something disabled to enable.
        let mut cfg: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&cfg_path).unwrap()).unwrap();
        for entry in cfg["mcp_servers"].as_array_mut().unwrap() {
            if entry["id"] == "search" {
                entry["enabled"] = serde_json::Value::Bool(false);
            }
        }
        std::fs::write(&cfg_path, serde_json::to_string_pretty(&cfg).unwrap()).unwrap();

        rt().block_on(async {
            toggle_mcp_server("search".to_string(), true, user_apps_free())
                .await
                .expect("toggle_mcp_server on");
        });

        let cj = read_claude_json(&home);
        let entry = &cj["mcpServers"]["search"];
        assert!(
            entry.is_object(),
            "expected `search` registered in ~/.claude.json mcpServers, got: {}",
            cj["mcpServers"]
        );
        // Canonical shape ({type:stdio, command, args, env}) — same as
        // `mcp_server_to_claude_entry` produces.
        assert_eq!(entry["type"], "stdio");
        assert!(
            entry.get("command").is_some(),
            "missing command field: {}",
            entry
        );
        assert!(
            entry.get("args").is_some(),
            "missing args field: {}",
            entry
        );
        assert!(
            entry.get("env").is_some(),
            "missing env field: {}",
            entry
        );
    }

    // ─── Fix #2: Secret-typed settings route through keychain ────────────

    /// P1-B: `update_mcp_setting` with a Secret-typed setting must route
    /// the value to the keychain and leave the JSON config value EMPTY.
    /// Pre-fix the value landed verbatim in orchestrator.json.
    ///
    /// v0.2.11: `search` no longer ships a Secret-typed setting (GITHUB_TOKEN
    /// was removed with the GitHub code-search tool). We inject a synthetic
    /// custom MCP with a Secret setting to exercise the keychain routing path
    /// — the underlying mechanism in `update_mcp_setting` is unchanged.
    ///
    /// Skipped when the OS keychain backend is unavailable (CI containers,
    /// headless build hosts).
    #[test]
    fn test_update_mcp_setting_secret_routes_to_keychain_not_json() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend in this test env");
            return;
        }
        let (home, _guard) = setup_temp_env();

        // Seed a config that includes a custom MCP with a Secret-typed key.
        let canary = format!("canary-test-pat-{}", uuid::Uuid::new_v4().simple());
        let mut config = OrchestratorConfig::default();
        let mut secret_settings = HashMap::new();
        secret_settings.insert(
            "MY_API_KEY".to_string(),
            McpSetting {
                label: "API Key".to_string(),
                value: String::new(),
                setting_type: McpSettingType::Secret,
                description: "Test secret".to_string(),
                editable: true,
            },
        );
        config.mcp_servers.push(McpServerConfig {
            id: "test-secret-mcp".to_string(),
            name: "Test Secret MCP".to_string(),
            description: "Synthetic MCP for keychain routing test".to_string(),
            enabled: true,
            command: "test".to_string(),
            args: vec![],
            env: HashMap::new(),
            min_tier: OrchestratorTier::Free,
            port: None,
            configurable: true,
            settings: secret_settings,
        });
        let path = home.join("orchestrator.json");
        std::fs::write(&path, serde_json::to_string_pretty(&config).unwrap()).unwrap();

        rt().block_on(async {
            update_mcp_setting(
                "test-secret-mcp".to_string(),
                "MY_API_KEY".to_string(),
                canary.clone(),
            )
            .await
            .expect("update_mcp_setting Secret");
        });

        // 1) JSON config value is EMPTY (NOT the canary).
        let cfg_text = std::fs::read_to_string(&path).unwrap();
        assert!(
            !cfg_text.contains(&canary),
            "leaked canary into orchestrator.json: {}",
            cfg_text
        );
        let cfg: OrchestratorConfig = serde_json::from_str(&cfg_text).unwrap();
        let mcp_entry = cfg
            .mcp_servers
            .iter()
            .find(|s| s.id == "test-secret-mcp")
            .expect("test-secret-mcp entry");
        let key_setting = mcp_entry
            .settings
            .get("MY_API_KEY")
            .expect("MY_API_KEY setting");
        assert_eq!(
            key_setting.value, "",
            "MY_API_KEY value should be cleared in JSON: {:?}",
            key_setting
        );
        assert_eq!(key_setting.setting_type, McpSettingType::Secret);

        // 2) Keychain has the canary at the documented namespace.
        let kc = crate::secrets::get(
            crate::secrets::SecretScope::Global,
            &mcp_secret_module_id("test-secret-mcp"),
            "MY_API_KEY",
        )
        .expect("keychain get");
        assert_eq!(
            kc.as_deref(),
            Some(canary.as_str()),
            "keychain did not receive the secret"
        );

        // Cleanup keychain best-effort.
        let _ = crate::secrets::delete(
            crate::secrets::SecretScope::Global,
            &mcp_secret_module_id("test-secret-mcp"),
            "MY_API_KEY",
        );
    }

    /// P1-B: when the orchestrator install's `.claude/settings.json` is
    /// rewritten with `apply_mcp_to_claude_settings`, Secret-typed
    /// settings MUST NOT appear in the env block. Pre-fix the cleared
    /// empty string still landed in env (same key, value=""), masking a
    /// keychain miss as "secret = empty string" which is worse than
    /// absent.
    #[test]
    fn test_apply_mcp_to_claude_settings_omits_secret_values() {
        let (home, _guard) = setup_temp_env();

        // Build a config whose install_path is a temp dir with a seeded
        // .claude/settings.json. The function reads the existing file
        // and merges the env block.
        let install_dir = home.join("orch-install");
        let claude_dir = install_dir.join(".claude");
        std::fs::create_dir_all(&claude_dir).unwrap();
        let settings_path = claude_dir.join("settings.json");
        std::fs::write(
            &settings_path,
            r#"{"env": {"PRE_EXISTING": "keep"}}"#,
        )
        .unwrap();

        let mut config = OrchestratorConfig::default();
        config.install_path = install_dir.display().to_string();
        // Inject a fake MCP with one Secret-typed setting + one Text-typed.
        let mut mcp_settings: HashMap<String, McpSetting> = HashMap::new();
        mcp_settings.insert(
            "MY_SECRET".to_string(),
            McpSetting {
                label: "My Secret".to_string(),
                value: "should-not-appear".to_string(),
                setting_type: McpSettingType::Secret,
                description: String::new(),
                editable: true,
            },
        );
        mcp_settings.insert(
            "MY_VISIBLE".to_string(),
            McpSetting {
                label: "Visible".to_string(),
                value: "ok-emit".to_string(),
                setting_type: McpSettingType::Text,
                description: String::new(),
                editable: true,
            },
        );
        let test_mcp = McpServerConfig {
            id: "test-mcp".to_string(),
            name: "Test MCP".to_string(),
            description: String::new(),
            enabled: true,
            command: "test".to_string(),
            args: vec![],
            env: HashMap::new(),
            min_tier: OrchestratorTier::Free,
            port: None,
            configurable: true,
            settings: mcp_settings,
        };
        config.mcp_servers.push(test_mcp);

        rt().block_on(async {
            apply_mcp_to_claude_settings(&config).await.unwrap();
        });

        let raw = std::fs::read_to_string(&settings_path).unwrap();
        // Even string-search the raw file: the canary value MUST NOT be
        // anywhere on disk under .claude/settings.json.
        assert!(
            !raw.contains("should-not-appear"),
            "Secret value leaked into .claude/settings.json: {}",
            raw
        );
        let parsed: serde_json::Value = serde_json::from_str(&raw).unwrap();
        let env = &parsed["env"];
        assert!(
            env.get("MY_SECRET").is_none(),
            "Secret key emitted into env block: {}",
            env
        );
        // Non-Secret value still emitted, pre-existing keys preserved.
        assert_eq!(env["MY_VISIBLE"], "ok-emit");
        assert_eq!(env["PRE_EXISTING"], "keep");
    }

    /// P1-B: the one-shot migration on first launcher start must move
    /// pre-existing plaintext Secret values from `~/.vct/orchestrator.json`
    /// into the keychain and clear the JSON values. Idempotent: a second
    /// run finds the app_state flag set and does nothing.
    ///
    /// v0.2.11: `search` no longer has a Secret-typed setting (GITHUB_TOKEN
    /// removed). We inject a synthetic custom MCP with a Secret setting to
    /// exercise the migration sweep — the sweep logic is unchanged.
    #[test]
    fn test_first_run_migrates_plaintext_secrets_to_keychain() {
        if !keyring_available() {
            eprintln!("[skip] no OS keychain backend in this test env");
            return;
        }
        let (home, _guard) = setup_temp_env();

        // Seed orchestrator.json with a plaintext Secret value (the
        // pre-fix bad state). Use a unique canary.
        let canary = format!(
            "migration-canary-{}",
            uuid::Uuid::new_v4().simple()
        );
        // Build a config with a synthetic MCP carrying a plaintext Secret.
        let mut config = OrchestratorConfig::default();
        let mut secret_settings = HashMap::new();
        secret_settings.insert(
            "LEGACY_PAT".to_string(),
            McpSetting {
                label: "Legacy PAT".to_string(),
                value: canary.clone(), // plaintext — the pre-fix bad state
                setting_type: McpSettingType::Secret,
                description: "Test secret for migration sweep".to_string(),
                editable: true,
            },
        );
        config.mcp_servers.push(McpServerConfig {
            id: "legacy-mcp".to_string(),
            name: "Legacy MCP".to_string(),
            description: "Synthetic pre-fix MCP for migration test".to_string(),
            enabled: true,
            command: "legacy".to_string(),
            args: vec![],
            env: HashMap::new(),
            min_tier: OrchestratorTier::Free,
            port: None,
            configurable: true,
            settings: secret_settings,
        });
        // Note: VCT_STATE_DIR points at `home`, so config_path() resolves
        // to `home/orchestrator.json`. (Setting it to `home/.vct` would
        // also work but the env override in setup_temp_env uses `home`
        // directly to avoid a redundant subdir.)
        let path = home.join("orchestrator.json");
        std::fs::write(&path, serde_json::to_string_pretty(&config).unwrap()).unwrap();

        let db = crate::db::Db::open_in_memory().unwrap();

        // Run #1: should migrate.
        let report = migrate_plaintext_mcp_secrets_to_keychain(&db).unwrap();
        assert!(
            !report.already_done,
            "first run should not be already_done"
        );
        assert!(
            report.migrated_keys.iter().any(|k| k == "legacy-mcp/LEGACY_PAT"),
            "expected legacy-mcp/LEGACY_PAT migrated, got: {:?}",
            report.migrated_keys
        );
        assert!(
            report.flag_set,
            "flag should flip after a clean migration"
        );

        // JSON cleared.
        let after = std::fs::read_to_string(&path).unwrap();
        assert!(
            !after.contains(&canary),
            "canary still in orchestrator.json post-migration: {}",
            after
        );

        // Keychain has it.
        let kc = crate::secrets::get(
            crate::secrets::SecretScope::Global,
            &mcp_secret_module_id("legacy-mcp"),
            "LEGACY_PAT",
        )
        .unwrap();
        assert_eq!(kc.as_deref(), Some(canary.as_str()));

        // Run #2: idempotent — already_done short-circuits.
        let report2 = migrate_plaintext_mcp_secrets_to_keychain(&db).unwrap();
        assert!(report2.already_done, "second run should be a no-op");
        assert!(
            report2.migrated_keys.is_empty(),
            "second run should report nothing migrated: {:?}",
            report2.migrated_keys
        );

        // Cleanup.
        let _ = crate::secrets::delete(
            crate::secrets::SecretScope::Global,
            &mcp_secret_module_id("legacy-mcp"),
            "LEGACY_PAT",
        );
    }
}
