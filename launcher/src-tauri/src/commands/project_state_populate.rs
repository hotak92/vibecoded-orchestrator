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
//!   - `mcp_servers` table population (verify the table exists + whether
//!     other code already auto-populates it before adding code here).
//!   - The "Open project" button on the launcher's own self-tile.
//!   - The RL reranker's misleading "Open" button.

use std::path::Path;

use serde_json::Value as JsonValue;

use crate::commands::projects_v2::sanitize_kg_collection;
use crate::db::Db;

/// Result summary for diagnostic logging. Not exposed to the frontend.
#[derive(Debug, Default, Clone)]
pub struct PopulateReport {
    pub agents_inserted: usize,
    pub skills_inserted: usize,
    pub hooks_inserted: usize,
    pub kg_bindings_inserted: usize,
    pub codegraph_bindings_inserted: usize,
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
        return report;
    }

    populate_agents(project_id, &claude_dir, db, &mut report);
    populate_skills(project_id, &claude_dir, db, &mut report);
    populate_hooks(project_id, &claude_dir, db, &mut report);
    populate_kg_bindings(project_id, project_name, db, &mut report);
    populate_codegraph_binding(project_id, project_name, db, &mut report);

    report
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
        db.insert_project(project_id, name, "/tmp/x", ProjectHost::Base, &slug)
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
        assert_eq!(primary.collection_name, "Agape_KnowledgeGraph");
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

    // ─── End-to-end ─────────────────────────────────────────────────

    /// Simulates the full real-project onboarding case: 26 agents, several
    /// skills, hooks block. Verifies the launcher GUI's per-project tabs
    /// would no longer be empty.
    #[test]
    fn end_to_end_simulates_agape_onboarding() {
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
            "agape-id",
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
        let snap = db.get_project_state_snapshot("agape-id").unwrap();
        assert_eq!(snap.agents.len(), 26);
        assert_eq!(snap.skills.len(), 4);
        assert_eq!(snap.hooks.len(), 2);
        assert_eq!(snap.kg_bindings.len(), 2);
        assert!(snap.codegraph_binding.is_some());

        std::fs::remove_dir_all(&folder).ok();
    }
}
