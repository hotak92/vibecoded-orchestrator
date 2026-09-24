//! Where VCO's three core services (Weaviate, Ollama, code-embed) are
//! reached — the ONE resolver behind the hub's `/api/v1/projects/{id}/config`
//! and the launcher's project env projection (`commands::project_env_settings::populate`).
//!
//! Before v0.2.97 those two computed the Weaviate URL differently: the hub
//! served `LocalConfig` (env → `vct-config.toml` → default) while `populate`
//! built `http://localhost:<port>` from the app_state port override and
//! `services.toml` adoption. A user who adopted an external Weaviate, or wrote
//! `vct-config.toml`, got one answer from the hub and another in every
//! project's env.
//!
//! ## Precedence (highest first)
//!
//! Weaviate URL:
//!   1. the machine statement — `VCT_WEAVIATE_URL`, else `vct-config.toml`'s
//!      `weaviate_url` ([`crate::config::LocalConfig::machine_weaviate_url_statement`]).
//!      A full URL, used as stated.
//!   2. app_state `weaviate.port_override` → `http://localhost:<port>`.
//!   3. `services.toml` adoption: `adopt` → the adopted `external_url`'s origin
//!      (its host is kept — an adopted Weaviate may live on another machine);
//!      `parallel` → `http://localhost:<parallel_port>`.
//!   4. `http://localhost:8081`.
//!
//! Ollama URL: the same shape, with an env-only statement (`VCT_OLLAMA_URL`
//! — Ollama has no `vct-config.toml` key) and the default
//! `http://localhost:11435` ([`machine_ollama_url`]).
//!
//! Ports (Ollama, code-embed, and the Weaviate port where only a port is
//! wanted): app_state override → adoption (`parallel_port`, or the port of the
//! adopted URL) → the default. `refuse` / `unresolved` rows are not addresses.
//!
//! `WEAVIATE_URL` is not a leg: it is what the projection WRITES, and reading
//! it back would return the previous projection (see
//! `LocalConfig::machine_weaviate_url_statement`).
//!
//! ## Why a (C)-tier mirror, and what pins it
//!
//! Python is this repo's SSOT for cross-language logic when latency allows.
//! It does not here: the hub answers `/config` on every hook invocation, and
//! spawning an interpreter per request would put Python's start-up on the
//! hot path of every session. So the rule exists twice — here and in
//! `vco_lib/service_endpoints.py` (which `vco_lib.config_projection` resolves
//! through when `install.py` or any other Python caller projects env) — and
//! BOTH execute the same committed case table,
//! `tests/fixtures/service_endpoint_parity.json`. MUST MATCH
//! `vco_lib/service_endpoints.py`; change a rule in the table first.

use crate::db::Db;
use crate::services::adoption::{self, AdoptionMode, AdoptionState};

/// The env var that states the machine's Weaviate URL (leg 1).
/// MUST MATCH `vco_lib/service_endpoints.py::STATEMENT_ENV`.
pub const STATEMENT_ENV: &str = "VCT_WEAVIATE_URL";

/// The env var that states the machine's Ollama URL (the Ollama chain's
/// leg 1 — Ollama has no `vct-config.toml` key; the statement is env-only).
/// MUST MATCH `vco_lib/service_endpoints.py::OLLAMA_STATEMENT_ENV`.
pub const OLLAMA_STATEMENT_ENV: &str = "VCT_OLLAMA_URL";

/// `app_state` keys for explicit port overrides (leg 2 / the port chain).
/// MUST MATCH the `app_state_key`s in `tests/fixtures/service_endpoint_parity.json`.
pub const APP_STATE_KEY_WEAVIATE_PORT: &str = "weaviate.port_override";
pub const APP_STATE_KEY_OLLAMA_PORT: &str = "ollama.port_override";
pub const APP_STATE_KEY_CODE_EMBED_PORT: &str = "code_embed.port_override";

/// Canonical host ports. MUST MATCH `vco_lib/service_endpoints.py` and the
/// compose defaults in `infrastructure/docker-compose.yml`.
pub const DEFAULT_WEAVIATE_PORT: u16 = 8081;
pub const DEFAULT_OLLAMA_PORT: u16 = 11435;
pub const DEFAULT_CODE_EMBED_PORT: u16 = 11440;

/// One of the three core services whose address the launcher resolves.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CoreService {
    Weaviate,
    Ollama,
    CodeEmbed,
}

impl CoreService {
    /// The service's row name in `services.toml`.
    pub fn adoption_name(self) -> &'static str {
        match self {
            CoreService::Weaviate => "weaviate",
            CoreService::Ollama => "ollama",
            CoreService::CodeEmbed => "code_embed",
        }
    }

    pub fn app_state_key(self) -> &'static str {
        match self {
            CoreService::Weaviate => APP_STATE_KEY_WEAVIATE_PORT,
            CoreService::Ollama => APP_STATE_KEY_OLLAMA_PORT,
            CoreService::CodeEmbed => APP_STATE_KEY_CODE_EMBED_PORT,
        }
    }

    pub fn default_port(self) -> u16 {
        match self {
            CoreService::Weaviate => DEFAULT_WEAVIATE_PORT,
            CoreService::Ollama => DEFAULT_OLLAMA_PORT,
            CoreService::CodeEmbed => DEFAULT_CODE_EMBED_PORT,
        }
    }
}

/// A raw app_state override as a port: ASCII digits only (surrounding
/// whitespace ignored), 1..=65535. Anything else is "no override", never an
/// error — the override is best-effort and must not block an env write.
pub fn parse_port_override(raw: Option<&str>) -> Option<u16> {
    let s = raw?.trim();
    if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    s.parse::<u16>().ok().filter(|p| *p > 0)
}

/// `scheme://authority` of a URL — path, query and trailing `/` dropped.
/// Surrounding whitespace (a Windows `\r`) is trimmed.
fn origin_of(url: &str) -> String {
    let url = url.trim();
    match url.split_once("://") {
        Some((scheme, rest)) => {
            let authority = rest.split('/').next().unwrap_or(rest);
            format!("{}://{}", scheme, authority)
        }
        None => url.split('/').next().unwrap_or(url).to_string(),
    }
}

/// The explicit port in a URL's authority, if it names one.
fn explicit_port(url: &str) -> Option<u16> {
    let origin = origin_of(url);
    let authority = origin.split_once("://").map(|(_, a)| a).unwrap_or(&origin);
    let (_, port) = authority.rsplit_once(':')?;
    if port.is_empty() || !port.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    port.parse::<u16>().ok()
}

/// The port a URL addresses: its explicit port, else the scheme's default
/// (443 for `https`, 80 for `http`). `None` for a URL with neither.
pub fn port_of_url(url: &str) -> Option<u16> {
    if let Some(p) = explicit_port(url) {
        return Some(p);
    }
    let scheme = url.trim().split_once("://").map(|(s, _)| s.to_ascii_lowercase());
    match scheme.as_deref() {
        Some("https") => Some(443),
        Some("http") => Some(80),
        _ => None,
    }
}

/// Port chain: app_state override → adoption → default. Pure.
pub fn resolve_port(service: CoreService, port_override: Option<&str>, adoption: &AdoptionState) -> u16 {
    if let Some(p) = parse_port_override(port_override) {
        return p;
    }
    if let Some(svc) = adoption.get(service.adoption_name()) {
        match svc.mode {
            AdoptionMode::Parallel => {
                if let Some(p) = svc.parallel_port {
                    return p;
                }
            }
            AdoptionMode::Adopt => {
                if let Some(p) = svc.external_url.as_deref().and_then(explicit_port) {
                    return p;
                }
            }
            AdoptionMode::Refuse | AdoptionMode::Unresolved => {}
        }
    }
    service.default_port()
}

/// Weaviate URL chain (see the module docs). Pure: every input is an argument.
pub fn resolve_weaviate_url(
    statement: Option<&str>,
    port_override: Option<&str>,
    adoption: &AdoptionState,
) -> String {
    if let Some(s) = statement.map(str::trim).filter(|s| !s.is_empty()) {
        return s.trim_end_matches('/').to_string();
    }
    url_below_statement(CoreService::Weaviate, port_override, adoption)
}

/// Ollama URL chain — the same shape as Weaviate's, with an env-only
/// statement (`VCT_OLLAMA_URL`; Ollama has no `vct-config.toml` key):
/// statement → app_state `ollama.port_override` → `services.toml`
/// adoption → `http://localhost:11435`. `OLLAMA_URL` is NOT a leg — it is
/// what the projection WRITES, exactly like `WEAVIATE_URL`. Pure.
pub fn resolve_ollama_url(
    statement: Option<&str>,
    port_override: Option<&str>,
    adoption: &AdoptionState,
) -> String {
    if let Some(s) = statement.map(str::trim).filter(|s| !s.is_empty()) {
        return s.trim_end_matches('/').to_string();
    }
    url_below_statement(CoreService::Ollama, port_override, adoption)
}

/// The legs below the statement, shared by the Weaviate and Ollama URL
/// chains: port override → adoption → the service's default port.
fn url_below_statement(
    service: CoreService,
    port_override: Option<&str>,
    adoption: &AdoptionState,
) -> String {
    if let Some(p) = parse_port_override(port_override) {
        return format!("http://localhost:{}", p);
    }
    if let Some(svc) = adoption.get(service.adoption_name()) {
        match svc.mode {
            AdoptionMode::Adopt => {
                if let Some(url) = svc.external_url.as_deref().filter(|u| !u.trim().is_empty()) {
                    return origin_of(url);
                }
            }
            AdoptionMode::Parallel => {
                if let Some(p) = svc.parallel_port {
                    return format!("http://localhost:{}", p);
                }
            }
            AdoptionMode::Refuse | AdoptionMode::Unresolved => {}
        }
    }
    format!("http://localhost:{}", service.default_port())
}

/// The Weaviate port that goes with a resolved URL (for `WEAVIATE_PORT`).
pub fn weaviate_port_for_url(url: &str) -> u16 {
    port_of_url(url).unwrap_or(DEFAULT_WEAVIATE_PORT)
}

/// The raw app_state override for `service`; `None` on a missing row or any
/// DB error (soft-fail — see [`parse_port_override`]).
pub fn read_port_override(db: &Db, service: CoreService) -> Option<String> {
    db.app_state_get(service.app_state_key()).ok().flatten()
}

/// This machine's port for `service`, from live state.
pub fn machine_port(db: &Db, service: CoreService) -> u16 {
    resolve_port(service, read_port_override(db, service).as_deref(), &adoption::read())
}

/// [`machine_port`] for code that holds no `Db` handle — the manifest
/// placeholders `{weaviate_port}` / `{ollama_port}` / `{code_embed_port}`
/// (`manifest::PlaceholderCtx::resolve`), which the hub's module health
/// poller and the launcher expand. The override is read from
/// `<vct root>/launcher.db` through a READ-ONLY connection (no migrations, no
/// writes; a missing file or table is "no override"), so both callers answer
/// the same chain the projection does.
pub fn machine_port_from_disk(service: CoreService) -> u16 {
    resolve_port(service, read_port_override_from_disk(service).as_deref(), &adoption::read())
}

fn read_port_override_from_disk(service: CoreService) -> Option<String> {
    use rusqlite::{Connection, OpenFlags};
    let path = crate::db::db_path();
    if !path.is_file() {
        return None;
    }
    let conn = Connection::open_with_flags(
        &path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .ok()?;
    conn.query_row(
        "SELECT value FROM app_state WHERE key = ?1",
        [service.app_state_key()],
        |r| r.get::<_, String>(0),
    )
    .ok()
}

/// This machine's Weaviate URL, from live state: the machine statement,
/// `db`'s app_state override, and `services.toml`. The hub's `/config` and
/// the launcher's `populate` both call this.
pub fn machine_weaviate_url(db: &Db) -> String {
    resolve_weaviate_url(
        crate::config::LocalConfig::machine_weaviate_url_statement().as_deref(),
        read_port_override(db, CoreService::Weaviate).as_deref(),
        &adoption::read(),
    )
}

/// This machine's Ollama URL, from live state: the `VCT_OLLAMA_URL`
/// statement, `db`'s app_state override, and `services.toml`. The hub's
/// `/config` serves this (v0.2.97 lane X; before, an env-only chain that
/// ignored the override and adoption).
pub fn machine_ollama_url(db: &Db) -> String {
    let statement = std::env::var(OLLAMA_STATEMENT_ENV)
        .ok()
        .and_then(|v| {
            let v = v.trim().to_string();
            (!v.is_empty()).then_some(v)
        });
    resolve_ollama_url(
        statement.as_deref(),
        read_port_override(db, CoreService::Ollama).as_deref(),
        &adoption::read(),
    )
}

/// Where a launcher-side Weaviate CLIENT connects (the KG / codegraph
/// dashboards, the maintenance and identity commands): the machine resolver,
/// under the two env statements a client has always honoured —
/// `VCT_WEAVIATE_URL`, then the legacy `WEAVIATE_URL` alias that
/// [`crate::config::LocalConfig`] documents. A client may read `WEAVIATE_URL`
/// (it is the projected transport, exactly what every Python client reads);
/// the machine resolver above it may not.
///
/// v0.2.97 (lane W): replaces four private copies (`commands::kg`,
/// `commands::codegraph`, `commands::maintenance`,
/// `commands::project_identity`) that fell through to `LocalConfig` and so
/// never saw an adopted external Weaviate.
pub fn client_weaviate_url(db: &Db) -> String {
    for key in [STATEMENT_ENV, "WEAVIATE_URL"] {
        if let Ok(v) = std::env::var(key) {
            let v = v.trim();
            if !v.is_empty() {
                return v.trim_end_matches('/').to_string();
            }
        }
    }
    machine_weaviate_url(db)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::adoption::ServiceAdoption;

    fn table() -> serde_json::Value {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../tests/fixtures/service_endpoint_parity.json");
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
        serde_json::from_str(&text).expect("parity table parses")
    }

    fn service(name: &str) -> CoreService {
        match name {
            "weaviate" => CoreService::Weaviate,
            "ollama" => CoreService::Ollama,
            "code_embed" => CoreService::CodeEmbed,
            other => panic!("unknown service {other}"),
        }
    }

    fn adoption_for(name: &str, row: &serde_json::Value) -> AdoptionState {
        let mut state = AdoptionState::default();
        if row.is_null() {
            return state;
        }
        let mode = match row["mode"].as_str().unwrap() {
            "adopt" => AdoptionMode::Adopt,
            "parallel" => AdoptionMode::Parallel,
            "refuse" => AdoptionMode::Refuse,
            "unresolved" => AdoptionMode::Unresolved,
            other => panic!("unknown mode {other}"),
        };
        state.upsert(ServiceAdoption {
            name: name.to_string(),
            mode,
            external_url: row["external_url"].as_str().map(String::from),
            parallel_port: row["parallel_port"].as_u64().map(|p| p as u16),
            container_name: None,
        });
        state
    }

    #[test]
    fn constants_match_the_parity_table() {
        let t = table();
        let c = &t["constants"];
        assert_eq!(c["statement_env"], STATEMENT_ENV);
        assert_eq!(c["ollama_statement_env"], OLLAMA_STATEMENT_ENV);
        for svc in [CoreService::Weaviate, CoreService::Ollama, CoreService::CodeEmbed] {
            let row = &c["services"][svc.adoption_name()];
            assert_eq!(row["app_state_key"], svc.app_state_key(), "{:?}", svc);
            assert_eq!(row["default_port"], svc.default_port(), "{:?}", svc);
        }
    }

    #[test]
    fn weaviate_url_cases_match_the_parity_table() {
        let t = table();
        let cases = t["weaviate_url_cases"].as_array().unwrap();
        assert!(!cases.is_empty());
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let adoption = adoption_for("weaviate", &case["adoption"]);
            let url = resolve_weaviate_url(
                case["statement"].as_str(),
                case["port_override"].as_str(),
                &adoption,
            );
            assert_eq!(url, case["expect_url"].as_str().unwrap(), "case `{name}`");
            assert_eq!(
                weaviate_port_for_url(&url) as u64,
                case["expect_port"].as_u64().unwrap(),
                "case `{name}`"
            );
        }
    }

    #[test]
    fn port_cases_match_the_parity_table() {
        let t = table();
        for case in t["port_cases"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let svc_name = case["service"].as_str().unwrap();
            let adoption = adoption_for(svc_name, &case["adoption"]);
            let port = resolve_port(service(svc_name), case["port_override"].as_str(), &adoption);
            assert_eq!(port as u64, case["expect_port"].as_u64().unwrap(), "case `{name}`");
        }
    }

    #[test]
    fn ollama_url_cases_match_the_parity_table() {
        let t = table();
        let cases = t["ollama_url_cases"].as_array().unwrap();
        assert!(!cases.is_empty());
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let adoption = adoption_for("ollama", &case["adoption"]);
            let url = resolve_ollama_url(
                case["statement"].as_str(),
                case["port_override"].as_str(),
                &adoption,
            );
            assert_eq!(url, case["expect_url"].as_str().unwrap(), "case `{name}`");
            assert_eq!(
                port_of_url(&url).unwrap_or(DEFAULT_OLLAMA_PORT) as u64,
                case["expect_port"].as_u64().unwrap(),
                "case `{name}`"
            );
        }
    }

    /// The live-state reader: a `vct-config.toml`-free machine with an adopted
    /// external Weaviate in `services.toml` and no env statement resolves to
    /// the adopted host — for the hub and the projection alike, since both
    /// call this function.
    #[test]
    fn machine_url_reads_services_toml_and_app_state() {
        let _g = crate::test_env::state_dir_guard_with(&[(STATEMENT_ENV, None)]);
        let db = Db::open_in_memory().unwrap();
        let mut state = AdoptionState::default();
        state.upsert(ServiceAdoption {
            name: "weaviate".into(),
            mode: AdoptionMode::Adopt,
            external_url: Some("http://weaviate.lan:8090".into()),
            parallel_port: None,
            container_name: None,
        });
        adoption::write(&state).unwrap();
        assert_eq!(machine_weaviate_url(&db), "http://weaviate.lan:8090");

        db.app_state_set(APP_STATE_KEY_WEAVIATE_PORT, "18081").unwrap();
        assert_eq!(machine_weaviate_url(&db), "http://localhost:18081");
        assert_eq!(machine_port(&db, CoreService::Weaviate), 18081);
    }

    #[test]
    fn machine_url_honours_the_env_statement_over_launcher_state() {
        let _g = crate::test_env::state_dir_guard_with(&[
            (STATEMENT_ENV, Some("http://from-env:7777")),
            ("WEAVIATE_URL", Some("http://transport:1")),
        ]);
        let db = Db::open_in_memory().unwrap();
        db.app_state_set(APP_STATE_KEY_WEAVIATE_PORT, "18081").unwrap();
        assert_eq!(machine_weaviate_url(&db), "http://from-env:7777");
    }

    #[test]
    fn client_url_honours_the_transport_then_falls_to_the_machine_resolver() {
        let _g = crate::test_env::state_dir_guard_with(&[
            (STATEMENT_ENV, None),
            ("WEAVIATE_URL", Some("http://transport:1/")),
        ]);
        let db = Db::open_in_memory().unwrap();
        db.app_state_set(APP_STATE_KEY_WEAVIATE_PORT, "18081").unwrap();
        assert_eq!(client_weaviate_url(&db), "http://transport:1");
        std::env::remove_var("WEAVIATE_URL");
        assert_eq!(client_weaviate_url(&db), "http://localhost:18081");
    }

    /// The Db-less reader answers what [`machine_port`] answers over the
    /// same on-disk launcher.db.
    #[test]
    fn port_from_disk_reads_the_launcher_db_override() {
        let _g = crate::test_env::state_dir_guard();
        assert_eq!(machine_port_from_disk(CoreService::CodeEmbed), DEFAULT_CODE_EMBED_PORT);
        let db = Db::open().unwrap();
        db.app_state_set(APP_STATE_KEY_CODE_EMBED_PORT, "21440").unwrap();
        assert_eq!(machine_port_from_disk(CoreService::CodeEmbed), 21440);
        assert_eq!(machine_port(&db, CoreService::CodeEmbed), 21440);
    }

    /// `WEAVIATE_URL` is the projection's own output; the machine resolver
    /// must not read it back.
    #[test]
    fn machine_url_ignores_the_projected_transport_variable() {
        let _g = crate::test_env::state_dir_guard_with(&[
            (STATEMENT_ENV, None),
            ("WEAVIATE_URL", Some("http://stale-projection:1")),
        ]);
        let db = Db::open_in_memory().unwrap();
        assert_eq!(machine_weaviate_url(&db), "http://localhost:8081");
    }

    /// The Ollama machine resolver: adoption and the app_state override
    /// are legs, `OLLAMA_URL` (the projection's output) is not, and the
    /// `VCT_OLLAMA_URL` statement wins over both.
    #[test]
    fn machine_ollama_url_reads_adoption_override_and_statement() {
        let _g = crate::test_env::state_dir_guard_with(&[
            (OLLAMA_STATEMENT_ENV, None),
            ("OLLAMA_URL", Some("http://stale-projection:1")),
        ]);
        let db = Db::open_in_memory().unwrap();
        let mut state = AdoptionState::default();
        state.upsert(ServiceAdoption {
            name: "ollama".into(),
            mode: AdoptionMode::Adopt,
            external_url: Some("http://ollama.lan:11439/api/tags".into()),
            parallel_port: None,
            container_name: None,
        });
        adoption::write(&state).unwrap();
        assert_eq!(machine_ollama_url(&db), "http://ollama.lan:11439");

        db.app_state_set(APP_STATE_KEY_OLLAMA_PORT, "21435").unwrap();
        assert_eq!(machine_ollama_url(&db), "http://localhost:21435");

        std::env::set_var(OLLAMA_STATEMENT_ENV, "http://from-env:11500");
        assert_eq!(machine_ollama_url(&db), "http://from-env:11500");
    }
}
