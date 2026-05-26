//! Coordination tab backend.
//!
//! Manages the per-project configuration of the `vct-coordination` module:
//! Supabase URL + service key (in keychain), team username, channels,
//! optional Telegram bridge. Provides a test_connection command that
//! probes Supabase directly from the launcher and a team_status proxy that
//! reads the coordination tables for the live activity panel.

use serde::{Deserialize, Serialize};
use tauri::{command, State};

use crate::db::Db;
use crate::secrets::{self, SecretScope};
use vct_launcher_core::process::CommandExt as _;

const MODULE_ID: &str = "vct-coordination";

// ─── Config view + update ────────────────────────────────────────────────

#[derive(Debug, Serialize)]
pub struct CoordinationConfig {
    pub project_id: String,
    pub installed: bool,
    pub enabled: bool,
    pub supabase_url: Option<String>,      // URL is not a secret — we return it
    pub supabase_key_set: bool,            // key presence only, never value
    pub telegram_bot_token_set: bool,
    pub username: Option<String>,
    pub user_aliases: Vec<String>,
    pub channels_enabled: Vec<String>,
    pub telegram_group_id: Option<String>,
}

#[command]
pub async fn coordination_get_config(
    project_id: String,
    db: State<'_, Db>,
) -> Result<CoordinationConfig, String> {
    let scope = SecretScope::PerProject { project_id: &project_id };
    let install_row = db.get_module_install(&project_id, MODULE_ID)?;

    // Settings
    let username = db
        .get_setting(&project_id, MODULE_ID, "VCT_USERNAME")?
        .and_then(|v| v.as_str().map(str::to_string));
    let aliases_raw = db
        .get_setting(&project_id, MODULE_ID, "VCT_USER_ALIASES")?
        .and_then(|v| v.as_str().map(str::to_string))
        .unwrap_or_default();
    let user_aliases: Vec<String> = aliases_raw
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect();
    let channels_enabled = db
        .get_setting(&project_id, MODULE_ID, "CHANNEL_WATCH")?
        .and_then(|v| v.as_array().cloned())
        .map(|a| {
            a.iter()
                .filter_map(|x| x.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_else(|| vec!["messages".to_string()]);
    let telegram_group_id = db
        .get_setting(&project_id, MODULE_ID, "TELEGRAM_GROUP_CHAT_ID")?
        .and_then(|v| v.as_str().map(str::to_string));

    // Secrets (presence only for key + telegram token; URL is returned)
    let supabase_url = secrets::get(scope, MODULE_ID, "SUPABASE_URL")?;
    let supabase_key_set = secrets::is_set(scope, MODULE_ID, "SUPABASE_KEY")?;
    let telegram_bot_token_set = secrets::is_set(scope, MODULE_ID, "TELEGRAM_BOT_TOKEN")?;

    Ok(CoordinationConfig {
        project_id,
        installed: install_row.is_some(),
        enabled: install_row.map(|r| r.enabled).unwrap_or(false),
        supabase_url,
        supabase_key_set,
        telegram_bot_token_set,
        username,
        user_aliases,
        channels_enabled,
        telegram_group_id,
    })
}

#[derive(Debug, Deserialize, Default)]
pub struct CoordinationConfigUpdate {
    pub supabase_url: Option<String>,
    pub supabase_key: Option<String>,
    pub telegram_bot_token: Option<String>,
    pub username: Option<String>,
    pub user_aliases: Option<Vec<String>>,
    pub channels_enabled: Option<Vec<String>>,
    pub telegram_group_id: Option<String>,
}

#[command]
pub async fn coordination_set_config(
    project_id: String,
    update: CoordinationConfigUpdate,
    db: State<'_, Db>,
) -> Result<CoordinationConfig, String> {
    let scope = SecretScope::PerProject { project_id: &project_id };

    if let Some(url) = update.supabase_url.as_deref() {
        let re = regex::Regex::new(r"^https://[a-z0-9-]+\.supabase\.co$")
            .map_err(|e| format!("regex: {}", e))?;
        if !re.is_match(url) {
            return Err("supabase_url must match https://<ref>.supabase.co".into());
        }
        secrets::set(scope, MODULE_ID, "SUPABASE_URL", url)?;
    }
    if let Some(key) = update.supabase_key.as_deref() {
        // service_role keys are either sb_secret_* (new) or eyJ... JWTs (legacy)
        let ok = key.starts_with("sb_secret_") || key.starts_with("eyJ");
        if !ok || key.len() < 20 {
            return Err("supabase_key looks malformed (expected sb_secret_* or eyJ... with len>=20)".into());
        }
        secrets::set(scope, MODULE_ID, "SUPABASE_KEY", key)?;
    }
    if let Some(tok) = update.telegram_bot_token.as_deref() {
        let re = regex::Regex::new(r"^[0-9]+:[A-Za-z0-9_-]{30,}$")
            .map_err(|e| format!("regex: {}", e))?;
        if !re.is_match(tok) {
            return Err("telegram_bot_token format invalid".into());
        }
        secrets::set(scope, MODULE_ID, "TELEGRAM_BOT_TOKEN", tok)?;
    }

    if let Some(u) = update.username.as_deref() {
        let re = regex::Regex::new(r"^[a-z0-9_-]+$").unwrap();
        if !re.is_match(u) {
            return Err("username must be lowercase alphanumeric + hyphen/underscore".into());
        }
        db.set_setting(
            &project_id,
            MODULE_ID,
            "VCT_USERNAME",
            &serde_json::Value::String(u.to_string()),
        )?;
    }
    if let Some(aliases) = update.user_aliases.as_ref() {
        let joined = aliases.join(",");
        db.set_setting(
            &project_id,
            MODULE_ID,
            "VCT_USER_ALIASES",
            &serde_json::Value::String(joined),
        )?;
    }
    if let Some(ch) = update.channels_enabled.as_ref() {
        let allowed = ["messages", "decisions", "work_items", "activity", "bugs", "telegram"];
        for c in ch {
            if !allowed.contains(&c.as_str()) {
                return Err(format!("unknown channel: {}", c));
            }
        }
        let arr = serde_json::Value::Array(
            ch.iter().map(|s| serde_json::Value::String(s.clone())).collect(),
        );
        db.set_setting(&project_id, MODULE_ID, "CHANNEL_WATCH", &arr)?;
    }
    if let Some(gid) = update.telegram_group_id.as_deref() {
        db.set_setting(
            &project_id,
            MODULE_ID,
            "TELEGRAM_GROUP_CHAT_ID",
            &serde_json::Value::String(gid.to_string()),
        )?;
    }

    db.audit(
        "coordination_config_update",
        Some(&project_id),
        Some(MODULE_ID),
        &serde_json::json!({
            "supabase_url_changed": update.supabase_url.is_some(),
            "supabase_key_changed": update.supabase_key.is_some(),
            "telegram_token_changed": update.telegram_bot_token.is_some(),
            "username_changed": update.username.is_some(),
            "aliases_changed": update.user_aliases.is_some(),
            "channels_changed": update.channels_enabled.is_some(),
            "telegram_group_changed": update.telegram_group_id.is_some(),
            // Never log the actual values.
        }),
    )?;

    coordination_get_config(project_id, db).await
}

// ─── Connection testing ─────────────────────────────────────────────────

#[derive(Debug, Serialize)]
pub struct ConnectionTestResult {
    pub reachable: bool,
    pub latency_ms: Option<u32>,
    pub auth_ok: bool,
    pub schema_applied: bool,
    pub error: Option<String>,
}

#[command]
pub async fn coordination_test_connection(
    project_id: String,
) -> Result<ConnectionTestResult, String> {
    let scope = SecretScope::PerProject { project_id: &project_id };
    let url = secrets::get(scope, MODULE_ID, "SUPABASE_URL")?
        .ok_or("SUPABASE_URL not set")?;
    let key = secrets::get(scope, MODULE_ID, "SUPABASE_KEY")?
        .ok_or("SUPABASE_KEY not set")?;

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(8))
        .build()
        .map_err(|e| format!("http client: {}", e))?;
    let start = std::time::Instant::now();

    // 1. Reachability + auth: query team_members (small table, exists in v1 schema)
    let resp = client
        .get(format!("{}/rest/v1/team_members?select=username&limit=1", url))
        .header("apikey", &key)
        .header("Authorization", format!("Bearer {}", key))
        .send()
        .await;

    let (reachable, latency, status_code) = match resp {
        Ok(r) => {
            let latency = start.elapsed().as_millis() as u32;
            let code = r.status().as_u16();
            (true, Some(latency), code)
        }
        Err(e) => {
            return Ok(ConnectionTestResult {
                reachable: false,
                latency_ms: None,
                auth_ok: false,
                schema_applied: false,
                error: Some(format!("unreachable: {}", e)),
            });
        }
    };

    let auth_ok = matches!(status_code, 200 | 206 | 404); // 404 = table absent (schema not applied), still auth'd
    let schema_applied = status_code == 200 || status_code == 206;

    let error = if !auth_ok {
        Some(format!("HTTP {}", status_code))
    } else if !schema_applied {
        Some(format!("auth OK but schema not applied (got {})", status_code))
    } else {
        None
    };

    Ok(ConnectionTestResult {
        reachable,
        latency_ms: latency,
        auth_ok,
        schema_applied,
        error,
    })
}

#[command]
pub async fn coordination_apply_schema(
    project_id: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    // Runs the coordination module's setup.py --non-interactive with the
    // project's Supabase env injected. The setup script itself knows how
    // to apply schema.sql / schema_v2.sql.
    let install = db
        .get_module_install(&project_id, MODULE_ID)?
        .ok_or("coordination module not installed for this project")?;
    let scope = SecretScope::PerProject { project_id: &project_id };
    let url = secrets::get(scope, MODULE_ID, "SUPABASE_URL")?
        .ok_or("SUPABASE_URL not set")?;
    let key = secrets::get(scope, MODULE_ID, "SUPABASE_KEY")?
        .ok_or("SUPABASE_KEY not set")?;

    let python = if cfg!(target_os = "windows") {
        format!("{}\\.venv\\Scripts\\python.exe", install.install_path)
    } else {
        format!("{}/.venv/bin/python", install.install_path)
    };

    let output = tokio::process::Command::new(&python).silent()
        .args(["setup.py", "--non-interactive"])
        .current_dir(&install.install_path)
        .env_clear()
        .env("SUPABASE_URL", url)
        .env("SUPABASE_KEY", key)
        .env("PATH", std::env::var("PATH").unwrap_or_default())
        .env("HOME", std::env::var("HOME").unwrap_or_default())
        .output()
        .await
        .map_err(|e| format!("spawn setup.py: {}", e))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "setup.py exit {}: {}",
            output.status.code().unwrap_or(-1),
            stderr.chars().take(500).collect::<String>()
        ));
    }
    db.audit(
        "coordination_apply_schema",
        Some(&project_id),
        Some(MODULE_ID),
        &serde_json::json!({}),
    )?;
    Ok(())
}

// ─── Live activity / team status ─────────────────────────────────────────

#[derive(Debug, Serialize)]
pub struct TeamStatus {
    pub members: Vec<TeamMember>,
    pub presence: Vec<PresenceEntry>,
    pub recent_messages_count: u32,
    pub online_now: u32,
    pub connection_ok: bool,
}

#[derive(Debug, Serialize)]
pub struct TeamMember {
    pub username: String,
    pub display_name: String,
    pub role: String,
}

#[derive(Debug, Serialize)]
pub struct PresenceEntry {
    pub username: String,
    pub source: String,
    pub status: String,
    pub last_seen: String,
}

#[command]
pub async fn coordination_team_status(project_id: String) -> Result<TeamStatus, String> {
    let scope = SecretScope::PerProject { project_id: &project_id };
    let url = secrets::get(scope, MODULE_ID, "SUPABASE_URL")?.ok_or("SUPABASE_URL not set")?;
    let key = secrets::get(scope, MODULE_ID, "SUPABASE_KEY")?.ok_or("SUPABASE_KEY not set")?;

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(8))
        .build()
        .map_err(|e| format!("http client: {}", e))?;

    let mut connection_ok = true;

    // Members
    let members_resp = client
        .get(format!(
            "{}/rest/v1/team_members?select=username,display_name,role&is_active=eq.true",
            url
        ))
        .header("apikey", &key)
        .header("Authorization", format!("Bearer {}", key))
        .send()
        .await;
    let members: Vec<TeamMember> = match members_resp {
        Ok(r) if r.status().is_success() => {
            let items: Vec<serde_json::Value> = r.json().await.unwrap_or_default();
            items
                .into_iter()
                .map(|v| TeamMember {
                    username: v
                        .get("username")
                        .and_then(|x| x.as_str())
                        .unwrap_or("")
                        .to_string(),
                    display_name: v
                        .get("display_name")
                        .and_then(|x| x.as_str())
                        .unwrap_or("")
                        .to_string(),
                    role: v
                        .get("role")
                        .and_then(|x| x.as_str())
                        .unwrap_or("contributor")
                        .to_string(),
                })
                .collect()
        }
        _ => {
            connection_ok = false;
            vec![]
        }
    };

    // Presence
    let presence_resp = client
        .get(format!(
            "{}/rest/v1/team_presence?order=last_seen.desc&limit=20",
            url
        ))
        .header("apikey", &key)
        .header("Authorization", format!("Bearer {}", key))
        .send()
        .await;
    let presence: Vec<PresenceEntry> = match presence_resp {
        Ok(r) if r.status().is_success() => {
            let items: Vec<serde_json::Value> = r.json().await.unwrap_or_default();
            items
                .into_iter()
                .map(|v| PresenceEntry {
                    username: v
                        .get("username")
                        .and_then(|x| x.as_str())
                        .unwrap_or("")
                        .to_string(),
                    source: v
                        .get("source")
                        .and_then(|x| x.as_str())
                        .unwrap_or("")
                        .to_string(),
                    status: v
                        .get("status")
                        .and_then(|x| x.as_str())
                        .unwrap_or("offline")
                        .to_string(),
                    last_seen: v
                        .get("last_seen")
                        .and_then(|x| x.as_str())
                        .unwrap_or("")
                        .to_string(),
                })
                .collect()
        }
        _ => vec![],
    };

    let online_now = presence.iter().filter(|p| p.status == "online").count() as u32;

    // Recent messages count (last 24h)
    let since = (chrono::Utc::now() - chrono::Duration::hours(24)).to_rfc3339();
    let msg_resp = client
        .get(format!(
            "{}/rest/v1/messages?select=id&created_at=gte.{}",
            url, since
        ))
        .header("apikey", &key)
        .header("Authorization", format!("Bearer {}", key))
        .header("Prefer", "count=exact")
        .send()
        .await;
    let recent_messages_count = match msg_resp {
        Ok(r) => r
            .headers()
            .get("content-range")
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.split('/').nth(1))
            .and_then(|n| n.parse::<u32>().ok())
            .unwrap_or(0),
        _ => 0,
    };

    Ok(TeamStatus {
        members,
        presence,
        recent_messages_count,
        online_now,
        connection_ok,
    })
}
