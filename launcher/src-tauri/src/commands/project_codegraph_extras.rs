// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! Tauri command surface for project-extra codegraph paths (v0.2.47).
//!
//! Backed by `crate::db::codegraph_extras` (migration 026). Every
//! mutation calls `db.audit(...)` so the audit log records who added /
//! removed / toggled / synced what. The Tauri layer owns:
//!
//!  * Path canonicalisation (absolute, exists, directory, symlinks
//!    resolved, trailing separator stripped, cross-platform forward-
//!    slash storage form). The DB layer takes paths verbatim.
//!
//!  * Validation rejections — same-as-repo, sub-of-repo, duplicate,
//!    non-absolute, non-directory, unreadable. Errors are user-facing
//!    strings.
//!
//!  * Launcher-project disambiguation — on add, the command detects
//!    when the requested path is the `folder_path` of an existing
//!    launcher project and (unless `force=true`) returns a
//!    `disambiguation_required` response shape so the GUI can show the
//!    "Add as project (grant access matrix) / Add as path anyway"
//!    modal (§13.1 of the plan).
//!
//!  * Analyzer dispatch — `sync_*` runs `analyze_code_graph.py`
//!    against ONE extra path; `reindex_project_codegraph_after_extras_change`
//!    runs it ONCE against the project's own repo PLUS every enabled
//!    extra (§14.2 critical invariant — single invocation, union-of-
//!    visited semantics, optional `--prune-stale`).
//!
//!  * Per-project serialisation — a `tokio::sync::Mutex` keyed by
//!    project_id guards the entire add/remove/toggle → reindex
//!    sequence so concurrent calls can't interleave snapshot reads and
//!    analyze invocations. Without this, a concurrent add could
//!    finish between the snapshot read and the reindex completion,
//!    causing the analyzer to see a different file set than what was
//!    in scope when the user clicked the button.
//!
//! Path-storage form (matches the DB's invariant):
//!  * Absolute, canonicalised via `dunce::canonicalize` (resolves
//!    symlinks, normalises segments, lowercases drive letter on
//!    Windows). `dunce` is preferred over `std::fs::canonicalize` to
//!    avoid Windows UNC-prefix surprises (`\\?\C:\...`).
//!  * Forward-slash separators throughout (Windows backslashes
//!    converted via `.replace('\\', '/')`) so prefix-match queries
//!    work cross-platform.
//!  * No trailing `/`.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use tauri::{command, AppHandle, Emitter, State};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::sync::Mutex as AsyncMutex;

use crate::db::codegraph_extras::CodegraphExtraPathRow;
use crate::db::Db;
use vct_launcher_core::process::CommandExt as _;

// ─── Constants ───────────────────────────────────────────────────────────

/// Cap on the analyzer stderr tail kept as failure evidence (the end of
/// the stream is where the actual error is). Streaming drain — see
/// [`vct_launcher_core::process::drain_child_stderr_capped`].
const STDERR_EVIDENCE_BYTES: usize = 2048;

/// Tauri event name for progress lines emitted during sync / reindex.
/// `ExtraCodegraphPathsPanel.svelte` subscribes and folds each event
/// (`extras-sync-progress.ts::applyExtrasProgress`, filtered by
/// `project_id`) into its "Syncing…" / "Re-syncing…" modal
/// (`ExtrasSyncProgressModal.svelte`), which renders a determinate bar and
/// the analyzer's current line (v0.2.100 F-W2-05 — built; before, the modal
/// only spun). The app shell (`stores/ui.ts`) also turns the finished
/// `progress >= 1` event into a notice. Must match
/// `extras-sync-progress.ts::EXTRAS_PROGRESS_EVENT`.
const PROGRESS_EVENT: &str = "vct-codegraph-extras-progress";

// ─── Types ───────────────────────────────────────────────────────────────

/// Wire-shape extra-path row returned by `list_*` / `add_*`. Mirrors
/// `CodegraphExtraPathRow` 1:1 plus a `display_label` convenience
/// field derived from the basename when `label` is `None`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExtraPath {
    pub project_id: String,
    pub path: String,
    pub label: Option<String>,
    pub added_at: i64,
    pub last_indexed_at: Option<i64>,
    pub last_indexed_commit: Option<String>,
    pub enabled: bool,
    /// `label` when set, otherwise the basename of `path` (no extension
    /// stripping — extras are directories, so the basename IS the
    /// natural label). Empty string only if `path` itself is empty
    /// (which can't happen post-canonicalisation).
    pub display_label: String,
    /// v0.2.100 W5R-04: the path's CURRENT repo HEAD (`git rev-parse HEAD`),
    /// `None` for a non-git path or when git is unavailable.
    #[serde(default)]
    pub head_commit: Option<String>,
    /// v0.2.100 W5R-04: HEAD is known and differs from `last_indexed_commit`
    /// — the code graph describes older code than the checkout. Decided by
    /// the one shared rule (`codegraph_extras::extra_path_is_stale`).
    #[serde(default)]
    pub stale: bool,
}

impl ExtraPath {
    fn from_row(row: CodegraphExtraPathRow) -> Self {
        let display_label = row
            .label
            .clone()
            .unwrap_or_else(|| basename_of(&row.path));
        Self {
            project_id: row.project_id,
            path: row.path,
            label: row.label,
            added_at: row.added_at,
            last_indexed_at: row.last_indexed_at,
            last_indexed_commit: row.last_indexed_commit,
            enabled: row.enabled,
            display_label,
            head_commit: None,
            stale: false,
        }
    }

    /// Attach the path's current repo HEAD and derive `stale` from it.
    fn with_head(mut self, head: Option<String>) -> Self {
        self.stale = vct_launcher_core::db::codegraph_extras::extra_path_is_stale(
            head.as_deref(),
            self.last_indexed_commit.as_deref(),
        );
        self.head_commit = head;
        self
    }
}

/// Lightweight project descriptor returned in the disambiguation
/// response. Holds just the fields the GUI needs to render the modal +
/// the follow-up "Add as project" action's parameters.
#[derive(Debug, Clone, Serialize)]
pub struct ProjectMeta {
    pub id: String,
    pub name: String,
    pub slug: String,
    pub folder_path: String,
}

/// Result of `add_project_codegraph_extra_path`. Two-variant enum
/// distinguished by `action`:
///
/// * `action = "added"` — the row was inserted; `row` carries the
///   canonical ExtraPath. `existing_project` is absent.
///
/// * `action = "disambiguation_required"` — the path matches an
///   existing launcher project's folder_path and `force=false`.
///   `existing_project` carries the matched project; `path` echoes
///   the canonicalised input so the GUI can pass it back unmodified
///   to the second-stage call (with `force=true` or to the access-
///   matrix grant). `row` is absent.
///
/// JSON shape (matches §13.1 of the plan):
/// ```json
/// // added
/// {"action": "added", "row": {...ExtraPath...}, "path": "/canonical/path"}
/// // disambiguation
/// {"action": "disambiguation_required",
///  "existing_project": {"id":"...","name":"...","slug":"...","folder_path":"..."},
///  "path": "/canonical/path"}
/// ```
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "action", rename_all = "snake_case")]
pub enum AddExtraPathOutcome {
    Added {
        row: ExtraPath,
        path: String,
    },
    DisambiguationRequired {
        existing_project: ProjectMeta,
        path: String,
    },
}

/// Result of a sync / reindex run. The analyzer's final-report JSON
/// drives these counts; missing fields default to 0.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct SyncOutcome {
    pub files_scanned: u64,
    pub entities_indexed: u64,
    pub duration_ms: u64,
    pub project_codegraph_prefix: String,
    /// Whether `--prune-stale` was active. Always `false` for single-
    /// path `sync_*`; `true` for `reindex_*` when caller asks.
    pub prune_stale: bool,
    /// Echo of which paths the analyzer was told to visit (primary +
    /// every `--extra-path`). Useful in the GUI's "what just happened"
    /// toast and audit-log forensics.
    pub paths: Vec<String>,
}

/// Per-progress-line event emitted to the GUI's modal.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExtrasProgress {
    pub project_id: String,
    /// Echoed back so the modal can sanity-check the event is for the
    /// run it started.
    pub label: String,
    /// Fractional [0, 1].
    pub progress: f64,
    pub message: String,
    pub file: String,
    pub lang: String,
}

// ─── Per-project mutex registry ──────────────────────────────────────────
//
// One async Mutex per project_id, lazily inserted on first access.
// Used to serialise the add/remove/toggle → reindex sequence so two
// concurrent extras mutations on the same project can't interleave.
// Different projects are independent (this scales O(num_projects) in
// memory but each Mutex is ~32 bytes so even 1k projects = 32 KB).
//
// The map itself is behind a sync `std::sync::Mutex` — held only for
// the lookup (microseconds), released BEFORE awaiting on the per-
// project mutex. The pattern matches `services::container_runtime`'s
// existing supervisor lock (added at 58ad931); kept inline here rather
// than promoted to a shared helper because the two registries store
// different value types and unifying them would force a generic that
// doesn't earn its keep.

/// Tauri-managed registry of per-project serialisation locks.
#[derive(Default)]
pub struct ExtrasLockRegistry(std::sync::Mutex<HashMap<String, Arc<AsyncMutex<()>>>>);

impl ExtrasLockRegistry {
    /// Get-or-insert the lock for `project_id`. The returned `Arc` can
    /// be awaited on outside the registry lock.
    fn get(&self, project_id: &str) -> Arc<AsyncMutex<()>> {
        let mut map = self.0.lock().expect("extras lock registry poisoned");
        map.entry(project_id.to_string())
            .or_insert_with(|| Arc::new(AsyncMutex::new(())))
            .clone()
    }
}

// ─── Validation + canonicalisation ───────────────────────────────────────

/// Canonicalise an input path for storage. Rejects:
///   * empty / whitespace-only
///   * relative paths
///   * non-existent paths
///   * non-directory paths
///
/// On success:
///   * resolves symlinks via `dunce::canonicalize` (the dunce wrapper
///     keeps Windows paths in their drive-letter form rather than
///     UNC `\\?\` form),
///   * converts backslashes to forward slashes,
///   * lowercases the drive letter on Windows,
///   * strips trailing separators.
fn canonicalise_for_storage(input: &str) -> Result<String, String> {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return Err("path must not be empty".to_string());
    }

    let candidate = PathBuf::from(trimmed);
    if !candidate.is_absolute() {
        return Err(format!(
            "path must be absolute (got '{}')",
            trimmed
        ));
    }

    // Canonicalise. dunce avoids the Windows UNC-prefix surprise.
    let canon = match dunce::canonicalize(&candidate) {
        Ok(p) => p,
        Err(e) => {
            return Err(format!(
                "path '{}' could not be resolved: {} (does it exist? is it readable?)",
                trimmed, e
            ))
        }
    };

    if !canon.is_dir() {
        return Err(format!(
            "path '{}' is not a directory",
            canon.display()
        ));
    }

    // Normalise to forward-slash form for cross-platform prefix match.
    let mut s = canon.to_string_lossy().replace('\\', "/");

    // Lowercase Windows drive letter. Detect "X:/..." or "X:" prefix.
    if cfg!(windows) {
        let bytes = s.as_bytes();
        if bytes.len() >= 2 && bytes[1] == b':' && bytes[0].is_ascii_alphabetic() {
            let lc = (bytes[0] as char).to_ascii_lowercase();
            s = format!("{}{}", lc, &s[1..]);
        }
    }

    // Strip trailing separators. Avoid stripping the lone root `/` or
    // `c:/` — those forms aren't valid extra paths anyway (we just
    // rejected non-directory paths above; a bare root would be a
    // catastrophic config error, but defensive: leave the platform
    // root intact).
    while s.len() > 3 && s.ends_with('/') {
        s.pop();
    }

    Ok(s)
}

/// Extract the trailing path component for use as a display label.
/// Returns an empty string for inputs that don't have one (root path).
fn basename_of(path: &str) -> String {
    Path::new(path)
        .file_name()
        .map(|os| os.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// Reject a path that overlaps the project's own folder: the same folder,
/// a folder INSIDE it (already indexed — or excluded on purpose — as part of
/// the project), or a folder CONTAINING it (every primary edit would be
/// drained with the extra's argv and the whole tree double-walked).
///
/// v0.2.102 W6-CGD (D-4): the rule has ONE home, the Python
/// `vco_lib.codegraph_extra_overlap` (rule A — the analyzer and the automatic
/// extra-folder refresh call the same function); this wrapper runs it through
/// [`crate::services::vco_lib_bridge::codegraph_extra_overlap`] and maps its
/// verdict onto the command's error channel, which the Add dialog shows as
/// is. A check that cannot run refuses the add (a broken install must not
/// register an unchecked folder). Both sides are realpath-ed by the rule, so
/// a symlinked or non-canonical `folder_path` row compares correctly.
fn reject_self_overlap(
    root: Option<&std::path::Path>,
    extra: &str,
    folder_path: &str,
) -> Result<(), String> {
    let reply = crate::services::vco_lib_bridge::codegraph_extra_overlap(root, extra, folder_path)
        .map_err(|e| format!("could not check '{}' against this project's folder: {}", extra, e))?;
    overlap_reply_to_result(&reply)
}

/// Map the overlap reply to the command's result: `refused` → the rule's own
/// sentence as the error; anything else → allowed. Split out so the mapping is
/// unit-testable without a child.
fn overlap_reply_to_result(reply: &serde_json::Value) -> Result<(), String> {
    if reply.get("refused").and_then(serde_json::Value::as_bool) == Some(true) {
        let msg = reply
            .get("message")
            .and_then(serde_json::Value::as_str)
            .filter(|m| !m.is_empty())
            .unwrap_or("this folder overlaps the project's own folder");
        return Err(msg.to_string());
    }
    Ok(())
}

/// The enabled extras the ONE overlap rule refuses for this project (v0.2.102
/// R11-03). `check` is [`crate::services::vco_lib_bridge::codegraph_extra_overlap`]
/// in production (a seam for tests). A check that cannot run is an `Err`: the
/// caller then runs nothing, rather than guess which folders are walked.
fn refused_extra_paths<F>(
    extras: &[String],
    folder_path: &str,
    check: F,
) -> Result<std::collections::HashSet<String>, String>
where
    F: Fn(&str, &str) -> Result<serde_json::Value, String>,
{
    let mut refused = std::collections::HashSet::new();
    for extra in extras {
        let reply = check(extra, folder_path).map_err(|e| {
            format!("could not check code folder '{}' against this project's folder: {}", extra, e)
        })?;
        if overlap_reply_to_result(&reply).is_err() {
            refused.insert(extra.clone());
        }
    }
    Ok(refused)
}

/// Which extras a "Reindex all" run may mark indexed, with the HEAD read
/// before the walk (NB-H6): every extra except those the overlap rule refused
/// — the analyzer does not walk a refused folder, so stamping it would show
/// it up to date and steer later incremental syncs from a commit nobody
/// indexed (v0.2.102 R11-03).
fn reindex_stamp_targets<'a>(
    extras: &'a [String],
    pre_heads: &'a [Option<String>],
    refused: &std::collections::HashSet<String>,
) -> Vec<(&'a str, Option<&'a str>)> {
    extras
        .iter()
        .zip(pre_heads.iter())
        .filter(|(path, _)| !refused.contains(*path))
        .map(|(path, sha)| (path.as_str(), sha.as_deref()))
        .collect()
}

/// Find a launcher project whose `folder_path` (post-normalisation)
/// equals `canonical_extra`. Returns `Ok(None)` when no project
/// matches. The match is case-insensitive on the drive letter for
/// Windows; otherwise byte-exact.
fn find_project_owning_path(
    db: &Db,
    canonical_extra: &str,
) -> Result<Option<ProjectMeta>, String> {
    // Cheap scan over all projects. The launcher rarely has > 100
    // projects; a SQL query with normalised collation would require
    // a function-based index that's not worth the complexity for
    // this size.
    let projects = match db.list_projects() {
        Ok(v) => v,
        Err(e) => return Err(format!("list projects (extras-disambig): {}", e)),
    };

    let target = canonical_extra.to_ascii_lowercase();
    for p in projects {
        // Apply the same normalisation we use for extras storage:
        // backslash→forward, lowercase drive letter, strip trailing
        // separator. We don't `canonicalize` the project's folder_path
        // here (it may be on a network share / no longer exist) —
        // best-effort byte match is sufficient.
        let mut norm = p.folder_path.replace('\\', "/");
        if cfg!(windows) {
            let bytes = norm.as_bytes();
            if bytes.len() >= 2 && bytes[1] == b':' && bytes[0].is_ascii_alphabetic() {
                let lc = (bytes[0] as char).to_ascii_lowercase();
                norm = format!("{}{}", lc, &norm[1..]);
            }
        }
        let norm = norm.trim_end_matches('/').to_ascii_lowercase();

        if norm == target {
            return Ok(Some(ProjectMeta {
                id: p.id,
                name: p.name,
                slug: p.slug,
                folder_path: p.folder_path,
            }));
        }
    }
    Ok(None)
}

// ─── Read commands ───────────────────────────────────────────────────────

/// List every extra-path row for a project (enabled + disabled).
/// Ordered newest-first by `added_at`.
#[command]
pub async fn list_project_codegraph_extra_paths(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<ExtraPath>, String> {
    let rows = db.list_codegraph_extras(&project_id)?;
    // HEAD per row (one `git rev-parse` each, off the async runtime) so the
    // panel can badge a path whose checkout moved past its last index.
    tokio::task::spawn_blocking(move || {
        rows.into_iter()
            .map(|r| {
                let head = git_head_sha(&r.path);
                ExtraPath::from_row(r).with_head(head)
            })
            .collect::<Vec<_>>()
    })
    .await
    .map_err(|e| format!("list extra paths: {}", e))
}

// ─── Add ─────────────────────────────────────────────────────────────────

/// Add a read-only filesystem path that contributes entities to the
/// project's codegraph.
///
/// On success returns `AddExtraPathOutcome::Added { row, path }`. If
/// the path matches an existing launcher project's `folder_path` AND
/// `force = false`, returns
/// `AddExtraPathOutcome::DisambiguationRequired { existing_project, path }`
/// instead — the GUI then shows the §13.1 modal and re-calls with
/// either `force = true` (insert anyway) or calls the access-matrix
/// command (grant the existing project's codegraph as a read source).
///
/// Validation cascade (in order — earliest failure wins):
///   1. canonicalise (absolute, exists, directory, symlinks resolved)
///   2. reject overlap with this project's own folder_path
///   3. detect launcher-project match → return disambiguation
///      (unless `force = true`)
///   4. DB insert (PK enforces dedup)
///
/// Audit log: `codegraph_extra_path_added` (default) /
/// `codegraph_extra_path_added_force` (when force=true).
#[command]
pub async fn add_project_codegraph_extra_path(
    project_id: String,
    path: String,
    label: Option<String>,
    force: Option<bool>,
    db: State<'_, Db>,
    locks: State<'_, ExtrasLockRegistry>,
) -> Result<AddExtraPathOutcome, String> {
    let force = force.unwrap_or(false);

    // Resolve the project first so we can compare against folder_path.
    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    // Step 1: canonicalise.
    let canonical = canonicalise_for_storage(&path)?;

    // Step 2: reject self-overlap (the one rule, `vco_lib.codegraph_extra_overlap`;
    // blocking child off the async executor).
    let root = crate::services::vco_lib_bridge::resolve_orchestrator_root(db.inner());
    let (extra_c, folder_c) = (canonical.clone(), project.folder_path.clone());
    tokio::task::spawn_blocking(move || reject_self_overlap(root.as_deref(), &extra_c, &folder_c))
        .await
        .map_err(|e| format!("overlap check: {}", e))??;

    // Step 3: launcher-project disambiguation. Only the EXACT
    // folder_path match triggers the modal — a sub-path of another
    // project doesn't, because the user might legitimately want to
    // index a subset of a sibling clone without granting access to the
    // whole sibling project.
    if !force {
        if let Some(existing) = find_project_owning_path(&db, &canonical)? {
            // Don't surface a disambiguation if the matched project
            // IS this one (would happen if the user somehow tried to
            // add their own root via a canonicalisation roundabout —
            // the self-overlap reject above should have caught it,
            // but defensive: skip the modal in that pathological case
            // because the resulting access grant would be self-to-self
            // which the access-matrix code refuses).
            if existing.id != project.id {
                return Ok(AddExtraPathOutcome::DisambiguationRequired {
                    existing_project: existing,
                    path: canonical,
                });
            }
        }
    }

    // Step 4: acquire the per-project lock so a concurrent add/remove
    // on the same project can't see a half-committed state.
    let lock = locks.get(&project.id);
    let _guard = lock.lock().await;

    // DB insert. The PRIMARY KEY catches duplicates; we surface the
    // SQLite "UNIQUE constraint failed" message as a friendly error.
    let row = match db.add_codegraph_extra(&project.id, &canonical, label.as_deref()) {
        Ok(r) => r,
        Err(e) if e.to_lowercase().contains("unique") || e.to_lowercase().contains("constraint") => {
            return Err(format!(
                "the path '{}' is already an extra codegraph path for this project",
                canonical
            ));
        }
        Err(e) => return Err(e),
    };

    // Audit.
    let op = if force {
        "codegraph_extra_path_added_force"
    } else {
        "codegraph_extra_path_added"
    };
    let _ = db.audit(
        op,
        Some(&project.id),
        None,
        &serde_json::json!({
            "path": canonical,
            "label": label,
            "force": force,
        }),
    );

    Ok(AddExtraPathOutcome::Added {
        row: ExtraPath::from_row(row),
        path: canonical,
    })
}

// ─── Remove ──────────────────────────────────────────────────────────────

/// Remove an extra-path row by (project_id, path). Soft-fails when
/// the row doesn't exist (returns Ok with no error so the GUI doesn't
/// have to handle "nothing to delete" specially).
///
/// Audit log: `codegraph_extra_path_removed`. The caller (GUI) is
/// expected to follow this with `reindex_project_codegraph_after_extras_change`
/// (prune_stale=true) so the removed path's entries are dropped from
/// Weaviate.
#[command]
pub async fn remove_project_codegraph_extra_path(
    project_id: String,
    path: String,
    db: State<'_, Db>,
    locks: State<'_, ExtrasLockRegistry>,
) -> Result<(), String> {
    // Acquire the lock around the DELETE + audit so a concurrent add
    // for the same path can't race against our removal.
    let lock = locks.get(&project_id);
    let _guard = lock.lock().await;

    let _ = db.remove_codegraph_extra(&project_id, &path)?;

    let _ = db.audit(
        "codegraph_extra_path_removed",
        Some(&project_id),
        None,
        &serde_json::json!({ "path": path }),
    );

    Ok(())
}

// ─── Enable / disable ────────────────────────────────────────────────────

/// Toggle the `enabled` flag for one extra-path row.
///
/// Audit log: `codegraph_extra_path_enabled_toggled` with `{path, enabled}`.
/// Returns an error if the row doesn't exist (vs `remove_*`'s soft-success
/// pattern — toggling a non-existent row is a programming error, not a
/// best-effort cleanup).
#[command]
pub async fn set_project_codegraph_extra_path_enabled(
    project_id: String,
    path: String,
    enabled: bool,
    db: State<'_, Db>,
    locks: State<'_, ExtrasLockRegistry>,
) -> Result<(), String> {
    let lock = locks.get(&project_id);
    let _guard = lock.lock().await;

    let n = db.set_codegraph_extra_enabled(&project_id, &path, enabled)?;
    if n == 0 {
        return Err(format!(
            "no extra-path row for project '{}' at '{}'",
            project_id, path
        ));
    }

    let _ = db.audit(
        "codegraph_extra_path_enabled_toggled",
        Some(&project_id),
        None,
        &serde_json::json!({ "path": path, "enabled": enabled }),
    );

    Ok(())
}

// ─── Sync ONE extra path ────────────────────────────────────────────────

/// Run the analyzer against a SINGLE extra path (the path's own
/// `last_indexed_commit` may steer `--incremental --since-commit`).
/// Does NOT pass `--prune-stale` — a single-path sync adds / refreshes
/// entities for the path itself and, when incremental, removes the
/// entities of files deleted or renamed away since `last_indexed_commit`
/// (scoped to this path's `project_source`; `vco_lib/codegraph_deleted_files.py`).
/// If that removal fails the analyzer exits 5 and the commit is NOT
/// recorded. The project-wide visited-UUID prune stays with `reindex_*`.
///
/// Audit log: `codegraph_extra_path_synced` with the outcome.
#[command]
pub async fn sync_project_codegraph_extra_path(
    project_id: String,
    path: String,
    incremental: Option<bool>,
    db: State<'_, Db>,
    app: AppHandle,
    locks: State<'_, ExtrasLockRegistry>,
) -> Result<SyncOutcome, String> {
    let incremental = incremental.unwrap_or(false);

    // Snapshot the row + project metadata inside the lock so the
    // analyzer args are consistent with what the user clicked on.
    let lock = locks.get(&project_id);
    let _guard = lock.lock().await;

    let row = db
        .get_codegraph_extra(&project_id, &path)?
        .ok_or_else(|| format!("no extra-path row at '{}' for project {}", path, project_id))?;
    if !row.enabled {
        return Err(format!(
            "the path '{}' is currently disabled — re-enable it before syncing",
            path
        ));
    }

    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    let prefix = resolve_collection_prefix(&db, &project)?;

    // Run analyzer against just this path. `--project <prefix>` makes
    // it write into the project's codegraph collections. The argv shape
    // has ONE home (`extra_path_sync_args`, v0.2.100 W5R-04) shared with
    // the automatic Stop-drain refresh, so the two triggers cannot drift.
    let label = row.label.clone().unwrap_or_else(|| basename_of(&row.path));
    let args = vct_launcher_core::db::codegraph_extras::extra_path_sync_args(
        &row.path,
        &prefix,
        incremental,
        row.last_indexed_commit.as_deref(),
        // W6-CGD (D-4): the analyzer refuses an extra overlapping the project.
        Some(project.folder_path.as_str()),
    );

    let started = std::time::Instant::now();
    // v0.2.102 NB-H6: the HEAD is read BEFORE the analyzer runs — a commit
    // landing mid-walk must NOT be stamped as indexed (its files were never
    // walked). The ordering lives in the one injectable seam below so a unit
    // test can pin it.
    let head_path = row.path.clone();
    let (report, sha) = analyze_with_pre_read_stamp(
        || run_analyzer_with_stream(&project.folder_path, &project_id, &label, &args, &app),
        move || git_head_sha(&head_path),
    )
    .await?;
    let duration_ms = started.elapsed().as_millis() as u64;

    // Record through the ONE writer shared with the hub route
    // (`record_codegraph_extra_indexed`): last_indexed_* on the row, the
    // V52-Z `code_graph_builds` upsert, and the audit row. SHA: the
    // PRE-analyze HEAD (NB-H6); `None` for a non-git extra path (clears any
    // prior value). A row that vanished mid-run is not resurrected — the
    // sync outcome still returns (the entities are already in Weaviate;
    // only bookkeeping is skipped).
    let entities_indexed = report.modules + report.classes + report.functions + report.apis;
    if let Err(e) = db.record_codegraph_extra_indexed(
        &project_id,
        &row.path,
        &vct_launcher_core::db::codegraph_extras::ExtraPathIndexedRun {
            commit: sha.as_deref(),
            files_analyzed: report.files_analyzed,
            entities_indexed,
            duration_ms,
            trigger: "manual_sync",
        },
    ) {
        tracing::warn!(
            "[vct] warning: recording extra-path sync for project {} failed: {}",
            project_id, e
        );
    }

    let outcome = SyncOutcome {
        files_scanned: report.files_analyzed,
        entities_indexed,
        duration_ms,
        project_codegraph_prefix: prefix,
        prune_stale: false,
        paths: vec![row.path.clone()],
    };

    Ok(outcome)
}

// ─── Reindex (project repo + all enabled extras in one shot) ────────────

/// Re-analyze the project's own repo PLUS every enabled extra in a
/// SINGLE analyzer invocation. Used after add / remove / enabled-
/// toggle so the prune-stale sweep sees the full union of visited
/// files. The critical invariant (§14.2 of the plan, REPEATED):
///
/// > `--prune-stale` runs MUST include `<repo_path>` as the primary
/// > path AND every currently-enabled extra path via `--extra-path`.
/// > Multi-pass analyze (one invocation per source root, each with
/// > `--prune-stale`) would cause each pass to delete the OTHER
/// > passes' UUIDs.
///
/// Audit log: `codegraph_reindex_after_extras_change` with the full
/// path list + prune flag + counts.
#[command]
pub async fn reindex_project_codegraph_after_extras_change(
    project_id: String,
    prune_stale: Option<bool>,
    db: State<'_, Db>,
    app: AppHandle,
    locks: State<'_, ExtrasLockRegistry>,
) -> Result<SyncOutcome, String> {
    let prune_stale = prune_stale.unwrap_or(false);

    // Acquire the per-project lock for the ENTIRE snapshot-read +
    // analyzer-invocation sequence. Without this, a concurrent
    // add/remove between the snapshot and the analyzer finishing
    // would leave the codegraph collection out of sync with the DB
    // (analyzer prunes UUIDs it doesn't visit, the racing add inserts
    // a row whose path was never visited, the resolver returns the
    // new path but Weaviate has no entities for it).
    let lock = locks.get(&project_id);
    let _guard = lock.lock().await;

    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    let extras = db.list_enabled_codegraph_extras(&project_id)?;
    let prefix = resolve_collection_prefix(&db, &project)?;

    // v0.2.102 R11-03: which extras the ONE overlap rule refuses (blocking
    // child off the async executor). They are still passed as `--extra-path`:
    // the analyzer refuses them too and keeps their rows from `--prune-stale`
    // — left out of the argv, an unvisited folder's rows would be pruned. What
    // changes is that a refused folder is never stamped as indexed below.
    let root = crate::services::vco_lib_bridge::resolve_orchestrator_root(db.inner());
    let (extra_paths, folder_c) = (
        extras.iter().map(|e| e.path.clone()).collect::<Vec<String>>(),
        project.folder_path.clone(),
    );
    let refused = tokio::task::spawn_blocking(move || {
        refused_extra_paths(&extra_paths, &folder_c, |extra, folder| {
            crate::services::vco_lib_bridge::codegraph_extra_overlap(root.as_deref(), extra, folder)
        })
    })
    .await
    .map_err(|e| format!("overlap check: {}", e))??;

    // Build args: primary path first, then every --extra-path entry.
    let mut args: Vec<String> = vec![
        project.folder_path.clone(),
        "--project".to_string(),
        prefix.clone(),
        "--json-progress".to_string(),
    ];
    let mut paths: Vec<String> = vec![project.folder_path.clone()];
    for e in &extras {
        args.push("--extra-path".to_string());
        args.push(e.path.clone());
        paths.push(e.path.clone());
    }
    if prune_stale {
        args.push("--prune-stale".to_string());
    }

    let started = std::time::Instant::now();
    // v0.2.102 fix-round (NB-H6, reindex arm): every extra's HEAD is read
    // BEFORE the multi-root analyze — the pre-fix loop below read them AFTER,
    // so a commit landing mid-walk was stamped as indexed although its files
    // were never walked. Same injectable seam as the single-path Sync.
    let head_paths: Vec<String> = extras.iter().map(|e| e.path.clone()).collect();
    let (report, pre_heads) = analyze_with_pre_read_stamp(
        || run_analyzer_with_stream(
            &project.folder_path,
            &project_id,
            &project.name,
            &args,
            &app,
        ),
        move || head_paths.iter().map(|p| git_head_sha(p)).collect::<Vec<Option<String>>>(),
    )
    .await?;
    let duration_ms = started.elapsed().as_millis() as u64;

    // After a successful reindex, refresh each WALKED extra's last_indexed_*
    // so subsequent incremental runs have an accurate baseline. The commit
    // stamped is the PRE-analyze HEAD (NB-H6); a refused extra is not walked
    // and keeps its stamp (R11-03).
    let now = chrono::Utc::now().timestamp_millis();
    let extra_list: Vec<String> = extras.iter().map(|e| e.path.clone()).collect();
    for (path, sha) in reindex_stamp_targets(&extra_list, &pre_heads, &refused) {
        let _ = db.update_codegraph_extra_last_indexed(&project_id, path, now, sha);
    }

    // V52-Z (v0.2.52): also upsert `code_graph_builds`. The reindex
    // walks the primary repo PLUS every extra in one invocation, so
    // this row represents the union-of-roots build. The UI's "last
    // successful build" timestamp now updates on every reindex (vs.
    // only on the legacy `run_build_task` primary-only path). See the
    // matching write in `sync_project_codegraph_extra_path` for the
    // single-path case + the design rationale.
    let started_at_ms = now - duration_ms as i64;
    if let Err(e) = db.upsert_code_graph_build(
        &project_id,
        "success",
        Some(started_at_ms),
        Some(now),
        Some(duration_ms as i64),
        report.files_analyzed as u32,
        None,           // languages: not surfaced here
        false,          // joern_used: DEPRECATED constant-false (CG-3, v0.2.73)
        None,
        None,
    ) {
        tracing::warn!(
            "[vct] warning: V52-Z code_graph_builds upsert failed for project {}: {}",
            project_id, e
        );
    }

    let outcome = SyncOutcome {
        files_scanned: report.files_analyzed,
        entities_indexed: report.modules + report.classes + report.functions + report.apis,
        duration_ms,
        project_codegraph_prefix: prefix,
        prune_stale,
        paths: paths.clone(),
    };

    let mut refused_sorted: Vec<&String> = refused.iter().collect();
    refused_sorted.sort();
    let _ = db.audit(
        "codegraph_reindex_after_extras_change",
        Some(&project_id),
        None,
        &serde_json::json!({
            "paths": paths,
            "files_scanned": outcome.files_scanned,
            "entities_indexed": outcome.entities_indexed,
            "duration_ms": outcome.duration_ms,
            "prune_stale": prune_stale,
            "refused_extras": refused_sorted,
        }),
    );

    Ok(outcome)
}

// ─── Analyzer support ────────────────────────────────────────────────────

/// Resolve the codegraph collection prefix for a project. Reads the
/// binding row first; falls back to a sanitised slug derivation when
/// the project has never been analysed (no binding row yet). Mirrors
/// the hub resolver's fallback in `config_api.rs`.
fn resolve_collection_prefix(
    db: &Db,
    project: &vct_launcher_core::db::models::ProjectRow,
) -> Result<String, String> {
    if let Some(b) = db.get_project_codegraph_binding(&project.id)? {
        if !b.collection_prefix.trim().is_empty() {
            return Ok(b.collection_prefix);
        }
    }
    // Fallback: ASCII-safe sanitisation of the slug. Simple form —
    // any non-alphanumeric run collapses to `_`, capitalise the first
    // character. This is the same provisional path the hub uses
    // before the first analyze run; it gets replaced on first
    // `set_project_codegraph_binding`.
    let mut s = String::with_capacity(project.slug.len());
    let mut prev_was_underscore = false;
    for c in project.slug.chars() {
        if c.is_ascii_alphanumeric() {
            s.push(c);
            prev_was_underscore = false;
        } else if !prev_was_underscore {
            s.push('_');
            prev_was_underscore = true;
        }
    }
    let s = s.trim_matches('_').to_string();
    if s.is_empty() {
        return Ok("Project".to_string());
    }
    let mut chars = s.chars();
    let first = chars.next().unwrap().to_ascii_uppercase();
    Ok(format!("{}{}", first, chars.as_str()))
}

/// Best-effort `git rev-parse HEAD` for a directory. Returns `None`
/// when the path isn't a git repo (no `.git`) or git is unavailable.
fn git_head_sha(repo_path: &str) -> Option<String> {
    let out = std::process::Command::new("git")
        .silent()
        .arg("-C")
        .arg(repo_path)
        .arg("rev-parse")
        .arg("HEAD")
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let sha = String::from_utf8(out.stdout).ok()?.trim().to_string();
    if sha.is_empty() {
        None
    } else {
        Some(sha)
    }
}

/// Minimal analyzer final-report shape. We only consume the counters
/// — the language / prune_stale / file_skipped fields aren't part of
/// the SyncOutcome surface and aren't read here. Defaults to zero on
/// missing fields so the analyzer can be extended without breaking
/// this reader.
#[derive(Debug, Clone, Default, Deserialize)]
struct AnalyzerFinalReport {
    #[serde(default)]
    pub files_analyzed: u64,
    #[serde(default)]
    pub modules: u64,
    #[serde(default)]
    pub classes: u64,
    #[serde(default)]
    pub functions: u64,
    #[serde(default)]
    pub apis: u64,
}

/// v0.2.102 NB-H6 — run the analyzer and return the commit stamp(s) to
/// record, with the HEAD read(s) BEFORE the analyze.
///
/// Reading `git rev-parse HEAD` AFTER the walk stamps a commit that landed
/// mid-run as "indexed" although its files were never walked — the row then
/// looks current and neither the incremental `--since-commit` path nor the
/// panel's stale badge ever revisits those files. The stamp must be the
/// PRE-analyze HEAD: if HEAD moved during the walk, the post-walk comparison
/// (`extra_path_is_stale`) still sees the delta and the next sync walks it.
///
/// The stamp type is generic: the single-path Sync uses `Option<String>`;
/// `reindex_project_codegraph_after_extras_change` collects one
/// `Option<String>` per extra (`Vec<Option<String>>`) before its multi-root
/// analyze (same mid-walk race, fix-round 2). Both legs are injectable so a
/// unit test can advance "HEAD" mid-run and pin the ordering. `read_head`
/// is called exactly once, before `run_analyzer` is awaited.
async fn analyze_with_pre_read_stamp<Run, Fut, ReadHead, Stamp>(
    run_analyzer: Run,
    read_head: ReadHead,
) -> Result<(AnalyzerFinalReport, Stamp), String>
where
    Run: FnOnce() -> Fut,
    Fut: std::future::Future<Output = Result<AnalyzerFinalReport, String>>,
    ReadHead: FnOnce() -> Stamp,
{
    let stamp = read_head();
    let report = run_analyzer().await?;
    Ok((report, stamp))
}

/// Resolve `python` to use for the analyzer. Mirrors
/// `codegraph_reanalyze::resolve_python_for_analyzer`.
///
/// v0.2.103: REALLY delegates to the shared RT-4 ladder now
/// (`vct_launcher_core::python_resolve::resolve_python_for_vco_lib`) — the
/// spawn below routes through `-m vco_lib.codegraph_walk` (the per-project
/// walk single-flight claim), whose module must be importable by the chosen
/// interpreter, and the ladder is the one home that resolves exactly such an
/// interpreter. The previous inline exe-walk claimed this docstring's
/// mirror but lacked the `$VCT_VENV` / env-root tiers, and its bare
/// `python3` last resort could import neither `vco_lib` nor `weaviate` —
/// it only ever produced a walk that died on import. A clean "no
/// interpreter" refusal replaces that dead spawn.
fn resolve_python_for_analyzer() -> Option<PathBuf> {
    vct_launcher_core::python_resolve::resolve_python_for_vco_lib()
}

/// Resolve `analyze_code_graph.py`. Tries the project folder first,
/// then walks up from the launcher binary looking for an installed
/// orchestrator clone.
fn resolve_analyzer_script(project_folder: &Path) -> Option<PathBuf> {
    let candidate = project_folder
        .join(".claude")
        .join("scripts")
        .join("analyze_code_graph.py");
    if candidate.is_file() {
        return Some(candidate);
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let mut cur = parent.to_path_buf();
            for _ in 0..8 {
                let probe = cur
                    .join(".claude")
                    .join("scripts")
                    .join("analyze_code_graph.py");
                if probe.is_file() {
                    return Some(probe);
                }
                if !cur.pop() {
                    break;
                }
            }
        }
    }
    None
}

/// Build the analyzer spawn for an extra-path sync.
///
/// v0.2.103: the analyzer is spawned THROUGH the per-project walk
/// single-flight claim (`-m vco_lib.codegraph_walk`, the Python SSOT —
/// [`vct_launcher_core::process::claimed_walk_args`]). The sync argv carries
/// `--project <prefix>` (the project's own codegraph prefix), so the claim
/// serializes this sync against a full build / Re-analyze / resync-driver
/// walk of the SAME project — the 2026-10-14 twin-walk defect. A refusal
/// exits 75 and maps to the "already running" message below, not a failure.
/// The claim module re-spawns the analyzer under `sys.executable` with
/// inherited stdio, so `--json-progress` streaming is unaffected.
/// Extracted from `run_analyzer_with_stream` so a unit test can inspect the
/// argv hop (`tokio::process::Command::as_std`).
fn build_extras_analyzer_command(
    python: &Path,
    script: &Path,
    args: &[String],
) -> tokio::process::Command {
    let mut cmd = tokio::process::Command::new(python).silent();
    cmd.args(vct_launcher_core::process::claimed_walk_args(script));
    for a in args {
        cmd.arg(a);
    }
    cmd.stdin(std::process::Stdio::null());
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    cmd
}

/// Spawn the analyzer, stream progress events to the GUI, parse the
/// final-report line, and return it. Mirrors the
/// `run_reanalysis_with_stream` pattern in
/// `commands::codegraph_reanalyze`.
async fn run_analyzer_with_stream(
    project_folder: &str,
    project_id: &str,
    label: &str,
    args: &[String],
    app: &AppHandle,
) -> Result<AnalyzerFinalReport, String> {
    let python = resolve_python_for_analyzer()
        .ok_or_else(|| "no python interpreter found for analyzer".to_string())?;
    let folder_path = PathBuf::from(project_folder);
    let script = resolve_analyzer_script(&folder_path).ok_or_else(|| {
        "analyze_code_graph.py not found (looked in project's .claude/scripts \
         and the launcher's install root)"
            .to_string()
    })?;

    let mut cmd = build_extras_analyzer_command(&python, &script, args);

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("spawn analyzer: {}", e))?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "child has no stdout pipe".to_string())?;
    let stderr = child.stderr.take();

    let mut reader = BufReader::new(stdout).lines();
    let mut final_report: Option<AnalyzerFinalReport> = None;

    let read_fut = async {
        while let Ok(Some(line)) = reader.next_line().await {
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            handle_analyzer_line(trimmed, project_id, label, app, &mut final_report);
        }
    };

    // v0.2.103 (owner rule "no timeout may cut valid long work"): the
    // 30-minute ANALYZE_TIMEOUT_SECS deadline is GONE — an extras
    // re-index is a full walk of the repo plus every enabled extra and
    // may legitimately run for hours; killing a slow-but-working walk
    // makes it look like a failure so the user clicks Sync again. With
    // no deadline as a backstop, stderr is drained CONCURRENTLY with the
    // stdout read (a piped stderr nobody reads fills its ~64 KiB kernel
    // buffer and the child blocks on write(2) forever — an unkillable
    // hang, not a slow walk). Both concerns live in the ONE shared home:
    // `vct_launcher_core::process::{drain_child_stderr_capped,
    // await_child_no_deadline}` (same composition as the Re-analyze
    // modal's wait). The per-file progress events above are the
    // liveness signal.
    let stderr_drain = async {
        match stderr {
            Some(stream) => {
                vct_launcher_core::process::drain_child_stderr_capped(
                    stream,
                    STDERR_EVIDENCE_BYTES,
                )
                .await
            }
            None => String::new(),
        }
    };
    let (status, stderr_text) =
        vct_launcher_core::process::await_child_no_deadline(read_fut, stderr_drain, child.wait())
            .await?;

    if !status.success() {
        // v0.2.103: the walk-claim refusal is not a failure — the project's
        // code graph is already being walked; say so (the Sync button's
        // error surface shows this sentence).
        if vct_launcher_core::process::is_walk_already_running(&status) {
            return Err(vct_launcher_core::process::walk_already_running_message());
        }
        return Err(format!(
            "analyzer {}",
            vct_launcher_core::process::failure_evidence(&status, &stderr_text, vct_launcher_core::process::StderrKeep::Whole)
        ));
    }

    Ok(final_report.unwrap_or_default())
}

fn handle_analyzer_line(
    line: &str,
    project_id: &str,
    label: &str,
    app: &AppHandle,
    final_report: &mut Option<AnalyzerFinalReport>,
) {
    let value: serde_json::Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(_) => return,
    };
    let obj = match value.as_object() {
        Some(o) => o,
        None => return,
    };

    if obj.get("final").and_then(|v| v.as_bool()) == Some(true) {
        if let Ok(report) = serde_json::from_value::<AnalyzerFinalReport>(value) {
            *final_report = Some(report);
        }
        return;
    }

    if obj.contains_key("progress") {
        let progress = obj
            .get("progress")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);
        let message = obj
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let file = obj
            .get("file")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let lang = obj
            .get("lang")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let payload = ExtrasProgress {
            project_id: project_id.to_string(),
            label: label.to_string(),
            progress,
            message,
            file,
            lang,
        };
        let _ = app.emit(PROGRESS_EVENT, payload);
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Tests
// ═══════════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;
    use vct_launcher_core::db::models::ProjectHost;

    fn fresh_db_with_project(id: &str, name: &str, folder: &str) -> Db {
        let db = Db::open_in_memory().expect("in-memory db");
        let slug = db.generate_unique_slug(name).unwrap();
        db.insert_project(id, name, folder, ProjectHost::Base, &slug)
            .unwrap();
        db
    }

    // ─── analyzer wait: no wall-clock kill, concurrent stderr drain ────

    /// Owner rule (2026-10-14): no timeout may cut valid long work — the
    /// extras analyzer wait (formerly `ANALYZE_TIMEOUT_SECS`, 30 min) must
    /// have NO deadline. BEHAVIOURAL (not a source scan): under paused
    /// tokio time, six hours of virtual wall-clock advance must leave the
    /// wait PENDING — any re-added deadline (a `timeout` wrapper, a
    /// `select!` on a `sleep`) fires when time advances and this test goes
    /// red. Only the child's own completion (the oneshots here) resolves
    /// the wait.
    #[cfg(unix)]
    #[tokio::test(start_paused = true)]
    async fn extras_analyzer_wait_has_no_wall_clock_kill() {
        use std::os::unix::process::ExitStatusExt;

        let (read_tx, read_rx) = tokio::sync::oneshot::channel::<()>();
        let (wait_tx, wait_rx) = tokio::sync::oneshot::channel::<()>();
        let read_fut = async {
            let _ = read_rx.await;
        };
        let wait_fut = async {
            let _ = wait_rx.await;
            Ok::<_, std::io::Error>(std::process::ExitStatus::from_raw(0))
        };
        let drain_fut = async { String::new() };

        let fut = vct_launcher_core::process::await_child_no_deadline(
            read_fut, drain_fut, wait_fut,
        );
        let mut fut = std::pin::pin!(fut);

        // Pending before anything fires...
        assert!(
            tokio::time::timeout(std::time::Duration::ZERO, fut.as_mut())
                .await
                .is_err(),
            "wait must stay pending while the child runs"
        );
        // ...and still pending after 6 h of virtual wall-clock — a
        // re-added deadline would fire here and fail the assert below.
        tokio::time::advance(std::time::Duration::from_secs(6 * 3600)).await;
        assert!(
            tokio::time::timeout(std::time::Duration::ZERO, fut.as_mut())
                .await
                .is_err(),
            "wait must survive 6 h of wall-clock with no deadline firing"
        );

        // The child's own completion resolves it.
        let _ = read_tx.send(());
        let _ = wait_tx.send(());
        let (status, tail) = fut.await.expect("wait completes when the child does");
        assert!(status.success());
        assert_eq!(tail, "");
    }

    /// A child writing MORE than the kernel pipe buffer (~64 KiB) to
    /// stderr and then exiting must complete — not hang. With the
    /// 30-minute timeout removed there is no backstop left, so the
    /// CONCURRENT stderr drain is the only thing preventing the child
    /// from blocking forever on its full stderr pipe. The real-time
    /// watchdog turns that hang into a clean test failure. Uses the
    /// exact composition `run_analyzer_with_stream` now uses.
    #[cfg(unix)]
    #[tokio::test]
    async fn huge_stderr_child_completes_rather_than_deadlocking() {
        // ~2 MiB of stderr (32x a 64 KiB pipe buffer), then exit 0.
        let mut cmd = tokio::process::Command::new("/bin/sh");
        cmd.arg("-c")
            .arg("yes 'EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE' | head -c 2200000 >&2; echo done");
        cmd.stdin(std::process::Stdio::null());
        cmd.stdout(std::process::Stdio::piped());
        cmd.stderr(std::process::Stdio::piped());

        let mut child = cmd.spawn().expect("spawn fake analyzer");
        let stdout = child.stdout.take().expect("stdout pipe");
        let stderr = child.stderr.take();

        let mut reader = BufReader::new(stdout).lines();
        let read_fut = async {
            while let Ok(Some(_line)) = reader.next_line().await {}
        };
        let drain_fut = async {
            match stderr {
                Some(stream) => {
                    vct_launcher_core::process::drain_child_stderr_capped(
                        stream,
                        STDERR_EVIDENCE_BYTES,
                    )
                    .await
                }
                None => String::new(),
            }
        };

        let (status, tail) =
            vct_launcher_core::process::await_child_no_deadline(
                read_fut, drain_fut, child.wait(),
            )
            .await
            .expect("child must complete, not deadlock on a full stderr pipe");
        assert!(status.success());
        // The capped tail keeps only the LAST STDERR_EVIDENCE_BYTES.
        assert!(tail.len() <= STDERR_EVIDENCE_BYTES, "tail len {}", tail.len());
        assert!(tail.ends_with('E'), "tail keeps the END of the stream");
    }

    // ─── canonicalise_for_storage ───────────────────────────────────────

    #[test]
    fn canonicalise_rejects_empty() {
        let err = canonicalise_for_storage("").unwrap_err();
        assert!(err.contains("empty"), "got: {}", err);
        let err = canonicalise_for_storage("   ").unwrap_err();
        assert!(err.contains("empty"), "got: {}", err);
    }

    // ─── v0.2.103: walk single-flight claim ──────────────────────────────

    #[test]
    fn extras_analyzer_command_routes_through_the_walk_claim() {
        let python = Path::new("/venv/bin/python");
        let script = Path::new("/x/.claude/scripts/analyze_code_graph.py");
        let sync_args = vec![
            "/extra/checkout".to_string(),
            "--project".to_string(),
            "Acme".to_string(),
            "--json-progress".to_string(),
            "--as-extra-path".to_string(),
        ];
        let cmd = build_extras_analyzer_command(python, script, &sync_args);
        let std_cmd = cmd.as_std();
        assert_eq!(std_cmd.get_program(), std::ffi::OsStr::new("/venv/bin/python"));
        let args: Vec<String> = std_cmd
            .get_args()
            .map(|a| a.to_string_lossy().to_string())
            .collect();
        assert_eq!(args[0], "-m", "the claim module hop comes first: {args:?}");
        assert_eq!(args[1], "vco_lib.codegraph_walk");
        assert_eq!(args[2], script.to_string_lossy());
        // The sync argv follows the script, unchanged and in order.
        assert_eq!(args[3..], sync_args[..], "analyzer args pass through verbatim");
    }

    /// Exit 75 (the vco_lib walk-claim refusal) is recognised by the shared
    /// predicate this module's failure arm maps to the "already running"
    /// sentence.
    #[cfg(unix)]
    #[test]
    fn extras_failure_arm_recognises_the_claim_refusal() {
        use std::os::unix::process::ExitStatusExt;
        let refusal = std::process::ExitStatus::from_raw(75 << 8);
        assert!(vct_launcher_core::process::is_walk_already_running(&refusal));
        let failure = std::process::ExitStatus::from_raw(5 << 8);
        assert!(!vct_launcher_core::process::is_walk_already_running(&failure));
    }

    #[test]
    fn canonicalise_rejects_relative_paths() {
        let err = canonicalise_for_storage("./rel/path").unwrap_err();
        assert!(err.contains("absolute"), "got: {}", err);
        let err = canonicalise_for_storage("foo/bar").unwrap_err();
        assert!(err.contains("absolute"), "got: {}", err);
    }

    #[test]
    fn canonicalise_rejects_nonexistent_paths() {
        // Use a path that has near-zero chance of existing.
        let probe = if cfg!(windows) {
            "C:/__vct_test_nonexistent_path_xyzzy_12345__"
        } else {
            "/__vct_test_nonexistent_path_xyzzy_12345__"
        };
        let err = canonicalise_for_storage(probe).unwrap_err();
        assert!(err.contains("could not be resolved"), "got: {}", err);
    }

    #[test]
    fn canonicalise_rejects_non_directory_paths() {
        // Create a tempfile and try to canonicalise its path.
        let tmp = std::env::temp_dir();
        let path = tmp.join(format!("vct-test-file-{}.txt", std::process::id()));
        std::fs::write(&path, b"data").unwrap();
        let result = canonicalise_for_storage(path.to_str().unwrap());
        let _ = std::fs::remove_file(&path);
        let err = result.unwrap_err();
        assert!(err.contains("not a directory"), "got: {}", err);
    }

    #[test]
    fn canonicalise_strips_trailing_separator() {
        let tmp = std::env::temp_dir();
        let s = tmp.to_string_lossy().to_string();
        // Try with and without trailing separator; both must yield the
        // same canonical form (no trailing separator).
        let normal = canonicalise_for_storage(&s).unwrap();
        let with_slash = canonicalise_for_storage(&format!("{}/", s.trim_end_matches('/'))).unwrap();
        assert_eq!(normal, with_slash);
        assert!(!normal.ends_with('/'), "result must not end with /: {}", normal);
    }

    #[test]
    fn canonicalise_uses_forward_slashes() {
        // Re-canonicalise the temp dir; result must contain only forward slashes.
        let tmp = std::env::temp_dir();
        let canon = canonicalise_for_storage(tmp.to_str().unwrap()).unwrap();
        assert!(!canon.contains('\\'), "result must not contain backslashes: {}", canon);
    }

    // ─── reject_self_overlap ────────────────────────────────────────────
    // W6-CGD (D-4): these drive the ONE rule end-to-end through the bridge
    // child (`python -m vco_lib.codegraph_extra_overlap`, this checkout's
    // vco_lib under cfg(test)); the rule's own cases live in
    // tests/test_v02102_codegraph_source_scope.py.

    #[test]
    fn reject_self_overlap_names_an_ignored_subfolder() {
        // A folder under an ignore-set dir is NOT indexed by the project, so the
        // refusal must not claim it is "already indexed".
        let err = reject_self_overlap(None, "/opt/myproj/vendor/lib", "/opt/myproj").unwrap_err();
        assert!(err.contains("never indexes"), "got: {}", err);
        assert!(!err.contains("already indexed"), "got: {}", err);
    }

    #[test]
    fn overlap_reply_mapping_refused_and_allowed() {
        let refused = serde_json::json!({"ok": true, "verdict": "inside_project", "refused": true,
                                         "message": "nope"});
        assert_eq!(overlap_reply_to_result(&refused).unwrap_err(), "nope");
        let allowed = serde_json::json!({"ok": true, "verdict": "ok", "refused": false, "message": ""});
        overlap_reply_to_result(&allowed).unwrap();
    }

    #[test]
    fn reject_self_overlap_rejects_exact_match() {
        let err = reject_self_overlap(None, "/opt/myproj", "/opt/myproj").unwrap_err();
        assert!(err.contains("project's own folder"), "got: {}", err);
    }

    #[test]
    fn reject_self_overlap_rejects_sub_path() {
        let err = reject_self_overlap(None, "/opt/myproj/sub/dir", "/opt/myproj").unwrap_err();
        assert!(err.contains("inside this project's folder"), "got: {}", err);
    }

    #[test]
    fn reject_self_overlap_accepts_sibling_paths() {
        // sibling of project: NOT inside.
        reject_self_overlap(None, "/opt/sibling", "/opt/myproj").unwrap();
        // sub-path that doesn't share a full segment boundary.
        reject_self_overlap(None, "/opt/myprojz/sub", "/opt/myproj").unwrap();
    }

    #[test]
    fn reject_self_overlap_handles_trailing_slash() {
        // Both with trailing slash + one without → still detect overlap.
        let err = reject_self_overlap(None, "/opt/myproj/", "/opt/myproj/").unwrap_err();
        assert!(err.contains("project's own folder"), "got: {}", err);
    }

    #[test]
    fn reject_self_overlap_normalises_backslashes() {
        // Folder path stored with backslashes (Windows DB row). Extra
        // path is already forward-slash form (post-canonicalisation).
        let err = reject_self_overlap(None, "c:/opt/myproj/sub", "c:\\opt\\myproj").unwrap_err();
        assert!(err.contains("inside this project's folder"), "got: {}", err);
    }

    // ─── find_project_owning_path ───────────────────────────────────────

    #[test]
    fn find_owning_project_returns_match() {
        let folder = if cfg!(windows) { r"C:\tmp\projA" } else { "/tmp/projA" };
        let db = fresh_db_with_project("pA", "A", folder);
        let canonical = folder.replace('\\', "/");
        let result = find_project_owning_path(&db, &canonical).unwrap();
        let m = result.expect("match found");
        assert_eq!(m.id, "pA");
        assert_eq!(m.name, "A");
    }

    #[test]
    fn find_owning_project_returns_none_when_no_match() {
        let folder = if cfg!(windows) { r"C:\tmp\projA" } else { "/tmp/projA" };
        let db = fresh_db_with_project("pA", "A", folder);
        let result = find_project_owning_path(&db, "/tmp/somewhere-else").unwrap();
        assert!(result.is_none());
    }

    #[test]
    fn find_owning_project_drives_case_insensitive_on_windows() {
        let folder = if cfg!(windows) { r"C:\tmp\caseA" } else { "/tmp/caseA" };
        let db = fresh_db_with_project("pA", "A", folder);
        // Search with lowercase drive letter (matches canonicalise output).
        let canonical = folder.replace('\\', "/");
        let mut lowered = canonical.clone();
        if cfg!(windows) {
            let bytes = lowered.as_bytes();
            if bytes.len() >= 2 && bytes[1] == b':' {
                let lc = (bytes[0] as char).to_ascii_lowercase();
                lowered = format!("{}{}", lc, &lowered[1..]);
            }
        }
        let result = find_project_owning_path(&db, &lowered).unwrap();
        assert!(result.is_some());
    }

    // ─── basename_of ────────────────────────────────────────────────────

    #[test]
    fn basename_of_returns_last_component() {
        assert_eq!(basename_of("/opt/sibling"), "sibling");
        assert_eq!(basename_of("/opt/sibling/sub"), "sub");
        // Empty / root → empty.
        assert_eq!(basename_of(""), "");
    }

    // ─── resolve_collection_prefix ──────────────────────────────────────

    #[test]
    fn resolve_collection_prefix_uses_binding_when_present() {
        let folder = if cfg!(windows) { r"C:\tmp\prefix-binding" } else { "/tmp/prefix-binding" };
        let db = fresh_db_with_project("pP", "MyProj", folder);
        db.set_project_codegraph_binding(
            "pP",
            "CustomPrefix",
            Some("CodeSage-Large-v2"),
            Some(2048),
            None,
            None,
            true,
            &serde_json::json!({}),
        )
        .unwrap();
        let project = db.get_project("pP").unwrap().unwrap();
        let prefix = resolve_collection_prefix(&db, &project).unwrap();
        assert_eq!(prefix, "CustomPrefix");
    }

    #[test]
    fn resolve_collection_prefix_falls_back_to_slug() {
        let folder = if cfg!(windows) { r"C:\tmp\no-binding" } else { "/tmp/no-binding" };
        let db = fresh_db_with_project("pQ", "Other Proj", folder);
        // No codegraph binding row → fallback path. The slug is
        // "other-proj" (whitespace + hyphen normalisation). Expect a
        // PascalCase-with-underscores derivation that capitalises the
        // first letter.
        let project = db.get_project("pQ").unwrap().unwrap();
        let prefix = resolve_collection_prefix(&db, &project).unwrap();
        // The exact form is "Other_proj" (first char upper, rest verbatim
        // after non-alnum collapse). The test pins the shape without
        // over-constraining if the sanitiser is refined later.
        assert!(prefix.starts_with('O'), "expected capitalised, got: {}", prefix);
        assert!(prefix.contains("proj") || prefix.contains("Proj"), "got: {}", prefix);
    }

    #[test]
    fn resolve_collection_prefix_handles_empty_slug_fallback() {
        // Pathological case: slug containing only separators. The
        // fallback returns "Project".
        let folder = if cfg!(windows) { r"C:\tmp\empty-slug-test" } else { "/tmp/empty-slug-test" };
        let db = Db::open_in_memory().expect("in-memory db");
        let now = chrono::Utc::now().timestamp_millis();
        // Insert a project row with a pathological slug. Bypass
        // generate_unique_slug to control the slug exactly.
        {
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                     VALUES ('p-empty', 'X', ?1, 'base', '___', ?2, ?2)",
                    rusqlite::params![folder, now],
                )
                .unwrap();
        }
        let project = db.get_project("p-empty").unwrap().unwrap();
        let prefix = resolve_collection_prefix(&db, &project).unwrap();
        assert_eq!(prefix, "Project", "all-separator slug must fall back to 'Project'");
    }

    // ─── ExtrasLockRegistry ─────────────────────────────────────────────

    #[tokio::test]
    async fn lock_registry_returns_same_mutex_per_project() {
        let reg = ExtrasLockRegistry::default();
        let l1 = reg.get("pA");
        let l2 = reg.get("pA");
        assert!(Arc::ptr_eq(&l1, &l2), "same project_id must return same Arc");
        let l3 = reg.get("pB");
        assert!(!Arc::ptr_eq(&l1, &l3), "different project_id must return different Arc");
    }

    #[tokio::test]
    async fn lock_registry_serialises_concurrent_locks() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        let reg = Arc::new(ExtrasLockRegistry::default());
        let counter = Arc::new(AtomicUsize::new(0));

        let n = 5;
        let mut handles = Vec::new();
        for _ in 0..n {
            let reg = reg.clone();
            let counter = counter.clone();
            handles.push(tokio::spawn(async move {
                let lock = reg.get("pX");
                let _g = lock.lock().await;
                let before = counter.fetch_add(1, Ordering::SeqCst);
                // Briefly hold to give concurrent tasks the chance to
                // contend. With the lock, contender increments only
                // see consecutive values; without, they'd see races.
                tokio::time::sleep(std::time::Duration::from_millis(5)).await;
                let after = counter.load(Ordering::SeqCst);
                assert_eq!(before + 1, after, "lock must serialise increments");
            }));
        }
        for h in handles {
            h.await.unwrap();
        }
        assert_eq!(counter.load(Ordering::SeqCst), n);
    }

    // ─── v0.2.102 R11-03: "Reindex all" never stamps a refused extra ─────

    #[test]
    fn reindex_refused_extras_come_from_the_one_overlap_rule() {
        // Real bridge child (`python -m vco_lib.codegraph_extra_overlap`), the
        // same rule the analyzer applies to a combined walk's `--extra-path`.
        let extras = vec![
            "/opt/myproj/sub".to_string(),
            "/opt/sibling".to_string(),
            "/opt/myproj".to_string(),
        ];
        let refused = refused_extra_paths(&extras, "/opt/myproj", |extra, folder| {
            crate::services::vco_lib_bridge::codegraph_extra_overlap(None, extra, folder)
        })
        .unwrap();
        let mut got: Vec<&String> = refused.iter().collect();
        got.sort();
        assert_eq!(got, vec!["/opt/myproj", "/opt/myproj/sub"]);
    }

    #[test]
    fn reindex_overlap_check_that_cannot_run_is_an_error() {
        let extras = vec!["/opt/sibling".to_string()];
        let err = refused_extra_paths(&extras, "/opt/myproj", |_e, _f| Err("no python".to_string()))
            .unwrap_err();
        assert!(err.contains("/opt/sibling") && err.contains("no python"), "{err}");
    }

    #[test]
    fn reindex_does_not_stamp_a_refused_extra() {
        // ACT: the refused nested folder is not walked, so it keeps its stamp.
        // LEAVE-ALONE: the walked folder is stamped with its PRE-analyze HEAD.
        let extras = vec!["/opt/myproj/sub".to_string(), "/opt/sibling".to_string()];
        let heads = vec![Some("aaa".to_string()), Some("bbb".to_string())];
        let refused: std::collections::HashSet<String> =
            ["/opt/myproj/sub".to_string()].into_iter().collect();
        assert_eq!(
            reindex_stamp_targets(&extras, &heads, &refused),
            vec![("/opt/sibling", Some("bbb"))]
        );
        let none = std::collections::HashSet::new();
        assert_eq!(reindex_stamp_targets(&extras, &heads, &none).len(), 2);
    }

    // ─── v0.2.102 NB-H6: the stamped commit is the PRE-analyze HEAD ──────

    #[tokio::test]
    async fn analyze_with_pre_read_stamp_reads_head_before_the_analyze() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        // "HEAD" advances mid-run: the first read (pre-analyze) is `aaa…`,
        // every later read is `bbb…` (a commit landed during the walk). The
        // stamp must be the PRE-analyze value — the post-walk stale check
        // (`extra_path_is_stale`) then still sees `bbb…` as unindexed.
        let head_reads = AtomicUsize::new(0);
        let events = std::sync::Mutex::new(Vec::new());
        let (report, sha) = analyze_with_pre_read_stamp(
            || async {
                events.lock().unwrap().push("analyze");
                Ok(AnalyzerFinalReport {
                    files_analyzed: 3,
                    modules: 1,
                    classes: 1,
                    functions: 2,
                    apis: 0,
                })
            },
            || {
                events.lock().unwrap().push("read_head");
                match head_reads.fetch_add(1, Ordering::SeqCst) {
                    0 => Some("a".repeat(40)),
                    _ => Some("b".repeat(40)),
                }
            },
        )
        .await
        .expect("analyzer ok");
        assert_eq!(sha.as_deref(), Some("a".repeat(40).as_str()),
            "the stamp must be the PRE-analyze HEAD, not a commit that landed mid-run");
        assert_eq!(head_reads.load(Ordering::SeqCst), 1,
            "HEAD is read exactly once — before the analyze, never again after it");
        assert_eq!(events.lock().unwrap().as_slice(), ["read_head", "analyze"],
            "the read must precede the analyzer run");
        assert_eq!(report.files_analyzed, 3);
    }

    #[tokio::test]
    async fn analyze_with_pre_read_stamp_propagates_analyzer_failure() {
        let (report, sha) = match analyze_with_pre_read_stamp(
            || async { Err::<AnalyzerFinalReport, String>("analyzer exited 1".to_string()) },
            || Some("c".repeat(40)),
        )
        .await
        {
            Ok(v) => v,
            Err(e) => {
                assert!(e.contains("analyzer exited 1"));
                return;
            }
        };
        panic!("analyzer failure must propagate, got ({report:?}, {sha:?})");
    }

    #[tokio::test]
    async fn analyze_with_pre_read_stamp_collects_every_stamp_before_the_analyze() {
        // The reindex arm's shape (fix-round 2, NB-H6): ONE read_head call
        // collects a stamp per extra BEFORE the multi-root analyze; a commit
        // landing mid-walk must surface in NONE of the stamps.
        let events = std::sync::Mutex::new(Vec::new());
        let (report, stamps) = analyze_with_pre_read_stamp(
            || async {
                events.lock().unwrap().push("analyze");
                Ok(AnalyzerFinalReport::default())
            },
            || {
                events.lock().unwrap().push("read_head");
                vec![Some("a".repeat(40)), None, Some("b".repeat(40))]
            },
        )
        .await
        .expect("analyzer ok");
        assert_eq!(events.lock().unwrap().as_slice(), ["read_head", "analyze"],
            "every extra's HEAD must be collected before the analyze starts");
        assert_eq!(stamps,
            vec![Some("a".repeat(40)), None, Some("b".repeat(40))],
            "the stamps are the PRE-analyze reads, verbatim and in extras order");
        let _ = report;
    }

    // ─── reject_self_overlap: ancestor arm (v0.2.102 M-A1) ───────────────

    #[test]
    fn reject_self_overlap_rejects_ancestor_of_project_root() {
        // An extra that CONTAINS the project root would hijack every primary
        // edit into the extra queue / wrong argv shape. Registration must
        // refuse it — the GUI's Add flow reaches this validator through
        // `add_project_codegraph_extra_path`, the only registration path.
        let err = reject_self_overlap(None, "/opt/work", "/opt/work/app").unwrap_err();
        assert!(err.contains("contains this project's own folder"), "got: {}", err);
        let err = reject_self_overlap(None, "/opt", "/opt/work/app").unwrap_err();
        assert!(err.contains("contains this project's own folder"), "got: {}", err);
        // Same boundary rule as the sub-path arm: /opt/work2 is NOT an
        // ancestor of /opt/work/app.
        reject_self_overlap(None, "/opt/work2", "/opt/work/app").unwrap();
        // Case-insensitive, mirroring the existing arms.
        let err = reject_self_overlap(None, "/OPT/WORK", "/opt/work/app").unwrap_err();
        assert!(err.contains("contains this project's own folder"), "got: {}", err);
    }

    // ─── from_row / display_label ───────────────────────────────────────

    #[test]
    fn from_row_derives_basename_label_when_none() {
        let row = CodegraphExtraPathRow {
            project_id: "p1".to_string(),
            path: "/opt/sibling-clone".to_string(),
            label: None,
            added_at: 100,
            last_indexed_at: None,
            last_indexed_commit: None,
            enabled: true,
        };
        let ep = ExtraPath::from_row(row);
        assert_eq!(ep.display_label, "sibling-clone");
        assert!(ep.label.is_none(), "label must stay None when not set");
    }

    #[test]
    fn from_row_uses_user_label_when_set() {
        let row = CodegraphExtraPathRow {
            project_id: "p1".to_string(),
            path: "/opt/sibling-clone".to_string(),
            label: Some("My Sibling".to_string()),
            added_at: 100,
            last_indexed_at: Some(200),
            last_indexed_commit: Some("abc".to_string()),
            enabled: false,
        };
        let ep = ExtraPath::from_row(row);
        assert_eq!(ep.display_label, "My Sibling");
        assert_eq!(ep.label.as_deref(), Some("My Sibling"));
        assert!(!ep.enabled);
    }

    /// v0.2.100 W5R-04: the wire row carries the checkout's HEAD and the
    /// shared staleness verdict the panel badges.
    #[test]
    fn with_head_marks_stale_only_when_head_moved() {
        let mk = |last: Option<&str>| CodegraphExtraPathRow {
            project_id: "p1".to_string(),
            path: "/opt/x".to_string(),
            label: None,
            added_at: 1,
            last_indexed_at: None,
            last_indexed_commit: last.map(str::to_string),
            enabled: true,
        };
        let moved = ExtraPath::from_row(mk(Some("aaa"))).with_head(Some("bbb".into()));
        assert!(moved.stale);
        assert_eq!(moved.head_commit.as_deref(), Some("bbb"));
        let same = ExtraPath::from_row(mk(Some("aaa"))).with_head(Some("aaa".into()));
        assert!(!same.stale);
        let non_git = ExtraPath::from_row(mk(None)).with_head(None);
        assert!(!non_git.stale);
        let never = ExtraPath::from_row(mk(None)).with_head(Some("bbb".into()));
        assert!(never.stale);
        let json = serde_json::to_string(&moved).unwrap();
        assert!(json.contains("\"stale\":true") && json.contains("\"head_commit\":\"bbb\""), "{json}");
    }

    // ─── AddExtraPathOutcome serialisation shape ────────────────────────

    #[test]
    fn add_outcome_added_serialises_with_tag() {
        let outcome = AddExtraPathOutcome::Added {
            row: ExtraPath {
                project_id: "p1".to_string(),
                path: "/opt/x".to_string(),
                label: None,
                added_at: 1,
                last_indexed_at: None,
                last_indexed_commit: None,
                enabled: true,
                display_label: "x".to_string(),
                head_commit: None,
                stale: false,
            },
            path: "/opt/x".to_string(),
        };
        let s = serde_json::to_string(&outcome).unwrap();
        assert!(s.contains("\"action\":\"added\""), "got: {}", s);
        assert!(s.contains("\"row\""), "got: {}", s);
        assert!(s.contains("\"path\":\"/opt/x\""), "got: {}", s);
    }

    #[test]
    fn add_outcome_disambiguation_serialises_with_tag() {
        let outcome = AddExtraPathOutcome::DisambiguationRequired {
            existing_project: ProjectMeta {
                id: "pE".to_string(),
                name: "Existing".to_string(),
                slug: "existing".to_string(),
                folder_path: "/opt/existing".to_string(),
            },
            path: "/opt/existing".to_string(),
        };
        let s = serde_json::to_string(&outcome).unwrap();
        assert!(s.contains("\"action\":\"disambiguation_required\""), "got: {}", s);
        assert!(s.contains("\"existing_project\""), "got: {}", s);
        assert!(s.contains("\"id\":\"pE\""), "got: {}", s);
        assert!(s.contains("\"name\":\"Existing\""), "got: {}", s);
        assert!(s.contains("\"slug\":\"existing\""), "got: {}", s);
    }

    // ─── End-to-end DB cycle (no analyzer subprocess) ───────────────────
    //
    // These tests exercise the validation + DB layer of the Tauri
    // commands by manipulating the DB directly. The analyzer dispatch
    // paths (sync_*, reindex_*) are tested separately at integration
    // time — a unit test that shells out to a real Python interpreter
    // is too brittle for CI.

    #[test]
    fn db_add_remove_cycle_via_extras_module() {
        let folder = if cfg!(windows) { r"C:\tmp\add-remove-cycle" } else { "/tmp/add-remove-cycle" };
        let db = fresh_db_with_project("pX", "X", folder);
        let canonical = if cfg!(windows) { "c:/tmp/some-sibling" } else { "/tmp/some-sibling" };

        // Add via the DB-level helper (the Tauri command boundary
        // calls this after canonicalisation).
        let row = db
            .add_codegraph_extra("pX", canonical, Some("My Sibling"))
            .unwrap();
        assert_eq!(row.path, canonical);
        let ep = ExtraPath::from_row(row);
        assert_eq!(ep.display_label, "My Sibling");

        // Remove.
        let n = db.remove_codegraph_extra("pX", canonical).unwrap();
        assert_eq!(n, 1);
        assert!(db.list_codegraph_extras("pX").unwrap().is_empty());
    }

    #[test]
    fn disambiguation_detection_picks_existing_project() {
        // Seed two projects. Try to add project B's folder as an extra
        // to project A — find_project_owning_path must match B.
        let folder_a = if cfg!(windows) { r"C:\tmp\disambig-a" } else { "/tmp/disambig-a" };
        let folder_b = if cfg!(windows) { r"C:\tmp\disambig-b" } else { "/tmp/disambig-b" };
        let db = fresh_db_with_project("pA", "ProjA", folder_a);
        let slug_b = db.generate_unique_slug("ProjB").unwrap();
        db.insert_project("pB", "ProjB", folder_b, ProjectHost::Base, &slug_b)
            .unwrap();

        let canonical_b = folder_b.replace('\\', "/");
        let m = find_project_owning_path(&db, &canonical_b).unwrap().expect("found B");
        assert_eq!(m.id, "pB");
        assert_eq!(m.name, "ProjB");
    }
}
