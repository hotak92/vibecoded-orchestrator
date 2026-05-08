//! vct — command-line interface for the VCT Launcher (P6).
//!
//! Talks to the launcher's local hub HTTP server (default port 7700,
//! discoverable via `~/.vct/hub.port`). Every operation that the GUI
//! exposes via Tauri commands has a corresponding hub HTTP endpoint
//! in `launcher/src-tauri/src/hub/cli_api.rs` (and friends).
//!
//! Why a separate binary?
//!   - The launcher GUI is Tauri (heavy native deps). Pulling Tauri
//!     into a CLI tool is wasteful. A small reqwest client is enough.
//!   - Built independently: `cargo build --release --bin vct` from
//!     `tools/vct-cli/`. The Tauri app build is unaffected.
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
    name = "vco",
    version,
    about = "vibecoded-orchestrator CLI — power-user / CI escape hatch.",
    long_about = "\
Talks to the running VCT Launcher's local hub server (default 127.0.0.1:7700).
The launcher must be running for these commands to succeed; if it is not,
start it via the system tray or the desktop app first.

Note: this CLI was renamed from `vct` to `vco` in v0.1.0 to avoid a name
collision with the bash secrets tool at tools/vct-secrets/vct."
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
    /// Enable a hook by numeric id.
    Enable {
        hook_id: i64,
        #[arg(long)]
        project: Option<String>,
    },
    /// Disable a hook by numeric id.
    Disable {
        hook_id: i64,
        #[arg(long)]
        project: Option<String>,
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
        let client = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(15))
            .build()
            .context("build http client")?;
        let token = resolve_token();
        Ok(Self { base, client, token })
    }

    fn url(&self) -> &str {
        &self.base
    }

    /// Apply the Authorization: Bearer <token> header if we have one.
    /// Centralised here so every method gets it consistently.
    fn with_auth(&self, b: reqwest::blocking::RequestBuilder) -> reqwest::blocking::RequestBuilder {
        if let Some(t) = self.token.as_ref() {
            b.bearer_auth(t)
        } else {
            b
        }
    }

    fn get_json<T: for<'de> Deserialize<'de>>(&self, path: &str) -> Result<T> {
        let resp = self.with_auth(self.client.get(format!("{}{}", self.base, path))).send()
            .map_err(|e| anyhow!("Cannot reach launcher hub: {}. Is the launcher running?", e))?;
        let status = resp.status();
        let body = resp.text().context("read response body")?;
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
        let resp = self.with_auth(self.client.post(format!("{}{}", self.base, path)).json(body)).send()
            .map_err(|e| anyhow!("Cannot reach launcher hub: {}. Is the launcher running?", e))?;
        let status = resp.status();
        let body = resp.text().context("read response body")?;
        if !status.is_success() {
            return Err(anyhow!("hub error {}: {}", status, body));
        }
        serde_json::from_str(&body).with_context(|| format!("decode {}", path))
    }

    fn patch_json<B: Serialize, T: for<'de> Deserialize<'de>>(
        &self,
        path: &str,
        body: &B,
    ) -> Result<T> {
        let resp = self.with_auth(self.client.patch(format!("{}{}", self.base, path)).json(body)).send()
            .map_err(|e| anyhow!("Cannot reach launcher hub: {}. Is the launcher running?", e))?;
        let status = resp.status();
        let body = resp.text().context("read response body")?;
        if !status.is_success() {
            return Err(anyhow!("hub error {}: {}", status, body));
        }
        serde_json::from_str(&body).with_context(|| format!("decode {}", path))
    }

    fn delete_json<T: for<'de> Deserialize<'de>>(&self, path: &str) -> Result<T> {
        let resp = self.with_auth(self.client.delete(format!("{}{}", self.base, path))).send()
            .map_err(|e| anyhow!("Cannot reach launcher hub: {}. Is the launcher running?", e))?;
        let status = resp.status();
        let body = resp.text().context("read response body")?;
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
    if let Ok(t) = std::env::var("VCT_HUB_TOKEN") {
        let trimmed = t.trim();
        if !trimmed.is_empty() {
            return Some(trimmed.to_string());
        }
    }
    // Standard path: read from disk, mirroring server.rs's
    // `auth::write_token_file`. We honour VCT_STATE_DIR the same
    // way `resolve_port` does so dev launchers / tests stay
    // isolated from the production state dir.
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

fn resolve_port(override_port: Option<u16>) -> Result<u16> {
    if let Some(p) = override_port {
        return Ok(p);
    }
    if let Ok(s) = std::env::var("VCT_HUB_PORT") {
        if let Ok(p) = s.parse::<u16>() {
            return Ok(p);
        }
    }
    // Read ~/.vct/hub.port if present (the launcher writes it on startup).
    if let Some(d) = directories::UserDirs::new() {
        let p = d.home_dir().join(".vct").join("hub.port");
        if let Ok(content) = std::fs::read_to_string(&p) {
            if let Ok(parsed) = content.trim().parse::<u16>() {
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
        eprintln!("vct: {}", e);
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let cli = Cli::parse();
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
    }
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
