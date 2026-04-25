//! Telemetry consent + dashboard commands.
//!
//! The actual collector lives Python-side in the orchestrator
//! (`commercial_workflow/telemetry/`). The launcher owns the consent UI
//! surface and the dashboard view. Consent flags are stored in
//! `~/.vibecoded/config.json` so Python and Rust both read the same file.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use tauri::{command, State};

use crate::db::Db;

fn consent_path() -> PathBuf {
    directories::UserDirs::new()
        .map(|d| d.home_dir().join(".vibecoded").join("config.json"))
        .unwrap_or_else(|| PathBuf::from(".vibecoded/config.json"))
}

fn queue_path() -> PathBuf {
    directories::UserDirs::new()
        .map(|d| d.home_dir().join(".vibecoded").join("telemetry.db"))
        .unwrap_or_else(|| PathBuf::from(".vibecoded/telemetry.db"))
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConsentFlags {
    #[serde(default = "default_version")]
    pub consent_version: String,
    #[serde(default)]
    pub granted_at: Option<String>,
    #[serde(default = "default_true")]
    pub always_on: bool,
    #[serde(default)]
    pub rl_data: bool,
    #[serde(default)]
    pub routing_data: bool,
    #[serde(default)]
    pub instinct_data: bool,
    #[serde(default)]
    pub hardware: bool,
}
fn default_version() -> String {
    "1.0".into()
}
fn default_true() -> bool {
    true
}

impl Default for ConsentFlags {
    fn default() -> Self {
        Self {
            consent_version: default_version(),
            granted_at: None,
            always_on: true,
            rl_data: false,
            routing_data: false,
            instinct_data: false,
            hardware: false,
        }
    }
}

#[derive(Debug, Serialize)]
pub struct TelemetryStatus {
    pub consent: ConsentFlags,
    pub queue_size: u32,
    pub last_upload_at: Option<i64>,
    pub last_upload_error: Option<String>,
    pub disabled_via_env: bool,
}

#[command]
pub async fn telemetry_status() -> Result<TelemetryStatus, String> {
    let consent = read_consent().unwrap_or_default();
    let disabled_via_env = std::env::var("VIBECODED_TELEMETRY")
        .map(|v| matches!(v.to_lowercase().as_str(), "false" | "0" | "no" | "off"))
        .unwrap_or(false);

    // Queue size: open the SQLite queue readonly if it exists.
    let queue_size = queue_row_count().unwrap_or(0);
    // last_upload_at / error: also read from the queue DB if present.
    let (last_upload_at, last_upload_error) = queue_last_upload().unwrap_or((None, None));

    Ok(TelemetryStatus {
        consent,
        queue_size,
        last_upload_at,
        last_upload_error,
        disabled_via_env,
    })
}

#[command]
pub async fn telemetry_set_consent(
    flags: ConsentFlags,
    db: State<'_, Db>,
) -> Result<ConsentFlags, String> {
    let mut flags = flags;
    flags.consent_version = default_version();
    if flags.granted_at.is_none() {
        flags.granted_at = Some(chrono::Utc::now().to_rfc3339());
    }
    // always_on is locked on — it's license validation + error rates, not optional.
    flags.always_on = true;

    write_consent(&flags)?;
    db.audit(
        "telemetry_consent_update",
        None,
        None,
        &serde_json::json!({
            "rl_data": flags.rl_data,
            "routing_data": flags.routing_data,
            "instinct_data": flags.instinct_data,
            "hardware": flags.hardware,
        }),
    )?;
    Ok(flags)
}

#[derive(Debug, Serialize)]
pub struct TelemetryEventView {
    pub id: i64,
    pub event_type: String,
    pub created_at: f64,
    pub uploaded_at: Option<f64>,
    pub payload_summary: String, // short, scrubbed preview
}

#[command]
pub async fn telemetry_recent_events(limit: u32) -> Result<Vec<TelemetryEventView>, String> {
    let limit = limit.min(100);
    let path = queue_path();
    if !path.exists() {
        return Ok(vec![]);
    }
    let conn = rusqlite::Connection::open_with_flags(
        &path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .map_err(|e| format!("open telemetry.db: {}", e))?;
    let mut stmt = conn
        .prepare(
            "SELECT id, event_type, payload_json, created_at, uploaded_at
             FROM events ORDER BY id DESC LIMIT ?1",
        )
        .map_err(|e| format!("prepare: {}", e))?;
    let rows = stmt
        .query_map(rusqlite::params![limit as i64], |row| {
            let payload: String = row.get(2)?;
            Ok(TelemetryEventView {
                id: row.get(0)?,
                event_type: row.get(1)?,
                created_at: row.get(3)?,
                uploaded_at: row.get(4)?,
                payload_summary: payload.chars().take(200).collect(),
            })
        })
        .map_err(|e| format!("query: {}", e))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|e| format!("collect: {}", e))
}

#[command]
pub async fn telemetry_clear_queue(db: State<'_, Db>) -> Result<(), String> {
    let path = queue_path();
    if !path.exists() {
        return Ok(());
    }
    let conn = rusqlite::Connection::open(&path).map_err(|e| format!("open: {}", e))?;
    conn.execute("DELETE FROM events", [])
        .map_err(|e| format!("clear: {}", e))?;
    db.audit("telemetry_clear_queue", None, None, &serde_json::json!({}))?;
    Ok(())
}

// ─── Internals ──────────────────────────────────────────────────────────

fn read_consent() -> Result<ConsentFlags, String> {
    let path = consent_path();
    if !path.exists() {
        return Ok(ConsentFlags::default());
    }
    let raw = std::fs::read_to_string(&path).map_err(|e| format!("read: {}", e))?;
    serde_json::from_str(&raw).map_err(|e| format!("parse: {}", e))
}

fn write_consent(flags: &ConsentFlags) -> Result<(), String> {
    let path = consent_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("mkdir: {}", e))?;
    }
    let body = serde_json::to_string_pretty(flags).map_err(|e| format!("serialize: {}", e))?;
    std::fs::write(&path, body).map_err(|e| format!("write: {}", e))
}

fn queue_row_count() -> Result<u32, String> {
    let path = queue_path();
    if !path.exists() {
        return Ok(0);
    }
    let conn = rusqlite::Connection::open_with_flags(
        &path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .map_err(|e| format!("open: {}", e))?;
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
        .unwrap_or(0);
    Ok(count as u32)
}

fn queue_last_upload() -> Result<(Option<i64>, Option<String>), String> {
    let path = queue_path();
    if !path.exists() {
        return Ok((None, None));
    }
    let conn = rusqlite::Connection::open_with_flags(
        &path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .map_err(|e| format!("open: {}", e))?;
    let ts: Option<f64> = conn
        .query_row(
            "SELECT MAX(uploaded_at) FROM events WHERE uploaded_at IS NOT NULL",
            [],
            |r| r.get(0),
        )
        .ok()
        .flatten();
    Ok((ts.map(|f| (f * 1000.0) as i64), None))
}
