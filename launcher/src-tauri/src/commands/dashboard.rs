use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use tauri::{command, State};

use crate::db::Db;
use crate::types::{McpServerConfig, McpSettingType, OrchestratorConfig, OrchestratorTier};
use vct_launcher_core::licensing::tier_rank;

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
// Tier-aware error copy
// ---------------------------------------------------------------------------
//
// The launcher used to hard-code "Upgrade to Pro" in three tier-gated error
// messages. That copy lies if the feature actually requires a higher tier
// (MAO, Enterprise) than Pro — and worse, it sends Pro users to an upsell
// flow they've already completed. `tier_required_message` resolves the
// label from the gate's `min_tier` so the same helper can produce
// "requires a Pro or higher tier license." OR "requires a MAO or higher
// tier license." without a copy-paste cluster.
//
// Forward-compatible: unknown tier strings flow through verbatim so a
// new tier added in OrchestratorTier (say "Enterprise") yields a clean
// "requires a Enterprise or higher tier license." message without
// touching this helper. (#26 polish, v0.2.31.)

/// Build a user-facing "this feature needs a higher tier" message.
///
/// `min_tier` is the lowercase tier slug from OrchestratorTier
/// (free/pro/mao/enterprise/admin). `feature` is a short noun phrase
/// describing what's gated — capitalised at start (e.g. "Auto-updates",
/// "RL-scored retrieval").
fn tier_required_message(min_tier: &str, feature: &str) -> String {
    let tier_label = match min_tier {
        "free" => "any",  // shouldn't happen, but defensive
        "pro" => "Pro",
        "mao" => "MAO",
        "enterprise" => "Enterprise",
        "admin" => "Admin",
        other => other,  // forward-compat: unknown tier → use literal
    };
    format!("{feature} requires a {tier_label} or higher tier license.")
}

// ---------------------------------------------------------------------------
// Tier & feature gating
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FeatureFlags {
    /// Lowercase tier slug from `tier_cache.orchestrator_tier`
    /// (`free` / `pro` / `mao` / `enterprise` / `admin`). Was the
    /// 3-variant `OrchestratorTier` enum pre-v0.2.54; the wire shape is
    /// unchanged for the 3 legacy values (serde already emitted the
    /// lowercase slug) and the frontend `tierLabel`/`tierColor` helpers
    /// pass unknown slugs through verbatim.
    pub tier: String,
    pub can_auto_update: bool,
    pub has_rl_retrieval: bool,
    pub has_curated_agents: bool,
    pub has_mao: bool,
}

/// Pure flag computation from a tier slug. Pro features unlock at
/// rank >= pro; MAO features at rank >= mao. `enterprise`/`admin` rank
/// above `mao` (see `licensing::tier_rank`), so they inherit every
/// flag — mirroring `VCThelpers/license/validator.py::TIER_FEATURES`
/// semantics (`require_tier` is a >= comparison on TIER_ORDER).
fn feature_flags_for_tier(tier: &str) -> FeatureFlags {
    let rank = tier_rank(tier);
    let pro = rank >= tier_rank("pro");
    let mao = rank >= tier_rank("mao");
    FeatureFlags {
        tier: tier.to_string(),
        can_auto_update: pro,
        has_rl_retrieval: pro,
        has_curated_agents: pro,
        has_mao: mao,
    }
}

/// Resolve the current tier slug from the launcher's `tier_cache` row —
/// the SAME source `license_get_tier` serves to the ActivationModal.
///
/// v0.2.54 Track H (P0-5): pre-fix, the dashboard commands derived the
/// tier from a frontend-supplied `user_apps` list (Supabase
/// `profiles.apps`). License-key activation never writes that list, so
/// Pro customers saw Free gates everywhere in the dashboard. The tier
/// cache is written by `license_refresh` / `license_activate` after a
/// server-side `/validate-tier` round-trip and is the canonical local
/// tier state.
///
/// Fail-open-to-free: a DB read error yields `"free"` (same posture as
/// `modules::is_module_licensed_v2`) — feature gating degrades to the
/// free tier rather than erroring the whole dashboard.
fn current_tier_slug(db: &Db) -> String {
    db.get_tier_cache()
        .map(|row| row.orchestrator_tier)
        .unwrap_or_else(|_| "free".to_string())
}

/// Get feature flags for the current cached license tier.
#[command]
pub fn get_feature_flags(db: State<'_, Db>) -> FeatureFlags {
    feature_flags_for_tier(&current_tier_slug(&db))
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
///
/// Tier gating reads the cached license tier (`tier_cache`) — see
/// `current_tier_slug` for the P0-5 rationale.
#[command]
pub async fn update_orchestrator_setting(key: String, value: String, db: State<'_, Db>) -> Result<OrchestratorConfig, String> {
    let tier = current_tier_slug(&db);
    update_orchestrator_setting_inner(key, value, &tier).await
}

/// Testable core of `update_orchestrator_setting` — takes the resolved
/// tier slug so unit tests don't need a managed `State<Db>`.
async fn update_orchestrator_setting_inner(
    key: String,
    value: String,
    tier: &str,
) -> Result<OrchestratorConfig, String> {
    let flags = feature_flags_for_tier(tier);
    let mut config = load_config();

    match key.as_str() {
        // v0.2.54 Track H: the "watermark_enabled" arm was removed along
        // with the watermark gate (no consumer ever shipped).
        "auto_update_enabled" => {
            let val: bool = value.parse().map_err(|_| "Invalid bool")?;
            if val && !flags.can_auto_update {
                return Err(tier_required_message("pro", "Auto-updates"));
            }
            config.auto_update_enabled = val;
        }
        "rl_retrieval_enabled" => {
            let val: bool = value.parse().map_err(|_| "Invalid bool")?;
            if val && !flags.has_rl_retrieval {
                return Err(tier_required_message("pro", "RL-scored retrieval"));
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
pub async fn toggle_mcp_server(mcp_id: String, enabled: bool, db: State<'_, Db>) -> Result<Vec<McpServerConfig>, String> {
    let tier = current_tier_slug(&db);
    toggle_mcp_server_inner(mcp_id, enabled, &tier).await
}

/// Testable core of `toggle_mcp_server` — takes the resolved tier slug
/// so unit tests don't need a managed `State<Db>`.
async fn toggle_mcp_server_inner(mcp_id: String, enabled: bool, tier: &str) -> Result<Vec<McpServerConfig>, String> {
    let mut config = load_config();
    // Cloned up front: composing the canonical bundled entry below needs
    // the install path while `server` mutably borrows `config`.
    let install_path = config.install_path.clone();

    let server = config.mcp_servers.iter_mut()
        .find(|s| s.id == mcp_id)
        .ok_or_else(|| format!("MCP server '{}' not found", mcp_id))?;

    // Check tier requirement
    if enabled && !tier_meets_requirement(tier, &server.min_tier) {
        return Err(format!(
            "MCP '{}' requires {:?} tier or higher",
            server.name, server.min_tier
        ));
    }

    server.enabled = enabled;
    // Snapshot the entry shape for ~/.claude.json BEFORE we drop the &mut.
    //
    // F-2 (v0.2.73): bundled MCP ids MUST be re-registered with the
    // CANONICAL entry from `build_default_mcp_entries` (absolute
    // venv-python / real wrapper module args / filtered env) — NOT the
    // GUI-catalog shape from `mcp_server_to_claude_entry`. The catalog's
    // command fields are display stubs (relative .py path for
    // weaviate-kg, bare `python` for the diagram wrappers, empty env):
    // writing them into ~/.claude.json produced an entry Claude Code
    // could not spawn, so a disable→enable cycle permanently broke the
    // MCP until the next `install.py --update` rewrote it. The catalog
    // shape remains correct for user-added custom MCPs (the user
    // supplied a real command at add time).
    //
    // Conservative arm: if the id is bundled but the canonical entry
    // cannot be composed (no venv-python under install_path), the whole
    // toggle FAILS before any state is persisted — surfacing the broken
    // install beats silently writing a broken entry.
    let entry = if enabled {
        match crate::mcp_registration::default_entry_for_bundled_mcp(
            std::path::Path::new(&install_path),
            &mcp_id,
            // Canonical default ports — same posture as
            // `maintenance.rs::rerun_mcp_registration`: re-registration
            // paths don't re-read user-overridden ports from app_state
            // today (see the comment there).
            crate::mcp_registration::ServicePorts::default(),
        )? {
            Some((canonical, _dropped)) => canonical,
            None => mcp_server_to_claude_entry(server),
        }
    } else {
        // Disable never registers an entry; placeholder is unused.
        serde_json::Value::Null
    };
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

/// Register a new custom MCP server (e.g., user adds an ecosystem app's live MCP).
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

    // Don't allow removing built-in servers. F-3 (v0.2.73): derive the
    // protection set from the SINGLE catalog source of truth
    // (`is_bundled_mcp`, backed by `BUNDLED_MCP_NAMES`) instead of a
    // hand-maintained 3-name list that disagreed with the 8-name catalog.
    // The old list omitted mermaid/excalidraw/etc., so `remove_mcp_server`
    // would delete a bundled-but-default-disabled MCP AND deregister it from
    // ~/.claude.json — then the next `install-bundle --update` silently
    // re-adds it (the exact "delete vs disable" confusion CLAUDE.md rule 2
    // forbids). Protecting EVERY bundled MCP steers the user to disable
    // (enabled=false, survives updates) rather than remove.
    if vct_launcher_core::db::project_mcp_servers::is_bundled_mcp(&mcp_id) {
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

/// Compare the cached tier slug against an MCP entry's `min_tier`.
/// Ranks via `licensing::tier_rank`, so `enterprise`/`admin` slugs
/// (which have no `OrchestratorTier` variant) rank above `mao` instead
/// of falling back to free.
fn tier_meets_requirement(user_tier: &str, required: &OrchestratorTier) -> bool {
    tier_rank(user_tier) >= tier_rank(required.as_slug())
}

/// Per-project ROUTING keys that must NEVER be emitted into the
/// install-root `.claude/settings.json env` from the global MCP tab
/// (F-4, v0.2.73). The orchestrator install root is normally itself a
/// registered project whose env block carries launcher-PROJECTED
/// per-project values (e.g. its real KG collection name). Pre-fix, any
/// MCP-tab action (even toggling an unrelated MCP) rewrote
/// `env.KG_COLLECTION` with the GUI catalog's DEFAULT ("KnowledgeGraph"),
/// the settings watcher's diff-guard saw a real change, SIGHUP'd the
/// live weaviate-kg MCP, and the root project's KG reads/writes silently
/// forked into a phantom collection until the next re-projection healed
/// the file. The canonical writer for these keys is the launcher's
/// per-project projection (`write_project_env_files`), never this legacy
/// path — same rationale as `mcp_registration::ALLOWED_ENV_KEYS` keeping
/// them out of ~/.claude.json.
const PROJECT_ROUTING_ENV_KEYS: &[&str] = &[
    "KG_COLLECTION",
    "DEVELOPMENT_COLLECTION",
    "SHARED_KG_COLLECTION",
    "PROJECT_NAME",
    "CODE_GRAPH_PROJECT",
    "KG_BASE_DIR",
];

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
                    // F-4 (v0.2.73): per-project routing keys are owned by
                    // the launcher's projection, not this legacy path —
                    // skipping them preserves whatever the projection wrote
                    // (see PROJECT_ROUTING_ENV_KEYS docstring).
                    if PROJECT_ROUTING_ENV_KEYS.contains(&key.as_str()) {
                        continue;
                    }
                    env_map.insert(key.clone(), serde_json::Value::String(setting.value.clone()));
                }
            }
        }

        // v0.2.54 Track H: the `VCT_WATERMARK` env emission was removed —
        // no hook, MCP server, or script ever read it. Stale entries in
        // existing settings.json files are harmless leftovers.
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
//   - Keychain-touching tests serialise via the shared
//     `crate::secrets::test_serialize::keychain_serialize_lock`; CI hosts
//     without an OS keychain backend tolerate the soft-fail paths.

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

    /// Set up a temp dir as the launcher's state root + the user's HOME.
    /// Returns a guard that restores prior env on drop and the temp path.
    /// Run the closure under the SERIALIZE mutex.
    struct EnvGuard {
        prev_state: Option<std::ffi::OsString>,
        prev_home: Option<std::ffi::OsString>,
        // v0.2.14 (2026-05-17): `_lock` is now a `KeychainGuard` (was
        // `MutexGuard<'static, ()>`) — the new guard bundles the
        // in-process mutex with a cross-process file lock so concurrent
        // `cargo test --lib` invocations from different terminals
        // serialise on the OS-shared keychain slot.
        _lock: crate::secrets::test_serialize::KeychainGuard,
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

    /// F-2: build a minimal pseudo install root (fake venv-python + MCP
    /// server dirs) under `base` so `default_entry_for_bundled_mcp` can
    /// compose canonical entries. Mirrors the fixture in
    /// `mcp_registration::tests::make_pseudo_install_root` (cfg(test)
    /// items aren't shareable across modules).
    fn make_pseudo_install_root(base: &std::path::Path) -> std::path::PathBuf {
        let root = base.join("pseudo-install");
        let (sub, py) = if cfg!(target_os = "windows") {
            ("Scripts", "python.exe")
        } else {
            ("bin", "python")
        };
        let venv_bin = root.join(".venv").join(sub);
        std::fs::create_dir_all(&venv_bin).unwrap();
        std::fs::write(venv_bin.join(py), b"#!/bin/sh\nexit 0\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let p = venv_bin.join(py);
            let mut perms = std::fs::metadata(&p).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&p, perms).unwrap();
        }
        std::fs::create_dir_all(root.join("claude_mcp_servers/weaviate_mcp")).unwrap();
        std::fs::create_dir_all(root.join("claude_mcp_servers/search_mcp")).unwrap();
        #[cfg(not(target_os = "windows"))]
        std::fs::write(
            root.join("claude_mcp_servers/search_mcp/wrapper.sh"),
            b"#!/usr/bin/env bash\nexit 0\n",
        )
        .unwrap();
        root
    }

    /// Seed a default config whose `install_path` points at a pseudo
    /// install root, so bundled toggle-ON can compose canonical entries.
    fn seed_config_with_install_root(
        state_dir: &std::path::Path,
    ) -> (std::path::PathBuf, std::path::PathBuf) {
        std::fs::create_dir_all(state_dir).unwrap();
        let install_root = make_pseudo_install_root(state_dir);
        let mut config = OrchestratorConfig::default();
        config.install_path = install_root.display().to_string();
        let path = state_dir.join("orchestrator.json");
        std::fs::write(&path, serde_json::to_string_pretty(&config).unwrap()).unwrap();
        (path, install_root)
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

    // ─── #26 (v0.2.31): tier_required_message — tier-blind upsell copy ──
    //
    // These pin the helper that replaced three hardcoded "Upgrade to Pro"
    // strings in `update_orchestrator_setting`. The launcher used to send
    // MAO users to a Pro upsell flow they'd already completed; the helper
    // now resolves the label from the gate's min_tier slug.

    #[test]
    fn tier_required_message_picks_pro_label() {
        let msg = tier_required_message("pro", "RL-scored retrieval");
        assert_eq!(
            msg,
            "RL-scored retrieval requires a Pro or higher tier license."
        );
    }

    #[test]
    fn tier_required_message_picks_mao_label() {
        let msg = tier_required_message("mao", "Multi-agent orchestration");
        assert_eq!(
            msg,
            "Multi-agent orchestration requires a MAO or higher tier license."
        );
        // Critical sanity: no Pro upsell language leaked into a MAO-gated copy.
        assert!(!msg.contains("Pro"), "MAO-gated message must not mention Pro: {msg}");
    }

    /// Regression guard: the bug this helper fixes was MAO users seeing
    /// "Upgrade to Pro" — a tier they already exceed. Ensure no message
    /// produced with min_tier="mao" contains the legacy upsell phrase.
    #[test]
    fn tier_required_message_no_upgrade_to_pro_for_mao_tier() {
        for feature in &[
            "Auto-updates",
            "RL-scored retrieval",
            "Curated agent packs",
            "MAO orchestration",
        ] {
            let msg = tier_required_message("mao", feature);
            assert!(
                !msg.contains("Upgrade to Pro"),
                "MAO-gated copy regressed to legacy 'Upgrade to Pro' for {feature}: {msg}"
            );
        }
    }

    // ─── P0-5 (v0.2.54 Track H): tier-cache-driven feature flags ────────
    //
    // `get_feature_flags` (and the gates in `update_orchestrator_setting`
    // / `toggle_mcp_server`) now resolve the tier from `tier_cache` —
    // the row `license_refresh` writes after a server-side
    // /validate-tier round-trip — instead of the frontend-supplied
    // Supabase `profiles.apps` list, which license-key activation never
    // populated (so Pro customers saw Free gates everywhere).

    #[test]
    fn feature_flags_free_tier_gates_everything() {
        let f = feature_flags_for_tier("free");
        assert_eq!(f.tier, "free");
        assert!(!f.can_auto_update);
        assert!(!f.has_rl_retrieval);
        assert!(!f.has_curated_agents);
        assert!(!f.has_mao);
    }

    #[test]
    fn feature_flags_pro_tier_unlocks_pro_features_not_mao() {
        let f = feature_flags_for_tier("pro");
        assert_eq!(f.tier, "pro");
        assert!(f.can_auto_update);
        assert!(f.has_rl_retrieval);
        assert!(f.has_curated_agents);
        assert!(!f.has_mao, "pro must not unlock MAO features");
    }

    /// The bug this fixes: enterprise/admin slugs have no
    /// `OrchestratorTier` variant; the legacy `from_apps` path could
    /// never produce them, and a rank table without their arms would
    /// gate paying top-tier customers back to Free.
    #[test]
    fn feature_flags_mao_enterprise_admin_are_supersets() {
        for slug in ["mao", "enterprise", "admin"] {
            let f = feature_flags_for_tier(slug);
            assert_eq!(f.tier, slug);
            assert!(f.can_auto_update, "{slug} must unlock pro features");
            assert!(f.has_rl_retrieval, "{slug} must unlock pro features");
            assert!(f.has_mao, "{slug} must unlock MAO features");
        }
    }

    /// Unknown / attacker-supplied tier strings rank as free (no silent
    /// escalation) — mirrors `licensing::tier_rank`'s fallthrough arm.
    #[test]
    fn feature_flags_unknown_tier_ranks_free() {
        let f = feature_flags_for_tier("titanium");
        assert!(!f.can_auto_update);
        assert!(!f.has_mao);
    }

    #[test]
    fn tier_meets_requirement_ranks_slug_against_min_tier() {
        assert!(tier_meets_requirement("free", &OrchestratorTier::Free));
        assert!(!tier_meets_requirement("free", &OrchestratorTier::Pro));
        assert!(tier_meets_requirement("pro", &OrchestratorTier::Pro));
        assert!(!tier_meets_requirement("pro", &OrchestratorTier::Mao));
        // enterprise/admin outrank mao despite having no enum variant.
        assert!(tier_meets_requirement("enterprise", &OrchestratorTier::Mao));
        assert!(tier_meets_requirement("admin", &OrchestratorTier::Mao));
    }

    /// `current_tier_slug` reads the same `tier_cache` row
    /// `license_get_tier` serves, and fails open to "free" on DB error.
    #[test]
    fn current_tier_slug_reads_tier_cache() {
        let db = Db::open_in_memory().expect("in-memory db");
        db.set_tier_cache("pro", &serde_json::json!({}), None).unwrap();
        assert_eq!(current_tier_slug(&db), "pro");
        db.set_tier_cache("admin", &serde_json::json!({}), None).unwrap();
        assert_eq!(current_tier_slug(&db), "admin");
    }

    /// Forward-compat: unknown tier slugs flow through verbatim so a new
    /// OrchestratorTier variant (say "enterprise") works without code
    /// changes here.
    #[test]
    fn tier_required_message_forward_compat_unknown_tier() {
        let msg = tier_required_message("titanium", "Quantum dedupe");
        assert_eq!(
            msg,
            "Quantum dedupe requires a titanium or higher tier license."
        );
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
            toggle_mcp_server_inner("search".to_string(), false, "free")
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
    /// into ~/.claude.json with the CANONICAL builder shape (F-2,
    /// v0.2.73) so Claude Code can actually spawn it.
    ///
    /// (v0.2.5: previously used `code-embed`. v0.2.11: was `ollama`; now
    /// uses `search` after Ollama MCP was removed from the default install.
    /// Exercise the toggle-on path by first flipping `search` off in the
    /// seeded config and then toggling it back on.)
    ///
    /// F-2 regression (was audit finding F-2, green-tested pre-fix): the
    /// old assertions only checked field PRESENCE, so the GUI-catalog
    /// stub (`command: "claude_mcp_servers/search_mcp/server.py"`,
    /// relative, no interpreter) passed. Now we assert the command is an
    /// ABSOLUTE path that EXISTS on disk — the runnable canonical shape.
    #[test]
    fn test_toggle_mcp_server_on_re_registers_canonical_entry_in_claude_json() {
        let (home, _guard) = setup_temp_env();
        let (cfg_path, install_root) = seed_config_with_install_root(&home);

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
            toggle_mcp_server_inner("search".to_string(), true, "free")
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
        assert_eq!(entry["type"], "stdio");
        // The command must be the canonical, RUNNABLE one — absolute and
        // existing on disk (wrapper.sh on Unix, venv python.exe on
        // Windows) — never the catalog's relative display stub.
        let cmd = entry["command"].as_str().expect("command is a string");
        assert!(
            std::path::Path::new(cmd).is_absolute(),
            "canonical command must be absolute, got catalog stub? {}",
            cmd
        );
        assert!(
            std::path::Path::new(cmd).exists(),
            "canonical command must exist on disk: {}",
            cmd
        );
        assert!(
            cmd.starts_with(&install_root.display().to_string()),
            "canonical command must live under the install root: {}",
            cmd
        );
        assert_ne!(
            cmd, "claude_mcp_servers/search_mcp/server.py",
            "must NOT write the GUI catalog's relative stub (F-2)"
        );
        assert!(entry.get("args").is_some(), "missing args field: {}", entry);
        assert!(entry.get("env").is_some(), "missing env field: {}", entry);
    }

    /// F-2 conservative arm: toggling a BUNDLED MCP on when the install
    /// root has no venv-python must FAIL the toggle and write NOTHING —
    /// neither a broken ~/.claude.json entry nor the enabled flag flip.
    #[test]
    fn test_toggle_on_bundled_without_venv_errors_and_writes_nothing() {
        let (home, _guard) = setup_temp_env();
        // Default config: install_path is EMPTY → no venv resolvable.
        let cfg_path = seed_default_config(&home);
        let mut cfg: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&cfg_path).unwrap()).unwrap();
        for entry in cfg["mcp_servers"].as_array_mut().unwrap() {
            if entry["id"] == "search" {
                entry["enabled"] = serde_json::Value::Bool(false);
            }
        }
        std::fs::write(&cfg_path, serde_json::to_string_pretty(&cfg).unwrap()).unwrap();

        let err = rt().block_on(async {
            toggle_mcp_server_inner("search".to_string(), true, "free").await
        });
        let msg = err.expect_err("bundled toggle-on without venv must error");
        assert!(
            msg.contains("no venv-python"),
            "error should explain the missing venv: {}",
            msg
        );

        // Nothing written to ~/.claude.json.
        let cj = read_claude_json(&home);
        assert!(
            cj["mcpServers"].get("search").is_none(),
            "no entry may be written on the failed toggle: {}",
            cj
        );
        // The enabled flip was NOT persisted (error before save_config).
        let cfg_after: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&cfg_path).unwrap()).unwrap();
        let search = cfg_after["mcp_servers"]
            .as_array()
            .unwrap()
            .iter()
            .find(|s| s["id"] == "search")
            .expect("search entry");
        assert_eq!(
            search["enabled"],
            serde_json::Value::Bool(false),
            "failed toggle must not persist the enabled flip"
        );
    }

    /// F-2: user-added CUSTOM MCPs keep their stored entry shape on
    /// toggle-on — the canonical-builder routing applies to bundled ids
    /// only.
    #[test]
    fn test_toggle_on_custom_mcp_keeps_stored_shape() {
        let (home, _guard) = setup_temp_env();
        std::fs::create_dir_all(&home).unwrap();
        let mut config = OrchestratorConfig::default();
        let custom_cmd = if cfg!(target_os = "windows") {
            r"C:\tools\my-mcp.exe"
        } else {
            "/usr/local/bin/my-mcp"
        };
        config.mcp_servers.push(McpServerConfig {
            id: "my-custom".to_string(),
            name: "My Custom".to_string(),
            description: String::new(),
            enabled: false,
            command: custom_cmd.to_string(),
            args: vec!["--flag".to_string()],
            env: HashMap::new(),
            min_tier: OrchestratorTier::Free,
            port: None,
            configurable: false,
            settings: HashMap::new(),
        });
        let path = home.join("orchestrator.json");
        std::fs::write(&path, serde_json::to_string_pretty(&config).unwrap()).unwrap();

        rt().block_on(async {
            toggle_mcp_server_inner("my-custom".to_string(), true, "free")
                .await
                .expect("custom toggle on");
        });

        let cj = read_claude_json(&home);
        let entry = &cj["mcpServers"]["my-custom"];
        assert_eq!(entry["command"], custom_cmd, "custom shape preserved");
        assert_eq!(entry["args"][0], "--flag");
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
    #[ignore = "requires OS keychain backend (keyring); skipped in CI headless env"]
    fn test_update_mcp_setting_secret_routes_to_keychain_not_json() {
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

    /// F-4 (v0.2.73): `apply_mcp_to_claude_settings` must NOT emit
    /// per-project ROUTING keys (KG_COLLECTION / DEVELOPMENT_COLLECTION /
    /// ...) into the install-root settings env — the launcher-projected
    /// values there must survive any MCP-tab action. Pre-fix, toggling an
    /// unrelated MCP rewrote KG_COLLECTION with the catalog DEFAULT
    /// ("KnowledgeGraph"), silently forking the root project's KG.
    #[test]
    fn test_apply_mcp_to_claude_settings_skips_project_routing_keys() {
        let (home, _guard) = setup_temp_env();

        let install_dir = home.join("orch-install");
        let claude_dir = install_dir.join(".claude");
        std::fs::create_dir_all(&claude_dir).unwrap();
        let settings_path = claude_dir.join("settings.json");
        // The launcher projection already wrote the REAL per-project value.
        std::fs::write(
            &settings_path,
            r#"{"env": {"KG_COLLECTION": "ProjectedRoot_KnowledgeGraph", "DEVELOPMENT_COLLECTION": "ProjectedRoot_Development"}}"#,
        )
        .unwrap();

        let mut config = OrchestratorConfig::default();
        config.install_path = install_dir.display().to_string();
        // The DEFAULT catalog already carries the hazardous settings
        // (weaviate-kg ships KG_COLLECTION="KnowledgeGraph" +
        // DEVELOPMENT_COLLECTION="Development", enabled=true) — use it
        // as-is so the test exercises the exact shipped shape. Add one
        // legit non-routing setting to prove those still flow.
        if let Some(server) = config.mcp_servers.iter_mut().find(|s| s.id == "search") {
            server.settings.insert(
                "OPENALEX_EMAIL".to_string(),
                McpSetting {
                    label: "OpenAlex Email".to_string(),
                    value: "user@example.com".to_string(),
                    setting_type: McpSettingType::Text,
                    description: String::new(),
                    editable: true,
                },
            );
        }

        rt().block_on(async {
            apply_mcp_to_claude_settings(&config).await.unwrap();
        });

        let raw = std::fs::read_to_string(&settings_path).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&raw).unwrap();
        let env = &parsed["env"];
        // Projected routing values PRESERVED — not clobbered with the
        // catalog defaults.
        assert_eq!(
            env["KG_COLLECTION"], "ProjectedRoot_KnowledgeGraph",
            "MCP-tab action must not clobber the projected KG_COLLECTION: {}",
            env
        );
        assert_eq!(
            env["DEVELOPMENT_COLLECTION"], "ProjectedRoot_Development",
            "MCP-tab action must not clobber the projected DEVELOPMENT_COLLECTION: {}",
            env
        );
        assert!(
            !raw.contains("\"KnowledgeGraph\""),
            "catalog default value must not appear anywhere in settings.json: {}",
            raw
        );
        // Non-routing settings still flow through.
        assert_eq!(env["OPENALEX_EMAIL"], "user@example.com");
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
    #[ignore = "requires OS keychain backend (keyring); skipped in CI headless env"]
    fn test_first_run_migrates_plaintext_secrets_to_keychain() {
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
