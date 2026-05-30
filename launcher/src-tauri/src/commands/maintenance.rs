//! PR-37 (v0.2.12, 2026-05-16): GUI-surfaced maintenance ops.
//!
//! Three operations that previously existed only on the CLI side are
//! exposed here as Tauri commands so the launcher GUI can drive them
//! without dropping the user into a terminal. Each operation has a
//! pure-`status` reader (cheap, side-effect-free) plus an action command
//! that mutates state. Action commands ALWAYS go through a consent
//! gate — either a per-entry consent Vec (stale-MCP rewrite) or a UUID
//! consent token issued by the frontend BEFORE displaying its modal
//! (schema migrations). The token approach prevents accidental re-runs
//! from page reloads / replayed Tauri events while keeping the API
//! purely synchronous from the FE's perspective.
//!
//! ## Surfaced ops
//!
//!   1. **MCP registration** (PR-23 wrapped).
//!      Backend wraps `mcp_registration::register_default_orchestrator_mcps`.
//!      Status reader (`mcp_registration_status`) inspects `~/.claude.json`
//!      and reports which orchestrator MCPs are present with paths that
//!      anchor on the current install_root. Action (`rerun_mcp_registration`)
//!      re-invokes the registration with current ports + install_root.
//!      Idempotent — already-correct entries are no-ops.
//!
//!   2. **Schema migrations** (PR-24 wrapped).
//!      Backend probes Weaviate `/v1/schema` to determine whether
//!      `*_Development` collections have the 4 temporal date props
//!      (`created` / `updated` / `valid_from` / `valid_until`) and
//!      whether the shared KG class has
//!      `invertedIndexConfig.indexNullState=True`. Action shells out
//!      to `scripts/migrate-development-temporal-props.{sh,ps1}` and
//!      `scripts/migrate-shared-kg-schema.{sh,ps1}`. Consent-token gated.
//!
//!   3. **Stale-MCP rewrite** (PR-33 GUI surface; CLI gate lives in
//!      install.py::_detect_stale_mcp_entries).
//!      Status reader (`stale_mcp_entries`) re-implements the same
//!      detection heuristic — flags `~/.claude.json mcpServers` entries
//!      whose `command` or `args[0]` looks like a vco install layout
//!      (`claude_mcp_servers` or `.venv` segments) BUT doesn't start
//!      with the current install_root. Action (`rewrite_stale_mcp_entries`)
//!      accepts a per-entry consent `Vec<(name, should_rewrite)>` and
//!      surgically rewrites only the entries the user opted in for.
//!
//! All commands are soft-fail: status readers return a degraded report
//! (e.g. "Weaviate not reachable") rather than erroring; action commands
//! return a structured report so the FE can surface per-op outcomes
//! without a follow-up RPC.
//!
//! ## Cross-OS
//!
//! Schema-migration scripts ship in both `.sh` and `.ps1` variants
//! under `scripts/`. The action command picks the correct one based on
//! `cfg!(target_os = "windows")`. Soft-fail when neither variant is
//! present on disk (e.g. a launcher binary running detached from its
//! install_root): the per-script outcome carries a "script not found"
//! note rather than panicking.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{command, State};

use crate::config::LocalConfig;
use crate::db::Db;
use crate::mcp_registration::{
    register_default_orchestrator_mcps, user_claude_json, DEFAULT_CODE_EMBED_PORT,
    DEFAULT_GRPC_PORT, DEFAULT_OLLAMA_PORT, DEFAULT_WEAVIATE_PORT, ServicePorts,
};
use vct_launcher_core::process::CommandExt as _;

/// Consent-token TTL for `run_schema_migrations`. Tokens issued by the
/// FE expire after this window — long enough to cover a slow modal
/// confirmation, short enough that an old token from a previous page
/// session can't be replayed.
const CONSENT_TOKEN_TTL: Duration = Duration::from_secs(300);

/// In-memory store of issued consent tokens. Keyed by the UUID string
/// the FE generates; value is the `Instant` of issuance. Lock-protected
/// because Tauri commands run on a multi-threaded runtime.
///
/// We never persist these to disk — a launcher restart invalidates all
/// pending tokens, which is the desired security property.
fn consent_store() -> &'static Mutex<HashMap<String, Instant>> {
    use std::sync::OnceLock;
    static STORE: OnceLock<Mutex<HashMap<String, Instant>>> = OnceLock::new();
    STORE.get_or_init(|| Mutex::new(HashMap::new()))
}

fn purge_expired_tokens(map: &mut HashMap<String, Instant>) {
    let now = Instant::now();
    map.retain(|_, issued| now.duration_since(*issued) < CONSENT_TOKEN_TTL);
}

// ═══════════════════════════════════════════════════════════════════════
// MCP registration status + rerun
// ═══════════════════════════════════════════════════════════════════════

/// Per-MCP registration view as the GUI status badge sees it.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct McpRegistrationEntry {
    pub name: String,
    /// True iff `mcpServers[name]` exists in the global `~/.claude.json`.
    pub present: bool,
    /// True iff `present` AND the entry's command / first-arg path
    /// starts with `install_root` (i.e. points at the current install
    /// rather than a stale prior one).
    pub path_matches_install: bool,
    /// Command stored in the entry (or empty when absent). Surfaced so
    /// the GUI can render the path inline for diagnostics.
    pub command: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct McpRegistrationStatusReport {
    /// Resolved install_root used to evaluate `path_matches_install`.
    /// Empty when the launcher couldn't locate an install on disk.
    pub install_root: String,
    /// Absolute path to `~/.claude.json` evaluated.
    pub claude_json_path: String,
    /// True iff `~/.claude.json` exists and is readable.
    pub claude_json_exists: bool,
    /// Per-MCP status. Ordered: `weaviate-kg`, `search` (the canonical
    /// default-orchestrator set per PR-23).
    pub entries: Vec<McpRegistrationEntry>,
    /// Overall: `green` (all registered + paths match), `yellow` (some
    /// missing or path-mismatched), `red` (none registered).
    pub badge: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RegistrationReport {
    pub claude_json_path: String,
    pub success_count: usize,
    pub total: usize,
    pub failures: Vec<String>,
    /// Soft-failures from the optional launcher.db sync step.
    pub db_warnings: Vec<String>,
}

/// Default-orchestrator MCP names. Mirror of the names produced by
/// `mcp_registration::build_default_mcp_entries`. Kept in this module
/// because the status reader needs the list without invoking the
/// builder (which requires a real venv-python on disk).
const DEFAULT_MCP_NAMES: &[&str] = &["weaviate-kg", "search"];

/// Resolve the install_root the same way `installer::get_known_install_path`
/// does — app_state cache first, walking from the launcher binary second.
/// Returns empty string when no install is detectable.
fn resolve_install_root(db: &Db) -> String {
    use crate::commands::installer::APP_STATE_KEY_INSTALL_PATH;
    if let Ok(Some(cached)) = db.app_state_get(APP_STATE_KEY_INSTALL_PATH) {
        if !cached.is_empty() {
            return cached;
        }
    }
    String::new()
}

/// Extract the resource-pointing path from an MCP entry. Prefers the
/// `command` field, falls back to `args[0]` if `command` is not a path
/// (e.g. `node` / `python` invocations where the actual script is in
/// args). Returns empty on absent / malformed.
fn entry_resource_path(entry: &serde_json::Value) -> String {
    let cmd = entry
        .get("command")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    // Heuristic match: if command looks like an absolute path (starts
    // with `/` on Unix or `C:` on Windows), prefer it. Otherwise the
    // resource path is probably the first arg.
    let cmd_is_path = cmd.starts_with('/')
        || (cmd.len() >= 2 && cmd.chars().nth(1) == Some(':'))
        || cmd.starts_with("\\\\");
    if cmd_is_path {
        return cmd.to_string();
    }
    let args = entry.get("args").and_then(|v| v.as_array());
    if let Some(args) = args {
        if let Some(first) = args.first().and_then(|v| v.as_str()) {
            if !first.is_empty() {
                return first.to_string();
            }
        }
    }
    cmd.to_string()
}

#[command]
pub async fn mcp_registration_status(
    db: State<'_, Db>,
) -> Result<McpRegistrationStatusReport, String> {
    let install_root = resolve_install_root(&db);
    let claude_json = user_claude_json();
    let claude_json_path = claude_json.display().to_string();

    let raw = match std::fs::read_to_string(&claude_json) {
        Ok(s) => s,
        Err(_) => {
            // File absent: every MCP is "missing".
            let entries: Vec<McpRegistrationEntry> = DEFAULT_MCP_NAMES
                .iter()
                .map(|n| McpRegistrationEntry {
                    name: n.to_string(),
                    present: false,
                    path_matches_install: false,
                    command: String::new(),
                })
                .collect();
            return Ok(McpRegistrationStatusReport {
                install_root,
                claude_json_path,
                claude_json_exists: false,
                entries,
                badge: "red".into(),
            });
        }
    };

    let parsed: serde_json::Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(e) => {
            return Err(format!("parse {}: {}", claude_json.display(), e));
        }
    };

    let servers = parsed
        .get("mcpServers")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();

    let mut entries = Vec::with_capacity(DEFAULT_MCP_NAMES.len());
    let mut present_count = 0;
    let mut path_match_count = 0;
    for name in DEFAULT_MCP_NAMES {
        match servers.get(*name) {
            Some(entry) => {
                present_count += 1;
                let path = entry_resource_path(entry);
                let matches = !install_root.is_empty() && path.starts_with(&install_root);
                if matches {
                    path_match_count += 1;
                }
                entries.push(McpRegistrationEntry {
                    name: (*name).to_string(),
                    present: true,
                    path_matches_install: matches,
                    command: path,
                });
            }
            None => entries.push(McpRegistrationEntry {
                name: (*name).to_string(),
                present: false,
                path_matches_install: false,
                command: String::new(),
            }),
        }
    }

    let badge = if present_count == 0 {
        "red"
    } else if present_count < DEFAULT_MCP_NAMES.len()
        || path_match_count < present_count
    {
        "yellow"
    } else {
        "green"
    };

    Ok(McpRegistrationStatusReport {
        install_root,
        claude_json_path,
        claude_json_exists: true,
        entries,
        badge: badge.into(),
    })
}

#[command]
pub async fn rerun_mcp_registration(
    db: State<'_, Db>,
) -> Result<RegistrationReport, String> {
    let install_root = resolve_install_root(&db);
    if install_root.is_empty() {
        return Err(
            "no install_root detected (run a full install first or open the launcher \
             from inside the vibecoded-orchestrator clone)"
                .into(),
        );
    }
    let install_path = PathBuf::from(&install_root);

    // We don't currently re-read user-overridden ports from app_state;
    // the canonical defaults match what the installer wrote into the
    // entries originally. A future PR can wire user-overridden ports
    // here if a user changes them post-install.
    let ports = ServicePorts {
        weaviate_port: DEFAULT_WEAVIATE_PORT,
        ollama_port: DEFAULT_OLLAMA_PORT,
        grpc_port: DEFAULT_GRPC_PORT,
        code_embed_port: DEFAULT_CODE_EMBED_PORT,
    };

    let report = register_default_orchestrator_mcps(
        &install_path,
        ports,
        None,
        Some(db.inner()),
    )
    .map_err(|e| format!("register_default_orchestrator_mcps: {}", e))?;

    let failures: Vec<String> = report
        .outcomes
        .iter()
        .filter(|o| !o.ok)
        .map(|o| {
            format!(
                "{}: {}",
                o.name,
                o.error.clone().unwrap_or_else(|| "unknown".into())
            )
        })
        .collect();

    let _ = db.audit(
        "maintenance_rerun_mcp_registration",
        None,
        None,
        &serde_json::json!({
            "install_root": install_root,
            "success_count": report.success_count(),
            "total": report.outcomes.len(),
            "failures": failures,
        }),
    );

    Ok(RegistrationReport {
        claude_json_path: report.claude_json_path.display().to_string(),
        success_count: report.success_count(),
        total: report.outcomes.len(),
        failures,
        db_warnings: report.db_warnings,
    })
}

// ═══════════════════════════════════════════════════════════════════════
// Schema migration status + run
// ═══════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DevelopmentCollectionSchema {
    pub class_name: String,
    /// Which of the four temporal props are present. Order is fixed
    /// (`created`, `updated`, `valid_from`, `valid_until`) so the GUI
    /// can render a 4-cell grid without sorting.
    pub temporal_props_present: Vec<String>,
    pub temporal_props_missing: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SchemaMigrationStatusReport {
    /// Whether `/v1/schema` was reachable. False → badge is yellow with
    /// a "Weaviate not reachable" message; the action button is disabled.
    pub weaviate_reachable: bool,
    pub weaviate_url: String,
    /// Per-`*_Development` collection: which of the four temporal date
    /// props are present. Empty when Weaviate is unreachable.
    pub development_collections: Vec<DevelopmentCollectionSchema>,
    /// Shared KG class name probed (canonical default
    /// `VibeCodedOrchestrator_KnowledgeGraph` if not overridden — v0.2.23 B1).
    pub shared_kg_class: String,
    /// True when the shared KG class exists. None when Weaviate
    /// unreachable.
    pub shared_kg_exists: Option<bool>,
    /// True when `invertedIndexConfig.indexNullState` is true on the
    /// shared KG class. None when class missing OR Weaviate unreachable.
    pub shared_kg_index_null_state: Option<bool>,
    /// Overall: `green` (all schemas correct), `yellow` (some
    /// missing / Weaviate unreachable), `red` (catastrophic — parse
    /// error, unrecoverable). Migrations are non-destructive on
    /// already-correct schemas, so most yellow states are safe to
    /// re-run.
    pub badge: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SchemaMigrationReport {
    /// Per-script outcome.
    pub scripts: Vec<SchemaMigrationScriptOutcome>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SchemaMigrationScriptOutcome {
    pub script: String,
    pub ok: bool,
    pub exit_code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
}

const TEMPORAL_PROPS: &[&str] = &["created", "updated", "valid_from", "valid_until"];

/// Default shared-KG class name. Mirrors the canonical default the rest
/// of the launcher uses in IdentityTab.svelte's `sharedKgName` derived
/// value. Kept as a constant rather than reading from app_state so a
/// stale persisted value doesn't trigger spurious "yellow" badges; the
/// migration scripts use the same canonical default.
///
/// v0.2.23 B1 (2026-05-21): flipped from lowercase-c "Vibecoded" to
/// capital-C "VibeCoded" to match the brand spelling. See
/// `commands/project_env_settings.rs::LAST_RESORT_SHARED_KG_COLLECTION` for
/// the source-of-truth constant.
const DEFAULT_SHARED_KG_CLASS: &str = "VibeCodedOrchestrator_KnowledgeGraph";

fn resolve_weaviate_url(cfg: &LocalConfig) -> String {
    if let Ok(v) = std::env::var("VCT_WEAVIATE_URL") {
        if !v.is_empty() {
            return v;
        }
    }
    if let Ok(v) = std::env::var("WEAVIATE_URL") {
        if !v.is_empty() {
            return v;
        }
    }
    cfg.weaviate_url.clone()
}

fn parse_schema_response(
    schema: &serde_json::Value,
    shared_kg_class: &str,
) -> (Vec<DevelopmentCollectionSchema>, Option<bool>, Option<bool>) {
    let classes = match schema.get("classes").and_then(|v| v.as_array()) {
        Some(a) => a,
        None => return (Vec::new(), Some(false), None),
    };
    let mut dev_collections = Vec::new();
    let mut shared_kg_exists: Option<bool> = Some(false);
    let mut shared_kg_index_null: Option<bool> = None;
    for cls in classes {
        let name = match cls.get("class").and_then(|v| v.as_str()) {
            Some(n) => n,
            None => continue,
        };
        if name.ends_with("_Development") {
            let props_arr = cls
                .get("properties")
                .and_then(|v| v.as_array())
                .cloned()
                .unwrap_or_default();
            let prop_names: Vec<String> = props_arr
                .iter()
                .filter_map(|p| p.get("name").and_then(|v| v.as_str()).map(String::from))
                .collect();
            let mut present = Vec::new();
            let mut missing = Vec::new();
            for tp in TEMPORAL_PROPS {
                if prop_names.iter().any(|p| p == tp) {
                    present.push((*tp).to_string());
                } else {
                    missing.push((*tp).to_string());
                }
            }
            dev_collections.push(DevelopmentCollectionSchema {
                class_name: name.to_string(),
                temporal_props_present: present,
                temporal_props_missing: missing,
            });
        }
        // v0.2.23 review-B HIGH-2 (2026-05-21): case-insensitive match
        // against the canonical name AND both legacy aliases. Pre-fix the
        // strict `name == shared_kg_class` check reported "not yet created"
        // for users on v0.2.12–v0.2.22 lowercase-c installs even when their
        // shared KG class existed — the launcher's Services panel then
        // misleadingly suggested running migrations.
        //
        // v0.2.24 B4 (2026-05-22): consolidated into the shared helper
        // `commands::project_env_settings::is_shared_kg_class_name`. Same
        // recognition contract; one source of truth shared with
        // `commands/kg.rs::kg_list_collections`.
        if crate::commands::project_env_settings::is_shared_kg_class_name(
            name,
            shared_kg_class,
        ) {
            shared_kg_exists = Some(true);
            shared_kg_index_null = Some(
                cls.get("invertedIndexConfig")
                    .and_then(|c| c.get("indexNullState"))
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false),
            );
        }
    }
    (dev_collections, shared_kg_exists, shared_kg_index_null)
}

#[command]
pub async fn schema_migration_status(
    cfg: State<'_, LocalConfig>,
) -> Result<SchemaMigrationStatusReport, String> {
    let base = resolve_weaviate_url(&cfg);
    let shared_kg_class = DEFAULT_SHARED_KG_CLASS.to_string();

    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
    {
        Ok(c) => c,
        Err(_) => {
            return Ok(SchemaMigrationStatusReport {
                weaviate_reachable: false,
                weaviate_url: base,
                development_collections: Vec::new(),
                shared_kg_class,
                shared_kg_exists: None,
                shared_kg_index_null_state: None,
                badge: "yellow".into(),
            });
        }
    };

    // Probe both readiness and schema.
    let ready = client
        .get(format!("{}/v1/.well-known/ready", base))
        .send()
        .await;
    if ready.is_err() || !ready.as_ref().map(|r| r.status().is_success()).unwrap_or(false) {
        return Ok(SchemaMigrationStatusReport {
            weaviate_reachable: false,
            weaviate_url: base,
            development_collections: Vec::new(),
            shared_kg_class,
            shared_kg_exists: None,
            shared_kg_index_null_state: None,
            badge: "yellow".into(),
        });
    }

    let resp = match client.get(format!("{}/v1/schema", base)).send().await {
        Ok(r) => r,
        Err(_) => {
            return Ok(SchemaMigrationStatusReport {
                weaviate_reachable: false,
                weaviate_url: base,
                development_collections: Vec::new(),
                shared_kg_class,
                shared_kg_exists: None,
                shared_kg_index_null_state: None,
                badge: "yellow".into(),
            });
        }
    };
    if !resp.status().is_success() {
        return Ok(SchemaMigrationStatusReport {
            weaviate_reachable: false,
            weaviate_url: base,
            development_collections: Vec::new(),
            shared_kg_class,
            shared_kg_exists: None,
            shared_kg_index_null_state: None,
            badge: "yellow".into(),
        });
    }

    let schema: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("parse /v1/schema response: {}", e))?;

    let (dev_collections, shared_kg_exists, shared_kg_index_null_state) =
        parse_schema_response(&schema, &shared_kg_class);

    // Badge:
    //   - green when every Dev collection has all 4 temporal props AND
    //     shared KG has indexNullState=true (or shared KG doesn't exist
    //     yet — the seed step will create it correctly).
    //   - yellow when at least one Dev collection is missing props OR
    //     shared KG exists but indexNullState=false.
    let dev_all_ok = dev_collections
        .iter()
        .all(|c| c.temporal_props_missing.is_empty());
    let shared_ok = match (shared_kg_exists, shared_kg_index_null_state) {
        (Some(true), Some(true)) => true,
        (Some(false), _) => true, // class doesn't exist yet — seed step creates with correct schema
        _ => false,
    };
    let badge = if dev_all_ok && shared_ok {
        "green"
    } else {
        "yellow"
    };

    Ok(SchemaMigrationStatusReport {
        weaviate_reachable: true,
        weaviate_url: base,
        development_collections: dev_collections,
        shared_kg_class,
        shared_kg_exists,
        shared_kg_index_null_state,
        badge: badge.into(),
    })
}

/// Issue a one-shot consent token for the schema-migration flow. The FE
/// calls this when the modal opens; the user-confirmation click round-trips
/// the same token back via `run_schema_migrations`. Token expires after
/// `CONSENT_TOKEN_TTL`.
#[command]
pub async fn issue_schema_migration_consent_token() -> Result<String, String> {
    let token = uuid::Uuid::new_v4().to_string();
    let mut map = consent_store().lock().map_err(|e| format!("lock: {}", e))?;
    purge_expired_tokens(&mut map);
    map.insert(token.clone(), Instant::now());
    Ok(token)
}

/// Pick the per-OS migration script for `name` ("migrate-development-temporal-props"
/// or "migrate-shared-kg-schema"). Returns the absolute path; the caller
/// soft-fails when the file doesn't exist.
fn migration_script_path(install_root: &Path, name: &str) -> PathBuf {
    let ext = if cfg!(target_os = "windows") {
        "ps1"
    } else {
        "sh"
    };
    install_root.join("scripts").join(format!("{}.{}", name, ext))
}

fn run_migration_script(
    install_root: &Path,
    script_basename: &str,
    weaviate_url: &str,
) -> SchemaMigrationScriptOutcome {
    let script = migration_script_path(install_root, script_basename);
    if !script.is_file() {
        return SchemaMigrationScriptOutcome {
            script: script.display().to_string(),
            ok: false,
            exit_code: None,
            stdout: String::new(),
            stderr: format!("script not found: {}", script.display()),
        };
    }
    let mut cmd = if cfg!(target_os = "windows") {
        let mut c = std::process::Command::new("powershell.exe").silent();
        c.arg("-NoProfile")
            .arg("-ExecutionPolicy")
            .arg("Bypass")
            .arg("-File")
            .arg(&script);
        c
    } else {
        let mut c = std::process::Command::new("bash").silent();
        c.arg(&script);
        c
    };
    cmd.env("WEAVIATE_URL", weaviate_url);
    // Run from the install_root so the shared-KG migration's relative
    // `.claude/scripts/kg-sync` lookup hits.
    cmd.current_dir(install_root);
    match cmd.output() {
        Ok(out) => SchemaMigrationScriptOutcome {
            script: script.display().to_string(),
            ok: out.status.success(),
            exit_code: out.status.code(),
            stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
        },
        Err(e) => SchemaMigrationScriptOutcome {
            script: script.display().to_string(),
            ok: false,
            exit_code: None,
            stdout: String::new(),
            stderr: format!("spawn: {}", e),
        },
    }
}

#[command]
pub async fn run_schema_migrations(
    consent_token: String,
    cfg: State<'_, LocalConfig>,
    db: State<'_, Db>,
) -> Result<SchemaMigrationReport, String> {
    // Validate the consent token (and remove it — single-use).
    {
        let mut map = consent_store().lock().map_err(|e| format!("lock: {}", e))?;
        purge_expired_tokens(&mut map);
        let issued = map.remove(&consent_token);
        match issued {
            Some(at) if Instant::now().duration_since(at) < CONSENT_TOKEN_TTL => {}
            _ => {
                return Err(
                    "missing or expired consent token — open the migration modal again \
                     and click Run within 5 minutes"
                        .into(),
                );
            }
        }
    }

    let install_root = resolve_install_root(&db);
    if install_root.is_empty() {
        return Err(
            "no install_root detected — run a full install first or open the launcher \
             from inside the vibecoded-orchestrator clone"
                .into(),
        );
    }
    let install_path = PathBuf::from(&install_root);
    let weaviate_url = resolve_weaviate_url(&cfg);

    let mut outcomes = Vec::new();
    outcomes.push(run_migration_script(
        &install_path,
        "migrate-development-temporal-props",
        &weaviate_url,
    ));
    outcomes.push(run_migration_script(
        &install_path,
        "migrate-shared-kg-schema",
        &weaviate_url,
    ));

    let _ = db.audit(
        "maintenance_run_schema_migrations",
        None,
        None,
        &serde_json::json!({
            "install_root": install_root,
            "weaviate_url": weaviate_url,
            "scripts": outcomes
                .iter()
                .map(|o| serde_json::json!({"script": o.script, "ok": o.ok, "exit_code": o.exit_code}))
                .collect::<Vec<_>>(),
        }),
    );

    Ok(SchemaMigrationReport { scripts: outcomes })
}

// ═══════════════════════════════════════════════════════════════════════
// Stale MCP entries detection + rewrite
// ═══════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StaleMcpEntry {
    pub name: String,
    /// The current command / arg path that anchors outside install_root.
    pub current_path: String,
    /// Suggested new path inside install_root. May be empty when the
    /// detector can't confidently rewrite (e.g. unfamiliar command shape).
    pub suggested_path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RewriteReport {
    pub claude_json_path: String,
    pub rewritten: Vec<String>,
    pub skipped: Vec<String>,
    pub errors: Vec<String>,
}

/// Mirror of `install.py::_detect_stale_mcp_entries` heuristic, ported
/// to Rust. Flags entries where `command` or `args[0]` looks like a vco
/// install layout (contains `claude_mcp_servers` or `.venv` segments)
/// BUT doesn't start with the current install_root. Returns the per-entry
/// stale tuple list so the GUI can show a checkbox row per entry.
fn detect_stale_mcp_entries(
    install_root: &str,
    claude_json: &Path,
) -> Vec<StaleMcpEntry> {
    if install_root.is_empty() {
        return Vec::new();
    }
    let raw = match std::fs::read_to_string(claude_json) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };
    let parsed: serde_json::Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    let servers = match parsed.get("mcpServers").and_then(|v| v.as_object()) {
        Some(s) => s,
        None => return Vec::new(),
    };

    let mut stale = Vec::new();
    for (name, entry) in servers {
        let cmd = entry
            .get("command")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let first_arg = entry
            .get("args")
            .and_then(|v| v.as_array())
            .and_then(|a| a.first())
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        for candidate in [&cmd, &first_arg] {
            if candidate.is_empty() {
                continue;
            }
            // Must look like an absolute path (Unix `/`, Windows drive,
            // UNC). Otherwise skip — bare `node` / `python` / user MCPs
            // that don't anchor on disk aren't ours to migrate.
            let looks_absolute = candidate.starts_with('/')
                || candidate.starts_with("C:\\")
                || candidate.starts_with("c:\\")
                || candidate.starts_with("\\\\");
            if !looks_absolute {
                continue;
            }
            // Anchor: only flag paths that look like vco install
            // layouts. Otherwise we'd flag every user-added MCP in
            // /usr/bin/foo.
            if !candidate.contains("claude_mcp_servers") && !candidate.contains(".venv") {
                continue;
            }
            if candidate.starts_with(install_root) {
                // Already inside install_root — not stale.
                break;
            }
            // Build a suggested rewrite. Best-effort: anchor on the
            // `claude_mcp_servers` / `.venv` substring and splice
            // install_root in. Returns empty string when the
            // substring isn't found cleanly.
            let suggested = suggest_rewrite_path(candidate, install_root);
            stale.push(StaleMcpEntry {
                name: name.clone(),
                current_path: candidate.clone(),
                suggested_path: suggested,
            });
            break;
        }
    }
    stale
}

/// Splice `install_root` over the prefix of `candidate` that comes
/// before the first occurrence of `claude_mcp_servers` or `.venv`.
/// Returns empty string when neither anchor is in the path.
fn suggest_rewrite_path(candidate: &str, install_root: &str) -> String {
    for anchor in ["claude_mcp_servers", ".venv"] {
        if let Some(idx) = candidate.find(anchor) {
            let suffix = &candidate[idx..];
            let sep = if cfg!(target_os = "windows") {
                "\\"
            } else {
                "/"
            };
            // Trim a trailing separator off install_root so we don't
            // end up with `//`.
            let trimmed = install_root
                .trim_end_matches('/')
                .trim_end_matches('\\');
            return format!("{}{}{}", trimmed, sep, suffix);
        }
    }
    String::new()
}

#[command]
pub async fn stale_mcp_entries(db: State<'_, Db>) -> Result<Vec<StaleMcpEntry>, String> {
    let install_root = resolve_install_root(&db);
    let claude_json = user_claude_json();
    Ok(detect_stale_mcp_entries(&install_root, &claude_json))
}

#[command]
pub async fn rewrite_stale_mcp_entries(
    consent: Vec<(String, bool)>,
    db: State<'_, Db>,
) -> Result<RewriteReport, String> {
    let install_root = resolve_install_root(&db);
    if install_root.is_empty() {
        return Err(
            "no install_root detected — open the launcher from a real install path \
             before rewriting MCP entries"
                .into(),
        );
    }
    let claude_json = user_claude_json();
    let raw = std::fs::read_to_string(&claude_json)
        .map_err(|e| format!("read {}: {}", claude_json.display(), e))?;
    let mut parsed: serde_json::Value = serde_json::from_str(&raw)
        .map_err(|e| format!("parse {}: {}", claude_json.display(), e))?;

    let stale = detect_stale_mcp_entries(&install_root, &claude_json);
    let stale_map: HashMap<String, StaleMcpEntry> =
        stale.into_iter().map(|s| (s.name.clone(), s)).collect();

    let mut rewritten = Vec::new();
    let mut skipped = Vec::new();
    let mut errors = Vec::new();

    let servers = parsed
        .get_mut("mcpServers")
        .and_then(|v| v.as_object_mut());
    let Some(servers) = servers else {
        return Err("~/.claude.json has no mcpServers object".into());
    };

    for (name, should_rewrite) in &consent {
        if !should_rewrite {
            skipped.push(name.clone());
            continue;
        }
        let Some(stale_entry) = stale_map.get(name) else {
            errors.push(format!("{}: not in stale set", name));
            continue;
        };
        if stale_entry.suggested_path.is_empty() {
            errors.push(format!(
                "{}: no suggested rewrite (unfamiliar path shape)",
                name
            ));
            continue;
        }
        let Some(entry) = servers.get_mut(name).and_then(|v| v.as_object_mut()) else {
            errors.push(format!("{}: entry vanished mid-rewrite", name));
            continue;
        };

        // Rewrite the command field IF it currently matches
        // `current_path`. Otherwise rewrite `args[0]`. Mirrors the
        // detection heuristic exactly.
        let cmd_matches = entry
            .get("command")
            .and_then(|v| v.as_str())
            .map(|s| s == stale_entry.current_path)
            .unwrap_or(false);
        if cmd_matches {
            entry.insert(
                "command".to_string(),
                serde_json::Value::String(stale_entry.suggested_path.clone()),
            );
        } else if let Some(args) = entry.get_mut("args").and_then(|v| v.as_array_mut()) {
            if let Some(first) = args.first_mut() {
                if first.as_str() == Some(stale_entry.current_path.as_str()) {
                    *first = serde_json::Value::String(stale_entry.suggested_path.clone());
                } else {
                    errors.push(format!("{}: args[0] no longer matches", name));
                    continue;
                }
            } else {
                errors.push(format!("{}: empty args array", name));
                continue;
            }
        } else {
            errors.push(format!("{}: no command/args path found", name));
            continue;
        }
        rewritten.push(name.clone());
    }

    if !rewritten.is_empty() {
        // Atomic write via the same temp+rename pattern register_mcp uses.
        let body = serde_json::to_string_pretty(&parsed)
            .map_err(|e| format!("serialize: {}", e))?;
        // Backup first.
        let bak = claude_json.with_extension("json.bak");
        let _ = std::fs::copy(&claude_json, &bak);
        let tmp = claude_json.with_extension("json.tmp");
        std::fs::write(&tmp, body).map_err(|e| format!("write tmp: {}", e))?;
        std::fs::rename(&tmp, &claude_json)
            .map_err(|e| format!("rename tmp -> claude.json: {}", e))?;
    }

    let _ = db.audit(
        "maintenance_rewrite_stale_mcp_entries",
        None,
        None,
        &serde_json::json!({
            "install_root": install_root,
            "rewritten": rewritten.clone(),
            "skipped": skipped.clone(),
            "errors": errors.clone(),
        }),
    );

    Ok(RewriteReport {
        claude_json_path: claude_json.display().to_string(),
        rewritten,
        skipped,
        errors,
    })
}

// ═══════════════════════════════════════════════════════════════════════
// PR-42 (v0.2.12 / 2026-05-16): SIGHUP-driven MCP env reload
// ═══════════════════════════════════════════════════════════════════════
//
// Fixes Issue B from
// `.claude/context/mcp-instability-vs-public-repo-2026-05-16.md`:
// editing `.claude/settings.json env` mid-chat left the running MCP
// subprocess pinned to the OLD env. Manual `pkill -f weaviate_mcp` was
// the only recovery.
//
// User-direction (2026-05-16):
//   1. **Safe way** — SIGHUP triggers a CLEAN sys.exit(0) on the MCP
//      side (see `claude_mcp_servers/_lib/sighup_handler.py`). NOT an
//      in-process reconnect. Claude Code respawns the MCP on the next
//      request with fresh env. No half-reloaded state.
//   2. **Live-reloadable for ALL env vars** — no per-var allowlist.
//      Any env change → full MCP restart → all vars picked up.
//   3. **Watcher + button** — the launcher's
//      `services::settings_json_watcher` auto-fires this command on
//      `.claude/settings.json` modify events; the GUI button surfaces
//      a manual path for when the watcher is disabled or the user
//      wants to force a reload.
//
// POSIX-only: `kill` and SIGHUP don't exist natively on Windows.
// On Windows the function reports zero signaled processes and the
// user-facing fallback is "restart your Claude Code chat session".

/// Names of the orchestrator MCP server scripts whose processes this
/// reload command targets. Match arguments for `pgrep -f`.
///
/// The patterns are intentionally narrow: a substring like
/// `weaviate_mcp/server.py` matches the absolute path Claude Code uses
/// to spawn the MCP (`<install>/claude_mcp_servers/weaviate_mcp/server.py`).
/// `_mcp/server.py` alone would over-match.
const MCP_PROCESS_PATTERNS: &[&str] = &[
    "weaviate_mcp/server.py",
    "search_mcp/server.py",
];

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReloadReport {
    /// Total number of PIDs signaled across all patterns. Zero when no
    /// MCP processes were found (e.g. user has no Claude Code session
    /// running yet) — soft-success.
    pub signaled_count: usize,
    /// Distinct PIDs we sent SIGHUP to, in discovery order. Surfaced so
    /// the GUI can show "Signaled PIDs 1234, 5678" for transparency.
    pub pids: Vec<u32>,
    /// Per-PID or per-pattern errors. Soft-fail: one stale PID
    /// disappearing between `pgrep` and `kill` doesn't abort the batch.
    pub errors: Vec<String>,
    /// True on Windows native, where SIGHUP doesn't exist. The GUI
    /// surfaces a "use Claude Code session restart" hint when this is
    /// set; the command itself reports zero signaled processes (the
    /// no-op case is not an error).
    pub posix_only_skipped: bool,
}

/// Pluggable command runner — production uses `std::process::Command`;
/// tests inject a stub returning a fake `pgrep` output without touching
/// real PIDs on the host machine.
trait MaintCommandRunner {
    /// Run `pgrep -f <pattern>` and return its stdout (one PID per
    /// line) or an error string. Empty stdout (no matches) is `Ok("")`,
    /// NOT an error.
    fn pgrep(&self, pattern: &str) -> Result<String, String>;
    /// Send SIGHUP to `pid` via `kill -HUP`. Returns `Ok(())` on success,
    /// `Err(msg)` on failure (e.g. permission denied, no such process).
    fn kill_sighup(&self, pid: u32) -> Result<(), String>;
}

/// Real implementation: shells out to `pgrep` and `kill`. POSIX-only.
struct ShellMaintRunner;

impl MaintCommandRunner for ShellMaintRunner {
    fn pgrep(&self, pattern: &str) -> Result<String, String> {
        let out = std::process::Command::new("pgrep").silent()
            .arg("-f")
            .arg(pattern)
            .output()
            .map_err(|e| format!("pgrep: {}", e))?;
        // pgrep exits 1 when no processes match — that's not an error
        // for us, just zero results. Anything else IS an error.
        if !out.status.success() && out.status.code() != Some(1) {
            return Err(format!(
                "pgrep -f {:?} exit={:?}: {}",
                pattern,
                out.status.code(),
                String::from_utf8_lossy(&out.stderr).trim()
            ));
        }
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    }

    fn kill_sighup(&self, pid: u32) -> Result<(), String> {
        let out = std::process::Command::new("kill").silent()
            .arg("-HUP")
            .arg(pid.to_string())
            .output()
            .map_err(|e| format!("kill: {}", e))?;
        if !out.status.success() {
            return Err(format!(
                "kill -HUP {} exit={:?}: {}",
                pid,
                out.status.code(),
                String::from_utf8_lossy(&out.stderr).trim()
            ));
        }
        Ok(())
    }
}

/// Parse `pgrep -f` stdout into a Vec<u32>. Whitespace-tolerant; any
/// line that doesn't parse as a non-zero unsigned int is silently
/// skipped (defensive against pgrep variants that might add headers).
fn parse_pgrep_pids(stdout: &str) -> Vec<u32> {
    stdout
        .lines()
        .filter_map(|line| line.trim().parse::<u32>().ok())
        .filter(|&p| p > 0)
        .collect()
}

/// Pure core: given a runner + a list of patterns, send SIGHUP to every
/// matched PID and return the aggregated report. Extracted from the
/// Tauri command so unit tests can drive it with a stub runner.
///
/// Discipline:
///   * De-dup PIDs across patterns (a single weaviate-mcp process may
///     match multiple substrings).
///   * Per-PID soft-fail: one kill failure doesn't abort the batch.
///   * Self-kill protection: skip our own PID, just in case a pattern
///     ever matches the launcher itself.
fn reload_mcps_with<R: MaintCommandRunner>(runner: &R, patterns: &[&str]) -> ReloadReport {
    let mut pids: Vec<u32> = Vec::new();
    let mut errors: Vec<String> = Vec::new();
    let own_pid = std::process::id();

    for pattern in patterns {
        let stdout = match runner.pgrep(pattern) {
            Ok(s) => s,
            Err(e) => {
                errors.push(format!("pgrep {:?}: {}", pattern, e));
                continue;
            }
        };
        for pid in parse_pgrep_pids(&stdout) {
            if pid == own_pid {
                continue;
            }
            if pids.contains(&pid) {
                continue;
            }
            pids.push(pid);
        }
    }

    for &pid in &pids {
        if let Err(e) = runner.kill_sighup(pid) {
            // Likely cause: PID dead between pgrep and kill (race) or
            // permission denied. Log it but don't abort the batch.
            errors.push(format!("kill -HUP {}: {}", pid, e));
        }
    }

    ReloadReport {
        // signaled_count counts PIDs we successfully signaled — derive
        // it from `pids - failed`. Simpler: count pids whose kill error
        // does NOT appear in errors. But `errors` may include
        // pgrep-level entries too; for simplicity we treat `pids.len()`
        // as the "we tried to signal" count and `errors.len()` as the
        // "things that went wrong" count. The GUI surfaces both.
        signaled_count: pids.len().saturating_sub(
            errors
                .iter()
                .filter(|e| e.starts_with("kill -HUP "))
                .count(),
        ),
        pids,
        errors,
        posix_only_skipped: false,
    }
}

/// Tauri command: signal all running orchestrator MCP processes to
/// pick up new env from `.claude/settings.json`.
///
/// Idempotent: zero matching PIDs is a soft-success (just reports
/// `signaled_count: 0`). The watcher and the GUI button both call this.
///
/// On Windows native, returns a no-op report with
/// `posix_only_skipped: true` — the GUI surfaces a "restart your
/// Claude Code session" hint instead.
#[command]
pub async fn reload_mcps_sighup(db: State<'_, Db>) -> Result<ReloadReport, String> {
    let report = if cfg!(windows) {
        ReloadReport {
            signaled_count: 0,
            pids: Vec::new(),
            errors: vec![
                "SIGHUP is POSIX-only; restart your Claude Code session to apply env changes"
                    .to_string(),
            ],
            posix_only_skipped: true,
        }
    } else {
        let runner = ShellMaintRunner;
        reload_mcps_with(&runner, MCP_PROCESS_PATTERNS)
    };

    let _ = db.audit(
        "maintenance_reload_mcps_sighup",
        None,
        None,
        &serde_json::json!({
            "signaled_count": report.signaled_count,
            "pids": report.pids.clone(),
            "errors": report.errors.clone(),
            "posix_only_skipped": report.posix_only_skipped,
        }),
    );

    Ok(report)
}

/// PR-42: launcher-internal entry point for the
/// `services::settings_json_watcher` task. Same semantics as the
/// `reload_mcps_sighup` Tauri command but without a `State<Db>`
/// dependency (the watcher task doesn't have one in scope). The
/// watcher does its own audit logging; we just run the pgrep+kill core.
pub fn reload_mcps_via_shell_for_watcher() -> ReloadReport {
    if cfg!(windows) {
        return ReloadReport {
            signaled_count: 0,
            pids: Vec::new(),
            errors: vec!["SIGHUP is POSIX-only".to_string()],
            posix_only_skipped: true,
        };
    }
    let runner = ShellMaintRunner;
    reload_mcps_with(&runner, MCP_PROCESS_PATTERNS)
}

// ═══════════════════════════════════════════════════════════════════════
// Tests — focus on the schema-probe parsing + stale-entry detector.
// Rust-side; mocks the Weaviate /v1/schema JSON shape.
// ═══════════════════════════════════════════════════════════════════════

#[cfg(test)]
#[allow(unused_imports)]
mod tests {
    use super::*;

    fn make_schema(classes: Vec<serde_json::Value>) -> serde_json::Value {
        serde_json::json!({ "classes": classes })
    }

    #[test]
    fn parse_schema_response_flags_dev_collections_missing_temporal_props() {
        let schema = make_schema(vec![
            serde_json::json!({
                "class": "Foo_Development",
                "properties": [
                    {"name": "title", "dataType": ["text"]},
                    {"name": "created", "dataType": ["date"]},
                ],
            }),
            serde_json::json!({
                "class": "FooBar_Development",
                "properties": [
                    {"name": "created", "dataType": ["date"]},
                    {"name": "updated", "dataType": ["date"]},
                    {"name": "valid_from", "dataType": ["date"]},
                    {"name": "valid_until", "dataType": ["date"]},
                ],
            }),
        ]);
        let (dev, kg_exists, kg_index_null) =
            parse_schema_response(&schema, "VibeCodedOrchestrator_KnowledgeGraph");

        assert_eq!(dev.len(), 2);
        let foo = dev.iter().find(|c| c.class_name == "Foo_Development").unwrap();
        assert_eq!(foo.temporal_props_present, vec!["created".to_string()]);
        assert_eq!(
            foo.temporal_props_missing,
            vec!["updated", "valid_from", "valid_until"]
        );
        let foobar = dev
            .iter()
            .find(|c| c.class_name == "FooBar_Development")
            .unwrap();
        assert_eq!(foobar.temporal_props_missing, Vec::<String>::new());
        assert_eq!(foobar.temporal_props_present.len(), 4);
        // No shared KG class in this schema.
        assert_eq!(kg_exists, Some(false));
        assert_eq!(kg_index_null, None);
    }

    #[test]
    fn parse_schema_response_detects_shared_kg_index_null_state() {
        let schema = make_schema(vec![
            serde_json::json!({
                "class": "AcmeOrchestrator_KnowledgeGraph",
                "invertedIndexConfig": {"indexNullState": true},
                "properties": [],
            }),
            serde_json::json!({
                "class": "OtherTool_KnowledgeGraph",
                "invertedIndexConfig": {"indexNullState": false},
                "properties": [],
            }),
        ]);

        let (_, exists, idx) = parse_schema_response(&schema, "AcmeOrchestrator_KnowledgeGraph");
        assert_eq!(exists, Some(true));
        assert_eq!(idx, Some(true));

        let (_, exists2, idx2) =
            parse_schema_response(&schema, "OtherTool_KnowledgeGraph");
        assert_eq!(exists2, Some(true));
        assert_eq!(idx2, Some(false));

        let (_, exists3, idx3) =
            parse_schema_response(&schema, "NotPresent_KnowledgeGraph");
        assert_eq!(exists3, Some(false));
        assert_eq!(idx3, None);
    }

    #[test]
    fn parse_schema_response_handles_missing_invertedindexconfig() {
        let schema = make_schema(vec![serde_json::json!({
            "class": "ExampleProj_KnowledgeGraph",
            "properties": [],
        })]);
        let (_, exists, idx) =
            parse_schema_response(&schema, "ExampleProj_KnowledgeGraph");
        assert_eq!(exists, Some(true));
        // indexNullState absent → treated as false.
        assert_eq!(idx, Some(false));
    }

    #[test]
    fn detect_stale_mcp_entries_flags_paths_outside_install_root() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-maintenance-detect-test-{}.json",
            uuid::Uuid::new_v4().simple()
        ));
        let install_root = if cfg!(target_os = "windows") {
            "C:\\Users\\foo\\vco"
        } else {
            "/home/foo/vco"
        };
        let stale_path = if cfg!(target_os = "windows") {
            "C:\\Users\\foo\\old-vco\\claude_mcp_servers\\weaviate_mcp\\server.py"
        } else {
            "/home/foo/old-vco/claude_mcp_servers/weaviate_mcp/server.py"
        };
        let venv_py = if cfg!(target_os = "windows") {
            "C:\\Users\\foo\\old-vco\\.venv\\Scripts\\python.exe"
        } else {
            "/home/foo/old-vco/.venv/bin/python"
        };
        let user_path = if cfg!(target_os = "windows") {
            "C:\\Program Files\\Node\\node.exe"
        } else {
            "/usr/bin/node"
        };

        let payload = serde_json::json!({
            "mcpServers": {
                "weaviate-kg": {
                    "command": venv_py,
                    "args": [stale_path],
                },
                "user-mcp": {
                    "command": user_path,
                    "args": ["/srv/user-mcp/server.js"],
                },
                "search": {
                    "command": format!("{}/claude_mcp_servers/search_mcp/wrapper.sh", install_root),
                    "args": [],
                },
            }
        });
        std::fs::write(&tmp, serde_json::to_string_pretty(&payload).unwrap()).unwrap();

        let stale = detect_stale_mcp_entries(install_root, &tmp);
        // `weaviate-kg` is stale (venv outside install_root).
        // `search` already anchored on install_root — not stale.
        // `user-mcp` is /usr/bin/node — not vco-shaped, ignored.
        assert_eq!(stale.len(), 1, "expected exactly 1 stale entry, got: {:?}", stale);
        assert_eq!(stale[0].name, "weaviate-kg");
        assert!(!stale[0].suggested_path.is_empty(), "suggested path should be set");
        assert!(
            stale[0].suggested_path.starts_with(install_root),
            "suggested path must anchor on install_root: {}",
            stale[0].suggested_path
        );

        std::fs::remove_file(&tmp).ok();
    }

    #[test]
    fn detect_stale_returns_empty_when_install_root_empty() {
        // Defensive: empty install_root should never produce false-positive
        // "everything is stale" results.
        let tmp = std::env::temp_dir().join(format!(
            "vct-maintenance-empty-root-{}.json",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::write(
            &tmp,
            serde_json::to_string(&serde_json::json!({
                "mcpServers": {"foo": {"command": "/anywhere/claude_mcp_servers/x.py"}}
            }))
            .unwrap(),
        )
        .unwrap();
        let stale = detect_stale_mcp_entries("", &tmp);
        assert!(stale.is_empty());
        std::fs::remove_file(&tmp).ok();
    }

    #[test]
    fn suggest_rewrite_path_splices_install_root() {
        let new_root = if cfg!(target_os = "windows") {
            "C:\\users\\acme\\fresh"
        } else {
            "/home/acme/fresh"
        };
        let old = if cfg!(target_os = "windows") {
            "C:\\users\\acme\\old\\claude_mcp_servers\\weaviate_mcp\\server.py"
        } else {
            "/home/acme/old/claude_mcp_servers/weaviate_mcp/server.py"
        };
        let got = suggest_rewrite_path(old, new_root);
        let want_suffix = if cfg!(target_os = "windows") {
            "claude_mcp_servers\\weaviate_mcp\\server.py"
        } else {
            "claude_mcp_servers/weaviate_mcp/server.py"
        };
        assert!(got.starts_with(new_root), "got: {}", got);
        assert!(got.ends_with(want_suffix), "got: {}", got);
    }

    #[test]
    fn suggest_rewrite_path_returns_empty_when_no_anchor() {
        let got = suggest_rewrite_path("/usr/bin/node", "/home/foo/vco");
        assert!(got.is_empty(), "expected empty, got: {}", got);
    }

    #[test]
    fn consent_token_is_one_shot_and_expires() {
        // Issue token, validate it consumes once.
        let token = uuid::Uuid::new_v4().to_string();
        {
            let mut map = consent_store().lock().unwrap();
            // Don't purge older test tokens — they shouldn't affect this case
            // (they expire on their own TTL). Just insert ours.
            map.insert(token.clone(), Instant::now());
        }
        // First consumption: succeeds.
        let consumed = {
            let mut map = consent_store().lock().unwrap();
            map.remove(&token).is_some()
        };
        assert!(consumed);
        // Second consumption: token is gone.
        let consumed2 = {
            let mut map = consent_store().lock().unwrap();
            map.remove(&token).is_some()
        };
        assert!(!consumed2);
    }

    #[test]
    fn entry_resource_path_prefers_command_when_absolute() {
        let entry = serde_json::json!({
            "command": "/home/foo/.venv/bin/python",
            "args": ["/home/foo/server.py"],
        });
        let path = entry_resource_path(&entry);
        // command IS an absolute path → use command.
        assert!(path.starts_with('/') || path.starts_with("C:"));
        // On unix this should be the python venv path; on Windows we
        // accept either since the test command is unix-shaped.
        if cfg!(not(target_os = "windows")) {
            assert_eq!(path, "/home/foo/.venv/bin/python");
        }
    }

    #[test]
    fn entry_resource_path_falls_back_to_args_when_command_is_bare() {
        let entry = serde_json::json!({
            "command": "node",
            "args": ["/srv/foobar/server.js"],
        });
        let path = entry_resource_path(&entry);
        assert_eq!(path, "/srv/foobar/server.js");
    }

    // ───────────────────────────────────────────────────────────────────
    // PR-42 reload_mcps_sighup tests
    // ───────────────────────────────────────────────────────────────────

    use std::cell::RefCell;

    /// Test fixture: records calls + replays canned pgrep output. NOT
    /// thread-safe — each test gets its own instance.
    struct StubRunner {
        /// pattern → canned stdout
        pgrep_responses: HashMap<String, Result<String, String>>,
        /// Records every PID we asked to signal.
        kill_calls: RefCell<Vec<u32>>,
        /// PIDs whose kill call should fail (simulates dead-PID race).
        kill_failures: HashMap<u32, String>,
    }

    impl StubRunner {
        fn new() -> Self {
            Self {
                pgrep_responses: HashMap::new(),
                kill_calls: RefCell::new(Vec::new()),
                kill_failures: HashMap::new(),
            }
        }
    }

    impl MaintCommandRunner for StubRunner {
        fn pgrep(&self, pattern: &str) -> Result<String, String> {
            self.pgrep_responses
                .get(pattern)
                .cloned()
                .unwrap_or_else(|| Ok(String::new()))
        }
        fn kill_sighup(&self, pid: u32) -> Result<(), String> {
            self.kill_calls.borrow_mut().push(pid);
            match self.kill_failures.get(&pid) {
                Some(e) => Err(e.clone()),
                None => Ok(()),
            }
        }
    }

    #[test]
    fn parse_pgrep_pids_handles_one_per_line() {
        let pids = parse_pgrep_pids("1234\n5678\n9012\n");
        assert_eq!(pids, vec![1234, 5678, 9012]);
    }

    #[test]
    fn parse_pgrep_pids_ignores_garbage_lines() {
        // Defensive: pgrep -f variants might prepend a header. Treat
        // any non-numeric line as a soft-skip.
        let pids = parse_pgrep_pids("\n  4242 \nnot-a-pid\n0\n1337\n");
        assert_eq!(pids, vec![4242, 1337]); // 0 filtered (not a real PID), garbage line skipped
    }

    #[test]
    fn parse_pgrep_pids_empty_when_no_matches() {
        assert!(parse_pgrep_pids("").is_empty());
        assert!(parse_pgrep_pids("\n\n  \n").is_empty());
    }

    #[test]
    fn reload_mcps_signals_all_matching_pids() {
        // Two patterns, each returning a different PID set. Expected:
        // we send SIGHUP to all 3 distinct PIDs, no errors.
        let mut runner = StubRunner::new();
        runner
            .pgrep_responses
            .insert("weaviate_mcp/server.py".to_string(), Ok("1111\n2222\n".into()));
        runner
            .pgrep_responses
            .insert("search_mcp/server.py".to_string(), Ok("3333\n".into()));

        let report = reload_mcps_with(&runner, MCP_PROCESS_PATTERNS);

        assert_eq!(report.signaled_count, 3);
        assert_eq!(report.pids, vec![1111, 2222, 3333]);
        assert!(report.errors.is_empty(), "errors: {:?}", report.errors);
        assert!(!report.posix_only_skipped);

        // Verify the kill stub recorded exactly the expected PIDs in
        // discovery order.
        let kills = runner.kill_calls.borrow().clone();
        assert_eq!(kills, vec![1111, 2222, 3333]);
    }

    #[test]
    fn reload_mcps_dedups_pids_across_patterns() {
        // A single MCP process could (hypothetically) match multiple
        // substrings. We must not send SIGHUP twice to the same PID.
        let mut runner = StubRunner::new();
        runner
            .pgrep_responses
            .insert("weaviate_mcp/server.py".to_string(), Ok("9999\n".into()));
        runner
            .pgrep_responses
            .insert("search_mcp/server.py".to_string(), Ok("9999\n".into()));

        let report = reload_mcps_with(&runner, MCP_PROCESS_PATTERNS);

        assert_eq!(report.pids, vec![9999]);
        assert_eq!(report.signaled_count, 1);
        assert_eq!(runner.kill_calls.borrow().clone(), vec![9999]);
    }

    #[test]
    fn reload_mcps_zero_pids_is_soft_success() {
        // No MCPs running yet. We report zero — NOT an error. This is
        // the common case during launcher boot before any Claude Code
        // session is open.
        let runner = StubRunner::new(); // no responses → all patterns return Ok("")
        let report = reload_mcps_with(&runner, MCP_PROCESS_PATTERNS);
        assert_eq!(report.signaled_count, 0);
        assert!(report.pids.is_empty());
        assert!(report.errors.is_empty());
    }

    #[test]
    fn reload_mcps_soft_fails_per_dead_pid() {
        // PID disappeared between pgrep and kill (race). One kill
        // failure does NOT abort the batch.
        let mut runner = StubRunner::new();
        runner
            .pgrep_responses
            .insert("weaviate_mcp/server.py".to_string(), Ok("4444\n5555\n".into()));
        runner
            .kill_failures
            .insert(4444, "No such process".to_string());

        let report = reload_mcps_with(&runner, MCP_PROCESS_PATTERNS);
        // Both PIDs were attempted; one failed (4444).
        assert_eq!(report.pids.len(), 2);
        assert_eq!(report.signaled_count, 1, "4444 failed, only 5555 succeeded");
        assert_eq!(report.errors.len(), 1);
        assert!(
            report.errors[0].contains("4444"),
            "error message must reference dead PID: {:?}",
            report.errors
        );
    }

    #[test]
    fn reload_mcps_records_pgrep_failure_without_aborting() {
        // One pattern's pgrep crashes; the other still works. We want
        // a partial-success report (not a fatal Err) so the watcher /
        // GUI can show "some MCPs signaled" rather than total failure.
        let mut runner = StubRunner::new();
        runner.pgrep_responses.insert(
            "weaviate_mcp/server.py".to_string(),
            Err("pgrep: command not found".into()),
        );
        runner
            .pgrep_responses
            .insert("search_mcp/server.py".to_string(), Ok("7777\n".into()));

        let report = reload_mcps_with(&runner, MCP_PROCESS_PATTERNS);
        assert_eq!(report.pids, vec![7777]);
        assert_eq!(report.signaled_count, 1);
        assert_eq!(report.errors.len(), 1);
        assert!(report.errors[0].contains("pgrep"));
    }

    #[test]
    fn reload_mcps_skips_own_pid() {
        // Defensive: if a pattern ever matches the launcher binary
        // itself, we MUST NOT send SIGHUP to our own process. The
        // launcher's default SIGHUP action is terminate.
        let own_pid = std::process::id();
        let mut runner = StubRunner::new();
        runner.pgrep_responses.insert(
            "weaviate_mcp/server.py".to_string(),
            Ok(format!("{}\n1234\n", own_pid)),
        );

        let report = reload_mcps_with(&runner, MCP_PROCESS_PATTERNS);
        // Own PID filtered out.
        assert_eq!(report.pids, vec![1234]);
        assert!(
            !runner.kill_calls.borrow().contains(&own_pid),
            "must not send SIGHUP to own PID {}",
            own_pid
        );
    }

    #[test]
    fn mcp_process_patterns_cover_shipped_servers() {
        // Regression guard: if a future PR adds a third MCP (or
        // renames an existing one), this assertion will remind us to
        // update MCP_PROCESS_PATTERNS or the watcher loses coverage.
        assert!(
            MCP_PROCESS_PATTERNS.iter().any(|p| p.contains("weaviate_mcp")),
            "weaviate_mcp pattern missing"
        );
        assert!(
            MCP_PROCESS_PATTERNS.iter().any(|p| p.contains("search_mcp")),
            "search_mcp pattern missing"
        );
        // Each pattern must be specific enough to NOT match unrelated
        // python scripts. `_mcp/server.py` is the discipline.
        for p in MCP_PROCESS_PATTERNS {
            assert!(
                p.ends_with("/server.py"),
                "pattern {:?} must end with /server.py for narrow matching",
                p
            );
        }
    }
}
