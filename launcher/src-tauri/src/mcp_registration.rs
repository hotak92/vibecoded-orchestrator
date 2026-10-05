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
//! MCP entries (`weaviate-kg`, `playwright` — Ollama was dropped in v0.2.11,
//! `search` was deleted and the diagram wrappers `mermaid`/`excalidraw`
//! retired from default shipping in v0.2.101; `vct-coordination` is Pro-tier
//! and intentionally omitted here) and writes them via `register_mcp` above.
//! Same function is
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
use std::path::{Path, PathBuf};

// The lock / read / atomic-write primitives below used to live in this file.
// They moved to `crate::json_file` when a second user-owned-JSON call-site
// appeared (`commands::artifact_tool`, which edits `~/.claude/settings.json`);
// extracting was the alternative to a second copy drifting from this one.
use crate::json_file::{acquire_lock, atomic_write_json, read_json_or_empty, BackupPolicy};

/// Backup policy for `~/.claude.json`: refreshed on every write. This file is
/// rewritten routinely (every MCP registration pass), and the useful recovery
/// target is "the bytes from just before the write that broke it".
const CLAUDE_JSON_BACKUP: BackupPolicy = BackupPolicy::EveryWrite { ext: "bak" };

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
    atomic_write_json(target, &root, CLAUDE_JSON_BACKUP)?;
    Ok(())
}

/// The names of every MCP entry registered in a `~/.claude.json`-shaped
/// file's `mcpServers` map (empty when the file or map is absent). One
/// home for this read: the GUI's server list uses it to surface legacy
/// wrapper entries (v0.2.101 — registered-but-uncataloged) for the
/// per-project toggle.
pub fn registered_mcp_names(claude_json: &Path) -> Result<Vec<String>, String> {
    if !claude_json.exists() {
        return Ok(Vec::new());
    }
    let root = crate::json_file::read_json_or_empty(claude_json)?;
    let names = root
        .get("mcpServers")
        .and_then(|v| v.as_object())
        .map(|obj| obj.keys().cloned().collect())
        .unwrap_or_default();
    Ok(names)
}

pub fn deregister_mcp(target: &Path, mcp_name: &str) -> Result<(), String> {
    if !target.exists() {
        return Ok(());
    }
    let _lock = acquire_lock(target, 5000)?;
    let mut root = read_json_or_empty(target)?;
    if let Some(obj) = root.as_object_mut() {
        if let Some(servers) = obj.get_mut("mcpServers").and_then(|v| v.as_object_mut()) {
            // `shift_remove`, never `remove`: under `preserve_order` (enabled
            // workspace-wide since v0.2.92) `Map::remove` is `swap_remove`,
            // which would move the LAST registered server into the vacated
            // slot. `~/.claude.json` is the user's file; deregistering one MCP
            // must not relocate an unrelated one.
            servers.shift_remove(mcp_name);
        }
    }
    atomic_write_json(target, &root, CLAUDE_JSON_BACKUP)?;
    Ok(())
}

// ═══════════════════════════════════════════════════════════════════════
// v0.2.101: per-project MCP opt-out (the channel Claude Code honours)
// ═══════════════════════════════════════════════════════════════════════
//
// `~/.claude.json mcpServers` is USER-scope: every entry there spawns in
// every project's session. The launcher's per-project MCP toggle used to
// write only launcher.db, so "disabled for this project" never reached
// Claude Code — a false promise. The documented per-project opt-out is
// `projects[<path>].disabledMcpServers`, and this module (the owner of
// `~/.claude.json` writes) is the ONE home for it.

/// The `~/.claude.json` `projects` map key for a project folder.
///
/// Claude Code keys the map by the path the session was OPENED in — the
/// literal, as-entered folder path, not a symlink-resolved/canonicalized
/// one (confirmed against Claude Code's own `disabledMcpServers` write on
/// this machine, which sits under the literal project path). Keying by
/// `fs::canonicalize` produced a physical path Claude Code never reads
/// whenever the folder is reached through a symlink, silently voiding the
/// per-project opt-out — so the key is the stored path AS GIVEN, and this
/// ONE function is used by the writer and every reader so the two sides
/// cannot drift apart.
pub fn project_key_for_claude_json(project_folder: &Path) -> String {
    project_folder.to_string_lossy().to_string()
}

/// Record a project's per-project MCP opt-out list in `~/.claude.json`.
///
/// `disabled` is the COMPLETE desired list for the project (not a delta):
/// the caller recomputes it from the launcher DB on every toggle, so the
/// write is idempotent and a re-enabled name can never linger as a stale
/// opt-out. An empty list REMOVES the `disabledMcpServers` key rather than
/// writing `[]` (Claude Code treats the two identically; omitting keeps the
/// user's file minimal).
///
/// Same discipline as [`register_mcp`]: sidecar lock, read, mutate ONLY
/// `projects[<key>].disabledMcpServers`, atomic write. Every other key —
/// `mcpServers`, other projects' entries, and unrelated top-level keys —
/// is left exactly as it was. When `disabled` is empty AND neither the
/// `projects` map nor the project's entry exists, nothing is written at all
/// (there is no stale opt-out to remove, and creating empty containers
/// would be noise in the user's file).
pub fn set_project_disabled_mcps(
    claude_json: &Path,
    project_folder: &Path,
    disabled: &[String],
) -> Result<(), String> {
    let _lock = acquire_lock(claude_json, 5000)?;
    let mut root = read_json_or_empty(claude_json)?;
    let root_obj = root
        .as_object_mut()
        .ok_or("target file root is not a JSON object")?;

    let mut list: Vec<String> = disabled.to_vec();
    list.sort();
    list.dedup();

    let projects_missing = !root_obj.contains_key("projects");
    if projects_missing && list.is_empty() {
        // Nothing to record and nothing to remove — leave the file untouched.
        return Ok(());
    }
    if projects_missing {
        root_obj.insert("projects".into(), serde_json::json!({}));
    }
    let projects = root_obj
        .get_mut("projects")
        .and_then(|v| v.as_object_mut())
        .ok_or("projects is not an object")?;

    let project_key = project_key_for_claude_json(project_folder);
    let entry_exists = projects.contains_key(&project_key);
    if !entry_exists && list.is_empty() {
        // No project entry, so no opt-out to clear. (The `projects` container
        // may have just been created above only when list was non-empty, so
        // this branch implies the container was already present.)
        return Ok(());
    }

    #[allow(clippy::map_entry)]
    if !entry_exists {
        projects.insert(
            project_key,
            serde_json::json!({ "disabledMcpServers": list }),
        );
    } else {
        // `shift_remove`, never `remove` (preserve_order; see `deregister_mcp`).
        let entry = projects
            .get_mut(&project_key)
            .and_then(|v| v.as_object_mut())
            .ok_or("projects entry is not an object")?;
        if list.is_empty() {
            entry.shift_remove("disabledMcpServers");
        } else {
            entry.insert("disabledMcpServers".into(), serde_json::json!(list));
        }
    }

    atomic_write_json(claude_json, &root, CLAUDE_JSON_BACKUP)?;
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
/// (launcher writes them via `vco_lib.config_projection apply`); they are
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
/// v0.2.83 WP-B4 — SINGLE SOURCE OF TRUTH is
/// `vco_lib/mcp_scan_rules.toml` ([env].allowed_global_keys), the SAME file
/// Python's `_ALLOWED_GLOBAL_ENV_KEYS` reads. This `&[&str]` is a
/// compiled-in COPY kept for the crate's `&str` call-sites (dashboard.rs
/// comment ref, the two scan loops below). It is drift-LOCKED to the table
/// by `mcp_scan_rules_allowed_env_keys_matches_table` (reads the .toml at
/// test time) + the cross-language `tests/test_mcp_scan_rules_parity.py`.
/// Edit the .toml, then this copy, then run the tests — they fail loudly on
/// any mismatch. Do NOT hand-edit this without the .toml (the sanctioned
/// compiled-copy-with-parity-test pattern, CLAUDE.md A>B>C tier B).
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

/// Where the registered MCPs reach the core services. Built by
/// [`machine_service_ports`] from the launcher.db `service_endpoints` rows;
/// the default is the compiled defaults on localhost.
///
/// v0.2.97: carries the rendered URLs too — the row's HOST, not only its
/// port. Before, the entries were `http://localhost:<port>`, so an adopted
/// Ollama on another machine was registered as a local port.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServicePorts {
    pub weaviate_port: u16,
    pub ollama_port: u16,
    pub grpc_port: u16,
    pub code_embed_port: u16,
    pub weaviate_url: String,
    pub ollama_url: String,
    pub code_embed_url: String,
}

impl Default for ServicePorts {
    fn default() -> Self {
        Self {
            weaviate_port: DEFAULT_WEAVIATE_PORT,
            ollama_port: DEFAULT_OLLAMA_PORT,
            grpc_port: DEFAULT_GRPC_PORT,
            code_embed_port: DEFAULT_CODE_EMBED_PORT,
            weaviate_url: format!("http://localhost:{}", DEFAULT_WEAVIATE_PORT),
            ollama_url: format!("http://localhost:{}", DEFAULT_OLLAMA_PORT),
            code_embed_url: format!("http://localhost:{}", DEFAULT_CODE_EMBED_PORT),
        }
    }
}

/// The ONE endpoint source for MCP registration (v0.2.97).
///
/// The entries `build_default_mcp_entries` writes land in `~/.claude.json` —
/// a MACHINE-GLOBAL surface every project on this machine shares — so every
/// value baked into them is the machine row's (`service_endpoints`: the
/// launcher.db row, else the compiled default), gRPC port included. No env
/// var is read: not `WEAVIATE_PORT`/`OLLAMA_PORT`/`CODE_EMBED_PORT` (the old
/// installer hand-off), not `WEAVIATE_GRPC_PORT`.
pub fn machine_service_ports() -> ServicePorts {
    use vct_launcher_core::services::service_endpoints::{machine_row_from_disk, CoreService};
    let w = machine_row_from_disk(CoreService::Weaviate);
    let o = machine_row_from_disk(CoreService::Ollama);
    let c = machine_row_from_disk(CoreService::CodeEmbed);
    service_ports_from_rows(w.as_ref(), o.as_ref(), c.as_ref())
}

/// [`ServicePorts`] for rows the caller already holds (`None` = no row → the
/// compiled default). The Rust twin of Python
/// `vco_lib.service_endpoints.urls_from_rows`, which the Tier-4 fallback
/// `install_mcp._build_python_mcp_entries` consumes; the two are pinned by the
/// shared case table `tests/fixtures/mcp_registration_url_parity.json`
/// (v0.2.100 WP-18B).
pub fn service_ports_from_rows(
    w: Option<&vct_launcher_core::db::service_endpoints::ServiceEndpointRow>,
    o: Option<&vct_launcher_core::db::service_endpoints::ServiceEndpointRow>,
    c: Option<&vct_launcher_core::db::service_endpoints::ServiceEndpointRow>,
) -> ServicePorts {
    use vct_launcher_core::services::service_endpoints::{
        render_grpc_port, render_port, render_url, CoreService,
    };
    ServicePorts {
        weaviate_port: render_port(CoreService::Weaviate, w),
        ollama_port: render_port(CoreService::Ollama, o),
        grpc_port: render_grpc_port(w),
        code_embed_port: render_port(CoreService::CodeEmbed, c),
        weaviate_url: render_url(CoreService::Weaviate, w),
        ollama_url: render_url(CoreService::Ollama, o),
        code_embed_url: render_url(CoreService::CodeEmbed, c),
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
    /// v0.2.101: names of `auto_scrub` deprecated MCP entries removed from
    /// `~/.claude.json` during this registration pass (a VCO-shaped entry
    /// whose module no longer ships). The caller prints one notice line per
    /// name. Empty on an idempotent second run.
    pub removed_deprecated: Vec<String>,
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
/// v0.2.83 WP-B4: the needle DATA is SOURCED from
/// `vco_lib/mcp_scan_rules.toml` ([env].secret_shaped_needles) via the core
/// loader — the SAME table Python's `_SECRET_SHAPED_SUBSTRINGS` reads. The
/// segment-split + `KEY`/`*_KEY` suffix PREDICATE below stays language-local
/// (it is control-flow, not data). `tests/test_secret_shaped_needles_parity.py`
/// anchors every impl on the table.
pub fn is_secret_shaped_env_key(key: &str) -> bool {
    let upper = key.to_ascii_uppercase();
    let needles = vct_launcher_core::mcp_scan_rules::secret_shaped_needles();
    // Split into segments on `_` and `-`, then check each segment for an
    // exact match against the needle list.
    let segments: Vec<&str> = upper.split(|c: char| c == '_' || c == '-').collect();
    for needle in needles.iter() {
        if segments.iter().any(|s| *s == needle.as_str()) {
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
///
/// MUST stay in sync with the Python mirror
/// `install.py::_build_python_mcp_entries` (Tier-4 pure-Python fallback
/// writer) — same entry names, same order, same shapes. A drift between
/// the two means fresh installs get different `~/.claude.json` contents
/// depending on whether the launcher binary or the Python fallback did
/// the write.
pub fn build_default_mcp_entries(
    install_root: &Path,
    venv_python: &Path,
    ports: ServicePorts,
) -> Vec<(String, serde_json::Value, Vec<String>)> {
    let weaviate_url = ports.weaviate_url.clone();
    let ollama_url = ports.ollama_url.clone();
    let code_embed_url = ports.code_embed_url.clone();
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
    // The launcher's env projection (config_projection apply) puts these in
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

    // ── playwright (F-1, v0.2.73) ───────────────────────────────────
    // Browser automation via Microsoft's `@playwright/mcp`. The entry
    // mirrors EXACTLY how the MCP is launched everywhere else in the
    // stack: bare `npx -y @playwright/mcp@latest` — the same invocation
    // the GUI catalog ships (vct-launcher-core/src/types.rs::
    // default_mcp_servers) and install.py's `_install_playwright_browsers`
    // pre-caches. No venv-python and no env vars are involved.
    // Default-enabled per project.
    //
    // v0.2.91 correction: this comment used to assert "`npx` resolves from
    // PATH cross-OS". The field disproved it — on a machine with no Node.js
    // (or an fnm/nvm shape where only `npm` was hand-symlinked onto PATH)
    // there is nothing for Claude Code to resolve, so the server never
    // starts and `claude mcp list` reports only "Failed to connect". The
    // bare-`npx` command string is still the right entry — it is what the
    // GUI catalog ships and what VCO's third-party-preservation and
    // stale-entry fingerprints match on — so the fix is VISIBILITY, not a
    // different command: `vco doctor` probes the ladder in
    // `vco_lib/npx_resolver.py` at the end of every install/update, defers
    // `npx_missing_mcp_unspawnable`, and the launcher's registration badge
    // turns yellow with the same remediation. (The VCO-owned mermaid /
    // excalidraw wrapper proxies CAN fall back to `npm exec` because they
    // spawn the upstream package themselves; a registered entry cannot.)
    //
    // Pre-v0.2.73 this entry was MISSING from both builders (audit
    // finding F-1): the GUI catalog shipped `enabled: true`, so the
    // toggle-ON write never fired on a fresh install, and no install
    // path wrote the entry — docs promised a default-enabled playwright
    // MCP while the ~150 MB Chromium pre-cache was spent on an MCP that
    // never reached ~/.claude.json.
    let playwright_env: serde_json::Map<String, serde_json::Value> = serde_json::Map::new();
    let playwright_entry = serde_json::json!({
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest"],
        "env": serde_json::Value::Object(playwright_env),
    });
    let playwright_dropped: Vec<String> = Vec::new();

    // NOTE (v0.2.101): `search` (module deleted) and the diagram wrapper MCPs
    // `mermaid` / `excalidraw` (registration retired) no longer appear here.
    // An install that already has those entries keeps them — they stay in
    // `[bundled].all_names` / `uninstall_scrub_names` — but no default
    // registration composes them.

    vec![
        ("weaviate-kg".to_string(), weaviate_entry, weaviate_dropped),
        ("playwright".to_string(), playwright_entry, playwright_dropped),
    ]
}

/// Names of the bundled MCPs whose canonical `~/.claude.json` entries are
/// composed by `build_default_mcp_entries`. The set that "which ids must be
/// (re)registered with the canonical builder shape" — the GUI toggle path
/// (`dashboard.rs::toggle_mcp_server_inner`) and the stale-rewrite partition
/// below both consult this list, and the
/// `default_mcp_entry_names_matches_builder_output` unit test pins it
/// against the builder's actual output so the two cannot drift (the
/// pre-v0.2.73 catalog disagreements are audit findings F-1/F-2/F-3).
///
/// v0.2.83 WP-B4 — SINGLE SOURCE OF TRUTH is
/// `vco_lib/mcp_scan_rules.toml` ([entries].default_names), the SAME file
/// Python's `_DEFAULT_MCP_ENTRY_NAMES` reads. This `&[&str]` is a
/// compiled-in COPY kept for the crate's `&str` call-sites (maintenance.rs
/// tests iterate + `.contains` it, the rewrite partition below borrows it).
/// It is drift-LOCKED to the table by
/// `mcp_scan_rules_default_entry_names_matches_table` (reads the .toml at
/// test time) + the cross-language `tests/test_mcp_scan_rules_parity.py`.
/// Edit the .toml, then this copy, then run the tests (sanctioned
/// compiled-copy-with-parity-test pattern, CLAUDE.md A>B>C tier B).
pub const DEFAULT_MCP_ENTRY_NAMES: &[&str] = &["weaviate-kg", "playwright"];

/// Compose the canonical `~/.claude.json` entry for ONE bundled MCP id
/// (F-2, v0.2.73).
///
/// Returns:
///   * `Ok(Some((entry, dropped_keys)))` — `mcp_id` is bundled and the
///     canonical entry was composed via `build_default_mcp_entries`.
///   * `Ok(None)` — `mcp_id` is not a bundled default (user-added custom
///     MCP); the caller should use its own stored entry shape.
///   * `Err(_)` — `mcp_id` IS bundled but the canonical entry cannot be
///     composed (no venv-python under `install_root`). Callers must NOT
///     fall back to a non-canonical shape on this arm — writing a broken
///     entry into ~/.claude.json is strictly worse than surfacing the
///     error (the write "succeeds" and the MCP silently never spawns
///     again).
///
/// Note: playwright's entry does not actually use the venv-python, but
/// every bundled id routes through the single builder anyway (one
/// concern, one home); a correctly-installed orchestrator always has the
/// venv, so the shared gate costs nothing in practice.
pub fn default_entry_for_bundled_mcp(
    install_root: &Path,
    mcp_id: &str,
    ports: ServicePorts,
) -> Result<Option<(serde_json::Value, Vec<String>)>, String> {
    if !DEFAULT_MCP_ENTRY_NAMES.contains(&mcp_id) {
        return Ok(None);
    }
    let venv_python = resolve_venv_python(install_root).ok_or_else(|| {
        format!(
            "cannot compose the canonical `{}` MCP entry: no venv-python found \
             under `{}` (tried .venv and claude_mcp_servers/.venv). Check the \
             launcher's install path / re-run the installer, then toggle again.",
            mcp_id,
            install_root.display()
        )
    })?;
    Ok(build_default_mcp_entries(install_root, &venv_python, ports)
        .into_iter()
        .find(|(name, _, _)| name == mcp_id)
        .map(|(_, entry, dropped)| (entry, dropped)))
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
        removed_deprecated: Vec::new(),
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
            tracing::info!(
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
                // v0.2.91 WP-E item 3: goes through the SHARED
                // default-disabled helper, not the raw UPSERT.
                //
                // The raw UPSERT's SQL writes `enabled = 1` on INSERT
                // unconditionally, so this path used to seed the orchestrator
                // root's `mermaid` / `excalidraw` rows ENABLED — against
                // `BUNDLED_MCP_DEFAULT_DISABLED` (the members it carried then;
                // the list is empty after the v0.2.101 diagram retirement),
                // while the populate path applied the rule correctly.
                // Same discipline, one home (`project_mcp_servers.rs`).
                if let Err(e) = db.register_project_mcp_server_honoring_defaults(
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

    // v0.2.101: remove `auto_scrub` deprecated MCP entries from the SAME
    // `~/.claude.json` — the module they pointed at no longer ships, so a
    // leftover entry fails to spawn on every session. Owner ruling
    // (PLAN-V0300 item 15): delete now + scrub, no consent prompt; the caller
    // prints one notice line. Only VCO-shaped entries are removed (path inside
    // install_root), so a user's own MCP that shares a name is left alone.
    // `mermaid`/`excalidraw` are NOT marked auto_scrub (owner: users keep
    // them). Idempotent — a second run finds nothing.
    report.removed_deprecated = scrub_auto_scrub_mcp_entries(&claude_json, install_root);

    Ok(report)
}

/// Remove every `[deprecated.<name>]` entry flagged `auto_scrub = true` whose
/// on-disk `command`/`args[0]` points INSIDE `install_root` (i.e. it is VCO's
/// entry, not a user's own MCP with the same name). Uses `deregister_mcp`
/// (the module's one writer, with its lock + atomic write). Returns the names
/// removed, in the table's stable (BTreeMap) order. Idempotent; soft-fail.
fn scrub_auto_scrub_mcp_entries(claude_json: &Path, install_root: &Path) -> Vec<String> {
    let deprecated = vct_launcher_core::mcp_scan_rules::deprecated_default_mcps();
    let auto_scrub: Vec<&String> = deprecated
        .iter()
        .filter(|(_, d)| d.auto_scrub)
        .map(|(name, _)| name)
        .collect();
    if auto_scrub.is_empty() || !claude_json.is_file() {
        return Vec::new();
    }
    let root = match fs::canonicalize(install_root) {
        Ok(c) => c,
        Err(_) => install_root.to_path_buf(),
    };
    let root_str = root.display().to_string();
    let root_str = root_str.trim_end_matches(['/', '\\']).to_string();

    let mut removed: Vec<String> = Vec::new();
    for name in auto_scrub {
        // Re-read the file per entry: `deregister_mcp` writes between calls,
        // so a cached read would go stale. Cheap — the set is one name.
        let root_json = match read_json_or_empty(claude_json) {
            Ok(v) => v,
            Err(_) => break,
        };
        let Some(entry) = root_json
            .get("mcpServers")
            .and_then(|v| v.as_object())
            .and_then(|m| m.get(name.as_str()))
        else {
            continue;
        };
        if !entry_path_inside_install_root(entry, &root_str) {
            // A user's own MCP that happens to share the name — leave it.
            continue;
        }
        match deregister_mcp(claude_json, name) {
            Ok(()) => {
                tracing::info!(
                    "[vct] removed obsolete MCP entry `{}` from {} (module no longer ships)",
                    name,
                    claude_json.display()
                );
                removed.push(name.clone());
            }
            Err(e) => {
                tracing::warn!(
                    "[vct] could not remove obsolete MCP entry `{}` from {}: {}",
                    name,
                    claude_json.display(),
                    e
                );
            }
        }
    }
    removed
}

/// True iff an MCP entry's `command` or `args[0]` is an absolute path that
/// starts with `install_root_str` (trailing separators trimmed). Mirrors the
/// `_path_is_inside_install_root` gate on the Python side so the two agree on
/// what "VCO's entry" means.
fn entry_path_inside_install_root(entry: &serde_json::Value, install_root_str: &str) -> bool {
    if install_root_str.is_empty() {
        return false;
    }
    let command = entry.get("command").and_then(|v| v.as_str()).unwrap_or("");
    let first_arg = entry
        .get("args")
        .and_then(|v| v.as_array())
        .and_then(|a| a.first())
        .and_then(|v| v.as_str())
        .unwrap_or("");
    [command, first_arg].iter().any(|candidate| {
        if candidate.is_empty() {
            return false;
        }
        let looks_absolute = candidate.starts_with('/')
            || candidate.starts_with("C:\\")
            || candidate.starts_with("c:\\")
            || candidate.starts_with("\\\\");
        if !looks_absolute {
            return false;
        }
        let cand = candidate.replace('\\', "/");
        let root = install_root_str.replace('\\', "/");
        cand == root || cand.starts_with(&format!("{}/", root))
    })
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
    // non-bundled (writer leaves alone). F-2 (v0.2.73): derive from
    // DEFAULT_MCP_ENTRY_NAMES instead of a drifted local copy — the
    // pre-v0.2.73 list here ("weaviate-kg", "search") had already
    // fallen out of sync with the builder (mermaid/excalidraw missing),
    // so an accepted stale mermaid entry was skipped as "non-bundled".
    let bundled = DEFAULT_MCP_ENTRY_NAMES;
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

    /// v0.2.97: registration values are the machine rows' — host, port and
    /// the gRPC port — and no env var (`WEAVIATE_PORT` & co., the old
    /// installer hand-off; `WEAVIATE_GRPC_PORT`) is read. Red against the
    /// pre-SE-4 env-read gRPC port and the `localhost:<port>` URLs.
    #[test]
    fn machine_service_ports_reads_the_rows_only() {
        use vct_launcher_core::db::service_endpoints::{EndpointMode, ServiceEndpointRow};
        let _g = vct_launcher_core::test_env::state_dir_guard_with(&[
            ("WEAVIATE_PORT", Some("19999")),
            ("OLLAMA_PORT", Some("19998")),
            ("CODE_EMBED_PORT", Some("19997")),
            ("WEAVIATE_GRPC_PORT", Some("50999")),
        ]);
        let db = vct_launcher_core::db::Db::open().unwrap();
        db.app_state_set("weaviate.port_override", "18083").unwrap();
        let mut row = ServiceEndpointRow::new("weaviate", EndpointMode::VcoManaged, "localhost", 18081);
        row.grpc_port = Some(50061);
        db.service_endpoint_seed_for_tests(&row).unwrap();
        db.service_endpoint_seed_for_tests(&ServiceEndpointRow::new(
            "ollama",
            EndpointMode::AdoptedExternal,
            "gpu.lan",
            21435,
        ))
        .unwrap();
        let ports = machine_service_ports();
        assert_eq!(ports.weaviate_port, 18081);
        assert_eq!(ports.grpc_port, 50061);
        assert_eq!(ports.ollama_port, 21435);
        assert_eq!(ports.ollama_url, "http://gpu.lan:21435", "the row's host reaches the entry");
        // No code_embed row on this harness state dir: the sentinel.
        assert_eq!(ports.code_embed_url, "http://127.0.0.1:9");
    }

    /// The entries carry the URLs as rendered — an adopted remote Ollama is
    /// registered at its host.
    #[test]
    fn entries_carry_the_rendered_urls() {
        let root = tempfile::tempdir().unwrap();
        let py = root.path().join("python");
        let mut ports = ServicePorts::default();
        ports.ollama_url = "http://gpu.lan:11434".into();
        ports.grpc_port = 50061;
        let entries = build_default_mcp_entries(root.path(), &py, ports);
        let weaviate = &entries.iter().find(|(n, _, _)| n == "weaviate-kg").unwrap().1;
        assert_eq!(weaviate["env"]["OLLAMA_URL"], "http://gpu.lan:11434");
        assert_eq!(weaviate["env"]["GRPC_PORT"], "50061");
    }

    /// v0.2.100 WP-18B: the shared case table the Python Tier-4 fallback
    /// (`install_mcp._build_python_mcp_entries` over `urls_from_rows`) runs
    /// too — the rows' host and scheme reach the registered entry on both.
    #[test]
    fn mcp_registration_url_parity_table() {
        use vct_launcher_core::db::service_endpoints::{EndpointMode, ServiceEndpointRow};
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/fixtures/mcp_registration_url_parity.json");
        let text = fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
        let table: serde_json::Value = serde_json::from_str(&text).expect("table parses");
        let cases = table["cases"].as_array().expect("cases");
        assert!(!cases.is_empty());
        let row_of = |svc: &str, v: &serde_json::Value| -> Option<ServiceEndpointRow> {
            if v.is_null() {
                return None;
            }
            let mode = EndpointMode::parse(v["mode"].as_str().unwrap()).expect("mode");
            let mut row = ServiceEndpointRow::new(
                svc,
                mode,
                v.get("host").and_then(|h| h.as_str()).unwrap_or("localhost"),
                v["port"].as_u64().unwrap() as u16,
            );
            if let Some(sc) = v.get("scheme").and_then(|s| s.as_str()) {
                row.scheme = sc.to_string();
            }
            row.grpc_port = v.get("grpc_port").and_then(|g| g.as_u64()).map(|g| g as u16);
            row.container_name =
                v.get("container_name").and_then(|c| c.as_str()).map(String::from);
            Some(row)
        };
        let root = tempfile::tempdir().unwrap();
        let py = root.path().join("python");
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let rows = &case["rows"];
            let w = row_of("weaviate", &rows["weaviate"]);
            let o = row_of("ollama", &rows["ollama"]);
            let c = row_of("code_embed", &rows["code_embed"]);
            let ports = service_ports_from_rows(w.as_ref(), o.as_ref(), c.as_ref());
            let entries = build_default_mcp_entries(root.path(), &py, ports);
            let env = &entries.iter().find(|(n, _, _)| n == "weaviate-kg").unwrap().1["env"];
            for (key, want) in case["expect_weaviate_kg_env"].as_object().unwrap() {
                assert_eq!(&env[key.as_str()], want, "{name}: {key}");
            }
        }
    }

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
        // Also create the MCP server dir so paths look plausible.
        fs::create_dir_all(root.join("claude_mcp_servers/weaviate_mcp")).unwrap();
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
        // v0.2.101: `search` (module deleted) and the diagram wrapper MCPs
        // (`mermaid`/`excalidraw`, registration retired) were removed from the
        // builder. F-1 (v0.2.73): playwright stays — default-enabled.
        assert_eq!(names, vec!["weaviate-kg", "playwright"]);

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

        // ── playwright shape (F-1, v0.2.73) ──────────────────────────
        // Must match the shipped launch command everywhere else:
        // `npx -y @playwright/mcp@latest`, empty env, no venv-python.
        let (_, playwright, playwright_dropped) = &entries[1];
        assert_eq!(playwright["type"], "stdio");
        assert_eq!(playwright["command"], "npx");
        let playwright_args = playwright["args"].as_array().unwrap();
        assert_eq!(playwright_args.len(), 2);
        assert_eq!(playwright_args[0], "-y");
        assert_eq!(playwright_args[1], "@playwright/mcp@latest");
        assert!(
            playwright["env"].as_object().unwrap().is_empty(),
            "playwright entry must carry an empty env: {}",
            playwright["env"]
        );
        assert!(
            playwright_dropped.is_empty(),
            "playwright entry drops no keys: {:?}",
            playwright_dropped
        );

        // The absolute-script entries keep the package-internal path (their
        // imports are top-level siblings, not a dotted package name).
        assert_eq!(
            weaviate["env"]["PYTHONPATH"].as_str().unwrap(),
            root.join("claude_mcp_servers").display().to_string(),
        );

        fs::remove_dir_all(&root).ok();
    }

    // ── v0.2.91 WP-E item 3: default-disabled parity on the DB-sync ────

    /// ACT: the registration DB-sync seeds the orchestrator-root project's
    /// rows through the SHARED default-disabled helper. v0.2.101: the
    /// default-disabled registry is empty (the diagram MCPs retired), so the
    /// builder rows all land `enabled = true` and NO row is created for the
    /// retired `search`/`mermaid`/`excalidraw` names.
    ///
    /// Red-proof (c67ef888): before the shared helper, this path called the
    /// raw `register_project_mcp_server`, whose SQL writes `enabled = 1` on
    /// INSERT unconditionally. That is why the field DB showed the root
    /// project's mermaid/excalidraw rows enabled against
    /// `BUNDLED_MCP_DEFAULT_DISABLED`. The per-name loop below stays meaningful
    /// if the registry ever gains a member again.
    #[test]
    fn db_sync_seeds_builder_rows_enabled() {
        use crate::db::models::ProjectHost;
        use crate::db::Db;

        let root = make_pseudo_install_root();
        let db = Db::open_in_memory().unwrap();
        let pid = uuid::Uuid::new_v4().to_string();
        db.insert_project(
            &pid,
            "Orchestrator Root",
            &root.to_string_lossy(),
            ProjectHost::Base,
            "orchestrator-root",
        )
        .unwrap();

        let target = tmp_target();
        let report = register_default_orchestrator_mcps(
            &root,
            ServicePorts::default(),
            Some(&target),
            Some(&db),
        )
        .expect("register_default_orchestrator_mcps");
        assert!(
            report.db_warnings.is_empty(),
            "db sync warnings: {:?}",
            report.db_warnings
        );

        let rows = db.list_project_mcp_servers(&pid).unwrap();
        let by_name: std::collections::HashMap<&str, bool> =
            rows.iter().map(|r| (r.mcp_name.as_str(), r.enabled)).collect();
        assert_eq!(by_name.len(), DEFAULT_MCP_ENTRY_NAMES.len());
        for name in vct_launcher_core::db::project_mcp_servers::BUNDLED_MCP_DEFAULT_DISABLED {
            assert_eq!(
                by_name.get(name),
                Some(&false),
                "`{}` is BUNDLED_MCP_DEFAULT_DISABLED — the DB-sync must seed it \
                 enabled=false, same as the populate path",
                name
            );
        }
        for name in DEFAULT_MCP_ENTRY_NAMES {
            if vct_launcher_core::db::project_mcp_servers::is_default_disabled_mcp(name) {
                continue;
            }
            assert_eq!(
                by_name.get(name),
                Some(&true),
                "`{}` is not default-disabled — it must seed enabled=true",
                name
            );
        }
        // v0.2.101: the retired names get NO DB row from the registration
        // sync (they are not in the builder set any more). Red-proof: re-add
        // `search`/`mermaid`/`excalidraw` to the builder → this fails.
        for retired in ["search", "mermaid", "excalidraw"] {
            assert!(
                !by_name.contains_key(retired),
                "`{}` was retired from default registration — no DB row must \
                 be seeded for it",
                retired
            );
        }

        fs::remove_file(&target).ok();
        fs::remove_dir_all(&root).ok();
    }

    /// LEAVE-ALONE: a re-run must NOT re-apply any default-enabled/dis-
    /// enabled flip, and must NOT undo a deliberate user toggle. The row
    /// already exists, so the UPSERT's enabled-preserving `DO UPDATE` is the
    /// only thing that runs.
    #[test]
    fn db_sync_rerun_preserves_user_disabled_toggle() {
        use crate::db::models::ProjectHost;
        use crate::db::Db;

        let root = make_pseudo_install_root();
        let db = Db::open_in_memory().unwrap();
        let pid = uuid::Uuid::new_v4().to_string();
        db.insert_project(
            &pid,
            "Orchestrator Root",
            &root.to_string_lossy(),
            ProjectHost::Base,
            "orchestrator-root",
        )
        .unwrap();
        let target = tmp_target();

        register_default_orchestrator_mcps(&root, ServicePorts::default(), Some(&target), Some(&db))
            .unwrap();
        // The user silences playwright for this project via the launcher.
        db.set_project_mcp_server_enabled(&pid, "playwright", false).unwrap();

        register_default_orchestrator_mcps(&root, ServicePorts::default(), Some(&target), Some(&db))
            .unwrap();

        let rows = db.list_project_mcp_servers(&pid).unwrap();
        let playwright = rows.iter().find(|r| r.mcp_name == "playwright").unwrap();
        assert!(
            !playwright.enabled,
            "re-registration must never re-enable a row the user disabled \
             (enabled is set on INSERT only)"
        );

        fs::remove_file(&target).ok();
        fs::remove_dir_all(&root).ok();
    }

    /// F-2 (v0.2.73): the names constant and the builder's output are two
    /// spellings of the same catalog — pin them together so a sixth MCP
    /// added to only one side fails loudly (the F-1 playwright gap was
    /// exactly this drift).
    #[test]
    fn default_mcp_entry_names_matches_builder_output() {
        let root = make_pseudo_install_root();
        let py = resolve_venv_python(&root).expect("pseudo venv-python");
        let entries = build_default_mcp_entries(&root, &py, ServicePorts::default());
        let names: Vec<&str> = entries.iter().map(|(n, _, _)| n.as_str()).collect();
        assert_eq!(
            names, DEFAULT_MCP_ENTRY_NAMES,
            "DEFAULT_MCP_ENTRY_NAMES must equal build_default_mcp_entries output (same order)"
        );
        fs::remove_dir_all(&root).ok();
    }

    // ── v0.2.83 WP-B4: compiled-copy ⇄ table drift locks ────────────────
    //
    // ALLOWED_ENV_KEYS and DEFAULT_MCP_ENTRY_NAMES are compiled-in `&[&str]`
    // copies of DATA whose single source of truth is
    // `vco_lib/mcp_scan_rules.toml`. These tests read the committed .toml at
    // test time and assert the compiled copies equal it — so a table edit
    // that isn't mirrored into the copy (or vice-versa) fails loudly. The
    // sanctioned compiled-copy-with-parity-test pattern (CLAUDE.md A>B>C B).
    //
    // Both go through `vct_launcher_core::mcp_scan_rules::RULES`, the same
    // compile-time-embedded loader `is_secret_shaped_env_key`'s needles read
    // at runtime — so all three consumers agree with the table.

    #[test]
    fn mcp_scan_rules_allowed_env_keys_matches_table() {
        let table = vct_launcher_core::mcp_scan_rules::allowed_global_env_keys();
        let compiled: Vec<&str> = ALLOWED_ENV_KEYS.to_vec();
        let from_table: Vec<&str> = table.iter().map(|s| s.as_str()).collect();
        assert_eq!(
            compiled, from_table,
            "ALLOWED_ENV_KEYS (compiled) drifted from \
             mcp_scan_rules.toml [env].allowed_global_keys. Edit the .toml \
             and this const together."
        );
    }

    #[test]
    fn mcp_scan_rules_default_entry_names_matches_table() {
        let table = vct_launcher_core::mcp_scan_rules::default_mcp_entry_names();
        let compiled: Vec<&str> = DEFAULT_MCP_ENTRY_NAMES.to_vec();
        let from_table: Vec<&str> = table.iter().map(|s| s.as_str()).collect();
        assert_eq!(
            compiled, from_table,
            "DEFAULT_MCP_ENTRY_NAMES (compiled) drifted from \
             mcp_scan_rules.toml [entries].default_names. Edit the .toml \
             and this const together."
        );
    }

    #[test]
    fn is_secret_shaped_reads_needles_from_table() {
        // The predicate's needle DATA comes from the table — a smoke check
        // that a key built from a table needle is flagged, and a non-needle
        // env key is not. (The full needle-set parity lives in
        // tests/test_secret_shaped_needles_parity.py.)
        let needles = vct_launcher_core::mcp_scan_rules::secret_shaped_needles();
        assert!(!needles.is_empty(), "needle table must be non-empty");
        for n in needles.iter() {
            let key = format!("MY_{}", n);
            assert!(
                is_secret_shaped_env_key(&key),
                "a key ending in table needle `{}` must be flagged",
                n
            );
        }
        assert!(!is_secret_shaped_env_key("WEAVIATE_URL"));
    }

    /// F-2: the per-id helper composes the SAME canonical entry the
    /// full builder produces — absolute venv-python command for
    /// weaviate-kg, not the GUI catalog's relative-path stub.
    #[test]
    fn default_entry_for_bundled_mcp_composes_canonical_entry() {
        let root = make_pseudo_install_root();
        let py = resolve_venv_python(&root).expect("pseudo venv-python");

        let (entry, _dropped) =
            default_entry_for_bundled_mcp(&root, "weaviate-kg", ServicePorts::default())
                .expect("no error with a valid pseudo install root")
                .expect("weaviate-kg is bundled");
        assert_eq!(entry["type"], "stdio");
        assert_eq!(entry["command"], py.display().to_string());
        let cmd = entry["command"].as_str().unwrap();
        assert!(
            std::path::Path::new(cmd).is_absolute(),
            "canonical command must be absolute: {}",
            cmd
        );
        assert!(
            std::path::Path::new(cmd).exists(),
            "canonical command must exist on disk: {}",
            cmd
        );

        // playwright routes through the same helper (no venv use, but
        // one home for entry composition).
        let (pw, _) =
            default_entry_for_bundled_mcp(&root, "playwright", ServicePorts::default())
                .expect("no error")
                .expect("playwright is bundled");
        assert_eq!(pw["command"], "npx");

        fs::remove_dir_all(&root).ok();
    }

    /// F-2: a custom (non-bundled) id yields Ok(None) — the caller keeps
    /// its own stored entry shape.
    #[test]
    fn default_entry_for_bundled_mcp_none_for_custom_id() {
        let root = make_pseudo_install_root();
        let out = default_entry_for_bundled_mcp(&root, "my-custom-mcp", ServicePorts::default())
            .expect("no error");
        assert!(out.is_none(), "custom ids are not composed by the builder");
        fs::remove_dir_all(&root).ok();
    }

    /// F-2 conservative arm: a bundled id over an install root with NO
    /// venv-python must ERROR — never silently fall back to a shape that
    /// writes a broken entry.
    #[test]
    fn default_entry_for_bundled_mcp_errors_without_venv() {
        let root = std::env::temp_dir().join(format!(
            "vct-empty-install-{}",
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir_all(&root).unwrap();
        let err = default_entry_for_bundled_mcp(&root, "weaviate-kg", ServicePorts::default())
            .expect_err("bundled id without venv must error");
        assert!(
            err.contains("no venv-python"),
            "error should name the missing venv: {}",
            err
        );
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
        // v0.2.101: the builder registers exactly weaviate-kg + playwright.
        // `search` (module deleted) and the diagram wrappers (registration
        // retired) are no longer written; the ollama MCP was dropped in v0.2.11.
        assert_eq!(report.success_count(), 2);

        let raw = fs::read_to_string(&target).unwrap();
        let json: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert!(json["mcpServers"]["weaviate-kg"].is_object());
        assert!(
            json["mcpServers"]["playwright"].is_object(),
            "playwright MCP must be registered in ~/.claude.json (F-1)"
        );
        assert_eq!(
            json["mcpServers"]["playwright"]["command"], "npx",
            "playwright registration must match the shipped launch command (npx)"
        );
        // Retired defaults must NOT be written any more. Red-proof: re-add
        // the entry to the builder → the corresponding assert fails.
        for retired in ["search", "mermaid", "excalidraw", "ollama"] {
            assert!(
                json["mcpServers"].get(retired).is_none(),
                "`{}` must NOT be auto-registered by v0.2.101",
                retired
            );
        }

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
        assert!(json["mcpServers"]["playwright"].is_object());

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

    // ── v0.2.83 (WP-B3): third-party MCP preservation guarantee ─────────
    //
    // A user may run their OWN MCP servers (searxng, Jira, Gmail, whatever) —
    // present AND working. Every VCO writer to ~/.claude.json must leave those
    // entries byte-for-byte intact (command, args, env), including any
    // secret-shaped env key: the env allowlist/secret-filter applies ONLY to
    // VCO's OWN bundled entries (which VCO composes fresh), never to entries
    // VCO does not own. These pins fail if a future edit widens any writer to
    // touch unknown mcpServers keys.

    #[test]
    fn third_party_mcp_survives_default_registration_byte_for_byte() {
        let root = make_pseudo_install_root();
        let target = tmp_target();

        // A user-added third-party MCP with a FULL entry, including a
        // secret-shaped env key that VCO WOULD strip from its OWN entries.
        let third_party = serde_json::json!({
            "type": "stdio",
            "command": "/opt/jira-mcp/bin/jira-mcp",
            "args": ["--project", "ACME", "serve"],
            "env": {
                "JIRA_API_TOKEN": "super-secret-do-not-touch",
                "JIRA_BASE_URL": "https://acme.atlassian.net"
            }
        });
        fs::write(
            &target,
            serde_json::to_string_pretty(&serde_json::json!({
                "mcpServers": { "jira": third_party.clone() }
            }))
            .unwrap(),
        )
        .unwrap();

        register_default_orchestrator_mcps(&root, ServicePorts::default(), Some(&target), None)
            .unwrap();

        let raw = fs::read_to_string(&target).unwrap();
        let json: serde_json::Value = serde_json::from_str(&raw).unwrap();

        // Byte-for-byte survival: the whole entry object is unchanged,
        // secret env key included (VCO's secret-filter is scoped to its own
        // bundled entries, never a user's).
        assert_eq!(
            json["mcpServers"]["jira"], third_party,
            "third-party MCP entry must survive a VCO defaults write byte-for-byte"
        );
        assert_eq!(
            json["mcpServers"]["jira"]["env"]["JIRA_API_TOKEN"],
            "super-secret-do-not-touch",
            "VCO must NOT strip secret-shaped env keys from entries it does not own"
        );
        // And VCO's own entries were still added alongside.
        assert!(json["mcpServers"]["weaviate-kg"].is_object());

        fs::remove_file(&target).ok();
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn ex_vco_named_searxng_entry_treated_as_user_property() {
        // VCO used to ship a `searxng` compose service pre-v0.2.11 (never an
        // MCP entry). Today VCO ships NO searxng at all — so a `searxng`
        // mcpServers entry is USER PROPERTY (they may deliberately run their
        // own searxng MCP) and must NOT be cleaned up as a "VCO leftover".
        let root = make_pseudo_install_root();
        let target = tmp_target();

        let user_searxng = serde_json::json!({
            "type": "stdio",
            "command": "/home/dev/my-searxng-mcp/serve.py",
            "args": ["--port", "8888"],
            "env": { "SEARXNG_URL": "http://localhost:8888" }
        });
        fs::write(
            &target,
            serde_json::to_string_pretty(&serde_json::json!({
                "mcpServers": { "searxng": user_searxng.clone() }
            }))
            .unwrap(),
        )
        .unwrap();

        register_default_orchestrator_mcps(&root, ServicePorts::default(), Some(&target), None)
            .unwrap();

        let raw = fs::read_to_string(&target).unwrap();
        let json: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(
            json["mcpServers"]["searxng"], user_searxng,
            "an ex-VCO-named `searxng` entry is user property and must survive untouched"
        );

        fs::remove_file(&target).ok();
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn scan_stale_leaves_third_party_entries_untouched() {
        // scan_stale_mcp_entries only flags absolute paths containing vco
        // install tokens (claude_mcp_servers / .venv) OUTSIDE install_root.
        // Third-party MCPs — including a user's own searxng — at unrelated
        // paths or via npx must NEVER be classified stale (== never rewritten).
        let root = make_pseudo_install_root();
        let target = tmp_target();
        fs::write(
            &target,
            serde_json::to_string_pretty(&serde_json::json!({
                "mcpServers": {
                    // user's own searxng at an unrelated absolute path
                    "searxng": {"command": "/usr/local/bin/searxng-mcp"},
                    // npx-launched third party (relative command)
                    "gmail": {"command": "npx", "args": ["-y", "@acme/gmail-mcp"]},
                    // vco-shaped path but INSIDE install_root → not stale
                    "weaviate-kg": {
                        "command": root.join("claude_mcp_servers/weaviate_mcp/server.py")
                            .display().to_string()
                    }
                }
            }))
            .unwrap(),
        )
        .unwrap();

        let stale = scan_stale_mcp_entries(&root, Some(&target));
        let stale_names: Vec<&str> = stale.iter().map(|s| s.name.as_str()).collect();
        assert!(
            !stale_names.contains(&"searxng"),
            "a user's own searxng at an unrelated path must not be flagged stale: {:?}",
            stale_names
        );
        assert!(
            !stale_names.contains(&"gmail"),
            "an npx-launched third-party MCP must not be flagged stale: {:?}",
            stale_names
        );
        assert!(
            !stale_names.contains(&"weaviate-kg"),
            "a vco-shaped path INSIDE install_root is current, not stale: {:?}",
            stale_names
        );

        fs::remove_file(&target).ok();
        fs::remove_dir_all(&root).ok();
    }

    // ── v0.2.101: per-project disabledMcpServers ─────────────────────────
    //
    // The channel Claude Code honours for disabling a USER-scope MCP in one
    // project. These pin the writer, not the command wiring (that lives in
    // `commands::project_state_cmd`).

    fn project_entry<'a>(
        json: &'a serde_json::Value,
        folder: &Path,
    ) -> Option<&'a serde_json::Value> {
        json["projects"].get(project_key_for_claude_json(folder))
    }

    #[test]
    fn set_project_disabled_mcps_records_names_and_leaves_the_rest_untouched() {
        let target = tmp_target();
        let folder = std::env::temp_dir().join("vct-proj-a");
        fs::create_dir_all(&folder).unwrap();
        let other = "/some/other/project";
        fs::write(
            &target,
            serde_json::to_string_pretty(&serde_json::json!({
                "mcpServers": {"weaviate-kg": {"command": "x"}},
                "projects": {other: {"history": [1, 2, 3], "disabledMcpServers": ["keep"]}},
                "numStartups": 7
            }))
            .unwrap(),
        )
        .unwrap();

        set_project_disabled_mcps(
            &target,
            &folder,
            &["playwright".to_string(), "weaviate-kg".to_string()],
        )
        .unwrap();

        let json: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&target).unwrap()).unwrap();
        // New project entry carries the sorted, deduped list.
        assert_eq!(
            project_entry(&json, &folder).unwrap()["disabledMcpServers"],
            serde_json::json!(["playwright", "weaviate-kg"]),
        );
        // Other project + unrelated top-level keys are byte-identical.
        assert_eq!(json["projects"][other]["disabledMcpServers"], serde_json::json!(["keep"]));
        assert_eq!(json["projects"][other]["history"], serde_json::json!([1, 2, 3]));
        assert_eq!(json["mcpServers"]["weaviate-kg"]["command"], "x");
        assert_eq!(json["numStartups"], 7);

        fs::remove_file(&target).ok();
        fs::remove_dir_all(&folder).ok();
    }

    /// The `projects` map key is the path AS OPENED (Claude Code's own key
    /// form), never a symlink-resolved one: a project reached through a
    /// symlink must record its opt-out under the literal path or Claude
    /// Code never reads it. Red-proof: restoring `fs::canonicalize` in
    /// `project_key_for_claude_json` resolves the symlink → this fails.
    #[test]
    fn set_project_disabled_mcps_keys_by_the_as_opened_symlinked_path() {
        let target = tmp_target();
        let base = std::env::temp_dir().join("vct-proj-keyed");
        let real = base.join("real-project");
        let link = base.join("link-project");
        fs::create_dir_all(&real).unwrap();
        let _ = fs::remove_file(&link);
        std::os::unix::fs::symlink(&real, &link).unwrap();

        set_project_disabled_mcps(&target, &link, &["mermaid".to_string()]).unwrap();

        let json: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&target).unwrap()).unwrap();
        let projects = json["projects"].as_object().unwrap();
        assert!(
            projects.contains_key(link.to_string_lossy().as_ref()),
            "opt-out must sit under the literal (as-opened) path {}; got keys {:?}",
            link.display(),
            projects.keys().collect::<Vec<_>>(),
        );
        assert!(
            !projects.contains_key(real.to_string_lossy().as_ref()),
            "the symlink-resolved path {} is a key Claude Code never reads",
            real.display(),
        );

        fs::remove_file(&link).ok();
        fs::remove_dir_all(&base).ok();
        fs::remove_file(&target).ok();
    }

    #[test]
    fn set_project_disabled_mcps_dedupes_and_sorts() {
        let target = tmp_target();
        let folder = std::env::temp_dir().join("vct-proj-dedupe");
        fs::create_dir_all(&folder).unwrap();

        set_project_disabled_mcps(
            &target,
            &folder,
            &["b".to_string(), "a".to_string(), "b".to_string()],
        )
        .unwrap();

        let json: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&target).unwrap()).unwrap();
        assert_eq!(
            project_entry(&json, &folder).unwrap()["disabledMcpServers"],
            serde_json::json!(["a", "b"]),
        );

        fs::remove_file(&target).ok();
        fs::remove_dir_all(&folder).ok();
    }

    /// Re-enabling a server must DELETE the name, never leave a stale opt-out.
    /// Red-proof: an early `if disabled.is_empty() { return }` (before the
    /// mutation) leaves the old entry in place → this fails.
    #[test]
    fn set_project_disabled_mcps_empty_removes_the_key() {
        let target = tmp_target();
        let folder = std::env::temp_dir().join("vct-proj-reenable");
        fs::create_dir_all(&folder).unwrap();
        set_project_disabled_mcps(&target, &folder, &["playwright".to_string()]).unwrap();
        // The project entry exists with the name…
        let json: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&target).unwrap()).unwrap();
        assert_eq!(
            project_entry(&json, &folder).unwrap()["disabledMcpServers"],
            serde_json::json!(["playwright"]),
        );

        // …then re-enable (empty desired list) must REMOVE the key.
        set_project_disabled_mcps(&target, &folder, &[]).unwrap();
        let json: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&target).unwrap()).unwrap();
        assert!(
            project_entry(&json, &folder)
                .and_then(|e: &serde_json::Value| e.get("disabledMcpServers"))
                .is_none(),
            "re-enable must delete the stale opt-out: {}",
            json,
        );

        fs::remove_file(&target).ok();
        fs::remove_dir_all(&folder).ok();
    }

    /// An empty list with nothing to remove must not create `projects` (or the
    /// file) — no noise in the user's file.
    #[test]
    fn set_project_disabled_mcps_empty_is_a_noop_when_nothing_to_clear() {
        let target = tmp_target();
        let folder = std::env::temp_dir().join("vct-proj-noop");
        fs::create_dir_all(&folder).unwrap();

        set_project_disabled_mcps(&target, &folder, &[]).unwrap();

        let json: serde_json::Value = serde_json::from_str(
            &fs::read_to_string(&target).unwrap_or_else(|_| "{}".to_string()),
        )
        .unwrap();
        assert!(
            json.get("projects").is_none(),
            "no `projects` container may be created for an empty no-op: {}",
            json,
        );

        fs::remove_file(&target).ok();
        fs::remove_dir_all(&folder).ok();
    }

    // ── v0.2.101: auto-scrub of deleted-MCP entries ──────────────────────

    fn pseudo_venv_python(root: &Path) -> PathBuf {
        let (sub, py) = if cfg!(target_os = "windows") {
            ("Scripts", "python.exe")
        } else {
            ("bin", "python")
        };
        root.join(".venv").join(sub).join(py)
    }

    fn vco_search_entry(root: &Path) -> serde_json::Value {
        serde_json::json!({
            "type": "stdio",
            "command": pseudo_venv_python(root).display().to_string(),
            "args": [root.join("claude_mcp_servers/search_mcp/server.py").display().to_string()],
        })
    }

    /// ACT: the ordinary registration pass removes a VCO-shaped orphaned
    /// `search` entry (module deleted) and reports it once. Red-proof: drop
    /// `auto_scrub` from the table / skip the scrub call → this fails.
    #[test]
    fn auto_scrub_removes_a_vco_shaped_deleted_mcp_entry() {
        let root = make_pseudo_install_root();
        let target = tmp_target();
        fs::write(
            &target,
            serde_json::to_string_pretty(&serde_json::json!({
                "mcpServers": {
                    "search": vco_search_entry(&root),
                    "my-user-mcp": {"command": "/usr/bin/my-mcp"},
                },
                "numStartups": 5,
            }))
            .unwrap(),
        )
        .unwrap();

        let report =
            register_default_orchestrator_mcps(&root, ServicePorts::default(), Some(&target), None)
                .unwrap();
        assert_eq!(report.removed_deprecated, vec!["search".to_string()]);
        let json: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&target).unwrap()).unwrap();
        assert!(json["mcpServers"].get("search").is_none(), "{}", json);
        // Unrelated keys survive; the defaults are still registered.
        assert_eq!(json["mcpServers"]["my-user-mcp"]["command"], "/usr/bin/my-mcp");
        assert_eq!(json["numStartups"], 5);
        assert!(json["mcpServers"]["weaviate-kg"].is_object());

        // Idempotent: a second pass reports nothing.
        let second =
            register_default_orchestrator_mcps(&root, ServicePorts::default(), Some(&target), None)
                .unwrap();
        assert!(
            second.removed_deprecated.is_empty(),
            "second run must remove nothing: {:?}",
            second.removed_deprecated
        );

        fs::remove_file(&target).ok();
        fs::remove_dir_all(&root).ok();
    }

    /// LEAVE-ALONE: the diagram MCP entries (mermaid/excalidraw) are NOT
    /// auto-scrubbed — owner ruling: an existing install keeps them.
    #[test]
    fn auto_scrub_leaves_diagram_mcp_entries_untouched() {
        let root = make_pseudo_install_root();
        let target = tmp_target();
        let py = pseudo_venv_python(&root).display().to_string();
        fs::write(
            &target,
            serde_json::to_string_pretty(&serde_json::json!({
                "mcpServers": {
                    "mermaid": {"command": py, "args": ["-m", "claude_mcp_servers.wrappers.mermaid_proxy"]},
                    "excalidraw": {"command": py, "args": ["-m", "claude_mcp_servers.wrappers.excalidraw_proxy"]},
                }
            }))
            .unwrap(),
        )
        .unwrap();

        let report =
            register_default_orchestrator_mcps(&root, ServicePorts::default(), Some(&target), None)
                .unwrap();
        assert!(report.removed_deprecated.is_empty(), "{:?}", report.removed_deprecated);
        let json: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&target).unwrap()).unwrap();
        assert!(json["mcpServers"]["mermaid"].is_object());
        assert!(json["mcpServers"]["excalidraw"].is_object());

        fs::remove_file(&target).ok();
        fs::remove_dir_all(&root).ok();
    }

    /// LEAVE-ALONE: a user's OWN MCP named `search` (command outside
    /// install_root) is never removed.
    #[test]
    fn auto_scrub_leaves_a_users_own_search_entry() {
        let root = make_pseudo_install_root();
        let target = tmp_target();
        fs::write(
            &target,
            serde_json::to_string_pretty(&serde_json::json!({
                "mcpServers": {"search": {"command": "/usr/local/bin/my-search"}}
            }))
            .unwrap(),
        )
        .unwrap();

        let report =
            register_default_orchestrator_mcps(&root, ServicePorts::default(), Some(&target), None)
                .unwrap();
        assert!(report.removed_deprecated.is_empty(), "{:?}", report.removed_deprecated);
        let json: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&target).unwrap()).unwrap();
        assert!(json["mcpServers"]["search"].is_object());

        fs::remove_file(&target).ok();
        fs::remove_dir_all(&root).ok();
    }
}
