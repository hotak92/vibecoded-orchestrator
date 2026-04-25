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
    name = "vct",
    version,
    about = "VCT Launcher CLI — power-user / CI escape hatch.",
    long_about = "\
Talks to the running VCT Launcher's local hub server (default 127.0.0.1:7700).
The launcher must be running for these commands to succeed; if it is not,
start it via the system tray or the desktop app first."
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

// ─── Hub client ─────────────────────────────────────────────────────────

struct Hub {
    base: String,
    client: reqwest::blocking::Client,
}

impl Hub {
    fn new(port_override: Option<u16>) -> Result<Self> {
        let port = resolve_port(port_override)?;
        let base = format!("http://127.0.0.1:{}/api/v1", port);
        let client = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(15))
            .build()
            .context("build http client")?;
        Ok(Self { base, client })
    }

    fn url(&self) -> &str {
        &self.base
    }

    fn get_json<T: for<'de> Deserialize<'de>>(&self, path: &str) -> Result<T> {
        let resp = self.client.get(format!("{}{}", self.base, path)).send()
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
        let resp = self.client.post(format!("{}{}", self.base, path)).json(body).send()
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
        let resp = self.client.patch(format!("{}{}", self.base, path)).json(body).send()
            .map_err(|e| anyhow!("Cannot reach launcher hub: {}. Is the launcher running?", e))?;
        let status = resp.status();
        let body = resp.text().context("read response body")?;
        if !status.is_success() {
            return Err(anyhow!("hub error {}: {}", status, body));
        }
        serde_json::from_str(&body).with_context(|| format!("decode {}", path))
    }

    fn delete_json<T: for<'de> Deserialize<'de>>(&self, path: &str) -> Result<T> {
        let resp = self.client.delete(format!("{}{}", self.base, path)).send()
            .map_err(|e| anyhow!("Cannot reach launcher hub: {}. Is the launcher running?", e))?;
        let status = resp.status();
        let body = resp.text().context("read response body")?;
        if !status.is_success() {
            return Err(anyhow!("hub error {}: {}", status, body));
        }
        serde_json::from_str(&body).with_context(|| format!("decode {}", path))
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
