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

use sha2::{Digest, Sha256};
use serde::Deserialize;
use tauri::{command, State};

use crate::db::diagrams::{AccessRow, DiagramRow, ModuleRow, SnapshotRow, ToolGrant};
use crate::db::Db;

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
    Ok(())
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
}
