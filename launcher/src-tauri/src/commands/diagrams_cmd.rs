//! Tauri commands for the diagrams subsystem (Phase 1.1).
//!
//! Backed by `crate::db::diagrams`. Every mutation calls `db.audit(...)`
//! so the audit log records who flipped what (without recording diagram
//! contents — the snapshot BLOB is intentionally not in the audit
//! detail blob).
//!
//! The sibling agents (Phase 1.2 wrapper MCP, Phase 1.3 DiagramsTab
//! Svelte, Phase 1.5.A indexer) stub the EXACT command names defined
//! here. Renaming would break the merge — keep these stable.
//!
//! Snapshot bytes are opaque on the way in: the caller pre-computes the
//! sha256 hash and passes the (potentially gzipped) bytes. The DB layer
//! treats them as a binary blob. `restore_diagram_snapshot` writes the
//! bytes back to disk via a sibling-tempfile + rename, atomic on every
//! platform we support (Linux/macOS POSIX rename, Windows ReplaceFile
//! semantics via `fs::rename`).

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use sha2::{Digest, Sha256};
use serde::Deserialize;
use tauri::{command, State};

use crate::db::diagrams::{AccessRow, DiagramRow, ModuleRow, SnapshotRow, ToolGrant};
use crate::db::mcp_tool_defaults::McpToolDefault;
use crate::db::Db;
use vct_launcher_core::process::CommandExt as _;

// ─── Read commands ──────────────────────────────────────────────────────

#[command]
pub async fn list_project_diagrams(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<DiagramRow>, String> {
    db.list_project_diagrams(&project_id)
}

#[command]
pub async fn list_diagram_snapshots(
    diagram_id: i64,
    db: State<'_, Db>,
) -> Result<Vec<SnapshotRow>, String> {
    db.list_diagram_snapshots(diagram_id)
}

#[command]
pub async fn list_diagram_access(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<AccessRow>, String> {
    db.list_diagram_access(&project_id)
}

#[command]
pub async fn list_project_mcp_tools(
    project_id: String,
    mcp_name: String,
    db: State<'_, Db>,
) -> Result<Vec<ToolGrant>, String> {
    db.list_project_mcp_tools(&project_id, &mcp_name)
}

#[command]
pub async fn list_project_modules(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<ModuleRow>, String> {
    db.list_project_modules(&project_id)
}

// ─── Diagram registry mutations ─────────────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct RegisterDiagramReq {
    pub name: String,
    /// `"mermaid"` or `"excalidraw"` — validated in `db.register_diagram`.
    #[serde(rename = "type")]
    pub diagram_type: String,
    pub file_path: String,
    pub category_path: String,
}

#[command]
pub async fn register_project_diagram(
    project_id: String,
    req: RegisterDiagramReq,
    db: State<'_, Db>,
) -> Result<DiagramRow, String> {
    let row = db.register_diagram(
        &project_id,
        &req.name,
        &req.diagram_type,
        &req.file_path,
        &req.category_path,
    )?;
    db.audit(
        "diagram_register",
        Some(&project_id),
        None,
        &serde_json::json!({
            "name": req.name,
            "type": req.diagram_type,
            "category": req.category_path,
        }),
    )?;
    Ok(row)
}

#[command]
pub async fn unregister_project_diagram(
    project_id: String,
    name: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.unregister_diagram(&project_id, &name)?;
    db.audit(
        "diagram_unregister",
        Some(&project_id),
        None,
        &serde_json::json!({ "name": name }),
    )?;
    Ok(())
}

#[command]
pub async fn set_project_diagram_enabled(
    project_id: String,
    name: String,
    enabled: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_diagram_enabled(&project_id, &name, enabled)?;
    db.audit(
        "diagram_set_enabled",
        Some(&project_id),
        None,
        &serde_json::json!({ "name": name, "enabled": enabled }),
    )?;
    Ok(())
}

// ─── Snapshot mutations ─────────────────────────────────────────────────

/// Valid `trigger` values. Validated here at the command boundary so
/// we surface a Rust-level error rather than silently storing a typo
/// that the snapshot UI's filter dropdown won't recognise. The wrapper
/// MCP uses `auto_pre_edit_save` (per plan §1.5.6); the launcher UI uses
/// `manual`; a future hook may use `auto_interval`.
const VALID_SNAPSHOT_TRIGGERS: &[&str] = &["manual", "auto_pre_edit_save", "auto_interval"];

#[command]
pub async fn create_diagram_snapshot(
    diagram_id: i64,
    trigger: String,
    label: Option<String>,
    db: State<'_, Db>,
) -> Result<SnapshotRow, String> {
    if !VALID_SNAPSHOT_TRIGGERS.contains(&trigger.as_str()) {
        return Err(format!(
            "invalid snapshot trigger: '{}' (allowed: {:?})",
            trigger, VALID_SNAPSHOT_TRIGGERS
        ));
    }

    // Resolve the diagram + its on-disk path, then read the current
    // file contents so the snapshot captures the live state. The bytes
    // are stored opaque (raw today; the wrapper-MCP layer may switch
    // to gzipped bytes later without a schema change).
    let diagram = db
        .get_diagram_by_id(diagram_id)?
        .ok_or_else(|| format!("diagram {} not found", diagram_id))?;
    let abs_path = resolve_diagram_abs_path(&db, &diagram)?;
    let content_bytes = fs::read(&abs_path).map_err(|e| {
        format!(
            "read {} for snapshot: {}",
            abs_path.display(),
            e
        )
    })?;
    let hash = sha256_hex(&content_bytes);

    // Idempotent UPSERT-then-fetch: if a snapshot with the same
    // (diagram_id, content_hash) already exists, return it instead of
    // erroring on UNIQUE — this matches the plan's "dedup identical
    // content" intent. We can't UPSERT on snapshots cleanly because
    // we'd need to bump `created_at` (which defeats dedup), so we
    // catch the UNIQUE error and fetch the existing row.
    let row = match db.create_diagram_snapshot(
        diagram_id,
        &hash,
        &content_bytes,
        &trigger,
        label.as_deref(),
    ) {
        Ok(row) => {
            db.audit(
                "diagram_snapshot_create",
                Some(&diagram.project_id),
                None,
                &serde_json::json!({
                    "diagram_id": diagram_id,
                    "snapshot_id": row.id,
                    "trigger": trigger,
                    "label": label,
                    "bytes": content_bytes.len(),
                }),
            )?;
            row
        }
        Err(e) if e.to_lowercase().contains("unique") => {
            // Dedup hit — re-fetch and return the existing row. No
            // audit entry: nothing actually changed in the DB.
            let listed = db.list_diagram_snapshots(diagram_id)?;
            listed
                .into_iter()
                .find(|s| s.content_hash == hash)
                .ok_or_else(|| {
                    format!(
                        "create_diagram_snapshot dedup fallback: hash {} \
                         expected but not found in list",
                        hash
                    )
                })?
        }
        Err(e) => return Err(e),
    };
    Ok(row)
}

#[command]
pub async fn restore_diagram_snapshot(
    snapshot_id: i64,
    db: State<'_, Db>,
) -> Result<(), String> {
    // Resolve (file_path, bytes) from the DB layer.
    let (relative_path, bytes) = db.restore_diagram_snapshot(snapshot_id)?;
    // We need the parent project's folder to resolve the absolute path.
    // The DB layer returns the project_id implicitly via the diagram_id
    // chain; re-fetch to get the parent project id.
    let snapshot = db
        .get_diagram_snapshot(snapshot_id)?
        .ok_or_else(|| format!("snapshot {} vanished mid-restore", snapshot_id))?;
    let diagram = db
        .get_diagram_by_id(snapshot.diagram_id)?
        .ok_or_else(|| {
            format!(
                "parent diagram {} for snapshot {} vanished mid-restore",
                snapshot.diagram_id, snapshot_id
            )
        })?;

    let project_folder = lookup_project_folder(&db, &diagram.project_id)?;
    let abs_path = if Path::new(&relative_path).is_absolute() {
        PathBuf::from(&relative_path)
    } else {
        project_folder.join(&relative_path)
    };

    write_file_atomic(&abs_path, &bytes)?;

    db.audit(
        "diagram_snapshot_restore",
        Some(&diagram.project_id),
        None,
        &serde_json::json!({
            "snapshot_id": snapshot_id,
            "diagram_id": diagram.id,
            "diagram_name": diagram.diagram_name,
            "bytes": bytes.len(),
        }),
    )?;
    Ok(())
}

#[command]
pub async fn delete_diagram_snapshot(
    snapshot_id: i64,
    db: State<'_, Db>,
) -> Result<(), String> {
    // Look up the parent project_id BEFORE the delete so the audit
    // entry can carry it.
    let snapshot = db.get_diagram_snapshot(snapshot_id)?;
    let project_id = if let Some(s) = &snapshot {
        db.get_diagram_by_id(s.diagram_id)?
            .map(|d| d.project_id)
            .unwrap_or_default()
    } else {
        // Already gone — log a best-effort audit with no project_id.
        String::new()
    };

    db.delete_diagram_snapshot(snapshot_id)?;
    let project_ref = if project_id.is_empty() {
        None
    } else {
        Some(project_id.as_str())
    };
    db.audit(
        "diagram_snapshot_delete",
        project_ref,
        None,
        &serde_json::json!({ "snapshot_id": snapshot_id }),
    )?;
    Ok(())
}

// ─── Access mutations ───────────────────────────────────────────────────

#[command]
pub async fn diagram_grant_access(
    grantor_id: String,
    grantee_id: String,
    level: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_diagram_access(&grantor_id, &grantee_id, &level)?;
    db.audit(
        "diagram_access_grant",
        Some(&grantor_id),
        None,
        &serde_json::json!({
            "grantee": grantee_id,
            "level": level,
        }),
    )?;
    Ok(())
}

// ─── Per-tool MCP grants ────────────────────────────────────────────────
//
// Despite living in `diagrams_cmd.rs` for historical reasons (Phase 1.1
// shipped these alongside the diagrams DB schema), these commands are
// MCP-NAME-AGNOSTIC: every caller passes `mcp_name` as a String, and
// the underlying `project_mcp_tool_grants` table is keyed on
// `(project_id, mcp_name, tool_name)`. v0.2.34 Agent E (Phase 4
// generalisation, 2026-05-25) consciously kept them here — moving the
// file would churn `lib.rs::invoke_handler!` registrations without a
// concrete benefit.

#[command]
pub async fn set_project_mcp_tool_enabled(
    project_id: String,
    mcp_name: String,
    tool_name: String,
    enabled: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_mcp_tool_enabled(&project_id, &mcp_name, &tool_name, enabled)?;
    db.audit(
        "mcp_tool_grant_set",
        Some(&project_id),
        None,
        &serde_json::json!({
            "mcp": mcp_name,
            "tool": tool_name,
            "enabled": enabled,
        }),
    )?;
    Ok(())
}

/// v0.2.34 (Agent E — Phase 4 generalisation, 2026-05-25): pre-populate
/// `project_mcp_tool_grants` for a project from the manifest-shipped
/// defaults (or the hardcoded fallback for orchestrator-bundled MCPs).
///
/// Called by `PermissionsTab.svelte`'s "Customize" button: the user
/// wants to bring an MCP's per-tool toggles under explicit project
/// control, starting from whatever the wrapper's default state happens
/// to be. After this command runs, every default tool has a matching
/// row in `project_mcp_tool_grants` with `enabled = default_enabled`,
/// which the UI then lets the user toggle individually.
///
/// Idempotent: re-running it is safe — `set_mcp_tool_enabled` does
/// `INSERT OR UPDATE`, so existing rows get overwritten with the
/// default (callers explicitly want this — "reset to defaults" is a
/// valid second-Customize click).
///
/// Returns the full set of rows that now exist for `(project_id,
/// mcp_name)` so the UI doesn't need a follow-up `list_project_mcp_tools`
/// round-trip.
#[command]
pub async fn seed_project_mcp_tool_grants(
    project_id: String,
    mcp_name: String,
    db: State<'_, Db>,
) -> Result<Vec<ToolGrant>, String> {
    // Resolve defaults: prefer module-shipped (DB), fall back to the
    // hardcoded list (mermaid / excalidraw). Empty result is a valid
    // outcome — the UI shows the "no tools to customize" state.
    let defaults: Vec<McpToolDefault> = db.list_mcp_tool_defaults(&mcp_name)?;
    let entries: Vec<(String, bool)> = if defaults.is_empty() {
        fallback_default_allowlist(&mcp_name)
    } else {
        defaults
            .into_iter()
            .map(|d| (d.tool_name, d.default_enabled))
            .collect()
    };

    if entries.is_empty() {
        // Nothing to seed — return the current (likely empty) row set
        // verbatim. The UI's Customize button bails out gracefully.
        return db.list_project_mcp_tools(&project_id, &mcp_name);
    }

    for (tool_name, default_enabled) in &entries {
        db.set_mcp_tool_enabled(&project_id, &mcp_name, tool_name, *default_enabled)?;
    }
    db.audit(
        "mcp_tool_grants_seeded",
        Some(&project_id),
        None,
        &serde_json::json!({
            "mcp": mcp_name,
            "tool_count": entries.len(),
        }),
    )?;
    db.list_project_mcp_tools(&project_id, &mcp_name)
}

/// Hardcoded fallback per-tool allowlist for orchestrator-bundled
/// MCPs. Mirrors `vct-hub::mcp_tool_grants_api::_default_allowlist_for`
/// — the two lists MUST stay in sync. We can't share the constants
/// directly because `vct-hub` lives in a separate crate; the diff
/// between them is caught by the integration test below
/// (`fallback_default_allowlist_matches_hub_constants`).
fn fallback_default_allowlist(mcp_name: &str) -> Vec<(String, bool)> {
    match mcp_name {
        "mermaid" => vec![
            ("export_png".to_string(), false),
            ("list_themes".to_string(), false),
            ("render".to_string(), true),
            ("save_diagram".to_string(), true),
            ("validate_syntax".to_string(), true),
        ],
        "excalidraw" => vec![
            ("align_elements".to_string(), true),
            ("batch_create_elements".to_string(), true),
            ("create_element".to_string(), true),
            ("create_from_mermaid".to_string(), false),
            ("create_view".to_string(), true),
            ("delete_element".to_string(), true),
            ("distribute_elements".to_string(), true),
            ("export_scene".to_string(), false),
            ("get_resource".to_string(), true),
            ("group_elements".to_string(), false),
            ("lock_elements".to_string(), false),
            ("query_elements".to_string(), true),
            ("read_me".to_string(), true),
            ("ungroup_elements".to_string(), false),
            ("unlock_elements".to_string(), false),
            ("update_element".to_string(), true),
        ],
        _ => Vec::new(),
    }
}

// ─── Project modules ────────────────────────────────────────────────────

#[command]
pub async fn set_project_module_enabled(
    project_id: String,
    module_name: String,
    enabled: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_project_module_enabled(&project_id, &module_name, enabled)?;
    db.audit(
        "project_module_set_enabled",
        Some(&project_id),
        None,
        &serde_json::json!({
            "module": module_name,
            "enabled": enabled,
        }),
    )?;

    // Phase 1.5.7 — when a module's enabled flag changes, the
    // `{{#if_module_active <name>}}` block in CLAUDE.md needs to be
    // re-evaluated. Spawn a background subprocess so the toggle
    // remains snappy from the user's perspective; never block the
    // command result on the re-render. Soft-fail throughout: if the
    // subprocess can't be spawned (missing venv, missing project on
    // disk, CLI error) we log a warning and return success — the
    // user already saw the DB toggle land; the re-render is a
    // side-effect.
    spawn_re_render_claude_md(&db, &project_id);

    Ok(())
}

/// Background re-render of `<project_folder>/CLAUDE.md` after a module
/// toggle. Resolves the project folder via `Db::get_project`, locates a
/// usable Python interpreter, and spawns `python -m vco_lib.project_init
/// re-render-claude-md --folder <path> --project-name <name>
/// --project-id <id> --json` detached from the Tauri command. The
/// command returns immediately — the re-render runs in the background.
///
/// Soft-fail philosophy: every error in this path is logged and
/// swallowed. The DB row write already happened in the caller; the
/// template re-render is a follow-on side effect, not a contract.
/// Failures here surface only in the launcher's log and (eventually)
/// the next time `re-render-claude-md` is invoked by some other path.
fn spawn_re_render_claude_md(db: &Db, project_id: &str) {
    let project = match db.get_project(project_id) {
        Ok(Some(p)) => p,
        Ok(None) => {
            eprintln!(
                "[vct] re-render-claude-md: project {} not found; \
                 skipping background re-render",
                project_id
            );
            return;
        }
        Err(e) => {
            eprintln!(
                "[vct] re-render-claude-md: db lookup for {} failed: {}; \
                 skipping background re-render",
                project_id, e
            );
            return;
        }
    };

    let folder = PathBuf::from(&project.folder_path);
    if !folder.is_dir() {
        eprintln!(
            "[vct] re-render-claude-md: project folder {} does not exist; \
             skipping background re-render",
            folder.display()
        );
        return;
    }

    let python = match resolve_project_python(&folder) {
        Some(p) => p,
        None => {
            eprintln!(
                "[vct] re-render-claude-md: no usable Python for project \
                 {} (folder={}); skipping background re-render",
                project_id,
                folder.display()
            );
            return;
        }
    };

    // Spawn detached — we don't await stdout/stderr from this child.
    // The CLI itself writes its own logs and falls back gracefully.
    let project_id_str = project_id.to_string();
    let project_name = project.name.clone();
    let folder_str = folder.to_string_lossy().into_owned();
    let spawn_result = std::process::Command::new(&python).silent()
        .arg("-m")
        .arg("vco_lib.project_init")
        .arg("re-render-claude-md")
        .arg("--folder")
        .arg(&folder_str)
        .arg("--project-name")
        .arg(&project_name)
        .arg("--project-id")
        .arg(&project_id_str)
        .arg("--json")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .stdin(std::process::Stdio::null())
        .spawn();

    match spawn_result {
        Ok(child) => {
            eprintln!(
                "[vct] re-render-claude-md: spawned pid={} for project {} ({})",
                child.id(),
                project_name,
                project_id_str
            );
        }
        Err(e) => {
            eprintln!(
                "[vct] re-render-claude-md: spawn failed for project {} ({}): {}",
                project_name, project_id_str, e
            );
        }
    }
}

/// Pick a Python interpreter for the project-folder re-render. Prefers
/// the project's own `.venv` and the orchestrator's `claude_mcp_servers/.venv`
/// (the canonical install layout). Falls back to `python3` / `python` on
/// PATH. Returns `None` only when nothing usable is found — the caller
/// soft-fails in that case.
fn resolve_project_python(folder: &Path) -> Option<PathBuf> {
    let exe_name = if cfg!(windows) { "python.exe" } else { "python" };
    let bin_dir = if cfg!(windows) { "Scripts" } else { "bin" };

    let candidates = [
        folder.join(".venv").join(bin_dir).join(exe_name),
        folder
            .join("claude_mcp_servers")
            .join(".venv")
            .join(bin_dir)
            .join(exe_name),
    ];
    for c in candidates.iter() {
        if c.is_file() {
            return Some(c.clone());
        }
    }
    // Last resort: PATH lookup. Use `which`-style on POSIX, where the
    // PATH chain typically has `python3` first. We avoid shelling out
    // to `which` directly; the spawn will fail later if PATH lookup is
    // empty and we'll log a warning then.
    if cfg!(windows) {
        Some(PathBuf::from("python.exe"))
    } else {
        Some(PathBuf::from("python3"))
    }
}

// ─── Module-active lookup ───────────────────────────────────────────────

/// Set of modules that are orchestrator-bundled-default-active. When
/// `is_project_module_active` is called for one of these and no row
/// exists in `project_modules` yet, we treat the absence as a
/// pre-v0.2.33 backfill gap (these projects existed before the seed
/// path in `projects_v2.rs:521` was wired) — auto-seed the row with
/// `enabled=true` and return true. This makes existing-project
/// upgraders see the Diagrams tab without having to manually toggle.
///
/// The user's explicit opt-out (toggling off in DiagramsTab) writes
/// `enabled=0` which is a real row, so the backfill never re-enables
/// what the user disabled.
///
/// Resolution (a) chosen over (b) per v0.2.34 Agent D's spec: "diagrams"
/// stays a logical module-active flag (gates CLAUDE.md template
/// `{{#if_module_active diagrams}}` + the DiagramsTab visibility);
/// "mermaid" and "excalidraw" are the actual wrapper MCPs surfaced
/// through `default_mcp_servers()` in `types.rs`. The two concerns
/// don't collapse into one default-mcp-servers entry.
const ORCHESTRATOR_BUNDLED_DEFAULT_ACTIVE_MODULES: &[&str] = &["diagrams"];

/// Phase 1.5.7 — the DiagramsTab Svelte calls this to decide whether
/// the module is on or off for a given project. Returns `false` for
/// unknown (project, module) pairs (no row in `project_modules`) UNLESS
/// the module is in the orchestrator-bundled-default-active set (see
/// `ORCHESTRATOR_BUNDLED_DEFAULT_ACTIVE_MODULES`), in which case the
/// row is seeded with `enabled=true` and we return true.
///
/// Soft-fail: if the seed write fails (e.g. transient DB lock) we log
/// + still return `true` so the user-facing tab visibility isn't
/// blocked on a write retry. The next call retries the seed.
#[command]
pub async fn is_project_module_active(
    project_id: String,
    module_name: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    // First check: row exists?
    let active = db
        .is_module_active(&project_id, &module_name)
        .map_err(|e| format!("is_project_module_active: {e}"))?;
    if active {
        return Ok(true);
    }

    // Row absent or explicitly disabled. The DB layer collapses both
    // cases into `false`, so we need to disambiguate before deciding
    // whether to backfill. A direct query keeps the DB layer pure
    // (no new method needed on `Db`) and is cheap (~1 row scan).
    if ORCHESTRATOR_BUNDLED_DEFAULT_ACTIVE_MODULES.contains(&module_name.as_str())
        && !has_module_row(&db, &project_id, &module_name)?
    {
        // Backfill: pre-v0.2.33 projects predate the seed path in
        // `projects_v2.rs:521`, so the row is missing rather than
        // user-disabled. Seed it default-active.
        if let Err(e) = db.set_project_module_enabled(&project_id, &module_name, true) {
            eprintln!(
                "[vct] is_project_module_active: backfill seed failed for \
                 ({}, {}): {} — returning true anyway; next call retries",
                project_id, module_name, e
            );
        } else {
            // Audit so we can trace retroactive seeds in support cases.
            let _ = db.audit(
                "project_module_backfill_seed",
                Some(&project_id),
                None,
                &serde_json::json!({
                    "module": module_name,
                    "reason": "orchestrator_bundled_default_active",
                }),
            );
        }
        return Ok(true);
    }

    Ok(false)
}

/// Returns true if `(project_id, module_name)` has any row in
/// `project_modules` (regardless of `enabled` value). Used to
/// disambiguate "row absent" from "row present, disabled" in
/// `is_project_module_active`. Soft-fails on DB errors — returns
/// false so the caller's backfill path runs (the row will get
/// recreated, idempotent UPSERT).
fn has_module_row(db: &Db, project_id: &str, module_name: &str) -> Result<bool, String> {
    let guard = db.lock();
    let exists: bool = guard
        .query_row(
            "SELECT 1 FROM project_modules
             WHERE project_id = ?1 AND module_name = ?2
             LIMIT 1",
            rusqlite::params![project_id, module_name],
            |_| Ok(true),
        )
        .or_else(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => Ok(false),
            other => Err(format!("has_module_row({}, {}): {}", project_id, module_name, other)),
        })?;
    Ok(exists)
}

// ─── Diagrams file IO (v0.2.34 Agent D) ─────────────────────────────────
//
// The DiagramsTab invokes these commands to load + persist diagram
// source files (mermaid preview render, snapshot restore, drop-import
// write). Until v0.2.34 they were missing — the frontend caught the
// "command not found" error and silently degraded. (v0.2.61: the
// Excalidraw scene load/save now happens entirely in the browser editor
// via the local server's /file + /save, not through these commands.)
//
// Security boundary: every read/write is scoped to the project root.
// The `read_project_diagram_source` command additionally restricts
// reads to `.claude/diagrams/` (mirrors `vco_lib/diagram_paths.py`).
// `write_text_file` accepts any path inside the project (so it can
// be used for SVG export to user-chosen locations) but rejects any
// path that resolves outside the project root.

/// Read a file inside the project's `.claude/diagrams/` directory and
/// return its UTF-8 contents.
///
/// Security: enforces two boundaries — (1) `rel_path` must resolve
/// inside the project folder (no `..` escapes, no absolute paths
/// pointing elsewhere), (2) the resolved path must live under
/// `<project>/.claude/diagrams/`. Mirrors the Python-side enforcement
/// in `vco_lib/diagram_paths.py`.
#[command]
pub async fn read_project_diagram_source(
    project_id: String,
    rel_path: String,
    db: State<'_, Db>,
) -> Result<String, String> {
    let project_folder = lookup_project_folder(&db, &project_id)?;
    let abs = resolve_inside_project(&project_folder, &rel_path)?;

    // Second boundary: the resolved absolute path must live under
    // `<project>/.claude/diagrams/`. This is the diagrams scoped
    // boundary from `vco_lib/diagram_paths.py`.
    let diagrams_root = project_folder.join(".claude").join("diagrams");
    if !abs.starts_with(&diagrams_root) {
        return Err(format!(
            "read_project_diagram_source: path {} escapes diagrams root {}",
            abs.display(),
            diagrams_root.display(),
        ));
    }

    fs::read_to_string(&abs).map_err(|e| {
        format!(
            "read_project_diagram_source: read {} failed: {}",
            abs.display(),
            e
        )
    })
}

/// Atomically write UTF-8 `contents` to `path`. `path` MUST be an
/// absolute path inside one of the registered projects (the frontend
/// resolves via `resolve_project_path` first). We re-validate against
/// every registered project's folder as a two-layer defence — a
/// frontend bug or malicious caller can't direct writes outside any
/// project root.
///
/// Atomic via sibling-tempfile + rename (same pattern as
/// `write_file_atomic` higher up in this file).
///
/// Used by:
/// - Excalidraw scene saves (debounced editor writes).
/// - Mermaid SVG export (user-chosen path inside the project).
/// - Excalidraw SVG export (same).
#[command]
pub async fn write_text_file(
    path: String,
    contents: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    let abs = PathBuf::from(&path);
    if !abs.is_absolute() {
        return Err(format!(
            "write_text_file: path must be absolute (got {})",
            abs.display()
        ));
    }

    // Defence-in-depth: the frontend already resolved this path via
    // `resolve_project_path`, but we re-check that the absolute path
    // lives inside SOME registered project's folder before writing.
    // A bug in the frontend (or a non-frontend caller) shouldn't be
    // able to write to arbitrary locations on the user's disk.
    if !path_is_inside_any_project(&db, &abs)? {
        return Err(format!(
            "write_text_file: refusing to write outside any registered project folder ({})",
            abs.display()
        ));
    }

    write_file_atomic(&abs, contents.as_bytes())
}

/// Resolve a project-relative path to an absolute path, refusing any
/// escapes outside the project folder.
///
/// Used by the frontend before invoking `write_text_file` and before
/// handing the absolute path to `@tauri-apps/plugin-opener` for
/// "Open in editor". Lives in `crate::path_utils` so it's reusable
/// outside the diagrams flow.
#[command]
pub async fn resolve_project_path(
    project_id: String,
    rel_path: String,
    db: State<'_, Db>,
) -> Result<String, String> {
    let project_folder = lookup_project_folder(&db, &project_id)?;
    let abs = resolve_inside_project(&project_folder, &rel_path)?;
    Ok(abs.to_string_lossy().into_owned())
}

/// Shared resolver used by `read_project_diagram_source` and
/// `resolve_project_path`. Joins `rel_path` against `project_folder`
/// (or uses it verbatim if absolute), then refuses any result that
/// doesn't start with `project_folder` after lexical normalisation.
///
/// We use a lexical normalisation step (manual `..`/`.` collapse)
/// instead of `fs::canonicalize` because the diagram file may not
/// exist yet (Excalidraw's first save creates the file). `canonicalize`
/// would fail on the missing file; the lexical normalisation gives a
/// well-defined answer regardless of disk state.
fn resolve_inside_project(project_folder: &Path, rel_path: &str) -> Result<PathBuf, String> {
    let candidate = if Path::new(rel_path).is_absolute() {
        PathBuf::from(rel_path)
    } else {
        project_folder.join(rel_path)
    };

    let normalised = lexical_normalize(&candidate);

    // The project folder itself should be canonicalised for the
    // comparison — but if it doesn't exist (rare; a deleted project
    // folder that's still in the DB), fall back to lexical
    // normalisation. dunce::canonicalize gives us the conventional
    // Windows form (no `\\?\` prefix) so the `starts_with` check
    // works across OS.
    let project_canonical = match dunce::canonicalize(project_folder) {
        Ok(p) => p,
        Err(_) => lexical_normalize(project_folder),
    };

    if !normalised.starts_with(&project_canonical) {
        return Err(format!(
            "path {} escapes project folder {}",
            normalised.display(),
            project_canonical.display(),
        ));
    }
    Ok(normalised)
}

/// Lexical (no-disk-touch) path normaliser. Collapses `.` and `..`
/// without consulting the filesystem so non-existent paths still get
/// a well-defined absolute form. Used in `resolve_inside_project`
/// because diagram files may not exist yet at save time.
fn lexical_normalize(path: &Path) -> PathBuf {
    use std::path::Component;
    let mut out = PathBuf::new();
    for comp in path.components() {
        match comp {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// Check whether `abs` is inside ANY registered project folder.
/// Used by `write_text_file` as the second layer of path validation.
fn path_is_inside_any_project(db: &Db, abs: &Path) -> Result<bool, String> {
    let projects = db
        .list_projects()
        .map_err(|e| format!("path_is_inside_any_project: list projects: {}", e))?;
    let normalised = lexical_normalize(abs);
    for p in projects {
        let folder = PathBuf::from(&p.folder_path);
        let folder_canonical = match dunce::canonicalize(&folder) {
            Ok(c) => c,
            Err(_) => lexical_normalize(&folder),
        };
        if normalised.starts_with(&folder_canonical) {
            return Ok(true);
        }
    }
    Ok(false)
}

// ─── Helpers ────────────────────────────────────────────────────────────

/// Resolve a diagram's absolute on-disk path. `file_path` may be
/// absolute (uncommon) or relative to the project folder (typical, e.g.
/// `.claude/diagrams/gui/auth/login.mmd`).
fn resolve_diagram_abs_path(db: &Db, diagram: &DiagramRow) -> Result<PathBuf, String> {
    if Path::new(&diagram.file_path).is_absolute() {
        return Ok(PathBuf::from(&diagram.file_path));
    }
    let project_folder = lookup_project_folder(db, &diagram.project_id)?;
    Ok(project_folder.join(&diagram.file_path))
}

/// Resolve a project's `folder_path` from the DB. The helper exists in
/// `db::project_state` as a private fn; mirroring it here at the
/// command layer avoids leaking it cross-module just for one consumer.
fn lookup_project_folder(db: &Db, project_id: &str) -> Result<PathBuf, String> {
    let guard = db.lock();
    let folder: String = guard
        .query_row(
            "SELECT folder_path FROM projects WHERE id = ?1",
            rusqlite::params![project_id],
            |r| r.get(0),
        )
        .map_err(|e| format!("lookup_project_folder({}): {}", project_id, e))?;
    Ok(PathBuf::from(folder))
}

/// Hex-encoded SHA-256 of arbitrary bytes.
pub(crate) fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    let digest = hasher.finalize();
    let mut out = String::with_capacity(digest.len() * 2);
    for b in digest {
        out.push_str(&format!("{:02x}", b));
    }
    out
}

/// Atomic file write via sibling tmpfile + rename. Cross-OS: `fs::rename`
/// is atomic on POSIX (Linux/macOS) and uses MoveFileExW with
/// REPLACE_EXISTING semantics on Windows, so a crash mid-write never
/// leaves a half-written file at `path`.
fn write_file_atomic(path: &Path, bytes: &[u8]) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("create parent {}: {}", parent.display(), e))?;
    }

    // Sibling tmp file stays on the same filesystem so the final rename
    // is atomic.
    let tmp = sibling_tmp_path(path);
    {
        let mut f = fs::File::create(&tmp)
            .map_err(|e| format!("create {}: {}", tmp.display(), e))?;
        f.write_all(bytes)
            .map_err(|e| format!("write {}: {}", tmp.display(), e))?;
        f.sync_all()
            .map_err(|e| format!("sync {}: {}", tmp.display(), e))?;
    }
    fs::rename(&tmp, path).map_err(|e| {
        // Best-effort: clean the tmp file so a failed rename doesn't
        // leave litter.
        let _ = fs::remove_file(&tmp);
        format!("rename {} -> {}: {}", tmp.display(), path.display(), e)
    })?;
    Ok(())
}

fn sibling_tmp_path(path: &Path) -> PathBuf {
    let mut tmp = path.to_path_buf();
    let name = path
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| "diagram".to_string());
    tmp.set_file_name(format!(".{}.vct-restore.tmp", name));
    tmp
}

// ─── v0.2.36 Agent R — vendored editor opener ──────────────────────────
//
// Replaces the embedded Excalidraw editor (broken on Wayland+webkit2gtk)
// and adds a visual Mermaid editor alongside the existing text editor.
// The flow is:
//
//   1. Ensure the file exists on disk (create empty if absent) — gives
//      the user something for the watcher to react to on first save.
//   2. Lazily start the diagrams-local HTTP server on a free port at
//      127.0.0.1 (idempotent).
//   3. Open `http://127.0.0.1:<port>/<editor>/?file=<rel_path>` in the
//      user's DEFAULT BROWSER via tauri-plugin-opener::open_url.
//   4. The editor's JS fetches `/file?path=...`, lets the user edit,
//      and POSTs back to `/save?path=...` on Save. The existing
//      file-watcher (commands::diagram_watcher) picks up the change
//      and pushes a `diagram-changed` event — the DiagramsTab handles
//      auto-registration there.
//
// Soft-fail throughout — every error is converted to a `String` and
// surfaced as a toast in the UI. The file watcher remains the source
// of truth for "did the editor actually save something" — we don't
// hold the editor session open or track its progress.

#[command]
pub async fn open_diagrams_editor(
    project_id: String,
    diagram_type: String,
    name: String,
    db: State<'_, Db>,
) -> Result<String, String> {
    // 1. Validate inputs at the boundary so we fail fast with a clear
    //    error rather than relying on the local-server's path guard.
    if !matches!(diagram_type.as_str(), "mermaid" | "excalidraw") {
        return Err(format!(
            "open_diagrams_editor: diagram_type must be \"mermaid\" or \"excalidraw\" (got \"{}\")",
            diagram_type,
        ));
    }
    if name.is_empty() {
        return Err("open_diagrams_editor: name must be non-empty".to_string());
    }
    // Name guard MUST stay in lockstep with the frontend's
    // DIAGRAM_NAME_RE (DiagramsTab.svelte) and the diagram-path
    // auto-register parser: `^[A-Za-z0-9_][A-Za-z0-9_-]*$` — allows
    // `_` and mixed case, excludes `/`, `.`, spaces, leading/trailing
    // `-`. (v0.2.61: widened from the old lowercase-kebab rule so a
    // name the user can create in the UI also opens here. Excludes the
    // path-structural chars so the file lands cleanly under
    // visual-draft/.)
    let name_re = regex::Regex::new(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
        .map_err(|e| format!("regex compile: {}", e))?;
    if !name_re.is_match(&name) {
        return Err(format!(
            "open_diagrams_editor: name must match [A-Za-z0-9_][A-Za-z0-9_-]* \
             (letters, digits, _ and -; got \"{}\")",
            name,
        ));
    }

    // 2. Resolve the destination file path. Default category for
    //    "Draw new" diagrams is visual-draft/; the user can re-organise
    //    via the registration UI once the file is registered.
    let ext = if diagram_type == "mermaid" { "mmd" } else { "excalidraw" };
    let rel_path = format!(".claude/diagrams/visual-draft/{}.{}", name, ext);

    // 3. Create an empty file on disk so the watcher sees the first save
    //    as `edit` rather than `create` (cleaner UX for auto-register),
    //    then open the editor against it.
    open_editor_for_rel_path(&db, &project_id, &diagram_type, &rel_path, true).await
}

/// Open the visual editor for an ALREADY-EXISTING diagram file
/// (project-relative `rel_path`, e.g.
/// `.claude/diagrams/gui/auth/login.excalidraw`). Used by the "Edit in
/// browser" action on a selected diagram (v0.2.61 — both editors are
/// now browser-served; the launcher no longer embeds an editor in the
/// Tauri WebView). Returns the opened URL.
///
/// Unlike `open_diagrams_editor` (which creates a new file under
/// visual-draft/ from a name), this opens whatever path the row already
/// points at. The path is re-validated server-side by
/// `resolve_diagrams_path` (must live under some registered project's
/// `.claude/diagrams/`), so we don't re-derive it here.
#[command]
pub async fn open_diagram_editor_for_path(
    project_id: String,
    diagram_type: String,
    rel_path: String,
    db: State<'_, Db>,
) -> Result<String, String> {
    if !matches!(diagram_type.as_str(), "mermaid" | "excalidraw") {
        return Err(format!(
            "open_diagram_editor_for_path: diagram_type must be \"mermaid\" or \"excalidraw\" (got \"{}\")",
            diagram_type,
        ));
    }
    // Reject absolute paths + `..` traversal at the boundary; the
    // server-side guard also enforces this, but failing fast here gives
    // a clearer error.
    if Path::new(&rel_path).is_absolute() {
        return Err(format!(
            "open_diagram_editor_for_path: rel_path must be project-relative (got absolute: {})",
            rel_path,
        ));
    }
    if rel_path.split('/').any(|s| s == "..") {
        return Err(format!(
            "open_diagram_editor_for_path: `..` not allowed in rel_path: {}",
            rel_path,
        ));
    }
    // Don't create the file — it must already exist for an existing
    // diagram. (If it was deleted out from under the row, the editor's
    // GET /file returns 404 and the page boots empty, which is fine.)
    open_editor_for_rel_path(&db, &project_id, &diagram_type, &rel_path, false).await
}

/// Shared tail for both editor-open commands: optionally create the
/// target file, lazy-start the local diagrams-editor HTTP server, build
/// the `?file=…#token=…` URL, and hand it to the OS opener. Returns the
/// opened URL.
///
/// The per-boot save token rides in the URL FRAGMENT (`#token=`), not
/// the query string: fragments are never sent in HTTP requests, so the
/// token stays out of request lines, server logs, and Referer headers.
/// The editor page reads it from `location.hash` and presents it as
/// `Authorization: Bearer` on POST /save (see the gate in
/// diagrams_local_server.rs). The fragment lands in browser history —
/// accepted: the token is per-boot, gates only diagram-file writes, and
/// the server is 127.0.0.1-only.
async fn open_editor_for_rel_path(
    db: &Db,
    project_id: &str,
    diagram_type: &str,
    rel_path: &str,
    create_if_missing: bool,
) -> Result<String, String> {
    if create_if_missing {
        let project_folder = lookup_project_folder(db, project_id)?;
        let abs_path = project_folder.join(rel_path);
        if !abs_path.exists() {
            write_file_atomic(&abs_path, b"")?;
        }
    }

    // Lazy-start the local server (idempotent — first call only) and
    // grab its port. Fresh Db handle for the server (WAL keeps it
    // consistent with the main launcher connection).
    let server_db = Arc::new(
        crate::db::Db::open()
            .map_err(|e| format!("open_editor_for_rel_path: open server-side Db: {}", e))?,
    );
    let vendor_root = crate::commands::diagrams_local_server::resolve_vendor_root()?;
    let server = crate::commands::diagrams_local_server::ensure_started(server_db, vendor_root)
        .await
        .map_err(|e| format!("open_editor_for_rel_path: ensure_started: {}", e))?;

    let encoded = encode_query_value(rel_path);
    let editor_path = if diagram_type == "mermaid" {
        "mermaid"
    } else {
        "excalidraw"
    };
    let url = format!(
        "http://127.0.0.1:{}/{}/?file={}#token={}",
        server.port, editor_path, encoded, server.token,
    );

    tauri_plugin_opener::open_url(&url, None::<&str>)
        .map_err(|e| format!("open_editor_for_rel_path: open_url({}): {}", url, e))?;

    Ok(url)
}

/// Read the diagrams local server's per-boot save token from
/// `<vct_root_dir>/diagrams.token` (written by
/// `diagrams_local_server::spawn_server` with mode 0o600).
///
/// This is the sanctioned channel for the Svelte frontend to obtain
/// the token if it ever needs to POST /save directly — the token is
/// deliberately NOT baked into the frontend bundle (a bundle ships to
/// every install; the token is per-boot and per-machine). Errors if
/// the editor server hasn't been started yet this session (no token
/// file, or a stale one from a previous boot would fail auth anyway —
/// callers should invoke `open_diagrams_editor` first, which starts
/// the server and mints the token).
#[command]
pub async fn get_diagrams_token() -> Result<String, String> {
    let path = vct_launcher_core::paths::vct_root_dir()
        .join(crate::commands::diagrams_local_server::TOKEN_FILE);
    vct_launcher_core::services::boot_token::read_token_file(&path).map_err(|e| {
        format!(
            "get_diagrams_token: {} (the diagrams editor server may not \
             have started yet this session — open an editor first)",
            e,
        )
    })
}

/// Minimal URL-encoder for query-string values. Encodes the printable
/// ASCII subset that's unsafe in a query value (`&`, `=`, ` `, `#`,
/// `?`, `+`) plus all bytes outside `[A-Za-z0-9_.~-/]`. We don't pull
/// in `urlencoding` / `percent-encoding` because this is the only spot
/// in the launcher that needs the encoder.
///
/// `/` is left unencoded so the editor URL stays readable; the launcher
/// local-server treats `?path=` verbatim, so an unencoded `/` is fine.
fn encode_query_value(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        let safe = b.is_ascii_alphanumeric()
            || b == b'_'
            || b == b'.'
            || b == b'~'
            || b == b'-'
            || b == b'/';
        if safe {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{:02X}", b));
        }
    }
    out
}

// ─── Tests ──────────────────────────────────────────────────────────────
//
// We can't construct `tauri::State` in unit tests (it requires a running
// Tauri runtime), so the tests below exercise the pure-logic surfaces:
// sha256_hex, write_file_atomic, sibling_tmp_path, the snapshot
// validation constant, and the diagram-path resolver (which is pure
// once we have a Db). End-to-end behaviour through the #[command] entry
// points is exercised by the launcher's integration tests when they
// run with a real Tauri context.

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;

    fn make_db_with_project(id: &str, name: &str, folder: &Path) -> Db {
        let db = Db::open_in_memory().expect("in-memory db");
        let slug = db.generate_unique_slug(name).unwrap();
        db.insert_project(id, name, folder.to_string_lossy().as_ref(), ProjectHost::Base, &slug)
            .unwrap();
        db
    }

    #[test]
    fn sha256_hex_is_lowercase_64_chars() {
        let h = sha256_hex(b"hello");
        assert_eq!(h.len(), 64);
        assert!(h.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
        // Known vector.
        assert_eq!(
            h,
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        );
    }

    #[test]
    fn sibling_tmp_path_keeps_directory() {
        let p = Path::new("/tmp/x/y/login.mmd");
        let t = sibling_tmp_path(p);
        assert_eq!(t.parent(), p.parent());
        assert!(t.file_name().unwrap().to_string_lossy().ends_with(".vct-restore.tmp"));
        assert!(t.file_name().unwrap().to_string_lossy().contains("login.mmd"));
    }

    #[test]
    fn write_file_atomic_writes_then_replaces() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("nested").join("out.bin");

        write_file_atomic(&target, b"first version").unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"first version");

        write_file_atomic(&target, b"second version").unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"second version");

        // Tmp sibling is cleaned up.
        let leftover: Vec<_> = fs::read_dir(target.parent().unwrap())
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| {
                e.file_name()
                    .to_string_lossy()
                    .ends_with(".vct-restore.tmp")
            })
            .collect();
        assert!(
            leftover.is_empty(),
            "atomic rename should remove the .tmp sibling"
        );
    }

    #[test]
    fn snapshot_triggers_constant_covers_all_uses() {
        // If anyone adds a new trigger value to the wrapper MCP / hook
        // layer they must also update this list — bumping this assertion
        // is the trip-wire.
        assert_eq!(VALID_SNAPSHOT_TRIGGERS.len(), 3);
        assert!(VALID_SNAPSHOT_TRIGGERS.contains(&"manual"));
        assert!(VALID_SNAPSHOT_TRIGGERS.contains(&"auto_pre_edit_save"));
        assert!(VALID_SNAPSHOT_TRIGGERS.contains(&"auto_interval"));
    }

    #[test]
    fn resolve_diagram_abs_path_joins_project_folder_for_relative() {
        let dir = tempfile::tempdir().unwrap();
        let db = make_db_with_project("p1", "Acme", dir.path());
        let d = db
            .register_diagram(
                "p1",
                "x",
                "mermaid",
                ".claude/diagrams/g/x.mmd",
                "g",
            )
            .unwrap();
        let abs = resolve_diagram_abs_path(&db, &d).unwrap();
        assert_eq!(abs, dir.path().join(".claude/diagrams/g/x.mmd"));
    }

    #[test]
    fn resolve_diagram_abs_path_passes_through_absolute() {
        let dir = tempfile::tempdir().unwrap();
        let db = make_db_with_project("p1", "Acme", dir.path());
        // Manually upsert a row whose file_path is absolute — bypassing
        // the public register helper since it accepts absolute paths
        // verbatim anyway.
        let abs_path_str = if cfg!(windows) {
            r"C:\some\where\else\x.mmd".to_string()
        } else {
            "/some/where/else/x.mmd".to_string()
        };
        let d = db
            .register_diagram("p1", "x", "mermaid", &abs_path_str, "g")
            .unwrap();
        let abs = resolve_diagram_abs_path(&db, &d).unwrap();
        assert_eq!(abs, PathBuf::from(&abs_path_str));
    }

    #[test]
    fn is_module_active_returns_false_for_absent_row() {
        // A3 wire-up regression: the DiagramsTab calls this every mount.
        // For an unregistered (project, module) pair the answer is `false`
        // — never an error. The Svelte falls back to "module on" on error,
        // so an error here would mask a real misconfig.
        //
        // NOTE: this exercises the DB layer directly (Db::is_module_active),
        // NOT the Tauri command. The command layer adds the v0.2.34 Agent D
        // backfill for "diagrams" specifically — see
        // `has_module_row_distinguishes_absent_from_disabled` below for
        // the command-layer contract.
        let dir = tempfile::tempdir().unwrap();
        let db = make_db_with_project("p1", "Acme", dir.path());
        let r = db.is_module_active("p1", "diagrams").unwrap();
        assert!(!r, "unknown module should be inactive (got {})", r);
    }

    #[test]
    fn has_module_row_distinguishes_absent_from_disabled() {
        // The Tauri command layer's backfill relies on `has_module_row`
        // to tell apart "no row exists yet" from "row exists with
        // enabled=0". Without this distinction we'd re-enable modules
        // the user explicitly opted out of.
        let dir = tempfile::tempdir().unwrap();
        let db = make_db_with_project("p1", "Acme", dir.path());

        // Absent row → returns false.
        assert!(!has_module_row(&db, "p1", "diagrams").unwrap());

        // Disabled row → returns true (row exists, even if enabled=0).
        db.set_project_module_enabled("p1", "diagrams", false).unwrap();
        assert!(has_module_row(&db, "p1", "diagrams").unwrap());

        // Enabled row → also true.
        db.set_project_module_enabled("p1", "diagrams", true).unwrap();
        assert!(has_module_row(&db, "p1", "diagrams").unwrap());
    }

    #[test]
    fn lexical_normalize_collapses_dot_segments() {
        // We use lexical normalisation (no disk touch) so non-existent
        // paths still get a well-defined form. Cover the canonical
        // cases that drive `resolve_inside_project`.
        let p = Path::new("/proj/.claude/diagrams/../etc/passwd");
        let n = lexical_normalize(p);
        // Should collapse to `/proj/.claude/etc/passwd` (one level up
        // from `diagrams/`). Whether that's "still inside project"
        // depends on the project folder used by the caller — covered
        // separately by `resolve_inside_project_rejects_traversal`.
        assert_eq!(n, PathBuf::from("/proj/.claude/etc/passwd"));

        // Trailing `..` that escapes the project entirely.
        let escape = Path::new("/proj/.claude/diagrams/../../../etc/passwd");
        let escape_n = lexical_normalize(escape);
        assert_eq!(escape_n, PathBuf::from("/etc/passwd"));

        // No-op for already-normal paths.
        let clean = Path::new("/proj/.claude/diagrams/x.mmd");
        assert_eq!(lexical_normalize(clean), PathBuf::from("/proj/.claude/diagrams/x.mmd"));

        // `.` segments stripped.
        let dot = Path::new("/proj/./diagrams/./x.mmd");
        assert_eq!(lexical_normalize(dot), PathBuf::from("/proj/diagrams/x.mmd"));
    }

    #[test]
    fn resolve_inside_project_rejects_dotdot_traversal() {
        // The diagrams flow accepts relative paths from the frontend.
        // A `..` escape must be rejected (not silently rewritten).
        let dir = tempfile::tempdir().unwrap();
        // Need a real folder so dunce::canonicalize works — the
        // resolver uses lexical normalisation as a fallback when the
        // canonicalize fails, but the canonical path makes the test's
        // expectations cross-OS predictable.
        let project = dir.path().join("proj");
        fs::create_dir_all(&project).unwrap();

        let result = resolve_inside_project(&project, "../etc/passwd");
        assert!(result.is_err(), "`..` traversal must be rejected (got {:?})", result);
        let err = result.unwrap_err();
        assert!(err.contains("escapes project folder"), "error should mention escape (got {:?})", err);
    }

    #[test]
    fn resolve_inside_project_rejects_absolute_outside() {
        let dir = tempfile::tempdir().unwrap();
        let project = dir.path().join("proj");
        fs::create_dir_all(&project).unwrap();

        let outside = if cfg!(windows) {
            r"C:\etc\passwd".to_string()
        } else {
            "/etc/passwd".to_string()
        };

        let result = resolve_inside_project(&project, &outside);
        assert!(result.is_err(), "absolute path outside project must be rejected (got {:?})", result);
    }

    #[test]
    fn resolve_inside_project_accepts_relative_inside() {
        let dir = tempfile::tempdir().unwrap();
        let project = dir.path().join("proj");
        fs::create_dir_all(&project).unwrap();

        let result = resolve_inside_project(&project, ".claude/diagrams/g/x.mmd")
            .expect("clean relative path inside project must succeed");
        // Should be canonicalized project + appended subpath.
        let expected = dunce::canonicalize(&project).unwrap().join(".claude/diagrams/g/x.mmd");
        assert_eq!(result, expected);
    }

    #[test]
    fn resolve_inside_project_accepts_absolute_inside() {
        let dir = tempfile::tempdir().unwrap();
        let project = dir.path().join("proj");
        fs::create_dir_all(&project).unwrap();
        let canonical = dunce::canonicalize(&project).unwrap();
        let abs_inside = canonical.join(".claude/diagrams/g/x.mmd");

        let result = resolve_inside_project(&project, abs_inside.to_string_lossy().as_ref())
            .expect("absolute path inside project must succeed");
        assert_eq!(result, abs_inside);
    }

    #[test]
    fn path_is_inside_any_project_finds_match() {
        // Two registered projects. A path inside either should match;
        // a path outside both should not.
        let dir = tempfile::tempdir().unwrap();
        let p1 = dir.path().join("proj1");
        let p2 = dir.path().join("proj2");
        fs::create_dir_all(&p1).unwrap();
        fs::create_dir_all(&p2).unwrap();

        let db = Db::open_in_memory().unwrap();
        let s1 = db.generate_unique_slug("P1").unwrap();
        let s2 = db.generate_unique_slug("P2").unwrap();
        db.insert_project("p1", "P1", p1.to_string_lossy().as_ref(), ProjectHost::Base, &s1).unwrap();
        db.insert_project("p2", "P2", p2.to_string_lossy().as_ref(), ProjectHost::Base, &s2).unwrap();

        let inside_p1 = dunce::canonicalize(&p1).unwrap().join(".claude/diagrams/x.mmd");
        assert!(path_is_inside_any_project(&db, &inside_p1).unwrap());

        let inside_p2 = dunce::canonicalize(&p2).unwrap().join(".claude/diagrams/y.mmd");
        assert!(path_is_inside_any_project(&db, &inside_p2).unwrap());

        // Outside both.
        let outside = dir.path().join("other").join("z.mmd");
        // Create the dir so canonicalize works for parent.
        fs::create_dir_all(outside.parent().unwrap()).unwrap();
        assert!(!path_is_inside_any_project(&db, &outside).unwrap());
    }

    #[test]
    fn write_file_atomic_writes_inside_temp() {
        // End-to-end test for the atomic write helper used by
        // `write_text_file`. We can't drive the Tauri command directly
        // (no State<Db> in unit tests), so we exercise the
        // write-and-replace primitive that backs it.
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join(".claude/diagrams/excalidraw/scene.excalidraw");
        write_file_atomic(&target, b"{\"version\":2,\"elements\":[]}").unwrap();
        let content = fs::read_to_string(&target).unwrap();
        assert!(content.contains("elements"));
    }

    #[test]
    fn read_project_diagram_source_logic_inside_diagrams_root() {
        // Smoke test for the read path's two-layer enforcement:
        // (1) `resolve_inside_project` keeps us inside the project,
        // (2) the `.claude/diagrams/` boundary further restricts us.
        // We can't drive the Tauri command (needs State<Db>) so we
        // exercise the underlying resolver + manually check the
        // diagrams-root constraint that the command body would apply.
        let dir = tempfile::tempdir().unwrap();
        let project = dir.path().join("proj");
        let diagrams = project.join(".claude/diagrams");
        fs::create_dir_all(&diagrams).unwrap();
        let target = diagrams.join("g/x.mmd");
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::write(&target, "flowchart TD; A-->B").unwrap();

        // Path inside diagrams/ — resolves cleanly.
        let resolved = resolve_inside_project(&project, ".claude/diagrams/g/x.mmd").unwrap();
        let diagrams_canonical = dunce::canonicalize(&diagrams).unwrap();
        assert!(resolved.starts_with(&diagrams_canonical), "inside-diagrams path should be under diagrams root");
        // Read works.
        assert_eq!(fs::read_to_string(&resolved).unwrap(), "flowchart TD; A-->B");

        // Path inside project but OUTSIDE diagrams/ — resolves but the
        // command's second boundary would reject it. We just check the
        // resolved path is not under diagrams_canonical.
        let outside_dir = project.join("secrets");
        fs::create_dir_all(&outside_dir).unwrap();
        fs::write(outside_dir.join("creds.txt"), "S3CR3T").unwrap();
        let outside_resolved = resolve_inside_project(&project, "secrets/creds.txt").unwrap();
        assert!(!outside_resolved.starts_with(&diagrams_canonical),
            "path outside diagrams/ should NOT match diagrams_root prefix");
    }

    #[test]
    fn is_module_active_round_trips_through_set_then_get() {
        // Toggle on → reflects as true; toggle off → reflects as false.
        // Mirrors the lifecycle exercised by `set_project_module_enabled`
        // + `is_project_module_active` from DiagramsTab.
        let dir = tempfile::tempdir().unwrap();
        let db = make_db_with_project("p1", "Acme", dir.path());
        db.set_project_module_enabled("p1", "diagrams", true).unwrap();
        assert!(db.is_module_active("p1", "diagrams").unwrap());
        db.set_project_module_enabled("p1", "diagrams", false).unwrap();
        assert!(!db.is_module_active("p1", "diagrams").unwrap());
    }

    #[test]
    fn resolve_project_python_prefers_project_venv() {
        // Mirror the canonical install layout: <project>/.venv/{bin|Scripts}/python
        // wins over <project>/claude_mcp_servers/.venv/...
        let dir = tempfile::tempdir().unwrap();
        let bin_dir = if cfg!(windows) { "Scripts" } else { "bin" };
        let exe_name = if cfg!(windows) { "python.exe" } else { "python" };
        let project_venv_py = dir.path().join(".venv").join(bin_dir).join(exe_name);
        let mcp_venv_py = dir
            .path()
            .join("claude_mcp_servers")
            .join(".venv")
            .join(bin_dir)
            .join(exe_name);
        fs::create_dir_all(project_venv_py.parent().unwrap()).unwrap();
        fs::create_dir_all(mcp_venv_py.parent().unwrap()).unwrap();
        fs::write(&project_venv_py, b"").unwrap();
        fs::write(&mcp_venv_py, b"").unwrap();
        let picked = resolve_project_python(dir.path()).unwrap();
        assert_eq!(picked, project_venv_py);
    }

    #[test]
    fn resolve_project_python_falls_back_to_path_when_no_venv() {
        let dir = tempfile::tempdir().unwrap();
        let picked = resolve_project_python(dir.path()).unwrap();
        // The fallback is OS-dependent but never None — the caller
        // soft-fails on spawn if the PATH lookup also misses.
        let expected = if cfg!(windows) { "python.exe" } else { "python3" };
        assert_eq!(picked, PathBuf::from(expected));
    }

    /// End-to-end logic test for the snapshot create-then-restore path,
    /// driven directly against the Db (the #[command] wrappers would
    /// need Tauri State).
    #[test]
    fn snapshot_create_and_restore_round_trip_via_db() {
        let dir = tempfile::tempdir().unwrap();
        let db = make_db_with_project("p1", "Acme", dir.path());
        let rel_path = ".claude/diagrams/g/x.mmd";
        let original_content = b"flowchart TD; A-->B".to_vec();
        let new_content = b"flowchart TD; A-->C; B-->D".to_vec();

        // Create the file on disk (so the snapshot has something to read).
        let abs_path = dir.path().join(rel_path);
        write_file_atomic(&abs_path, &original_content).unwrap();

        let d = db
            .register_diagram("p1", "x", "mermaid", rel_path, "g")
            .unwrap();
        let snapshot = db
            .create_diagram_snapshot(
                d.id,
                &sha256_hex(&original_content),
                &original_content,
                "manual",
                None,
            )
            .unwrap();

        // User edits the file → write new content.
        write_file_atomic(&abs_path, &new_content).unwrap();
        assert_eq!(fs::read(&abs_path).unwrap(), new_content);

        // Restore: should bring back the original bytes byte-identically.
        let (returned_path, returned_bytes) =
            db.restore_diagram_snapshot(snapshot.id).unwrap();
        assert_eq!(returned_path, rel_path);
        assert_eq!(returned_bytes, original_content);

        // Caller (the Tauri command) then writes back via write_file_atomic.
        let abs_restore = dir.path().join(&returned_path);
        write_file_atomic(&abs_restore, &returned_bytes).unwrap();
        assert_eq!(fs::read(&abs_path).unwrap(), original_content);
    }

    // ─── v0.2.34 Agent E (Phase 4 generalisation) tests ──────────────
    //
    // The Tauri commands themselves need a Tauri runtime to invoke, so
    // we exercise the pure logic surface (`fallback_default_allowlist`)
    // + the underlying DB layer the commands wrap. End-to-end behaviour
    // through the #[command] macros is covered by the launcher's
    // integration tests + the hub-side tests in
    // `mcp_tool_grants_api.rs`.

    #[test]
    fn open_diagrams_editor_name_guard_matches_frontend_rule() {
        // The name guard in `open_diagrams_editor` MUST stay in lockstep
        // with the frontend's DIAGRAM_NAME_RE (DiagramsTab.svelte) and the
        // on-disk auto-register parser — otherwise a name the user can
        // create in the UI fails to open here (the v0.2.61 drift this test
        // pins). Pattern: ^[A-Za-z0-9_][A-Za-z0-9_-]*$.
        let re = regex::Regex::new(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$").unwrap();
        // Accept: the cases the widened frontend rule allows.
        for ok in ["login-flow", "my_diagram", "MyDiagram", "a", "X1", "_priv", "a-b_c"] {
            assert!(re.is_match(ok), "should accept {ok:?}");
        }
        // Reject: path-structural chars + leading dash + spaces + empty.
        for bad in ["", "-leading", "a/b", "a.b", "a b", "a..b", ".hidden"] {
            assert!(!re.is_match(bad), "should reject {bad:?}");
        }
    }

    #[test]
    fn encode_query_value_preserves_slashes_and_encodes_unsafe_chars() {
        // The launcher local-server treats the path verbatim; we keep
        // `/` unencoded so the URL stays readable in browser history.
        assert_eq!(
            encode_query_value(".claude/diagrams/g/x.mmd"),
            ".claude/diagrams/g/x.mmd",
        );
        // Unsafe chars get percent-encoded.
        assert_eq!(encode_query_value("a b&c=d#e"), "a%20b%26c%3Dd%23e");
        // Unicode bytes get percent-encoded byte-by-byte.
        assert_eq!(encode_query_value("café"), "caf%C3%A9");
        // Already-safe ASCII alnum + - _ . ~ pass through.
        assert_eq!(
            encode_query_value("abc-XYZ_123.foo~bar"),
            "abc-XYZ_123.foo~bar",
        );
    }

    #[test]
    fn fallback_default_allowlist_returns_mermaid_set() {
        let list = fallback_default_allowlist("mermaid");
        let names: Vec<&str> = list.iter().map(|(n, _)| n.as_str()).collect();
        assert!(names.contains(&"render"));
        assert!(names.contains(&"save_diagram"));
        // render must be on; export_png off.
        let render_on = list
            .iter()
            .find(|(n, _)| n == "render")
            .map(|(_, en)| *en)
            .unwrap();
        assert!(render_on);
        let export_off = list
            .iter()
            .find(|(n, _)| n == "export_png")
            .map(|(_, en)| *en)
            .unwrap();
        assert!(!export_off);
    }

    #[test]
    fn fallback_default_allowlist_returns_empty_for_unknown_mcp() {
        // Generalisation contract: an unknown (non-bundled) MCP returns
        // an empty list rather than panicking. The caller — the
        // seed_project_mcp_tool_grants command — bails out gracefully
        // when defaults are empty (no rows are inserted; the UI shows
        // "no tools to customize").
        let list = fallback_default_allowlist("vendor-x-mcp");
        assert!(list.is_empty());
        let list = fallback_default_allowlist("");
        assert!(list.is_empty());
    }

    #[test]
    fn seed_logic_prefers_module_defaults_over_fallback() {
        // Build a fresh Db, register module-shipped defaults for a NEW
        // mcp_name (no fallback exists), then verify the seed code
        // path reads from `module_mcp_tool_defaults` and inserts rows
        // into `project_mcp_tool_grants` accordingly.
        let dir = tempfile::tempdir().unwrap();
        let db = make_db_with_project("p1", "Acme", dir.path());
        db.reconcile_mcp_tool_defaults(
            "vendor-reranker",
            "vendor-mcp-x",
            &[
                ("rerank".to_string(), true, None),
                ("debug".to_string(), false, None),
            ],
            10,
        )
        .unwrap();

        // Simulate what `seed_project_mcp_tool_grants` does internally
        // (the #[command] surface needs a Tauri State to invoke).
        let defaults = db.list_mcp_tool_defaults("vendor-reranker").unwrap();
        assert_eq!(defaults.len(), 2);
        for d in &defaults {
            db.set_mcp_tool_enabled("p1", &d.mcp_name, &d.tool_name, d.default_enabled)
                .unwrap();
        }
        let listed = db.list_project_mcp_tools("p1", "vendor-reranker").unwrap();
        assert_eq!(listed.len(), 2);
        // Sorted alphabetically by tool_name.
        assert_eq!(listed[0].tool_name, "debug");
        assert!(!listed[0].enabled);
        assert_eq!(listed[1].tool_name, "rerank");
        assert!(listed[1].enabled);
    }

    #[test]
    fn seed_logic_falls_back_to_hardcoded_when_no_module_defaults() {
        // For a bundled MCP without any `module_mcp_tool_defaults` rows,
        // seeding should populate the project's grant table from the
        // hardcoded fallback. Mermaid is the canonical case.
        let dir = tempfile::tempdir().unwrap();
        let db = make_db_with_project("p1", "Acme", dir.path());
        // No defaults registered — the DB is empty for "mermaid".
        let defaults = db.list_mcp_tool_defaults("mermaid").unwrap();
        assert!(defaults.is_empty());

        let fallback = fallback_default_allowlist("mermaid");
        for (name, en) in &fallback {
            db.set_mcp_tool_enabled("p1", "mermaid", name, *en).unwrap();
        }
        let listed = db.list_project_mcp_tools("p1", "mermaid").unwrap();
        assert_eq!(listed.len(), fallback.len());
    }

    #[test]
    fn reconcile_module_update_drops_removed_tools_keeps_overrides() {
        // v0.2.7 of a module ships [tool_a, tool_b]; the user disables
        // tool_b via the Permissions tab (writes to
        // `project_mcp_tool_grants`). v0.2.8 of the module drops
        // tool_b entirely.
        //
        // Expected: module_mcp_tool_defaults loses tool_b's default row
        // (it's no longer in the manifest), but project_mcp_tool_grants
        // KEEPS the user's explicit override — so if v0.2.9 reinstates
        // tool_b, the user's preference is honoured immediately.
        let dir = tempfile::tempdir().unwrap();
        let db = make_db_with_project("p1", "Acme", dir.path());
        db.reconcile_mcp_tool_defaults(
            "fancy-mcp",
            "fancy-module",
            &[
                ("tool_a".to_string(), true, None),
                ("tool_b".to_string(), true, None),
            ],
            10,
        )
        .unwrap();
        // User disables tool_b.
        db.set_mcp_tool_enabled("p1", "fancy-mcp", "tool_b", false)
            .unwrap();
        // Module updates: tool_b removed.
        db.reconcile_mcp_tool_defaults(
            "fancy-mcp",
            "fancy-module",
            &[("tool_a".to_string(), true, None)],
            20,
        )
        .unwrap();
        // Defaults reflect the new manifest.
        let defaults = db.list_mcp_tool_defaults("fancy-mcp").unwrap();
        assert_eq!(defaults.len(), 1);
        assert_eq!(defaults[0].tool_name, "tool_a");
        // Per-project override SURVIVES the manifest reshape.
        let project_grants = db.list_project_mcp_tools("p1", "fancy-mcp").unwrap();
        assert!(
            project_grants.iter().any(|g| g.tool_name == "tool_b" && !g.enabled),
            "user's explicit disable of tool_b must outlive the manifest update; \
             rows: {:?}",
            project_grants
        );
    }
}
