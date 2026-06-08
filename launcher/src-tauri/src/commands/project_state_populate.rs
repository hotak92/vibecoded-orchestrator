//! Populate the per-project state DB tables from a freshly-onboarded
//! project's `.claude/` directory.
//!
//! Called from `create_project_v2` after the `projects` row is inserted.
//! Without this, the launcher's per-project tabs (Agents, Skills, Hooks,
//! KG Bindings, Codegraph Bindings) appear empty even when the filesystem
//! has 26+ agents and a full bundled `.claude/` tree.
//!
//! Idempotence: every insert routes through the existing `register_*`
//! upsert helpers in `crate::db::project_state`, which leave the `enabled`
//! column untouched on conflict. Re-running this function therefore
//! preserves user toggles (a row a user disabled in the GUI stays
//! disabled across re-onboarding).
//!
//! Errors are tolerated per-row: a single bad frontmatter or unreadable
//! file logs a warning and we continue. Project creation must NEVER fail
//! over a populate hiccup.
//!
//! Out of scope here (tracked separately, not yet investigated):
//!   - The "Open project" button on the launcher's own self-tile.
//!   - The RL reranker's misleading "Open" button.
//!
//! KNOWN_ISSUES.md (v0.2.x) entry resolved 2026-05-10:
//!   "Custom MCP tab is not populated by initial project registration —
//!    `project_state_populate` mirrors `.claude/settings.json::mcpServers`
//!    into the launcher's per-project DB on `create_project_v2`, but
//!    doesn't flag user-added entries (anything beyond bundled
//!    `weaviate-kg` / `ollama` / `search` / `code-embedding` /
//!    `playwright`) as `is_user_added=true`."
//!
//! `populate_mcp_servers` (added in this file 2026-05-10) reads
//! `<folder>/.claude/settings.json::mcpServers` AND `<folder>/.mcp.json`
//! (Anthropic project-scoped MCP config), inserts one row per entry into
//! `project_mcp_servers`, and computes `is_user_added` from
//! `BUNDLED_MCP_NAMES` (see `crate::db::project_mcp_servers`). Idempotent
//! UPSERT preserves the `enabled` toggle.

use std::path::Path;

use serde_json::Value as JsonValue;

use crate::commands::project_env_settings::LAST_RESORT_SHARED_KG_COLLECTION;
use crate::commands::projects_v2::sanitize_kg_collection;
use crate::db::project_mcp_servers::{is_bundled_mcp, is_default_disabled_mcp};
use crate::db::project_state::{resolve_kind_paths, AgentOrSkill};
use crate::db::Db;

/// Result summary for diagnostic logging. Not exposed to the frontend.
#[derive(Debug, Default, Clone)]
pub struct PopulateReport {
    pub agents_inserted: usize,
    pub skills_inserted: usize,
    pub hooks_inserted: usize,
    pub kg_bindings_inserted: usize,
    pub codegraph_bindings_inserted: usize,
    /// PR-3 Commit 2 (2026-05-06): default kg_collection_access rows
    /// written for the new project (own primary + own dev + shared
    /// read-only). Pre-PR-3 the access matrix was permanently empty for
    /// fresh projects, which made every collection read fail with
    /// `project X has no read access to collection Y` until the user
    /// manually granted via the GUI access matrix.
    pub kg_access_rows_inserted: usize,
    /// Migration 010 (2026-05-10): MCP server rows seeded into
    /// `project_mcp_servers` from `.claude/settings.json::mcpServers` +
    /// `.mcp.json`. Counts both bundled and user-added entries; the
    /// is_user_added flag is computed from the bundled-name allowlist
    /// in `crate::db::project_mcp_servers::BUNDLED_MCP_NAMES`.
    pub mcp_servers_inserted: usize,
    /// Soft errors, one entry per row that could not be inserted. The
    /// caller logs these but does NOT fail the project creation.
    pub warnings: Vec<String>,
}

/// Scan `<folder_path>/.claude/` and insert per-project state rows.
///
/// Idempotent: safe to call multiple times for the same project. The
/// underlying `register_*` upserts leave `enabled` untouched on conflict
/// so user toggles survive a re-run.
pub fn populate_project_state_from_filesystem(
    project_id: &str,
    project_name: &str,
    folder_path: &Path,
    db: &Db,
) -> PopulateReport {
    let mut report = PopulateReport::default();

    let claude_dir = folder_path.join(".claude");
    if !claude_dir.is_dir() {
        // Brand-new project with no `.claude/` yet (e.g. user pointed at
        // an empty folder and let the orchestrator install populate it
        // post-create). Still write KG/codegraph bindings — those don't
        // depend on the filesystem at all.
        populate_kg_bindings(project_id, project_name, db, &mut report);
        populate_codegraph_binding(project_id, project_name, db, &mut report);
        populate_kg_collection_access(project_id, project_name, db, &mut report);
        // MCP servers can also live in `<folder>/.mcp.json` (Anthropic's
        // project-scoped MCP config), which exists outside `.claude/`.
        // Run the populator so a project with `.mcp.json` but no
        // `.claude/` directory still surfaces user-added MCPs.
        populate_mcp_servers(project_id, folder_path, db, &mut report);
        return report;
    }

    populate_agents(project_id, folder_path, &claude_dir, db, &mut report);
    populate_skills(project_id, folder_path, &claude_dir, db, &mut report);
    populate_hooks(project_id, &claude_dir, db, &mut report);
    populate_kg_bindings(project_id, project_name, db, &mut report);
    populate_codegraph_binding(project_id, project_name, db, &mut report);
    populate_kg_collection_access(project_id, project_name, db, &mut report);
    populate_mcp_servers(project_id, folder_path, db, &mut report);

    report
}

// ─── KG collection access defaults (PR-3 Commit 2, 2026-05-06) ────────
//
// Why default-grant: the read gate `require_kg_read` (commands/kg.rs +
// hub/cli_api.rs) rejects every collection access the project doesn't
// have an explicit row for. Pre-PR-3 the access matrix was permanently
// empty for fresh projects, so every search/read of the project's own
// KG (or the shared bundled KG) failed with "project X has no read
// access to collection Y". The user had to manually grant via the GUI
// access matrix before the launcher's own UI worked.
//
// Default-deny is the right posture for cross-project access (other
// projects' KGs / codegraphs); but for a project's OWN KG and the
// machine-shared bundled KG, default-grant matches user expectation.
//
// Idempotent: a pre-existing row at (project_id, collection_name)
// preserves the user-set level, so a re-onboarding flow doesn't
// silently undo a level the user changed.

fn populate_kg_collection_access(
    project_id: &str,
    project_name: &str,
    db: &Db,
    report: &mut PopulateReport,
) {
    // v0.2.49 access-matrix Phase 4 (item #10): delegate to the
    // centralized core helper so this surface and the hub's
    // `vct project create` path stay in lock-step. The core helper:
    //   - sanitizes `project_name` (own KG/dev names)
    //   - resolves the canonical shared KG name from `app_state`
    //     (Phase 1 single-source-of-truth)
    //   - writes three rows via `kg_seed_access` (INSERT OR IGNORE):
    //     own primary → "write", own dev → "write", shared → "read"
    //   - returns the count of rows actually inserted (user-configured
    //     rows are preserved; INSERT OR IGNORE is the idempotent
    //     primitive replacing the prior get-then-set check).
    //
    // The launcher-side wrapper still owns:
    //   - `PopulateReport.kg_access_rows_inserted` accounting (for the
    //     `create_project_v2` summary log).
    //   - Warning emission on SQL errors (preserves prior soft-fail
    //     contract — project creation never fails over an access-matrix
    //     hiccup).
    match db.populate_kg_collection_access_for_project(project_id, project_name) {
        Ok(n) => report.kg_access_rows_inserted += n,
        Err(e) => {
            report.warnings.push(format!(
                "populate_kg_collection_access_for_project({}, {}): {}",
                project_id, project_name, e
            ));
        }
    }
}

/// v0.2.49 item #13 (M-3): populate KG access rows for a global-scope
/// module's declared KG collections across ALL projects.
///
/// Called by `commands/modules.rs::install_module` (Stream A's `is_global`
/// branch) when `manifest.kg_collections.is_some()`. For each declared
/// collection, iterates `db.list_projects()` and seeds the resolver's
/// default access level (`db::access::resolve_default_access_level`).
///
/// Idempotency / user-preservation: uses `db.kg_seed_access` (INSERT OR
/// IGNORE) so a row already present from a prior install is preserved
/// untouched. User-configured downgrades (`is_user_configured()` TRUE)
/// survive re-runs of the global install.
///
/// Per-project modules do NOT use this path — their access matrix is
/// seeded by the per-project `populate_kg_collection_access` helper at
/// project-create time.
pub fn populate_kg_collection_access_for_global_module(
    collections: &[String],
    db: &Db,
    report: &mut PopulateReport,
) {
    if collections.is_empty() {
        return;
    }
    let projects = match db.list_projects() {
        Ok(rows) => rows,
        Err(e) => {
            report.warnings.push(format!("list_projects: {}", e));
            return;
        }
    };
    for collection in collections {
        for project in &projects {
            let level = match db.resolve_default_access_level(&project.id, collection) {
                Ok(l) => l,
                Err(e) => {
                    report.warnings.push(format!(
                        "resolve_default_access_level({}, {}): {}",
                        project.id, collection, e
                    ));
                    continue;
                }
            };
            match db.kg_seed_access(&project.id, collection, level.as_str()) {
                Ok(1) => report.kg_access_rows_inserted += 1,
                Ok(0) => {} // row exists; preserved
                Ok(other) => report.warnings.push(format!(
                    "kg_seed_access({}, {}) returned unexpected count: {}",
                    project.id, collection, other
                )),
                Err(e) => report.warnings.push(format!(
                    "kg_seed_access({}, {}): {}",
                    project.id, collection, e
                )),
            }
        }
    }
}

// ─── Agents ────────────────────────────────────────────────────────────

/// File-stem names (lowercased, no extension) that are NEVER agents and
/// must be skipped by the disk-walker. README.md is the prime offender:
/// it lives in `.claude/agents/` to document the directory itself, has
/// no frontmatter, and was being registered as a phantom agent named
/// "README" with model=None — visible in the launcher GUI as an
/// Unregister-able row that the user never created (v0.2.22 item #18).
///
/// The list is intentionally short. Anything else with a `.md` extension
/// in `.claude/agents/` is treated as an agent. If a project adds a real
/// agent called `readme-bot.md` the file stem is `readme-bot`, which is
/// NOT in this set — it would still be registered correctly.
const AGENT_FILE_STEM_BLOCKLIST: &[&str] = &["readme", "index", "template"];

/// True iff this `.md` file should be skipped by `populate_agents`. The
/// check is case-insensitive on the stem (Windows / case-insensitive
/// filesystems may produce `README.MD`, `Readme.md`, etc.).
fn is_blocklisted_agent_file(path: &Path) -> bool {
    let stem = match path.file_stem().and_then(|s| s.to_str()) {
        Some(s) => s.to_lowercase(),
        None => return false,
    };
    AGENT_FILE_STEM_BLOCKLIST.iter().any(|b| *b == stem)
}

fn populate_agents(
    project_id: &str,
    folder_path: &Path,
    claude_dir: &Path,
    db: &Db,
    report: &mut PopulateReport,
) {
    let agents_dir = claude_dir.join("agents");
    let entries = match std::fs::read_dir(&agents_dir) {
        Ok(it) => it,
        Err(_) => {
            // Missing dir is fine — bundled installs always have it but
            // a custom project might not.
            return;
        }
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|s| s.to_str()) != Some("md") {
            continue;
        }
        // Don't-resurrect-a-disabled-file guard (FS-disable plan,
        // Subagent D, Wave 2). If an agent file exists in BOTH
        // `.claude/agents/<name>.md` AND `.claude/agents.disabled/<name>.md`
        // simultaneously (a corrupt-state fluke or a partial migration),
        // skip registration. The user's explicit disable choice
        // (recorded by the presence of the `.disabled/` companion) wins.
        // Path math is delegated to `resolve_kind_paths` so this stays
        // consistent with `set_project_agent_enabled` and the
        // one-time migration in `vct-launcher-core::db::project_state`.
        if let Some(stem) = path.file_stem().and_then(|s| s.to_str()) {
            let (_enabled, disabled) =
                resolve_kind_paths(folder_path, stem, AgentOrSkill::Agent);
            if disabled.exists() {
                report.warnings.push(format!(
                    "agent {} present in both agents/ and agents.disabled/ \
                     — skipping registration. Run the launcher's FS-disable \
                     migration or manually remove one copy.",
                    stem
                ));
                continue;
            }
        }
        // Skip documentation files that live in `.claude/agents/` but
        // are not agents themselves (README.md, index.md, template.md).
        // ALSO defensively delete any pre-existing DB row for them so
        // the GUI's phantom "README" row clears on the next populate
        // run without requiring the user to click Unregister.
        // v0.2.22 item #18.
        if is_blocklisted_agent_file(&path) {
            let stem = path
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("")
                .to_string();
            // Try both original case and the canonical lowercase name
            // — the populate scanner historically registered the agent
            // under the EXACT file-stem case (e.g. "README" for README.md),
            // so we must delete using the same casing it was inserted with.
            // Unregister is idempotent: missing row = 0 rows affected.
            let _ = db.unregister_project_agent(project_id, &stem);
            let _ = db.unregister_project_agent(project_id, &stem.to_lowercase());
            let _ = db.unregister_project_agent(project_id, &stem.to_uppercase());
            continue;
        }
        let raw = match std::fs::read_to_string(&path) {
            Ok(s) => s,
            Err(e) => {
                report
                    .warnings
                    .push(format!("read {}: {}", path.display(), e));
                continue;
            }
        };
        let fm = parse_frontmatter(&raw);
        // Prefer `name:` from frontmatter, fall back to file stem.
        let agent_name = fm
            .get("name")
            .cloned()
            .unwrap_or_else(|| {
                path.file_stem()
                    .and_then(|s| s.to_str())
                    .unwrap_or("unknown")
                    .to_string()
            });
        let model = fm.get("model").cloned();
        let description = fm.get("description").cloned().unwrap_or_default();
        let config = serde_json::json!({ "description": description });

        // v0.2.22 item #19 defensive surface: bundled-source agents whose
        // frontmatter lacks a `model:` line render as `—` in the GUI's
        // Model column. The bundled .md files SHOULD always carry a
        // model: line — its absence usually indicates the user's project
        // bundle is stale relative to the orchestrator template. Emit a
        // warning so the user (and future-debuggers) can correlate the
        // GUI's `—` with "your bundle is stale; re-propagate from
        // launcher Settings → Update bundle". We do NOT block the
        // registration — `—` is a valid render and the agent still works
        // (Claude Code reads model from the frontmatter directly, so
        // missing model just means Claude Code falls back to its own
        // session default).
        if model.is_none() {
            report.warnings.push(format!(
                "bundled agent {} has no `model:` frontmatter at {} \
                 — GUI will render Model column as `—`. If unexpected, \
                 re-propagate the bundle via launcher Settings → Update bundle.",
                agent_name,
                path.display()
            ));
        }

        let path_str = path.to_string_lossy().to_string();
        if let Err(e) = db.register_project_agent(
            project_id,
            &agent_name,
            "bundled",
            None,
            model.as_deref(),
            Some(&path_str),
            &config,
        ) {
            report
                .warnings
                .push(format!("register_project_agent({}): {}", agent_name, e));
        } else {
            report.agents_inserted += 1;
        }
    }
}

// ─── Skills ────────────────────────────────────────────────────────────

fn populate_skills(
    project_id: &str,
    folder_path: &Path,
    claude_dir: &Path,
    db: &Db,
    report: &mut PopulateReport,
) {
    let skills_dir = claude_dir.join("skills");
    let entries = match std::fs::read_dir(&skills_dir) {
        Ok(it) => it,
        Err(_) => return,
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let skill_md = path.join("SKILL.md");
        if !skill_md.is_file() {
            continue;
        }
        // Don't-resurrect-a-disabled-file guard (FS-disable plan,
        // Subagent D, Wave 2). Mirror of the agent guard above:
        // if a skill directory exists in BOTH `.claude/skills/<name>/`
        // AND `.claude/skills.disabled/<name>/`, skip registration.
        // The `.disabled/` companion encodes the user's explicit
        // disable choice and must not be silently undone by a
        // re-populate sweep.
        if let Some(name) = path.file_name().and_then(|s| s.to_str()) {
            let (_enabled, disabled) =
                resolve_kind_paths(folder_path, name, AgentOrSkill::Skill);
            if disabled.exists() {
                report.warnings.push(format!(
                    "skill {} present in both skills/ and skills.disabled/ \
                     — skipping registration. Run the launcher's FS-disable \
                     migration or manually remove one copy.",
                    name
                ));
                continue;
            }
        }
        let raw = match std::fs::read_to_string(&skill_md) {
            Ok(s) => s,
            Err(e) => {
                report
                    .warnings
                    .push(format!("read {}: {}", skill_md.display(), e));
                continue;
            }
        };
        let fm = parse_frontmatter(&raw);
        // Prefer frontmatter `name`; fall back to directory name.
        let skill_name = fm
            .get("name")
            .cloned()
            .unwrap_or_else(|| {
                path.file_name()
                    .and_then(|s| s.to_str())
                    .unwrap_or("unknown")
                    .to_string()
            });
        let model = fm.get("model").cloned();
        let description = fm.get("description").cloned().unwrap_or_default();
        let config = serde_json::json!({ "description": description });

        // v0.2.22 item #19 defensive surface — mirror of the agent path:
        // bundled SKILL.md files without a `model:` frontmatter render as
        // `—` in the GUI. Emit a warning so the user can correlate to a
        // stale bundle. Registration proceeds; missing model is valid.
        if model.is_none() {
            report.warnings.push(format!(
                "bundled skill {} has no `model:` frontmatter at {} \
                 — GUI will render Model column as `—`. If unexpected, \
                 re-propagate the bundle via launcher Settings → Update bundle.",
                skill_name,
                skill_md.display()
            ));
        }

        let path_str = skill_md.to_string_lossy().to_string();
        if let Err(e) = db.register_project_skill(
            project_id,
            &skill_name,
            "bundled",
            None,
            model.as_deref(),
            Some(&path_str),
            &config,
        ) {
            report
                .warnings
                .push(format!("register_project_skill({}): {}", skill_name, e));
        } else {
            report.skills_inserted += 1;
        }
    }
}

// ─── Hooks ─────────────────────────────────────────────────────────────

fn populate_hooks(
    project_id: &str,
    claude_dir: &Path,
    db: &Db,
    report: &mut PopulateReport,
) {
    // Hooks are config-driven, source-of-truth = `.claude/settings.json`.
    let settings_path = claude_dir.join("settings.json");
    let raw = match std::fs::read_to_string(&settings_path) {
        Ok(s) => s,
        Err(_) => {
            // Missing settings.json is fine for very minimal projects; a
            // create-then-install flow writes it during `install.py`. Skip
            // silently rather than warn.
            return;
        }
    };
    let parsed: JsonValue = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(e) => {
            report.warnings.push(format!(
                "{} parse error: {} (skipping hook population)",
                settings_path.display(),
                e
            ));
            return;
        }
    };
    let hooks_root = match parsed.get("hooks").and_then(|v| v.as_object()) {
        Some(o) => o,
        None => return,
    };

    // Schema (Anthropic): hooks: { Event: [ { matcher?, hooks: [ { command,
    // type, timeout?, background?, ... } ] } ] }
    for (event, blocks) in hooks_root {
        let blocks = match blocks.as_array() {
            Some(a) => a,
            None => continue,
        };
        for block in blocks {
            let matcher = block
                .get("matcher")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let hook_arr = match block.get("hooks").and_then(|v| v.as_array()) {
                Some(a) => a,
                None => continue,
            };
            for h in hook_arr {
                let command = match h.get("command").and_then(|v| v.as_str()) {
                    Some(c) => c.to_string(),
                    None => continue,
                };
                // settings.json `timeout` is in SECONDS (Claude Code
                // convention); the DB column is `timeout_ms`.
                let timeout_ms = h
                    .get("timeout")
                    .and_then(|v| v.as_i64())
                    .map(|s| s.saturating_mul(1000));
                // Stash the rest of the hook entry as config so the
                // launcher GUI can show `background`, `type`, etc.
                let cfg = h.clone();
                if let Err(e) = db.register_project_hook(
                    project_id,
                    event,
                    &matcher,
                    &command,
                    "project",
                    None,
                    timeout_ms,
                    &cfg,
                ) {
                    report.warnings.push(format!(
                        "register_project_hook({}/{}): {}",
                        event, matcher, e
                    ));
                } else {
                    report.hooks_inserted += 1;
                }
            }
        }
    }
}

// ─── MCP servers (migration 010, 2026-05-10) ───────────────────────────
//
// Reads two source files and merges them into `project_mcp_servers`:
//   1. `<folder>/.claude/settings.json::mcpServers` — the conventional
//      per-project Claude Code surface. Empty in our default templates;
//      user-added entries land here.
//   2. `<folder>/.mcp.json` — Anthropic's project-scoped MCP config,
//      flat top-level `{ "mcpServers": { ... } }` with the same entry
//      shape. Some users prefer this file because it doesn't share a
//      surface with hooks/permissions/env.
//
// `is_user_added` is the discriminator the Custom MCP tab filters on.
// Computed via `is_bundled_mcp(name)`: true when the name is NOT in the
// orchestrator's bundled allowlist.
//
// Idempotency: `register_project_mcp_server` UPSERTs on
// (project_id, mcp_name) and leaves `enabled` untouched on conflict.
// Re-running this function is safe and preserves user toggles.
//
// What we DO NOT scan: the global `~/.claude.json::mcpServers`. Those
// entries are launcher-owned; the orchestrator DOES register the bundled
// MCPs there for Claude Code to spawn, but they are not project-scoped.
// Mirroring them into every project would inflate every Custom MCP tab
// with the same global rows. The bundled set is seeded explicitly by
// the orchestrator install path (when settings.json carries them) or
// can be backfilled by a separate migration step if needed.

fn populate_mcp_servers(
    project_id: &str,
    folder_path: &Path,
    db: &Db,
    report: &mut PopulateReport,
) {
    // Two source files in priority order. A name appearing in both files
    // wins from `.mcp.json` (last-write semantics — UPSERT replaces the
    // earlier row's config_json). This matches Claude Code's own
    // precedence (project-scoped `.mcp.json` overrides settings.json).
    let candidates: [(std::path::PathBuf, &str); 2] = [
        (
            folder_path.join(".claude").join("settings.json"),
            ".claude/settings.json",
        ),
        (folder_path.join(".mcp.json"), ".mcp.json"),
    ];

    for (path, rel_label) in candidates.iter() {
        if !path.is_file() {
            continue;
        }
        let raw = match std::fs::read_to_string(path) {
            Ok(s) => s,
            Err(e) => {
                report
                    .warnings
                    .push(format!("read {}: {}", path.display(), e));
                continue;
            }
        };
        let parsed: JsonValue = match serde_json::from_str(&raw) {
            Ok(v) => v,
            Err(e) => {
                // settings.json with invalid JSON is already warned about
                // by populate_hooks; avoid double-warning by suppressing
                // here when the path matches. `.mcp.json` parse errors
                // ARE worth reporting though.
                if rel_label != &".claude/settings.json" {
                    report.warnings.push(format!(
                        "{} parse error: {} (skipping mcp population)",
                        path.display(),
                        e
                    ));
                }
                continue;
            }
        };
        let mcp_obj = match parsed.get("mcpServers").and_then(|v| v.as_object()) {
            Some(o) => o,
            None => continue,
        };
        for (name, entry) in mcp_obj {
            // Convenience top-level command lookup. Some MCP entries use
            // `{ "command": "x" }`, others nest under `{ "transport":
            // {"type": "stdio", "command": "x"} }`. We only persist the
            // top-level field here for fast list rendering; the full
            // entry survives in config_json.
            let command = entry.get("command").and_then(|v| v.as_str());
            let user_added = !is_bundled_mcp(name);
            // Source attribution: bundled names get source='bundled'
            // even when populated from a user-edited file, because the
            // discriminator the rest of the codebase reads is
            // `is_user_added`, not `source`. Keep the SQL CHECK happy
            // (allowed values: bundled|user|paid-module|project).
            let source = if user_added { "user" } else { "bundled" };

            // Phase 1.2 (diagrams plan): bundled MCPs marked
            // default-disabled (currently just `mermaid`) get their
            // initial `enabled=false` flag on FRESH inserts. We pre-
            // compute the fresh-insert flag BEFORE the UPSERT — by
            // the time the call returns, the row exists either way.
            // Re-populate (row already exists) MUST preserve the
            // user's enabled toggle — see project_mcp_servers.rs
            // "upsert_preserves_enabled_flag_on_re_register" test.
            let was_fresh_insert = if !user_added && is_default_disabled_mcp(name) {
                match db.project_mcp_server_exists(project_id, name) {
                    Ok(exists) => !exists,
                    Err(_) => false, // err on the side of "not fresh" → don't touch enabled
                }
            } else {
                false
            };

            if let Err(e) = db.register_project_mcp_server(
                project_id,
                name,
                user_added,
                source,
                None,
                Some(rel_label),
                command,
                entry,
            ) {
                report.warnings.push(format!(
                    "register_project_mcp_server({}/{}): {}",
                    rel_label, name, e
                ));
            } else {
                report.mcp_servers_inserted += 1;
                // Default-disabled on first insert. We do this AFTER the
                // upsert so SQL-side INSERT defaults stay simple
                // (enabled=1 unconditionally on INSERT, then we toggle).
                if was_fresh_insert {
                    if let Err(e) = db.set_project_mcp_server_enabled(project_id, name, false) {
                        report.warnings.push(format!(
                            "set_project_mcp_server_enabled({}/{}, false): {}",
                            rel_label, name, e
                        ));
                    }
                }
            }
        }
    }
}

// ─── KG bindings ───────────────────────────────────────────────────────

fn populate_kg_bindings(
    project_id: &str,
    project_name: &str,
    db: &Db,
    report: &mut PopulateReport,
) {
    let pascal = sanitize_kg_collection(project_name);
    let primary_collection = format!("{}_KnowledgeGraph", pascal);
    // Single source of truth — see project_env_settings.rs. Renamed from
    // "VibeCodedTools_KnowledgeGraph" in v0.2.12 PR-26 / Group E.
    let shared_collection = LAST_RESORT_SHARED_KG_COLLECTION;
    let weaviate_url = "http://localhost:8081";
    let embedding_model = "qwen3-embedding:0.6b";
    let embedding_dim: i64 = 1024;

    // Idempotence: set_project_kg_binding upserts ON CONFLICT(project_id,
    // role). User edits to collection_name etc. WILL be overwritten by
    // a re-run — which is the intended contract for these defaults
    // (they're orchestrator-managed, not user-editable through this
    // path; the launcher GUI edits them via set_project_kg_binding
    // directly).
    if !kg_binding_already_exists(db, project_id, "primary") {
        if let Err(e) = db.set_project_kg_binding(
            project_id,
            "primary",
            &primary_collection,
            Some(embedding_model),
            Some(embedding_dim),
            None,
            Some(weaviate_url),
            &JsonValue::Null,
        ) {
            report
                .warnings
                .push(format!("set_project_kg_binding(primary): {}", e));
        } else {
            report.kg_bindings_inserted += 1;
        }
    }
    if !kg_binding_already_exists(db, project_id, "shared") {
        if let Err(e) = db.set_project_kg_binding(
            project_id,
            "shared",
            shared_collection,
            Some(embedding_model),
            Some(embedding_dim),
            None,
            Some(weaviate_url),
            &JsonValue::Null,
        ) {
            report
                .warnings
                .push(format!("set_project_kg_binding(shared): {}", e));
        } else {
            report.kg_bindings_inserted += 1;
        }
    }
}

fn kg_binding_already_exists(db: &Db, project_id: &str, role: &str) -> bool {
    db.list_project_kg_bindings(project_id)
        .map(|rows| rows.iter().any(|r| r.role == role))
        .unwrap_or(false)
}

// ─── Codegraph binding ─────────────────────────────────────────────────

fn populate_codegraph_binding(
    project_id: &str,
    project_name: &str,
    db: &Db,
    report: &mut PopulateReport,
) {
    // Idempotence: set_project_codegraph_binding upserts ON
    // CONFLICT(project_id) and would clobber the `enabled` flag. Pre-check
    // existence — preserving the toggle is the documented user-facing
    // contract.
    if db
        .get_project_codegraph_binding(project_id)
        .ok()
        .flatten()
        .is_some()
    {
        return;
    }
    let prefix = sanitize_kg_collection(project_name);
    if let Err(e) = db.set_project_codegraph_binding(
        project_id,
        &prefix,
        Some("codesage-large-v2"),
        Some(2048),
        None,
        None,
        true,
        &JsonValue::Null,
    ) {
        report
            .warnings
            .push(format!("set_project_codegraph_binding: {}", e));
    } else {
        report.codegraph_bindings_inserted += 1;
    }
}

// ─── Frontmatter parsing ───────────────────────────────────────────────

/// Minimal YAML-frontmatter scraper for the keys we care about
/// (`name`, `description`, `model`). Avoids pulling in `serde_yaml` for
/// what is a 5-key flat map. Handles:
///   - `key: value`
///   - `key: "quoted value"`
///   - `key: 'quoted value'`
/// Skips nested mappings and lists (we don't need them).
fn parse_frontmatter(content: &str) -> std::collections::HashMap<String, String> {
    let mut out = std::collections::HashMap::new();
    let trimmed = content.trim_start();
    if !trimmed.starts_with("---") {
        return out;
    }
    // Skip first `---` marker line.
    let after_first = match trimmed.split_once('\n') {
        Some((_, rest)) => rest,
        None => return out,
    };
    // Find closing `---` on its own line.
    let end_idx = match find_closing_marker(after_first) {
        Some(i) => i,
        None => return out,
    };
    let block = &after_first[..end_idx];

    for raw_line in block.lines() {
        let line = raw_line.trim_end();
        // Skip nested-mapping lines (start with whitespace) and list items.
        if line.is_empty() || line.starts_with(' ') || line.starts_with('\t')
            || line.starts_with('-')
        {
            continue;
        }
        if let Some((k, v)) = line.split_once(':') {
            let key = k.trim();
            // Skip keys we don't use to keep the map small.
            if !matches!(key, "name" | "description" | "model") {
                continue;
            }
            let val = v.trim();
            // Strip optional matching quotes.
            let unquoted = strip_matching_quotes(val);
            out.insert(key.to_string(), unquoted.to_string());
        }
    }
    out
}

fn find_closing_marker(s: &str) -> Option<usize> {
    let mut idx = 0usize;
    for line in s.split_inclusive('\n') {
        let stripped = line.trim_end_matches('\n').trim_end_matches('\r');
        if stripped.trim() == "---" {
            return Some(idx);
        }
        idx += line.len();
    }
    None
}

fn strip_matching_quotes(s: &str) -> &str {
    let bytes = s.as_bytes();
    if bytes.len() >= 2
        && ((bytes[0] == b'"' && bytes[bytes.len() - 1] == b'"')
            || (bytes[0] == b'\'' && bytes[bytes.len() - 1] == b'\''))
    {
        &s[1..s.len() - 1]
    } else {
        s
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Tests
// ═══════════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;

    fn make_db_with_project(project_id: &str, name: &str) -> Db {
        let db = Db::open_in_memory().expect("in-memory db");
        let slug = db.generate_unique_slug(name).unwrap();
        // Platform-aware placeholder folder path. Stored only as a string
        // in the projects table; never touches disk.
        let folder = if cfg!(windows) { r"C:\tmp\x" } else { "/tmp/x" };
        db.insert_project(project_id, name, folder, ProjectHost::Base, &slug)
            .unwrap();
        db
    }

    fn write_agent_file(dir: &Path, file_name: &str, frontmatter: &str, body: &str) {
        let p = dir.join(file_name);
        let content = format!("---\n{}\n---\n{}\n", frontmatter, body);
        std::fs::write(p, content).unwrap();
    }

    fn write_skill_dir(skills_dir: &Path, name: &str, frontmatter: &str) {
        let d = skills_dir.join(name);
        std::fs::create_dir_all(&d).unwrap();
        let content = format!("---\n{}\n---\n# {}\n", frontmatter, name);
        std::fs::write(d.join("SKILL.md"), content).unwrap();
    }

    fn scratch_dir(tag: &str) -> std::path::PathBuf {
        let p = std::env::temp_dir().join(format!(
            "vct-populate-{}-{}",
            tag,
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    // ─── frontmatter ────────────────────────────────────────────────

    #[test]
    fn frontmatter_parses_canonical_keys() {
        let raw = "---\nname: coder\ndescription: writes code\nmodel: sonnet\ntools: Read, Write\n---\n# body\n";
        let fm = parse_frontmatter(raw);
        assert_eq!(fm.get("name").map(String::as_str), Some("coder"));
        assert_eq!(fm.get("description").map(String::as_str), Some("writes code"));
        assert_eq!(fm.get("model").map(String::as_str), Some("sonnet"));
        // tools is filtered out (not a recognised key).
        assert!(!fm.contains_key("tools"));
    }

    #[test]
    fn frontmatter_handles_quoted_values() {
        let raw = "---\nname: \"my agent\"\ndescription: 'with apostrophes'\n---\n";
        let fm = parse_frontmatter(raw);
        assert_eq!(fm.get("name").map(String::as_str), Some("my agent"));
        assert_eq!(
            fm.get("description").map(String::as_str),
            Some("with apostrophes")
        );
    }

    #[test]
    fn frontmatter_skips_nested_mappings() {
        // Mirrors a real-world agent file with mcpServers nested config.
        let raw = "---\nname: coder\nmodel: sonnet\nmcpServers:\n  orchestrator-tools:\n    command: /bin/python\n---\n";
        let fm = parse_frontmatter(raw);
        assert_eq!(fm.get("name").map(String::as_str), Some("coder"));
        assert_eq!(fm.get("model").map(String::as_str), Some("sonnet"));
        // mcpServers is filtered + nested keys are skipped (start with whitespace).
        assert!(!fm.contains_key("mcpServers"));
        assert!(!fm.contains_key("command"));
    }

    #[test]
    fn frontmatter_no_block_returns_empty() {
        let fm = parse_frontmatter("# just a heading\n");
        assert!(fm.is_empty());
    }

    // ─── populate_agents ────────────────────────────────────────────

    #[test]
    fn populate_agents_inserts_one_row_per_md() {
        let folder = scratch_dir("agents-basic");
        let claude = folder.join(".claude");
        let agents_dir = claude.join("agents");
        std::fs::create_dir_all(&agents_dir).unwrap();
        write_agent_file(
            &agents_dir,
            "coder.md",
            "name: coder\ndescription: writes code\nmodel: sonnet",
            "# Coder",
        );
        write_agent_file(
            &agents_dir,
            "tester.md",
            "name: tester\ndescription: writes tests\nmodel: haiku",
            "# Tester",
        );
        // Non-md file should be ignored.
        std::fs::write(agents_dir.join("README"), "ignore me").unwrap();

        let db = make_db_with_project("p1", "P One");
        let report =
            populate_project_state_from_filesystem("p1", "P One", &folder, &db);

        assert_eq!(report.agents_inserted, 2);
        let rows = db.list_project_agents("p1").unwrap();
        assert_eq!(rows.len(), 2);
        let names: Vec<&str> = rows.iter().map(|a| a.agent_name.as_str()).collect();
        assert!(names.contains(&"coder"));
        assert!(names.contains(&"tester"));
        let coder = rows.iter().find(|a| a.agent_name == "coder").unwrap();
        assert_eq!(coder.model.as_deref(), Some("sonnet"));
        assert_eq!(coder.source, "bundled");
        assert!(coder.enabled);
        assert!(coder
            .file_path
            .as_deref()
            .unwrap_or("")
            .ends_with("coder.md"));

        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn populate_agents_falls_back_to_filename_when_no_frontmatter_name() {
        let folder = scratch_dir("agents-noname");
        let agents_dir = folder.join(".claude/agents");
        std::fs::create_dir_all(&agents_dir).unwrap();
        // No frontmatter at all.
        std::fs::write(agents_dir.join("planner.md"), "# Planner agent\n").unwrap();

        let db = make_db_with_project("p1", "P");
        let report =
            populate_project_state_from_filesystem("p1", "P", &folder, &db);
        assert_eq!(report.agents_inserted, 1);
        let rows = db.list_project_agents("p1").unwrap();
        assert_eq!(rows[0].agent_name, "planner");

        std::fs::remove_dir_all(&folder).ok();
    }

    // ─── v0.2.22 item #18: README/index/template .md files must not register as agents ──

    /// `populate_agents` must skip `.claude/agents/README.md` — the file
    /// documents the directory, has no frontmatter, and was historically
    /// registered as a phantom agent named "README" with model=None.
    /// Regression guard: confirms the scanner walks past README and
    /// returns the same row count as if README didn't exist.
    #[test]
    fn populate_agents_skips_readme_md() {
        let folder = scratch_dir("agents-readme-skip");
        let agents_dir = folder.join(".claude/agents");
        std::fs::create_dir_all(&agents_dir).unwrap();
        write_agent_file(
            &agents_dir,
            "coder.md",
            "name: coder\nmodel: sonnet",
            "# Coder",
        );
        // README.md sibling — documents the directory, NOT an agent.
        std::fs::write(
            agents_dir.join("README.md"),
            "# Agents\n\nThis directory contains agent definitions.\n",
        )
        .unwrap();

        let db = make_db_with_project("p1", "P");
        let report = populate_project_state_from_filesystem("p1", "P", &folder, &db);

        // Only `coder` is registered, NOT README.
        assert_eq!(
            report.agents_inserted, 1,
            "README.md must not count as an inserted agent"
        );
        let rows = db.list_project_agents("p1").unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].agent_name, "coder");
        assert!(
            !rows.iter().any(|a| a.agent_name.eq_ignore_ascii_case("readme")),
            "README must not appear in the agents list"
        );

        std::fs::remove_dir_all(&folder).ok();
    }

    /// Case-insensitive skip — `Readme.md`, `README.MD`, `readme.md` all
    /// land at the same documentation file on case-insensitive filesystems
    /// (Windows / default macOS HFS+ / APFS). The blocklist matches on the
    /// lowercased stem so every variant is skipped.
    #[test]
    fn populate_agents_skips_readme_case_insensitive() {
        let folder = scratch_dir("agents-readme-case");
        let agents_dir = folder.join(".claude/agents");
        std::fs::create_dir_all(&agents_dir).unwrap();
        write_agent_file(
            &agents_dir,
            "coder.md",
            "name: coder\nmodel: sonnet",
            "# Coder",
        );
        // Some Linux filesystems will keep BOTH if the OS is
        // case-sensitive — write a single file with mixed case that
        // demonstrates the blocklist matches lowercase.
        std::fs::write(agents_dir.join("Readme.md"), "# Docs").unwrap();

        let db = make_db_with_project("p1", "P");
        let report = populate_project_state_from_filesystem("p1", "P", &folder, &db);

        assert_eq!(report.agents_inserted, 1);
        let rows = db.list_project_agents("p1").unwrap();
        assert!(!rows.iter().any(|a| a.agent_name.eq_ignore_ascii_case("readme")));

        std::fs::remove_dir_all(&folder).ok();
    }

    /// Index.md / template.md are also skipped — these are placeholder
    /// files some bundles ship to seed the directory structure but
    /// they're not agents.
    #[test]
    fn populate_agents_skips_index_and_template_md() {
        let folder = scratch_dir("agents-index-skip");
        let agents_dir = folder.join(".claude/agents");
        std::fs::create_dir_all(&agents_dir).unwrap();
        write_agent_file(&agents_dir, "coder.md", "name: coder\nmodel: sonnet", "");
        std::fs::write(agents_dir.join("index.md"), "# index").unwrap();
        std::fs::write(agents_dir.join("TEMPLATE.md"), "# template").unwrap();

        let db = make_db_with_project("p1", "P");
        let report = populate_project_state_from_filesystem("p1", "P", &folder, &db);

        assert_eq!(report.agents_inserted, 1);
        let rows = db.list_project_agents("p1").unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].agent_name, "coder");

        std::fs::remove_dir_all(&folder).ok();
    }

    /// Real agents whose name contains "readme" as a substring must NOT
    /// be skipped — the blocklist matches the FULL stem, not a substring.
    /// e.g. `readme-bot.md` is a hypothetical real agent and should still
    /// land in the DB.
    #[test]
    fn populate_agents_only_skips_exact_stems_not_substrings() {
        let folder = scratch_dir("agents-readme-substring");
        let agents_dir = folder.join(".claude/agents");
        std::fs::create_dir_all(&agents_dir).unwrap();
        write_agent_file(
            &agents_dir,
            "readme-bot.md",
            "name: readme-bot\nmodel: haiku",
            "# RB",
        );

        let db = make_db_with_project("p1", "P");
        let report = populate_project_state_from_filesystem("p1", "P", &folder, &db);
        assert_eq!(report.agents_inserted, 1);
        let rows = db.list_project_agents("p1").unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].agent_name, "readme-bot");

        std::fs::remove_dir_all(&folder).ok();
    }

    /// Legacy state cleanup: if a previous populate run registered a
    /// phantom "README" agent (before the v0.2.22 fix), the NEXT run
    /// must DELETE that row, not just skip re-registering it. Otherwise
    /// existing user databases never clear the phantom row. The fix
    /// proactively calls `unregister_project_agent` on the blocklisted
    /// stem variants whenever it encounters a blocklisted file.
    #[test]
    fn populate_agents_cleans_up_legacy_readme_row() {
        let folder = scratch_dir("agents-readme-cleanup");
        let agents_dir = folder.join(".claude/agents");
        std::fs::create_dir_all(&agents_dir).unwrap();
        write_agent_file(&agents_dir, "coder.md", "name: coder\nmodel: sonnet", "");
        std::fs::write(agents_dir.join("README.md"), "# Docs").unwrap();

        let db = make_db_with_project("p1", "P");

        // Simulate a legacy "README" row inserted by a pre-fix populate
        // (file_path points at README.md, model=None, source=bundled).
        let readme_path = agents_dir.join("README.md").to_string_lossy().to_string();
        db.register_project_agent(
            "p1",
            "README",
            "bundled",
            None,
            None,
            Some(&readme_path),
            &serde_json::json!({"description": ""}),
        )
        .unwrap();
        // Confirm the phantom row exists pre-cleanup.
        assert!(db
            .list_project_agents("p1")
            .unwrap()
            .iter()
            .any(|a| a.agent_name == "README"));

        // Run populate — it should DELETE the phantom row AND register coder.
        let _ = populate_project_state_from_filesystem("p1", "P", &folder, &db);
        let rows = db.list_project_agents("p1").unwrap();
        assert!(
            !rows.iter().any(|a| a.agent_name.eq_ignore_ascii_case("readme")),
            "legacy README row must be cleaned up after populate"
        );
        assert!(rows.iter().any(|a| a.agent_name == "coder"));

        std::fs::remove_dir_all(&folder).ok();
    }

    // ─── v0.2.22 item #19: warn when bundled agent .md has no model field ──

    /// When a bundled agent's frontmatter omits the `model:` line, the
    /// scanner writes `model=None` to the DB which the GUI renders as
    /// `—`. That's a valid render (model is genuinely missing) but
    /// almost always indicates a stale user bundle. The scanner emits
    /// a warning in `report.warnings` so the user can correlate.
    #[test]
    fn populate_agents_warns_when_model_missing_from_frontmatter() {
        let folder = scratch_dir("agents-no-model");
        let agents_dir = folder.join(".claude/agents");
        std::fs::create_dir_all(&agents_dir).unwrap();
        write_agent_file(
            &agents_dir,
            "stale-bundle.md",
            "name: stale-bundle\ndescription: bundle without model",
            "# body",
        );

        let db = make_db_with_project("p1", "P");
        let report = populate_project_state_from_filesystem("p1", "P", &folder, &db);

        // Registration still succeeds — missing model is non-fatal.
        assert_eq!(report.agents_inserted, 1);
        let rows = db.list_project_agents("p1").unwrap();
        assert_eq!(rows[0].agent_name, "stale-bundle");
        assert!(rows[0].model.is_none());

        // BUT a warning is emitted naming the agent and pointing at
        // the stale-bundle remedy.
        let has_warning = report.warnings.iter().any(|w| {
            w.contains("stale-bundle")
                && w.contains("no `model:` frontmatter")
                && w.contains("Update bundle")
        });
        assert!(
            has_warning,
            "expected warning about missing model: line, got: {:?}",
            report.warnings
        );

        std::fs::remove_dir_all(&folder).ok();
    }

    /// Mirror of the agent test for skills — when SKILL.md omits the
    /// `model:` line, populate emits a warning AND registers with
    /// `model=None`.
    #[test]
    fn populate_skills_warns_when_model_missing_from_frontmatter() {
        let folder = scratch_dir("skills-no-model");
        let skills_dir = folder.join(".claude/skills");
        std::fs::create_dir_all(&skills_dir).unwrap();
        write_skill_dir(
            &skills_dir,
            "stale-skill",
            "name: stale-skill\ndescription: no model field",
        );

        let db = make_db_with_project("p1", "P");
        let report = populate_project_state_from_filesystem("p1", "P", &folder, &db);

        assert_eq!(report.skills_inserted, 1);
        let rows = db.list_project_skills("p1").unwrap();
        assert!(rows[0].model.is_none());

        let has_warning = report.warnings.iter().any(|w| {
            w.contains("stale-skill")
                && w.contains("no `model:` frontmatter")
                && w.contains("Update bundle")
        });
        assert!(
            has_warning,
            "expected warning about missing skill model: line, got: {:?}",
            report.warnings
        );

        std::fs::remove_dir_all(&folder).ok();
    }

    /// Conversely: an agent with `model: haiku` in frontmatter must NOT
    /// produce the missing-model warning AND the registered row must
    /// have `model = Some("haiku")` — pinning the IPC-layer contract
    /// the GUI's `{a.model ?? '—'}` template relies on.
    /// Regression guard for v0.2.22 item #19's IPC contract.
    #[test]
    fn populate_agents_with_model_serializes_through_to_list() {
        let folder = scratch_dir("agents-with-model");
        let agents_dir = folder.join(".claude/agents");
        std::fs::create_dir_all(&agents_dir).unwrap();
        // The exact shape from production: model + effort + tools (the
        // last two MUST not interfere with the model extraction).
        write_agent_file(
            &agents_dir,
            "code-graph-updater.md",
            "name: code-graph-updater\ndescription: graph updates\ntools: Read, Bash, Grep, Glob\nmodel: haiku\neffort: high",
            "# CGU",
        );

        let db = make_db_with_project("p1", "P");
        let report = populate_project_state_from_filesystem("p1", "P", &folder, &db);

        assert_eq!(report.agents_inserted, 1);
        // No missing-model warning.
        assert!(
            !report.warnings.iter().any(|w| w.contains("no `model:` frontmatter")),
            "model is present; no warning expected, got: {:?}",
            report.warnings
        );

        // The row's model is Some("haiku") — survives the upsert.
        let rows = db.list_project_agents("p1").unwrap();
        assert_eq!(rows[0].agent_name, "code-graph-updater");
        assert_eq!(rows[0].model.as_deref(), Some("haiku"));

        // Pin the IPC contract — serialize the row exactly as the Tauri
        // command does and verify the JSON carries `"model":"haiku"`.
        // This is the regression guard for the screenshot bug ("model
        // is correct in DB but `—` in GUI").
        let json = serde_json::to_string(&rows[0]).unwrap();
        assert!(
            json.contains("\"model\":\"haiku\""),
            "IPC JSON must carry model=\"haiku\", got: {}",
            json
        );
        // And ensure no null sneaks in.
        assert!(
            !json.contains("\"model\":null"),
            "IPC JSON must not carry model=null for an agent with model: haiku, got: {}",
            json
        );

        std::fs::remove_dir_all(&folder).ok();
    }

    // ─── populate_skills ────────────────────────────────────────────

    #[test]
    fn populate_skills_inserts_one_per_skill_dir() {
        let folder = scratch_dir("skills-basic");
        let skills_dir = folder.join(".claude/skills");
        std::fs::create_dir_all(&skills_dir).unwrap();
        write_skill_dir(&skills_dir, "architect", "name: architect\ndescription: design");
        write_skill_dir(&skills_dir, "tdd", "name: tdd\ndescription: test-first");
        // Dir without SKILL.md must be ignored.
        std::fs::create_dir_all(skills_dir.join("not-a-skill")).unwrap();

        let db = make_db_with_project("p1", "P");
        let report =
            populate_project_state_from_filesystem("p1", "P", &folder, &db);

        assert_eq!(report.skills_inserted, 2);
        let rows = db.list_project_skills("p1").unwrap();
        assert_eq!(rows.len(), 2);
        let names: Vec<&str> = rows.iter().map(|s| s.skill_name.as_str()).collect();
        assert!(names.contains(&"architect"));
        assert!(names.contains(&"tdd"));

        std::fs::remove_dir_all(&folder).ok();
    }

    // ─── populate_disabled_tests (Wave 2 D: don't-resurrect-disabled) ──
    //
    // Subagent D contract: `populate_project_state_from_filesystem`
    // must NOT silently register an agent or skill that has a
    // `.disabled/` companion on disk. The companion encodes the
    // user's explicit disable choice (set by the launcher GUI via
    // `set_project_agent_enabled` / `set_project_skill_enabled`,
    // which moves the file into `.claude/{agents,skills}.disabled/`).
    //
    // Three scenarios per kind:
    //   1. Fresh project: file in `agents/` (no `.disabled/` sibling)
    //      → registered normally.
    //   2. Disabled-only: file ONLY in `agents.disabled/`
    //      → naturally skipped (populate iterates `agents/`).
    //   3. Both: file in BOTH locations (corrupt state)
    //      → registration SKIPPED + warning emitted. The `.disabled/`
    //      copy is untouched (we never write to FS in this code path).

    mod populate_disabled_tests {
        use super::*;

        // ─── Agents ────────────────────────────────────────────────

        #[test]
        fn fresh_project_registers_agent_normally() {
            let folder = scratch_dir("disable-agent-fresh");
            let agents_dir = folder.join(".claude/agents");
            std::fs::create_dir_all(&agents_dir).unwrap();
            write_agent_file(&agents_dir, "foo.md", "name: foo\nmodel: sonnet", "");

            let db = make_db_with_project("p1", "P");
            let report =
                populate_project_state_from_filesystem("p1", "P", &folder, &db);

            assert_eq!(report.agents_inserted, 1);
            let rows = db.list_project_agents("p1").unwrap();
            assert_eq!(rows.len(), 1);
            assert_eq!(rows[0].agent_name, "foo");
            assert!(rows[0].enabled);

            std::fs::remove_dir_all(&folder).ok();
        }

        #[test]
        fn disabled_only_agent_is_naturally_skipped() {
            // Simulates the post-disable state: launcher moved
            // `agents/foo.md` → `agents.disabled/foo.md`. Populate
            // iterates `agents/`, which is now empty for that file,
            // so no row is inserted. The `.disabled/` copy survives
            // populate untouched.
            let folder = scratch_dir("disable-agent-disabled-only");
            let agents_dir = folder.join(".claude/agents");
            let disabled_dir = folder.join(".claude/agents.disabled");
            std::fs::create_dir_all(&agents_dir).unwrap();
            std::fs::create_dir_all(&disabled_dir).unwrap();
            // Put a file ONLY in the disabled directory.
            write_agent_file(
                &disabled_dir,
                "foo.md",
                "name: foo\nmodel: sonnet",
                "",
            );

            let db = make_db_with_project("p1", "P");
            let report =
                populate_project_state_from_filesystem("p1", "P", &folder, &db);

            // Zero agents registered — the file isn't in `agents/`.
            assert_eq!(report.agents_inserted, 0);
            assert_eq!(db.list_project_agents("p1").unwrap().len(), 0);
            // The `.disabled/` file is still on disk.
            assert!(
                disabled_dir.join("foo.md").exists(),
                "populate must not touch the .disabled/ copy"
            );
            // And the enabled-side file did NOT reappear.
            assert!(
                !agents_dir.join("foo.md").exists(),
                "populate must never resurrect a file into agents/"
            );

            std::fs::remove_dir_all(&folder).ok();
        }

        #[test]
        fn agent_present_in_both_locations_is_skipped_with_warning() {
            // Corrupt state: file in BOTH `agents/foo.md` AND
            // `agents.disabled/foo.md`. The don't-resurrect guard
            // skips registration and emits a warning so the user
            // can clean up (the FS-disable migration in
            // `vct-launcher-core::db::project_state` handles this
            // case automatically on launcher startup).
            let folder = scratch_dir("disable-agent-both");
            let agents_dir = folder.join(".claude/agents");
            let disabled_dir = folder.join(".claude/agents.disabled");
            std::fs::create_dir_all(&agents_dir).unwrap();
            std::fs::create_dir_all(&disabled_dir).unwrap();
            write_agent_file(&agents_dir, "foo.md", "name: foo\nmodel: sonnet", "");
            write_agent_file(&disabled_dir, "foo.md", "name: foo\nmodel: sonnet", "");

            let db = make_db_with_project("p1", "P");
            let report =
                populate_project_state_from_filesystem("p1", "P", &folder, &db);

            // Skipped — the disabled companion wins.
            assert_eq!(report.agents_inserted, 0);
            assert!(db.list_project_agents("p1").unwrap().is_empty());
            // Both files survive populate (no FS mutation in this code path).
            assert!(agents_dir.join("foo.md").exists());
            assert!(disabled_dir.join("foo.md").exists());
            // Warning surfaced for user/operator visibility.
            assert!(
                report.warnings.iter().any(|w|
                    w.contains("foo")
                        && w.contains("agents.disabled/")
                ),
                "expected both-locations warning for agent foo, got: {:?}",
                report.warnings
            );

            std::fs::remove_dir_all(&folder).ok();
        }

        // ─── Skills (whole directories) ────────────────────────────

        #[test]
        fn fresh_project_registers_skill_normally() {
            let folder = scratch_dir("disable-skill-fresh");
            let skills_dir = folder.join(".claude/skills");
            std::fs::create_dir_all(&skills_dir).unwrap();
            write_skill_dir(&skills_dir, "tdd", "name: tdd\nmodel: sonnet");

            let db = make_db_with_project("p1", "P");
            let report =
                populate_project_state_from_filesystem("p1", "P", &folder, &db);

            assert_eq!(report.skills_inserted, 1);
            let rows = db.list_project_skills("p1").unwrap();
            assert_eq!(rows.len(), 1);
            assert_eq!(rows[0].skill_name, "tdd");

            std::fs::remove_dir_all(&folder).ok();
        }

        #[test]
        fn disabled_only_skill_is_naturally_skipped() {
            let folder = scratch_dir("disable-skill-disabled-only");
            let skills_dir = folder.join(".claude/skills");
            let disabled_dir = folder.join(".claude/skills.disabled");
            std::fs::create_dir_all(&skills_dir).unwrap();
            std::fs::create_dir_all(&disabled_dir).unwrap();
            write_skill_dir(&disabled_dir, "tdd", "name: tdd\nmodel: sonnet");

            let db = make_db_with_project("p1", "P");
            let report =
                populate_project_state_from_filesystem("p1", "P", &folder, &db);

            assert_eq!(report.skills_inserted, 0);
            assert_eq!(db.list_project_skills("p1").unwrap().len(), 0);
            assert!(
                disabled_dir.join("tdd").join("SKILL.md").exists(),
                "populate must not touch the .disabled/ skill dir"
            );
            assert!(
                !skills_dir.join("tdd").exists(),
                "populate must never resurrect a skill dir into skills/"
            );

            std::fs::remove_dir_all(&folder).ok();
        }

        #[test]
        fn skill_present_in_both_locations_is_skipped_with_warning() {
            let folder = scratch_dir("disable-skill-both");
            let skills_dir = folder.join(".claude/skills");
            let disabled_dir = folder.join(".claude/skills.disabled");
            std::fs::create_dir_all(&skills_dir).unwrap();
            std::fs::create_dir_all(&disabled_dir).unwrap();
            write_skill_dir(&skills_dir, "tdd", "name: tdd\nmodel: sonnet");
            write_skill_dir(&disabled_dir, "tdd", "name: tdd\nmodel: sonnet");

            let db = make_db_with_project("p1", "P");
            let report =
                populate_project_state_from_filesystem("p1", "P", &folder, &db);

            assert_eq!(report.skills_inserted, 0);
            assert!(db.list_project_skills("p1").unwrap().is_empty());
            assert!(skills_dir.join("tdd").join("SKILL.md").exists());
            assert!(disabled_dir.join("tdd").join("SKILL.md").exists());
            assert!(
                report.warnings.iter().any(|w|
                    w.contains("tdd")
                        && w.contains("skills.disabled/")
                ),
                "expected both-locations warning for skill tdd, got: {:?}",
                report.warnings
            );

            std::fs::remove_dir_all(&folder).ok();
        }
    }

    // ─── populate_hooks ─────────────────────────────────────────────

    #[test]
    fn populate_hooks_reads_settings_json() {
        let folder = scratch_dir("hooks-basic");
        let claude = folder.join(".claude");
        std::fs::create_dir_all(&claude).unwrap();
        let settings = serde_json::json!({
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Edit(*.py)",
                        "hooks": [
                            {"type": "command", "command": "ruff check --fix", "timeout": 5},
                            {"type": "command", "command": "pyright", "background": true}
                        ]
                    }
                ],
                "PostCompact": [
                    {
                        "hooks": [
                            {"type": "command", "command": "bash hooks/post-compact.sh", "timeout": 10}
                        ]
                    }
                ]
            }
        });
        std::fs::write(
            claude.join("settings.json"),
            serde_json::to_string_pretty(&settings).unwrap(),
        )
        .unwrap();

        let db = make_db_with_project("p1", "P");
        let report =
            populate_project_state_from_filesystem("p1", "P", &folder, &db);

        // 2 PostToolUse hooks + 1 PostCompact = 3
        assert_eq!(report.hooks_inserted, 3);
        let rows = db.list_project_hooks("p1").unwrap();
        assert_eq!(rows.len(), 3);
        let ruff = rows
            .iter()
            .find(|h| h.command.contains("ruff"))
            .expect("ruff hook");
        assert_eq!(ruff.event, "PostToolUse");
        assert_eq!(ruff.matcher, "Edit(*.py)");
        // 5s in settings.json → 5000ms in DB.
        assert_eq!(ruff.timeout_ms, Some(5_000));
        assert_eq!(ruff.source, "project");

        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn populate_hooks_missing_settings_json_no_warning() {
        let folder = scratch_dir("hooks-nosettings");
        std::fs::create_dir_all(folder.join(".claude")).unwrap();
        let db = make_db_with_project("p1", "P");
        let report =
            populate_project_state_from_filesystem("p1", "P", &folder, &db);
        assert_eq!(report.hooks_inserted, 0);
        // KG/codegraph still populate; only hooks are missing.
        assert!(report.warnings.is_empty(),
                "no warnings expected for missing settings.json: {:?}", report.warnings);
        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn populate_hooks_corrupted_settings_warns_continues() {
        let folder = scratch_dir("hooks-corrupt");
        let claude = folder.join(".claude");
        std::fs::create_dir_all(&claude).unwrap();
        std::fs::write(claude.join("settings.json"), "{not json").unwrap();
        let db = make_db_with_project("p1", "P");
        let report =
            populate_project_state_from_filesystem("p1", "P", &folder, &db);
        assert_eq!(report.hooks_inserted, 0);
        assert!(report
            .warnings
            .iter()
            .any(|w| w.contains("settings.json") && w.contains("parse error")));
        // KG/codegraph still landed (independent of settings.json).
        assert_eq!(report.kg_bindings_inserted, 2);
        assert_eq!(report.codegraph_bindings_inserted, 1);
        std::fs::remove_dir_all(&folder).ok();
    }

    // ─── KG / codegraph defaults ────────────────────────────────────

    #[test]
    fn populate_kg_bindings_writes_primary_and_shared() {
        let folder = scratch_dir("kg");
        let db = make_db_with_project("p1", "Acme");
        let report =
            populate_project_state_from_filesystem("p1", "Acme", &folder, &db);
        assert_eq!(report.kg_bindings_inserted, 2);

        let bindings = db.list_project_kg_bindings("p1").unwrap();
        assert_eq!(bindings.len(), 2);
        let primary = bindings.iter().find(|b| b.role == "primary").unwrap();
        assert_eq!(primary.collection_name, "Acme_KnowledgeGraph");
        assert_eq!(primary.embedding_model.as_deref(), Some("qwen3-embedding:0.6b"));
        assert_eq!(primary.embedding_dim, Some(1024));
        assert_eq!(
            primary.weaviate_url.as_deref(),
            Some("http://localhost:8081")
        );
        let shared = bindings.iter().find(|b| b.role == "shared").unwrap();
        assert_eq!(shared.collection_name, "VibeCodedOrchestrator_KnowledgeGraph");

        std::fs::remove_dir_all(&folder).ok();
    }

    // ─── PR-3 Commit 2: kg_collection_access auto-population ──────────

    #[test]
    fn populate_kg_collection_access_grants_own_and_shared() {
        let folder = scratch_dir("kg-access-default");
        let db = make_db_with_project("p1", "Acme");
        let report =
            populate_project_state_from_filesystem("p1", "Acme", &folder, &db);

        // 3 default rows: own primary (write), own dev (write), shared (read)
        assert_eq!(report.kg_access_rows_inserted, 3);

        let access = db.kg_list_access("p1").unwrap();
        let by_collection: std::collections::HashMap<&str, &str> = access
            .iter()
            .map(|(c, l)| (c.as_str(), l.as_str()))
            .collect();
        assert_eq!(by_collection.get("Acme_KnowledgeGraph"), Some(&"write"));
        assert_eq!(by_collection.get("Acme_Development"), Some(&"write"));
        assert_eq!(
            by_collection.get("VibeCodedOrchestrator_KnowledgeGraph"),
            Some(&"read")
        );

        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn populate_kg_collection_access_idempotent_preserves_user_level() {
        // The user explicitly downgrades the shared collection to "none",
        // then re-populates. The level must survive — default-grant should
        // not run on a (project, collection) that already has a row.
        let folder = scratch_dir("kg-access-preserve");
        let db = make_db_with_project("p1", "Acme");
        populate_project_state_from_filesystem("p1", "Acme", &folder, &db);

        // User downgrades shared collection.
        db.kg_set_access("p1", "VibeCodedOrchestrator_KnowledgeGraph", "none")
            .unwrap();
        // User upgrades own dev to write (was already write — confirm
        // it stays write after re-run).
        db.kg_set_access("p1", "Acme_Development", "write").unwrap();

        // Re-run.
        let report =
            populate_project_state_from_filesystem("p1", "Acme", &folder, &db);
        // No new rows inserted — every default already exists.
        assert_eq!(report.kg_access_rows_inserted, 0);

        let access = db.kg_list_access("p1").unwrap();
        let by_collection: std::collections::HashMap<&str, &str> = access
            .iter()
            .map(|(c, l)| (c.as_str(), l.as_str()))
            .collect();
        // User's downgrade survived re-populate.
        assert_eq!(
            by_collection.get("VibeCodedOrchestrator_KnowledgeGraph"),
            Some(&"none"),
            "user-set 'none' must NOT be reset to default 'read'"
        );

        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn populate_kg_collection_access_runs_for_empty_project_folder() {
        // Even when `.claude/` doesn't exist, default access rows must
        // be written — otherwise the bundle install (which lands later)
        // can't read its own KG.
        let folder = scratch_dir("kg-access-empty");
        let db = make_db_with_project("p1", "Empty");
        let report =
            populate_project_state_from_filesystem("p1", "Empty", &folder, &db);
        assert_eq!(report.kg_access_rows_inserted, 3);
        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn populate_codegraph_binding_uses_sanitized_prefix() {
        let folder = scratch_dir("cg");
        let db = make_db_with_project("p1", "my project name");
        let report = populate_project_state_from_filesystem(
            "p1",
            "my project name",
            &folder,
            &db,
        );
        assert_eq!(report.codegraph_bindings_inserted, 1);
        let cg = db.get_project_codegraph_binding("p1").unwrap().unwrap();
        // sanitize_kg_collection lowercases punctuation, TitleCases words.
        assert_eq!(cg.collection_prefix, "MyProjectName");
        assert_eq!(cg.embedding_model.as_deref(), Some("codesage-large-v2"));
        assert_eq!(cg.embedding_dim, Some(2048));
        assert!(cg.enabled);
        std::fs::remove_dir_all(&folder).ok();
    }

    // ─── Idempotence ────────────────────────────────────────────────

    #[test]
    fn double_run_does_not_duplicate_rows() {
        let folder = scratch_dir("idem");
        let agents_dir = folder.join(".claude/agents");
        std::fs::create_dir_all(&agents_dir).unwrap();
        write_agent_file(
            &agents_dir,
            "coder.md",
            "name: coder\nmodel: sonnet",
            "# coder",
        );
        let skills_dir = folder.join(".claude/skills");
        std::fs::create_dir_all(&skills_dir).unwrap();
        write_skill_dir(&skills_dir, "architect", "name: architect");
        let claude = folder.join(".claude");
        std::fs::write(
            claude.join("settings.json"),
            serde_json::json!({
                "hooks": {"PostToolUse": [{"hooks":[{"type":"command","command":"echo"}]}]}
            })
            .to_string(),
        )
        .unwrap();

        let db = make_db_with_project("p1", "P");
        let _ = populate_project_state_from_filesystem("p1", "P", &folder, &db);
        let _ = populate_project_state_from_filesystem("p1", "P", &folder, &db);

        let agents = db.list_project_agents("p1").unwrap();
        assert_eq!(agents.len(), 1, "agents must not duplicate on re-run");
        let skills = db.list_project_skills("p1").unwrap();
        assert_eq!(skills.len(), 1, "skills must not duplicate");
        let hooks = db.list_project_hooks("p1").unwrap();
        assert_eq!(hooks.len(), 1, "hooks must not duplicate");
        let kg = db.list_project_kg_bindings("p1").unwrap();
        assert_eq!(kg.len(), 2, "kg bindings must not duplicate");

        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn re_run_preserves_user_disabled_flag_for_agents() {
        let folder = scratch_dir("preserve-agent");
        let agents_dir = folder.join(".claude/agents");
        std::fs::create_dir_all(&agents_dir).unwrap();
        write_agent_file(&agents_dir, "coder.md", "name: coder\nmodel: sonnet", "");

        let db = make_db_with_project("p1", "P");
        populate_project_state_from_filesystem("p1", "P", &folder, &db);
        // User disables it via the GUI.
        db.set_project_agent_enabled("p1", "coder", false).unwrap();
        // Re-run populate (e.g. user re-onboards the project).
        populate_project_state_from_filesystem("p1", "P", &folder, &db);

        let agents = db.list_project_agents("p1").unwrap();
        assert_eq!(agents.len(), 1);
        assert!(
            !agents[0].enabled,
            "user's disabled flag must survive re-populate"
        );
        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn re_run_preserves_user_disabled_flag_for_skills() {
        let folder = scratch_dir("preserve-skill");
        let skills_dir = folder.join(".claude/skills");
        std::fs::create_dir_all(&skills_dir).unwrap();
        write_skill_dir(&skills_dir, "tdd", "name: tdd");

        let db = make_db_with_project("p1", "P");
        populate_project_state_from_filesystem("p1", "P", &folder, &db);
        db.set_project_skill_enabled("p1", "tdd", false).unwrap();
        populate_project_state_from_filesystem("p1", "P", &folder, &db);

        let skills = db.list_project_skills("p1").unwrap();
        assert!(!skills[0].enabled);
        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn re_run_preserves_user_disabled_flag_for_hooks() {
        let folder = scratch_dir("preserve-hook");
        let claude = folder.join(".claude");
        std::fs::create_dir_all(&claude).unwrap();
        std::fs::write(
            claude.join("settings.json"),
            serde_json::json!({
                "hooks": {"PostToolUse": [{"matcher": "*", "hooks":[{"type":"command","command":"echo"}]}]}
            })
            .to_string(),
        )
        .unwrap();

        let db = make_db_with_project("p1", "P");
        populate_project_state_from_filesystem("p1", "P", &folder, &db);
        let hooks = db.list_project_hooks("p1").unwrap();
        let hook_id = hooks[0].id;
        db.set_project_hook_enabled(hook_id, false).unwrap();
        populate_project_state_from_filesystem("p1", "P", &folder, &db);

        let hooks = db.list_project_hooks("p1").unwrap();
        assert_eq!(hooks.len(), 1);
        assert!(!hooks[0].enabled, "user's disabled flag must survive on hooks too");
        std::fs::remove_dir_all(&folder).ok();
    }

    // ─── CLAUDE.md Dev Constraint #8(a) regression pin ──────────────
    //
    // Multi-row breadth test: when a user disables MULTIPLE rows across
    // ALL THREE shapes (agents, skills, hooks) and the populate helper
    // runs a second time (the install-bundle --update launcher flow
    // re-hits this code path on the next launcher boot for un-seeded
    // projects + the orchestrator-root ensure_orchestrator_root path
    // calls it on every boot), every disabled row must remain disabled,
    // AND no other row may have been incidentally flipped from enabled
    // to disabled (or vice-versa).
    //
    // The pre-existing single-row tests
    // (`re_run_preserves_user_disabled_flag_for_{agents,skills,hooks}`)
    // each cover one shape with one disabled row. This test pins the
    // contract for the realistic multi-row case the user runs into
    // (disable several agents they don't use + a couple skills + one
    // hook they don't want firing) and confirms the populate pass is
    // surgical, not bulk-resetting.
    //
    // Why this is the right pin for #8(a): the constraint says
    // "user-editable settings survive every update path". The
    // population path uses SQL `ON CONFLICT DO UPDATE SET` clauses
    // that intentionally omit the `enabled` column (agents/skills/hooks
    // share that pattern via `register_project_*` in
    // `db/project_state.rs`). If a future refactor adds `enabled =
    // excluded.enabled` to any of those upserts, this test catches it.
    #[test]
    fn re_run_preserves_3_agents_2_skills_1_hook_disabled_together() {
        let folder = scratch_dir("preserve-multi");
        let claude = folder.join(".claude");
        let agents_dir = claude.join("agents");
        let skills_dir = claude.join("skills");
        std::fs::create_dir_all(&agents_dir).unwrap();
        std::fs::create_dir_all(&skills_dir).unwrap();

        // 5 agents — we'll disable 3, leave 2 enabled. Names mirror real
        // bundled agent files (coder.md, planner.md, tester.md, reviewer.md,
        // architect.md ship in templates/agents/).
        for n in &["coder", "planner", "tester", "reviewer", "architect"] {
            write_agent_file(
                &agents_dir,
                &format!("{}.md", n),
                &format!("name: {}\nmodel: sonnet", n),
                "# body",
            );
        }
        // 4 skills — we'll disable 2, leave 2 enabled. Names mirror real
        // bundled skills (tdd, architect, fix-issue, context).
        for n in &["tdd", "architect", "fix-issue", "context"] {
            write_skill_dir(&skills_dir, n, &format!("name: {}\nmodel: sonnet", n));
        }
        // 3 hooks — we'll disable 1, leave 2 enabled. The matcher+command
        // combo uniquely identifies the row (UNIQUE on
        // project_id/event/matcher/command).
        std::fs::write(
            claude.join("settings.json"),
            serde_json::json!({
                "hooks": {
                    "PostToolUse": [
                        {"matcher": "Edit(*)",  "hooks": [{"type": "command", "command": "hook-edit"}]},
                        {"matcher": "Write(*)", "hooks": [{"type": "command", "command": "hook-write"}]}
                    ],
                    "PreToolUse": [
                        {"matcher": "*", "hooks": [{"type": "command", "command": "hook-pre"}]}
                    ]
                }
            })
            .to_string(),
        )
        .unwrap();

        let db = make_db_with_project("p1", "MultiPreserve");

        // First populate — establish baseline.
        let r1 = populate_project_state_from_filesystem(
            "p1",
            "MultiPreserve",
            &folder,
            &db,
        );
        assert_eq!(r1.agents_inserted, 5, "baseline: 5 agents seeded");
        assert_eq!(r1.skills_inserted, 4, "baseline: 4 skills seeded");
        assert_eq!(r1.hooks_inserted, 3, "baseline: 3 hooks seeded");

        // User disables 3 agents, 2 skills, 1 hook via the GUI / DB.
        const DISABLED_AGENTS: [&str; 3] = ["coder", "tester", "architect"];
        const DISABLED_SKILLS: [&str; 2] = ["tdd", "context"];
        for a in &DISABLED_AGENTS {
            db.set_project_agent_enabled("p1", a, false).unwrap();
        }
        for s in &DISABLED_SKILLS {
            db.set_project_skill_enabled("p1", s, false).unwrap();
        }
        // For hooks the API takes the id, not the name — look it up.
        let pre_hooks = db.list_project_hooks("p1").unwrap();
        let disabled_hook = pre_hooks
            .iter()
            .find(|h| h.command == "hook-pre")
            .expect("hook-pre seeded");
        db.set_project_hook_enabled(disabled_hook.id, false).unwrap();
        let disabled_hook_id = disabled_hook.id;

        // Sanity snapshot pre-second-populate.
        let pre_agents = db.list_project_agents("p1").unwrap();
        let pre_skills = db.list_project_skills("p1").unwrap();
        let pre_hooks = db.list_project_hooks("p1").unwrap();
        assert_eq!(
            pre_agents.iter().filter(|a| !a.enabled).count(),
            3,
            "pre: exactly 3 agents disabled"
        );
        assert_eq!(
            pre_skills.iter().filter(|s| !s.enabled).count(),
            2,
            "pre: exactly 2 skills disabled"
        );
        assert_eq!(
            pre_hooks.iter().filter(|h| !h.enabled).count(),
            1,
            "pre: exactly 1 hook disabled"
        );

        // Second populate — same on-disk filesystem, simulating the next
        // launcher boot's populate sweep OR the orchestrator-root
        // ensure_orchestrator_root call.
        let r2 = populate_project_state_from_filesystem(
            "p1",
            "MultiPreserve",
            &folder,
            &db,
        );
        // Re-run reports hits because the upsert touches every row, but
        // the disabled flag must not have been clobbered. The exact
        // *_inserted counts include re-upsert hits in the underlying
        // helpers; we don't pin them here — the contract under test is
        // toggle preservation, not insert tallies.
        let _ = r2; // warnings are non-fatal; we don't assert on them.

        // ─── Contract pins ────────────────────────────────────────────
        // a) The 3 disabled agents are STILL disabled.
        let post_agents = db.list_project_agents("p1").unwrap();
        assert_eq!(post_agents.len(), 5, "agent row count unchanged");
        for name in &DISABLED_AGENTS {
            let row = post_agents
                .iter()
                .find(|a| a.agent_name == *name)
                .unwrap_or_else(|| panic!("agent {} must still exist", name));
            assert!(
                !row.enabled,
                "agent {} must remain disabled after re-populate, got enabled={}",
                name, row.enabled
            );
        }
        // b) The 2 OTHER agents stayed enabled (no incidental flip).
        for row in &post_agents {
            if !DISABLED_AGENTS.contains(&row.agent_name.as_str()) {
                assert!(
                    row.enabled,
                    "agent {} must remain enabled (was not in disabled set)",
                    row.agent_name
                );
            }
        }

        // c) The 2 disabled skills are STILL disabled.
        let post_skills = db.list_project_skills("p1").unwrap();
        assert_eq!(post_skills.len(), 4, "skill row count unchanged");
        for name in &DISABLED_SKILLS {
            let row = post_skills
                .iter()
                .find(|s| s.skill_name == *name)
                .unwrap_or_else(|| panic!("skill {} must still exist", name));
            assert!(
                !row.enabled,
                "skill {} must remain disabled after re-populate, got enabled={}",
                name, row.enabled
            );
        }
        // d) The 2 OTHER skills stayed enabled.
        for row in &post_skills {
            if !DISABLED_SKILLS.contains(&row.skill_name.as_str()) {
                assert!(
                    row.enabled,
                    "skill {} must remain enabled (was not in disabled set)",
                    row.skill_name
                );
            }
        }

        // e) The 1 disabled hook is STILL disabled.
        let post_hooks = db.list_project_hooks("p1").unwrap();
        assert_eq!(post_hooks.len(), 3, "hook row count unchanged");
        let post_disabled_hook = post_hooks
            .iter()
            .find(|h| h.id == disabled_hook_id)
            .expect("originally-disabled hook id must still exist");
        assert!(
            !post_disabled_hook.enabled,
            "hook id {} must remain disabled (command=hook-pre)",
            disabled_hook_id
        );
        // f) The 2 OTHER hooks stayed enabled.
        for row in &post_hooks {
            if row.id != disabled_hook_id {
                assert!(
                    row.enabled,
                    "hook id {} (command={}) must remain enabled",
                    row.id, row.command
                );
            }
        }

        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn re_run_preserves_codegraph_enabled_flag() {
        // The codegraph binding's UPSERT-on-conflict would clobber the
        // `enabled` column. populate must NOT call set when a row already
        // exists for this project.
        let folder = scratch_dir("preserve-cg");
        let db = make_db_with_project("p1", "P");
        populate_project_state_from_filesystem("p1", "P", &folder, &db);
        // Disable the codegraph binding (mimics user toggling it off).
        db.set_project_codegraph_binding(
            "p1",
            "P",
            Some("codesage-large-v2"),
            Some(2048),
            None,
            None,
            false,
            &JsonValue::Null,
        )
        .unwrap();
        // Re-run populate. The pre-check must short-circuit and leave
        // enabled=false.
        populate_project_state_from_filesystem("p1", "P", &folder, &db);
        let cg = db.get_project_codegraph_binding("p1").unwrap().unwrap();
        assert!(!cg.enabled, "codegraph enabled flag must be preserved");
        std::fs::remove_dir_all(&folder).ok();
    }

    // ─── Empty-folder edge case ─────────────────────────────────────

    #[test]
    fn empty_project_folder_still_writes_kg_and_codegraph() {
        let folder = scratch_dir("empty");
        let db = make_db_with_project("p1", "Acme");
        let report =
            populate_project_state_from_filesystem("p1", "Acme", &folder, &db);
        assert_eq!(report.agents_inserted, 0);
        assert_eq!(report.skills_inserted, 0);
        assert_eq!(report.hooks_inserted, 0);
        assert_eq!(report.kg_bindings_inserted, 2);
        assert_eq!(report.codegraph_bindings_inserted, 1);
        std::fs::remove_dir_all(&folder).ok();
    }

    // ─── MCP servers (migration 010, 2026-05-10) ───────────────────

    #[test]
    fn populate_mcp_servers_from_settings_json_flags_user_added() {
        // settings.json carries one bundled (weaviate-kg) and one
        // user-added (transcrypt-live) entry. Populate must flag them
        // correctly so the Custom MCP tab filters surface only the
        // user-added one.
        let folder = scratch_dir("mcp-settings");
        let claude = folder.join(".claude");
        std::fs::create_dir_all(&claude).unwrap();
        let settings = serde_json::json!({
            "mcpServers": {
                "weaviate-kg": {
                    "command": "/usr/bin/python3",
                    "args": ["-m", "weaviate_mcp.server"],
                    "env": {"OLLAMA_URL": "http://localhost:11435"}
                },
                "transcrypt-live": {
                    "command": "/usr/local/bin/transcrypt-live",
                    "args": []
                }
            }
        });
        std::fs::write(
            claude.join("settings.json"),
            serde_json::to_string_pretty(&settings).unwrap(),
        )
        .unwrap();

        let db = make_db_with_project("p1", "Acme");
        let report =
            populate_project_state_from_filesystem("p1", "Acme", &folder, &db);

        assert_eq!(report.mcp_servers_inserted, 2);
        let mcp = db.list_project_mcp_servers("p1").unwrap();
        assert_eq!(mcp.len(), 2);
        let by_name: std::collections::HashMap<&str, &crate::db::project_mcp_servers::ProjectMcpServer> =
            mcp.iter().map(|m| (m.mcp_name.as_str(), m)).collect();
        let weaviate = by_name.get("weaviate-kg").expect("weaviate-kg row");
        assert!(!weaviate.is_user_added, "weaviate-kg is bundled");
        assert_eq!(weaviate.source, "bundled");
        assert_eq!(weaviate.command.as_deref(), Some("/usr/bin/python3"));
        assert_eq!(weaviate.source_file.as_deref(), Some(".claude/settings.json"));

        let transcrypt = by_name.get("transcrypt-live").expect("transcrypt-live row");
        assert!(transcrypt.is_user_added, "transcrypt-live is user-added");
        assert_eq!(transcrypt.source, "user");

        // Custom MCP tab feed surfaces only the user-added entry.
        let custom = db.list_user_added_mcp_servers("p1").unwrap();
        assert_eq!(custom.len(), 1);
        assert_eq!(custom[0].mcp_name, "transcrypt-live");

        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn populate_mcp_servers_from_mcp_json_works_without_claude_dir() {
        // Project folder has `.mcp.json` at the top level but NO
        // `.claude/` subdirectory yet. The populate dispatcher's
        // empty-folder branch must still pick up MCP servers from
        // `.mcp.json`.
        let folder = scratch_dir("mcp-only-mcpjson");
        let mcp_json = serde_json::json!({
            "mcpServers": {
                "my-custom-mcp": {
                    "command": "/path/to/mcp",
                    "args": ["--port", "9000"]
                }
            }
        });
        std::fs::write(
            folder.join(".mcp.json"),
            serde_json::to_string_pretty(&mcp_json).unwrap(),
        )
        .unwrap();

        let db = make_db_with_project("p1", "Acme");
        let report =
            populate_project_state_from_filesystem("p1", "Acme", &folder, &db);

        assert_eq!(report.mcp_servers_inserted, 1);
        let rows = db.list_user_added_mcp_servers("p1").unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].mcp_name, "my-custom-mcp");
        assert_eq!(rows[0].source_file.as_deref(), Some(".mcp.json"));
        assert_eq!(rows[0].command.as_deref(), Some("/path/to/mcp"));

        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn populate_mcp_servers_merges_settings_and_mcp_json() {
        // Both source files present with overlapping names — `.mcp.json`
        // wins (last-write semantics). Documents the precedence so the
        // Custom MCP tab matches Claude Code's own loader behaviour.
        let folder = scratch_dir("mcp-both-files");
        let claude = folder.join(".claude");
        std::fs::create_dir_all(&claude).unwrap();
        std::fs::write(
            claude.join("settings.json"),
            serde_json::json!({
                "mcpServers": {
                    "shared-name": {"command": "/from/settings.json"},
                    "settings-only": {"command": "/only/in/settings"}
                }
            })
            .to_string(),
        )
        .unwrap();
        std::fs::write(
            folder.join(".mcp.json"),
            serde_json::json!({
                "mcpServers": {
                    "shared-name": {"command": "/from/mcp.json"},
                    "mcp-only": {"command": "/only/in/mcp"}
                }
            })
            .to_string(),
        )
        .unwrap();

        let db = make_db_with_project("p1", "Acme");
        let report =
            populate_project_state_from_filesystem("p1", "Acme", &folder, &db);

        // 4 register calls: 2 from settings.json + 2 from .mcp.json. The
        // shared-name UPSERTs once, so the table has 3 distinct rows but
        // the inserted counter logs the call count.
        assert_eq!(report.mcp_servers_inserted, 4);
        let rows = db.list_project_mcp_servers("p1").unwrap();
        assert_eq!(rows.len(), 3);
        let shared = rows
            .iter()
            .find(|r| r.mcp_name == "shared-name")
            .expect("shared-name");
        assert_eq!(
            shared.command.as_deref(),
            Some("/from/mcp.json"),
            ".mcp.json should override settings.json"
        );
        assert_eq!(
            shared.source_file.as_deref(),
            Some(".mcp.json"),
            "source_file reflects the last write"
        );

        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn populate_mcp_servers_idempotent_preserves_user_disable() {
        let folder = scratch_dir("mcp-idempotent");
        let claude = folder.join(".claude");
        std::fs::create_dir_all(&claude).unwrap();
        std::fs::write(
            claude.join("settings.json"),
            serde_json::json!({
                "mcpServers": {
                    "my-mcp": {"command": "x"}
                }
            })
            .to_string(),
        )
        .unwrap();

        let db = make_db_with_project("p1", "Acme");
        populate_project_state_from_filesystem("p1", "Acme", &folder, &db);
        // User disables it via the GUI.
        db.set_project_mcp_server_enabled("p1", "my-mcp", false).unwrap();
        // Re-run populate — disabled flag must survive.
        populate_project_state_from_filesystem("p1", "Acme", &folder, &db);
        let rows = db.list_project_mcp_servers("p1").unwrap();
        assert_eq!(rows.len(), 1);
        assert!(
            !rows[0].enabled,
            "user's disabled flag must survive re-populate"
        );

        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn populate_mcp_servers_no_settings_no_mcpjson_inserts_zero() {
        // Empty project folder — neither file exists. Populate must
        // simply insert zero rows and return zero warnings (a missing
        // file is not an error condition).
        let folder = scratch_dir("mcp-none");
        let db = make_db_with_project("p1", "Acme");
        let report =
            populate_project_state_from_filesystem("p1", "Acme", &folder, &db);
        assert_eq!(report.mcp_servers_inserted, 0);
        // The KG/codegraph/access populators run unconditionally and
        // generate no MCP-related warnings.
        let mcp_warnings: Vec<_> = report
            .warnings
            .iter()
            .filter(|w| w.contains("mcp") || w.contains("settings.json"))
            .collect();
        assert!(
            mcp_warnings.is_empty(),
            "no MCP warnings expected, got: {:?}",
            mcp_warnings
        );

        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn populate_mcp_servers_corrupt_mcpjson_warns_continues() {
        // `.mcp.json` parse error must surface in warnings so a typo
        // doesn't silently hide every user-added MCP. settings.json
        // parse errors are already warned about by populate_hooks; we
        // intentionally skip a duplicate warning there.
        let folder = scratch_dir("mcp-corrupt");
        std::fs::write(folder.join(".mcp.json"), "{not json").unwrap();

        let db = make_db_with_project("p1", "Acme");
        let report =
            populate_project_state_from_filesystem("p1", "Acme", &folder, &db);
        assert_eq!(report.mcp_servers_inserted, 0);
        assert!(
            report
                .warnings
                .iter()
                .any(|w| w.contains(".mcp.json") && w.contains("parse error")),
            "expected parse-error warning for .mcp.json, got: {:?}",
            report.warnings
        );
        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn populate_mcp_servers_appears_in_snapshot() {
        // The GUI reads via get_project_state_snapshot; confirm
        // mcp_servers field is wired through.
        let folder = scratch_dir("mcp-snapshot");
        let claude = folder.join(".claude");
        std::fs::create_dir_all(&claude).unwrap();
        std::fs::write(
            claude.join("settings.json"),
            serde_json::json!({
                "mcpServers": {
                    "weaviate-kg": {"command": "/x"},
                    "my-mcp": {"command": "/y"}
                }
            })
            .to_string(),
        )
        .unwrap();
        let db = make_db_with_project("p1", "Acme");
        populate_project_state_from_filesystem("p1", "Acme", &folder, &db);
        let snap = db.get_project_state_snapshot("p1").unwrap();
        assert_eq!(snap.mcp_servers.len(), 2);
        let user_added: Vec<&str> = snap
            .mcp_servers
            .iter()
            .filter(|m| m.is_user_added)
            .map(|m| m.mcp_name.as_str())
            .collect();
        assert_eq!(user_added, vec!["my-mcp"]);
        std::fs::remove_dir_all(&folder).ok();
    }

    // ─── End-to-end ─────────────────────────────────────────────────

    /// Simulates the full real-project onboarding case: 26 agents, several
    /// skills, hooks block. Verifies the launcher GUI's per-project tabs
    /// would no longer be empty.
    #[test]
    fn end_to_end_simulates_full_onboarding() {
        let folder = scratch_dir("e2e");
        let claude = folder.join(".claude");
        let agents_dir = claude.join("agents");
        let skills_dir = claude.join("skills");
        std::fs::create_dir_all(&agents_dir).unwrap();
        std::fs::create_dir_all(&skills_dir).unwrap();

        for i in 0..26 {
            let name = format!("agent-{:02}", i);
            write_agent_file(
                &agents_dir,
                &format!("{}.md", name),
                &format!("name: {}\nmodel: sonnet", name),
                "# body",
            );
        }
        for n in &["architect", "tdd", "context", "fix-issue"] {
            write_skill_dir(&skills_dir, n, &format!("name: {}\nmodel: sonnet", n));
        }
        std::fs::write(
            claude.join("settings.json"),
            serde_json::json!({
                "hooks": {
                    "PostToolUse": [
                        {"matcher":"Edit(*)","hooks":[{"type":"command","command":"hook1"}]},
                        {"matcher":"Write(*)","hooks":[{"type":"command","command":"hook2"}]}
                    ]
                }
            })
            .to_string(),
        )
        .unwrap();

        let db = make_db_with_project("acme-id", "Acme");
        let report = populate_project_state_from_filesystem(
            "acme-id",
            "Acme",
            &folder,
            &db,
        );

        assert_eq!(report.agents_inserted, 26);
        assert_eq!(report.skills_inserted, 4);
        assert_eq!(report.hooks_inserted, 2);
        assert_eq!(report.kg_bindings_inserted, 2);
        assert_eq!(report.codegraph_bindings_inserted, 1);
        assert!(report.warnings.is_empty(), "warnings: {:?}", report.warnings);

        // Snapshot is what the GUI consumes — verify it has everything.
        let snap = db.get_project_state_snapshot("acme-id").unwrap();
        assert_eq!(snap.agents.len(), 26);
        assert_eq!(snap.skills.len(), 4);
        assert_eq!(snap.hooks.len(), 2);
        assert_eq!(snap.kg_bindings.len(), 2);
        assert!(snap.codegraph_binding.is_some());

        std::fs::remove_dir_all(&folder).ok();
    }

    // ─── v0.2.49 item #13 (M-3): global-scope module KG access populate ──
    // Tests for Option A — manifest field `kg_collections` + populate code
    // running on `install.scope=global` install. Pre-implementation these
    // should fail to COMPILE because:
    //   - `populate_kg_collection_access_for_global_module` doesn't exist
    //   - `ModuleManifest::kg_collections` field doesn't exist
    //   - `Db::resolve_default_access_level` doesn't exist
    //
    // When main chat Phase 2 lands `resolve_default_access_level` + I add
    // the manifest field + the populate helper, these tests pass.

    #[test]
    fn global_module_with_kg_collections_populates_access_for_all_projects() {
        let _folder = scratch_dir("global-kg-populate");
        let db = make_db_with_project("p1", "P1");
        // Add a second project so we can verify access lands on BOTH.
        let folder2 = if cfg!(windows) { r"C:\tmp\y" } else { "/tmp/y" };
        let slug2 = db.generate_unique_slug("P2").unwrap();
        db.insert_project("p2", "P2", folder2, crate::db::models::ProjectHost::Base, &slug2).unwrap();

        let mut report = PopulateReport::default();
        populate_kg_collection_access_for_global_module(
            &["RLMeta_KnowledgeGraph".to_string()],
            &db,
            &mut report,
        );

        // 1 collection × 2 projects = 2 access rows inserted.
        assert_eq!(report.kg_access_rows_inserted, 2);
        assert!(report.warnings.is_empty(), "warnings: {:?}", report.warnings);

        let p1_access = db.kg_get_access("p1", "RLMeta_KnowledgeGraph").unwrap();
        let p2_access = db.kg_get_access("p2", "RLMeta_KnowledgeGraph").unwrap();
        // Both projects get the resolver's default (write for own; for
        // a global-shipped collection neither project owns, the resolver
        // determines this — likely write per F-2a "default R/W on shared").
        assert!(p1_access.is_some());
        assert!(p2_access.is_some());
    }

    #[test]
    fn global_module_with_empty_kg_collections_no_access_rows() {
        let _folder = scratch_dir("global-kg-empty");
        let db = make_db_with_project("p1", "P1");

        let mut report = PopulateReport::default();
        populate_kg_collection_access_for_global_module(&[], &db, &mut report);

        assert_eq!(report.kg_access_rows_inserted, 0);
        assert!(report.warnings.is_empty());
    }

    #[test]
    fn global_module_kg_collections_idempotent_preserves_user_level() {
        // End-to-end invariant verification leveraging Step A.5's
        // `kg_seed_access` non-clobber semantics + `is_user_configured()`:
        //   1. First install: kg_seed_access writes row with
        //      created_at == updated_at → is_user_configured FALSE
        //   2. User downgrades via kg_set_access (mutation path) → UPSERT
        //      bumps updated_at → is_user_configured TRUE
        //   3. Second install: kg_seed_access detects existing row,
        //      returns 0 (preserved), no clobber. Row stays at "none" +
        //      still flagged user_configured.
        let _folder = scratch_dir("global-kg-idempotent");
        let db = make_db_with_project("p1", "P1");

        // First install via the seed path.
        let mut report1 = PopulateReport::default();
        populate_kg_collection_access_for_global_module(
            &["RLMeta_KG".to_string()],
            &db,
            &mut report1,
        );
        assert_eq!(report1.kg_access_rows_inserted, 1);
        let row_seeded = db.kg_get_access_row("p1", "RLMeta_KG").unwrap().unwrap();
        assert!(
            !row_seeded.is_user_configured(),
            "freshly-seeded row should NOT read as user-configured"
        );

        // Sleep 2ms to ensure user mutation lands in a distinct millisecond
        // from the seed write — `is_user_configured` reads
        // `updated_at != created_at` (both in millis). Without this,
        // back-to-back calls in test collide on the same ms and the
        // assertion below would flake. Production callers always separate
        // seed and user mutation by orders of magnitude more.
        std::thread::sleep(std::time::Duration::from_millis(2));

        // User downgrades to none via kg_set_access (user-mutation path).
        db.kg_set_access("p1", "RLMeta_KG", "none").unwrap();
        let row_after_user = db.kg_get_access_row("p1", "RLMeta_KG").unwrap().unwrap();
        assert!(
            row_after_user.is_user_configured(),
            "user mutation via kg_set_access should flip is_user_configured to TRUE"
        );

        // Second install: kg_seed_access returns 0, row preserved (still 'none').
        let mut report2 = PopulateReport::default();
        populate_kg_collection_access_for_global_module(
            &["RLMeta_KG".to_string()],
            &db,
            &mut report2,
        );
        assert_eq!(
            report2.kg_access_rows_inserted, 0,
            "re-install must not clobber a user-configured row"
        );
        assert_eq!(
            db.kg_get_access("p1", "RLMeta_KG").unwrap(),
            Some("none".to_string()),
            "user's explicit downgrade preserved"
        );

        // The user-configured invariant survives the re-install.
        let row_final = db.kg_get_access_row("p1", "RLMeta_KG").unwrap().unwrap();
        assert!(
            row_final.is_user_configured(),
            "re-install must preserve is_user_configured invariant"
        );
    }

    #[test]
    fn global_module_multiple_kg_collections_populates_all() {
        let _folder = scratch_dir("global-kg-multi");
        let db = make_db_with_project("p1", "P1");

        let mut report = PopulateReport::default();
        populate_kg_collection_access_for_global_module(
            &[
                "MetaKG_A".to_string(),
                "MetaKG_B".to_string(),
                "MetaKG_C".to_string(),
            ],
            &db,
            &mut report,
        );

        // 3 collections × 1 project = 3 rows.
        assert_eq!(report.kg_access_rows_inserted, 3);
        assert!(db.kg_get_access("p1", "MetaKG_A").unwrap().is_some());
        assert!(db.kg_get_access("p1", "MetaKG_B").unwrap().is_some());
        assert!(db.kg_get_access("p1", "MetaKG_C").unwrap().is_some());
    }

    #[test]
    fn global_module_no_projects_returns_empty_report() {
        // Edge case: orchestrator boots before any project is registered.
        // Global module install should not crash.
        let db = Db::open_in_memory().expect("in-memory db");

        let mut report = PopulateReport::default();
        populate_kg_collection_access_for_global_module(
            &["MetaKG".to_string()],
            &db,
            &mut report,
        );

        assert_eq!(report.kg_access_rows_inserted, 0);
        assert!(report.warnings.is_empty());
    }
}
