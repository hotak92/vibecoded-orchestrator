//! Row access for the `service_endpoints` table (migration 047, v0.2.97).
//!
//! One row per core service (`weaviate`, `ollama`, `code_embed`): where it is
//! reached and who owns the container behind it. This module only READS.
//! The table's one writer is `vco_lib.service_endpoints` (Python), which
//! enforces the migration's CHECKs before it writes; the Rust side never
//! writes a row in a release build. The `#[cfg(any(test, debug_assertions))]`
//! seeding helper below exists so tests in every crate can put a row in
//! place — it is a test seam, not a second writer.
//!
//! Rendering a row into a URL / port, and falling back to the compiled
//! default when it is absent, is `crate::services::service_endpoints`.

use rusqlite::{params, Connection, OptionalExtension};

use super::Db;

/// `service_endpoints.mode`. MUST MATCH the migration's CHECK and
/// `vco_lib/service_endpoints.py::MODES`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EndpointMode {
    /// VCO's own compose owns the container.
    VcoManaged,
    /// Someone else's container; VCO starts/stops it by name only.
    AdoptedContainer,
    /// A URL (native process or remote host); VCO has no lifecycle over it.
    AdoptedExternal,
}

impl EndpointMode {
    pub fn as_str(self) -> &'static str {
        match self {
            EndpointMode::VcoManaged => "vco_managed",
            EndpointMode::AdoptedContainer => "adopted_container",
            EndpointMode::AdoptedExternal => "adopted_external",
        }
    }

    pub fn parse(raw: &str) -> Option<Self> {
        match raw {
            "vco_managed" => Some(EndpointMode::VcoManaged),
            "adopted_container" => Some(EndpointMode::AdoptedContainer),
            "adopted_external" => Some(EndpointMode::AdoptedExternal),
            _ => None,
        }
    }
}

/// One `service_endpoints` row, as stored. `Deserialize` so a wire type that
/// carries it (the launcher's and the hub's `/services/status` snapshot) can
/// round-trip.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct ServiceEndpointRow {
    pub service: String,
    pub mode: EndpointMode,
    pub scheme: String,
    pub host: String,
    pub port: u16,
    pub grpc_port: Option<u16>,
    pub container_name: Option<String>,
    pub compose_project: Option<String>,
    /// Raw `data_mount_json` (`{"kind":…,"source":…,"destination":…}`).
    pub data_mount_json: Option<String>,
    pub enabled: bool,
    pub autostart: bool,
    pub source: String,
    pub confirmed_by_user: bool,
    pub verified_at: Option<i64>,
    pub updated_at: i64,
}

impl ServiceEndpointRow {
    /// A minimal row for `service` at `host:port` — the rest at the DDL
    /// defaults. Used by tests (with [`Db::service_endpoint_seed_for_tests`])
    /// and by nothing that writes in a release build.
    pub fn new(service: &str, mode: EndpointMode, host: &str, port: u16) -> Self {
        ServiceEndpointRow {
            service: service.to_string(),
            mode,
            scheme: "http".to_string(),
            host: host.to_string(),
            port,
            grpc_port: None,
            container_name: None,
            compose_project: None,
            data_mount_json: None,
            enabled: true,
            autostart: true,
            source: "install_probe".to_string(),
            confirmed_by_user: false,
            verified_at: None,
            updated_at: 0,
        }
    }
}

const SELECT_COLUMNS: &str = "service, mode, scheme, host, port, grpc_port, container_name, \
     compose_project, data_mount_json, enabled, autostart, source, confirmed_by_user, \
     verified_at, updated_at";

/// Why a row could not be read. `MissingTable` is the pre-047 DB (a stale
/// launcher.db opened read-only by a newer binary, or a DB some tool created
/// by hand); every resolver treats it exactly like an absent row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RowReadError {
    MissingTable,
    /// The row exists but a value is outside the schema (impossible under
    /// the CHECKs; reported rather than rendered).
    Malformed(String),
    Sqlite(String),
}

impl std::fmt::Display for RowReadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RowReadError::MissingTable => write!(f, "no service_endpoints table"),
            RowReadError::Malformed(m) => write!(f, "malformed service_endpoints row: {}", m),
            RowReadError::Sqlite(m) => write!(f, "service_endpoints read failed: {}", m),
        }
    }
}

fn port_from(raw: i64, what: &str) -> Result<u16, RowReadError> {
    u16::try_from(raw)
        .ok()
        .filter(|p| *p > 0)
        .ok_or_else(|| RowReadError::Malformed(format!("{} {} out of range", what, raw)))
}

type RawRow = (
    String,
    String,
    String,
    String,
    i64,
    Option<i64>,
    Option<String>,
    Option<String>,
    Option<String>,
    i64,
    i64,
    String,
    i64,
    Option<i64>,
    i64,
);

fn raw_from(r: &rusqlite::Row<'_>) -> rusqlite::Result<RawRow> {
    Ok((
        r.get(0)?,
        r.get(1)?,
        r.get(2)?,
        r.get(3)?,
        r.get(4)?,
        r.get(5)?,
        r.get(6)?,
        r.get(7)?,
        r.get(8)?,
        r.get(9)?,
        r.get(10)?,
        r.get(11)?,
        r.get(12)?,
        r.get(13)?,
        r.get(14)?,
    ))
}

fn row_from_raw(raw: RawRow) -> Result<ServiceEndpointRow, RowReadError> {
    let (
        service,
        mode,
        scheme,
        host,
        port,
        grpc_port,
        container_name,
        compose_project,
        data_mount_json,
        enabled,
        autostart,
        source,
        confirmed_by_user,
        verified_at,
        updated_at,
    ) = raw;
    let mode = EndpointMode::parse(&mode)
        .ok_or_else(|| RowReadError::Malformed(format!("mode {:?}", mode)))?;
    Ok(ServiceEndpointRow {
        port: port_from(port, "port")?,
        grpc_port: grpc_port.map(|g| port_from(g, "grpc_port")).transpose()?,
        service,
        mode,
        scheme,
        host,
        container_name,
        compose_project,
        data_mount_json,
        enabled: enabled != 0,
        autostart: autostart != 0,
        source,
        confirmed_by_user: confirmed_by_user != 0,
        verified_at,
        updated_at,
    })
}

fn classify(e: rusqlite::Error) -> RowReadError {
    let msg = e.to_string();
    if msg.contains("no such table") {
        RowReadError::MissingTable
    } else {
        RowReadError::Sqlite(msg)
    }
}

/// The row for `service` on `conn`, or `Ok(None)` when there is none.
pub fn read_row(conn: &Connection, service: &str) -> Result<Option<ServiceEndpointRow>, RowReadError> {
    let sql = format!("SELECT {} FROM service_endpoints WHERE service = ?1", SELECT_COLUMNS);
    let raw = conn
        .query_row(&sql, params![service], raw_from)
        .optional()
        .map_err(classify)?;
    raw.map(row_from_raw).transpose()
}

/// Every row on `conn`, ordered by service name.
pub fn read_all(conn: &Connection) -> Result<Vec<ServiceEndpointRow>, RowReadError> {
    let sql = format!("SELECT {} FROM service_endpoints ORDER BY service", SELECT_COLUMNS);
    let mut stmt = conn.prepare(&sql).map_err(classify)?;
    let raws = stmt
        .query_map([], raw_from)
        .map_err(classify)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(classify)?;
    raws.into_iter().map(row_from_raw).collect()
}

/// The row for `service` in `<vct root>/launcher.db`, read through a
/// READ-ONLY connection (no migrations, no writes, no WAL checkpoint) — for
/// code that holds no `Db` handle (the manifest placeholders, the tray, the
/// MCP registration). A missing file is `Ok(None)`: a machine that has never
/// run the launcher has no rows, and every resolver answers the default.
pub fn read_row_from_disk(service: &str) -> Result<Option<ServiceEndpointRow>, RowReadError> {
    use rusqlite::OpenFlags;
    let path = super::db_path();
    if !path.is_file() {
        return Ok(None);
    }
    let conn = Connection::open_with_flags(
        &path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|e| RowReadError::Sqlite(format!("open {}: {}", path.display(), e)))?;
    read_row(&conn, service)
}

impl Db {
    /// The `service_endpoints` row for `service`, through the managed
    /// connection. Poison-tolerant (a reader must never take the hub down).
    pub fn service_endpoint_get(
        &self,
        service: &str,
    ) -> Result<Option<ServiceEndpointRow>, RowReadError> {
        let guard = self.lock_recover();
        read_row(&guard, service)
    }

    /// Every `service_endpoints` row, ordered by service name.
    pub fn service_endpoints_all(&self) -> Result<Vec<ServiceEndpointRow>, RowReadError> {
        let guard = self.lock_recover();
        read_all(&guard)
    }

    /// TEST SEAM: point all three services at the unroutable sentinel
    /// `127.0.0.1:9` (IANA discard) — what every Rust test harness that
    /// resolves an endpoint seeds, the twin of `tests/conftest.py`'s rows.
    /// (Absent rows on a test database already resolve there through the
    /// resolver's harness guard; seeding makes a harness say so itself.)
    #[cfg(any(test, debug_assertions))]
    pub fn seed_sentinel_service_endpoints_for_tests(&self) -> Result<(), String> {
        for service in ["weaviate", "ollama", "code_embed"] {
            let mut row = ServiceEndpointRow::new(service, EndpointMode::VcoManaged, "127.0.0.1", 9);
            if service == "weaviate" {
                row.grpc_port = Some(9);
            }
            row.source = "test_harness_sentinel".to_string();
            self.service_endpoint_seed_for_tests(&row)?;
        }
        Ok(())
    }

    /// TEST SEAM: upsert `row` verbatim. Not compiled into release builds —
    /// production rows are written by `vco_lib.service_endpoints` only.
    #[cfg(any(test, debug_assertions))]
    pub fn service_endpoint_seed_for_tests(&self, row: &ServiceEndpointRow) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO service_endpoints (service, mode, scheme, host, port, grpc_port, \
                 container_name, compose_project, data_mount_json, enabled, autostart, source, \
                 confirmed_by_user, verified_at, updated_at) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15) \
                 ON CONFLICT(service) DO UPDATE SET mode = excluded.mode, \
                 scheme = excluded.scheme, host = excluded.host, port = excluded.port, \
                 grpc_port = excluded.grpc_port, container_name = excluded.container_name, \
                 compose_project = excluded.compose_project, \
                 data_mount_json = excluded.data_mount_json, enabled = excluded.enabled, \
                 autostart = excluded.autostart, source = excluded.source, \
                 confirmed_by_user = excluded.confirmed_by_user, \
                 verified_at = excluded.verified_at, updated_at = excluded.updated_at",
                params![
                    row.service,
                    row.mode.as_str(),
                    row.scheme,
                    row.host,
                    row.port as i64,
                    row.grpc_port.map(|g| g as i64),
                    row.container_name,
                    row.compose_project,
                    row.data_mount_json,
                    row.enabled as i64,
                    row.autostart as i64,
                    row.source,
                    row.confirmed_by_user as i64,
                    row.verified_at,
                    row.updated_at,
                ],
            )
            .map_err(|e| format!("seed service_endpoints({}): {}", row.service, e))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn weaviate_row() -> ServiceEndpointRow {
        let mut row = ServiceEndpointRow::new("weaviate", EndpointMode::AdoptedContainer, "localhost", 18081);
        row.grpc_port = Some(50055);
        row.container_name = Some("their_weaviate".into());
        row.data_mount_json = Some(r#"{"kind":"volume","source":"w","destination":"/var/lib/weaviate"}"#.into());
        row.confirmed_by_user = true;
        row.verified_at = Some(1_700_000_000_000);
        row.updated_at = 1_700_000_000_001;
        row
    }

    #[test]
    fn a_seeded_row_reads_back_verbatim() {
        let db = Db::open_in_memory().unwrap();
        assert_eq!(db.service_endpoint_get("weaviate").unwrap(), None);
        let row = weaviate_row();
        db.service_endpoint_seed_for_tests(&row).unwrap();
        assert_eq!(db.service_endpoint_get("weaviate").unwrap(), Some(row.clone()));
        assert_eq!(db.service_endpoints_all().unwrap(), vec![row]);
    }

    /// The migration's CHECKs are the schema's guarantee; the seed goes
    /// through them too, so a test cannot plant a row the writer never could.
    #[test]
    fn the_schema_refuses_rows_the_design_forbids() {
        let db = Db::open_in_memory().unwrap();
        let mut no_grpc = weaviate_row();
        no_grpc.grpc_port = None;
        assert!(db.service_endpoint_seed_for_tests(&no_grpc).is_err(), "weaviate needs grpc_port");
        let mut remote_managed = ServiceEndpointRow::new("ollama", EndpointMode::VcoManaged, "gpu.lan", 11435);
        assert!(db.service_endpoint_seed_for_tests(&remote_managed).is_err(), "vco_managed is local");
        remote_managed.host = "127.0.0.1".into();
        assert!(db.service_endpoint_seed_for_tests(&remote_managed).is_ok());
        let adopted_embed = ServiceEndpointRow::new("code_embed", EndpointMode::AdoptedExternal, "x", 1);
        assert!(db.service_endpoint_seed_for_tests(&adopted_embed).is_err(), "code_embed is vco_managed");
        let nameless = ServiceEndpointRow::new("ollama", EndpointMode::AdoptedContainer, "localhost", 11434);
        assert!(db.service_endpoint_seed_for_tests(&nameless).is_err(), "adopted_container is named");
    }

    /// A pre-047 DB has no table: that is "no row", not an error the
    /// resolver would surface.
    #[test]
    fn a_db_without_the_table_reports_missing_table() {
        let conn = Connection::open_in_memory().unwrap();
        assert_eq!(read_row(&conn, "weaviate"), Err(RowReadError::MissingTable));
        assert_eq!(read_all(&conn), Err(RowReadError::MissingTable));
    }
}
