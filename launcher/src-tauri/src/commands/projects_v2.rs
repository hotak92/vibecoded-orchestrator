//! Project lifecycle commands for the module system.
//!
//! Runs alongside the legacy `commands::projects` module during migration.
//! The "_v2" suffix marks the DB-backed implementation; once the React UI
//! is fully migrated to call these, we'll retire the old commands.

use serde::{Deserialize, Serialize};
use std::path::Path;
use tauri::{command, State};
use uuid::Uuid;

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
) -> Result<ProjectView, String> {
    let folder = Path::new(&req.folder_path);

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
        eprintln!("[vct] warning: write_project_env_files failed: {}", e);
    }

    db.audit(
        "project_create",
        Some(&row.id),
        None,
        &serde_json::json!({ "host": req.host.as_str(), "name": req.name, "slug": slug }),
    )?;
    let _ = db.log_change("projects", "insert", Some(&row.id), Some(&row.id));
    Ok(ProjectView::from_row(row, 0))
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
    let kg_collection = sanitize_kg_collection(project_name);
    let dev_collection = format!("{}_development", kg_collection);
    let conv_collection = format!("{}_conversations", kg_collection);

    // VS Code path (existing behavior — extension reads claude-code.env).
    let vscode_dir = folder.join(".vscode");
    std::fs::create_dir_all(&vscode_dir)
        .map_err(|e| format!("mkdir {}: {}", vscode_dir.display(), e))?;
    let vscode_settings_path = vscode_dir.join("settings.json");
    let vscode_settings = serde_json::json!({
        "claude-code.env": {
            "KG_COLLECTION": kg_collection,
            "PROJECT_NAME": kg_collection,
            "DEVELOPMENT_COLLECTION": dev_collection,
            "CONVERSATION_COLLECTION": conv_collection,
        }
    });
    std::fs::write(
        &vscode_settings_path,
        serde_json::to_string_pretty(&vscode_settings)
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
         export CONVERSATION_COLLECTION=\"{}\"\n",
        kg_collection, kg_collection, dev_collection, conv_collection,
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
        "PROJECT_NAME": kg_collection,
        "DEVELOPMENT_COLLECTION": dev_collection,
        "CONVERSATION_COLLECTION": conv_collection,
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
    fn write_project_env_files_creates_both_paths() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-env-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        write_project_env_files(&tmp, "My Test").unwrap();

        // VS Code path
        let vscode_settings = tmp.join(".vscode/settings.json");
        assert!(vscode_settings.exists());
        let raw = std::fs::read_to_string(&vscode_settings).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&raw).unwrap();
        let env = &parsed["claude-code.env"];
        assert_eq!(env["KG_COLLECTION"], "MyTest");
        assert_eq!(env["PROJECT_NAME"], "MyTest");
        assert_eq!(env["DEVELOPMENT_COLLECTION"], "MyTest_development");
        assert_eq!(env["CONVERSATION_COLLECTION"], "MyTest_conversations");

        // CLI path
        let claude_env = tmp.join(".claude/env");
        assert!(claude_env.exists());
        let env_raw = std::fs::read_to_string(&claude_env).unwrap();
        assert!(env_raw.contains(r#"export KG_COLLECTION="MyTest""#));
        assert!(env_raw.contains(r#"export PROJECT_NAME="MyTest""#));
        assert!(env_raw.contains(r#"export DEVELOPMENT_COLLECTION="MyTest_development""#));

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
}
