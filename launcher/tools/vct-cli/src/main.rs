//! vct-cli — command-line interface for the VCT Launcher (P6).
//!
//! Talks to the launcher's local hub HTTP server (default port 7700,
//! discoverable via `~/.vct/hub.port`). Every operation that the GUI
//! exposes via Tauri commands has a corresponding hub HTTP endpoint
//! in `launcher/src-tauri/src/hub/cli_api.rs` (and friends).
//!
//! Why a separate binary?
//!   - The launcher GUI is Tauri (heavy native deps). Pulling Tauri
//!     into a CLI tool is wasteful. A small reqwest client is enough.
//!   - Built independently: `cargo build --release --bin vct-cli` from
//!     `launcher/tools/vct-cli/`. The Tauri app build is unaffected.
//!
//! The binary is `vct-cli` (v0.2.97). It was `vco` before, which is also the
//! Python console script `pyproject.toml` registers (`vco doctor`,
//! `vco project move`, ...); see the `[[bin]]` comment in Cargo.toml.
//!   - Same Rust toolchain as the launcher, so we can share types
//!     with copy-paste comments rather than a shared crate (the
//!     surface here is tiny).

use anyhow::{anyhow, Context, Result};
use clap::{Parser, Subcommand};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

const DEFAULT_PORT: u16 = 7700;

// ─── Top-level CLI ──────────────────────────────────────────────────────

#[derive(Parser)]
#[command(
    name = "vct-cli",
    version,
    about = "vibecoded-orchestrator CLI — power-user / CI escape hatch.",
    long_about = "\
Talks to the running VCT Launcher's local hub server (default 127.0.0.1:7700).
The launcher must be running for these commands to succeed; if it is not,
start it via the system tray or the desktop app first.

Note: this CLI was named `vco` until v0.2.97 (and `vct` before v0.1.0). It is
`vct-cli` now because `vco` is the orchestrator's Python CLI (`vco doctor`,
`vco project move`, ...) and `vct` is the secrets tool (tools/vct-secrets/vct)."
)]
struct Cli {
    #[command(subcommand)]
    command: TopCommand,

    /// Override the launcher hub port (env: VCT_HUB_PORT, default 7700).
    #[arg(long, global = true)]
    port: Option<u16>,
}

#[derive(Subcommand)]
enum TopCommand {
    /// Project lifecycle (create / list / rename / delete / switch).
    Project {
        #[command(subcommand)]
        cmd: ProjectCmd,
    },
    /// Modules — list catalog, see installed.
    Module {
        #[command(subcommand)]
        cmd: ModuleCmd,
    },
    /// Audit log access (list / filter).
    Audit {
        #[command(subcommand)]
        cmd: AuditCmd,
    },
    /// License inspection / activation.
    License {
        #[command(subcommand)]
        cmd: LicenseCmd,
    },
    /// Hooks (registered events on a project).
    Hooks {
        #[command(subcommand)]
        cmd: HooksCmd,
    },
    /// Telemetry consent.
    Telemetry {
        #[command(subcommand)]
        cmd: TelemetryCmd,
    },
    /// Hub/launcher health and metadata.
    Hub {
        #[command(subcommand)]
        cmd: HubCmd,
    },
    /// Knowledge graph search and collection inspection.
    Kg {
        #[command(subcommand)]
        cmd: KgCmd,
    },
    /// Code graph search and collection inspection.
    Codegraph {
        #[command(subcommand)]
        cmd: CodegraphCmd,
    },
}

// ─── Subcommands ────────────────────────────────────────────────────────

#[derive(Subcommand)]
enum ProjectCmd {
    /// List all registered projects.
    List,
    /// Create a new project from a folder on disk.
    Create {
        #[arg(long)]
        name: String,
        #[arg(long)]
        path: PathBuf,
        /// Project host (base or mao). Defaults to base.
        #[arg(long, default_value = "base")]
        host: String,
    },
    /// Rename a project (id or slug).
    Rename {
        id_or_slug: String,
        new_name: String,
    },
    /// Delete a project (id or slug).
    Delete { id_or_slug: String },
    /// Print info for a single project (id or slug).
    Show { id_or_slug: String },
}

#[derive(Subcommand)]
enum ModuleCmd {
    /// List the module catalog.
    List,
    /// List installed modules for a project.
    Installed {
        /// Project id or slug.
        project: String,
    },
}

#[derive(Subcommand)]
enum AuditCmd {
    /// List audit events. Optional --project (slug or id), --since (ms epoch),
    /// --limit (default 200, max 1000).
    List {
        #[arg(long)]
        project: Option<String>,
        #[arg(long)]
        since: Option<i64>,
        #[arg(long)]
        limit: Option<u32>,
    },
}

#[derive(Subcommand)]
enum LicenseCmd {
    /// Print current tier + cache info.
    Status,
    /// Activate a license key.
    Activate { key: String },
    /// Clear the local license key.
    Deactivate,
}

#[derive(Subcommand)]
enum HooksCmd {
    /// List hooks for a project (id or slug).
    List { project: String },
    /// Enable a hook by numeric id — restores its `.claude/settings.json`
    /// entry (v0.2.91 wave 5: a real edit via the hub's hook-enforcement
    /// bridge, not a DB-only mirror flag).
    Enable {
        hook_id: i64,
        /// Owning project (id or slug), REQUIRED: toggling a hook edits
        /// that project's .claude/settings.json, so the hub must know
        /// which project's file to change.
        #[arg(long)]
        project: String,
    },
    /// Disable a hook by numeric id — removes its `.claude/settings.json`
    /// entry (v0.2.91 wave 5: a real edit via the hub's hook-enforcement
    /// bridge, not a DB-only mirror flag).
    Disable {
        hook_id: i64,
        /// Owning project (id or slug), REQUIRED: toggling a hook edits
        /// that project's .claude/settings.json, so the hub must know
        /// which project's file to change.
        #[arg(long)]
        project: String,
    },
}

#[derive(Subcommand)]
enum TelemetryCmd {
    /// Show current consent state.
    Status,
    /// Grant telemetry consent.
    On,
    /// Revoke telemetry consent.
    Off,
    /// Print the events waiting in ~/.vibecoded/telemetry_pending.jsonl —
    /// what an opted-in install would have uploaded. Reads the file
    /// directly, so the launcher does not need to be running.
    Pending,
}

#[derive(Subcommand)]
enum HubCmd {
    /// Hub health check.
    Health,
    /// Print the hub URL the CLI is talking to.
    Url,
}

#[derive(Subcommand)]
enum KgCmd {
    /// List orchestrator-shaped KG collections detected on the local
    /// Weaviate instance.
    Collections,
    /// Search across one or more KG collections.
    ///
    /// If `--collections` is omitted the hub auto-detects every
    /// orchestrator-shaped class on Weaviate (those with
    /// `title`/`node_type`/`tags`/`typed_links`).
    Search {
        /// Search query (free text).
        query: String,
        /// Comma-separated collections (default: auto-detect).
        #[arg(long, value_delimiter = ',')]
        collections: Option<Vec<String>>,
        /// Project context (id or slug). Required — used to enforce
        /// per-collection ACLs and attribute the audit log entry.
        #[arg(long)]
        project: String,
        /// Max hits per collection (clamped to 100 by hub).
        #[arg(long, default_value_t = 20)]
        limit: u32,
    },
}

#[derive(Subcommand)]
enum CodegraphCmd {
    /// List code-graph collections detected on the local Weaviate
    /// instance (canonical 5 + per-project namespaced variants).
    Collections,
    /// Search across one or more code-graph collections.
    Search {
        /// Search query (free text).
        query: String,
        /// Comma-separated collections (default: auto-detect).
        #[arg(long, value_delimiter = ',')]
        collections: Option<Vec<String>>,
        /// Project context (id or slug). Required for audit
        /// attribution.
        #[arg(long)]
        project: String,
        /// Restrict to a subset:
        ///   * `all` (default) — every code-graph class
        ///   * `code` — CodeModule / CodeClass / CodeFunction only
        ///   * `interaction` — CodeAPI / CodeInteraction only
        #[arg(long, default_value = "all")]
        scope: String,
        #[arg(long, default_value_t = 20)]
        limit: u32,
    },
}

// ─── Hub client ─────────────────────────────────────────────────────────

struct Hub {
    base: String,
    client: reqwest::blocking::Client,
    /// Bearer token read from `<vct_root_dir>/hub.token`. `None` when
    /// the file is missing or empty — we still try the request (so
    /// `health` and other unauthenticated endpoints keep working) but
    /// authenticated endpoints will get a 401 from the server.
    token: Option<String>,
}

impl Hub {
    fn new(port_override: Option<u16>) -> Result<Self> {
        let port = resolve_port(port_override)?;
        let base = format!("http://127.0.0.1:{}/api/v1", port);
        // MUST MATCH `vct_launcher_core::services::loopback_http` (the one
        // loopback-client rule; this crate is a separate workspace without
        // that dependency, and its client is blocking): the request carries
        // the hub bearer token to 127.0.0.1, so no proxy — reqwest does not
        // exempt loopback from HTTP_PROXY — and no redirects. Pinned by
        // `tests::the_hub_client_never_goes_through_a_proxy`.
        let client = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(15))
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .context("build http client")?;
        let token = resolve_token();
        Ok(Self { base, client, token })
    }

    fn url(&self) -> &str {
        &self.base
    }

    /// Apply the `Authorization: Bearer <token>` header if we have one.
    /// Centralised here so every method gets it consistently. Takes the
    /// token EXPLICITLY (rather than reading `self.token`) — that is the
    /// seam the stale-env retry needs so its second attempt can present
    /// the on-disk token instead.
    fn with_auth_token(
        b: reqwest::blocking::RequestBuilder,
        token: Option<&str>,
    ) -> reqwest::blocking::RequestBuilder {
        if let Some(t) = token {
            b.bearer_auth(t)
        } else {
            b
        }
    }

    /// Send `build(token)` and return `(status, body)`, retrying ONCE with
    /// the on-disk token when the hub PROVABLY refuses (401/403) a
    /// `$VCT_HUB_TOKEN` that is provably stale.
    ///
    /// v0.2.91 (WP-D item 4) — MUST MATCH the Python SSOT
    /// `vco_lib/project_config.py::_stale_env_token_fallback` +
    /// `_get_with_401_retry` and the sh / ps1 mirrors. Failure of the
    /// extra attempt returns the ORIGINAL `(status, body)` verbatim, so
    /// every error message this CLI prints is byte-identical to
    /// pre-v0.2.91. Bounded: at most one extra request, never a loop.
    fn send_with_stale_token_retry(
        &self,
        build: impl Fn(Option<&str>) -> reqwest::blocking::RequestBuilder,
    ) -> Result<(reqwest::StatusCode, String)> {
        let resp = build(self.token.as_deref())
            .send()
            .map_err(|e| anyhow!("Cannot reach launcher hub: {}. Is the launcher running?", e))?;
        let status = resp.status();
        let body = resp.text().context("read response body")?;
        if !is_auth_refusal(status) {
            return Ok((status, body));
        }
        let fallback = match stale_env_fallback_token() {
            Some(t) => t,
            None => return Ok((status, body)),
        };
        let retry = match build(Some(&fallback)).send() {
            Ok(r) => r,
            // The extra attempt could not complete — keep today's path.
            Err(_) => return Ok((status, body)),
        };
        let retry_status = retry.status();
        if !retry_answer_is_definitive(retry_status) {
            return Ok((status, body));
        }
        let retry_body = match retry.text() {
            Ok(b) => b,
            Err(_) => return Ok((status, body)),
        };
        warn_stale_env_token();
        Ok((retry_status, retry_body))
    }

    fn get_json<T: for<'de> Deserialize<'de>>(&self, path: &str) -> Result<T> {
        let url = format!("{}{}", self.base, path);
        let (status, body) = self.send_with_stale_token_retry(|tok| {
            Self::with_auth_token(self.client.get(&url), tok)
        })?;
        if !status.is_success() {
            return Err(anyhow!("hub error {}: {}", status, body));
        }
        serde_json::from_str(&body).with_context(|| format!("decode {}", path))
    }

    fn post_json<B: Serialize, T: for<'de> Deserialize<'de>>(
        &self,
        path: &str,
        body: &B,
    ) -> Result<T> {
        let url = format!("{}{}", self.base, path);
        let (status, resp_body) = self.send_with_stale_token_retry(|tok| {
            Self::with_auth_token(self.client.post(&url).json(body), tok)
        })?;
        if !status.is_success() {
            return Err(anyhow!("hub error {}: {}", status, resp_body));
        }
        serde_json::from_str(&resp_body).with_context(|| format!("decode {}", path))
    }

    fn patch_json<B: Serialize, T: for<'de> Deserialize<'de>>(
        &self,
        path: &str,
        body: &B,
    ) -> Result<T> {
        let url = format!("{}{}", self.base, path);
        let (status, resp_body) = self.send_with_stale_token_retry(|tok| {
            Self::with_auth_token(self.client.patch(&url).json(body), tok)
        })?;
        if !status.is_success() {
            return Err(anyhow!("hub error {}: {}", status, resp_body));
        }
        serde_json::from_str(&resp_body).with_context(|| format!("decode {}", path))
    }

    fn delete_json<T: for<'de> Deserialize<'de>>(&self, path: &str) -> Result<T> {
        let url = format!("{}{}", self.base, path);
        let (status, body) = self.send_with_stale_token_retry(|tok| {
            Self::with_auth_token(self.client.delete(&url), tok)
        })?;
        if !status.is_success() {
            return Err(anyhow!("hub error {}: {}", status, body));
        }
        serde_json::from_str(&body).with_context(|| format!("decode {}", path))
    }
}

/// Read the auth token from `<VCT_STATE_DIR or ~/.vct>/hub.token`.
///
/// Returns None if the file is missing/empty. We don't fail the
/// command here — sending a request without the header lets the
/// server return a precise 401 (which we then surface as the hub
/// error). Empty-vs-missing token: same outcome from the server's
/// view, so we collapse both to None at the client.
fn resolve_token() -> Option<String> {
    // Honour VCT_HUB_TOKEN (set by tests / dev harnesses) for the
    // same reason resolve_port honours VCT_HUB_PORT — no need to
    // round-trip through a tempdir VCT_STATE_DIR if a test just
    // wants to inject a known token.
    //
    // v0.2.91 (WP-D item 4): the pin still wins on every FIRST attempt.
    // It is only set aside AFTER the hub provably refuses it (401/403),
    // by the one-shot retry in `Hub::send_with_stale_token_retry`; set
    // `VCT_HUB_TOKEN_STRICT=1` to disable even that, so a harness that
    // pins a deliberately-wrong token still observes the refusal.
    if let Ok(t) = std::env::var("VCT_HUB_TOKEN") {
        let trimmed = t.trim();
        if !trimmed.is_empty() {
            return Some(trimmed.to_string());
        }
    }
    on_disk_hub_token()
}

/// The on-disk token, IGNORING `$VCT_HUB_TOKEN`.
///
/// Standard path: read from disk, mirroring server.rs's
/// `auth::write_token_file`. We honour VCT_STATE_DIR the same way
/// `resolve_port` does so dev launchers / tests stay isolated from the
/// production state dir. This CLI only ever calls GLOBAL-token routes
/// (`/cli/*`, `/projects`, `/modules`, …), so there is no scoped
/// `hub.token.<project_id>` variant to resolve here — unlike the sh /
/// ps1 / Python resolvers, which do hit `/env` + `/config`.
fn on_disk_hub_token() -> Option<String> {
    let state_dir = std::env::var("VCT_STATE_DIR")
        .ok()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .or_else(|| {
            directories::UserDirs::new().map(|d| d.home_dir().join(".vct"))
        })?;
    let path = state_dir.join("hub.token");
    let raw = std::fs::read_to_string(&path).ok()?;
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

/// The ONE definitive line printed after a stale env token is overridden.
/// Byte-identical to `vco_lib.project_config.STALE_ENV_TOKEN_MESSAGE` and
/// to the sh / ps1 / wrapper mirrors (locked by
/// `tests/test_stale_env_token_parity_v0291.py`).
const STALE_ENV_TOKEN_MESSAGE: &str =
    "stale VCT_HUB_TOKEN in env overridden by on-disk hub.token — \
run `unset VCT_HUB_TOKEN` or open a new shell";

/// PROVABLE credential refusals — the ONLY trigger for the fallback.
/// 401 = the bearer matched nothing; 403 = the bearer is real but refused
/// on this route. Anything else is not a credential problem.
fn is_auth_refusal(status: reqwest::StatusCode) -> bool {
    status.as_u16() == 401 || status.as_u16() == 403
}

/// May a stale-env RETRY's answer be ADOPTED (and the definitive line printed)?
///
/// Only when it PROVES the fallback credential was accepted: `2xx`, or `404` —
/// the hub answers "not found" only AFTER its auth middleware accepted the
/// bearer, so it is a post-auth answer just like a 200.
///
/// Everything else proves nothing about the credential. v0.2.91 wave-3
/// (MINOR-1): before this, ANY non-401/403 answer was adopted, so a 401
/// followed by a 5xx printed "stale VCT_HUB_TOKEN…" and surfaced
/// `hub error 503` in place of the truthful `hub error 401`.
///
/// MUST MATCH `vco_lib/project_config.py::_retry_answer_is_definitive` and the
/// sh / ps1 mirrors.
fn retry_answer_is_definitive(status: reqwest::StatusCode) -> bool {
    status.is_success() || status.as_u16() == 404
}

/// Decide whether a provably-refused request may be retried once with the
/// on-disk token, and return that token when it may.
///
/// MUST MATCH `vco_lib/project_config.py::_stale_env_token_fallback`
/// (the SSOT) and the sh / ps1 mirrors. Rules, in order:
///   1. `VCT_HUB_TOKEN_STRICT=1`      → None (the pin is authoritative)
///   2. `VCT_HUB_TOKEN` unset/empty   → None (nothing was pinned)
///   3. no readable on-disk token     → None (nothing better to try)
///   4. on-disk == env (trimmed)      → None (the pin is not stale)
fn stale_env_fallback_token() -> Option<String> {
    if std::env::var("VCT_HUB_TOKEN_STRICT")
        .map(|v| v.trim() == "1")
        .unwrap_or(false)
    {
        return None;
    }
    let env_tok = std::env::var("VCT_HUB_TOKEN").ok()?;
    let env_tok = env_tok.trim();
    if env_tok.is_empty() {
        return None;
    }
    let disk_tok = on_disk_hub_token()?;
    if disk_tok == env_tok {
        return None;
    }
    Some(disk_tok)
}

/// Emit the definitive line once per process (best-effort, stderr).
fn warn_stale_env_token() {
    use std::sync::atomic::{AtomicBool, Ordering};
    static WARNED: AtomicBool = AtomicBool::new(false);
    if !WARNED.swap(true, Ordering::SeqCst) {
        eprintln!("vct-cli: {}", STALE_ENV_TOKEN_MESSAGE);
    }
}

/// THE hub-port value rule (R7b F9), for the env and the WHOLE file alike:
/// trim C-locale whitespace at the ends, then ASCII `[0-9]{1,5}` in
/// 1..=65535 — no sign (`str::parse::<u16>` accepts `+7822`), no `_`, no
/// non-ASCII numerals, no internal whitespace. MUST MATCH
/// `vct_launcher_core::services::hub_port::parse_hub_port` (this workspace
/// cannot depend on it) and `vco_lib.hub_ensure.parse_hub_port`; the shared
/// table `tests/fixtures/hub_port_cases.json` runs through both.
fn valid_hub_port(raw: &str) -> Option<u16> {
    let value = raw.trim_matches(|c: char| matches!(c, ' ' | '\t' | '\n' | '\x0b' | '\x0c' | '\r'));
    if value.is_empty() || value.len() > 5 || !value.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    value.parse::<u32>().ok().filter(|p| (1..=65535).contains(p)).map(|p| p as u16)
}

/// The hub-port client ladder. MUST MATCH the ONE readers
/// `vco_lib::hub_ensure::resolve_hub_port` (Python) and
/// `vct_launcher_core::services::hub_port` (Rust) — vct-cli is its OWN cargo
/// workspace and cannot depend on vct-launcher-core, so this is a B-tier
/// mirror: the shared case table `tests/fixtures/hub_port_cases.json` runs
/// through this function (`hub_port_ladder_matches_the_parity_table` below)
/// and through the Python reader + sh/ps1 clients
/// (`tests/test_v0297_hub_port_clients.py`).
///
/// Owner ruling 2026-09-24: a set-but-INVALID `VCT_HUB_PORT` (non-numeric, 0,
/// > 65535) falls through to `<state dir>/hub.port` — the file names the
/// RUNNING hub — and only then to 7700. The state dir honours
/// `$VCT_STATE_DIR` (else `~/.vct`) the same way `on_disk_hub_token` above
/// does, so dev launchers / tests stay isolated from the production state
/// dir.
fn resolve_port(override_port: Option<u16>) -> Result<u16> {
    if let Some(p) = override_port {
        return Ok(p);
    }
    if let Ok(s) = std::env::var("VCT_HUB_PORT") {
        if let Some(p) = valid_hub_port(&s) {
            return Ok(p);
        }
    }
    // Read <state dir>/hub.port if present (the launcher writes it on
    // startup).
    let state_dir = std::env::var("VCT_STATE_DIR")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .map(PathBuf::from)
        .or_else(|| directories::UserDirs::new().map(|d| d.home_dir().join(".vct")));
    if let Some(d) = state_dir {
        if let Ok(content) = std::fs::read_to_string(d.join("hub.port")) {
            if let Some(parsed) = valid_hub_port(&content) {
                return Ok(parsed);
            }
        }
    }
    Ok(DEFAULT_PORT)
}

// ─── Output helpers ─────────────────────────────────────────────────────

fn print_json<T: Serialize>(v: &T) -> Result<()> {
    println!("{}", serde_json::to_string_pretty(v).context("pretty-print JSON")?);
    Ok(())
}

// ─── Main ───────────────────────────────────────────────────────────────

fn main() {
    if let Err(e) = run() {
        eprintln!("vct-cli: {}", e);
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let cli = Cli::parse();
    // A local file read: it must answer with the launcher down, so it never
    // resolves a hub port or token.
    if matches!(cli.command, TopCommand::Telemetry { cmd: TelemetryCmd::Pending }) {
        return print_json(&telemetry_pending(&telemetry_pending_path()?)?);
    }
    let hub = Hub::new(cli.port)?;

    match cli.command {
        TopCommand::Project { cmd } => project(&hub, cmd),
        TopCommand::Module { cmd } => module(&hub, cmd),
        TopCommand::Audit { cmd } => audit(&hub, cmd),
        TopCommand::License { cmd } => license(&hub, cmd),
        TopCommand::Hooks { cmd } => hooks(&hub, cmd),
        TopCommand::Telemetry { cmd } => telemetry(&hub, cmd),
        TopCommand::Hub { cmd } => hub_cmd(&hub, cmd),
        TopCommand::Kg { cmd } => kg(&hub, cmd),
        TopCommand::Codegraph { cmd } => codegraph(&hub, cmd),
    }
}

// ─── Command handlers ───────────────────────────────────────────────────

fn project(hub: &Hub, cmd: ProjectCmd) -> Result<()> {
    match cmd {
        ProjectCmd::List => {
            let v: serde_json::Value = hub.get_json("/projects")?;
            print_json(&v)
        }
        ProjectCmd::Create { name, path, host } => {
            let canonical = path.canonicalize().context("canonicalize path")?;
            let body = serde_json::json!({
                "name": name,
                "folder_path": canonical.to_string_lossy(),
                "host": host,
            });
            let v: serde_json::Value = hub.post_json("/cli/projects", &body)?;
            print_json(&v)
        }
        ProjectCmd::Rename { id_or_slug, new_name } => {
            let body = serde_json::json!({ "new_name": new_name });
            let v: serde_json::Value = hub.patch_json(&format!("/cli/projects/{}", id_or_slug), &body)?;
            print_json(&v)
        }
        ProjectCmd::Delete { id_or_slug } => {
            let v: serde_json::Value = hub.delete_json(&format!("/cli/projects/{}", id_or_slug))?;
            print_json(&v)
        }
        ProjectCmd::Show { id_or_slug } => {
            // Try id first, then by-slug.
            let v: Result<serde_json::Value> = hub.get_json(&format!("/projects/{}", id_or_slug));
            let v = match v {
                Ok(x) => x,
                Err(_) => hub.get_json(&format!("/projects/by-slug/{}", id_or_slug))?,
            };
            print_json(&v)
        }
    }
}

fn module(hub: &Hub, cmd: ModuleCmd) -> Result<()> {
    match cmd {
        ModuleCmd::List => {
            let v: serde_json::Value = hub.get_json("/modules/catalog")?;
            print_json(&v)
        }
        ModuleCmd::Installed { project } => {
            // Resolve slug to id if it's not already a UUID-shaped string.
            let pid = resolve_project_id(hub, &project)?;
            let v: serde_json::Value =
                hub.get_json(&format!("/modules/installed?project_id={}", pid))?;
            print_json(&v)
        }
    }
}

fn audit(hub: &Hub, cmd: AuditCmd) -> Result<()> {
    match cmd {
        AuditCmd::List { project, since, limit } => {
            let mut q = vec![];
            if let Some(p) = project.as_ref() {
                if looks_like_uuid(p) {
                    q.push(format!("project_id={}", p));
                } else {
                    q.push(format!("project_slug={}", p));
                }
            }
            if let Some(s) = since {
                q.push(format!("since_ms={}", s));
            }
            if let Some(l) = limit {
                q.push(format!("limit={}", l));
            }
            let qs = if q.is_empty() { String::new() } else { format!("?{}", q.join("&")) };
            let v: serde_json::Value = hub.get_json(&format!("/cli/audit{}", qs))?;
            print_json(&v)
        }
    }
}

fn license(hub: &Hub, cmd: LicenseCmd) -> Result<()> {
    match cmd {
        LicenseCmd::Status => {
            let v: serde_json::Value = hub.get_json("/cli/license")?;
            print_json(&v)
        }
        LicenseCmd::Activate { key } => {
            let body = serde_json::json!({ "key": key });
            let v: serde_json::Value = hub.post_json("/cli/license/activate", &body)?;
            print_json(&v)
        }
        LicenseCmd::Deactivate => {
            let v: serde_json::Value =
                hub.post_json("/cli/license/deactivate", &serde_json::json!({}))?;
            print_json(&v)
        }
    }
}

fn hooks(hub: &Hub, cmd: HooksCmd) -> Result<()> {
    match cmd {
        HooksCmd::List { project } => {
            let v: serde_json::Value = hub.get_json(&format!("/cli/hooks/{}", project))?;
            print_json(&v)
        }
        HooksCmd::Enable { hook_id, project } => {
            let body = serde_json::json!({ "project_id": project, "enabled": true });
            let v: serde_json::Value =
                hub.patch_json(&format!("/cli/hooks/{}/enabled", hook_id), &body)?;
            print_json(&v)
        }
        HooksCmd::Disable { hook_id, project } => {
            let body = serde_json::json!({ "project_id": project, "enabled": false });
            let v: serde_json::Value =
                hub.patch_json(&format!("/cli/hooks/{}/enabled", hook_id), &body)?;
            print_json(&v)
        }
    }
}

fn telemetry(hub: &Hub, cmd: TelemetryCmd) -> Result<()> {
    match cmd {
        TelemetryCmd::Status => {
            let v: serde_json::Value = hub.get_json("/cli/telemetry")?;
            print_json(&v)
        }
        TelemetryCmd::On => {
            let body = serde_json::json!({ "consent": true });
            let v: serde_json::Value = hub.post_json("/cli/telemetry/consent", &body)?;
            print_json(&v)
        }
        TelemetryCmd::Off => {
            let body = serde_json::json!({ "consent": false });
            let v: serde_json::Value = hub.post_json("/cli/telemetry/consent", &body)?;
            print_json(&v)
        }
        TelemetryCmd::Pending => print_json(&telemetry_pending(&telemetry_pending_path()?)?),
    }
}

/// Where opted-in telemetry waits while no upload endpoint is deployed.
/// MUST MATCH the one writer, `VCThelpers/telemetry/uploader.py`
/// (`Path.home() / ".vibecoded" / _PENDING_FILE`); pinned by
/// `tests/test_v0297_cli_program_names.py`.
const TELEMETRY_PENDING_DIR: &str = ".vibecoded";
const TELEMETRY_PENDING_FILE: &str = "telemetry_pending.jsonl";

fn telemetry_pending_path() -> Result<PathBuf> {
    directories::UserDirs::new()
        .map(|d| {
            d.home_dir()
                .join(TELEMETRY_PENDING_DIR)
                .join(TELEMETRY_PENDING_FILE)
        })
        .ok_or_else(|| anyhow!("cannot resolve the home directory"))
}

/// The pending events, one JSON object per line. A missing file is the
/// normal state (telemetry off, or nothing recorded yet), not an error. A
/// line that is not JSON is counted, never dropped silently.
fn telemetry_pending(path: &std::path::Path) -> Result<serde_json::Value> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(e) => return Err(e).with_context(|| format!("read {}", path.display())),
    };
    let mut events = Vec::new();
    let mut unparsed_lines = 0u64;
    for line in text.lines().map(str::trim).filter(|l| !l.is_empty()) {
        match serde_json::from_str::<serde_json::Value>(line) {
            Ok(v) => events.push(v),
            Err(_) => unparsed_lines += 1,
        }
    }
    Ok(serde_json::json!({
        "path": path.display().to_string(),
        "exists": path.is_file(),
        "count": events.len(),
        "unparsed_lines": unparsed_lines,
        "events": events,
    }))
}

fn hub_cmd(hub: &Hub, cmd: HubCmd) -> Result<()> {
    match cmd {
        HubCmd::Health => {
            let v: serde_json::Value = hub.get_json("/health")?;
            print_json(&v)
        }
        HubCmd::Url => {
            println!("{}", hub.url());
            Ok(())
        }
    }
}

fn kg(hub: &Hub, cmd: KgCmd) -> Result<()> {
    match cmd {
        KgCmd::Collections => {
            let v: serde_json::Value = hub.get_json("/cli/kg/collections")?;
            print_json(&v)
        }
        KgCmd::Search {
            query,
            collections,
            project,
            limit,
        } => {
            let pid = resolve_project_id(hub, &project)?;
            let mut body = serde_json::json!({
                "project_id": pid,
                "query": query,
                "limit": limit,
            });
            if let Some(cs) = collections {
                body["collections"] = serde_json::json!(cs);
            }
            let v: serde_json::Value = hub.post_json("/cli/kg/search", &body)?;
            print_json(&v)
        }
    }
}

fn codegraph(hub: &Hub, cmd: CodegraphCmd) -> Result<()> {
    match cmd {
        CodegraphCmd::Collections => {
            let v: serde_json::Value = hub.get_json("/cli/codegraph/collections")?;
            print_json(&v)
        }
        CodegraphCmd::Search {
            query,
            collections,
            project,
            scope,
            limit,
        } => {
            let pid = resolve_project_id(hub, &project)?;
            let mut body = serde_json::json!({
                "project_id": pid,
                "query": query,
                "scope": scope,
                "limit": limit,
            });
            if let Some(cs) = collections {
                body["collections"] = serde_json::json!(cs);
            }
            let v: serde_json::Value = hub.post_json("/cli/codegraph/search", &body)?;
            print_json(&v)
        }
    }
}

// ─── Internals ──────────────────────────────────────────────────────────

fn looks_like_uuid(s: &str) -> bool {
    // Cheap heuristic: 36 chars with dashes at the right positions.
    s.len() == 36 && s.chars().filter(|&c| c == '-').count() == 4
}

fn resolve_project_id(hub: &Hub, id_or_slug: &str) -> Result<String> {
    if looks_like_uuid(id_or_slug) {
        return Ok(id_or_slug.to_string());
    }
    let v: serde_json::Value = hub.get_json(&format!("/projects/by-slug/{}", id_or_slug))?;
    v.get("id")
        .and_then(|x| x.as_str())
        .map(|s| s.to_string())
        .ok_or_else(|| anyhow!("could not resolve slug '{}' to a project id", id_or_slug))
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::CommandFactory;
    use std::collections::BTreeMap;

    /// `cli_verbs.json` is the command tree other checks read without
    /// building this crate (tests/test_v0297_cli_program_names.py uses it to
    /// check every `vct-cli <verb>` and `vco <verb>` in shipped text). It is
    /// only worth reading if it IS the tree clap builds — this proves that.
    #[test]
    fn verb_table_matches_the_parser() {
        let table: serde_json::Value =
            serde_json::from_str(include_str!("../cli_verbs.json")).expect("cli_verbs.json parses");
        let mut cmd = Cli::command();
        cmd.build();
        assert_eq!(
            cmd.get_name(),
            env!("CARGO_BIN_NAME"),
            "clap name != Cargo [[bin]] name"
        );
        assert_eq!(table["program"].as_str(), Some(cmd.get_name()));
        let parsed: BTreeMap<String, Vec<String>> = cmd
            .get_subcommands()
            .map(|verb| {
                let mut subs: Vec<String> = verb
                    .get_subcommands()
                    .map(|s| s.get_name().to_string())
                    .collect();
                subs.sort();
                (verb.get_name().to_string(), subs)
            })
            .collect();
        let committed: BTreeMap<String, Vec<String>> =
            serde_json::from_value(table["verbs"].clone()).expect("verbs is {verb: [sub, ...]}");
        assert_eq!(
            parsed, committed,
            "launcher/tools/vct-cli/cli_verbs.json no longer matches the clap tree — update it"
        );
    }

    #[test]
    fn telemetry_pending_reads_every_event_and_counts_bad_lines() {
        let dir = std::env::temp_dir().join(format!("vct-cli-pending-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(TELEMETRY_PENDING_FILE);
        std::fs::write(&path, "{\"event\":\"a\"}\n\nnot json\n{\"event\":\"b\"}\n").unwrap();
        let v = telemetry_pending(&path).unwrap();
        assert_eq!(v["exists"], true);
        assert_eq!(v["count"], 2);
        assert_eq!(v["unparsed_lines"], 1);
        assert_eq!(v["events"][1]["event"], "b");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn telemetry_pending_without_a_file_is_empty_not_an_error() {
        let path = std::env::temp_dir()
            .join(format!("vct-cli-no-pending-{}", std::process::id()))
            .join(TELEMETRY_PENDING_FILE);
        let v = telemetry_pending(&path).unwrap();
        assert_eq!(v["exists"], false);
        assert_eq!(v["count"], 0);
        assert_eq!(v["events"], serde_json::json!([]));
    }

    /// O-A1 (v0.2.97 review round 7): the hub-port ladder is a pinned mirror
    /// of the ONE readers (`vco_lib.hub_ensure.resolve_hub_port`,
    /// `vct_launcher_core::services::hub_port`). This test runs the SHARED
    /// case table `tests/fixtures/hub_port_cases.json` through
    /// `resolve_port(None)` exactly the way
    /// `tests/test_v0297_hub_port_clients.py` runs it through the Python
    /// reader and the sh/ps1 clients — before it, a drift here (e.g.
    /// `VCT_HUB_PORT=0` winning, or `VCT_STATE_DIR` ignored for `hub.port`)
    /// passed every test.
    /// Serializes the tests that change the process environment.
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    /// With every proxy variable pointing at a dead port and no `NO_PROXY`,
    /// the hub client still reaches the hub on 127.0.0.1 (a test-owned
    /// responder on an ephemeral port). Red without `.no_proxy()`: reqwest
    /// would send the request — bearer token included — to the proxy.
    #[test]
    fn the_hub_client_never_goes_through_a_proxy() {
        use std::io::{Read, Write};
        let _lock = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let mut stream = stream;
                let mut buf = [0u8; 2048];
                let _ = stream.read(&mut buf);
                let _ = stream.write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{\"ok\":true}",
                );
            }
        });
        let vars = ["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"];
        let saved: Vec<(&str, Option<String>)> = vars
            .iter()
            .chain(["NO_PROXY", "no_proxy"].iter())
            .map(|k| (*k, std::env::var(k).ok()))
            .collect();
        for k in vars {
            std::env::set_var(k, "http://127.0.0.1:9");
        }
        std::env::remove_var("NO_PROXY");
        std::env::remove_var("no_proxy");
        let hub = Hub::new(Some(port));
        for (k, v) in &saved {
            match v {
                Some(v) => std::env::set_var(k, v),
                None => std::env::remove_var(k),
            }
        }
        let got: serde_json::Value = hub.expect("client").get_json("/health").expect("direct to the hub");
        assert_eq!(got["ok"], true);
    }

    #[test]
    fn hub_port_ladder_matches_the_parity_table() {
        let _lock = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        struct EnvGuard {
            home: Option<String>,
            port: Option<String>,
            state: Option<String>,
        }
        impl Drop for EnvGuard {
            fn drop(&mut self) {
                match self.home.take() {
                    Some(v) => std::env::set_var("HOME", v),
                    None => std::env::remove_var("HOME"),
                }
                match self.port.take() {
                    Some(v) => std::env::set_var("VCT_HUB_PORT", v),
                    None => std::env::remove_var("VCT_HUB_PORT"),
                }
                match self.state.take() {
                    Some(v) => std::env::set_var("VCT_STATE_DIR", v),
                    None => std::env::remove_var("VCT_STATE_DIR"),
                }
            }
        }
        let _guard = EnvGuard {
            home: std::env::var("HOME").ok(),
            port: std::env::var("VCT_HUB_PORT").ok(),
            state: std::env::var("VCT_STATE_DIR").ok(),
        };
        let dir = std::env::temp_dir().join(format!("vct-cli-hub-port-{}", std::process::id()));
        let state = dir.join("state");
        std::fs::create_dir_all(&state).unwrap();
        // The home fallback must not reach the developer's real ~/.vct either.
        std::env::set_var("HOME", &dir);
        std::env::set_var("VCT_STATE_DIR", &state);
        let port_file = state.join("hub.port");

        let text = include_str!("../../../../tests/fixtures/hub_port_cases.json");
        let table: serde_json::Value = serde_json::from_str(text).expect("fixture parses");
        let cases = table["cases"].as_array().expect("cases");
        assert!(!cases.is_empty());
        for case in cases {
            let name = case["name"].as_str().unwrap();
            match case["env_port"].as_str() {
                Some(v) => std::env::set_var("VCT_HUB_PORT", v),
                None => std::env::remove_var("VCT_HUB_PORT"),
            }
            match case["file_port"].as_str() {
                Some(v) => std::fs::write(&port_file, v).unwrap(),
                None => {
                    let _ = std::fs::remove_file(&port_file);
                }
            }
            assert_eq!(
                resolve_port(None).expect("resolve_port never fails"),
                case["expect"].as_u64().unwrap() as u16,
                "case `{name}`"
            );
        }
        std::env::remove_var("VCT_HUB_PORT");
        let _ = std::fs::remove_file(&port_file);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
