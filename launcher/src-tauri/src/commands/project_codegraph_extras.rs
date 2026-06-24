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
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{command, AppHandle, Emitter, State};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, BufReader};
use tokio::sync::Mutex as AsyncMutex;
use tokio::time::timeout;

use crate::db::codegraph_extras::CodegraphExtraPathRow;
use crate::db::Db;
use vct_launcher_core::process::CommandExt as _;

// ─── Constants ───────────────────────────────────────────────────────────

/// Hard ceiling on analyzer subprocess lifetime. Mirrors the
/// `REANALYSIS_TIMEOUT_SECS` in `codegraph_reanalyze.rs` — 30 minutes
/// for the worst-case full re-walk of a large polyglot repo + extras.
const ANALYZE_TIMEOUT_SECS: u64 = 30 * 60;

/// Tauri event name for progress lines emitted during sync / reindex.
/// The GUI's "Syncing…" / "Re-indexing…" modal subscribes to this and
/// updates the progress bar.
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
        }
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

/// Reject a path that overlaps with the project's own folder_path:
///   * exact match
///   * sub-path of folder_path
///
/// Returns the user-facing error string when overlap detected.
fn reject_self_overlap(extra: &str, folder_path: &str) -> Result<(), String> {
    let extra_norm = extra.trim_end_matches('/');
    // Normalise folder_path to forward slashes too (Windows DB rows
    // may have raw backslashes; we don't canonicalise at DB read time
    // so the comparison MUST be tolerant).
    let folder_norm: String = folder_path.replace('\\', "/").trim_end_matches('/').to_string();

    if extra_norm.eq_ignore_ascii_case(&folder_norm) {
        return Err(format!(
            "the path '{}' is this project's own folder — it's already covered \
             by the main codegraph analysis, no need to add it as an extra",
            extra
        ));
    }

    // Sub-path check. We require the prefix to be followed by a `/`
    // so `/opt/foo` does NOT shadow `/opt/foobar`.
    let prefix_with_sep = format!("{}/", folder_norm);
    if extra_norm
        .to_ascii_lowercase()
        .starts_with(&prefix_with_sep.to_ascii_lowercase())
    {
        return Err(format!(
            "the path '{}' is INSIDE this project's own folder ('{}') — it's already \
             covered by the main codegraph analysis",
            extra, folder_path
        ));
    }

    Ok(())
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
    Ok(rows.into_iter().map(ExtraPath::from_row).collect())
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

    // Step 2: reject self-overlap.
    reject_self_overlap(&canonical, &project.folder_path)?;

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
/// Does NOT pass `--prune-stale` — single-path syncs only add /
/// refresh entities for the path itself; pruning is a project-wide
/// concern handled by `reindex_*`.
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
    // it write into the project's codegraph collections.
    let label = row.label.clone().unwrap_or_else(|| basename_of(&row.path));
    let mut args: Vec<String> = vec![
        row.path.clone(),
        "--project".to_string(),
        prefix.clone(),
        "--json-progress".to_string(),
    ];
    if incremental {
        args.push("--incremental".to_string());
        if let Some(sha) = row.last_indexed_commit.as_ref() {
            // Agent B's analyzer extension supplies --since-commit;
            // if it lands later, the analyzer may not yet recognise
            // the flag — in which case it'll error 2 and we surface
            // the message. Per the v0.2.47 fan-out plan, Agent B
            // either ships the flag or documents the gap. We pass it
            // unconditionally; the worst case is a clear analyzer
            // error rather than a silent partial-sync.
            args.push("--since-commit".to_string());
            args.push(sha.clone());
        }
    }

    let started = std::time::Instant::now();
    let report = run_analyzer_with_stream(
        &project.folder_path,
        &project_id,
        &label,
        &args,
        &app,
    )
    .await?;
    let duration_ms = started.elapsed().as_millis() as u64;

    // Update last_indexed_*. SHA: best-effort; fall back to None if
    // git fails (non-git extra path) — the column update handles
    // None correctly (clears prior value, see DB doc).
    let now = chrono::Utc::now().timestamp_millis();
    let sha = git_head_sha(&row.path);
    let _ = db.update_codegraph_extra_last_indexed(
        &project_id,
        &row.path,
        now,
        sha.as_deref(),
    );

    // V52-Z (v0.2.52): also upsert `code_graph_builds` so the launcher's
    // "last successful build" UI reflects extra-path activity. Without
    // this the UI shows the last PRIMARY-folder analyzer run timestamp
    // even when an extra-path sync was the most recent activity (the
    // launcher.db audit log already records the path-level event, but
    // the GUI reads the timestamp from `code_graph_builds`).
    //
    // Schema constraint: `code_graph_builds` is PRIMARY KEY on
    // project_id (one row per project) → we UPSERT, overwriting the
    // previous row. This intentionally loses the primary-vs-extra
    // discrimination at this layer; if/when V52-O.5 introduces a
    // history table with a `source` discriminator column, this call
    // site is the second of two that need to migrate over.
    //
    // Best-effort: a failing upsert MUST NOT fail the user-visible
    // sync (the data is already in Weaviate; the row write is
    // bookkeeping). Errors flow through eprintln! via the helper's
    // signature contract.
    let started_at_ms = now - duration_ms as i64;
    if let Err(e) = db.upsert_code_graph_build(
        &project_id,
        "success",
        Some(started_at_ms),
        Some(now),
        Some(duration_ms as i64),
        report.files_analyzed as u32,
        None,           // languages: not surfaced by AnalyzerFinalReport here
        false,          // joern_used: extra-path sync doesn't pass --cfg/--pdg
        None,           // error_message: success path
        None,           // log_tail: not captured for the row-level UI
    ) {
        eprintln!(
            "[vct] warning: V52-Z code_graph_builds upsert failed for project {}: {}",
            project_id, e
        );
    }

    let outcome = SyncOutcome {
        files_scanned: report.files_analyzed,
        entities_indexed: report.modules + report.classes + report.functions + report.apis,
        duration_ms,
        project_codegraph_prefix: prefix,
        prune_stale: false,
        paths: vec![row.path.clone()],
    };

    let _ = db.audit(
        "codegraph_extra_path_synced",
        Some(&project_id),
        None,
        &serde_json::json!({
            "path": row.path,
            "files_scanned": outcome.files_scanned,
            "entities_indexed": outcome.entities_indexed,
            "duration_ms": outcome.duration_ms,
            "prune_stale": false,
        }),
    );

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
    let report = run_analyzer_with_stream(
        &project.folder_path,
        &project_id,
        &project.name,
        &args,
        &app,
    )
    .await?;
    let duration_ms = started.elapsed().as_millis() as u64;

    // After a successful reindex, refresh each extra's last_indexed_*
    // so subsequent incremental runs have an accurate baseline.
    let now = chrono::Utc::now().timestamp_millis();
    for e in &extras {
        let sha = git_head_sha(&e.path);
        let _ = db.update_codegraph_extra_last_indexed(
            &project_id,
            &e.path,
            now,
            sha.as_deref(),
        );
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
        false,          // joern_used: reindex doesn't pass --cfg/--pdg
        None,
        None,
    ) {
        eprintln!(
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

/// Resolve `python` to use for the analyzer. Mirrors
/// `codegraph_reanalyze::resolve_python_for_analyzer`. Walks up from
/// the launcher binary looking for a `.venv`; falls back to
/// `python3` / `python.exe` on PATH.
fn resolve_python_for_analyzer() -> Option<PathBuf> {
    let venv_in = |root: &Path| -> Option<PathBuf> {
        for layout in [
            root.join(".venv"),
            root.join("claude_mcp_servers").join(".venv"),
        ] {
            for candidate in [
                layout.join("bin").join("python"),
                layout.join("bin").join("python3"),
                layout.join("Scripts").join("python.exe"),
            ] {
                if candidate.is_file() {
                    return Some(candidate);
                }
            }
        }
        None
    };

    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let mut cur = parent.to_path_buf();
            for _ in 0..8 {
                if let Some(py) = venv_in(&cur) {
                    return Some(py);
                }
                if !cur.pop() {
                    break;
                }
            }
        }
    }
    Some(PathBuf::from(if cfg!(target_os = "windows") {
        "python.exe"
    } else {
        "python3"
    }))
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

    let mut cmd = tokio::process::Command::new(&python).silent();
    cmd.arg(&script);
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

    let timed = timeout(
        Duration::from_secs(ANALYZE_TIMEOUT_SECS),
        async {
            read_fut.await;
            child.wait().await
        },
    )
    .await;

    let status = match timed {
        Ok(Ok(s)) => s,
        Ok(Err(e)) => return Err(format!("wait analyzer: {}", e)),
        Err(_) => {
            let _ = child.start_kill();
            return Err(format!(
                "Analyze timed out after {}s; subprocess killed. Re-running is \
                 safe (idempotent) — unchanged files are skipped.",
                ANALYZE_TIMEOUT_SECS
            ));
        }
    };

    if !status.success() {
        let stderr_text = if let Some(err_stream) = stderr {
            read_stderr_capped(err_stream, 2048).await
        } else {
            String::new()
        };
        let code = status.code().unwrap_or(-1);
        return Err(format!(
            "analyzer exit {}: {}",
            code,
            if stderr_text.is_empty() {
                "no stderr".to_string()
            } else {
                stderr_text
            }
        ));
    }

    Ok(final_report.unwrap_or_default())
}

async fn read_stderr_capped(
    stderr: tokio::process::ChildStderr,
    max_bytes: usize,
) -> String {
    let mut buf = Vec::with_capacity(max_bytes.min(4096));
    let mut reader = BufReader::new(stderr);
    let mut limited = (&mut reader).take(max_bytes as u64);
    let _ = limited.read_to_end(&mut buf).await;
    String::from_utf8_lossy(&buf).into_owned()
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

    // ─── canonicalise_for_storage ───────────────────────────────────────

    #[test]
    fn canonicalise_rejects_empty() {
        let err = canonicalise_for_storage("").unwrap_err();
        assert!(err.contains("empty"), "got: {}", err);
        let err = canonicalise_for_storage("   ").unwrap_err();
        assert!(err.contains("empty"), "got: {}", err);
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

    #[test]
    fn reject_self_overlap_rejects_exact_match() {
        let err = reject_self_overlap("/opt/myproj", "/opt/myproj").unwrap_err();
        assert!(err.contains("project's own folder"), "got: {}", err);
    }

    #[test]
    fn reject_self_overlap_rejects_sub_path() {
        let err = reject_self_overlap("/opt/myproj/sub/dir", "/opt/myproj").unwrap_err();
        assert!(err.contains("INSIDE this project"), "got: {}", err);
    }

    #[test]
    fn reject_self_overlap_accepts_sibling_paths() {
        // sibling of project: NOT inside.
        reject_self_overlap("/opt/sibling", "/opt/myproj").unwrap();
        // sub-path that doesn't share a full segment boundary.
        reject_self_overlap("/opt/myprojz/sub", "/opt/myproj").unwrap();
    }

    #[test]
    fn reject_self_overlap_handles_trailing_slash() {
        // Both with trailing slash + one without → still detect overlap.
        let err = reject_self_overlap("/opt/myproj/", "/opt/myproj/").unwrap_err();
        assert!(err.contains("project's own folder"), "got: {}", err);
    }

    #[test]
    fn reject_self_overlap_normalises_backslashes() {
        // Folder path stored with backslashes (Windows DB row). Extra
        // path is already forward-slash form (post-canonicalisation).
        let err = reject_self_overlap("c:/opt/myproj/sub", "c:\\opt\\myproj").unwrap_err();
        assert!(err.contains("INSIDE this project"), "got: {}", err);
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
