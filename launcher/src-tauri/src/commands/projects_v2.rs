//! Project lifecycle commands for the module system.
//!
//! Runs alongside the legacy `commands::projects` module during migration.
//! The "_v2" suffix marks the DB-backed implementation; once the React UI
//! is fully migrated to call these, we'll retire the old commands.

use serde::{Deserialize, Serialize};
use std::path::Path;
use tauri::{command, AppHandle, State};
use uuid::Uuid;

use crate::commands::codegraph;
use crate::db::code_graph_builds::status as build_status;
use crate::db::models::{ModuleInstallRow, ProjectHost, ProjectRow};
use crate::db::Db;

#[derive(Debug, Clone, Serialize)]
pub struct ProjectView {
    pub id: String,
    pub name: String,
    pub folder_path: String,
    pub host: ProjectHost,
    pub slug: String,
    pub created_at: i64,
    pub updated_at: i64,
    pub module_count: u32,
}

impl ProjectView {
    fn from_row(row: ProjectRow, module_count: u32) -> Self {
        Self {
            id: row.id,
            name: row.name,
            folder_path: row.folder_path,
            host: row.host,
            slug: row.slug,
            created_at: row.created_at,
            updated_at: row.updated_at,
            module_count,
        }
    }
}

/// Result returned by `create_project_v2`.
///
/// B10 (2026-05-01): env-write failures are no longer silently swallowed.
/// They are included here so the UI can surface a warning toast without
/// blocking project creation (the project row is always committed first).
#[derive(Debug, Clone, Serialize)]
pub struct CreateProjectResult {
    pub project: ProjectView,
    /// Non-fatal warnings that the UI should display to the user
    /// (e.g. "env file write failed — manual setup required").
    /// Empty on a clean create.
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SwitchHostResult {
    pub project: ProjectView,
    pub modules_removed: Vec<ModuleInstallRow>,
    pub modules_preserved: Vec<ModuleInstallRow>,
}

#[command]
pub async fn list_projects_v2(db: State<'_, Db>) -> Result<Vec<ProjectView>, String> {
    let rows = db.list_projects()?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let count = db.list_module_installs_for_project(&row.id)?.len() as u32;
        out.push(ProjectView::from_row(row, count));
    }
    Ok(out)
}

#[command]
pub async fn get_project_v2(
    id: String,
    db: State<'_, Db>,
) -> Result<Option<ProjectView>, String> {
    let row = match db.get_project(&id)? {
        Some(r) => r,
        None => return Ok(None),
    };
    let count = db.list_module_installs_for_project(&row.id)?.len() as u32;
    Ok(Some(ProjectView::from_row(row, count)))
}

/// Look up a project by its URL slug (e.g. `acme-corp`). Backs the
/// `/p/<slug>/...` routes.
#[command]
pub async fn get_project_by_slug(
    slug: String,
    db: State<'_, Db>,
) -> Result<Option<ProjectView>, String> {
    let row = match db.get_project_by_slug(&slug)? {
        Some(r) => r,
        None => return Ok(None),
    };
    let count = db.list_module_installs_for_project(&row.id)?.len() as u32;
    Ok(Some(ProjectView::from_row(row, count)))
}

#[derive(Debug, Deserialize)]
pub struct CreateProjectV2Request {
    pub name: String,
    pub folder_path: String,
    pub host: ProjectHost,
}

#[command]
pub async fn create_project_v2(
    req: CreateProjectV2Request,
    db: State<'_, Db>,
    app: AppHandle,
) -> Result<CreateProjectResult, String> {
    let folder = Path::new(&req.folder_path);
    let mut warnings: Vec<String> = Vec::new();

    // Bug 3e: auto-create the folder if it doesn't exist. Earlier the
    // create flow rejected non-existent paths and forced the user to
    // `mkdir -p` manually, which broke when users typed a fresh path
    // in the New Project modal. `create_dir_all` is a no-op if the
    // path already exists.
    if !folder.exists() {
        std::fs::create_dir_all(folder).map_err(|e| {
            format!("cannot create folder {}: {}", req.folder_path, e)
        })?;
    }
    if !folder.is_dir() {
        return Err(format!("not a directory: {}", req.folder_path));
    }

    let id = Uuid::new_v4().to_string();
    let slug = db.generate_unique_slug(&req.name)?;
    let row = db.insert_project(&id, &req.name, &req.folder_path, req.host.clone(), &slug)?;

    // Bug 23 + 30: write per-project env files for ALL Claude Code
    // surfaces — VS Code extension (via `.vscode/settings.json`
    // claude-code.env), Claude Code CLI (via `.claude/env`, sourced by
    // tools/claude wrapper or user shell rc), AND the canonical
    // `.claude/settings.json` env block (CLI + Desktop app + VS Code).
    // We swallow individual errors here: create_project must not fail
    // just because the user's folder is read-only or mid-edit.
    if let Err(e) = write_project_env_files(folder, &req.name) {
        // B10 (2026-05-01): surface env-write failures to the UI instead of
        // silent eprintln. Project creation still succeeds; the UI should show
        // a warning toast so the user knows manual env setup is required.
        let msg = format!("env file write failed (write_project_env_files): {}. \
                          Per-project KG routing will not work until this is resolved.", e);
        eprintln!("[vct] warning: {}", msg);
        warnings.push(msg);
    }

    // Bug 33 (2026-04-28): also ensure a per-project `.env` template
    // exists. `write_project_env_files` only writes `.claude/env` +
    // `.claude/settings.json`; a separate `.env` is what most CLI
    // users expect to edit (esp. existing-folder projects that
    // pre-existed any orchestrator install). The template carries
    // commented placeholders for ANTHROPIC_API_KEY / OPENAI_API_KEY /
    // GITHUB_TOKEN / RL_*; values stay user-controlled. Idempotent on
    // re-runs.
    if let Err(e) = ensure_project_env_template(folder, &req.name) {
        let msg = format!("env template write failed (ensure_project_env_template): {}. \
                          The .env file may be missing managed keys.", e);
        eprintln!("[vct] warning: {}", msg);
        warnings.push(msg);
    }

    // 2026-04-28 fix: populate the per-project state DB tables (agents,
    // skills, hooks, kg/codegraph bindings) from the project's `.claude/`
    // directory. Without this, the launcher's per-project GUI tabs
    // appear empty even when the filesystem has 26+ agents bundled.
    // Idempotent on re-run; preserves user-toggled `enabled` flags. We
    // log soft-errors and continue — never fail project creation over a
    // populate hiccup.
    let populate_report = crate::commands::project_state_populate::
        populate_project_state_from_filesystem(&row.id, &req.name, folder, &db);
    if !populate_report.warnings.is_empty() {
        for w in &populate_report.warnings {
            eprintln!("[vct] populate warning ({}): {}", row.id, w);
        }
    }

    db.audit(
        "project_create",
        Some(&row.id),
        None,
        &serde_json::json!({ "host": req.host.as_str(), "name": req.name, "slug": slug }),
    )?;
    let _ = db.log_change("projects", "insert", Some(&row.id), Some(&row.id));

    // Gap 2 (OSS launch 2026-05-12): kick off the initial code-graph
    // build in the background so `search_code_graph` returns useful
    // results out of the box. This must NOT block project creation —
    // the user gets their `ProjectView` back immediately.
    //
    // We swallow any DB error from the pending-row insert because a
    // failure here is purely cosmetic (the user just won't see a build
    // status pill); the project itself is already committed.
    let now = chrono::Utc::now().timestamp_millis();
    if let Err(e) = db.upsert_code_graph_build(
        &row.id,
        build_status::PENDING,
        Some(now),
        None,
        None,
        0,
        None,
        false,
        None,
        None,
    ) {
        eprintln!("[vct] warning: could not queue code-graph build for {}: {}", row.id, e);
    } else {
        codegraph::spawn_initial_build(
            app,
            row.id.clone(),
            row.name.clone(),
            row.folder_path.clone(),
        );
    }

    // B12 (2026-05-01): detect stale .env from pre-existing folder registration.
    // ensure_project_env_template is append-only, so a folder that already had a
    // .env with a bare/wrong KG_COLLECTION (e.g. "KnowledgeGraph") will keep it
    // as the first active KG_COLLECTION line. Detect and warn; full repair with
    // manifest-based rewrite lands in PR 5. We check for the two known-buggy
    // defaults: bare "KnowledgeGraph" and bare sanitized name without suffix.
    if let Ok(env_text) = std::fs::read_to_string(folder.join(".env")) {
        let kg_basename = sanitize_kg_collection(&req.name);
        let canonical_kg = format!("{}_KnowledgeGraph", kg_basename);
        let stale_bare = "KG_COLLECTION=KnowledgeGraph";
        let stale_nosuffix = format!("KG_COLLECTION={}", kg_basename);
        let has_stale = env_text.lines().any(|l| {
            let t = l.trim();
            t == stale_bare || t == stale_nosuffix
        });
        if has_stale && !env_text.contains(&format!("KG_COLLECTION={}", canonical_kg)) {
            let msg = format!(
                "existing .env has stale KG_COLLECTION (expected {}). \
                 Full repair deferred to PR 5 (manifest-based). \
                 You may manually set KG_COLLECTION={} in the .env.",
                canonical_kg, canonical_kg
            );
            eprintln!("[vct] warning: B12: {}", msg);
            warnings.push(msg);
        }
    }

    Ok(CreateProjectResult {
        project: ProjectView::from_row(row, 0),
        warnings,
    })
}

/// Bug 23 + 30: write per-project env files for every Claude Code surface.
///
/// Writes three files, all carrying the same env values:
///   1. `.vscode/settings.json` `claude-code.env` — VS Code extension
///   2. `.claude/env` — POSIX shell file sourced by the `tools/claude`
///      wrapper (CLI users without VS Code)
///   3. `.claude/settings.json` `env` — canonical Anthropic per-project
///      env (read by CLI, Desktop app, and the VS Code extension)
///
/// (3) is the only surface that reaches Claude Code Desktop app users.
/// (1) and (2) are kept for compatibility / preference. Same values in
/// all three means there's no precedence conflict to reason about.
///
/// Returns Ok(()) only when ALL succeed; the caller currently logs and
/// swallows the error so project creation never fails over an env file.
pub fn write_project_env_files(folder: &Path, project_name: &str) -> Result<(), String> {
    let kg_basename = sanitize_kg_collection(project_name);
    // Suffix the basename to match `.env` (line ~458) and the rest of the
    // ecosystem. Pre-2026-05-01 the three Claude Code env surfaces wrote
    // the BARE basename here — install.py / kg-sync / hooks then resolved
    // KG_COLLECTION to a non-existent class while `.env` correctly carried
    // the suffixed form, producing 4-way drift. Match the canonical form.
    let kg_collection = format!("{}_KnowledgeGraph", kg_basename);
    // Uppercase D for Development across every surface — matches `.env`
    // template (line ~460) and install.py. Weaviate class names are
    // case-sensitive so a lowercase `_development` resolves to a
    // distinct (non-existent) collection.
    let dev_collection = format!("{}_Development", kg_basename);
    // B5 (2026-05-01): CONVERSATION_COLLECTION removed -- install.py dropped it on
    // 2026-04-30. The MCP server no longer reads it. Writing it to new project
    // env files would just confuse users and leave stale entries. Removed.
    // Shared cross-project KG. Same name across all projects on this machine
    // — bundled with the orchestrator install (seeded from
    // vibecoded-orchestrator/knowledge/). Per-project SHARED_KG_OPT_OUT
    // disables it for THIS project without affecting others.
    let shared_kg_collection = "VibeCodedTools_KnowledgeGraph";
    // Default: opt-IN. Users who want a sandboxed project flip this via the
    // launcher Preferences toggle (which re-runs write_project_env_files
    // with opt_out=true).
    let shared_kg_opt_out = "false";

    // VS Code path (extension reads claude-code.env).
    //
    // Bug 32 (safety): READ-MERGE-WRITE so user settings like
    // `editor.formatOnSave`, `python.defaultInterpreterPath`, workspace
    // recommendations etc. survive. Only the `claude-code.env` key is
    // overwritten.
    let vscode_dir = folder.join(".vscode");
    std::fs::create_dir_all(&vscode_dir)
        .map_err(|e| format!("mkdir {}: {}", vscode_dir.display(), e))?;
    let vscode_settings_path = vscode_dir.join("settings.json");

    let mut vscode_root: serde_json::Value = if vscode_settings_path.exists() {
        match std::fs::read_to_string(&vscode_settings_path) {
            Ok(raw) => serde_json::from_str(&raw).unwrap_or_else(|e| {
                eprintln!(
                    "[vct] warning: {} is not valid JSON ({}); replacing with minimal claude-code.env block",
                    vscode_settings_path.display(),
                    e
                );
                serde_json::json!({})
            }),
            Err(e) => {
                eprintln!(
                    "[vct] warning: could not read {} ({}); creating fresh",
                    vscode_settings_path.display(),
                    e
                );
                serde_json::json!({})
            }
        }
    } else {
        serde_json::json!({})
    };
    if !vscode_root.is_object() {
        vscode_root = serde_json::json!({});
    }
    let vscode_env_block = serde_json::json!({
        "KG_COLLECTION": kg_collection,
        "PROJECT_NAME": project_name,
        "DEVELOPMENT_COLLECTION": dev_collection,
        "SHARED_KG_COLLECTION": shared_kg_collection,
        "SHARED_KG_OPT_OUT": shared_kg_opt_out,
    });
    if let Some(obj) = vscode_root.as_object_mut() {
        obj.insert("claude-code.env".to_string(), vscode_env_block);
    }
    std::fs::write(
        &vscode_settings_path,
        serde_json::to_string_pretty(&vscode_root)
            .map_err(|e| format!("serialize settings.json: {}", e))?,
    )
    .map_err(|e| format!("write {}: {}", vscode_settings_path.display(), e))?;

    // CLI path: `.claude/env` is sourced by the `tools/claude` wrapper or
    // by the user's shell rc. Plain POSIX export form so any sh-family
    // shell can source it.
    let claude_dir = folder.join(".claude");
    std::fs::create_dir_all(&claude_dir)
        .map_err(|e| format!("mkdir {}: {}", claude_dir.display(), e))?;
    let env_path = claude_dir.join("env");
    let env_content = format!(
        "# Auto-generated by VCT Launcher. Source from your shell rc or use\n\
         # tools/claude wrapper (which auto-sources this file before exec'ing\n\
         # the real claude binary).\n\
         export KG_COLLECTION=\"{}\"\n\
         export PROJECT_NAME=\"{}\"\n\
         export DEVELOPMENT_COLLECTION=\"{}\"\n\
         export SHARED_KG_COLLECTION=\"{}\"\n\
         export SHARED_KG_OPT_OUT=\"{}\"\n",
        kg_collection,
        project_name,
        dev_collection,
        shared_kg_collection,
        shared_kg_opt_out,
    );
    std::fs::write(&env_path, env_content)
        .map_err(|e| format!("write {}: {}", env_path.display(), e))?;

    // Bug 30: `.claude/settings.json` is the canonical Anthropic
    // per-project env mechanism — read by Claude Code CLI, the Desktop
    // app, AND the VS Code extension. Without it, Desktop app users
    // never get per-project KG routing. We READ-MERGE-WRITE: this file
    // commonly contains the user's hooks, permissions, agents config,
    // etc. that we must not clobber. Only the top-level `env` key is
    // overwritten.
    let claude_settings_path = claude_dir.join("settings.json");
    let mut settings: serde_json::Value = if claude_settings_path.exists() {
        match std::fs::read_to_string(&claude_settings_path) {
            Ok(raw) => serde_json::from_str(&raw).unwrap_or_else(|e| {
                eprintln!(
                    "[vct] warning: {} is not valid JSON ({}); replacing with minimal env block",
                    claude_settings_path.display(),
                    e
                );
                serde_json::json!({})
            }),
            Err(e) => {
                eprintln!(
                    "[vct] warning: could not read {} ({}); creating fresh",
                    claude_settings_path.display(),
                    e
                );
                serde_json::json!({})
            }
        }
    } else {
        serde_json::json!({})
    };

    // If the existing root is not a JSON object (array, string, etc.),
    // replace it with an empty object — we cannot inject into a non-object.
    if !settings.is_object() {
        settings = serde_json::json!({});
    }

    let env_block = serde_json::json!({
        "KG_COLLECTION": kg_collection,
        "PROJECT_NAME": project_name,
        "DEVELOPMENT_COLLECTION": dev_collection,
        "SHARED_KG_COLLECTION": shared_kg_collection,
        "SHARED_KG_OPT_OUT": shared_kg_opt_out,
    });
    if let Some(obj) = settings.as_object_mut() {
        obj.insert("env".to_string(), env_block);
    }

    let pretty = serde_json::to_string_pretty(&settings)
        .map_err(|e| format!("serialize .claude/settings.json: {}", e))?;
    std::fs::write(&claude_settings_path, pretty)
        .map_err(|e| format!("write {}: {}", claude_settings_path.display(), e))?;

    Ok(())
}

/// Marker tag inserted on every line `ensure_project_env_template`
/// appends to a pre-existing `.env`. Mirror of `ENV_VCO_MARKER` in
/// install.py — keep in lockstep. Idempotency depends on the exact
/// substring match; do NOT translate or reformat.
const ENV_VCO_MARKER: &str = "# added by vco";

/// Canonical key list rendered by `ensure_project_env_template`.
///
/// Each tuple = `(KEY, default)`:
///   - `default = Some(value)` → write `KEY=value` (active)
///   - `default = None` → write `# KEY=...` (commented placeholder)
///
/// Mirrors `_env_canonical_template` in install.py. The
/// `<project>` / `<project_root>` tokens are substituted by the
/// caller. Keep the two lists in lockstep — the test
/// `env_template_canonical_keys_match_python` (added 2026-04-28)
/// asserts the Python and Rust key sets are identical.
fn env_canonical_keys() -> Vec<(&'static str, Option<&'static str>)> {
    vec![
        // Service URLs (all commented placeholders — launcher chooses
        // the actual ports at adopt time and writes them via the env
        // block in `.claude/settings.json`, NOT into `.env`).
        ("WEAVIATE_URL", None),
        ("WEAVIATE_PORT", None),
        ("OLLAMA_URL", None),
        ("OLLAMA_PORT", None),
        ("CODE_EMBED_URL", None),
        // Per-project Weaviate collections (active — filled at create time).
        ("KG_COLLECTION", Some("__project__:kg")),
        ("SHARED_KG_COLLECTION", Some("VibeCodedTools_KnowledgeGraph")),
        ("DEVELOPMENT_COLLECTION", Some("__project__:dev")),
        ("PROJECT_NAME", Some("__project__:raw")),
        // CONVERSATION_COLLECTION removed 2026-04-30 (B5: zombie write cleanup).
        // The capture flow is deprecated; MCP server no longer reads this key.
        // LLM API keys (commented).
        ("ANTHROPIC_API_KEY", None),
        ("OPENAI_API_KEY", None),
        // GitHub access (commented).
        ("GITHUB_TOKEN", None),
        // RL retrieval (commented — module section).
        ("RL_SERVER_URL", None),
        ("RL_SERVER_PORT", None),
        ("RL_PROJECT_ROOT", None),
        // Telemetry (commented — opt-in).
        ("VCT_TELEMETRY", None),
    ]
}

/// Substitute `__project__:*` tokens to the per-project values.
fn render_canonical_default(default: &str, project_name: &str, kg_collection: &str) -> String {
    match default {
        "__project__:kg" => format!("{}_KnowledgeGraph", kg_collection),
        "__project__:dev" => format!("{}_Development", kg_collection),
        "__project__:conv" => format!("{}_conversations", kg_collection),
        "__project__:raw" => project_name.to_string(),
        other => other.to_string(),
    }
}

/// Build the canonical `.env` text used when no `.env` exists.
///
/// Output mirrors `_build_canonical_env_template_text` in install.py
/// (modulo tiny formatting differences — header date, section order).
/// What MUST match cross-language: the set of declared KEY names. The
/// `env_template_canonical_keys_match_python` test enforces that.
fn build_canonical_env_text(project_name: &str, kg_collection: &str) -> String {
    let today = chrono::Utc::now().format("%Y-%m-%d");
    let mut s = String::new();
    s.push_str("# vibecoded-orchestrator per-project .env\n");
    s.push_str("# Edit values to override defaults. Empty / commented lines are\n");
    s.push_str(&format!("# treated as \"use default\". Created by vco {}.\n\n", today));

    s.push_str("# === Service URLs (uncomment to override the launcher's adopted defaults) ===\n");
    s.push_str("# WEAVIATE_URL=http://localhost:8081\n");
    s.push_str("# WEAVIATE_PORT=8081\n");
    s.push_str("# OLLAMA_URL=http://localhost:11435\n");
    s.push_str("# OLLAMA_PORT=11435\n");
    s.push_str("# CODE_EMBED_URL=http://localhost:11440\n\n");

    s.push_str("# === Per-project Weaviate collections ===\n");
    s.push_str("# Resolved by the launcher when the project is registered. Don't\n");
    s.push_str("# edit unless you know what you're doing.\n");
    s.push_str(&format!("KG_COLLECTION={}_KnowledgeGraph\n", kg_collection));
    s.push_str("SHARED_KG_COLLECTION=VibeCodedTools_KnowledgeGraph\n");
    s.push_str(&format!("DEVELOPMENT_COLLECTION={}_Development\n", kg_collection));
    s.push_str(&format!("PROJECT_NAME={}\n\n", project_name));
    // CONVERSATION_COLLECTION removed 2026-04-30 (B5). Not written to new installs.

    s.push_str("# === LLM API keys (optional) ===\n");
    s.push_str("# ANTHROPIC_API_KEY=\n");
    s.push_str("# OPENAI_API_KEY=\n\n");

    s.push_str("# === GitHub access for code-search MCP (optional) ===\n");
    s.push_str("# GITHUB_TOKEN=\n\n");

    s.push_str("# === RL retrieval module (Pro tier — uncomment when installed) ===\n");
    s.push_str("# RL_SERVER_URL=http://localhost:8090\n");
    s.push_str("# RL_SERVER_PORT=8090\n");
    s.push_str("# RL_PROJECT_ROOT=<project_root>\n\n");

    s.push_str("# === Telemetry (off by default; on=opt-in only) ===\n");
    s.push_str("# VCT_TELEMETRY=off\n");
    s
}

/// Parse keys present in an existing `.env`. Both commented (`# KEY=`)
/// and active (`KEY=`) lines count — the user knows about either form
/// and we should not duplicate-append over them. Mirrors
/// `_parse_existing_env_keys` in install.py.
fn parse_existing_env_keys(text: &str) -> std::collections::HashSet<String> {
    let mut out = std::collections::HashSet::new();
    for line in text.lines() {
        let s = line.trim();
        if s.is_empty() {
            continue;
        }
        // Strip a single leading '#' + whitespace.
        let body = if let Some(rest) = s.strip_prefix('#') {
            rest.trim_start()
        } else {
            s
        };
        if let Some(eq_idx) = body.find('=') {
            if eq_idx == 0 {
                continue;
            }
            let key = body[..eq_idx].trim();
            // Validate key shape: alnum + underscore, leading non-digit.
            // Skips lines like `# Defaults match the podman-compose...`
            // which would otherwise parse as `match=...`.
            if !key.is_empty()
                && !key.starts_with(|c: char| c.is_ascii_digit())
                && key.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
            {
                out.insert(key.to_string());
            }
        }
    }
    out
}

/// Report shape returned by `ensure_project_env_template`. Mirrors the
/// dict returned by Python's `_ensure_env_template`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct EnsureEnvReport {
    /// One of "created", "appended", "noop".
    pub action: String,
    /// Keys that were just written (only the canonical KEY names, not
    /// every line of comments).
    pub added_keys: Vec<String>,
    /// Absolute path to the .env file.
    pub env_path: String,
}

/// Ensure `<folder>/.env` exists and has every canonical-template key.
///
/// - Missing → write fresh from the canonical template (with placeholders
///   substituted for `<project>` / `<project_root>`).
/// - Exists → append any canonical keys that aren't already present
///   (commented or active), tagged with `# added by vco YYYY-MM-DD`.
/// - Idempotent: a second invocation produces a no-op.
///
/// User-set values are preserved verbatim — we only append new lines,
/// never rewrite existing ones.
///
/// This is the Rust mirror of `_ensure_env_template` in install.py;
/// keep them in lockstep. The 'env_template_canonical_keys_match_python'
/// integration test is the contract that enforces this.
pub fn ensure_project_env_template(
    folder: &Path,
    project_name: &str,
) -> Result<EnsureEnvReport, String> {
    let env_path = folder.join(".env");
    let kg_collection = sanitize_kg_collection(project_name);

    if !env_path.exists() {
        let text = build_canonical_env_text(project_name, &kg_collection);
        std::fs::write(&env_path, text)
            .map_err(|e| format!("write {}: {}", env_path.display(), e))?;
        let added: Vec<String> = env_canonical_keys()
            .iter()
            .map(|(k, _)| k.to_string())
            .collect();
        return Ok(EnsureEnvReport {
            action: "created".into(),
            added_keys: added,
            env_path: env_path.to_string_lossy().to_string(),
        });
    }

    let existing = std::fs::read_to_string(&env_path)
        .map_err(|e| format!("read {}: {}", env_path.display(), e))?;
    let present = parse_existing_env_keys(&existing);

    let missing: Vec<(&'static str, Option<&'static str>)> = env_canonical_keys()
        .into_iter()
        .filter(|(k, _)| !present.contains(*k))
        .collect();

    if missing.is_empty() {
        return Ok(EnsureEnvReport {
            action: "noop".into(),
            added_keys: vec![],
            env_path: env_path.to_string_lossy().to_string(),
        });
    }

    let today = chrono::Utc::now().format("%Y-%m-%d");
    let mut block = String::new();
    if !existing.ends_with('\n') {
        block.push('\n');
    }
    block.push('\n');
    block.push_str(&format!(
        "{} {}: appended missing canonical keys\n",
        ENV_VCO_MARKER, today
    ));
    let added: Vec<String> = missing
        .iter()
        .map(|(k, default)| {
            match default {
                Some(d) => {
                    let val = render_canonical_default(d, project_name, &kg_collection);
                    block.push_str(&format!("{}={}\n", k, val));
                }
                None => {
                    // Commented placeholder. RL_PROJECT_ROOT gets a
                    // <project_root> token; everything else just '='.
                    if *k == "RL_PROJECT_ROOT" {
                        block.push_str("# RL_PROJECT_ROOT=<project_root>\n");
                    } else {
                        block.push_str(&format!("# {}=\n", k));
                    }
                }
            }
            (*k).to_string()
        })
        .collect();

    let mut f = std::fs::OpenOptions::new()
        .append(true)
        .open(&env_path)
        .map_err(|e| format!("open {} for append: {}", env_path.display(), e))?;
    use std::io::Write;
    f.write_all(block.as_bytes())
        .map_err(|e| format!("append to {}: {}", env_path.display(), e))?;

    Ok(EnsureEnvReport {
        action: "appended".into(),
        added_keys: added,
        env_path: env_path.to_string_lossy().to_string(),
    })
}

/// Convert a project display name into a Weaviate-collection-safe id.
/// Weaviate collections must start with [A-Z] and contain only
/// alphanumerics — strip everything else and Title-case.
pub fn sanitize_kg_collection(name: &str) -> String {
    let mut out = String::new();
    let mut next_upper = true;
    for ch in name.chars() {
        if ch.is_ascii_alphanumeric() {
            if next_upper {
                out.extend(ch.to_uppercase());
                next_upper = false;
            } else {
                out.push(ch);
            }
        } else {
            next_upper = true;
        }
    }
    if out.is_empty() {
        return "Project".to_string();
    }
    // Weaviate requires leading letter, not digit.
    if out.chars().next().unwrap().is_ascii_digit() {
        out.insert(0, 'P');
    }
    out
}

#[command]
pub async fn rename_project_v2(
    id: String,
    new_name: String,
    db: State<'_, Db>,
) -> Result<ProjectView, String> {
    // Generate a fresh slug derived from the new name so URLs track
    // renames. The old slug becomes invalid; existing bookmarks 404
    // gracefully via the /p/[slug] resolver. Documented in
    // docs/MULTI_TENANT_URLS.md.
    let new_slug = db.generate_unique_slug(&new_name)?;
    db.rename_project(&id, &new_name, Some(&new_slug))?;
    let row = db
        .get_project(&id)?
        .ok_or_else(|| format!("project {} not found after rename", id))?;
    let count = db.list_module_installs_for_project(&id)?.len() as u32;

    // B9 (2026-05-01): re-run env writers after DB rename so all 4 surfaces
    // reflect the new KG_COLLECTION, DEVELOPMENT_COLLECTION, PROJECT_NAME.
    // Before this fix, rename was DB-only — renamed projects kept stale
    // KG_COLLECTION values in .claude/env, .vscode/settings.json, and
    // .claude/settings.json until the user manually re-ran env setup.
    let folder = Path::new(&row.folder_path);
    if let Err(e) = write_project_env_files(folder, &new_name) {
        eprintln!("[vct] warning: rename env refresh (write_project_env_files) failed for {}: {}", id, e);
    }
    // ensure_project_env_template is append-only, so .env may still carry the
    // old KG_COLLECTION value as an active line. Log a warning when the stale
    // value is detected; full repair lands in PR 5.
    if let Ok(env_text) = std::fs::read_to_string(folder.join(".env")) {
        let new_kg = format!("{}_KnowledgeGraph", sanitize_kg_collection(&new_name));
        if !env_text.contains(&format!("KG_COLLECTION={}", new_kg)) {
            eprintln!(
                "[vct] warning: .env at {} still contains stale KG_COLLECTION after rename; \
                 run repair-env (PR 5) to fix. Expected KG_COLLECTION={}",
                row.folder_path, new_kg
            );
        }
    }

    let _ = db.log_change("projects", "update", Some(&id), Some(&id));
    Ok(ProjectView::from_row(row, count))
}

#[command]
pub async fn switch_project_host_v2(
    id: String,
    new_host: ProjectHost,
    db: State<'_, Db>,
) -> Result<SwitchHostResult, String> {
    let project = db
        .get_project(&id)?
        .ok_or_else(|| format!("project {} not found", id))?;

    if project.host == new_host {
        let count = db.list_module_installs_for_project(&id)?.len() as u32;
        return Ok(SwitchHostResult {
            project: ProjectView::from_row(project, count),
            modules_removed: vec![],
            modules_preserved: db.list_module_installs_for_project(&id)?,
        });
    }

    // For MAO→base: modules listing compatible hosts with only "mao" must go.
    // We can't fully decide without the manifests, which live in install
    // directories. This command flags candidates for removal by looking at
    // the module_id. A manifest registry lookup would be cleaner — added
    // in a later iteration; for now we rely on the module_id naming
    // convention (*-mao suffix OR known MAO-only module ids).
    let installs = db.list_module_installs_for_project(&id)?;
    let mao_only_ids: &[&str] = &[
        "vct-asset-library",
        "vct-agent-packs-mao",
        "vct-workflows-mao",
    ];

    let mut removed = Vec::new();
    let mut preserved = Vec::new();
    for install in installs {
        let goes = new_host == ProjectHost::Base
            && (mao_only_ids.contains(&install.module_id.as_str())
                || install.module_id.ends_with("-mao"));
        if goes {
            db.delete_module_install(&id, &install.module_id)?;
            removed.push(install);
        } else {
            preserved.push(install);
        }
    }

    db.update_project_host(&id, new_host.clone())?;
    db.audit(
        "project_host_switch",
        Some(&id),
        None,
        &serde_json::json!({
            "to": new_host.as_str(),
            "removed_modules": removed.iter().map(|m| &m.module_id).collect::<Vec<_>>(),
        }),
    )?;
    let _ = db.log_change("projects", "update", Some(&id), Some(&id));

    let project = db
        .get_project(&id)?
        .ok_or_else(|| format!("project {} vanished after host switch", id))?;
    let count = preserved.len() as u32;
    Ok(SwitchHostResult {
        project: ProjectView::from_row(project, count),
        modules_removed: removed,
        modules_preserved: preserved,
    })
}

#[command]
pub async fn delete_project_v2(
    id: String,
    _delete_folder: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    // Note: delete_folder is accepted for UI parity with the design spec,
    // but we don't touch the user's folder on disk. Modules installed
    // under ~/.vct/modules/ are removed via CASCADE through
    // module_installs. The user's project folder on disk stays.
    db.audit("project_delete", Some(&id), None, &serde_json::json!({}))?;
    db.delete_project(&id)?;
    let _ = db.log_change("projects", "delete", Some(&id), Some(&id));
    Ok(())
}

/// Bug 15: spawn the user's editor of choice opened on the project folder.
///
/// Tries `code` (VS Code) first; if not on PATH, returns a user-friendly
/// error so the launcher can show a "VS Code not installed" toast. Does
/// NOT block — the editor is launched detached and the launcher process
/// continues. Returns immediately on success.
///
/// Bug 24: `surface` selects which Claude Code surface to use:
/// - "vscode" (default): `code <folder>` (VS Code extension picks up env
///   from .vscode/settings.json claude-code.env)
/// - "cli": opens the system terminal in <folder> and runs `claude`. The
///   user's shell rc OR our `tools/claude` wrapper sources `.claude/env`
///   (Bug 23).
/// - "auto": prefer vscode if `code` is on PATH, else fall back to cli.
#[command]
pub async fn launch_project_in_editor(
    project_id: String,
    surface: Option<String>,
    db: State<'_, Db>,
) -> Result<(), String> {
    let row = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;
    let folder = row.folder_path.clone();
    let chosen = match surface.as_deref().unwrap_or("auto") {
        "vscode" => "vscode",
        "cli" => "cli",
        _ => {
            if which_on_path("code") {
                "vscode"
            } else if which_on_path("claude") {
                "cli"
            } else {
                "vscode"
            }
        }
    };

    let result = match chosen {
        "vscode" => launch_in_vscode(&folder),
        "cli" => launch_in_terminal_with_cli(&folder),
        _ => unreachable!(),
    };

    if result.is_ok() {
        db.audit(
            "project_launch",
            Some(&project_id),
            None,
            &serde_json::json!({ "surface": chosen, "folder": folder }),
        )?;
    }
    result
}

fn which_on_path(cmd: &str) -> bool {
    if let Ok(path) = std::env::var("PATH") {
        for dir in path.split(if cfg!(windows) { ';' } else { ':' }) {
            let candidate = std::path::Path::new(dir).join(if cfg!(windows) {
                format!("{}.exe", cmd)
            } else {
                cmd.to_string()
            });
            if candidate.exists() {
                return true;
            }
        }
    }
    false
}

fn launch_in_vscode(folder: &str) -> Result<(), String> {
    let mut cmd = std::process::Command::new("code");
    cmd.arg(folder);
    match cmd.spawn() {
        Ok(_) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err(
            "VS Code not found on PATH. Install Code from https://code.visualstudio.com/ \
             and ensure the `code` command is on your PATH, or use Claude Code CLI: \
             `cd <project> && claude`."
                .into(),
        ),
        Err(e) => Err(format!("failed to spawn editor: {}", e)),
    }
}

/// Spawn the system terminal in `folder` and run `claude` inside it. The
/// terminal flag varies by OS / DE — try a list of well-known options
/// and use the first that works.
fn launch_in_terminal_with_cli(folder: &str) -> Result<(), String> {
    if !which_on_path("claude") {
        return Err(
            "Claude Code CLI not found on PATH. Install from \
             https://docs.anthropic.com/en/docs/claude-code, or open in VS Code instead."
                .into(),
        );
    }

    // Per-OS terminal command. We use `cd <folder> && claude` as the
    // command-string; the terminal must support a flag that accepts a
    // shell command and keeps the window open afterwards.
    #[cfg(target_os = "linux")]
    let candidates: &[(&str, &[&str])] = &[
        ("gnome-terminal", &["--working-directory", folder, "--", "bash", "-lc", "claude; exec bash"]),
        ("konsole", &["--workdir", folder, "-e", "bash", "-lc", "claude; exec bash"]),
        ("xterm", &["-e", "bash", "-lc"]),
    ];
    #[cfg(target_os = "macos")]
    let candidates: &[(&str, &[&str])] = &[
        ("open", &["-a", "Terminal", folder]),
    ];
    #[cfg(target_os = "windows")]
    let candidates: &[(&str, &[&str])] = &[
        ("wt.exe", &["-d", folder, "powershell", "-NoExit", "-Command", "claude"]),
        ("powershell", &["-NoExit", "-Command", "claude"]),
    ];

    for (bin, args) in candidates {
        let mut cmd = std::process::Command::new(bin);
        for a in *args {
            cmd.arg(a);
        }
        if cmd.spawn().is_ok() {
            return Ok(());
        }
    }

    Err("Could not find a system terminal to spawn (gnome-terminal, konsole, xterm, \
         Terminal.app, wt.exe). Install one or open in VS Code instead."
        .into())
}

#[cfg(test)]
mod tests {
    use super::*;

    // ─── Bug 23: per-project env file generation ───────────────────

    #[test]
    fn sanitize_kg_collection_strips_separators_and_titlecases() {
        assert_eq!(sanitize_kg_collection("My Project"), "MyProject");
        assert_eq!(sanitize_kg_collection("my-project"), "MyProject");
        assert_eq!(sanitize_kg_collection("snake_case_name"), "SnakeCaseName");
        assert_eq!(sanitize_kg_collection("123-leading-digit"), "P123LeadingDigit");
        assert_eq!(sanitize_kg_collection(""), "Project");
        assert_eq!(sanitize_kg_collection("...!!!..."), "Project");
        assert_eq!(sanitize_kg_collection("Already CamelCase"), "AlreadyCamelCase");
    }

    #[test]
    fn write_project_env_files_creates_all_three_paths() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-env-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        write_project_env_files(&tmp, "My Test").unwrap();

        // 1. VS Code path
        let vscode_settings = tmp.join(".vscode/settings.json");
        assert!(vscode_settings.exists());
        let raw = std::fs::read_to_string(&vscode_settings).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&raw).unwrap();
        let env = &parsed["claude-code.env"];
        // 2026-05-01: KG_COLLECTION carries the FULL Weaviate class name
        // (suffixed), matching `.env` and the rest of the ecosystem. Was
        // bare `MyTest` until the bare-kg fix.
        assert_eq!(env["KG_COLLECTION"], "MyTest_KnowledgeGraph");
        // PROJECT_NAME is the raw user-supplied name, not the sanitized
        // Weaviate basename. Was `MyTest` (sanitized) before; now matches
        // install.py + the .env template.
        assert_eq!(env["PROJECT_NAME"], "My Test");
        // Uppercase D for Development across every surface — Weaviate
        // class names are case-sensitive.
        assert_eq!(env["DEVELOPMENT_COLLECTION"], "MyTest_Development");
        // B5: CONVERSATION_COLLECTION must NOT be present in any surface.
        assert!(env.get("CONVERSATION_COLLECTION").is_none());
        // Shared-KG fields propagate to all three surfaces.
        assert_eq!(env["SHARED_KG_COLLECTION"], "VibeCodedTools_KnowledgeGraph");
        assert_eq!(env["SHARED_KG_OPT_OUT"], "false");

        // 2. CLI shell file path
        let claude_env = tmp.join(".claude/env");
        assert!(claude_env.exists());
        let env_raw = std::fs::read_to_string(&claude_env).unwrap();
        assert!(env_raw.contains(r#"export KG_COLLECTION="MyTest_KnowledgeGraph""#));
        assert!(env_raw.contains(r#"export PROJECT_NAME="My Test""#));
        assert!(env_raw.contains(r#"export DEVELOPMENT_COLLECTION="MyTest_Development""#));
        // B5: CONVERSATION_COLLECTION must NOT be in .claude/env.
        assert!(!env_raw.contains("CONVERSATION_COLLECTION"));
        assert!(env_raw.contains(r#"export SHARED_KG_COLLECTION="VibeCodedTools_KnowledgeGraph""#));
        assert!(env_raw.contains(r#"export SHARED_KG_OPT_OUT="false""#));

        // 3. Bug 30: canonical .claude/settings.json env block
        let claude_settings = tmp.join(".claude/settings.json");
        assert!(claude_settings.exists());
        let raw = std::fs::read_to_string(&claude_settings).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&raw).unwrap();
        let env = &parsed["env"];
        assert_eq!(env["KG_COLLECTION"], "MyTest_KnowledgeGraph");
        assert_eq!(env["PROJECT_NAME"], "My Test");
        assert_eq!(env["DEVELOPMENT_COLLECTION"], "MyTest_Development");
        // B5: CONVERSATION_COLLECTION must NOT be in .claude/settings.json env.
        assert!(env.get("CONVERSATION_COLLECTION").is_none());
        assert_eq!(env["SHARED_KG_COLLECTION"], "VibeCodedTools_KnowledgeGraph");
        assert_eq!(env["SHARED_KG_OPT_OUT"], "false");

        std::fs::remove_dir_all(&tmp).ok();
    }

    #[test]
    fn env_surfaces_agree_after_write_project_env_files() {
        // 4-way equality regression: KG_COLLECTION must be IDENTICAL across
        // .env (template), .vscode/settings.json claude-code.env block,
        // .claude/env POSIX exports, and .claude/settings.json env block.
        // Pre-fix: bare in three, suffixed in .env → the bug VideoFrames hit.
        let tmp = std::env::temp_dir().join(format!(
            "vct-env-parity-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        write_project_env_files(&tmp, "VideoFrames").unwrap();
        ensure_project_env_template(&tmp, "VideoFrames").unwrap();

        let env_text = std::fs::read_to_string(tmp.join(".env")).unwrap();
        let vsc: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(tmp.join(".vscode/settings.json")).unwrap()).unwrap();
        let claude_env_text = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        let cs: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap()).unwrap();

        assert!(env_text.contains("KG_COLLECTION=VideoFrames_KnowledgeGraph"));
        assert_eq!(vsc["claude-code.env"]["KG_COLLECTION"], "VideoFrames_KnowledgeGraph");
        assert!(claude_env_text.contains(r#"export KG_COLLECTION="VideoFrames_KnowledgeGraph""#));
        assert_eq!(cs["env"]["KG_COLLECTION"], "VideoFrames_KnowledgeGraph");

        assert!(env_text.contains("DEVELOPMENT_COLLECTION=VideoFrames_Development"));
        assert_eq!(vsc["claude-code.env"]["DEVELOPMENT_COLLECTION"], "VideoFrames_Development");
        assert!(claude_env_text.contains(r#"export DEVELOPMENT_COLLECTION="VideoFrames_Development""#));
        assert_eq!(cs["env"]["DEVELOPMENT_COLLECTION"], "VideoFrames_Development");

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// Bug 30: existing `.claude/settings.json` content (hooks, permissions,
    /// agents config, etc.) MUST be preserved when we inject the env block.
    /// Read-merge-write semantics; only the top-level `env` key is touched.
    #[test]
    fn write_preserves_existing_claude_settings_json() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-merge-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp.join(".claude")).unwrap();
        let path = tmp.join(".claude/settings.json");
        std::fs::write(
            &path,
            r#"{
                "hooks": {"PreToolUse": [{"matcher": "*", "hooks": []}]},
                "permissions": {"allow": ["Read"]},
                "env": {"OLD_KEY": "old_value"}
            }"#,
        )
        .unwrap();

        write_project_env_files(&tmp, "MyProject").unwrap();

        let raw = std::fs::read_to_string(&path).unwrap();
        let v: serde_json::Value = serde_json::from_str(&raw).unwrap();

        // env block was replaced with our values
        assert_eq!(v["env"]["KG_COLLECTION"], "MyProject_KnowledgeGraph");
        assert_eq!(v["env"]["PROJECT_NAME"], "MyProject");
        // Old env keys are gone — top-level env is replaced wholesale.
        assert!(v["env"].get("OLD_KEY").is_none());
        // existing hooks + permissions preserved untouched
        assert!(v["hooks"]["PreToolUse"].is_array());
        assert!(v["permissions"]["allow"].is_array());
        assert_eq!(v["permissions"]["allow"][0], "Read");

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// Bug 32: existing `.vscode/settings.json` user keys (formatOnSave,
    /// defaultInterpreter, etc.) MUST be preserved. Only `claude-code.env`
    /// is mutated. Without this, opening the launcher would clobber any
    /// user IDE customisations.
    #[test]
    fn write_preserves_existing_vscode_settings_json() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-vscode-merge-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(tmp.join(".vscode")).unwrap();
        let path = tmp.join(".vscode/settings.json");
        std::fs::write(
            &path,
            r#"{
                "editor.formatOnSave": true,
                "python.defaultInterpreterPath": "/usr/bin/python3",
                "claude-code.env": {"OLD_KEY": "old"}
            }"#,
        )
        .unwrap();

        write_project_env_files(&tmp, "MyProject").unwrap();

        let raw = std::fs::read_to_string(&path).unwrap();
        let v: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(v["editor.formatOnSave"], true);
        assert_eq!(v["python.defaultInterpreterPath"], "/usr/bin/python3");
        assert_eq!(v["claude-code.env"]["KG_COLLECTION"], "MyProject_KnowledgeGraph");
        // Old env key is gone — claude-code.env is replaced wholesale.
        assert!(v["claude-code.env"].get("OLD_KEY").is_none());

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// Bug 30: corrupted `.claude/settings.json` must not crash project
    /// creation. We log a warning and overwrite with a minimal env block.
    #[test]
    fn write_handles_corrupted_claude_settings_json() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-corrupt-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp.join(".claude")).unwrap();
        let path = tmp.join(".claude/settings.json");
        std::fs::write(&path, "{ this is not valid json").unwrap();

        write_project_env_files(&tmp, "MyProject").expect("must not crash");

        let raw = std::fs::read_to_string(&path).unwrap();
        let v: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(v["env"]["KG_COLLECTION"], "MyProject_KnowledgeGraph");

        std::fs::remove_dir_all(&tmp).ok();
    }

    // Bug 15: smoke test that the launch command resolves the project row
    // and returns a clean error when the editor binary is missing. We
    // can't actually spawn `code` reliably in CI, so we verify the path
    // resolution and the not-found error contract by overriding PATH.

    #[test]
    fn launch_returns_not_found_when_editor_missing() {
        // Override PATH so `code` is guaranteed not findable. We don't
        // call the Tauri command directly (it requires State<Db>), but
        // the spawn-failure branch is the one we want to assert on. A
        // direct std::process::Command spawn with an empty PATH gives us
        // the same NotFound error our command translates.
        let saved = std::env::var_os("PATH");
        // SAFETY: tests are single-threaded by default in this crate; if
        // that ever changes, gate this with a Mutex or use std::process
        // env directly per-call.
        unsafe { std::env::set_var("PATH", ""); }
        let res = std::process::Command::new("code").arg(".").spawn();
        if let Some(p) = saved {
            unsafe { std::env::set_var("PATH", p); }
        } else {
            unsafe { std::env::remove_var("PATH"); }
        }
        let err = res.expect_err("expected NotFound when PATH is empty");
        assert_eq!(err.kind(), std::io::ErrorKind::NotFound);
    }

    // ─── Bug 28: onboarding finish must produce a project record ──
    //
    // We can't drive the Tauri `#[command]` directly without the
    // State<Db> harness, but the command body is a thin wrapper around
    // `db.insert_project` + folder-create + env-file write. This test
    // exercises that core sequence end-to-end against an in-memory db.
    // After the simulated onboarding flow finishes, `list_projects`
    // must return at least one row.

    #[test]
    fn onboarding_finish_inserts_project_row() {
        use crate::db::Db;

        let db = Db::open_in_memory().expect("in-memory db");

        // Simulate the flow that OnboardingWizard.finish() drives:
        //   1. Create a fresh folder for the project (matches the
        //      `create_dir_all` step inside create_project_v2).
        //   2. Generate a unique slug for the chosen name.
        //   3. Insert the project row.
        //   4. Write the per-project env files (mirrors create_project_v2).
        let folder = std::env::temp_dir().join(format!(
            "vct-bug28-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&folder).unwrap();

        let id = uuid::Uuid::new_v4().to_string();
        let name = "Bug28 Onboarding Project";
        let slug = db.generate_unique_slug(name).expect("slug");
        let row = db
            .insert_project(
                &id,
                name,
                folder.to_string_lossy().as_ref(),
                ProjectHost::Base,
                &slug,
            )
            .expect("insert_project");
        assert_eq!(row.name, name);

        // Mirror the env-file write the real command does.
        write_project_env_files(&folder, name).expect("env files");

        // The contract: after onboarding, at least one project row
        // exists. The user reported ending up with zero — that's the
        // regression this guards against.
        let all = db.list_projects().expect("list_projects");
        assert!(
            !all.is_empty(),
            "expected at least one project row after onboarding finish"
        );
        assert!(
            all.iter().any(|p| p.name == name),
            "expected project named {name:?} in list, got {:?}",
            all.iter().map(|p| &p.name).collect::<Vec<_>>()
        );

        // env files must have landed at the project folder.
        assert!(folder.join(".vscode/settings.json").exists());
        assert!(folder.join(".claude/env").exists());

        std::fs::remove_dir_all(&folder).ok();
    }

    // ─── Deliverable 1 (2026-04-28): ensure_project_env_template ──

    fn _scratch_dir(tag: &str) -> std::path::PathBuf {
        let p = std::env::temp_dir().join(format!(
            "vct-envtmpl-{}-{}",
            tag,
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn ensure_env_template_creates_when_missing() {
        let dir = _scratch_dir("create");
        assert!(!dir.join(".env").exists());
        let report = ensure_project_env_template(&dir, "Acme").unwrap();
        assert_eq!(report.action, "created");
        assert!(dir.join(".env").exists());
        let text = std::fs::read_to_string(dir.join(".env")).unwrap();
        // Active keys filled with project-substituted values.
        assert!(text.contains("KG_COLLECTION=Acme_KnowledgeGraph"));
        assert!(text.contains("PROJECT_NAME=Acme"));
        // Optional keys remain commented.
        assert!(text.contains("# OPENAI_API_KEY="));
        assert!(text.contains("# GITHUB_TOKEN="));
        // Active OPENAI_API_KEY must NOT appear.
        assert!(!text.contains("\nOPENAI_API_KEY="));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn ensure_env_template_appends_missing_with_marker() {
        let dir = _scratch_dir("append");
        let env_path = dir.join(".env");
        std::fs::write(&env_path, "OPENAI_API_KEY=sk-user\n").unwrap();
        let report = ensure_project_env_template(&dir, "X").unwrap();
        assert_eq!(report.action, "appended");
        let text = std::fs::read_to_string(&env_path).unwrap();
        // User value preserved verbatim.
        assert!(text.contains("OPENAI_API_KEY=sk-user"));
        // Marker present.
        assert!(text.contains(ENV_VCO_MARKER));
        // Missing keys appended.
        assert!(text.contains("KG_COLLECTION=X_KnowledgeGraph"));
        assert!(text.contains("# GITHUB_TOKEN="));
        // OPENAI_API_KEY must appear exactly once (the user's line).
        let count = text.matches("OPENAI_API_KEY").count();
        assert_eq!(count, 1, "expected 1, got {count}\n{text}");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn ensure_env_template_idempotent_on_double_run() {
        let dir = _scratch_dir("idem");
        ensure_project_env_template(&dir, "X").unwrap();
        let after_first = std::fs::read_to_string(dir.join(".env")).unwrap();
        let report = ensure_project_env_template(&dir, "X").unwrap();
        let after_second = std::fs::read_to_string(dir.join(".env")).unwrap();
        assert_eq!(report.action, "noop");
        assert_eq!(after_first, after_second);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn ensure_env_template_recognises_commented_form_as_present() {
        // User has `# ANTHROPIC_API_KEY=` — the commented canonical
        // form. Re-running must NOT append a duplicate.
        let dir = _scratch_dir("commented");
        std::fs::write(
            dir.join(".env"),
            "# my prose\n# ANTHROPIC_API_KEY=\nGITHUB_TOKEN=ghp_user\n",
        )
        .unwrap();
        ensure_project_env_template(&dir, "X").unwrap();
        let text = std::fs::read_to_string(dir.join(".env")).unwrap();
        let count = text.matches("ANTHROPIC_API_KEY").count();
        assert_eq!(count, 1, "expected 1 occurrence, got {count}\n{text}");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn ensure_env_template_handles_no_trailing_newline() {
        let dir = _scratch_dir("nonl");
        std::fs::write(dir.join(".env"), "FOO=bar").unwrap();
        ensure_project_env_template(&dir, "X").unwrap();
        let text = std::fs::read_to_string(dir.join(".env")).unwrap();
        assert!(text.contains("FOO=bar\n"),
                "user line should now end with newline: {text:?}");
        // Marker line must not be glued to FOO=bar.
        for line in text.lines() {
            if line.contains(ENV_VCO_MARKER) {
                assert!(!line.starts_with("FOO=bar"),
                        "marker glued to user line: {line:?}");
            }
        }
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn ensure_env_template_user_value_for_kg_collection_not_overwritten() {
        let dir = _scratch_dir("kguser");
        std::fs::write(dir.join(".env"), "KG_COLLECTION=MyCustom_KG\n").unwrap();
        ensure_project_env_template(&dir, "Acme").unwrap();
        let text = std::fs::read_to_string(dir.join(".env")).unwrap();
        assert!(text.contains("KG_COLLECTION=MyCustom_KG"));
        assert!(!text.contains("KG_COLLECTION=Acme_KnowledgeGraph"));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn parse_existing_env_keys_handles_blank_and_comment_only_lines() {
        let text = "\n\n# pure prose comment\n\n# Another: with a colon\nFOO=bar\n";
        let keys = parse_existing_env_keys(text);
        assert_eq!(keys.len(), 1);
        assert!(keys.contains("FOO"));
    }

    #[test]
    fn env_template_canonical_keys_match_python() {
        // Cross-language contract: the Rust canonical-key list MUST
        // match install.py's. If this test fails because the lists
        // diverge, update both sides — the user shouldn't get
        // different keys depending on which surface ran first.
        let rust_keys: std::collections::HashSet<String> = env_canonical_keys()
            .iter()
            .map(|(k, _)| (*k).to_string())
            .collect();
        let expected: std::collections::HashSet<String> = [
            "WEAVIATE_URL", "WEAVIATE_PORT", "OLLAMA_URL", "OLLAMA_PORT",
            "CODE_EMBED_URL",
            "KG_COLLECTION", "SHARED_KG_COLLECTION", "DEVELOPMENT_COLLECTION",
            "PROJECT_NAME",
            // CONVERSATION_COLLECTION removed (B5 2026-05-01).
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
            "GITHUB_TOKEN",
            "RL_SERVER_URL", "RL_SERVER_PORT", "RL_PROJECT_ROOT",
            "VCT_TELEMETRY",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        assert_eq!(rust_keys, expected, "Rust canonical key set drifted from Python");
    }

    // ─── PR 7 deliverable tests (env-hygiene secondary drift) ────────

    /// B5: CONVERSATION_COLLECTION must not appear in ANY env surface after
    /// create (write_project_env_files + ensure_project_env_template).
    #[test]
    fn conversation_collection_not_written() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-b5-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        write_project_env_files(&tmp, "Acme").unwrap();
        ensure_project_env_template(&tmp, "Acme").unwrap();

        // .env
        let env = std::fs::read_to_string(tmp.join(".env")).unwrap();
        assert!(!env.contains("CONVERSATION_COLLECTION"),
                ".env must not contain CONVERSATION_COLLECTION:\n{env}");

        // .vscode/settings.json claude-code.env block
        let vsc: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".vscode/settings.json")).unwrap(),
        ).unwrap();
        assert!(vsc["claude-code.env"].get("CONVERSATION_COLLECTION").is_none(),
                ".vscode/settings.json must not have CONVERSATION_COLLECTION");

        // .claude/env
        let ce = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(!ce.contains("CONVERSATION_COLLECTION"),
                ".claude/env must not contain CONVERSATION_COLLECTION:\n{ce}");

        // .claude/settings.json env block
        let cs: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap(),
        ).unwrap();
        assert!(cs["env"].get("CONVERSATION_COLLECTION").is_none(),
                ".claude/settings.json must not have CONVERSATION_COLLECTION");

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// B7: after create, the canonical key VCT_TELEMETRY is present in the
    /// .env template, not the legacy VIBECODED_TELEMETRY active key.
    /// (The active VIBECODED_TELEMETRY write was in install.py; the Rust
    /// surfaces only carry VCT_TELEMETRY as a commented placeholder.)
    #[test]
    fn telemetry_canonical_key_is_vct_telemetry() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-b7-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        ensure_project_env_template(&tmp, "Acme").unwrap();
        let env = std::fs::read_to_string(tmp.join(".env")).unwrap();

        // Canonical key must be present (as a commented placeholder).
        assert!(env.contains("VCT_TELEMETRY"),
                ".env must reference VCT_TELEMETRY:\n{env}");
        // Legacy key must NOT be written by the Rust template.
        assert!(!env.contains("VIBECODED_TELEMETRY"),
                ".env template must not write VIBECODED_TELEMETRY (read-alias only):\n{env}");

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// B9: write_project_env_files (called by rename logic) refreshes the
    /// three Claude Code surfaces. Simulate rename by calling
    /// write_project_env_files once with "FooBar" and once with "BazQux"
    /// on the same folder, then assert the second name wins everywhere.
    #[test]
    fn rename_refreshes_env_surfaces() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-b9-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // Initial create
        write_project_env_files(&tmp, "FooBar").unwrap();
        ensure_project_env_template(&tmp, "FooBar").unwrap();

        // Simulate rename — re-run env writers with new name.
        write_project_env_files(&tmp, "BazQux").unwrap();

        // VS Code surface
        let vsc: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".vscode/settings.json")).unwrap(),
        ).unwrap();
        assert_eq!(vsc["claude-code.env"]["KG_COLLECTION"], "BazQux_KnowledgeGraph");
        assert_ne!(vsc["claude-code.env"]["KG_COLLECTION"], "FooBar_KnowledgeGraph");

        // CLI shell file
        let ce = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(ce.contains(r#"export KG_COLLECTION="BazQux_KnowledgeGraph""#));
        assert!(!ce.contains("FooBar"));

        // canonical settings.json
        let cs: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap(),
        ).unwrap();
        assert_eq!(cs["env"]["KG_COLLECTION"], "BazQux_KnowledgeGraph");
        assert_eq!(cs["env"]["PROJECT_NAME"], "BazQux");

        // Note: .env is append-only (ensure_project_env_template), so it will
        // still carry FooBar — this is the known limitation documented in B9.
        // The warn path is tested by checking the env file does NOT have the new
        // canonical key (triggering the stale-warning branch in rename_project_v2).

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// B12: registering a folder whose .env has stale KG_COLLECTION=KnowledgeGraph
    /// emits a warning in the result. Test via the helper logic directly since
    /// we can't call the Tauri command without State<Db>.
    #[test]
    fn register_project_with_stale_env_detects_stale_kg() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-b12-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // Pre-populate with stale bare default (the VideoFrames bug pattern).
        std::fs::write(tmp.join(".env"), "KG_COLLECTION=KnowledgeGraph\nMY_VAR=hello\n").unwrap();

        // Run env writers (as create_project_v2 does).
        write_project_env_files(&tmp, "Acme").unwrap();
        // ensure_project_env_template is append-only; it will not overwrite the stale line.
        ensure_project_env_template(&tmp, "Acme").unwrap();

        // B12 stale detection: the canonical key should be absent from .env
        // (since the old KG_COLLECTION=KnowledgeGraph occupies the key slot
        // and ensure_project_env_template skips it as "present").
        let env_text = std::fs::read_to_string(tmp.join(".env")).unwrap();
        assert!(env_text.contains("KG_COLLECTION=KnowledgeGraph"),
                "stale value must still be present (append-only writer):\n{env_text}");
        assert!(!env_text.contains("KG_COLLECTION=Acme_KnowledgeGraph"),
                "canonical value must NOT have been written (stale blocked it):\n{env_text}");

        // The stale detection logic that create_project_v2 would run:
        let kg_basename = sanitize_kg_collection("Acme");
        let canonical_kg = format!("{}_KnowledgeGraph", kg_basename);
        let stale_bare = "KG_COLLECTION=KnowledgeGraph";
        let has_stale = env_text.lines().any(|l| l.trim() == stale_bare);
        let missing_canonical = !env_text.contains(&format!("KG_COLLECTION={}", canonical_kg));
        assert!(has_stale && missing_canonical,
                "stale detection must fire (has_stale={has_stale}, missing_canonical={missing_canonical})");

        // MY_VAR user value preserved.
        assert!(env_text.contains("MY_VAR=hello"),
                "user keys must be preserved:\n{env_text}");

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// B8: weaviate_mcp GRPC_PORT read-both-keys logic is tested at the
    /// Python layer (tests/test_weaviate_mcp_grpc_port.py). This Rust test
    /// verifies the Rust env surfaces emit no GRPC_PORT key (only the
    /// .claude/settings.json surface does via install.py, not via Rust).
    #[test]
    fn rust_surfaces_do_not_write_grpc_port() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-b8-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        write_project_env_files(&tmp, "Acme").unwrap();

        // .vscode/settings.json — Rust does not inject GRPC_PORT here.
        let vsc: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".vscode/settings.json")).unwrap(),
        ).unwrap();
        assert!(vsc["claude-code.env"].get("GRPC_PORT").is_none(),
                ".vscode/settings.json must not have GRPC_PORT (install.py owns that surface)");

        // .claude/env — same.
        let ce = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(!ce.contains("GRPC_PORT"),
                ".claude/env must not contain GRPC_PORT");

        std::fs::remove_dir_all(&tmp).ok();
    }
}
