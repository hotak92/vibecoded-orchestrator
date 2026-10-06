// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Tauri command surface for the chat-model context table (v0.2.92, WP-11).
//!
//! `launcher.db` is canonical (`vct_launcher_core::db::chat_model_context`,
//! migration 043); this layer owns the two things the DB layer deliberately
//! does not: FILE I/O and PATHS.
//!
//!   * **Seed** — read the ONE shipped seed file,
//!     `<orchestrator_root>/claude_mcp_servers/model_router/chat_model_context.seed.json`,
//!     the same file the gateway carries inside its own wheel as a fallback.
//!     Read, never copied: an `include_str!` reaching out of the crate — or a
//!     seed inlined into the migration SQL — would fork the shipped data into
//!     two copies with two update paths. On boot the seed CONVERGES the table
//!     with it per row: absent rows are inserted, untouched rows whose values
//!     drifted are refreshed (a shipped correction reaches existing installs,
//!     v0.2.98), user-edited rows are left byte-identical and deleted ids stay
//!     deleted.
//!   * **Export** — write `<vct_root>/model-gateway/chat_model_context.json`,
//!     which the gateway reads. On EVERY mutation and on EVERY boot, because
//!     a table edit that never reaches the file is a preference the daemon
//!     never sees.
//!
//! ## Why the export exists at all, when a hub route also serves the table
//!
//! The gateway is deliberately hub-independent: it must keep advertising the
//! right context windows when neither the launcher nor `vct-hub` is running.
//! A file it can `stat` costs it nothing and has no liveness requirement. The
//! hub route (`GET /api/v1/chat-model-context`) exists for CLIs and agents,
//! not for the gateway.
//!
//! ## The failure that must never be silent
//!
//! If the export write fails, the DB write has already committed — the user
//! sees their edit in the pane while the gateway keeps serving the old file.
//! So every mutating command returns the export outcome alongside its own
//! result and the pane renders the failure. The alternative (log-and-forget)
//! is the shipped-a-preference-nothing-consumes defect with a log line
//! attached.

use std::path::{Path, PathBuf};

use serde::Serialize;
use tauri::{command, State};

use vct_launcher_core::db::chat_model_context::{
    export_document, now_iso8601_utc, parse_document, ChatModelContextInput, ChatModelContextRow,
    ReseedOutcome, EXPORT_SCHEMA_VERSION,
};
use vct_launcher_core::paths::vct_root_dir;

use crate::db::Db;
use crate::json_file::{atomic_write_json, BackupPolicy};

// ─── Paths ────────────────────────────────────────────────────────────────

/// Subdirectory of `<vct_root>` holding the gateway's state files.
///
/// MUST MATCH `claude_mcp_servers/model_router/config.py` (`_STATE_SUBDIR`,
/// `_EXPORT_BASENAME`, and the `VCT_MODEL_GATEWAY_CONTEXT_TABLE` env name).
/// The two are held in lockstep by
/// `tests/test_v0292_chat_model_context_contract.py`, which reads both source
/// files and compares the literals — this is a deliberate (C)-tier mirror
/// under the repo's A>B>C rule: (A) shelling to Python to resolve a two-
/// segment path would make the launcher's boot seed depend on the gateway
/// package being importable, which is exactly backwards (the launcher writes
/// the file the gateway may never be installed to read).
const GATEWAY_STATE_SUBDIR: &str = "model-gateway";
const EXPORT_BASENAME: &str = "chat_model_context.json";
const EXPORT_PATH_ENV: &str = "VCT_MODEL_GATEWAY_CONTEXT_TABLE";

/// Where the shipped seed lives inside the orchestrator clone.
const SEED_RELATIVE_PATH: [&str; 3] = [
    "claude_mcp_servers",
    "model_router",
    "chat_model_context.seed.json",
];

/// Where the launcher WRITES the table for the gateway to read.
///
/// Honours `VCT_MODEL_GATEWAY_CONTEXT_TABLE` for the same reason the gateway
/// does: a user who redirects the table redirects BOTH ends of it, otherwise
/// the launcher would keep refreshing a file nothing reads while the gateway
/// waited on a file nothing writes. The two processes resolve it
/// independently from their own environments, so a mismatch is possible —
/// which is why `chat_model_context_status` reports the resolved path and
/// whether an override was in effect, making the mismatch visible rather
/// than mysterious.
pub fn export_path() -> PathBuf {
    if let Ok(custom) = std::env::var(EXPORT_PATH_ENV) {
        let trimmed = custom.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    vct_root_dir().join(GATEWAY_STATE_SUBDIR).join(EXPORT_BASENAME)
}

/// Absolute path of the shipped seed, or `None` when no orchestrator clone is
/// discoverable.
///
/// Resolution goes through `installer::resolve_orchestrator_root(db)` — the
/// canonical resolver, which reads `app_state['launcher.install_path']`
/// (seeded by `install.py`, which is why this works when the launcher binary
/// lives OUTSIDE the clone) and falls back to a `current_exe()` walk-up. No
/// `env!("CARGO_MANIFEST_DIR")`, no assumed layout: both would bake the build
/// host's path into a shipped binary.
pub fn seed_path(db: &Db) -> Option<PathBuf> {
    let root = crate::commands::installer::resolve_orchestrator_root(db)?;
    let mut p = root;
    for seg in SEED_RELATIVE_PATH {
        p = p.join(seg);
    }
    Some(p)
}

// ─── Seed loading ─────────────────────────────────────────────────────────

/// Read + parse the shipped seed. `Ok(None)` = "no clone / no file", which is
/// a normal state on a binary-only install and NOT an error: the gateway
/// ships the same seed inside its own wheel and serves it when no export
/// exists. `Err` = the file is there and unusable, which is a damaged install
/// and says so.
pub fn load_seed_rows(db: &Db) -> Result<Option<(PathBuf, Vec<ChatModelContextInput>)>, String> {
    let Some(path) = seed_path(db) else {
        return Ok(None);
    };
    if !path.exists() {
        return Ok(None);
    }
    let raw = std::fs::read_to_string(&path)
        .map_err(|e| format!("read shipped seed {}: {}", path.display(), e))?;
    let value: serde_json::Value = serde_json::from_str(&raw)
        .map_err(|e| format!("parse shipped seed {}: {}", path.display(), e))?;
    let rows = parse_document(&value)
        .map_err(|e| format!("shipped seed {} is not usable: {}", path.display(), e))?;
    Ok(Some((path, rows)))
}

// ─── Export ───────────────────────────────────────────────────────────────

/// What an export attempt did. `ok == false` is rendered by the pane: the
/// table and the file have diverged and the gateway is serving stale data.
#[derive(Debug, Clone, Serialize)]
pub struct ExportReport {
    pub ok: bool,
    /// Resolved absolute path, always reported (even on failure) so the
    /// user can look at the right file.
    pub path: String,
    /// `true` when `VCT_MODEL_GATEWAY_CONTEXT_TABLE` redirected the path.
    pub path_overridden_by_env: bool,
    pub models: usize,
    /// ISO-8601 UTC stamp written into the document, on success.
    pub generated_at: Option<String>,
    pub error: Option<String>,
}

/// Write the table to the gateway-readable file. Callers treat a failure as
/// reportable, never fatal: the DB is the source of truth and a later export
/// (next mutation, next boot) converges.
///
/// Goes through `json_file::atomic_write_json` (lock + temp + rename) rather
/// than hand-formatting, so the key order the launcher's `preserve_order`
/// `serde_json` produced is exactly what lands on disk, and a crash mid-write
/// cannot leave the gateway a truncated file.
///
/// Backup policy `Once`: on a normal machine the first export finds no file
/// and writes NO sidecar (`write_backup` returns early when the target does
/// not exist), so the directory stays clean. A sidecar appears only if
/// something was already at that path before VCO first wrote it — the
/// already-damaged case, and the one time a copy is worth keeping.
pub fn export_now(db: &Db) -> ExportReport {
    let path = export_path();
    let overridden = std::env::var(EXPORT_PATH_ENV)
        .map(|v| !v.trim().is_empty())
        .unwrap_or(false);

    let rows = match db.list_chat_model_context() {
        Ok(r) => r,
        Err(e) => {
            return ExportReport {
                ok: false,
                path: path.display().to_string(),
                path_overridden_by_env: overridden,
                models: 0,
                generated_at: None,
                error: Some(format!("could not read the table: {e}")),
            }
        }
    };
    // The tombstones ride along so the gateway's own seed fallback cannot
    // advertise a row this machine deleted (cross-lane contract, v0.2.94). A
    // read failure here degrades to "no tombstones" rather than failing the
    // export: the models are the load-bearing half, and an export that never
    // lands leaves the gateway on a stale file.
    let tombstones = db.list_chat_model_context_tombstones().unwrap_or_else(|e| {
        tracing::warn!("[vct] chat-model context: could not read tombstones: {}", e);
        Vec::new()
    });
    let generated_at = now_iso8601_utc();
    let doc = export_document(&rows, &tombstones, &generated_at);

    match atomic_write_json(&path, &doc, BackupPolicy::Once { ext: "pre-vco" }) {
        Ok(()) => ExportReport {
            ok: true,
            path: path.display().to_string(),
            path_overridden_by_env: overridden,
            models: rows.len(),
            generated_at: Some(generated_at),
            error: None,
        },
        Err(e) => ExportReport {
            ok: false,
            path: path.display().to_string(),
            path_overridden_by_env: overridden,
            models: rows.len(),
            generated_at: None,
            error: Some(format!(
                "could not write {}: {e}. The table is saved, but the model \
                 gateway will keep serving the previous file until this \
                 succeeds.",
                path.display()
            )),
        },
    }
}

// ─── Boot ─────────────────────────────────────────────────────────────────

/// The one-line boot summary of a converge, or `None` for a boot that wrote
/// nothing (a quiet boot stays quiet — no line, so `unchanged`-only runs do
/// not spam the log on every launch).
///
/// A RETIRE-ONLY converge is not quiet (v0.2.101, review nit 3): a row
/// vanished from the table and no pane shows a per-row trace (the reseed
/// toast carries the count, the retire log lines in the core module name
/// the ids). Extracted as a pure function so the fire/quiet boundary is
/// testable without a tracing subscriber.
fn converge_summary_line(path: &Path, outcome: &ReseedOutcome) -> Option<String> {
    if outcome.inserted == 0 && outcome.updated == 0 && outcome.retired == 0 {
        return None;
    }
    Some(format!(
        "[vct] chat-model context: converged with {}: {} new, {} refreshed, \
         {} retired, {} unchanged, {} user edit(s) preserved",
        path.display(),
        outcome.inserted,
        outcome.updated,
        outcome.retired,
        outcome.unchanged,
        outcome.preserved_user_edits
    ))
}

/// Boot converge + export, called once from `lib.rs::run`.
///
/// The converge brings the table in step with the shipped seed per row —
/// including on an UPGRADED install, which the first-boot-only gate this
/// path replaced could not touch (0.2.93 shipped four Claude rows that
/// reached no existing table) and including a CORRECTED row, which the
/// insert-if-absent-only rule could not reach either (v0.2.98 corrected the
/// eight qwen rows from 200K to the vendor's per-model 1M; until the boot
/// path learned the per-row rule, that correction landed only on fresh
/// installs and every existing table kept advertising the wrong window).
/// A row the user edited is left byte-identical and a deleted id stays
/// deleted; only the explicit "Reseed from shipped defaults" may undo
/// either. The export then runs unconditionally, so a table that changed
/// reaches the gateway in the same boot rather than waiting for the next
/// mutation.
///
/// Soft-fail end to end: nothing here may block the launcher from starting.
/// Every failure path logs a line a user can act on, and none of them leave
/// the table half-written (the converge validates the whole batch first, in
/// one transaction).
pub fn seed_and_export_on_boot(db: &Db) {
    match load_seed_rows(db) {
        Ok(Some((path, rows))) => match db.converge_chat_model_context_seed(&rows) {
            Ok(outcome) => {
                log_written_rows(PROVENANCE_SOURCE_CATALOG_SYNC, &outcome);
                if let Some(line) = converge_summary_line(&path, &outcome) {
                    tracing::info!("{}", line);
                }
            }
            Err(e) => tracing::warn!("[vct] chat-model context: seeding failed: {}", e),
        },
        Ok(None) => {
            // "No seed" hides TWO states, and the old line conflated them by
            // claiming the harmless one unconditionally.
            //
            //   * Empty table (a binary-only install): nothing to converge
            //     and nothing to export, so the gateway's own bundled copy —
            //     the same seed file, inside its wheel — answers every id.
            //     Harmless, and quiet.
            //   * Non-empty table: the rows stay exactly as they are. The
            //     export below runs unconditionally and the gateway prefers
            //     the export PER ROW (model_router/context_table.py), so this
            //     machine's rows — stale ones included — keep being served
            //     and no shipped correction can reach them. That is the state
            //     a deleted/renamed clone leaves behind, and it is a silent
            //     early-/compact for the user, so it warns.
            let rows = db.list_chat_model_context().map(|r| r.len()).unwrap_or(0);
            if rows == 0 {
                tracing::debug!(
                    "[vct] chat-model context: no shipped seed found (no orchestrator \
                     clone, or no seed file inside it); the table is empty, so the \
                     model gateway's bundled copy answers every id"
                );
            } else {
                tracing::warn!(
                    "[vct] chat-model context: no shipped seed found (no orchestrator \
                     clone, or no seed file inside it) while the table holds {} row(s) — \
                     they cannot be converged with the shipped defaults, and the export \
                     below keeps serving them. Re-run `python install.py` to restore the \
                     clone, then relaunch the launcher.",
                    rows
                );
            }
        }
        Err(e) => tracing::warn!(
            "[vct] chat-model context: {} — re-run `python install.py` if this persists",
            e
        ),
    }

    let report = export_now(db);
    if report.ok {
        tracing::info!(
            "[vct] chat-model context: exported {} model(s) to {}",
            report.models,
            report.path
        );
    } else {
        tracing::warn!(
            "[vct] chat-model context: export to {} failed: {}",
            report.path,
            report.error.as_deref().unwrap_or("unknown error")
        );
    }
}

// ─── Status ───────────────────────────────────────────────────────────────

/// What the pane shows above the table. Every field is read by the pane; none
/// is decorative.
#[derive(Debug, Clone, Serialize)]
pub struct ChatModelContextStatus {
    pub rows: usize,
    pub user_edited_rows: usize,
    pub export_path: String,
    pub export_path_overridden_by_env: bool,
    pub export_exists: bool,
    /// `generated_at` read back OUT of the file on disk — the honest answer
    /// to "what does the gateway currently see", which is not the same
    /// question as "when did we last try to write".
    pub export_generated_at: Option<String>,
    pub export_models: Option<usize>,
    /// Set when the file exists but the gateway will refuse it: unparseable,
    /// or a schema version this launcher did not write. Surfaced so the
    /// already-damaged case is visible in the GUI instead of only in the
    /// gateway's log.
    pub export_problem: Option<String>,
    pub seed_path: Option<String>,
    pub seed_available: bool,
    pub seed_problem: Option<String>,
}

fn inspect_export(path: &Path) -> (bool, Option<String>, Option<usize>, Option<String>) {
    if !path.exists() {
        return (false, None, None, None);
    }
    let raw = match std::fs::read_to_string(path) {
        Ok(r) => r,
        Err(e) => return (true, None, None, Some(format!("could not read it: {e}"))),
    };
    let value: serde_json::Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(e) => {
            return (
                true,
                None,
                None,
                Some(format!(
                    "it is not valid JSON ({e}); the gateway is ignoring it and \
                     serving its own bundled table. The next export overwrites it."
                )),
            )
        }
    };
    let generated_at = value
        .get("generated_at")
        .and_then(|v| v.as_str())
        .map(str::to_string);
    let version = value.get("schema_version").and_then(|v| v.as_u64());
    let models = value
        .get("models")
        .and_then(|v| v.as_object())
        .map(|m| m.len());
    let problem = match version {
        Some(v) if v == EXPORT_SCHEMA_VERSION => None,
        Some(v) => Some(format!(
            "it declares schema_version {v}, but this launcher writes \
             {EXPORT_SCHEMA_VERSION}; the gateway is ignoring it and serving its \
             own bundled table. The next export overwrites it."
        )),
        None => Some(
            "it has no schema_version; the gateway is ignoring it and serving its \
             own bundled table. The next export overwrites it."
                .to_string(),
        ),
    };
    (true, generated_at, models, problem)
}

// ─── Commands ─────────────────────────────────────────────────────────────

/// Result of a mutating command: what changed, and whether the gateway got
/// it. Both halves always travel together — see the module doc.
#[derive(Debug, Clone, Serialize)]
pub struct ChatModelContextMutation {
    pub row: Option<ChatModelContextRow>,
    pub deleted: bool,
    pub reseed: Option<ReseedOutcome>,
    pub export: ExportReport,
}

#[command]
pub async fn chat_model_context_list(
    db: State<'_, Db>,
) -> Result<Vec<ChatModelContextRow>, String> {
    db.list_chat_model_context()
}

#[command]
pub async fn chat_model_context_status(
    db: State<'_, Db>,
) -> Result<ChatModelContextStatus, String> {
    let rows = db.list_chat_model_context()?;
    let path = export_path();
    let (exists, generated_at, models, problem) = inspect_export(&path);

    let (seed_path_str, seed_available, seed_problem) = match load_seed_rows(&db) {
        Ok(Some((p, _))) => (Some(p.display().to_string()), true, None),
        Ok(None) => (
            seed_path(&db).map(|p| p.display().to_string()),
            false,
            None,
        ),
        Err(e) => (seed_path(&db).map(|p| p.display().to_string()), false, Some(e)),
    };

    Ok(ChatModelContextStatus {
        rows: rows.len(),
        user_edited_rows: rows.iter().filter(|r| r.user_edited).count(),
        export_path: path.display().to_string(),
        export_path_overridden_by_env: std::env::var(EXPORT_PATH_ENV)
            .map(|v| !v.trim().is_empty())
            .unwrap_or(false),
        export_exists: exists,
        export_generated_at: generated_at,
        export_models: models,
        export_problem: problem,
        seed_path: seed_path_str,
        seed_available,
        seed_problem,
    })
}

// The three mutators live as PLAIN FUNCTIONS taking `&Db`, with the `#[command]`
// wrappers below reduced to argument marshalling. That is what makes
// "every mutation re-exports" testable: a `#[command] async fn` takes
// `State<'_, Db>`, which a unit test cannot construct without a Tauri app, so
// keeping the logic in the command body would leave the export-on-mutation
// promise backed by nothing but a code reading.

// ─── v0.2.101 (Q6 / G1): the model-row provenance log ─────────────────────
//
// The owner saw a duplicate row in Claude Code's /model picker at each new
// Claude release (P300 G1 ≡ P299-A6b) and the producer was untraceable
// because no layer records WHERE a picker-shaping row came from. These
// table rows are that shape's persistent state: the gateway reads the
// exported table and advertises `window_1m` rows as `<id>[1m]`, so every
// writer of a row is a candidate producer of the next duplicate. ONE
// log-line shape, ONE home (this module — the layer that owns all three
// insertion paths), emitted to the launcher's tracing log:
//
//     [vct] model-picker row: model=<id> source=<source> action=<action>
//
// The three sources are the three writers below:
//   * `gateway-catalog-sync` — the boot converge of the shipped gateway
//     catalog seed (`seed_and_export_on_boot`);
//   * `gui-add`             — the Preferences pane's row editor
//     (`upsert_and_export`);
//   * `reseed-import`       — the "Reseed from shipped defaults" import
//     (`reseed_and_export`).
// Only rows actually WRITTEN log (a steady-state boot converges an
// unchanged table and stays silent), so the log names exactly the row
// appearances and changes a duplicate-trace needs.

/// Provenance source ids — see the module block above.
pub const PROVENANCE_SOURCE_CATALOG_SYNC: &str = "gateway-catalog-sync";
pub const PROVENANCE_SOURCE_GUI_ADD: &str = "gui-add";
pub const PROVENANCE_SOURCE_RESEED_IMPORT: &str = "reseed-import";

/// The ONE log-line shape (pure, so tests pin the format itself).
pub fn model_picker_row_provenance_line(
    source: &str,
    model_id: &str,
    action: &str,
) -> String {
    format!("[vct] model-picker row: model={model_id} source={source} action={action}")
}

fn log_model_picker_row_provenance(source: &str, model_id: &str, action: &str) {
    tracing::info!(
        "{}",
        model_picker_row_provenance_line(source, model_id, action)
    );
}

/// Log one line per row a converge/reseed pass actually wrote (the
/// `written` record the DB layer returns; empty on a steady-state boot).
fn log_written_rows(source: &str, outcome: &ReseedOutcome) {
    for (model_id, action) in &outcome.written {
        log_model_picker_row_provenance(source, model_id, action);
    }
}

/// Insert or update one row FROM THE GUI, so `user_edited = 1` — which is
/// what protects it from every automatic path that re-applies the shipped
/// rows (the boot converge and "Reseed from shipped defaults" alike) — then
/// re-export.
pub fn upsert_and_export(
    db: &Db,
    input: ChatModelContextInput,
) -> Result<ChatModelContextMutation, String> {
    let row = db.upsert_chat_model_context(input, true)?;
    log_model_picker_row_provenance(PROVENANCE_SOURCE_GUI_ADD, &row.model_id, "upsert");
    Ok(ChatModelContextMutation {
        row: Some(row),
        deleted: false,
        reseed: None,
        export: export_now(db),
    })
}

/// Delete one row, then re-export. A missing row is `deleted: false`, not an
/// error — but it still re-exports, so a stale file left by an earlier failed
/// export converges on the next attempt.
pub fn delete_and_export(db: &Db, model_id: &str) -> Result<ChatModelContextMutation, String> {
    let deleted = db.delete_chat_model_context(model_id.trim())?;
    Ok(ChatModelContextMutation {
        row: None,
        deleted,
        reseed: None,
        export: export_now(db),
    })
}

/// Re-apply the shipped rows, never over a user edit, then re-export.
///
/// Fails LOUDLY when the shipped seed cannot be found: the button's label
/// promises "from shipped defaults", and quietly doing nothing would be that
/// promise going unbacked.
pub fn reseed_and_export(db: &Db) -> Result<ChatModelContextMutation, String> {
    let rows = match load_seed_rows(db)? {
        Some((_, rows)) => rows,
        None => {
            return Err(
                "the shipped defaults file could not be located. It lives at \
                 claude_mcp_servers/model_router/chat_model_context.seed.json inside \
                 the orchestrator clone; this launcher could not resolve a clone, so \
                 there is nothing to reseed from. The model gateway still has its own \
                 bundled copy — this only affects editing the table here."
                    .to_string(),
            )
        }
    };
    let outcome = db.reseed_chat_model_context(&rows)?;
    log_written_rows(PROVENANCE_SOURCE_RESEED_IMPORT, &outcome);
    Ok(ChatModelContextMutation {
        row: None,
        deleted: false,
        reseed: Some(outcome),
        export: export_now(db),
    })
}

#[command]
pub async fn chat_model_context_upsert(
    input: ChatModelContextInput,
    db: State<'_, Db>,
) -> Result<ChatModelContextMutation, String> {
    upsert_and_export(&db, input)
}

#[command]
pub async fn chat_model_context_delete(
    model_id: String,
    db: State<'_, Db>,
) -> Result<ChatModelContextMutation, String> {
    delete_and_export(&db, &model_id)
}

#[command]
pub async fn chat_model_context_reseed(
    db: State<'_, Db>,
) -> Result<ChatModelContextMutation, String> {
    reseed_and_export(&db)
}

/// Force an export without changing anything — the pane's "Export now".
#[command]
pub async fn chat_model_context_export(db: State<'_, Db>) -> Result<ExportReport, String> {
    Ok(export_now(&db))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    /// In-memory launcher.db. NEVER opens the real `~/.vct/launcher.db`, and
    /// nothing here reaches hub-spawning code.
    fn make_db() -> Db {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        vct_launcher_core::db::migrations::apply(&conn).unwrap();
        Db(Mutex::new(conn))
    }

    fn input(model_id: &str) -> ChatModelContextInput {
        ChatModelContextInput {
            model_id: model_id.into(),
            vendor: "zai".into(),
            context_window: 200_000,
            max_output: 128_000,
            window_1m: false,
            text_only: false,
            source: "https://docs.z.ai/guides/llm/glm-5.1".into(),
            source_note: String::new(),
        }
    }

    /// Capture every tracing event `f` emits (message field only), so the
    /// provenance-emission tests assert REAL output rather than a code
    /// reading. Same shape as `upstream_fetch.rs`'s CaptureLogs.
    #[cfg(unix)]
    fn capture_tracing(f: impl FnOnce()) -> Vec<String> {
        struct MessageOf(String);
        impl tracing::field::Visit for MessageOf {
            fn record_debug(
                &mut self,
                field: &tracing::field::Field,
                value: &dyn std::fmt::Debug,
            ) {
                if field.name() == "message" {
                    self.0 = format!("{value:?}");
                }
            }
        }
        struct CaptureLogs(std::sync::Arc<std::sync::Mutex<Vec<String>>>);
        impl tracing::Subscriber for CaptureLogs {
            fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
                true
            }
            fn new_span(&self, _: &tracing::span::Attributes<'_>) -> tracing::span::Id {
                tracing::span::Id::from_u64(1)
            }
            fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}
            fn record_follows_from(
                &self,
                _: &tracing::span::Id,
                _: &tracing::span::Id,
            ) {
            }
            fn event(&self, event: &tracing::Event<'_>) {
                let mut m = MessageOf(String::new());
                event.record(&mut m);
                self.0.lock().unwrap().push(m.0);
            }
            fn enter(&self, _: &tracing::span::Id) {}
            fn exit(&self, _: &tracing::span::Id) {}
        }
        let logs = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let _sub = tracing::subscriber::set_default(CaptureLogs(logs.clone()));
        f();
        let out = logs.lock().unwrap().clone();
        out
    }

    /// A directory `resolve_orchestrator_root` accepts as a clone (install.py
    /// + CLAUDE.md + an `installed` manifest, which is what
    /// `check_install_status` gates the CACHED path on) but which carries NO
    /// shipped seed.
    ///
    /// Pinning this through `app_state` is the only way to make "the seed is
    /// unreachable" deterministic: the resolver's fallback walks up from
    /// `current_exe()`, so a test binary inside this checkout otherwise finds
    /// the real repo root and its real shipped seed.
    fn clone_without_seed(dir: &Path) -> PathBuf {
        let clone = dir.join("clone-without-seed");
        std::fs::create_dir_all(clone.join("state")).unwrap();
        std::fs::write(clone.join("install.py"), "# marker").unwrap();
        std::fs::write(clone.join("vct-module.json"), r#"{"id": "orchestrator"}"#).unwrap(); // W1R-06: identity-checked cache
        std::fs::write(clone.join("CLAUDE.md"), "# marker").unwrap();
        std::fs::write(
            clone.join("state").join("install-manifest.json"),
            r#"{"installed": true}"#,
        )
        .unwrap();
        clone
    }

    fn tmp_dir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!(
            "vct-cmc-{}-{}",
            tag,
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    // ── path resolution (OS-shape, not OS-specific) ──────────────────────

    /// The default path is `<vct_root>/model-gateway/chat_model_context.json`
    /// on every OS. Asserted as a SHAPE (last two components + parentage of
    /// `vct_root_dir()`), which is what makes it a tri-OS test rather than a
    /// Linux one: the separator and the home-directory convention are the
    /// only per-OS parts and both come from `vct_root_dir`.
    #[test]
    #[serial_test::serial]
    fn default_export_path_is_the_gateway_state_dir_under_vct_root() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        // Guard: another test in this binary may have set the override.
        std::env::remove_var(EXPORT_PATH_ENV);
        let p = export_path();
        assert_eq!(p.file_name().unwrap(), EXPORT_BASENAME);
        assert_eq!(p.parent().unwrap().file_name().unwrap(), GATEWAY_STATE_SUBDIR);
        assert_eq!(p.parent().unwrap().parent().unwrap(), vct_root_dir());
        assert!(p.is_absolute() || vct_root_dir().is_relative());
    }

    /// The env override the GATEWAY honours is honoured here too, so a user
    /// who redirects the table redirects both ends of it.
    #[test]
    #[serial_test::serial]
    fn export_path_honours_the_gateways_env_override() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let dir = tmp_dir("envpath");
        let custom = dir.join("elsewhere.json");
        std::env::set_var(EXPORT_PATH_ENV, &custom);
        assert_eq!(export_path(), custom);

        // Whitespace-only is treated as unset, matching config.py's
        // `.strip()` — otherwise a stray space would silently redirect the
        // export to a relative path.
        std::env::set_var(EXPORT_PATH_ENV, "   ");
        assert_eq!(export_path().file_name().unwrap(), EXPORT_BASENAME);

        std::env::remove_var(EXPORT_PATH_ENV);
        std::fs::remove_dir_all(&dir).ok();
    }

    /// The seed's location inside the clone is composed with `join`, never a
    /// literal `"a/b/c"` string, so it is correct on Windows too.
    #[test]
    fn seed_relative_path_is_composed_not_hardcoded_with_separators() {
        let root = PathBuf::from("ROOT");
        let mut p = root.clone();
        for seg in SEED_RELATIVE_PATH {
            p = p.join(seg);
        }
        assert_eq!(p.file_name().unwrap(), "chat_model_context.seed.json");
        assert_eq!(p.parent().unwrap().file_name().unwrap(), "model_router");
        assert!(
            !SEED_RELATIVE_PATH.iter().any(|s| s.contains('/') || s.contains('\\')),
            "path segments must not embed separators"
        );
    }

    // ── export ───────────────────────────────────────────────────────────

    #[test]
    #[serial_test::serial]
    fn export_writes_the_contract_shape_and_reports_where() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let dir = tmp_dir("export");
        let target = dir.join("model-gateway").join("chat_model_context.json");
        std::env::set_var(EXPORT_PATH_ENV, &target);

        let db = make_db();
        db.upsert_chat_model_context(input("glm-5.1"), false).unwrap();

        let report = export_now(&db);
        std::env::remove_var(EXPORT_PATH_ENV);

        assert!(report.ok, "export failed: {:?}", report.error);
        assert_eq!(report.models, 1);
        assert!(report.path_overridden_by_env);
        assert_eq!(report.path, target.display().to_string());

        let written: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&target).unwrap()).unwrap();
        assert_eq!(written["schema_version"], serde_json::json!(1));
        assert_eq!(written["source"], serde_json::json!("launcher.db"));
        assert_eq!(
            written["models"]["glm-5.1"]["context_window"],
            serde_json::json!(200_000)
        );
        // The parent directory is created — the gateway state dir need not
        // pre-exist on a fresh machine.
        assert!(target.parent().unwrap().is_dir());
        std::fs::remove_dir_all(&dir).ok();
    }

    /// ALREADY-DAMAGED: a hand-edited / stale / wrong-schema file at the
    /// export path is a VCO-owned artefact and is overwritten — but the
    /// bytes that were there first are preserved ONCE, so the overwrite is
    /// recoverable.
    #[test]
    #[serial_test::serial]
    fn export_overwrites_a_damaged_file_and_keeps_the_pre_vco_bytes_once() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let dir = tmp_dir("damaged");
        let target = dir.join("chat_model_context.json");
        std::fs::write(&target, "{ not json at all").unwrap();
        std::env::set_var(EXPORT_PATH_ENV, &target);

        let db = make_db();
        db.upsert_chat_model_context(input("glm-5.1"), false).unwrap();
        let report = export_now(&db);

        // A second export must NOT overwrite the preserved original with a
        // VCO-written copy.
        db.upsert_chat_model_context(input("glm-5.2"), false).unwrap();
        let second = export_now(&db);
        std::env::remove_var(EXPORT_PATH_ENV);

        assert!(report.ok && second.ok);
        let written: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&target).unwrap()).unwrap();
        assert_eq!(written["models"].as_object().unwrap().len(), 2);
        assert_eq!(
            std::fs::read_to_string(dir.join("chat_model_context.json.pre-vco")).unwrap(),
            "{ not json at all",
            "the first-seen bytes are kept once, not replaced on every write"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    /// A fresh machine gets NO sidecar: `BackupPolicy::Once` only copies a
    /// file that already exists, so the gateway state dir stays clean.
    #[test]
    #[serial_test::serial]
    fn a_first_export_on_a_clean_machine_writes_no_sidecar() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let dir = tmp_dir("clean");
        let target = dir.join("chat_model_context.json");
        std::env::set_var(EXPORT_PATH_ENV, &target);
        let db = make_db();
        db.upsert_chat_model_context(input("glm-5.1"), false).unwrap();
        assert!(export_now(&db).ok);
        std::env::remove_var(EXPORT_PATH_ENV);

        let entries: Vec<String> = std::fs::read_dir(&dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .collect();
        assert_eq!(
            entries,
            vec!["chat_model_context.json"],
            "no backup, no leftover temp or lock file"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    /// A failing write is REPORTED, not swallowed: the table is saved but
    /// the gateway has not seen it, and the user is told exactly that.
    #[test]
    #[serial_test::serial]
    fn a_failing_export_is_reported_rather_than_logged_and_forgotten() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let dir = tmp_dir("failwrite");
        // Point the export at a path whose "parent" is a FILE, so
        // create_dir_all cannot succeed on any OS.
        let blocker = dir.join("not-a-dir");
        std::fs::write(&blocker, "x").unwrap();
        std::env::set_var(EXPORT_PATH_ENV, blocker.join("chat_model_context.json"));

        let db = make_db();
        db.upsert_chat_model_context(input("glm-5.1"), false).unwrap();
        let report = export_now(&db);
        std::env::remove_var(EXPORT_PATH_ENV);

        assert!(!report.ok);
        let msg = report.error.expect("a failure must carry its reason");
        assert!(
            msg.contains("keep serving the previous file"),
            "the message must say what the gateway will do, got: {}",
            msg
        );
        // The row is still in the DB — the export failure did not roll it back.
        assert_eq!(db.list_chat_model_context().unwrap().len(), 1);
        std::fs::remove_dir_all(&dir).ok();
    }

    // ── boot summary (v0.2.101, review nit 3) ────────────────────────────

    /// A RETIRE-ONLY converge is not a quiet boot: the row vanished, no pane
    /// shows it, and the summary line is the only trace. It must fire and
    /// must carry the retired count.
    #[test]
    fn the_boot_summary_fires_on_a_retire_only_converge_and_names_the_count() {
        let line = converge_summary_line(
            Path::new("/clone/claude_mcp_servers/model_router/chat_model_context.seed.json"),
            &ReseedOutcome {
                inserted: 0,
                updated: 0,
                unchanged: 20,
                preserved_user_edits: 1,
                retired: 1,
                written: Vec::new(),
            },
        )
        .expect("a retire-only converge must produce a summary line");
        assert!(line.contains("1 retired"), "got: {}", line);
        assert!(line.contains("converged with /clone"), "got: {}", line);
        assert!(line.contains("1 user edit(s) preserved"), "got: {}", line);
    }

    /// A boot that wrote nothing stays quiet — the unchanged/preserved
    /// counters alone never produce a line, or every launch would log.
    #[test]
    fn the_boot_summary_stays_quiet_when_nothing_was_written() {
        assert!(converge_summary_line(
            Path::new("/clone/seed.json"),
            &ReseedOutcome {
                inserted: 0,
                updated: 0,
                unchanged: 21,
                preserved_user_edits: 0,
                retired: 0,
                written: Vec::new(),
            }
        )
        .is_none());
    }

    /// The pre-existing half: a converge that only inserted or refreshed
    /// still fires (the condition is ANY of the three write kinds).
    #[test]
    fn the_boot_summary_fires_on_writes_without_retires_too() {
        for outcome in [
            ReseedOutcome { inserted: 2, updated: 0, unchanged: 0, preserved_user_edits: 0, retired: 0, written: Vec::new() },
            ReseedOutcome { inserted: 0, updated: 1, unchanged: 3, preserved_user_edits: 0, retired: 0, written: Vec::new() },
        ] {
            assert!(
                converge_summary_line(Path::new("/s"), &outcome).is_some(),
                "a converge that wrote must not be silenced: {:?}",
                outcome
            );
        }
    }

    // ── every mutation re-exports (R16 item 4) ───────────────────────────

    /// A table edit that never reaches the file is a preference the gateway
    /// never sees. Each mutator is proven to re-export, and to REPORT what it
    /// exported — not merely to have an `export_now` call in its body.
    #[test]
    #[serial_test::serial]
    fn every_mutation_re_exports_and_says_so() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let dir = tmp_dir("mutations");
        let clone = dir.join("clone");
        let seed_dir = clone.join("claude_mcp_servers").join("model_router");
        std::fs::create_dir_all(&seed_dir).unwrap();
        std::fs::write(clone.join("install.py"), "# marker").unwrap();
        std::fs::write(clone.join("vct-module.json"), r#"{"id": "orchestrator"}"#).unwrap(); // W1R-06: identity-checked cache
        std::fs::write(clone.join("CLAUDE.md"), "# marker").unwrap();
        std::fs::create_dir_all(clone.join("state")).unwrap();
        std::fs::write(
            clone.join("state").join("install-manifest.json"),
            r#"{"installed": true}"#,
        )
        .unwrap();
        std::fs::write(
            seed_dir.join("chat_model_context.seed.json"),
            r#"{"schema_version": 1, "models": {
                 "glm-5.2": {"vendor": "zai", "context_window": 1000000,
                             "max_output": 128000, "window_1m": true,
                             "source": "https://docs.z.ai/guides/llm/glm-5.2"}}}"#,
        )
        .unwrap();

        let db = make_db();
        db.app_state_set("launcher.install_path", &clone.display().to_string())
            .unwrap();

        let target = dir.join("chat_model_context.json");
        std::env::set_var(EXPORT_PATH_ENV, &target);

        let models_on_disk = || -> usize {
            let v: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(&target).unwrap()).unwrap();
            v["models"].as_object().unwrap().len()
        };

        // UPSERT → the row is in the file the gateway reads.
        let m = upsert_and_export(&db, input("glm-5.1")).unwrap();
        assert!(m.export.ok, "{:?}", m.export.error);
        assert_eq!(m.export.models, 1);
        assert_eq!(models_on_disk(), 1);
        assert!(m.row.expect("upsert returns the row").user_edited);

        // RESEED → the shipped row lands in the file too.
        let m = reseed_and_export(&db).unwrap();
        assert!(m.export.ok);
        assert_eq!(m.reseed.expect("reseed reports what it did").inserted, 1);
        assert_eq!(models_on_disk(), 2);

        // DELETE → the removal reaches the file, not just the table.
        let m = delete_and_export(&db, "glm-5.1").unwrap();
        assert!(m.export.ok && m.deleted);
        assert_eq!(models_on_disk(), 1);

        // Deleting something absent is not an error, and still re-exports —
        // which is how a file left stale by an earlier failed export heals.
        let m = delete_and_export(&db, "not-there").unwrap();
        assert!(!m.deleted && m.export.ok);
        assert_eq!(models_on_disk(), 1);

        std::env::remove_var(EXPORT_PATH_ENV);
        std::fs::remove_dir_all(&dir).ok();
    }

    /// The reseed button's label promises "from shipped defaults". With no
    /// defaults reachable it must FAIL LOUDLY rather than report a no-op
    /// success — an unbacked promise is the defect, not the missing file.
    ///
    /// The clone is PINNED via `app_state['launcher.install_path']` rather
    /// than left to discovery. `resolve_orchestrator_root`'s fallback walks
    /// up from `current_exe()`, and a test binary inside this checkout finds
    /// the REAL repo root — and its real seed. A test that relied on "no
    /// clone is discoverable" would assert nothing here while still passing,
    /// which is the test-name-promises-more failure mode.
    #[test]
    #[serial_test::serial]
    fn reseed_with_no_shipped_seed_file_fails_loudly() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let dir = tmp_dir("noseed");
        let clone = clone_without_seed(&dir);
        std::env::set_var(EXPORT_PATH_ENV, dir.join("chat_model_context.json"));
        let db = make_db();
        db.app_state_set("launcher.install_path", &clone.display().to_string())
            .unwrap();
        let err = reseed_and_export(&db).expect_err("must not report success");
        std::env::remove_var(EXPORT_PATH_ENV);

        assert!(err.contains("chat_model_context.seed.json"), "got: {}", err);
        assert!(
            err.contains("bundled copy"),
            "the message must say the gateway is still fine, got: {}",
            err
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    // ── status ───────────────────────────────────────────────────────────

    #[test]
    #[serial_test::serial]
    fn status_reports_a_missing_export_without_inventing_a_problem() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let dir = tmp_dir("statusmissing");
        std::env::set_var(EXPORT_PATH_ENV, dir.join("chat_model_context.json"));
        let (exists, gen, models, problem) = inspect_export(&export_path());
        std::env::remove_var(EXPORT_PATH_ENV);

        assert!(!exists);
        assert_eq!(gen, None);
        assert_eq!(models, None);
        assert_eq!(
            problem, None,
            "an absent export is the normal state on a fresh machine, not a fault"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn status_names_an_unparseable_or_wrong_schema_export() {
        let dir = tmp_dir("statusbad");

        let broken = dir.join("broken.json");
        std::fs::write(&broken, "{{{").unwrap();
        let (exists, _, _, problem) = inspect_export(&broken);
        assert!(exists);
        assert!(problem.unwrap().contains("not valid JSON"));

        let future = dir.join("future.json");
        std::fs::write(
            &future,
            r#"{"schema_version": 99, "generated_at": "t", "models": {}}"#,
        )
        .unwrap();
        let (_, gen, models, problem) = inspect_export(&future);
        assert_eq!(gen.as_deref(), Some("t"));
        assert_eq!(models, Some(0));
        assert!(
            problem.unwrap().contains("schema_version 99"),
            "an unknown schema version is a NAMED condition, distinct from malformed"
        );

        let ok = dir.join("ok.json");
        std::fs::write(
            &ok,
            r#"{"schema_version": 1, "generated_at": "2026-09-02T00:00:00Z", "models": {"a": {}}}"#,
        )
        .unwrap();
        let (_, gen, models, problem) = inspect_export(&ok);
        assert_eq!(gen.as_deref(), Some("2026-09-02T00:00:00Z"));
        assert_eq!(models, Some(1));
        assert_eq!(problem, None, "a good file must not be flagged");

        std::fs::remove_dir_all(&dir).ok();
    }

    // ── the shipped seed, parsed by the code that will parse it ──────────

    /// The REAL shipped seed loads under the launcher's parser, with the ten
    /// cited GLM rows, the four first-party Claude 5 rows (read only by
    /// `vco_lib.vscode_settings.decorate_1m`; the gateway publishes
    /// first-party ids verbatim), the eight v0.2.98-CORRECTED qwen Token-Plan
    /// rows (the vendor's PER-MODEL figures — 1M window, `[1m]`, stated
    /// `max_output`; until v0.2.98 they carried the integration page's
    /// CLIENT-default 200K and an unstated 0 output) and the version-key
    /// evidence intact.
    ///
    /// This reads the repo file directly via `CARGO_MANIFEST_DIR`, which is
    /// compile-time-only path resolution INSIDE `#[cfg(test)]` (the same
    /// sanctioned use as `commands::module_gui`'s privacy test). No
    /// production path resolves this way — `seed_path()` goes through
    /// `resolve_orchestrator_root`.
    #[test]
    fn the_shipped_seed_parses_under_the_launchers_own_parser() {
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..");
        let mut seed = repo_root;
        for seg in SEED_RELATIVE_PATH {
            seed = seed.join(seg);
        }
        let raw = std::fs::read_to_string(&seed)
            .unwrap_or_else(|e| panic!("shipped seed {} unreadable: {}", seed.display(), e));
        let value: serde_json::Value = serde_json::from_str(&raw).expect("seed is valid JSON");
        let rows = parse_document(&value).expect("seed parses under the writer's rules");

        assert_eq!(
            rows.len(),
            21,
            "the cited GLM rows of handoff §7 minus glm-5.2 (owner-retired in \
             v0.2.100), the four Claude 5 rows, and the eight v0.2.96 qwen \
             Token-Plan rows"
        );
        assert!(
            rows.iter().all(|r| r.model_id != "glm-5.2"),
            "glm-5.2 is retired (owner, v0.2.100): the seed must not list it"
        );
        assert!(
            rows.iter().all(|r| !r.source.trim().is_empty()),
            "R10: no row without a cited source"
        );

        let by_id = |id: &str| rows.iter().find(|r| r.model_id == id).unwrap_or_else(|| {
            panic!("seed is missing {}", id)
        });
        // The first-party rows: vendor `anthropic`, 1M, cited to Anthropic's
        // docs. They exist for the settings writer's [1m] decoration only.
        for id in ["claude-fable-5-1", "claude-fable-5", "claude-opus-5", "claude-sonnet-5"] {
            let row = by_id(id);
            assert_eq!(row.vendor, "anthropic", "{}", id);
            assert!(row.window_1m && row.context_window == 1_000_000, "{}", id);
            assert!(row.source.starts_with("https://docs.anthropic.com"), "{}", id);
        }
        // The v0.2.98-corrected qwen Token-Plan rows: vendor `qwen`, the
        // vendor's PER-MODEL figures from the cited page — 1M window with
        // the [1m] companion, and a STATED max_output (never the invented
        // figure the pre-correction row refused to guess). Until v0.2.98
        // these rows carried 200K / window_1m false / max_output 0 because
        // the citation was the integration page's sentence about the
        // CLIENT's default window.
        let qwen: Vec<&ChatModelContextInput> =
            rows.iter().filter(|r| r.vendor == "qwen").collect();
        assert_eq!(qwen.len(), 8, "the eight Token-Plan rows; no PAYG row exists");
        for row in &qwen {
            assert_eq!(
                row.context_window, 1_000_000,
                "{}: the vendor's per-model window, not the client default",
                row.model_id
            );
            assert!(
                row.window_1m,
                "{}: the vendor's own table states the 1M window",
                row.model_id
            );
            assert!(
                row.max_output > 0,
                "{}: a stated figure since v0.2.98 — 0 would mean the \
                 correction was reverted",
                row.model_id
            );
            assert!(
                row.source.starts_with("https://docs.qwencloud.com/"),
                "{}: cited to the vendor docs, got {}",
                row.model_id,
                row.source
            );
        }
        // The correction's own evidence travels with the primary row.
        assert!(
            qwen.iter()
                .any(|r| r.source_note.contains("v0.2.98")
                    && r.source_note.contains("CLIENT's default window")),
            "the qwen3.8-max note must say where the old 200K came from, so a \
             future editor does not 'correct' the row backwards"
        );
        // The stated output figures are the vendor's, expanded by the
        // non-overstating decimal reading documented in the seed.
        assert_eq!(by_id("qwen3.8-max").max_output, 128_000);
        assert_eq!(by_id("qwen3.7-max").max_output, 64_000);
        assert_eq!(by_id("deepseek-v4-pro").max_output, 393_216);
        // The version-key evidence: same family, 5x apart. If a future edit
        // ever collapses these into a `glm-5*` rule, this reds.
        assert!(by_id("glm-5.3").window_1m && by_id("glm-5.3").context_window == 1_000_000);
        assert!(!by_id("glm-5.1").window_1m && by_id("glm-5.1").context_window == 200_000);
        // The honest citation caveat survives into the rows we will store.
        let air = by_id("glm-4.5-air");
        assert!(
            air.source_note.contains("404") && air.source_note.contains("NOT official"),
            "the glm-4.5-air caveat must reach the DB, or a future editor \
             'corrects' the row backwards from a third-party listing: {:?}",
            air.source_note
        );
        assert!(!air.window_1m, "no unofficial 1M glm-4.5-air");
    }

    /// CONSUMER-LEVEL CONTRACT (v0.2.96): an UNSTATED row never surfaces a
    /// max-output figure anywhere along the launcher's own pipeline. A seed
    /// carrying `max_output: 0` rows goes through parse → table → exported
    /// file, and the file the gateway reads carries the 0 marker verbatim:
    /// never an invented count, never an absent key (the reader's `or 0`
    /// coalescing means absent and 0 must not diverge). The leave-alone half
    /// pins that a row WITH a published figure keeps it.
    ///
    /// Since v0.2.98 the REAL shipped seed states a figure on every qwen
    /// row, so this pins the contract on a synthetic clone seed — the
    /// contract itself is still live: `upsert_chat_model_context` accepts a
    /// 0 row and the export must keep carrying it as 0.
    #[test]
    #[serial_test::serial]
    fn unstated_rows_flow_from_a_seed_into_the_export_as_zero() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let dir = tmp_dir("unstated");
        let clone = dir.join("clone");
        let seed_dir = clone.join("claude_mcp_servers").join("model_router");
        std::fs::create_dir_all(&seed_dir).unwrap();
        std::fs::write(clone.join("install.py"), "# marker").unwrap();
        std::fs::write(clone.join("vct-module.json"), r#"{"id": "orchestrator"}"#).unwrap(); // W1R-06: identity-checked cache
        std::fs::write(clone.join("CLAUDE.md"), "# marker").unwrap();
        std::fs::create_dir_all(clone.join("state")).unwrap();
        std::fs::write(
            clone.join("state").join("install-manifest.json"),
            r#"{"installed": true}"#,
        )
        .unwrap();
        std::fs::write(
            seed_dir.join("chat_model_context.seed.json"),
            r#"{"schema_version": 1, "models": {
                 "qwen3.8-max": {"vendor": "qwen", "context_window": 1000000,
                                 "max_output": 0, "window_1m": true,
                                 "source": "https://docs.qwencloud.com/x",
                                 "source_note": "vendor page states no figure"},
                 "glm-5.2": {"vendor": "zai", "context_window": 1000000,
                             "max_output": 128000, "window_1m": true,
                             "source": "https://docs.z.ai/guides/llm/glm-5.2"}}}"#,
        )
        .unwrap();

        let db = make_db();
        db.app_state_set("launcher.install_path", &clone.display().to_string())
            .unwrap();
        let (_, rows) = load_seed_rows(&db)
            .expect("seed loads")
            .expect("the clone is pinned, so the seed is found");
        assert_eq!(
            db.converge_chat_model_context_seed(&rows).unwrap().inserted,
            2,
            "every seed row — the unstated one included — stores through the \
             relaxed CHECK (migration 046)"
        );

        let target = dir.join("chat_model_context.json");
        std::env::set_var(EXPORT_PATH_ENV, &target);
        let report = export_now(&db);
        std::env::remove_var(EXPORT_PATH_ENV);
        assert!(report.ok, "export failed: {:?}", report.error);

        let written: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&target).unwrap()).unwrap();
        let models = written["models"].as_object().unwrap();
        let entry = models
            .get("qwen3.8-max")
            .expect("the export is not missing the unstated row");
        assert_eq!(
            entry["max_output"],
            serde_json::json!(0),
            "the export carries the unstated marker, never a figure"
        );
        assert!(
            entry.as_object().unwrap().contains_key("max_output"),
            "the key is present — absent and 0 must mean the same thing to \
             the reader, and the file says which one it wrote"
        );
        // Leave-alone: a row WITH a published figure keeps it exactly.
        assert_eq!(
            models["glm-5.2"]["max_output"],
            serde_json::json!(128_000),
            "a stated figure is untouched by the unstated-marker contract"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    /// The v0.2.98 delivery consequence, end to end on a synthetic clone: an
    /// EXISTING install whose table still holds the pre-correction row an
    /// earlier boot seeded (200K, no [1m], never user-edited) gets the
    /// corrected shipped row on the next boot, and the FILE THE GATEWAY
    /// READS carries it — so the `[1m]` advertisement follows the
    /// correction without anyone pressing "Reseed from shipped defaults".
    #[test]
    #[serial_test::serial]
    fn boot_converges_a_corrected_shipped_row_into_the_export() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let dir = tmp_dir("converge");
        let clone = dir.join("clone");
        let seed_dir = clone.join("claude_mcp_servers").join("model_router");
        std::fs::create_dir_all(&seed_dir).unwrap();
        std::fs::write(clone.join("install.py"), "# marker").unwrap();
        std::fs::write(clone.join("vct-module.json"), r#"{"id": "orchestrator"}"#).unwrap(); // W1R-06: identity-checked cache
        std::fs::write(clone.join("CLAUDE.md"), "# marker").unwrap();
        std::fs::create_dir_all(clone.join("state")).unwrap();
        std::fs::write(
            clone.join("state").join("install-manifest.json"),
            r#"{"installed": true}"#,
        )
        .unwrap();
        std::fs::write(
            seed_dir.join("chat_model_context.seed.json"),
            r#"{"schema_version": 1, "models": {
                 "qwen3.8-max": {"vendor": "qwen", "context_window": 1000000,
                                 "max_output": 128000, "window_1m": true,
                                 "source": "https://docs.qwencloud.com/developer-guides/getting-started/text-generation-models"}}}"#,
        )
        .unwrap();

        let db = make_db();
        db.app_state_set("launcher.install_path", &clone.display().to_string())
            .unwrap();
        // The pre-v0.2.98 row, exactly as an earlier boot seeded it: 200K,
        // no [1m], an unstated 0 output, `user_edited = 0`.
        db.upsert_chat_model_context(
            ChatModelContextInput {
                model_id: "qwen3.8-max".into(),
                vendor: "qwen".into(),
                context_window: 200_000,
                max_output: 0,
                window_1m: false,
                text_only: false,
                source: "https://docs.qwencloud.com/".into(),
                source_note: String::new(),
            },
            false,
        )
        .unwrap();

        let target = dir.join("chat_model_context.json");
        std::env::set_var(EXPORT_PATH_ENV, &target);
        seed_and_export_on_boot(&db);
        std::env::remove_var(EXPORT_PATH_ENV);

        let row = db.get_chat_model_context("qwen3.8-max").unwrap().unwrap();
        assert_eq!(row.context_window, 1_000_000, "the table converges");
        assert!(row.window_1m);
        assert!(!row.user_edited, "still not a user edit");
        let written: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&target).unwrap()).unwrap();
        assert_eq!(
            written["models"]["qwen3.8-max"]["context_window"],
            serde_json::json!(1_000_000),
            "the correction reaches the file the gateway reads"
        );
        assert_eq!(
            written["models"]["qwen3.8-max"]["window_1m"],
            serde_json::json!(true),
            "so the gateway advertises the <id>[1m] companion"
        );
        assert_eq!(
            written["models"]["qwen3.8-max"]["max_output"],
            serde_json::json!(128_000)
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    /// End-to-end on a synthetic clone: seed → table → export, with the
    /// clone root discovered exactly the way production discovers it
    /// (`app_state['launcher.install_path']`, which `install.py` seeds).
    #[test]
    #[serial_test::serial]
    fn boot_seeds_from_the_clone_and_exports_in_one_pass() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let dir = tmp_dir("boot");
        let clone = dir.join("some-odd-place").join("orchestrator");
        let seed_dir = clone.join("claude_mcp_servers").join("model_router");
        std::fs::create_dir_all(&seed_dir).unwrap();
        // `resolve_orchestrator_root`'s cached-path branch validates the
        // path with `check_install_status` (install.py + CLAUDE.md).
        std::fs::write(clone.join("install.py"), "# marker").unwrap();
        std::fs::write(clone.join("vct-module.json"), r#"{"id": "orchestrator"}"#).unwrap(); // W1R-06: identity-checked cache
        std::fs::write(clone.join("CLAUDE.md"), "# marker").unwrap();
        // `resolve_orchestrator_root`'s cached-path branch also requires
        // `check_install_status` to pass, which wants an install manifest
        // (or a `.venv/`). Write the manifest — the cheaper of the two.
        std::fs::create_dir_all(clone.join("state")).unwrap();
        std::fs::write(
            clone.join("state").join("install-manifest.json"),
            r#"{"installed": true}"#,
        )
        .unwrap();
        std::fs::write(
            seed_dir.join("chat_model_context.seed.json"),
            r#"{"schema_version": 1, "generated_at": "2026-09-02T00:00:00Z",
                "source": "shipped-seed",
                "_comment": "skipped by both parsers",
                "models": {
                  "glm-5.2": {"vendor": "zai", "context_window": 1000000,
                              "max_output": 128000, "window_1m": true,
                              "source": "https://docs.z.ai/guides/llm/glm-5.2"},
                  "glm-5.1": {"vendor": "zai", "context_window": 200000,
                              "max_output": 128000, "window_1m": false,
                              "source": "https://docs.z.ai/guides/llm/glm-5.1"}}}"#,
        )
        .unwrap();

        let db = make_db();
        db.app_state_set("launcher.install_path", &clone.display().to_string())
            .unwrap();

        let target = dir.join("model-gateway").join("chat_model_context.json");
        std::env::set_var(EXPORT_PATH_ENV, &target);
        seed_and_export_on_boot(&db);

        assert_eq!(db.list_chat_model_context().unwrap().len(), 2);
        let written: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&target).unwrap()).unwrap();
        assert_eq!(written["models"]["glm-5.2"]["window_1m"], serde_json::json!(true));
        assert_eq!(written["models"]["glm-5.1"]["window_1m"], serde_json::json!(false));

        // SECOND BOOT after a user edit: the converge still runs, but the
        // `user_edited` row is preserved byte-identically, and the export is
        // refreshed.
        db.upsert_chat_model_context(
            ChatModelContextInput { context_window: 777, ..input("glm-5.1") },
            true,
        )
        .unwrap();
        seed_and_export_on_boot(&db);
        std::env::remove_var(EXPORT_PATH_ENV);

        let written: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&target).unwrap()).unwrap();
        assert_eq!(
            written["models"]["glm-5.1"]["context_window"],
            serde_json::json!(777),
            "a boot must never silently revert a user edit"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    /// No shipped seed reachable (a binary-only install, or a clone without
    /// the gateway package): boot must not fail, must seed NOTHING, and must
    /// still export — an empty export is a valid document, and writing it is
    /// what keeps the file in step after a user empties the table.
    ///
    /// The clone is pinned rather than discovered, for the reason spelled out
    /// on `reseed_with_no_shipped_seed_file_fails_loudly`.
    #[test]
    #[serial_test::serial]
    fn boot_with_no_shipped_seed_is_quiet_and_still_exports() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let dir = tmp_dir("noseedboot");
        let clone = clone_without_seed(&dir);
        let target = dir.join("chat_model_context.json");
        std::env::set_var(EXPORT_PATH_ENV, &target);

        let db = make_db();
        db.app_state_set("launcher.install_path", &clone.display().to_string())
            .unwrap();
        seed_and_export_on_boot(&db);
        std::env::remove_var(EXPORT_PATH_ENV);

        assert!(
            db.list_chat_model_context().unwrap().is_empty(),
            "nothing to seed from means nothing seeded — not a partial table"
        );
        assert!(target.exists(), "an empty table still produces a valid export");
        let written: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&target).unwrap()).unwrap();
        assert_eq!(written["schema_version"], serde_json::json!(1));
        assert!(written["models"].as_object().unwrap().is_empty());
        std::fs::remove_dir_all(&dir).ok();
    }

    /// A DAMAGED shipped seed is an error with a path in it, not a silent
    /// skip: the file ships with the code, so a bad one is a broken install.
    #[test]
    fn a_damaged_shipped_seed_is_reported_and_seeds_nothing() {
        let dir = tmp_dir("badseed");
        let clone = dir.join("clone");
        let seed_dir = clone.join("claude_mcp_servers").join("model_router");
        std::fs::create_dir_all(&seed_dir).unwrap();
        std::fs::write(clone.join("install.py"), "# marker").unwrap();
        std::fs::write(clone.join("vct-module.json"), r#"{"id": "orchestrator"}"#).unwrap(); // W1R-06: identity-checked cache
        std::fs::write(clone.join("CLAUDE.md"), "# marker").unwrap();
        // `resolve_orchestrator_root`'s cached-path branch also requires
        // `check_install_status` to pass, which wants an install manifest
        // (or a `.venv/`). Write the manifest — the cheaper of the two.
        std::fs::create_dir_all(clone.join("state")).unwrap();
        std::fs::write(
            clone.join("state").join("install-manifest.json"),
            r#"{"installed": true}"#,
        )
        .unwrap();
        std::fs::write(seed_dir.join("chat_model_context.seed.json"), "{ oops").unwrap();

        let db = make_db();
        db.app_state_set("launcher.install_path", &clone.display().to_string())
            .unwrap();

        let err = load_seed_rows(&db).expect_err("a broken seed must be an error");
        assert!(err.contains("parse shipped seed"), "got: {}", err);
        assert!(err.contains("chat_model_context.seed.json"), "must name the file: {}", err);
        assert!(db.list_chat_model_context().unwrap().is_empty());
        std::fs::remove_dir_all(&dir).ok();
    }

    /// An uncited row in the seed fails the LOAD — R10 enforced on the way
    /// in, so it can never reach the table or the export.
    #[test]
    fn an_uncited_seed_row_fails_the_load() {
        let dir = tmp_dir("uncitedseed");
        let clone = dir.join("clone");
        let seed_dir = clone.join("claude_mcp_servers").join("model_router");
        std::fs::create_dir_all(&seed_dir).unwrap();
        std::fs::write(clone.join("install.py"), "# marker").unwrap();
        std::fs::write(clone.join("vct-module.json"), r#"{"id": "orchestrator"}"#).unwrap(); // W1R-06: identity-checked cache
        std::fs::write(clone.join("CLAUDE.md"), "# marker").unwrap();
        // `resolve_orchestrator_root`'s cached-path branch also requires
        // `check_install_status` to pass, which wants an install manifest
        // (or a `.venv/`). Write the manifest — the cheaper of the two.
        std::fs::create_dir_all(clone.join("state")).unwrap();
        std::fs::write(
            clone.join("state").join("install-manifest.json"),
            r#"{"installed": true}"#,
        )
        .unwrap();
        std::fs::write(
            seed_dir.join("chat_model_context.seed.json"),
            r#"{"schema_version": 1, "models": {"guessed": {
                 "vendor": "zai", "context_window": 1000000,
                 "max_output": 128000, "window_1m": true, "source": ""}}}"#,
        )
        .unwrap();

        let db = make_db();
        db.app_state_set("launcher.install_path", &clone.display().to_string())
            .unwrap();
        let err = load_seed_rows(&db).expect_err("uncited seed row must fail");
        assert!(err.contains("source citation is required"), "got: {}", err);
        std::fs::remove_dir_all(&dir).ok();
    }

    // ── v0.2.101 (Q6/G1): the model-picker provenance log ────────────────

    /// ONE log-line shape, and every one of the three insertion paths can
    /// produce it with its own source id and the row's identity. The line
    /// is what the next duplicate picker row gets traced through, so the
    /// format itself is pinned here.
    #[test]
    fn provenance_line_has_one_shape_for_all_three_sources() {
        for source in [
            PROVENANCE_SOURCE_CATALOG_SYNC,
            PROVENANCE_SOURCE_GUI_ADD,
            PROVENANCE_SOURCE_RESEED_IMPORT,
        ] {
            let line = model_picker_row_provenance_line(source, "glm-5.3", "inserted");
            assert_eq!(
                line,
                format!("[vct] model-picker row: model=glm-5.3 source={} action=inserted", source),
                "one shape, parameterised only by source"
            );
        }
        // The three sources are three DISTINCT ids — collapsing two of them
        // would make the log untraceable exactly when it is needed.
        let ids = [
            PROVENANCE_SOURCE_CATALOG_SYNC,
            PROVENANCE_SOURCE_GUI_ADD,
            PROVENANCE_SOURCE_RESEED_IMPORT,
        ];
        assert_eq!(ids.iter().collect::<std::collections::HashSet<_>>().len(), 3);
    }

    /// The GUI-add path logs the row it just wrote (identity + action).
    /// Real capture of the tracing output (same CaptureLogs shape as
    /// `upstream_fetch.rs` tests) — this is the emission proof, not a
    /// code reading.
    #[cfg(unix)]
    #[test]
    #[serial_test::serial]
    fn gui_add_path_emits_provenance_for_the_written_row() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let dir = tmp_dir("prov-gui");
        std::env::set_var(EXPORT_PATH_ENV, dir.join("chat_model_context.json"));
        let db = make_db();

        let logs = capture_tracing(|| {
            let mutation = upsert_and_export(&db, input("glm-5.3")).expect("upsert");
            assert_eq!(mutation.row.as_ref().unwrap().model_id, "glm-5.3");
        });
        std::env::remove_var(EXPORT_PATH_ENV);
        std::fs::remove_dir_all(&dir).ok();

        assert!(
            logs.iter().any(|l| l.contains("model=glm-5.3")
                && l.contains("source=gui-add")
                && l.contains("action=upsert")),
            "the gui-add insertion path must emit its provenance line; got: {:?}",
            logs
        );
    }

    /// A clone fixture whose seed carries one citable model — the shape the
    /// catalog-sync and reseed-import emission tests share.
    #[cfg(unix)]
    fn clone_with_one_seed_model(dir: &Path) -> PathBuf {
        let clone = dir.join("clone");
        let seed_dir = clone.join("claude_mcp_servers").join("model_router");
        std::fs::create_dir_all(&seed_dir).unwrap();
        std::fs::write(clone.join("install.py"), "# marker").unwrap();
        std::fs::write(clone.join("vct-module.json"), r#"{"id": "orchestrator"}"#).unwrap();
        std::fs::write(clone.join("CLAUDE.md"), "# marker").unwrap();
        std::fs::create_dir_all(clone.join("state")).unwrap();
        std::fs::write(
            clone.join("state").join("install-manifest.json"),
            r#"{"installed": true}"#,
        )
        .unwrap();
        std::fs::write(
            seed_dir.join("chat_model_context.seed.json"),
            r#"{"schema_version": 1, "models": {
                 "glm-5.3": {"vendor": "zai", "context_window": 200000,
                             "max_output": 128000, "window_1m": false,
                             "source": "https://docs.z.ai/guides/llm/glm-5.3"}}}"#,
        )
        .unwrap();
        clone
    }

    /// The boot converge (the gateway catalog seed syncing into the table)
    /// emits one provenance line per row it wrote.
    #[cfg(unix)]
    #[test]
    #[serial_test::serial]
    fn catalog_sync_path_emits_provenance_on_boot_converge() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let dir = tmp_dir("prov-boot");
        let clone = clone_with_one_seed_model(&dir);
        let db = make_db();
        db.app_state_set("launcher.install_path", &clone.display().to_string())
            .unwrap();
        std::env::set_var(EXPORT_PATH_ENV, dir.join("chat_model_context.json"));

        let logs = capture_tracing(|| seed_and_export_on_boot(&db));
        std::env::remove_var(EXPORT_PATH_ENV);
        std::fs::remove_dir_all(&dir).ok();

        assert!(
            logs.iter()
                .any(|l| l.contains("model=glm-5.3") && l.contains("source=gateway-catalog-sync")),
            "the boot catalog-sync path must emit its provenance line; got: {:?}",
            logs
        );
    }

    /// The "Reseed from shipped defaults" import emits the same line shape
    /// with its own source id.
    #[cfg(unix)]
    #[test]
    #[serial_test::serial]
    fn reseed_import_path_emits_provenance() {
        let _env_lock = vct_launcher_core::test_env::env_lock();
        let dir = tmp_dir("prov-reseed");
        let clone = clone_with_one_seed_model(&dir);
        let db = make_db();
        db.app_state_set("launcher.install_path", &clone.display().to_string())
            .unwrap();
        std::env::set_var(EXPORT_PATH_ENV, dir.join("chat_model_context.json"));

        let logs = capture_tracing(|| {
            reseed_and_export(&db).expect("reseed with a pinned seed clone");
        });
        std::env::remove_var(EXPORT_PATH_ENV);
        std::fs::remove_dir_all(&dir).ok();

        assert!(
            logs.iter()
                .any(|l| l.contains("model=glm-5.3") && l.contains("source=reseed-import")),
            "the reseed-import path must emit its provenance line; got: {:?}",
            logs
        );
    }

    /// The converge/reseed paths derive their lines from the `written`
    /// record the DB layer returns — one line per entry, none for an
    /// empty record (the quiet steady-state boot).
    #[test]
    fn written_rows_derive_one_line_each() {
        let outcome = ReseedOutcome {
            inserted: 1,
            updated: 1,
            unchanged: 0,
            preserved_user_edits: 0,
            retired: 0,
            written: vec![
                ("glm-5.3".to_string(), "inserted".to_string()),
                ("glm-5.1".to_string(), "updated".to_string()),
            ],
        };
        let lines: Vec<String> = outcome
            .written
            .iter()
            .map(|(id, action)| {
                model_picker_row_provenance_line(PROVENANCE_SOURCE_CATALOG_SYNC, id, action)
            })
            .collect();
        assert_eq!(lines.len(), 2);
        assert!(lines[0].contains("model=glm-5.3"));
        assert!(lines[0].contains("action=inserted"));
        assert!(lines[1].contains("model=glm-5.1"));
        assert!(lines[1].contains("action=updated"));

        let quiet = ReseedOutcome::default();
        assert!(quiet.written.is_empty());
    }
}
