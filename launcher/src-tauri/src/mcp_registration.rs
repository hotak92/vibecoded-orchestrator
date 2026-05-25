//! Safe JSON editor for `~/.claude.json` and per-project `.mcp.json`.
//!
//! Concerns:
//!   1. Concurrent writes — lock via a sidecar `.lock` file.
//!   2. Data loss on crash — always write to `<file>.tmp` then rename.
//!   3. Schema preservation — read existing JSON, mutate `mcpServers.<name>`
//!      only, write back with the same indentation when possible.
//!
//! This module is NOT a generic JSON editor. It intentionally operates only
//! on the `mcpServers` object, leaving everything else in the file untouched.
//!
//! ## PR-23 (v0.2.12, 2026-05-16): default-orchestrator-MCP registration
//!
//! `register_default_orchestrator_mcps` constructs the canonical bundled
//! MCP entries (`weaviate-kg`, `search` — Ollama MCP was dropped from the
//! default install in v0.2.11; `vct-coordination` is Pro-tier and intentionally
//! omitted here) and writes them via `register_mcp` above. Same function is
//! invoked both from the launcher's `install_orchestrator()` Tauri command
//! AND from a thin CLI subcommand on the launcher binary
//! (`vct-launcher --register-default-mcps <install_root>`) which install.py
//! shells out to. Single writer == ~/.claude.json and the per-project
//! launcher.db stay in sync from minute zero of a fresh install.
//!
//! Security boundary: `~/.claude.json` is readable by every process
//! running as the same user, including Claude Code chat sessions that
//! are NOT launcher-managed. Therefore secret-shaped env keys (TOKEN,
//! SECRET, PAT, PASSWORD, AUTH, *_KEY) are silently dropped from the
//! written entry — they belong in `.claude/settings.json env` (per-project,
//! launcher-written) or in the OS keychain, never here.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

pub fn user_claude_json() -> PathBuf {
    directories::UserDirs::new()
        .map(|d| d.home_dir().join(".claude.json"))
        .unwrap_or_else(|| PathBuf::from(".claude.json"))
}

/// Resolve the project-scoped `.mcp.json` path for a given project folder.
///
/// Currently unused: the only caller was the archived `commands::mcp_reg`
/// Tauri wrapper. Restore from launch-assets/launcher-archived-rust/mcp_reg.rs
/// if/when a direct-from-FE registration path is wired up.
#[allow(dead_code)]
pub fn project_mcp_json(project_folder: &Path) -> PathBuf {
    project_folder.join(".mcp.json")
}

fn lock_path(target: &Path) -> PathBuf {
    let mut p = target.to_path_buf();
    p.set_extension(match target.extension().and_then(|s| s.to_str()) {
        Some(ext) => format!("{}.lock", ext),
        None => "lock".to_string(),
    });
    p
}

/// Acquire a simple advisory file lock. Blocks up to `max_wait_ms`.
/// Lock file content is the current PID.
fn acquire_lock(target: &Path, max_wait_ms: u64) -> Result<LockGuard, String> {
    let lock = lock_path(target);
    let start = std::time::Instant::now();
    loop {
        match fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&lock)
        {
            Ok(mut f) => {
                let _ = writeln!(f, "{}", std::process::id());
                return Ok(LockGuard {
                    path: lock,
                    _file: f,
                });
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                if start.elapsed().as_millis() as u64 > max_wait_ms {
                    // Stale lock? If the PID inside is not alive, remove it.
                    if let Ok(content) = fs::read_to_string(&lock) {
                        if let Ok(pid) = content.trim().parse::<u32>() {
                            if !pid_alive(pid) {
                                let _ = fs::remove_file(&lock);
                                continue;
                            }
                        }
                    }
                    return Err(format!(
                        "timed out waiting for lock {} (held by another process)",
                        lock.display()
                    ));
                }
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(e) => return Err(format!("acquire lock {}: {}", lock.display(), e)),
        }
    }
}

fn pid_alive(_pid: u32) -> bool {
    // Conservative: assume alive. The cost of a false "alive" is a slightly
    // longer wait; the cost of a false "dead" is corrupting someone's JSON.
    true
}

struct LockGuard {
    path: PathBuf,
    _file: fs::File,
}

impl Drop for LockGuard {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

/// Register an MCP server in a target JSON file.
///
/// `entry` is the full JSON object that will be stored at
/// `mcpServers[mcp_name]`. Existing entry is replaced.
///
/// Creates the file + the `mcpServers` object if absent.
pub fn register_mcp(
    target: &Path,
    mcp_name: &str,
    entry: &serde_json::Value,
) -> Result<(), String> {
    let _lock = acquire_lock(target, 5000)?;

    let mut root = read_json_or_empty(target)?;
    let mcp_servers = root
        .as_object_mut()
        .ok_or("target file root is not a JSON object")?;
    if !mcp_servers.contains_key("mcpServers") {
        mcp_servers.insert("mcpServers".into(), serde_json::json!({}));
    }
    let servers = mcp_servers
        .get_mut("mcpServers")
        .and_then(|v| v.as_object_mut())
        .ok_or("mcpServers is not an object")?;

    servers.insert(mcp_name.to_string(), entry.clone());
    atomic_write_json(target, &root)?;
    Ok(())
}

pub fn deregister_mcp(target: &Path, mcp_name: &str) -> Result<(), String> {
    if !target.exists() {
        return Ok(());
    }
    let _lock = acquire_lock(target, 5000)?;
    let mut root = read_json_or_empty(target)?;
    if let Some(obj) = root.as_object_mut() {
        if let Some(servers) = obj.get_mut("mcpServers").and_then(|v| v.as_object_mut()) {
            servers.remove(mcp_name);
        }
    }
    atomic_write_json(target, &root)?;
    Ok(())
}

fn read_json_or_empty(path: &Path) -> Result<serde_json::Value, String> {
    if !path.exists() {
        return Ok(serde_json::json!({}));
    }
    let raw = fs::read_to_string(path).map_err(|e| format!("read {}: {}", path.display(), e))?;
    if raw.trim().is_empty() {
        return Ok(serde_json::json!({}));
    }
    serde_json::from_str(&raw).map_err(|e| format!("parse {}: {}", path.display(), e))
}

fn atomic_write_json(path: &Path, value: &serde_json::Value) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("create parent {}: {}", parent.display(), e))?;
    }
    // Back up the existing file before overwriting so a bad write is recoverable.
    if path.exists() {
        let mut bak = path.to_path_buf();
        bak.set_extension(match path.extension().and_then(|s| s.to_str()) {
            Some(ext) => format!("{}.bak", ext),
            None => "bak".to_string(),
        });
        fs::copy(path, &bak).map_err(|e| format!("backup {}: {}", bak.display(), e))?;
    }
    let mut tmp = path.to_path_buf();
    tmp.set_extension(match path.extension().and_then(|s| s.to_str()) {
        Some(ext) => format!("{}.tmp", ext),
        None => "tmp".to_string(),
    });
    let body = serde_json::to_string_pretty(value)
        .map_err(|e| format!("serialize: {}", e))?;
    fs::write(&tmp, &body).map_err(|e| format!("write tmp {}: {}", tmp.display(), e))?;
    fs::rename(&tmp, path).map_err(|e| format!("rename {} -> {}: {}", tmp.display(), path.display(), e))?;
    Ok(())
}

// ═══════════════════════════════════════════════════════════════════════
// PR-23: default-orchestrator-MCP registration
// ═══════════════════════════════════════════════════════════════════════

/// Env keys that may appear in `~/.claude.json mcpServers.*.env`. Everything
/// else is silently dropped before write — see module-level docstring for
/// the secret-leak rationale. Per-project keys (KG_COLLECTION, PROJECT_NAME,
/// DEVELOPMENT_COLLECTION, SHARED_KG_COLLECTION, CODE_GRAPH_PROJECT,
/// KG_BASE_DIR) live in each project's `.claude/settings.json env` instead
/// (launcher writes them via `write_project_env_files`); they are
/// intentionally absent from this allowlist.
///
/// CRITICAL CONTRACT (Issue H.1 from mcp-instability audit 2026-05-16):
/// Anthropic semantics say "project scope overrides user scope" for env
/// vars, but Claude Code applies ~/.claude.json mcpServers.*.env keys
/// LAST to MCP subprocesses — so they WIN against .claude/settings.json
/// env. This is the wrong direction for any per-project-varying value.
/// Therefore this allowlist is restricted to keys that are TRULY
/// machine-invariant.
///
/// Removed in PR-43 (post-PR-23):
///   - RL_SERVER_URL: varies per user setup (VCO_dev: 11442, MAO: 11439,
///     etc.). Belongs in per-project .claude/settings.json env.
///   - EMBEDDING_MODEL: users may want per-project override; if it's in
///     this global allowlist, the per-project value gets overridden the
///     WRONG WAY due to Claude Code's precedence. MUST stay project-scoped.
///
/// MUST stay in sync with install.py::_ALLOWED_GLOBAL_ENV_KEYS.
const ALLOWED_ENV_KEYS: &[&str] = &[
    "WEAVIATE_URL",
    "OLLAMA_URL",
    "GRPC_PORT",
    "PYTHONPATH",
    "ACTIVE_EMBEDDING",
    "CODE_EMBED_SERVICE_URL",
];

/// Default Weaviate REST port (mirrors install.py:208 + installer.rs:477).
pub const DEFAULT_WEAVIATE_PORT: u16 = 8081;
/// Default Ollama API port (mirrors install.py:210 + installer.rs:478).
pub const DEFAULT_OLLAMA_PORT: u16 = 11435;
/// Default Weaviate gRPC port (mirrors install.py:209).
pub const DEFAULT_GRPC_PORT: u16 = 50052;
/// Default code-embedding service port (mirrors install.py:211).
pub const DEFAULT_CODE_EMBED_PORT: u16 = 11440;

/// Ports passed in from the caller (launcher GUI's adopted-services state
/// or install.py's env). Defaults match the canonical-port constants.
#[derive(Debug, Clone, Copy)]
pub struct ServicePorts {
    pub weaviate_port: u16,
    pub ollama_port: u16,
    pub grpc_port: u16,
    pub code_embed_port: u16,
}

impl Default for ServicePorts {
    fn default() -> Self {
        Self {
            weaviate_port: DEFAULT_WEAVIATE_PORT,
            ollama_port: DEFAULT_OLLAMA_PORT,
            grpc_port: DEFAULT_GRPC_PORT,
            code_embed_port: DEFAULT_CODE_EMBED_PORT,
        }
    }
}

/// Per-MCP outcome of the register step.
#[derive(Debug, Clone)]
pub struct McpRegisterOutcome {
    pub name: String,
    pub ok: bool,
    pub error: Option<String>,
    /// Env keys that were dropped because they matched the denylist or
    /// weren't in `ALLOWED_ENV_KEYS`. Surfaced for observability; tests
    /// assert that secret-shaped keys are listed here, not in the
    /// written JSON.
    pub dropped_keys: Vec<String>,
}

/// Aggregate report returned from `register_default_orchestrator_mcps`.
#[derive(Debug, Clone, Default)]
pub struct RegistrationReport {
    pub claude_json_path: PathBuf,
    pub outcomes: Vec<McpRegisterOutcome>,
    /// Soft-failures from the optional launcher.db sync step. Never fatal.
    pub db_warnings: Vec<String>,
}

impl RegistrationReport {
    pub fn all_succeeded(&self) -> bool {
        !self.outcomes.is_empty() && self.outcomes.iter().all(|o| o.ok)
    }
    pub fn success_count(&self) -> usize {
        self.outcomes.iter().filter(|o| o.ok).count()
    }
}

/// Resolve absolute venv-python path. Tries canonical `<install>/.venv`
/// first, falls back to legacy `<install>/claude_mcp_servers/.venv`.
/// Cross-OS: appends `Scripts/python.exe` on Windows, `bin/python` elsewhere.
pub fn resolve_venv_python(install_root: &Path) -> Option<PathBuf> {
    let (sub, py) = if cfg!(target_os = "windows") {
        ("Scripts", "python.exe")
    } else {
        ("bin", "python")
    };
    let candidates = [
        install_root.join(".venv").join(sub).join(py),
        install_root.join("claude_mcp_servers").join(".venv").join(sub).join(py),
    ];
    for c in candidates.iter() {
        if c.exists() {
            return Some(c.clone());
        }
    }
    None
}

/// True when `key` is a secret-shaped name we MUST NOT write into
/// ~/.claude.json. Case-insensitive. Matches the secret needles
/// (TOKEN, SECRET, PAT, PASSWORD, PASS, AUTH) as SEGMENTS between
/// `_` / `-` separators, not as raw substrings, to avoid false
/// positives like `PYTHONPATH` matching `PAT` or `COMPASS` matching
/// `PASS`. Additionally flags exact `KEY` and `*_KEY` (catches
/// `STRIPE_KEY`, `OPENAI_API_KEY`, etc.).
///
/// MUST stay in sync with the Python mirror in
/// install.py::_is_secret_shaped_env_key.
pub fn is_secret_shaped_env_key(key: &str) -> bool {
    let upper = key.to_ascii_uppercase();
    let needles = ["TOKEN", "SECRET", "PAT", "PASSWORD", "PASS", "AUTH"];
    // Split into segments on `_` and `-`, then check each segment for an
    // exact match against the needle list.
    let segments: Vec<&str> = upper.split(|c: char| c == '_' || c == '-').collect();
    for needle in needles.iter() {
        if segments.iter().any(|s| s == needle) {
            return true;
        }
    }
    upper == "KEY" || upper.ends_with("_KEY")
}

/// Filter a candidate env map down to the allowlist, dropping any
/// secret-shaped or non-allowlisted entries. Returns the safe map plus
/// the list of dropped keys (for the outcome report). Pure function;
/// the caller does the writing.
pub fn filter_env_for_global_json(
    candidate: &serde_json::Map<String, serde_json::Value>,
) -> (serde_json::Map<String, serde_json::Value>, Vec<String>) {
    let mut safe = serde_json::Map::new();
    let mut dropped = Vec::new();
    for (k, v) in candidate.iter() {
        if is_secret_shaped_env_key(k) {
            dropped.push(k.clone());
            continue;
        }
        if !ALLOWED_ENV_KEYS.iter().any(|allowed| *allowed == k.as_str()) {
            dropped.push(k.clone());
            continue;
        }
        safe.insert(k.clone(), v.clone());
    }
    (safe, dropped)
}

/// Build the canonical default-MCP entries for an install rooted at
/// `install_root`. Caller resolves the venv-python and ports; this
/// function just composes the entries (paths + filtered env). Returns a
/// vec of `(mcp_name, entry_json, dropped_keys)`.
///
/// Why this is a separate pure function: makes the entry shape easy to
/// unit-test in Rust AND mirror in `tests/test_install_mcp_registration.py`
/// without spinning up an actual file write.
pub fn build_default_mcp_entries(
    install_root: &Path,
    venv_python: &Path,
    ports: ServicePorts,
) -> Vec<(String, serde_json::Value, Vec<String>)> {
    let weaviate_url = format!("http://localhost:{}", ports.weaviate_port);
    let ollama_url = format!("http://localhost:{}", ports.ollama_port);
    let code_embed_url = format!("http://localhost:{}", ports.code_embed_port);
    let mcp_root = install_root.join("claude_mcp_servers");
    let pythonpath = mcp_root.display().to_string();
    let venv_python_str = venv_python.display().to_string();

    // ── weaviate-kg ─────────────────────────────────────────────────────
    let weaviate_server = mcp_root.join("weaviate_mcp").join("server.py");
    // PR-43 (post-PR-23): EMBEDDING_MODEL + RL_SERVER_URL are intentionally
    // omitted here. They were originally written as "global defaults that
    // per-project may override" but Claude Code's actual env precedence
    // makes ~/.claude.json mcpServers.*.env WIN against
    // .claude/settings.json env — so the override goes the wrong direction.
    // The launcher's write_project_env_files puts these in
    // .claude/settings.json env where they reach MCP subprocesses
    // correctly. Don't shadow them here.
    let mut weaviate_env = serde_json::Map::new();
    weaviate_env.insert("WEAVIATE_URL".into(), weaviate_url.clone().into());
    weaviate_env.insert("OLLAMA_URL".into(), ollama_url.clone().into());
    weaviate_env.insert("GRPC_PORT".into(), ports.grpc_port.to_string().into());
    weaviate_env.insert("PYTHONPATH".into(), pythonpath.clone().into());
    weaviate_env.insert("ACTIVE_EMBEDDING".into(), "qwen3".into());
    weaviate_env.insert("CODE_EMBED_SERVICE_URL".into(), code_embed_url.into());
    let (weaviate_env_safe, weaviate_dropped) = filter_env_for_global_json(&weaviate_env);
    let weaviate_entry = serde_json::json!({
        "type": "stdio",
        "command": venv_python_str.clone(),
        "args": [weaviate_server.display().to_string()],
        "env": serde_json::Value::Object(weaviate_env_safe),
    });

    // ── search ──────────────────────────────────────────────────────────
    // v0.2.11 search MCP needs no secrets (SEARXNG / GITHUB_TOKEN removed)
    // but we still go through the wrapper.sh on Unix for backward-compat
    // with anything that exports its own GITHUB_TOKEN or OPENALEX_EMAIL.
    // On Windows there is no wrapper.sh, so invoke python directly.
    let search_server = mcp_root.join("search_mcp").join("server.py");
    let search_wrapper = mcp_root.join("search_mcp").join("wrapper.sh");
    let (search_cmd, search_args) = if cfg!(target_os = "windows") {
        (
            venv_python_str.clone(),
            vec![search_server.display().to_string()],
        )
    } else {
        (search_wrapper.display().to_string(), Vec::<String>::new())
    };
    let mut search_env = serde_json::Map::new();
    search_env.insert("PYTHONPATH".into(), pythonpath.clone().into());
    let (search_env_safe, search_dropped) = filter_env_for_global_json(&search_env);
    let search_entry = serde_json::json!({
        "type": "stdio",
        "command": search_cmd,
        "args": search_args,
        "env": serde_json::Value::Object(search_env_safe),
    });

    // ── mermaid (Phase 1.2 — wrapper MCP) ───────────────────────────
    // The wrapper proxies the npm `claude-mermaid` package and filters
    // its tool surface per-project. Spawn command: `<venv-python> -m
    // claude_mcp_servers.wrappers.mermaid_proxy`. NOT a direct `npx`
    // invocation — the wrapper is the entry point; it spawns `npx` as
    // its own child once it's resolved the per-project allowlist.
    //
    // Default-disabled per project (see `BUNDLED_MCP_DEFAULT_DISABLED`
    // in vct-launcher-core/src/db/project_mcp_servers.rs). The user
    // opts in via the launcher's DiagramsTab. Until opted in the entry
    // sits in ~/.claude.json but the launcher's per-project gate keeps
    // Claude Code from spawning it.
    let mut mermaid_env = serde_json::Map::new();
    mermaid_env.insert("PYTHONPATH".into(), pythonpath.clone().into());
    let (mermaid_env_safe, mermaid_dropped) = filter_env_for_global_json(&mermaid_env);
    let mermaid_entry = serde_json::json!({
        "type": "stdio",
        "command": venv_python_str.clone(),
        "args": [
            "-m",
            "claude_mcp_servers.wrappers.mermaid_proxy",
        ],
        "env": serde_json::Value::Object(mermaid_env_safe),
    });

    // ── excalidraw (Phase 2 — wrapper MCP) ──────────────────────────
    // The wrapper proxies the in-tree-vendored `excalidraw-mcp-server`
    // (see claude_mcp_servers/excalidraw_mcp_fork/VENDORED.md) and
    // filters its tool surface per-project. Spawn command:
    // `<venv-python> -m claude_mcp_servers.wrappers.excalidraw_proxy`.
    // The wrapper itself spawns Node on the vendored entry point
    // once it's resolved the per-project allowlist.
    //
    // Default-disabled per project (see `BUNDLED_MCP_DEFAULT_DISABLED`
    // in vct-launcher-core/src/db/project_mcp_servers.rs). The user
    // opts in via the launcher's DiagramsTab. Until opted in the entry
    // sits in ~/.claude.json but the launcher's per-project gate keeps
    // Claude Code from spawning it. Same posture as Mermaid above.
    let mut excalidraw_env = serde_json::Map::new();
    excalidraw_env.insert("PYTHONPATH".into(), pythonpath.clone().into());
    let (excalidraw_env_safe, excalidraw_dropped) =
        filter_env_for_global_json(&excalidraw_env);
    let excalidraw_entry = serde_json::json!({
        "type": "stdio",
        "command": venv_python_str.clone(),
        "args": [
            "-m",
            "claude_mcp_servers.wrappers.excalidraw_proxy",
        ],
        "env": serde_json::Value::Object(excalidraw_env_safe),
    });

    vec![
        ("weaviate-kg".to_string(), weaviate_entry, weaviate_dropped),
        ("search".to_string(), search_entry, search_dropped),
        ("mermaid".to_string(), mermaid_entry, mermaid_dropped),
        ("excalidraw".to_string(), excalidraw_entry, excalidraw_dropped),
    ]
}

/// Register the canonical bundled-orchestrator MCPs in `~/.claude.json`
/// (or `claude_json_override` for tests). Soft-fail: each MCP is
/// registered independently; a failure on one does not block the others.
///
/// Optional second step: if `db` is provided AND a project row exists
/// with `folder_path == install_root`, ALSO upsert each MCP into that
/// project's row in `project_mcp_servers`. This keeps the JSON and the
/// launcher DB in sync from minute zero. If no matching project row
/// exists (typical at fresh install time — the user hasn't registered
/// a project yet), the DB step is silently skipped; later when the
/// user opens the GUI, `project_state_populate` will pick up the
/// already-written `.claude/settings.json mcpServers` rows.
pub fn register_default_orchestrator_mcps(
    install_root: &Path,
    ports: ServicePorts,
    claude_json_override: Option<&Path>,
    db: Option<&crate::db::Db>,
) -> Result<RegistrationReport, String> {
    let claude_json = claude_json_override
        .map(PathBuf::from)
        .unwrap_or_else(user_claude_json);

    let venv_python = resolve_venv_python(install_root);
    let mut report = RegistrationReport {
        claude_json_path: claude_json.clone(),
        outcomes: Vec::new(),
        db_warnings: Vec::new(),
    };

    let py = match venv_python.as_ref() {
        Some(p) => p.clone(),
        None => {
            return Err(format!(
                "no venv-python found under {} (tried .venv and claude_mcp_servers/.venv)",
                install_root.display()
            ));
        }
    };

    let entries = build_default_mcp_entries(install_root, &py, ports);

    for (name, entry, dropped) in entries {
        let outcome = match register_mcp(&claude_json, &name, &entry) {
            Ok(()) => McpRegisterOutcome {
                name: name.clone(),
                ok: true,
                error: None,
                dropped_keys: dropped.clone(),
            },
            Err(e) => McpRegisterOutcome {
                name: name.clone(),
                ok: false,
                error: Some(e),
                dropped_keys: dropped.clone(),
            },
        };
        // Surface any dropped keys as a soft warning so callers see them
        // in logs without parsing the structured report.
        if !outcome.dropped_keys.is_empty() {
            eprintln!(
                "[vct] register_default_orchestrator_mcps: dropped {} env key(s) \
                 from `{}` entry (allowlist/secret-shape filter): {:?}",
                outcome.dropped_keys.len(),
                name,
                outcome.dropped_keys
            );
        }
        report.outcomes.push(outcome);

        // Optional DB sync. Look up project_id by folder_path.
        if let Some(db) = db {
            if let Some(project_id) = find_project_id_for_folder(db, install_root) {
                // Build a tiny config blob mirroring what the populate
                // step does: top-level command + the full entry under
                // config_json. is_user_added=false (bundled).
                let command = entry
                    .get("command")
                    .and_then(|v| v.as_str());
                if let Err(e) = db.register_project_mcp_server(
                    &project_id,
                    &name,
                    false,
                    "bundled",
                    None,
                    Some("install.py:_register_mcps"),
                    command,
                    &entry,
                ) {
                    report.db_warnings.push(format!(
                        "register_project_mcp_server({}/{}): {}",
                        project_id, name, e
                    ));
                }
            }
        }
    }

    Ok(report)
}

// ═══════════════════════════════════════════════════════════════════════
// PR-33 (v0.2.12, 2026-05-16): consent-prompted rewrite of stale entries
// ═══════════════════════════════════════════════════════════════════════
//
// Detection (PR-23) is unconditional on --update. Rewrite (PR-33) is
// behind an explicit --rewrite-stale-mcps flag on install.py AND
// requires per-entry consent on the Python side (the launcher binary's
// `--register-default-mcps --rewrite` subcommand is invoked AFTER the
// Python side has gathered consent; the Rust function below does not
// re-prompt). Two-level backup is the Python side's responsibility.
//
// Stale-entry detection here uses the same anchor as the install.py
// mirror (`_scan_stale_mcp_entries`): only flag paths that look like
// vco install layouts (claude_mcp_servers/ or .venv/ tokens). User-added
// MCPs at /usr/bin/foo are not classified as orchestrator-stale.

/// One entry the scan classified as stale.
#[derive(Debug, Clone)]
pub struct StaleMcpEntry {
    pub name: String,
    pub stale_path: String,
    /// Env keys on the existing entry that will NOT survive a rewrite
    /// (because they fail the global-JSON allowlist or match the
    /// secret-shape denylist). Surfaced for observability; the consent
    /// prompt on the Python side warns the user about these.
    pub dropping_env_keys: Vec<String>,
}

/// Aggregate report from `rewrite_stale_orchestrator_mcps`.
#[derive(Debug, Clone, Default)]
pub struct RewriteReport {
    pub stale_entries_found: Vec<StaleMcpEntry>,
    pub rewritten: Vec<String>,
    pub skipped_non_bundled: Vec<String>,
    /// Underlying RegistrationReport from the actual writer (when we
    /// invoked it). None when the scan found no stale entries OR when
    /// the caller didn't accept any.
    pub registration: Option<RegistrationReport>,
}

/// Scan `~/.claude.json` (or `claude_json_override`) for `mcpServers`
/// entries whose `command` or `args[0]` points at a vco-install-shaped
/// path OUTSIDE `install_root`. Returns the list of `StaleMcpEntry`,
/// each annotated with the env keys that would be dropped on rewrite.
///
/// Pure read; never mutates the file.
pub fn scan_stale_mcp_entries(
    install_root: &Path,
    claude_json_override: Option<&Path>,
) -> Vec<StaleMcpEntry> {
    let claude_json = claude_json_override
        .map(PathBuf::from)
        .unwrap_or_else(user_claude_json);
    if !claude_json.is_file() {
        return Vec::new();
    }
    let raw = match fs::read_to_string(&claude_json) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };
    let data: serde_json::Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    let mcp_servers = match data.get("mcpServers").and_then(|v| v.as_object()) {
        Some(m) => m,
        None => return Vec::new(),
    };

    let install_root_str = match fs::canonicalize(install_root) {
        Ok(p) => p.display().to_string(),
        Err(_) => install_root.display().to_string(),
    };

    let mut stale = Vec::new();
    for (name, entry) in mcp_servers.iter() {
        let entry_obj = match entry.as_object() {
            Some(o) => o,
            None => continue,
        };
        let cmd = entry_obj
            .get("command")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let first_arg = entry_obj
            .get("args")
            .and_then(|v| v.as_array())
            .and_then(|arr| arr.first())
            .and_then(|v| v.as_str())
            .unwrap_or("");
        for candidate in [cmd, first_arg].iter() {
            if candidate.is_empty() {
                continue;
            }
            // Absolute-path heuristic (cross-OS).
            let is_abs = candidate.starts_with('/')
                || candidate.starts_with("C:\\")
                || candidate.starts_with("c:\\")
                || candidate.starts_with("\\\\");
            if !is_abs {
                continue;
            }
            // Anchor on vco install tokens.
            if !candidate.contains("claude_mcp_servers") && !candidate.contains(".venv") {
                continue;
            }
            if !candidate.starts_with(&install_root_str) {
                // Compute env keys that would be dropped on rewrite.
                let mut dropping = Vec::new();
                if let Some(env) = entry_obj.get("env").and_then(|v| v.as_object()) {
                    for k in env.keys() {
                        if is_secret_shaped_env_key(k)
                            || !ALLOWED_ENV_KEYS.iter().any(|a| *a == k.as_str())
                        {
                            dropping.push(k.clone());
                        }
                    }
                }
                stale.push(StaleMcpEntry {
                    name: name.clone(),
                    stale_path: (*candidate).to_string(),
                    dropping_env_keys: dropping,
                });
                break;
            }
        }
    }
    stale
}

/// Consent-prompted rewrite path. Caller (the Python install.py side
/// OR a launcher CLI subcommand) is responsible for gathering per-entry
/// consent BEFORE invoking this function; we do not prompt in Rust.
///
/// When `accept_names` is empty, this function is effectively a scan +
/// report — no writes happen. When non-empty, the named entries are
/// rewritten via `register_default_orchestrator_mcps`, which means the
/// env-key allowlist + secret-shaped-key denylist are applied uniformly
/// (stale env values like a hand-edited GITHUB_TOKEN are dropped).
///
/// Soft-fail: a missing venv-python returns Err — the caller (install.py)
/// downgrades that to a warning and emits a deferral, just like the
/// fresh-install path.
pub fn rewrite_stale_orchestrator_mcps(
    install_root: &Path,
    ports: ServicePorts,
    claude_json_override: Option<&Path>,
    db: Option<&crate::db::Db>,
    accept_names: &[String],
) -> Result<RewriteReport, String> {
    let claude_json = claude_json_override
        .map(PathBuf::from)
        .unwrap_or_else(user_claude_json);
    let stale = scan_stale_mcp_entries(install_root, Some(&claude_json));
    let mut report = RewriteReport {
        stale_entries_found: stale.clone(),
        rewritten: Vec::new(),
        skipped_non_bundled: Vec::new(),
        registration: None,
    };
    if stale.is_empty() {
        return Ok(report);
    }
    if accept_names.is_empty() {
        // Scan-only call (no consent gathered yet).
        return Ok(report);
    }

    // Partition accepted names into bundled (writer can touch) and
    // non-bundled (writer leaves alone). The bundled set must stay in
    // sync with `build_default_mcp_entries` — currently weaviate-kg
    // and search.
    let bundled = ["weaviate-kg", "search"];
    let mut accept_bundled = false;
    for name in accept_names {
        if bundled.iter().any(|b| *b == name.as_str()) {
            accept_bundled = true;
            report.rewritten.push(name.clone());
        } else {
            report.skipped_non_bundled.push(name.clone());
        }
    }

    if accept_bundled {
        let reg = register_default_orchestrator_mcps(
            install_root,
            ports,
            Some(&claude_json),
            db,
        )?;
        report.registration = Some(reg);
    }
    Ok(report)
}

/// Look up the project_id whose folder_path matches `target` (canonical
/// path comparison after `canonicalize`). Returns None when no project
/// is registered yet — which is the common case at fresh-install time.
fn find_project_id_for_folder(db: &crate::db::Db, target: &Path) -> Option<String> {
    let canon_target = fs::canonicalize(target).ok()?;
    let projects = db.list_projects().ok()?;
    for p in projects {
        let folder = PathBuf::from(&p.folder_path);
        if let Ok(canon) = fs::canonicalize(&folder) {
            if canon == canon_target {
                return Some(p.id);
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn tmp_target() -> PathBuf {
        let p = std::env::temp_dir().join(format!(
            "vct-mcp-reg-test-{}.json",
            uuid::Uuid::new_v4().simple()
        ));
        // Don't create the file — register_mcp is supposed to handle absence.
        p
    }

    #[test]
    fn register_creates_file_and_writes_mcpservers_block() {
        let target = tmp_target();
        assert!(!target.exists());

        // Use a cross-OS path stub for the test — the real registrar will resolve
        // a real venv path; here we just verify the registrar writes whatever
        // command we pass through unchanged.
        let test_command = if cfg!(target_os = "windows") {
            "C:\\Python\\python.exe"
        } else {
            "/usr/bin/python3"
        };
        let entry = serde_json::json!({
            "type": "stdio",
            "command": test_command,
            "args": ["server.py"],
            "env": {"FOO": "bar"},
        });

        register_mcp(&target, "my-mcp", &entry).expect("register_mcp");

        let raw = fs::read_to_string(&target).expect("read back");
        let json: serde_json::Value = serde_json::from_str(&raw).unwrap();

        // Bug 6 contract: server lives under mcpServers.<id> with the
        // exact entry block we passed in. Earlier `add_custom_mcp_server`
        // only wrote `env` keys to settings.json, which Claude Code does
        // not read.
        let server = &json["mcpServers"]["my-mcp"];
        assert_eq!(server["type"], "stdio");
        assert_eq!(server["command"], test_command);
        assert_eq!(server["args"][0], "server.py");
        assert_eq!(server["env"]["FOO"], "bar");

        fs::remove_file(&target).ok();
    }

    #[test]
    fn register_then_deregister_removes_only_named_entry() {
        let target = tmp_target();
        let a = serde_json::json!({"type": "stdio", "command": "a"});
        let b = serde_json::json!({"type": "stdio", "command": "b"});
        register_mcp(&target, "alpha", &a).unwrap();
        register_mcp(&target, "beta", &b).unwrap();

        deregister_mcp(&target, "alpha").unwrap();

        let raw = fs::read_to_string(&target).unwrap();
        let json: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert!(json["mcpServers"].get("alpha").is_none());
        assert_eq!(json["mcpServers"]["beta"]["command"], "b");

        fs::remove_file(&target).ok();
    }

    // ── PR-23 tests: default-orchestrator-MCP registration ─────────────

    /// Build a temp pseudo-install layout with a fake venv-python so
    /// `resolve_venv_python` / `register_default_orchestrator_mcps`
    /// see something at the canonical path. Returns the install root.
    fn make_pseudo_install_root() -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "vct-pseudo-install-{}",
            uuid::Uuid::new_v4().simple()
        ));
        let (sub, py) = if cfg!(target_os = "windows") {
            ("Scripts", "python.exe")
        } else {
            ("bin", "python")
        };
        let venv_bin = root.join(".venv").join(sub);
        fs::create_dir_all(&venv_bin).unwrap();
        fs::write(venv_bin.join(py), b"#!/bin/sh\nexit 0\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let p = venv_bin.join(py);
            let mut perms = fs::metadata(&p).unwrap().permissions();
            perms.set_mode(0o755);
            fs::set_permissions(&p, perms).unwrap();
        }
        // Also create the MCP server dirs so paths look plausible.
        fs::create_dir_all(root.join("claude_mcp_servers/weaviate_mcp")).unwrap();
        fs::create_dir_all(root.join("claude_mcp_servers/search_mcp")).unwrap();
        // wrapper.sh placeholder (Unix only — Windows path uses python directly).
        #[cfg(not(target_os = "windows"))]
        fs::write(
            root.join("claude_mcp_servers/search_mcp/wrapper.sh"),
            b"#!/usr/bin/env bash\nexit 0\n",
        )
        .unwrap();
        root
    }

    #[test]
    fn secret_shaped_keys_are_detected() {
        // Positive cases — every one of these MUST be filtered out.
        for k in [
            "GITHUB_TOKEN",
            "github_token",
            "OPENAI_API_KEY",
            "MY_PAT",
            "SECRET_VALUE",
            "PASSWORD",
            "DB_PASS",
            "AUTH_HEADER",
            "KEY",
            "STRIPE_KEY",
            "my_key",
        ] {
            assert!(
                is_secret_shaped_env_key(k),
                "expected `{}` to be flagged as secret-shaped",
                k
            );
        }
        // Negative cases — these are NOT secrets and must pass through.
        for k in [
            "WEAVIATE_URL",
            "OLLAMA_URL",
            "PYTHONPATH",
            "KG_COLLECTION",
            "KG_BASE_DIR",
            "ACTIVE_EMBEDDING",
            "EMBEDDING_MODEL",
            "CODE_EMBED_SERVICE_URL",
            "RL_SERVER_URL",
            "GRPC_PORT",
        ] {
            assert!(
                !is_secret_shaped_env_key(k),
                "expected `{}` to be allowed (not secret-shaped)",
                k
            );
        }
    }

    #[test]
    fn filter_env_drops_secrets_and_unallowlisted() {
        let mut input = serde_json::Map::new();
        input.insert("WEAVIATE_URL".into(), "http://localhost:8081".into());
        input.insert("OLLAMA_URL".into(), "http://localhost:11435".into());
        input.insert("GITHUB_TOKEN".into(), "ghp_super_secret".into());
        input.insert("OPENAI_API_KEY".into(), "sk-xyz".into());
        // Per-project keys: not in allowlist, even though not secret-shaped.
        input.insert("KG_COLLECTION".into(), "FooProj_KG".into());
        input.insert("PROJECT_NAME".into(), "Foo".into());

        let (safe, dropped) = filter_env_for_global_json(&input);

        // Allowlisted keys are kept.
        assert_eq!(safe.get("WEAVIATE_URL").and_then(|v| v.as_str()), Some("http://localhost:8081"));
        assert_eq!(safe.get("OLLAMA_URL").and_then(|v| v.as_str()), Some("http://localhost:11435"));
        // Secrets are dropped.
        assert!(safe.get("GITHUB_TOKEN").is_none(), "GITHUB_TOKEN must NOT survive the filter");
        assert!(safe.get("OPENAI_API_KEY").is_none(), "OPENAI_API_KEY must NOT survive the filter");
        assert!(dropped.contains(&"GITHUB_TOKEN".to_string()));
        assert!(dropped.contains(&"OPENAI_API_KEY".to_string()));
        // Per-project non-secret keys are also dropped from the global file.
        assert!(safe.get("KG_COLLECTION").is_none(), "KG_COLLECTION belongs in .claude/settings.json env");
        assert!(safe.get("PROJECT_NAME").is_none(), "PROJECT_NAME belongs in .claude/settings.json env");
        assert!(dropped.contains(&"KG_COLLECTION".to_string()));
        assert!(dropped.contains(&"PROJECT_NAME".to_string()));
    }

    #[test]
    fn build_default_mcp_entries_shape_is_correct() {
        let root = make_pseudo_install_root();
        let py = resolve_venv_python(&root).expect("pseudo venv-python");
        let entries = build_default_mcp_entries(&root, &py, ServicePorts::default());

        let names: Vec<&str> = entries.iter().map(|(n, _, _)| n.as_str()).collect();
        // Note: Ollama MCP was dropped from the default install in v0.2.11
        // (see install.py:_check_ollama_mcp_remnants); we explicitly do NOT
        // include it here. vct-coordination is Pro-tier and likewise excluded.
        // Phase 1.2 (diagrams plan): mermaid wrapper appended.
        // Phase 2 (diagrams plan): excalidraw wrapper appended.
        assert_eq!(
            names,
            vec!["weaviate-kg", "search", "mermaid", "excalidraw"]
        );

        // ── weaviate-kg shape ────────────────────────────────────────
        let (_, weaviate, _) = &entries[0];
        assert_eq!(weaviate["type"], "stdio");
        assert_eq!(weaviate["command"], py.display().to_string());
        let weaviate_args = weaviate["args"].as_array().unwrap();
        assert_eq!(weaviate_args.len(), 1);
        assert!(
            weaviate_args[0]
                .as_str()
                .unwrap()
                .ends_with("weaviate_mcp/server.py")
                || weaviate_args[0]
                    .as_str()
                    .unwrap()
                    .ends_with("weaviate_mcp\\server.py")
        );
        assert_eq!(weaviate["env"]["WEAVIATE_URL"], "http://localhost:8081");
        assert_eq!(weaviate["env"]["GRPC_PORT"], "50052");
        assert!(
            weaviate["env"].get("KG_COLLECTION").is_none(),
            "KG_COLLECTION must NOT be written to ~/.claude.json — per-project keys belong in .claude/settings.json env"
        );
        assert!(weaviate["env"].get("GITHUB_TOKEN").is_none(), "secrets MUST be absent");

        // ── search shape ──────────────────────────────────────────────
        let (_, search, _) = &entries[1];
        assert_eq!(search["type"], "stdio");
        if cfg!(target_os = "windows") {
            assert_eq!(search["command"], py.display().to_string());
            assert_eq!(search["args"].as_array().unwrap().len(), 1);
        } else {
            assert!(
                search["command"]
                    .as_str()
                    .unwrap()
                    .ends_with("search_mcp/wrapper.sh"),
                "Unix search MCP must use wrapper.sh"
            );
            assert_eq!(search["args"].as_array().unwrap().len(), 0);
        }

        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn register_default_orchestrator_mcps_writes_canonical_entries() {
        let root = make_pseudo_install_root();
        let target = tmp_target();

        let report = register_default_orchestrator_mcps(
            &root,
            ServicePorts::default(),
            Some(&target),
            None, // No DB sync in this test.
        )
        .expect("register_default_orchestrator_mcps");

        assert!(report.all_succeeded(), "report.outcomes: {:?}", report.outcomes);
        // Phase 1.2 (diagrams plan): mermaid wrapper added → 3 entries.
        // Phase 2 (diagrams plan): excalidraw wrapper added → 4 entries.
        // Prior: weaviate-kg + search (2). Both wrappers register in
        // ~/.claude.json but are default-DISABLED at the per-project
        // gate (see BUNDLED_MCP_DEFAULT_DISABLED in vct-launcher-core).
        assert_eq!(report.success_count(), 4);

        let raw = fs::read_to_string(&target).unwrap();
        let json: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert!(json["mcpServers"]["weaviate-kg"].is_object());
        assert!(json["mcpServers"]["search"].is_object());
        assert!(
            json["mcpServers"]["mermaid"].is_object(),
            "mermaid wrapper MCP must be registered in ~/.claude.json"
        );
        assert!(
            json["mcpServers"]["excalidraw"].is_object(),
            "excalidraw wrapper MCP must be registered in ~/.claude.json"
        );
        // ollama MCP must NOT be written (deprecated in v0.2.11).
        assert!(
            json["mcpServers"].get("ollama").is_none(),
            "Ollama MCP was dropped from default install in v0.2.11 and must NOT be auto-registered"
        );
        // Mermaid entry points at the wrapper module, NOT direct npx.
        // The wrapper internally spawns `npx -y claude-mermaid@<pin>`.
        let mermaid_args = json["mcpServers"]["mermaid"]["args"]
            .as_array()
            .expect("mermaid.args is an array");
        assert_eq!(mermaid_args[0], "-m");
        assert_eq!(
            mermaid_args[1], "claude_mcp_servers.wrappers.mermaid_proxy",
            "mermaid MCP must point at the wrapper module, not direct npx"
        );
        // Excalidraw entry points at the wrapper module, NOT direct node.
        // The wrapper internally spawns Node on the vendored entry point.
        let excalidraw_args = json["mcpServers"]["excalidraw"]["args"]
            .as_array()
            .expect("excalidraw.args is an array");
        assert_eq!(excalidraw_args[0], "-m");
        assert_eq!(
            excalidraw_args[1], "claude_mcp_servers.wrappers.excalidraw_proxy",
            "excalidraw MCP must point at the wrapper module, not direct node"
        );

        fs::remove_file(&target).ok();
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn register_returns_error_when_no_venv_python() {
        // Empty temp dir — no .venv/ exists.
        let root = std::env::temp_dir().join(format!(
            "vct-empty-install-{}",
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir_all(&root).unwrap();
        let target = tmp_target();
        let err = register_default_orchestrator_mcps(
            &root,
            ServicePorts::default(),
            Some(&target),
            None,
        )
        .unwrap_err();
        assert!(
            err.contains("no venv-python"),
            "expected 'no venv-python' in error, got: {}",
            err
        );
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn register_preserves_unrelated_top_level_keys_when_writing_defaults() {
        let root = make_pseudo_install_root();
        let target = tmp_target();
        fs::write(
            &target,
            serde_json::to_string_pretty(&serde_json::json!({
                "permissions": {"allow": ["Read"]},
                "mcpServers": {"my-user-mcp": {"command": "/usr/bin/my-mcp"}},
            }))
            .unwrap(),
        )
        .unwrap();

        register_default_orchestrator_mcps(&root, ServicePorts::default(), Some(&target), None)
            .unwrap();

        let raw = fs::read_to_string(&target).unwrap();
        let json: serde_json::Value = serde_json::from_str(&raw).unwrap();
        // User's pre-existing MCP must survive.
        assert_eq!(json["mcpServers"]["my-user-mcp"]["command"], "/usr/bin/my-mcp");
        // User's pre-existing top-level key must survive.
        assert_eq!(json["permissions"]["allow"][0], "Read");
        // Orchestrator MCPs were added.
        assert!(json["mcpServers"]["weaviate-kg"].is_object());
        assert!(json["mcpServers"]["search"].is_object());

        fs::remove_file(&target).ok();
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn register_preserves_existing_top_level_keys() {
        let target = tmp_target();
        // Pre-seed the file with unrelated user config — register_mcp
        // must NOT clobber it.
        fs::write(
            &target,
            serde_json::to_string_pretty(&serde_json::json!({
                "permissions": {"allow": ["Read", "Edit"]},
                "feedbackSurveyState": {"lastShownTime": 1234567890},
            }))
            .unwrap(),
        )
        .unwrap();

        let entry = serde_json::json!({"type": "stdio", "command": "x"});
        register_mcp(&target, "x", &entry).unwrap();

        let raw = fs::read_to_string(&target).unwrap();
        let json: serde_json::Value = serde_json::from_str(&raw).unwrap();

        // Existing keys preserved
        assert_eq!(json["permissions"]["allow"][0], "Read");
        assert_eq!(json["feedbackSurveyState"]["lastShownTime"], 1234567890);
        // New key added
        assert_eq!(json["mcpServers"]["x"]["command"], "x");

        fs::remove_file(&target).ok();
    }
}
