//! SQLite database for the VCT Hub.
//!
//! Tables:
//!   - app_registry: registered apps/services and their capabilities
//!   - messages: async message bus between apps
//!   - data_catalog: what data each app exposes (queryable by others)
//!   - kv_store: arbitrary key-value config per app

use rusqlite::{Connection, Result as SqlResult, params};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

pub type Db = Arc<Mutex<Connection>>;

/// Database path: ~/.vct/hub.db
pub fn db_path() -> PathBuf {
    directories::UserDirs::new()
        .map(|d| d.home_dir().join(".vct").join("hub.db"))
        .unwrap_or_else(|| PathBuf::from(".vct/hub.db"))
}

/// Open (or create) the hub database and run migrations.
pub fn open_db() -> SqlResult<Db> {
    let path = db_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).ok();
    }

    let conn = Connection::open(&path)?;
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")?;
    migrate(&conn)?;
    Ok(Arc::new(Mutex::new(conn)))
}

fn migrate(conn: &Connection) -> SqlResult<()> {
    conn.execute_batch(
        "
        -- App registry: each app registers itself on startup
        CREATE TABLE IF NOT EXISTS app_registry (
            app_id      TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            version     TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'stopped',  -- running, stopped, error
            api_url     TEXT,                              -- http://localhost:PORT if app has API
            pid         INTEGER,
            capabilities TEXT NOT NULL DEFAULT '[]',       -- JSON array of capability strings
            metadata    TEXT NOT NULL DEFAULT '{}',        -- JSON object for arbitrary data
            registered_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_heartbeat TEXT
        );

        -- Async message bus
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sender      TEXT NOT NULL,              -- app_id of sender
            recipient   TEXT NOT NULL,              -- app_id or '*' for broadcast
            msg_type    TEXT NOT NULL DEFAULT 'data', -- data, request, response, event
            topic       TEXT NOT NULL DEFAULT '',    -- routing key (e.g. 'transcription', 'kg_query')
            payload     TEXT NOT NULL DEFAULT '{}',  -- JSON body
            reply_to    INTEGER REFERENCES messages(id), -- for request/response pairing
            status      TEXT NOT NULL DEFAULT 'pending', -- pending, delivered, read, expired
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at  TEXT                         -- NULL = no expiry
        );
        CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient, status);
        CREATE INDEX IF NOT EXISTS idx_messages_topic ON messages(topic, status);

        -- Data catalog: apps declare what data they can provide
        CREATE TABLE IF NOT EXISTS data_catalog (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id      TEXT NOT NULL REFERENCES app_registry(app_id),
            data_type   TEXT NOT NULL,              -- 'transcription', 'kg_node', 'code_entity', etc.
            description TEXT NOT NULL DEFAULT '',
            schema      TEXT NOT NULL DEFAULT '{}', -- JSON schema of the data
            access_url  TEXT,                        -- direct URL to query this data (if HTTP)
            mcp_tool    TEXT,                        -- MCP tool name (if via MCP)
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_data_catalog_type ON data_catalog(data_type);

        -- Per-app key-value config store
        CREATE TABLE IF NOT EXISTS kv_store (
            app_id      TEXT NOT NULL,
            key         TEXT NOT NULL,
            value       TEXT NOT NULL DEFAULT '',
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (app_id, key)
        );
        "
    )?;
    Ok(())
}

// ---------------------------------------------------------------------------
// App registry operations
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct AppRegistration {
    pub app_id: String,
    pub name: String,
    pub version: String,
    pub api_url: Option<String>,
    pub pid: Option<u32>,
    pub capabilities: Vec<String>,
    pub metadata: serde_json::Value,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct RegisteredApp {
    pub app_id: String,
    pub name: String,
    pub version: String,
    pub status: String,
    pub api_url: Option<String>,
    pub pid: Option<u32>,
    pub capabilities: Vec<String>,
    pub metadata: serde_json::Value,
    pub registered_at: String,
    pub last_heartbeat: Option<String>,
}

pub fn register_app(db: &Db, reg: &AppRegistration) -> SqlResult<()> {
    let conn = db.lock().unwrap();
    let caps_json = serde_json::to_string(&reg.capabilities).unwrap_or_default();
    let meta_json = serde_json::to_string(&reg.metadata).unwrap_or_default();

    conn.execute(
        "INSERT INTO app_registry (app_id, name, version, status, api_url, pid, capabilities, metadata, last_heartbeat)
         VALUES (?1, ?2, ?3, 'running', ?4, ?5, ?6, ?7, datetime('now'))
         ON CONFLICT(app_id) DO UPDATE SET
           name = excluded.name,
           version = excluded.version,
           status = 'running',
           api_url = excluded.api_url,
           pid = excluded.pid,
           capabilities = excluded.capabilities,
           metadata = excluded.metadata,
           last_heartbeat = datetime('now')",
        params![reg.app_id, reg.name, reg.version, reg.api_url, reg.pid, caps_json, meta_json],
    )?;
    Ok(())
}

pub fn deregister_app(db: &Db, app_id: &str) -> SqlResult<()> {
    let conn = db.lock().unwrap();
    conn.execute(
        "UPDATE app_registry SET status = 'stopped', pid = NULL WHERE app_id = ?1",
        params![app_id],
    )?;
    Ok(())
}

pub fn heartbeat_app(db: &Db, app_id: &str) -> SqlResult<()> {
    let conn = db.lock().unwrap();
    conn.execute(
        "UPDATE app_registry SET last_heartbeat = datetime('now') WHERE app_id = ?1",
        params![app_id],
    )?;
    Ok(())
}

pub fn list_apps(db: &Db) -> SqlResult<Vec<RegisteredApp>> {
    let conn = db.lock().unwrap();
    let mut stmt = conn.prepare(
        "SELECT app_id, name, version, status, api_url, pid, capabilities, metadata, registered_at, last_heartbeat
         FROM app_registry ORDER BY name"
    )?;

    let rows = stmt.query_map([], |row| {
        let caps_str: String = row.get(6)?;
        let meta_str: String = row.get(7)?;
        Ok(RegisteredApp {
            app_id: row.get(0)?,
            name: row.get(1)?,
            version: row.get(2)?,
            status: row.get(3)?,
            api_url: row.get(4)?,
            pid: row.get(5)?,
            capabilities: serde_json::from_str(&caps_str).unwrap_or_default(),
            metadata: serde_json::from_str(&meta_str).unwrap_or_default(),
            registered_at: row.get(8)?,
            last_heartbeat: row.get(9)?,
        })
    })?.collect::<SqlResult<Vec<_>>>()?;

    Ok(rows)
}

// ---------------------------------------------------------------------------
// Message bus operations
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct Message {
    pub id: Option<i64>,
    pub sender: String,
    pub recipient: String,
    pub msg_type: String,
    pub topic: String,
    pub payload: serde_json::Value,
    pub reply_to: Option<i64>,
    pub status: String,
    pub created_at: String,
    pub expires_at: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SendMessage {
    pub sender: String,
    pub recipient: String,
    pub msg_type: Option<String>,
    pub topic: String,
    pub payload: serde_json::Value,
    pub reply_to: Option<i64>,
    pub expires_at: Option<String>,
}

pub fn send_message(db: &Db, msg: &SendMessage) -> SqlResult<i64> {
    let conn = db.lock().unwrap();
    let msg_type = msg.msg_type.as_deref().unwrap_or("data");

    conn.execute(
        "INSERT INTO messages (sender, recipient, msg_type, topic, payload, reply_to, expires_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            msg.sender,
            msg.recipient,
            msg_type,
            msg.topic,
            serde_json::to_string(&msg.payload).unwrap_or_default(),
            msg.reply_to,
            msg.expires_at,
        ],
    )?;

    Ok(conn.last_insert_rowid())
}

pub fn poll_messages(db: &Db, recipient: &str, topic: Option<&str>, limit: u32) -> SqlResult<Vec<Message>> {
    let conn = db.lock().unwrap();

    let query = if let Some(t) = topic {
        format!(
            "SELECT id, sender, recipient, msg_type, topic, payload, reply_to, status, created_at, expires_at
             FROM messages
             WHERE (recipient = ?1 OR recipient = '*')
               AND status = 'pending'
               AND topic = '{}'
               AND (expires_at IS NULL OR expires_at > datetime('now'))
             ORDER BY id ASC LIMIT ?2",
            t
        )
    } else {
        "SELECT id, sender, recipient, msg_type, topic, payload, reply_to, status, created_at, expires_at
         FROM messages
         WHERE (recipient = ?1 OR recipient = '*')
           AND status = 'pending'
           AND (expires_at IS NULL OR expires_at > datetime('now'))
         ORDER BY id ASC LIMIT ?2".to_string()
    };

    let mut stmt = conn.prepare(&query)?;
    let rows = stmt.query_map(params![recipient, limit], |row| {
        let payload_str: String = row.get(5)?;
        Ok(Message {
            id: row.get(0)?,
            sender: row.get(1)?,
            recipient: row.get(2)?,
            msg_type: row.get(3)?,
            topic: row.get(4)?,
            payload: serde_json::from_str(&payload_str).unwrap_or_default(),
            reply_to: row.get(6)?,
            status: row.get(7)?,
            created_at: row.get(8)?,
            expires_at: row.get(9)?,
        })
    })?.collect::<SqlResult<Vec<_>>>()?;

    // Mark as delivered
    let ids: Vec<i64> = rows.iter().filter_map(|m| m.id).collect();
    if !ids.is_empty() {
        let placeholders: Vec<String> = ids.iter().map(|id| id.to_string()).collect();
        conn.execute(
            &format!(
                "UPDATE messages SET status = 'delivered' WHERE id IN ({})",
                placeholders.join(",")
            ),
            [],
        )?;
    }

    Ok(rows)
}

pub fn ack_message(db: &Db, message_id: i64) -> SqlResult<()> {
    let conn = db.lock().unwrap();
    conn.execute(
        "UPDATE messages SET status = 'read' WHERE id = ?1",
        params![message_id],
    )?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Data catalog operations
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct DataEntry {
    pub id: Option<i64>,
    pub app_id: String,
    pub data_type: String,
    pub description: String,
    pub schema: serde_json::Value,
    pub access_url: Option<String>,
    pub mcp_tool: Option<String>,
}

pub fn register_data(db: &Db, entry: &DataEntry) -> SqlResult<i64> {
    let conn = db.lock().unwrap();
    let schema_json = serde_json::to_string(&entry.schema).unwrap_or_default();

    conn.execute(
        "INSERT INTO data_catalog (app_id, data_type, description, schema, access_url, mcp_tool)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![entry.app_id, entry.data_type, entry.description, schema_json, entry.access_url, entry.mcp_tool],
    )?;

    Ok(conn.last_insert_rowid())
}

pub fn query_data_catalog(db: &Db, data_type: Option<&str>, app_id: Option<&str>) -> SqlResult<Vec<DataEntry>> {
    let conn = db.lock().unwrap();

    let (query, p1, p2) = match (data_type, app_id) {
        (Some(dt), Some(aid)) => (
            "SELECT id, app_id, data_type, description, schema, access_url, mcp_tool FROM data_catalog WHERE data_type = ?1 AND app_id = ?2",
            dt.to_string(), aid.to_string(),
        ),
        (Some(dt), None) => (
            "SELECT id, app_id, data_type, description, schema, access_url, mcp_tool FROM data_catalog WHERE data_type = ?1 AND ?2 = ?2",
            dt.to_string(), "1".to_string(),
        ),
        (None, Some(aid)) => (
            "SELECT id, app_id, data_type, description, schema, access_url, mcp_tool FROM data_catalog WHERE ?1 = ?1 AND app_id = ?2",
            "1".to_string(), aid.to_string(),
        ),
        (None, None) => (
            "SELECT id, app_id, data_type, description, schema, access_url, mcp_tool FROM data_catalog WHERE ?1 = ?1 AND ?2 = ?2",
            "1".to_string(), "1".to_string(),
        ),
    };

    let mut stmt = conn.prepare(query)?;
    let rows = stmt.query_map(params![p1, p2], |row| {
        let schema_str: String = row.get(4)?;
        Ok(DataEntry {
            id: row.get(0)?,
            app_id: row.get(1)?,
            data_type: row.get(2)?,
            description: row.get(3)?,
            schema: serde_json::from_str(&schema_str).unwrap_or_default(),
            access_url: row.get(5)?,
            mcp_tool: row.get(6)?,
        })
    })?.collect::<SqlResult<Vec<_>>>()?;

    Ok(rows)
}
