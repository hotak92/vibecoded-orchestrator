//! Where VCO's three core services (Weaviate, Ollama, code-embed) are
//! reached — the ONE resolver behind the hub's `/config`, the launcher's
//! project env projection (`commands::project_env_settings::populate`), the
//! MCP registration, the manifest port placeholders and the tray.
//!
//! ## The rule (v0.2.97, service endpoints SSOT)
//!
//! **row → compiled default.** The machine's answer is its
//! `service_endpoints` row in launcher.db (migration 047, read by
//! [`crate::db::service_endpoints`]); when there is no row — first boot
//! before install finished, or a broken install — the compiled default
//! (`http://localhost:8081` + gRPC 50052, `:11435`, `:11440`), with ONE
//! warning per process per service. Never an error.
//!
//! Nothing else is a leg. These inputs were legs before v0.2.97 and are read
//! by NO resolver now (their values are imported into rows once, by the
//! Python migration in `vco_lib.service_reconcile`):
//!   * `services.toml` adoption rows (`adopt` / `parallel`);
//!   * the app_state `weaviate/ollama/code_embed.port_override` keys;
//!   * `vct-config.toml`'s `weaviate_url`;
//!   * `VCT_WEAVIATE_URL`, `VCT_OLLAMA_URL`, and the projected transport
//!     (`WEAVIATE_URL`, `OLLAMA_URL`, `*_PORT`, `GRPC_PORT`, …).
//!
//! The hub and the launcher are machine-scoped processes that any project's
//! hook can spawn with that project's projected env; reading any of those
//! variables back would serve one project's (possibly stale) projection to
//! every project. The constants naming them stay below only so callers and
//! tests can say "this input is ignored".
//!
//! ## The render — the only cross-language mirror
//!
//! `vco_lib/service_endpoints.py` renders the same row the same way. Both
//! execute `tests/fixtures/service_endpoint_parity.json` (render cases,
//! absent-row cases, and ignored-input cases run as behaviour). MUST MATCH
//! `vco_lib/service_endpoints.py`; change a rule in the table first.
//!
//!   URL  = `scheme://host:port`, the `:port` omitted when it is the
//!          scheme's default (80 / 443). `host` is used verbatim, so an
//!          IPv6 literal is stored and rendered with its brackets.
//!   port = row port.  gRPC = row `grpc_port` (Weaviate).
//!
//! Why the render is mirrored rather than shared: the hub answers `/config`
//! on every hook call and cannot spawn a Python per request (A>B>C, tier C).

use std::sync::atomic::{AtomicBool, Ordering};

use crate::db::service_endpoints::{self as rows, RowReadError, ServiceEndpointRow};
use crate::db::Db;

/// RETIRED INPUT (read by no resolver since v0.2.97): the pre-v0.2.97
/// machine statement of the Weaviate URL. Named so callers and tests can pin
/// that it is ignored. MUST MATCH `vco_lib/service_endpoints.py`.
pub const STATEMENT_ENV: &str = "VCT_WEAVIATE_URL";

/// RETIRED INPUT: the pre-v0.2.97 machine statement of the Ollama URL.
pub const OLLAMA_STATEMENT_ENV: &str = "VCT_OLLAMA_URL";

/// RETIRED INPUTS: the app_state port-override keys. No Rust or Python code
/// ever wrote them; the v0.2.97 importer folds any present value into the
/// row's `port` and deletes the key. No resolver reads them.
pub const APP_STATE_KEY_WEAVIATE_PORT: &str = "weaviate.port_override";
pub const APP_STATE_KEY_OLLAMA_PORT: &str = "ollama.port_override";
pub const APP_STATE_KEY_CODE_EMBED_PORT: &str = "code_embed.port_override";

/// Compiled defaults — the answer for an absent row. MUST MATCH
/// `vco_lib/service_endpoints.py` and the compose defaults in
/// `infrastructure/docker-compose.yml`.
pub const DEFAULT_WEAVIATE_PORT: u16 = 8081;
pub const DEFAULT_WEAVIATE_GRPC_PORT: u16 = 50052;
pub const DEFAULT_OLLAMA_PORT: u16 = 11435;
pub const DEFAULT_CODE_EMBED_PORT: u16 = 11440;
pub const DEFAULT_SCHEME: &str = "http";
pub const DEFAULT_HOST: &str = "localhost";

/// One of the three core services.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CoreService {
    Weaviate,
    Ollama,
    CodeEmbed,
}

impl CoreService {
    pub const ALL: [CoreService; 3] = [CoreService::Weaviate, CoreService::Ollama, CoreService::CodeEmbed];

    /// The `service_endpoints.service` key (also the compose service name).
    pub fn name(self) -> &'static str {
        match self {
            CoreService::Weaviate => "weaviate",
            CoreService::Ollama => "ollama",
            CoreService::CodeEmbed => "code_embed",
        }
    }

    /// Same as [`CoreService::name`]; the pre-v0.2.97 name of this accessor
    /// (it keyed `services.toml` rows, which use the same names).
    pub fn adoption_name(self) -> &'static str {
        self.name()
    }

    pub fn from_name(name: &str) -> Option<Self> {
        CoreService::ALL.into_iter().find(|s| s.name() == name)
    }

    /// The retired app_state override key for this service (importer input).
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

    fn index(self) -> usize {
        match self {
            CoreService::Weaviate => 0,
            CoreService::Ollama => 1,
            CoreService::CodeEmbed => 2,
        }
    }
}

// ─── the render (pure; the mirror the parity table pins) ────────────────

fn scheme_default_port(scheme: &str) -> Option<u16> {
    match scheme {
        "http" => Some(80),
        "https" => Some(443),
        _ => None,
    }
}

/// The URL `row` addresses, or the compiled default for `service` when there
/// is no row. Pure.
pub fn render_url(service: CoreService, row: Option<&ServiceEndpointRow>) -> String {
    match row {
        None => format!("{}://{}:{}", DEFAULT_SCHEME, DEFAULT_HOST, service.default_port()),
        Some(r) => {
            if scheme_default_port(&r.scheme) == Some(r.port) {
                format!("{}://{}", r.scheme, r.host)
            } else {
                format!("{}://{}:{}", r.scheme, r.host, r.port)
            }
        }
    }
}

/// The host port `row` states, or `service`'s compiled default. Pure.
pub fn render_port(service: CoreService, row: Option<&ServiceEndpointRow>) -> u16 {
    row.map(|r| r.port).unwrap_or_else(|| service.default_port())
}

/// Weaviate's gRPC port from its row, or 50052. Pure.
pub fn render_grpc_port(row: Option<&ServiceEndpointRow>) -> u16 {
    row.and_then(|r| r.grpc_port).unwrap_or(DEFAULT_WEAVIATE_GRPC_PORT)
}

/// The explicit port in a URL's authority, else 443 for `https` / 80 for
/// `http`; `None` for a URL with neither.
pub fn port_of_url(url: &str) -> Option<u16> {
    let url = url.trim();
    let (scheme, rest) = match url.split_once("://") {
        Some((s, r)) => (Some(s.to_ascii_lowercase()), r),
        None => (None, url),
    };
    let authority = rest.split('/').next().unwrap_or(rest);
    // An IPv6 literal's colons are inside the brackets; the port follows `]`.
    let tail = match authority.rfind(']') {
        Some(i) => &authority[i + 1..],
        None => authority,
    };
    if let Some((_, p)) = tail.rsplit_once(':') {
        if !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()) {
            return p.parse::<u16>().ok();
        }
    }
    match scheme.as_deref() {
        Some("https") => Some(443),
        Some("http") => Some(80),
        _ => None,
    }
}

/// The Weaviate port that goes with a Weaviate URL (for `WEAVIATE_PORT`).
pub fn weaviate_port_for_url(url: &str) -> u16 {
    port_of_url(url).unwrap_or(DEFAULT_WEAVIATE_PORT)
}

// ─── live reads ─────────────────────────────────────────────────────────

static WARNED_ABSENT: [AtomicBool; 3] = [AtomicBool::new(false), AtomicBool::new(false), AtomicBool::new(false)];

/// Log, once per process per service, that the resolver answered the
/// compiled default because `service` has no usable row.
fn warn_default_once(service: CoreService, why: &str) {
    if WARNED_ABSENT[service.index()].swap(true, Ordering::Relaxed) {
        return;
    }
    tracing::warn!(
        service = service.name(),
        reason = why,
        default = %render_url(service, None),
        "[service-endpoints] no usable service_endpoints row; answering the compiled default \
         (a successful install/update writes the row — run `python install.py --update` if this persists)"
    );
}

fn usable(service: CoreService, read: Result<Option<ServiceEndpointRow>, RowReadError>) -> Option<ServiceEndpointRow> {
    match read {
        Ok(Some(row)) => Some(row),
        Ok(None) => {
            warn_default_once(service, "no row");
            None
        }
        Err(e) => {
            warn_default_once(service, &e.to_string());
            None
        }
    }
}

/// `service`'s row through `db`, or `None` (default applies; warned once).
pub fn machine_row(db: &Db, service: CoreService) -> Option<ServiceEndpointRow> {
    usable(service, db.service_endpoint_get(service.name()))
}

/// `service`'s row from `<vct root>/launcher.db` through a read-only
/// connection — for code that holds no `Db` handle.
pub fn machine_row_from_disk(service: CoreService) -> Option<ServiceEndpointRow> {
    usable(service, rows::read_row_from_disk(service.name()))
}

/// This machine's URL for `service`.
pub fn machine_url(db: &Db, service: CoreService) -> String {
    render_url(service, machine_row(db, service).as_ref())
}

/// This machine's Weaviate URL — what the hub's `/config`, the projection
/// and every launcher dashboard use.
pub fn machine_weaviate_url(db: &Db) -> String {
    machine_url(db, CoreService::Weaviate)
}

/// This machine's Ollama URL.
pub fn machine_ollama_url(db: &Db) -> String {
    machine_url(db, CoreService::Ollama)
}

/// This machine's code-embed service URL.
pub fn machine_code_embed_url(db: &Db) -> String {
    machine_url(db, CoreService::CodeEmbed)
}

/// This machine's host port for `service`.
pub fn machine_port(db: &Db, service: CoreService) -> u16 {
    render_port(service, machine_row(db, service).as_ref())
}

/// This machine's Weaviate gRPC port.
pub fn machine_grpc_port(db: &Db) -> u16 {
    render_grpc_port(machine_row(db, CoreService::Weaviate).as_ref())
}

/// [`machine_port`] for code that holds no `Db` handle — the manifest
/// placeholders `{weaviate_port}` / `{ollama_port}` / `{code_embed_port}`,
/// the tray, the MCP registration.
pub fn machine_port_from_disk(service: CoreService) -> u16 {
    render_port(service, machine_row_from_disk(service).as_ref())
}

/// [`machine_url`] for code that holds no `Db` handle.
pub fn machine_url_from_disk(service: CoreService) -> String {
    render_url(service, machine_row_from_disk(service).as_ref())
}

/// [`machine_grpc_port`] for code that holds no `Db` handle.
pub fn machine_grpc_port_from_disk() -> u16 {
    render_grpc_port(machine_row_from_disk(CoreService::Weaviate).as_ref())
}

/// Where a launcher-side Weaviate CLIENT connects (the KG / codegraph
/// dashboards, the maintenance and identity commands, the hub's CLI proxy).
///
/// v0.2.97 INTERIM: this still honours `VCT_WEAVIATE_URL`, then the
/// projected `WEAVIATE_URL`, above the machine row. Those env legs are the
/// defect the service-endpoints plan retires (§4f: a launcher or hub started
/// from a project's hook inherits that project's projection); the work
/// package that migrates these callers onto [`machine_weaviate_url`]
/// deletes this function. Only the fallback below it moved to the row.
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

/// Endpoint env vars a user or an older VCO may still export, which no
/// resolver reads any more. Each `(name, value)` present and non-empty in
/// this process's environment — for a one-line startup warning.
pub fn retired_endpoint_env_present() -> Vec<(&'static str, String)> {
    [STATEMENT_ENV, OLLAMA_STATEMENT_ENV]
        .into_iter()
        .filter_map(|k| {
            std::env::var(k)
                .ok()
                .filter(|v| !v.trim().is_empty())
                .map(|v| (k, v))
        })
        .collect()
}

/// Warn once (per call site) about [`retired_endpoint_env_present`]: the
/// launcher and the hub call this at startup.
pub fn warn_retired_endpoint_env(process: &str) {
    for (key, value) in retired_endpoint_env_present() {
        tracing::warn!(
            process,
            key,
            value = %value,
            "[service-endpoints] {} is set but no longer read (v0.2.97): the launcher DB's \
             service_endpoints row is the one source of truth. See \
             `python -m vco_lib.service_endpoints show`.",
            key
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::service_endpoints::EndpointMode;

    fn table() -> serde_json::Value {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../tests/fixtures/service_endpoint_parity.json");
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
        serde_json::from_str(&text).expect("parity table parses")
    }

    fn service(name: &str) -> CoreService {
        CoreService::from_name(name).unwrap_or_else(|| panic!("unknown service {name}"))
    }

    /// A table row object → a `ServiceEndpointRow` (unset fields at the DDL
    /// defaults).
    fn row_from(svc: &str, v: &serde_json::Value) -> ServiceEndpointRow {
        let mode = EndpointMode::parse(v["mode"].as_str().unwrap()).expect("mode");
        let mut row = ServiceEndpointRow::new(
            svc,
            mode,
            v.get("host").and_then(|h| h.as_str()).unwrap_or(DEFAULT_HOST),
            v["port"].as_u64().unwrap() as u16,
        );
        if let Some(s) = v.get("scheme").and_then(|s| s.as_str()) {
            row.scheme = s.to_string();
        }
        row.grpc_port = v.get("grpc_port").and_then(|g| g.as_u64()).map(|g| g as u16);
        row.container_name = v.get("container_name").and_then(|c| c.as_str()).map(String::from);
        row
    }

    #[test]
    fn constants_match_the_parity_table() {
        let t = table();
        let c = &t["constants"];
        for svc in CoreService::ALL {
            assert_eq!(c["default_ports"][svc.name()], svc.default_port(), "{:?}", svc);
        }
        assert_eq!(c["weaviate_grpc_default"], DEFAULT_WEAVIATE_GRPC_PORT);
        assert_eq!(c["default_scheme"], DEFAULT_SCHEME);
        assert_eq!(c["default_host"], DEFAULT_HOST);
        let not_legs: Vec<&str> = c["retired_inputs"]["env"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert!(not_legs.contains(&STATEMENT_ENV) && not_legs.contains(&OLLAMA_STATEMENT_ENV));
        let keys: Vec<&str> = c["retired_inputs"]["app_state_keys"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        for svc in CoreService::ALL {
            assert!(keys.contains(&svc.app_state_key()), "{:?}", svc);
        }
    }

    #[test]
    fn render_cases_match_the_parity_table() {
        let t = table();
        let cases = t["render_cases"].as_array().unwrap();
        assert!(!cases.is_empty());
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let svc_name = case["service"].as_str().unwrap();
            let svc = service(svc_name);
            let row = row_from(svc_name, &case["row"]);
            assert_eq!(render_url(svc, Some(&row)), case["expect_url"].as_str().unwrap(), "case `{name}`");
            assert_eq!(render_port(svc, Some(&row)) as u64, case["expect_port"].as_u64().unwrap(), "case `{name}`");
            if let Some(g) = case.get("expect_grpc_port").and_then(|g| g.as_u64()) {
                assert_eq!(render_grpc_port(Some(&row)) as u64, g, "case `{name}`");
            }
            // The row also round-trips through the real table (its CHECKs
            // are the schema the writer enforces).
            let db = Db::open_in_memory().unwrap();
            db.service_endpoint_seed_for_tests(&row)
                .unwrap_or_else(|e| panic!("case `{name}` violates the schema: {e}"));
            assert_eq!(machine_url(&db, svc), case["expect_url"].as_str().unwrap(), "case `{name}`");
        }
    }

    #[test]
    fn absent_row_cases_match_the_parity_table() {
        let t = table();
        let db = Db::open_in_memory().unwrap();
        for case in t["absent_row_cases"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let svc = service(case["service"].as_str().unwrap());
            assert_eq!(render_url(svc, None), case["expect_url"].as_str().unwrap(), "case `{name}`");
            assert_eq!(machine_url(&db, svc), case["expect_url"].as_str().unwrap(), "case `{name}`");
            assert_eq!(machine_port(&db, svc) as u64, case["expect_port"].as_u64().unwrap(), "case `{name}`");
            if let Some(g) = case.get("expect_grpc_port").and_then(|g| g.as_u64()) {
                assert_eq!(machine_grpc_port(&db) as u64, g, "case `{name}`");
            }
        }
    }

    /// Removes a file on drop (the `vct-config.toml` planted beside the test
    /// binary must never outlive the case).
    struct RemoveOnDrop(std::path::PathBuf);
    impl Drop for RemoveOnDrop {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
        }
    }

    /// Every retired input from the table is PUT IN PLACE for real — the
    /// env, the app_state keys in the same launcher.db, a `services.toml`,
    /// and a `vct-config.toml` next to the running binary — and the resolver
    /// still answers the row (or, with no row, the default).
    #[test]
    fn ignored_input_cases_match_the_parity_table() {
        use crate::services::adoption::{self, AdoptionMode, AdoptionState, ServiceAdoption};
        let t = table();
        for case in t["ignored_input_cases"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let svc_name = case["service"].as_str().unwrap();
            let svc = service(svc_name);
            let env: Vec<(String, String)> = case["env"]
                .as_object()
                .map(|m| m.iter().map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string())).collect())
                .unwrap_or_default();
            let env_refs: Vec<(&str, Option<&str>)> =
                env.iter().map(|(k, v)| (k.as_str(), Some(v.as_str()))).collect();
            let _g = crate::test_env::state_dir_guard_with(&env_refs);

            let db = Db::open().unwrap();
            if let Some(kv) = case["app_state"].as_object() {
                for (k, v) in kv {
                    db.app_state_set(k, v.as_str().unwrap()).unwrap();
                }
            }
            if let Some(list) = case["services_toml"].as_array() {
                let mut state = AdoptionState::default();
                for r in list {
                    state.upsert(ServiceAdoption {
                        name: r["name"].as_str().unwrap().to_string(),
                        mode: match r["mode"].as_str().unwrap() {
                            "adopt" => AdoptionMode::Adopt,
                            "parallel" => AdoptionMode::Parallel,
                            "refuse" => AdoptionMode::Refuse,
                            _ => AdoptionMode::Unresolved,
                        },
                        external_url: r.get("external_url").and_then(|u| u.as_str()).map(String::from),
                        parallel_port: r.get("parallel_port").and_then(|p| p.as_u64()).map(|p| p as u16),
                        container_name: None,
                    });
                }
                adoption::write(&state).unwrap();
            }
            let _cfg = case["vct_config_toml"].as_str().map(|body| {
                let dir = std::env::current_exe().unwrap().parent().unwrap().to_path_buf();
                let p = dir.join("vct-config.toml");
                std::fs::write(&p, body).unwrap();
                RemoveOnDrop(p)
            });
            if !case["row"].is_null() {
                db.service_endpoint_seed_for_tests(&row_from(svc_name, &case["row"])).unwrap();
            }

            let want_url = case["expect_url"].as_str().unwrap();
            let want_port = case["expect_port"].as_u64().unwrap();
            assert_eq!(machine_url(&db, svc), want_url, "case `{name}`");
            let named = match svc {
                CoreService::Weaviate => machine_weaviate_url(&db),
                CoreService::Ollama => machine_ollama_url(&db),
                CoreService::CodeEmbed => machine_code_embed_url(&db),
            };
            assert_eq!(named, want_url, "case `{name}` (named resolver)");
            assert_eq!(machine_url_from_disk(svc), want_url, "case `{name}` (from disk)");
            assert_eq!(machine_port(&db, svc) as u64, want_port, "case `{name}`");
            assert_eq!(machine_port_from_disk(svc) as u64, want_port, "case `{name}` (from disk)");
            if let Some(g) = case.get("expect_grpc_port").and_then(|g| g.as_u64()) {
                assert_eq!(machine_grpc_port(&db) as u64, g, "case `{name}`");
                assert_eq!(machine_grpc_port_from_disk() as u64, g, "case `{name}` (from disk)");
            }
        }
    }

    /// Red-proof (2): every pre-v0.2.97 leg set at once — `VCT_WEAVIATE_URL`,
    /// `WEAVIATE_URL`, a services.toml `parallel` row, an app_state
    /// `port_override` — and the machine resolver answers the ROW.
    #[test]
    fn machine_weaviate_url_is_the_row_whatever_else_is_set() {
        use crate::services::adoption::{self, AdoptionMode, AdoptionState, ServiceAdoption};
        let _g = crate::test_env::state_dir_guard_with(&[
            (STATEMENT_ENV, Some("http://statement.invalid:1")),
            ("WEAVIATE_URL", Some("http://transport.invalid:2")),
        ]);
        let db = Db::open().unwrap();
        let mut state = AdoptionState::default();
        state.upsert(ServiceAdoption {
            name: "weaviate".into(),
            mode: AdoptionMode::Parallel,
            external_url: None,
            parallel_port: Some(18082),
            container_name: None,
        });
        adoption::write(&state).unwrap();
        db.app_state_set(APP_STATE_KEY_WEAVIATE_PORT, "18083").unwrap();
        let mut row = ServiceEndpointRow::new("weaviate", EndpointMode::AdoptedContainer, "localhost", 18090);
        row.grpc_port = Some(50060);
        row.container_name = Some("their_weaviate".into());
        db.service_endpoint_seed_for_tests(&row).unwrap();

        assert_eq!(machine_weaviate_url(&db), "http://localhost:18090");
        assert_eq!(machine_port(&db, CoreService::Weaviate), 18090);
        assert_eq!(machine_grpc_port(&db), 50060);
    }

    /// Red-proof (3): the Db-less reader reads the ROW from launcher.db
    /// through a read-only connection — not an app_state key in the same DB.
    #[test]
    fn port_from_disk_reads_the_row() {
        let _g = crate::test_env::state_dir_guard();
        assert_eq!(machine_port_from_disk(CoreService::CodeEmbed), DEFAULT_CODE_EMBED_PORT);
        let db = Db::open().unwrap();
        db.app_state_set(APP_STATE_KEY_CODE_EMBED_PORT, "21441").unwrap();
        assert_eq!(
            machine_port_from_disk(CoreService::CodeEmbed),
            DEFAULT_CODE_EMBED_PORT,
            "the retired app_state key is not a leg"
        );
        db.service_endpoint_seed_for_tests(&ServiceEndpointRow::new(
            "code_embed",
            EndpointMode::VcoManaged,
            "127.0.0.1",
            21440,
        ))
        .unwrap();
        assert_eq!(machine_port_from_disk(CoreService::CodeEmbed), 21440);
        assert_eq!(machine_url_from_disk(CoreService::CodeEmbed), "http://127.0.0.1:21440");
        assert_eq!(machine_port(&db, CoreService::CodeEmbed), 21440);
    }

    /// A launcher.db from before migration 047 (no table) is "no row": the
    /// default, never an error or a panic.
    #[test]
    fn a_pre_047_db_on_disk_answers_the_default() {
        let g = crate::test_env::state_dir_guard();
        let conn = rusqlite::Connection::open(g.path().join("launcher.db")).unwrap();
        conn.execute_batch("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT)").unwrap();
        drop(conn);
        assert_eq!(machine_port_from_disk(CoreService::Ollama), DEFAULT_OLLAMA_PORT);
        assert_eq!(machine_url_from_disk(CoreService::Weaviate), "http://localhost:8081");
        assert_eq!(machine_grpc_port_from_disk(), DEFAULT_WEAVIATE_GRPC_PORT);
    }

    #[test]
    fn port_of_url_handles_ipv6_and_scheme_defaults() {
        assert_eq!(port_of_url("http://[::1]:8081"), Some(8081));
        assert_eq!(port_of_url("http://[::1]"), Some(80));
        assert_eq!(port_of_url("https://weaviate.example.com/v1"), Some(443));
        assert_eq!(port_of_url("localhost"), None);
        assert_eq!(weaviate_port_for_url("nonsense"), DEFAULT_WEAVIATE_PORT);
    }

    /// The client resolver's fallback (below its interim env legs) is the row.
    #[test]
    fn client_url_falls_to_the_row() {
        let _g = crate::test_env::state_dir_guard_with(&[(STATEMENT_ENV, None), ("WEAVIATE_URL", None)]);
        let db = Db::open_in_memory().unwrap();
        let mut row = ServiceEndpointRow::new("weaviate", EndpointMode::AdoptedExternal, "weaviate.lan", 8090);
        row.grpc_port = Some(50051);
        db.service_endpoint_seed_for_tests(&row).unwrap();
        assert_eq!(client_weaviate_url(&db), "http://weaviate.lan:8090");
    }

    #[test]
    fn retired_env_is_reported_for_the_startup_warning() {
        let _g = crate::test_env::state_dir_guard_with(&[
            (STATEMENT_ENV, Some("http://old:1")),
            (OLLAMA_STATEMENT_ENV, Some("  ")),
        ]);
        assert_eq!(retired_endpoint_env_present(), vec![(STATEMENT_ENV, "http://old:1".to_string())]);
    }
}
