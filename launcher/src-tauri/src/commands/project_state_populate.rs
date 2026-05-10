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

use crate::commands::projects_v2::sanitize_kg_collection;
use crate::db::project_mcp_servers::is_bundled_mcp;
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

    populate_agents(project_id, &claude_dir, db, &mut report);
    populate_skills(project_id, &claude_dir, db, &mut report);
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
    let pascal = sanitize_kg_collection(project_name);
    let primary_collection = format!("{}_KnowledgeGraph", pascal);
    let dev_collection = format!("{}_Development", pascal);
    let shared_collection = "VibeCodedTools_KnowledgeGraph";

    // Project's OWN primary KG: write access by default. The project's
    // hooks + MCP server need to write to this — the .claude/env carries
    // KG_COLLECTION pointing here.
    grant_default_access(db, project_id, &primary_collection, "write", report);

    // Project's OWN development collection: write access by default.
    // Same rationale — the development collection is the project's
    // private workspace for in-flight notes.
    grant_default_access(db, project_id, &dev_collection, "write", report);

    // Cross-project SHARED KG: read access by default. Writes to the
    // shared KG are gated separately via `SHARED_KG_WRITE_DISABLED` env
    // (asymmetric semantic since 2026-05-01) — the access-matrix row
    // is purely a read-gate. Users who want to grant write access can
    // flip the level via the GUI access matrix; users who want to
    // bottle up writes flip `SHARED_KG_WRITE_DISABLED=true` via the
    // shared-KG toggle (which doesn't touch this row).
    grant_default_access(db, project_id, shared_collection, "read", report);
}

fn grant_default_access(
    db: &Db,
    project_id: &str,
    collection: &str,
    level: &str,
    report: &mut PopulateReport,
) {
    // Idempotency: preserve any user-set level by short-circuiting when
    // a row already exists for this (project, collection). The `kg_set_access`
    // helper would otherwise upsert and clobber.
    match db.kg_get_access(project_id, collection) {
        Ok(Some(_)) => {
            // User-set or previously-defaulted row already exists.
            // Leave alone.
        }
        Ok(None) => {
            if let Err(e) = db.kg_set_access(project_id, collection, level) {
                report
                    .warnings
                    .push(format!("kg_set_access({}): {}", collection, e));
            } else {
                report.kg_access_rows_inserted += 1;
            }
        }
        Err(e) => {
            report
                .warnings
                .push(format!("kg_get_access({}): {}", collection, e));
        }
    }
}

// ─── Agents ────────────────────────────────────────────────────────────

fn populate_agents(
    project_id: &str,
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
    let shared_collection = "VibeCodedTools_KnowledgeGraph";
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
        assert_eq!(shared.collection_name, "VibeCodedTools_KnowledgeGraph");

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
            by_collection.get("VibeCodedTools_KnowledgeGraph"),
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
        db.kg_set_access("p1", "VibeCodedTools_KnowledgeGraph", "none")
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
            by_collection.get("VibeCodedTools_KnowledgeGraph"),
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
            write_skill_dir(&skills_dir, n, &format!("name: {}", n));
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
}
